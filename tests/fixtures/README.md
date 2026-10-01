# Operator fixture corpus

Vendored from [`coding-runtime`](https://github.com/language-operator/coding-runtime)
at **v0.1.4** — the same version as the `BASE` pinned in the `Dockerfile`:

- `operator/` ← `test/fixtures/operator/` — configs as the operator writes them
  (`*.yaml`), plus env-only inputs (`*.env.json`).
- `golden/` ← `test/fixtures/golden/normalized/` — coding-runtime's normalized
  output for each.

`tests/test_agent_config.py` checks `agent_config` against the golden fields that
map onto it (primary model, gateway URL/key, instructions, MCP tools). Re-sync both
directories whenever the base image is bumped.
