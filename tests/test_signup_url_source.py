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

from algovault_bot import cta, keyboards, messages
from algovault_bot.messages import signup_url

SRC = Path(__file__).resolve().parents[1] / "src" / "algovault_bot"

# The FULL live inventory — 11 tags across two call paths. Step 0 verified the count
# and corrected the spec's attribution: `help_message` is a handlers.py:1301 call
# site (via keyboards.upgrade_markup), not a keyboards.py literal.
DIRECT_TAGS = {            # signup_url('<tag>') literals
    "regime_alert", "quota_100", "quota_90", "quota_75",          # cta.py
    "scan_quota_exhausted", "regime_quota_exhausted",             # handlers.py
    "call_quota_exhausted", "funding_quota_exhausted",            # handlers.py
    "watchlist_cap",                                              # messages.py
}
BUTTON_TAGS = {"start_welcome", "help_message"}   # via upgrade_button/upgrade_markup
# OPS-BOT-LINKED-TIER-REFRESH-W1 CH3d — the downgrade notice's reactivation link. It is a
# real conversion surface and so belongs in this inventory, but NOTHING SENDS IT YET: the
# copy is PENDING-MR1 and the send is gated off behind
# `ALGOVAULT_LINK_DOWNGRADE_NOTICE_ENABLED`. The tag is declared here because the call site
# exists in messages.py; declaring it is what keeps this gate meaningful rather than
# something a wave routes around.
GATED_TAGS = {"link_downgraded"}
ALL_TAGS = DIRECT_TAGS | BUTTON_TAGS | GATED_TAGS


# ── AC 2.2 — untagged is BYTE-IDENTICAL to before the wave ────────────────


def test_untagged_url_is_byte_identical_to_pre_wave():
    """Every pre-CH1 subscriber must emit exactly the old string."""
    assert (
        signup_url("quota_100")
        == "api.algovault.com/signup?plan=starter&utm_source=tg_bot&utm_campaign=quota_100"
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
    for name in ("cta.py", "handlers.py", "messages.py", "keyboards.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        # strip comments — a mention in a comment is not a call site
        code = "\n".join(
            ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
        )
        found |= set(re.findall(r"signup_url\(\s*['\"]([a-z0-9_]+)['\"]", code))
        found |= set(
            re.findall(r"upgrade_(?:button|markup)\(\s*['\"]([a-z0-9_]+)['\"]", code)
        )
    assert found == ALL_TAGS, f"campaign inventory drifted: {found ^ ALL_TAGS}"
    # The groups must stay disjoint: a tag appearing in two of them would make the union
    # smaller than the sum and quietly hide a deletion.
    assert len(ALL_TAGS) == len(DIRECT_TAGS) + len(BUTTON_TAGS) + len(GATED_TAGS)


# ── the CTA button paths thread the source through ────────────────────────


def test_upgrade_button_threads_source():
    b = keyboards.upgrade_button("help_message", "geo")
    assert b.url.startswith("https://")
    assert "utm_campaign=help_message" in b.url and "utm_medium=geo" in b.url
    # default stays byte-identical
    assert "utm_medium" not in keyboards.upgrade_button("help_message").url


def test_main_menu_upgrade_button_carries_source():
    kb = keyboards.main_menu_kb("awesome_list")
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert any("utm_medium=awesome_list" in u for u in urls)
    # and the pre-wave call site is untouched
    kb0 = keyboards.main_menu_kb()
    urls0 = [b.url for row in kb0.inline_keyboard for b in row if b.url]
    assert all("utm_medium" not in u for u in urls0)


def test_existing_cta_text_paths_unchanged_without_a_source():
    """cta.py renders identically for every user who has no recorded source."""
    assert "utm_medium" not in cta.regime_cta_text()
    assert "utm_campaign=regime_alert" in cta.regime_cta_text()
    assert "utm_medium" not in messages.signup_url("watchlist_cap")


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
