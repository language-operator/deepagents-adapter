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
After the run it idles in service mode and exits in task mode
(`AGENT_EXECUTION_MODE=task`: `0` if the run completed, `1` otherwise).

It ships as a **single combined image** plus a **Helm chart** that registers a
cluster-scoped `LanguageAgentRuntime` named `deepagents`. There is **no init
container** — the server is our own code and reads the config directly.

## Key files

- `agent_config.py` — the pure config-translation core (model selection, the gateway
  credential from `MODEL_API_KEY`, persona system prompt, task/instructions, MCP
  server map, A2A card/peers, env-var fallbacks). **This is what the tests target** — keep it pure and side-effect free.
- `server.py` — thin FastAPI server: `GET /health` (probe), `GET /` (live UI),
  `GET /events` (SSE replay + live), `GET /state`, `POST /restart`.
  No human-in-the-loop: the agent runs every tool without approval (deployers opt
  into an autonomous agent). Its `main()` runs uvicorn in-process so a task-mode
  run can stop the server and set the exit code; the server stays up during the run
  because the pod's probes hit `/health` in both modes. `WorkspaceBackend` confines
  the agent's files to the workspace while keeping the pod's real paths
  (`/workspace/x` is `/workspace/x`); it overrides two private deepagents methods,
  so re-run its tests on a deepagents bump.
- `entrypoint.sh` / `Dockerfile` — container build on `coding-runtime`'s **thin** base
  (`ARG BASE`, pinned by digest): uid 1000 `agent`, `tini` ENTRYPOINT, git/gh/glab/uv.
  **Never create a user or override ENTRYPOINT** (the server runs as `CMD`). Runtime
  venv is built `--no-dev`.
- `tests/` — pytest suite over `agent_config.py`, including the vendored coding-runtime
  operator fixture corpus (`tests/fixtures/`, re-sync on base bumps), plus
  `test_server.py` for the `Runner`'s finish signal (what task mode exits on) and
  the `WorkspaceBackend` path mapping.
- `chart/` — the `LanguageAgentRuntime` Helm chart (`Chart.yaml`, `values.yaml`).
- `.github/workflows/` — `test.yaml`, `build-image.yaml`, `release-chart.yaml`.

## Testing

- `make test` / `uv run pytest -q` — run the pytest suite against the local venv.
- `make conformance` — build the image and run coding-runtime's conformance suite
  (extracted from the image) in `adapter` mode, under the real pod posture.
- Add/extend tests under `tests/` whenever `agent_config.py` behavior changes.
- **No Python linter** is configured. CI correctness == the three `test.yaml` jobs:
  `pytest`, `image-test` (conformance suite against the built image) and `chart-lint` (`helm lint chart` +
  `helm template deepagents chart`).
- What to run for a change:
  - always `make test` (`pytest`);
  - `Dockerfile` touched → `make conformance` (`image-test`);
  - chart touched → `helm lint chart && helm template deepagents chart >/dev/null` (`chart-lint`).
- The PR title must be a conventional commit (`feat:`, `fix:`, `chore:`, `docs:`, `test:`).

## Build & dev deploy

- `make build` — build `ghcr.io/language-operator/deepagents-adapter:<git-sha>` + `:latest`.
- `make dev` — build, import into local k3s, and `helm upgrade` the runtime
  (requires the `language-operator` chart / `LanguageAgentRuntime` CRD installed first).
  Then: `kubectl get languageagentruntime deepagents`.
- `make publish` — push image tags to ghcr.io. `make uninstall` — remove the release.

## Releases

Cut a release with `/release major|minor|patch` (`.claude/commands/release.md`).
Version is kept in **lockstep**: `chart/Chart.yaml` `version` + `appVersion`,
`chart/values.yaml` `image.tag`, `pyproject.toml` `version` (+ `uv.lock`), and the
git tag `vX.Y.Z` all become the same `X.Y.Z`. `/release` also publishes GitHub
release notes, breaking changes first. Pushing a `v*` tag triggers `build-image.yaml` and `release-chart.yaml`.

## Issue-driven workflow

`/iterate [#issue] [--auto]` handles **one issue** per run: pick the next issue (or
`#issue`) → worktree → plan → implement → test → PR → poll CI → squash-merge → close,
then stop. The plan pauses for approval, or is posted as an issue comment when `--auto`
is passed. Work happens inside a git worktree under `.claude/worktrees/`.

It comes from the shared `langop` plugin in
[`language-operator/skills`](https://github.com/language-operator/skills), pinned to a tag
in `.claude/settings.json` — not from a copy in this repo, which is what it replaced.
`/iterate` and `/langop:iterate` both invoke it. There is nothing per-repo in the skill
itself: it reads `## Testing` above to learn how to test a change here, so keep that section
accurate.

Interactive sessions need no install step — the plugin loads at the pinned tag once the folder
is trusted. Non-interactive ones (`claude -p`, scheduled or in-cluster agents) have no trust
dialog, so they need this once, with the tag the repo pins:

```bash
claude plugin marketplace add 'language-operator/skills#v0.1.0'
claude plugin install langop@language-operator --scope project
```

Two things to avoid: a marketplace add without `#<tag>` follows `main` rather than the pin,
and `--scope project` on the *marketplace* add rewrites `.claude/settings.json` and drops
its `ref`. To take a newer release, change `ref` there. Machine-specific permissions belong
in the untracked `.claude/settings.local.json`.
