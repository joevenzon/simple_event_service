FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /opt/ses
COPY pyproject.toml uv.lock ./
COPY host/pyproject.toml host/pyproject.toml
COPY clients/frigate_monitor/pyproject.toml clients/frigate_monitor/pyproject.toml
RUN uv sync --frozen --no-dev --package ses-frigate-monitor --no-install-workspace
COPY clients/frigate_monitor/frigate_monitor clients/frigate_monitor/frigate_monitor
RUN uv sync --frozen --no-dev --package ses-frigate-monitor --no-editable

FROM python:3.12-slim AS runtime
ENV PATH="/opt/ses/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 ses \
    && useradd --uid 10001 --gid ses --no-create-home ses \
    && mkdir /data \
    && chown ses:ses /data \
    && chmod 700 /data
WORKDIR /opt/ses
COPY --from=builder /opt/ses/.venv /opt/ses/.venv
USER ses
CMD ["ses-frigate-monitor", "--config", "/config/config.yaml", "run"]
