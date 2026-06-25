from __future__ import annotations

from algovault_bot.db import Database
from algovault_bot.handlers import (
    handle_help,
    handle_list,
    handle_start,
    handle_unwatch,
    handle_watch,
)


def test_start_creates_subscriber_no_default_watchlist(tmp_db: Database) -> None:
    reply = handle_start(tmp_db, 42, "alice", "en")
    assert "👋 Welcome to AlgoVault" in reply
    # The bot must NEVER auto-add a watchlist on /start (spec C1 line 106 + AC1.4 D5 fix).
    assert tmp_db.count_watches(42) == 0
    # Subscriber row created.
    assert tmp_db.get_subscriber(42) is not None


def test_help_full_command_list(tmp_db: Database) -> None:
    reply = handle_help(tmp_db, 1, "u", "en")
    for cmd in ("/start", "/watch", "/unwatch", "/list", "/help"):
        assert cmd in reply
    assert "[TYPE]" in reply  # AC2.4 — type-flag mentioned


def test_watch_default_exchange_and_type(tmp_db: Database) -> None:
    # AC2.1 with D8 (BINANCE default) + 2026-05-08 default TYPE=calls
    reply = handle_watch(tmp_db, 1, "u", "en", ["BTC", "4h"])
    # TG-BUTTON-UX-W1: single-watch confirmation is now the shared card.
    assert "You're now watching" in reply and "BTC · 4h · BINANCE · Trade calls" in reply
    assert tmp_db.count_watches(1) == 1


def test_watch_explicit_exchange_and_regime_only(tmp_db: Database) -> None:
    # AC2.2 — alert_type='regime' stored
    reply = handle_watch(tmp_db, 1, "u", "en", ["ETH", "1h", "binance", "regime"])
    assert "ETH · 1h · BINANCE · Regime" in reply
    rows = tmp_db.list_watches(1)
    assert rows[0]["alert_type"] == "regime"


def test_watch_calls_only_on_hl(tmp_db: Database) -> None:
    reply = handle_watch(tmp_db, 1, "u", "en", ["SOL", "15m", "HL", "calls"])
    assert "SOL · 15m · HL · Trade calls" in reply


def test_watch_invalid_coin(tmp_db: Database) -> None:
    reply = handle_watch(tmp_db, 1, "u", "en", ["BTC-USD", "4h"])
    assert reply.startswith("❌")


def test_watch_invalid_timeframe(tmp_db: Database) -> None:
    reply = handle_watch(tmp_db, 1, "u", "en", ["BTC", "5h"])
    assert reply.startswith("❌")


def test_watch_invalid_exchange(tmp_db: Database) -> None:
    reply = handle_watch(tmp_db, 1, "u", "en", ["BTC", "4h", "kraken"])
    assert reply.startswith("❌")


def test_watch_invalid_type(tmp_db: Database) -> None:
    reply = handle_watch(tmp_db, 1, "u", "en", ["BTC", "4h", "BINANCE", "buy"])
    assert reply.startswith("❌")


def test_watch_too_few_args_shows_usage(tmp_db: Database) -> None:
    reply = handle_watch(tmp_db, 1, "u", "en", ["BTC"])
    assert "Usage:" in reply


def test_unwatch_existing(tmp_db: Database) -> None:
    handle_watch(tmp_db, 1, "u", "en", ["BTC", "4h"])
    reply = handle_unwatch(tmp_db, 1, "u", "en", ["BTC", "4h"])
    assert "🗑️" in reply
    assert tmp_db.count_watches(1) == 0


def test_unwatch_missing(tmp_db: Database) -> None:
    reply = handle_unwatch(tmp_db, 1, "u", "en", ["BTC", "4h"])
    assert "isn't on your watchlist" in reply


def test_list_empty(tmp_db: Database) -> None:
    reply = handle_list(tmp_db, 1, "u", "en")
    assert "Your watchlist is empty" in reply


def test_list_after_watch(tmp_db: Database) -> None:
    handle_watch(tmp_db, 1, "u", "en", ["BTC", "4h"])
    handle_watch(tmp_db, 1, "u", "en", ["ETH", "1h", "BINANCE", "regime"])
    reply = handle_list(tmp_db, 1, "u", "en")
    assert "BTC 4h on BINANCE" in reply
    assert "ETH 1h on BINANCE" in reply
    assert "2 watches" in reply  # TG-BATCH-WATCHLIST-W1 — footer no longer "/50"


def test_watch_51st_accepted_cap_removed(tmp_db: Database) -> None:
    # TG-BATCH-WATCHLIST-W1 AC1.4 — the 50-combo cap is removed; 51st accepted.
    for i in range(50):
        coin = f"COIN{i:02d}"
        r = handle_watch(tmp_db, 1, "u", "en", [coin, "4h"])
        assert r.startswith("✅"), f"add #{i} failed: {r}"
    assert tmp_db.count_watches(1) == 50
    # 51st now accepted (was rejected pre-W1) — no "watchlist cap" rejection.
    reply = handle_watch(tmp_db, 1, "u", "en", ["XYZ", "4h"])
    assert reply.startswith("✅")
    assert "cap" not in reply.lower()
    assert tmp_db.count_watches(1) == 51


def test_watch_cap_does_not_apply_to_existing_entry_update(tmp_db: Database) -> None:
    # If user already has 50 entries, updating ONE of them (changing alert_type) is OK.
    for i in range(50):
        coin = f"COIN{i:02d}"
        handle_watch(tmp_db, 1, "u", "en", [coin, "4h"])
    # Re-add COIN00 with different alert_type — should NOT trigger cap.
    reply = handle_watch(tmp_db, 1, "u", "en", ["COIN00", "4h", "BINANCE", "regime"])
    assert reply.startswith("✅"), reply
    assert tmp_db.count_watches(1) == 50  # still 50, not 51


def test_watch_creates_subscriber_implicitly(tmp_db: Database) -> None:
    # New chat issuing /watch first (no /start) should still get a subscriber row.
    handle_watch(tmp_db, 999, "newuser", "en", ["BTC", "4h"])
    assert tmp_db.get_subscriber(999) is not None
