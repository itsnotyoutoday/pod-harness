"""The pod-local API: how the app's worker talks to the harness that booted it.

    POST /busy  {"busy": bool, "detail"?}   flip at each step's start and end
    GET  /state → {drain, park, extend_until, window_until, models_rev, models, busy, detail,
                   stage, ready}
    POST /done  {"rc", "reason"?, "stage"?}  the only exit
    POST /workload/restart                   restart the workload (ComfyUI); 200 once it
                                             answers again, non-2xx with the error if not
    POST /models/fetch {"files"?: [{path, url, sha256?, size?}], "paths"?: [...]}
                                             fetch models now, mid-life (a held task's):
                                             202 at once; each path's state shows in
                                             GET /state `models`
    GET  /health                             no token

Served by podh-boot on 127.0.0.1 only (PODH_LOCAL_PORT, else an ephemeral port), behind
PODH_LOCAL_TOKEN (X-Podh-Token, or Authorization: Bearer). Both are exported to the app
stage, the workload and app.serve as PODH_LOCAL_URL / PODH_LOCAL_TOKEN.

## Why the worker reports busy here and not to the control plane

The idle clock that stops a pod is pod-control's, and it can only run on what the pod says.
The harness already heartbeats; the worker's busy flag rides on that beat instead of the
worker holding a second credential and a second connection. The other direction is the
same: drain, park and models_rev come back on the heartbeat reply, and /state hands them to
the worker so it stops claiming work the moment the app drains the pod — without asking
the app.

`podh-report busy|idle|done` wrap this for shell workers.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import heartbeat


class LocalState:
    """What the worker can read and set. Thread-safe; owned by podh-boot."""

    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.detail: Optional[str] = None
        self.busy_since: Optional[float] = None
        self.stage: Optional[str] = None
        self.ready = False
        self.models: dict = {}
        self.reply: dict = {}

    def set_busy(self, busy: bool, detail: Optional[str] = None) -> None:
        with self.lock:
            if busy and not self.busy:
                self.busy_since = time.time()
            self.busy, self.detail = bool(busy), (detail or None)
            if not busy:
                self.busy_since = None

    def set_model(self, path: str, state: str) -> None:
        with self.lock:
            self.models[path] = state

    def take_reply(self, reply: dict) -> None:
        with self.lock:
            self.reply = dict(reply)

    def snapshot(self) -> dict:
        with self.lock:
            r = self.reply
            return {"drain": bool(r.get("drain")), "park": bool(r.get("park")),
                    "extend_until": r.get("extend_until"), "models_rev": r.get("models_rev"),
                    # the end of the pod's paid keep-warm/idle window, when the control
                    # plane says (a worker claims `fits_window_s` tasks against it)
                    "window_until": r.get("window_until"),
                    "models": dict(self.models), "busy": self.busy, "detail": self.detail,
                    "stage": self.stage, "ready": self.ready}


class LocalServer:
    def __init__(self, state: LocalState, *, token: Optional[str] = None,
                 port: Optional[int] = None,
                 on_done: Optional[Callable] = None, on_busy: Optional[Callable] = None,
                 on_restart: Optional[Callable] = None,
                 on_models: Optional[Callable] = None):
        self.state = state
        self.token = token or os.environ.get("PODH_LOCAL_TOKEN") or secrets.token_hex(16)
        if port is None:
            raw = os.environ.get("PODH_LOCAL_PORT", "").strip()
            port = int(raw) if raw.isdigit() else 0
        outer = self
        self.on_done, self.on_busy, self.on_restart = on_done, on_busy, on_restart
        self.on_models = on_models

        self.active = 0
        self.active_lock = threading.Lock()

        class H(BaseHTTPRequestHandler):
            def handle_one_request(self):
                with outer.active_lock:
                    outer.active += 1
                try:
                    super().handle_one_request()
                finally:
                    with outer.active_lock:
                        outer.active -= 1

            def _authed(self) -> bool:
                got = self.headers.get("X-Podh-Token") or ""
                auth = self.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    got = got or auth[7:].strip()
                return secrets.compare_digest(got, outer.token)

            def _json(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return {}
                obj = json.loads(self.rfile.read(n))
                if not isinstance(obj, dict):
                    raise ValueError("body must be a JSON object")
                return obj

            def do_GET(self):           # noqa: N802
                if self.path == "/health":
                    return self._send(200, {"ok": True})
                if not self._authed():
                    return self._send(401, {"error": "bad or missing token"})
                if self.path == "/state":
                    return self._send(200, outer.state.snapshot())
                self._send(404, {"error": "not found"})

            def do_POST(self):          # noqa: N802
                if not self._authed():
                    return self._send(401, {"error": "bad or missing token"})
                try:
                    body = self._json()
                except ValueError as e:
                    return self._send(400, {"error": f"bad JSON: {e}"})
                if self.path == "/busy":
                    if not isinstance(body.get("busy"), bool):
                        return self._send(422, {"error": "busy must be true or false"})
                    detail = body.get("detail")
                    outer.state.set_busy(body["busy"], str(detail) if detail else None)
                    if outer.on_busy:
                        outer.on_busy()
                    return self._send(200, {"busy": body["busy"]})
                if self.path == "/workload/restart":
                    if outer.on_restart is None:
                        return self._send(503, {"error": "nothing to restart"})
                    code, obj = outer.on_restart()
                    return self._send(code, obj)
                if self.path == "/models/fetch":
                    if outer.on_models is None:
                        return self._send(503, {"error": "this pod fetches no models"})
                    files, paths = body.get("files") or [], body.get("paths") or []
                    if not isinstance(files, list) or not isinstance(paths, list):
                        return self._send(422, {"error": "files and paths must be lists"})
                    code, obj = outer.on_models(files, [str(p) for p in paths])
                    return self._send(code, obj)
                if self.path == "/done":
                    try:
                        rc = int(body.get("rc", 0))
                    except (TypeError, ValueError):
                        return self._send(422, {"error": "rc must be an integer"})
                    reason = body.get("reason")
                    stage = body.get("stage")
                    if outer.on_done is None:
                        return self._send(503, {"error": "nothing to report done to"})
                    return self._send(200, outer.on_done(rc, reason and str(reason),
                                                         stage and str(stage)))
                self._send(404, {"error": "not found"})

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "LocalServer":
        threading.Thread(target=self.server.serve_forever, daemon=True,
                         name="podh-local-api").start()
        return self

    def drain(self, timeout: float = 3.0) -> None:
        """Wait (briefly) for requests in flight — the /done that ended the pod must get its
        answer before the process exits."""
        end = time.time() + timeout
        while time.time() < end:
            with self.active_lock:
                if self.active == 0:
                    return
            time.sleep(0.05)

    def env(self) -> dict:
        return {"PODH_LOCAL_URL": self.url, "PODH_LOCAL_TOKEN": self.token}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# -- client ---------------------------------------------------------------------------------

def configured() -> bool:
    return bool(os.environ.get("PODH_LOCAL_URL"))


def call(method: str, path: str, body: Optional[dict] = None,
         timeout: float = 10) -> tuple:
    """(status, obj) from the local API; (0, None) when it does not answer."""
    url = os.environ.get("PODH_LOCAL_URL", "").rstrip("/")
    if not url:
        return 0, None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{url}{path}", data=data, method=method,
        headers={"X-Podh-Token": os.environ.get("PODH_LOCAL_TOKEN", ""),
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


class Client:
    """The local API from Python, for workers and handlers: every call is best effort and
    never raises; each returns (status, body) like `call`, status 0 when nothing answers.

        c = Client()            # PODH_LOCAL_URL / PODH_LOCAL_TOKEN
        c.busy(True, "task-1"); c.state(); c.restart_workload(); c.done(0)
    """

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None):
        self.url = (url if url is not None else os.environ.get("PODH_LOCAL_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("PODH_LOCAL_TOKEN", "")

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def call(self, method: str, path: str, body: Optional[dict] = None,
             timeout: float = 10) -> tuple:
        if not self.url:
            return 0, None
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.url}{path}", data=data, method=method,
            headers={"X-Podh-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except Exception:
                return e.code, None
        except Exception:
            return 0, None

    def health(self) -> bool:
        return self.call("GET", "/health", timeout=3)[0] == 200

    def state(self) -> dict:
        """GET /state, or {} when the API does not answer."""
        st, obj = self.call("GET", "/state", timeout=5)
        return obj if st == 200 and isinstance(obj, dict) else {}

    def busy(self, busy: bool, detail: Optional[str] = None) -> tuple:
        body: dict = {"busy": bool(busy)}
        if detail:
            body["detail"] = str(detail)[:200]
        return self.call("POST", "/busy", body, timeout=5)

    def done(self, rc: int, reason: Optional[str] = None, stage: Optional[str] = None) -> tuple:
        body: dict = {"rc": int(rc)}
        if reason:
            body["reason"] = reason
        if stage:
            body["stage"] = stage
        return self.call("POST", "/done", body)

    def restart_workload(self, timeout: float = 600) -> tuple:
        return self.call("POST", "/workload/restart", {}, timeout=timeout)

    def models_fetch(self, files: Optional[list] = None, paths: Optional[list] = None) -> tuple:
        return self.call("POST", "/models/fetch",
                         {"files": list(files or []), "paths": list(paths or [])}, timeout=30)


def start_beat(state: LocalState, interval: float = 60.0, live=None,
               stop: Optional[threading.Event] = None):
    """Heartbeat for podh-boot: /alive with the worker's busy flag, every `interval`,
    sooner when the flag flips (`wake()`), never more than once per 2 s.

    Returns (thread, wake). Quiet when the STOP_FILE exists, as every heartbeat loop is, or
    when `live()` says the workload is not answering — so a hung workload looks stuck.
    """
    url, _, pod_id = heartbeat._cfg()
    ev = threading.Event()
    stop = stop or threading.Event()
    heartbeat.on_reply(state.take_reply)
    if not url or not pod_id:
        return None, ev.set

    def loop():
        last = 0.0
        while not stop.is_set():
            if os.path.exists(heartbeat.STOP_FILE):
                return
            gap = time.time() - last
            if gap < 2.0:
                time.sleep(2.0 - gap)
            ok = True
            try:
                ok = live is None or live()
            except Exception:
                ok = True
            if ok:
                with state.lock:
                    busy, detail = state.busy, state.detail
                try:
                    heartbeat.alive(busy=busy, detail=detail)
                except Exception:
                    pass
            last = time.time()
            ev.wait(interval)
            ev.clear()

    t = threading.Thread(target=loop, daemon=True, name="podh-boot-heartbeat")
    t.start()
    return t, ev.set
