"""Tests for the pure config-translation core (agent_config.py).

Covers the same ground opencode-adapter's test.sh covers for seed-config: full
config mapping, env-var fallbacks, graceful-empty, primary-model selection, and
non-http tool skipping.
"""

import textwrap

import pytest

import agent_config


FULL_CONFIG = textwrap.dedent(
    """
    agent:
      name: research-agent
      namespace: default
    instructions: |-
      Research the topic and write a concise summary.
    personas:
      - name: Ada
        tone: precise
        personality: curious
        expertise: distributed systems
    tools:
      context7:
        endpoint: http://context7.default.svc.cluster.local:8080/mcp
        protocol: mcp
      legacy-grpc:
        endpoint: grpc://legacy.default.svc.cluster.local:9000
        protocol: mcp
    models:
      fast-model:
        role: secondary
        provider: anthropic
        model: claude-haiku-4-5
        endpoint: http://gateway.default.svc.cluster.local:8000
      smart-model:
        role: primary
        provider: anthropic
        model: claude-sonnet-4-6
        endpoint: http://gateway.default.svc.cluster.local:8000
    a2a:
      skills:
        - id: research
          name: Research
          description: Investigate a topic and cite sources.
          tags: [research, web]
    peers:
      summarizer:
        url: http://summarizer.default.svc.cluster.local:8080
      legacy-grpc:
        url: grpc://legacy.default.svc.cluster.local:9000
    """
)


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Write a config.yaml and point load_operator_config at it."""

    def _write(text: str):
        path = tmp_path / "config.yaml"
        path.write_text(text)
        monkeypatch.setattr(agent_config, "CONFIG_PATH", str(path))
        return str(path)

    return _write


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "MODEL_ENDPOINT",
        "LLM_MODEL",
        "MCP_SERVERS",
        "AGENT_INSTRUCTIONS",
        "AGENT_PERSONA",
        "AGENT_REPO_DIR",
        "HITL_TOOLS",
        "A2A_MODE",
        "A2A_SKILLS",
        "A2A_PEERS",
        "A2A_PUBLIC_URL",
        "A2A_VERSION",
        "AGENT_NAME",
        "AGENT_NAMESPACE",
        "PORT",
    ):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# Full config.yaml mapping
# --------------------------------------------------------------------------- #
def test_full_config_model(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    params = agent_config.resolve_model(cfg)
    # primary role wins over the (earlier) secondary entry
    assert params["name"] == "claude-sonnet-4-6"
    assert params["base_url"] == "http://gateway.default.svc.cluster.local:8000/v1"
    assert params["api_key"] == "sk-langop-proxy"


def test_full_config_build_model_instance(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    model = agent_config.build_model(cfg)
    assert model.model_name == "claude-sonnet-4-6"
    assert model.openai_api_base == "http://gateway.default.svc.cluster.local:8000/v1"


def test_full_config_system_prompt_is_persona_only(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    prompt = agent_config.build_system_prompt(cfg)
    # system prompt = persona (identity), NOT the task instructions
    assert "You are Ada." in prompt
    assert "Tone: precise" in prompt
    assert "Expertise: distributed systems" in prompt
    assert "Research the topic" not in prompt


def test_full_config_task_is_instructions(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    task = agent_config.build_task(cfg)
    assert task == "Research the topic and write a concise summary."


def test_full_config_interrupt_on(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    servers = agent_config.build_mcp_servers(cfg)
    interrupt_on = agent_config.build_interrupt_on(cfg, servers.keys())
    # side-effecting builtins + the resolved MCP tool, all → True
    assert interrupt_on == {"write_file": True, "edit_file": True, "context7": True}


def test_full_config_mcp_servers(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    servers = agent_config.build_mcp_servers(cfg)
    assert servers == {
        "context7": {
            "transport": "http",
            "url": "http://context7.default.svc.cluster.local:8080/mcp",
        }
    }
    # non-http (grpc://) endpoint is skipped
    assert "legacy-grpc" not in servers


# --------------------------------------------------------------------------- #
# Primary-model selection precedence
# --------------------------------------------------------------------------- #
def test_primary_role_precedence(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    key, model = agent_config.select_primary_model(cfg)
    assert key == "smart-model"
    assert model["model"] == "claude-sonnet-4-6"


def test_no_role_falls_back_to_first(write_config):
    cfg = agent_config.load_operator_config(
        write_config(
            textwrap.dedent(
                """
                models:
                  only-model:
                    model: gpt-4o
                    endpoint: http://gateway.default.svc.cluster.local:8000
                """
            )
        )
    )
    params = agent_config.resolve_model(cfg)
    assert params["name"] == "gpt-4o"


def test_model_name_falls_back_to_crd_key(write_config):
    cfg = agent_config.load_operator_config(
        write_config(
            textwrap.dedent(
                """
                models:
                  claude-sonnet-4-6:
                    role: primary
                    endpoint: http://gateway.default.svc.cluster.local:8000
                """
            )
        )
    )
    params = agent_config.resolve_model(cfg)
    assert params["name"] == "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# Env-var fallback (no config file)
# --------------------------------------------------------------------------- #
def test_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_config, "CONFIG_PATH", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("MODEL_ENDPOINT", "http://gateway.default.svc.cluster.local:8000")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6,claude-haiku-4-5")
    monkeypatch.setenv("MCP_SERVERS", "http://context7.default.svc.cluster.local:8080/mcp")
    monkeypatch.setenv("AGENT_INSTRUCTIONS", "Be helpful and terse.")
    monkeypatch.setenv("AGENT_PERSONA", "You are Sage, a careful analyst.")

    cfg = agent_config.load_operator_config()
    assert cfg == {}

    params = agent_config.resolve_model(cfg)
    assert params["name"] == "claude-sonnet-4-6"  # first of LLM_MODEL
    assert params["base_url"] == "http://gateway.default.svc.cluster.local:8000/v1"

    # task comes from AGENT_INSTRUCTIONS, persona from AGENT_PERSONA
    assert agent_config.build_task(cfg) == "Be helpful and terse."
    assert agent_config.build_system_prompt(cfg) == "You are Sage, a careful analyst."

    servers = agent_config.build_mcp_servers(cfg)
    assert servers == {
        "context7": {
            "transport": "http",
            "url": "http://context7.default.svc.cluster.local:8080/mcp",
        }
    }


# --------------------------------------------------------------------------- #
# Graceful empty (no config, no env)
# --------------------------------------------------------------------------- #
def test_graceful_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_config, "CONFIG_PATH", str(tmp_path / "absent.yaml"))
    cfg = agent_config.load_operator_config()
    assert cfg == {}
    assert agent_config.resolve_model(cfg) is None
    assert agent_config.build_model(cfg) is None
    assert agent_config.build_system_prompt(cfg) == ""
    assert agent_config.build_task(cfg) == ""
    assert agent_config.build_mcp_servers(cfg) == {}
    # A2A degrades to empty too (no skills, no peers, autonomous mode)
    assert agent_config.a2a_mode() == ""
    assert agent_config.build_a2a_skills(cfg) == []
    assert agent_config.build_peers(cfg) == {}


def test_interrupt_on_default_no_tools():
    # no MCP tools → just the side-effecting builtins
    assert agent_config.build_interrupt_on({}, []) == {"write_file": True, "edit_file": True}


def test_interrupt_on_env_overrides(monkeypatch):
    monkeypatch.setenv("HITL_TOOLS", "none")
    assert agent_config.build_interrupt_on({}, ["context7"]) == {}
    monkeypatch.setenv("HITL_TOOLS", "*")
    assert agent_config.build_interrupt_on({}, ["context7"]) == {
        "write_file": True,
        "edit_file": True,
        "context7": True,
    }
    monkeypatch.setenv("HITL_TOOLS", "context7")
    assert agent_config.build_interrupt_on({}, ["context7"]) == {"context7": True}


def test_workspace_root(monkeypatch):
    monkeypatch.delenv("AGENT_REPO_DIR", raising=False)
    assert agent_config.workspace_root() == "/workspace"
    monkeypatch.setenv("AGENT_REPO_DIR", "/workspace/repo")
    assert agent_config.workspace_root() == "/workspace/repo"


def test_persona_explicit_system_prompt(write_config):
    cfg = agent_config.load_operator_config(
        write_config(
            textwrap.dedent(
                """
                personas:
                  - name: Ada
                    systemPrompt: You are a terse code reviewer.
                    tone: ignored-when-systemprompt-present
                """
            )
        )
    )
    prompt = agent_config.build_system_prompt(cfg)
    assert prompt == "You are a terse code reviewer."


# --------------------------------------------------------------------------- #
# A2A (Agent2Agent) helpers
# --------------------------------------------------------------------------- #
def test_a2a_mode(monkeypatch):
    assert agent_config.a2a_mode() == ""
    monkeypatch.setenv("A2A_MODE", "Server")
    assert agent_config.a2a_mode() == "server"  # lower-cased + stripped


def test_a2a_skills_from_config(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    skills = agent_config.build_a2a_skills(cfg)
    assert skills == [
        {
            "id": "research",
            "name": "Research",
            "description": "Investigate a topic and cite sources.",
            "tags": ["research", "web"],
        }
    ]


def test_a2a_skills_from_env(monkeypatch):
    monkeypatch.setenv("A2A_SKILLS", "research, summarize ,")  # spaces + trailing comma
    skills = agent_config.build_a2a_skills({})
    assert skills == [
        {"id": "research", "name": "research", "description": "", "tags": []},
        {"id": "summarize", "name": "summarize", "description": "", "tags": []},
    ]


def test_a2a_card_constructed_url(write_config, monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "research-agent")
    monkeypatch.setenv("AGENT_NAMESPACE", "agents")
    monkeypatch.setenv("PORT", "9000")
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    card = agent_config.build_a2a_card(cfg)
    assert card["name"] == "research-agent"
    assert card["url"] == "http://research-agent.agents.svc.cluster.local:9000"
    assert card["version"] == agent_config.DEFAULT_AGENT_VERSION
    assert card["protocol_version"] == agent_config.A2A_PROTOCOL_VERSION
    assert card["capabilities"] == {"streaming": False, "push_notifications": False}
    assert card["default_input_modes"] == ["text/plain"]
    assert [s["id"] for s in card["skills"]] == ["research"]


def test_a2a_card_public_url_and_version_override(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "specialist")
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://specialist.example.com/")  # trailing slash trimmed
    monkeypatch.setenv("A2A_VERSION", "2.3.4")
    card = agent_config.build_a2a_card({})
    assert card["url"] == "https://specialist.example.com"
    assert card["version"] == "2.3.4"


def test_a2a_peers_from_config(write_config):
    cfg = agent_config.load_operator_config(write_config(FULL_CONFIG))
    peers = agent_config.build_peers(cfg)
    # http peer kept (trailing path-less base url), grpc peer skipped
    assert peers == {"summarizer": "http://summarizer.default.svc.cluster.local:8080"}
    assert "legacy-grpc" not in peers


def test_a2a_peers_from_env(monkeypatch):
    monkeypatch.setenv(
        "A2A_PEERS",
        "http://summarizer.default.svc.cluster.local:8080/, grpc://nope:9000",
    )
    peers = agent_config.build_peers({})
    # env-derived name comes from the URL host; non-http skipped; trailing / trimmed
    assert peers == {"summarizer": "http://summarizer.default.svc.cluster.local:8080"}
