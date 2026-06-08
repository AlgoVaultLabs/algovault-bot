"""FEATURE-PARITY-CHANNELS-W1 CH3 — the bot derives its tool-backed surface from
the MCP /capabilities projection (channels.bot) + a bot-side surface map, with a
3-tier fail-open fetch (live → committed snapshot → hardcoded surface, never empty).
"""
from __future__ import annotations

import httpx

from algovault_bot import capabilities as cap


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def raise_for_status(self) -> None:  # noqa: D401
        return None

    def json(self) -> dict:
        return self._p


def _caps(*bot_tools: str, mcp_only: tuple[str, ...] = ()) -> dict:
    """Minimal /capabilities projection: `bot_tools` are channels.bot==true."""
    tools = [
        {"name": n, "canonical": n, "channels": {"mcp": True, "httpX402": False, "bot": True, "webhook": False}}
        for n in bot_tools
    ]
    tools += [
        {"name": n, "canonical": n, "channels": {"mcp": True, "httpX402": False, "bot": False, "webhook": False}}
        for n in mcp_only
    ]
    return {"server": "x", "version": "t", "tools": tools}


def test_fetch_live_success(monkeypatch) -> None:
    payload = _caps("get_trade_call", "scan_trade_calls")
    monkeypatch.setattr(cap.httpx, "get", lambda *a, **k: _FakeResp(payload))
    assert cap.fetch_capabilities("http://x/capabilities") == payload


def test_fetch_live_fail_uses_committed_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(cap.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    out = cap.fetch_capabilities("http://x/capabilities")
    # The committed snapshot reflects the post-CH1 registry (scan_trade_calls bot-flagged).
    assert out.get("tools")
    assert "scan_trade_calls" in cap.bot_flagged_tools(out)


def test_fetch_live_fail_no_snapshot_uses_hardcoded_surface(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cap.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    monkeypatch.setattr(cap, "_FALLBACK_PATH", tmp_path / "missing.json")
    out = cap.fetch_capabilities("http://x/capabilities")
    # Tier 3 — synthesized from BOT_TOOL_SURFACE; NEVER empty (the silent-corruption trap).
    assert cap.bot_flagged_tools(out) == set(cap.BOT_TOOL_SURFACE)


def test_bot_flagged_collapses_alias_to_canonical() -> None:
    caps = {"tools": [
        {"name": "get_trade_call", "canonical": "get_trade_call", "channels": {"bot": True}},
        {"name": "get_trade_signal", "canonical": "get_trade_call", "channels": {"bot": True}},  # alias
        {"name": "scan_trade_calls", "canonical": "scan_trade_calls", "channels": {"bot": True}},
        {"name": "scan_funding_arb", "canonical": "scan_funding_arb", "channels": {"bot": False}},
    ]}
    assert cap.bot_flagged_tools(caps) == {"get_trade_call", "scan_trade_calls"}


def test_derive_alert_types_and_commands() -> None:
    caps = _caps("get_trade_call", "get_market_regime", "scan_trade_calls")
    assert cap.derive_alert_types(caps) == {"calls", "regime"}
    assert cap.derive_commands(caps) == {"scan", "scanwatch"}


def test_surface_coverage_gap_empty_for_committed_snapshot() -> None:
    caps = cap._load_fallback_snapshot()
    assert caps is not None
    # Every channels.bot==true tool in the committed snapshot is mapped (the canary's by-construction parity).
    assert cap.surface_coverage_gap(caps) == set()


def test_surface_coverage_gap_catches_unmapped_bot_tool() -> None:
    caps = _caps("get_trade_call", "some_future_tool")
    assert cap.surface_coverage_gap(caps) == {"some_future_tool"}
