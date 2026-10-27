"""A stand-in pod-control on 127.0.0.1, for tests that need to see what the pod SENDS.

Records every POST as (path, json body). `strict` makes it answer 422 to any key it was not
built knowing, the way a control plane that validates its bodies would before it learned
about reason/stage/boot — which is what the fallback paths are tested against.

Not a test itself (no test_ prefix), so CI's `tests/test_*.py` loop does not run it.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: What a control plane knew before reason/stage and /v1/boot existed.
OLD_KEYS = {"/v1/alive": {"pod_id", "provider", "job_id", "deadline_ts"},
            "/v1/done": {"pod_id", "provider", "job_id", "rc", "status", "handoff"}}


class FakeControl:
    def __init__(self, strict: bool = False):
        self.calls: list = []
        self.strict = strict
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):          # noqa: N802 — http.server's naming
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append((self.path, body, self.headers.get("X-Podh-Token")))
                if outer.strict:
                    known = OLD_KEYS.get(self.path)
                    if known is None:
                        return self._send(404, {"detail": "not found"})
                    if set(body) - known:
                        return self._send(422, {"detail": f"unknown {sorted(set(body) - known)}"})
                self._send(200, {"ok": True})

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
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bodies(self, path: str) -> list:
        return [b for p, b, _ in self.calls if p == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
