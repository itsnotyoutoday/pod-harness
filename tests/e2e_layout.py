"""End-to-end: run the engine for real and assert on the STORE.

## Why this exists

Six defects in one session, each found by launching a pod, waiting ten minutes and reading
a log that died with the pod. Four of them were the same shape: every piece individually
correct, wired together wrongly. prepare wrote where publish did not read. config resolved
a root nobody else used. The harness called publish not at all.

Unit tests with a stubbed mount cannot catch that, because the stub agrees with whichever
side wrote it. The only test that can is one that runs the real sequence and then asks the
STORE what is actually there.

So this seeds a tiny two-source corpus under a scratch prefix, runs the engine with a
fixture workload, and asserts the resulting object layout. Every assertion below is a
promise this system has made and broken at least once tonight.

## What it covers, and what it does not

Covers: roots resolution, prepare, the stage loop, chunk enter/exit, publish, eviction
ordering, and the layout. That is where every defect tonight lived.

Does NOT cover: the container image, Caddy, the API sidecar, the heartbeat and the control
plane. Those need a pod or a container, and they are a separate tier. Stating the gap
rather than implying coverage.

    python3 tests/e2e_layout.py            against the configured store, scratch prefix
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE / "fixture_workload"))

PREFIX = f"_e2e/{int(time.time())}"
ok: list[bool] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    ok.append(bool(cond))
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  — {detail}" if detail and not cond
                                                 else ""), flush=True)


def main() -> int:
    from pod_harness.objectstore import get_storage

    st = get_storage(os.environ.get("E2E_PROFILE") or None)
    cfg = st.require()
    print(f"\n  store: {cfg.bucket}  scratch prefix: {PREFIX}\n")

    root = Path(tempfile.mkdtemp(prefix="e2e-"))
    # The mount fetches into <workspace>/scratch, and podh-roots derives the workload's
    # roots from THAT — not from the workspace. Resolving them any other way here would
    # test a wiring the pod does not use, which is the entire failure mode this exists to
    # catch, reproduced inside the test itself.
    scratch = root / "scratch"
    corpus = scratch / "corpus"
    sources = ("srcA", "srcB")

    # --- seed a two-source corpus in the store -----------------------------------------
    seeded = 0
    for sid in sources:
        for i in range(3):
            st.client.put_object(Bucket=cfg.bucket,
                                 Key=f"{PREFIX}/corpus/raw/{sid}/f{i}.wav",
                                 Body=b"RIFF" + bytes(600))
            seeded += 1
    st.client.put_object(Bucket=cfg.bucket, Key=f"{PREFIX}/corpus/manifest.json",
                         Body=json.dumps({"sources": list(sources)}).encode())
    print(f"  seeded {seeded} object(s) across {len(sources)} source(s)\n")

    # --- run the engine ------------------------------------------------------------------
    job = f"job_E2E{int(time.time())}"
    os.environ.update(
        PODH_JOB_ID=job,
        PODH_WORKSPACE=str(root),
        PODH_CORPUS_ROOT=str(corpus),
        PODH_OUT_ROOT=str(scratch / "out"),
    )
    spec = {
        "spec_version": 2,
        "mount": {"kind": "object", "root": PREFIX + "/corpus", "chunk_by": "source",
                  "roots": {"corpus_root": "corpus", "out_root": "out"}},
        "pipeline": {"stages_from": "fixture.stages:STAGES",
                     "stages": ["acquire", "normalize", "measure", "profile"]},
        "params": {"region": "_e2e", "sources": [{"id": s} for s in sources]},
    }

    from pod_harness import execute_job

    result = execute_job.run(spec, job_id=job)
    check("every stage passed", result.get("ok"), json.dumps(result)[:200])
    check("ran once per chunk", result.get("chunks") == len(sources),
          f"chunks={result.get('chunks')}")

    # --- publish, exactly as the harness does -------------------------------------------
    from pod_harness.mount import for_spec

    m = for_spec(spec)
    for local, key, skip in (
            (scratch / "out", f"{PREFIX}/runs/{job}/out", False),
            (scratch / "assets" / "derived", f"{PREFIX}/assets/derived", False),
            (scratch / "assets" / "profiles", f"{PREFIX}/assets/profiles", False),
            (corpus / "raw", f"{PREFIX}/corpus/raw", True)):
        m.publish_tree(local, key, skip_existing=skip)

    # --- assert on the STORE, not on return values ---------------------------------------
    def listing(p: str) -> dict:
        out = {}
        for page in st.client.get_paginator("list_objects_v2").paginate(
                Bucket=cfg.bucket, Prefix=f"{PREFIX}/{p}"):
            for o in page.get("Contents", []):
                out[o["Key"][len(PREFIX) + 1:]] = o["Size"]
        return out

    runs = listing(f"runs/{job}/out/")
    check("the run published its own outputs", len(runs) > 0)

    derived = listing("assets/derived/")
    check("assets/derived is populated", len(derived) > 0)
    for sid in sources:
        check(f"assets/derived has {sid}",
              any(f"/{sid}/" in k for k in derived))

    profiles = listing("assets/profiles/")
    check("assets/profiles is populated", len(profiles) > 0)

    cur = [k for k in profiles if k.endswith("/current")]
    check("a `current` pointer exists", bool(cur))
    if cur:
        body = st.client.get_object(
            Bucket=cfg.bucket, Key=f"{PREFIX}/{cur[0]}")["Body"].read().decode().strip()
        check("`current` names this run", body == job, f"points at {body!r}, wanted {job!r}")

    # THE REDUCE SAW EVERY CHUNK.
    #
    # Chunk-scoped stages run once per source; job-scoped stages run once and are handed
    # whatever accumulated. If a chunk stage REPLACES that accumulator instead of adding to
    # it, the reduce silently fits the last chunk alone — and reports ok, because from
    # inside it there is nothing to notice. Every layout check above still passes: the
    # objects are all in the right places, and one of them is simply wrong.
    #
    # That is not hypothetical. A three-source neutro run wrote a confident profile from
    # one source, produced no second variety, left the intersection with nothing to
    # intersect, and promoted the result over a good one. The fixture happened to
    # accumulate correctly, so this suite passed 12/12 across the whole defect.
    prof_key = [k for k in profiles if k.endswith(f"/{job}/profile.json")]
    check("the reduce stage ran over EVERY chunk, not just the last", bool(prof_key))
    if prof_key:
        body = json.loads(st.client.get_object(
            Bucket=cfg.bucket, Key=f"{PREFIX}/{prof_key[0]}")["Body"].read())
        saw = sorted(body.get("sources") or [])
        check("the profile names every source", saw == sorted(sources),
              f"profile fitted {saw}, corpus had {sorted(sources)}")

    # corpus/ is external input. Derived output inside it collapses two retention policies.
    check("nothing was written under corpus/out", not listing("corpus/out/"))
    raw = listing("corpus/raw/")
    check("corpus/raw was not re-uploaded", len(raw) == seeded,
          f"{len(raw)} objects, seeded {seeded}")

    # Eviction is what makes chunking bound anything: with both sources resident the whole
    # point is lost, and the failure only shows up as out-of-space on a real corpus.
    left = [d.name for d in (corpus / "raw").glob("*") if d.is_dir()]
    check("chunks were evicted from local disk", not left, f"still present: {left}")

    shutil.rmtree(root, ignore_errors=True)
    if os.environ.get("E2E_KEEP") != "1":
        for page in st.client.get_paginator("list_objects_v2").paginate(
                Bucket=cfg.bucket, Prefix=PREFIX):
            for o in page.get("Contents", []):
                st.client.delete_object(Bucket=cfg.bucket, Key=o["Key"])
        print(f"\n  cleaned {PREFIX}")

    print(f"\n  {sum(ok)}/{len(ok)} checks passed\n")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
