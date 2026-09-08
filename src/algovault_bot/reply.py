"""ONE way to put a reply on the wire, whether it is a string or a composed money notice.

GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 R3.

WHY A NEW LEAF AND NOT `handlers.py`
────────────────────────────────────
Both callers need this: `handlers.py` and `wizard.py`. `handlers.py` imports `wizard`, and
`wizard.py`'s own module docstring records the constraint that keeps that edge one-way —
"dependency-injected (no import of handlers.py -> no module cycle)". Putting the helper in
`handlers.py` would make `wizard` import it back and close exactly the cycle `wizard` was built
to avoid; putting it in `notices.py` would give the composer module a send path, which its own
taxonomy row forbids. So: a leaf that imports neither. (Architect ruling Q4, 2026-09-07.)

WHAT IT PRESERVES, AND WHY THAT IS THE WHOLE POINT
──────────────────────────────────────────────────
Every one of the eight live sites this replaces passes ``disable_web_page_preview=True`` today.
The first draft of this signature omitted it, which would have silently re-enabled Telegram link
previews on every ``/scan``, ``/regime``, ``/call``, ``/funding`` reply, the menu reply and the
wizard one-shot — a user-visible change nobody ratified, invisible in review, and invisible to a
test that only asserts on text. It therefore DEFAULTS TO TRUE: the default preserves behaviour
and no call site has to remember.

A ``CallbackQuery`` target EDITS; a ``Message`` target REPLIES. That is not a style choice — the
wizard's one-shot edits its own "Scanning…" placeholder in place, while the menu deliberately
posts a NEW message below the menu. Callers pick by passing ``q`` or ``q.message``.
"""
from __future__ import annotations

from telegram import CallbackQuery, InlineKeyboardMarkup, Message

from .notices import MoneyNotice

__all__ = ["send_reply"]


async def send_reply(
    target: Message | CallbackQuery,
    reply: str | MoneyNotice,
    *,
    extra_markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool = True,
) -> None:
    """Deliver ``reply`` to ``target``, carrying a money notice's keyboard when there is one.

    🛑 A MoneyNotice's OWN markup WINS over ``extra_markup``. The only site that passes both is
    the menu callback, whose ``extra_markup`` is a navigation keyboard: when the answer to
    "/scan" is "you are out of alerts", the plan picker is the thing the user needs, and a menu
    must never displace a money CTA. Stated as a rule here rather than left to argument order.
    """
    if isinstance(reply, MoneyNotice):
        text, markup = reply.text, (reply.markup or extra_markup)
    else:
        text, markup = reply, extra_markup

    # 🛑 DISPATCH ON CAPABILITY, NOT ON CONCRETE TYPE. `edit_message_text` is a `CallbackQuery`
    # method and `reply_text` is a `Message` method, so the attribute IS the discriminator —
    # and an `isinstance` against the PTB classes makes this helper unusable from the suite's
    # own lightweight doubles. Measured: `tests/test_wizard_scan.py` drives the wizard with a
    # `_Query` stand-in, and the isinstance form sent its one-shot down the `reply_text` branch
    # and raised. A helper that only works with real Telegram objects cannot be unit-tested at
    # the seam it exists to own.
    if isinstance(target, CallbackQuery) or hasattr(target, "edit_message_text"):
        await target.edit_message_text(
            text, disable_web_page_preview=disable_web_page_preview, reply_markup=markup
        )
        return
    assert isinstance(target, Message) or hasattr(target, "reply_text")
    await target.reply_text(
        text, disable_web_page_preview=disable_web_page_preview, reply_markup=markup
    )
