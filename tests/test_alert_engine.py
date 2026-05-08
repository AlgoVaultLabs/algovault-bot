"""Alert-engine unit tests — pure-function logic + lazy-dispatch + flap.

No live MCP / Telegram in these tests; the cron wires those at runtime via
``alert_engine.main``. AC3.7 (regime fires after streak ≥ 2) and AC3.6
(per-TF lazy dispatch) are validated here algorithmically — the live cron
behavior is observed via ``journalctl`` in the C3 verification gate.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from algovault_bot.alert_engine import (
    WatchRow,
    format_regime_alert,
    format_trade_call_alert,
)
from algovault_bot.db import Database
from algovault_bot.quota import (
    FREE_TIER_MONTHLY_QUOTA,
    consume_quota,
    get_quota_state,
)
from algovault_bot.validators import TF_SECONDS


# ── lazy-dispatch SQL filtering ────────────────────────────────


def test_lazy_dispatch_never_fetched_is_due(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    for tf in ("1m", "5m", "15m", "1h", "4h"):
        tmp_db.add_watch(1, "BTC", tf, "BINANCE", "both")
    due = tmp_db.list_due_watches(int(time.time()), TF_SECONDS)
    # All 5 rows have last_fetched_at NULL → all due.
    assert len(due) == 5


def test_lazy_dispatch_only_1m_due_at_61s_after_fetch(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    for tf in ("1m", "5m", "15m", "1h", "4h"):
        tmp_db.add_watch(1, "BTC", tf, "BINANCE", "both")

    # Force last_fetched_at = now-61s on every row.
    past = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    with tmp_db._cursor() as cur:
        cur.execute("UPDATE watchlists SET last_fetched_at = ?", (past,))

    due = tmp_db.list_due_watches(int(time.time()), TF_SECONDS)
    due_tfs = sorted({r["timeframe"] for r in due})
    # 1m TF (60s) → due (61s ≥ 60). Others not.
    assert due_tfs == ["1m"], f"expected only 1m due, got {due_tfs}"


def test_lazy_dispatch_1m_5m_due_at_301s(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    for tf in ("1m", "5m", "15m", "1h", "4h"):
        tmp_db.add_watch(1, "BTC", tf, "BINANCE", "both")

    past = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    with tmp_db._cursor() as cur:
        cur.execute("UPDATE watchlists SET last_fetched_at = ?", (past,))

    due = tmp_db.list_due_watches(int(time.time()), TF_SECONDS)
    due_tfs = sorted({r["timeframe"] for r in due})
    # 1m and 5m due. 15m+ not.
    assert due_tfs == ["1m", "5m"], f"got {due_tfs}"


# ── flap suppression / regime streak ───────────────────────────


def test_regime_alert_format_includes_glyph_and_confidence() -> None:
    row = WatchRow(
        chat_id=1, coin="BTC", timeframe="4h", exchange="BINANCE",
        alert_type="both", regime_last_seen="TRENDING_UP",
        last_verdict="RANGING", last_verdict_streak=2,
    )
    msg = format_regime_alert(row, "TRENDING_UP", "RANGING", 76)
    assert "📊 Regime shift: BTC 4h on BINANCE" in msg
    assert "TRENDING_UP" in msg
    assert "RANGING" in msg
    assert "Confidence: 76" in msg


def test_trade_call_alert_includes_quota_line() -> None:
    from algovault_bot.quota import QuotaState

    row = WatchRow(
        chat_id=1, coin="ETH", timeframe="1h", exchange="BINANCE",
        alert_type="both", regime_last_seen=None,
        last_verdict=None, last_verdict_streak=0,
    )
    msg = format_trade_call_alert(
        row, "BUY", 78, 84250.50, "TRENDING_UP", "NORMAL", "trend up + funding mild",
        QuotaState(47, 100, datetime.now(timezone.utc), 0.47),
    )
    assert "🟢 BUY: ETH 1h on BINANCE" in msg
    assert "Confidence: 78" in msg
    assert "$84,250.50" in msg
    assert "📊 Quota: 47/100 free calls used this month" in msg


# ── bot-side quota counter ─────────────────────────────────────


def test_quota_starts_at_zero(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    state = get_quota_state(tmp_db, 1)
    assert state.used == 0
    assert state.total == FREE_TIER_MONTHLY_QUOTA
    assert not state.exhausted


def test_consume_quota_increments_and_starts_window(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    s1 = consume_quota(tmp_db, 1)
    assert s1.used == 1
    assert s1.window_start is not None
    s2 = consume_quota(tmp_db, 1)
    assert s2.used == 2
    # Same window — window_start unchanged.
    assert s2.window_start == s1.window_start


def test_quota_exhausted_at_100(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    for _ in range(FREE_TIER_MONTHLY_QUOTA):
        consume_quota(tmp_db, 1)
    state = get_quota_state(tmp_db, 1)
    assert state.exhausted
    assert state.used == FREE_TIER_MONTHLY_QUOTA


def test_quota_window_resets_after_30_days(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    consume_quota(tmp_db, 1)
    # Force window_start to 31 days ago.
    past = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count = 73, alerts_window_start = ? "
            "WHERE chat_id = 1", (past,),
        )
    state = get_quota_state(tmp_db, 1)
    # get_quota_state should reset stale window.
    assert state.used == 0
    assert state.window_start is None
