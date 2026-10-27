"""The control-plane reports: what /done and /boot SEND, and that they degrade safely.

Against a fake pod-control on 127.0.0.1 — nothing leaves the machine. Covers the payloads,
the clipping, the fallback for a control plane that predates reason/stage, the `podh-report`
CLI's exit codes, and podh-init's fatal() carrying its message and failing step.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


from _fake_control import FakeControl  # noqa: E402
from pod_harness import heartbeat, report  # noqa: E402

tmp = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(tmp / "stop")          # never touch the real /tmp flag
for k in ("PODH_CONTROL_FINGERPRINT", "PODH_JOB_ID", "PODH_LOG_DIR", "PODH_LOG_ROOT"):
    os.environ.pop(k, None)

fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="tok", RUNPOD_POD_ID="pod1",
                  PODH_CONTROL_JOB_ID="job_1")

# -- done ---------------------------------------------------------------------------------
heartbeat.done(0)
b = fc.bodies("/v1/done")[-1]
check("done without a reason sends no reason/stage keys",
      b["rc"] == 0 and b["pod_id"] == "pod1" and "reason" not in b and "stage" not in b)
check("the scoped token rides along", fc.calls[-1][2] == "tok")

heartbeat.done(7, reason="nvidia-smi lists 0 GPUs\n  expected 1", stage="gpu_check")
b = fc.bodies("/v1/done")[-1]
check("done carries rc, reason and stage",
      b["rc"] == 7 and b["stage"] == "gpu_check" and b["job_id"] == "job_1")
check("a multi-line reason arrives as one line", b["reason"] == "nvidia-smi lists 0 GPUs expected 1")

heartbeat.done(1, reason="x" * 2000, stage="s" * 200)
b = fc.bodies("/v1/done")[-1]
check("reason is clipped to the server's 500", len(b["reason"]) == heartbeat.MAX_REASON == 500)
check("stage is clipped to the server's 64", len(b["stage"]) == heartbeat.MAX_STAGE == 64)

# -- boot ---------------------------------------------------------------------------------
heartbeat.boot("mounting the data view", stage="mount")
b = fc.bodies("/v1/boot")[-1]
check("boot sends pod_id, line and stage",
      b["pod_id"] == "pod1" and b["line"] == "mounting the data view" and b["stage"] == "mount")
heartbeat.boot("y" * 1000)
b = fc.bodies("/v1/boot")[-1]
check("a boot line is clipped to 300 and stage is optional", len(b["line"]) == 300 and "stage" not in b)
n = len(fc.calls)
check("an empty boot line sends nothing", heartbeat.boot("   ") is None and len(fc.calls) == n)

# -- an older, strict control plane ---------------------------------------------------------
old = FakeControl(strict=True)
os.environ["PODH_CONTROL_URL"] = old.url
r = heartbeat.done(3, reason="spec missing", stage="spec")
sent = old.bodies("/v1/done")
check("a server that refuses the new keys still gets the done, without them",
      r is not None and len(sent) == 2 and "reason" not in sent[1] and sent[1]["rc"] == 3)
check("boot against a server without /v1/boot returns None and does not raise",
      heartbeat.boot("hello", stage="mount") is None)
old.close()

# -- unreachable: never raises, never hangs ----------------------------------------------------
os.environ["PODH_CONTROL_URL"] = "http://127.0.0.1:9"     # discard port; nothing listens
check("boot to an unreachable control plane returns None", heartbeat.boot("x", stage="s") is None)
check("...and keeps the reason for the caller", bool(heartbeat.last_error()))

# -- podh-report CLI ------------------------------------------------------------------------
check("podh-report boot exits 0 even when nothing is reachable",
      report.main(["boot", "mounting", "--stage", "mount"]) == 0)
check("podh-report done exits 1 when the request fails", report.main(["done", "1"]) == 1)
check("...and stops the heartbeat regardless", Path(heartbeat.STOP_FILE).exists())
os.environ["PODH_CONTROL_URL"] = fc.url
check("podh-report done exits 0 when sent",
      report.main(["done", "7", "--reason", "bad gpu", "--stage", "gpu_check"]) == 0)
b = fc.bodies("/v1/done")[-1]
check("...with what it was given", (b["rc"], b["reason"], b["stage"]) == (7, "bad gpu", "gpu_check"))
os.environ.pop("PODH_CONTROL_URL")
check("podh-report done exits 3 when not configured", report.main(["done", "0"]) == 3)
check("podh-report boot is silent and 0 when not configured", report.main(["boot", "x"]) == 0)

# -- podh-init's fatal() ----------------------------------------------------------------------
# fatal() idles forever by design, so the harness under test overrides `sleep` to end the
# shell at the first idle tick — everything before the idle loop runs for real.
src = (ROOT / "docker" / "harness" / "podh-init").read_text()
start = src.index("fatal() {")
end = src.index("\n}\n", start) + 3
script = (src[start:end] + '\nsleep() { exit 0; }\nSTEP=gpu_check\nFATAL_RC=7\n'
          'fatal "GPU check failed: nvidia-smi lists 0 GPU(s), job asked for 1"\n')
env = dict(os.environ, PODH_CONTROL_URL=fc.url, PYTHONPATH=str(ROOT / "src"),
           PODH_HEARTBEAT_STOP_FILE=str(tmp / "stop2"))
p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
b = (fc.bodies("/v1/done") or [{}])[-1]
check("fatal() reports its message as the reason",
      b.get("reason") == "GPU check failed: nvidia-smi lists 0 GPU(s), job asked for 1")
check("fatal() reports the failing step as the stage, with its rc",
      b.get("stage") == "gpu_check" and b.get("rc") == 7)
check("fatal() still prints the message to the console", "✗ GPU check failed" in p.stdout)

fc.close()
print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
