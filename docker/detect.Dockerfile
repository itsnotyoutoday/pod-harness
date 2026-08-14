# The ANALYSIS image — statistics without an audio stack.
#
# ## Why a third image exists
#
# Two images was a false choice between "orchestration only" and "everything MFA needs":
#
#   pod-harness      237 MB   python + boto3 + fastapi + caddy + the engine
#   lingua-pipeline  6.07 GB  the above plus MFA, torch, librosa, speechbrain, ffmpeg
#
# lingua-detect sits between them, and running it on either is wrong in a different way.
# The slim image cannot answer the questions the detector needs to answer; the pipeline
# image answers them while carrying six gigabytes of audio machinery to read JSON. Detection
# never opens an audio file — the trainer already turned sound into numbers — so every
# dependency here is numerical and none is acoustic.
#
# Target ~450 MB: thirteen times smaller than the pipeline image, and the difference is
# entirely torch, MFA, librosa and the Japanese and Thai dictionaries the MFA base drags in.
#
# ## What the extra packages actually buy
#
# numpy       vectorised scoring, and a covariance matrix that pure Python would make
#             tedious. Five of our f0 features correlate up to r=0.974 and are currently
#             summed as if independent, which double-counts the same evidence five times
#             and inflates every margin. Fixing that is Mahalanobis distance, which needs
#             a matrix inverse.
#
# scipy       proper statistical tests, and bootstrap confidence intervals on accuracy.
#             We report 0.911 balanced with no interval; over 1,240 Bogota samples that is
#             roughly +/-2.5 points, and saying so is the difference between a number and
#             a claim.
#
# scikit-learn RandomForest importance and permutation importance — the discovery method
#             for "which features distinguish these two populations", complementing the
#             variance attribution the trainer already does. Also LDA and logistic
#             regression as DISCRIMINATIVE baselines: without one we cannot say whether
#             0.911 from a generative one-class scorer is good or merely adequate. And
#             calibration, so `confident` means a probability rather than a margin
#             clearing a hardcoded threshold.
#
# ## What is deliberately absent
#
# No librosa, no torch, no MFA, no ffmpeg. If a stage here ever needs to open a wav, it
# belongs in the trainer instead: measurement is the trainer's job and judgement is this
# one's, and the whole reason lingua-detect is a separate repo is that a detector able to
# re-measure is a detector able to grade its own homework.

FROM --platform=linux/amd64 python:3.12-slim-bookworm

ARG CADDY_VERSION=2.8.4

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PODH_WORKSPACE=/workspace \
    PODH_MODEL_ROOT=/opt/models \
    PODH_IMAGE_KIND=analysis \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    PYTHONPATH=/app

# OPENBLAS/OMP pinned to 1 on purpose. A pod reports the HOST's core count, not the
# container's, so BLAS spawns a thread per host core inside a 2-vCPU container and spends
# its time context-switching. resources.py learned this the expensive way for the worker
# pool; the same trap applies to every numpy operation.

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl tar tini; \
    curl -sL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin caddy; \
    chmod +x /usr/local/bin/caddy; \
    pip install --no-cache-dir \
        "boto3>=1.34" "botocore>=1.34" \
        "fastapi>=0.110" "uvicorn[standard]>=0.29" \
        "numpy>=2.0" "scipy>=1.13" "scikit-learn>=1.5"; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /root/.cache; \
    mkdir -p /workspace /opt/models /app

COPY --chmod=0755 docker/harness/podh-init docker/harness/podh-mount docker/harness/podh-code docker/harness/podh-publish docker/harness/podh-logs docker/harness/podh-roots docker/harness/podh-prepare \
     docker/harness/podh-preflight docker/harness/podh-watchdog \
     /usr/local/bin/
COPY docker/harness/Caddyfile /etc/caddy/Caddyfile
COPY serve/ /app/serve/
COPY src/pod_harness/ /app/pod_harness/
COPY contract.json /app/contract.json

# ABI check, immediately after the install and for the same reason pipeline.Dockerfile has
# one: without it, the first symptom of a broken wheel is an ImportError raised from inside
# a job on a billed pod, which reads like a code bug and sends you debugging the wrong file.
RUN echo "=== ABI check ===" \
 && python -c "\
import numpy, scipy, scipy.stats, scipy.linalg, sklearn, sklearn.ensemble, sys; \
import pod_harness, pod_harness.framework, pod_harness.execute_job, pod_harness.stage_manifest; \
print('numerical stack imports OK on', sys.version.split()[0]); \
print('numpy', numpy.__version__, '| scipy', scipy.__version__, \
      '| sklearn', sklearn.__version__)"

# The same independence guard the other images carry: no loader module may appear here. A
# pod cannot launch a pod because the code is not present, not because it was asked not to.
COPY docker/assert_independence.py /tmp/assert_independence.py
RUN python /tmp/assert_independence.py && rm /tmp/assert_independence.py

# Prove the absence, do not merely intend it. This image is defined as much by what it does
# NOT carry as by what it does, and an audio dependency arriving as a transitive of some
# future package would silently turn a 450 MB image into a large one.
RUN set -eux; \
    for m in torch librosa soundfile speechbrain montreal_forced_aligner; do \
        if python -c "import $m" 2>/dev/null; then \
            echo "FAIL: $m is present — this is the analysis image; measurement belongs in the trainer"; \
            exit 1; \
        fi; \
    done; \
    echo "confirmed: no audio stack"

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["podh-init"]
