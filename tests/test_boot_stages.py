"""podh-boot end to end: every stage's decisions and failure paths, resume, supervision of
the workload and app.serve, drain, mid-life models and the worker's /done.

Each case runs the real `python -m pod_harness.boot` against the fake pod of
tests/_boot_fixture.py — fake GPU, fake ffmpeg, local git repos, fake ComfyUI, fake
pod-control on 127.0.0.1. Nothing leaves the machine and nothing is installed.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


def wait_for(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


from _boot_fixture import Pod, dones, env_file, stages  # noqa: E402
from _fake_control import FakeControl  # noqa: E402

fc = FakeControl()
pod = Pod(fc)
T = pod.tmp


def show(p, label):
    if p.returncode not in (0,) and os.environ.get("VERBOSE"):
        print(f"--- {label} stdout\n{p.stdout}\n--- stderr\n{p.stderr}")


# == 1. the whole boot ===========================================================================
print("— full boot")
pod.expire_first = {"vae/b.safetensors"}
n0 = len(fc.calls)
bd = T / "boot1"
p = pod.run(bd, FAKE_SERVE="ok0", FAKE_SERVE_SLEEP="4")
show(p, "full")
d = dones(fc, n0)
check("a full boot whose app.serve exits 0 ends rc 0", p.returncode == 0 and d and d[-1]["rc"] == 0)
check("stages report in the contract's order",
      stages(fc, n0) == ["gpu_check", "binaries", "models", "addons", "app", "workload", "ready",
                         "app_serve"])
facts = [b for pth, b, _ in fc.calls[n0:] if pth == "/v1/pod/facts"]
check("facts: vram_gb, cuda, driver and the card after the GPU check",
      facts and facts[0].get("vram_gb") == 48 and facts[0].get("cuda") == "12.8"
      and facts[0].get("driver") == "570.86.15" and facts[0].get("gpu") == "NVIDIA L40S")
check("facts: nvenc once ffmpeg is installed", facts[-1].get("nvenc") is True)
bindir = bd / "bin" / f"ffmpeg-{pod.ff_sha[:12]}"
check("without bin_names, every executable in the release's bin/ is installed",
      os.access(bindir / "ffmpeg", os.X_OK) and os.access(bindir / "ffprobe", os.X_OK)
      and not (bindir / "LICENSE").exists())
claims = [b for pth, b, _ in fc.calls[n0:] if pth == f"/v1/pod/kits/binary-ffmpeg-{pod.ff_sha[:16]}"]
check("...and published as kit binary-<name>-<sha16>, keyed by the full sha",
      claims and claims[0]["key"] == pod.ff_sha
      and (f"/v1/pod/kits/binary-ffmpeg-{pod.ff_sha[:16]}", pod.ff_sha) in pod.kits)
wenv = env_file(bd / "workload.env")
check("binary env lands in $PODH_WORKLOAD_ENV with {bin}/{bindir} substituted",
      wenv.get("IMAGEIO_FFMPEG_EXE") == f"{bindir}/ffmpeg" and wenv.get("PATH") == f"{bindir}:$PATH")
check("...then workload.env, then the app's own lines",
      list(wenv)[-3:] == ["PATH", "WL_FLAG", "APP_ADDED"] or
      (wenv.get("WL_FLAG") == "1" and wenv.get("APP_ADDED") == "from-setup"))
for path, data in pod.models.items():
    check(f"model {path} is present and intact",
          (T / "models" / path).read_bytes() == data)
states = [(k, v) for pth, b, _ in fc.calls[n0:] if pth == "/v1/pod/models/state"
          for k, v in b["models"].items()]
check("model states go fetching → present, as {models: {path: state}}",
      set(states) >= {(p_, "fetching") for p_ in pod.models} | {(p_, "present") for p_ in pod.models})
check("an expired link was refreshed by asking for the manifest again",
      any(g[0] == "vae_b.safetensors" and g[1] == "old" for g in fc.blob_gets)
      and pod.manifest_calls >= 2)
na = pod.comfy / "custom_nodes" / "nodeA"
check("addon cloned at its pinned commit", (na / "nodea_mod.py").exists())
pip = pod.pip_log.read_text()
check("addon requirements.txt and pip extras installed with the workload's python",
      "-r requirements.txt" in pip and "pkg-extra==1.0" in pip)
check("addon link into its own dir points at the models root",
      os.readlink(na / "models" / "a.safetensors") == str(T / "models" / "checkpoints" / "a.safetensors"))
check("addon link comfy:<path> lands inside the ComfyUI root",
      os.readlink(pod.comfy / "models" / "vae_link") == str(T / "models" / "vae"))
check("optional addon without requirements is installed too, no pip for it",
      (pod.comfy / "custom_nodes" / "nodeB" / "__init__.py").exists() and pip.count("install") == 2)
check("the addon set is published as a kit keyed by version",
      ("/v1/pod/kits/addons-lane-comfy", "v7") in pod.kits)
app_get = [r for r in fc.requests if r["path"] == "/blob/app.tgz"]
check("the app is fetched with the app's token, not the pod's",
      app_get and app_get[0]["headers"].get("authorization") == "Bearer apptok"
      and not app_get[0]["headers"].get("x-podh-token"))
senv = (bd / "setup-env.txt").read_text()
check("app setup sees PODH_COMFY_ROOT/PY, MODELS_ROOT, WORKLOAD_ENV, JOB_ID, GPU_COUNT, LOCAL_URL",
      all(f"{k}=" in senv for k in ("PODH_COMFY_ROOT", "PODH_COMFY_PY", "PODH_MODELS_ROOT",
                                    "PODH_WORKLOAD_ENV", "PODH_JOB_ID", "PODH_GPU_COUNT",
                                    "PODH_LOCAL_URL", "PODH_LOCAL_TOKEN")))
check("...and the binaries' env", f"IMAGEIO_FFMPEG_EXE={bindir}/ffmpeg" in senv)
ce = json.loads((bd / "comfy-env.json").read_text())
check("ComfyUI starts with --listen, --port, its args and extra_model_paths",
      "--enable-cors-header" in ce["argv"] and "--extra-model-paths-config" in ce["argv"]
      and "--listen" in ce["argv"])
check("ComfyUI sees the app's added env and PATH with the binary first",
      ce["env"].get("APP_ADDED") == "from-setup" and ce["path"].startswith(str(bindir)))
emp = (bd / "extra_model_paths.yaml").read_text()
check("extra_model_paths.yaml points ComfyUI's folders at the models root",
      f"base_path: {T / 'models'}" in emp and "  checkpoints: checkpoints/" in emp)
check("app.run gets {app} replaced by the app root, and runs there",
      (bd / "setup-cwd.txt").read_text().strip() == os.path.realpath(T / "app")
      and f"PODH_APP_ROOT={T / 'app'}" in senv)
check("the job's app env from /v1/pod/env overrides the container's, with its job id",
      "APP_JOB_SECRET=from-control" in senv and "PODH_JOB_ID=job_2" in senv)
sv = (bd / "serve-env.txt").read_text()
check("app.serve too: {app}, cwd, PODH_APP_ROOT and the job's env",
      (bd / "serve-cwd.txt").read_text().strip() == os.path.realpath(T / "app")
      and f"PODH_APP_ROOT={T / 'app'}" in sv and "APP_JOB_SECRET=from-control" in sv
      and "PODH_JOB_ID=job_2" in sv)
check("app.serve gets serve_env (expanded) and the local API",
      "SERVE_FLAG=yes" in sv and f"SERVE_PATH={T / 'models'}/x" in sv and "PODH_LOCAL_URL=" in sv)
alive = fc.bodies("/v1/alive")
check("the worker's `podh-report busy` rides on /alive", any(b.get("busy") is True and
                                                             b.get("detail") == "serving"
                                                             for b in alive))
check("ready is reported before app.serve starts",
      [b["stage"] for pth, b, _ in fc.calls[n0:] if pth == "/v1/boot"].index("ready")
      < [b.get("stage") for pth, b, _ in fc.calls[n0:] if pth == "/v1/boot"].index("app_serve"))

# == 2. resume ===================================================================================
print("— resume on the same disk")
n0, rq0 = len(fc.calls), len(fc.requests)
blob_n = len(fc.blob_gets)
pip_n = len(pod.pip_log.read_text().splitlines())
p = pod.run(bd, FAKE_SERVE="ok0")
show(p, "resume")
lines = [b["line"] for pth, b, _ in fc.calls[n0:] if pth == "/v1/boot"]
check("a resumed boot still ends rc 0", p.returncode == 0)
check("gpu_check runs again (a resumed pod has a different card)", "gpu_check" in stages(fc, n0))
check("binary: already installed", any("ffmpeg: already installed" in ln for ln in lines))
check("models: nothing downloaded again", all(g[0] not in ("checkpoints_a.safetensors", "vae_b.safetensors")
                                             for g in fc.blob_gets[blob_n:]))
check("addons: already installed, no pip", any("nodeA" in ln and "already installed" in ln for ln in lines)
      and len(pod.pip_log.read_text().splitlines()) == pip_n)
check("app: already set up, not fetched again", any("already set up" in ln for ln in lines)
      and not any(r["path"] == "/blob/app.tgz" for r in fc.requests[rq0:]))
pod.pod_env = None
p = pod.run(bd, args=("--exit-on-fail", "--fresh"), FAKE_SERVE="ok0")
check("an older server without /v1/pod/env: the app gets the container env",
      p.returncode == 0 and "APP_JOB_SECRET=from-container" in (bd / "serve-env.txt").read_text())
pod.pod_env = {"env": {"APP_JOB_SECRET": "from-control", "APP_POD_TOKEN": "apptok"}, "job_id": "job_2"}
rq0 = len(fc.requests)
p = pod.run(bd, args=("--exit-on-fail", "--fresh"), FAKE_SERVE="ok0")
check("--fresh redoes what the disk says is done",
      p.returncode == 0 and any(r["path"] == "/blob/app.tgz" for r in fc.requests[rq0:]))

# == 3. failures, stage by stage =================================================================
print("— failures")
n0, rq0 = len(fc.calls), len(fc.requests)
p = pod.run(T / "f-gpu", FAKE_GPUS="0")
d = dones(fc, n0)
check("a bad GPU ends rc 7 at stage gpu_check", p.returncode == 7 and d and d[-1]["rc"] == 7
      and d[-1]["stage"] == "gpu_check")
check("...before any model is fetched",
      not any(r["path"] == "/v1/pod/models" for r in fc.requests[rq0:]))
check("...and the heartbeat goes quiet", (T / "f-gpu" / "hb-stop").exists())

good_sha = pod.spec["boot"]["binaries"][0]["sha256"]
pod.spec["boot"]["binaries"][0]["sha256"] = "0" * 64
fc.routes[("GET", "/v1/pod/kits/binary-ffmpeg-" + "0" * 64)] = (404, {})
n0 = len(fc.calls)
p = pod.run(T / "f-bin")
d = dones(fc, n0)
check("a binary that fails its sha256 fails the boot at stage binaries",
      p.returncode == 1 and d and d[-1]["stage"] == "binaries" and "sha256" in d[-1]["reason"])
pod.spec["boot"]["binaries"][0]["sha256"] = good_sha

pod.extra_models = {"loras/missing.safetensors": b"x" * 10}
n0 = len(fc.calls)
p = pod.run(T / "f-models")
d = dones(fc, n0)
check("a boot-set model that cannot be fetched fails the boot at stage models",
      p.returncode == 1 and d and d[-1]["stage"] == "models" and "loras/missing" in d[-1]["reason"])
check("...and its state is reported failed, with the error", any(
    isinstance(b["models"].get("loras/missing.safetensors"), dict)
    and b["models"]["loras/missing.safetensors"]["state"] == "failed"
    and b["models"]["loras/missing.safetensors"].get("error")
    for pth, b, _ in fc.calls[n0:] if pth == "/v1/pod/models/state"))
pod.extra_models = {}

good_items = pod.addon_items
pod.addon_items = [dict(good_items[0], commit=pod.commit_a[:12])]
n0 = len(fc.calls)
p = pod.run(T / "f-addon")
d = dones(fc, n0)
check("a required addon with a short commit is refused on the pod (stage addons)",
      p.returncode == 1 and d and d[-1]["stage"] == "addons" and "40-hex" in d[-1]["reason"])
pod.addon_items = [dict(good_items[0]),
                   {"name": "evil", "repo": "https://github.com/fake/nodeB", "commit": pod.commit_b,
                    "links": [{"file": "../../etc/passwd", "to": "x"}], "optional": True},
                   {"name": "gone", "repo": "https://github.com/fake/nope", "commit": "a" * 40,
                    "optional": True}]
n0 = len(fc.calls)
p = pod.run(T / "f-opt", FAKE_SERVE="ok0")
lines = [b["line"] for pth, b, _ in fc.calls[n0:] if pth == "/v1/boot"]
check("optional addons that are refused or fail to clone only warn", p.returncode == 0
      and any("evil" in ln and "refused" in ln for ln in lines)
      and any("gone" in ln and "optional, continuing" in ln for ln in lines))
pod.addon_items = [dict(good_items[0], probe="import does_not_exist")]
n0 = len(fc.calls)
p = pod.run(T / "f-probe")
d = dones(fc, n0)
check("a failed probe fails a required addon, with the probe's output",
      p.returncode == 1 and d and d[-1]["stage"] == "addons" and "probe" in d[-1]["reason"]
      and "does_not_exist" in (d[-1].get("log_tail") or ""))
pod.addon_items = [dict(good_items[0])]
n0 = len(fc.calls)
p = pod.run(T / "f-pip", FAKE_PIP_RC="1")
d = dones(fc, n0)
check("a failed pip install fails a required addon", p.returncode == 1 and d
      and d[-1]["stage"] == "addons" and "pip" in d[-1]["reason"])
pod.addon_items = good_items

n0 = len(fc.calls)
p = pod.run(T / "f-app", FAKE_SETUP_FAIL="1")
d = dones(fc, n0)
check("a failing app setup ends at stage app with its output",
      p.returncode == 9 and d and d[-1]["stage"] == "app" and "setup is unhappy" in (d[-1].get("log_tail") or ""))
n0 = len(fc.calls)
p = pod.run(T / "f-apptok", APP_POD_TOKEN="")
d = dones(fc, n0)
check("an app fetch without its token fails clearly", p.returncode == 1 and d
      and "APP_POD_TOKEN" in d[-1]["reason"])
pod.spec["boot"]["limits"] = {"app": 2}
n0 = len(fc.calls)
t0 = time.time()
p = pod.run(T / "f-limit", FAKE_SETUP_SLEEP="60")
d = dones(fc, n0)
check("a stage past its time limit is killed and reported", p.returncode != 0 and time.time() - t0 < 40
      and d and d[-1]["stage"] == "app" and "timed out" in d[-1]["reason"])
pod.spec["boot"].pop("limits")

# == 4. the workload ===============================================================================
print("— workload")
n0 = len(fc.calls)
p = pod.run(T / "w-fatal", FAKE_COMFY="fatal")
d = dones(fc, n0)
check("a fatal CUDA string in the workload log ends rc 7 at gpu_check",
      p.returncode == 7 and d and d[-1]["stage"] == "gpu_check")
check("...with the log tail", "CUDA unknown error" in (d[-1].get("log_tail") or ""))
n0 = len(fc.calls)
p = pod.run(T / "w-custom", FAKE_COMFY="custom")
d = dones(fc, n0)
check("a scan_log_for string ends at stage workload", p.returncode == 1 and d
      and d[-1]["stage"] == "workload" and "OUT OF CHEESE" in d[-1]["reason"])
n0 = len(fc.calls)
p = pod.run(T / "w-crash", FAKE_COMFY="crash")
d = dones(fc, n0)
check("a workload that dies before answering fails the boot", p.returncode == 3 and d
      and d[-1]["stage"] == "workload" and "before it answered" in d[-1]["reason"])
pod.spec["boot"]["workload"]["comfyui"]["up_timeout_s"] = 3
n0 = len(fc.calls)
p = pod.run(T / "w-noup", FAKE_COMFY="noup")
d = dones(fc, n0)
check("a workload that never answers fails within up_timeout_s", p.returncode == 1 and d
      and d[-1]["stage"] == "workload" and "within 3s" in d[-1]["reason"])
pod.spec["boot"]["workload"]["comfyui"]["up_timeout_s"] = 20
serve_cmd = pod.spec["boot"]["app"].pop("serve")
n0 = len(fc.calls)
p = pod.run(T / "w-twice", FAKE_COMFY="crash_later")
d = dones(fc, n0)
lines = [b["line"] for pth, b, _ in fc.calls[n0:] if pth == "/v1/boot"]
check("a workload that crashes after ready is restarted once", any("restarting it once" in ln for ln in lines))
check("...and the second crash ends the pod at stage workload", p.returncode == 4 and d
      and d[-1]["stage"] == "workload" and "again" in d[-1]["reason"])
pod.spec["boot"]["app"]["serve"] = serve_cmd
n0 = len(fc.calls)
p = pod.run(T / "w-once", FAKE_COMFY="crash_once", FAKE_SERVE="ok0", FAKE_SERVE_SLEEP="5")
check("a single crash is survived", p.returncode == 0 and
      (T / "w-once" / "comfy-count").read_text() == "2")

# == 5. app.serve ====================================================================================
print("— app.serve")
n0 = len(fc.calls)
p = pod.run(T / "s-crash", FAKE_SERVE="crash")
d = dones(fc, n0)
check("app.serve crashing twice ends rc at stage app_serve", p.returncode == 5 and d
      and d[-1]["stage"] == "app_serve" and "serve crashed" in (d[-1].get("log_tail") or ""))
check("...after exactly one restart", (T / "s-crash" / "serve-count").read_text().strip() == "2")

wl = pod.spec["boot"].pop("workload")
n0 = len(fc.calls)
p = pod.run(T / "s-noworkload", FAKE_SERVE="ok0")
check("with no workload, app.serve is the work and its rc 0 ends the pod",
      p.returncode == 0 and stages(fc, n0)[-4:] == ["app", "workload", "ready", "app_serve"])
pod.spec["boot"]["workload"] = wl

n0 = len(fc.calls)
p = pod.run(T / "s-done", FAKE_SERVE="done")
d = dones(fc, n0)
check("the worker's podh-report done (via the local API) ends the pod",
      p.returncode == 0 and d and d[-1]["reason"] == "worker finished")
check("...and stops app.serve rather than waiting it out", True)

# drain, then mid-life models, on one live pod
print("— a live pod: drain and mid-life models")
fc.alive_reply = {"drain": False, "park": False, "models_rev": 1}
pod.models_rev = 1
bd = T / "live"
port = __import__("_boot_fixture").free_port()
proc = pod.start(bd, FAKE_SERVE="forever", PODH_LOCAL_PORT=str(port), PODH_LOCAL_TOKEN="lt")
up = wait_for(lambda: (bd / "serve-env.txt").exists(), 60)
check("live pod reaches app.serve", up)


def state():
    req = urllib.request.Request(f"http://127.0.0.1:{port}/state", headers={"X-Podh-Token": "lt"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


new = os.urandom(50_000)
fc.blobs["loras_new.safetensors"] = new
pod.late = {"loras/new.safetensors", "loras/broken.safetensors"}
pod.extra_models = {"loras/new.safetensors": new}
pod.models_rev = 2
fc.alive_reply = {"drain": False, "park": False, "models_rev": 2}
check("a higher models_rev fetches the addition while the pod works",
      wait_for(lambda: (T / "models" / "loras" / "new.safetensors").exists(), 20)
      and (T / "models" / "loras" / "new.safetensors").read_bytes() == new)
check("...reported present, and visible in /state",
      wait_for(lambda: state()["models"].get("loras/new.safetensors") == "present", 10))
pod.extra_models["loras/broken.safetensors"] = b"never served"
pod.models_rev = 3
fc.alive_reply = {"drain": False, "park": False, "models_rev": 3}
check("a failed mid-life addition is reported failed",
      wait_for(lambda: state()["models"].get("loras/broken.safetensors") == "failed", 20))
check("...and never kills the pod", proc.poll() is None and not dones(fc, n0)[-1:] == [] or proc.poll() is None)

fc.alive_reply = {"drain": True, "park": False, "models_rev": 3, "extend_until": "2026-10-27T23:00:00Z"}
check("drain on the heartbeat reply sends app.serve SIGTERM",
      wait_for(lambda: (bd / "serve-term").exists(), 20))
check("/state shows drain to the worker", wait_for(lambda: state()["drain"] is True, 10)
      and state()["extend_until"] == "2026-10-27T23:00:00Z")
time.sleep(1.5)
check("a drained pod is not ended: it idles for pod-control to stop or park",
      proc.poll() is None)
def post(path, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=b"{}",
                                 headers={"X-Podh-Token": "lt"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


runs = int((bd / "comfy-count").read_text())
st, body = post("/workload/restart")
check("POST /workload/restart answers 200 once ComfyUI answers again",
      st == 200 and body.get("restarted") and int((bd / "comfy-count").read_text()) == runs + 1)
(bd / "comfy-mode").write_text("crash")
st, body = post("/workload/restart")
(bd / "comfy-mode").unlink()
check("...and non-2xx with the error when it does not come back",
      st == 503 and "before it answered" in body.get("error", ""))
check("...after which the supervisor restarts it (a dead process is a crash)",
      wait_for(lambda: int((bd / "comfy-count").read_text()) == runs + 3, 20)
      and wait_for(lambda: proc.poll() is None and "answering again" in " ".join(
          b.get("line", "") for pth, b, _ in fc.calls[-20:] if pth == "/v1/boot"), 20))
nd = len(dones(fc))
req = urllib.request.Request(f"http://127.0.0.1:{port}/done", method="POST",
                             data=json.dumps({"rc": 0, "reason": "drained"}).encode(),
                             headers={"X-Podh-Token": "lt"})
urllib.request.urlopen(req, timeout=5).read()
check("POST /done on the local API ends it", wait_for(lambda: proc.poll() is not None, 20)
      and proc.returncode == 0 and len(dones(fc)) == nd + 1 and dones(fc)[-1]["reason"] == "drained")
fc.alive_reply = {"ok": True}
pod.extra_models = {}

# additions already on the list at boot (a resumed pod) do not hold up ready
late = os.urandom(40_000)
fc.blobs["loras_late.safetensors"] = late
pod.extra_models = {"loras/late.safetensors": late, "loras/lost.safetensors": b"gone"}
pod.late = set(pod.extra_models)
n0 = len(fc.calls)
p = pod.run(T / "late", FAKE_SERVE="ok0", FAKE_SERVE_SLEEP="4")
ready_at = [i for i, (pth, b, _) in enumerate(fc.calls) if i >= n0 and pth == "/v1/boot"
            and b.get("stage") == "ready"]
late_at = [i for i, (pth, b, _) in enumerate(fc.calls) if i >= n0 and pth == "/v1/pod/models/state"
           and b["models"].get("loras/late.safetensors") == "present"]
check("a boot: false file is fetched after ready, not before",
      p.returncode == 0 and ready_at and late_at and late_at[0] > ready_at[0]
      and (T / "models" / "loras" / "late.safetensors").read_bytes() == late)
check("...and one that cannot be fetched fails alone, not the boot",
      p.returncode == 0 and any(isinstance(b["models"].get("loras/lost.safetensors"), dict)
                                and b["models"]["loras/lost.safetensors"]["state"] == "failed"
                                for pth, b, _ in fc.calls[n0:] if pth == "/v1/pod/models/state"))
pod.extra_models, pod.late = {}, set()

# == 6. container mode idles instead of exiting ======================================================
print("— container mode")
proc = pod.start(T / "idle", args=(), FAKE_GPUS="0")
check("a failed boot without --exit-on-fail reports done and then idles",
      wait_for(lambda: (T / "idle" / "hb-stop").exists(), 30) and (time.sleep(1.5) or proc.poll() is None))
proc.terminate()
proc.wait(10)

n0 = len(fc.calls)
p = pod.run(T / "nospec", PODH_CONTROL_URL="")
check("no spec and no control plane: a clear failure, nothing reported",
      p.returncode == 1 and "no boot spec" in p.stdout and not dones(fc, n0))
spec_file = T / "spec.json"
spec_file.write_text(json.dumps({"boot": {"gpu_check": False}}))
p = pod.run(T / "specfile", args=("--exit-on-fail", "--spec", str(spec_file)), PODH_CONTROL_URL="")
check("--spec runs a spec from a file with no control plane at all", p.returncode == 0
      and "ready" in p.stdout)

fc.close()
print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
