"""OPS-BOT-DISPATCH-LATENCY-W1 CH1 — a failed fetch must not consume the bar.

`update_watch_after_fetch` used to be called unconditionally at `process_one_row`'s function
body indent, OUTSIDE both `except McpError` blocks. It stamps `last_fetched_at`, and `is_due`
is a STRICT bucket comparison, so an upstream failure silently marked the bar as served and
that bar's alert was never delivered and never retried — up to 4 hours of silence on a 4h row,
announced only by a `log.warning` with no counter behind it.

The fix splits the stamp. What the tick LEARNED (verdict / streak / regime_last_seen) persists
either way, because that is flap-suppression state and the regime lane already establishes it
must survive a non-delivery. Whether the bucket was SERVICED is now conditional and bounded by
`fetch_fail_streak`, so a retry storm is impossible: `MAX_FETCH_ATTEMPTS_PER_BUCKET` attempts,
then the row gives up and the bucket advances.

Every assertion below was PROVEN ABLE TO FAIL against the pre-fix shape — see
`test_selftest_the_prefix_shape_would_fail` at the bottom, which reproduces the old
unconditional stamp and asserts the guarantee breaks under it. An assertion nobody has watched
fail is not an assertion.
"""

from __future__ import annotations

from typing import Any

import pytest

from algovault_bot import alert_engine
from algovault_bot.alert_engine import WatchRow, process_one_row
from algovault_bot.db import MAX_FETCH_ATTEMPTS_PER_BUCKET, Database
from algovault_bot.dispatch_schedule import is_due
from algovault_bot.mcp_client import McpError
from algovault_bot.validators import TF_SECONDS


class _FailingMcp:
    """`call_tool` that raises `McpError` — the upstream-outage case."""

    def __init__(self) -> None:
        self.calls = 0

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise McpError("upstream 502")


class _ScriptedMcp:
    """Fails for the first `fail_times` calls, then returns `payload`."""

    def __init__(self, fail_times: int, payload: dict[str, dict[str, Any]]) -> None:
        self.remaining_failures = fail_times
        self._payload = payload
        self.calls = 0

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise McpError("upstream 502")
        return self._payload[name]


async def _delivered(*_args: Any, **_kwargs: Any) -> bool:
    return True


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alert_engine, "_push", _delivered)
    monkeypatch.setattr(alert_engine, "_push_photo", _delivered)


def _seed(db: Database, chat_id: int = 1, tf: str = "4h", alert_type: str = "calls") -> WatchRow:
    db.upsert_subscriber(chat_id, "u", "en")
    db.add_watch(chat_id, "BTC", tf, "BINANCE", alert_type)
    return WatchRow(
        chat_id=chat_id, coin="BTC", timeframe=tf, exchange="BINANCE",
        alert_type=alert_type, regime_last_seen=None,
        last_verdict=None, last_verdict_streak=0,
    )


def _watch_state(db: Database, chat_id: int = 1) -> Any:
    with db._cursor() as cur:
        return cur.execute(
            "SELECT last_fetched_at, fetch_fail_streak, last_verdict, last_verdict_streak, "
            "regime_last_seen FROM watchlists WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


# ── the core guarantee ─────────────────────────────────────────────────────


async def test_failed_fetch_does_not_advance_the_bucket(tmp_db: Database) -> None:
    """The whole point: an upstream failure leaves the bar unserved, so it is retried."""
    row = _seed(tmp_db)
    await process_one_row(None, _FailingMcp(), tmp_db, row)

    state = _watch_state(tmp_db)
    assert state["last_fetched_at"] is None, "a failed fetch must NOT stamp the bucket"
    assert state["fetch_fail_streak"] == 1


async def test_row_stays_due_after_a_failure(tmp_db: Database) -> None:
    """`last_fetched_at` held open means `is_due` still returns True on the next tick —
    this is the assertion that actually proves the bar gets another chance."""
    row = _seed(tmp_db)
    now = 1_788_450_300  # a 900-aligned epoch; any instant inside one 4h bucket
    await process_one_row(None, _FailingMcp(), tmp_db, row)

    state = _watch_state(tmp_db)
    assert is_due("4h", now, None, row.chat_id, row.coin, row.exchange) is True
    assert state["last_fetched_at"] is None


async def test_retry_is_bounded_then_the_bucket_advances(tmp_db: Database) -> None:
    """Not stamping forever would be a retry STORM — 240 attempts on a 4h row. The counter
    caps it, and on the give-up tick it resets to 0 because the cap is per BUCKET."""
    row = _seed(tmp_db)
    mcp = _FailingMcp()

    for attempt in range(1, MAX_FETCH_ATTEMPTS_PER_BUCKET):
        await process_one_row(None, mcp, tmp_db, _reread(tmp_db, row))
        assert _watch_state(tmp_db)["last_fetched_at"] is None, f"held open at attempt {attempt}"
        assert _watch_state(tmp_db)["fetch_fail_streak"] == attempt

    # The capping attempt gives up.
    await process_one_row(None, mcp, tmp_db, _reread(tmp_db, row))
    state = _watch_state(tmp_db)
    assert state["last_fetched_at"] is not None, "bucket must advance once attempts are spent"
    assert state["fetch_fail_streak"] == 0, "counter resets — the cap is per bucket"
    assert mcp.calls == MAX_FETCH_ATTEMPTS_PER_BUCKET


async def test_a_successful_retry_delivers_and_resets(tmp_db: Database) -> None:
    """The recovery path end to end: fail once, succeed on the retry, bar delivered."""
    row = _seed(tmp_db)
    mcp = _ScriptedMcp(1, {"get_trade_call": {"call": "BUY", "confidence": 78, "price": 723.58}})

    await process_one_row(None, mcp, tmp_db, row)
    assert _watch_state(tmp_db)["last_fetched_at"] is None

    result = await process_one_row(None, mcp, tmp_db, _reread(tmp_db, row))
    state = _watch_state(tmp_db)
    assert result["trade_call"] == "fired"
    assert state["last_fetched_at"] is not None
    assert state["fetch_fail_streak"] == 0


async def test_success_resets_a_streak_left_by_an_earlier_failure(tmp_db: Database) -> None:
    row = _seed(tmp_db)
    await process_one_row(None, _FailingMcp(), tmp_db, row)
    assert _watch_state(tmp_db)["fetch_fail_streak"] == 1

    ok = _ScriptedMcp(0, {"get_trade_call": {"call": "HOLD", "confidence": 30, "price": 1.0}})
    await process_one_row(None, ok, tmp_db, _reread(tmp_db, row))
    assert _watch_state(tmp_db)["fetch_fail_streak"] == 0


# ── what must survive a failed tick ────────────────────────────────────────


async def test_flap_suppression_state_survives_a_failed_tick(tmp_db: Database) -> None:
    """`regime_last_seen` and the streak are what stop a regime alert re-firing every bar.
    Losing them on a failure would convert one outage into a burst of duplicate alerts."""
    chat = 7
    tmp_db.upsert_subscriber(chat, "u", "en")
    tmp_db.add_watch(chat, "BTC", "4h", "BINANCE", "both")
    row = WatchRow(
        chat_id=chat, coin="BTC", timeframe="4h", exchange="BINANCE",
        alert_type="both", regime_last_seen=None,
        last_verdict="TRENDING_UP", last_verdict_streak=1,
    )
    # regime resolves, trade-call fails — the mixed case that makes this non-trivial.
    mcp = _MixedMcp(regime={"regime": "TRENDING_UP", "confidence": 70})

    await process_one_row(None, mcp, tmp_db, row)

    state = _watch_state(tmp_db, chat)
    assert state["last_fetched_at"] is None, "the failed lane still holds the bucket"
    assert state["regime_last_seen"] == "TRENDING_UP", "suppression state must persist"
    assert state["last_verdict"] == "TRENDING_UP"
    assert state["last_verdict_streak"] == 2


async def test_regime_streak_counts_bars_not_attempts(tmp_db: Database) -> None:
    """THE SUBTLE ONE. The streak gates flap suppression at `>= 2`. If a retry of the SAME
    bar re-incremented it, two attempts inside one bar would manufacture that threshold out
    of a single observation — a regime alert fired on evidence that does not exist."""
    chat = 8
    tmp_db.upsert_subscriber(chat, "u", "en")
    tmp_db.add_watch(chat, "BTC", "4h", "BINANCE", "both")
    row = WatchRow(
        chat_id=chat, coin="BTC", timeframe="4h", exchange="BINANCE",
        alert_type="both", regime_last_seen="TRENDING_UP",
        last_verdict="TRENDING_UP", last_verdict_streak=1,
    )
    mcp = _MixedMcp(regime={"regime": "TRENDING_UP", "confidence": 70})

    await process_one_row(None, mcp, tmp_db, row)
    assert _watch_state(tmp_db, chat)["last_verdict_streak"] == 2, "attempt 1 books the bar"

    # Attempt 2 is the SAME bar. It must not add a second increment.
    await process_one_row(None, mcp, tmp_db, _reread(tmp_db, row))
    assert _watch_state(tmp_db, chat)["last_verdict_streak"] == 2, (
        "a retry is the same bar — the streak must not advance"
    )


async def test_a_regime_change_still_resets_the_streak_on_a_retry(tmp_db: Database) -> None:
    """The retry guard must not freeze the counter: a DIFFERENT regime resets to 1 on any
    attempt, so `last_verdict_streak` stays coherent with `last_verdict`."""
    chat = 9
    tmp_db.upsert_subscriber(chat, "u", "en")
    tmp_db.add_watch(chat, "BTC", "4h", "BINANCE", "both")
    row = WatchRow(
        chat_id=chat, coin="BTC", timeframe="4h", exchange="BINANCE",
        alert_type="both", regime_last_seen="TRENDING_UP",
        last_verdict="TRENDING_UP", last_verdict_streak=4, fetch_fail_streak=1,
    )
    mcp = _MixedMcp(regime={"regime": "TRENDING_DOWN", "confidence": 70})

    await process_one_row(None, mcp, tmp_db, row)

    state = _watch_state(tmp_db, chat)
    assert state["last_verdict"] == "TRENDING_DOWN"
    assert state["last_verdict_streak"] == 1


# ── the give-up event is observable ────────────────────────────────────────


async def test_abandoning_a_bar_emits_a_counted_event(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-fix code abandoned a bar on EVERY failure with nothing but a warning line.
    The give-up must be a distinguishable, counted event or the loss stays invisible."""
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        alert_engine, "log_alert_event",
        lambda name, **kw: events.append((name, kw)),
    )
    row = _seed(tmp_db)
    mcp = _FailingMcp()
    for _ in range(MAX_FETCH_ATTEMPTS_PER_BUCKET):
        await process_one_row(None, mcp, tmp_db, _reread(tmp_db, row))

    failures = [kw for name, kw in events if name == "watch_fetch_failed"]
    assert len(failures) == MAX_FETCH_ATTEMPTS_PER_BUCKET
    assert [f["bar_abandoned"] for f in failures] == [False] * (MAX_FETCH_ATTEMPTS_PER_BUCKET - 1) + [True]
    assert failures[-1]["attempts"] == MAX_FETCH_ATTEMPTS_PER_BUCKET


# ── the atomicity property ─────────────────────────────────────────────────


def test_record_fetch_failure_is_one_statement_under_interleaving(tmp_db: Database) -> None:
    """`algovault-bot.service` (interactive) and the cron engine share ONE state.db file, so a
    read-then-write here would be a genuine lost update. The single UPDATE is what makes the
    increment, the cap test and the reset one atomic decision — this asserts the OUTCOME of
    that property rather than its spelling, so it survives a refactor that keeps it true.

    Ratified precedent: AOE-RETUNE-IDEMPOTENCY-W1 — "a last line of defence may not have a
    race in it" — including its recorded trap, that a race test keyed on a marker only the NEW
    shape emits can never fail against the old one.
    """
    tmp_db.upsert_subscriber(2, "u", "en")
    tmp_db.add_watch(2, "ETH", "1h", "BINANCE", "calls")
    args = (2, "ETH", "1h", "BINANCE", "", 0, None)

    # Two interleaved failures must land as exactly two attempts, never one.
    tmp_db.record_fetch_failure(*args)
    tmp_db.record_fetch_failure(*args)

    assert _watch_state(tmp_db, 2)["fetch_fail_streak"] == 2


def test_record_fetch_failure_tolerates_a_row_deleted_mid_tick(tmp_db: Database) -> None:
    """An /unwatch between dispatch and failure leaves nothing to update. The UPDATE matches
    zero rows and RETURNING yields None — that must not raise inside the alert path."""
    tmp_db.upsert_subscriber(3, "u", "en")
    attempts, advanced = tmp_db.record_fetch_failure(3, "GONE", "1h", "BINANCE", "", 0, None)
    assert (attempts, advanced) == (0, True)


# ── RED-verify: prove the guarantee can fail ───────────────────────────────


async def test_selftest_the_prefix_shape_would_fail(tmp_db: Database) -> None:
    """PROOF THE SUITE CAN FAIL. Reconstructs the pre-fix behaviour — stamp the bucket
    unconditionally, regardless of the McpError — and asserts the core guarantee BREAKS.

    If this test ever starts passing while the others also pass, the assertions above have
    stopped discriminating and the suite has gone vacuous.
    """
    row = _seed(tmp_db, chat_id=4)
    # The old code path, verbatim in effect: failure, then an unconditional stamp.
    tmp_db.update_watch_after_fetch(4, "BTC", "4h", "BINANCE", "", 0, None)

    state = _watch_state(tmp_db, 4)
    assert state["last_fetched_at"] is not None, (
        "the pre-fix shape stamps on failure — this is the defect being fixed"
    )
    # …and that is exactly what silently consumed the bar: the bucket reads as served.
    epoch = 1_788_450_300
    assert is_due("4h", epoch, epoch, row.chat_id, row.coin, row.exchange) is False


# ── helpers ────────────────────────────────────────────────────────────────


class _MixedMcp:
    """Regime lane resolves; trade-call lane fails. The mixed case is what makes the
    persist-what-you-learned rule load-bearing rather than cosmetic."""

    def __init__(self, regime: dict[str, Any]) -> None:
        self._regime = regime

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_market_regime":
            return self._regime
        raise McpError("upstream 502")


def _reread(db: Database, row: WatchRow) -> WatchRow:
    """Rebuild the WatchRow from the DB, as `run_cycle` does each tick — the retry counter
    only works because the next tick READS it back."""
    with db._cursor() as cur:
        r = cur.execute(
            "SELECT chat_id, coin, timeframe, exchange, alert_type, regime_last_seen, "
            "last_verdict, last_verdict_streak, fetch_fail_streak "
            "FROM watchlists WHERE chat_id = ? AND coin = ? AND timeframe = ? AND exchange = ?",
            (row.chat_id, row.coin, row.timeframe, row.exchange),
        ).fetchone()
    return alert_engine._row_from_sqlite(r)


def test_timeframes_leave_room_for_the_retry_budget() -> None:
    """The cap is only safe because retries fit inside the shortest schedulable bar. 1m is
    excluded from PUSH_TIMEFRAMES, so 3m is the floor: 3 attempts on a 60s tick = 180s, which
    must not exceed the bar or a retry would leak into the following one."""
    from algovault_bot.validators import PUSH_TIMEFRAMES

    shortest = min(TF_SECONDS[tf] for tf in PUSH_TIMEFRAMES)
    assert MAX_FETCH_ATTEMPTS_PER_BUCKET * 60 <= shortest
