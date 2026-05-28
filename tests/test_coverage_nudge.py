"""OPS-TRADE-CALL-CLUSTER-W1 CH4 — vitest-equivalent seam for coverage nudge.

Tests the per-(coin × tf × exchange) activity proxy + the band classifier
+ the nudge formatter that get-watched / get-listed subscribers see.

Per CH4 AC4.5: NO PFE-WR-derived language in user-facing bot text.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from algovault_bot.coverage_nudge import (
    CoverageEstimate,
    _classify_band,
    _reset_cache_for_test,
    compute_coverage_estimate,
    format_nudge,
    format_nudge_short,
)


# Sample signal-performance resource payload mimicking live shape per
# Plan-Mode Path η probe at 2026-05-28 10:55 UTC.
SAMPLE_PERF = {
    "period": {"from": "2026-04-10", "to": "2026-05-28"},  # 48 days
    "byAsset": {
        "BTC": {"count": 4188, "tier": 1, "pfeWinRate": 0.92},
        "ETH": {"count": 3500, "tier": 1, "pfeWinRate": 0.91},
        "RANDOMCOIN": {"count": 0, "tier": 4, "pfeWinRate": 0.0},  # no track record
    },
    "byExchange": {
        "BINANCE": {
            "byTimeframe": {
                "1m": {"count": 6215, "evaluated": 6215, "pfeWinRate": 0.92},
                "5m": {"count": 45038, "evaluated": 45038, "pfeWinRate": 0.92},
                "4h": {"count": 2580, "evaluated": 2580, "pfeWinRate": 0.85},  # quiet
                "1d": {"count": 50, "evaluated": 50, "pfeWinRate": 0.58},  # silent-ish (1.04/day)
            },
        },
        "BYBIT": {
            "byTimeframe": {
                "5m": {"count": 12795, "evaluated": 12795, "pfeWinRate": 0.94},
            },
        },
    },
}


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Reset the in-process cache before each test so mocks land cleanly."""
    _reset_cache_for_test()
    yield
    _reset_cache_for_test()


def _make_mcp_mock(perf_payload: dict | None = SAMPLE_PERF) -> MagicMock:
    mock = MagicMock()
    if perf_payload is None:
        from algovault_bot.mcp_client import McpError
        mock.read_resource.side_effect = McpError("simulated fetch failure")
    else:
        mock.read_resource.return_value = perf_payload
    return mock


def test_classify_band_returns_silent_for_zero_coin_total() -> None:
    """coin lacks any track record → silent regardless of venue+TF activity."""
    assert _classify_band(0, 1000.0) == "silent"


def test_classify_band_returns_busy_for_high_venue_tf_volume() -> None:
    # BTC + 5m BYBIT (12795/48d = 266/day) → busy
    assert _classify_band(4188, 266.0) == "busy"


def test_classify_band_returns_quiet_for_4h_binance() -> None:
    """The DiophantusGrey case: BTC 4h BINANCE (2580/48d = 53.75/day) → moderate.

    Updated: 53.75/day falls in [20, 100) → moderate per spec L204-206 bands.
    The intent is still 'less active than 5m'.
    """
    band = _classify_band(4188, 53.75)
    assert band == "moderate"


def test_classify_band_returns_silent_for_sub_1_per_day() -> None:
    assert _classify_band(100, 0.5) == "silent"
    assert _classify_band(100, 0.99) == "silent"


def test_compute_coverage_estimate_busy_btc_5m_binance() -> None:
    mock = _make_mcp_mock()
    est = compute_coverage_estimate("BTC", "5m", "BINANCE", mcp=mock)
    assert est["available"] is True
    assert est["coin_total_signals"] == 4188
    assert est["coin_tier"] == 1
    # 45038 / 48 = 938.29
    assert abs(est["venue_tf_signals_per_day"] - 938.29) < 0.5
    assert est["period_days"] == 48
    assert est["band"] == "busy"


def test_compute_coverage_estimate_quiet_btc_4h_binance() -> None:
    """DiophantusGrey case from OPS-BOT-NO-TRADE-CALLS-AUDIT-W1."""
    mock = _make_mcp_mock()
    est = compute_coverage_estimate("BTC", "4h", "BINANCE", mcp=mock)
    assert est["available"] is True
    assert est["coin_total_signals"] == 4188
    # 2580 / 48 = 53.75
    assert abs(est["venue_tf_signals_per_day"] - 53.75) < 0.5
    assert est["band"] in ("moderate", "quiet")  # band depends on threshold


def test_compute_coverage_estimate_silent_for_unknown_coin() -> None:
    mock = _make_mcp_mock()
    est = compute_coverage_estimate("RANDOMCOIN", "5m", "BINANCE", mcp=mock)
    assert est["available"] is True
    assert est["coin_total_signals"] == 0
    assert est["band"] == "silent"


def test_compute_coverage_estimate_silent_for_1d_low_activity() -> None:
    mock = _make_mcp_mock()
    est = compute_coverage_estimate("BTC", "1d", "BINANCE", mcp=mock)
    assert est["available"] is True
    # 50 / 48 = 1.04/day → above silent threshold (<1) but below quiet (20)
    assert est["band"] == "quiet"


def test_compute_coverage_estimate_handles_mcp_failure() -> None:
    """McpError → returns band=unknown + available=False (bot continues without nudge)."""
    mock = _make_mcp_mock(perf_payload=None)
    est = compute_coverage_estimate("BTC", "5m", "BINANCE", mcp=mock)
    assert est["available"] is False
    assert est["band"] == "unknown"


def test_format_nudge_silent_band_no_pfe_language() -> None:
    """AC4.5: NO PFE-WR-derived language in user-facing text."""
    est: CoverageEstimate = {
        "coin_total_signals": 4188, "coin_tier": 1,
        "venue_tf_signals_per_day": 0.5, "period_days": 48,
        "band": "silent", "available": True,
    }
    msg = format_nudge("BTC", "4h", "BINANCE", est)
    assert "PFE" not in msg
    assert "outcome" not in msg.lower()
    assert "win rate" not in msg.lower()
    assert "win_rate" not in msg.lower()
    assert "Heads up" in msg
    assert "BINANCE 4h" in msg


def test_format_nudge_busy_band_no_pfe_language() -> None:
    """AC4.5 (busy band branch)."""
    est: CoverageEstimate = {
        "coin_total_signals": 4188, "coin_tier": 1,
        "venue_tf_signals_per_day": 938.29, "period_days": 48,
        "band": "busy", "available": True,
    }
    msg = format_nudge("BTC", "5m", "BINANCE", est)
    assert "PFE" not in msg
    assert "outcome" not in msg.lower()
    assert "win rate" not in msg.lower()
    assert "Expect frequent alerts" in msg
    assert "BINANCE 5m" in msg


def test_format_nudge_short_for_list_rows() -> None:
    """Compact one-liner suitable for /list per-row append."""
    est: CoverageEstimate = {
        "coin_total_signals": 4188, "coin_tier": 1,
        "venue_tf_signals_per_day": 938.29, "period_days": 48,
        "band": "busy", "available": True,
    }
    short = format_nudge_short(est)
    assert "938" in short
    assert "📊" in short
    # also returns empty when unavailable
    est_unavail: CoverageEstimate = {
        "coin_total_signals": 0, "coin_tier": None,
        "venue_tf_signals_per_day": 0.0, "period_days": 0,
        "band": "unknown", "available": False,
    }
    assert format_nudge_short(est_unavail) == ""


def test_format_nudge_returns_empty_when_unavailable() -> None:
    est: CoverageEstimate = {
        "coin_total_signals": 0, "coin_tier": None,
        "venue_tf_signals_per_day": 0.0, "period_days": 0,
        "band": "unknown", "available": False,
    }
    assert format_nudge("BTC", "4h", "BINANCE", est) == ""
