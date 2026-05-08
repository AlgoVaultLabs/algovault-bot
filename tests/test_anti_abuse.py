"""C5 — anti-abuse rate-limit + quota-burn tests.

Covers AC5.1 / AC5.2 / AC5.3 / AC5.4 algorithmically. Live "21 alerts in 1 hr"
stress-tests are exercised against the real bot service via journalctl in the
C5 verification gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from algovault_bot.db import Database
from algovault_bot.rate_limit import (
    CALLS_24H_CAP,
    QUOTA_BURN_24H_CAP,
    REGIME_24H_CAP,
    get_rate_limit_state,
    increment_calls_count,
    increment_regime_count,
    trip_burn_suppression,
)


def test_initial_state_zero(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    rl = get_rate_limit_state(tmp_db, 1)
    assert rl.regime_count == 0
    assert rl.calls_count == 0
    assert not rl.regime_capped()
    assert not rl.calls_capped()
    assert not rl.quota_burn_capped()
    assert not rl.burn_suppression_active()


# AC5.1
def test_regime_cap_at_20(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    for _ in range(REGIME_24H_CAP):
        rl = increment_regime_count(tmp_db, 1)
    assert rl.regime_count == REGIME_24H_CAP
    assert rl.regime_capped()


# AC5.2
def test_calls_cap_at_30(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    for _ in range(CALLS_24H_CAP):
        rl = increment_calls_count(tmp_db, 1)
    assert rl.calls_count == CALLS_24H_CAP
    assert rl.calls_capped()


# AC5.4 — quota-burn at 50, suppression trips
def test_quota_burn_50_trips_suppression(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    for _ in range(QUOTA_BURN_24H_CAP):
        rl = increment_calls_count(tmp_db, 1)
    assert rl.quota_burn_capped()
    until = trip_burn_suppression(tmp_db, 1)
    assert until > datetime.now(timezone.utc)
    rl_after = get_rate_limit_state(tmp_db, 1)
    assert rl_after.burn_suppression_active()


def test_window_resets_after_24h(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    increment_regime_count(tmp_db, 1)
    increment_calls_count(tmp_db, 1)
    increment_calls_count(tmp_db, 1)
    # Force window_start to 25h ago.
    past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alerts_24h_window_start = ? WHERE chat_id = 1",
            (past,),
        )
    rl = get_rate_limit_state(tmp_db, 1)
    # get_rate_limit_state should reset the stale window.
    assert rl.regime_count == 0
    assert rl.calls_count == 0
    assert rl.window_start is None


def test_burn_suppression_expires_when_until_in_past(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET calls_burn_suppressed_until = ? WHERE chat_id = 1",
            (past,),
        )
    rl = get_rate_limit_state(tmp_db, 1)
    assert not rl.burn_suppression_active()


# Telegram global semaphore (smoke — just confirm it's an asyncio primitive of size 25)
def test_telegram_global_semaphore_size_25() -> None:
    from algovault_bot.rate_limit import TELEGRAM_GLOBAL_SEMAPHORE
    # asyncio.Semaphore exposes ._value (CPython internal) — bound check.
    assert TELEGRAM_GLOBAL_SEMAPHORE._value == 25


# C5 — digest renderer end-to-end
def test_digest_renders_with_zero_state(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest
    text = render_digest(tmp_db)
    assert "algovault-bot — daily digest" in text
    assert "Subscribers: 0" in text
    assert "📊 Regime: 0" in text


def test_digest_aggregates_sample_data(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.upsert_subscriber(2, "u2", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "both")
    increment_regime_count(tmp_db, 1)
    increment_regime_count(tmp_db, 1)
    increment_calls_count(tmp_db, 2)
    tmp_db.increment_total_regime_alerts(1)
    tmp_db.increment_total_regime_alerts(1)
    tmp_db.increment_total_call_alerts(2)
    text = render_digest(tmp_db)
    assert "Subscribers: 2" in text
    assert "📊 Regime: 2" in text
    assert "📈 Calls: 1" in text
