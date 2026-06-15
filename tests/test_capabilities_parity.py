"""FEATURE-PARITY-CHANNELS-W1 CH5 — by-construction parity: the bot's REGISTERED
command surface covers exactly the bot-flagged tools derived from /capabilities.

This is the bot-side twin of the MCP drift canary (scripts/check-feature-registry-
drift.mjs): a tool flipped channels.bot=true in the registry that the bot does NOT
map/register fails this test until BOT_TOOL_SURFACE + a handler are added.
"""
from __future__ import annotations

from algovault_bot import capabilities as cap
from algovault_bot.handlers import register_handlers


class _CapturingApp:
    """Minimal duck-typed Application — captures add_handler() calls so we can read
    the registered CommandHandlers without constructing a live telegram Bot/Application."""

    def __init__(self) -> None:
        self.captured: list = []

    def add_handler(self, handler, *args, **kwargs) -> None:  # noqa: ANN001
        self.captured.append(handler)


def _registered_commands(db) -> set[str]:  # noqa: ANN001
    app = _CapturingApp()
    register_handlers(app, db)
    cmds: set[str] = set()
    for h in app.captured:
        c = getattr(h, "commands", None)  # only CommandHandler has .commands
        if c:
            cmds |= set(c)
    return cmds


def test_committed_snapshot_fully_covered() -> None:
    caps = cap._load_fallback_snapshot()
    assert caps is not None
    # Every channels.bot==true tool in the committed /capabilities is mapped in
    # BOT_TOOL_SURFACE (no silent gap — the by-construction parity invariant).
    assert cap.surface_coverage_gap(caps) == set()


def test_derived_surface_matches_expected() -> None:
    caps = cap._load_fallback_snapshot()
    assert cap.derive_commands(caps) == {"scan", "scanwatch", "funding"}
    assert cap.derive_alert_types(caps) == {"calls", "regime"}


def test_registered_commands_cover_the_derived_set(tmp_db) -> None:
    caps = cap._load_fallback_snapshot()
    registered = _registered_commands(tmp_db)
    # Every derived tool-pull command (scan + scanwatch) is actually registered, and
    # /unscanwatch (the scanwatch inverse) too.
    assert cap.derive_commands(caps) <= registered
    assert {"scan", "scanwatch", "unscanwatch", "funding"} <= registered
