"""Tests for the Runner's finish signal (server.py).

Task mode turns "the runner has nothing left to do" into the process exit, so
what matters is when ``on_finish`` fires and with which status. The agent is a
stand-in: only its ``astream`` is exercised.
"""

import asyncio

from server import Runner


class FakeAgent:
    """Yields nothing; finishes, raises, or blocks until released."""

    def __init__(self, error: Exception | None = None, gate: asyncio.Event | None = None):
        self._error = error
        self._gate = gate
        self.runs = 0

    async def astream(self, *_args, **_kwargs):
        self.runs += 1
        if self._gate is not None:
            await self._gate.wait()
        if self._error is not None:
            raise self._error
        return
        yield  # pragma: no cover - makes this an async generator


async def test_finish_reports_completed():
    finished = []
    runner = Runner(FakeAgent(), "do the thing", on_finish=finished.append)
    runner.start()
    await runner._run_task
    assert finished == ["completed"]


async def test_finish_reports_error():
    finished = []
    runner = Runner(FakeAgent(error=RuntimeError("gateway down")), "do the thing", on_finish=finished.append)
    runner.start()
    await runner._run_task
    assert finished == ["error"]
    assert runner.error == "gateway down"


async def test_nothing_to_run_finishes_idle():
    finished = []
    Runner(None, "do the thing", on_finish=finished.append).start()  # no model
    Runner(FakeAgent(), "", on_finish=finished.append).start()  # no instructions
    assert finished == ["idle", "idle"]


async def test_restart_does_not_finish_the_cancelled_run():
    finished = []
    gate = asyncio.Event()
    agent = FakeAgent(gate=gate)
    runner = Runner(agent, "do the thing", on_finish=finished.append)
    runner.start()
    await asyncio.sleep(0)  # let the first run reach the gate

    await runner.restart()
    assert finished == []  # the cancelled run must not end the process

    gate.set()
    await runner._run_task
    assert finished == ["completed"]
    assert agent.runs == 2


async def test_close_ends_event_streams_after_delivering_the_run():
    runner = Runner(FakeAgent(), "do the thing")
    live = runner.subscribe()
    first = asyncio.ensure_future(anext(live))  # subscribed before the run starts
    await asyncio.sleep(0)

    runner.start()
    await runner._run_task
    runner.close()

    seen = [await first] + [ev async for ev in live]
    assert [ev["status"] for ev in seen] == ["running", "completed"]
    # A viewer arriving after close still gets the replay, then the stream ends.
    assert [ev["status"] async for ev in runner.subscribe()] == ["running", "completed"]


async def test_no_callback_is_fine():
    runner = Runner(FakeAgent(), "do the thing")
    runner.start()
    await runner._run_task
    assert runner.status == "completed"
