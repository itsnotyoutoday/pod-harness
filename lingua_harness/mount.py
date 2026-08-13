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
        roots = {"corpus_root": self.root / "corpus",
                 "out_root": self.root / "out",
                 "manifest": self.root / "code" / "corpus_research.json"}
        for p in (roots["corpus_root"], roots["out_root"]):
            p.mkdir(parents=True, exist_ok=True)
        missing = [str(p) for p in (roots["corpus_root"],) if not p.exists()]
        return {**{k: str(v) for k, v in roots.items()},
                "ready": not missing, "missing": missing, "strategy": self.kind}

    def publish(self, spec: dict, out_root: Path) -> dict:
        # Nothing to do: the volume IS the durable store, and it outlives the pod.
        return {"published": True, "strategy": self.kind, "location": str(out_root)}

    def launch_env(self, spec: dict) -> dict:
        return {"LINGUA_CORPUS_ROOT": str(self.root / "corpus"),
                "LINGUA_OUT_ROOT": str(self.root / "out"),
                "LINGUA_MANIFEST": str(self.root / "code" / "corpus_research.json")}


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
        corpus = self.scratch / "corpus"
        out = self.scratch / "out"
        corpus.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        pulled = []
        try:
            st = self._storage()
            for src in spec.get("sources", []):
                rel = src.get("path") or f"raw/{src['id']}"
                key = f"{self.prefix}/{rel}".strip("/")
                st.download_prefix(key, corpus / rel)
                pulled.append(key)
        except Exception as exc:
            return {"ready": False, "strategy": self.kind, "missing": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "hint": "check LINGUA_S3_* env on the compute — ObjectMount needs "
                            "credentials at launch, unlike VolumeMount"}
        return {"corpus_root": str(corpus), "out_root": str(out),
                "manifest": str(self.scratch / "corpus_research.json"),
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
        env = {"LINGUA_CORPUS_ROOT": str(self.scratch / "corpus"),
               "LINGUA_OUT_ROOT": str(self.scratch / "out"),
               "LINGUA_MOUNT_KIND": "object"}
        if cfg is not None:
            env.update({"LINGUA_S3_BUCKET": cfg.bucket,
                        "LINGUA_S3_ENDPOINT": cfg.endpoint_url,
                        "LINGUA_S3_ACCESS": cfg.access_key,
                        "LINGUA_S3_SECRET": cfg.secret_key,
                        "LINGUA_S3_REGION": cfg.region})
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
                    "hint": "set LINGUA_S3_* env, see control/objectstore.py"}

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

        return {"corpus_root": str(self.mountpoint),
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
               "LINGUA_OUT_ROOT": str(self.mountpoint.parent / "out"),
               "LINGUA_MOUNT_KIND": "fuse"}
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
    kind = mount.get("kind", os.environ.get("LINGUA_MOUNT_KIND", "local"))
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

    volume_root = Path(os.environ.get("LINGUA_VOLUME_ROOT", "/workspace"))
    if (volume_root / "corpus").exists():
        return VolumeMount(root=str(volume_root))
    if FuseMount.probe()["usable"]:
        return FuseMount(prefix=mount.get("root", ""))
    return ObjectMount(prefix=mount.get("root", ""))
