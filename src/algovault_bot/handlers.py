"""Pure command-handler implementations.

Each ``handle_*`` function is a sync pure function returning the reply string.
The ``register_handlers`` adapter wires these to ``python-telegram-bot``'s
async ``Update`` API. Splitting the logic from the framework gives us a clean
unit-test seam — the C2 verification gate runs the pure handlers directly with
a temp SQLite, no Telegram sockets in the loop.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from typing import Sequence

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from datetime import datetime, timezone

from . import adoption, asset_universe, batch, keyboards, messages, referral, referral_client, wizard
from .admin import handle_stats as admin_handle_stats
from .coverage_nudge import compute_coverage_estimate, format_nudge, format_nudge_short
from .db import Database, PER_USER_WATCHLIST_CAP
from .link_validator import validate_api_key
from .log_setup import log_alert_event
from .mcp_client import McpError, from_env
from .validators import (
    DEFAULT_ALERT_TYPE,
    DEFAULT_EXCHANGE,
    EXCHANGES,
    TIMEFRAMES,
    ValidationError,
    normalize_alert_type,
    normalize_coin,
    normalize_exchange,
    normalize_timeframe,
)
from .quota import consume_quota, get_quota_state, record_call_delivered
from .capabilities import rank_label, rank_lens_help, recognized_rank_tokens
from .scan_digest import cadence_for_timeframe, is_valid_cadence, render_scan_digest_line, scan_digest_reminder

log = logging.getLogger(__name__)


# ── BOT-WATCH-VALIDATE-W1 — symbol preflight at /watch time ───
#
# Discovered 2026-05-17: a watch row whose symbol the upstream doesn't
# recognize (e.g. `XAUUSD` instead of `GOLD` / `XAU`) gets a 200 OK with
# null call/price on every cron tick — the alert engine treats null call
# as HOLD-equivalent and silently absorbs it. Result: the watch sits dead
# in the DB forever, never firing a single alert.
#
# Fix: at /watch insert time, fire one preflight `get_trade_call` against
# the proposed (coin, tf, exchange). If the upstream returns a clean null
# response, reject the watch before it lands. If the MCP itself errors,
# fail-open (allow the watch through; cron will sort it out — beats
# blocking legitimate watches during a transient outage).


def _validate_symbol_impl(coin: str, timeframe: str, exchange: str) -> str | None:
    """Real preflight implementation — fires a live MCP call. Tests call this
    directly to exercise the real logic; production calls ``_validate_symbol``
    which is just an alias (the alias seam exists so the test fixture can
    monkeypatch the alias to a no-op without breaking direct-impl tests)."""
    try:
        with from_env() as cli:
            result = cli.call_tool(
                "get_trade_call",
                {
                    "coin": coin,
                    "timeframe": timeframe,
                    "exchange": exchange,
                    "includeReasoning": False,
                },
            )
    except McpError as e:
        log.warning(
            json.dumps({
                "event": "watch_validate_mcp_failed",
                "coin": coin,
                "timeframe": timeframe,
                "exchange": exchange,
                "err": str(e)[:200],
            })
        )
        return None  # fail-open
    # Clean null response = upstream silently doesn't know this symbol.
    # A valid HOLD call returns call='HOLD' + a real price.
    if result.get("call") is None and result.get("price") is None:
        log.info(
            json.dumps({
                "event": "watch_rejected_unknown_symbol",
                "coin": coin,
                "timeframe": timeframe,
                "exchange": exchange,
            })
        )
        return messages.symbol_unknown_message(coin, exchange)
    return None


# Test seam — production wires through this alias. The conftest autouse
# fixture monkeypatches THIS alias to a no-op so handler tests don't hit
# a live MCP server. Tests that want to exercise the real logic call
# ``_validate_symbol_impl`` directly (which the autouse fixture leaves
# untouched).
def _validate_symbol(coin: str, timeframe: str, exchange: str) -> str | None:
    return _validate_symbol_impl(coin, timeframe, exchange)


# ── /start deep-link parameter (BOT-W2) ───────────────────────


_AUTH_PREFIX = "auth_"
_REF_PREFIX = "ref_"  # TG-REFERRAL-W1: /start ref_<CODE> deep-link referral join


# TG-BUTTON-UX-W1: curated Menu-button + `/` autocomplete list, set on boot via
# post_init → app.bot.set_my_commands (the Menu button is empty by default).
CURATED_COMMANDS: list[BotCommand] = [
    BotCommand("watch", "Recurring BUY/SELL + regime alerts for a coin"),
    BotCommand("scan", "One-shot scan of the top perps for actionable calls"),
    BotCommand("scanwatch", "Standing scan digest (BUY/SELL only)"),
    BotCommand("regime", "One-shot market regime for a coin"),
    BotCommand("call", "One-shot BUY/SELL/HOLD call for a coin"),
    BotCommand("funding", "Cross-venue funding-rate arbitrage scan"),
    BotCommand("list", "Show your watches + scan digests"),
    BotCommand("unwatch", "Remove a watch"),
    BotCommand("unwatchall", "Clear your entire watchlist"),
    BotCommand("unscanwatch", "Stop a scan digest"),
    BotCommand("referral", "Invite friends — they get bonus calls, you earn"),
    BotCommand("help", "Full command list"),
]


async def post_init(app: Application) -> None:
    """Populate Telegram's Menu button + `/` autocomplete on startup (TG-BUTTON-UX-W1).
    Best-effort — never block the polling loop on a transient Bot-API error."""
    try:
        await app.bot.set_my_commands(CURATED_COMMANDS)
        log.info('{"event": "set_my_commands_ok", "n": %d}', len(CURATED_COMMANDS))
    except Exception as e:  # noqa: BLE001
        log.warning('{"event": "set_my_commands_failed", "err": "%s"}', str(e)[:160])


# ── TG-BATCH-WATCHLIST-W1 — batch reply + helpers ──────────────


class BatchReply(str):
    """A handler reply that behaves exactly like its message string (so the
    existing pure-handler tests' ``in`` / ``==`` / ``startswith`` assertions
    keep working) while optionally carrying confirmation-keyboard metadata for
    the async wrapper:

    - ``confirm``: True → the wrapper attaches an inline keyboard instead of
      committing immediately.
    - ``pending``: the batch spec to stash in ``ctx.user_data`` under a token.
    - ``combos``: the expansion size (for the "Add all N" button label).
    """

    confirm: bool
    pending: dict | None
    combos: int

    def __new__(
        cls,
        text: str,
        *,
        confirm: bool = False,
        pending: dict | None = None,
        combos: int = 0,
    ) -> "BatchReply":
        obj = super().__new__(cls, text)
        obj.confirm = confirm
        obj.pending = pending
        obj.combos = combos
        return obj


def _batch_threshold() -> int:
    """Confirmation-nudge threshold (env ``BATCH_CONFIRM_THRESHOLD``, default 50)."""
    try:
        return int(
            os.environ.get("BATCH_CONFIRM_THRESHOLD", batch.DEFAULT_BATCH_CONFIRM_THRESHOLD)
        )
    except (TypeError, ValueError):
        return batch.DEFAULT_BATCH_CONFIRM_THRESHOLD


def _commit_watch_combos(
    db: Database,
    chat_id: int,
    combos: list[tuple[str, str, str]],
    n_coins: int,
    n_tfs: int,
    n_exch: int,
    alert_type: str,
    skip_preflight: bool,
) -> str:
    """Insert a batch of combos (idempotent). Preflights each DISTINCT new coin
    ONCE (never per combo — avoids the 41K-preflight trap on `all all all`);
    universe-sourced coins (`all` / Top-N) skip preflight (known-valid). On a
    rejected coin, nothing is inserted and the error is returned."""
    if not skip_preflight:
        existing = {
            (r["coin"], r["timeframe"], r["exchange"]) for r in db.list_watches(chat_id)
        }
        new_combos = [c for c in combos if c not in existing]
        checked: set[str] = set()
        for coin, tf, exch in new_combos:
            if coin in checked:
                continue
            checked.add(coin)
            err = _validate_symbol(coin, tf, exch)
            if err:
                return err

    inserted = db.add_watch_batch(chat_id, combos, alert_type)

    # TG-WATCH-ADOPTION-BROADCAST-W1 (R4): instrument typed-command watch
    # creation (source=command). The one-tap button path emits its own
    # source-attributed event in the callback handler; this is the single
    # signal for the "I typed /watch" path. One event per user action.
    if inserted > 0:
        first = combos[0] if combos else ("(batch)", "", "")
        adoption.emit_watch_created(
            chat_id,
            coin=first[0] if len(combos) == 1 else "(batch)",
            timeframe=first[1] if len(combos) == 1 else "",
            exchange=first[2] if len(combos) == 1 else "",
            source=adoption.SOURCE_COMMAND,
            created=True,
        )

    if len(combos) == 1:
        # TG-BUTTON-UX-W1: persistent confirmation card (shared renderer) + coverage nudge.
        coin, tf, exch = combos[0]
        base = messages.format_subscription_confirmation(
            "watch", coin=coin, tf=tf, exchange=exch, mode=alert_type
        )
        try:
            est = compute_coverage_estimate(coin, tf, exch)
            return base + format_nudge(coin, tf, exch, est)
        except Exception as exc:  # noqa: BLE001 — bot must never crash on nudge failure
            log.warning("coverage nudge failed for %s %s %s: %s", coin, tf, exch, exc)
            return base
    return messages.batch_watch_added_message(len(combos), n_coins, n_tfs, n_exch, alert_type)


# ── pure handlers ──────────────────────────────────────────────


def handle_start(
    db: Database, chat_id: int, username: str | None, lang_code: str | None
) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    # BOT-ZOMBIE-W1: if this subscriber was previously marked bot-blocked,
    # /start is hard proof they've unblocked. Clear the flag so digest/stats
    # count them as a reachable subscriber again.
    db.unmark_subscriber_blocked(chat_id)
    return messages.WELCOME_MESSAGE


def handle_link(
    db: Database,
    chat_id: int,
    username: str | None,
    lang_code: str | None,
    api_key: str,
) -> str:
    """Validate api_key against signal-MCP, then link this chat to it.

    BOT-W2 / D1-C: api_key arrives via the `/start auth_<api_key>` deep-link
    fired from the /welcome page. Bot validates the key via signal-MCP's
    /api/bot/validate-key endpoint (loopback, internal-bypass-gated) and
    stores the linked tier so the C3 quota gate can honor it.

    NEVER log the api_key value at INFO. Only structured fields.
    """
    db.upsert_subscriber(chat_id, username, lang_code)
    validated = validate_api_key(api_key)
    if validated is None:
        log.info(
            '{"event": "link_failed", "chat_id": %d, "reason": "validation_returned_none"}',
            chat_id,
        )
        return messages.link_invalid_key_message()

    previous_tier, is_new_link = db.link_subscriber(chat_id, api_key, validated.tier)
    log.info(
        '{"event": "link_ok", "chat_id": %d, "tier": "%s", "is_new_link": %s, '
        '"previous_tier": "%s"}',
        chat_id,
        validated.tier,
        "true" if is_new_link else "false",
        previous_tier or "null",
    )

    if is_new_link:
        return messages.link_first_time_message(validated.tier)
    if previous_tier != validated.tier:
        return messages.link_tier_changed_message(previous_tier, validated.tier)
    return messages.link_already_linked_message(validated.tier)


def handle_help(db: Database, chat_id: int, username: str | None, lang_code: str | None) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    return messages.HELP_MESSAGE


# ── FEATURE-PARITY-CHANNELS-W1 CH3 — /scan (pull the market scanner) ──
DEFAULT_SCAN_TOP_N = 20
DEFAULT_SCAN_TF = "15m"


def _scan_via_mcp_impl(top_n: int, timeframe: str, exchange: str, rank_by: str | None = None) -> dict:
    """Real scan call — fires scan_trade_calls over the internal-bypass MCP edge. The RAW
    rank token is forwarded as `rankBy` (the MCP resolves the alias); omitted ⇒ default oi
    (byte-identical to the historical /scan call)."""
    # SCAN-DIGEST-MCP-PARITY-W1 CH3: /scan renders the enriched line (price + drivers +
    # reasoning) from ONE enriched scan — no per-coin get_trade_call depth call.
    payload: dict = {"topN": top_n, "timeframe": timeframe, "exchange": exchange, "includeReasoning": True}
    if rank_by is not None:
        payload["rankBy"] = rank_by
    with from_env() as cli:
        return cli.call_tool("scan_trade_calls", payload)


# Test seam (mirrors _validate_symbol): tests monkeypatch THIS alias to return a
# fixture result without a live MCP server.
def _scan_via_mcp(top_n: int, timeframe: str, exchange: str, rank_by: str | None = None) -> dict:
    return _scan_via_mcp_impl(top_n, timeframe, exchange, rank_by)


def _parse_scan_args(args: list[str]) -> tuple[int, str, str, str | None]:
    """Tolerant positional parse of [RANK] [TOP_N] [TIMEFRAME] [EXCHANGE] (any order).
    The scanner ranks the top-N perps by the chosen lens (default OI) — it takes no coin
    list (use /watch for specific coins). A RANK token is recognized against the
    /capabilities-advertised set and forwarded RAW (the MCP resolves the alias). Raises
    ValidationError on an unrecognized token (lens tokens are disjoint from TF/EXCHANGE/digits)."""
    top_n, timeframe, exchange = DEFAULT_SCAN_TOP_N, DEFAULT_SCAN_TF, DEFAULT_EXCHANGE
    rank: str | None = None
    for raw in args:
        tok = raw.strip()
        if tok.isdigit():
            n = int(tok)
            if not 1 <= n <= 100:
                raise ValidationError("TOP_N must be between 1 and 100")
            top_n = n
        elif tok.lower() in TIMEFRAMES:
            timeframe = tok.lower()
        elif tok.upper() in EXCHANGES:
            exchange = tok.upper()
        elif tok.lower() in recognized_rank_tokens():
            rank = tok.lower()
        else:
            raise ValidationError(f"unrecognized argument {raw!r}. {rank_lens_help()}")
    return top_n, timeframe, exchange, rank


def _format_scan_reply(
    result: dict, top_n: int, timeframe: str, exchange: str, rank: str | None = None
) -> str:
    calls = result.get("calls") or []
    non_hold = [c for c in calls if c.get("call") not in (None, "HOLD")]
    scanned = result.get("scanned", 0)
    header = f"🔍 Scan — top {top_n} perps by {rank_label(rank)} on {exchange} @ {timeframe}"
    if not non_hold:
        return f"{header}\n\nNo actionable BUY/SELL calls right now ({scanned} scanned)."
    # SCAN-DIGEST-MCP-PARITY-W1 CH3: render each actionable line via the ONE shared
    # render_scan_digest_line (== the MCP renderScanDigestLine + the /scanwatch digest;
    # single-derivation). The enriched scan carries price + drivers + reasoning per call;
    # the rank lens still SELECTS the universe (rank_value rides in the structured payload).
    blocks = [render_scan_digest_line(c) for c in non_hold]
    return "\n\n".join([f"{header} — {len(non_hold)} actionable:", *blocks])


def handle_scan(
    db: Database, chat_id: int, username: str | None, lang_code: str | None, args: list[str]
) -> str:
    """Pull the market scanner (scan_trade_calls) + meter max(1, non-HOLD) against the
    user's monthly quota (HOLD-only scan = 1; paid tiers = no-op via consume_quota)."""
    db.upsert_subscriber(chat_id, username, lang_code)
    try:
        top_n, timeframe, exchange, rank = _parse_scan_args(list(args))
    except ValidationError as e:
        return (
            "Usage: /scan [RANK] [TOP_N] [TF] [EXCH]\n"
            "Ranks the top-N perps by the chosen lens for actionable (BUY/SELL) calls.\n"
            "  /scan            — top 20 by OI on BINANCE @ 15m\n"
            "  /scan nfr 20     — most-negative funding (crowded shorts)\n"
            "  /scan gain 1h    — top 24h gainers @ 1h\n"
            f"{rank_lens_help()}\n"
            "(For specific coins use /watch — the scanner takes no coin list.)\n"
            f"↳ {e}"
        )
    state = get_quota_state(db, chat_id)
    if state.exhausted:
        return (
            f"You've used all {state.total} free calls this month. "
            f"Upgrade for more: {messages.signup_url('scan_quota_exhausted')}"
        )
    try:
        result = _scan_via_mcp(top_n, timeframe, exchange, rank)
    except McpError as e:
        log.warning(json.dumps({"event": "scan_mcp_failed", "err": str(e)[:200]}))
        return "⚠️ The scanner is temporarily unavailable — please try again shortly."
    calls = result.get("calls") or []
    non_hold = sum(1 for c in calls if c.get("call") not in (None, "HOLD"))
    # BOT-DIGEST-COUNT-ALL-CALLS-W1: record + meter one per actionable call returned
    # (alerts_fired + quota via the shared recorder) so /scan calls show in the digest.
    # K≥1 → K units == the prior max(1, K). An all-HOLD scan (K=0) delivers no call to
    # record but still charges the 1-unit request floor (unchanged /scan billing).
    for _ in range(non_hold):
        record_call_delivered(db, chat_id, "scan")
    if non_hold == 0:
        consume_quota(db, chat_id)
    return _format_scan_reply(result, top_n, timeframe, exchange, rank)


# ── ON-DEMAND PER-COIN PULLS — /regime + /call ───────────────────────
#
# /scan covers the whole-market scanner (scan_trade_calls); these two add the
# per-coin one-shot pulls for get_market_regime / get_trade_call. Both tools are
# channels.bot=true in the feature registry (SOT), so this is ADDITIVE bot
# surface — no registry change. The recurring side of the same tools is
# /watch <COIN> <TF> [EXCH] regime|calls|both. Shape mirrors /scan (impl +
# test-seam + parse + format).

# get_market_regime classifies at 1h/4h/1d only; finer TFs coarse-grain to 1h
# (same as the alert engine's regime path, alert_engine.py).
REGIME_TFS = ("1h", "4h", "1d")

_REGIME_GLYPHS = {
    "TRENDING_UP": "📈",
    "TRENDING_DOWN": "📉",
    "RANGING": "↔️",
    "VOLATILE": "🌪️",
}

_USAGE_REGIME = (
    "Usage: /regime <COIN> <TF> [EXCH]\n"
    "One-shot market regime for a coin (TRENDING_UP/DOWN, RANGING, VOLATILE).\n"
    "  /regime BTC 1h          — BTC 1h on BINANCE\n"
    "  /regime ETH 4h BYBIT    — ETH 4h on Bybit\n"
    "Classified at 1h/4h/1d (finer TFs map to 1h)."
)
_USAGE_CALL = (
    "Usage: /call <COIN> <TF> [EXCH]\n"
    "One-shot BUY/SELL/HOLD trade call for a coin.\n"
    "  /call SOL 15m           — SOL 15m on BINANCE\n"
    "  /call BTC 1h HL         — BTC 1h on Hyperliquid\n"
    "Use /watch for recurring alerts."
)


def _regime_via_mcp_impl(coin: str, timeframe: str, exchange: str) -> dict:
    """Real call — fires get_market_regime over the internal-bypass MCP edge."""
    with from_env() as cli:
        return cli.call_tool(
            "get_market_regime",
            {"coin": coin, "timeframe": timeframe, "exchange": exchange},
        )


# Test seam (mirrors _scan_via_mcp): tests monkeypatch THIS alias.
def _regime_via_mcp(coin: str, timeframe: str, exchange: str) -> dict:
    return _regime_via_mcp_impl(coin, timeframe, exchange)


def _call_via_mcp_impl(coin: str, timeframe: str, exchange: str) -> dict:
    """Real call — fires get_trade_call (with reasoning) over the bypass edge."""
    with from_env() as cli:
        return cli.call_tool(
            "get_trade_call",
            {
                "coin": coin,
                "timeframe": timeframe,
                "exchange": exchange,
                "includeReasoning": True,
            },
        )


# Test seam.
def _call_via_mcp(coin: str, timeframe: str, exchange: str) -> dict:
    return _call_via_mcp_impl(coin, timeframe, exchange)


def _parse_coin_tf_exchange(args: list[str]) -> tuple[str, str, str]:
    """Parse ``<COIN> <TF> [EXCHANGE]`` for the per-coin on-demand commands.
    COIN + TF required; EXCHANGE defaults to BINANCE. Raises ValidationError."""
    if len(args) < 2:
        raise ValidationError("need a coin and a timeframe")
    if len(args) > 3:
        raise ValidationError("too many arguments")
    coin = normalize_coin(args[0])
    timeframe = normalize_timeframe(args[1])
    exchange = normalize_exchange(args[2]) if len(args) >= 3 else DEFAULT_EXCHANGE
    return coin, timeframe, exchange


def _format_regime_reply(coin: str, timeframe: str, exchange: str, result: dict) -> str:
    regime = result.get("regime", "?")
    conf = result.get("confidence")
    glyph = _REGIME_GLYPHS.get(str(regime), "📊")
    conf_str = f" · conf {conf}" if conf is not None else ""
    lines = [f"📊 Regime — {coin} {timeframe} on {exchange}", "", f"{glyph} {regime}{conf_str}"]
    suggestion = result.get("suggestion")
    if suggestion:
        lines.append(f"💡 {suggestion}")
    return "\n".join(lines)


def handle_regime(
    db: Database, chat_id: int, username: str | None, lang_code: str | None, args: list[str]
) -> str:
    """On-demand get_market_regime for one coin. Per-call quota (not HOLD-free):
    an exhausted user is asked to upgrade before the call fires (like /scan)."""
    db.upsert_subscriber(chat_id, username, lang_code)
    try:
        coin, timeframe, exchange = _parse_coin_tf_exchange(list(args))
    except ValidationError as e:
        return f"{_USAGE_REGIME}\n↳ {e}"
    state = get_quota_state(db, chat_id)
    if state.exhausted:
        return (
            f"You've used all {state.total} free calls this month. "
            f"Upgrade for more: {messages.signup_url('regime_quota_exhausted')}"
        )
    regime_tf = timeframe if timeframe in REGIME_TFS else "1h"
    try:
        result = _regime_via_mcp(coin, regime_tf, exchange)
    except McpError as e:
        log.warning(json.dumps({"event": "regime_mcp_failed", "err": str(e)[:200]}))
        return "⚠️ The regime classifier is temporarily unavailable — please try again shortly."
    if not result.get("regime"):
        return messages.symbol_unknown_message(coin, exchange)
    consume_quota(db, chat_id, units=1)
    return _format_regime_reply(coin, regime_tf, exchange, result)


def _format_call_reply(
    coin: str, timeframe: str, exchange: str, result: dict
) -> str:
    call = (result.get("call") or "HOLD").upper()
    mark = {"BUY": "🟢", "SELL": "🔴"}.get(call, "⚪")
    bits = []
    conf = result.get("confidence")
    if conf is not None:
        bits.append(f"conf {conf}")
    regime = result.get("regime")
    if regime:
        bits.append(str(regime))
    price = result.get("price")
    if isinstance(price, (int, float)):
        bits.append(f"${price:,.2f}")
    lines = [f"{mark} {call}: {coin} {timeframe} on {exchange}"]
    if bits:
        lines.append(" · ".join(bits))
    reasoning = result.get("reasoning")
    if reasoning:
        lines += ["", str(reasoning)]
    return "\n".join(lines)


def handle_call(
    db: Database, chat_id: int, username: str | None, lang_code: str | None, args: list[str]
) -> str:
    """On-demand get_trade_call for one coin. HOLD-free metering (parity with the
    watch engine): a HOLD is always shown free; a BUY/SELL costs 1 call and is
    gated behind the monthly quota."""
    db.upsert_subscriber(chat_id, username, lang_code)
    try:
        coin, timeframe, exchange = _parse_coin_tf_exchange(list(args))
    except ValidationError as e:
        return f"{_USAGE_CALL}\n↳ {e}"
    try:
        result = _call_via_mcp(coin, timeframe, exchange)
    except McpError as e:
        log.warning(json.dumps({"event": "call_mcp_failed", "err": str(e)[:200]}))
        return "⚠️ The signal engine is temporarily unavailable — please try again shortly."
    call = (result.get("call") or "").upper()
    if not call and result.get("price") is None:
        return messages.symbol_unknown_message(coin, exchange)
    if call in ("BUY", "SELL"):
        state = get_quota_state(db, chat_id)
        if state.exhausted:
            return (
                f"You've used all {state.total} free calls this month. "
                f"Upgrade for more: {messages.signup_url('call_quota_exhausted')}"
            )
        consume_quota(db, chat_id, units=1)
    return _format_call_reply(coin, timeframe, exchange, result)


# ── /funding — cross-venue funding-rate arbitrage (scan_funding_arb) ──────
# BOT-FUNDING-SOT-W1: scan_funding_arb was flipped channels.bot=true in the MCP
# registry (the SOT); this is its bot command. Cross-venue (NO exchange arg) —
# ranks the largest long/short funding spreads across all 5 venues.
DEFAULT_FUNDING_LIMIT = 5
_DEFAULT_FUNDING_MIN_BPS = 5

_USAGE_FUNDING = (
    "Usage: /funding [TOP_N]\n"
    "Cross-venue funding-rate arbitrage — biggest long/short spreads across\n"
    "Binance, Bybit, OKX, Bitget, Hyperliquid (no exchange arg — it scans all).\n"
    "  /funding        — top 5 spreads\n"
    "  /funding 10     — top 10"
)


def _funding_via_mcp_impl(limit: int, min_spread_bps: int) -> dict:
    """Real call — fires scan_funding_arb over the internal-bypass MCP edge."""
    with from_env() as cli:
        return cli.call_tool(
            "scan_funding_arb",
            {"limit": limit, "minSpreadBps": min_spread_bps},
        )


# Test seam.
def _funding_via_mcp(limit: int, min_spread_bps: int) -> dict:
    return _funding_via_mcp_impl(limit, min_spread_bps)


def _parse_funding_args(args: list[str]) -> int:
    """Parse the optional [TOP_N] for /funding (1–20, default 5)."""
    if not args:
        return DEFAULT_FUNDING_LIMIT
    if len(args) > 1:
        raise ValidationError("too many arguments")
    tok = args[0].strip()
    if not tok.isdigit():
        raise ValidationError(f"unrecognized argument {args[0]!r}")
    n = int(tok)
    if not 1 <= n <= 20:
        raise ValidationError("TOP_N must be between 1 and 20")
    return n


def _format_funding_reply(opps: list, limit: int) -> str:
    header = f"💰 Funding arb — top {limit} cross-venue spreads"
    if not opps:
        return f"{header}\n\nNo funding spreads above threshold right now."
    lines = [f"{header} — {min(len(opps), limit)} found:", ""]
    for o in opps[:limit]:
        coin = o.get("coin", "?")
        arb = o.get("bestArb") or {}
        longv = arb.get("longVenue", "?")
        shortv = arb.get("shortVenue", "?")
        bits = []
        bps = arb.get("spreadBps")
        if isinstance(bps, (int, float)):
            bits.append(f"{bps:.1f}bps")
        apr = arb.get("annualizedPct")
        if isinstance(apr, (int, float)):
            bits.append(f"{apr:.0f}% APR")
        urgency = (arb.get("urgency") or {}).get("label")
        if urgency:
            bits.append(str(urgency))
        suffix = (" · " + " · ".join(bits)) if bits else ""
        lines.append(f"• {coin}: long {longv} / short {shortv}{suffix}")
    return "\n".join(lines)


def handle_funding(
    db: Database, chat_id: int, username: str | None, lang_code: str | None, args: list[str]
) -> str:
    """On-demand scan_funding_arb (cross-venue). Per-call quota (like /scan): an
    exhausted user is asked to upgrade before the scan fires."""
    db.upsert_subscriber(chat_id, username, lang_code)
    try:
        limit = _parse_funding_args(list(args))
    except ValidationError as e:
        return f"{_USAGE_FUNDING}\n↳ {e}"
    state = get_quota_state(db, chat_id)
    if state.exhausted:
        return (
            f"You've used all {state.total} free calls this month. "
            f"Upgrade for more: {messages.signup_url('funding_quota_exhausted')}"
        )
    try:
        result = _funding_via_mcp(limit, _DEFAULT_FUNDING_MIN_BPS)
    except McpError as e:
        log.warning(json.dumps({"event": "funding_mcp_failed", "err": str(e)[:200]}))
        return "⚠️ The funding scanner is temporarily unavailable — please try again shortly."
    consume_quota(db, chat_id, units=1)
    return _format_funding_reply(result.get("opportunities") or [], limit)


# ── FEATURE-PARITY-CHANNELS-W1 CH4 — /scanwatch (scheduled scan digest → chat) ──


def _parse_scanwatch_args(args: list[str]) -> tuple[int, str, str, str, str | None]:
    """Positional-ish [RANK] [TOP_N] [TF] [EXCHANGE] [CADENCE]. Cadence values (1h/4h/1d)
    are also valid timeframes, so the FIRST time-token is the TF and the SECOND is the
    cadence. A RANK token (lens, forwarded raw) is recognized against the /capabilities
    set — disjoint from TF/cadence/exchange/digits. Cadence defaults to
    cadence_for_timeframe(tf); rank defaults None (→ 'oi')."""
    top_n, timeframe, exchange, cadence = DEFAULT_SCAN_TOP_N, DEFAULT_SCAN_TF, DEFAULT_EXCHANGE, None
    rank: str | None = None
    seen_tf = False
    for raw in args:
        tok = raw.strip()
        if tok.isdigit():
            n = int(tok)
            if not 1 <= n <= 100:
                raise ValidationError("TOP_N must be between 1 and 100")
            top_n = n
        elif tok.upper() in EXCHANGES:
            exchange = tok.upper()
        elif tok.lower() in TIMEFRAMES:
            if not seen_tf:
                timeframe = tok.lower()
                seen_tf = True
            elif is_valid_cadence(tok.lower()):
                cadence = tok.lower()
            else:
                raise ValidationError(f"cadence must be one of 1h/4h/1d, got {raw!r}")
        elif tok.lower() in recognized_rank_tokens():
            rank = tok.lower()
        else:
            raise ValidationError(f"unrecognized argument {raw!r}. {rank_lens_help()}")
    if cadence is None:
        cadence = cadence_for_timeframe(timeframe)
    return top_n, timeframe, exchange, cadence, rank


def _scanwatch_usage(err: object) -> str:
    return (
        "Usage: /scanwatch [RANK] [TOP_N] [TF] [EXCH]\n"
        "Schedules a recurring whole-market scan digest pushed to this chat.\n"
        "  /scanwatch              — top 20 by OI on BINANCE @ 15m, every 1h\n"
        "  /scanwatch nfr 4h       — most-negative funding @ 4h (crowded shorts)\n"
        "  /scanwatch 4h 1d BYBIT  — top 20 @ 4h on BYBIT, every 1d\n"
        "Cadence ∈ {1h, 4h, 1d}; defaults from your timeframe (floor 1h).\n"
        f"{rank_lens_help()}\n"
        f"↳ {err}"
    )


def handle_scanwatch(
    db: Database, chat_id: int, username: str | None, lang_code: str | None, args: list[str]
) -> str:
    """Create/update a scheduled scan-digest subscription (the alert-engine cron pushes it)."""
    db.upsert_subscriber(chat_id, username, lang_code)
    try:
        top_n, timeframe, exchange, cadence, rank = _parse_scanwatch_args(list(args))
    except ValidationError as e:
        return _scanwatch_usage(e)
    rank_by = rank or "oi"
    inserted = db.add_scan_watch(chat_id, top_n, timeframe, exchange, cadence, rank_by)
    if inserted:
        # TG-WATCH-ADOPTION-BROADCAST-W1 (R4): instrument typed `/scanwatch`.
        adoption.emit_scan_watch_created(
            chat_id, top_n, timeframe, exchange, cadence,
            source=adoption.SOURCE_COMMAND, created=True,
        )
    # TG-BUTTON-UX-W1: persistent standing-scan confirmation card (shared renderer)
    # + the cadence-vs-timeframe reminder (preserves the faster-than-TF heads-up).
    card = messages.format_subscription_confirmation(
        "scanwatch", top_n=top_n, tf=timeframe, exchange=exchange, cadence=cadence
    )
    # SCAN-RANKBY-W1: surface the lens on the confirmation when it's not the default oi.
    if rank_by != "oi":
        card = f"{card}\nLens: {rank_label(rank_by)}."
    reminder = scan_digest_reminder(cadence, timeframe)
    return f"{card}\n{reminder}" if reminder else card


def handle_unscanwatch(
    db: Database, chat_id: int, username: str | None, lang_code: str | None, args: list[str]
) -> str:
    """Remove a scheduled scan-digest subscription (matched by TOP_N/TF/EXCHANGE)."""
    db.upsert_subscriber(chat_id, username, lang_code)
    try:
        top_n, timeframe, exchange, _, rank = _parse_scanwatch_args(list(args))
    except ValidationError as e:
        return _scanwatch_usage(e)
    rank_by = rank or "oi"
    removed = db.remove_scan_watch(chat_id, top_n, timeframe, exchange, rank_by)
    lens = "" if rank_by == "oi" else f" [{rank_label(rank_by)}]"
    if removed:
        return f"Removed scan digest — top {top_n} on {exchange} @ {timeframe}{lens}."
    return f"No scan digest found for top {top_n} on {exchange} @ {timeframe}{lens}. See /list."


def _format_scan_watch_section(scan_rows: list) -> str:
    """The scan-digest block appended to /list — empty string when there are none
    (so /list's watchlist rendering stays byte-identical for users with no digests)."""
    if not scan_rows:
        return ""
    lines = ["🔁 Scheduled scan digests:"]
    for r in scan_rows:
        lines.append(f"  • top {r['top_n']} on {r['exchange']} @ {r['timeframe']} — every {r['cadence']}")
    lines.append("Remove one with /unscanwatch <TOP_N> <TF> <EXCHANGE>.")
    return "\n".join(lines)


def _with_scan(base: str, scan_section: str) -> str:
    """Append the scan-digest section to a /list reply (no-op when there are none)."""
    return f"{base}\n\n{scan_section}" if scan_section else base


def handle_watch(
    db: Database,
    chat_id: int,
    username: str | None,
    lang_code: str | None,
    args: Sequence[str],
) -> BatchReply:
    """TG-BATCH-WATCHLIST-W1: bulk-capable /watch. Each of COINS / TFS /
    EXCHANGES may be a single token, a comma-list, or ``all``. Returns a
    ``BatchReply`` — if ``.confirm`` is set the async wrapper shows a
    confirmation keyboard (large expansion or `all` coins) instead of
    committing. The cap is GONE; server safety lives in the C2 fetch budget.
    """
    db.upsert_subscriber(chat_id, username, lang_code)

    if len(args) < 2:
        return BatchReply(messages.usage_watch_message())
    if len(args) > 4:
        return BatchReply("Too many arguments. " + messages.usage_watch_message())

    coins_raw = args[0]
    tfs_raw = args[1]
    exch_raw = args[2] if len(args) >= 3 else DEFAULT_EXCHANGE
    type_raw = args[3] if len(args) >= 4 else None

    try:
        alert_type = normalize_alert_type(type_raw)
    except ValidationError as e:
        return BatchReply(f"❌ {e}")

    coins_is_all = batch.is_all(coins_raw)
    universe = asset_universe.get_asset_universe() if coins_is_all else []
    if coins_is_all and not universe:
        return BatchReply(
            "⚠️ Couldn't load the asset list right now. Try specific coins, "
            "e.g. /watch BTC,ETH 1h."
        )

    try:
        coins = batch.parse_coins(coins_raw, universe)
        tfs = batch.parse_timeframes(tfs_raw)
        exchanges = batch.parse_exchanges(exch_raw)
    except ValidationError as e:
        return BatchReply(f"❌ {e}")

    combos = batch.cartesian(coins, tfs, exchanges)
    if not combos:
        return BatchReply(messages.usage_watch_message())

    n_combos, n_coins, n_tfs, n_exch = len(combos), len(coins), len(tfs), len(exchanges)

    if batch.should_confirm(n_combos, coins_raw, _batch_threshold()):
        pending = {
            "kind": "watch",
            "coins_raw": coins_raw,
            "tfs_raw": tfs_raw,
            "exch_raw": exch_raw,
            "alert_type": alert_type,
            "coins_is_all": coins_is_all,
            "n_combos": n_combos,
            "n_coins": n_coins,
            "n_tfs": n_tfs,
            "n_exch": n_exch,
        }
        return BatchReply(
            messages.batch_confirm_message(n_combos, n_coins, n_tfs, n_exch),
            confirm=True,
            pending=pending,
            combos=n_combos,
        )

    return BatchReply(
        _commit_watch_combos(
            db, chat_id, combos, n_coins, n_tfs, n_exch, alert_type,
            skip_preflight=coins_is_all,
        )
    )


def commit_watch_batch(db: Database, chat_id: int, pending: dict | None, choice: str) -> str:
    """Commit a confirmed batch (``choice`` ∈ {"add", "top", "cancel"}). Called
    by the inline-keyboard callback after the confirmation nudge. Re-expands
    from the stored raw spec; ``top`` clamps COINS to the Top-N most-active."""
    if not pending:
        return messages.batch_expired_message()
    if choice == "cancel":
        return messages.batch_cancelled_message()

    try:
        tfs = batch.parse_timeframes(pending["tfs_raw"])
        exchanges = batch.parse_exchanges(pending["exch_raw"])
        if choice == "top":
            coins = asset_universe.get_top_assets(batch.DEFAULT_TOP_N)
            skip_preflight = True  # Top-N coins are universe-sourced → known valid
        else:  # "add"
            coins_is_all = bool(pending.get("coins_is_all"))
            universe = asset_universe.get_asset_universe() if coins_is_all else []
            coins = batch.parse_coins(pending["coins_raw"], universe)
            skip_preflight = coins_is_all
    except ValidationError as e:
        return f"❌ {e}"

    if not coins:
        return messages.batch_expired_message()

    combos = batch.cartesian(coins, tfs, exchanges)
    return _commit_watch_combos(
        db, chat_id, combos, len(coins), len(tfs), len(exchanges),
        pending["alert_type"], skip_preflight=skip_preflight,
    )


def handle_unwatch(
    db: Database,
    chat_id: int,
    username: str | None,
    lang_code: str | None,
    args: Sequence[str],
) -> BatchReply:
    """TG-BATCH-WATCHLIST-W1: bulk-capable /unwatch. Each dimension is either a
    specific token (filter) or ``all`` / omitted (wildcard — no filter). So
    ``/unwatch BTC all`` removes every BTC row; ``/unwatch all 1m`` removes
    every 1m row; ``/unwatch BTC 4h`` removes BTC 4h on any exchange."""
    db.upsert_subscriber(chat_id, username, lang_code)

    if len(args) < 2:
        return BatchReply(messages.usage_unwatch_message())
    if len(args) > 3:
        return BatchReply("Too many arguments. " + messages.usage_unwatch_message())

    try:
        coin = None if batch.is_all(args[0]) else normalize_coin(args[0])
        timeframe = None if batch.is_all(args[1]) else normalize_timeframe(args[1])
        if len(args) >= 3 and not batch.is_all(args[2]):
            exchange: str | None = normalize_exchange(args[2])
        else:
            exchange = None  # omitted or `all` → wildcard
    except ValidationError as e:
        return BatchReply(f"❌ {e}")

    removed = db.remove_watch_batch(
        chat_id, coin=coin, timeframe=timeframe, exchange=exchange
    )
    return BatchReply(messages.batch_unwatch_message(removed))


def handle_unwatchall(
    db: Database, chat_id: int, username: str | None, lang_code: str | None
) -> BatchReply:
    """TG-BATCH-WATCHLIST-W1: clear the entire watchlist — confirm first."""
    db.upsert_subscriber(chat_id, username, lang_code)
    n = db.count_watches(chat_id)
    if n == 0:
        return BatchReply(messages.unwatchall_empty_message())
    return BatchReply(
        messages.unwatchall_confirm_message(n),
        confirm=True,
        pending={"kind": "unwatchall"},
        combos=n,
    )


def commit_unwatchall(db: Database, chat_id: int) -> str:
    """Delete every watchlist row for the user (after /unwatchall confirm)."""
    removed = db.remove_all_watches(chat_id)
    return messages.unwatchall_done_message(removed)


def handle_stats(db: Database, chat_id: int, username: str | None, lang_code: str | None) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    return admin_handle_stats(db, chat_id)


def handle_list(db: Database, chat_id: int, username: str | None, lang_code: str | None) -> str:
    db.upsert_subscriber(chat_id, username, lang_code)
    rows = db.list_watches(chat_id)
    # FEATURE-PARITY-CHANNELS-W1 CH4: /list also shows scheduled scan digests. The
    # watchlist rendering below is BYTE-UNCHANGED; the scan section is appended (and
    # is "" for users with no digests, so existing /list output is identical).
    scan_section = _format_scan_watch_section(db.list_scan_watches(chat_id))
    if not rows:
        return scan_section or messages.list_empty_message()
    # TG-BATCH-WATCHLIST-W1: watchlists are now uncapped, so a per-row dump can
    # be thousands of lines. Above the page threshold (repurposed
    # PER_USER_WATCHLIST_CAP), show a bounded grouped summary (by TF + exchange)
    # and skip per-row coverage nudges (would be N MCP fetches).
    if len(rows) > PER_USER_WATCHLIST_CAP:
        return _with_scan(
            messages.list_summary_message(
                [
                    {
                        "coin": r["coin"],
                        "timeframe": r["timeframe"],
                        "exchange": r["exchange"],
                        "alert_type": r["alert_type"],
                    }
                    for r in rows
                ]
            ),
            scan_section,
        )
    # OPS-TRADE-CALL-CLUSTER-W1 CH4 — append compact per-row nudge so existing
    # subscribers see expected fire-rate for each watched combo. Same coverage
    # source as /watch (signal-performance MCP resource; cached 5min in-process).
    enriched_rows: list[dict[str, str]] = []
    for r in rows:
        nudge = ""
        try:
            est = compute_coverage_estimate(r["coin"], r["timeframe"], r["exchange"])
            nudge = format_nudge_short(est)
        except Exception as exc:  # noqa: BLE001 — bot must never crash on nudge failure
            log.warning("coverage nudge failed for /list row %s: %s", r, exc)
        enriched_rows.append({
            "coin": r["coin"],
            "timeframe": r["timeframe"],
            "exchange": r["exchange"],
            "alert_type": r["alert_type"],
            "nudge": nudge,
        })
    return _with_scan(messages.list_message(enriched_rows, cap=PER_USER_WATCHLIST_CAP), scan_section)


# ── telegram framework adapters ─────────────────────────────────


def _maybe_fire_first_command_event(db: Database, chat_id: int) -> None:
    """ACTIVATION-FUNNEL-AUDIT-W1 (2026-05-28): fire `tg_bot_first_command`
    (funnel stage 11) the FIRST time a subscriber issues any non-/start
    command. Dedup via `subscribers.first_command_fired_at` column: once-set,
    never re-emitted for the same chat_id. Q-C Option α: event lands in
    `/var/log/algovault-bot/alerts.log` as JSON line; snapshot reader greps
    for `"event": "tg_bot_first_command"` within window.

    Fail-open: any DB or log error is swallowed so the command handler still
    reaches reply_text(). The funnel emit is OBSERVATIONAL — it must not
    block user-visible bot behavior.
    """
    try:
        if db.get_first_command_fired_at(chat_id) is not None:
            return  # already fired for this subscriber; dedup
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.set_first_command_fired_at(chat_id, now_iso)
        log_alert_event("tg_bot_first_command", chat_id=chat_id)
    except Exception as e:  # pragma: no cover — fail-open
        logging.warning("tg_bot_first_command emit failed for chat_id=%s: %s", chat_id, e)


def should_send_first_watch_nudge(db: Database, chat_id: int) -> bool:
    """TG-WATCH-ADOPTION-BROADCAST-W1 (R1): a subscriber is eligible for the
    one-time first-watch onboarding nudge iff they have ZERO engagement
    (no watch + no scan) AND the nudge has never been sent to them.

    Pure (no env / no network) so the dedup + segment logic is unit-tested
    directly. The caller separately gates the actual SEND on
    ``adoption.adoption_broadcasts_live()`` (the go-live flag)."""
    if db.get_first_watch_nudge_sent_at(chat_id) is not None:
        return False
    return not db.has_any_engagement(chat_id)


def handle_adoption_watch_tap(
    db: Database, chat_id: int, username: str | None, lang_code: str | None, data: str
) -> str | None:
    """Pure logic for a one-tap watch button (TG-WATCH-ADOPTION-BROADCAST-W1).
    Parses callback_data, creates the watch, emits the source-attributed
    ``watch_created`` event, pins the onboarding dedup flag on conversion, and
    returns the confirmation toast (≤200 chars). None on a malformed payload."""
    parsed = adoption.parse_watch_callback(data)
    if parsed is None:
        return None
    coin, tf, exch, source = parsed
    db.upsert_subscriber(chat_id, username, lang_code)
    created = db.add_watch(chat_id, coin, tf, exch, DEFAULT_ALERT_TYPE)
    adoption.emit_watch_created(chat_id, coin, tf, exch, source, created)
    if source == adoption.SOURCE_ONBOARDING and db.get_first_watch_nudge_sent_at(chat_id) is None:
        db.mark_first_watch_nudge_sent(
            chat_id, datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
    if created:
        return f"✅ Watching {coin} {tf} on {exch} — I'll ping you when the verdict flips."
    return f"👁 You're already watching {coin} {tf} on {exch}."


def handle_adoption_scanwatch_tap(
    db: Database, chat_id: int, username: str | None, lang_code: str | None, data: str
) -> str | None:
    """Pure logic for the one-tap 'set a standing scan' button. Creates a
    default scan_watch, emits ``scan_watch_created``, returns the toast."""
    source = adoption.parse_scanwatch_callback(data)
    if source is None:
        return None
    db.upsert_subscriber(chat_id, username, lang_code)
    created = db.add_scan_watch(
        chat_id,
        adoption.SCANWATCH_DEFAULT_TOP_N,
        adoption.SCANWATCH_DEFAULT_TF,
        adoption.SCANWATCH_DEFAULT_EXCHANGE,
        adoption.SCANWATCH_DEFAULT_CADENCE,
    )
    adoption.emit_scan_watch_created(
        chat_id,
        adoption.SCANWATCH_DEFAULT_TOP_N,
        adoption.SCANWATCH_DEFAULT_TF,
        adoption.SCANWATCH_DEFAULT_EXCHANGE,
        adoption.SCANWATCH_DEFAULT_CADENCE,
        source=source,
        created=created,
    )
    if created:
        return (
            f"✅ Standing scan set — top {adoption.SCANWATCH_DEFAULT_TOP_N} every "
            f"{adoption.SCANWATCH_DEFAULT_CADENCE}. See it in /list."
        )
    return "📡 You already have this standing scan. See /list."


def _user_meta(update: Update) -> tuple[int, str | None, str | None]:
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else 0
    username = user.username if user else None
    lang = user.language_code if user else None
    return chat_id, username, lang


def register_handlers(app: Application, db: Database) -> None:
    async def _start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        # BOT-W2: detect deep-link param `/start auth_<api_key>`.
        # NEVER log args[0] value at INFO — it may carry the api_key.
        args = ctx.args or []
        if args and isinstance(args[0], str) and args[0].startswith(_AUTH_PREFIX):
            api_key = args[0][len(_AUTH_PREFIX):]
            log.info(
                '{"event": "start_auth_param_received", "chat_id": %d, '
                '"has_param": true, "param_kind": "auth", "param_len": %d}',
                chat_id,
                len(api_key),
            )
            reply = handle_link(db, chat_id, username, lang, api_key)
            # Link-confirmation replies are plain text.
            await update.message.reply_text(reply, disable_web_page_preview=True)
        elif args and isinstance(args[0], str) and args[0].startswith(_REF_PREFIX):
            # TG-REFERRAL-W1: ?start=ref_<CODE> — record the referral + grant the
            # referee bonus, then onboard. The engine enforces one-grant-per-tg +
            # self-referral refusal; this path is fail-soft (a blip never blocks /start).
            ref_code = args[0][len(_REF_PREFIX):].upper()
            await _handle_ref_start(update, chat_id, username, lang, ref_code)
        else:
            # TG-START-COPY-TRIM-W1: the welcome carries an <a> "Upgrade" link →
            # send as HTML (body is fully HTML-escaped in messages.WELCOME_MESSAGE).
            reply = handle_start(db, chat_id, username, lang)
            # TG-BUTTON-UX-W1 (C4): append the inline button menu (prose unchanged).
            await update.message.reply_text(
                reply, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                reply_markup=keyboards.main_menu_kb(),
            )
        # TG-WATCH-ADOPTION-BROADCAST-W1 (R1): fire the one-time first-watch
        # nudge for a 0-engagement sub right after /start. Gated by the go-live
        # flag; deduped via first_watch_nudge_sent_at (mark only on success).
        await _maybe_send_first_watch_nudge(update, chat_id)

    async def _maybe_send_first_watch_nudge(update: Update, chat_id: int) -> None:
        if update.message is None:
            return
        if not adoption.adoption_broadcasts_live():
            return
        if not should_send_first_watch_nudge(db, chat_id):
            return
        try:
            await update.message.reply_text(
                adoption.FIRST_WATCH_NUDGE_TEXT,
                reply_markup=adoption.onboarding_keyboard(),
                disable_web_page_preview=True,
            )
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.mark_first_watch_nudge_sent(chat_id, now_iso)
            log_alert_event(
                "first_watch_nudge_sent", chat_id=chat_id, source="onboarding_start"
            )
        except Exception as e:  # noqa: BLE001 — never break /start on a nudge failure
            log.warning("first-watch nudge send failed chat_id=%s: %s", chat_id, e)

    async def _handle_ref_start(
        update: Update, chat_id: int, username: str | None, lang: str | None, ref_code: str
    ) -> None:
        """TG-REFERRAL-W1: a ?start=ref_<CODE> join — attribute via the engine,
        grant the referee bonus (engine-confirmed; one-grant-per-tg + self-ref
        refused server-side), then onboard. Fail-soft: a referral-engine blip never
        blocks onboarding (the standard welcome always sends)."""
        if update.message is None:
            return
        db.upsert_subscriber(chat_id, username, lang)  # ensure the row for the bonus credit
        log.info(
            '{"event": "start_ref_param_received", "chat_id": %d, "ref_code": "%s"}',
            chat_id,
            ref_code[:16],  # a referral code is public (shareable); PII-safe
        )
        res = referral_client.attribute(ref_code, chat_id)
        if res and res.get("recorded") and int(res.get("bonus_calls", 0) or 0) > 0:
            bonus = int(res["bonus_calls"])
            db.grant_referral_bonus(chat_id, bonus)
            code_data = referral_client.get_code(chat_id)  # the referee's own terms (+ their link, C3)
            terms = (code_data or {}).get("terms", {})
            await update.message.reply_text(
                referral.format_ref_join_greeting(bonus, terms, lang),
                disable_web_page_preview=True,
            )
            log_alert_event("tg_referral_join", chat_id=chat_id, lang_code=lang)
            # C3 compounding loop: the referee instantly becomes a referrer — show
            # THEIR own link + Share so this ring seeds the next (the K-factor loop).
            if code_data:
                await _send_referral_card(update.message, code_data, lang)
        # Onboard everyone (granted or not) with the standard welcome.
        welcome = handle_start(db, chat_id, username, lang)
        await update.message.reply_text(
            welcome, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    async def _help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        reply = handle_help(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _send_referral_card(message: Message, code_data: dict, lang: str | None) -> None:
        """Render the /referral body + a one-tap Share url-button. Reused by the
        /referral command and the C3 ref-join compounding prompt (DRY)."""
        body = referral.format_referral_body(code_data, lang)
        share_text = referral.format_share_text(code_data.get("terms", {}), lang)
        share_url = referral.build_share_url(str(code_data.get("deep_link", "")), share_text)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(referral.share_button_label(lang), url=share_url)]]
        )
        await message.reply_text(body, reply_markup=keyboard, disable_web_page_preview=True)

    async def _referral(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """TG-REFERRAL-W1: show the user's referral code, deep link, stats + a
        one-tap Share button (double-sided framing from the engine SoT terms)."""
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        db.upsert_subscriber(chat_id, username, lang)
        code_data = referral_client.get_code(chat_id)
        if code_data is None:
            await update.message.reply_text(
                referral.format_referral_unavailable(lang), disable_web_page_preview=True
            )
            return
        # REFERRAL-PARITY-NOTIFS-W1 / C2: cache code→chat_id so the notification drain
        # can resolve a pending TG row (keyed by code) to this chat locally.
        ref_code = code_data.get("code")
        if isinstance(ref_code, str) and ref_code:
            db.set_referral_code(chat_id, ref_code)
        await _send_referral_card(update.message, code_data, lang)
        log_alert_event("tg_referral_shown", chat_id=chat_id, lang_code=lang)

    async def _menu(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """TG-BUTTON-UX-W1 (C4): re-render the inline button menu on demand."""
        if update.message is None:
            return
        await update.message.reply_text(
            "📋 AlgoVault — tap to act:", reply_markup=keyboards.main_menu_kb()
        )

    async def _on_menu_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Main-menu buttons that map to existing handlers (Watch/Scan are the wizards'
        OWN entry_points; Upgrade is a url button). mnu:regime/call need a coin → the
        handler's no-arg usage reply guides the user to type it."""
        q = update.callback_query
        if q is None or q.data is None or q.from_user is None:
            return
        await q.answer()
        u = q.from_user
        cid, un, lg = u.id, u.username, u.language_code
        _maybe_fire_first_command_event(db, cid)
        action = q.data.split(":", 1)[1]
        if action == "list":
            text = handle_list(db, cid, un, lg)
        elif action == "help":
            text = handle_help(db, cid, un, lg)
        elif action == "funding":
            text = handle_funding(db, cid, un, lg, [])
        elif action == "regime":
            text = handle_regime(db, cid, un, lg, [])
        elif action == "call":
            text = handle_call(db, cid, un, lg, [])
        else:
            return
        if isinstance(q.message, Message):
            await q.message.reply_text(text, disable_web_page_preview=True)

    async def _notifications(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """REFERRAL-PARITY-NOTIFS-W1 / C2: toggle referral join/earnings notifications
        (default-ON). `/notifications off | on`; no arg → usage. Writes the engine's
        notify_opt_out (the single source of truth across TG + email)."""
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        db.upsert_subscriber(chat_id, username, lang)
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg in ("off", "stop", "mute"):
            referral_client.set_notify_pref(chat_id, True)
            await update.message.reply_text(referral.format_notifications_toggle(True, lang))
        elif arg in ("on", "start", "resume"):
            referral_client.set_notify_pref(chat_id, False)
            await update.message.reply_text(referral.format_notifications_toggle(False, lang))
        else:
            await update.message.reply_text(referral.format_notifications_toggle(None, lang))

    async def _scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_scan(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _scanwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_scanwatch(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _unscanwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        args = ctx.args or []
        reply = handle_unscanwatch(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _regime(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_regime(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _call(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_call(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _funding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_funding(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_watch(db, chat_id, username, lang, args)
        if reply.confirm and reply.pending is not None:
            # Stash the pending spec under a short token; the callback re-reads
            # it from ctx.user_data (per-user) and commits the chosen action.
            token = secrets.token_urlsafe(6)
            if ctx.user_data is not None:
                ctx.user_data[token] = reply.pending
            rows = [
                [InlineKeyboardButton(
                    messages.batch_btn_add_all(reply.combos), callback_data=f"bw:add:{token}"
                )]
            ]
            if reply.pending.get("coins_is_all"):
                # Top-N clamp only makes sense when COINS == all (it narrows coins).
                rows.append([InlineKeyboardButton(
                    messages.batch_btn_top_n(batch.DEFAULT_TOP_N), callback_data=f"bw:top:{token}"
                )])
            rows.append([InlineKeyboardButton(
                messages.BATCH_BTN_CANCEL, callback_data=f"bw:cancel:{token}"
            )])
            await update.message.reply_text(
                str(reply), reply_markup=InlineKeyboardMarkup(rows),
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(str(reply), disable_web_page_preview=True)

    async def _unwatchall(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        reply = handle_unwatchall(db, chat_id, username, lang)
        if reply.confirm:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(messages.UNWATCHALL_BTN_YES, callback_data="uwa:yes"),
                InlineKeyboardButton(messages.UNWATCHALL_BTN_CANCEL, callback_data="uwa:cancel"),
            ]])
            await update.message.reply_text(str(reply), reply_markup=kb)
        else:
            await update.message.reply_text(str(reply))

    async def _on_batch_watch_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles the [Add all N] / [Top N most-active] / [Cancel] nudge taps."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        parts = query.data.split(":")
        if len(parts) != 3:
            return
        _, choice, token = parts
        pending = ctx.user_data.pop(token, None) if ctx.user_data is not None else None
        # from_user is always present on a callback query (the tapper).
        chat_id = query.from_user.id if query.from_user else 0
        text = commit_watch_batch(db, chat_id, pending, choice)
        try:
            await query.edit_message_text(text, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001 — message may be uneditable; log + drop
            log.warning("batch-watch callback edit failed chat_id=%s err=%s", chat_id, e)

    async def _on_unwatchall_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles the /unwatchall [Yes, clear all] / [Cancel] taps."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        choice = query.data.split(":")[-1]
        chat_id = query.from_user.id if query.from_user else 0
        text = commit_unwatchall(db, chat_id) if choice == "yes" else messages.batch_cancelled_message()
        try:
            await query.edit_message_text(text)
        except Exception as e:  # noqa: BLE001
            log.warning("unwatchall callback edit failed chat_id=%s err=%s", chat_id, e)

    async def _unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        args = ctx.args or []
        reply = handle_unwatch(db, chat_id, username, lang, args)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _list(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        reply = handle_list(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    async def _stats(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        reply = handle_stats(db, chat_id, username, lang)
        await update.message.reply_text(reply, disable_web_page_preview=True)

    # TG-BROADCAST-STACK-W1 C4 (2026-05-28): /unlock_premium_alerts entry.
    # Two-button inline keyboard offering X-follow OR npm-install paths;
    # callback handlers below dispatch on `unlock:x` / `unlock:npm` payloads.
    async def _unlock_premium_alerts(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        chat_id, username, lang = _user_meta(update)
        _maybe_fire_first_command_event(db, chat_id)
        from .unlock import (
            CB_UNLOCK_NPM,
            CB_UNLOCK_X,
            format_already_verified_body,
            format_button_labels,
            format_intro_body,
            x_follow_unlock_enabled,
        )

        # If active Pro grant exists, short-circuit with already_verified reply.
        grant = db.get_pro_grant(chat_id)
        if grant is not None:
            try:
                expires_at = datetime.fromisoformat(
                    str(grant["expires_at"]).replace("Z", "+00:00")
                )
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at > datetime.now(timezone.utc):
                    body = format_already_verified_body(expires_at, lang)
                    await update.message.reply_text(body, disable_web_page_preview=True)
                    return
            except Exception:
                pass

        # REFERRAL-LIGHT-W1 follow-up: the X-follow screenshot unlock is deprecated
        # (superseded by /referral). By default omit its button + offer npm-install +
        # the referral program; UNLOCK_X_FOLLOW_ENABLED=1 re-adds it (clean rollback).
        xf_enabled = x_follow_unlock_enabled()
        body = format_intro_body(lang, x_follow_enabled=xf_enabled)
        x_label, npm_label = format_button_labels(lang)
        rows = []
        if xf_enabled:
            rows.append([InlineKeyboardButton(x_label, callback_data=CB_UNLOCK_X)])
        rows.append([InlineKeyboardButton(npm_label, callback_data=CB_UNLOCK_NPM)])
        keyboard = InlineKeyboardMarkup(rows)
        await update.message.reply_text(
            body,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        log_alert_event("tg_unlock_attempted", chat_id=chat_id, lang_code=lang)

    async def _on_photo(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """TG-BROADCAST-STACK-W1 CH5: photo handler for X-follow screenshots.

        Triggers only when subscriber is in ``pending_x_screenshot`` state.
        Downloads the highest-resolution photo, saves to disk, updates DB,
        and fires operator-review DM (when TG_ADMIN_CHAT_ID env set).
        """
        if update.message is None or not update.message.photo:
            return
        chat_id, username, lang = _user_meta(update)
        status, method, _, _ = db.get_unlock_state(chat_id)
        from .screenshots import (
            DEFAULT_SCREENSHOTS_DIR,
            compute_screenshot_path,
            format_operator_review_caption,
            is_pending_x_screenshot,
        )

        if not is_pending_x_screenshot(status):
            # Photo arrived from a subscriber NOT in the X-screenshot flow;
            # ignore silently (could be unsolicited; do NOT reply or store).
            return

        # Pick the highest-resolution PhotoSize (last in the list per
        # python-telegram-bot semantics).
        photo = update.message.photo[-1]
        try:
            tg_file = await photo.get_file()
        except Exception as e:  # noqa: BLE001
            log.warning("get_file failed chat_id=%s err=%s", chat_id, e)
            return

        DEFAULT_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = compute_screenshot_path(chat_id)
        try:
            await tg_file.download_to_drive(custom_path=str(path))
        except Exception as e:  # noqa: BLE001
            log.warning("download_to_drive failed chat_id=%s err=%s", chat_id, e)
            return

        db.set_unlock_screenshot_path(chat_id, str(path))
        log_alert_event(
            "tg_unlock_screenshot_uploaded",
            chat_id=chat_id,
            lang_code=lang,
            screenshot_path=str(path),
        )

        # Operator-review DM with [Approve]/[Reject] buttons.
        admin_chat_id_raw = os.environ.get("TG_ADMIN_CHAT_ID", "").strip()
        if admin_chat_id_raw:
            try:
                admin_chat_id = int(admin_chat_id_raw)
                caption = format_operator_review_caption(chat_id, username, lang)
                from .unlock import CB_APPROVE_PREFIX, CB_REJECT_PREFIX
                review_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Approve",
                                callback_data=f"{CB_APPROVE_PREFIX}{chat_id}",
                            ),
                            InlineKeyboardButton(
                                "❌ Reject",
                                callback_data=f"{CB_REJECT_PREFIX}{chat_id}",
                            ),
                        ]
                    ]
                )
                with open(path, "rb") as photo_fp:
                    await update.get_bot().send_photo(
                        chat_id=admin_chat_id,
                        photo=photo_fp,
                        caption=caption,
                        reply_markup=review_keyboard,
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("operator-review DM failed: %s", e)
        else:
            log.info(
                "TG_ADMIN_CHAT_ID unset; screenshot saved at %s but no "
                "operator-review DM sent (manual review required)",
                path,
            )

        # Acknowledge to subscriber that screenshot was received.
        from .unlock import normalize_lang as _nl
        lang_norm = _nl(lang)
        if lang_norm == "id":
            ack = "Screenshot diterima! Saya akan verifikasi dalam 24 jam."
        elif lang_norm == "zh-hans":
            ack = "已收到截图！我将在 24 小时内验证。"
        else:
            ack = "Screenshot received! I'll verify within 24h."
        await update.message.reply_text(ack)

    async def _on_unlock_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handles taps on the [Follow X] / [Install] inline buttons."""
        from .unlock import (
            CB_UNLOCK_NPM,
            CB_UNLOCK_X,
            METHOD_NPM_INSTALL,
            METHOD_X_FOLLOW,
            STATE_PENDING_NPM,
            STATE_PENDING_X,
            format_pending_npm_body,
            format_pending_x_body,
            format_x_follow_retired_body,
            generate_track_token,
            x_follow_unlock_enabled,
        )

        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()  # acknowledge tap (no toast)
        # from_user is the tapper (always present on a callback query); the
        # prior query.message.chat_id fallback tripped mypy on the
        # MaybeInaccessibleMessage union and is unreachable in practice.
        chat_id = query.from_user.id if query.from_user else 0
        sub = db.get_subscriber(chat_id)
        lang = sub["lang_code"] if sub else None

        if query.data == CB_UNLOCK_X:
            # REFERRAL-LIGHT-W1 follow-up: X-follow screenshot unlock DEPRECATED
            # (superseded by /referral). Default OFF → a stale [Follow X] tap gets the
            # retired/redirect reply and does NOT open the screenshot flow.
            if not x_follow_unlock_enabled():
                body = format_x_follow_retired_body(lang)
                if isinstance(query.message, Message):
                    await query.message.reply_text(body, disable_web_page_preview=True)
                log_alert_event("tg_unlock_x_retired", chat_id=chat_id, lang_code=lang)
                return
            db.set_unlock_pending(chat_id, STATE_PENDING_X, METHOD_X_FOLLOW)
            body = format_pending_x_body(lang)
            # isinstance narrows MaybeInaccessibleMessage → Message (has
            # reply_text); skips gracefully if the message is inaccessible.
            if isinstance(query.message, Message):
                await query.message.reply_text(body, disable_web_page_preview=True)
            log_alert_event("tg_unlock_x_chosen", chat_id=chat_id, lang_code=lang)
        elif query.data == CB_UNLOCK_NPM:
            track_token = generate_track_token()
            db.set_unlock_pending(
                chat_id, STATE_PENDING_NPM, METHOD_NPM_INSTALL, track_token=track_token
            )
            body = format_pending_npm_body(track_token, lang)
            if isinstance(query.message, Message):
                # parse_mode=None so the ```snippet``` code-fence renders raw.
                await query.message.reply_text(body, disable_web_page_preview=True)
            log_alert_event(
                "tg_unlock_npm_chosen",
                chat_id=chat_id,
                lang_code=lang,
                track_token_prefix=track_token[:8],  # PII-safe shape
            )

    async def _on_review_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Operator-side [Approve]/[Reject] tap handler. callback_data shape:
        ``unlock_approve:<chat_id>`` or ``unlock_reject:<chat_id>``.
        Only fires for the configured TG_ADMIN_CHAT_ID — silently ignores
        taps from non-admin chats.
        """
        from .unlock import (
            CB_APPROVE_PREFIX,
            CB_REJECT_PREFIX,
            METHOD_X_FOLLOW,
            compute_grant_expiry,
            format_rejected_body,
            format_verified_body,
        )

        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        # Auth gate: only the configured admin chat can approve/reject.
        admin_chat_id_raw = os.environ.get("TG_ADMIN_CHAT_ID", "").strip()
        if not admin_chat_id_raw or not query.from_user:
            return
        try:
            admin_chat_id = int(admin_chat_id_raw)
        except ValueError:
            return
        if query.from_user.id != admin_chat_id:
            return

        # Parse target subscriber chat_id from callback_data.
        data = query.data
        if data.startswith(CB_APPROVE_PREFIX):
            decision = "approve"
            target_str = data[len(CB_APPROVE_PREFIX):]
        elif data.startswith(CB_REJECT_PREFIX):
            decision = "reject"
            target_str = data[len(CB_REJECT_PREFIX):]
        else:
            return
        try:
            target_chat_id = int(target_str)
        except ValueError:
            return

        sub = db.get_subscriber(target_chat_id)
        target_lang = sub["lang_code"] if sub else None

        if decision == "approve":
            now = datetime.now(timezone.utc)
            expires = compute_grant_expiry(now)
            db.set_unlock_verified(target_chat_id, now.isoformat(timespec="seconds"))
            db.insert_or_replace_pro_grant(
                target_chat_id,
                expires.isoformat(timespec="seconds"),
                METHOD_X_FOLLOW,
            )
            body = format_verified_body(METHOD_X_FOLLOW, target_lang)
            try:
                await update.get_bot().send_message(
                    chat_id=target_chat_id, text=body, disable_web_page_preview=True
                )
            except Exception as e:  # noqa: BLE001
                log.warning("approve DM failed target=%s err=%s", target_chat_id, e)
            log_alert_event(
                "tg_unlock_verified",
                chat_id=target_chat_id,
                method=METHOD_X_FOLLOW,
                granted_expires_at=expires.isoformat(timespec="seconds"),
            )
            if isinstance(query.message, Message):
                try:
                    await query.message.reply_text(
                        f"✅ Approved chat_id={target_chat_id} · Pro until {expires.strftime('%Y-%m-%d')}"
                    )
                except Exception:
                    pass
        else:  # reject
            db.reset_unlock_state(target_chat_id)
            body = format_rejected_body(target_lang)
            try:
                await update.get_bot().send_message(
                    chat_id=target_chat_id, text=body, disable_web_page_preview=True
                )
            except Exception as e:  # noqa: BLE001
                log.warning("reject DM failed target=%s err=%s", target_chat_id, e)
            log_alert_event(
                "tg_unlock_failed",
                chat_id=target_chat_id,
                reason="operator_rejected_screenshot",
            )
            if isinstance(query.message, Message):
                try:
                    await query.message.reply_text(
                        f"❌ Rejected chat_id={target_chat_id} · subscriber asked to retry"
                    )
                except Exception:
                    pass

    async def _on_adoption_watch_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A5: one-tap watch button (nudge/digest). Thin shell over the pure
        ``handle_adoption_watch_tap``; answers with the confirmation toast."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        user = query.from_user
        chat_id = user.id if user else 0
        _maybe_fire_first_command_event(db, chat_id)
        toast = handle_adoption_watch_tap(
            db, chat_id,
            user.username if user else None,
            user.language_code if user else None,
            query.data,
        )
        await query.answer(text=toast or "", show_alert=False)

    async def _on_adoption_scanwatch_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A5: one-tap 'set a standing scan' button (scan-showcase). Thin shell
        over the pure ``handle_adoption_scanwatch_tap``."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        user = query.from_user
        chat_id = user.id if user else 0
        _maybe_fire_first_command_event(db, chat_id)
        toast = handle_adoption_scanwatch_tap(
            db, chat_id,
            user.username if user else None,
            user.language_code if user else None,
            query.data,
        )
        await query.answer(text=toast or "", show_alert=False)

    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("referral", _referral))
    # TG-BUTTON-UX-W1 (C4): /menu re-renders the inline menu; the mnu:* router serves the
    # menu buttons mapping to existing handlers (Watch/Scan are the wizards' own entry_points).
    app.add_handler(CommandHandler("menu", _menu))
    app.add_handler(CallbackQueryHandler(_on_menu_callback, pattern=r"^mnu:(regime|call|funding|list|help)$"))
    app.add_handler(CommandHandler("notifications", _notifications))
    # TG-BUTTON-UX-W1 (C2): the Watch wizard's ConversationHandler IS the /watch entry —
    # args → typed _watch (verbatim), no-args → tap wizard; also entered via mnu:watch (C4).
    # Terminal reuses _commit_watch_combos (the typed path's persist) — no fork, normal quota.
    app.add_handler(wizard.build_watch_conversation(
        db,
        typed_watch=_watch,
        commit_watch=lambda chat_id, coin, tf, exch, mode: _commit_watch_combos(
            db, chat_id, [(coin, tf, exch)], 1, 1, 1, mode, skip_preflight=True
        ),
        get_popular_coins=lambda: list(keyboards.WATCH_QUICKPICKS),
        get_universe=asset_universe.get_asset_universe,
    ))
    app.add_handler(CommandHandler("unwatch", _unwatch))
    app.add_handler(CommandHandler("unwatchall", _unwatchall))
    app.add_handler(CommandHandler("list", _list))
    app.add_handler(CommandHandler("stats", _stats))
    # FEATURE-PARITY-CHANNELS-W1 CH3/CH4 + TG-BUTTON-UX-W1 (C3): the Scan wizard's
    # ConversationHandler IS the /scan (one-shot) + /scanwatch (standing) entry — args →
    # typed _scan/_scanwatch verbatim; no-args → tap wizard; also entered via mnu:scan (C4).
    # Reuses handle_scan / handle_scanwatch (no forked scan logic, normal quota).
    app.add_handler(wizard.build_scan_conversation(
        db,
        typed_scan=_scan,
        typed_scanwatch=_scanwatch,
        run_scan=lambda chat_id, username, lang, top_n, tf, exch: handle_scan(
            db, chat_id, username, lang, [str(top_n), tf, exch]
        ),
        commit_scanwatch=lambda chat_id, username, lang, top_n, tf, exch: handle_scanwatch(
            db, chat_id, username, lang, [str(top_n), tf, exch]
        ),
    ))
    app.add_handler(CommandHandler("unscanwatch", _unscanwatch))
    # On-demand per-coin pulls for the bot-flagged get_market_regime /
    # get_trade_call (additive surface; recurring side is /watch …regime|calls).
    app.add_handler(CommandHandler("regime", _regime))
    app.add_handler(CommandHandler("call", _call))
    # BOT-FUNDING-SOT-W1: scan_funding_arb flipped channels.bot=true → /funding.
    app.add_handler(CommandHandler("funding", _funding))
    # TG-BATCH-WATCHLIST-W1 — batch /watch confirm-nudge + /unwatchall confirm.
    app.add_handler(
        CallbackQueryHandler(_on_batch_watch_callback, pattern=r"^bw:(add|top|cancel):")
    )
    app.add_handler(
        CallbackQueryHandler(_on_unwatchall_callback, pattern=r"^uwa:(yes|cancel)$")
    )
    # TG-WATCH-ADOPTION-BROADCAST-W1 (A5): one-tap watch / scan adoption buttons.
    app.add_handler(
        CallbackQueryHandler(_on_adoption_watch_callback, pattern=adoption.WATCH_CB_PATTERN)
    )
    app.add_handler(
        CallbackQueryHandler(_on_adoption_scanwatch_callback, pattern=adoption.SCANWATCH_CB_PATTERN)
    )
    # TG-BROADCAST-STACK-W1 C4 + C5: /unlock_premium_alerts +
    # 2 CallbackQueryHandlers (unlock-path picker + operator approve/reject) +
    # photo MessageHandler for X-follow screenshot upload.
    app.add_handler(CommandHandler("unlock_premium_alerts", _unlock_premium_alerts))
    app.add_handler(
        CallbackQueryHandler(_on_unlock_callback, pattern=r"^unlock:(x|npm)$")
    )
    app.add_handler(
        CallbackQueryHandler(_on_review_callback, pattern=r"^unlock_(approve|reject):\d+$")
    )
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))


# Re-export the constants kept (for now) so existing tests/import paths still work.
__all__ = [
    "DEFAULT_ALERT_TYPE",
    "DEFAULT_EXCHANGE",
    "BatchReply",
    "commit_unwatchall",
    "commit_watch_batch",
    "handle_help",
    "handle_list",
    "handle_scan",
    "handle_scanwatch",
    "handle_start",
    "handle_unscanwatch",
    "handle_unwatch",
    "handle_unwatchall",
    "handle_watch",
    "register_handlers",
]
