# syntax=docker/dockerfile:1.7
#
# podh-comfyui-video — podh-comfyui plus the lip-sync and voice packs, which bring the heavy
# face and speech stacks the base image is better off without.
#
# Built FROM the base image's exact digest: CI passes BASE_IMAGE=<registry>/podh-comfyui@sha256:…
# for the base it pushed moments earlier, so the two images can never pair a video layer
# with a different base than the one it was tested on. Locally, build the base first and
# pass its tag.
#
# Everything about the base holds here unchanged: one interpreter, the lock as constraints,
# the guard in /opt/venv/pip.conf, packs baked the addons stage's way and recorded in
# $PODH_BAKED_ROOT/addons.json, models never baked, CUDA from torch's pip wheels.
#
# Three layers on top of the base's, ordered as there: the face stack (changes rarely), then
# the packs and the checks, which leave nothing behind, then the image's identity (manifest
# and node catalogue, < 1 MB).
ARG BASE_IMAGE=podh-comfyui:latest

# --- insightface: the one package with no wheel ----------------------------------------------
# insightface 0.7.3 is an sdist with a Cython extension, so it is compiled HERE, against the
# base's own python and numpy, and only the wheel crosses into the image: the compiler and
# headers (~250 MB) never ship. No build isolation, so it builds against the numpy that
# will import it rather than whatever PyPI's newest is.
FROM ${BASE_IMAGE} AS insightface
ARG INSIGHTFACE_VERSION=0.7.3
RUN set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends build-essential python3-dev; \
    pip install --no-input -c /opt/podh/lock.txt cython; \
    pip wheel --no-input --no-deps --no-build-isolation -w /wheels "insightface==${INSIGHTFACE_VERSION}"; \
    ls -l /wheels

FROM ${BASE_IMAGE}
ARG INSIGHTFACE_VERSION=0.7.3
ARG ONNXRUNTIME_GPU_VERSION=1.24.1

# --- 1. the face stack: onnxruntime-gpu and insightface -------------------------------------
# The wheel is bind-mounted from the builder rather than COPYd, so it is not a layer of its
# own. onnxruntime-gpu 1.24 is built for CUDA 12 + cuDNN 9, and its CUDA provider links
# libcublas(Lt).so.12, libcudart.so.12, libcufft.so.11, libcurand.so.10 and libcudnn.so.9
# by soname: all of them torch's CUDA wheels, found through the base's LD_LIBRARY_PATH in
# any import order (so no onnxruntime.preload_dlls() is needed). The checks below `ldd` it.
RUN --mount=type=bind,from=insightface,source=/wheels,target=/tmp/wheels \
    set -eux; \
    grep -qx "onnxruntime-gpu==${ONNXRUNTIME_GPU_VERSION}" /opt/podh/lock.txt; \
    pip install --no-input -c /opt/podh/lock.txt -c /opt/podh/cuda.txt \
        "onnxruntime-gpu==${ONNXRUNTIME_GPU_VERSION}" \
        /tmp/wheels/insightface-${INSIGHTFACE_VERSION}-*.whl; \
    rm -rf /root/.cache

# --- 2. the video packs, baked, and the image proves itself ----------------------------------
# LatentSyncWrapper checks for pytorch_lightning and accelerate the first time its node is
# used and pip-installs whatever is missing — and, because it looks `ffmpeg-python` up as a
# module name, it would reinstall that one on every pod. Both are installed here, and the
# flag file it writes after a successful check is written now, so a pod never runs pip from
# inside a node.
#
# ChatterBox normalises loudness only when pyloudnorm imports ("pyloudnorm not available.
# Loudness normalization disabled" otherwise), and its requirements do not list it, so it is
# installed here at the lock's pin. Its Perth watermarker is deliberately NOT installed.
#
# The checks are the base's (check_image.py, quick-test) plus the face and voice stacks,
# run in this same layer, writing no bytecode, so they add nothing to it.
ARG QUICK_TEST_STRICT=1
RUN --mount=type=bind,source=docker/comfyui,target=/tmp/ctx \
    set -eux; \
    bake=/opt/podh/podh-bake-node; \
    $bake ComfyUI-LatentSyncWrapper https://github.com/ShmuelRonen/ComfyUI-LatentSyncWrapper 360d5283d7276aee68b4237b1387e594e4ce640e; \
    $bake ComfyUI_Fill-ChatterBox https://github.com/filliptm/ComfyUI_Fill-ChatterBox f7d7a16187430abcaf91a3039b9c83aa9960816a; \
    pip install --no-input -c /opt/podh/lock.txt pytorch-lightning accelerate pyloudnorm; \
    echo "Dependencies installed at image build" > /root/.latentsync16_dependencies_installed; \
    rm -rf /root/.cache; \
    cat /opt/podh/baked/addons.json; \
    export PYTHONDONTWRITEBYTECODE=1; \
    python /opt/podh/check_image.py --pins /opt/podh/cuda.txt \
        --import insightface onnxruntime decord librosa diffusers s3tokenizer pyloudnorm \
        --dist onnxruntime-gpu transformers diffusers librosa \
        --ldd 'onnxruntime/capi/libonnxruntime_providers_cuda.so'; \
    python -c "import importlib.metadata as m; v = m.version('insightface'); assert v == '${INSIGHTFACE_VERSION}', v"; \
    python -c "import onnxruntime as o; print('onnxruntime', o.__version__, o.get_available_providers())"; \
    install -m 0644 /tmp/ctx/00_podh_latentsync.py /opt/ComfyUI/custom_nodes/00_podh_latentsync.py; \
    python /opt/ComfyUI/custom_nodes/00_podh_latentsync.py probe /opt/ComfyUI/custom_nodes/ComfyUI-LatentSyncWrapper; \
    sh /opt/podh/quick-test "$QUICK_TEST_STRICT"

# --- 3. identity: the manifest and the node catalogue, this image's own ---------------------
# As the base's layer 9: the catalogue re-dumped with this image's packs loaded, and the
# manifest re-written from what is installed, extending the base's (kept as `parent`).
ARG IMAGE_VERSION=dev
ARG GIT_SHA=unknown
ARG SOURCE_URL=
RUN --mount=type=bind,source=docker/comfyui/write_manifest.py,target=/tmp/write_manifest.py \
    set -eux; \
    export PYTHONDONTWRITEBYTECODE=1; \
    podh-catalogue dump --start --out /opt/podh/catalogue.json --timeout 900 \
        --image podh-comfyui-video --version "$IMAGE_VERSION" --min-nodes 300 \
        --require-pack ComfyUI-VideoHelperSuite --require-pack ComfyUI-LatentSyncWrapper \
        --require-pack ComfyUI_Fill-ChatterBox; \
    git -C /opt/ComfyUI clean -fdxq -e custom_nodes; \
    rm -rf /root/.cache; \
    python /tmp/write_manifest.py --image podh-comfyui-video --version "$IMAGE_VERSION" \
        --revision "$GIT_SHA" --source "$SOURCE_URL" --extends /opt/podh/manifest.json \
        --catalogue /opt/podh/catalogue.json --out /opt/podh/manifest.json; \
    chmod 0644 /opt/podh/catalogue.json

ARG BASE_IMAGE
LABEL org.opencontainers.image.title="podh-comfyui-video" \
      org.opencontainers.image.description="podh-comfyui plus lip-sync and voice node packs" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}" \
      io.podh.manifest="image=podh-comfyui-video;version=${IMAGE_VERSION};revision=${GIT_SHA};parent=podh-comfyui;cuda=12.8;python=3.12;manifest=/opt/podh/manifest.json;catalogue=/opt/podh/catalogue.json"
