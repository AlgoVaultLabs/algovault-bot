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
from typing import Any, Awaitable, Callable

from telegram import Bot
from telegram.error import Forbidden, TelegramError

from datetime import datetime, timezone

from . import fetch_budget
from .alert_image import SeeAlsoCell, TradeCallView, render_trade_call_card
from .capabilities import rank_label  # SCAN-RANKBY-W1: shared lens display label
from .caption import compose_caption, format_verdict_caption_line
from .cta import (
    quota_threshold,
    regime_alert_should_show_cta,
    regime_cta_text,
    referral_nudge_text,
    trade_call_cta_text,
)
from .db import Database, DEFAULT_DB_PATH
from .log_setup import log_alert_event
from .mcp_client import McpClient, McpError
from .quota import (
    QuotaState,
    evaluate_delivery,
    record_call_delivered,
    record_regime_delivered,
    refuse_and_notify,
)
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
        parts.append(f"📊 Quota: {quota.used}/{quota.total} free alerts used")
    if cta:
        parts.append("")
        parts.append(cta)
    return "\n".join(parts)


def _sender(bot: Bot, chat_id: int, db: Database) -> Callable[[str], Awaitable[bool]]:
    """Bind a push target for `quota.refuse_and_notify`.

    A named factory rather than a `lambda text, _cid=cid: ...`: the default-arg
    capture is a late-binding footgun when the call site sits in a loop, and mypy
    cannot infer the lambda's type at all.

    BOT-QUOTA-REFUSAL-SEAM-W1 also RETIRED `format_quota_exhausted_alert` from this
    spot. It rendered the walled-user message for the one lane that had one, which is
    precisely how three lanes ended up with three behaviours. The body now comes from
    `quota.build_refusal_text` — one message, every push lane.
    """

    async def send(text: str) -> bool:
        return await _push(bot, chat_id, text, db=db)

    return send


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
                    # BOT-QUOTA-REFUSAL-SEAM-W1 (R-1a): the regime lane CHARGES quota
                    # (QUOTA-CONSISTENCY-COUNT-ALL-W1) but never enforced it — `run_cycle`
                    # builds its pre-skip set from `alert_type == "calls"` only, and this
                    # branch read the decision nowhere. Measured: one walled free user took
                    # 11 regime pushes AFTER crossing the wall, each charging quota and
                    # driving the counter to 110/100. Charge-without-enforce was the only
                    # incoherent option of the three; ruled to enforce.
                    regime_decision = evaluate_delivery(db, row.chat_id)
                    if not regime_decision.allowed:
                        # Refuse, but do NOT return — `regime_seen` / streak
                        # bookkeeping below this block is flap-suppression state and
                        # must persist whether or not the alert was delivered.
                        await refuse_and_notify(
                            db,
                            row.chat_id,
                            "watch",
                            send=_sender(bot, row.chat_id, db),
                            decision=regime_decision,
                        )
                        fetched["regime"] = "refused_quota"
                    else:
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
                            # BOT-DIGEST-COUNT-ALL-CALLS-W1: ONE delivery seam —
                            # alerts_fired INSERT + consume_quota together (recorded
                            # only after _push returned True, so failed sends don't
                            # inflate the 24h count). source='watch'.
                            record_regime_delivered(db, row.chat_id, "watch")
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

            # BOT-QUOTA-REFUSAL-SEAM-W1 (2026-08-16): the TG-BROADCAST-STACK-W1 CH3
            # paywall-at-quota hook stood here and was DARK for ~80 days — 0 of 57
            # subscribers ever stamped, 0 `tg_paywall_dm_fired` events. It keyed on
            # the MCP `_algovault.tier_warning`, but the bot authenticates with
            # `X-AlgoVault-Internal-Key` → `tier:'internal'`, and signal-MCP's
            # `withTierWarning` returns the meta unchanged for bot-internal callers.
            # The field could never arrive. Its copy was not wasted: `paywall.py`'s
            # `format_paywall_body` is now the walled-user body for EVERY push lane,
            # fed from the meter this bot actually enforces (see `quota.build_refusal_text`).

            if call in ("BUY", "SELL"):
                # No 24h cap — only the 100/mo quota gate applies.
                decision = evaluate_delivery(db, row.chat_id)
                state = decision.state
                now = datetime.now(timezone.utc)
                if not decision.allowed:
                    # ONE refusal seam. It owns whether this exhaustion episode has
                    # already been announced, sends the shared walled body if not, and
                    # stamps only on a delivered send. Pre-seam, this branch re-derived
                    # the decision AND was unreachable for an already-walled user,
                    # because `run_cycle`'s pre-skip dropped the row before it ran.
                    notified = await refuse_and_notify(
                        db,
                        row.chat_id,
                        "watch",
                        send=_sender(bot, row.chat_id, db),
                        decision=decision,
                    )
                    fetched["trade_call"] = "refused_quota"
                    if notified:
                        # ACTIVATION-FUNNEL-AUDIT-W1 (2026-05-28): funnel stage 13.
                        # Fires ONLY on a delivered notice — no event for a blocked
                        # subscriber, and none for the ~10k silent re-refusals that
                        # follow the one announcement of an episode.
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
                    # TG-REFERRAL-W1 (C3): at a value moment with no quota CTA to
                    # show, maybe append the referral nudge (throttled ≤1/7d; never
                    # stacks with a quota CTA). Marked below only when shown.
                    ref_nudge = referral_nudge_text(state, now=now) if not cta else ""
                    cta = cta or ref_nudge
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
                        # BOT-DIGEST-COUNT-ALL-CALLS-W1: ONE delivery seam (alerts_fired
                        # INSERT + consume_quota), recorded only after _push_photo
                        # returned True; quota-exhausted notices (handled above) are
                        # operator UX nudges and are NOT recorded. source='watch'.
                        record_call_delivered(db, row.chat_id, "watch")
                        db.increment_total_call_alerts(row.chat_id)
                        if cta:
                            db.increment_total_ctas_shown(row.chat_id)
                            # 24h-per-threshold throttle for the soft/urgent
                            # nudges; '100' is no-op (not throttled).
                            if threshold in ("75", "90"):
                                db.mark_quota_cta_fired(
                                    row.chat_id, threshold, now.isoformat()
                                )
                            elif ref_nudge:
                                # TG-REFERRAL-W1 (C3): stamp the 7d referral-nudge throttle.
                                db.mark_referral_nudge_sent(row.chat_id, now.isoformat())
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
    # BOT-QUOTA-REFUSAL-SEAM-W1: evaluate ONCE per distinct owner and project from
    # that decision — both the scheduler's skip set and the notice below read the
    # same snapshot rather than deriving it twice.
    decisions = {cid: evaluate_delivery(db, cid) for cid in calls_owners}
    exhausted = {cid for cid, d in decisions.items() if not d.allowed}
    # This pre-skip is a FETCH-BUDGET optimisation, and it must never again be the
    # thing that decides a user hears nothing. It drops the row before
    # `process_one_row` runs, which is exactly why that function's refusal branch was
    # unreachable for an already-walled user and why two subscribers were refused
    # ~10,000 times in silence. Announce the episode HERE, before dropping the rows.
    # `refuse_and_notify` no-ops once the episode is announced, so the cost is one
    # message per user per 30-day window — not one per cycle.
    for cid in sorted(exhausted):
        await refuse_and_notify(
            db,
            cid,
            "watch",
            send=_sender(bot, cid, db),
            decision=decisions[cid],
        )
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


# ── scan-digest verdict rendering ─────────────────────────────────────────────
# SCAN-DIGEST-MCP-PARITY-W1 CH3: the per-call digest line + its helpers (price/factor/
# reasoning formatting) moved to scan_digest.render_scan_digest_line — the bot mirror of
# the MCP renderScanDigestLine, ONE renderer shared by /scan + /scanwatch + (via the MCP)
# the webhook (single-derivation; CH4 canary pins it byte-identical to the TS SoT). The
# Python re-derivation — the per-coin get_trade_call depth merge (_enrich_scan_call) — is
# RETIRED: the scan now returns the enriched calls directly (includeReasoning:true).


def _format_scan_digest_push(
    enriched: list[dict[str, Any]], top_n: int, tf: str, exchange: str,
    rank_by: str = "oi",
) -> str:
    """Enriched scan-digest body: a 🚀 header + render_scan_digest_line per actionable
    call. Only called with ≥1 actionable call (all-HOLD rounds are suppressed upstream).
    SCAN-RANKBY-W1: the header reflects the lens (oi ⇒ 'OI', byte-identical to pre-wave).
    TG-SCANWATCH-TF-CADENCE-W1: re-scan cadence == the timeframe, so `@ {tf}` conveys it
    (dropped the coarse '(1h)' cadence tag)."""
    from .scan_digest import render_scan_digest_line

    header = (
        f"🚀Scan digest — top {top_n} perps by {rank_label(rank_by)} on {exchange} @ {tf}"
        f" — {len(enriched)} actionable:"
    )
    return "\n\n".join([header, *(render_scan_digest_line(c) for c in enriched)])


async def process_scan_digests(
    token: str, db_path: str, mcp_url: str | None, bypass_key: str, *, now_epoch: int | None = None
) -> dict[str, int]:
    """FEATURE-PARITY-CHANNELS-W1 CH4 — scheduled scan-digest producer (the bot twin of the
    webhook scan_digest scheduler). ISOLATED from run_cycle so the /watch path is byte-
    unchanged. For each scan-digest sub whose CURRENT cadence bucket hasn't fired: run
    scan_trade_calls ONCE per (top_n,tf,exchange) group, push the ranked calls, meter
    max(1, non-HOLD), and advance the bucket (one push per bucket; an exhausted owner is
    skipped but the bucket still advances — parity with the webhook PAUSE, no re-scan storm).
    An all-HOLD round (no actionable BUY/SELL) is SUPPRESSED — no push, no charge (parity
    with /watch silent-on-HOLD) — but the bucket still advances so it never re-scans the tick."""
    from .mcp_client import McpClientConfig
    from .scan_digest import timeframe_bucket_epoch

    db = Database(db_path)
    now = now_epoch if now_epoch is not None else int(time.time())
    counts: dict[str, int] = {"scan_due": 0, "scan_fired": 0, "scan_skipped_exhausted": 0, "scan_skipped_empty": 0, "scan_skipped_dup": 0, "scan_errors": 0}

    rows = db.list_all_scan_watches()
    # TG-SCANWATCH-TF-CADENCE-W1 (Approach B): re-scan bucket = the subscription's OWN
    # timeframe (not the coarse cadence column) → a 5m scanwatch is due every 5m.
    due = [r for r in rows if timeframe_bucket_epoch(r["timeframe"], now) > r["last_fired_bucket"]]
    counts["scan_due"] = len(due)
    if not due:
        return counts

    bot = Bot(token=token)
    cfg = McpClientConfig(url=mcp_url or "http://127.0.0.1:3000/mcp", internal_bypass_key=bypass_key)

    # SCAN-RANKBY-W1: rank_by joins the group key — each lens scans + pushes + dedups
    # independently (a chat's oi and nfr standing scans never collide on the bucket marker).
    groups: dict[tuple[int, str, str, str], list[Any]] = {}
    for r in due:
        groups.setdefault((r["top_n"], r["timeframe"], r["exchange"], r["rank_by"]), []).append(r)

    try:
        with McpClient(cfg) as mcp:
            for (top_n, tf, exchange, rank_by), grp in groups.items():
                try:
                    # Forward the stored lens (the MCP resolves; 'oi' is byte-identical to
                    # omitting → existing oi watches push the same digest as before).
                    result = mcp.call_tool(
                        "scan_trade_calls",
                        # SCAN-DIGEST-MCP-PARITY-W1 CH3: the enriched scan IS the digest — one
                        # call returns price+factors+reasoning+oi_change_window per coin (retires
                        # the per-coin get_trade_call depth re-derivation). Composes with the lens.
                        {"topN": top_n, "timeframe": tf, "exchange": exchange,
                         "rankBy": rank_by, "includeReasoning": True},
                    )
                except McpError as e:
                    log.warning(json.dumps({
                        "event": "scan_digest_mcp_failed", "top_n": top_n,
                        "tf": tf, "exchange": exchange, "rank_by": rank_by, "err": str(e)[:200],
                    }))
                    counts["scan_errors"] += len(grp)
                    continue
                calls = result.get("calls") or []
                non_hold = [c for c in calls if c.get("call") not in (None, "HOLD")]
                # TG-SCANWATCH-TF-CADENCE-W1: bucket = the group's TIMEFRAME (Approach B), and a
                # content-dedup signature = the sorted actionable (coin:call) set — a persistent
                # set re-sends NOTHING; only a NEW/changed set fires (timely, not spammy).
                bucket = timeframe_bucket_epoch(tf, now)
                sig = ",".join(
                    f"{c.get('coin')}:{c.get('call')}"
                    for c in sorted(non_hold, key=lambda c: str(c.get("coin")))
                )
                # All-HOLD round → no actionable BUY/SELL. Suppress the digest entirely
                # (parity with /watch, silent on HOLD): NO push + NO charge. STILL advance the
                # TF-bucket, and RESET the dedup sig to '' so a RETURNING set re-fires (X→∅→X).
                if not non_hold:
                    for r in grp:
                        counts["scan_skipped_empty"] += 1
                        db.mark_scan_watch_fired(
                            r["chat_id"], top_n, tf, exchange, bucket, rank_by, sig="",
                        )
                    continue
                # SCAN-DIGEST-MCP-PARITY-W1 CH3: the scan calls are ALREADY enriched
                # (price+factors+reasoning+oi_change_window via enrichScanCall) — NO per-coin
                # get_trade_call depth call, NO Python re-derivation. Render straight from the
                # scan payload (single-derivation LAW; the digest is computed once).
                for r in grp:
                    chat_id = r["chat_id"]
                    # Exhausted free owner → skip push+charge, advance the bucket, LEAVE the sig.
                    # BOT-QUOTA-REFUSAL-SEAM-W1: route the refusal through the seam so the
                    # owner is TOLD (once per window). This lane was the worst of the three
                    # — it refused silently AND wrote no telemetry, so a scanwatch owner
                    # hitting the wall was invisible on every operator surface.
                    scan_decision = evaluate_delivery(db, chat_id)
                    if not scan_decision.allowed:
                        counts["scan_skipped_exhausted"] += 1
                        await refuse_and_notify(
                            db,
                            chat_id,
                            "scanwatch",
                            send=_sender(bot, chat_id, db),
                            decision=scan_decision,
                        )
                        db.mark_scan_watch_fired(chat_id, top_n, tf, exchange, bucket, rank_by)
                        continue
                    # Content-dedup: the actionable set is UNCHANGED since last delivery →
                    # advance the bucket, no re-send (fires only on new/changed BUY/SELL).
                    if sig == (r["last_sent_sig"] or ""):
                        counts["scan_skipped_dup"] += 1
                        db.mark_scan_watch_fired(chat_id, top_n, tf, exchange, bucket, rank_by)
                        continue
                    text = _format_scan_digest_push(non_hold, top_n, tf, exchange, rank_by)
                    ok = await _push(bot, chat_id, text, db)
                    if ok:
                        # BOT-DIGEST-COUNT-ALL-CALLS-W1: one recorder call per actionable call
                        # delivered (alerts_fired + quota together). Store the delivered sig so
                        # an unchanged set next bucket dedups.
                        for _ in non_hold:
                            record_call_delivered(db, chat_id, "scanwatch")
                        counts["scan_fired"] += 1
                        db.mark_scan_watch_fired(chat_id, top_n, tf, exchange, bucket, rank_by, sig=sig)
                    else:
                        # push failed → advance the bucket but DON'T store the sig (retry next).
                        db.mark_scan_watch_fired(chat_id, top_n, tf, exchange, bucket, rank_by)
    except McpError as e:
        log.error("scan-digest mcp client init failed: %s", e)
        counts["scan_errors"] += len(due)
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

    # FEATURE-PARITY-CHANNELS-W1 CH4: scheduled scan-digest pass — isolated from run_cycle
    # (the /watch path above is byte-unchanged). Pushes due scan digests per cadence bucket.
    # Fail-soft: a scan-digest error never aborts the watch cycle (already journaled above).
    try:
        scan_counts = asyncio.run(process_scan_digests(token, db_path, mcp_url, bypass_key))
        log.info(json.dumps({"event": "scan_digests: complete", **scan_counts}))
    except Exception as e:  # noqa: BLE001 — a scan-digest failure must never crash the cron
        log.exception("scan-digest pass failed: %s", e)


if __name__ == "__main__":
    main()
