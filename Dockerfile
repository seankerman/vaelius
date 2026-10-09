FROM public.ecr.aws/docker/library/python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS builder
WORKDIR /src
COPY packages/client/ /src/client/
COPY packages/server/ /src/server/
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels /src/client /src/server

FROM public.ecr.aws/docker/library/python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
COPY --from=builder /wheels /wheels
COPY requirements-runtime.lock /requirements-runtime.lock
RUN python -m pip install --no-cache-dir -r /requirements-runtime.lock /wheels/*.whl \
    && python -m pip check && rm -rf /wheels
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AWS_EC2_METADATA_DISABLED=true OMP_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
WORKDIR /tmp
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "agenthub.cloud_runtime", "--profile", "/profile", "--bind", "0.0.0.0", "--port", "8080"]
