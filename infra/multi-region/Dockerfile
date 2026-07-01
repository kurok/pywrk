FROM python:3.15-rc-alpine AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.15-rc-alpine

COPY --from=builder /build/dist/*.whl /tmp/
# Install with [otel] extra for OpenTelemetry/Prometheus observability support.
# Excludes [tui], [dev], [lint], [security] extras not needed in production image.
# pip handles the glob internally; [otel] is a PEP 508 extra specifier.
# Python 3.15 is still an RC, so aiohttp and its C-extension deps have no
# prebuilt musllinux/cp315 wheels yet and must compile from source. Install a
# toolchain as a virtual package and drop it in the same layer to keep the
# runtime image slim.
RUN apk add --no-cache --virtual .build-deps build-base && \
    pip install --no-cache-dir "/tmp/*.whl[otel]" && \
    apk del .build-deps && \
    rm /tmp/*.whl

COPY infra/multi-region/scenarios/ /scenarios/

ENTRYPOINT ["pywrkr"]
