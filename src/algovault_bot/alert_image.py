"""Trade-call alert image renderer.

Replaces the plain-text trade-call alert with a styled metric-card PNG that
matches the BTC/Binance Trade Call card on the website (per operator design
direction 2026-05-08). Uses Pillow + DejaVu fonts (pre-installed on the
Hetzner host); no headless browser dependency.

The output PNG is sent via Telegram's ``send_photo`` with an optional
caption that carries the quota line + CTA (URLs are clickable in captions).

Layout (vertical stack, ~1024px wide):
- Header strip: ``<COIN> / <Exchange> — <TF> Trade Call`` (serif)
- Metric table: 2 columns (label, value), 10 rows max, alt-tinted backgrounds
- Reasoning section: full text block, wrapped
- Footer strip: ``💎 <Tier> Plan`` (paid) OR ``📊 Quota: N/M free calls`` (free)

Falls back gracefully when a font is missing — Pillow's default bitmap font
is hideous but safe.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


# ── style constants ────────────────────────────────────────────

WIDTH: Final = 1024
PAD_X: Final = 56
HEADER_PAD_Y: Final = 36
ROW_PAD_Y: Final = 22
SECTION_GAP: Final = 28

BG = (14, 17, 23)            # Telegram-dark background
ROW_BG_A = (22, 25, 32)
ROW_BG_B = (16, 19, 26)
TEXT_PRIMARY = (235, 237, 240)
TEXT_MUTED = (140, 148, 160)
ACCENT_GREEN = (46, 204, 113)
ACCENT_RED = (231, 76, 60)
ACCENT_AMBER = (244, 196, 48)
ACCENT_ORANGE = (230, 126, 34)
SEPARATOR = (40, 45, 55)

# Per-tier diamond + label color, sampled from the live algovault.com palette
# (Tailwind tokens — emerald-400 / gold-400 / purple-400). x402 keeps the
# soft-blue x402 brand color since the landing's X402 PER CALL header uses
# Tailwind blue-400.
TIER_COLORS: dict[str, tuple[int, int, int]] = {
    "Starter": (0x34, 0xD3, 0x99),     # emerald-400
    "Pro": (0xD4, 0xB2, 0x55),         # gold-400 (custom)
    "Enterprise": (0xC0, 0x84, 0xFC),  # purple-400
    "X402": (0x60, 0xA5, 0xFA),        # blue-400
}
TIER_BADGE_FALLBACK = (0x60, 0xA5, 0xFA)


# Font search paths — (path, ttc_index) tuples; first hit wins per role.
# ttc_index lets us pick a specific weight from a TrueType Collection (e.g.
# Helvetica.ttc index 1 = bold).
_LINUX_FONTS = "/usr/share/fonts/truetype/dejavu"
_FONT_CANDIDATES: dict[str, list[tuple[str, int]]] = {
    "serif_bold": [
        (f"{_LINUX_FONTS}/DejaVuSerif-Bold.ttf", 0),
        ("/System/Library/Fonts/Supplemental/Georgia.ttf", 0),
        ("/System/Library/Fonts/NewYork.ttf", 0),
    ],
    "sans": [
        (f"{_LINUX_FONTS}/DejaVuSans.ttf", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 0),
    ],
    "sans_bold": [
        (f"{_LINUX_FONTS}/DejaVuSans-Bold.ttf", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 1),  # bold face inside the TTC
        ("/System/Library/Fonts/Helvetica.ttc", 0),  # last-resort: regular
    ],
    "mono": [
        (f"{_LINUX_FONTS}/DejaVuSansMono.ttf", 0),
        ("/System/Library/Fonts/Menlo.ttc", 0),
    ],
}


def _load_font(role: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path, index in _FONT_CANDIDATES.get(role, []):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size, index=index)
            except OSError:
                continue
    log.warning("alert_image: no font for role %r — falling back to bitmap", role)
    return ImageFont.load_default()


# ── data shape ─────────────────────────────────────────────────


@dataclass
class SeeAlsoCell:
    """A single cross-asset suggestion line.

    Sourced from signal-MCP's ``also_see`` field (which only carries
    ``{coin, timeframe, confidence}`` after the leaderboard trim). The
    ``exchange`` field is filled in by the bot from the alert's own row
    (since the trimmed cell omits it); set to None to render
    "<COIN> <TF> — <conf>% confidence" without an exchange tag.
    """

    coin: str
    timeframe: str
    confidence: int
    exchange: str | None = None


@dataclass
class TradeCallView:
    """Everything the renderer needs for one trade-call alert.

    Indicators are pulled from the upstream ``get_trade_call`` ``indicators``
    block (funding_rate / funding_24h_avg / funding_state / oi_change_pct /
    volume_24h / trend_persistence / breakout_pending). Fields that are
    None / missing are omitted from the rendered card rather than shown as
    ``?`` or ``N/A``.
    """

    coin: str
    timeframe: str
    exchange: str
    call: str  # BUY / SELL / HOLD
    confidence: int | None
    price: float | None
    regime: str | None
    funding_rate: float | None
    funding_24h_avg: float | None
    funding_state: str | None
    oi_change_pct: float | None
    volume_24h: float | None
    trend_persistence: str | None
    breakout_pending: str | None
    reasoning: str | None
    # See-Also: rendered only on low-confidence calls; populated by the
    # caller from ``tc_result.also_see`` filtered to same-TF + conf ≥80.
    see_also: SeeAlsoCell | None = None
    # Footer state — exactly one of these is rendered.
    tier_label: str | None = None  # "Starter" / "Pro" / "Enterprise" / "X402"
    quota_used: int | None = None
    quota_total: int | None = None


# ── formatting helpers ─────────────────────────────────────────


def _confidence_label(c: int) -> str:
    if c < 25:
        return "very low"
    if c < 50:
        return "low"
    if c < 75:
        return "medium"
    return "high"


def _format_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:,.4f}".rstrip("0").rstrip(".")
    return f"${p:,.6f}".rstrip("0").rstrip(".")


def _format_volume(v: float) -> str:
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.2f}K"
    return f"${v:,.2f}"


def _format_funding_rate(r: float) -> str:
    # signal-MCP returns rates like -3.196e-05 — show 8-decimal fixed plus the
    # state qualifier in parens at the call site.
    return f"{r:+.8f}"


def _exchange_pretty(ex: str) -> str:
    return {
        "BINANCE": "Binance USDT-M Futures",
        "BYBIT": "Bybit USDT-M Futures",
        "OKX": "OKX USDT-M Futures",
        "BITGET": "Bitget USDT-M Futures",
        "HL": "Hyperliquid",
    }.get(ex, ex)


def _call_color(call: str) -> tuple[int, int, int]:
    if call == "BUY":
        return ACCENT_GREEN
    if call == "SELL":
        return ACCENT_RED
    return ACCENT_AMBER


# ── row collection ─────────────────────────────────────────────


def _metric_rows(view: TradeCallView) -> list[tuple[str, str, tuple[int, int, int] | None]]:
    """Return [(label, value_str, value_color_or_None)]; skips rows whose
    underlying field is None so we never render ``?`` or ``N/A``.

    The "Call" row's value is just ``BUY`` / ``SELL`` / ``HOLD`` (no glyph) —
    the renderer draws a colored circle to the left of it explicitly so we
    don't depend on color-emoji fonts being installed.
    """
    color = _call_color(view.call)
    rows: list[tuple[str, str, tuple[int, int, int] | None]] = []

    rows.append(("Call", view.call, color))

    if view.confidence is not None:
        rows.append((
            "Confidence",
            f"{view.confidence}% ({_confidence_label(view.confidence)})",
            None,
        ))

    if view.price is not None:
        rows.append(("Price", _format_price(view.price), None))

    if view.regime:
        rows.append(("Regime", view.regime, None))

    if view.funding_rate is not None:
        funding_value = _format_funding_rate(view.funding_rate)
        if view.funding_state:
            funding_value = f"{funding_value} ({view.funding_state})"
        rows.append(("Funding rate", funding_value, None))

    if view.funding_24h_avg is not None:
        rows.append(("24h funding avg", _format_funding_rate(view.funding_24h_avg), None))

    if view.oi_change_pct is not None:
        if view.oi_change_pct % 1 == 0:
            oi_str = f"{int(view.oi_change_pct)}%"
        else:
            oi_str = f"{view.oi_change_pct:.2f}".rstrip("0").rstrip(".") + "%"
        rows.append(("OI change", oi_str, None))

    if view.volume_24h is not None:
        rows.append(("24h volume", _format_volume(view.volume_24h), None))

    if view.trend_persistence:
        rows.append(("Trend persistence", view.trend_persistence, None))

    if view.breakout_pending:
        rows.append(("Breakout pending", view.breakout_pending, None))

    return rows


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        candidate = f"{current} {w}".strip()
        bbox = font.getbbox(candidate)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _bulletize_reasoning(text: str) -> list[str]:
    """Split a reasoning prose blob into bullet candidates.

    Splits on sentence boundaries (``. ``); each bullet is the trimmed
    sentence without its trailing period. Empty fragments are dropped
    AFTER trimming (so inputs like ``". . ."`` collapse to []).
    Compound clauses joined with ``; `` are kept inside a single bullet —
    splitting those over-fragments the typical 3-5 sentence reasoning blob.
    """
    return [
        cleaned
        for cleaned in (raw.strip().rstrip(".").strip() for raw in text.split(". "))
        if cleaned
    ]


def _format_see_also(cell: SeeAlsoCell) -> str:
    parts = [cell.coin, cell.timeframe]
    if cell.exchange:
        parts.append(cell.exchange.title())
    parts.append(f"{cell.confidence}% confidence")
    return " ".join(parts)


# ── main render ────────────────────────────────────────────────


def render_trade_call_card(view: TradeCallView) -> bytes:
    """Render a trade-call alert as a PNG. Returns raw bytes."""
    f_header = _load_font("serif_bold", 38)
    f_label = _load_font("sans", 26)
    f_value = _load_font("sans_bold", 26)
    f_value_mono = _load_font("mono", 24)
    f_section = _load_font("sans_bold", 22)
    f_body = _load_font("sans", 22)
    f_footer = _load_font("sans_bold", 24)

    rows = _metric_rows(view)

    # Pre-measure to compute total height.
    row_h = 56
    metric_block_h = row_h * len(rows) + 24  # +pad

    # Reasoning is rendered as a bulleted list — each sentence becomes one
    # bullet; long bullets wrap to subsequent lines indented under the bullet.
    reasoning_bullets: list[list[str]] = []  # list of wrapped-line lists per bullet
    reasoning_block_h = 0
    bullet_indent = 30
    line_h = 32
    bullet_gap = 8  # extra px between bullets
    if view.reasoning:
        bullets = _bulletize_reasoning(view.reasoning)
        for b in bullets:
            wrapped = _wrap_text(b, f_body, WIDTH - 2 * PAD_X - bullet_indent)
            reasoning_bullets.append(wrapped or [b])
        title_h = 38  # "Reasoning" header
        body_h = sum(line_h * len(w) + bullet_gap for w in reasoning_bullets)
        reasoning_block_h = 18 + title_h + body_h + 18

    # See Also section — single line below reasoning, only if populated.
    see_also_block_h = 0
    if view.see_also is not None:
        see_also_block_h = 18 + 38 + line_h + 18  # sep + title + line + pad

    header_h = HEADER_PAD_Y * 2 + 50
    footer_h = 80
    total_h = (
        header_h
        + metric_block_h
        + reasoning_block_h
        + see_also_block_h
        + footer_h
        + SECTION_GAP * 3
    )

    img = Image.new("RGB", (WIDTH, total_h), BG)
    draw = ImageDraw.Draw(img)

    # Header
    title = f"{view.coin} / {_exchange_pretty(view.exchange)} — {view.timeframe} Trade Call"
    draw.text((PAD_X, HEADER_PAD_Y), title, font=f_header, fill=TEXT_PRIMARY)
    y = header_h
    draw.line([(PAD_X, y), (WIDTH - PAD_X, y)], fill=SEPARATOR, width=2)
    y += SECTION_GAP

    # Metric rows
    label_x = PAD_X
    value_x = WIDTH // 2
    for i, (label, value, color) in enumerate(rows):
        bg = ROW_BG_A if i % 2 == 0 else ROW_BG_B
        draw.rectangle([(0, y), (WIDTH, y + row_h)], fill=bg)
        # Vertically center text in row
        bbox = f_label.getbbox(label)
        text_h = bbox[3] - bbox[1]
        ty = y + (row_h - text_h) // 2 - 4
        draw.text((label_x, ty), label, font=f_label, fill=TEXT_MUTED)
        # Use mono for numeric-looking values; sans-bold for everything else.
        is_numeric = label in {"Funding rate", "24h funding avg", "Price", "24h volume", "OI change"}
        font_for_value = f_value_mono if is_numeric else f_value
        # On the Call row, draw a colored status circle to the left of the
        # value text (replaces the 🟢 / 🔴 / 🟡 emoji — Pillow + DejaVu / Helvetica
        # don't render color emoji, so we draw a real circle).
        text_left = value_x
        if label == "Call":
            circle_d = 22
            cy = y + row_h // 2
            draw.ellipse(
                [(value_x, cy - circle_d // 2),
                 (value_x + circle_d, cy + circle_d // 2)],
                fill=color or TEXT_PRIMARY,
            )
            text_left = value_x + circle_d + 14
        draw.text(
            (text_left, ty),
            value,
            font=font_for_value,
            fill=color or TEXT_PRIMARY,
        )
        y += row_h

    y += SECTION_GAP // 2

    # Reasoning — rendered as bullets (one bullet per sentence; wrapped lines
    # indent under the bullet for readability).
    if reasoning_bullets:
        draw.line([(PAD_X, y), (WIDTH - PAD_X, y)], fill=SEPARATOR, width=2)
        y += 18
        draw.text((PAD_X, y), "Reasoning", font=f_section, fill=TEXT_MUTED)
        y += 38
        bullet_dot_r = 4
        bullet_x = PAD_X + 6
        text_x = PAD_X + bullet_indent
        for wrapped in reasoning_bullets:
            # Draw bullet glyph centered with the first line.
            cy = y + line_h // 2 - 2
            draw.ellipse(
                [(bullet_x - bullet_dot_r, cy - bullet_dot_r),
                 (bullet_x + bullet_dot_r, cy + bullet_dot_r)],
                fill=TEXT_MUTED,
            )
            for line in wrapped:
                draw.text((text_x, y), line, font=f_body, fill=TEXT_PRIMARY)
                y += line_h
            y += bullet_gap
        y += 18

    # See Also — only when caller populated the field (low-conf trade calls
    # with a same-TF, ≥80%-confidence leaderboard suggestion available).
    if view.see_also is not None:
        draw.line([(PAD_X, y), (WIDTH - PAD_X, y)], fill=SEPARATOR, width=2)
        y += 18
        draw.text((PAD_X, y), "See Also", font=f_section, fill=TEXT_MUTED)
        y += 38
        draw.text(
            (PAD_X, y),
            _format_see_also(view.see_also),
            font=f_body,
            fill=TEXT_PRIMARY,
        )
        y += line_h + 18

    # Footer
    draw.line([(PAD_X, y), (WIDTH - PAD_X, y)], fill=SEPARATOR, width=2)
    y += 24
    if view.tier_label:
        # Diamond glyph (drawn shape; no emoji-font dependency) + "<Tier> Plan".
        # Color matches the tier's website-palette accent.
        tier_color = TIER_COLORS.get(view.tier_label, TIER_BADGE_FALLBACK)
        d_size = 24
        cx, cy = PAD_X + d_size // 2, y + 18
        diamond = [
            (cx, cy - d_size // 2),
            (cx + d_size // 2, cy),
            (cx, cy + d_size // 2),
            (cx - d_size // 2, cy),
        ]
        draw.polygon(diamond, fill=tier_color)
        draw.text(
            (PAD_X + d_size + 14, y),
            f"{view.tier_label} Plan",
            font=f_footer,
            fill=tier_color,
        )
    elif view.quota_used is not None and view.quota_total is not None:
        text = f"Quota: {view.quota_used}/{view.quota_total} free alerts used"
        draw.text((PAD_X, y), text, font=f_footer, fill=TEXT_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
