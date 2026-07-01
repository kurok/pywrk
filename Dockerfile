FROM python:3.15-rc-alpine AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN uv build --wheel --out-dir dist/

FROM python:3.15-rc-alpine

LABEL org.opencontainers.image.source="https://github.com/kurok/pywrkr"
LABEL org.opencontainers.image.description="pywrkr — Python HTTP benchmarking tool"
LABEL org.opencontainers.image.licenses="MIT"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=builder /build/dist/*.whl /tmp/
RUN uv pip install --system --no-cache /tmp/*.whl && rm /tmp/*.whl

ENTRYPOINT ["pywrkr"]
