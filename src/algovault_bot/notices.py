"""THE composer for every message in which this bot asks for money.

GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 R1.

WHY THIS MODULE EXISTS
──────────────────────
On 2026-09-07 the operator received the live downgrade notice and its call to action —
``Reactivate any time: api.algovault.com/signup?...`` — did not work for him. The body carried a
SCHEME-LESS domain, so Telegram auto-linked it as ``http://``, and the tap depended on an
auto-linker's guess plus a 308 hop. The same shape was live on eight other money surfaces, each
one assembling its own body and its own URL. The plan-picker wave had already attached working
BUTTONS to the free wall, which left that surface showing a working button beside a
broken-looking link.

The class is "a money CTA is a URL pasted into prose". This module retires it by construction:
every money body is composed HERE, every CTA is the ONE keyboard (``keyboards.plan_picker_kb``),
and gate leg L6 in ``scripts/check-quota-refusal-seam.py`` makes the ninth surface unwritable.

WHAT A COMPOSER MAY AND MAY NOT DO
──────────────────────────────────
Composers are PURE. They take resolved values and return a ``MoneyNotice``; they never touch the
DB, never send, and never read ``.exhausted``/``.allowed`` — that decision belongs to
``quota.evaluate_delivery`` and reading it here would mint a second derivation the refusal-seam
gate exists to forbid. The caller resolves ``ladder``, ``src`` and ``lang`` and passes them in.

🛑 EVERY ``plan_picker_kb`` CALL PASSES ITS CAMPAIGN AS A STRING LITERAL, AT ITS OWN SITE.
Not a variable, not a module constant, not an f-string. ``tests/test_signup_url_source.py``
discovers the live campaign-tag inventory by regex over this module's source, and its matcher
sees only a quoted literal second argument — measured, a variable captures NOTHING. Passing
``campaign`` through would leave eight of ten tags undiscoverable while the gate still reported
PASS, which is the dark-guard shape this estate has paid for repeatedly. The small duplication
below is the price of the inventory staying real. (Architect ruling Q5, 2026-09-07.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from telegram import InlineKeyboardMarkup

from .keyboards import plan_picker_kb

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .quota import Ladder, QuotaState

__all__ = [
    "MoneyNotice",
    "PullSource",
    "WALL_FOLLOWUP_3D",
    "WALL_FOLLOWUP_7D",
    "WallFollowupKind",
    "compose_caption_cta",
    "compose_downgrade",
    "compose_followup",
    "compose_plan_wall",
    "compose_pull_refusal",
    "compose_wall",
]

#: The two wall-follow-up KINDS. These name a LEDGER ROW and a LOG EVENT; they are deliberately
#: NOT the campaign tags, which are `quota_followup_3d` / `quota_followup_7d` and name a
#: CONVERSION SURFACE in `signup_attribution`. Two vocabularies for two different questions —
#: "which notice did we send this chat this episode" versus "which CTA converted" — and the
#: `entitlement_drain.WALL_FOLLOWUPS` tuple is the one place they are paired.
WALL_FOLLOWUP_3D = "wall_followup_3d"
WALL_FOLLOWUP_7D = "wall_followup_7d"
WallFollowupKind = Literal["wall_followup_3d", "wall_followup_7d"]

#: The four on-demand lanes that can refuse for quota.
PullSource = Literal["scan", "regime", "call", "funding"]

#: Telegram's own ceiling is far higher, but the T2 voice rule is <= 300 characters per rendered
#: body and every trilingual composer asserts it. id runs ~15% longer than en; zh runs shorter.
MAX_BODY_CHARS = 300


@dataclass(frozen=True)
class MoneyNotice:
    """A composed money notice: what to say, what to attach, and what it is tagged as.

    ``markup`` is None only where there is genuinely nothing to sell — a subscriber already at
    the top of the self-serve ladder. A None markup is a DECISION, never a fallback.
    """

    text: str
    markup: InlineKeyboardMarkup | None
    campaign: str


def _lang_pick(lang: str | None, en: str, id_: str, zh: str) -> str:
    """The trilingual shape `paywall.py` already used, extracted so it is written once."""
    code = (lang or "en").lower().replace("_", "-")
    if code.startswith("id"):
        return id_
    if code.startswith("zh"):
        return zh
    return en


def _watches_phrase(lang: str | None, n_watches: int) -> str:
    """"1 watch" / "3 watches", in the recipient's language.

    Trilingual because the follow-up bodies are. The first draft rendered the English phrase
    into the id and zh bodies — an English noun sitting inside a translated sentence, which is
    the same defect class as a hand-typed figure: correct-looking, and wrong for most readers.
    Neither `id` nor `zh` inflects for number, so only `en` carries a plural branch.
    """
    return _lang_pick(
        lang,
        "1 watch" if n_watches == 1 else f"{n_watches} watches",
        f"{n_watches} pantauan",
        f"{n_watches} 个关注",
    )


def compose_wall(
    state: QuotaState, ladder: Ladder, src: str | None, lang: str | None
) -> MoneyNotice:
    """The FREE wall (d0) — §Copy A (monthly) / §Copy B (daily).

    Which wall is projected from ``state.limit_kind``, the single derivation `evaluate_delivery`
    already made. The copy layer never re-decides it: the contradiction that would produce is
    telling a user walled until midnight to come back in 30 days.
    """
    from .quota import reset_horizon, reset_sentence

    if state.limit_kind == "daily":
        text = _lang_pick(
            lang,
            f"Daily limit reached ({state.day_used}/{state.day_total} alerts today). "
            "Resets 00:00 UTC. Tap a plan below to upgrade.",
            f"Batas harian tercapai ({state.day_used}/{state.day_total} alert hari ini). "
            "Direset pukul 00:00 UTC. Ketuk paket di bawah untuk upgrade.",
            f"已达每日上限（今日 {state.day_used}/{state.day_total} 条提醒）。"
            "UTC 00:00 重置。点击下方套餐升级。",
        )
    else:
        when = reset_sentence("free", lang, reset_horizon(state.window_start))
        text = _lang_pick(
            lang,
            f"You've used all {state.used}/{state.total} free alerts. {when} "
            "Tap a plan below to upgrade.",
            f"Anda telah memakai semua {state.used}/{state.total} alert gratis. {when} "
            "Ketuk paket di bawah untuk upgrade.",
            f"您已用完全部 {state.used}/{state.total} 次免费提醒。{when}点击下方套餐升级。",
        )
    return MoneyNotice(
        text=text,
        markup=plan_picker_kb(ladder, "quota_exhausted_push", src),
        campaign="quota_exhausted_push",
    )


def compose_plan_wall(
    state: QuotaState,
    ladder: Ladder,
    src: str | None,
    lang: str | None,
    *,
    next_plan: dict | None,
    top_of_ladder: str,
) -> MoneyNotice:
    """The PAID wall — §Copy C.

    🛑 THE NEXT RUNG COMES FROM THE SERVER, NOT FROM US. ``next_plan`` is `plan_next_json`, the
    server's own answer for THIS subscriber; the ladder is a fallback for a single absent figure,
    never the authority on which rung is next. Naming a rung here would make the bot the
    authority on the shape of the ladder — the `_TIER_QUOTA` failure mode that was wrong for
    every linked subscriber from the day the ladder moved. (Architect ruling Q6, 2026-09-07.)

    ``next_plan is None`` means there is no self-serve rung above this subscriber. Say the
    ratified top-of-ladder sentence and attach NOTHING: a button that leads nowhere is worse
    than no button.
    """
    from .quota import _parse_ts, reset_horizon, reset_sentence

    tier = (state.effective_tier.tier or "").capitalize()
    used, total = state.plan_used, state.plan_total
    figures = f"{used}/{total}" if used is not None and total is not None else ""

    if (state.plan_limit_kind or "monthly") == "daily":
        when = _lang_pick(lang, "Resets 00:00 UTC.", "Direset pukul 00:00 UTC.", "UTC 00:00 重置。")
    else:
        # `plan_period_start` is the raw mirror TEXT column; `_parse_ts` is the one parser.
        when = reset_sentence("plan", lang, reset_horizon(_parse_ts(state.plan_period_start)))

    if not next_plan:
        text = _lang_pick(
            lang,
            f"{tier} plan allowance used: {figures}. {when} {top_of_ladder}",
            f"Kuota paket {tier} habis: {figures}. {when} {top_of_ladder}",
            f"{tier} 套餐额度已用完：{figures}。{when}{top_of_ladder}",
        )
        return MoneyNotice(text=text, markup=None, campaign="plan_wall")

    # `.get` and not a subscript: the server omits `label` on rungs the fixtures already carry,
    # and a KeyError on a live serving path is the one thing a wall must never do.
    label = (next_plan.get("label") or next_plan.get("id") or "").title()
    calls = next_plan.get("monthly_calls")
    if not isinstance(calls, int):
        calls = ladder.pro_monthly_calls
    text = _lang_pick(
        lang,
        f"{tier} plan allowance used: {figures}. {when} "
        f"Tap {label} below — {calls:,} alerts/mo.",
        f"Kuota paket {tier} habis: {figures}. {when} "
        f"Ketuk {label} di bawah — {calls:,} alert/bln.",
        f"{tier} 套餐额度已用完：{figures}。{when}"
        f"点击下方 {label} — 每月 {calls:,} 条提醒。",
    )
    return MoneyNotice(
        text=text,
        markup=plan_picker_kb(ladder, "plan_wall", src, above_tier=_above_tier(state)),
        campaign="plan_wall",
    )


def _above_tier(state: QuotaState) -> str | None:
    from .quota import picker_above_tier

    return picker_above_tier(state)


def compose_pull_refusal(
    source: PullSource,
    state: QuotaState,
    ladder: Ladder,
    src: str | None,
    lang: str | None,
) -> MoneyNotice:
    """An ON-DEMAND refusal (/scan, /regime, /call, /funding, the wizard one-shot, the menu).

    §Copy E is TWO bodies, projected from ``state.limit_kind`` exactly as the push wall is. All
    four sites previously interpolated the MONTHLY total while firing on monthly OR daily, so a
    user walled until midnight was shown the 30-day cap beside a midnight clock. The push lane
    fixed that at `build_refusal_text`; this projects from the same derivation.
    """
    from .quota import reset_horizon, reset_sentence

    if state.limit_kind == "daily":
        text = _lang_pick(
            lang,
            f"Daily limit reached ({state.day_used}/{state.day_total} alerts today). "
            "Resets 00:00 UTC. Tap a plan below for more.",
            f"Batas harian tercapai ({state.day_used}/{state.day_total} alert hari ini). "
            "Direset pukul 00:00 UTC. Ketuk paket di bawah untuk menambah.",
            f"已达每日上限（今日 {state.day_used}/{state.day_total} 条提醒）。"
            "UTC 00:00 重置。点击下方套餐获取更多。",
        )
    else:
        when = reset_sentence("free", lang, reset_horizon(state.window_start))
        text = _lang_pick(
            lang,
            f"You've used all {state.total} free alerts. {when} Tap a plan below for more.",
            f"Anda telah memakai semua {state.total} alert gratis. {when} "
            "Ketuk paket di bawah untuk menambah.",
            f"您已用完全部 {state.total} 次免费提醒。{when}点击下方套餐获取更多。",
        )

    # Four literal sites. See the module docstring: the inventory gate reads THESE.
    if source == "scan":
        markup, campaign = plan_picker_kb(ladder, "scan_quota_exhausted", src), "scan_quota_exhausted"
    elif source == "regime":
        markup, campaign = plan_picker_kb(ladder, "regime_quota_exhausted", src), "regime_quota_exhausted"
    elif source == "call":
        markup, campaign = plan_picker_kb(ladder, "call_quota_exhausted", src), "call_quota_exhausted"
    else:
        markup, campaign = plan_picker_kb(ladder, "funding_quota_exhausted", src), "funding_quota_exhausted"
    return MoneyNotice(text=text, markup=markup, campaign=campaign)


def compose_caption_cta(
    threshold: Literal["75", "90"], state: QuotaState, ladder: Ladder, src: str | None
) -> MoneyNotice:
    """The 75% / 90% alert-caption nudges — §Copy G. ENGLISH BY DESIGN.

    Alert bodies and their CTAs are English on every surface (`cta.py`), so these two are not
    trilingual and no `lang` is taken. That is a pre-existing ratified decision this wave carries
    forward rather than silently widening.
    """
    if threshold == "90":
        return MoneyNotice(
            text=f"🔥 Only {state.remaining} free alerts left. Tap a plan below to keep them coming.",
            markup=plan_picker_kb(ladder, "quota_90", src),
            campaign="quota_90",
        )
    return MoneyNotice(
        text="⏰ You've used 75% of your free alerts. Tap a plan below to keep them coming.",
        markup=plan_picker_kb(ladder, "quota_75", src),
        campaign="quota_75",
    )


def compose_followup(
    kind: WallFollowupKind,
    state: QuotaState,
    ladder: Ladder,
    src: str | None,
    lang: str | None,
    *,
    n_watches: int,
    days_since_wall: int,
) -> MoneyNotice:
    """A WALL FOLLOW-UP — §Copy H (d3) / §Copy I (d7).

    ``days_since_wall`` is the pass's own per-candidate figure, not a hand-typed 3: the first
    live cohort was aged 1.8–6.8 days, so a literal would have told most of them a number that
    was false about themselves. It is >= 3 by the candidate predicate, so the plural always
    holds. (Architect ruling Q11, 2026-09-07.)

    §Copy H's free path is the referral, stated the way the shipped `/referral` copy states it:
    the FRIEND gets bonus alerts on joining, and the referrer earns a commission IF THEY
    SUBSCRIBE. The earlier draft said "you both get bonus alerts", which is false for the
    referrer — `db.grant_referral_bonus` has exactly one caller, the referee's join.
    """
    from .quota import reset_horizon

    watches = _watches_phrase(lang, n_watches)
    if kind == WALL_FOLLOWUP_7D:
        horizon = reset_horizon(state.window_start)
        # A follow-up requires a stamped wall inside a LIVE window, which `evaluate_delivery`'s
        # `limit_kind == "monthly"` guarantees — the SQL alone does not, because the 30-day roll
        # nulls the window between the row read and the state read.
        assert horizon is not None, "a monthly-walled chat always has a window start"
        date = horizon[0]
        text = _lang_pick(
            lang,
            f"Still paused: {watches} waiting. Your free alerts reset on {date}. "
            "Tap a plan below and alerts resume now.",
            f"Masih dijeda: {watches} menunggu. Alert gratis Anda direset {date}. "
            "Ketuk paket di bawah dan alert berjalan lagi sekarang.",
            f"仍暂停中：{watches} 等待中。您的免费提醒将于 {date} 重置。"
            "点击下方套餐，提醒立即恢复。",
        )
        return MoneyNotice(
            text=text,
            markup=plan_picker_kb(ladder, "quota_followup_7d", src),
            campaign="quota_followup_7d",
        )

    text = _lang_pick(
        lang,
        f"Your alerts have been paused for {days_since_wall} days — {watches} waiting. "
        "Tap a plan below to resume today. Prefer free? Share your link with /referral: "
        "friends get bonus alerts, and you earn commission if they subscribe.",
        f"Alert Anda dijeda selama {days_since_wall} hari — {watches} menunggu. "
        "Ketuk paket di bawah untuk lanjut hari ini. Mau tetap gratis? Bagikan tautan Anda "
        "dengan /referral: teman dapat alert bonus, dan Anda dapat komisi jika mereka berlangganan.",
        f"您的提醒已暂停 {days_since_wall} 天 — {watches} 等待中。"
        "点击下方套餐立即恢复。想继续免费？用 /referral 分享您的链接："
        "好友获得奖励提醒，好友订阅后您获得佣金。",
    )
    return MoneyNotice(
        text=text,
        markup=plan_picker_kb(ladder, "quota_followup_3d", src),
        campaign="quota_followup_3d",
    )


def compose_downgrade(
    monthly_total: int, daily_total: int, lang: str | None
) -> MoneyNotice:
    """The DOWNGRADE notice — §Copy D. GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH2 R7c.

    🛑 THIS IS THE MESSAGE THAT DISPATCHED THE WAVE. On 2026-09-07 the operator received the
    live version and its call to action — `Reactivate any time: api.algovault.com/signup?...` —
    did not work for him: the body carried a scheme-less domain, Telegram auto-linked it as
    `http://`, and the tap depended on an auto-linker's guess plus a 308 hop. The URL is now a
    plan-picker BUTTON and the sentence ends by pointing at it.

    🛑 RATIFIED PUBLIC COPY, twice. The 2026-08-21 ratification approved everything up to the
    final clause; this wave's dispatch re-ratifies the whole string with that clause replaced.
    Editing it is a public-copy change needing fresh sign-off, not a wording tidy-up.
    `tests/test_link_lifecycle.py` pins it verbatim, in all three languages.

    The shape is unchanged and every part of it is load-bearing: state the fact, assign no
    blame, give ONE action, and say explicitly what did NOT change — a subscriber whose
    watchlist silently vanished would read this as data loss on top of a billing problem.

    It takes the ladder figures rather than a `Ladder`, because its caller (the entitlement
    drain) already resolved them for the downgrade it is applying, and re-resolving would be a
    second read of the same mirror inside one operation.
    """
    from .quota import _fallback_ladder

    text = _lang_pick(
        lang,
        "Your AlgoVault subscription no longer appears active, so this chat has moved back "
        f"to the free tier ({monthly_total} alerts/month, {daily_total}/day). "
        "Your watchlist is unchanged. Reactivate any time — tap a plan below.",
        "Langganan AlgoVault Anda tampaknya sudah tidak aktif, jadi chat ini kembali ke "
        f"tier gratis ({monthly_total} alert/bulan, {daily_total}/hari). "
        "Watchlist Anda tidak berubah. Aktifkan kembali kapan saja — ketuk paket di bawah.",
        f"你的 AlgoVault 订阅似乎已不再有效，此对话已回到免费套餐（每月 {monthly_total} 条提醒，"
        f"每日 {daily_total} 条）。你的自选列表未受影响。随时可重新订阅——点击下方套餐。",
    )
    return MoneyNotice(
        text=text,
        markup=plan_picker_kb(_fallback_ladder(), "link_downgraded", None),
        campaign="link_downgraded",
    )
