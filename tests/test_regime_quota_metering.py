"""QUOTA-CONSISTENCY-COUNT-ALL-W1 R1 — regime alerts now meter the shared
100/mo free quota (parity with signal-MCP, which meters ``get_market_regime``),
while HOLD trade calls stay free and paid-linked users remain a no-op.

These exercise the REAL ``process_one_row`` fire path with the Telegram push
seam (``_push`` / ``_push_photo``) monkeypatched to "delivered", and a fake
sync ``mcp.call_tool`` (mirrors ``McpClient.call_tool(name, args)``). AC1:
a fired regime alert ticks ``alert_count`` +1 for a free user; a paid-linked
user is a no-op; a HOLD trade call does not tick.

(Bot ``/scan`` + funding-arb metering is deferred — the bot has no such surface
yet; see status.md QUOTA-CONSISTENCY-COUNT-ALL-W1 deferrals.)
"""

from __future__ import annotations

from typing import Any

import pytest

from algovault_bot import alert_engine
from algovault_bot.alert_engine import WatchRow, process_one_row
from algovault_bot.db import Database
from algovault_bot.quota import get_quota_state


class _FakeMcp:
    """Sync ``call_tool`` stub mirroring ``McpClient.call_tool(name, args)``."""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._responses[name]


async def _delivered(*_args: Any, **_kwargs: Any) -> bool:
    """Stand-in for ``_push`` / ``_push_photo``: pretend Telegram delivered."""
    return True


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alert_engine, "_push", _delivered)
    monkeypatch.setattr(alert_engine, "_push_photo", _delivered)


def _regime_row(chat_id: int) -> WatchRow:
    """A regime watch primed so ONE matching regime read crosses the
    ``streak >= 2`` flap gate: ``last_verdict`` == the incoming regime →
    streak 1→2; ``regime_last_seen=None`` differs from it → fires."""
    return WatchRow(
        chat_id=chat_id, coin="BTC", timeframe="4h", exchange="BINANCE",
        alert_type="regime", regime_last_seen=None,
        last_verdict="TRENDING_UP", last_verdict_streak=1,
    )


async def test_regime_alert_consumes_quota_for_free_user(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    assert get_quota_state(tmp_db, 1).used == 0
    mcp = _FakeMcp({"get_market_regime": {"regime": "TRENDING_UP", "confidence": 70}})

    await process_one_row(None, mcp, tmp_db, _regime_row(1))

    # NEW (QUOTA-CONSISTENCY-COUNT-ALL-W1): a fired regime alert ticks +1.
    assert get_quota_state(tmp_db, 1).used == 1


async def test_regime_alert_no_op_for_paid_user(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(2, "u", "en")
    with tmp_db._cursor() as cur:
        cur.execute("UPDATE subscribers SET linked_tier = 'starter' WHERE chat_id = 2")
    mcp = _FakeMcp({"get_market_regime": {"regime": "TRENDING_UP", "confidence": 70}})

    await process_one_row(None, mcp, tmp_db, _regime_row(2))

    # PAID_TIERS bypass: consume_quota is a no-op for paid-linked users.
    state = get_quota_state(tmp_db, 2)
    assert state.is_paid
    assert state.used == 0


async def test_hold_trade_call_does_not_consume(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(3, "u", "en")
    row = WatchRow(
        chat_id=3, coin="ETH", timeframe="1h", exchange="BINANCE",
        alert_type="calls", regime_last_seen=None,
        last_verdict=None, last_verdict_streak=0,
    )
    mcp = _FakeMcp({"get_trade_call": {"call": "HOLD", "confidence": 30, "price": 3000.0}})

    await process_one_row(None, mcp, tmp_db, row)

    # HOLD trade calls stay free (silent, no tick) — unchanged by this wave.
    assert get_quota_state(tmp_db, 3).used == 0
