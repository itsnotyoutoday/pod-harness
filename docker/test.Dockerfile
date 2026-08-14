# pod-harness-test — the SAME harness, without the ten-minute conda layer.
#
# ## Why this image exists
#
# pipeline.Dockerfile takes ~35 minutes to build because MFA comes from conda and the
# baked weights are a few hundred megabytes. That is far too slow a loop for debugging a
# bash entrypoint or an HTTP route, and "push to CI and wait" is a terrible way to find
# out you typo'd a Caddyfile directive.
#
# So this image copies the harness and the API verbatim — same scripts, same Caddyfile,
# same serve/ package — onto python:3.11-slim, and stands in a fixture for the pipeline.
# It builds in about a minute and exercises everything the real image does EXCEPT the
# pipeline itself: init ordering, log mirroring, model seeding, Caddy auth and routing,
# the /v1 surface, event sequencing, long-poll, and job lifecycle.
#
# What it deliberately does NOT prove: that MFA installs, that the weights bake, that the
# real stages run. Those need the real image, and the build-time sanity check in
# pipeline.Dockerfile is what guards them.
#
# Build and run:
#   docker build -f docker/test.Dockerfile -t pod-harness-test .
#   docker run --rm -p 8000:8000 -e PODH_API_TOKEN=dev pod-harness-test

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        curl tar unzip ca-certificates procps tini fuse3 git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ARG CADDY_VERSION=2.8.4
RUN curl -sL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin caddy \
 && chmod +x /usr/local/bin/caddy && curl -sL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o /tmp/rc.zip \
 && cd /tmp && unzip -q rc.zip && mv rclone-*/rclone /usr/local/bin/rclone \
 && chmod +x /usr/local/bin/rclone && rm -rf /tmp/rc.zip /tmp/rclone-*

WORKDIR /app
COPY requirements-serve.txt .
RUN pip install --no-cache-dir -r requirements-serve.txt

# Same engine the real image carries — so the harness test exercises the real code path
# (serve/jobs.py shelling pod_harness.execute_job) rather than a stand-in for it.
# Pinned by SHA, not a branch. A mutable ref means Docker's layer cache can serve a
# STALE engine: the RUN line is unchanged, so the cache hits even though the branch has
# moved. That is the same immutability argument as code/<repo>/<sha>/ for workloads —
# bump this deliberately.
# --- the engine ---------------------------------------------------------------------------
# COPIED from this repo, not pip-installed from git. It used to be a git install of
# lingua-core, which had two faults: the engine lived in another repo, so this image could
# not be built from its own source and the harness scripts could disagree with the engine
# they shipped beside; and a floating ref served a STALE build out of the layer cache,
# which is why a SHA was pinned here and had to be bumped by hand on every engine change.
# The engine is part of this repo now, so the image is self-contained and always coherent.
COPY src/pod_harness/ /app/pod_harness/
# The interface this image implements, served at /v1/contract so a loader can
# validate against the EXACT image it is about to launch.
COPY contract.json /app/contract.json

COPY docker/harness/Caddyfile /etc/caddy/Caddyfile
COPY docker/harness/podh-roots docker/harness/podh-prepare docker/harness/podh-init         /usr/local/bin/podh-init
COPY docker/harness/podh-preflight    /usr/local/bin/podh-preflight
COPY docker/harness/podh-watchdog     /usr/local/bin/podh-watchdog
COPY docker/harness/podh-self-delete  /usr/local/bin/podh-self-delete
COPY docker/harness/podh-seed-models  /usr/local/bin/podh-seed-models
COPY docker/harness/podh-mount         /usr/local/bin/podh-mount
RUN chmod +x /usr/local/bin/podh-*

COPY serve/ /app/serve/
# A fake WORKLOAD: stage classes plus a registry and a capabilities.json, exactly what a
# real workload publishes. The engine loads it through pipeline.stages_from, so the test
# exercises the real resolution path instead of a special case.
COPY tests/fixture_workload/ /workspace/code/fixture/

ENV PODH_CACHE_ROOT=/workspace/.cache \
    PODH_LOG_ROOT=/workspace/logs \
    PODH_MODEL_ROOT=/opt/models \
    MFA_ROOT_DIR=/workspace/.cache/mfa \
    PODH_API_PORT=8010 \
    PODH_SERVE_API=1 \
    PODH_STATUS_S3=0 \
    PYTHONPATH=/app

RUN mkdir -p /workspace/logs /workspace/.cache

ENV PODH_CODE_ROOT=/workspace/code/fixture \
    PODH_DEFAULT_STAGES_FROM=fixture.stages:STAGES
# Every module podh-init names must actually import. Added after a pod spent 13 minutes
# failing because the shell script still referenced runners.execute_job, three Python moves
# later. Catching it here costs 2 minutes; catching it on a pod cost real money.
COPY docker/assert_independence.py /tmp/assert_independence.py
RUN python /tmp/assert_independence.py \
 && python -c "import serve.jobs, serve.code, serve.api; print('serve imports OK')"

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/podh-init"]
CMD []
