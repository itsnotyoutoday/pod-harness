"""pod-control's /v1/pod/tasks/* routes, on a FakeControl (TASK-API §3, §8, §9).

    ft = FakeTasks(fc); ft.queue.append(task); … ft.dones[id], ft.releases[id], ft.beats[id]

The shapes are pod-control's own (src/pod_control/taskapi.py and tasks.py: claim_view,
beat, start, done, release, add_artifacts, task_event, output_links, sign), so a worker
that passes against this passes against the real server; tests/test_worker_against_server.py
checks that directly. In particular:

  claim     each task comes back the way claim_view builds it: inputs {name: {url, ref?} |
            {value}} (store:// refs presigned here, as at claim), outputs {to?, keep,
            put_url?, put? {name: url} for outputs.names only, sign?}, missing_models,
            timeout_s null for none. No model links: a held task's models come from podh-boot
            (GET /v1/pod/models).
  beat      200 {"cancel": false, "state", "lease_until"}; a task id in `cancel` gets
            {"cancel": true, "state": "cancelled"}; one in `lost` gets {"cancel": true,
            "state": "queued"} (the lease lapsed and it was requeued) — 200, not an error.
            `gone[id] = code` answers that status instead (404 outside the lane).
  start     200 {"state": "running"}; cancelled or lost: 409 {"detail": {"error", "cancel"}}.
  done      200 {"accepted", "state"}; on a cancelled task {"accepted": false, "state":
            "cancelled", "cancel": true} (the body still lands in `dones`). Artifacts as pod-control keeps them (ref = outputs.to
            + name) go to `final[id]`.
  release   200 {"released": true, "state": "queued"}.
  outputs   {"names"} → {"put": {name: /put/<id>_<name>}}; 409 without outputs.to.
  sign      {"refs"} → {"urls": {ref: blob link}, "expires_s"}; anything else 422.
  event     {"data"} → {"cancel": false, "emitted": true}.
  artifacts {name, size?, sha256?, url?} or {"artifacts": [...]} → {"cancel": false,
            "artifacts": n}. Any other verb (e.g. the singular `artifact`) is a 404.

`bad` collects bodies the real server would refuse or misread. Not a test itself.
"""
from __future__ import annotations

import threading


def _detail(error: str, **extra) -> dict:
    """FastAPI's HTTPException body."""
    return {"detail": {"error": error, **extra}}


class FakeTasks:
    def __init__(self, fc, lease_s: float = 3.0):
        self.fc = fc
        self.lease_s = lease_s
        self.queue: list = []
        self.claims: list = []
        self.beats: dict = {}
        self.starts: list = []
        self.dones: dict = {}
        self.final: dict = {}
        self.releases: dict = {}
        self.signs: list = []
        self.outputs: list = []
        self.events: dict = {}
        self.artifacts: dict = {}
        self.cancel: set = set()
        self.lost: set = set()
        self.gone: dict = {}
        self.start_status: dict = {}
        self.claimed: dict = {}             # id → the task as the claim gave it
        self.bad: list = []                 # (verb, body) the real server would refuse
        self.lock = threading.Lock()
        fc.routes[("POST", "/v1/pod/tasks/claim")] = self._claim
        fc.prefix_routes.append(("POST", "/v1/pod/tasks/", self._task))

    # -- claim ------------------------------------------------------------------------------
    def _input(self, v):
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and v.startswith("store://"):
            return {"url": self.fc.blob_url(v.rsplit("/", 1)[-1], sig="claim"), "ref": v}
        return {"url": v}

    def _view(self, t: dict) -> dict:
        out = dict(t.get("outputs") or {})
        names = out.pop("names", None) or []
        if out.get("to") and not str(out["to"]).endswith("/"):
            out["to"] = out["to"] + "/"
        out.setdefault("keep", [])
        if names:
            out["put"] = {**{n: f"{self.fc.url}/put/{t['task_id']}_{n}" for n in names},
                          **(out.get("put") or {})}
        if out.get("to"):
            out["sign"] = f"/v1/pod/tasks/{t['task_id']}/outputs"
        v = {"lane": "test", "pool": "p", "state": "claimed", "attempt": 1,
             "lease_s": self.lease_s, "lease_until": 0, "timeout_s": None, "needs": {},
             "workflow": {}, "payload": {}, "labels": {}, "held_reason": None,
             "missing_models": [], **t}
        v["inputs"] = {k: self._input(x) for k, x in (t.get("inputs") or {}).items()}
        v["outputs"] = out
        if v["state"] == "held":
            v["held_reason"] = "pod_fetch"
            v["missing_models"] = v["missing_models"] or [
                m.get("path") if isinstance(m, dict) else m
                for m in (v["needs"].get("models") or [])]
        return v

    def _claim(self, req):
        b = req["body"] or {}
        with self.lock:
            self.claims.append(b)
            n = b.get("capacity", 1)
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 16:
                return 422, _detail("capacity must be a whole number 0-16")
            if not isinstance(b.get("have") or {}, dict):
                return 422, _detail("have must be an object")
            out, self.queue = self.queue[:n], self.queue[n:]
            views = [self._view(t) for t in out]
            for v in views:
                self.claimed[v["task_id"]] = v
        return 200, {"tasks": views, "lease_s": max([v["lease_s"] for v in views],
                                                    default=self.lease_s)}

    # -- per task ---------------------------------------------------------------------------
    def _state(self, tid: str) -> str:
        if tid in self.cancel:
            return "cancelled"
        if tid in self.lost:
            return "queued"
        return ""

    def _task(self, req):
        rest = req["path"][len("/v1/pod/tasks/"):]
        tid, _, verb = rest.partition("/")
        b = req["body"] or {}
        with self.lock:
            if tid in self.gone:
                return self.gone[tid], _detail(f"no task {tid}")
            gone = self._state(tid)
            to = str(((self.claimed.get(tid) or {}).get("outputs") or {}).get("to") or "")
            if verb == "beat":
                self.beats.setdefault(tid, []).append(b)
                if gone:
                    return 200, {"cancel": True, "state": gone}
                return 200, {"cancel": False, "state": "running", "lease_until": 0}
            if verb == "start":
                self.starts.append(tid)
                if gone:
                    return 409, _detail(f"task {tid} is {gone}", cancel=True)
                code = self.start_status.get(tid, 200)
                return code, ({"state": "running", "lease_until": 0} if code == 200 else
                              _detail("try again"))
            if verb == "done":
                if not isinstance(b.get("artifacts", []), list):
                    self.bad.append((verb, b))
                    return 422, _detail("artifacts must be a list")
                self.dones[tid] = b             # what the pod said, accepted or not
                if gone:
                    return 200, {"accepted": False, "state": gone, "cancel": gone == "cancelled"}
                self.final[tid] = [self._artifact(a, to) for a in b.get("artifacts") or []]
                state = "succeeded" if b.get("ok") is True else (
                    "queued" if b.get("retryable") is True else "failed")
                return 200, {"accepted": True, "state": state}
            if verb == "release":
                if gone:
                    return 200, {"released": False, "state": gone}
                self.releases[tid] = b.get("reason")
                return 200, {"released": True, "state": "queued"}
            if verb == "event":
                if gone:
                    return 200, {"cancel": True, "state": gone, "emitted": False}
                if "data" not in b:
                    self.bad.append((verb, b))
                self.events.setdefault(tid, []).append(b.get("data", b))
                return 200, {"cancel": False, "emitted": True}
            if verb == "artifacts":
                if gone:
                    return 200, {"cancel": True, "state": gone}
                items = b.get("artifacts") if "artifacts" in b else [b]
                self.artifacts.setdefault(tid, []).extend(
                    self._artifact(a, to) for a in items)
                return 200, {"cancel": False, "artifacts": len(self.artifacts[tid])}
            if verb == "outputs":
                self.outputs.append((tid, b))
                if gone:
                    return 409, _detail(f"task {tid} is not running on this pod", cancel=True)
                names = b.get("names")
                if not isinstance(names, list) or not names:
                    self.bad.append((verb, b))
                    return 422, _detail("names: 1-200 output names")
                if not to:
                    return 409, _detail("this task's outputs are not a store prefix")
                return 200, {"put": {n: f"{self.fc.url}/put/{tid}_{n}" for n in names}}
            if verb == "sign":
                self.signs.append((tid, b))
                if gone:
                    return 409, _detail(f"task {tid} is {gone}", cancel=True)
                refs = b.get("refs")
                if not isinstance(refs, list) or not refs:
                    self.bad.append((verb, b))
                    return 422, _detail("refs: 1-200 store:// refs")
                return 200, {"urls": {r: self.fc.blob_url(r.rsplit("/", 1)[-1]) for r in refs},
                             "expires_s": 3600}
        self.bad.append((verb, b))
        return 404, {"detail": "Not Found"}

    def _artifact(self, a, to: str) -> dict:
        """What pod-control keeps for an artifact: its ref is outputs.to + name."""
        out = {k: a[k] for k in ("name", "size", "sha256") if k in a}
        if to:
            out["ref"] = to + str(a.get("name"))
        elif a.get("url"):
            out["url"] = a["url"]
        else:
            self.bad.append(("artifact", a))
        return out
