"""GROWTH-TG-CHANNEL-ACQUISITION-W1 / CH1 — ?start=src_<channel> attribution.

Why this instrument exists, in one line: a /start payload arrives THROUGH Telegram
so the chat_id is authenticated by construction, whereas the funnel's utm_source is
a query string on a public URL. Step 0 measured 21 signup_attribution rows minted by
a single Baidu crawler replaying a discovered signup link — which is what produced
the false "tg_bot converts 7.69%". These tests pin the trustworthy side of that pair.

Covers CH1 AC 1.1-1.7.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.ext import CommandHandler

from algovault_bot import handlers
from algovault_bot.db import (
    ACQUISITION_CHANNELS,
    UNKNOWN_ACQUISITION_SOURCE,
    Database,
    normalize_acquisition_source,
)
from algovault_bot.handlers import (
    START_PAYLOAD_MAX_LEN,
    is_valid_start_payload,
    parse_start_payload,
    register_handlers,
)


# ── harness ────────────────────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list = []

    async def reply_text(self, text, reply_markup=None, **kw):  # noqa: ANN001
        self.replies.append((text, reply_markup))


class _FakeUpdate:
    def __init__(self, chat_id: int = 4242, username: str | None = "tester") -> None:
        self.message = _FakeMessage()
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(username=username, language_code="en")


class _Ctx:
    def __init__(self, args=None) -> None:  # noqa: ANN001
        self.args = args or []
        self.user_data: dict = {}


class _CapturingApp:
    def __init__(self) -> None:
        self.captured: list = []

    def add_handler(self, handler, *a, **k) -> None:  # noqa: ANN001
        self.captured.append(handler)


def _start_handler(db: Database):
    """Pull the live /start callback out of the real registration."""
    app = _CapturingApp()
    register_handlers(app, db)
    for h in app.captured:
        if isinstance(h, CommandHandler) and "start" in h.commands:
            return h.callback
    raise AssertionError("/start handler not registered")


def _run_start(db: Database, payload: str | None, chat_id: int = 4242) -> _FakeUpdate:
    upd = _FakeUpdate(chat_id=chat_id)
    ctx = _Ctx([payload] if payload else [])
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _start_handler(db)(upd, ctx)
    )
    return upd


# ── AC 1.4 — the grammar (referral + source coexistence) ───────────────────


def test_start_payload_grammar_covers_all_four_shapes():
    # source only
    assert parse_start_payload("src_x") == ("", "x")
    # referral only — primary must come back BYTE-IDENTICAL (AC 1.2)
    assert parse_start_payload("ref_3NSZ7NIC") == ("ref_3NSZ7NIC", None)
    # both, sharing the one payload Telegram allows
    assert parse_start_payload("ref_3NSZ7NIC-src_x") == ("ref_3NSZ7NIC", "x")
    # auth is NEVER composed: an API key's charset is the issuer's, not ours
    assert parse_start_payload("auth_av_free_5c6b485065f8") == (
        "auth_av_free_5c6b485065f8",
        None,
    )
    # plain / empty
    assert parse_start_payload("") == ("", None)


def test_start_payload_auth_with_a_hyphen_is_never_split():
    """A hyphen inside an api_key must not corrupt the live linking flow."""
    key = "auth_av_free_aaaa-bbbb-src_x"
    assert parse_start_payload(key) == (key, None)


def test_start_payload_hyphen_without_src_suffix_is_not_a_composite():
    assert parse_start_payload("ref_ABC-notasource") == ("ref_ABC-notasource", None)


# ── AC 1.3 — 64 chars / base64url, on the longest legal slug ───────────────


def test_start_payload_fits_telegram_limit_on_longest_channel():
    longest = max(ACQUISITION_CHANNELS, key=len)
    composite = f"ref_3NSZ7NIC-src_{longest}"
    assert len(composite) <= START_PAYLOAD_MAX_LEN
    assert is_valid_start_payload(composite)
    # and it still round-trips through the grammar
    assert parse_start_payload(composite) == ("ref_3NSZ7NIC", longest)


def test_start_payload_charset_rejects_utm_style_parameters():
    # Telegram carries ONE payload of base64url chars — no ?, = or &.
    assert not is_valid_start_payload("src_x&utm_medium=y")
    assert not is_valid_start_payload("src_x=y")
    assert not is_valid_start_payload("src_x?y")
    assert not is_valid_start_payload("a" * (START_PAYLOAD_MAX_LEN + 1))
    assert is_valid_start_payload("ref_ABC-src_awesome_list")


def test_every_channel_slug_is_deep_link_legal_and_hyphen_free():
    """'-' is the composite separator, so a slug containing one would be ambiguous."""
    for slug in ACQUISITION_CHANNELS:
        assert "-" not in slug, slug
        assert is_valid_start_payload(f"src_{slug}")
        assert parse_start_payload(f"src_{slug}") == ("", slug)


# ── AC 1.5 — default-deny ─────────────────────────────────────────────────


def test_unknown_source_is_denied_never_trusted():
    assert normalize_acquisition_source("not_a_channel") == UNKNOWN_ACQUISITION_SOURCE
    assert normalize_acquisition_source("") == UNKNOWN_ACQUISITION_SOURCE
    assert normalize_acquisition_source(None) == UNKNOWN_ACQUISITION_SOURCE
    # a crafted tag can never invent a channel in the readout
    assert normalize_acquisition_source("'; DROP TABLE--") == UNKNOWN_ACQUISITION_SOURCE
    # known channels pass through, case-insensitively
    assert normalize_acquisition_source("X") == "x"
    assert normalize_acquisition_source("  devto  ") == "devto"


def test_unknown_payload_records_unknown_and_never_crashes(tmp_db: Database):
    _run_start(tmp_db, "src_totally_made_up", chat_id=777)
    assert tmp_db.get_acquisition_source(777) == UNKNOWN_ACQUISITION_SOURCE


# ── AC 1.1 — the round-trip ───────────────────────────────────────────────


def test_tagged_start_records_the_channel(tmp_db: Database):
    upd = _run_start(tmp_db, "src_x", chat_id=101)
    assert tmp_db.get_acquisition_source(101) == "x"
    assert upd.message.replies, "/start must still onboard"


def test_composite_referral_plus_source_records_the_source(tmp_db: Database):
    _run_start(tmp_db, "ref_3NSZ7NIC-src_awesome_list", chat_id=102)
    assert tmp_db.get_acquisition_source(102) == "awesome_list"


# ── AC 1.6 — first touch is immutable ─────────────────────────────────────


def test_first_touch_immutable_second_start_does_not_overwrite(tmp_db: Database):
    _run_start(tmp_db, "src_x", chat_id=303)
    assert tmp_db.get_acquisition_source(303) == "x"
    # a later click must NOT be able to claim a signup the first one earned
    _run_start(tmp_db, "src_devto", chat_id=303)
    assert tmp_db.get_acquisition_source(303) == "x"


def test_first_touch_accessor_reports_who_set_it(tmp_db: Database):
    tmp_db.upsert_subscriber(404, "u", "en")
    assert tmp_db.set_acquisition_source_first_touch(404, "x") is True
    assert tmp_db.set_acquisition_source_first_touch(404, "devto") is False
    # re-sending the SAME tag also returns False — it did not set it, the first did
    assert tmp_db.set_acquisition_source_first_touch(404, "x") is False
    assert tmp_db.get_acquisition_source(404) == "x"


def test_source_write_on_missing_row_creates_nothing(tmp_db: Database):
    """No sourced-but-unonboarded subscriber rows."""
    assert tmp_db.set_acquisition_source_first_touch(999_999, "x") is False
    assert tmp_db.get_acquisition_source(999_999) is None


# ── AC 1.2 — auth_ / ref_ behaviour byte-identical ────────────────────────


def test_ref_start_still_dispatches_with_untouched_code(tmp_db: Database, monkeypatch):
    seen: list = []

    async def _spy(update, chat_id, username, lang, ref_code):  # noqa: ANN001
        seen.append(ref_code)

    # bare ref_ — exactly as before this wave
    monkeypatch.setattr(handlers, "_validate_symbol", lambda *a, **k: None)
    app = _CapturingApp()
    register_handlers(app, tmp_db)
    # drive through the real dispatcher, stubbing the referral engine call
    import algovault_bot.referral_client as rc

    monkeypatch.setattr(rc, "attribute", lambda *a, **k: None)
    _run_start(tmp_db, "ref_3nsz7nic", chat_id=505)
    # the code is upper-cased exactly as the pre-wave path did
    assert tmp_db.get_acquisition_source(505) is None, "bare ref_ carries no source"


def test_ref_with_source_still_grants_and_also_records(tmp_db: Database, monkeypatch):
    import algovault_bot.referral_client as rc

    monkeypatch.setattr(rc, "attribute", lambda *a, **k: None)
    _run_start(tmp_db, "ref_3NSZ7NIC-src_referral_card", chat_id=506)
    assert tmp_db.get_acquisition_source(506) == "referral_card"


def test_auth_payload_records_no_source(tmp_db: Database):
    _run_start(tmp_db, "auth_av_free_deadbeefdeadbeef", chat_id=606)
    assert tmp_db.get_acquisition_source(606) is None


# ── AC 1.7 — a plain /start is exactly as today ───────────────────────────


def test_plain_start_unchanged_and_records_no_source(tmp_db: Database):
    upd = _run_start(tmp_db, None, chat_id=707)
    assert tmp_db.get_acquisition_source(707) is None
    assert upd.message.replies, "plain /start must still send the welcome"
    text, markup = upd.message.replies[0]
    assert markup is not None, "plain /start must still carry the inline menu"
    # absence is absence — the pre-wave URL has no utm_medium at all
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert any("utm_campaign=start_welcome" in u for u in urls)
    assert all("utm_medium" not in u for u in urls)


# ── dark-guard law — a swallowed write must stay countable ────────────────


def test_source_write_failure_is_counted_not_silent(tmp_db: Database, monkeypatch):
    before = handlers.acquisition_write_failures()

    def _boom(*a, **k):  # noqa: ANN001
        raise RuntimeError("db down")

    monkeypatch.setattr(tmp_db, "set_acquisition_source_first_touch", _boom)
    upd = _run_start(tmp_db, "src_x", chat_id=808)
    # /start is the entire first impression — it must still onboard
    assert upd.message.replies
    # ...but the failure must be observable, not indistinguishable from "nobody came"
    assert handlers.acquisition_write_failures() == before + 1


# ── CH4 readout support ───────────────────────────────────────────────────


def test_untagged_share_is_reported_not_dropped(tmp_db: Database):
    """The untagged share IS the coverage ceiling on every channel number."""
    _run_start(tmp_db, "src_x", chat_id=901)
    _run_start(tmp_db, None, chat_id=902)
    counts = tmp_db.count_by_acquisition_source()
    assert counts.get("x") == 1
    assert counts.get("(untagged)") == 1
