"""CTA injection tests.

History:
- BOT-W1 C4: regime-frequency CTA + 75/90/100% trade-call CTAs.
- BOT-ALERT-CLEANUP-W1 (2026-05-08): regime CTA disabled; 75/90 CTAs throttled
  to once-per-24h-per-threshold; 100% notice unchanged (essential UX).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from algovault_bot.cta import (
    quota_threshold,
    regime_alert_should_show_cta,
    regime_cta_text,
    trade_call_cta_text,
)
from algovault_bot.quota import QuotaState


def _state(
    used: int,
    total: int = 100,
    *,
    last_75: datetime | None = None,
    last_90: datetime | None = None,
) -> QuotaState:
    return QuotaState(
        used,
        total,
        datetime.now(timezone.utc),
        used / total if total else 0.0,
        quota_75_last_fired_at=last_75,
        quota_90_last_fired_at=last_90,
    )


_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


# ── trade-call CTA — quota threshold branching ─────────────────


def test_trade_call_cta_no_cta_below_75() -> None:
    for used in (0, 50, 74):
        assert trade_call_cta_text(_state(used), now=_NOW) == "", f"used={used}"


def test_trade_call_cta_quota_75_at_75pct_first_fire() -> None:
    cta = trade_call_cta_text(_state(75), now=_NOW)
    assert "75%" in cta
    assert "Starter ($9.99 → 3,000 calls/mo)" in cta
    assert "utm_campaign=quota_75" in cta


def test_trade_call_cta_quota_75_at_89pct_first_fire() -> None:
    cta = trade_call_cta_text(_state(89), now=_NOW)
    assert "utm_campaign=quota_75" in cta
    assert "utm_campaign=quota_90" not in cta


def test_trade_call_cta_quota_90_at_90pct_first_fire() -> None:
    cta = trade_call_cta_text(_state(90), now=_NOW)
    assert "Only 10 free calls left" in cta
    assert "utm_campaign=quota_90" in cta


def test_trade_call_cta_quota_90_at_99pct_first_fire() -> None:
    cta = trade_call_cta_text(_state(99), now=_NOW)
    assert "Only 1 free calls left" in cta
    assert "utm_campaign=quota_90" in cta


def test_trade_call_cta_quota_100_at_exhausted() -> None:
    cta = trade_call_cta_text(_state(100), now=_NOW)
    assert "utm_campaign=quota_100" in cta
    assert "x402" in cta


def test_trade_call_cta_quota_100_above_cap() -> None:
    cta = trade_call_cta_text(_state(105), now=_NOW)
    assert "utm_campaign=quota_100" in cta


# ── 24h throttle (BOT-ALERT-CLEANUP-W1) ────────────────────────


def test_trade_call_cta_75_suppressed_within_24h() -> None:
    last = _NOW - timedelta(hours=12)
    cta = trade_call_cta_text(_state(80, last_75=last), now=_NOW)
    assert cta == ""


def test_trade_call_cta_75_re_fires_after_24h() -> None:
    last = _NOW - timedelta(hours=24, seconds=1)
    cta = trade_call_cta_text(_state(80, last_75=last), now=_NOW)
    assert "utm_campaign=quota_75" in cta


def test_trade_call_cta_90_suppressed_within_24h() -> None:
    last = _NOW - timedelta(hours=23, minutes=59)
    cta = trade_call_cta_text(_state(95, last_90=last), now=_NOW)
    assert cta == ""


def test_trade_call_cta_90_re_fires_after_24h() -> None:
    last = _NOW - timedelta(hours=25)
    cta = trade_call_cta_text(_state(95, last_90=last), now=_NOW)
    assert "utm_campaign=quota_90" in cta


def test_trade_call_cta_75_throttle_does_not_block_90_threshold() -> None:
    # User got the 75% nudge 5 hours ago, then crossed into 90% — the 90%
    # throttle is independent, so the urgent nudge fires.
    last_75 = _NOW - timedelta(hours=5)
    cta = trade_call_cta_text(_state(92, last_75=last_75), now=_NOW)
    assert "utm_campaign=quota_90" in cta


def test_trade_call_cta_100_not_throttled_by_75_or_90() -> None:
    last_75 = _NOW - timedelta(minutes=1)
    last_90 = _NOW - timedelta(minutes=1)
    cta = trade_call_cta_text(
        _state(100, last_75=last_75, last_90=last_90), now=_NOW
    )
    assert "utm_campaign=quota_100" in cta


# ── quota_threshold helper ─────────────────────────────────────


def test_quota_threshold_brackets() -> None:
    assert quota_threshold(_state(0)) is None
    assert quota_threshold(_state(74)) is None
    assert quota_threshold(_state(75)) == "75"
    assert quota_threshold(_state(89)) == "75"
    assert quota_threshold(_state(90)) == "90"
    assert quota_threshold(_state(99)) == "90"
    assert quota_threshold(_state(100)) == "100"
    assert quota_threshold(_state(150)) == "100"


def test_quota_threshold_returns_none_for_paid() -> None:
    s = QuotaState(used=50, total=100, window_start=None, pct_used=0.5, linked_tier="pro")
    assert quota_threshold(s) is None


# ── regime alert CTA — disabled (always returns False) ─────────


def test_regime_cta_never_fires() -> None:
    for n in (1, 2, 3, 5, 7, 10, 15, 20, 25, 35, 45, 55, 105, 1000):
        assert regime_alert_should_show_cta(n) is False, f"n={n}"


def test_regime_cta_text_still_renders_when_called() -> None:
    msg = regime_cta_text()
    assert "api.algovault.com/signup" in msg
    assert "utm_campaign=regime_alert" in msg
    assert "BUY/SELL" in msg
