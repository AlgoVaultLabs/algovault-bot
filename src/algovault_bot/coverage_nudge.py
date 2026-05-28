"""Coverage nudge for /watch + /list — surfaces expected fire-rate before commit.

OPS-TRADE-CALL-CLUSTER-W1 CH4 (T1 retail UX).

DiophantusGrey-class subscribers add coin+TF+exchange combos like
BTC 4h BINANCE that have fired 0 alerts in 7d per OPS-BOT-NO-TRADE-CALLS-AUDIT-W1.
Without pre-watch expected-fire-rate visibility, subscribers think the bot
is broken. This module computes a per-venue+TF activity proxy using the
signal-MCP's `performance://signal-performance` resource (per Plan-Mode
Path η ratification — no new producer/consumer edge; uses existing
bot↔signal-MCP edge).

Read-path: signal-performance resource exposes:
  - byAsset.<COIN>.{count, tier, pfeWinRate}            (per-coin lifetime)
  - byTimeframe.<TF>.{count, evaluated, pfeWinRate}     (per-TF lifetime)
  - byExchange.<X>.byTimeframe.<TF>.{count, evaluated, pfeWinRate}  ← KEY: per-(exchange × TF)
  - period.{from, to}                                   (audit window)

Algorithm:
  1. Fetch signal-performance once (cache 5min in-process).
  2. Per-combo proxy = byExchange[X].byTimeframe[TF].count / period_days.
     This is venue+TF activity across ALL coins, not per-coin specifically;
     genuine per-(coin × TF × exchange) breakdown not in resource shape.
  3. Combine with byAsset[coin] for coin-tier hint.
  4. Classify band: busy / moderate / quiet / silent.

Per CH4 AC4.5: bot text uses simple count-based "alerts/day" phrasing only
(audience-discipline T1). Helper returns counts only; nudge formatter
consumes counts + band only.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Literal, TypedDict

from .mcp_client import McpClient, McpError, from_env

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Cached signal-performance resource (5min TTL)

_CACHE_TTL_SECONDS = 300
_cached_perf: dict[str, Any] | None = None
_cached_at: float = 0.0


def _reset_cache_for_test() -> None:
    """Test seam — reset cache state."""
    global _cached_perf, _cached_at
    _cached_perf = None
    _cached_at = 0.0


def _fetch_perf(mcp: McpClient | None = None) -> dict[str, Any] | None:
    """Return cached signal-performance payload (refreshes every 5min).

    Returns None on McpError (defensive: bot continues without nudge).
    """
    global _cached_perf, _cached_at
    now = time.time()
    if _cached_perf is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached_perf
    try:
        if mcp is None:
            with from_env() as client:
                _cached_perf = client.read_resource("performance://signal-performance")
        else:
            _cached_perf = mcp.read_resource("performance://signal-performance")
        _cached_at = now
        return _cached_perf
    except McpError as exc:
        log.warning("coverage_nudge: signal-performance fetch failed: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — defensive; bot must never crash on nudge failure
        log.warning("coverage_nudge: unexpected error fetching signal-performance: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API


Band = Literal["busy", "moderate", "quiet", "silent", "unknown"]


class CoverageEstimate(TypedDict):
    coin_total_signals: int
    coin_tier: int | None
    venue_tf_signals_per_day: float
    period_days: int
    band: Band
    available: bool  # True when signal-performance data was fetched successfully


def _period_days(period: dict[str, str]) -> int:
    """Compute days between period.from and period.to (defensive on bad dates)."""
    try:
        d_from = date.fromisoformat(period["from"])
        d_to = date.fromisoformat(period["to"])
        return max(1, (d_to - d_from).days)
    except (KeyError, ValueError, TypeError):
        return 48  # fallback: typical period


def _classify_band(coin_total: int, venue_tf_per_day: float) -> Band:
    """Classify activity band from per-coin lifetime + per-venue+TF/day signals.

    Heuristic (per CH4 spec L204-206 bands; signals/day at the VENUE+TF aggregate,
    NOT per-coin — per-coin precision not in signal-performance resource):
      busy:     venue+TF fires > 100/day across all coins (e.g. 5m BYBIT)
      moderate: 20-100/day (e.g. 15m BINANCE)
      quiet:    1-20/day (e.g. 4h BINANCE — DiophantusGrey's case)
      silent:   < 1/day OR coin has zero lifetime signals

    coin_total = 0 → coin lacks track record entirely → silent regardless of venue+TF.
    """
    if coin_total == 0:
        return "silent"
    if venue_tf_per_day < 1:
        return "silent"
    if venue_tf_per_day < 20:
        return "quiet"
    if venue_tf_per_day < 100:
        return "moderate"
    return "busy"


def compute_coverage_estimate(
    coin: str,
    tf: str,
    exchange: str,
    mcp: McpClient | None = None,
) -> CoverageEstimate:
    """Compute per-combo activity estimate from signal-performance resource.

    Returns a CoverageEstimate dict. On fetch failure, returns band='unknown'
    + available=False so the caller can either skip the nudge or show a
    minimal fallback message.

    NOTE: venue_tf_signals_per_day is the AGGREGATE rate across all coins on
    (exchange × tf), NOT per-coin. The signal-performance resource doesn't
    expose 3D breakdown (coin × tf × exchange); we use venue+TF as the
    dominant activity signal and coin_total as a coin-quality hint.
    """
    perf = _fetch_perf(mcp)
    if perf is None:
        return CoverageEstimate(
            coin_total_signals=0,
            coin_tier=None,
            venue_tf_signals_per_day=0.0,
            period_days=0,
            band="unknown",
            available=False,
        )

    coin_data = (perf.get("byAsset") or {}).get(coin) or {}
    coin_total = int(coin_data.get("count") or 0)
    coin_tier = coin_data.get("tier")
    if coin_tier is not None:
        coin_tier = int(coin_tier)

    by_exchange = perf.get("byExchange") or {}
    venue_data = by_exchange.get(exchange) or {}
    by_tf = venue_data.get("byTimeframe") or {}
    venue_tf = by_tf.get(tf) or {}
    venue_tf_count = int(venue_tf.get("count") or 0)

    period = perf.get("period") or {}
    days = _period_days(period)
    venue_tf_per_day = venue_tf_count / days if days > 0 else 0.0
    band = _classify_band(coin_total, venue_tf_per_day)

    return CoverageEstimate(
        coin_total_signals=coin_total,
        coin_tier=coin_tier,
        venue_tf_signals_per_day=round(venue_tf_per_day, 2),
        period_days=days,
        band=band,
        available=True,
    )


def format_nudge(coin: str, tf: str, exchange: str, est: CoverageEstimate) -> str:
    """Produce the per-band nudge text for /watch confirmation + /list rows.

    Per CH4 AC4.5: bot text uses simple count-based "alerts/day" phrasing only
    (audience-discipline T1). Uses "signals/day" (count proxy) + coin tier
    (liquidity hint) only; no performance-metric-derived language.
    """
    if not est["available"]:
        return ""  # gracefully skip on fetch failure
    rate = est["venue_tf_signals_per_day"]
    if est["band"] == "silent":
        if est["coin_total_signals"] == 0:
            return (
                f"\n⚠️ Heads up: {coin} has no recent signal history across any TF/exchange. "
                f"Consider a more active coin (BTC/ETH/SOL/ZEC/TAO) or shorter timeframe."
            )
        return (
            f"\n⚠️ Heads up: {exchange} {tf} fires ~{rate}/day across all coins. "
            f"Alerts on this combo will be rare. Consider a shorter timeframe."
        )
    if est["band"] == "quiet":
        return (
            f"\n🟡 Heads up: {exchange} {tf} fires ~{rate}/day across all coins. "
            f"Alerts will be infrequent on this combo."
        )
    if est["band"] == "moderate":
        return (
            f"\n📊 {exchange} {tf} fires ~{rate}/day across all coins. "
            f"Expect occasional alerts."
        )
    if est["band"] == "busy":
        return (
            f"\n📊 {exchange} {tf} fires ~{rate}/day across all coins. "
            f"Expect frequent alerts."
        )
    return ""


def format_nudge_short(est: CoverageEstimate) -> str:
    """Compact one-liner for /list rows (less verbose than full nudge)."""
    if not est["available"]:
        return ""
    rate = est["venue_tf_signals_per_day"]
    icon = {
        "busy": "📊",
        "moderate": "📊",
        "quiet": "🟡",
        "silent": "⚠️",
        "unknown": "",
    }.get(est["band"], "")
    if not icon:
        return ""
    return f" {icon} ~{rate}/day venue+TF"
