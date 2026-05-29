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
import os
import secrets
from typing import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from datetime import datetime, timezone

from . import asset_universe, batch, messages
from .admin import handle_stats as admin_handle_stats
from .coverage_nudge import compute_coverage_estimate, format_nudge, format_nudge_short
from .db import Database, PER_USER_WATCHLIST_CAP
from .link_validator import validate_api_key
from .log_setup import log_alert_event
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


# ── TG-BATCH-WATCHLIST-W1 — batch reply + helpers ──────────────


class BatchReply(str):
    """A handler reply that behaves exactly like its message string (so the
    existing pure-handler tests' ``in`` / ``==`` / ``startswith`` assertions
    keep working) while optionally carrying confirmation-keyboard metadata for
    the async wrapper:

    - ``confirm``: True → the wrapper attaches an inline keyboard instead of
      committing immediately.
    - ``pending``: the batch spec to stash in ``ctx.user_data`` under a token.
    - ``combos``: the expansion size (for the "Add all N" button label).
    """

    confirm: bool
    pending: dict | None
    combos: int

    def __new__(
        cls,
        text: str,
        *,
        confirm: bool = False,
        pending: dict | None = None,
        combos: int = 0,
    ) -> "BatchReply":
        obj = super().__new__(cls, text)
        obj.confirm = confirm
        obj.pending = pending
        obj.combos = combos
        return obj


def _batch_threshold() -> int:
    """Confirmation-nudge threshold (env ``BATCH_CONFIRM_THRESHOLD``, default 50)."""
    try:
        return int(
            os.environ.get("BATCH_CONFIRM_THRESHOLD", batch.DEFAULT_BATCH_CONFIRM_THRESHOLD)
        )
    except (TypeError, ValueError):
        return batch.DEFAULT_BATCH_CONFIRM_THRESHOLD


def _commit_watch_combos(
    db: Database,
    chat_id: int,
    combos: list[tuple[str, str, str]],
    n_coins: int,
    n_tfs: int,
    n_exch: int,
    alert_type: str,
    skip_preflight: bool,
) -> str:
    """Insert a batch of combos (idempotent). Preflights each DISTINCT new coin
    ONCE (never per combo — avoids the 41K-preflight trap on `all all all`);
    universe-sourced coins (`all` / Top-N) skip preflight (known-valid). On a
    rejected coin, nothing is inserted and the error is returned."""
    if not skip_preflight:
        existing = {
            (r["coin"], r["timeframe"], r["exchange"]) for r in db.list_watches(chat_id)
        }
        new_combos = [c for c in combos if c not in existing]
        checked: set[str] = set()
        for coin, tf, exch in new_combos:
            if coin in checked:
                continue
            checked.add(coin)
            err = _validate_symbol(coin, tf, exch)
            if err:
                return err

    db.add_watch_batch(chat_id, combos, alert_type)

    if len(combos) == 1:
        # Backward-compatible single-add message + coverage nudge.
        coin, tf, exch = combos[0]
        base = messages.watch_added_message(coin, tf, exch, alert_type)
        try:
            est = compute_coverage_estimate(coin, tf, exch)
            return base + format_nudge(coin, tf, exch, est)
        except Exception as exc:  # noqa: BLE001 — bot must never crash on nudge failure
            log.warning("coverage nudge failed for %s %s %s: %s", coin, tf, exch, exc)
            return base
    return messages.batch_watch_added_message(len(combos), n_coins, n_tfs, n_exch, alert_type)


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
) -> BatchReply:
    """TG-BATCH-WATCHLIST-W1: bulk-capable /watch. Each of COINS / TFS /
    EXCHANGES may be a single token, a comma-list, or ``all``. Returns a
    ``BatchReply`` — if ``.confirm`` is set the async wrapper shows a
    confirmation keyboard (large expansion or `all` coins) instead of
    committing. The cap is GONE; server safety lives in the C2 fetch budget.
    """
    db.upsert_subscriber(chat_id, username, lang_code)

    if len(args) < 2:
        return BatchReply(messages.usage_watch_message())
    if len(args) > 4:
        return BatchReply("Too many arguments. " + messages.usage_watch_message())

    coins_raw = args[0]
    tfs_raw = args[1]
    exch_raw = args[2] if len(args) >= 3 else DEFAULT_EXCHANGE
    type_raw = args[3] if len(args) >= 4 else None

    try:
        alert_type = normalize_alert_type(type_raw)
    except ValidationError as e:
        return BatchReply(f"❌ {e}")

    coins_is_all = batch.is_all(coins_raw)
    universe = asset_universe.get_asset_universe() if coins_is_all else []
    if coins_is_all and not universe:
        return BatchReply(
            "⚠️ Couldn't load the asset list right now. Try specific coins, "
            "e.g. /watch BTC,ETH 1h."
        )

    try:
        coins = batch.parse_coins(coins_raw, universe)
        tfs = batch.parse_timeframes(tfs_raw)
        exchanges = batch.parse_exchanges(exch_raw)
    except ValidationError as e:
        return BatchReply(f"❌ {e}")

    combos = batch.cartesian(coins, tfs, exchanges)
    if not combos:
        return BatchReply(messages.usage_watch_message())

    n_combos, n_coins, n_tfs, n_exch = len(combos), len(coins), len(tfs), len(exchanges)

    if batch.should_confirm(n_combos, coins_raw, _batch_threshold()):
        pending = {
            "kind": "watch",
            "coins_raw": coins_raw,
            "tfs_raw": tfs_raw,
            "exch_raw": exch_raw,
            "alert_type": alert_type,
            "coins_is_all": coins_is_all,
            "n_combos": n_combos,
            "n_coins": n_coins,
            "n_tfs": n_tfs,
            "n_exch": n_exch,
        }
        return BatchReply(
            messages.batch_confirm_message(n_combos, n_coins, n_tfs, n_exch),
            confirm=True,
            pending=pending,
            combos=n_combos,
        )

    return BatchReply(
        _commit_watch_combos(
            db, chat_id, combos, n_coins, n_tfs, n_exch, alert_type,
            skip_preflight=coins_is_all,
        )
    )


def commit_watch_batch(db: Database, chat_id: int, pending: dict | None, choice: str) -> str:
    """Commit a confirmed batch (``choice`` ∈ {"add", "top", "cancel"}). Called
    by the inline-keyboard callback after the confirmation nudge. Re-expands
    from the stored raw spec; ``top`` clamps COINS to the Top-N most-active."""
    if not pending:
        return messages.batch_expired_message()
    if choice == "cancel":
        return messages.batch_cancelled_message()

    try:
        tfs = batch.parse_timeframes(pending["tfs_raw"])
        exchanges = batch.parse_exchanges(pending["exch_raw"])
        if choice == "top":
            coins = asset_universe.get_top_assets(batch.DEFAULT_TOP_N)
            skip_preflight = True  # Top-N coins are universe-sourced → known valid
        else:  # "add"
            coins_is_all = bool(pending.get("coins_is_all"))
            universe = asset_universe.get_asset_universe() if coins_is_all else []
            coins = batch.parse_coins(pending["coins_raw"], universe)
            skip_preflight = coins_is_all
    except ValidationError as e:
        return f"❌ {e}"

    if not coins:
        return messages.batch_expired_message()

    combos = batch.cartesian(coins, tfs, exchanges)
    return _commit_watch_combos(
        db, chat_id, combos, len(coins), len(tfs), len(exchanges),
        pending["alert_type"], skip_preflight=skip_preflight,
    )


def handle_unwatch(
    db: Database,
    chat_id: int,
    username: str | None,
    lang_code: str | None,
    args: Sequence[str],
) -> BatchReply:
    """TG-BATCH-WATCHLIST-W1: bulk-capable /unwatch. Each dimension is either a
    specific token (filter) or ``all`` / omitted (wildcard — no filter). So
    ``/unwatch BTC all`` removes every BTC row; ``/unwatch all 1m`` removes
    every 1m row; ``/unwatch BTC 4h`` removes BTC 4h on any exchange."""
    db.upsert_subscriber(chat_id, username, lang_code)

    if len(args) < 2:
        return BatchReply(messages.usage_unwatch_message())
    if len(args) > 3:
        return BatchReply("Too many arguments. " + messages.usage_unwatch_message())

    try:
        coin = None if batch.is_all(args[0]) else normalize_coin(args[0])
        timeframe = None if batch.is_all(args[1]) else normalize_timeframe(args[1])
        if len(args) >= 3 and not batch.is_all(args[2]):
            exchange: str | None = normalize_exchange(args[2])
        else:
            exchange = None  # omitted or `all` → wildcard
    except ValidationError as e:
        return BatchReply(f"❌ {e}")

    removed = db.remove_watch_batch(
        chat_id, coin=coin, timeframe=timeframe, exchange=exchange
    )
    return BatchReply(messages.batch_unwatch_message(removed))


def handle_unwatchall(
    db: Database, chat_id: int, username: str | None, lang_code: str | None
) -> BatchReply:
    """TG-BATCH-WATCHLIST-W1: clear the entire watchlist — confirm first."""
    db.upsert_subscriber(chat_id, username, lang_code)
    n = db.count_watches(chat_id)
    if n == 0:
        return BatchReply(messages.unwatchall_empty_message())
    return BatchReply(
        messages.unwatchall_confirm_message(n),
        confirm=True,
        pending={"kind": "unwatchall"},
        combos=n,
    )


def commit_unwatchall(db: Database, chat_id: int) -> str:
    """Delete every watchlist row for the user (after /unwatchall confirm)."""
    removed = db.remove_all_watches(chat_id)
    return messages.unwatchall_done_message(removed)


def handle_stats(db: Database, chat_id: int, username: str | None, lang_code: str | None) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    return admin_handle_stats(db, chat_id)


def handle_list(db: Database, chat_id: int, username: str | None, lang_code: str | None) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    rows = db.list_watches(chat_id)
    if not rows:
        return messages.list_empty_message()
    # TG-BATCH-WATCHLIST-W1: watchlists are now uncapped, so a per-row dump can
    # be thousands of lines. Above the page threshold (repurposed
    # PER_USER_WATCHLIST_CAP), show a bounded grouped summary (by TF + exchange)
    # and skip per-row coverage nudges (would be N MCP fetches).
    if len(rows) > PER_USER_WATCHLIST_CAP:
        return messages.list_summary_message(
            [
                {
                    "coin": r["coin"],
                    "timeframe": r["timeframe"],
                    "exchange": r["exchange"],
                    "alert_type": r["alert_type"],
                }
                for r in rows
            ]
        )
    # OPS-TRADE-CALL-CLUSTER-W1 CH4 — append compact per-row nudge so existing
    # subscribers see expected fire-rate for each watched combo. Same coverage
    # source as /watch (signal-performance MCP resource; cached 5min in-process).
    enriched_rows: list[dict[str, str]] = []
    for r in rows:
        nudge = ""
        try:
            est = compute_coverage_estimate(r["coin"], r["timeframe"], r["exchange"])
            nudge = format_nudge_short(est)
        except Exception as exc:  # noqa: BLE001 — bot must never crash on nudge failure
            log.warning("coverage nudge failed for /list row %s: %s", r, exc)
        enriched_rows.append({
            "coin": r["coin"],
            "timeframe": r["timeframe"],
            "exchange": r["exchange"],
            "alert_type": r["alert_type"],
            "nudge": nudge,
        })
    return messages.list_message(enriched_rows, cap=PER_USER_WATCHLIST_CAP)


# ── telegram framework adapters ─────────────────────────────────


def _maybe_fire_first_command_event(db: Database, chat_id: int) -> None:
    """ACTIVATION-FUNNEL-AUDIT-W1 (2026-05-28): fire `tg_bot_first_command`
    (funnel stage 11) the FIRST time a subscriber issues any non-/start
    command. Dedup via `subscribers.first_command_fired_at` column: once-set,
    never re-emitted for the same chat_id. Q-C Option α: event lands in
    `/var/log/algovault-bot/alerts.log` as JSON line; snapshot reader greps
    for `"event": "tg_bot_first_command"` within window.

    Fail-open: any DB or log error is swallowed so the command handler still
    reaches reply_text(). The funnel emit is OBSERVATIONAL — it must not
    block user-visible bot behavior.
    """
    try:
        if db.get_first_command_fired_at(chat_id) is not None:
            return  # already fired for this subscriber; dedup
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.set_first_command_fired_at(chat_id, now_iso)
        log_alert_event("tg_bot_first_command", chat_id=chat_id)
    except Exception as e:  # pragma: no cover — fail-open
        logging.warning("tg_bot_first_command emit failed for chat_id=%s: %s", chat_id, e)


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
        _maybe_fire_first_command_event(db, chat_id)
        reply = handle_help(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_watch(db, chat_id, username, lang, args)
        if reply.confirm and reply.pending is not None:
            # Stash the pending spec under a short token; the callback re-reads
            # it from ctx.user_data (per-user) and commits the chosen action.
            token = secrets.token_urlsafe(6)
            if ctx.user_data is not None:
                ctx.user_data[token] = reply.pending
            rows = [
                [InlineKeyboardButton(
                    messages.batch_btn_add_all(reply.combos), callback_data=f"bw:add:{token}"
                )]
            ]
            if reply.pending.get("coins_is_all"):
                # Top-N clamp only makes sense when COINS == all (it narrows coins).
                rows.append([InlineKeyboardButton(
                    messages.batch_btn_top_n(batch.DEFAULT_TOP_N), callback_data=f"bw:top:{token}"
                )])
            rows.append([InlineKeyboardButton(
                messages.BATCH_BTN_CANCEL, callback_data=f"bw:cancel:{token}"
            )])
            await update.message.reply_text(
                str(reply), reply_markup=InlineKeyboardMarkup(rows),
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(str(reply), disable_web_page_preview=True)

    async def _unwatchall(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        reply = handle_unwatchall(db, chat_id, username, lang)
        if reply.confirm:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(messages.UNWATCHALL_BTN_YES, callback_data="uwa:yes"),
                InlineKeyboardButton(messages.UNWATCHALL_BTN_CANCEL, callback_data="uwa:cancel"),
            ]])
            await update.message.reply_text(str(reply), reply_markup=kb)
        else:
            await update.message.reply_text(str(reply))

    async def _on_batch_watch_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles the [Add all N] / [Top N most-active] / [Cancel] nudge taps."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        parts = query.data.split(":")
        if len(parts) != 3:
            return
        _, choice, token = parts
        pending = ctx.user_data.pop(token, None) if ctx.user_data is not None else None
        # from_user is always present on a callback query (the tapper).
        chat_id = query.from_user.id if query.from_user else 0
        text = commit_watch_batch(db, chat_id, pending, choice)
        try:
            await query.edit_message_text(text, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001 — message may be uneditable; log + drop
            log.warning("batch-watch callback edit failed chat_id=%s err=%s", chat_id, e)

    async def _on_unwatchall_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles the /unwatchall [Yes, clear all] / [Cancel] taps."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        choice = query.data.split(":")[-1]
        chat_id = query.from_user.id if query.from_user else 0
        text = commit_unwatchall(db, chat_id) if choice == "yes" else messages.batch_cancelled_message()
        try:
            await query.edit_message_text(text)
        except Exception as e:  # noqa: BLE001
            log.warning("unwatchall callback edit failed chat_id=%s err=%s", chat_id, e)

    async def _unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_unwatch(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _list(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        reply = handle_list(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _stats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        reply = handle_stats(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    # TG-BROADCAST-STACK-W1 C4 (2026-05-28): /unlock_premium_alerts entry.
    # Two-button inline keyboard offering X-follow OR npm-install paths;
    # callback handlers below dispatch on `unlock:x` / `unlock:npm` payloads.
    async def _unlock_premium_alerts(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        from .unlock import (
            CB_UNLOCK_NPM,
            CB_UNLOCK_X,
            format_already_verified_body,
            format_button_labels,
            format_intro_body,
        )

        # If active Pro grant exists, short-circuit with already_verified reply.
        grant = db.get_pro_grant(chat_id)
        if grant is not None:
            try:
                expires_at = datetime.fromisoformat(
                    str(grant["expires_at"]).replace("Z", "+00:00")
                )
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at > datetime.now(timezone.utc):
                    body = format_already_verified_body(expires_at, lang)
                    await update.message.reply_text(body, disable_web_page_preview=True)
                    return
            except Exception:
                pass

        body = format_intro_body(lang)
        x_label, npm_label = format_button_labels(lang)
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(x_label, callback_data=CB_UNLOCK_X)],
                [InlineKeyboardButton(npm_label, callback_data=CB_UNLOCK_NPM)],
            ]
        )
        await update.message.reply_text(
            body,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        log_alert_event("tg_unlock_attempted", chat_id=chat_id, lang_code=lang)

    async def _on_photo(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """TG-BROADCAST-STACK-W1 CH5: photo handler for X-follow screenshots.

        Triggers only when subscriber is in ``pending_x_screenshot`` state.
        Downloads the highest-resolution photo, saves to disk, updates DB,
        and fires operator-review DM (when TG_ADMIN_CHAT_ID env set).
        """
        if update.message is None or not update.message.photo:
            return
        chat_id, username, lang = _user_meta(update)
        status, method, _, _ = db.get_unlock_state(chat_id)
        from .screenshots import (
            DEFAULT_SCREENSHOTS_DIR,
            compute_screenshot_path,
            format_operator_review_caption,
            is_pending_x_screenshot,
        )

        if not is_pending_x_screenshot(status):
            # Photo arrived from a subscriber NOT in the X-screenshot flow;
            # ignore silently (could be unsolicited; do NOT reply or store).
            return

        # Pick the highest-resolution PhotoSize (last in the list per
        # python-telegram-bot semantics).
        photo = update.message.photo[-1]
        try:
            tg_file = await photo.get_file()
        except Exception as e:  # noqa: BLE001
            log.warning("get_file failed chat_id=%s err=%s", chat_id, e)
            return

        DEFAULT_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = compute_screenshot_path(chat_id)
        try:
            await tg_file.download_to_drive(custom_path=str(path))
        except Exception as e:  # noqa: BLE001
            log.warning("download_to_drive failed chat_id=%s err=%s", chat_id, e)
            return

        db.set_unlock_screenshot_path(chat_id, str(path))
        log_alert_event(
            "tg_unlock_screenshot_uploaded",
            chat_id=chat_id,
            lang_code=lang,
            screenshot_path=str(path),
        )

        # Operator-review DM with [Approve]/[Reject] buttons.
        admin_chat_id_raw = os.environ.get("TG_ADMIN_CHAT_ID", "").strip()
        if admin_chat_id_raw:
            try:
                admin_chat_id = int(admin_chat_id_raw)
                caption = format_operator_review_caption(chat_id, username, lang)
                from .unlock import CB_APPROVE_PREFIX, CB_REJECT_PREFIX
                review_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Approve",
                                callback_data=f"{CB_APPROVE_PREFIX}{chat_id}",
                            ),
                            InlineKeyboardButton(
                                "❌ Reject",
                                callback_data=f"{CB_REJECT_PREFIX}{chat_id}",
                            ),
                        ]
                    ]
                )
                with open(path, "rb") as photo_fp:
                    await update.get_bot().send_photo(
                        chat_id=admin_chat_id,
                        photo=photo_fp,
                        caption=caption,
                        reply_markup=review_keyboard,
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("operator-review DM failed: %s", e)
        else:
            log.info(
                "TG_ADMIN_CHAT_ID unset; screenshot saved at %s but no "
                "operator-review DM sent (manual review required)",
                path,
            )

        # Acknowledge to subscriber that screenshot was received.
        from .unlock import normalize_lang as _nl
        lang_norm = _nl(lang)
        if lang_norm == "id":
            ack = "Screenshot diterima! Saya akan verifikasi dalam 24 jam."
        elif lang_norm == "zh-hans":
            ack = "已收到截图！我将在 24 小时内验证。"
        else:
            ack = "Screenshot received! I'll verify within 24h."
        await update.message.reply_text(ack)

    async def _on_unlock_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles taps on the [Follow X] / [Install] inline buttons."""
        from .unlock import (
            CB_UNLOCK_NPM,
            CB_UNLOCK_X,
            METHOD_NPM_INSTALL,
            METHOD_X_FOLLOW,
            STATE_PENDING_NPM,
            STATE_PENDING_X,
            format_pending_npm_body,
            format_pending_x_body,
            generate_track_token,
        )

        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()  # acknowledge tap (no toast)
        chat_id = query.from_user.id if query.from_user else (
            query.message.chat_id if query.message else 0
        )
        sub = db.get_subscriber(chat_id)
        lang = sub["lang_code"] if sub else None

        if query.data == CB_UNLOCK_X:
            db.set_unlock_pending(chat_id, STATE_PENDING_X, METHOD_X_FOLLOW)
            body = format_pending_x_body(lang)
            if query.message:
                await query.message.reply_text(body, disable_web_page_preview=True)
            log_alert_event("tg_unlock_x_chosen", chat_id=chat_id, lang_code=lang)
        elif query.data == CB_UNLOCK_NPM:
            track_token = generate_track_token()
            db.set_unlock_pending(
                chat_id, STATE_PENDING_NPM, METHOD_NPM_INSTALL, track_token=track_token
            )
            body = format_pending_npm_body(track_token, lang)
            if query.message:
                # parse_mode=None so the ```snippet``` code-fence renders raw.
                await query.message.reply_text(body, disable_web_page_preview=True)
            log_alert_event(
                "tg_unlock_npm_chosen",
                chat_id=chat_id,
                lang_code=lang,
                track_token_prefix=track_token[:8],  # PII-safe shape
            )

    async def _on_review_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Operator-side [Approve]/[Reject] tap handler. callback_data shape:
        ``unlock_approve:<chat_id>`` or ``unlock_reject:<chat_id>``.
        Only fires for the configured TG_ADMIN_CHAT_ID — silently ignores
        taps from non-admin chats.
        """
        from .unlock import (
            CB_APPROVE_PREFIX,
            CB_REJECT_PREFIX,
            METHOD_X_FOLLOW,
            compute_grant_expiry,
            format_rejected_body,
            format_verified_body,
        )

        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        # Auth gate: only the configured admin chat can approve/reject.
        admin_chat_id_raw = os.environ.get("TG_ADMIN_CHAT_ID", "").strip()
        if not admin_chat_id_raw or not query.from_user:
            return
        try:
            admin_chat_id = int(admin_chat_id_raw)
        except ValueError:
            return
        if query.from_user.id != admin_chat_id:
            return

        # Parse target subscriber chat_id from callback_data.
        data = query.data
        if data.startswith(CB_APPROVE_PREFIX):
            decision = "approve"
            target_str = data[len(CB_APPROVE_PREFIX):]
        elif data.startswith(CB_REJECT_PREFIX):
            decision = "reject"
            target_str = data[len(CB_REJECT_PREFIX):]
        else:
            return
        try:
            target_chat_id = int(target_str)
        except ValueError:
            return

        sub = db.get_subscriber(target_chat_id)
        target_lang = sub["lang_code"] if sub else None

        if decision == "approve":
            now = datetime.now(timezone.utc)
            expires = compute_grant_expiry(now)
            db.set_unlock_verified(target_chat_id, now.isoformat(timespec="seconds"))
            db.insert_or_replace_pro_grant(
                target_chat_id,
                expires.isoformat(timespec="seconds"),
                METHOD_X_FOLLOW,
            )
            body = format_verified_body(METHOD_X_FOLLOW, target_lang)
            try:
                await update.get_bot().send_message(
                    chat_id=target_chat_id, text=body, disable_web_page_preview=True
                )
            except Exception as e:  # noqa: BLE001
                log.warning("approve DM failed target=%s err=%s", target_chat_id, e)
            log_alert_event(
                "tg_unlock_verified",
                chat_id=target_chat_id,
                method=METHOD_X_FOLLOW,
                granted_expires_at=expires.isoformat(timespec="seconds"),
            )
            if query.message:
                try:
                    await query.message.reply_text(
                        f"✅ Approved chat_id={target_chat_id} · Pro until {expires.strftime('%Y-%m-%d')}"
                    )
                except Exception:
                    pass
        else:  # reject
            db.reset_unlock_state(target_chat_id)
            body = format_rejected_body(target_lang)
            try:
                await update.get_bot().send_message(
                    chat_id=target_chat_id, text=body, disable_web_page_preview=True
                )
            except Exception as e:  # noqa: BLE001
                log.warning("reject DM failed target=%s err=%s", target_chat_id, e)
            log_alert_event(
                "tg_unlock_failed",
                chat_id=target_chat_id,
                reason="operator_rejected_screenshot",
            )
            if query.message:
                try:
                    await query.message.reply_text(
                        f"❌ Rejected chat_id={target_chat_id} · subscriber asked to retry"
                    )
                except Exception:
                    pass

    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("watch", _watch))
    app.add_handler(CommandHandler("unwatch", _unwatch))
    app.add_handler(CommandHandler("unwatchall", _unwatchall))
    app.add_handler(CommandHandler("list", _list))
    app.add_handler(CommandHandler("stats", _stats))
    # TG-BATCH-WATCHLIST-W1 — batch /watch confirm-nudge + /unwatchall confirm.
    app.add_handler(
        CallbackQueryHandler(_on_batch_watch_callback, pattern=r"^bw:(add|top|cancel):")
    )
    app.add_handler(
        CallbackQueryHandler(_on_unwatchall_callback, pattern=r"^uwa:(yes|cancel)$")
    )
    # TG-BROADCAST-STACK-W1 C4 + C5: /unlock_premium_alerts +
    # 2 CallbackQueryHandlers (unlock-path picker + operator approve/reject) +
    # photo MessageHandler for X-follow screenshot upload.
    app.add_handler(CommandHandler("unlock_premium_alerts", _unlock_premium_alerts))
    app.add_handler(
        CallbackQueryHandler(_on_unlock_callback, pattern=r"^unlock:(x|npm)$")
    )
    app.add_handler(
        CallbackQueryHandler(_on_review_callback, pattern=r"^unlock_(approve|reject):\d+$")
    )
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))


# Re-export the constants kept (for now) so existing tests/import paths still work.
__all__ = [
    "DEFAULT_ALERT_TYPE",
    "DEFAULT_EXCHANGE",
    "BatchReply",
    "commit_unwatchall",
    "commit_watch_batch",
    "handle_help",
    "handle_list",
    "handle_start",
    "handle_unwatch",
    "handle_unwatchall",
    "handle_watch",
    "register_handlers",
]
