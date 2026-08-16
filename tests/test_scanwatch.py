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
    def __init__(self, result: dict, depth: dict | None = None) -> None:
        self._r = result
        self._depth = depth or {}

    def __enter__(self) -> "_FakeMcp":
        return self

    def __exit__(self, *a) -> bool:
        return False

    def call_tool(self, name: str, args: dict) -> dict:
        # scan_trade_calls → the breadth result; get_trade_call → per-coin depth.
        return self._depth if name == "get_trade_call" else self._r


def _mock_engine(monkeypatch, result: dict, pushed: list):
    monkeypatch.setattr(alert_engine, "McpClient", lambda cfg: _FakeMcp(result))
    monkeypatch.setattr(alert_engine, "Bot", lambda token: object())

    async def fake_push(bot, chat_id, text, db=None):  # noqa: ANN001
        pushed.append((chat_id, text))
        return True

    monkeypatch.setattr(alert_engine, "_push", fake_push)


# ── /scanwatch + /unscanwatch + /list ──

def test_scanwatch_creates_row_and_confirmation(tmp_db):
    reply = handlers.handle_scanwatch(tmp_db, 1, "u", "en", ["25", "1h"])
    rows = tmp_db.list_scan_watches(1)
    assert len(rows) == 1
    assert rows[0]["top_n"] == 25 and rows[0]["timeframe"] == "1h"
    # TG-SCANWATCH-TF-CADENCE-W1: card states the TF re-check cadence + content-dedup (no reminder).
    assert "re-check every 1h" in reply and "NEW BUY/SELL" in reply


def test_scanwatch_parser_tf_and_vestigial_cadence_arg(tmp_db):
    # Parser unchanged: 1st time-token=tf, 2nd (valid cadence)=cadence. Under TF-dispatch the
    # cadence column is vestigial; the card states the TF re-check cadence (every 4h), not "1h".
    reply = handlers.handle_scanwatch(tmp_db, 2, "u", "en", ["4h", "1h"])
    row = tmp_db.list_scan_watches(2)[0]
    assert row["timeframe"] == "4h"
    assert "re-check every 4h" in reply


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

def test_producer_fires_meters_and_dedups(tmp_db, monkeypatch):
    tmp_db.upsert_subscriber(111, "u", "en")
    tmp_db.add_scan_watch(111, 20, "15m", "BINANCE", "1h")
    pushed: list = []
    _mock_engine(monkeypatch, _result(THREE), pushed)
    now = 1_700_002_800  # aligned to 15m (900s) and 5m (300s)

    # first 15m bucket → fires, meters max(1, 3 non-HOLD)
    counts = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now))
    assert counts["scan_fired"] == 1
    assert len(pushed) == 1 and pushed[0][0] == 111 and "BTC" in pushed[0][1]
    assert get_quota_state(tmp_db, 111).used == 3

    # same 15m bucket → not due (idempotent)
    c2 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 60))
    assert c2["scan_due"] == 0

    # TG-SCANWATCH-TF-CADENCE-W1: NEXT 15m bucket, SAME actionable set → content-dedup
    # (no re-send, no re-charge — timely, not spammy).
    c3 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 900))
    assert c3["scan_fired"] == 0 and c3["scan_skipped_dup"] == 1
    assert len(pushed) == 1  # still just the first push
    assert get_quota_state(tmp_db, 111).used == 3  # no extra charge


def test_producer_5m_cadence_and_refires_on_changed_set(tmp_db, monkeypatch):
    # A 5m scanwatch is due every 5m (TF-cadence, NOT hourly); a CHANGED actionable set re-fires.
    tmp_db.upsert_subscriber(112, "u", "en")
    tmp_db.add_scan_watch(112, 20, "5m", "BINANCE", "1h")
    pushed: list = []
    _mock_engine(monkeypatch, _result(THREE), pushed)
    now = 1_700_002_800  # aligned to 5m

    c1 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now))
    assert c1["scan_fired"] == 1  # 5m sub fires this 5m bucket
    # next 5m bucket (+300s), CHANGED set → re-fires (SOL→XRP)
    changed = [
        THREE[0], THREE[1],
        {"coin": "XRP", "timeframe": "5m", "exchange": "BINANCE", "call": "BUY", "confidence": 60, "regime": "TRENDING_UP"},
    ]
    _mock_engine(monkeypatch, _result(changed), pushed)
    c2 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 300))
    assert c2["scan_fired"] == 1 and len(pushed) == 2


def test_producer_shares_one_scan_across_same_param_subs(tmp_db, monkeypatch):
    for cid in (201, 202):
        tmp_db.upsert_subscriber(cid, "u", "en")
        tmp_db.add_scan_watch(cid, 20, "15m", "BINANCE", "1h")
    seen: dict[str, int] = {}

    class _CountingMcp(_FakeMcp):
        def call_tool(self, name, args):
            seen[name] = seen.get(name, 0) + 1
            return _result(THREE) if name == "scan_trade_calls" else {}

    monkeypatch.setattr(alert_engine, "McpClient", lambda cfg: _CountingMcp(_result(THREE)))
    monkeypatch.setattr(alert_engine, "Bot", lambda token: object())

    async def fake_push(*a, **k):
        return True

    monkeypatch.setattr(alert_engine, "_push", fake_push)
    counts = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=1_700_003_600))
    assert seen["scan_trade_calls"] == 1  # ONE shared scan across both subs
    # SCAN-DIGEST-MCP-PARITY-W1 CH3: the per-coin get_trade_call depth re-derivation is
    # RETIRED — the enriched scan IS the digest, so ZERO get_trade_call calls (was len(THREE)).
    assert seen.get("get_trade_call", 0) == 0
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
    # BOT-QUOTA-REFUSAL-SEAM-W1: exhausted → still no DIGEST and no charge, but the
    # owner is now TOLD. This assertion used to read `pushed == []`, which pinned the
    # defect: it made "the user hears nothing" the documented contract, and the lane
    # honoured it for two subscribers across ~10,000 refusals.
    assert len(pushed) == 1, "walled owner must receive exactly one refusal notice"
    chat, body = pushed[0]
    assert chat == 222
    assert "alerts" in body, "notice must state the BOT's unit, not the API's 'calls'"
    assert "upgrade" in body.lower()
    # No charge: the refusal must not consume quota (it is not a delivery).
    assert tmp_db.get_subscriber(222)["alert_count"] == FREE_TIER_MONTHLY_QUOTA
    # Bucket advanced → not due on the next tick in the same bucket (no re-scan storm).
    c2 = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=now + 60))
    assert c2["scan_due"] == 0


def test_producer_walled_owner_is_told_once_per_window(tmp_db, monkeypatch):
    """The episode is announced ONCE, not once per cycle.

    A walled user stays walled for up to 30 days at a 1-minute dispatch cadence.
    Announcing on every refusal would be ~43,000 messages per window; announcing on
    none is the bug this wave fixed. Exactly one, re-armed by the next window.
    """
    tmp_db.upsert_subscriber(223, "u", "en")
    tmp_db.add_scan_watch(223, 20, "15m", "BINANCE", "1h")
    with tmp_db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET alert_count=?, alerts_window_start=? WHERE chat_id=?",
            (FREE_TIER_MONTHLY_QUOTA, datetime.now(timezone.utc).isoformat(), 223),
        )
    pushed: list = []
    _mock_engine(monkeypatch, _result(THREE), pushed)

    for i in range(4):
        asyncio.run(
            alert_engine.process_scan_digests(
                "tok", tmp_db.path, "http://x/mcp", "key", now_epoch=1_700_003_600 + i * 900
            )
        )
    assert len(pushed) == 1, f"expected 1 notice across 4 refusals, got {len(pushed)}"


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


class _ScanOnlyMcp:
    """SCAN-DIGEST-MCP-PARITY-W1 CH3 strict mock: scan_trade_calls returns the enriched
    result; get_trade_call MUST NOT be called (the per-coin depth re-derivation is retired)."""

    def __init__(self, result: dict) -> None:
        self._r = result
        self.scan_args: dict | None = None

    def __enter__(self) -> "_ScanOnlyMcp":
        return self

    def __exit__(self, *a) -> bool:
        return False

    def call_tool(self, name: str, args: dict) -> dict:
        if name == "get_trade_call":
            raise AssertionError("CH3: process_scan_digests must NOT call get_trade_call (re-derivation retired)")
        self.scan_args = args
        return self._r


def test_producer_renders_from_the_enriched_scan_no_depth_call(tmp_db, monkeypatch):
    """CH3: the scan IS the digest — price/drivers/reasoning come straight from the
    enriched scan payload (includeReasoning:true), NO per-coin get_trade_call depth call.
    OI driver carries its (window); proof/disclaimer/unscanwatch stay removed; metering
    preserved (1 actionable → 1 unit)."""
    enriched_call = {
        "coin": "BTC", "timeframe": "15m", "exchange": "BINANCE", "call": "BUY",
        "confidence": 80, "regime": "TRENDING_UP",
        "price": 73.23,
        "factors": [
            {"factor": "oi_change_pct", "direction": "neutral", "value": "+1.6%"},
            {"factor": "trend_persistence", "direction": "neutral", "value": "HIGH"},
            {"factor": "funding_state", "direction": "neutral", "value": "NORMAL"},
        ],
        "reasoning": "Trending up, momentum building. Breakout pending.",
        "oi_change_window": "24h",
    }
    scan = {
        **_result([enriched_call]),  # 1 actionable: BTC BUY conf 80 (already enriched)
        "_receipts": {
            "track_record": {"pfe_win_rate": 0.9165, "n": 259295},
            "verification_uri": "https://algovault.com/track-record",
        },
    }
    tmp_db.upsert_subscriber(444, "u", "en")
    tmp_db.add_scan_watch(444, 20, "15m", "BINANCE", "1h")
    pushed: list = []
    mcp = _ScanOnlyMcp(scan)
    monkeypatch.setattr(alert_engine, "McpClient", lambda cfg: mcp)
    monkeypatch.setattr(alert_engine, "Bot", lambda token: object())

    async def fake_push(bot, chat_id, text, db=None):  # noqa: ANN001
        pushed.append((chat_id, text))
        return True

    monkeypatch.setattr(alert_engine, "_push", fake_push)
    counts = asyncio.run(alert_engine.process_scan_digests("tok", tmp_db.path, "http://x/mcp", "key", now_epoch=1_700_003_600))

    assert counts["scan_fired"] == 1
    # CH3: the scan was requested ENRICHED (so calls[] carry the digest fields).
    assert mcp.scan_args is not None and mcp.scan_args.get("includeReasoning") is True
    text = pushed[0][1]
    assert "BTC — BUY @ $73.23 · 80% conviction · TRENDING_UP" in text
    # OPS-RECEIPTS-FACTORS-DIRECTION-FIX-W1 R3 — RE-BASELINED, Class 1: arrow gone, signed figure remains.
    assert "OI +1.6% (24h)" in text  # drivers from the enriched scan payload (+ window)
    assert "trend persistence HIGH" in text and "funding normal" in text
    assert "💡 Trending up, momentum building" in text  # one-line why (first sentence)
    assert text.startswith("🚀")  # rocket header
    assert "PFE win-rate" not in text and "track-record" not in text  # proof line stays removed
    assert "Not financial advice" not in text  # disclaimer stays removed
    assert "/unscanwatch" not in text  # in-digest management hint stays removed
    # Metering preserved: 1 actionable call → 1 unit (no change from dropping depth calls).
    assert get_quota_state(tmp_db, 444).used == 1
