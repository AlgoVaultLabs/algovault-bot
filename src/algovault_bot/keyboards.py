"""TG-BUTTON-UX-W1 — pure inline-keyboard builders.

NO DB / network / business logic — just `InlineKeyboardMarkup` construction, so
these are trivially unit-testable. Single source for every button grid: the main
`/start` menu (C4), the Watch wizard (C2) and Scan wizard (C3) step grids, and the
post-subscribe follow-up.

Callback data is short ASCII (≤64 bytes) in three reserved namespaces that do NOT
collide with the existing `bw:`/`uwa:`/`wb:`/`sw:`/`unlock:` handlers:
  - ``mnu:*``  main menu        — ``mnu:watch`` / ``mnu:scan`` / ``mnu:regime`` …
  - ``wz:*``   watch wizard     — ``wz:coin:BTC`` / ``wz:tf:15m`` / ``wz:ex:BYBIT`` /
                                  ``wz:mode:both`` / ``wz:type`` / ``wz:back`` / ``wz:cancel``
  - ``scn:*``  scan wizard      — same shape (C3 adds kind/topN grids)

The step grids take a ``prefix`` so the Watch (``wz``) and Scan (``scn``) wizards
reuse the SAME TF/exchange builders (single-derivation — no duplicate arrays).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .messages import SignupPlan, _usd, plan_signup_url
from .validators import (
    EXCHANGE_DISPLAY_ORDER,
    EXCHANGES,
    PUSH_TIMEFRAMES,
    TF_SECONDS,
    TIMEFRAMES,
)

if TYPE_CHECKING:  # pragma: no cover — types only, never a runtime import
    # `quota` imports THIS module (for `plan_picker_kb`), so importing `Ladder` at runtime would
    # close the cycle. `from __future__ import annotations` makes every annotation a string, so
    # this form is complete: mypy sees the real type and Python never performs the import. Same
    # reason `paywall.py` defers its own `quota` import.
    from .quota import Ladder

# Wizard TF grid — the full ON-DEMAND set (1m–1d), ordered shortest→longest. In parity
# with what the engine will ANSWER for, which is what the scan wizard's /scan half needs.
# NOT in parity with typed `/watch` any more: since SIGNAL-CLOSEDBAR-FLIP-W1 CH3 the push
# surfaces carry a floor (1m cannot be scheduled) — see PUSH_WIZARD_TIMEFRAMES below.
WIZARD_TIMEFRAMES: tuple[str, ...] = tuple(sorted(TIMEFRAMES, key=lambda t: TF_SECONDS[t]))

# The push-eligible subset, same order — for wizards that only ever SCHEDULE alerts.
# Derived from PUSH_TIMEFRAMES so the grid cannot drift from what the validators accept
# (SIGNAL-CLOSEDBAR-FLIP-W1 CH3).
PUSH_WIZARD_TIMEFRAMES: tuple[str, ...] = tuple(
    t for t in WIZARD_TIMEFRAMES if t in PUSH_TIMEFRAMES
)

# Stable display order — the single validators source (12 venues, HL-first, /help order).
_EXCHANGE_ORDER: tuple[str, ...] = EXCHANGE_DISPLAY_ORDER
EXCHANGE_DISPLAY: tuple[str, ...] = tuple(e for e in _EXCHANGE_ORDER if e in EXCHANGES)

# Watch-wizard quick-picks — a curated crypto + TradFi showcase (the bot watches BOTH:
# the live universe carries 900+ perps incl. XAU/gold, QQQ, and US-equity perps). A
# quick-pick tap commits DIRECTLY (skip_preflight), so EVERY entry MUST be a live-universe
# symbol — these were probed 2026-06-25. Any other symbol is reachable via 🔤 Type ticker.
WATCH_QUICKPICKS: tuple[str, ...] = ("BTC", "ETH", "SOL", "SPCX", "QQQ", "XAU")
# Fallback ONLY if the injected popular-coins source returns nothing — never primary.
FALLBACK_POPULAR_COINS: tuple[str, ...] = WATCH_QUICKPICKS

_MODE_LABELS: dict[str, str] = {
    "calls": "📈 Trade calls",
    "regime": "📊 Regime",
    "both": "📊📈 Both",
}


def _rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def _nav_row(prefix: str, *, back: bool = True) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if back:
        row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"{prefix}:back"))
    row.append(InlineKeyboardButton("✖️ Cancel", callback_data=f"{prefix}:cancel"))
    return row


def plan_picker_kb(
    ladder: Ladder,
    campaign: str,
    source: str | None = None,
    *,
    above_tier: str | None = None,
) -> InlineKeyboardMarkup | None:
    """THE one builder every money CTA in this bot calls. GROWTH-TG-PLAN-PICKER-W1 R3.

    Replaces `upgrade_button` / `upgrade_markup`, which offered a SINGLE SKU: a walled user saw
    one price and never learned the ladder existed. Four SKUs, two rows::

        [ Starter · $9.99/mo ]   [ Starter · $39.90/6mo ]
        [ Pro · $49/mo ]         [ Pro · $129/6mo ]

    🛑 RAIL-AGNOSTIC BY CONSTRUCTION, and that is the point rather than a nicety. One ROW per
    plan, one COLUMN per term — so the next rail, Telegram Stars, is one more column, not a fifth
    copy of the ladder. The old builders were DELETED rather than left beside this one for the
    same reason: two builders is how two surfaces end up disagreeing about what we sell.

    🛑 NO FIGURE IS TYPED HERE. Every label renders through `_usd` from the `ladder` argument,
    which `quota.resolve_ladder` derives from the server's published ladder with pinned per-field
    fallbacks that SERVE. Move a price in `plans.ts` and every button follows within one drain
    cycle, with no bot deploy.

    `above_tier` is the caller's EFFECTIVE tier (`quota.QuotaState.effective_tier.tier`) or None:

        None                 -> both rows (a free or unlinked chat)
        'starter'            -> the Pro row only — never sell someone the plan they have
        'pro' | 'enterprise' -> None. There is no self-serve rung above Pro, and a button that
                                leads nowhere is worse than no button; the CALLER sends text
                                only, and `messages.plan_picker_text` returns the ratified
                                top-of-ladder sentence for exactly these two values.

    PURE — no DB, no network, no clock (this module's contract; see the header). The caller
    resolves the ladder and passes it in.
    """

    def row(
        plan: SignupPlan, label: str, monthly: float, six_month: float
    ) -> list[InlineKeyboardButton]:
        return [
            InlineKeyboardButton(
                f"{label} · {_usd(monthly)}/mo",
                url="https://" + plan_signup_url(plan, "month", campaign, source),
            ),
            InlineKeyboardButton(
                f"{label} · {_usd(six_month)}/6mo",
                url="https://" + plan_signup_url(plan, "6month", campaign, source),
            ),
        ]

    if above_tier in ("pro", "enterprise"):
        return None
    pro = row("pro", "Pro", ladder.pro_price_usd, ladder.pro_price_usd_6month)
    if above_tier == "starter":
        return InlineKeyboardMarkup([pro])
    starter = row(
        "starter", "Starter", ladder.starter_price_usd, ladder.starter_price_usd_6month
    )
    return InlineKeyboardMarkup([starter, pro])


def main_menu_kb() -> InlineKeyboardMarkup:
    """The /start BotFather-style menu. Watch/Scan → wizards; others → existing handlers.

    GROWTH-TG-PLAN-PICKER-W1 R4: ⬆️ Upgrade is now a CALLBACK (`mnu:upgrade`), not a URL button.
    A URL button can carry one destination, so it could only ever advertise one SKU; the callback
    opens the four-SKU picker INSIDE Telegram, where the user compares prices before leaving.

    That also retires this builder's `source` parameter. It existed solely to thread `utm_medium`
    into the one URL built here; the picker's URLs are now built at the CALLBACK, which is where
    the chat_id — and therefore the acquisition source — is actually available. Threading it into
    a static menu was carrying a per-user value through a builder that has no user."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Watch", callback_data="mnu:watch"),
         InlineKeyboardButton("🔍 Scan", callback_data="mnu:scan")],
        [InlineKeyboardButton("📡 Regime", callback_data="mnu:regime"),
         InlineKeyboardButton("🎯 Call", callback_data="mnu:call"),
         InlineKeyboardButton("💰 Funding", callback_data="mnu:funding")],
        [InlineKeyboardButton("📋 My list", callback_data="mnu:list"),
         InlineKeyboardButton("❓ Help", callback_data="mnu:help")],
        [InlineKeyboardButton("⬆️ Upgrade", callback_data="mnu:upgrade")],
    ])


def coin_grid_kb(coins: list[str], prefix: str = "wz", *, back: bool = False) -> InlineKeyboardMarkup:
    """Quick-pick shortlist (caller passes the curated WATCH_QUICKPICKS) + 🔤 Type ticker."""
    btns = [InlineKeyboardButton(c, callback_data=f"{prefix}:coin:{c}") for c in coins]
    rows = _rows(btns, 3)
    rows.append([InlineKeyboardButton("🔤 Type ticker", callback_data=f"{prefix}:type")])
    rows.append(_nav_row(prefix, back=back))
    return InlineKeyboardMarkup(rows)


def tf_grid_kb(prefix: str = "wz", *, push_only: bool = False) -> InlineKeyboardMarkup:
    """Timeframe grid, in parity with the typed path it mirrors. Shared by both wizards.

    `push_only=True` drops the timeframes that cannot be SCHEDULED (validators.PUSH_TIMEFRAMES)
    — used by the watch wizard, which only ever creates push alerts. It defaults to False
    because the scan wizard is entered by BOTH `/scan` (a one-shot answer, where every
    timeframe is valid) and `/scanwatch`; offering the full grid there and letting the
    scanwatch commit path explain the refusal is preferable to hiding a timeframe that the
    /scan half of the same wizard genuinely supports.
    """
    tfs = PUSH_WIZARD_TIMEFRAMES if push_only else WIZARD_TIMEFRAMES
    btns = [InlineKeyboardButton(tf, callback_data=f"{prefix}:tf:{tf}") for tf in tfs]
    rows = _rows(btns, 4)
    rows.append(_nav_row(prefix))
    return InlineKeyboardMarkup(rows)


def exchange_grid_kb(prefix: str = "wz") -> InlineKeyboardMarkup:
    """Exchange grid — the live 5. Shared by both wizards."""
    btns = [
        InlineKeyboardButton(e, callback_data=f"{prefix}:ex:{e}")  # uppercase, matches the typed convention
        for e in EXCHANGE_DISPLAY
    ]
    rows = _rows(btns, 3)
    rows.append(_nav_row(prefix))
    return InlineKeyboardMarkup(rows)


def mode_kb(prefix: str = "wz") -> InlineKeyboardMarkup:
    """Alert-type grid — calls / regime / both (the {regime,calls,both} SoT)."""
    btns = [
        InlineKeyboardButton(_MODE_LABELS[m], callback_data=f"{prefix}:mode:{m}")
        for m in ("calls", "regime", "both")
    ]
    rows = _rows(btns, 3)
    rows.append(_nav_row(prefix))
    return InlineKeyboardMarkup(rows)


def confirm_followup_kb() -> InlineKeyboardMarkup:
    """Shown under a subscription confirmation card — keep the user in the loop."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add another", callback_data="mnu:watch"),
        InlineKeyboardButton("📋 My list", callback_data="mnu:list"),
    ]])


# ── Scan wizard (C3) — kind picker + top-N grid ──
def scan_kind_kb() -> InlineKeyboardMarkup:
    """One-shot scan vs a standing (recurring) scan digest."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ One-shot scan", callback_data="scn:kind:oneshot")],
        [InlineKeyboardButton("🔔 Standing digest", callback_data="scn:kind:standing")],
        [InlineKeyboardButton("✖️ Cancel", callback_data="scn:cancel")],
    ])


def topn_grid_kb(prefix: str = "scn") -> InlineKeyboardMarkup:
    """How many top perps (by OI) to scan."""
    btns = [InlineKeyboardButton(f"Top {n}", callback_data=f"{prefix}:n:{n}") for n in (5, 10, 20)]
    return InlineKeyboardMarkup([btns, _nav_row(prefix)])
