"""GROWTH-TG-LEVER-ACTIVATION-W1 / CH2 — the 4 quota-exhausted CTAs carry the source.

`scan_quota_exhausted` produced BOTH real conversions (two live $9.99 Stripe
checkouts, 128s and 126s from attribution row to checkout). Until this chapter it
emitted a bare URL, so the readout could say a user converted but not where they
came from — on the one path where conversion actually happens.

For scan/regime/funding the exhausted branch returns BEFORE any MCP call, so those
drive the real handlers with nothing stubbed — exhaust the quota in the DB and read
the returned string. `handle_call` is the exception: it fetches via MCP first and
gates only on a BUY/SELL (a HOLD is always free), so it alone needs a stub.

Covers CH2 AC 2.1-2.3.
"""
from __future__ import annotations

import pytest

from algovault_bot.db import Database
from algovault_bot.handlers import (
    handle_call,
    handle_funding,
    handle_regime,
    handle_scan,
)
from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA, consume_quota

# (handler, campaign tag) — the 4 sites this chapter wires.
SITES = [
    (handle_scan, "scan_quota_exhausted"),
    (handle_regime, "regime_quota_exhausted"),
    (handle_call, "call_quota_exhausted"),
    (handle_funding, "funding_quota_exhausted"),
]


def _exhaust(db: Database, chat_id: int) -> None:
    db.upsert_subscriber(chat_id, "tester", "en")
    for _ in range(FREE_TIER_MONTHLY_QUOTA):
        consume_quota(db, chat_id, 1)


@pytest.fixture()
def _stub_call_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """`handle_call` is structurally different from its three siblings: it fetches
    via MCP FIRST and only gates on quota when the verdict is BUY/SELL (a HOLD is
    always free). So it needs a BUY to even reach the CTA. Harmless for the other
    three, which gate before any upstream call."""
    from algovault_bot import handlers

    monkeypatch.setattr(
        handlers, "_call_via_mcp", lambda c, tf, ex: {"call": "BUY", "price": 1.0}
    )


# ── AC 2.1 — all 4 sites carry the source when one is recorded ────────────


@pytest.mark.parametrize("handler,campaign", SITES, ids=[c for _, c in SITES])
def test_quota_cta_carries_source(tmp_db: Database, handler, campaign, _stub_call_mcp) -> None:
    chat_id = 6001
    _exhaust(tmp_db, chat_id)
    assert tmp_db.set_acquisition_source_first_touch(chat_id, "devto") is True

    out = handler(tmp_db, chat_id, "tester", "en", [])

    assert f"utm_campaign={campaign}" in out, out
    assert "utm_medium=devto" in out, out
    # AC 2.2 — the channel slug is derived from this and must never be re-slugged
    assert "utm_source=tg_bot" in out, out


# ── AC 2.3 — an untagged user's URL is byte-identical to before the wave ──


@pytest.mark.parametrize("handler,campaign", SITES, ids=[c for _, c in SITES])
def test_quota_cta_untagged_is_byte_identical(tmp_db: Database, handler, campaign, _stub_call_mcp) -> None:
    chat_id = 6002
    _exhaust(tmp_db, chat_id)
    assert tmp_db.get_acquisition_source(chat_id) is None

    out = handler(tmp_db, chat_id, "tester", "en", [])

    assert (
        f"Upgrade for more: api.algovault.com/signup?plan=starter"
        f"&utm_source=tg_bot&utm_campaign={campaign}" in out
    ), out
    # absence is absence — no empty parameter, no utm_medium=none
    assert "utm_medium" not in out, out


# ── the source must be FIRST-TOUCH, not last-click, on the converting path ─


def test_converting_cta_reports_first_touch_not_latest(tmp_db: Database, _stub_call_mcp) -> None:
    """A later tagged link must not be able to claim a conversion the first earned."""
    chat_id = 6003
    _exhaust(tmp_db, chat_id)
    tmp_db.set_acquisition_source_first_touch(chat_id, "x")
    tmp_db.set_acquisition_source_first_touch(chat_id, "geo")  # no-op by design

    out = handle_scan(tmp_db, chat_id, "tester", "en", [])

    assert "utm_medium=x" in out and "utm_medium=geo" not in out, out


# ── the two dimensions stay orthogonal ───────────────────────────────────


def test_campaign_and_medium_are_not_collapsed(tmp_db: Database, _stub_call_mcp) -> None:
    """utm_campaign says WHICH CTA converted; utm_medium says HOW they found the bot."""
    chat_id = 6004
    _exhaust(tmp_db, chat_id)
    tmp_db.set_acquisition_source_first_touch(chat_id, "awesome_list")

    scan = handle_scan(tmp_db, chat_id, "tester", "en", [])
    funding = handle_funding(tmp_db, chat_id, "tester", "en", [])

    # same user, same source — different CTAs
    assert "utm_medium=awesome_list" in scan and "utm_medium=awesome_list" in funding
    assert "utm_campaign=scan_quota_exhausted" in scan
    assert "utm_campaign=funding_quota_exhausted" in funding
    for out in (scan, funding):
        assert out.count("utm_medium=") == 1 and out.count("utm_campaign=") == 1
