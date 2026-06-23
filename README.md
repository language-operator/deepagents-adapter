# deepagents-adapter

A [Language Operator](https://github.com/language-operator) **runtime** that runs a
[langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) agent as a
Kubernetes workload. It is the project's first *framework* runtime — the existing
runtimes (opencode, claude-code, openclaw) wrap interactive coding CLIs; this one is
an **autonomous executor**: it runs the agent's `instructions` itself.

## What's here

A **single combined image** plus a **Helm chart** that registers a
`LanguageAgentRuntime`.

- **Image** (`ghcr.io/language-operator/deepagents-adapter`) — at startup it reads
  the operator-injected `/etc/agent/config.yaml`, builds a deepagents agent
  (planning, sub-agents, virtual filesystem, MCP tools) pointed at the cluster
  LiteLLM gateway, and **autonomously runs the agent's task** (its `instructions`)
  once, streaming every event to **STDOUT** (so `kubectl logs` is the primary UI)
  and to a live browser view. Then it idles. **No init container** — unlike the
  CLI-wrapping runtimes, the server is our own code and reads the config directly.
  - **Human-in-the-loop** is wired: `create_deep_agent(interrupt_on=…)` pauses
    before side-effecting tools (`write_file`/`edit_file` and MCP tools by
    default; override with `HITL_TOOLS`). The run resumes when approved.
  - Endpoints (thin server): `GET /health` (probe), `GET /` (live UI),
    `GET /events` (SSE: replay + live), `GET /state` (status + pending interrupt),
    `POST /resume` (`{decisions:[…]}` — approve/reject), `POST /restart`.
  - `agent_config.py` — the pure config-translation core (model selection, persona
    system prompt, task/instructions, MCP server map, interrupt policy, env-var
    fallbacks). This is what the tests target.
  - Session state persists across restarts via a LangGraph SQLite checkpointer on
    the `/workspace` PVC.
- **Chart** (`chart/`) — a cluster-scoped `LanguageAgentRuntime` named `deepagents`
  (single image, httpGet `/health` probes, no init container).

## Install

Requires the `language-operator` chart (which provides the `LanguageAgentRuntime`
CRD) installed first.

```sh
helm install deepagents oci://ghcr.io/language-operator/charts/deepagents
```

Then reference the runtime from a `LanguageAgent`:

```yaml
apiVersion: langop.io/v1alpha1
kind: LanguageAgent
metadata:
  name: researcher
spec:
  runtime: deepagents
  model: <your-LanguageModel>          # routed via the LiteLLM gateway
  instructions: |
    Research the question and write a concise, cited summary.
  tools:
    - <your-mcp-tool>                   # e.g. context7
```

The agent runs its `instructions` on startup. Watch it with `kubectl logs`, or
`kubectl port-forward` and open `/` for the live view (streaming output, HITL
Approve/Reject buttons, and Restart).

## Development

| Target           | What it does                                                        |
| ---------------- | ------------------------------------------------------------------- |
| `make build`     | Build the image (`:<git-sha>` + `:latest`).                         |
| `make test`      | Build, then run the pytest suite (`test.sh`) inside the image.      |
| `make publish`   | Build and push the image tags to ghcr.io.                           |
| `make dev`       | Build, import into local k3s, and `helm upgrade` the runtime.       |
| `make uninstall` | Uninstall the runtime release.                                      |

Local inner loop: `make dev` (requires the operator chart installed in the cluster),
then `kubectl get languageagentruntime deepagents`.

Run the unit tests directly with `uv`:

```sh
uv run pytest -q
```

## CI

Three GitHub Actions workflows (`.github/workflows/`):

- **test.yaml** — builds the image, runs `test.sh` (pytest), and `helm lint` /
  `helm template` the chart.
- **build-image.yaml** — builds and pushes the image to `ghcr.io` with a
  `docker/metadata-action` tag matrix (on `main` and `v*` tags).
- **release-chart.yaml** — `helm package` + `helm push` to
  `oci://ghcr.io/language-operator/charts` (on `main` and `v*` tags).

Cut a release with the `/release major|minor|patch` command
(`.claude/commands/release.md`): it bumps `chart/Chart.yaml` version/appVersion +
`chart/values.yaml` `image.tag` + the git tag `vX.Y.Z` in lockstep.
