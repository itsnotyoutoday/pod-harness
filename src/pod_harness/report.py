"""`podh-report` — the control-plane reports, for shell scripts.

    podh-report boot "<line>" [--stage S]
    podh-report done <rc> [--reason R] [--stage S]
    podh-report busy [--detail D]  |  podh-report idle

The harness image reports from podh-init. A hot-installed image (sidecar mode) has no
podh-init: its own boot script mounts, downloads and starts the workload, and until now had
no way to say what it was doing or why it gave up — so a pod that died fetching a model and
a pod that died with a bad GPU looked identical from outside. This is the same client
podh-init uses, as a command a shell script can call between its steps.

Exit codes are chosen so a boot script can call this without guarding it:

    boot   always 0. A progress line is never a reason to abort the boot it describes.
    done   0 sent; 1 the control plane could not be reached or refused; 3 not configured
           (no PODH_CONTROL_URL or RUNPOD_POD_ID — a local run, or a launcher that does not
           report). `done` also stops this pod's heartbeat either way: the work is over.
    busy   0 sent; 1 the local API did not take it; 3 no PODH_LOCAL_URL (not under
    idle   podh-boot). The flag rides on podh-boot's heartbeat: the control plane's idle
           clock runs only while the pod says it is not busy.

Under podh-boot (PODH_LOCAL_URL set), `done` goes through the local API, so the harness
that supervises the workload hears it too and stops it; if that does not answer it is sent
to the control plane directly, as without podh-boot.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from . import heartbeat, local


def _configured() -> bool:
    url, _, pod_id = heartbeat._cfg()
    return bool(url and pod_id)


def _boot(a) -> int:
    if _configured() and heartbeat.boot(a.line, a.stage) is None and heartbeat.last_error():
        # Said on stderr and still exit 0: see the module docstring.
        print(f"podh-report: boot line not sent — {heartbeat.last_error()}", file=sys.stderr)
    return 0


def _done(a) -> int:
    # Quiet first, then report — the same order as the sidecar's /v1/done. If the request
    # fails, a pod that has stopped heartbeating is one the control plane can still reap.
    try:
        Path(heartbeat.STOP_FILE).touch()
    except OSError:
        pass
    if local.configured():
        st, obj = local.call("POST", "/done", {"rc": a.rc, "reason": a.reason, "stage": a.stage})
        if st == 200 and isinstance(obj, dict):
            if obj.get("requested"):
                print("terminate requested")
                return 0
            if obj.get("configured") is False:
                print("NOT SENT — no control url or pod id configured")
                return 3
            print(f"NOT SENT — {obj.get('error') or 'no response'}")
            return 1
        print("podh-report: local API did not answer; reporting directly", file=sys.stderr)
    if not _configured():
        print("NOT SENT — no control url or pod id configured")
        return 3
    r = heartbeat.done(a.rc, reason=a.reason, stage=a.stage)
    if r is None:
        print(f"NOT SENT — {heartbeat.last_error() or 'no response'}")
        return 1
    print("terminate requested")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="podh-report",
                                 description="report boot progress or completion to pod-control")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("boot", help="one progress line while the pod boots (never fails)")
    b.add_argument("line")
    b.add_argument("--stage", help='the step this line belongs to; "ready" = workload is up')
    d = sub.add_parser("done", help="ask to be terminated, saying how and why the pod ended")
    d.add_argument("rc", type=int)
    d.add_argument("--reason", help=f"short human reason (≤{heartbeat.MAX_REASON} chars)")
    d.add_argument("--stage", help=f"the step that ended it (≤{heartbeat.MAX_STAGE} chars)")
    u = sub.add_parser("busy", help="the app's work is running (under podh-boot)")
    u.add_argument("--detail", help="what it is doing, for the control plane's view")
    sub.add_parser("idle", help="the app's work has stopped (under podh-boot)")
    a = ap.parse_args(argv)
    if a.cmd in ("busy", "idle"):
        return _busy(a.cmd == "busy", getattr(a, "detail", None))
    return _boot(a) if a.cmd == "boot" else _done(a)


def _busy(busy: bool, detail: Optional[str]) -> int:
    if not local.configured():
        print("podh-report: no PODH_LOCAL_URL — busy/idle need podh-boot's local API",
              file=sys.stderr)
        return 3
    body = {"busy": busy}
    if detail:
        body["detail"] = detail
    st, obj = local.call("POST", "/busy", body)
    if st == 200:
        return 0
    print(f"podh-report: local API answered {st or 'nothing'}: {obj}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
