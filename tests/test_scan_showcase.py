"""TG-WATCH-ADOPTION-BROADCAST-W1 (R3 + A3 + A4): scan-showcase aggregation,
live-derived counts, render, and suppress-on-empty."""
from __future__ import annotations

from algovault_bot import adoption


def _venue_result(scanned, calls):
    return {"scanned": scanned, "eligible_non_hold": len(calls), "calls": calls}


def test_fetch_aggregates_dedupes_by_coin_keeps_highest_confidence():
    # BTC appears on two venues — the higher-confidence (BYBIT 90) must win.
    data = {
        "BINANCE": _venue_result(100, [
            {"coin": "BTC", "call": "BUY", "confidence": 80, "regime": "TRENDING_UP"},
            {"coin": "ETH", "call": "SELL", "confidence": 70, "regime": "TRENDING_DOWN"},
        ]),
        "BYBIT": _venue_result(90, [
            {"coin": "BTC", "call": "BUY", "confidence": 90, "regime": "TRENDING_UP"},
            {"coin": "SOL", "call": "BUY", "confidence": 65, "regime": "TRENDING_UP"},
        ]),
    }

    def fake_scan(top_n, tf, venue):
        return data[venue]

    top3, asset_count, venue_count = adoption.fetch_showcase_setups(
        venues=["BINANCE", "BYBIT"], scan_fn=fake_scan
    )
    assert asset_count == 190  # 100 + 90 — LIVE-derived, summed
    assert venue_count == 2
    coins = [s["coin"] for s in top3]
    assert coins == ["BTC", "ETH", "SOL"]  # sorted by confidence desc
    btc = next(s for s in top3 if s["coin"] == "BTC")
    assert btc["confidence"] == 90 and btc["exchange"] == "BYBIT"


def test_fetch_caps_at_top_3():
    calls = [{"coin": c, "call": "BUY", "confidence": i, "regime": "TRENDING_UP"}
             for i, c in enumerate(["A", "B", "C", "D", "E"], start=60)]

    def fake_scan(top_n, tf, venue):
        return _venue_result(50, calls)

    top3, _, _ = adoption.fetch_showcase_setups(venues=["BINANCE"], scan_fn=fake_scan)
    assert len(top3) == 3
    assert [s["coin"] for s in top3] == ["E", "D", "C"]  # highest confidence first


def test_fetch_tolerates_a_failing_venue():
    def fake_scan(top_n, tf, venue):
        if venue == "OKX":
            raise RuntimeError("venue down")
        return _venue_result(50, [{"coin": "BTC", "call": "BUY", "confidence": 80, "regime": "X"}])

    top3, asset_count, venue_count = adoption.fetch_showcase_setups(
        venues=["BINANCE", "OKX"], scan_fn=fake_scan
    )
    assert venue_count == 1 and asset_count == 50  # OKX skipped, not aborted
    assert len(top3) == 1


def test_render_has_live_counts_and_button_inputs():
    top3 = [
        {"coin": "BTC", "call": "BUY", "confidence": 90, "exchange": "BYBIT"},
        {"coin": "ETH", "call": "SELL", "confidence": 70, "exchange": "BINANCE"},
    ]
    body = adoption.render_scan_showcase(top3, asset_count=290, venue_count=5)
    assert "290 assets across 5 venues" in body
    assert "BTC BUY · 90% (BYBIT)" in body
    assert "/scanwatch" in body


def test_render_empty_returns_none_for_suppression():
    assert adoption.render_scan_showcase([], 0, 0) is None
    # And fetch over a venue with no non-HOLD calls yields empty → suppress.
    top3, _, _ = adoption.fetch_showcase_setups(
        venues=["BINANCE"],
        scan_fn=lambda n, tf, v: _venue_result(50, [{"coin": "X", "call": "HOLD", "confidence": 99}]),
    )
    assert top3 == []
