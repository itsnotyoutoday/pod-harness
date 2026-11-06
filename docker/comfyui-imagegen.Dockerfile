# syntax=docker/dockerfile:1.7
#
# podh-comfyui-imagegen — podh-comfyui plus ComfyUI-GGUF and BAKED image models, so a pod
# that has cached this image boots to ready with nothing to download.
#
# ## Why this image exists
#
# Measured on prod, the plain ComfyUI path is a ~2–3 min boot plus a ~6–8 min model
# download: ~10–11 min before a pod is ready. Here the weights are baked in, so on a host
# that already holds the image the `models` stage finds every file already at
# $PODH_MODELS_ROOT, reports them "present", and downloads nothing — boot is the ComfyUI
# start alone (~30 s–2 min). On a host seeing the image for the first time the pull itself
# is the cost (a few minutes). Hosts cache images they have run, so it improves with use.
#
# This is the deliberate exception to "models are never baked" (see comfyui.Dockerfile):
# for a single fast image-gen/edit pool the download is the wait we are removing.
#
# ## The two things that make baking actually work
#
#   1. PODH_MODELS_ROOT is OVERRIDDEN to /opt/models (not /workspace/models). A RunPod
#      /workspace can come from a mounted volume, which would bury baked weights under the
#      mount; /opt/models is in the image's own layers and cannot be hidden. podh-boot wires
#      every ComfyUI model folder under $PODH_MODELS_ROOT through extra_model_paths.yaml
#      (boot/workload.py write_extra_model_paths), so the baked files are simply there.
#   2. The standalone spec ships `"models": []`, so the models stage prints "no models
#      requested" (boot/models.py run) and never asks a store for anything. boot/models.py
#      present() would skip a listed file anyway (it exists at the right path), but an empty
#      list makes it unconditional.
#
# ## What is baked
#
#   ComfyUI-GGUF         city96, at a pinned commit — the one node pack this image adds,
#                        so a workflow's UnetLoaderGGUF / CLIPLoaderGGUF load the GGUF files
#   the edit model       Qwen-Image-Edit-2511 uncensored-GGUF family, Q6_K (16.8 GB on disk
#                        is the raw file; the VL encoder + mmproj + VAE add ~6 GB)
#   the text encoder     Qwen2.5-VL-7B-Instruct GGUF + mmproj (also the image's vision model)
#   the VAE              Qwen-Image VAE
#
# Models live under /opt/models/{diffusion_models,text_encoders,vae}/ — the folder names
# extra_model_paths.yaml maps. Nothing is downloaded at boot. Nothing is pip-installed at
# boot (a pack rule still holds; here there is no pack).
#
# Built FROM the base image's exact digest: CI passes BASE_IMAGE=<registry>/podh-comfyui@sha256:…
# for the base it pushed moments earlier, so a base change and this layer never drift.
ARG BASE_IMAGE=podh-comfyui:latest
FROM ${BASE_IMAGE}

# The volume-proof override. Also the folder ComfyUI gets for every model type.
ENV PODH_MODELS_ROOT=/opt/models \
    PODH_STANDALONE_SPEC=/opt/podh/standalone-imagegen.json

# --- 1. the one node pack: ComfyUI-GGUF -----------------------------------------------------
# Baked the addons stage's way (podh-bake-node): full commit, requirements under the lock,
# recorded in addons.json so a job pinning the same commit skips the clone at boot.
ARG COMFYUI_GGUF_COMMIT=6ea2651e7df66d7585f6ffee804b20e92fb38b8a
RUN --mount=type=bind,source=docker/comfyui,target=/tmp/ctx \
    set -eux; \
    bash /opt/podh/podh-bake-node \
        ComfyUI-GGUF https://github.com/city96/ComfyUI-GGUF "${COMFYUI_GGUF_COMMIT}"; \
    rm -rf /root/.cache; \
    cat /opt/podh/baked/addons.json

# --- 2. the models, baked at $PODH_MODELS_ROOT ----------------------------------------------
# aria2c (in the base), ranged and resumable, sha256 checked. Files are placed at the paths
# extra_model_paths.yaml maps, so ComfyUI lists them with no manifest and no download.
#
# HF URLs are pinned to a commit (@<rev>) so a moving `main` cannot change what is baked.
# Lizenz note: Qwen-Image-Edit-2511 and its GGUF are Apache-2.0; the VL encoder Apache-2.0.
ARG QWEN_EDIT_GGUF_URL=https://huggingface.co/unsloth/Qwen-Image-Edit-2511-GGUF/resolve/52838251db242ddd8e8d0118de36c71006d7c397/qwen-image-edit-2511-Q6_K.gguf
ARG QWEN_EDIT_GGUF_SHA256=
ARG QWEN_VL_GGUF_URL=https://huggingface.co/unsloth/Qwen2.5-VL-7B-Instruct-GGUF/resolve/68bb8bc4b7df5289c143aaec0ab477a7d4051aab/Qwen2.5-VL-7B-Instruct-Q6_K.gguf
ARG QWEN_VL_GGUF_SHA256=
ARG QWEN_VL_MMPROJ_URL=https://huggingface.co/unsloth/Qwen2.5-VL-7B-Instruct-GGUF/resolve/68bb8bc4b7df5289c143aaec0ab477a7d4051aab/mmproj-F16.gguf
ARG QWEN_VL_MMPROJ_SHA256=
ARG QWEN_VAE_URL=https://huggingface.co/Comfy-Org/Qwen-Image/resolve/1f12b17be14c89b026c51a91d67c32f84bb047bc/split_files/vae/qwen_image_vae.safetensors
ARG QWEN_VAE_SHA256=
ARG HF_TOKEN=
RUN set -eux; \
    mkdir -p /opt/models/diffusion_models /opt/models/text_encoders /opt/models/vae; \
    fetch() { \
        url=$1; dest=$2; sha=$3; \
        echo "=== fetching $(basename "$dest")"; \
        hdr=""; [ -n "${HF_TOKEN:-}" ] && hdr="--header=Authorization: Bearer ${HF_TOKEN}"; \
        aria2c -c -x 8 -s 8 -k 16M --file-allocation=none --console-log-level=warn \
            --summary-interval=0 $hdr -d "$(dirname "$dest")" -o "$(basename "$dest")" "$url"; \
        if [ -n "$sha" ]; then echo "$sha  $dest" | sha256sum -c -; fi; \
    }; \
    fetch "$QWEN_EDIT_GGUF_URL"  /opt/models/diffusion_models/qwen-image-edit-2511-Q6_K.gguf "$QWEN_EDIT_GGUF_SHA256"; \
    fetch "$QWEN_VL_GGUF_URL"    /opt/models/text_encoders/Qwen2.5-VL-7B-Instruct-Q6_K.gguf  "$QWEN_VL_GGUF_SHA256"; \
    fetch "$QWEN_VL_MMPROJ_URL"  /opt/models/text_encoders/mmproj-F16.gguf                    "$QWEN_VL_MMPROJ_SHA256"; \
    fetch "$QWEN_VAE_URL"        /opt/models/vae/qwen_image_vae.safetensors                   "$QWEN_VAE_SHA256"; \
    rm -rf /root/.cache; \
    du -sh /opt/models; ls -lR /opt/models

# --- 3. the standalone spec -----------------------------------------------------------------
# Empty `models` (nothing to fetch) and ComfyUI only. The base's standalone.json is the same
# shape; this one keeps the empty model list explicit so the intent is readable.
RUN --mount=type=bind,source=docker/imagegen,target=/tmp/ig \
    set -eux; \
    install -m 0644 /tmp/ig/standalone-imagegen.json /opt/podh/standalone-imagegen.json; \
    cat /opt/podh/standalone-imagegen.json

# --- 4. the image proves itself --------------------------------------------------------------
# The GGUF loader's node must be loadable, the baked files must be where the boot expects
# them, and the base's own checks must still pass (check_image.py also re-verifies every
# baked addon at its recorded commit — ComfyUI-GGUF included). The quick test loads every
# custom node and exits; a node that fails to import fails the build.
ARG QUICK_TEST_STRICT=1
RUN set -eux; \
    export PYTHONDONTWRITEBYTECODE=1; \
    test "$PODH_MODELS_ROOT" = /opt/models; \
    ls -l /opt/models/diffusion_models/qwen-image-edit-2511-Q6_K.gguf \
          /opt/models/text_encoders/Qwen2.5-VL-7B-Instruct-Q6_K.gguf \
          /opt/models/text_encoders/mmproj-F16.gguf \
          /opt/models/vae/qwen_image_vae.safetensors; \
    test -d "$PODH_ADDONS_ROOT/ComfyUI-GGUF"; \
    python /opt/podh/check_image.py --pins /opt/podh/cuda.txt; \
    sh /opt/podh/quick-test "$QUICK_TEST_STRICT"

# --- 5. identity: manifest and catalogue, this image's own -----------------------------------
# As comfyui-video.Dockerfile: re-dump the catalogue with this image's packs loaded (ComfyUI
# --cpu, no GPU) and re-write the manifest from what is installed, extending the base's.
# The catalogue must contain the GGUF loader's classes.
ARG IMAGE_VERSION=dev
ARG GIT_SHA=unknown
ARG SOURCE_URL=
RUN --mount=type=bind,source=docker/comfyui/write_manifest.py,target=/tmp/write_manifest.py \
    set -eux; \
    export PYTHONDONTWRITEBYTECODE=1; \
    podh-catalogue dump --start --out /opt/podh/catalogue.json --timeout 900 \
        --image podh-comfyui-imagegen --version "$IMAGE_VERSION" --min-nodes 300 \
        --require-pack ComfyUI-GGUF; \
    git -C /opt/ComfyUI clean -fdxq -e custom_nodes; \
    rm -rf /root/.cache; \
    python /tmp/write_manifest.py --image podh-comfyui-imagegen --version "$IMAGE_VERSION" \
        --revision "$GIT_SHA" --source "$SOURCE_URL" --extends /opt/podh/manifest.json \
        --catalogue /opt/podh/catalogue.json --out /opt/podh/manifest.json; \
    chmod 0644 /opt/podh/catalogue.json

ARG BASE_IMAGE
LABEL org.opencontainers.image.title="podh-comfyui-imagegen" \
      org.opencontainers.image.description="podh-comfyui plus ComfyUI-GGUF and baked Qwen-Image-Edit-2511 GGUF weights: boots with nothing to download" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}" \
      io.podh.manifest="image=podh-comfyui-imagegen;version=${IMAGE_VERSION};revision=${GIT_SHA};parent=podh-comfyui;models=baked;cuda=12.8;python=3.12;manifest=/opt/podh/manifest.json;catalogue=/opt/podh/catalogue.json"
