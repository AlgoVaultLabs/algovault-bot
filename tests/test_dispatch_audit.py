"""OPS-CLOSEDBAR-DISPATCH-OFFSET-INCIDENT-W2 — the read-only dispatch-timing auditor.

The measured case first: the page of 2026-10-07 04:11Z judged 14 rows of 15m, 12 serviced at the
first tick (+1…+7 s of execution) and 2 serviced one tick later (+71/+72 s into the bar). The bash
copy of the schedule read that as OFFSET_FAULT. Judged against the dispatcher's own record it is
designed recovery — and the SAME stamps with no recorded reason must still be a fault: the
true-fault direction survives, which is what keeps this from being a blunted guard.

Fixtures are real `Database(...)` files (the producer's own migration) filled by SQL, so every
case exercises the live read path. Ids stay ≤ 7 digits and epochs are computed: this repo is public
and `scripts/check-chat-id-literals.py` refuses any bare 8–16-digit run.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

import pytest

from algovault_bot import dispatch_audit as da
from algovault_bot.db import DISPOSITIONS, Database, Disposition

BAR = int(datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc).timestamp())  # the incident's bar
P15 = 900
NOW = BAR + 11 * 60 + 1  # the probe's :11 run
DEPLOY = BAR - 2 * 86_400
EXEC = [1, 3, 3, 3, 4, 5, 5, 6, 7, 7, 7, 7]  # the page's 11…17 s, less the :10 tick phase
LATE = [(10240, "XAU", "BINANCE", 72), (16131, "XAU", "BINGX", 71)]
REPO = Path(__file__).resolve().parents[1]


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Fixture:
    def __init__(self, path: Path) -> None:
        self.path = str(path)
        Database(self.path)  # the producer's own schema, ledger included

    def watch(self, chat: int, coin: str, exch: str, *, tf: str = "15m", stamp: int | None,
              added: int = BAR - 30 * 86_400) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat,))
            c.execute(
                "INSERT INTO watchlists (chat_id, coin, timeframe, exchange, alert_type,"
                " added_at, last_fetched_at) VALUES (?, ?, ?, ?, 'calls', ?, ?)",
                (chat, coin, tf, exch, _iso(added), None if stamp is None else _iso(stamp)),
            )

    def event(self, chat: int, coin: str, exch: str, bucket: int, disposition: str, *,
              tick: int, n: int = 1, fired: int | None = None, tf: str = "15m",
              shift: int = 0) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute(
                "INSERT INTO dispatch_ledger (chat_id, coin, timeframe, exchange, bucket_epoch,"
                " disposition, due_epoch, first_tick, last_tick, n, fired_epoch)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (chat, coin, tf, exch, bucket, disposition, bucket + shift, tick,
                 tick + 60 * (n - 1), n, fired),
            )

    def on_time(self, chat: int, coin: str, exch: str, bucket: int, exec_s: int = 5) -> int:
        fired = bucket + 10 + exec_s
        self.event(chat, coin, exch, bucket, "serviced", tick=bucket + 10, fired=fired)
        return fired

    def sha(self) -> str:
        return hashlib.sha256(Path(self.path).read_bytes()).hexdigest()


def measured(tmp_path: Path, *, late_cause: str | None = "fetch_failed",
             late_history: str | None = None) -> Fixture:
    """The page's 14 rows. `late_cause` is the recorded reason the 2 late rows were serviced
    one tick late (None = no reason recorded). `late_history` makes their 3 prior buckets late
    for that cause too."""
    fx = Fixture(tmp_path / "state.db")
    history = [BAR - 3 * P15, BAR - 2 * P15, BAR - P15]
    for i, exec_s in enumerate(EXEC):
        chat, coin = 20_000 + i, f"C{i}"
        for b in history:
            fx.on_time(chat, coin, "BINANCE", b)
        fired = fx.on_time(chat, coin, "BINANCE", BAR, exec_s)
        fx.watch(chat, coin, "BINANCE", stamp=fired)
    for chat, coin, exch, off in LATE:
        for b in history:
            if late_history:
                fx.event(chat, coin, exch, b, late_history, tick=b + 10)
                fx.event(chat, coin, exch, b, "serviced", tick=b + 70, fired=b + off)
            else:
                fx.on_time(chat, coin, exch, b)
        if late_cause:
            fx.event(chat, coin, exch, BAR, late_cause, tick=BAR + 10)
        fx.event(chat, coin, exch, BAR, "serviced", tick=BAR + 70, fired=BAR + off)
        fx.watch(chat, coin, exch, stamp=BAR + off)
    return fx


def run(fx: Fixture, **kw: Any) -> da.AuditResult:
    conn = da.open_read_only(fx.path)
    try:
        kw.setdefault("now", NOW)
        kw.setdefault("tf_scope", "15m")
        kw.setdefault("deploy_epoch", DEPLOY)
        kw.setdefault("env_literals", {
            "ALGOVAULT_BOT_DISPATCH_OFFSET_PCT": "0",
            "ALGOVAULT_BOT_CLOSE_GRACE_MIN": "0",
            "ALGOVAULT_BOT_JITTER_WINDOW_MIN": "1",
        })
        return da.audit(conn, **kw)
    finally:
        conn.close()


def classes(res: da.AuditResult) -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for f in res.findings:
        out.setdefault(f.cls, []).append((f.row.chat_id, f.cause))
    return out


# ── the measured case, both directions ─────────────────────────────────────────────────────


def test_the_page_is_designed_recovery_when_the_record_explains_it(tmp_path: Path) -> None:
    res = run(measured(tmp_path))
    assert res.verdict == "OK"
    late = sorted(f.row.chat_id for f in res.findings if f.cls == "LATE_EXPLAINED")
    assert late == sorted(c for c, *_ in LATE)
    assert {f.cause for f in res.findings if f.cls == "LATE_EXPLAINED"} == {"fetch_failed"}
    assert len([f for f in res.findings if f.cls == "ON_TIME"]) == 12
    f = next(x for x in res.findings if x.row.chat_id == 10240)
    assert (f.lag0, f.spanned, f.late_ticks, f.exec_s) == (10, 1, 1, 2)


def test_the_real_mechanism_budget_deferral_is_explained_and_never_chronic(
    tmp_path: Path,
) -> None:
    """What CH1 measured the 71/72 to be. Deferral has its own owner (the saturation alarm, R5),
    so even recurring it never pages here."""
    res = run(measured(tmp_path, late_cause="deferred_budget", late_history="deferred_budget"))
    assert res.verdict == "OK"
    assert {f.cause for f in res.findings if f.cls == "LATE_EXPLAINED"} == {"deferred"}
    assert not any(f.chronic for f in res.findings)


def test_three_prior_late_buckets_for_a_fetch_failure_is_chronic_late(tmp_path: Path) -> None:
    res = run(measured(tmp_path, late_history="fetch_failed"))
    assert res.verdict == "CHRONIC_LATE"
    assert sorted(f.row.chat_id for f in res.findings if f.chronic) == sorted(c for c, *_ in LATE)


def test_the_same_stamps_with_no_recorded_reason_are_still_a_fault(tmp_path: Path) -> None:
    """THE DIRECTION THAT KEEPS THIS FROM BEING A BLUNTED GUARD."""
    res = run(measured(tmp_path, late_cause=None))
    assert res.verdict == "TIMING_FAULT"
    bad = [f for f in res.findings if f.cls == "LATE_UNEXPLAINED"]
    assert sorted(f.row.chat_id for f in bad) == sorted(c for c, *_ in LATE)
    assert {f.cause for f in bad} == {"first_tick_late"}


def test_execution_is_measured_from_the_tick_not_the_due_time(tmp_path: Path) -> None:
    """+71 s into the bar is +1 s of execution after the tick that serviced it."""
    res = run(measured(tmp_path))
    f = next(x for x in res.findings if x.row.chat_id == 16131)
    assert (f.tick, f.fired, f.exec_s) == (BAR + 70, BAR + 71, 1)


def test_a_slow_service_beyond_the_allowance_is_a_latency_breach(tmp_path: Path) -> None:
    fx = measured(tmp_path)
    with sqlite3.connect(fx.path) as c:
        c.execute("UPDATE dispatch_ledger SET fired_epoch = ? WHERE chat_id = 20000 AND"
                  " bucket_epoch = ?", (BAR + 10 + 31, BAR))
        c.execute("UPDATE watchlists SET last_fetched_at = ? WHERE chat_id = 20000",
                  (_iso(BAR + 41),))
    res = run(fx)
    assert res.verdict == "CHRONIC_LATE"
    assert classes(res)["LATENCY_BREACH"] == [(20000, "exec")]


# ── the explained-only shapes never page ─────────────────────────────────────────────────────


def test_deferred_only_is_ok(tmp_path: Path) -> None:
    fx = Fixture(tmp_path / "s.db")
    fx.watch(1, "A", "BINANCE", stamp=fx.on_time(1, "A", "BINANCE", BAR - P15))
    fx.on_time(9, "Z", "BINANCE", BAR - 3 * P15)
    fired = fx.on_time(2, "B", "BINANCE", BAR)
    fx.watch(2, "B", "BINANCE", stamp=fired)
    fx.event(1, "A", "BINANCE", BAR, "deferred_budget", tick=BAR + 10, n=11)
    res = run(fx)
    assert res.verdict == "OK"
    assert classes(res)["PENDING_DEFERRED"] == [(1, "deferred")]


def test_a_skipped_exhausted_frozen_anchor_is_ok(tmp_path: Path) -> None:
    """The H5a rows of CH1: frozen for weeks, judged every hour by the old probe."""
    fx = measured(tmp_path)
    fx.watch(12515, "BTC", "BINANCE", stamp=BAR - 22 * 86_400 + 11)
    fx.event(12515, "BTC", "BINANCE", BAR, "skipped_exhausted", tick=BAR + 10, n=11)
    res = run(fx)
    assert res.verdict == "OK"
    assert classes(res)["UNSERVICED_SKIPPED"] == [(12515, "skipped")]


def test_a_first_fetch_is_counted_never_judged(tmp_path: Path) -> None:
    fx = measured(tmp_path)
    fx.watch(777, "NEW", "BINANCE", stamp=BAR + 250, added=BAR + 200)
    fx.event(777, "NEW", "BINANCE", BAR, "serviced_first", tick=BAR + 250, fired=BAR + 250)
    res = run(fx)
    assert res.verdict == "OK"
    assert classes(res)["NEW_ROW"] == [(777, "added_in_bar")]


# ── what the record cannot explain ───────────────────────────────────────────────────────────


def test_post_deploy_stamps_with_an_empty_ledger_are_a_deploy_regression(tmp_path: Path) -> None:
    fx = Fixture(tmp_path / "s.db")
    for i in range(3):
        fx.watch(30 + i, f"R{i}", "BINANCE", stamp=BAR + 15)
    res = run(fx)
    assert res.verdict == "DEPLOY_REGRESSION"
    assert {f.cls for f in res.findings} == {"UNPROVENANCED"}


def test_a_due_row_with_no_event_is_missed(tmp_path: Path) -> None:
    fx = measured(tmp_path)
    fx.watch(40, "GAP", "BINANCE", stamp=fx.on_time(40, "GAP", "BINANCE", BAR - P15))
    res = run(fx)
    assert res.verdict == "TIMING_FAULT"
    assert classes(res)["MISSED"] == [(40, "no_event")]


def test_every_due_row_missed_is_the_dispatcher_dark(tmp_path: Path) -> None:
    fx = Fixture(tmp_path / "s.db")
    for i in range(3):
        # Serviced (and recorded) four bars ago; nothing at all since.
        fx.watch(50 + i, f"D{i}", "BINANCE", stamp=fx.on_time(50 + i, f"D{i}", "BINANCE",
                                                                  BAR - 4 * P15))
    res = run(fx)
    assert res.verdict == "DEPLOY_REGRESSION"
    assert {f.cls for f in res.findings} == {"MISSED"}


def test_a_stamp_rewound_by_a_second_writer_is_a_stamp_mismatch(tmp_path: Path) -> None:
    fx = measured(tmp_path)
    with sqlite3.connect(fx.path) as c:
        c.execute("UPDATE watchlists SET last_fetched_at = ? WHERE chat_id = 20001",
                  (_iso(BAR - 20),))
    res = run(fx)
    assert res.verdict == "DEPLOY_REGRESSION"
    assert classes(res)["STAMP_MISMATCH"] == [(20001, "stamp_rewound")]


def test_two_services_in_one_bucket_is_the_ratchet_signature(tmp_path: Path) -> None:
    """Q1: `is_due` fires a row at most once per bucket. A second stamping event in one bucket
    is the ratchet in ledger terms."""
    fx = measured(tmp_path)
    with sqlite3.connect(fx.path) as c:
        c.execute("UPDATE dispatch_ledger SET n = 2 WHERE chat_id = 20002 AND bucket_epoch = ?"
                  " AND disposition = 'serviced'", (BAR,))
    res = run(fx)
    assert res.verdict == "DEPLOY_REGRESSION"
    assert classes(res)["STAMP_MISMATCH"] == [(20002, "double_service")]


def test_a_config_change_realigns_and_never_pages(tmp_path: Path) -> None:
    """Q1, the other half: a recorded shift change (the 2026-09-04 flip's shape, 60 → 0) is a
    dispatch-config change, judged again after REALIGN_BARS buckets — not a regression."""
    fx = measured(tmp_path)
    with sqlite3.connect(fx.path) as c:
        c.execute("UPDATE dispatch_ledger SET due_epoch = bucket_epoch + 60 WHERE bucket_epoch < ?",
                  (BAR,))
    res = run(fx)
    assert res.verdict == "INDETERMINATE"  # every row realigning: nothing judged, no page
    assert {f.cls for f in res.findings} == {"REALIGNING"}
    assert res.reason == "insufficient"


def test_an_unknown_disposition_is_noise_never_explained(tmp_path: Path) -> None:
    """R2.5. Built without the CHECK, as a newer producer's row would look to an older reader."""
    path = tmp_path / "s.db"
    with sqlite3.connect(path) as c:
        c.executescript(
            "CREATE TABLE subscribers (chat_id INTEGER PRIMARY KEY);"
            "CREATE TABLE watchlists (chat_id INTEGER, coin TEXT, timeframe TEXT, exchange TEXT,"
            " alert_type TEXT, last_fetched_at TEXT, added_at TEXT);"
            "CREATE TABLE dispatch_ledger (chat_id INTEGER, coin TEXT, timeframe TEXT,"
            " exchange TEXT, bucket_epoch INTEGER, disposition TEXT, due_epoch INTEGER,"
            " first_tick INTEGER, last_tick INTEGER, n INTEGER, fired_epoch INTEGER);"
        )
        for b in (BAR - 3 * P15, BAR - 2 * P15, BAR - P15):
            c.execute("INSERT INTO dispatch_ledger VALUES (1,'A','15m','X',?,?,?,?,?,1,?)",
                      (b, "serviced", b, b + 10, b + 10, b + 15))
        c.execute("INSERT INTO dispatch_ledger VALUES (1,'A','15m','X',?,?,?,?,?,1,NULL)",
                  (BAR, "quiet_hours", BAR, BAR + 10, BAR + 10))
        c.execute("INSERT INTO dispatch_ledger VALUES (1,'A','15m','X',?,?,?,?,?,1,?)",
                  (BAR, "serviced", BAR, BAR + 70, BAR + 70, BAR + 72))
        c.execute("INSERT INTO watchlists VALUES (1,'A','15m','X','calls',?,?)",
                  (_iso(BAR + 72), _iso(BAR - 86_400)))
    conn = da.open_read_only(str(path))
    try:
        res = da.audit(conn, now=NOW, tf_scope="15m", deploy_epoch=DEPLOY)
    finally:
        conn.close()
    assert res.verdict == "TIMING_FAULT"
    assert classes(res)["LATE_UNEXPLAINED"] == [(1, "unknown_disposition:quiet_hours")]


def test_a_missing_ledger_is_a_deploy_regression(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE watchlists (chat_id INTEGER, coin TEXT, timeframe TEXT,"
                  " exchange TEXT, alert_type TEXT, last_fetched_at TEXT, added_at TEXT)")
    conn = da.open_read_only(str(path))
    try:
        res = da.audit(conn, now=NOW, tf_scope="15m")
    finally:
        conn.close()
    assert (res.verdict, res.reason) == ("DEPLOY_REGRESSION", "schema_missing")


# ── config: the one TRUE meaning of "the offset value is wrong" ──────────────────────────────


def test_an_inline_comment_is_part_of_the_value_and_rejected(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.write_text(
        "PUBLIC_BOT_TOKEN=never-read\n"
        "# ALGOVAULT_BOT_DISPATCH_OFFSET_PCT=75\n"
        "ALGOVAULT_BOT_DISPATCH_OFFSET_PCT=0 # x\n"
        "ALGOVAULT_BOT_CLOSE_GRACE_MIN=0\n"
        "ALGOVAULT_BOT_JITTER_WINDOW_MIN='1'\n"
    )
    literals = da.read_env_knobs(str(env))
    assert set(literals) == {
        "ALGOVAULT_BOT_DISPATCH_OFFSET_PCT",
        "ALGOVAULT_BOT_CLOSE_GRACE_MIN",
        "ALGOVAULT_BOT_JITTER_WINDOW_MIN",
    }, "only the three knob lines are kept — the file holds the bot's secrets"
    assert literals["ALGOVAULT_BOT_DISPATCH_OFFSET_PCT"] == "0 # x"
    assert literals["ALGOVAULT_BOT_JITTER_WINDOW_MIN"] == "1"
    res = run(measured(tmp_path), env_literals=literals)
    assert res.verdict == "TIMING_FAULT"
    config = next(line for line in res.lines if line.startswith("AUDIT_CONFIG "))
    assert "OFFSET_PCT=0 # x→75" in config and "rejected=OFFSET_PCT" in config


def test_accepted_knobs_are_not_rejected(tmp_path: Path) -> None:
    res = run(measured(tmp_path))
    config = next(line for line in res.lines if line.startswith("AUDIT_CONFIG "))
    assert "rejected=none skew=0" in config
    assert "OFFSET_PCT=0→0" in config


def test_a_recorded_shift_that_disagrees_with_the_env_is_config_skew(tmp_path: Path) -> None:
    res = run(measured(tmp_path), env_literals={
        "ALGOVAULT_BOT_DISPATCH_OFFSET_PCT": "0",
        "ALGOVAULT_BOT_CLOSE_GRACE_MIN": "1",
        "ALGOVAULT_BOT_JITTER_WINDOW_MIN": "1",
    })
    assert res.verdict == "TIMING_FAULT"
    config = next(line for line in res.lines if line.startswith("AUDIT_CONFIG "))
    assert "rejected=none" in config and "skew=0" not in config


# ── scope (R7) ───────────────────────────────────────────────────────────────────────────────


def test_a_bar_whose_retry_budget_has_not_elapsed_is_not_yet_due(tmp_path: Path) -> None:
    fx = Fixture(tmp_path / "s.db")
    fx.on_time(9, "Z", "BINANCE", BAR - 4 * P15)
    fx.watch(60, "N", "BINANCE", stamp=fx.on_time(60, "N", "BINANCE", BAR - P15))
    res = run(fx, now=BAR + 60)
    assert {f.cls for f in res.findings} == {"NOT_YET_DUE"}
    assert res.verdict == "INDETERMINATE" and res.reason == "insufficient"
    assert any(line.startswith("CRON_MINUTE_HINT=") for line in res.lines)


def test_only_bars_that_opened_at_the_top_of_the_hour_are_audited() -> None:
    four_am = BAR + 11 * 60
    assert [tf for tf, _, _ in da.audited_bars(four_am, "all")] == [
        "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"
    ]
    five_am = BAR + 3600 + 11 * 60
    assert "4h" not in [tf for tf, _, _ in da.audited_bars(five_am, "all")]


# ── exhaustiveness (R2.5) ────────────────────────────────────────────────────────────────────


def test_the_routing_maps_are_exhaustive_over_their_literals() -> None:
    assert set(da.DISPOSITION_TO_CAUSE) == set(get_args(Disposition)) == set(DISPOSITIONS)
    assert set(da.CLASS_TO_VERDICT) == set(get_args(da.RowClass)) == set(da.ROW_CLASSES)
    assert set(da.EXIT_CODE) == set(da.VERDICTS)
    assert set(da.VERDICT_PRECEDENCE) | {"INDETERMINATE"} == set(da.VERDICTS)


# ── read-only, structurally ──────────────────────────────────────────────────────────────────


def test_a_write_through_the_auditors_connection_raises(tmp_path: Path) -> None:
    fx = measured(tmp_path)
    conn = da.open_read_only(fx.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM dispatch_ledger")
    finally:
        conn.close()


def test_the_cli_leaves_the_database_bytes_unchanged(tmp_path: Path) -> None:
    fx = measured(tmp_path)
    before = fx.sha()
    out, code = _cli("--db", fx.path, "--tf", "15m", "--now", str(NOW),
                     "--deploy-epoch", str(DEPLOY))
    assert code == 0, out
    assert fx.sha() == before


def test_root_drops_to_the_db_owner_before_sqlite_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Any]] = []
    state = {"euid": 0}

    class _St:
        st_uid, st_gid = 1234, 5678

    monkeypatch.setattr(da.os, "geteuid", lambda: state["euid"])
    monkeypatch.setattr(da.os, "stat", lambda _p: _St())
    monkeypatch.setattr(da.os, "setgroups", lambda g: calls.append(("setgroups", g)))
    monkeypatch.setattr(da.os, "setgid", lambda g: calls.append(("setgid", g)))

    def _setuid(u: int) -> None:
        calls.append(("setuid", u))
        state["euid"] = u

    monkeypatch.setattr(da.os, "setuid", _setuid)
    assert da.drop_privileges_to_owner("state.db") == 1234
    assert calls == [("setgroups", []), ("setgid", 5678), ("setuid", 1234)]


def test_a_non_root_process_does_not_touch_its_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(da.os, "geteuid", lambda: 501)
    monkeypatch.setattr(da.os, "setuid", lambda _u: pytest.fail("must not setuid"))
    assert da.drop_privileges_to_owner("state.db") is None


# ── the CLI: one token, the exit map, --print-verdicts ───────────────────────────────────────


def _cli(*args: str) -> tuple[str, int]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "algovault_bot.dispatch_audit", *args],
        capture_output=True, text=True, cwd=REPO, env=env, timeout=120, check=False,
    )
    return proc.stdout, proc.returncode


def _tokens(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("DISPATCH_AUDIT_VERDICT=")]


@pytest.mark.parametrize("late_cause,verdict,code", [
    ("fetch_failed", "OK", 0),
    (None, "TIMING_FAULT", 1),
])
def test_token_and_exit_map(tmp_path: Path, late_cause: str | None, verdict: str,
                            code: int) -> None:
    fx = measured(tmp_path, late_cause=late_cause)
    out, rc = _cli("--db", fx.path, "--tf", "15m", "--now", str(NOW),
                   "--deploy-epoch", str(DEPLOY))
    assert _tokens(out) == [f"DISPATCH_AUDIT_VERDICT={verdict}"]
    assert out.rstrip().splitlines()[-1] == f"DISPATCH_AUDIT_VERDICT={verdict}"
    assert rc == code


def test_insufficient_is_indeterminate_3(tmp_path: Path) -> None:
    fx = Fixture(tmp_path / "s.db")
    out, rc = _cli("--db", fx.path, "--tf", "15m", "--now", str(NOW))
    assert _tokens(out) == ["DISPATCH_AUDIT_VERDICT=INDETERMINATE"]
    assert "AUDIT_REASON=insufficient" in out and rc == 3


def test_a_crash_is_a_verdict_never_a_missing_token(tmp_path: Path) -> None:
    out, rc = _cli("--db", str(tmp_path / "absent.db"), "--tf", "15m")
    assert _tokens(out) == ["DISPATCH_AUDIT_VERDICT=INDETERMINATE"]
    assert "AUDIT_REASON=crash" in out and "AUDIT_ERROR " in out and rc == 3
    out, rc = _cli("--db", str(tmp_path / "absent.db"), "--tf", "7m")
    assert _tokens(out) == ["DISPATCH_AUDIT_VERDICT=INDETERMINATE"] and rc == 3


def test_print_verdicts_is_the_closed_enum() -> None:
    out, rc = _cli("--print-verdicts")
    assert rc == 0
    assert out.split() == list(da.VERDICTS)
    assert list(da.VERDICTS) == [
        "OK", "TIMING_FAULT", "DEPLOY_REGRESSION", "CHRONIC_LATE", "INDETERMINATE"
    ]


def test_rows_print_with_last4_and_the_verdict_they_contribute(tmp_path: Path) -> None:
    res = run(measured(tmp_path, late_cause=None))
    rows = [line for line in res.lines if line.startswith("AUDIT_ROW ")]
    assert len(rows) == 2, "ON_TIME rows are counted, not listed"
    assert all(" last4=" in r and r.endswith("verdict=TIMING_FAULT") for r in rows)
    summary = next(line for line in res.lines if line.startswith("AUDIT_SUMMARY "))
    assert "judged=14" in summary and "offenders=2" in summary
