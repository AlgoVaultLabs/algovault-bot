"""End-to-end quota threshold + alert-format integration tests (C4)."""

from __future__ import annotations

from datetime import datetime, timezone

from algovault_bot.alert_engine import (
    WatchRow,
    format_quota_exhausted_alert,
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
    assert "📊 Quota: 47/100 free calls used this month" in msg
    assert "utm_campaign=quota_75" not in msg
    assert "utm_campaign=quota_90" not in msg
    assert "utm_campaign=quota_100" not in msg


# AC4.2
def test_alert_at_80_quota_75_cta() -> None:
    quota = _state(80)
    cta = trade_call_cta_text(quota) or None
    msg = format_trade_call_alert(
        _row(), "BUY", 78, 84250.50, "TRENDING_UP", "NORMAL", None, quota, cta=cta,
    )
    assert "📊 Quota: 80/100 free calls used this month" in msg
    assert "utm_campaign=quota_75" in msg


# AC4.3
def test_alert_at_95_quota_90_cta() -> None:
    quota = _state(95)
    cta = trade_call_cta_text(quota) or None
    msg = format_trade_call_alert(
        _row(), "SELL", 80, 84250.50, "TRENDING_DOWN", "ELEVATED", None, quota, cta=cta,
    )
    assert "📊 Quota: 95/100 free calls used this month" in msg
    assert "Only 5 free calls left" in msg
    assert "utm_campaign=quota_90" in msg


# AC4.4
def test_alert_at_100_exhausted_with_x402_fallback() -> None:
    quota = _state(100)
    cta = trade_call_cta_text(quota)
    msg = format_quota_exhausted_alert(_row(), "BUY", cta)
    assert "Free tier limit reached" in msg
    assert "100/100" in msg
    assert "utm_campaign=quota_100" in msg
    assert "x402" in msg


# AC4.5 — regime alert frequency: #1=CTA, #2=none, #3=CTA, #4-6=none, #7=CTA
def test_regime_alert_5_includes_cta_at_1() -> None:
    msg = format_regime_alert(_row(), "TRENDING_UP", "RANGING", 76, cta=regime_cta_text())
    assert "utm_campaign=regime_alert" in msg


def test_regime_alert_no_cta_at_2() -> None:
    msg = format_regime_alert(_row(), "RANGING", "TRENDING_UP", 76, cta=None)
    assert "utm_campaign" not in msg
