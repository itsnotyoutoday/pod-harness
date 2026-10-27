"""docker/comfyui/write_manifest.py: the image manifest, against a fake image tree (baked
packs and binaries, a ComfyUI checkout, the compat shim) — no docker, no torch.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "docker" / "comfyui" / "write_manifest.py"
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


T = Path(tempfile.mkdtemp())
baked = T / "baked"
(baked / "bin" / "ffmpeg-d113aa5b57ab").mkdir(parents=True)
ff = baked / "bin" / "ffmpeg-d113aa5b57ab" / "ffmpeg"
ff.write_text("#!/bin/sh\ncase \"$*\" in\n  *-encoders*) echo ' V....D h264_nvenc  NVIDIA NVENC';;\n"
              "  *) echo 'ffmpeg version n8.1.3 Copyright';;\nesac\n")
ff.chmod(0o755)
(baked / "bin" / "ffmpeg-d113aa5b57ab" / ".podh-binary.json").write_text(json.dumps(
    {"name": "ffmpeg", "sha256": "d113aa5b" + "0" * 56, "bins": ["ffmpeg", "ffprobe"]}))
(baked / "addons.json").write_text(json.dumps({"addons": {
    "PackB": {"commit": "b" * 40, "dir": "/x/PackB", "repo": "https://github.com/o/PackB"},
    "PackA": {"commit": "a" * 40, "dir": "/x/PackA", "repo": "https://github.com/o/PackA"}}}))
comfy = T / "ComfyUI"
(comfy / "custom_nodes").mkdir(parents=True)
(comfy / "custom_nodes" / "00_podh_compat.py").write_text("# shim\n")
env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
for c in (["git", "init", "-q"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit",
                                  "-q", "--allow-empty", "-m", "x"]):
    subprocess.run(c, cwd=comfy, check=True, env=env)
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=comfy, capture_output=True, text=True,
                      env=env).stdout.strip()


def write(out, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--baked-root", str(baked),
                           "--comfy-root", str(comfy), "--no-torch", "--out", str(out), *args],
                          capture_output=True, text=True, env=env, timeout=120)


base = T / "base.json"
p = write(base, "--image", "podh-comfyui", "--version", "v0.43.0-cu128-abc1234",
          "--revision", "f" * 40, "--base", "ubuntu:24.04@sha256:" + "2" * 64,
          "--comfyui-tag", "v0.43.0", "--catalogue", "/opt/podh/catalogue.json")
m = json.loads(base.read_text()) if p.returncode == 0 else {}
check(f"writes the manifest ({p.returncode} {p.stderr[-200:]})", p.returncode == 0)
check("identity: image, version, revision, base, format",
      m.get("image") == "podh-comfyui" and m.get("version") == "v0.43.0-cu128-abc1234"
      and m.get("revision") == "f" * 40 and m.get("base", "").startswith("ubuntu:24.04@sha256:")
      and m.get("format") == 1 and "digest" not in m)
check("ComfyUI: tag as given, commit read from the checkout",
      m.get("comfyui") == {"tag": "v0.43.0", "commit": head})
check("node packs from addons.json: name, repo, commit, sorted",
      m.get("node_packs") == [
          {"name": "PackA", "repo": "https://github.com/o/PackA", "commit": "a" * 40},
          {"name": "PackB", "repo": "https://github.com/o/PackB", "commit": "b" * 40}])
check("baked binaries: name, sha256, version line",
      m.get("binaries") and m["binaries"][0]["name"] == "ffmpeg"
      and m["binaries"][0]["sha256"].startswith("d113aa5b")
      and m["binaries"][0].get("version", "").startswith("ffmpeg version n8.1.3"))
f = m.get("features") or {}
check("features probed: nvenc ffmpeg and the compat shim yes, latentsync no",
      f.get("nvenc_capable_ffmpeg") is True and f.get("compat_shim") is True
      and f.get("latentsync_fixes") is False
      and set(f) == {"nvenc_capable_ffmpeg", "insightface", "onnxruntime_gpu",
                     "latentsync_fixes", "compat_shim", "sshd", "triton_cc"})
check("python and the torch/CUDA sections are present",
      m.get("python") and set(m.get("torch") or {}) == {"torch", "torchvision", "torchaudio"}
      and "cudnn" in (m.get("cuda") or {}))

(comfy / "custom_nodes" / "00_podh_latentsync.py").write_text("# fixes\n")
video = T / "video.json"
p = write(video, "--image", "podh-comfyui-video", "--version", "v0.43.0-cu128-abc1234",
          "--revision", "f" * 40, "--extends", str(base))
v = json.loads(video.read_text()) if p.returncode == 0 else {}
check("the video image extends the base: parent kept, base and ComfyUI tag inherited",
      p.returncode == 0 and v.get("parent") == {"image": "podh-comfyui",
                                                "version": "v0.43.0-cu128-abc1234",
                                                "revision": "f" * 40}
      and v.get("base") == m.get("base") and v["comfyui"]["tag"] == "v0.43.0"
      and v["features"]["latentsync_fixes"] is True)
p = write(T / "x.json", "--image", "x", "--version", "1", "--extends", str(T / "nope.json"))
check("a missing --extends fails the build", p.returncode != 0)

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
