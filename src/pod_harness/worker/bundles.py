"""Prebuilt bundles (pod-control TASK-API §12): fetch, verify, extract, start. Nothing is installed.

A bundle is an environment built ahead of time against one image digest and stored as a
tarball with its sha256. The claim lists the ones a task needs (`task["bundles"]`, each with a
presigned `url`); before the handler runs, for each one:

1. refuse it when its `image_digest` is not this pod's image (the task fails, not retryable:
   another pod of the same image would refuse it too);
2. use `<cache>/<name>/<sha256>/` when it is there and its `bundle.json` checks out (a parked
   pod keeps its cache);
3. else download it, verify the sha256 (a mismatch deletes the download and fails the task),
   extract it into a staging dir and move that into place in one rename;
4. start it:
   - `comfy_nodes`: link each `custom_nodes/<dir>` into ComfyUI's custom_nodes, list the
     bundle's `site/` in ComfyUI's python as a .pth (after the image's own site-packages, so a
     bundle adds libraries and never shadows the image's torch), then restart ComfyUI once for
     all of the task's new node bundles;
   - `service`: run `entry` (default `bin/serve`) with PORT, MODELS_DIR and BUNDLE_DIR set and wait
     for `health` to answer 200. A service that died or stopped answering is started again
     before the next task that needs it.
5. report, in the task's usage, `bundles: [{name, fresh, bytes, pull_s, start_s, sha256}]`.

Layout of a bundle (the builder's side):

    bundle.json        {name, kind, image_digest, built_at, python, cuda, torch, files: {path: sha256}}
    custom_nodes/<d>/  comfy_nodes: the node folders
    site/              comfy_nodes: their libraries (pip --target, nothing the image already has)
    venv/              service: its own venv; bin/serve runs "$BUNDLE_DIR/venv/bin/python -m …"
    bin/serve          service: starts it on $PORT
    bin/probe          optional: a quick smoke test, run after extracting (exit 0 = good)

The pod lists what it holds as `bundle:<name>` in its claims' `have.loaded`, so pod-control's
affinity sends tasks that need a bundle to a pod that already has it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .. import fetch
from ..boot import image as image_id
from .handlers import TaskFailed

KINDS = ("comfy_nodes", "service")
DEFAULT_CACHE = "/workspace/bundles"
MANIFEST = "bundle.json"
PTH_NAME = "podh-bundles.pth"


def _digest(s) -> str:
    s = str(s or "").strip().lower()
    return s.rsplit("@", 1)[1] if "@" in s else s


class Bundles:
    """One per worker. `ensure(task_bundles, log)` makes a task's bundles ready and returns the
    usage list; `loaded()` is what the pod holds, as `bundle:<name>` entries."""

    def __init__(self, *, cache_dir=None, image_digest: Optional[str] = None,
                 comfy_root=None, comfy_python: Optional[str] = None,
                 restart_comfy: Optional[Callable[[], tuple]] = None,
                 models_root=None, log: Callable[[str], None] = print,
                 health_timeout_s: float = 300.0, download=None,
                 others_running: Optional[Callable[[], bool]] = None, comfy_site=None):
        self.cache = Path(cache_dir or os.environ.get("PODH_BUNDLES_DIR") or DEFAULT_CACHE)
        if image_digest is None:
            image_digest = image_id.identity().get("image_digest") or ""
        self.image_digest = _digest(image_digest)
        self.comfy_root = Path(comfy_root or os.environ.get("PODH_COMFY_ROOT") or "/opt/ComfyUI")
        self.comfy_python = comfy_python or os.environ.get("PODH_COMFY_PY") or "python3"
        self.restart_comfy = restart_comfy
        self.models_root = str(models_root or os.environ.get("PODH_MODELS_ROOT")
                               or Path(os.environ.get("PODH_WORKSPACE") or "/workspace") / "models")
        self.log = log
        self.health_timeout_s = float(health_timeout_s)
        self.download = download or fetch.download
        self.others_running = others_running or (lambda: False)
        self.comfy_site = Path(comfy_site) if comfy_site else None   # tests; else asked of python
        self.lock = threading.RLock()
        self.linked: dict = {}         # name -> sha256 of the node bundle linked into ComfyUI
        self.services: dict = {}       # name -> {"proc", "sha256", "port", "health"}
        self.held: dict = {}           # name -> sha256 extracted in the cache
        self._scan()

    # -- what the pod holds ------------------------------------------------------------------
    def _scan(self) -> None:
        """What the cache already holds for this image (a resumed or parked pod keeps it)."""
        try:
            names = [p for p in self.cache.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            return
        for d in names:
            for s in d.iterdir():
                try:
                    m = json.loads((s / MANIFEST).read_text())
                except (OSError, ValueError):
                    continue
                if (isinstance(m, dict) and m.get("name") == d.name and m.get("kind") in KINDS
                        and _digest(m.get("image_digest")) == self.image_digest):
                    self.held[d.name] = s.name

    def loaded(self) -> list:
        with self.lock:
            return [f"bundle:{n}" for n in sorted(self.held)]

    # -- one task ----------------------------------------------------------------------------
    def ensure(self, items, log: Optional[Callable[[str], None]] = None) -> list:
        """Make every bundle in `items` ready. Raises TaskFailed(stage="bundles")."""
        log = log or self.log
        if not items:
            return []
        if not isinstance(items, list):
            raise TaskFailed("the claim's bundles is not a list", "bundles", retryable=False)
        usage, restart = [], []
        with self.lock:
            for b in items:
                u = self._one(b, log)
                usage.append(u)
                if b.get("kind") == "comfy_nodes" and u.pop("_link_changed", False):
                    restart.append(u)
            if restart:
                if self.others_running():
                    # A restart would kill the other task mid-run; this one goes back to the
                    # queue and comes back once the pod is free (its bundle stays cached).
                    raise TaskFailed("bundles: ComfyUI must restart for node bundles while another "
                                     "task runs on this pod", "bundles", retryable=True)
                t0 = time.time()
                self._write_pth()
                self._restart(log)
                share = (time.time() - t0) / len(restart)
                for u in restart:
                    u["start_s"] = round(u["start_s"] + share, 3)
            for b, u in zip(items, usage):
                if b.get("kind") == "service":
                    t0 = time.time()
                    self._service(b, log)
                    u["start_s"] = round(u["start_s"] + time.time() - t0, 3)
        return usage

    def _one(self, b: dict, log) -> dict:
        if not isinstance(b, dict):
            raise TaskFailed("a bundle entry is not an object", "bundles", retryable=False)
        name, kind, sha = str(b.get("name") or ""), b.get("kind"), str(b.get("sha256") or "").lower()
        if not name or "/" in name or name.startswith(".") or kind not in KINDS or len(sha) != 64:
            raise TaskFailed(f"bundle {name!r}: needs a name, a kind in {KINDS} and a sha256",
                             "bundles", retryable=False)
        want = _digest(b.get("image_digest"))
        if not self.image_digest:
            raise TaskFailed(f"bundle {name!r}: this pod does not know its image digest "
                             "(PODH_IMAGE_DIGEST or the image manifest), so it cannot match a bundle",
                             "bundles", retryable=False)
        if want != self.image_digest:
            raise TaskFailed(f"bundle {name!r} was built for another image ({want[:19]}…); this pod "
                             f"runs {self.image_digest[:19]}…: rebuild the bundle", "bundles",
                             retryable=False)
        dest = self.cache / name / sha
        t0 = time.time()
        fresh = not self._valid(dest, name, kind)
        nbytes = int(b.get("size_bytes") or 0)
        if fresh:
            url = b.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise TaskFailed(f"bundle {name!r}: the claim gave no url", "bundles", retryable=True)
            log(f"[bundles] {name}: fetching {nbytes / 1e9:.2f} GB")
            self._fetch(url, dest, sha, nbytes or None, name, kind)
        else:
            log(f"[bundles] {name}: cached")
        self.held[name] = sha
        pull_s = time.time() - t0
        t1 = time.time()
        changed = False
        if kind == "comfy_nodes":
            changed = self._link(name, sha, dest, log)
        start_s = time.time() - t1
        u = {"name": name, "fresh": fresh, "bytes": nbytes, "pull_s": round(pull_s, 3),
             "start_s": round(start_s, 3), "sha256": sha}
        if changed:
            u["_link_changed"] = True
        return u

    def _valid(self, dest: Path, name: str, kind: str) -> bool:
        try:
            m = json.loads((dest / MANIFEST).read_text())
        except (OSError, ValueError):
            return False
        return (isinstance(m, dict) and m.get("name") == name and m.get("kind") == kind
                and _digest(m.get("image_digest")) == self.image_digest)

    def _fetch(self, url: str, dest: Path, sha: str, size, name: str, kind: str) -> None:
        tmp = self.cache / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        part = tmp / f"{name}-{sha[:12]}.tar.zst"
        stage = tmp / f"{name}-{sha[:12]}.d"
        shutil.rmtree(stage, ignore_errors=True)
        try:
            try:
                self.download(url, part, size=size, sha256=sha)
            except fetch.DownloadError as e:
                part.unlink(missing_ok=True)
                bad = "sha256" in str(e).lower() or "!= expected" in str(e)
                raise TaskFailed(f"bundle {name!r}: {e}", "bundles", retryable=not bad)
            try:
                fetch.safe_extract(part, stage)
            except Exception as e:
                raise TaskFailed(f"bundle {name!r}: extract failed: {e}", "bundles", retryable=False)
            if not self._valid(stage, name, kind):
                raise TaskFailed(f"bundle {name!r}: its {MANIFEST} does not match (name, kind, image)",
                                 "bundles", retryable=False)
            probe = stage / "bin" / "probe"
            if probe.is_file():
                r = subprocess.run([str(probe)], cwd=str(stage), capture_output=True, text=True,
                                   timeout=120, env={**os.environ, "BUNDLE_DIR": str(dest)})
                if r.returncode != 0:
                    raise TaskFailed(f"bundle {name!r}: probe failed: {(r.stderr or r.stdout)[-300:]}",
                                     "bundles", retryable=False)
            dest.parent.mkdir(parents=True, exist_ok=True)
            fetch.replace_dir(stage, dest)
        finally:
            part.unlink(missing_ok=True)
            shutil.rmtree(stage, ignore_errors=True)

    # -- comfy_nodes ---------------------------------------------------------------------------
    def _link(self, name: str, sha: str, dest: Path, log) -> bool:
        """Link the bundle's node folders into ComfyUI. True when anything changed."""
        if self.linked.get(name) == sha:
            return False
        nodes = self.comfy_root / "custom_nodes"
        src = dest / "custom_nodes"
        if not src.is_dir():
            raise TaskFailed(f"bundle {name!r}: no custom_nodes/ in it", "bundles", retryable=False)
        nodes.mkdir(parents=True, exist_ok=True)
        for d in sorted(p for p in src.iterdir() if p.is_dir()):
            link = nodes / d.name
            if link.exists() and not link.is_symlink():
                raise TaskFailed(f"bundle {name!r}: ComfyUI already has a {d.name} that is not ours",
                                 "bundles", retryable=False)
            if link.is_symlink():
                link.unlink()
            link.symlink_to(d)
        self.linked[name] = sha
        log(f"[bundles] {name}: nodes linked")
        return True

    def _site_dirs(self) -> list:
        return [str(self.cache / n / s / "site") for n, s in sorted(self.linked.items())
                if (self.cache / n / s / "site").is_dir()]

    def _write_pth(self) -> None:
        """List the node bundles' site/ dirs in ComfyUI's python (a .pth: after site-packages)."""
        site = self.comfy_site
        if site is None:
            r = subprocess.run([self.comfy_python, "-c",
                                "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or not r.stdout.strip():
                raise TaskFailed(f"bundles: cannot find ComfyUI's site-packages: {r.stderr[-200:]}",
                                 "bundles", retryable=False)
            site = self.comfy_site = Path(r.stdout.strip())
        pth = site / PTH_NAME
        tmp = pth.with_suffix(".tmp")
        tmp.write_text("".join(f"{d}\n" for d in self._site_dirs()))
        os.replace(tmp, pth)

    def _restart(self, log) -> None:
        if self.restart_comfy is None:
            raise TaskFailed("bundles: ComfyUI must restart to load node bundles, and this pod "
                             "cannot restart it", "bundles", retryable=False)
        log("[bundles] restarting ComfyUI for node bundles")
        code, obj = self.restart_comfy()
        if not 200 <= int(code or 0) < 300:
            raise TaskFailed(f"bundles: ComfyUI restart answered {code}: {obj}", "bundles",
                             retryable=True)

    # -- service --------------------------------------------------------------------------------
    def _healthy(self, port: int, path: str, timeout: float = 3.0) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
                return 200 <= r.status < 300
        except Exception:
            return False

    def _service(self, b: dict, log) -> None:
        name, sha = b["name"], str(b["sha256"]).lower()
        sv = ((b.get("start") or {}).get("service")) or {}
        port, health = int(sv.get("port") or 0), str(sv.get("health") or "/")
        entry = str(sv.get("entry") or "bin/serve")
        if not port:
            raise TaskFailed(f"bundle {name!r}: a service needs start.service.port", "bundles",
                             retryable=False)
        cur = self.services.get(name)
        if cur and cur["sha256"] == sha and cur["proc"].poll() is None and self._healthy(port, health):
            return
        if cur:
            log(f"[bundles] {name}: service not answering, starting it again")
            self._stop(name)
        dest = self.cache / name / sha
        exe = (dest / entry).resolve()
        if not str(exe).startswith(str(dest.resolve()) + os.sep) or not exe.is_file():
            raise TaskFailed(f"bundle {name!r}: no {entry} in it", "bundles", retryable=False)
        logf = open(self.cache / f"{name}.log", "ab")
        env = {**os.environ, "PORT": str(port), "MODELS_DIR": self.models_root,
               "BUNDLE_DIR": str(dest)}
        proc = subprocess.Popen([str(exe)], cwd=str(dest), env=env, stdout=logf,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self.services[name] = {"proc": proc, "sha256": sha, "port": port, "health": health,
                               "log": logf}
        deadline = time.time() + self.health_timeout_s
        while time.time() < deadline:
            if proc.poll() is not None:
                raise TaskFailed(f"bundle {name!r}: the service exited ({proc.returncode}) "
                                 f"before answering {health}", "bundles", retryable=False)
            if self._healthy(port, health):
                log(f"[bundles] {name}: service up on {port}")
                return
            time.sleep(1.0)
        self._stop(name)
        raise TaskFailed(f"bundle {name!r}: {health} did not answer within "
                         f"{self.health_timeout_s:.0f} s", "bundles", retryable=True)

    def _stop(self, name: str) -> None:
        s = self.services.pop(name, None)
        if not s:
            return
        p = s["proc"]
        if p.poll() is None:
            try:
                os.killpg(p.pid, 15)
                p.wait(timeout=20)
            except Exception:
                try:
                    os.killpg(p.pid, 9)
                except Exception:
                    pass
        try:
            s["log"].close()
        except Exception:
            pass

    def close(self) -> None:
        with self.lock:
            for n in list(self.services):
                self._stop(n)
