"""A fake WORKLOAD for harness tests — the minimum a real one provides.

Deliberately shaped like the real thing: Stage subclasses, a STAGES registry, and a
capabilities.json alongside. The engine resolves it through `pipeline.stages_from`, so the
harness test exercises the real code-loading path rather than a special case for tests.
"""
import time

from pod_harness.framework import Stage, Verification


def _roots():
    """Where the documented layout says things go, resolved the way a workload resolves it.

    corpus/, assets/ and out/ are siblings under the mount root — the same anchoring the
    trainer uses, so this fixture exercises the real filing rule rather than a test-only one.
    """
    import os
    from pathlib import Path
    corpus = Path(os.environ.get("PODH_CORPUS_ROOT", "/workspace/corpus"))
    mount = corpus.parent
    return corpus, mount / "assets" / "derived", mount / "assets" / "profiles"


class Acquire(Stage):
    # chunk-scoped: in a real workload this is where audio arrives, and audio is what does
    # not fit on the disk.
    scope = "chunk"
    name, number = "acquire", 1
    produces = ("sources",)
    def execute(self, ctx):
        corpus, _, _ = _roots()
        sid = (ctx.params.get("sources") or [{}])[0].get("id", "src")
        found = sorted(p.name for p in (corpus / "raw" / sid).glob("*.wav"))
        ctx.artifacts.setdefault("sources", []).extend(found)
        return {"source": sid, "n": len(found)}


class Normalize(Stage):
    scope = "chunk"
    name, number = "normalize", 2
    requires, produces = ("sources",), ("normalized",)
    def execute(self, ctx):
        import json
        corpus, derived, _ = _roots()
        sid = (ctx.params.get("sources") or [{}])[0].get("id", "src")
        out = derived / "normalized" / sid
        out.mkdir(parents=True, exist_ok=True)
        srcs = sorted(p.name for p in (corpus / "raw" / sid).glob("*.wav"))
        for i, s in enumerate(srcs):
            ctx.progress(i + 1, len(srcs), note=s)
            (out / s).write_bytes(b"NORM" + bytes(200))
        # Beside the artifact, so a later run can tell what it was made from and whether
        # reusing it is safe. Without this a cached directory is just bytes.
        (out / "_derivation.json").write_text(json.dumps(
            {"stage": "normalized", "source_id": sid, "params": {"rate": 16000}}))
        ctx.artifacts.setdefault("normalized", []).extend(srcs)
        return {"source": sid, "n": len(srcs)}


class Embed(Stage):
    name, number = "embed", 3
    requires, produces = ("normalized",), ("embeddings",)
    def execute(self, ctx):
        ctx.put("embeddings", {"dim": 192})
        return {"dim": 192}


class Measure(Stage):
    scope = "chunk"
    name, number = "measure", 4
    requires, produces = ("normalized",), ("measurements",)
    def execute(self, ctx):
        import json, os
        from pathlib import Path
        sid = (ctx.params.get("sources") or [{}])[0].get("id", "src")
        out = Path(os.environ.get("PODH_OUT_ROOT", "/workspace/out")) / "runs" / ctx.region
        out.mkdir(parents=True, exist_ok=True)
        (out / f"measurements_{sid}.jsonl").write_text(
            json.dumps({"source": sid, "n": len(ctx.get("normalized") or [])}) + "\n")
        ctx.artifacts.setdefault("measurements", []).append(sid)
        return {"source": sid}


class Profile(Stage):
    """Job-scoped: runs ONCE, over every chunk's measurements, and promotes the result.

    This is the reduce half. It also exercises promotion — an immutable per-run directory
    plus a `current` pointer — which is how a result outlives the run that produced it,
    since runs/ is prunable by age.
    """
    name, number = "profile", 5
    requires = ("measurements",)
    def execute(self, ctx):
        import json, os
        _, _, profiles = _roots()
        run_id = os.environ.get("PODH_JOB_ID", "local")
        dest = profiles / ctx.region / run_id
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "profile.json").write_text(json.dumps(
            {"region": ctx.region, "sources": ctx.get("measurements")}))
        # `current` LAST, so it can never name a directory that is not finished.
        (profiles / ctx.region / "current").write_text(run_id)
        return {"sources": len(ctx.get("measurements") or []), "run_id": run_id}

    def verify_outputs(self, ctx):
        _, _, profiles = _roots()
        import os
        run_id = os.environ.get("PODH_JOB_ID", "local")
        p = profiles / ctx.region / run_id / "profile.json"
        return Verification(ok=p.exists(),
                            checks={"profile": p.exists()},
                            failures=[] if p.exists() else [f"missing {p}"])


class Fail(Stage):
    """Raises. Distinct from Liar: this one admits it."""
    name, number = "_fail", 90
    def execute(self, ctx):
        raise RuntimeError("fixture stage _fail always fails")


class Liar(Stage):
    """Reports success, produces nothing — the bug class verification exists to catch."""
    name, number = "_liar", 91
    produces = ("never_made",)
    def execute(self, ctx):
        return {"claimed": 40}


class Slow(Stage):
    name, number = "_slow", 92
    def execute(self, ctx):
        for i in range(80):
            ctx.progress(i + 1, 80)
            time.sleep(0.1)
        return {}


STAGES = {"acquire": Acquire, "normalize": Normalize, "embed": Embed,
          "measure": Measure, "profile": Profile, "_fail": Fail, "_liar": Liar, "_slow": Slow}
