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


@dataclass
class QuotaState:
    used: int
    total: int
    window_start: datetime | None
    pct_used: float

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.total


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
    """Read the user's current quota state. Auto-rolls expired window."""
    row = db.get_subscriber(chat_id)
    if row is None:
        return QuotaState(0, FREE_TIER_MONTHLY_QUOTA, None, 0.0)

    used = int(row["alert_count"] or 0)
    window_start = _parse_ts(row["alerts_window_start"])

    # Window expired → reset counter.
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
    return QuotaState(used, FREE_TIER_MONTHLY_QUOTA, window_start, pct)


def consume_quota(db: Database, chat_id: int) -> QuotaState:
    """Increment the user's trade-call counter. Starts a new window if needed.

    Called BEFORE actually pushing a trade-call alert. The bot must check
    ``state.exhausted`` and route the user to the upgrade message if true,
    instead of consuming + pushing.

    Implementation note: when starting a new window we write the timestamp
    via Python (canonical ISO 8601 with microseconds) rather than SQLite's
    ``datetime('now')`` (second-precision, no tz). That way the next
    ``get_quota_state`` call reads back the exact same value — no
    microsecond drift across calls within the same window.
    """
    state = get_quota_state(db, chat_id)
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
