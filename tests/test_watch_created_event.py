"""TG-WATCH-ADOPTION-BROADCAST-W1 (R4 + A5): watch_created / scan_watch_created
event capture across typed-command + one-tap-button paths."""
from __future__ import annotations

import pytest

from algovault_bot import adoption, handlers


@pytest.fixture()
def events(monkeypatch):
    """Capture every adoption analytics emit (alerts.log JSON line) in-memory."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        adoption, "log_alert_event",
        lambda event, **fields: captured.append((event, fields)),
    )
    return captured


# ── typed-command paths ──────────────────────────────────────────────────────

def test_typed_watch_emits_watch_created_source_command(tmp_db, events):
    handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "1h"])
    assert tmp_db.count_watches(1) == 1
    wc = [f for e, f in events if e == "watch_created"]
    assert len(wc) == 1
    assert wc[0]["source"] == adoption.SOURCE_COMMAND
    assert wc[0]["coin"] == "BTC" and wc[0]["created"] is True


def test_duplicate_typed_watch_does_not_re_emit(tmp_db, events):
    handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "1h"])
    events.clear()
    handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "1h"])  # same combo → 0 inserted
    assert [e for e, _ in events if e == "watch_created"] == []


def test_typed_scanwatch_emits_scan_watch_created(tmp_db, events):
    handlers.handle_scanwatch(tmp_db, 1, "u", "en", ["25", "1h"])
    sw = [f for e, f in events if e == "scan_watch_created"]
    assert len(sw) == 1
    assert sw[0]["source"] == adoption.SOURCE_COMMAND and sw[0]["created"] is True


# ── one-tap button paths (pure tap handlers) ─────────────────────────────────

def test_watch_button_tap_creates_and_attributes_source(tmp_db, events):
    data = adoption.build_watch_callback("BTC", "1h", "BINANCE", adoption.SOURCE_DIGEST)
    toast = handlers.handle_adoption_watch_tap(tmp_db, 7, "u", "en", data)
    assert tmp_db.count_watches(7) == 1
    assert "Watching BTC 1h" in toast
    wc = [f for e, f in events if e == "watch_created"]
    assert wc and wc[0]["source"] == adoption.SOURCE_DIGEST and wc[0]["created"] is True


def test_onboarding_tap_pins_nudge_dedup_flag(tmp_db, events):
    data = adoption.build_watch_callback("BTC", "1h", "BINANCE", adoption.SOURCE_ONBOARDING)
    handlers.handle_adoption_watch_tap(tmp_db, 8, "u", "en", data)
    assert tmp_db.get_first_watch_nudge_sent_at(8) is not None  # converted → won't re-target


def test_watch_button_tap_already_watching_reports_not_created(tmp_db, events):
    data = adoption.build_watch_callback("BTC", "1h", "BINANCE", adoption.SOURCE_DIGEST)
    handlers.handle_adoption_watch_tap(tmp_db, 9, "u", "en", data)
    events.clear()
    toast = handlers.handle_adoption_watch_tap(tmp_db, 9, "u", "en", data)
    assert "already watching" in toast.lower()
    wc = [f for e, f in events if e == "watch_created"]
    assert wc and wc[0]["created"] is False  # engagement signal, not a new watch


def test_scanwatch_button_tap_creates_default(tmp_db, events):
    data = adoption.build_scanwatch_callback(adoption.SOURCE_SCAN_SHOWCASE)
    toast = handlers.handle_adoption_scanwatch_tap(tmp_db, 11, "u", "en", data)
    rows = tmp_db.list_scan_watches(11)
    assert len(rows) == 1 and rows[0]["top_n"] == 20 and rows[0]["cadence"] == "1h"
    assert "Standing scan set" in toast
    sw = [f for e, f in events if e == "scan_watch_created"]
    assert sw and sw[0]["source"] == adoption.SOURCE_SCAN_SHOWCASE


def test_malformed_callback_returns_none_no_side_effect(tmp_db, events):
    assert handlers.handle_adoption_watch_tap(tmp_db, 12, "u", "en", "wb:bogus") is None
    assert tmp_db.count_watches(12) == 0
    assert events == []
