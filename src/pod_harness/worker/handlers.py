"""Handlers: what runs a task of a given `kind` on the pod (TASK-API §5).

    class MyStep(TaskHandler):
        def prepare(self, task, ctx):          # optional: derive inputs, build what run needs
            return {...}
        def run(self, task, ctx, prepared):    # the work; ctx.progress(...) as it goes
            ...
        def finish(self, task, ctx, result):   # optional: upload, validate; returns the
            ctx.upload(path, "out.wav")         # task's result document (JSON, ≤ 256 KB)
            return {"seconds": 12.5}

`comfy` is the built-in ComfyRunner; an app subclasses it to wrap one workflow, or uses
ctx.comfy.run(...) from a TaskHandler to chain several. Apps declare theirs in
$PODH_APP_ROOT/podh-handlers.json: {"handlers": {"<kind>": "module.path:Class"}}, imported
with the app root on sys.path, one instance per kind for the worker's life.

A handler raises TaskFailed(reason, stage=...) for a failure it understands; any other
exception fails the task with its type and message. ctx.check() raises Cancelled, a
BaseException, so `except Exception` cannot swallow a cancel.
"""
from __future__ import annotations

import copy
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Optional

from ..comfy import Cancelled  # noqa: F401  (re-exported: handlers import it from here)

HANDLERS_FILE = "podh-handlers.json"


class TaskFailed(Exception):
    """A failure the handler understands: reported as error {stage, reason}. `retryable`
    asks pod-control for another attempt (attempt + 1, up to max_attempts) instead of
    failing the task; None leaves it to the stage (inputs, upload and stall are)."""

    def __init__(self, reason: str, stage: str = "run", retryable: Optional[bool] = None):
        super().__init__(reason)
        self.reason, self.stage, self.retryable = reason, stage, retryable


class TaskHandler:
    """Base handler. `run` is the only method a subclass must write."""

    kind: Optional[str] = None

    def prepare(self, task: dict, ctx):
        return None

    def run(self, task: dict, ctx, prepared):
        raise NotImplementedError(f"{type(self).__name__}.run")

    def finish(self, task: dict, ctx, result):
        """The task's result document. The default passes a dict result through."""
        return result if isinstance(result, dict) else None


# -- workflows ----------------------------------------------------------------------------------

def _inside(root: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise TaskFailed(f"workflow path {rel!r} must be relative to the app bundle", "prepare")
    p = (root / rel).resolve()
    if p != root.resolve() and root.resolve() not in p.parents:
        raise TaskFailed(f"workflow path {rel!r} leaves the app bundle", "prepare")
    return p


def _read_json(root: Path, rel: str, what: str):
    p = _inside(root, rel)
    try:
        return json.loads(p.read_text())
    except OSError as e:
        raise TaskFailed(f"{what} {rel}: {e.strerror or e}", "prepare")
    except ValueError as e:
        raise TaskFailed(f"{what} {rel}: not JSON ({e})", "prepare")


def load_workflow(spec: dict, app_root: Path) -> dict:
    """The API-format graph from `workflow.inline` or `workflow.ref` (in the app bundle)."""
    if not isinstance(spec, dict):
        raise TaskFailed("the task has no workflow", "prepare")
    if spec.get("inline") is not None:
        g = spec["inline"]
    elif spec.get("ref"):
        g = _read_json(app_root, spec["ref"], "workflow")
    else:
        raise TaskFailed("workflow needs `ref` or `inline`", "prepare")
    if isinstance(g, dict) and isinstance(g.get("prompt"), dict) and "nodes" not in g:
        g = g["prompt"]
    if not isinstance(g, dict) or isinstance(g.get("nodes"), list):
        raise TaskFailed("workflow must be ComfyUI's API format ({node_id: {class_type, "
                         "inputs}}), not the editor's", "prepare")
    return copy.deepcopy(g)


def load_map(spec: dict, app_root: Path) -> dict:
    """workflow.map: {name: "<node>.inputs.<input>"} inline, or a file in the app bundle."""
    m = (spec or {}).get("map")
    if m is None:
        return {}
    if isinstance(m, str):
        m = _read_json(app_root, m, "workflow map")
    if not isinstance(m, dict):
        raise TaskFailed("workflow.map must be an object", "prepare")
    return {str(k): str(v) for k, v in m.items()}


_PLACEHOLDER = re.compile(r"\{input:([A-Za-z0-9_.-]+)\}")


def substitute(value, names: dict):
    """{input:<name>} → the name ComfyUI knows that input by, in strings, lists and dicts.
    A value input (a dependency's result) replaces a string that is only its placeholder
    as itself (a number stays a number), and is spliced in as text otherwise."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole and whole.group(1) in names:
            return names[whole.group(1)]

        def rep(m):
            if m.group(1) not in names:
                raise TaskFailed(f"patch names input {m.group(1)!r}, which the task does "
                                 f"not have", "prepare")
            v = names[m.group(1)]
            return v if isinstance(v, str) else json.dumps(v)
        return _PLACEHOLDER.sub(rep, value)
    if isinstance(value, list):
        return [substitute(v, names) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, names) for k, v in value.items()}
    return value


def apply_patch(graph: dict, patch: dict, names: Optional[dict] = None,
                mapping: Optional[dict] = None) -> dict:
    """Set graph[node]["inputs"][input] = value for each "<node>.inputs.<input>" (or a name
    from `mapping`). An unknown node is an error, not a silent no-op: a patch that misses
    renders the workflow's defaults and reports success."""
    for key, value in (patch or {}).items():
        target = (mapping or {}).get(key, key)
        node, sep, inp = target.partition(".inputs.")
        if not sep or not node or not inp:
            raise TaskFailed(f"patch key {key!r}: expected '<node>.inputs.<name>' or a name "
                             f"from workflow.map", "prepare")
        if node not in graph or not isinstance(graph[node], dict):
            raise TaskFailed(f"patch key {key!r}: the workflow has no node {node!r}", "prepare")
        graph[node].setdefault("inputs", {})[inp] = substitute(value, names or {})
    return graph


class ComfyRunner(TaskHandler):
    """kind `comfy`: one workflow, patched, run once; outputs by `keep` globs, each uploaded
    under its path relative to the output directory."""

    kind = "comfy"

    def prepare(self, task: dict, ctx) -> dict:
        spec = task.get("workflow") or {}
        graph = load_workflow(spec, ctx.app_root)
        mapping = load_map(spec, ctx.app_root)
        names = {}
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task.get("task_id") or "task"))[:80]
        for name, path in ctx.inputs.items():
            names[name] = ctx.comfy.upload_input(path, name=f"{safe}_{Path(path).name}")
        names.update(getattr(ctx, "values", None) or {})
        return apply_patch(graph, spec.get("patch") or {}, names, mapping)

    def run(self, task: dict, ctx, graph: dict):
        keep = (task.get("outputs") or {}).get("keep") or None
        return ctx.comfy.run(graph, timeout_s=task.get("timeout_s") or None,
                             stall_s=ctx.stall_s, keep=keep, dest=ctx.workdir / "outputs")

    def finish(self, task: dict, ctx, result) -> dict:
        keep = (task.get("outputs") or {}).get("keep") or []
        if keep and not result.files:
            raise TaskFailed(f"the run saved no file matching {keep} "
                             f"({len(result.outputs)} output(s) in all)", "outputs")
        # Each under its path relative to ComfyUI's output directory (subfolder/filename):
        # two runs' "out_00001.mp4" in different subfolders stay two artifacts.
        names = getattr(result, "names", None) or [Path(p).name for p in result.files]
        arts = [ctx.upload(p, n) for p, n in zip(result.files, names)]
        return {"prompt_id": result.prompt_id, "outputs": arts}


# -- loading ----------------------------------------------------------------------------------

def load_handlers(app_root: Optional[Path]) -> tuple:
    """({kind: handler instance}, {kind: load error}) from the built-ins and the app's
    podh-handlers.json. A handler that does not import is an error for its kind only."""
    found: dict = {"comfy": ComfyRunner()}
    errors: dict = {}
    if not app_root:
        return found, errors
    f = Path(app_root) / HANDLERS_FILE
    if not f.is_file():
        return found, errors
    try:
        decl = json.loads(f.read_text()).get("handlers") or {}
        if not isinstance(decl, dict):
            raise ValueError("`handlers` is not an object")
    except (OSError, ValueError, AttributeError) as e:
        errors["*"] = f"{HANDLERS_FILE}: {e}"
        return found, errors
    root = str(Path(app_root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    for kind, target in decl.items():
        try:
            mod, _, cls = str(target).partition(":")
            if not mod or not cls:
                raise ValueError(f"{target!r} is not 'module:Class'")
            obj = getattr(importlib.import_module(mod), cls)
            inst = obj() if isinstance(obj, type) else obj
            if not callable(getattr(inst, "run", None)):
                raise TypeError(f"{target} has no run()")
            found[str(kind)] = inst
        except Exception as e:
            errors[str(kind)] = f"{type(e).__name__}: {e}"
    return found, errors
