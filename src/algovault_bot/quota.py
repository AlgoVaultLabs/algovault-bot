"""Bot-side per-user quota tracking (D1-C).

signal-MCP's free tier is keyed by IP hash (one bucket per request IP). The
bot calls signal-MCP from one Hetzner host → all bot users would share one
bucket. D1-C resolves this by:

1. Bot calls signal-MCP with the X-AlgoVault-Internal-Key header → maps to
   ``tier:'internal'`` server-side → quota counter bypassed.
2. Bot enforces the user-facing 100 calls/month cap **here**, in its own
   SQLite ``subscribers`` table.

Calendar-month windowing matches signal-MCP's existing ``MONTH_MS`` rolling
30-day model: at first trade-call alert each new window, ``alerts_window_start``
is set to the current ``datetime('now')``; subsequent calls in the same
30-day window increment ``alert_count``.

Both **trade-call alerts (BUY/SELL)** and **regime-shift alerts** that actually
fire consume quota — parity with signal-MCP, which meters get_trade_call
(non-HOLD), get_market_regime, and scan_funding_arb alike. Only **HOLD trade
calls** stay free (silent, no tick), mirroring signal-MCP's free-HOLD policy in
``getTradeSignal``.

QUOTA-CONSISTENCY-COUNT-ALL-W1 (2026-06-08): corrected the prior premise that
regime alerts were free. (Funding-arb / market-scan are not yet bot features —
metering for those is deferred to a follow-up wave.)

BOT-QUOTA-REFUSAL-SEAM-W1 (2026-08-16): this module also owns the REFUSAL
side of the meter. See ``refuse_and_notify`` below — before it, three push
lanes each re-derived "is this user out of quota?" independently and drifted
to three different answers (silent-refuse / silent-refuse-and-invisible /
never-refuse-at-all), leaving two walled users refused ~10,000 times without
ever being told. Both halves of the meter now live here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Awaitable, Callable, Final, Literal

from .db import Database
from .messages import signup_url
from .paywall import format_paywall_body


log = logging.getLogger(__name__)

FREE_TIER_MONTHLY_QUOTA: Final = 100
WINDOW_DAYS: Final = 30
WINDOW = timedelta(days=WINDOW_DAYS)

# BOT-W2 C3 — paid tiers bypass the bot-side 100/mo cap entirely. The user's
# real Stripe-backed quota (3K/15K/100K) is enforced server-side by signal-MCP
# on their direct API calls; bot-driven calls go through tier:'internal' which
# doesn't tick any counter. Net: paid users get unlimited bot pushes.
PAID_TIERS: Final[frozenset[str]] = frozenset({"starter", "pro", "enterprise", "x402"})

# PRICING-BOT-DELIVERY-METERING-W1 CH5 — how old a plan mirror may be before the wall stops
# trusting it. THE SoT: `entitlement_drain` imports this rather than keeping its own copy, because
# two thresholds that must agree are two thresholds that will drift.
PLAN_MIRROR_STALE_AFTER: Final = timedelta(minutes=90)


class PlanState(Enum):
    """Three states, not two — the whole point of this chapter.

    A boolean cannot distinguish "the server says no" from "we could not ask", and collapsing them
    is how a paying customer gets walled by a network blip. Same discipline as
    `verification-gates.md`'s exit-3, applied to a live serving path, and the same lesson as the
    prior wave's `?? 0` rendering: an absent value is not a zero.
    """

    ALLOW = "allow"
    REFUSE = "refuse"
    INDETERMINATE = "indeterminate"


@dataclass
class QuotaState:
    used: int
    total: int
    window_start: datetime | None
    pct_used: float
    # BOT-W2 C3 — set when subscribers.linked_tier is in PAID_TIERS.
    # Engine reads this to skip the quota gate + the quota line in the alert
    # message + all 75/90/100% CTAs (the user is already paying).
    linked_tier: str | None = None
    # BOT-ALERT-CLEANUP-W1 — last-fired timestamps for the soft 75% / urgent
    # 90% trade-call CTAs. ``cta.trade_call_cta_text`` uses these to enforce a
    # 24h-per-threshold throttle so users see at most one nudge per threshold
    # per day. Both are ``None`` until the threshold first fires.
    quota_75_last_fired_at: datetime | None = None
    quota_90_last_fired_at: datetime | None = None
    # BOT-QUOTA-REFUSAL-SEAM-W1 — when this subscriber was last TOLD they hit the
    # wall. Compared against ``window_start`` by ``_notice_due`` so the notice fires
    # once per exhaustion episode. None = never told (a walled user with None here
    # is the exact population this wave found refused ~10,000 times in silence).
    quota_100_last_fired_at: datetime | None = None
    # TG-REFERRAL-W1 (C2) — bot-side referee bonus-call pool (persistent; NOT
    # window-reset). Drawn AFTER the monthly free `total` by consume_quota, and
    # it extends `remaining`/`exhausted`. 0 for everyone who wasn't referred →
    # byte-identical behaviour for the existing free/paid base.
    referral_bonus_remaining: int = 0
    # TG-REFERRAL-W1 (C3) — last value-moment referral-nudge timestamp; the 7d
    # throttle lives in cta.referral_nudge_text. None until the first nudge fires.
    referral_nudge_last_at: datetime | None = None
    # PRICING-BOT-DELIVERY-METERING-W1 CH5 — the PLAN MIRROR: a local copy of the server's answer
    # for a paid-linked subscriber, refreshed by the drainer.
    # 🛑 EVERY field below MUST keep a default. The existing suite constructs QuotaState
    # POSITIONALLY (e.g. `QuotaState(47, 100, now, 0.47)`), so a non-defaulted field breaks the
    # whole suite at construction rather than at the assertion.
    plan_used: int | None = None
    plan_total: int | None = None          # None = no ceiling (uncapped tier), NEVER zero
    plan_allowed: int | None = None        # 0/1 as stored
    plan_limit_kind: str | None = None     # 'monthly' | 'daily' | None
    plan_period_start: str | None = None   # monthly episode key
    plan_daily_day: str | None = None      # daily episode key
    plan_next_json: str | None = None      # verbatim next_plan JSON from the server
    plan_state_as_of: datetime | None = None   # None = NEVER OBSERVED
    plan_wall_notice_day: str | None = None    # UTC date of the last DAILY-wall notice

    @property
    def plan_state(self) -> PlanState:
        """The paid lane's three-state read of the mirror.

        INDETERMINATE covers BOTH "never observed" (`plan_state_as_of is None`) and "too old to
        trust". Both mean the same thing operationally: we do not currently know, so we must not
        refuse. Only a FRESH mirror may wall anyone.
        """
        if self.plan_state_as_of is None:
            return PlanState.INDETERMINATE
        if (_now() - self.plan_state_as_of) > PLAN_MIRROR_STALE_AFTER:
            return PlanState.INDETERMINATE
        return PlanState.ALLOW if self.plan_allowed else PlanState.REFUSE

    @property
    def remaining(self) -> int:
        if self.linked_tier in PAID_TIERS:
            # Project the PLAN's headroom when the mirror is fresh and the tier is capped.
            # `plan_total is None` means NO CEILING — never zero — so it falls through to the
            # effectively-unlimited sentinel rather than rendering a wall.
            if (
                self.plan_state is not PlanState.INDETERMINATE
                and self.plan_total is not None
                and self.plan_used is not None
            ):
                return max(0, self.plan_total - self.plan_used)
            return 10**9  # unknown or uncapped — never present a ceiling we cannot prove
        return max(0, self.total - self.used) + self.referral_bonus_remaining

    @property
    def exhausted(self) -> bool:
        if self.linked_tier in PAID_TIERS:
            # PRICING-BOT-DELIVERY-METERING-W1 R-1: a paid subscriber IS walled at the plan
            # ceiling — but ONLY on a fresh REFUSE. INDETERMINATE serves (fail-open): never wall a
            # paying customer on a measurement we could not take.
            return self.plan_state is PlanState.REFUSE
        return self.used >= self.total and self.referral_bonus_remaining <= 0

    @property
    def is_paid(self) -> bool:
        return self.linked_tier in PAID_TIERS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # SQLite datetime('now') yields "YYYY-MM-DD HH:MM:SS" (UTC, no tz suffix)
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def get_quota_state(db: Database, chat_id: int) -> QuotaState:
    """Read the user's current quota state. Auto-rolls expired window.

    BOT-W2 C3: when the subscriber is linked to a paid tier, the QuotaState's
    ``linked_tier`` field is populated and the engine treats the user as
    unlimited (skips the gate, omits the quota line, suppresses CTAs).
    """
    row = db.get_subscriber(chat_id)
    if row is None:
        return QuotaState(0, FREE_TIER_MONTHLY_QUOTA, None, 0.0, linked_tier=None)

    used = int(row["alert_count"] or 0)
    window_start = _parse_ts(row["alerts_window_start"])
    linked_tier = row["linked_tier"]
    last_75 = _parse_ts(row["quota_75_last_fired_at"]) if "quota_75_last_fired_at" in row.keys() else None
    last_90 = _parse_ts(row["quota_90_last_fired_at"]) if "quota_90_last_fired_at" in row.keys() else None
    last_100 = _parse_ts(row["quota_100_last_fired_at"]) if "quota_100_last_fired_at" in row.keys() else None
    bonus = int(row["referral_bonus_remaining"] or 0) if "referral_bonus_remaining" in row.keys() else 0
    nudge_last = _parse_ts(row["referral_nudge_last_at"]) if "referral_nudge_last_at" in row.keys() else None
    k = row.keys()
    def _m(col: str):  # mirror column, tolerant of a DB that predates the migration
        return row[col] if col in k else None

    # Window expired → reset counter (still applies to free-tier users; paid
    # users don't tick the counter at all per ``consume_quota`` below).
    if window_start is not None and (_now() - window_start) > WINDOW:
        used = 0
        window_start = None
        with db._cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET alert_count = 0, alerts_window_start = NULL "
                "WHERE chat_id = ?",
                (chat_id,),
            )

    pct = (used / FREE_TIER_MONTHLY_QUOTA) if FREE_TIER_MONTHLY_QUOTA else 0.0
    return QuotaState(
        used,
        FREE_TIER_MONTHLY_QUOTA,
        window_start,
        pct,
        linked_tier=linked_tier,
        quota_75_last_fired_at=last_75,
        quota_90_last_fired_at=last_90,
        quota_100_last_fired_at=last_100,
        referral_bonus_remaining=bonus,
        referral_nudge_last_at=nudge_last,
        plan_used=_m("plan_used"),
        plan_total=_m("plan_total"),
        plan_allowed=_m("plan_allowed"),
        plan_limit_kind=_m("plan_limit_kind"),
        plan_period_start=_m("plan_period_start"),
        plan_daily_day=_m("plan_daily_day"),
        plan_next_json=_m("plan_next_json"),
        plan_state_as_of=_parse_ts(_m("plan_state_as_of")),
        plan_wall_notice_day=_m("plan_wall_notice_day"),
    )


def _clamp_units(units: int) -> int:
    """Default-deny clamp (CLAUDE.md): NaN/invalid/<1 → 1; else floor to an int ≥ 1."""
    try:
        u = int(units)
    except (TypeError, ValueError):
        return 1
    return u if u >= 1 else 1


def consume_quota(db: Database, chat_id: int, units: int = 1) -> QuotaState:
    """Increment the user's billable-alert counter by `units` (default 1 = byte-identical
    for existing callers; the scanner rule passes max(1, non-HOLD) — FEATURE-PARITY-CHANNELS-W1
    CH3). Starts a new window if needed.

    BOT-W2 C3: paid-tier-linked users SKIP the increment entirely (no-op
    return of current state). Their bot-pushed alerts don't count against
    anything — not the bot's 100/mo, not signal-MCP's per-key quota (bot
    calls go through tier:'internal' which bypasses the counter server-side).

    Free / unlinked users: same as W1 — increment + start a new 30-day window
    if needed, write the timestamp via Python (canonical ISO 8601) so the
    next ``get_quota_state`` reads back the exact same value (no microsecond
    drift across calls within the same window).
    """
    state = get_quota_state(db, chat_id)
    if state.is_paid:
        return state  # no-op for paid tiers
    units = _clamp_units(units)
    bonus = state.referral_bonus_remaining
    if bonus > 0:
        # TG-REFERRAL-W1: fill the monthly free headroom first, then draw any
        # overflow from the referee bonus pool (persistent; not window-reset).
        headroom = max(0, FREE_TIER_MONTHLY_QUOTA - state.used)
        monthly_charge = min(units, headroom)
        new_used = state.used + monthly_charge
        new_bonus = max(0, bonus - (units - monthly_charge))
    else:
        # byte-identical to the pre-bonus meter for the (today: 100%) bonus-free base
        new_used = state.used + units
        new_bonus = 0
    with db._cursor() as cur:
        if state.window_start is None:
            window_start = _now()
            cur.execute(
                "UPDATE subscribers SET alert_count = ?, alerts_window_start = ?, "
                "referral_bonus_remaining = ? WHERE chat_id = ?",
                (new_used, window_start.isoformat(), new_bonus, chat_id),
            )
        else:
            window_start = state.window_start
            cur.execute(
                "UPDATE subscribers SET alert_count = ?, referral_bonus_remaining = ? "
                "WHERE chat_id = ?",
                (new_used, new_bonus, chat_id),
            )
    return QuotaState(
        new_used,
        FREE_TIER_MONTHLY_QUOTA,
        window_start,
        new_used / FREE_TIER_MONTHLY_QUOTA,
        referral_bonus_remaining=new_bonus,
    )


# ── BOT-DIGEST-COUNT-ALL-CALLS-W1: the single delivery seam ──────────────────
# Every bot delivery path routes one actionable item through one of these recorders,
# which BOTH log the row for the digest AND meter quota — so alerts_fired can never
# again drift from the quota meter (the bug this wave fixes: scanwatch + scan charged
# quota but never wrote alerts_fired, so the digest undercounted). Future delivery
# paths (webhook top:N, batch tools) inherit correct telemetry by calling these.


def _enqueue_plan_debit(db: Database, chat_id: int, kind: str, alerts_fired_id: int) -> None:
    """PRICING-BOT-DELIVERY-METERING-W1 CH4d — queue ONE plan debit for a paid-linked subscriber.

    THE BOT-SIDE GENERATOR HOOK. This sits inside the two recorders rather than at any lane, so
    every current lane (watch / scanwatch / scan) and every future one (webhook, batch — already in
    ALLOWED_ALERT_SOURCES) inherits plan metering by calling the recorder it already calls. Adding
    the enqueue to a lane directly would be the per-channel decision this whole wave retires.

    FREE SUBSCRIBERS ARE UNTOUCHED — no row, no behaviour change, not one byte. The free lane's
    meter remains the bot's own SQLite (docs/METERING-DIVERGENCE.md), and only the PAID lane is
    unified with the server's.

    Local SQLite in the same already-autocommitting path as the alerts_fired INSERT above it, so it
    cannot fail or delay the delivery. It also cannot RAISE into the dispatch loop: a metering
    bookkeeping fault must never cost a subscriber an alert they were entitled to.
    """
    try:
        row = db.get_subscriber(chat_id)
        if row is None:
            return
        keys = row.keys()
        if "linked_api_key" not in keys or not row["linked_api_key"]:
            return
        if row["linked_tier"] not in PAID_TIERS:
            return
        if not alerts_fired_id:
            # No delivery-ledger id ⇒ no idempotency source. Refuse to mint a substitute: a
            # synthesised key defeats the guard on exactly the retry it exists for.
            log.warning(
                '{"event": "plan_debit_no_ledger_id", "chat_id": %d, "kind": "%s"}', chat_id, kind
            )
            return
        db.enqueue_entitlement_debit(f"bot:{chat_id}:{alerts_fired_id}", chat_id, kind)
    except Exception as err:  # noqa: BLE001 — never break a delivery over bookkeeping
        log.warning("plan debit enqueue failed chat_id=%s err=%s", chat_id, err)


def record_call_delivered(db: Database, chat_id: int, source: str) -> None:
    """Record + meter ONE delivered actionable trade call. ``alerts_fired`` INSERT
    ALWAYS (even paid tiers — the digest is delivery volume, not billing); quota
    ``consume_quota`` is a no-op for paid tiers. Call exactly once per non-HOLD call
    delivered (HOLD verdicts stay silent + free and never reach here). ``source`` ∈
    {'watch','scanwatch','scan',...} per ALLOWED_ALERT_SOURCES.

    PRICING-BOT-DELIVERY-METERING-W1 CH4d: a paid-linked subscriber ALSO gets one
    ``entitlement_outbox`` row, keyed on the ``alerts_fired`` id this call just wrote."""
    alerts_fired_id = db.record_alert_fired(chat_id, "call", source)
    consume_quota(db, chat_id)
    _enqueue_plan_debit(db, chat_id, "call", alerts_fired_id)


def record_regime_delivered(db: Database, chat_id: int, source: str) -> None:
    """Record + meter ONE delivered regime-shift alert — same insert+meter contract
    as ``record_call_delivered`` (regime alerts count toward quota since
    QUOTA-CONSISTENCY-COUNT-ALL-W1). Bot regime pushes are ``source='watch'``."""
    alerts_fired_id = db.record_alert_fired(chat_id, "regime", source)
    consume_quota(db, chat_id)
    _enqueue_plan_debit(db, chat_id, "regime", alerts_fired_id)


# ── BOT-QUOTA-REFUSAL-SEAM-W1: the single REFUSAL seam ───────────────────────
# The symmetric counterpart of the delivery seam above. Every lane that can be
# refused for quota routes through ``evaluate_delivery`` (the ONE derivation) and,
# if it is a PUSH lane, through ``refuse_and_notify`` (the ONE notice). Before this
# wave three push lanes each re-derived the decision and drifted to three different
# behaviours; `scripts/check-quota-refusal-seam.py` now makes that unwritable.

# The lane table IS the gate's source of truth. A lane is keyed by the name of the
# function that reads the decision, and its VALUE declares how it refuses:
#   'push' — the user is ABSENT. Refusing silently is invisible to them, so the lane
#            MUST call ``refuse_and_notify``.
#   'pull' — the user is PRESENT and waiting on a reply. The returned message IS the
#            notice, so the lane MUST return a value from the refusal branch.
# Adding a lane without declaring it here FAILS the gate; declaring one that no
# longer reads the decision FAILS it too (a stale entry rots into a permission slip
# — that is exactly how the dark `paywall.py` survived 80 days).
REFUSAL_LANES: Final[dict[str, Literal["push", "pull"]]] = {
    "process_one_row": "push",
    "run_cycle": "push",
    "process_scan_digests": "push",
    "handle_scan": "pull",
    "handle_regime": "pull",
    "handle_call": "pull",
    "handle_funding": "pull",
}


@dataclass(frozen=True)
class QuotaDecision:
    """The ONE derivation of "may this user be served?".

    Consumers PROJECT from these fields; they never re-read ``.exhausted``
    themselves (the gate enforces it). ``state`` is carried so a caller that
    needs the numbers renders them from the same snapshot the decision was
    made on, rather than issuing a second read that can disagree.
    """

    allowed: bool
    state: QuotaState
    notify: bool


def _notice_due(state: QuotaState) -> bool:
    """Has this exhaustion EPISODE already been announced?

    Window-scoped rather than time-throttled: the key is the window the user is
    currently walled in, so a new window re-arms the notice by itself with no timer and no
    cleanup job. A walled user stays walled for up to 30 days — a 24h-style throttle (the shape
    used by the 75%/90% CTAs) would send the same "you are out" message ~21 times and earn the
    bot a block.

    PRICING-BOT-DELIVERY-METERING-W1 CH5c — THREE lanes, because they wall on different clocks:

      free                     episode = `window_start`        stamp = quota_100_last_fired_at
      paid, limit = 'monthly'  episode = `plan_period_start`   stamp = quota_100_last_fired_at
      paid, limit = 'daily'    episode = `plan_daily_day`      stamp = plan_wall_notice_day

    The daily cap re-arms every UTC day. Reusing the monthly stamp for it would send at most ONE
    notice ever — the user would hit the daily wall on day 2 and hear nothing. The free lane's
    arithmetic below is byte-identical to before this wave and is pinned by a regression test.
    """
    if state.is_paid:
        if state.plan_limit_kind == "daily":
            # A calendar-day episode: a plain string compare is the whole test.
            return bool(state.plan_daily_day) and state.plan_wall_notice_day != state.plan_daily_day
        if state.plan_limit_kind == "monthly":
            period = _parse_ts(state.plan_period_start)
            if period is None:
                return False
            last = state.quota_100_last_fired_at
            return last is None or last < period
        # Walled with no limit_kind: we cannot scope an episode, so announce once ever rather
        # than risk a loop.
        return state.quota_100_last_fired_at is None
    if state.window_start is None:
        return False  # never consumed anything ⇒ cannot be exhausted
    last = state.quota_100_last_fired_at
    return last is None or last < state.window_start


def evaluate_delivery(db: Database, chat_id: int) -> QuotaDecision:
    """THE decision. Pure read — no writes, no network, safe to call on every cycle.

    CH5: for a paid-linked subscriber this projects from the local plan MIRROR, which is why the
    mirror lives on `subscribers` and why the drainer keeps it warm. This function must never
    acquire network I/O — it runs O(subscribers)/minute on the dispatch loop.
    """
    state = get_quota_state(db, chat_id)
    if state.is_paid and state.plan_state is PlanState.INDETERMINATE:
        # SERVE, and say so. A paying customer is never walled on a measurement we could not take;
        # CH6 counts these so a silently-dark mirror is visible rather than free service forever.
        log.info(
            '{"event": "plan_mirror_indeterminate", "chat_id": %d, "as_of": "%s"}',
            chat_id,
            state.plan_state_as_of.isoformat() if state.plan_state_as_of else "never",
        )
    exhausted = state.exhausted
    return QuotaDecision(
        allowed=not exhausted,
        state=state,
        notify=exhausted and _notice_due(state),
    )


def build_refusal_text(db: Database, chat_id: int, state: QuotaState) -> str:
    """The ONE walled-user message, shared by every push lane.

    BOT-QUOTA-REFUSAL-SEAM-W1 R-4(a): reuses ``paywall.format_paywall_body`` —
    trilingual, ≤300 chars, already tested — but fed from the BOT's own meter
    (``state.used``/``state.total``) instead of the MCP ``_algovault.tier_warning``
    it originally keyed on. That field is unreachable here by construction: the bot
    authenticates with ``X-AlgoVault-Internal-Key`` → ``tier:'internal'``, and
    signal-MCP's ``withTierWarning`` returns the meta unchanged for bot-internal
    callers, so the module never once fired in ~80 days live (0/57 subscribers ever
    stamped). Rewiring it to the meter we actually enforce is what makes it reachable.

    ``referral_link``/``bonus_calls`` are deliberately omitted: sourcing them needs a
    network call to the engine SoT, and this runs on the dispatch loop's refusal path
    where a guard must be cheap and must not throw. ``format_paywall_body`` documents
    the absent-referral fallback as the verbatim block copy. Follow-up flagged.
    """
    row = db.get_subscriber(chat_id)
    lang_code = None
    if row is not None and "lang_code" in row.keys():
        lang_code = row["lang_code"]
    src = db.get_acquisition_source(chat_id)
    # The REAL horizon, not "next month": this meter is a rolling 30-day window
    # anchored on `alerts_window_start`, so the reset date is a property of when the
    # user first consumed, not of the calendar.
    resets_at = None
    if state.window_start is not None:
        resets_at = (state.window_start + WINDOW).strftime("%d %b %Y")
    return format_paywall_body(
        "block",
        state.used,
        state.total,
        signup_url("quota_exhausted_push", src),
        lang_code,
        resets_at=resets_at,
    )


def build_plan_refusal_text(db: Database, chat_id: int, state: QuotaState) -> str:
    """The PAID lane's walled message. PRICING-BOT-DELIVERY-METERING-W1 CH5d.

    🛑 ZERO HAND-TYPED FIGURES. Every number here comes from the mirror (`plan_used`,
    `plan_total`) or from `plan_next_json`, both of which are the server's own answer projected
    from `plans.ts`. That is not stylistic: `messages._TIER_QUOTA` hard-coded the ladder and was
    WRONG for every linked subscriber from the day the ladder moved, and gate leg L4 (CH6) exists
    to make that unwritable. A literal here would also trip L3's `\b100\b…calls?` ban the moment
    Pro's "100,000 calls" appeared.

    The two walls re-open on different clocks and the copy must say which:
      monthly — the server's ROLLING 30 days from `plan_period_start`, so state the date.
      daily   — 00:00 UTC, a calendar boundary that can simply be named.

    Like its free-lane sibling: cheap, and it REFUSES rather than throws.
    """
    used = state.plan_used
    total = state.plan_total
    limit = state.plan_limit_kind or "monthly"
    tier = (state.linked_tier or "").capitalize()

    row = db.get_subscriber(chat_id)
    lang = None
    if row is not None and "lang_code" in row.keys():
        lang = row["lang_code"]
    lang = (lang or "en").lower().replace("_", "-")

    figures = f"{used}/{total}" if used is not None and total is not None else ""

    if limit == "daily":
        when_en = "Resets 00:00 UTC."
        when_id = "Direset pukul 00:00 UTC."
        when_zh = "UTC 00:00 重置。"
    else:
        reopen = ""
        period = _parse_ts(state.plan_period_start)
        if period is not None:
            reopen = (period + timedelta(days=30)).strftime("%d %b %Y")
        when_en = f"Resets {reopen}." if reopen else "Resets when your 30-day plan window rolls."
        when_id = f"Direset {reopen}." if reopen else "Direset saat jendela 30 hari paket Anda berputar."
        when_zh = f"{reopen} 重置。" if reopen else "您的 30 天套餐周期结束后重置。"

    # The next rung, projected verbatim from the server. `null` (enterprise) means there is NO
    # self-serve next rung — say so plainly rather than fabricate one.
    nxt = None
    if state.plan_next_json:
        try:
            nxt = json.loads(state.plan_next_json)
        except (ValueError, TypeError):
            nxt = None

    if nxt:
        calls = nxt.get("monthly_calls")
        upsell_en = (
            f"Upgrade to {nxt.get('label') or nxt.get('id')}"
            + (f" ({calls:,} calls/mo)" if isinstance(calls, int) else "")
            + f": {nxt.get('signup_url', '')}"
        )
        upsell_id = upsell_zh = upsell_en
    else:
        upsell_en = "You are on the top self-serve plan — reply here and we will size the next step with you."
        upsell_id = "Anda sudah di paket mandiri tertinggi — balas di sini dan kami bantu langkah berikutnya."
        upsell_zh = "您已使用最高自助套餐——请回复，我们将为您安排后续方案。"

    if lang.startswith("id"):
        return f"Kuota paket {tier} habis: {figures} alert. {when_id} {upsell_id}"
    if lang.startswith("zh"):
        return f"{tier} 套餐额度已用完：{figures}。{when_zh}{upsell_zh}"
    return f"{tier} plan allowance used: {figures}. {when_en} {upsell_en}"


async def refuse_and_notify(
    db: Database,
    chat_id: int,
    source: str,
    *,
    send: Callable[[str], Awaitable[bool]],
    decision: QuotaDecision | None = None,
) -> bool:
    """Refuse ONE push-lane delivery, and tell the user the first time it happens.

    Returns True iff a notice was delivered on this call.

    ``send`` is INJECTED rather than imported so this module stays a leaf — importing
    ``alert_engine._push`` here would close a cycle (alert_engine already imports this
    module). ``decision`` may be passed by a caller that already evaluated, so a lane
    never derives the decision twice within one cycle.

    Telemetry is a LOG LINE, never a table row: refusals run at ~10k/week and a row
    per refusal is write amplification for a quantity that is a STATE, not an event.
    The digest renders that state live (``walled_now``).

    Refuses, never throws (`build-and-runtime.md`: a guard on a live serving path
    REFUSES, it does not THROW). A failure to render or send a notice must not take
    the dispatch loop down for every other subscriber.
    """
    d = decision if decision is not None else evaluate_delivery(db, chat_id)
    notified = False
    try:
        if d.notify:
            # CH5e: the paid lane gets the PLAN wall's copy (server figures, plan clock); the free
            # lane keeps its own, byte-identical to before this wave.
            text = (
                build_plan_refusal_text(db, chat_id, d.state)
                if d.state.is_paid
                else build_refusal_text(db, chat_id, d.state)
            )
            if await send(text):
                # Stamp ONLY after a delivered send — a blocked or rate-limited
                # subscriber must not silently burn the one notice of the episode
                # (the discipline the pre-seam watch lane already applied to
                # ``quota_notices_fired``, preserved here).
                # Stamp the episode key this wall actually belongs to. The daily wall re-arms
                # every UTC day, so it carries its own stamp — see `_notice_due`.
                if d.state.is_paid and d.state.plan_limit_kind == "daily" and d.state.plan_daily_day:
                    db.mark_plan_wall_notice_day(chat_id, d.state.plan_daily_day)
                else:
                    db.mark_quota_cta_fired(chat_id, "100", _now().isoformat())
                db.record_quota_notice_fired(chat_id)
                db.increment_total_ctas_shown(chat_id)
                notified = True
    except Exception as err:  # noqa: BLE001 — never break the dispatch loop
        log.warning(
            "refusal notice failed chat_id=%s source=%s err=%s", chat_id, source, err
        )
    log.info(
        '{"event": "quota_refused", "chat_id": %d, "source": "%s", '
        '"used": %d, "total": %d, "notified": %s, "tier": "%s", "limit_kind": "%s"}',
        chat_id,
        source,
        d.state.used,
        d.state.total,
        "true" if notified else "false",
        d.state.linked_tier or "free",
        d.state.plan_limit_kind or "",
    )
    return notified


def count_walled_now(db: Database) -> tuple[int, int, int]:
    """``(walled, silent, walled_paid)`` across live subscribers, RIGHT NOW.

    Projects from ``evaluate_delivery`` — never a re-implemented ``alert_count >=
    100`` in SQL, which would be a second derivation of the very decision this
    module exists to own. ``silent`` counts walled users who have not been told;
    it is 0 in a healthy system and any non-zero value is a seam defect, which is
    precisely the signal the digest was missing.
    """
    walled = silent = walled_paid = 0
    for chat_id in db.get_active_chat_ids():
        d = evaluate_delivery(db, chat_id)
        if d.allowed:
            continue
        walled += 1
        if d.state.is_paid:
            # CH5f: a walled PAYING subscriber is a different operator signal from a walled free
            # one — it is revenue at its ceiling, not a conversion opportunity.
            walled_paid += 1
        if d.state.quota_100_last_fired_at is None:
            silent += 1
    return walled, silent, walled_paid
