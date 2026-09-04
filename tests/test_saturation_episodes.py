"""OPS-BOT-DISPATCH-LATENCY-W1 CH1 — the saturation alarm could not fire.

`update_saturation_state` asked for `DEFAULT_SATURATION_TICKS = 5` BACK-TO-BACK deferred
ticks. Real budget pressure on this engine is boundary-clustered: rows collapse onto the
minutes after a shared bar boundary, the budget drains them over 3-4 ticks, then 56 minutes
are clean. Measured over the full 40-day journal the longest run was 3-4, so the alarm had
NEVER fired — while the budget was genuinely binding on 26 ticks in 26 hours, every one of
them at minute :03.

That is a dark guard, not a mis-tuned one: it was asking the wrong question. The fix adds a
second arm that counts RECURRENCE (distinct bursts inside a rolling window) alongside the
original arm that counts an unbroken run. `test_the_real_measured_shape_*` pair is the proof:
the same tick sequence, taken from production, is silent under arm A alone and fires under
arm B.
"""

from __future__ import annotations

from algovault_bot import fetch_budget
from algovault_bot.fetch_budget import update_saturation_state

HOUR = 3600
MIN = 60
T0 = 1_788_000_000


def _drive(ticks: list[tuple[int, int]], *, threshold: int = 5,
           episode_threshold: int = 3, window_seconds: int = 3 * HOUR) -> list[int]:
    """Run a (now_epoch, deferred) sequence; return the epochs at which it alerted."""
    state: dict = {}
    fired: list[int] = []
    for now, deferred in ticks:
        state, alert = update_saturation_state(
            state, deferred, threshold,
            now, episode_threshold=episode_threshold, window_seconds=window_seconds,
        )
        if alert:
            fired.append(now)
    return fired


def _production_shape(hours: int) -> list[tuple[int, int]]:
    """The measured live pattern: a 3-tick deferred burst at :03-:05 of each hour, clean for
    the other 57 minutes. Never 5 consecutive, so arm A can never see it."""
    ticks: list[tuple[int, int]] = []
    for h in range(hours):
        base = T0 + h * HOUR
        for m in range(60):
            deferred = 9 if m in (3, 4, 5) else 0
            ticks.append((base + m * MIN, deferred))
    return ticks


# ── the defect, and the fix, on the same input ─────────────────────────────


def test_the_real_measured_shape_is_invisible_to_the_consecutive_arm() -> None:
    """Arm A alone over three hours of the REAL pattern: silent. This is the dark guard."""
    # episode_threshold set unreachably high == arm B disabled == the pre-wave detector.
    fired = _drive(_production_shape(3), threshold=5, episode_threshold=10**9)
    assert fired == [], "arm A cannot see boundary-clustered pressure — that is the defect"


def test_the_real_measured_shape_fires_on_the_episode_arm() -> None:
    """Same input, arm B enabled: the third recurrence alerts. Once, not per tick."""
    fired = _drive(_production_shape(3))
    assert len(fired) == 1, "chronic saturation must alert exactly once per cluster"
    # Third burst begins at :03 of the third hour.
    assert fired[0] == T0 + 2 * HOUR + 3 * MIN


# ── arm B's own boundaries ─────────────────────────────────────────────────


def test_a_single_bad_hour_stays_silent() -> None:
    """One boundary burst is ordinary. Alerting on it would be the lane fix (threshold 3)
    that this design deliberately rejected — it would page on every busy hour."""
    assert _drive(_production_shape(1)) == []


def test_two_bursts_stay_silent_three_fire() -> None:
    assert _drive(_production_shape(2)) == []
    assert len(_drive(_production_shape(3))) == 1


def test_a_multi_tick_burst_counts_as_ONE_episode() -> None:
    """A 4-tick drain is one burst. Counting each tick would make the window meaningless and
    would fire on the first boundary — the same false-positive as the lane fix."""
    ticks = [(T0 + i * MIN, 9 if i < 4 else 0) for i in range(10)]
    assert _drive(ticks, threshold=99) == []


def test_episodes_outside_the_window_are_forgotten() -> None:
    """Recurrence is what matters, so the window must actually roll: three bursts spread
    over 5 hours are NOT chronic under a 3h window."""
    ticks = []
    for h in (0, 2, 4):
        ticks.append((T0 + h * HOUR, 9))
        ticks.append((T0 + h * HOUR + MIN, 0))
    assert _drive(ticks, threshold=99, window_seconds=3 * HOUR) == []
    # …but the same three inside a wider window do fire.
    assert len(_drive(ticks, threshold=99, window_seconds=6 * HOUR)) == 1


def test_both_counters_clear_on_fire() -> None:
    """Otherwise a chronic condition re-alerts on every subsequent tick."""
    state: dict = {}
    fired = 0
    for i in range(40):
        state, alert = update_saturation_state(
            state, 9, 5, T0 + i * MIN, episode_threshold=3, window_seconds=3 * HOUR
        )
        fired += int(alert)
    # An unbroken 40-tick run trips arm A every 5 ticks; it must never fire twice on one tick
    # and must reset in between, so the count is bounded by 40/5, not 40.
    assert fired == 8
    assert state["episode_starts"] == [] or state["consecutive"] < 5


# ── arm A must not regress ─────────────────────────────────────────────────


def test_arm_a_still_fires_on_an_unbroken_run() -> None:
    """A genuine stall — never draining — is a different failure and must keep its signal."""
    ticks = [(T0 + i * MIN, 9) for i in range(5)]
    assert _drive(ticks, threshold=5, episode_threshold=10**9) == [T0 + 4 * MIN]


def test_a_clean_tick_resets_the_consecutive_counter() -> None:
    ticks = [(T0, 9), (T0 + MIN, 9), (T0 + 2 * MIN, 0), (T0 + 3 * MIN, 9)]
    assert _drive(ticks, threshold=3, episode_threshold=10**9) == []


# ── state-file robustness ──────────────────────────────────────────────────


def test_tolerates_a_state_file_written_by_the_pre_wave_shape() -> None:
    """The live state file on disk has only {"consecutive": N}. It must not crash or reset
    the consecutive count on the first tick after deploy."""
    state, alert = update_saturation_state({"consecutive": 4}, 9, 5, T0)
    assert alert is True, "the pre-wave counter must carry over, not be discarded"


def test_tolerates_a_corrupt_episode_list() -> None:
    for bad in ({"episode_starts": "nonsense"}, {"episode_starts": None},
                {"episode_starts": [T0, "x", None, T0 + 60]}):
        state, _ = update_saturation_state(dict(bad), 9, 99, T0 + 120)
        assert isinstance(state["episode_starts"], list)


# ── the env knobs resolve, and default-deny on garbage ─────────────────────


def test_env_knobs_default_deny(monkeypatch) -> None:
    monkeypatch.setenv("FETCH_SATURATION_EPISODES", "not-a-number")
    monkeypatch.setenv("FETCH_SATURATION_WINDOW_SEC", "")
    assert fetch_budget.saturation_episodes() == fetch_budget.DEFAULT_SATURATION_EPISODES
    assert fetch_budget.saturation_window_seconds() == fetch_budget.DEFAULT_SATURATION_WINDOW_SEC


def test_env_knobs_are_readable(monkeypatch) -> None:
    monkeypatch.setenv("FETCH_SATURATION_EPISODES", "5")
    monkeypatch.setenv("FETCH_SATURATION_WINDOW_SEC", "7200")
    assert fetch_budget.saturation_episodes() == 5
    assert fetch_budget.saturation_window_seconds() == 7200


def test_the_clock_is_required_not_optional() -> None:
    """An optional clock would let a caller silently leave arm B dark — the exact failure
    mode being retired. Absence must be a TypeError, not a default."""
    import pytest

    with pytest.raises(TypeError):
        update_saturation_state({}, 9, 5)  # type: ignore[call-arg]
