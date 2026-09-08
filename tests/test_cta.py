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


def test_trade_call_cta_returns_the_BUCKET_not_a_body() -> None:
    """V2 CH1 R3 — this function decides WHICH nudge is due; `notices` decides what it says.

    It used to return a rendered body carrying a pasted `signup_url(...)`, which is the class
    this wave retires. The copy assertions that lived here moved to `tests/test_notices.py`,
    where they run against the composer for all three languages; what stays here is the
    DECISION, which is what this module still owns.
    """
    assert trade_call_cta_text(_state(75), now=_NOW) == "75"
    assert trade_call_cta_text(_state(89), now=_NOW) == "75"
    assert trade_call_cta_text(_state(90), now=_NOW) == "90"
    assert trade_call_cta_text(_state(99), now=_NOW) == "90"


def test_trade_call_cta_is_silent_at_and_above_the_wall() -> None:
    """The "100" caption branch is DELETED and this is the assertion that keeps it deleted.

    It was unreachable on the push lane — `alert_engine` reaches this function only in the
    `else` of `if not decision.allowed`, and for a free user `remaining <= 0` is exactly
    `monthly_exhausted`, so `exhausted` is true and `allowed` is false: the WALL refuses before
    a caption can render. An exhausted state must therefore produce no caption at all, and the
    user hears about it through `notices.compose_wall` instead.

    `quota_threshold` still RETURNS "100" — `referral_nudge_text` guards on that value — so this
    pins the CAPTION's silence, not the bucket's disappearance.
    """
    assert trade_call_cta_text(_state(100), now=_NOW) == ""
    assert trade_call_cta_text(_state(105), now=_NOW) == ""
    assert quota_threshold(_state(100)) == "100", "the bucket survives for referral_nudge_text"


def test_trade_call_cta_75_suppressed_within_24h() -> None:
    last = _NOW - timedelta(hours=12)
    cta = trade_call_cta_text(_state(80, last_75=last), now=_NOW)
    assert cta == ""


def test_trade_call_cta_75_re_fires_after_24h() -> None:
    last = _NOW - timedelta(hours=24, seconds=1)
    cta = trade_call_cta_text(_state(80, last_75=last), now=_NOW)
    assert cta == "75"


def test_trade_call_cta_90_suppressed_within_24h() -> None:
    last = _NOW - timedelta(hours=23, minutes=59)
    cta = trade_call_cta_text(_state(95, last_90=last), now=_NOW)
    assert cta == ""


def test_trade_call_cta_90_re_fires_after_24h() -> None:
    last = _NOW - timedelta(hours=25)
    cta = trade_call_cta_text(_state(95, last_90=last), now=_NOW)
    assert cta == "90"


def test_trade_call_cta_75_throttle_does_not_block_90_threshold() -> None:
    # User got the 75% nudge 5 hours ago, then crossed into 90% — the 90%
    # throttle is independent, so the urgent nudge fires.
    last_75 = _NOW - timedelta(hours=5)
    cta = trade_call_cta_text(_state(92, last_75=last_75), now=_NOW)
    assert cta == "90"


def test_trade_call_cta_100_not_throttled_by_75_or_90() -> None:
    last_75 = _NOW - timedelta(minutes=1)
    last_90 = _NOW - timedelta(minutes=1)
    cta = trade_call_cta_text(
        _state(100, last_75=last_75, last_90=last_90), now=_NOW
    )
    assert cta == ""


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


# ── regime alert CTA — DELETED, and the absence is what is asserted now ─────────


def test_the_regime_cta_pair_is_gone() -> None:
    """V2 CH1 R3 deleted `regime_alert_should_show_cta` and `regime_cta_text` together.

    The predicate was a bare `return False` that read none of its arguments, so the copy behind
    it was unreachable for every input. This asserts the DELETION rather than the old behaviour:
    the previous two tests pinned "the gate always returns False" and "the text still renders
    when called", which together described dark copy and would have gone on passing forever.

    Asserting an absence is the only thing that can fail if someone re-adds a regime CTA without
    a wave — at which point it needs a picker keyboard, not a pasted URL.
    """
    import algovault_bot.cta as cta_module

    assert not hasattr(cta_module, "regime_alert_should_show_cta")
    assert not hasattr(cta_module, "regime_cta_text")
