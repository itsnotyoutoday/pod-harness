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

srv.close(); bare.close(); fc.close()
print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
