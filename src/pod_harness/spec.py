"""The job spec — what the engine reads, and what it deliberately does not.

## The line, and why it is drawn here

An earlier draft of this design made the engine stage-blind: run the job as an opaque
subprocess, push everything into a `params` blob. That decouples, and it destroys the most
valuable thing in the system. `framework.py` exists because three separate bugs had the
same shape — *a stage reported success and produced nothing*. An engine that cannot see
stages reports `running` then `done`, with no idea which stage, no verification result, no
free dry-run and no resume.

So the split is not "framework knows nothing about pipelines". It is:

    framework fields    the engine READS and ACTS on
      pipeline.stages     validate, order, report per-stage, resume
      pipeline.stages_from  where to import the implementations from
      code, mount, runner, resume

    params              opaque to the ENGINE, meaningful to the STAGES
      region, sources, opts — the engine passes them through untouched

`params` is opaque to the transport, not invisible to the system. Stages read it; nothing
in `pod_harness` interprets it.

## Versioning

v1 specs are the ones already sitting in `jobs/*.json`: `stages`, `region`, `sources` and
`opts` at the top level. `normalize()` lifts them into v2 shape so an existing spec keeps
running during the migration rather than needing a rewrite on day one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CURRENT_VERSION = 2

# What a v1 spec assumes, since v1 predates the idea of a pluggable registry.
#
# Deployment-configurable on purpose: a pod carries exactly one workload, and it is the one
# thing that knows which registry its own code publishes. Hardcoding a workload name here
# would make the v1 shim work for exactly one repo and fail confusingly for every other —
# the engine would be generic in principle and trainer-specific in practice.
DEFAULT_STAGES_FROM = os.environ.get(
    "LINGUA_DEFAULT_STAGES_FROM", "trainer.stages:STAGES")


class SpecError(ValueError):
    """Invalid spec. Message names the offending field and what was expected, because the
    caller is usually a human or an agent about to spend money."""


def normalize(raw: dict, *, default_stages_from: str | None = None) -> dict:
    """Return a v2 spec. Accepts v1 and lifts it.

    Deliberately non-destructive: unknown top-level keys are preserved under `params` rather
    than dropped, so a spec written against a newer workload does not silently lose fields
    when an older engine reads it.
    """
    default_stages_from = default_stages_from or os.environ.get(
        "LINGUA_DEFAULT_STAGES_FROM", DEFAULT_STAGES_FROM)

    if raw.get("spec_version") == CURRENT_VERSION:
        return _validated(dict(raw))

    known_v1 = {"job_id", "region", "stages", "sources", "opts", "mount", "base", "limit",
                "max_minutes"}
    params = {k: v for k, v in raw.items() if k in known_v1 - {"job_id", "stages", "mount"}}
    params.update({k: v for k, v in raw.items() if k not in known_v1 and k not in
                   ("spec_version", "code", "pipeline", "runner", "resume", "exec",
                    "idempotency_key")})

    spec: dict[str, Any] = {
        "spec_version": CURRENT_VERSION,
        "code": raw.get("code") or {},
        "mount": raw.get("mount") or {"kind": "local", "root": "corpus/"},
        "runner": raw.get("runner") or {},
        "resume": raw.get("resume") or {"enabled": False, "from": "auto"},
        "params": params,
    }
    if raw.get("idempotency_key"):
        spec["idempotency_key"] = raw["idempotency_key"]
    # A v1 job_id becomes the idempotency key, not the job id: ids are server-minted now,
    # but re-submitting an old spec should still not start a second pod-hour.
    elif raw.get("job_id"):
        spec["idempotency_key"] = str(raw["job_id"])

    if raw.get("exec"):
        spec["exec"] = raw["exec"]
    else:
        spec["pipeline"] = {"stages_from": default_stages_from,
                            "stages": list(raw.get("stages") or [])}
    return _validated(spec)


def _validated(spec: dict) -> dict:
    has_pipeline, has_exec = "pipeline" in spec, "exec" in spec
    if has_pipeline == has_exec:
        raise SpecError(
            "spec must have exactly one of 'pipeline' (stage-based, the normal case) or "
            "'exec' (raw command, the escape hatch); "
            f"got {'both' if has_pipeline else 'neither'}")

    if has_pipeline:
        p = spec["pipeline"]
        if not p.get("stages"):
            raise SpecError("pipeline.stages is empty — nothing to do")
        if not isinstance(p["stages"], list):
            raise SpecError("pipeline.stages must be a list of stage names")
        if not p.get("stages_from"):
            raise SpecError(
                "pipeline.stages_from is required: 'module.path:REGISTRY_NAME', e.g. "
                "'trainer.stages:STAGES'")
        if ":" not in p["stages_from"]:
            raise SpecError(
                f"pipeline.stages_from must be 'module:attr', got {p['stages_from']!r}")
    else:
        if not spec["exec"].get("module"):
            raise SpecError("exec.module is required")

    mk = (spec.get("mount") or {}).get("kind", "local")
    if mk not in ("local", "volume", "object", "s3", "fuse"):
        raise SpecError(f"mount.kind {mk!r} unknown; want local|volume|object|fuse")

    spec.setdefault("params", {})
    spec.setdefault("resume", {"enabled": False, "from": "auto"})
    return spec


def load(path: str | Path, **kw) -> dict:
    return normalize(json.loads(Path(path).read_text(encoding="utf-8")), **kw)


def stage_names(spec: dict) -> list[str]:
    """The stages this spec declares, or [] for an exec-style job."""
    return list((spec.get("pipeline") or {}).get("stages") or [])


def validate_against_capabilities(spec: dict, caps: dict) -> list[str]:
    """Check stage names and wiring against a published `capabilities.json`.

    This is the SHALLOW dry-run: it needs no code present, so the control plane can reject a
    typo'd stage name or an unwired pipeline before spending a pod. The DEEP check —
    `Runner.plan()` against real artifacts on the mount — can only happen in the pod.
    """
    problems: list[str] = []
    declared = {s["name"]: s for s in caps.get("stages", [])}
    if not declared:
        return ["capabilities.json declares no stages"]

    requested = stage_names(spec)
    unknown = [s for s in requested if s not in declared]
    if unknown:
        problems.append(f"unknown stage(s): {unknown}. Available: {sorted(declared)}")
        return problems          # wiring check is meaningless with unknown names

    # A stage that is NOT in this run is assumed to have already happened — its outputs are
    # on the mount from a previous run. That mirrors what the engine actually does: it seeds
    # `sources` when `acquire` is absent and `normalized` when `normalize` is absent,
    # precisely so a job can re-do embedding without re-normalising 7 minutes of audio.
    #
    # Without this rule the shallow check is STRICTER than the engine and rejects legitimate
    # jobs — verified against real specs: `neutro_speakers` (embed,cluster) and
    # `align_phones` (align) both run against already-normalised audio.
    #
    # What it can still catch: a stage requiring an artifact that NO stage anywhere
    # produces — a typo, or a pipeline wired to something that does not exist. What it
    # cannot catch is "declared present but actually absent on this mount"; only the deep
    # check, with the volume in front of it, can answer that.
    produced_by_absent = {a for n, s in declared.items() if n not in requested
                          for a in s.get("produces", [])}
    available: set[str] = set(caps.get("given") or []) | produced_by_absent

    for name in requested:
        s = declared[name]
        unmet = [r for r in s.get("requires", []) if r not in available]
        if unmet:
            everything = {a for d in declared.values() for a in d.get("produces", [])}
            orphan = [r for r in unmet if r not in everything]
            if orphan:
                problems.append(
                    f"{name!r} requires {orphan}, which NO stage in this workload "
                    f"produces — a typo, or a pipeline wired to something that does not "
                    f"exist")
            else:
                problems.append(
                    f"{name!r} requires {unmet}, produced by a stage that runs LATER in "
                    f"this list — reorder the stages")
        available |= set(s.get("produces", []))
    return problems
