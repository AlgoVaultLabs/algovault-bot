"""FEATURE-PARITY-CHANNELS-W1 CH4 — scan-digest cadence helpers (bot side).

A faithful Python MIRROR of the MCP ``src/lib/scan-digest.ts`` so the scheduled
scan digest behaves identically on BOTH push channels (webhook + TG bot): same
nearest-cadence-≥-tf map (floor 1h), same bucket math, same reminder copy.

SHARED-LOGIC CANDIDATE: this duplicates the MCP cadence logic. Per the CLAUDE.md
3-example rule it is flagged for extraction-via-/capabilities at the 3rd consumer;
until then the two test suites (tests/test_cadence_for_timeframe.py here +
tests/scan-digest.test.ts in the MCP repo) pin the identical map by construction.
"""
from __future__ import annotations

from .validators import TF_SECONDS  # the canonical bot-side tf→seconds map (11 TFs)

VALID_CADENCES: tuple[str, ...] = ("1h", "4h", "1d")
_CADENCE_SECONDS: dict[str, int] = {"1h": 3600, "4h": 14_400, "1d": 86_400}


def cadence_for_timeframe(timeframe: str) -> str:
    """Nearest cadence ≥ the timeframe, hard-floored at 1h. 1m–1h→1h, 2h–4h→4h,
    8h–1d→1d. Unknown tf → '1d' (conservative default-deny: slowest = least quota)."""
    sec = TF_SECONDS.get(timeframe)
    if sec is None:
        return "1d"
    if sec <= _CADENCE_SECONDS["1h"]:
        return "1h"
    if sec <= _CADENCE_SECONDS["4h"]:
        return "4h"
    return "1d"


def is_valid_cadence(cadence: object) -> bool:
    return isinstance(cadence, str) and cadence in VALID_CADENCES


def cadence_bucket_epoch(cadence: str, now_sec: int) -> int:
    """`now_sec` floored to the cadence period — the bucket start (idempotency key)."""
    period = _CADENCE_SECONDS[cadence]
    return (now_sec // period) * period


def cadence_faster_than_timeframe(cadence: str, timeframe: str) -> bool:
    """True iff the cadence fires faster than the scan refreshes (→ stronger heads-up)."""
    tf_sec = TF_SECONDS.get(timeframe)
    if tf_sec is None:
        return False
    return _CADENCE_SECONDS[cadence] < tf_sec


def repeats_per_timeframe(cadence: str, timeframe: str) -> int:
    """~How many times a `cadence` digest repeats the same `timeframe` scan."""
    tf_sec = TF_SECONDS.get(timeframe)
    if tf_sec is None:
        return 1
    return max(1, round(tf_sec / _CADENCE_SECONDS[cadence]))


def scan_digest_reminder(cadence: str, timeframe: str) -> str:
    """The create-confirmation reminder copy — MIRRORS the MCP create-response
    (webhook-api.ts). Appends a stronger heads-up when the cadence is faster than tf."""
    msg = (
        f"Digest cadence: {cadence}. Cadence defaults to your timeframe — a {timeframe} scan "
        f"refreshes every {timeframe}, so a faster digest repeats results and draws extra quota. "
        f"Each delivery costs max(1, calls)."
    )
    if cadence_faster_than_timeframe(cadence, timeframe):
        reps = repeats_per_timeframe(cadence, timeframe)
        msg += f" ⚠️ a {cadence} digest on a {timeframe} scan repeats the same calls ~{reps}× and charges each time."
    return msg
