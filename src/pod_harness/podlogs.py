"""The pod's logs, on request and at the end (pod-control APP-API §9 "Pod logs").

pod-control never connects into a pod, so logs travel the way everything else does — out:

  on request  An app asks pod-control for a job's logs; the next /v1/alive reply carries
              `logs: {names, lines, req_id}`. `Responder` (subscribed to every reply) reads
              the last `lines` lines of each named log, strips terminal colour codes, keeps
              at most 256 KB per name (the newest lines) and POSTs them to /v1/pod/logs.
              Empty `names` means every log this pod keeps.
  at the end  `upload_all` gzips each log and asks POST /v1/pod/logs/upload for presigned
              PUTs, then PUTs them one at a time under one short overall budget, so a pod
              that is ending is never held up for long. podh-boot calls it before it reports
              done — on success, on a failed stage, and when a drain completes.

A log is a file `<name>.log` in the logs directory: $PODH_LOGS_DIR, else $PODH_BOOT_DIR/logs,
else podh-init's $PODH_LOG_DIR (console.log, api.log, … — its heartbeat answers requests too).
podh-boot writes boot.log (its own stage lines), workload.log, serve.log, app-setup.log and
addon-<name>.log there; podh-worker adds worker.log.

Backwards compatible both ways: a server whose /v1/alive reply has no `logs` key is never
asked anything, and a 404 from either route (an older server, or no LOG_STORE configured)
is the answer "skip", said once on the console. Nothing here ever raises into its caller.
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from . import heartbeat

#: The server's own limits: names per call, and the stored tail per name.
NAMES_MAX = 16
TAIL_MAX_BYTES = 256 * 1024
LINES_DEFAULT, LINES_MAX = 200, 2000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: The end-of-pod upload's overall budget (seconds), and how much of each log it sends
#: (the newest bytes; a runaway log must not turn into a long upload).
UPLOAD_BUDGET_S = float(os.environ.get("PODH_LOG_UPLOAD_S") or 60)
UPLOAD_MAX_BYTES = int(float(os.environ.get("PODH_LOG_UPLOAD_MAX_MB") or 256) * 1024 * 1024)

#: CSI (colours, cursor moves), OSC (titles, links) and the two-byte escapes.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def logs_dir() -> Optional[Path]:
    v = os.environ.get("PODH_LOGS_DIR")
    if v:
        return Path(v)
    b = os.environ.get("PODH_BOOT_DIR")
    if b:
        return Path(b) / "logs"
    v = os.environ.get("PODH_LOG_DIR")
    return Path(v) if v else None


def available(d) -> dict:
    """{name: path} for every `<name>.log` in `d`, newest first, at most NAMES_MAX."""
    if d is None:
        return {}
    try:
        files = [p for p in Path(d).iterdir() if p.suffix == ".log" and p.is_file()
                 and NAME_RE.match(p.stem)]
    except OSError:
        return {}

    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    files.sort(key=mtime, reverse=True)
    return {p.stem: p for p in files[:NAMES_MAX]}


def _fit(lines: list) -> tuple:
    """The newest lines whose UTF-8 size (one newline each) fits TAIL_MAX_BYTES."""
    out, size = [], 0
    for line in reversed(lines):
        b = len(line.encode("utf-8", "replace")) + 1
        if size + b > TAIL_MAX_BYTES:
            if not out:
                keep = line.encode("utf-8", "replace")[-(TAIL_MAX_BYTES - 1):]
                out.append(keep.decode("utf-8", "ignore"))
            return list(reversed(out)), True
        out.append(line)
        size += b
    return list(reversed(out)), False


def read_tail(path, lines: int = LINES_DEFAULT) -> tuple:
    """(the last `lines` lines of the file, truncated). Colour codes stripped; a line
    rewritten in place with carriage returns (a progress bar) keeps only what a terminal
    would show last. `truncated` is set when the 256 KB cap cut lines that were asked for."""
    lines = max(1, min(int(lines or LINES_DEFAULT), LINES_MAX))
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - TAIL_MAX_BYTES)
            f.seek(start)
            raw = f.read()
    except OSError:
        return [], False
    text = raw.decode("utf-8", "replace")
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    partial = start > 0
    if partial and parts:
        parts.pop(0)                     # began mid-line
    out = []
    for p in parts:
        p = p.rstrip("\r")
        if "\r" in p:
            p = p.rsplit("\r", 1)[1]
        out.append(strip_ansi(p))
    asked = out[-lines:]
    kept, cut = _fit(asked)
    # Lines were asked for that lay before the 256 KB read: the cap cut them.
    if partial and len(out) < lines:
        cut = True
    return kept, cut


def collect(names, lines: int, d=None) -> list:
    """[{name, lines, truncated}] for the requested names (all, when empty) that exist."""
    have = available(d if d is not None else logs_dir())
    want = [n for n in (names or []) if isinstance(n, str)] or list(have)
    out = []
    for n in want[:NAMES_MAX]:
        p = have.get(n)
        if p is None:
            continue
        got, cut = read_tail(p, lines)
        out.append({"name": n, "lines": got, "truncated": cut})
    return out


def push(names, lines: int, req_id: Optional[str] = None, d=None) -> tuple:
    """POST /v1/pod/logs with the tails asked for: (status, reply). Never raises."""
    try:
        logs = collect(names, lines, d)
        body = {**heartbeat._identity(), "logs": logs}
        if req_id:
            body["req_id"] = str(req_id)
        return heartbeat.request("POST", "/v1/pod/logs", body, timeout=20)
    except Exception as exc:          # a log request never breaks the pod
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


class Responder:
    """Answers the log requests that arrive on /v1/alive replies, each once, off the
    heartbeat's own thread (a slow push must not delay the next beat)."""

    def __init__(self, d=None, say=print):
        self.d, self.say = d, say
        self.done: set = set()
        self.busy: set = set()
        self.lock = threading.Lock()
        self.unsupported = False

    def __call__(self, reply: dict) -> None:
        req = reply.get("logs") if isinstance(reply, dict) else None
        if not isinstance(req, dict) or self.unsupported:
            return
        rid = str(req.get("req_id") or "")
        with self.lock:
            if rid in self.done or rid in self.busy:
                return
            self.busy.add(rid)
        threading.Thread(target=self._answer, args=(req, rid), daemon=True,
                         name="podh-logs-push").start()

    def _answer(self, req: dict, rid: str) -> None:
        names = req.get("names") if isinstance(req.get("names"), list) else []
        try:
            lines = int(req.get("lines") or LINES_DEFAULT)
        except (TypeError, ValueError):
            lines = LINES_DEFAULT
        st, obj = push(names, lines, rid or None, self.d)
        with self.lock:
            self.busy.discard(rid)
            if st == 404:
                self.unsupported = True
            if 200 <= st < 300 or 400 <= st < 500:
                self.done.add(rid)     # answered, or refused for good: not asked again
        if 200 <= st < 300:
            stored = (obj or {}).get("stored") if isinstance(obj, dict) else None
            self.say(f"  [logs] sent {len(stored or [])} log tail(s) for request {rid or '-'}")
        elif st == 404:
            self.say("  [logs] this control plane has no /v1/pod/logs; log requests ignored")
        else:
            self.say(f"  [logs] could not send the log tails ({st or heartbeat.last_error()})")


def watch(d=None, say=print) -> Responder:
    """Subscribe a Responder to every /v1/alive reply."""
    r = Responder(d, say)
    heartbeat.on_reply(r)
    return r


# -- the end of the pod ------------------------------------------------------------------------

def _gzip(src: Path, dst: Path) -> int:
    with open(src, "rb") as f, gzip.open(dst, "wb", compresslevel=6) as g:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size > UPLOAD_MAX_BYTES:
            f.seek(size - UPLOAD_MAX_BYTES)
            g.write(f"[podh: the first {size - UPLOAD_MAX_BYTES} bytes are not included]\n"
                    .encode())
        else:
            f.seek(0)
        shutil.copyfileobj(f, g, 1024 * 1024)
    return dst.stat().st_size


def _put(url: str, path: Path, timeout: float) -> int:
    import urllib.error
    import urllib.request
    size = path.stat().st_size
    with open(path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="PUT",
                                     headers={"Content-Length": str(size),
                                              "Content-Type": "application/gzip"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


def upload_all(d=None, budget_s: Optional[float] = None, say=print) -> dict:
    """Gzip every log and PUT it into the log store through presigned URLs, within
    `budget_s` overall. {"uploaded": [names], "failed": [names], "skipped": why?}.
    Never raises."""
    budget = UPLOAD_BUDGET_S if budget_s is None else float(budget_s)
    end = time.time() + budget
    out: dict = {"uploaded": [], "failed": []}
    if not heartbeat._cfg()[0]:
        out["skipped"] = "no control plane"
        return out
    tmp = None
    try:
        have = {n: p for n, p in available(d if d is not None else logs_dir()).items()
                if p.stat().st_size > 0}
        if not have:
            out["skipped"] = "no logs"
            return out
        tmp = Path(tempfile.mkdtemp(prefix="podh-logs-"))
        gz = {}
        for n, p in have.items():
            if time.time() >= end:
                break
            try:
                dst = tmp / f"{n}.log.gz"
                gz[n] = (dst, _gzip(p, dst))
            except OSError as e:
                say(f"  [logs] cannot gzip {n}: {e}")
        if not gz:
            out["skipped"] = "nothing to send"
            return out
        body = {**heartbeat._identity(),
                "names": [{"name": n, "size": s} for n, (_, s) in gz.items()]}
        st, obj = heartbeat.request("POST", "/v1/pod/logs/upload", body,
                                    timeout=max(1.0, min(20.0, end - time.time())))
        if st == 404:
            out["skipped"] = "no log store"
            say("  [logs] no log store on this control plane; the full logs stay on the pod")
            return out
        urls = (obj or {}).get("urls") if isinstance(obj, dict) else None
        if not (200 <= st < 300) or not isinstance(urls, dict):
            out["skipped"] = f"upload refused ({st or heartbeat.last_error()})"
            say(f"  [logs] could not get upload links: {out['skipped']}")
            return out
        for n, (path, _) in gz.items():
            left = end - time.time()
            url = urls.get(n)
            if not url:
                continue
            if left <= 1:
                out["failed"].append(n)
                continue
            try:
                code = _put(url, path, timeout=left)
            except Exception as e:      # noqa: BLE001 — one log never stops the others
                code = 0
                say(f"  [logs] {n}: {type(e).__name__}: {e}")
            (out["uploaded"] if 200 <= code < 300 else out["failed"]).append(n)
        say(f"  [logs] uploaded {len(out['uploaded'])} log(s)"
            + (f"; {len(out['failed'])} not ({', '.join(out['failed'])})" if out["failed"] else ""))
        return out
    except Exception as exc:          # noqa: BLE001 — the pod's ending comes first
        out["skipped"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


class FileLog:
    """Append lines to a log file, thread-safe, opened lazily; failures are silent (the
    console still has the line)."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.f = None

    def write(self, text: str) -> None:
        if self.path is None:
            return
        with self.lock:
            try:
                if self.f is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.f = open(self.path, "a", encoding="utf-8")
                self.f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {text}\n")
                self.f.flush()
            except OSError:
                self.path = None
