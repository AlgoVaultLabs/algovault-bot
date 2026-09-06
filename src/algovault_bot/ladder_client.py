"""GROWTH-TG-QUOTA-PARITY-W1 CH2a — the bot FOLLOWS the published plan ladder.

Before this wave the free allowance was a constant in `quota.py` and a hand-typed literal in
eleven shipped strings across three languages, bound to that constant by nothing at all. The
allowance is now signal-MCP's to declare: `src/lib/plans.ts` is the SoT, `GET /api/plans/public`
is its public projection (CH1), and this module mirrors that projection into the bot's own DB on
the EXISTING entitlement drain.

WHY A MIRROR AND NOT A FETCH-PER-READ. `quota.get_quota_state` runs O(subscribers)/minute on the
dispatch loop and `evaluate_delivery` is documented as never acquiring network I/O. So the drain
fetches once every five minutes and writes a single row; the serving path reads that row.

NO THIRD URL ENV VAR. The base is derived from `ALGOVAULT_MCP_URL` exactly as
`referral_client._referral_base()` and `capabilities.capabilities_url()` already do. A dedicated
env var would be a fourth place to get the host wrong.

FAIL-OPEN, ALWAYS. Every failure path here returns None and the caller serves `quota.py`'s pinned
fallbacks. A ladder we could not read is not a reason to refuse a user — the same discipline as
`PlanState.INDETERMINATE` on the paid lane, and the same as `capabilities.fetch_capabilities`'s
3-tier degradation one module over.

🛑 TOLERANT PARSE, BY CONTRACT. `parse_ladder` reads `free.monthly_calls`, `free.daily_calls` and
the starter rung, and IGNORES every other key — `_algovault` included (CH1 §2, ratified Q3=b).
A strict parser here would turn any future ADDITIVE change to a public endpoint into a bot
outage: signal-MCP ships a new field, the bot rejects the whole response, and every free
subscriber silently drops to the fallback ladder. Unknown keys are pinned as ACCEPTABLE by a
fixture in `tests/test_ladder_client.py` carrying two invented ones.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10.0

#: One rung, as mirrored: (price_usd, monthly_calls, daily_calls, price_usd_6month). `None` for
#: any field means "the response did not carry it" — absence, never a synthetic zero. The caller
#: falls back PER FIELD.
#:
#: GROWTH-TG-PLAN-PICKER-W1 R2 widened this from the starter-only pair. The bot's plan picker
#: renders four SKUs and both daily caps, so every one of those figures now arrives from the
#: server instead of being hand-typed beside a derived one.
Rung = tuple[float | None, int | None, int | None, float | None]


def ladder_url() -> str:
    """Derive the /api/plans/public URL from ALGOVAULT_MCP_URL (a sibling of /mcp)."""
    mcp_url = os.environ.get("ALGOVAULT_MCP_URL", "http://127.0.0.1:3000/mcp").rstrip("/")
    base = mcp_url[: -len("/mcp")] if mcp_url.endswith("/mcp") else mcp_url
    return f"{base}/api/plans/public"


_ABSENT_RUNG: Rung = (None, None, None, None)


def _rung(payload: dict[str, Any], tier_id: str) -> Rung:
    """Pull one tier's price + monthly calls + daily cap + 6-month total, or absence per field.

    Selected BY ID, never by position: `tiers[0]` would silently become Pro the day the ladder is
    reordered, and a reorder is not a change anyone would think to test the bot against. That rule
    is the whole reason this generalises to a `tier_id` parameter rather than growing a second
    position-indexed reader beside the first.

    `bool` is a subclass of `int` in Python, so `True` would otherwise mirror as a cap of 1 — the
    same guard `parse_ladder` applies to the free rung. Every numeric read here rejects it.
    """
    tiers = payload.get("tiers")
    if not isinstance(tiers, list):
        return _ABSENT_RUNG
    for t in tiers:
        if isinstance(t, dict) and t.get("id") == tier_id:
            price = t.get("price_usd")
            calls = t.get("monthly_calls")
            daily = t.get("daily_calls")
            six = t.get("price_usd_6month")
            return (
                float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else None,
                int(calls) if isinstance(calls, int) and not isinstance(calls, bool) else None,
                int(daily) if isinstance(daily, int) and not isinstance(daily, bool) else None,
                float(six) if isinstance(six, (int, float)) and not isinstance(six, bool) else None,
            )
    return _ABSENT_RUNG


def parse_ladder(payload: Any) -> dict[str, Any] | None:
    """Project the mirrored fields out of a `/api/plans/public` body.

    Returns None when the FREE rung — the only part the meter cannot serve without — is missing or
    non-positive. The starter rung is optional: it feeds copy, not enforcement, so its absence
    degrades a price string to its pinned fallback rather than dropping the whole ladder.

    Unknown top-level and per-tier keys are ignored by construction (see the module docstring).
    """
    if not isinstance(payload, dict):
        return None
    free = payload.get("free")
    if not isinstance(free, dict):
        return None
    monthly = free.get("monthly_calls")
    daily = free.get("daily_calls")
    # bool is a subclass of int in Python; `True` must never be read as a quota of 1.
    if not isinstance(monthly, int) or isinstance(monthly, bool) or monthly <= 0:
        return None
    if not isinstance(daily, int) or isinstance(daily, bool) or daily <= 0:
        return None
    s_price, s_calls, s_daily, s_six = _rung(payload, "starter")
    p_price, p_calls, p_daily, p_six = _rung(payload, "pro")
    return {
        "free_monthly": monthly,
        "free_daily": daily,
        "starter_price_usd": s_price,
        "starter_monthly_calls": s_calls,
        # GROWTH-TG-PLAN-PICKER-W1 R2 — the rest of the four-SKU ladder the picker renders.
        # An endpoint that has not yet shipped `price_usd_6month` yields None here, and None
        # means the caller serves its pinned constant. That is what makes the deploy ORDER of
        # the two repos free: this file can land before the server field does.
        "starter_daily_calls": s_daily,
        "starter_price_usd_6month": s_six,
        "pro_price_usd": p_price,
        "pro_monthly_calls": p_calls,
        "pro_daily_calls": p_daily,
        "pro_price_usd_6month": p_six,
    }


def fetch_ladder(url: str | None = None) -> dict[str, Any] | None:
    """Fetch + parse the published ladder. Returns None on ANY failure — never raises.

    The caller (the entitlement drain) treats None as "keep the mirror we already have", so a
    transient outage costs nothing: the previous row stays, and only a mirror that goes stale for
    longer than `quota.LADDER_STALE_AFTER` drops the meter to its pinned constants.
    """
    target = url or ladder_url()
    try:
        resp = httpx.get(target, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        parsed = parse_ladder(resp.json())
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as e:
        log.warning(json.dumps({"event": "ladder_fetch_failed", "err": str(e)[:200]}))
        return None
    if parsed is None:
        log.warning(json.dumps({"event": "ladder_payload_unusable", "url": target}))
        return None
    return parsed
