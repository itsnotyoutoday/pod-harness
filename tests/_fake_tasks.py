"""pod-control's /v1/pod/tasks/* routes, on a FakeControl (TASK-API §3, §8).

    ft = FakeTasks(fc); ft.queue.append(task); … ft.dones[id], ft.releases[id], ft.beats[id]

Tasks are handed out by claim in queue order, `capacity` at a time. A task id in `cancel`
gets {"cancel": true} on its next beat; one in `lost` gets 410. `sign` answers store:// refs
with blob links and output names with /put/<id>_<name> links. Not a test itself.
"""
from __future__ import annotations

import threading


class FakeTasks:
    def __init__(self, fc, lease_s: float = 3.0):
        self.fc = fc
        self.lease_s = lease_s
        self.queue: list = []
        self.claims: list = []
        self.beats: dict = {}
        self.starts: list = []
        self.dones: dict = {}
        self.releases: dict = {}
        self.signs: list = []
        self.events: dict = {}
        self.artifacts: dict = {}
        self.cancel: set = set()
        self.lost: set = set()
        self.start_status: dict = {}
        self.lock = threading.Lock()
        fc.routes[("POST", "/v1/pod/tasks/claim")] = self._claim
        fc.prefix_routes.append(("POST", "/v1/pod/tasks/", self._task))

    def _claim(self, req):
        with self.lock:
            self.claims.append(req["body"])
            n = int(req["body"].get("capacity") or 1)
            out, self.queue = self.queue[:n], self.queue[n:]
        return 200, {"tasks": out, "lease_s": self.lease_s}

    def _task(self, req):
        rest = req["path"][len("/v1/pod/tasks/"):]
        tid, _, verb = rest.partition("/")
        b = req["body"] or {}
        with self.lock:
            if verb == "beat":
                self.beats.setdefault(tid, []).append(b)
                if tid in self.lost:
                    return 410, {"detail": "lease lost"}
                return 200, {"cancel": tid in self.cancel}
            if verb == "start":
                self.starts.append(tid)
                return self.start_status.get(tid, 200), {"ok": True}
            if verb == "done":
                self.dones[tid] = b
                return 200, {"ok": True}
            if verb == "release":
                self.releases[tid] = b.get("reason")
                return 200, {"ok": True}
            if verb == "event":
                self.events.setdefault(tid, []).append(b.get("event"))
                return 200, {"ok": True}
            if verb == "artifact":
                self.artifacts.setdefault(tid, []).append(b)
                return 200, {"ok": True}
            if verb == "sign":
                self.signs.append((tid, b))
                urls = {r: self.fc.blob_url(r.rsplit("/", 1)[-1]) for r in b.get("refs") or []}
                put = {n: {"url": f"{self.fc.url}/put/{tid}_{n}", "ref": f"store://out/{tid}/{n}"}
                       for n in b.get("put") or []}
                return 200, {"urls": urls, "put": put}
        return 404, {"detail": "no such verb"}
