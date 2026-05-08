"""BOT-W2 C2 — link_validator HTTP client tests.

Mocks ``httpx.get`` to verify behavior end-to-end without hitting signal-MCP.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from algovault_bot.link_validator import ValidatedKey, validate_api_key


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
    assert result == ValidatedKey(customer_id="cus_x", tier="starter")
    # Verify the call shape — bypass header present, key in query
    call = mock_get.call_args
    assert call.kwargs["params"] == {"api_key": "av_live_test123"}
    assert call.kwargs["headers"] == {"X-AlgoVault-Internal-Key": "a" * 32}


def test_404_returns_none() -> None:
    with patch("httpx.get", return_value=_mock_response(404, {"valid": False})):
        result = validate_api_key("av_live_unknown")
    assert result is None


def test_401_returns_none() -> None:
    with patch("httpx.get", return_value=_mock_response(401, {"error": "unauthorized"})):
        result = validate_api_key("av_live_test")
    assert result is None


def test_200_with_valid_false_returns_none() -> None:
    with patch("httpx.get", return_value=_mock_response(200, {"valid": False})):
        result = validate_api_key("av_live_test")
    assert result is None


def test_200_with_missing_tier_returns_none() -> None:
    with patch("httpx.get", return_value=_mock_response(200, {"valid": True})):
        result = validate_api_key("av_live_test")
    assert result is None


def test_short_or_empty_key_returns_none_without_http() -> None:
    with patch("httpx.get") as mock_get:
        assert validate_api_key("") is None
        assert validate_api_key("short") is None
        mock_get.assert_not_called()


def test_placeholder_bypass_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGOVAULT_INTERNAL_BYPASS_KEY", "__C3_PLACEHOLDER__")
    with patch("httpx.get") as mock_get:
        assert validate_api_key("av_live_test123") is None
        mock_get.assert_not_called()


def test_transport_error_returns_none() -> None:
    import httpx
    with patch("httpx.get", side_effect=httpx.ConnectError("connection refused")):
        result = validate_api_key("av_live_test123")
    assert result is None
