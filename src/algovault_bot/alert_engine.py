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
from telegram.error import Forbidden, TelegramError

from datetime import datetime, timezone

from . import fetch_budget
from .alert_image import SeeAlsoCell, TradeCallView, render_trade_call_card
from .caption import compose_caption, format_verdict_caption_line
from .cta import (
    quota_exhausted_message,
    quota_threshold,
    regime_alert_should_show_cta,
    regime_cta_text,
    trade_call_cta_text,
)
from .db import Database, DEFAULT_DB_PATH
from .log_setup import log_alert_event
from .mcp_client import McpClient, McpError
from .paywall import (
    extract_tier_warning,
    format_paywall_body,
    mark_fired as paywall_mark_fired,
    should_fire_paywall_dm,
)
from .quota import QuotaState, consume_quota, get_quota_state
from .rate_limit import TELEGRAM_GLOBAL_SEMAPHORE
from .validators import TF_SECONDS


# BOT-ALERT-IMAGE-W1 — primary call confidence below this triggers the
# "See Also" surface (only when a same-TF + same-exchange + ≥80%-conf cell
# exists in the upstream `also_see` payload). Matches the renderer's
# "low" / "very low" confidence labels.
LOW_CONFIDENCE_THRESHOLD: int = 50

# Confidence floor for a See Also cell to be shown. Operator-set 2026-05-08.
SEE_ALSO_MIN_CONFIDENCE: int = 80


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


def _handle_forbidden(db: Database | None, chat_id: int, err: BaseException) -> None:
    """BOT-ZOMBIE-W1: when bot.send_* raises Forbidden (almost always
    "bot was blocked by the user"), mark the subscriber so the digest/stats
    counts exclude them. Falls through silently if db is None (some test
    paths or one-shot scripts don't have DB access)."""
    if db is None:
        return
    try:
        db.mark_subscriber_blocked(chat_id, datetime.now(timezone.utc).isoformat())
        log.info(json.dumps({
            "event": "subscriber_marked_blocked",
            "chat_id": chat_id,
            "err": str(err)[:200],
        }))
    except Exception as e:  # noqa: BLE001 — mark is best-effort
        log.warning(json.dumps({
            "event": "mark_subscriber_blocked_failed",
            "chat_id": chat_id,
            "err": str(e)[:200],
        }))


async def _push(bot: Bot, chat_id: int, text: str, db: Database | None = None) -> bool:
    """Send a message under the per-bot Telegram global semaphore."""
    async with TELEGRAM_GLOBAL_SEMAPHORE:
        try:
            await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
            return True
        except Forbidden as e:
            _handle_forbidden(db, chat_id, e)
            return False
        except TelegramError as e:
            log.warning(
                json.dumps({"event": "telegram_send_failed", "chat_id": chat_id, "err": str(e)})
            )
            return False


async def _push_photo(
    bot: Bot,
    chat_id: int,
    photo_bytes: bytes,
    caption: str | None = None,
    db: Database | None = None,
) -> bool:
    """Send a photo (PNG bytes) under the global semaphore. Used for the
    image-format trade-call alerts. Caption holds optional CTA text (URLs are
    clickable in Telegram captions).
    """
    async with TELEGRAM_GLOBAL_SEMAPHORE:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo_bytes,
                caption=caption,
            )
            return True
        except Forbidden as e:
            _handle_forbidden(db, chat_id, e)
            return False
        except TelegramError as e:
            log.warning(
                json.dumps({"event": "telegram_send_photo_failed", "chat_id": chat_id, "err": str(e)})
            )
            return False


def _pick_see_also(
    also_see: list[dict[str, Any]] | None,
    *,
    primary_confidence: int,
    same_tf: str,
    same_exchange: str,
) -> SeeAlsoCell | None:
    """Apply the BOT-ALERT-IMAGE-W1 See Also filter:
    - primary call must be low confidence (<50%)
    - candidate must match same TF AND same exchange
    - candidate must have confidence ≥ 80%
    - return the highest-confidence match, or None.

    The upstream cell SHOULD carry `exchange` post-2026-05-08 (signal-MCP
    v1.10.8). For deploy-ordering safety, when `exchange` is missing on the
    cell we fall back to assuming same-as-alert (Path B) so the wave still
    surfaces something useful before signal-MCP rolls out.
    """
    if primary_confidence >= LOW_CONFIDENCE_THRESHOLD:
        return None
    if not also_see:
        return None
    candidates: list[SeeAlsoCell] = []
    for cell in also_see:
        if cell.get("timeframe") != same_tf:
            continue
        cell_exchange = cell.get("exchange") or same_exchange  # Path B fallback
        if cell_exchange != same_exchange:
            continue
        cell_conf = cell.get("confidence", 0)
        try:
            conf_int = int(cell_conf)
        except (TypeError, ValueError):
            continue
        if conf_int < SEE_ALSO_MIN_CONFIDENCE:
            continue
        coin = cell.get("coin")
        if not coin:
            continue
        candidates.append(SeeAlsoCell(
            coin=coin,
            timeframe=same_tf,
            confidence=conf_int,
            exchange=cell_exchange,
        ))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.confidence)


def _build_trade_call_view(
    row: WatchRow,
    tc_result: dict[str, Any],
    state: QuotaState,
    see_also: SeeAlsoCell | None,
) -> TradeCallView:
    """Pull the full metric set (call + indicators) out of the upstream
    response and shape it for ``render_trade_call_card``. Indicator fields
    that the upstream omits stay as None — the renderer drops those rows
    rather than showing ``?`` or ``N/A``.
    """
    indicators = tc_result.get("indicators") or {}
    tier_label: str | None = None
    quota_used: int | None = None
    quota_total: int | None = None
    if state.is_paid and state.linked_tier:
        tier_label = state.linked_tier.capitalize()
    else:
        quota_used = state.used
        quota_total = state.total
    return TradeCallView(
        coin=row.coin,
        timeframe=row.timeframe,
        exchange=row.exchange,
        call=(tc_result.get("call") or "").upper(),
        confidence=_maybe_int(tc_result.get("confidence")),
        price=_maybe_float(tc_result.get("price_at_signal") or tc_result.get("price")),
        regime=tc_result.get("regime"),
        funding_rate=_maybe_optional_float(indicators.get("funding_rate")),
        funding_24h_avg=_maybe_optional_float(indicators.get("funding_24h_avg")),
        funding_state=indicators.get("funding_state"),
        oi_change_pct=_maybe_optional_float(indicators.get("oi_change_pct")),
        volume_24h=_maybe_optional_float(indicators.get("volume_24h")),
        trend_persistence=indicators.get("trend_persistence"),
        breakout_pending=indicators.get("breakout_pending"),
        reasoning=tc_result.get("reasoning"),
        see_also=see_also,
        tier_label=tier_label,
        quota_used=quota_used,
        quota_total=quota_total,
    )


def _maybe_optional_float(v: Any) -> float | None:
    """Like ``_maybe_float`` but returns None on missing/invalid (so the
    image renderer can omit the corresponding row rather than show 0.0)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
                    if await _push(bot, row.chat_id, text, db=db):
                        regime_seen = current_regime
                        fetched["regime"] = "fired"
                        # QUOTA-CONSISTENCY-COUNT-ALL-W1 (2026-06-08): regime
                        # alerts now count toward the shared 100/mo free quota —
                        # parity with signal-MCP, which meters get_market_regime.
                        # Only HOLD *trade calls* stay free. Paid-linked users are
                        # a no-op inside consume_quota (PAID_TIERS bypass).
                        consume_quota(db, row.chat_id)
                        # BOT-DIGEST-LAST24H-W1: per-alert log for rolling-24h
                        # digest count. Recorded ONLY after _push returned True,
                        # so failed-send rows don't inflate the 24h count.
                        db.record_alert_fired(row.chat_id, "regime")
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

            # TG-BROADCAST-STACK-W1 CH3 (2026-05-28): paywall-at-quota hook.
            # Inspect MCP `_algovault.tier_warning` for 75% / 90% / 100% quota
            # crossings on subscriber's linked_api_key; fire one-time DM per
            # level per calendar month (UTC). Idempotent via paywall.mark_fired.
            # Fail-open: any error swallowed; alert flow continues unaffected.
            try:
                paywall_warning = extract_tier_warning(tc_result)
                if paywall_warning is not None:
                    fire, level = should_fire_paywall_dm(
                        db.path, row.chat_id, paywall_warning
                    )
                    if fire and level:
                        sub = db.get_subscriber(row.chat_id)
                        lang_code = sub["lang_code"] if sub else None
                        paywall_body = format_paywall_body(
                            level,
                            paywall_warning.get("current_usage"),
                            paywall_warning.get("monthly_limit"),
                            paywall_warning.get("suggested_upgrade_url"),
                            lang_code,
                        )
                        if await _push(bot, row.chat_id, paywall_body, db=db):
                            paywall_mark_fired(db.path, row.chat_id, level)
                            log_alert_event(
                                "tg_paywall_dm_fired",
                                chat_id=row.chat_id,
                                level=level,
                                lang_code=lang_code,
                                current_usage=paywall_warning.get("current_usage"),
                                monthly_limit=paywall_warning.get("monthly_limit"),
                            )
            except Exception as paywall_err:  # noqa: BLE001
                log.warning(
                    "paywall hook error chat_id=%s err=%s",
                    row.chat_id, paywall_err,
                )

            if call in ("BUY", "SELL"):
                # No 24h cap — only the 100/mo quota gate applies.
                state = get_quota_state(db, row.chat_id)
                now = datetime.now(timezone.utc)
                if state.exhausted:
                    # C4: send the exhausted-quota notice + quota_100 CTA + x402 fallback.
                    cta = trade_call_cta_text(state, now=now)
                    text = format_quota_exhausted_alert(row, call, cta)
                    if await _push(bot, row.chat_id, text, db=db):
                        fetched["trade_call"] = "exhausted_alert_sent"
                        db.increment_total_ctas_shown(row.chat_id)
                        # ACTIVATION-FUNNEL-AUDIT-W1 (2026-05-28): funnel stage 13.
                        # Q-C Option α: emit to alerts.log JSON-line stream; the
                        # snapshot reader greps for "event": "tg_bot_quota_hit"
                        # within the window. Fire ONLY when _push returned True
                        # (Telegram message actually delivered) — no event for
                        # blocked-subscriber or rate-limit-suppressed cases.
                        log_alert_event(
                            "tg_bot_quota_hit",
                            chat_id=row.chat_id,
                            coin=row.coin,
                            timeframe=row.timeframe,
                            exchange=row.exchange,
                            call=call,
                            quota_used=state.used,
                            quota_total=state.total,
                        )
                else:
                    cta = trade_call_cta_text(state, now=now)
                    threshold = quota_threshold(state)
                    primary_conf = _maybe_int(tc_result.get("confidence"))
                    see_also = _pick_see_also(
                        tc_result.get("also_see"),
                        primary_confidence=primary_conf,
                        same_tf=row.timeframe,
                        same_exchange=row.exchange,
                    )
                    view = _build_trade_call_view(row, tc_result, state, see_also)
                    photo_bytes = render_trade_call_card(view)
                    # TG-ALERT-VERDICT-CAPTION-W1: line 1 is a one-line
                    # glanceable verdict (e.g. "LTC 15min Buy 76% Binance") so
                    # the call surfaces in the lock-screen / banner notification
                    # (a photo-only message previews as just "📷 Photo"). Any
                    # quota-CTA text (URLs clickable) is preserved verbatim
                    # below it; image carries the metric card itself.
                    verdict_line = format_verdict_caption_line(
                        row.coin, row.timeframe, call, primary_conf, row.exchange
                    )
                    caption = compose_caption(verdict_line, cta or None)
                    if await _push_photo(bot, row.chat_id, photo_bytes, caption, db=db):
                        consume_quota(db, row.chat_id)
                        db.increment_total_call_alerts(row.chat_id)
                        # BOT-DIGEST-LAST24H-W1: per-alert log for rolling-24h
                        # digest count. Recorded ONLY after _push_photo
                        # returned True; quota-exhausted notices (handled above)
                        # are operator UX nudges and are NOT recorded.
                        db.record_alert_fired(row.chat_id, "call")
                        if cta:
                            db.increment_total_ctas_shown(row.chat_id)
                            # 24h-per-threshold throttle for the soft/urgent
                            # nudges; '100' is no-op (not throttled).
                            if threshold in ("75", "90"):
                                db.mark_quota_cta_fired(
                                    row.chat_id, threshold, now.isoformat()
                                )
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
                            see_also_shown=see_also is not None,
                            primary_confidence=primary_conf,
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


# TG-BATCH-WATCHLIST-W1 C2 — persistent sustained-deferred state (the cron is a
# oneshot, so cross-tick state lives on disk). Resets on every clean tick.
_SATURATION_STATE_PATH = os.environ.get(
    "FETCH_SATURATION_STATE_PATH", "/var/lib/algovault-bot/fetch_budget_saturation.json"
)


def _percentiles_ms(latencies: list[float]) -> tuple[int, int]:
    """p50 / p95 in milliseconds from a list of per-row seconds (0,0 if empty)."""
    if not latencies:
        return (0, 0)
    s = sorted(latencies)
    p50 = s[len(s) // 2]
    p95 = s[min(len(s) - 1, int(len(s) * 0.95))]
    return (int(p50 * 1000), int(p95 * 1000))


def _emit_saturation_alert(deferred: int, threshold: int) -> None:
    """Sustained-deferred operator-action signal. Severity-gate + 24h cooldown +
    OPS-<CLASS>-W{NEXT} template resolution all live in send_telegram.sh — we
    delegate (never re-implement those gates) and fail open."""
    wrapper = "/opt/algovault-monitoring/send_telegram.sh"
    if not os.path.exists(wrapper):
        log.warning("saturation: wrapper missing at %s (deferred=%s)", wrapper, deferred)
        return
    body = (
        f"AlgoVault bot fetch budget saturated — {deferred} watch rows deferred for "
        f"{threshold}+ consecutive ticks. Real demand exceeds FETCH_BUDGET_PER_MIN; "
        f"raise the budget or ship shared-fetch dedup.\n"
        f"Recommended wave: OPS-TG-FETCH-BUDGET-TUNE-W{{NEXT}}"
    )
    try:
        import subprocess

        subprocess.run(
            [wrapper, "tg_fetch_budget_saturation", "CRITICAL_PERSISTENT", "-"],
            input=body,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as e:  # noqa: BLE001 — alerting must never break the cycle
        log.warning("saturation alert send failed: %s", e)


def _record_saturation(deferred: int) -> None:
    """Update the persistent consecutive-deferred counter; emit the operator
    signal when it crosses the threshold. Fail-open (observability only)."""
    try:
        threshold = fetch_budget.saturation_ticks()
        state: dict = {}
        if os.path.exists(_SATURATION_STATE_PATH):
            try:
                with open(_SATURATION_STATE_PATH) as f:
                    state = json.load(f) or {}
            except (json.JSONDecodeError, OSError):
                state = {}
        new_state, should_alert = fetch_budget.update_saturation_state(
            state, deferred, threshold
        )
        try:
            with open(_SATURATION_STATE_PATH, "w") as f:
                json.dump(new_state, f)
        except OSError as e:
            log.warning("saturation state write failed: %s", e)
        if should_alert:
            _emit_saturation_alert(deferred, threshold)
    except Exception as e:  # noqa: BLE001 — never break the cycle on observability
        log.warning("saturation bookkeeping failed: %s", e)


async def run_cycle(token: str, db_path: str, mcp_url: str | None, bypass_key: str) -> dict[str, int]:
    """One cron-fire cycle. Routes due rows through the C2 fetch budget
    (skip-exhausted → fair-share round-robin → TF-priority → deadline guard) so
    server load is bounded by ``FETCH_BUDGET_PER_MIN`` for ANY watchlist size.
    Returns counts for the journal."""
    db = Database(db_path)
    bot = Bot(token=token)

    now_epoch = int(time.time())
    due_rows_raw = db.list_due_watches(now_epoch, TF_SECONDS)
    due_rows = [_row_from_sqlite(r) for r in due_rows_raw]

    counts: dict[str, int] = {
        "due": len(due_rows),
        "regime_fired": 0,
        "calls_fired": 0,
        "errors": 0,
        "processed": 0,
        "deferred": 0,
        "skipped_exhausted": 0,
        "active_users": 0,
        "budget": fetch_budget.fetch_budget_per_min(),
        "fetch_p50_ms": 0,
        "fetch_p95_ms": 0,
    }
    if not due_rows:
        _record_saturation(0)  # clean tick resets the sustained-deferred counter
        return counts

    # C2: skip-exhausted (compute once per distinct `calls` owner) + budget +
    # fair-share + TF-priority. Deferred rows are simply not marked fetched →
    # they stay due and are picked up next tick.
    budget = counts["budget"]
    calls_owners = {r.chat_id for r in due_rows if r.alert_type == "calls"}
    exhausted = {cid for cid in calls_owners if get_quota_state(db, cid).exhausted}
    sched = fetch_budget.schedule(
        due_rows, budget=budget, is_exhausted=lambda cid: cid in exhausted
    )
    counts["skipped_exhausted"] = sched.stats["skipped_exhausted"]
    counts["active_users"] = sched.stats["active_users"]

    from .mcp_client import McpClient, McpClientConfig

    cfg = McpClientConfig(
        url=mcp_url or "http://127.0.0.1:3000/mcp",
        internal_bypass_key=bypass_key,
    )
    deadline = fetch_budget.tick_deadline_sec()
    tick_start = time.monotonic()
    latencies: list[float] = []
    deadline_deferred = 0
    try:
        with McpClient(cfg) as mcp:
            for i, row in enumerate(sched.scheduled):
                # Wall-clock guard: a latency spike must never overrun the 60s
                # tick — defer the rest (they remain due, unmarked).
                if time.monotonic() - tick_start > deadline:
                    deadline_deferred = len(sched.scheduled) - i
                    log.warning(
                        json.dumps({
                            "event": "fetch_tick_deadline_hit",
                            "deadline_s": deadline,
                            "deadline_deferred": deadline_deferred,
                        })
                    )
                    break
                row_start = time.monotonic()
                try:
                    fetched = await process_one_row(bot, mcp, db, row)
                    counts["processed"] += 1
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
                finally:
                    latencies.append(time.monotonic() - row_start)
    except McpError as e:
        log.error("mcp client init failed: %s", e)
        counts["errors"] += 1

    total_deferred = sched.stats["deferred"] + deadline_deferred
    counts["deferred"] = total_deferred
    counts["fetch_p50_ms"], counts["fetch_p95_ms"] = _percentiles_ms(latencies)
    _record_saturation(total_deferred)
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
                # TG-BATCH-WATCHLIST-W1 C2 — fetch-budget observability.
                "processed": counts["processed"],
                "deferred": counts["deferred"],
                "skipped_exhausted": counts["skipped_exhausted"],
                "active_users": counts["active_users"],
                "budget": counts["budget"],
                "fetch_p50_ms": counts["fetch_p50_ms"],
                "fetch_p95_ms": counts["fetch_p95_ms"],
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
        processed=counts["processed"],
        deferred=counts["deferred"],
        skipped_exhausted=counts["skipped_exhausted"],
        active_users=counts["active_users"],
        budget=counts["budget"],
        fetch_p50_ms=counts["fetch_p50_ms"],
        fetch_p95_ms=counts["fetch_p95_ms"],
        regime_fired=counts["regime_fired"],
        calls_fired=counts["calls_fired"],
        errors=counts["errors"],
        elapsed_s=round(elapsed, 2),
    )


if __name__ == "__main__":
    main()
