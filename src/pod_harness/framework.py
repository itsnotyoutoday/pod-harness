"""Runner framework — stages as objects with contracts you can verify.

## Why this exists

Three separate bugs in this pipeline had the same shape: **a stage reported success and
produced nothing.**

    ingest globbed *.wav on a FLAC corpus  -> "40 files" that were zero files
    embed trusted a stale empty manifest   -> "done" over an empty list
    checkpoint survived a wiped volume     -> "done: 24" with no outputs

A procedural `run()` cannot catch that, because success is whatever the function did not
crash doing. A Stage declares what it REQUIRES and what it PRODUCES, and the runner checks
both — before, so a stage does not start without its inputs, and after, so it cannot claim
an output it did not create.

## The three questions a stage must answer

    check(ctx)   can I run? are my inputs present?      -> Readiness
    run(ctx)     do the work                            -> StageResult
    verify(ctx)  did I actually produce what I claimed? -> Verification

`verify` is the one that matters. It is not a test of the pipeline; it is part of it.

## Modes

    plan()    show order, dependencies and readiness. Runs NOTHING.
    verify()  check every contract against what is on disk. Runs NOTHING.
    run()     execute, verifying each stage's output before moving on.

plan() and verify() are why the runner can be trusted without spending an hour or a pod.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Sequence


# --------------------------------------------------------------------------------------
# Context: what flows between stages
# --------------------------------------------------------------------------------------

@dataclass
class Context:
    """Shared state. Stages read and write here rather than through globals.

    `params` carries whatever the workload's spec declared — region, sources, opts. The
    engine never interprets it; only the stages do. The named fields below are kept because
    existing stages use them, and `params` is the general channel for anything new.
    """

    region: str
    base: str | None = None
    job: Any = None
    store: Any = None
    limit: int | None = None
    max_minutes: float | None = None
    opts: dict = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    #: The chunk contract, supplied by the HARNESS, never invented by the engine.
    #:
    #: `chunks` names the units of work — for this pipeline, one per corpus source, because
    #: a source is the smallest thing alignment can be run over without splitting a speaker.
    #: `enter_chunk` materialises one and narrows params to it; `exit_chunk` publishes its
    #: outputs and evicts it.
    #:
    #: Left empty, the runner behaves exactly as before. That is deliberate: chunking is an
    #: answer to a disk limit, and a workload that fits should not pay for the machinery.
    chunks: tuple = ()
    enter_chunk: Any = None
    exit_chunk: Any = None

    # Set by the runner. A stage calls ctx.progress(done, total) while it works; the sink
    # throttles and forwards to the status protocol. Default is a no-op so a stage can call
    # it unconditionally and a bare Context (tests, a local run) still works.
    progress_sink: Any = None

    def progress(self, done: int, total: int = 0, note: str = "") -> None:
        """Report progress WITHIN a stage.

        Stage-level events tell you a stage started; they cannot distinguish "grinding
        through 2,507 files" from "hung". This closes that gap, and it is the only signal
        that does — which is why the sink is on the Context rather than something a stage
        has to be handed separately.
        """
        if self.progress_sink is not None:
            try:
                self.progress_sink(done, total, note)
            except Exception:
                pass        # telemetry must never break the work it is reporting on

    def put(self, key: str, value: Any) -> None:
        self.artifacts[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.artifacts.get(key, default)

    def has(self, key: str) -> bool:
        v = self.artifacts.get(key)
        if v is None:
            return False
        if isinstance(v, (list, dict, tuple, set, str)):
            return len(v) > 0          # an empty list is NOT a satisfied requirement
        return True


@dataclass
class Readiness:
    ready: bool
    missing: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verification:
    ok: bool
    checks: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class StageResult:
    stage: str
    status: str                     # ok | skipped | failed | unverified
    seconds: float = 0.0
    detail: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------------------

class Stage:
    """One step, with declared inputs and outputs.

    ## scope

    `"job"` (the default) means the stage runs once, over everything.

    `"chunk"` means it runs once PER CHUNK, and the runner materialises one chunk at a time
    around it. That is what lets an 11 GB corpus be processed on a 20 GB container disk: the
    data-heavy stages see one source at a time, while the stages that follow — which work on
    measurements, not audio — run once over the accumulated results.

    The split is a property of the STAGE, not of the pipeline, because only the stage knows
    whether its work is per-source or global. Declaring it here means a workload gets
    chunking by adding one attribute, and a runner with no chunk-scoped stages behaves
    exactly as it always did.

    Subclasses override `execute` and ideally `verify_outputs`. The default verification
    only checks that everything in `produces` landed in the context — better than nothing,
    but a stage that can check its artifacts on disk should.
    """

    name: str = "stage"
    number: int = 0                    # the six-stage map: 1 acquire … 6 detect
    #: "job" — once, over everything. "chunk" — once per chunk, one chunk resident at a time.
    scope: str = "job"
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    optional: bool = False          # if True, a failure does not stop the runner

    def check(self, ctx: Context) -> Readiness:
        missing = [r for r in self.requires if not ctx.has(r)]
        return Readiness(
            ready=not missing, missing=missing,
            reason="" if not missing else f"missing inputs: {', '.join(missing)}")

    def execute(self, ctx: Context) -> dict:
        raise NotImplementedError

    def verify_outputs(self, ctx: Context) -> Verification:
        """Default: everything declared in `produces` must be present AND non-empty."""
        checks, failures = {}, []
        for key in self.produces:
            present = ctx.has(key)
            checks[key] = present
            if not present:
                failures.append(
                    f"declared output {key!r} is missing or empty — the stage reported "
                    f"success without producing it")
        return Verification(ok=not failures, checks=checks, failures=failures)

    # -- orchestration hook -------------------------------------------------------------

    def __call__(self, ctx: Context, *, verify: bool = True) -> StageResult:
        ready = self.check(ctx)
        if not ready.ready:
            return StageResult(self.name, "skipped", detail=ready.as_dict(),
                               error=ready.reason)
        t0 = time.time()
        try:
            detail = self.execute(ctx) or {}
        except Exception as exc:
            return StageResult(self.name, "failed", round(time.time() - t0, 2),
                               error=f"{type(exc).__name__}: {exc}",
                               detail={"traceback": traceback.format_exc()[-900:]})
        elapsed = round(time.time() - t0, 2)

        if not verify:
            return StageResult(self.name, "unverified", elapsed, detail)

        v = self.verify_outputs(ctx)
        return StageResult(self.name, "ok" if v.ok else "failed", elapsed,
                           detail, v.as_dict(),
                           None if v.ok else "; ".join(v.failures))


class FnStage(Stage):
    """Wrap a plain function as a Stage — for quick composition and for tests."""

    def __init__(self, name: str, fn: Callable[[Context], dict], *,
                 number: int = 0, requires: Sequence[str] = (),
                 produces: Sequence[str] = (), optional: bool = False,
                 verifier: Callable[[Context], Verification] | None = None):
        self.name, self.fn = name, fn
        self.number = number
        self.requires, self.produces = tuple(requires), tuple(produces)
        self.optional = optional
        self._verifier = verifier

    def execute(self, ctx: Context) -> dict:
        return self.fn(ctx)

    def verify_outputs(self, ctx: Context) -> Verification:
        if self._verifier:
            return self._verifier(ctx)
        return super().verify_outputs(ctx)


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

class NullReporter:
    """Does nothing, cheaply — the default.

    Declared HERE rather than imported from the status module on purpose: the stage model
    must not depend on the transport that reports it. `pod_harness.status.EventReporter`
    subclasses this and writes the event protocol; a local run with no harness gets the
    no-op and behaves identically.
    """

    def job_start(self, total: int) -> None: ...
    def stage_start(self, stage: Any) -> None: ...
    def stage_done(self, stage: Any, result: Any) -> None: ...
    def job_done(self, ok: bool, error: str | None = None) -> None: ...

    def progress_for(self, stage: Any):
        """Return a sink for sub-stage progress, or None to disable it."""
        return None


# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------

class Runner:
    """Composes stages, checks the wiring, executes with verification."""

    def __init__(self, name: str, stages: Sequence[Stage]):
        self.name = name
        self.stages = list(stages)

    # -- static checks: no work done -----------------------------------------------------

    def wiring(self, given: Sequence[str] = ()) -> dict:
        """Does every requirement get produced by an earlier stage, or supplied up front?

        Catches a broken pipeline definition without running anything — the cheapest
        possible verification, and the one that would have flagged a stage reading an
        artifact nobody writes.

        `given` names artifacts the caller has already placed in the context. A job spec
        that points at a corpus already on the mount supplies `sources` without running
        `acquire`, and that is a legitimate pipeline, not a broken one. Without this the
        check rejects the most common real configuration.
        """
        available: set[str] = set(given)
        problems, order = [], []
        for s in self.stages:
            unmet = [r for r in s.requires if r not in available]
            order.append({"stage": s.name, "number": s.number,
                          "requires": list(s.requires), "produces": list(s.produces),
                          "unmet_at_this_point": unmet, "optional": s.optional})
            for r in unmet:
                problems.append(
                    f"{s.name!r} requires {r!r}, which no earlier stage produces")
            available |= set(s.produces)
        dupes: dict[str, list[str]] = {}
        for s in self.stages:
            for p in s.produces:
                dupes.setdefault(p, []).append(s.name)
        collisions = {k: v for k, v in dupes.items() if len(v) > 1}
        for k, v in collisions.items():
            problems.append(f"artifact {k!r} is produced by more than one stage: {v}")
        return {"runner": self.name, "ok": not problems, "order": order,
                "problems": problems, "given": sorted(given),
                "final_artifacts": sorted(available)}

    def plan(self, ctx: Context) -> dict:
        """Readiness of each stage given the CURRENT context. Runs nothing."""
        sim: set[str] = set(k for k in ctx.artifacts if ctx.has(k))
        wiring = self.wiring(given=sorted(sim))
        rows = []
        for s in self.stages:
            missing = [r for r in s.requires if r not in sim]
            rows.append({"stage": s.name, "number": s.number,
                         "would_run": not missing,
                         "missing": missing, "produces": list(s.produces)})
            sim |= set(s.produces)
        return {"runner": self.name, "wiring_ok": wiring["ok"],
                "wiring_problems": wiring["problems"], "stages": rows}

    # -- execution ----------------------------------------------------------------------

    def _run_chunked(self, ctx, planned, chunked, keys, *, verify, stop_on_failure,
                     reporter, started) -> dict:
        """Run the chunk-scoped stages once per chunk, then the rest once.

        Sequential across chunks on purpose: two resident at once defeats the entire point
        of bounding the disk. A chunk is entered, worked, published and evicted before the
        next is fetched — the order that matters, because evicting before publishing loses
        the outputs and there is nothing left to recompute them from.
        """
        rest = [s for s in planned if getattr(s, "scope", "job") != "chunk"]
        reporter.job_start(len(chunked) * len(keys) + len(rest))
        results = []

        for i, key in enumerate(keys, 1):
            print(f"\n{'#'*74}\n  CHUNK {i}/{len(keys)} · {key}\n{'#'*74}", flush=True)
            enter = getattr(ctx, "enter_chunk", None)
            if callable(enter):
                enter(key)
            try:
                for s in chunked:
                    res = self._one(s, ctx, verify=verify, reporter=reporter)
                    res_d = res.as_dict()
                    res_d["chunk"] = key
                    results.append(res_d)
                    if res.status not in ("ok", "skipped") and stop_on_failure \
                            and not s.optional:
                        break
            finally:
                # Publish-then-evict happens in exit_chunk, and it runs even when a stage
                # raised: a chunk's completed work must not be discarded because a later
                # stage in the same chunk failed.
                leave = getattr(ctx, "exit_chunk", None)
                if callable(leave):
                    leave(key)

        for s in rest:
            res = self._one(s, ctx, verify=verify, reporter=reporter)
            results.append(res.as_dict())
            if res.status not in ("ok", "skipped") and stop_on_failure and not s.optional:
                break

        ok = all(r["status"] in ("ok", "skipped") for r in results)
        reporter.job_done(ok)
        return {"runner": self.name, "ok": ok, "chunks": len(keys),
                "elapsed_minutes": round((time.time() - started) / 60, 2),
                "stages": results,
                "artifacts": sorted(k for k in ctx.artifacts if ctx.has(k))}

    def _one(self, s, ctx, *, verify, reporter):
        """Run one stage with the banner, progress routing and reporting the loop expects."""
        print(f"\n{'='*74}\n  STAGE {s.number} · {s.name.upper()}\n{'='*74}", flush=True)
        ctx.progress_sink = reporter.progress_for(s)
        reporter.stage_start(s)
        res = s(ctx, verify=verify)
        ctx.progress_sink = None
        reporter.stage_done(s, res)
        if res.status == "ok":
            extra = " ".join(f"{k}={v}" for k, v in list(res.detail.items())[:4]
                             if not isinstance(v, (dict, list)))
            print(f"  ✓ {res.seconds}s  {extra}", flush=True)
        elif res.status == "skipped":
            print(f"  – skipped: {res.error}", flush=True)
        else:
            print(f"  ✗ {res.status}: {res.error}", flush=True)
            for f in (res.verification.get("failures") or []):
                print(f"      {f}", flush=True)
        return res

    def run(self, ctx: Context, *, verify: bool = True, stop_on_failure: bool = True,
            only: str | None = None, reporter: Any = None) -> dict:
        """Execute, verifying each stage's output before moving on.

        `reporter` receives the same events the printed banner conveys, but machine-readable
        and out-of-process — which is what makes a running job observable from outside the
        pod. It is optional and defaults to a no-op, so a local run needs no harness present.
        """
        reporter = reporter or NullReporter()
        wiring = self.wiring(given=sorted(k for k in ctx.artifacts if ctx.has(k)))
        if not wiring["ok"]:
            reporter.job_done(False, error="wiring invalid")
            return {"runner": self.name, "ok": False,
                    "error": "runner wiring is invalid — fix before executing",
                    "problems": wiring["problems"]}

        planned = [s for s in self.stages if not only or s.name == only]

        # Split into the chunk-scoped prefix and the job-scoped remainder.
        #
        # An 11 GB corpus does not fit beside its outputs on a 20 GB container disk, and
        # RunPod caps that disk at 20. But the pipeline is already map/reduce in shape: the
        # data-heavy stages are per-source, and everything after them works on measurements
        # — about 8 MB — rather than audio. So the heavy stages run once per chunk with one
        # chunk resident at a time, and the rest run once over what accumulated.
        #
        # ctx supplies the chunk keys and the enter/exit hooks. The engine never learns what
        # a chunk IS or how it is fetched; that stays with the harness, which owns the mount.
        chunked = [s for s in planned if getattr(s, "scope", "job") == "chunk"]
        keys = list(getattr(ctx, "chunks", ()) or ())
        if chunked and len(keys) > 1:
            return self._run_chunked(ctx, planned, chunked, keys, verify=verify,
                                     stop_on_failure=stop_on_failure, reporter=reporter,
                                     started=time.time())

        reporter.job_start(len(planned))

        results, started = [], time.time()
        for s in planned:
            print(f"\n{'='*74}\n  STAGE {s.number} · {s.name.upper()}\n{'='*74}",
                  flush=True)
            # Route this stage's sub-stage progress to the reporter, then take it away
            # again so a later stage cannot emit progress attributed to it.
            ctx.progress_sink = reporter.progress_for(s)
            reporter.stage_start(s)
            res = s(ctx, verify=verify)
            ctx.progress_sink = None
            reporter.stage_done(s, res)
            results.append(res.as_dict())

            if res.status == "ok":
                extra = " ".join(f"{k}={v}" for k, v in list(res.detail.items())[:4]
                                 if not isinstance(v, (dict, list)))
                print(f"  ✓ {res.seconds}s  {extra}", flush=True)
            elif res.status == "skipped":
                print(f"  – skipped: {res.error}", flush=True)
            else:
                print(f"  ✗ {res.status}: {res.error}", flush=True)
                if res.verification.get("failures"):
                    for f in res.verification["failures"]:
                        print(f"      {f}", flush=True)
                if stop_on_failure and not s.optional:
                    break

        ok = all(r["status"] in ("ok", "skipped") for r in results)
        reporter.job_done(ok)
        return {"runner": self.name, "ok": ok,
                "elapsed_minutes": round((time.time() - started) / 60, 2),
                "stages": results,
                "artifacts": sorted(k for k in ctx.artifacts if ctx.has(k))}


# --------------------------------------------------------------------------------------
# Self-test — proves the framework catches what it claims to
# --------------------------------------------------------------------------------------

def selftest() -> dict:
    """Verify the framework itself, with no corpus and no network.

    Exercises exactly the failure modes that bit this project: a stage that returns
    success while producing nothing, and a pipeline wired to an artifact nobody creates.
    """
    cases = {}

    good = Runner("good", [
        FnStage("a", lambda c: (c.put("x", [1, 2]), {"n": 2})[1], number=1, produces=["x"]),
        FnStage("b", lambda c: (c.put("y", {"k": 1}), {"n": 1})[1],
                number=2, requires=["x"], produces=["y"]),
    ])
    cases["wiring_valid"] = good.wiring()["ok"] is True

    broken = Runner("broken", [
        FnStage("a", lambda c: {}, number=1, produces=["x"]),
        FnStage("b", lambda c: {}, number=2, requires=["nobody_makes_this"]),
    ])
    cases["detects_unmet_requirement"] = broken.wiring()["ok"] is False

    # THE bug class: reports success, produces nothing
    liar = Runner("liar", [
        FnStage("liar", lambda c: {"claimed": 40}, number=1, produces=["files"]),
    ])
    r = liar.run(Context(region="_t"), verify=True)
    cases["catches_success_without_output"] = r["ok"] is False

    # empty list must not satisfy a requirement
    ctx = Context(region="_t")
    ctx.put("empty", [])
    cases["empty_list_is_not_satisfied"] = ctx.has("empty") is False

    cases["good_runner_passes"] = good.run(Context(region="_t"))["ok"] is True

    return {"passed": all(cases.values()), "cases": cases}
