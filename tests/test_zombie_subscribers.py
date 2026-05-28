"""BOT-ZOMBIE-W1 — bot-blocked subscriber bookkeeping.

When bot.send_* raises Forbidden ("bot was blocked by the user"),
alert_engine._handle_forbidden writes ``subscribers.bot_blocked_at``.
Digest/admin/stats exclude blocked rows from "Total Subscribers" and
"New Subscribers last 24h" so the counts reflect reachable users only.
A "🚫 Blocked the bot: N" line surfaces when N > 0.

When a previously-blocked subscriber sends /start, handle_start clears
the flag.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from telegram.error import Forbidden

from algovault_bot import handlers
from algovault_bot.alert_engine import _push, _push_photo
from algovault_bot.db import Database


# ── DB helpers ────────────────────────────────────────────────


def test_mark_and_unmark_blocked(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u1", "en")
    assert tmp_db.count_active_subscribers() == 1
    assert tmp_db.count_blocked_subscribers() == 0

    tmp_db.mark_subscriber_blocked(1, "2026-05-17T12:00:00+00:00")
    assert tmp_db.count_active_subscribers() == 0
    assert tmp_db.count_blocked_subscribers() == 1

    tmp_db.unmark_subscriber_blocked(1)
    assert tmp_db.count_active_subscribers() == 1
    assert tmp_db.count_blocked_subscribers() == 0


def test_mark_is_idempotent(tmp_db: Database) -> None:
    """Re-marking just refreshes the timestamp; doesn't break anything."""
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.mark_subscriber_blocked(1, "2026-05-17T12:00:00+00:00")
    tmp_db.mark_subscriber_blocked(1, "2026-05-17T13:00:00+00:00")
    assert tmp_db.count_blocked_subscribers() == 1


def test_unmark_on_unblocked_is_noop(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.unmark_subscriber_blocked(1)  # already null
    assert tmp_db.count_active_subscribers() == 1


# ── _push / _push_photo Forbidden handling ────────────────────


def test_push_marks_blocked_on_forbidden(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(42, "alice", "en")
    bot = AsyncMock()
    bot.send_message.side_effect = Forbidden("Forbidden: bot was blocked by the user")
    ok = asyncio.run(_push(bot, 42, "hello", db=tmp_db))
    assert ok is False
    assert tmp_db.count_blocked_subscribers() == 1
    assert tmp_db.count_active_subscribers() == 0


def test_push_photo_marks_blocked_on_forbidden(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(42, "alice", "en")
    bot = AsyncMock()
    bot.send_photo.side_effect = Forbidden("Forbidden: bot was blocked by the user")
    ok = asyncio.run(_push_photo(bot, 42, b"\x89PNG...", caption="x", db=tmp_db))
    assert ok is False
    assert tmp_db.count_blocked_subscribers() == 1


def test_push_other_telegram_error_does_not_mark(tmp_db: Database) -> None:
    """Non-Forbidden TelegramError (network blip, etc.) should NOT mark
    the subscriber as blocked — that would prune live users on transient
    failures."""
    from telegram.error import TelegramError
    tmp_db.upsert_subscriber(42, "alice", "en")
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramError("timeout")
    ok = asyncio.run(_push(bot, 42, "hello", db=tmp_db))
    assert ok is False
    assert tmp_db.count_blocked_subscribers() == 0


def test_push_without_db_handles_forbidden_silently(tmp_db: Database) -> None:
    """If _push is called without db= (one-shot scripts), Forbidden is still
    caught; just no DB write."""
    bot = AsyncMock()
    bot.send_message.side_effect = Forbidden("Forbidden: bot was blocked by the user")
    ok = asyncio.run(_push(bot, 42, "hello"))
    assert ok is False  # didn't raise, just reported failure


# ── handle_start clears the block flag ────────────────────────


def test_handle_start_clears_blocked_flag(tmp_db: Database) -> None:
    """Pre-block the subscriber, then call /start — flag should clear."""
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.mark_subscriber_blocked(1, "2026-05-17T12:00:00+00:00")
    assert tmp_db.count_blocked_subscribers() == 1
    handlers.handle_start(tmp_db, 1, "u1", "en")
    assert tmp_db.count_blocked_subscribers() == 0
    assert tmp_db.count_active_subscribers() == 1


# ── digest renders correctly with blocked users ──────────────


def test_digest_excludes_blocked_from_total(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.upsert_subscriber(2, "u2", "en")
    tmp_db.mark_subscriber_blocked(2, "2026-05-17T12:00:00+00:00")
    text = render_digest(tmp_db)
    assert "Total Subscribers: 1" in text
    assert "🚫 Blocked the bot: 1" in text


def test_digest_omits_blocked_line_when_zero(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")
    text = render_digest(tmp_db)
    assert "Total Subscribers: 1" in text
    assert "Blocked the bot" not in text  # no zero-noise line


def test_digest_new_subs_24h_excludes_blocked(tmp_db: Database) -> None:
    """A subscriber created in last 24h but currently blocked must NOT
    be counted in New Subscribers last 24h."""
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")  # just created (within 24h)
    tmp_db.mark_subscriber_blocked(1, datetime.now(timezone.utc).isoformat())
    text = render_digest(tmp_db)
    assert "Total Subscribers: 0" in text
    assert "New Subscribers last 24h: 0" in text
    assert "🚫 Blocked the bot: 1" in text


# ── admin /stats renders correctly with blocked users ────────


def test_admin_stats_excludes_blocked_from_total(tmp_db: Database) -> None:
    from algovault_bot.admin import render_stats
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.upsert_subscriber(2, "u2", "en")
    tmp_db.mark_subscriber_blocked(2, "2026-05-17T12:00:00+00:00")
    text = render_stats(tmp_db)
    assert "Total Subscribers: 1" in text
    assert "🚫 Blocked the bot: 1" in text


def test_admin_stats_omits_blocked_line_when_zero(tmp_db: Database) -> None:
    from algovault_bot.admin import render_stats
    tmp_db.upsert_subscriber(1, "u1", "en")
    text = render_stats(tmp_db)
    assert "Total Subscribers: 1" in text
    assert "Blocked the bot" not in text
