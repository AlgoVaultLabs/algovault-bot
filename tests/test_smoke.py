"""Welcome-message constant fixtures.

TG-START-COPY-TRIM-W1: the quota-burn examples block + standalone "⚡ Add in
bulk" block were removed; /watch is taught in one consolidated line; "Upgrade"
is a clickable HTML <a> link (welcome sent with parse_mode=HTML) instead of a
raw wrapping URL.
"""

from algovault_bot.messages import WELCOME_MESSAGE


def test_welcome_message_consolidated_watch_line() -> None:
    # Single Get-started /watch line (placeholders HTML-escaped) + one Example.
    assert "/watch &lt;COIN&gt; &lt;TF&gt; &lt;Exch&gt; [regime|calls|both]" in WELCOME_MESSAGE
    assert "Example: ETH 15m Bybit regime / BTC All Binance / XRP 5m All" in WELCOME_MESSAGE
    # BOT-ONDEMAND-CMDS-W1: /start now surfaces the one-shot pulls too.
    assert "/scan [TOP_N] [TF] [EXCH]" in WELCOME_MESSAGE
    assert "/regime &lt;COIN&gt; &lt;TF&gt; [EXCH]" in WELCOME_MESSAGE
    assert "/call &lt;COIN&gt; &lt;TF&gt; [EXCH]" in WELCOME_MESSAGE
    assert "/funding [TOP_N]" in WELCOME_MESSAGE
    # The removed blocks are gone.
    assert "faster quota burn" not in WELCOME_MESSAGE
    assert "⚡ Add in bulk" not in WELCOME_MESSAGE


def test_welcome_message_quota_line() -> None:
    # QUOTA-CONSISTENCY-COUNT-ALL-W1: regime shifts AND trade calls both count
    # toward the free 100/mo; only HOLD trade calls stay free (parity w/ signal-MCP).
    assert "📊 Regime shifts — count toward your free 100 calls/month" in WELCOME_MESSAGE
    assert "📈 Trade calls (BUY/SELL) — count toward your free 100 calls/month" in WELCOME_MESSAGE
    assert "HOLD verdicts are silent + free." in WELCOME_MESSAGE
    # The pre-wave "regime free, no limit" claim must be gone (metering parity).
    assert "free, no limit" not in WELCOME_MESSAGE


def test_welcome_message_upgrade_link_with_utm() -> None:
    # D3-A applied: api.algovault.com/signup?plan=starter (NOT algovault.com/signup
    # which 404s). Clickable HTML <a> upgrade CTA (utm preserved, generated from
    # signup_url('start_welcome')); the raw URL line is gone.
    assert '<a href="https://api.algovault.com/signup?plan=starter' in WELCOME_MESSAGE
    assert "utm_source=tg_bot" in WELCOME_MESSAGE
    assert "utm_campaign=start_welcome" in WELCOME_MESSAGE
    # ACTIVATION-NUDGE-W1: button text is "Unlock 3,000 calls/mo →" + the primary
    # funnel-attribution param upgrade_from=tg_start (utm preserved alongside, A2).
    assert ">Unlock 3,000 calls/mo →</a>" in WELCOME_MESSAGE
    assert "upgrade_from=tg_start" in WELCOME_MESSAGE
    assert "→ api.algovault.com/signup" not in WELCOME_MESSAGE  # no raw URL line


def test_welcome_message_track_record_trust_line() -> None:
    # ACTIVATION-NUDGE-W1: the on-chain-verified track record is surfaced in /start
    # as the trust→conversion lever (a clickable HTML link, no query to escape).
    assert "Free tier: 100 calls/month." in WELCOME_MESSAGE
    assert "on-chain-verified track record" in WELCOME_MESSAGE
    assert '<a href="https://algovault.com/track-record">algovault.com/track-record</a>' in WELCOME_MESSAGE
