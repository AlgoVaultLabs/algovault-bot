"""FEATURE-PARITY-CHANNELS-W1 CH4 — the bot's cadence_for_timeframe MIRRORS the MCP
scan-digest.ts (same map: nearest cadence ≥ tf, floor 1h). These tests pin the
shared contract on the bot side; the MCP side pins it in tests/scan-digest.test.ts.
"""
from __future__ import annotations

import pytest

from algovault_bot.scan_digest import (
    VALID_CADENCES,
    cadence_bucket_epoch,
    cadence_faster_than_timeframe,
    cadence_for_timeframe,
    is_valid_cadence,
    scan_digest_reminder,
)


@pytest.mark.parametrize(
    "tf,expected",
    [
        ("1m", "1h"), ("3m", "1h"), ("5m", "1h"), ("15m", "1h"), ("30m", "1h"), ("1h", "1h"),
        ("2h", "4h"), ("4h", "4h"),
        ("8h", "1d"), ("12h", "1d"), ("1d", "1d"),
    ],
)
def test_cadence_for_timeframe(tf: str, expected: str) -> None:
    assert cadence_for_timeframe(tf) == expected


def test_unknown_tf_defaults_to_slowest() -> None:
    assert cadence_for_timeframe("7m") == "1d"
    assert cadence_for_timeframe("") == "1d"


def test_is_valid_cadence() -> None:
    for c in VALID_CADENCES:
        assert is_valid_cadence(c)
    for bad in ("30m", "2h", "1w", "", None, 1):
        assert not is_valid_cadence(bad)


def test_cadence_bucket_epoch() -> None:
    base = 1_699_999_200  # 3600-aligned
    assert cadence_bucket_epoch("1h", base) == cadence_bucket_epoch("1h", base + 3599)
    assert cadence_bucket_epoch("1h", base + 3600) > cadence_bucket_epoch("1h", base)
    day = 1_699_920_000  # 86400-aligned
    assert cadence_bucket_epoch("1d", day) == cadence_bucket_epoch("1d", day + 86399)


def test_cadence_faster_than_timeframe() -> None:
    assert cadence_faster_than_timeframe("1h", "4h") is True
    assert cadence_faster_than_timeframe("1h", "2h") is True
    assert cadence_faster_than_timeframe("1h", "15m") is False  # default is never faster
    assert cadence_faster_than_timeframe("4h", "4h") is False


def test_reminder_copy_mirrors_mcp() -> None:
    msg = scan_digest_reminder("1h", "15m")
    assert "1h" in msg and "15m" in msg
    assert "max(1, calls)" in msg
    # A faster-than-tf cadence appends the stronger heads-up.
    msg2 = scan_digest_reminder("1h", "4h")
    assert "⚠️" in msg2 and "repeats" in msg2
