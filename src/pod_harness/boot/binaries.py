"""binaries: pinned builds, installed once per sha and shared through a kit.

Each item is generic — nothing here knows what ffmpeg is:

    {"name": "ffmpeg", "pin": "n8.1", "url": "https://…/ffmpeg-n8.1-linux64-gpl.tar.xz",
     "sha256": "<64 hex>", "env": {"IMAGEIO_FFMPEG_EXE": "{bin}/ffmpeg", "PATH": "{bindir}:$PATH"},
     "archive_path"?: "ffmpeg-*/bin", "bin_names"?: ["ffmpeg", "ffprobe"]}

`pin` is the human name of the build (the server resolves it to url + sha256). Without
`bin_names`, the executables are found: the shallowest `bin/` holding one named like the
binary, and every executable beside it.

The download is verified against sha256 BEFORE it is unpacked; `archive_path` (a glob, since
release tarballs name their top directory after the version) is where in the archive the
`bin_names` are; a bare executable needs neither. They land in
`<boot dir>/bin/<name>-<sha12>/`, published as kit `binary-<name>-<sha16>` (key: the full sha256) so the next pod
unpacks instead of downloading from the release host. `{bin}` and `{bindir}` in `env` both
become that directory, and the result goes into $PODH_WORKLOAD_ENV (with `$VAR` expanded
when it is read), followed by the spec's `workload.env`.

Already installed (the marker names the sha and every binary is there): skipped. That is
the resume path for a parked pod.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .. import fetch, heartbeat, kits
from . import gpu
from .core import NAME_RE, BootFailure, Context, set_env_lines

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MARKER = ".podh-binary.json"


def _validate(it) -> dict:
    if not isinstance(it, dict):
        raise BootFailure(f"binary item is not an object: {it!r}")
    name = it.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or name in (".", ".."):
        raise BootFailure(f"binary name {name!r} is not allowed")
    sha = str(it.get("sha256") or "").lower()
    if not SHA_RE.match(sha):
        raise BootFailure(f"binary {name}: a pinned binary needs a sha256 (got {sha or 'none'})")
    url = it.get("url")
    if not isinstance(url, str) or not re.match(r"^https?://", url):
        raise BootFailure(f"binary {name}: url must be http(s) (the spec had {url!r}; a "
                          f"server resolves pins to url+sha256)")
    bins = it.get("bin_names") or None          # None: discover them under the tree's bin/
    if bins is not None and (not isinstance(bins, list) or not all(
            isinstance(b, str) and NAME_RE.match(b) for b in bins)):
        raise BootFailure(f"binary {name}: bad bin_names {bins!r}")
    env = it.get("env") or {}
    if not isinstance(env, dict):
        raise BootFailure(f"binary {name}: env must be an object")
    return {"name": name, "sha256": sha, "url": url, "bins": bins, "env": env,
            "archive_path": it.get("archive_path")}


def _installed(d: Path, it: dict) -> bool:
    try:
        m = json.loads((d / MARKER).read_text())
    except (OSError, ValueError):
        return False
    bins = it["bins"] or m.get("bins") or [it["name"]]
    return m.get("sha256") == it["sha256"] and all(os.access(d / b, os.X_OK) for b in bins)


def _exe(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _discover(tree: Path, it: dict) -> tuple:
    """(directory, [executables]) to install.

    bin_names given: those, from archive_path (or the top). Absent: the shallowest `bin/`
    directory (under archive_path, if given) holding an executable named like the binary,
    and every executable beside it — a release's bin/ is ffmpeg AND ffprobe. With no such
    bin/, the executable named like the binary wherever it is.
    """
    if it["bins"]:
        return _locate(tree, it["archive_path"]), it["bins"]
    base = _locate(tree, it["archive_path"])
    name = it["name"]
    if base.name == "bin" or it["archive_path"]:
        dirs = [base]
    else:
        dirs = sorted((Path(r) for r, _, _ in os.walk(base) if Path(r).name == "bin"),
                      key=lambda d: (len(d.parts), str(d)))
    for d in dirs:
        if _exe(d / name):
            return d, sorted(f.name for f in d.iterdir() if _exe(f) and not f.is_symlink()
                             and NAME_RE.match(f.name))
    for r, _, files in sorted(os.walk(base), key=lambda x: (len(Path(x[0]).parts), x[0])):
        if name in files and _exe(Path(r) / name):
            return Path(r), [name]
    raise BootFailure(f"{name}: no executable named {name} in the archive"
                      f"{' under ' + it['archive_path'] if it['archive_path'] else ''}")


def _locate(tree: Path, archive_path) -> Path:
    if not archive_path:
        return tree
    hits = sorted(glob.glob(str(tree / archive_path)))
    hits = [h for h in hits if os.path.realpath(h).startswith(os.path.realpath(tree) + os.sep)]
    if not hits:
        raise BootFailure(f"archive_path {archive_path!r} is not in the archive")
    return Path(hits[0])


def install(ctx: Context, it: dict) -> Path:
    d = ctx.boot_dir / "bin" / f"{it['name']}-{it['sha256'][:12]}"
    if not ctx.fresh and _installed(d, it):
        ctx.line(f"{it['name']}: already installed")
        return d
    kit = f"binary-{it['name'][:40]}-{it['sha256'][:16]}"     # names are ≤64; key = full sha
    d.parent.mkdir(parents=True, exist_ok=True)
    if heartbeat._cfg()[0]:
        try:
            if kits.get(kit, it["sha256"], d, kit_facts={}, deadline=ctx.deadline) and _installed(d, it):
                ctx.line(f"{it['name']}: unpacked from kit")
                return d
        except kits.KitError as e:
            ctx.line(f"{it['name']}: kit unusable ({e}); downloading")
    ctx.line(f"{it['name']}: downloading {it['sha256'][:12]}")
    work = Path(tempfile.mkdtemp(prefix=f".{it['name']}-", dir=str(d.parent)))
    try:
        arc = work / "download"
        try:
            fetch.download(it["url"], arc, sha256=it["sha256"], deadline=ctx.deadline)
        except fetch.DownloadError as e:
            raise BootFailure(f"{it['name']}: {e}")
        tree = work / "tree"
        try:
            if fetch.sniff(arc) == "raw":
                tree.mkdir()
                shutil.copy2(arc, tree / (it["bins"] or [it["name"]])[0])
                os.chmod(tree / (it["bins"] or [it["name"]])[0], 0o755)
            else:
                fetch.safe_extract(arc, tree)
        except (fetch.UnsafeArchive, OSError, Exception) as e:
            if isinstance(e, BootFailure):
                raise
            raise BootFailure(f"{it['name']}: cannot unpack: {e}")
        src, bins = _discover(tree, it)
        staged = work / "out"
        staged.mkdir()
        for b in bins:
            s = src / b
            if not s.is_file():
                raise BootFailure(f"{it['name']}: {b} is not in the archive at "
                                  f"{it['archive_path'] or '/'}")
            shutil.copy2(s, staged / b)
            os.chmod(staged / b, 0o755)
        (staged / MARKER).write_text(json.dumps({"name": it["name"], "sha256": it["sha256"],
                                                 "bins": bins}))
        fetch.replace_dir(staged, d)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if heartbeat._cfg()[0]:
        try:
            if kits.put(kit, it["sha256"], d, kit_facts={}) is not None:
                ctx.line(f"{it['name']}: published as a kit")
        except kits.KitError as e:
            ctx.line(f"{it['name']}: could not publish the kit ({e}); not fatal")
    return d


def run(ctx: Context) -> None:
    items = ctx.boot.get("binaries") or []
    if not isinstance(items, list):
        raise BootFailure("boot.binaries must be a list")
    env: dict = {}
    for raw in items:
        it = _validate(raw)
        d = install(ctx, it)
        ctx.bin_dirs.append(d)
        for k, v in it["env"].items():
            env[str(k)] = str(v).replace("{bindir}", str(d)).replace("{bin}", str(d))
    wl_env = (ctx.boot.get("workload") or {}).get("env") or {}
    if not isinstance(wl_env, dict):
        raise BootFailure("boot.workload.env must be an object", stage="binaries")
    env.update({str(k): str(v) for k, v in wl_env.items()})
    set_env_lines(ctx.workload_env, env)
    if not items:
        ctx.line("no binaries requested")
    gpu.probe_nvenc(ctx)
