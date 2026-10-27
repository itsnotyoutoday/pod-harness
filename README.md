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

| image | size | for |
|---|---|---|
| `ghcr.io/itsnotyoutoday/pod-harness` | ~240 MB | anything that is not machine learning |
| `ghcr.io/itsnotyoutoday/lingua-pipeline` | ~6 GB | forced alignment + audio measurement |

Both are public, so pods pull without registry auth and without hitting Docker Hub's
anonymous rate limit.

Most jobs want the small one. It carries the harness, the API and boto3 — nothing else.

### Run one locally

```bash
docker run -d -p 8000:8000 \
  -e PODH_API_TOKEN=dev \
  -e PODH_WORKSPACE=/workspace \
  -e PODH_LOG_ROOT=/workspace/runs \
  -v "$PWD/work:/workspace" \
  ghcr.io/itsnotyoutoday/pod-harness:latest

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
| `models` | the manifest's presigned files into `$PODH_MODELS_ROOT`, 2 at a time, resumable (aria2c if present), fresh links on expiry, size/sha checked, per-path state reported |
| `addons` | git repos at full commits, validated again on the pod, `pip` into the workload's python, links (never outside their roots), import probes; `optional` items only warn; the set is kept as a kit |
| `app` | the app's code fetched with ITS token (`Authorization: Bearer $<fetch.auth_env>`), unpacked into `$PODH_APP_ROOT`, its `run` executed there (`{app}` in argv = that dir) — before the workload, so it can add files and `KEY=value` lines to `$PODH_WORKLOAD_ENV` |
| `workload` | ComfyUI (models wired through `extra_model_paths.yaml`) or a command; up within `up_timeout_s`; log scanned for fatal strings; restarted once |
| `ready` | boot line with stage `ready` |
| `app.serve` | the app's long-running process: one restart, exit 0 ends the pod, drain/park → SIGTERM then SIGKILL after `serve_grace_s` |

A failure reports `done(rc, reason, stage, log_tail)` and idles (`--exit-on-fail` exits). A
resumed pod skips what its disk already has. After ready a watcher fetches models added
mid-life (`models_rev` on the heartbeat reply) without ever ending the pod.

Two ways to run it:

```bash
# an image built for it
ENTRYPOINT ["podh-boot"]

# a catalog image: the provider's start command
bash -lc "pip install 'pod-harness @ git+https://github.com/<owner>/pod-harness@<tag>' && podh-boot"
```

The app stage, the workload and `app.serve` get `PODH_COMFY_ROOT`, `PODH_COMFY_PY`,
`PODH_MODELS_ROOT`, `PODH_WORKLOAD_ENV`, `PODH_JOB_ID`, `PODH_GPU_COUNT`, `PODH_APP_ROOT` and
the local API, `PODH_LOCAL_URL` + `PODH_LOCAL_TOKEN` (127.0.0.1 only). `app.run` and
`app.serve` also get the current job's app env from `GET /v1/pod/env` (read on every boot,
so a resumed pod serving a new job gets that job's), over the container env.

- `POST /busy {"busy", "detail"?}` — rides on the next heartbeat; pod-control's idle clock
  runs only while the pod is not busy. `podh-report busy [--detail D]` / `podh-report idle`.
- `GET /state` → `{drain, park, extend_until, models_rev, models, …}` from the last
  heartbeat reply, so a worker stops claiming work on drain without asking its app.
- `POST /done {"rc", "reason"?, "stage"?}` — the only exit. `podh-report done` uses it.
- `POST /workload/restart` — restart the workload (a job wedged in a C-level node ignores
  ComfyUI's /interrupt); 200 once it answers again, non-2xx with the error if it does not.

`podh-kit get|put <name> <app-key> <dir>` caches anything slow to build (a venv, wheels):
`get` exits 0 with the dir filled or 2 when nobody has built it yet; the key also carries
the python, torch, CUDA and image the build is for. The exact routes and fields are in
`contract.json` under `control_plane.pod_calls`, `local_api` and `boot`.

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
