"""TG-BUTTON-UX-W1 / C4 — /start inline button menu (BotFather pattern)."""
from __future__ import annotations

from telegram.ext import CallbackQueryHandler

from algovault_bot import keyboards
from algovault_bot.handlers import register_handlers
from algovault_bot.messages import WELCOME_MESSAGE


class _CapturingApp:
    def __init__(self) -> None:
        self.captured: list = []

    def add_handler(self, handler, *a, **k) -> None:  # noqa: ANN001
        self.captured.append(handler)


def test_menu_main_kb_has_all_actions_and_wizard_targets():
    kb = keyboards.main_menu_kb()
    btns = [b for row in kb.inline_keyboard for b in row]
    cbs = {b.callback_data for b in btns if b.callback_data}
    # Watch/Scan → wizards; the rest → existing handlers
    assert {"mnu:watch", "mnu:scan", "mnu:regime", "mnu:call", "mnu:funding", "mnu:list", "mnu:help"} <= cbs
    # Upgrade is a url button — https + utm preserved (signup_url SoT)
    assert any(b.url and b.url.startswith("https://") and "utm_campaign=start_welcome" in b.url for b in btns)
    assert all(len(row) <= 3 for row in kb.inline_keyboard)
    # every callback button is in the reserved mnu: namespace, ≤64B ASCII
    for b in btns:
        if b.callback_data:
            assert b.callback_data.startswith("mnu:") and b.callback_data.isascii()
            assert len(b.callback_data.encode()) <= 64


def test_menu_router_serves_existing_handlers_not_wizards(tmp_db):
    app = _CapturingApp()
    register_handlers(app, tmp_db)
    routers = [
        h for h in app.captured
        if isinstance(h, CallbackQueryHandler) and h.pattern is not None and h.pattern.match("mnu:list")
    ]
    assert routers, "mnu:* router not registered"
    pat = routers[0].pattern
    for ok in ("mnu:regime", "mnu:call", "mnu:funding", "mnu:list", "mnu:help"):
        assert pat.match(ok)
    # Watch/Scan are the wizards' OWN entry_points — the menu router must NOT claim them.
    for not_ok in ("mnu:watch", "mnu:scan"):
        assert not pat.match(not_ok)


def test_start_welcome_prose_preserved():
    # C4 only appends the keyboard — the approved welcome body is untouched.
    assert "Welcome to AlgoVault" in WELCOME_MESSAGE
    assert "utm_campaign=start_welcome" in WELCOME_MESSAGE  # upgrade utm intact
    assert "/referral" in WELCOME_MESSAGE  # prior-wave lines preserved
