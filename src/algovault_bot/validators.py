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

# SIGNAL-CLOSEDBAR-FLIP-W1 CH3 (2026-08-07): 1m is a valid ON-DEMAND timeframe and a
# structurally invalid PUSH timeframe. The two are not the same question, so they get two
# sets.
#
# The engine now scores on CONFIRMED bars only, and the dispatcher fires on a 60-second
# tick. On a 60-second bar that leaves EXACTLY ONE dispatch opportunity per bar, so the
# close-grace cannot be honoured: whatever phase the tick lands on, the just-closed bar may
# not be published by the venue yet, and the alert then carries a silently one-bar-stale
# verdict. There is no offset to shift to — the bar and the tick are the same length — so
# this is arithmetic, not a tuning problem, and no config value fixes it.
#
# It does NOT affect one-shot answers: /call, /regime, /scan and the MCP tools compute at
# request time against whatever has closed by then, which is exactly the freshness contract
# a caller asked for. So:
#
#   TIMEFRAMES       — may I ANSWER for this timeframe right now?      (on-demand)
#   PUSH_TIMEFRAMES  — may I SCHEDULE a repeating alert on it?         (push)
#
# Anything that schedules validates against PUSH_TIMEFRAMES; anything answering a one-shot
# request keeps TIMEFRAMES. Deriving one from the other (rather than writing a second
# literal) keeps a future timeframe addition from silently skipping the push question.
PUSH_TIMEFRAMES: Final[frozenset[str]] = TIMEFRAMES - {"1m"}

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


def smallest_push_timeframe() -> str:
    """The shortest timeframe that CAN be pushed — derived, never a literal.

    Named in the rejection message below, so that retiring or restoring a push timeframe
    updates the user-facing suggestion automatically instead of leaving it pointing at a
    timeframe we no longer push.
    """
    return min(PUSH_TIMEFRAMES, key=lambda x: TF_SECONDS[x])


def normalize_push_timeframe(raw: str) -> str:
    """`normalize_timeframe` for surfaces that SCHEDULE a repeating alert.

    Kept separate rather than folded into `normalize_timeframe` because "may I answer for
    this timeframe" and "may I schedule a repeating alert on it" are different questions —
    see PUSH_TIMEFRAMES.

    A timeframe that is valid on demand but not for push gets its OWN message naming the
    alternative, because the generic "Pick one of: …" list reads as a typo report and would
    leave the user guessing why a timeframe they can plainly see in /help was refused.
    """
    tf = raw.strip().lower()
    if tf in PUSH_TIMEFRAMES:
        return tf
    if tf in TIMEFRAMES:
        raise ValidationError(
            f"{tf} alerts can't be scheduled. A {tf} candle is the same length as the alert "
            f"tick, so there's no room to wait for the candle to close first — the verdict "
            f"could reach you one candle out of date. Use {smallest_push_timeframe()} for "
            f"alerts, or ask any time with /call <COIN> {tf}."
        )
    sorted_tfs = " ".join(sorted(PUSH_TIMEFRAMES, key=lambda x: TF_SECONDS[x]))
    raise ValidationError(f"Invalid timeframe '{raw}'. Pick one of: {sorted_tfs}")


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
