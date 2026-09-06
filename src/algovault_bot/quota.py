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
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Final, Literal, NamedTuple

from .db import Database
from .messages import signup_url
from .paywall import format_paywall_body


log = logging.getLogger(__name__)

# GROWTH-TG-QUOTA-PARITY-W1 CH2b (2026-08-27) — these four are PINNED FALLBACKS, not the answer.
#
# The live values come from the ladder mirror (`free_tier_ladder`, refreshed by the entitlement
# drain from signal-MCP's `GET /api/plans/public`, whose SoT is `src/lib/plans.ts`). These are what
# we SERVE when that mirror is absent or stale — never a reason to refuse anyone.
#
# 🛑 They must EQUAL the live ladder at ship time, and `tests/test_ladder_client.py` asserts
# exactly that against the endpoint's real response. A fallback that has drifted from the thing it
# stands in for is the same defect as a hand-typed constant, wearing a different coat.
#
# The architect's 2026-08-27 ruling unified the ALLOWANCE across the API and the bot: same NUMBER,
# different UNIT. The API meters a returned verdict (a HOLD is a call); the bot meters a DELIVERED
# ALERT (a silent HOLD costs nothing). That distinction is Rule 1 + Rule 3 of
# `docs/METERING-DIVERGENCE.md` and it SURVIVES this wave untouched.
FREE_TIER_MONTHLY_QUOTA: Final = 200
FREE_TIER_DAILY_QUOTA: Final = 100
#: Starter rung, mirrored for COPY only — it gates nothing. CH3 renders the upgrade line from it.
STARTER_PRICE_USD: Final = 9.99
STARTER_MONTHLY_CALLS: Final = 10_000

# GROWTH-TG-PLAN-PICKER-W1 R2 — the rest of the four-SKU ladder, pinned on the same terms as the
# two above: COPY ONLY, gating nothing, and they SERVE when the mirror is absent or stale.
#
# RE-EXPORTED, not defined here, and this module stays the place to import them from. They live in
# the leaf `plan_ladder.py` for one measured reason: `messages.welcome_message` needs the six-month
# total as a DEFAULT ARGUMENT, defaults are evaluated at `def` time, and `quota` imports `messages`
# — so the deferred-import trick `paywall.py` uses for this same cycle cannot supply one. The only
# alternative was hand-typing 39.90 a second time in `messages.py`, which is the `_TIER_QUOTA`
# defect this wave exists to retire. See that module's docstring for the full argument.
#
# Before this wave the two six-month totals were hand-typed inside shipped copy strings, because
# `/api/plans/public` carried no prepay field. R1 of this wave added `price_usd_6month` to that
# endpoint, so they are mirrored now and these constants are the fallback, not the source.
from .plan_ladder import (  # noqa: E402  (re-export beside its siblings, not at the import block)
    PRO_DAILY_CALLS,
    PRO_MONTHLY_CALLS,
    PRO_PRICE_6MONTH_USD,
    PRO_PRICE_USD,
    STARTER_DAILY_CALLS,
    STARTER_PRICE_6MONTH_USD,
)

__all__ = [
    "PRO_DAILY_CALLS",
    "PRO_MONTHLY_CALLS",
    "PRO_PRICE_6MONTH_USD",
    "PRO_PRICE_USD",
    "STARTER_DAILY_CALLS",
    "STARTER_PRICE_6MONTH_USD",
]

WINDOW_DAYS: Final = 30
WINDOW = timedelta(days=WINDOW_DAYS)

# BOT-W2 C3, amended by PRICING-BOT-DELIVERY-METERING-W1 (2026-08-17).
#
# Paid tiers bypass the bot-side 100/mo cap — that part is unchanged, and it is why the free
# meter never ticks for them. What changed is the other half: a paid-linked delivery now DEBITS
# the subscriber's plan allowance through the entitlement primitive, and the subscriber is HARD
# WALLED at the plan ceiling (architect ruling R-1). The old claim here — that paid users got
# uncapped bot pushes — is retired, and so are the per-tier figures that used to sit in this
# comment: they named a ladder the server had already moved past, i.e. they had been wrong for
# every linked subscriber since.
#
# The ladder's SoT is signal-MCP's `src/lib/plans.ts`, reached through the entitlement API. It is
# deliberately NOT restated here — a restated number is a number that goes stale, which is the
# whole reason gate leg L4b now fails a ladder-shaped run of figures in a comment.
PAID_TIERS: Final[frozenset[str]] = frozenset({"starter", "pro", "enterprise", "x402"})

# PRICING-BOT-DELIVERY-METERING-W1 CH5 — how old a plan mirror may be before the wall stops
# trusting it. THE SoT: `entitlement_drain` imports this rather than keeping its own copy, because
# two thresholds that must agree are two thresholds that will drift.
PLAN_MIRROR_STALE_AFTER: Final = timedelta(minutes=90)

# GROWTH-TG-QUOTA-PARITY-W1 CH2 — how old the LADDER mirror may be before the meter falls back to
# the pinned constants above.
#
# 🛑 DELIBERATELY NOT `PLAN_MIRROR_STALE_AFTER`, and these two must never be "aligned". They answer
# different questions. The plan mirror is a PER-SUBSCRIBER entitlement whose staleness means "this
# person's allowance may have changed while we were not looking" — 90 minutes is right for that.
# The ladder is near-static CONFIG that moves maybe monthly and identically for everyone; a
# 90-minute TTL would drop the whole free base to the fallback on any drain hiccup, for a value
# that had not changed. Two thresholds that answer different questions are not drift.
LADDER_STALE_AFTER: Final = timedelta(days=7)

#: Where an effective tier came from. Carried beside the value so a caller can say "last
#: known" rather than implying the server confirmed it a moment ago.
TierSource = Literal["mirror", "link", "unknown"]


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
    # OPS-BOT-LINKED-TIER-REFRESH-W1 CH2 — the server's CURRENT tier, from the same response
    # and stamped by the same `plan_state_as_of`. None = unobserved. Read it through
    # `effective_tier`, never directly: the fallback to `linked_tier` is the whole contract.
    plan_tier: str | None = None
    # GROWTH-TG-QUOTA-PARITY-W1 CH2c — the FREE lane's DAILY meter. `day_total` defaults to the
    # pinned fallback rather than to None: a meter with no ceiling is not a meter, and every
    # construction site that does not pass one is one the ladder has not reached yet.
    #
    # 🛑 These keep defaults for the reason stated above — the suite constructs QuotaState
    # POSITIONALLY (`QuotaState(47, 100, now, 0.47)`), so a non-defaulted field here breaks every
    # such test at CONSTRUCTION, before any assertion runs, and the failure names the wrong thing.
    day_used: int = 0
    day_total: int = FREE_TIER_DAILY_QUOTA
    #: UTC date of the last DAILY-wall notice. The daily lane's episode key — see `_notice_due`.
    quota_day_notice_day: str | None = None
    # CH3 renders the upgrade line from these. COPY ONLY — neither gates anything.
    starter_price_usd: float = STARTER_PRICE_USD
    starter_monthly_calls: int = STARTER_MONTHLY_CALLS
    # GROWTH-TG-PLAN-PICKER-W1 R2 — the six-month total, carried exactly the way the two above
    # are. It retires the `$39.90/6mo` string that `cta.py` hand-typed beside them, which was the
    # last figure in this lane bound to the server's ladder by nothing at all.
    #
    # 🛑 It keeps a default for the same reason they do: the suite constructs QuotaState
    # POSITIONALLY, so a non-defaulted field breaks every such test at CONSTRUCTION.
    starter_price_usd_6month: float = STARTER_PRICE_6MONTH_USD

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
    def effective_tier(self) -> EffectiveTier:
        """THE tier of this subscriber, and where it came from. See `_derive_tier`.

        🛑 THIS IS FOR LABELS, NOT FOR ENTITLEMENT. `is_paid` / `remaining` / `exhausted`
        below deliberately keep reading `linked_tier`, and that is not an oversight to be
        tidied up later:

          - The two agree on PAID_TIERS MEMBERSHIP in every reachable state, so nothing is
            gained. `plan_tier` is only ever written from an entitlement 200, whose `tier`
            comes from the same `validateApiKey` that must have returned a paid tier for the
            call to succeed at all — a 404 never reaches `update_plan_mirror`.
          - What WOULD change is the failure mode. Routing entitlement through the mirror
            means any future server response carrying a non-paid `tier` instantly strips paid
            treatment, with no grace window and no notice — a downgrade on a single
            observation, which is precisely what Build Rule 5 and CH3's 72h streak exist to
            prevent. Membership must move only through the lifecycle.

        The defect this wave fixes is a wrong LABEL, and the label is what re-points here.
        """
        return _derive_tier(self.plan_tier, self.plan_state_as_of, self.linked_tier)

    @property
    def remaining(self) -> int:
        if self.linked_tier in PAID_TIERS:
            # Project the PLAN's headroom when the mirror is fresh and the tier is capped.
            # `plan_total is None` means NO CEILING — never zero — so it falls through to the
            # effectively-uncapped sentinel rather than rendering a wall.
            if (
                self.plan_state is not PlanState.INDETERMINATE
                and self.plan_total is not None
                and self.plan_used is not None
            ):
                return max(0, self.plan_total - self.plan_used)
            return 10**9  # unknown or uncapped — never present a ceiling we cannot prove
        return max(0, self.total - self.used) + self.referral_bonus_remaining

    @property
    def monthly_exhausted(self) -> bool:
        """The FREE lane's 30-day rolling meter, bonus pool included."""
        return self.used >= self.total and self.referral_bonus_remaining <= 0

    @property
    def daily_exhausted(self) -> bool:
        """The FREE lane's UTC-day meter.

        🛑 The referral bonus does NOT lift this cap, deliberately. The bonus is extra BUDGET, and
        the daily cap is PACING — `OPS-QUOTA-CLAIM-ALIAS-W1` Probe 3 settled that "pacing is not
        budget". A referred user gets more alerts in total, spread over more days; they do not get
        to spend the whole pool in one afternoon.
        """
        return self.day_used >= self.day_total

    @property
    def exhausted(self) -> bool:
        if self.linked_tier in PAID_TIERS:
            # PRICING-BOT-DELIVERY-METERING-W1 R-1: a paid subscriber IS walled at the plan
            # ceiling — but ONLY on a fresh REFUSE. INDETERMINATE serves (fail-open): never wall a
            # paying customer on a measurement we could not take.
            return self.plan_state is PlanState.REFUSE
        # GROWTH-TG-QUOTA-PARITY-W1 CH2c: two REAL caps, not a cap and a sub-limit. A call is
        # refused when EITHER is spent — the same shape `plans.ts` documents for the API side.
        return self.monthly_exhausted or self.daily_exhausted

    @property
    def limit_kind(self) -> Literal["monthly", "daily"] | None:
        """WHICH free-lane meter refused, or None when nothing did.

        Single-derivation (CH2d): `evaluate_delivery` decides ONCE and every consumer projects
        from that answer. The copy layer must never re-derive which wall was hit — two
        independent derivations of one classification drift to contradiction, and here the
        contradiction would be telling a user to wait for a 30-day window that is not what
        stopped them.

        Monthly wins a tie: it is the wall with the longer horizon, so naming it is the more
        useful thing to tell someone who is out of both.
        """
        if self.linked_tier in PAID_TIERS:
            return None
        if self.monthly_exhausted:
            return "monthly"
        if self.daily_exhausted:
            return "daily"
        return None

    @property
    def is_paid(self) -> bool:
        return self.linked_tier in PAID_TIERS


class EffectiveTier(NamedTuple):
    """A tier and the provenance of that answer. Unpacks as ``(tier, source)``."""

    tier: str | None
    source: TierSource


def _derive_tier(
    plan_tier: str | None,
    plan_state_as_of: datetime | None,
    linked_tier: str | None,
) -> EffectiveTier:
    """THE SINGLE DERIVATION of "what tier is this subscriber".

    OPS-BOT-LINKED-TIER-REFRESH-W1 CH2. Every tier-labelled surface — the card badge, the
    link messages, the CTAs, the digest, admin stats — projects from this one function.
    There is no second copy, because a second copy is exactly what this wave retired:
    `linked_tier` was written once at /link and never refreshed while the server's answer
    arrived every few minutes and was discarded.

        fresh mirror        -> (plan_tier,   "mirror")   server truth
        absent/stale mirror -> (linked_tier, "link")     last known, LABELLED as such
        no link at all      -> (None,        "unknown")

    🛑 A STALE MIRROR FALLS BACK; it never renders blank and never fabricates. That is why
    this change cannot make anything worse than the behaviour it replaces: the floor is
    exactly `linked_tier`, which is what every surface read before.

    Freshness is `PLAN_MIRROR_STALE_AFTER` — the SAME window the wall's three-state decision
    turns on, not a second one. Two windows that must agree are two windows that will drift.
    """
    if plan_tier and plan_state_as_of is not None:
        if (_now() - plan_state_as_of) <= PLAN_MIRROR_STALE_AFTER:
            return EffectiveTier(plan_tier, "mirror")
    if linked_tier:
        return EffectiveTier(linked_tier, "link")
    return EffectiveTier(None, "unknown")


def effective_tier(row: Any) -> EffectiveTier:
    """Row adapter for `_derive_tier` — for callers holding a `subscribers` row rather than
    a QuotaState. Tolerant of a DB predating either migration."""
    keys = row.keys() if hasattr(row, "keys") else ()
    def _c(col: str) -> Any:
        return row[col] if col in keys else None
    return _derive_tier(_c("plan_tier"), _parse_ts(_c("plan_state_as_of")), _c("linked_tier"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_day_key(now: datetime | None = None) -> str:
    """Today's UTC calendar day as 'YYYY-MM-DD'.

    GROWTH-TG-QUOTA-PARITY-W1 CH2c. The free lane's daily meter keys on the same shape the paid
    lane's server-supplied `plan_daily_day` already uses, so a mismatch IS the roll signal and the
    daily counter needs no reset job, no timer and no cleanup cron.

    A CALENDAR boundary on purpose: unlike the rolling 30-day window — which starts at each user's
    own first alert and therefore resets on a date nobody can be told in advance — 00:00 UTC can be
    STATED in the refusal copy. That is what CH3's daily-wall strings say.
    """
    return (now or _now()).strftime("%Y-%m-%d")


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


class Ladder(NamedTuple):
    """The whole ladder the meter and the copy serve from, and where it came from.

    GROWTH-TG-PLAN-PICKER-W1 R2 widened this from the free + starter rungs to all four SKUs the
    plan picker renders. ONE VOCABULARY throughout: the field names here are the wire names
    `/api/plans/public` publishes and the column names `free_tier_ladder` stores, so a figure keeps
    the same name from `plans.ts` to the button label. Two names for one number is how the two
    sides of a mirror drift apart while every individual file reads correct.
    """

    free_monthly: int
    free_daily: int
    starter_price_usd: float
    starter_monthly_calls: int
    starter_daily_calls: int
    starter_price_usd_6month: float
    pro_price_usd: float
    pro_monthly_calls: int
    pro_daily_calls: int
    pro_price_usd_6month: float
    #: 'mirror' = the live ladder signal-MCP published. 'fallback' = the pinned constants, because
    #: the mirror was absent, stale, or unreadable. Carried so a caller can SAY which it served.
    #:
    #: 🛑 KEEP THIS LAST and construct `Ladder` by KEYWORD. It is a NamedTuple, so a positional
    #: construction silently re-binds every field when the ladder next widens — and this one has
    #: already widened once.
    source: Literal["mirror", "fallback"]


def _fallback_ladder() -> Ladder:
    """The pinned ladder, served whenever the mirror is absent, stale or unreadable.

    ONE construction site rather than one per fallback branch: two literals of the same ladder is
    the duplication this whole module exists to retire, and the second copy is the one that gets
    forgotten when a rung is added.
    """
    return Ladder(
        free_monthly=FREE_TIER_MONTHLY_QUOTA,
        free_daily=FREE_TIER_DAILY_QUOTA,
        starter_price_usd=STARTER_PRICE_USD,
        starter_monthly_calls=STARTER_MONTHLY_CALLS,
        starter_daily_calls=STARTER_DAILY_CALLS,
        starter_price_usd_6month=STARTER_PRICE_6MONTH_USD,
        pro_price_usd=PRO_PRICE_USD,
        pro_monthly_calls=PRO_MONTHLY_CALLS,
        pro_daily_calls=PRO_DAILY_CALLS,
        pro_price_usd_6month=PRO_PRICE_6MONTH_USD,
        source="fallback",
    )


def _row_get(row: sqlite3.Row, column: str) -> Any:
    """A column's value, or None when the row predates the migration that added it.

    `sqlite3.Row` raises `IndexError` on an unknown column rather than returning None, so a bot
    running against a DB that has not yet applied `PLAN_PICKER_MIGRATIONS` would crash the whole
    serving path on a read the caller is fully prepared to fall back on. The same tolerance
    `build_plan_refusal_text` already applies to `lang_code`.
    """
    return row[column] if column in row.keys() else None


def _pos_int(value: Any, pinned: int) -> int:
    """A mirrored count, or the pinned fallback. `bool` is not a count; zero is not a cap."""
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else pinned


def _pos_float(value: Any, pinned: float) -> float:
    """A mirrored price, or the pinned fallback. A free plan is not something this ladder sells."""
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        else pinned
    )


def resolve_ladder(db: Database, now: datetime | None = None) -> Ladder:
    """The ONE derivation of "what is the free allowance right now?".

    GROWTH-TG-QUOTA-PARITY-W1 CH2b. Reads the mirror written by the entitlement drain and falls
    back, PER FIELD, to the pinned constants.

    🛑 THE FALLBACK SERVES, IT NEVER REFUSES — the same discipline as `PlanState.INDETERMINATE` on
    the paid lane. A ladder we could not read is not evidence that a user is out of quota, and
    walling someone on a failed config read would be the fail-closed mistake this codebase has
    already paid for once.

    Per-field fallback rather than all-or-nothing: the starter rung feeds COPY and the free rung
    feeds ENFORCEMENT, so a response that carried the free rung but no starter tier should still
    move the meter. `parse_ladder` already refuses a payload missing the free rung outright.

    GROWTH-TG-PLAN-PICKER-W1 R2: the six new rungs inherit that discipline UNCHANGED. A DB that
    predates their migration returns a row without them, so `row[...]` is read through
    `_row_get` and every one of them independently degrades to its pinned constant. The source
    still reads 'mirror' in that case, and it is honest: the free rung — the only part that gates
    anything — genuinely did come from the mirror.
    """
    row = db.get_free_tier_ladder()
    if row is None:
        return _fallback_ladder()
    fetched = _parse_ts(row["fetched_at"])
    if fetched is None or ((now or _now()) - fetched) > LADDER_STALE_AFTER:
        return _fallback_ladder()
    return Ladder(
        free_monthly=_pos_int(_row_get(row, "free_monthly"), FREE_TIER_MONTHLY_QUOTA),
        free_daily=_pos_int(_row_get(row, "free_daily"), FREE_TIER_DAILY_QUOTA),
        starter_price_usd=_pos_float(_row_get(row, "starter_price_usd"), STARTER_PRICE_USD),
        starter_monthly_calls=_pos_int(_row_get(row, "starter_monthly_calls"), STARTER_MONTHLY_CALLS),
        starter_daily_calls=_pos_int(_row_get(row, "starter_daily_calls"), STARTER_DAILY_CALLS),
        starter_price_usd_6month=_pos_float(
            _row_get(row, "starter_price_usd_6month"), STARTER_PRICE_6MONTH_USD
        ),
        pro_price_usd=_pos_float(_row_get(row, "pro_price_usd"), PRO_PRICE_USD),
        pro_monthly_calls=_pos_int(_row_get(row, "pro_monthly_calls"), PRO_MONTHLY_CALLS),
        pro_daily_calls=_pos_int(_row_get(row, "pro_daily_calls"), PRO_DAILY_CALLS),
        pro_price_usd_6month=_pos_float(
            _row_get(row, "pro_price_usd_6month"), PRO_PRICE_6MONTH_USD
        ),
        source="mirror",
    )


def get_quota_state(db: Database, chat_id: int) -> QuotaState:
    """Read the user's current quota state. Auto-rolls expired window.

    BOT-W2 C3: when the subscriber is linked to a paid tier, the QuotaState's ``linked_tier``
    field is populated and the FREE meter is skipped. Since
    PRICING-BOT-DELIVERY-METERING-W1 that no longer means "served without limit": the paid lane is
    gated by the PLAN mirror instead (see ``plan_state`` / ``exhausted``).
    """
    ladder = resolve_ladder(db)
    row = db.get_subscriber(chat_id)
    if row is None:
        return QuotaState(
            0, ladder.free_monthly, None, 0.0, linked_tier=None,
            day_total=ladder.free_daily,
            starter_price_usd=ladder.starter_price_usd,
            starter_monthly_calls=ladder.starter_monthly_calls,
            starter_price_usd_6month=ladder.starter_price_usd_6month,
        )

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

    # GROWTH-TG-QUOTA-PARITY-W1 CH2c — the DAILY meter, rolled on READ as well as on write.
    # Reading a stale day as 0 is what makes the roll free: no cron, no timer, and a subscriber who
    # was walled yesterday is served today without anything having run overnight.
    day_key = _utc_day_key()
    stored_day = _m("alerts_day")
    day_used = int(_m("alerts_day_count") or 0) if stored_day == day_key else 0

    pct = (used / ladder.free_monthly) if ladder.free_monthly else 0.0
    return QuotaState(
        used,
        ladder.free_monthly,
        window_start,
        pct,
        linked_tier=linked_tier,
        day_used=day_used,
        day_total=ladder.free_daily,
        quota_day_notice_day=_m("quota_day_notice_day"),
        starter_price_usd=ladder.starter_price_usd,
        starter_monthly_calls=ladder.starter_monthly_calls,
        starter_price_usd_6month=ladder.starter_price_usd_6month,
        quota_75_last_fired_at=last_75,
        quota_90_last_fired_at=last_90,
        quota_100_last_fired_at=last_100,
        referral_bonus_remaining=bonus,
        referral_nudge_last_at=nudge_last,
        plan_tier=_m("plan_tier"),
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

    # OPS-BOT-DISPATCH-LATENCY-W1 CH2 — THE CHARGE IS ONE STATEMENT.
    #
    # This was a read-modify-write: the `get_quota_state` above read `alert_count`, the bonus
    # arithmetic ran here in Python, and an UPDATE wrote the ABSOLUTE result. Two charges
    # interleaving on one subscriber both read N and both wrote N+1 — one delivered alert
    # billed to nobody. It needs no concurrency inside the engine to happen:
    # `record_call_delivered` runs in the cron engine AND in `handlers.py` inside the separate,
    # always-running `algovault-bot.service`, and both open the same state.db.
    #
    # The bonus/headroom rule, the daily roll and the first-window stamp all moved INTO the
    # statement — not for tidiness, but because each of them read a counter that the other
    # writer could move underneath it. `referral_bonus_remaining` is covered deliberately: it
    # carries granted user value, and fixing only `alert_count` would close the lost-update
    # class on the counter nobody was losing while leaving it open on the one that costs a
    # user something.
    charged = db.consume_quota_atomic(
        chat_id, units, state.total, _utc_day_key(), _now().isoformat()
    )
    if charged is None:
        # Subscriber deleted between the read and the charge. Nothing to meter.
        return state
    new_used, new_bonus, new_day_used, window_raw = charged
    window_start = _parse_ts(window_raw)

    return QuotaState(
        new_used,
        state.total,
        window_start,
        (new_used / state.total) if state.total else 0.0,
        referral_bonus_remaining=new_bonus,
        day_used=new_day_used,
        day_total=state.day_total,
        quota_day_notice_day=state.quota_day_notice_day,
        starter_price_usd=state.starter_price_usd,
        starter_monthly_calls=state.starter_monthly_calls,
        starter_price_usd_6month=state.starter_price_usd_6month,
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
    # GROWTH-TG-QUOTA-PARITY-W1 CH2d — WHICH free-lane meter refused ('monthly' | 'daily'), or
    # None when nothing did or the subscriber is on the paid lane. Projected from
    # `QuotaState.limit_kind`, never re-decided: `build_refusal_text` reads this to pick the copy,
    # so the wall a user is TOLD about is by construction the wall that actually stopped them.
    #
    # Defaulted so every existing positional construction in the suite keeps working, for the same
    # reason QuotaState's fields are defaulted.
    limit_kind: Literal["monthly", "daily"] | None = None


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
    # GROWTH-TG-QUOTA-PARITY-W1 CH2e — the FREE lane now walls on TWO clocks, so it needs two
    # episode keys, exactly as the paid lane above already does.
    #
    # 🛑 The daily branch MUST NOT reuse `quota_100_last_fired_at`. That stamp is scoped to a
    # 30-day window; the daily wall re-arms every UTC day. Reusing it announces the daily wall at
    # most ONCE EVER — the user hits it again on day 2 and hears nothing. Same string-compare
    # shape as the paid `daily` branch, against the free lane's own `quota_day_notice_day`.
    #
    # Ordering mirrors `limit_kind`: monthly first, so a user out of both is told about the wall
    # with the longer horizon rather than the one that resets tonight.
    if state.monthly_exhausted:
        if state.window_start is None:
            return False  # never consumed anything ⇒ cannot be exhausted
        last = state.quota_100_last_fired_at
        return last is None or last < state.window_start
    if state.daily_exhausted:
        today = _utc_day_key()
        return state.quota_day_notice_day != today
    return False


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
        # CH2d — projected from the state's single derivation, never re-decided downstream.
        limit_kind=state.limit_kind,
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

    # GROWTH-TG-QUOTA-PARITY-W1 CH3 — the level is PROJECTED from `state.limit_kind`, the single
    # derivation `evaluate_delivery` already made. The copy layer never re-decides which wall was
    # hit: two independent derivations of one classification drift to contradiction, and here the
    # contradiction would be telling a user to wait 30 days when what stopped them resets at
    # midnight. `daily_block` renders the DAILY numerator/denominator for the same reason.
    if state.limit_kind == "daily":
        return format_paywall_body(
            "daily_block",
            state.day_used,
            state.day_total,
            signup_url("quota_exhausted_push", src),
            lang_code,
            starter_price_usd=state.starter_price_usd,
            starter_monthly_calls=state.starter_monthly_calls,
        )
    return format_paywall_body(
        "block",
        state.used,
        state.total,
        signup_url("quota_exhausted_push", src),
        lang_code,
        resets_at=resets_at,
        starter_price_usd=state.starter_price_usd,
        starter_monthly_calls=state.starter_monthly_calls,
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
    # OPS-BOT-LINKED-TIER-REFRESH-W1 CH2 — same single derivation as every other label.
    tier = (state.effective_tier.tier or "").capitalize()

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
                # GROWTH-TG-QUOTA-PARITY-W1 CH2e — THREE lanes, three stamps. `_notice_due`
                # reads a different episode key per lane, so stamping the wrong one is not a
                # cosmetic slip: it either re-notifies forever or silences the lane for good.
                #
                #   paid + daily   -> plan_wall_notice_day     (server's day key)
                #   FREE + daily   -> quota_day_notice_day     (our UTC day key)   <- NEW
                #   everything else-> quota_100_last_fired_at  (the monthly episode)
                #
                # Without the free-daily branch the daily wall fell through to the MONTHLY stamp,
                # which `_notice_due`'s daily branch never reads — so the notice would have
                # re-fired on EVERY dispatch cycle while the user stayed walled, and it would have
                # corrupted the monthly episode key on the way past. Caught in review, not by a
                # test: the tests covered the DECISION and not the stamping that follows it.
                if d.state.is_paid and d.state.plan_limit_kind == "daily" and d.state.plan_daily_day:
                    db.mark_plan_wall_notice_day(chat_id, d.state.plan_daily_day)
                elif not d.state.is_paid and d.limit_kind == "daily":
                    db.mark_quota_day_notice(chat_id, _utc_day_key())
                else:
                    db.mark_quota_cta_fired(chat_id, "100", _now().isoformat())
                # Record WHICH wall fired. For the paid lane `d.limit_kind` is None by
                # design (it describes the FREE lane's two meters); the paid wall's own
                # episode key is `plan_wall_notice_day`, stamped above.
                db.record_quota_notice_fired(chat_id, d.limit_kind)
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
        d.state.effective_tier.tier or "free",
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
