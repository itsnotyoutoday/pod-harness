"""`ctx`: what a handler gets for one task (TASK-API §5, §8).

    ctx.task, ctx.task_id, ctx.payload, ctx.models_root, ctx.app_root, ctx.workdir
    ctx.inputs            {name: local Path}, fetched and verified before prepare()
    ctx.comfy             a pod_harness.comfy.ComfyClient wired to this task (cancel,
                          timeout, progress, restart): ctx.comfy.run(graph, timeout_s=, stall_s=)
    ctx.progress(f, msg)  0..1, carried on the next beat
    ctx.log(msg)          the worker's log; the tail goes with a failure
    ctx.emit({...})       a task.event (≤ 1/s, ≤ 16 KB)
    ctx.fetch(url | store://ref, dest=None) → Path
    ctx.upload(path, name) → {"name", "ref"}   (mid-run too)
    ctx.restart_workload() → bool
    ctx.cancelled() → bool;  ctx.check() raises Cancelled (or TaskFailed on a timeout)
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .. import fetch, local
from ..comfy import Cancelled, ComfyClient
from . import api
from .handlers import TaskFailed

MAX_EVENT = 16 * 1024
UPLOAD_ATTEMPTS = 4


def _sha_size(path: Path) -> tuple:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def put_file(url: str, path: Path, timeout: float = 600) -> int:
    """PUT a file to a presigned URL (no pod token). Returns the HTTP status; raises on
    no answer."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="PUT",
                                     headers={"Content-Length": str(size),
                                              "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


class TaskContext:
    def __init__(self, task: dict, *, workdir: Path, models_root: Path, app_root: Path,
                 stall_s: Optional[float] = None, interrupt_grace: float = 20.0,
                 local_client: Optional[local.Client] = None, log=None, use_ws: bool = True):
        self.task = task
        self.task_id = str(task.get("task_id"))
        self.kind = task.get("kind")
        self.payload = task.get("payload") or {}
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.models_root = Path(models_root)
        self.app_root = Path(app_root)
        self.stall_s = float(task.get("stall_s") or stall_s or 0) or None
        self.inputs: dict = {}
        self.artifacts: list = []
        self.local = local_client or local.Client()
        self._log = log or (lambda t: print(t, flush=True))
        self.lines: collections.deque = collections.deque(maxlen=60)
        self.fraction, self.message = 0.0, ""
        self.last_progress = time.time()
        self.stop_reason: Optional[str] = None     # cancelled | lost | timeout
        self.stop_event = threading.Event()
        self._last_emit = 0.0
        self._lock = threading.Lock()
        self.comfy = ComfyClient(should_stop=lambda: self.stop_reason,
                                 on_progress=self.progress, restart=self.restart_workload,
                                 log=self.log, interrupt_grace=interrupt_grace, use_ws=use_ws)

    # -- reporting --------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"[podh-worker {self.task_id}] {msg}"
        self.lines.append(line)
        self._log(line)

    def log_tail(self, limit: int = 1024) -> str:
        return "\n".join(self.lines)[-limit:]

    def progress(self, fraction: float, message: str = "") -> None:
        try:
            f = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            return
        with self._lock:
            self.fraction, self.message = f, str(message or "")[:300]
            self.last_progress = time.time()

    def emit(self, event: dict) -> bool:
        """A task.event for the app. Waits out the 1/s limit rather than dropping one."""
        if not isinstance(event, dict):
            raise TypeError("emit takes a dict")
        if len(json.dumps(event)) > MAX_EVENT:
            raise ValueError(f"an event is at most {MAX_EVENT} bytes")
        gap = time.time() - self._last_emit
        if gap < 1.0:
            time.sleep(1.0 - gap)
        self._last_emit = time.time()
        st, _ = api.event(self.task_id, event)
        return 200 <= st < 300

    # -- stopping ---------------------------------------------------------------------------
    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            self.stop_event.set()

    def cancelled(self) -> bool:
        return self.stop_reason in ("cancelled", "lost")

    def check(self) -> None:
        """Raise if the task must stop: Cancelled on a cancel (final), TaskFailed on the
        task's timeout. Call it between steps of a long handler."""
        r = self.stop_reason
        if r in ("cancelled", "lost"):
            raise Cancelled(r)
        if r == "timeout":
            raise TaskFailed("the task passed its timeout_s", "timeout")

    def restart_workload(self) -> bool:
        st, obj = self.local.restart_workload()
        self.log(f"workload restart: {st} {obj if st != 200 else ''}".rstrip())
        return st == 200

    # -- files ------------------------------------------------------------------------------
    def fetch(self, src, dest=None, *, sha256: Optional[str] = None,
              size: Optional[int] = None) -> Path:
        """A URL, or a store:// ref signed through pod-control, to `dest` (default: the
        workdir under the name's last part). Resumable and verified."""
        url = src.get("url") if isinstance(src, dict) else str(src)
        if isinstance(src, dict):
            sha256 = sha256 or src.get("sha256")
            size = size if size is not None else src.get("size")
        if url and url.startswith("store://"):
            st, obj = api.sign(self.task_id, refs=[url])
            got = ((obj or {}).get("urls") or {}).get(url) if st == 200 else None
            if isinstance(got, dict):
                got = got.get("url")
            if not got:
                raise TaskFailed(f"could not sign {url}: pod-control answered {st}", "inputs")
            name, url = url.rstrip("/").rsplit("/", 1)[-1], got
        else:
            name = (url or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "download"
        dest = Path(dest) if dest else self.workdir / name
        try:
            return fetch.download(url, dest, size=size, sha256=sha256)
        except fetch.DownloadError as e:
            raise TaskFailed(f"fetch {name}: {e}", "inputs")

    def _put_link(self, name: str, fresh: bool = False) -> tuple:
        """(url, ref) to upload `name` to: the claim's outputs.put[name], else
        outputs.put_url ("{name}" filled in), else asked of pod-control (sign put)."""
        out = self.task.get("outputs") or {}
        ref = None
        if out.get("to"):
            ref = str(out["to"]).rstrip("/") + "/" + name
        if not fresh:
            link = (out.get("put") or {}).get(name) if isinstance(out.get("put"), dict) else None
            if isinstance(link, dict):
                return link.get("url"), link.get("ref") or ref
            if isinstance(link, str):
                return link, ref
            if out.get("put_url"):
                u = str(out["put_url"])
                return (u.replace("{name}", name) if "{name}" in u else u), ref
        st, obj = api.sign(self.task_id, put=[name])
        link = ((obj or {}).get("put") or {}).get(name) if st == 200 else None
        if isinstance(link, dict):
            return link.get("url"), link.get("ref") or ref
        return link, ref

    def upload(self, path, name: Optional[str] = None) -> dict:
        """Upload `path` as artifact `name` (stable, the app may cite it). Retried; a refused
        link is asked for again. Returns {"name", "ref"}."""
        path = Path(path)
        name = name or path.name
        if "/" in name or name in ("", ".", ".."):
            raise TaskFailed(f"artifact name {name!r} must be a plain file name", "upload")
        sha, size = _sha_size(path)
        url, ref = self._put_link(name)
        err = "no upload link for it"
        for i in range(UPLOAD_ATTEMPTS):
            if not url:
                break
            try:
                st = put_file(url, path)
            except Exception as e:
                st, err = 0, f"{type(e).__name__}: {e}"
            if 200 <= st < 300:
                art = {"name": name, "ref": ref, "size": size, "sha256": sha}
                with self._lock:
                    self.artifacts = [a for a in self.artifacts if a["name"] != name] + [art]
                api.artifact(self.task_id, art)
                self.log(f"uploaded {name} ({size} bytes)")
                return {"name": name, "ref": ref}
            if st:
                err = f"HTTP {st}"
            if st in (401, 403):
                url, ref = self._put_link(name, fresh=True)
            time.sleep(min(0.5 * 2 ** i, 8))
        raise TaskFailed(f"upload {name}: {err}", "upload")
