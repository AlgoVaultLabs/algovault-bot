"""SIGNAL-CLOSEDBAR-FLIP-W1 CH3 — 1m is on-demand-valid and push-invalid.

The engine scores on CONFIRMED bars and the dispatcher ticks every 60s, so a 60s bar has
exactly ONE dispatch opportunity and the close-grace cannot be honoured. That makes 1m a
structurally invalid PUSH timeframe while remaining a perfectly good ON-DEMAND one.

These tests pin BOTH halves. Pinning only the rejection would let a later wave "simplify"
by dropping 1m from TIMEFRAMES outright, silently breaking `/call BTC 1m`.
"""
from __future__ import annotations

import pytest

from algovault_bot import batch, handlers, keyboards
from algovault_bot.validators import (
    PUSH_TIMEFRAMES,
    TIMEFRAMES,
    ValidationError,
    normalize_push_timeframe,
    normalize_timeframe,
    smallest_push_timeframe,
)


# ── the two sets ────────────────────────────────────────────────────────────────

def test_push_set_is_the_on_demand_set_minus_1m() -> None:
    assert PUSH_TIMEFRAMES == TIMEFRAMES - {"1m"}
    assert "1m" in TIMEFRAMES, "1m must stay ON-DEMAND valid"
    assert "1m" not in PUSH_TIMEFRAMES
    assert "3m" in PUSH_TIMEFRAMES, "3m is the push floor"


def test_smallest_push_timeframe_is_derived_not_hardcoded() -> None:
    # Named in the user-facing rejection message; must follow the set, not a literal.
    assert smallest_push_timeframe() == "3m"


# ── validators ──────────────────────────────────────────────────────────────────

def test_on_demand_validator_still_accepts_1m() -> None:
    assert normalize_timeframe("1m") == "1m"
    assert normalize_timeframe(" 1M ") == "1m"


def test_push_validator_rejects_1m_with_an_explanatory_message() -> None:
    with pytest.raises(ValidationError) as e:
        normalize_push_timeframe("1m")
    msg = str(e.value)
    # It must explain WHY and name the alternative — a bare "invalid timeframe" would read
    # as a typo report for a timeframe the user can plainly see in /help.
    assert "1m" in msg
    assert "3m" in msg, "must name the push floor as the alternative"
    assert "/call" in msg, "must point at the on-demand path that still works"


def test_push_validator_accepts_every_other_timeframe() -> None:
    for tf in sorted(PUSH_TIMEFRAMES):
        assert normalize_push_timeframe(tf) == tf


def test_push_validator_still_rejects_genuine_nonsense() -> None:
    with pytest.raises(ValidationError):
        normalize_push_timeframe("5h")


# ── batch (/watch) ──────────────────────────────────────────────────────────────

def test_batch_all_excludes_1m() -> None:
    assert "1m" not in batch.parse_timeframes("all")
    assert batch.parse_timeframes("all") == list(batch.PUSH_TF_ORDER)


def test_batch_explicit_1m_is_rejected() -> None:
    with pytest.raises(ValidationError):
        batch.parse_timeframes("1m")
    with pytest.raises(ValidationError):
        batch.parse_timeframes("3m, 1m")  # rejected even alongside a valid one


# ── keyboards: the watch wizard hides 1m, the shared scan wizard does not ───────

def _tf_callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row if ":tf:" in (b.callback_data or "")]


def test_watch_wizard_grid_hides_1m() -> None:
    cbs = _tf_callbacks(keyboards.tf_grid_kb("wz", push_only=True))
    assert "wz:tf:1m" not in cbs
    assert "wz:tf:3m" in cbs
    assert len(cbs) == len(PUSH_TIMEFRAMES)


def test_scan_wizard_grid_keeps_1m_because_it_also_serves_on_demand_scan() -> None:
    # The scan wizard is entered by BOTH /scan (one-shot, 1m genuinely supported) and
    # /scanwatch (push). Hiding 1m there would remove it from the /scan half too.
    cbs = _tf_callbacks(keyboards.tf_grid_kb("scn"))
    assert "scn:tf:1m" in cbs
    assert len(cbs) == len(TIMEFRAMES)


# ── handlers: push refuses, on-demand and REMOVAL still accept ──────────────────

def test_scanwatch_push_parse_rejects_1m() -> None:
    with pytest.raises(ValidationError):
        handlers._parse_scanwatch_args(["20", "1m", "BINANCE"])


def test_on_demand_scan_parse_still_accepts_1m() -> None:
    # /scan is a one-shot answer — the freshness contract is "as of now", which 1m meets.
    _, tf, _, _ = handlers._parse_scan_args(["20", "1m", "BINANCE"])
    assert tf == "1m"


def test_unwatch_can_still_remove_an_existing_1m_row(tmp_db) -> None:
    # Removal must accept any timeframe that could ever have been added, or the rows this
    # very wave retires would be undeletable by their owner.
    # Seed a legal watch first so the subscriber row exists (watchlists has an FK), then
    # insert the 1m row directly — which is exactly how the 2 live rows got there before
    # this wave closed the door.
    handlers.handle_watch(tmp_db, 1, "u", "en", ["ETH", "3m", "BINANCE"])
    tmp_db.add_watch(1, "BTC", "1m", "BINANCE", "calls")
    assert tmp_db.count_watches(1) == 2

    reply = handlers.handle_unwatch(tmp_db, 1, "u", "en", ["BTC", "1m", "BINANCE"])
    assert "🗑️" in reply
    assert tmp_db.count_watches(1) == 1  # the 1m row is gone, the legal one remains
