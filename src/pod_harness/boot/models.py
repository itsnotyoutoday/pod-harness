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

## Two sources for one file

A manifest entry may carry BOTH `url` (the store's presigned GET) and `source` (the file's public
home, e.g. hf://…@<rev>), with the same sha256. The pod then downloads it from both at once: aria2c
splits the file into chunks, takes them from whichever source is faster at that moment, and carries
on from the other if one is slow or down (fetch.aria2c_download). The bytes are verified against the
sha256, so a mirror that serves anything else is refused. A source that needs a token (HF_TOKEN) is
never mixed with the store's link: a token for one host must not be sent to another.

## When the store does not have a file (TASK-API §10)

A manifest entry may come as {path, in_store: false, source, put_url, sha256?, size?}
instead of {path, url}: the app's store lacks the file, and the pod is the fallback. The
file is downloaded straight from `source` (https://, hf://<owner>/<repo>/<file>@<rev> with
$HF_TOKEN, civitai://<version id> with $CIVITAI_TOKEN — fetch.resolve_source), resumable and
checked like any other, and used at once. By default the pod writes nothing to the store: a `put_url` is ignored
(seeding is a separate process, not the pod runner's job). Only a request that asks for it (fetch.upload_back=true, or upload_back on the file) gets the file
copied back, in the BACKGROUND: one upload at a time, at low priority, only once the pod is ready, never while it
drains or parks, and never holding up ready or a task. Each upload ends in a boot line. Paths the manifest
lists under `missing` (not in the store and no source) fail: at boot they fail the boot.

The worker can also ask for models directly, through the local POST /models/fetch
(`ensure`): a task held until its models are on the pod names them, with links when the
claim carried them, else by path (the manifest is asked again for those). The same
download, verify and report path runs; the worker watches GET /state for the outcome.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .. import dl, fetch, heartbeat
from .core import BootFailure, Context, inside, say

PARALLEL = max(1, int(os.environ.get("PODH_FETCH_PARALLEL") or 2))   # the default; a job's fetch.parallel_files wins
ATTEMPTS = 4


class Manifest:
    """The current manifest, re-fetched on demand (fresh links), one refresh at a time."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.rev = 0
        self.items: dict = {}
        self.missing: list = []           # paths not in the store, with no source
        self.fetched_at = 0.0
        self.job = self._job(None)        # the job's `fetch` settings (dl.job_settings), re-read on refresh

    def _job(self, from_manifest) -> dict:
        spec = self.ctx.boot.get("models")
        try:
            return dl.job_settings(spec.get("fetch") if isinstance(spec, dict) else None, from_manifest)
        except dl.OptionError as e:
            raise BootFailure(f"models: fetch settings: {e}", stage="models")

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
                if not isinstance(m, dict) or not isinstance(m.get("path"), str) or not (
                        m.get("url") or m.get("source") or m.get("sources")):
                    raise BootFailure(f"models manifest: bad item {_redact(m)!r}", stage="models")
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
            self.job = self._job(obj.get("fetch") if isinstance(obj, dict) else None)
            for m in items.values():                       # a bad per-file option fails the boot early
                try:
                    dl.plan_for(m, self.job)
                except dl.OptionError as e:
                    raise BootFailure(f"models: {e}", stage="models")
            miss = obj.get("missing") if isinstance(obj, dict) else None
            self.missing = [p for p in (miss or []) if isinstance(p, str)]
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


def _redact(m) -> dict:
    """An item for a message: no signed URLs."""
    if not isinstance(m, dict):
        return m
    return {k: (str(v).split("?", 1)[0] + "?…" if k in ("url", "put_url") and "?" in str(v) else v)
            for k, v in m.items()}


def _job_of(man) -> dict:
    """The job's fetch settings from a manifest (the defaults for a bare stand-in)."""
    j = getattr(man, "job", None)
    return j if isinstance(j, dict) else dl.job_settings()


def _download(ctx: Context, man: Optional["Manifest"], path: str, m: dict, urls, dest: Path,
              deadline: Optional[float], line, headers=None) -> dict:
    """One file through the downloader its plan names. Returns the stats dict."""
    job = _job_of(man)
    plan = dl.plan_for(m, job)
    prog = dl.Progress(path, m.get("size"), line)
    return dl.download(urls, dest, plan, size=m.get("size"), sha256=m.get("sha256"), deadline=deadline,
                       headers=headers, parallel_files=job["parallel_files"],
                       budget=job["max_total_connections"], progress=prog, log=line)


def _stats_line(path: str, st: dict) -> str:
    return (f"{path}: {st['bytes'] / 1e6:.0f} MB in {st['secs']:.0f}s ({st['mbps']} MB/s) via {st['method']}, "
            f"{st['sources']} source(s) x {st['connections']} connection(s)")


def from_source(ctx: Context, path: str, m: dict, deadline: Optional[float], line,
                man: Optional["Manifest"] = None) -> Optional[str]:
    """The §10 fallback: the store lacks `path`; fetch it from its source, then queue the
    upload back to the store. None on success, else the error."""
    srcs = _entries(m)
    try:
        resolved = [r for r in (_resolve(x) for x in srcs) if r is not None]
    except ValueError as e:
        return str(e)
    if not resolved:
        return "no source"
    public = [u for u, h in resolved if not h]
    if len(public) > 1 and not m.get("sha256"):
        public = public[:1]                  # nothing proves two copies equal: one source
    url, headers = (public, None) if public else (resolved[0][0], resolved[0][1])
    scheme = (srcs[0] if isinstance(srcs[0], str) else str((srcs[0] or {}).get("url") or "")).split(":", 1)[0]
    line(f"{path}: not in the store — fetching it from its source ({scheme})")
    t0 = time.time()
    try:
        dest = inside(ctx.models_root, path, "model path")
        st = _download(ctx, man, path, m, url, dest, deadline, line, headers=headers or None)
        line(_stats_line(path, st))
    except fetch.LinkExpired as e:
        hint = {"hf": " (is HF_TOKEN set and allowed this repo?)",
                "civitai": " (is CIVITAI_TOKEN set?)"}.get(scheme, "")
        return f"source refused: {e}{hint}"
    except fetch.DownloadError as e:
        return f"from source: {e}"
    except (OSError, ValueError) as e:
        return f"from source: {type(e).__name__}: {e}"
    _remember(ctx, path, m.get("sha256"))
    size = dest.stat().st_size
    ctx.count(size)
    took = f"{size / 1e6:.0f} MB in {time.time() - t0:.0f}s"
    up = m["upload_back"] if isinstance(m.get("upload_back"), bool) else _job_of(man)["upload_back"]
    if m.get("put_url") and up:
        line(f"{path}: fetched from source ({took}); upload_back was asked for, so it is copied to the store in the background")
        uploader(ctx).add(path, dest, m["put_url"])
    else:
        line(f"{path}: fetched from source ({took}); nothing is written back to the store")
    return None


class StoreUploader:
    """Puts files fetched from a source back into the store: one at a time, low priority,
    after ready, never while the pod drains or parks. Never raises into the boot."""

    ATTEMPTS = 3

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.q: list = []
        self.cv = threading.Condition()
        self.done: dict = {}              # path -> "uploaded" | "skipped: …" | "failed: …"
        self.thread: Optional[threading.Thread] = None

    def add(self, path: str, dest: Path, put_url: str) -> None:
        with self.cv:
            if path in self.done or any(p == path for p, _, _ in self.q):
                return
            self.q.append((path, Path(dest), put_url))
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True,
                                               name="podh-store-upload")
                self.thread.start()
            self.cv.notify_all()

    def idle(self) -> bool:
        with self.cv:
            return not self.q and not getattr(self, "_busy", False)

    def _draining(self) -> bool:
        r = {**heartbeat.last_reply(), **(self.ctx.state.reply or {})}
        return bool(r.get("drain") or r.get("park"))

    def _line(self, text: str) -> None:
        try:
            self.ctx.line(text, "models")
        except Exception:
            pass

    def _loop(self) -> None:
        try:                              # low priority: this thread only (Linux)
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except (AttributeError, OSError, PermissionError):
            pass
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait(5)
                path, dest, url = self.q[0]
                self._busy = True
            try:
                self._one(path, dest, url)
            finally:
                with self.cv:
                    self.q.pop(0)
                    self._busy = False

    def _one(self, path: str, dest: Path, url: str) -> None:
        ctx = self.ctx
        # Not before ready: the boot's own downloads come first.
        while not ctx.state.ready and not ctx.finished.is_set():
            time.sleep(1)
        if ctx.finished.is_set() or self._draining():
            why = "the pod is ending" if ctx.finished.is_set() else "the pod is draining"
            self.done[path] = f"skipped: {why}"
            self._line(f"{path}: upload back to the store skipped ({why})")
            return
        t0 = time.time()
        err = "no answer"
        for i in range(self.ATTEMPTS):
            try:
                st = fetch.put_file(url, dest, timeout=3600)
            except Exception as e:
                st, err = 0, f"{type(e).__name__}: {e}"
            if 200 <= st < 300:
                self.done[path] = "uploaded"
                self._line(f"{path}: uploaded back to the store "
                           f"({dest.stat().st_size / 1e6:.0f} MB in {time.time() - t0:.0f}s)")
                return
            if st:
                err = f"HTTP {st}"
            if st in (401, 403):
                break                     # an expired put_url does not come back
            if self._draining():
                break
            time.sleep(min(2 ** i, 10))
        self.done[path] = f"failed: {err}"
        self._line(f"{path}: upload back to the store FAILED ({err}); the pod keeps the file")


_UPLOADER_LOCK = threading.Lock()


def uploader(ctx: Context) -> StoreUploader:
    with _UPLOADER_LOCK:
        u = getattr(ctx, "store_uploader", None)
        if u is None:
            u = ctx.store_uploader = StoreUploader(ctx)
        return u


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict = {}                      # path -> [Event, error], one download per path


def _entries(m: dict) -> list:
    """The raw source entries: `sources` (a list), else the one `source`. Each is a string
    ("https://…", "hf://…", "civitai://…") or an object: {"store": true} marks where the store's own
    link goes in the order; {"url": "…", "auth_env": "NAME"} is a source that needs a token."""
    v = m.get("sources")
    if isinstance(v, list) and v:
        return list(v)
    return [m["source"]] if m.get("source") else []


def _source_list(m: dict) -> list:
    """The plain-string sources of an entry, once each."""
    return [x for x in dict.fromkeys(e for e in _entries(m) if isinstance(e, str))]


def _resolve(e) -> Optional[tuple]:
    """(url, headers) for one entry; None for the store marker; ValueError if it makes no sense."""
    if isinstance(e, str):
        return fetch.resolve_source(e)
    if isinstance(e, dict):
        if e.get("store") is True:
            return None
        if isinstance(e.get("url"), str):
            url, headers = fetch.resolve_source(e["url"])
            env = e.get("auth_env")
            if env:
                tok = os.environ.get(str(env))
                if not tok:
                    raise ValueError(f"source {url.split('?', 1)[0]}: ${env} is not set on this pod")
                headers = {**headers, "Authorization": f"Bearer {tok}"}
            return url, headers
    raise ValueError(f"source {e!r}: not a URL string, {{\"store\": true}} or {{\"url\": …}}")


def _public_urls(m: dict) -> list:
    """The URLs of an entry's sources that need no token, in the order given (the store marker is
    skipped). A source that carries a token is left out: it would be sent to the other mirrors' hosts
    too."""
    out = []
    for e in _entries(m):
        try:
            r = _resolve(e)
        except ValueError:
            continue
        if r and not r[1]:
            out.append(r[0])
    return list(dict.fromkeys(out))


def _mirrors(m: dict) -> list:
    """The URLs that serve this model's bytes, in the order given: the public sources, and the store's
    link where {"store": true} sits (last when there is no marker). One list, one file: the downloader
    takes chunks from all of them at once. Without a sha256 nothing proves two copies equal, so then the
    store's link goes alone."""
    if not m.get("sha256"):
        return [m["url"]]
    ordered: list = []
    for e in _entries(m):
        try:
            r = _resolve(e)
        except ValueError:
            continue
        if r is None:
            ordered.append(None)
        elif not r[1]:
            ordered.append(r[0])
    ordered = list(dict.fromkeys(ordered))
    pub = [u for u in ordered if u]
    if os.environ.get("PODH_SKIP_STORE") == "1" and pub:
        return pub                    # a measurement knob: the public sources alone, the store's link unused
    if None not in ordered:
        ordered.append(None)
    return [m["url"] if u is None else u for u in ordered]


def fetch_one(ctx: Context, man: Manifest, path: str, deadline: Optional[float],
              line) -> Optional[str]:
    """Download one path; None on success, else the error. Never raises.

    One download per path at a time: the boot, the models_rev watcher and the worker's
    POST /models/fetch can all ask for the same file at once. A second caller waits for the
    first and takes its outcome — two writers on one .part would race on the rename (the
    loser saw FileNotFoundError and reported a file that was in fact present as failed).
    """
    key = (str(ctx.models_root), path)
    with _INFLIGHT_LOCK:
        slot = _INFLIGHT.get(key)
        mine = slot is None
        if mine:
            slot = _INFLIGHT[key] = [threading.Event(), None]
    if not mine:
        slot[0].wait()
        return slot[1]
    try:
        slot[1] = _fetch_one(ctx, man, path, deadline, line)
        return slot[1]
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
        slot[0].set()


def _fetch_one(ctx: Context, man: Manifest, path: str, deadline: Optional[float],
               line) -> Optional[str]:
    m = man.items.get(path)
    try:
        if m and present(ctx, m):
            report(ctx, path, "present", man.rev)
            return None
        report(ctx, path, "fetching", man.rev)
        if m and not m.get("url") and (m.get("source") or m.get("sources")):
            err = from_source(ctx, path, m, deadline, line, man)
            report(ctx, path, "failed" if err else "present", man.rev, err)
            return err
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
            urls = _mirrors(m)
            try:
                dest = inside(ctx.models_root, path, "model path")
                st = _download(ctx, man, path, m, urls, dest, deadline, line)
                _remember(ctx, path, m.get("sha256"))
                ctx.count(st["bytes"])
                line(_stats_line(path, st))
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
    par = max(1, int(_job_of(man)["parallel_files"]))
    with ThreadPoolExecutor(max_workers=par, thread_name_prefix="podh-model") as ex:
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
    if man.missing:
        for p in man.missing:
            report(ctx, p, "failed", man.rev, "not in the store, and no source to fetch it from")
        raise BootFailure(f"{len(man.missing)} model(s) not in the store and with no source: "
                          f"{', '.join(man.missing)[:200]}", stage="models")
    if not todo:
        ctx.line(f"all {len(items) - len(later)} boot model(s) present"
                 + (f"; {len(later)} addition(s) after ready" if later else ""))
        return
    todo = dl.order_paths(items, todo, man.job)
    short = dl.preflight(ctx.models_root, items, todo)
    if short:
        raise BootFailure(short, stage="models")
    j = man.job
    conns = dl.normalize_options(dl.env_layer(), j["options"])["connections_per_source"]
    ctx.line(f"downloading {len(todo)} of {len(items)} model(s): {j['method']}, {conns} connection(s) per source, "
             f"{j['parallel_files']} file(s) at once, {j['order'].replace('_', ' ')}; "
             f"models on {dl.fs_type(ctx.models_root) or 'unknown fs'}")
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
        for p in man.missing:
            if ctx.state.models.get(p) != "failed":
                report(ctx, p, "failed", man.rev, "not in the store, and no source to fetch it from")
        if todo:
            line(f"models_rev {man.rev}: fetching {len(todo)} new model(s)")
        failed = fetch_all(ctx, man, todo, time.time() + self.deadline_s, line)
        for p, e in failed.items():
            line(f"{p}: FAILED (pod keeps working): {e}")
        ctx.models_rev = max(ctx.models_rev, man.rev)
        return failed


def ensure(ctx: Context, files: list, paths: list, deadline_s: float = 6 * 3600) -> tuple:
    """(status, body) for the local POST /models/fetch: fetch these now, in the background.

    `files` carry their own links ({path, url, sha256?, size?}, or {path, source, put_url?,
    sha256?, size?} for one the store lacks) and are merged into the manifest; bare `paths` are looked up in a fresh manifest. Paths already present are
    reported present at once. Each path's state lands in ctx.state.models.
    """
    want: list = []
    given: dict = {}
    for m in files:
        if not isinstance(m, dict) or not isinstance(m.get("path"), str) or not (
                m.get("url") or m.get("source") or m.get("sources")):
            return 422, {"error": f"bad file {_redact(m)!r}: needs path and url (or source[s])"}
        if not m.get("url"):
            try:
                for src in _entries(m):
                    _resolve(src)
            except ValueError as e:
                return 422, {"error": str(e)}
        given[m["path"]] = m
    for p in list(given) + list(paths):
        try:
            inside(ctx.models_root, p, "model path")
        except ValueError as e:
            return 422, {"error": str(e)}
        if p not in want:
            want.append(p)
    if not want:
        return 202, {"fetching": []}
    ctx.models_root.mkdir(parents=True, exist_ok=True)
    man = getattr(ctx, "manifest", None) or Manifest(ctx)
    ctx.manifest = man
    for p in want:
        ctx.state.set_model(p, "fetching")

    def work():
        try:
            if any(p not in given and p not in man.items for p in want):
                try:
                    man.refresh()
                except BootFailure as e:
                    say(f"models: manifest for a fetch request failed: {e.reason}")
            with man.lock:
                man.items.update(given)
            todo = []
            for p in want:
                m = man.items.get(p)
                if m and present(ctx, m):
                    report(ctx, p, "present", man.rev)
                elif m is None and (ctx.models_root / p).is_file():
                    report(ctx, p, "present", man.rev)
                else:
                    todo.append(p)

            def line(t):
                ctx.line(t, "models")
            if todo:
                line(f"fetching {len(todo)} model(s) for the worker")
            fetch_all(ctx, man, todo, time.time() + deadline_s, line)
        except Exception as e:             # never leave a path "fetching" forever
            for p in want:
                if ctx.state.models.get(p) == "fetching":
                    report(ctx, p, "failed", man.rev, f"{type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True, name="podh-models-ensure").start()
    return 202, {"fetching": want}
