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


def timeframe_bucket_epoch(timeframe: str, now_sec: int) -> int:
    """TG-SCANWATCH-TF-CADENCE-W1 (Approach B): `now_sec` floored to the TIMEFRAME period —
    the scanwatch re-scan bucket. Dispatch cadence == the subscription's OWN timeframe (no
    coarsening) — a 5m scanwatch re-scans every 5m, a 1h hourly. Leaves cadence_for_timeframe
    (the MCP scan-digest.ts mirror) + the 1h/4h/1d cadence column untouched. Unknown tf → 1d
    floor (conservative; matches cadence_for_timeframe's unknown-tf default)."""
    period = TF_SECONDS.get(timeframe, 86_400)
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


# ── SCAN-DIGEST-MCP-PARITY-W1 CH3 — the per-call digest-line renderer ──────────
# A faithful MIRROR of src/lib/scan-digest.ts::renderScanDigestLine. Both /scan and
# /scanwatch (and the webhook, via the MCP) render each actionable call through
# render_scan_digest_line (single-derivation); the CH4 canary pins it byte-identical
# to the TS SoT. The factor mapping/arrows mirror the MCP enrichScanCall + receipts.

# Short labels for the receipt factor names (the 📊 drivers line). Unknown factors
# fall back to their raw name.
_FACTOR_LABELS: dict[str, str] = {
    "oi_change_pct": "OI",
    "trend_persistence": "trend persistence",
    "funding_state": "funding",
    "funding_24h_avg": "funding",
    "breakout_pending": "breakout",
    "volume_24h": "vol",
}
_DIR_ARROW: dict[str, str] = {"bullish": " ↑", "bearish": " ↓"}  # neutral → no arrow


def _fmt_price(p: object) -> str:
    """≥1000 → no-decimal w/ comma; ≥1 → 2-decimal; <1 → 4-decimal stripped.
    MIRRORS the MCP fmtScanPrice."""
    try:
        v = float(p)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _trim_reasoning(text: object, max_len: int = 110) -> str:
    """First sentence of the engine reasoning, capped — MIRRORS the MCP trimReasoning."""
    if not isinstance(text, str) or not text.strip():
        return ""
    first = text.strip().split(". ")[0].rstrip(".")
    return first[: max_len - 1].rstrip() + "…" if len(first) > max_len else first


def _render_drivers(factors: list[dict], oi_window: str | None = None) -> str:
    """Top ≤3 factors → 'trend persistence HIGH · funding elevated ↑ · OI +10.0% (24h) ↑'.
    SCAN-DIGEST-MCP-PARITY-W1 CH3: the OI driver carries its (window). MIRRORS the MCP
    renderDrivers."""
    parts: list[str] = []
    for f in factors[:3]:
        name = str(f.get("factor", ""))
        label = _FACTOR_LABELS.get(name, name)
        val = f.get("value", "")
        if name in ("funding_state", "breakout_pending") and isinstance(val, str):
            val = val.lower()
        if name == "oi_change_pct" and oi_window:
            val = f"{val} ({oi_window})"
        arrow = _DIR_ARROW.get(str(f.get("direction", "")), "")
        piece = f"{label} {val}{arrow}".strip()
        if piece:
            parts.append(piece)
    return " · ".join(parts)


def render_scan_digest_line(call: dict) -> str:
    """ONE actionable scan call as the digest block — the SoT MIRRORED from the MCP
    renderScanDigestLine (src/lib/scan-digest.ts); the CH4 canary pins them byte-identical:

        🟢 CL — BUY @ $71.49 · 60% conviction · TRENDING_UP
           📊 trend persistence HIGH · funding elevated ↑ · OI +10.0% (24h) ↑
           💡 Trending regime, upward bias

    🟢 BUY / 🔴 SELL. The 📊 line is omitted with no drivers, 💡 with no reasoning,
    the price clause when price is absent."""
    mark = "🟢" if call.get("call") == "BUY" else "🔴"
    price = _fmt_price(call.get("price")) if call.get("price") is not None else ""
    price_str = f" @ ${price}" if price else ""
    lines = [
        f"{mark} {call.get('coin')} — {call.get('call')}{price_str} · "
        f"{call.get('confidence')}% conviction · {call.get('regime')}"
    ]
    drivers = _render_drivers(call.get("factors") or [], call.get("oi_change_window"))
    if drivers:
        lines.append(f"   📊 {drivers}")
    why = _trim_reasoning(call.get("reasoning"))
    if why:
        lines.append(f"   💡 {why}")
    return "\n".join(lines)
