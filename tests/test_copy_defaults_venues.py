"""TG-COPY-DEFAULTS-VENUES-W1 — 12-venue resolution, /help Upgrade utm, and the
bare-command smart defaults (missing args → default; not an error)."""
from __future__ import annotations

from algovault_bot import handlers
from algovault_bot.db import Database
from algovault_bot.handlers import (
    DEFAULT_SCAN_TF,
    DEFAULT_SCAN_TOP_N,
    _parse_scan_args,
    _parse_scanwatch_args,
    handle_call,
    handle_regime,
    handle_watch,
)
from algovault_bot.messages import HELP_MESSAGE, signup_url
from algovault_bot.scan_digest import cadence_for_timeframe
from algovault_bot.validators import EXCHANGE_DISPLAY_ORDER, EXCHANGES, normalize_exchange

_ALL_12 = (
    "HL", "BINANCE", "BYBIT", "OKX", "BITGET",
    "ASTER", "BINGX", "GATE", "HTX", "KUCOIN", "MEXC", "PHEMEX",
)


# ── venue expansion (R9) ──
def test_all_12_venues_resolve() -> None:
    assert len(EXCHANGES) == 12
    assert set(EXCHANGES) == set(_ALL_12)
    assert set(EXCHANGE_DISPLAY_ORDER) == set(EXCHANGES)  # single ordered source covers the enum
    for v in _ALL_12:
        assert normalize_exchange(v) == v
        assert normalize_exchange(v.lower()) == v  # case-insensitive


def test_venue_aliases_resolve() -> None:
    assert normalize_exchange("gateio") == "GATE"
    assert normalize_exchange("gate") == "GATE"
    assert normalize_exchange("kc") == "KUCOIN"
    assert normalize_exchange("kucoin") == "KUCOIN"
    assert normalize_exchange("hyperliquid") == "HL"


# ── /help Upgrade URL byte-identical (AC1) ──
def test_help_upgrade_url_utm_byte_identical() -> None:
    assert f"Upgrade → {signup_url('help_message')}" in HELP_MESSAGE
    assert "api.algovault.com/signup?plan=starter&utm_source=tg_bot&utm_campaign=help_message" in HELP_MESSAGE


# ── bare-command defaults (R8 / AC2) ──
def test_bare_watch_subscribes_btc_1h_binance_calls(tmp_db: Database) -> None:
    reply = handle_watch(tmp_db, 1, "u", "en", [])
    assert tmp_db.count_watches(1) == 1
    row = tmp_db.list_watches(1)[0]
    assert row["coin"] == "BTC" and row["timeframe"] == "1h"
    assert row["exchange"] == "BINANCE" and row["alert_type"] == "calls"
    assert "couldn't read" not in reply  # took the default, not the error block


def test_bare_scan_parse_defaults() -> None:
    assert _parse_scan_args([]) == (DEFAULT_SCAN_TOP_N, DEFAULT_SCAN_TF, "BINANCE", None)
    assert (DEFAULT_SCAN_TOP_N, DEFAULT_SCAN_TF) == (20, "15m")


def test_bare_scanwatch_parse_defaults_hourly() -> None:
    result = _parse_scanwatch_args([])
    assert result[0] == 20 and result[1] == "15m" and result[2] == "BINANCE"
    assert cadence_for_timeframe("15m") == "1h"  # bare /scanwatch = hourly (AC5)


def test_bare_regime_uses_default_btc_binance(tmp_db: Database, monkeypatch) -> None:
    seen: list = []

    def fake(coin, timeframe, exchange):
        seen.append((coin, timeframe, exchange))
        return {"regime": "RANGING", "confidence": 30}

    monkeypatch.setattr(handlers, "_regime_via_mcp", fake)
    out = handle_regime(tmp_db, 1, "u", "en", [])
    assert seen and seen[0][0] == "BTC" and seen[0][2] == "BINANCE"
    assert "couldn't read" not in out


def test_bare_call_uses_default_btc_1h_binance(tmp_db: Database, monkeypatch) -> None:
    seen: list = []

    def fake(coin, timeframe, exchange):
        seen.append((coin, timeframe, exchange))
        return {"call": "HOLD", "confidence": 40, "regime": "RANGING", "price": 100.0}

    monkeypatch.setattr(handlers, "_call_via_mcp", fake)
    out = handle_call(tmp_db, 1, "u", "en", [])
    assert seen and seen[0] == ("BTC", "1h", "BINANCE")
    assert "couldn't read" not in out
