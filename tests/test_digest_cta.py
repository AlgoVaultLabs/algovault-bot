"""TG-WATCH-ADOPTION-BROADCAST-W1 (R2): daily-digest per-setup /watch CTA +
one-tap button keyboard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from algovault_bot import adoption
from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA

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
    body = daily_digest.render_digest_body(TOP3, "2026-06-19", free_monthly=FREE_TIER_MONTHLY_QUOTA)
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
    body = daily_digest.render_digest_body(TOP3, "2026-06-19", free_monthly=FREE_TIER_MONTHLY_QUOTA)
    assert len(body) <= daily_digest.MAX_DIGEST_CHARS


# ── GROWTH-TG-FREE-ALLOWANCE-W1 — the closing CTA states the allowance it is GIVEN ─────────────


def test_closing_cta_states_the_allowance_it_is_given():
    """The sentence is the approved one; only its number comes from the ladder now."""
    body = daily_digest.render_digest_body(TOP3, "2026-06-19", free_monthly=37)
    assert body.endswith("👇 One tap to start watching · 37 free alerts/month.")
    live = daily_digest.render_digest_body(TOP3, "2026-06-19", free_monthly=FREE_TIER_MONTHLY_QUOTA)
    assert live.endswith(f"👇 One tap to start watching · {FREE_TIER_MONTHLY_QUOTA} free alerts/month.")


def test_a_rendered_digest_refuses_to_guess_the_allowance():
    with pytest.raises(ValueError):
        daily_digest.render_digest_body(TOP3, "2026-06-19", free_monthly=None)


def test_an_empty_digest_needs_no_allowance():
    body = daily_digest.render_digest_body([], "2026-06-19", free_monthly=None)
    assert body == daily_digest.render_empty_state("2026-06-19")


def test_a_suppressed_day_never_opens_the_database(monkeypatch):
    """The script runs from ROOT's crontab. Opening state.db as root on a suppressed day risks
    root-owned WAL siblings that lock the bot out, so the ladder is read only when a body will
    carry it — and every retained run so far was a suppressed day."""

    def _boom(*_a, **_k):
        raise AssertionError("Database opened on a suppressed day")

    monkeypatch.setattr(daily_digest, "fetch_top_setups", lambda *_a, **_k: [])
    monkeypatch.setattr(daily_digest, "Database", _boom)
    assert daily_digest.main(["--dry-run"]) == 0
