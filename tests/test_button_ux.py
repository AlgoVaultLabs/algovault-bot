"""TG-BUTTON-UX-W1 / C1 — keyboard builders (pure) + confirmation card + curated commands."""
from __future__ import annotations

import pytest
from telegram import InlineKeyboardMarkup

from algovault_bot import keyboards as k
from algovault_bot import messages
from algovault_bot.handlers import CURATED_COMMANDS
from algovault_bot.validators import EXCHANGES


def _buttons(kb: InlineKeyboardMarkup):
    return [b for row in kb.inline_keyboard for b in row]


def _assert_cb_valid(kb: InlineKeyboardMarkup) -> None:
    for b in _buttons(kb):
        if b.callback_data is not None:
            assert b.callback_data.isascii(), b.callback_data
            assert len(b.callback_data.encode()) <= 64, b.callback_data


# ── keyboards ──

def test_keyboard_main_menu_shape_and_callbacks():
    kb = k.main_menu_kb()
    _assert_cb_valid(kb)
    cbs = [b.callback_data for b in _buttons(kb) if b.callback_data]
    assert "mnu:watch" in cbs and "mnu:scan" in cbs
    urls = [b.url for b in _buttons(kb) if b.url]
    assert any(u.startswith("https://") and "utm_campaign=start_welcome" in u for u in urls)
    assert all(len(row) <= 3 for row in kb.inline_keyboard)


def test_keyboard_tf_grid_excludes_1m_and_has_nav():
    kb = k.tf_grid_kb("wz")
    _assert_cb_valid(kb)
    cbs = [b.callback_data for b in _buttons(kb)]
    assert "wz:tf:1m" not in cbs  # wizard 3m floor — 1m excluded
    assert "wz:tf:3m" in cbs and "wz:tf:1d" in cbs
    assert "wz:back" in cbs and "wz:cancel" in cbs


def test_keyboard_grids_reuse_prefix_for_both_wizards():
    # ONE builder serves the watch (wz) AND scan (scn) wizards (single-derivation).
    assert any(b.callback_data == "scn:tf:15m" for b in _buttons(k.tf_grid_kb("scn")))
    assert any(b.callback_data == "scn:ex:BINANCE" for b in _buttons(k.exchange_grid_kb("scn")))


def test_keyboard_exchange_grid_covers_the_five():
    cbs = {b.callback_data for b in _buttons(k.exchange_grid_kb("wz")) if b.callback_data and ":ex:" in b.callback_data}
    assert {f"wz:ex:{e}" for e in EXCHANGES} == cbs


def test_keyboard_mode_grid_has_three_modes():
    cbs = [b.callback_data for b in _buttons(k.mode_kb("wz"))]
    for m in ("calls", "regime", "both"):
        assert f"wz:mode:{m}" in cbs


def test_keyboard_coin_grid_has_type_ticker_and_coins():
    kb = k.coin_grid_kb(["BTC", "ETH", "SOL"], "wz")
    _assert_cb_valid(kb)
    cbs = [b.callback_data for b in _buttons(kb)]
    assert "wz:coin:BTC" in cbs and "wz:type" in cbs


# ── confirmation card (shared renderer) ──

def test_confirmation_watch_card_content():
    card = messages.format_subscription_confirmation("watch", coin="BTC", tf="15m", exchange="BYBIT", mode="both")
    assert "BTC · 15m · BYBIT · Regime + Calls" in card
    assert "100/mo" in card and "/unwatch" in card
    assert "<" not in card and ">" not in card  # plain text → parse-mode safe


def test_confirmation_scanwatch_card_content():
    card = messages.format_subscription_confirmation("scanwatch", top_n=10, tf="15m", exchange="BINANCE", cadence="1h")
    assert "top 10 · 15m · BINANCE (every 1h)" in card
    assert "HOLD" in card and "/unscanwatch" in card


def test_confirmation_unknown_kind_raises():
    with pytest.raises(ValueError):
        messages.format_subscription_confirmation("bogus", tf="15m", exchange="HL")


# ── curated commands (Menu button SoT) ──

def test_commands_curated_list_covers_all_and_is_within_telegram_limits():
    names = {c.command for c in CURATED_COMMANDS}
    for expected in (
        "watch", "scan", "scanwatch", "unscanwatch", "regime", "call",
        "funding", "list", "unwatch", "unwatchall", "referral", "help",
    ):
        assert expected in names
    for c in CURATED_COMMANDS:
        assert 1 <= len(c.command) <= 32
        assert 1 <= len(c.description) <= 256
