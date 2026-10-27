"""A stand-in pod-control on 127.0.0.1, for tests that need to see what the pod SENDS.

Records every POST as (path, json body, token) in `calls`, and every request of any method
in `requests`. `strict` makes it answer 422 to any key it was not built knowing, the way a
control plane that validates its bodies would before it learned about reason/stage/boot —
which is what the fallback paths are tested against.

For podh-boot it also plays the parts outside pod-control:

  routes    {(METHOD, path): (code, obj) | fn(req) -> (code, obj)} — /v1/pod/* answers
  alive_reply   what POST /v1/alive answers (drain, park, models_rev, …)
  blobs     {name: bytes} served at GET /blob/<name>?sig=S, with Range support — a stand-in
            for presigned bucket URLs. `expired` sigs answer 403; `truncate[name] = n`
            sends n bytes of the first response and then drops the connection;
            `ignore_range` answers 200 with the whole body to a ranged request.
  PUT /put/<name> stores the body in blobs (a presigned PUT); `put_fail[name] = n` answers
            the first n PUTs of that name with 500, `put_refuse[name] = n` with 403.
  prefix_routes [(METHOD, path prefix, fn(req) -> (code, obj))] — for routes with ids in
            them (/v1/pod/tasks/{id}/beat); tried after `routes`.

Not a test itself (no test_ prefix), so CI's `tests/test_*.py` loop does not run it.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: What a control plane knew before reason/stage and /v1/boot existed.
OLD_KEYS = {"/v1/alive": {"pod_id", "provider", "job_id", "deadline_ts"},
            "/v1/done": {"pod_id", "provider", "job_id", "rc", "status", "handoff"}}


class FakeControl:
    def __init__(self, strict: bool = False):
        self.calls: list = []
        self.requests: list = []
        self.strict = strict
        self.routes: dict = {}
        self.alive_reply: dict = {"ok": True}
        self.blobs: dict = {}
        self.expired: set = set()
        self.truncate: dict = {}
        self.ignore_range = False
        self.blob_gets: list = []
        self.put_fail: dict = {}
        self.put_refuse: dict = {}
        self.prefix_routes: list = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _req(self, body):
                u = urllib.parse.urlsplit(self.path)
                return {"method": self.command, "path": u.path,
                        "query": dict(urllib.parse.parse_qsl(u.query)), "body": body,
                        "headers": {k.lower(): v for k, v in self.headers.items()}}

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _route(self, req):
                r = outer.routes.get((req["method"], req["path"]))
                if r is None:
                    for m, prefix, fn in outer.prefix_routes:
                        if m == req["method"] and req["path"].startswith(prefix):
                            return fn(req)
                    return None
                return r(req) if callable(r) else r

            def do_GET(self):           # noqa: N802 — http.server's naming
                req = self._req(None)
                with outer.lock:
                    outer.requests.append(req)
                if req["path"].startswith("/blob/"):
                    return self._blob(req)
                r = self._route(req)
                if r is None:
                    return self._send(404, {"detail": "not found"})
                self._send(*r)

            def do_PUT(self):           # noqa: N802
                data = self._body()
                req = self._req(data)
                with outer.lock:
                    outer.requests.append(req)
                if req["path"].startswith("/put/"):
                    name = req["path"][len("/put/"):]
                    with outer.lock:
                        if outer.put_fail.get(name):
                            outer.put_fail[name] -= 1
                            return self._send(500, {"detail": "try again"})
                        if outer.put_refuse.get(name):
                            outer.put_refuse[name] -= 1
                            return self._send(403, {"detail": "Request has expired"})
                    if req["headers"].get("x-podh-token"):
                        return self._send(400, {"detail": "a bucket must never see the pod token"})
                    outer.blobs[name] = data
                    return self._send(200, {"ok": True})
                self._send(404, {"detail": "not found"})

            def do_POST(self):          # noqa: N802
                body = json.loads(self._body() or b"{}")
                req = self._req(body)
                with outer.lock:
                    outer.requests.append(req)
                    outer.calls.append((req["path"], body, self.headers.get("X-Podh-Token")))
                if outer.strict:
                    known = OLD_KEYS.get(req["path"])
                    if known is None:
                        return self._send(404, {"detail": "not found"})
                    if set(body) - known:
                        return self._send(422, {"detail": f"unknown {sorted(set(body) - known)}"})
                r = self._route(req)
                if r is not None:
                    return self._send(*r)
                if req["path"] == "/v1/alive":
                    return self._send(200, dict(outer.alive_reply))
                self._send(200, {"ok": True})

            def _blob(self, req):
                name = req["path"][len("/blob/"):]
                with outer.lock:
                    outer.blob_gets.append((name, req["query"].get("sig"),
                                            req["headers"].get("range")))
                if req["query"].get("sig") in outer.expired:
                    return self._send(403, {"detail": "Request has expired"})
                if req["headers"].get("x-podh-token"):
                    return self._send(400, {"detail": "a bucket must never see the pod token"})
                data = outer.blobs.get(name)
                if data is None:
                    return self._send(404, {"detail": "no such blob"})
                start, code = 0, 200
                rng = req["headers"].get("range")
                if rng and not outer.ignore_range:
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    code = 206
                part = data[start:]
                self.send_response(code)
                self.send_header("Content-Length", str(len(part)))
                if code == 206:
                    self.send_header("Content-Range",
                                     f"bytes {start}-{len(data) - 1}/{len(data)}")
                self.end_headers()
                cut = outer.truncate.pop(name, None)
                if cut is not None:
                    self.wfile.write(part[:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return
                try:
                    self.wfile.write(part)
                except (BrokenPipeError, ConnectionResetError):
                    pass                # the client gave up on purpose (a test of that)

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):  # keep test output readable
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def blob_url(self, name: str, sig: str = "ok") -> str:
        return f"{self.url}/blob/{name}?sig={sig}"

    def bodies(self, path: str) -> list:
        return [b for p, b, _ in self.calls if p == path]

    def gets(self, path: str) -> list:
        return [r for r in self.requests if r["method"] == "GET" and r["path"] == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
