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
from .validators import EXCHANGES, TF_SECONDS, TIMEFRAMES

# Wizard TF grid: the typed path accepts all 11 TIMEFRAMES (incl. 1m), but the
# tap-grid curates to a 3m floor — 1m alerts are latency-noisy and a poor default
# for a one-tap subscribe. NOTE: there is no HIDE_TFS constant in the bot; this
# curation is wizard-grid-only — typed `/watch BTC 1m` is unchanged.
WIZARD_TIMEFRAMES: tuple[str, ...] = tuple(
    tf for tf in sorted(TIMEFRAMES, key=lambda t: TF_SECONDS[t]) if tf != "1m"
)

# Stable display order (the EXCHANGES frozenset is unordered); BINANCE first (default).
_EXCHANGE_ORDER: tuple[str, ...] = ("BINANCE", "BYBIT", "OKX", "BITGET", "HL")
EXCHANGE_DISPLAY: tuple[str, ...] = tuple(e for e in _EXCHANGE_ORDER if e in EXCHANGES)

# Fallback shortlist ONLY if the live top-OI fetch (asset_universe.get_top_assets)
# returns nothing — never the primary source.
FALLBACK_POPULAR_COINS: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE")

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


def main_menu_kb() -> InlineKeyboardMarkup:
    """The /start BotFather-style menu. Watch/Scan → wizards; others → existing
    handlers; Upgrade → signup URL button (utm preserved via signup_url)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Watch", callback_data="mnu:watch"),
         InlineKeyboardButton("🔍 Scan", callback_data="mnu:scan")],
        [InlineKeyboardButton("📡 Regime", callback_data="mnu:regime"),
         InlineKeyboardButton("🎯 Call", callback_data="mnu:call"),
         InlineKeyboardButton("💰 Funding", callback_data="mnu:funding")],
        [InlineKeyboardButton("📋 My list", callback_data="mnu:list"),
         InlineKeyboardButton("❓ Help", callback_data="mnu:help")],
        [InlineKeyboardButton("⬆️ Upgrade", url="https://" + signup_url("start_welcome"))],
    ])


def coin_grid_kb(coins: list[str], prefix: str = "wz", *, back: bool = False) -> InlineKeyboardMarkup:
    """Popular-coin shortlist (caller passes get_top_assets result) + 🔤 Type ticker."""
    btns = [InlineKeyboardButton(c, callback_data=f"{prefix}:coin:{c}") for c in coins]
    rows = _rows(btns, 3)
    rows.append([InlineKeyboardButton("🔤 Type ticker", callback_data=f"{prefix}:type")])
    rows.append(_nav_row(prefix, back=back))
    return InlineKeyboardMarkup(rows)


def tf_grid_kb(prefix: str = "wz") -> InlineKeyboardMarkup:
    """Timeframe grid — 3m–1d (WIZARD_TIMEFRAMES; 1m excluded). Shared by both wizards."""
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
