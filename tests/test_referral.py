"""TG-REFERRAL-W1 / C2 — bot /referral surface + referee bonus invariants.

Covers: trilingual renderers (terms interpolated from the engine SoT, never
hardcoded) + share-url encoding; referral_client fail-soft (mocked httpx);
db bonus grant/read; the bonus-aware quota meter (monthly-then-bonus draw,
remaining/exhausted, byte-identical for the bonus-free base); and the
bonus-aware CTA threshold (no upsell while bonus remains).
"""
from __future__ import annotations

from algovault_bot import referral, referral_client
from algovault_bot.cta import quota_threshold
from algovault_bot.quota import QuotaState, consume_quota, get_quota_state


# ── referral.py renderers (pure; terms come from the engine, not hardcoded) ──

_TERMS = {"bonus_calls": 500, "commission_pct": 30, "commission_months": 12}
_DATA = {
    "code": "ABCD12",
    "share_url": "https://api.algovault.com/signup?ref=ABCD12",
    "deep_link": "https://t.me/algovaultofficialbot?start=ref_ABCD12",
    "terms": _TERMS,
    "stats": {"signups": 3, "conversions": 1},
}


def test_referral_body_interpolates_terms_and_link():
    body = referral.format_referral_body(_DATA, "en")
    assert "https://t.me/algovaultofficialbot?start=ref_ABCD12" in body
    assert "500" in body and "30%" in body and "12 months" in body
    assert "Referred: 3" in body and "Subscribed: 1" in body


def test_referral_body_is_not_hardcoded():
    # different SoT terms → different rendered numbers (proves interpolation)
    data = {**_DATA, "terms": {"bonus_calls": 999, "commission_pct": 42, "commission_months": 6}}
    body = referral.format_referral_body(data, "en")
    assert "999" in body and "42%" in body and "6 months" in body
    assert "500" not in body and "30%" not in body


def test_referral_body_trilingual():
    en = referral.format_referral_body(_DATA, "en")
    idn = referral.format_referral_body(_DATA, "id")
    zh = referral.format_referral_body(_DATA, "zh-hans")
    assert en != idn and en != zh and idn != zh
    for b in (en, idn, zh):
        assert "ref_ABCD12" in b  # every language carries the deep link


def test_ref_join_greeting_has_bonus_and_double_sided_terms():
    g = referral.format_ref_join_greeting(500, _TERMS, "en")
    assert "500" in g and "30%" in g and "12 months" in g


def test_share_url_encodes_link_and_text():
    url = referral.build_share_url("https://t.me/algovaultofficialbot?start=ref_X", "join me")
    assert url.startswith("https://t.me/share/url?url=")
    assert "%3A%2F%2F" in url  # the deep link is percent-encoded
    assert "join%20me" in url


def test_share_text_trilingual_carries_bonus():
    for lang in ("en", "id", "zh-hans"):
        assert "500" in referral.format_share_text(_TERMS, lang)


# ── referral_client.py (fail-soft over mocked httpx) ──

class _FakeResp:
    def __init__(self, payload, raise_exc=None):
        self._p = payload
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        return self._p


def test_client_get_code_success(monkeypatch):
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", "k" * 32)
    monkeypatch.setattr(referral_client.httpx, "get", lambda *a, **k: _FakeResp({"ok": True, "code": "ABCD12", "terms": _TERMS}))
    out = referral_client.get_code(123)
    assert out and out["code"] == "ABCD12"


def test_client_get_code_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("ALGOVAULT_INTERNAL_BYPASS_KEY", raising=False)
    assert referral_client.get_code(123) is None


def test_client_get_code_http_error_returns_none(monkeypatch):
    import httpx
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", "k" * 32)

    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(referral_client.httpx, "get", _boom)
    assert referral_client.get_code(123) is None


def test_client_attribute_success(monkeypatch):
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", "k" * 32)
    monkeypatch.setattr(referral_client.httpx, "post", lambda *a, **k: _FakeResp({"ok": True, "recorded": True, "bonus_calls": 500}))
    out = referral_client.attribute("ABCD12", 456)
    assert out and out["recorded"] is True and out["bonus_calls"] == 500


def test_client_attribute_error_returns_none(monkeypatch):
    import httpx
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", "k" * 32)

    def _boom(*a, **k):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(referral_client.httpx, "post", _boom)
    assert referral_client.attribute("ABCD12", 456) is None


# ── db bonus pool ──

def test_db_grant_and_get_bonus_additive(tmp_db):
    tmp_db.upsert_subscriber(900, "u", "en")
    assert tmp_db.get_referral_bonus(900) == 0
    assert tmp_db.grant_referral_bonus(900, 500) == 500
    assert tmp_db.grant_referral_bonus(900, 500) == 1000  # additive
    assert tmp_db.get_referral_bonus(900) == 1000


def test_db_grant_clamps_negative(tmp_db):
    tmp_db.upsert_subscriber(901, "u", "en")
    assert tmp_db.grant_referral_bonus(901, -50) == 0


# ── bonus-aware quota meter ──

def test_quotastate_remaining_and_exhausted_with_bonus():
    s = QuotaState(used=100, total=100, window_start=None, pct_used=1.0, referral_bonus_remaining=50)
    assert s.remaining == 50 and s.exhausted is False
    s2 = QuotaState(used=100, total=100, window_start=None, pct_used=1.0, referral_bonus_remaining=0)
    assert s2.remaining == 0 and s2.exhausted is True


def test_consume_bonus_free_is_byte_identical(tmp_db):
    tmp_db.upsert_subscriber(910, "u", "en")
    st = consume_quota(tmp_db, 910, 1)
    assert st.used == 1 and st.referral_bonus_remaining == 0


def test_consume_draws_monthly_then_bonus(tmp_db):
    tmp_db.upsert_subscriber(911, "u", "en")
    consume_quota(tmp_db, 911, 100)            # exhaust the monthly 100
    tmp_db.grant_referral_bonus(911, 500)      # +500 bonus
    st = get_quota_state(tmp_db, 911)
    assert st.used == 100 and st.referral_bonus_remaining == 500
    assert st.exhausted is False and st.remaining == 500
    st2 = consume_quota(tmp_db, 911, 1)        # overflow draws the bonus, monthly stays 100
    assert st2.used == 100 and st2.referral_bonus_remaining == 499
    # drain the rest of the bonus → truly exhausted
    consume_quota(tmp_db, 911, 499)
    final = get_quota_state(tmp_db, 911)
    assert final.referral_bonus_remaining == 0 and final.exhausted is True


# ── bonus-aware CTA ──

def test_cta_threshold_suppressed_while_bonus_remains():
    # 75% used but has bonus → no upsell nudge
    assert quota_threshold(QuotaState(75, 100, None, 0.75, referral_bonus_remaining=50)) is None
    # truly out (no monthly, no bonus) → "100"
    assert quota_threshold(QuotaState(100, 100, None, 1.0, referral_bonus_remaining=0)) == "100"
    # has bonus, monthly full → not "100" (bonus covers it), suppressed
    assert quota_threshold(QuotaState(100, 100, None, 1.0, referral_bonus_remaining=10)) is None
    # bonus-free 75% → normal "75" (unchanged behaviour)
    assert quota_threshold(QuotaState(75, 100, None, 0.75, referral_bonus_remaining=0)) == "75"


# ── C3: value-moment nudge + compounding ──

from datetime import datetime, timedelta, timezone  # noqa: E402

from algovault_bot.cta import referral_nudge_text  # noqa: E402

_NOW = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)


def test_referral_nudge_copy_is_qualitative_and_trilingual():
    for lang in ("en", "id", "zh-hans"):
        n = referral.format_referral_nudge(lang)
        assert "/referral" in n
        # qualitative — NO hardcoded program numbers (the SoT lives in /referral)
        assert "500" not in n and "30%" not in n and "12 months" not in n


def test_nudge_fires_for_active_free_user_when_due():
    s = QuotaState(10, 100, None, 0.1, referral_bonus_remaining=0, referral_nudge_last_at=None)
    assert referral_nudge_text(s, now=_NOW) != ""


def test_nudge_suppressed_for_paid_bonus_quota_and_throttle():
    # paid → ''
    assert referral_nudge_text(QuotaState(10, 100, None, 0.1, linked_tier="pro"), now=_NOW) == ""
    # holds bonus → '' (already knows referral)
    assert referral_nudge_text(QuotaState(10, 100, None, 0.1, referral_bonus_remaining=50), now=_NOW) == ""
    # a quota CTA owns the slot (75%) → '' (never stack)
    assert referral_nudge_text(QuotaState(80, 100, None, 0.8, referral_bonus_remaining=0), now=_NOW) == ""
    # throttled (nudged 1 day ago) → ''
    recent = _NOW - timedelta(days=1)
    assert referral_nudge_text(QuotaState(10, 100, None, 0.1, referral_nudge_last_at=recent), now=_NOW) == ""
    # due again after 8 days → fires
    old = _NOW - timedelta(days=8)
    assert referral_nudge_text(QuotaState(10, 100, None, 0.1, referral_nudge_last_at=old), now=_NOW) != ""


def test_db_mark_referral_nudge_throttle(tmp_db):
    tmp_db.upsert_subscriber(920, "u", "en")
    assert get_quota_state(tmp_db, 920).referral_nudge_last_at is None
    tmp_db.mark_referral_nudge_sent(920, _NOW.isoformat())
    assert get_quota_state(tmp_db, 920).referral_nudge_last_at is not None


def test_welcome_and_help_mention_referral():
    from algovault_bot import messages
    assert "/referral" in messages.WELCOME_MESSAGE
    assert "/referral" in messages.HELP_MESSAGE
