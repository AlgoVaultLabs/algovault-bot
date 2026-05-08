from __future__ import annotations

import pytest

from algovault_bot.validators import (
    DEFAULT_ALERT_TYPE,
    DEFAULT_EXCHANGE,
    TF_SECONDS,
    ValidationError,
    normalize_alert_type,
    normalize_coin,
    normalize_exchange,
    normalize_timeframe,
)


# coin

@pytest.mark.parametrize("raw,expected", [("btc", "BTC"), ("ETH", "ETH"), ("Sol", "SOL"), ("1000PEPE", "1000PEPE")])
def test_coin_valid(raw: str, expected: str) -> None:
    assert normalize_coin(raw) == expected


@pytest.mark.parametrize("raw", ["B", "x", "TOOLONGCOIN1", "BTC-USD", "btc usd"])
def test_coin_invalid(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_coin(raw)


# timeframe

@pytest.mark.parametrize("raw,expected", [("1m", "1m"), ("4H", "4h"), ("1D", "1d"), ("12h", "12h")])
def test_timeframe_valid(raw: str, expected: str) -> None:
    assert normalize_timeframe(raw) == expected


@pytest.mark.parametrize("raw", ["2m", "5h", "1w", "30s", "abc"])
def test_timeframe_invalid(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_timeframe(raw)


def test_timeframe_seconds_map_complete() -> None:
    # Spec C3 line 248 lists the 11 TFs verbatim — all must be in TF_SECONDS.
    assert set(TF_SECONDS.keys()) == {
        "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"
    }
    assert TF_SECONDS["1m"] == 60
    assert TF_SECONDS["1h"] == 3600
    assert TF_SECONDS["1d"] == 86400


# exchange

@pytest.mark.parametrize("raw,expected", [
    (None, "BINANCE"),       # D8 inline-fix: default BINANCE (NOT HL)
    ("", "BINANCE"),
    ("hl", "HL"),
    ("BINANCE", "BINANCE"),
    ("Bybit", "BYBIT"),
    ("okx", "OKX"),
    ("BITGET", "BITGET"),
])
def test_exchange_valid(raw: str | None, expected: str) -> None:
    assert normalize_exchange(raw) == expected


@pytest.mark.parametrize("raw", ["KRAKEN", "coinbase", "ftx", "huobi"])
def test_exchange_invalid(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_exchange(raw)


def test_default_exchange_is_binance() -> None:
    # D8 inline-fix proof — REGIME-BOT-W1 P5 found HL upstream rate-limit hot.
    assert DEFAULT_EXCHANGE == "BINANCE"


# alert_type

@pytest.mark.parametrize("raw,expected", [
    (None, "calls"), ("", "calls"), ("regime", "regime"), ("CALLS", "calls"), ("Both", "both")
])
def test_alert_type_valid(raw: str | None, expected: str) -> None:
    assert normalize_alert_type(raw) == expected


@pytest.mark.parametrize("raw", ["all", "buy", "sell", "regime+calls"])
def test_alert_type_invalid(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_alert_type(raw)


def test_default_alert_type_is_calls() -> None:
    assert DEFAULT_ALERT_TYPE == "calls"
