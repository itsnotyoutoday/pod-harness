# FastSpeech2 training image, built on coqui.
#
# Smaller and simpler than the StyleTTS2 image and deliberately so: coqui ships the trainer,
# so there is nothing to vendor and no upstream loop to keep in sync. That is this repo's
# entire argument for existing as a fallback.
#
# The transformers and torchcodec pins are load-bearing and were both found by importing,
# not by reading: coqui calls `isin_mps_friendly` (transformers >= 4.45) and requires
# torchcodec for audio IO from torch 2.9. Neither failure names the responsible package.
FROM --platform=linux/amd64 nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/app \
    TTS_HOME=/models/coqui \
    HF_HOME=/models/hf

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip git curl ca-certificates \
        ffmpeg libsndfile1 espeak-ng espeak-ng-data tini; \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python; \
    apt-get clean; rm -rf /var/lib/apt/lists/*; \
    mkdir -p /app /models/coqui /models/hf /workspace

RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu124 \
        "torch>=2.2" "torchaudio>=2.2"

RUN python -m pip install --no-cache-dir \
        "coqui-tts>=0.24" "transformers>=4.45,<5" torchcodec \
        "numpy<2.0" "scipy>=1.11" "soundfile>=0.12" "librosa>=0.10" "boto3>=1.34" \
        "pod-harness @ git+https://github.com/itsnotyoutoday/pod-harness@main"


# The harness, exactly as every other image embeds it. The WORKLOAD's code is deliberately
# absent: `podh-code` pulls the published code tree onto the pod at job time, which is why
# `runctl launch` reports a tree hash and why editing a stage does not rebuild an image.
# Baking code/ in would fork that: the image would carry one version and the codestore
# another, and the pod would run whichever the entrypoint happened to find first.
COPY --chmod=0755 docker/harness/podh-init docker/harness/podh-mount docker/harness/podh-code \
     docker/harness/podh-publish docker/harness/podh-logs docker/harness/podh-roots \
     docker/harness/podh-prepare docker/harness/podh-preflight docker/harness/podh-watchdog \
     /usr/local/bin/
COPY docker/harness/Caddyfile /etc/caddy/Caddyfile
COPY serve/ /app/serve/
COPY src/pod_harness/ /app/pod_harness/
COPY contract.json /app/contract.json

# The independence guard the other images carry: no loader module may appear here. A pod
# cannot launch a pod because the code is not present, not because it was asked not to.
COPY docker/assert_independence.py /tmp/assert_independence.py
RUN python /tmp/assert_independence.py && rm /tmp/assert_independence.py

RUN echo "=== ABI check ===" \
 && python -c "\
import torch, librosa, soundfile, numpy, scipy, sys; \
import TTS; from TTS.tts.configs.fastspeech2_config import Fastspeech2Config; \
import pod_harness, pod_harness.framework, pod_harness.execute_job, pod_harness.stage_manifest; \
a = Fastspeech2Config().model_args; \
assert a.use_pitch and a.use_energy, 'pitch/energy predictors absent — this image is pointless without them'; \
print('imports OK on', sys.version.split()[0]); \
print('torch', torch.__version__, '| cuda', torch.version.cuda); \
print('predictors: pitch, energy, duration + speaker embedding')"

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "pod_harness.execute_job"]
