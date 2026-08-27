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


def _usd(e2: Any) -> str:
    """Render integer e2-cents (USD x 100) as $X.YY (the engine's money encoding)."""
    cents = int(e2 or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def _earnings_line(stats: dict[str, Any], terms: dict[str, Any], lang: str) -> str:
    """REFERRAL-PARITY-NOTIFS-W1 / C2 — the commission earnings line for /referral,
    matching the web /account. All figures from the engine payload (SoT); never hardcoded."""
    accrued = _usd(stats.get("accrued_usd_e2", 0))
    pending_e2 = int(stats.get("usdc_pending_usd_e2", 0))
    pending = _usd(pending_e2)
    paid = _usd(stats.get("usdc_paid_usd_e2", 0))
    min_usd = int(terms.get("usdc_min_payout_usd", 0))
    gap_e2 = max(0, min_usd * 100 - pending_e2)
    if lang == "id":
        tail = f"Anda kurang {_usd(gap_e2)} dari payout ${min_usd}." if gap_e2 > 0 else "Siap untuk payout."
        return f"💰 Pendapatan: {accrued} · tertunda {pending} · dibayar {paid}. {tail}"
    if lang == "zh-hans":
        tail = f"距离 ${min_usd} 提现还差 {_usd(gap_e2)}。" if gap_e2 > 0 else "可以提现了。"
        return f"💰 收益：{accrued} · 待付 {pending} · 已付 {paid}。{tail}"
    tail = f"You're {_usd(gap_e2)} from your ${min_usd} payout." if gap_e2 > 0 else "Ready for payout."
    return f"💰 Earned: {accrued} · pending {pending} · paid {paid}. {tail}"


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
        f"Use my link and get {bonus} free alerts 👇"
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
    earnings = _earnings_line(stats, data.get("terms", {}) or {}, lang)

    if lang == "id":
        return (
            "🎁 Program referral AlgoVault\n\n"
            f"Bagikan link Anda. Setiap teman yang bergabung lewat link itu:\n"
            f"• mendapat {bonus} panggilan bonus, dan\n"
            f"• Anda dapat {pct}% dari langganan mereka selama {months} bulan.\n\n"
            f"Link referral Anda:\n{deep_link}\n\n"
            f"📊 Diajak: {signups} · Berlangganan: {conversions}\n"
            f"{earnings}\n\n"
            "Ketuk tombol di bawah untuk berbagi."
        )
    if lang == "zh-hans":
        return (
            "🎁 AlgoVault 推荐计划\n\n"
            f"分享你的链接。每位通过它加入的好友：\n"
            f"• 获得 {bonus} 次奖励调用，且\n"
            f"• 你可从其订阅中获得 {pct}% 佣金，持续 {months} 个月。\n\n"
            f"你的推荐链接：\n{deep_link}\n\n"
            f"📊 已推荐：{signups} · 已订阅：{conversions}\n"
            f"{earnings}\n\n"
            "点击下方按钮分享。"
        )
    return (
        "🎁 AlgoVault referral program\n\n"
        "Share your link. Every friend who joins through it:\n"
        f"• gets {bonus} bonus calls, and\n"
        f"• you earn {pct}% of their subscription for {months} months.\n\n"
        f"Your referral link:\n{deep_link}\n\n"
        f"📊 Referred: {signups} · Subscribed: {conversions}\n"
        f"{earnings}\n\n"
        "Tap the button below to share."
    )


def format_ref_join_greeting(
    bonus_calls: int,
    terms: dict[str, Any],
    monthly_total: int,
    lang_code: str | None = None,
) -> str:
    """Greeting when a NEW user joins via someone's ref link (the referee bonus + the give-get pitch).

    GROWTH-TG-QUOTA-PARITY-W1 CH3 (ratified 2026-08-27, Q1=a): the free allowance is INTERPOLATED
    in all three languages. It was hand-typed here — three more copies than §2's table counted, and
    gate leg L5 now makes the literal form unwritable.

    🛑 The sentence deliberately states NO UNIT for `{monthly_total}`. Two words earlier it says
    "bonus calls", and the referral unit noun is genuinely SPLIT in this module today: `bonus_calls`
    is minted by signal-MCP's REFERRAL_TERMS in API CALLS and spent by `quota.consume_quota` per
    DELIVERED ALERT. Naming a unit here would make one sentence claim two different units for two
    quantities drawn from the same pool. Resolving that is `OPS-BOT-REFERRAL-UNIT-NOUN-W1`, not
    this chapter — an allowance wave is the wrong place to settle a cross-meter naming question.
    """
    lang = normalize_lang(lang_code)
    _b, pct, months = _terms(terms)
    if lang == "id":
        return (
            f"🎉 Selamat datang di AlgoVault! Anda mendapat {bonus_calls} panggilan bonus "
            f"di atas jatah gratis {monthly_total}/bulan.\n\n"
            f"Ingin lebih? Ajak teman — mereka dapat {bonus_calls} bonus juga, dan Anda dapat "
            f"{pct}% dari langganan mereka selama {months} bulan. Ketik /referral."
        )
    if lang == "zh-hans":
        return (
            f"🎉 欢迎来到 AlgoVault！你已获得 {bonus_calls} 次奖励调用"
            f"（在每月 {monthly_total} 次免费额度之外）。\n\n"
            f"想要更多？推荐好友——他们同样获得 {bonus_calls} 次奖励，你可从其订阅中获得 "
            f"{pct}% 佣金，持续 {months} 个月。发送 /referral。"
        )
    return (
        f"🎉 Welcome to AlgoVault! You got {bonus_calls} bonus calls on top of your "
        f"free {monthly_total}/month.\n\n"
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


def format_referral_nudge(lang_code: str | None = None) -> str:
    """TG-REFERRAL-W1 / C3 — the value-moment nudge appended to a trade-call alert.
    QUALITATIVE on purpose (no program numbers — /referral shows the live SoT terms),
    so it never hardcodes / drifts from the engine numbers (numerical-citation LAW)."""
    lang = normalize_lang(lang_code)
    if lang == "id":
        return (
            "💡 Suka sinyalnya? Ajak teman ke AlgoVault — mereka dapat panggilan bonus "
            "dan Anda dapat komisi dari langganan mereka. Ketik /referral"
        )
    if lang == "zh-hans":
        return (
            "💡 觉得信号有用？邀请好友加入 AlgoVault——他们获得奖励调用，"
            "你可从其订阅中获得佣金。点击 /referral"
        )
    return (
        "💡 Getting value? Invite friends to AlgoVault — they get bonus calls and you "
        "earn commission on their subscription. Tap /referral"
    )


# ── REFERRAL-PARITY-NOTIFS-W1 / C2 — auto-notification rendering + opt-out ──

def format_notification(event: str, payload: dict[str, Any] | None, lang_code: str | None = None) -> str:
    """Render a referrer notification (friend_joined | commission_earned) from the
    engine payload (SoT numbers embedded by the engine; never hardcoded here)."""
    lang = normalize_lang(lang_code)
    p = payload or {}
    pct = int(p.get("commission_pct", 0))
    months = int(p.get("commission_months", 0))
    if event == "friend_joined":
        if lang == "id":
            return (
                f"👋 Teman baru bergabung lewat link Anda! Jika mereka berlangganan, "
                f"Anda dapat {pct}% dari paket mereka selama {months} bulan. Bagikan lagi → /referral"
            )
        if lang == "zh-hans":
            return (
                f"👋 有好友通过你的链接加入了！若其订阅，你可获得其套餐 {pct}% 佣金，"
                f"持续 {months} 个月。再分享 → /referral"
            )
        return (
            f"👋 A friend just joined with your link! If they subscribe, you earn "
            f"{pct}% of their plan for {months} months. Share again → /referral"
        )
    # commission_earned
    amount = _usd(p.get("amount_usd_e2", 0))
    pending = _usd(p.get("pending_usd_e2", 0))
    min_usd = int(p.get("usdc_min_payout_usd", 0))
    if lang == "id":
        return (
            f"💸 Anda dapat {amount} — teman yang Anda ajak berlangganan. Tertunda: {pending} "
            f"(dibayar pada ${min_usd}, sebelum tanggal 10 bulan berikutnya). Lihat pendapatan → /referral"
        )
    if lang == "zh-hans":
        return (
            f"💸 你赚得 {amount}——你推荐的好友订阅了。待付：{pending}"
            f"（满 ${min_usd} 后于次月 10 日前支付）。查看收益 → /referral"
        )
    return (
        f"💸 You earned {amount} — a friend you referred subscribed. Pending payout: {pending} "
        f"(paid at ${min_usd}, by the 10th of next month). See your earnings → /referral"
    )


def format_notifications_toggle(opt_out: bool | None, lang_code: str | None = None) -> str:
    """/notifications copy. opt_out None → usage; True → turned off; False → turned on."""
    lang = normalize_lang(lang_code)
    if opt_out is True:
        if lang == "id":
            return "🔕 Notifikasi referral DIMATIKAN. Aktifkan lagi → /notifications on"
        if lang == "zh-hans":
            return "🔕 推荐通知已关闭。重新开启 → /notifications on"
        return "🔕 Referral notifications are OFF. Turn back on → /notifications on"
    if opt_out is False:
        if lang == "id":
            return "🔔 Notifikasi referral DIAKTIFKAN — Anda akan diberi tahu saat teman bergabung atau Anda dapat komisi."
        if lang == "zh-hans":
            return "🔔 推荐通知已开启——好友加入或你获得佣金时会通知你。"
        return "🔔 Referral notifications are ON — you'll hear when a friend joins or you earn."
    if lang == "id":
        return "🔔 Notifikasi join + pendapatan referral aktif. Matikan → /notifications off · Aktifkan → /notifications on"
    if lang == "zh-hans":
        return "🔔 推荐加入与收益通知已开启。关闭 → /notifications off · 开启 → /notifications on"
    return "🔔 Referral join + earnings alerts are on. Turn off → /notifications off · on → /notifications on"
