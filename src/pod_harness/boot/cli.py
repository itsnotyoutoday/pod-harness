"""`podh-boot` — run the job's boot spec, then keep the pod honest until it ends.

    podh-boot [--spec FILE] [--exit-on-fail] [--fresh] [--boot-dir DIR] [--python PY]

The spec comes from GET {PODH_CONTROL_URL}/v1/pod/spec with the pod token, or from --spec
(or $PODH_BOOT_SPEC) — a JSON file with the same shape: {"boot": {...}, "policy": {...}}.

Stages, in this order, each under a time limit and each reporting boot lines:

    gpu_check → binaries → models → addons → app → workload → ready → app.serve

A failed stage reports done(rc, reason, stage, log_tail) and then IDLES: an exiting
container is restarted by the provider and fails identically, forever, while billing (the
same rule as podh-init's fatal). --exit-on-fail exits with the rc instead — for tests and
for a supervisor that is not a container runtime. Every other ending — app.serve finishing,
the workload dying twice, the worker's POST /done — ends the same way.

Stages already satisfied on a resumed pod's disk are skipped, except gpu_check, which always
runs. --fresh ignores what the disk says.

Two ways to run it:

  - as the main process of an image built for it (ENTRYPOINT ["podh-boot"]);
  - on a catalog image, from the provider's start command:
        bash -lc "pip install 'pod-harness @ git+https://github.com/<owner>/pod-harness@<tag>' && podh-boot"
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .. import heartbeat
from ..local import LocalServer, LocalState, start_beat
from . import addons, app, binaries, gpu, models, serve, workload
from .core import BootFailure, Context, run_stage, say

EXIT_USAGE = 2


def load_spec(path: Optional[str], ctx: Context) -> dict:
    if path:
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError) as e:
            raise BootFailure(f"cannot read the boot spec {path}: {e}", stage="spec")
    if not heartbeat._cfg()[0]:
        raise BootFailure("no boot spec: give --spec FILE or set PODH_CONTROL_URL", stage="spec")
    deadline = time.time() + ctx.limit("spec")
    delay = 2.0
    while True:
        st, obj = heartbeat.request("GET", "/v1/pod/spec", timeout=20)
        if st == 200 and isinstance(obj, dict):
            return obj
        if st in (401, 403, 404) or time.time() + delay > deadline:
            raise BootFailure(f"GET /v1/pod/spec answered {st or 'nothing'} "
                              f"({heartbeat.last_error() or obj})", stage="spec")
        say(f"spec: control plane answered {st or 'nothing'}; retrying in {delay:.0f}s")
        time.sleep(delay)
        delay = min(delay * 2, 30)


def ready(ctx: Context) -> None:
    w = ctx.workload
    ctx.state.ready = True
    ctx.line(f"ready{f' — {w.name} on {w.url}' if w and w.url else ''}", "ready")


def _stages(ctx: Context) -> list:
    return [("gpu_check", gpu.run, None),
            ("binaries", binaries.run, None),
            ("models", models.run, ctx.limit("models", (ctx.boot.get("models") or {}).get("timeout_s")
                                             if isinstance(ctx.boot.get("models"), dict) else None)),
            ("addons", addons.run, None),
            ("app", app.run, None),
            ("workload", workload.run, workload.limit(ctx)),
            ("ready", ready, None)]


def _end(ctx: Context, exit_on_fail: bool) -> int:
    rc = (ctx.result or {}).get("rc", 0)
    if exit_on_fail:
        return rc
    say("idling rather than exiting — an exiting container is restarted by the provider "
        "and repeats itself while billing; pod-control stops this pod")
    while True:
        time.sleep(300)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="podh-boot", description="run the job's boot spec")
    ap.add_argument("--spec", default=os.environ.get("PODH_BOOT_SPEC"),
                    help="boot spec JSON file (default: GET /v1/pod/spec)")
    ap.add_argument("--exit-on-fail", action="store_true",
                    default=os.environ.get("PODH_BOOT_EXIT") == "1",
                    help="exit with the rc when the pod's work ends, instead of idling")
    ap.add_argument("--fresh", action="store_true", help="ignore what a resumed disk has")
    ap.add_argument("--boot-dir", help="state, logs, binaries (default $PODH_BOOT_DIR, "
                                       "else $PODH_WORKSPACE/.podh-boot)")
    ap.add_argument("--python", help="the workload's python (default $PODH_COMFY_PY, "
                                     "the ComfyUI venv, python3)")
    a = ap.parse_args(argv)

    state = LocalState()
    ctx = Context(boot_dir=a.boot_dir, python=a.python, fresh=a.fresh, state=state)
    say(f"podh-boot  {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  "
        f"boot dir {ctx.boot_dir}  models {ctx.models_root}")

    if heartbeat._cfg()[0]:
        try:
            os.unlink(heartbeat.STOP_FILE)    # a stale flag would silence a fresh boot
        except FileNotFoundError:
            pass
        except OSError:
            pass

    srv = LocalServer(state, on_done=lambda rc, reason, stage: ctx.finish(rc, reason, stage))
    srv.start()
    ctx.exports.update(srv.env())
    os.environ.update(srv.env())                # podh-report in our own children finds it
    say(f"local API {srv.url}")

    def live() -> bool:
        w = ctx.workload
        if not state.ready or w is None or not w.url:
            return True
        return w.answers(timeout=5)

    interval = float(os.environ.get("PODH_HEARTBEAT_SEC") or 60)
    _, wake = start_beat(state, interval=min(interval, 90), live=live)
    srv.on_busy = wake

    def on_term(signum, _frame):
        say(f"signal {signum}: stopping what this pod runs")
        for thing in (ctx.serve, ctx.workload):
            if thing is not None:
                try:
                    thing.stop(10)
                except Exception:
                    pass
        ctx.kill_procs()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, on_term)

    try:
        ctx.stage = "spec"
        ctx.load(load_spec(a.spec, ctx))
        for name, fn, lim in _stages(ctx):
            run_stage(ctx, name, fn, lim)
    except BootFailure as f:
        ctx.fail(f)
        return _end(ctx, a.exit_on_fail)

    if ctx.boot.get("models"):
        models.Watcher(ctx).start()

    def on_reply(reply: dict) -> None:
        if (reply.get("drain") or reply.get("park")) and ctx.serve is not None:
            import threading
            threading.Thread(target=ctx.serve.drain, daemon=True).start()
    heartbeat.on_reply(on_reply)

    try:
        s = serve.build(ctx)
    except BootFailure as f:
        ctx.fail(f)
        return _end(ctx, a.exit_on_fail)
    if s is not None:
        ctx.serve = s
        ctx.line(f"starting app serve: {' '.join(s.cmd)[:120]}", "app_serve")
        try:
            s.start()
        except OSError as e:
            ctx.fail(BootFailure(f"app serve could not start: {e}", stage="app_serve"))
            return _end(ctx, a.exit_on_fail)
    if ctx.workload is not None:
        ctx.workload.supervise()

    if ctx.workload is None and ctx.serve is None and a.exit_on_fail:
        say("ready, and nothing to supervise")
        return 0
    ctx.finished.wait()
    srv.drain()
    return _end(ctx, a.exit_on_fail)


if __name__ == "__main__":
    raise SystemExit(main())
