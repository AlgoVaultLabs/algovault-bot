"""User-facing message strings.

D3-A: every CTA URL points at canonical ``api.algovault.com/signup?plan=starter``
(NOT ``algovault.com/signup`` — that surface 404s; verified live 2026-05-08).

The welcome-message constants exposed here are byte-stable: the C1
verification gate fixtures grep against the literal lines.
"""

from __future__ import annotations

from typing import Final

from .batch import DEFAULT_TOP_N, TF_ORDER


SIGNUP_BASE: Final = "api.algovault.com/signup?plan=starter"


def signup_url(campaign: str) -> str:
    """Build a UTM-tagged signup URL. Used for every bot-side CTA."""
    return f"{SIGNUP_BASE}&utm_source=tg_bot&utm_campaign={campaign}"


# TG-START-COPY-TRIM-W1: the /start welcome is sent with parse_mode=HTML so the
# upgrade CTA is a clickable link instead of a raw wrapping URL. The href is
# GENERATED from signup_url('start_welcome') (utm attribution byte-identical)
# with the scheme prepended and `&` HTML-escaped to `&amp;`. The four <…>
# placeholders are HTML-escaped to &lt;…&gt; so they render literally; every
# other char in the body is HTML-safe. KEEP this body fully escaped — it is
# sent as HTML (see handlers._start).
# ACTIVATION-NUDGE-W1 (2026-06-18): `upgrade_from=tg_start` added (utm preserved,
# A2) so the bot's /start CTA carries the primary funnel-attribution param the
# /signup handler reads for upgrade_cta_clicked.
_UPGRADE_HREF: Final = "https://" + (
    signup_url("start_welcome") + "&upgrade_from=tg_start"
).replace("&", "&amp;")

# ACTIVATION-NUDGE-W1: the public on-chain-verified track record — the
# trust→conversion lever surfaced in /start. Plain https link (no query to escape).
_TRACK_RECORD_HREF: Final = "https://algovault.com/track-record"

WELCOME_MESSAGE: Final = (
    "👋 Welcome to AlgoVault — the brain layer for AI trading agents.\n"
    "\n"
    "I push two kinds of alerts to your watchlist:\n"
    "📊 Regime shifts — count toward your free 100 calls/month\n"
    "📈 Trade calls (BUY/SELL) — count toward your free 100 calls/month\n"
    "HOLD verdicts are silent + free.\n"
    "\n"
    "Free tier covers all 900+ assets — crypto + TradFi (gold, stocks, pre-IPO) — and all 11 timeframes (1m–1d). YOU choose what to watch.\n"
    "\n"
    "Get started:\n"
    "/watch &lt;COIN&gt; &lt;TF&gt; &lt;Exch&gt; [regime|calls|both] — recurring alerts\n"
    "Example: ETH 15m Bybit regime / BTC All Binance / XRP 5m All\n"
    "/scan [TOP_N] [TF] [EXCH]    — one-shot scan of top perps\n"
    "/scanwatch [TOP_N] [TF] [EXCH] — recurring scan digest (BUY/SELL only)\n"
    "/regime &lt;COIN&gt; &lt;TF&gt; [EXCH]  — one-shot market regime\n"
    "/call &lt;COIN&gt; &lt;TF&gt; [EXCH]    — one-shot BUY/SELL/HOLD call\n"
    "/funding [TOP_N]            — cross-venue funding arb\n"
    "/list                       — see your picks\n"
    '/unwatch &lt;COIN&gt; &lt;TF&gt;        — remove one (TF/EXCH can be "all")\n'
    "/unwatchall                 — clear everything\n"
    "/unscanwatch [TOP_N] [TF] [EXCH] — stop a scan digest\n"
    "/help                       — full commands\n"
    "/referral                   — invite friends, earn rewards\n"
    "\n"
    # ACTIVATION-NUDGE-W1: track-record trust line + the upgrade CTA (button text
    # "Unlock 3,000 calls/mo →"; upgrade_from=tg_start, utm preserved). HTML-safe.
    "Free tier: 100 calls/month. See the live, on-chain-verified track record: "
    f'<a href="{_TRACK_RECORD_HREF}">algovault.com/track-record</a>.\n'
    f'<a href="{_UPGRADE_HREF}">Unlock 3,000 calls/mo →</a> with Starter ($9.99/mo), or pay per call via x402.'
)


HELP_MESSAGE: Final = (
    "AlgoVault Bot — full command list\n"
    "\n"
    "Args are positional: COIN TF EXCH (space-separated). EXCH optional, default BINANCE.\n"
    "\n"
    "/start — welcome + tap menu\n"
    "/watch <COIN> <TF> <Exch> [regime|calls|both] — recurring alerts (default calls)\n"
    "/scan [TOP_N] [TF] [EXCH] — one-shot scan of top perps by OI\n"
    "/scanwatch [TOP_N] [TF] [EXCH] — recurring scan digest (BUY/SELL only)\n"
    "/regime <COIN> <TF> [EXCH] — one-shot market regime (1h/4h/1d)\n"
    "/call <COIN> <TF> [EXCH] — one-shot BUY/SELL/HOLD call\n"
    "/funding [TOP_N] — cross-venue funding arb\n"
    "/list — see your picks\n"
    '/unwatch <COIN> <TF> — remove one (TF/EXCH can be "all")\n'
    "/unwatchall — clear everything\n"
    "/unscanwatch [TOP_N] [TF] [EXCH] — stop a scan digest\n"
    "/referral — invite friends, earn rewards\n"
    "/help — this message\n"
    "\n"
    "Values:\n"
    "  COIN — BTC ETH SOL … + TradFi (XAU QQQ TSLA SPCX)\n"
    "  TF   — 1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d\n"
    "  EXCH — HL BINANCE BYBIT OKX BITGET\n"
    "\n"
    "Examples:\n"
    "  /watch ETH 15m Bybit regime  — regime alerts, ETH 15m on Bybit\n"
    "  /watch BTC all               — BTC, every timeframe\n"
    "  /watch BTC,ETH,SOL 15m       — 3 coins, one timeframe\n"
    "  /unwatch BTC all             — remove every BTC watch\n"
    "\n"
    "Free tier: 100 calls/month — regime shifts + BUY/SELL count; HOLD is silent + free.\n"
    "Informational analytics, not financial advice.\n"
    f"Upgrade → {signup_url('help_message')}"
)


def cap_reached_message(cap: int = 50) -> str:
    return (
        f"⚠️ You've hit the per-user watchlist cap of {cap} entries.\n"
        f"Use /unwatch to drop one before adding more, or upgrade for unlimited tracking:\n"
        f"→ {signup_url('watchlist_cap')}"
    )


def watch_added_message(coin: str, timeframe: str, exchange: str, alert_type: str) -> str:
    types = {
        "regime": "regime only",
        "calls": "trade calls only",
        "both": "regime + calls",
    }
    return f"✅ Watching {coin} {timeframe} on {exchange} ({types[alert_type]})."


# TG-BUTTON-UX-W1 — the single persistent subscription-confirmation card. ONE
# renderer projected by the typed /watch + /scanwatch paths AND the Watch + Scan
# wizards (single-derivation). Plain text (no HTML special chars) so it renders
# identically via sendMessage and edit_message_text regardless of parse_mode.
# Used ONLY for RECURRING subscribes (/watch, /scanwatch) — never one-shot /scan,
# /call (their verdict IS the result). `lang` is reserved for forward-compat
# (the watch/scan surface is English today, matching watch_added_message).
_CONFIRM_MODE_LABELS: Final[dict[str, str]] = {
    "regime": "Regime",
    "calls": "Trade calls",
    "both": "Regime + Calls",
}


def format_subscription_confirmation(
    kind: str,
    *,
    coin: str | None = None,
    top_n: int | None = None,
    tf: str,
    exchange: str,
    mode: str | None = None,
    cadence: str | None = None,
    lang: str = "en",
) -> str:
    """Persistent confirmation card for a recurring subscribe. `kind` ∈ {watch, scanwatch}."""
    exch = exchange  # uppercase (BINANCE/HL/OKX…) — matches the typed convention
    if kind == "watch":
        mode_label = _CONFIRM_MODE_LABELS.get(mode or "calls", "Trade calls")
        return (
            "✅ You're now watching\n"
            f"{coin} · {tf} · {exch} · {mode_label}\n"
            "📊 Regime shifts — free · 📈 BUY/SELL count toward your 100/mo\n"
            "Manage: /list · /unwatch"
        )
    if kind == "scanwatch":
        return (
            f"✅ Standing scan: top {top_n} · {tf} · {exch} (every {cadence})\n"
            "🚀 You'll get a digest only on actionable BUY/SELL — HOLD rounds stay silent + free.\n"
            "📈 Actionable digests count toward your 100/mo. Manage: /list · /unscanwatch"
        )
    raise ValueError(f"unknown subscription kind: {kind!r}")


def watch_removed_message(coin: str, timeframe: str, exchange: str) -> str:
    return f"🗑️ No longer watching {coin} {timeframe} on {exchange}."


def watch_not_found_message(coin: str, timeframe: str, exchange: str) -> str:
    return f"That entry isn't on your watchlist: {coin} {timeframe} on {exchange}."


def list_empty_message() -> str:
    return (
        "Your watchlist is empty.\n"
        "Try `/watch BTC 4h` to start tracking. /help for the full command list."
    )


def list_message(rows: list[dict[str, str]], cap: int = 50) -> str:
    type_glyph = {"regime": "📊", "calls": "📈", "both": "📊📈"}
    lines = ["Your watchlist:"]
    for r in rows:
        glyph = type_glyph.get(r["alert_type"], "")
        # OPS-TRADE-CALL-CLUSTER-W1 CH4 — `nudge` field optional (handlers.py
        # populates from coverage_nudge.format_nudge_short); empty string for
        # backward-compatible call sites that don't pass a nudge.
        nudge = r.get("nudge", "")
        lines.append(
            f"  {glyph} {r['coin']} {r['timeframe']} on {r['exchange']}  ({r['alert_type']}){nudge}"
        )
    # TG-BATCH-WATCHLIST-W1: cap removed — footer is a plain count (no "/50").
    n = len(rows)
    lines.append(f"\n{n} watch{'es' if n != 1 else ''}.")
    return "\n".join(lines)


def list_summary_message(rows: list[dict[str, str]]) -> str:
    """TG-BATCH-WATCHLIST-W1: bounded grouped summary for large watchlists
    (used when a user has more than the per-page threshold). Aggregates by
    timeframe + exchange so the message length stays bounded regardless of
    how many coins are watched (a per-row dump of thousands is unusable)."""
    n = len(rows)
    coins = {r["coin"] for r in rows}
    by_tf: dict[str, int] = {}
    by_exch: dict[str, int] = {}
    for r in rows:
        by_tf[r["timeframe"]] = by_tf.get(r["timeframe"], 0) + 1
        by_exch[r["exchange"]] = by_exch.get(r["exchange"], 0) + 1
    # TF in canonical ascending order; exchanges by count desc.
    tf_lines = [
        f"  {tf}: {by_tf[tf]}" for tf in TF_ORDER if tf in by_tf
    ]
    exch_lines = [
        f"  {x}: {c}" for x, c in sorted(by_exch.items(), key=lambda kv: kv[1], reverse=True)
    ]
    lines = [
        f"📋 Your watchlist — {n} watches "
        f"({len(coins)} coins, {len(by_tf)} timeframes, {len(by_exch)} exchanges).",
        "",
        "By timeframe:",
        *tf_lines,
        "",
        "By exchange:",
        *exch_lines,
        "",
        "Too many to list one-by-one. Trim with /unwatch <COIN> all, "
        "/unwatch all <TF>, or /unwatchall.",
    ]
    return "\n".join(lines)


# ── TG-BATCH-WATCHLIST-W1 — batch add / nudge / bulk-remove copy ──

# Inline-keyboard button labels (T2 plain voice; honest to the byAsset.count
# "most-active" ranking — NOT "most-liquid", which would imply an AUM source).
def batch_btn_add_all(n: int) -> str:
    return f"✅ Add all {n}"


def batch_btn_top_n(n: int) -> str:
    return f"Top {n} most-active"


BATCH_BTN_CANCEL: Final = "Cancel"
UNWATCHALL_BTN_YES: Final = "Yes, clear all"
UNWATCHALL_BTN_CANCEL: Final = "Cancel"


def batch_watch_added_message(
    n_combos: int, n_coins: int, n_tfs: int, n_exch: int, alert_type: str
) -> str:
    types = {"regime": "regime only", "calls": "trade calls only", "both": "regime + calls"}
    return (
        f"✅ Watching {n_combos} combos: "
        f"{n_coins} coins × {n_tfs} TFs × {n_exch} exchanges ({types[alert_type]})."
    )


def batch_confirm_message(n_combos: int, n_coins: int, n_tfs: int, n_exch: int) -> str:
    return (
        f"⚠️ That's {n_combos} combos ({n_coins} coins × {n_tfs} TFs × {n_exch} exchanges) "
        f"→ expect a lot of alerts and your 100 free calls/month can go fast. "
        f"Add all, or start with the Top {DEFAULT_TOP_N} most-active?"
    )


def batch_cancelled_message() -> str:
    return "Cancelled — nothing added."


def batch_expired_message() -> str:
    return "That request expired. Re-run your /watch command."


def batch_unwatch_message(removed: int) -> str:
    if removed <= 0:
        return "Nothing matched — that isn't on your watchlist."
    return f"🗑️ Removed {removed} watch{'es' if removed != 1 else ''}."


def unwatchall_confirm_message(n: int) -> str:
    return f"Remove all {n} watch{'es' if n != 1 else ''}? This can't be undone."


def unwatchall_empty_message() -> str:
    return "Your watchlist is already empty."


def unwatchall_done_message(n: int) -> str:
    return f"🗑️ Cleared your watchlist — removed {n} watch{'es' if n != 1 else ''}."


def usage_watch_message() -> str:
    return (
        "Usage: /watch <COIN> <TF> <Exch> [regime|calls|both]\n"
        "Coin · TimeFrame · Exchange (space-separated). EXCH optional, default Binance.\n"
        "Examples:\n"
        "  /watch BTC 4h\n"
        "  /watch ETH 1h Bybit regime\n"
        "Type /help for the full command reference."
    )


def usage_unwatch_message() -> str:
    return (
        "Usage: /unwatch <COIN> <TF> [EXCH]   (TF/EXCH can be \"all\")\n"
        "Example: /unwatch BTC 4h"
    )


def symbol_unknown_message(coin: str, exchange: str) -> str:
    """Reply when /watch tried to add a symbol the upstream doesn't recognize.

    BOT-WATCH-VALIDATE-W1 (2026-05-17): preflight `get_trade_call` returned a
    clean null-call/null-price response — the upstream silently doesn't know
    the symbol. Tell the user before the watch lands in the DB and starts
    swallowing 1m ticks for days with zero alerts.
    """
    return (
        f"❌ '{coin}' isn't recognized by AlgoVault on {exchange}.\n"
        "\n"
        "TradFi symbols (Binance / Bybit / OKX / Bitget):\n"
        "  • GOLD or XAU — gold\n"
        "  • SP500 — S&P 500\n"
        "  • TSLA, NVDA, MSTR, AAPL — US stocks\n"
        "Crypto: use uppercase tickers (BTC, ETH, SOL, DOGE, etc.).\n"
        "\n"
        "Use /help for the full command reference."
    )


# ── BOT-W2 link confirmation messages ──────────────────────────


_TIER_QUOTA = {"starter": 3_000, "pro": 15_000, "enterprise": 100_000}


def _quota_str(tier: str) -> str:
    n = _TIER_QUOTA.get(tier)
    return f"{n:,}" if n else "unlimited"


def link_first_time_message(tier: str) -> str:
    return (
        f"✅ Linked! Your AlgoVault {tier} subscription is connected to this Telegram chat.\n"
        f"Bot-side quota refreshed to {_quota_str(tier)} calls/mo — the 100/mo cap no longer applies here."
    )


def link_tier_changed_message(prev_tier: str | None, new_tier: str) -> str:
    prev = prev_tier or "free"
    return (
        f"✅ Subscription updated: {prev} → {new_tier}.\n"
        f"Bot-side quota refreshed to {_quota_str(new_tier)} calls/mo."
    )


def link_already_linked_message(tier: str) -> str:
    return (
        f"This Telegram chat is already linked to your {tier} subscription. "
        f"Quota: {_quota_str(tier)} calls/mo."
    )


def link_invalid_key_message() -> str:
    return (
        "❌ That signup link wasn't recognized. The API key in the link is "
        "either expired or doesn't match an active subscription.\n"
        "Sign up or recover your key: https://api.algovault.com/signup?plan=starter"
    )
