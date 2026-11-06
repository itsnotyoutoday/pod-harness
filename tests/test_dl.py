"""The download layer: options and how they layer, the methods, the plan, the safety rules.

Everything runs against fake executables on PATH and 127.0.0.1. Nothing leaves the machine.
"""
import hashlib
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


def raises(fn, exc=Exception, text=""):
    try:
        fn()
    except exc as e:
        return text in str(e)
    return False


from pod_harness import dl, fetch  # noqa: E402
from pod_harness.boot import models as bm  # noqa: E402

tmp = Path(tempfile.mkdtemp())
for k in ("PODH_FETCH_CONNECTIONS", "PODH_FETCH_MIN_SPLIT", "PODH_FETCH_ARIA2_ARGS", "PODH_FETCH_PARALLEL",
          "PODH_NO_ARIA2", "PODH_FETCH_EXEC_ALLOW", "PODH_SKIP_STORE"):
    os.environ.pop(k, None)

print("— options: defaults, layers, checks")
d = dl.normalize_options()
check("defaults: 1 connection per source, 16M chunks, no pre-allocation, adaptive",
      d["connections_per_source"] == 1 and d["min_split"] == "16M" and d["file_allocation"] == "none"
      and d["uri_selector"] == "adaptive" and d["extra_args"] == [])
o = dl.normalize_options({"connections_per_source": 4, "extra_args": ["--a=1"]}, {"connections_per_source": 2})
check("a later layer wins", o["connections_per_source"] == 2)
check("...and a list replaces, it does not append",
      dl.normalize_options({"extra_args": ["--a=1"]}, {"extra_args": ["--b=2"]})["extra_args"] == ["--b=2"])
check("an unknown option is refused (a typo cannot silently do nothing)",
      raises(lambda: dl.normalize_options({"conections_per_source": 2}), dl.OptionError, "unknown download option"))
check("connections above 16 are refused", raises(lambda: dl.normalize_options({"connections_per_source": 17}), dl.OptionError))
check("connections of 0 or a bool are refused",
      raises(lambda: dl.normalize_options({"connections_per_source": 0}), dl.OptionError)
      and raises(lambda: dl.normalize_options({"connections_per_source": True}), dl.OptionError))
check("min_split must look like a size", raises(lambda: dl.normalize_options({"min_split": "huge"}), dl.OptionError)
      and dl.normalize_options({"min_split": "4M"})["min_split"] == "4M")
check("file_allocation / uri_selector are enums",
      raises(lambda: dl.normalize_options({"file_allocation": "magic"}), dl.OptionError)
      and raises(lambda: dl.normalize_options({"uri_selector": "x"}), dl.OptionError))
for bad in ("--dir=/etc", "--out=x", "--on-download-complete=/bin/sh", "--enable-rpc", "--rpc-secret=x", "--checksum=md5=1",
            "--input-file=/etc/passwd", "--log=/tmp/x", "--header=Authorization: x", "--conf-path=/x", "-d/tmp", "plain"):
    check(f"extra_args refuses {bad!r}", raises(lambda b=bad: dl.normalize_options({"extra_args": [b]}), dl.OptionError))
check("extra_args accepts a harmless flag", dl.normalize_options({"extra_args": ["--timeout=30", "--stream-piece-selector=inorder"]})["extra_args"]
      == ["--timeout=30", "--stream-piece-selector=inorder"])
check("exec argv: an allowlisted binary passes, another is refused",
      dl.normalize_options({"argv": ["curl", "-o", "{part}", "{url}"]})["argv"][0] == "curl"
      and raises(lambda: dl.normalize_options({"argv": ["bash", "-c", "x"]}), dl.OptionError, "not an allowed"))
check("exec argv: a path outside /usr and /opt is refused",
      raises(lambda: dl.normalize_options({"argv": ["/tmp/curl", "{url}"]}), dl.OptionError, "bare name"))
os.environ["PODH_FETCH_EXEC_ALLOW"] = "mytool"
check("PODH_FETCH_EXEC_ALLOW adds a binary", dl.normalize_options({"argv": ["mytool", "{url}"]})["argv"][0] == "mytool")
os.environ.pop("PODH_FETCH_EXEC_ALLOW")

print("— the pod's environment is the lowest layer")
os.environ.update(PODH_FETCH_CONNECTIONS="3", PODH_FETCH_MIN_SPLIT="4M", PODH_FETCH_ARIA2_ARGS="--disk-cache=64M --timeout=20")
e = dl.env_layer()
check("env: connections, chunk size, extra flags", e == {"connections_per_source": 3, "min_split": "4M",
                                                          "extra_args": ["--disk-cache=64M", "--timeout=20"]})
check("env is below the job and the file",
      dl.normalize_options(e, {"connections_per_source": 2}, {"min_split": "1M"})["connections_per_source"] == 2
      and dl.normalize_options(e, {"connections_per_source": 2}, {"min_split": "1M"})["min_split"] == "1M")
for k in ("PODH_FETCH_CONNECTIONS", "PODH_FETCH_MIN_SPLIT", "PODH_FETCH_ARIA2_ARGS"):
    os.environ.pop(k)

print("— job settings and per-file plans")
j = dl.job_settings()
check("job defaults: aria2, 2 files at once, largest first, 48-connection budget, sha256, no upload back",
      j["method"] == "aria2" and j["parallel_files"] == 2 and j["order"] == "largest_first"
      and j["max_total_connections"] == 48 and j["verify"] == "sha256" and j["upload_back"] is False)
check("upload_back is off unless the request asks for it (job or file), never from the environment",
      j["upload_back"] is False and dl.job_settings({"upload_back": True})["upload_back"] is True)
os.environ["PODH_UPLOAD_BACK"] = "1"
check("...PODH_UPLOAD_BACK in the environment does nothing", dl.job_settings()["upload_back"] is False)
os.environ.pop("PODH_UPLOAD_BACK")
check("a file's own upload_back must be true or false",
      raises(lambda: dl.plan_for({"path": "a", "upload_back": "yes"}, dl.job_settings()), dl.OptionError))
check("a later layer overrides, options merge",
      dl.job_settings({"parallel_files": 4, "options": {"min_split": "4M"}}, {"options": {"retries": 1}})["options"]
      == {"min_split": "4M", "retries": 1})
check("an unknown fetch setting is refused", raises(lambda: dl.job_settings({"paralel_files": 2}), dl.OptionError, "unknown fetch setting"))
check("a bad method / order / verify / parallel is refused",
      raises(lambda: dl.job_settings({"method": "ftp"}), dl.OptionError)
      and raises(lambda: dl.job_settings({"order": "random"}), dl.OptionError)
      and raises(lambda: dl.job_settings({"verify": "md5"}), dl.OptionError)
      and raises(lambda: dl.job_settings({"parallel_files": 0}), dl.OptionError)
      and raises(lambda: dl.job_settings({"upload_back": "yes"}), dl.OptionError))
check("bad job options are caught when the job is read, not at the first file",
      raises(lambda: dl.job_settings({"options": {"connections_per_source": 99}}), dl.OptionError))
pl = dl.plan_for({"path": "a", "options": {"connections_per_source": 2}, "method": "curl", "group": "nsfw", "priority": 3},
                 dl.job_settings({"options": {"min_split": "4M"}}))
check("a file's own method/options/group/priority override the job",
      pl.method == "curl" and pl.options["connections_per_source"] == 2 and pl.options["min_split"] == "4M"
      and pl.group == "nsfw" and pl.priority == 3)
check("exec without options.argv is refused", raises(lambda: dl.plan_for({"path": "a", "method": "exec"}, dl.job_settings()), dl.OptionError, "argv"))
check("a bad file method / priority is refused",
      raises(lambda: dl.plan_for({"path": "a", "method": "nope"}, dl.job_settings()), dl.OptionError)
      and raises(lambda: dl.plan_for({"path": "a", "priority": "high"}, dl.job_settings()), dl.OptionError))

print("— order, connection budget, disk")
items = {"s": {"size": 1}, "m": {"size": 5}, "b": {"size": 9}, "p": {"size": 2, "priority": -1}}
check("largest first, a lower priority number before it",
      dl.order_paths(items, list(items), dl.job_settings()) == ["p", "b", "m", "s"])
check("smallest first", dl.order_paths(items, list(items), dl.job_settings({"order": "smallest_first"})) == ["p", "s", "m", "b"])
check("given order keeps the order (priority still first)",
      dl.order_paths(items, ["s", "m", "b", "p"], dl.job_settings({"order": "given"})) == ["p", "s", "m", "b"])
check("budget: 4 files x 3 sources x 16 connections is cut to fit 48",
      dl.effective_connections({"connections_per_source": 16}, 3, 4, 48) == 4
      and dl.effective_connections({"connections_per_source": 1}, 3, 4, 48) == 1
      and dl.effective_connections({"connections_per_source": 16}, 3, 2, 480) == 16
      and dl.effective_connections({"connections_per_source": 16}, 20, 16, 48) == 1)
real = dl.shutil.disk_usage


class _U:
    free = 10 * 10 ** 9


dl.shutil.disk_usage = lambda p: _U()
msg = dl.preflight(tmp, {"a": {"size": 31 * 10 ** 9}}, ["a"])
check("preflight: files bigger than the free space are refused with the numbers", msg and "need 31.0 GB" in msg and "container_gb" in msg)
check("preflight: files that fit pass", dl.preflight(tmp, {"a": {"size": 5 * 10 ** 9}}, ["a"]) is None)
(tmp / "x.bin.part").write_bytes(b"0" * 100)
check("preflight: a partial download counts as already there",
      dl.preflight(tmp, {"x.bin": {"size": 100 + 9 * 10 ** 9}}, ["x.bin"]) is None)
dl.shutil.disk_usage = real

print("— the aria2c flags")
o = dl.normalize_options()
f = dl.aria2_flags(o, 3, 1)
check("3 sources x 1 connection: 3 chunks in flight, 1 per server",
      "--max-connection-per-server=1" in f and "--split=3" in f and "--uri-selector=adaptive" in f)
check("min split, retries and allocation come from the options",
      "--min-split-size=16M" in f and "--max-tries=5" in f and "--file-allocation=none" in f)
o2 = dl.normalize_options({"split": 8, "lowest_speed": "1M", "disk_cache": "64M", "file_allocation": "falloc", "extra_args": ["--timeout=9"]})
f2 = dl.aria2_flags(o2, 3, 2, dest_fs="ext4")
check("split, lowest speed, disk cache, falloc and extra flags are passed (extra flags last)",
      "--split=8" in f2 and "--lowest-speed-limit=1M" in f2 and "--disk-cache=64M" in f2
      and "--file-allocation=falloc" in f2 and f2[-1] == "--timeout=9")
check("a FUSE / network disk forces allocation to none (measured: falloc exits 17 there)",
      "--file-allocation=none" in dl.aria2_flags(o2, 3, 2, dest_fs="fuse")
      and "--file-allocation=none" in dl.aria2_flags(o2, 3, 2, dest_fs="nfs4"))

print("— the methods, with fake tools")
bindir = tmp / "bin"
bindir.mkdir()
data = os.urandom(300_000)
sha = hashlib.sha256(data).hexdigest()
(tmp / "good.bin").write_bytes(data)
(tmp / "bad.bin").write_bytes(os.urandom(300_000))
LOG = tmp / "calls.log"


def tool(name, body):
    p = bindir / name
    p.write_text(f"#!{sys.executable}\nimport sys, os, shutil, time\na = sys.argv[1:]\nopen({str(LOG)!r}, 'a').write({name!r} + ' ' + ' '.join(a) + '\\n')\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


tool("aria2c", f'''
d = a[a.index("-d") + 1]; o = a[a.index("-o") + 1]
urls = [x for x in a if x.startswith("http")]
if os.environ.get("FAKE_SLOW"): time.sleep(float(os.environ["FAKE_SLOW"]))
if os.environ.get("FAKE_BAD_MULTI") and len(urls) > 1: sys.exit(32)
src = {str(tmp / "good.bin")!r} if not urls[0].endswith("bad") else {str(tmp / "bad.bin")!r}
shutil.copy(src, os.path.join(d, o))
''')
tool("curl", f'''
o = a[a.index("-o") + 1]
shutil.copy({str(tmp / "good.bin")!r}, o)
''')
os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
logtxt = lambda: LOG.read_text() if LOG.exists() else ""
u1, u2, u3 = "http://127.0.0.1:9/a", "http://127.0.0.1:9/b", "http://127.0.0.1:9/c"
say = []

plan = dl.plan_for({"path": "m/a.bin"}, dl.job_settings())
st = dl.download([u1, u2, u3], tmp / "o1" / "a.bin", plan, size=len(data), sha256=sha, log=say.append)
last = logtxt().strip().splitlines()[-1]
check("aria2: a verified file, stats say method / sources / connections / MB/s",
      (tmp / "o1" / "a.bin").read_bytes() == data and st["method"] == "aria2" and st["sources"] == 3
      and st["connections"] == 1 and st["bytes"] == len(data) and "mbps" in st)
check("...the command has the three URLs after -o and the sha256 check", last.count("http://") == 3
      and f"--checksum=sha-256={sha}" in last and last.index(" -o ") < last.index("http://"))
check("...and no -x16 / 16-per-host default any more", "--max-connection-per-server=1 " in last + " ")

plan = dl.plan_for({"path": "m/b.bin"}, dl.job_settings({"options": {"connections_per_source": 16}, "max_total_connections": 12, "parallel_files": 4}))
say.clear()
st = dl.download([u1, u2, u3], tmp / "o2" / "b.bin", plan, size=len(data), sha256=sha, parallel_files=4, budget=12, log=say.append)
check("the connection budget cuts 16 to what fits, and says so", st["connections"] == 1 and any("budget" in x for x in say))

plan = dl.plan_for({"path": "m/c.bin", "method": "curl"}, dl.job_settings())
st = dl.download([u1], tmp / "o3" / "c.bin", plan, size=len(data), sha256=sha)
check("method curl runs curl and verifies the file", st["method"] == "curl" and (tmp / "o3" / "c.bin").read_bytes() == data)

plan = dl.plan_for({"path": "m/d.bin", "method": "exec", "options": {"argv": ["curl", "-o", "{part}", "{url}"]}}, dl.job_settings())
st = dl.download([u1], tmp / "o4" / "d.bin", plan, size=len(data), sha256=sha)
check("method exec runs the allowlisted tool from the template, then verifies and renames",
      st["method"] == "exec" and (tmp / "o4" / "d.bin").read_bytes() == data and "curl -o " in logtxt())

plan = dl.plan_for({"path": "m/e.bin", "method": "exec", "options": {"argv": ["curl", "{urls}", "-o", "{part}"]}}, dl.job_settings())
dl.download([u1, u2], tmp / "o5" / "e.bin", plan, size=len(data), sha256=sha)
check("{urls} expands to every URL", logtxt().strip().splitlines()[-1].count("http://") == 2)

os.environ["PODH_NO_ARIA2"] = "1"
plan = dl.plan_for({"path": "m/f.bin"}, dl.job_settings())
say.clear()
try:
    dl.download([u1], tmp / "o6" / "f.bin", plan, size=len(data), sha256=sha, log=say.append)
except fetch.DownloadError:
    pass
check("without aria2c the Python downloader is used, and the log says so", any("Python downloader" in x for x in say))
os.environ.pop("PODH_NO_ARIA2")

plan = dl.plan_for({"path": "m/g.bin", "verify": "none"}, dl.job_settings())
dl.download([u1], tmp / "o7" / "g.bin", plan, size=len(data), sha256="0" * 64)
check("verify none: a wrong sha256 is not checked", (tmp / "o7" / "g.bin").exists())

print("— a mismatching mirror is found")
os.environ["FAKE_BAD_MULTI"] = "1"
plan = dl.plan_for({"path": "m/h.bin"}, dl.job_settings())
say.clear()
st = dl.download([u1, u2, u3], tmp / "o8" / "h.bin", plan, size=len(data), sha256=sha, log=say.append)
check("the sources together failed their checksum; one alone passed, flagged `isolated`",
      st["isolated"] and (tmp / "o8" / "h.bin").read_bytes() == data and any("failed the checksum" in x for x in say))
os.environ.pop("FAKE_BAD_MULTI")
tool("aria2c", 'sys.exit(32)')
plan = dl.plan_for({"path": "m/i.bin"}, dl.job_settings())
check("if no source alone passes either, it is an error, and no file is left",
      raises(lambda: dl.download([u1, u2], tmp / "o9" / "i.bin", plan, size=len(data), sha256=sha), fetch.DownloadError, "no source")
      and not (tmp / "o9" / "i.bin").exists())

print("— progress, so a long download is never silent")
tool("aria2c", f'''
d = a[a.index("-d") + 1]; o = a[a.index("-o") + 1]
open(os.path.join(d, o), "wb").write(b"x" * 400000)
time.sleep(0.8)
shutil.copy({str(tmp / "good.bin")!r}, os.path.join(d, o))
''')
ticks = []
plan = dl.plan_for({"path": "m/j.bin"}, dl.job_settings())
dl.download([u1], tmp / "o10" / "j.bin", plan, size=len(data), sha256=sha, interval=0.2, progress=lambda n: ticks.append(n))
check("progress is reported while the downloader runs", len(ticks) >= 2 and max(ticks) > 0)
lines = []
pg = dl.Progress("m/j.bin", 9_400_000_000, lines.append)
pg(4_100_000_000)
check("the progress line shows path, GB done/total, percent and MB/s",
      lines and "m/j.bin: 4.1/9.4 GB (43%)" in lines[0] and "MB/s" in lines[0])
tool("aria2c", "time.sleep(30)")
t0 = time.time()
check("the deadline stops a hung downloader", raises(lambda: dl.download(
    [u1], tmp / "o11" / "k.bin", plan, size=len(data), sha256=sha, deadline=time.time() + 1, interval=0.2),
    fetch.DownloadError, "timed out") and time.time() - t0 < 10)

print("— sources: the store marker, tokens, ordering")
store = "https://bucket.example/m/x.safetensors?sig=abc"
hf = "https://huggingface.co/o/r/resolve/rev/x.safetensors"
mir = "https://mirror.example/o/r/x.safetensors"
m = {"url": store, "sha256": sha, "sources": [hf, {"store": True}, mir]}
check("{\"store\": true} puts the store's link where it is listed", bm._mirrors(m) == [hf, store, mir])
check("with no marker the store's link goes last", bm._mirrors({"url": store, "sha256": sha, "sources": [hf, mir]}) == [hf, mir, store])
check("the marker only counts with a sha256 (nothing proves the copies equal)", bm._mirrors({"url": store, "sources": [hf, {"store": True}]}) == [store])
os.environ["MY_TOKEN"] = "t"
check("a {url, auth_env} source is a token source: never mixed into the shared list",
      bm._mirrors({"url": store, "sha256": sha, "sources": [{"url": hf, "auth_env": "MY_TOKEN"}, mir]}) == [mir, store])
check("...and resolves with its Bearer header", bm._resolve({"url": hf, "auth_env": "MY_TOKEN"}) == (hf, {"Authorization": "Bearer t"}))
os.environ.pop("MY_TOKEN")
check("...a missing token variable is an error that names it",
      raises(lambda: bm._resolve({"url": hf, "auth_env": "NOT_SET_ANYWHERE"}), ValueError, "NOT_SET_ANYWHERE"))
check("a junk source entry is refused", raises(lambda: bm._resolve({"nope": 1}), ValueError))
check("the store marker resolves to None", bm._resolve({"store": True}) is None)
check("20 sources are kept in order, once each", bm._mirrors({"url": store, "sha256": sha, "sources": [f"https://m{i}.example/x" for i in range(20)] + [f"https://m{i}.example/x" for i in range(5)]})[:20]
      == [f"https://m{i}.example/x" for i in range(20)])

print("— a dry run")
spec = {"fetch": {"parallel_files": 4, "options": {"connections_per_source": 2}},
        "files": [{"path": "a", "sources": ["https://x/a", "https://y/a"], "url": "https://s/a"}, {"path": "b", "method": "curl", "source": "https://x/b"}]}
job = dl.job_settings(spec["fetch"])
exp = dl.explain({f["path"]: f for f in spec["files"]}, job)
check("explain: resolved method / options / source count per file, no network",
      exp[0]["method"] == "aria2" and exp[0]["options"]["connections_per_source"] == 2 and exp[0]["sources"] == 3
      and exp[1]["method"] == "curl" and exp[1]["sources"] == 1)

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
