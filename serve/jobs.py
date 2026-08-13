"""Job execution — the thin layer between an HTTP request and `runners.execute_job`.

## Why a subprocess rather than an in-process call

`execute_job` is a long, CPU-bound, third-party-heavy pipeline. Running it inside the API
process would mean a segfault in a native audio library takes the control surface down with
it — and the control surface is the only way to find out what happened. A subprocess gives
isolation, a real kill switch for cancellation, and a natural place to tee stdout into the
job log. It also keeps the API responsive, which matters when the whole point is that a
webapp or an agent can poll it during a 40-minute job.

## Why this does not reimplement anything

`runners/execute_job.py` already validates stage names against STAGE_CLASSES and refuses
unknown ones up front. `runners/framework.py` already computes wiring and readiness without
running anything. This module calls both and translates the results to HTTP — it holds no
pipeline knowledge of its own, which is what keeps the local CLI, this API, and a future
serverless handler genuinely equivalent rather than three subtly different pipelines.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .events import EventLog, job_dir

REPO = Path(__file__).resolve().parent.parent

# Live subprocesses, so cancel can reach them. Jobs are not tracked across an API restart
# on purpose — the durable record is the event log on disk, and a job whose parent died is
# reported from its files rather than from memory.
_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


def available_stages() -> list[str]:
    """The stage vocabulary, read from the pipeline rather than duplicated here."""
    try:
        sys.path.insert(0, str(REPO))
        from runners.execute_job import STAGE_CLASSES     # type: ignore
        return sorted(STAGE_CLASSES)
    except Exception:
        return []


def validate(spec: dict) -> dict:
    """Cheap, total, and free — the check that stops an agent burning a pod on a typo.

    Returns {ok, problems, would_run, missing, wiring_ok}. Runs NOTHING: this is
    `Runner.wiring()` and `Runner.plan()`, which framework.py built precisely so the
    pipeline could be trusted "without spending an hour or a pod".
    """
    problems: list[str] = []
    stages = spec.get("stages") or []
    if not stages:
        problems.append("spec has no 'stages'")
    known = available_stages()
    if known:
        unknown = [s for s in stages if s not in known]
        if unknown:
            problems.append(f"unknown stage(s): {unknown}. Available: {known}")
    if not spec.get("job_id"):
        problems.append("spec has no 'job_id'")

    result = {"ok": not problems, "problems": problems,
              "wiring_ok": None, "would_run": [], "missing": {}}
    if problems or not known:
        return result

    try:
        sys.path.insert(0, str(REPO))
        from runners.execute_job import build_runner, context_from_spec   # type: ignore
        runner = build_runner(stages)
        ctx = context_from_spec(spec)
        plan = runner.plan(ctx)
        result["wiring_ok"] = plan.get("wiring_ok")
        result["would_run"] = [r["stage"] for r in plan.get("stages", []) if r["would_run"]]
        result["missing"] = {r["stage"]: r["missing"]
                             for r in plan.get("stages", []) if r.get("missing")}
        if not plan.get("wiring_ok"):
            result["ok"] = False
            result["problems"].extend(plan.get("wiring_problems", []))
    except Exception as exc:
        # A planning failure is information, not a crash — report it as a problem so the
        # caller sees why, rather than a 500 that says nothing.
        result["ok"] = False
        result["problems"].append(f"{type(exc).__name__}: {exc}")
    return result


def submit(spec: dict) -> dict:
    """Start a job. Idempotent on job_id — re-POSTing returns the existing job.

    Agents retry on timeout, and a retry that silently starts a SECOND pod-hour of work is
    an expensive surprise. So the job id is the idempotency key.
    """
    job_id = spec["job_id"]
    log = EventLog(job_id, spec=spec)
    st = log.status()
    with _lock:
        if job_id in _procs and _procs[job_id].poll() is None:
            return {"job_id": job_id, "state": st.get("job_state"), "existing": True}
    if st.get("job_state") in ("running", "done", "failed", "cancelled"):
        return {"job_id": job_id, "state": st.get("job_state"), "existing": True}

    d = job_dir(job_id)
    spec_path = d / "spec.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    log.emit(stage="", index=0, total=len(spec.get("stages", [])),
             state="running", job_state="running", event="submitted")

    env = dict(os.environ)
    env["LINGUA_JOB_ID"] = job_id
    env["PYTHONUNBUFFERED"] = "1"
    logfile = (d / "job.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "runners.execute_job", "--spec", str(spec_path)],
        cwd=str(REPO), env=env, stdout=logfile, stderr=subprocess.STDOUT,
        start_new_session=True,     # own process group, so cancel kills children too
    )
    with _lock:
        _procs[job_id] = proc
    threading.Thread(target=_reap, args=(job_id, proc, logfile), daemon=True).start()
    return {"job_id": job_id, "state": "running", "existing": False}


def _reap(job_id: str, proc: subprocess.Popen, logfile) -> None:
    """Wait for exit and record the terminal event.

    Without this a crashed job sits at 'running' forever and every poller waits on a
    process that is gone — the failure mode where the status endpoint is worse than no
    status endpoint, because it is confidently wrong.
    """
    rc = proc.wait()
    try:
        logfile.close()
    except Exception:
        pass
    with _lock:
        _procs.pop(job_id, None)
    log = EventLog(job_id)
    st = log.status()

    # A cancelled job exits non-zero because we SIGTERM'd it, so the naive reading is
    # "failed" — which is wrong, and wrong in a way that matters: an operator scanning for
    # failures should not have to sift out the jobs they cancelled on purpose. Cancellation
    # is already terminal, so preserve it rather than racing over it.
    if st.get("job_state") == "cancelled":
        log.emit(stage="", state="skipped", index=st.get("index", 0),
                 total=st.get("total", 0), job_state="cancelled", returncode=rc,
                 note="process exited after cancellation")
        return

    state = "done" if rc == 0 else "failed"
    log.emit(stage="", state="ok" if rc == 0 else "failed",
             index=st.get("index", 0), total=st.get("total", 0),
             job_state=state, returncode=rc)


def cancel(job_id: str) -> dict:
    with _lock:
        proc = _procs.get(job_id)
    if not proc or proc.poll() is not None:
        return {"job_id": job_id, "cancelled": False, "reason": "not running"}

    # Record the intent BEFORE killing. The reaper thread wakes the instant the process
    # dies and writes a terminal state from the exit code; if the kill came first it could
    # win that race and stamp "failed" on a job the operator deliberately stopped. Marking
    # first makes the outcome deterministic — _reap sees "cancelled" and preserves it.
    EventLog(job_id).emit(stage="", state="skipped", job_state="cancelled",
                          event="cancelled")
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    return {"job_id": job_id, "cancelled": True}


def list_jobs() -> list[dict]:
    """Every job this pod knows about, read from disk rather than memory so it survives
    an API restart."""
    root = job_dir("_").parent
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        st = EventLog(d.name).status()
        out.append({"job_id": d.name, "job_state": st.get("job_state"),
                    "stage": st.get("stage"),
                    "progress": f"{st.get('index', 0)}/{st.get('total', 0)}",
                    "updated_at": st.get("updated_at")})
    return out
