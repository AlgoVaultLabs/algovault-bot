"""FEATURE-PARITY-CHANNELS-W1 CH4 — /scanwatch + the scheduled scan-digest producer."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from algovault_bot import alert_engine, handlers
from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA, get_quota_state

THREE = [
    {"coin": "BTC", "timeframe": "15m", "exchange": "BINANCE", "call": "BUY", "confidence": 80, "regime": "TRENDING_UP"},
    {"coin": "ETH", "timeframe": "15m", "exchange": "BINANCE", "call": "SELL", "confidence": 70, "regime": "TRENDING_DOWN"},
    {"coin": "SOL", "timeframe": "15m", "exchange": "BINANCE", "call": "BUY", "confidence": 65, "regime": "TRENDING_UP"},
]


def _result(calls: list[dict]) -> dict:
    nh = [c for c in calls if c["call"] != "HOLD"]
    return {"scanned": 20, "eligible_non_hold": len(nh), "holds": len(calls) - len(nh),
            "errors": 0, "partial": False, "calls": calls}


class _FakeMcp:
    def __init__(self, result: dict) -> None:
        self._r = result

    def __enter__(self) -> "_FakeMcp":
        return self

    def __exit__(self, *a) -> bool:
        return False

    def call_tool(self, name: str, args: dict) -> dict:
        return self._r


def _mock_engine(monkeypatch, result: dict, pushed: list):
    monkeypatch.setattr(alert_engine, "McpClient", lambda cfg: _FakeMcp(result))
    monkeypatch.setattr(alert_engine, "Bot", lambda token: object())

    async def fake_push(bot, chat_id, text, db=None):  # noqa: ANN001
        pushed.append((chat_id, text))
        return True

    monkeypatch.setattr(alert_engine, "_push", fake_push)


# ── /scanwatch + /unscanwatch + /list ──

def test_scanwatch_creates_row_and_reminder(tmp_db):
    reply = handlers.handle_scanwatch(tmp_db, 1, "u", "en", ["25", "1h"])
    rows = tmp_db.list_scan_watches(1)
    assert len(rows) == 1
    assert rows[0]["top_n"] == 25 and rows[0]["timeframe"] == "1h" and rows[0]["cadence"] == "1h"
    assert "every 1h" in reply and "max(1, calls)" in reply


def test_scanwatch_first_time_token_is_tf_second_is_cadence(tmp_db):
    # tf=4h (first time-token), cadence=1h (second) → cadence is FASTER → heads-up.
    reply = handlers.handle_scanwatch(tmp_db, 2, "u", "en", ["4h", "1h"])
    row = tmp_db.list_scan_watches(2)[0]
    assert row["timeframe"] == "4h" and row["cadence"] == "1h"
    assert "⚠️" in reply


def test_scanwatch_defaults(tmp_db):
    handlers.handle_scanwatch(tmp_db, 3, "u", "en", [])
    row = tmp_db.list_scan_watches(3)[0]
    assert (row["top_n"], row["timeframe"], row["exchange"], row["cadence"]) == (20, "15m", "BINANCE", "1h")


def test_unscanwatch_removes(tmp_db):
    handlers.handle_scanwatch(tmp_db, 4, "u", "en", ["20", "15m"])
    assert len(tmp_db.list_scan_watches(4)) == 1
    reply = handlers.handle_unscanwatch(tmp_db, 4, "u", "en", ["20", "15m"])
    assert "Removed" in reply
    assert tmp_db.list_scan_watches(4) == []


def test_list_shows_scan_digests(tmp_db):
    tmp_db.upsert_subscriber(5, "u", "en")
    handlers.handle_scanwatch(tmp_db, 5, "u", "en", ["20", "15m"])
    reply = handlers.handle_list(tmp_db, 5, "u", "en")
    assert "Scheduled scan digests" in reply and "top 20" in reply


# ── the scheduled producer ──

def test_producer_fires_meters_and_is_idempotent(tmp_db, monkeypatch):
    tmp_db.upsert_subscriber(111, "u", "en")
    tmp_db.add_scan_watch(111, 20, "15m", "BINANCE", "1h")
    pushed: list = []
    _mock_engine(monkeypatch, _result(THREE), pushed)
    now = 1_700_003_600

    counts = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now))
    assert counts["scan_fired"] == 1
    assert len(pushed) == 1 and pushed[0][0] == 111 and "BTC" in pushed[0][1]
    assert get_quota_state(tmp_db, 111).used == 3  # max(1, 3 non-HOLD)

    # Same bucket → idempotent (not due).
    c2 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 60))
    assert c2["scan_due"] == 0

    # Next bucket → fires again.
    c3 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 3600))
    assert c3["scan_fired"] == 1


def test_producer_shares_one_scan_across_same_param_subs(tmp_db, monkeypatch):
    for cid in (201, 202):
        tmp_db.upsert_subscriber(cid, "u", "en")
        tmp_db.add_scan_watch(cid, 20, "15m", "BINANCE", "1h")
    calls_seen = {"n": 0}

    class _CountingMcp(_FakeMcp):
        def call_tool(self, name, args):
            calls_seen["n"] += 1
            return _result(THREE)

    monkeypatch.setattr(alert_engine, "McpClient", lambda cfg: _CountingMcp(_result(THREE)))
    monkeypatch.setattr(alert_engine, "Bot", lambda token: object())

    async def fake_push(*a, **k):
        return True

    monkeypatch.setattr(alert_engine, "_push", fake_push)
    counts = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=1_700_003_600))
    assert calls_seen["n"] == 1  # ONE shared scan
    assert counts["scan_fired"] == 2  # both chats pushed


def test_producer_exhausted_owner_skips_push_but_advances_bucket(tmp_db, monkeypatch):
    tmp_db.upsert_subscriber(222, "u", "en")
    tmp_db.add_scan_watch(222, 20, "15m", "BINANCE", "1h")
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=?",
            (FREE_TIER_MONTHLY_QUOTA, datetime.now(timezone.utc).isoformat(), 222),
        )
    pushed: list = []
    _mock_engine(monkeypatch, _result(THREE), pushed)
    now = 1_700_003_600

    counts = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now))
    assert counts["scan_skipped_exhausted"] == 1
    assert pushed == []  # exhausted → no push, no charge
    # Bucket advanced → not due on the next tick in the same bucket (no re-scan storm).
    c2 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 60))
    assert c2["scan_due"] == 0


def test_producer_all_hold_round_suppressed_no_push_no_charge(tmp_db, monkeypatch):
    """All-HOLD round → silent (parity with /watch): no DM, no charge; bucket advances."""
    holds = [
        {"coin": "BTC", "timeframe": "15m", "exchange": "BINANCE", "call": "HOLD", "confidence": 40, "regime": "CHOPPY"},
        {"coin": "ETH", "timeframe": "15m", "exchange": "BINANCE", "call": "HOLD", "confidence": 30, "regime": "CHOPPY"},
    ]
    tmp_db.upsert_subscriber(333, "u", "en")
    tmp_db.add_scan_watch(333, 20, "15m", "BINANCE", "1h")
    pushed: list = []
    _mock_engine(monkeypatch, _result(holds), pushed)
    now = 1_700_003_600

    counts = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now))
    assert counts["scan_skipped_empty"] == 1
    assert counts["scan_fired"] == 0
    assert pushed == []  # all-HOLD → NO DM
    assert get_quota_state(tmp_db, 333).used == 0  # NO charge for an empty round
    # Bucket advanced → not re-due on the next tick in the same bucket (no re-scan storm).
    c2 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 60))
    assert c2["scan_due"] == 0
    # Next bucket with actionable calls → fires normally.
    _mock_engine(monkeypatch, _result(THREE), pushed)
    c3 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 3600))
    assert c3["scan_fired"] == 1 and len(pushed) == 1
