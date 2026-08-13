# lingua-harness-test — the SAME harness, without the ten-minute conda layer.
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
#   docker build -f docker/test.Dockerfile -t lingua-harness-test .
#   docker run --rm -p 8000:8000 -e LINGUA_API_TOKEN=dev lingua-harness-test

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
# (serve/jobs.py shelling lingua_core.execute_job) rather than a stand-in for it.
ARG LINGUA_CORE_REF=main
RUN pip install --no-cache-dir \
        "git+https://github.com/itsnotyoutoday/lingua-core.git@${LINGUA_CORE_REF}"

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
# A fake WORKLOAD: stage classes plus a registry and a capabilities.json, exactly what a
# real workload publishes. The engine loads it through pipeline.stages_from, so the test
# exercises the real resolution path instead of a special case.
COPY tests/fixture_workload/ /workspace/code/fixture/

ENV LINGUA_CACHE_ROOT=/workspace/.cache \
    LINGUA_LOG_ROOT=/workspace/logs \
    LINGUA_MODEL_ROOT=/opt/models \
    MFA_ROOT_DIR=/workspace/.cache/mfa \
    LINGUA_API_PORT=8010 \
    LINGUA_SERVE_API=1 \
    LINGUA_STATUS_S3=0 \
    PYTHONPATH=/app

RUN mkdir -p /workspace/logs /workspace/.cache

ENV LINGUA_CODE_ROOT=/workspace/code/fixture \
    LINGUA_DEFAULT_STAGES_FROM=fixture.stages:STAGES
RUN python -c "import serve.jobs, serve.code, serve.api, lingua_core.execute_job; \
print('harness + engine imports OK')"

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/lingua-init"]
CMD []
