"""An image started without a control plane boots its own default spec.

    PYTHONPATH=src python3 tests/test_standalone.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

for k in ("PODH_CONTROL_URL", "PODH_CONTROL_TOKEN", "PODH_STANDALONE_SPEC", "PODH_BOOT_SPEC"):
    os.environ.pop(k, None)

from pod_harness.boot import cli  # noqa: E402
from pod_harness.boot.core import BootFailure  # noqa: E402

ok = []


def check(label, cond):
    ok.append(bool(cond))
    print(f"  {'✅' if cond else '❌'} {label}")


print("podh-boot standalone")
try:
    cli.load_spec(None, None)
    check("no control plane and no fallback: fails at stage spec", False)
except BootFailure as e:
    check("no control plane and no fallback: fails at stage spec", getattr(e, "stage", "") == "spec")

tmp = Path(tempfile.mkdtemp())
spec = {"boot": {"gpu_check": True, "workload": {"comfyui": {"port": 8188}}}, "policy": {}}
(tmp / "standalone.json").write_text(json.dumps(spec))
os.environ["PODH_STANDALONE_SPEC"] = str(tmp / "standalone.json")
check("with PODH_STANDALONE_SPEC: that spec is used", cli.load_spec(None, None) == spec)

explicit = tmp / "job.json"
explicit.write_text(json.dumps({"boot": {"gpu_check": False}}))
check("an explicit --spec still wins over the standalone default",
      cli.load_spec(str(explicit), None) == {"boot": {"gpu_check": False}})

(tmp / "standalone.json").write_text("{not json")
try:
    cli.load_spec(None, None)
    check("an unreadable standalone spec fails clearly", False)
except BootFailure as e:
    check("an unreadable standalone spec fails clearly", "standalone" in str(e))

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
