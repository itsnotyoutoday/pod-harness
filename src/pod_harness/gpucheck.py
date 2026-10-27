"""`podh-gpucheck` — prove the GPU works before paying for anything that needs it.

## Why

A rented GPU can be broken in ways the provider does not see: the driver cannot talk to
the card, the container got fewer GPUs than it was billed for, or CUDA initialises into
"CUDA unknown error". Nothing fails at boot. The pod mounts data, downloads models for ten
minutes, starts the workload, and only then dies in the first CUDA call — or worse, a
framework quietly falls back to the CPU and runs a hundred times slower while billing GPU
rates. Asking the GPU directly takes seconds, so it is asked first.

## What it does

    1. decide whether this pod is a GPU pod at all (see `decide`)
    2. `nvidia-smi -L` must list at least the GPUs the job asked for
    3. a CUDA init probe in a subprocess, through the first python that can import torch:
       torch.cuda.init(), device_count(), get_device_name(0) — with a timeout, because a
       broken driver HANGS as often as it raises. No torch anywhere: the probe is skipped
       and nvidia-smi alone decides.

On failure it exits 7 and, when the control plane is configured, reports
done(7, reason=<the error>, stage="gpu_check") so the pod is released and the reason is on
record — unless --no-report, which podh-init uses because its own fatal() reports.

`--scan-log FILE` is a separate helper for supervisors: exit 7 if the file contains one of
the fatal CUDA strings below. It does not report; what to do about it is the caller's call.

Environment:
    PODH_GPU_COUNT          GPUs the job asked for. >0 check; 0 skip (CPU pod); unset =
                            unknown (an older launcher): check only if nvidia-smi exists
                            and lists a GPU.
    PODH_GPUCHECK_PYTHON    python(s) to probe with, comma-separated; default python3
    PODH_GPUCHECK_TIMEOUT   CUDA probe timeout in seconds; default 120
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

EXIT_GPU = 7

#: Strings that mean CUDA is unusable in this process, whatever the workload was doing.
#: Each is a terminal state, not a transient: retrying in the same container does not help.
FATAL_CUDA = ("CUDA unknown error",
              "No CUDA GPUs are available",
              "CUDA driver initialization failed",
              "CUDA error: no kernel image")

#: Run in the candidate interpreter. Markers rather than bare output so a site-customize
#: that prints, or a torch import warning, cannot be mistaken for the answer. Exit 3 with
#: PODH_NO_TORCH means "try the next python", which is not a GPU failure.
_PROBE = r"""
import json, sys
try:
    import torch
except Exception as e:
    print("PODH_NO_TORCH " + type(e).__name__)
    sys.exit(3)
torch.cuda.init()
n = torch.cuda.device_count()
print("PODH_PROBE " + json.dumps({
    "torch": torch.__version__, "cuda": torch.version.cuda, "count": n,
    "name": torch.cuda.get_device_name(0) if n else None}))
"""


def _run(cmd: list, timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run with a timeout that cannot itself hang.

    run()'s own timeout kills the child and then waits for its pipes to close. A process
    stuck inside the GPU driver can be unkillable for a long time (uninterruptible sleep),
    and then that second wait is unbounded — the check would hang on exactly the failure it
    exists to report. So the wait after the kill is bounded too, and the child abandoned.
    """
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


# -- deciding ------------------------------------------------------------------------------

def requested(env=None) -> Optional[int]:
    """PODH_GPU_COUNT as an int, or None when unset/unreadable (= unknown)."""
    raw = (env if env is not None else os.environ).get("PODH_GPU_COUNT", "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        print(f"  [gpucheck] PODH_GPU_COUNT={raw!r} is not a number — treating as unknown",
              file=sys.stderr)
        return None


def list_gpus(timeout: float = 30) -> dict:
    """`nvidia-smi -L`: {"ok", "names", "error"}. Missing binary is an error, not zero GPUs."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"ok": False, "names": [], "error": "nvidia-smi not found"}
    try:
        p = _run([exe, "-L"], timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "names": [], "error": f"nvidia-smi -L timed out after {timeout:.0f}s"}
    if p.returncode != 0:
        why = _last_line(p.stdout + "\n" + p.stderr) or f"exit {p.returncode}"
        return {"ok": False, "names": [], "error": f"nvidia-smi failed: {why}"}
    names = [re.sub(r"\s*\(UUID:.*\)$", "", m.group(1)).strip()
             for m in re.finditer(r"^GPU \d+:\s*(.+)$", p.stdout, re.M)]
    return {"ok": True, "names": names, "error": None}


def decide(count: Optional[int], *, force: bool = False, skip: bool = False,
           lister=list_gpus) -> dict:
    """Whether to run, and how many GPUs to expect: {"run", "expected", "why", "listing"}.

    Unset means the launcher predates PODH_GPU_COUNT, so the pod is asked instead: a GPU
    pod has nvidia-smi and it lists something. That inference only ever turns the check ON
    — a CPU pod on an old launcher simply has no nvidia-smi and is left alone.
    """
    if skip:
        return {"run": False, "expected": 0, "why": "gpu check: skipped (--skip)", "listing": None}
    if force:
        return {"run": True, "expected": count if count else 1, "why": "forced", "listing": None}
    if count == 0:
        return {"run": False, "expected": 0, "why": "cpu pod: skipped", "listing": None}
    if count:
        return {"run": True, "expected": count, "why": f"job asked for {count}", "listing": None}
    if not shutil.which("nvidia-smi"):
        return {"run": False, "expected": 0, "why": "no GPU requested or visible: skipped",
                "listing": None}
    listing = lister()
    if listing["ok"] and listing["names"]:
        return {"run": True, "expected": 1, "why": "nvidia-smi lists a GPU", "listing": listing}
    detail = f" ({listing['error']})" if listing.get("error") else ""
    return {"run": False, "expected": 0, "listing": listing,
            "why": f"no GPU requested or visible{detail}: skipped"}


# -- checking ------------------------------------------------------------------------------

def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def driver_version(timeout: float = 30) -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        p = _run([exe, "--query-gpu=driver_version", "--format=csv,noheader"], timeout)
    except Exception:
        return None
    return (p.stdout.strip().splitlines() or [None])[0] if p.returncode == 0 else None


def candidates(explicit: Optional[list] = None, env=None) -> list:
    if explicit:
        return list(explicit)
    raw = (env if env is not None else os.environ).get("PODH_GPUCHECK_PYTHON", "")
    found = [x for x in re.split(r"[,\s]+", raw) if x]
    return found or ["python3"]


def cuda_probe(pythons: list, timeout: float = 120) -> dict:
    """CUDA init through the first python that has torch.

    {"ok": True, ...} on success; {"ok": False, "error"} on failure; {"ok": True,
    "skipped": ...} when no candidate has torch. A timeout is a failure and stops the
    search: a CUDA init that hangs under one interpreter is the GPU, not the interpreter.
    """
    tried = []
    for py in pythons:
        exe = shutil.which(py) or (py if os.path.isfile(py) and os.access(py, os.X_OK) else None)
        if not exe:
            tried.append(f"{py} (not found)")
            continue
        try:
            p = _run([exe, "-c", _PROBE], timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "python": py,
                    "error": f"CUDA probe timed out after {timeout:.0f}s ({py})"}
        if p.returncode == 3 and "PODH_NO_TORCH" in p.stdout:
            tried.append(f"{py} (no torch)")
            continue
        m = re.search(r"^PODH_PROBE (.+)$", p.stdout, re.M)
        if p.returncode != 0 or not m:
            why = _last_line(p.stderr) or _last_line(p.stdout) or f"exit {p.returncode}"
            return {"ok": False, "python": py, "error": f"CUDA init failed: {why}"}
        try:
            got = json.loads(m.group(1))
        except ValueError:
            return {"ok": False, "python": py, "error": "CUDA probe printed an unreadable answer"}
        return {"ok": True, "python": py, **got}
    return {"ok": True, "skipped": "no torch: CUDA probe skipped", "tried": tried}


def check(expected: int, *, pythons: list, probe_timeout: float = 120,
          smi_timeout: float = 30, listing: Optional[dict] = None) -> dict:
    out = {"ok": False, "ran": True, "skipped": None, "expected": expected, "count": 0,
           "gpus": [], "driver_version": None, "cuda_probe": None, "error": None}
    listing = listing or list_gpus(smi_timeout)
    out["gpus"], out["count"] = listing["names"], len(listing["names"])
    if not listing["ok"]:
        out["error"] = f"{listing['error']} (job asked for {expected} GPU(s))"
        return out
    if out["count"] < expected:
        out["error"] = f"nvidia-smi lists {out['count']} GPU(s), job asked for {expected}"
        return out
    out["driver_version"] = driver_version(smi_timeout)

    probe = cuda_probe(pythons, probe_timeout)
    out["cuda_probe"] = probe
    if not probe["ok"]:
        out["error"] = probe["error"]
        return out
    if not probe.get("skipped") and probe.get("count", 0) < expected:
        out["error"] = (f"torch sees {probe.get('count', 0)} CUDA device(s), "
                        f"job asked for {expected}")
        return out
    out["ok"] = True
    return out


def human(r: dict) -> str:
    if not r["ran"]:
        return r["skipped"]
    if not r["ok"]:
        return f"gpu check: FAILED — {r['error']}"
    names = ", ".join(r["gpus"])
    s = f"gpu check: ok — {r['count']} GPU(s): {names}"
    if r["driver_version"]:
        s += f"; driver {r['driver_version']}"
    p = r["cuda_probe"] or {}
    if p.get("skipped"):
        s += f"; {p['skipped']}"
    else:
        s += f"; torch {p.get('torch')} (cuda {p.get('cuda')}) sees {p.get('count')} via {p.get('python')}"
    return s


# -- log scan ------------------------------------------------------------------------------

def scan_log(path: str) -> list:
    """[(line_no, pattern, line)] for every fatal CUDA string in `path`. Case-insensitive,
    streamed, so a multi-gigabyte log costs memory for one line at a time."""
    pats = [(p, p.lower()) for p in FATAL_CUDA]
    hits = []
    with open(path, "rb") as f:
        for n, raw in enumerate(f, 1):
            low = raw.decode("utf-8", "replace").lower()
            for orig, p in pats:
                if p in low:
                    hits.append((n, orig, raw.decode("utf-8", "replace").strip()[:300]))
                    break
    return hits


# -- CLI -----------------------------------------------------------------------------------

def _report_failure(error: str) -> None:
    from . import heartbeat
    if not os.environ.get("PODH_CONTROL_URL"):
        return
    try:
        Path(heartbeat.STOP_FILE).touch()     # the work is over; stop claiming otherwise
    except OSError:
        pass
    if heartbeat.done(EXIT_GPU, reason=error, stage="gpu_check") is None:
        print(f"  [gpucheck] could not report to the control plane: "
              f"{heartbeat.last_error() or 'not configured'}", file=sys.stderr)


def _scan_main(a) -> int:
    try:
        hits = scan_log(a.scan_log)
    except OSError as e:
        print(f"podh-gpucheck: cannot read {a.scan_log}: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"file": a.scan_log, "fatal": bool(hits),
                          "hits": [{"line": n, "pattern": p, "text": t} for n, p, t in hits]}))
    elif hits:
        n, p, t = hits[0]
        more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
        print(f"fatal CUDA error in {a.scan_log}:{n}: {t}{more}")
    else:
        print(f"no fatal CUDA errors in {a.scan_log}")
    return EXIT_GPU if hits else 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="podh-gpucheck",
                                 description="check the GPU works before using it (exit 7 if not)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--force", action="store_true", help="check even if this looks like a CPU pod")
    g.add_argument("--skip", action="store_true", help="do not check; exit 0")
    ap.add_argument("--python", action="append", help="interpreter to probe CUDA with (repeatable)")
    ap.add_argument("--timeout", type=float,
                    default=float(os.environ.get("PODH_GPUCHECK_TIMEOUT") or 120),
                    help="CUDA probe timeout, seconds (default 120)")
    ap.add_argument("--smi-timeout", type=float, default=30, help="nvidia-smi timeout, seconds")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of a line")
    ap.add_argument("--no-report", action="store_true",
                    help="do not report a failure to the control plane")
    ap.add_argument("--scan-log", metavar="FILE",
                    help="only scan FILE for fatal CUDA errors; exit 7 if any")
    a = ap.parse_args(argv)

    if a.scan_log:
        return _scan_main(a)

    d = decide(requested(), force=a.force, skip=a.skip,
               lister=lambda: list_gpus(a.smi_timeout))
    if not d["run"]:
        r = {"ok": True, "ran": False, "skipped": d["why"], "expected": 0, "count": None,
             "gpus": (d["listing"] or {}).get("names", []), "driver_version": None,
             "cuda_probe": None, "error": None}
    else:
        r = check(d["expected"], pythons=candidates(a.python), probe_timeout=a.timeout,
                  smi_timeout=a.smi_timeout, listing=d["listing"])
    print(json.dumps(r) if a.json else human(r), flush=True)
    if r["ok"]:
        return 0
    if not a.no_report:
        _report_failure(r["error"])
    return EXIT_GPU


if __name__ == "__main__":
    raise SystemExit(main())
