"""TG-BUTTON-UX-W1 / C4 — /start inline button menu (BotFather pattern)."""
from __future__ import annotations

from telegram.ext import CallbackQueryHandler

from algovault_bot import keyboards
from algovault_bot.handlers import register_handlers
from algovault_bot.messages import help_message, welcome_message
from algovault_bot.quota import (
    FREE_TIER_DAILY_QUOTA,
    FREE_TIER_MONTHLY_QUOTA,
    STARTER_MONTHLY_CALLS,
    STARTER_PRICE_USD,
)

# GROWTH-TG-QUOTA-PARITY-W1 CH3: WELCOME_MESSAGE / HELP_MESSAGE became FUNCTIONS — a constant
# cannot interpolate the ladder. Rendered here at the pinned defaults so every assertion below is
# unchanged: what is being tested is the COPY, not the numbers, and the numbers have their own
# tests in test_quota_daily_cap.py / test_ladder_client.py.
_LADDER = (
    FREE_TIER_MONTHLY_QUOTA, FREE_TIER_DAILY_QUOTA, STARTER_PRICE_USD, STARTER_MONTHLY_CALLS,
)
WELCOME_MESSAGE = welcome_message(*_LADDER)
HELP_MESSAGE = help_message(FREE_TIER_MONTHLY_QUOTA, FREE_TIER_DAILY_QUOTA)



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


def test_start_welcome_prose_present():
    # TG-COPY-DEFAULTS-VENUES-W1 (R1): plain-language welcome (the C4 button menu still appends).
    assert "Welcome to AlgoVault" in WELCOME_MESSAGE
    assert "New here? Just type /watch and I'll start you on BTC 1h (Binance)." in WELCOME_MESSAGE
