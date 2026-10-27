"""Two callers fetching the same model path at once share one download (no .part race)."""
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


from pod_harness import fetch  # noqa: E402
from pod_harness.boot import models  # noqa: E402
from pod_harness.boot.core import Context  # noqa: E402
from pod_harness import local  # noqa: E402

tmp = Path(tempfile.mkdtemp())
import os  # noqa: E402
os.environ["PODH_MODELS_ROOT"] = str(tmp / "models")
ctx = Context(boot_dir=tmp / "bd", state=local.LocalState())
calls = []


def fake_download(url, dest, size=None, sha256=None, deadline=None):
    calls.append(url)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"x" * 10)
    time.sleep(0.3)                       # long enough for the second caller to arrive
    os.replace(part, dest)                # a second writer would now miss the .part
    return dest


fetch.download = fake_download
models.report = lambda *a, **k: None


class Man:
    rev = 1
    items = {"models/loras/a.safetensors": {"path": "models/loras/a.safetensors",
                                            "url": "https://x/a", "size": 10}}


res = []
ts = [threading.Thread(target=lambda: res.append(
    models.fetch_one(ctx, Man, "models/loras/a.safetensors", None, lambda t: None)))
    for _ in range(3)]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("three concurrent callers: one download", len(calls) == 1)
check("all three see success", res == [None, None, None])
check("the file is there", (ctx.models_root / "models/loras/a.safetensors").is_file())
check("a later call after it lands downloads nothing", models.fetch_one(
    ctx, Man, "models/loras/a.safetensors", None, lambda t: None) is None and len(calls) == 1)
print(f"{sum(ok)}/{len(ok)}")
sys.exit(0 if all(ok) else 1)
