# lingua-runner — THE framework image. Every other image is built on top of this one.
#
# ## Why this exists
#
# A storage benchmark whose only import is boto3 was pulling `lingua-pipeline`: MFA,
# torch, torchaudio, speechbrain, librosa, scipy, scikit-learn. Multiple gigabytes and
# 2-4 minutes of pull, on every launch and every retry, to run four stages that touch
# nothing but the filesystem and an S3 client.
#
# Most of what the framework is FOR is like that. Maintenance, mirroring, surveys,
# pruning, benchmarks, dispatch probes — none of them are machine learning. They are
# ordinary jobs that want the stage engine, the control API and an object store. That is
# this image.
#
# ## Why this is a base image and not a second copy of the harness
#
# The framework — harness scripts, the /v1 control surface, the stage engine — is the same
# in every image. If each Dockerfile COPYs it independently, they drift, and drift here is
# not theoretical: three times in one day a Python module moved and a shell script kept
# pointing at the old path. The last one billed 13 minutes and served 404.
#
# So the framework is built ONCE, here, and every other image takes it from this image:
#
#     FROM mmcauliffe/montreal-forced-aligner@sha256:...        # or any base a workload needs
#     COPY --from=ghcr.io/itsnotyoutoday/lingua-harness:latest /usr/local/bin/lingua-* /usr/local/bin/
#     COPY --from=ghcr.io/itsnotyoutoday/lingua-harness:latest /app/serve /app/serve
#
# One framework, many bases. A workload picks whatever base its dependencies demand — MFA,
# a CUDA image, a TTS stack — and inherits an identical control surface. Two images cannot
# disagree about the harness when only one of them builds it.
#
# Standalone, this image also runs any job that is not machine learning: maintenance,
# mirroring, surveys, pruning, benchmarks, dispatch probes. That is most of what a
# framework is for.
#
# ## Why so few layers
#
# Every RUN and COPY is a layer, and RunPod pulls them serially onto a cold machine before
# a single second of work happens. A previous project's image took forever to pull for
# exactly this reason. So: one base, one RUN that installs and cleans up in the same
# layer, one COPY for all our source. Four layers total on top of the base.
#
# Cleaning up in a SEPARATE RUN would save nothing at all — the deleted files still exist
# in the earlier layer and still ship. That mistake is the single most common reason
# "slim" images are not slim.
#
# Target: ~250 MB against several GB, and a pull measured in seconds.

FROM --platform=linux/amd64 python:3.12-slim-bookworm

ARG CADDY_VERSION=2.8.4

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LINGUA_WORKSPACE=/workspace \
    LINGUA_MODEL_ROOT=/opt/models \
    LINGUA_IMAGE_KIND=runner \
    PYTHONPATH=/app

# One layer: system packages, Caddy, python deps, and the cleanup that only helps if it
# happens HERE. No git: the engine is COPYd from this repo rather than pulled from another
# one, so the image builds from its own source and cannot ship an engine that disagrees
# with the harness scripts beside it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl tar tini; \
    curl -sL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin caddy; \
    chmod +x /usr/local/bin/caddy; \
    pip install --no-cache-dir \
        "boto3>=1.34" "botocore>=1.34" \
        "fastapi>=0.110" "uvicorn[standard]>=0.29"; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /root/.cache; \
    mkdir -p /workspace /opt/models /app

# One COPY for every script and the API. Seven COPYs holding 25 KB would be six wasted
# layers. `chmod` rides along rather than costing its own RUN.
COPY --chmod=0755 docker/harness/lingua-init docker/harness/lingua-mount \
     docker/harness/lingua-preflight docker/harness/lingua-watchdog \
     /usr/local/bin/
COPY docker/harness/Caddyfile /etc/caddy/Caddyfile
COPY serve/ /app/serve/
COPY lingua_harness/ /app/lingua_harness/

# Assertions, not documentation. The same guard `pipeline.Dockerfile` carries: every
# lingua_harness module the shell harness names must actually import. Three separate times a
# Python move left the harness pointing at a module that no longer existed, and the last
# one was only visible as a pod that billed for 13 minutes and served 404. Failing the
# build is the cheapest place to find it.
COPY docker/assert_independence.py /tmp/assert_independence.py
RUN python /tmp/assert_independence.py && rm /tmp/assert_independence.py

WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/lingua-init"]
CMD []
