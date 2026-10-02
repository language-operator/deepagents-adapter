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
  and to a live browser view. Then it idles (service mode) or exits (task mode) —
  see [Execution modes](#execution-modes). **No init container** — unlike the
  CLI-wrapping runtimes, the server is our own code and reads the config directly.
  - Built on [`coding-runtime`](https://github.com/language-operator/coding-runtime)'s
    **thin** base: runs as uid 1000 under `tini`, with `git`, `gh` and `glab`
    available to the agent (the operator exports `GH_TOKEN`/`GITLAB_TOKEN`).
  - **No human-in-the-loop.** The agent calls every tool — file writes, MCP
    tools, peer delegation — without asking for approval. Deploy this runtime
    only where an autonomous agent acting on its own is what you want.
  - Endpoints (thin server): `GET /health` (probe), `GET /` (live UI),
    `GET /events` (SSE: replay + live), `GET /state` (run status), `POST /restart`.
  - `agent_config.py` — the pure config-translation core (model selection, persona
    system prompt, task/instructions, MCP server map, A2A card /
    skills / peer map, env-var fallbacks). This is what the tests target.
  - Session state persists across restarts via a LangGraph SQLite checkpointer on
    the `/workspace` PVC.
  - **Paths.** The agent's filesystem is its workspace (`/workspace`, or the cloned
    repo when there is one), and it uses the pod's real paths: `/workspace/notes.txt`,
    `/notes.txt` and `notes.txt` are the same file, and listings show
    `/workspace/notes.txt`. Any other absolute path stays confined to the workspace
    (`/home/user/x` lands at `/workspace/home/user/x`), and `..` is refused.
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
`kubectl port-forward` and open `/` for the live view (streaming output and Restart).

## Execution modes

The runtime supports both `spec.execution.mode` values. It learns the mode from the
`AGENT_EXECUTION_MODE` env var the operator injects, and treats an unset value as
`service`.

| Mode | After the run | Use it for |
| ---- | ------------- | ---------- |
| `service` (default) | Keeps serving: `/health` stays Ready, the live view and `POST /restart` stay available. | A long-lived agent you watch or re-run by hand. |
| `task` | Stops the server and exits, so the run completes. | Scheduled or one-off runs (`spec.execution.schedule`). |

In task mode the exit code is the run's result: `0` when the run completed, `1` when it
failed or there was nothing to run (no model resolved, or no `instructions`). The
server is still up *during* a task run — the pod's probes hit `/health` in both modes —
so `kubectl logs` and the live view work the same way until it exits.

Task mode needs an operator that injects `AGENT_EXECUTION_MODE`. On an older operator
the variable is missing, the runtime behaves as a service, and a task run never
completes; set `spec.execution.activeDeadlineSeconds` as a backstop.

## Gateway credentials

All model traffic goes to the cluster gateway (`MODEL_ENDPOINT`), which holds the real
provider keys. By default the runtime sends the gateway the placeholder key
`sk-langop-proxy`, so every agent in a namespace looks the same to it.

Set `MODEL_API_KEY` on the `LanguageAgent` (through `spec.credentials` or
`spec.deployment.env`) to send a per-agent gateway key instead. A gateway with
per-agent keys enabled then attributes usage to that agent. Unset, empty or
whitespace-only falls back to the placeholder. The startup log says which is in use
(`gateway key: per-agent (MODEL_API_KEY)` or `gateway key: placeholder`) and never
prints the value.

## A2A (Agent2Agent)

deepagents agents can delegate to each other natively over
[A2A](https://a2a-protocol.org). It's **additive** and off by default — it shares
this runtime's existing FastAPI server (port 8080), built on the official
[`a2a-sdk`](https://github.com/a2aproject/a2a-python) (native A2A **v1** JSON-RPC).
Two roles, set via env (the operator injects them per `LanguageAgent`):

**Server (the "specialist")** — `A2A_MODE=server` makes the runtime *request-driven*:
it serves an Agent Card and answers JSON-RPC calls instead of auto-running
`instructions`. Service mode only: a task agent has no Service to be called on, so
in task mode `A2A_MODE=server` is ignored (with a log line) and the task runs.

- `GET /.well-known/agent-card.json` — the Agent Card (name, in-cluster `url`,
  version, capabilities, and `skills`).
- JSON-RPC at `POST /` — `message/send` runs the agent on the incoming message
  (fresh thread per task) and returns a **completed Task** whose artifact is the
  answer; `tasks/get` reads it back. Backed by an in-memory task store.
- `A2A_SKILLS` — comma-separated skill ids to advertise (or a richer
  `a2a.skills:` block — `{id,name,description,tags}` — in `config.yaml`).
- `A2A_PUBLIC_URL` — override the advertised `url` (default: the in-cluster service
  address `http://<name>.<namespace>.svc.cluster.local:<port>`).

**Client (the "orchestrator")** — `A2A_PEERS` (comma-separated peer base URLs, or a
`peers:` block in `config.yaml`) gives the autonomous agent a `delegate_to_<peer>`
tool per peer — alongside its MCP tools — that performs an A2A `message/send` and
returns the peer's answer. The orchestrator stays autonomous; its `instructions`
tell it when to delegate.

With none of these set, behavior is unchanged (autonomous single run).

> MVP scope: synchronous `message/send` only. No `message/stream`, push
> notifications, gRPC/REST transports, or auth/`securitySchemes` (isolation relies
> on the operator's agent-to-agent NetworkPolicy).

## Development

| Target           | What it does                                                        |
| ---------------- | ------------------------------------------------------------------- |
| `make build`     | Build the image (`:<git-sha>` + `:latest`).                         |
| `make test`      | Run the pytest suite (`uv run pytest -q`).                          |
| `make conformance` | Build, then run coding-runtime's conformance suite on the image.  |
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

- **test.yaml** — runs pytest, builds the image and runs coding-runtime's conformance
  suite against it (`adapter` mode), and `helm lint` / `helm template` the chart.
- **build-image.yaml** — builds and pushes the image to `ghcr.io` with a
  `docker/metadata-action` tag matrix (on `main` and `v*` tags).
- **release-chart.yaml** — `helm package` + `helm push` to
  `oci://ghcr.io/language-operator/charts` (on `v*` tags only). It refuses to push a
  chart version that is already published.

Cut a release with the `/release major|minor|patch` command
(`.claude/commands/release.md`): it bumps `chart/Chart.yaml` version/appVersion +
`chart/values.yaml` `image.tag` + the git tag `vX.Y.Z` in lockstep.
