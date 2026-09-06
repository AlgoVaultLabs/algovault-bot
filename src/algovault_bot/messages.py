"""User-facing message strings.

D3-A: every CTA URL points at canonical ``api.algovault.com/signup`` (NOT
``algovault.com/signup`` — that surface 404s; verified live 2026-05-08). The PLAN rides as a
query parameter: text CTAs use ``signup_url`` (starter/month), and the plan picker's buttons use
``plan_signup_url`` with the SKU the user tapped (GROWTH-TG-PLAN-PICKER-W1 R3).

The welcome-message constants exposed here are byte-stable: the C1
verification gate fixtures grep against the literal lines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:  # pragma: no cover — types only, never a runtime import
    # `quota` imports THIS module, so importing `Ladder` at runtime would close the cycle.
    # `from __future__ import annotations` makes every annotation below a string, so the
    # TYPE_CHECKING-only form is complete: mypy sees the real type, Python never imports it.
    from .quota import Ladder

from .batch import DEFAULT_TOP_N, TF_ORDER
# `plan_ladder` is a pure-data LEAF that imports nothing from the package — see its docstring.
# `quota` imports THIS module, so the pinned ladder cannot be read from there; a default argument
# is evaluated at `def` time, which also rules out the deferred import `paywall.py` uses.
from .plan_ladder import STARTER_PRICE_6MONTH_USD
# `unlock` imports nothing local, so this cannot cycle — same edge `referral.py` uses.
from .unlock import normalize_lang


#: The signup path, WITHOUT a plan. GROWTH-TG-PLAN-PICKER-W1 R3 moved `?plan=starter` out of this
#: constant and into `plan_signup_url`, because the bot now offers four SKUs and the plan is an
#: ARGUMENT, not part of the address. A bare `/signup` renders the web plan picker (200) — correct
#: for a human, wrong for a button, because it loses per-button attribution. Every bot button
#: therefore carries `plan` (and `interval`) itself.
SIGNUP_BASE: Final = "api.algovault.com/signup"

#: The SKUs the bot can send a buyer to. `enterprise` is deliberately absent: it has no self-serve
#: Stripe Price, so a button for it would 4xx a paying prospect.
SignupPlan = Literal["starter", "pro"]
#: The billing terms `src/index.ts` accepts. `month` is the default and emits NO `interval` param,
#: which is what keeps the starter/month URL byte-identical to every historical row.
SignupInterval = Literal["month", "6month"]


def plan_signup_url(
    plan: SignupPlan,
    interval: SignupInterval,
    campaign: str,
    source: str | None = None,
) -> str:
    """THE composer for every bot-side signup URL. GROWTH-TG-PLAN-PICKER-W1 R3.

    Shape, and the PARAMETER ORDER is part of the contract::

        api.algovault.com/signup?plan=<plan>[&interval=6month]&utm_source=tg_bot
                                &utm_campaign=<campaign>[&utm_medium=<source>]

    🛑 `interval=month` EMITS NOTHING. `src/index.ts` reads
    `req.query.interval === '6month' ? '6month' : 'month'`, so month is the default there — and
    emitting it here would change the starter/month URL that ~400 historical
    `signup_attribution` rows were minted with, for no behavioural gain. Absence is absence, the
    same rule `source` already follows.

    Three ORTHOGONAL dimensions ride this URL and must never be collapsed:

        plan + interval -> WHICH SKU the buyer chose      (this wave)
        utm_campaign    -> WHICH in-bot CTA converted
        utm_medium      -> HOW the user found the bot

    ``utm_source`` stays ``tg_bot`` FOREVER. signal-MCP's ``deriveChannel``
    (src/lib/subscriber-attribution.ts) keys the channel slug off it, so re-slugging would orphan
    every historical row and break REVENUE-TRUTH-W1's join. Add; never re-slug.
    """
    url = f"{SIGNUP_BASE}?plan={plan}"
    if interval != "month":
        url = f"{url}&interval={interval}"
    url = f"{url}&utm_source=tg_bot&utm_campaign={campaign}"
    if source:
        url = f"{url}&utm_medium={source}"
    return url


def signup_url(campaign: str, source: str | None = None) -> str:
    """The starter/month projection of `plan_signup_url`. Used for every TEXT CTA.

    🛑 A PROJECTION, NOT A SECOND COMPOSER. It exists so the ~13 call sites that send a buyer to
    the default SKU in prose keep reading the way they always did, and so `tests/
    test_signup_url_source.py`'s byte-identity assertion — the one that guards ~400 historical
    `signup_attribution` rows against a silent re-slug — keeps passing UNEDITED.

    GROWTH-TG-CHANNEL-ACQUISITION-W1 (CH2): ``source`` is the subscriber's first-touch acquisition
    channel (db.get_acquisition_source), carried as ``utm_medium``. It is already read by
    signal-MCP (index.ts) and persisted to ``signup_attribution.utm_medium``, which was verified
    NULL on all 396 live rows at that wave — so it needed ZERO change in that repo.

    ``source=None`` (every pre-CH1 subscriber, and anyone who arrived untagged) emits the URL
    BYTE-IDENTICALLY to before that wave. Absence is absence: no empty parameter, no
    ``utm_medium=none``.
    """
    return plan_signup_url("starter", "month", campaign, source)


# ── the plan picker's copy (GROWTH-TG-PLAN-PICKER-W1 R3) ─────────────────────────────────────
#
# Ratified copy, dispatched as the wave's §Copy block. T2 voice: short sentences, action-verb
# lead, the `alerts` unit (the Telegram-bot carve-out — this bot meters a DELIVERED ALERT, the API
# meters a returned verdict), no HOLD claim, and NO urgency or scarcity framing on either prepay
# price. brand-facts.md: Starter $39.90/6mo is standing copy; Pro $129/6mo is limited-time by
# owner intent but NO end date exists, so the bot states the price and nothing else.
#
# 🛑 EVERY FIGURE IS INTERPOLATED FROM THE LADDER ARGUMENT. Not one is typed here — that is the
# whole point of R1 + R2, and gate legs L4/L4b/L5 exist to keep it that way.

#: The top-of-ladder sentence. ONE derivation, shared by the plan wall (`quota.
#: build_plan_refusal_text`) and the picker: a Pro subscriber must not be told two different
#: things by two surfaces answering the same question. Ratified 2026-08-17
#: (PRICING-BOT-DELIVERY-METERING-W1 CH5d) and live since.
TOP_SELF_SERVE_EN: Final = (
    "You are on the top self-serve plan — reply here and we will size the next step with you."
)
TOP_SELF_SERVE_ID: Final = (
    "Anda sudah di paket mandiri tertinggi — balas di sini dan kami bantu langkah berikutnya."
)
TOP_SELF_SERVE_ZH: Final = "您已使用最高自助套餐——请回复，我们将为您安排后续方案。"


#: The ⭐ demand probe's toast. GROWTH-TG-STARS-DEMAND-PROBE-W1, ratified §Copy.
#:
#: Telegram caps a callback answer at 200 characters and shows it for a few seconds over the
#: chat — so it must be true, complete and finished in one read. Two sentences, ≤20 words each:
#: the first is HONEST about what does not exist yet (a probe that implies a live rail is a lie
#: that converts), the second names what works TODAY so the tap is not a dead end, and it
#: promises a message on this surface rather than an email nobody gave us.
STARS_INTEREST_TOAST: Final = (
    "Noted — Stars checkout isn't live yet. "
    "Card checkout works today; we'll message you here when Stars is ready."
)


def plan_picker_text(ladder: Ladder, *, above_tier: str | None = None) -> str:
    """The message body the plan picker's keyboard hangs under.

    `above_tier` is the caller's EFFECTIVE tier (`quota.QuotaState.effective_tier.tier`, the single
    derivation every tier label in this bot projects from), or None for a free/unlinked chat:

        None        -> the full ladder, both rungs, four buttons
        'starter'   -> "move up to Pro": the Pro line only, two buttons
        'pro' | 'enterprise' -> the top-of-ladder sentence, NO buttons

    Pairs with `keyboards.plan_picker_kb`, which takes the same argument and returns None on
    exactly the branch that renders no buttons here. The two are kept in step by
    `tests/test_plan_picker.py` rather than by this comment.

    Plain text, no parse_mode — every caller sends it that way, so the domain auto-links and a
    stray `<` in a future edit cannot break the send.
    """
    if above_tier in ("pro", "enterprise"):
        return TOP_SELF_SERVE_EN
    pro_line = (
        f"Pro · {ladder.pro_monthly_calls:,} alerts/mo · {ladder.pro_daily_calls:,}/day"
    )
    if above_tier == "starter":
        return (
            "⬆️ Move up to Pro\n"
            "\n"
            f"{pro_line}\n"
            "\n"
            "Same key works in this bot and in the API. Card checkout via Stripe."
        )
    return (
        "⬆️ Upgrade — pick a plan\n"
        "\n"
        f"Starter · {ladder.starter_monthly_calls:,} alerts/mo · {ladder.starter_daily_calls:,}/day\n"
        f"{pro_line}\n"
        "\n"
        "Same key works in this bot and in the API. Card checkout via Stripe."
    )


# TG-COPY-DEFAULTS-VENUES-W1 (R1): plain-language onboarding, PLAIN TEXT (no HTML tags;
# handle_start sends without parse_mode so the plain domain auto-links). The clickable
# Upgrade CTA moved to /help (byte-identical URL); /start is link-light by design.
def _usd(amount: float) -> str:
    """`$9.99` / `$49` / `$39.90` — a price as it renders in copy.

    Mirrors signal-MCP's `plans.ts::planPriceLabel` so the same number renders identically on both
    sides of the estate: a WHOLE number gets no decimals, and everything else gets EXACTLY two.
    GROWTH-TG-QUOTA-PARITY-W1 CH3b-2.

    🛑 DO NOT "TIDY" A TRAILING ZERO AWAY. This function used to end
    `.rstrip("0").rstrip(".")`, which is NOT what `planPriceLabel` does — that helper is
    `Number.isInteger(p) ? p : p.toFixed(2)` and never strips. The divergence was invisible for as
    long as every price the bot rendered happened to have a non-zero cent (`$9.99`, `$49`), and it
    surfaced the moment GROWTH-TG-PLAN-PICKER-W1 R2 fed it the six-month total: the ratified brand
    figure `$39.90` came out as `$39.9`. `planPrepayPriceLabel(starter, 6)` returns `"$39.90"`,
    measured, and brand-facts.md carries `$39.90/6mo` as standing copy — so the bug was here, in
    the mirror, not in the number. Every previously-rendered value is byte-identical either way.
    """
    return f"${amount:.2f}" if amount % 1 else f"${int(amount)}"


def welcome_message(
    monthly_total: int,
    daily_total: int,
    starter_price_usd: float,
    starter_monthly_calls: int,
    starter_price_usd_6month: float = STARTER_PRICE_6MONTH_USD,
) -> str:
    """GROWTH-TG-QUOTA-PARITY-W1 CH3a — a FUNCTION, because a constant cannot interpolate.

    Every figure below arrives from the caller's `QuotaState`, which projects the ladder mirror.
    It was a module-level `Final` holding the allowance as a literal, bound to `quota.py` by
    nothing at all — which is precisely the defect this wave exists to retire. Gate leg L5 now
    makes the literal form unwritable, so this cannot regress quietly.

    GROWTH-TG-PLAN-PICKER-W1 R2 added the six-month total, which CH3a had to leave hand-typed
    because `/api/plans/public` carried no prepay field. It is DEFAULTED so every pre-existing
    caller and fixture emits a BYTE-IDENTICAL string with no edit — the default is the very
    constant `quota` would have served on the fallback path.
    """
    return (
    "👋 Welcome to AlgoVault, the brain layer for AI trading agents.\n"
    "\n"
    "I watch the markets for you and message you the moment something changes.\n"
    "\n"
    f"You get {monthly_total} free alerts a month, up to {daily_total} a day. "
    "Each alert I send uses one. Silent HOLDs are always free.\n"
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
    f"Free: {monthly_total} alerts/month, {daily_total}/day. Want more? Starter is "
    f"{_usd(starter_price_usd)}/mo or {_usd(starter_price_usd_6month)}/6mo for "
    f"{starter_monthly_calls:,} API calls/mo, or pay per call with x402."
    )


# TG-COPY-DEFAULTS-VENUES-W1 (R2): plain-language full guide, PLAIN TEXT (sent without
# parse_mode by _help, so <coin>/<timeframe> render literally). Upgrade URL byte-identical
# via signup_url('help_message'). 12 venues listed.
def help_message(monthly_total: int, daily_total: int) -> str:
    """GROWTH-TG-QUOTA-PARITY-W1 CH3a — see `welcome_message` for why this stopped being a
    constant."""
    return (
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
    f"Free tier: {monthly_total} alerts a month, up to {daily_total} a day. "
    "Regime and BUY/SELL alerts each use one. Silent HOLDs are free.\n"
    "Informational analytics, not financial advice.\n"
    # TG-SCANWATCH-TF-CADENCE-W1 (B): CTA lead-in → the inline Upgrade button renders below
    # (attached as reply_markup by _help); the raw signup URL line is gone.
    f"Need more than {monthly_total} alerts a month?"
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
    monthly_total: int,
    lang: str = "en",
) -> str:
    """Persistent confirmation card for a recurring subscribe. `kind` ∈ {watch, scanwatch}."""
    exch = exchange  # uppercase (BINANCE/HL/OKX…) — matches the typed convention
    if kind == "watch":
        mode_label = _CONFIRM_MODE_LABELS.get(mode or "calls", "Trade calls")
        return (
            "✅ You're now watching\n"
            f"{coin} · {tf} · {exch} · {mode_label}\n"
            f"📊 Regime + 📈 BUY/SELL alerts both count toward your {monthly_total}/mo\n"
            "Manage: /list · /unwatch"
        )
    if kind == "scanwatch":
        # TG-SCANWATCH-TF-CADENCE-W1: cadence == the timeframe; content-deduped to new calls.
        return (
            f"✅ Standing scan: top {top_n} · {tf} · {exch}\n"
            f"🔁 I'll re-check every {tf} and message only NEW BUY/SELL — repeats + HOLD rounds stay silent + free.\n"
            f"📈 Actionable digests count toward your {monthly_total} alerts/mo. "
            "Manage: /list · /unscanwatch"
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


def batch_confirm_message(
    n_combos: int, n_coins: int, n_tfs: int, n_exch: int, monthly_total: int
) -> str:
    return (
        f"⚠️ That's {n_combos} combos ({n_coins} coins × {n_tfs} TFs × {n_exch} exchanges) "
        f"→ expect a lot of alerts and your {monthly_total} free alerts/month can go fast. "
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


# PRICING-BOT-DELIVERY-METERING-W1 CH6a — `_TIER_QUOTA` is DELETED.
#
# It hard-typed a per-tier allowance dict that the server's ladder had long since moved past, so
# it had been wrong for every linked subscriber from the day it drifted — and `_quota_str`
# rendered a bare "no ceiling" word for any tier absent from that dict, which included `x402`
# (a member of PAID_TIERS), so an x402 subscriber was told they had no cap.
#
# The figures are deliberately NOT restated here, not even as history: gate leg L4b fails a
# ladder-shaped run of numbers in a comment, and it caught this very block on its first run. A
# comment that restates a ladder is a ladder that goes stale with nothing able to notice. The
# live values live in signal-MCP's `src/lib/plans.ts` and reach the bot only via the mirror.
#
# The bot no longer states a plan allowance it has not been told. Figures come from the server
# mirror; when there is no mirror the clause is OMITTED rather than guessed. Gate leg L4 makes a
# replacement dict unwritable.


def link_first_time_message(tier: str, monthly_total: int) -> str:
    return (
        f"✅ Linked! Your AlgoVault {tier} subscription is connected to this Telegram chat.\n"
        f"Alerts here now draw down your plan allowance instead of the free {monthly_total}/mo cap."
    )


def link_tier_changed_message(prev_tier: str | None, new_tier: str) -> str:
    prev = prev_tier or "free"
    return (
        f"✅ Subscription updated: {prev} → {new_tier}.\n"
        "Alerts here draw down your plan allowance."
    )


def link_already_linked_message(tier: str) -> str:
    return (
        f"This Telegram chat is already linked to your {tier} subscription. "
        "Alerts here draw down your plan allowance."
    )


def link_invalid_key_message() -> str:
    """Shown ONLY on a DETERMINED negative — the server told us there is no active
    subscription behind this key. OPS-BOT-LINKED-TIER-REFRESH-W1 CH1: it used to be shown
    for "we could not check" too, which is why the retry message below exists."""
    return (
        "❌ That signup link wasn't recognized. The API key in the link is "
        "either expired or doesn't match an active subscription.\n"
        "Sign up or recover your key: https://api.algovault.com/signup?plan=starter"
    )


def link_downgraded_message(
    monthly_total: int, daily_total: int, lang_code: str | None = None
) -> str:
    """OPS-BOT-LINKED-TIER-REFRESH-W1 CH3d — the downgrade notice.

    RATIFIED BY THE ARCHITECT 2026-08-21 — approved as-is, and the EN string was verified
    byte-identical to the approved wording before the send was enabled. It is LIVE: the gate
    in `entitlement_drain` is now default-ON with `ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED=0`
    as a kill switch.

    🛑 THIS IS RATIFIED PUBLIC COPY. Editing the EN string is a public-copy change requiring
    a fresh ratification — not a wording tidy-up. The `id` / `zh-hans` renderings are
    translations OF that approved string and move with it.

    Shape, and why each part is load-bearing: state the fact, assign no blame, give ONE
    action, and say explicitly what did NOT change — a subscriber whose watchlist silently
    vanished would read this as data loss on top of a billing problem. Trilingual through
    the existing `normalize_lang` path, each under 300 characters.
    """
    lang = normalize_lang(lang_code)
    url = signup_url("link_downgraded")
    if lang == "id":
        return (
            "Langganan AlgoVault Anda tampaknya sudah tidak aktif, jadi chat ini kembali ke "
            f"tier gratis ({monthly_total} alert/bulan, {daily_total}/hari). "
            "Watchlist Anda tidak berubah. "
            f"Aktifkan kembali kapan saja: {url}"
        )
    if lang == "zh-hans":
        return (
            f"你的 AlgoVault 订阅似乎已不再有效，此对话已回到免费套餐（每月 {monthly_total} 条提醒，"
            f"每日 {daily_total} 条）。"
            f"你的自选列表未受影响。随时可重新订阅：{url}"
        )
    return (
        "Your AlgoVault subscription no longer appears active, so this chat has moved back "
        f"to the free tier ({monthly_total} alerts/month, {daily_total}/day). "
        "Your watchlist is unchanged. "
        f"Reactivate any time: {url}"
    )


def link_could_not_verify_message() -> str:
    """OPS-BOT-LINKED-TIER-REFRESH-W1 CH1 — the INDETERMINATE reply.

    Assigns no blame to the customer's key, because at this point we have not
    established anything about it, and states plainly that nothing changed. One
    action, no signup link: sending a paying customer to the signup page implies
    their subscription is the problem, which is the very claim we cannot make.
    """
    return (
        "⏳ We couldn't verify your subscription just now — that's on our side, "
        "not your key.\n"
        "Nothing has changed here. Please tap the link again in a few minutes."
    )
