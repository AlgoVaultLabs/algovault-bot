"""OPS-BOT-LINKED-TIER-REFRESH-W1 CH3 — the link has a lifecycle.

A link used to have creation and no other state: nothing ever re-asked the server, so a
revoked key kept paid treatment forever. Chat 1793689937 has been in exactly that state
since 2026-05-08 — `linked_tier='starter'`, `validate-key` answering 404 — and because
`linked_tier in PAID_TIERS` makes `consume_quota` a no-op, the bot-side 100/mo wall never
applied to them.

🛑 BUILD RULE 5 IS WHAT THESE TESTS ARE FOR. Every transition that could reduce a
subscriber's entitlement fires only on a DETERMINED negative. An INDETERMINATE keeps
current state, always. A wrong downgrade walls a paying customer; a late one serves a
lapsed customer a few days longer. Only one of those is worth avoiding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from algovault_bot import entitlement_drain
from algovault_bot.db import Database
from algovault_bot.entitlement_drain import LINK_INVALID_GRACE
from algovault_bot.link_validator import KeyCheck


VALID_PRO = KeyCheck(status="VALID", tier="pro", customer_id="cus_x", reason="ok")
VALID_STARTER = KeyCheck(
    status="VALID", tier="starter", customer_id="cus_x", reason="ok"
)
INVALID = KeyCheck(
    status="INVALID", tier=None, customer_id=None, reason="no_active_subscription"
)
INDETERMINATE = KeyCheck(
    status="INDETERMINATE", tier=None, customer_id=None, reason="http_503"
)
BOT_MISCONFIGURED = KeyCheck(
    status="INDETERMINATE", tier=None, customer_id=None, reason="bot_misconfigured"
)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """Three linked subscribers, mirroring the live shape measured 2026-08-21."""
    d = Database(str(tmp_path / "t.db"))
    d.upsert_subscriber(1, "free", "en")
    d.upsert_subscriber(2, "paid", "en")
    d.upsert_subscriber(3, "dead", "en")
    d.upsert_subscriber(4, "control", "en")
    d.link_subscriber(2, "av_live_paidkey", "starter")
    d.link_subscriber(3, "av_live_deadkey", "starter")
    d.link_subscriber(4, "av_live_ctrlkey", "starter")
    return d


def _drain(db: Database, checks: dict[int, KeyCheck]) -> dict[str, int]:
    """Run one drain pass with a scripted validator, no network anywhere."""
    key_to_chat = {}
    for chat_id in (2, 3, 4):
        row = db.get_subscriber(chat_id)
        if row is not None and row["linked_api_key"]:
            key_to_chat[row["linked_api_key"]] = chat_id

    def _fake_validate(api_key: str) -> KeyCheck:
        return checks.get(key_to_chat.get(api_key, -1), INDETERMINATE)

    with patch.object(entitlement_drain, "validate_api_key", side_effect=_fake_validate), \
         patch.object(entitlement_drain, "read_state", return_value=None), \
         patch.object(entitlement_drain, "consume", return_value=None):
        return entitlement_drain.drain_entitlement_debits(db.path)


def _row(db: Database, chat_id: int) -> dict:
    r = db.get_subscriber(chat_id)
    assert r is not None
    return {k: r[k] for k in r.keys()}


def _age_the_streak(db: Database, chat_id: int, hours: float) -> None:
    """Backdate `link_invalid_since` — the grace window measures WALL-CLOCK, so this is
    how a long-running streak is expressed without waiting three days."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET link_invalid_since = ? WHERE chat_id = ?",
            (since, chat_id),
        )


# ── the state machine, one test per row ────────────────────────────────────────


def test_VALID_with_matching_tier_is_silent_and_leaves_the_link_alone(db: Database) -> None:
    counts = _drain(db, {2: VALID_STARTER, 3: VALID_STARTER, 4: VALID_STARTER})
    assert counts["revalidated"] == 3
    assert counts["key_invalid"] == 0 and counts["downgraded"] == 0
    for chat_id in (2, 3, 4):
        r = _row(db, chat_id)
        assert r["linked_api_key"] is not None
        assert r["link_invalid_streak"] == 0
        assert r["link_invalid_since"] is None


def test_VALID_with_a_DIFFERENT_tier_updates_silently(db: Database) -> None:
    """A customer who just upgraded does not need a bot message about it — CH2 already
    renders the new tier off the mirror the next refresh writes."""
    counts = _drain(db, {2: VALID_PRO, 3: VALID_STARTER, 4: VALID_STARTER})
    assert counts["downgraded"] == 0
    assert _row(db, 2)["linked_api_key"] is not None


def test_INVALID_advances_the_streak_and_stamps_first_seen(db: Database) -> None:
    counts = _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert counts["key_invalid"] == 1
    r = _row(db, 3)
    assert r["link_invalid_streak"] == 1
    assert r["link_invalid_since"] is not None
    assert r["linked_api_key"] is not None, "one observation is not a downgrade"


def test_INVALID_streak_accumulates_across_passes(db: Database) -> None:
    for expected in (1, 2, 3):
        _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
        assert _row(db, 3)["link_invalid_streak"] == expected
    # ...and `since` is stamped ONCE, at the start
    first = _row(db, 3)["link_invalid_since"]
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert _row(db, 3)["link_invalid_since"] == first


# ── 🛑 INDETERMINATE: the proof that matters most ──────────────────────────────


@pytest.mark.parametrize("unknown", [INDETERMINATE, BOT_MISCONFIGURED])
def test_INDETERMINATE_never_advances_the_streak(db: Database, unknown: KeyCheck) -> None:
    counts = _drain(db, {2: VALID_STARTER, 3: unknown, 4: VALID_STARTER})
    assert counts["key_invalid"] == 0
    assert counts["indeterminate"] == 1
    r = _row(db, 3)
    assert r["link_invalid_streak"] == 0
    assert r["link_invalid_since"] is None


@pytest.mark.parametrize("unknown", [INDETERMINATE, BOT_MISCONFIGURED])
def test_INDETERMINATE_never_downgrades_even_past_the_grace_window(
    db: Database, unknown: KeyCheck
) -> None:
    """The single most important assertion in this file."""
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=LINK_INVALID_GRACE.total_seconds() / 3600.0 + 48)

    counts = _drain(db, {2: VALID_STARTER, 3: unknown, 4: VALID_STARTER})

    assert counts["downgraded"] == 0
    r = _row(db, 3)
    assert r["linked_api_key"] is not None, "an unknown must never tear down a link"
    assert r["linked_tier"] == "starter"
    assert r["link_invalid_streak"] == 0, "an unknown ENDS a streak, it does not extend it"


def test_INDETERMINATE_resets_a_long_running_streak(db: Database) -> None:
    for _ in range(5):
        _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert _row(db, 3)["link_invalid_streak"] == 5
    _drain(db, {2: VALID_STARTER, 3: INDETERMINATE, 4: VALID_STARTER})
    assert _row(db, 3)["link_invalid_streak"] == 0
    assert _row(db, 3)["link_invalid_since"] is None


def test_VALID_resets_a_long_running_streak(db: Database) -> None:
    """A card that failed once and recovered must cost the customer nothing."""
    for _ in range(5):
        _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=71.0)
    _drain(db, {2: VALID_STARTER, 3: VALID_STARTER, 4: VALID_STARTER})
    r = _row(db, 3)
    assert r["link_invalid_streak"] == 0 and r["link_invalid_since"] is None
    # ...and a fresh streak starting now restarts the whole 72h, not the last 1h
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert _row(db, 3)["linked_api_key"] is not None


# ── the grace window ───────────────────────────────────────────────────────────


def test_71h_of_sustained_invalidity_does_NOT_downgrade(db: Database) -> None:
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=71.0)
    counts = _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert counts["downgraded"] == 0
    assert _row(db, 3)["linked_api_key"] is not None


def test_73h_of_sustained_invalidity_DOES_downgrade(db: Database) -> None:
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=73.0)
    counts = _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert counts["downgraded"] == 1
    r = _row(db, 3)
    assert r["linked_api_key"] is None, "unlink_subscriber() ran"
    assert r["linked_tier"] is None
    assert r["link_invalid_streak"] == 0, "the episode is over; the counter is cleared"
    assert r["link_invalid_since"] is None


def test_the_downgraded_subscriber_becomes_a_NORMAL_FREE_USER(db: Database) -> None:
    from algovault_bot.quota import get_quota_state

    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=73.0)
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})

    state = get_quota_state(db, 3)
    assert state.is_paid is False
    assert state.effective_tier == (None, "unknown")
    assert state.total == 100, "back on the free 100/mo meter"


def test_a_stale_streak_cannot_survive_an_unlink_into_the_NEXT_link(db: Database) -> None:
    """A streak carried across an unlink could downgrade a brand-new subscription on
    someone else's history."""
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=73.0)
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert _row(db, 3)["linked_api_key"] is None

    db.link_subscriber(3, "av_live_brandnew", "pro")
    r = _row(db, 3)
    assert r["link_invalid_streak"] == 0 and r["link_invalid_since"] is None


# ── the cohort-corroboration guard (the 404 ambiguity containment) ─────────────


def test_an_ALL_INVALID_pass_advances_NOTHING(db: Database) -> None:
    """A Stripe outage, or one lost STRIPE_SECRET_KEY, presents as every linked key
    answering 404 at once, because signal-MCP's route drops `validateApiKey`'s
    `indeterminate` flag. N simultaneous cancellations is not a thing that happens."""
    counts = _drain(db, {2: INVALID, 3: INVALID, 4: INVALID})
    assert counts["uncorroborated"] == 3
    assert counts["key_invalid"] == 0
    assert counts["downgraded"] == 0
    for chat_id in (2, 3, 4):
        r = _row(db, chat_id)
        assert r["linked_api_key"] is not None
        assert r["link_invalid_streak"] == 0


def test_an_ALL_INVALID_pass_cannot_downgrade_even_past_the_grace_window(
    db: Database,
) -> None:
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=999.0)
    counts = _drain(db, {2: INVALID, 3: INVALID, 4: INVALID})
    assert counts["downgraded"] == 0
    assert _row(db, 3)["linked_api_key"] is not None


def test_ONE_invalid_among_valid_peers_IS_corroborated(db: Database) -> None:
    """The real dead key must still be caught — the guard filters outages, not cancellations."""
    counts = _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert counts["uncorroborated"] == 0
    assert counts["key_invalid"] == 1


def test_a_sole_linked_subscriber_is_never_downgraded(db: Database) -> None:
    """Fail-safe direction: with no peer to corroborate against, we cannot tell a
    cancellation from an outage, so we serve. Logged, not absorbed."""
    db.unlink_subscriber(2)
    db.unlink_subscriber(4)
    _drain(db, {3: INVALID})
    _age_the_streak(db, 3, hours=999.0)
    counts = _drain(db, {3: INVALID})
    assert counts["downgraded"] == 0
    assert counts["uncorroborated"] == 1
    assert _row(db, 3)["linked_api_key"] is not None


# ── free-tier and unrelated subscribers are untouched ──────────────────────────


def test_a_free_subscriber_is_never_revalidated_or_touched(db: Database) -> None:
    before = _row(db, 1)
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=73.0)
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert _row(db, 1) == before, "a free subscriber's row must be byte-identical"


def test_a_downgrade_touches_only_the_downgraded_row(db: Database) -> None:
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    peers_before = {c: _row(db, c) for c in (1, 2, 4)}
    _age_the_streak(db, 3, hours=73.0)
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    for c in (1, 2, 4):
        assert _row(db, c) == peers_before[c], f"chat {c} moved during another's downgrade"


def test_revalidation_runs_ABOVE_the_staleness_filter(db: Database) -> None:
    """The placement that makes the lifecycle work at all.

    The mirror-warming poll `continue`s on a FRESH mirror. If revalidation sat underneath
    it, an ACTIVE subscriber — the one whose mirror is always fresh — would never be
    re-asked, which is exactly the population the lifecycle is for.
    """
    db.update_plan_mirror(3, {"tier": "starter", "used": 1, "total": 10}, source="debit")
    assert db.get_subscriber(3)["plan_state_as_of"] is not None  # fresh ⇒ poll would skip
    counts = _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert counts["polled"] == 0, "the fresh mirror was indeed skipped by the poll"
    assert counts["revalidated"] == 3, "...and yet all three were revalidated"
    assert _row(db, 3)["link_invalid_streak"] == 1


# ── 3d — the notice is RATIFIED (architect, 2026-08-21) and LIVE ──────────────
#
# These assertions used to read the other way round: the gate was fail-CLOSED and the tests
# asserted that an unset env var meant NO SEND, because the copy was PENDING-MR1 and an unset
# var must never mean "ship unratified copy to paying customers". Ratification inverts the
# risk — a flag that must be SET on every host to get approved behaviour is one that is off by
# accident on the next host — so the default flipped and these flipped with it. Each is
# annotated with what it used to assert.


def test_the_downgrade_notice_SENDS_by_default_now_that_the_copy_is_ratified(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # was: `send.assert_not_called()` with the env var unset (PENDING-MR1)
    monkeypatch.delenv("ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED", raising=False)
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=73.0)
    with patch.object(entitlement_drain, "_send_downgrade_notice", return_value=True) as send:
        _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
        assert send.call_count == 1, "ratified copy must not depend on host env to send"
    assert _row(db, 3)["linked_api_key"] is None, "the DOWNGRADE still happens"
    assert _row(db, 3)["link_downgrade_notice_at"] is not None


def test_the_notice_carries_the_RATIFIED_english_string_verbatim() -> None:
    """Pins the approved wording. A wording change is a public-copy change needing fresh
    ratification, so it must fail here rather than ship quietly."""
    from algovault_bot.messages import link_downgraded_message, signup_url

    assert link_downgraded_message("en") == (
        "Your AlgoVault subscription no longer appears active, so this chat has moved back "
        "to the free tier (100 alerts/month). Your watchlist is unchanged. "
        "Reactivate any time: " + signup_url("link_downgraded")
    )


@pytest.mark.parametrize("flag", ["", "1", "true", "yes", "TRUE", " ", "anything"])
def test_everything_except_literal_0_leaves_the_notice_ENABLED(
    db: Database, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """Backwards-compatible in the SAFE direction: hosts still carrying the
    pre-ratification `=1` keep sending, and a typo cannot silently mute a ratified notice."""
    monkeypatch.setenv("ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED", flag)
    assert entitlement_drain._downgrade_notice_enabled() is True


def test_the_kill_switch_is_exactly_0(monkeypatch: pytest.MonkeyPatch) -> None:
    # was: `test_the_gate_opens_on_exactly_1`
    monkeypatch.setenv("ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED", "0")
    assert entitlement_drain._downgrade_notice_enabled() is False


def test_the_kill_switch_suppresses_the_send_but_NOT_the_downgrade(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killing the notice must never wedge the lifecycle — a subscriber is still returned to
    the free tier, they just are not told. That asymmetry is logged at WARNING."""
    monkeypatch.setenv("ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED", "0")
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=73.0)
    with patch.object(entitlement_drain, "_send_downgrade_notice") as send:
        _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
        send.assert_not_called()
    assert _row(db, 3)["linked_api_key"] is None
    assert _row(db, 3)["link_downgrade_notice_at"] is None


def test_when_ENABLED_the_notice_sends_once_and_is_stamped(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED", "1")
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=73.0)
    with patch.object(entitlement_drain, "_send_downgrade_notice", return_value=True) as send:
        _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
        assert send.call_count == 1
    assert _row(db, 3)["link_downgrade_notice_at"] is not None


def test_a_notice_failure_REFUSES_and_never_takes_the_loop_down(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED", raising=False)
    _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    _age_the_streak(db, 3, hours=73.0)
    with patch.object(entitlement_drain, "_send_downgrade_notice", return_value=False):
        counts = _drain(db, {2: VALID_STARTER, 3: INVALID, 4: VALID_STARTER})
    assert counts["downgraded"] == 1, "the drain completed"
    assert _row(db, 3)["link_downgrade_notice_at"] is None, "not stamped — it did not send"


def test_the_send_helper_refuses_rather_than_raising(db: Database) -> None:
    """`_send_downgrade_notice` is called from a cron loop. It must never raise, even with
    no bot token, no network and no Telegram stack available."""
    with patch("algovault_bot.broadcast.sendDM", side_effect=RuntimeError("no token")):
        assert entitlement_drain._send_downgrade_notice(3, "en", db.path) is False


def test_the_notice_copy_is_trilingual_and_within_300_chars() -> None:
    from algovault_bot.messages import link_downgraded_message

    rendered = {lang: link_downgraded_message(lang) for lang in (None, "en", "id", "zh-Hans")}
    for lang, body in rendered.items():
        assert len(body) <= 300, f"{lang} exceeds 300 chars"
        assert "algovault.com/signup" in body, "one action, and it must be reactivation"
        assert "watchlist" in body.lower() or "自选" in body or "Watchlist" in body
    assert len({rendered["en"], rendered["id"], rendered["zh-Hans"]}) == 3


# ── the counters are a real denominator, not decoration ────────────────────────


def test_revalidated_is_a_denominator_so_a_dark_loop_is_visible(db: Database) -> None:
    """A run of zeroes with no denominator is indistinguishable from a loop that never
    executed — the shape that let a canary alert nobody for 40 consecutive runs."""
    counts = _drain(db, {2: VALID_STARTER, 3: VALID_STARTER, 4: VALID_STARTER})
    assert counts["revalidated"] == 3
    db.unlink_subscriber(2)
    db.unlink_subscriber(3)
    db.unlink_subscriber(4)
    assert _drain(db, {})["revalidated"] == 0


def test_dry_run_revalidates_nothing_and_downgrades_nothing(db: Database) -> None:
    with patch.object(entitlement_drain, "validate_api_key") as v:
        counts = entitlement_drain.drain_entitlement_debits(db.path, dry_run=True)
        v.assert_not_called()
    assert counts["revalidated"] == 0 and counts["downgraded"] == 0
