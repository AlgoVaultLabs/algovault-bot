"""BOT-W2 C3 — tier-aware quota gate tests.

Paid-tier-linked users:
- ``QuotaState.is_paid`` is True; ``exhausted`` is always False; ``remaining``
  is effectively unlimited.
- ``consume_quota`` is a no-op (counter doesn't tick).
- ``trade_call_cta_text`` returns '' (CTAs suppressed; user is already paying).
- ``format_trade_call_alert`` renders a tier badge instead of "X/100".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from algovault_bot.alert_engine import WatchRow, format_trade_call_alert
from algovault_bot.cta import trade_call_cta_text
from algovault_bot.db import Database
from algovault_bot.quota import (
    PAID_TIERS,
    FREE_TIER_MONTHLY_QUOTA,
    QuotaState,
    consume_quota,
    get_quota_state,
)


def _row() -> WatchRow:
    return WatchRow(
        chat_id=1, coin="BTC", timeframe="4h", exchange="BINANCE",
        alert_type="both", regime_last_seen=None,
        last_verdict=None, last_verdict_streak=0,
    )


# ── QuotaState helpers ─────────────────────────────────────────


@pytest.mark.parametrize("tier", ["starter", "pro", "enterprise", "x402"])
def test_paid_state_never_exhausted(tier: str) -> None:
    s = QuotaState(used=999, total=100, window_start=None, pct_used=9.99, linked_tier=tier)
    assert s.is_paid
    assert s.exhausted is False
    assert s.remaining > FREE_TIER_MONTHLY_QUOTA


@pytest.mark.parametrize("tier", [None, "free"])
def test_unlinked_or_free_state_uses_normal_quota(tier: str | None) -> None:
    s = QuotaState(used=100, total=100, window_start=None, pct_used=1.0, linked_tier=tier)
    assert s.is_paid is False
    assert s.exhausted is True
    assert s.remaining == 0


def test_paid_tiers_constant_matches_signal_mcp_license_tiers() -> None:
    # Mirror src/types.ts LicenseTier (minus 'free' + 'internal')
    assert PAID_TIERS == frozenset({"starter", "pro", "enterprise", "x402"})


# ── get_quota_state reads linked_tier from db ──────────────────


def test_quota_state_unlinked_chat(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    s = get_quota_state(tmp_db, 1)
    assert s.linked_tier is None
    assert s.is_paid is False
    assert s.total == FREE_TIER_MONTHLY_QUOTA


def test_quota_state_linked_starter(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.link_subscriber(1, "av_live_test", "starter")
    s = get_quota_state(tmp_db, 1)
    assert s.linked_tier == "starter"
    assert s.is_paid is True
    assert s.exhausted is False


def test_quota_state_linked_pro(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.link_subscriber(1, "av_live_test", "pro")
    s = get_quota_state(tmp_db, 1)
    assert s.linked_tier == "pro"
    assert s.is_paid is True


# ── consume_quota is no-op for paid ────────────────────────────


def test_consume_quota_paid_tier_is_noop(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.link_subscriber(1, "av_live_test", "pro")
    s_before = get_quota_state(tmp_db, 1)
    s_after = consume_quota(tmp_db, 1)
    assert s_before.used == s_after.used == 0
    # DB row should also show used = 0 (no UPDATE happened)
    row = tmp_db.get_subscriber(1)
    assert int(row["alert_count"]) == 0


def test_consume_quota_free_tier_still_ticks(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    s_after = consume_quota(tmp_db, 1)
    assert s_after.used == 1
    assert s_after.linked_tier is None


# ── CTA suppression for paid ───────────────────────────────────


@pytest.mark.parametrize("tier", ["starter", "pro", "enterprise"])
def test_paid_tier_no_cta_at_any_quota(tier: str) -> None:
    # Even if the (irrelevant) free counter is at 999, paid users see no CTA.
    s = QuotaState(used=999, total=100, window_start=None, pct_used=9.99, linked_tier=tier)
    assert trade_call_cta_text(s) == ""


def test_free_tier_cta_at_75_first_fire() -> None:
    # First-fire (no last_75 timestamp) — soft nudge appears.
    s = QuotaState(used=80, total=100, window_start=None, pct_used=0.8, linked_tier=None)
    cta = trade_call_cta_text(s)
    assert "utm_campaign=quota_75" in cta


def test_free_tier_exhausted_cta_still_fires() -> None:
    s = QuotaState(used=100, total=100, window_start=None, pct_used=1.0, linked_tier=None)
    cta = trade_call_cta_text(s)
    assert "utm_campaign=quota_100" in cta
    assert "x402" in cta


# ── alert format renders tier badge for paid ───────────────────


def test_alert_format_paid_shows_tier_badge() -> None:
    s = QuotaState(used=42, total=100, window_start=None, pct_used=0.42, linked_tier="pro")
    msg = format_trade_call_alert(
        _row(), "BUY", 78, 84250.50, "TRENDING_UP", "NORMAL", "trend up", s, cta=None,
    )
    assert "💎 Pro plan — unlimited via bot" in msg
    assert "Quota:" not in msg


def test_alert_format_free_shows_quota_line() -> None:
    s = QuotaState(used=47, total=100, window_start=None, pct_used=0.47, linked_tier=None)
    msg = format_trade_call_alert(
        _row(), "BUY", 78, 84250.50, "TRENDING_UP", "NORMAL", None, s, cta=None,
    )
    assert "📊 Quota: 47/100 free calls used this month" in msg
    assert "💎" not in msg
