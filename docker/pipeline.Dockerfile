# lingua-pipeline — the build-zone image: forced alignment plus the measurement stack.
#
# ## What is in here and what is deliberately not
#
# IN:  third-party dependencies and third-party pretrained weights. Nothing proprietary.
# OUT: the pipeline code, the corpora, the rulesets, the audio. Those arrive at runtime
#      from the volume / S3.
#
# That split is what makes the GHCR package PUBLIC — and a public package avoids both
# registry auth on every pod template and the Docker Hub anonymous pull rate-limit that
# plexus kept hitting from RunPod datacentre IPs. It also means a code change needs no
# image rebuild at all.
#
# ## Why the OFFICIAL MFA image is the base
#
# Two CI builds died trying to hand-assemble a conda environment here:
#
#   1. pip-installed scipy against Ubuntu's older libstdc++ ->
#      ImportError: ... version `CXXABI_1.3.15' not found
#   2. moving the compiled stack to conda-forge ->
#      libmamba: Could not solve for environment specs
#
# Both are the same underlying problem: MFA pins a large, opinionated dependency set, and
# any pin of ours has to agree with it. Upstream already solved that, and their solution is
# published. `runners/batch_pod.py` reached the same conclusion independently and runs this
# exact image on pods today.
#
# What it already carries, verified by inspection:
#
#   MFA 3.4.2, python 3.13, conda env at /env
#   numpy 2.4.6  scipy 1.18.0  librosa 0.11.0  soundfile 0.14.0  scikit-learn 1.9.0
#   torch 2.8.0 — CPU-ONLY (torch.version.cuda is None), which is exactly what we want:
#                 the measurement stages are CPU-bound and CUDA would add ~8 GB
#   ffmpeg + ffprobe on PATH, tar, tini, git
#
# So this file adds six pip packages, three static binaries, the weights and the harness.
#
# ## The size trade, stated honestly
#
# The base is ~5.3 GB and includes things Spanish work never touches — sudachidict_core
# (208 MB, Japanese), pythainlp (64 MB, Thai), statsmodels, sympy. We cannot delete those
# to shrink the pull: removing a file in a later layer only MASKS it, the base's bytes
# still ship. Genuinely shrinking it needs a multi-stage `COPY --from` that flattens a
# pruned /env into one layer. That is a worthwhile follow-up, deliberately not attempted
# before the first green build.
#
# Offsetting it: `batch_pod.py` already pulls this image, so RunPod machines in our pool
# may hold its layers, and a pull that hits cache costs nothing.
#
# ## Pinned by digest
#
# `:latest` would let an upstream rebuild change our image with no commit from us. To
# update deliberately:
#   docker pull mmcauliffe/montreal-forced-aligner:latest
#   docker image inspect … --format '{{index .RepoDigests 0}}'
FROM --platform=linux/amd64 mmcauliffe/montreal-forced-aligner@sha256:33ce62903cc9b213324634ef2461c3b32ff96c648035e18bc6112628491bb41d

# The base runs as mfauser. RunPod pods run as root, and a non-root user cannot write to
# the network volume mount without extra setup.
USER root

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PY=/env/bin/python

# --- 1. the few system tools the base lacks -------------------------------------------
# ffmpeg, ffprobe, tar, tini and git are already present in /env — only these are missing.
# fuse3 provides fusermount3, needed to unmount cleanly in FuseMount.publish().
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        curl ca-certificates unzip procps fuse3 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --- 2. static tooling ----------------------------------------------------------------
# caddy serves logs and proxies /v1; runpodctl is best-effort self-delete; rclone backs
# the FuseMount strategy. All three are single static binaries.
ARG CADDY_VERSION=2.8.4
RUN curl -sL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin caddy \
 && chmod +x /usr/local/bin/caddy \
 && curl -sL https://github.com/runpod/runpodctl/releases/latest/download/runpodctl-linux-amd64 \
        -o /usr/local/bin/runpodctl \
 && chmod +x /usr/local/bin/runpodctl \
 && curl -sL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o /tmp/rc.zip \
 && cd /tmp && unzip -q rc.zip && mv rclone-*/rclone /usr/local/bin/rclone \
 && chmod +x /usr/local/bin/rclone && rm -rf /tmp/rc.zip /tmp/rclone-*

# --- 3. the handful of python packages the base lacks ---------------------------------
# Into the base's own interpreter, so there is one environment and one libstdc++. These
# are pure-python or thin wrappers over what is already installed, so no ABI question
# arises — which is the entire reason for adopting this base.
WORKDIR /app
COPY requirements-pipeline.txt requirements-serve.txt ./
RUN $PY -m pip install --no-cache-dir \
        -r requirements-pipeline.txt -r requirements-serve.txt

# --- lingua-core: the stage engine ------------------------------------------------------
# The image ships the ENGINE (stage model, runner, resume, status protocol) but never a
# workload's stages — those arrive at runtime from the volume. Pinned by ref so an image
# rebuild is the only thing that can change the engine under a running fleet.
ARG LINGUA_CORE_REF=main
RUN $PY -m pip install --no-cache-dir \
        "git+https://github.com/itsnotyoutoday/lingua-core.git@${LINGUA_CORE_REF}"

# --- 3b. ABI check, immediately after the install --------------------------------------
# Imports every compiled extension in dependency order. Without this the first symptom of
# a mismatch is an ImportError raised from inside a model download in the next layer,
# which reads like a network problem and sends you debugging the wrong thing.
RUN echo "=== ABI check ===" \
 && $PY -c "\
import numpy, scipy, scipy.spatial, sklearn, librosa, soundfile, torch, torchaudio, speechbrain, sys; \
import lingua_core, lingua_core.framework, lingua_core.execute_job; \
print('compiled stack imports OK on', sys.version.split()[0]); \
print('numpy', numpy.__version__, '| scipy', scipy.__version__, \
      '| torch', torch.__version__, '| cuda', torch.version.cuda)"

# --- 4. baked weights ------------------------------------------------------------------
# MUST live outside /workspace: RunPod mounts the network volume there, and a mount
# SHADOWS whatever the image had at that path — so weights baked under the mount point are
# invisible at runtime and download again anyway. lingua-seed-models links these into the
# cache tree at container start.
# HF_HOME matters more than it looks. speechbrain's `savedir` holds SYMLINKS into the
# huggingface cache, not copies — so the real 85 MB of ECAPA weights live wherever HF_HOME
# points. Left at its default that is /root/.cache/huggingface, which means the baked
# weights would sit outside /opt and the symlink chain would depend on nothing ever
# shadowing /root. Pointing it into /opt keeps the whole bake self-contained under one
# path, consistent with the rule that baked artifacts never live where a mount can hide
# them. It is set again in the runtime ENV below so the pipeline resolves the same cache.
ENV LINGUA_MODEL_ROOT=/opt/models \
    LINGUA_BAKED_MFA=/opt/mfa \
    HF_HOME=/opt/models/hf

# MFA_ROOT_DIR is set INLINE for the download only, never as a persistent ENV.
#
# It has two different correct values at two different times, and collapsing them into one
# ENV silently broke the seeding. At BUILD time it must point into /opt so the models bake
# outside any future mount point. At RUN time it must point at writable volume storage,
# because MFA writes corpus and temporary state under this root during alignment — with it
# left at /opt that state lands on the container disk (20 GB on the pod we tested) instead
# of the volume, which is fine for a smoke test and wrong for a real alignment run.
#
# Setting both to /opt/mfa also made lingua-seed-models a no-op: source and destination
# were the same path, so it logged "keep … not overriding" and linked nothing. Verified on
# a real pod. The runtime value is set in the ENV block further down.
RUN mkdir -p /opt/models /opt/mfa \
 && MFA_ROOT_DIR=/opt/mfa mfa model download acoustic spanish_mfa \
 && MFA_ROOT_DIR=/opt/mfa mfa model download dictionary spanish_mfa \
 && $PY -c "\
from speechbrain.inference.speaker import EncoderClassifier; \
EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', \
                               savedir='/opt/models/speechbrain/ecapa'); \
print('ECAPA cached')" \
 && du -sh /opt/mfa /opt/models

# --- 5. harness + control API ----------------------------------------------------------
# One COPY for the scripts rather than one per file: seven layers holding 25 KB is pure
# manifest overhead.
COPY docker/harness/Caddyfile /etc/caddy/Caddyfile
COPY docker/harness/lingua-init docker/harness/lingua-preflight \
     docker/harness/lingua-watchdog docker/harness/lingua-self-delete \
     docker/harness/lingua-seed-models docker/harness/lingua-mount \
     /usr/local/bin/
RUN chmod +x /usr/local/bin/lingua-*

COPY serve/ /app/serve/

# --- 6. runtime defaults ---------------------------------------------------------------
# CACHE_ROOT and the log root sit on the volume: writable, growable, surviving the pod.
# Only the baked subdirectories resolve into /opt, via symlink.
#
# PYTHONPATH puts /workspace/code AHEAD of the baked /app. That single ordering is what
# makes the deps-only doctrine work at runtime: the image ships dependencies, the volume
# ships CODE, and editing a stage is a few-KB upload rather than a 5 GB image rebuild.
# batch_pod.py has always exported this inline in its start command; setting it as an image
# default means every launch path gets it — LINGUA_MODE=batch, a job submitted through /v1,
# or an interactive shell — instead of only the one launcher that remembered.
#
# Missing it does not error. It silently runs the BAKED code while you believe you are
# running what you just synced, which is the "succeeded against the wrong thing" failure
# runners/framework.py exists to catch.
ENV LINGUA_CORPUS_ROOT=/workspace/corpus \
    LINGUA_OUT_ROOT=/workspace/out \
    LINGUA_CACHE_ROOT=/workspace/.cache \
    LINGUA_LOG_ROOT=/workspace/logs \
    LINGUA_MANIFEST=/workspace/manifest/corpus_research.json \
    MFA_ROOT_DIR=/workspace/.cache/mfa \
    LINGUA_API_PORT=8010 \
    LINGUA_SERVE_API=1 \
    PYTHONPATH=/workspace/code:/app \
    HF_HOME=/opt/models/hf \
    PATH=/env/bin:$PATH

RUN mkdir -p /workspace/corpus /workspace/out /workspace/logs /workspace/manifest

# --- 7. build-time sanity check ---------------------------------------------------------
# Fails the image, and therefore the GHA run, the moment a dependency is broken or landed
# on a different interpreter than `python3` resolves to. plexus added this after a real
# incident where pip succeeded and the runtime still raised ModuleNotFoundError.
RUN echo "=== runtime sanity check ===" \
 && echo "python3 -> $(readlink -f $(which python3))" \
 && python3 --version \
 && python3 -c "import serve.jobs, serve.code, serve.api; \
import lingua_core.events, lingua_core.registry, lingua_core.resume; \
print('harness + engine imports OK')" \
 && ffmpeg -version | head -1 \
 && mfa version \
 && test -d /opt/models/speechbrain/ecapa || (echo 'ECAPA not baked' && exit 1) \
 && python3 -c "\
import os; \
p='/opt/models/speechbrain/ecapa/embedding_model.ckpt'; \
assert os.path.exists(os.path.realpath(p)), 'ECAPA symlink dangles: '+os.path.realpath(p); \
sz=os.path.getsize(os.path.realpath(p)); \
assert sz > 10_000_000, f'ECAPA weights look truncated: {sz} bytes'; \
print(f'ECAPA weights resolve, {sz/1e6:.1f} MB')" \
 && test -d /opt/mfa/pretrained_models || (echo 'MFA models not baked' && exit 1) \
 && runpodctl version \
 && caddy version \
 && rclone version | head -1 \
 && echo "=== image is good ==="

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/lingua-init"]
CMD []
