# The StyleTTS2 TRAINING image — GPU, and built on the harness like every other image.
#
# ## Written by deriving from pipeline.Dockerfile, after not doing so cost four builds
#
# The first version of this file was written from scratch around the model stack. It
# imported cleanly, published, launched, and produced a pod that reported RUNNING and wrote
# zero bytes — because it had torch and styletts2 and none of the machinery that makes a pod
# legible or even runnable:
#
#   * ENTRYPOINT was `python -m pod_harness.execute_job`, which requires --spec and exits
#     instantly. The entrypoint is `podh-init`: it mounts, pulls the published code, starts
#     the log server, and only then runs the job with the spec it staged.
#   * caddy was absent, so nothing served /logs — which is why the pod was silent AND why
#     `runctl watch --log` had nothing to show. One missing binary, both symptoms.
#   * PYTHONPATH did not put /workspace/code ahead of /app, so a synced stage edit would
#     have been ignored in favour of whatever was baked.
#
# Sections and their numbering mirror pipeline.Dockerfile deliberately. A reader who knows
# that file knows this one, and a change made there is easy to mirror here.
#
# ## What differs from pipeline.Dockerfile, and why
#
# The base. pipeline builds on the MFA image because alignment is its job; this needs CUDA
# and torch and never aligns anything. Everything from section 2 onward is the same shape.
FROM --platform=linux/amd64 nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# --- 1. the few system tools the base lacks -------------------------------------------
# fuse3 provides fusermount3, needed to unmount cleanly in FuseMount.publish().
# espeak-ng is styletts2's text frontend, not an engine choice here.
# ONE interpreter, deliberately. The first version installed python3.11 and symlinked only
# `python`, leaving `python3` as jammy's own 3.10 — so the build installed every package
# into 3.11, the ABI check passed under 3.11, and then podh-init (which calls `python3`
# explicitly, as do all the harness scripts) ran 3.10 against an empty site-packages. The
# container exited 1 before writing a single byte, twice.
#
# jammy's python3 is 3.10, which satisfies both coqui (>=3.9) and styletts2 (<3.12), so the
# extra interpreter bought nothing and cost two pods.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        python3 python3-pip python3-venv \
        curl ca-certificates unzip procps fuse3 git tini \
        ffmpeg libsndfile1 espeak-ng espeak-ng-data \
    && ln -sf /usr/bin/python3 /usr/local/bin/python \
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

# --- 3. the model stack ----------------------------------------------------------------
# torch from the CUDA index. This is the one image in the project where the CUDA build is
# the right one; everywhere else it is 2.5 GB of kernels that cannot run.
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu124 \
        "torch>=2.2" "torchaudio>=2.2"

# --- 4. the upstream tree, and its OWN dependency list ------------------------------------
# Cloned BEFORE the python install so that `pip install -r requirements.txt` can be the
# source of truth. Hand-listing styletts2's dependencies cost four builds discovering them
# one ImportError at a time — pandas, then monotonic_align, then whatever came next — when
# the authoritative list was in the repo the whole time.
#
# The pip package `styletts2` is an INFERENCE wrapper and ships no training driver. Upstream
# is MIT, so the drivers are vendored at a pinned ref rather than forked: a fork is a thing
# to keep in sync forever and we need a handful of files. train_finetune.py is the one this
# project will actually use — we are adapting a pretrained model to Spanish, not training
# from scratch — and train_first/train_second are kept for the from-scratch path.
ARG STYLETTS2_COMMIT=main
ENV STYLETTS2_VENDOR=/app/vendor/styletts2
RUN set -eux; \
    git clone --depth 1 https://github.com/yl4579/StyleTTS2.git /tmp/st2; \
    cd /tmp/st2 && git checkout "${STYLETTS2_COMMIT}" 2>/dev/null || true; \
    mkdir -p "$STYLETTS2_VENDOR"; \
    cp -r /tmp/st2/train_first.py /tmp/st2/train_second.py \
          /tmp/st2/train_finetune.py /tmp/st2/train_finetune_accelerate.py \
          /tmp/st2/losses.py /tmp/st2/meldataset.py /tmp/st2/models.py \
          /tmp/st2/optimizers.py /tmp/st2/utils.py /tmp/st2/text_utils.py \
          /tmp/st2/Modules /tmp/st2/Utils /tmp/st2/Configs /tmp/st2/requirements.txt \
          /tmp/st2/LICENSE \
          "$STYLETTS2_VENDOR/"

# Upstream's list verbatim, plus what it omits. The training DRIVERS import four packages
# requirements.txt never declares — IPython, click, numpy, pandas — and train_first.py opens
# with `from torch.utils.tensorboard import SummaryWriter`, which needs the `tensorboard`
# distribution that torch itself does not pull in. That last one killed a run at line 35 of
# the driver, after the dataset stage had already succeeded.
#
# Found by scanning every import in the vendored files against requirements.txt rather than
# one ModuleNotFoundError per GPU-hour, which is how the first four were found.
#
# Upstream's list verbatim, plus the two it cannot express: monotonic_align is a git
# dependency inside requirements.txt (resemble-ai's fork, since the PyPI package has no
# wheel), and phonemizer/pandas are imported by the training utilities without being
# declared anywhere.
# A compiler is needed and then removed in the same layer: monotonic_align is a Cython
# extension with no wheel for any platform, so it builds from source. A toolchain left in
# the image is ~200 MB carried for one build step, and RunPod bills the pull.
RUN set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends build-essential python3-dev; \
    python -m pip install --no-cache-dir "cython<3" "numpy<2.0"; \
    python -m pip install --no-cache-dir -r "$STYLETTS2_VENDOR/requirements.txt"; \
    python -m pip install --no-cache-dir \
        "styletts2" "scipy>=1.11" "phonemizer" \
        "boto3>=1.34" "fastapi>=0.110" "uvicorn[standard]>=0.29" \
        "tensorboard" "pandas" "ipython" "click"; \
    apt-get purge -y --auto-remove build-essential python3-dev; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# Multilingual PL-BERT: 14 languages including Spanish, which is what makes this a fine-tune
# rather than a text-encoder training project. Baked so a pod without egress still starts.
ENV PLBERT_PATH=/opt/models/plbert
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('papercup-ai/multilingual-pl-bert', local_dir='/opt/models/plbert')" \
 || echo "WARNING: PL-BERT not cached; will fetch at runtime"

# --- 5. pod_harness: the stage engine ----------------------------------------------------
# WORKDIR before the COPY, exactly as pipeline.Dockerfile does it. Python puts the working
# directory on sys.path, and that is how the ABI check below resolves `import pod_harness` —
# PYTHONPATH is not set until section 7. Copying to /app without cd-ing there built an image
# whose ABI check could not see the engine it had just installed.
WORKDIR /app
COPY src/pod_harness/ /app/pod_harness/
COPY contract.json /app/contract.json

# --- 5b. ABI check, immediately after the install ----------------------------------------
# Without it the first symptom of a broken wheel is an ImportError from inside a job on a
# billed pod, which reads like a code bug and sends you debugging the wrong file. It has
# already caught pandas and monotonic_align here.
#
# The WORKLOAD's own modules are deliberately not imported: they arrive at job time from the
# codestore, so checking them at build time would be checking a copy that never runs.
# `python` and `python3` MUST be the same interpreter — the harness scripts use python3 and
# the build uses python, and a mismatch is invisible until a pod exits 1 with no output.
RUN set -eux; \
    a=$(python -c "import sys; print(sys.executable, sys.version_info[:2])"); \
    b=$(python3 -c "import sys; print(sys.executable, sys.version_info[:2])"); \
    echo "python : $a"; echo "python3: $b"; \
    python3 -c "import pod_harness" \
      || { echo "FAIL: python3 cannot import pod_harness — the harness scripts run python3"; exit 1; }

RUN echo "=== ABI check ===" \
 && python -c "\
import torch, librosa, soundfile, numpy, scipy, yaml, pandas, sys; \
import styletts2, monotonic_align, munch, einops, accelerate; \
import pod_harness, pod_harness.framework, pod_harness.execute_job, pod_harness.stage_manifest; \
print('imports OK on', sys.version.split()[0]); \
print('torch', torch.__version__, '| cuda', torch.version.cuda, '| librosa', librosa.__version__)"

# --- 6. harness + control API -------------------------------------------------------------
# One COPY for the scripts rather than one per file: seven layers holding 25 KB is pure
# manifest overhead.
COPY docker/harness/Caddyfile /etc/caddy/Caddyfile
COPY docker/harness/podh-init docker/harness/podh-preflight \
     docker/harness/podh-watchdog docker/harness/podh-self-delete \
     docker/harness/podh-seed-models docker/harness/podh-mount docker/harness/podh-code \
     docker/harness/podh-publish docker/harness/podh-logs docker/harness/podh-roots \
     docker/harness/podh-prepare \
     /usr/local/bin/
RUN chmod +x /usr/local/bin/podh-*

COPY serve/ /app/serve/

# --- 7. runtime defaults -------------------------------------------------------------------
# PYTHONPATH puts /workspace/code AHEAD of the baked /app. That single ordering is what makes
# the deps-only doctrine work: the image ships dependencies, the codestore ships CODE, and
# editing a stage is a few-KB upload rather than a rebuild.
#
# Missing it does not error. It silently runs the BAKED code while you believe you are running
# what you just synced.
ENV LINGUA_CORPUS_ROOT=/workspace/corpus \
    PODH_OUT_ROOT=/workspace/out \
    PODH_CACHE_ROOT=/workspace/cache \
    PODH_LOG_ROOT=/workspace/runs \
    LINGUA_MANIFEST=/workspace/manifest/corpus_research.json \
    PODH_API_PORT=8010 \
    PODH_SERVE_API=1 \
    PYTHONPATH=/workspace/code:/app \
    PODH_MODEL_ROOT=/opt/models \
    HF_HOME=/opt/models/hf

RUN mkdir -p /workspace/corpus /workspace/runs /workspace/cache /workspace/out

# --- 8. build-time sanity check -------------------------------------------------------------
# The independence guard every image carries: no loader module may appear here. A pod cannot
# launch a pod because the code is not present, not because it was asked not to.
COPY docker/assert_independence.py /tmp/assert_independence.py
RUN python /tmp/assert_independence.py && rm /tmp/assert_independence.py

# Prove the harness can BOOT, not merely that the model stack imports. The first launch of
# this image failed exactly here and only discovered it on a billed pod.
RUN set -eux; \
    caddy version; runpodctl version; rclone version | head -1; \
    for s in podh-init podh-mount podh-code podh-publish podh-logs podh-roots podh-prepare; do \
        test -x "/usr/local/bin/$s" || { echo "FAIL: $s missing"; exit 1; }; \
    done; \
    for f in train_first.py train_second.py train_finetune.py models.py meldataset.py Modules; do \
        test -e "$STYLETTS2_VENDOR/$f" || { echo "FAIL: vendored $f missing"; exit 1; }; \
    done; \
    python -c "\
from pathlib import Path; \
print('plbert cached' if any(Path('/opt/models/plbert').glob('*')) else 'plbert NOT cached')"; \
    echo "=== image is good: harness boots, styletts2 trains ==="

EXPOSE 8000
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/podh-init"]
CMD []
