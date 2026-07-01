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
# Python 3.15 is still an RC, so aiohttp and its C-extension deps have no
# prebuilt musllinux/cp315 wheels yet and must compile from source. Install a
# toolchain as a virtual package and drop it in the same layer to keep the
# runtime image slim.
RUN apk add --no-cache --virtual .build-deps build-base && \
    uv pip install --system --no-cache /tmp/*.whl && \
    apk del .build-deps && \
    rm /tmp/*.whl

ENTRYPOINT ["pywrkr"]
