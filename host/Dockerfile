FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /opt/ses
COPY pyproject.toml uv.lock ./
COPY host/pyproject.toml host/pyproject.toml
COPY clients/frigate_monitor/pyproject.toml clients/frigate_monitor/pyproject.toml
RUN uv sync --frozen --no-dev --package ses-host --no-install-workspace
COPY host/app host/app
RUN uv sync --frozen --no-dev --package ses-host --no-editable

FROM python:3.12-slim AS runtime
ENV PATH="/opt/ses/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SES_DATA_DIR=/data
RUN groupadd --gid 10001 ses \
    && useradd --uid 10001 --gid ses --no-create-home ses \
    && mkdir -p /data/images /data/thumbnails \
    && chown -R ses:ses /data \
    && chmod 700 /data /data/images /data/thumbnails
WORKDIR /opt/ses
COPY --from=builder /opt/ses/.venv /opt/ses/.venv
USER ses
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=7s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=6).close()"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
