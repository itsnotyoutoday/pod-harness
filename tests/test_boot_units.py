"""podh-boot's decision logic, in-process: addon validation, link-escape refusal, the
workload env file, path containment, the stage runner's limits and failure translation,
binary item validation, the log scan's verdicts, and spec loading.

No network, no subprocess beyond `true`/`sleep`.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


for k in list(os.environ):
    if k.startswith("PODH_") and k not in ("PODH_HEARTBEAT_STOP_FILE",):
        os.environ.pop(k)
from pod_harness.boot import addons, binaries, core, workload  # noqa: E402
from pod_harness.boot.core import BootFailure, Context  # noqa: E402

tmp = Path(tempfile.mkdtemp())
os.environ["PODH_MODELS_ROOT"] = str(tmp / "models")
(tmp / "models" / "checkpoints").mkdir(parents=True)

# -- addon validation ------------------------------------------------------------------------
C = "a" * 40
good = {"name": "Some-Node.Pack_1", "repo": "https://github.com/owner/repo", "commit": C}
check("a well-formed item validates", addons.validate(good) == [])
check("...with .git on the repo too", addons.validate(dict(good, repo="https://github.com/o/r.git")) == [])
cases = {
    "a name with a slash": dict(good, name="a/b"),
    "a name of '..'": dict(good, name=".."),
    "a name starting with a dot": dict(good, name=".hidden"),
    "a 65-char name": dict(good, name="a" * 65),
    "http (not https)": dict(good, repo="http://github.com/o/r"),
    "a host that is not GitHub": dict(good, repo="https://gitlab.com/o/r"),
    "a lookalike host": dict(good, repo="https://github.com.evil.io/o/r"),
    "a path with ..": dict(good, repo="https://github.com/o/../r"),
    "a short commit": dict(good, commit="abc1234"),
    "an uppercase commit": dict(good, commit="A" * 40),
    "a branch name": dict(good, commit="main"),
    "a pip option": dict(good, pip=["--index-url=https://evil/simple", "x"]),
    "a pip short option": dict(good, pip=["-e", "."]),
    "an absolute link file": dict(good, links=[{"file": "/etc/passwd", "to": "x"}]),
    "a link file with ..": dict(good, links=[{"file": "../secrets", "to": "x"}]),
    "a link to with ..": dict(good, links=[{"file": "a", "to": "../../outside"}]),
    "a comfy: link with ..": dict(good, links=[{"file": "a", "to": "comfy:../x"}]),
    "an absolute comfy: link": dict(good, links=[{"file": "a", "to": "comfy:/etc"}]),
    "a non-string probe": dict(good, probe=["import x"]),
}
for label, item in cases.items():
    check(f"refuses {label}", addons.validate(item) != [])

# -- links stay inside their roots, symlinked parents included -------------------------------
ctx = Context(boot_dir=tmp / "bd")
ctx.comfy_root = tmp / "comfy"
(ctx.comfy_root / "models").mkdir(parents=True)
addon = tmp / "comfy" / "custom_nodes" / "pack"
addon.mkdir(parents=True)
src, dst = addons.link_paths(ctx, addon, {"file": "checkpoints/x.safetensors", "to": "models/x"})
check("a link's source is under the models root, its target inside the addon",
      src == tmp / "models" / "checkpoints" / "x.safetensors" and dst == addon / "models" / "x")
src, dst = addons.link_paths(ctx, addon, {"file": "checkpoints", "to": "comfy:models/ckpt"})
check("comfy:<path> lands inside the ComfyUI root", dst == ctx.comfy_root / "models" / "ckpt")
(addon / "escape").symlink_to("/tmp")
try:
    addons.link_paths(ctx, addon, {"file": "checkpoints", "to": "escape/x"})
    check("a target under a symlink that leaves the addon dir is refused", False)
except ValueError:
    check("a target under a symlink that leaves the addon dir is refused", True)
(tmp / "models" / "out").symlink_to("/etc")
try:
    addons.link_paths(ctx, addon, {"file": "out/passwd", "to": "x"})
    check("a source under a symlink that leaves the models root is refused", False)
except ValueError:
    check("a source under a symlink that leaves the models root is refused", True)
ctx2 = Context(boot_dir=tmp / "bd2")
ctx2.comfy_root = None
try:
    addons.link_paths(ctx2, addon, {"file": "checkpoints", "to": "comfy:models/x"})
    check("comfy: without a ComfyUI root is refused", False)
except ValueError:
    check("comfy: without a ComfyUI root is refused", True)
s, d = addons.link_paths(ctx, addon, {"file": "checkpoints", "to": "models/ck"})
addons.make_link(s, d)
addons.make_link(s, d)
check("making a link twice replaces it", os.readlink(d) == str(s))
(addon / "real").mkdir()
(addon / "real" / "keep.txt").write_text("x")
try:
    addons.make_link(s, addon / "real")
    check("a real, non-empty directory is never replaced by a link", False)
except ValueError:
    check("a real, non-empty directory is never replaced by a link", (addon / "real" / "keep.txt").exists())
check("an addon kit omits absolute links (an unpacking pod must refuse them)",
      addons._no_abs_links(str(addon / "models"), ["ck"]) == ["ck"])
# -- baked addons: only the exact checkout the image recorded ---------------------------------
bk = tmp / "baked"
bk.mkdir()
bdir = tmp / "nodes" / "Pack"
check("nothing is baked without PODH_BAKED_ROOT", addons.baked({"name": "Pack", "commit": C}, bdir) is False)
os.environ["PODH_BAKED_ROOT"] = str(bk)
check("...nor with no addons.json", addons.baked({"name": "Pack", "commit": C}, bdir) is False)
(bk / "addons.json").write_text('{"addons": {"Pack": {"commit": "%s", "dir": "%s"}}}' % (C, bdir))
check("a baked addon is recognised at its commit and directory",
      addons.baked({"name": "Pack", "commit": C}, bdir) is True)
check("...not at another commit", addons.baked({"name": "Pack", "commit": "b" * 40}, bdir) is False)
check("...not in another directory",
      addons.baked({"name": "Pack", "commit": C}, tmp / "elsewhere" / "Pack") is False)
(bk / "addons.json").write_text("not json")
check("a broken addons.json bakes nothing", addons.baked({"name": "Pack", "commit": C}, bdir) is False)
os.environ.pop("PODH_BAKED_ROOT")

check("kit names fit the server's 64", len(addons._kit_name("lane/" + "x" * 100)) <= 64
      and addons._kit_name("media/comfy") == "addons-media-comfy")

# -- the workload env file -----------------------------------------------------------------------
ef = tmp / "workload.env"
core.set_env_lines(ef, {"IMAGEIO_FFMPEG_EXE": "/b/ffmpeg", "PATH": "/b:$PATH"})
with open(ef, "a") as f:
    f.write("# a comment\nexport APP_X='quoted value'\nAPP_Y=${APP_X}/sub\n")
core.set_env_lines(ef, {"PATH": "/c:$PATH", "WL": "1"})
pairs = core.read_env_file(ef)
check("upsert replaces a key in place and keeps foreign lines",
      [k for k, _ in pairs] == ["IMAGEIO_FFMPEG_EXE", "PATH", "APP_X", "APP_Y", "WL"])
env = core.apply_env(pairs, {"PATH": "/usr/bin"})
check("$PATH expands against the env so far", env["PATH"] == "/c:/usr/bin")
check("quotes and `export` are understood, later lines see earlier ones",
      env["APP_X"] == "quoted value" and env["APP_Y"] == "quoted value/sub")
check("an unknown $VAR expands to empty, as in a shell",
      core.apply_env([("A", "x$NOPE_NOT_SET")], {})["A"] == "x")

# -- containment ------------------------------------------------------------------------------------
for bad in ("/abs", "../up", "a/../../b", "", ".", "a/\x00b"):
    try:
        core.safe_rel(bad)
        check(f"safe_rel refuses {bad!r}", False)
    except ValueError:
        check(f"safe_rel refuses {bad!r}", True)
check("safe_rel normalises a plain path", core.safe_rel("a/./b") == "a/b")

# -- binary items ------------------------------------------------------------------------------------
it = {"name": "ffmpeg", "url": "https://example.invalid/f.tar.xz", "sha256": "ab" * 32}
check("a binary item validates; without bin_names they are found later",
      binaries._validate(it)["bins"] is None
      and binaries._validate(dict(it, pin="n8.1"))["name"] == "ffmpeg")
for label, bad in {"no sha256": dict(it, sha256=None), "a short sha": dict(it, sha256="ab12"),
                   "a pin with no url": {"name": "ffmpeg", "pin": "n8.1", "sha256": "ab" * 32},
                   "a file url": dict(it, url="file:///etc/passwd"),
                   "a bin name with a slash": dict(it, bin_names=["../x"])}.items():
    try:
        binaries._validate(bad)
        check(f"binary with {label} is refused", False)
    except BootFailure:
        check(f"binary with {label} is refused", True)

# -- finding a release's executables ------------------------------------------------------------
rel = tmp / "rel"
for f, mode in {"pkg-1.0/bin/tool": 0o755, "pkg-1.0/bin/tool-helper": 0o755,
                "pkg-1.0/bin/README": 0o644, "pkg-1.0/share/bin/tool": 0o755,
                "other/tool2": 0o755}.items():
    (rel / f).parent.mkdir(parents=True, exist_ok=True)
    (rel / f).write_text("#!/bin/sh\n")
    os.chmod(rel / f, mode)
base = {"name": "tool", "bins": None, "archive_path": None}
d, bins = binaries._discover(rel, base)
check("no bin_names: the shallowest bin/ with the binary, and its other executables",
      d == rel / "pkg-1.0" / "bin" and bins == ["tool", "tool-helper"])
d, bins = binaries._discover(rel, dict(base, name="tool2"))
check("no bin/ with it: the executable named like the binary, wherever it is",
      d == rel / "other" and bins == ["tool2"])
d, bins = binaries._discover(rel, dict(base, bins=["tool"], archive_path="pkg-*/bin"))
check("explicit archive_path + bin_names are used as given", d == rel / "pkg-1.0" / "bin" and bins == ["tool"])
try:
    binaries._discover(rel, dict(base, name="absent"))
    check("no executable named like the binary is a failure", False)
except BootFailure:
    check("no executable named like the binary is a failure", True)
from pod_harness.boot import app as appmod  # noqa: E402
check("{app} in argv is the app root", appmod.app_argv(["bash", "{app}/run.sh", "x"], Path("/r"))
      == ["bash", "/r/run.sh", "x"] and appmod.app_argv("go {app}", Path("/r")) == ["bash", "-lc", "go /r"])

# -- the stage runner ---------------------------------------------------------------------------------
ctx = Context(boot_dir=tmp / "bd3")


got = {}


def slow(c):
    got["rc"] = c.run(["sleep", "30"], timeout=60)[0]


t0 = time.time()
core.run_stage(ctx, "app", slow, limit=1)
check("a stage's commands are bounded by its limit (killed, rc None)",
      got["rc"] is None and time.time() - t0 < 10)


def stuck(c):
    time.sleep(30)


t0 = time.time()
try:
    core.run_stage(ctx, "addons", stuck, limit=0.5)
    check("a stage that ignores its deadline is abandoned and reported", False)
except BootFailure as f:
    check("a stage that ignores its deadline is abandoned and reported",
          time.time() - t0 < 10 and "did not finish" in f.reason and f.stage == "addons")


def broken(c):
    raise KeyError("oops")


try:
    core.run_stage(ctx, "models", broken, limit=5)
except BootFailure as f:
    check("an unexpected exception becomes a BootFailure naming the stage",
          f.stage == "models" and "KeyError" in f.reason)


def fails(c):
    raise BootFailure("nope", rc=9)


try:
    core.run_stage(ctx, "binaries", fails, limit=5)
except BootFailure as f:
    check("a BootFailure keeps its rc and gets the stage", f.rc == 9 and f.stage == "binaries")
rc, out = ctx.run(["sh", "-c", "echo hello; exit 3"], timeout=10)
check("ctx.run returns rc and the output's tail", rc == 3 and "hello" in out)
check("ctx.limit reads boot.limits, else the default",
      (ctx.boot.update({"limits": {"app": 7}}) or ctx.limit("app")) == 7.0
      and ctx.limit("models") == core.DEFAULT_LIMITS["models"])

# -- spec shapes ---------------------------------------------------------------------------------------
c = Context(boot_dir=tmp / "bd4")
c.load({"job_id": "j", "boot": {"models": {"paths": []}}, "policy": {"idle_stop_s": 1}})
check("a server spec {boot, policy, ...} is read as such", c.boot == {"models": {"paths": []}}
      and c.policy == {"idle_stop_s": 1})
c.load({"gpu_check": False})
check("a bare boot object is read as the boot spec", c.boot == {"gpu_check": False})
try:
    c.load({"boot": ["x"]})
    check("a boot that is not an object is a spec failure", False)
except BootFailure as f:
    check("a boot that is not an object is a spec failure", f.stage == "spec")

# -- the workload log scan -----------------------------------------------------------------------------
log = tmp / "w.log"
log.write_text("starting\n")
w = workload.Workload(ctx, ["true"], cwd=None, env={}, url=None, up_timeout=1,
                      patterns=["CUDA unknown error", "OUT OF CHEESE"], log=log)
w.offset = 0
check("a clean log has no verdict", w.scan() is None)
with open(log, "a") as f:
    f.write("RuntimeError: CUDA unknown error\n")
hit = w.scan()
check("a CUDA string is a GPU verdict (rc 7, gpu_check)",
      hit and hit[2] is True and w._fatal(hit).rc == 7 and w._fatal(hit).stage == "gpu_check")
with open(log, "a") as f:
    f.write("out of cheese, redo from start\n")
hit = w.scan()
check("a scan_log_for string is a workload verdict, case-insensitive",
      hit and hit[2] is False and w._fatal(hit).stage == "workload")
with open(log, "a") as f:
    f.write("partial line with CUDA unknown")
check("a half-written line waits for its end", w.scan() is None)

# -- image identity ------------------------------------------------------------------------
from pod_harness.boot import image  # noqa: E402
import json as _json  # noqa: E402

os.environ.pop("PODH_IMAGE_DIGEST", None)
mf = tmp / "manifest.json"
os.environ["PODH_IMAGE_MANIFEST"] = str(mf)
check("no manifest, no digest: no identity, still a container start",
      image.identity() == {} and image.facts()["container_started_at"] <= time.time() + 1)
mf.write_text(_json.dumps({"image": "podh-x", "version": "v2", "digest": "sha256:" + "1" * 64}))
check("the manifest names the image (name:version) and may carry a digest",
      image.identity() == {"image": "podh-x:v2", "image_digest": "sha256:" + "1" * 64})
os.environ["PODH_IMAGE_DIGEST"] = "reg/podh-x@sha256:" + "2" * 64
check("PODH_IMAGE_DIGEST wins, a full reference reduced to its digest",
      image.identity()["image_digest"] == "sha256:" + "2" * 64)
os.environ.pop("PODH_IMAGE_DIGEST")
mf.write_text("not json")
check("an unreadable manifest is no manifest", image.identity() == {})
os.environ.pop("PODH_IMAGE_MANIFEST")

# -- workload.comfyui.fast_disk ----------------------------------------------------------------
print("— fast_disk")
fd = tmp / "fdcomfy"
(fd / "comfy").mkdir(parents=True)
(fd / "main.py").write_text("")
(fd / "comfy" / "cli_args.py").write_text('parser.add_argument("--fast-disk", action="store_true")\n')
fctx = Context(boot_dir=tmp / "fdboot")
fctx.load({"workload": {"comfyui": {"root": str(fd), "fast_disk": True}}})
w = workload.build(fctx)
check("fast_disk: true passes --fast-disk", "--fast-disk" in w.cmd)
fctx.load({"workload": {"comfyui": {"root": str(fd), "fast_disk": False}}})
check("fast_disk: false passes --disable-fast-disk",
      "--disable-fast-disk" in workload.build(fctx).cmd)
fctx.load({"workload": {"comfyui": {"root": str(fd)}}})
c0 = workload.build(fctx).cmd
check("unset: neither flag (ComfyUI decides)",
      "--fast-disk" not in c0 and "--disable-fast-disk" not in c0)
os.environ["PODH_COMFY_FAST_DISK"] = "1"
check("$PODH_COMFY_FAST_DISK=1 when the spec does not say", "--fast-disk" in workload.build(fctx).cmd)
fctx.load({"workload": {"comfyui": {"root": str(fd), "fast_disk": False}}})
check("...the spec wins over the env", "--fast-disk" not in workload.build(fctx).cmd)
os.environ.pop("PODH_COMFY_FAST_DISK")
fctx.load({"workload": {"comfyui": {"root": str(fd), "fast_disk": True,
                                     "args": ["--disable-fast-disk"]}}})
c1 = workload.build(fctx).cmd
check("args that already say win, once", c1.count("--disable-fast-disk") == 1
      and "--fast-disk" not in c1)
(fd / "comfy" / "cli_args.py").write_text("# an older ComfyUI\n")
fctx.load({"workload": {"comfyui": {"root": str(fd), "fast_disk": True}}})
check("a ComfyUI without the flag is not given it", "--fast-disk" not in workload.build(fctx).cmd)

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
