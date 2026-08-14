"""Rebuild the exact code a job pinned. The read half of the content-addressed code store.

## The layout this reads

    code/<workload>/_packs/<tree>.tgz   every file of that tree, in ONE object
    code/<workload>/_trees/<tree>.json  manifest: {"files": {path: sha}, "exec": [path,…]}
    code/<workload>/jobs/<job_id>       {"tree": <sha>, "workload": …, "git_rev": …, …}

A job names a tree; the tree sha names both the pack and the manifest. Three requests
rebuild any tree, whatever its file count — the per-file layout this replaced cost one
request per file, measured at 1.43 s for 54 files and extrapolating to ~22 minutes at
50,000, billed to a running pod the whole time.

## Why the read side lives here and not in the loader

The harness must not import the loader — that independence is asserted at image build time
by `assert_independence.py`, because the whole point of splitting the repos was that a pod
carries the engine and nothing else.

That split is safe here because the two sides are **asymmetric**: only the writer computes
hashes and canonicalises trees. This side reads a tree and fetches blobs by name. There is
no shared serialisation logic, so there is nothing to drift — which is the failure this
project has already paid for three times when two copies of the same knowledge disagreed.

## Why it verifies

Every file is re-hashed against the tree after it is written. It is nearly free next to the
download, and it converts a truncated or corrupted fetch into a clear error at fetch time
rather than a `SyntaxError` at import time in a module nobody edited.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import os
from pathlib import Path


class CodeStoreError(RuntimeError):
    pass


def read_pointer(job_key: str, *, store=None) -> dict:
    """The job pointer: which tree this job ran, and how it was published."""
    if store is None:
        from .objectstore import get_storage
        store = get_storage()
    cfg = store.require()
    try:
        return json.loads(store.client.get_object(
            Bucket=cfg.bucket, Key=job_key)["Body"].read())
    except Exception as e:
        raise CodeStoreError(
            f"no code pointer at {job_key} ({type(e).__name__}).\n"
            f"  The loader writes this before the pod is created, so an absent pointer "
            f"means the launch did not finish publishing — not that the code is missing.") from e


def fetch(job_key: str, dest: str | Path, *, store=None, verify: bool = True,
          verbose: bool = True, **_ignored) -> dict:
    """Rebuild the tree that `job_key` pins, under `dest`.

    Three requests regardless of how many files the tree holds: the pointer, the pack, and
    the manifest it is verified against. The previous per-file layout cost one request per
    file — measured at 1.43 s for 54 files against R2, extrapolating to ~22 minutes at
    50,000, all of it billed to a running pod.
    """
    if store is None:
        from .objectstore import get_storage
        store = get_storage()
    cfg = store.require()

    pointer = read_pointer(job_key, store=store)
    workload = pointer.get("workload") or job_key.split("/")[1]
    tree_sha = pointer.get("tree")
    if not tree_sha:
        raise CodeStoreError(f"code pointer {job_key} names no tree: {pointer}")

    base = f"code/{workload}"
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        pack = store.client.get_object(
            Bucket=cfg.bucket, Key=f"{base}/_packs/{tree_sha}.tgz")["Body"].read()
    except Exception as e:
        raise CodeStoreError(
            f"job {job_key} points at tree {tree_sha[:12]} whose pack is not in the store "
            f"({type(e).__name__}).\n"
            f"  A pointer is written only after its pack, so this means the pack was "
            f"deleted — check whether a gc ran against live jobs.") from e

    with tarfile.open(fileobj=io.BytesIO(pack), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            # A pack is data from the store. An entry like ../../etc/x would otherwise
            # write anywhere the pod can reach — the classic tar extraction escape.
            out = (dest / member.name).resolve()
            if not str(out).startswith(str(dest.resolve())):
                raise CodeStoreError(
                    f"pack entry escapes the destination: {member.name!r}")
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            out.write_bytes(src.read())
            if member.mode & 0o111:
                out.chmod(out.stat().st_mode | 0o111)

    checked = 0
    if verify:
        # Against the manifest, not the pack's own bytes. The manifest records the sha of
        # each ORIGINAL file, so this proves the reconstruction equals what was published
        # — a truncated pack or a bad extraction otherwise surfaces as a SyntaxError at
        # import time in a module nobody edited.
        try:
            tree = json.loads(store.client.get_object(
                Bucket=cfg.bucket, Key=f"{base}/_trees/{tree_sha}.json")["Body"].read())
        except Exception as e:
            raise CodeStoreError(
                f"tree manifest {tree_sha[:12]} is unreadable ({type(e).__name__}); "
                f"cannot verify the rebuild. Pass verify=False to proceed anyway.") from e
        bad = []
        for rel, sha in (tree.get("files") or {}).items():
            f = dest / rel
            if not f.exists():
                bad.append(f"{rel}: missing from the pack")
            elif hashlib.sha256(f.read_bytes()).hexdigest() != sha:
                bad.append(f"{rel}: content does not match the manifest")
            else:
                checked += 1
        if bad:
            raise CodeStoreError(
                f"{len(bad)} file(s) did not survive the round trip; first: {bad[0]}\n"
                f"  Refusing to run against a code tree that is not what was published.")

    out = {"tree": tree_sha, "workload": workload, "files": pointer.get("files", checked),
           "verified": checked, "bytes": pointer.get("bytes", 0),
           "git_rev": pointer.get("git_rev", ""), "git_dirty": pointer.get("git_dirty"),
           "dest": str(dest)}
    if verbose:
        dirty = " (dirty)" if pointer.get("git_dirty") else ""
        print(f"  code: {out['files']} file(s) from tree {tree_sha[:12]} "
              f"[git {pointer.get('git_rev', '?')}{dirty}] → {dest}"
              + (f", {checked} verified" if verify else ""), flush=True)
    return out


def fetch_legacy(prefix: str, dest: str | Path, *, store=None) -> dict:
    """Rebuild from the OLD layout, where a revision was a directory of real objects.

    Kept because revisions published before the content-addressed store exist and must stay
    launchable. Deleting this would strand them, and 'republish everything' is not a
    migration, it is a demand.
    """
    if store is None:
        from .objectstore import get_storage
        store = get_storage()
    cfg = store.require()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    p = prefix.rstrip("/") + "/"
    n = 0
    for page in store.client.get_paginator("list_objects_v2").paginate(
            Bucket=cfg.bucket, Prefix=p):
        for o in page.get("Contents", []):
            rel = o["Key"][len(p):]
            if not rel or rel.startswith("."):
                continue
            f = dest / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(store.client.get_object(
                Bucket=cfg.bucket, Key=o["Key"])["Body"].read())
            n += 1
    return {"files": n, "dest": str(dest), "layout": "legacy"}
