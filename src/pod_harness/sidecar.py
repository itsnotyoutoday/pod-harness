"""Sidecar mode: pod-harness's /v1 control surface inside a container it did not build.

The harness image owns its entrypoint and runs staged jobs. Catalog images (runpod/comfyui
and the like) own theirs, and rebuilding them on our base costs a custom image per upstream
release. The sidecar is the other way round: `pip install` it into whatever is running,
start it beside the real workload, and the pod gains the observability the harness image
has — without the harness taking over anything.

What it does:
  - serves /v1 on PODH_SIDECAR_PORT (default 8000), token-authenticated like the harness
  - lists and tails the log files it is told about (bounded reads, offset-follow, long-poll)
  - takes progress lines from the workload (POST /v1/progress) and serves them as events
  - reports disk and GPU state
  - lists and serves files under declared artifact roots, read-only, no escaping a root
  - relays "terminate me" to pod-control (POST /v1/done)
  - optionally heartbeats to pod-control, ONLY while a liveness URL answers

What it deliberately does not do: run stages, own the entrypoint, or heartbeat by default.
A sidecar that heartbeats on its own keeps a pod "alive" after the workload beside it has
hung, which disables pod-control's primary reaper (heartbeating, then quiet). So heartbeats
need PODH_SIDECAR_HEARTBEAT=1 AND a PODH_SIDECAR_LIVENESS URL that the workload answers.

Environment:
  PODH_API_TOKEN            required; header X-Podh-Token on every route but /v1/health
  PODH_SIDECAR_PORT         listen port (default 8000); expose it as "<port>/http"
  PODH_SIDECAR_LOGS         comma list of `name=path` or `path` (name = file name); globs ok
  PODH_SIDECAR_ARTIFACTS    comma list of directories served read-only
  PODH_SIDECAR_HEARTBEAT    "1" to heartbeat to pod-control (PODH_CONTROL_URL/_TOKEN)
  PODH_SIDECAR_LIVENESS     URL that must answer 2xx for a heartbeat to be sent
  PODH_SIDECAR_BEAT_SEC     heartbeat interval (default 60)
  RUNPOD_POD_ID, PODH_CONTROL_URL, PODH_CONTROL_TOKEN, PODH_CONTROL_JOB_ID: as the harness
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse

from . import heartbeat

MAX_TAIL = 262_144          # no single read returns more than 256 KB
MAX_WAIT = 30.0             # longest a long-poll blocks
MAX_EVENTS = 2_000          # progress lines kept in memory

app = FastAPI(title="pod-harness sidecar", version="1",
              docs_url="/v1/docs", openapi_url="/v1/openapi.json")
_STARTED = time.time()
_EVENTS: list = []
_EVENTS_LOCK = threading.Condition()
_BEAT = {"enabled": False, "last_sent": None, "last_ok": None, "last_skip": None}


def auth(x_podh_token: Optional[str] = Header(default=None)) -> None:
    want = os.environ.get("PODH_API_TOKEN", "")
    if not want:
        # Refuse rather than serve open: logs routinely carry paths, prompts and errors.
        raise HTTPException(503, detail={"error": "PODH_API_TOKEN is not set on this pod"})
    if x_podh_token != want:
        raise HTTPException(401, detail={"error": "bad or missing X-Podh-Token"})


def _logs() -> dict:
    """{name: Path} from PODH_SIDECAR_LOGS, globs expanded at each call (logs appear late)."""
    out = {}
    for item in [x.strip() for x in os.environ.get("PODH_SIDECAR_LOGS", "").split(",") if x.strip()]:
        name, sep, pattern = item.partition("=")
        if not sep:
            name, pattern = "", item
        matches = sorted(glob.glob(pattern)) or ([pattern] if not any(c in pattern for c in "*?[") else [])
        for m in matches:
            p = Path(m)
            out[(name if name and len(matches) == 1 else p.name)] = p
    return out


def _roots() -> list:
    return [Path(x.strip()).resolve() for x in os.environ.get("PODH_SIDECAR_ARTIFACTS", "").split(",")
            if x.strip()]


def _inside_root(rel: str) -> Path:
    """Resolve `rel` (relative to a root, or absolute inside one) and refuse anything outside
    every root, symlinks included: resolve() follows them before the check."""
    for root in _roots():
        cand = (root / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
        if cand == root or root in cand.parents:
            if cand.exists():
                return cand
    raise HTTPException(404, detail={"error": f"{rel!r} is not under an artifact root"})


@app.get("/v1/health")
def health() -> dict:
    return {"ok": True, "mode": "sidecar", "pod_id": os.environ.get("RUNPOD_POD_ID", ""),
            "uptime_sec": round(time.time() - _STARTED)}


@app.get("/v1/", dependencies=[Depends(auth)])
def index() -> dict:
    with _EVENTS_LOCK:
        last = _EVENTS[-1] if _EVENTS else None
    return {"mode": "sidecar", "pod_id": os.environ.get("RUNPOD_POD_ID", ""),
            "uptime_sec": round(time.time() - _STARTED),
            "logs": sorted(_logs()), "artifact_roots": [str(r) for r in _roots()],
            "heartbeat": dict(_BEAT), "last_progress": last,
            "routes": ["/v1/health", "/v1/logs", "/v1/logs/{name}", "/v1/progress", "/v1/events",
                       "/v1/system", "/v1/artifacts", "/v1/artifacts/file", "/v1/done"]}


@app.get("/v1/logs", dependencies=[Depends(auth)])
def logs() -> dict:
    out = []
    for name, p in sorted(_logs().items()):
        st = p.stat() if p.exists() else None
        out.append({"name": name, "path": str(p), "exists": st is not None,
                    "size": st.st_size if st else 0, "mtime": st.st_mtime if st else None})
    return {"logs": out}


@app.get("/v1/logs/{name}", dependencies=[Depends(auth)])
def log(name: str, tail: int = Query(8192, ge=0, le=MAX_TAIL), offset: Optional[int] = Query(None, ge=0),
        wait: float = Query(0, ge=0, le=MAX_WAIT)) -> dict:
    """The end of a log (`tail` bytes), or everything after `offset` to follow it.

    With `offset` and `wait`, blocks until the file grows past `offset` or `wait` passes.
    `next_offset` is where the next call should start; `total_bytes` is always reported so
    a truncated read admits it.
    """
    p = _logs().get(name)
    if p is None:
        raise HTTPException(404, detail={"error": f"no log {name!r}", "have": sorted(_logs())})
    deadline = time.time() + wait
    while offset is not None and wait and time.time() < deadline and \
            (not p.exists() or p.stat().st_size <= offset):
        time.sleep(0.5)
    if not p.exists():
        return {"name": name, "text": "", "total_bytes": 0, "from_byte": 0, "next_offset": 0}
    size = p.stat().st_size
    if offset is None:
        start = max(0, size - tail)
    else:
        start = min(offset, size)
    end = min(size, start + MAX_TAIL)
    with p.open("rb") as f:
        f.seek(start)
        data = f.read(end - start)
    return {"name": name, "text": data.decode("utf-8", "replace"), "total_bytes": size,
            "from_byte": start, "next_offset": end, "truncated": end < size}


@app.post("/v1/progress", dependencies=[Depends(auth)])
def progress(body: dict) -> dict:
    """A workload step, e.g. {"step": "model wan2.2_high (14.3G in 190s)"}."""
    step = str(body.get("step") or "").strip()
    if not step:
        raise HTTPException(422, detail={"error": "step is required"})
    with _EVENTS_LOCK:
        seq = (_EVENTS[-1]["seq"] + 1) if _EVENTS else 1
        ev = {"seq": seq, "at": time.time(), "step": step[:500],
              "detail": body.get("detail") if isinstance(body.get("detail"), (dict, str)) else None}
        _EVENTS.append(ev)
        del _EVENTS[:-MAX_EVENTS]
        _EVENTS_LOCK.notify_all()
    return {"seq": seq}


@app.get("/v1/events", dependencies=[Depends(auth)])
def events(since_seq: int = Query(0, ge=0), wait: float = Query(0, ge=0, le=MAX_WAIT)) -> dict:
    """Progress lines after `since_seq`; long-polls up to `wait` seconds for the next one."""
    deadline = time.time() + wait
    with _EVENTS_LOCK:
        while wait and not any(e["seq"] > since_seq for e in _EVENTS):
            left = deadline - time.time()
            if left <= 0:
                break
            _EVENTS_LOCK.wait(left)
        got = [e for e in _EVENTS if e["seq"] > since_seq]
        last = _EVENTS[-1]["seq"] if _EVENTS else 0
    return {"events": got, "last_seq": last}


def _gpu() -> list:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 5:
            gpus.append({"name": parts[0], "mem_used_mb": parts[1], "mem_total_mb": parts[2],
                         "util_pct": parts[3], "temp_c": parts[4]})
    return gpus


@app.get("/v1/system", dependencies=[Depends(auth)])
def system() -> dict:
    disks = {}
    for path in {"/", "/workspace", *[str(r) for r in _roots()]}:
        if os.path.exists(path):
            u = shutil.disk_usage(path)
            disks[path] = {"free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)}
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    return {"disks": disks, "gpus": _gpu(), "load": load}


@app.get("/v1/artifacts", dependencies=[Depends(auth)])
def artifacts(path: str = "") -> dict:
    if not path:
        return {"roots": [str(r) for r in _roots()]}
    p = _inside_root(path)
    if p.is_file():
        return {"path": str(p), "size": p.stat().st_size}
    entries = []
    for c in sorted(p.iterdir())[:1000]:
        entries.append({"name": c.name, "dir": c.is_dir(), "size": c.stat().st_size if c.is_file() else None})
    return {"path": str(p), "entries": entries}


@app.get("/v1/artifacts/file", dependencies=[Depends(auth)])
def artifact_file(path: str):
    p = _inside_root(path)
    if not p.is_file():
        raise HTTPException(404, detail={"error": f"{path!r} is not a file"})
    return FileResponse(str(p))


@app.post("/v1/done", dependencies=[Depends(auth)])
def done(body: Optional[dict] = None) -> dict:
    """Ask pod-control to terminate this pod now, and stop heartbeating either way."""
    Path("/tmp/podh-stop-heartbeat").touch()
    url, _, pod_id = heartbeat._cfg()
    if not url or not pod_id:
        raise HTTPException(503, detail={"error": "PODH_CONTROL_URL or RUNPOD_POD_ID is not set",
                                         "note": "heartbeat stopped; pod-control's sweep still bounds the pod"})
    r = heartbeat.done(int((body or {}).get("rc", 0)))
    return {"requested": True, "response": r, "error": None if r else heartbeat.last_error()}


def _live() -> bool:
    url = os.environ.get("PODH_SIDECAR_LIVENESS", "")
    if not url:
        return False
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "podh-sidecar"}),
                                    timeout=5) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _beat_loop(interval: float) -> None:
    while not os.path.exists("/tmp/podh-stop-heartbeat"):
        if _live():
            _BEAT["last_sent"] = time.time()
            if heartbeat.alive():
                _BEAT["last_ok"] = time.time()
        else:
            # Quiet on purpose: a workload that stopped answering must look stuck to the reaper.
            _BEAT["last_skip"] = time.time()
        time.sleep(interval)


def start_heartbeat() -> bool:
    """Heartbeat only when explicitly enabled AND a liveness URL is given."""
    if os.environ.get("PODH_SIDECAR_HEARTBEAT") != "1":
        return False
    if not os.environ.get("PODH_SIDECAR_LIVENESS"):
        print("  [sidecar] PODH_SIDECAR_HEARTBEAT=1 needs PODH_SIDECAR_LIVENESS; not heartbeating",
              flush=True)
        return False
    _BEAT["enabled"] = True
    threading.Thread(target=_beat_loop, args=(float(os.environ.get("PODH_SIDECAR_BEAT_SEC", 60)),),
                     daemon=True, name="podh-sidecar-heartbeat").start()
    return True


def main(argv: Optional[list] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="podh-serve", description="pod-harness control surface")
    ap.add_argument("--sidecar", action="store_true", help="serve /v1 beside an existing workload")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PODH_SIDECAR_PORT", 8000)))
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args(argv)
    if not a.sidecar:
        ap.error("only --sidecar is installable; the full harness ships as its own image")
    if not os.environ.get("PODH_API_TOKEN"):
        print("  [sidecar] PODH_API_TOKEN is not set: every route but /v1/health will answer 503",
              flush=True)
    start_heartbeat()
    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
