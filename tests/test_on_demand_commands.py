"""On-demand per-coin commands /regime + /call (get_market_regime / get_trade_call).

These surface two already-bot-flagged tools as one-shot pulls; the recurring side
is /watch …regime|calls. Tests monkeypatch the _regime_via_mcp / _call_via_mcp
seams so no live MCP server is needed (mirrors the /scan _scan_via_mcp seam).
"""

from __future__ import annotations

import pytest

from algovault_bot import handlers, messages
from algovault_bot.db import Database
from algovault_bot.handlers import handle_call, handle_regime, register_handlers
from algovault_bot.mcp_client import McpError
from algovault_bot.quota import FREE_TIER_MONTHLY_QUOTA, consume_quota, get_quota_state


def _exhaust(db: Database, chat_id: int) -> None:
    db.upsert_subscriber(chat_id, "u", "en")
    for _ in range(FREE_TIER_MONTHLY_QUOTA):
        consume_quota(db, chat_id)


# ── /regime ───────────────────────────────────────────────────


def test_regime_error_on_invalid_args(tmp_db: Database) -> None:
    # TG-COPY-DEFAULTS-VENUES-W1: 1 arg (missing TF) → friendly error; bare [] → default.
    reply = handle_regime(tmp_db, 1, "u", "en", ["BTC"])
    assert "couldn't read that regime check" in reply


def test_regime_success_consumes_one(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers,
        "_regime_via_mcp",
        lambda c, tf, ex: {"regime": "TRENDING_UP", "confidence": 72, "suggestion": "ride the trend"},
    )
    reply = handle_regime(tmp_db, 1, "u", "en", ["BTC", "1h", "BINANCE"])
    assert "TRENDING_UP" in reply
    assert "conf 72" in reply
    assert "ride the trend" in reply
    assert get_quota_state(tmp_db, 1).used == 1


def test_regime_coarse_grains_fine_tf(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def _fake(coin: str, tf: str, exchange: str) -> dict:
        captured["tf"] = tf
        return {"regime": "RANGING", "confidence": 50}

    monkeypatch.setattr(handlers, "_regime_via_mcp", _fake)
    handle_regime(tmp_db, 1, "u", "en", ["BTC", "5m"])
    assert captured["tf"] == "1h", "5m coarse-grains to 1h for get_market_regime"


def test_regime_exhausted_returns_upgrade(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "_regime_via_mcp", lambda c, tf, ex: {"regime": "RANGING"})
    _exhaust(tmp_db, 1)
    reply = handle_regime(tmp_db, 1, "u", "en", ["BTC", "1h"])
    assert "used all" in reply.lower()
    assert "upgrade" in reply.lower()


def test_regime_unknown_symbol_not_charged(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "_regime_via_mcp", lambda c, tf, ex: {})  # no regime key
    reply = handle_regime(tmp_db, 1, "u", "en", ["NOPE", "1h"])
    assert reply == messages.symbol_unknown_message("NOPE", "BINANCE")
    assert get_quota_state(tmp_db, 1).used == 0


def test_regime_mcp_error_graceful(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(c: str, tf: str, ex: str) -> dict:
        raise McpError("down")

    monkeypatch.setattr(handlers, "_regime_via_mcp", _boom)
    reply = handle_regime(tmp_db, 1, "u", "en", ["BTC", "1h"])
    assert "temporarily unavailable" in reply
    assert get_quota_state(tmp_db, 1).used == 0


# ── /call ─────────────────────────────────────────────────────


def test_call_hold_is_free(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers,
        "_call_via_mcp",
        lambda c, tf, ex: {"call": "HOLD", "confidence": 40, "regime": "RANGING", "price": 100.0},
    )
    reply = handle_call(tmp_db, 1, "u", "en", ["BTC", "1h"])
    assert "HOLD" in reply
    assert get_quota_state(tmp_db, 1).used == 0  # HOLD consumes no quota (the real "free")


def test_call_buy_consumes_one(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers,
        "_call_via_mcp",
        lambda c, tf, ex: {
            "call": "BUY", "confidence": 78, "regime": "TRENDING_UP",
            "price": 84250.5, "reasoning": "trend up + funding mild",
        },
    )
    reply = handle_call(tmp_db, 1, "u", "en", ["BTC", "1h"])
    assert "🟢 BUY" in reply
    assert "conf 78" in reply
    assert "$84,250.50" in reply
    assert "trend up + funding mild" in reply
    assert get_quota_state(tmp_db, 1).used == 1


def test_call_buy_when_exhausted_returns_upgrade(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handlers,
        "_call_via_mcp",
        lambda c, tf, ex: {"call": "SELL", "confidence": 70, "price": 100.0},
    )
    _exhaust(tmp_db, 1)
    reply = handle_call(tmp_db, 1, "u", "en", ["BTC", "1h"])
    assert "used all" in reply.lower()
    assert "SELL" not in reply, "the paid call is not revealed to an exhausted user"


def test_call_unknown_symbol_not_charged(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "_call_via_mcp", lambda c, tf, ex: {"call": None, "price": None})
    reply = handle_call(tmp_db, 1, "u", "en", ["NOPE", "1h"])
    assert reply == messages.symbol_unknown_message("NOPE", "BINANCE")
    assert get_quota_state(tmp_db, 1).used == 0


def test_call_mcp_error_graceful(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(c: str, tf: str, ex: str) -> dict:
        raise McpError("down")

    monkeypatch.setattr(handlers, "_call_via_mcp", _boom)
    reply = handle_call(tmp_db, 1, "u", "en", ["BTC", "1h"])
    assert "temporarily unavailable" in reply


# ── registration ──────────────────────────────────────────────


class _CapturingApp:
    def __init__(self) -> None:
        self.captured: list = []

    def add_handler(self, handler, *a, **k) -> None:  # noqa: ANN001, ANN002, ANN003
        self.captured.append(handler)


def test_regime_and_call_registered(tmp_db: Database) -> None:
    app = _CapturingApp()
    register_handlers(app, tmp_db)  # type: ignore[arg-type]
    cmds: set[str] = set()
    for h in app.captured:
        c = getattr(h, "commands", None)
        if c:
            cmds |= set(c)
    assert {"regime", "call"} <= cmds


# ── /funding (BOT-FUNDING-SOT-W1) ─────────────────────────────

_FUNDING_RESULT = {
    "opportunities": [
        {
            "coin": "BTC",
            "bestArb": {
                "longVenue": "BINANCE", "shortVenue": "OKX",
                "spreadBps": 12.3, "annualizedPct": 45.0,
                "urgency": {"label": "HIGH"},
            },
        },
        {
            "coin": "ETH",
            "bestArb": {
                "longVenue": "BYBIT", "shortVenue": "HL",
                "spreadBps": 8.0, "annualizedPct": 29.0,
                "urgency": {"label": "MEDIUM"},
            },
        },
    ],
    "scannedPairs": 80,
}


def test_funding_error_on_bad_arg(tmp_db: Database) -> None:
    # TG-COPY-DEFAULTS-VENUES-W1 (R6): invalid arg → friendly error; bare [] → top 5.
    reply = handlers.handle_funding(tmp_db, 1, "u", "en", ["banana"])
    assert "couldn't read that funding request" in reply


def test_funding_success_renders_and_charges(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "_funding_via_mcp", lambda limit, bps: _FUNDING_RESULT)
    reply = handlers.handle_funding(tmp_db, 1, "u", "en", [])
    assert "Funding arb" in reply
    assert "BTC: long BINANCE / short OKX" in reply
    assert "12.3bps" in reply
    assert "45% APR" in reply
    assert "HIGH" in reply
    assert get_quota_state(tmp_db, 1).used == 1


def test_funding_empty_still_charges(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "_funding_via_mcp", lambda limit, bps: {"opportunities": []})
    reply = handlers.handle_funding(tmp_db, 1, "u", "en", [])
    assert "No funding spreads" in reply
    assert get_quota_state(tmp_db, 1).used == 1


def test_funding_exhausted_returns_upgrade(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "_funding_via_mcp", lambda limit, bps: _FUNDING_RESULT)
    _exhaust(tmp_db, 1)
    reply = handlers.handle_funding(tmp_db, 1, "u", "en", [])
    assert "used all" in reply.lower()


def test_funding_mcp_error_graceful(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(limit: int, bps: int) -> dict:
        raise McpError("down")

    monkeypatch.setattr(handlers, "_funding_via_mcp", _boom)
    reply = handlers.handle_funding(tmp_db, 1, "u", "en", [])
    assert "temporarily unavailable" in reply
    assert get_quota_state(tmp_db, 1).used == 0
