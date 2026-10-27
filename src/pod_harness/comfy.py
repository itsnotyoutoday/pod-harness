"""A ComfyUI client for workers and handlers: inputs in, one execution followed, outputs out.

    c = ComfyClient()                       # $PODH_COMFY_URL (podh-boot exports it), $PODH_COMFY_ROOT
    name = c.upload_input("/tmp/start.png")             # → "podh/start.png", for a LoadImage
    r = c.run(graph, timeout_s=900, stall_s=300, keep=["*.mp4"], dest=workdir)
    r.prompt_id, r.outputs, r.files

Stdlib only: the websocket that carries ComfyUI's progress is spoken here directly (a
handshake and text frames are all it needs), so following a run costs no dependency in an
image where every megabyte is cold-start latency. When /ws refuses, `run` polls /history.

## Why a run can be stopped three ways, and what each costs

`run` stops an execution when the caller's `should_stop()` names a reason (a cancel from the
control plane), when `timeout_s` passes, or when nothing has progressed for `stall_s` — a
wedged node, which ComfyUI never reports by itself. Each stop is the same sequence:
POST /interrupt (and the prompt removed from the queue if it has not started), then up to
`interrupt_grace` seconds for ComfyUI to say it stopped; a node that ignores the interrupt
(custom nodes in a C loop do) gets the workload restarted through the local API, because a
ComfyUI still busy with the last task would run the next one late or not at all. The stall
guard needs the websocket: polling cannot tell a long step from a stuck one, so a polled run
is bounded by `timeout_s` alone.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import shutil
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

DEFAULT_URL = "http://127.0.0.1:8188"
INTERRUPT_GRACE = 20.0


class Cancelled(BaseException):
    """The task was cancelled. A BaseException, so a handler's `except Exception` cannot
    swallow it: a cancel is final, and the worker must see it."""


class ComfyError(Exception):
    """ComfyUI refused the graph or failed executing it."""

    def __init__(self, reason: str, stage: str = "comfy", detail: Optional[dict] = None):
        super().__init__(reason)
        self.reason, self.stage, self.detail = reason, stage, detail or {}


class Stalled(ComfyError):
    """No progress for stall_s: interrupted (or the workload restarted)."""


class TimedOut(ComfyError):
    """The execution passed timeout_s: interrupted (or the workload restarted)."""


class RunResult:
    """`files` are local paths; `names[i]` is files[i]'s path relative to ComfyUI's output
    directory (`subfolder/filename`), the name a handler uploads it under."""

    def __init__(self, prompt_id: str, outputs: list, files: list, history: dict,
                 names: Optional[list] = None):
        self.prompt_id, self.outputs, self.files, self.history = prompt_id, outputs, files, history
        self.names = list(names) if names is not None else [Path(f).name for f in files]

    def __repr__(self):
        return f"RunResult({self.prompt_id}, {len(self.files)} file(s))"


# -- a minimal websocket client (RFC 6455, text frames) ---------------------------------------

class _WS:
    def __init__(self, url: str, timeout: float = 10):
        u = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\n"
                           f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            got = self.sock.recv(4096)
            if not got:
                raise ConnectionError("websocket handshake: connection closed")
            buf += got
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError(f"websocket handshake refused: {status.decode(errors='replace')}")
        want = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if want.encode() not in head:
            raise ConnectionError("websocket handshake: bad Sec-WebSocket-Accept")
        self.frags = b""

    def _frame(self):
        """(opcode, fin, payload) from the buffer, or None if it holds no whole frame."""
        b = self.buf
        if len(b) < 2:
            return None
        op, fin, ln, i = b[0] & 0x0F, bool(b[0] & 0x80), b[1] & 0x7F, 2
        masked = bool(b[1] & 0x80)
        if ln == 126:
            if len(b) < 4:
                return None
            ln, i = struct.unpack(">H", b[2:4])[0], 4
        elif ln == 127:
            if len(b) < 10:
                return None
            ln, i = struct.unpack(">Q", b[2:10])[0], 10
        mask = b""
        if masked:
            if len(b) < i + 4:
                return None
            mask, i = b[i:i + 4], i + 4
        if len(b) < i + ln:
            return None
        data = b[i:i + ln]
        if masked:
            data = bytes(c ^ mask[j % 4] for j, c in enumerate(data))
        self.buf = b[i + ln:]
        return op, fin, data

    def _send(self, op: int, data: bytes = b"") -> None:
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(c ^ mask[j % 4] for j, c in enumerate(data)))

    def recv(self, timeout: float) -> Optional[str]:
        """The next text message, None if none came within `timeout`. Binary frames
        (previews) are skipped. Raises ConnectionError when the server closes."""
        end = time.time() + timeout
        while True:
            fr = self._frame()
            if fr is not None:
                op, fin, data = fr
                if op == 8:
                    raise ConnectionError("websocket closed by ComfyUI")
                if op == 9:
                    self._send(10, data)
                    continue
                if op in (1, 0):
                    self.frags += data
                    if fin:
                        out, self.frags = self.frags, b""
                        return out.decode("utf-8", errors="replace")
                continue
            left = end - time.time()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                got = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not got:
                raise ConnectionError("websocket closed by ComfyUI")
            self.buf += got

    def close(self) -> None:
        try:
            self._send(8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# -- the client -------------------------------------------------------------------------------

class ComfyClient:
    def __init__(self, url: Optional[str] = None, root=None, *,
                 should_stop: Optional[Callable[[], Optional[str]]] = None,
                 on_progress: Optional[Callable[[float, str], None]] = None,
                 restart: Optional[Callable[[], bool]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 interrupt_grace: float = INTERRUPT_GRACE, use_ws: bool = True):
        self.url = (url or os.environ.get("PODH_COMFY_URL") or DEFAULT_URL).rstrip("/")
        r = root if root is not None else os.environ.get("PODH_COMFY_ROOT")
        self.root = Path(r) if r else None
        self.should_stop = should_stop
        self.on_progress = on_progress
        self.restart = restart
        self.log = log or (lambda t: print(f"[comfy] {t}", flush=True))
        self.interrupt_grace = interrupt_grace
        self.use_ws = use_ws
        self.client_id = uuid.uuid4().hex

    # -- HTTP --------------------------------------------------------------------------------
    def _req(self, method: str, path: str, body=None, timeout: float = 30, raw: bool = False):
        data, headers = None, {}
        if body is not None:
            data, headers = json.dumps(body).encode(), {"Content-Type": "application/json"}
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = r.read()
                return r.status, (out if raw else (json.loads(out) if out.strip() else None))
        except urllib.error.HTTPError as e:
            out = e.read()
            try:
                return e.code, json.loads(out)
            except Exception:
                return e.code, out.decode(errors="replace")[:500]

    def get(self, path: str, timeout: float = 30):
        return self._req("GET", path, timeout=timeout)

    def post(self, path: str, body=None, timeout: float = 30):
        return self._req("POST", path, body if body is not None else {}, timeout=timeout)

    def alive(self, timeout: float = 5) -> bool:
        try:
            return self._req("GET", "/system_stats", timeout=timeout)[0] == 200
        except Exception:
            return False

    def object_info(self) -> dict:
        st, obj = self.get("/object_info", timeout=60)
        if st != 200 or not isinstance(obj, dict):
            raise ComfyError(f"GET /object_info answered {st}")
        return obj

    @property
    def input_dir(self) -> Optional[Path]:
        return self.root / "input" if self.root else None

    @property
    def output_dir(self) -> Optional[Path]:
        return self.root / "output" if self.root else None

    # -- inputs ------------------------------------------------------------------------------
    def upload_input(self, src, name: Optional[str] = None, subfolder: str = "podh") -> str:
        """Put a file where a Load* node finds it; returns the name to give that node
        ("<subfolder>/<name>"). A copy into input/ when the root is known, else
        POST /upload/image."""
        src = Path(src)
        name = name or src.name
        if "/" in name or name in ("", ".", ".."):
            raise ValueError(f"bad input name {name!r}")
        if self.input_dir is not None:
            d = self.input_dir / subfolder
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".{name}.podh-tmp"
            shutil.copyfile(src, tmp)
            os.replace(tmp, d / name)
            return f"{subfolder}/{name}" if subfolder else name
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (("subfolder", subfolder), ("overwrite", "true"), ("type", "input")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                         f"{v}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                     f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                     .encode() + src.read_bytes() + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body, method="POST",
                                     headers={"Content-Type":
                                              f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            obj = json.loads(r.read())
        sub = obj.get("subfolder") or ""
        return f"{sub}/{obj['name']}" if sub else obj["name"]

    # -- execution ---------------------------------------------------------------------------
    def submit(self, graph: dict) -> str:
        st, obj = self.post("/prompt", {"prompt": graph, "client_id": self.client_id}, timeout=60)
        if st != 200 or not isinstance(obj, dict) or not obj.get("prompt_id"):
            detail = obj if isinstance(obj, dict) else {"body": obj}
            msg = (detail.get("error") or {}).get("message") if isinstance(
                detail.get("error"), dict) else detail.get("error")
            raise ComfyError(f"ComfyUI refused the graph ({st}): {msg or detail}"[:500],
                             stage="submit", detail=detail)
        return str(obj["prompt_id"])

    def history(self, prompt_id: str) -> Optional[dict]:
        try:
            st, obj = self.get(f"/history/{urllib.parse.quote(prompt_id)}", timeout=30)
        except Exception:
            return None
        if st == 200 and isinstance(obj, dict) and prompt_id in obj:
            return obj[prompt_id]
        return None

    def queued(self, prompt_id: str) -> Optional[str]:
        """'running' | 'pending' | None (not in the queue), or 'unknown' if no answer."""
        try:
            st, obj = self.get("/queue", timeout=10)
        except Exception:
            return "unknown"
        if st != 200 or not isinstance(obj, dict):
            return "unknown"
        for state, key in (("running", "queue_running"), ("pending", "queue_pending")):
            for item in obj.get(key) or []:
                if isinstance(item, list) and len(item) > 1 and item[1] == prompt_id:
                    return state
        return None

    def interrupt(self, prompt_id: Optional[str] = None) -> None:
        try:
            if prompt_id:
                self.post("/queue", {"delete": [prompt_id]}, timeout=10)
            self.post("/interrupt", {"prompt_id": prompt_id} if prompt_id else {}, timeout=10)
        except Exception as e:
            self.log(f"interrupt: {type(e).__name__}: {e}")

    def stop(self, prompt_id: str, ws: Optional[_WS] = None) -> str:
        """Interrupt, wait up to interrupt_grace for ComfyUI to stop, else restart it.
        'interrupted' | 'restarted' | 'unstopped'."""
        self.interrupt(prompt_id)
        end = time.time() + self.interrupt_grace
        while time.time() < end:
            if ws is not None:
                try:
                    m = ws.recv(min(0.5, max(0.01, end - time.time())))
                except (ConnectionError, OSError):
                    ws = None
                    continue
                if m:
                    msg = _parse(m)
                    if msg and msg[1].get("prompt_id") in (None, prompt_id) and msg[0] in (
                            "execution_interrupted", "execution_error", "execution_success"):
                        return "interrupted"
                    continue
            else:
                time.sleep(0.5)
            if self.history(prompt_id) is not None or self.queued(prompt_id) is None:
                return "interrupted"
        self.log(f"{prompt_id}: ComfyUI ignored the interrupt for {self.interrupt_grace:.0f}s; "
                 f"restarting the workload")
        if self.restart is not None and self.restart():
            return "restarted"
        return "unstopped"

    def run(self, graph: dict, *, timeout_s: Optional[float] = None,
            stall_s: Optional[float] = None, keep: Optional[list] = None,
            dest=None) -> RunResult:
        """Submit `graph` and follow it to the end. Raises ComfyError (refused or failed),
        Stalled, TimedOut, or Cancelled when should_stop() says "cancelled"."""
        ws = None
        if self.use_ws:
            try:
                ws = _WS(self.url.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}")
            except (OSError, ConnectionError) as e:
                self.log(f"no websocket ({e}); polling /history (no stall guard)")
        try:
            pid = self.submit(graph)
            entry = self._follow(pid, graph, ws, timeout_s, stall_s)
        finally:
            if ws is not None:
                ws.close()
        outs = outputs_of(entry)
        st = (entry.get("status") or {})
        if st.get("status_str") == "error":
            raise ComfyError(f"ComfyUI execution failed: {_status_reason(st)}", stage="execute",
                             detail={"prompt_id": pid})
        files, names = [], []
        for o, f in zip(*self._collect(outs, keep, dest)):
            files.append(f)
            names.append(rel_name(o))
        return RunResult(pid, outs, files, entry, names)

    def _follow(self, pid: str, graph: dict, ws: Optional[_WS], timeout_s, stall_s) -> dict:
        t0 = last = time.time()
        total = max(1, len(graph))
        done_nodes: set = set()
        cached = 0
        step = 0.0
        node = None
        finished = False
        poll_at = 0.0

        def abort(reason: str):
            how = self.stop(pid, ws)
            if reason == "cancelled":
                raise Cancelled(f"cancelled ({how})")
            if reason == "stalled":
                raise Stalled(f"no progress for {stall_s:.0f}s on node {node} ({how})",
                              stage="stall", detail={"prompt_id": pid, "node": node})
            if reason == "timeout":
                raise TimedOut(f"execution passed {timeout_s:.0f}s ({how})", stage="timeout",
                               detail={"prompt_id": pid})
            raise Cancelled(f"{reason} ({how})")

        while True:
            reason = self.should_stop() if self.should_stop else None
            if reason:
                abort(reason)
            now = time.time()
            if timeout_s and now - t0 > timeout_s:
                abort("timeout")
            if stall_s and ws is not None and now - last > stall_s:
                abort("stalled")
            if finished or (ws is None and now >= poll_at):
                # ComfyUI writes a history entry when a run ends, success or error alike.
                entry = self.history(pid)
                if entry is not None:
                    if (entry.get("status") or {}).get("status_str") != "error":
                        self._progress(1.0, "done")
                    return entry
                if finished and now - last > 30:
                    raise ComfyError("ComfyUI reported the run finished but has no history "
                                     "for it", stage="execute", detail={"prompt_id": pid})
                poll_at = now + (0.2 if finished else 1.0)
            if ws is None:
                time.sleep(0.2)
                continue
            try:
                m = ws.recv(0.5)
            except (ConnectionError, OSError) as e:
                self.log(f"websocket dropped ({e}); polling /history")
                ws = None
                continue
            if not m:
                continue
            msg = _parse(m)
            if not msg:
                continue
            kind, data = msg
            if data.get("prompt_id") not in (None, pid):
                continue
            if kind == "status":
                continue
            last = time.time()
            if kind == "execution_cached":
                cached = len(data.get("nodes") or [])
            elif kind == "executing":
                if data.get("node") is None:
                    if data.get("prompt_id") == pid:
                        finished = True
                else:
                    if node is not None:
                        done_nodes.add(node)
                    node, step = data.get("node"), 0.0
            elif kind == "progress":
                try:
                    step = float(data.get("value") or 0) / float(data.get("max") or 1)
                except (TypeError, ValueError, ZeroDivisionError):
                    step = 0.0
            elif kind == "executed":
                done_nodes.add(data.get("node"))
            elif kind == "execution_success":
                finished = True
            elif kind == "execution_interrupted":
                raise Cancelled("ComfyUI reports the run was interrupted")
            elif kind == "execution_error":
                raise ComfyError(f"{data.get('node_type') or 'node'} {data.get('node_id') or ''}: "
                                 f"{data.get('exception_message') or 'failed'}"[:500],
                                 stage="execute", detail={"prompt_id": pid,
                                                          "node": data.get("node_id")})
            frac = (len(done_nodes) + cached + step) / max(total, len(done_nodes) + cached + 1)
            self._progress(min(0.99, frac), f"node {node}" if node else kind)

    def _progress(self, frac: float, msg: str) -> None:
        if self.on_progress:
            try:
                self.on_progress(frac, msg)
            except Exception:
                pass

    # -- outputs -----------------------------------------------------------------------------
    def collect(self, outs: list, keep: Optional[list] = None, dest=None) -> list:
        """Local paths of the outputs matching `keep` (all when empty): the file in
        output/ when the root is known, else a copy fetched from /view into `dest`
        (at its subfolder/filename there)."""
        return self._collect(outs, keep, dest)[1]

    def _collect(self, outs: list, keep: Optional[list] = None, dest=None) -> tuple:
        kept, files = [], []
        for o in outs:
            if keep and not matches(o, keep):
                continue
            local = None
            if self.root is not None:
                base = self.root / (o.get("type") or "output")
                p = base / (o.get("subfolder") or "") / o["filename"]
                if p.is_file():
                    local = p
            if local is None:
                if dest is None:
                    raise ComfyError(f"{o['filename']}: not on this disk and no dest to fetch "
                                     f"it into", stage="outputs")
                d = Path(dest)
                d.mkdir(parents=True, exist_ok=True)
                q = urllib.parse.urlencode({"filename": o["filename"],
                                            "subfolder": o.get("subfolder") or "",
                                            "type": o.get("type") or "output"})
                st, data = self._req("GET", f"/view?{q}", timeout=600, raw=True)
                if st != 200:
                    raise ComfyError(f"{o['filename']}: GET /view answered {st}", stage="outputs")
                local = d / rel_name(o)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(data)
            kept.append(o)
            files.append(local)
        return kept, files


def _parse(m: str):
    try:
        obj = json.loads(m)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    return str(obj.get("type")), (data if isinstance(data, dict) else {})


def _status_reason(st: dict) -> str:
    for m in st.get("messages") or []:
        if isinstance(m, list) and len(m) > 1 and m[0] in ("execution_error",
                                                          "execution_interrupted"):
            d = m[1] or {}
            return str(d.get("exception_message") or m[0])[:300]
    return st.get("status_str") or "error"


def outputs_of(entry: dict) -> list:
    """Every saved file named in a history entry: [{filename, subfolder, type, node}]. Temp
    files (previews) are left out."""
    out = []
    for node, o in ((entry or {}).get("outputs") or {}).items():
        if not isinstance(o, dict):
            continue
        for items in o.values():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("filename"), str) \
                        and (it.get("type") or "output") != "temp":
                    out.append({"filename": it["filename"], "subfolder": it.get("subfolder") or "",
                                "type": it.get("type") or "output", "node": str(node)})
    return out


def rel_name(o: dict) -> str:
    """An output's path relative to its ComfyUI directory: `subfolder/filename`, with
    anything that would climb out (`..`, absolute, empty segments) dropped."""
    parts = [p for p in f"{o.get('subfolder') or ''}/{o['filename']}".replace("\\", "/").split("/")
             if p not in ("", ".", "..")]
    return "/".join(parts) or "output"


def matches(o: dict, keep: list) -> bool:
    name = o["filename"]
    rel = f"{o['subfolder']}/{name}" if o.get("subfolder") else name
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g) for g in keep)


# -- podh-catalogue ---------------------------------------------------------------------------

def missing_nodes(workflow: dict, object_info: dict) -> list:
    """Node types a workflow uses that this ComfyUI does not have. API format
    ({id: {class_type}}) or UI format ({"nodes": [{type}]})."""
    types = set()
    if isinstance(workflow.get("nodes"), list):
        types = {n.get("type") for n in workflow["nodes"] if isinstance(n, dict)}
    else:
        types = {n.get("class_type") for n in workflow.values() if isinstance(n, dict)}
    types.discard(None)
    # UI-only helpers that never reach the server
    types -= {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}
    return sorted(t for t in types if t not in object_info)


def catalogue_main(argv: Optional[list] = None) -> int:
    """podh-catalogue check <workflow.json ...> [--url URL]: exit 1 if a node is missing."""
    import argparse
    ap = argparse.ArgumentParser(prog="podh-catalogue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="do these workflows' node types exist in this ComfyUI?")
    c.add_argument("workflows", nargs="+")
    c.add_argument("--url", help="ComfyUI (default $PODH_COMFY_URL, else 127.0.0.1:8188)")
    c.add_argument("--object-info", help="a saved /object_info JSON instead of asking ComfyUI")
    a = ap.parse_args(argv)
    if a.object_info:
        info = json.loads(Path(a.object_info).read_text())
    else:
        try:
            info = ComfyClient(a.url).object_info()
        except (ComfyError, OSError) as e:
            print(f"podh-catalogue: {e}")
            return 2
    bad = 0
    for w in a.workflows:
        try:
            wf = json.loads(Path(w).read_text())
        except (OSError, ValueError) as e:
            print(f"{w}: cannot read: {e}")
            bad += 1
            continue
        miss = missing_nodes(wf, info)
        print(f"{w}: {'ok' if not miss else 'MISSING ' + ', '.join(miss)}")
        bad += bool(miss)
    return 1 if bad else 0
