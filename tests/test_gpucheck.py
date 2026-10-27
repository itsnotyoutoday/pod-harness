"""podh-gpucheck: when it runs, what it catches, and what it reports.

No GPU needed: `nvidia-smi` and the probe interpreter are fake executables on PATH, driven
by FAKE_* variables, so every failure a real pod has shown — no driver, fewer GPUs than
billed, CUDA init raising, CUDA init hanging, no torch at all — is reproduced on a laptop.
The control plane is a fake on 127.0.0.1.
"""
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


from _fake_control import FakeControl  # noqa: E402
from pod_harness import gpucheck, heartbeat  # noqa: E402

tmp = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(tmp / "stop")
gpu_bin, bare_bin = tmp / "gpu", tmp / "bare"
gpu_bin.mkdir(); bare_bin.mkdir()


def exe(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


exe(gpu_bin, "nvidia-smi", r'''
[ -n "$FAKE_SMI_FAIL" ] && { echo "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."; exit 9; }
case "$1" in
  -L) i=0; while [ $i -lt "${FAKE_GPUS:-0}" ]; do echo "GPU $i: NVIDIA A40 (UUID: GPU-0000$i)"; i=$((i+1)); done ;;
  --query-gpu=driver_version) echo "550.54.15" ;;
esac
''')
probe = r'''
case "${FAKE_TORCH:-ok}" in
  none) echo "PODH_NO_TORCH ModuleNotFoundError"; exit 3 ;;
  fail) echo "Traceback (most recent call last):" >&2
        echo "RuntimeError: CUDA unknown error - this may be due to an incorrectly set up environment" >&2; exit 1 ;;
  hang) exec sleep 30 ;;
  ok)   echo "PODH_PROBE {\"torch\": \"2.3.0\", \"cuda\": \"12.1\", \"count\": ${FAKE_TORCH_COUNT:-1}, \"name\": \"NVIDIA A40\"}" ;;
esac
'''
for d in (gpu_bin, bare_bin):
    exe(d, "fakepy", probe)
    exe(d, "notorch", 'echo "PODH_NO_TORCH ModuleNotFoundError"; exit 3\n')

for k in list(os.environ):
    if k.startswith(("FAKE_", "PODH_GPU", "PODH_CONTROL")):
        os.environ.pop(k)


def run(args, path=gpu_bin, **env):
    """main() in-process with PATH and FAKE_* set; returns (rc, stdout)."""
    saved = dict(os.environ)
    os.environ["PATH"] = f"{path}:/usr/bin:/bin"
    os.environ.update({k: str(v) for k, v in env.items()})
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = gpucheck.main(args)
    finally:
        os.environ.clear(); os.environ.update(saved)
    return rc, buf.getvalue().strip()


def js(args, **kw):
    rc, out = run(args + ["--json", "--no-report"], **kw)
    return rc, json.loads(out.splitlines()[-1])


# -- when it runs --------------------------------------------------------------------------
rc, out = run(["--no-report"], PODH_GPU_COUNT=0, FAKE_GPUS=1)
check("PODH_GPU_COUNT=0 skips, exit 0, says cpu pod", rc == 0 and out == "cpu pod: skipped")
rc, r = js([], path=bare_bin)
check("unset and no nvidia-smi: skipped (a CPU pod on an old launcher)", rc == 0 and r["ran"] is False)
rc, r = js([], FAKE_GPUS=0)
check("unset and nvidia-smi lists nothing: skipped", rc == 0 and r["ran"] is False)
rc, r = js(["--python", "fakepy"], FAKE_GPUS=1)
check("unset and nvidia-smi lists a GPU: it runs", rc == 0 and r["ran"] and r["ok"])
rc, r = js(["--python", "fakepy"], PODH_GPU_COUNT=2, FAKE_GPUS=2, FAKE_TORCH_COUNT=2)
check("PODH_GPU_COUNT=2 with 2 GPUs passes, names and driver reported",
      rc == 0 and r["gpus"] == ["NVIDIA A40"] * 2 and r["driver_version"] == "550.54.15"
      and r["cuda_probe"]["count"] == 2)
rc, out = run(["--skip"], PODH_GPU_COUNT=2, FAKE_GPUS=0)
check("--skip skips even a GPU job", rc == 0 and "skipped" in out)
rc, r = js(["--force", "--python", "fakepy"], PODH_GPU_COUNT=0, FAKE_GPUS=0)
check("--force checks even a CPU pod (and fails it here)", rc == 7 and r["ran"])
check("an unreadable PODH_GPU_COUNT is unknown, not zero", gpucheck.requested({"PODH_GPU_COUNT": "two"}) is None)

# -- what it catches -----------------------------------------------------------------------
rc, r = js(["--python", "fakepy"], path=bare_bin, PODH_GPU_COUNT=1)
check("a GPU job with no nvidia-smi fails 7", rc == 7 and "nvidia-smi not found" in r["error"])
rc, r = js(["--python", "fakepy"], PODH_GPU_COUNT=1, FAKE_GPUS=1, FAKE_SMI_FAIL=1)
check("nvidia-smi that cannot reach the driver fails 7, with its message",
      rc == 7 and "couldn't communicate with the NVIDIA driver" in r["error"])
rc, r = js(["--python", "fakepy"], PODH_GPU_COUNT=2, FAKE_GPUS=1)
check("fewer GPUs than asked for fails 7",
      rc == 7 and r["error"] == "nvidia-smi lists 1 GPU(s), job asked for 2")
rc, r = js(["--python", "fakepy"], PODH_GPU_COUNT=2, FAKE_GPUS=2, FAKE_TORCH_COUNT=1)
check("torch seeing fewer devices than nvidia-smi fails 7", rc == 7 and "torch sees 1" in r["error"])
rc, r = js(["--python", "fakepy"], PODH_GPU_COUNT=1, FAKE_GPUS=1, FAKE_TORCH="fail")
check("CUDA init raising fails 7 with the error line, not the traceback header",
      rc == 7 and r["error"].startswith("CUDA init failed: RuntimeError: CUDA unknown error"))
t0 = time.time()
rc, r = js(["--python", "fakepy", "--timeout", "1"], PODH_GPU_COUNT=1, FAKE_GPUS=1, FAKE_TORCH="hang")
check("CUDA init hanging fails 7 at the timeout, not after it",
      rc == 7 and "timed out after 1s" in r["error"] and time.time() - t0 < 8)
rc, r = js(["--python", "notorch"], PODH_GPU_COUNT=1, FAKE_GPUS=1)
check("no torch anywhere: probe skipped, nvidia-smi alone passes",
      rc == 0 and r["cuda_probe"]["skipped"] == "no torch: CUDA probe skipped")
rc, r = js(["--python", "notorch", "--python", "missing-python", "--python", "fakepy"],
           PODH_GPU_COUNT=1, FAKE_GPUS=1)
check("the first python that HAS torch is the one probed", rc == 0 and r["cuda_probe"]["python"] == "fakepy")
check("PODH_GPUCHECK_PYTHON lists candidates; default is python3",
      gpucheck.candidates(env={"PODH_GPUCHECK_PYTHON": "/opt/a/python, py2"}) == ["/opt/a/python", "py2"]
      and gpucheck.candidates(env={}) == ["python3"])
rc, out = run(["--python", "fakepy", "--no-report"], PODH_GPU_COUNT=1, FAKE_GPUS=1)
check("the human line names the GPU, driver and torch",
      rc == 0 and out.startswith("gpu check: ok — 1 GPU(s): NVIDIA A40; driver 550.54.15; torch 2.3.0"))

# -- what it reports -----------------------------------------------------------------------
fc = FakeControl()
ctl = dict(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="t", RUNPOD_POD_ID="pod1")
rc, out = run(["--python", "fakepy"], PODH_GPU_COUNT=2, FAKE_GPUS=1, **ctl)
b = (fc.bodies("/v1/done") or [{}])[-1]
check("a failure reports done(7, reason, stage=gpu_check)",
      rc == 7 and b.get("rc") == 7 and b.get("stage") == "gpu_check"
      and b.get("reason") == "nvidia-smi lists 1 GPU(s), job asked for 2")
check("...and stops the heartbeat", Path(heartbeat.STOP_FILE).exists())
n = len(fc.calls)
rc, _ = run(["--python", "fakepy", "--no-report"], PODH_GPU_COUNT=2, FAKE_GPUS=1, **ctl)
check("--no-report reports nothing", rc == 7 and len(fc.calls) == n)
rc, _ = run(["--python", "fakepy"], PODH_GPU_COUNT=1, FAKE_GPUS=1, **ctl)
check("a pass reports nothing", rc == 0 and len(fc.calls) == n)
fc.close()

# -- log scan ------------------------------------------------------------------------------
good, bad = tmp / "good.log", tmp / "bad.log"
good.write_text("loading model\nstep 1/10\n")
bad.write_text("loading model\nRuntimeError: CUDA error: no kernel image is available for execution\n"
               "torch: no cuda gpus are available\n")
rc, out = run(["--scan-log", str(good)])
check("a clean log scans 0", rc == 0 and "no fatal CUDA errors" in out)
rc, out = run(["--scan-log", str(bad)])
check("a fatal CUDA string scans 7 and names the line", rc == 7 and f"{bad}:2:" in out and "(+1 more)" in out)
rc, out = run(["--scan-log", str(bad), "--json"])
check("--json lists every hit, case-insensitively",
      [h["pattern"] for h in json.loads(out)["hits"]] == ["CUDA error: no kernel image", "No CUDA GPUs are available"])
rc, _ = run(["--scan-log", str(tmp / "absent.log")])
check("an unreadable log is 2, not a GPU verdict", rc == 2)

# -- podh-init runs it before the mount, and fails with its stage --------------------------------
init = (ROOT / "docker" / "harness" / "podh-init").read_text()
check("podh-init runs the GPU check, without double-reporting",
      "-m pod_harness.gpucheck --no-report" in init)
check("...before the data view is mounted",
      init.index("-m pod_harness.gpucheck") < init.index("/usr/local/bin/podh-mount; then"))
check("...and a failure is fatal at stage gpu_check with rc 7",
      "STEP=gpu_check" in init and "FATAL_RC=7" in init)

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
