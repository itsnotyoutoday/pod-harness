"""Object-store resolution — so we are not married to any one S3 vendor.

## What already exists, and what this adds

`pipeline/core/storage.py` is already vendor-neutral in the way that matters: `S3Config`
carries an explicit `endpoint_url`, and `region_from_endpoint()` exists because SigV4
rejects a mismatched signing region outright — the failure that makes people wrongly
conclude "this only works with AWS".

What it does not have is a way to configure that from ENV, or to hold more than one store
at a time. Both matter here:

  * A pod or a serverless worker is configured by environment, not by a key file that has
    to be baked in or mounted. Baking credentials into an image is exactly what the whole
    deps-only doctrine exists to avoid.
  * We have two real stores today — RunPod's S3 for network volumes, and Cloudflare R2 —
    and the decision of which to use is per-deployment, not per-build.

So: named profiles, resolved from env, falling back to the existing key file. Nothing about
Storage changes; this only decides which S3Config to hand it.

## Profiles

    LINGUA_S3_PROFILE=r2            which profile is active

    LINGUA_S3_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com
    LINGUA_S3_R2_BUCKET=lingua
    LINGUA_S3_R2_ACCESS=…
    LINGUA_S3_R2_SECRET=…
    LINGUA_S3_R2_REGION=auto        optional; see the Cloudflare note below

    LINGUA_S3_RUNPOD_ENDPOINT=https://s3api-us-nc-1.runpod.io
    LINGUA_S3_RUNPOD_BUCKET=…
    …

Unprefixed `LINGUA_S3_ENDPOINT` / `_BUCKET` / `_ACCESS` / `_SECRET` / `_REGION` work as the
default profile, which is what a single-store deployment should use.

## The Cloudflare R2 trap

`region_from_endpoint()` matches hosts shaped like `s3api-us-nc-1.runpod.io`. An R2 endpoint
is `https://<account-id>.r2.cloudflarestorage.com` — no region segment at all — so the regex
does not match and it falls back to `us-east-1`. R2 wants `auto`. Getting this wrong
produces an opaque SigV4 AccessDenied that reads like a credentials problem and is not, so
this module special-cases it rather than leaving it to be rediscovered under time pressure.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# host fragment -> correct signing region, for stores whose endpoint carries no region.
_REGION_QUIRKS = {
    "r2.cloudflarestorage.com": "auto",      # Cloudflare R2
    "storage.googleapis.com": "auto",        # GCS S3-compat
}


def active_profile() -> str:
    return os.environ.get("LINGUA_S3_PROFILE", "").strip()


def _env(profile: str, field: str) -> str:
    """Profile-prefixed lookup with an unprefixed fallback."""
    if profile:
        v = os.environ.get(f"LINGUA_S3_{profile.upper()}_{field}")
        if v:
            return v.strip()
    return (os.environ.get(f"LINGUA_S3_{field}") or "").strip()


def signing_region(endpoint_url: str, explicit: str = "") -> str:
    """Resolve the SigV4 region for an arbitrary S3-compatible endpoint.

    Order: explicit wins, then the known quirks, then the pipeline's own host parser, then
    the AWS default. Every step is deliberate — silently defaulting to us-east-1 is what
    breaks non-AWS stores.
    """
    if explicit:
        return explicit
    for frag, region in _REGION_QUIRKS.items():
        if frag in (endpoint_url or ""):
            return region

    # Parsed HERE rather than delegated to pipeline.core.storage. Delegating meant that
    # whenever the pipeline package was not importable — a standalone control plane, a
    # serverless worker carrying only the harness — the import failed and the fallback
    # silently returned "us-east-1" for a RunPod endpoint that requires "us-nc-1". The
    # symptom is an AccessDenied that reads like bad credentials. A three-line regex is
    # cheaper than that debugging session, so it is duplicated deliberately; keep it in
    # step with region_from_endpoint().
    import re
    m = re.search(r"s3(?:api)?[.-]([a-z]{2}-[a-z]{2,4}-\d+)", endpoint_url or "")
    if m:
        return m.group(1)
    return "us-east-1"


@dataclass
class _StandaloneS3Config:
    """Fallback used when the pipeline package is not importable.

    A control plane, or a worker carrying only the harness, must still be able to resolve
    and REPORT which store it is configured against. Hard-depending on
    `pipeline.core.storage` meant `describe()` answered "not configured" while four correct
    R2 variables sat in the environment — a lie that sends you looking in the wrong place.

    Field-compatible with the pipeline's S3Config, so whichever is in play the rest of the
    code is identical.
    """
    bucket: str
    endpoint_url: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    source_file: str = ""

    def describe(self) -> dict:
        import hashlib
        return {"bucket": self.bucket, "endpoint_url": self.endpoint_url,
                "access_key_prefix": (self.access_key[:6] + "…") if self.access_key else None,
                "secret_fingerprint": (
                    hashlib.sha256(self.secret_key.encode()).hexdigest()[:12]
                    if self.secret_key else None),
                "source_file": self.source_file}


def _config_types():
    """Prefer the pipeline's own S3Config so the two never drift; fall back when absent."""
    try:
        from .storage import S3Config, load_config     # type: ignore
        return S3Config, load_config
    except Exception:
        return _StandaloneS3Config, (lambda *a, **kw: None)


def resolve_config(profile: str | None = None) -> Any | None:
    """Build an S3Config from env, or fall back to the key file Storage already reads.

    Returns None when nothing is configured — callers must treat object storage as
    optional, because the local and docker paths work without it and a control plane that
    refuses to start without S3 credentials is needlessly fragile.
    """
    S3Config, load_config = _config_types()
    prof = profile if profile is not None else active_profile()
    endpoint = _env(prof, "ENDPOINT")
    bucket = _env(prof, "BUCKET")
    access = _env(prof, "ACCESS") or _env(prof, "ACCESS_KEY_ID")
    secret = _env(prof, "SECRET") or _env(prof, "SECRET_ACCESS_KEY")

    if bucket and access and secret:
        return S3Config(
            bucket=bucket, endpoint_url=endpoint, access_key=access, secret_key=secret,
            region=signing_region(endpoint, _env(prof, "REGION")),
            source_file=f"env:LINGUA_S3_{prof.upper() + '_' if prof else ''}*")

    # No env config — the key file remains a first-class way to configure this, and is
    # still the right one on a laptop.
    return load_config()


def get_storage(profile: str | None = None):
    """A Storage bound to the resolved profile. The only place a store is chosen."""
    from .storage import Storage                        # type: ignore
    return Storage(resolve_config(profile))


def describe(profile: str | None = None) -> dict:
    """Fingerprints only, safe to log and safe to return from the API.

    Reported by discovery so an operator can confirm WHICH store a pod is actually talking
    to — the question you ask when results are landing somewhere you did not expect.
    """
    cfg = resolve_config(profile)
    if cfg is None:
        return {"configured": False,
                "profile": profile if profile is not None else active_profile(),
                "hint": "set LINGUA_S3_BUCKET/_ACCESS/_SECRET/_ENDPOINT, or provide a "
                        "runpods3.key file"}
    d = cfg.describe()
    d.update({"configured": True,
              "profile": profile if profile is not None else active_profile(),
              "region": cfg.region})
    return d
