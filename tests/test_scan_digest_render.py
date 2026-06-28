"""SCAN-DIGEST-MCP-PARITY-W1 CH3 — render_scan_digest_line (bot side).

A faithful Python MIRROR of the MCP ``src/lib/scan-digest.ts::renderScanDigestLine``.
Both /scan and /scanwatch render each actionable call through THIS one function
(single-derivation), and the CH4 canary pins it byte-identical to the TS SoT using
the SAME fixture used here (CL BUY @ $71.49).
"""
from __future__ import annotations

from algovault_bot.scan_digest import render_scan_digest_line

# The shared fixture (identical to tests/unit/scan-digest-enrich.test.ts).
CALL = {
    "coin": "CL",
    "call": "BUY",
    "confidence": 60,
    "regime": "TRENDING_UP",
    "price": 71.49,
    "factors": [
        {"factor": "trend_persistence", "direction": "neutral", "value": "HIGH"},
        {"factor": "funding_state", "direction": "bullish", "value": "ELEVATED"},
        {"factor": "oi_change_pct", "direction": "bullish", "value": "+10.0%"},
    ],
    "reasoning": "Trending regime, upward bias. Funding pressure mild.",
    "oi_change_window": "24h",
}

EXPECTED = (
    "🟢 CL — BUY @ $71.49 · 60% conviction · TRENDING_UP\n"
    "   📊 trend persistence HIGH · funding elevated ↑ · OI +10.0% (24h) ↑\n"
    "   💡 Trending regime, upward bias"
)


def test_renders_the_spec_example_block():
    assert render_scan_digest_line(CALL) == EXPECTED


def test_sell_renders_red_marker():
    sell = {**CALL, "call": "SELL", "regime": "TRENDING_DOWN"}
    assert render_scan_digest_line(sell).split("\n")[0] == "🔴 CL — SELL @ $71.49 · 60% conviction · TRENDING_DOWN"


def test_price_format_mirrors_fmt_price():
    assert "@ $71,000" in render_scan_digest_line({**CALL, "price": 71000}).split("\n")[0]
    assert "@ $0.0123" in render_scan_digest_line({**CALL, "price": 0.0123}).split("\n")[0]
    assert "@ $2.00" in render_scan_digest_line({**CALL, "price": 2}).split("\n")[0]


def test_omits_clauses_when_absent():
    bare = {"coin": "X", "call": "BUY", "confidence": 40, "regime": "RANGING"}
    line = render_scan_digest_line(bare)
    assert line == "🟢 X — BUY · 40% conviction · RANGING"
    assert "📊" not in line and "💡" not in line and "@ $" not in line


def test_first_sentence_only_for_why():
    c = {**CALL, "reasoning": "Trending regime, upward bias. Funding mild. Extra."}
    why = next(ln for ln in render_scan_digest_line(c).split("\n") if "💡" in ln)
    assert why == "   💡 Trending regime, upward bias"


def test_arrows_bearish_and_window():
    c = {
        **CALL,
        "factors": [
            {"factor": "trend_persistence", "direction": "neutral", "value": "LOW"},
            {"factor": "funding_state", "direction": "bearish", "value": "ELEVATED"},
            {"factor": "oi_change_pct", "direction": "bearish", "value": "-3.2%"},
        ],
    }
    drivers = next(ln for ln in render_scan_digest_line(c).split("\n") if "📊" in ln)
    assert drivers == "   📊 trend persistence LOW · funding elevated ↓ · OI -3.2% (24h) ↓"
