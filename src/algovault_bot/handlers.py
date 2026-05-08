"""Pure command-handler implementations.

Each ``handle_*`` function is a sync pure function returning the reply string.
The ``register_handlers`` adapter wires these to ``python-telegram-bot``'s
async ``Update`` API. Splitting the logic from the framework gives us a clean
unit-test seam — the C2 verification gate runs the pure handlers directly with
a temp SQLite, no Telegram sockets in the loop.
"""

from __future__ import annotations

import logging
from typing import Sequence

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from . import messages
from .db import Database, PER_USER_WATCHLIST_CAP
from .validators import (
    DEFAULT_ALERT_TYPE,
    DEFAULT_EXCHANGE,
    ValidationError,
    normalize_alert_type,
    normalize_coin,
    normalize_exchange,
    normalize_timeframe,
)

log = logging.getLogger(__name__)


# ── pure handlers ──────────────────────────────────────────────


def handle_start(
    db: Database, chat_id: int, username: str | None, lang_code: str | None
) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    return messages.WELCOME_MESSAGE


def handle_help(db: Database, chat_id: int, username: str | None, lang_code: str | None) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    return messages.HELP_MESSAGE


def handle_watch(
    db: Database,
    chat_id: int,
    username: str | None,
    lang_code: str | None,
    args: Sequence[str],
) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)

    if len(args) < 2:
        return messages.usage_watch_message()
    if len(args) > 4:
        return "Too many arguments. " + messages.usage_watch_message()

    try:
        coin = normalize_coin(args[0])
        timeframe = normalize_timeframe(args[1])
        exchange = normalize_exchange(args[2] if len(args) >= 3 else None)
        alert_type = normalize_alert_type(args[3] if len(args) >= 4 else None)
    except ValidationError as e:
        return f"❌ {e}"

    # Cap check — only on insert, not on update of an existing entry.
    existing_count = db.count_watches(chat_id)
    # If this exact (chat_id, coin, tf, exchange) already exists, we update
    # alert_type instead of inserting; cap doesn't apply.
    rows = db.list_watches(chat_id)
    is_existing = any(
        r["coin"] == coin and r["timeframe"] == timeframe and r["exchange"] == exchange
        for r in rows
    )
    if not is_existing and existing_count >= PER_USER_WATCHLIST_CAP:
        return messages.cap_reached_message(PER_USER_WATCHLIST_CAP)

    db.add_watch(chat_id, coin, timeframe, exchange, alert_type)
    return messages.watch_added_message(coin, timeframe, exchange, alert_type)


def handle_unwatch(
    db: Database,
    chat_id: int,
    username: str | None,
    lang_code: str | None,
    args: Sequence[str],
) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)

    if len(args) < 2:
        return messages.usage_unwatch_message()
    if len(args) > 3:
        return "Too many arguments. " + messages.usage_unwatch_message()

    try:
        coin = normalize_coin(args[0])
        timeframe = normalize_timeframe(args[1])
        exchange = normalize_exchange(args[2] if len(args) >= 3 else None)
    except ValidationError as e:
        return f"❌ {e}"

    if db.remove_watch(chat_id, coin, timeframe, exchange):
        return messages.watch_removed_message(coin, timeframe, exchange)
    return messages.watch_not_found_message(coin, timeframe, exchange)


def handle_list(db: Database, chat_id: int, username: str | None, lang_code: str | None) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    rows = db.list_watches(chat_id)
    if not rows:
        return messages.list_empty_message()
    return messages.list_message(
        [
            {
                "coin": r["coin"],
                "timeframe": r["timeframe"],
                "exchange": r["exchange"],
                "alert_type": r["alert_type"],
            }
            for r in rows
        ],
        cap=PER_USER_WATCHLIST_CAP,
    )


# ── telegram framework adapters ─────────────────────────────────


def _user_meta(update: Update) -> tuple[int, str | None, str | None]:
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else 0
    username = user.username if user else None
    lang = user.language_code if user else None
    return chat_id, username, lang


def register_handlers(app: Application, db: Database) -> None:
    async def _start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        reply = handle_start(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        reply = handle_help(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        args = ctx.args or []
        reply = handle_watch(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        args = ctx.args or []
        reply = handle_unwatch(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _list(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        reply = handle_list(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("watch", _watch))
    app.add_handler(CommandHandler("unwatch", _unwatch))
    app.add_handler(CommandHandler("list", _list))


# Re-export the constants kept (for now) so existing tests/import paths still work.
__all__ = [
    "DEFAULT_ALERT_TYPE",
    "DEFAULT_EXCHANGE",
    "handle_help",
    "handle_list",
    "handle_start",
    "handle_unwatch",
    "handle_watch",
    "register_handlers",
]
