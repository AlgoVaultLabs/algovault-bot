"""Alert-engine unit tests — pure-function logic + lazy-dispatch + flap.

No live MCP / Telegram in these tests; the cron wires those at runtime via
``alert_engine.main``. AC3.7 (regime fires after streak ≥ 2) and AC3.6
(per-TF lazy dispatch) are validated here algorithmically — the live cron
behavior is observed via ``journalctl`` in the C3 verification gate.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

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


# SIGNAL-CLOSEDBAR-SHADOW-W1 CH6 — the two lazy-dispatch tests below used to read the LIVE
# wall clock (`datetime.now()` / `time.time()`). Under the old relative-age contract that was
# harmless: only the ELAPSED gap mattered. Due-ness is now BUCKET-based, so whether a gap
# crosses a bucket boundary depends on the clock's PHASE — a live clock made these tests
# flaky by construction rather than merely wrong. They now pin a fixed epoch and zero the
# jitter, so the assertion is deterministic and still says what it always said: short
# timeframes dispatch, long ones do not.
#
# `_LAZY_NOW` is chosen so that for every timeframe below, the shifted instants for `now` and
# `now - gap` land in the SAME bucket for the long TFs and DIFFERENT buckets for the short
# ones. With jitter forced to 0, shift(tf) = TF_SECONDS[tf] * 75 // 100 + 60.
_LAZY_BASE = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())  # ≡ 0 mod 1d


def _pin_no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Window 1 ⇒ `jitter_minutes` returns 0 for every row, so the phase math is exact."""
    monkeypatch.setenv("ALGOVAULT_BOT_JITTER_WINDOW_MIN", "1")
    monkeypatch.delenv("ALGOVAULT_BOT_DISPATCH_OFFSET_PCT", raising=False)
    monkeypatch.delenv("ALGOVAULT_BOT_CLOSE_GRACE_MIN", raising=False)


def _seed(tmp_db: Database, at_epoch: int) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    for tf in ("1m", "5m", "15m", "1h", "4h"):
        tmp_db.add_watch(1, "BTC", tf, "BINANCE", "both")
    stamp = datetime.fromtimestamp(at_epoch, tz=timezone.utc).replace(tzinfo=None).isoformat()
    with tmp_db._cursor() as cur:
        cur.execute("UPDATE watchlists SET last_fetched_at = ?", (stamp,))


def test_lazy_dispatch_only_1m_due_at_61s_after_fetch(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_no_jitter(monkeypatch)
    now = _LAZY_BASE + 120
    _seed(tmp_db, now - 61)

    due = tmp_db.list_due_watches(now, TF_SECONDS)
    due_tfs = sorted({r["timeframe"] for r in due})
    # Only the 1m bucket advanced across a 61s gap; every longer TF is still in its bucket.
    assert due_tfs == ["1m"], f"expected only 1m due, got {due_tfs}"


def test_lazy_dispatch_1m_5m_due_at_301s(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_no_jitter(monkeypatch)
    now = _LAZY_BASE + 300
    _seed(tmp_db, now - 301)

    due = tmp_db.list_due_watches(now, TF_SECONDS)
    due_tfs = sorted({r["timeframe"] for r in due})
    # A 301s gap always crosses a 60s and a 300s boundary; 15m+ stay put at this phase.
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
    assert "📊 Quota: 47/100 free alerts used" in msg


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
