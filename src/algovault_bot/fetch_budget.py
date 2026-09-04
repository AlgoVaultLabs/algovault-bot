"""TG-BATCH-WATCHLIST-W1 C2 — global fetch-rate budget + fair-share scheduler.

C1 made watchlists uncapped. Without a budget, one user's `all all all` at the
1m TF ≈ 3,550 fetches/min would melt CPX22 (2 vCPU) and trip exchange rate
limits. This module caps server load STRUCTURALLY, independent of watchlist
size — the generator-level invariant of the wave (decouple watchlist size from
fetch load):

  1. **Skip-exhausted** — drop `calls`-type rows whose owner's monthly quota is
     exhausted (they cannot receive a trade call until reset/upgrade, so
     fetching is pure waste). `regime` and `both` rows are kept — regime
     alerts are still delivered (they now count toward quota per
     QUOTA-CONSISTENCY-COUNT-ALL-W1, but are not exhaustion-gated).
  2. **Budget + fair-share** — process at most ``FETCH_BUDGET_PER_MIN`` rows per
     tick, **round-robin across users** (no single user can consume the whole
     budget), and **TF-priority within a user** (rarer/higher TFs first; 1m
     last). Rows not scheduled this tick are simply NOT marked fetched → they
     remain due and are picked up next tick. This guarantees
     ``fetches/min ≤ FETCH_BUDGET_PER_MIN`` for ALL watchlist sizes.

The wall-clock ``FETCH_TICK_DEADLINE_SEC`` guard is enforced engine-side (a
latency spike defers the remaining scheduled rows so a tick never overruns the
60s cron interval).

``schedule()`` is pure / I/O-free (quota state injected as a predicate) and
fully unit-testable. ``update_saturation_state()`` is the pure detector for the
sustained-deferred operator-action signal (the actual severity-gated, cooldown'd
Telegram send is delegated to ``/opt/algovault-monitoring/send_telegram.sh`` —
consumers MUST NOT re-implement those gates).
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from .validators import TF_SECONDS

# Ratified by the Plan-Mode capacity probe (2026-05-29): p50 1.4s / p95 7.7s
# per signal-MCP call, sequential engine, 60s tick → ~30 rows is the safe
# sequential ceiling. Env-overridable (no redeploy to tune).
# OPS-BOT-DISPATCH-LATENCY-W1 CH3 — 30 was the SEQUENTIAL ceiling and is now stale twice over.
# It was ratified 2026-05-29 against a 2-vCPU CPX22 holding EIGHT watchlist rows; the box became
# an 8-vCPU CPX42 on 2026-06-05 (OPS-CPX42-RESIZE-W1) six days later, and the loop is no longer
# sequential. Replaying the original audit's own derivation at today's measured per-row cost
# sizes it at 67-101 rows, and it binds on only ~2% of ticks — every one of them at the hour
# boundary, which is exactly where the alert that should have reported it could not fire.
#
# 150 is chosen against the WORST CASE, not today's load: 105 live rows, a measured max of 51
# eligible in any tick across 40 days, and CH4's jitter collapse putting up to 104 rows on one
# tick. It leaves ~40% watchlist headroom before deferral resumes, and it is still a real cap —
# the point of the budget was never throughput, it is a bound on what one tick can ask of the
# venue layer.
DEFAULT_FETCH_BUDGET_PER_MIN: int = 150
DEFAULT_FETCH_TICK_DEADLINE_SEC: int = 45
# How many rows may be in flight at once. Rows are sharded by chat_id, so this bounds distinct
# SUBSCRIBERS in flight, never two rows of the same one. 8 == the box's vCPU count, and the
# work per row is one blocking HTTP call offloaded to a thread, so the threads are I/O-bound
# and the ceiling is the upstream's tolerance rather than the CPU's.
DEFAULT_FETCH_CONCURRENCY: int = 8
# Consecutive ticks with deferred>0 before the operator-action signal fires.
DEFAULT_SATURATION_TICKS: int = 5

# OPS-BOT-DISPATCH-LATENCY-W1 CH1 — THE CONSECUTIVE ARM ALONE IS A DARK GUARD.
#
# `DEFAULT_SATURATION_TICKS = 5` asks for five BACK-TO-BACK deferred ticks. Real budget
# pressure here is not shaped like that: it is boundary-clustered. Rows collapse onto the
# minutes after a shared bar boundary, the budget drains them over 3-4 ticks, and then 56
# minutes are clean. Measured over the full 40-day journal the longest run is 3-4 and the
# alarm has NEVER fired — while the budget was genuinely binding on 26 ticks in 26 hours.
#
# So the guard was not merely mis-tuned; it was asking the wrong question. Lowering the
# threshold to 3 would be the lane fix and would make it fire on a single ordinary boundary.
# The quantity that means "an operator should look" is RECURRENCE: a burst that keeps coming
# back every hour is chronic saturation, and a burst that never ends is a stall. Those are two
# different failures and the guard now has an arm for each:
#
#   arm A (kept) — `consecutive >= saturation_ticks`      : one unbroken run = a stall.
#   arm B (new)  — `episodes >= saturation_episodes`      : repeated bursts = chronic pressure.
#                  within `saturation_window_seconds`
#
# An EPISODE is one maximal run of deferred>0 ticks, counted once at its first tick, so a
# 4-tick drain is one episode and not four.
DEFAULT_SATURATION_EPISODES: int = 3
# 3 hours: at the hourly boundary that is three chances to recur, so a genuinely chronic
# condition alerts within one working morning while a single bad hour stays silent.
DEFAULT_SATURATION_WINDOW_SEC: int = 10_800


class SchedulableRow(Protocol):
    chat_id: int
    timeframe: str
    alert_type: str


def fetch_budget_per_min() -> int:
    try:
        return max(1, int(os.environ.get("FETCH_BUDGET_PER_MIN", DEFAULT_FETCH_BUDGET_PER_MIN)))
    except (TypeError, ValueError):
        return DEFAULT_FETCH_BUDGET_PER_MIN


def fetch_concurrency() -> int:
    """Max rows in flight. Clamped to [1, 32]: 1 restores the pre-CH3 sequential behaviour
    exactly (a working rollback with no redeploy), and the upper bound stops a typo turning
    one tick into a stampede against the venue layer."""
    try:
        return max(1, min(32, int(os.environ.get("FETCH_CONCURRENCY", DEFAULT_FETCH_CONCURRENCY))))
    except (TypeError, ValueError):
        return DEFAULT_FETCH_CONCURRENCY


def tick_deadline_sec() -> float:
    try:
        return max(
            1.0, float(os.environ.get("FETCH_TICK_DEADLINE_SEC", DEFAULT_FETCH_TICK_DEADLINE_SEC))
        )
    except (TypeError, ValueError):
        return float(DEFAULT_FETCH_TICK_DEADLINE_SEC)


def saturation_ticks() -> int:
    try:
        return max(1, int(os.environ.get("FETCH_SATURATION_TICKS", DEFAULT_SATURATION_TICKS)))
    except (TypeError, ValueError):
        return DEFAULT_SATURATION_TICKS


@dataclass
class ScheduleResult:
    scheduled: list  # rows to process this tick (len ≤ budget)
    deferred: list   # eligible rows NOT processed (remain due next tick)
    stats: dict


def _tf_priority(row: SchedulableRow) -> int:
    # Higher TF_SECONDS = rarer/higher TF = scheduled first; 1m (60s) last.
    return TF_SECONDS.get(row.timeframe, 0)


def schedule(
    due_rows: Sequence[SchedulableRow],
    *,
    budget: int,
    is_exhausted: Callable[[int], bool],
) -> ScheduleResult:
    """Select up to ``budget`` rows to fetch this tick.

    Skip-exhausted first, then round-robin across users (fair-share) taking the
    highest-TF row from each user per round. Returns the scheduled subset, the
    deferred remainder (unmodified — same row objects), and per-tick stats.
    """
    # 1. Skip-exhausted: drop pure `calls` rows for exhausted owners.
    eligible: list[SchedulableRow] = []
    skipped_exhausted = 0
    for row in due_rows:
        if row.alert_type == "calls" and is_exhausted(row.chat_id):
            skipped_exhausted += 1
            continue
        eligible.append(row)

    # 2. Bucket by user; TF-priority within each user.
    buckets: "OrderedDict[int, list[SchedulableRow]]" = OrderedDict()
    for row in eligible:
        buckets.setdefault(row.chat_id, []).append(row)
    for cid in buckets:
        buckets[cid].sort(key=_tf_priority, reverse=True)

    # 3. Round-robin across users until the budget is filled.
    scheduled: list[SchedulableRow] = []
    order = list(buckets.keys())
    while len(scheduled) < budget:
        progressed = False
        for cid in order:
            q = buckets[cid]
            if q:
                scheduled.append(q.pop(0))
                progressed = True
                if len(scheduled) >= budget:
                    break
        if not progressed:
            break

    deferred = [r for cid in order for r in buckets[cid]]
    stats = {
        "due": len(due_rows),
        "skipped_exhausted": skipped_exhausted,
        "eligible": len(eligible),
        "processed": len(scheduled),
        "deferred": len(deferred),
        "active_users": len(buckets),
        "budget": budget,
    }
    return ScheduleResult(scheduled=scheduled, deferred=deferred, stats=stats)


def update_saturation_state(
    state: dict,
    deferred: int,
    threshold: int,
    now_epoch: int,
    *,
    episode_threshold: int | None = None,
    window_seconds: int | None = None,
) -> tuple[dict, bool]:
    """Pure detector for the sustained-deferred operator signal. TWO arms.

    ``arm A`` — consecutive: increments while ``deferred > 0``, resets on a clean tick, fires
    at ``threshold``. An unbroken run means the engine is not draining at all.

    ``arm B`` — episodes: counts distinct BURSTS (one maximal deferred run = one episode,
    stamped at its first tick) inside a rolling ``window_seconds``, and fires at
    ``episode_threshold``. Recurrence is the signal arm A structurally cannot see: measured
    over 40 days, real deferral never exceeded a 3-4 tick run, so arm A alone had never fired
    once despite the budget binding 26 times in 26 hours.

    ``now_epoch`` is INJECTED rather than read here — the house idiom (``is_due``,
    ``list_due_watches``) — so the detector stays pure and its window is testable without a
    clock. It is REQUIRED, deliberately: an optional clock would let a caller silently leave
    arm B dark, which is the exact failure mode this change exists to retire.

    Returns ``(new_state, should_alert)``. Both counters clear on fire, so an episode-cluster
    alerts ONCE (the 24h send-cooldown in send_telegram.sh is additional, not a substitute).
    """
    episodes_needed = (
        DEFAULT_SATURATION_EPISODES if episode_threshold is None else episode_threshold
    )
    window = DEFAULT_SATURATION_WINDOW_SEC if window_seconds is None else window_seconds

    prior_consecutive = int(state.get("consecutive", 0) or 0)
    # Tolerate a state file written by the pre-wave shape (no episode list) or a corrupt one.
    raw_starts = state.get("episode_starts")
    starts = [int(t) for t in raw_starts if isinstance(t, (int, float))] if isinstance(raw_starts, list) else []

    if deferred > 0:
        consecutive = prior_consecutive + 1
        # Stamp only the FIRST tick of a run: a 4-tick drain is one episode, not four.
        if prior_consecutive == 0:
            starts.append(now_epoch)
    else:
        consecutive = 0

    starts = [t for t in starts if now_epoch - t < window]

    should_alert = consecutive >= threshold or len(starts) >= episodes_needed
    if should_alert:
        return ({"consecutive": 0, "episode_starts": []}, True)
    return ({"consecutive": consecutive, "episode_starts": starts}, False)


def saturation_episodes() -> int:
    try:
        return max(1, int(os.environ.get("FETCH_SATURATION_EPISODES", DEFAULT_SATURATION_EPISODES)))
    except (TypeError, ValueError):
        return DEFAULT_SATURATION_EPISODES


def saturation_window_seconds() -> int:
    try:
        return max(
            60, int(os.environ.get("FETCH_SATURATION_WINDOW_SEC", DEFAULT_SATURATION_WINDOW_SEC))
        )
    except (TypeError, ValueError):
        return DEFAULT_SATURATION_WINDOW_SEC
