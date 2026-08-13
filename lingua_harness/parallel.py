"""Parallel map with progress — the pipeline was entirely serial.

Every heavy stage processed one file at a time: ffmpeg spawned per utterance, ECAPA called
with `unsqueeze(0)` (a batch of ONE), librosa pyin run in a plain for-loop. On a 16-core pod
that used one core and left the GPU idle, which is why embedding 2,507 clips took 36 minutes
at 16x realtime when the hardware could do far better.

## Which kind of parallelism

    CPU-bound, separate processes   normalize (ffmpeg), measure (librosa/pyin)
    GPU-bound, one process, BATCHED embed (stack tensors, not more processes)

Those are different problems. Spawning eight processes each loading its own copy of an ECAPA
model would exhaust memory and still under-use the GPU; batching feeds the device wide
tensors instead. So this module handles the first case, and `batched()` exists only to chunk
work for the second.

## Failure policy

A worker that raises must not abort the run — one corrupt file in 12,907 should cost that
file, not the corpus. Exceptions are captured per item and returned alongside results, so the
caller can count them and decide. Silent `except: pass` is what let earlier stages report
success over nothing.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence


def default_workers(reserve: int = 1) -> int:
    """Leave a core for the parent so progress reporting is not starved."""
    n = os.cpu_count() or 2
    return max(1, n - reserve)


@dataclass
class MapResult:
    results: list = field(default_factory=list)
    errors: list = field(default_factory=list)     # (index, item, "TypeName: msg")
    seconds: float = 0.0
    workers: int = 1

    @property
    def ok(self) -> int:
        return len(self.results)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "failed": len(self.errors),
                "seconds": round(self.seconds, 1), "workers": self.workers,
                "first_errors": [e[2] for e in self.errors[:3]]}


def pmap(fn: Callable[[Any], Any], items: Sequence, *, workers: int | None = None,
         label: str = "work", every: int = 100, use_threads: bool = False,
         chunksize: int = 1) -> MapResult:
    """Run `fn` over `items` in parallel, reporting progress and collecting failures.

    `fn` must be a module-level function — process pools pickle it. A closure or lambda
    raises PicklingError, which is why the stages define their workers at module scope.

    `use_threads=True` for I/O-bound work that releases the GIL (subprocess calls like
    ffmpeg), processes otherwise.
    """
    items = list(items)
    if not items:
        return MapResult(workers=0)

    n_workers = workers or default_workers()
    if n_workers <= 1 or len(items) == 1:
        out = MapResult(workers=1)
        t0 = time.time()
        for i, it in enumerate(items):
            try:
                out.results.append(fn(it))
            except Exception as exc:
                out.errors.append((i, it, f"{type(exc).__name__}: {exc}"))
        out.seconds = time.time() - t0
        return out

    Pool = ThreadPoolExecutor if use_threads else ProcessPoolExecutor
    out = MapResult(workers=n_workers)
    t0 = time.time()
    done = 0
    shown = 0
    with Pool(max_workers=n_workers) as ex:
        futures = {ex.submit(fn, it): (i, it) for i, it in enumerate(items)}
        for fut in as_completed(futures):
            i, it = futures[fut]
            try:
                out.results.append(fut.result())
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                out.errors.append((i, it, msg))
                # Report the FIRST few failures immediately. Collecting them silently and
                # printing a summary at the end meant a run that failed 5,833 of 7,394
                # items emitted no error at all until it finished — indistinguishable from
                # a slow run, and impossible to diagnose while it burned pod time.
                if shown < 3:
                    shown += 1
                    import traceback
                    tb = "".join(traceback.format_exception(
                        type(exc), exc, exc.__traceback__))[-600:]
                    print(f"  ✗ FAILURE {shown} of many — {label}: {msg}\n"
                          f"    item: {str(it)[:160]}\n{tb}", flush=True)
            done += 1
            if every and done % every == 0:
                el = time.time() - t0
                rate = done / max(el, 1e-9)
                eta = (len(items) - done) / max(rate, 1e-9) / 60
                nfail = len(out.errors)
                warn = ""
                # A high failure fraction is a defect, not a statistic. Say so in the
                # progress line rather than letting "N failed" scroll past.
                if done >= 250 and nfail / done > 0.25:
                    warn = f"  ⚠ {100*nfail/done:.0f}% FAILING"
                print(f"  [{done}/{len(items)}] {100*done/len(items):>5.1f}% · "
                      f"{rate:.1f}/s · {n_workers}w · ETA {eta:.0f} min · "
                      f"{nfail} failed{warn}", flush=True)
    out.seconds = time.time() - t0
    return out


def ramped_pmap(fn: Callable[[Any], Any], items: Sequence, *,
                ladder: Sequence[int] = (3, 7, 12, 24), label: str = "work",
                fail_threshold: float = 0.05, wave_items: int = 400,
                use_threads: bool = False, every: int = 250) -> MapResult:
    """Ramp concurrency up a ladder, backing off when the failure rate rises.

    ## Why not just start at the cap

    Opening 24 readers against a network volume in the same instant is a thundering herd:
    the filesystem sees a burst it has no chance to admit gradually, and the failures it
    returns look like our bug rather than its backpressure. Starting small and climbing
    only while things stay clean finds the level the storage will actually sustain — which
    is not knowable in advance, because it depends on the pod, the volume and whatever
    else is hitting it.

    ## The rule

        wave clean (failures <= threshold)   -> step UP the ladder
        wave dirty                           -> step DOWN and STAY there

    Staying down after a failure matters. Oscillating back up re-triggers the same
    contention and turns one bad wave into a pattern of them.

    Cost is one barrier per wave — a few seconds against a run measured in minutes, in
    exchange for never repeating the 80%-failure case.
    """
    items = list(items)
    if not items:
        return MapResult(workers=0)

    out = MapResult()
    rung = 0
    start = time.time()
    i = 0
    while i < len(items):
        workers = ladder[rung]
        wave = items[i:i + wave_items]
        res = pmap(fn, wave, workers=workers, label=f"{label}@{workers}w",
                   every=every, use_threads=use_threads)
        out.results.extend(res.results)
        out.errors.extend(res.errors)
        i += len(wave)

        rate = len(res.errors) / max(len(wave), 1)
        if rate > fail_threshold:
            if rung > 0:
                rung -= 1
                print(f"  ↓ {100*rate:.0f}% failed at {workers}w — backing off to "
                      f"{ladder[rung]}w and holding", flush=True)
            else:
                print(f"  ⚠ {100*rate:.0f}% failed even at {workers}w — the failure is "
                      f"not contention; check the first traceback above", flush=True)
        elif rung < len(ladder) - 1:
            rung += 1
            print(f"  ↑ clean at {workers}w — stepping up to {ladder[rung]}w",
                  flush=True)

        done = i
        el = time.time() - start
        print(f"  [{done}/{len(items)}] {100*done/len(items):>5.1f}% · "
              f"{done/max(el,1e-9):.1f}/s · {ladder[rung]}w next · "
              f"{len(out.errors)} failed", flush=True)

    out.seconds = time.time() - start
    out.workers = ladder[rung]
    return out


def batched(items: Sequence, size: int) -> Iterable[list]:
    """Chunk a sequence — for GPU batching, where more processes would not help."""
    buf: list = []
    for it in items:
        buf.append(it)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
