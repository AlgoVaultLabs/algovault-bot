"""GROWTH-TG-PLAN-PICKER-W1 R5 — the four-SKU plan picker.

The property that matters most here is that NOTHING IS HAND-TYPED. Every label and every URL is
derived from the `Ladder`, which `resolve_ladder` derives from the server's published ladder with
pinned per-field fallbacks. So the fixtures below use DELIBERATELY WRONG figures — a $77 Starter,
a $404 Pro — because a test built on the real prices passes just as happily against a hardcoded
renderer, and would have caught nothing.

The wall-notice assertions run against the REAL `refuse_and_notify` with an injected `send`
double, and assert on that double's SECOND argument. Its ability to fail was proven by hand: see
the module docstring note under `test_the_free_wall_carries_the_full_picker`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import Message
from telegram.ext import CallbackQueryHandler, CommandHandler

from algovault_bot import keyboards, messages
from algovault_bot.db import Database
from algovault_bot.handlers import register_handlers
from algovault_bot.quota import (
    PRO_DAILY_CALLS,
    PRO_MONTHLY_CALLS,
    PRO_PRICE_6MONTH_USD,
    PRO_PRICE_USD,
    STARTER_DAILY_CALLS,
    STARTER_MONTHLY_CALLS,
    STARTER_PRICE_6MONTH_USD,
    STARTER_PRICE_USD,
    Ladder,
    consume_quota,
    evaluate_delivery,
    get_quota_state,
    picker_above_tier,
    refuse_and_notify,
    resolve_ladder,
)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

#: Figures nobody sells, on purpose — see the module docstring.
FIXTURE = Ladder(
    free_monthly=7,
    free_daily=3,
    starter_price_usd=77.0,
    starter_monthly_calls=7777,
    starter_daily_calls=777,
    starter_price_usd_6month=77.50,
    pro_price_usd=404.0,
    pro_monthly_calls=40404,
    pro_daily_calls=4040,
    pro_price_usd_6month=444.40,
    source="mirror",
)


def _urls(kb) -> list[str]:  # noqa: ANN001
    return [b.url for row in kb.inline_keyboard for b in row]


def _labels(kb) -> list[str]:  # noqa: ANN001
    return [b.text for row in kb.inline_keyboard for b in row]


# ── (a) four buttons, exact labels, exact URLs ───────────────────────────────


def test_the_picker_renders_four_skus_with_derived_labels_and_urls() -> None:
    kb = keyboards.plan_picker_kb(FIXTURE, "upgrade_command", "geo")
    assert kb is not None
    # Two rows of two: one ROW per plan, one COLUMN per term. Stars adds a column, not a copy.
    assert [len(r) for r in kb.inline_keyboard] == [2, 2]
    assert _labels(kb) == [
        "Starter · $77/mo",
        "Starter · $77.50/6mo",
        "Pro · $404/mo",
        "Pro · $444.40/6mo",
    ]
    assert _urls(kb) == [
        "https://api.algovault.com/signup?plan=starter"
        "&utm_source=tg_bot&utm_campaign=upgrade_command&utm_medium=geo",
        "https://api.algovault.com/signup?plan=starter&interval=6month"
        "&utm_source=tg_bot&utm_campaign=upgrade_command&utm_medium=geo",
        "https://api.algovault.com/signup?plan=pro"
        "&utm_source=tg_bot&utm_campaign=upgrade_command&utm_medium=geo",
        "https://api.algovault.com/signup?plan=pro&interval=6month"
        "&utm_source=tg_bot&utm_campaign=upgrade_command&utm_medium=geo",
    ]


def test_only_the_prepay_buttons_carry_an_interval() -> None:
    """`interval=month` emits NOTHING — that is what keeps the starter/month URL byte-identical
    to the ~400 historical `signup_attribution` rows minted before this wave."""
    kb = keyboards.plan_picker_kb(FIXTURE, "upgrade_command")
    assert kb is not None
    urls = _urls(kb)
    assert [u for u in urls if "interval=" in u] == [urls[1], urls[3]]
    assert all("interval=month" not in u for u in urls)


def test_absence_of_a_source_emits_no_utm_medium_at_all() -> None:
    kb = keyboards.plan_picker_kb(FIXTURE, "upgrade_command")
    assert kb is not None
    assert all("utm_medium" not in u for u in _urls(kb))


def test_the_picker_never_offers_enterprise() -> None:
    """Enterprise has no self-serve Stripe Price. A button for it would 4xx a paying prospect."""
    kb = keyboards.plan_picker_kb(FIXTURE, "upgrade_command")
    assert kb is not None
    assert all("plan=enterprise" not in u for u in _urls(kb))
    assert all("Enterprise" not in t for t in _labels(kb))


# ── (b) the above_tier projection ────────────────────────────────────────────


def test_a_starter_is_offered_pro_only() -> None:
    kb = keyboards.plan_picker_kb(FIXTURE, "plan_wall", above_tier="starter")
    assert kb is not None
    assert [len(r) for r in kb.inline_keyboard] == [2]
    assert _labels(kb) == ["Pro · $404/mo", "Pro · $444.40/6mo"]
    assert all("plan=starter" not in u for u in _urls(kb))


@pytest.mark.parametrize("tier", ["pro", "enterprise"])
def test_the_top_of_the_ladder_gets_no_keyboard(tier: str) -> None:
    """None, not an empty keyboard: a button that leads nowhere is worse than no button."""
    assert keyboards.plan_picker_kb(FIXTURE, "plan_wall", above_tier=tier) is None


def test_the_text_and_the_keyboard_agree_on_every_branch() -> None:
    """The two builders take the SAME argument and must stop offering at the SAME point.

    A picker that renders "pick a plan" over no buttons, or a Pro row over "you are on the top
    plan", is the kind of contradiction two independently-branching renderers produce.
    """
    for tier in (None, "starter", "pro", "enterprise"):
        text = messages.plan_picker_text(FIXTURE, above_tier=tier)
        kb = keyboards.plan_picker_kb(FIXTURE, "upgrade_command", above_tier=tier)
        offers_buttons = kb is not None
        offers_prices = "Pro · " in text
        assert offers_buttons is offers_prices, tier
    assert messages.plan_picker_text(FIXTURE, above_tier="pro") == messages.TOP_SELF_SERVE_EN


def test_the_text_derives_every_figure_and_states_the_bot_unit() -> None:
    body = messages.plan_picker_text(FIXTURE)
    assert "Starter · 7,777 alerts/mo · 777/day" in body
    assert "Pro · 40,404 alerts/mo · 4,040/day" in body
    # The Telegram-bot carve-out: this bot meters a DELIVERED ALERT, never an API "call".
    assert "calls/mo" not in body
    # No HOLD claim, and no urgency or scarcity framing on either prepay price (brand-facts.md).
    assert "HOLD" not in body
    for banned in ("limited", "hurry", "only", "ends", "last chance", "save"):
        assert banned not in body.lower(), banned
    assert messages.plan_picker_text(FIXTURE, above_tier="starter").startswith("⬆️ Move up to Pro")


# ── (c) both resolve_ladder branches, through a real DB ──────────────────────


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "picker.db"))


def test_an_unmirrored_db_serves_the_pinned_ladder(db: Database) -> None:
    """The fallback SERVES. A ladder we could not read is never a reason to render nothing."""
    lad = resolve_ladder(db)
    assert lad.source == "fallback"
    kb = keyboards.plan_picker_kb(lad, "upgrade_command")
    assert kb is not None
    assert _labels(kb) == [
        f"Starter · {messages._usd(STARTER_PRICE_USD)}/mo",
        f"Starter · {messages._usd(STARTER_PRICE_6MONTH_USD)}/6mo",
        f"Pro · {messages._usd(PRO_PRICE_USD)}/mo",
        f"Pro · {messages._usd(PRO_PRICE_6MONTH_USD)}/6mo",
    ]
    body = messages.plan_picker_text(lad)
    assert f"{STARTER_MONTHLY_CALLS:,} alerts/mo · {STARTER_DAILY_CALLS:,}/day" in body
    assert f"{PRO_MONTHLY_CALLS:,} alerts/mo · {PRO_DAILY_CALLS:,}/day" in body


def test_a_mirrored_db_serves_the_MIRRORED_ladder(db: Database) -> None:
    """The whole point: move a price on the server and every button follows, with no bot deploy."""
    db.upsert_free_tier_ladder(
        free_monthly=7, free_daily=3,
        starter_price_usd=77.0, starter_monthly_calls=7777,
        fetched_at=NOW().isoformat(),
        starter_daily_calls=777, starter_price_usd_6month=77.50,
        pro_price_usd=404.0, pro_monthly_calls=40404,
        pro_daily_calls=4040, pro_price_usd_6month=444.40,
    )
    lad = resolve_ladder(db)
    assert lad.source == "mirror"
    assert lad == FIXTURE
    kb = keyboards.plan_picker_kb(lad, "upgrade_command")
    assert kb is not None
    assert _labels(kb)[3] == "Pro · $444.40/6mo"


def test_a_STALE_mirror_falls_back_rather_than_rendering_a_stale_price(db: Database) -> None:
    db.upsert_free_tier_ladder(
        free_monthly=7, free_daily=3,
        starter_price_usd=77.0, starter_monthly_calls=7777,
        fetched_at=(NOW() - timedelta(days=30)).isoformat(),
        pro_price_usd=404.0,
    )
    lad = resolve_ladder(db)
    assert lad.source == "fallback"
    assert lad.pro_price_usd == PRO_PRICE_USD


# ── (d) a mirror written before the server field existed ─────────────────────


def test_a_pre_prepay_mirror_row_degrades_per_field_to_the_pinned_total(db: Database) -> None:
    """The deploy-order case, end to end: bot deployed, signal-MCP not yet (or rolled back).

    `parse_ladder` reads the absent prepay field as None, the drain writes NULL, and the picker
    must render the PINNED total — never `$0/6mo`, and never no button at all.
    """
    db.upsert_free_tier_ladder(
        free_monthly=7, free_daily=3,
        starter_price_usd=77.0, starter_monthly_calls=7777,
        fetched_at=NOW().isoformat(),
        starter_daily_calls=777,
        # the two prepay totals absent, exactly as a pre-R1 endpoint yields
        pro_price_usd=404.0, pro_monthly_calls=40404, pro_daily_calls=4040,
    )
    lad = resolve_ladder(db)
    assert lad.source == "mirror"
    assert lad.starter_price_usd_6month == STARTER_PRICE_6MONTH_USD
    assert lad.pro_price_usd_6month == PRO_PRICE_6MONTH_USD
    # ...while everything the row DID carry is still mirrored — degradation is per FIELD.
    assert lad.starter_price_usd == 77.0 and lad.pro_monthly_calls == 40404
    kb = keyboards.plan_picker_kb(lad, "upgrade_command")
    assert kb is not None
    assert "$0" not in " ".join(_labels(kb))
    assert _labels(kb)[1] == f"Starter · {messages._usd(STARTER_PRICE_6MONTH_USD)}/6mo"


# ── (e) the wall notice carries the picker ───────────────────────────────────
#
# 🛑 PROVEN ABLE TO FAIL (GROWTH-TG-PLAN-PICKER-W1 R5g, 2026-09-06). `refuse_and_notify` was
# temporarily edited to call `send(text, None)` on both lanes. MEASURED: the two tests below
# went RED (2 failed / 20 passed in this file) while the ENTIRE REST of the suite stayed green
# — 1017 passed, 1 skipped, zero failures outside this file. That second half is the finding,
# not a footnote: nothing else in the codebase observes the notice's second argument, so these
# two assertions are the only thing standing between a shipped picker and a silently
# keyboard-less wall. The edit was reverted and `src/algovault_bot/quota.py` restored
# byte-identically, verified by sha256
# 1778d64c68cf04f3c627beb6e7c10dafa3cacf803178ccf82dd7b4263283a99e before and after.


def _wall_db(tmp_path: Path) -> Database:
    d = Database(str(tmp_path / "wall.db"))
    d.upsert_subscriber(1, "free", "en")
    d.upsert_subscriber(2, "paid", "en")
    d.link_subscriber(2, "av_live_k", "starter")
    return d


def _capture_wall(db: Database, chat_id: int) -> tuple[str, object]:
    """Run the REAL refusal seam and return what `send` was actually called with."""
    seen: list[tuple[str, object]] = []

    async def _send(text: str, markup=None) -> bool:  # noqa: ANN001
        seen.append((text, markup))
        return True

    asyncio.run(
        refuse_and_notify(
            db, chat_id, "watch", send=_send, decision=evaluate_delivery(db, chat_id)
        )
    )
    assert seen, "the wall sent nothing at all — the fixture is not walled"
    return seen[0]


def test_the_free_wall_carries_the_full_picker(tmp_path: Path) -> None:
    db = _wall_db(tmp_path)
    consume_quota(db, 1, resolve_ladder(db).free_monthly)
    text, markup = _capture_wall(db, 1)
    assert markup is not None, "the free wall must carry the picker"
    assert [len(r) for r in markup.inline_keyboard] == [2, 2]
    assert all("utm_campaign=quota_exhausted_push" in u for u in _urls(markup))
    # The ≤300-char BODY is unchanged — this wave attached a keyboard, it did not rewrite copy.
    assert len(text) <= 300


def test_a_walled_STARTER_is_offered_pro_only(tmp_path: Path) -> None:
    db = _wall_db(tmp_path)
    db.update_plan_mirror(
        2,
        {
            "used": 10000, "total": 10000, "allowed": False, "limit": "monthly",
            "period_start": (NOW() - timedelta(days=3)).isoformat(),
            "daily_day": NOW().strftime("%Y-%m-%d"),
            # R4b: `tier` is what makes the mirror AUTHORITATIVE, and the live server has sent
            # it in every consume/state 200 all along (both linked rows on signal-1 carry a
            # populated plan_tier). Without it the mirror is fresh but tier-less, the effective
            # tier degrades to the `link` source, and `picker_above_tier` correctly declines to
            # withhold a rung — so omitting it here tested a shape the server never sends.
            "tier": "starter",
            "next_plan": {"id": "pro", "label": "Pro", "monthly_calls": 100000,
                          "price_usd": 49,
                          "signup_url": "https://api.algovault.com/signup?plan=pro"},
        },
        source="debit",
    )
    _text, markup = _capture_wall(db, 2)
    assert markup is not None, "a walled Starter must be offered the rung above them"
    assert [len(r) for r in markup.inline_keyboard] == [2]
    assert all("plan=pro" in u for u in _urls(markup))
    assert all("utm_campaign=plan_wall" in u for u in _urls(markup))


# ── the real handlers, through the real registration ─────────────────────────


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list = []

    async def reply_text(self, text, reply_markup=None, **kw):  # noqa: ANN001
        self.replies.append((text, reply_markup))


class _FakeUpdate:
    def __init__(self, chat_id: int = 909) -> None:
        self.message = _FakeMessage()
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(username="tester", language_code="en")


class _CallbackMessage(Message):
    """A real `telegram.Message` subclass, on purpose.

    `_on_menu_callback` guards its reply with `isinstance(q.message, Message)`. A duck-typed
    double therefore makes every assertion here VACUOUS — the handler skips the send, the test
    reads "no reply", and the obvious next move is to weaken the assertion rather than the
    double. Subclassing keeps the guard exercised as production runs it.
    """

    def __init__(self, chat_id: int) -> None:
        super().__init__(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=SimpleNamespace(id=chat_id, type="private"),  # type: ignore[arg-type]
        )
        object.__setattr__(self, "replies", [])

    async def reply_text(self, text, reply_markup=None, **kw):  # noqa: ANN001
        self.replies.append((text, reply_markup))


class _FakeQuery:
    def __init__(self, data: str, chat_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=chat_id, username="tester", language_code="en")
        self.message = _CallbackMessage(chat_id)
        self.answered = False

    async def answer(self, *a, **k) -> None:  # noqa: ANN001
        self.answered = True


class _CapturingApp:
    def __init__(self) -> None:
        self.captured: list = []

    def add_handler(self, handler, *a, **k) -> None:  # noqa: ANN001
        self.captured.append(handler)


def _handlers(db: Database) -> list:
    app = _CapturingApp()
    register_handlers(app, db)
    return app.captured


def _command(db: Database, name: str):  # noqa: ANN001
    for h in _handlers(db):
        if isinstance(h, CommandHandler) and name in h.commands:
            return h.callback
    raise AssertionError(f"/{name} handler not registered")


def test_the_upgrade_command_is_registered_and_renders_the_picker(db: Database) -> None:
    upd = _FakeUpdate()
    asyncio.run(_command(db, "upgrade")(upd, SimpleNamespace(args=[], user_data={})))
    assert upd.message.replies, "/upgrade sent nothing"
    text, markup = upd.message.replies[0]
    assert text.startswith("⬆️ Upgrade — pick a plan")
    assert markup is not None
    assert all("utm_campaign=upgrade_command" in u for u in _urls(markup))


def test_upgrade_is_in_the_curated_command_menu_before_help() -> None:
    from algovault_bot.handlers import CURATED_COMMANDS

    names = [c.command for c in CURATED_COMMANDS]
    assert "upgrade" in names
    assert names.index("upgrade") < names.index("help")
    assert dict(zip(names, (c.description for c in CURATED_COMMANDS)))["upgrade"] == (
        "Plans and pricing — upgrade in two taps"
    )


def _menu_router(db: Database):  # noqa: ANN001
    for h in _handlers(db):
        if isinstance(h, CallbackQueryHandler) and h.pattern is not None:
            if h.pattern.match("mnu:upgrade"):
                return h
    raise AssertionError("mnu:upgrade is not routed — the pattern never learned it")


def test_the_menu_upgrade_button_is_routed_and_opens_the_picker(db: Database) -> None:
    router = _menu_router(db)
    q = _FakeQuery("mnu:upgrade", 909)
    asyncio.run(router.callback(SimpleNamespace(callback_query=q), SimpleNamespace()))
    assert q.answered
    assert q.message.replies, "mnu:upgrade replied with nothing"
    text, markup = q.message.replies[0]
    assert text.startswith("⬆️ Upgrade — pick a plan")
    assert markup is not None
    assert all("utm_campaign=start_welcome" in u for u in _urls(markup))


def test_mnu_help_now_carries_the_picker_too(db: Database) -> None:
    """Closes the inconsistency this wave found: `/help` typed as a command carried the upgrade
    CTA and the SAME help text reached through the menu button did not, so which surface a user
    tapped decided whether they were ever shown a price."""
    router = _menu_router(db)
    q = _FakeQuery("mnu:help", 909)
    asyncio.run(router.callback(SimpleNamespace(callback_query=q), SimpleNamespace()))
    _text, markup = q.message.replies[0]
    assert markup is not None
    assert all("utm_campaign=help_message" in u for u in _urls(markup))


def test_the_help_command_carries_the_picker(db: Database) -> None:
    upd = _FakeUpdate()
    asyncio.run(_command(db, "help")(upd, SimpleNamespace(args=[], user_data={})))
    _text, markup = upd.message.replies[0]
    assert markup is not None
    assert [len(r) for r in markup.inline_keyboard] == [2, 2]
    assert all("utm_campaign=help_message" in u for u in _urls(markup))


def test_every_money_surface_renders_the_SAME_picker(db: Database) -> None:
    """AC3, as one assertion: /upgrade, /help, the /start menu's ⬆️ Upgrade and the wall all
    build from ONE builder, so their keyboards differ only in the campaign tag."""
    shapes = []
    upd = _FakeUpdate()
    asyncio.run(_command(db, "upgrade")(upd, SimpleNamespace(args=[], user_data={})))
    shapes.append(upd.message.replies[0][1])
    upd2 = _FakeUpdate()
    asyncio.run(_command(db, "help")(upd2, SimpleNamespace(args=[], user_data={})))
    shapes.append(upd2.message.replies[0][1])
    q = _FakeQuery("mnu:upgrade", 909)
    asyncio.run(_menu_router(db).callback(SimpleNamespace(callback_query=q), SimpleNamespace()))
    shapes.append(q.message.replies[0][1])
    assert all(kb is not None for kb in shapes)
    assert len({tuple(_labels(kb)) for kb in shapes}) == 1, "the four labels must be identical"
    assert len({tuple(len(r) for r in kb.inline_keyboard) for kb in shapes}) == 1


# ── (f) the byte-identity guard still holds through the new composer ─────────


def test_the_starter_month_url_is_byte_identical_through_both_paths() -> None:
    """`signup_url` is a PROJECTION of `plan_signup_url`, not a second composer — so the URL ~400
    historical `signup_attribution` rows were minted with must come out of both, unchanged."""
    assert messages.signup_url("quota_100") == messages.plan_signup_url(
        "starter", "month", "quota_100"
    )
    assert messages.signup_url("quota_100") == (
        "api.algovault.com/signup?plan=starter&utm_source=tg_bot&utm_campaign=quota_100"
    )


# ── R4b — only a FRESH MIRROR may withhold a rung ────────────────────────────
#
# The three cases below are the three LIVE shapes on signal-1 at 2026-09-06 15:5xZ, transcribed
# from the subscribers table rather than invented. The middle one is the defect the operator
# caught: R4 shipped it offering Pro-only to a lapsed subscriber.


def _linked(db: Database, chat_id: int, *, linked: str, mirror_tier: str | None,
            mirror_age_min: float = 1.0) -> None:
    db.upsert_subscriber(chat_id, "u", "en")
    db.link_subscriber(chat_id, f"av_live_{chat_id}", linked)
    if mirror_tier is not None:
        db.update_plan_mirror(
            chat_id,
            {
                "used": 1, "total": 10000, "allowed": True, "limit": None,
                "period_start": (NOW() - timedelta(days=3)).isoformat(),
                "daily_day": NOW().strftime("%Y-%m-%d"),
                "tier": mirror_tier,
                "next_plan": None,
            },
            source="debit",
        )
        if mirror_age_min > 1.0:
            stamp = (NOW() - timedelta(minutes=mirror_age_min)).isoformat()
            with db._cursor() as cur:
                cur.execute(
                    "UPDATE subscribers SET plan_state_as_of = ? WHERE chat_id = ?",
                    (stamp, chat_id),
                )


def test_a_LAPSED_link_is_offered_the_FULL_ladder(db: Database) -> None:
    """🛑 THE REGRESSION THIS EXISTS FOR. Live chat 1793689937, 2026-09-06.

    `linked_tier='starter'` written once at /link on 2026-05-08, NO plan mirror ever observed,
    and the server answering that key INVALID 568 consecutive times since 2026-09-04. R4 read
    `effective_tier.tier` (which falls back to `linked_tier`) and showed them Pro at $49/$129
    ONLY — refusing a lapsed subscriber the chance to re-buy the plan they lapsed from.
    """
    _linked(db, 1793689937, linked="starter", mirror_tier=None)
    state = get_quota_state(db, 1793689937)
    # The LABEL still reads starter, and that is correct — it is last-known, and says so.
    assert state.effective_tier.tier == "starter"
    assert state.effective_tier.source == "link"
    # ...but the label is NOT good enough to withhold a purchase option.
    assert picker_above_tier(state) is None
    kb = keyboards.plan_picker_kb(resolve_ladder(db), "upgrade_command",
                                  above_tier=picker_above_tier(state))
    assert kb is not None
    assert [len(r) for r in kb.inline_keyboard] == [2, 2], "a lapsed link must see all four SKUs"
    assert any("plan=starter" in u for u in _urls(kb))


def test_a_FRESH_mirror_still_withholds_the_rung_it_names(db: Database) -> None:
    """Live chat 8776880162: linked starter, mirror fresh and saying starter."""
    _linked(db, 8776880162, linked="starter", mirror_tier="starter")
    state = get_quota_state(db, 8776880162)
    assert state.effective_tier.source == "mirror"
    assert picker_above_tier(state) == "starter"
    kb = keyboards.plan_picker_kb(resolve_ladder(db), "upgrade_command",
                                  above_tier=picker_above_tier(state))
    assert kb is not None
    assert all("plan=pro" in u for u in _urls(kb))


def test_the_mirror_OVERRIDES_a_stale_linked_tier(db: Database) -> None:
    """Live chat 1061466212: linked_tier says starter, the fresh mirror says PRO.

    The picker must follow the mirror — offering a Pro subscriber the Pro row because a
    four-month-old `/link` row still says "starter" is the same defect in the other direction.
    """
    _linked(db, 1061466212, linked="starter", mirror_tier="pro")
    state = get_quota_state(db, 1061466212)
    assert state.effective_tier == ("pro", "mirror")
    assert picker_above_tier(state) == "pro"
    assert keyboards.plan_picker_kb(resolve_ladder(db), "upgrade_command",
                                    above_tier=picker_above_tier(state)) is None


def test_a_STALE_mirror_reopens_the_full_ladder(db: Database) -> None:
    """Direction of failure, pinned: past PLAN_MIRROR_STALE_AFTER the tier degrades to 'link',
    and a measurement we could not take must never withhold a purchase option."""
    _linked(db, 4242, linked="starter", mirror_tier="starter", mirror_age_min=91.0)
    state = get_quota_state(db, 4242)
    assert state.effective_tier.source == "link"
    assert picker_above_tier(state) is None


def test_an_unlinked_free_chat_sees_everything(db: Database) -> None:
    db.upsert_subscriber(55, "u", "en")
    state = get_quota_state(db, 55)
    assert state.effective_tier == (None, "unknown")
    assert picker_above_tier(state) is None
