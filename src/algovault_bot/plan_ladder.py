"""GROWTH-TG-PLAN-PICKER-W1 R2 — the PINNED plan-ladder fallbacks, as a leaf module.

WHY A SEPARATE MODULE, AND WHY IT IS NOT OVER-ENGINEERING. These constants belong beside
`quota.FREE_TIER_MONTHLY_QUOTA` conceptually, and `quota.py` is where you should read them from
(it re-exports every name below, so `from .quota import PRO_PRICE_USD` works and every existing
importer is untouched). But `messages.py` also needs one of them — the six-month total, for the
welcome copy's default argument — and `quota.py` IMPORTS `messages.py`. A default argument is
evaluated at `def` time, so the deferred-import trick `paywall.py` uses for exactly this cycle
cannot supply one.

The choice was therefore: hand-type `39.90` a second time in `messages.py`, or put the data in a
module that imports nothing. Hand-typing it is the `_TIER_QUOTA` defect this wave exists to
retire — a figure bound to the ladder by nothing at all — so it is not a choice at all.

🛑 THIS MODULE IMPORTS NOTHING FROM THE PACKAGE, and must never start. It is a leaf precisely so
that both sides of the `quota` -> `messages` edge can read it, and one local import would put the
cycle straight back.

🛑 THESE ARE FALLBACKS, NOT THE ANSWER. The live values come from the ladder mirror
(`free_tier_ladder`, refreshed by the entitlement drain from signal-MCP's `GET /api/plans/public`,
whose SoT is `src/lib/plans.ts`). These are what we SERVE when that mirror is absent, stale or
unreadable — never a reason to refuse anyone, and never a reason to render nothing.

🛑 SEPARATE NAMED CONSTANTS, NEVER A DICT KEYED BY TIER. A `{"starter": ..., "pro": ...}` literal
is the `messages._TIER_QUOTA` shape that was wrong for every linked subscriber from the day the
ladder moved, and gate leg L4 in `scripts/check-quota-refusal-seam.py` fails any dict whose string
keys intersect the paid-tier names and whose values are numbers.

🛑 AND DO NOT WRITE TWO OF THESE FIGURES INTO ONE COMMENT separated by `/` or `·` — that is leg
L4b, which fails a ladder-shaped run of numbers in a comment for the same reason.
"""
from __future__ import annotations

from typing import Final

#: Starter's per-UTC-day cap.
STARTER_DAILY_CALLS: Final = 1_000
#: TOTAL charged once for Starter's six-month prepay term — not a monthly rate.
STARTER_PRICE_6MONTH_USD: Final = 39.90
#: Pro's monthly price.
PRO_PRICE_USD: Final = 49.0
#: Pro's monthly allowance.
PRO_MONTHLY_CALLS: Final = 100_000
#: Pro's per-UTC-day cap.
PRO_DAILY_CALLS: Final = 10_000
#: TOTAL charged once for Pro's six-month prepay term — not a monthly rate.
PRO_PRICE_6MONTH_USD: Final = 129.0
