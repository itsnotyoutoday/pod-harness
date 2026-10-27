"""Build-time check of a ComfyUI image: imports, versions against the lock, cv2's build,
and the baked addons' checkouts. Run with the image's python; exits non-zero on any miss.

    python check_image.py [--import MOD ...] [--dist DIST ...]

No GPU is needed or touched: CUDA is checked on the pod, by podh-boot's gpu_check.
"""
import argparse
import importlib
import importlib.metadata as md
import json
import os
import subprocess
import sys

ALWAYS_IMPORT = ["torch", "torchvision", "torchaudio", "cv2", "numpy", "pod_harness.boot"]
ALWAYS_DIST = ["torch", "torchvision", "torchaudio", "numpy", "opencv-python",
               "opencv-python-headless", "opencv-contrib-python"]


def lock_versions(path):
    out = {}
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            out[name.strip().lower().replace("_", "-")] = ver.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--import", dest="imports", nargs="*", default=[])
    ap.add_argument("--dist", dest="dists", nargs="*", default=[])
    a = ap.parse_args()
    bad = []

    for mod in ALWAYS_IMPORT + a.imports:
        try:
            importlib.import_module(mod)
        except Exception as e:                      # noqa: BLE001 — report every miss
            bad.append(f"import {mod}: {type(e).__name__}: {e}")

    lock = lock_versions(os.environ.get("PODH_LOCK", "/opt/podh/lock.txt"))
    for dist in ALWAYS_DIST + a.dists:
        want = lock.get(dist.lower())
        try:
            have = md.version(dist)
        except md.PackageNotFoundError:
            bad.append(f"{dist}: not installed")
            continue
        if want is None:
            bad.append(f"{dist}: not in the lock")
        elif have != want:
            bad.append(f"{dist}: {have} installed, lock says {want}")

    try:
        import cv2
        if not hasattr(cv2, "ximgproc"):
            bad.append("cv2 is not the contrib build: another opencv package overwrote it")
    except Exception:                               # noqa: BLE001 — already reported above
        pass

    # Every baked addon must be a checkout at its recorded commit, where it says it is —
    # that is exactly what podh-boot's addons stage will test.
    baked = os.path.join(os.environ.get("PODH_BAKED_ROOT", "/opt/podh/baked"), "addons.json")
    try:
        addons = json.load(open(baked)).get("addons", {})
    except (OSError, ValueError) as e:
        addons = {}
        bad.append(f"{baked}: {e}")
    for name, rec in sorted(addons.items()):
        try:
            head = subprocess.run(["git", "-C", rec["dir"], "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, KeyError, subprocess.SubprocessError) as e:
            head = f"? ({e})"
        if head != rec.get("commit"):
            bad.append(f"baked addon {name}: HEAD {head[:12]} != {str(rec.get('commit'))[:12]}")

    import numpy
    import torch
    print(f"python {sys.version.split()[0]} | torch {torch.__version__} (CUDA {torch.version.cuda})"
          f" | numpy {numpy.__version__} | baked addons: {', '.join(sorted(addons)) or 'none'}")
    for b in bad:
        print(f"  FAIL {b}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
