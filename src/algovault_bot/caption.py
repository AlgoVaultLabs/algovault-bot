"""Glanceable verdict-caption formatting (TG-ALERT-VERDICT-CAPTION-W1).

Pure, I/O-free formatters that project a fired trade call into a single
human-readable line — e.g. ``LTC 15min Buy 76% Binance`` — suitable as the
FIRST line of a Telegram photo caption. On a locked phone, a photo-only
message previews as just "📷 Photo"; a caption populates the notification
body, so prepending this line makes the call readable at a glance without
unlocking and opening the chat.

Built as a shared primitive (not inlined at the send site) so future
glanceable surfaces — regime one-liners, daily-digest rows, push-title
text — reuse the same display mapping.

Display mapping (internal token → caption token):
  - Direction: ``BUY`` → ``Buy``, ``SELL`` → ``Sell`` (HOLD never reaches
    this path — HOLDs are silent).
  - Timeframe: minute TFs render ``<n>min`` (``1m``→``1min`` … ``30m``→
    ``30min``); hour TFs (``1h``…``12h``) and the day TF (``1d``) pass
    through unchanged.
  - Confidence: ``<int>%``.
  - Exchange: brand-canonical title-case — ``BINANCE``→``Binance``,
    ``BYBIT``→``Bybit``, ``OKX``→``OKX``, ``BITGET``→``Bitget``,
    ``HL``→``Hyperliquid``.
"""

from __future__ import annotations

from typing import Final

# Internal direction token → caption casing. Only BUY/SELL reach the
# trade-call caption path (HOLD verdicts are silent).
_DIRECTION_DISPLAY: Final[dict[str, str]] = {"BUY": "Buy", "SELL": "Sell"}

# Internal exchange token → brand-canonical display name. Mirrors the 5
# exchanges in ``validators.EXCHANGES`` (HL, BINANCE, BYBIT, OKX, BITGET).
_EXCHANGE_DISPLAY: Final[dict[str, str]] = {
    "BINANCE": "Binance",
    "BYBIT": "Bybit",
    "OKX": "OKX",
    "BITGET": "Bitget",
    "HL": "Hyperliquid",
}


def _display_timeframe(tf: str) -> str:
    """Minute TFs render ``<n>min`` (``1m``→``1min`` … ``30m``→``30min``);
    hour TFs (``1h``…``12h``) and the day TF (``1d``) pass through unchanged."""
    tf = tf.strip().lower()
    if tf.endswith("m") and tf[:-1].isdigit():
        return f"{tf[:-1]}min"
    return tf


def _display_direction(direction: str) -> str:
    norm = direction.strip().upper()
    # Allow-list mapping; fall back to title-case for any unexpected token
    # rather than throwing (a malformed verdict line must never suppress the
    # alert send — the rich image card still carries the call).
    return _DIRECTION_DISPLAY.get(norm, norm.capitalize())


def _display_exchange(exchange: str) -> str:
    norm = exchange.strip().upper()
    return _EXCHANGE_DISPLAY.get(norm, exchange.strip())


def format_verdict_caption_line(
    coin: str, tf: str, direction: str, confidence: int, exchange: str
) -> str:
    """Project a fired trade call into one glanceable caption line.

    >>> format_verdict_caption_line("LTC", "15m", "BUY", 76, "BINANCE")
    'LTC 15min Buy 76% Binance'
    >>> format_verdict_caption_line("BTC", "4h", "SELL", 81, "HL")
    'BTC 4h Sell 81% Hyperliquid'

    Pure / no I/O. Every field is already in hand at the trade-call send
    site — this is a re-projection of existing data, zero new fetch.
    """
    return (
        f"{coin.strip().upper()} {_display_timeframe(tf)} "
        f"{_display_direction(direction)} {int(confidence)}% {_display_exchange(exchange)}"
    )


def compose_caption(verdict_line: str, cta: str | None) -> str:
    """Compose the final photo caption: the verdict line is ALWAYS line 1;
    any existing CTA caption is preserved verbatim below it. An empty or
    ``None`` CTA leaves the verdict line standing alone.

    Telegram captions are capped at 1024 chars; the verdict line is ~30
    chars and any CTA already fit under the cap, so the sum stays well
    under the limit.
    """
    if cta:
        return f"{verdict_line}\n{cta}"
    return verdict_line
