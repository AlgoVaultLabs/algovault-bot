"""Alert engine — runs every minute via systemd timer with per-TF lazy dispatch.

Spec C3 line 247-262: the cron fires at HH:MM:00, then queries the DB for
watchlist rows where ``now - last_fetched_at >= TF_SECONDS``. Only those
rows are processed in the current cycle. A 1m-TF row processes every cycle;
a 4h-TF row processes once every 240 cycles. One systemd timer handles all
11 TFs.

Flap suppression for regime alerts: require ``last_verdict_streak >= 2``
consecutive same-regime observations before firing the change-alert.
Trade-call alerts have no flap suppression — every BUY/SELL is high-conviction
by design.

D1-C: every signal-MCP call goes through `McpClient` with the internal-bypass
header — quota is bot-side only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from telegram import Bot
from telegram.error import TelegramError

from .cta import (
    quota_exhausted_message,
    regime_alert_should_show_cta,
    regime_cta_text,
    trade_call_cta_text,
)
from .db import Database, DEFAULT_DB_PATH
from .log_setup import log_alert_event
from .mcp_client import McpClient, McpError
from .messages import signup_url
from .quota import FREE_TIER_MONTHLY_QUOTA, QuotaState, consume_quota, get_quota_state
from .rate_limit import TELEGRAM_GLOBAL_SEMAPHORE
from .validators import TF_SECONDS


log = logging.getLogger("algovault_bot.alert_engine")


# ── alert formatting ───────────────────────────────────────────


@dataclass
class WatchRow:
    chat_id: int
    coin: str
    timeframe: str
    exchange: str
    alert_type: str
    regime_last_seen: str | None
    last_verdict: str | None
    last_verdict_streak: int


def _row_from_sqlite(r: Any) -> WatchRow:
    return WatchRow(
        chat_id=int(r["chat_id"]),
        coin=r["coin"],
        timeframe=r["timeframe"],
        exchange=r["exchange"],
        alert_type=r["alert_type"],
        regime_last_seen=r["regime_last_seen"],
        last_verdict=r["last_verdict"],
        last_verdict_streak=int(r["last_verdict_streak"] or 0),
    )


REGIME_GLYPH = {
    "TRENDING_UP": "🟢",
    "TRENDING_DOWN": "🔴",
    "RANGING": "🟡",
    "VOLATILE": "🟠",
}


def format_regime_alert(
    row: WatchRow,
    prev: str | None,
    current: str,
    confidence: int,
    cta: str | None = None,
) -> str:
    prev_glyph = REGIME_GLYPH.get(prev or "", "")
    cur_glyph = REGIME_GLYPH.get(current, "")
    parts = [
        f"📊 Regime shift: {row.coin} {row.timeframe} on {row.exchange}",
        f"{prev_glyph} {prev or 'UNKNOWN'} → {cur_glyph} {current}",
        f"Confidence: {confidence}",
    ]
    if cta:
        parts.append("")
        parts.append(cta)
    return "\n".join(parts)


def format_trade_call_alert(
    row: WatchRow,
    call: str,
    confidence: int,
    price: float,
    regime: str,
    funding: str,
    reasoning: str | None,
    quota: QuotaState,
    cta: str | None = None,
) -> str:
    glyph = "🟢" if call == "BUY" else "🔴"
    parts = [
        f"{glyph} {call}: {row.coin} {row.timeframe} on {row.exchange}",
        f"Confidence: {confidence}  ·  Price: ${price:,.2f}",
        f"Regime: {regime}  ·  Funding: {funding}",
    ]
    if reasoning:
        parts.append(f"Reasoning: {reasoning[:280]}{'...' if len(reasoning) > 280 else ''}")
    # BOT-W2 C3: paid-tier-linked users see a tier badge instead of "47/100".
    if quota.is_paid and quota.linked_tier:
        parts.append(f"💎 {quota.linked_tier.capitalize()} plan — unlimited via bot")
    else:
        parts.append(f"📊 Quota: {quota.used}/{quota.total} free calls used this month")
    if cta:
        parts.append("")
        parts.append(cta)
    return "\n".join(parts)


def format_quota_exhausted_alert(row: WatchRow, call: str, cta: str) -> str:
    """Sent in place of the trade-call alert when the user hit 100% quota.

    Spec C4 lines 357-361: "bot relays signal-MCP's existing exhausted message
    + adds quota_100 CTA + x402 fallback line". Under D1-C, signal-MCP doesn't
    see this user's quota — the bot owns the gate, and we craft the same
    message shape locally (mirrored from src/lib/license.ts:getQuotaExhaustedMessage).
    """
    glyph = "🟢" if call == "BUY" else "🔴"
    return "\n".join(
        [
            f"{glyph} {call} signal blocked — {row.coin} {row.timeframe} on {row.exchange}",
            quota_exhausted_message(),
            "",
            cta,
        ]
    )


# ── one-shot cron tick ─────────────────────────────────────────


async def _push(bot: Bot, chat_id: int, text: str) -> bool:
    """Send a message under the per-bot Telegram global semaphore."""
    async with TELEGRAM_GLOBAL_SEMAPHORE:
        try:
            await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
            return True
        except TelegramError as e:
            log.warning(
                json.dumps({"event": "telegram_send_failed", "chat_id": chat_id, "err": str(e)})
            )
            return False


def _maybe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _maybe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def process_one_row(
    bot: Bot, mcp: McpClient, db: Database, row: WatchRow
) -> dict[str, Any]:
    """Process a single watchlist row. Returns a structured-log dict for journal."""
    fetched: dict[str, str] = {"regime": "skip", "trade_call": "skip"}
    new_verdict = row.last_verdict or ""
    new_streak = row.last_verdict_streak
    regime_seen: str | None = row.regime_last_seen

    if row.alert_type in ("regime", "both"):
        try:
            # get_market_regime supports {1h, 4h, 1d}; for shorter TFs we coarse-grain to 1h.
            regime_tf = row.timeframe if row.timeframe in ("1h", "4h", "1d") else "1h"
            regime_result = mcp.call_tool(
                "get_market_regime",
                {"coin": row.coin, "timeframe": regime_tf, "exchange": row.exchange},
            )
            current_regime = regime_result.get("regime", "")
            confidence = _maybe_int(regime_result.get("confidence"))
            fetched["regime"] = "ok"

            if current_regime:
                # Streak counter: increment if same as last_verdict, else reset.
                if current_regime == row.last_verdict:
                    new_streak = row.last_verdict_streak + 1
                else:
                    new_streak = 1
                new_verdict = current_regime

                # Flap suppression: only fire when streak >= 2 AND it differs from last_seen.
                if new_streak >= 2 and current_regime != row.regime_last_seen:
                    # C4 frequency-driven soft CTA on alerts #1, 3, 7, 15, then every 10.
                    # No 24h cap — Telegram doesn't impose one, neither do we.
                    next_count = db.increment_total_regime_alerts(row.chat_id)
                    cta = regime_cta_text() if regime_alert_should_show_cta(next_count) else None
                    text = format_regime_alert(
                        row, row.regime_last_seen, current_regime, confidence, cta=cta,
                    )
                    if await _push(bot, row.chat_id, text):
                        regime_seen = current_regime
                        fetched["regime"] = "fired"
                        if cta:
                            db.increment_total_ctas_shown(row.chat_id)
                        log_alert_event(
                            "regime_alert_fired",
                            chat_id=row.chat_id,
                            coin=row.coin,
                            timeframe=row.timeframe,
                            exchange=row.exchange,
                            from_regime=row.regime_last_seen,
                            to_regime=current_regime,
                            confidence=confidence,
                            total_regime_alerts=next_count,
                            cta_shown=bool(cta),
                        )
        except McpError as e:
            log.warning(
                json.dumps({"event": "mcp_get_market_regime_failed", "err": str(e)[:200]})
            )

    if row.alert_type in ("calls", "both"):
        try:
            tc_result = mcp.call_tool(
                "get_trade_call",
                {
                    "coin": row.coin,
                    "timeframe": row.timeframe,
                    "exchange": row.exchange,
                    "includeReasoning": True,
                },
            )
            call = (tc_result.get("call") or "").upper()
            fetched["trade_call"] = "ok"

            if call in ("BUY", "SELL"):
                # No 24h cap — only the 100/mo quota gate applies.
                state = get_quota_state(db, row.chat_id)
                if state.exhausted:
                    # C4: send the exhausted-quota notice + quota_100 CTA + x402 fallback.
                    cta = trade_call_cta_text(state)
                    text = format_quota_exhausted_alert(row, call, cta)
                    if await _push(bot, row.chat_id, text):
                        fetched["trade_call"] = "exhausted_alert_sent"
                        db.increment_total_ctas_shown(row.chat_id)
                else:
                    cta = trade_call_cta_text(state)
                    text = format_trade_call_alert(
                        row,
                        call,
                        _maybe_int(tc_result.get("confidence")),
                        _maybe_float(tc_result.get("price_at_signal") or tc_result.get("price")),
                        tc_result.get("regime", "?"),
                        tc_result.get("funding_state") or tc_result.get("funding", "?"),
                        tc_result.get("reasoning"),
                        state,
                        cta=cta or None,
                    )
                    if await _push(bot, row.chat_id, text):
                        consume_quota(db, row.chat_id)
                        db.increment_total_call_alerts(row.chat_id)
                        if cta:
                            db.increment_total_ctas_shown(row.chat_id)
                        fetched["trade_call"] = "fired"
                        log_alert_event(
                            "trade_call_alert_fired",
                            chat_id=row.chat_id,
                            coin=row.coin,
                            timeframe=row.timeframe,
                            exchange=row.exchange,
                            call=call,
                            quota_used=state.used + 1,
                            quota_total=state.total,
                            cta_shown=bool(cta),
                        )
            # HOLD verdicts are silently absorbed — no message, no quota tick (per spec).
        except McpError as e:
            log.warning(
                json.dumps({"event": "mcp_get_trade_call_failed", "err": str(e)[:200]})
            )

    db.update_watch_after_fetch(
        row.chat_id, row.coin, row.timeframe, row.exchange,
        new_verdict, new_streak, regime_seen,
    )
    return fetched


async def run_cycle(token: str, db_path: str, mcp_url: str | None, bypass_key: str) -> dict[str, int]:
    """One cron-fire cycle. Returns counts for journal."""
    db = Database(db_path)
    bot = Bot(token=token)

    now_epoch = int(time.time())
    due_rows_raw = db.list_due_watches(now_epoch, TF_SECONDS)
    due_rows = [_row_from_sqlite(r) for r in due_rows_raw]

    counts = {
        "due": len(due_rows),
        "regime_fired": 0,
        "calls_fired": 0,
        "errors": 0,
    }
    if not due_rows:
        return counts

    from .mcp_client import McpClient, McpClientConfig

    cfg = McpClientConfig(
        url=mcp_url or "http://127.0.0.1:3000/mcp",
        internal_bypass_key=bypass_key,
    )
    try:
        with McpClient(cfg) as mcp:
            for row in due_rows:
                try:
                    fetched = await process_one_row(bot, mcp, db, row)
                    if fetched.get("regime") == "fired":
                        counts["regime_fired"] += 1
                    if fetched.get("trade_call") == "fired":
                        counts["calls_fired"] += 1
                except Exception as e:  # noqa: BLE001
                    counts["errors"] += 1
                    log.exception(
                        "row processing failed: %s/%s/%s — %s",
                        row.coin, row.timeframe, row.exchange, e,
                    )
    except McpError as e:
        log.error("mcp client init failed: %s", e)
        counts["errors"] += 1
    return counts


def main() -> None:
    """Cron entry point — invoked by systemd timer every 1 min."""
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # CRITICAL: silence httpx + httpcore — their INFO logs print the full URL
    # which for Telegram embeds the bearer token. Same leak shape as digest.py.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.environ.get("PUBLIC_BOT_TOKEN", "").strip()
    if not token:
        sys.stderr.write("FATAL: PUBLIC_BOT_TOKEN not set\n")
        sys.exit(2)

    bypass_key = os.environ.get("ALGOVAULT_INTERNAL_BYPASS_KEY", "").strip()
    if not bypass_key or bypass_key == "__C3_PLACEHOLDER__":
        sys.stderr.write("FATAL: ALGOVAULT_INTERNAL_BYPASS_KEY not set\n")
        sys.exit(2)

    db_path = os.environ.get("ALGOVAULT_BOT_DB_PATH", DEFAULT_DB_PATH)
    mcp_url = os.environ.get("ALGOVAULT_MCP_URL", "http://127.0.0.1:3000/mcp")

    started = time.time()
    counts = asyncio.run(run_cycle(token, db_path, mcp_url, bypass_key))
    elapsed = time.time() - started
    log.info(
        json.dumps(
            {
                "event": "alert_engine: complete",
                "due": counts["due"],
                "regime_fired": counts["regime_fired"],
                "calls_fired": counts["calls_fired"],
                "errors": counts["errors"],
                "elapsed_s": round(elapsed, 2),
            }
        )
    )
    # Mirror the cycle summary into the on-disk alerts.log so logrotate-archived
    # events survive journald vacuum cycles. This is what the C5 gate `jq .`s.
    log_alert_event(
        "cycle_complete",
        due=counts["due"],
        regime_fired=counts["regime_fired"],
        calls_fired=counts["calls_fired"],
        errors=counts["errors"],
        elapsed_s=round(elapsed, 2),
    )


if __name__ == "__main__":
    main()
