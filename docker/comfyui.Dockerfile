# syntax=docker/dockerfile:1.7
#
# podh-comfyui — ComfyUI on CUDA 12.8, booted by podh-boot. The base of podh-comfyui-video.
#
# ## What a job gets
#
# The job's boot spec runs as podh-boot's stages (gpu_check → binaries → models → addons →
# app → workload → ready), and this image has most of the slow ones already done:
#
#   binaries  the pinned ffmpeg is baked under $PODH_BAKED_ROOT/bin, laid out the way the
#             binaries stage lays out its own installs, so a spec pinning the same sha256
#             uses it in place: no download, no kit
#   addons    six light node packs are checked out in custom_nodes at full pinned commits,
#             their requirements installed and recorded in $PODH_BAKED_ROOT/addons.json, so
#             an addon item pinning the same commit skips the clone and requirements.txt
#             (its pip extras, links and probe still run)
#   workload  ComfyUI itself, at a release tag, in its own venv
#
# Models are never baked. They are the job's, and land in $PODH_MODELS_ROOT.
#
# With PUBLIC_KEY in its environment (the GPU clouds' convention), the container also runs
# sshd on :22, key-only, with host keys made at start (/opt/podh/entry → start-sshd).
#
# ## CUDA without a CUDA base image
#
# FROM plain Ubuntu. CUDA and cuDNN are the nvidia-*-cu12 pip wheels torch itself requires
# (docker/comfyui/cuda.txt, exactly torch's pins): an nvidia/cuda base would ship a second
# copy of the same libraries (~2 GB compressed) that torch never loads. Who finds them how:
#
#   torch       its own RPATHs into site-packages/nvidia/*/lib — nothing to configure
#   the rest    LD_LIBRARY_PATH (below) names the same directories, so a library that links
#               libcudnn.so.9 / libcublas.so.12 / … by soname — onnxruntime-gpu in the video
#               image, or one an addon pip-installs at boot — resolves them in any import
#               order. The build `ldd`s libtorch_cuda.so (and, in the video image,
#               onnxruntime's CUDA provider) and fails on anything unresolved.
#   the driver  libcuda, libnvidia-encode (ffmpeg's NVENC), … are the host's, mounted by the
#               NVIDIA container runtime as NVIDIA_DRIVER_CAPABILITIES asks — which must
#               include `video`, or NVENC/NVDEC are not mounted.
#
# ## One interpreter
#
# Ubuntu 24.04's python3 is 3.12, the version everything here is pinned for, so there is no
# second interpreter to get wrong (styletts2.Dockerfile explains what that cost). The venv
# at /opt/venv is made from it and is FIRST on PATH, so `python`, `python3`, `pip` and
# podh-boot are all the venv's. The build asserts it.
#
# ## Versions
#
# docker/comfyui/lock.txt pins every python package (see lock.in to regenerate) and is
# passed as a constraints file to every install; cuda.txt pins torch's CUDA wheels.
# docker/comfyui/guard.txt — the torch trio and numpy — is installed as $PIP_CONSTRAINT, so
# it binds every pip run in the container, including the addons stage's at boot: a pack
# that asks for another torch fails loudly instead of replacing the CUDA build.
#
# ## Layers
#
# Eight, of a few hundred MB to ~1.2 GB each (compressed), so a pull downloads them
# side by side instead of waiting on one giant, ordered by how rarely they change:
#
#   1  apt (python, git, aria2, sshd, …), the venv, ffmpeg          ~0.3 GB
#   2  cuDNN + NCCL                                                 ~1.0 GB
#   3  cuBLAS + cuSPARSE + cuSOLVER                                 ~1.15 GB
#   4  the rest of torch's CUDA wheels, triton                      ~1.0 GB
#   5  torch, torchvision, torchaudio (--no-deps)                   ~0.85 GB
#   6  ComfyUI, its requirements, pod-harness's dependencies        ~0.5–1 GB
#   7  the node packs (+ sam2)                                      ~0.5–1 GB
#   8  pod-harness, the small files, and the build's own checks     < 1 MB
#
# Everything else is metadata (ENV, EXPOSE, ENTRYPOINT: no filesystem layer). Build-only
# inputs come in through `RUN --mount=type=bind`, never COPY, so they are not layers; each
# RUN mounts only the files it reads, so editing one does not invalidate the layers below
# it. The checks run inside layer 8 (a RUN of its own, changing nothing, would still make
# an empty layer) with PYTHONDONTWRITEBYTECODE, so they add nothing to it.
FROM --platform=linux/amd64 ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c

ARG COMFYUI_TAG=v0.43.0
ARG COMFYUI_COMMIT=426f5eea52a7ece1dd599fff4cb6cd1f352ddd24
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-26-14-25/ffmpeg-n8.1.3-24-g92611900bb-linux64-gpl-8.1.tar.xz
ARG FFMPEG_SHA256=d113aa5b57ab0dcfee171d835240ce3ea5db826a0ee80ed7528c8d26a61e87a4
ARG PIP_VERSION=26.2.2
ARG SAM2_COMMIT=2b90b9f5ceec907a1c18123530e92e794ad901a4

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    PODH_COMFY_ROOT=/opt/ComfyUI \
    PODH_COMFY_PY=/opt/venv/bin/python \
    PODH_ADDONS_ROOT=/opt/ComfyUI/custom_nodes \
    PODH_BAKED_ROOT=/opt/podh/baked \
    PODH_MODELS_ROOT=/workspace/models \
    PODH_WORKSPACE=/workspace \
    PODH_LOCK=/opt/podh/lock.txt \
    IMAGEIO_FFMPEG_EXE=/usr/local/bin/ffmpeg
# The CUDA wheels' library directories, for everything that is not torch (see the top); the
# build fails if one of them does not exist. The last two are where older GPU runtimes
# mount the driver, as the nvidia/cuda images had it.
ENV LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cufft/lib:/opt/venv/lib/python3.12/site-packages/nvidia/curand/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cusolver/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cusparse/lib:/opt/venv/lib/python3.12/site-packages/nvidia/nvjitlink/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64

# --- 1. system, the venv, ffmpeg ----------------------------------------------------------
# libgl1 + libglib2.0-0: opencv's non-headless build (one of the packs asks for it). aria2:
# the models stage's parallel ranged downloads. zstd: kits are published as tar.zst when it
# exists. git stays: podh-boot's addons stage clones, and recognises a baked pack by its
# HEAD. openssh-server: for start-sshd; the host keys its install generates are deleted
# (every container makes its own), and logins are key-only. gcc + libc6-dev + python3-dev:
# not for building wheels (every pinned wheel is binary) but for Triton, which compiles its
# GPU kernels' host launcher with a C compiler AT RUN TIME — torch.compile, Triton-backed ops
# and SageAttention fail without one. ~80 MB.
# safe.directory: a pod that runs as another uid must still be able to read a baked
# checkout's HEAD, or it would re-clone over it.
#
# ffmpeg: BtbN's static GPL build (NVENC included), sha256-verified, into
# $PODH_BAKED_ROOT/bin/ffmpeg-<sha12>/ with the binaries stage's own marker, so a boot spec
# pinning this sha256 finds it; /usr/local/bin links to it for everything else.
RUN set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
        python3 python3-venv \
        git aria2 tini curl ca-certificates xz-utils zstd procps openssh-server \
        gcc libc6-dev python3-dev \
        libgl1 libglib2.0-0; \
    apt-get clean; rm -rf /var/lib/apt/lists/*; \
    rm -f /etc/ssh/ssh_host_*; \
    printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
        'PermitRootLogin prohibit-password' 'PubkeyAuthentication yes' \
        > /etc/ssh/sshd_config.d/podh.conf; \
    git config --system --add safe.directory '*'; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/python -m pip install --no-input "pip==${PIP_VERSION}"; \
    mkdir -p /opt/podh/baked /workspace/models; \
    d="/opt/podh/baked/bin/ffmpeg-$(echo "$FFMPEG_SHA256" | cut -c1-12)"; \
    mkdir -p "$d" /tmp/ff; \
    curl -fsSL --retry 5 -o /tmp/ff/ff.tar.xz "$FFMPEG_URL"; \
    echo "$FFMPEG_SHA256  /tmp/ff/ff.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/ff/ff.tar.xz -C /tmp/ff --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'; \
    install -m 0755 /tmp/ff/*/bin/ffmpeg /tmp/ff/*/bin/ffprobe "$d/"; \
    printf '{"name": "ffmpeg", "sha256": "%s", "bins": ["ffmpeg", "ffprobe"]}\n' "$FFMPEG_SHA256" \
        > "$d/.podh-binary.json"; \
    ln -s "$d/ffmpeg" /usr/local/bin/ffmpeg; ln -s "$d/ffprobe" /usr/local/bin/ffprobe; \
    rm -rf /tmp/ff /root/.cache; \
    ffmpeg -hide_banner -version | head -1

# --- 2–4. torch's CUDA: the nvidia-*-cu12 wheels, in three layers ---------------------------
# Exactly the versions cuda.txt pins (torch's own requirements), from PyPI, --no-deps; which
# wheel lands in which layer is decided here, by name, to keep the three about one size.
# Section 6 resolves torch's requirements against cuda.txt, so a torch that wanted anything
# else would fail there rather than download a second copy into a later layer.
RUN --mount=type=bind,source=docker/comfyui/cuda.txt,target=/tmp/cuda.txt \
    set -eux; \
    pip install --no-input --no-deps --only-binary=:all: \
        $(grep -E '^(nvidia-cudnn-cu12|nvidia-nccl-cu12)==' /tmp/cuda.txt)
RUN --mount=type=bind,source=docker/comfyui/cuda.txt,target=/tmp/cuda.txt \
    set -eux; \
    pip install --no-input --no-deps --only-binary=:all: \
        $(grep -E '^(nvidia-cublas-cu12|nvidia-cusparse-cu12|nvidia-cusolver-cu12)==' /tmp/cuda.txt)
RUN --mount=type=bind,source=docker/comfyui/cuda.txt,target=/tmp/cuda.txt \
    set -eux; \
    pip install --no-input --no-deps --only-binary=:all: \
        $(grep -vE '^(#|$|nvidia-cudnn-cu12==|nvidia-nccl-cu12==|nvidia-cublas-cu12==|nvidia-cusparse-cu12==|nvidia-cusolver-cu12==)' /tmp/cuda.txt); \
    python -I -B -c 'import importlib.metadata as m, sys; p = [l.strip().split("==") for l in open(sys.argv[1]) if "==" in l and not l.startswith("#")]; b = [n for n, v in p if m.version(n) != v]; sys.exit("FAIL: not as cuda.txt pins: %s" % b if b else 0)' /tmp/cuda.txt

# --- 5. torch, from the cu128 index -------------------------------------------------------
# The exact trio (the guard's), --no-deps (its CUDA is already in; its small pure-python
# requirements come with section 6, under the lock), and only from download.pytorch.org:
# the +cu128 local version exists nowhere else. guard.txt and cuda.txt go into the image
# here, with the torch they describe.
RUN --mount=type=bind,source=docker/comfyui/guard.txt,target=/tmp/guard.txt \
    --mount=type=bind,source=docker/comfyui/cuda.txt,target=/tmp/cuda.txt \
    set -eux; \
    pip install --no-input --no-deps --index-url https://download.pytorch.org/whl/cu128 \
        $(grep -E '^(torch|torchvision|torchaudio)==' /tmp/guard.txt); \
    install -m 0644 /tmp/guard.txt /tmp/cuda.txt /opt/podh/
ENV PIP_CONSTRAINT=/opt/podh/guard.txt

# --- 6. ComfyUI at its release tag, and pod-harness's dependencies ------------------------
# Cloned by tag and then held to the commit the tag named when this was written: a moved
# tag fails the build rather than shipping something else under the same name.
# ComfyUI-Manager is deliberately absent: what a pod runs is decided by its boot spec.
# The torch trio is named again so that its own requirements (filelock, sympy, … and the
# CUDA wheels, already present) resolve here, under the lock AND cuda.txt.
RUN --mount=type=bind,source=docker/comfyui/lock.txt,target=/tmp/lock.txt \
    set -eux; \
    install -m 0644 /tmp/lock.txt /opt/podh/lock.txt; \
    while read -r l; do \
        case "$l" in ''|'#'*) continue;; esac; \
        grep -qxF "$l" /opt/podh/lock.txt \
            || { echo "FAIL: guard.txt '$l' is not in lock.txt: one torch installed, another defended"; exit 1; }; \
    done < /opt/podh/guard.txt; \
    pip install --no-input -c /opt/podh/lock.txt setuptools; \
    git clone -q --depth 1 --branch "$COMFYUI_TAG" https://github.com/Comfy-Org/ComfyUI /opt/ComfyUI; \
    test "$(git -C /opt/ComfyUI rev-parse HEAD)" = "$COMFYUI_COMMIT" \
        || { echo "FAIL: $COMFYUI_TAG is $(git -C /opt/ComfyUI rev-parse HEAD), expected $COMFYUI_COMMIT"; exit 1; }; \
    pip install --no-input -c /opt/podh/lock.txt -c /opt/podh/cuda.txt \
        $(grep -E '^(torch|torchvision|torchaudio)==' /opt/podh/guard.txt) \
        -r /opt/ComfyUI/requirements.txt \
        "boto3>=1.34" "fastapi>=0.110" "uvicorn[standard]>=0.29"; \
    rm -rf /root/.cache; \
    python -B -c "import torch, torchvision, torchaudio; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'cudnn', torch.backends.cudnn.version())"

# --- 7. the light node packs, baked -------------------------------------------------------
# Each at a full commit, checked out the way the addons stage does it, requirements under
# the lock, recorded in addons.json. Directory names are the repo names: an addon item
# must use the same `name` to be recognised.
#
# Exceptions, each deliberate:
#   * Frame-Interpolation ships no requirements.txt; its install.py installs
#     requirements-no-cupy.txt and then cupy. That file is used here and cupy is not
#     baked (only a few of its VFI models want it; an addon item's pip extras can add it).
#   * Impact-Pack's requirements.txt carries an UNPINNED `git+…/sam2` line. podh-bake-node
#     skips URL lines, and sam2 is installed here at a pinned commit, with no CUDA
#     extension (SAM2_BUILD_CUDA=0: there is no nvcc in this image, and the extension
#     only speeds up a post-processing step) and no build isolation (which would download
#     a second torch from PyPI just to read setup.py).
#   * Install scripts (install.py) are not run: Impact-Pack's downloads SAM weights, and
#     models are the job's, not the image's.
#   * Three opencv distributions all provide `cv2`, and the last one installed owns its
#     files. All three are pinned to one version; contrib (the superset) is reinstalled
#     last so its build is the one that loads.
RUN --mount=type=bind,source=docker/comfyui/podh-bake-node,target=/tmp/podh-bake-node \
    set -eux; \
    bake="bash /tmp/podh-bake-node"; \
    $bake ComfyUI-VideoHelperSuite https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite 4d907bee61e92c2e65af3bd6383a4e4d356126d1; \
    $bake ComfyUI-Frame-Interpolation https://github.com/Fannovel16/ComfyUI-Frame-Interpolation 26545cc2dd95bc3d27f056016300673bdeee78f5 requirements-no-cupy.txt; \
    SAM2_BUILD_CUDA=0 pip install --no-input --no-build-isolation -c /opt/podh/lock.txt \
        "sam-2 @ git+https://github.com/facebookresearch/sam2@${SAM2_COMMIT}"; \
    $bake ComfyUI-Impact-Pack https://github.com/ltdrdata/ComfyUI-Impact-Pack 429d0159ad429e64d2b3916e6e7be9c22d025c3c; \
    $bake ComfyUI-Impact-Subpack https://github.com/ltdrdata/ComfyUI-Impact-Subpack 50c7b71a6a224734cc9b21963c6d1926816a97f1; \
    $bake rgthree-comfy https://github.com/rgthree/rgthree-comfy d7297b42f0fa78bec1c0411478f887423c249502; \
    $bake ComfyUI-KJNodes https://github.com/kijai/ComfyUI-KJNodes 2371136d2fa6fd0914e514cd99b36dabe5db9adf; \
    pip install --no-input --no-deps --force-reinstall \
        $(grep -E '^opencv-contrib-python==' /opt/podh/lock.txt); \
    rm -rf /root/.cache; \
    cat /opt/podh/baked/addons.json

# --- 8. pod-harness, the small files, and the image proves itself ---------------------------
# Last, because it changes most: a harness change rebuilds this layer and nothing else. Its
# dependencies are already in (section 6), so this is the package alone, built from a
# scratch copy of the bind-mounted source (pip builds inside the tree it is given).
#
# Then, with no GPU at build time: one interpreter; imports and versions against the lock
# and cuda.txt; every CUDA library resolvable (ldd) and every LD_LIBRARY_PATH directory
# present; cv2's build; every baked addon at its commit (check_image.py); ffmpeg;
# podh-boot; no baked ssh host key; and ComfyUI's own CI start — --quick-test-for-ci --cpu
# loads every custom node and exits. ComfyUI only LOGS a custom node that failed to
# import; here it fails the build (QUICK_TEST_STRICT=0 reports it instead, for a pack that
# cannot import without a GPU). No bytecode is written and what the test writes into the
# tree is cleaned, so the checks leave nothing behind.
#
# Started on its own (no control plane, no spec), the image still boots: GPU check, then
# ComfyUI on :8188 with models under /workspace/models (standalone.json). Under a control
# plane the job's spec wins.
ARG QUICK_TEST_STRICT=1
RUN --mount=type=bind,source=pyproject.toml,target=/tmp/ph-src/pyproject.toml \
    --mount=type=bind,source=src,target=/tmp/ph-src/src \
    --mount=type=bind,source=docker/comfyui,target=/tmp/ctx \
    set -eux; \
    cp -r /tmp/ph-src /tmp/ph; \
    pip install --no-input --no-deps --no-build-isolation /tmp/ph; \
    rm -rf /tmp/ph; \
    install -m 0755 /tmp/ctx/podh-bake-node /tmp/ctx/quick-test /tmp/ctx/entry /tmp/ctx/start-sshd /opt/podh/; \
    install -m 0644 /tmp/ctx/check_image.py /tmp/ctx/standalone.json /opt/podh/; \
    install -m 0644 /tmp/ctx/00_podh_compat.py /opt/ComfyUI/custom_nodes/00_podh_compat.py; \
    export PYTHONDONTWRITEBYTECODE=1; \
    test "$(command -v python3)" = /opt/venv/bin/python3; \
    test "$(command -v python)" = /opt/venv/bin/python; \
    test "$(command -v podh-boot)" = /opt/venv/bin/podh-boot; \
    if ls /etc/ssh/ssh_host_* 2>/dev/null; then echo "FAIL: an ssh host key is baked"; exit 1; fi; \
    python /opt/podh/check_image.py --pins /opt/podh/cuda.txt \
        --dist transformers comfy-kitchen comfyui-frontend-package; \
    ffmpeg -hide_banner -version | head -1; ffprobe -hide_banner -version | head -1; \
    podh-boot --help >/dev/null; \
    sh /opt/podh/quick-test "$QUICK_TEST_STRICT"

ENV PODH_STANDALONE_SPEC=/opt/podh/standalone.json
WORKDIR /workspace
EXPOSE 8188 22
LABEL org.opencontainers.image.title="podh-comfyui" \
      org.opencontainers.image.description="ComfyUI on CUDA 12.8 (torch's CUDA wheels on Ubuntu 24.04), booted by podh-boot"
ENTRYPOINT ["/opt/podh/entry"]
CMD []
