"""Render sample trade-call alert PNGs for operator review + print captions.

Outputs previews modeling the cases that actually fire from the bot and,
for each, prints the FULL Telegram caption — the TG-ALERT-VERDICT-CAPTION-W1
glanceable verdict line (always line 1) plus any quota-CTA below it. The
verdict line is printed at column 0 so it is reviewable without sending to
a live chat (and so a render-gate grep can confirm its shape).

Cases:
  1. SOL 5m BUY on BYBIT — free user at 47/100 quota, no CTA
  2. BTC 1h SELL on BINANCE — free user at 80/100 quota (carries a quota_75
     CTA below the verdict line)
  3. SOL 5m BUY on BYBIT — paid Starter user (no CTA)
  4. ETH 4h SELL on BINANCE — paid Pro user (no CTA)
  5. BTC 1h BUY on BINANCE — paid Enterprise user (no CTA)

Run from repo root:
    PYTHONPATH=src python scripts/preview_alert_images.py [output_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

from algovault_bot.alert_image import (
    SeeAlsoCell,
    TradeCallView,
    render_trade_call_card,
)
from algovault_bot.caption import compose_caption, format_verdict_caption_line
from algovault_bot.cta import trade_call_cta_text
from algovault_bot.quota import QuotaState


SAMPLE_REASONING_BUY = (
    "Trending regime, upward bias. Funding pressure extreme; heavy one-sided "
    "crowd. Compression building, breakout setup pending. Trend persistence "
    "elevated; momentum structure. Moderate conviction from blended signals."
)

SAMPLE_REASONING_SELL = (
    "Trending regime, downward bias. Funding pressure mild. Compression "
    "building, breakout setup pending. Trend persistence elevated; momentum "
    "structure. Conditions mixed; better setups likely available elsewhere."
)


# Real quota_75 CTA text for a free user at 80/100 (last-fired None → fires).
# Derived from the production formatter so the preview can't drift from it.
_QUOTA_75_CTA = trade_call_cta_text(QuotaState(80, 100, None, 0.80))


# (filename, view, cta_or_None, note)
SAMPLES = [
    (
        "01_free_low_conf_with_see_also_SOL_5m_BUY.png",
        TradeCallView(
            coin="SOL",
            timeframe="5m",
            exchange="BYBIT",
            call="BUY",
            confidence=48,  # low — triggers See Also
            price=88.61,
            regime="TRENDING_UP",
            funding_rate=-0.00001994,
            funding_24h_avg=-0.00001994,
            funding_state="NORMAL",
            oi_change_pct=0,
            volume_24h=985_000_000,
            trend_persistence="HIGH",
            breakout_pending="IMMINENT",
            reasoning=SAMPLE_REASONING_BUY,
            see_also=SeeAlsoCell(coin="ADA", timeframe="5m", confidence=82, exchange="BINANCE"),
            tier_label=None,
            quota_used=47,
            quota_total=100,
        ),
        None,
        "free, low-conf, no quota CTA — verdict line stands alone",
    ),
    (
        "02_free_with_quota75_cta_BTC_1h_SELL.png",
        TradeCallView(
            coin="BTC",
            timeframe="1h",
            exchange="BINANCE",
            call="SELL",
            confidence=80,
            price=80251.80,
            regime="TRENDING_DOWN",
            funding_rate=-0.00003196,
            funding_24h_avg=-0.00003196,
            funding_state="NORMAL",
            oi_change_pct=0,
            volume_24h=10_065_514_788.95,
            trend_persistence="MEDIUM",
            breakout_pending="INACTIVE",
            reasoning=SAMPLE_REASONING_SELL,
            tier_label=None,
            quota_used=80,
            quota_total=100,
        ),
        _QUOTA_75_CTA,
        "free, 75% nudge active — verdict line 1, quota_75 CTA below",
    ),
    (
        "03_paid_starter_SOL_5m_BUY.png",
        TradeCallView(
            coin="SOL",
            timeframe="5m",
            exchange="BYBIT",
            call="BUY",
            confidence=72,
            price=88.45,
            regime="TRENDING_UP",
            funding_rate=-0.00001994,
            funding_24h_avg=-0.00001994,
            funding_state="NORMAL",
            oi_change_pct=0,
            volume_24h=985_000_000,
            trend_persistence="MEDIUM",
            breakout_pending="INACTIVE",
            reasoning=(
                "Trending regime, upward bias. Funding pressure extreme; heavy "
                "one-sided crowd. Volatility neither expanding nor compressed. "
                "Trend persistence balanced. Moderate conviction from blended "
                "signals."
            ),
            tier_label="Starter",
        ),
        None,
        "paid Starter — no CTA; green diamond + 'Starter Plan' footer",
    ),
    (
        "04_paid_pro_ETH_4h_SELL.png",
        TradeCallView(
            coin="ETH",
            timeframe="4h",
            exchange="BINANCE",
            call="SELL",
            confidence=78,
            price=2290.90,
            regime="TRENDING_DOWN",
            funding_rate=0.00004821,
            funding_24h_avg=0.00004112,
            funding_state="ELEVATED",
            oi_change_pct=2.4,
            volume_24h=4_280_000_000,
            trend_persistence="HIGH",
            breakout_pending="INACTIVE",
            reasoning=(
                "Trending regime, downward bias. Funding pressure elevated; "
                "longs crowded into resistance. Trend persistence high; "
                "structural follow-through likely. Moderate-to-high conviction "
                "from blended signals."
            ),
            tier_label="Pro",
        ),
        None,
        "paid Pro — no CTA; gold diamond + 'Pro Plan' footer",
    ),
    (
        "05_paid_enterprise_BTC_1h_BUY.png",
        TradeCallView(
            coin="BTC",
            timeframe="1h",
            exchange="BINANCE",
            call="BUY",
            confidence=85,
            price=80251.80,
            regime="TRENDING_UP",
            funding_rate=0.00001145,
            funding_24h_avg=0.00000820,
            funding_state="NORMAL",
            oi_change_pct=1.1,
            volume_24h=10_065_514_788.95,
            trend_persistence="HIGH",
            breakout_pending="IMMINENT",
            reasoning=(
                "Trending regime, upward bias. Funding pressure mild; positioning "
                "balanced. Compression building, breakout setup pending. Trend "
                "persistence high. High conviction from blended signals."
            ),
            tier_label="Enterprise",
        ),
        None,
        "paid Enterprise — no CTA; purple diamond + 'Enterprise Plan' footer",
    ),
]


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./preview-out")
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, view, cta, note in SAMPLES:
        path = out_dir / fname
        path.write_bytes(render_trade_call_card(view))
        # The verdict line re-projects the same fields the card renders.
        verdict_line = format_verdict_caption_line(
            view.coin, view.timeframe, view.call, view.confidence, view.exchange
        )
        caption = compose_caption(verdict_line, cta)
        print(f"WROTE {path}  ({note})")
        print("  caption (verdict line is line 1; printed at column 0 below):")
        # Print caption content at column 0 so the verdict line is reviewable
        # verbatim and matches the render-gate regex anchor.
        print(caption)
        print()


if __name__ == "__main__":
    main()
