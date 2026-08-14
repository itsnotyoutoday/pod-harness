"""The generic entry point. Runs a workload's stages; knows nothing about what they do.

    python -m pod_harness.execute_job --spec /workspace/jobs/<job_id>.json

## What moved, and why it matters

The old `runners/execute_job.py` carried a hardcoded `STAGE_CLASSES` dict of the thirteen
linguistics stages. That is what made it a runner for *one* pipeline. Here the registry is
named by the spec:

    "pipeline": {"stages_from": "trainer.stages:STAGES",
                 "stages": ["normalize", "embed", "measure"]}

so the engine imports the workload's registry from whatever the code root provides. The
engine keeps everything valuable — ordering, wiring checks, per-stage verification, resume,
stage-level status — while knowing nothing about audio.

## Why it still refuses unknown stages up front

Inherited from the original, and worth keeping loudly: a typo'd stage that quietly does
nothing is the failure mode this whole framework exists to prevent. Failing before the first
stage runs costs nothing; failing halfway through costs a pod-hour.

## Provider-agnostic by construction

This module never asks where it is running. It is handed a spec and a mount that already
exists, which is why a local run is a real rehearsal for a pod run rather than a different
program that happens to look similar.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import resume as resume_mod
from . import spec as spec_mod
from .framework import Context, NullReporter, Runner


def load_registry(stages_from: str) -> dict[str, Any]:
    """Import `module:attr` and return the stage registry it names.

    Failures here are reported with the code root and `sys.path`, because the overwhelmingly
    common cause is that the code was never synced — and "ModuleNotFoundError: trainer" on
    its own sends people looking in the wrong place entirely.
    """
    module_path, _, attr = stages_from.partition(":")
    try:
        mod = importlib.import_module(module_path)
    except Exception as exc:
        raise SystemExit(
            f"cannot import {module_path!r} named by pipeline.stages_from.\n"
            f"  {type(exc).__name__}: {exc}\n"
            f"  PODH_CODE_ROOT={os.environ.get('PODH_CODE_ROOT', '(unset)')}\n"
            f"  sys.path[:4]={sys.path[:4]}\n"
            f"  hint: the code package is probably not synced to the volume, or "
            f"code.root in the spec points at the wrong prefix") from None

    registry = getattr(mod, attr, None)
    if registry is None:
        raise SystemExit(
            f"{module_path!r} has no attribute {attr!r}. A stage registry is a dict of "
            f"{{name: StageClass}} — see pod-loader-rpc/README.md")
    if not isinstance(registry, dict):
        raise SystemExit(
            f"{stages_from} is {type(registry).__name__}, expected a dict of "
            f"{{name: StageClass}}")
    return registry


def build_runner(spec: dict, registry: dict[str, Any], *, name: str = "job") -> Runner:
    names = spec_mod.stage_names(spec)
    unknown = [n for n in names if n not in registry]
    if unknown:
        raise SystemExit(
            f"unknown stage(s): {unknown}. Available: {sorted(registry)}")
    return Runner(name, [registry[n]() for n in names])


def context_from_spec(spec: dict) -> Context:
    """Build the Context from `params`, which the engine passes through untouched.

    The named Context fields exist because the current stages use them; `params` carries
    the whole blob so a stage can read anything its workload declared without the engine
    needing to know the field exists.
    """
    p = spec.get("params") or {}
    ctx = Context(
        region=p.get("region") or "_default",
        base=p.get("base"),
        limit=p.get("limit"),
        max_minutes=p.get("max_minutes"),
        opts=p.get("opts") or {},
        params=p,
    )
    attach_chunks(spec, ctx)
    return ctx


def attach_chunks(spec: dict, ctx: Context) -> None:
    """Give the Context its chunk keys and the hooks that materialise them.

    Only when the spec asks for it — `mount.chunk_by: "source"`. Chunking answers a disk
    limit (RunPod caps the container disk at 20 GB, against an 11 GB corpus), and a workload
    that fits should not pay for the machinery.

    One chunk per SOURCE, because that is the smallest unit alignment can run over without
    splitting a speaker across two partial alignments that each look whole.

    The engine still learns nothing about mounts: it calls hooks. The hooks are built here,
    at the boundary where the spec is already known, and they degrade to no-ops when the
    mount cannot materialise anything — a local run with the corpus already on disk needs
    no fetching and should not fail for lack of it.
    """
    mount = spec.get("mount") or {}
    if (mount.get("chunk_by") or "") != "source":
        return

    sources = list((spec.get("params") or {}).get("sources") or [])
    keys = [(s.get("id") if isinstance(s, dict) else s) for s in sources]
    keys = [k for k in keys if k]
    if len(keys) < 2:
        return                      # one chunk is just an ordinary run

    by_id = {(s.get("id") if isinstance(s, dict) else s): s for s in sources}

    try:
        from .mount import for_spec
        m = for_spec(spec)
    except Exception:
        m = None

    def enter(key):
        # Narrow params to this chunk BEFORE fetching, so the stages and the fetch agree
        # about what "this chunk" means rather than deriving it twice.
        ctx.params["sources"] = [by_id[key]]
        if m is None:
            return
        try:
            r = m.prepare({**spec, "params": {**(spec.get("params") or {}),
                                              "sources": [by_id[key]]}})
            if not r.get("ready", True):
                print(f"  ! chunk {key}: {r.get('error')}", flush=True)
        except Exception as exc:
            print(f"  ! chunk {key}: {type(exc).__name__}: {exc}", flush=True)

    def leave(key):
        # Publish BEFORE evicting. Reversing these loses the outputs, and once the inputs
        # are gone too there is nothing left to recompute them from.
        if m is None:
            return
        try:
            m.publish(spec, ctx.params.get("out_root") or "")
        except Exception as exc:
            print(f"  ! chunk {key} publish: {type(exc).__name__}: {exc}", flush=True)

    ctx.chunks = tuple(keys)
    ctx.enter_chunk = enter
    ctx.exit_chunk = leave
    print(f"  chunked: {len(keys)} chunk(s), one source at a time — {', '.join(keys)}",
          flush=True)


def run(spec: dict, *, job_id: str = "", reporter: Any = None,
        stage_state: dict | None = None) -> dict:
    """Execute one spec. Returns the runner's result dict."""
    registry = load_registry(spec["pipeline"]["stages_from"])
    runner = build_runner(spec, registry, name=job_id or "job")
    ctx = context_from_spec(spec)

    plan = {"skip": [], "start_at": None}
    rs = spec.get("resume") or {}
    if rs.get("enabled"):
        plan = resume_mod.trim(runner, ctx, mode=rs.get("from", "auto"),
                               completed=stage_state or {})
        if plan["skip"]:
            print(f"  resume: skipping {plan['skip']} (verified complete); "
                  f"starting at {plan['start_at']}", flush=True)
            runner = resume_mod.apply(runner, plan)

    result = runner.run(ctx, reporter=reporter or NullReporter())
    result["resume"] = plan
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--job-id", default=os.environ.get("PODH_JOB_ID", ""))
    ap.add_argument("--plan", action="store_true",
                    help="show what WOULD run and why, then exit. Runs nothing.")
    args = ap.parse_args()

    spec = spec_mod.load(args.spec)
    job_id = args.job_id or spec.get("idempotency_key") or "local"

    if args.plan:
        registry = load_registry(spec["pipeline"]["stages_from"])
        runner = build_runner(spec, registry, name=job_id)
        print(json.dumps(runner.plan(context_from_spec(spec)), indent=2))
        return 0

    # Imported lazily: a local run with no harness present must not require it.
    try:
        from .status import reporter_for
        reporter = reporter_for(job_id)
    except Exception:
        reporter = NullReporter()

    result = run(spec, job_id=job_id, reporter=reporter)
    print(json.dumps(result, indent=2, default=str), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
