"""models: the boot set before ready, and additions while the pod works.

GET /v1/pod/models answers {"models_rev": n, "files": [{path, url, sha256?, size?, boot?}]}
— presigned GETs for the job's paths (`models`/`items` and a bare list are read too). Each lands at $PODH_MODELS_ROOT/<path>, two at a time,
through fetch.download (aria2c when present; ranged and resumable either way). A refused
link (expired) re-asks for the manifest and resumes the same .part with the fresh URL. Size
and sha256 are verified when the manifest gives them; a verified sha is remembered so a
resumed pod does not hash 40 GB again to learn what it already knew.

Each path's state goes to POST /v1/pod/models/state {"models": {path: state |
{"state", "error"}}} — "fetching", then "present" or "failed" — and into the local /state.

Only the boot set can fail the boot: a file marked `"boot": false` (added mid-life, and
listed when a parked pod resumes) is fetched without holding up ready. After ready, a watcher follows `models_rev` on the
heartbeat reply; a higher one fetches the manifest again and downloads what is new, while
the workload keeps running. A failed addition is reported and never ends the pod.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .. import fetch, heartbeat
from .core import BootFailure, Context, inside, say

PARALLEL = 2
ATTEMPTS = 4


class Manifest:
    """The current manifest, re-fetched on demand (fresh links), one refresh at a time."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.rev = 0
        self.items: dict = {}
        self.fetched_at = 0.0

    def refresh(self, stale: Optional[tuple] = None) -> dict:
        """Fetch the manifest. With `stale=(path, url)`, only if that path still has that
        url — two downloads refused at once cause one refresh, not two."""
        with self.lock:
            if stale and self.items.get(stale[0], {}).get("url") not in (None, stale[1]):
                return dict(self.items)
            st, obj = heartbeat.request("GET", "/v1/pod/models", timeout=30)
            if st != 200 or obj is None:
                raise BootFailure(f"models manifest: control plane answered {st or 'nothing'} "
                                  f"({heartbeat.last_error() or obj})", stage="models")
            raw = obj if isinstance(obj, list) else (
                obj.get("files") or obj.get("models") or obj.get("items") or [])
            if not isinstance(raw, list):
                raise BootFailure("models manifest: `files` is not a list", stage="models")
            items = {}
            for m in raw:
                if not isinstance(m, dict) or not isinstance(m.get("path"), str) or not m.get("url"):
                    raise BootFailure(f"models manifest: bad item {m!r}", stage="models")
                try:
                    inside(self.ctx.models_root, m["path"], "model path")
                except ValueError as e:
                    raise BootFailure(f"models manifest: {e}", stage="models")
                items[m["path"]] = m
            if isinstance(obj, dict) and obj.get("models_rev") is not None:
                try:
                    self.rev = int(obj["models_rev"])
                except (TypeError, ValueError):
                    pass
            self.items, self.fetched_at = items, time.time()
            return dict(items)


def _verified_path(ctx: Context) -> Path:
    return ctx.boot_dir / "models-verified.json"


_VLOCK = threading.Lock()


def _verified(ctx: Context) -> dict:
    try:
        return json.loads(_verified_path(ctx).read_text())
    except (OSError, ValueError):
        return {}


def _remember(ctx: Context, path: str, sha: Optional[str]) -> None:
    if not sha:
        return
    with _VLOCK:
        v = _verified(ctx)
        v[path] = sha.lower()
        p = _verified_path(ctx)
        tmp = p.with_name(p.name + f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(v, indent=1, sort_keys=True))
        os.replace(tmp, p)


def present(ctx: Context, m: dict) -> bool:
    dest = ctx.models_root / m["path"]
    if not dest.is_file():
        return False
    if m.get("size") is not None and dest.stat().st_size != int(m["size"]):
        return False
    sha = (m.get("sha256") or "").lower()
    if not sha:
        return True
    if _verified(ctx).get(m["path"]) == sha:
        return True
    if fetch.sha256_file(dest) == sha:
        _remember(ctx, m["path"], sha)
        return True
    return False


def report(ctx: Context, path: str, state: str, rev: int, error: Optional[str] = None) -> None:
    ctx.state.set_model(path, state)
    if heartbeat._cfg()[0]:
        v = {"state": state, "error": error[:300]} if error else state
        heartbeat.request("POST", "/v1/pod/models/state", {"models": {path: v}}, timeout=5)


def fetch_one(ctx: Context, man: Manifest, path: str, deadline: Optional[float],
              line) -> Optional[str]:
    """Download one path; None on success, else the error. Never raises."""
    m = man.items.get(path)
    try:
        if m and present(ctx, m):
            report(ctx, path, "present", man.rev)
            return None
        report(ctx, path, "fetching", man.rev)
        err = "not in the manifest"
        for attempt in range(ATTEMPTS):
            if attempt:
                try:
                    m = man.refresh(stale=(path, (m or {}).get("url"))).get(path)
                except BootFailure as e:
                    err = e.reason
                    continue
            if not m:
                err = "not in the manifest"
                break
            t0 = time.time()
            try:
                dest = inside(ctx.models_root, path, "model path")
                fetch.download(m["url"], dest, size=m.get("size"), sha256=m.get("sha256"),
                               deadline=deadline)
                _remember(ctx, path, m.get("sha256"))
                size = dest.stat().st_size
                line(f"{path}: {size / 1e6:.0f} MB in {time.time() - t0:.0f}s")
                report(ctx, path, "present", man.rev)
                return None
            except fetch.LinkExpired as e:
                err = str(e)
                line(f"{path}: link refused, asking for fresh links")
            except fetch.DownloadError as e:
                err = str(e)
                if "timed out" in err:
                    break
            except (OSError, ValueError) as e:
                err = f"{type(e).__name__}: {e}"
                break
        report(ctx, path, "failed", man.rev, err)
        return err
    except Exception as e:                 # a model is never worth a crashed thread
        err = f"{type(e).__name__}: {e}"
        report(ctx, path, "failed", man.rev, err)
        return err


def fetch_all(ctx: Context, man: Manifest, paths: list, deadline: Optional[float],
              line) -> dict:
    """{path: error} for the ones that failed."""
    if not paths:
        return {}
    with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="podh-model") as ex:
        results = dict(zip(paths, ex.map(lambda p: fetch_one(ctx, man, p, deadline, line), paths)))
    return {p: e for p, e in results.items() if e}


def run(ctx: Context) -> None:
    spec = ctx.boot.get("models")
    if not spec:
        ctx.line("no models requested")
        return
    ctx.models_root.mkdir(parents=True, exist_ok=True)
    man = Manifest(ctx)
    ctx.manifest = man
    items = man.refresh()
    todo = [p for p, m in items.items() if not present(ctx, m)]
    for p in items:
        if p not in todo:
            report(ctx, p, "present", man.rev)
    later = [p for p in todo if items[p].get("boot") is False]
    todo = [p for p in todo if p not in later]
    ctx.models_rev = man.rev
    ctx.models_later = later              # the watcher fetches these once the pod is ready
    if not todo:
        ctx.line(f"all {len(items) - len(later)} boot model(s) present"
                 + (f"; {len(later)} addition(s) after ready" if later else ""))
        return
    ctx.line(f"downloading {len(todo)} of {len(items)} model(s)")
    t0 = time.time()
    failed = fetch_all(ctx, man, todo, ctx.deadline, ctx.line)
    if failed:
        first = next(iter(failed.items()))
        raise BootFailure(f"{len(failed)} model(s) failed; {first[0]}: {first[1]}", stage="models")
    ctx.line(f"models done in {time.time() - t0:.0f}s")


class Watcher:
    """After ready: fetch models added mid-life when the heartbeat reply's models_rev rises."""

    def __init__(self, ctx: Context, deadline_s: float = 6 * 3600):
        self.ctx = ctx
        self.ev = threading.Event()
        self.want = ctx.models_rev
        self.deadline_s = deadline_s
        self.stop = threading.Event()
        heartbeat.on_reply(self.on_reply)

    def on_reply(self, reply: dict) -> None:
        try:
            rev = int(reply.get("models_rev") or 0)
        except (TypeError, ValueError):
            return
        if rev > self.want:
            self.want = rev
            self.ev.set()

    def start(self) -> "Watcher":
        if getattr(self.ctx, "models_later", None):
            self.ev.set()
        threading.Thread(target=self.loop, daemon=True, name="podh-models-watch").start()
        return self

    def loop(self) -> None:
        while not self.stop.is_set() and not self.ctx.finished.is_set():
            if not self.ev.wait(5):
                continue
            self.ev.clear()
            self.once()

    def once(self) -> dict:
        ctx = self.ctx
        man = getattr(ctx, "manifest", None) or Manifest(ctx)
        ctx.manifest = man

        def line(t):
            ctx.line(t, "models")
        try:
            items = man.refresh()
        except BootFailure as e:
            say(f"models: mid-life manifest failed: {e.reason}")
            return {"manifest": e.reason}
        todo = [p for p, m in items.items() if not present(ctx, m)]
        for p in items:
            if p not in todo and ctx.state.models.get(p) != "present":
                report(ctx, p, "present", man.rev)
        if todo:
            line(f"models_rev {man.rev}: fetching {len(todo)} new model(s)")
        failed = fetch_all(ctx, man, todo, time.time() + self.deadline_s, line)
        for p, e in failed.items():
            line(f"{p}: FAILED (pod keeps working): {e}")
        ctx.models_rev = max(ctx.models_rev, man.rev)
        return failed
