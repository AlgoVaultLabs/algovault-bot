"""GROWTH-TG-CHANNEL-ACQUISITION-W1 / CH2 — signup_url carries the source.

Two ORTHOGONAL dimensions ride the outbound URL and must never collapse into one:
    utm_campaign -> WHICH in-bot CTA converted  (the declared inventory below)
    utm_medium   -> HOW the user found the bot  (this wave)

utm_source stays `tg_bot` forever: signal-MCP's deriveChannel keys the channel slug
off it, so re-slugging would orphan every historical row. Add; never re-slug.

Covers CH2 AC 2.1-2.3.
"""
from __future__ import annotations

import re
from pathlib import Path

from algovault_bot import keyboards, quota
from algovault_bot.messages import signup_url

SRC = Path(__file__).resolve().parents[1] / "src" / "algovault_bot"

# The FULL live inventory — 11 tags across two call paths. Step 0 verified the count
# and corrected the spec's attribution: `help_message` is a handlers.py:1301 call
# site (via keyboards.upgrade_markup), not a keyboards.py literal.
# GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R5 rebalanced these three sets, and the
# direction of travel is the whole wave: a money CTA is a BUTTON, so tags move DIRECT -> BUTTON
# as each surface stops pasting a URL into prose. The sets stay disjoint and their union stays
# an ENUMERATION — never a count — so a deletion cannot hide inside a total that still adds up.
# 🛑 DIRECT_TAGS IS EMPTY, AND THAT EMPTINESS IS THE WAVE'S ACCEPTANCE CRITERION.
#
# It held ten tags at the start of CH1. Every one of them was a `signup_url('<tag>')` literal
# sitting in a message body, which is the class this wave exists to retire: Telegram auto-links
# a scheme-less domain as `http://`, and the operator's own tap on the downgrade notice did not
# work. `link_downgraded` was the last to go, in CH2 with `messages.link_downgraded_message`.
#
# The set STAYS DECLARED rather than being deleted. An empty set is a statement — "no money CTA
# in this bot is a URL in prose" — and it is the thing that goes RED the moment someone writes
# the eleventh one. Deleting it would retire the assertion along with the debt.
#
# (`set()` and not `{}` — braces with only comments inside are an empty DICT, and `dict | set`
# raises at import. The gate caught it; recorded because it is a one-character trap.)
#
# RETIRED BY THIS WAVE, each with the copy that carried it:
#   regime_alert    — `cta.regime_cta_text`, reachable only through a bare `return False`
#   quota_100       — the caption branch the wall refuses before it can render
#   watchlist_cap   — `messages.cap_reached_message`, a cap that does not exist
#   quota_75/90, the four `*_quota_exhausted`, quota_exhausted_push — now picker BUTTONS
#   link_downgraded — `messages.link_downgraded_message`, retired in CH2
DIRECT_TAGS: set[str] = set()
# Tags carried by the plan picker's BUTTONS — `keyboards.plan_picker_kb`, called with a STRING
# LITERAL campaign at each composer's own site in `notices.py`. The literal is not stylistic: the
# regex below sees only a quoted literal second argument, so a variable would make eight of these
# undiscoverable while this gate still printed PASS.
BUTTON_TAGS = {
    "start_welcome", "help_message", "upgrade_command",           # handlers.py
    "plan_wall", "quota_exhausted_push",                          # notices.py — the two walls
    "scan_quota_exhausted", "regime_quota_exhausted",             # notices.py — the pull lanes
    "call_quota_exhausted", "funding_quota_exhausted",
    "quota_75", "quota_90",                                       # notices.py — the captions
    "quota_followup_3d", "quota_followup_7d",                     # notices.py — the cadence
    "link_downgraded",                                            # notices.py — the win-back
}
# GATED_TAGS is EMPTY and stays declared. `link_downgraded` left it because the notice is LIVE —
# the operator received one on 2026-09-07, which is what dispatched this wave. An empty set here
# is a statement (nothing is gated today), not a leftover.
GATED_TAGS: set[str] = set()
ALL_TAGS = DIRECT_TAGS | BUTTON_TAGS | GATED_TAGS


# ── AC 2.2 — untagged is BYTE-IDENTICAL to before the wave ────────────────


def test_untagged_url_is_byte_identical_to_pre_wave():
    """The QUERY STRING is the historical artifact; the SCHEME is not.

    Re-pinned by GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R3, which moved `https://`
    out of `keyboards.py` and into `SIGNUP_BASE` so a TEXT CTA could never again emit a
    scheme-less domain for Telegram to guess at.

    🛑 WHAT THIS PIN PROTECTS IS UNCHANGED, and it is asserted separately below: ~400 historical
    `signup_attribution` rows were minted with this exact PATH AND QUERY, and signal-MCP's
    `deriveChannel` keys the channel slug off `utm_source=tg_bot`. Re-slugging any of that would
    orphan every one of them. The emitted BUTTON url is byte-identical before and after this
    wave — measured — because the keyboard used to prepend the very scheme the base now carries.
    """
    assert (
        signup_url("quota_100")
        == "https://api.algovault.com/signup?plan=starter&utm_source=tg_bot&utm_campaign=quota_100"
    )
    # The half that is genuinely historical, pinned on its own so a future scheme change cannot
    # take the query string with it.
    assert signup_url("quota_100").endswith(
        "/signup?plan=starter&utm_source=tg_bot&utm_campaign=quota_100"
    )
    # absence is absence: no empty parameter, no utm_medium=none
    for falsy in (None, ""):
        assert signup_url("quota_100", falsy) == signup_url("quota_100")
        assert "utm_medium" not in signup_url("quota_100", falsy)


# ── AC 2.1 — a tagged user's URL carries the source, utm_source untouched ──


def test_tagged_url_carries_source_and_keeps_utm_source_tg_bot():
    url = signup_url("scan_quota_exhausted", "x")
    assert "utm_source=tg_bot" in url, "deriveChannel keys off this — never re-slug"
    assert "utm_campaign=scan_quota_exhausted" in url
    assert "utm_medium=x" in url
    # the two dimensions stay separate
    assert url.count("utm_campaign=") == 1 and url.count("utm_medium=") == 1


def test_source_rides_utm_medium_which_was_verified_free():
    """utm_medium is NULL on all 396 live signup_attribution rows and is already
    read by signal-MCP — so this needs ZERO change in that repo."""
    assert signup_url("quota_100", "devto").endswith("&utm_medium=devto")


# ── AC 2.3 — every declared tag still emits its existing value ────────────


def test_all_campaign_tags_still_emit_unchanged():
    for tag in ALL_TAGS:
        assert f"utm_campaign={tag}" in signup_url(tag)
        # and adding a source never disturbs the campaign
        assert f"utm_campaign={tag}" in signup_url(tag, "x")


def test_campaign_tag_inventory_matches_the_source():
    """Guards the inventory itself: a new tag (or a deleted one) must fail loudly rather
    than silently widen/narrow the readout's campaign dimension.

    The assertion is `found == ALL_TAGS` — the ENUMERATION, not a numeral. A hardcoded
    count here read `== 11` and had to be edited by this wave anyway, which is the whole
    argument against duplicating a fact that the set beside it already states."""
    found = set()
    # 🛑 `quota.py` IS IN THIS LIST, and adding it is half of what makes the gate meaningful.
    # It was absent until GROWTH-TG-PLAN-PICKER-W1 R3, so `quota_exhausted_push` — a live
    # `signup_url(...)` literal at two sites in the free wall's own copy since
    # BOT-QUOTA-REFUSAL-SEAM-W1 — was never in the inventory and nothing could notice. A gate
    # over a hand-listed subset of the corpus reports PASS over the files it does not read;
    # `scripts/check-quota-refusal-seam.py` learned the same lesson and now scans every module.
    # 🛑 AND `notices.py` IS IN IT TOO, for the same reason `quota.py` had to be added: it is
    # where every campaign literal now LIVES. A composer module absent from this list would make
    # the whole inventory invisible while the assertion below still passed.
    for name in (
        "cta.py", "handlers.py", "messages.py", "keyboards.py", "quota.py", "notices.py"
    ):
        text = (SRC / name).read_text(encoding="utf-8")
        # strip comments — a mention in a comment is not a call site
        code = "\n".join(
            ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
        )
        # 🛑 THE LOOKBEHIND IS LOAD-BEARING. `plan_signup_url` ENDS IN `signup_url`, so a bare
        # pattern matches its suffix and captures the PLAN ("starter") as if it were a campaign
        # tag — measured, this exact false positive appeared the moment R3 landed. Reject a
        # preceding identifier character; do not "simplify" it away.
        found |= set(
            re.findall(r"(?<![A-Za-z0-9_])signup_url\(\s*['\"]([a-z0-9_]+)['\"]", code)
        )
        # `plan_signup_url` takes plan + interval FIRST, so the campaign is its third argument;
        # `plan_picker_kb` takes it second. Both are matched, because this inventory is only
        # worth having if it sees every builder that can mint a campaign tag.
        found |= set(
            re.findall(
                r"plan_signup_url\(\s*[^,]+,\s*[^,]+,\s*['\"]([a-z0-9_]+)['\"]", code
            )
        )
        found |= set(
            re.findall(r"plan_picker_kb\(\s*[^,]+,\s*['\"]([a-z0-9_]+)['\"]", code)
        )
    assert found == ALL_TAGS, f"campaign inventory drifted: {found ^ ALL_TAGS}"
    # The groups must stay disjoint: a tag appearing in two of them would make the union
    # smaller than the sum and quietly hide a deletion.
    assert len(ALL_TAGS) == len(DIRECT_TAGS) + len(BUTTON_TAGS) + len(GATED_TAGS)


# ── the CTA button paths thread the source through ────────────────────────


def test_plan_picker_threads_source_into_every_button():
    """Replaces `test_upgrade_button_threads_source` — the builder it named is retired.

    The property is unchanged and now has four times the surface: EVERY button, not just the
    one, must carry the acquisition channel, and omitting the source must leave every url
    byte-identical to the untagged form.
    """
    from algovault_bot.quota import _fallback_ladder

    kb = keyboards.plan_picker_kb(_fallback_ladder(), "help_message", "geo")
    assert kb is not None
    # url-bearing buttons only: the ⭐ demand-probe row added by
    # GROWTH-TG-STARS-DEMAND-PROBE-W1 is a callback, and carries no utm because it goes nowhere.
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert len(urls) == 4
    for u in urls:
        assert u.startswith("https://")
        assert "utm_campaign=help_message" in u and "utm_medium=geo" in u
    kb0 = keyboards.plan_picker_kb(_fallback_ladder(), "help_message")
    assert kb0 is not None
    assert all(
        "utm_medium" not in b.url for row in kb0.inline_keyboard for b in row if b.url
    )


def test_composed_notices_carry_no_utm_medium_without_a_source():
    """Absence is absence, on the path that replaced the text CTAs.

    Re-pointed by V2 CH1 R3. Its two former subjects — `cta.regime_cta_text` and the
    `watchlist_cap` URL — are both DELETED with the copy that carried them, so the property they
    protected ("a user with no recorded acquisition source emits no empty `utm_medium`") now has
    to be asserted where the URLs are actually minted: the picker BUTTONS a composer attaches.
    Deleting the test with its subjects would have dropped the property silently.
    """
    from algovault_bot.notices import compose_wall
    from algovault_bot.quota import _fallback_ladder

    state = quota.QuotaState(used=200, total=200, window_start=None, pct_used=1.0)
    notice = compose_wall(state, _fallback_ladder(), None, "en")
    assert notice.markup is not None
    urls = [b.url for row in notice.markup.inline_keyboard for b in row if b.url]
    assert urls, "the wall must carry buttons"
    for u in urls:
        assert "utm_medium" not in u
        assert u.startswith("https://")
    assert "algovault.com" not in notice.text, "no URL in the body — the wave's whole point"


# ── AC 2.4 is enforced by the wave gate (git diff in the other repo), and ──
# ── AC 2.5 (backfill impossibility) is a status.md statement, not code.  ──


def test_backfill_is_structurally_impossible_not_merely_skipped():
    """The 26 historical tg_bot signups have no recoverable upstream: the bot never
    captured one, and signup_attribution's channel is derived from a spoofable query
    string. New-traffic-forward only. This test documents the constraint so a future
    wave cannot 'helpfully' invent a backfill."""
    from algovault_bot.db import UNKNOWN_ACQUISITION_SOURCE, normalize_acquisition_source

    # there is no value that means "reconstructed" — only a real tag or unknown
    assert normalize_acquisition_source("historical") == UNKNOWN_ACQUISITION_SOURCE
