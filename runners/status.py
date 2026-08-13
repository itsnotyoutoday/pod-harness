"""Status reporting for the pipeline's Runner — the six-line change that makes a job
observable from outside.

## Where this goes

This file belongs in the PIPELINE repo alongside `framework.py`, and is kept here until
the two merge. It has no third-party dependencies, and importing it is safe even when the
control surface is absent: if `serve.events` cannot be imported (a plain laptop run with
no harness), every method becomes a no-op and the pipeline behaves exactly as it does now.

## The change to framework.py

`Runner.run()` already loops the stages, prints a banner, and collects `StageResult`s.
Reporting hangs off that loop; nothing else moves:

    def run(self, ctx, *, verify=True, stop_on_failure=True, only=None,
            reporter=None):                                        # <- add
        ...
        reporter = reporter or NullReporter()                      # <- add
        reporter.job_start(len(self.stages))                       # <- add
        for s in self.stages:
            if only and s.name != only:
                continue
            print(...)
            reporter.stage_start(s)                                # <- add
            res = s(ctx, verify=verify)
            reporter.stage_done(s, res)                            # <- add
            results.append(res.as_dict())
            ...
        ok = all(...)
        reporter.job_done(ok)                                      # <- add
        return {...}

`execute_job.py` then constructs one when a job id is present:

    from runners.status import reporter_for
    result = runner.run(ctx, reporter=reporter_for(spec.get("job_id")))

## Why the state vocabulary is copied, not simplified

`StageResult.status` distinguishes `ok` from `unverified` from `failed`, and that
distinction is the entire point of the Stage contract: a stage that "reported success and
produced nothing" is `failed`. If this reporter collapsed those into a boolean, the status
endpoint would show green for exactly the bug class framework.py was written to catch —
the three real incidents in its docstring. So the wire format carries the distinction
through untouched, and `summary.failures` surfaces `unverified` alongside `failed`.
"""
from __future__ import annotations

import os
from typing import Any


class NullReporter:
    """Does nothing, cheaply. The default, so a local run needs no harness present."""

    def job_start(self, total: int) -> None: ...
    def stage_start(self, stage: Any) -> None: ...
    def stage_done(self, stage: Any, result: Any) -> None: ...
    def job_done(self, ok: bool) -> None: ...


class EventReporter(NullReporter):
    """Writes the status protocol that `serve/events.py` defines.

    Every method is individually guarded. Reporting is telemetry: a job must never die
    because the status file could not be written, and an exception here would be a
    spectacular own goal — losing a completed pipeline run to a failed progress update.
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.total = 0
        self.index = 0
        self._log = None
        try:
            from serve.events import EventLog     # type: ignore
            self._log = EventLog(job_id)
        except Exception:
            self._log = None

    def _emit(self, **kw) -> None:
        if self._log is None:
            return
        try:
            self._log.emit(**kw)
        except Exception:
            pass

    def job_start(self, total: int) -> None:
        self.total, self.index = total, 0
        self._emit(stage="", index=0, total=total, state="running",
                   job_state="running", event="job_start")

    def stage_start(self, stage: Any) -> None:
        self.index += 1
        self._emit(stage=getattr(stage, "name", "?"), index=self.index,
                   total=self.total, state="running")

    def stage_done(self, stage: Any, result: Any) -> None:
        # `status` carries ok|skipped|failed|unverified straight through — see the module
        # docstring for why collapsing it would defeat the framework's whole purpose.
        self._emit(stage=getattr(stage, "name", "?"), index=self.index,
                   total=self.total,
                   state=getattr(result, "status", "unverified"),
                   seconds=getattr(result, "seconds", None),
                   error=getattr(result, "error", None),
                   verification=getattr(result, "verification", None))

    def job_done(self, ok: bool) -> None:
        self._emit(stage="", index=self.index, total=self.total,
                   state="ok" if ok else "failed",
                   job_state="done" if ok else "failed", event="job_done")


def reporter_for(job_id: str | None = None) -> NullReporter:
    """Pick a reporter. Explicit id wins, then the env var the launcher sets, then none.

    Returning NullReporter rather than None means callers never branch on whether
    reporting is configured — `reporter.stage_start(s)` is always a valid call.
    """
    jid = job_id or os.environ.get("LINGUA_JOB_ID", "")
    if not jid:
        return NullReporter()
    return EventReporter(jid)
