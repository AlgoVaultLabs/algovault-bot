"""CTA injection tests.

Soft 75% / urgent 90% trade-call CTAs and the regime-frequency CTA were
removed 2026-05-08 (operator: too distracting). Only the 100%-exhausted
trade-call notice remains — that's the user's "you've hit the cap" heads-up.
"""

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


# ── trade-call CTA — only the 100%-exhausted branch returns text ───────


def test_trade_call_cta_no_cta_below_75() -> None:
    for used in (0, 50, 74):
        assert trade_call_cta_text(_state(used)) == "", f"used={used}"


def test_trade_call_cta_no_cta_at_75pct() -> None:
    # Soft nudge removed 2026-05-08.
    assert trade_call_cta_text(_state(75)) == ""


def test_trade_call_cta_no_cta_at_89pct() -> None:
    assert trade_call_cta_text(_state(89)) == ""


def test_trade_call_cta_no_cta_at_90pct() -> None:
    # Urgent nudge removed 2026-05-08.
    assert trade_call_cta_text(_state(90)) == ""


def test_trade_call_cta_no_cta_at_99pct() -> None:
    assert trade_call_cta_text(_state(99)) == ""


def test_trade_call_cta_quota_100_at_exhausted() -> None:
    # 100%-exhausted notice retained — essential UX, not a marketing nudge.
    cta = trade_call_cta_text(_state(100))
    assert "utm_campaign=quota_100" in cta
    assert "x402" in cta


def test_trade_call_cta_quota_100_above_cap() -> None:
    cta = trade_call_cta_text(_state(105))
    assert "utm_campaign=quota_100" in cta


# ── regime alert CTA — disabled (always returns False) ─────────────────


def test_regime_cta_never_fires() -> None:
    # Regime CTA disabled 2026-05-08; previously fired on #1, 3, 7, 15, then every 10.
    for n in (1, 2, 3, 5, 7, 10, 15, 20, 25, 35, 45, 55, 105, 1000):
        assert regime_alert_should_show_cta(n) is False, f"n={n}"


def test_regime_cta_text_still_renders_when_called() -> None:
    # Helper preserved so the feature can be re-enabled with one line.
    msg = regime_cta_text()
    assert "api.algovault.com/signup" in msg
    assert "utm_campaign=regime_alert" in msg
    assert "BUY/SELL" in msg
