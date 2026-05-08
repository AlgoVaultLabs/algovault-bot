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

Only **trade-call alerts that actually fire** consume quota. HOLD verdicts and
regime alerts are FREE per the spec (matches signal-MCP's free-HOLD policy in
``getTradeSignal``).
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

    @property
    def remaining(self) -> int:
        if self.linked_tier in PAID_TIERS:
            return 10**9  # effectively unlimited; never hit ceiling
        return max(0, self.total - self.used)

    @property
    def exhausted(self) -> bool:
        if self.linked_tier in PAID_TIERS:
            return False
        return self.used >= self.total

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
    return QuotaState(used, FREE_TIER_MONTHLY_QUOTA, window_start, pct, linked_tier=linked_tier)


def consume_quota(db: Database, chat_id: int) -> QuotaState:
    """Increment the user's trade-call counter. Starts a new window if needed.

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
    new_used = state.used + 1
    with db._cursor() as cur:
        if state.window_start is None:
            window_start = _now()
            cur.execute(
                "UPDATE subscribers SET alert_count = ?, alerts_window_start = ? "
                "WHERE chat_id = ?",
                (new_used, window_start.isoformat(), chat_id),
            )
        else:
            window_start = state.window_start
            cur.execute(
                "UPDATE subscribers SET alert_count = ? WHERE chat_id = ?",
                (new_used, chat_id),
            )
    return QuotaState(
        new_used,
        FREE_TIER_MONTHLY_QUOTA,
        window_start,
        new_used / FREE_TIER_MONTHLY_QUOTA,
    )
