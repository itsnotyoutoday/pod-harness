"""The task calls to pod-control (TASK-API §3, §8, §9), over heartbeat.request: same token,
same pin, never raises. Each returns (status, body); status 0 is "no answer". The shapes
are pod-control's (src/pod_control/taskapi.py, tasks.py):

    POST /v1/pod/tasks/claim         {"capacity", "have": {"models", "loaded"}, "nvenc",
                                      "kinds"?, "window_s"?}
        → {"tasks": [task], "lease_s", "draining"?}; task = {task_id, kind, lane, pool,
          state (claimed|held), attempt, lease_s, lease_until, timeout_s (null: none), needs,
          workflow, payload, labels, inputs {name: {url, ref?} | {value}},
          outputs {to?, keep, put_url?, put? {name: url}, sign?}, held_reason,
          missing_models [path]}
    POST /v1/pod/tasks/{id}/beat     {"progress", "message"}
        → {"cancel": bool, "state", "lease_until"?}; cancel with state != "cancelled" means
          the lease is gone (requeued, or another pod's)
    POST /v1/pod/tasks/{id}/start    → {"state": "running"}; 409 {cancel: true} if not ours
    POST /v1/pod/tasks/{id}/done     {"ok", "result"?, "artifacts", "error"? {stage, reason,
                                      log_tail}, "usage"? {vram_peak_mb, phases}, "retryable"?}
        → {"accepted", "state", "cancel"?}
    POST /v1/pod/tasks/{id}/release  {"reason"} → {"released", "state"}; only "no handler"
                                      counts toward max_releases
    POST /v1/pod/tasks/{id}/outputs  {"names": [...]} → {"put": {name: url}} (under outputs.to)
    POST /v1/pod/tasks/{id}/sign     {"refs": [store://…]} → {"urls": {ref: url}, "expires_s"}
    POST /v1/pod/tasks/{id}/event    {"data": {...}} → {"emitted", "cancel"} (≤ 1/s, ≤ 16 KB)
    POST /v1/pod/tasks/{id}/artifacts {"name", "size"?, "sha256"?, "url"?} → {"cancel", ...}

`kinds` and `window_s` are sent only when there is something to say; pod-control ignores
keys it does not use.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Optional

from .. import heartbeat

#: Answers that mean the task is no longer this pod's: stop work, report nothing. pod-control
#: answers 404 for a task outside the pod's lane and 409 {cancel: true} to start/sign on a
#: task not bound to it; beat/artifacts/event answer 200 {cancel: true, state} instead.
LOST = (404, 409, 410)


def gone(obj) -> Optional[str]:
    """For a 200 reply with cancel: "cancelled" (final, report it) or "lost" (the lease is
    gone: the task was requeued or is another pod's; report nothing). None: still ours."""
    if not isinstance(obj, dict) or not obj.get("cancel"):
        return None
    return "cancelled" if obj.get("state") in (None, "cancelled") else "lost"


def _p(task_id: str, verb: str) -> str:
    return f"/v1/pod/tasks/{urllib.parse.quote(str(task_id), safe='')}/{verb}"


def claim(capacity: int, models: list, loaded: list, nvenc: bool,
          kinds: Optional[list] = None, window_s: Optional[float] = None) -> tuple:
    body: dict = {"capacity": int(capacity), "have": {"models": models, "loaded": loaded},
                  "nvenc": bool(nvenc)}
    if kinds:
        body["kinds"] = list(kinds)
    if window_s is not None:
        body["window_s"] = int(window_s)
    return heartbeat.request("POST", "/v1/pod/tasks/claim", body, timeout=30)


def beat(task_id: str, progress: float, message: str = "") -> tuple:
    return heartbeat.request("POST", _p(task_id, "beat"),
                             {"progress": round(float(progress), 4), "message": message[:300]},
                             timeout=15)


def start(task_id: str) -> tuple:
    return heartbeat.request("POST", _p(task_id, "start"), {}, timeout=15)


def done(task_id: str, ok: bool, artifacts: list, result=None, error: Optional[dict] = None,
         usage: Optional[dict] = None, retryable: bool = False, attempts: int = 5) -> tuple:
    """Final; retried while the answer is no answer or a 5xx, since it is the report that
    frees the task."""
    body: dict = {"ok": bool(ok), "artifacts": [_art(a) for a in artifacts]}
    if result is not None:
        body["result"] = result
    if error:
        body["error"] = error
    if usage:
        body["usage"] = usage
    if retryable and not ok:
        body["retryable"] = True
    st, obj = 0, None
    for i in range(attempts):
        st, obj = heartbeat.request("POST", _p(task_id, "done"), body, timeout=30)
        if st and st < 500:
            break
        time.sleep(min(0.5 * 2 ** i, 8))
    return st, obj


def _art(a: dict) -> dict:
    """An artifact as pod-control takes it: {name, size?, sha256?, url?}. Its ref is the
    server's to make (outputs.to + name); a url is given only when there is no store prefix."""
    return {k: a[k] for k in ("name", "size", "sha256", "url") if a.get(k) is not None}


def release(task_id: str, reason: str) -> tuple:
    st, obj = 0, None
    for i in range(3):
        st, obj = heartbeat.request("POST", _p(task_id, "release"), {"reason": reason[:300]},
                                    timeout=15)
        if st and st < 500:
            break
        time.sleep(0.5 * 2 ** i)
    return st, obj


def outputs(task_id: str, names: list) -> tuple:
    """Presigned PUTs for output names chosen at run time (under the task's outputs.to)."""
    return heartbeat.request("POST", _p(task_id, "outputs"), {"names": list(names)}, timeout=30)


def sign(task_id: str, refs: list) -> tuple:
    """Presigned GETs for store:// refs the task may read."""
    return heartbeat.request("POST", _p(task_id, "sign"), {"refs": list(refs)}, timeout=30)


def event(task_id: str, data: dict) -> tuple:
    return heartbeat.request("POST", _p(task_id, "event"), {"data": data}, timeout=15)


def artifact(task_id: str, art: dict) -> tuple:
    """A mid-run upload: shows in the task at once, with a task.artifact event."""
    return heartbeat.request("POST", _p(task_id, "artifacts"),
                             {k: art[k] for k in ("name", "size", "sha256", "url")
                              if art.get(k) is not None}, timeout=15)
