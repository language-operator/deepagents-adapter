# -----------------------------------------------------------------------------
# Builder stage: resolve the runtime venv with uv (no dev deps), then discard the
# build context. Builder and runtime share the same python:3.13-slim base so the
# venv's interpreter references stay valid when copied across.
# -----------------------------------------------------------------------------
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# -----------------------------------------------------------------------------
# Runtime stage. This is a service (FastAPI + deepagents), not a TUI image — OS
# tooling stays minimal: ca-certificates, curl (HEALTHCHECK), git. uv is kept so
# test.sh can sync the dev group and run pytest inside the image.
# -----------------------------------------------------------------------------
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Runtime virtualenv (no dev deps) from the builder.
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Application code + project metadata. pyproject.toml/uv.lock and tests/ power the
# in-image pytest run (test.sh); they are inert at runtime.
COPY agent_config.py server.py index.html pyproject.toml uv.lock ./
COPY tests ./tests
COPY --chmod=755 entrypoint.sh /entrypoint.sh
COPY --chmod=755 test.sh /app/test.sh

# Non-root runtime user. /workspace is the operator-provisioned PVC mount — the
# SQLite checkpointer and FilesystemBackend write there.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /workspace \
    && chown -R app:app /workspace /app
USER app

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/health" >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
