"""BOT-ALERT-IMAGE-W1 — render_trade_call_card smoke + invariants.

These don't assert pixel-perfect output (font rendering varies by host /
font availability) — they verify:
- PNG bytes are returned (non-empty, valid PNG signature)
- Pure functions in alert_image (formatters, threshold helpers) match the
  expected output shape
- Renderer doesn't crash on minimum-viable input (None indicators)
"""

from __future__ import annotations

from algovault_bot.alert_image import (
    SeeAlsoCell,
    TradeCallView,
    _bulletize_reasoning,
    _confidence_label,
    _exchange_pretty,
    _format_funding_rate,
    _format_price,
    _format_see_also,
    _format_volume,
    render_trade_call_card,
)


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _minimal_view(**overrides) -> TradeCallView:
    base = dict(
        coin="BTC",
        timeframe="1h",
        exchange="BINANCE",
        call="BUY",
        confidence=70,
        price=80251.80,
        regime="TRENDING_UP",
        funding_rate=None,
        funding_24h_avg=None,
        funding_state=None,
        oi_change_pct=None,
        volume_24h=None,
        trend_persistence=None,
        breakout_pending=None,
        reasoning=None,
        see_also=None,
        tier_label=None,
        quota_used=10,
        quota_total=100,
    )
    base.update(overrides)
    return TradeCallView(**base)


# ── render smoke ───────────────────────────────────────────────


def test_render_returns_png_bytes() -> None:
    out = render_trade_call_card(_minimal_view())
    assert isinstance(out, bytes)
    assert out.startswith(PNG_MAGIC)
    assert len(out) > 1000  # sanity: an image of any size


def test_render_with_full_indicators_works() -> None:
    out = render_trade_call_card(_minimal_view(
        funding_rate=-0.00001994,
        funding_24h_avg=-0.00001994,
        funding_state="NORMAL",
        oi_change_pct=2.4,
        volume_24h=985_000_000,
        trend_persistence="HIGH",
        breakout_pending="IMMINENT",
        reasoning="Trending regime, upward bias. Funding pressure mild.",
    ))
    assert out.startswith(PNG_MAGIC)


def test_render_with_see_also_works() -> None:
    out = render_trade_call_card(_minimal_view(
        confidence=40,
        see_also=SeeAlsoCell(coin="ADA", timeframe="1h", confidence=82, exchange="BINANCE"),
        reasoning="Low conviction primary; better setup elsewhere.",
    ))
    assert out.startswith(PNG_MAGIC)


def test_render_paid_tier_works_for_all_tiers() -> None:
    for tier in ("Starter", "Pro", "Enterprise", "X402"):
        out = render_trade_call_card(_minimal_view(
            tier_label=tier,
            quota_used=None,
            quota_total=None,
        ))
        assert out.startswith(PNG_MAGIC), f"failed for tier={tier}"


# ── confidence labels ──────────────────────────────────────────


def test_confidence_label_brackets() -> None:
    assert _confidence_label(0) == "very low"
    assert _confidence_label(24) == "very low"
    assert _confidence_label(25) == "low"
    assert _confidence_label(49) == "low"
    assert _confidence_label(50) == "medium"
    assert _confidence_label(74) == "medium"
    assert _confidence_label(75) == "high"
    assert _confidence_label(99) == "high"


# ── price / volume / funding-rate formatting ───────────────────


def test_format_price() -> None:
    assert _format_price(80251.80) == "$80,251.80"
    assert _format_price(88.61) == "$88.61"
    assert _format_price(1.234567) == "$1.2346"
    assert _format_price(0.000123) == "$0.000123"


def test_format_volume() -> None:
    assert _format_volume(10_065_514_788.95) == "$10.07B"
    assert _format_volume(985_000_000) == "$985.00M"
    assert _format_volume(12_345) == "$12.35K"
    assert _format_volume(500) == "$500.00"


def test_format_funding_rate_signed_8_decimals() -> None:
    assert _format_funding_rate(-0.00001994) == "-0.00001994"
    assert _format_funding_rate(0.00003196) == "+0.00003196"
    assert _format_funding_rate(0) == "+0.00000000"


# ── exchange pretty-name ───────────────────────────────────────


def test_exchange_pretty_known_venues() -> None:
    assert _exchange_pretty("BINANCE") == "Binance USDT-M Futures"
    assert _exchange_pretty("BYBIT") == "Bybit USDT-M Futures"
    assert _exchange_pretty("OKX") == "OKX USDT-M Futures"
    assert _exchange_pretty("BITGET") == "Bitget USDT-M Futures"
    assert _exchange_pretty("HL") == "Hyperliquid"


def test_exchange_pretty_unknown_pass_through() -> None:
    assert _exchange_pretty("KRAKEN") == "KRAKEN"


# ── reasoning bulletization ────────────────────────────────────


def test_bulletize_reasoning_splits_on_period_space() -> None:
    out = _bulletize_reasoning(
        "Trending regime, upward bias. Funding pressure mild. Compression building."
    )
    assert out == [
        "Trending regime, upward bias",
        "Funding pressure mild",
        "Compression building",
    ]


def test_bulletize_keeps_semicolon_clauses_together() -> None:
    out = _bulletize_reasoning(
        "Funding pressure extreme; heavy one-sided crowd. Trend persistence high."
    )
    assert out == [
        "Funding pressure extreme; heavy one-sided crowd",
        "Trend persistence high",
    ]


def test_bulletize_drops_empty_fragments() -> None:
    assert _bulletize_reasoning("") == []
    assert _bulletize_reasoning(".  .  .") == []


# ── see_also formatting ────────────────────────────────────────


def test_format_see_also_with_exchange() -> None:
    cell = SeeAlsoCell(coin="ADA", timeframe="5m", confidence=82, exchange="BINANCE")
    assert _format_see_also(cell) == "ADA 5m Binance 82% confidence"


def test_format_see_also_without_exchange() -> None:
    cell = SeeAlsoCell(coin="ADA", timeframe="5m", confidence=82, exchange=None)
    assert _format_see_also(cell) == "ADA 5m 82% confidence"
