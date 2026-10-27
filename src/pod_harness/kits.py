"""`podh-kit` — cached tarballs: the first pod builds a thing, later pods unpack it.

    podh-kit get <name> <app-key> <dir> [--python PY]    0 filled · 2 no kit yet · 1 error
    podh-kit put <name> <app-key> <dir> [--python PY]    0 published · 4 another pod is
                                                         building it · 1 error
    (both: 3 when no control plane is configured)

A kit is a built venv, a wheelhouse, a pinned binary, a prepared addon set — anything that
is slow to make and identical on every pod that makes it. Keyed twice: `app-key` is the
caller's part (a requirements hash, a version), and the pod adds what makes a build
non-portable — the Python it was built for, torch and CUDA if present, and the image digest
if the image says it. A kit is only ever reused where all of that matches; a venv unpacked
onto a different Python is worse than none.

The control plane holds no bytes. It answers with presigned URLs:

    GET  /v1/pod/kits/{name}?key=&python=&torch=&cuda=&image=   200 {url, sha256, size} | 404
    POST /v1/pod/kits/{name} {key, sha256, size, format, python, torch, cuda, image}
         → 200 {url, headers?}  (a presigned PUT; 409 = another pod holds the claim)
    PUT  <url>  the archive
    POST /v1/pod/kits/{name}/commit {key, sha256, size}   (a server that verifies on its own
         may not have it; 404 is fine)

`get` checks the sha256 before unpacking, and unpacks atomically: the directory is either
what it was or the whole kit, never half of one. Archives are tar.zst when zstd is
available (set PODH_KIT_FORMAT=gz to force gzip) and tar.gz otherwise; either is read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import fetch, heartbeat

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EXIT_ABSENT = 2
EXIT_NOT_CONFIGURED = 3
EXIT_CLAIMED = 4

_FACTS_PROBE = r"""
import json, sys
out = {"python": "%d.%d" % sys.version_info[:2]}
try:
    import torch
    out["torch"] = torch.__version__
    out["cuda"] = torch.version.cuda
except Exception:
    pass
print("PODH_FACTS " + json.dumps(out))
"""


class KitError(Exception):
    pass


def facts(python: Optional[str] = None, timeout: float = 120) -> dict:
    """What a build depends on: {python, torch?, cuda?, image?} for the given interpreter.

    The workload's python, not ours: on a catalog image the harness may be installed in a
    different interpreter from the one the venv or addons are built for.
    """
    out: dict = {}
    py = python or os.environ.get("PODH_COMFY_PY") or sys.executable
    try:
        p = subprocess.run([py, "-c", _FACTS_PROBE], capture_output=True, text=True,
                           timeout=timeout)
        m = re.search(r"^PODH_FACTS (.+)$", p.stdout, re.M)
        if m:
            out.update({k: v for k, v in json.loads(m.group(1)).items() if v})
    except Exception:
        pass
    if "python" not in out and not python:
        out["python"] = "%d.%d" % sys.version_info[:2]
    img = os.environ.get("PODH_IMAGE_DIGEST", "").strip()
    if img:
        out["image"] = img
    return out


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or "") or name in (".", ".."):
        raise KitError(f"bad kit name {name!r} (want {NAME_RE.pattern})")
    return name


def _path(name: str) -> str:
    return "/v1/pod/kits/" + urllib.parse.quote(_check_name(name), safe="")


def configured() -> bool:
    return bool(heartbeat._cfg()[0])


def get(name: str, key: str, dest, *, python: Optional[str] = None,
        kit_facts: Optional[dict] = None, deadline: Optional[float] = None) -> bool:
    """Fill `dest` from the kit. True when filled, False when there is no such kit yet."""
    dest = Path(dest)
    f = kit_facts if kit_facts is not None else facts(python)
    st, obj = heartbeat.request("GET", _path(name), query={"key": key, **f})
    if st == 404 or (st == 200 and not (obj or {}).get("url")):
        return False
    if st != 200:
        raise KitError(f"kit {name}: control plane answered {st or 'nothing'} "
                       f"({heartbeat.last_error()})")
    want = (obj.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        raise KitError(f"kit {name}: the control plane gave no sha256 to verify against")
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{dest.name}.kit-", dir=str(dest.parent)))
    try:
        arc = work / "kit.archive"
        fetch.download(obj["url"], arc, size=obj.get("size"), sha256=want, deadline=deadline)
        staged = work / "tree"
        fetch.safe_extract(arc, staged)
        arc.unlink()
        fetch.replace_dir(staged, dest)
    except (fetch.DownloadError, fetch.UnsafeArchive, tarfile.TarError, OSError) as e:
        raise KitError(f"kit {name}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return True


def pack(src, out_dir, fmt: Optional[str] = None) -> tuple:
    """Tar `src`'s contents into out_dir: (path, format). zstd when possible, else gzip."""
    src, out_dir = Path(src), Path(out_dir)
    fmt = fmt or os.environ.get("PODH_KIT_FORMAT") or ("zst" if shutil.which("zstd") else "gz")
    if fmt == "zst":
        out = out_dir / "kit.tar.zst"
        with open(out, "wb") as fo:
            p = subprocess.Popen([shutil.which("zstd") or "zstd", "-q", "-T0", "-c"],
                                 stdin=subprocess.PIPE, stdout=fo)
            with tarfile.open(fileobj=p.stdin, mode="w|") as tf:
                tf.add(str(src), arcname=".")
            p.stdin.close()
            if p.wait() != 0:
                raise KitError(f"zstd failed packing {src}")
        return out, "tar.zst"
    out = out_dir / "kit.tar.gz"
    with tarfile.open(str(out), "w:gz", compresslevel=6) as tf:
        tf.add(str(src), arcname=".")
    return out, "tar.gz"


def put(name: str, key: str, src, *, python: Optional[str] = None,
        kit_facts: Optional[dict] = None, timeout: float = 3600) -> Optional[dict]:
    """Publish `src` as the kit. Returns {sha256, size} when published, None when another
    pod holds the claim. Raises KitError on failure."""
    src = Path(src)
    if not src.is_dir():
        raise KitError(f"kit {name}: {src} is not a directory")
    f = kit_facts if kit_facts is not None else facts(python)
    work = Path(tempfile.mkdtemp(prefix=".podh-kit-"))
    try:
        arc, fmt = pack(src, work)
        sha, size = fetch.sha256_file(arc), arc.stat().st_size
        body = {"key": key, "sha256": sha, "size": size, "format": fmt, **f}
        st, obj = heartbeat.request("POST", _path(name), body)
        if st == 409:
            return None
        if st != 200 or not (obj or {}).get("url"):
            raise KitError(f"kit {name}: claim answered {st or 'nothing'} "
                           f"({heartbeat.last_error() or obj})")
        headers = {"Content-Length": str(size), **(obj.get("headers") or {})}
        with open(arc, "rb") as data:
            req = urllib.request.Request(obj["url"], data=data, method="PUT", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    r.read()
            except Exception as e:
                raise KitError(f"kit {name}: upload failed: {type(e).__name__}: {e}")
        st, _ = heartbeat.request("POST", _path(name) + "/commit",
                                  {"key": key, "sha256": sha, "size": size})
        if st not in (200, 201, 204, 404):
            raise KitError(f"kit {name}: commit answered {st or 'nothing'} "
                           f"({heartbeat.last_error()})")
        return {"sha256": sha, "size": size, "format": fmt}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="podh-kit",
                                 description="fetch or publish a cached tarball (a kit)")
    ap.add_argument("op", choices=("get", "put"))
    ap.add_argument("name")
    ap.add_argument("key", help="the app's part of the key, e.g. its requirements hash")
    ap.add_argument("dir")
    ap.add_argument("--python", help="interpreter the kit is built for (default "
                                     "$PODH_COMFY_PY, else this one)")
    a = ap.parse_args(argv)
    if not configured():
        print("podh-kit: no PODH_CONTROL_URL — kits need the control plane", file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    t0 = time.time()
    try:
        if a.op == "get":
            if not get(a.name, a.key, a.dir, python=a.python):
                print(f"podh-kit: no kit {a.name} for key {a.key} yet")
                return EXIT_ABSENT
            print(f"podh-kit: {a.name} unpacked into {a.dir} in {time.time() - t0:.0f}s")
            return 0
        r = put(a.name, a.key, a.dir, python=a.python)
        if r is None:
            print(f"podh-kit: another pod is building {a.name}; not published")
            return EXIT_CLAIMED
        print(f"podh-kit: published {a.name} ({r['size']} bytes, {r['format']}) "
              f"in {time.time() - t0:.0f}s")
        return 0
    except KitError as e:
        print(f"podh-kit: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
