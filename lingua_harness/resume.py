"""Resume a job from where it actually failed — verification-driven, never marker-driven.

## The bug this is designed against

`runners/batch_pod.py` records what happened when resume trusted a marker file:

    checkpoint survived a wiped volume -> "done: 24" with no outputs

and the restart guard it added had the mirror-image failure:

    "a failed run left a marker that the restart guard then honoured, so the next pod
     refused to run the fixed code and reported the OLD failure"

Both come from the same mistake: treating "we recorded that this finished" as evidence that
the outputs exist. A marker is a claim; it is not the thing itself. Storage gets wiped,
volumes get recreated, a stage gets fixed and needs re-running.

So resume here asks the only question that cannot lie: **does `verify_outputs()` still pass
against what is on disk right now?**

## How it works

Walk the planned stages in order, re-running each completed stage's verification against
the live context. Skip while they pass; the first one that fails is where execution
restarts, and everything after it runs regardless of what the record claims.

    normalize  verify ✓  skip
    embed      verify ✓  skip
    align      verify ✗  ← restart here
    measure              run  (everything downstream runs, verified or not)

This is why `Stage.verify_outputs` earns its keep twice: once to catch a stage that lied
about succeeding, and once to decide whether its work still exists.

## Why "everything downstream runs regardless"

A stage whose input was regenerated cannot trust its own previous output, even if that
output still verifies — it may have been produced from different bytes. Verification proves
existence and shape, not provenance. Re-running downstream is the conservative choice, and
the expensive alternative (fingerprinting inputs) is not worth it until a pipeline is long
enough for it to matter.
"""
from __future__ import annotations

from typing import Any


def trim(runner: Any, ctx: Any, *, mode: str = "auto",
         completed: dict | None = None) -> dict:
    """Decide which stages to skip. Returns a report; does not mutate the runner.

    mode:
      auto        skip leading stages whose verify_outputs() still passes
      never       run everything from the top
      <stage>     explicit restart point, by name — verification is not consulted

    `completed` is the job record's `stage_state`, used only as a hint about which stages
    to bother verifying. It is never trusted on its own — see the module docstring.
    """
    stages = list(runner.stages)
    if mode == "never":
        return {"mode": mode, "skip": [], "start_at": stages[0].name if stages else None,
                "checked": []}

    if mode not in ("auto", ""):
        names = [s.name for s in stages]
        if mode not in names:
            raise ValueError(f"resume.from={mode!r} is not a stage in this run: {names}")
        i = names.index(mode)
        return {"mode": "explicit", "skip": names[:i], "start_at": mode, "checked": []}

    skip, checked = [], []
    for s in stages:
        # Only consider stages the record claims finished. An unrecorded stage has to run;
        # verifying it would at best re-prove work we have no evidence was ever done.
        claimed = (completed or {}).get(s.name, {}).get("state") in ("ok", "skipped")
        if not claimed:
            checked.append({"stage": s.name, "claimed": False, "verified": None})
            return {"mode": "auto", "skip": skip, "start_at": s.name, "checked": checked}

        try:
            v = s.verify_outputs(ctx)
            ok = bool(getattr(v, "ok", False))
        except Exception as exc:
            ok = False
            checked.append({"stage": s.name, "claimed": True, "verified": False,
                            "error": f"{type(exc).__name__}: {exc}"})
        else:
            checked.append({"stage": s.name, "claimed": True, "verified": ok,
                            "failures": list(getattr(v, "failures", []))})
        if not ok:
            # The record said done; disk disagrees. Disk wins.
            return {"mode": "auto", "skip": skip, "start_at": s.name, "checked": checked}
        skip.append(s.name)

    return {"mode": "auto", "skip": skip, "start_at": None, "checked": checked}


def apply(runner: Any, report: dict) -> Any:
    """Return a runner with the skipped stages removed.

    Rebuilds rather than mutating so the original plan stays intact for reporting — an
    operator should be able to see both what was planned and what was actually run.
    """
    skip = set(report.get("skip", []))
    if not skip:
        return runner
    kept = [s for s in runner.stages if s.name not in skip]
    return type(runner)(runner.name, kept)
