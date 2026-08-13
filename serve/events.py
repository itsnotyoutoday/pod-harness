"""The status protocol — one event schema, several transports.

## Why this module has no third-party imports

It is imported from two very different places: the FastAPI control app, and the pipeline's
own `Runner.run()` loop deep inside a batch job. The pipeline must not grow a dependency on
fastapi or boto3 just to report progress, so everything here is stdlib. S3 mirroring is
opportunistic and imported lazily.

## The schema

    {"job_id": "...", "seq": 7, "ts": "2026-08-12T20:14:03Z",
     "stage": "align", "index": 3, "total": 7,
     "state": "running|ok|skipped|failed|unverified",
     "detail": {...}}

`seq` is the load-bearing field. It is a monotonic per-job counter, which is what lets a
client resume from where it left off no matter how it is connected — long-poll passes
`since_seq`, SSE passes `Last-Event-ID`, a reader of the S3 copy passes nothing and gets
everything. Same events, four transports, one cursor.

## Why `state` mirrors StageResult rather than inventing its own vocabulary

`runners/framework.py` distinguishes `ok` from `unverified` from `failed`, and that
distinction is the entire point of the Stage contract: a stage that "reported success and
produced nothing" is `failed`, not `ok`. If the wire format collapsed those, the status
endpoint would report green for exactly the bug class the framework exists to catch. So the
vocabulary is copied verbatim.

## Files

    <log_dir>/jobs/<job_id>/events.jsonl   append-only, one JSON object per line
    <log_dir>/jobs/<job_id>/status.json    latest snapshot — the cheap poll target
    <log_dir>/jobs/<job_id>/job.log        the job's own stdout/stderr

events.jsonl is append-only so a crashed writer truncates at most the final line, and a
reader tailing it never sees a partially-rewritten history. status.json is written whole
via atomic replace, so a reader never sees half a document.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# State vocabulary, copied from runners/framework.py StageResult.status plus the two
# job-level states a single stage never has.
JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
STAGE_STATES = ("running", "ok", "skipped", "failed", "unverified")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log_root() -> Path:
    """Where everything lands. On a pod this is under the volume mount, so it survives
    pod death and is readable from S3 without the pod being alive."""
    d = os.environ.get("LINGUA_LOG_DIR")
    if d:
        return Path(d)
    root = Path(os.environ.get("LINGUA_LOG_ROOT", "/workspace/logs"))
    return root / os.environ.get("RUNPOD_POD_ID", "local")


def job_dir(job_id: str) -> Path:
    return log_root() / "jobs" / job_id


class EventLog:
    """Append-only event log for one job, with a snapshot alongside it.

    Safe to construct repeatedly for the same job — it reads the existing sequence back
    rather than restarting the counter, so an API process and the job subprocess can both
    hold one without colliding on `seq`.
    """

    def __init__(self, job_id: str, *, spec: dict | None = None):
        self.job_id = job_id
        self.dir = job_dir(job_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.status_path = self.dir / "status.json"
        self.log_path = self.dir / "job.log"
        self.spec_path = self.dir / "spec.json"
        self._mirror_error: str | None = None
        if spec is not None and not self.spec_path.exists():
            self._atomic_write(self.spec_path, json.dumps(spec, indent=2))

    # -- writing ------------------------------------------------------------------------

    def emit(self, *, stage: str = "", index: int = 0, total: int = 0,
             state: str = "running", **detail) -> dict:
        """Append one event and refresh the snapshot. Returns the event."""
        seq = self._next_seq()
        ev = {"job_id": self.job_id, "seq": seq, "ts": _now(),
              "stage": stage, "index": index, "total": total,
              "state": state, "detail": detail}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())     # a crash right here must not lose the last event
        self._refresh_status(ev)
        self._mirror()
        return ev

    def _next_seq(self) -> int:
        if not self.events_path.exists():
            return 1
        last = 0
        with self.events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line).get("seq", last)
                except json.JSONDecodeError:
                    continue          # torn final line from a crashed writer — skip it
        return last + 1

    def _refresh_status(self, ev: dict) -> None:
        cur = self.status()
        cur.update({
            "job_id": self.job_id,
            "seq": ev["seq"],
            "updated_at": ev["ts"],
            "stage": ev["stage"],
            "index": ev["index"],
            "total": ev["total"],
        })
        cur.setdefault("started_at", ev["ts"])

        # Job-level state is derived, never guessed: a terminal stage state promotes the
        # job, everything else leaves it running.
        job_state = ev["detail"].get("job_state")
        if job_state:
            cur["job_state"] = job_state
        elif ev["state"] == "failed":
            cur["job_state"] = "failed"
        else:
            cur.setdefault("job_state", "running")

        stages = cur.setdefault("stages", {})
        if ev["stage"]:
            stages[ev["stage"]] = {"state": ev["state"], "index": ev["index"],
                                   "seconds": ev["detail"].get("seconds"),
                                   "error": ev["detail"].get("error")}
        if cur["job_state"] in ("done", "failed", "cancelled"):
            cur["finished_at"] = ev["ts"]
        self._atomic_write(self.status_path, json.dumps(cur, indent=2, default=str))

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Write-then-rename, so a reader never observes a half-written document."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _mirror(self) -> None:
        """Best-effort copy of the snapshot to S3.

        This is the transport that works EVERYWHERE — pods, serverless workers with no
        inbound port, and after the pod is gone. It is deliberately non-fatal: a job must
        never die because object storage hiccuped, and the local file remains the source
        of truth for anything that can reach the pod.
        """
        if os.environ.get("LINGUA_STATUS_S3", "1") != "1":
            return
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from control.objectstore import get_storage      # type: ignore
            st = get_storage()
            if not st.available:            # a PROPERTY on Storage, not a method
                return
            cfg = st.require()
            st.client.put_object(Bucket=cfg.bucket,
                                 Key=f"status/{self.job_id}/status.json",
                                 Body=self.status_path.read_bytes())
            self._mirror_error = None
        except Exception as exc:
            # Non-fatal, but NOT invisible. Swallowing this silently is how a mirror stays
            # broken for months: the local file keeps working, so nothing looks wrong until
            # the day the pod is gone and S3 is the only source left. Record the reason and
            # surface it in summary() so it is discoverable without reading code.
            self._mirror_error = f"{type(exc).__name__}: {exc}"

    # -- reading ------------------------------------------------------------------------

    def status(self) -> dict:
        if not self.status_path.exists():
            return {"job_id": self.job_id, "job_state": "queued", "seq": 0, "stages": {}}
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"job_id": self.job_id, "job_state": "unknown", "seq": 0, "stages": {}}

    def since(self, seq: int = 0, limit: int = 500) -> list[dict]:
        """Events after `seq`. Bounded by default — an unbounded event dump is the same
        context-killing mistake as an unbounded log."""
        out: list[dict] = []
        if not self.events_path.exists():
            return out
        with self.events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("seq", 0) > seq:
                    out.append(ev)
                    if len(out) >= limit:
                        break
        return out

    def wait(self, seq: int = 0, timeout: float = 30.0,
             interval: float = 0.25) -> list[dict]:
        """Long-poll: block until there is something after `seq`, or timeout.

        This is the agent-native transport. It gives push-like latency with no connection
        state, and it behaves identically on a pod, on a serverless worker, and against a
        replayed S3 copy — which a WebSocket cannot, because a serverless worker has no
        inbound port to accept one on.

        File polling rather than a condition variable because the writer is usually a
        different PROCESS (the job subprocess), so in-process signalling would not see it.

        Returns immediately once the job reaches a terminal state and has nothing new. A
        poll that blocks the full timeout against a job that finished ten minutes ago is
        pure waste — and for an agent it is worse than waste, because it turns "is it done
        yet" into a minute of dead time per call. The terminal check happens INSIDE the
        loop, not just before it, so a job that finishes mid-wait also returns promptly.
        """
        deadline = time.monotonic() + timeout
        while True:
            evs = self.since(seq)
            if evs:
                return evs
            if self.status().get("job_state") in ("done", "failed", "cancelled"):
                return []
            if time.monotonic() >= deadline:
                return []
            time.sleep(interval)

    def summary(self, *, tail_lines: int = 20) -> dict:
        """The ~500-token digest. The endpoint an agent should reach for first.

        Deliberately NOT the log: stage table, failures, and a short tail. Ninety percent
        of "what happened to my job" is answerable from this without reading a byte of
        console output.
        """
        st = self.status()
        stages = st.get("stages", {})
        failed = [{"stage": k, "error": v.get("error")}
                  for k, v in stages.items()
                  if v.get("state") in ("failed", "unverified")]
        tail: list[str] = []
        if self.log_path.exists():
            try:
                tail = self.log_path.read_text(encoding="utf-8",
                                               errors="replace").splitlines()[-tail_lines:]
            except OSError:
                pass
        return {
            "job_id": self.job_id,
            "job_state": st.get("job_state"),
            "started_at": st.get("started_at"),
            "finished_at": st.get("finished_at"),
            "progress": f"{st.get('index', 0)}/{st.get('total', 0)}",
            "current_stage": st.get("stage"),
            "stages": [{"stage": k, "state": v.get("state"), "seconds": v.get("seconds")}
                       for k, v in stages.items()],
            "failures": failed,
            "log_tail": tail,
            "log_bytes": self.log_path.stat().st_size if self.log_path.exists() else 0,
            "mirror_error": self._mirror_error,
        }


def read_tail(path: Path, tail: int = 8192) -> dict:
    """Bounded read of a file's end.

    Every log-ish response in this system goes through here, because the invariant is that
    no endpoint may return an unbounded body. `total_bytes` is always reported so the
    caller knows what it did NOT see — a truncated response that does not admit it is
    truncated is worse than no response.
    """
    if not path.exists():
        return {"text": "", "total_bytes": 0, "returned_bytes": 0, "from_byte": 0}
    size = path.stat().st_size
    tail = max(0, min(tail, size))
    with path.open("rb") as f:
        f.seek(size - tail)
        data = f.read(tail)
    return {"text": data.decode("utf-8", errors="replace"),
            "total_bytes": size, "returned_bytes": len(data),
            "from_byte": size - tail}
