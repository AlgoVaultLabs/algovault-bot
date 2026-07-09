"""TG-SCANWATCH-TF-CADENCE-W1 — TF-based scanwatch dispatch bucket (Approach B) + proof the
MCP-mirror `cadence_for_timeframe` stays untouched."""
from __future__ import annotations

from algovault_bot.scan_digest import cadence_for_timeframe, timeframe_bucket_epoch

_NOW = 1_700_002_800  # aligned to 5m (300s), 15m (900s), and 1h (3600s)


def test_timeframe_bucket_epoch_is_the_tf_period() -> None:
    # bucket period == the timeframe's own seconds → dispatch cadence == the TF (no coarsening).
    assert timeframe_bucket_epoch("5m", _NOW) == _NOW
    assert timeframe_bucket_epoch("5m", _NOW + 299) == _NOW          # same 5m bucket
    assert timeframe_bucket_epoch("5m", _NOW + 300) == _NOW + 300    # next 5m bucket
    assert timeframe_bucket_epoch("15m", _NOW + 300) == _NOW         # still the same 15m bucket
    assert timeframe_bucket_epoch("15m", _NOW + 900) == _NOW + 900
    assert timeframe_bucket_epoch("1h", _NOW + 900) == _NOW          # same 1h bucket
    assert timeframe_bucket_epoch("1h", _NOW + 3600) == _NOW + 3600


def test_timeframe_bucket_epoch_all_11_tfs_resolve() -> None:
    # every TF resolves with no KeyError (the identity map covers 1m–1d).
    for tf in ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"):
        b = timeframe_bucket_epoch(tf, _NOW)
        assert isinstance(b, int) and b <= _NOW


def test_cadence_for_timeframe_unchanged_mcp_mirror() -> None:
    # Approach B leaves cadence_for_timeframe (the MCP scan-digest.ts mirror) BYTE-UNCHANGED —
    # it still coarsens (1m–1h → 1h); the vestigial cadence column keeps 1h/4h/1d. Dispatch
    # cadence now comes from timeframe_bucket_epoch, not this function.
    assert cadence_for_timeframe("5m") == "1h"
    assert cadence_for_timeframe("15m") == "1h"
    assert cadence_for_timeframe("4h") == "4h"
    assert cadence_for_timeframe("1d") == "1d"
