"""OPS-BOT-LINKED-TIER-REFRESH-W1 CH1 — validate_api_key returns THREE states.

One test per row of the mapping table, measured against the live endpoint's real
behaviour on 2026-08-21 (P4):

    valid key            -> 200 {"valid":true,"customer_id":...,"tier":"pro"}
    dead / unknown key   -> 404 {"valid":false}
    missing/wrong header -> 401 {"error":"unauthorized"}

The whole point of the chapter is that the six conditions which used to collapse
into one ``None`` are now distinguishable, and that OUR faults never present as
THEIR key being invalid.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from algovault_bot import link_validator
from algovault_bot.db import Database
from algovault_bot.handlers import handle_link
from algovault_bot.link_validator import KeyCheck, validate_api_key


#: Long enough to pass the length guard, and distinctive enough that its presence in
#: ANY captured log line is unambiguous. Never a real key shape.
SENTINEL_KEY = "SENTINELKEY_never_log_me_9f3a7c21"
BYPASS = "test-bypass-key"


class _Resp:
    """Minimal httpx.Response stand-in: only what validate_api_key touches."""

    def __init__(self, status_code: int, payload: Any = None, bad_json: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


@pytest.fixture(autouse=True)
def _bypass_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a CONFIGURED bot. The misconfigured row unsets it explicitly."""
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", BYPASS)
    monkeypatch.delenv("ALGOVAULT_VALIDATE_KEY_URL", raising=False)


def _with_response(resp: _Resp) -> Any:
    return patch.object(link_validator.httpx, "get", return_value=resp)


# ── the mapping table, one test per row ────────────────────────────────────────


def test_row_200_valid_with_tier_is_VALID() -> None:
    payload = {"valid": True, "customer_id": "cus_UepUXyDjxzx99c", "tier": "pro"}
    with _with_response(_Resp(200, payload)):
        check = validate_api_key(SENTINEL_KEY)
    assert check == KeyCheck(
        status="VALID", tier="pro", customer_id="cus_UepUXyDjxzx99c", reason="ok"
    )
    assert check.is_valid is True
    assert check.is_determined_invalid is False


def test_row_404_is_INVALID_no_active_subscription() -> None:
    # The live shape for BOTH a dead key and an unknown key (P4, measured).
    with _with_response(_Resp(404, {"valid": False})):
        check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INVALID"
    assert check.reason == "no_active_subscription"
    assert check.tier is None and check.customer_id is None
    assert check.is_determined_invalid is True


def test_row_200_valid_false_is_INVALID() -> None:
    # Defensive branch: the live endpoint uses 404 for this, but a 200 saying so is
    # still a determination, not an unknown.
    with _with_response(_Resp(200, {"valid": False})):
        check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INVALID"
    assert check.reason == "no_active_subscription"


def test_row_short_key_is_INVALID_malformed_and_makes_no_request() -> None:
    with patch.object(link_validator.httpx, "get") as spy:
        check = validate_api_key("abc")
    assert check.status == "INVALID"
    assert check.reason == "malformed_key"
    spy.assert_not_called()


def test_row_empty_key_is_INVALID_malformed() -> None:
    check = validate_api_key("")
    assert check.status == "INVALID"
    assert check.reason == "malformed_key"


@pytest.mark.parametrize("bypass", ["", "   ", link_validator.PLACEHOLDER_BYPASS_KEY])
def test_row_bot_misconfigured_is_INDETERMINATE(
    monkeypatch: pytest.MonkeyPatch, bypass: str
) -> None:
    """THE live conversion bug. OUR env being wrong is not a fact about THEIR key."""
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", bypass)
    with patch.object(link_validator.httpx, "get") as spy:
        check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INDETERMINATE", "a misconfigured bot must never read as INVALID"
    assert check.reason == "bot_misconfigured"
    assert check.is_determined_invalid is False
    spy.assert_not_called()


def test_row_bot_misconfigured_when_env_absent_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALGOVAULT_INTERNAL_BYPASS_KEY", raising=False)
    check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INDETERMINATE"
    assert check.reason == "bot_misconfigured"


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectError("refused"), "transport_ConnectError"),
        (httpx.ReadTimeout("slow"), "transport_ReadTimeout"),
        (httpx.ConnectTimeout("slow"), "transport_ConnectTimeout"),
    ],
)
def test_row_transport_error_is_INDETERMINATE(exc: Exception, expected: str) -> None:
    with patch.object(link_validator.httpx, "get", side_effect=exc):
        check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INDETERMINATE"
    assert check.reason == expected, "the exception CLASS is the distinguishing detail"


def test_row_200_non_json_is_INDETERMINATE_bad_body() -> None:
    with _with_response(_Resp(200, bad_json=True)):
        check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INDETERMINATE"
    assert check.reason == "bad_body"


def test_row_200_non_object_json_is_INDETERMINATE_bad_body() -> None:
    with _with_response(_Resp(200, ["not", "an", "object"])):
        check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INDETERMINATE"
    assert check.reason == "bad_body"


def test_row_200_valid_true_without_tier_is_INDETERMINATE_bad_body() -> None:
    """The 7th condition the spec's 6-row table omitted.

    A 200 asserting valid=true while carrying no tier is the SERVER contradicting
    itself — a shape mismatch, not a determination about the subscriber. Classifying
    it INVALID would unlink a paying customer over a server-side shape regression.
    """
    with _with_response(_Resp(200, {"valid": True, "customer_id": "cus_x"})):
        check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INDETERMINATE"
    assert check.reason == "bad_body"


@pytest.mark.parametrize("code", [401, 403, 500, 502, 503])
def test_row_http_error_is_INDETERMINATE(code: int) -> None:
    with _with_response(_Resp(code, {"error": "unauthorized"})):
        check = validate_api_key(SENTINEL_KEY)
    assert check.status == "INDETERMINATE"
    assert check.reason == f"http_{code}"


def test_reason_is_always_populated_including_on_valid() -> None:
    """The sentinel-collapse fix: the distinguishing detail rides beside the verdict."""
    cases = [
        _Resp(200, {"valid": True, "tier": "starter"}),
        _Resp(404, {"valid": False}),
        _Resp(500, {}),
        _Resp(200, bad_json=True),
    ]
    for resp in cases:
        with _with_response(resp):
            check = validate_api_key(SENTINEL_KEY)
        assert check.reason, f"empty reason for status={resp.status_code}"


def test_validate_api_key_never_returns_none() -> None:
    """AC4's companion: the type no longer HAS a None inhabitant."""
    probes = [
        _Resp(200, {"valid": True, "tier": "pro"}),
        _Resp(200, {"valid": False}),
        _Resp(200, {"valid": True}),
        _Resp(200, bad_json=True),
        _Resp(404, {"valid": False}),
        _Resp(401, {"error": "unauthorized"}),
        _Resp(500, {}),
    ]
    for resp in probes:
        with _with_response(resp):
            assert validate_api_key(SENTINEL_KEY) is not None
    with patch.object(link_validator.httpx, "get", side_effect=httpx.ConnectError("x")):
        assert validate_api_key(SENTINEL_KEY) is not None
    assert validate_api_key("short") is not None


# ── 1c — the api_key value never reaches a log line ────────────────────────────


def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every angle a key could leak from: the formatted message AND the raw args."""
    parts: list[str] = []
    for rec in caplog.records:
        parts.append(rec.getMessage())
        parts.append(str(rec.msg))
        parts.append(str(rec.args))
    return "\n".join(parts)


def test_api_key_value_never_appears_in_any_log_line(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanical, not by inspection — drive EVERY mapping row and grep the output."""
    caplog.set_level(logging.DEBUG)

    responses = [
        _Resp(200, {"valid": True, "customer_id": "cus_x", "tier": "pro"}),
        _Resp(200, {"valid": False}),
        _Resp(200, {"valid": True}),
        _Resp(200, bad_json=True),
        _Resp(200, ["array"]),
        _Resp(404, {"valid": False}),
        _Resp(401, {"error": "unauthorized"}),
        _Resp(403, {"error": "forbidden"}),
        _Resp(500, {}),
    ]
    for resp in responses:
        with _with_response(resp):
            validate_api_key(SENTINEL_KEY)
    with patch.object(link_validator.httpx, "get", side_effect=httpx.ConnectError("boom")):
        validate_api_key(SENTINEL_KEY)
    validate_api_key("short")
    monkeypatch.delenv("ALGOVAULT_INTERNAL_BYPASS_KEY", raising=False)
    validate_api_key(SENTINEL_KEY)

    text = _all_log_text(caplog)
    assert caplog.records, "the probe logged nothing — the assertion would be vacuous"
    assert SENTINEL_KEY not in text
    assert BYPASS not in text, "the internal bypass key must not leak either"


def test_handle_link_never_logs_the_api_key(
    tmp_db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for resp in (
        _Resp(200, {"valid": True, "customer_id": "cus_x", "tier": "starter"}),
        _Resp(404, {"valid": False}),
        _Resp(500, {}),
    ):
        with _with_response(resp):
            handle_link(tmp_db, 42, "alice", "en", SENTINEL_KEY)

    text = _all_log_text(caplog)
    assert caplog.records
    assert SENTINEL_KEY not in text
    assert BYPASS not in text


# ── 1b — handle_link produces three DISTINCT outcomes ──────────────────────────


def _linked_state(db: Database, chat_id: int) -> tuple[Any, Any]:
    row = db.get_subscriber(chat_id)
    if row is None:
        return (None, None)
    return (row["linked_api_key"], row["linked_tier"])


def test_handle_link_three_distinct_outcomes(tmp_db: Database) -> None:
    with _with_response(_Resp(200, {"valid": True, "customer_id": "c", "tier": "starter"})):
        valid_reply = handle_link(tmp_db, 1, "a", "en", SENTINEL_KEY)
    with _with_response(_Resp(404, {"valid": False})):
        invalid_reply = handle_link(tmp_db, 2, "b", "en", SENTINEL_KEY)
    with _with_response(_Resp(503, {})):
        indet_reply = handle_link(tmp_db, 3, "c", "en", SENTINEL_KEY)

    assert len({valid_reply, invalid_reply, indet_reply}) == 3
    assert "✅ Linked!" in valid_reply
    assert invalid_reply.startswith("❌")
    assert indet_reply.startswith("⏳")


def test_indeterminate_reply_does_not_blame_the_key_and_offers_no_signup(
    tmp_db: Database,
) -> None:
    with _with_response(_Resp(500, {})):
        reply = handle_link(tmp_db, 7, "a", "en", SENTINEL_KEY)
    lowered = reply.lower()
    assert "wasn't recognized" not in lowered
    assert "expired" not in lowered
    assert "invalid" not in lowered
    # Sending them to signup implies their subscription is the problem — the one claim
    # an INDETERMINATE cannot support.
    assert "signup" not in lowered


@pytest.mark.parametrize(
    "resp",
    [
        _Resp(401, {"error": "unauthorized"}),
        _Resp(403, {"error": "forbidden"}),
        _Resp(500, {}),
        _Resp(200, bad_json=True),
        _Resp(200, {"valid": True}),
    ],
)
def test_INDETERMINATE_never_writes_linked_state(tmp_db: Database, resp: _Resp) -> None:
    with _with_response(resp):
        reply = handle_link(tmp_db, 55, "alice", "en", SENTINEL_KEY)
    assert reply.startswith("⏳")
    assert _linked_state(tmp_db, 55) == (None, None)


def test_INDETERMINATE_does_not_clobber_an_EXISTING_link(tmp_db: Database) -> None:
    """Build Rule 5 at the /link surface: an unknown keeps current state, always."""
    with _with_response(_Resp(200, {"valid": True, "customer_id": "c", "tier": "pro"})):
        handle_link(tmp_db, 77, "alice", "en", SENTINEL_KEY)
    assert _linked_state(tmp_db, 77) == (SENTINEL_KEY, "pro")

    with _with_response(_Resp(503, {})):
        handle_link(tmp_db, 77, "alice", "en", "some-other-key-entirely")
    assert _linked_state(tmp_db, 77) == (SENTINEL_KEY, "pro"), "an unknown must change nothing"


def test_misconfigured_bot_yields_retry_not_invalid_key(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3, demonstrated: unset the bypass key, get the retry message."""
    monkeypatch.delenv("ALGOVAULT_INTERNAL_BYPASS_KEY", raising=False)
    reply = handle_link(tmp_db, 88, "alice", "en", SENTINEL_KEY)
    assert reply.startswith("⏳")
    assert reply != __import__(
        "algovault_bot.messages", fromlist=["x"]
    ).link_invalid_key_message()
    assert _linked_state(tmp_db, 88) == (None, None)
