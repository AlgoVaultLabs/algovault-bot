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
from typing import Final, Literal

# V2 CH1 R3 — `signup_url`, `_usd` and the four pinned ladder constants all left this module in
# the same edit, and their absence is the point: this file no longer writes copy or renders a
# figure. It owns the THROTTLE and the bucket; `notices.py` owns what the user reads.
from .quota import QuotaState
from .referral import format_referral_nudge


#: What `trade_call_cta_text` answers: WHICH caption nudge is due, or "" for none. It used to
#: return the rendered body; V2 CH1 R3 reduced it to the decision so `notices.compose_caption_cta`
#: could own the copy. Typed as a Literal so a caller cannot pass an arbitrary string into the
#: composer — mypy is the enforcement, and without it the narrowing at the one call site is a
#: convention rather than a check.
CaptionBucket = Literal["75", "90", ""]

THROTTLE_WINDOW: Final = timedelta(hours=24)
# TG-REFERRAL-W1 (C3): the value-moment referral nudge fires at most once per 7d.
REFERRAL_NUDGE_THROTTLE: Final = timedelta(days=7)


# `regime_alert_should_show_cta` and `regime_cta_text` DELETED — V2 CH1 R3.
#
# The gate was a bare `return False` that consulted none of its arguments, so the copy behind it
# was unreachable for every input. Deleting the predicate and the string together is the point:
# leaving either half would have left dark copy for L6 to police, and a `False` constant is not a
# feature flag — nothing could ever flip it without a code change anyway. The `regime_alert`
# campaign tag leaves the inventory with them.


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


def trade_call_cta_text(state: QuotaState, *, now: datetime | None = None) -> CaptionBucket:
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

    # The "100" branch is DELETED — V2 CH1 R3. It was unreachable on this lane: `alert_engine`
    # reaches this function only in the `else` of `if not decision.allowed`, and for a free user
    # `remaining <= 0` is exactly `monthly_exhausted`, which makes `exhausted` true and
    # `allowed` false. The wall refuses before a caption can render. `quota_threshold` still
    # RETURNS "100" — `referral_nudge_text` guards on that value and is unaffected.
    # 🛑 THE THROTTLE STAYS HERE; THE COPY DOES NOT. This function still owns WHETHER a caption
    # fires — the 24h-per-threshold window and the bucket — and returns the THRESHOLD, which the
    # caller composes through `notices.compose_caption_cta`. One throttle, one composer, instead
    # of a throttle that also writes copy.
    if threshold == "90":
        if _within_throttle(state.quota_90_last_fired_at, now):
            return ""
        return "90"

    if threshold == "75":
        if _within_throttle(state.quota_75_last_fired_at, now):
            return ""
        return "75"
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


# `quota_exhausted_message` DELETED — V2 CH1 R3. It had ZERO callers anywhere: not in `src/`,
# not in `scripts/`, not even in a test. It was a mirror of a signal-MCP string for a path the
# bot stopped taking, and it carried the x402 line onto a human surface. The one live
# walled-user body is `notices.compose_wall`.
