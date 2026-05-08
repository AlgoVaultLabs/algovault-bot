"""Anti-abuse rate-limiting (C5).

Per-user 24h rolling-window caps:
- 20 regime alerts / 24h
- 30 trade-call alerts / 24h
- 50 trade-call fetches / 24h → quota-burn protection (suppress remaining
  trade-call alerts for the 24h window; one explanatory message sent on
  the 51st attempt).

Per-bot Telegram global rate-limit: ``asyncio.Semaphore(25)`` keeps us under
Telegram's 30 msg/sec ceiling (5 req/sec headroom for command replies + the
fanout we cap below).

D7 inline-fix: spec line 415 said "30 min stable cycles" — corrected to
"≥ 2 TF-cycles" (matches C3 line 281). 30 min is only correct at 15m TF;
for 1h-TF the suppression-confirm window is 2h.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from .db import Database


log = logging.getLogger(__name__)


REGIME_24H_CAP: Final = 20
CALLS_24H_CAP: Final = 30
QUOTA_BURN_24H_CAP: Final = 50

# Telegram allows ~30 msg/sec to the same bot. We use 25 to leave headroom
# for command replies running concurrently with the cron's alert fanout.
TELEGRAM_GLOBAL_SEMAPHORE: Final = asyncio.Semaphore(25)

WINDOW_24H = timedelta(hours=24)


@dataclass
class RateLimitState:
    regime_count: int
    calls_count: int
    window_start: datetime | None
    burn_suppressed_until: datetime | None

    def regime_capped(self) -> bool:
        return self.regime_count >= REGIME_24H_CAP

    def calls_capped(self) -> bool:
        return self.calls_count >= CALLS_24H_CAP

    def quota_burn_capped(self) -> bool:
        return self.calls_count >= QUOTA_BURN_24H_CAP

    def burn_suppression_active(self) -> bool:
        if self.burn_suppressed_until is None:
            return False
        return _now() < self.burn_suppressed_until


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def get_rate_limit_state(db: Database, chat_id: int) -> RateLimitState:
    """Read the user's current 24h state. Auto-rolls expired window."""
    row = db.get_subscriber(chat_id)
    if row is None:
        return RateLimitState(0, 0, None, None)

    regime = int(row["alerts_24h_regime_count"] or 0)
    calls = int(row["alerts_24h_calls_count"] or 0)
    window_start = _parse_ts(row["alerts_24h_window_start"])
    burn_until = _parse_ts(row["calls_burn_suppressed_until"])

    # Roll the 24h window if expired.
    if window_start is not None and (_now() - window_start) > WINDOW_24H:
        regime = 0
        calls = 0
        window_start = None
        burn_until = None
        with db._cursor() as cur:
            cur.execute(
                """
                UPDATE subscribers
                SET alerts_24h_regime_count = 0,
                    alerts_24h_calls_count = 0,
                    alerts_24h_window_start = NULL,
                    calls_burn_suppressed_until = NULL
                WHERE chat_id = ?
                """,
                (chat_id,),
            )

    return RateLimitState(regime, calls, window_start, burn_until)


def _ensure_window(db: Database, chat_id: int, state: RateLimitState) -> datetime:
    if state.window_start is not None:
        return state.window_start
    now = _now()
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alerts_24h_window_start = ? WHERE chat_id = ?",
            (now.isoformat(), chat_id),
        )
    return now


def increment_regime_count(db: Database, chat_id: int) -> RateLimitState:
    state = get_rate_limit_state(db, chat_id)
    _ensure_window(db, chat_id, state)
    new_count = state.regime_count + 1
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alerts_24h_regime_count = ? WHERE chat_id = ?",
            (new_count, chat_id),
        )
    state.regime_count = new_count
    return state


def increment_calls_count(db: Database, chat_id: int) -> RateLimitState:
    state = get_rate_limit_state(db, chat_id)
    _ensure_window(db, chat_id, state)
    new_count = state.calls_count + 1
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alerts_24h_calls_count = ? WHERE chat_id = ?",
            (new_count, chat_id),
        )
    state.calls_count = new_count
    return state


def trip_burn_suppression(db: Database, chat_id: int) -> datetime:
    """Set ``calls_burn_suppressed_until = now + 24h``. Returns the until-time."""
    until = _now() + WINDOW_24H
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET calls_burn_suppressed_until = ? WHERE chat_id = ?",
            (until.isoformat(), chat_id),
        )
    return until
