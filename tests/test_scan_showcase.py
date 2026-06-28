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


def _enriched_call(coin, call, conf, regime, exchange, **extra):
    return {
        "coin": coin, "call": call, "confidence": conf, "regime": regime,
        "exchange": exchange, **extra,
    }


def test_fetch_preserves_enriched_fields_for_canonical_render():
    # OPS-SCAN-SHOWCASE-ENRICH-W1: the enriched scan fields (price/factors/reasoning/
    # oi_change_window) MUST survive aggregation so the showcase can render the
    # canonical digest line. The old stripped 6-field row would have dropped them.
    enriched = _venue_result(50, [{
        "coin": "BTC", "call": "BUY", "confidence": 80, "regime": "TRENDING_UP",
        "price": 71000,
        "factors": [{"factor": "funding_state", "direction": "bullish", "value": "ELEVATED"}],
        "reasoning": "Strong uptrend.", "oi_change_window": "24h",
    }])
    top3, _, _ = adoption.fetch_showcase_setups(
        venues=["BINANCE"], scan_fn=lambda n, tf, v: enriched
    )
    row = top3[0]
    assert row["price"] == 71000
    assert row["factors"] and row["reasoning"] == "Strong uptrend."
    assert row["oi_change_window"] == "24h"
    assert row["exchange"] == "BINANCE"  # winning venue stamped
    assert row["confidence"] == 80  # raw (int) kept for render parity, not float-coerced


def test_render_preserves_framing_and_projects_canonical_lines():
    # OPS-SCAN-SHOWCASE-ENRICH-W1: framing (live counts + CTA) preserved; the per-call
    # lines now project render_scan_digest_line — the SAME render /scan + /scanwatch use.
    from algovault_bot.scan_digest import render_scan_digest_line

    top3 = [
        _enriched_call(
            "CL", "BUY", 60, "TRENDING_UP", "BINANCE", price=71.49,
            factors=[
                {"factor": "trend_persistence", "direction": "neutral", "value": "HIGH"},
                {"factor": "funding_state", "direction": "bullish", "value": "ELEVATED"},
            ],
            reasoning="Trending regime, upward bias. Funding mild.",
            oi_change_window="24h",
        ),
        _enriched_call("ETH", "SELL", 55, "TRENDING_DOWN", "BYBIT",
                       price=2450.0, factors=[], reasoning=""),
    ]
    body = adoption.render_scan_showcase(top3, asset_count=290, venue_count=5)
    # Framing PRESERVED (Req 2).
    assert "📡 This week I scanned 290 assets across 5 venues." in body
    assert "Top fresh setups:" in body
    assert "Want this on your coins automatically? Set a standing scan: /scanwatch." in body
    # PARITY (Req 1 + Req 4): every per-call line IS the canonical render — byte-identical,
    # closing the residual the SCAN-DIGEST canary previously excluded.
    for s in top3:
        assert render_scan_digest_line(s) in body
    # The legacy bespoke line format is GONE — no venue suffix, no "· NN% (" shorthand.
    assert "(BINANCE)" not in body and "(BYBIT)" not in body
    assert "BUY · 60% (" not in body


def test_render_empty_returns_none_for_suppression():
    assert adoption.render_scan_showcase([], 0, 0) is None
    # And fetch over a venue with no non-HOLD calls yields empty → suppress.
    top3, _, _ = adoption.fetch_showcase_setups(
        venues=["BINANCE"],
        scan_fn=lambda n, tf, v: _venue_result(50, [{"coin": "X", "call": "HOLD", "confidence": 99}]),
    )
    assert top3 == []
