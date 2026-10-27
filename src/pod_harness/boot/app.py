"""app: fetch the app's own code with its own token, unpack it, run its setup.

    "app": {"fetch": {"url": "https://…/bootstrap", "auth_env": "APP_POD_TOKEN"},
            "run": ["bash", "/opt/app/setup.sh"],          # setup; must exit 0
            "dir": "/opt/app",                               # default $PODH_APP_DIR or /opt/app
            "serve": [...], "serve_env": {...}, "serve_grace_s": 120}   # see serve.py

The fetch sends `Authorization: Bearer $<auth_env>` — the APP's token, never the pod's
control token. The answer is a tar (gzip/xz/zstd) or zip, unpacked member-checked into
`dir`, replacing what was there. `run` gets the boot's environment: PODH_COMFY_ROOT,
PODH_COMFY_PY, PODH_MODELS_ROOT, PODH_WORKLOAD_ENV, PODH_JOB_ID, PODH_GPU_COUNT,
PODH_LOCAL_URL/TOKEN and PODH_APP_DIR, plus what binaries wrote to the workload env. It runs
BEFORE the workload, so it may drop files into the workload tree and add workload env by
appending KEY=value lines to $PODH_WORKLOAD_ENV.

Done once per definition: a resumed pod whose app stage already succeeded with the same
fetch/run skips it (podh-boot --fresh redoes it).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .. import fetch
from .core import BootFailure, Context, as_cmd, digest

SETUP_TIMEOUT = 1800


def app_dir(ctx: Context) -> Path:
    app = ctx.boot.get("app") or {}
    return Path(app.get("dir") or os.environ.get("PODH_APP_DIR") or "/opt/app")


def run(ctx: Context) -> None:
    app = ctx.boot.get("app") or {}
    if not isinstance(app, dict):
        raise BootFailure("boot.app must be an object")
    d = app_dir(ctx)
    ctx.exports["PODH_APP_DIR"] = str(d)
    f, setup = app.get("fetch"), app.get("run")
    if not f and not setup:
        ctx.line("no app setup")
        return
    want = digest({"fetch": f, "run": setup, "dir": str(d)})
    if ctx.marker("app") == want:
        ctx.line("app already set up (resumed pod)")
        return
    if f:
        if not isinstance(f, dict) or not isinstance(f.get("url"), str):
            raise BootFailure("app.fetch needs a url")
        headers = {}
        if f.get("auth_env"):
            tok = os.environ.get(str(f["auth_env"]))
            if not tok:
                raise BootFailure(f"app.fetch: ${f['auth_env']} is not set on this pod")
            headers["Authorization"] = f"Bearer {tok}"
        d.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".{d.name}.podh-", dir=str(d.parent)))
        try:
            arc = work / "app.archive"
            ctx.line(f"fetching the app into {d}")
            try:
                fetch.http_download(f["url"], arc, headers=headers, deadline=ctx.deadline)
            except fetch.DownloadError as e:
                raise BootFailure(f"app fetch: {e}")
            if fetch.sniff(arc) == "raw":
                raise BootFailure("app fetch: the answer is not an archive (tar or zip)")
            staged = work / "tree"
            try:
                fetch.safe_extract(arc, staged)
            except (fetch.UnsafeArchive, OSError) as e:
                raise BootFailure(f"app fetch: {e}")
            fetch.replace_dir(staged, d)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if setup:
        try:
            cmd = as_cmd(setup)
        except ValueError as e:
            raise BootFailure(f"app.run: {e}")
        ctx.line(f"running app setup: {' '.join(cmd)[:120]}")
        log = ctx.boot_dir / "logs" / "app-setup.log"
        rc, out = ctx.run(cmd, timeout=SETUP_TIMEOUT, log=log,
                          cwd=str(d) if d.is_dir() else None)
        if rc != 0:
            raise BootFailure(f"app setup {'timed out' if rc is None else f'exited {rc}'}",
                              rc=rc or 1, log_tail=out)
    ctx.set_marker("app", want)
