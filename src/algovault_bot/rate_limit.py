"""Rate-limiting — Telegram-imposed only.

The 24h per-user caps (20 regime / 30 calls / 50 burn-protection) were
removed 2026-05-08 — Telegram doesn't impose per-user caps, so neither
should we. The 100 calls/month bot-side quota (in ``quota.py``) is the
only call-volume cap; the funnel mechanic relies on users hitting it.

What remains here:

* **TELEGRAM_GLOBAL_SEMAPHORE(25)** — keeps us under Telegram's 30 msg/sec
  ceiling across the entire bot. Telegram-imposed; required.

The schema columns ``alerts_24h_*`` + ``calls_burn_suppressed_until`` are
left in place for backward-compat (no migration cost; harmless), but no
code reads or writes them anymore.
"""

from __future__ import annotations

import asyncio
from typing import Final


# Telegram allows ~30 msg/sec to the same bot; we use 25 to leave headroom
# for command replies running concurrently with the cron's alert fanout.
TELEGRAM_GLOBAL_SEMAPHORE: Final = asyncio.Semaphore(25)
