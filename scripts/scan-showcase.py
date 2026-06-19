#!/usr/bin/env python3
"""TG-WATCH-ADOPTION-BROADCAST-W1 (R3): weekly scan-showcase broadcast.

Invoked by ``scan-showcase.sh`` from cron at 13:17 UTC every Monday
(`17 13 * * 1`, off-:00 + collision-free). Steps:
 1. Scan each supported venue via ``scan_trade_calls`` (top-N by OI).
 2. Aggregate fresh (non-HOLD) calls, dedupe by coin (highest confidence),
    take the top 3 cross-venue.
 3. Render the T1-voice body with LIVE-derived asset + venue counts (A4)
    + a one-tap "set a standing scan" button (A5).
 4. Empty → SUPPRESS (A3, anti-spam). Non-empty → broadcast (A2 go-live gate)
    OR preview-to-operator OR dry-run-count.

CLI:
   scan-showcase.py                    # cron: real broadcast IFF ADOPTION_BROADCASTS_LIVE=1
   scan-showcase.py --dry-run          # render + count cohort; no send, no ledger
   scan-showcase.py --preview-operator # send the exact message + button to BOT_ADMIN_CHAT_IDS

Env reads (from /etc/algovault-bot/env via the .sh wrapper):
   PUBLIC_BOT_TOKEN, ALGOVAULT_MCP_URL, ALGOVAULT_INTERNAL_BYPASS_KEY,
   BOT_ADMIN_CHAT_IDS, ADOPTION_BROADCASTS_LIVE, ALGOVAULT_BOT_DB_PATH.

Spec reference: ``Prompt/tg-watch-adoption-broadcast-w1.md`` R3.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the bot package is importable when invoked as a CLI from cron.
_PKG_PARENT = Path(__file__).resolve().parent.parent / "src"
if _PKG_PARENT.is_dir() and str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from algovault_bot import adoption  # noqa: E402
from algovault_bot.broadcast import sendBroadcast, sendDM  # noqa: E402

log = logging.getLogger("scan-showcase")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AlgoVault TG bot weekly scan-showcase")
    p.add_argument("--dry-run", action="store_true", help="Render + count cohort; no send")
    p.add_argument(
        "--preview-operator",
        action="store_true",
        help="Send the rendered showcase (with button) ONLY to BOT_ADMIN_CHAT_IDS",
    )
    p.add_argument("--top-n", type=int, default=adoption.SHOWCASE_TOP_N)
    p.add_argument("--timeframe", default=adoption.SHOWCASE_TIMEFRAME)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    # httpx INFO leaks the bot token via the request URL — silence it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    args = _parse_args(argv or sys.argv[1:])
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    top3, asset_count, venue_count = adoption.fetch_showcase_setups(
        top_n=args.top_n, timeframe=args.timeframe
    )
    body = adoption.render_scan_showcase(top3, asset_count, venue_count)
    log.info(
        "scan_showcase top3=%d assets=%d venues=%d body=%s",
        len(top3), asset_count, venue_count, "yes" if body else "EMPTY",
    )

    broadcast_type = f"{adoption.SCAN_SHOWCASE_TYPE_PREFIX}_{date_str}"
    keyboard = adoption.scan_showcase_keyboard()

    # ── Operator DRY_RUN preview (A1) ─────────────────────────────────────────
    if args.preview_operator:
        ops = adoption.operator_chat_ids()
        if not ops:
            print("PREVIEW_ERROR: BOT_ADMIN_CHAT_IDS unset — no operator target")
            return 2
        if body is None:
            note = (
                "📡 [PREVIEW] No fresh cross-venue setups right now → live mode "
                "SUPPRESSES the weekly scan-showcase today (A3)."
            )
            for chat_id in ops:
                sendDM(chat_id, note)
            print(f"PREVIEW: empty (would suppress); notified {len(ops)} operator(s)")
            return 0
        sent = sum(1 for chat_id in ops if sendDM(chat_id, body, reply_markup=keyboard))
        print(f"PREVIEW: sent showcase sample to {sent}/{len(ops)} operator(s)")
        return 0

    # ── A3 suppress-on-empty ──────────────────────────────────────────────────
    if body is None:
        log.info("scan-showcase suppressed: no fresh setups")
        print(json.dumps({"status": "suppressed_empty"}))
        return 0

    if args.dry_run:
        result = sendBroadcast(body, broadcast_type, dry_run=True, reply_markup=keyboard)
        log.info("scan-showcase dry-run result: %s", result)
        print(json.dumps(result, indent=2))
        return 0

    # ── A2 go-live gate ───────────────────────────────────────────────────────
    if not adoption.adoption_broadcasts_live():
        log.info("ADOPTION_BROADCASTS_LIVE not set — skipping live scan-showcase")
        print(json.dumps({"status": "skipped_not_live", "top3_count": len(top3)}))
        return 0

    result = sendBroadcast(body, broadcast_type, dry_run=False, reply_markup=keyboard)
    log.info("scan-showcase result: %s", result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
