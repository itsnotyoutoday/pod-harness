"""The machinery every boot stage shares: the context, the stage runner, the workload env
file, child processes, and the one way a boot ends.

A stage is a function `run(ctx)`. It reports progress with `ctx.line()`, runs commands with
`ctx.run()` (which bounds them by the stage's deadline and kills them on timeout), and fails
by raising `BootFailure(reason, rc=, stage=, log_tail=)`. Everything else — the time limit,
the boot lines, turning an exception into done(rc, reason, stage) — is the runner's.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .. import heartbeat
from ..local import LocalState

#: Seconds each stage may take unless the spec says otherwise (boot.limits.<stage>, and the
#: stage's own timeout_s / up_timeout_s where the schema has one). Generous: they exist to
#: turn a wedged boot into a reported failure, not to race a slow but working one —
#: pod-control's boot_stall_s and boot_timeout_s are the real bounds.
DEFAULT_LIMITS = {"spec": 300, "gpu_check": 300, "binaries": 1200, "models": 5400,
                  "addons": 2700, "app": 1800, "workload": 600, "ready": 60}

#: The order is the contract (APP-API §4); `app_serve` is not a boot stage but names the
#: failure of the long-running app process.
STAGES = ("gpu_check", "binaries", "models", "addons", "app", "workload", "ready")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TAIL_BYTES = 1024


class BootFailure(Exception):
    def __init__(self, reason: str, rc: int = 1, stage: Optional[str] = None,
                 log_tail: Optional[str] = None):
        super().__init__(reason)
        self.reason, self.rc, self.stage, self.log_tail = reason, rc, stage, log_tail


def say(text: str) -> None:
    print(f"[podh-boot] {text}", flush=True)


def tail(path, n: int = TAIL_BYTES) -> Optional[str]:
    """The last `n` bytes of a file, as text; None when there is none."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            t = f.read().decode("utf-8", "replace")
        return t or None
    except OSError:
        return None


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def safe_rel(rel: str, what: str = "path") -> str:
    """A relative path that stays where it is put: no absolute, no `..`, not empty."""
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError(f"{what}: empty")
    if os.path.isabs(rel) or rel.startswith(("/", "\\")) or "\x00" in rel:
        raise ValueError(f"{what} {rel!r}: absolute paths are refused")
    norm = os.path.normpath(rel)
    if norm == "." or norm.startswith("..") or ".." in Path(rel).parts:
        raise ValueError(f"{what} {rel!r}: escapes its root")
    return norm


def inside(root, rel: str, what: str = "path") -> Path:
    """root/rel, refusing anything that resolves outside root (symlinked parents included)."""
    rel = safe_rel(rel, what)
    root_r = Path(os.path.realpath(root))
    target = Path(root) / rel
    parent = Path(os.path.realpath(target.parent))
    if parent != root_r and root_r not in parent.parents:
        raise ValueError(f"{what} {rel!r}: resolves outside {root}")
    return target


# -- the workload env file --------------------------------------------------------------------

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def read_env_file(path) -> list:
    """[(key, raw value)] in file order; blank lines and comments skipped."""
    out = []
    try:
        text = Path(path).read_text()
    except OSError:
        return out
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = _LINE.match(ln)
        if m:
            out.append((m.group(1), _unquote(m.group(2))))
    return out


def apply_env(pairs, base: dict) -> dict:
    """Apply KEY=value pairs in order, expanding $VAR / ${VAR} against the env so far — so
    `PATH={bindir}:$PATH` prepends, and a later line sees an earlier one."""
    env = dict(base)
    for k, v in pairs:
        if isinstance(v, str):
            v = _VAR.sub(lambda m: env.get(m.group(1) or m.group(2), ""), v)
        env[k] = str(v)
    return env


def set_env_lines(path, mapping: dict) -> None:
    """Upsert KEY=value lines (unexpanded) into the env file, keeping everything else."""
    if not mapping:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    done = set()
    out = []
    for ln in lines:
        m = _LINE.match(ln)
        if m and m.group(1) in mapping:
            if m.group(1) in done:
                continue
            out.append(f"{m.group(1)}={mapping[m.group(1)]}")
            done.add(m.group(1))
        else:
            out.append(ln)
    for k, v in mapping.items():
        if k not in done:
            out.append(f"{k}={v}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.replace(tmp, path)


# -- child processes ----------------------------------------------------------------------------

def stop_process(p: subprocess.Popen, grace: float) -> Optional[int]:
    """SIGTERM the process GROUP, wait `grace`, then SIGKILL it. A shell wrapper's children
    go with it, so nothing keeps holding the GPU or the port after a stop."""
    if p.poll() is not None:
        return p.returncode
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        p.terminate()
    try:
        return p.wait(timeout=max(0.0, grace))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        p.kill()
    try:
        return p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return None


def as_cmd(cmd) -> list:
    if isinstance(cmd, str):
        return ["bash", "-lc", cmd]
    if isinstance(cmd, list) and cmd and all(isinstance(x, (str, int, float)) for x in cmd):
        return [str(x) for x in cmd]
    raise ValueError(f"a command is a non-empty list of strings or a string, not {cmd!r}")


# -- the context ----------------------------------------------------------------------------------

def _default_workspace() -> Path:
    return Path(os.environ.get("PODH_WORKSPACE") or "/workspace")


def _writable_dir(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        return os.access(p, os.W_OK)
    except OSError:
        return False


COMFY_CANDIDATES = ("/opt/ComfyUI", "/workspace/runpod-slim/ComfyUI", "/workspace/ComfyUI")


# -- what the image baked in ------------------------------------------------------------------

def baked_root() -> Optional[Path]:
    """$PODH_BAKED_ROOT: what an image built for podh-boot installed at build time.

        bin/<name>-<sha12>/   a pinned binary, laid out (marker included) exactly as the
                              binaries stage lays out its own installs
        addons.json           {"addons": {name: {"commit", "dir"}}}: checkouts the image made
                              AND whose requirements.txt it installed, at that commit, there

    Unset (any image not built for it): nothing is baked.
    """
    v = os.environ.get("PODH_BAKED_ROOT")
    return Path(v) if v else None


def baked_addons() -> dict:
    r = baked_root()
    if r is None:
        return {}
    try:
        obj = json.loads((r / "addons.json").read_text())
    except (OSError, ValueError):
        return {}
    items = obj.get("addons") if isinstance(obj, dict) else None
    if not isinstance(items, dict):
        return {}
    return {k: v for k, v in items.items() if isinstance(v, dict)}


class Context:
    def __init__(self, *, boot_dir=None, python: Optional[str] = None, fresh: bool = False,
                 state: Optional[LocalState] = None):
        ws = _default_workspace()
        bd = boot_dir or os.environ.get("PODH_BOOT_DIR")
        if not bd:
            bd = ws / ".podh-boot"
            if not _writable_dir(bd):
                bd = Path.home() / ".podh-boot"
        self.boot_dir = Path(bd)
        self.boot_dir.mkdir(parents=True, exist_ok=True)
        (self.boot_dir / "logs").mkdir(exist_ok=True)
        self.models_root = Path(os.environ.get("PODH_MODELS_ROOT") or ws / "models")
        self.workload_env = Path(os.environ.get("PODH_WORKLOAD_ENV")
                                 or self.boot_dir / "workload.env")
        self.python_override = python
        self.fresh = fresh
        self.state = state or LocalState()
        self.boot: dict = {}
        self.policy: dict = {}
        self.facts: dict = {}
        self.comfy_root: Optional[Path] = None
        self.comfy_py: Optional[str] = None
        self.bin_dirs: list = []
        self.models_rev = 0
        self.exports: dict = {}
        self.stage: Optional[str] = None
        self.deadline: Optional[float] = None
        self.procs: list = []
        self.procs_lock = threading.Lock()
        self.finished = threading.Event()
        self.result: Optional[dict] = None
        self._finish_lock = threading.Lock()
        self.workload = None          # set by the workload stage
        self.serve = None             # set when app.serve starts
        self.manifest = None          # models.Manifest, kept for mid-life additions
        self.app_env: dict = {}       # the current job's app env, from GET /v1/pod/env
        self._markers = self.boot_dir / "markers.json"

    # -- configuration --------------------------------------------------------------------
    def load(self, spec: dict) -> None:
        if not isinstance(spec, dict):
            raise BootFailure("the boot spec is not a JSON object", stage="spec")
        if "boot" in spec or "policy" in spec:
            self.boot = spec.get("boot") or {}
            self.policy = spec.get("policy") or {}
        else:
            self.boot = spec
        if not isinstance(self.boot, dict):
            raise BootFailure("boot is not a JSON object", stage="spec")
        self.resolve_comfy()
        self.refresh_exports()

    def resolve_comfy(self) -> None:
        wl = (self.boot.get("workload") or {})
        cfg = wl.get("comfyui") if isinstance(wl.get("comfyui"), dict) else {}
        root = cfg.get("root") or os.environ.get("PODH_COMFY_ROOT")
        if not root:
            for c in COMFY_CANDIDATES:
                if (Path(c) / "main.py").is_file():
                    root = c
                    break
        self.comfy_root = Path(root) if root else None
        py = self.python_override or cfg.get("python") or os.environ.get("PODH_COMFY_PY")
        if not py and self.comfy_root:
            for c in ("venv/bin/python", ".venv/bin/python"):
                if (self.comfy_root / c).is_file():
                    py = str(self.comfy_root / c)
                    break
        self.comfy_py = py

    @property
    def python(self) -> str:
        """The workload's interpreter: addons install into it, probes run with it."""
        return self.comfy_py or shutil.which("python3") or sys.executable

    def refresh_exports(self) -> None:
        e = {"PODH_MODELS_ROOT": str(self.models_root),
             "PODH_WORKLOAD_ENV": str(self.workload_env),
             "PODH_BOOT_DIR": str(self.boot_dir)}
        if self.comfy_root:
            e["PODH_COMFY_ROOT"] = str(self.comfy_root)
        if self.comfy_py:
            e["PODH_COMFY_PY"] = self.comfy_py
        for k in ("PODH_JOB_ID", "PODH_GPU_COUNT"):
            if os.environ.get(k) is not None:
                e[k] = os.environ[k]
        self.exports.update(e)

    def limit(self, stage: str, default: Optional[float] = None) -> float:
        lim = (self.boot.get("limits") or {}).get(stage)
        try:
            return float(lim) if lim else float(default or DEFAULT_LIMITS.get(stage, 600))
        except (TypeError, ValueError):
            return float(default or DEFAULT_LIMITS.get(stage, 600))

    # -- reporting --------------------------------------------------------------------------
    def line(self, text: str, stage: Optional[str] = None) -> None:
        stage = stage or self.stage
        say(f"{stage}: {text}" if stage else text)
        if heartbeat._cfg()[0]:
            heartbeat.boot(text, stage)

    # -- time ---------------------------------------------------------------------------------
    def remaining(self) -> Optional[float]:
        return None if self.deadline is None else max(0.0, self.deadline - time.time())

    def bounded(self, t: float) -> float:
        r = self.remaining()
        return t if r is None else max(1.0, min(t, r))

    # -- env -----------------------------------------------------------------------------------
    def child_env(self, extra: Optional[dict] = None) -> dict:
        """os.environ, then the workload env file, then our exports, then `extra`."""
        env = apply_env(read_env_file(self.workload_env), dict(os.environ))
        env.update(self.exports)
        if extra:
            env = apply_env(list(extra.items()), env)
        return env

    # -- commands ---------------------------------------------------------------------------
    def run(self, cmd, *, timeout: float, log=None, cwd=None, env=None) -> tuple:
        """Run to completion within min(timeout, the stage's time left). (rc, output tail).

        rc is None on timeout (the process group is killed). Output goes to `log` (appended)
        when given, else is captured; either way its tail comes back for a failure report.
        """
        timeout = self.bounded(timeout)
        if log is not None:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            out = open(log, "ab")
        else:
            out = subprocess.PIPE
        try:
            p = subprocess.Popen(as_cmd(cmd), stdout=out, stderr=subprocess.STDOUT, cwd=cwd,
                                 env=env if env is not None else self.child_env(),
                                 start_new_session=True)
        except OSError as e:
            if log is not None:
                out.close()
            return 127, f"{type(e).__name__}: {e}"
        with self.procs_lock:
            self.procs.append(p)
        try:
            try:
                data, _ = p.communicate(timeout=timeout)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                stop_process(p, 5)
                data, rc = None, None
                if log is None:
                    try:
                        data = p.stdout.read() if p.stdout else None
                    except Exception:
                        data = None
        finally:
            with self.procs_lock:
                if p in self.procs:
                    self.procs.remove(p)
            if log is not None:
                out.close()
        if log is not None:
            return rc, tail(log)
        text = (data or b"").decode("utf-8", "replace")
        return rc, text[-TAIL_BYTES:] or None

    def kill_procs(self) -> None:
        with self.procs_lock:
            procs = list(self.procs)
        for p in procs:
            stop_process(p, 2)

    # -- markers (what a resumed pod already has) ------------------------------------------------
    def markers(self) -> dict:
        try:
            return json.loads(self._markers.read_text())
        except (OSError, ValueError):
            return {}

    def marker(self, key: str):
        return None if self.fresh else self.markers().get(key)

    def set_marker(self, key: str, value) -> None:
        m = self.markers()
        if value is None:
            m.pop(key, None)
        else:
            m[key] = value
        tmp = self._markers.with_name(self._markers.name + ".tmp")
        tmp.write_text(json.dumps(m, indent=1, sort_keys=True))
        os.replace(tmp, self._markers)

    # -- the end ---------------------------------------------------------------------------------
    def finish(self, rc: int, reason: Optional[str] = None, stage: Optional[str] = None,
               log_tail: Optional[str] = None) -> dict:
        """End the pod's work exactly once: stop reporting alive, report done, stop children.

        Every exit path comes here — a failed stage, the workload dying twice, app.serve
        ending, the worker's POST /done — so there is one place that says how the pod
        ended, and a second ending (the workload dying while we stop it) cannot overwrite
        the first.
        """
        with self._finish_lock:
            if self.result is not None:
                return {"requested": False, "error": "already finished",
                        "configured": bool(heartbeat._cfg()[0])}
            self.result = {"rc": int(rc), "reason": reason, "stage": stage}
        try:
            Path(heartbeat.STOP_FILE).touch()
        except OSError:
            pass
        url, _, pod_id = heartbeat._cfg()
        out = {"requested": False, "configured": bool(url and pod_id), "error": None}
        mark = "✓" if rc == 0 else "✗"
        say(f"{mark} done rc={rc}" + (f" stage={stage}" if stage else "")
            + (f": {reason}" if reason else ""))
        if url and pod_id:
            r = heartbeat.done(rc, reason=reason, stage=stage, log_tail=log_tail)
            out["requested"] = r is not None
            if r is None:
                out["error"] = heartbeat.last_error() or "no response"
                say(f"  control: NOT SENT — {out['error']}")
        for thing in (self.serve, self.workload):
            if thing is not None:
                try:
                    thing.stop(10)
                except Exception:
                    pass
        self.kill_procs()
        self.finished.set()
        return out

    def fail(self, f: BootFailure) -> dict:
        return self.finish(f.rc, f.reason, f.stage or self.stage, f.log_tail)


def run_stage(ctx: Context, name: str, fn, limit: Optional[float] = None) -> None:
    """Run one stage under its time limit; translate any failure into a BootFailure."""
    limit = limit if limit is not None else ctx.limit(name)
    ctx.stage = name
    ctx.state.stage = name
    ctx.deadline = time.time() + limit
    t0 = time.time()
    box: dict = {}

    def target():
        try:
            fn(ctx)
        except BaseException as e:     # noqa: BLE001 — re-raised on the main thread
            box["exc"] = e

    t = threading.Thread(target=target, daemon=True, name=f"podh-boot-{name}")
    t.start()
    t.join(limit + 5)
    if t.is_alive():
        ctx.kill_procs()
        raise BootFailure(f"{name} did not finish within {limit:.0f}s", stage=name)
    exc = box.get("exc")
    ctx.deadline = None
    if exc is None:
        say(f"{name}: done in {time.time() - t0:.0f}s")
        return
    if isinstance(exc, BootFailure):
        exc.stage = exc.stage or name
        raise exc
    raise BootFailure(f"{name}: {type(exc).__name__}: {exc}", stage=name)
