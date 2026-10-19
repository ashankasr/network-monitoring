# One image for every Python service (runner, test endpoint, ingest worker, dashboard API);
# docker-compose.yml picks the command. The dashboard is built in a Node stage and served by the API.

FROM node:22.17.0-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12.11-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app/ app/
COPY configs/ configs/
COPY --from=web /web/dist web/dist

RUN useradd --create-home --uid 10001 qoe && mkdir -p /app/output && chown qoe /app/output
USER qoe

# Default: a scheduled runner. Per-instance details come from QOE_* environment variables.
CMD ["python", "-m", "app.traffic_generator.main", "--config", "configs/runner.yaml"]
