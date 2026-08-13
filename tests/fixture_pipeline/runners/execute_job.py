"""Fixture standing in for the real `runners/execute_job.py` during harness tests.

## What this is for

The real module needs conda MFA, torch, speechbrain and a corpus. None of that is relevant
to the questions the harness test asks — does init order correctly, does Caddy authenticate,
does long-poll return when a stage completes, does cancel actually kill the process tree.
So this fixture mirrors the real module's INTERFACE exactly and does trivial work.

It deliberately reproduces the three shapes serve/jobs.py depends on:

    STAGE_CLASSES          the stage vocabulary, used by /v1/ discovery and validation
    build_runner(names)    raises SystemExit on an unknown stage, as the real one does
    context_from_spec()    turns a spec into a context
    Runner.plan(ctx)       readiness without running anything — what dry_run exposes

If the real module's interface drifts from this, the harness tests keep passing while
production breaks. That is a real risk and the reason this file names the coupling loudly:
these four shapes are the contract between the pipeline and the control surface.

The fixture also emits status events, which the real pipeline does through
`runners/status.py` wired into `Runner.run()`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
from serve.events import EventLog          # noqa: E402


class _FakeStage:
    def __init__(self, name: str, number: int, seconds: float = 0.6,
                 fails: bool = False, produces: tuple = ()):
        self.name, self.number, self.seconds = name, number, seconds
        self.fails, self.produces = fails, produces
        self.requires: tuple = ()


# Mirrors the real STAGE_CLASSES keys so /v1/ discovery reports a realistic vocabulary.
STAGE_CLASSES = {
    "acquire": _FakeStage, "ruleset": _FakeStage, "normalize": _FakeStage,
    "embed": _FakeStage, "cluster": _FakeStage, "align": _FakeStage,
    "markers": _FakeStage, "measure": _FakeStage, "comply": _FakeStage,
    "intersect": _FakeStage, "profile": _FakeStage, "delta": _FakeStage,
    "equidistance": _FakeStage,
    # Test-only stages, so failure and slowness are reachable deterministically.
    "_fail": _FakeStage, "_slow": _FakeStage,
}


class Context:
    def __init__(self, region: str = "_test", **kw):
        self.region = region
        self.artifacts: dict = {}
        self.opts = kw.get("opts", {})


class Runner:
    def __init__(self, name: str, stages: list):
        self.name, self.stages = name, stages

    def plan(self, ctx) -> dict:
        return {"runner": self.name, "wiring_ok": True, "wiring_problems": [],
                "stages": [{"stage": s.name, "number": s.number, "would_run": True,
                            "missing": [], "produces": list(s.produces)}
                           for s in self.stages]}

    def run(self, ctx, *, job_id: str = "", verify: bool = True) -> dict:
        log = EventLog(job_id) if job_id else None
        total = len(self.stages)
        results = []
        for i, s in enumerate(self.stages, start=1):
            print(f"\n{'='*74}\n  STAGE {i} · {s.name.upper()}\n{'='*74}", flush=True)
            if log:
                log.emit(stage=s.name, index=i, total=total, state="running")
            t0 = time.time()
            time.sleep(s.seconds)
            failed = s.name == "_fail"
            elapsed = round(time.time() - t0, 2)
            state = "failed" if failed else "ok"
            print(f"  {'✗' if failed else '✓'} {elapsed}s", flush=True)
            if log:
                log.emit(stage=s.name, index=i, total=total, state=state,
                         seconds=elapsed,
                         error="fixture stage _fail always fails" if failed else None)
            results.append({"stage": s.name, "status": state, "seconds": elapsed})
            if failed:
                return {"runner": self.name, "ok": False, "stages": results}
        return {"runner": self.name, "ok": True, "stages": results}


def build_runner(names: list[str]) -> Runner:
    unknown = [n for n in names if n not in STAGE_CLASSES]
    if unknown:
        raise SystemExit(
            f"unknown stage(s): {unknown}. Available: {sorted(STAGE_CLASSES)}")
    seconds = {"_slow": 8.0}
    return Runner("job", [_FakeStage(n, i + 1, seconds.get(n, 0.6))
                          for i, n in enumerate(names)])


def context_from_spec(spec: dict) -> Context:
    return Context(region=spec.get("region", "_test"), opts=spec.get("opts", {}))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    args = ap.parse_args()
    spec = json.loads(Path(args.spec).read_text())
    job_id = spec.get("job_id") or os.environ.get("LINGUA_JOB_ID", "unknown")

    os.system("/usr/local/bin/lingua-preflight")
    runner = build_runner(spec["stages"])
    result = runner.run(context_from_spec(spec), job_id=job_id)
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
