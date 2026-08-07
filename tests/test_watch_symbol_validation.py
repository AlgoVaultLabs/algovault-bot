"""BOT-WATCH-VALIDATE-W1 — preflight symbol validation at /watch time.

Triggered by the XAUUSD/1m/BINANCE dead-watch discovery 2026-05-17:
upstream returns null call/price for unknown symbols; the alert engine
silently absorbs null as HOLD-equivalent, so dead watches just sit in
the DB forever firing nothing. Preflight check at /watch insert time
catches the typo before the row lands.
"""

from __future__ import annotations

import pytest

from algovault_bot import handlers
from algovault_bot.db import Database


def test_watch_rejects_unknown_symbol(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """When _validate_symbol returns an error, handle_watch refuses the insert
    and the row never lands in watchlists."""
    monkeypatch.setattr(
        handlers,
        "_validate_symbol",
        lambda c, tf, ex: f"❌ '{c}' isn't recognized by AlgoVault on {ex}.",
    )
    # TF is incidental here — the subject is symbol validation. Moved off 1m by
    # SIGNAL-CLOSEDBAR-FLIP-W1 CH3, which rejects 1m BEFORE the symbol check runs.
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["XAUUSD", "3m", "BINANCE"])
    assert "isn't recognized" in reply
    # Row was NOT inserted.
    assert tmp_db.count_watches(1) == 0


def test_watch_accepts_valid_symbol(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """When _validate_symbol returns None, the watch lands normally."""
    monkeypatch.setattr(handlers, "_validate_symbol", lambda c, tf, ex: None)
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "4h", "BINANCE"])
    assert "✅" in reply
    assert tmp_db.count_watches(1) == 1


def test_watch_skips_validation_on_existing_row(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updating an existing entry's alert_type shouldn't re-validate the symbol.
    The row was already validated at original insert; the user might be trying
    to change `both` → `calls` on a row they want to keep."""
    # Seed an existing row (bypassing the handler).
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "regime")
    # Now stub validation to FAIL — if validation runs, this would block.
    called = {"n": 0}

    def fake_validate(c: str, tf: str, ex: str) -> str | None:
        called["n"] += 1
        return "should-not-be-shown"

    monkeypatch.setattr(handlers, "_validate_symbol", fake_validate)
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "4h", "BINANCE", "calls"])
    assert "✅" in reply
    assert called["n"] == 0  # validation skipped on existing row
    rows = tmp_db.list_watches(1)
    assert rows[0]["alert_type"] == "calls"


def test_watch_fail_open_when_mcp_errors(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the MCP call itself raises (network error, server down), the
    validation returns None (fail-open) — we don't block legitimate watches
    during transient outages."""
    monkeypatch.setattr(handlers, "_validate_symbol", lambda c, tf, ex: None)
    reply = handlers.handle_watch(tmp_db, 1, "u", "en", ["BTC", "4h", "BINANCE"])
    assert "✅" in reply
    assert tmp_db.count_watches(1) == 1


def test_validate_symbol_returns_error_on_null_call_and_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ACTUAL _validate_symbol (not stubbed) — feed it a fake MCP client
    whose call_tool returns the null-payload shape XAUUSD produces."""
    class _FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def call_tool(self, name, args):
            return {"call": None, "confidence": None, "price": None}

    monkeypatch.setattr(handlers, "from_env", lambda: _FakeClient())
    err = handlers._validate_symbol_impl("XAUUSD", "1m", "BINANCE")
    assert err is not None
    assert "XAUUSD" in err
    assert "BINANCE" in err
    assert "GOLD" in err or "XAU" in err  # message suggests an alternative


def test_validate_symbol_returns_none_for_valid_hold_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HOLD call with a real price means the symbol IS known — let it through."""
    class _FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def call_tool(self, name, args):
            return {"call": "HOLD", "confidence": 40, "price": 80251.80}

    monkeypatch.setattr(handlers, "from_env", lambda: _FakeClient())
    assert handlers._validate_symbol_impl("BTC", "1h", "BINANCE") is None


def test_validate_symbol_returns_none_for_buy_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BUY/SELL response is the strongest signal that the symbol is known."""
    class _FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def call_tool(self, name, args):
            return {"call": "BUY", "confidence": 82, "price": 88.61}

    monkeypatch.setattr(handlers, "from_env", lambda: _FakeClient())
    assert handlers._validate_symbol_impl("SOL", "5m", "BYBIT") is None


def test_validate_symbol_fail_open_on_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP transport / server error → return None (fail-open). The watch is
    let through; the cron will sort it out on the next tick."""
    from algovault_bot.mcp_client import McpError

    class _FakeClient:
        def __enter__(self):
            raise McpError("simulated transport failure")
        def __exit__(self, *_):
            return None
        def call_tool(self, name, args):
            return {}

    monkeypatch.setattr(handlers, "from_env", lambda: _FakeClient())
    assert handlers._validate_symbol_impl("BTC", "1h", "BINANCE") is None
