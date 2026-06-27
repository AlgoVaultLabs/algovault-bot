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
import time
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


# ── SCAN-RANKBY-W1: the scan_trade_calls rankBy lens set, DERIVED from /capabilities
#    (the bot forwards the RAW token; the MCP resolves the alias). The bot never
#    hardcodes the set — it reads the MCP advertisement, with the same 3-tier fail-open
#    as the surface fetch. The Tier-3 fallback (the bot's own knowledge) keeps /scan +
#    /scanwatch parsing working even if /capabilities + the snapshot are unavailable. ──
_FALLBACK_RANK_LENSES: dict[str, Any] = {
    "param": "rankBy",
    "values": ["oi", "volume", "gainers", "losers", "movers", "funding_positive",
               "funding_negative", "volatility", "oi_change"],  # SCAN-RANKBY-W2 / W3
    "aliases": {"oi": "oi", "vol": "volume", "gain": "gainers", "lose": "losers",
                "move": "movers", "pfr": "funding_positive", "nfr": "funding_negative",
                "atr": "volatility", "oid": "oi_change"},  # SCAN-RANKBY-W2 / W3
    "default": "oi",
}
_RANK_LENSES_TTL = 300.0
_rank_lenses_cache: tuple[float, dict[str, Any]] | None = None


def derive_rank_lenses(caps: dict[str, Any]) -> dict[str, Any] | None:
    """Extract scan_trade_calls' advertised rankBy lens set, or None if not present."""
    for t in caps.get("tools", []):
        if isinstance(t, dict) and t.get("name") == "scan_trade_calls" and isinstance(t.get("lenses"), dict):
            return t["lenses"]
    return None


def rank_lenses(*, force: bool = False) -> dict[str, Any]:
    """Cached scan_trade_calls rankBy lens set {param, values, aliases, default},
    derived from /capabilities (Tier 1/2) with a hardcoded Tier-3 fallback. Never empty."""
    global _rank_lenses_cache
    now = time.monotonic()
    if not force and _rank_lenses_cache and now - _rank_lenses_cache[0] < _RANK_LENSES_TTL:
        return _rank_lenses_cache[1]
    lenses = derive_rank_lenses(fetch_capabilities())
    if lenses is None or not lenses.get("values"):
        log.warning(json.dumps({"event": "rank_lenses_using_hardcoded_fallback"}))
        lenses = _FALLBACK_RANK_LENSES
    _rank_lenses_cache = (now, lenses)
    return lenses


def _reset_rank_lenses_cache() -> None:
    """Test seam — clear the rank-lenses cache between cases."""
    global _rank_lenses_cache
    _rank_lenses_cache = None


def recognized_rank_tokens() -> set[str]:
    """All accepted rankBy tokens (canonical values ∪ alias keys), lowercased — the set
    the /scan + /scanwatch parsers use to RECOGNIZE a lens token (forwarded raw)."""
    L = rank_lenses()
    return {str(v).lower() for v in L.get("values", [])} | {str(a).lower() for a in L.get("aliases", {})}


def rank_lens_help() -> str:
    """A short, derived 'valid lenses' line for command help + the friendly error."""
    L = rank_lenses()
    vals = " ".join(str(v) for v in L.get("values", []))
    return f"Lenses: {vals} (default {L.get('default', 'oi')}; aliases vol/gain/lose/move/pfr/nfr)."


# Human DISPLAY labels for a lens (UX copy — the canonical SET is derived above; these
# only label it). `oi` keeps the legacy "OI" string so default /scan + /scanwatch copy
# stays byte-identical. Single source for both the /scan reply + the scan-digest header.
RANK_LABELS: dict[str, str] = {
    "oi": "OI",
    "volume": "24h volume",
    "gainers": "24h gainers",
    "losers": "24h losers",
    "movers": "24h movers",
    "funding_positive": "funding (most positive)",
    "funding_negative": "funding (most negative)",
    "volatility": "ATRP (volatility)",  # SCAN-RANKBY-W2
    "oi_change": "OI change (24h)",  # SCAN-RANKBY-W3
}


def rank_label(token: str | None) -> str:
    """Human display label for a RAW lens token. None/oi → 'OI' (byte-identical default);
    a raw alias (nfr/pfr/…) resolves to its canonical label."""
    if not token:
        return "OI"
    t = token.lower()
    canonical = t if t in RANK_LABELS else rank_lenses().get("aliases", {}).get(t, t)
    return RANK_LABELS.get(canonical, canonical)
