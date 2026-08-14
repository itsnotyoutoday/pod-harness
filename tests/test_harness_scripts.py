"""Execute the harness scripts, rather than only parsing them.

## Why this exists

`podh-publish` referenced a variable that was never defined. Python raises NameError only
when the line runs, so the file imported fine, `ast.parse` was happy, and the build guards
— which check that modules import and that env names resolve — all passed. The failure
appeared on a pod: seven stages passed, publish crashed before uploading anything, and the
run lost its entire product.

That is the third time a harness script has been broken in a way no static check could see.
The pattern is stable: these scripts are the least-exercised code in the system and the most
expensive to get wrong, because each mistake costs a pod-hour to discover.

So these tests RUN them, against a temporary directory tree, with the object store stubbed
so nothing touches the network. They are fast and they would have caught the NameError.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
HARNESS = HERE.parent / "docker" / "harness"
sys.path.insert(0, str(HERE.parent / "src"))


class _FakeMount:
    """Stands in for ObjectMount: records what would be published, uploads nothing."""

    def __init__(self):
        self.calls = []

    def publish_tree(self, local, prefix, skip_existing=False):
        p = pathlib.Path(local)
        self.calls.append((str(p), prefix, skip_existing))
        if not p.is_dir():
            return {"published": False, "skipped": "absent"}
        files = [f for f in p.rglob("*") if f.is_file()]
        if not files:
            return {"published": False, "skipped": "empty"}
        return {"published": True, "prefix": prefix, "files": len(files),
                "skipped": 0, "bytes": sum(f.stat().st_size for f in files)}


def _load(script: str) -> dict:
    src = (HARNESS / script).read_text()
    g: dict = {"__name__": "test"}
    exec(compile(src, script, "exec"), g)      # noqa: S102 — executing our own script
    return g


def _tree() -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "scratch" / "out").mkdir(parents=True)
    (d / "scratch" / "out" / "result.json").write_text("{}")
    norm = d / "scratch" / "assets" / "derived" / "normalized" / "src1"
    norm.mkdir(parents=True)
    (norm / "a.wav").write_bytes(b"x" * 32)
    (d / "scratch" / "corpus" / "raw").mkdir(parents=True)
    spec = {"mount": {"kind": "object", "root": "corpus/"}, "params": {}}
    (d / "spec.json").write_text(json.dumps(spec))
    return d


def _env(d: pathlib.Path) -> None:
    os.environ.update(
        PODH_JOB_SPEC=str(d / "spec.json"), PODH_JOB_ID="job_TEST",
        PODH_OUT_ROOT=str(d / "scratch" / "out"),
        PODH_CORPUS_ROOT=str(d / "scratch" / "corpus"))


def test_publish_runs_and_covers_every_tree():
    """The NameError test. Also pins WHICH trees get published, and with what settings."""
    import pod_harness.mount as M

    d = _tree()
    _env(d)
    fake = _FakeMount()
    M.for_spec = lambda spec: fake
    g = _load("podh-publish")
    assert g["main"]() == 0

    by_prefix = {c[1]: c for c in fake.calls}
    assert set(by_prefix) == {"runs/job_TEST/out", "assets/derived",
                              "assets/profiles", "corpus/raw"}, \
        f"a tree stopped being published: {sorted(by_prefix)}"

    # corpus/raw must never re-upload what the store already has: it holds 11 GB and
    # acquire only ever adds to it.
    assert by_prefix["corpus/raw"][2] is True, "corpus/raw lost skip_existing"
    assert by_prefix["runs/job_TEST/out"][2] is False

    # Order is a policy about what survives a PARTIAL publish, not a preference. A run was
    # cut short while pushing 110 MB of reproducible audio, and the 20 KB profile — the
    # thing the pipeline exists to produce — was still queued behind it and was lost.
    order = [c[1] for c in fake.calls]
    assert order.index("assets/profiles") < order.index("assets/derived"), \
        "the irreplaceable profile must publish before the reproducible audio"
    assert order.index("runs/job_TEST/out") < order.index("assets/derived"), \
        "this run's results must publish before the reproducible audio"


def test_publish_fails_when_nothing_was_produced():
    """Seven green stages over an empty output tree must NOT be a successful run."""
    import pod_harness.mount as M

    d = pathlib.Path(tempfile.mkdtemp())
    (d / "scratch" / "out").mkdir(parents=True)          # exists, but empty
    (d / "spec.json").write_text(json.dumps({"mount": {"kind": "object"}}))
    _env(d)
    M.for_spec = lambda spec: _FakeMount()
    g = _load("podh-publish")
    assert g["main"]() == 1, "publishing nothing was treated as success"


def test_publish_allows_empty_when_the_spec_says_so():
    """A probe or benchmark genuinely produces nothing, and has to say so explicitly."""
    import pod_harness.mount as M

    d = pathlib.Path(tempfile.mkdtemp())
    (d / "scratch" / "out").mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps(
        {"mount": {"kind": "object", "allow_empty_output": True}}))
    _env(d)
    M.for_spec = lambda spec: _FakeMount()
    g = _load("podh-publish")
    assert g["main"]() == 0


def test_roots_resolver_refuses_to_guess():
    """A named-but-absent spec must be an error, not a plausible default.

    mount_roots() answers an empty spec with a generic default, and that default once got
    reported as "applied 2 from the job spec" over a spec that had never been opened.
    """
    os.environ["PODH_JOB_SPEC"] = "/nonexistent/spec.json"
    try:
        _load("podh-roots")
    except SystemExit as e:
        assert e.code and "does not exist" in str(e.code)
    else:
        raise AssertionError("podh-roots accepted a spec path that does not exist")


def test_logs_never_change_the_outcome():
    """A failure to upload a log must not fail the job."""
    d = pathlib.Path(tempfile.mkdtemp())
    os.environ["PODH_LOG_DIR"] = str(d)
    os.environ["PODH_JOB_ID"] = "job_TEST"
    (d / "console.log").write_text("hello")
    g = _load("podh-logs")
    assert g["main"]() == 0        # no store configured in the test env


def test_chunk_scoped_stages_run_once_per_chunk():
    """The disk-bound case: heavy stages per chunk, the rest once over the accumulation.

    An 11 GB corpus does not fit beside its outputs on a 20 GB container disk, and RunPod
    caps that disk at 20. This is the mechanism that makes the full corpus runnable at all,
    so it is worth pinning the ORDER — publish before evict, reduce after every chunk.
    """
    from pod_harness.framework import Context, Runner, Stage, Verification

    seen = []

    class Heavy(Stage):
        name, number, scope = "heavy", 1, "chunk"
        produces = ("m",)

        def execute(self, ctx):
            seen.append(("heavy", ctx.params.get("only")))
            ctx.artifacts.setdefault("m", []).append(ctx.params.get("only"))
            return {"n": 1}

        def verify_outputs(self, ctx):
            return Verification(ok=True)

    class Reduce(Stage):
        name, number = "reduce", 2
        requires = ("m",)

        def execute(self, ctx):
            seen.append(("reduce", tuple(ctx.get("m"))))
            return {"total": len(ctx.get("m"))}

        def verify_outputs(self, ctx):
            return Verification(ok=True)

    ctx = Context(region="r")
    ctx.chunks = ("a", "b", "c")
    ctx.enter_chunk = lambda k: (seen.append(("enter", k)),
                                 ctx.params.__setitem__("only", k))
    ctx.exit_chunk = lambda k: seen.append(("exit", k))

    r = Runner("t", [Heavy(), Reduce()]).run(ctx)
    assert r["ok"] and r["chunks"] == 3
    assert [s for s in seen] == [
        ("enter", "a"), ("heavy", "a"), ("exit", "a"),
        ("enter", "b"), ("heavy", "b"), ("exit", "b"),
        ("enter", "c"), ("heavy", "c"), ("exit", "c"),
        ("reduce", ("a", "b", "c"))], seen
    # every chunk's stage result is attributed to its chunk, or a failure cannot be located
    assert {s.get("chunk") for s in r["stages"] if s["stage"] == "heavy"} == {"a", "b", "c"}


def test_a_runner_without_chunk_stages_is_unchanged():
    """Chunking answers a disk limit; a workload that fits must not pay for it."""
    from pod_harness.framework import Context, Runner, Stage, Verification

    class Plain(Stage):
        name, number = "plain", 1

        def execute(self, ctx):
            return {"ran": True}

        def verify_outputs(self, ctx):
            return Verification(ok=True)

    ctx = Context(region="r")
    ctx.chunks = ("a", "b")            # present, but no stage is chunk-scoped
    r = Runner("t", [Plain()]).run(ctx)
    assert r["ok"] and "chunks" not in r and len(r["stages"]) == 1


def test_exit_chunk_runs_even_when_a_stage_fails():
    """A chunk's completed work must not be discarded because a later stage failed."""
    from pod_harness.framework import Context, Runner, Stage, Verification

    left = []

    class Boom(Stage):
        name, number, scope = "boom", 1, "chunk"

        def execute(self, ctx):
            raise RuntimeError("stage exploded")

        def verify_outputs(self, ctx):
            return Verification(ok=True)

    ctx = Context(region="r")
    ctx.chunks = ("a", "b")
    ctx.enter_chunk = lambda k: None
    ctx.exit_chunk = lambda k: left.append(k)
    Runner("t", [Boom()]).run(ctx)
    assert "a" in left, "publish/evict was skipped when a stage raised"


def test_chunk_exit_publishes_then_evicts():
    """Without the evict, chunking bounds nothing — the disk fills on chunk three.

    Also pins the ORDER: a publish failure must leave the local copy alone, because at that
    moment it is the only copy.
    """
    from pod_harness import execute_job as EJ

    d = pathlib.Path(tempfile.mkdtemp())
    corpus = d / "corpus"
    raw = corpus / "raw" / "srcA"
    raw.mkdir(parents=True)
    (raw / "a.wav").write_bytes(b"x" * 1000)
    norm = d / "assets" / "derived" / "normalized" / "srcA"
    norm.mkdir(parents=True)
    (norm / "a.wav").write_bytes(b"y" * 1000)

    os.environ["PODH_CORPUS_ROOT"] = str(corpus)
    spec = {"mount": {"kind": "object", "chunk_by": "source"},
            "params": {"sources": [{"id": "srcA"}, {"id": "srcB"}]}}

    published, fail = [], {"boom": False}

    class M:
        def prepare(self, spec):
            return {"ready": True}

        def publish_tree(self, local, prefix, skip_existing=False):
            if fail["boom"]:
                raise RuntimeError("store unavailable")
            published.append(prefix)
            return {"published": True, "files": 1, "prefix": prefix}

    EJ_for_spec = EJ.__dict__.get("for_spec")
    import pod_harness.mount as MM
    MM.for_spec = lambda spec: M()

    from pod_harness.framework import Context
    ctx = Context(region="r", params=spec["params"])
    EJ.attach_chunks(spec, ctx)

    # a publish failure must NOT evict
    fail["boom"] = True
    ctx.exit_chunk("srcA")
    assert raw.exists() and norm.exists(), "evicted despite a failed publish"

    # a successful publish evicts, and publishes raw + derived for that chunk only
    fail["boom"] = False
    ctx.exit_chunk("srcA")
    assert not raw.exists() and not norm.exists(), "chunk was not evicted"
    assert "corpus/raw/srcA" in published
    assert "assets/derived/normalized/srcA" in published
    assert not any(p.startswith("runs/") for p in published), \
        "the run's out/ was published per chunk — it accumulates and is published once"
