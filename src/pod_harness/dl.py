"""The pod's model downloader: what to fetch, from where, and HOW — all of it data.

A job (or one file in it) says how to download; the image only has to hold the tools. Nothing
here needs a new image to change.

    "fetch": {                              # job level: the defaults for every file
        "method": "aria2",                  # aria2 | http | curl | exec
        "options": {"connections_per_source": 1, "min_split": "16M", ...},
        "parallel_files": 2,                # files downloaded at the same time
        "order": "largest_first",           # largest_first | smallest_first | given
        "max_total_connections": 48,        # a ceiling over files x sources x connections
        "verify": "sha256",                 # sha256 | size | none
        "upload_back": false                # true: copy source-fetched files into the store, in the background
                                            # (off unless the request says so; seeding is normally a separate process)
    }
    a file:  {"path", "sha256", "size", "sources": [...], "method"?, "options"?, "group"?, "priority"?,
              "upload_back"?}            # the request carries it: per job in `fetch`, or per file
    a source: "https://…" | "hf://owner/repo/file@rev" | "civitai://id" | {"store": true}
              | {"url": "…", "auth_env": "HF_TOKEN"}

Layering, lowest first: the harness defaults, the pod's environment (PODH_FETCH_CONNECTIONS,
PODH_FETCH_MIN_SPLIT, PODH_FETCH_ARIA2_ARGS), the job's `fetch`, the file's own keys. A list
(`extra_args`) replaces, it does not append. Unknown keys are refused, so a typo cannot silently
do nothing.

OPTIONS (method aria2 unless said)
    connections_per_source  1-16, default 1. Each source gets this many, so total = sources x this.
                            Measured: 16 per host was slower than 1 per source.
    split                   total connections for the file (default: sources x connections)
    min_split               chunk floor, e.g. "16M" (aria2 range 1M-1024M)
    file_allocation         none | prealloc | trunc | falloc (a network/FUSE disk is forced to none)
    uri_selector            adaptive | feedback | inorder
    retries, retry_wait     per connection
    lowest_speed            drop a connection slower than this, e.g. "1M"
    disk_cache              aria2's write cache, e.g. "64M"
    extra_args              more aria2c flags ("--name=value"); never the ones this layer owns
    argv                    method exec only: a command template, binary from the allowlist below
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import fetch

METHODS = ("aria2", "http", "curl", "exec")
ORDERS = ("largest_first", "smallest_first", "given")
VERIFY = ("sha256", "size", "none")
#: Binaries an `exec` method may run. PODH_FETCH_EXEC_ALLOW adds names (comma separated).
EXEC_ALLOW = {"aria2c", "curl", "wget", "rclone", "s5cmd", "axel"}
#: aria2c options this layer owns or that would let a job run commands or write elsewhere.
_DENY_ARIA = re.compile(
    r"^--(dir|out|input-file|conf-path|no-conf|log|log-level|rpc-.*|enable-rpc|daemon|save-session|"
    r"load-cookies|save-cookies|on-.*|checksum|stop-with-process|metalink-.*|bt-.*|follow-torrent|"
    r"continue|allow-overwrite|auto-file-renaming|user-agent|header)(=|$)")
_SIZE = re.compile(r"^\d+[KMG]?$")


class OptionError(ValueError):
    """A download option that is unknown, out of range or not allowed."""


def _int(lo: int, hi: int):
    def f(v, name):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise OptionError(f"{name}: a whole number {lo}-{hi}, not {v!r}")
        return v
    return f


def _enum(*vals):
    def f(v, name):
        if v not in vals:
            raise OptionError(f"{name}: one of {', '.join(vals)}, not {v!r}")
        return v
    return f


def _size(v, name):
    if isinstance(v, int) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str) or not _SIZE.match(v):
        raise OptionError(f"{name}: a size like 16M, not {v!r}")
    return v


def _opt_size(v, name):
    return None if v in (None, "", 0) else _size(v, name)


def _opt_int(lo, hi):
    chk = _int(lo, hi)
    return lambda v, name: None if v is None else chk(v, name)


def _extra_args(v, name):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise OptionError(f"{name}: a list of strings")
    for a in v:
        if not a.startswith("--") or _DENY_ARIA.match(a):
            raise OptionError(f"{name}: {a!r} is not allowed (flags must be --name[=value], and "
                              f"not one this downloader owns)")
    return list(v)


def _argv(v, name):
    if v is None:
        return None
    if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
        raise OptionError(f"{name}: a non-empty list of strings")
    allow = EXEC_ALLOW | {x.strip() for x in (os.environ.get("PODH_FETCH_EXEC_ALLOW") or "").split(",") if x.strip()}
    if os.path.basename(v[0]) not in allow:
        raise OptionError(f"{name}: {os.path.basename(v[0])!r} is not an allowed downloader "
                          f"({', '.join(sorted(allow))})")
    if "/" in v[0] and not v[0].startswith(("/usr/", "/opt/")):
        raise OptionError(f"{name}: the binary must be a bare name or live under /usr or /opt")
    return list(v)


SCHEMA = {
    "connections_per_source": _int(1, 16),
    "split": _opt_int(1, 512),
    "min_split": _size,
    "file_allocation": _enum("none", "prealloc", "trunc", "falloc"),
    "uri_selector": _enum("adaptive", "feedback", "inorder"),
    "retries": _int(0, 20),
    "retry_wait": _int(0, 60),
    "lowest_speed": _opt_size,
    "disk_cache": _opt_size,
    "extra_args": _extra_args,
    "argv": _argv,
}


def defaults() -> dict:
    return {"connections_per_source": 1, "split": None, "min_split": "16M", "file_allocation": "none",
            "uri_selector": "adaptive", "retries": 5, "retry_wait": 2, "lowest_speed": None,
            "disk_cache": None, "extra_args": [], "argv": None}


def env_layer() -> dict:
    """The pod's environment as an options layer (the lowest one above the defaults)."""
    out: dict = {}
    c = os.environ.get("PODH_FETCH_CONNECTIONS")
    if c:
        try:
            out["connections_per_source"] = max(1, min(16, int(c)))
        except ValueError:
            pass
    m = os.environ.get("PODH_FETCH_MIN_SPLIT")
    if m and _SIZE.match(m):
        out["min_split"] = m
    a = os.environ.get("PODH_FETCH_ARIA2_ARGS")
    if a:
        try:
            out["extra_args"] = shlex.split(a)
        except ValueError:
            pass
    return out


def normalize_options(*layers) -> dict:
    """The defaults, then each layer on top, every value checked."""
    out = defaults()
    for layer in layers:
        if not layer:
            continue
        if not isinstance(layer, dict):
            raise OptionError("options must be an object")
        for k, v in layer.items():
            if k not in SCHEMA:
                raise OptionError(f"unknown download option {k!r}")
            out[k] = SCHEMA[k](v, k)
    return out


JOB_KEYS = {"method", "options", "parallel_files", "order", "max_total_connections", "verify", "upload_back"}


def job_settings(*layers) -> dict:
    """The job-level `fetch` object, layers merged (later wins), every key checked."""
    out = {"method": "aria2", "options": {}, "parallel_files": int(os.environ.get("PODH_FETCH_PARALLEL") or 2),
           "order": "largest_first", "max_total_connections": 48, "verify": "sha256", "upload_back": False}
    for layer in layers:
        if not layer:
            continue
        if not isinstance(layer, dict):
            raise OptionError("fetch must be an object")
        for k, v in layer.items():
            if k not in JOB_KEYS:
                raise OptionError(f"unknown fetch setting {k!r}")
            if k == "options":
                if not isinstance(v, dict):
                    raise OptionError("fetch.options must be an object")
                out["options"] = {**out["options"], **v}
            else:
                out[k] = v
    if out["method"] not in METHODS:
        raise OptionError(f"fetch.method: one of {', '.join(METHODS)}, not {out['method']!r}")
    out["parallel_files"] = _int(1, 16)(out["parallel_files"], "fetch.parallel_files")
    out["max_total_connections"] = _int(1, 512)(out["max_total_connections"], "fetch.max_total_connections")
    if out["order"] not in ORDERS:
        raise OptionError(f"fetch.order: one of {', '.join(ORDERS)}")
    if out["verify"] not in VERIFY:
        raise OptionError(f"fetch.verify: one of {', '.join(VERIFY)}")
    if not isinstance(out["upload_back"], bool):
        raise OptionError("fetch.upload_back: true or false")
    normalize_options(out["options"])            # check the job's options now, not at the first file
    return out


class Plan:
    """How one file will be fetched, resolved: nothing left to decide at download time."""

    def __init__(self, path: str, method: str, options: dict, verify: str, group: str, priority: int):
        self.path, self.method, self.options = path, method, options
        self.verify, self.group, self.priority = verify, group, priority

    def describe(self) -> dict:
        return {"path": self.path, "method": self.method, "verify": self.verify, "group": self.group,
                "priority": self.priority, "options": {k: v for k, v in self.options.items() if v not in (None, [])}}


def plan_for(m: dict, job: dict) -> Plan:
    """Resolve one manifest entry against the job's settings."""
    method = m.get("method") or job["method"]
    if method not in METHODS:
        raise OptionError(f"{m.get('path')}: method must be one of {', '.join(METHODS)}, not {method!r}")
    opts = normalize_options(env_layer(), job["options"], m.get("options"))
    if method == "exec" and not opts["argv"]:
        raise OptionError(f"{m.get('path')}: method exec needs options.argv")
    grp = m.get("group")
    prio = m.get("priority", 0)
    if grp is not None and not isinstance(grp, str):
        raise OptionError(f"{m.get('path')}: group is a string")
    if isinstance(prio, bool) or not isinstance(prio, int):
        raise OptionError(f"{m.get('path')}: priority is a whole number")
    if "upload_back" in m and not isinstance(m["upload_back"], bool):
        raise OptionError(f"{m.get('path')}: upload_back is true or false")
    verify = m.get("verify") or job["verify"]
    if verify not in VERIFY:
        raise OptionError(f"{m.get('path')}: verify is one of {', '.join(VERIFY)}")
    return Plan(m["path"], method, opts, verify, grp or "", prio)


def order_paths(items: dict, paths: list, job: dict) -> list:
    """`paths` in the order they should start: priority first, then by the job's `order`."""
    def key(p):
        m = items.get(p) or {}
        size = int(m.get("size") or 0)
        prio = m.get("priority", 0) if isinstance(m.get("priority", 0), int) else 0
        tie = {"largest_first": -size, "smallest_first": size, "given": 0}[job["order"]]
        return (prio, tie)
    return sorted(paths, key=key) if job["order"] != "given" else sorted(paths, key=lambda p: key(p)[0])


def effective_connections(opts: dict, n_sources: int, parallel: int, budget: int) -> int:
    """connections_per_source, cut down so files x sources x connections stays under `budget`."""
    n = opts["connections_per_source"]
    return max(1, min(n, budget // max(1, parallel * max(1, n_sources))))


def fs_type(path) -> str:
    """The filesystem type holding `path` (Linux /proc/mounts), '' when unknown."""
    try:
        p = os.path.realpath(str(path))
        best, typ = "", ""
        for ln in open("/proc/mounts"):
            f = ln.split()
            if len(f) >= 3 and (p == f[1] or p.startswith(f[1].rstrip("/") + "/")) and len(f[1]) >= len(best):
                best, typ = f[1], f[2]
        return typ
    except OSError:
        return ""


def preflight(root, items: dict, paths: list, margin: float = 1.02) -> Optional[str]:
    """None when the files fit on the disk holding `root`, else a message saying by how much not."""
    need = 0
    for p in paths:
        m = items.get(p) or {}
        size = int(m.get("size") or 0)
        have = 0
        part = Path(root) / (p + ".part")
        if part.exists():
            have = part.stat().st_size
        need += max(0, size - have)
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        return None
    if need * margin > free:
        return (f"not enough disk for the models: need {need / 1e9:.1f} GB, {free / 1e9:.1f} GB free "
                f"under {root} — raise disk.container_gb")
    return None


def aria2_flags(opts: dict, n_urls: int, connections: int, dest_fs: str = "") -> list:
    """The aria2c command-line flags for these options (the executable and the paths come later)."""
    alloc = opts["file_allocation"]
    if alloc != "none" and (dest_fs.startswith("fuse") or dest_fs in ("nfs", "nfs4", "cifs", "smb3", "9p")):
        alloc = "none"                  # a network/FUSE disk cannot pre-allocate (measured: exit 17)
    f = ["--continue=true", "--allow-overwrite=true", "--auto-file-renaming=false",
         f"--max-connection-per-server={min(connections, 16)}",
         f"--split={opts['split'] or connections * n_urls}",
         f"--min-split-size={opts['min_split']}", f"--file-allocation={alloc}",
         "--summary-interval=0", "--console-log-level=warn", f"--user-agent={fetch._UA}",
         f"--max-tries={opts['retries']}", f"--retry-wait={opts['retry_wait']}",
         f"--uri-selector={opts['uri_selector']}"]
    if opts["lowest_speed"]:
        f.append(f"--lowest-speed-limit={opts['lowest_speed']}")
    if opts["disk_cache"]:
        f.append(f"--disk-cache={opts['disk_cache']}")
    return f + list(opts["extra_args"])


def download(urls, dest, plan: Plan, *, size: Optional[int] = None, sha256: Optional[str] = None,
             deadline: Optional[float] = None, headers: Optional[dict] = None,
             parallel_files: int = 1, budget: int = 48, progress: Optional[Callable] = None,
             interval: float = 30.0, log: Optional[Callable] = None) -> dict:
    """Fetch one file by its plan. Returns stats: bytes, secs, mbps, method, sources, connections,
    and `isolated` (the combined download failed its checksum, one source alone passed).
    Raises fetch.LinkExpired / fetch.DownloadError like fetch.download."""
    urls = [urls] if isinstance(urls, str) else list(urls)
    if not urls:
        raise fetch.DownloadError("no URL to download from")
    o = plan.options
    want = sha256 if plan.verify == "sha256" else None
    chk_size = size if plan.verify in ("sha256", "size") else None
    dest = Path(dest)
    say = log or (lambda s: None)
    method = plan.method
    exe = shutil.which("aria2c") if method == "aria2" and os.environ.get("PODH_NO_ARIA2") != "1" else None
    if method == "aria2" and not exe:
        method = "http"
        say(f"{plan.path}: aria2c is not available, using the Python downloader")
    conns = effective_connections(o, len(urls), parallel_files, budget)
    if conns < o["connections_per_source"]:
        say(f"{plan.path}: {conns} connection(s) per source (the {budget}-connection budget)")
    fs = fs_type(dest.parent)

    def run(us: list, hdrs) -> None:
        if method == "aria2":
            fetch.aria2c_download(us, dest, size=chk_size, sha256=want, deadline=deadline, exe=exe,
                                  headers=hdrs, progress=progress, interval=interval,
                                  flags=aria2_flags(o, len(us), conns, fs))
        elif method == "curl":
            fetch.curl_download(us, dest, size=chk_size, sha256=want, deadline=deadline,
                                headers=hdrs, progress=progress, interval=interval)
        elif method == "exec":
            fetch.exec_download(o["argv"], us, dest, size=chk_size, sha256=want, deadline=deadline,
                                progress=progress, interval=interval)
        else:
            last: Optional[Exception] = None
            for u in us:
                try:
                    fetch.http_download(u, dest, size=chk_size, sha256=want, deadline=deadline, headers=hdrs)
                    return
                except fetch.LinkExpired as e:
                    last = e
                except fetch.DownloadError as e:
                    last = e
                    if "timed out" in str(e):
                        break
            raise last if last else fetch.DownloadError("no URL to download from")

    t0 = time.time()
    isolated = False
    try:
        run(urls, headers)
    except fetch.DownloadError as e:
        mismatch = "sha256" in str(e) and not isinstance(e, fetch.LinkExpired)
        if not (mismatch and len(urls) > 1 and not headers):
            raise
        # The sources together produced a wrong file: one of them serves different bytes. Try each
        # alone, in order; the first that verifies wins, and the log names the ones that did not.
        bad, ok = [], False
        for u in urls:
            try:
                run([u], None)
                ok = True
                isolated = True
                break
            except fetch.DownloadError as e2:
                bad.append(f"{u.split('?', 1)[0]} ({str(e2)[-60:]})")
        if not ok:
            raise fetch.DownloadError(f"{dest.name}: no source gave a file with the right sha256 "
                                      f"(tried each alone)")
        say(f"{plan.path}: the sources together failed the checksum; passed from {u.split('?', 1)[0]} alone; "
            f"suspect: {', '.join(bad) or 'the others'}")
    secs = time.time() - t0
    nbytes = dest.stat().st_size
    return {"bytes": nbytes, "secs": round(secs, 2), "mbps": round(nbytes / 1e6 / max(secs, 0.001), 1),
            "method": method, "sources": len(urls), "connections": conns, "isolated": isolated}


class Progress:
    """Throttled progress lines for one file: 'path: 4.1/9.4 GB (44%) at 187 MB/s'."""

    def __init__(self, path: str, total: Optional[int], line: Callable):
        self.path, self.total, self.line = path, total, line
        self.t_prev, self.b_prev = time.time(), 0
        self.lock = threading.Lock()

    def __call__(self, done: int, total: Optional[int] = None) -> None:
        with self.lock:
            now = time.time()
            tot = total or self.total
            rate = (done - self.b_prev) / 1e6 / max(now - self.t_prev, 0.001)
            self.t_prev, self.b_prev = now, done
        pct = f" ({100 * done // tot}%)" if tot else ""
        of = f"/{tot / 1e9:.1f}" if tot else ""
        self.line(f"{self.path}: {done / 1e9:.1f}{of} GB{pct} at {rate:.0f} MB/s")


def explain(items: dict, job: dict) -> list:
    """What would run, per file, with no network: the resolved plans (a dry run)."""
    out = []
    for p, m in items.items():
        pl = plan_for(m, job)
        d = pl.describe()
        d["sources"] = len(m.get("sources") or ([m["source"]] if m.get("source") else [])) + (1 if m.get("url") else 0)
        out.append(d)
    return out


if __name__ == "__main__":                       # python -m pod_harness.dl plan.json  (a dry run)
    import json
    import sys
    spec = json.load(open(sys.argv[1]))
    job = job_settings(spec.get("fetch"))
    files = {f["path"]: f for f in (spec.get("files") or spec.get("paths") or [])}
    print(json.dumps({"job": job, "files": explain(files, job)}, indent=1))
