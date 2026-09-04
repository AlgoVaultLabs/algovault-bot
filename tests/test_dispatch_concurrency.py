"""OPS-BOT-DISPATCH-LATENCY-W1 CH3 — the tick is concurrent, and per-chat it is not.

Two properties, and they pull in opposite directions, which is why both are asserted:

  1. ROWS RUN IN PARALLEL. The tick was a plain `for` with one `await` per row, so its wall
     time was the SUM of the row times — the journal showed elapsed / (rows x per-row p50) at
     1.34, a sequential sum. Simply wrapping it in `asyncio.gather` would have changed NOTHING,
     because `McpClient.call_tool` is a blocking `httpx.Client` request and N tasks would
     serialize on the event loop. `call_tool_async` offloads it with `asyncio.to_thread`, and
     `test_rows_for_different_chats_run_in_parallel` is what proves that is actually true
     rather than merely intended.

  2. ROWS FOR ONE SUBSCRIBER DO NOT. Groups are sharded by chat_id and each group stays
     sequential, so the quota gate -> send -> consume span in `process_one_row` is never raced
     against itself whatever `FETCH_CONCURRENCY` is set to. CH2 made the counter safe under a
     race; this makes the race unreachable inside the engine in the first place.

The timing assertions use a blocking `time.sleep` in the fake's `call_tool` deliberately — a
blocking sleep is precisely what a sequential loop cannot hide and an `asyncio.sleep` would
have parallelised even under the old shape, making the test pass against the defect.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from algovault_bot import alert_engine, fetch_budget, mcp_client
from algovault_bot.db import Database

ROW_SECONDS = 0.15


class _SlowMcp:
    """Blocking `call_tool`, plus an occupancy trace so overlap can be asserted directly."""

    def __init__(self, seconds: float = ROW_SECONDS) -> None:
        self.seconds = seconds
        self.live: list[str] = []
        self.max_overlap = 0
        self.per_chat_max: dict[int, int] = {}
        self._live_by_chat: dict[int, int] = {}
        self._lock = __import__("threading").Lock()
        self.current_chat: int | None = None

    def __enter__(self) -> "_SlowMcp":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        chat = self.current_chat
        with self._lock:
            self._live_by_chat[chat] = self._live_by_chat.get(chat, 0) + 1
            total = sum(self._live_by_chat.values())
            self.max_overlap = max(self.max_overlap, total)
            self.per_chat_max[chat] = max(self.per_chat_max.get(chat, 0), self._live_by_chat[chat])
        time.sleep(self.seconds)          # BLOCKING on purpose — see the module docstring
        with self._lock:
            self._live_by_chat[chat] -= 1
        return {"call": "HOLD", "confidence": 10, "price": 1.0}


async def _delivered(*_a: Any, **_k: Any) -> bool:
    return True


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alert_engine, "_push", _delivered)
    monkeypatch.setattr(alert_engine, "_push_photo", _delivered)


def _seed(db: Database, chats: list[int], per_chat: int = 1) -> None:
    coins = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LINK",
             "AVAX", "DOT", "MATIC", "ATOM", "NEAR", "APT", "OP", "ARB"]
    for cid in chats:
        db.upsert_subscriber(cid, "u", "en")
        for i in range(per_chat):
            db.add_watch(cid, coins[i], "4h", "BINANCE", "calls")


async def _tick(db: Database, mcp: _SlowMcp, monkeypatch: pytest.MonkeyPatch) -> float:
    """Run one real `run_cycle` against the fake client; return wall seconds."""
    monkeypatch.setattr(mcp_client, "McpClient", lambda _cfg: mcp)

    real = alert_engine.process_one_row

    async def traced(bot, m, d, row):
        m.current_chat = row.chat_id
        return await real(bot, m, d, row)

    monkeypatch.setattr(alert_engine, "process_one_row", traced)
    t0 = time.monotonic()
    await alert_engine.run_cycle("tok", db.path, "http://x/mcp", "key")
    return time.monotonic() - t0


# ── property 1: it is actually concurrent ──────────────────────────────────


async def test_rows_for_different_chats_run_in_parallel(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ASSERTION THE WHOLE CHAPTER RESTS ON. Eight rows, eight subscribers, one blocking
    150ms call each. Sequential would be ~1.2s; concurrent is ~1 row. Anything above half the
    sequential time means the offload is not working and `gather` is decorative."""
    _seed(tmp_db, list(range(1, 9)))
    monkeypatch.setenv("FETCH_CONCURRENCY", "8")
    mcp = _SlowMcp()

    elapsed = await _tick(tmp_db, mcp, monkeypatch)

    sequential = 8 * ROW_SECONDS
    assert elapsed < sequential / 2, f"{elapsed:.2f}s vs {sequential:.2f}s sequential"
    assert mcp.max_overlap > 1, "no two calls were ever in flight together"


async def test_concurrency_one_restores_sequential_behaviour(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FETCH_CONCURRENCY=1 is the documented rollback — no redeploy, no revert. It must
    genuinely serialise, or the rollback is a placebo."""
    _seed(tmp_db, list(range(11, 15)))
    monkeypatch.setenv("FETCH_CONCURRENCY", "1")
    mcp = _SlowMcp()

    elapsed = await _tick(tmp_db, mcp, monkeypatch)

    assert mcp.max_overlap == 1, "concurrency=1 must never overlap two rows"
    assert elapsed >= 4 * ROW_SECONDS * 0.8


# ── property 2: one subscriber is never raced against itself ───────────────


async def test_rows_for_the_SAME_chat_never_overlap(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat_id shard is what makes CH2's gate->send->consume span unraceable inside the
    engine. Six rows on ONE subscriber with concurrency 8: parallelism is available and must
    still not be taken."""
    _seed(tmp_db, [42], per_chat=6)
    monkeypatch.setenv("FETCH_CONCURRENCY", "8")
    mcp = _SlowMcp()

    await _tick(tmp_db, mcp, monkeypatch)

    assert mcp.per_chat_max.get(42) == 1, "two rows of one subscriber were in flight together"


async def test_the_shard_does_not_serialise_everything(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: sharding must not accidentally serialise DIFFERENT subscribers. Four
    chats x two rows each — per-chat sequential, across-chat parallel."""
    _seed(tmp_db, [51, 52, 53, 54], per_chat=2)
    monkeypatch.setenv("FETCH_CONCURRENCY", "8")
    mcp = _SlowMcp()

    elapsed = await _tick(tmp_db, mcp, monkeypatch)

    assert all(v == 1 for v in mcp.per_chat_max.values())
    assert mcp.max_overlap > 1
    # 8 rows, but only 2 deep per chat -> ~2 row-times, not 8.
    assert elapsed < 8 * ROW_SECONDS / 2


# ── the deadline guard still defers, and still does not cancel ─────────────


async def test_the_deadline_defers_rather_than_cancelling(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard sits BETWEEN rows, so it stops what has not started and never kills a row in
    flight. Cancelling mid-row is the dangerous shape: `process_one_row` can be between a
    delivered Telegram message and its `record_call_delivered`."""
    # NOTE: `tick_deadline_sec()` floors at 1.0s, so a sub-second value is silently clamped.
    # 12 rows x 0.15s = 1.8s sequential against that floor is what actually exercises the guard.
    _seed(tmp_db, [61], per_chat=12)
    monkeypatch.setenv("FETCH_CONCURRENCY", "1")
    monkeypatch.setenv("FETCH_TICK_DEADLINE_SEC", "1")
    mcp = _SlowMcp()

    monkeypatch.setattr(mcp_client, "McpClient", lambda _cfg: mcp)
    counts = await alert_engine.run_cycle("tok", tmp_db.path, "http://x/mcp", "key")

    assert counts["deferred"] >= 1, "the deadline must defer the tail"
    assert counts["processed"] >= 1, "and must not cancel what already started"
    assert counts["processed"] + counts["deferred"] == counts["due"]


async def test_a_deferred_row_is_not_stamped(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred row must stay due — otherwise the deadline silently eats a bar, which is the
    same defect class CH1a fixed for a failed fetch."""
    _seed(tmp_db, [71], per_chat=12)
    monkeypatch.setenv("FETCH_CONCURRENCY", "1")
    monkeypatch.setenv("FETCH_TICK_DEADLINE_SEC", "1")
    mcp = _SlowMcp()
    monkeypatch.setattr(mcp_client, "McpClient", lambda _cfg: mcp)

    await alert_engine.run_cycle("tok", tmp_db.path, "http://x/mcp", "key")

    with tmp_db._cursor() as cur:
        unstamped = cur.execute(
            "SELECT COUNT(*) c FROM watchlists WHERE chat_id = 71 AND last_fetched_at IS NULL"
        ).fetchone()["c"]
    assert unstamped >= 1, "deferred rows must remain due"


# ── the offload itself ─────────────────────────────────────────────────────


async def test_call_tool_async_runs_off_the_event_loop() -> None:
    """Asserts the SEAM, not just its effect: a blocking call inside `call_tool_async` must not
    stall a concurrently-awaiting coroutine. This is the one line that made `gather`
    non-decorative, so it is pinned directly."""
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    class _Blocking:
        def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.15)
            return {"ok": True}

    hb = asyncio.create_task(heartbeat())
    await alert_engine.call_tool_async(_Blocking(), "x", {})
    await hb
    assert ticks > 5, "the event loop was blocked during the MCP call"


def test_concurrency_default_is_bounded() -> None:
    assert 1 <= fetch_budget.DEFAULT_FETCH_CONCURRENCY <= 32
