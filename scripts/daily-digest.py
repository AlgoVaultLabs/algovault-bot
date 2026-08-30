#!/usr/bin/env python3
"""TG-BROADCAST-STACK-W1 CH2 (2026-05-28): daily-digest content generator.

Invoked by ``daily-digest.sh`` from cron at 12:03 UTC daily. Steps:
 1. Call ``scan_funding_arb`` via McpClient → top cross-venue setups.
 2. Filter HOLDs + min confidence ≥ 75.
 3. Take top 3 by (confidence × spread × tier-weight).
 4. Render T1-voice body (≤500 chars) OR empty-state fallback.
 5. Call ``sendBroadcast(body, 'daily_digest_YYYY-MM-DD', dry_run)``.

CLI:
   daily-digest.py                                 # full broadcast at 12:03 UTC
   daily-digest.py --dry-run                       # render body + count cohort; no send
   daily-digest.py --dry-run --cohort-override=mr1-only
                                                   # spec's verification-gate variant;
                                                   # renders body + reports would_send=1
                                                   # against the explicit Mr.1 chat_id

Env reads:
   ALGOVAULT_BOT_TOKEN  — required for non-dry-run fires
   MCP_URL              — defaults to http://127.0.0.1:3000/mcp
   MCP_INTERNAL_BYPASS_KEY — defaults to empty (allows public access)
   ALGOVAULT_BOT_DB_PATH — defaults to /var/lib/algovault-bot/state.db

Spec reference: ``Prompt/tg-broadcast-stack-w1.md`` Chapter C2.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the bot package is importable when invoked as a CLI from cron.
_PKG_PARENT = Path(__file__).resolve().parent.parent / "src"
if _PKG_PARENT.is_dir() and str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from algovault_bot import adoption  # noqa: E402
from algovault_bot.broadcast import sendBroadcast, sendDM  # noqa: E402
from algovault_bot.mcp_client import McpClient, McpClientConfig, McpError  # noqa: E402


log = logging.getLogger("daily-digest")

DEFAULT_MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:3000/mcp")
DEFAULT_INTERNAL_BYPASS_KEY = os.environ.get("MCP_INTERNAL_BYPASS_KEY", "")
DEFAULT_DB_PATH = os.environ.get(
    "ALGOVAULT_BOT_DB_PATH", "/var/lib/algovault-bot/state.db"
)
MIN_CONFIDENCE = 75
# Raised from 500 → 900 for the per-setup /watch CTA lines (R2). Still far
# under Telegram's 4096 ceiling; inline buttons are separate from body length.
MAX_DIGEST_CHARS = 900
DIGEST_BODY_PREFIX = "📊 AlgoVault Daily Digest"


# ── Top-3 ranking ────────────────────────────────────────────────────────

def _tier_weight(tier: str | None) -> float:
    """Tier-weighted preference: T1 + T2 majors favored, T3/T4 down-weighted."""
    if not tier:
        return 0.5
    t = tier.upper()
    return {"T1": 1.0, "T2": 0.8, "T3": 0.5, "T4": 0.3}.get(t, 0.5)


def _setup_score(setup: dict[str, Any]) -> float:
    """Composite score: confidence × cross-venue spread × tier weight."""
    confidence = float(setup.get("confidence") or 0)
    spread_bps = abs(float(setup.get("spread_bps") or 0))
    tier_w = _tier_weight(setup.get("tier"))
    return confidence * spread_bps * tier_w


def _filter_setups(raw_setups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop HOLDs + min confidence floor."""
    out: list[dict[str, Any]] = []
    for s in raw_setups:
        verdict = str(s.get("verdict") or s.get("call") or "").upper()
        if verdict == "HOLD" or not verdict:
            continue
        conf = float(s.get("confidence") or 0)
        if conf < MIN_CONFIDENCE:
            continue
        out.append(s)
    return out


def _rank_top_3(setups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(setups, key=_setup_score, reverse=True)[:3]


# ── Body renderers ───────────────────────────────────────────────────────

# TG-WATCH-ADOPTION-BROADCAST-W1: each top-3 setup is watched on this TF by
# default (funding-arb setups carry no native TF). The one-tap button + the
# typed-command hint both target this TF for the setup's coin.
DIGEST_WATCH_TF = "1h"


def render_digest_body(top3: list[dict[str, Any]], date_str: str) -> str:
    """T1-voice body. Outcome-framed; ≤2 sentences per line.

    TG-WATCH-ADOPTION-BROADCAST-W1 (R2): each top-3 setup ends with a one-tap
    ``/watch {COIN} {TF}`` CTA (the inline button carries the same action with
    source attribution — see ``adoption.digest_keyboard``). Empty top3 is
    handled by the caller (A3 suppress-on-empty), not rendered here."""
    if not top3:
        return render_empty_state(date_str)

    lines: list[str] = [f"{DIGEST_BODY_PREFIX} — {date_str}", "", "Top 3 cross-venue setups:"]
    for i, s in enumerate(top3, start=1):
        coin = str(s.get("coin") or s.get("symbol") or "?").upper()
        verdict = str(s.get("verdict") or s.get("call") or "?").upper()
        conf = int(float(s.get("confidence") or 0))
        spread = float(s.get("spread_bps") or 0)
        venue_pair = s.get("venue_pair") or s.get("venues") or "—"
        spread_str = f"{spread:+.0f} bps"
        tf = str(s.get("timeframe") or DIGEST_WATCH_TF)
        lines.append(
            f"{i}. {coin} {verdict} {conf}% · {spread_str} ({venue_pair})"
        )
        # R2 per-setup CTA (approved copy). The button below does the same tap.
        lines.append(f"   → never miss the next flip: /watch {coin} {tf}")
    lines.append("")
    lines.append("👇 One tap to start watching · 200 free alerts/month.")
    body = "\n".join(lines)
    # Hard cap; truncate body lines (not closing CTA) if absurdly long.
    if len(body) <= MAX_DIGEST_CHARS:
        return body
    return body[: MAX_DIGEST_CHARS - 3] + "..."


def render_empty_state(date_str: str) -> str:
    """T1-voice empty-state fallback — concise + actionable + on-brand."""
    return (
        f"{DIGEST_BODY_PREFIX} — {date_str}\n\n"
        "Markets quiet last 24h — no high-confidence cross-venue setups.\n\n"
        "Check t.me/algovaultofficialbot — type /watch <COIN> <TF> <EXCHANGE> "
        "to set on-demand alerts."
    )


# ── MCP probe ────────────────────────────────────────────────────────────

def fetch_top_setups(mcp_url: str, bypass_key: str) -> list[dict[str, Any]]:
    """Call scan_funding_arb via McpClient + return the raw setups list.

    Empty list on McpError or unexpected response shape; logger warns.
    """
    cfg = McpClientConfig(
        url=mcp_url,
        internal_bypass_key=bypass_key,
        client_name="algovault-bot-daily-digest",
        client_version="1.0",
    )
    try:
        with McpClient(cfg) as mcp:
            result = mcp.call_tool("scan_funding_arb", {})
    except McpError as e:
        log.warning("scan_funding_arb failed: %s", e)
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("scan_funding_arb unexpected error: %s", e)
        return []

    # Tool response shape varies; accept multiple common shapes.
    if isinstance(result, dict):
        for key in ("opportunities", "setups", "results", "items"):
            if isinstance(result.get(key), list):
                return result[key]
        if isinstance(result.get("data"), list):
            return result["data"]
    if isinstance(result, list):
        return result
    log.info("scan_funding_arb returned non-list shape: %r", type(result))
    return []


# ── CLI ──────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AlgoVault TG bot daily digest")
    p.add_argument("--dry-run", action="store_true", help="Skip the actual broadcast")
    p.add_argument(
        "--cohort-override",
        default="",
        help='Restrict cohort (only "mr1-only" supported; for verification-gate use)',
    )
    p.add_argument(
        "--preview-operator",
        action="store_true",
        help="Send the rendered digest (with buttons) ONLY to BOT_ADMIN_CHAT_IDS "
        "(operator DRY_RUN preview); does not broadcast or touch the ledger.",
    )
    p.add_argument("--mcp-url", default=DEFAULT_MCP_URL, help="MCP server URL")
    p.add_argument(
        "--bypass-key",
        default=DEFAULT_INTERNAL_BYPASS_KEY,
        help="MCP internal-bypass key (header)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv or sys.argv[1:])
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    raw = fetch_top_setups(args.mcp_url, args.bypass_key)
    filtered = _filter_setups(raw)
    top3 = _rank_top_3(filtered)
    # TG-WATCH-ADOPTION-BROADCAST-W1 (R2): one-tap watch button per setup.
    keyboard = adoption.digest_keyboard(top3) if top3 else None
    body = render_digest_body(top3, date_str)
    log.info("digest_body_chars=%d top3_count=%d raw_count=%d", len(body), len(top3), len(raw))

    broadcast_type = f"daily_digest_{date_str}"

    # ── Operator DRY_RUN preview (A1) — send the exact rendered message + buttons
    # only to BOT_ADMIN_CHAT_IDS, no broadcast, no ledger write. ───────────────
    if args.preview_operator:
        ops = adoption.operator_chat_ids()
        if not ops:
            print("PREVIEW_ERROR: BOT_ADMIN_CHAT_IDS unset — no operator target")
            return 2
        if not top3:
            note = (
                f"{DIGEST_BODY_PREFIX} — {date_str}\n\n[PREVIEW] No high-confidence "
                "setups right now → live mode SUPPRESSES the digest today (A3)."
            )
            for chat_id in ops:
                sendDM(chat_id, note)
            print(f"PREVIEW: empty (would suppress); notified {len(ops)} operator(s)")
            return 0
        sent = sum(1 for chat_id in ops if sendDM(chat_id, body, reply_markup=keyboard))
        print(f"PREVIEW: sent digest sample to {sent}/{len(ops)} operator(s); buttons={bool(keyboard)}")
        return 0

    if args.cohort_override == "mr1-only":
        # Legacy verification-gate path — render body, print result, no real send.
        result_payload = {
            "status": "dry_run", "would_send": 1, "would_skip_blocked": 0,
            "cohort_override": "mr1-only", "body_chars": len(body), "body": body,
        }
        print(f"DRY_RUN_BROADCAST: would_send=1 skipped=0 cohort=mr1-only chars={len(body)}")
        print(json.dumps(result_payload, indent=2))
        return 0

    # ── A3 suppress-on-empty: a quiet day sends NO message (anti-spam). ────────
    if not top3:
        log.info("digest suppressed: no high-confidence setups (top3 empty)")
        print(json.dumps({"status": "suppressed_empty", "top3_count": 0}))
        return 0

    if args.dry_run:
        result = sendBroadcast(body, broadcast_type, dry_run=True, reply_markup=keyboard)
        log.info("sendBroadcast dry-run result: %s", result)
        print(json.dumps(result, indent=2))
        return 0

    # ── A2 go-live gate: real broadcast fires ONLY when the flag is set. ───────
    if not adoption.adoption_broadcasts_live():
        log.info("ADOPTION_BROADCASTS_LIVE not set — skipping live digest broadcast")
        print(json.dumps({"status": "skipped_not_live", "top3_count": len(top3)}))
        return 0

    result = sendBroadcast(body, broadcast_type, dry_run=False, reply_markup=keyboard)
    log.info("sendBroadcast result: %s", result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
