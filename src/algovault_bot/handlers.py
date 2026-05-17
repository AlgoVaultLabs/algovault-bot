"""Pure command-handler implementations.

Each ``handle_*`` function is a sync pure function returning the reply string.
The ``register_handlers`` adapter wires these to ``python-telegram-bot``'s
async ``Update`` API. Splitting the logic from the framework gives us a clean
unit-test seam — the C2 verification gate runs the pure handlers directly with
a temp SQLite, no Telegram sockets in the loop.
"""

from __future__ import annotations

import json
import logging
from typing import Sequence

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from . import messages
from .admin import handle_stats as admin_handle_stats
from .db import Database, PER_USER_WATCHLIST_CAP
from .link_validator import validate_api_key
from .mcp_client import McpError, from_env
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


# ── BOT-WATCH-VALIDATE-W1 — symbol preflight at /watch time ───
#
# Discovered 2026-05-17: a watch row whose symbol the upstream doesn't
# recognize (e.g. `XAUUSD` instead of `GOLD` / `XAU`) gets a 200 OK with
# null call/price on every cron tick — the alert engine treats null call
# as HOLD-equivalent and silently absorbs it. Result: the watch sits dead
# in the DB forever, never firing a single alert.
#
# Fix: at /watch insert time, fire one preflight `get_trade_call` against
# the proposed (coin, tf, exchange). If the upstream returns a clean null
# response, reject the watch before it lands. If the MCP itself errors,
# fail-open (allow the watch through; cron will sort it out — beats
# blocking legitimate watches during a transient outage).


def _validate_symbol_impl(coin: str, timeframe: str, exchange: str) -> str | None:
    """Real preflight implementation — fires a live MCP call. Tests call this
    directly to exercise the real logic; production calls ``_validate_symbol``
    which is just an alias (the alias seam exists so the test fixture can
    monkeypatch the alias to a no-op without breaking direct-impl tests)."""
    try:
        with from_env() as cli:
            result = cli.call_tool(
                "get_trade_call",
                {
                    "coin": coin,
                    "timeframe": timeframe,
                    "exchange": exchange,
                    "includeReasoning": False,
                },
            )
    except McpError as e:
        log.warning(
            json.dumps({
                "event": "watch_validate_mcp_failed",
                "coin": coin,
                "timeframe": timeframe,
                "exchange": exchange,
                "err": str(e)[:200],
            })
        )
        return None  # fail-open
    # Clean null response = upstream silently doesn't know this symbol.
    # A valid HOLD call returns call='HOLD' + a real price.
    if result.get("call") is None and result.get("price") is None:
        log.info(
            json.dumps({
                "event": "watch_rejected_unknown_symbol",
                "coin": coin,
                "timeframe": timeframe,
                "exchange": exchange,
            })
        )
        return messages.symbol_unknown_message(coin, exchange)
    return None


# Test seam — production wires through this alias. The conftest autouse
# fixture monkeypatches THIS alias to a no-op so handler tests don't hit
# a live MCP server. Tests that want to exercise the real logic call
# ``_validate_symbol_impl`` directly (which the autouse fixture leaves
# untouched).
def _validate_symbol(coin: str, timeframe: str, exchange: str) -> str | None:
    return _validate_symbol_impl(coin, timeframe, exchange)


# ── /start deep-link parameter (BOT-W2) ───────────────────────


_AUTH_PREFIX = "auth_"


# ── pure handlers ──────────────────────────────────────────────


def handle_start(
    db: Database, chat_id: int, username: str | None, lang_code: str | None
) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    # BOT-ZOMBIE-W1: if this subscriber was previously marked bot-blocked,
    # /start is hard proof they've unblocked. Clear the flag so digest/stats
    # count them as a reachable subscriber again.
    db.unmark_subscriber_blocked(chat_id)
    return messages.WELCOME_MESSAGE


def handle_link(
    db: Database,
    chat_id: int,
    username: str | None,
    lang_code: str | None,
    api_key: str,
) -> str:
    """Validate api_key against signal-MCP, then link this chat to it.

    BOT-W2 / D1-C: api_key arrives via the `/start auth_<api_key>` deep-link
    fired from the /welcome page. Bot validates the key via signal-MCP's
    /api/bot/validate-key endpoint (loopback, internal-bypass-gated) and
    stores the linked tier so the C3 quota gate can honor it.

    NEVER log the api_key value at INFO. Only structured fields.
    """
    db.upsert_subscriber(chat_id, username, lang_code)
    validated = validate_api_key(api_key)
    if validated is None:
        log.info(
            '{"event": "link_failed", "chat_id": %d, "reason": "validation_returned_none"}',
            chat_id,
        )
        return messages.link_invalid_key_message()

    previous_tier, is_new_link = db.link_subscriber(chat_id, api_key, validated.tier)
    log.info(
        '{"event": "link_ok", "chat_id": %d, "tier": "%s", "is_new_link": %s, '
        '"previous_tier": "%s"}',
        chat_id,
        validated.tier,
        "true" if is_new_link else "false",
        previous_tier or "null",
    )

    if is_new_link:
        return messages.link_first_time_message(validated.tier)
    if previous_tier != validated.tier:
        return messages.link_tier_changed_message(previous_tier, validated.tier)
    return messages.link_already_linked_message(validated.tier)


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

    # Symbol preflight (BOT-WATCH-VALIDATE-W1) — only on insert. Modifying
    # an existing entry's alert_type doesn't need re-validation (the row was
    # already validated when first added, OR predates the validation; in
    # both cases the user can /unwatch via /list without help).
    if not is_existing:
        symbol_err = _validate_symbol(coin, timeframe, exchange)
        if symbol_err:
            return symbol_err

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


def handle_stats(db: Database, chat_id: int, username: str | None, lang_code: str | None) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    return admin_handle_stats(db, chat_id)


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
    async def _start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        # BOT-W2: detect deep-link param `/start auth_<api_key>`.
        # NEVER log args[0] value at INFO — it may carry the api_key.
        args = ctx.args or []
        if args and isinstance(args[0], str) and args[0].startswith(_AUTH_PREFIX):
            api_key = args[0][len(_AUTH_PREFIX):]
            log.info(
                '{"event": "start_auth_param_received", "chat_id": %d, '
                '"has_param": true, "param_kind": "auth", "param_len": %d}',
                chat_id,
                len(api_key),
            )
            reply = handle_link(db, chat_id, username, lang, api_key)
        else:
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

    async def _stats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        reply = handle_stats(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("watch", _watch))
    app.add_handler(CommandHandler("unwatch", _unwatch))
    app.add_handler(CommandHandler("list", _list))
    app.add_handler(CommandHandler("stats", _stats))


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
