"""podh-worker: the generic task worker for pods in a task pool (TASK-API §4–§5, §8).

    podh-worker [--capacity N] [--prefetch N] [--once] [--kinds a,b] [--poll S]
                [--stall-s S] [--interrupt-grace S] [--app-root DIR] [--workdir DIR]

The default `app.serve` of a task pool: it claims tasks from pod-control with the pod's
token, fetches what a held task needs through podh-boot's local API, runs each through the
handler for its `kind`, uploads the outputs to presigned links and reports done — beating
every lease_s/3 and keeping the local busy flag true while it holds work.

Handlers are the interface an app writes against:

    from pod_harness.worker import TaskHandler, ComfyRunner, TaskFailed, Cancelled
"""
from __future__ import annotations

from ..comfy import Cancelled, ComfyClient, ComfyError
from .context import TaskContext
from .handlers import ComfyRunner, TaskFailed, TaskHandler, load_handlers
from .loop import Worker

__all__ = ["Cancelled", "ComfyClient", "ComfyError", "ComfyRunner", "TaskContext",
           "TaskFailed", "TaskHandler", "Worker", "load_handlers", "main"]


def main(argv=None) -> int:
    from .cli import main as _main
    return _main(argv)
