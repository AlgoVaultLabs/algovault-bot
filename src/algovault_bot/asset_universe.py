"""TG-BATCH-WATCHLIST-W1 C1 — asset-universe source for `all`-coin expansion.

`all` coins resolves to the set of assets the signal engine actually produces
signals for. Plan-Mode Step-0 probe (2026-05-29): `/api/assets` 404s, but the
existing `performance://signal-performance` MCP resource (already consumed by
``coverage_nudge``) exposes ``byAsset.<COIN>.{count, tier}`` — 751 keys live,
loopback-reachable, no new cross-repo contract. We reuse that read path.

- ``get_asset_universe()`` → sorted list of asset symbols (the `all` universe).
- ``get_top_assets(n)`` → the n most-ACTIVE assets, ranked by ``byAsset.count``
  desc (signal-activity as a liquidity/activity proxy — the honest substitute
  for an AUM/OI coin-ranking, which is not bot-reachable; AUM/OI ranking is a
  deferred upgrade, see OPS-TG-SHARED-FETCH-CACHE-W1 / status.md).

Defensive: any fetch failure returns an empty list — the caller degrades
(shows the user a "couldn't load the asset list, try specific coins" path)
rather than crashing. 5-minute in-process cache mirrors ``coverage_nudge``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .mcp_client import McpClient, McpError, from_env

log = logging.getLogger(__name__)

_PERF_RESOURCE = "performance://signal-performance"
_CACHE_TTL_SECONDS = 300
_cached_perf: dict[str, Any] | None = None
_cached_at: float = 0.0


def _reset_cache_for_test() -> None:
    """Test seam — clear the cached payload."""
    global _cached_perf, _cached_at
    _cached_perf = None
    _cached_at = 0.0


def _fetch_perf(mcp: McpClient | None = None) -> dict[str, Any] | None:
    """Cached signal-performance payload (5-min TTL). None on any failure."""
    global _cached_perf, _cached_at
    now = time.time()
    if _cached_perf is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached_perf
    try:
        if mcp is None:
            with from_env() as client:
                _cached_perf = client.read_resource(_PERF_RESOURCE)
        else:
            _cached_perf = mcp.read_resource(_PERF_RESOURCE)
        _cached_at = now
        return _cached_perf
    except McpError as exc:
        log.warning("asset_universe: signal-performance fetch failed: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — defensive; never crash the command path
        log.warning("asset_universe: unexpected error fetching signal-performance: %s", exc)
        return None


def _by_asset(mcp: McpClient | None) -> dict[str, Any]:
    perf = _fetch_perf(mcp)
    if not perf:
        return {}
    by_asset = perf.get("byAsset")
    return by_asset if isinstance(by_asset, dict) else {}


def get_asset_universe(mcp: McpClient | None = None) -> list[str]:
    """Return the `all`-coins universe (sorted asset symbols). Empty on failure."""
    return sorted(_by_asset(mcp).keys())


def get_top_assets(n: int, mcp: McpClient | None = None) -> list[str]:
    """Return the n most-active assets, ranked by lifetime signal count desc."""
    by_asset = _by_asset(mcp)
    if not by_asset:
        return []

    def _count(item: tuple[str, Any]) -> int:
        try:
            return int((item[1] or {}).get("count") or 0)
        except (TypeError, ValueError):
            return 0

    ranked = sorted(by_asset.items(), key=_count, reverse=True)
    return [coin for coin, _ in ranked[: max(0, n)]]
