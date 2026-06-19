"""TG-WATCH-ADOPTION-BROADCAST-W1 (R2): daily-digest per-setup /watch CTA +
one-tap button keyboard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from algovault_bot import adoption

# daily-digest.py is a hyphenated script (not an importable module name) — load it.
_SPEC = importlib.util.spec_from_file_location(
    "daily_digest_mod",
    Path(__file__).resolve().parent.parent / "scripts" / "daily-digest.py",
)
daily_digest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(daily_digest)


TOP3 = [
    {"coin": "BTC", "verdict": "LONG", "confidence": 85, "spread_bps": 12, "venue_pair": "BINANCE/BYBIT"},
    {"coin": "ETH", "verdict": "SHORT", "confidence": 78, "spread_bps": -9, "venue_pair": "OKX/BITGET"},
    {"coin": "SOL", "verdict": "LONG", "confidence": 76, "spread_bps": 7, "venue_pair": "BINANCE/OKX"},
]


def test_body_has_watch_cta_per_setup():
    body = daily_digest.render_digest_body(TOP3, "2026-06-19")
    # Each of the 3 setups ends with a /watch CTA for its coin.
    assert "/watch BTC 1h" in body
    assert "/watch ETH 1h" in body
    assert "/watch SOL 1h" in body
    assert body.count("/watch") == 3


def test_digest_keyboard_one_button_per_setup_with_attribution():
    kb = adoption.digest_keyboard(TOP3)
    flat = [b for row in kb.inline_keyboard for b in row]
    assert len(flat) == 3
    parsed = [adoption.parse_watch_callback(b.callback_data) for b in flat]
    coins = [p[0] for p in parsed]
    assert coins == ["BTC", "ETH", "SOL"]
    # Every digest button is source-attributed as 'digest'.
    assert all(p[3] == adoption.SOURCE_DIGEST for p in parsed)
    # Default TF + exchange when the funding-arb setup carries none.
    assert all(p[1] == "1h" and p[2] == "BINANCE" for p in parsed)


def test_digest_keyboard_none_when_empty():
    assert adoption.digest_keyboard([]) is None


def test_body_within_char_cap():
    body = daily_digest.render_digest_body(TOP3, "2026-06-19")
    assert len(body) <= daily_digest.MAX_DIGEST_CHARS
