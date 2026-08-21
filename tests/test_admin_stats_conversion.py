"""BOT-W2 C4 — admin /stats conversion attribution block tests."""

from __future__ import annotations

import pytest

from algovault_bot.admin import handle_stats, render_stats
from algovault_bot.db import Database


def test_stats_zero_state_includes_conversion_block(tmp_db: Database) -> None:
    text = render_stats(tmp_db)
    assert "Conversion attribution" in text
    assert "Linked subscribers : 0" in text
    assert "(none yet)" in text
    assert "CTAs → linked    : n/a" in text


def test_stats_with_linked_users_renders_breakdown(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "alice", "en")
    tmp_db.upsert_subscriber(2, "bob", "en")
    tmp_db.upsert_subscriber(3, "carol", "en")
    tmp_db.link_subscriber(1, "av_live_aaa", "starter")
    tmp_db.link_subscriber(2, "av_live_bbb", "starter")
    tmp_db.link_subscriber(3, "av_live_ccc", "pro")
    text = render_stats(tmp_db)
    assert "Linked subscribers : 3" in text
    # OPS-BOT-LINKED-TIER-REFRESH-W1 CH2: the breakdown projects through `effective_tier`
    # and carries the SOURCE. These three were linked but never mirrored, so every row is
    # "(link)" — last known, explicitly labelled as such rather than implied to be current.
    assert "starter (link)" in text
    assert "pro (link)" in text
    # 'starter' has 2 linked → larger count → appears before 'pro' (count desc)
    starter_idx = text.find("starter (link)")
    pro_idx = text.find("pro (link)")
    assert starter_idx > 0 and pro_idx > starter_idx


def test_stats_breakdown_prefers_a_FRESH_mirror_over_the_stale_link_copy(
    tmp_db: Database,
) -> None:
    """The defect, at the admin surface: chat 1061466212's exact shape.

    `linked_tier='starter'` (written at /link, June) + a fresh mirror saying `pro`. The
    operator must be shown Pro, and must be able to see that the server said so.
    """
    tmp_db.upsert_subscriber(1, "alice", "en")
    tmp_db.link_subscriber(1, "av_live_aaa", "starter")
    tmp_db.update_plan_mirror(
        1, {"tier": "pro", "used": 5294, "total": 100000, "allowed": True}, source="debit"
    )
    text = render_stats(tmp_db)
    assert "pro (mirror)" in text
    assert "starter" not in text


def test_stats_conversion_ratio_with_ctas(tmp_db: Database) -> None:
    tmp_db.upsert_subscriber(1, "alice", "en")
    tmp_db.upsert_subscriber(2, "bob", "en")
    # 4 CTAs shown across 2 users; 1 user linked → 25%
    for _ in range(2):
        tmp_db.increment_total_ctas_shown(1)
        tmp_db.increment_total_ctas_shown(2)
    tmp_db.link_subscriber(1, "av_live_aaa", "starter")
    text = render_stats(tmp_db)
    assert "CTAs → linked    : 25.0%" in text


def test_stats_admin_only_gate(tmp_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_ADMIN_CHAT_IDS", "777")
    assert handle_stats(tmp_db, 999) == "Not authorized."
    text = handle_stats(tmp_db, 777)
    assert "Conversion attribution" in text
