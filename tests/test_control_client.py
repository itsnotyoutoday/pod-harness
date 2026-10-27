"""The control-plane client underneath every pod report: generic requests, /alive's busy flag
and reply hook, /done's log tail, and the certificate pin checked BEFORE the token is sent.

Against fakes on 127.0.0.1 (plain HTTP, and a local TLS server with a throwaway
self-signed certificate when openssl is available); nothing leaves the machine.
"""
import hashlib
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


from _fake_control import FakeControl  # noqa: E402
from pod_harness import heartbeat  # noqa: E402

tmp = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(tmp / "stop")
for k in ("PODH_CONTROL_FINGERPRINT", "PODH_JOB_ID"):
    os.environ.pop(k, None)
fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="tok", RUNPOD_POD_ID="pod1",
                  PODH_CONTROL_JOB_ID="job_1")

# -- request() ----------------------------------------------------------------------------
fc.routes[("GET", "/v1/pod/spec")] = (200, {"boot": {"gpu_check": False}})
st, obj = heartbeat.request("GET", "/v1/pod/spec", query={"a": 1, "b": None})
check("request() GETs and parses", st == 200 and obj == {"boot": {"gpu_check": False}})
r = fc.gets("/v1/pod/spec")[-1]
check("...with the token, and None query values dropped",
      r["headers"].get("x-podh-token") == "tok" and r["query"] == {"a": "1"})
st, obj = heartbeat.request("GET", "/v1/pod/nothing")
check("a 404 is an answer, not an exception", st == 404 and obj == {"detail": "not found"})
os.environ["PODH_CONTROL_URL"] = "http://127.0.0.1:9"
st, obj = heartbeat.request("GET", "/v1/pod/spec", timeout=2)
check("unreachable is status 0, with a reason", st == 0 and obj is None and heartbeat.last_error())
os.environ["PODH_CONTROL_URL"] = fc.url

# -- alive: busy/detail and the reply -------------------------------------------------------
seen = []
heartbeat.on_reply(seen.append)
fc.alive_reply = {"extend_until": "2026-10-27T18:00:00Z", "drain": False, "park": False,
                  "models_rev": 3}
r = heartbeat.alive(busy=True, detail="step 4/9 " + "x" * 500)
b = fc.bodies("/v1/alive")[-1]
check("alive carries busy and a clipped detail",
      b["busy"] is True and len(b["detail"]) == heartbeat.MAX_DETAIL)
check("the reply is kept for last_reply()", heartbeat.last_reply().get("models_rev") == 3)
check("...and handed to subscribers", seen and seen[-1]["extend_until"].startswith("2026"))
heartbeat.alive()
b = fc.bodies("/v1/alive")[-1]
check("an old-style alive sends no busy key", "busy" not in b and "detail" not in b)


def boom(_):
    raise RuntimeError("subscriber bug")


heartbeat.on_reply(boom)
check("a broken subscriber does not break alive", heartbeat.alive(busy=False) is not None)
heartbeat._REPLY_HOOKS.clear()

old = FakeControl(strict=True)
os.environ["PODH_CONTROL_URL"] = old.url
r = heartbeat.alive(busy=True, detail="x")
sent = old.bodies("/v1/alive")
check("a server that refuses busy still gets the beat, without it",
      r is not None and len(sent) == 2 and "busy" not in sent[1])
old.close()
os.environ["PODH_CONTROL_URL"] = fc.url

# -- done: log_tail ---------------------------------------------------------------------------
heartbeat.done(1, reason="workload died", stage="workload",
               log_tail="line\n" * 1000 + "Traceback: the end")
b = fc.bodies("/v1/done")[-1]
check("done carries the END of the log, ≤1 KB, newlines kept",
      len(b["log_tail"]) == heartbeat.MAX_LOG_TAIL and b["log_tail"].endswith("the end")
      and "\n" in b["log_tail"])

# -- the pin is checked before a byte of the request ------------------------------------------
if shutil.which("openssl"):
    key, crt = tmp / "k.pem", tmp / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert(crt.read_text())
    fp = hashlib.sha256(der).hexdigest()
    got_tokens = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):          # noqa: N802
            got_tokens.append(self.headers.get("X-Podh-Token"))
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            data = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(crt), str(key))
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["PODH_CONTROL_URL"] = f"https://127.0.0.1:{srv.server_address[1]}"

    os.environ["PODH_CONTROL_FINGERPRINT"] = ":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper()
    check("the right pin connects", heartbeat.boot("hello", stage="mount") is not None)
    check("...and the token arrives", got_tokens == ["tok"])
    os.environ["PODH_CONTROL_FINGERPRINT"] = "00" * 32
    check("a wrong pin refuses", heartbeat.boot("hello") is None and
          "pin" in heartbeat.last_error())
    check("...before the token is ever sent", got_tokens == ["tok"])
    os.environ.pop("PODH_CONTROL_FINGERPRINT")
    srv.shutdown()
else:
    print("  (openssl not found: pin test skipped)")

fc.close()
print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
