#!/usr/bin/env python3
"""TG-WATCH-ADOPTION-BROADCAST-W1 (R1): first-watch onboarding batch sweep.

Sends the one-time first-watch nudge to EXISTING subscribers who have zero
engagement (no watch + no scan) and have never been nudged. New subscribers
are handled in-bot post-/start (handlers._maybe_send_first_watch_nudge); this
script is the one-shot backfill for the subscribers who joined before the
mechanic existed.

Dedup: ``subscribers.first_watch_nudge_sent_at`` — set ONLY on a successful
send, so a failed/blocked send is retried next run; a successful one is never
re-sent (anti-spam). The segment query already excludes nudged + engaged subs.

CLI:
   first-watch-nudge.py                    # real sweep IFF ADOPTION_BROADCASTS_LIVE=1
   first-watch-nudge.py --dry-run          # count the 0-engagement segment; no send
   first-watch-nudge.py --preview-operator # send ONE sample to BOT_ADMIN_CHAT_IDS

Spec reference: ``Prompt/tg-watch-adoption-broadcast-w1.md`` R1.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_PKG_PARENT = Path(__file__).resolve().parent.parent / "src"
if _PKG_PARENT.is_dir() and str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from algovault_bot import adoption  # noqa: E402
from algovault_bot.broadcast import sendDM  # noqa: E402
from algovault_bot.db import DEFAULT_DB_PATH, Database  # noqa: E402
from algovault_bot.log_setup import log_alert_event  # noqa: E402

log = logging.getLogger("first-watch-nudge")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AlgoVault first-watch onboarding sweep")
    p.add_argument("--dry-run", action="store_true", help="Count segment; no send")
    p.add_argument(
        "--preview-operator", action="store_true",
        help="Send ONE sample nudge to BOT_ADMIN_CHAT_IDS",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    args = _parse_args(argv or sys.argv[1:])
    db = Database(os.environ.get("ALGOVAULT_BOT_DB_PATH", DEFAULT_DB_PATH))
    segment = db.list_zero_engagement_unnudged()
    keyboard = adoption.onboarding_keyboard()
    body = adoption.FIRST_WATCH_NUDGE_TEXT

    if args.preview_operator:
        ops = adoption.operator_chat_ids()
        if not ops:
            print("PREVIEW_ERROR: BOT_ADMIN_CHAT_IDS unset — no operator target")
            return 2
        sent = sum(1 for chat_id in ops if sendDM(chat_id, body, reply_markup=keyboard))
        print(
            f"PREVIEW: sent sample nudge to {sent}/{len(ops)} operator(s); "
            f"live segment = {len(segment)} subscriber(s)"
        )
        return 0

    if args.dry_run:
        print(json.dumps({"status": "dry_run", "segment_size": len(segment), "chat_ids": segment}))
        return 0

    if not adoption.adoption_broadcasts_live():
        log.info("ADOPTION_BROADCASTS_LIVE not set — skipping live first-watch sweep")
        print(json.dumps({"status": "skipped_not_live", "segment_size": len(segment)}))
        return 0

    sent = 0
    failed = 0
    for chat_id in segment:
        # Re-check the dedup flag at send time (defensive against a concurrent run).
        if db.get_first_watch_nudge_sent_at(chat_id) is not None:
            continue
        if sendDM(chat_id, body, reply_markup=keyboard, db_path=db.path):
            db.mark_first_watch_nudge_sent(
                chat_id, datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            log_alert_event("first_watch_nudge_sent", chat_id=chat_id, source="onboarding_sweep")
            sent += 1
        else:
            failed += 1
    log.info("first-watch sweep: sent=%d failed=%d segment=%d", sent, failed, len(segment))
    print(json.dumps({"status": "sent", "sent": sent, "failed": failed, "segment_size": len(segment)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
