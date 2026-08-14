"""Mount strategies — how a job's data reaches the compute, and the one place RunPod is
actually load-bearing today.

## The coupling, stated plainly

It is natural to assume the tie to RunPod is the S3 API. It is not: `pipeline/core/storage.py`
already takes an explicit `endpoint_url` and works against any S3-compatible store.

The real tie is the MOUNT. `runners/batch_pod.py` passes `network_volume_id` at pod-create,
RunPod attaches its own network volume at `/workspace`, and the pipeline then does ordinary
file I/O. The pod receives NO S3 credentials at all — three path env vars and nothing else,
and the code sync deliberately skips `.key` files. That is a genuinely good security
property, and it works because RunPod exposes one storage two ways: POSIX from inside, S3
API from outside.

Cloudflare R2 cannot be attached that way. There is no variable that points the mount at it.
FUSE (rclone/s3fs) inside the container would need `/dev/fuse` and `SYS_ADMIN`, and
`batch_pod.py:47` already records that RunPod restricts capabilities. So portability needs a
second strategy, not a cleverer mount.

## Two strategies behind a field that already exists

`JobSpec.mount` is `{"kind": "local|s3", "root": "..."}`. The abstraction point was designed
in from the start; it simply was not honoured pod-side. These are the implementations.

    VolumeMount   provider attaches storage; plain file I/O. No creds on the pod.
                  Fast, and the right default while we are on RunPod.
    ObjectMount   the pipeline reads and writes through Storage over the S3 API, with
                  credentials injected at launch. Needs no container capabilities, works on
                  serverless, works with RunPod S3 / R2 / GCS / MinIO alike.

## On the performance trade, honestly

A network volume is NOT local disk — RunPod's own docs rate it "variable (network)" against
faster local options. So both strategies are network-bound and the difference is protocol
overhead, not locality: the volume gives filesystem syscalls, page cache, seeks and partial
reads, while the object path pays an HTTPS request with auth per object.

That difference is still large for many small files, which is why VolumeMount stays the
default. But it is a smaller gap than "local versus remote" implies, and it is largely
engineerable — prefetch a working set to the container disk, batch, and write outputs
directly (they are kilobytes to a few MB). ObjectMount is therefore a viable primary one
day, not merely an escape hatch.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any



def mount_roots(spec: dict | None = None) -> dict[str, str]:
    """The named directories a workload expects, relative to the mount root.

    Supplied by the loader, never invented here. This used to be hardcoded as
    corpus/, out/ and code/corpus_research.json — the linguistics layout, baked into what
    is otherwise a completely generic pod harness. Imports looked clean; the coupling was
    in string literals, which is why the dependency graph did not show it.

    A harness that names a workload's directories can only ever serve that workload. A
    harness that is told them serves any of them, which is the whole point.

    Precedence: spec.mount.roots, then PODH_MOUNT_ROOTS as JSON, then a single generic
    `data` root — a fallback that belongs to no domain, so a workload that forgets to
    declare its roots gets an obviously-wrong answer rather than a plausibly-wrong one.
    """
    import json as _json
    if spec:
        r = (spec.get("mount") or {}).get("roots")
        if isinstance(r, dict) and r:
            return {k: str(v) for k, v in r.items()}
    raw = os.environ.get("PODH_MOUNT_ROOTS", "").strip()
    if raw:
        try:
            r = _json.loads(raw)
            if isinstance(r, dict) and r:
                return {k: str(v) for k, v in r.items()}
        except ValueError:
            raise ValueError(
                f"PODH_MOUNT_ROOTS is not valid JSON: {raw[:80]!r}\n"
                f"  Expected e.g. {{\"corpus_root\": \"corpus\", \"out_root\": \"out\"}}")
    return {"data_root": "data"}


class MountStrategy(ABC):
    """How a job sees its data. Chosen from JobSpec.mount.kind."""

    kind: str = "base"

    @abstractmethod
    def prepare(self, spec: dict) -> dict:
        """Make the job's inputs reachable. Returns the resolved roots the pipeline should
        use, as {corpus_root, out_root, manifest}."""

    @abstractmethod
    def publish(self, spec: dict, out_root: Path) -> dict:
        """Make outputs durable once the job is done."""

    @abstractmethod
    def launch_env(self, spec: dict) -> dict:
        """Env vars the LAUNCHER must set on the compute for this strategy to work.

        This is where credential delivery is decided, and it is deliberately the strategy's
        business rather than the provider's: the provider knows where the work runs, the
        strategy knows what the work needs to read. Secrets are passed at launch, never
        baked into an image.
        """

    def describe(self) -> dict:
        return {"kind": self.kind}


class VolumeMount(MountStrategy):
    """Provider-attached storage, plain file I/O. The default today.

    `prepare` and `publish` are almost no-ops by design: the provider already did the work
    by attaching the volume. That is exactly why it is fast and why it carries no secrets.
    """

    kind = "volume"

    def __init__(self, root: str = "/workspace"):
        self.root = Path(root)

    def prepare(self, spec: dict) -> dict:
        rel = mount_roots(spec)
        roots = {k: self.root / v for k, v in rel.items()}
        # Directories, not files: a declared root ending in a suffix is a pointer to a
        # file the workload supplies, and creating it as a directory would mask its
        # absence with something worse than an error.
        for k, p in roots.items():
            if not p.suffix:
                p.mkdir(parents=True, exist_ok=True)
        missing = [str(p) for p in roots.values() if not p.exists()]
        return {**{k: str(v) for k, v in roots.items()},
                "ready": not missing, "missing": missing, "strategy": self.kind}

    def publish(self, spec: dict, out_root: Path) -> dict:
        # Nothing to do: the volume IS the durable store, and it outlives the pod.
        return {"published": True, "strategy": self.kind, "location": str(out_root)}

    def launch_env(self, spec: dict) -> dict:
        """Export each declared root as LINGUA_<NAME>, derived from the name the loader
        chose rather than from a list this harness carries."""
        env = {}
        for k, v in mount_roots(spec).items():
            env[f"PODH_{k.upper()}"] = str(self.root / v)
            env[f"LINGUA_{k.upper()}"] = str(self.root / v)
        return env


class ObjectMount(MountStrategy):
    """S3-compatible object storage, spoken directly. The portable strategy.

    Inputs are pulled to a local scratch directory before the run and outputs pushed after,
    so the pipeline itself still sees ordinary paths and needs no changes. Pulling a working
    set once beats per-file requests during the run, and it is what keeps the protocol
    overhead bounded.
    """

    kind = "object"

    def __init__(self, prefix: str = "", scratch: str = "/workspace/scratch",
                 profile: str | None = None):
        self.prefix = prefix.strip("/")
        self.scratch = Path(scratch)
        self.profile = profile

    def _storage(self):
        from .objectstore import get_storage
        return get_storage(self.profile)

    def prepare(self, spec: dict) -> dict:
        rel_roots = mount_roots(spec)
        roots = {k: self.scratch / v for k, v in rel_roots.items()}
        for k, d in roots.items():
            if not d.suffix:
                d.mkdir(parents=True, exist_ok=True)
        # Sources land in the FIRST declared root, which the loader orders deliberately.
        # This used to be hardcoded to a directory named "corpus", which quietly made a
        # generic pod harness serve exactly one domain.
        landing = next(iter(roots.values()))
        pulled = []
        try:
            st = self._storage()
            # Fetch concurrently and within a disk budget measured from the pod, not
            # assumed. A remote object costs ~30ms, so pulling 40,000 of them one at a
            # time measures ~8 MB/s and looks like broken storage; 32-way it measures
            # ~125 MB/s. The budget matters because the image itself already occupies the
            # container disk — 6 GB for the MFA image against a 20 GB cap — so "how much
            # can I hold" is a question only the running pod can answer.
            from .chunks import Item, plan
            from .parallel import pmap

            items = []
            for src in spec.get("sources", []):
                rel = src.get("path") or f"raw/{src['id']}"
                key = f"{self.prefix}/{rel}".strip("/")
                pulled.append(key)
                cfg = st.require()
                for page in st.client.get_paginator("list_objects_v2").paginate(
                        Bucket=cfg.bucket, Prefix=key.rstrip("/") + "/"):
                    for o in page.get("Contents", []):
                        r = o["Key"][len(self.prefix.rstrip("/")) + 1:] if self.prefix \
                            else o["Key"]
                        items.append(Item(key=o["Key"], rel=r, size=o["Size"],
                                          # group by source: a stage that reads one source
                                          # must see all of it, never half.
                                          group=src.get("id") or rel))

            # A declared root that names a FILE is a single object the workload needs —
            # the source manifest, a dictionary, a ruleset. sources/ covers directories;
            # nothing covered these, so acquire started with no manifest and died looking
            # for a path that only ever existed on the old volume layout.
            for name, relpath in rel_roots.items():
                if not Path(relpath).suffix:
                    continue
                key = f"{self.prefix}/{relpath}".strip("/") if self.prefix else relpath
                items.append(Item(key=key, rel=relpath, size=0, group=f"_file_{name}"))

            chunks, why = plan(items, path=str(self.scratch))
            print(f"    mount: {why}", flush=True)
            if len(chunks) > 1:
                print(f"    mount: the working set does not fit at once — the job must "
                      f"process it in {len(chunks)} passes, or run on more disk",
                      flush=True)

            cfg = st.require()

            def _get(it):
                dest = landing / it.rel
                if dest.exists() and it.size and dest.stat().st_size == it.size:
                    return 0
                dest.parent.mkdir(parents=True, exist_ok=True)
                import shutil as _sh
                with open(dest, "wb") as f:
                    _sh.copyfileobj(
                        st.client.get_object(Bucket=cfg.bucket, Key=it.key)["Body"], f)
                return 1

            res = pmap(_get, chunks[0].items if chunks else [], workers=32,
                       label="fetch", use_threads=True)
            if res.errors:
                raise RuntimeError(
                    f"{len(res.errors)} object(s) failed to fetch; first: "
                    f"{res.errors[0][2][:140]}\n"
                    f"  Refusing to start with an incomplete working set — a partial "
                    f"corpus produces partial results that look whole.")
        except Exception as exc:
            return {"ready": False, "strategy": self.kind, "missing": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "hint": "check PODH_S3_* env on the compute — ObjectMount needs "
                            "credentials at launch, unlike VolumeMount"}
        return {**{k: str(v) for k, v in roots.items()},
                "ready": True, "pulled": pulled, "strategy": self.kind}

    def publish(self, spec: dict, out_root: Path) -> dict:
        try:
            st = self._storage()
            prefix = f"{self.prefix}/out/{spec.get('job_id', 'unknown')}".strip("/")
            r = st.upload_dir(Path(out_root), prefix)
            return {"published": True, "strategy": self.kind, "prefix": prefix,
                    "detail": r}
        except Exception as exc:
            # Loud, because the outputs are the entire product of a pod-hour. A silent
            # publish failure is the worst outcome in this whole system.
            return {"published": False, "strategy": self.kind,
                    "error": f"{type(exc).__name__}: {exc}"}

    def launch_env(self, spec: dict) -> dict:
        """Credentials go here, at launch, never into the image.

        Only the resolved profile's values are forwarded, so a compute node learns about
        exactly one store and nothing about any other configured profile.
        """
        from .objectstore import resolve_config
        cfg = resolve_config(self.profile)
        # Both prefixes, deliberately. The framework's own variables are PODH_*, but a
        # workload reads whatever it has always read — the trainer looks for
        # LINGUA_MANIFEST — and a harness that silently renames a workload's variables
        # breaks it for no benefit at all.
        env = {}
        for k, v in mount_roots(spec).items():
            env[f"PODH_{k.upper()}"] = str(self.scratch / v)
            env[f"LINGUA_{k.upper()}"] = str(self.scratch / v)
        env["PODH_MOUNT_KIND"] = "object"
        if cfg is not None:
            env.update({"PODH_S3_BUCKET": cfg.bucket,
                        "PODH_S3_ENDPOINT": cfg.endpoint_url,
                        "PODH_S3_ACCESS": cfg.access_key,
                        "PODH_S3_SECRET": cfg.secret_key,
                        "PODH_S3_REGION": cfg.region})
        return env


class FuseMount(MountStrategy):
    """Object storage presented as a POSIX filesystem, via rclone.

    ## The case for it, which is strong

    Every tool works unchanged. MFA, ffmpeg, librosa and soundfile take paths and have no
    idea anything is unusual. There is no prepare/publish phase, no working-set arithmetic
    against a 20 GB container disk, and one code path instead of two. When the abstraction
    holds, this is simply better than ObjectMount.

    ## NOT USABLE ON RUNPOD — settled, do not re-litigate

    Three independent confirmations, 2026-08-13:

      1. Our own probe on a live pod: /dev/fuse ABSENT, and the effective capability mask
         (CapEff 0xa80405fb) decodes to MKNOD granted but **SYS_ADMIN denied**. So even
         after mknod succeeds in creating the device node, mount(2) cannot succeed. This is
         structural, not a configuration anyone can be talked into.
      2. Community reports that RunPod does not support FUSE because it requires granting
         container privileges.
      3. skypilot-org/skypilot#8592 — JuiceFS specifically, failing on RunPod with
         "mknod /dev/fuse: operation not permitted", still failing WITH --privileged, and
         working unchanged on AWS. Unresolved.

    None of this matters much in practice, because RunPod already provides a real POSIX
    mount: the network volume. Use VolumeMount there. FuseMount exists for providers that
    do permit FUSE, and it is verified working against MinIO — including atomic rename
    surviving under --vfs-cache-mode full, which is the property S3 cannot offer natively.

    DECISION (2026-08-13): foreign object storage on RunPod is an accepted LIMITATION. We
    are not building an fsspec/JuiceFS abstraction layer to work around it, because on
    RunPod the volume covers the need and building for a provider we are not on is
    speculative. Revisit only if we target somewhere else. The interfaces here already make
    that a contained change rather than a rewrite.

    ## Why it is a third strategy and not a replacement

    Two things can make the abstraction leak, and neither is hypothetical:

    1. CAPABILITIES. See above — `prepare()` probes and reports a usable reason rather than
       failing obscurely mid-job.

    2. RENAME IS NOT ATOMIC. S3 has no rename; it is copy-then-delete. Anything relying on
       write-temp-then-rename for crash safety silently loses that guarantee — including
       `serve/events.py._atomic_write`, which exists so a poller never reads half a
       status.json. This does not error. It is occasionally, quietly wrong, which is worse.

    `--vfs-cache-mode full` addresses (2): rclone writes to a local cache and uploads on
    close, restoring sane rename and write semantics. That is the mode used here, and it is
    why this is not merely ObjectMount with extra steps — but note it still needs local disk
    for the cache, so the disk budget does not disappear, it just stops being hand-managed.

    ## Choosing between the three

        volume   on RunPod with a network volume — fastest, no credentials on the pod
        fuse     POSIX over any S3, where the capability exists
        object   the guaranteed floor: no capabilities, works on serverless, works anywhere
    """

    kind = "fuse"

    def __init__(self, prefix: str = "", mountpoint: str = "/workspace/corpus",
                 cache_dir: str = "/workspace/.cache/rclone",
                 profile: str | None = None, vfs_cache_mode: str = "full"):
        self.prefix = prefix.strip("/")
        self.mountpoint = Path(mountpoint)
        self.cache_dir = Path(cache_dir)
        self.profile = profile
        self.vfs_cache_mode = vfs_cache_mode

    @staticmethod
    def probe() -> dict:
        """Can FUSE work here at all? Answered before the job starts, not during it."""
        import shutil
        dev = Path("/dev/fuse")
        have_dev = dev.exists()
        have_rclone = shutil.which("rclone") is not None
        writable = False
        if have_dev:
            try:
                with dev.open("rb"):
                    writable = True
            except PermissionError:
                writable = False
            except OSError:
                # ENXIO here means the device node exists and is claimable — the normal
                # result of opening /dev/fuse without a mount, and a GOOD sign.
                writable = True
        return {"usable": bool(have_dev and have_rclone and writable),
                "dev_fuse": have_dev, "rclone": have_rclone, "permitted": writable,
                "hint": None if (have_dev and have_rclone and writable) else
                        ("/dev/fuse missing or not permitted — the provider does not allow "
                         "FUSE in this container; use mount.kind=object instead"
                         if not (have_dev and writable) else
                         "rclone is not installed in this image")}

    def _rclone_env(self) -> dict:
        """rclone is configured entirely by env, so no config file is ever written and no
        credential ever touches disk."""
        from .objectstore import resolve_config
        cfg = resolve_config(self.profile)
        if cfg is None:
            return {}
        return {"RCLONE_CONFIG_S3_TYPE": "s3",
                "RCLONE_CONFIG_S3_PROVIDER": "Other",
                "RCLONE_CONFIG_S3_ENV_AUTH": "false",
                "RCLONE_CONFIG_S3_ACCESS_KEY_ID": cfg.access_key,
                "RCLONE_CONFIG_S3_SECRET_ACCESS_KEY": cfg.secret_key,
                "RCLONE_CONFIG_S3_ENDPOINT": cfg.endpoint_url,
                "RCLONE_CONFIG_S3_REGION": cfg.region,
                "RCLONE_CONFIG_S3_NO_CHECK_BUCKET": "true"}

    def prepare(self, spec: dict) -> dict:
        import subprocess
        probe = self.probe()
        if not probe["usable"]:
            return {"ready": False, "strategy": self.kind, "probe": probe,
                    "error": probe["hint"],
                    "hint": "set mount.kind=object for a strategy that needs no "
                            "capabilities and works on serverless"}

        from .objectstore import resolve_config
        cfg = resolve_config(self.profile)
        if cfg is None:
            return {"ready": False, "strategy": self.kind,
                    "error": "no object store configured",
                    "hint": "set PODH_S3_* env, see control/objectstore.py"}

        self.mountpoint.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        remote = f"s3:{cfg.bucket}/{self.prefix}".rstrip("/")
        cmd = ["rclone", "mount", remote, str(self.mountpoint),
               "--vfs-cache-mode", self.vfs_cache_mode,
               "--cache-dir", str(self.cache_dir),
               "--dir-cache-time", "30s",
               "--daemon"]
        env = {**os.environ, **self._rclone_env()}
        try:
            p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
            if p.returncode != 0:
                return {"ready": False, "strategy": self.kind,
                        "error": (p.stderr or p.stdout or "")[-400:]}
        except Exception as exc:
            return {"ready": False, "strategy": self.kind,
                    "error": f"{type(exc).__name__}: {exc}"}

        return {"corpus_root": str(self.mountpoint),   # FUSE exposes ONE tree
                "out_root": str(self.mountpoint.parent / "out"),
                "manifest": str(self.mountpoint.parent / "corpus_research.json"),
                "ready": True, "strategy": self.kind, "remote": remote, "probe": probe}

    def publish(self, spec: dict, out_root: Path) -> dict:
        """Flush and unmount. The upload already happened on close, but the VFS cache can
        hold writes that have not been flushed, and unmounting is what forces them.

        Several unmount tools are tried because none is universally present — the official
        rclone image ships no `fusermount` at all, so a single hardcoded call silently
        leaves the mount up and the last writes unflushed. That would lose exactly the
        outputs a pod-hour was spent producing, so the failure is reported, not swallowed.
        """
        import subprocess
        attempts = []
        for cmd in (["fusermount3", "-u", str(self.mountpoint)],
                    ["fusermount", "-u", str(self.mountpoint)],
                    ["umount", str(self.mountpoint)]):
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                attempts.append({"cmd": cmd[0], "rc": p.returncode})
                if p.returncode == 0:
                    return {"published": True, "strategy": self.kind, "unmounted_with": cmd[0],
                            "note": "writes uploaded on close by the rclone VFS cache"}
            except FileNotFoundError:
                attempts.append({"cmd": cmd[0], "rc": "not installed"})
            except Exception as exc:
                attempts.append({"cmd": cmd[0], "rc": f"{type(exc).__name__}"})
        return {"published": False, "strategy": self.kind, "attempts": attempts,
                "error": "could not unmount — cached writes may not have been flushed",
                "hint": "install fuse3 (fusermount3) in the image, or use mount.kind=object"}

    def launch_env(self, spec: dict) -> dict:
        env = {"LINGUA_CORPUS_ROOT": str(self.mountpoint),
               "PODH_OUT_ROOT": str(self.mountpoint.parent / "out"),
               "PODH_MOUNT_KIND": "fuse"}
        env.update(self._rclone_env())
        return env


STRATEGIES: dict[str, type[MountStrategy]] = {
    "local": VolumeMount,       # JobSpec's existing vocabulary
    "volume": VolumeMount,
    "s3": ObjectMount,
    "object": ObjectMount,
    "fuse": FuseMount,
}


def for_spec(spec: dict) -> MountStrategy:
    """Resolve the strategy from the spec's existing `mount` field."""
    mount = spec.get("mount") or {}
    kind = mount.get("kind", os.environ.get("PODH_MOUNT_KIND", "local"))
    cls = STRATEGIES.get(kind)
    if cls is None:
        raise ValueError(f"unknown mount kind {kind!r}. Known: {sorted(STRATEGIES)}")
    if cls is ObjectMount:
        return ObjectMount(prefix=mount.get("root", ""), profile=mount.get("profile"))
    if cls is FuseMount:
        return FuseMount(prefix=mount.get("root", ""), profile=mount.get("profile"),
                         mountpoint=mount.get("mountpoint", "/workspace/corpus"))
    return VolumeMount(root=mount.get("root_path", "/workspace"))


def best_available(spec: dict) -> MountStrategy:
    """Pick the best strategy this environment can actually support.

    Preference order is volume → fuse → object: fastest and credential-free first, POSIX
    convenience second, guaranteed-portable floor last. A job that names a strategy
    explicitly gets it; this is for jobs that just want data and do not care how.

    Degrading here rather than failing is the point — the same spec should run on a RunPod
    pod with a volume, on a serverless worker with neither volume nor FUSE, and on a laptop,
    without the caller knowing which.
    """
    mount = spec.get("mount") or {}
    if mount.get("kind"):
        return for_spec(spec)

    volume_root = Path(os.environ.get("PODH_VOLUME_ROOT", "/workspace"))
    if (volume_root / "corpus").exists():
        return VolumeMount(root=str(volume_root))
    if FuseMount.probe()["usable"]:
        return FuseMount(prefix=mount.get("root", ""))
    return ObjectMount(prefix=mount.get("root", ""))
