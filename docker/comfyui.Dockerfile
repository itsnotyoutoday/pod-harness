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
# passed as a constraints file to every install. docker/comfyui/guard.txt — the torch trio
# and numpy — is installed as $PIP_CONSTRAINT, so it binds every pip run in the container,
# including the addons stage's at boot: a pack that asks for another torch fails loudly
# instead of replacing the CUDA build.
#
# ## Layers
#
# Ordered by how often they change: system and ffmpeg, then torch (the bulk, cached across
# nearly every build), then ComfyUI, then the node packs, and pod-harness last — so a change
# to the harness, which rebuilds this image, rebuilds a few hundred KB.
FROM --platform=linux/amd64 nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

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
    PODH_COMFY_ROOT=/opt/ComfyUI \
    PODH_COMFY_PY=/opt/venv/bin/python \
    PODH_ADDONS_ROOT=/opt/ComfyUI/custom_nodes \
    PODH_BAKED_ROOT=/opt/podh/baked \
    PODH_MODELS_ROOT=/workspace/models \
    PODH_WORKSPACE=/workspace \
    PODH_LOCK=/opt/podh/lock.txt

# --- 1. system, the venv ------------------------------------------------------------------
# libgl1 + libglib2.0-0: opencv's non-headless build (one of the packs asks for it). aria2:
# the models stage's parallel ranged downloads. zstd: kits are published as tar.zst when it
# exists. git stays: podh-boot's addons stage clones, and recognises a baked pack by its
# HEAD. No compiler: every pinned wheel here is binary (the video image compiles its one
# exception in a builder stage). safe.directory: a pod that runs as another uid must still
# be able to read a baked checkout's HEAD, or it would re-clone over it.
RUN set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
        python3 python3-venv \
        git aria2 tini curl ca-certificates xz-utils zstd procps \
        libgl1 libglib2.0-0; \
    apt-get clean; rm -rf /var/lib/apt/lists/*; \
    git config --system --add safe.directory '*'; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/python -m pip install --no-input "pip==${PIP_VERSION}"; \
    mkdir -p /opt/podh/baked /workspace/models

# The torch trio and numpy, and nothing else yet: the full lock is copied after torch, so a
# change to some pack's dependency does not invalidate the torch layer beneath it.
COPY docker/comfyui/guard.txt /opt/podh/guard.txt

# --- 2. ffmpeg, pinned, verified, laid out as the binaries stage would --------------------
# BtbN's static GPL build (NVENC included). Into $PODH_BAKED_ROOT/bin/ffmpeg-<sha12>/ with
# the stage's own marker, so a boot spec pinning this sha256 finds it; /usr/local/bin links
# to it for everything else.
RUN set -eux; \
    d="/opt/podh/baked/bin/ffmpeg-$(echo "$FFMPEG_SHA256" | cut -c1-12)"; \
    mkdir -p "$d" /tmp/ff; \
    curl -fsSL --retry 5 -o /tmp/ff/ff.tar.xz "$FFMPEG_URL"; \
    echo "$FFMPEG_SHA256  /tmp/ff/ff.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/ff/ff.tar.xz -C /tmp/ff --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'; \
    install -m 0755 /tmp/ff/*/bin/ffmpeg /tmp/ff/*/bin/ffprobe "$d/"; \
    printf '{"name": "ffmpeg", "sha256": "%s", "bins": ["ffmpeg", "ffprobe"]}\n' "$FFMPEG_SHA256" \
        > "$d/.podh-binary.json"; \
    ln -s "$d/ffmpeg" /usr/local/bin/ffmpeg; ln -s "$d/ffprobe" /usr/local/bin/ffprobe; \
    rm -rf /tmp/ff; \
    ffmpeg -hide_banner -version | head -1
ENV IMAGEIO_FFMPEG_EXE=/usr/local/bin/ffmpeg

# --- 3. torch, from the cu128 index -------------------------------------------------------
# The exact trio (the guard's, which the build checks against the lock below), and only from
# download.pytorch.org: the +cu128 local version exists nowhere else. Its own layer, because
# it is most of the image and changes least.
RUN set -eux; \
    pip install --no-input --index-url https://download.pytorch.org/whl/cu128 \
        $(grep -E '^(torch|torchvision|torchaudio)==' /opt/podh/guard.txt); \
    python -c "import torch, torchvision, torchaudio; print('torch', torch.__version__, 'cuda', torch.version.cuda)"
ENV PIP_CONSTRAINT=/opt/podh/guard.txt

# --- 4. ComfyUI at its release tag, and pod-harness's dependencies ------------------------
# Cloned by tag and then held to the commit the tag named when this was written: a moved
# tag fails the build rather than shipping something else under the same name.
# ComfyUI-Manager is deliberately absent: what a pod runs is decided by its boot spec.
COPY docker/comfyui/lock.txt /opt/podh/lock.txt
COPY --chmod=0755 docker/comfyui/podh-bake-node /opt/podh/podh-bake-node
RUN set -eux; \
    while read -r l; do \
        case "$l" in ''|'#'*) continue;; esac; \
        grep -qxF "$l" /opt/podh/lock.txt \
            || { echo "FAIL: guard.txt '$l' is not in lock.txt: one torch installed, another defended"; exit 1; }; \
    done < /opt/podh/guard.txt; \
    pip install --no-input -c /opt/podh/lock.txt setuptools; \
    git clone -q --depth 1 --branch "$COMFYUI_TAG" https://github.com/Comfy-Org/ComfyUI /opt/ComfyUI; \
    test "$(git -C /opt/ComfyUI rev-parse HEAD)" = "$COMFYUI_COMMIT" \
        || { echo "FAIL: $COMFYUI_TAG is $(git -C /opt/ComfyUI rev-parse HEAD), expected $COMFYUI_COMMIT"; exit 1; }; \
    pip install --no-input -c /opt/podh/lock.txt -r /opt/ComfyUI/requirements.txt \
        "boto3>=1.34" "fastapi>=0.110" "uvicorn[standard]>=0.29"

# --- 5. the light node packs, baked -------------------------------------------------------
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
#     extension (SAM2_BUILD_CUDA=0: there is no nvcc in a runtime image, and the extension
#     only speeds up a post-processing step) and no build isolation (which would download
#     a second torch from PyPI just to read setup.py).
#   * Install scripts (install.py) are not run: Impact-Pack's downloads SAM weights, and
#     models are the job's, not the image's.
#   * Three opencv distributions all provide `cv2`, and the last one installed owns its
#     files. All three are pinned to one version; contrib (the superset) is reinstalled
#     last so its build is the one that loads.
RUN set -eux; \
    bake=/opt/podh/podh-bake-node; \
    $bake ComfyUI-VideoHelperSuite https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite 4d907bee61e92c2e65af3bd6383a4e4d356126d1; \
    $bake ComfyUI-Frame-Interpolation https://github.com/Fannovel16/ComfyUI-Frame-Interpolation 26545cc2dd95bc3d27f056016300673bdeee78f5 requirements-no-cupy.txt; \
    SAM2_BUILD_CUDA=0 pip install --no-input --no-build-isolation -c /opt/podh/lock.txt \
        "sam2 @ git+https://github.com/facebookresearch/sam2@${SAM2_COMMIT}"; \
    $bake ComfyUI-Impact-Pack https://github.com/ltdrdata/ComfyUI-Impact-Pack 429d0159ad429e64d2b3916e6e7be9c22d025c3c; \
    $bake ComfyUI-Impact-Subpack https://github.com/ltdrdata/ComfyUI-Impact-Subpack 50c7b71a6a224734cc9b21963c6d1926816a97f1; \
    $bake rgthree-comfy https://github.com/rgthree/rgthree-comfy d7297b42f0fa78bec1c0411478f887423c249502; \
    $bake ComfyUI-KJNodes https://github.com/kijai/ComfyUI-KJNodes 2371136d2fa6fd0914e514cd99b36dabe5db9adf; \
    pip install --no-input --no-deps --force-reinstall \
        $(grep -E '^opencv-contrib-python==' /opt/podh/lock.txt); \
    cat /opt/podh/baked/addons.json

# --- 6. pod-harness, from this repo -------------------------------------------------------
# Last, because it changes most. Its dependencies are already in (section 4), so this layer
# is the package alone.
COPY pyproject.toml /opt/pod-harness/pyproject.toml
COPY src/ /opt/pod-harness/src/
RUN pip install --no-input --no-deps --no-build-isolation /opt/pod-harness

# --- 7. the image proves itself -------------------------------------------------------------
# No GPU at build time, so: one interpreter, imports and versions against the lock, cv2's
# build, every baked addon at its commit (check_image.py), ffmpeg, podh-boot, and ComfyUI's
# own CI start — --quick-test-for-ci --cpu loads every custom node and exits. ComfyUI only
# LOGS a custom node that failed to import; here it fails the build (QUICK_TEST_STRICT=0
# reports it instead, for a pack that cannot import without a GPU). What the test writes
# into the tree is cleaned in the same layer.
ARG QUICK_TEST_STRICT=1
COPY docker/comfyui/check_image.py docker/comfyui/quick-test /opt/podh/
RUN set -eux; \
    test "$(command -v python3)" = /opt/venv/bin/python3; \
    test "$(command -v python)" = /opt/venv/bin/python; \
    test "$(command -v podh-boot)" = /opt/venv/bin/podh-boot; \
    python /opt/podh/check_image.py --dist transformers comfy-kitchen comfyui-frontend-package; \
    ffmpeg -hide_banner -version | head -1; ffprobe -hide_banner -version | head -1; \
    podh-boot --help >/dev/null; \
    sh /opt/podh/quick-test "$QUICK_TEST_STRICT"

WORKDIR /workspace
EXPOSE 8188
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venv/bin/podh-boot"]
CMD []
