"""Anti-abuse tests — Telegram-imposed only after 2026-05-08 simplification.

The per-user 24h caps (20 regime / 30 calls / 50 burn-protection) were
removed: Telegram doesn't impose per-user caps, so the bot doesn't either.
The 100 calls/month quota gate in ``quota.py`` is the only call-volume cap.

Remaining surface here: Telegram global semaphore + nightly digest.
"""

from __future__ import annotations

from algovault_bot.db import Database
from algovault_bot.rate_limit import TELEGRAM_GLOBAL_SEMAPHORE


# Telegram global semaphore — required to stay under Telegram's 30 msg/sec ceiling.
def test_telegram_global_semaphore_size_25() -> None:
    # asyncio.Semaphore exposes ._value (CPython internal) — bound check.
    assert TELEGRAM_GLOBAL_SEMAPHORE._value == 25


# Nightly digest renderer — sanity end-to-end.
def test_digest_renders_with_zero_state(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest
    text = render_digest(tmp_db)
    assert "Algovault-Telegram-bot — Daily Digest" in text
    assert "Total Subscribers: 0" in text
    assert "New Subscribers last 24h: 0" in text


def test_digest_aggregates_sample_data(tmp_db: Database) -> None:
    from algovault_bot.digest import render_digest
    tmp_db.upsert_subscriber(1, "u1", "en")
    tmp_db.upsert_subscriber(2, "u2", "en")
    tmp_db.add_watch(1, "BTC", "4h", "BINANCE", "both")
    # BOT-DIGEST-LAST24H-W1 2026-05-21: digest now reads from alerts_fired
    # (rolling-24h log) instead of subscribers.total_regime_alerts /
    # total_call_alerts (lifetime). Test rewritten to record alerts via the
    # new helper; lifetime counter increments would no longer surface in
    # the digest body.
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.record_alert_fired(1, "regime")
    tmp_db.record_alert_fired(2, "call")
    text = render_digest(tmp_db)
    assert "Total Subscribers: 2" in text
    # Both subscribers were just upserted → both count as "new" within last 24h.
    assert "New Subscribers last 24h: 2" in text
    # Last 24h block renders the alerts_fired counts.
    assert "📊 Regime: 2" in text
    assert "📈 Calls: 1" in text
