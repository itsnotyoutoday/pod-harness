"""podh-boot: run a job's boot spec on a pod as ordered, time-limited, reported stages.

    gpu_check → binaries → models → addons → app → workload → ready → app.serve

See cli.py for the command, core.py for what stages share, and each stage's module for its
part of the spec. APP-API §3–§4 and §7 are the contract with the control plane.
"""
from __future__ import annotations


def main(argv=None) -> int:
    from .cli import main as _main
    return _main(argv)
