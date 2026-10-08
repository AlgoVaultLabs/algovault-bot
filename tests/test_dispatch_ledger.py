"""OPS-CLOSEDBAR-DISPATCH-OFFSET-INCIDENT-W2 — the dispatcher writes its own account of each tick.

Every due row gets EXACTLY ONE disposition per tick, recorded by the producer at the tick it
acted. Each mechanism that services a due row late — or not at all — is driven here through the
REAL `run_cycle` (fake MCP, pinned `time.time`), never through a helper: a unit test calling a
helper cannot prove anything calls it.

The seventh mechanism is the one this wave's Plan Mode found measured and recurring: the tick
itself dying (`McpClient` initialise refused while signal-MCP is recreated — 10 times in the 8
days to 2026-10-07). It is recorded `errored`, and the crash still propagates exactly as before.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from algovault_bot import alert_engine, dispatch_schedule, fetch_budget, mcp_client
from algovault_bot.db import (
    DISPATCH_LEDGER_RETENTION_SECONDS,
    DISPOSITIONS,
    Database,
    _iso_to_epoch,
)
from algovault_bot.mcp_client import McpError
from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA
from algovault_bot.validators import TF_SECONDS

# A 4h-aligned bar (2026-10-07 04:00Z — the incident's hour), computed so no 8+ digit literal
# sits in this public file. Ticks fire at :10, as the live timer does.
BAR = int(datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc).timestamp())
T0 = BAR + 10


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class _Mcp:
    """Stand-in for `McpClient`. `plan[coin]` is consumed one outcome per call: "ok" → HOLD,
    "mcp" → McpError (the bounded-retry path), "boom" → RuntimeError (the exception path)."""

    def __init__(self, plan: dict[str, list[str]] | None = None) -> None:
        self.plan = {k: list(v) for k, v in (plan or {}).items()}

    def __enter__(self) -> "_Mcp":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        queue = self.plan.get(arguments["coin"])
        outcome = queue.pop(0) if queue else "ok"
        if outcome == "mcp":
            raise McpError("upstream 502")
        if outcome == "boom":
            raise RuntimeError("deterministic row bug")
        return {"call": "HOLD", "confidence": 10, "price": 1.0}


class _RefusingClient:
    """`McpClient` whose initialise is refused — the 7th path."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __enter__(self) -> "_RefusingClient":
        raise self.exc

    def __exit__(self, *_a: Any) -> None:
        return None


async def _delivered(*_a: Any, **_k: Any) -> bool:
    return True


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Production's dispatch config (0/0/1, so a bucket is due at its bar), no pushes, and the
    alerts.log events captured."""
    monkeypatch.setenv(dispatch_schedule.ENV_OFFSET_PCT, "0")
    monkeypatch.setenv(dispatch_schedule.ENV_CLOSE_GRACE_MIN, "0")
    monkeypatch.setenv(dispatch_schedule.ENV_JITTER_WINDOW_MIN, "1")
    monkeypatch.setattr(alert_engine, "_push", _delivered)
    monkeypatch.setattr(alert_engine, "_push_photo", _delivered)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        alert_engine, "log_alert_event", lambda event, **f: events.append((event, f))
    )
    return events


def _seed(db: Database, chat_id: int, coin: str, tf: str = "4h", *, anchored: bool = True) -> None:
    """A row whose anchor sits in the PREVIOUS bucket, so it is due at `T0` (or never fetched)."""
    db.upsert_subscriber(chat_id, "u", "en")
    db.add_watch(chat_id, coin, tf, "BINANCE", "calls")
    if anchored:
        with db._cursor() as cur:
            cur.execute(
                "UPDATE watchlists SET last_fetched_at = ? WHERE chat_id = ? AND coin = ?",
                (_iso(BAR - TF_SECONDS[tf] + 12), chat_id, coin),
            )


async def _tick(
    db: Database, monkeypatch: pytest.MonkeyPatch, client: Any, now: int
) -> dict[str, int]:
    monkeypatch.setattr(mcp_client, "McpClient", lambda _cfg: client)
    monkeypatch.setattr(alert_engine.time, "time", lambda: float(now))
    return await alert_engine.run_cycle("tok", db.path, "http://x/mcp", "key")


def _ledger(db: Database, chat_id: int | None = None) -> list[sqlite3.Row]:
    with db._cursor() as cur:
        sql = "SELECT * FROM dispatch_ledger"
        args: tuple = ()
        if chat_id is not None:
            sql += " WHERE chat_id = ?"
            args = (chat_id,)
        return list(cur.execute(sql + " ORDER BY chat_id, coin, first_tick, disposition", args))


def _stamp(db: Database, chat_id: int) -> int | None:
    with db._cursor() as cur:
        row = cur.execute(
            "SELECT last_fetched_at FROM watchlists WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return _iso_to_epoch(row["last_fetched_at"])


# ── the mechanisms, one by one ─────────────────────────────────────────────────────────────


async def test_serviced_row_records_bucket_due_tick_and_the_stamp_it_wrote(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_db, 101, "BTC")
    await _tick(tmp_db, monkeypatch, _Mcp(), T0)

    (row,) = _ledger(tmp_db)
    assert row["disposition"] == "serviced"
    assert (row["bucket_epoch"], row["due_epoch"]) == (BAR, BAR)
    assert (row["first_tick"], row["last_tick"], row["n"]) == (T0, T0, 1)
    # `fired_epoch` IS the stamp — the value RETURNING handed back, not a second clock read.
    assert row["fired_epoch"] is not None
    assert row["fired_epoch"] == _stamp(tmp_db, 101)


async def test_a_never_fetched_row_is_serviced_first(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_db, 102, "ETH", anchored=False)
    await _tick(tmp_db, monkeypatch, _Mcp(), T0)
    (row,) = _ledger(tmp_db)
    assert row["disposition"] == "serviced_first"
    assert row["fired_epoch"] == _stamp(tmp_db, 102)


async def test_fetch_failure_then_retry_records_both_ticks_in_one_bucket(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured 71/72 shape, by its bounded-retry mechanism: a failure at the first tick, the
    service one tick later — two dispositions, ONE bucket, the stamp only on the second."""
    _seed(tmp_db, 103, "XAU")
    mcp = _Mcp({"XAU": ["mcp", "ok"]})
    await _tick(tmp_db, monkeypatch, mcp, T0)
    assert _stamp(tmp_db, 103) == BAR - TF_SECONDS["4h"] + 12, "a failure must not stamp"
    await _tick(tmp_db, monkeypatch, mcp, T0 + 60)

    rows = _ledger(tmp_db)
    assert [(r["disposition"], r["bucket_epoch"], r["first_tick"]) for r in rows] == [
        ("fetch_failed", BAR, T0),
        ("serviced", BAR, T0 + 60),
    ]
    assert rows[0]["fired_epoch"] is None
    assert rows[1]["fired_epoch"] == _stamp(tmp_db, 103)


async def test_three_failures_give_up_and_the_give_up_stamp_is_recorded(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_db, 104, "SOL")
    mcp = _Mcp({"SOL": ["mcp", "mcp", "mcp"]})
    for i in range(3):
        await _tick(tmp_db, monkeypatch, mcp, T0 + 60 * i)

    rows = {r["disposition"]: r for r in _ledger(tmp_db)}
    assert set(rows) == {"fetch_failed", "gave_up"}
    # UPSERT: the second failure in the same bucket is a further TICK of one row, not a new row.
    assert (rows["fetch_failed"]["n"], rows["fetch_failed"]["first_tick"],
            rows["fetch_failed"]["last_tick"]) == (2, T0, T0 + 60)
    assert rows["gave_up"]["first_tick"] == T0 + 120
    assert rows["gave_up"]["fired_epoch"] == _stamp(tmp_db, 104)


async def test_a_row_exception_is_errored_and_retried_next_tick(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_db, 105, "BNB")
    mcp = _Mcp({"BNB": ["boom", "ok"]})
    counts = await _tick(tmp_db, monkeypatch, mcp, T0)
    assert counts["errors"] == 1
    await _tick(tmp_db, monkeypatch, mcp, T0 + 60)
    assert [r["disposition"] for r in _ledger(tmp_db)] == ["errored", "serviced"]


async def test_budget_deferral_is_recorded_per_row_and_counts_its_ticks(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FETCH_BUDGET_PER_MIN", "1")
    for cid, coin in ((111, "BTC"), (112, "ETH"), (113, "SOL")):
        _seed(tmp_db, cid, coin)
    mcp = _Mcp()
    for i in range(3):
        await _tick(tmp_db, monkeypatch, mcp, T0 + 60 * i)

    last = _ledger(tmp_db, 113)
    deferred = [r for r in last if r["disposition"] == "deferred_budget"]
    assert len(deferred) == 1
    assert (deferred[0]["n"], deferred[0]["first_tick"], deferred[0]["last_tick"]) == (
        2, T0, T0 + 60
    )
    assert [r["disposition"] for r in last][-1] == "serviced"


async def test_deadline_deferral_is_recorded(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_db, 120, "ADA")
    monkeypatch.setattr(fetch_budget, "tick_deadline_sec", lambda: -1.0)
    await _tick(tmp_db, monkeypatch, _Mcp(), T0)
    assert [r["disposition"] for r in _ledger(tmp_db)] == ["deferred_deadline"]


async def test_an_exhausted_owners_row_is_skipped_and_recorded(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_notice(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(alert_engine, "refuse_and_notify", _no_notice)
    _seed(tmp_db, 130, "DOT")
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count = ?, alerts_window_start = ? WHERE chat_id = ?",
            (FREE_TIER_MONTHLY_QUOTA, datetime.now(timezone.utc).isoformat(), 130),
        )
    counts = await _tick(tmp_db, monkeypatch, _Mcp(), T0)
    assert counts["skipped_exhausted"] == 1
    (row,) = _ledger(tmp_db)
    assert (row["disposition"], row["fired_epoch"]) == ("skipped_exhausted", None)


# ── the seventh path: the tick itself dies ──────────────────────────────────────────────────


async def test_mcp_client_init_failure_records_every_scheduled_row_errored(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_db, 140, "BTC")
    _seed(tmp_db, 141, "ETH")
    counts = await _tick(tmp_db, monkeypatch, _RefusingClient(McpError("init refused")), T0)
    assert counts["errors"] == 1
    assert [r["disposition"] for r in _ledger(tmp_db)] == ["errored", "errored"]


async def test_a_crashing_tick_still_writes_its_account_and_still_crashes(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured shape: a bare httpx error escapes `run_cycle`. Recorded, and re-raised —
    record only; the crash behaviour is not this wave's to change."""
    _seed(tmp_db, 150, "BTC")
    _seed(tmp_db, 151, "ETH")
    exc = httpx.ConnectError("[Errno 111] Connection refused")
    with pytest.raises(httpx.ConnectError):
        await _tick(tmp_db, monkeypatch, _RefusingClient(exc), T0)
    assert {r["disposition"] for r in _ledger(tmp_db)} == {"errored"}
    assert len(_ledger(tmp_db)) == 2


async def test_a_crash_before_scheduling_records_every_due_row_errored(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_db, 160, "BTC")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(alert_engine, "evaluate_delivery", _boom)
    with pytest.raises(sqlite3.OperationalError):
        await _tick(tmp_db, monkeypatch, _Mcp(), T0)
    assert [r["disposition"] for r in _ledger(tmp_db)] == ["errored"]


# ── one account per tick, whole ─────────────────────────────────────────────────────────────


async def test_every_due_row_gets_exactly_one_disposition_and_no_gap(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch, _env: list[tuple[str, dict[str, Any]]]
) -> None:
    monkeypatch.setenv("FETCH_BUDGET_PER_MIN", "3")
    plan = {"C1": ["mcp"], "C2": ["boom"]}
    for cid, coin in ((171, "C1"), (172, "C2"), (173, "C3"), (174, "C4"), (175, "C5")):
        _seed(tmp_db, cid, coin)
    counts = await _tick(tmp_db, monkeypatch, _Mcp(plan), T0)

    rows = _ledger(tmp_db)
    assert len(rows) == counts["due"] == 5
    assert len({(r["chat_id"], r["coin"]) for r in rows}) == 5
    assert all(r["first_tick"] == T0 for r in rows)
    assert not [e for e, _ in _env if e == "dispatch_ledger_gap"]


async def test_a_ledger_write_failure_never_blocks_delivery(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch, _env: list[tuple[str, dict[str, Any]]]
) -> None:
    _seed(tmp_db, 180, "BTC")

    def _fail(self: Database, rows: Any) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Database, "record_dispatch_dispositions", _fail)
    counts = await _tick(tmp_db, monkeypatch, _Mcp(), T0)
    assert counts["processed"] == 1
    assert _stamp(tmp_db, 180) is not None, "the stamp must land whatever the ledger does"
    failed = [f for e, f in _env if e == "dispatch_ledger_write_failed"]
    assert failed and failed[0]["phase"] == "write"
    assert _ledger(tmp_db) == []


async def test_the_ledger_is_pruned_once_an_hour_inside_the_tick(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = BAR - DISPATCH_LEDGER_RETENTION_SECONDS - 3600
    tmp_db.record_dispatch_dispositions(
        [(999, "OLD", "1h", "BINANCE", old, "serviced", old, old + 10, old + 10, old + 12)]
    )
    _seed(tmp_db, 190, "BTC", tf="1h")
    # Not the top of the hour: nothing is pruned.
    await _tick(tmp_db, monkeypatch, _Mcp(), T0 + 30 * 60)
    assert any(r["coin"] == "OLD" for r in _ledger(tmp_db))
    # The :00 tick prunes.
    await _tick(tmp_db, monkeypatch, _Mcp(), T0 + 3600)
    assert not any(r["coin"] == "OLD" for r in _ledger(tmp_db))


# ── the schema: additive, idempotent, closed ─────────────────────────────────────────────────


def test_migration_is_idempotent_and_the_check_is_the_literal(tmp_path: Any) -> None:
    path = str(tmp_path / "s.db")
    Database(path)
    Database(path)  # a second start against an existing ledger must not raise
    with sqlite3.connect(path) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='dispatch_ledger'"
        ).fetchone()[0]
        indexes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dispatch_ledger'"
            )
        }
        inside = ddl.split("disposition IN (", 1)[1].split(")", 1)[0]
        assert tuple(x.strip().strip("'") for x in inside.split(",")) == DISPOSITIONS
        assert {"idx_dispatch_ledger_tf_bucket", "idx_dispatch_ledger_bucket"} <= indexes
        # …and the CHECK is enforced, not decorative.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatch_ledger (chat_id, coin, timeframe, exchange, bucket_epoch,"
                " disposition, due_epoch, first_tick, last_tick) VALUES (1,'A','1h','B',0,"
                "'invented',0,0,0)"
            )
    with sqlite3.connect(path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlists)")]
    assert "dispatch" not in " ".join(cols), "no column is added to watchlists"


# ── due_instant: the producer's own due-time ─────────────────────────────────────────────────

_CFGS = [("0", "0", "1"), ("0", "1", "3"), ("75", "1", "3")]
_ROWS = [(7, "BTC", "BINANCE"), (16240, "XAU", "BINANCE"), (16131, "XAU", "BINGX"), (99, "ETH", "HL")]


@pytest.mark.parametrize("cfg", _CFGS, ids=["0/0/1", "0/1/3", "75/1/3"])
@pytest.mark.parametrize("tf", sorted(TF_SECONDS, key=lambda t: TF_SECONDS[t]))
def test_due_instant_is_the_first_instant_is_due_turns_true(
    cfg: tuple[str, str, str], tf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured through the ENV, as production is — `jitter_window_for`'s headroom bound reads
    the env, so passing the config as kwargs would test a schedule production never runs."""
    pct, grace, window = cfg
    monkeypatch.setenv(dispatch_schedule.ENV_OFFSET_PCT, pct)
    monkeypatch.setenv(dispatch_schedule.ENV_CLOSE_GRACE_MIN, grace)
    monkeypatch.setenv(dispatch_schedule.ENV_JITTER_WINDOW_MIN, window)
    period = TF_SECONDS[tf]
    for chat, coin, exch in _ROWS:
        for t in (BAR + 5 * period + 1, BAR + 7 * period + period // 2):
            d = dispatch_schedule.due_instant(tf, t, chat, coin, exch)
            bucket = dispatch_schedule.target_epoch(tf, t, chat, coin, exch)
            assert dispatch_schedule.target_epoch(tf, d, chat, coin, exch) == bucket
            prev_lo = d - period
            for last in (prev_lo, prev_lo + period // 2, d - 1):
                assert dispatch_schedule.is_due(tf, d, last, chat, coin, exch), (tf, cfg, last)
                assert not dispatch_schedule.is_due(tf, d - 1, last, chat, coin, exch), (
                    tf, cfg, last,
                )


def test_due_instant_honours_the_15m_headroom_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """75/1/3 at 15m: window 3 is bounded to 2 by the tick headroom, so no row's due-time lands
    past 675 + 60 + 60 into its bar (the 2026-08-02 quantization case)."""
    monkeypatch.setenv(dispatch_schedule.ENV_OFFSET_PCT, "75")
    monkeypatch.setenv(dispatch_schedule.ENV_CLOSE_GRACE_MIN, "1")
    monkeypatch.setenv(dispatch_schedule.ENV_JITTER_WINDOW_MIN, "3")
    assert dispatch_schedule.jitter_window_for("15m") == 2
    for chat, coin, exch in _ROWS:
        d = dispatch_schedule.due_instant("15m", BAR + 5, chat, coin, exch)
        bucket = dispatch_schedule.target_epoch("15m", BAR + 5, chat, coin, exch)
        assert 675 + 60 <= d - bucket <= 675 + 60 + 60
