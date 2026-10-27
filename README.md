# pod-harness

A container harness for running staged batch jobs on rented GPU/CPU pods — with the
reporting you need to trust the result, and none of the guesswork about what actually ran.

Built for [RunPod](https://runpod.io), portable off it by design. Nothing here knows what
your job does.

---

## Introduction

Renting a pod to run a long job leaves you with two problems the provider does not solve:
**you cannot see what it is doing**, and **you cannot tell whether it really worked.**
RunPod exposes no logs API, so a job either prints to a console you have to be watching or
disappears into silence. And a batch script that exits 0 has told you it *ran*, not that it
*produced anything*.

This harness answers both.

It boots your container, mounts your data, resolves the code to run, executes your job as
an ordered sequence of **stages**, and serves a small HTTP API the whole time — so progress
is pollable from a laptop, a web app, or an agent. Every stage declares what it produces,
and the harness **verifies those outputs exist before reporting success**.

That last part is the reason this exists. Three separate bugs in the project this grew out
of had the same shape: a stage reported success and produced nothing. A glob matched
`*.wav` against a FLAC corpus and cheerfully reported "40 files" that were zero files. The
harness now catches that class of failure by construction — a stage that claims an output
it did not create is reported `unverified`, never `ok`.

**What it does**

- Runs a job as ordered stages with declared inputs and outputs
- Verifies each stage produced what it claimed, and fails loudly when it did not
- Resumes from the first stage that does not verify — never from a marker file
- Serves `/v1` for status, per-stage detail, logs, artifacts and cancel
- Mirrors status to object storage, so it survives the pod's death
- Runs your code from a mounted volume or object store, so a code change needs no rebuild

**What it deliberately does not do**

- Provision or terminate pods (that is a [loader](https://github.com/itsnotyoutoday/pod-loader-rpc)'s
  job, and must be external — see *Cost control* below)
- Know your storage layout — it is told every root and refuses to guess
- Contain any credentials

---

## Use

### The images

| image (Docker Hub, the primary) | mirror | size | for |
|---|---|---|---|
| `docker.io/itsnotyoutoday/pod-harness` | `ghcr.io/itsnotyoutoday/pod-harness` | ~240 MB | anything that is not machine learning |
| `docker.io/itsnotyoutoday/lingua-pipeline` | `ghcr.io/itsnotyoutoday/lingua-pipeline` | ~6 GB | forced alignment + audio measurement |
| `docker.io/itsnotyoutoday/podh-comfyui` | `ghcr.io/itsnotyoutoday/podh-comfyui` | ~6 GB (zstd) | ComfyUI on CUDA 12.8, booted by `podh-boot` ([below](#images-podh-comfyui-and-podh-comfyui-video)) |
| `docker.io/itsnotyoutoday/podh-comfyui-video` | `ghcr.io/itsnotyoutoday/podh-comfyui-video` | ~6.5 GB (zstd) | the same plus lip-sync and voice node packs |

Pull from Docker Hub: measured from RunPod, it is the faster of the two (137 s against
158 s for the same image in the same data centre). GHCR carries the same tags and digests
as the mirror and the fallback. All are public, so pods pull without registry auth.

Most jobs want the small one. It carries the harness, the API and boto3 — nothing else.

### Run one locally

```bash
docker run -d -p 8000:8000 \
  -e PODH_API_TOKEN=dev \
  -e PODH_WORKSPACE=/workspace \
  -e PODH_LOG_ROOT=/workspace/runs \
  -v "$PWD/work:/workspace" \
  docker.io/itsnotyoutoday/pod-harness:latest     # or ghcr.io/itsnotyoutoday/pod-harness:latest

curl -s localhost:8000/v1/health
```

### Sidecar: install into a container you did not build

Catalog images (runpod/comfyui and the like) own their entrypoint. Rather than rebuilding
them on this base, install the control surface beside the workload:

```bash
pip install "pod-harness[serve] @ git+https://github.com/itsnotyoutoday/pod-harness@v0.2.0"
PODH_API_TOKEN=<per-pod token> \
PODH_SIDECAR_LOGS="comfy=/workspace/comfyui.log,/workspace/doppel/*.log" \
PODH_SIDECAR_ARTIFACTS=/workspace/outputs \
podh-serve --sidecar &          # port PODH_SIDECAR_PORT, default 8000; expose "8000/http"
```

Routes (all but health need `X-Podh-Token`): `GET /v1/health`, `GET /v1/`, `GET /v1/logs`,
`GET /v1/logs/{name}?tail=&offset=&wait=` (bounded, followable), `POST /v1/progress
{"step"}` and `GET /v1/events?since_seq=&wait=`, `GET /v1/system` (disk, GPU),
`GET /v1/artifacts?path=` and `/v1/artifacts/file?path=` (read-only, never outside a
root), `POST /v1/boot {"line", "stage"?}` (relay a boot line to pod-control, and keep it as
an event), `POST /v1/done {"rc", "reason"?, "stage"?}` (ask pod-control to terminate this
pod, saying why).

It runs no stages and does not heartbeat by default: a sidecar that heartbeats on its own
keeps a hung pod looking alive. `PODH_SIDECAR_HEARTBEAT=1` plus `PODH_SIDECAR_LIVENESS=<url
the workload answers>` heartbeats only while the workload does. See `pod_harness/sidecar.py`.

Two commands come with the install for the image's own boot script, which is the only
thing that knows what the pod is waiting on. Neither needs the sidecar to be running — they
talk to pod-control directly with the same `PODH_CONTROL_*` environment:

- `podh-report boot "<line>" [--stage S]` — one progress line per step; `--stage ready`
  once the workload is up. Always exits 0: a progress line never aborts a boot.
- `podh-report done <rc> [--reason R] [--stage S]` — ask to be terminated, saying why.
  Exits 0 sent, 1 failed, 3 not configured. Stops the heartbeat either way.
- `podh-gpucheck` — is the GPU usable? Runs when `PODH_GPU_COUNT>0` (or, if unset, when
  `nvidia-smi` lists a GPU); checks `nvidia-smi -L` and a `torch.cuda.init()` probe with a
  timeout; exits 7 and reports `done(7, stage=gpu_check)` when it is not. `--json` for
  machines, `--no-report` to report yourself.
- `podh-gpucheck --scan-log FILE` — exit 7 if a log holds a fatal CUDA error ("CUDA unknown
  error", "No CUDA GPUs are available", …). Reports nothing; a supervisor decides.

```bash
#!/bin/bash
# boot script for a hot-installed image — generic shape
podh-report boot "checking the GPU" --stage gpu_check
podh-gpucheck || { sleep infinity; }        # already reported done(7); idle, don't restart

podh-report boot "downloading models" --stage models
if ! ./fetch-models.sh; then
    podh-report done 1 --reason "model download failed" --stage models
    sleep infinity
fi

./start-workload.sh > /workspace/workload.log 2>&1 &
until curl -sf "$WORKLOAD_URL" >/dev/null; do sleep 2; done
podh-report boot "workload listening" --stage ready

# supervise: a CUDA error in the log means this machine is done
while sleep 60; do
    podh-gpucheck --scan-log /workspace/workload.log >/dev/null
    [ $? -eq 7 ] || continue                 # 0 clean, 2 unreadable: not a GPU verdict
    podh-report done 7 --reason "fatal CUDA error in workload log" --stage gpu_check
    break
done
```

### podh-boot: a job's boot spec, run on the pod

`podh-boot` takes a pod from "created" to "ready" and keeps it honest after. It fetches the
job's boot spec (`GET /v1/pod/spec` with the pod token; `--spec FILE` for local runs) and
runs it as ordered stages, each with a time limit and a boot line:

```
gpu_check → binaries → models → addons → app → workload → ready → app.serve
```

| stage | does |
|---|---|
| `gpu_check` | podh-gpucheck; posts `vram_gb`, `cuda`, `driver`, `gpu` and (after binaries) an NVENC probe to `/v1/pod/facts`. Always runs, even on a resumed pod. |
| `binaries` | pinned `{name, pin, url, sha256, env}` (+ optional `archive_path`, `bin_names`; else the release's `bin/` is found), verified, installed once per sha, shared as kit `binary-<name>-<sha16>`; `env` (with `{bin}`/`{bindir}`) goes to `$PODH_WORKLOAD_ENV` |
| `models` | the manifest's presigned files into `$PODH_MODELS_ROOT`, 2 at a time, resumable (aria2c if present), fresh links on expiry, size/sha checked, per-path state reported; a file the store lacks (`in_store: false`, with a `source`) is fetched from the source instead and put back into the store in the background (below) |
| `addons` | git repos at full commits, validated again on the pod, `pip` into the workload's python, links (never outside their roots), import probes; `optional` items only warn; the set is kept as a kit |
| `app` | the app's code fetched with ITS token (`Authorization: Bearer $<fetch.auth_env>`), or from a presigned `{url, sha256}` (no header; the archive must match the sha256), unpacked into `$PODH_APP_ROOT`, its `run` executed there (`{app}` in argv = that dir) — before the workload, so it can add files and `KEY=value` lines to `$PODH_WORKLOAD_ENV` |
| `workload` | ComfyUI (models wired through `extra_model_paths.yaml`; `fast_disk` → `--fast-disk`, below) or a command; up within `up_timeout_s`; log scanned for fatal strings; restarted once |
| `ready` | boot line with stage `ready` |
| `app.serve` | the app's long-running process (for a task pool, `podh-worker`): one restart, exit 0 ends the pod (unless the pod is draining or parked), drain/park → SIGTERM then SIGKILL after `serve_grace_s` |

Each stage's last boot line carries `stage_s` (how long it took: "done in Ns", or "failed
after Ns"), and the binaries and models stages' also `bytes` and `files` (what they
downloaded). The first `/v1/pod/facts` post, before the GPU check, says what image the pod
runs and when its container started: `image` (`<name>:<version>` from
`/opt/podh/manifest.json`), `image_digest` (from `$PODH_IMAGE_DIGEST` — an image cannot
hold its own digest, so whoever starts the container passes it — else the manifest's
`digest` if one was written there) and `container_started_at` (epoch of the container's
PID 1 start), so the image pull can be told apart from the container's own start-up. Older
servers that refuse any of these (400/422) get the report without them.

A failure reports `done(rc, reason, stage, log_tail)` and idles (`--exit-on-fail` exits). A
resumed pod skips what its disk already has. After ready a watcher fetches models added
mid-life (`models_rev` on the heartbeat reply) without ever ending the pod.

**When the store lacks a model** (TASK-API §10, a safety net: apps seed their store first).
A manifest entry, or a task claim's `from_source` item, may be `{path, source, put_url,
sha256?, size?}` instead of `{path, url}`. The pod downloads it straight from `source` —
`https://…`, `hf://<owner>/<repo>/<file>@<rev>` (Bearer `$HF_TOKEN` when set) or
`civitai://<model-version-id>` (Bearer `$CIVITAI_TOKEN`) — resumable and checked, logs
"not in store: fetched from source; uploading back to the store", uses it at once, and then
PUTs it to `put_url` in the background: one upload at a time, at low priority, only once
the pod is ready, skipped while it drains or parks, never holding up ready or a task, with
a boot line when it ends. Paths the manifest lists under `missing` (no source) fail the boot.

**Logs** (APP-API §9). Every log is `<name>.log` in `$PODH_BOOT_DIR/logs`: `boot`
(podh-boot's own stage lines, timestamped), `workload`, `serve`, `app-setup`,
`addon-<name>` and, from podh-worker, `worker`. pod-control never connects in: when an app
asks for a job's logs, the next `/v1/alive` reply carries `logs: {names, lines, req_id}`
and the pod POSTs the last `lines` lines of each (every log when `names` is empty) to
`/v1/pod/logs` — colour codes stripped, at most 256 KB per name, the newest lines kept. At
the end the pod gzips every log and PUTs it into the log store through
`POST /v1/pod/logs/upload`'s presigned URLs, one at a time within 60 s
(`PODH_LOG_UPLOAD_S`): before it reports done (so a failed stage always keeps its logs),
after a drain or park has stopped `app.serve`, and on SIGTERM (8 s). An older server
(no `logs` in the reply, 404 on either route) or no log store: nothing is sent.

Two ways to run it:

```bash
# an image built for it
ENTRYPOINT ["podh-boot"]

# a catalog image: the provider's start command
bash -lc "pip install 'pod-harness @ git+https://github.com/<owner>/pod-harness@<tag>' && podh-boot"
```

The app stage, the workload and `app.serve` get `PODH_COMFY_ROOT`, `PODH_COMFY_PY`,
`PODH_COMFY_URL`, `PODH_MODELS_ROOT`, `PODH_WORKLOAD_ENV`, `PODH_JOB_ID`, `PODH_GPU_COUNT`,
`PODH_APP_ROOT`, after the NVENC probe `PODH_NVENC` (1/0) and `PODH_FFMPEG_ENCODER`
(`h264_nvenc` or `libx264`), and the local API, `PODH_LOCAL_URL` + `PODH_LOCAL_TOKEN` (127.0.0.1 only). `app.run` and
`app.serve` also get the current job's app env from `GET /v1/pod/env` (read on every boot,
so a resumed pod serving a new job gets that job's), over the container env.

- `POST /busy {"busy", "detail"?}` — rides on the next heartbeat; pod-control's idle clock
  runs only while the pod is not busy. `podh-report busy [--detail D]` / `podh-report idle`.
- `GET /state` → `{drain, park, extend_until, window_until, models_rev, models, …}` from
  the last heartbeat reply, so a worker stops claiming work on drain without asking its app.
- `POST /models/fetch {"files"?: [{path, url, sha256?, size?} | {path, source, put_url?, …}], "paths"?: [...]}` — fetch
  models now through the same machinery as the boot set (resumable, verified, states
  reported); 202 at once, then each path's state shows in `/state`'s `models`.
- `POST /done {"rc", "reason"?, "stage"?}` — the only exit. `podh-report done` uses it.
- `POST /workload/restart` — restart the workload (a job wedged in a C-level node ignores
  ComfyUI's /interrupt); 200 once it answers again, non-2xx with the error if it does not.

`pod_harness.local.Client` is the same API from Python (`busy`, `state`, `done`,
`restart_workload`, `models_fetch`); it never raises.

`podh-kit get|put <name> <app-key> <dir>` caches anything slow to build (a venv, wheels):
`get` exits 0 with the dir filled or 2 when nobody has built it yet; the key also carries
the python, torch, CUDA and image the build is for. The exact routes and fields are in
`contract.json` under `control_plane.pod_calls`, `local_api` and `boot`.

### Tasks and handlers: podh-worker

`podh-worker` is the generic worker for task pools: the pod claims units of work from the
control plane instead of running a job of its own. It is the pool's `app.serve`:

```
claim → (held: fetch models) → start → fetch inputs → handler prepare/run/finish
      → upload outputs → done                      beating every lease_s/3 throughout
```

- **Claims** stay within `--capacity` (tasks executing at once, default 1) plus
  `--prefetch` (claimed ahead, default 1): the next task's models download while one runs.
  The claim reports the models on disk and `have.loaded` (the last task's `needs.loads`), so
  the control plane can prefer a pod that already has the checkpoint.
- **Held** tasks wait for their models (`missing_models`, by path): the control plane has
  added them to the pod's job, and podh-boot's `POST /models/fetch` fetches them; a model
  that cannot be fetched releases the task (no attempt counted).
- **Busy**: the local busy flag is true while the worker holds any task, so the pod's idle
  clock is right.
- **Cancel** (a beat reply `{"cancel": true, "state": "cancelled"}`): ComfyUI is
  interrupted; if it does not stop within 20 s the workload is restarted; reported as
  `cancelled`. A task cancelled before it starts is dropped. A beat reply with `cancel` and
  any other state, or a 404/409/410, means the lease is gone (requeued, or another pod's):
  work stops, nothing is reported.
- **Stall**: a ComfyUI execution with no progress for `stall_s` (task, else `--stall-s`,
  default 600) is interrupted, then restarted if need be — a failed attempt, stage `stall`.
  `timeout_s` caps the whole task. Failures at `inputs`, `upload` and `stall` are sent
  `retryable` (another attempt, up to `max_attempts`); a handler can choose with
  `TaskFailed(reason, stage, retryable=True|False)`.
- **Drain / park / SIGTERM**: no more claims, unstarted tasks released `draining`, the
  running ones finish, exit 0.
- An unknown `kind`, or one outside `--kinds`, is released with reason `no handler`.
- A task with `fallback: "app"` is released rather than failed when it fails on the pod.
- Inputs arrive presigned in the claim (`{url, ref?}`) and are cached across tasks up to
  `PODH_INPUT_CACHE_GB` (default 2). An input that is a dependency's result path arrives as
  `{value}`: it is in `ctx.values`, and `{input:<name>}` in a patch becomes the value.
- Outputs go to the claim's `outputs.put` links (names declared at submit), else to links
  asked for by name under `outputs.to` (`POST …/outputs`), else to the app's `put_url`.
- `done` carries `result` (finish()'s JSON, ≤ 256 KB), `artifacts` (`{name, size, sha256}`;
  the control plane files each under `outputs.to`) and `usage` (fetch, run and upload
  seconds; VRAM peak when `nvidia-smi` exists).

`--once` runs one task and exits (for tests and one-shot pods); `--poll S` sets the gap
between empty claims. The routes it calls are in `contract.json` under
`control_plane.pod_calls`; `tests/test_worker_against_server.py` runs the worker against a
real control plane when its source is importable.

**The built-in `comfy` kind** loads `workflow.ref` (a file in the app bundle) or
`workflow.inline` (ComfyUI's API format), applies `workflow.patch`
(`"<node>.inputs.<name>": value`, or names from `workflow.map`), copies each input into
ComfyUI's `input/` and substitutes `{input:<name>}` with the name ComfyUI knows it by,
posts the graph, follows `/ws` (polling `/history` if there is none), and uploads the saved
files that match `outputs.keep`, each named by its path under ComfyUI's output directory
(`clips/run1/x.mp4` for a file saved in subfolder `clips/run1`; a plain file name when there
is no subfolder). A `keep` that matches nothing fails the task.

**Your own kinds** ship in the app bundle, declared in `podh-handlers.json` at its root and
imported with the bundle root on `sys.path`:

```json
{"handlers": {"myapp.upscale": "myapp_pod.handlers:Upscale"}}
```

```python
# myapp_pod/handlers.py
import json
from pod_harness.worker import TaskHandler, TaskFailed

class Upscale(TaskHandler):
    def prepare(self, task, ctx):
        # ctx.inputs: {name: local path}, already fetched and verified
        graph = json.loads((ctx.app_root / "workflows/upscale.json").read_text())
        graph["5"]["inputs"]["image"] = ctx.comfy.upload_input(ctx.inputs["image"])
        return graph

    def run(self, task, ctx, graph):
        frames = []
        for scale in task["payload"]["scales"]:      # several ComfyUI runs in one task
            ctx.check()                               # raises Cancelled on a cancel
            graph["7"]["inputs"]["scale_by"] = scale
            r = ctx.comfy.run(graph, timeout_s=600, stall_s=120, keep=["*.png"],
                              dest=ctx.workdir)
            frames += r.files
            ctx.emit({"scale": scale, "files": len(r.files)})
        if not frames:
            raise TaskFailed("no frames came out", stage="run")
        return frames

    def finish(self, task, ctx, frames):
        refs = [ctx.upload(p, f"x{i}.png") for i, p in enumerate(frames)]
        return {"frames": refs}                       # the task's result
```

`ComfyRunner` (the `comfy` kind) can be subclassed the same way, overriding `prepare` to
change the graph or `finish` to check the outputs. `ctx` gives `task`, `payload`,
`models_root`, `app_root`, `workdir`, `inputs`, `comfy` (a `pod_harness.comfy.ComfyClient`
wired to this task's cancel, timeout and progress: `upload_input`, `run`, `interrupt`,
`history`, `collect`), `progress(fraction, message)`, `log`, `emit(dict)`,
`fetch(url | store://ref, dest)`, `upload(path, name)` → `{name, ref}` (mid-run too; `name`
is a file name or a relative path `a/b/c.ext`, normalised, with `..`, absolute paths and
empty segments refused),
`restart_workload()`, `cancelled()` and `check()`. `Cancelled` is a `BaseException`: an
`except Exception` in a handler cannot swallow a cancel.

`podh-catalogue check <workflow.json ...>` asks ComfyUI's `/object_info` whether every node
type the workflows use exists (exit 1 naming the missing ones) — run it in `app.run` to
fail the boot instead of the first task. `--object-info FILE` checks against a saved
catalogue instead, so an app can check a workflow against an image without a pod.

`podh-catalogue dump --out FILE [--url URL | --start | --object-info FILE]` writes the
compact node catalogue: for every node class its inputs' and outputs' types, category and
the module (node pack) it comes from — `{"format": 1, "count", "packs": {module: n},
"nodes": {class: {"in", "opt"?, "out", "out_names"?, "cat"?, "pack"?, "output_node"?}}}`
(choice lists become `COMBO`). `--start` starts ComfyUI `--cpu` on a free 127.0.0.1 port,
waits for `/object_info` and stops it — no GPU needed, which is how the images build theirs.

### Images: podh-comfyui and podh-comfyui-video

GPU images whose entrypoint is `podh-boot` (`/opt/podh/entry` → `tini -- podh-boot`, ports 8188 and 22), built by
`.github/workflows/build-comfyui.yml` from `docker/comfyui.Dockerfile` and
`docker/comfyui-video.Dockerfile`. They are the stages above with the slow parts done.

| | podh-comfyui | podh-comfyui-video |
|---|---|---|
| base | `ubuntu:24.04` (by digest); CUDA 12.8 + cuDNN 9 are torch's own `nvidia-*-cu12` wheels | podh-comfyui, by digest |
| python | 3.12 (Ubuntu's, one interpreter), venv `/opt/venv` | same |
| torch | 2.11.0+cu128, torchvision 0.26.0, torchaudio 2.11.0 | same |
| ComfyUI | v0.43.0 in `/opt/ComfyUI`, no ComfyUI-Manager | same |
| ffmpeg | BtbN n8.1.3 static GPL (NVENC), sha256-pinned | same |
| ssh | `sshd` on :22, key-only, only when `PUBLIC_KEY` is set | same |
| node packs | VideoHelperSuite, Frame-Interpolation, Impact-Pack (+ sam2), Impact-Subpack, rgthree-comfy, KJNodes | + LatentSyncWrapper, Fill-ChatterBox, onnxruntime-gpu 1.24.1, insightface 0.7.3 |

Exact commits are in the Dockerfiles; every python package version is in
`docker/comfyui/lock.txt` (regenerated from `lock.in`), and torch's CUDA wheels in
`docker/comfyui/cuda.txt`.

**How a job uses one.** Launch the image with a boot spec, as for any `podh-boot` pod. The
defaults already point at the image: `PODH_COMFY_ROOT=/opt/ComfyUI`,
`PODH_COMFY_PY=/opt/venv/bin/python`, `PODH_ADDONS_ROOT=/opt/ComfyUI/custom_nodes`,
`PODH_MODELS_ROOT=/workspace/models`. What the image baked is recorded under
`PODH_BAKED_ROOT=/opt/podh/baked`, and the stages use it:

- a `binaries` item with the baked ffmpeg's sha256 is used in place — no download, no kit;
- an `addons` item whose `name` is the pack's directory (the repo name, e.g.
  `ComfyUI-KJNodes`) and whose `commit` is the baked commit is **instant**: no clone, no
  `requirements.txt`; its `pip` extras, `links` and `probe` still run. Any other commit is
  cloned and installed as usual, beside or over it;
- `workload.comfyui` starts the baked ComfyUI with its models wired to `PODH_MODELS_ROOT`.

**`fast_disk`.** ComfyUI v0.43.0 logs `Model storage policy: fast_disk=…` per model. Unless
told, it is true only when every model file is on a block device it can trace through
`/sys` to NVMe at PCIe ≥8 GT/s x4 (`comfy/storage.py`); a network volume or a container's
overlay has no such entry, so `/workspace` is always `False` — and moving models to the
container disk (`PODH_MODELS_ROOT`) does not change that. With `False`, dynamic loading
copies each model's weights into pinned RAM before they go to the GPU; with `True` it
streams them from the file instead. `workload.comfyui.fast_disk: true` (or
`PODH_COMFY_FAST_DISK=1`) passes `--fast-disk`, `false` passes `--disable-fast-disk`;
unset leaves ComfyUI to decide. Opt in where the model files are on fast local disk —
streaming from a network volume is likely slower than the RAM copy it saves.

```json
{"boot": {"gpu_check": true,
          "binaries": [{"name": "ffmpeg", "pin": "n8.1.3", "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-26-14-25/ffmpeg-n8.1.3-24-g92611900bb-linux64-gpl-8.1.tar.xz",
                        "sha256": "d113aa5b57ab0dcfee171d835240ce3ea5db826a0ee80ed7528c8d26a61e87a4",
                        "env": {"IMAGEIO_FFMPEG_EXE": "{bin}/ffmpeg", "PATH": "{bindir}:$PATH"}}],
          "models": {"profile": "…"},
          "addons": {"set": "…", "version": 1},
          "workload": {"comfyui": {"port": 8188, "up_timeout_s": 300}}}}
```

**Models are never baked.** They are the job's: the `models` stage fetches them into
`PODH_MODELS_ROOT`. Nothing is downloaded at build (the packs' install scripts, which fetch
weights, are not run). A pack that fetches its weights on first use when they are not where
it looks (LatentSyncWrapper's `checkpoints/`) should be given them with addon `links`.

**ComfyUI's venv is guarded.** `/opt/venv/pip.conf` names `/opt/podh/guard.txt`, which pins the torch trio and numpy for
every pip run from ComfyUI's venv (`/opt/venv`), the addons stage's included, and for nothing else: an app's own venv
on the pod resolves its own pins. In the guarded venv, an addon that asks for another
torch or `numpy<2` fails with pip's resolution error instead of replacing the CUDA build.

**CUDA comes from pip, not from the base image.** There is no system CUDA: the
`nvidia-*-cu12` wheels torch 2.11.0+cu128 requires are installed at exactly its versions,
and `LD_LIBRARY_PATH` names their `site-packages/nvidia/*/lib` directories, so anything else
that links CUDA or cuDNN by soname (onnxruntime-gpu in the video image, or a package an
addon installs) finds the same libraries. The driver's libraries (libcuda, NVENC's
libnvidia-encode) come from the host through the NVIDIA container runtime, which the image
asks for with `NVIDIA_DRIVER_CAPABILITIES=compute,utility,video`. triton ships (torch
requires it), but there is no C compiler in the image, which triton needs at run time to
build its kernel launchers: expect `torch.compile` and Triton kernels to fail until one is
added. SageAttention is not included: no prebuilt SageAttention 2 wheel exists for torch
2.11/cu128/py3.12 (PyPI has only the Triton-only 1.x, which would need that compiler too).

**Layers.** Nine, of up to ~1.2 GB each, ordered rarely-changed first (system and ffmpeg;
the CUDA wheels in three; torch; ComfyUI; the node packs; pod-harness; the identity files),
so a pull downloads them in parallel and a harness change ships as a layer of a few hundred
KB. All
layers are **zstd**-compressed: pulling needs Docker ≥ 23 or containerd ≥ 1.5 on the host.

**SSH.** Set `PUBLIC_KEY` (one or more `authorized_keys` lines) and the entrypoint starts
`sshd` on :22 before `podh-boot`: root, key-only (no password or keyboard-interactive
logins), host keys generated per container. Without `PUBLIC_KEY` nothing listens on 22.

**Tags.** `<registry>/<owner>/podh-comfyui:<comfyui tag>-cu128-<short sha>` (e.g.
`v0.43.0-cu128-1a2b3c4`) and `:latest`; the same for `podh-comfyui-video`. Docker Hub
(`docker.io/itsnotyoutoday/…`, pushed when the repository has the
`DOCKERHUB_USER`/`DOCKERHUB_TOKEN` secrets) is the one to pull; GHCR always gets the same
push and is the mirror. Pin the versioned tag, or better the digest, in a job; `latest`
moves.

**What is in it, without running it.** Each image carries `/opt/podh/manifest.json`
(written at build by `docker/comfyui/write_manifest.py`) and `/opt/podh/catalogue.json`
(`podh-catalogue dump --start`, after the image's checks):

```
manifest.json   {format: 1, image, version, revision (git sha), source, built_at,
                 base (ubuntu ref by digest), python, torch {torch, torchvision, torchaudio},
                 cuda {cuda, cudnn, wheels {cuda_runtime, cudnn, cublas}},
                 comfyui {tag, commit}, node_packs [{name, repo, commit}],
                 binaries [{name, sha256, bins, version}],
                 features {nvenc_capable_ffmpeg, insightface, onnxruntime_gpu,
                           latentsync_fixes, compat_shim, sshd, triton_cc},
                 pod_harness, catalogue, parent? {image, version, revision} (the video image)}
catalogue.json  {format: 1, image, version, count, packs {module: n},
                 nodes {class: {in, opt?, out, out_names?, cat?, pack?, output_node?}}}
```

The digest cannot be inside the image it hashes: the published copies carry it (below),
and a pod reports it when its launcher sets `PODH_IMAGE_DIGEST`. The OCI labels say the
same in brief: `org.opencontainers.image.{title,description,version,revision,source,
base.name}` and `io.podh.manifest` (`image=…;version=…;revision=…;comfyui=…;cuda=12.8;
python=3.12;manifest=/opt/podh/manifest.json;catalogue=/opt/podh/catalogue.json`), so
`docker buildx imagetools inspect` answers without a pull.

**Read it without a pod.** After each push, CI (`docker/comfyui/publish-identity`)
fetches just the identity layer's blob from the registry, takes the two files out, adds
`digest` and `refs` (`image`: the Docker Hub reference by digest; `mirror`: GHCR's) to the
manifest and the digest to the catalogue, and publishes them as GitHub release assets:

```
# one release per image build, by short commit
https://github.com/itsnotyoutoday/pod-harness/releases/download/images-<image>-<short sha>/manifest.json
https://github.com/itsnotyoutoday/pod-harness/releases/download/images-<image>-<short sha>/catalogue.json

# the newest of each image, always at the same URLs
https://github.com/itsnotyoutoday/pod-harness/releases/download/images-latest/index.json
https://github.com/itsnotyoutoday/pod-harness/releases/download/images-latest/<image>.manifest.json
https://github.com/itsnotyoutoday/pod-harness/releases/download/images-latest/<image>.catalogue.json
```

`index.json` is `{format: 1, updated_at, images: {<image>: {name, image, mirror?, tag,
digest, revision, release, manifest_url, catalogue_url, published_at, tags {image,
mirror?}}}}` — `image` is the Docker Hub reference by digest (`docker.io/itsnotyoutoday/
<image>@sha256:…`), `mirror` the GHCR one. An app can check a workflow against an image
before renting anything: `podh-catalogue check wf.json --object-info <image>.catalogue.json`
reads the published catalogue (its `nodes` keys are the class names `check` looks for).

### Submit a job

```bash
curl -X POST localhost:8000/v1/jobs \
  -H 'X-Podh-Token: dev' -H 'Content-Type: application/json' \
  -d '{
    "spec_version": 2,
    "pipeline": {"stages_from": "mywork.stages:STAGES",
                 "stages": ["extract", "transform", "load"]},
    "params": {"input": "data/batch-7"}
  }'
```

Add `?dry_run=true` to validate the spec, the stage names and the wiring **without running
anything**. It costs nothing and catches the typo that would otherwise surface fifteen
minutes into a paid pod.

### Watch it

```bash
curl localhost:8000/v1/jobs/$JOB                    # snapshot  → job_state, per-stage
curl localhost:8000/v1/jobs/$JOB/summary            # ~500-token digest; start here
curl localhost:8000/v1/jobs/$JOB/stages             # verification detail per stage
curl "localhost:8000/v1/jobs/$JOB/events?since_seq=0&wait=30"   # long-poll
curl "localhost:8000/v1/jobs/$JOB/log?tail=8192"    # bounded, reports total_bytes
```

> The job snapshot key is **`job_state`**, not `state`. Reading the wrong one gets you
> `None` and a silently wrong answer — which is why response keys are pinned in
> `contract.json`.

**A chunked run reads two ways.** `stages` answers *where is this job now* and is
last-writer, so three chunks over seven stages leave seven keys there. `chunks` —
`{chunk: {stage: outcome}}` — answers *which units of work reached which stage*, which is
the only view that can attribute a failure to a source. Every event carries a `chunk` field
for the same reason; without it, `align` on one chunk and `align` on another are the same
event twice. Chunk boundaries are emitted as events too, because the gap between chunks is
a multi-gigabyte fetch and unexplained silence is the problem the event stream exists to
remove.

### Reporting to a control plane

If the launcher supplies `PODH_CONTROL_URL`, `PODH_CONTROL_TOKEN` and
`PODH_CONTROL_FINGERPRINT`, the harness reports `/alive` while working, a `/boot` line per
boot step (GPU check, mount, spec, code, preflight, then `ready`), and `/done` when it
finishes — so a finished pod stops billing in a second rather than at the next poll. A
failure's `/done` carries a short `reason` and the `stage` that failed, so "bad GPU",
"missing spec" and "publish failed" are told apart without the console log. The exact
calls and field limits are in `contract.json` under `control_plane`.

`/alive` is sent only while the pod produces output — console lines or job events within
`PODH_HEARTBEAT_STALE_SEC` (default 1800). A hung workload therefore goes quiet and looks
stuck, which is the signal the control plane reaps on. A long stage that prints nothing
should call `ctx.progress()`.

On a GPU job (`PODH_GPU_COUNT>0`) the harness checks the GPU before mounting or downloading
anything, and a broken one ends the pod with `rc` 7 at stage `gpu_check`.

Three properties worth knowing:

- **The token is scoped to this pod.** It says "I am alive" and "I am finished" about
  itself and nothing else. It cannot terminate another pod, submit work or read a queue.
  The control plane's master credential never enters a pod.
- **TLS is verified by pinned certificate.** A control plane on a bare IP has no CA to
  vouch for it, so the harness checks for one exact certificate — stronger than trusting
  every CA. With no fingerprint set, verification stays at the default and a self-signed
  endpoint simply fails to connect; nothing silently downgrades.
- **None of it is load-bearing.** A pod that cannot reach the control plane still gets
  reaped, because the control plane polls the provider and that is the authority. Losing
  these requests costs money, never correctness.

### A batch job idles when it finishes

It does not exit. A container that exits is restarted by the provider, and a restarted
batch container runs the same job again — observed as eight runs of one benchmark across
eighteen boots, which would have continued until the budget ran out. Idling costs pennies
a minute and is bounded by the reaper; restarting costs a whole job each time and is
bounded by nothing.

### Cost control — read this

An in-pod timeout **cannot terminate a RunPod pod**. Verified on real hardware:
`runpodctl` invoked from inside a pod returns `Unauthorized`. So the harness carries no
in-pod deadline; a timer that can only detect, try to self-delete, be refused and log the
refusal reads like cost control and is not.

**Termination must come from outside**, from whatever launched the pod. The pod's part is
to make that easy: it heartbeats only while it produces output, so a hung pod goes quiet,
and it reports `/done` — with a reason — the moment it is finished. `pod-loader-rpc` does this with a context manager, a wall-clock
deadline thread, and a sweep for orphans.

---

## Integration

### Writing a workload

A stage is a class. Nothing needs to be imported from this repo — the harness accepts
anything with the right shape, so your workload can stay dependency-free if you prefer.

```python
# mywork/stages.py
from pod_harness.framework import Stage      # convenience; duck typing also works

class ExtractStage(Stage):
    name, number = "extract", 1
    produces = ("rows",)

    def execute(self, ctx):
        rows = read(ctx.params["input"])
        for i, r in enumerate(rows):
            ctx.progress(i, len(rows))        # sub-stage progress, throttled
        ctx.put("rows", rows)
        return {"count": len(rows)}

    def verify_outputs(self, ctx):            # optional, strongly encouraged
        rows = ctx.get("rows")
        return Verification(ok=bool(rows), checks={"rows": len(rows or [])})

STAGES = {"extract": ExtractStage, ...}
```

Point a spec at it with `"stages_from": "mywork.stages:STAGES"` and the harness imports the
registry from whatever code root it was given.

### Delivering your code

Publish `code/` to object storage and point the pod at it with `PODH_CODE_ROOT`. The pod
then holds **no credentials and no git access** — it reads files that were already placed
where it can see them.

Do not vendor this package into your workload (no submodules): the code root is prepended
to `PYTHONPATH`, so a vendored copy would shadow the image's and you would have two
harnesses resolved by import order. Depend on it for *development* only:

```toml
[project.optional-dependencies]
dev = ["pod-harness @ git+https://github.com/itsnotyoutoday/pod-harness.git@<sha>"]
```

### Driving it from your own launcher

The harness shares **no code** with whatever launches it. They agree on `contract.json` and
nothing else: the job spec schema, the event/status schema, the environment variables, and
the `/v1` endpoints.

A live copy is served at `GET /v1/contract`, so a launcher can validate against the
interface of the *exact image* it is about to run rather than a vendored copy that may have
drifted.

Environment the harness requires (it refuses to start without them, rather than guessing —
a guess is a second definition of something the launcher owns):

```
PODH_WORKSPACE        root of the mounted data view
PODH_LOG_ROOT         where run output goes
PODH_MODE             batch | serve | shell
PODH_JOB_ID           what this pod owns — assigned, never discovered
PODH_JOB_SPEC         path to the spec, written before the pod is created
PODH_RUN_PREFIX       object-key prefix for published status
PODH_WRITE_PREFIXES   the only prefixes this job may write to
```

`PODH_WRITE_PREFIXES` is a grant, not a hint: a write outside it raises. A harness that
could write anywhere could overwrite the one dataset you cannot regenerate.

[`pod-loader-rpc`](https://github.com/itsnotyoutoday/pod-loader-rpc) is a reference
implementation — provision, publish code, launch, poll, reap.

---

## Development

```bash
pip install -e ".[serve,dev]"

python docker/assert_contract.py        # the API honours contract.json
python docker/assert_independence.py    # no launcher code leaked into the harness
docker build -f docker/test.Dockerfile -t pod-harness-test .
```

Both checks run in CI and in every image build. They are not style rules — each guards a
failure that has happened: a shell script pointing at a moved Python module (a pod billed
13 minutes serving 404), and a storage layout defined in two places that drifted three
times in one day.

## Licence

MIT.
