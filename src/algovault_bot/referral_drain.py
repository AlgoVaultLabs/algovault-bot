"""REFERRAL-PARITY-NOTIFS-W1 / C2 — drain pending TG referral notifications.

Pulls the engine's pending tg-channel rows (keyed by CODE — the engine never holds a
chat_id), maps code→chat_id via the bot's local cache (subscribers.referral_code, set
on /referral), renders trilingual (referral.format_notification), sends via the rate-safe
Bot API (broadcast.sendDM — backoff + bot_blocked handling), and marks each delivered.

Host-cron driven (every few minutes). Fail-soft per row: a not-yet-cached referrer's row
is LEFT pending (delivered once they next open /referral). NEVER uses send_telegram.sh
(that wrapper is operator-action-required alerts only).
"""
from __future__ import annotations

import logging

from . import referral, referral_client
from .broadcast import sendDM
from .db import DEFAULT_DB_PATH, Database

log = logging.getLogger(__name__)

_VALID_EVENTS = ("friend_joined", "commission_earned")


def drain_referral_notifications(db_path: str = DEFAULT_DB_PATH, dry_run: bool = False) -> dict[str, int]:
    """Deliver pending TG referral notifications. Returns a small result summary."""
    db = Database(db_path)
    pending = referral_client.get_notifications()
    sent = 0
    skipped_uncached = 0
    failed = 0
    for n in pending:
        nid = n.get("id")
        code = n.get("code")
        event = n.get("event")
        payload = n.get("payload") if isinstance(n.get("payload"), dict) else {}
        if not isinstance(nid, int) or not isinstance(code, str) or event not in _VALID_EVENTS:
            continue
        chat_ids = db.chat_ids_for_referral_code(code)
        if not chat_ids:
            # referrer's code not cached yet (hasn't run /referral since deploy) — leave pending
            skipped_uncached += 1
            continue
        ok_any = False
        for chat_id in chat_ids:
            row = db.get_subscriber(chat_id)
            lang = row["lang_code"] if row is not None and "lang_code" in row.keys() else None
            body = referral.format_notification(event, payload, lang)
            if dry_run:
                print(f"[dry-run] chat_id={chat_id} event={event} :: {body}")
                ok_any = True
                continue
            if sendDM(chat_id, body, db_path=db_path):
                ok_any = True
            else:
                failed += 1
        if ok_any and dry_run:
            sent += 1
        elif ok_any:
            referral_client.mark_delivered(nid)  # mark only after a real successful send
            sent += 1
    result = {"pending": len(pending), "sent": sent, "skipped_uncached": skipped_uncached, "failed": failed}
    log.info('{"event":"referral_drain","result":%s}', result)
    return result
