"""TG-BROADCAST-STACK-W1 CH1 (2026-05-28): broadcast helpers.

Reusable primitives for outbound TG bot fanout:
- ``sendBroadcast(body, broadcast_type, dry_run)`` — iterate non-blocked
  subscribers, fire one message per subscriber, log to ``tg_broadcasts``
  table with per-event idempotency (skip re-fire within same day).
- ``sendDM(chat_id, body)`` — single-target send with retry/backoff.

Design notes
============
- Uses the existing ``TELEGRAM_GLOBAL_SEMAPHORE`` (semaphore=25) at
  ``rate_limit.py`` to stay under Telegram's 30 msg/sec ceiling.
- 3-attempt exponential backoff (1s / 2s / 4s) per send. ``Forbidden``
  errors trigger ``mark_subscriber_blocked()`` once and DO NOT retry
  (per CLAUDE.md `Cohort coverage` rule from spec).
- ``event_id`` = ``<broadcast_type>:<YYYY-MM-DD>:<body_hash[:8]>``.
  Per-day idempotency: re-firing the same broadcast_type with the same
  body within 24h is suppressed at the iterate-start boundary.
- Sync wrappers (``sendBroadcast`` / ``sendDM``) are for CLI / cron-script
  use (e.g. ``scripts/daily-digest.py`` will call ``sendBroadcast`` directly).
  Async helpers (``send_broadcast_async`` / ``send_dm_async``) take a
  pre-instantiated ``Bot`` + ``Database`` for in-bot use (e.g. C4 command
  handlers calling sendDM to acknowledge the /unlock_premium_alerts tap).

Return shapes
=============
- ``sendBroadcast(..., dry_run=True)`` returns
  ``{"status": "dry_run", "would_send": int, "would_skip_blocked": int,
    "event_id": str}``.
- ``sendBroadcast(..., dry_run=False)`` returns
  ``{"status": "sent" | "suppressed_duplicate", "sent": int,
    "skipped": int, "failed": int, "event_id": str}``.
- ``sendDM`` returns ``True`` if the message landed (within retries),
  ``False`` if the subscriber is blocked or 3 attempts all failed.

Spec reference: ``Prompt/tg-broadcast-stack-w1.md`` Chapter C1.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import Forbidden, TelegramError

from .db import DEFAULT_DB_PATH, Database
from .log_setup import log_alert_event
from .rate_limit import TELEGRAM_GLOBAL_SEMAPHORE

log = logging.getLogger(__name__)

# Number of sequential per-subscriber send attempts before giving up
# (Forbidden errors short-circuit immediately; no retry on blocked users).
SEND_MAX_ATTEMPTS = 3
# Exponential backoff schedule (seconds). Length == SEND_MAX_ATTEMPTS-1
# (no wait after the last attempt).
SEND_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)


def _compute_event_id(broadcast_type: str, body: str, day: str | None = None) -> str:
    """Compose an idempotent event_id for the tg_broadcasts ledger.

    ``day`` defaults to today's UTC date (YYYY-MM-DD) so two broadcasts
    of the same type+body fired on consecutive days both record cleanly,
    but a re-fire within the same day is suppressed.
    """
    if day is None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return f"{broadcast_type}:{day}:{body_hash}"


def _event_already_fired(db_path: str, event_id: str) -> bool:
    """Idempotency probe — returns True if tg_broadcasts has a row with
    this event_id. Safe against fresh DBs (no table) by best-effort check.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            cur = conn.execute(
                "SELECT 1 FROM tg_broadcasts WHERE event_id = ? LIMIT 1",
                (event_id,),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # Table doesn't exist yet (pre-migration); treat as "not fired".
        return False


def _record_broadcast(
    db_path: str,
    event_id: str,
    broadcast_type: str,
    body_hash: str,
    sent_count: int,
    skipped_count: int,
    failed_count: int,
) -> None:
    """Persist a broadcast-fire ledger row. Idempotent on event_id PK."""
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO tg_broadcasts
                   (event_id, broadcast_type, body_hash, sent_count,
                    skipped_count, failed_count, fired_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    broadcast_type,
                    body_hash,
                    sent_count,
                    skipped_count,
                    failed_count,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        log.warning("tg_broadcasts INSERT skipped: %s", e)


def _list_non_blocked_chat_ids(db_path: str) -> list[int]:
    """SELECT chat_id from subscribers WHERE bot_blocked_at IS NULL.

    Returns the cohort the broadcast iterates over. Empty list on fresh DB.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            cur = conn.execute(
                "SELECT chat_id FROM subscribers WHERE bot_blocked_at IS NULL"
            )
            return [int(row[0]) for row in cur.fetchall()]
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return []


async def _send_with_retry(
    bot: Bot,
    chat_id: int,
    text: str,
    db: Database | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> tuple[bool, str]:
    """Send a single message with 3-attempt exponential backoff. Returns
    (success, reason_code). Forbidden short-circuits to (False, "blocked")
    and marks the subscriber via db.mark_subscriber_blocked() if db given.

    ``reply_markup`` (TG-WATCH-ADOPTION-BROADCAST-W1) attaches an inline
    keyboard (the one-tap watch / scan CTAs); None keeps the legacy
    text-only behavior byte-for-byte.
    """
    last_err: str = "unknown"
    for attempt in range(SEND_MAX_ATTEMPTS):
        try:
            async with TELEGRAM_GLOBAL_SEMAPHORE:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    disable_web_page_preview=True,
                    parse_mode=None,
                    reply_markup=reply_markup,
                )
            return True, "sent"
        except Forbidden as e:
            # Subscriber blocked the bot — DO NOT retry; mark + skip.
            if db is not None:
                try:
                    db.mark_subscriber_blocked(
                        chat_id,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )
                except Exception:
                    pass
            log.info("Forbidden chat_id=%s err=%s", chat_id, e)
            return False, "blocked"
        except TelegramError as e:
            last_err = str(e)
            # Retryable error — back off + try again.
            if attempt < SEND_MAX_ATTEMPTS - 1:
                await asyncio.sleep(SEND_BACKOFF_SECONDS[attempt])
            continue
    log.warning("send failed after retries chat_id=%s err=%s", chat_id, last_err)
    return False, "failed"


async def send_broadcast_async(
    bot: Bot,
    db: Database,
    body: str,
    broadcast_type: str,
    dry_run: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> dict[str, Any]:
    """Async core for sendBroadcast — iterate non-blocked subscribers + fire.

    Use this from inside an existing async context (e.g. command handler).
    For sync entry-points (cron scripts), prefer :func:`sendBroadcast`.

    Idempotency: dry-run does NOT consult the tg_broadcasts ledger
    (callers want to count cohort without affecting state). Real fire
    DOES check before iterating; returns ``status="suppressed_duplicate"``
    when this event_id has already fired within the day window.
    """
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    event_id = _compute_event_id(broadcast_type, body)
    chat_ids = _list_non_blocked_chat_ids(db.path)

    if dry_run:
        log.info(
            "DRY_RUN_BROADCAST: would_send=%d skipped=0 event_id=%s",
            len(chat_ids),
            event_id,
        )
        return {
            "status": "dry_run",
            "would_send": len(chat_ids),
            "would_skip_blocked": 0,
            "event_id": event_id,
        }

    # Real fire — idempotency gate.
    if _event_already_fired(db.path, event_id):
        log.info(
            "SUPPRESSED_DUPLICATE: broadcast_already_fired_today event_id=%s",
            event_id,
        )
        return {
            "status": "suppressed_duplicate",
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "event_id": event_id,
        }

    sent_count = 0
    skipped_count = 0
    failed_count = 0
    for chat_id in chat_ids:
        ok, reason = await _send_with_retry(
            bot, chat_id, body, db=db, reply_markup=reply_markup
        )
        if ok:
            sent_count += 1
        elif reason == "blocked":
            skipped_count += 1
        else:
            failed_count += 1

    _record_broadcast(
        db.path,
        event_id,
        broadcast_type,
        body_hash,
        sent_count,
        skipped_count,
        failed_count,
    )
    log_alert_event(
        "tg_broadcast_fired",
        broadcast_type=broadcast_type,
        event_id=event_id,
        sent=sent_count,
        skipped=skipped_count,
        failed=failed_count,
    )

    return {
        "status": "sent",
        "sent": sent_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "event_id": event_id,
    }


async def send_dm_async(
    bot: Bot,
    chat_id: int,
    body: str,
    db: Database | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Async core for sendDM — single-subscriber send with retry. Returns
    True on success, False if blocked OR 3 attempts all failed.
    """
    ok, _reason = await _send_with_retry(
        bot, chat_id, body, db=db, reply_markup=reply_markup
    )
    return ok


def _get_bot_token() -> str | None:
    """Read the bot token from env. Returns None when unset (CLI scripts
    should refuse to fire in this case).

    TG-WATCH-ADOPTION-BROADCAST-W1 (2026-06-19): ``PUBLIC_BOT_TOKEN`` is the
    canonical var the live bot actually runs on (``bot.py`` reads it from
    ``/etc/algovault-bot/env``). The legacy ``ALGOVAULT_BOT_TOKEN`` /
    ``TELEGRAM_BOT_TOKEN`` names were never present in that env file, so the
    daily-digest cron silently fired ``status: no_token`` on every run.
    Prefer ``PUBLIC_BOT_TOKEN``; keep the legacy names as fallbacks.
    """
    return (
        os.environ.get("PUBLIC_BOT_TOKEN")
        or os.environ.get("ALGOVAULT_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
    )


def sendBroadcast(
    body: str,
    broadcast_type: str,
    dry_run: bool = False,
    db_path: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> dict[str, Any]:
    """Sync wrapper around send_broadcast_async — entry for CLI / cron scripts.

    For dry_run=True we do NOT need a bot token (cohort count + event_id
    only); skip bot instantiation. For dry_run=False we instantiate a Bot
    from the env-loaded token, drive the async core via ``asyncio.run()``.

    Spec reference: ``Prompt/tg-broadcast-stack-w1.md`` C1 Verification Gate.
    """
    resolved_db_path = db_path or DEFAULT_DB_PATH
    db = Database(resolved_db_path)

    if dry_run:
        # No event loop / no Bot needed for dry-run; synchronous fast path.
        return asyncio.run(
            send_broadcast_async(
                bot=None,  # type: ignore[arg-type]  # unused in dry-run branch
                db=db,
                body=body,
                broadcast_type=broadcast_type,
                dry_run=True,
                reply_markup=reply_markup,
            )
        )

    token = _get_bot_token()
    if not token:
        log.error(
            "sendBroadcast refused: PUBLIC_BOT_TOKEN / ALGOVAULT_BOT_TOKEN / "
            "TELEGRAM_BOT_TOKEN unset"
        )
        return {
            "status": "no_token",
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "event_id": _compute_event_id(broadcast_type, body),
        }
    bot = Bot(token=token)
    return asyncio.run(
        send_broadcast_async(
            bot=bot,
            db=db,
            body=body,
            broadcast_type=broadcast_type,
            dry_run=False,
            reply_markup=reply_markup,
        )
    )


def sendDM(
    chat_id: int,
    body: str,
    db_path: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Sync wrapper around send_dm_async — entry for CLI / one-off scripts."""
    token = _get_bot_token()
    if not token:
        log.error(
            "sendDM refused: PUBLIC_BOT_TOKEN / ALGOVAULT_BOT_TOKEN / "
            "TELEGRAM_BOT_TOKEN unset"
        )
        return False
    bot = Bot(token=token)
    resolved_db_path = db_path or DEFAULT_DB_PATH
    db = Database(resolved_db_path)
    return asyncio.run(
        send_dm_async(
            bot=bot, chat_id=chat_id, body=body, db=db, reply_markup=reply_markup
        )
    )
