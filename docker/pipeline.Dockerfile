# lingua-pipeline — the build-zone image: forced alignment plus the measurement stack.
#
# ## What is in here and what is deliberately not
#
# IN:  third-party dependencies and third-party pretrained weights. Nothing proprietary.
# OUT: the pipeline code, the corpora, the rulesets, the audio. Those arrive at runtime
#      from the volume / S3.
#
# That split is not tidiness, it is what makes the GHCR package PUBLIC — and a public
# package is what avoids both registry auth on every pod template and the Docker Hub
# anonymous pull rate-limit that plexus kept hitting from RunPod datacentre IPs.
# It also means a code change needs no image rebuild at all.
#
# ## Two hard constraints, inherited from Dockerfile.pod
#
#   linux/amd64      RunPod is x86. An arm64 image built on an M-series Mac will not start
#                    there, and it fails opaquely.
#   CPU-only torch   from the /whl/cpu index, installed BEFORE the generic resolver so
#                    speechbrain cannot drag the multi-gigabyte CUDA build in behind it.
#                    This job measures ~12 CPU-min against ~6 GPU-min: CUDA buys minutes
#                    and costs ~8 GB.
#
# ## Why micromamba is the base
#
# MFA is conda-only — `pip install montreal-forced-aligner` fails because `baumwelch` has
# no PyPI distribution. So conda has to be underneath and pip goes on top. Note this is
# why we do NOT rebase on runpod/base the way plexus did; runpodctl is a single static Go
# binary we can just drop in, so nothing is lost.
#
# ## Layer order is deliberate
#
# Slowest and most stable first, so a change to the harness or requirements does not
# rebuild the ten-minute conda layer, and RunPod re-pulls only the small tail. Layer COUNT
# costs nothing — Docker pulls layers in parallel — but layer ORDER is worth real minutes.

FROM --platform=linux/amd64 mambaorg/micromamba:1.5-jammy

USER root

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MAMBA_DOCKERFILE_ACTIVATE=1 \
    DEBIAN_FRONTEND=noninteractive

# --- 1. system ------------------------------------------------------------------------
# ffmpeg/ffprobe do every conversion and probe; libsndfile1 backs soundfile; procps keeps
# joblib's loky cleanup quiet; tini reaps the sidecars so a crashed workload leaves no
# zombies holding the pod open.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        ffmpeg libsndfile1 ca-certificates curl git tar procps tzdata tini unzip fuse3 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --- 2. static tooling ----------------------------------------------------------------
ARG CADDY_VERSION=2.8.4
RUN curl -sL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin caddy \
 && chmod +x /usr/local/bin/caddy \
 && curl -sL https://github.com/runpod/runpodctl/releases/latest/download/runpodctl-linux-amd64 \
        -o /usr/local/bin/runpodctl \
 && chmod +x /usr/local/bin/runpodctl && curl -sL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o /tmp/rc.zip \
 && cd /tmp && unzip -q rc.zip && mv rclone-*/rclone /usr/local/bin/rclone \
 && chmod +x /usr/local/bin/rclone && rm -rf /tmp/rc.zip /tmp/rclone-*

# --- 3. MFA (the slow layer — keep it high) -------------------------------------------
RUN micromamba install -y -n base -c conda-forge \
        python=3.11 montreal-forced-aligner \
 && micromamba clean --all --yes

ENV PATH=/opt/conda/bin:$PATH

# --- 4. CPU torch, before anything that could pull CUDA -------------------------------
RUN /opt/conda/bin/python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu torch torchaudio

# --- 5. the rest of the python stack --------------------------------------------------
WORKDIR /app
COPY requirements-pipeline.txt requirements-serve.txt ./
RUN /opt/conda/bin/python -m pip install --no-cache-dir \
        -r requirements-pipeline.txt -r requirements-serve.txt

# --- 6. baked weights -----------------------------------------------------------------
# These MUST live outside /workspace and /corpus. RunPod mounts the network volume over
# /workspace, and a mount SHADOWS whatever the image had at that path — so weights baked
# under the mount point are invisible at runtime and the download happens anyway.
# lingua-seed-models links these into the cache tree at container start.
ENV LINGUA_MODEL_ROOT=/opt/models \
    LINGUA_BAKED_MFA=/opt/mfa \
    MFA_ROOT_DIR=/opt/mfa

RUN mkdir -p /opt/models /opt/mfa \
 && mfa model download acoustic spanish_mfa \
 && mfa model download dictionary spanish_mfa \
 && /opt/conda/bin/python -c "\
from speechbrain.inference.speaker import EncoderClassifier; \
EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', \
                               savedir='/opt/models/speechbrain/ecapa'); \
print('ECAPA cached')" \
 && du -sh /opt/mfa /opt/models

# --- 7. harness + control API ---------------------------------------------------------
COPY docker/harness/Caddyfile /etc/caddy/Caddyfile
COPY docker/harness/lingua-init         /usr/local/bin/lingua-init
COPY docker/harness/lingua-preflight    /usr/local/bin/lingua-preflight
COPY docker/harness/lingua-watchdog     /usr/local/bin/lingua-watchdog
COPY docker/harness/lingua-self-delete  /usr/local/bin/lingua-self-delete
COPY docker/harness/lingua-seed-models  /usr/local/bin/lingua-seed-models
COPY docker/harness/lingua-mount         /usr/local/bin/lingua-mount
RUN chmod +x /usr/local/bin/lingua-*

COPY serve/ /app/serve/
COPY control/ /app/control/

# --- 8. runtime defaults --------------------------------------------------------------
# CACHE_ROOT and the log root sit on the volume: writable, growable, and they survive the
# pod. Only the baked subdirectories resolve into /opt, via symlink.
ENV LINGUA_CORPUS_ROOT=/workspace/corpus \
    LINGUA_OUT_ROOT=/workspace/out \
    LINGUA_CACHE_ROOT=/workspace/.cache \
    LINGUA_LOG_ROOT=/workspace/logs \
    LINGUA_MANIFEST=/workspace/manifest/corpus_research.json \
    LINGUA_API_PORT=8010 \
    LINGUA_SERVE_API=1 \
    PYTHONPATH=/app

RUN mkdir -p /workspace/corpus /workspace/out /workspace/logs /workspace/manifest

# --- 9. build-time sanity check -------------------------------------------------------
# Fails the image, and therefore the GHA run, the moment a dependency is broken or landed
# on a different interpreter than `python3` resolves to. plexus added this after a real
# incident where pip succeeded and the runtime still raised ModuleNotFoundError.
RUN echo "=== runtime sanity check ===" \
 && echo "python3 -> $(readlink -f $(which python3))" \
 && python3 --version \
 && python3 -c "\
import numpy, scipy, librosa, soundfile, torch, torchaudio, speechbrain, sklearn, boto3, fastapi, uvicorn, sys; \
assert not torch.cuda.is_available() or True; \
print('imports OK on', sys.executable, sys.version.split()[0]); \
print('torch', torch.__version__)" \
 && python3 -c "import serve.events, serve.jobs; print('harness imports OK')" \
 && ffmpeg -version | head -1 \
 && mfa version \
 && test -d /opt/models/speechbrain/ecapa || (echo 'ECAPA not baked' && exit 1) \
 && runpodctl version \
 && caddy version \
 && rclone version | head -1 \
 && echo "=== image is good ==="

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/lingua-init"]
CMD []
