"""What image this pod runs, and when its container started.

An image built for podh-boot carries /opt/podh/manifest.json (docker/comfyui/
write_manifest.py writes it at build): its name, version tag, build revision and what is
in it. An image cannot contain its own digest — the digest is a hash of the image, made
after the build — so the digest comes from $PODH_IMAGE_DIGEST when whoever started the
container knows it (a control plane that pins images by digest can pass it), else from a
`digest` field in the manifest if something wrote one there, else it is not reported.

`container_started_at` is when the container's first process started (PID 1's start time,
from /proc), falling back to this process's own start. With the pod's create time and its
first heartbeat, the control plane can then tell the image pull (create → container start)
from the container's own start-up (container start → first heartbeat).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

DEFAULT_MANIFEST = "/opt/podh/manifest.json"

#: When this module was first imported: podh-boot imports it at start, so this is the
#: boot process's start when /proc cannot say better.
_IMPORTED_AT = time.time()


def manifest_path() -> Path:
    return Path(os.environ.get("PODH_IMAGE_MANIFEST") or DEFAULT_MANIFEST)


def manifest() -> dict:
    """The image's manifest, or {} when there is none (an image not built for podh-boot)."""
    try:
        obj = json.loads(manifest_path().read_text())
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def identity(man: Optional[dict] = None) -> dict:
    """{"image": "name:version", "image_digest": "sha256:…"} — each only when known."""
    man = manifest() if man is None else man
    out: dict = {}
    name, version = man.get("image"), man.get("version")
    if isinstance(name, str) and name:
        out["image"] = f"{name}:{version}" if isinstance(version, str) and version else name
    digest = os.environ.get("PODH_IMAGE_DIGEST") or man.get("digest")
    if isinstance(digest, str) and digest.strip():
        d = digest.strip()
        if "@" in d:                       # a full reference: keep the digest part
            d = d.rsplit("@", 1)[1]
        out["image_digest"] = d
    return out


def _proc_start(pid: int = 1) -> Optional[float]:
    """Epoch seconds when `pid` started, from /proc (Linux); None elsewhere."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # The command name may hold spaces and parentheses: split after the LAST ')'.
        fields = stat.rsplit(")", 1)[1].split()
        ticks = int(fields[19])            # field 22 overall: starttime, in clock ticks
        btime = None
        for ln in Path("/proc/stat").read_text().splitlines():
            if ln.startswith("btime "):
                btime = int(ln.split()[1])
                break
        if btime is None:
            return None
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def container_started_at() -> float:
    t = _proc_start(1)
    if t is None or t > time.time() + 5:
        t = _proc_start(os.getpid()) or _IMPORTED_AT
    return round(t, 3)


def facts() -> dict:
    return {**identity(), "container_started_at": container_started_at()}
