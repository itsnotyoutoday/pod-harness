"""The worker loop (TASK-API §4): claim → models → start → inputs → run → upload → done.

One orchestrating thread claims, dispatches and watches; each claimed task has a beat
thread from claim to report (so a held or prefetched task keeps its lease too) and a thread
that prepares it (handler lookup, models) and then executes it. At most `capacity` execute
at once; up to `capacity + prefetch` are held, so the next task's models download while
one runs.

Endings, per task:
  ok                      done {ok: true, result, artifacts, usage}
  failed                  done {ok: false, error {stage, reason, log_tail}}, with
                          retryable: true for a transient stage (inputs, upload, stall) —
                          or a release, when the task says `fallback: "app"` (pod-control
                          ends it `returned`; the app runs it itself)
  cancelled (beat reply {cancel: true, state: "cancelled"})   ComfyUI interrupted (workload
                          restarted if it ignores that), then done {ok: false, error
                          {stage: "cancelled"}}; before start: dropped
  lease lost (beat {cancel: true, state: other}, or 404/409/410)   work stopped, nothing
                          reported
  no handler / kind not served here             release "no handler"
  drain, park, SIGTERM    unstarted tasks released ("draining"), running ones finish,
                          then exit 0
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from .. import local
from ..comfy import Cancelled, ComfyError
from . import api
from .context import TaskContext
from .handlers import TaskFailed, load_handlers

DEFAULT_LEASE = 180.0
#: Failures worth another attempt, perhaps on another pod: pod-control requeues them
#: (attempt + 1) on done {retryable: true}, or fails them at max_attempts.
RETRYABLE_STAGES = ("inputs", "upload", "stall")
MAX_RESULT = 256 * 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SKIP = (".part", ".aria2", ".tmp", ".podh-tmp")


def _log(t: str) -> None:
    print(t, flush=True)


class TaskRun:
    def __init__(self, task: dict, lease_s: float, ctx: TaskContext, prefetched: bool):
        self.task, self.ctx = task, ctx
        self.id = str(task["task_id"])
        self.kind = str(task.get("kind") or "")
        self.lease_s = float(task.get("lease_s") or lease_s or DEFAULT_LEASE)
        self.held = task.get("state") == "held" or bool(task.get("held"))
        self.claimed_at = time.time()
        self.prefetched = prefetched
        self.ready = threading.Event()
        self.handler = None
        self.started = False
        self.started_at: Optional[float] = None
        self.stop_at: Optional[float] = None
        self.closed = False             # released, dropped or reported: nothing more to say
        self.lock = threading.Lock()
        self.beat_stop = threading.Event()
        self.held_s = 0.0
        self.vram_peak = 0


class InputCache:
    """Fetched inputs kept across tasks under a byte budget, keyed by sha256 when the task
    gives one, else by the URL without its query (a presigned link's signature changes;
    the object it names does not). Least recently used goes first."""

    def __init__(self, root: Path, budget_bytes: int):
        self.root, self.budget = Path(root), int(budget_bytes)
        self.lock = threading.Lock()

    @staticmethod
    def key(url: str, sha256: Optional[str]) -> str:
        if sha256:
            return "sha-" + sha256.lower()
        return "url-" + hashlib.sha256(url.split("?", 1)[0].encode()).hexdigest()

    def get(self, key: str, dest: Path) -> bool:
        if self.budget <= 0:
            return False
        p = self.root / key
        with self.lock:
            if not p.is_file():
                return False
            os.utime(p)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
            return True

    def put(self, key: str, src: Path) -> None:
        if self.budget <= 0 or src.stat().st_size > self.budget:
            return
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / f".{key}.tmp"
            shutil.copyfile(src, tmp)
            os.replace(tmp, self.root / key)
            files = sorted((f for f in self.root.iterdir() if f.is_file() and not
                            f.name.startswith(".")), key=lambda f: f.stat().st_mtime)
            total = sum(f.stat().st_size for f in files)
            while files and total > self.budget:
                f = files.pop(0)
                total -= f.stat().st_size
                f.unlink()


class Worker:
    def __init__(self, *, capacity: int = 1, prefetch: int = 1, once: bool = False,
                 kinds: Optional[list] = None, poll_s: float = 5.0, stall_s: float = 600.0,
                 interrupt_grace: float = 20.0, app_root=None, workdir=None,
                 models_root=None, model_timeout_s: float = 3600.0, use_ws: bool = True,
                 local_client: Optional[local.Client] = None, log=_log):
        self.capacity = max(1, int(capacity))
        self.prefetch = max(0, int(prefetch))
        self.once = once
        self.kinds = [k for k in (kinds or []) if k]
        self.poll_s = float(poll_s)
        self.stall_s = float(stall_s)
        self.interrupt_grace = float(interrupt_grace)
        self.app_root = Path(app_root or os.environ.get("PODH_APP_ROOT") or os.getcwd())
        self.models_root = Path(models_root or os.environ.get("PODH_MODELS_ROOT")
                                or Path(os.environ.get("PODH_WORKSPACE") or "/workspace") / "models")
        base = workdir or os.environ.get("PODH_WORKER_DIR") or (
            Path(os.environ["PODH_BOOT_DIR"]) / "tasks" if os.environ.get("PODH_BOOT_DIR")
            else Path(tempfile.gettempdir()) / "podh-worker")
        self.workdir = Path(base)
        self.model_timeout_s = float(model_timeout_s)
        self.use_ws = use_ws
        self.local = local_client or local.Client()
        self.log = log
        gb = float(os.environ.get("PODH_INPUT_CACHE_GB") or 2)
        self.cache = InputCache(self.workdir / ".input-cache", int(gb * 1e9))
        self.handlers, self.handler_errors = load_handlers(self.app_root)
        self.nvenc = os.environ.get("PODH_NVENC") == "1"
        self.tasks: dict = {}               # id → TaskRun, in claim order
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.draining = False
        self.broken: Optional[str] = None
        self.claimed_total = 0
        self.loaded: list = []
        self.next_claim = 0.0
        self.busy: Optional[bool] = None
        self.reports: list = []             # (task_id, outcome), for logs and tests
        self.threads: list = []

    # -- the loop -------------------------------------------------------------------------
    def run(self) -> int:
        for k, e in self.handler_errors.items():
            self.log(f"[podh-worker] handler {k}: {e}")
        self.log(f"[podh-worker] kinds {sorted(self.handlers)}; capacity {self.capacity}, "
                 f"prefetch {self.prefetch}; models {self.models_root}")
        self._start_vram()
        while True:
            st = self.local.state() if self.local.configured else {}
            if (st.get("drain") or st.get("park")) and not self.draining:
                self.log("[podh-worker] drain: no more claims; releasing what has not started")
                self.draining = True
            with self.lock:
                runs = list(self.tasks.values())
            if self.draining or self.broken:
                for t in runs:
                    if not t.started:
                        self._release(t, "draining")
                with self.lock:
                    if not self.tasks:
                        break
            elif self.once and self.claimed_total >= 1:
                with self.lock:
                    if not self.tasks:
                        break
            else:
                want = self.capacity + self.prefetch - len(runs)
                if self.once:
                    want = 1 - self.claimed_total
                if want > 0 and time.time() >= self.next_claim:
                    self._claim(want, st)
            self._dispatch()
            self._watch()
            self._set_busy()
            self.wake.wait(0.25)
            self.wake.clear()
        self._set_busy(force=False)
        for th in self.threads:
            th.join(timeout=5)
        if self.broken:
            self.log(f"[podh-worker] exiting 1: {self.broken}")
            return 1
        return 0

    def _set_busy(self, force: Optional[bool] = None) -> None:
        with self.lock:
            busy = bool(self.tasks) if force is None else force
            ids = ",".join(self.tasks)[:200]
        if busy != self.busy and self.local.configured:
            self.local.busy(busy, ids or None)
        self.busy = busy

    def _thread(self, fn, *args, name: str) -> None:
        th = threading.Thread(target=fn, args=args, daemon=True, name=name)
        self.threads = [t for t in self.threads if t.is_alive()] + [th]
        th.start()

    # -- claiming -------------------------------------------------------------------------
    def have_models(self, limit: int = 20000) -> list:
        out = []
        root = self.models_root
        if not root.is_dir():
            return out
        for d, dirs, files in os.walk(root, followlinks=True):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for f in sorted(files):
                if f.startswith(".") or f.endswith(_SKIP):
                    continue
                out.append(Path(d, f).relative_to(root).as_posix())
                if len(out) >= limit:
                    return out
        return out

    def _window_s(self, st: dict) -> Optional[float]:
        try:
            until = float(st.get("window_until"))
        except (TypeError, ValueError):
            return None
        return max(0.0, until - time.time())

    def _claim(self, n: int, st: dict) -> None:
        status, obj = api.claim(n, self.have_models(), list(self.loaded), self.nvenc,
                                self.kinds or None, self._window_s(st))
        if status != 200 or not isinstance(obj, dict):
            if status not in (204,):
                self.log(f"[podh-worker] claim: pod-control answered {status or 'nothing'}")
            self.next_claim = time.time() + self.poll_s
            return
        if obj.get("draining") and not self.draining:
            self.log("[podh-worker] pod-control says this pod is draining: no more claims")
            self.draining = True
        got = [t for t in (obj.get("tasks") or []) if isinstance(t, dict) and t.get("task_id")]
        if not got:
            self.next_claim = time.time() + self.poll_s
            return
        lease = float(obj.get("lease_s") or DEFAULT_LEASE)
        for task in got:
            with self.lock:
                if str(task["task_id"]) in self.tasks:
                    continue
                # beyond the slots free now: claimed ahead, its models fetched while it waits
                prefetched = len(self.tasks) >= self.capacity
            ctx = TaskContext(task, workdir=self.workdir / _safe(str(task["task_id"])),
                              models_root=self.models_root, app_root=self.app_root,
                              stall_s=self.stall_s, interrupt_grace=self.interrupt_grace,
                              local_client=self.local, log=self.log, use_ws=self.use_ws)
            t = TaskRun(task, lease, ctx, prefetched)
            with self.lock:
                self.tasks[t.id] = t
                self.claimed_total += 1
            ctx.log(f"claimed ({t.kind}{', held' if t.held else ''}"
                    f"{', prefetched' if prefetched else ''}); lease {t.lease_s:.0f}s")
            self._thread(self._beat, t, name=f"podh-beat-{t.id}")
            self._thread(self._prep, t, name=f"podh-prep-{t.id}")
        self.next_claim = time.time()

    # -- per task -------------------------------------------------------------------------
    def _beat(self, t: TaskRun) -> None:
        every = max(0.2, t.lease_s / 3.0)
        while not t.beat_stop.wait(every):
            st, obj = api.beat(t.id, t.ctx.fraction, t.ctx.message)
            if st in api.LOST:
                t.ctx.log(f"lease lost (beat answered {st}); stopping, reporting nothing")
                t.ctx.stop("lost")
                if not t.started:
                    self._drop(t)
            elif 200 <= st < 300 and api.gone(obj) == "lost":
                t.ctx.log(f"lease lost (the task is {obj.get('state')}); stopping, "
                          f"reporting nothing")
                t.ctx.stop("lost")
                if not t.started:
                    self._drop(t)
            elif 200 <= st < 300 and api.gone(obj) == "cancelled":
                if t.ctx.stop_reason is None:
                    t.ctx.log("cancelled by pod-control")
                t.ctx.stop("cancelled")
                if not t.started:
                    self._drop(t)
            if t.ctx.stop_reason and t.stop_at is None:
                t.stop_at = time.time()
                self.wake.set()

    def _prep(self, t: TaskRun) -> None:
        try:
            if self.kinds and t.kind not in self.kinds:
                t.ctx.log(f"kind {t.kind!r} is not served here ({self.kinds})")
                return self._release(t, "no handler")
            h = self.handlers.get(t.kind)
            if h is None:
                why = self.handler_errors.get(t.kind) or self.handler_errors.get("*") or \
                    "no handler declared"
                t.ctx.log(f"no handler for kind {t.kind!r}: {why}")
                return self._release(t, "no handler")
            t.handler = h
            if t.held or t.task.get("missing_models"):
                t0 = time.time()
                err = self._models(t)
                t.held_s = time.time() - t0
                if t.closed or t.ctx.stop_reason:
                    return
                if err:
                    t.ctx.log(f"models: {err}")
                    return self._release(t, f"models: {err}"[:300])
            t.ready.set()
            self.wake.set()
        except Exception as e:              # a prep crash must not strand the lease
            t.ctx.log(f"prepare crashed: {type(e).__name__}: {e}")
            self._release(t, f"worker error: {type(e).__name__}"[:300])

    def _needed(self, t: TaskRun) -> list:
        """The task's models (needs.models, and the claim's missing_models) not on this disk,
        by path. pod-control has added them to this pod's job (mid-life additions), so
        podh-boot's local API fetches them through GET /v1/pod/models."""
        sizes: dict = {}
        for m in (t.task.get("needs") or {}).get("models") or []:
            p = m.get("path") if isinstance(m, dict) else m
            if isinstance(p, str) and p:
                sizes[p] = m.get("size") if isinstance(m, dict) else None
        for p in t.task.get("missing_models") or []:
            if isinstance(p, str) and p:
                sizes.setdefault(p, None)

        def missing(p, size=None):
            f = self.models_root / p
            return not f.is_file() or (size is not None and f.stat().st_size != int(size))
        return [p for p, size in sizes.items() if missing(p, size)]

    def _models(self, t: TaskRun) -> Optional[str]:
        want = self._needed(t)
        if not want:
            return None
        t.ctx.log(f"fetching {len(want)} model(s): {', '.join(want)[:200]}")
        t.ctx.progress(0.0, "fetching models")
        if not self.local.configured:
            return f"no podh-boot local API to fetch {', '.join(want)[:200]}"
        st, obj = self.local.models_fetch([], want)
        if st != 202:
            return f"local /models/fetch answered {st}: {obj}"
        end = time.time() + self.model_timeout_s
        while time.time() < end:
            if t.ctx.stop_reason or t.closed:
                return "stopped"
            states = (self.local.state().get("models") or {})
            if all(states.get(p) in ("present", "failed") for p in want):
                bad = [p for p in want if states.get(p) == "failed"]
                return f"failed: {', '.join(bad)}" if bad else None
            time.sleep(0.5)
        return f"not present after {self.model_timeout_s:.0f}s"

    def _dispatch(self) -> None:
        with self.lock:
            runs = list(self.tasks.values())
        active = sum(1 for t in runs if t.started)
        for t in runs:
            if active >= self.capacity or self.draining or self.broken:
                break
            if not t.started and not t.closed and t.ready.is_set():
                t.started, t.started_at = True, time.time()
                active += 1
                self._thread(self._execute, t, name=f"podh-run-{t.id}")

    def _watch(self) -> None:
        """Task timeout_s, and a handler that will not stop."""
        now = time.time()
        with self.lock:
            runs = [t for t in self.tasks.values() if t.started and not t.closed]
        for t in runs:
            limit = t.task.get("timeout_s")
            if limit and t.ctx.stop_reason is None and now - t.started_at > float(limit):
                t.ctx.log(f"timeout_s {float(limit):.0f} passed; stopping")
                t.ctx.stop("timeout")
            if t.ctx.stop_reason and t.stop_at is None:
                t.stop_at = now
            if t.stop_at and now - t.stop_at > 2 * self.interrupt_grace + 10:
                t.ctx.log("the handler did not stop; restarting the workload and exiting")
                t.ctx.restart_workload()
                self._report(t, ok=False, error={
                    "stage": "cancelled" if t.ctx.cancelled() else (t.ctx.stop_reason or "run"),
                    "reason": "the handler did not stop"}, retryable=not t.ctx.cancelled())
                self.broken = f"task {t.id}: the handler did not stop"
                self._close(t)

    def _fetch_inputs(self, t: TaskRun) -> None:
        d = t.ctx.workdir / "inputs"
        for name, spec in (t.task.get("inputs") or {}).items():
            if not _NAME.match(str(name)):
                raise TaskFailed(f"input name {name!r} is not a plain name", "inputs")
            if isinstance(spec, str):
                spec = {"url": spec}
            if isinstance(spec, dict) and "value" in spec and not spec.get("url"):
                t.ctx.values[name] = spec["value"]      # a dependency's result path
                continue
            if not isinstance(spec, dict) or not spec.get("url"):
                raise TaskFailed(f"input {name}: no link (the claim should carry one)", "inputs")
            url = str(spec["url"])
            base = spec.get("filename") or url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            ext = Path(base).suffix if _NAME.match(Path(base).name or "x") else ""
            dest = d / f"{name}{ext}"
            key = InputCache.key(url, spec.get("sha256"))
            if not self.cache.get(key, dest):
                t.ctx.fetch(spec, dest)
                try:
                    self.cache.put(key, dest)
                except OSError as e:
                    t.ctx.log(f"input cache: {e}")
            t.ctx.inputs[name] = dest
            t.ctx.check()

    def _execute(self, t: TaskRun) -> None:
        ctx, task = t.ctx, t.task
        phase, phases = "start", {}
        ok, result, error = False, None, None
        retryable: Optional[bool] = None
        mark = time.time()

        def lap(name):
            nonlocal mark
            now = time.time()
            phases[name] = round(phases.get(name, 0.0) + now - mark, 3)
            mark = now
        try:
            for i in range(4):
                st, obj = api.start(t.id)
                if 200 <= st < 300 or st in api.LOST:
                    break
                time.sleep(0.5 * 2 ** i)
            if st in api.LOST:
                ctx.log(f"start answered {st}: no longer ours")
                ctx.stop("lost")
                return self._close(t)
            if not 200 <= st < 300:
                return self._release(t, f"start answered {st or 'nothing'}")
            ctx.check()
            phase = "inputs"
            self._fetch_inputs(t)
            lap("fetch_s")
            phase = "prepare"
            prepared = t.handler.prepare(task, ctx)
            ctx.check()
            phase = "run"
            out = t.handler.run(task, ctx, prepared)
            ctx.check()
            lap("run_s")
            phase = "finish"
            result = t.handler.finish(task, ctx, out)
            lap("upload_s")
            if result is not None:
                if not isinstance(result, dict):
                    raise TaskFailed("finish() must return a dict (the task's result) or None",
                                     "finish")
                if len(json.dumps(result, default=str)) > MAX_RESULT:
                    raise TaskFailed(f"the result is over {MAX_RESULT} bytes", "finish")
                result = json.loads(json.dumps(result, default=str))
            ok = True
        except Cancelled as e:
            ctx.log(f"stopped: {e}")
            if ctx.stop_reason == "lost":
                return self._close(t)
            ctx.stop_reason = ctx.stop_reason or "cancelled"
            error = {"stage": "cancelled", "reason": str(e) or "cancelled"}
        except TaskFailed as e:
            error = {"stage": e.stage or phase, "reason": e.reason}
            if e.retryable is not None:
                retryable = bool(e.retryable)
        except ComfyError as e:
            error = {"stage": e.stage or phase, "reason": e.reason}
        except Exception as e:
            error = {"stage": phase, "reason": f"{type(e).__name__}: {e}"}
        if error and phase != "start":
            lap({"inputs": "fetch_s", "finish": "upload_s"}.get(phase, "run_s"))
        if error:
            ctx.log(f"failed at {error['stage']}: {error['reason'][:300]}")
            error["log_tail"] = ctx.log_tail()
        usage = {"phases": phases, "held_s": round(t.held_s, 3), "prefetched": t.prefetched}
        if t.vram_peak:
            usage["vram_peak_mb"] = t.vram_peak
        if error and retryable is None:
            retryable = error["stage"] in RETRYABLE_STAGES
        self._report(t, ok, result=result, error=error, usage=usage,
                     retryable=bool(retryable) and error is not None
                     and error["stage"] != "cancelled")
        if ok:
            loads = (task.get("needs") or {}).get("loads")
            if isinstance(loads, list):
                self.loaded = [str(x) for x in loads]
        self._close(t)

    # -- endings ----------------------------------------------------------------------------
    def _report(self, t: TaskRun, ok: bool, result=None, error=None, usage=None,
                retryable: bool = False) -> None:
        with t.lock:
            if t.closed:
                return
            t.closed = True
        t.beat_stop.set()
        if error and error.get("stage") != "cancelled" and t.task.get("fallback") == "app":
            reason = f"failed on pod: {error['stage']}: {error['reason']}"[:300]
            api.release(t.id, reason)
            self.reports.append((t.id, "released"))
            return
        st, obj = api.done(t.id, ok, list(t.ctx.artifacts), result=result, error=error,
                           usage=usage, retryable=retryable)
        outcome = "succeeded" if ok else ("cancelled" if error and error.get("stage") ==
                                          "cancelled" else "retry" if retryable else "failed")
        said = f"{st or 'nothing'}"
        if isinstance(obj, dict) and "state" in obj:
            said += f", {'accepted' if obj.get('accepted') else 'not accepted'}: {obj['state']}"
        t.ctx.log(f"done: {outcome} (pod-control answered {said})")
        self.reports.append((t.id, outcome))

    def _release(self, t: TaskRun, reason: str) -> None:
        with t.lock:
            if t.closed:
                return
            t.closed = True
        t.beat_stop.set()
        st, _ = api.release(t.id, reason)
        t.ctx.log(f"released: {reason} ({st or 'no answer'})")
        self.reports.append((t.id, "released"))
        self._close(t)

    def _drop(self, t: TaskRun) -> None:
        """Cancelled or lost before it started: say nothing (a cancel is final)."""
        with t.lock:
            t.closed = True
        self.reports.append((t.id, "dropped"))
        self._close(t)

    def _close(self, t: TaskRun) -> None:
        t.closed = True
        t.beat_stop.set()
        with self.lock:
            self.tasks.pop(t.id, None)
        shutil.rmtree(t.ctx.workdir, ignore_errors=True)
        self.wake.set()

    # -- usage ------------------------------------------------------------------------------
    def _start_vram(self) -> None:
        exe = shutil.which("nvidia-smi")
        if not exe:
            return

        def loop():
            while True:
                time.sleep(2)
                with self.lock:
                    running = [t for t in self.tasks.values() if t.started and not t.closed]
                if not running:
                    continue
                try:
                    out = subprocess.run([exe, "--query-gpu=memory.used",
                                          "--format=csv,noheader,nounits"],
                                         capture_output=True, text=True, timeout=10).stdout
                    mb = sum(int(x.strip()) for x in out.splitlines() if x.strip().isdigit())
                except (OSError, subprocess.SubprocessError, ValueError):
                    continue
                for t in running:
                    t.vram_peak = max(t.vram_peak, mb)
        threading.Thread(target=loop, daemon=True, name="podh-vram").start()


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:120] or "task"
