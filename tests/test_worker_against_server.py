"""podh-worker against the REAL pod-control Task API, in-process: no fake task routes.

pod-control's FastAPI app runs under uvicorn on 127.0.0.1 with a temp DB and config, a
fake provider (no RunPod) and a presigner that points at a local bucket stand-in (no S3).
A pool and a pod are made the way pod-control's own tests make them (pools.launch +
dispatch, the pod's token captured from queue.pod_env, /v1/boot ready); tasks are
submitted through /v1/tasks; then podh-worker's loop (--once) claims and runs each against
the fake ComfyUI, with the pod's token, over HTTP. Last, pod logs (APP-API §9): an app's
log request carried on the pod's /v1/alive reply and answered with POST /v1/pod/logs, then
the end-of-pod upload — skipped with no LOG_STORE (404), and through presigned PUTs once
LOG_STORE is set. Nothing leaves the machine.

    POD_CONTROL_SRC=/path/to/pod-control/src  (default: ../pod-control/src beside this repo)
    /path/to/pod-control/.venv/bin/python tests/test_worker_against_server.py

Skipped (exit 0) when pod-control's source or its dependencies (fastapi, uvicorn) are not
importable, so the pod-harness suite stands on its own.
"""
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PC = Path(os.environ.get("POD_CONTROL_SRC") or ROOT.parent / "pod-control" / "src")


def skip(why: str):
    print(f"skipped: {why}")
    sys.exit(0)


if not (PC / "pod_control" / "taskapi.py").is_file():
    skip(f"no pod-control source at {PC} (set POD_CONTROL_SRC)")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(PC))
try:
    import uvicorn
    from fastapi.testclient import TestClient
    from pod_control import config, pools, queue, s3sign, service
    from pod_control.registry import Registry
except Exception as e:                       # noqa: BLE001 — any import failure is a skip
    skip(f"pod-control is not importable here ({type(e).__name__}: {e}); "
         f"run with pod-control's .venv python")

for k in list(os.environ):
    if k.startswith("PODH_") and k != "PODH_HEARTBEAT_STOP_FILE":
        os.environ.pop(k)
from _fake_comfy import FakeComfy  # noqa: E402
from _fake_control import FakeControl  # noqa: E402
from pod_harness import heartbeat  # noqa: E402
from pod_harness.worker import Worker  # noqa: E402

ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# -- pod-control, configured the way its tests do it ---------------------------------------------
T = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(T / "stop")
(T / "master.key").write_text("MASTER\n")
(T / "clients").write_text(f"app {hashlib.sha256(b'tok-app').hexdigest()} lanes=media\n")
(T / "runpod.key").write_text("x")
creds = T / "creds"
creds.write_text("AKID SECRET\n")
creds.chmod(0o600)
(T / ".control.env").write_text(
    f"PROVIDERS = runpod\nPROVIDER_RUNPOD_KEYFILE = {T / 'runpod.key'}\n"
    f"CONTROL_TOKEN_FILE = {T / 'master.key'}\nCLIENT_TOKENS_FILE = {T / 'clients'}\n"
    f"DB_PATH = {T / 'control.db'}\nDRY_RUN = false\n"
    f"MODEL_PROFILE_MODELS = s3://models-bucket\nMODEL_PROFILE_MODELS_CREDS_FILE = {creds}\n"
    f"STORE_PROFILE_ART = s3://art-bucket/out\nSTORE_PROFILE_ART_CREDS_FILE = {creds}\n"
    f"KITS_PROFILE = models\n")
service.CFG = CFG = config.load(T / ".control.env")
service.REG = REG = Registry(CFG.db)

fc = FakeControl()                           # here only as the bucket: /blob GET, /put PUT
SIGNED = []


def fake_presign(st, method, path, *, expires=3600, now=None, raw_key=False):
    SIGNED.append((st["name"], method, path))
    key = f"{st['name']}/{path}"
    return f"{fc.url}/put/{key}" if method == "PUT" else fc.blob_url(key, sig="ok")


s3sign.presign = fake_presign


class P:
    def __init__(s, pid):
        s.pod_id, s.key, s.target, s.name = pid, f"runpod:{pid}", "runpod", pid
        s.cost_hr, s.age_sec, s.running = 0.5, 60, True

    def describe(s):
        return {"pod_id": s.pod_id}


class FakeLoader:
    def __init__(s):
        s.n, s.live = 0, []

    def create_with_capacity(s, create, **cap):
        s.n += 1
        s.live.append(P(f"pod{s.n}"))
        return {"id": f"pod{s.n}", "costPerHr": 0.5}, create

    def placement_facts(s, pod, wait_sec=0):
        return {"machine_id": "m1", "gpu_name": "NVIDIA L40", "gpu_type_id": "NVIDIA L40",
                "gpu_count": 1, "cost_hr": 0.5}

    def stop(s, pid):
        s.live = [p for p in s.live if p.pod_id != pid]
        return {}

    def list_pods(s):
        return s.live


service.LOADERS = {"runpod": FakeLoader()}
TOK = {}
_orig_env = queue.pod_env


def _capture(cfg, job, env):
    out = _orig_env(cfg, job, env)
    TOK[job["job_id"]] = out["PODH_CONTROL_TOKEN"]
    return out


queue.pod_env = _capture

with socket.socket() as s_:
    s_.bind(("127.0.0.1", 0))
    PORT = s_.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(service.app, host="127.0.0.1", port=PORT,
                                       lifespan="off", log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
if not wait_for(lambda: server.started, 15):
    print("❌ pod-control did not start"); sys.exit(1)
URL = f"http://127.0.0.1:{PORT}"
app = TestClient(service.app)                # the app's side: same process, same registry
A = {"X-Podh-Token": "tok-app"}

TEMPLATE = {"image": "img:1", "gpu": {"min_vram_gb": 24},
            "boot": {"models": {"profile": "models", "paths": []},
                     "workload": {"comfyui": {"port": 8188}}},
            "policy": {"idle_stop_s": 300}}
r = app.put("/v1/pools/media/video", headers=A,
            json={"job": TEMPLATE, "scale": {"max_pods": 1}, "match": {}})
check(f"PUT /v1/pools/media/video ({r.status_code})", r.status_code == 200)
jid = pools.launch(CFG, REG, service.LOADERS, pools.get(REG, "media", "video"), reason="test",
                   now=time.time())
for _ in range(5):
    queue.dispatch_once(CFG, REG, service.LOADERS)
    if REG.job(jid)["pod_id"]:
        break
POD_TOKEN = TOK[jid]
r = app.post("/v1/boot", json={"line": "up", "stage": "ready"}, headers={"X-Podh-Token": POD_TOKEN})
check("a pool pod is placed and booted, with its own token",
      r.status_code == 200 and REG.job(jid)["state"] == "running")

# -- the pod: fake ComfyUI, an app bundle, the worker's env ---------------------------------------
cf = FakeComfy(T / "comfy")
APP = T / "app"
(APP / "workflows").mkdir(parents=True)
GRAPH = {"3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
         "5": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
         "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "out"}}}
(APP / "workflows" / "t2i.json").write_text(json.dumps(GRAPH))
os.environ.update(PODH_CONTROL_URL=URL, PODH_CONTROL_TOKEN=POD_TOKEN,
                  RUNPOD_POD_ID=REG.job(jid)["pod_id"], PODH_MODELS_ROOT=str(T / "models"),
                  PODH_NO_ARIA2="1", PODH_COMFY_URL=cf.url, PODH_COMFY_ROOT=str(T / "comfy"),
                  PODH_APP_ROOT=str(APP), PODH_WORKER_DIR=str(T / "work"),
                  PODH_INPUT_CACHE_GB="0")
fc.blobs["art/in/start.png"] = b"PNG-start"


def submit(tid, **kw):
    body = {"task_id": tid, "lane": "media", "pool": "video", "kind": "comfy",
            "lease_s": 15,
            "workflow": {"ref": "workflows/t2i.json",
                         "patch": {"3.inputs.seed": 7, "5.inputs.image": "{input:start}"}},
            "inputs": {"start": "store://art/in/start.png"},
            "outputs": {"to": f"store://art/out/{tid}/", "keep": ["*.png"]}, **kw}
    r = app.post("/v1/tasks", json=body, headers=A)
    return r.status_code, r.json()


def run(timeout=60):
    w = Worker(poll_s=0.2, interrupt_grace=1.0, once=True, prefetch=0)
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("rc", w.run()), daemon=True)
    th.start()
    th.join(timeout)
    return w, box.get("rc", "hung")


def task(tid):
    return app.get(f"/v1/tasks/{tid}", headers=A).json()


# -- success -------------------------------------------------------------------------------------
print("— a comfy task, end to end against pod-control")
cf.mode, cf.steps, cf.step_delay, cf.outputs = "ok", 3, 0.05, ["out_00001.png"]
st, body = submit("t-ok")
check(f"POST /v1/tasks → 202 queued ({st})", st == 202 and body.get("state") == "queued")
w, rc = run()
v = task("t-ok")
check("the worker exits 0 after one task", rc == 0)
check(f"the task succeeded (state {v.get('state')}, error {v.get('error')})",
      v.get("state") == "succeeded")
check("ComfyUI ran the patched graph with the input pod-control presigned at claim",
      cf.prompts and cf.prompts[-1][1]["3"]["inputs"]["seed"] == 7
      and cf.prompts[-1][1]["5"]["inputs"]["image"] == "podh/t-ok_start.png"
      and ("art", "GET", "in/start.png") in SIGNED)
check("result: prompt id and the outputs' {name, ref}",
      (v.get("result") or {}).get("prompt_id") == cf.prompts[-1][0]
      and (v.get("result") or {}).get("outputs") ==
      [{"name": "out_00001.png", "ref": "store://art/out/t-ok/out_00001.png"}])
arts = v.get("artifacts") or []
a0 = arts[0] if arts else {}
check("artifacts: the PNG by ref under outputs.to, with size and sha256",
      [a.get("name") for a in arts] == ["out_00001.png"]
      and a0.get("ref") == "store://art/out/t-ok/out_00001.png"
      and a0.get("size") == len(fc.blobs.get("art/out/t-ok/out_00001.png", b""))
      and len(a0.get("sha256") or "") == 64)
check("the bytes went to the presigned PUT from POST …/outputs",
      ("art", "PUT", "out/t-ok/out_00001.png") in SIGNED
      and fc.blobs.get("art/out/t-ok/out_00001.png", b"").endswith(b"out_00001.png"))
u = v.get("usage") or {}
check("usage: cost, wall, and the pod's fetch/run/upload phases",
      isinstance(u.get("cost_usd"), (int, float)) and u.get("wall_s", 0) > 0
      and set(u.get("phases") or {}) >= {"fetch_s", "run_s", "upload_s"})
ev = app.get("/v1/events", params={"job_id": "t-ok"}, headers=A)
kinds = [e.get("kind") for e in (ev.json().get("events") or [])] if ev.status_code == 200 else []
check(f"events: claimed → running → artifact → succeeded ({kinds})",
      all(k in kinds for k in ("task.claimed", "task.running", "task.artifact", "task.succeeded")))

# -- cancel mid-run ------------------------------------------------------------------------------
print("— cancel while it runs")
cf.mode = "stall"
i0 = cf.interrupts
st, _ = submit("t-cancel")
started = {}


def cancel_when_running():
    if wait_for(lambda: task("t-cancel").get("state") == "running", 20):
        started["ok"] = True
        time.sleep(0.5)
        started["reply"] = app.delete("/v1/tasks/t-cancel", headers=A).json()


threading.Thread(target=cancel_when_running, daemon=True).start()
w, rc = run(timeout=60)
v = task("t-cancel")
check("DELETE on the running task answers cancelled, heard in the next beat",
      started.get("ok") and (started.get("reply") or {}).get("cancelled") is True)
check(f"the worker interrupted ComfyUI and exited 0 (rc {rc})", rc == 0 and cf.interrupts > i0)
check(f"the task stays cancelled; the pod's late report changes nothing (state {v.get('state')})",
      v.get("state") == "cancelled" and not v.get("artifacts"))
check("the worker reported it as cancelled", ("t-cancel", "cancelled") in w.reports)

# -- a model the store lacks, fetched by the pod from its source (TASK-API §10) -----------------
print("— a model missing from the store, with a source: from_source")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pod_harness import fetch, local  # noqa: E402
from pod_harness.boot import models  # noqa: E402
from pod_harness.boot.core import Context  # noqa: E402

MODEL = "checkpoints/src.safetensors"
MDATA = os.urandom(64_000)
SRC_GETS = []


class Source(BaseHTTPRequestHandler):          # a stand-in for huggingface.co
    def do_GET(self):  # noqa: N802
        SRC_GETS.append((self.path, self.headers.get("Authorization")))
        if self.path != "/o/r/resolve/v1/src.safetensors":
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(MDATA)))
        self.end_headers()
        self.wfile.write(MDATA)

    def log_message(self, *a):
        pass


src = ThreadingHTTPServer(("127.0.0.1", 0), Source)
threading.Thread(target=src.serve_forever, daemon=True).start()
fetch.HF_BASE = f"http://127.0.0.1:{src.server_port}"
os.environ["HF_TOKEN"] = "hf-tok"
_orig_head = s3sign.head
s3sign.head = lambda st, path, **kw: ({"exists": False, "size": None} if path == MODEL
                                      else _orig_head(st, path, **kw))
# The pod's podh-boot side: the local API the worker asks for models.
bstate = local.LocalState()
bstate.ready = True
bctx = Context(boot_dir=T / "bd", state=bstate)
lsrv = local.LocalServer(bstate, token="lt",
                         on_models=lambda f, p_: models.ensure(bctx, f, p_)).start()
os.environ.update(PODH_LOCAL_URL=lsrv.url, PODH_LOCAL_TOKEN="lt")
cf.mode = "ok"
st, body = submit("t-src", needs={"models": [{"path": MODEL, "source": "hf://o/r/src.safetensors@v1",
                                              "sha256": hashlib.sha256(MDATA).hexdigest(),
                                              "size": len(MDATA)}]})
check(f"a task needing a model with a source is accepted ({st})", st == 202)
w, rc = run()
v = task("t-src")
check(f"the claim's from_source model was fetched from its source; the task succeeded "
      f"(state {v.get('state')}, error {v.get('error')})",
      v.get("state") == "succeeded" and (T / "models" / MODEL).read_bytes() == MDATA)
check("...the source was asked at its hf:// resolve URL with HF_TOKEN",
      SRC_GETS and SRC_GETS[0] == ("/o/r/resolve/v1/src.safetensors", "Bearer hf-tok"))
check("...and the pod put it back into the store through the presigned put_url",
      wait_for(lambda: fc.blobs.get(f"models/{MODEL}") == MDATA, 15)
      and ("models", "PUT", MODEL) in SIGNED)
s3sign.head = _orig_head
lsrv.close(); src.shutdown()

# -- pod logs (APP-API §9): a request round-trip, then the end-of-pod upload ---------------------
print("— pod logs: a request on the heartbeat reply, the full logs at the end")
import gzip  # noqa: E402
from pod_harness import podlogs  # noqa: E402
from pod_harness.boot import core  # noqa: E402

pctx = Context(boot_dir=T / "pod", state=local.LocalState())
LD = pctx.logs_dir
(LD / "workload.log").write_text("".join(f"step {i}\n" for i in range(50))
                                 + "\x1b[31mERROR\x1b[0m out of memory\n")
(LD / "worker.log").write_text("[podh-worker] claimed t-1\n")
resp = podlogs.watch(LD, say=lambda s: None)
r = app.post(f"/v1/jobs/{jid}/logs/request", json={"names": ["workload"], "lines": 3}, headers=A)
req_id = r.json().get("req_id") if r.status_code == 202 else None
check(f"the app asks for the pod's logs ({r.status_code})", r.status_code == 202 and req_id)
reply = heartbeat.alive() or {}
check("the pod's next /v1/alive reply carries the request",
      (reply.get("logs") or {}).get("req_id") == req_id
      and reply["logs"]["names"] == ["workload"] and reply["logs"]["lines"] == 3)


def tails():
    g = app.get(f"/v1/jobs/{jid}/logs", headers=A).json()
    return g if g.get("logs") else None


check("the pod pushed the tail; the request is answered (no longer pending)",
      wait_for(lambda: tails() is not None and not tails()["pending"], 15))
g = tails() or {"logs": [{}]}
check(f"the app reads the last 3 lines, colour codes stripped ({g['logs'][0].get('lines')})",
      g["logs"][0].get("name") == "workload"
      and g["logs"][0].get("lines") == ["step 48", "step 49", "ERROR out of memory"]
      and g["logs"][0].get("truncated") is False)
heartbeat.alive()
check("the answered request is gone from the next reply", "logs" not in heartbeat.last_reply())
heartbeat._REPLY_HOOKS.remove(resp)

res = podlogs.upload_all(LD, say=lambda s: None)
check(f"no LOG_STORE: the server answers 404 and the upload is skipped ({res.get('skipped')})",
      res.get("skipped") == "no log store" and not res["uploaded"])


def gunzip(key):
    try:
        return gzip.decompress(fc.blobs.get(key, b""))
    except OSError:
        return b""


CFG.log_store = "art"
n_signed = len(SIGNED)
pctx.fail(core.BootFailure("addons: a node pack would not import", stage="addons"))
lane_key = f"logs/media/{jid}"
check("with LOG_STORE: a failed stage uploads every log through presigned PUTs",
      {("art", "PUT", f"{lane_key}/{n}.log.gz") for n in ("boot", "workload", "worker")}
      <= set(SIGNED[n_signed:]))
check("…the bytes are each whole log, gzipped",
      gunzip(f"art/{lane_key}/workload.log.gz") == (LD / "workload.log").read_bytes()
      and b"addons: a node pack would not import" in gunzip(f"art/{lane_key}/boot.log.gz"))
d = app.get(f"/v1/jobs/{jid}/logs/workload/download", headers=A)
dj = d.json() if d.status_code == 200 else {}
check(f"the app gets the uploaded log's ref, size and a presigned GET ({d.status_code})",
      dj.get("ref") == f"store://art/{lane_key}/workload.log.gz"
      and dj.get("size") == len(fc.blobs.get(f"art/{lane_key}/workload.log.gz", b""))
      and dj.get("url"))
check(f"…and the pod reported done after the upload (job {REG.job(jid).get('state')})",
      REG.job(jid).get("state") not in ("running", "booting"))

server.should_exit = True
cf.close(); fc.close()
print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
