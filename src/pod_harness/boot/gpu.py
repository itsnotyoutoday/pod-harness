"""gpu_check: is the GPU usable, how big is it, and can it encode video.

Always runs, even on a resumed pod: the disk survived the park, the GPU it is now attached
to is a different card. Reuses podh-gpucheck's decision and checks, so a boot and a
hand-run `podh-gpucheck` agree on what "usable" means, and a failure ends the pod with rc 7
at stage gpu_check — the stage pod-control blocks the host for.

It also records what the app needs to know about the card — vram_gb, cuda, driver — and,
once binaries are installed, whether NVENC works (a one-frame h264_nvenc null encode). Those
(and `gpu`, the first card's name) go to POST /v1/pod/facts, best effort: a server without the route answers 404 and the boot
carries on. A missing ffmpeg or a failed encode is a fact (nvenc false/unknown), not a
failed boot — the app decides whether it needs NVENC.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Optional

from .. import gpucheck, heartbeat
from .core import BootFailure, Context

NVENC_PROBE = ["-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "color=c=black:s=64x64:d=0.04", "-frames:v", "1",
               "-c:v", "h264_nvenc", "-f", "null", "-"]


def _smi(args: list, timeout: float = 30) -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        p = gpucheck._run([exe] + args, timeout)
    except Exception:
        return None
    return p.stdout if p.returncode == 0 else None


def vram_gb() -> Optional[int]:
    """Memory of the first card in GB (nvidia-smi reports MiB)."""
    out = _smi(["--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    m = re.search(r"(\d+(?:\.\d+)?)", out or "")
    return int(round(float(m.group(1)) / 1024)) if m else None


def smi_cuda() -> Optional[str]:
    m = re.search(r"CUDA Version:\s*([\d.]+)", _smi([]) or "")
    return m.group(1) if m else None


def post_facts(ctx: Context) -> None:
    keys = ("vram_gb", "nvenc", "cuda", "driver", "gpu")
    body = {k: ctx.facts[k] for k in keys if ctx.facts.get(k) is not None}
    if body and heartbeat._cfg()[0]:
        heartbeat.request("POST", "/v1/pod/facts", body, timeout=5)


def run(ctx: Context) -> None:
    want = ctx.boot.get("gpu_check")
    count = gpucheck.requested()
    if want is False:
        ctx.line("gpu check: off in the boot spec")
        return
    d = gpucheck.decide(count, force=bool(want is True and count is None and
                                          shutil.which("nvidia-smi")))
    if not d["run"]:
        ctx.line(d["why"])
        return
    ctx.line(f"checking the GPU ({d['why']})")
    pythons = gpucheck.candidates()
    if not os.environ.get("PODH_GPUCHECK_PYTHON") and ctx.comfy_py and ctx.comfy_py not in pythons:
        pythons = [ctx.comfy_py] + pythons      # catalog images keep torch in the app venv
    timeout = float(os.environ.get("PODH_GPUCHECK_TIMEOUT") or 120)
    r = gpucheck.check(d["expected"], pythons=pythons, probe_timeout=ctx.bounded(timeout),
                       listing=d["listing"])
    if not r["ok"]:
        raise BootFailure(r["error"], rc=gpucheck.EXIT_GPU, stage="gpu_check")
    probe = r.get("cuda_probe") or {}
    ctx.facts.update({"has_gpu": True, "gpu": (r["gpus"] or [None])[0],
                      "gpus": r["gpus"], "driver": r.get("driver_version"),
                      "cuda": probe.get("cuda") or smi_cuda(), "vram_gb": vram_gb()})
    if probe.get("torch"):
        ctx.facts["torch"] = probe["torch"]
    ctx.line(gpucheck.human(r) + (f"; {ctx.facts['vram_gb']} GB" if ctx.facts.get("vram_gb") else ""))
    post_facts(ctx)


def find_ffmpeg(ctx: Context) -> Optional[str]:
    for d in ctx.bin_dirs:
        p = os.path.join(str(d), "ffmpeg")
        if os.access(p, os.X_OK):
            return p
    return shutil.which("ffmpeg", path=ctx.child_env().get("PATH"))


def probe_nvenc(ctx: Context) -> None:
    """After binaries: one-frame h264_nvenc encode. Reported, never fatal."""
    if not ctx.facts.get("has_gpu"):
        return
    ff = find_ffmpeg(ctx)
    if not ff:
        ctx.line("nvenc: unknown (no ffmpeg to probe with)")
        return
    try:
        p = subprocess.run([ff] + NVENC_PROBE, capture_output=True, text=True,
                           timeout=ctx.bounded(60))
        ok = p.returncode == 0
        why = "" if ok else " — " + (p.stderr.strip().splitlines() or [f"exit {p.returncode}"])[-1][:120]
    except subprocess.TimeoutExpired:
        ok, why = False, " — encode timed out"
    except OSError as e:
        ok, why = False, f" — {e}"
    ctx.facts["nvenc"] = ok
    ctx.line(f"nvenc: {'yes' if ok else 'no'}{why}")
    post_facts(ctx)
