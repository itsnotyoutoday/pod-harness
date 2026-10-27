"""The local API's additions for podh-worker: POST /models/fetch (models.ensure, the mid-life
machinery asked directly), local.Client, window_until on /state, and app.serve exiting 0 on a
drain without ending the pod.

Against a fake pod-control on 127.0.0.1; nothing leaves the machine.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


for k in list(os.environ):
    if k.startswith("PODH_") and k != "PODH_HEARTBEAT_STOP_FILE":
        os.environ.pop(k)
from _fake_control import FakeControl  # noqa: E402
from pod_harness import heartbeat, local  # noqa: E402
from pod_harness.boot import models, serve  # noqa: E402
from pod_harness.boot.core import Context  # noqa: E402

tmp = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(tmp / "stop")
fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="tok", RUNPOD_POD_ID="pod1",
                  PODH_MODELS_ROOT=str(tmp / "models"), PODH_NO_ARIA2="1")

state = local.LocalState()
ctx = Context(boot_dir=tmp / "bd", state=state)
srv = local.LocalServer(state, token="lt",
                        on_models=lambda files, paths: models.ensure(ctx, files, paths)).start()
c = local.Client(srv.url, "lt")

print("— local.Client")
check("health answers", c.health())
check("busy flips the shared state", c.busy(True, "task-1")[0] == 200 and state.busy
      and state.detail == "task-1")
c.busy(False)
check("...and back", not state.busy)
state.take_reply({"drain": False, "window_until": 1234.5})
check("/state carries window_until from the beat reply", c.state().get("window_until") == 1234.5)
check("a client with no URL answers (0, None) and never raises",
      local.Client("", "").call("GET", "/state") == (0, None) and local.Client("", "").state() == {})
check("a wrong token is refused", local.Client(srv.url, "nope").state() == {})

print("— POST /models/fetch")
fc.blobs["a"] = b"A" * 3000
st, body = c.models_fetch(files=[{"path": "loras/a.safetensors", "url": fc.blob_url("a"),
                                  "size": 3000}])
check("202 with the paths it fetches", st == 202 and body == {"fetching": ["loras/a.safetensors"]})
check("the file lands under the models root and shows present on /state",
      wait_for(lambda: c.state().get("models", {}).get("loras/a.safetensors") == "present")
      and (tmp / "models" / "loras" / "a.safetensors").read_bytes() == b"A" * 3000)
check("states are reported to pod-control as for any mid-life model",
      any(b["models"].get("loras/a.safetensors") == "present"
          for b in fc.bodies("/v1/pod/models/state")))

# by path: the manifest is asked for the link
fc.blobs["b"] = b"B" * 100
fc.routes[("GET", "/v1/pod/models")] = (200, {"models_rev": 3, "files": [
    {"path": "vae/b.safetensors", "url": fc.blob_url("b"), "size": 100}]})
st, _ = c.models_fetch(paths=["vae/b.safetensors"])
check("a bare path is looked up in a fresh manifest and fetched",
      st == 202 and wait_for(lambda: c.state()["models"].get("vae/b.safetensors") == "present"))
st, _ = c.models_fetch(paths=["vae/missing.safetensors"])
check("a path nobody has a link for ends failed, not fetching forever",
      wait_for(lambda: c.state()["models"].get("vae/missing.safetensors") == "failed"))
st, body = c.models_fetch(paths=["../../etc/passwd"])
check("a path outside the models root is refused (422)", st == 422)
st, body = c.models_fetch(files=[{"path": "x"}])
check("a file without a url is refused (422)", st == 422)
bare = local.LocalServer(local.LocalState(), token="t").start()
check("no models machinery: 503", local.Client(bare.url, "t").models_fetch(paths=["a"])[0] == 503)

print("— app.serve exiting 0 on a drain")
ends = []
ctx.finish = lambda rc, reason=None, stage=None, **k: ends.append((rc, reason))
s = serve.Serve(ctx, [sys.executable, "-c", "pass"], dict(os.environ), str(tmp), 5,
                tmp / "serve.log")
state.take_reply({"drain": True})
s.start()
time.sleep(1.0)
check("a worker exiting 0 under drain does not finish the pod", ends == [] and s.drained)
state.take_reply({"drain": False})
s2 = serve.Serve(ctx, [sys.executable, "-c", "pass"], dict(os.environ), str(tmp), 5,
                 tmp / "serve2.log")
s2.start()
check("without a drain, exit 0 still finishes it", wait_for(lambda: ends == [(0, "app serve finished")]))

print("— models the store lacks, through /models/fetch (TASK-API §10)")
import hashlib  # noqa: E402
from pod_harness import fetch as _fetch  # noqa: E402
sdata = os.urandom(20_000)
fc.blobs["srcfile"] = sdata
state.ready = True
st, obj = c.models_fetch([{"path": "loras/s.safetensors", "source": fc.blob_url("srcfile"),
                           "put_url": f"{fc.url}/put/stored_s",
                           "sha256": hashlib.sha256(sdata).hexdigest(), "size": len(sdata)}], [])
check("a {path, source, put_url} file is accepted", st == 202)
check("...fetched from the source and present",
      wait_for(lambda: state.models.get("loras/s.safetensors") == "present")
      and (tmp / "models" / "loras/s.safetensors").read_bytes() == sdata)
check("...and put back into the store in the background",
      wait_for(lambda: fc.blobs.get("stored_s") == sdata))
st, obj = c.models_fetch([{"path": "x.bin", "source": "ftp://nope/x"}], [])
check("a source that is not https/hf/civitai is refused (422)", st == 422 and "ftp" in obj["error"])
fc.blobs["src2"] = b"two"
state.take_reply({"drain": True})
before = set(fc.blobs)
c.models_fetch([{"path": "loras/d.bin", "source": fc.blob_url("src2"),
                 "put_url": f"{fc.url}/put/stored_d"}], [])
check("while the pod drains, the file is still fetched and used",
      wait_for(lambda: state.models.get("loras/d.bin") == "present"))
check("...but its upload is skipped", wait_for(
    lambda: models.uploader(ctx).done.get("loras/d.bin", "").startswith("skipped"))
      and "stored_d" not in fc.blobs)
state.take_reply({"drain": False})
fc.blobs["src3"] = b"three"
fc.put_refuse["stored_e"] = 5
c.models_fetch([{"path": "loras/e.bin", "source": fc.blob_url("src3"),
                 "put_url": f"{fc.url}/put/stored_e"}], [])
check("a refused upload (expired put_url) is reported failed and the model stays present",
      wait_for(lambda: models.uploader(ctx).done.get("loras/e.bin", "").startswith("failed"))
      and state.models.get("loras/e.bin") == "present")

print("— source references")
os.environ.pop("HF_TOKEN", None)
os.environ["CIVITAI_TOKEN"] = "civ"
check("hf://owner/repo/dir/file@rev → resolve URL",
      _fetch.resolve_source("hf://o/r/dir/f.safetensors@abc123") ==
      ("https://huggingface.co/o/r/resolve/abc123/dir/f.safetensors", {}))
os.environ["HF_TOKEN"] = "hft"
check("...rev defaults to main, HF_TOKEN as a Bearer header",
      _fetch.resolve_source("hf://o/r/f.bin") ==
      ("https://huggingface.co/o/r/resolve/main/f.bin", {"Authorization": "Bearer hft"}))
check("civitai://<id> → the download API, CIVITAI_TOKEN as a Bearer header",
      _fetch.resolve_source("civitai://4242") ==
      ("https://civitai.com/api/download/models/4242", {"Authorization": "Bearer civ"}))
bad = 0
for s_ in ("hf://o/r", "hf://o/../f@x", "civitai://abc", "", "s3://b/k"):
    try:
        _fetch.resolve_source(s_)
    except ValueError:
        bad += 1
check("...malformed ones are refused", bad == 5)
os.environ.pop("HF_TOKEN"); os.environ.pop("CIVITAI_TOKEN")

# A source's token must not follow a redirect to another host (the CDN, a bucket).
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
import threading  # noqa: E402
seen = []


class Redir(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        seen.append((self.headers.get("Host"), self.headers.get("Authorization")))
        if self.path.startswith("/first"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/cdn")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "3")
        self.end_headers()
        self.wfile.write(b"abc")

    def log_message(self, *a):
        pass


rs = ThreadingHTTPServer(("127.0.0.1", 0), Redir)
threading.Thread(target=rs.serve_forever, daemon=True).start()
_fetch.http_download(f"http://127.0.0.1:{rs.server_port}/first", tmp / "redir.bin",
                     headers={"Authorization": "Bearer secret"})
rs.shutdown()
check("the token goes to the source, not to the host it redirects to",
      seen[0][1] == "Bearer secret" and seen[1][1] is None
      and (tmp / "redir.bin").read_bytes() == b"abc")

srv.close(); bare.close(); fc.close()
print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
