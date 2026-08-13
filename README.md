# lingua-backend

Container images and control surface for the True Lingua compute zone.

This repo holds the things that are **not** the pipeline: the image definitions, the in-pod
observability harness, the `/v1` control API, and the provider/mount/storage abstractions
that keep all of it portable. The pipeline itself (`pipeline/`, `runners/`) merges in from
`linguabackend_pipeline` — see [Merging the pipeline](#merging-the-pipeline).

---

## The one-paragraph version

Third-party dependencies and third-party pretrained weights are **baked into a public image**.
Our code, corpora and rulesets are **not** — they arrive at runtime from the volume or from
object storage. That split is what lets the GHCR package stay public, which is what removes
registry auth from every pod template and sidesteps the Docker Hub pull rate-limit that
kills pod fleets. It also means changing pipeline code needs no image rebuild.

---

## Layout

```
docker/
  harness/                 identical in every image, CPU or GPU
    lingua-init            ENTRYPOINT: tee → console.log, seed models, start sidecars, exec
    lingua-seed-models     link baked /opt weights into the cache tree
    lingua-preflight       one diagnostic block at job start
    lingua-watchdog        idle + lifetime backstop
    lingua-self-delete     best-effort; see "cost control" below
    Caddyfile              :8000 — static logs + reverse-proxy to /v1
  pipeline.Dockerfile      micromamba + MFA + CPU torch + baked weights   (build zone)
  test.Dockerfile          same harness, no conda layer — ~1 min local build
serve/                     runs INSIDE the container
  events.py                the status protocol (stdlib only)
  jobs.py                  submit / cancel / validate over runners.execute_job
  api.py                   the /v1 FastAPI surface
control/                   runs OUTSIDE the compute
  providers.py             provider registry over runners.provider
  status_source.py         where status is READ from (http / s3 / local / chained)
  objectstore.py           S3 profile resolution — RunPod, R2, GCS, MinIO
  mount.py                 VolumeMount vs ObjectMount
runners/status.py          reporter that hooks into the pipeline's Runner.run()
tests/fixture_pipeline/    stands in for the pipeline during harness tests
```

---

## Local development

`docker-compose.yml` **is** the local `VolumeMount` — there is no separate "docker local"
strategy. On RunPod the provider attaches a network volume; here Docker binds
`${CORPUS_DIR}` (default `../corpus_data`) at the same container paths. The pipeline cannot
tell the difference, which is what makes a local run a real rehearsal rather than an
approximation.

```bash
docker compose run --rm pipeline -m runners.execute_job --spec /workspace/jobs/x.json
docker compose up -d dev            # long-lived, full harness, /v1 on :8000
docker compose up -d harness        # lean image, ~1 min build
docker compose --profile s3 up -d minio    # local S3 for object/fuse strategies
```

`pipeline` is named that because `runners/provider.py:LocalProvider` shells
`docker compose build pipeline` and `docker compose run --rm ... pipeline` — the name is an
interface, not a preference. Note that LocalProvider overrides the entrypoint, so batch runs
bypass `lingua-init`: no Caddy, no `console.log`, stdout straight to your terminal. Status is
still written, because reporting lives in `runners/status.py` inside the pipeline rather than
in the harness. Use `dev` to test the harness itself.

The whole harness runs in OrbStack with no cloud involvement. This is the loop to use —
never push to CI to find out whether an entrypoint works.

```bash
docker build -f docker/test.Dockerfile -t lingua-harness-test .
docker run -d --name lht -p 8000:8000 -e LINGUA_API_TOKEN=dev -e MAX_IDLE_SEC=0 \
  lingua-harness-test

curl -s localhost:8000/v1/health
curl -s -H 'X-Lingua-Token: dev' localhost:8000/v1/ | jq

# free validation — runs nothing, costs nothing
curl -s -X POST 'localhost:8000/v1/jobs?dry_run=true' -H 'X-Lingua-Token: dev' \
  -H 'Content-Type: application/json' \
  -d '{"job_id":"t","stages":["normalize","typo"]}' | jq

curl -s -X POST localhost:8000/v1/jobs -H 'X-Lingua-Token: dev' \
  -H 'Content-Type: application/json' \
  -d '{"job_id":"t1","stages":["normalize","embed","measure"]}'
curl -s -H 'X-Lingua-Token: dev' localhost:8000/v1/jobs/t1/summary | jq
```

The test image swaps in `tests/fixture_pipeline/` for the real pipeline. It proves the
harness, the API and the event protocol; it does **not** prove that MFA installs or that the
weights bake — the build-time sanity check at the end of `pipeline.Dockerfile` guards those.

---

## Startup: one image, told how to run

The container is **instructed** at start rather than assuming. Two independent env vars, and
every combination is legal — which is what lets a batch pod, a dev pod, a local compose run
and a future service be the same artifact configured differently.

```
LINGUA_MODE         batch | serve | shell        (default: serve)
LINGUA_MOUNT_KIND   volume | fuse | object | auto (default: auto)
```

| `LINGUA_MODE` | what it does |
|---|---|
| `batch` | preflight, run `LINGUA_JOB_SPEC`, exit. Refuses if the spec is missing or unreadable |
| `serve` | idle in the foreground, serve `/v1`. A pod that exits immediately bills, reports no runtime and looks broken |
| `shell` | drop to bash for interactive debugging |

An explicit `command:` always overrides the mode.

| `LINGUA_MOUNT_KIND` | at startup |
|---|---|
| `volume` | verify the bind mount / network volume attached, and **warn loudly if it is empty** — a stage over zero files can still report success |
| `fuse` | `rclone mount` before anything reads a path; refuses with an actionable hint if `/dev/fuse` is absent |
| `object` | nothing; each job's `prepare()` pulls its own working set |
| `auto` | degrade `volume → fuse → object` |

All six combinations verified locally, including FUSE against MinIO with atomic rename
holding through the startup mount.

## The `/v1` surface

Served by the pod, and by the control plane at the same paths. A client written against one
works against the other, which is what lets the compute move from pod to serverless to a
different vendor without touching the caller.

```
GET    /v1/health                     liveness, no auth
GET    /v1/                           discovery: endpoints, live stage vocabulary, schema
POST   /v1/jobs?dry_run=true          validate; runs nothing
POST   /v1/jobs                       submit; idempotent on job_id
GET    /v1/jobs                       list
GET    /v1/jobs/{id}                  status snapshot — the cheap poll target
GET    /v1/jobs/{id}/summary          ~500-token digest — start here
GET    /v1/jobs/{id}/log?tail=8192    bounded; always reports total_bytes
GET    /v1/jobs/{id}/events           ?since_seq=N&wait=S — long-poll
GET    /v1/jobs/{id}/artifacts        produced files
DELETE /v1/jobs/{id}                  cancel
```

Auth is a single `X-Lingua-Token` header, so it is curl-able in one line. The API refuses to
serve when `LINGUA_API_TOKEN` is unset rather than defaulting open.

### Designed for an agent as well as a webapp

Both audiences submit work and watch it; the asymmetry is that an agent pays for every byte
it reads, in a context window it cannot get back. So:

- **no unbounded responses.** Log and event reads are capped and always report what was
  omitted. A silently truncated response is worse than a small one.
- **`/summary`** answers "what happened" without reading a log.
- **`dry_run` is free and total**, built on `Runner.wiring()`/`plan()` which were written to
  be trusted "without spending an hour or a pod". A typo'd stage name costs nothing.
- **`GET /v1/` is self-describing**, so a caller that has lost its context recovers in one
  call.
- **errors carry a `hint`** naming the next action.

### Transports

One event schema, several ways to receive it — because the transport that works depends on
where the compute is:

| transport | works on | for |
|---|---|---|
| long-poll `?wait=30&since_seq=N` | everywhere, incl. serverless and S3 replay | agents |
| SSE | pods (and serverless with ports exposed) | webapp progress |
| `status.json` in S3 | everywhere, always, and after the compute is gone | fallback |

Events carry a monotonic `seq`; every transport resumes from it.

---

## Portability

Three independent axes, each an interface with adapters. Adding an implementation is one
class plus one registry line; nothing above changes.

| axis | interface | today | tomorrow |
|---|---|---|---|
| where work **runs** | `runners.provider.Provider` via `control/providers.py` | local, runpod | vast, lambda, modal, bare metal |
| where status is **read** | `control/status_source.StatusSource` | http, s3, local, chained | anything |
| how data is **mounted** | `control/mount.MountStrategy` | volume, object | anything |
| which **object store** | `control/objectstore.py` profiles | runpod s3, r2 | gcs, minio, s3 |

### The actual RunPod coupling

It is **not** the S3 API — `Storage` already takes an explicit `endpoint_url`. It is the
**mount**. `batch_pod.py` passes `network_volume_id` at pod-create, RunPod attaches its own
network volume at `/workspace`, and the pipeline does plain file I/O. The pod receives no S3
credentials at all. R2 cannot be attached that way, and FUSE inside the container would need
`/dev/fuse` + `SYS_ADMIN`, which RunPod restricts.

Note that a RunPod network volume is **not** local disk (their docs rate it "variable
(network)"), so every strategy is network-bound and the gap is protocol overhead rather than
locality.

### Three mount strategies, chosen per job by `mount.kind`

| kind | mechanism | needs | when |
|---|---|---|---|
| `volume` | provider attaches it; plain file I/O | nothing on the pod | RunPod today — fastest, no credentials |
| `fuse` | `rclone mount --vfs-cache-mode full` | `/dev/fuse` + `SYS_ADMIN`, rclone in image | POSIX over any S3 — **not RunPod**, see below |
| `object` | pull working set → run → push | nothing special | the guaranteed floor: serverless, any vendor |

`best_available()` degrades `volume → fuse → object` when a job doesn't name one, so the same
spec runs on a pod with a volume, a serverless worker with neither, or a laptop.

**On RunPod, use `volume`.** The network volume is the native path: fastest, and no
credentials ever reach the pod. `fuse` and `object` exist for portability, not for RunPod.

**FUSE does not work on RunPod — settled.** Three independent confirmations:

| evidence | finding |
|---|---|
| our probe on a live pod | `/dev/fuse` absent; `CapEff 0xa80405fb` → **MKNOD granted, SYS_ADMIN denied**, so `mount(2)` cannot succeed even after creating the device node |
| community reports | FUSE unsupported — requires container privileges |
| [skypilot#8592](https://github.com/skypilot-org/skypilot/issues/8592) | JuiceFS mount fails on RunPod, **still fails with `--privileged`**, works unchanged on AWS |

`FuseMount.probe()` therefore refuses at startup with a pointer to `object`, rather than
failing obscurely mid-job.

**Accepted limitation (2026-08-13).** Mounting *foreign* object storage (R2, GCS) inside a
RunPod pod is not possible, and we are **not** building an fsspec or JuiceFS abstraction to
work around it. RunPod already provides a real POSIX mount — the network volume — which
covers the need, and is simultaneously readable over S3 from outside. Building a portability
layer for a provider we are not on is speculative. Revisit if we ever target elsewhere; the
`MountStrategy` interface makes that a contained change rather than a rewrite.

Worth knowing if it is revisited: neither fsspec nor the JuiceFS Python SDK can help **MFA**,
because Kaldi is a subprocess that needs real files on disk. Materialisation is required
whichever abstraction is chosen. And JuiceFS additionally needs an always-on metadata engine
(Redis/MySQL/TiKV) reachable from every pod.

FUSE remains a valid strategy for providers that permit it, and it does work: verified
against MinIO with POSIX read and — the risky part — **atomic rename surviving** under
`--vfs-cache-mode full`, which matters because S3 has no native rename and
`serve/events.py._atomic_write` depends on one.

### Object store profiles

```bash
LINGUA_S3_PROFILE=r2
LINGUA_S3_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
LINGUA_S3_R2_BUCKET=lingua
LINGUA_S3_R2_ACCESS=…
LINGUA_S3_R2_SECRET=…
```

Unprefixed `LINGUA_S3_*` is the default profile. Signing regions are resolved per endpoint —
R2 needs `auto`, RunPod needs the region embedded in its host (`us-nc-1`), and getting this
wrong produces an `AccessDenied` that reads like a credentials problem.

---

## Images

Built by `.github/workflows/build-images.yml` on push to `main`, pushed to GHCR:

```
ghcr.io/<owner>/lingua-pipeline:latest
ghcr.io/<owner>/lingua-pipeline:<short-sha>     pin a build to a job
```

CI runs the harness smoke tests first (~2 min) so a broken endpoint fails fast rather than
after the ~35-minute pipeline build. First build is slow because conda MFA is slow;
subsequent builds are minutes thanks to layer ordering plus per-image cache scope.

**Layer count is not the cost.** Docker pulls layers in parallel, and more, smaller layers
make an incremental pull cheaper. Layer **order** is what matters, and it runs
slowest-and-most-stable first so a code or harness change never rebuilds the conda layer.

### The `/opt` rule

Baked weights live in `/opt`, never under `/workspace` or `/corpus`. **A mount shadows
whatever the image had at that path**, so weights baked under the volume mount are invisible
at runtime and get downloaded anyway. `lingua-seed-models` symlinks `/opt` into the cache
tree at start; the cache root itself stays writable on the volume, because the HF cache can
grow to gigabytes and container disk is small.

---

## Cost control

**An in-pod ceiling is not cost control.** Verified on a real pod: `runpodctl` from inside a
RunPod pod returns `Error: Unauthorized`. There is no pod-scoped credential, so
`lingua-watchdog` will detect a timeout, call self-delete, be refused, and log the refusal —
while the pod keeps billing. That settles the contradiction between plexus's
`trainer.Dockerfile` and its `runpod_cleanup.py`: the latter was right.

So termination is external, and automatic:

```python
from control.reaper import pod
with pod(create_kwargs, budget_min=15) as p:
    ...                       # pod CANNOT outlive the budget
```

Four independent kill paths, because each covers a failure the others don't:

| path | covers |
|---|---|
| `finally` | normal exit, ordinary exceptions |
| SIGINT/SIGTERM handlers | operator pressing ctrl-C |
| `atexit` | shutdown paths that skip `finally` |
| **deadline thread** | wall clock, *even while the main thread is blocked on a hung call* |

That last one is the one that matters, and it's tested: a pod launched with `budget_min=2`
while the main thread slept for 10 minutes was killed at 2 minutes.

Anything a `kill -9`'d process left behind is collected by the janitor:

```bash
python control/reaper.py list                  # what's billing right now
python control/reaper.py sweep --dry-run       # what would be collected
python control/reaper.py sweep                 # collect it
```

### Ephemeral pods are named differently from real work — deliberately

`batch_pod.py` names real jobs `lingua-<job_id>`. `sweep()` only touches **`lingua-test-`**,
which only `reaper.pod()` assigns, and it **refuses** a broader prefix without `force=True`.
A corpus build that is meant to run for days is invisible to the janitor by construction —
a deliberate long job must never be at the mercy of an age limit.

`lingua-watchdog` still earns its place as a loud signal in the log that a job wedged, and
on providers that do permit self-delete. It's a smoke alarm, not a sprinkler.

---

## Merging the pipeline

Not yet done, to avoid disturbing a run in progress. When it is:

1. Copy `pipeline/`, `runners/`, `corpora/`, `jobs/`, `runctl.py` in from
   `linguabackend_pipeline`. Verified clean: `.env` and `*.key` are gitignored and nothing
   sensitive sits in the ~100 committable files.
2. Keep `runners/status.py` from here; apply the six-line `reporter=` hook to
   `Runner.run()` — the exact diff is in that file's docstring.
3. Point `batch_pod.py` at `ghcr.io/<owner>/lingua-pipeline:latest` and delete its
   `BOOTSTRAP` pip install — that is the ~2-3 minutes per pod start this repo exists to
   remove.
4. Reconcile `requirements-pipeline.txt` with `requirements-runpod.txt`.
5. Retire `Dockerfile`, `Dockerfile.align`, `Dockerfile.gpu`, `Dockerfile.runpod` —
   `Dockerfile.pod`'s own header says it supersedes two of them, and `pipeline.Dockerfile`
   supersedes it.
