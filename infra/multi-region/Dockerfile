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
RUN pip install --no-cache-dir "/tmp/*.whl[otel]" && rm /tmp/*.whl

COPY infra/multi-region/scenarios/ /scenarios/

ENTRYPOINT ["pywrkr"]
