"""Downloads (resume, refusal, verification, deadline, aria2c), unpacking that refuses unsafe
archives, and kits (get/put/verify, atomic unpack, the CLI's exit codes).

Every URL is a fake on 127.0.0.1; aria2c is a fake executable on PATH. Nothing leaves the
machine.
"""
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


from _fake_control import FakeControl  # noqa: E402
from pod_harness import fetch, heartbeat, kits  # noqa: E402

tmp = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(tmp / "stop")
os.environ.pop("PODH_CONTROL_FINGERPRINT", None)
os.environ["PODH_NO_ARIA2"] = "1"
fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="tok", RUNPOD_POD_ID="pod1")

data = os.urandom(3 * 1024 * 1024 + 17)
sha = hashlib.sha256(data).hexdigest()
fc.blobs["m.bin"] = data

# -- plain, resumed, refused ---------------------------------------------------------------
d = tmp / "dl"
out = fetch.http_download(fc.blob_url("m.bin"), d / "a.bin", size=len(data), sha256=sha)
check("a download lands verified at its final name", out.read_bytes() == data
      and not (d / "a.bin.part").exists())
check("...and the bucket never sees the control token",
      not any(r["headers"].get("x-podh-token") for r in fc.requests if r["path"].startswith("/blob/")))

(d / "b.bin.part").write_bytes(data[:1_000_000])
n = len(fc.blob_gets)
fetch.http_download(fc.blob_url("m.bin"), d / "b.bin", size=len(data), sha256=sha)
check("a .part is resumed with a Range request, not restarted",
      fc.blob_gets[n][2] == "bytes=1000000-" and (d / "b.bin").read_bytes() == data)

fc.truncate["m.bin"] = 500_000
n = len(fc.blob_gets)
fetch.http_download(fc.blob_url("m.bin"), d / "c.bin", size=len(data), sha256=sha)
check("a dropped connection resumes from where it stopped",
      (d / "c.bin").read_bytes() == data and len(fc.blob_gets) - n >= 2
      and fc.blob_gets[n + 1][2] == "bytes=500000-")

fc.ignore_range = True
(d / "e.bin.part").write_bytes(b"garbage" * 100)
fetch.http_download(fc.blob_url("m.bin"), d / "e.bin", size=len(data), sha256=sha)
check("a server that ignores Range (200) restarts the file instead of appending",
      (d / "e.bin").read_bytes() == data)
fc.ignore_range = False

fc.expired.add("old")
(d / "f.bin.part").write_bytes(data[:2000])
try:
    fetch.http_download(fc.blob_url("m.bin", "old"), d / "f.bin", size=len(data))
    check("an expired link raises LinkExpired", False)
except fetch.LinkExpired:
    check("an expired link raises LinkExpired", True)
check("...and keeps the .part for the fresh link", (d / "f.bin.part").stat().st_size == 2000)
fetch.http_download(fc.blob_url("m.bin", "new"), d / "f.bin", size=len(data), sha256=sha)
check("the fresh link finishes it", (d / "f.bin").read_bytes() == data)

try:
    fetch.http_download(fc.blob_url("m.bin"), d / "g.bin", sha256="0" * 64)
    check("a sha256 mismatch is refused", False)
except fetch.DownloadError as e:
    check("a sha256 mismatch is refused", "sha256" in str(e))
check("...and nothing is left at the final name or as .part",
      not (d / "g.bin").exists() and not (d / "g.bin.part").exists())

try:
    fetch.http_download(fc.blob_url("m.bin"), d / "h.bin", size=12)
    check("a size the server contradicts is refused", False)
except fetch.DownloadError:
    check("a size the server contradicts is refused", True)

try:
    fetch.http_download(fc.blob_url("nope"), d / "i.bin")
    check("a 404 is a DownloadError, not LinkExpired", False)
except fetch.LinkExpired:
    check("a 404 is a DownloadError, not LinkExpired", False)
except fetch.DownloadError:
    check("a 404 is a DownloadError, not LinkExpired", True)

try:
    fetch.http_download(fc.blob_url("m.bin"), d / "j.bin", deadline=time.time() - 1)
    check("a passed deadline stops the download", False)
except fetch.DownloadError as e:
    check("a passed deadline stops the download", "timed out" in str(e))

# -- aria2c, when present ---------------------------------------------------------------------
bindir = tmp / "bin"
bindir.mkdir()
aria = bindir / "aria2c"
aria.write_text(f"""#!{sys.executable}
import sys, urllib.request, urllib.error, os
a = sys.argv[1:]
d = a[a.index("-d") + 1]; o = a[a.index("-o") + 1]; url = a[-1]
open(os.path.join(os.environ["ARIA_LOG"]), "a").write(" ".join(a) + "\\n")
try:
    data = urllib.request.urlopen(url).read()
except urllib.error.HTTPError as e:
    print("errorCode=22 status=%d" % e.code); sys.exit(22)
open(os.path.join(d, o), "wb").write(data)
""")
aria.chmod(0o755)
os.environ["ARIA_LOG"] = str(tmp / "aria.log")
os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
os.environ.pop("PODH_NO_ARIA2")
fetch.download(fc.blob_url("m.bin"), d / "k.bin", size=len(data), sha256=sha)
check("aria2c is used when the image has it", (tmp / "aria.log").exists()
      and (d / "k.bin").read_bytes() == data and "--continue=true" in (tmp / "aria.log").read_text())
try:
    fetch.download(fc.blob_url("m.bin", "old"), d / "l.bin")
    check("aria2c's 403 is LinkExpired too", False)
except fetch.LinkExpired:
    check("aria2c's 403 is LinkExpired too", True)
os.environ["PODH_NO_ARIA2"] = "1"
(tmp / "aria.log").unlink()
fetch.download(fc.blob_url("m.bin"), d / "m.bin", size=len(data))
check("PODH_NO_ARIA2=1 falls back to the Python downloader", not (tmp / "aria.log").exists())

# -- safe extraction ---------------------------------------------------------------------------


def tar_with(members, path, mode="w:gz"):
    with tarfile.open(str(path), mode) as tf:
        for name, kind, payload in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(payload)
                ti.mode = 0o755
                tf.addfile(ti, io.BytesIO(payload))
            elif kind == "sym":
                ti.type = tarfile.SYMTYPE
                ti.linkname = payload
                tf.addfile(ti)
            elif kind == "dev":
                ti.type = tarfile.CHRTYPE
                tf.addfile(ti)
    return path


good = tar_with([("pkg/bin/tool", "file", b"#!/bin/sh\necho hi\n"),
                 ("pkg/bin/alias", "sym", "tool")], tmp / "good.tar.gz")
x = fetch.safe_extract(good, tmp / "x1")
check("a clean archive extracts, executable bit kept",
      os.access(x / "pkg/bin/tool", os.X_OK) and os.readlink(x / "pkg/bin/alias") == "tool")
for label, members in [
        ("a ../ member", [("../evil", "file", b"x")]),
        ("an absolute member", [("/tmp/evil", "file", b"x")]),
        ("a symlink out of the tree", [("a/link", "sym", "../../../etc")]),
        ("an absolute symlink", [("link", "sym", "/etc/passwd")]),
        ("a device node", [("dev", "dev", None)])]:
    bad = tar_with(members, tmp / "bad.tar")
    target = tmp / f"x-{len(ok)}"
    try:
        fetch.safe_extract(bad, target)
        check(f"refuses {label}", False)
    except fetch.UnsafeArchive:
        check(f"refuses {label}", not (tmp / "evil").exists())
raw = tmp / "rawtool"
raw.write_bytes(b"\x7fELF not really")
fetch.safe_extract(raw, tmp / "x2")
check("a bare file (not an archive) is copied in by name", (tmp / "x2" / "rawtool").exists())

# -- kits ----------------------------------------------------------------------------------------
store: dict = {}


def kit_get(req):
    k = store.get((req["path"], req["query"].get("key"), req["query"].get("python")))
    if not k:
        return 404, {"detail": "no kit"}
    return 200, {"url": fc.blob_url(k["blob"]), "sha256": k["sha256"], "size": k["size"]}


def kit_claim(req):
    if req["body"]["key"] == "busy":
        return 409, {"detail": "claimed"}
    blob = f"kit{len(store)}-{req['body']['key']}"
    store[("pending", req["path"])] = (blob, req["body"])
    return 200, {"url": f"{fc.url}/put/{blob}"}


def kit_commit(req):
    path = req["path"][: -len("/commit")]
    blob, claim = store.pop(("pending", path))
    got = fc.blobs[blob]
    if hashlib.sha256(got).hexdigest() != req["body"]["sha256"]:
        return 422, {"detail": "hash mismatch"}
    store[(path, claim["key"], claim.get("python"))] = {"blob": blob, **req["body"]}
    return 200, {"ok": True}


for nm in ("venv", "other"):
    fc.routes[("GET", f"/v1/pod/kits/{nm}")] = kit_get
    fc.routes[("POST", f"/v1/pod/kits/{nm}")] = kit_claim
    fc.routes[("POST", f"/v1/pod/kits/{nm}/commit")] = kit_commit

F = {"python": "3.11", "torch": "2.7.1", "cuda": "12.8", "image": "sha256:abc"}
src = tmp / "built"
(src / "lib").mkdir(parents=True)
(src / "lib" / "mod.py").write_text("X = 1\n")
(src / "run").write_text("#!/bin/sh\n")
(src / "run").chmod(0o755)

dest = tmp / "venv"
check("get of a kit nobody built is False", kits.get("venv", "r1", dest, kit_facts=F) is False)
for fmt in (["gz", "zst"] if shutil.which("zstd") else ["gz"]):
    os.environ["PODH_KIT_FORMAT"] = fmt
    r = kits.put("venv", f"r-{fmt}", src, kit_facts=F)
    claim = [b for p, b, _ in fc.calls if p == "/v1/pod/kits/venv"][-1]
    check(f"put ({fmt}) claims with key, sha256, size and the pod's facts",
          r and claim["sha256"] == r["sha256"] and claim["size"] == r["size"]
          and claim["python"] == "3.11" and claim["torch"] == "2.7.1" and claim["image"] == "sha256:abc")
    (dest / "stale").parent.mkdir(parents=True, exist_ok=True)
    (dest / "stale").write_text("old")
    check(f"get ({fmt}) fills the dir", kits.get("venv", f"r-{fmt}", dest, kit_facts=F) is True
          and (dest / "lib" / "mod.py").read_text() == "X = 1\n" and os.access(dest / "run", os.X_OK))
    check(f"...replacing what was there wholesale", not (dest / "stale").exists())
    q = fc.gets("/v1/pod/kits/venv")[-1]["query"]
    check(f"...asking with the key and facts", q["key"] == f"r-{fmt}" and q["cuda"] == "12.8")
os.environ.pop("PODH_KIT_FORMAT")
check("a different python is a different kit",
      kits.get("venv", "r-gz", tmp / "v2", kit_facts={**F, "python": "3.12"}) is False)
check("a claim held by another pod is None, not an error",
      kits.put("venv", "busy", src, kit_facts=F) is None)

# a kit whose bytes do not match its sha256 is refused and the dir is untouched
key = ("/v1/pod/kits/venv", "r-gz", "3.11")
store[key] = {**store[key], "sha256": "1" * 64}
(dest / "keep").write_text("mine")
try:
    kits.get("venv", "r-gz", dest, kit_facts=F)
    check("a kit that fails its sha256 is refused", False)
except kits.KitError as e:
    check("a kit that fails its sha256 is refused", "sha256" in str(e))
check("...and the destination is left exactly as it was", (dest / "keep").read_text() == "mine")
check("...with no temp dirs left beside it",
      not [p for p in dest.parent.iterdir() if p.name.startswith(".venv.kit-")])
try:
    kits.get("../x", "k", dest, kit_facts=F)
    check("a kit name with a path in it is refused", False)
except kits.KitError:
    check("a kit name with a path in it is refused", True)

# CLI
os.environ["PODH_COMFY_PY"] = sys.executable
check("podh-kit get exits 2 when there is no kit", kits.main(["get", "other", "k1", str(tmp / "o")]) == 2)
check("podh-kit put exits 0 when published", kits.main(["put", "other", "k1", str(src)]) == 0)
check("podh-kit get exits 0 and fills the dir",
      kits.main(["get", "other", "k1", str(tmp / "o")]) == 0 and (tmp / "o" / "lib" / "mod.py").exists())
check("podh-kit put exits 4 when another pod holds the claim",
      kits.main(["put", "other", "busy", str(src)]) == 4)
fq = fc.gets("/v1/pod/kits/other")[-1]["query"]
check("the CLI sends this pod's python version as a fact",
      fq.get("python") == "%d.%d" % sys.version_info[:2])
os.environ.pop("PODH_CONTROL_URL")
check("podh-kit exits 3 without a control plane", kits.main(["get", "other", "k1", str(tmp / "o")]) == 3)

fc.close()
print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
