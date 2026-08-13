"""The reporter — turns Runner events into the status protocol.

## What this carries that a coarse reporter would not

    stage + index/total      which stage, how far through the plan
    state                    ok | skipped | failed | unverified — verbatim
    verification             the checks that passed and the ones that did not
    progress.done/total      movement WITHIN a stage
    seconds                  per stage, for the "why was this slow" question

The `unverified` state is the one worth defending. `StageResult.status` distinguishes it
from `ok` because a stage can finish without its outputs being checked, and `framework.py`
exists precisely because three bugs had the shape *"reported success and produced nothing"*.
A reporter that collapsed `unverified` into `ok` would show green for exactly that bug
class. So the vocabulary passes through untouched, and `summary.failures` surfaces
`unverified` alongside `failed`.

## Why every method is individually guarded

Reporting is telemetry. A job must never die because a status file could not be written —
losing a completed pipeline run to a failed progress update would be the most embarrassing
possible outcome for an observability feature. Each method swallows its own exceptions, and
`Context.progress` guards again on the way in.

## Why it degrades to nothing

If the log directory cannot be written — a laptop run with no workspace, a read-only mount —
`_log` stays None and every method becomes a no-op. The same code runs locally and on a pod
without branching on which.
"""
from __future__ import annotations

import os
from typing import Any

from .framework import NullReporter
from .progress import ProgressSink


class EventReporter(NullReporter):
    """Writes the event protocol that `serve/events.py` defines."""

    def __init__(self, job_id: str, *, registry: Any = None):
        self.job_id = job_id
        self.registry = registry          # optional: mirror stage outcomes into the record
        self.total = 0
        self.index = 0
        try:
            from .events import EventLog
            self._log = EventLog(job_id)
        except Exception:
            # Only reachable if the log directory is unwritable. Reporting must never be
            # the reason a job fails, so degrade to a no-op rather than raise.
            self._log = None

    # -- emission -----------------------------------------------------------------------

    def _emit(self, **kw) -> None:
        if self._log is None:
            return
        try:
            self._log.emit(**kw)
        except Exception:
            pass

    # -- Runner hooks -------------------------------------------------------------------

    def job_start(self, total: int) -> None:
        self.total, self.index = total, 0
        self._emit(stage="", index=0, total=total, state="running",
                   job_state="running", event="job_start")

    def stage_start(self, stage: Any) -> None:
        self.index += 1
        self._emit(stage=getattr(stage, "name", "?"), index=self.index,
                   total=self.total, state="running",
                   requires=list(getattr(stage, "requires", ())),
                   produces=list(getattr(stage, "produces", ())))

    def stage_done(self, stage: Any, result: Any) -> None:
        name = getattr(stage, "name", "?")
        state = getattr(result, "status", "unverified")
        verification = getattr(result, "verification", None) or {}
        self._emit(stage=name, index=self.index, total=self.total, state=state,
                   seconds=getattr(result, "seconds", None),
                   error=getattr(result, "error", None),
                   verification=verification,
                   produces=list(getattr(stage, "produces", ())))
        if self.registry is not None:
            try:
                self.registry.record_stage(self.job_id, name, {
                    "state": state,
                    "seconds": getattr(result, "seconds", None),
                    "verification": verification,
                    "error": getattr(result, "error", None)})
            except Exception:
                pass

    def job_done(self, ok: bool, error: str | None = None) -> None:
        self._emit(stage="", index=self.index, total=self.total,
                   state="ok" if ok else "failed",
                   job_state="done" if ok else "failed",
                   event="job_done", error=error)

    # -- sub-stage progress --------------------------------------------------------------

    def progress_for(self, stage: Any):
        """A throttled sink bound to this stage.

        Progress events reuse `state="running"` on the same stage rather than inventing a
        new event type, so a client that only understands stage transitions still sees a
        coherent stream and simply observes the stage repeating — while one that reads
        `detail.progress` gets the movement.
        """
        name = getattr(stage, "name", "?")
        idx, total = self.index, self.total

        def emit(done: int, of: int, note: str) -> None:
            self._emit(stage=name, index=idx, total=total, state="running",
                       progress={"done": done, "total": of, "note": note})

        return ProgressSink(emit)


def reporter_for(job_id: str | None = None, *, registry: Any = None) -> NullReporter:
    """Pick a reporter. Explicit id wins, then the env the launcher sets, then none.

    Returning NullReporter rather than None means callers never branch on whether reporting
    is configured — `reporter.stage_start(s)` is always a valid call.
    """
    jid = job_id or os.environ.get("LINGUA_JOB_ID", "")
    if not jid:
        return NullReporter()
    return EventReporter(jid, registry=registry)
