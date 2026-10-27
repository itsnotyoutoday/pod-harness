"""Tell pod-control this pod is alive, and tell it the moment it is not.

## Why the pod reports at all, when polling already covers it

It does not have to. pod-control polls the provider every minute and would notice a
finished pod anyway. The heartbeat buys two things:

    /done   terminates within a second instead of within a poll interval. On a job that
            finishes in four minutes, a minute of idle billing is a quarter of the run.
    /alive  proves the job is still working, which lets a deadline be extended safely for
            a long job rather than set generously up front for every job.

Both are optimisations. **Neither is load-bearing**, and that is deliberate: a pod that
cannot reach pod-control still gets reaped on its deadline, because the poll is the
authority. Losing these requests costs money, never correctness.

## Why failures here are swallowed

A job must never die because a watchdog was unreachable. Every call is best-effort with a
short timeout, and the thread is a daemon so it cannot hold the process open. The one
thing that is NOT swallowed is a misconfiguration at startup — that is reported once,
loudly, because a heartbeat silently doing nothing looks exactly like one that works.
"""
from __future__ import annotations

import json
import os
import threading
import time
import ssl
import urllib.error
import urllib.request

#: RunPod's proxy sits behind Cloudflare, which rejects urllib's default agent with
#: `error code: 1010` — a 403 that reads like an auth failure and is not.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

#: Ceiling on the completion report, matching pod-control's own cap. Kept the same on both
#: ends deliberately: a client that sends more than the server stores would have its report
#: silently discarded, and the failure would look like a chain that refuses for no reason.
_MAX_REPORT_BYTES = 64 * 1024

#: Caps on the free-text fields, matching the control plane's column sizes. Truncated HERE
#: rather than left to the server: a server that rejects an over-long reason would drop the
#: whole /done with it, and /done is the request that stops the billing.
MAX_REASON = 500
MAX_STAGE = 64
MAX_LINE = 300

#: Touched when the work is over. Every heartbeat loop — this module's and the sidecar's —
#: checks it and goes quiet. See `start` for why a finished pod must stop reporting.
#: Overridable only so tests on a workstation do not silence a real pod's flag in /tmp.
STOP_FILE = os.environ.get("PODH_HEARTBEAT_STOP_FILE") or "/tmp/podh-stop-heartbeat"


def _cfg() -> tuple[str, str, str]:
    return (os.environ.get("PODH_CONTROL_URL", "").rstrip("/"),
            os.environ.get("PODH_CONTROL_TOKEN", ""),
            os.environ.get("RUNPOD_POD_ID", ""))


def _ssl_context():
    """Verify pod-control by PINNED CERTIFICATE, not by certificate authority.

    The control plane has no DNS name, so no public CA can vouch for it. That is fine here
    and arguably better: both ends are ours, so the pod checks for one exact certificate
    rather than trusting the entire CA ecosystem to have made no mistakes. A token that can
    terminate machines never crosses a connection the pod has not verified.

    `PODH_CONTROL_FINGERPRINT` is the SHA-256 of the DER certificate, colon-separated, as
    `openssl x509 -fingerprint -sha256` prints it.

    With no fingerprint set, verification is left at the default. That means a plain
    self-signed endpoint simply fails to connect — heartbeats stop, the poll still reaps,
    and nothing silently downgrades to an unverified connection.
    """
    import hashlib
    import ssl

    want = os.environ.get("PODH_CONTROL_FINGERPRINT", "").replace(":", "").lower()
    if not want:
        return None

    ctx = ssl.create_default_context()
    # Hostname and CA checks are turned off because the pin REPLACES them, and it is a
    # stronger check: it admits exactly one certificate rather than any certificate any CA
    # was willing to sign. Turning them off without the pin would be the usual disaster.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx._podh_expected = want
    return ctx


def _verify_pin(sock, expected: str) -> None:
    import hashlib
    der = sock.getpeercert(binary_form=True)
    got = hashlib.sha256(der).hexdigest()
    if got != expected:
        raise ssl.SSLError(
            f"pod-control certificate does not match the pin.\n"
            f"  expected {expected[:24]}…\n  got      {got[:24]}…\n"
            f"  Refusing to send the control token to an endpoint that is not the one "
            f"this pod was told to trust.")


#: Why the last request failed, for callers that need to report rather than retry.
_LAST_ERROR = ""
#: HTTP status of the last failed request (0 when it never got an answer). Kept apart from
#: the message because "the server said 422" is a decision input — see `_post_tolerant` —
#: while the message is only for humans.
_LAST_STATUS = 0


def last_error() -> str:
    """The most recent transport failure, or empty if there has not been one."""
    return _LAST_ERROR


def _post(path: str, body: dict, timeout: float = 10) -> dict | None:
    global _LAST_ERROR, _LAST_STATUS
    url, token, _ = _cfg()
    if not url:
        return None
    _LAST_ERROR, _LAST_STATUS = "", 0
    req = urllib.request.Request(
        f"{url}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": _UA,
                 "X-Podh-Token": token}, method="POST")
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            if ctx is not None and hasattr(r, "fp"):
                sock = getattr(getattr(r.fp, "raw", None), "_sock", None)
                if sock is not None:
                    _verify_pin(sock, ctx._podh_expected)
            return json.loads(r.read())
    except Exception as exc:
        # Never fatal — the sweep still holds the deadline — but never SILENT either.
        # This returned a bare None, so a caller could not tell "not configured" from
        # "the request failed", and podh-init reported a termination failure as
        # "no control url or pod id" while the url was plainly set. The one call
        # responsible for stopping the billing was the one whose reason was discarded.
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        _LAST_STATUS = exc.code if isinstance(exc, urllib.error.HTTPError) else 0
        return None


def _post_tolerant(path: str, body: dict, newer: tuple, timeout: float = 10) -> dict | None:
    """POST `body`; if the server refuses it as malformed, resend without the `newer` keys.

    The fields in `newer` were added after the control plane first shipped. One that
    validates strictly answers 400/422 to a key it does not know, and that must not cost
    the request itself — least of all /done, whose job is to stop the billing. So the
    report degrades to what an older server understands, and says so once in the log
    rather than failing quietly.
    """
    r = _post(path, body, timeout=timeout)
    present = [k for k in newer if k in body]
    if r is None and present and _LAST_STATUS in (400, 422):
        print(f"  [heartbeat] control plane refused {path} with {_LAST_STATUS} — resending "
              f"without {', '.join(present)} (an older server that does not know them)",
              flush=True)
        r = _post(path, {k: v for k, v in body.items() if k not in newer}, timeout=timeout)
    return r


def _clip(text, limit: int) -> str:
    """One line, at most `limit` characters. A reason is a summary; the log has the rest."""
    t = " ".join(str(text).split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _identity() -> dict:
    # job_id accompanies every call: the scoped token is bound to it, and pod-control
    # checks that this job actually owns this pod.
    return {"pod_id": _cfg()[2], "provider": "runpod",
            "job_id": os.environ.get("PODH_CONTROL_JOB_ID")
                      or os.environ.get("PODH_JOB_ID", "")}


def alive(deadline_ts: float | None = None) -> dict | None:
    url, _, pod_id = _cfg()
    if not url or not pod_id:
        return None
    body = _identity()
    if deadline_ts:
        body["deadline_ts"] = deadline_ts
    return _post("/v1/alive", body)


def done(rc: int = 0, reason: str | None = None, stage: str | None = None) -> dict | None:
    """Ask to be terminated now. Called when the job finishes, however it finishes.

    `reason` and `stage` say WHY a pod ended, in words a person can act on: "nvidia-smi
    lists 0 GPUs" at stage "gpu_check" is a bad machine to rent again elsewhere; "job spec
    not found" at stage "spec" is a launcher bug. An exit code alone cannot tell those
    apart, and before this the only way to learn the difference was to read a console
    log that dies with the pod. Both are optional and clipped to the server's limits.

    The exit code travels with the request because the control plane otherwise has no way
    to record HOW the job ended. It used to terminate the pod and leave the job row saying
    "running", so the next sweep saw a job whose pod had vanished and marked it failed —
    a successful run recorded as a failure by the very mechanism that reaped it correctly.

    The per-stage summary travels with it for a different reason: `rc` says how the job
    ENDED, not what it PRODUCED, and a chained job downstream of this one needs the second
    question answered before it is allowed to start. A job can exit 0 having produced a
    fraction of what it claimed — that is not hypothetical, it is how 600 unaligned
    utterances reached a training run unnoticed.

    Reported here rather than read back out of the bucket by the control plane. This pod
    wrote status.json, so fetching it from S3 would be the same bytes by a longer path —
    and it would mean the control plane needed object-store credentials to do something it
    is already being told directly. It is not a trust boundary either way: the artifact
    and the report have the same author.
    """
    url, _, pod_id = _cfg()
    if not url or not pod_id:
        return None
    body = {**_identity(), "rc": int(rc)}
    if reason:
        body["reason"] = _clip(reason, MAX_REASON)
    if stage:
        body["stage"] = _clip(stage, MAX_STAGE)
    s = _status_summary()
    if s:
        body["status"] = s
    h = _handoff()
    if h:
        body["handoff"] = h
    return _post_tolerant("/v1/done", body, ("reason", "stage"))


def boot(line: str, stage: str | None = None) -> dict | None:
    """Tell the control plane what the pod is doing while it boots. Never raises.

    The minutes between "pod created" and "job running" — mounting, fetching code, checking
    the GPU — are otherwise invisible from outside: the job's own events do not exist yet,
    and a pod stuck there looks exactly like one making progress. One short line per step
    makes "stuck in mount for 9 minutes" a fact the control plane can show and act on.

    `stage` names the step; "ready" means the workload is up. Best-effort and quick: a short
    timeout, because a boot script calls this between steps and must not stall waiting on
    a control plane that is down. Older control planes without /v1/boot answer 404, which
    is swallowed like any other failure.
    """
    try:
        url, _, pod_id = _cfg()
        if not url or not pod_id or not str(line).strip():
            return None
        body = {**_identity(), "line": _clip(line, MAX_LINE)}
        if stage:
            body["stage"] = _clip(stage, MAX_STAGE)
        return _post_tolerant("/v1/boot", body, ("stage",), timeout=4)
    except Exception as exc:          # a progress line is never worth a crashed boot
        global _LAST_ERROR
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def _handoff() -> dict | None:
    """Facts this job chose to hand to whatever runs next. See events.write_handoff."""
    try:
        from . import events as _events
        job = (os.environ.get("PODH_JOB_ID")
               or os.environ.get("PODH_CONTROL_JOB_ID") or "")
        return _events.read_handoff(job) or None if job else None
    except Exception:
        return None


def _status_summary() -> dict | None:
    """`stages` and `chunks` from this job's own status.json, and nothing else.

    Deliberately not the whole document: the interesting parts are small and bounded, while
    the full snapshot grows with the run. A completion report that can get large is one that
    starts failing on the runs that matter most.
    """
    try:
        from . import events as _events
        job = (os.environ.get("PODH_JOB_ID")
               or os.environ.get("PODH_CONTROL_JOB_ID") or "")
        if not job:
            return None
        p = _events.job_dir(job) / "status.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        out = {"job_state": d.get("job_state"),
               "stages": {k: {"state": (v or {}).get("state")}
                          for k, v in (d.get("stages") or {}).items()},
               "chunks": {c: {k: {"state": (v or {}).get("state")}
                              for k, v in (st or {}).items()}
                          for c, st in (d.get("chunks") or {}).items()}}

        # Bounded, because this rides on the request that stops the billing. `stages` is
        # fixed by the pipeline, but `chunks` grows with the input — so a run large enough
        # to matter is exactly the one that would push this over a limit and lose the
        # termination request with it.
        #
        # When it does not fit, drop the chunks that are FINE and keep the ones that are
        # not. A truncation that discarded the failures would turn a partial run into a
        # clean-looking one, which is the precise error this whole channel exists to stop.
        if len(json.dumps(out)) > _MAX_REPORT_BYTES:
            bad = {c: st for c, st in out["chunks"].items()
                   if any((v or {}).get("state") not in ("ok", "skipped")
                          for v in (st or {}).values())}
            out["chunk_total"] = len(out["chunks"])
            out["chunk_bad"] = len(bad)
            out["chunks"] = dict(list(bad.items())[:200])
            out["truncated"] = True
        return out
    except Exception:
        # Never break the one request whose job is to get this pod terminated.
        return None


def newest_activity(paths) -> float:
    """The latest mtime among `paths` that exist, or 0.0 when none do."""
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, os.stat(p).st_mtime)
        except OSError:
            continue
    return newest


def activity_live(paths, stale_after_sec: float):
    """A liveness predicate: True while any of `paths` changed within `stale_after_sec`.

    ## Why liveness is judged by output and not by the process

    "The process exists" is true of a hung job. A daemon thread inside the job is worse:
    it keeps beating while the main thread is wedged in a CUDA call or a dead socket, so
    the pod reports healthy for exactly as long as it is useless. The control plane's
    primary reaper is "was heartbeating, went quiet", and anything that beats regardless
    of progress switches that reaper off.

    A working job writes: console output, events, status. A hung one stops. So the beat is
    gated on the newest mtime of those files, and a job silent for longer than the window
    is reported the only way a heartbeat can report it — by going quiet. A stage that is
    legitimately silent for longer should call ctx.progress(), which writes events; the
    window is configurable for the job that cannot.

    `stale_after_sec <= 0` disables the gate (always live). With no file present at all
    the answer is False: no evidence of work is not evidence of work.
    """
    paths = [str(p) for p in paths]

    def live() -> bool:
        if stale_after_sec <= 0:
            return True
        newest = newest_activity(paths)
        return newest > 0 and (time.time() - newest) <= stale_after_sec
    return live


def start(interval_sec: float = 60.0, live=None) -> threading.Thread | None:
    """Begin heartbeating in the background. Returns None when not configured.

    `live`, when given, is asked before every beat; a beat is sent only when it returns
    True. Skipped beats print nothing — the console is itself an activity file, and a
    heartbeat that logged "skipping" would refresh the very signal it is judging.
    """
    url, token, pod_id = _cfg()
    if not url:
        return None
    if not pod_id:
        print("  [heartbeat] PODH_CONTROL_URL is set but RUNPOD_POD_ID is not — "
              "nothing to report about; skipping", flush=True)
        return None
    if not token:
        print("  [heartbeat] PODH_CONTROL_URL is set but PODH_CONTROL_TOKEN is not — "
              "every heartbeat will be rejected", flush=True)

    def loop():
        while True:
            # A finished pod must go QUIET, not keep insisting it is healthy.
            #
            # The watchdog's primary signal is "was reporting, stopped reporting". A pod
            # that heartbeats forever is by definition never stuck, so a finished-but-alive
            # pod was invisible to the one mechanism meant to bound it — leaving only the
            # 8-hour budget, which is a ceiling and not a reaper.
            if os.path.exists(STOP_FILE):
                print("  [heartbeat] job finished — going quiet so the watchdog can "
                      "reap this pod if termination did not take", flush=True)
                return
            try:
                if live is None or live():
                    alive()
            except Exception:
                pass                    # a broken predicate must not kill the loop
            time.sleep(interval_sec)

    t = threading.Thread(target=loop, daemon=True, name="podh-heartbeat")
    t.start()
    print(f"  [heartbeat] reporting to {url} every {interval_sec:.0f}s", flush=True)
    return t


def main(argv: list | None = None) -> int:
    """`python3 -m pod_harness.heartbeat --activity FILE ...` — beat until the work is done.

    Run by podh-init as its own background process for the container's working life, and
    ended by STOP_FILE when the job finishes or fails. A separate process rather than a
    thread in the job, so it covers the boot steps before the job exists and the publish
    after it, and so a job that wedges the interpreter cannot take the evidence with it.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m pod_harness.heartbeat")
    ap.add_argument("--activity", action="append", default=[],
                    help="file whose changes count as work (repeatable)")
    ap.add_argument("--stale-after", type=float,
                    default=float(os.environ.get("PODH_HEARTBEAT_STALE_SEC") or 1800),
                    help="seconds without activity before beats stop (0 = never gate)")
    ap.add_argument("--interval", type=float,
                    default=float(os.environ.get("PODH_HEARTBEAT_SEC") or 60))
    a = ap.parse_args(argv)
    live = activity_live(a.activity, a.stale_after) if a.activity else None
    if live is None:
        print("  [heartbeat] no --activity files: beating unconditionally until stopped",
              flush=True)
    t = start(a.interval, live)
    if t is None:
        return 0
    t.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
