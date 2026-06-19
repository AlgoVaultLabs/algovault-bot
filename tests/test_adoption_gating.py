"""TG-WATCH-ADOPTION-BROADCAST-W1 (A2): go-live flag + operator-chat parsing."""
from __future__ import annotations

import pytest

from algovault_bot import adoption


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("nope", False),
])
def test_adoption_broadcasts_live_flag(monkeypatch, val, expected):
    monkeypatch.setenv("ADOPTION_BROADCASTS_LIVE", val)
    assert adoption.adoption_broadcasts_live() is expected


def test_adoption_broadcasts_live_default_off(monkeypatch):
    monkeypatch.delenv("ADOPTION_BROADCASTS_LIVE", raising=False)
    assert adoption.adoption_broadcasts_live() is False


def test_operator_chat_ids_parsing(monkeypatch):
    monkeypatch.setenv("BOT_ADMIN_CHAT_IDS", "1793689937, 42 ,bogus,")
    assert adoption.operator_chat_ids() == [1793689937, 42]


def test_operator_chat_ids_empty(monkeypatch):
    monkeypatch.delenv("BOT_ADMIN_CHAT_IDS", raising=False)
    assert adoption.operator_chat_ids() == []
