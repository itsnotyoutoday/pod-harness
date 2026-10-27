"""`podh-worker` command line. Flags override PODH_WORKER_* env; see __init__.py."""
from __future__ import annotations

import argparse
import os
import signal
from typing import Optional

from .. import heartbeat
from .loop import Worker


def _env(name: str, default):
    v = os.environ.get(name)
    return type(default)(v) if v not in (None, "") else default


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="podh-worker", description="claim and run tasks")
    ap.add_argument("--capacity", type=int, default=_env("PODH_WORKER_CAPACITY", 1),
                    help="tasks executing at once (default 1)")
    ap.add_argument("--prefetch", type=int, default=_env("PODH_WORKER_PREFETCH", 1),
                    help="extra tasks claimed ahead, models fetched while one runs (default 1)")
    ap.add_argument("--once", action="store_true", help="one task, then exit")
    ap.add_argument("--kinds", default=os.environ.get("PODH_WORKER_KINDS", ""),
                    help="comma-separated kinds this worker takes (default: all it has)")
    ap.add_argument("--poll", type=float, default=_env("PODH_WORKER_POLL_S", 5.0),
                    help="seconds between claims while there is no work")
    ap.add_argument("--stall-s", type=float, default=_env("PODH_WORKER_STALL_S", 600.0),
                    help="a ComfyUI execution with no progress this long is stopped")
    ap.add_argument("--interrupt-grace", type=float, default=20.0,
                    help="seconds an interrupted run gets before the workload restarts")
    ap.add_argument("--app-root", default=os.environ.get("PODH_APP_ROOT"))
    ap.add_argument("--workdir", default=os.environ.get("PODH_WORKER_DIR"))
    a = ap.parse_args(argv)
    if not heartbeat._cfg()[0]:
        print("podh-worker: PODH_CONTROL_URL is not set; there is nowhere to claim from",
              flush=True)
        return 2
    w = Worker(capacity=a.capacity, prefetch=0 if a.once else a.prefetch, once=a.once,
               kinds=[k.strip() for k in a.kinds.split(",") if k.strip()], poll_s=a.poll,
               stall_s=a.stall_s, interrupt_grace=a.interrupt_grace, app_root=a.app_root,
               workdir=a.workdir)

    def on_term(signum, _frame):
        print(f"[podh-worker] signal {signum}: draining", flush=True)
        w.draining = True
        w.wake.set()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    return w.run()


if __name__ == "__main__":
    raise SystemExit(main())
