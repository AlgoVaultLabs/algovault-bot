"""FEATURE-PARITY-CHANNELS-W1 CH3 — /scan pull command + metering + derived surface."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from algovault_bot import handlers
from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA, get_quota_state
from algovault_bot.validators import ALERT_TYPES

THREE = [
    {"coin": "BTC", "timeframe": "15m", "exchange": "BINANCE", "call": "BUY", "confidence": 80, "regime": "TRENDING_UP"},
    {"coin": "ETH", "timeframe": "15m", "exchange": "BINANCE", "call": "SELL", "confidence": 70, "regime": "TRENDING_DOWN"},
    {"coin": "SOL", "timeframe": "15m", "exchange": "BINANCE", "call": "BUY", "confidence": 65, "regime": "TRENDING_UP"},
]


def _result(calls: list[dict]) -> dict:
    non_hold = [c for c in calls if c["call"] != "HOLD"]
    return {"scanned": 20, "eligible_non_hold": len(non_hold),
            "holds": len(calls) - len(non_hold), "errors": 0, "partial": False, "calls": calls}


def test_scan_returns_ranked_calls_and_meters_n(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, "_scan_via_mcp", lambda *a, **k: _result(THREE))
    reply = handlers.handle_scan(tmp_db, 111, "u", "en", [])
    for coin in ("BTC", "ETH", "SOL"):
        assert coin in reply
    assert "BUY" in reply and "SELL" in reply
    assert get_quota_state(tmp_db, 111).used == 3  # max(1, 3 non-HOLD)


def test_hold_only_or_empty_scan_charges_one(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, "_scan_via_mcp", lambda *a, **k: _result([]))
    handlers.handle_scan(tmp_db, 222, "u", "en", [])
    assert get_quota_state(tmp_db, 222).used == 1  # max(1, 0)


def test_scan_passes_parsed_args_to_mcp(tmp_db, monkeypatch):
    captured: dict = {}

    def fake(top_n, tf, exchange, rank=None):
        captured.update(top_n=top_n, tf=tf, exchange=exchange, rank=rank)
        return _result(THREE)

    monkeypatch.setattr(handlers, "_scan_via_mcp", fake)
    handlers.handle_scan(tmp_db, 333, "u", "en", ["25", "1h", "BYBIT"])
    # SCAN-RANKBY-W1: no rank token → rank None (byte-identical forwarding; MCP defaults oi).
    assert captured == {"top_n": 25, "tf": "1h", "exchange": "BYBIT", "rank": None}


def test_scan_defaults_when_no_args(tmp_db, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(handlers, "_scan_via_mcp",
                        lambda t, f, e, r=None: (captured.update(t=t, f=f, e=e, r=r), _result(THREE))[1])
    handlers.handle_scan(tmp_db, 334, "u", "en", [])
    assert captured == {"t": 20, "f": "15m", "e": "BINANCE", "r": None}


def test_paid_user_not_charged(tmp_db, monkeypatch):
    tmp_db.upsert_subscriber(444, "u", "en")
    with tmp_db._cursor() as cur:
        cur.execute("UPDATE subscribers SET linked_tier='starter' WHERE chat_id=?", (444,))
    monkeypatch.setattr(handlers, "_scan_via_mcp", lambda *a, **k: _result(THREE))
    handlers.handle_scan(tmp_db, 444, "u", "en", [])
    assert get_quota_state(tmp_db, 444).used == 0  # paid = consume_quota no-op


def test_exhausted_free_user_blocked_no_scan(tmp_db, monkeypatch):
    tmp_db.upsert_subscriber(555, "u", "en")
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=?",
            (FREE_TIER_MONTHLY_QUOTA, datetime.now(timezone.utc).isoformat(), 555),
        )
    called = {"n": 0}

    def fake(*a, **k):
        called["n"] += 1
        return _result(THREE)

    monkeypatch.setattr(handlers, "_scan_via_mcp", fake)
    reply = handlers.handle_scan(tmp_db, 555, "u", "en", [])
    assert "Upgrade" in reply
    assert called["n"] == 0  # exhausted → no scan
    assert get_quota_state(tmp_db, 555).used == FREE_TIER_MONTHLY_QUOTA  # not charged


def test_bad_arg_returns_usage_no_scan(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, "_scan_via_mcp", lambda *a, **k: pytest.fail("must not scan on bad args"))
    reply = handlers.handle_scan(tmp_db, 666, "u", "en", ["BTC,ETH"])
    assert "Usage:" in reply


def test_alert_types_derive_to_canonical_set():
    # ALERT_TYPES is now DERIVED from capabilities.BOT_TOOL_SURFACE (+ 'both'),
    # value-identical to the prior hardcoded set.
    assert ALERT_TYPES == frozenset({"calls", "regime", "both"})
