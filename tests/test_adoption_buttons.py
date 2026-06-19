"""TG-WATCH-ADOPTION-BROADCAST-W1 (A5): inline-button callback_data build/parse
+ keyboard construction + the 64-byte Telegram callback_data ceiling."""
from __future__ import annotations

from algovault_bot import adoption


def test_watch_callback_roundtrip_all_sources():
    for src in (adoption.SOURCE_ONBOARDING, adoption.SOURCE_DIGEST, adoption.SOURCE_SCAN_SHOWCASE):
        data = adoption.build_watch_callback("btc", "1h", "binance", src)
        assert adoption.parse_watch_callback(data) == ("BTC", "1h", "BINANCE", src)


def test_watch_callback_rejects_malformed_and_unknown_source():
    assert adoption.parse_watch_callback("wb:BTC:1h:BINANCE") is None  # too few parts
    assert adoption.parse_watch_callback("wb:BTC:1h:BINANCE:bogus") is None  # bad source
    assert adoption.parse_watch_callback("xx:BTC:1h:BINANCE:digest") is None  # wrong prefix
    assert adoption.parse_watch_callback("") is None


def test_scanwatch_callback_roundtrip_and_reject():
    assert adoption.parse_scanwatch_callback("sw:scan_showcase") == "scan_showcase"
    assert adoption.parse_scanwatch_callback("sw:bogus") is None
    assert adoption.parse_scanwatch_callback("wb:scan_showcase") is None


def test_callback_data_under_64_bytes_worst_case():
    # Worst case: long coin ticker + longest source.
    data = adoption.build_watch_callback("1000PEPE", "12h", "HYPERLIQUID", adoption.SOURCE_SCAN_SHOWCASE)
    assert len(data.encode("utf-8")) <= 64


def test_onboarding_keyboard_has_btc_and_eth_buttons():
    kb = adoption.onboarding_keyboard()
    flat = [b for row in kb.inline_keyboard for b in row]
    assert len(flat) == 2
    cbs = [adoption.parse_watch_callback(b.callback_data) for b in flat]
    assert ("BTC", "1h", "BINANCE", "onboarding") in cbs
    assert ("ETH", "4h", "BINANCE", "onboarding") in cbs


def test_scan_showcase_keyboard_carries_source():
    kb = adoption.scan_showcase_keyboard()
    btn = kb.inline_keyboard[0][0]
    assert adoption.parse_scanwatch_callback(btn.callback_data) == "scan_showcase"
