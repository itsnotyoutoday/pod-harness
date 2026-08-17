"""`given`: artifacts a job declares come from outside the pipeline."""
import sys, tempfile, os
sys.path.insert(0, "src")
os.environ.setdefault("PODH_LOG_ROOT", tempfile.mkdtemp())
from pod_harness.execute_job import seed_given, GIVEN, context_from_spec
from pod_harness.framework import Context, Runner, Stage, Verification

ok=[]
def check(n,c): ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")

class Probe(Stage):
    name, number = "probe", 3
    requires, produces = ("checkpoint",), ("adherence",)
    def execute(self, ctx): 
        ctx.put("adherence", {"err": 0.01}); return {}

# 1. Without `given`, a stage requiring something nobody produces is refused.
r = Runner("t", [Probe()])
w = r.wiring(given=[])
check("a requirement nobody produces is refused", not w["ok"])
check("  ...and the message names the artifact",
      any("checkpoint" in p for p in w["problems"]))

# 2. With it, the same pipeline is legal.
ctx = context_from_spec({"pipeline": {"stages": ["probe"], "given": ["checkpoint"]},
                         "params": {"region": "r"}})
check("the name is seeded into the context", ctx.has("checkpoint"))
check("  ...as a placeholder, not a value", ctx.get("checkpoint") == GIVEN)
check("wiring now passes",
      r.wiring(given=sorted(k for k in ctx.artifacts if ctx.has(k)))["ok"])

# 3. Top-level `given` works too, and an empty one changes nothing.
c2 = context_from_spec({"given": ["checkpoint"], "params": {}})
check("top-level `given` is accepted", c2.has("checkpoint"))
c3 = context_from_spec({"params": {}})
check("no `given` seeds nothing", not c3.artifacts)

# 4. It must not overwrite a real artifact.
c4 = Context(region="r")
c4.put("checkpoint", {"path": "/real.pth"})
seed_given({"given": ["checkpoint"]}, c4)
check("a real artifact is never overwritten by a placeholder",
      c4.get("checkpoint") == {"path": "/real.pth"})

# 5. A stage that CANNOT supply what was declared must not run on the placeholder.
class Honest(Probe):
    def check(self, ctx):
        c = ctx.get("checkpoint")
        if isinstance(c, dict) and c.get("_given") and not c.get("path"):
            ctx.artifacts.pop("checkpoint", None)     # withdraw it
        return super().check(ctx)

ctx5 = context_from_spec({"given": ["checkpoint"], "params": {}})
rd = Honest().check(ctx5)
check("withdrawing the placeholder makes the stage NOT ready", not rd.ready)
check("  ...naming the missing input", "checkpoint" in rd.missing)

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
