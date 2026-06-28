"""TG-WATCH-ADOPTION-BROADCAST-W1 (2026-06-19): watch/scan adoption mechanics.

Turns passive subscribers into active watchers via three reuse-only surfaces:
  1. First-watch onboarding nudge (one-time, deduped) — DM to 0-engagement subs.
  2. Daily-digest per-setup one-tap watch button (enriches the existing broadcast).
  3. Weekly scan-showcase broadcast (live multi-venue scan) + one-tap scan button.

Per architect ratification (2026-06-19):
  - A5 = inline BUTTONS (callback_data), not typed commands → exact source
    attribution + lower friction. Same approved message text; the CTA is a tap.
  - A2 = go-live gated: real cohort sends fire ONLY when
    ``ADOPTION_BROADCASTS_LIVE=1``; until then crons/handlers no-op or
    preview to the operator (BOT_ADMIN_CHAT_IDS). The flag flip is the go-live.
  - A3 = suppress on empty (no broadcast on a quiet day).
  - A4 = scan-showcase uses ``scan_trade_calls`` with LIVE-derived asset/venue
    counts (never hardcoded 290/5).

Design notes
============
- Button builders + callback parsers are PURE (no network/DB) so the digest /
  onboarding / showcase render tests assert callback_data by construction.
- ``watch_created`` / ``scan_watch_created`` land in ``alerts.log`` via
  ``log_alert_event`` (the bot's canonical analytics; the MCP-side
  ``funnel-snapshot.ts`` greps the same stream). No new analytics backend.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .log_setup import log_alert_event
from .scan_digest import render_scan_digest_line
from .validators import DEFAULT_EXCHANGE, EXCHANGES

log = logging.getLogger(__name__)

# ── Source attribution (callback_data + emitted events) ──────────────────────
SOURCE_ONBOARDING = "onboarding"
SOURCE_DIGEST = "digest"
SOURCE_SCAN_SHOWCASE = "scan_showcase"
SOURCE_COMMAND = "command"
_VALID_SOURCES = frozenset(
    {SOURCE_ONBOARDING, SOURCE_DIGEST, SOURCE_SCAN_SHOWCASE, SOURCE_COMMAND}
)

# callback_data prefixes (Telegram caps callback_data at 64 bytes).
WATCH_CB_PREFIX = "wb"
SCANWATCH_CB_PREFIX = "sw"
# Regex patterns for CallbackQueryHandler registration.
WATCH_CB_PATTERN = r"^wb:"
SCANWATCH_CB_PATTERN = r"^sw:"

# Default scan_watch a scan-showcase tap creates — mirrors `/scanwatch` no-args.
SCANWATCH_DEFAULT_TOP_N = 20
SCANWATCH_DEFAULT_TF = "15m"
SCANWATCH_DEFAULT_EXCHANGE = DEFAULT_EXCHANGE
SCANWATCH_DEFAULT_CADENCE = "1h"

# ── Approved copy (pre-flagged; Mr.1-approved 2026-06-19) ────────────────────
# Buttons carry the one-tap action; the text keeps the typed-command hint too
# so power users can still type. T1 voice, CTA-ended, no profit/accuracy claims.
FIRST_WATCH_NUDGE_TEXT = (
    "You're in — but you're not watching anything yet. Set a watch and I'll "
    "ping you the moment a coin's verdict flips. Start with the big one: "
    "tap /watch BTC 1h (add more anytime: /watch ETH 4h)."
)

SCAN_SHOWCASE_TYPE_PREFIX = "scan_showcase"  # tg_broadcasts.broadcast_type prefix
# How many top perps per venue the weekly showcase ranks (by OI) before
# aggregating the top-3 fresh setups cross-venue.
SHOWCASE_TOP_N = 100
SHOWCASE_TIMEFRAME = "1h"


# ── Go-live gating ───────────────────────────────────────────────────────────

def adoption_broadcasts_live() -> bool:
    """True iff real cohort sends are enabled (the go-live flag). Default OFF:
    until Mr.1 flips ``ADOPTION_BROADCASTS_LIVE=1`` in /etc/algovault-bot/env,
    the in-bot nudge no-ops and the digest/showcase crons skip the real
    broadcast (operator-preview / dry-run modes still work)."""
    raw = os.environ.get("ADOPTION_BROADCASTS_LIVE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def operator_chat_ids() -> list[int]:
    """Parse BOT_ADMIN_CHAT_IDS (CSV) — the DRY_RUN / preview target(s).
    Same env var that gates admin /stats. Empty list when unset/garbage."""
    raw = os.environ.get("BOT_ADMIN_CHAT_IDS", "").strip()
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            log.warning("BOT_ADMIN_CHAT_IDS: ignoring non-int token %r", tok)
    return out


# ── Callback data: build + parse (pure) ──────────────────────────────────────

def build_watch_callback(coin: str, timeframe: str, exchange: str, source: str) -> str:
    """``wb:<COIN>:<TF>:<EXCH>:<SOURCE>`` — stays well under the 64-byte cap."""
    return f"{WATCH_CB_PREFIX}:{coin.upper()}:{timeframe}:{exchange.upper()}:{source}"


def parse_watch_callback(data: str) -> tuple[str, str, str, str] | None:
    """Parse a watch-button callback into (coin, tf, exch, source). None on a
    malformed payload or an unknown source (default-deny)."""
    if not data or not data.startswith(WATCH_CB_PREFIX + ":"):
        return None
    parts = data.split(":")
    if len(parts) != 5:
        return None
    _, coin, tf, exch, source = parts
    if source not in _VALID_SOURCES or not coin or not tf or not exch:
        return None
    return coin.upper(), tf, exch.upper(), source


def build_scanwatch_callback(source: str) -> str:
    return f"{SCANWATCH_CB_PREFIX}:{source}"


def parse_scanwatch_callback(data: str) -> str | None:
    if not data or not data.startswith(SCANWATCH_CB_PREFIX + ":"):
        return None
    parts = data.split(":")
    if len(parts) != 2 or parts[1] not in _VALID_SOURCES:
        return None
    return parts[1]


# ── Inline keyboards (pure) ──────────────────────────────────────────────────

def _watch_button(coin: str, timeframe: str, exchange: str, source: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        f"👁 Watch {coin.upper()} {timeframe}",
        callback_data=build_watch_callback(coin, timeframe, exchange, source),
    )


def onboarding_keyboard() -> InlineKeyboardMarkup:
    """One-tap buttons for the first-watch nudge (BTC 1h + ETH 4h, per copy)."""
    return InlineKeyboardMarkup(
        [
            [_watch_button("BTC", "1h", DEFAULT_EXCHANGE, SOURCE_ONBOARDING)],
            [_watch_button("ETH", "4h", DEFAULT_EXCHANGE, SOURCE_ONBOARDING)],
        ]
    )


def digest_keyboard(top3: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    """One watch button per top-3 digest setup. None when there are no setups."""
    rows: list[list[InlineKeyboardButton]] = []
    for s in top3:
        coin = str(s.get("coin") or s.get("symbol") or "").upper()
        tf = str(s.get("timeframe") or s.get("tf") or "1h")
        exch = str(s.get("exchange") or DEFAULT_EXCHANGE).upper()
        if exch not in EXCHANGES:
            exch = DEFAULT_EXCHANGE
        if not coin:
            continue
        rows.append([_watch_button(coin, tf, exch, SOURCE_DIGEST)])
    return InlineKeyboardMarkup(rows) if rows else None


def scan_showcase_keyboard() -> InlineKeyboardMarkup:
    """One-tap 'set a standing scan' button for the weekly showcase."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "📡 Set a standing scan",
            callback_data=build_scanwatch_callback(SOURCE_SCAN_SHOWCASE),
        )]]
    )


# ── Analytics emit (watch_created / scan_watch_created) ───────────────────────

def emit_watch_created(
    chat_id: int, coin: str, timeframe: str, exchange: str, source: str, created: bool
) -> None:
    """Emit a ``watch_created`` analytics event (alerts.log JSON line).

    Fired from BOTH the button-callback path (exact source) and the typed
    ``/watch`` commit path (source=command) — one shared emit so the funnel
    has a single watch-creation signal. ``created`` is False when the tap hit
    an already-existing watch (still useful as an engagement signal)."""
    try:
        log_alert_event(
            "watch_created",
            chat_id=chat_id,
            coin=coin,
            timeframe=timeframe,
            exchange=exchange,
            source=source,
            created=created,
        )
    except Exception as e:  # pragma: no cover — observational, never block UX
        log.warning("watch_created emit failed chat_id=%s: %s", chat_id, e)


def emit_scan_watch_created(
    chat_id: int, top_n: int, timeframe: str, exchange: str, cadence: str,
    source: str, created: bool,
) -> None:
    """Emit a ``scan_watch_created`` analytics event (alerts.log JSON line)."""
    try:
        log_alert_event(
            "scan_watch_created",
            chat_id=chat_id,
            top_n=top_n,
            timeframe=timeframe,
            exchange=exchange,
            cadence=cadence,
            source=source,
            created=created,
        )
    except Exception as e:  # pragma: no cover
        log.warning("scan_watch_created emit failed chat_id=%s: %s", chat_id, e)


# ── Weekly scan-showcase: live multi-venue scan + render ──────────────────────

def supported_venues() -> list[str]:
    """The live venue set (SoT = validators.EXCHANGES). Sorted for determinism."""
    return sorted(EXCHANGES)


def _scan_one_venue(top_n: int, timeframe: str, exchange: str) -> dict[str, Any]:
    """Live scan_trade_calls for ONE venue. Test seam: monkeypatch THIS fn."""
    from .mcp_client import from_env  # local import → keep module import-light

    with from_env() as cli:
        return cli.call_tool(
            "scan_trade_calls",
            # OPS-SCAN-SHOWCASE-ENRICH-W1: includeReasoning enriches each call
            # (price/factors/reasoning/oi_change_window via enrichScanCall) so the
            # showcase renders the canonical digest line (render_scan_digest_line) —
            # the SAME render /scan + /scanwatch use (single-derivation). No billing
            # change: the showcase broadcast does not meter per call (zero-user-cost,
            # digest-wave Q3c precedent).
            {"topN": top_n, "timeframe": timeframe, "exchange": exchange,
             "includeReasoning": True},
        )


def fetch_showcase_setups(
    top_n: int = SHOWCASE_TOP_N,
    timeframe: str = SHOWCASE_TIMEFRAME,
    venues: list[str] | None = None,
    scan_fn: Callable[[int, str, str], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Scan each supported venue, aggregate fresh (non-HOLD) calls, dedupe by
    coin (keep the highest-confidence venue), and return
    ``(top3, asset_count, venue_count)``.

    - ``asset_count`` = total perps scanned across all successful venues
      (live, summed from each scan's ``scanned``) — the "X assets" figure.
    - ``venue_count`` = number of venues that scanned without error — the
      "across Y venues" figure. Both LIVE-derived (A4); never hardcoded.
    Per-venue errors are tolerated (skipped); a venue that raises does not
    abort the showcase.
    """
    venues = venues or supported_venues()
    scan = scan_fn or _scan_one_venue
    asset_count = 0
    venue_count = 0
    best_by_coin: dict[str, dict[str, Any]] = {}
    for venue in venues:
        try:
            result = scan(top_n, timeframe, venue)
        except Exception as e:  # noqa: BLE001 — one bad venue must not abort
            log.warning("scan-showcase venue %s failed: %s", venue, e)
            continue
        if not isinstance(result, dict):
            continue
        venue_count += 1
        try:
            asset_count += int(result.get("scanned") or 0)
        except (TypeError, ValueError):
            pass
        for c in result.get("calls") or []:
            call = str(c.get("call") or "").upper()
            if call in ("", "HOLD"):
                continue
            coin = str(c.get("coin") or c.get("symbol") or "").upper()
            if not coin:
                continue
            try:
                conf = float(c.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            # OPS-SCAN-SHOWCASE-ENRICH-W1: PRESERVE the enriched call as-is
            # (price/factors/reasoning/oi_change_window from includeReasoning) so
            # render_scan_showcase can project the canonical digest line byte-for-byte.
            # We only normalize the coin/call keys + stamp the winning venue/timeframe;
            # the raw confidence (int) is kept for render parity, `conf` (float) sorts.
            row = dict(c)
            row["coin"] = coin
            row["call"] = call
            row["exchange"] = venue
            row["timeframe"] = timeframe
            prev = best_by_coin.get(coin)
            if prev is None or conf > float(prev.get("confidence") or 0):
                best_by_coin[coin] = row
    top3 = sorted(
        best_by_coin.values(),
        key=lambda r: float(r.get("confidence") or 0),
        reverse=True,
    )[:3]
    return top3, asset_count, venue_count


def render_scan_showcase(
    top3: list[dict[str, Any]], asset_count: int, venue_count: int
) -> str | None:
    """Render the weekly scan-showcase body. Returns None when there are no
    fresh setups → caller SUPPRESSES the broadcast (A3, anti-spam).

    OPS-SCAN-SHOWCASE-ENRICH-W1: each setup renders through the canonical per-call
    digest line (``render_scan_digest_line``) — the SAME function /scan + /scanwatch
    project from — so the showcase can never fork the digest format (single-derivation;
    the last bot render surface folded in). The showcase's own framing (header + live
    counts + CTA) is PRESERVED; only the per-call lines unify on the enriched canonical
    block. The winning-venue annotation drops (the canonical line carries no venue —
    venue lives in the header's "across Y venues" count)."""
    if not top3:
        return None
    setups = "\n\n".join(render_scan_digest_line(s) for s in top3)
    return "\n".join(
        [
            f"📡 This week I scanned {asset_count} assets across {venue_count} venues.",
            "",
            "Top fresh setups:",
            "",
            setups,
            "",
            "Want this on your coins automatically? Set a standing scan: /scanwatch.",
        ]
    )
