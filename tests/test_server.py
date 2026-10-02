"""Tests for server.py: the Runner's finish signal and the WorkspaceBackend.

Runner: task mode turns "the runner has nothing left to do" into the process
exit, so what matters is when ``on_finish`` fires and with which status. The agent
is a stand-in: only its ``astream`` is exercised.

WorkspaceBackend: it hooks two private deepagents methods, so it is tested only
through the public operations. If a deepagents upgrade stops routing paths
through those hooks, these tests are what notices.
"""

import asyncio

import pytest

from server import Runner, WorkspaceBackend


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


# --------------------------------------------------------------------------- #
# WorkspaceBackend: the agent's paths are the real paths under the workspace
# --------------------------------------------------------------------------- #
@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _paths(entries):
    return sorted(e["path"] for e in entries)


def test_real_path_is_not_nested(workspace):
    backend = WorkspaceBackend(str(workspace))
    result = backend.write(f"{workspace}/story.txt", "once\n")
    assert result.error is None
    assert (workspace / "story.txt").read_text() == "once\n"
    # the bug: the real root was appended under itself
    assert not (workspace / str(workspace).lstrip("/")).exists()


def test_all_spellings_reach_the_same_file(workspace):
    backend = WorkspaceBackend(str(workspace))
    backend.write("notes.txt", "hello\n")
    for spelling in ("notes.txt", "/notes.txt", f"{workspace}/notes.txt"):
        assert backend.read(spelling).file_data["content"] == "hello\n", spelling
    assert backend.edit(f"{workspace}/notes.txt", "hello", "goodbye").error is None
    assert (workspace / "notes.txt").read_text() == "goodbye\n"


def test_listings_use_real_paths(workspace):
    backend = WorkspaceBackend(str(workspace))
    backend.write("/a.txt", "needle one\n")
    backend.write("/docs/b.md", "needle two\n")

    expected_top = [f"{workspace}/a.txt", f"{workspace}/docs/"]
    assert _paths(backend.ls(str(workspace)).entries) == expected_top
    assert _paths(backend.ls("/").entries) == expected_top  # "/" is the workspace too
    assert _paths(backend.ls(f"{workspace}/docs").entries) == [f"{workspace}/docs/b.md"]

    assert _paths(backend.glob(f"{workspace}/**/*.md").matches) == [f"{workspace}/docs/b.md"]
    assert _paths(backend.glob("*.md", path=f"{workspace}/docs").matches) == [f"{workspace}/docs/b.md"]

    hits = backend.grep("needle")
    assert _paths(hits.matches) == [f"{workspace}/a.txt", f"{workspace}/docs/b.md"]
    # what a listing returns can be handed straight back
    for match in hits.matches:
        assert "needle" in backend.read(match["path"]).file_data["content"]


def test_paths_stay_confined_to_the_workspace(workspace, tmp_path):
    backend = WorkspaceBackend(str(workspace))
    for traversal in ("../escape.txt", f"{workspace}/../escape.txt"):
        with pytest.raises(ValueError):
            backend.write(traversal, "x")
    # an absolute path outside the workspace lands inside it, not on the real fs
    assert backend.write("/home/user/x.txt", "x").error is None
    assert (workspace / "home/user/x.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_symlinked_root_accepts_both_spellings(workspace, tmp_path):
    link = tmp_path / "ws-link"
    link.symlink_to(workspace)
    backend = WorkspaceBackend(str(link))
    backend.write(f"{link}/via-link.txt", "a")
    backend.write(f"{workspace}/via-real.txt", "b")
    assert sorted(p.name for p in workspace.iterdir()) == ["via-link.txt", "via-real.txt"]
    # listed under the root as it was configured
    assert _paths(backend.ls("/").entries) == [f"{link}/via-link.txt", f"{link}/via-real.txt"]
