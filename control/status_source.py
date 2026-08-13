"""Where a job's status is READ from — the second axis of vendor independence.

## Why this is a separate interface from Provider

`runners/provider.py` abstracts where work RUNS: local docker, a RunPod pod, and one day
Vast or Lambda or a bare server. That is the write side of the boundary.

It does not abstract where a client READS progress, and those are genuinely different
questions with different answers at the same moment. One job's status can be legitimately
served three ways during its life:

    while the pod is up          HTTP to the pod's own /v1 — sub-second, live
    if it is a serverless worker HTTP only when the endpoint exposes a port; often not
    after the compute is gone    S3 — the only thing left, and the only universal one

If the control plane hardcoded "GET the pod", then finishing a job would make its history
unreadable, and moving to serverless would break every client. So reading is its own
interface with its own adapters, and the control plane composes the two: a Provider says
where it ran, a StatusSource says how to look at it.

## The contract

Every source answers the same four questions and returns the same shapes that
`serve/events.py` writes. That is what lets `GET /v1/jobs/{id}/summary` mean exactly one
thing to a caller regardless of which adapter answered it — the property that makes a
webapp or an agent survive the compute moving underneath it.

## Adding a source

Subclass, implement four methods, register it. Nothing above changes. The same is true of
adding a Provider — see providers.py — and between them that is the whole of what "we could
leave RunPod one day" has to mean in practice.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class StatusSource(ABC):
    """Read-side adapter. Implementations must not raise on a missing job: return the
    'unknown' shape instead, because a control plane that 500s when asked about a job it
    has not heard of is useless during exactly the incident you need it for."""

    kind: str = "base"

    @abstractmethod
    def status(self, job_id: str) -> dict:
        """Latest snapshot. Same schema as serve/events.py writes."""

    @abstractmethod
    def events(self, job_id: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
        """Events after `since_seq`, bounded."""

    @abstractmethod
    def summary(self, job_id: str) -> dict:
        """The ~500-token digest."""

    @abstractmethod
    def log(self, job_id: str, tail: int = 8192) -> dict:
        """Bounded log read. Must report total_bytes."""

    def available(self) -> bool:
        """Can this source answer right now? Lets the control plane fall back in order
        (live pod → S3) without exception handling as control flow."""
        return True

    @staticmethod
    def unknown(job_id: str) -> dict:
        return {"job_id": job_id, "job_state": "unknown", "seq": 0, "stages": {},
                "hint": "no source could answer for this job — it may never have started"}


class LocalStatusSource(StatusSource):
    """Reads the files directly. Used when the control plane and the job share a
    filesystem: a laptop run, a docker-compose run, or the pod reading its own state."""

    kind = "local"

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _dir(self, job_id: str) -> Path:
        return self.root / "jobs" / job_id

    def available(self) -> bool:
        return self.root.exists()

    def status(self, job_id: str) -> dict:
        p = self._dir(job_id) / "status.json"
        if not p.exists():
            return self.unknown(job_id)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return self.unknown(job_id)

    def events(self, job_id: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
        p = self._dir(job_id) / "events.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("seq", 0) > since_seq:
                out.append(ev)
                if len(out) >= limit:
                    break
        return out

    def summary(self, job_id: str) -> dict:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from serve.events import EventLog          # type: ignore
        import os
        os.environ.setdefault("LINGUA_LOG_DIR", str(self.root))
        return EventLog(job_id).summary()

    def log(self, job_id: str, tail: int = 8192) -> dict:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from serve.events import read_tail         # type: ignore
        return read_tail(self._dir(job_id) / "job.log", tail)


class HttpStatusSource(StatusSource):
    """Talks to a live pod's own /v1 surface.

    The fast path, and the only one with sub-second latency, because the pod is writing
    the events as it goes. Unavailable the moment the pod stops — which is precisely why
    it is never the only source configured.
    """

    kind = "http"

    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> Any:
        import urllib.parse
        import urllib.request
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-Lingua-Token": self.token})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def available(self) -> bool:
        try:
            self._get("/v1/health")
            return True
        except Exception:
            return False

    def status(self, job_id: str) -> dict:
        try:
            return self._get(f"/v1/jobs/{job_id}")
        except Exception:
            return self.unknown(job_id)

    def events(self, job_id: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
        try:
            return self._get(f"/v1/jobs/{job_id}/events",
                             {"since_seq": since_seq, "limit": limit}).get("events", [])
        except Exception:
            return []

    def summary(self, job_id: str) -> dict:
        try:
            return self._get(f"/v1/jobs/{job_id}/summary")
        except Exception:
            return self.unknown(job_id)

    def log(self, job_id: str, tail: int = 8192) -> dict:
        try:
            return self._get(f"/v1/jobs/{job_id}/log", {"tail": tail})
        except Exception:
            return {"text": "", "total_bytes": 0, "returned_bytes": 0, "from_byte": 0}


class S3StatusSource(StatusSource):
    """Reads the mirror that `serve/events.py._mirror()` pushes.

    The universal fallback. It works for a pod, for a serverless worker with no inbound
    port, and — critically — for compute that no longer exists. Slower and eventually
    consistent, which is the right trade for the one source that always answers.

    Deliberately built on the pipeline's own Storage abstraction rather than boto3
    directly, so swapping RunPod's S3 for R2 or GCS is a Storage concern and never a
    control-plane one.
    """

    kind = "s3"

    def __init__(self, storage=None, prefix: str = "status", profile: str | None = None):
        self._storage = storage
        self.prefix = prefix
        self.profile = profile

    def _st(self):
        """Resolved through objectstore, never by constructing Storage() directly.

        That indirection is the difference between "S3" meaning RunPod's volume endpoint
        forever and it meaning whichever store the deployment names — RunPod today,
        Cloudflare R2 tomorrow, something else later — with no code change here.
        """
        if self._storage is None:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from .objectstore import get_storage           # type: ignore
            self._storage = get_storage(self.profile)
        return self._storage

    def available(self) -> bool:
        try:
            # `available` is a PROPERTY on Storage, not a method. Calling it returns a
            # bool that is always truthy, which silently made every source look usable.
            return bool(self._st().available)
        except Exception:
            return False

    def _get_json(self, key: str) -> dict | None:
        # Storage exposes check/upload_dir/download_prefix and a boto3 `client`; there is
        # no get_bytes/put_bytes. Go through the client exactly as batch_pod.py does.
        try:
            st = self._st()
            cfg = st.require()
            body = st.client.get_object(Bucket=cfg.bucket,
                                        Key=f"{self.prefix}/{key}")["Body"].read()
            return json.loads(body.decode())
        except Exception:
            return None

    def status(self, job_id: str) -> dict:
        return self._get_json(f"{job_id}/status.json") or self.unknown(job_id)

    def events(self, job_id: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
        # Only the snapshot is mirrored on the hot path; the event stream is derived from
        # it so S3 stays cheap. A full event mirror is a later optimisation if it is ever
        # needed — the interface does not change when it lands.
        st = self.status(job_id)
        if st.get("seq", 0) > since_seq:
            return [{"job_id": job_id, "seq": st.get("seq", 0),
                     "ts": st.get("updated_at", ""), "stage": st.get("stage", ""),
                     "index": st.get("index", 0), "total": st.get("total", 0),
                     "state": st.get("job_state", "unknown"),
                     "detail": {"source": "s3-snapshot"}}]
        return []

    def summary(self, job_id: str) -> dict:
        st = self.status(job_id)
        stages = st.get("stages", {})
        return {"job_id": job_id, "job_state": st.get("job_state"),
                "progress": f"{st.get('index', 0)}/{st.get('total', 0)}",
                "current_stage": st.get("stage"),
                "stages": [{"stage": k, "state": v.get("state")}
                           for k, v in stages.items()],
                "failures": [{"stage": k, "error": v.get("error")}
                             for k, v in stages.items()
                             if v.get("state") in ("failed", "unverified")],
                "source": "s3"}

    def log(self, job_id: str, tail: int = 8192) -> dict:
        return {"text": "", "total_bytes": 0, "returned_bytes": 0, "from_byte": 0,
                "hint": "logs are not mirrored to S3 on the hot path; reach the pod "
                        "directly while it lives, or enable log mirroring"}


class ChainedStatusSource(StatusSource):
    """Try each source in order; first one that answers wins.

    This is what makes a job's identity outlive its compute. Configure
    [HttpStatusSource(pod), S3StatusSource()] and the same job id resolves to the live pod
    while it exists and to the S3 mirror forever after — with no branch anywhere in the
    caller, and no change when the pod becomes a serverless worker or a different vendor.
    """

    kind = "chained"

    def __init__(self, *sources: StatusSource):
        self.sources = list(sources)

    def _first(self, fn, job_id: str, *a, **kw):
        for s in self.sources:
            if not s.available():
                continue
            r = getattr(s, fn)(job_id, *a, **kw)
            if r and (not isinstance(r, dict) or r.get("job_state") != "unknown"):
                if isinstance(r, dict):
                    r.setdefault("source", s.kind)
                return r
        return [] if fn == "events" else self.unknown(job_id)

    def status(self, job_id): return self._first("status", job_id)
    def summary(self, job_id): return self._first("summary", job_id)
    def events(self, job_id, since_seq=0, limit=200):
        return self._first("events", job_id, since_seq, limit)
    def log(self, job_id, tail=8192): return self._first("log", job_id, tail)


SOURCES: dict[str, type[StatusSource]] = {
    "local": LocalStatusSource,
    "http": HttpStatusSource,
    "s3": S3StatusSource,
}
