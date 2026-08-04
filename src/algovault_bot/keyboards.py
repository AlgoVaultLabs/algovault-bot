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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .messages import signup_url
from .validators import EXCHANGE_DISPLAY_ORDER, EXCHANGES, TF_SECONDS, TIMEFRAMES

# Wizard TF grid — the FULL supported set (1m–1d), ordered shortest→longest, in
# exact parity with the typed `/watch` path (no floor). Shared by both wizards.
WIZARD_TIMEFRAMES: tuple[str, ...] = tuple(sorted(TIMEFRAMES, key=lambda t: TF_SECONDS[t]))

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


def upgrade_button(campaign: str, source: str | None = None) -> InlineKeyboardButton:
    """TG-SCANWATCH-TF-CADENCE-W1 (B): the shared Upgrade CTA button (label + utm-tagged
    signup URL). /start's menu + /help both build from this → the CTA is single-derived.
    `campaign` feeds signup_url's utm_campaign (start_welcome / help_message).

    GROWTH-TG-CHANNEL-ACQUISITION-W1 (CH2): optional `source` feeds utm_medium — the
    subscriber's first-touch acquisition channel. Defaults to None so every existing
    caller emits a byte-identical URL."""
    return InlineKeyboardButton("⬆️ Upgrade", url="https://" + signup_url(campaign, source))


def upgrade_markup(campaign: str, source: str | None = None) -> InlineKeyboardMarkup:
    """Standalone one-button Upgrade markup — /help attaches this as its reply_markup."""
    return InlineKeyboardMarkup([[upgrade_button(campaign, source)]])


def main_menu_kb(source: str | None = None) -> InlineKeyboardMarkup:
    """The /start BotFather-style menu. Watch/Scan → wizards; others → existing
    handlers; Upgrade → the shared upgrade_button (utm preserved via signup_url).

    `source` (GROWTH-TG-CHANNEL-ACQUISITION-W1 CH2) is threaded to the Upgrade
    button only; None → byte-identical to pre-wave."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Watch", callback_data="mnu:watch"),
         InlineKeyboardButton("🔍 Scan", callback_data="mnu:scan")],
        [InlineKeyboardButton("📡 Regime", callback_data="mnu:regime"),
         InlineKeyboardButton("🎯 Call", callback_data="mnu:call"),
         InlineKeyboardButton("💰 Funding", callback_data="mnu:funding")],
        [InlineKeyboardButton("📋 My list", callback_data="mnu:list"),
         InlineKeyboardButton("❓ Help", callback_data="mnu:help")],
        [upgrade_button("start_welcome", source)],
    ])


def coin_grid_kb(coins: list[str], prefix: str = "wz", *, back: bool = False) -> InlineKeyboardMarkup:
    """Quick-pick shortlist (caller passes the curated WATCH_QUICKPICKS) + 🔤 Type ticker."""
    btns = [InlineKeyboardButton(c, callback_data=f"{prefix}:coin:{c}") for c in coins]
    rows = _rows(btns, 3)
    rows.append([InlineKeyboardButton("🔤 Type ticker", callback_data=f"{prefix}:type")])
    rows.append(_nav_row(prefix, back=back))
    return InlineKeyboardMarkup(rows)


def tf_grid_kb(prefix: str = "wz") -> InlineKeyboardMarkup:
    """Timeframe grid — 1m–1d (WIZARD_TIMEFRAMES; full parity with typed /watch). Shared by both wizards."""
    btns = [InlineKeyboardButton(tf, callback_data=f"{prefix}:tf:{tf}") for tf in WIZARD_TIMEFRAMES]
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
