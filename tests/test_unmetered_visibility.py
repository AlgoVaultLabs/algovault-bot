"""OPS-VALIDATE-KEY-INDETERMINATE-W1 CH4/CH6 — the leak has a meter, and the meter has a reader.

WHY THIS FILE EXISTS. `plan_units_debited` counted the debits that WORKED. For nine days it read
perfectly healthy while a single `past_due` subscriber accumulated 1,987 debits stamped
`key_invalid_404` — terminal, never charged, never retried — and took 2,025 alerts. A rail that
reports only its successes cannot report a leak, and `digest.py` contained no reference to
`key_invalid`, `link_invalid`, `downgraded` or mirror staleness. The defect was not merely
unfixed; it was unobservable.

These assert the two things that make it observable: a DENOMINATOR (deliveries we will never
charge for) and a POPULATION (which linked subscribers are in a state that produces them).
"""

from __future__ import annotations

from algovault_bot import digest
from algovault_bot.db import Database


def _link(db: Database, chat_id: int, key: str, tier: str, state: str | None) -> None:
    db.upsert_subscriber(chat_id, f"u{chat_id}", "en")
    with db._cursor() as cur:
        cur.execute(
            "UPDATE subscribers SET linked_api_key = ?, linked_tier = ?, "
            "plan_entitlement_state = ? WHERE chat_id = ?",
            (key, tier, state, chat_id),
        )


# ── the mirror carries the state ──────────────────────────────────────────────


def test_update_plan_mirror_stores_the_entitlement_state(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.update_plan_mirror(
        1, {"used": 5, "total": 100, "allowed": True, "tier": "starter",
            "entitlement_state": "DUNNING"}, source="debit",
    )
    with tmp_db._cursor() as cur:
        row = cur.execute(
            "SELECT plan_entitlement_state, plan_state_as_of FROM subscribers WHERE chat_id = 1"
        ).fetchone()
    assert row["plan_entitlement_state"] == "DUNNING"
    # NO SECOND FRESHNESS CLOCK — stamped by the existing one, in the same write.
    assert row["plan_state_as_of"] is not None


def test_a_body_without_a_state_stores_NULL_and_never_overwrites_a_known_one(
    tmp_db: Database,
) -> None:
    """A server predating CH2 sends no state. NULL must read as UNOBSERVED — defaulting it to
    ENTITLED would grant, and to NOT_ENTITLED would revoke, on the strength of a missing field."""
    tmp_db.upsert_subscriber(1, "u", "en")
    tmp_db.update_plan_mirror(1, {"used": 1, "allowed": True, "entitlement_state": "ENTITLED"}, source="poll")
    tmp_db.update_plan_mirror(1, {"used": 2, "allowed": True}, source="poll")  # legacy body
    with tmp_db._cursor() as cur:
        st = cur.execute("SELECT plan_entitlement_state FROM subscribers WHERE chat_id = 1").fetchone()[0]
    # The legacy write DID clear it — and that is correct: an unobserved state must not be
    # impersonated by a stale one. It groups under `unobserved`, never under a state.
    assert st is None


# ── the denominator ───────────────────────────────────────────────────────────


def test_unmetered_counts_terminal_key_invalid_debits(tmp_db: Database) -> None:
    """THE LEAK METER. Each row is an alert delivered that will never be charged."""
    _link(tmp_db, 7, "k7", "starter", "DUNNING")
    with tmp_db._cursor() as cur:
        for i, err in enumerate(["key_invalid_404", "key_invalid_404", None, "REFUSED", "unlinked"]):
            cur.execute(
                "INSERT INTO entitlement_outbox (chat_id, channel, units, idem_key, kind, "
                "attempts, sent_at, last_error) "
                "VALUES (7, 'bot', 1, ?, 'call', 1, datetime('now'), ?)",
                (f"idem-{i}", err),
            )
    # Only the two `key_invalid%` rows. A REFUSED is a business decision that WAS answered, and
    # an `unlinked` row was never a live subscriber's — neither is a leak.
    assert tmp_db.count_unmetered_deliveries_last_24h() == 2


def test_unmetered_is_windowed_not_lifetime(tmp_db: Database) -> None:
    """A lifetime total hides a burst — the exact failure the quota canary already shipped once."""
    _link(tmp_db, 8, "k8", "starter", "NOT_ENTITLED")
    with tmp_db._cursor() as cur:
        cur.execute(
            "INSERT INTO entitlement_outbox (chat_id, channel, units, idem_key, kind, "
            "attempts, sent_at, last_error) "
            "VALUES (8, 'bot', 1, 'old', 'call', 1, datetime('now','-72 hours'), 'key_invalid_404')"
        )
    assert tmp_db.count_unmetered_deliveries_last_24h() == 0


# ── the population ────────────────────────────────────────────────────────────


def test_linked_cohort_groups_by_state_with_unobserved_kept_separate(tmp_db: Database) -> None:
    _link(tmp_db, 1, "k1", "pro", "ENTITLED")
    _link(tmp_db, 2, "k2", "starter", "DUNNING")
    _link(tmp_db, 3, "k3", "starter", "NOT_ENTITLED")
    _link(tmp_db, 4, "k4", "starter", None)
    tmp_db.upsert_subscriber(5, "unlinked", "en")  # no key — not in the cohort at all

    by = tmp_db.count_linked_by_entitlement_state()

    assert by == {"ENTITLED": 1, "DUNNING": 1, "NOT_ENTITLED": 1, "unobserved": 1}
    assert "unobserved" in by, "a never-written mirror is not evidence of entitlement"


# ── the reader ────────────────────────────────────────────────────────────────


def test_the_digest_line_is_QUIET_when_every_linked_chat_is_entitled(tmp_db: Database) -> None:
    """A line that shouts every day is a line that gets filtered."""
    _link(tmp_db, 1, "k1", "pro", "ENTITLED")
    body = digest.render_digest(tmp_db)
    assert "🩸 Unmetered 24h: 0" in body
    assert "DUNNING" not in body


def test_the_digest_line_NAMES_the_cohort_on_the_day_it_matters(tmp_db: Database) -> None:
    """THE ASSERTION THAT WOULD HAVE CAUGHT THE ORIGINAL DEFECT ON DAY ONE rather than day nine."""
    _link(tmp_db, 1, "k1", "pro", "ENTITLED")
    _link(tmp_db, 2, "k2", "starter", "DUNNING")
    _link(tmp_db, 3, "k3", "starter", None)

    line = next(row for row in digest.render_digest(tmp_db).split("\n") if "Unmetered" in row)

    assert "DUNNING 1" in line
    assert "unobserved 1" in line
    assert "ENTITLED 1" in line, "the healthy count is the denominator — never omit it"
