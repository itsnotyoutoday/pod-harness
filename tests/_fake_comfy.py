"""A stand-in ComfyUI on 127.0.0.1: /prompt, /ws (real websocket frames), /history, /queue,
/interrupt, /view, /object_info, /system_stats — enough to drive podh-worker's comfy runner.

Behaviour, set on the instance before a run:
  mode          "ok" | "stall" (starts, then nothing) | "error" (execution_error) | "refuse"
                (POST /prompt answers 400)
  steps, step_delay   progress messages per node that has `steps` in its inputs
  outputs       file names the run saves (into <root>/output/<subfolder>/), plus a temp preview
  ignore_interrupt    POST /interrupt does nothing (a node in a C loop)
  ws            False: /ws answers 404, so the client polls /history

`restart()` is the local API's /workload/restart: the current run dies, sockets drop.
Not a test itself (no test_ prefix).
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeComfy:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "input").mkdir(parents=True, exist_ok=True)
        (self.root / "output").mkdir(parents=True, exist_ok=True)
        self.mode = "ok"
        self.steps, self.step_delay = 3, 0.05
        self.outputs = ["out_00001.png"]
        self.subfolder = ""
        self.ignore_interrupt = False
        self.ws = True
        self.node_types = {"KSampler", "SaveImage", "LoadImage", "CheckpointLoaderSimple"}
        self.prompts: list = []           # (prompt_id, graph, client_id)
        self.history: dict = {}
        self.running = None
        self.pending: list = []
        self.interrupts = 0
        self.restarts = 0
        self.flag = threading.Event()     # interrupt requested
        self.killed = threading.Event()
        self.clients: dict = {}           # client_id → [conn]
        self.lock = threading.Lock()
        self.closing = False
        self.work = threading.Condition()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj, raw=None):
                data = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                if u.path == "/ws":
                    return self._ws(q.get("clientId", ""))
                if u.path == "/system_stats":
                    return self._send(200, {"system": {}})
                if u.path == "/object_info":
                    return self._send(200, {t: {} for t in outer.node_types})
                if u.path.startswith("/history/"):
                    pid = u.path[len("/history/"):]
                    with outer.lock:
                        e = outer.history.get(pid)
                    return self._send(200, {pid: e} if e else {})
                if u.path == "/queue":
                    with outer.lock:
                        run = [[0, outer.running]] if outer.running else []
                        pend = [[0, p[0]] for p in outer.pending]
                    return self._send(200, {"queue_running": run, "queue_pending": pend})
                if u.path == "/view":
                    p = outer.root / q.get("type", "output") / q.get("subfolder", "") / q["filename"]
                    if not p.is_file():
                        return self._send(404, {"error": "no file"})
                    return self._send(200, None, raw=p.read_bytes())
                self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                b = self._body()
                if self.path == "/prompt":
                    if outer.mode == "refuse":
                        return self._send(400, {"error": {"message": "Prompt outputs failed validation"},
                                                "node_errors": {}})
                    pid = uuid.uuid4().hex
                    with outer.work:
                        outer.prompts.append((pid, b.get("prompt"), b.get("client_id")))
                        outer.pending.append((pid, b.get("prompt"), b.get("client_id")))
                        outer.work.notify()
                    return self._send(200, {"prompt_id": pid, "number": len(outer.prompts),
                                            "node_errors": {}})
                if self.path == "/interrupt":
                    outer.interrupts += 1
                    if not outer.ignore_interrupt:
                        outer.flag.set()
                    return self._send(200, {})
                if self.path == "/queue":
                    with outer.lock:
                        dels = set(b.get("delete") or [])
                        outer.pending = [p for p in outer.pending if p[0] not in dels]
                    return self._send(200, {})
                self._send(404, {"error": "not found"})

            def _ws(self, cid):
                if not outer.ws:
                    return self._send(404, {"error": "no ws"})
                key = self.headers.get("Sec-WebSocket-Key", "")
                acc = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", acc)
                self.end_headers()
                self.wfile.flush()
                conn = {"w": self.wfile, "alive": True, "lock": threading.Lock(),
                        "sock": self.connection}
                with outer.lock:
                    outer.clients.setdefault(cid, []).append(conn)
                outer._frame(conn, {"type": "status", "data": {"status": {}}})
                while conn["alive"] and not outer.closing:
                    time.sleep(0.05)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a: None      # a client hanging up is not news
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._executor, daemon=True).start()

    # -- websocket out ------------------------------------------------------------------------
    def _frame(self, conn, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
        try:
            with conn["lock"]:
                conn["w"].write(head + data)
                conn["w"].flush()
        except OSError:
            conn["alive"] = False

    def send(self, cid, kind, data):
        with self.lock:
            conns = list(self.clients.get(cid, []))
        for c in conns:
            if c["alive"]:
                self._frame(c, {"type": kind, "data": data})

    # -- execution ----------------------------------------------------------------------------
    def _executor(self):
        while not self.closing:
            with self.work:
                while not self.pending and not self.closing:
                    self.work.wait(0.2)
                if self.closing:
                    return
                with self.lock:
                    if not self.pending:
                        continue
                    pid, graph, cid = self.pending.pop(0)
                    self.running = pid
            self.flag.clear()
            self.killed.clear()
            try:
                self._execute(pid, graph or {}, cid)
            finally:
                with self.lock:
                    self.running = None

    def _stopped(self, pid, cid) -> bool:
        if self.killed.is_set():
            return True
        if self.flag.is_set():
            with self.lock:
                self.history[pid] = {"outputs": {}, "status": {
                    "status_str": "error", "completed": False,
                    "messages": [["execution_interrupted", {"prompt_id": pid}]]}}
            self.send(cid, "execution_interrupted", {"prompt_id": pid})
            return True
        return False

    def _execute(self, pid, graph, cid):
        self.send(cid, "execution_start", {"prompt_id": pid})
        nodes = list(graph)
        if self.mode == "stall":
            self.send(cid, "executing", {"node": nodes[0] if nodes else "1", "prompt_id": pid})
            while not self._stopped(pid, cid) and not self.closing:
                time.sleep(0.05)
            return
        for node in nodes:
            if self._stopped(pid, cid):
                return
            self.send(cid, "executing", {"node": node, "prompt_id": pid})
            if "steps" in ((graph[node] or {}).get("inputs") or {}):
                for i in range(1, self.steps + 1):
                    time.sleep(self.step_delay)
                    if self._stopped(pid, cid):
                        return
                    self.send(cid, "progress", {"value": i, "max": self.steps,
                                                "prompt_id": pid, "node": node})
            if self.mode == "error" and node == nodes[-1]:
                with self.lock:
                    self.history[pid] = {"outputs": {}, "status": {
                        "status_str": "error", "completed": False, "messages": [
                            ["execution_error", {"exception_message": "CUDA out of memory"}]]}}
                self.send(cid, "execution_error", {"prompt_id": pid, "node_id": node,
                                                   "node_type": "KSampler",
                                                   "exception_message": "CUDA out of memory"})
                return
        out = self.root / "output" / self.subfolder
        out.mkdir(parents=True, exist_ok=True)
        images = []
        for f in self.outputs:
            (out / f).write_bytes(f"{pid}:{f}".encode())
            images.append({"filename": f, "subfolder": self.subfolder, "type": "output"})
        (self.root / "temp").mkdir(exist_ok=True)
        (self.root / "temp" / "preview.png").write_bytes(b"p")
        with self.lock:
            self.history[pid] = {"outputs": {"9": {"images": images},
                                             "10": {"images": [{"filename": "preview.png",
                                                                "subfolder": "", "type": "temp"}]}},
                                 "status": {"status_str": "success", "completed": True,
                                            "messages": []}}
        self.send(cid, "executed", {"node": "9", "prompt_id": pid, "output": {"images": images}})
        self.send(cid, "execution_success", {"prompt_id": pid})
        self.send(cid, "executing", {"node": None, "prompt_id": pid})

    def restart(self):
        """The workload restarted: the run dies without a word, sockets drop."""
        self.restarts += 1
        self.killed.set()
        with self.lock:
            conns = [c for cs in self.clients.values() for c in cs]
            self.clients = {}
            self.pending = []
        for c in conns:
            c["alive"] = False
        return 200, {"restarted": True, "after_s": 0.0, "url": self.url}

    def close(self):
        self.closing = True
        self.server.shutdown()
        self.server.server_close()
