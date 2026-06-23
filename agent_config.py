"""Operator config translation for the deepagents runtime.

This is the testable core of the runtime — the role ``seed-config.mjs`` plays for
the opencode adapter, except there is no init container: ``server.py`` calls these
functions directly at startup.

The Language Operator injects ``/etc/agent/config.yaml`` (read-only) into every
agent container. These pure functions translate that config — plus the env-var
fallbacks the operator also sets — into the inputs deepagents needs: a model
pointed at the LiteLLM gateway, an assembled system prompt, and an MCP server map.

Everything degrades gracefully: a missing config file, a missing models section,
or a missing tools section each fall back to env vars or an empty result rather
than raising, so the server can still come up and answer health probes.

No FastAPI imports live here — this module is what ``tests/test_agent_config.py``
targets. ``langchain_openai`` is imported lazily inside ``build_model`` so the
pure translation logic can be exercised without the heavy dependency.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import yaml

# Path the operator mounts the agent config at (read-only).
CONFIG_PATH = "/etc/agent/config.yaml"

# Placeholder credential. The LiteLLM gateway holds the real provider keys; agents
# never see them. The OpenAI-compatible client still requires *some* api_key.
GATEWAY_API_KEY = "sk-langop-proxy"

# deepagents' built-in filesystem tools that mutate state. By default the agent
# pauses (human-in-the-loop) before these; the read-only builtins (ls/read_file)
# are not interrupted. See deepagents/middleware/filesystem.py.
BUILTIN_WRITE_TOOLS = ("write_file", "edit_file")


def load_operator_config(path: str = CONFIG_PATH) -> dict:
    """Parse ``/etc/agent/config.yaml``; return ``{}`` if absent or unreadable.

    Mirrors the opencode seed-config behavior: a parse failure is logged-by-caller
    territory, but here we simply degrade to an empty dict so the runtime can fall
    back to env vars.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}


def select_primary_model(cfg: dict):
    """Return ``(crd_key, model_dict)`` for the primary model, or ``None``.

    Precedence: the entry whose ``role == "primary"``; otherwise the first entry
    in insertion order. ``cfg["models"]`` is keyed by CRD name.
    """
    models = (cfg or {}).get("models") or {}
    if not models:
        return None
    for key, model in models.items():
        if (model or {}).get("role") == "primary":
            return key, (model or {})
    key = next(iter(models))
    return key, (models[key] or {})


def resolve_model(cfg: dict):
    """Resolve the model name + gateway base_url, or ``None`` if undeterminable.

    Name source of truth: prefer the config ``model`` field, fall back to the CRD
    key (the operator routes LiteLLM on the names carried in ``LLM_MODEL``). When
    no models section exists, fall back to the first name in ``LLM_MODEL`` and the
    ``MODEL_ENDPOINT`` env var. The base_url always has ``/v1`` appended — the
    gateway is OpenAI-compatible.
    """
    selected = select_primary_model(cfg)
    if selected is not None:
        crd_key, model = selected
        name = model.get("model") or crd_key
        endpoint = model.get("endpoint") or os.environ.get("MODEL_ENDPOINT")
    else:
        names = [s.strip() for s in os.environ.get("LLM_MODEL", "").split(",") if s.strip()]
        name = names[0] if names else None
        endpoint = os.environ.get("MODEL_ENDPOINT")

    if not name or not endpoint:
        return None
    return {
        "name": name,
        "base_url": endpoint.rstrip("/") + "/v1",
        "api_key": GATEWAY_API_KEY,
    }


def build_model(cfg: dict):
    """Build a ``ChatOpenAI`` pointed at the LiteLLM gateway, or ``None``.

    deepagents accepts a LangChain model instance, so this is how all LLM traffic
    is routed through the gateway. Imported lazily so the pure resolution logic
    above stays importable without ``langchain_openai``.
    """
    params = resolve_model(cfg)
    if params is None:
        return None
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=params["name"],
        base_url=params["base_url"],
        api_key=params["api_key"],
    )


def _persona_block(persona: dict) -> str:
    """Render one persona as prompt text.

    Prefer an explicit ``systemPrompt`` (the operator may pre-render one);
    otherwise assemble from ``tone``/``personality``/``expertise`` with labels.
    This mirrors the content the operator places in the ``AGENT_PERSONA`` env var.
    """
    persona = persona or {}
    explicit = persona.get("systemPrompt")
    if explicit and str(explicit).strip():
        return str(explicit).strip()

    lines = []
    name = persona.get("name")
    if name:
        lines.append(f"You are {name}.")
    for field, label in (("tone", "Tone"), ("personality", "Personality"), ("expertise", "Expertise")):
        value = persona.get(field)
        if value and str(value).strip():
            lines.append(f"{label}: {str(value).strip()}")
    return "\n".join(lines)


def build_system_prompt(cfg: dict) -> str:
    """Assemble the agent's system prompt (its role/identity) from personas.

    The persona is the agent's *identity*; the task to execute (``instructions``)
    is a separate kickoff message — see ``build_task``. Falls back to the
    ``AGENT_PERSONA`` env var when the config carries no personas. Returns ``""``
    when there is no persona (the agent then uses deepagents' default prompt).
    """
    cfg = cfg or {}
    parts: list[str] = []

    for persona in cfg.get("personas") or []:
        block = _persona_block(persona)
        if block.strip():
            parts.append(block.strip())

    if not parts:
        env_persona = os.environ.get("AGENT_PERSONA", "")
        if env_persona.strip():
            parts.append(env_persona.strip())

    return "\n\n".join(parts).strip()


def build_task(cfg: dict) -> str:
    """The task the agent executes autonomously: the top-level ``instructions``.

    This becomes the kickoff user message of the autonomous run (mirroring how
    claude-code passes ``AGENT_INSTRUCTIONS`` as the first prompt). Falls back to
    the ``AGENT_INSTRUCTIONS`` env var. Returns ``""`` when there is no task — the
    runtime then stays idle instead of auto-running.
    """
    cfg = cfg or {}
    instructions = cfg.get("instructions")
    if not (instructions and str(instructions).strip()):
        instructions = os.environ.get("AGENT_INSTRUCTIONS", "")
    return str(instructions).strip() if instructions else ""


def build_interrupt_on(cfg: dict, tool_names=None) -> dict:
    """Build the ``create_deep_agent(interrupt_on=…)`` human-in-the-loop policy.

    Maps tool name → ``True`` (all decisions — approve/edit/reject/respond —
    allowed). Default policy pauses before *side-effecting* operations: the
    built-in ``write_file``/``edit_file`` plus every resolved MCP tool (external
    side effects). Read-only builtins (``ls``/``read_file``) are never paused.

    Override with the ``HITL_TOOLS`` env var: ``"*"`` interrupts on every tool
    (builtin writers + all MCP tools), a comma list names exactly which tools to
    pause on, and ``"none"``/``""`` disables interrupts entirely.
    """
    tool_names = list(tool_names or [])
    override = os.environ.get("HITL_TOOLS")

    if override is not None:
        value = override.strip()
        if value in ("", "none"):
            return {}
        if value == "*":
            names = list(BUILTIN_WRITE_TOOLS) + tool_names
        else:
            names = [n.strip() for n in value.split(",") if n.strip()]
    else:
        names = list(BUILTIN_WRITE_TOOLS) + tool_names

    return {name: True for name in dict.fromkeys(names)}


def _name_from_url(url: str) -> str:
    """Derive a stable server name from an MCP URL host (e.g. ``context7``)."""
    host = urlparse(url).hostname or url
    return host.split(".")[0] or "mcp"


def build_mcp_servers(cfg: dict) -> dict:
    """Build the ``MultiServerMCPClient`` config map from the tools section.

    Shape: ``{name: {"transport": "http", "url": endpoint}}`` (transport "http" =
    Streamable HTTP). Non-http endpoints are skipped, the same guard opencode
    uses. Falls back to the comma-separated ``MCP_SERVERS`` env var (full MCP URLs
    incl. ``/mcp``) when the config has no tools.
    """
    cfg = cfg or {}
    tools = cfg.get("tools") or {}
    servers: dict[str, dict] = {}

    if tools:
        for name, tool in tools.items():
            endpoint = (tool or {}).get("endpoint")
            if not endpoint or not str(endpoint).startswith(("http://", "https://")):
                continue
            servers[name] = {"transport": "http", "url": endpoint}
    else:
        for url in (s.strip() for s in os.environ.get("MCP_SERVERS", "").split(",")):
            if not url or not url.startswith(("http://", "https://")):
                continue
            servers[_name_from_url(url)] = {"transport": "http", "url": url}

    return servers


def workspace_root() -> str:
    """Writable root for the FilesystemBackend + checkpointer.

    ``AGENT_REPO_DIR`` when the operator git-clones a repo into the workspace,
    otherwise the ``/workspace`` PVC.
    """
    return os.environ.get("AGENT_REPO_DIR") or "/workspace"
