"""CTA injection — quota-threshold for trade calls; regime-frequency disabled.

History:
- BOT-W1 C4: introduced soft 75% / urgent 90% / exhausted 100% trade-call CTAs
  + regime-alert frequency CTA (#1, 3, 7, 15, then every 10).
- BOT-W2 C3: paid-tier-linked users get NO CTA (they're already paying).
- BOT-ALERT-CLEANUP-W1 (2026-05-08): regime-frequency CTA disabled (operator
  feedback: too distracting). Soft/urgent trade-call CTAs preserved but now
  throttled to once-per-24h-per-threshold so a user who lingers in the 75-89%
  band for a week sees the soft nudge once, not on every alert. Threshold
  state lives in ``subscribers.quota_{75,90}_last_fired_at``; alert_engine
  writes via ``db.mark_quota_cta_fired`` after a successful Telegram push.

Trade-call alert behavior:
- 0–74%   : no CTA
- 75–89%  : soft nudge (utm_campaign=quota_75) — at most once per 24h
- 90–99%  : urgent nudge (utm_campaign=quota_90) — at most once per 24h
- 100%    : exhausted notice (utm_campaign=quota_100) + x402 fallback line —
            no throttle (it's the user's "you've hit the cap" heads-up).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

from .messages import _usd, signup_url
from .quota import (
    FREE_TIER_MONTHLY_QUOTA,
    STARTER_MONTHLY_CALLS,
    STARTER_PRICE_USD,
    QuotaState,
)
from .referral import format_referral_nudge


THROTTLE_WINDOW: Final = timedelta(hours=24)
# TG-REFERRAL-W1 (C3): the value-moment referral nudge fires at most once per 7d.
REFERRAL_NUDGE_THROTTLE: Final = timedelta(days=7)


def regime_alert_should_show_cta(total_regime_alerts: int) -> bool:
    """Regime-alert CTA disabled. Always returns False.

    Previously fired on alerts #1, 3, 7, 15, then every 10. Re-enable by
    restoring the prior sequence logic if A/B data argues for it.
    """
    return False


def regime_cta_text() -> str:
    return (
        "📈 Want directional BUY/SELL calls (not just regime shifts)?\n"
        f"→ {signup_url('regime_alert')}"
    )


def quota_threshold(state: QuotaState) -> str | None:
    """Returns the trade-call quota bucket for this state: '75', '90', '100', or None.

    None means the user is below 75% used, paid, or has no quota allocation.
    Pure function of state — does NOT consult time / last-fired timestamps.
    """
    if state.is_paid or state.total <= 0:
        return None
    # TG-REFERRAL-W1: bonus-aware — only flag "100" when truly out (monthly + the
    # referee bonus pool), and suppress the 75/90 upgrade nudges while bonus calls
    # remain (a referee with bonus isn't an upsell moment).
    if state.remaining <= 0:
        return "100"
    if state.referral_bonus_remaining > 0:
        return None
    pct = state.used / state.total
    if pct >= 0.90:
        return "90"
    if pct >= 0.75:
        return "75"
    return None


def _within_throttle(last_at: datetime | None, now: datetime) -> bool:
    if last_at is None:
        return False
    return (now - last_at) < THROTTLE_WINDOW


def trade_call_cta_text(state: QuotaState, *, now: datetime | None = None) -> str:
    """Returns the CTA snippet for a trade-call alert, or ''.

    Soft 75% and urgent 90% nudges are throttled to at most once per 24h per
    threshold per user (BOT-ALERT-CLEANUP-W1). The 100%-exhausted notice is
    not throttled — it's the user-facing cap-reached heads-up, not a
    marketing nudge. Paid-tier-linked users always get '' (BOT-W2 C3).

    ``state.quota_{75,90}_last_fired_at`` are populated by ``get_quota_state``
    from the ``subscribers`` table. Pass ``now`` for deterministic tests; it
    defaults to ``datetime.now(timezone.utc)``.
    """
    threshold = quota_threshold(state)
    if threshold is None:
        return ""
    if now is None:
        now = datetime.now(timezone.utc)

    if threshold == "100":
        # Always render — essential UX, not a marketing nudge.
        return (
            f"→ {signup_url('quota_100')}\n"
            "\n"
            "Or pay per call via x402 (no signup) — see x402.org"
        )

    if threshold == "90":
        if _within_throttle(state.quota_90_last_fired_at, now):
            return ""
        remaining = state.remaining
        return (
            f"🔥 Only {remaining} free alerts left. Upgrade now to keep getting them:\n"
            f"→ {signup_url('quota_90')}"
        )

    if threshold == "75":
        if _within_throttle(state.quota_75_last_fired_at, now):
            return ""
        return (
            "⏰ You've used 75% of your free alerts. "
            f"Upgrade to Starter ({_usd(state.starter_price_usd)}/mo or $39.90/6mo "
            f"→ {state.starter_monthly_calls:,} API calls/mo):\n"
            f"→ {signup_url('quota_75')}"
        )
    return ""


def _referral_nudge_due(last_at: datetime | None, now: datetime) -> bool:
    return last_at is None or (now - last_at) >= REFERRAL_NUDGE_THROTTLE


def referral_nudge_text(state: QuotaState, *, now: datetime | None = None) -> str:
    """TG-REFERRAL-W1 / C3 — value-moment referral nudge for a trade-call alert.

    Fires ONLY when there is no quota CTA to show (an active free user who isn't
    low on quota), the user holds no referee bonus (they already know referral),
    and the per-user 7d throttle allows. Returns '' otherwise. Paid users never get
    it (the BOT-W2 '' contract). alert_engine stamps the throttle when it's shown;
    qualitative copy (no program numbers — /referral shows the live SoT terms)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if state.is_paid:
        return ""
    if quota_threshold(state) is not None:
        return ""  # a quota CTA owns this slot — never stack two CTAs
    if state.referral_bonus_remaining > 0:
        return ""
    if not _referral_nudge_due(state.referral_nudge_last_at, now):
        return ""
    return format_referral_nudge("en")  # alert bodies/CTAs are English

    return ""


def quota_exhausted_message(state: QuotaState | None = None) -> str:
    """Drop-in replacement for signal-MCP's getQuotaExhaustedMessage when the
    bot detects 100% usage locally (D1-C: signal-MCP doesn't tick bot quota,
    bot owns the gate). Mirrors the upstream message shape.

    GROWTH-TG-QUOTA-PARITY-W1 CH3b-2: every figure now derives. `state` is optional so existing
    callers keep working; absent it, the pinned fallbacks are used — which SERVE, as everywhere
    else in this lane. `$39.90/6mo` stays hand-typed by ruling: `/api/plans/public` carries no
    prepay field, and widening a shipped public contract for one string is
    `OPS-PLANS-PUBLIC-PREPAY-FIELD-W1`, not this wave. The figure is currently correct.
    """
    total = state.total if state is not None else FREE_TIER_MONTHLY_QUOTA
    price = state.starter_price_usd if state is not None else STARTER_PRICE_USD
    calls = state.starter_monthly_calls if state is not None else STARTER_MONTHLY_CALLS
    return (
        f"Free tier limit reached ({total}/{total} "
        f"alerts this month). Upgrade to Starter ({_usd(price)}/mo or $39.90/6mo) for "
        f"{calls:,} API calls/mo, or pay per call via x402."
    )
