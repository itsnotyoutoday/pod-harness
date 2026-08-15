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
    PYTHONPATH=/app/code \
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

COPY code/ /app/code/

RUN python - <<'PY'
import torch
from voice_fs2 import stages, speakers, infer
from TTS.tts.configs.fastspeech2_config import Fastspeech2Config
print("=== lingua-fastspeech2 ===")
print(f"  stages   {[s.name for s in stages.STAGES]}")
print(f"  voices   {speakers.all_keys()}")
print(f"  torch    {torch.__version__}  cuda_built={torch.version.cuda}")
a = Fastspeech2Config().model_args
print(f"  predictors  pitch={a.use_pitch} energy={a.use_energy} "
      f"duration=yes  speaker_emb={hasattr(a,'use_speaker_embedding')}")
print(f"  fps      {infer.frames_per_second()}")
PY

WORKDIR /app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "pod_harness.execute_job"]
