# -----------------------------------------------------------------------------
# Built on coding-runtime's thin variant: uid 1000 (`agent`) with a passwd entry,
# tini as PID 1, git/gh/glab/uv, and the in-image conformance suite. Pinned by
# digest — never :latest or a main build.
# -----------------------------------------------------------------------------
ARG BASE=ghcr.io/language-operator/coding-runtime:0.1.4-python@sha256:3e95d047ad47e9677d35432e6ac29619ec0f4aac72cad50e95b06ca7f57c2960

# -----------------------------------------------------------------------------
# Builder stage: resolve the runtime venv with uv (no dev deps). Built FROM the
# same base as the runtime so the venv's interpreter references stay valid when
# copied across.
# -----------------------------------------------------------------------------
FROM ${BASE} AS build
USER root
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# -----------------------------------------------------------------------------
# Runtime stage. The base already runs as uid 1000 and owns /workspace (the
# operator-provisioned PVC, where the SQLite checkpointer and FilesystemBackend
# write). Do not create a user — a second one at another uid is exactly the
# failure this base exists to prevent.
# -----------------------------------------------------------------------------
FROM ${BASE}
WORKDIR /app

# Runtime virtualenv (no dev deps) from the builder.
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8080

COPY agent_config.py server.py index.html ./
COPY --chmod=755 entrypoint.sh /entrypoint.sh

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/health" >/dev/null || exit 1

# Keep the base ENTRYPOINT (tini) so orphans are reaped and SIGTERM propagates;
# the server runs as its CMD.
CMD ["/entrypoint.sh"]
