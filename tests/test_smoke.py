"""Welcome-message (/start) copy fixtures.

TG-COPY-DEFAULTS-VENUES-W1 (R1): /start is plain-language PLAIN TEXT (no HTML tags,
sent without parse_mode so the plain track-record domain auto-links). The clickable
Upgrade CTA + its utm live in /help now (F2); /start is link-light by design.
"""

from algovault_bot.messages import WELCOME_MESSAGE


def test_welcome_plain_language_intro() -> None:
    assert "👋 Welcome to AlgoVault, the brain layer for AI trading agents." in WELCOME_MESSAGE
    assert (
        "You get 100 free alerts a month. Each alert I send uses one. Silent HOLDs are always free."
        in WELCOME_MESSAGE
    )
    assert "📊 Regime: the market's mood flips" in WELCOME_MESSAGE
    assert "📈 Trade call: a clear BUY or SELL" in WELCOME_MESSAGE
    # coverage line: 900+ markets across 12 exchanges
    assert "900+ markets (crypto, gold, stocks, pre-IPO) across 12 exchanges" in WELCOME_MESSAGE


def test_welcome_start_here_and_default_hint() -> None:
    assert "🔔 Watch a coin → /watch BTC 4h" in WELCOME_MESSAGE
    assert "🔍 Scan the top movers → /scan" in WELCOME_MESSAGE
    assert "📈 Get one call now → /call ETH 1h" in WELCOME_MESSAGE
    # the smart-default nudge (bare /watch → BTC 1h Binance)
    assert "New here? Just type /watch and I'll start you on BTC 1h (Binance)." in WELCOME_MESSAGE


def test_welcome_is_plain_text_no_html_no_inline_upgrade_link() -> None:
    # R1/F2: plain text (no HTML tags); track-record is a plain auto-linked domain;
    # the clickable Upgrade <a> + its utm moved to /help.
    assert "<a href" not in WELCOME_MESSAGE
    assert "utm_campaign=start_welcome" not in WELCOME_MESSAGE
    assert "algovault.com/track-record" in WELCOME_MESSAGE
    # CH5: the API ladder moved (Starter 3,000 -> 10,000/mo) and this bot's own free
    # allowance is stated in ALERTS, which is what it actually meters.
    assert "Starter is $9.99/mo or $39.90/6mo for 10,000 API " in WELCOME_MESSAGE
    assert "Free: 100 alerts/month here." in WELCOME_MESSAGE
    assert "3,000 calls" not in WELCOME_MESSAGE
