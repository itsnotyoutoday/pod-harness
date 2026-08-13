"""Sub-stage progress — the difference between "working" and "hung".

## Why this exists

Stage-level events tell you a stage started and, eventually, that it finished. Between
those two points a stage processing 2,507 files is indistinguishable from a stage that
wedged on the first one. `batch_pod.py` already names this problem for logs — *"a
12-minute job with no visible progress is indistinguishable from a hung one"* — and the
same is true of the status protocol.

So a stage calls `ctx.progress(done, total)` and this module decides what actually reaches
the event log.

## Why it throttles

A stage looping over 2,507 files would otherwise emit 2,507 events, each one an fsync'd
append plus a status rewrite plus an S3 mirror. That is slower than the work being
reported, and it buries the stage transitions an operator actually reads.

Throttling is by BOTH time and completion so neither pathology wins: at most one event per
`min_interval` seconds, plus a guaranteed emit at each `step_pct` of the way through and at
100%. A ten-second stage reports a couple of times; a two-hour stage reports steadily
without flooding.

## Why failures are swallowed

Telemetry must never break the work it reports on. A progress sink that raises would turn a
successful pipeline into a failed one — the most embarrassing possible failure mode for an
observability feature. `Context.progress` also guards, so this is belt and braces.
"""
from __future__ import annotations

import time
from typing import Any, Callable


class ProgressSink:
    """Throttled forwarder from a stage to the event log.

    One per stage: the runner installs it on the Context at stage start and removes it at
    stage end, so progress can never be attributed to the wrong stage.
    """

    def __init__(self, emit: Callable[[int, int, str], None], *,
                 min_interval: float = 1.0, step_pct: int = 10):
        self._emit = emit
        self.min_interval = min_interval
        self.step_pct = max(1, step_pct)
        self._last_at = 0.0
        self._last_pct = -1
        self._last_done = -1

    def __call__(self, done: int, total: int = 0, note: str = "") -> None:
        now = time.monotonic()
        pct = int(done * 100 / total) if total else -1

        due = False
        if now - self._last_at >= self.min_interval:
            due = True
        if pct >= 0 and pct // self.step_pct != self._last_pct // self.step_pct:
            due = True
        if total and done >= total and self._last_done != done:
            due = True          # always report completion, however briefly it lasted
        if not due:
            return

        self._last_at, self._last_pct, self._last_done = now, pct, done
        try:
            self._emit(done, total, note)
        except Exception:
            pass                # see module docstring


def null_sink(done: int, total: int = 0, note: str = "") -> None:
    """Accepts and discards. Used when no reporter is attached, so a stage can call
    ctx.progress() unconditionally rather than testing for a sink first."""
