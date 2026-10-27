"""podh-worker end to end, in-process, against a fake pod-control (with the task routes), a
fake ComfyUI (real websocket frames) and a real podh-boot local API. Nothing leaves the
machine.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
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


for k in list(os.environ):
    if k.startswith("PODH_") and k != "PODH_HEARTBEAT_STOP_FILE":
        os.environ.pop(k)
from _fake_comfy import FakeComfy  # noqa: E402
from _fake_control import FakeControl  # noqa: E402
from _fake_tasks import FakeTasks  # noqa: E402
from pod_harness import comfy, heartbeat, local  # noqa: E402
from pod_harness.boot import models  # noqa: E402
from pod_harness.boot.core import Context  # noqa: E402
from pod_harness.worker import Worker  # noqa: E402
from pod_harness.worker.handlers import TaskFailed, apply_patch  # noqa: E402

T = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(T / "stop")
fc = FakeControl()
ft = FakeTasks(fc, lease_s=1.5)
cf = FakeComfy(T / "comfy")
APP = T / "app"
(APP / "workflows").mkdir(parents=True)
GRAPH = {"3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
         "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a.safetensors"}},
         "5": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
         "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "out"}}}
(APP / "workflows" / "t2i.json").write_text(json.dumps(GRAPH))
(APP / "workflows" / "t2i.map.json").write_text(json.dumps({"seed": "3.inputs.seed",
                                                             "image": "5.inputs.image"}))
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="tok", RUNPOD_POD_ID="pod1",
                  PODH_MODELS_ROOT=str(T / "models"), PODH_NO_ARIA2="1",
                  PODH_COMFY_URL=cf.url, PODH_COMFY_ROOT=str(T / "comfy"),
                  PODH_APP_ROOT=str(APP), PODH_WORKER_DIR=str(T / "work"))

state = local.LocalState()
bctx = Context(boot_dir=T / "bd", state=state)
busy_log = []
srv = local.LocalServer(state, token="lt", on_restart=cf.restart,
                        on_models=lambda f, p: models.ensure(bctx, f, p))
srv.on_busy = lambda: busy_log.append(state.busy)
srv.start()
os.environ.update(PODH_LOCAL_URL=srv.url, PODH_LOCAL_TOKEN="lt")
fc.blobs["start.png"] = b"PNG-start"


def task(tid, **kw):
    t = {"task_id": tid, "kind": "comfy", "state": "claimed", "attempt": 1,
         "workflow": {"ref": "workflows/t2i.json",
                      "patch": {"3.inputs.seed": 71560655, "5.inputs.image": "{input:start}"}},
         "inputs": {"start": fc.blob_url("start.png", sig=f"s-{tid}")},
         "outputs": {"to": f"store://art/{tid}/", "keep": ["*.png"]}}
    t.update(kw)
    return t


def run(timeout=30, **kw):
    """Run a Worker to its end in a thread; returns (worker, rc)."""
    args = dict(poll_s=0.2, interrupt_grace=1.0, once=True, prefetch=0)
    args.update(kw)
    w = Worker(**args)
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("rc", w.run()), daemon=True)
    th.start()
    th.join(timeout)
    return w, box.get("rc", "hung")


def reset():
    cf.mode, cf.ignore_interrupt, cf.ws, cf.steps, cf.step_delay = "ok", False, True, 3, 0.05
    cf.outputs, cf.subfolder = ["out_00001.png"], ""


# -- happy path ----------------------------------------------------------------------------------
print("— claim → start → inputs → run → upload → done")
reset()
ft.queue.append(task("t1", needs={"loads": ["ckpt:a", "wf:t2i"]}))
w, rc = run()
d = ft.dones.get("t1") or {}
check("exits 0 after one task with --once", rc == 0)
check("the claim says capacity, have.models/loaded and nvenc",
      ft.claims and ft.claims[0]["capacity"] == 1 and ft.claims[0]["have"] == {"models": [], "loaded": []}
      and ft.claims[0]["nvenc"] is False and "kinds" not in ft.claims[0])
check("start was called before done", ft.starts == ["t1"])
check("done ok with the PNG output as an artifact",
      d.get("ok") is True and [a["name"] for a in d.get("artifacts", [])] == ["out_00001.png"])
art = (d.get("artifacts") or [{}])[0]
check("...as pod-control takes it: name, size, sha256 (no ref: the server makes it)",
      set(art) == {"name", "size", "sha256"} and art.get("size") == len(fc.blobs.get("t1_out_00001.png", b"x"))
      and len(art.get("sha256") or "") == 64)
check("...and pod-control files it under outputs.to",
      ft.final.get("t1", [{}])[0].get("ref") == "store://art/t1/out_00001.png")
check("the PUT link was asked of POST …/outputs {names} and used without the pod token",
      ("t1", {"names": ["out_00001.png"]}) in ft.outputs
      and fc.blobs.get("t1_out_00001.png", b"").endswith(b"out_00001.png"))
check("the temp preview was not uploaded", not any("preview" in k for k in fc.blobs))
check("result carries the prompt id and the outputs' {name, ref}",
      d.get("result", {}).get("outputs") == [{"name": "out_00001.png", "ref": "store://art/t1/out_00001.png"}]
      and d["result"].get("prompt_id") == cf.prompts[-1][0])
g = cf.prompts[-1][1]
check("patch applied: seed set", g["3"]["inputs"]["seed"] == 71560655)
check("{input:start} became the ComfyUI input name, and the file is in input/",
      g["5"]["inputs"]["image"] == "podh/t1_start.png"
      and (T / "comfy" / "input" / "podh" / "t1_start.png").read_bytes() == b"PNG-start")
check("unpatched inputs keep the workflow's values", g["4"]["inputs"]["ckpt_name"] == "a.safetensors")
check("usage: fetch/run/upload phases", set(d.get("usage", {}).get("phases", {})) >= {"fetch_s", "run_s", "upload_s"})
check("the artifact was announced mid-run", ft.artifacts.get("t1", [{}])[0].get("name") == "out_00001.png")
check("busy flipped true while it held the task, then false", True in busy_log and busy_log[-1] is False)
check("after the run, loaded = the task's needs.loads", w.loaded == ["ckpt:a", "wf:t2i"])
check("the task's workdir is cleaned up", not (T / "work" / "t1").exists())

# -- polling instead of the websocket -------------------------------------------------------------
print("— without /ws: polls /history")
reset(); cf.ws = False
ft.queue.append(task("t-poll"))
w, rc = run()
check("a polled run succeeds too", ft.dones.get("t-poll", {}).get("ok") is True)

# -- input cache, store:// inputs, named patches, globs ------------------------------------------
print("— inputs: cache, store:// refs; workflow.map; keep globs")
reset()
gets0 = len([g for g in fc.blob_gets if g[0] == "start.png"])
ft.queue.append(task("t-cache"))
run()
check("the same input (a new signature) comes from the cache, not the bucket",
      ft.dones["t-cache"]["ok"] and len([g for g in fc.blob_gets if g[0] == "start.png"]) == gets0)
fc.blobs["ref.png"] = b"REF"
ft.queue.append(task("t-store", inputs={"start": "store://in/x/ref.png"},
                     workflow={"ref": "workflows/t2i.json", "map": "workflows/t2i.map.json",
                               "patch": {"seed": 5, "image": "{input:start}"}}))
run()
g = cf.prompts[-1][1]
check("a store:// input comes presigned in the claim ({url, ref}) and is fetched, no sign call",
      ft.dones["t-store"]["ok"] and ("ref.png", "claim", None) in fc.blob_gets
      and not any(s[0] == "t-store" for s in ft.signs))
check("workflow.map names patch the mapped inputs", g["3"]["inputs"]["seed"] == 5
      and g["5"]["inputs"]["image"] == "podh/t-store_start.png")
ft.queue.append(task("t-value", inputs={"start": fc.blob_url("start.png"), "seed": {"value": 99}},
                     workflow={"ref": "workflows/t2i.json",
                               "patch": {"3.inputs.seed": "{input:seed}", "5.inputs.image": "{input:start}"}}))
run()
check("a {value} input (a dependency's result) patches in as itself",
      ft.dones["t-value"]["ok"] and cf.prompts[-1][1]["3"]["inputs"]["seed"] == 99)
cf.outputs = ["a.png", "b.mp4", "c.txt"]
ft.queue.append(task("t-glob", outputs={"keep": ["*.mp4", "*.png"], "to": "store://art/g/",
                                         "put": {"a.png": fc.url + "/put/given_a.png"}}))
run()
check("keep globs choose the artifacts", sorted(a["name"] for a in ft.dones["t-glob"]["artifacts"]) == ["a.png", "b.mp4"])
check("a put link given in the claim is used as is, its ref from outputs.to",
      fc.blobs.get("given_a.png") and {"name": "a.png", "ref": "store://art/g/a.png"}
      in ft.dones["t-glob"]["result"]["outputs"])
cf.outputs, cf.subfolder = ["x.mp4", "x.png"], "clips/run1"
ft.queue.append(task("t-sub", outputs={"keep": ["clips/*/*.mp4"], "to": "store://art/s/"}))
run()
check("a kept output keeps its path under the output dir as its artifact name",
      [a["name"] for a in ft.dones["t-sub"]["artifacts"]] == ["clips/run1/x.mp4"]
      and ft.dones["t-sub"]["result"]["outputs"] == [{"name": "clips/run1/x.mp4",
                                                       "ref": "store://art/s/clips/run1/x.mp4"}]
      and ("t-sub", {"names": ["clips/run1/x.mp4"]}) in ft.outputs)
cf.subfolder = ""
from pod_harness.worker.context import artifact_name  # noqa: E402


def refused(n):
    try:
        artifact_name(n)
        return False
    except ValueError:
        return True


check("artifact names: relative paths normalised",
      artifact_name("a/b/c.ext") == "a/b/c.ext" and artifact_name("./a/./b.txt") == "a/b.txt"
      and artifact_name("plain.png") == "plain.png")
check("...and `..`, absolute, empty segments refused",
      all(refused(n) for n in ("../x", "a/../b", "/abs/x", "a//b", "a/", "", ".", "a\\b", None, "x" * 513, "a\tb")))
cf.outputs = ["c.txt"]
ft.queue.append(task("t-noout", outputs={"keep": ["*.mp4"]}))
run()
dn = ft.dones["t-noout"]
check("nothing matching keep fails the task at stage outputs (not a silent success)",
      dn["ok"] is False and dn["error"]["stage"] == "outputs" and dn["error"].get("log_tail"))
reset()
ft.queue.append(task("t-inline", workflow={"inline": GRAPH, "patch": {"77.inputs.seed": 1}}))
run()
check("a patch naming a node the workflow lacks fails at prepare",
      ft.dones["t-inline"]["ok"] is False and ft.dones["t-inline"]["error"]["stage"] == "prepare"
      and "77" in ft.dones["t-inline"]["error"]["reason"])
try:
    apply_patch({"1": {"inputs": {}}}, {"1.inputs.x": "{input:nope}"}, {})
    check("a placeholder for an input the task lacks is refused", False)
except TaskFailed:
    check("a placeholder for an input the task lacks is refused", True)

# -- ComfyUI failures ---------------------------------------------------------------------------
print("— ComfyUI refuses or fails")
cf.mode = "refuse"
ft.queue.append(task("t-refuse"))
run()
check("a refused graph fails at stage submit", ft.dones["t-refuse"]["error"]["stage"] == "submit")
cf.mode = "error"
ft.queue.append(task("t-err"))
run()
check("an execution error fails at stage execute with ComfyUI's message, not retryable",
      ft.dones["t-err"]["error"]["stage"] == "execute" and "out of memory" in ft.dones["t-err"]["error"]["reason"]
      and "retryable" not in ft.dones["t-err"])
cf.mode = "error"
ft.queue.append(task("t-fb", fallback="app"))
run()
check("a fallback:app task that fails on the pod is released, not failed",
      "t-fb" not in ft.dones and "failed on pod" in (ft.releases.get("t-fb") or ""))
reset()

# -- lease beats ---------------------------------------------------------------------------------
print("— lease beats")
cf.steps, cf.step_delay = 12, 0.2
ft.queue.append(task("t-beat"))
run()
bs = ft.beats.get("t-beat", [])
check(f"beats every lease_s/3 while it runs ({len(bs)} in ~2.5 s at lease 1.5 s)", len(bs) >= 3)
check("beats carry rising progress and a message",
      any(b["progress"] > 0 for b in bs) and all(0 <= b["progress"] <= 1 for b in bs)
      and any(b["message"] for b in bs))
reset()

# -- cancel --------------------------------------------------------------------------------------
print("— cancel mid-run")
cf.mode = "stall"
i0, r0 = cf.interrupts, cf.restarts
ft.queue.append(task("t-cancel"))
threading.Timer(1.0, lambda: ft.cancel.add("t-cancel")).start()
run()
dc = ft.dones.get("t-cancel", {})
check("a cancel on the beat interrupts ComfyUI and reports cancelled",
      dc.get("ok") is False and dc.get("error", {}).get("stage") == "cancelled" and cf.interrupts > i0)
check("...with no workload restart when the interrupt works", cf.restarts == r0)
cf.ignore_interrupt = True
ft.queue.append(task("t-cancel2"))
threading.Timer(1.0, lambda: ft.cancel.add("t-cancel2")).start()
run()
check("a node that ignores the interrupt gets the workload restarted, still cancelled",
      cf.restarts == r0 + 1 and ft.dones.get("t-cancel2", {}).get("error", {}).get("stage") == "cancelled")
reset()
ft.cancel.add("t-cancel3")
# a held task whose model is slow to arrive (the manifest takes 3 s to answer)
fc.blobs["never"] = b"n" * 10
fc.routes[("GET", "/v1/pod/models")] = lambda req: (time.sleep(3), (200, {"files": [
    {"path": "x/never.bin", "url": fc.blob_url("never")}]}))[1]
ft.queue.append(task("t-cancel3", state="held", needs={"models": ["x/never.bin"]}))
cf.mode = "stall"
run(timeout=10)
check("a task cancelled before it starts is dropped: no start, no done, no release",
      "t-cancel3" not in ft.starts and "t-cancel3" not in ft.dones and "t-cancel3" not in ft.releases)
reset()

# -- lost lease ----------------------------------------------------------------------------------
print("— lease lost")
cf.mode = "stall"
ft.queue.append(task("t-lost"))
i0 = cf.interrupts
threading.Timer(1.0, lambda: ft.lost.add("t-lost")).start()
run()
check("a beat reply {cancel: true, state: queued} (requeued) stops the work and reports nothing",
      "t-lost" not in ft.dones and "t-lost" not in ft.releases and cf.interrupts > i0)
i0 = cf.interrupts
ft.queue.append(task("t-lost2"))
threading.Timer(1.0, lambda: ft.gone.__setitem__("t-lost2", 404)).start()
run()
check("a 404 on the beat does the same", "t-lost2" not in ft.dones and cf.interrupts > i0)
reset()

# -- stall and timeout ---------------------------------------------------------------------------
print("— stall guard, timeout")
cf.mode = "stall"
r0 = cf.restarts
ft.queue.append(task("t-stall", stall_s=1))
run()
ds = ft.dones.get("t-stall", {})
check("no progress for stall_s: interrupted, a retryable attempt at stage stall",
      ds.get("ok") is False and ds["error"]["stage"] == "stall" and cf.restarts == r0
      and ds.get("retryable") is True)
cf.ignore_interrupt = True
ft.queue.append(task("t-stall2", stall_s=1))
run()
check("...and a wedged ComfyUI is restarted", ft.dones["t-stall2"]["error"]["stage"] == "stall"
      and cf.restarts == r0 + 1)
cf.ignore_interrupt = False
ft.queue.append(task("t-timeout", timeout_s=1))
run()
check("timeout_s fails the task at stage timeout (not retried)",
      ft.dones["t-timeout"]["error"]["stage"] == "timeout" and "retryable" not in ft.dones["t-timeout"])
reset()

# -- upload retry --------------------------------------------------------------------------------
print("— upload retry")
fc.put_fail["t-up_out_00001.png"] = 2
ft.queue.append(task("t-up"))
run()
check("two failed PUTs are retried; the third lands", ft.dones["t-up"]["ok"]
      and fc.blobs.get("t-up_out_00001.png"))
fc.put_refuse["t-up2_out_00001.png"] = 1
ft.queue.append(task("t-up2"))
run()
check("a refused (expired) link is asked for again", ft.dones["t-up2"]["ok"]
      and len([o for o in ft.outputs if o[0] == "t-up2"]) >= 2)
fc.put_fail["t-up3_out_00001.png"] = 9
ft.queue.append(task("t-up3"))
run(timeout=40)
check("an upload that keeps failing ends the attempt at stage upload, retryable",
      ft.dones["t-up3"]["error"]["stage"] == "upload" and ft.dones["t-up3"].get("retryable") is True)
fc.put_fail.clear()

# -- kinds and handlers ----------------------------------------------------------------------------
print("— unknown kinds; handlers from the app bundle")
ft.queue.append(task("t-kind", kind="nobody.here"))
run()
check("an unknown kind is released with reason 'no handler'", ft.releases.get("t-kind") == "no handler"
      and "t-kind" not in ft.starts)
ft.queue.append(task("t-kinds"))
w, _ = run(kinds=["app.echo"])
check("--kinds: the claim names them, and another kind is released",
      ft.claims[-1].get("kinds") == ["app.echo"] and ft.releases.get("t-kinds") == "no handler")
(APP / "apphandlers").mkdir()
(APP / "apphandlers" / "__init__.py").write_text('''
from pathlib import Path
from pod_harness.worker import TaskHandler, ComfyRunner, TaskFailed

class Echo(TaskHandler):
    def run(self, task, ctx, prepared):
        ctx.check()
        p = ctx.workdir / "echo.txt"
        p.write_text(task["payload"]["say"] + ":" + Path(ctx.inputs["start"]).read_text())
        ctx.progress(0.5, "half")
        ctx.emit({"said": task["payload"]["say"]})
        ctx.fetch("store://in/x/s.txt", ctx.workdir / "again.txt")
        return {"ref": ctx.upload(p, "echo.txt")}
    def finish(self, task, ctx, result):
        return {"echo": result["ref"], "models_root": str(ctx.models_root)}

class Seeded(ComfyRunner):
    def prepare(self, task, ctx):
        g = super().prepare(task, ctx)
        g["3"]["inputs"]["seed"] = 4242
        return g
    def finish(self, task, ctx, result):
        out = super().finish(task, ctx, result)
        out["checked"] = True
        return out
''')
(APP / "podh-handlers.json").write_text(json.dumps({"handlers": {
    "app.echo": "apphandlers:Echo", "app.seeded": "apphandlers:Seeded",
    "app.broken": "apphandlers.missing:Nope"}}))
fc.blobs["s.txt"] = b"hello"
ft.queue.append({"task_id": "t-echo", "kind": "app.echo", "payload": {"say": "hi"},
                 "inputs": {"start": fc.blob_url("s.txt")}, "outputs": {"to": "store://a"}})
run()
de = ft.dones.get("t-echo", {})
check("an app handler from podh-handlers.json runs with the app root on sys.path",
      de.get("ok") is True and fc.blobs.get("t-echo_echo.txt") == b"hi:hello")
check("finish()'s result is done's result; ctx.upload returned {name, ref}",
      de.get("result", {}).get("echo") == {"name": "echo.txt", "ref": "store://a/echo.txt"})
check("ctx.emit reached pod-control as event {data}", ft.events.get("t-echo") == [{"said": "hi"}])
check("ctx.fetch(store://…) is signed through POST …/sign {refs}",
      ("t-echo", {"refs": ["store://in/x/s.txt"]}) in ft.signs)
ft.queue.append(task("t-seeded", kind="app.seeded"))
run()
check("a ComfyRunner subclass can change the graph and the result",
      ft.dones["t-seeded"]["ok"] and cf.prompts[-1][1]["3"]["inputs"]["seed"] == 4242
      and ft.dones["t-seeded"]["result"]["checked"] is True)
ft.queue.append(task("t-broken", kind="app.broken"))
run()
check("a handler that does not import releases its kind with 'no handler'",
      ft.releases.get("t-broken") == "no handler")

# -- held tasks and prefetch ---------------------------------------------------------------------
print("— held tasks; prefetch while one runs")
fc.blobs["m1"] = b"M" * 5000
# what GET /v1/pod/models lists once pod-control has added a held task's models to the job
fc.routes[("GET", "/v1/pod/models")] = (200, {"models_rev": 2, "files": [
    {"path": "checkpoints/m1.safetensors", "url": fc.blob_url("m1"), "size": 5000},
    {"path": "loras/gone.safetensors", "url": fc.blob_url("gone")}]})
cf.steps, cf.step_delay = 10, 0.2
ft.queue.extend([task("t-run"),
                 task("t-held", state="held",
                      needs={"models": [{"path": "checkpoints/m1.safetensors", "size": 5000}]})])
seen = {}


def watch():
    wait_for(lambda: "t-held" in ft.beats or "t-held" in ft.starts, 15)
    seen["model_before_t1_done"] = (T / "models" / "checkpoints" / "m1.safetensors").is_file() \
        and "t-run" not in ft.dones
threading.Thread(target=watch, daemon=True).start()
w = Worker(poll_s=0.2, interrupt_grace=1.0, prefetch=1)
th = threading.Thread(target=w.run, daemon=True)
th.start()
check("both finish", wait_for(lambda: "t-run" in ft.dones and "t-held" in ft.dones, 30))
check("one claim asked for capacity + prefetch = 2 and got both",
      any(c["capacity"] == 2 for c in ft.claims))
check("the held task's model was fetched (via podh-boot's local API) while the other ran",
      seen.get("model_before_t1_done") and state.models.get("checkpoints/m1.safetensors") == "present")
check("it started only after the running one finished (capacity 1)",
      ft.starts.index("t-held") > ft.starts.index("t-run"))
check("its usage says prefetched, with held_s",
      ft.dones["t-held"]["usage"]["prefetched"] is True and "held_s" in ft.dones["t-held"]["usage"])
check("the next claim reports the model it now has",
      wait_for(lambda: any("checkpoints/m1.safetensors" in c["have"]["models"] for c in ft.claims), 5))
# drain, on the same live worker
reset()
cf.steps, cf.step_delay = 10, 0.2
ft.queue.extend([task("t-d1"), task("t-d2")])
wait_for(lambda: "t-d1" in ft.starts and len(w.tasks) == 2, 10)
state.take_reply({"drain": True})
th.join(20)
check("drain: the claimed-but-unstarted task is released 'draining'",
      ft.releases.get("t-d2") == "draining" and "t-d2" not in ft.starts)
check("...the running one finishes and reports", ft.dones.get("t-d1", {}).get("ok") is True)
check("...then the worker exits 0", not th.is_alive())
state.take_reply({"drain": False})
reset()
fc.blobs.pop("gone", None)
ft.queue.append(task("t-nomodel", state="held", missing_models=["loras/gone.safetensors"]))
run()
check("a held task whose model cannot be fetched is released, not failed",
      (ft.releases.get("t-nomodel") or "").startswith("models:") and "t-nomodel" not in ft.dones)

# a model the store lacks, with a source: the claim's from_source (TASK-API §10)
reset()
import hashlib  # noqa: E402
sdata = b"S" * 4096
fc.blobs["hf_ckpt"] = sdata
state.ready = True
ft.queue.append(task("t-src", needs={"models": [{"path": "checkpoints/src.safetensors"}]},
                     from_source=[{"path": "checkpoints/src.safetensors",
                                   "source": fc.blob_url("hf_ckpt"),
                                   "put_url": f"{fc.url}/put/store_src?sig=p",
                                   "sha256": hashlib.sha256(sdata).hexdigest(),
                                   "size": len(sdata)}]))
run()
check("a from_source model is fetched by the pod from its source, and the task runs",
      ft.dones.get("t-src", {}).get("ok") is True
      and (T / "models" / "checkpoints/src.safetensors").read_bytes() == sdata)
check("...and it is put back into the store in the background",
      wait_for(lambda: fc.blobs.get("store_src") == sdata))

# -- the client on its own -------------------------------------------------------------------------
print("— pod_harness.comfy")
check("missing_nodes finds node types this ComfyUI lacks (API and editor formats)",
      comfy.missing_nodes(GRAPH, {"KSampler": {}, "SaveImage": {}}) ==
      ["CheckpointLoaderSimple", "LoadImage"]
      and comfy.missing_nodes({"nodes": [{"type": "KSampler"}, {"type": "Note"}, {"type": "X"}]},
                              {"KSampler": {}}) == ["X"])
wf = T / "wf.json"
wf.write_text(json.dumps(GRAPH))
env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
p = subprocess.run([sys.executable, "-c", "from pod_harness.comfy import catalogue_main as m; "
                    "raise SystemExit(m())", "check", str(wf)], env=env, capture_output=True, text=True)
check("podh-catalogue check passes a workflow whose nodes all exist", p.returncode == 0 and "ok" in p.stdout)
cf.node_types.discard("LoadImage")
p = subprocess.run([sys.executable, "-c", "from pod_harness.comfy import catalogue_main as m; "
                    "raise SystemExit(m())", "check", str(wf)], env=env, capture_output=True, text=True)
check("...and exits 1 naming a missing one", p.returncode == 1 and "LoadImage" in p.stdout)
cf.node_types.add("LoadImage")

# podh-catalogue dump
INFO = {"KSampler": {"input": {"required": {"model": ["MODEL"], "seed": ["INT", {"default": 0}],
                                            "sampler_name": [["euler", "dpmpp_2m"], {}]},
                               "optional": {"noise": ["NOISE"]}, "hidden": {"x": "PROMPT"}},
                     "output": ["LATENT"], "output_name": ["LATENT"], "category": "sampling",
                     "python_module": "nodes"},
        "VHS_VideoCombine": {"input": {"required": {"images": ["IMAGE"]}}, "output": [],
                             "category": "video", "output_node": True,
                             "python_module": "custom_nodes.ComfyUI-VideoHelperSuite"}}
cat = comfy.compact_catalogue(INFO, {"image": "podh-x", "version": None})
check("compact_catalogue: types per input, COMBO for choice lists, outputs, pack, counts",
      cat["count"] == 2 and cat["image"] == "podh-x" and "version" not in cat
      and cat["nodes"]["KSampler"] == {"in": {"model": "MODEL", "seed": "INT",
                                              "sampler_name": "COMBO"},
                                       "opt": {"noise": "NOISE"}, "out": ["LATENT"],
                                       "cat": "sampling", "pack": "nodes"}
      and cat["nodes"]["VHS_VideoCombine"].get("output_node") is True
      and cat["packs"] == {"custom_nodes.ComfyUI-VideoHelperSuite": 1, "nodes": 1})
saved = T / "oi.json"
saved.write_text(json.dumps(INFO))
out = T / "cat" / "catalogue.json"


def dump(*args):
    return subprocess.run([sys.executable, "-c", "from pod_harness.comfy import catalogue_main as m; "
                           "raise SystemExit(m())", "dump", "--out", str(out), *args],
                          env=env, capture_output=True, text=True, timeout=120)


p = dump("--object-info", str(saved), "--require-pack", "ComfyUI-VideoHelperSuite")
check("podh-catalogue dump --object-info writes the compact catalogue",
      p.returncode == 0 and json.loads(out.read_text())["count"] == 2)
p = dump("--object-info", str(saved), "--require-pack", "ComfyUI-Missing")
check("...--require-pack fails when no node comes from that pack", p.returncode == 1
      and "ComfyUI-Missing" in p.stdout)
wf2 = T / "wf2.json"
wf2.write_text(json.dumps({"1": {"class_type": "KSampler"}, "2": {"class_type": "Nope"}}))
p = subprocess.run([sys.executable, "-c", "from pod_harness.comfy import catalogue_main as m; "
                    "raise SystemExit(m())", "check", str(wf2), "--object-info", str(out)],
                   env=env, capture_output=True, text=True)
check("podh-catalogue check reads a published catalogue too", p.returncode == 1
      and "Nope" in p.stdout and "KSampler" not in p.stdout)
p = dump("--url", cf.url, "--min-nodes", "3")
check("...from a running ComfyUI (--url)", p.returncode == 0
      and set(json.loads(out.read_text())["nodes"]) == set(cf.node_types))
fake_root = T / "fakecomfy"
fake_root.mkdir()
(fake_root / "main.py").write_text(
    "import json, sys, time\n"
    "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
    "a = sys.argv\n"
    "assert '--cpu' in a and a[a.index('--listen') + 1] == '127.0.0.1'\n"
    "port = int(a[a.index('--port') + 1])\n"
    "time.sleep(1.5)                       # loading custom nodes\n"
    f"INFO = json.loads(open({str(saved)!r}).read())\n"
    "class H(BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        b = json.dumps(INFO).encode() if self.path == '/object_info' else b'{}'\n"
    "        self.send_response(200); self.send_header('Content-Length', str(len(b)))\n"
    "        self.end_headers(); self.wfile.write(b)\n"
    "    def log_message(self, *x): pass\n"
    "HTTPServer(('127.0.0.1', port), H).serve_forever()\n")
t0 = time.time()
p = dump("--start", "--root", str(fake_root), "--python", sys.executable, "--timeout", "60")
check("...--start runs ComfyUI --cpu on 127.0.0.1, waits for /object_info, and stops it",
      p.returncode == 0 and json.loads(out.read_text())["count"] == 2 and time.time() - t0 < 30)
(fake_root / "main.py").write_text("import sys; print('boom: a node exploded'); sys.exit(3)\n")
p = dump("--start", "--root", str(fake_root), "--python", sys.executable, "--timeout", "30")
check("...and fails with ComfyUI's output when it exits instead", p.returncode == 2
      and "boom" in p.stdout)

# -- the command ---------------------------------------------------------------------------------
print("— podh-worker --once, as a process")
ft.queue.append(task("t-cli"))
p = subprocess.run([sys.executable, "-m", "pod_harness.worker", "--once", "--poll", "0.2"],
                   env=env, capture_output=True, text=True, timeout=60)
check("python -m pod_harness.worker --once runs one task and exits 0",
      p.returncode == 0 and ft.dones.get("t-cli", {}).get("ok") is True)
p = subprocess.run([sys.executable, "-m", "pod_harness.worker", "--once"],
                   env=dict(env, PODH_CONTROL_URL=""), capture_output=True, text=True, timeout=30)
check("with no control plane it exits 2 and says why", p.returncode == 2 and "PODH_CONTROL_URL" in p.stdout)

check("nothing was sent that pod-control would refuse or misread", not ft.bad or print(ft.bad))
srv.close(); cf.close(); fc.close()
print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
