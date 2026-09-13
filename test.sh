#!/bin/sh
# Runs the pytest suite inside the container image to verify agent_config.py
# (the config-translation core). The runtime venv is built --no-dev, so sync the
# dev group first (uv + pyproject.toml + uv.lock ship in the image). Run as root
# so uv can write to /app/.venv (the Makefile/CI test target passes --user root).
# Exit 0 = all pass.
set -e
cd /app

# The operator authenticates these for spec.repository (GH_TOKEN / GITLAB_TOKEN);
# the image must ship them, and the runtime user must be the operator's uid 1000.
git --version >/dev/null
gh --version >/dev/null
glab --version >/dev/null
[ "$(id -u app)" = "1000" ]

uv sync --frozen --group dev
exec uv run --no-sync pytest -q
