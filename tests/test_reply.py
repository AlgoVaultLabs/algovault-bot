"""`reply.send_reply` — the one way a reply reaches the wire.

GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R6.

Two properties carry real risk and both are asserted here rather than assumed:

1. **`disable_web_page_preview` defaults to True.** All eight live call sites passed True before
   this helper existed. A signature that dropped it would have silently re-enabled Telegram link
   previews on every `/scan`, `/regime`, `/call`, `/funding` reply, the menu reply and the wizard
   one-shot — a user-visible change nobody ratified, invisible in review, and invisible to any
   test that only asserts on text.

2. **A MoneyNotice's own markup WINS over a caller's `extra_markup`.** The one site that passes
   both is the menu callback, whose extra markup is a navigation keyboard. When the answer to
   `/scan` is "you are out of alerts", the plan picker is what the user needs; a menu must never
   displace a money CTA.
"""
from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from algovault_bot.notices import MoneyNotice
from algovault_bot.reply import send_reply


def _kb(label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url="https://example.test")]])


class _Message:
    """A Message-shaped double: it can `reply_text` and cannot `edit_message_text`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kw) -> None:
        self.calls.append((text, kw))


class _Query:
    """A CallbackQuery-shaped double: it EDITS.

    🛑 THIS DOUBLE IS THE REASON `send_reply` DISPATCHES ON CAPABILITY. The first draft used
    `isinstance(target, CallbackQuery)`, which sent every double down the `reply_text` branch and
    raised — measured against `tests/test_wizard_scan.py`'s own stand-in. A helper that only
    works with real Telegram objects cannot be unit-tested at the seam it exists to own.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def edit_message_text(self, text: str, **kw) -> None:
        self.calls.append((text, kw))


def test_a_message_target_replies_and_a_query_target_edits() -> None:
    m, q = _Message(), _Query()
    asyncio.run(send_reply(m, "hello"))
    asyncio.run(send_reply(q, "hello"))
    assert m.calls and q.calls
    assert m.calls[0][0] == "hello" and q.calls[0][0] == "hello"


def test_link_previews_stay_suppressed_by_default() -> None:
    """The default is the behaviour every existing call site had. Pinned so it cannot drift."""
    for target in (_Message(), _Query()):
        asyncio.run(send_reply(target, "text with algovault.com in it"))
        assert target.calls[0][1]["disable_web_page_preview"] is True


def test_a_caller_can_still_turn_previews_on_deliberately() -> None:
    m = _Message()
    asyncio.run(send_reply(m, "x", disable_web_page_preview=False))
    assert m.calls[0][1]["disable_web_page_preview"] is False


def test_a_money_notice_carries_its_own_markup_through() -> None:
    m = _Message()
    picker = _kb("Starter · $9.99/mo")
    asyncio.run(send_reply(m, MoneyNotice(text="out of alerts", markup=picker, campaign="c")))
    assert m.calls[0][0] == "out of alerts"
    assert m.calls[0][1]["reply_markup"] is picker


def test_a_money_notice_markup_BEATS_a_callers_extra_markup() -> None:
    """The menu callback passes both. The picker wins — a menu must not displace a money CTA."""
    m = _Message()
    picker, menu = _kb("Starter"), _kb("Menu")
    asyncio.run(
        send_reply(m, MoneyNotice(text="x", markup=picker, campaign="c"), extra_markup=menu)
    )
    assert m.calls[0][1]["reply_markup"] is picker


def test_extra_markup_is_used_when_the_reply_is_a_plain_string() -> None:
    """The menu's ordinary path: no money notice, so its own keyboard rides."""
    m = _Message()
    menu = _kb("Menu")
    asyncio.run(send_reply(m, "just text", extra_markup=menu))
    assert m.calls[0][1]["reply_markup"] is menu


def test_a_money_notice_with_NO_markup_falls_back_to_the_callers() -> None:
    """A top-of-ladder wall carries `markup=None` by design. The caller's keyboard is then the
    only one there is, and dropping it too would leave the user with nothing at all."""
    m = _Message()
    menu = _kb("Menu")
    asyncio.run(
        send_reply(m, MoneyNotice(text="top of ladder", markup=None, campaign="plan_wall"),
                   extra_markup=menu)
    )
    assert m.calls[0][1]["reply_markup"] is menu
