# Base: python:3.12.16-slim-trixie from the public ECR mirror of Docker Official
# Images. The tag is informational (Dependabot reads it); the digest is what pulls.
FROM public.ecr.aws/docker/library/python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS builder
WORKDIR /src
# Hash-locked build backend; --no-build-isolation stops pip fetching an unverified one.
COPY requirements-build.txt /tmp/requirements-build.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-build.txt
COPY packages/client/ /src/client/
COPY packages/server/ /src/server/
RUN python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /wheels /src/client /src/server

FROM public.ecr.aws/docker/library/python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07
COPY --from=builder /wheels /wheels
COPY requirements-runtime.txt /requirements-runtime.txt
# Third-party packages come only from the hash-locked file. The project wheels are
# then installed offline, so any extra that the lock does not satisfy fails the build.
RUN python -m pip install --no-cache-dir --require-hashes -r /requirements-runtime.txt \
    && python -m pip install --no-cache-dir --no-index --find-links /wheels 'vaelius-server[cloud,semantic]' \
    && python -m pip check && rm -rf /wheels
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AWS_EC2_METADATA_DISABLED=true OMP_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
WORKDIR /tmp
USER 10001:10001
EXPOSE 8080
# Plain HTTP for a TLS-terminating proxy or loopback only. A production profile must
# declare deployment.tls (proxy or direct) or the API refuses to start; see
# docs/SELF_HOSTING.md "HTTPS and production transport".
# Liveness only: GET /health needs no database or credentials. Uses the image's
# Python (no shell or curl) and http.client, which ignores proxy variables. Assumes
# the default --port 8080, plain HTTP and 127.0.0.1 in allowed_hosts; override or
# disable it if you change the port, use deployment.tls "direct", or omit that host.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import http.client,sys;c=http.client.HTTPConnection('127.0.0.1',8080,timeout=4);c.request('GET','/health');sys.exit(c.getresponse().status!=200)"]
CMD ["python", "-m", "agenthub.cloud_runtime", "--profile", "/profile", "--bind", "0.0.0.0", "--port", "8080"]
