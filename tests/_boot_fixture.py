"""A whole pod for podh-boot tests, on a laptop, with nothing leaving it.

  - nvidia-smi and the CUDA probe interpreter: fake executables on PATH (FAKE_GPUS, …)
  - ffmpeg: a fake inside a release-shaped tarball, served as a blob; FAKE_NVENC=1 makes
    its h264_nvenc encode succeed
  - addons: real git repos on disk, reached as https://github.com/fake/<name> through a git
    `insteadOf` rewrite in a private GIT_CONFIG_GLOBAL — the pod-side validation sees GitHub
    URLs, git fetches from the local directory
  - the workload's python: a wrapper that logs `-m pip` instead of installing anything and
    runs everything else with this interpreter
  - ComfyUI: a main.py that serves HTTP on --port, misbehaving on FAKE_COMFY
  - the app: a tarball with a setup script and a serve script (FAKE_SERVE)
  - pod-control: tests/_fake_control.py, with the /v1/pod/* routes filled in here

Not a test itself (no test_ prefix).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def tgz(files: dict) -> bytes:
    """{name: (text, mode)} → tar.gz bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, (text, mode) in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), mode
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def make_repo(root: Path, name: str, files: dict) -> str:
    d = root / "fake" / name
    d.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(d))
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    git("add", "-A", cwd=d)
    git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "init", cwd=d)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=d)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True,
                          text=True, check=True).stdout.strip()


FAKE_COMFY = r'''
import json, os, sys, time, http.server, threading
a = sys.argv[1:]
port = int(a[a.index("--port") + 1])
mode = os.environ.get("FAKE_COMFY", "ok")
count_file = os.environ["FAKE_COMFY_COUNT"]
n = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
open(count_file, "w").write(str(n))
out = os.environ.get("FAKE_COMFY_ENVOUT")
if out:
    json.dump({"argv": a, "env": {k: v for k, v in os.environ.items()
                                  if k.startswith(("PODH_", "APP_", "IMAGEIO", "WL_"))},
               "path": os.environ.get("PATH", "")}, open(out, "w"))
print(f"fake ComfyUI run {n} mode {mode}", flush=True)
if mode == "crash":
    sys.exit(3)
if mode == "noup":
    time.sleep(3600)
if mode == "fatal":
    print("RuntimeError: CUDA unknown error - this may be due to an incorrectly set up environment", flush=True)
if mode == "custom":
    print("ERROR: OUT OF CHEESE", flush=True)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *x): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
if mode == "crash_later" or (mode == "crash_once" and n == 1):
    time.sleep(1.0)
    print("fake ComfyUI: segfault (pretend)", flush=True)
    os._exit(4)
while True:
    time.sleep(1)
'''

SETUP = r'''#!/bin/bash
set -e
env | grep -E '^(PODH_|IMAGEIO|WL_)' | sort > "$PODH_BOOT_DIR/setup-env.txt"
echo "APP_ADDED=from-setup" >> "$PODH_WORKLOAD_ENV"
mkdir -p "$PODH_COMFY_ROOT/custom_nodes/app-shim"
echo "# dropped by the app" > "$PODH_COMFY_ROOT/custom_nodes/app-shim/__init__.py"
[ -n "$FAKE_SETUP_SLEEP" ] && sleep "$FAKE_SETUP_SLEEP"
[ -n "$FAKE_SETUP_FAIL" ] && { echo "setup is unhappy"; exit 9; }
echo setup ok
'''

SERVE = r'''#!/bin/bash
mode="${FAKE_SERVE:-ok0}"
env | grep -E '^(PODH_|SERVE_|APP_)' | sort > "$PODH_BOOT_DIR/serve-env.txt"
count="$PODH_BOOT_DIR/serve-count"
n=$(( $(cat "$count" 2>/dev/null || echo 0) + 1 )); echo $n > "$count"
echo "serve run $n mode $mode"
"$PYTHON_FOR_TESTS" -m pod_harness.report busy --detail "serving" || echo "busy report failed"
case "$mode" in
  ok0)   sleep "${FAKE_SERVE_SLEEP:-1}"; exit 0 ;;
  crash) sleep 0.5; echo "serve crashed"; exit 5 ;;
  forever)
    trap 'echo "got TERM"; echo term > "$PODH_BOOT_DIR/serve-term"; exit 0' TERM
    while true; do sleep 0.2; done ;;
  done) "$PYTHON_FOR_TESTS" -m pod_harness.report done 0 --reason "worker finished"; sleep 30 ;;
esac
'''


class Pod:
    def __init__(self, fc):
        self.fc = fc
        self.tmp = Path(tempfile.mkdtemp(prefix="podh-boot-test-"))
        t = self.tmp
        self.bin = t / "bin"
        exe(self.bin / "nvidia-smi", r'''#!/bin/sh
[ "${FAKE_GPUS:-0}" -gt 0 ] || [ "$1" != "-L" ] || exit 0
case "$1" in
  -L) i=0; while [ $i -lt "${FAKE_GPUS:-0}" ]; do echo "GPU $i: NVIDIA L40S (UUID: GPU-0000$i)"; i=$((i+1)); done ;;
  --query-gpu=driver_version) echo "570.86.15" ;;
  --query-gpu=memory.total) echo "49140" ;;
  *) echo "| NVIDIA-SMI 570.86.15   Driver Version: 570.86.15   CUDA Version: 12.8 |" ;;
esac
''')
        exe(self.bin / "fakeprobe", r'''#!/bin/sh
echo "PODH_PROBE {\"torch\": \"2.7.1\", \"cuda\": \"12.8\", \"count\": 1, \"name\": \"NVIDIA L40S\"}"
''')
        self.pip_log = t / "pip.log"
        self.py = exe(self.bin / "workpython", f'''#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  echo "$@" >> "{self.pip_log}"
  exit "${{FAKE_PIP_RC:-0}}"
fi
exec "{sys.executable}" "$@"
''')
        # ComfyUI
        self.comfy = t / "ComfyUI"
        (self.comfy / "custom_nodes").mkdir(parents=True)
        (self.comfy / "main.py").write_text(FAKE_COMFY)
        # ffmpeg release tarball
        ff = r'''#!/bin/sh
case "$*" in *h264_nvenc*) [ "$FAKE_NVENC" = "1" ] && exit 0; echo "Cannot load libnvidia-encode.so.1" >&2; exit 1 ;; esac
echo "ffmpeg version n8.1-fake"
'''
        self.ff_tar = tgz({"ffmpeg-n8.1-linux64-gpl/bin/ffmpeg": (ff, 0o755),
                           "ffmpeg-n8.1-linux64-gpl/bin/ffprobe": ("#!/bin/sh\n", 0o755),
                           "ffmpeg-n8.1-linux64-gpl/LICENSE": ("gpl", 0o644)})
        fc.blobs["ffmpeg.tar.gz"] = self.ff_tar
        self.ff_sha = hashlib.sha256(self.ff_tar).hexdigest()
        # models
        self.models = {"checkpoints/a.safetensors": os.urandom(300_000),
                       "vae/b.safetensors": os.urandom(120_000)}
        for p, data in self.models.items():
            fc.blobs[p.replace("/", "_")] = data
        self.expire_first = set()     # paths whose FIRST manifest link is already expired
        fc.expired.add("old")
        self.manifest_calls = 0
        self.extra_models: dict = {}
        self.late: set = set()        # paths the manifest marks "boot": false (mid-life)
        self.models_rev = 1
        # addons (git repos reached through insteadOf)
        self.repos = t / "repos"
        self.commit_a = make_repo(self.repos, "nodeA", {
            "__init__.py": "NODE_CLASS_MAPPINGS = {}\n",
            "nodea_mod.py": "OK = True\n",
            "requirements.txt": "einops==0.8.0\n"})
        self.commit_b = make_repo(self.repos, "nodeB", {"__init__.py": "\n"})
        (self.repos / "fake" / "nodeB.git").symlink_to("nodeB")   # GitHub answers both
        self.gitconfig = t / "gitconfig"
        self.gitconfig.write_text(f'[url "file://{self.repos}/"]\n\tinsteadOf = https://github.com/\n'
                                  f'[protocol "file"]\n\tallow = always\n'
                                  f'[init]\n\tdefaultBranch = main\n')
        self.addon_items = [
            {"name": "nodeA", "repo": "https://github.com/fake/nodeA", "commit": self.commit_a,
             "pip": ["pkg-extra==1.0"], "requirements": True,
             "links": [{"file": "checkpoints/a.safetensors", "to": "models/a.safetensors"},
                       {"file": "vae", "to": "comfy:models/vae_link"}],
             "probe": "import nodea_mod; assert nodea_mod.OK"},
            {"name": "nodeB", "repo": "https://github.com/fake/nodeB.git", "commit": self.commit_b,
             "requirements": False, "optional": True}]
        # app
        fc.blobs["app.tgz"] = tgz({"setup.sh": (SETUP, 0o755), "serve.sh": (SERVE, 0o755)})
        # kits: an in-memory store behind the claim → PUT → commit flow
        self.kits: dict = {}
        self.pending: dict = {}
        self.spec = self.full_spec()
        self.wire()

    def full_spec(self) -> dict:
        fc = self.fc
        return {"boot": {
            "gpu_check": True,
            "binaries": [{"name": "ffmpeg", "url": fc.blob_url("ffmpeg.tar.gz"),
                          "sha256": self.ff_sha, "archive_path": "ffmpeg-*/bin",
                          "bin_names": ["ffmpeg", "ffprobe"],
                          "env": {"IMAGEIO_FFMPEG_EXE": "{bin}/ffmpeg", "PATH": "{bindir}:$PATH"}}],
            "models": {"profile": "p", "paths": list(self.models)},
            "addons": {"set": "lane/comfy", "version": 7},
            "app": {"fetch": {"url": fc.blob_url("app.tgz"), "auth_env": "APP_POD_TOKEN"},
                    "run": ["bash", "setup.sh"], "dir": str(self.tmp / "app"),
                    "serve": ["bash", str(self.tmp / "app" / "serve.sh")],
                    "serve_env": {"SERVE_FLAG": "yes", "SERVE_PATH": "$PODH_MODELS_ROOT/x"},
                    "serve_grace_s": 5},
            "workload": {"comfyui": {"port": free_port(), "args": ["--enable-cors-header"],
                                     "up_timeout_s": 20},
                         "env": {"WL_FLAG": "1"},
                         "scan_log_for": ["OUT OF CHEESE"]}},
            "policy": {"idle_stop_s": 300}}

    def wire(self):
        fc = self.fc
        fc.routes[("GET", "/v1/pod/spec")] = lambda req: (200, self.spec)

        def manifest(req):
            self.manifest_calls += 1
            items = []
            for p, data in {**self.models, **self.extra_models}.items():
                sig = "old" if p in self.expire_first and self.manifest_calls == 1 else f"s{self.manifest_calls}"
                items.append({"path": p, "url": fc.blob_url(p.replace("/", "_"), sig),
                              "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                              "boot": p not in self.late})
            return 200, {"job_id": "job_1", "models_rev": self.models_rev, "files": items}
        fc.routes[("GET", "/v1/pod/models")] = manifest
        fc.routes[("GET", "/v1/pod/addons")] = lambda req: (
            200, {"set": "lane/comfy", "version": 7, "items": self.addon_items})

        def kit_get(req):
            k = self.kits.get((req["path"], req["query"].get("key")))
            if not k:
                return 404, {"detail": "none"}
            return 200, {"url": fc.blob_url(k["blob"]), "sha256": k["sha256"], "size": k["size"]}

        def kit_claim(req):
            if (req["path"], req["body"]["key"]) in self.kits:
                return 409, {"error": "already published"}
            blob = "kit-" + req["path"].rsplit("/", 1)[1] + "-" + req["body"]["key"]
            cid = f"c{len(self.pending) + len(self.kits)}"
            self.pending[cid] = (req["path"], blob, req["body"])
            return 200, {"claim_id": cid, "put_url": f"{fc.url}/put/{blob}", "expires_s": 3600}

        def kit_commit(req):
            got = self.pending.pop(req["body"].get("claim_id"), None)
            if not got:
                return 404, {"error": "no such pending claim"}
            path, blob, claim = got
            self.kits[(path, claim["key"])] = {"blob": blob, "sha256": claim["sha256"],
                                               "size": claim["size"]}
            return 200, {"published": True}
        self.kit_routes = (kit_get, kit_claim, kit_commit)
        for name in (f"binary-ffmpeg-{self.ff_sha[:16]}", "addons-lane-comfy"):
            fc.routes[("GET", f"/v1/pod/kits/{name}")] = kit_get
            fc.routes[("POST", f"/v1/pod/kits/{name}")] = kit_claim
            fc.routes[("POST", f"/v1/pod/kits/{name}/commit")] = kit_commit

    def env(self, boot_dir: Path, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PODH_", "FAKE_", "RUNPOD_", "GIT_"))}
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(ROOT / "src"), "PYTHON_FOR_TESTS": sys.executable,
            "PODH_CONTROL_URL": self.fc.url, "PODH_CONTROL_TOKEN": "tok",
            "RUNPOD_POD_ID": "pod1", "PODH_JOB_ID": "job_1", "PODH_GPU_COUNT": "1",
            "PODH_GPUCHECK_PYTHON": "fakeprobe", "PODH_HEARTBEAT_SEC": "1",
            "PODH_HEARTBEAT_STOP_FILE": str(boot_dir / "hb-stop"),
            "PODH_BOOT_DIR": str(boot_dir), "PODH_MODELS_ROOT": str(self.tmp / "models"),
            "PODH_COMFY_ROOT": str(self.comfy), "PODH_COMFY_PY": str(self.py),
            "PODH_NO_ARIA2": "1", "PODH_KIT_FORMAT": "gz",
            "GIT_CONFIG_GLOBAL": str(self.gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
            "APP_POD_TOKEN": "apptok", "FAKE_NVENC": "1", "FAKE_GPUS": "1",
            "FAKE_COMFY_COUNT": str(boot_dir / "comfy-count"),
            "FAKE_COMFY_ENVOUT": str(boot_dir / "comfy-env.json"),
        })
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run(self, boot_dir: Path, args=("--exit-on-fail",), timeout=120, **env):
        boot_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.run([sys.executable, "-m", "pod_harness.boot", *args],
                              env=self.env(boot_dir, **env), capture_output=True, text=True,
                              timeout=timeout)

    def start(self, boot_dir: Path, args=("--exit-on-fail",), **env):
        boot_dir.mkdir(parents=True, exist_ok=True)
        log = open(boot_dir / "podh-boot.out", "w")
        return subprocess.Popen([sys.executable, "-m", "pod_harness.boot", *args],
                                env=self.env(boot_dir, **env), stdout=log,
                                stderr=subprocess.STDOUT, text=True)


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def stages(fc, since: int = 0) -> list:
    """The boot-line stages from call index `since`, consecutive repeats collapsed."""
    out = []
    for p, b, _ in fc.calls[since:]:
        if p == "/v1/boot":
            s = b.get("stage")
            if not out or out[-1] != s:
                out.append(s)
    return out


def dones(fc, since: int = 0) -> list:
    return [b for p, b, _ in fc.calls[since:] if p == "/v1/done"]


def env_file(path: Path) -> dict:
    out = {}
    for ln in path.read_text().splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k] = v
    return out


__all__ = ["Pod", "stages", "dones", "env_file", "free_port", "json", "tgz", "exe"]
