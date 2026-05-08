"""CTA injection — quota-exhausted notice for trade calls only.

Soft/urgent quota-warning CTAs (75% / 90%) and the regime-alert frequency CTA
were removed 2026-05-08 (operator: too distracting in alert messages). The
helpers and call sites are preserved so the feature can be re-enabled with a
one-line change if conversion data later argues for it.

Trade-call alert thresholds (quota usage %):
- 0–99%      : no CTA
- 100%       : exhausted (utm_campaign=quota_100) + x402 fallback line — kept
               because it's the user's "you've hit the cap, here's why no
               more alerts" notice, not a marketing nudge.
"""

from __future__ import annotations

from .messages import signup_url
from .quota import FREE_TIER_MONTHLY_QUOTA, QuotaState


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


def trade_call_cta_text(state: QuotaState) -> str:
    """Returns the CTA snippet for a trade-call alert, or ''.

    Only the 100%-exhausted branch returns text — that one is essential UX
    (the user's "you've hit your free cap" heads-up). The 75% / 90% soft and
    urgent nudges return '' (suppressed 2026-05-08).

    BOT-W2 C3: paid-tier-linked users get NO upgrade CTA — they're already
    paying. Suppress at the source rather than relying on copywriting.
    """
    if state.is_paid:
        return ""
    if state.total <= 0:
        return ""
    if state.used >= state.total:
        # 100% exhausted (state.exhausted) — kept as user-facing cap notice.
        return (
            f"→ {signup_url('quota_100')}\n"
            "\n"
            "Or pay per call via x402 (no signup) — see x402.org"
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
