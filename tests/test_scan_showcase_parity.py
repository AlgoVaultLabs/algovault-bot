"""FIX-CONVICTION-CALL-POSTS-W1 — the PYTHON half of the two-sided showcase parity test.

The weekly "📡 This week I scanned N assets across M venues" digest now renders on TWO
surfaces: this bot (Python, the canonical implementation, shipped for months) and the
dev.to market-insight post (TypeScript, new). Python cannot import TypeScript, so the
framing over there is necessarily a MIRROR — and a mirror with no canary is just a fork
that has not drifted yet.

The two repos cannot import each other's suites either, so the contract is a GOLDEN FIXTURE
committed to BOTH: `tests/fixtures/scan-showcase-golden.json`, whose `expected` field was
produced by executing THIS module's `render_scan_showcase`. The TS side
(`tests/scan-showcase-render.test.ts` in crypto-quant-signal-mcp) asserts its renderer
reproduces the same bytes. Either side drifting fails its own repo's CI.

The fixture deliberately exercises all three price buckets, an arrowed driver, a
window-suffixed driver, and a setup with NEITHER drivers nor reasoning — the branches most
likely to get "tidied" independently on one side.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from algovault_bot.adoption import (
    SHOWCASE_MIN_LIQUIDITY_USD,
    render_scan_showcase,
)

FIXTURE = Path(__file__).parent / "fixtures" / "scan-showcase-golden.json"
GOLDEN = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_render_matches_the_golden_fixture_byte_for_byte() -> None:
    out = render_scan_showcase(GOLDEN["setups"], GOLDEN["assetCount"], GOLDEN["venueCount"])
    assert out == GOLDEN["expected"]


def test_fixture_has_not_been_edited_in_place() -> None:
    """Guards the failure mode where a drift is 'fixed' by rewriting the expectation
    rather than the code. Regenerate deliberately and update BOTH repos together."""
    recorded = (FIXTURE.with_suffix(".json.sha256")).read_text(encoding="utf-8").strip()
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == recorded


def test_empty_setups_returns_none_so_the_caller_decides() -> None:
    """None (not "") is load-bearing: this bot SUPPRESSES the broadcast on a quiet week,
    while dev.to publishes an honest market-state note. Both branch on this being None."""
    assert render_scan_showcase([], 900, 9) is None


def test_counts_interpolate_raw_no_thousands_separators() -> None:
    """The TS mirror uses template interpolation; a well-meaning `toLocaleString()` there
    would render '1,200 assets' against this f-string's '1200 assets'. Invisible in review,
    and only reachable at the four-digit counts production actually runs at."""
    out = render_scan_showcase(GOLDEN["setups"], 1200, 12)
    assert out is not None
    assert "scanned 1200 assets across 12 venues" in out
    assert "1,200" not in out


def test_cta_line_is_the_telegram_wording() -> None:
    out = render_scan_showcase(GOLDEN["setups"], 900, 9)
    assert out is not None
    assert out.endswith("Want this on your coins automatically? Set a standing scan: /scanwatch.")


def test_golden_fixture_exercises_the_drift_prone_branches() -> None:
    """A parity test over a trivial fixture passes forever while the real format rots."""
    expected = GOLDEN["expected"]
    assert "$64,171" in expected          # >=1000 -> comma-grouped, 0 decimals
    assert "$1.19" in expected            # >=1    -> 2 decimals
    assert "$0.0412" in expected          # <1     -> 4dp, trailing zeros stripped
    assert "funding elevated ↓" in expected      # lowercased value + bearish arrow
    assert "OI +27.6% (24h) ↑" in expected       # window suffix + bullish arrow
    assert "🔴 KOMA — SELL" in expected          # non-BUY marker
    # KOMA carries neither drivers nor reasoning, so its block is a SINGLE line.
    koma = next(b for b in expected.split("\n\n") if "KOMA" in b)
    assert len(koma.split("\n")) == 1


@pytest.mark.parametrize("field", ["topN", "timeframe", "exchange", "includeReasoning", "minLiquidityUsd"])
def test_scan_call_carries_every_showcase_param(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The liquidity floor must actually reach the wire.

    It is applied SERVER-side (the per-call payload carries no liquidity field, so this
    client has nothing to filter on), which means the only thing proving the floor is in
    effect on THIS surface is that the param is sent. A silently-dropped param would leave
    the bot's digest ungated while the dev.to one is filtered — the exact asymmetry the
    'floor on both surfaces or neither' rule exists to prevent.
    """
    from algovault_bot import adoption

    captured: dict[str, object] = {}

    class _FakeClient:
        def __enter__(self):  # noqa: D105
            return self

        def __exit__(self, *exc):  # noqa: D105
            return False

        def call_tool(self, name, args):  # noqa: D102
            captured["name"] = name
            captured.update(args)
            return {"scanned": 0, "calls": []}

    monkeypatch.setattr(adoption, "from_env", lambda **kw: _FakeClient(), raising=False)
    monkeypatch.setitem(
        __import__("sys").modules, "algovault_bot.mcp_client",
        type("M", (), {"from_env": staticmethod(lambda **kw: _FakeClient())}),
    )
    adoption._scan_one_venue(adoption.SHOWCASE_TOP_N, adoption.SHOWCASE_TIMEFRAME, "BINANCE")
    assert captured["name"] == "scan_trade_calls"
    assert field in captured, f"{field} never reached the scan_trade_calls arguments"
    if field == "minLiquidityUsd":
        assert captured[field] == SHOWCASE_MIN_LIQUIDITY_USD
