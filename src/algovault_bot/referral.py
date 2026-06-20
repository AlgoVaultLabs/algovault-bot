"""TG-REFERRAL-W1 / C2 — referral surface copy (PURE; no telegram/httpx/db).

Trilingual (en / id / zh-hans, via unlock.normalize_lang — mirrors the other
viral flow). Every program number (bonus calls / commission % / months) is
interpolated from the engine's ``terms`` payload (the REFERRAL_TERMS SoT in
crypto-quant-signal-mcp) — NEVER hardcoded here, so the bot can't drift from the
single source of truth. The handlers (handlers.py) wrap the share URL in an
InlineKeyboardButton; this module stays free of framework imports for testability.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .unlock import normalize_lang


def build_share_url(deep_link: str, share_text: str) -> str:
    """The one-tap native share sheet: t.me/share/url?url=<reflink>&text=<framing>."""
    return f"https://t.me/share/url?url={quote(deep_link, safe='')}&text={quote(share_text, safe='')}"


def share_button_label(lang_code: str | None = None) -> str:
    lang = normalize_lang(lang_code)
    if lang == "id":
        return "📤 Bagikan link referral"
    if lang == "zh-hans":
        return "📤 分享推荐链接"
    return "📤 Share your referral link"


def _terms(data_terms: dict[str, Any]) -> tuple[int, int, int]:
    """(bonus_calls, commission_pct, commission_months) from the engine terms."""
    return (
        int(data_terms.get("bonus_calls", 0)),
        int(data_terms.get("commission_pct", 0)),
        int(data_terms.get("commission_months", 0)),
    )


def format_share_text(terms: dict[str, Any], lang_code: str | None = None) -> str:
    """Friend-facing text pre-filled in the share sheet (the referee will read it)."""
    bonus, _pct, _months = _terms(terms)
    lang = normalize_lang(lang_code)
    if lang == "id":
        return (
            f"Saya pakai AlgoVault untuk sinyal trading kripto (AI quant). "
            f"Pakai link saya dan dapat {bonus} panggilan gratis 👇"
        )
    if lang == "zh-hans":
        return (
            f"我在用 AlgoVault 获取加密量化交易信号。"
            f"用我的链接注册可获得 {bonus} 次免费调用 👇"
        )
    return (
        f"I'm using AlgoVault for AI quant crypto trade signals. "
        f"Use my link and get {bonus} free calls 👇"
    )


def format_referral_body(data: dict[str, Any], lang_code: str | None = None) -> str:
    """The /referral message body: the give-get deal, the deep link, and stats.
    `data` is the engine's /api/referral/code payload."""
    lang = normalize_lang(lang_code)
    bonus, pct, months = _terms(data.get("terms", {}))
    deep_link = str(data.get("deep_link", ""))
    stats = data.get("stats", {}) or {}
    signups = int(stats.get("signups", 0))
    conversions = int(stats.get("conversions", 0))

    if lang == "id":
        return (
            "🎁 Program referral AlgoVault\n\n"
            f"Bagikan link Anda. Setiap teman yang bergabung lewat link itu:\n"
            f"• mendapat {bonus} panggilan bonus, dan\n"
            f"• Anda dapat {pct}% dari langganan mereka selama {months} bulan.\n\n"
            f"Link referral Anda:\n{deep_link}\n\n"
            f"📊 Diajak: {signups} · Berlangganan: {conversions}\n\n"
            "Ketuk tombol di bawah untuk berbagi."
        )
    if lang == "zh-hans":
        return (
            "🎁 AlgoVault 推荐计划\n\n"
            f"分享你的链接。每位通过它加入的好友：\n"
            f"• 获得 {bonus} 次奖励调用，且\n"
            f"• 你可从其订阅中获得 {pct}% 佣金，持续 {months} 个月。\n\n"
            f"你的推荐链接：\n{deep_link}\n\n"
            f"📊 已推荐：{signups} · 已订阅：{conversions}\n\n"
            "点击下方按钮分享。"
        )
    return (
        "🎁 AlgoVault referral program\n\n"
        "Share your link. Every friend who joins through it:\n"
        f"• gets {bonus} bonus calls, and\n"
        f"• you earn {pct}% of their subscription for {months} months.\n\n"
        f"Your referral link:\n{deep_link}\n\n"
        f"📊 Referred: {signups} · Subscribed: {conversions}\n\n"
        "Tap the button below to share."
    )


def format_ref_join_greeting(bonus_calls: int, terms: dict[str, Any], lang_code: str | None = None) -> str:
    """Greeting when a NEW user joins via someone's ref link (the referee bonus + the give-get pitch)."""
    lang = normalize_lang(lang_code)
    _b, pct, months = _terms(terms)
    if lang == "id":
        return (
            f"🎉 Selamat datang di AlgoVault! Anda mendapat {bonus_calls} panggilan bonus "
            "di atas jatah gratis 100/bulan.\n\n"
            f"Ingin lebih? Ajak teman — mereka dapat {bonus_calls} bonus juga, dan Anda dapat "
            f"{pct}% dari langganan mereka selama {months} bulan. Ketik /referral."
        )
    if lang == "zh-hans":
        return (
            f"🎉 欢迎来到 AlgoVault！你已获得 {bonus_calls} 次奖励调用"
            "（在每月 100 次免费额度之外）。\n\n"
            f"想要更多？推荐好友——他们同样获得 {bonus_calls} 次奖励，你可从其订阅中获得 "
            f"{pct}% 佣金，持续 {months} 个月。发送 /referral。"
        )
    return (
        f"🎉 Welcome to AlgoVault! You got {bonus_calls} bonus calls on top of your "
        "free 100/month.\n\n"
        f"Want more? Refer friends — they get {bonus_calls} bonus calls too, and you earn "
        f"{pct}% of their subscription for {months} months. Send /referral."
    )


def format_referral_unavailable(lang_code: str | None = None) -> str:
    """Fail-soft reply when the engine is momentarily unreachable."""
    lang = normalize_lang(lang_code)
    if lang == "id":
        return "Program referral sedang tidak tersedia sebentar. Coba lagi nanti."
    if lang == "zh-hans":
        return "推荐功能暂时不可用，请稍后再试。"
    return "The referral program is briefly unavailable. Please try again shortly."
