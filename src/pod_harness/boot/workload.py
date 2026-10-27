"""workload: start ComfyUI (or a given command), wait until it answers, then supervise it.

    "workload": {"comfyui": {"root"?, "python"?, "port": 8188, "args": [...], "up_timeout_s": 300},
                 "env": {...},                       # merged into $PODH_WORKLOAD_ENV by binaries
                 "scan_log_for": ["…"]}
    or          {"command": [...] | "shell line", "port"?: n, "up_url"?: "http://…",
                 "up_timeout_s"?: n, "env": {...}, "scan_log_for": [...]}

ComfyUI's root is `root`, else $PODH_COMFY_ROOT, else the first of /opt/ComfyUI,
/workspace/runpod-slim/ComfyUI, /workspace/ComfyUI that has main.py; its python is `python`,
else $PODH_COMFY_PY, else the root's venv, else python3. Its model folders point at
$PODH_MODELS_ROOT through a generated extra_model_paths.yaml, so models land in one place
whatever image this is.

Up means the port answers HTTP (any status below 500) within up_timeout_s. While waiting,
and for the rest of its life, its log is scanned for fatal strings — podh-gpucheck's CUDA
list plus `scan_log_for`. A CUDA one ends the pod at stage gpu_check with rc 7 (the host is
bad); any other at stage workload. A crash is restarted once; the second ends the pod at
stage workload with the log's tail. The worker may also ask for a restart (local API POST
/workload/restart) when a job wedges inside the workload; that does not count as a crash.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .. import gpucheck
from .core import BootFailure, Context, as_cmd, read_env_file, say, stop_process, tail

#: ComfyUI's model folder names (folder_paths.py), each pointed at the same-named
#: directory under the models root. Unknown names are harmless; missing ones just are not
#: searched.
COMFY_FOLDERS = ("checkpoints", "clip", "clip_vision", "configs", "controlnet",
                 "diffusion_models", "unet", "embeddings", "loras", "upscale_models", "vae",
                 "vae_approx", "text_encoders", "hypernetworks", "photomaker", "style_models",
                 "gligen", "ipadapter", "audio_encoders", "model_patches", "latent_upscale_models",
                 "diffusers", "sams", "ultralytics", "insightface", "LLM")


def write_extra_model_paths(ctx: Context) -> Path:
    root = ctx.models_root
    names = list(COMFY_FOLDERS)
    try:
        names += sorted(p.name for p in root.iterdir() if p.is_dir() and p.name not in names)
    except OSError:
        pass
    lines = ["# written by podh-boot: ComfyUI's model folders live under PODH_MODELS_ROOT",
             "podh:", f"  base_path: {root}", "  is_default: true"]
    lines += [f"  {n}: {n}/" for n in names]
    out = ctx.boot_dir / "extra_model_paths.yaml"
    out.write_text("\n".join(lines) + "\n")
    return out


class Workload:
    def __init__(self, ctx: Context, cmd: list, *, cwd: Optional[str], env: dict,
                 url: Optional[str], up_timeout: float, patterns: list, log: Path,
                 name: str = "workload"):
        self.ctx, self.cmd, self.cwd, self.env = ctx, cmd, cwd, env
        self.url, self.up_timeout, self.log, self.name = url, up_timeout, log, name
        self.patterns = [(p, p.lower()) for p in patterns if p]
        self.cuda = {p.lower() for p in gpucheck.FATAL_CUDA}
        self.proc: Optional[subprocess.Popen] = None
        self.restarts = 0
        self.stopping = False
        self.offset = 0
        self.lock = threading.Lock()

    # -- process ------------------------------------------------------------------------
    def start(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "ab") as out:
            out.write(f"\n=== podh-boot: starting {' '.join(self.cmd)} ===\n".encode())
            out.flush()
            self.offset = out.tell()
            self.proc = subprocess.Popen(self.cmd, stdout=out, stderr=subprocess.STDOUT,
                                         cwd=self.cwd, env=self.env, start_new_session=True)

    def stop(self, grace: float = 10) -> None:
        self.stopping = True
        if self.proc is not None:
            stop_process(self.proc, grace)

    # -- checks -------------------------------------------------------------------------
    def answers(self, timeout: float = 3) -> bool:
        if not self.url:
            return self.proc is not None and self.proc.poll() is None
        try:
            with urllib.request.urlopen(self.url, timeout=timeout) as r:
                return r.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except Exception:
            return False

    def scan(self) -> Optional[tuple]:
        """(pattern, line, is_cuda) for the first fatal string in new log output."""
        try:
            with open(self.log, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return None
        cut = data.rfind(b"\n")
        if cut < 0:
            return None
        self.offset += cut + 1
        for raw in data[:cut + 1].splitlines():
            low = raw.decode("utf-8", "replace").lower()
            for orig, p in self.patterns:
                if p in low:
                    return orig, raw.decode("utf-8", "replace").strip()[:200], p in self.cuda
        return None

    def _fatal(self, hit) -> BootFailure:
        pat, line, is_cuda = hit
        if is_cuda:
            return BootFailure(f"fatal CUDA error in the {self.name} log: {line}",
                               rc=gpucheck.EXIT_GPU, stage="gpu_check", log_tail=tail(self.log))
        return BootFailure(f"fatal string {pat!r} in the {self.name} log: {line}",
                           stage="workload", log_tail=tail(self.log))

    def wait_up(self, deadline: float) -> None:
        while True:
            hit = self.scan()
            if hit:
                raise self._fatal(hit)
            rc = self.proc.poll()
            if rc is not None:
                hit = self.scan()
                if hit:
                    raise self._fatal(hit)
                raise BootFailure(f"{self.name} exited {rc} before it answered",
                                  rc=rc or 1, stage="workload", log_tail=tail(self.log))
            if self.answers():
                return
            if time.time() >= deadline:
                raise BootFailure(f"{self.name} did not answer {self.url} within "
                                  f"{self.up_timeout:.0f}s", stage="workload",
                                  log_tail=tail(self.log))
            time.sleep(0.5)

    # -- supervision --------------------------------------------------------------------
    def supervise(self, interval: float = 2.0) -> None:
        threading.Thread(target=self._loop, args=(interval,), daemon=True,
                         name="podh-workload-supervise").start()

    def _loop(self, interval: float) -> None:
        ctx = self.ctx
        while not self.stopping and not ctx.finished.is_set():
            time.sleep(interval)
            with self.lock:                # never at the same time as an asked-for restart
                if self.stopping or ctx.finished.is_set():
                    return
                hit = self.scan()
                if hit:
                    ctx.fail(self._fatal(hit))
                    return
                rc = self.proc.poll()
                if rc is None:
                    continue
                if self.restarts >= 1:
                    ctx.fail(BootFailure(f"{self.name} exited {rc} again after a restart",
                                         rc=rc or 1, stage="workload", log_tail=tail(self.log)))
                    return
                self.restarts += 1
                ctx.line(f"{self.name} exited {rc}; restarting it once", "workload")
                try:
                    self.start()
                    self.wait_up(time.time() + self.up_timeout)
                    ctx.line(f"{self.name} answering again", "workload")
                except BootFailure as f:
                    ctx.fail(f)
                    return

    def restart(self) -> None:
        """Stop and start the workload on request, and wait until it answers again.

        For a worker whose job wedged inside the workload (a C-level node ignores
        /interrupt): only a new process frees it. Asked-for restarts do not count against
        the one crash restart. Raises BootFailure when it does not come back; a fatal CUDA
        string on the way up also ends the pod (the host is bad), anything else is left to
        the supervisor, which treats a dead process as a crash.
        """
        with self.lock:
            if self.stopping or self.ctx.finished.is_set():
                raise BootFailure(f"{self.name} is stopping; not restarting", stage="workload")
            self.ctx.line(f"restarting {self.name} on request", "workload")
            if self.proc is not None:
                stop_process(self.proc, 10)
            self.start()
            try:
                self.wait_up(time.time() + self.up_timeout)
            except BootFailure as f:
                if f.rc == gpucheck.EXIT_GPU:
                    self.ctx.fail(f)
                raise
            self.ctx.line(f"{self.name} answering again after the requested restart", "workload")


def restart_for_api(ctx: Context) -> tuple:
    """(status, body) for the local API's POST /workload/restart."""
    w = ctx.workload
    if w is None:
        return 409, {"error": "this pod has no workload to restart"}
    if ctx.finished.is_set():
        return 409, {"error": "the pod's work has ended"}
    t0 = time.time()
    try:
        w.restart()
    except BootFailure as f:
        return 503, {"error": f.reason, "log_tail": f.log_tail}
    except OSError as e:
        return 500, {"error": f"{type(e).__name__}: {e}"}
    return 200, {"restarted": True, "after_s": round(time.time() - t0, 1), "url": w.url}


def build(ctx: Context) -> Optional[Workload]:
    wl = ctx.boot.get("workload")
    if not wl:
        return None
    if not isinstance(wl, dict):
        raise BootFailure("boot.workload must be an object")
    env = ctx.child_env()
    patterns = list(gpucheck.FATAL_CUDA) + [str(p) for p in (wl.get("scan_log_for") or [])]
    log = ctx.boot_dir / "logs" / "workload.log"
    cfg = wl.get("comfyui")
    if cfg is not None:
        cfg = cfg if isinstance(cfg, dict) else {}
        if not ctx.comfy_root or not (ctx.comfy_root / "main.py").is_file():
            raise BootFailure("no ComfyUI found (set workload.comfyui.root or $PODH_COMFY_ROOT; "
                              "looked in " + ", ".join(
                                  ["$PODH_COMFY_ROOT", "/opt/ComfyUI",
                                   "/workspace/runpod-slim/ComfyUI", "/workspace/ComfyUI"]) + ")")
        port = int(cfg.get("port") or 8188)
        args = [str(a) for a in (cfg.get("args") or [])]
        cmd = [ctx.python, "main.py"]
        if "--listen" not in args:
            cmd += ["--listen", "0.0.0.0"]
        if "--port" not in args:
            cmd += ["--port", str(port)]
        if "--extra-model-paths-config" not in args:
            cmd += ["--extra-model-paths-config", str(write_extra_model_paths(ctx))]
        cmd += args
        return Workload(ctx, cmd, cwd=str(ctx.comfy_root), env=env,
                        url=f"http://127.0.0.1:{port}/",
                        up_timeout=float(cfg.get("up_timeout_s") or 300),
                        patterns=patterns, log=log, name="ComfyUI")
    if wl.get("command"):
        try:
            cmd = as_cmd(wl["command"])
        except ValueError as e:
            raise BootFailure(f"workload.command: {e}")
        url = wl.get("up_url") or (f"http://127.0.0.1:{int(wl['port'])}/" if wl.get("port") else None)
        return Workload(ctx, cmd, cwd=wl.get("cwd"), env=env, url=url,
                        up_timeout=float(wl.get("up_timeout_s") or 300), patterns=patterns,
                        log=log)
    return None


def run(ctx: Context) -> None:
    w = build(ctx)
    if w is None:
        ctx.line("no workload: the app is the work")
        return
    ctx.line(f"starting {w.name}: {' '.join(w.cmd)[:160]}")
    w.start()
    ctx.workload = w
    t0 = time.time()
    w.wait_up(t0 + w.up_timeout)
    ctx.line(f"{w.name} answering{f' on {w.url}' if w.url else ''} after {time.time() - t0:.0f}s")


def limit(ctx: Context) -> float:
    wl = ctx.boot.get("workload") or {}
    cfg = wl.get("comfyui") if isinstance(wl.get("comfyui"), dict) else wl
    up = float((cfg or {}).get("up_timeout_s") or 300)
    return ctx.limit("workload", up + 60)
