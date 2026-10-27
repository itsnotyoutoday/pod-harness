"""The heartbeat: beats while the job produces output, goes quiet when it hangs or ends.

The bug this pins: heartbeat.start() was called by nothing, so harness-image pods never
sent /alive. The property it must keep: a hung workload LOOKS stuck — a heartbeat that
beats regardless of progress switches off the control plane's "went quiet" reaper.

Against a fake pod-control on 127.0.0.1; nothing leaves the machine.
"""
import os
import subprocess
import sys
import tempfile
import time
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
from pod_harness import heartbeat  # noqa: E402

tmp = Path(tempfile.mkdtemp())
stop = tmp / "stop"
heartbeat.STOP_FILE = str(stop)
console = tmp / "console.log"
console.write_text("booting\n")

# -- the predicate ------------------------------------------------------------------------
live = heartbeat.activity_live([console, tmp / "absent.jsonl"], stale_after_sec=60)
check("fresh output is live", live() is True)
old = time.time() - 600
os.utime(console, (old, old))
check("output older than the window is NOT live — a hung job looks stuck", live() is False)
check("no activity file at all is not live",
      heartbeat.activity_live([tmp / "nope"], 60)() is False)
check("a window of 0 disables the gate", heartbeat.activity_live([tmp / "nope"], 0)() is True)
console.write_text("progress\n")
check("new output makes it live again", live() is True)

# -- the loop -------------------------------------------------------------------------------
fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="t", RUNPOD_POD_ID="pod1")
os.environ.pop("PODH_CONTROL_FINGERPRINT", None)
gate = {"live": True}
t = heartbeat.start(0.05, live=lambda: gate["live"])
check("start() beats while live", wait_for(lambda: len(fc.bodies("/v1/alive")) >= 2))
gate["live"] = False
time.sleep(0.2)
n = len(fc.bodies("/v1/alive"))
time.sleep(0.4)
check("...and sends nothing while not live", len(fc.bodies("/v1/alive")) == n)
t_raise = {"n": 0}


def bad():
    t_raise["n"] += 1
    raise RuntimeError("predicate exploded")


t2 = heartbeat.start(0.05, live=bad)
check("a raising predicate does not kill the loop", wait_for(lambda: t_raise["n"] >= 3) and t2.is_alive())
stop.touch()
check("the stop flag ends every loop", wait_for(lambda: not t.is_alive() and not t2.is_alive()))

# -- the CLI podh-init runs -------------------------------------------------------------------
stop.unlink()
before = len(fc.bodies("/v1/alive"))
console.write_text("job running\n")
env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PODH_HEARTBEAT_STOP_FILE=str(stop))
p = subprocess.Popen([sys.executable, "-m", "pod_harness.heartbeat", "--activity", str(console),
                      "--interval", "0.1", "--stale-after", "60"],
                     env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
check("python -m pod_harness.heartbeat beats while output is fresh",
      wait_for(lambda: len(fc.bodies("/v1/alive")) > before))
os.utime(console, (old, old))
time.sleep(0.3)
n = len(fc.bodies("/v1/alive"))
time.sleep(0.5)
check("...goes quiet when the output stalls", len(fc.bodies("/v1/alive")) == n)
stop.touch()
try:
    p.wait(timeout=5)
    exited = p.returncode == 0
except subprocess.TimeoutExpired:
    p.kill()
    exited = False
check("...and exits once the work is done", exited)

# -- podh-init actually starts it ---------------------------------------------------------------
init = (ROOT / "docker" / "harness" / "podh-init").read_text()
check("podh-init launches the heartbeat (it was never started before)",
      "-m pod_harness.heartbeat" in init)
check("podh-init gates it on the console, not on the process existing",
      '--activity "$CONSOLE"' in init)
check("podh-init clears a stale stop flag before starting it", 'rm -f "$HB_STOP"' in init)

fc.close()
print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
