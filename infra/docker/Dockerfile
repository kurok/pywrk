# Production Dockerfile for pywrkr load testing on ECS Fargate.
# Includes scenario files baked into the image.

FROM python:3.13-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/kurok/pywrkr"
LABEL org.opencontainers.image.description="pywrkr — distributed HTTP benchmarking"
LABEL org.opencontainers.image.licenses="MIT"

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir "/tmp/*.whl[all]" && rm /tmp/*.whl

# Bake scenario files into the image
COPY infra/scenarios/ /scenarios/

# Health check: pywrkr --help exits 0
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD pywrkr --help > /dev/null 2>&1 || exit 1

ENTRYPOINT ["pywrkr"]
