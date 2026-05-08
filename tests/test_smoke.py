"""C1 smoke tests — no Telegram network calls; verify message constants only."""

from algovault_bot.bot import WELCOME_MESSAGE


def test_welcome_message_includes_4_watch_examples() -> None:
    # AC1.3 verbatim line match
    for tf in ("1d", "4h", "15m", "1m"):
        assert f"/watch BTC {tf}" in WELCOME_MESSAGE


def test_welcome_message_quota_line() -> None:
    assert "📈 Trade calls (BUY/SELL) — counts against your free 100 calls/month" in WELCOME_MESSAGE


def test_welcome_message_signup_url_with_utm() -> None:
    # D3-A applied: canonical signup surface is api.algovault.com (not algovault.com)
    assert (
        "api.algovault.com/signup?plan=starter&utm_source=tg_bot&utm_campaign=start_welcome"
        in WELCOME_MESSAGE
    )
