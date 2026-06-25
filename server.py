"""Autonomous deepagents executor with a thin HTTP server.

This runtime does not wait to be asked. On startup it builds the agent from the
operator-injected ``/etc/agent/config.yaml`` (via :mod:`agent_config`) and then
**autonomously runs the agent's task** (its ``instructions``) as a single session,
streaming every event to STDOUT — so ``kubectl logs`` is a real UI — and to any
connected browser. It runs the task once, then idles (staying Ready for probes).

Human-in-the-loop is wired: ``create_deep_agent(interrupt_on=…)`` pauses before
side-effecting tools. langgraph interrupts cannot be fire-and-forget — resuming
needs an out-of-band channel — so the thin server exposes ``POST /resume`` (and the
UI's Approve/Reject buttons) to continue a paused run, plus ``POST /restart``.

Endpoints:
  GET  /health  — probe target.
  GET  /        — live UI (AGENT_NAME templated).
  GET  /events  — SSE: replays the run so far, then streams live events.
  GET  /state   — current status + any pending HITL interrupt.
  POST /resume  — {decisions:[…]} → continue a paused run.
  POST /restart — cancel + re-run the task on a fresh thread.

A2A (Agent2Agent) is additive (see :mod:`agent_config`):
  * ``A2A_MODE=server`` — request-driven: serve ``GET /.well-known/agent-card.json``
    + JSON-RPC (``message/send``/``tasks/get``) on this same app and do **not**
    auto-run ``instructions``. Each request runs the agent on a fresh thread.
  * ``A2A_PEERS`` — the autonomous agent gains ``delegate_to_<peer>`` tools that
    call peer agents over A2A, alongside its MCP tools.

State persists across restarts via a LangGraph SQLite checkpointer on /workspace.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from agent_config import (
    a2a_mode,
    build_a2a_card,
    build_interrupt_on,
    build_mcp_servers,
    build_model,
    build_peers,
    build_system_prompt,
    build_task,
    load_operator_config,
    workspace_root,
)

from deepagents import create_deep_agent

try:  # import path moved across deepagents versions; tolerate both.
    from deepagents.backends.filesystem import FilesystemBackend
except ImportError:  # pragma: no cover - resolved at image build time
    from deepagents.backends import FilesystemBackend

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

APP_DIR = Path(__file__).resolve().parent
AGENT_NAME = os.environ.get("AGENT_NAME", "agent")

INDEX_HTML = (APP_DIR / "index.html").read_text(encoding="utf-8").replace(
    "__AGENT_NAME__", html.escape(AGENT_NAME)
)


def _text_of(message) -> str:
    """Extract plain text from a LangChain message or message chunk."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
        return "".join(out)
    return ""


def _normalize(item) -> dict | None:
    """Turn one astream (messages, subgraphs) item into a UI/stdout event, or None."""
    # subgraphs=True wraps each item as (namespace, payload).
    payload = item[1] if (isinstance(item, tuple) and isinstance(item[0], tuple)) else item
    message = payload[0] if isinstance(payload, tuple) else payload

    mtype = type(message).__name__
    if mtype == "ToolMessage":
        return {"type": "tool_result", "name": getattr(message, "name", ""), "text": _text_of(message)}

    text = _text_of(message)
    if text:
        return {"type": "token", "text": text}

    for call in getattr(message, "tool_call_chunks", None) or []:
        if call.get("name"):
            return {"type": "tool_call", "name": call["name"]}
    return None


class Runner:
    """Owns the single autonomous run, broadcasting events to stdout + subscribers."""

    def __init__(self, agent, task: str):
        self._agent = agent
        self._task = task
        self.thread_id = uuid.uuid4().hex
        self.status = "idle"          # idle | running | interrupted | completed | error
        self.error: str | None = None
        self.pending_interrupt: dict | None = None
        self.events: list[dict] = []
        self._seq = 0
        self._subscribers: set[asyncio.Queue] = set()
        self._resume: asyncio.Future | None = None
        self._run_task: asyncio.Task | None = None

    # -- broadcast ---------------------------------------------------------
    def _publish(self, event: dict) -> None:
        event = {"seq": self._seq, **event}
        self._seq += 1
        self.events.append(event)
        # stdout = the pod-logs UI
        print(f"[agent] {json.dumps(event)}", flush=True)
        for q in list(self._subscribers):
            q.put_nowait(event)

    def _set_status(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self._publish({"type": "status", "status": status, **({"error": error} if error else {})})

    async def subscribe(self):
        """Replay the run so far, then yield live events (deduped by seq)."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        backlog = list(self.events)
        last = backlog[-1]["seq"] if backlog else -1
        try:
            for ev in backlog:
                yield ev
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                if ev["seq"] > last:
                    yield ev
        finally:
            self._subscribers.discard(q)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._agent is None:
            self._set_status("idle", "no model configured")
            return
        if not self._task:
            self._set_status("idle", "no task (instructions) provided")
            return
        self._run_task = asyncio.create_task(self._run())

    def _config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}}

    async def _run(self) -> None:
        self.error = None
        self._set_status("running")
        agent_input = {"messages": [{"role": "user", "content": self._task}]}
        try:
            while True:
                async for item in self._agent.astream(
                    agent_input, config=self._config(),
                    stream_mode="messages", subgraphs=True,
                ):
                    ev = _normalize(item)
                    if ev:
                        self._publish(ev)

                snapshot = await self._agent.aget_state(self._config())
                if snapshot.interrupts:
                    decisions = await self._await_decisions(snapshot.interrupts[0])
                    agent_input = Command(resume={"decisions": decisions})
                    continue
                break
            self._set_status("completed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface to logs + UI instead of dying silently
            self._set_status("error", str(exc))

    async def _await_decisions(self, interrupt) -> list:
        """Publish the HITL request and block until /resume supplies decisions."""
        request = interrupt.value  # HITLRequest: {action_requests, review_configs}
        self.pending_interrupt = {"id": interrupt.id, "request": request}
        self._resume = asyncio.get_event_loop().create_future()
        self._set_status("interrupted")
        self._publish({"type": "interrupt", "id": interrupt.id, "request": request})
        try:
            decisions = await self._resume
        finally:
            self.pending_interrupt = None
            self._resume = None
        self._set_status("running")
        return decisions

    def resume(self, decisions: list) -> bool:
        if self._resume is None or self._resume.done():
            return False
        self._resume.set_result(decisions)
        return True

    async def restart(self) -> None:
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        self.thread_id = uuid.uuid4().hex
        self.pending_interrupt = None
        self._resume = None
        # Clear the replay buffer for fresh subscribers, but keep _seq monotonic so
        # connected clients don't mistake new events for already-seen ones.
        self.events = []
        self._publish({"type": "restart"})
        self.start()


# --------------------------------------------------------------------------- #
# A2A (Agent2Agent) — server (serve a card + JSON-RPC) and client (peer tools).
# All ``a2a-sdk`` imports are lazy so the rest of the runtime stays importable
# without it and so this block is only exercised when A2A is configured.
# --------------------------------------------------------------------------- #
async def run_agent_once(agent, text: str) -> str:
    """Run the agent once on ``text`` (fresh thread) and return its final answer.

    Used by the A2A server executor: synchronous request/response, so it runs to
    completion (``ainvoke``) and returns the last assistant message's text. The
    server-mode agent is built with HITL disabled, so it never pauses mid-request.
    """
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}
    result = await agent.ainvoke({"messages": [{"role": "user", "content": text}]}, config=config)
    for message in reversed(result.get("messages") or []):
        if type(message).__name__ == "AIMessage":
            answer = _text_of(message)
            if answer.strip():
                return answer
    return ""


def _build_agent_card(card: dict):
    """Translate the pure ``build_a2a_card`` dict into an ``a2a-sdk`` AgentCard."""
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
    from a2a.utils import TransportProtocol

    skills = [
        AgentSkill(id=s["id"], name=s["name"], description=s["description"], tags=s["tags"])
        for s in card["skills"]
    ]
    caps = card["capabilities"]
    return AgentCard(
        name=card["name"],
        description=card["description"],
        version=card["version"],
        supported_interfaces=[
            AgentInterface(
                url=card["url"],
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=card["protocol_version"],
            )
        ],
        capabilities=AgentCapabilities(
            streaming=caps["streaming"],
            push_notifications=caps["push_notifications"],
        ),
        default_input_modes=card["default_input_modes"],
        default_output_modes=card["default_output_modes"],
        skills=skills,
    )


def _make_executor(agent):
    """An a2a-sdk AgentExecutor that runs the deepagents agent per request."""
    from a2a.helpers import new_task_from_user_message
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.tasks import TaskUpdater
    from a2a.types import Part

    class DeepagentsExecutor(AgentExecutor):
        async def execute(self, context, event_queue) -> None:
            text = context.get_user_input()
            # A Task must be enqueued before any status/artifact update. Reuse an
            # existing task (resubmission) or mint one from the incoming message.
            task = context.current_task or new_task_from_user_message(context.message)
            if context.current_task is None:
                await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await updater.start_work()
            try:
                answer = await run_agent_once(agent, text)
            except Exception:
                await updater.failed()
                raise
            await updater.add_artifact([Part(text=answer)], name="response")
            await updater.complete()

        async def cancel(self, context, event_queue) -> None:  # MVP: no cancellation
            raise NotImplementedError("cancel is not supported")

    return DeepagentsExecutor()


def mount_a2a_server(app: FastAPI, agent, cfg: dict) -> None:
    """Mount the Agent Card + JSON-RPC (message/send, tasks/get) on the FastAPI app.

    JSON-RPC lives at POST ``/`` (coexists with the GET ``/`` live UI); the card at
    ``GET /.well-known/agent-card.json``. Backed by an in-memory TaskStore.
    """
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import (
        add_a2a_routes_to_fastapi,
        create_agent_card_routes,
        create_jsonrpc_routes,
    )
    from a2a.server.tasks import InMemoryTaskStore

    card = _build_agent_card(build_a2a_card(cfg))
    handler = DefaultRequestHandler(
        agent_executor=_make_executor(agent),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        # Native A2A v1 JSON-RPC (message/send, tasks/get). Our card advertises
        # protocol_version "1.0", so a2a-sdk clients use the v1 transport too.
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
    )
    skills = [s["id"] for s in build_a2a_card(cfg)["skills"]]
    print(f"deepagents-adapter: A2A server mode — card at /.well-known/agent-card.json, skills={skills}")


def _safe_tool_name(name: str) -> str:
    """Sanitize a peer name into a valid tool-name suffix (letters/digits/_)."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")
    return cleaned or "peer"


def _extract_a2a_text(resp) -> str:
    """Pull text out of an a2a-sdk StreamResponse (task artifacts / message parts)."""
    def parts_text(parts) -> str:
        return "".join(p.text for p in (parts or []) if getattr(p, "text", ""))

    out = []
    task = getattr(resp, "task", None)
    if task is not None:
        for art in getattr(task, "artifacts", None) or []:
            out.append(parts_text(getattr(art, "parts", None)))
    message = getattr(resp, "message", None)
    if message is not None:
        out.append(parts_text(getattr(message, "parts", None)))
    update = getattr(resp, "artifact_update", None)
    if update is not None and getattr(update, "artifact", None) is not None:
        out.append(parts_text(update.artifact.parts))
    return "".join(out)


async def _a2a_send(base_url: str, message: str) -> str:
    """Send one A2A ``message/send`` to a peer and return its answer text."""
    import httpx

    from a2a.client import A2ACardResolver, ClientConfig, create_client
    from a2a.types import Message, Part, Role, SendMessageRequest
    from a2a.utils import TransportProtocol

    async with httpx.AsyncClient(timeout=120) as hc:
        card = await A2ACardResolver(hc, base_url=base_url).get_agent_card()
        config = ClientConfig(
            streaming=False,
            httpx_client=hc,
            supported_protocol_bindings=[TransportProtocol.JSONRPC],
        )
        client = await create_client(card, config)
        request = SendMessageRequest(
            message=Message(
                role=Role.ROLE_USER,
                parts=[Part(text=message)],
                message_id=uuid.uuid4().hex,
            )
        )
        chunks = [_extract_a2a_text(resp) async for resp in client.send_message(request)]
        await client.close()
    return "".join(chunks).strip()


async def build_peer_tools(cfg: dict) -> list:
    """Build ``delegate_to_<peer>`` LangChain tools from the configured peers.

    Mirrors the MCP tool load: a peer whose card can't be fetched at startup is
    logged and still registered (the call will surface the error at delegate time).
    """
    from langchain_core.tools import StructuredTool

    tools = []
    for name, base_url in build_peers(cfg).items():
        description = f"Delegate a task to the '{name}' A2A peer agent and return its answer."
        try:
            import httpx

            from a2a.client import A2ACardResolver

            async with httpx.AsyncClient(timeout=10) as hc:
                card = await A2ACardResolver(hc, base_url=base_url).get_agent_card()
            if getattr(card, "description", ""):
                description = f"Delegate to peer agent '{card.name}': {card.description}"
        except Exception as exc:
            print(f"deepagents-adapter: A2A peer '{name}' card fetch failed ({exc}); registering tool anyway")

        def _delegate_factory(url: str):
            async def _delegate(message: str) -> str:
                return await _a2a_send(url, message)

            return _delegate

        tools.append(
            StructuredTool.from_function(
                coroutine=_delegate_factory(base_url),
                name=f"delegate_to_{_safe_tool_name(name)}",
                description=description,
            )
        )
        print(f"deepagents-adapter: A2A peer tool delegate_to_{_safe_tool_name(name)} -> {base_url}")
    return tools


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_operator_config()

    model = build_model(cfg)
    system_prompt = build_system_prompt(cfg)
    task = build_task(cfg)
    servers = build_mcp_servers(cfg)
    root = workspace_root()

    server_mode = a2a_mode() == "server"

    tools = []
    if model is not None:
        if servers:
            try:
                client = MultiServerMCPClient(servers)
                tools = await client.get_tools()
                print(f"deepagents-adapter: loaded {len(tools)} MCP tool(s) from {list(servers)}")
            except Exception as exc:
                print(f"deepagents-adapter: MCP tool load failed ({exc}); continuing with none")
        # Peer-delegation tools sit alongside the MCP tools (A2A client side).
        tools += await build_peer_tools(cfg)

    # In server mode each A2A request is synchronous with no human to resume, so
    # HITL is disabled; otherwise pause before side-effecting tools as usual.
    interrupt_on = {} if server_mode else build_interrupt_on(cfg, servers.keys())

    if model is None:
        print("deepagents-adapter: no model resolved — runtime will idle")
    else:
        print(f"deepagents-adapter: model={model.model_name} via {model.openai_api_base}")
        print(f"deepagents-adapter: interrupt_on={list(interrupt_on)} task={'yes' if task else 'none'} a2a_mode={a2a_mode() or 'autonomous'}")

    ckpt_dir = os.path.join(root, ".deepagents")
    os.makedirs(ckpt_dir, exist_ok=True)
    db_path = os.path.join(ckpt_dir, "checkpoints.sqlite")

    async with AsyncExitStack() as stack:
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(db_path))
        agent = None
        if model is not None:
            agent = create_deep_agent(
                model=model,
                system_prompt=system_prompt or None,
                tools=tools,
                # virtual_mode=True: every path the agent uses is confined under
                # root_dir (/workspace, the writable PVC) and '..'/'~' traversal is
                # blocked. Without this an absolute path the model invents (e.g.
                # /home/user/x) hits the read-only root fs. The agent's filesystem
                # IS its workspace.
                backend=FilesystemBackend(root_dir=root, virtual_mode=True),
                checkpointer=saver,
                interrupt_on=interrupt_on,
            )
            print(f"deepagents-adapter: agent ready (workspace={root}, checkpoints={db_path})")

        runner = Runner(agent, task)
        app.state.runner = runner

        if server_mode and agent is not None:
            # Request-driven: stand up the A2A server and wait for calls instead of
            # auto-running the task.
            mount_a2a_server(app, agent, cfg)
        else:
            if server_mode:
                print("deepagents-adapter: A2A server mode requested but no model — idling")
            runner.start()
        try:
            yield
        finally:
            if runner._run_task and not runner._run_task.done():
                runner._run_task.cancel()


app = FastAPI(title=f"deepagents-adapter ({AGENT_NAME})", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML, headers={"cache-control": "no-store"})


@app.get("/state")
async def state(request: Request):
    r: Runner = request.app.state.runner
    return {
        "agent": AGENT_NAME,
        "status": r.status,
        "thread_id": r.thread_id,
        "error": r.error,
        "pending_interrupt": r.pending_interrupt,
    }


@app.get("/events")
async def events(request: Request):
    r: Runner = request.app.state.runner

    async def gen():
        async for ev in r.subscribe():
            if await request.is_disconnected():
                break
            if ev.get("type") == "ping":
                yield ": ping\n\n"
            else:
                yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"cache-control": "no-store", "x-accel-buffering": "no"},
    )


@app.post("/resume")
async def resume(request: Request):
    r: Runner = request.app.state.runner
    body = await request.json()
    decisions = body.get("decisions")
    if not isinstance(decisions, list):
        return JSONResponse({"error": "decisions must be a list"}, status_code=400)
    if not r.resume(decisions):
        return JSONResponse({"error": "no pending interrupt"}, status_code=409)
    return {"ok": True}


@app.post("/restart")
async def restart(request: Request):
    r: Runner = request.app.state.runner
    await r.restart()
    return {"ok": True, "thread_id": r.thread_id}
