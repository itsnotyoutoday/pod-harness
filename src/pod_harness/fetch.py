"""Downloading big files onto a pod, and unpacking archives without trusting them.

## Why not just urlretrieve

The files are model weights and tarballs of several gigabytes, fetched from presigned
bucket URLs over links that drop. Three things go wrong, and each has cost a boot:

  - a dropped connection restarts a 20 GB file from zero. So a download writes to
    `<name>.part` and resumes it with a Range request, and only a finished, verified file
    takes the real name — a file at its final path is a complete one.
  - a presigned link expires mid-boot (they are valid for hours; a slow host takes longer).
    That is a 401/403 from the bucket, raised here as `LinkExpired` so the caller can ask
    the control plane for fresh links and carry on from the .part — not a failure.
  - a server can ignore Range and answer 200 with the whole body; appending that to the
    .part would corrupt it silently. A 200 to a ranged request restarts the file.

`aria2c`, when the image has it, is used instead (several connections per file is much
faster from a bucket); it is never installed here. The same .part / verify / rename rules
apply, and the same LinkExpired classification of its failures.

The pod's control token is never sent with a download: these URLs are a bucket's, and a
bucket has no business seeing the token.

## Why unpacking checks every member

An archive names its own paths. `../../etc/profile` or a symlink pointing at `/` is legal
in a tarball, and tarfile on the Pythons this runs on extracts it. Every member is checked
against the destination before it is written: no absolute names, no `..`, links that stay
inside, no devices. One bad member refuses the whole archive.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.client import HTTPException, IncompleteRead
from pathlib import Path
from typing import Callable, Optional

#: Same agent as the control client: Cloudflare-fronted endpoints answer urllib's default
#: one with a 403 that reads like an expired link and is not.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
CHUNK = 1 << 20


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect to ANOTHER host loses the Authorization header. A model source (Hugging
    Face, Civitai) answers with a redirect to a CDN or a presigned bucket URL: the app's
    token must not follow it there, and a bucket refuses a request that carries two kinds
    of authorisation."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).netloc
            if urllib.parse.urlsplit(newurl).netloc != old_host:
                for k in [k for k in new.headers if k.lower() == "authorization"]:
                    del new.headers[k]
                new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_DropAuthOnRedirect)


def _urlopen(req, timeout):
    return _OPENER.open(req, timeout=timeout)


class DownloadError(Exception):
    """The file could not be fetched (and asking for a new link will not help)."""


class LinkExpired(DownloadError):
    """The URL was refused (401/403): ask for a fresh one and resume."""


class UnsafeArchive(Exception):
    """An archive member would land outside the destination, or is not a plain file."""


def sha256_file(path, chunk: int = CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _remaining(deadline: Optional[float]) -> Optional[float]:
    return None if deadline is None else deadline - time.time()


def _finish(part: Path, dest: Path, size: Optional[int], sha256: Optional[str]) -> Path:
    got = part.stat().st_size
    if size is not None and got != int(size):
        part.unlink()
        raise DownloadError(f"{dest.name}: size {got} != expected {size}")
    if sha256:
        digest = sha256_file(part)
        if digest != sha256.lower():
            part.unlink()
            raise DownloadError(f"{dest.name}: sha256 {digest[:16]}… != expected "
                                f"{sha256.lower()[:16]}…")
    os.replace(part, dest)
    return dest


def http_download(url: str, dest, *, size: Optional[int] = None,
                  sha256: Optional[str] = None, deadline: Optional[float] = None,
                  headers: Optional[dict] = None, read_timeout: float = 60,
                  retries: int = 5, progress: Optional[Callable] = None) -> Path:
    """Fetch `url` to `dest`, resuming `<dest>.part`. Returns dest when complete and verified.

    Raises LinkExpired on 401/403 (the .part is kept for the next URL), DownloadError on
    anything else that will not fix itself, including the overall `deadline` passing.
    Dropped connections are retried from where they stopped, up to `retries` in a row
    without progress.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    stalls = 0
    restarted = False
    while True:
        left = _remaining(deadline)
        if left is not None and left <= 0:
            raise DownloadError(f"{dest.name}: timed out (partial download kept)")
        offset = part.stat().st_size if part.exists() else 0
        if size is not None and offset > int(size):
            part.unlink()
            offset = 0
        if size is not None and offset == int(size) and offset > 0:
            return _finish(part, dest, size, sha256)
        h = {"User-Agent": _UA, **(headers or {})}
        if offset:
            h["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(url, headers=h)
        timeout = read_timeout if left is None else max(1.0, min(read_timeout, left))
        try:
            resp = _urlopen(req, timeout)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise LinkExpired(f"{dest.name}: HTTP {e.code} (link expired or refused)")
            if e.code == 416 and offset and not restarted:
                # The server says there is nothing after `offset`. With no size to compare,
                # that is either complete or a .part from some other file; start over once.
                if size is None:
                    part.unlink()
                    restarted = True
                    continue
            raise DownloadError(f"{dest.name}: HTTP {e.code} {e.reason}")
        except (urllib.error.URLError, socket.timeout, ConnectionError, HTTPException) as e:
            stalls += 1
            if stalls > retries:
                raise DownloadError(f"{dest.name}: {type(e).__name__}: {e}")
            time.sleep(min(2 ** stalls, 10) * 0.1)
            continue
        with resp:
            mode = "ab"
            if offset and resp.status == 200:
                mode = "wb"          # Range ignored: the body is the whole file
                offset = 0
            total = None
            cr = resp.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)\s*$", cr)
            if m:
                total = int(m.group(1))
            elif resp.headers.get("Content-Length") is not None:
                total = offset + int(resp.headers["Content-Length"])
            if size is not None and total is not None and total != int(size):
                raise DownloadError(f"{dest.name}: server has {total} bytes, expected {size}")
            got = 0
            try:
                with open(part, mode) as f:
                    while True:
                        left = _remaining(deadline)
                        if left is not None and left <= 0:
                            raise DownloadError(f"{dest.name}: timed out (partial download kept)")
                        b = resp.read(CHUNK)
                        if not b:
                            break
                        f.write(b)
                        got += len(b)
                        if progress:
                            progress(offset + got, total)
            except (IncompleteRead, socket.timeout, ConnectionError, HTTPException) as e:
                stalls = 0 if got else stalls + 1
                if stalls > retries:
                    raise DownloadError(f"{dest.name}: {type(e).__name__}: {e}")
                continue
            have = part.stat().st_size if part.exists() else 0
            if total is not None and have < total:
                stalls = 0 if got else stalls + 1
                if stalls > retries:
                    raise DownloadError(f"{dest.name}: stream ended at {have}/{total}")
                continue
            if not part.exists():
                part.touch()          # an empty file is still a file
            return _finish(part, dest, size, sha256)


def aria2c_download(url: str, dest, *, size: Optional[int] = None,
                    sha256: Optional[str] = None, deadline: Optional[float] = None,
                    exe: str = "aria2c", connections: int = 4) -> Path:
    """The same contract as http_download, through aria2c (several connections, resume)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    left = _remaining(deadline)
    if left is not None and left <= 0:
        raise DownloadError(f"{dest.name}: timed out (partial download kept)")
    cmd = [exe, "--continue=true", "--allow-overwrite=true", "--auto-file-renaming=false",
           f"--max-connection-per-server={connections}", f"--split={connections}",
           "--min-split-size=16M", "--file-allocation=none", "--summary-interval=0",
           "--console-log-level=warn", f"--user-agent={_UA}",
           "-d", str(dest.parent), "-o", part.name, url]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           timeout=None if left is None else max(1.0, left))
    except subprocess.TimeoutExpired:
        raise DownloadError(f"{dest.name}: timed out (partial download kept)")
    if p.returncode != 0:
        out = p.stdout or ""
        if p.returncode == 24 or re.search(r"\b(401|403)\b|expired|AccessDenied", out):
            raise LinkExpired(f"{dest.name}: aria2c: link refused (exit {p.returncode})")
        tail = " ".join(out.strip().splitlines()[-2:])[:300]
        raise DownloadError(f"{dest.name}: aria2c exit {p.returncode}: {tail}")
    ctl = dest.with_name(part.name + ".aria2")
    if ctl.exists():
        ctl.unlink()
    return _finish(part, dest, size, sha256)


def put_file(url: str, path, timeout: float = 600) -> int:
    """PUT a file to a presigned URL (no pod token, one stream). Returns the HTTP status;
    raises on no answer."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="PUT",
                                     headers={"Content-Length": str(size),
                                              "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


# -- model sources (TASK-API §10: a pod's fallback when the store lacks a file) ----------------

HF_BASE = "https://huggingface.co"
CIVITAI_BASE = "https://civitai.com"


def resolve_source(source: str) -> tuple:
    """(url, headers) for a model's `source`:

        https://…                          as is (http:// too)
        hf://<owner>/<repo>/<file…>@<rev>  → {HF_BASE}/<owner>/<repo>/resolve/<rev>/<file…>
                                             (rev defaults to main); Bearer $HF_TOKEN if set
        civitai://<model-version-id>       → {CIVITAI_BASE}/api/download/models/<id>;
                                             Bearer $CIVITAI_TOKEN if set

    Raises ValueError for anything else.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is empty")
    src = source.strip()
    if src.startswith(("https://", "http://")):
        return src, {}
    if src.startswith("hf://"):
        body, rev = src[len("hf://"):], "main"
        if "@" in body:
            body, rev = body.rsplit("@", 1)
        parts = [p for p in body.split("/") if p]
        if len(parts) < 3 or not rev or any(p in (".", "..") for p in parts):
            raise ValueError(f"source {source!r}: hf:// needs <owner>/<repo>/<file>[@<rev>]")
        repo, file = "/".join(parts[:2]), "/".join(parts[2:])
        url = (f"{HF_BASE}/{repo}/resolve/{urllib.parse.quote(rev, safe='')}/"
               f"{urllib.parse.quote(file)}")
        tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        return url, ({"Authorization": f"Bearer {tok}"} if tok else {})
    if src.startswith("civitai://"):
        vid = src[len("civitai://"):].strip("/")
        if not vid.isdigit():
            raise ValueError(f"source {source!r}: civitai:// needs a numeric model version id")
        tok = os.environ.get("CIVITAI_TOKEN")
        return (f"{CIVITAI_BASE}/api/download/models/{vid}",
                {"Authorization": f"Bearer {tok}"} if tok else {})
    raise ValueError(f"source {source!r}: not https://, hf:// or civitai://")


def download(url: str, dest, *, size: Optional[int] = None, sha256: Optional[str] = None,
             deadline: Optional[float] = None, use_aria2: Optional[bool] = None) -> Path:
    """aria2c when present (unless use_aria2=False or PODH_NO_ARIA2=1), else Python."""
    if use_aria2 is None:
        use_aria2 = os.environ.get("PODH_NO_ARIA2") != "1"
    exe = shutil.which("aria2c") if use_aria2 else None
    if exe:
        return aria2c_download(url, dest, size=size, sha256=sha256, deadline=deadline, exe=exe)
    return http_download(url, dest, size=size, sha256=sha256, deadline=deadline)


# -- unpacking ------------------------------------------------------------------------------

def sniff(path) -> str:
    """'gz' | 'xz' | 'bz2' | 'zst' | 'zip' | 'tar' | 'raw' from the first bytes."""
    with open(path, "rb") as f:
        head = f.read(512)
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zst"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return "raw"


def _inside(root: Path, rel: str) -> bool:
    target = os.path.normpath(os.path.join(str(root), rel))
    return target == str(root) or target.startswith(str(root) + os.sep)


def _check_member(root: Path, name: str, *, link: Optional[str] = None,
                  hard: bool = False) -> None:
    if not name or os.path.isabs(name) or name.startswith(("/", "\\")):
        raise UnsafeArchive(f"absolute member path: {name!r}")
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        raise UnsafeArchive(f"member escapes the destination: {name!r}")
    if not _inside(root, name):
        raise UnsafeArchive(f"member escapes the destination: {name!r}")
    if link is not None:
        if os.path.isabs(link):
            raise UnsafeArchive(f"absolute link target: {name!r} -> {link!r}")
        base = "" if hard else os.path.dirname(name)
        if not _inside(root, os.path.join(base, link)):
            raise UnsafeArchive(f"link escapes the destination: {name!r} -> {link!r}")


def _zstd_reader(path):
    """A readable stream of the decompressed bytes, or raise UnsafeArchive if impossible."""
    try:
        import zstandard  # type: ignore
        return zstandard.ZstdDecompressor().stream_reader(open(path, "rb")), None
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if not exe:
        raise UnsafeArchive("archive is zstd-compressed and neither the zstandard module "
                            "nor the zstd binary is available")
    p = subprocess.Popen([exe, "-dcq", str(path)], stdout=subprocess.PIPE)
    return p.stdout, p


def safe_extract(archive, dest) -> Path:
    """Extract a tar (any common compression) or zip into `dest`, checking every member.

    A 'raw' file (not an archive) is copied in under its own name — a pinned binary is
    sometimes a bare executable.
    """
    archive, dest = Path(archive), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    kind = sniff(archive)
    if kind == "raw":
        shutil.copy2(archive, dest / archive.name)
        return dest
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            for i in infos:
                _check_member(root, i.filename.rstrip("/") or ".")
                if stat.S_ISLNK(i.external_attr >> 16):
                    raise UnsafeArchive(f"symlink in zip refused: {i.filename!r}")
            for i in infos:
                out = z.extract(i, str(root))
                mode = (i.external_attr >> 16) & 0o777
                if mode and not i.is_dir():
                    os.chmod(out, mode)
        return dest
    proc = None
    if kind == "zst":
        fileobj, proc = _zstd_reader(archive)
        tf = tarfile.open(fileobj=fileobj, mode="r|")
    else:
        tf = tarfile.open(str(archive), mode="r|*")
    kw = {"filter": "fully_trusted"} if hasattr(tarfile, "data_filter") else {}
    try:
        for m in tf:
            if m.name in (".", "./"):
                continue
            if m.isdev() or m.isfifo():
                raise UnsafeArchive(f"device or fifo in archive: {m.name!r}")
            _check_member(root, m.name,
                          link=m.linkname if (m.issym() or m.islnk()) else None,
                          hard=m.islnk())
            m.mode &= 0o777               # no setuid/setgid from an archive
            tf.extract(m, str(root), **kw)
    finally:
        tf.close()
        if proc is not None:
            proc.stdout.close()
            proc.wait()
    return dest


def replace_dir(staged, dest) -> Path:
    """Move a fully prepared `staged` directory to `dest`, replacing what was there.

    Two renames on one filesystem: `dest` is either the old tree or the new one, never a
    half-unpacked mix — so a pod killed mid-unpack resumes from a clean state.
    """
    staged, dest = Path(staged), Path(dest)
    old = None
    if dest.exists() or dest.is_symlink():
        old = dest.with_name(f".{dest.name}.old-{os.getpid()}-{int(time.time() * 1000)}")
        os.replace(dest, old)
    os.replace(staged, dest)
    if old is not None:
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink()
    return dest
