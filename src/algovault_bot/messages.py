"""User-facing message strings.

D3-A: every CTA URL points at canonical ``api.algovault.com/signup?plan=starter``
(NOT ``algovault.com/signup`` — that surface 404s; verified live 2026-05-08).

The welcome-message constants exposed here are byte-stable: the C1
verification gate fixtures grep against the literal lines.
"""

from __future__ import annotations

from typing import Final


SIGNUP_BASE: Final = "api.algovault.com/signup?plan=starter"


def signup_url(campaign: str) -> str:
    """Build a UTM-tagged signup URL. Used for every bot-side CTA."""
    return f"{SIGNUP_BASE}&utm_source=tg_bot&utm_campaign={campaign}"


WELCOME_MESSAGE: Final = (
    "👋 Welcome to AlgoVault — the brain layer for AI trading agents.\n"
    "\n"
    "I push two kinds of alerts to your watchlist:\n"
    "📊 Regime shifts — free, no limit\n"
    "📈 Trade calls (BUY/SELL) — counts against your free 100 calls/month\n"
    "HOLD verdicts are silent + free.\n"
    "\n"
    "Free tier covers all 710+ assets and all 11 timeframes (1m–1d). YOU choose what to watch.\n"
    "\n"
    "More assets + lower timeframes = faster quota burn. Examples:\n"
    "• /watch BTC 1d        — slow burn (~1 alert/mo)\n"
    "• /watch BTC 4h        — moderate (~5 alerts/mo per pair)\n"
    "• /watch BTC 15m       — fast (~30 alerts/mo per pair)\n"
    "• /watch BTC 1m        — very fast (cap blown in days)\n"
    "\n"
    "Get started:\n"
    "/watch <COIN> <TF>     — add to watchlist\n"
    "/list                  — see your picks\n"
    "/help                  — full commands\n"
    "\n"
    "Hit the cap? Upgrade to Starter ($9.99 → 3,000 calls/mo) or pay per call via x402.\n"
    f"→ {signup_url('start_welcome')}"
)


HELP_MESSAGE: Final = (
    "AlgoVault Bot — full command list\n"
    "\n"
    "/start — show welcome message\n"
    "/watch <COIN> <TF> [EXCHANGE] [TYPE] — add to watchlist\n"
    "    COIN:     BTC, ETH, SOL, etc. (uppercase, 2–10 chars)\n"
    "    TF:       1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d\n"
    "    EXCHANGE: HL BINANCE BYBIT OKX BITGET (default: BINANCE)\n"
    "    TYPE:     regime | calls | both (default: both)\n"
    "/unwatch <COIN> <TF> [EXCHANGE] — remove from watchlist\n"
    "/list — show your watchlist (max 50 entries)\n"
    "/help — this message\n"
    "\n"
    "Examples:\n"
    "  /watch BTC 4h                — both alert types, default BINANCE\n"
    "  /watch ETH 1h HL regime      — regime-only on Hyperliquid\n"
    "  /watch SOL 15m BYBIT calls   — trade calls only on Bybit\n"
    "  /unwatch BTC 4h              — remove BTC 4h from BINANCE\n"
    "\n"
    "Quota:\n"
    "📊 Regime shifts — free, no limit\n"
    "📈 Trade calls — count against your free 100 calls/month\n"
    "HOLD verdicts are silent + free.\n"
    "\n"
    f"Upgrade: → {signup_url('help_message')}"
)


def cap_reached_message(cap: int = 50) -> str:
    return (
        f"⚠️ You've hit the per-user watchlist cap of {cap} entries.\n"
        f"Use /unwatch to drop one before adding more, or upgrade for unlimited tracking:\n"
        f"→ {signup_url('watchlist_cap')}"
    )


def watch_added_message(coin: str, timeframe: str, exchange: str, alert_type: str) -> str:
    types = {
        "regime": "regime only",
        "calls": "trade calls only",
        "both": "regime + calls",
    }
    return f"✅ Watching {coin} {timeframe} on {exchange} ({types[alert_type]})."


def watch_removed_message(coin: str, timeframe: str, exchange: str) -> str:
    return f"🗑️ No longer watching {coin} {timeframe} on {exchange}."


def watch_not_found_message(coin: str, timeframe: str, exchange: str) -> str:
    return f"That entry isn't on your watchlist: {coin} {timeframe} on {exchange}."


def list_empty_message() -> str:
    return (
        "Your watchlist is empty.\n"
        "Try `/watch BTC 4h` to start tracking. /help for the full command list."
    )


def list_message(rows: list[dict[str, str]], cap: int = 50) -> str:
    type_glyph = {"regime": "📊", "calls": "📈", "both": "📊📈"}
    lines = ["Your watchlist:"]
    for r in rows:
        glyph = type_glyph.get(r["alert_type"], "")
        lines.append(f"  {glyph} {r['coin']} {r['timeframe']} on {r['exchange']}  ({r['alert_type']})")
    lines.append(f"\n{len(rows)}/{cap} used.")
    return "\n".join(lines)


def usage_watch_message() -> str:
    return (
        "Usage: /watch <COIN> <TIMEFRAME> [EXCHANGE] [TYPE]\n"
        "Examples:\n"
        "  /watch BTC 4h\n"
        "  /watch ETH 1h binance regime\n"
        "Type /help for the full command reference."
    )


def usage_unwatch_message() -> str:
    return (
        "Usage: /unwatch <COIN> <TIMEFRAME> [EXCHANGE]\n"
        "Example: /unwatch BTC 4h"
    )
