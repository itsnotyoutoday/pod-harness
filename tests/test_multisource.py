"""One file from several sources at once: the aria2c command, the checksum, the Python fallback's
failover, and which URLs a model entry is fetched from.

Every URL is a fake on 127.0.0.1; aria2c is a fake executable on PATH. Nothing leaves the machine.
"""
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


from _fake_control import FakeControl  # noqa: E402
from pod_harness import fetch, heartbeat  # noqa: E402
from pod_harness.boot import models as bm  # noqa: E402

tmp = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(tmp / "stop")
os.environ.pop("PODH_CONTROL_FINGERPRINT", None)
os.environ["PODH_NO_ARIA2"] = "1"
fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="tok", RUNPOD_POD_ID="pod1")

data = os.urandom(2 * 1024 * 1024 + 5)
sha = hashlib.sha256(data).hexdigest()
fc.blobs["m.bin"] = data
fc.expired.add("old")                      # a link signed "old" is refused with a 403
d = tmp / "out"
d.mkdir()

# -- the Python fallback (no aria2c): mirrors in order, the next one when a download fails ------------
try:
    fetch.download(fc.blob_url("m.bin", "old"), d / "x.bin")
    refused = False
except fetch.LinkExpired:
    refused = True
check("(the fake server refuses an 'old' link)", refused)
fetch.download([fc.blob_url("m.bin", "old"), fc.blob_url("m.bin")], d / "a.bin", size=len(data), sha256=sha)
check("without aria2c a refused first mirror falls through to the second",
      (d / "a.bin").read_bytes() == data)
fetch.download([fc.blob_url("nope.bin"), fc.blob_url("m.bin")], d / "b.bin", size=len(data), sha256=sha)
check("...and so does a missing one", (d / "b.bin").read_bytes() == data)
try:
    fetch.download([fc.blob_url("m.bin", "old"), fc.blob_url("m.bin", "old")], d / "c.bin")
    check("all mirrors refused: LinkExpired", False)
except fetch.LinkExpired:
    check("all mirrors refused: LinkExpired", True)
fetch.download(fc.blob_url("m.bin"), d / "d.bin", size=len(data))
check("a single URL (a string) still works", (d / "d.bin").read_bytes() == data)

# -- aria2c: the command ------------------------------------------------------------------------------
bindir = tmp / "bin"
bindir.mkdir()
aria = bindir / "aria2c"
aria.write_text(f"""#!{sys.executable}
import sys, urllib.request, urllib.error, os
a = sys.argv[1:]
d = a[a.index("-d") + 1]; o = a[a.index("-o") + 1]
urls = [x for x in a if x.startswith("http")]
open(os.environ["ARIA_LOG"], "a").write(" ".join(a) + "\\n")
if os.environ.get("ARIA_EXIT"):
    print("checksum validation failed"); sys.exit(int(os.environ["ARIA_EXIT"]))
for u in urls:                      # what aria2c's mirrors do: the first one that answers serves it
    try:
        body = urllib.request.urlopen(u).read()
    except urllib.error.HTTPError:
        continue
    open(os.path.join(d, o), "wb").write(body)
    sys.exit(0)
print("errorCode=22"); sys.exit(22)
""")
aria.chmod(0o755)
os.environ["ARIA_LOG"] = str(tmp / "aria.log")
os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
os.environ.pop("PODH_NO_ARIA2")


def last_cmd():
    return (tmp / "aria.log").read_text().strip().splitlines()[-1]


u1, u2 = fc.blob_url("m.bin"), fc.blob_url("m.bin")
fetch.download([u1, u2], d / "e.bin", size=len(data), sha256=sha)
c = last_cmd()
check("both mirrors are on one aria2c command, after -o", c.count("http") == 2 and c.index("-o") < c.index("http"))
check("the faster source is preferred as it goes (--uri-selector=adaptive)", "--uri-selector=adaptive" in c)
check("16 connections per source by default, 32 chunks in flight for two sources",
      "--max-connection-per-server=16" in c and "--split=32" in c)
check("aria2c verifies the sha256 itself (--checksum)", f"--checksum=sha-256={sha}" in c)
check("a refused first mirror: the second serves it", (d / "e.bin").read_bytes() == data)
fetch.download(fc.blob_url("m.bin"), d / "f.bin", connections=8)
c = last_cmd()
check("connections= is honoured, and one source gets 8 chunks at once",
      "--max-connection-per-server=8" in c and "--split=8" in c and "--checksum" not in c)
os.environ["PODH_FETCH_ARIA2_ARGS"] = "--min-split-size=4M --disk-cache=64M"
fetch.download(fc.blob_url("m.bin"), d / "x2.bin")
c = last_cmd()
check("PODH_FETCH_ARIA2_ARGS adds flags after the defaults, before the URLs",
      "--disk-cache=64M" in c and c.index("--min-split-size=4M") > c.index("--min-split-size=16M")
      and c.index("--disk-cache=64M") < c.index(" -o "))
os.environ.pop("PODH_FETCH_ARIA2_ARGS")
fetch.download(fc.blob_url("m.bin"), d / "g.bin", headers={"Authorization": "Bearer t"})
check("headers go on the command for a single URL", "--header=Authorization: Bearer t" in last_cmd())
try:
    fetch.download([u1, u2], d / "h.bin", headers={"Authorization": "Bearer t"})
    check("headers with two mirrors are refused (a token for one host must not reach another)", False)
except ValueError:
    check("headers with two mirrors are refused (a token for one host must not reach another)", True)
os.environ["ARIA_EXIT"] = "32"
try:
    fetch.download([u1, u2], d / "i.bin", size=len(data), sha256=sha)
    check("aria2c's checksum failure (exit 32) is a DownloadError, not a corrupt file", False)
except fetch.LinkExpired:
    check("aria2c's checksum failure (exit 32) is a DownloadError, not a corrupt file", False)
except fetch.DownloadError as e:
    check("aria2c's checksum failure (exit 32) is a DownloadError, not a corrupt file",
          "sha256" in str(e) and not (d / "i.bin").exists())
os.environ.pop("ARIA_EXIT")

# -- which URLs a model entry is fetched from ---------------------------------------------------------
store = "https://bucket.example/models/x.safetensors?sig=abc"
pub = "https://huggingface.co/o/r/resolve/abc123/x.safetensors"
os.environ.pop("HF_TOKEN", None)
os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
check("a file with a source and a sha256 comes from the source AND the store, source first",
      bm._mirrors({"url": store, "source": pub, "sha256": sha}) == [pub, store])
check("an hf:// source resolves to its public URL",
      bm._mirrors({"url": store, "source": "hf://o/r/x.safetensors@abc123", "sha256": sha})[0] == pub)
check("without a sha256 nothing proves two copies are the same: the store alone",
      bm._mirrors({"url": store, "source": pub}) == [store])
check("no source: the store alone", bm._mirrors({"url": store, "sha256": sha}) == [store])
check("a source that cannot be resolved is ignored, not fatal",
      bm._mirrors({"url": store, "source": "ftp://nope", "sha256": sha}) == [store])
pub2 = "https://mirror.example/o/r/x.safetensors"
pub3 = "https://other.example/x.safetensors"
check("several sources + the store: every public source first, in order, the store last",
      bm._mirrors({"url": store, "sources": [pub, pub2, pub3], "sha256": sha}) == [pub, pub2, pub3, store])
check("a duplicated source is used once",
      bm._mirrors({"url": store, "sources": [pub, pub, pub2], "sha256": sha}) == [pub, pub2, store])
check("`source` and `sources` together: the list wins (pod-control sends the first as `source` too)",
      bm._mirrors({"url": store, "source": pub, "sources": [pub, pub2], "sha256": sha}) == [pub, pub2, store])
check("several sources without a sha256: the store alone",
      bm._mirrors({"url": store, "sources": [pub, pub2]}) == [store])
check("one unresolvable source in the list is skipped, the rest are used",
      bm._mirrors({"url": store, "sources": ["ftp://nope", pub2], "sha256": sha}) == [pub2, store])
check("the source list of an entry", bm._source_list({"sources": [pub, pub2]}) == [pub, pub2]
      and bm._source_list({"source": pub}) == [pub] and bm._source_list({"url": store}) == [])
os.environ["PODH_SKIP_STORE"] = "1"
check("PODH_SKIP_STORE=1: the public sources alone",
      bm._mirrors({"url": store, "sources": [pub, pub2], "sha256": sha}) == [pub, pub2])
check("PODH_SKIP_STORE=1 with no public source still uses the store",
      bm._mirrors({"url": store, "sha256": sha}) == [store])
os.environ.pop("PODH_SKIP_STORE")
os.environ["HF_TOKEN"] = "hf_secret"
check("a source that needs a token is never mixed with the store's link",
      bm._mirrors({"url": store, "source": "hf://o/r/x.safetensors@abc123", "sha256": sha}) == [store])
check("with HF_TOKEN set, the hf:// source is left out of a list and the public mirrors stay",
      bm._mirrors({"url": store, "sources": ["hf://o/r/x.safetensors@abc123", pub2], "sha256": sha}) == [pub2, store])
os.environ.pop("HF_TOKEN")

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
