"""C4 — quota-threshold CTA + regime-frequency CTA tests."""

from __future__ import annotations

from datetime import datetime, timezone

from algovault_bot.cta import (
    regime_alert_should_show_cta,
    regime_cta_text,
    trade_call_cta_text,
)
from algovault_bot.quota import QuotaState


def _state(used: int, total: int = 100) -> QuotaState:
    return QuotaState(used, total, datetime.now(timezone.utc), used / total if total else 0.0)


# ── trade-call CTA — quota threshold branching ─────────────────


def test_trade_call_cta_no_cta_below_75() -> None:
    for used in (0, 50, 74):
        assert trade_call_cta_text(_state(used)) == "", f"used={used}"


def test_trade_call_cta_quota_75_at_75pct() -> None:
    cta = trade_call_cta_text(_state(75))
    assert "75%" in cta
    assert "Starter ($9.99 → 3,000 calls/mo)" in cta
    assert "utm_campaign=quota_75" in cta
    assert "api.algovault.com/signup" in cta


def test_trade_call_cta_quota_75_at_89pct() -> None:
    cta = trade_call_cta_text(_state(89))
    assert "utm_campaign=quota_75" in cta
    assert "utm_campaign=quota_90" not in cta


def test_trade_call_cta_quota_90_at_90pct() -> None:
    cta = trade_call_cta_text(_state(90))
    assert "Only 10 free calls left" in cta
    assert "utm_campaign=quota_90" in cta


def test_trade_call_cta_quota_90_at_99pct() -> None:
    cta = trade_call_cta_text(_state(99))
    assert "Only 1 free calls left" in cta
    assert "utm_campaign=quota_90" in cta


def test_trade_call_cta_quota_100_at_exhausted() -> None:
    cta = trade_call_cta_text(_state(100))
    assert "utm_campaign=quota_100" in cta
    assert "x402" in cta  # x402 fallback line


def test_trade_call_cta_quota_100_above_cap() -> None:
    # If somehow the counter overshot, still treat as exhausted.
    cta = trade_call_cta_text(_state(105))
    assert "utm_campaign=quota_100" in cta


# ── regime alert CTA — frequency-driven (#1, 3, 7, 15, then every 10) ────


def test_regime_cta_fires_on_alert_1() -> None:
    assert regime_alert_should_show_cta(1) is True


def test_regime_cta_skips_2() -> None:
    assert regime_alert_should_show_cta(2) is False


def test_regime_cta_fires_on_3() -> None:
    assert regime_alert_should_show_cta(3) is True


def test_regime_cta_skips_4_5_6() -> None:
    for n in (4, 5, 6):
        assert regime_alert_should_show_cta(n) is False, f"n={n}"


def test_regime_cta_fires_on_7() -> None:
    assert regime_alert_should_show_cta(7) is True


def test_regime_cta_skips_8_through_14() -> None:
    for n in range(8, 15):
        assert regime_alert_should_show_cta(n) is False, f"n={n}"


def test_regime_cta_fires_on_15() -> None:
    assert regime_alert_should_show_cta(15) is True


def test_regime_cta_skips_16_through_24() -> None:
    for n in range(16, 25):
        assert regime_alert_should_show_cta(n) is False, f"n={n}"


def test_regime_cta_fires_on_25_35_45_etc() -> None:
    # "then every 10" starting from 25
    for n in (25, 35, 45, 55, 105):
        assert regime_alert_should_show_cta(n) is True, f"n={n}"


def test_regime_cta_skips_26_through_34() -> None:
    for n in range(26, 35):
        assert regime_alert_should_show_cta(n) is False, f"n={n}"


def test_regime_cta_text_includes_signup_url_and_campaign() -> None:
    msg = regime_cta_text()
    assert "api.algovault.com/signup" in msg
    assert "utm_campaign=regime_alert" in msg
    assert "BUY/SELL" in msg
