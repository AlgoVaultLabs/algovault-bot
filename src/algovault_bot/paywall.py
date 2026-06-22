"""TG-BROADCAST-STACK-W1 CH3 (2026-05-28): paywall-at-quota hook.

When MCP returns a ``_algovault.tier_warning`` field on a tool call response
(per ACTIVATION-PAYWALL-W1's tier-warning structured envelope), this module
fires a one-time DM to the subscriber whose watchlist row triggered the call.
Idempotency is per-subscriber-per-level-per-month via 3 NEW ``subscribers``
columns: ``quota_hit_soft_at`` / ``quota_hit_hard_at`` / ``quota_hit_block_at``.

T1 voice: ≤300 chars, outcome-framed, no jargon. The body always references
``/unlock_premium_alerts`` as the free path forward + Stripe `suggested_upgrade_url`
as the paid path.

Spec reference: ``Prompt/tg-broadcast-stack-w1.md`` Chapter C3.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

VALID_LEVELS = ("soft", "hard", "block")


def _column_for_level(level: str) -> str:
    """Return the subscribers column tracking the last fire timestamp."""
    if level == "soft":
        return "quota_hit_soft_at"
    if level == "hard":
        return "quota_hit_hard_at"
    if level == "block":
        return "quota_hit_block_at"
    raise ValueError(f"invalid paywall level: {level!r}")


def has_fired_this_month(
    db_path: str, chat_id: int, level: str, now: datetime | None = None
) -> bool:
    """Returns True if the subscriber has ALREADY received a paywall DM at
    this level within the current calendar month (UTC).

    Per CLAUDE.md `Cohort coverage` + spec's `Idempotency: same threshold fires
    DM only once per month` rule. Returns False on fresh DB / missing column
    (fail-open: caller will fire + record).
    """
    col = _column_for_level(level)
    now = now or datetime.now(timezone.utc)
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            cur = conn.execute(
                f"SELECT {col} FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False
    if not row or row[0] is None:
        return False
    fired_at_str = str(row[0])
    try:
        fired_at = datetime.fromisoformat(fired_at_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    # Same calendar-month + year (UTC) → still throttled.
    return fired_at.year == now.year and fired_at.month == now.month


def mark_fired(
    db_path: str, chat_id: int, level: str, now: datetime | None = None
) -> None:
    """Record that the subscriber just received a paywall DM at this level."""
    col = _column_for_level(level)
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            conn.execute(
                f"UPDATE subscribers SET {col} = ? WHERE chat_id = ?",
                (now_iso, chat_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        log.warning("paywall mark_fired failed level=%s err=%s", level, e)


def format_paywall_body(
    level: str,
    current_usage: int | None,
    monthly_limit: int | None,
    suggested_upgrade_url: str | None,
    lang_code: str | None = None,
    referral_link: str | None = None,
    bonus_calls: int | None = None,
) -> str:
    """Render the T1-voice paywall DM body (≤300 chars per spec).

    Trilingual surface: en (default), id, zh-hans. Other lang_codes → en.
    Falls back to safe defaults when usage/limit/url are absent.

    REFERRAL-INPRODUCT-NUDGE-W1 / C2: at the WALL (``block`` level) — the limit
    moment, mirroring the MCP C1 — when ``referral_link`` + ``bonus_calls`` are
    present (the caller fetched them from the engine SoT for this user), the body
    leads with the referral free path (refer a friend → you both get N bonus
    calls) and retains the upgrade path (North Star: acquisition > revenue).
    Fail-soft: absent referral args → the existing block copy verbatim. The
    soft/hard pre-wall warnings are unchanged (mirrors C1 leaving 80% soft alone).
    ``bonus_calls`` is the engine SoT number (never hardcoded in the bot).
    """
    used = current_usage if current_usage is not None else "?"
    total = monthly_limit if monthly_limit is not None else 100
    url = suggested_upgrade_url or "https://api.algovault.com/signup?plan=starter&upgrade_from=tg_quota"
    lang = (lang_code or "en").lower().replace("_", "-")

    if level == "block" and referral_link and bonus_calls:
        # The wall, referral-PROMINENT (lead) + upgrade-RETAINED + the existing
        # /unlock free path kept (mirrors MCP C1 limit-keyed; ≤300 chars).
        if lang.startswith("id"):
            return (
                f"Quota habis ({used}/{total}) bulan ini. "
                f"Tetap gratis: ajak teman — kalian berdua dapat {bonus_calls} panggilan bonus → {referral_link}. "
                f"Atau /unlock_premium_alerts, atau upgrade: {url}"
            )
        if lang.startswith("zh"):
            return (
                f"本月额度已用完（{used}/{total}）。"
                f"继续免费：邀请好友——你们都获得 {bonus_calls} 次奖励调用 → {referral_link}。"
                f"或 /unlock_premium_alerts，或升级：{url}"
            )
        return (
            f"Out of verdicts ({used}/{total}) this month. "
            f"Keep going free: refer a friend — you both get {bonus_calls} bonus calls → {referral_link}. "
            f"Or /unlock_premium_alerts, or upgrade: {url}"
        )

    if level == "soft":
        if lang.startswith("id"):
            return (
                f"Anda telah memakai {used}/{total} verdict bulan ini. "
                f"Upgrade ke Pro ($49/bln, 15K panggilan): {url}, "
                f"ATAU dapatkan 30 hari Pro gratis via /unlock_premium_alerts."
            )
        if lang.startswith("zh"):
            return (
                f"您本月已使用 {used}/{total} 次验证。"
                f"升级到 Pro（$49/月、15K 次）：{url}，"
                f"或通过 /unlock_premium_alerts 免费获取 30 天 Pro。"
            )
        return (
            f"You've used {used}/{total} verdicts this month. "
            f"Upgrade to Pro ($49/mo, 15K calls): {url}, "
            f"OR earn 30 days free Pro via /unlock_premium_alerts."
        )
    if level == "hard":
        if lang.startswith("id"):
            return (
                f"⚠️ {used}/{total} verdict bulan ini sudah dipakai. "
                f"Tinggal sedikit lagi sebelum quota habis. "
                f"Upgrade: {url} · ATAU /unlock_premium_alerts gratis."
            )
        if lang.startswith("zh"):
            return (
                f"⚠️ 本月已使用 {used}/{total} 次。"
                f"额度即将耗尽。升级：{url} · "
                f"或使用 /unlock_premium_alerts 免费。"
            )
        return (
            f"⚠️ Used {used}/{total} verdicts this month. "
            f"Approaching quota. Upgrade: {url} · "
            f"OR /unlock_premium_alerts for 30 days free."
        )
    if level == "block":
        if lang.startswith("id"):
            return (
                f"Quota habis: {used}/{total} verdict bulan ini. "
                f"Reset bulan depan. Upgrade sekarang: {url} · "
                f"ATAU /unlock_premium_alerts untuk 30 hari Pro gratis."
            )
        if lang.startswith("zh"):
            return (
                f"本月额度已用完：{used}/{total}。"
                f"下月重置。立即升级：{url} · "
                f"或 /unlock_premium_alerts 免费获取 30 天 Pro。"
            )
        return (
            f"You've used all {used}/{total} free verdicts this month. "
            f"Resets next month. Upgrade now: {url} · "
            f"OR /unlock_premium_alerts for 30 days free."
        )
    raise ValueError(f"invalid paywall level: {level!r}")


def extract_tier_warning(mcp_response: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull the ``_algovault.tier_warning`` block out of an MCP tool response.

    Returns None when the response doesn't carry the warning (paid tier,
    bot-internal traffic, below soft threshold). Returns the warning dict
    with ``level`` / ``current_usage`` / ``monthly_limit`` / ``tier`` /
    ``suggested_upgrade_url`` keys when present.
    """
    if not isinstance(mcp_response, dict):
        return None
    meta = mcp_response.get("_algovault")
    if not isinstance(meta, dict):
        return None
    warning = meta.get("tier_warning")
    if not isinstance(warning, dict):
        return None
    level = warning.get("level")
    if level not in VALID_LEVELS:
        return None
    return warning


def should_fire_paywall_dm(
    db_path: str,
    chat_id: int,
    warning: dict[str, Any],
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Decision helper: returns ``(fire, level)``. ``fire`` is True only when
    the warning is present + level is valid + we haven't already fired this
    month. Caller then sends the DM + calls ``mark_fired()``.
    """
    level = warning.get("level")
    if level not in VALID_LEVELS:
        return False, None
    if has_fired_this_month(db_path, chat_id, level, now=now):
        return False, level
    return True, level
