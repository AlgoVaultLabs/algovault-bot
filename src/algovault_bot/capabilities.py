"""FEATURE-PARITY-CHANNELS-W1 CH3 — the bot FOLLOWS MCP.

The bot derives its tool-backed surface (push-alert types + tool-pull commands)
from the MCP feature registry's PUBLIC projection (``GET /capabilities``) instead
of hand-maintaining its own list. A drift canary (CH5) asserts the bot's surface
COVERS exactly the bot-flagged tools — a future tool flipped ``channels.bot=true``
FAILS the canary until it is mapped in ``BOT_TOOL_SURFACE`` below.

A1 (architect-ratified): ``/capabilities`` deliberately does NOT expose the command
NAME — a command name is a bot-UX decision, not a universal feature property that
external consumers need. So the bot derives WHICH tools are bot-flagged
(``channels.bot==true``) from the live projection, and maps each canonical tool →
its bot surface here. The registry owns the WHAT (channel reach); the bot owns the
HOW (command/alert name); the canary enforces no-drift.

3-tier graceful degradation for the fetch (external-api-classification contract):
  Tier 1 — live ``GET /capabilities``
  Tier 2 — committed ``capabilities-fallback.json`` snapshot (offline-safe)
  Tier 3 — synthesize from ``BOT_TOOL_SURFACE`` (the bot's own knowledge; never empty)
Each lower tier emits an operator-visible WARNING. An empty/missing set is the
silent-corruption trap this contract exists to prevent.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10.0

# The bot-side canonical-tool → surface map (A1). 'alert' tools are push (the
# /watch alert types); 'command' tools are pull (slash commands). The CH5 canary
# asserts every channels.bot==true canonical tool in /capabilities is a key here.
BOT_TOOL_SURFACE: dict[str, dict[str, Any]] = {
    "get_trade_call": {"kind": "alert", "alert_type": "calls"},
    "get_market_regime": {"kind": "alert", "alert_type": "regime"},
    "scan_trade_calls": {"kind": "command", "commands": ["scan", "scanwatch"]},
    # BOT-FUNDING-SOT-W1 (2026-06-15): scan_funding_arb flipped channels.bot=true
    # in the MCP registry → its command surfaces here as /funding.
    "scan_funding_arb": {"kind": "command", "commands": ["funding"]},
}

_FALLBACK_PATH = Path(__file__).resolve().parent / "capabilities-fallback.json"


def capabilities_url() -> str:
    """Derive the /capabilities URL from ALGOVAULT_MCP_URL (a sibling of /mcp)."""
    mcp_url = os.environ.get("ALGOVAULT_MCP_URL", "http://127.0.0.1:3000/mcp").rstrip("/")
    base = mcp_url[: -len("/mcp")] if mcp_url.endswith("/mcp") else mcp_url
    return f"{base}/capabilities"


def _load_fallback_snapshot() -> dict[str, Any] | None:
    try:
        return json.loads(_FALLBACK_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning(json.dumps({"event": "capabilities_fallback_snapshot_unreadable", "err": str(e)[:200]}))
        return None


def _synthetic_from_surface() -> dict[str, Any]:
    """Tier 3 — derive a minimal projection from the bot's own surface map."""
    return {
        "server": "bot-fallback",
        "version": "hardcoded-surface",
        "tools": [
            {"name": n, "canonical": n,
             "channels": {"mcp": True, "httpX402": False, "bot": True, "webhook": False}}
            for n in BOT_TOOL_SURFACE
        ],
    }


def fetch_capabilities(url: str | None = None) -> dict[str, Any]:
    """Return the MCP capabilities projection ``{tools:[...]}``. 3-tier fail-open:
    live → committed snapshot → synthetic-from-surface. NEVER returns empty."""
    target = url or capabilities_url()
    try:
        resp = httpx.get(target, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("tools"), list) and data["tools"]:
            return data
        log.warning(json.dumps({"event": "capabilities_live_empty_or_malformed", "url": target}))
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
        log.warning(json.dumps({"event": "capabilities_live_fetch_failed", "err": str(e)[:200]}))

    snap = _load_fallback_snapshot()
    if snap and isinstance(snap.get("tools"), list) and snap["tools"]:
        log.warning(json.dumps({"event": "capabilities_using_fallback_snapshot"}))
        return snap

    log.warning(json.dumps({"event": "capabilities_using_hardcoded_surface_fallback"}))
    return _synthetic_from_surface()


def bot_flagged_tools(caps: dict[str, Any]) -> set[str]:
    """Canonical names of tools with ``channels.bot == true`` (aliases collapse to canonical)."""
    out: set[str] = set()
    for t in caps.get("tools", []):
        if isinstance(t, dict) and isinstance(t.get("channels"), dict) and t["channels"].get("bot") is True:
            name = t.get("canonical") or t.get("name")
            if name:
                out.add(name)
    return out


def surface_coverage_gap(caps: dict[str, Any]) -> set[str]:
    """Bot-flagged tools with NO BOT_TOOL_SURFACE mapping (the CH5 canary requires this be empty)."""
    return bot_flagged_tools(caps) - set(BOT_TOOL_SURFACE)


def derive_alert_types(caps: dict[str, Any]) -> set[str]:
    """The bot's push-alert types — from the bot-flagged 'alert'-kind tools."""
    return {
        BOT_TOOL_SURFACE[t]["alert_type"]
        for t in bot_flagged_tools(caps)
        if BOT_TOOL_SURFACE.get(t, {}).get("kind") == "alert"
    }


def derive_commands(caps: dict[str, Any]) -> set[str]:
    """The bot's tool-pull commands — from the bot-flagged 'command'-kind tools."""
    cmds: set[str] = set()
    for t in bot_flagged_tools(caps):
        s = BOT_TOOL_SURFACE.get(t)
        if s and s.get("kind") == "command":
            cmds.update(s.get("commands", []))
    return cmds
