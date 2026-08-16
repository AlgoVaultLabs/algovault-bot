"""End-to-end quota threshold + alert-format integration tests (C4)."""

from __future__ import annotations

from datetime import datetime, timezone

from algovault_bot.alert_engine import (
    WatchRow,
    format_regime_alert,
    format_trade_call_alert,
)
from algovault_bot.cta import regime_cta_text, trade_call_cta_text
from algovault_bot.quota import QuotaState


def _row(alert_type: str = "both") -> WatchRow:
    return WatchRow(
        chat_id=1, coin="BTC", timeframe="4h", exchange="BINANCE",
        alert_type=alert_type, regime_last_seen=None,
        last_verdict=None, last_verdict_streak=0,
    )


def _state(used: int) -> QuotaState:
    return QuotaState(used, 100, datetime.now(timezone.utc), used / 100)


# AC4.1
def test_alert_at_47_no_cta() -> None:
    quota = _state(47)
    cta = trade_call_cta_text(quota) or None
    msg = format_trade_call_alert(
        _row(), "BUY", 78, 84250.50, "TRENDING_UP", "NORMAL", "trend up", quota, cta=cta,
    )
    assert "📊 Quota: 47/100 free alerts used" in msg
    assert "utm_campaign=quota_75" not in msg
    assert "utm_campaign=quota_90" not in msg
    assert "utm_campaign=quota_100" not in msg


# AC4.2 — soft 75% nudge fires once per 24h per user.
def test_alert_at_80_quota_75_cta() -> None:
    quota = _state(80)  # last_75/last_90 default None → first fire
    cta = trade_call_cta_text(quota) or None
    msg = format_trade_call_alert(
        _row(), "BUY", 78, 84250.50, "TRENDING_UP", "NORMAL", None, quota, cta=cta,
    )
    assert "📊 Quota: 80/100 free alerts used" in msg
    assert "utm_campaign=quota_75" in msg


# AC4.3 — urgent 90% nudge fires once per 24h per user.
def test_alert_at_95_quota_90_cta() -> None:
    quota = _state(95)
    cta = trade_call_cta_text(quota) or None
    msg = format_trade_call_alert(
        _row(), "SELL", 80, 84250.50, "TRENDING_DOWN", "ELEVATED", None, quota, cta=cta,
    )
    assert "📊 Quota: 95/100 free alerts used" in msg
    assert "Only 5 free alerts left" in msg
    assert "utm_campaign=quota_90" in msg


# AC4.4 — BOT-QUOTA-REFUSAL-SEAM-W1 retired `format_quota_exhausted_alert`: it was
# the walled-user body for the ONE lane that had one, which is how three lanes ended
# up with three behaviours. The body is now `quota.build_refusal_text`, shared by
# every push lane. The assertions below hold the same contract against the new body.
def test_walled_body_states_the_wall_and_keeps_the_x402_fallback(tmp_path) -> None:
    from algovault_bot.db import Database
    from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA, build_refusal_text, get_quota_state

    db = Database(str(tmp_path / "t.db"))
    db.upsert_subscriber(1, "u", "en")
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=?",
            (FREE_TIER_MONTHLY_QUOTA, datetime.now(timezone.utc).isoformat(), 1),
        )
    msg = build_refusal_text(db, 1, get_quota_state(db, 1))
    assert "100/100" in msg
    assert "alerts" in msg, "the BOT's unit, not the API's 'calls'"
    assert "x402" in msg, "the pay-per-call rail must survive at the wall"
    assert "utm_campaign=quota_exhausted_push" in msg
    assert "Resets" in msg and "30 days" not in msg, (
        "must name the real rolling-window date, not a calendar-month horizon"
    )


# AC4.5 — regime alert frequency: #1=CTA, #2=none, #3=CTA, #4-6=none, #7=CTA
def test_regime_alert_5_includes_cta_at_1() -> None:
    msg = format_regime_alert(_row(), "TRENDING_UP", "RANGING", 76, cta=regime_cta_text())
    assert "utm_campaign=regime_alert" in msg


def test_regime_alert_no_cta_at_2() -> None:
    msg = format_regime_alert(_row(), "RANGING", "TRENDING_UP", 76, cta=None)
    assert "utm_campaign" not in msg
