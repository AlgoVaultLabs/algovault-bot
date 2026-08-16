"""TG-ALERT-VERDICT-CAPTION-W1 — glanceable verdict caption line.

Unit tests for the pure ``format_verdict_caption_line`` re-projection helper
and the ``compose_caption`` composer that prepends the verdict above any
existing quota-CTA. No I/O — these run without a Bot, DB, or MCP server.
"""

from __future__ import annotations

import pytest

from algovault_bot.caption import compose_caption, format_verdict_caption_line


# ── AC1 / AC2: exact-string verdict lines ──────────────────────


def test_buy_verdict_line_exact() -> None:
    # AC1
    assert (
        format_verdict_caption_line("LTC", "15m", "BUY", 76, "BINANCE")
        == "LTC 15min Buy 76% Binance"
    )


def test_sell_verdict_line_exact() -> None:
    # AC2
    assert (
        format_verdict_caption_line("BTC", "4h", "SELL", 81, "HL")
        == "BTC 4h Sell 81% Hyperliquid"
    )


# ── Timeframe display mapping (each TF class) ──────────────────


@pytest.mark.parametrize(
    ("tf", "expected_tf"),
    [
        ("1m", "1min"),
        ("3m", "3min"),
        ("5m", "5min"),
        ("15m", "15min"),
        ("30m", "30min"),
    ],
)
def test_minute_timeframes_render_min_suffix(tf: str, expected_tf: str) -> None:
    line = format_verdict_caption_line("BTC", tf, "BUY", 70, "BINANCE")
    assert line == f"BTC {expected_tf} Buy 70% Binance"


@pytest.mark.parametrize("tf", ["1h", "2h", "4h", "8h", "12h"])
def test_hour_timeframes_unchanged(tf: str) -> None:
    line = format_verdict_caption_line("ETH", tf, "SELL", 65, "BYBIT")
    assert line == f"ETH {tf} Sell 65% Bybit"


def test_day_timeframe_unchanged() -> None:
    assert (
        format_verdict_caption_line("SOL", "1d", "BUY", 90, "OKX")
        == "SOL 1d Buy 90% OKX"
    )


# ── Exchange display mapping (all 5) ───────────────────────────


@pytest.mark.parametrize(
    ("exchange", "expected_exchange"),
    [
        ("BINANCE", "Binance"),
        ("BYBIT", "Bybit"),
        ("OKX", "OKX"),
        ("BITGET", "Bitget"),
        ("HL", "Hyperliquid"),
    ],
)
def test_all_five_exchanges_display_mapping(exchange: str, expected_exchange: str) -> None:
    line = format_verdict_caption_line("ADA", "1h", "BUY", 55, exchange)
    assert line == f"ADA 1h Buy 55% {expected_exchange}"


# ── Direction display mapping ──────────────────────────────────


def test_direction_buy_maps_to_titlecase() -> None:
    assert format_verdict_caption_line("XRP", "5m", "BUY", 60, "OKX") == "XRP 5min Buy 60% OKX"


def test_direction_sell_maps_to_titlecase() -> None:
    assert format_verdict_caption_line("XRP", "5m", "SELL", 60, "OKX") == "XRP 5min Sell 60% OKX"


# ── Confidence rendering ───────────────────────────────────────


@pytest.mark.parametrize("confidence", [0, 5, 50, 99, 100])
def test_confidence_renders_with_percent(confidence: int) -> None:
    line = format_verdict_caption_line("BTC", "1h", "BUY", confidence, "BINANCE")
    assert line == f"BTC 1h Buy {confidence}% Binance"


# ── compose_caption: verdict is always line 1 ──────────────────


def test_verdict_line_is_first_line_when_cta_present() -> None:
    # AC3: verdict line 1, CTA below (e.g. a quota_90 nudge).
    verdict = format_verdict_caption_line("LTC", "15m", "BUY", 76, "BINANCE")
    cta = (
        "🔥 Only 5 free alerts left. Upgrade now to keep getting them:\n"
        "→ https://api.algovault.com/signup?utm_campaign=quota_90"
    )
    caption = compose_caption(verdict, cta)
    assert caption.split("\n")[0] == "LTC 15min Buy 76% Binance"
    assert cta in caption
    assert caption == f"{verdict}\n{cta}"


def test_verdict_line_stands_alone_when_no_cta() -> None:
    verdict = format_verdict_caption_line("BTC", "4h", "SELL", 81, "HL")
    assert compose_caption(verdict, None) == "BTC 4h Sell 81% Hyperliquid"
    # An empty-string CTA is treated as "no CTA" (the send path passes
    # ``cta or None``; an empty quota CTA collapses to None upstream).
    assert compose_caption(verdict, "") == "BTC 4h Sell 81% Hyperliquid"
