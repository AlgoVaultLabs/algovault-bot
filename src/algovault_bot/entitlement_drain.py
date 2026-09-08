"""PRICING-BOT-DELIVERY-METERING-W1 CH4f — drain the plan-debit outbox.

The recorder enqueues a debit locally so a delivery is never blocked by a metering call; this
drains that queue to signal-MCP out of band, and refreshes the local plan MIRROR from every
response so CH5's wall reads a warm, correct local copy.

OUTCOME → ACTION, and each is a deliberate choice:

  CHARGED / ALREADY_CHARGED  stamp sent_at, refresh mirror (source='debit'). ALREADY_CHARGED is a
                             SUCCESS: the server is telling us this exact delivery was already
                             billed, which is the guard working, not a fault.
  REFUSED                    stamp sent_at, refresh mirror. The wall is a business decision, not a
                             transport failure — retrying it forever would be a queue that never
                             drains. Refreshing the mirror here is HOW THE WALL ARMS.
  INDETERMINATE / transport  leave PENDING, attempts += 1, backoff. Never stamp: we do not know
                             whether the charge landed, and stamping would silently forgive it.
  unlinked / 404             stamp sent_at with a terminal reason. A real terminal state, recorded
                             — never a silent drop. Since OPS-VALIDATE-KEY-INDETERMINATE-W1 CH2 a
                             404 is unambiguous: a Stripe outage answers 503 (INDETERMINATE, stays
                             pending) and a `past_due` customer answers 200 and CHARGES. Before
                             that, all three shared one 404 and a dunning customer's debits were
                             stamped terminal forever — 1,987 of them, uncharged, in nine days.

Fail-soft throughout: this runs on a cron, and a metering fault must never wall a paying customer
or crash the drain.
"""
from __future__ import annotations

from typing import Any, Final, TypedDict

import logging
import os
from datetime import datetime, timedelta, timezone

from .db import Database, DEFAULT_DB_PATH
from .notices import WALL_FOLLOWUP_3D, WALL_FOLLOWUP_7D, WallFollowupKind
from .entitlement_client import consume, read_state
from .link_validator import KeyCheck, validate_api_key
from .quota import PAID_TIERS, PLAN_MIRROR_STALE_AFTER
from .ladder_client import fetch_ladder

log = logging.getLogger(__name__)

BATCH_LIMIT = 200
#: Give up after this many attempts. The row keeps `last_error` and is counted in the digest, so an
#: abandoned debit is VISIBLE — an invisible one would be revenue quietly lost.
#: Backoff is the CRON CADENCE itself: the drain runs every 5 minutes and retries a pending row on
#: each pass, so attempt N is ~5N minutes after the first. No timestamp bookkeeping, and no
#: separate backoff helper to drift out of sync with the schedule that actually governs it.
MAX_ATTEMPTS = 8
#: A mirror older than this is INDETERMINATE to CH5's wall, which SERVES on it (never wall on a
#: measurement we could not take). The poll below exists to keep mirrors inside this window.
#: IMPORTED, not redeclared: `quota.PLAN_MIRROR_STALE_AFTER` is the SoT, because the drainer's
#: refresh cadence and the wall's trust window are the SAME number wearing two names, and two
#: names is how they drift apart.
STALENESS = PLAN_MIRROR_STALE_AFTER

# ── OPS-BOT-LINKED-TIER-REFRESH-W1 CH3 — the link lifecycle ──────────────────────────────
#
#: How long a link must be CONTINUOUSLY, DETERMINEDLY invalid before it is torn down.
#: Architect-overridable by editing this line.
#:
#: 72h, and the reasoning is an asymmetry, not a preference. Stripe dunning retries run over
#: days, so a card that failed once and recovered must not cost the customer their tier. A
#: WRONG downgrade walls a paying customer; a LATE downgrade serves a lapsed one a few days
#: longer. Only one of those is worth avoiding.
LINK_INVALID_GRACE = timedelta(hours=72)

#: How many DETERMINED-invalid observations must fall inside that window before a teardown.
#:
#: OPS-VALIDATE-KEY-INDETERMINATE-W1 CH3 introduced this, and it is the safety half of making
#: INDETERMINATE HOLD instead of RESET. With a reset, an outage wiped the clock — which is what
#: livelocked the teardown for 12.5 measured days. With a plain hold and nothing else, the
#: opposite hazard appears: one determined-invalid, a week-long outage, one more
#: determined-invalid, and `elapsed >= 72h` would tear down a customer we observed as invalid
#: exactly twice.
#:
#: So elapsed time is necessary and no longer sufficient. The drain runs 10x/hour, so a genuinely
#: lapsed subscriber accumulates ~720 determined negatives across the 72h window; 24 is ~2.4h of
#: sustained invalidity — trivially cleared by a real lapse, unreachable by the outage shape.
#: Neither condition alone can tear down a link.
MIN_INVALID_OBSERVATIONS = 24

#: 3d's notice. RATIFIED BY THE ARCHITECT 2026-08-21, copy approved as-is and verified
#: byte-identical to the approved string before this flip.
#:
#: 🛑 THE POLARITY IS INVERTED FROM THE ONE THIS WAVE SHIPPED, DELIBERATELY. While the copy was
#: PENDING-MR1 the gate was fail-CLOSED (only a literal "1" opened it), because an unset env var
#: must never mean "ship unratified copy to paying customers". Ratification removes that risk and
#: introduces the opposite one: a flag that must be SET on every host to get approved behaviour
#: is a safeguard that is off by accident on the next host, the next rebuild, or the next
#: `/etc/algovault-bot/env` restore — nothing tracks it, and it fails silently in the direction
#: of not telling a downgraded customer why their tier changed. So the ratified behaviour is now
#: the CODE's default and depends on no host state at all.
#:
#: What remains is a KILL SWITCH: the literal "0" disables the send. Any other value, including
#: unset and including the "1" the pre-ratification hosts may carry, enables it — so the flip is
#: backwards-compatible in the safe direction.
DOWNGRADE_NOTICE_KILL_SWITCH = "ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED"


#: GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH2 R8 — the wall-follow-up cadence.
#:
#: (offset_days, LEDGER KIND, CAMPAIGN TAG). Three columns because they answer three different
#: questions and collapsing any two would lose one: the offset is WHEN, the kind is WHAT WE TOLD
#: THIS CHAT (a `notice_ledger` row and a log event), and the campaign is WHICH CTA CONVERTED (a
#: `signup_attribution` value the funnel reads). The kind/campaign asymmetry — `wall_followup_3d`
#: vs `quota_followup_3d` — is deliberate and this tuple is the ONE place they are paired.
#:
#: A FOURTH TOUCH IS A ROW HERE. That is the wave's generator-level claim: a paid-lane cadence, a
#: downgrade follow-up, or a re-engagement for the never-engaged blockers costs a tuple entry,
#: not a scheduler and not a migration.
WALL_FOLLOWUPS: Final[tuple[tuple[int, WallFollowupKind, str], ...]] = (
    (3, WALL_FOLLOWUP_3D, "quota_followup_3d"),
    (7, WALL_FOLLOWUP_7D, "quota_followup_7d"),
)

#: Per-cycle send cap. The drain runs ~10x/hour, so a backlog drains within the hour while a
#: single cycle can never blast the whole cohort — the blast-radius bound on a sender that DMs
#: real people.
WALL_FOLLOWUP_MAX_PER_CYCLE: Final = 20

#: 🛑 DEFAULT **OFF**, and this is the one flag in this module that inverts its neighbour's
#: polarity. `monitoring-and-recovery.md` (TG-WATCH-ADOPTION-BROADCAST-W1): a user-facing sender
#: ships behind ONE default-OFF go-live flag, because the flip is both the enable and the instant
#: rollback. The downgrade notice above defaults ON because it was ratified and soaked long
#: before its switch existed; this one has never sent a message to anybody. Only the literal "1"
#: enables it — an unset, empty, "true" or "yes" value all mean OFF, so a typo cannot arm a
#: sender by accident. That is the OPPOSITE of the downgrade switch's tolerance, deliberately:
#: there the safe direction is keep sending, here it is stay silent.
WALL_FOLLOWUPS_FLAG = "ALGOVAULT_WALL_FOLLOWUPS_ENABLED"


class FollowupRow(TypedDict):
    """One row of the dry-run table — the artifact a human approves before the flag is flipped.

    A TypedDict rather than `dict[str, object]` so the renderer cannot silently start printing a
    field that is not there: the table IS the go-live evidence, and a KeyError at the moment the
    operator asks for it is the worst possible time to find out.
    """

    chat_id: int
    days_since_wall: float
    due_kinds: list[str]
    would_send: str | None
    would_supersede: list[str]
    skip_reason: str | None


def _wall_followups_enabled() -> bool:
    """Default OFF. Only the literal "1" arms the sender."""
    return os.environ.get(WALL_FOLLOWUPS_FLAG, "").strip() == "1"


def _downgrade_notice_enabled() -> bool:
    """Default ON. Only the literal "0" turns the ratified notice off."""
    return os.environ.get(DOWNGRADE_NOTICE_KILL_SWITCH, "").strip() != "0"


def _parse_stamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _send_downgrade_notice(
    chat_id: int, lang_code: str | None, db_path: str, db: Database
) -> bool:
    """REFUSES, never throws. A notice failure may not take the poll loop down.

    Imported lazily: `broadcast` pulls in the Telegram stack, and the drainer must stay
    importable (and testable) on a box that has no bot token.
    """
    try:
        from .broadcast import sendDM
        from .notices import compose_downgrade
        from .quota import resolve_ladder

        # GROWTH-TG-QUOTA-PARITY-W1 CH3: the notice states the ladder this chat is returning TO,
        # rendered from the mirror rather than the two literals it used to carry in three
        # languages. Lazy import for the same reason `broadcast` is: this module must stay
        # importable on a box with no bot token.
        #
        # V2 CH2 R7c — and the reactivation link is now a BUTTON. `sendDM` has always accepted
        # `reply_markup`; nothing here passed one, so the one message in this bot that exists to
        # win a customer back shipped its CTA as a scheme-less domain in prose. That is the tap
        # that failed for the operator on 2026-09-07 and dispatched this wave.
        lad = resolve_ladder(db)
        notice = compose_downgrade(lad.free_monthly, lad.free_daily, lang_code)
        return bool(
            sendDM(
                chat_id,
                notice.text,
                db_path=db_path,
                reply_markup=notice.markup,
            )
        )
    except Exception as err:  # noqa: BLE001 — a notice fault is never fatal to the drain
        log.warning(
            '{"event": "link_downgrade_notice_failed", "chat_id": %d, "err": "%s"}',
            chat_id,
            str(err)[:200],
        )
        return False


def _apply_link_observation(
    db: Database,
    sub: Any,
    check: KeyCheck,
    corroborated: bool,
    counts: dict[str, int],
    now: datetime,
    db_path: str,
) -> bool:
    """Advance one subscriber's link lifecycle by ONE observation.

    Returns True if the chat is still linked afterwards (so the caller may go on to poll
    its mirror), False if it was just torn down.

    🛑 BUILD RULE 5 LIVES HERE. Every transition that could REDUCE entitlement fires only on
    a DETERMINED negative. An INDETERMINATE keeps current state and does not advance the
    grace counter — it is not evidence of anything.
    """
    chat_id = int(sub["chat_id"])

    # ── 🛑 THE LIVELOCK THIS BRANCH USED TO BE. OPS-VALIDATE-KEY-INDETERMINATE-W1 CH3. ─────────
    #
    # This function's own docstring has always said "An INDETERMINATE keeps current state and
    # does not advance the grace counter — it is not evidence of anything." The CODE did the
    # opposite: it RESET the streak and cleared `link_invalid_since`. Resetting is not keeping.
    # It credits the subscriber with a determined VALID on the strength of a measurement we
    # failed to take.
    #
    # And an INDETERMINATE is a GLOBAL signal, not a per-subscriber one: when signal-MCP blips,
    # every linked chat resets in the SAME pass. So the 72h grace could only elapse if the
    # server stayed continuously reachable for 72h.
    #
    # MEASURED over the entire retained drain log — 3,014 passes across 12.5 days, 2026-08-23
    # to 2026-09-04:
    #     downgraded > 0        in 0 passes        (never, not once)
    #     uncorroborated > 0    in 0 passes        (corroboration was never the blocker)
    #     reset events          9                  (indeterminate > 0)
    #     longest clean run     578 passes = 57.8h
    #     needed for teardown   720 passes = 72.0h
    # The teardown was UNREACHABLE for the whole observed record. Two determinedly-invalid
    # subscribers sat in that limbo indefinitely, served and unmetered.
    #
    # The fix is to make the code do what the docstring says: HOLD.
    if check.status == "INDETERMINATE":
        counts["indeterminate"] += 1
        log.info(
            '{"event": "link_revalidate_indeterminate", "chat_id": %d, "reason": "%s", '
            '"streak_held_at": %d, "note": "not evidence — streak neither advanced nor reset"}',
            chat_id,
            check.reason,
            int(sub["link_invalid_streak"] or 0),
        )
        return True

    if check.status == "DUNNING":
        # A determined POSITIVE: they hold a subscription and Stripe is retrying their card.
        # This ENDS a streak, because it is real evidence the link is live — and it is evidence
        # we could not previously obtain, since a `past_due` customer answered a bare 404 that
        # was indistinguishable from a cancellation.
        #
        # 🛑 NO SECOND GRACE TIMER. Stripe's dunning window IS the grace period, it is already
        # delivered to us as `customer.subscription.updated`, and when Stripe gives up the
        # subscription moves to `unpaid`/`canceled` — which arrives here as a determined INVALID
        # and starts the streak for real. `LINK_INVALID_GRACE` was a reinvention of exactly this,
        # and the reinvention is what livelocked.
        counts["dunning"] += 1
        log.warning(
            '{"event": "link_dunning", "chat_id": %d, "tier": "%s", '
            '"note": "past_due — link HELD and deliveries METERED; Stripe still collecting"}',
            chat_id,
            check.tier or "unknown",
        )
        if int(sub["link_invalid_streak"] or 0) or sub["link_invalid_since"]:
            db.reset_link_invalid_streak(chat_id)
        return True

    if check.status != "INVALID":
        # VALID. A determined positive ends the streak. A VALID whose tier moved needs no
        # special case — the mirror refresh below carries the new tier, CH2 renders it, and a
        # customer who just upgraded does not need a bot message about it.
        if int(sub["link_invalid_streak"] or 0) or sub["link_invalid_since"]:
            db.reset_link_invalid_streak(chat_id)
        return True

    # ── determined INVALID ───────────────────────────────────────────────────────────────
    #
    # COHORT CORROBORATION, and this is the guard the wave's own spec did not have.
    #
    # MEASURED 2026-08-21: signal-MCP's `validateApiKey` distinguishes indeterminacy — it
    # returns `{valid:false, indeterminate:true}` when Stripe is unconfigured or unreachable
    # — but its HTTP route DROPS that flag and answers a bare `404 {"valid":false}` for both
    # "no active subscription" and "we could not ask Stripe". From here the two are
    # indistinguishable, so a Stripe outage, or one lost STRIPE_SECRET_KEY, would present as
    # every linked subscriber being determinedly invalid at once and would tear down the
    # entire paid base after 72h.
    #
    # N simultaneous cancellations is not a thing that happens; an outage is. So the streak
    # advances only when at least one OTHER linked subscriber validated VALID in the SAME
    # pass — proof that the validator itself is answering truthfully right now.
    #
    # A cohort with no VALID member yields no corroboration and therefore no downgrade, ever.
    # That is the fail-safe direction (Build Rule 5), but it is also a mechanism that can sit
    # inert, so it is logged at WARNING rather than absorbed. Retiring the ambiguity at its
    # source is OPS-VALIDATE-KEY-INDETERMINATE-W{NEXT}, after which this guard becomes
    # redundant defence rather than the only defence.
    if not corroborated:
        counts["uncorroborated"] += 1
        log.warning(
            '{"event": "link_invalid_uncorroborated", "chat_id": %d, "reason": "%s", '
            '"note": "no other linked subscriber validated in this pass — treating as '
            'INDETERMINATE, streak NOT advanced"}',
            chat_id,
            check.reason,
        )
        return True

    streak, since_raw = db.advance_link_invalid_streak(chat_id)
    counts["key_invalid"] += 1
    since = _parse_stamp(since_raw)
    elapsed = (now - since) if since else timedelta(0)
    log.info(
        '{"event": "link_invalid_observed", "chat_id": %d, "reason": "%s", "streak": %d, '
        '"elapsed_h": %.1f, "grace_h": %.1f}',
        chat_id,
        check.reason,
        streak,
        elapsed.total_seconds() / 3600.0,
        LINK_INVALID_GRACE.total_seconds() / 3600.0,
    )

    # BOTH conditions, never either alone. See MIN_INVALID_OBSERVATIONS for why elapsed time
    # stopped being sufficient the moment INDETERMINATE started holding instead of resetting.
    if elapsed < LINK_INVALID_GRACE or streak < MIN_INVALID_OBSERVATIONS:
        return True

    # ── sustained past the grace window: notify, then tear the link down ─────────────────
    if _downgrade_notice_enabled():
        if _send_downgrade_notice(chat_id, sub["lang_code"], db_path, db):
            db.mark_link_downgrade_notified(chat_id)
    else:
        log.warning(
            '{"event": "link_downgrade_notice_suppressed", "chat_id": %d, '
            '"reason": "kill switch %s=0 — this subscriber was downgraded WITHOUT being told"}',
            chat_id,
            DOWNGRADE_NOTICE_KILL_SWITCH,
        )

    db.unlink_subscriber(chat_id)
    counts["downgraded"] += 1
    log.warning(
        '{"event": "link_downgraded", "chat_id": %d, "reason": "%s", "streak": %d, '
        '"elapsed_h": %.1f}',
        chat_id,
        check.reason,
        streak,
        elapsed.total_seconds() / 3600.0,
    )
    return False


def _is_stale(as_of: str | None, now: datetime) -> bool:
    if not as_of:
        return True  # never observed
    try:
        raw = as_of.replace(" ", "T")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - dt) > STALENESS


def drain_entitlement_debits(
    db_path: str | None = None, dry_run: bool = False
) -> dict[str, int]:
    """Drain pending debits, then warm idle mirrors. Returns a counter dict for the log."""
    db = Database(db_path or DEFAULT_DB_PATH)
    counts = {
        "pending": 0, "charged": 0, "already": 0, "refused": 0,
        "retry": 0, "abandoned": 0, "unlinked": 0, "polled": 0,
        # CH3 — the link lifecycle. `revalidated` is the DENOMINATOR: without it a run of
        # zeroes is indistinguishable from a loop that never executed.
        "revalidated": 0, "key_invalid": 0, "indeterminate": 0,
        "uncorroborated": 0, "downgraded": 0,
        # CH3 — the dunning cohort, counted so "served but not paying" is never invisible again.
        "dunning": 0,
    }
    now = datetime.now(timezone.utc)

    for row in db.pending_entitlement_debits(BATCH_LIMIT):
        counts["pending"] += 1
        api_key = row["linked_api_key"] if "linked_api_key" in row.keys() else None
        tier = row["linked_tier"] if "linked_tier" in row.keys() else None

        # Resolved at SEND time, never from the queue row: an unlinked subscriber's queued debits
        # must not charge a revoked key.
        if not api_key or tier not in PAID_TIERS:
            counts["unlinked"] += 1
            if not dry_run:
                db.mark_entitlement_debit_sent(row["id"], last_error="unlinked")
            continue

        if row["attempts"] >= MAX_ATTEMPTS:
            counts["abandoned"] += 1
            if not dry_run:
                db.mark_entitlement_debit_sent(row["id"], last_error=f"abandoned after {row['attempts']} attempts")
            continue

        if dry_run:
            continue

        resp = consume(
            api_key=api_key,
            channel=row["channel"],
            units=int(row["units"]),
            idempotency_key=row["idem_key"],
            kind=row["kind"],
        )

        if resp is None:
            counts["retry"] += 1
            db.bump_entitlement_debit_attempt(row["id"], "transport")
            continue
        if "_http_status" in resp:
            status = resp["_http_status"]
            if status == 404:
                # The key no longer validates — terminal for this row, recorded not dropped.
                counts["unlinked"] += 1
                db.mark_entitlement_debit_sent(row["id"], last_error="key_invalid_404")
            else:
                counts["retry"] += 1
                db.bump_entitlement_debit_attempt(row["id"], f"http_{status}")
            continue

        outcome = str(resp.get("outcome", ""))
        if outcome in ("CHARGED", "ALREADY_CHARGED"):
            counts["charged" if outcome == "CHARGED" else "already"] += 1
            db.mark_entitlement_debit_sent(row["id"], last_error=None)
            db.update_plan_mirror(row["chat_id"], resp, source="debit")
        elif outcome == "REFUSED":
            # The wall. Stamp it — a refusal is settled, not pending — and refresh the mirror,
            # which is what arms CH5's local wall for the next delivery.
            counts["refused"] += 1
            db.mark_entitlement_debit_sent(row["id"], last_error="REFUSED")
            db.update_plan_mirror(row["chat_id"], resp, source="debit")
        else:
            # INDETERMINATE, or an outcome we do not recognise. Stay pending: we do NOT know
            # whether the charge landed, and stamping would silently forgive an unknown.
            counts["retry"] += 1
            db.bump_entitlement_debit_attempt(row["id"], outcome or "unknown_outcome")

    # ── revalidate every link, then keep idle mirrors warm ───────────────────
    #
    # OPS-BOT-LINKED-TIER-REFRESH-W1 CH3a — revalidation runs on THIS poll. No new schedule
    # and no new cron: the drainer already visits every linked subscriber here.
    #
    # 🛑 IT SITS ABOVE THE STALENESS FILTER, DELIBERATELY. The mirror-warming below
    # `continue`s on a FRESH mirror, so folding revalidation in underneath it would mean a
    # subscriber whose mirror is fresh — i.e. an ACTIVE one, taking alerts — is never
    # re-asked at all, which is exactly the population the lifecycle is for. The cost of
    # hoisting it is one loopback call per linked subscriber per pass; the linked population
    # is single-digit and `validate-key` is loopback, not a Caddy round-trip.
    #
    # The whole cohort is validated FIRST, then judged, because the corroboration guard in
    # `_apply_link_observation` needs to know whether ANY key validated in this pass before
    # it may act on any single failure.
    if not dry_run:
        subs = list(db.paid_linked_chat_ids())
        checks: dict[int, KeyCheck] = {}
        for sub in subs:
            checks[int(sub["chat_id"])] = validate_api_key(sub["linked_api_key"] or "")
            counts["revalidated"] += 1
        # CH3 — `corroborates` is VALID **or DUNNING**: both are positive determinations that
        # required Stripe to answer. Before this wave a dunning customer answered a bare 404 and
        # counted as INVALID, so a cohort where every paying customer was mid-dunning offered no
        # corroboration at all — the guard was weakest exactly when the population needed it.
        corroborated = any(c.corroborates for c in checks.values())

        for sub in subs:
            chat_id = int(sub["chat_id"])
            still_linked = _apply_link_observation(
                db, sub, checks[chat_id], corroborated, counts, now, db.path
            )
            if not still_linked:
                # Torn down this pass — there is no key left to poll a mirror with.
                continue

            # A subscriber taking no alerts still needs a fresh mirror: it is what re-opens
            # a wall after the server's period resets, and what keeps CH5 out of its
            # INDETERMINATE branch.
            if sub["linked_tier"] not in PAID_TIERS:
                continue
            if not _is_stale(sub["plan_state_as_of"], now):
                continue
            state = read_state(sub["linked_api_key"], "bot")
            if not state or "_http_status" in state:
                continue
            db.update_plan_mirror(chat_id, state, source="poll")
            counts["polled"] += 1

    # GROWTH-TG-QUOTA-PARITY-W1 CH2a — refresh the LADDER mirror on the same pass.
    #
    # Deliberately here and not on a new schedule: this job already runs every five minutes and
    # already exists to keep server-published state warm. A dedicated cron for a value that moves
    # maybe monthly would be a second thing to install, monitor and forget.
    #
    # LAST, and unconditionally: it must not be able to abort the debit drain above it, and it must
    # still run on a pass where every subscriber was skipped. Failure is a WARNING and a kept
    # mirror — `fetch_ladder` never raises, and a ladder we could not read is never a reason to
    # refuse anyone.
    counts["ladder_fetched"] = 0
    if not dry_run:
        ladder = fetch_ladder()
        if ladder is not None:
            db.upsert_free_tier_ladder(
                free_monthly=ladder["free_monthly"],
                free_daily=ladder["free_daily"],
                starter_price_usd=ladder["starter_price_usd"],
                starter_monthly_calls=ladder["starter_monthly_calls"],
                fetched_at=now.isoformat(),
                # GROWTH-TG-PLAN-PICKER-W1 R2 — the rest of the four-SKU ladder. `.get` rather
                # than `[...]`: absence must write NULL (which `resolve_ladder` reads as "serve
                # the pinned constant"), never raise inside the drain and take the debit pass
                # above it down with it.
                starter_daily_calls=ladder.get("starter_daily_calls"),
                starter_price_usd_6month=ladder.get("starter_price_usd_6month"),
                pro_price_usd=ladder.get("pro_price_usd"),
                pro_monthly_calls=ladder.get("pro_monthly_calls"),
                pro_daily_calls=ladder.get("pro_daily_calls"),
                pro_price_usd_6month=ladder.get("pro_price_usd_6month"),
            )
            counts["ladder_fetched"] = 1

    # V2 CH2 R8 — the wall-follow-up pass, on the drain's EXISTING schedule. No new cron, no
    # second scheduler: a cadence that needs its own timer is a cadence nobody can reason about
    # alongside the one already running. It REFUSES rather than throws — a follow-up fault must
    # never take the debit drain above it down.
    try:
        followup_counts, followup_table = wall_followup_pass(
            db, db_path or DEFAULT_DB_PATH, now=now, dry_run=dry_run
        )
        counts.update(followup_counts)
        if dry_run:
            _print_followup_table(followup_table)
    except Exception as err:  # noqa: BLE001
        log.warning('{"event": "wall_followup_pass_failed", "err": "%s"}', str(err)[:200])
        if dry_run:
            print("WALL_FOLLOWUP_DRYRUN_VERDICT=INDETERMINATE")

    log.info('{"event": "entitlement_drain", %s}' % ", ".join(f'"{k}": {v}' for k, v in counts.items()))
    return counts


def _print_followup_table(table: list[FollowupRow]) -> None:
    """The table a human reads BEFORE the flag is flipped. Printed at column 0, on stdout.

    🛑 THIS IS THE GO-LIVE ARTIFACT, which is why it prints rather than logs. CH3's acceptance
    is that the operator reads this and compares it to what the first live cycle then does; a
    cadence approved from a count alone would be approved without anyone seeing WHO it reaches.

    The verdict token is terminal and gated on by the caller, never by the exit code.
    """
    print("WALL_FOLLOWUP_DRYRUN_TABLE")
    print("chat_id | days_since_wall | due_kinds | would_send | would_supersede | skip_reason")
    for e in table:
        print(
            f"{e['chat_id']} | {e['days_since_wall']} | "
            f"{','.join(e['due_kinds']) or '-'} | "
            f"{e['would_send'] or '-'} | "
            f"{','.join(e['would_supersede']) or '-'} | "
            f"{e['skip_reason'] or '-'}"
        )
    print(f"WALL_FOLLOWUP_DRYRUN_ROWS={len(table)}")
    print("WALL_FOLLOWUP_DRYRUN_VERDICT=PASS")


def wall_followup_pass(
    db: Database, db_path: str, *, now: datetime, dry_run: bool = False
) -> tuple[dict[str, int], list[FollowupRow]]:
    """The wall-follow-up cadence — d0 (the wall, elsewhere) then +3d, +7d. CH2 R8.

    Declared in `quota.REFUSAL_LANES` as the `followup` lane, and the seam gate enforces the two
    rules that make that shape safe: this function MUST call `record_notice` inside the branch
    guarded by the send result, and MUST NOT call `refuse_and_notify` — a follow-up is not a
    refusal and must never re-fire the d0 wall's stamps.

    WHY IT EXISTS. A free user walled on the monthly meter was told once and then heard nothing
    for up to 27 days, while the only surface that has ever converted is one they have to come
    back to on their own. Blocks are measurably NOT caused by the wall — 0 of 26 blocked users
    had ever been walled — so a bounded cadence is safe to add; what has to be managed is
    nagging, and it is, five ways: capped per cycle, idempotent per episode, superseded rather
    than doubled, silent once the user pays or unwatches everything, and default-OFF.

    Returns `(counts, table)`. The TABLE is what a human reads before the flag is flipped, and
    it is produced on both paths — a dry run that reported nothing would leave the operator
    approving a cadence they could not see.
    """
    from .broadcast import sendDM
    from .quota import evaluate_delivery

    counts = {
        # 🛑 THE DENOMINATOR FIRST. A run of zeroes with no candidate count is indistinguishable
        # from a pass that never executed — this module's own rule, and the reason `revalidated`
        # exists one loop up.
        "wall_followup_candidates": 0,
        "wall_followup_3d": 0,
        "wall_followup_7d": 0,
        "wall_followup_superseded": 0,
        "wall_followup_skipped": 0,
        "wall_followup_flag_off": 0,
    }
    table: list[FollowupRow] = []
    enabled = _wall_followups_enabled()

    # 🛑 BOUND AS AN ISO STRING WITH ITS OFFSET, NEVER `datetime('now','-3 days')`.
    # `quota_100_last_fired_at` is written from Python's `.isoformat()` — T-separated, `+00:00`
    # — while the house SQL idiom in `db.py` is SQLite's space-separated, offset-less
    # `datetime()`. Comparing the two lexicographically drops every chat whose wall DATE equals
    # the boundary date, which is EXACTLY the set that has just become due: measured on the live
    # cohort it cut candidates from 7 to 5, silently and in one direction, turning a 3-day
    # cadence into a 4-day one for whoever it hit.
    cutoff = (now - timedelta(days=WALL_FOLLOWUPS[0][0])).isoformat()

    with db._cursor() as cur:
        cur.execute(
            "SELECT chat_id, lang_code, quota_100_last_fired_at, alerts_window_start "
            "FROM subscribers "
            "WHERE bot_blocked_at IS NULL "
            "  AND linked_api_key IS NULL "
            "  AND quota_100_last_fired_at IS NOT NULL "
            "  AND alerts_window_start IS NOT NULL "
            "  AND quota_100_last_fired_at >= alerts_window_start "
            "  AND quota_100_last_fired_at <= ? "
            "ORDER BY quota_100_last_fired_at ASC",
            (cutoff,),
        )
        rows = cur.fetchall()

    sent_this_cycle = 0
    for row in rows:
        chat_id = int(row["chat_id"])
        walled_at = _parse_stamp(row["quota_100_last_fired_at"])
        if walled_at is None:
            continue
        counts["wall_followup_candidates"] += 1
        days_since = (now - walled_at).total_seconds() / 86400.0
        entry: FollowupRow = {
            "chat_id": chat_id,
            "days_since_wall": round(days_since, 2),
            "due_kinds": [],
            "would_send": None,
            "would_supersede": [],
            "skip_reason": None,
        }
        table.append(entry)

        # THE decision, through the one derivation. `persist_roll=not dry_run`: this call rolls
        # an expired window and WRITES that roll, so a dry run would mutate the very column the
        # table is reporting on — and `episode_key` is that column.
        d = evaluate_delivery(db, chat_id, persist_roll=not dry_run)
        if d.allowed or d.limit_kind != "monthly" or d.state.is_paid:
            entry["skip_reason"] = (
                "paid" if d.state.is_paid
                else "not_walled" if d.allowed
                else f"limit_kind={d.limit_kind}"
            )
            counts["wall_followup_skipped"] += 1
            continue

        # Nothing to resume => a follow-up is a nag, not a service.
        if db.count_watches(chat_id) + db.count_scan_watches(chat_id) == 0:
            entry["skip_reason"] = "no_watches"
            counts["wall_followup_skipped"] += 1
            continue

        # `window_start` is guaranteed non-None by `limit_kind == "monthly"` above — NOT by the
        # SQL, which reads `alerts_window_start` before `get_quota_state` may roll it away.
        episode_key = d.state.window_start.isoformat() if d.state.window_start else None
        if episode_key is None:
            entry["skip_reason"] = "no_episode_key"
            counts["wall_followup_skipped"] += 1
            continue

        due = [
            (offset, kind, campaign)
            for offset, kind, campaign in WALL_FOLLOWUPS
            if days_since >= offset and not db.has_notice(chat_id, kind, episode_key)
        ]
        if not due:
            entry["skip_reason"] = "already_told"
            counts["wall_followup_skipped"] += 1
            continue
        entry["due_kinds"] = [k for _, k, _ in due]

        # 🛑 SEND ONLY THE LATEST; RECORD THE EARLIER AS SUPERSEDED. A chat first seen at day 9
        # is due for both touches at once, and sending two messages in one cycle is the nagging
        # this cadence is bounded to avoid. 'superseded' is a real ledger status, not a silent
        # skip: it records that we CHOSE not to send, so the next cycle cannot deliver the
        # earlier touch late and the digest can tell "we decided" from "we never got there".
        *earlier, latest = due
        entry["would_send"] = latest[1]
        entry["would_supersede"] = [k for _, k, _ in earlier]

        if not enabled:
            counts["wall_followup_flag_off"] += 1
            entry["skip_reason"] = "flag_off"
            continue
        if dry_run:
            continue
        if sent_this_cycle >= WALL_FOLLOWUP_MAX_PER_CYCLE:
            entry["skip_reason"] = "cycle_cap"
            counts["wall_followup_skipped"] += 1
            continue

        offset, kind, campaign = latest
        try:
            notice = _compose_followup_notice(
                db, d, chat_id, row["lang_code"], days_since, kind
            )
            delivered = bool(
                sendDM(chat_id, notice.text, db_path=db_path, reply_markup=notice.markup)
            )
        except Exception as err:  # noqa: BLE001 — one bad chat may not take the cycle down
            log.warning(
                '{"event": "wall_followup_failed", "chat_id": %d, "kind": "%s", "err": "%s"}',
                chat_id, kind, str(err)[:200],
            )
            entry["skip_reason"] = "send_error"
            counts["wall_followup_skipped"] += 1
            continue

        if delivered:
            # Stamp ONLY after a delivered send — the wall's own discipline. A blocked or
            # rate-limited subscriber must not burn their one touch of the episode.
            # (`broadcast._send_with_retry` already marks a blocked chat; adding a second
            # `mark_subscriber_blocked` here would be a second derivation of one fact.)
            db.record_notice(chat_id, kind, episode_key, "sent", campaign, now.isoformat())
            for _, earlier_kind, earlier_campaign in earlier:
                db.record_notice(
                    chat_id, earlier_kind, episode_key, "superseded",
                    earlier_campaign, now.isoformat(),
                )
                counts["wall_followup_superseded"] += 1
            db.increment_total_ctas_shown(chat_id)
            counts[kind] += 1
            sent_this_cycle += 1
            # WARNING, not INFO: this log is read from the file the crontab redirects, and the
            # CH3 gate greps for this exact event. A counts-dict key alone is invisible to it.
            log.warning(
                '{"event": "wall_followup_sent", "chat_id": %d, "kind": "%s", '
                '"episode_key": "%s", "days_since_wall": %.2f}',
                chat_id, kind, episode_key, days_since,
            )
        else:
            entry["skip_reason"] = "not_delivered"
            counts["wall_followup_skipped"] += 1

    if not enabled:
        log.warning(
            '{"event": "wall_followup_flag_off", "candidates": %d, "would_send": %d}',
            counts["wall_followup_candidates"], counts["wall_followup_flag_off"],
        )
    return counts, table


def _compose_followup_notice(
    db: Database, decision, chat_id: int, lang, days_since: float, kind: WallFollowupKind
):
    """Compose the notice for a kind the CALLER already chose.

    🛑 `kind` IS PASSED, NOT RE-DERIVED. The first draft picked it back out of `WALL_FOLLOWUPS`
    by comparing `days_since` again — a second derivation of a decision the supersede rule had
    just made, and the two would disagree the moment that rule changed. It is the same defect
    class as the copy layer re-deciding which wall was hit, one screen up in `quota.py`. mypy
    caught it as a Literal mismatch, which is the shape working as intended.
    """
    from .notices import compose_followup
    from .quota import resolve_ladder

    return compose_followup(
        kind,
        decision.state,
        resolve_ladder(db),
        db.get_acquisition_source(chat_id),
        lang,
        n_watches=db.count_watches(chat_id) + db.count_scan_watches(chat_id),
        days_since_wall=int(days_since),
    )
