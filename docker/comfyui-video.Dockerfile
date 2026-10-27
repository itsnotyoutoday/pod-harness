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
# the guard as PIP_CONSTRAINT, packs baked the addons stage's way and recorded in
# $PODH_BAKED_ROOT/addons.json, models never baked.
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
# own. onnxruntime-gpu 1.24 runs on CUDA 12 + cuDNN 9, which the base image carries.
RUN --mount=type=bind,from=insightface,source=/wheels,target=/tmp/wheels \
    set -eux; \
    grep -qx "onnxruntime-gpu==${ONNXRUNTIME_GPU_VERSION}" /opt/podh/lock.txt; \
    pip install --no-input -c /opt/podh/lock.txt \
        "onnxruntime-gpu==${ONNXRUNTIME_GPU_VERSION}" \
        /tmp/wheels/insightface-${INSIGHTFACE_VERSION}-*.whl

# --- 2. the video packs, baked ----------------------------------------------------------------
# LatentSyncWrapper checks for pytorch_lightning and accelerate the first time its node is
# used and pip-installs whatever is missing — and, because it looks `ffmpeg-python` up as a
# module name, it would reinstall that one on every pod. Both are installed here, and the
# flag file it writes after a successful check is written now, so a pod never runs pip from
# inside a node.
RUN set -eux; \
    bake=/opt/podh/podh-bake-node; \
    $bake ComfyUI-LatentSyncWrapper https://github.com/ShmuelRonen/ComfyUI-LatentSyncWrapper 360d5283d7276aee68b4237b1387e594e4ce640e; \
    $bake ComfyUI_Fill-ChatterBox https://github.com/filliptm/ComfyUI_Fill-ChatterBox f7d7a16187430abcaf91a3039b9c83aa9960816a; \
    pip install --no-input -c /opt/podh/lock.txt pytorch-lightning accelerate; \
    echo "Dependencies installed at image build" > /root/.latentsync16_dependencies_installed; \
    cat /opt/podh/baked/addons.json

# --- 3. the image proves itself ----------------------------------------------------------------
ARG QUICK_TEST_STRICT=1
RUN set -eux; \
    python /opt/podh/check_image.py \
        --import insightface onnxruntime decord librosa diffusers s3tokenizer \
        --dist onnxruntime-gpu transformers diffusers librosa; \
    python -c "import importlib.metadata as m; v = m.version('insightface'); assert v == '${INSIGHTFACE_VERSION}', v"; \
    python -c "import onnxruntime as o; print('onnxruntime', o.__version__, o.get_available_providers())"; \
    sh /opt/podh/quick-test "$QUICK_TEST_STRICT"
