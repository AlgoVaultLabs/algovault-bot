"""C4 — admin /stats command tests (gated by BOT_ADMIN_CHAT_IDS env)."""

from __future__ import annotations

import os

import pytest

from algovault_bot.admin import handle_stats, is_admin, render_stats
from algovault_bot.db import Database


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reset to a clean slate; tests opt-in to admin via monkeypatch.
    monkeypatch.delenv("BOT_ADMIN_CHAT_IDS", raising=False)


def test_is_admin_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_admin(123) is False


def test_is_admin_csv_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_ADMIN_CHAT_IDS", "123, 456 , 789")
    assert is_admin(123) is True
    assert is_admin(456) is True
    assert is_admin(789) is True
    assert is_admin(999) is False


def test_is_admin_invalid_entries_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_ADMIN_CHAT_IDS", "abc, 123, , xyz")
    assert is_admin(123) is True
    assert is_admin(0) is False


# AC4.6
def test_handle_stats_non_admin_returns_not_authorized(tmp_db: Database) -> None:
    reply = handle_stats(tmp_db, 999)
    assert reply == "Not authorized."


def test_handle_stats_admin_returns_breakdown(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_ADMIN_CHAT_IDS", "777")
    # Seed some data
    tmp_db.upsert_subscriber(1, "alice", "en")
    tmp_db.upsert_subscriber(2, "bob", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "both")
    tmp_db.add_watch(2, "BTC", "1h", "BINANCE", "regime")
    tmp_db.add_watch(2, "ETH", "1h", "BINANCE", "calls")
    tmp_db.increment_total_regime_alerts(1)
    tmp_db.increment_total_call_alerts(1)
    tmp_db.increment_total_ctas_shown(1)

    reply = handle_stats(tmp_db, 777)
    # Counters
    assert "Subscribers" in reply
    assert "Watchlist entries : 3" in reply
    assert "Regime shifts   : 1" in reply
    assert "Trade calls     : 1" in reply
    assert "CTAs shown      : 1" in reply
    # Top assets
    assert "BTC" in reply
    assert "ETH" in reply
    # UTM-attribution note (D2-B / no /api/usage-stats)
    assert "Plausible" in reply
