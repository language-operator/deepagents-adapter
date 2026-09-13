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
# tooling stays minimal: ca-certificates, curl (HEALTHCHECK), git, and the
# vendor CLIs (gh, glab) the operator authenticates for spec.repository. uv is
# kept so test.sh can sync the dev group and run pytest inside the image.
# -----------------------------------------------------------------------------
FROM python:3.13-slim
ARG GH_VERSION=2.65.0
ARG GLAB_VERSION=1.117.0
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# gh (GitHub CLI) and glab (GitLab CLI) from their official release tarballs;
# Debian does not package gh, and the release names use the dpkg arch. The
# operator exports GH_TOKEN or GITLAB_TOKEN (by spec.repository.vendor) into the
# agent container, so both are authenticated without any adapter code.
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL -o /tmp/gh.tar.gz \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${ARCH}.tar.gz" && \
    tar -xzf /tmp/gh.tar.gz -C /tmp && \
    mv "/tmp/gh_${GH_VERSION}_linux_${ARCH}/bin/gh" /usr/local/bin/gh && \
    rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_${ARCH}" && \
    curl -fsSL -o /tmp/glab.tar.gz \
        "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${ARCH}.tar.gz" && \
    tar -xzf /tmp/glab.tar.gz -C /tmp bin/glab && \
    mv /tmp/bin/glab /usr/local/bin/glab && \
    rm -rf /tmp/glab.tar.gz /tmp/bin

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

# Non-root runtime user. The operator runs every agent container as uid 1000
# (fsGroup 101), so the image user carries that uid: it gives the running
# process a passwd entry and a home directory (ssh, gh and glab look them up)
# and keeps /workspace — the operator-provisioned PVC mount the SQLite
# checkpointer and FilesystemBackend write to — owned by the same uid.
RUN groupadd --gid 1000 app \
    && useradd --create-home --uid 1000 --gid 1000 app \
    && mkdir -p /workspace \
    && chown -R app:app /workspace /app
USER app

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/health" >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
