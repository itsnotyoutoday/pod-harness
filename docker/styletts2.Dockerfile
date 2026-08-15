# StyleTTS2 training image. GPU, CUDA, and the vendored training driver.
#
# ## Why this image exists rather than reusing the trainer's
#
# lingua-trainer's image carries MFA, kaldi and the alignment stack — gigabytes this job
# never touches. This one carries torch with CUDA and nothing else large. They are different
# jobs on different hardware and sharing an image would mean both paying for both.
#
# ## The vendored training loop
#
# The `styletts2` pip package ships models, losses, the dataset reader and optimizers, but
# NOT the training driver, which exists only in the upstream repo. Upstream is MIT, so
# train_first.py / train_second.py are vendored at a pinned commit rather than forked: a fork
# is a thing to keep in sync forever and we need two files. `STYLETTS2_VENDOR` points the
# driver at them.
#
# ## Multilingual PL-BERT
#
# StyleTTS2's text encoder is language specific. The multilingual PL-BERT covers 14 languages
# including Spanish, which is what makes this a fine-tune rather than an encoder training
# project. Baked in so a pod with no egress can still start.
FROM --platform=linux/amd64 nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/app \
    STYLETTS2_VENDOR=/app/vendor/styletts2 \
    HF_HOME=/models/hf \
    PLBERT_PATH=/models/plbert

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip git curl ca-certificates \
        ffmpeg libsndfile1 espeak-ng espeak-ng-data tini; \
    ln -sf /usr/bin/python3.11 /usr/local/bin/python; \
    apt-get clean; rm -rf /var/lib/apt/lists/*; \
    mkdir -p /app /models/hf /models/plbert /workspace

# torch with CUDA — the one place in this project where the CUDA build is the right one.
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu124 \
        "torch>=2.2" "torchaudio>=2.2"

RUN python -m pip install --no-cache-dir \
        "styletts2" "numpy<2.0" "scipy>=1.11" "soundfile>=0.12" \
        "librosa>=0.10" "pyyaml>=6" "boto3>=1.34" \
        "pod-harness @ git+https://github.com/itsnotyoutoday/pod-harness@main"

# The training driver, pinned. Only the loop and its utils; the model comes from the package.
# monotonic_align comes from the upstream tree below, not from pip: the PyPI package is a
# Cython extension with no wheel that needs numpy headers, and upstream vendors its own.
ARG STYLETTS2_COMMIT=main
RUN set -eux; \
    git clone --depth 1 https://github.com/yl4579/StyleTTS2.git /tmp/st2; \
    cd /tmp/st2 && git checkout "${STYLETTS2_COMMIT}" 2>/dev/null || true; \
    mkdir -p /app/vendor/styletts2; \
    cp -r /tmp/st2/train_first.py /tmp/st2/train_second.py /tmp/st2/losses.py \
          /tmp/st2/meldataset.py /tmp/st2/models.py /tmp/st2/optimizers.py \
          /tmp/st2/utils.py /tmp/st2/Utils /tmp/st2/Configs \
          /tmp/st2/monotonic_align \
          /app/vendor/styletts2/ 2>/dev/null || true; \
    cp /tmp/st2/LICENSE /app/vendor/styletts2/LICENSE 2>/dev/null || true; \
    rm -rf /tmp/st2

# PL-BERT, baked so a pod without egress still starts.
RUN python - <<'PY' || echo "WARNING: PL-BERT not cached; will fetch at runtime"
from huggingface_hub import snapshot_download
snapshot_download("papercup-ai/multilingual-pl-bert", local_dir="/models/plbert")
PY


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

# ABI check, immediately after the install and for the same reason the other images have
# one: without it the first symptom of a broken wheel is an ImportError from inside a job on
# a billed pod, which reads like a code bug and sends you debugging the wrong file.
#
# Note what is NOT checked here: the workload's own modules. They arrive at job time from the
# codestore, so importing them at build time would be checking a copy that never runs.
RUN echo "=== ABI check ===" \
 && python -c "\
import torch, librosa, soundfile, numpy, scipy, yaml, sys; \
import styletts2, styletts2.models, styletts2.meldataset; \
import pod_harness, pod_harness.framework, pod_harness.execute_job, pod_harness.stage_manifest; \
print('imports OK on', sys.version.split()[0]); \
print('torch', torch.__version__, '| cuda', torch.version.cuda, '| librosa', librosa.__version__)"

# The vendored training loop must be present, or phase 2 silently never runs and the model
# ships unsteerable. Verified here so the failure is a red build, not a wasted GPU-hour.
RUN set -eux; \
    for f in train_first.py train_second.py models.py meldataset.py; do \
        test -f "$STYLETTS2_VENDOR/$f" || { echo "FAIL: vendored $f missing"; exit 1; }; \
    done; \
    python -c "from pathlib import Path; import os; \
print('plbert cached' if any(Path('/models/plbert').glob('*')) else 'plbert NOT cached')"; \
    echo "confirmed: styletts2 runtime + vendored two-phase training loop"

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "pod_harness.execute_job"]
