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
    build_interrupt_on,
    build_mcp_servers,
    build_model,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_operator_config()

    model = build_model(cfg)
    system_prompt = build_system_prompt(cfg)
    task = build_task(cfg)
    servers = build_mcp_servers(cfg)
    root = workspace_root()

    tools = []
    if servers:
        try:
            client = MultiServerMCPClient(servers)
            tools = await client.get_tools()
            print(f"deepagents-adapter: loaded {len(tools)} MCP tool(s) from {list(servers)}")
        except Exception as exc:
            print(f"deepagents-adapter: MCP tool load failed ({exc}); continuing with none")

    interrupt_on = build_interrupt_on(cfg, servers.keys())

    if model is None:
        print("deepagents-adapter: no model resolved — runtime will idle")
    else:
        print(f"deepagents-adapter: model={model.model_name} via {model.openai_api_base}")
        print(f"deepagents-adapter: interrupt_on={list(interrupt_on)} task={'yes' if task else 'none'}")

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
