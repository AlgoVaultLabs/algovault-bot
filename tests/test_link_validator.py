"""BOT-W2 C2 — link_validator HTTP client tests.

Mocks ``httpx.get`` to verify behavior end-to-end without hitting signal-MCP.

OPS-BOT-LINKED-TIER-REFRESH-W1 CH1 — EVERY assertion in this file used to read
``result is None``. That was not a coincidence of style: ``None`` WAS the contract,
and these tests encoded the six-condition collapse as intended behaviour. Each one
is flipped below to the state it actually meant, so the file now records the
distinction rather than the collapse. The exhaustive mapping table, the reason
vocabulary and the no-leak assertion live in
``tests/test_link_validator_three_state.py``; what is unique here is the CALL SHAPE.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from algovault_bot.link_validator import KeyCheck, validate_api_key


@pytest.fixture(autouse=True)
def _bypass_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", "a" * 32)
    monkeypatch.delenv("ALGOVAULT_VALIDATE_KEY_URL", raising=False)


def _mock_response(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=body if body is not None else {})
    return resp


def test_valid_starter_key_returns_validated() -> None:
    body = {"valid": True, "customer_id": "cus_x", "tier": "starter"}
    with patch("httpx.get", return_value=_mock_response(200, body)) as mock_get:
        result = validate_api_key("av_live_test123")
    assert result == KeyCheck(
        status="VALID", tier="starter", customer_id="cus_x", reason="ok"
    )
    # Verify the call shape — bypass header present, key in query. This is the
    # assertion this file uniquely owns.
    call = mock_get.call_args
    assert call.kwargs["params"] == {"api_key": "av_live_test123"}
    assert call.kwargs["headers"] == {"X-AlgoVault-Internal-Key": "a" * 32}


def test_404_is_determined_invalid() -> None:
    # was: `result is None`
    with patch("httpx.get", return_value=_mock_response(404, {"valid": False})):
        result = validate_api_key("av_live_unknown")
    assert result.status == "INVALID"
    assert result.reason == "no_active_subscription"


def test_401_is_indeterminate_not_invalid() -> None:
    # was: `result is None` — indistinguishable from a real rejection, which is how a
    # broken internal key told paying customers their subscription was bad.
    with patch("httpx.get", return_value=_mock_response(401, {"error": "unauthorized"})):
        result = validate_api_key("av_live_test")
    assert result.status == "INDETERMINATE"
    assert result.reason == "http_401"
    assert result.is_determined_invalid is False


def test_200_with_valid_false_is_determined_invalid() -> None:
    # was: `result is None`
    with patch("httpx.get", return_value=_mock_response(200, {"valid": False})):
        result = validate_api_key("av_live_test")
    assert result.status == "INVALID"
    assert result.reason == "no_active_subscription"


def test_200_with_missing_tier_is_indeterminate_not_invalid() -> None:
    # was: `result is None`. A 200 claiming valid=true with no tier is the server
    # contradicting itself — a shape mismatch, never a fact about the subscriber.
    with patch("httpx.get", return_value=_mock_response(200, {"valid": True})):
        result = validate_api_key("av_live_test")
    assert result.status == "INDETERMINATE"
    assert result.reason == "bad_body"


def test_short_or_empty_key_is_invalid_without_http() -> None:
    # was: `is None` for both
    with patch("httpx.get") as mock_get:
        assert validate_api_key("").reason == "malformed_key"
        assert validate_api_key("short").reason == "malformed_key"
        assert validate_api_key("short").status == "INVALID"
        mock_get.assert_not_called()


def test_placeholder_bypass_key_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    # was: `is None`. THE live conversion bug — our misconfiguration presenting as
    # their key being invalid.
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", "__C3_PLACEHOLDER__")
    with patch("httpx.get") as mock_get:
        result = validate_api_key("av_live_test123")
        assert result.status == "INDETERMINATE"
        assert result.reason == "bot_misconfigured"
        mock_get.assert_not_called()


def test_transport_error_is_indeterminate() -> None:
    # was: `is None`
    import httpx

    with patch("httpx.get", side_effect=httpx.ConnectError("connection refused")):
        result = validate_api_key("av_live_test123")
    assert result.status == "INDETERMINATE"
    assert result.reason == "transport_ConnectError"
