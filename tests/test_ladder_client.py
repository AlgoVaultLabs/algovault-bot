"""GROWTH-TG-QUOTA-PARITY-W1 CH2a — the ladder mirror's parser.

The single most important property here is NEGATIVE: the parser must IGNORE keys it does not
know. signal-MCP's `/api/plans/public` is a public endpoint that will grow fields; a strict parser
would turn every additive change there into a silent bot outage, dropping the whole free base to
the fallback ladder for no reason. That is pinned below with a fixture carrying invented keys.
"""
from __future__ import annotations

import os

import pytest

from algovault_bot.ladder_client import ladder_url, parse_ladder
from algovault_bot.quota import (
    FREE_TIER_DAILY_QUOTA,
    FREE_TIER_MONTHLY_QUOTA,
    STARTER_MONTHLY_CALLS,
    STARTER_PRICE_USD,
)

#: The live response, captured verbatim 2026-08-27 from
#: `GET https://api.algovault.com/api/plans/public`.
LIVE_BODY = {
    "free": {"monthly_calls": 200, "daily_calls": 100},
    "tiers": [
        {"id": "starter", "label": "Starter", "monthly_calls": 10000,
         "daily_calls": 1000, "price_usd": 9.99},
        {"id": "pro", "label": "Pro", "monthly_calls": 100000,
         "daily_calls": 10000, "price_usd": 49},
        {"id": "enterprise", "label": "Enterprise", "monthly_calls": 100000,
         "daily_calls": None, "price_usd": 299},
    ],
    "generated_at": "2026-08-27T12:59:58.767Z",
    "_algovault": {
        "brand": "The Brain Layer for AI Trading Agents",
        "note": "Building an agent? Get a free API key — higher limits, all tools, x402 pay-per-call.",
        "get_started": "https://algovault.com/#pricing",
        "docs": "https://algovault.com/docs.html",
    },
}


def test_parses_the_live_response() -> None:
    out = parse_ladder(LIVE_BODY)
    assert out == {
        "free_monthly": 200,
        "free_daily": 100,
        "starter_price_usd": 9.99,
        "starter_monthly_calls": 10000,
    }


def test_unknown_keys_are_IGNORED_never_rejected() -> None:
    """🛑 The ratified contract (CH1 §2 / Q3=b).

    `_algovault` is already an unknown-to-the-meter key and is in the live body above. This adds
    two INVENTED ones, top-level and per-tier, standing in for whatever signal-MCP ships next. A
    parser that rejected them would take the bot's whole free lane to the fallback ladder on a
    change that broke nothing.
    """
    body = {
        **LIVE_BODY,
        "some_future_field": {"nested": [1, 2, 3]},
        "another_new_key": "whatever",
    }
    body["tiers"] = [{**t, "future_per_tier_key": True} for t in body["tiers"]]
    assert parse_ladder(body) == parse_ladder(LIVE_BODY)


def test_the_starter_rung_is_selected_by_id_not_position() -> None:
    """`tiers[0]` would silently become Pro the day the ladder is reordered."""
    body = {**LIVE_BODY, "tiers": list(reversed(LIVE_BODY["tiers"]))}
    out = parse_ladder(body)
    assert out is not None
    assert out["starter_price_usd"] == 9.99
    assert out["starter_monthly_calls"] == 10000


def test_a_missing_starter_rung_degrades_only_the_price() -> None:
    """The starter rung feeds COPY; the free rung feeds ENFORCEMENT. Losing the former must not
    drop the latter."""
    body = {**LIVE_BODY, "tiers": [t for t in LIVE_BODY["tiers"] if t["id"] != "starter"]}
    out = parse_ladder(body)
    assert out is not None
    assert out["free_monthly"] == 200 and out["free_daily"] == 100
    assert out["starter_price_usd"] is None and out["starter_monthly_calls"] is None


@pytest.mark.parametrize(
    "body",
    [
        None,
        "not a dict",
        {},
        {"free": None},
        {"free": {}},
        {"free": {"monthly_calls": 200}},                      # no daily
        {"free": {"daily_calls": 100}},                        # no monthly
        {"free": {"monthly_calls": 0, "daily_calls": 100}},    # zero is not a quota
        {"free": {"monthly_calls": -5, "daily_calls": 100}},
        {"free": {"monthly_calls": "200", "daily_calls": 100}},  # a string is not an int
        {"free": {"monthly_calls": True, "daily_calls": 100}},   # bool is an int subclass
    ],
)
def test_an_unusable_payload_returns_none(body) -> None:
    """None means "keep the mirror you have and serve the fallback" — never "refuse the user"."""
    assert parse_ladder(body) is None


def test_ladder_url_is_derived_from_the_mcp_url(monkeypatch) -> None:
    """No third URL env var: the base is the /mcp sibling, exactly as referral_client does it."""
    monkeypatch.setenv("ALGOVAULT_MCP_URL", "https://api.algovault.com/mcp")
    assert ladder_url() == "https://api.algovault.com/api/plans/public"
    monkeypatch.setenv("ALGOVAULT_MCP_URL", "https://api.algovault.com/mcp/")
    assert ladder_url() == "https://api.algovault.com/api/plans/public"
    monkeypatch.delenv("ALGOVAULT_MCP_URL", raising=False)
    assert ladder_url() == "http://127.0.0.1:3000/api/plans/public"


def test_the_pinned_fallbacks_equal_the_captured_live_ladder() -> None:
    """🛑 CH2b's acceptance criterion, in the deterministic form.

    A fallback that has drifted from the thing it stands in for is the same defect as a hand-typed
    constant. This asserts against LIVE_BODY, captured verbatim from production on 2026-08-27 —
    hermetic, so it cannot flake on a network blip. The NON-hermetic half (does LIVE_BODY still
    match production?) is `test_live_endpoint_still_matches_the_pinned_fallbacks` below, which is
    opt-in precisely because a suite that needs the internet is a suite that fails on a train.
    """
    out = parse_ladder(LIVE_BODY)
    assert out is not None
    assert out["free_monthly"] == FREE_TIER_MONTHLY_QUOTA
    assert out["free_daily"] == FREE_TIER_DAILY_QUOTA
    assert out["starter_price_usd"] == STARTER_PRICE_USD
    assert out["starter_monthly_calls"] == STARTER_MONTHLY_CALLS


@pytest.mark.skipif(
    not os.environ.get("ALGOVAULT_LIVE_LADDER_CHECK"),
    reason="live network probe; set ALGOVAULT_LIVE_LADDER_CHECK=1 to run",
)
def test_live_endpoint_still_matches_the_pinned_fallbacks() -> None:
    """The seam the hermetic test above is structurally blind to.

    A hermetic fixture cannot notice that production has moved away from it — that is exactly what
    `verification-gates.md` means by "a hermetic self-test is blind to what its own seam replaces".
    This hits the real endpoint. Opt-in, and run by hand at ship time with the output pasted into
    the wave report.
    """
    import httpx

    url = os.environ.get(
        "ALGOVAULT_LIVE_LADDER_URL", "https://api.algovault.com/api/plans/public"
    )
    out = parse_ladder(httpx.get(url, timeout=20.0).json())
    assert out is not None, f"live endpoint returned an unusable body: {url}"
    assert out["free_monthly"] == FREE_TIER_MONTHLY_QUOTA
    assert out["free_daily"] == FREE_TIER_DAILY_QUOTA
    assert out["starter_price_usd"] == STARTER_PRICE_USD
    assert out["starter_monthly_calls"] == STARTER_MONTHLY_CALLS
