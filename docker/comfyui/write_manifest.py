#!/usr/bin/env python3
"""write_manifest.py — at image build, say what is in the image: /opt/podh/manifest.json.

Run in the final layer of each ComfyUI image, after everything else is installed, with the
image's own python (it reads versions from the environment it describes):

    python write_manifest.py --image podh-comfyui --version <tag> --revision <git sha> \\
        --base ubuntu:24.04@sha256:… --comfyui-tag v0.43.0 --out /opt/podh/manifest.json
    # the video image, on top of the base's:
    python write_manifest.py --image podh-comfyui-video … --extends /opt/podh/manifest.json

Everything except the names passed in is READ from the image, not restated: python, the
torch trio, CUDA and cuDNN (torch's own report, and the pip wheels'), ComfyUI's commit, the
baked node packs ($PODH_BAKED_ROOT/addons.json), the baked binaries
($PODH_BAKED_ROOT/bin/*/.podh-binary.json), and a set of feature flags probed one by one.
`--extends` keeps the parent's identity as `parent` and its `base`.

The image's digest is NOT here: it is a hash of the finished image, made after this runs.
CI adds it to the published copy (see .github/workflows/build-comfyui.yml); a pod learns it
from $PODH_IMAGE_DIGEST when its launcher passes it.

Stdlib only. Never imports anything heavy except torch, once, for its CUDA/cuDNN report
(skipped with --no-torch, and tolerated when torch is absent).
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

FORMAT = 1


def dist(name: str):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def run(cmd, timeout: float = 30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def torch_report(skip: bool) -> dict:
    """torch.version.cuda and cudnn's version, as torch itself reports them (no GPU needed)."""
    if skip or importlib.util.find_spec("torch") is None:
        return {}
    out = run([sys.executable, "-c",
               "import json, torch; c = None\n"
               "try:\n    c = torch.backends.cudnn.version()\nexcept Exception:\n    pass\n"
               "print(json.dumps({'cuda': torch.version.cuda, 'cudnn': c}))"], timeout=300)
    try:
        return json.loads(out.strip().splitlines()[-1]) if out else {}
    except (ValueError, IndexError):
        return {}


def cudnn_str(v) -> str | None:
    """torch reports cuDNN as an int (91002 → 9.10.2)."""
    if isinstance(v, int) and v > 0:
        major, rest = divmod(v, 10000)
        minor, patch = divmod(rest, 100)
        return f"{major}.{minor}.{patch}"
    return v if isinstance(v, str) else None


def node_packs(baked: Path) -> list:
    try:
        items = json.loads((baked / "addons.json").read_text()).get("addons") or {}
    except (OSError, ValueError, AttributeError):
        return []
    return [{"name": n, "repo": v.get("repo"), "commit": v.get("commit")}
            for n, v in sorted(items.items()) if isinstance(v, dict)]


def binaries(baked: Path) -> list:
    out = []
    for marker in sorted((baked / "bin").glob("*/.podh-binary.json")):
        try:
            m = json.loads(marker.read_text())
        except (OSError, ValueError):
            continue
        b = {"name": m.get("name"), "sha256": m.get("sha256"), "bins": m.get("bins"),
             "dir": str(marker.parent)}
        exe = marker.parent / str(m.get("name"))
        first = run([str(exe), "-version"], timeout=10) if os.access(exe, os.X_OK) else None
        if first:
            b["version"] = first.strip().splitlines()[0][:200]
        out.append(b)
    return out


def ffmpeg_nvenc(bins: list) -> bool:
    for b in bins:
        if b.get("name") == "ffmpeg":
            enc = run([str(Path(b["dir"]) / "ffmpeg"), "-hide_banner", "-encoders"], timeout=20)
            if enc is not None:
                return "h264_nvenc" in enc
    exe = shutil.which("ffmpeg")
    enc = run([exe, "-hide_banner", "-encoders"], timeout=20) if exe else None
    return bool(enc and "h264_nvenc" in enc)


def git_head(root: Path):
    out = run(["git", "-C", str(root), "rev-parse", "HEAD"])
    return out.strip() if out else None


def build(a) -> dict:
    baked = Path(a.baked_root or os.environ.get("PODH_BAKED_ROOT") or "/opt/podh/baked")
    comfy = Path(a.comfy_root or os.environ.get("PODH_COMFY_ROOT") or "/opt/ComfyUI")
    parent = {}
    if a.extends:
        try:
            parent = json.loads(Path(a.extends).read_text())
        except (OSError, ValueError) as e:
            raise SystemExit(f"write_manifest: cannot read --extends {a.extends}: {e}")
    t = torch_report(a.no_torch)
    bins = binaries(baked)
    custom = comfy / "custom_nodes"
    man = {
        "format": FORMAT,
        "image": a.image,
        "version": a.version,
        "revision": a.revision,
        "source": a.source,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": a.base or parent.get("base"),
        "python": platform.python_version(),
        "torch": {"torch": dist("torch"), "torchvision": dist("torchvision"),
                  "torchaudio": dist("torchaudio")},
        "cuda": {"cuda": t.get("cuda"), "cudnn": cudnn_str(t.get("cudnn")),
                 "wheels": {"cuda_runtime": dist("nvidia-cuda-runtime-cu12"),
                            "cudnn": dist("nvidia-cudnn-cu12"),
                            "cublas": dist("nvidia-cublas-cu12")}},
        "comfyui": {"tag": a.comfyui_tag or (parent.get("comfyui") or {}).get("tag"),
                    "commit": git_head(comfy)},
        "node_packs": node_packs(baked),
        "binaries": [{k: v for k, v in b.items() if k != "dir"} for b in bins],
        "features": {
            "nvenc_capable_ffmpeg": ffmpeg_nvenc(bins),
            "insightface": importlib.util.find_spec("insightface") is not None,
            "onnxruntime_gpu": dist("onnxruntime-gpu") is not None,
            "latentsync_fixes": (custom / "00_podh_latentsync.py").is_file(),
            "compat_shim": (custom / "00_podh_compat.py").is_file(),
            "sshd": Path("/usr/sbin/sshd").exists() and Path("/opt/podh/start-sshd").exists(),
            "triton_cc": dist("triton") is not None
                         and any(shutil.which(c) for c in ("cc", "gcc")),
        },
        "pod_harness": dist("pod-harness"),
        "catalogue": a.catalogue,
    }
    if parent:
        man["parent"] = {k: parent.get(k) for k in ("image", "version", "revision")}
    if a.refs:
        man["refs"] = a.refs
    return {k: v for k, v in man.items() if v is not None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="write_manifest.py")
    ap.add_argument("--image", required=True, help="the image's name, e.g. podh-comfyui")
    ap.add_argument("--version", required=True, help="its version tag")
    ap.add_argument("--revision", default="unknown", help="the git commit it was built from")
    ap.add_argument("--source", help="the repository URL")
    ap.add_argument("--base", help="the base image reference, with its digest")
    ap.add_argument("--comfyui-tag", help="the ComfyUI release tag")
    ap.add_argument("--extends", help="the parent image's manifest (kept as `parent`)")
    ap.add_argument("--catalogue", help="where the node catalogue is in the image")
    ap.add_argument("--refs", nargs="*", help="registry references it is pushed as")
    ap.add_argument("--baked-root", help="default $PODH_BAKED_ROOT, /opt/podh/baked")
    ap.add_argument("--comfy-root", help="default $PODH_COMFY_ROOT, /opt/ComfyUI")
    ap.add_argument("--no-torch", action="store_true", help="do not ask torch for CUDA/cuDNN")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    man = build(a)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(man, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, out)
    os.chmod(out, 0o644)
    print(json.dumps(man, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
