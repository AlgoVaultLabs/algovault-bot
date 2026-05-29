"""TG-BATCH-WATCHLIST-W1 C2 — global fetch-rate budget + fair-share scheduler.

C1 made watchlists uncapped. Without a budget, one user's `all all all` at the
1m TF ≈ 3,550 fetches/min would melt CPX22 (2 vCPU) and trip exchange rate
limits. This module caps server load STRUCTURALLY, independent of watchlist
size — the generator-level invariant of the wave (decouple watchlist size from
fetch load):

  1. **Skip-exhausted** — drop `calls`-type rows whose owner's monthly quota is
     exhausted (they cannot receive a trade call until reset/upgrade, so
     fetching is pure waste). `regime` (free) and `both` rows are kept.
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
DEFAULT_FETCH_BUDGET_PER_MIN: int = 30
DEFAULT_FETCH_TICK_DEADLINE_SEC: int = 45
# Consecutive ticks with deferred>0 before the operator-action signal fires.
DEFAULT_SATURATION_TICKS: int = 5


class SchedulableRow(Protocol):
    chat_id: int
    timeframe: str
    alert_type: str


def fetch_budget_per_min() -> int:
    try:
        return max(1, int(os.environ.get("FETCH_BUDGET_PER_MIN", DEFAULT_FETCH_BUDGET_PER_MIN)))
    except (TypeError, ValueError):
        return DEFAULT_FETCH_BUDGET_PER_MIN


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
    state: dict, deferred: int, threshold: int
) -> tuple[dict, bool]:
    """Pure detector for the sustained-deferred operator signal.

    Increments a consecutive-deferred counter while ``deferred > 0``; resets it
    on a clean tick (``deferred == 0``). Returns ``(new_state, should_alert)``;
    ``should_alert`` is True the tick the counter first reaches ``threshold``,
    and the counter resets after firing so it alerts ONCE per sustained episode
    (the 24h send-cooldown is additionally enforced by send_telegram.sh).
    """
    consecutive = (state.get("consecutive", 0) + 1) if deferred > 0 else 0
    should_alert = consecutive >= threshold
    return ({"consecutive": 0 if should_alert else consecutive}, should_alert)
