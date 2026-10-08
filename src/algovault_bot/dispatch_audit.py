"""Read-only dispatch-timing auditor — OPS-CLOSEDBAR-DISPATCH-OFFSET-INCIDENT-W2 R2.

THE GUARD JUDGES THE PRODUCER'S RECORD, NEVER A COPY OF ITS SCHEDULE.

The hourly liveness probe (`closedbar-w1-liveness.sh`, signal-MCP repo) used to re-derive this
bot's dispatch schedule in bash — `offset + grace + j·60` — and judge `last_fetched_at` against
that private copy. The producer has designed reasons to service a due row on a LATER tick (a
budget or deadline deferral, a bounded fetch retry, an exception retry, a skipped exhausted
owner, a first fetch) and the copy modelled none of them, so every one of them paged as a fault.
Six fix commits went into that copy; the seventh page (2026-10-07) was a 4h-boundary budget
deferral that the copy read as "the offset value is wrong".

So the dispatcher now writes its own account — `dispatch_ledger`, one disposition per due row
per tick — and this module classifies the observable stamp against that account. It holds no
schedule arithmetic: every due-time comes from the ledger (written by the producer through
`dispatch_schedule`) or from `dispatch_schedule`'s own functions under the producer's env.

READ-ONLY, structurally:
  * SQLite opened `file:…?mode=ro` with `PRAGMA query_only = ON`, inside one read transaction.
    `Database(...)` is never constructed — its `_init_schema` writes and `_enforce_mode_660`
    chmods.
  * Run as root (the probe's cron is root's), it first reads the three dispatch knobs from the
    bot's env file, then DROPS to `state.db`'s owner before SQLite touches the database. A
    read-only open of a WAL database creates `-wal`/`-shm` when they are absent and cannot
    delete them afterwards (measured, upstream SQLite 3.50.4); dropping first makes any such file
    the bot's own instead of relying on SQLite's root-chown.

CLI:
  python -B -m algovault_bot.dispatch_audit --db PATH --tf all|<tf> [--now EPOCH]
      [--deploy-epoch EPOCH] [--env-file PATH] [--allowance 30] [--all-rows]
  python -B -m algovault_bot.dispatch_audit --print-verdicts

Line protocol (stable, prefix-keyed; the probe greps it):
  AUDIT_SCOPE …
  AUDIT_CONFIG OFFSET_PCT=<literal>→<effective> GRACE_MIN=… JITTER_WINDOW_MIN=… rejected=<…> skew=<n>
  AUDIT_ROW <class> cause=<c> chronic=<0|1> chat=<id> last4=<l4> <coin>/<tf>/<exch> bar=<B>
      due=<d> first_tick=<f> tick=<T> fired=<x> lag0=<s> spanned=<n> late_ticks=<k> exec=<s>
      verdict=<V|none>
  AUDIT_SUMMARY tf=<tf> bar=<B> judged=… on_time=… explained=… fetch_failed=… errored=…
      deferred=… skipped=… first=… offenders=… not_yet_due=… realigning=…
  AUDIT_REASON=<insufficient|schema_missing|crash>   (only when relevant)
  CRON_MINUTE_HINT=<…> · AUDIT_ERROR <…>              (only when relevant)
  DISPATCH_AUDIT_VERDICT=<OK|TIMING_FAULT|DEPLOY_REGRESSION|CHRONIC_LATE|INDETERMINATE>
Exit 0 OK · 1 TIMING_FAULT / DEPLOY_REGRESSION / CHRONIC_LATE · 3 INDETERMINATE. Any exception
is INDETERMINATE with `AUDIT_REASON=crash` — never a missing token.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Final, Literal, Sequence, get_args
from unittest import mock

from . import dispatch_schedule as ds
from .db import (
    DISPOSITIONS,
    MAX_FETCH_ATTEMPTS_PER_BUCKET,
    STAMPING_DISPOSITIONS,
    Disposition,
    _iso_to_epoch,
)
from .validators import TF_SECONDS

Verdict = Literal["OK", "TIMING_FAULT", "DEPLOY_REGRESSION", "CHRONIC_LATE", "INDETERMINATE"]
VERDICTS: Final[tuple[str, ...]] = get_args(Verdict)

RowClass = Literal[
    "ON_TIME",
    "LATE_EXPLAINED",
    "GAVE_UP",
    "LATE_UNEXPLAINED",
    "EARLY",
    "LATENCY_BREACH",
    "MISSED",
    "ERRORING",
    "UNSERVICED_SKIPPED",
    "PENDING_DEFERRED",
    "NOT_YET_DUE",
    "REALIGNING",
    "NEW_ROW",
    "UNPROVENANCED",
    "STAMP_MISMATCH",
]
ROW_CLASSES: Final[tuple[str, ...]] = get_args(RowClass)

# Execution allowance, measured from the TICK that serviced the row (its `run_cycle` start) to
# the stamp it wrote. The old probe measured from BAR OPEN, so the timer's `:10` phase silently
# ate 10 s of its 30. Measured 2026-10-07: normal ticks stamp 1–8 s after the tick (15m rows
# 11–18 s into the bar minus the 10 s phase); the worst excursion on record (2026-10-05 00–03Z,
# 2026-10-07 02–03Z) ran whole ticks in ≤ 24.4 s. 30 s holds every measured tick with ~5 s
# headroom; beyond it alerts are genuinely late, which pages CHRONIC_LATE (R6) — never widen it.
LATENCY_ALLOWANCE_SECONDS: Final = 30
# A row late for a fetch failure, an exception or a give-up in this many CONSECUTIVE serviced
# buckets is degradation, not a transient (R6).
CHRONIC_LATE_BARS: Final = 3
# Bars after the provenance epoch (the later of the deploy and the ledger's first write), and
# buckets after a recorded change of a row's shift, that are not judged: their first bucket may
# have been acted on before the record existed, or under the previous schedule.
REALIGN_BARS: Final = 2
# `AccuracySec=1s` plus interpreter start: how much later than the `:10` phase a tick's
# `now_epoch` may legitimately read.
TIMER_SLACK_SECONDS: Final = 3
TICK_SECONDS: Final = ds.TICK_SECONDS
# A stamp this young may belong to a tick whose single ledger write has not landed yet.
IN_FLIGHT_SECONDS: Final = 2 * TICK_SECONDS

KNOBS: Final[tuple[str, ...]] = (ds.ENV_OFFSET_PCT, ds.ENV_CLOSE_GRACE_MIN, ds.ENV_JITTER_WINDOW_MIN)
KNOB_LABELS: Final[dict[str, str]] = {
    ds.ENV_OFFSET_PCT: "OFFSET_PCT",
    ds.ENV_CLOSE_GRACE_MIN: "GRACE_MIN",
    ds.ENV_JITTER_WINDOW_MIN: "JITTER_WINDOW_MIN",
}

# R2.5 — both maps are keyed by the Literals and a test asserts the key sets are EXACTLY the
# Literal's members: a disposition or class cannot be added without a routing decision.
DISPOSITION_TO_CAUSE: Final[dict[Disposition, str | None]] = {
    "serviced": None,
    "serviced_first": "first",
    "fetch_failed": "fetch_failed",
    "gave_up": "gave_up",
    "errored": "errored",
    "deferred_budget": "deferred",
    "deferred_deadline": "deferred",
    "skipped_exhausted": "skipped",
}
CLASS_TO_VERDICT: Final[dict[RowClass, Verdict | None]] = {
    "ON_TIME": "OK",
    "LATE_EXPLAINED": "OK",
    "GAVE_UP": "OK",
    "UNSERVICED_SKIPPED": "OK",
    "PENDING_DEFERRED": "OK",
    "LATE_UNEXPLAINED": "TIMING_FAULT",
    "EARLY": "TIMING_FAULT",
    "MISSED": "TIMING_FAULT",
    "LATENCY_BREACH": "CHRONIC_LATE",
    "ERRORING": "CHRONIC_LATE",
    "UNPROVENANCED": "DEPLOY_REGRESSION",
    "STAMP_MISMATCH": "DEPLOY_REGRESSION",
    "NOT_YET_DUE": None,
    "REALIGNING": None,
    "NEW_ROW": None,
}
# The same map read by a plain string: a disposition read back from the DB is a `str`.
_CAUSE_BY_NAME: Final[dict[str, str | None]] = {str(k): v for k, v in DISPOSITION_TO_CAUSE.items()}
VERDICT_PRECEDENCE: Final[tuple[Verdict, ...]] = (
    "DEPLOY_REGRESSION",
    "TIMING_FAULT",
    "CHRONIC_LATE",
    "OK",
)
EXIT_CODE: Final[dict[str, int]] = {
    "OK": 0,
    "TIMING_FAULT": 1,
    "DEPLOY_REGRESSION": 1,
    "CHRONIC_LATE": 1,
    "INDETERMINATE": 3,
}
# Causes that make a late row DEGRADATION when they recur (R6); deferral and skips have owners
# of their own (the fetch-budget saturation alarm; the quota wall) and never page here (R5).
CHRONIC_CAUSES: Final = frozenset({"fetch_failed", "errored", "gave_up"})
# Classes printed by default. ON_TIME and the steady-state skips are counted, not listed: the
# probe log has no rotation, and 180 lines an hour would bury the rows that matter.
QUIET_CLASSES: Final = frozenset({"ON_TIME", "NOT_YET_DUE", "REALIGNING", "UNSERVICED_SKIPPED"})

RowKey = tuple[int, str, str, str]


@dataclass(frozen=True)
class Event:
    disposition: str
    bucket_epoch: int
    due_epoch: int
    first_tick: int
    last_tick: int
    n: int
    fired_epoch: int | None


@dataclass(frozen=True)
class WatchState:
    chat_id: int
    coin: str
    timeframe: str
    exchange: str
    added_epoch: int | None
    stamp_epoch: int | None

    @property
    def key(self) -> RowKey:
        return (self.chat_id, self.coin, self.timeframe, self.exchange)


@dataclass
class Finding:
    row: WatchState
    bar: int
    cls: RowClass
    cause: str
    chronic: bool = False
    due: int | None = None
    first_tick: int | None = None
    tick: int | None = None
    fired: int | None = None
    lag0: int | None = None
    spanned: int | None = None
    late_ticks: int = 0
    exec_s: int | None = None

    @property
    def verdict(self) -> Verdict | None:
        if self.chronic:
            return "CHRONIC_LATE"
        return CLASS_TO_VERDICT[self.cls]


@dataclass(frozen=True)
class KnobConfig:
    read: bool
    literals: dict[str, str]
    effective: dict[str, int]
    rejected: tuple[str, ...]


@dataclass
class AuditResult:
    verdict: Verdict
    reason: str | None
    lines: list[str]
    findings: list[Finding]


# ── the three knobs, read with systemd EnvironmentFile semantics ─────────────────────────────


def parse_env_knobs(text: str) -> dict[str, str]:
    """The three dispatch knobs as systemd's `EnvironmentFile=` hands them to the producer.

    Only `#`/`;` at the START of a line is a comment — there are NO inline comments, so
    `KEY=0 # note` reaches the bot as the value `0 # note`, which its parser rejects and silently
    replaces with the default. Surrounding whitespace is stripped; a value wrapped in matching
    quotes is unquoted; the last assignment wins. Every other key is skipped without being kept:
    the file also holds the bot token and the bypass key.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in KNOBS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def read_env_knobs(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_env_knobs(fh.read())


class _KnobEnv:
    """The producer's environment for the three knobs, applied to THIS process while the
    `dispatch_schedule` getters run, and restored afterwards (a patched copy, never a guess)."""

    def __init__(self, literals: dict[str, str] | None) -> None:
        self._literals = literals
        self._patch: Any = None

    def __enter__(self) -> _KnobEnv:
        if self._literals is not None:
            self._patch = mock.patch.dict(os.environ)
            self._patch.start()
            for key in KNOBS:
                if key in self._literals:
                    os.environ[key] = self._literals[key]
                else:
                    os.environ.pop(key, None)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._patch is not None:
            self._patch.stop()


def evaluate_config(literals: dict[str, str] | None) -> KnobConfig:
    """CONFIG_REJECTED is the one TRUE meaning of "the offset value is wrong": a literal that
    is present but rejected by the producer's own parser, so the producer is silently on its
    default. Evaluated through `dispatch_schedule`'s getters, never re-parsed here."""
    with _KnobEnv(literals):
        effective = {
            ds.ENV_OFFSET_PCT: ds.dispatch_offset_pct(),
            ds.ENV_CLOSE_GRACE_MIN: ds.close_grace_min(),
            ds.ENV_JITTER_WINDOW_MIN: ds.jitter_window_min(),
        }
    if literals is None:
        return KnobConfig(read=False, literals={}, effective=effective, rejected=())
    rejected = []
    for key in KNOBS:
        if key not in literals:
            continue
        try:
            accepted = int(literals[key]) == effective[key]
        except ValueError:
            accepted = False
        if not accepted:
            rejected.append(KNOB_LABELS[key])
    return KnobConfig(read=True, literals=dict(literals), effective=effective, rejected=tuple(rejected))


# ── read-only access ─────────────────────────────────────────────────────────────────────────


def drop_privileges_to_owner(db_path: str) -> int | None:
    """As root, become `db_path`'s owner before SQLite opens it, so a `-wal`/`-shm` that a
    read-only open creates belongs to the bot. Returns the uid now in effect, or None when the
    process was not root (nothing to do)."""
    if os.geteuid() != 0:
        return None
    st = os.stat(db_path)
    if st.st_uid == 0:
        return 0
    os.setgroups([])
    os.setgid(st.st_gid)
    os.setuid(st.st_uid)
    if os.geteuid() != st.st_uid:
        raise RuntimeError(f"privilege drop to uid {st.st_uid} did not take effect")
    return st.st_uid


def open_read_only(db_path: str) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Belt and braces: a write through this connection raises even if the URI flag were lost.
    conn.execute("PRAGMA query_only = ON")
    return conn


def _has_ledger(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatch_ledger'"
    ).fetchone()
    return row is not None


def _load_watches(conn: sqlite3.Connection) -> list[WatchState]:
    rows = conn.execute(
        "SELECT chat_id, coin, timeframe, exchange, last_fetched_at, added_at FROM watchlists"
    ).fetchall()
    return [
        WatchState(
            chat_id=int(r["chat_id"]),
            coin=str(r["coin"]),
            timeframe=str(r["timeframe"]),
            exchange=str(r["exchange"]),
            added_epoch=_iso_to_epoch(r["added_at"]),
            stamp_epoch=_iso_to_epoch(r["last_fetched_at"]),
        )
        for r in rows
    ]


def _load_events(conn: sqlite3.Connection) -> dict[RowKey, dict[int, list[Event]]]:
    out: dict[RowKey, dict[int, list[Event]]] = defaultdict(lambda: defaultdict(list))
    for r in conn.execute(
        "SELECT chat_id, coin, timeframe, exchange, bucket_epoch, disposition, due_epoch,"
        " first_tick, last_tick, n, fired_epoch FROM dispatch_ledger"
    ):
        key = (int(r["chat_id"]), str(r["coin"]), str(r["timeframe"]), str(r["exchange"]))
        out[key][int(r["bucket_epoch"])].append(
            Event(
                disposition=str(r["disposition"]),
                bucket_epoch=int(r["bucket_epoch"]),
                due_epoch=int(r["due_epoch"]),
                first_tick=int(r["first_tick"]),
                last_tick=int(r["last_tick"]),
                n=int(r["n"]),
                fired_epoch=None if r["fired_epoch"] is None else int(r["fired_epoch"]),
            )
        )
    return out


# ── classification ───────────────────────────────────────────────────────────────────────────


def _cause_of(events: Sequence[Event]) -> str:
    causes = {c for c in (_CAUSE_BY_NAME.get(e.disposition) for e in events) if c is not None}
    if not causes:
        return "none"
    return causes.pop() if len(causes) == 1 else "mixed"


def _bucket_is_chronic_late(events: Sequence[Event]) -> bool:
    """A serviced bucket that was late for a REASON of degradation (R6)."""
    for e in events:
        if e.disposition in ("fetch_failed", "errored", "gave_up"):
            return True
    return False


def _shifts(events_by_bucket: dict[int, list[Event]], lo: int, hi: int) -> set[int]:
    return {
        e.due_epoch - e.bucket_epoch
        for b, evs in events_by_bucket.items()
        if lo <= b <= hi
        for e in evs
    }


def classify_row(
    row: WatchState,
    bar: int,
    period: int,
    events_by_bucket: dict[int, list[Event]],
    *,
    now: int,
    provenance: int | None,
    env_shift: int,
    allowance: int,
) -> Finding:
    """Exactly one class for `row` in the bucket that opened at `bar`."""
    ev_b = events_by_bucket.get(bar, [])
    f = Finding(row=row, bar=bar, cls="ON_TIME", cause="none")

    # 1. stamp integrity — the observable stamp must be one the dispatcher RECORDED writing.
    stamped = [
        e for evs in events_by_bucket.values() for e in evs
        if e.disposition in STAMPING_DISPOSITIONS and e.fired_epoch is not None
        and (row.added_epoch is None or e.first_tick >= row.added_epoch)
    ]
    latest_fired = max((e.fired_epoch for e in stamped if e.fired_epoch is not None), default=None)
    stamp = row.stamp_epoch
    if provenance is not None:
        if stamp is not None and now - stamp >= IN_FLIGHT_SECONDS and stamp >= provenance:
            if latest_fired is None or stamp > latest_fired:
                f.cls, f.cause = "UNPROVENANCED", "stamp_without_event"
                return f
            if stamp < latest_fired:
                f.cls, f.cause = "STAMP_MISMATCH", "stamp_rewound"
                return f
        if stamp is None and latest_fired is not None and now - latest_fired >= IN_FLIGHT_SECONDS:
            f.cls, f.cause = "STAMP_MISMATCH", "stamp_cleared"
            return f

    # 2. bucket determinism — `is_due` fires a row at most once per bucket, on aligned buckets.
    #    A second stamping event in one bucket is the ratchet's signature in ledger terms (Q1).
    if any(b % period for b in events_by_bucket):
        f.cls, f.cause = "STAMP_MISMATCH", "misaligned_bucket"
        return f
    stamping_b = [e for e in ev_b if e.disposition in STAMPING_DISPOSITIONS]
    if len(stamping_b) > 1 or any(e.n > 1 for e in stamping_b):
        f.cls, f.cause = "STAMP_MISMATCH", "double_service"
        return f

    # 3. rows the bar cannot judge.
    if row.added_epoch is not None and row.added_epoch > bar:
        f.cls, f.cause = "NEW_ROW", "added_in_bar"
        return f
    if provenance is not None and bar < (provenance // period) * period + REALIGN_BARS * period:
        f.cls, f.cause = "REALIGNING", "provenance"
        return f
    if len(_shifts(events_by_bucket, bar - REALIGN_BARS * period, bar)) > 1:
        # The row's recorded shift changed inside the window: a dispatch-config change. Its
        # first buckets under the new shift can come due anywhere; judged again after.
        f.cls, f.cause = "REALIGNING", "shift_change"
        return f

    if not ev_b:
        # The row's own most recent recorded shift is the producer's account of its schedule;
        # the env-derived shift is the fallback for a row the ledger has never seen.
        latest = max(events_by_bucket) if events_by_bucket else None
        shift = (
            env_shift if latest is None
            else events_by_bucket[latest][0].due_epoch - events_by_bucket[latest][0].bucket_epoch
        )
        due = bar + shift
        f.due = due
        if due + MAX_FETCH_ATTEMPTS_PER_BUCKET * TICK_SECONDS > now:
            f.cls, f.cause = "NOT_YET_DUE", "before_retry_budget"
        else:
            f.cls, f.cause = "MISSED", "no_event"
        return f

    unknown = sorted({e.disposition for e in ev_b if e.disposition not in DISPOSITIONS})
    if unknown:
        # R2.5: a value nothing routes is NOISE, never explained.
        f.cls, f.cause = "LATE_UNEXPLAINED", "unknown_disposition:" + ",".join(unknown)
        return f

    nonstamp = [e for e in ev_b if e.disposition not in STAMPING_DISPOSITIONS]
    k = sum(e.n for e in nonstamp)
    first = min(e.first_tick for e in ev_b)
    due = min(e.due_epoch for e in ev_b)
    f.due, f.first_tick, f.late_ticks, f.lag0 = due, first, k, first - due

    if not stamping_b:
        kinds = {e.disposition for e in nonstamp}
        errored = sum(e.n for e in nonstamp if e.disposition == "errored")
        if kinds == {"skipped_exhausted"}:
            f.cls, f.cause = "UNSERVICED_SKIPPED", "skipped"
        elif kinds <= {"deferred_budget", "deferred_deadline", "skipped_exhausted"}:
            f.cls, f.cause = "PENDING_DEFERRED", "deferred"
        elif errored >= MAX_FETCH_ATTEMPTS_PER_BUCKET:
            f.cls, f.cause = "ERRORING", "errored"
        elif (
            "fetch_failed" in kinds
            and MAX_FETCH_ATTEMPTS_PER_BUCKET * TICK_SECONDS > period
            and any(b > bar for b in events_by_bucket)
        ):
            # A timeframe whose retry budget spans more than one period (1m): the bucket was
            # superseded mid-retry. Explained, never MISSED (R7).
            f.cls, f.cause = "LATE_EXPLAINED", "fetch_failed"
        elif due + MAX_FETCH_ATTEMPTS_PER_BUCKET * TICK_SECONDS > now:
            f.cls, f.cause = "NOT_YET_DUE", "in_retry"
        else:
            f.cls, f.cause = "LATE_UNEXPLAINED", "unserviced"
        return f

    s = stamping_b[0]
    f.tick, f.fired = s.first_tick, s.fired_epoch
    f.spanned = round((s.first_tick - first) / TICK_SECONDS)
    f.exec_s = None if s.fired_epoch is None else s.fired_epoch - s.first_tick
    if s.disposition == "serviced_first":
        f.cls, f.cause = "NEW_ROW", "first"
        return f
    if f.lag0 is not None and f.lag0 < 0:
        f.cls, f.cause = "EARLY", "before_due"
        return f
    if f.lag0 is not None and f.lag0 > TICK_SECONDS + TIMER_SLACK_SECONDS:
        f.cls, f.cause = "LATE_UNEXPLAINED", "first_tick_late"
        return f
    if f.spanned != k:
        # Every tick between the first event and the service must be accounted for, and the
        # account may not over-count: a tick that acted on nothing is unexplained.
        f.cls, f.cause = "LATE_UNEXPLAINED", ("tick_gap" if f.spanned > k else "overcount")
        return f
    if f.exec_s is None:
        f.cls, f.cause = "STAMP_MISMATCH", "fired_missing"
        return f
    if f.exec_s < 0 or f.exec_s > allowance:
        f.cls, f.cause = "LATENCY_BREACH", "exec"
        return f

    cause = _cause_of(nonstamp)
    if s.disposition == "gave_up":
        f.cls, f.cause = "GAVE_UP", (cause if cause != "none" else "gave_up")
    elif k == 0:
        f.cls, f.cause = "ON_TIME", "none"
    else:
        f.cls, f.cause = "LATE_EXPLAINED", cause

    if f.cls in ("LATE_EXPLAINED", "GAVE_UP") and _bucket_is_chronic_late(ev_b):
        serviced = sorted(
            (b for b, evs in events_by_bucket.items()
             if b <= bar and any(e.disposition in STAMPING_DISPOSITIONS for e in evs)),
            reverse=True,
        )[:CHRONIC_LATE_BARS]
        f.chronic = len(serviced) == CHRONIC_LATE_BARS and all(
            _bucket_is_chronic_late(events_by_bucket[b]) for b in serviced
        )
    return f


# ── the audit ────────────────────────────────────────────────────────────────────────────────


def _fmt(v: int | None) -> str:
    return "-" if v is None else str(v)


def render_row(f: Finding) -> str:
    r = f.row
    return (
        f"AUDIT_ROW {f.cls} cause={f.cause} chronic={int(f.chronic)} chat={r.chat_id} "
        f"last4={str(r.chat_id)[-4:]} {r.coin}/{r.timeframe}/{r.exchange} bar={f.bar} "
        f"due={_fmt(f.due)} first_tick={_fmt(f.first_tick)} tick={_fmt(f.tick)} "
        f"fired={_fmt(f.fired)} lag0={_fmt(f.lag0)} spanned={_fmt(f.spanned)} "
        f"late_ticks={f.late_ticks} exec={_fmt(f.exec_s)} verdict={f.verdict or 'none'}"
    )


def audited_bars(now: int, tf_scope: str) -> list[tuple[str, int, int]]:
    """R7 — every timeframe whose bar opened at HH:00 of the current UTC hour."""
    hour = now - now % 3600
    tfs = sorted(TF_SECONDS, key=lambda t: TF_SECONDS[t]) if tf_scope == "all" else [tf_scope]
    out = []
    for tf in tfs:
        period = TF_SECONDS[tf]
        if hour % period == 0:
            out.append((tf, period, hour))
    return out


def audit(
    conn: sqlite3.Connection,
    *,
    now: int,
    tf_scope: str = "all",
    deploy_epoch: int | None = None,
    env_literals: dict[str, str] | None = None,
    allowance: int = LATENCY_ALLOWANCE_SECONDS,
    all_rows: bool = False,
) -> AuditResult:
    if tf_scope != "all" and tf_scope not in TF_SECONDS:
        raise ValueError(f"unknown --tf {tf_scope!r}")
    config = evaluate_config(env_literals)
    lines: list[str] = []
    contributions: list[Verdict] = []
    reason: str | None = None

    conn.execute("BEGIN")
    try:
        ledger = _has_ledger(conn)
        watches = _load_watches(conn)
        events = _load_events(conn) if ledger else {}
        ledger_start_row = (
            conn.execute("SELECT MIN(first_tick) FROM dispatch_ledger").fetchone() if ledger else None
        )
    finally:
        conn.execute("COMMIT")
    ledger_start = None if ledger_start_row is None or ledger_start_row[0] is None else int(
        ledger_start_row[0]
    )
    starts = [e for e in (deploy_epoch, ledger_start) if e is not None]
    provenance = max(starts) if starts else None
    bars = audited_bars(now, tf_scope)
    n_events = sum(len(evs) for by_b in events.values() for evs in by_b.values())

    lines.append(
        f"AUDIT_SCOPE now={now} hour={now - now % 3600} tfs={','.join(tf for tf, _, _ in bars) or '-'} "
        f"ledger_events={n_events} ledger_start={_fmt(ledger_start)} deploy={_fmt(deploy_epoch)} "
        f"provenance={_fmt(provenance)} euid={os.geteuid()}"
    )

    findings: list[Finding] = []
    skew = 0
    if not ledger:
        reason = "schema_missing"
        contributions.append("DEPLOY_REGRESSION")
    else:
        with _KnobEnv(env_literals):
            for tf, period, bar in bars:
                rows = [w for w in watches if w.timeframe == tf]
                if not rows:
                    continue
                tf_findings = []
                for w in rows:
                    env_shift = ds.due_instant(
                        tf, now, w.chat_id, w.coin, w.exchange
                    ) - ds.target_epoch(tf, now, w.chat_id, w.coin, w.exchange)
                    fnd = classify_row(
                        w, bar, period, events.get(w.key, {}),
                        now=now, provenance=provenance, env_shift=env_shift, allowance=allowance,
                    )
                    if (
                        config.read
                        and fnd.cls in ("ON_TIME", "LATE_EXPLAINED", "GAVE_UP", "LATENCY_BREACH")
                        and fnd.due is not None
                        and fnd.due - bar != env_shift
                    ):
                        skew += 1
                    tf_findings.append(fnd)
                findings.extend(tf_findings)
                judged = [x for x in tf_findings if x.verdict is not None]
                missed = [x for x in judged if x.cls == "MISSED"]
                if judged and len(missed) == len(judged):
                    # No event for ANY due row: the dispatcher is dark or its writer is absent.
                    contributions.append("DEPLOY_REGRESSION")
                causes = Counter(x.cause for x in tf_findings)
                classes = Counter(x.cls for x in tf_findings)
                lines.extend(
                    render_row(x) for x in tf_findings
                    if all_rows or x.cls not in QUIET_CLASSES or x.chronic
                )
                lines.append(
                    f"AUDIT_SUMMARY tf={tf} bar={bar} judged={len(judged)} "
                    f"on_time={classes['ON_TIME']} "
                    f"explained={classes['LATE_EXPLAINED'] + classes['GAVE_UP']} "
                    f"fetch_failed={causes['fetch_failed']} errored={causes['errored']} "
                    f"deferred={causes['deferred']} skipped={causes['skipped']} "
                    f"first={causes['first'] + causes['added_in_bar']} "
                    f"offenders={sum(1 for x in judged if x.verdict != 'OK')} "
                    f"not_yet_due={classes['NOT_YET_DUE']} realigning={classes['REALIGNING']}"
                )

    if config.read:
        parts = []
        for key in KNOBS:
            literal = config.literals.get(key, "<unset>")
            parts.append(f"{KNOB_LABELS[key]}={literal}→{config.effective[key]}")
        lines.insert(
            1,
            "AUDIT_CONFIG " + " ".join(parts)
            + f" rejected={','.join(config.rejected) or 'none'} skew={skew}",
        )
        if config.rejected or skew:
            contributions.append("TIMING_FAULT")
    else:
        lines.insert(1, "AUDIT_CONFIG skipped=no_env_file")

    contributions.extend(f.verdict for f in findings if f.verdict is not None)
    judged_total = sum(1 for f in findings if f.verdict is not None)
    verdict: Verdict = "INDETERMINATE"
    for v in VERDICT_PRECEDENCE:
        if v in contributions:
            verdict = v
            break
    if verdict == "OK" and judged_total == 0:
        verdict = "INDETERMINATE"
    if verdict == "INDETERMINATE":
        reason = reason or "insufficient"
    if reason:
        lines.append(f"AUDIT_REASON={reason}")
    if reason == "insufficient":
        minute = (now % 3600) // 60
        lines.append(
            f"CRON_MINUTE_HINT=ran at minute {minute:02d}; judged rows exist only after each "
            f"HH:00 bar's due-time + {MAX_FETCH_ATTEMPTS_PER_BUCKET} ticks has elapsed"
        )
    lines.append(f"DISPATCH_AUDIT_VERDICT={verdict}")
    return AuditResult(verdict=verdict, reason=reason, lines=lines, findings=findings)


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m algovault_bot.dispatch_audit")
    p.add_argument("--db")
    p.add_argument("--tf", default="all")
    p.add_argument("--now", type=int)
    p.add_argument("--deploy-epoch", type=int)
    p.add_argument("--env-file")
    p.add_argument("--allowance", type=int, default=LATENCY_ALLOWANCE_SECONDS)
    p.add_argument("--all-rows", action="store_true")
    p.add_argument("--print-verdicts", action="store_true")
    return p


def _crash(err: BaseException | str) -> int:
    text = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
    print("AUDIT_REASON=crash")
    print("AUDIT_ERROR " + " ".join(str(text).split())[:300])
    print("DISPATCH_AUDIT_VERDICT=INDETERMINATE", flush=True)
    return EXIT_CODE["INDETERMINATE"]


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        if e.code in (0, None):
            raise
        return _crash(f"argument error (exit {e.code})")
    if args.print_verdicts:
        for v in VERDICTS:
            print(v)
        return 0
    try:
        if not args.db:
            return _crash("--db is required")
        literals = read_env_knobs(args.env_file) if args.env_file else None
        drop_privileges_to_owner(args.db)
        conn = open_read_only(args.db)
        try:
            result = audit(
                conn,
                now=int(time.time()) if args.now is None else args.now,
                tf_scope=args.tf,
                deploy_epoch=args.deploy_epoch,
                env_literals=literals,
                allowance=args.allowance,
                all_rows=args.all_rows,
            )
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — a crash is a verdict, never a missing token
        return _crash(e)
    for line in result.lines:
        print(line)
    sys.stdout.flush()
    return EXIT_CODE[result.verdict]


if __name__ == "__main__":
    sys.exit(main())
