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


# TG-COPY-DEFAULTS-VENUES-W1 (R1): plain-language onboarding, PLAIN TEXT (no HTML tags;
# handle_start sends without parse_mode so the plain domain auto-links). The clickable
# Upgrade CTA moved to /help (byte-identical URL); /start is link-light by design.
WELCOME_MESSAGE: Final = (
    "👋 Welcome to AlgoVault, the brain layer for AI trading agents.\n"
    "\n"
    "I watch the markets for you and message you the moment something changes.\n"
    "\n"
    "You get 100 free calls a month. Each alert uses one call. Silent HOLDs are always free.\n"
    "\n"
    "Two kinds of alerts:\n"
    "📊 Regime: the market's mood flips (trending, ranging, or wild)\n"
    "📈 Trade call: a clear BUY or SELL\n"
    "\n"
    "You choose what to watch: 900+ markets (crypto, gold, stocks, pre-IPO) across 12 exchanges, on any timeframe from 1m to 1d.\n"
    "\n"
    "Start here:\n"
    "🔔 Watch a coin → /watch BTC 4h\n"
    "🔍 Scan the top movers → /scan\n"
    "📈 Get one call now → /call ETH 1h\n"
    "\n"
    "New here? Just type /watch and I'll start you on BTC 1h (Binance).\n"
    "\n"
    "📋 See your picks → /list\n"
    "❓ Every command → /help\n"
    "✅ Live, on-chain-verified results → algovault.com/track-record\n"
    "\n"
    "Free: 100 calls/month. Want more? Starter is $9.99/mo for 3,000 calls, or pay per call with x402."
)


# TG-COPY-DEFAULTS-VENUES-W1 (R2): plain-language full guide, PLAIN TEXT (sent without
# parse_mode by _help, so <coin>/<timeframe> render literally). Upgrade URL byte-identical
# via signup_url('help_message'). 12 venues listed.
HELP_MESSAGE: Final = (
    "📖 AlgoVault — full command guide\n"
    "\n"
    "I send you alerts when the market changes. You pick the coins, timeframes, and alert type.\n"
    "New here? Every command works on its own — just type /watch, /scan, or /call and I'll use a smart default.\n"
    "\n"
    "Every command uses three simple parts:\n"
    "• Coin — BTC, ETH, SOL … or a stock like XAU, TSLA, QQQ\n"
    "• Timeframe — 1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d\n"
    "• Exchange — optional, default Binance\n"
    "   HL · Binance · Bybit · OKX · Bitget · Aster · BingX · Gate · HTX · KuCoin · MEXC · Phemex\n"
    "\n"
    "━━ Get alerts ━━\n"
    "🔔 /watch <coin> <timeframe> [exchange] [regime|calls|both]\n"
    "Recurring alerts for a coin. Default alert type: calls.\n"
    "   /watch                      → BTC 1h on Binance (calls)\n"
    "   /watch BTC 4h               → BUY/SELL alerts, BTC 4h\n"
    "   /watch ETH 15m Bybit regime → mood-change alerts, ETH 15m on Bybit\n"
    "\n"
    "🔍 /scan [lens] [how many] [timeframe] [exchange]\n"
    "One-time ranking of the top coins with a live BUY/SELL.\n"
    "   /scan                       → top 20 by open interest, Binance, 15m\n"
    "\n"
    "🔁 /scanwatch [lens] [how many] [timeframe] [exchange]\n"
    "The scan on repeat — I re-check on your chosen timeframe and message only new BUY/SELL calls.\n"
    "   /scanwatch                  → top 20 by open interest, Binance, 15m (re-checked every 15m)\n"
    "\n"
    "Scan lenses (how to rank the top coins):\n"
    "   oi: most open interest (default)\n"
    "   volume: most traded (vol)\n"
    "   gainers: biggest 24h winners (gain)\n"
    "   losers: biggest 24h losers (lose)\n"
    "   movers: biggest moves either way (move)\n"
    "   funding_positive: crowded longs (pfr)\n"
    "   funding_negative: crowded shorts (nfr)\n"
    "   volatility: most volatile (atr)\n"
    "   oi_change: fastest-rising open interest (oid)\n"
    "\n"
    "━━ Check right now ━━\n"
    "📈 /call <coin> <timeframe> [exchange]     one BUY/SELL/HOLD call (default BTC 1h Binance)\n"
    "📊 /regime <coin> <timeframe> [exchange]   the market's mood, 1h/4h/1d (default BTC 1h Binance)\n"
    "💰 /funding [how many]                     biggest funding gaps across exchanges (default top 5)\n"
    "\n"
    "━━ Manage ━━\n"
    "📋 /list                          your watchlist\n"
    '✂️ /unwatch <coin> <timeframe>    remove one ("all" works for either)\n'
    "🧹 /unwatchall                    clear everything\n"
    "🔕 /unscanwatch [how many] [timeframe] [exchange]   stop a scan digest\n"
    "🎁 /referral                      invite friends, earn rewards\n"
    "\n"
    "Power moves:\n"
    "   /watch BTC all            every timeframe for BTC\n"
    "   /watch BTC,ETH,SOL 15m    three coins at once\n"
    "   /unwatch BTC all          remove every BTC watch\n"
    "\n"
    "Free tier: 100 calls a month. Regime and BUY/SELL alerts each use one call. Silent HOLDs are free.\n"
    "Informational analytics, not financial advice.\n"
    # TG-SCANWATCH-TF-CADENCE-W1 (B): CTA lead-in → the inline Upgrade button renders below
    # (attached as reply_markup by _help); the raw signup URL line is gone.
    "Need more than 100 calls a month?"
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
        # TG-SCANWATCH-TF-CADENCE-W1: cadence == the timeframe; content-deduped to new calls.
        return (
            f"✅ Standing scan: top {top_n} · {tf} · {exch}\n"
            f"🔁 I'll re-check every {tf} and message only NEW BUY/SELL — repeats + HOLD rounds stay silent + free.\n"
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
        # TG-COPY-DEFAULTS-VENUES-W1 (R3): fires only on UNPARSEABLE /watch — bare
        # /watch now runs the BTC 1h Binance calls default (handle_watch), not this.
        "🤔 I couldn't read that watch.\n"
        "\n"
        "Format: /watch <coin> <timeframe> [exchange] [regime|calls|both]\n"
        "Exchange and alert type are optional. Defaults: Binance, calls.\n"
        "\n"
        "Try:\n"
        "   /watch BTC 4h\n"
        "   /watch ETH 1h Bybit regime\n"
        "\n"
        "Tip: just type /watch for BTC 1h on Binance.\n"
        "❓ Full guide → /help"
    )


def usage_unwatch_message() -> str:
    return (
        "Usage: /unwatch <COIN> <TF> [EXCH]   (TF/EXCH can be \"all\")\n"
        "Example: /unwatch BTC 4h"
    )


def scan_error_message(arg: str) -> str:
    # TG-COPY-DEFAULTS-VENUES-W1 (R4): fires only on an UNPARSEABLE /scan token; {arg} is
    # the offending token (bare /scan runs the oi/20/15m/Binance default, not this).
    return (
        f"🤔 I didn't recognize \"{arg}\" in that scan.\n"
        "\n"
        "Format: /scan [lens] [how many] [timeframe] [exchange]\n"
        "Everything is optional. Default: top 20 by open interest, Binance, 15m.\n"
        "\n"
        "Try:\n"
        "   /scan            top 20 by open interest\n"
        "   /scan nfr 20     20 most-crowded shorts\n"
        "   /scan gain 1h    top 24h gainers, 1h\n"
        "\n"
        "Lenses: oi, volume, gainers, losers, movers, funding_positive, funding_negative, volatility, oi_change\n"
        "Want specific coins? Use /watch — scan ranks the whole market.\n"
        "❓ Full guide → /help"
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
