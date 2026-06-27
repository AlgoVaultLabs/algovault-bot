"""Bot-side per-user quota tracking (D1-C).

signal-MCP's free tier is keyed by IP hash (one bucket per request IP). The
bot calls signal-MCP from one Hetzner host → all bot users would share one
bucket. D1-C resolves this by:

1. Bot calls signal-MCP with the X-AlgoVault-Internal-Key header → maps to
   ``tier:'internal'`` server-side → quota counter bypassed.
2. Bot enforces the user-facing 100 calls/month cap **here**, in its own
   SQLite ``subscribers`` table.

Calendar-month windowing matches signal-MCP's existing ``MONTH_MS`` rolling
30-day model: at first trade-call alert each new window, ``alerts_window_start``
is set to the current ``datetime('now')``; subsequent calls in the same
30-day window increment ``alert_count``.

Both **trade-call alerts (BUY/SELL)** and **regime-shift alerts** that actually
fire consume quota — parity with signal-MCP, which meters get_trade_call
(non-HOLD), get_market_regime, and scan_funding_arb alike. Only **HOLD trade
calls** stay free (silent, no tick), mirroring signal-MCP's free-HOLD policy in
``getTradeSignal``.

QUOTA-CONSISTENCY-COUNT-ALL-W1 (2026-06-08): corrected the prior premise that
regime alerts were free. (Funding-arb / market-scan are not yet bot features —
metering for those is deferred to a follow-up wave.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from .db import Database


log = logging.getLogger(__name__)

FREE_TIER_MONTHLY_QUOTA: Final = 100
WINDOW_DAYS: Final = 30
WINDOW = timedelta(days=WINDOW_DAYS)

# BOT-W2 C3 — paid tiers bypass the bot-side 100/mo cap entirely. The user's
# real Stripe-backed quota (3K/15K/100K) is enforced server-side by signal-MCP
# on their direct API calls; bot-driven calls go through tier:'internal' which
# doesn't tick any counter. Net: paid users get unlimited bot pushes.
PAID_TIERS: Final[frozenset[str]] = frozenset({"starter", "pro", "enterprise", "x402"})


@dataclass
class QuotaState:
    used: int
    total: int
    window_start: datetime | None
    pct_used: float
    # BOT-W2 C3 — set when subscribers.linked_tier is in PAID_TIERS.
    # Engine reads this to skip the quota gate + the quota line in the alert
    # message + all 75/90/100% CTAs (the user is already paying).
    linked_tier: str | None = None
    # BOT-ALERT-CLEANUP-W1 — last-fired timestamps for the soft 75% / urgent
    # 90% trade-call CTAs. ``cta.trade_call_cta_text`` uses these to enforce a
    # 24h-per-threshold throttle so users see at most one nudge per threshold
    # per day. Both are ``None`` until the threshold first fires.
    quota_75_last_fired_at: datetime | None = None
    quota_90_last_fired_at: datetime | None = None
    # TG-REFERRAL-W1 (C2) — bot-side referee bonus-call pool (persistent; NOT
    # window-reset). Drawn AFTER the monthly free `total` by consume_quota, and
    # it extends `remaining`/`exhausted`. 0 for everyone who wasn't referred →
    # byte-identical behaviour for the existing free/paid base.
    referral_bonus_remaining: int = 0
    # TG-REFERRAL-W1 (C3) — last value-moment referral-nudge timestamp; the 7d
    # throttle lives in cta.referral_nudge_text. None until the first nudge fires.
    referral_nudge_last_at: datetime | None = None

    @property
    def remaining(self) -> int:
        if self.linked_tier in PAID_TIERS:
            return 10**9  # effectively unlimited; never hit ceiling
        return max(0, self.total - self.used) + self.referral_bonus_remaining

    @property
    def exhausted(self) -> bool:
        if self.linked_tier in PAID_TIERS:
            return False
        return self.used >= self.total and self.referral_bonus_remaining <= 0

    @property
    def is_paid(self) -> bool:
        return self.linked_tier in PAID_TIERS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # SQLite datetime('now') yields "YYYY-MM-DD HH:MM:SS" (UTC, no tz suffix)
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def get_quota_state(db: Database, chat_id: int) -> QuotaState:
    """Read the user's current quota state. Auto-rolls expired window.

    BOT-W2 C3: when the subscriber is linked to a paid tier, the QuotaState's
    ``linked_tier`` field is populated and the engine treats the user as
    unlimited (skips the gate, omits the quota line, suppresses CTAs).
    """
    row = db.get_subscriber(chat_id)
    if row is None:
        return QuotaState(0, FREE_TIER_MONTHLY_QUOTA, None, 0.0, linked_tier=None)

    used = int(row["alert_count"] or 0)
    window_start = _parse_ts(row["alerts_window_start"])
    linked_tier = row["linked_tier"]
    last_75 = _parse_ts(row["quota_75_last_fired_at"]) if "quota_75_last_fired_at" in row.keys() else None
    last_90 = _parse_ts(row["quota_90_last_fired_at"]) if "quota_90_last_fired_at" in row.keys() else None
    bonus = int(row["referral_bonus_remaining"] or 0) if "referral_bonus_remaining" in row.keys() else 0
    nudge_last = _parse_ts(row["referral_nudge_last_at"]) if "referral_nudge_last_at" in row.keys() else None

    # Window expired → reset counter (still applies to free-tier users; paid
    # users don't tick the counter at all per ``consume_quota`` below).
    if window_start is not None and (_now() - window_start) > WINDOW:
        used = 0
        window_start = None
        with db._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET alert_count = 0, alerts_window_start = NULL "
                "WHERE chat_id = ?",
                (chat_id,),
            )

    pct = (used / FREE_TIER_MONTHLY_QUOTA) if FREE_TIER_MONTHLY_QUOTA else 0.0
    return QuotaState(
        used,
        FREE_TIER_MONTHLY_QUOTA,
        window_start,
        pct,
        linked_tier=linked_tier,
        quota_75_last_fired_at=last_75,
        quota_90_last_fired_at=last_90,
        referral_bonus_remaining=bonus,
        referral_nudge_last_at=nudge_last,
    )


def _clamp_units(units: int) -> int:
    """Default-deny clamp (CLAUDE.md): NaN/invalid/<1 → 1; else floor to an int ≥ 1."""
    try:
        u = int(units)
    except (TypeError, ValueError):
        return 1
    return u if u >= 1 else 1


def consume_quota(db: Database, chat_id: int, units: int = 1) -> QuotaState:
    """Increment the user's billable-alert counter by `units` (default 1 = byte-identical
    for existing callers; the scanner rule passes max(1, non-HOLD) — FEATURE-PARITY-CHANNELS-W1
    CH3). Starts a new window if needed.

    BOT-W2 C3: paid-tier-linked users SKIP the increment entirely (no-op
    return of current state). Their bot-pushed alerts don't count against
    anything — not the bot's 100/mo, not signal-MCP's per-key quota (bot
    calls go through tier:'internal' which bypasses the counter server-side).

    Free / unlinked users: same as W1 — increment + start a new 30-day window
    if needed, write the timestamp via Python (canonical ISO 8601) so the
    next ``get_quota_state`` reads back the exact same value (no microsecond
    drift across calls within the same window).
    """
    state = get_quota_state(db, chat_id)
    if state.is_paid:
        return state  # no-op for paid tiers
    units = _clamp_units(units)
    bonus = state.referral_bonus_remaining
    if bonus > 0:
        # TG-REFERRAL-W1: fill the monthly free headroom first, then draw any
        # overflow from the referee bonus pool (persistent; not window-reset).
        headroom = max(0, FREE_TIER_MONTHLY_QUOTA - state.used)
        monthly_charge = min(units, headroom)
        new_used = state.used + monthly_charge
        new_bonus = max(0, bonus - (units - monthly_charge))
    else:
        # byte-identical to the pre-bonus meter for the (today: 100%) bonus-free base
        new_used = state.used + units
        new_bonus = 0
    with db._cursor() as cur:
        if state.window_start is None:
            window_start = _now()
            cur.execute(
                "UPDATE subscribers SET alert_count = ?, alerts_window_start = ?, "
                "referral_bonus_remaining = ? WHERE chat_id = ?",
                (new_used, window_start.isoformat(), new_bonus, chat_id),
            )
        else:
            window_start = state.window_start
            cur.execute(
                "UPDATE subscribers SET alert_count = ?, referral_bonus_remaining = ? "
                "WHERE chat_id = ?",
                (new_used, new_bonus, chat_id),
            )
    return QuotaState(
        new_used,
        FREE_TIER_MONTHLY_QUOTA,
        window_start,
        new_used / FREE_TIER_MONTHLY_QUOTA,
        referral_bonus_remaining=new_bonus,
    )


# ── BOT-DIGEST-COUNT-ALL-CALLS-W1: the single delivery seam ──────────────────
# Every bot delivery path routes one actionable item through one of these recorders,
# which BOTH log the row for the digest AND meter quota — so alerts_fired can never
# again drift from the quota meter (the bug this wave fixes: scanwatch + scan charged
# quota but never wrote alerts_fired, so the digest undercounted). Future delivery
# paths (webhook top:N, batch tools) inherit correct telemetry by calling these.


def record_call_delivered(db: Database, chat_id: int, source: str) -> None:
    """Record + meter ONE delivered actionable trade call. ``alerts_fired`` INSERT
    ALWAYS (even paid tiers — the digest is delivery volume, not billing); quota
    ``consume_quota`` is a no-op for paid tiers. Call exactly once per non-HOLD call
    delivered (HOLD verdicts stay silent + free and never reach here). ``source`` ∈
    {'watch','scanwatch','scan',...} per ALLOWED_ALERT_SOURCES."""
    db.record_alert_fired(chat_id, "call", source)
    consume_quota(db, chat_id)


def record_regime_delivered(db: Database, chat_id: int, source: str) -> None:
    """Record + meter ONE delivered regime-shift alert — same insert+meter contract
    as ``record_call_delivered`` (regime alerts count toward quota since
    QUOTA-CONSISTENCY-COUNT-ALL-W1). Bot regime pushes are ``source='watch'``."""
    db.record_alert_fired(chat_id, "regime", source)
    consume_quota(db, chat_id)
