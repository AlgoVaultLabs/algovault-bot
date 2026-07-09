"""TG-BATCH-WATCHLIST-W1 C1 — pure batch-spec parser + Cartesian expander.

No I/O. Each `/watch` / `/unwatch` dimension (COINS / TFS / EXCHANGES) accepts
a single token, a comma-list (`BTC,ETH,SOL`), or the literal `all`. The asset
universe for `all` coins is INJECTED as an argument (the HTTP/MCP fetch lives
in ``asset_universe.py``), keeping this module fully unit-testable.

`all` TF → all 11 timeframes (ascending); `all` EXCHANGE → all 12 exchanges;
`all` COIN → the injected universe. ``expand_watch_spec`` returns the
de-duplicated Cartesian product as ``(coin, tf, exchange)`` tuples.

Nudge policy (TG-BATCH-WATCHLIST-W1, adjustment 3): a confirmation keyboard
fires when the expansion exceeds ``BATCH_CONFIRM_THRESHOLD`` combos OR the
COIN dimension is the literal ``all`` — a bounded `/watch BTC all` (11 TFs)
commits inline, the nudge is reserved for big / all-coins expansions.
"""

from __future__ import annotations

from typing import Sequence

from .validators import (
    EXCHANGE_DISPLAY_ORDER,
    TF_SECONDS,
    normalize_coin,
    normalize_exchange,
    normalize_timeframe,
)

ALL_TOKEN = "all"

# Default confirmation threshold (env-overridable in the handler layer).
DEFAULT_BATCH_CONFIRM_THRESHOLD: int = 50

# Default size of the "Top N most-active" clamp offered in the nudge keyboard.
DEFAULT_TOP_N: int = 30

# Canonical ascending TF order (1m … 1d) — TF_SECONDS preserves insertion order.
TF_ORDER: tuple[str, ...] = tuple(TF_SECONDS.keys())

# Canonical exchange order for `all` expansion — the single validators source (12 venues).
EXCHANGE_ORDER: tuple[str, ...] = EXCHANGE_DISPLAY_ORDER


def is_all(raw: str) -> bool:
    return raw.strip().lower() == ALL_TOKEN


def _split_tokens(raw: str) -> list[str]:
    return [t for t in (s.strip() for s in raw.split(",")) if t]


def parse_coins(raw: str, universe: Sequence[str]) -> list[str]:
    """`all` → injected universe; comma-list / single → normalized coins.

    Raises ``ValidationError`` on any malformed coin token.
    """
    if is_all(raw):
        return list(universe)
    return [normalize_coin(t) for t in _split_tokens(raw)]


def parse_timeframes(raw: str) -> list[str]:
    if is_all(raw):
        return list(TF_ORDER)
    return [normalize_timeframe(t) for t in _split_tokens(raw)]


def parse_exchanges(raw: str) -> list[str]:
    if is_all(raw):
        return list(EXCHANGE_ORDER)
    return [normalize_exchange(t) for t in _split_tokens(raw)]


def cartesian(
    coins: Sequence[str], tfs: Sequence[str], exchanges: Sequence[str]
) -> list[tuple[str, str, str]]:
    """De-duplicated Cartesian product, preserving first-seen order."""
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for c in coins:
        for t in tfs:
            for x in exchanges:
                key = (c, t, x)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    return out


def expand_watch_spec(
    coins_raw: str, tfs_raw: str, exchanges_raw: str, *, universe: Sequence[str]
) -> list[tuple[str, str, str]]:
    """Parse each dimension then return the de-duplicated Cartesian product."""
    return cartesian(
        parse_coins(coins_raw, universe),
        parse_timeframes(tfs_raw),
        parse_exchanges(exchanges_raw),
    )


def should_confirm(num_combos: int, coins_raw: str, threshold: int) -> bool:
    """Nudge fires iff the expansion is large OR the COIN dim is `all`."""
    return num_combos > threshold or is_all(coins_raw)
