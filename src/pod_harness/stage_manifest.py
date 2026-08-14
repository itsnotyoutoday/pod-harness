"""Emit `capabilities.json` — a workload's stage vocabulary, without importing the workload.

## What this is for

A control plane deciding whether to spend a pod-hour cannot import the workload's stack. The
trainer's stages pull in MFA, torch and librosa; a laptop validating a job spec has none of
that and should not need it. So CI — which has already checked the code out and can import
it once — introspects the registry and writes the answer down beside the published code.

That file is what makes three things possible off-pod:

    /v1/ discovery      reports the real stage vocabulary rather than a hardcoded list
    shallow dry-run     rejects a typo'd stage name on a laptop, for free, before any spend
    wiring validation   catches a pipeline whose stages cannot satisfy each other's inputs

## The name

`stage_manifest`, not `capabilities`: `pod_loader.capabilities` already means what the
PROVIDER can do (RunPod is not true S3, batch delete takes 307s). This is what a WORKLOAD's
stages are. The independence guard flagged the collision before the ambiguity could cost
anyone an afternoon. The emitted file keeps the name `capabilities.json`, which is what the
control plane and the plan both call it.

## Why it was missing

`publish-code.yml` has called this since it was written and it did not exist — so every workload's publish job failed at that step. lingua-
trainer and lingua-detect both. The reusable workflow documented a contract that nothing
implemented, which is the same shape as the other defects this system has had: a step that
looks wired, reports nothing, and is believed.

## Usage

    python -m pod_harness.stage_manifest trainer.stages:STAGES -o capabilities.json --rev SHA
"""
from __future__ import annotations

import argparse
import datetime
import importlib
import json
import sys


def load_registry(spec: str) -> dict:
    """Import `module:ATTR` and return the stage registry it names."""
    if ":" not in spec:
        raise SystemExit(
            f"stages_from must look like 'package.module:ATTR', got {spec!r}")
    mod_name, attr = spec.split(":", 1)
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:
        raise SystemExit(
            f"could not import {mod_name!r}: {type(exc).__name__}: {exc}\n"
            f"  The registry module is imported here, so everything it imports at module "
            f"level must be installable in CI. Keep heavy dependencies inside execute().")
    try:
        reg = getattr(mod, attr)
    except AttributeError:
        raise SystemExit(f"{mod_name!r} has no attribute {attr!r}")
    if not isinstance(reg, dict):
        raise SystemExit(f"{spec} is {type(reg).__name__}, expected a dict of name -> Stage")
    return reg


def describe(reg: dict) -> list[dict]:
    """One record per stage: what it is called, what it needs, what it makes."""
    out = []
    for name, cls in sorted(reg.items(), key=lambda kv: getattr(kv[1], "number", 0)):
        out.append({
            "name": getattr(cls, "name", name),
            "key": name,
            "number": getattr(cls, "number", None),
            "requires": list(getattr(cls, "requires", ()) or ()),
            "produces": list(getattr(cls, "produces", ()) or ()),
            "optional": bool(getattr(cls, "optional", False)),
            # "chunk" means the runner materialises one unit of work at a time and runs this
            # stage once per unit; "job" means once over everything that accumulated. A
            # caller that does not know this cannot predict how many stage events to expect.
            "scope": getattr(cls, "scope", "job"),
            "verifies": callable(getattr(cls, "verify_outputs", None)),
        })
    return out


def wiring(stages: list[dict]) -> dict:
    """Which declared inputs no earlier stage produces.

    Reported rather than raised: a workload may legitimately receive an artifact from the
    mount rather than from a stage. Unsatisfied inputs are the single most useful thing a
    shallow dry-run can surface, so they are named here and judged by the caller.
    """
    produced: set[str] = set()
    unsatisfied: dict[str, list[str]] = {}
    for s in stages:
        missing = [r for r in s["requires"] if r not in produced]
        if missing:
            unsatisfied[s["key"]] = missing
        produced.update(s["produces"])
    return {"produced": sorted(produced), "unsatisfied_inputs": unsatisfied}


def build(spec: str, *, rev: str | None = None) -> dict:
    reg = load_registry(spec)
    stages = describe(reg)
    return {
        "stages_from": spec,
        "stages": stages,
        "wiring": wiring(stages),
        "rev": rev,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .replace(microsecond=0).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stages_from", help="module:ATTR of the stage registry")
    ap.add_argument("-o", "--out", default="capabilities.json")
    ap.add_argument("--rev", default=None, help="commit that produced this code")
    a = ap.parse_args(argv)

    doc = build(a.stages_from, rev=a.rev)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    n = len(doc["stages"])
    bad = doc["wiring"]["unsatisfied_inputs"]
    print(f"capabilities: {n} stage(s) from {a.stages_from} -> {a.out}")
    for s in doc["stages"]:
        print(f"  {s['number']:>2}. {s['name']:<14} scope={s['scope']:<6} "
              f"requires={s['requires']} produces={s['produces']}"
              f"{' [optional]' if s['optional'] else ''}")
    if bad:
        print(f"  note: inputs no earlier stage produces (may come from the mount): {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
