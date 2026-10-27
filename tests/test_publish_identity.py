"""docker/comfyui/publish-identity: extract an image's manifest and catalogue from its layers
and publish them as release assets — against fake `crane` and `gh` (no registry, no GitHub).
"""
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "docker" / "comfyui" / "publish-identity"
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


if not shutil.which("bash") or not shutil.which("gzip"):
    print("skipped: needs bash and gzip")
    sys.exit(0)

T = Path(tempfile.mkdtemp())
reg = T / "registry"
reg.mkdir()


def layer(files: dict, compress: bool) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    return gzip.compress(raw) if compress else raw


DIGEST = "sha256:" + "d" * 64
man = {"format": 1, "image": "podh-comfyui", "version": "v0.43.0-cu128-abcdef1",
       "revision": "abcdef1" + "0" * 33, "comfyui": {"tag": "v0.43.0"}}
cat = {"format": 1, "count": 2, "nodes": {"A": {"in": {}, "out": []}, "B": {"in": {}, "out": []}}}
blobs = {"sha256:" + "1" * 64: layer({"opt/podh/manifest.json": json.dumps(man).encode(),
                                      "opt/podh/catalogue.json": json.dumps(cat).encode()}, True),
         "sha256:" + "2" * 64: layer({"etc/other": b"x"}, False)}
for d, b in blobs.items():
    (reg / d.replace(":", "_")).write_bytes(b)
# The files sit in the second-to-last layer: the script must look past the last one.
(reg / "manifest.json").write_text(json.dumps({"layers": [
    {"digest": "sha256:" + "0" * 64}, {"digest": "sha256:" + "1" * 64},
    {"digest": "sha256:" + "2" * 64}]}))
(reg / ("sha256_" + "0" * 64)).write_bytes(layer({"usr/x": b"y"}, True))

bin_ = T / "bin"
bin_.mkdir()
(bin_ / "crane").write_text(f"""#!/bin/sh
echo "crane $*" >> {T}/crane.log
case "$1" in
  manifest) cat {reg}/manifest.json;;
  blob) d=$(echo "$2" | sed 's/.*@//; s/:/_/'); cat {reg}/$d;;
esac
""")
state = T / "gh"
state.mkdir()
(bin_ / "gh").write_text(f"""#!/bin/sh
echo "gh $*" >> {T}/gh.log
S={state}
[ "$1" = release ] || exit 2
cmd=$2; tag=$3; shift 3
case $cmd in
  view) [ -d "$S/$tag" ];;
  create) mkdir -p "$S/$tag";;
  upload)
    while [ $# -gt 0 ]; do
      case $1 in --repo) shift 2;; --clobber) shift;; *) cp "$1" "$S/$tag/"; shift;; esac
    done;;
  download)
    pat=; dir=
    while [ $# -gt 0 ]; do
      case $1 in --pattern) pat=$2; shift 2;; --dir) dir=$2; shift 2;; *) shift;; esac
    done
    [ -f "$S/$tag/$pat" ] && cp "$S/$tag/$pat" "$dir/";;
esac
""")
for f in bin_.iterdir():
    f.chmod(0o755)
env = {**os.environ, "CRANE": str(bin_ / "crane"), "GH": str(bin_ / "gh"),
       "GITHUB_SHA": "abcdef1" + "0" * 33}


def publish(name="podh-comfyui", version="v0.43.0-cu128-abcdef1", primary=True):
    args = ["bash", str(SCRIPT), "--name", name, "--version", version, "--digest", DIGEST,
            "--revision", "abcdef1" + "0" * 33, "--mirror", f"ghcr.io/o/{name}",
            "--repo", "o/pod-harness", "--work", str(T / f"work-{name}")]
    if primary:
        args += ["--primary", f"docker.io/u/{name}"]
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=60)


p = publish()
check(f"publishes ({p.returncode}: {p.stderr[-300:]})", p.returncode == 0)
rel = state / "images-podh-comfyui-abcdef1"
pm = json.loads((rel / "manifest.json").read_text()) if (rel / "manifest.json").is_file() else {}
check("the per-build release has manifest.json and catalogue.json",
      pm and json.loads((rel / "catalogue.json").read_text())["count"] == 2)
check("the published manifest adds digest and refs: Docker Hub as image, GHCR as mirror",
      pm.get("digest") == DIGEST
      and pm.get("refs") == {"image": f"docker.io/u/podh-comfyui@{DIGEST}",
                             "mirror": f"ghcr.io/o/podh-comfyui@{DIGEST}"}
      and pm.get("catalogue_url", "").endswith("/images-podh-comfyui-abcdef1/catalogue.json"))
check("only the layers' blobs were fetched (from the mirror, by digest), the files found past "
      "the last layer", "crane blob ghcr.io/o/podh-comfyui@sha256:" + "1" * 64
      in (T / "crane.log").read_text())
idx = json.loads((state / "images-latest" / "index.json").read_text())
e = idx["images"]["podh-comfyui"]
check("images-latest/index.json lists it: image (docker.io), mirror, tag, digest, URLs",
      e["image"] == f"docker.io/u/podh-comfyui@{DIGEST}"
      and e["mirror"] == f"ghcr.io/o/podh-comfyui@{DIGEST}"
      and e["tag"] == "v0.43.0-cu128-abcdef1" and e["digest"] == DIGEST
      and e["manifest_url"] == "https://github.com/o/pod-harness/releases/download/"
                               "images-podh-comfyui-abcdef1/manifest.json"
      and (state / "images-latest" / "podh-comfyui.manifest.json").is_file())
check("releases are made with --latest=false", "--latest=false" in (T / "gh.log").read_text())

man.update(image="podh-comfyui-video")
(reg / ("sha256_" + "1" * 64)).write_bytes(layer({
    "opt/podh/manifest.json": json.dumps(man).encode(),
    "opt/podh/catalogue.json": json.dumps(cat).encode()}, True))
p = publish("podh-comfyui-video", primary=False)
idx = json.loads((state / "images-latest" / "index.json").read_text())
check("a second image joins the index without dropping the first; GHCR is image when there "
      "is no Docker Hub", p.returncode == 0 and set(idx["images"]) == {"podh-comfyui",
                                                                       "podh-comfyui-video"}
      and idx["images"]["podh-comfyui-video"]["image"] == f"ghcr.io/o/podh-comfyui-video@{DIGEST}")
p = publish("podh-comfyui-video", version="v-other")
check("an image whose manifest names another version is refused (build args not wired)",
      p.returncode != 0 and "CI built" in p.stderr)

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
