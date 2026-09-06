"""GROWTH-TG-STARS-DEMAND-PROBE-W1 — the ⭐ demand probe.

This wave measures whether anyone wants a Telegram-Stars checkout BEFORE one is built, so the
tests exist to protect two properties that are easy to lose and expensive to lose quietly:

  1. **The probe takes no payment and never will.** A test greps the whole package for the
     payment verbs, so the day someone "just wires it up" the suite says no.
  2. **The line renders at ZERO.** An omitted line is indistinguishable from a broken handler, a
     dead button or a wave that never shipped — and absence reads as "no demand", which is the
     one wrong conclusion this instrument exists to prevent.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import CallbackQueryHandler

from algovault_bot import keyboards, messages
from algovault_bot.db import STARS_INTEREST_KIND, Database
from algovault_bot.digest import (
    STARS_PROBE_TRIGGER_USERS,
    STARS_PROBE_WINDOW_DAYS,
    render_digest,
)
from algovault_bot.handlers import register_handlers
from algovault_bot.quota import _fallback_ladder

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731
STARS_LABEL = "⭐ Pay with Stars"


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "stars.db"))


def _rows(kb) -> list[list[str]]:  # noqa: ANN001
    return [[b.text for b in row] for row in kb.inline_keyboard]


def _stars_line(db: Database) -> str:
    hits = [ln for ln in render_digest(db).splitlines() if "Stars interest" in ln]
    assert len(hits) == 1, f"expected exactly one Stars line, got {hits}"
    return hits[0]


# ── (a) the row rides every rendering the picker returns ─────────────────────


def test_the_star_row_is_last_on_the_full_picker() -> None:
    kb = keyboards.plan_picker_kb(_fallback_ladder(), "upgrade_command")
    assert kb is not None
    assert _rows(kb) == [
        ["Starter · $9.99/mo", "Starter · $39.90/6mo"],
        ["Pro · $49/mo", "Pro · $129/6mo"],
        [STARS_LABEL],
    ]


def test_the_star_row_rides_the_PRO_ONLY_picker_too() -> None:
    """A paying Starter asking for Stars is the most valuable row in the table: someone who has
    already proved they will pay, naming the rail they would rather pay on."""
    kb = keyboards.plan_picker_kb(_fallback_ladder(), "plan_wall", above_tier="starter")
    assert kb is not None
    assert _rows(kb) == [["Pro · $49/mo", "Pro · $129/6mo"], [STARS_LABEL]]


@pytest.mark.parametrize("tier", ["pro", "enterprise"])
def test_no_picker_means_no_star_row(tier: str) -> None:
    """The probe rides the picker; it does not resurrect one. A top-of-ladder user gets the
    ratified sentence and nothing to tap."""
    assert keyboards.plan_picker_kb(_fallback_ladder(), "plan_wall", above_tier=tier) is None


def test_the_picker_TEXT_is_untouched_by_this_wave() -> None:
    """AC1. This wave adds a button; the copy above it is the picker wave's, byte for byte."""
    body = messages.plan_picker_text(_fallback_ladder())
    assert body.startswith("⬆️ Upgrade — pick a plan")
    assert body.endswith("Same key works in this bot and in the API. Card checkout via Stripe.")
    assert "Stars" not in body


def test_the_callback_payload_carries_the_campaign_and_respects_the_64_byte_cap() -> None:
    """Telegram caps `callback_data` at 64 BYTES. The campaign is a nice-to-have, so it is
    DROPPED rather than truncated when it will not fit — a truncated campaign is a wrong
    campaign, and a wrong one is worse than none in a table nobody will re-derive."""
    kb = keyboards.plan_picker_kb(_fallback_ladder(), "quota_exhausted_push")
    assert kb is not None
    data = kb.inline_keyboard[-1][0].callback_data
    assert data == "stars:interest:quota_exhausted_push"
    assert len(data.encode()) <= 64

    long_kb = keyboards.plan_picker_kb(_fallback_ladder(), "c" * 200)
    assert long_kb is not None
    fallback = long_kb.inline_keyboard[-1][0].callback_data
    assert fallback == "stars:interest"
    assert len(fallback.encode()) <= 64


def test_the_router_accepts_both_payload_shapes(db: Database) -> None:
    """Whatever the builder can emit, the router must match — otherwise the tap is silently
    swallowed by python-telegram-bot and the counter reads zero forever."""
    app = _CapturingApp()
    register_handlers(app, db)
    pats = [
        h.pattern for h in app.captured
        if isinstance(h, CallbackQueryHandler) and h.pattern is not None
    ]
    assert any(p.match("stars:interest") for p in pats)
    assert any(p.match("stars:interest:quota_exhausted_push") for p in pats)
    # ...and it must not swallow the menu namespace
    assert not any(
        p.match("stars:interest") and p.match("mnu:help") for p in pats
    )


# ── (b) one row per user, taps accumulate, first_at is first ─────────────────


def test_a_second_tap_increments_taps_and_leaves_first_at_alone(db: Database) -> None:
    first = (NOW() - timedelta(days=2)).isoformat()
    later = NOW().isoformat()
    db.record_interest(STARS_INTEREST_KIND, 7, "upgrade_command", first)
    db.record_interest(STARS_INTEREST_KIND, 7, "plan_wall", later)
    with db._cursor() as cur:
        cur.execute(
            "SELECT taps, first_at, last_at, campaign FROM interest_events "
            "WHERE kind = ? AND chat_id = 7",
            (STARS_INTEREST_KIND,),
        )
        taps, first_at, last_at, campaign = cur.fetchone()
    assert taps == 2
    assert first_at == first, "first_at must record when they FIRST asked"
    assert last_at == later
    # first-touch, not last-touch: the surface that converted them is the interesting one
    assert campaign == "upgrade_command"


def test_ten_taps_by_one_user_are_one_user(db: Database) -> None:
    """The product is DISTINCT people. A row-per-tap table would let one enthusiast look like
    ten, which is precisely the reading this instrument exists to prevent."""
    for _ in range(10):
        db.record_interest(STARS_INTEREST_KIND, 1, None, NOW().isoformat())
    assert db.count_interest(STARS_INTEREST_KIND, "1970-01-01T00:00:00+00:00") == (1, 10)


def test_a_first_tap_with_no_campaign_can_be_filled_in_later(db: Database) -> None:
    db.record_interest(STARS_INTEREST_KIND, 3, None, NOW().isoformat())
    db.record_interest(STARS_INTEREST_KIND, 3, "help_message", NOW().isoformat())
    with db._cursor() as cur:
        cur.execute("SELECT campaign FROM interest_events WHERE chat_id = 3")
        assert cur.fetchone()[0] == "help_message"


# ── (c) the window ───────────────────────────────────────────────────────────


def test_count_interest_excludes_rows_outside_the_window(db: Database) -> None:
    stale = (NOW() - timedelta(days=STARS_PROBE_WINDOW_DAYS + 1)).isoformat()
    fresh = NOW().isoformat()
    db.record_interest(STARS_INTEREST_KIND, 100, None, stale)
    db.record_interest(STARS_INTEREST_KIND, 200, None, fresh)
    since = (NOW() - timedelta(days=STARS_PROBE_WINDOW_DAYS)).isoformat()
    assert db.count_interest(STARS_INTEREST_KIND, since) == (1, 1)


def test_the_window_keys_on_LAST_tap_so_re_engaged_demand_stays_current(db: Database) -> None:
    """A user who first tapped 40 days ago and tapped again today is CURRENT demand. Keying the
    window on `first_at` would silently retire live interest — and a trigger that under-counts
    is a rail that never gets built."""
    db.record_interest(
        STARS_INTEREST_KIND, 5, None, (NOW() - timedelta(days=40)).isoformat()
    )
    db.record_interest(STARS_INTEREST_KIND, 5, None, NOW().isoformat())
    since = (NOW() - timedelta(days=STARS_PROBE_WINDOW_DAYS)).isoformat()
    assert db.count_interest(STARS_INTEREST_KIND, since) == (1, 2)


def test_a_different_kind_is_a_different_probe(db: Database) -> None:
    """`kind` is the extension point: a second probe is one more value and ZERO schema."""
    db.record_interest(STARS_INTEREST_KIND, 1, None, NOW().isoformat())
    db.record_interest("gram_wallet", 1, None, NOW().isoformat())
    since = "1970-01-01T00:00:00+00:00"
    assert db.count_interest(STARS_INTEREST_KIND, since) == (1, 1)
    assert db.count_interest("gram_wallet", since) == (1, 1)


# ── (d) the digest line, at zero / below / at the trigger ────────────────────
#
# 🛑 PROVEN ABLE TO FAIL (R5d, 2026-09-06). `_stars_interest_line`'s comparison was flipped from
# `>=` to `>`. `test_the_trigger_fires_at_exactly_the_threshold` went RED on the boundary case
# while the 0- and 9-user cases stayed green — which is the point: an off-by-one on a
# pre-registered trigger is invisible to every test that does not sit exactly on it. Restored
# byte-identically, `src/algovault_bot/digest.py` sha256
# 0c4004b7726a78b1bca668231365b1a630c0c652be302dc398d54258e664b3a5 before and after.


def test_the_line_renders_at_zero(db: Database) -> None:
    """An absent line is indistinguishable from a broken probe, and absence reads as no demand."""
    assert _stars_line(db) == "⭐ Stars interest: 0 users · 0 taps (24h: +0)"


def test_below_the_threshold_there_is_no_suffix(db: Database) -> None:
    for i in range(STARS_PROBE_TRIGGER_USERS - 1):
        db.record_interest(STARS_INTEREST_KIND, i, None, NOW().isoformat())
    line = _stars_line(db)
    assert f"{STARS_PROBE_TRIGGER_USERS - 1} users" in line
    assert "STARS_PROBE_TRIGGER" not in line


def test_the_trigger_fires_at_exactly_the_threshold(db: Database) -> None:
    """The boundary is the whole test. `>= N` and `> N` agree on every count except this one."""
    for i in range(STARS_PROBE_TRIGGER_USERS):
        db.record_interest(STARS_INTEREST_KIND, i, None, NOW().isoformat())
    assert _stars_line(db).endswith(" → STARS_PROBE_TRIGGER=FIRED")


def test_taps_and_users_are_reported_separately(db: Database) -> None:
    """Two different numbers, and conflating them is how three taps become three users."""
    db.record_interest(STARS_INTEREST_KIND, 1, None, NOW().isoformat())
    db.record_interest(STARS_INTEREST_KIND, 1, None, NOW().isoformat())
    db.record_interest(STARS_INTEREST_KIND, 2, None, NOW().isoformat())
    assert _stars_line(db) == "⭐ Stars interest: 2 users · 3 taps (24h: +2)"


def test_stale_demand_leaves_the_30d_figure_and_the_24h_delta(db: Database) -> None:
    db.record_interest(
        STARS_INTEREST_KIND, 1, None, (NOW() - timedelta(days=40)).isoformat()
    )
    db.record_interest(
        STARS_INTEREST_KIND, 2, None, (NOW() - timedelta(days=10)).isoformat()
    )
    db.record_interest(STARS_INTEREST_KIND, 3, None, NOW().isoformat())
    assert _stars_line(db) == "⭐ Stars interest: 2 users · 2 taps (24h: +1)"


# ── (e) the handler, through the real registration ───────────────────────────


class _CapturingApp:
    def __init__(self) -> None:
        self.captured: list = []

    def add_handler(self, handler, *a, **k) -> None:  # noqa: ANN001
        self.captured.append(handler)


class _FakeQuery:
    def __init__(self, data: str, chat_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=chat_id, username="tester", language_code="en")
        self.answers: list[dict] = []
        #: Anything sent here would be a MESSAGE in the chat — see the assertion below.
        self.message = None

    async def answer(self, *a, **k) -> None:  # noqa: ANN001
        self.answers.append(k)


def _stars_handler(db: Database):  # noqa: ANN001
    app = _CapturingApp()
    register_handlers(app, db)
    for h in app.captured:
        if isinstance(h, CallbackQueryHandler) and h.pattern is not None:
            if h.pattern.match("stars:interest:upgrade_command"):
                return h.callback
    raise AssertionError("the stars:interest handler is not registered")


def _tap(db: Database, chat_id: int = 909, data: str = "stars:interest:upgrade_command"):  # noqa: ANN001
    q = _FakeQuery(data, chat_id)
    asyncio.run(_stars_handler(db)(SimpleNamespace(callback_query=q), SimpleNamespace()))
    return q


def test_a_tap_answers_the_ratified_toast_and_writes_one_row(db: Database) -> None:
    q = _tap(db)
    assert q.answers == [{"text": messages.STARS_INTEREST_TOAST, "show_alert": False}]
    assert db.count_interest(STARS_INTEREST_KIND, "1970-01-01T00:00:00+00:00") == (1, 1)
    with db._cursor() as cur:
        cur.execute("SELECT campaign FROM interest_events WHERE chat_id = 909")
        assert cur.fetchone()[0] == "upgrade_command"


def test_the_toast_is_a_TOAST_and_never_a_second_message(db: Database) -> None:
    """🛑 A probe that replies in-channel becomes a nag, and the surest way to poison this
    measurement is teaching users that tapping ⭐ costs them a notification. `q.message` is left
    as None on purpose: if the handler ever reaches for it to send something, this raises."""
    q = _tap(db)
    assert q.message is None
    assert len(q.answers) == 1


def test_a_repeat_tap_is_the_same_user(db: Database) -> None:
    _tap(db)
    _tap(db)
    assert db.count_interest(STARS_INTEREST_KIND, "1970-01-01T00:00:00+00:00") == (1, 2)


def test_a_payload_without_a_campaign_still_records_the_interest(db: Database) -> None:
    """The COUNT is the product. A payload too long to carry a campaign must never cost us the
    row it exists to create."""
    _tap(db, chat_id=42, data="stars:interest")
    assert db.count_interest(STARS_INTEREST_KIND, "1970-01-01T00:00:00+00:00") == (1, 1)
    with db._cursor() as cur:
        cur.execute("SELECT campaign FROM interest_events WHERE chat_id = 42")
        assert cur.fetchone()[0] is None


def test_the_tap_registers_the_subscriber(db: Database) -> None:
    """A ⭐ tap can be someone's first interaction; the row must exist before it is counted."""
    _tap(db, chat_id=555)
    assert db.get_subscriber(555) is not None


# ── the standing prohibition ─────────────────────────────────────────────────


def test_the_probe_contains_NO_PAYMENT_CODE_anywhere(db: Database) -> None:
    """AC4, as a gate rather than a promise.

    This button records an intention. The day it learns to charge, that is a wave with a payment
    provider, a refund story and a Stars-amount SoT — not a quiet edit to a demand probe.
    """
    import re

    src = Path(__file__).resolve().parents[1] / "src" / "algovault_bot"
    banned = re.compile(r"send_invoice|create_invoice_link|\bXTR\b|successful_payment|pre_checkout")
    offenders = [
        f.name for f in src.glob("*.py")
        if banned.search(
            "\n".join(
                ln for ln in f.read_text(encoding="utf-8").splitlines()
                if not ln.lstrip().startswith("#")
            )
        )
    ]
    assert offenders == [], f"payment code reached the probe: {offenders}"
