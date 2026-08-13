"""A fake WORKLOAD for harness tests — the minimum a real one provides.

Deliberately shaped like the real thing: Stage subclasses, a STAGES registry, and a
capabilities.json alongside. The engine resolves it through `pipeline.stages_from`, so the
harness test exercises the real code-loading path rather than a special case for tests.
"""
import time

from pod_harness.framework import Stage, Verification


class Acquire(Stage):
    name, number = "acquire", 1
    produces = ("sources",)
    def execute(self, ctx):
        ctx.put("sources", [f"f{i}.wav" for i in range(4)])
        return {"n": 4}


class Normalize(Stage):
    name, number = "normalize", 2
    requires, produces = ("sources",), ("normalized",)
    def execute(self, ctx):
        srcs = ctx.get("sources")
        for i, s in enumerate(srcs):
            ctx.progress(i + 1, len(srcs), note=s)
            time.sleep(0.05)
        ctx.put("normalized", srcs)
        return {"n": len(srcs)}


class Embed(Stage):
    name, number = "embed", 3
    requires, produces = ("normalized",), ("embeddings",)
    def execute(self, ctx):
        ctx.put("embeddings", {"dim": 192})
        return {"dim": 192}


class Measure(Stage):
    name, number = "measure", 4
    requires, produces = ("normalized",), ("measurements",)
    def execute(self, ctx):
        ctx.put("measurements", {"x": 1})
        return {"ok": True}


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
          "measure": Measure, "_fail": Fail, "_liar": Liar, "_slow": Slow}
