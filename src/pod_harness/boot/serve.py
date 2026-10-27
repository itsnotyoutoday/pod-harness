"""app.serve: the app's long-running process, started once the pod is ready.

    "app": {..., "serve": [cmd] | "shell line", "serve_env": {...}, "serve_grace_s": 120}

Started after `ready` (after the workload answers, or right after app.run when there is no
workload), in the app root, `{app}` in its argv replaced by it, with the boot's environment,
the job's app env (/v1/pod/env), then serve_env. Supervised:

  - exits 0            the work is finished: done(0). A workload-less job (a trainer) ends
                       exactly like this.
  - exits non-zero     restarted once; the second time, done(rc, stage "app_serve") with the
                       end of its log.
  - drain or park on the heartbeat reply
                       SIGTERM to its process group, `serve_grace_s` (default 120) to finish
                       the current step, then SIGKILL. Not an ending: the pod idles and
                       pod-control stops or parks it per policy.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Optional

from .core import BootFailure, Context, as_cmd, stop_process, tail

DEFAULT_GRACE = 120.0


class Serve:
    def __init__(self, ctx: Context, cmd: list, env: dict, cwd: Optional[str],
                 grace: float, log: Path):
        self.ctx, self.cmd, self.env, self.cwd, self.grace, self.log = ctx, cmd, env, cwd, grace, log
        self.proc: Optional[subprocess.Popen] = None
        self.restarts = 0
        self.stopping = False
        self.drained = False
        self.lock = threading.RLock()   # _wait holds it while finish() calls stop()

    def start(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "ab") as out:
            out.write(f"\n=== podh-boot: starting app.serve {' '.join(self.cmd)} ===\n".encode())
            out.flush()
            self.proc = subprocess.Popen(self.cmd, stdout=out, stderr=subprocess.STDOUT,
                                         cwd=self.cwd, env=self.env, start_new_session=True)
        threading.Thread(target=self._wait, args=(self.proc,), daemon=True,
                         name="podh-serve-supervise").start()

    def _wait(self, proc: subprocess.Popen) -> None:
        rc = proc.wait()
        ctx = self.ctx
        with self.lock:
            if self.stopping or ctx.finished.is_set() or proc is not self.proc:
                return
            if rc == 0:
                snap = ctx.state.snapshot()
                if snap["drain"] or snap["park"]:
                    # A worker that saw the drain on /state and exited 0 before our own
                    # drain() ran: the pod idles for pod-control, it is not finished.
                    self.drained = True
                    ctx.line("app serve exited 0 on drain; the pod idles", "app_serve")
                    return
                ctx.finish(0, "app serve finished")
                return
            if self.restarts >= 1:
                ctx.fail(BootFailure(f"app serve exited {rc} again after a restart",
                                     rc=rc, stage="app_serve", log_tail=tail(self.log)))
                return
            self.restarts += 1
            ctx.line(f"app serve exited {rc}; restarting it once", "app_serve")
            try:
                self.start()
            except OSError as e:
                ctx.fail(BootFailure(f"app serve could not restart: {e}", stage="app_serve"))

    def stop(self, grace: Optional[float] = None) -> Optional[int]:
        with self.lock:
            self.stopping = True
            p = self.proc
        if p is None:
            return None
        return stop_process(p, self.grace if grace is None else grace)

    def drain(self) -> None:
        """Drain/park: stop serving, gracefully, once. The pod stays up and idles."""
        if self.drained or self.stopping:
            return
        self.drained = True
        self.ctx.line(f"drain: stopping app serve (grace {self.grace:.0f}s)", "app_serve")
        rc = self.stop()
        self.ctx.line(f"app serve stopped for drain (rc {rc})", "app_serve")


def build(ctx: Context) -> Optional[Serve]:
    app = ctx.boot.get("app") or {}
    cmd = app.get("serve")
    if not cmd:
        return None
    from .app import app_argv, app_dir, app_env
    d = app_dir(ctx)
    try:
        cmd = app_argv(cmd, d)
    except ValueError as e:
        raise BootFailure(f"app.serve: {e}", stage="app_serve")
    d.mkdir(parents=True, exist_ok=True)
    ctx.exports["PODH_APP_ROOT"] = ctx.exports["PODH_APP_DIR"] = str(d)
    env = app_env(ctx, {str(k): str(v) for k, v in (app.get("serve_env") or {}).items()})
    grace = float(app.get("serve_grace_s") or DEFAULT_GRACE)
    return Serve(ctx, cmd, env, str(d), grace, ctx.boot_dir / "logs" / "serve.log")
