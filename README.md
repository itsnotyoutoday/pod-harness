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
`PODH_CONTROL_FINGERPRINT`, the harness reports `/alive` while working and `/done` when it
finishes — so a finished pod stops billing in a second rather than at the next poll.

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
`runpodctl` invoked from inside a pod returns `Unauthorized`. `MAX_LIFE_SEC` will detect
the timeout, attempt self-delete, be refused, log the refusal — and the pod keeps billing.

Treat the in-pod ceiling as a detector. **Termination must come from outside**, from
whatever launched the pod. `pod-loader-rpc` does this with a context manager, a wall-clock
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
