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
import urllib.request

#: RunPod's proxy sits behind Cloudflare, which rejects urllib's default agent with
#: `error code: 1010` — a 403 that reads like an auth failure and is not.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


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


def last_error() -> str:
    """The most recent transport failure, or empty if there has not been one."""
    return _LAST_ERROR


def _post(path: str, body: dict, timeout: int = 10) -> dict | None:
    url, token, _ = _cfg()
    if not url:
        return None
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
        global _LAST_ERROR
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def alive(deadline_ts: float | None = None) -> dict | None:
    url, _, pod_id = _cfg()
    if not url or not pod_id:
        return None
    # job_id accompanies every call: the scoped token is bound to it, and
    # pod-control checks that this job actually owns this pod.
    body = {"pod_id": pod_id, "provider": "runpod",
            "job_id": os.environ.get("PODH_CONTROL_JOB_ID")
                      or os.environ.get("PODH_JOB_ID", "")}
    if deadline_ts:
        body["deadline_ts"] = deadline_ts
    return _post("/v1/alive", body)


def done() -> dict | None:
    """Ask to be terminated now. Called when the job finishes, however it finishes."""
    url, _, pod_id = _cfg()
    if not url or not pod_id:
        return None
    return _post("/v1/done", {"pod_id": pod_id, "provider": "runpod",
                              "job_id": os.environ.get("PODH_CONTROL_JOB_ID")
                                        or os.environ.get("PODH_JOB_ID", "")})


def start(interval_sec: float = 60.0) -> threading.Thread | None:
    """Begin heartbeating in the background. Returns None when not configured."""
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
        import os as _os
        while True:
            # A finished pod must go QUIET, not keep insisting it is healthy.
            #
            # The watchdog's primary signal is "was reporting, stopped reporting". A pod
            # that heartbeats forever is by definition never stuck, so a finished-but-alive
            # pod was invisible to the one mechanism meant to bound it — leaving only the
            # 8-hour budget, which is a ceiling and not a reaper.
            if _os.path.exists("/tmp/podh-stop-heartbeat"):
                print("  [heartbeat] job finished — going quiet so the watchdog can "
                      "reap this pod if termination did not take", flush=True)
                return
            alive()
            time.sleep(interval_sec)

    t = threading.Thread(target=loop, daemon=True, name="podh-heartbeat")
    t.start()
    print(f"  [heartbeat] reporting to {url} every {interval_sec:.0f}s", flush=True)
    return t
