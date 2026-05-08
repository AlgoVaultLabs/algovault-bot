"""BOT-ALERT-IMAGE-W1 — _pick_see_also filter tests.

Filter rules:
- primary_confidence < 50 → eligible (low or very low)
- candidate.timeframe must equal alert TF
- candidate.exchange must equal alert exchange (or be missing → fall back
  to assuming same-as-alert per Path B for deploy-ordering safety)
- candidate.confidence ≥ 80
- highest-conf match wins
"""

from __future__ import annotations

from algovault_bot.alert_engine import _pick_see_also
from algovault_bot.alert_image import SeeAlsoCell


def _cell(coin: str, tf: str, conf: int, ex: str | None = "BYBIT") -> dict:
    c: dict = {"coin": coin, "timeframe": tf, "confidence": conf}
    if ex is not None:
        c["exchange"] = ex
    return c


def test_returns_none_when_primary_confidence_above_threshold() -> None:
    cells = [_cell("ADA", "5m", 90, "BYBIT")]
    assert _pick_see_also(cells, primary_confidence=50, same_tf="5m", same_exchange="BYBIT") is None
    assert _pick_see_also(cells, primary_confidence=75, same_tf="5m", same_exchange="BYBIT") is None


def test_fires_for_low_and_very_low_primary_confidence() -> None:
    cells = [_cell("ADA", "5m", 82, "BYBIT")]
    out_low = _pick_see_also(cells, primary_confidence=49, same_tf="5m", same_exchange="BYBIT")
    out_very_low = _pick_see_also(cells, primary_confidence=10, same_tf="5m", same_exchange="BYBIT")
    assert out_low is not None and out_low.coin == "ADA"
    assert out_very_low is not None and out_very_low.coin == "ADA"


def test_returns_none_for_empty_or_missing_also_see() -> None:
    assert _pick_see_also(None, primary_confidence=30, same_tf="5m", same_exchange="BYBIT") is None
    assert _pick_see_also([], primary_confidence=30, same_tf="5m", same_exchange="BYBIT") is None


def test_filters_by_timeframe() -> None:
    cells = [
        _cell("ADA", "1h", 90, "BYBIT"),  # wrong TF
        _cell("DOT", "5m", 85, "BYBIT"),  # match
    ]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert out is not None and out.coin == "DOT"


def test_filters_by_exchange() -> None:
    cells = [
        _cell("ADA", "5m", 90, "BINANCE"),  # wrong exchange
        _cell("DOT", "5m", 82, "BYBIT"),    # match
    ]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert out is not None and out.coin == "DOT"


def test_filters_by_min_confidence_80() -> None:
    cells = [
        _cell("ADA", "5m", 79, "BYBIT"),  # below floor
        _cell("DOT", "5m", 80, "BYBIT"),  # exactly at floor — passes
    ]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert out is not None and out.coin == "DOT"


def test_returns_none_when_all_below_min_confidence() -> None:
    cells = [
        _cell("ADA", "5m", 60, "BYBIT"),
        _cell("DOT", "5m", 75, "BYBIT"),
    ]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert out is None


def test_picks_highest_confidence_match() -> None:
    cells = [
        _cell("ADA", "5m", 82, "BYBIT"),
        _cell("DOT", "5m", 95, "BYBIT"),  # highest
        _cell("LINK", "5m", 88, "BYBIT"),
    ]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert out is not None
    assert out.coin == "DOT"
    assert out.confidence == 95


def test_path_b_fallback_when_exchange_missing_on_cell() -> None:
    # Pre-signal-MCP-deploy edge case: cell has no `exchange` field. Filter
    # falls back to assuming same-as-alert exchange.
    cells = [_cell("ADA", "5m", 85, ex=None)]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert out is not None
    assert out.coin == "ADA"
    assert out.exchange == "BYBIT"  # filled in from alert row


def test_returns_seealsocell_with_correct_shape() -> None:
    cells = [_cell("ADA", "5m", 82, "BYBIT")]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert isinstance(out, SeeAlsoCell)
    assert out.coin == "ADA"
    assert out.timeframe == "5m"
    assert out.confidence == 82
    assert out.exchange == "BYBIT"


def test_invalid_confidence_value_dropped() -> None:
    cells = [
        {"coin": "ADA", "timeframe": "5m", "confidence": "not-a-number", "exchange": "BYBIT"},
        _cell("DOT", "5m", 85, "BYBIT"),
    ]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert out is not None and out.coin == "DOT"


def test_missing_coin_dropped() -> None:
    cells = [
        {"timeframe": "5m", "confidence": 90, "exchange": "BYBIT"},  # no coin
        _cell("DOT", "5m", 82, "BYBIT"),
    ]
    out = _pick_see_also(cells, primary_confidence=30, same_tf="5m", same_exchange="BYBIT")
    assert out is not None and out.coin == "DOT"
