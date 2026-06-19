"""TG-WATCH-ADOPTION-BROADCAST-W1: sendBroadcast/sendDM thread an optional
inline keyboard (reply_markup) through to telegram bot.send_message."""
from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from algovault_bot import broadcast


class _RecordingBot:
    def __init__(self):
        self.sends: list[dict] = []

    async def send_message(self, **kwargs):
        self.sends.append(kwargs)
        return object()


def _kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="wb:BTC:1h:BINANCE:digest")]])


def test_broadcast_passes_reply_markup_to_every_send(tmp_db):
    tmp_db.upsert_subscriber(1, "a", "en")
    tmp_db.upsert_subscriber(2, "b", "en")
    bot = _RecordingBot()
    kb = _kb()
    result = asyncio.run(
        broadcast.send_broadcast_async(bot, tmp_db, "hello", "t_test", reply_markup=kb)
    )
    assert result["status"] == "sent" and result["sent"] == 2
    assert len(bot.sends) == 2
    assert all(s["reply_markup"] is kb for s in bot.sends)


def test_broadcast_default_reply_markup_is_none(tmp_db):
    tmp_db.upsert_subscriber(1, "a", "en")
    bot = _RecordingBot()
    asyncio.run(broadcast.send_broadcast_async(bot, tmp_db, "hi", "t_test2"))
    assert bot.sends[0]["reply_markup"] is None


def test_send_dm_passes_reply_markup(tmp_db):
    bot = _RecordingBot()
    kb = _kb()
    ok = asyncio.run(broadcast.send_dm_async(bot, 99, "hi", db=tmp_db, reply_markup=kb))
    assert ok is True
    assert bot.sends[0]["reply_markup"] is kb and bot.sends[0]["chat_id"] == 99


def test_public_bot_token_is_read(monkeypatch):
    monkeypatch.delenv("ALGOVAULT_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("PUBLIC_BOT_TOKEN", "123:abc")
    assert broadcast._get_bot_token() == "123:abc"
