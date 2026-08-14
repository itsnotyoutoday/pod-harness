"""The /v1 control surface — one API, two audiences.

## The audiences want the same thing, with one asymmetry

A webapp and an agent both want to submit work and watch it. The asymmetry is that a
webapp can afford to receive a megabyte and render 2% of it; an agent pays for every byte
it reads, forever, in a context window it cannot get back. So the rules below are written
for the agent, and the webapp is fine under them:

  * No endpoint returns an unbounded body. Log and event reads are capped and ALWAYS
    report what was omitted (`total_bytes`, `truncated`), because a silently truncated
    response is worse than a small one.
  * `/summary` exists so the common question — "what happened to my job" — costs ~500
    tokens instead of a log.
  * `dry_run` is free and total. An agent can validate a spec before spending a pod-hour,
    which is the difference between a typo costing nothing and costing real money.
  * `GET /v1/` is self-describing. An agent that has lost its context can rediscover the
    whole surface, including the live stage vocabulary, in one call.
  * Errors carry a `hint` naming the next action, not just what failed.

## Why the same paths exist on the pod and on the control plane

The control plane proxies these paths for pods, and answers them from S3 for serverless
workers and for pods that no longer exist. A client written against this surface keeps
working as the compute moves underneath it — which is the whole point, given the compute
is expected to move from pods to serverless.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from . import code as code_mod
from . import extensions as ext_mod
from . import jobs as J
from pod_harness.events import EventLog, job_dir, log_root, read_tail


def _mint_job_id() -> str:
    """Job ids are minted HERE, never accepted from the client.

    A client-chosen id collides the moment two callers pick the same string, and there is no
    way to detect that after the fact. ULIDs are server-minted and sort by creation time, so
    a job listing is chronological and log correlation needs no join. The client's channel
    for de-duplication is `idempotency_key`, which is checked separately.
    """
    try:
        from pod_harness.registry import new_job_id
        return new_job_id()
    except Exception:
        import secrets, time
        return f"job_{int(time.time()*1000):013d}{secrets.token_hex(5)}"

API_VERSION = "1"
MAX_TAIL = 262_144          # 256 KB ceiling on any single log read
MAX_WAIT = 60.0             # longest a long-poll may block

app = FastAPI(
    title="lingua pod control API",
    version=API_VERSION,
    docs_url="/v1/docs",
    openapi_url="/v1/openapi.json",   # generates the Node client and, later, an MCP shim
)


# Workload endpoints, mounted under /v1/x. Done at import so they exist before the first
# request, and non-fatal: a pod whose workload API failed to load is exactly the pod you
# need the core endpoints on to find out why.
ext_mod.mount(app)


def auth(x_podh_token: str | None = Header(default=None)) -> None:
    """Shared-token auth, enforced here as well as at Caddy.

    Defence in depth on purpose: a misconfigured proxy that forwards everything must not
    also mean an open control surface. If no token is configured the API refuses rather
    than defaulting open — a control plane that fails open is worse than one that fails.
    """
    want = os.environ.get("PODH_API_TOKEN", "")
    if not want:
        raise HTTPException(503, detail={
            "error": "PODH_API_TOKEN is not set on this pod",
            "hint": "set PODH_API_TOKEN in the pod env and restart; the API refuses to "
                    "serve unauthenticated rather than defaulting open"})
    if x_podh_token != want:
        raise HTTPException(401, detail={
            "error": "bad or missing X-Podh-Token header",
            "hint": "send header X-Podh-Token: <the pod's PODH_API_TOKEN>"})


@app.get("/v1/health")
def health() -> dict:
    """Unauthenticated on purpose — a liveness probe that needs a secret is useless to a
    launcher deciding whether the pod came up."""
    return {"ok": True, "version": API_VERSION,
            "pod_id": os.environ.get("RUNPOD_POD_ID", "local"),
            "uptime_hint": "see /v1/ for the full surface (requires token)"}


@app.get("/v1/contract", dependencies=[Depends(auth)])
def contract() -> dict:
    """The interface this harness implements, served by the harness itself.

    A loader can validate its spec against the contract of the EXACT image it is about to
    launch, rather than against a vendored copy that may have drifted from it. That is the
    whole reason the two repos can share no code and still stay in step: the authority
    travels with the running code.
    """
    import json
    from pathlib import Path
    for c in (Path("/app/contract.json"), Path(__file__).resolve().parent.parent / "contract.json"):
        if c.is_file():
            return json.loads(c.read_text())
    raise HTTPException(
        status_code=500,
        detail={"error": "contract.json is not present in this image",
                "hint": "the image was built without its interface definition; rebuild it"})


@app.get("/v1/", dependencies=[Depends(auth)])
def discover() -> dict:
    """Self-description. One call rebuilds everything a caller needs to know."""
    return {
        "version": API_VERSION,
        "pod_id": os.environ.get("RUNPOD_POD_ID", "local"),
        "log_root": str(log_root()),
        # Read from the workload's published capabilities.json, not from a hardcoded
        # import. Empty means no code is loaded here — honest rather than misleading.
        "stages": J.available_stages(),
        "code": code_mod.resolve().as_dict(),
        # Extensions this pod is serving because of the workload loaded on it. Reported
        # even when loading FAILED — a silently absent endpoint is the failure mode this
        # codebase keeps getting bitten by.
        "extensions": ext_mod.describe(),
        "endpoints": {
            "GET  /v1/":                       "this document",
            "GET  /v1/health":                 "liveness, no auth",
            "POST /v1/jobs?dry_run=true":      "validate a spec, run nothing, free",
            "POST /v1/jobs":                   "submit; idempotent on spec.job_id",
            "GET  /v1/jobs":                   "list known jobs",
            "GET  /v1/jobs/{id}":              "status snapshot (cheap poll target)",
            "GET  /v1/jobs/{id}/summary":      "~500-token digest — start here",
            "GET  /v1/jobs/{id}/stages":       "per-stage table incl. verification detail",
            "GET  /v1/jobs/{id}/log?tail=N":   "bounded log tail, reports total_bytes",
            "GET  /v1/jobs/{id}/events":       "?since_seq=N&wait=S — long-poll",
            "GET  /v1/jobs/{id}/artifacts":    "produced files",
            "DEL  /v1/jobs/{id}":              "cancel",
        },
        "job_spec_schema": {
            "spec_version": 2,
            "idempotency_key": "str|null — YOUR de-dup key; the job_id is minted here",
            "code": {"root": "code/<repo>/<sha>", "rev": "sha"},
            "pipeline": {"stages_from": "module:REGISTRY", "stages": "list[str]"},
            "exec": "{module, args} — escape hatch, mutually exclusive with pipeline",
            "mount": {"kind": "volume|object", "root": "corpus/"},
            "runner": {"provider": "runpod|local", "flavor": "cpu3c",
                       "budget_min": "int — enforced EXTERNALLY by the reaper"},
            "resume": {"enabled": "bool", "from": "auto|<stage>|never"},
            "params": "dict — opaque to the engine, meaningful to your stages",
        },
        "conventions": {
            "auth": "header X-Podh-Token",
            "job_ids": "server-minted ULIDs; use idempotency_key to de-duplicate",
            "dry_run_depth": "shallow (capabilities.json, no code) vs deep (Runner.plan)",
            "bounded_reads": "log and event responses are capped; check `truncated`",
            "cursor": "events carry monotonic `seq`; resume with since_seq",
        },
    }


@app.post("/v1/jobs", dependencies=[Depends(auth)])
def submit_job(spec: dict, dry_run: bool = Query(default=False)) -> JSONResponse:
    """Submit, or validate for free.

    The job id is minted here. A client-supplied id is IGNORED — it collides the moment two
    callers pick the same string, with no way to detect it afterwards. Use
    `idempotency_key` instead: a retried submit returns the existing job rather than
    starting a second pod-hour.
    """
    if not isinstance(spec, dict):
        raise HTTPException(422, detail={
            "error": "body must be a job spec object",
            "hint": "GET /v1/ returns job_spec_schema"})
    if not (spec.get("pipeline") or spec.get("exec") or spec.get("stages")):
        raise HTTPException(422, detail={
            "error": "spec needs 'pipeline' (stage-based) or 'exec' (raw command)",
            "hint": "GET /v1/ returns job_spec_schema; v1 specs with a top-level "
                    "'stages' list are accepted and lifted automatically"})

    check = J.validate(spec)
    if dry_run:
        return JSONResponse(check, status_code=200 if check["ok"] else 422)
    if not check["ok"]:
        raise HTTPException(422, detail={
            "error": "spec did not validate", "problems": check["problems"],
            "depth": check.get("depth"),
            "hint": "POST the same body with ?dry_run=true to iterate for free"})

    # Idempotency: an existing job with this key is returned rather than duplicated. An
    # agent that retries on timeout must not start a second pod-hour.
    key = spec.get("idempotency_key") or spec.get("job_id")
    existing = J.find_by_idempotency_key(key) if key else None
    if existing:
        return JSONResponse({**existing, "existing": True}, status_code=200)

    job_id = _mint_job_id()
    return JSONResponse(J.submit(spec, job_id=job_id), status_code=202)


@app.get("/v1/jobs/{job_id}/stages", dependencies=[Depends(auth)])
def job_stages(job_id: str) -> dict:
    """Per-stage table, including what verification checked and what it rejected.

    This endpoint is only possible because the engine understands stages. Under a
    stage-blind design the best available answer would be "the job failed"; here it is
    "align failed, 42.1s, verification rejected it because alignments were not produced".
    """
    _require(job_id)
    return J.stages(job_id)


@app.get("/v1/jobs", dependencies=[Depends(auth)])
def list_jobs() -> dict:
    return {"jobs": J.list_jobs()}


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
def job_status(job_id: str) -> dict:
    _require(job_id)
    return EventLog(job_id).status()


@app.get("/v1/jobs/{job_id}/summary", dependencies=[Depends(auth)])
def job_summary(job_id: str, tail_lines: int = Query(default=20, ge=0, le=200)) -> dict:
    _require(job_id)
    return EventLog(job_id).summary(tail_lines=tail_lines)


@app.get("/v1/jobs/{job_id}/log", dependencies=[Depends(auth)])
def job_log(job_id: str, tail: int = Query(default=8192, ge=0, le=MAX_TAIL)) -> dict:
    """Bounded by construction. `tail` cannot exceed MAX_TAIL even if asked."""
    _require(job_id)
    r = read_tail(job_dir(job_id) / "job.log", tail)
    r["truncated"] = r["total_bytes"] > r["returned_bytes"]
    r["hint"] = ("full file is served with Range support at "
                 f"/jobs/{job_id}/job.log on the static root") if r["truncated"] else None
    return r


@app.get("/v1/jobs/{job_id}/events", dependencies=[Depends(auth)])
def job_events(job_id: str,
               since_seq: int = Query(default=0, ge=0),
               wait: float = Query(default=0.0, ge=0.0, le=MAX_WAIT),
               limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    """Poll, or long-poll with `wait`.

    `wait` is the agent-native transport: one request that blocks until something changes
    gives push-like latency with no connection state, and behaves identically whether it is
    served by a pod, by a serverless worker, or replayed from S3 after the compute is gone.
    """
    _require(job_id)
    log = EventLog(job_id)
    evs = log.wait(since_seq, timeout=wait) if wait else log.since(since_seq, limit=limit)
    evs = evs[:limit]
    return {"job_id": job_id, "events": evs,
            "next_seq": evs[-1]["seq"] if evs else since_seq,
            "job_state": log.status().get("job_state")}


@app.get("/v1/jobs/{job_id}/artifacts", dependencies=[Depends(auth)])
def job_artifacts(job_id: str) -> dict:
    _require(job_id)
    d = job_dir(job_id)
    files = []
    for p in sorted(d.rglob("*")):
        if p.is_file():
            files.append({"path": str(p.relative_to(d)), "bytes": p.stat().st_size})
    return {"job_id": job_id, "root": str(d), "files": files}


@app.delete("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
def cancel_job(job_id: str) -> dict:
    _require(job_id)
    return J.cancel(job_id)


def _require(job_id: str) -> None:
    if not job_dir(job_id).exists():
        raise HTTPException(404, detail={
            "error": f"no such job: {job_id}",
            "hint": "GET /v1/jobs lists what this pod knows about"})
