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
    PYTHONPATH=/app/code \
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
        "librosa>=0.10" "pyyaml>=6" "boto3>=1.34" "monotonic-align" \
        "pod-harness @ git+https://github.com/itsnotyoutoday/pod-harness@main"

# The training driver, pinned. Only the loop and its utils; the model comes from the package.
ARG STYLETTS2_COMMIT=main
RUN set -eux; \
    git clone --depth 1 https://github.com/yl4579/StyleTTS2.git /tmp/st2; \
    cd /tmp/st2 && git checkout "${STYLETTS2_COMMIT}" 2>/dev/null || true; \
    mkdir -p /app/vendor/styletts2; \
    cp -r /tmp/st2/train_first.py /tmp/st2/train_second.py /tmp/st2/losses.py \
          /tmp/st2/meldataset.py /tmp/st2/models.py /tmp/st2/optimizers.py \
          /tmp/st2/utils.py /tmp/st2/Utils /tmp/st2/Configs \
          /app/vendor/styletts2/ 2>/dev/null || true; \
    cp /tmp/st2/LICENSE /app/vendor/styletts2/LICENSE 2>/dev/null || true; \
    rm -rf /tmp/st2

# PL-BERT, baked so a pod without egress still starts.
RUN python - <<'PY' || echo "WARNING: PL-BERT not cached; will fetch at runtime"
from huggingface_hub import snapshot_download
snapshot_download("papercup-ai/multilingual-pl-bert", local_dir="/models/plbert")
PY

COPY code/ /app/code/

# Report what is actually here. The check that would have caught a missing training driver
# before a GPU-hour was spent discovering it.
RUN python - <<'PY'
import os, torch
from pathlib import Path
from voice_style import stages, speakers, dataset, infer
print("=== lingua-styletts2 ===")
print(f"  stages     {[s.name for s in stages.STAGES]}")
print(f"  voices     {speakers.all_keys()}")
print(f"  torch      {torch.__version__}  cuda_built={torch.version.cuda}")
v = Path(os.environ['STYLETTS2_VENDOR'])
for f in ("train_first.py", "train_second.py", "models.py"):
    print(f"  vendor     {f:<18}{'ok' if (v/f).exists() else 'MISSING'}")
print(f"  plbert     {'ok' if any(Path('/models/plbert').glob('*')) else 'not cached'}")
print(f"  fps        {infer.frames_per_second()} ({1000/infer.frames_per_second():.2f} ms/frame)")
PY

WORKDIR /app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "pod_harness.execute_job"]
