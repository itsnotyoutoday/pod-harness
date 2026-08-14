"""S3-compatible object storage — moving corpus in and results out.

Backed by RunPod's S3 endpoint for network volumes, but nothing here is RunPod-specific:
any S3-compatible store works by pointing `endpoint_url` elsewhere.

## Why object storage rather than rsync

A pod is disposable. rsync ties results to one machine's lifetime; S3 outlives the pod, so a
spot instance can vanish mid-run without losing what it already produced. It also gives the
laptop and the pod a shared view — upload once, run anywhere.

## Credentials

Read from a key file, never baked into an image and never logged.

    runpods3.key
        bucket_name=…
        endpoint_url=…
        access=…
        secret=…

`describe()` returns a fingerprint, never the secret.

## What moves, and what does not

    UP    corpus_data/raw/<source>/   audio only — archives and caches are pure cost
    DOWN  out/                        JSON, embeddings, measurements. Small.

Audio never comes back: it was already uploaded, and re-downloading gigabytes to a laptop
defeats the point of pushing the work out.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_KEY_FILES = ("runpods3.key", "/run/secrets/runpods3.key",
                     "../runpods3.key", "/app/runpods3.key")


def _search_up(name: str, levels: int = 6):
    """See runpod_api._search_up — fixed relative paths broke when the repos moved."""
    import pathlib as _pl
    cur = _pl.Path.cwd().resolve()
    home = _pl.Path.home().resolve()
    for _ in range(levels):
        c = cur / name
        if c.is_file():
            return c
        if cur == home or cur.parent == cur:
            break
        cur = cur.parent
    return None

# Never uploaded: derived, re-creatable, or secret.
SKIP_PATTERNS = (".zip", ".tgz", ".tar.gz", ".key", ".env", ".DS_Store", "__pycache__")


@dataclass
class S3Config:
    bucket: str
    endpoint_url: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    source_file: str = ""

    def describe(self) -> dict:
        """Safe to log — fingerprints only."""
        return {
            "bucket": self.bucket,
            "endpoint_url": self.endpoint_url,
            "access_key_prefix": self.access_key[:6] + "…" if self.access_key else None,
            "secret_fingerprint": (
                hashlib.sha256(self.secret_key.encode()).hexdigest()[:12]
                if self.secret_key else None),
            "source_file": self.source_file,
        }


def region_from_endpoint(endpoint_url: str, default: str = "us-east-1") -> str:
    """Derive the signing region from the endpoint host.

    RunPod endpoints look like https://s3api-us-nc-1.runpod.io, and SigV4 rejects a
    mismatched region outright:

        AccessDenied: the region 'us-east-1' is wrong; expecting 'us-nc-1'

    Defaulting to us-east-1 silently breaks every non-AWS S3 endpoint, so parse it.
    """
    import re

    # Stores whose endpoint carries no region at all. R2 wants "auto"; defaulting to
    # us-east-1 produces a SigV4 AccessDenied that reads like bad credentials.
    for frag, region in (("r2.cloudflarestorage.com", "auto"),
                         ("storage.googleapis.com", "auto")):
        if frag in (endpoint_url or ""):
            return region

    m = re.search(r"s3(?:api)?[.-]([a-z]{2}-[a-z]{2,4}-\d+)", endpoint_url or "")
    return m.group(1) if m else default


def load_config(path: str | Path | None = None) -> S3Config | None:
    """Parse a key file of `name=value` lines. Returns None rather than raising."""
    candidates = [path] if path else []
    candidates += [os.environ.get("PODH_S3_KEY_FILE")] + list(DEFAULT_KEY_FILES)
    _up = _search_up("runpods3.key")
    if _up:
        candidates.append(str(_up))
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if not p.is_file():
            continue
        kv = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            kv[k.strip().lower()] = v.strip()
        # Accept common spellings. A key file that differs by one field name resolves to
        # an EMPTY bucket rather than an error, and the resulting AccessDenied reads like a
        # credentials problem — so alias rather than demand exactness.
        for alias, canon in (("buck_name", "bucket_name"), ("bucket", "bucket_name"),
                             ("endpoint", "endpoint_url"), ("access_key", "access"),
                             ("access_key_id", "access"), ("secret_key", "secret"),
                             ("secret_access_key", "secret")):
            if alias in kv and canon not in kv:
                kv[canon] = kv[alias]
        if not kv.get("bucket_name") or not kv.get("access"):
            continue
        endpoint = (kv.get("endpoint_url", "") or "").rstrip("/")
        # A pasted console URL often ends in the bucket. boto3 adds the bucket itself under
        # path addressing, so leaving it produces keys at /<bucket>/<bucket>/… — objects
        # that write successfully and can never be found.
        _b = kv.get("bucket_name", "")
        if _b and endpoint.endswith("/" + _b):
            endpoint = endpoint[: -(len(_b) + 1)]
        return S3Config(
            bucket=kv["bucket_name"],
            endpoint_url=endpoint,
            access_key=kv.get("access", ""),
            secret_key=kv.get("secret", ""),
            region=kv.get("region") or region_from_endpoint(endpoint),
            source_file=str(p),
        )
    return None


def write_prefixes() -> tuple[str, ...]:
    """Object-key prefixes this harness may write to, as granted by the loader.

    Deliberately has no default. A default would be the harness deciding part of the
    layout, which is the thing that keeps going wrong.
    """
    import os
    raw = os.environ.get("PODH_WRITE_PREFIXES", "").strip()
    if not raw:
        run = os.environ.get("PODH_RUN_PREFIX", "").strip("/")
        if run:
            return (run + "/",)
        raise PermissionError(
            "PODH_WRITE_PREFIXES is not set, so this harness has no write grant.\n"
            "  The loader must state where a job may write. Refusing rather than "
            "defaulting: the default would be a second definition of the storage layout.")
    return tuple(p.strip().strip("/") + "/" for p in raw.split(",") if p.strip())


class Storage:
    """Thin S3 wrapper: check, upload a directory, download a prefix."""

    def __init__(self, cfg: S3Config | None = None):
        self.cfg = cfg or load_config()
        self._client = None

    @property
    def available(self) -> bool:
        return self.cfg is not None

    def require(self) -> S3Config:
        if not self.cfg:
            raise RuntimeError(
                "no S3 credentials. Provide runpods3.key with bucket_name, endpoint_url, "
                "access and secret, or set PODH_S3_KEY_FILE.")
        return self.cfg

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            cfg = self.require()
            self._client = boto3.client(
                "s3",
                endpoint_url=cfg.endpoint_url or None,
                aws_access_key_id=cfg.access_key,
                aws_secret_access_key=cfg.secret_key,
                region_name=cfg.region,
                config=Config(retries={"max_attempts": 5, "mode": "standard"},
                              s3={"addressing_style": "path"}),
            )
        return self._client

    # -- checks -------------------------------------------------------------------------

    def put(self, key: str, body, *, where: str = "") -> dict:
        """Write one object, but only beneath a prefix the loader granted.

        The single door for writes. Enforcing here rather than at each call site is the
        difference between a structure and a suggestion — three incompatible layouts grew
        in this bucket precisely because every caller invented its own path.

        The rule used to be "validate against STRUCTURE.md", which required the harness to
        carry a copy of the storage layout. That copy is what drifted: the layout changed
        in the loader and the harness kept writing to the old locations, three times in one
        day. So the harness no longer knows the layout at all. It knows only which prefixes
        it was granted, which is a rule that stays true no matter how the layout evolves.

        `where` is only for the error message, naming who tried, so the fix is obvious.
        """
        allowed = write_prefixes()
        k = key.lstrip("/")
        if not any(k.startswith(p) for p in allowed):
            raise PermissionError(
                f"{where or 'a write'} tried to write {key!r}, which is outside every "
                f"prefix this harness was granted: {', '.join(allowed)}.\n"
                f"  The loader grants prefixes via PODH_WRITE_PREFIXES. A harness that "
                f"could write anywhere could overwrite corpus/raw/, the one thing in this "
                f"system that cannot be regenerated.")
        cfg = self.require()
        return self.client.put_object(Bucket=cfg.bucket, Key=k, Body=body)

    def check(self) -> dict:
        """Verify connectivity WITHOUT uploading. Run before moving anything large."""
        try:
            cfg = self.require()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            self.client.head_bucket(Bucket=cfg.bucket)
            resp = self.client.list_objects_v2(Bucket=cfg.bucket, MaxKeys=5)
            keys = [o["Key"] for o in resp.get("Contents", [])]
            return {"ok": True, **cfg.describe(),
                    "objects_sampled": keys,
                    "object_count_hint": resp.get("KeyCount", 0)}
        except Exception as exc:
            return {"ok": False, **cfg.describe(),
                    "error": f"{type(exc).__name__}: {str(exc)[:220]}"}

    # -- transfer -----------------------------------------------------------------------

    def _skip(self, p: Path) -> bool:
        s = str(p)
        return any(pat in s for pat in SKIP_PATTERNS)

    def upload_dir(self, local: Path, prefix: str, *, dry_run: bool = False,
                   max_files: int | None = None, skip_existing: bool = False) -> dict:
        """Upload a tree. With skip_existing, send only what the store does not already have.

        skip_existing exists for corpus/raw/, which is 11 GB and grows: acquire downloads a
        source into it, and without publishing that back the download dies with the pod and
        the next run fetches it from the origin again — which for one 2,142 MB file was
        measured crawling at 0.05 MB/s. Re-uploading the whole tree every run to save that
        would be worse than the problem.

        The check is ONE paginated listing compared against local names, not a HEAD per
        file: at 19,000 objects that is ~19 requests instead of 19,000, and it gets
        relatively cheaper as the tree grows.

        Deliberately name-and-size only. Hashing every object to decide whether to skip
        costs more than the upload it avoids, and these objects are immutable by policy —
        corpus/raw is "exactly as downloaded, never modified".
        """
        local = Path(local)
        if not local.exists():
            return {"ok": False, "error": f"not found: {local}"}
        files = [p for p in sorted(local.rglob("*"))
                 if p.is_file() and not self._skip(p)]
        if max_files:
            files = files[:max_files]
        total = sum(p.stat().st_size for p in files)

        if dry_run:
            return {"ok": True, "dry_run": True, "files": len(files),
                    "bytes": total, "megabytes": round(total / 1e6, 2),
                    "prefix": prefix,
                    "sample": [str(p.relative_to(local)) for p in files[:5]]}

        cfg = self.require()

        have: dict = {}
        if skip_existing:
            base = prefix.rstrip("/") + "/"
            for page in self.client.get_paginator("list_objects_v2").paginate(
                    Bucket=cfg.bucket, Prefix=base):
                for o in page.get("Contents", []):
                    have[o["Key"][len(base):]] = o["Size"]

        todo = []
        skipped = 0
        for p in files:
            rel = p.relative_to(local).as_posix()
            if skip_existing and have.get(rel) == p.stat().st_size:
                skipped += 1
                continue
            todo.append((p, f"{prefix.rstrip('/')}/{rel}"))

        # Concurrent, for the same reason the fetch path is: an object costs ~30 ms of
        # round trip and almost no bandwidth, so a sequential loop measures the latency,
        # not the link. Publishing 15,921 files one at a time was observed taking minutes
        # while the same store fetches 32-way at ~125 MB/s.
        #
        # Threads rather than processes: this is entirely network wait, and boto3 clients
        # are documented as thread-safe for distinct calls.
        def _put(item):
            f, key = item
            with f.open("rb") as fh:
                self.client.put_object(Bucket=cfg.bucket, Key=key, Body=fh)
            return 1

        sent = 0
        if todo:
            from concurrent.futures import ThreadPoolExecutor
            done = 0
            with ThreadPoolExecutor(max_workers=32) as ex:
                for r in ex.map(_put, todo):
                    sent += r
                    done += 1
                    if done % 500 == 0:
                        print(f"    uploaded {done}/{len(todo)}", flush=True)

        return {"ok": True, "files": sent, "skipped": skipped, "bytes": total,
                "megabytes": round(total / 1e6, 2), "prefix": prefix}

    def download_prefix(self, prefix: str, local: Path, *,
                        max_files: int | None = None) -> dict:
        cfg = self.require()
        local = Path(local)
        local.mkdir(parents=True, exist_ok=True)
        paginator = self.client.get_paginator("list_objects_v2")
        got, nbytes = 0, 0
        for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if max_files and got >= max_files:
                    break
                rel = obj["Key"][len(prefix):].lstrip("/")
                if not rel:
                    continue
                dst = local / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                # get_object, NOT download_file: boto3's transfer manager issues a
                # HeadObject first, and RunPod's S3 returns 403 for HEAD even when GET is
                # permitted. Streaming the body avoids the unsupported call entirely.
                body = self.client.get_object(Bucket=cfg.bucket, Key=obj["Key"])["Body"]
                with dst.open("wb") as fh:
                    for chunk in iter(lambda: body.read(1 << 20), b""):
                        fh.write(chunk)
                got += 1
                nbytes += obj.get("Size", 0)
        return {"ok": True, "files": got, "bytes": nbytes,
                "megabytes": round(nbytes / 1e6, 2), "prefix": prefix,
                "local": str(local)}

    def roundtrip_test(self) -> dict:
        """Upload a tiny object, read it back, delete it. Proves write+read+delete."""
        import io
        import json as _json
        from datetime import datetime, timezone

        cfg = self.require()
        key = "_lingua_selftest/roundtrip.json"
        payload = _json.dumps({"test": True,
                               "at": datetime.now(timezone.utc).isoformat()}).encode()
        try:
            self.client.put_object(Bucket=cfg.bucket, Key=key, Body=payload)
            back = self.client.get_object(Bucket=cfg.bucket, Key=key)["Body"].read()
            ok = back == payload
            self.client.delete_object(Bucket=cfg.bucket, Key=key)
            return {"ok": ok, "wrote_bytes": len(payload),
                    "read_back_identical": ok, "key": key,
                    "note": "write, read and delete all succeeded" if ok
                            else "content mismatch on read-back"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:220]}"}
