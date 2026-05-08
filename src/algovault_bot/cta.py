"""CTA injection — quota-threshold for trade calls + frequency-driven for regime alerts.

Spec C4 line 344-368 + D3-A canonical signup URL.

Trade-call alert thresholds (quota usage %):
- 0–74%      : no CTA
- 75–89%     : soft nudge (utm_campaign=quota_75)
- 90–99%     : urgent (utm_campaign=quota_90)
- 100%       : exhausted (utm_campaign=quota_100) + x402 fallback line

Regime alert frequency: append CTA on alert #1, 3, 7, 15, then every 10
(utm_campaign=regime_alert).
"""

from __future__ import annotations

from typing import Final

from .messages import signup_url
from .quota import FREE_TIER_MONTHLY_QUOTA, QuotaState


# Sequence: 1, 3, 7, 15, then every 10 starting at 25.
# Membership check: n in {1, 3, 7, 15} OR n >= 25 AND n % 10 == 5 (matches 25, 35, 45, ...).
_REGIME_CTA_FIXED: Final[frozenset[int]] = frozenset({1, 3, 7, 15})


def regime_alert_should_show_cta(total_regime_alerts: int) -> bool:
    """Returns True if the Nth regime alert should append the soft CTA.

    N is the count AFTER incrementing for the alert about to be sent.
    """
    if total_regime_alerts in _REGIME_CTA_FIXED:
        return True
    if total_regime_alerts >= 25 and (total_regime_alerts - 15) % 10 == 0:
        return True
    return False


def regime_cta_text() -> str:
    return (
        "📈 Want directional BUY/SELL calls (not just regime shifts)?\n"
        f"→ {signup_url('regime_alert')}"
    )


def trade_call_cta_text(state: QuotaState) -> str:
    """Returns the CTA snippet for a trade-call alert based on quota %, or ''.

    The caller decides whether to pre-pend a separator newline.

    BOT-W2 C3: paid-tier-linked users get NO upgrade CTA — they're already
    paying. Suppress at the source rather than relying on copywriting.
    """
    if state.is_paid:
        return ""
    if state.total <= 0:
        return ""
    pct = state.used / state.total
    if state.used >= state.total:
        # 100% exhausted (state.exhausted)
        return (
            f"→ {signup_url('quota_100')}\n"
            "\n"
            "Or pay per call via x402 (no signup) — see x402.org"
        )
    if pct >= 0.90:
        remaining = max(0, state.total - state.used)
        return (
            f"🔥 Only {remaining} free calls left. Upgrade now to keep getting calls:\n"
            f"→ {signup_url('quota_90')}"
        )
    if pct >= 0.75:
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
