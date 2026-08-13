"""lingua_harness — the in-pod execution engine.

Self-contained by design: this package imports nothing of ours. It knows HOW to run a
job — stage ordering, wiring, verification, resume, status reporting — and nothing about
WHERE anything lives. Every root and prefix is supplied by the loader through the
environment, and the harness refuses to start rather than defaulting.

That refusal is the point. The storage layout used to be defined here as well as in the
loader, and the two copies drifted three times in a single day: logs/<pod_id> against
runs/<job_id>, .cache against cache, and a module path that had moved — the last of which
billed thirteen minutes and served 404. One definition, in the loader; the harness is told.

The loader talks to this package over four contracts and no shared code: the job spec
schema, the event/status schema, the environment variable names, and the /v1 endpoints.
See CONTRACT.md.
"""
__all__ = ["framework", "execute_job", "events", "status", "spec", "resume", "progress",
           "registry", "mount", "storage", "objectstore"]
