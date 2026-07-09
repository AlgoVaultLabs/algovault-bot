"""Input validation for bot commands.

Spec C2: COIN matches ``^[A-Z0-9]{2,10}$``; TIMEFRAME ∈ 11 supported; EXCHANGE
∈ 5 supported (default BINANCE per inline-fix D8 — HL upstream is rate-limit
hot from external IPs and would compete with signal-MCP's seed-signals cron);
TYPE ∈ ``{regime, calls, both}``.
"""

from __future__ import annotations

import re
from typing import Final

from .capabilities import BOT_TOOL_SURFACE


COIN_RE: Final = re.compile(r"^[A-Z0-9]{2,10}$")

TIMEFRAMES: Final[frozenset[str]] = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"}
)

# Per CLAUDE.md TF→seconds map cited in C3 spec line 248.
TF_SECONDS: Final[dict[str, int]] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}

# TG-COPY-DEFAULTS-VENUES-W1: 12 venues — the exact set the signal server serves per command
# (verified live: scan_trade_calls enum == these 12; get_trade_call/get_market_regime accept
# them; scan_funding_arb is cross-venue). Display order lives in keyboards._EXCHANGE_ORDER.
EXCHANGES: Final[frozenset[str]] = frozenset({
    "HL", "BINANCE", "BYBIT", "OKX", "BITGET",
    "ASTER", "BINGX", "GATE", "HTX", "KUCOIN", "MEXC", "PHEMEX",
})

# D8 inline-fix: default exchange BINANCE (was HL in spec; HL upstream
# rate-limit hot from external IPs per REGIME-BOT-W1 P5 truth-table).
DEFAULT_EXCHANGE: Final = "BINANCE"

# TG-COPY-DEFAULTS-VENUES-W1: friendly aliases → canonical token, applied in
# normalize_exchange BEFORE the membership check. Canonical tokens (gate, kucoin, …)
# already resolve via upper()+membership; only NON-canonical variants live here.
EXCHANGE_ALIASES: Final[dict[str, str]] = {
    "HYPERLIQUID": "HL",
    "GATEIO": "GATE",
    "KC": "KUCOIN",
}

# TG-COPY-DEFAULTS-VENUES-W1: the SINGLE ordered venue source (HL-first, matching the /help
# copy). keyboards (wizard grid) + batch ("/watch all"/"all exchanges" expansion) both derive
# from this so a future venue-add can't leave one list stale (the drift this wave hit).
EXCHANGE_DISPLAY_ORDER: Final[tuple[str, ...]] = (
    "HL", "BINANCE", "BYBIT", "OKX", "BITGET",
    "ASTER", "BINGX", "GATE", "HTX", "KUCOIN", "MEXC", "PHEMEX",
)

# FEATURE-PARITY-CHANNELS-W1 CH3: DERIVED from the bot-flagged 'alert'-kind tools in
# capabilities.BOT_TOOL_SURFACE (the SoT the CH5 canary ties to /capabilities) + the
# 'both' composite. Value-identical to the prior hardcoded {regime,calls,both} — now
# sourced from the surface map, so a future alert tool extends it by construction
# (replaces the hand-maintained list per CH3(b)).
_ALERT_BASE: Final[frozenset[str]] = frozenset(
    s["alert_type"] for s in BOT_TOOL_SURFACE.values() if s.get("kind") == "alert"
)
ALERT_TYPES: Final[frozenset[str]] = _ALERT_BASE | {"both"}
DEFAULT_ALERT_TYPE: Final = "calls"


class ValidationError(ValueError):
    """Raised when a user-supplied argument is invalid."""


def normalize_coin(raw: str) -> str:
    coin = raw.strip().upper()
    if not COIN_RE.match(coin):
        raise ValidationError(
            f"Invalid coin '{raw}'. Must be 2–10 uppercase letters/digits (e.g. BTC, ETH, SOL)."
        )
    return coin


def normalize_timeframe(raw: str) -> str:
    tf = raw.strip().lower()
    if tf not in TIMEFRAMES:
        sorted_tfs = " ".join(sorted(TIMEFRAMES, key=lambda x: TF_SECONDS[x]))
        raise ValidationError(f"Invalid timeframe '{raw}'. Pick one of: {sorted_tfs}")
    return tf


def normalize_exchange(raw: str | None) -> str:
    if raw is None or raw == "":
        return DEFAULT_EXCHANGE
    ex = raw.strip().upper()
    ex = EXCHANGE_ALIASES.get(ex, ex)  # friendly alias → canonical (gateio→GATE, kc→KUCOIN, …)
    if ex not in EXCHANGES:
        raise ValidationError(
            f"Invalid exchange '{raw}'. Pick one of: {' '.join(sorted(EXCHANGES))}"
        )
    return ex


def normalize_alert_type(raw: str | None) -> str:
    if raw is None or raw == "":
        return DEFAULT_ALERT_TYPE
    t = raw.strip().lower()
    if t not in ALERT_TYPES:
        raise ValidationError(
            f"Invalid alert type '{raw}'. Pick one of: {' '.join(sorted(ALERT_TYPES))}"
        )
    return t
