"""A chunked run is legible per chunk, not just per stage.

## The bug this closes

`status["stages"]` is keyed by stage name. In a chunked run every chunk writes the same
keys, so it is a LAST-WRITER view: seven stages over three chunks records seven rows and
discards fourteen. The rows most likely to be lost are the interesting ones — a stage that
failed on chunk 1 and was never reached again is overwritten by nothing, or worse, the run
aborts and `stages` shows the failure while giving no way to say WHICH source it was.

The consequence is already recorded in `framework._run_chunked`'s own comment: one source
failed to normalise, and comply/intersect/profile then ran green over a third of the data.
Knowing which third is not a nicety.

Events carried no chunk field at all, so no client — a console, an agent, a person reading
JSONL — could reconstruct it. This asserts that they now can.

No network, no store, no pod.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, "src")

# The log root is supplied by the loader and computed by nothing, so a test supplies it too.
os.environ["PODH_LOG_DIR"] = tempfile.mkdtemp()
os.environ["PODH_STATUS_S3"] = "0"        # no mirroring; this is about the local protocol

from pod_harness.events import EventLog          # noqa: E402
from pod_harness.status import EventReporter     # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(f"  {'✅' if cond else '❌'} {name}")


class FakeStage:
    def __init__(self, name):
        self.name, self.requires, self.produces = name, (), (name + "_out",)


class FakeResult:
    def __init__(self, status, seconds=1.0, verification=None, error=None):
        self.status, self.seconds = status, seconds
        self.verification = verification or {}
        self.error = error


VERIFIED = {"ok": True, "checks": {"x": True}, "failures": []}

# A 3-stage x 3-chunk run, with `align` failing on the second chunk only. Literal, because
# a builder that constructs the history hides what is being asserted about.
CHUNKS = ("heroico", "ciempiess", "colombian")
STAGES = ("acquire", "normalize", "align")

reporter = EventReporter("job-1")
reporter.job_start(len(STAGES) * len(CHUNKS))
for chunk in CHUNKS:
    reporter.chunk_start(chunk)
    for stage in STAGES:
        reporter.stage_start(FakeStage(stage))
        failed = (chunk == "ciempiess" and stage == "align")
        reporter.stage_done(
            FakeStage(stage),
            FakeResult("failed" if failed else "ok",
                       verification={} if failed else VERIFIED,
                       error="declared output 'align_out' is missing" if failed else None))
    reporter.chunk_done(chunk)

log = EventLog("job-1")
status = log.status()
events = log.since(0, limit=500)

# 1. the grid exists at all
grid = status.get("chunks", {})
check("the snapshot carries a per-chunk view", set(grid) == set(CHUNKS))
check("every chunk records every stage it reached",
      all(set(grid[c]) == set(STAGES) for c in CHUNKS))

# 2. the failure is attributable to ONE chunk — the whole point
check("the failing chunk is identifiable",
      grid["ciempiess"]["align"]["state"] == "failed")
check("the chunks that succeeded still read ok",
      grid["heroico"]["align"]["state"] == "ok"
      and grid["colombian"]["align"]["state"] == "ok")

# 3. `stages` keeps its last-writer meaning rather than being quietly redefined
check("stages/ still answers 'where is this job now'",
      status["stages"]["align"]["state"] == "ok")   # colombian wrote last

# 4. events carry the chunk, so a client reconstructing history needs no snapshot
stage_events = [e for e in events if e["stage"] == "align" and e["state"] != "running"]
check("every stage event names its chunk",
      [e["chunk"] for e in stage_events] == list(CHUNKS))

# 5. chunk boundaries are events in their own right. Without them a client watching a
#    chunked run sees a multi-gigabyte fetch as unexplained silence.
boundaries = [(e["detail"].get("event"), e["chunk"]) for e in events
              if e["detail"].get("event") in ("chunk_start", "chunk_done")]
check("chunk boundaries are emitted",
      boundaries == [(k, c) for c in CHUNKS for k in ("chunk_start", "chunk_done")])

# 6. the summary surfaces the failure WITH its chunk, or an agent reading only the digest
#    still cannot say which source broke
summary = log.summary()
failures = summary["failures"]
check("summary names the failing chunk",
      len(failures) == 1 and failures[0]["chunk"] == "ciempiess"
      and failures[0]["stage"] == "align")
check("summary carries the whole grid", set(summary["chunks"]) == set(CHUNKS))

# 7. an UNCHUNKED run is unaffected — a client that does not care about chunks never has
#    to learn about them
plain = EventReporter("job-2")
plain.job_start(1)
plain.stage_start(FakeStage("profile"))
plain.stage_done(FakeStage("profile"), FakeResult("ok", verification=VERIFIED))
plain_status = EventLog("job-2").status()
check("an unchunked run records no chunks", not plain_status.get("chunks"))
check("an unchunked run still records its stages",
      plain_status["stages"]["profile"]["state"] == "ok")

# 8. `unverified` is never collapsed into ok — the contract the whole status protocol
#    exists to preserve, asserted here because the merge logic above is new code
u = EventReporter("job-3")
u.job_start(1)
u.chunk_start("heroico")
u.stage_start(FakeStage("measure"))
u.stage_done(FakeStage("measure"), FakeResult("unverified"))
u.chunk_done("heroico")
ustatus = EventLog("job-3").status()
check("unverified survives into the per-chunk view",
      ustatus["chunks"]["heroico"]["measure"]["state"] == "unverified")

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
