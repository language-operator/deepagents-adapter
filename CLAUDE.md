# CLAUDE.md

Guidance for working in the `deepagents-adapter` repository.

## What this is

A [Language Operator](https://github.com/language-operator) **runtime** that runs a
[langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) agent as a
Kubernetes workload. Unlike the CLI-wrapping runtimes (opencode, claude-code,
openclaw), this is an **autonomous executor**: at startup it reads the
operator-injected `/etc/agent/config.yaml`, builds a deepagents agent pointed at
the cluster LiteLLM gateway, and runs the agent's `instructions` once — streaming
every event to STDOUT (`kubectl logs` is the primary UI) and a live browser view.

It ships as a **single combined image** plus a **Helm chart** that registers a
cluster-scoped `LanguageAgentRuntime` named `deepagents`. There is **no init
container** — the server is our own code and reads the config directly.

## Key files

- `agent_config.py` — the pure config-translation core (model selection, persona
  system prompt, task/instructions, MCP server map, A2A card/peers, env-var
  fallbacks). **This is what the tests target** — keep it pure and side-effect free.
- `server.py` — thin FastAPI server: `GET /health` (probe), `GET /` (live UI),
  `GET /events` (SSE replay + live), `GET /state`, `POST /restart`.
  No human-in-the-loop: the agent runs every tool without approval (deployers opt
  into an autonomous agent).
- `entrypoint.sh` / `Dockerfile` — container build; runtime venv is built `--no-dev`.
- `tests/` — pytest suite over `agent_config.py`.
- `chart/` — the `LanguageAgentRuntime` Helm chart (`Chart.yaml`, `values.yaml`).
- `.github/workflows/` — `test.yaml`, `build-image.yaml`, `release-chart.yaml`.

## Testing

- `make test` — builds the image and runs `test.sh` (pytest) inside it with
  `--user root` (the runtime venv is `--no-dev`, so `test.sh` runs
  `uv sync --frozen --group dev` first).
- `uv run pytest -q` — run the suite directly against the local venv.
- Add/extend tests under `tests/` whenever `agent_config.py` behavior changes.
- **No Python linter** is configured. CI correctness == the two `test.yaml` jobs:
  `image-test` (pytest in Docker) and `chart-lint` (`helm lint chart` +
  `helm template deepagents chart`).

## Build & dev deploy

- `make build` — build `ghcr.io/language-operator/deepagents-adapter:<git-sha>` + `:latest`.
- `make dev` — build, import into local k3s, and `helm upgrade` the runtime
  (requires the `language-operator` chart / `LanguageAgentRuntime` CRD installed first).
  Then: `kubectl get languageagentruntime deepagents`.
- `make publish` — push image tags to ghcr.io. `make uninstall` — remove the release.

## Releases

Cut a release with `/release major|minor|patch` (`.claude/commands/release.md`).
Version is kept in **lockstep**: `chart/Chart.yaml` `version` + `appVersion`,
`chart/values.yaml` `image.tag`, and the git tag `vX.Y.Z` all become the same
`X.Y.Z`. Pushing a `v*` tag triggers `build-image.yaml` and `release-chart.yaml`.

## Issue-driven workflow

`/iterate [#issue] [--auto]` (`.claude/commands/iterate.md`) handles **one issue**
per run: pick the next issue (or `#issue`) → worktree → plan → implement → test →
PR → poll CI → squash-merge → close, then stop. The plan pauses for approval, or is
posted as an issue comment when `--auto` is passed or `AGENT_NAME` is set. Work
happens inside a git worktree under `.claude/worktrees/`. The command body and the
`iterate/*.sh` scripts are the org's canonical copy (language-operator#932) — only
the frontmatter `allowed-tools` and the `## Testing` section are repo-specific.
