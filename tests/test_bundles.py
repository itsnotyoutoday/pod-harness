"""Prebuilt bundles on a pod (worker/bundles.py): fetch, verify, extract, link or start, report.

Bundles are served from a local HTTP server on 127.0.0.1; ComfyUI's restart and site-packages
are fakes. Nothing leaves the machine and nothing is installed.
"""
import hashlib
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


os.environ["PODH_NO_ARIA2"] = "1"
from pod_harness.worker.bundles import Bundles  # noqa: E402
from pod_harness.worker.handlers import TaskFailed  # noqa: E402

tmp = Path(tempfile.mkdtemp())
DIG = "sha256:" + "ab" * 32
BLOBS = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        b = BLOBS.get(self.path.lstrip("/").split("?")[0])
        if b is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def tarball(files: dict, zstd: bool = True) -> bytes:
    """{path: bytes | (bytes, mode)} -> a tar.zst (or tar.gz) of them."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path, v in files.items():
            data, mode = v if isinstance(v, tuple) else (v, 0o644)
            ti = tarfile.TarInfo(path)
            ti.size, ti.mode = len(data), mode
            tf.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    if zstd:
        return subprocess.run(["zstd", "-q", "-c"], input=raw, capture_output=True, check=True).stdout
    import gzip
    return gzip.compress(raw)


def publish(name: str, kind: str, files: dict, digest: str = DIG) -> dict:
    man = {"name": name, "kind": kind, "image_digest": digest, "built_at": "2026-11-04T00:00:00Z"}
    blob = tarball({"bundle.json": json.dumps(man).encode(), **files})
    BLOBS[f"{name}.tar.zst"] = blob
    return {"name": name, "kind": kind, "sha256": hashlib.sha256(blob).hexdigest(),
            "size_bytes": len(blob), "image_digest": digest, "url": f"{BASE}/{name}.tar.zst"}


restarts = []
comfy = tmp / "ComfyUI"
site = tmp / "site-packages"
site.mkdir()


def make(**kw):
    args = dict(cache_dir=tmp / "bundles", image_digest=DIG, comfy_root=comfy, comfy_site=site,
                restart_comfy=lambda: (restarts.append(time.time()) or (200, {"restarted": True})),
                models_root=tmp / "models", log=lambda s: None, health_timeout_s=15)
    return Bundles(**{**args, **kw})


print("node bundles")
nodes = publish("nodes1", "comfy_nodes", {"custom_nodes/NodeA/__init__.py": b"X=1\n",
                                          "site/libfoo/__init__.py": b"V=2\n"})
nodes["start"] = {"comfyui_restart": True}
b = make()
u = b.ensure([nodes])
check("a fresh bundle is fetched, verified and reported",
      u and u[0]["fresh"] is True and u[0]["name"] == "nodes1" and u[0]["sha256"] == nodes["sha256"]
      and u[0]["pull_s"] >= 0 and u[0]["bytes"] == nodes["size_bytes"])
link = comfy / "custom_nodes" / "NodeA"
check("its node folder is linked into ComfyUI", link.is_symlink() and (link / "__init__.py").is_file())
pth = (site / "podh-bundles.pth").read_text()
check("its site/ is listed in ComfyUI's python by a .pth",
      str(tmp / "bundles" / "nodes1" / nodes["sha256"] / "site") in pth)
check("ComfyUI restarted once", len(restarts) == 1)
check("the pod reports it as bundle:nodes1", b.loaded() == ["bundle:nodes1"])
u = b.ensure([nodes])
check("a second task finds it cached and linked: no fetch, no restart",
      u[0]["fresh"] is False and len(restarts) == 1)
b2 = make()
check("a restarted worker reports what its cache holds before any task", b2.loaded() == ["bundle:nodes1"])
u = b2.ensure([nodes])
check("a restarted worker (a resumed pod) uses the cache but links and restarts again",
      u[0]["fresh"] is False and len(restarts) == 2)

print("\nrefusals")


def fails(items, bb=None):
    try:
        (bb or make()).ensure(items)
    except TaskFailed as e:
        return e
    return None


e = fails([dict(nodes, image_digest="sha256:" + "cd" * 32)])
check("a bundle built for another image is refused, not retryable, in words",
      e and e.retryable is False and e.stage == "bundles" and "rebuild" in e.reason)
bad = publish("bad1", "comfy_nodes", {"custom_nodes/B/__init__.py": b""})
e = fails([dict(bad, sha256="00" * 32)])
check("a sha256 mismatch fails the task and keeps nothing",
      e and e.retryable is False and not (tmp / "bundles" / "bad1").exists()
      and not any((tmp / "bundles" / ".tmp").iterdir()))
lie = publish("lie1", "comfy_nodes", {"custom_nodes/L/__init__.py": b""}, digest="sha256:" + "ee" * 32)
e = fails([dict(lie, image_digest=DIG)])
check("a bundle whose own bundle.json names another image is refused", e and "does not match" in e.reason)
e = fails([dict(nodes, url=None)], make(cache_dir=tmp / "empty"))
check("no url and not cached: retryable", e and e.retryable is True)
evil = BLOBS["evil.tar.zst"] = tarball({"bundle.json": b"{}", "../escape.txt": b"x"})
e = fails([{"name": "evil", "kind": "comfy_nodes", "sha256": hashlib.sha256(evil).hexdigest(),
            "size_bytes": len(evil), "image_digest": DIG, "url": f"{BASE}/evil.tar.zst"}])
check("an archive that climbs out of its dir is refused", e and not (tmp / "bundles" / "escape.txt").exists())
other = publish("nodes2", "comfy_nodes", {"custom_nodes/NodeB/__init__.py": b""})
e = fails([other], make(others_running=lambda: True))
check("a node bundle that needs a restart while another task runs goes back to the queue",
      e and e.retryable is True and "another task" in e.reason)
(comfy / "custom_nodes" / "Mine").mkdir(parents=True)
mine = publish("mine1", "comfy_nodes", {"custom_nodes/Mine/__init__.py": b""})
e = fails([mine])
check("a node folder ComfyUI already has (not ours) is never replaced", e and "not ours" in e.reason)

print("\nservice bundles")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


port = free_port()
serve = (b"#!/bin/sh\ncd \"$BUNDLE_DIR\"\nmkdir -p www/health && echo ok > www/health/index.html\n"
         b"exec " + sys.executable.encode() + b" -m http.server \"$PORT\" --bind 127.0.0.1 --directory www\n")
svc = publish("svc1", "service", {"bin/serve": (serve, 0o755)})
svc["start"] = {"service": {"port": port, "health": "/health/", "entry": "bin/serve"}}
b = make()
u = b.ensure([svc])
check("a service bundle is started and answers its health", u[0]["fresh"] and u[0]["start_s"] > 0
      and b.services["svc1"]["proc"].poll() is None)
u = b.ensure([svc])
check("a running service is reused", u[0]["fresh"] is False and u[0]["start_s"] < 1)
os.killpg(b.services["svc1"]["proc"].pid, 9)
time.sleep(0.5)
b.ensure([svc])
check("a service that died is started again before the next task",
      b.services["svc1"]["proc"].poll() is None)
dead = publish("dead1", "service", {"bin/serve": (b"#!/bin/sh\nexit 3\n", 0o755)})
dead["start"] = {"service": {"port": free_port(), "health": "/health"}}
e = fails([dead], b)
check("a service that exits at once fails the task in words", e and "exited (3)" in e.reason)
check("the pod holds every bundle it fetched", set(b.loaded()) >= {"bundle:svc1"})
b.close()
check("close() stops the services", not b.services)
srv.shutdown()

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
