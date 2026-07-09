"""TG-BUTTON-UX-W1 / C2 — guided Watch wizard (ConversationHandler).

Drives the wizard's callbacks (extracted from the ConversationHandler) with mock
Update/Query/Context to assert: the tap-through subscribes the SAME db row as the
typed path (reuse, no fork), ticker validation, and the per_message config.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from algovault_bot import wizard
from algovault_bot.db import Database
from algovault_bot.handlers import _commit_watch_combos


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list = []

    async def reply_text(self, text, reply_markup=None, **kw):  # noqa: ANN001
        self.replies.append((text, reply_markup))


class _FakeQuery:
    def __init__(self, data: str, user_id: int = 1) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _FakeMessage()
        self.edits: list = []
        self.answers: list = []

    async def answer(self, text=None, **kw):  # noqa: ANN001
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kw):  # noqa: ANN001
        self.edits.append((text, reply_markup))


class _FakeUpdate:
    def __init__(self, query=None, message=None) -> None:  # noqa: ANN001
        self.callback_query = query
        self.message = message


class _Ctx:
    def __init__(self, args=None) -> None:  # noqa: ANN001
        self.args = args or []
        self.user_data: dict = {}


def _build(tmp_db: Database, typed_spy=None):  # noqa: ANN001
    async def _noop_typed(update, ctx):  # noqa: ANN001
        if typed_spy is not None:
            typed_spy.append((update, ctx))

    return wizard.build_watch_conversation(
        tmp_db,
        typed_watch=_noop_typed,
        commit_watch=lambda cid, c, tf, ex, m: _commit_watch_combos(
            tmp_db, cid, [(c, tf, ex)], 1, 1, 1, m, skip_preflight=True
        ),
        get_popular_coins=lambda: ["BTC", "ETH", "SOL"],
        get_universe=lambda: ["BTC", "ETH", "SOL", "ARB"],
    )


def _cb(conv, state, idx):  # noqa: ANN001
    return conv.states[state][idx].callback


def test_wizard_watch_structure_and_per_message(tmp_db):
    conv = _build(tmp_db)
    assert conv.per_message is False  # mixed callback + ForceReply message states
    # entered by /watch AND the mnu:watch menu button
    cb_patterns = [h.pattern.pattern for h in conv.entry_points if getattr(h, "pattern", None) is not None]
    assert any("mnu:watch" in p for p in cb_patterns)
    assert set(conv.states) == {wizard.W_COIN, wizard.W_TF, wizard.W_EXCHANGE, wizard.W_MODE}


def test_wizard_watch_full_flow_subscribes_same_row_as_typed(tmp_db):
    tmp_db.upsert_subscriber(1, "u", "en")
    conv = _build(tmp_db)
    ctx = _Ctx()
    q1 = _FakeQuery("wz:coin:BTC")
    assert asyncio.run(_cb(conv, wizard.W_COIN, 0)(_FakeUpdate(query=q1), ctx)) == wizard.W_TF
    q2 = _FakeQuery("wz:tf:15m")
    assert asyncio.run(_cb(conv, wizard.W_TF, 0)(_FakeUpdate(query=q2), ctx)) == wizard.W_EXCHANGE
    q3 = _FakeQuery("wz:ex:BYBIT")
    assert asyncio.run(_cb(conv, wizard.W_EXCHANGE, 0)(_FakeUpdate(query=q3), ctx)) == wizard.W_MODE
    q4 = _FakeQuery("wz:mode:both")
    assert asyncio.run(_cb(conv, wizard.W_MODE, 0)(_FakeUpdate(query=q4), ctx)) == ConversationHandler.END

    # SAME db row a typed `/watch BTC 15m BYBIT both` would create (reuse, no fork).
    rows = tmp_db.list_watches(1)
    assert any(
        r["coin"] == "BTC" and r["timeframe"] == "15m" and r["exchange"] == "BYBIT" and r["alert_type"] == "both"
        for r in rows
    )
    # terminal edits the single message into the persistent confirmation card + followup kb
    assert "You're now watching" in q4.edits[-1][0]
    assert q4.edits[-1][1] is not None  # confirm_followup_kb


def test_wizard_watch_ticker_validation(tmp_db):
    conv = _build(tmp_db)
    got_ticker = _cb(conv, wizard.W_COIN, 2)  # the MessageHandler
    ctx = _Ctx()
    # valid + in-universe → advances to TF
    assert asyncio.run(got_ticker(_FakeUpdate(message=_FakeMessage("ARB")), ctx)) == wizard.W_TF
    # garbage (bad format) → stays in COIN (re-prompt)
    assert asyncio.run(got_ticker(_FakeUpdate(message=_FakeMessage("!!!")), ctx)) == wizard.W_COIN
    # well-formed but NOT in the universe → stays in COIN (re-prompt)
    assert asyncio.run(got_ticker(_FakeUpdate(message=_FakeMessage("NOTACOIN")), ctx)) == wizard.W_COIN


def test_wizard_watch_command_entry_always_delegates(tmp_db):
    # TG-COPY-DEFAULTS-VENUES-W1: the bare TYPED /watch now delegates to the typed handler
    # (which runs the BTC 1h Binance default) — NOT the wizard. The mnu:watch button entry
    # still opens the wizard (see test_wizard_watch_menu_entry_opens_wizard).
    spy: list = []
    conv = _build(tmp_db, typed_spy=spy)
    entry = conv.entry_points[0].callback  # CommandHandler("watch")
    ended = asyncio.run(entry(_FakeUpdate(message=_FakeMessage()), _Ctx(args=["BTC", "15m"])))
    assert ended == ConversationHandler.END and len(spy) == 1
    # no args → ALSO delegates to the typed handler (default), NOT the wizard COIN state
    ended2 = asyncio.run(entry(_FakeUpdate(message=_FakeMessage()), _Ctx()))
    assert ended2 == ConversationHandler.END and len(spy) == 2


def test_mnu_watch_button_still_opens_wizard(tmp_db):
    # AC7: the /start button path (mnu:watch → _entry_menu) STILL launches the guided
    # wizard — the bare TYPED /watch repoint (→ default) left the callback entry untouched.
    conv = _build(tmp_db)
    menu_entry = next(h.callback for h in conv.entry_points if getattr(h, "pattern", None) is not None)
    state = asyncio.run(menu_entry(_FakeUpdate(query=_FakeQuery("mnu:watch")), _Ctx()))
    assert state == wizard.W_COIN
