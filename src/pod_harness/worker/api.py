"""The task calls to pod-control (TASK-API §3, §8), over heartbeat.request: same token, same
pin, never raises. Each returns (status, body); status 0 is "no answer".

    POST /v1/pod/tasks/claim         {"capacity", "have": {"models", "loaded"}, "nvenc",
                                      "kinds"?, "window_s"?}  → {"tasks": [...], "lease_s"}
    POST /v1/pod/tasks/{id}/beat     {"progress", "message"}   → {"cancel": bool}
    POST /v1/pod/tasks/{id}/start
    POST /v1/pod/tasks/{id}/done     {"ok", "result"?, "artifacts", "error"?, "usage"?}
    POST /v1/pod/tasks/{id}/release  {"reason"}
    POST /v1/pod/tasks/{id}/sign     {"refs"?: [store://…], "put"?: [name]}
                                      → {"urls": {ref: url}, "put": {name: url | {url, ref}}}
    POST /v1/pod/tasks/{id}/event    {"event": {...}}           (ctx.emit, ≤ 1/s)
    POST /v1/pod/tasks/{id}/artifact {"name", "ref", "size", "sha256"}   (a mid-run upload)

`kinds` and `window_s` are sent only when there is something to say, so a server that does
not know them is not asked to.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Optional

from .. import heartbeat

#: Answers that mean the task is no longer this pod's: stop work, report nothing.
LOST = (404, 409, 410)


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
         usage: Optional[dict] = None, attempts: int = 5) -> tuple:
    """Final; retried while the answer is no answer or a 5xx, since it is the report that
    frees the task."""
    body: dict = {"ok": bool(ok), "artifacts": artifacts}
    if result is not None:
        body["result"] = result
    if error:
        body["error"] = error
    if usage:
        body["usage"] = usage
    st, obj = 0, None
    for i in range(attempts):
        st, obj = heartbeat.request("POST", _p(task_id, "done"), body, timeout=30)
        if st and st < 500:
            break
        time.sleep(min(0.5 * 2 ** i, 8))
    return st, obj


def release(task_id: str, reason: str) -> tuple:
    st, obj = 0, None
    for i in range(3):
        st, obj = heartbeat.request("POST", _p(task_id, "release"), {"reason": reason[:300]},
                                    timeout=15)
        if st and st < 500:
            break
        time.sleep(0.5 * 2 ** i)
    return st, obj


def sign(task_id: str, refs: Optional[list] = None, put: Optional[list] = None) -> tuple:
    body: dict = {}
    if refs:
        body["refs"] = list(refs)
    if put:
        body["put"] = list(put)
    return heartbeat.request("POST", _p(task_id, "sign"), body, timeout=30)


def event(task_id: str, ev: dict) -> tuple:
    return heartbeat.request("POST", _p(task_id, "event"), {"event": ev}, timeout=15)


def artifact(task_id: str, art: dict) -> tuple:
    return heartbeat.request("POST", _p(task_id, "artifact"),
                             {k: art.get(k) for k in ("name", "ref", "size", "sha256")},
                             timeout=15)
