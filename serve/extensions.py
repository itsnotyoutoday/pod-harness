"""Let a workload add its own endpoints to the pod's `/v1` API.

## The gap this closes

The harness ships a fixed control surface: submit a job, poll it, read its log, cancel it.
That is the right surface for a BATCH pod — you hand it work at creation and it reports on
that work.

It is the wrong surface for a pod that is itself a service. lingua-detect wants to come up,
stay up, and answer "here is an audio file, what is it?" over HTTP. Expressed as a job, that
means writing a spec, submitting it, polling for completion and fetching an artifact — four
round trips and a filesystem, to do 0.4 seconds of arithmetic. The natural shape is one
POST with a file and one JSON response.

The harness cannot ship that endpoint, because the harness knows nothing about audio or
accents and must keep knowing nothing: `assert_independence.py` fails the build if any
workload concept appears here. So the workload has to supply it, the same way it supplies
stages.

## The mechanism, and why it mirrors `stages_from`

    "pipeline": {"stages_from": "detect.stages:STAGES",
                 "api_from":    "detect.api:ROUTES"}

`api_from` names a module attribute holding a FastAPI `APIRouter`. At serve start the
harness imports it from the synced code root and mounts it. Same contract shape as stages,
same failure mode when the code root is wrong, and nothing about the workload is compiled
in.

## Namespaced under /v1/x, deliberately

Workload routes mount beneath `/v1/x/`, never at `/v1/`. `contract.json` is a fixed
interface that `assert_contract.py` checks the API against, and that every loader and client
is written to. If a workload could define `/v1/jobs`, it could silently redefine the
contract for callers who have no idea a workload is loaded — and the conformance check would
either start failing or, worse, start passing against the wrong thing.

`/v1/x/` says exactly what it is: this pod's extras, present because of what is loaded on
it. `/v1/` lists them so a caller that lost context still finds them in one call.

## Failure is loud and contained

A broken extension must not take down the control surface. If the import fails the API still
serves — a pod whose workload API is broken is exactly the pod you need `/v1/jobs/{id}/log`
on to find out why. The error is recorded and reported by `/v1/` rather than swallowed,
because a silently absent endpoint is the failure mode this codebase has been bitten by
repeatedly: something looks wired, answers nothing, and is believed.
"""
from __future__ import annotations

import importlib
import os
from typing import Any

#: Where workload routes live. Not configurable — a caller must be able to tell core
#: endpoints from extensions by looking at the path.
PREFIX = "/v1/x"

#: Populated at mount time and reported by `/v1/`. Holds the outcome either way: what
#: mounted, or why nothing did.
STATE: dict[str, Any] = {"loaded": False, "spec": None, "routes": [], "error": None}


def _spec_from_env() -> str | None:
    """Which registry to load, in the order the pod learns things.

    The env var wins because it is what the launcher sets explicitly. The job spec is the
    fallback for a batch pod that also wants to expose its workload's API while it runs.
    """
    spec = os.environ.get("PODH_API_FROM", "").strip()
    if spec:
        return spec

    path = os.environ.get("PODH_JOB_SPEC", "")
    if path and os.path.isfile(path):
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            return ((doc.get("pipeline") or {}).get("api_from") or "").strip() or None
        except Exception:
            return None
    return None


def mount(app, spec: str | None = None) -> dict:
    """Import the workload's router and attach it under `/v1/x`.

    Returns the state dict rather than raising: the caller is the API's startup path, and
    an extension that cannot load is a degraded pod, not a dead one.
    """
    spec = spec or _spec_from_env()
    STATE["spec"] = spec
    if not spec:
        return STATE

    if ":" not in spec:
        STATE["error"] = (f"api_from must look like 'package.module:ROUTER', got {spec!r}")
        return STATE

    mod_name, attr = spec.split(":", 1)
    try:
        mod = importlib.import_module(mod_name)
        router = getattr(mod, attr)
    except Exception as exc:
        # Loud, and specific about the likely cause: the overwhelmingly common reason is
        # that the code root is not on PYTHONPATH, which is the same failure `serve/code.py`
        # exists to make visible for stages.
        STATE["error"] = (
            f"{type(exc).__name__}: {exc} — could not import {spec!r}. The workload's code "
            f"root must be on PYTHONPATH; check /v1/ 'code' for what was actually resolved.")
        return STATE

    try:
        app.include_router(router, prefix=PREFIX)
    except Exception as exc:
        STATE["error"] = f"{type(exc).__name__}: {exc} — {attr} is not a FastAPI APIRouter"
        return STATE

    STATE["loaded"] = True
    STATE["error"] = None
    # Enumerate from the ROUTER, not from app.routes. Recent FastAPI wraps an included
    # router in an `_IncludedRouter` object that carries no `.path`, so walking app.routes
    # finds nothing and reports an empty extension list for a mount that worked perfectly —
    # the endpoint answers 200 while `/v1/` swears it does not exist. Reading the router's
    # own routes is both correct and independent of how the framework stores them.
    STATE["routes"] = sorted(
        f"{sorted(r.methods)[0]} {PREFIX}{r.path}"
        for r in getattr(router, "routes", [])
        if getattr(r, "methods", None))
    return STATE


def describe() -> dict:
    """What `/v1/` reports about extensions — including the failure, when there is one."""
    d = {"prefix": PREFIX, "api_from": STATE["spec"], "loaded": STATE["loaded"],
         "routes": STATE["routes"]}
    if STATE["error"]:
        d["error"] = STATE["error"]
    if not STATE["spec"]:
        d["note"] = ("no workload API loaded — set PODH_API_FROM or pipeline.api_from in "
                     "the job spec to add endpoints under /v1/x")
    return d
