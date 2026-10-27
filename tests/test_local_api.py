"""The pod-local API (busy, state, done), the podh-report wrappers over it, and podh-boot's
heartbeat that carries the busy flag and keeps the reply for /state.

Against a fake pod-control on 127.0.0.1; nothing leaves the machine.
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


from _fake_control import FakeControl  # noqa: E402
from pod_harness import heartbeat, local, report  # noqa: E402

tmp = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(tmp / "stop")
os.environ.pop("PODH_CONTROL_FINGERPRINT", None)
fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="tok", RUNPOD_POD_ID="pod1",
                  PODH_CONTROL_JOB_ID="job_1")
for k in ("PODH_LOCAL_URL", "PODH_LOCAL_TOKEN", "PODH_LOCAL_PORT"):
    os.environ.pop(k, None)

state = local.LocalState()
dones = []


def on_done(rc, reason, stage):
    dones.append((rc, reason, stage))
    return {"requested": True, "configured": True}


srv = local.LocalServer(state, token="lt", on_done=on_done).start()


def raw(method, path, body=None, token="lt"):
    req = urllib.request.Request(srv.url + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Podh-Token": token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


check("served on 127.0.0.1 only", srv.url.startswith("http://127.0.0.1:"))
check("/health needs no token", raw("GET", "/health", token=None)[0] == 200)
check("/state refuses without the token", raw("GET", "/state", token=None)[0] == 401)
check("/state refuses a wrong token", raw("GET", "/state", token="nope")[0] == 401)
req = urllib.request.Request(srv.url + "/state", headers={"Authorization": "Bearer lt"})
check("Authorization: Bearer works too", urllib.request.urlopen(req).status == 200)
check("/busy wants a boolean", raw("POST", "/busy", {"busy": "yes"})[0] == 422)
check("/busy sets the flag and detail", raw("POST", "/busy", {"busy": True, "detail": "step 1"})[0] == 200
      and state.busy and state.detail == "step 1")
st, snap = raw("GET", "/state")
check("/state has the contract's keys",
      set(snap) >= {"drain", "park", "extend_until", "models_rev", "models"} and snap["busy"] is True)
state.take_reply({"drain": True, "park": False, "extend_until": "2026-10-27T20:00:00Z",
                  "models_rev": 4})
state.set_model("vae/b.safetensors", "fetching")
st, snap = raw("GET", "/state")
check("/state reports the last heartbeat reply and model states",
      snap["drain"] is True and snap["models_rev"] == 4
      and snap["models"] == {"vae/b.safetensors": "fetching"})
check("/done reaches the supervisor", raw("POST", "/done", {"rc": 3, "reason": "r", "stage": "s"})[0] == 200
      and dones[-1] == (3, "r", "s"))
check("/done wants an integer rc", raw("POST", "/done", {"rc": "x"})[0] == 422)

# -- podh-report over it ------------------------------------------------------------------
check("podh-report busy exits 3 outside podh-boot", report.main(["busy"]) == 3)
os.environ.update(PODH_LOCAL_URL=srv.url, PODH_LOCAL_TOKEN="lt")
check("podh-report idle exits 0 and clears the flag", report.main(["idle"]) == 0 and not state.busy)
check("podh-report busy --detail sets both",
      report.main(["busy", "--detail", "render 3/8"]) == 0 and state.busy and state.detail == "render 3/8")
n = len(fc.bodies("/v1/done"))
check("podh-report done goes through the local API under podh-boot",
      report.main(["done", "0", "--reason", "finished"]) == 0 and dones[-1] == (0, "finished", None)
      and len(fc.bodies("/v1/done")) == n)
os.environ["PODH_LOCAL_TOKEN"] = "wrong"
check("podh-report busy exits 1 when the local API refuses", report.main(["busy"]) == 1)
os.environ["PODH_LOCAL_URL"] = "http://127.0.0.1:9"
check("podh-report done falls back to the control plane when the local API is gone",
      report.main(["done", "1", "--stage", "x"]) == 0 and fc.bodies("/v1/done")[-1]["rc"] == 1)
for k in ("PODH_LOCAL_URL", "PODH_LOCAL_TOKEN"):
    os.environ.pop(k)

# -- the boot heartbeat carries busy, and its reply lands in /state -------------------------
try:
    os.unlink(heartbeat.STOP_FILE)
except FileNotFoundError:
    pass
st2 = local.LocalState()
fc.alive_reply = {"drain": False, "park": True, "models_rev": 9, "extend_until": None}
n = len(fc.bodies("/v1/alive"))
t, wake = local.start_beat(st2, interval=30)
check("the boot heartbeat beats at once", wait_for(lambda: len(fc.bodies("/v1/alive")) > n))
check("...saying not busy", fc.bodies("/v1/alive")[-1]["busy"] is False)
check("...and its reply lands in /state", wait_for(lambda: st2.snapshot()["park"] is True)
      and st2.snapshot()["models_rev"] == 9)
st2.set_busy(True, "working")
n = len(fc.bodies("/v1/alive"))
wake()
check("a busy flip is sent promptly, not at the next interval",
      wait_for(lambda: len(fc.bodies("/v1/alive")) > n)
      and fc.bodies("/v1/alive")[-1]["busy"] is True and fc.bodies("/v1/alive")[-1]["detail"] == "working")
Path(heartbeat.STOP_FILE).touch()
wake()
check("the stop flag silences it", wait_for(lambda: not t.is_alive()))
heartbeat._REPLY_HOOKS.clear()

os.unlink(heartbeat.STOP_FILE)
st3 = local.LocalState()
n = len(fc.bodies("/v1/alive"))
t, wake = local.start_beat(st3, interval=0.2, live=lambda: False)
time.sleep(1.0)
check("a workload that does not answer means no beats (it must look stuck)",
      len(fc.bodies("/v1/alive")) == n)
Path(heartbeat.STOP_FILE).touch()
heartbeat._REPLY_HOOKS.clear()

srv.close()
fc.close()
print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
