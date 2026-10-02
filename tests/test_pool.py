"""Warm pool with a fake `claude` that speaks the stream-json protocol."""

import asyncio

import pytest

from conftest import FAKE_CLAUDE_STREAM, engine, write_script


@pytest.fixture
def claude_spec(fakebin):
    # The pool picks the protocol by executable name, so the fake must be called "claude".
    script = write_script(fakebin / "claude", FAKE_CLAUDE_STREAM)
    return engine.AgentSpec("Claude", [str(script), "-p", "{prompt}"], is_persistent=True, timeout=10)


@pytest.fixture
def pool():
    p = engine.AgentPool()
    yield p
    asyncio.run(p.shutdown()) if p.agents else None


def test_supports_only_known_protocols(pool, claude_spec, fakebin):
    assert pool.supports(claude_spec)
    other = engine.AgentSpec("X", [str(write_script(fakebin / "other", "print(1)")), "{prompt}"], is_persistent=True)
    assert not pool.supports(other)
    claude_spec.is_persistent = False
    assert not pool.supports(claude_spec)


def test_same_run_reuses_session_and_new_run_starts_clean(pool, claude_spec):
    async def main():
        a = await pool.ask(claude_spec, "one", session="run1")
        b = await pool.ask(claude_spec, "two", session="run1")
        pool.end_session("run1")
        await asyncio.sleep(0.3)  # background /clear
        c = await pool.ask(claude_spec, "three", session="run2")
        await pool.shutdown()
        return a, b, c

    a, b, c = asyncio.run(main())
    assert a.extra["pool"] and "seen=1" in a.output
    assert "seen=2" in b.output and "session=1" in b.output       # same run keeps context
    assert "seen=1" in c.output and "session=1" not in c.output   # new run: fresh session


def test_auto_heals_after_crash(pool, claude_spec):
    async def main():
        await pool.ask(claude_spec, "warm", session="s")
        crashed = await pool.ask(claude_spec, "CRASH", session="s")
        healed = await pool.ask(claude_spec, "after", session="s")
        agent = pool.get(claude_spec)
        await pool.shutdown()
        return crashed, healed, agent.starts

    crashed, healed, starts = asyncio.run(main())
    assert not crashed.ok
    assert healed.ok and "last=after" in healed.output and starts >= 2


def test_busy_agent_falls_back(pool, claude_spec):
    async def main():
        slow = asyncio.ensure_future(pool.ask(claude_spec, "SLOW", session="s"))
        await asyncio.sleep(0.5)
        second = await pool.ask(claude_spec, "x", session="t")
        slow.cancel()
        try:
            await slow
        except asyncio.CancelledError:
            pass
        agent = pool.get(claude_spec)
        alive_after_cancel = agent.alive
        await pool.shutdown()
        return second, alive_after_cancel

    second, alive_after_cancel = asyncio.run(main())
    assert second is None               # caller falls back to a one-shot process
    assert alive_after_cancel is False  # cancelled mid-answer → process discarded


def test_timeout_kills_process(pool, claude_spec):
    claude_spec.timeout = 1

    async def main():
        r = await pool.ask(claude_spec, "SLOW", session="s")
        agent = pool.get(claude_spec)
        await pool.shutdown()
        return r, agent.alive

    r, alive = asyncio.run(main())
    assert not r.ok and not alive


def test_shutdown_leaves_no_processes(pool, claude_spec):
    async def main():
        await pool.sync([claude_spec])
        proc = pool.get(claude_spec).proc
        await pool.shutdown()
        return proc

    proc = asyncio.run(main())
    assert proc.returncode is not None and pool.agents == {}
