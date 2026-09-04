"""TG-BATCH-WATCHLIST-W1 C2 — fetch-rate budget + fair-share + skip-exhausted
+ sustained-deferred saturation detector. Pure scheduler tests (no DB/MCP)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from algovault_bot import fetch_budget


@dataclass
class Row:
    chat_id: int
    timeframe: str
    alert_type: str = "calls"


def _rows(chat_id: int, n: int, tf: str = "1m", atype: str = "calls") -> list[Row]:
    return [Row(chat_id, tf, atype) for _ in range(n)]


_NONE_EXHAUSTED = lambda _cid: False  # noqa: E731


# ── budget cap ─────────────────────────────────────────────────


# OPS-BOT-DISPATCH-LATENCY-W1 CH1: `update_saturation_state` now takes an INJECTED clock so its
# rolling episode window is testable. These legacy cases exercise arm A (consecutive), so they
# advance one tick (60s) per call and never accumulate enough episodes to trip arm B.
_CLOCK = [1_788_000_000]


def _t() -> int:
    _CLOCK[0] += 60
    return _CLOCK[0]


def test_budget_caps_processed_exactly() -> None:
    # AC2.1 — 1000 due, budget 200 → exactly 200 scheduled, 800 deferred.
    due = _rows(1, 1000)
    res = fetch_budget.schedule(due, budget=200, is_exhausted=_NONE_EXHAUSTED)
    assert len(res.scheduled) == 200
    assert len(res.deferred) == 800
    assert res.stats["processed"] == 200
    assert res.stats["deferred"] == 800


def test_all_due_under_budget_all_scheduled() -> None:
    due = _rows(1, 10)
    res = fetch_budget.schedule(due, budget=200, is_exhausted=_NONE_EXHAUSTED)
    assert len(res.scheduled) == 10
    assert res.deferred == []


def test_empty_due_empty_result() -> None:
    res = fetch_budget.schedule([], budget=30, is_exhausted=_NONE_EXHAUSTED)
    assert res.scheduled == [] and res.deferred == []
    assert res.stats["active_users"] == 0


# ── fair-share (round-robin) ───────────────────────────────────


def test_fair_share_small_user_not_starved() -> None:
    # AC2.2 — a 990-row user must not starve a 10-row user.
    due = _rows(1, 990, tf="1m") + _rows(2, 10, tf="1m")
    res = fetch_budget.schedule(due, budget=200, is_exhausted=_NONE_EXHAUSTED)
    scheduled_user2 = [r for r in res.scheduled if r.chat_id == 2]
    assert len(scheduled_user2) == 10  # small user fully served in one tick
    assert res.stats["active_users"] == 2


def test_fair_share_even_split_when_budget_tight() -> None:
    # 2 users, 100 each, budget 10 → 5 each (round-robin).
    due = _rows(1, 100, tf="1m") + _rows(2, 100, tf="1m")
    res = fetch_budget.schedule(due, budget=10, is_exhausted=_NONE_EXHAUSTED)
    u1 = sum(1 for r in res.scheduled if r.chat_id == 1)
    u2 = sum(1 for r in res.scheduled if r.chat_id == 2)
    assert u1 == 5 and u2 == 5


# ── TF-priority within a user ──────────────────────────────────


def test_tf_priority_higher_tf_first() -> None:
    due = [Row(1, "1m"), Row(1, "1d"), Row(1, "1h")]
    res = fetch_budget.schedule(due, budget=2, is_exhausted=_NONE_EXHAUSTED)
    tfs = [r.timeframe for r in res.scheduled]
    assert tfs == ["1d", "1h"]  # 1m deferred last
    assert res.deferred[0].timeframe == "1m"


# ── skip-exhausted ─────────────────────────────────────────────


def test_skip_exhausted_drops_calls_keeps_regime() -> None:
    # AC2.3 — exhausted user's `calls` rows dropped; `regime` rows kept.
    due = [
        Row(1, "1m", "calls"),
        Row(1, "4h", "regime"),
        Row(2, "1m", "calls"),
    ]
    exhausted = {1}
    res = fetch_budget.schedule(due, budget=100, is_exhausted=lambda cid: cid in exhausted)
    kinds = {(r.chat_id, r.alert_type) for r in res.scheduled}
    assert (1, "calls") not in kinds  # dropped
    assert (1, "regime") in kinds     # kept (free)
    assert (2, "calls") in kinds      # other user unaffected
    assert res.stats["skipped_exhausted"] == 1


def test_skip_exhausted_keeps_both_rows() -> None:
    # `both` rows still fire the (free) regime half → kept even when exhausted.
    due = [Row(1, "1m", "both")]
    res = fetch_budget.schedule(due, budget=10, is_exhausted=lambda _cid: True)
    assert len(res.scheduled) == 1
    assert res.stats["skipped_exhausted"] == 0


# ── deferred rows untouched ────────────────────────────────────


def test_deferred_rows_returned_unmodified() -> None:
    due = _rows(1, 5, tf="1m")
    res = fetch_budget.schedule(due, budget=2, is_exhausted=_NONE_EXHAUSTED)
    # Deferred rows are the SAME objects (engine won't mark last_fetched_at →
    # they stay due next tick).
    assert all(any(d is orig for orig in due) for d in res.deferred)
    assert len(res.deferred) == 3


# ── env knobs ──────────────────────────────────────────────────


def test_budget_env_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Asserted against the CONSTANT, not a literal. The literal 30 was baked in here and had to
    # be hand-edited when OPS-BOT-DISPATCH-LATENCY-W1 CH3 raised the default — a duplicated fact
    # that goes stale, which is exactly what this repo's own rules say not to write.
    default = fetch_budget.DEFAULT_FETCH_BUDGET_PER_MIN
    monkeypatch.delenv("FETCH_BUDGET_PER_MIN", raising=False)
    assert fetch_budget.fetch_budget_per_min() == default
    monkeypatch.setenv("FETCH_BUDGET_PER_MIN", "12")
    assert fetch_budget.fetch_budget_per_min() == 12
    monkeypatch.setenv("FETCH_BUDGET_PER_MIN", "garbage")
    assert fetch_budget.fetch_budget_per_min() == default  # default-deny on bad value


def test_the_budget_covers_the_measured_worst_case() -> None:
    """The number itself is load-bearing, so it is pinned to what it was sized against rather
    than left as a bare literal nobody can re-derive. 51 = max eligible rows in any tick across
    the 40-day journal; 104 = the whole watchlist collapsing onto one tick, which is what CH4's
    jitter change produces. A future wave lowering this below either figure re-creates the
    silent deferral this wave exists to retire."""
    assert fetch_budget.DEFAULT_FETCH_BUDGET_PER_MIN >= 51, "measured max eligible/tick"
    assert fetch_budget.DEFAULT_FETCH_BUDGET_PER_MIN >= 104, "CH4 full-collapse worst case"


def test_concurrency_env_default_override_and_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    default = fetch_budget.DEFAULT_FETCH_CONCURRENCY
    monkeypatch.delenv("FETCH_CONCURRENCY", raising=False)
    assert fetch_budget.fetch_concurrency() == default
    monkeypatch.setenv("FETCH_CONCURRENCY", "4")
    assert fetch_budget.fetch_concurrency() == 4
    # 1 is the documented rollback: it restores the pre-CH3 sequential tick with no redeploy.
    monkeypatch.setenv("FETCH_CONCURRENCY", "1")
    assert fetch_budget.fetch_concurrency() == 1
    # Clamped both ways so a typo cannot stampede the venue layer or halt the tick.
    monkeypatch.setenv("FETCH_CONCURRENCY", "0")
    assert fetch_budget.fetch_concurrency() == 1
    monkeypatch.setenv("FETCH_CONCURRENCY", "9999")
    assert fetch_budget.fetch_concurrency() == 32
    monkeypatch.setenv("FETCH_CONCURRENCY", "garbage")
    assert fetch_budget.fetch_concurrency() == default


# ── sustained-deferred saturation detector ─────────────────────


def test_saturation_fires_after_threshold_consecutive_ticks() -> None:
    state: dict = {}
    fired = []
    for _ in range(5):
        state, alert = fetch_budget.update_saturation_state(state, 7, 5, _t())
        fired.append(alert)
    assert fired == [False, False, False, False, True]  # fires on the 5th


def test_saturation_resets_on_clean_tick() -> None:
    state: dict = {}
    state, _ = fetch_budget.update_saturation_state(state, 7, 3, _t())
    state, _ = fetch_budget.update_saturation_state(state, 7, 3, _t())
    state, alert = fetch_budget.update_saturation_state(state, 0, 3, _t())
    assert alert is False
    assert state["consecutive"] == 0


def test_saturation_fires_once_per_episode() -> None:
    # After firing, the counter resets so it does NOT re-fire every subsequent
    # tick (the 24h send-cooldown is additionally enforced by send_telegram.sh).
    state: dict = {}
    alerts = []
    for _ in range(8):  # 8 consecutive deferred ticks, threshold 3
        state, alert = fetch_budget.update_saturation_state(state, 4, 3, _t())
        alerts.append(alert)
    # fires on tick 3 and tick 6 (reset after each) — once per 3-tick episode.
    assert alerts == [False, False, True, False, False, True, False, False]
