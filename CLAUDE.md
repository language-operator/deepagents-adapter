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
  system prompt, task/instructions, MCP server map, interrupt/HITL policy, env-var
  fallbacks). **This is what the tests target** — keep it pure and side-effect free.
- `server.py` — thin FastAPI server: `GET /health` (probe), `GET /` (live UI),
  `GET /events` (SSE replay + live), `GET /state`, `POST /resume`, `POST /restart`.
  Human-in-the-loop pauses before side-effecting tools (`write_file`/`edit_file` and
  MCP tools by default; override with `HITL_TOOLS`).
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

`/iterate [queue-number | #issue]` (`.claude/commands/iterate.md`) runs the full
loop: pick an issue → worktree → plan → implement → test → PR → poll CI → squash-
merge → close. Work happens inside a git worktree under `.claude/worktrees/`.
