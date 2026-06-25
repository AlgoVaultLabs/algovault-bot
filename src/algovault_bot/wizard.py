"""TG-BUTTON-UX-W1 / C2 (+C3) — guided tap-to-subscribe wizards (ConversationHandler).

INPUT COLLECTOR only: the terminal step calls the SAME subscribe fn the typed
command calls (passed in — no forked persist / quota logic). Per-user state lives
in ``context.user_data`` (single-process polling bot). ``per_message=False`` — the
conversation mixes callback-button grids with a ForceReply "type ticker" message,
and ``per_message=True`` would warn "not every handler is tracked" on that mix.

Dependency-injected (no import of handlers.py → no module cycle): the builder takes
the typed handler + the existing commit fn + the live coin sources, so the wizard
reuses the exact same business logic the typed `/watch` path runs.
"""
from __future__ import annotations

import warnings
from typing import Any, Awaitable, Callable

from telegram import ForceReply, Message, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from . import keyboards
from .validators import ValidationError, normalize_coin

# Watch wizard states.
W_COIN, W_TF, W_EXCHANGE, W_MODE = range(4)
_UD = "wz_watch"  # context.user_data key for the in-flight pick
POPULAR_N = 9  # coins in the shortlist grid (3×3)


def build_watch_conversation(
    db: Any,
    *,
    typed_watch: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]],
    commit_watch: Callable[[int, str, str, str, str], str],
    get_popular_coins: Callable[[], list[str]],
    get_universe: Callable[[], list[str]],
) -> ConversationHandler:
    """Watch wizard: COIN → TF → EXCHANGE → MODE → (commit + card)."""

    def _pick(ctx: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
        if ctx.user_data is None:  # pragma: no cover - PTB always provides it under polling
            return {}
        return ctx.user_data.setdefault(_UD, {})

    async def _send_coin_grid(send: Callable[..., Awaitable[Any]]) -> int:
        coins = get_popular_coins() or list(keyboards.FALLBACK_POPULAR_COINS)
        await send(
            "📈 Watch a coin — tap one or type a ticker:",
            reply_markup=keyboards.coin_grid_kb(coins, "wz"),
        )
        return W_COIN

    async def _entry_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if update.message is None:
            return ConversationHandler.END
        if ctx.args:  # typed fast-path — reuse the existing handler verbatim, no wizard
            await typed_watch(update, ctx)
            return ConversationHandler.END
        _pick(ctx).clear()
        return await _send_coin_grid(update.message.reply_text)

    async def _entry_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None or q.message is None:
            return ConversationHandler.END
        await q.answer()
        _pick(ctx).clear()
        return await _send_coin_grid(q.edit_message_text)

    async def _pick_coin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None or q.data is None:
            return W_COIN
        await q.answer()
        _pick(ctx)["coin"] = q.data.split(":", 2)[2]
        await q.edit_message_text(
            f"📈 {_pick(ctx)['coin']} — pick a timeframe:", reply_markup=keyboards.tf_grid_kb("wz")
        )
        return W_TF

    async def _type_ticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None or q.message is None:
            return W_COIN
        await q.answer()
        await q.edit_message_text("🔤 Send the ticker symbol (e.g. ARB):")
        if isinstance(q.message, Message):
            await q.message.reply_text("Type a ticker:", reply_markup=ForceReply(selective=True))
        return W_COIN

    async def _got_ticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if update.message is None:
            return W_COIN
        raw = (update.message.text or "").strip()
        try:
            coin = normalize_coin(raw)
        except ValidationError as e:
            await update.message.reply_text(
                f"❌ {e}\nType a ticker:", reply_markup=ForceReply(selective=True)
            )
            return W_COIN
        if coin not in set(get_universe()):  # validated against the live 710+ universe
            await update.message.reply_text(
                f"❌ {coin} isn't a tracked perp. Type another ticker:",
                reply_markup=ForceReply(selective=True),
            )
            return W_COIN
        _pick(ctx)["coin"] = coin
        await update.message.reply_text(
            f"📈 {coin} — pick a timeframe:", reply_markup=keyboards.tf_grid_kb("wz")
        )
        return W_TF

    async def _pick_tf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None or q.data is None:
            return W_TF
        await q.answer()
        _pick(ctx)["tf"] = q.data.split(":", 2)[2]
        await q.edit_message_text(
            f"📈 {_pick(ctx).get('coin')} · {_pick(ctx)['tf']} — pick an exchange:",
            reply_markup=keyboards.exchange_grid_kb("wz"),
        )
        return W_EXCHANGE

    async def _pick_exchange(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None or q.data is None:
            return W_EXCHANGE
        await q.answer()
        _pick(ctx)["exchange"] = q.data.split(":", 2)[2]
        await q.edit_message_text(
            "📈 Alert type — what should I push?", reply_markup=keyboards.mode_kb("wz")
        )
        return W_MODE

    async def _pick_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None or q.data is None or q.from_user is None:
            return W_MODE
        await q.answer("Added ✓")
        pick = _pick(ctx)
        mode = q.data.split(":", 2)[2]
        # SAME subscribe fn as the typed path — no fork, normal quota.
        card = commit_watch(q.from_user.id, pick["coin"], pick["tf"], pick["exchange"], mode)
        await q.edit_message_text(card, reply_markup=keyboards.confirm_followup_kb())
        pick.clear()
        return ConversationHandler.END

    # ── Back (per-state) ──
    async def _back_to_coin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None:
            return W_TF
        await q.answer()
        return await _send_coin_grid(q.edit_message_text)

    async def _back_to_tf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None:
            return W_EXCHANGE
        await q.answer()
        await q.edit_message_text(
            f"📈 {_pick(ctx).get('coin')} — pick a timeframe:", reply_markup=keyboards.tf_grid_kb("wz")
        )
        return W_TF

    async def _back_to_exchange(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        if q is None:
            return W_MODE
        await q.answer()
        await q.edit_message_text(
            f"📈 {_pick(ctx).get('coin')} · {_pick(ctx).get('tf')} — pick an exchange:",
            reply_markup=keyboards.exchange_grid_kb("wz"),
        )
        return W_EXCHANGE

    async def _cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.callback_query
        _pick(ctx).clear()
        if q is not None:
            await q.answer()
            await q.edit_message_text("✖️ Cancelled. Send /watch to start again.")
        elif update.message is not None:
            await update.message.reply_text("✖️ Cancelled.")
        return ConversationHandler.END

    _cancel_h: BaseHandler[Update, Any, object] = CallbackQueryHandler(_cancel, pattern=r"^wz:cancel$")
    entry: list[BaseHandler[Update, Any, object]] = [
        CommandHandler("watch", _entry_command),
        CallbackQueryHandler(_entry_menu, pattern=r"^mnu:watch$"),
    ]
    states: dict[object, list[BaseHandler[Update, Any, object]]] = {
        W_COIN: [
            CallbackQueryHandler(_pick_coin, pattern=r"^wz:coin:"),
            CallbackQueryHandler(_type_ticker, pattern=r"^wz:type$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, _got_ticker),
            _cancel_h,
        ],
        W_TF: [
            CallbackQueryHandler(_pick_tf, pattern=r"^wz:tf:"),
            CallbackQueryHandler(_back_to_coin, pattern=r"^wz:back$"),
            _cancel_h,
        ],
        W_EXCHANGE: [
            CallbackQueryHandler(_pick_exchange, pattern=r"^wz:ex:"),
            CallbackQueryHandler(_back_to_tf, pattern=r"^wz:back$"),
            _cancel_h,
        ],
        W_MODE: [
            CallbackQueryHandler(_pick_mode, pattern=r"^wz:mode:"),
            CallbackQueryHandler(_back_to_exchange, pattern=r"^wz:back$"),
            _cancel_h,
        ],
    }
    fallbacks: list[BaseHandler[Update, Any, object]] = [_cancel_h, CommandHandler("cancel", _cancel)]
    # per_message=False is correct for this MIXED conversation (callback grids + a
    # ForceReply ticker): track per (chat, user) and edit the evolving message via the
    # callback. PTB warns "CallbackQueryHandler not tracked per message" for ANY
    # per_message setting on a mixed conversation — suppress that one known, benign warning.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=PTBUserWarning)
        return ConversationHandler(
            entry_points=entry, states=states, fallbacks=fallbacks,
            per_message=False, name="watch_wizard",
        )
