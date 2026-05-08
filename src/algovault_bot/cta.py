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

from .messages import signup_url
from .quota import FREE_TIER_MONTHLY_QUOTA, QuotaState


THROTTLE_WINDOW: Final = timedelta(hours=24)


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
    if state.used >= state.total:
        return "100"
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
        remaining = max(0, state.total - state.used)
        return (
            f"🔥 Only {remaining} free calls left. Upgrade now to keep getting calls:\n"
            f"→ {signup_url('quota_90')}"
        )

    if threshold == "75":
        if _within_throttle(state.quota_75_last_fired_at, now):
            return ""
        return (
            "⏰ You've used 75% of your free calls. "
            "Upgrade to Starter ($9.99 → 3,000 calls/mo):\n"
            f"→ {signup_url('quota_75')}"
        )

    return ""


def quota_exhausted_message() -> str:
    """Drop-in replacement for signal-MCP's getQuotaExhaustedMessage when the
    bot detects 100% usage locally (D1-C: signal-MCP doesn't tick bot quota,
    bot owns the gate). Mirrors the upstream message shape."""
    return (
        f"Free tier limit reached ({FREE_TIER_MONTHLY_QUOTA}/{FREE_TIER_MONTHLY_QUOTA} "
        "calls this month). Upgrade to Starter ($9.99/mo) for 3,000 calls/mo, "
        "or pay per call via x402."
    )
