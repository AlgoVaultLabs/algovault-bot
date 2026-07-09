"""TG-BUTTON-UX-W1 / C3 — guided Scan wizard (one-shot /scan + standing /scanwatch)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from algovault_bot import wizard
from algovault_bot.db import Database
from algovault_bot.handlers import handle_scanwatch


class _Msg:
    def __init__(self, text=None):  # noqa: ANN001
        self.text = text
        self.replies: list = []

    async def reply_text(self, text, reply_markup=None, **kw):  # noqa: ANN001
        self.replies.append((text, reply_markup))


class _Query:
    def __init__(self, data, uid=1):  # noqa: ANN001
        self.data = data
        self.from_user = SimpleNamespace(id=uid, username="u", language_code="en")
        self.message = _Msg()
        self.edits: list = []

    async def answer(self, text=None, **kw):  # noqa: ANN001
        pass

    async def edit_message_text(self, text, reply_markup=None, **kw):  # noqa: ANN001
        self.edits.append((text, reply_markup))


class _Upd:
    def __init__(self, query=None, message=None):  # noqa: ANN001
        self.callback_query = query
        self.message = message


class _Ctx:
    def __init__(self, args=None):  # noqa: ANN001
        self.args = args or []
        self.user_data: dict = {}


def _build(tmp_db: Database, *, scan_spy=None, sw_spy=None, run_result="🔍 Scan — 2 actionable: BTC BUY"):  # noqa: ANN001
    async def _ts(u, c):  # noqa: ANN001
        if scan_spy is not None:
            scan_spy.append(1)

    async def _tsw(u, c):  # noqa: ANN001
        if sw_spy is not None:
            sw_spy.append(1)

    return wizard.build_scan_conversation(
        tmp_db,
        typed_scan=_ts,
        typed_scanwatch=_tsw,
        run_scan=lambda cid, un, lg, n, tf, ex: run_result,
        commit_scanwatch=lambda cid, un, lg, n, tf, ex: handle_scanwatch(tmp_db, cid, un, lg, [str(n), tf, ex]),
    )


def _cb(conv, state, idx):  # noqa: ANN001
    return conv.states[state][idx].callback


def test_wizard_scan_structure(tmp_db):
    conv = _build(tmp_db)
    assert conv.per_message is False
    assert set(conv.states) == {wizard.S_KIND, wizard.S_TOPN, wizard.S_TF, wizard.S_EXCHANGE}
    # entered by /scan, /scanwatch (commands) + mnu:scan (callback)
    cmds = {h.commands for h in conv.entry_points if hasattr(h, "commands")}
    assert frozenset({"scan"}) in cmds and frozenset({"scanwatch"}) in cmds


def test_wizard_scan_standing_creates_row_and_card(tmp_db):
    tmp_db.upsert_subscriber(1, "u", "en")
    conv = _build(tmp_db)
    ctx = _Ctx()
    assert asyncio.run(_cb(conv, wizard.S_KIND, 0)(_Upd(query=_Query("scn:kind:standing")), ctx)) == wizard.S_TOPN
    assert asyncio.run(_cb(conv, wizard.S_TOPN, 0)(_Upd(query=_Query("scn:n:10")), ctx)) == wizard.S_TF
    assert asyncio.run(_cb(conv, wizard.S_TF, 0)(_Upd(query=_Query("scn:tf:15m")), ctx)) == wizard.S_EXCHANGE
    q = _Query("scn:ex:BINANCE")
    assert asyncio.run(_cb(conv, wizard.S_EXCHANGE, 0)(_Upd(query=q), ctx)) == ConversationHandler.END
    # same scan_watch row a typed /scanwatch would create
    rows = tmp_db.list_scan_watches(1)
    assert any(r["top_n"] == 10 and r["timeframe"] == "15m" and r["exchange"] == "BINANCE" for r in rows)
    assert "Standing scan" in q.edits[-1][0] and q.edits[-1][1] is not None  # card + followup kb


def test_wizard_scan_oneshot_shows_result_no_card(tmp_db):
    tmp_db.upsert_subscriber(1, "u", "en")
    conv = _build(tmp_db, run_result="🔍 Scan — 2 actionable: BTC BUY")
    ctx = _Ctx()
    asyncio.run(_cb(conv, wizard.S_KIND, 0)(_Upd(query=_Query("scn:kind:oneshot")), ctx))
    asyncio.run(_cb(conv, wizard.S_TOPN, 0)(_Upd(query=_Query("scn:n:5")), ctx))
    asyncio.run(_cb(conv, wizard.S_TF, 0)(_Upd(query=_Query("scn:tf:15m")), ctx))
    q = _Query("scn:ex:BINANCE")
    assert asyncio.run(_cb(conv, wizard.S_EXCHANGE, 0)(_Upd(query=q), ctx)) == ConversationHandler.END
    final = q.edits[-1][0]
    assert "2 actionable" in final  # the one-shot result IS the message
    assert "Standing scan" not in final  # NOT a subscription card
    assert tmp_db.list_scan_watches(1) == []  # one-shot persists nothing


def test_wizard_scan_reuses_tf_and_exchange_grids(tmp_db):
    # the scan TF/exchange grids carry the scn: prefix (C2 builders, prefix-parameterised)
    from algovault_bot import keyboards
    tf_cbs = [b.callback_data for row in keyboards.tf_grid_kb("scn").inline_keyboard for b in row]
    ex_cbs = [b.callback_data for row in keyboards.exchange_grid_kb("scn").inline_keyboard for b in row]
    assert "scn:tf:15m" in tf_cbs and "scn:ex:BINANCE" in ex_cbs


def test_wizard_scan_typed_args_delegate(tmp_db):
    scan_spy: list = []
    sw_spy: list = []
    conv = _build(tmp_db, scan_spy=scan_spy, sw_spy=sw_spy)
    scan_entry = conv.entry_points[0].callback  # /scan
    sw_entry = conv.entry_points[1].callback  # /scanwatch
    assert asyncio.run(scan_entry(_Upd(message=_Msg()), _Ctx(args=["10", "15m"]))) == ConversationHandler.END
    assert len(scan_spy) == 1
    assert asyncio.run(sw_entry(_Upd(message=_Msg()), _Ctx(args=["10", "1h"]))) == ConversationHandler.END
    assert len(sw_spy) == 1


def test_mnu_scan_button_still_opens_wizard(tmp_db):
    # AC7: mnu:scan → _entry_menu STILL opens the scan wizard (kind picker); the bare
    # TYPED /scan + /scanwatch repoint left the callback entry untouched.
    conv = _build(tmp_db)
    menu_entry = next(h.callback for h in conv.entry_points if getattr(h, "pattern", None) is not None)
    state = asyncio.run(menu_entry(_Upd(query=_Query("mnu:scan")), _Ctx()))
    assert state == wizard.S_KIND
