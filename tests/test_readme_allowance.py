"""GROWTH-TG-FREE-ALLOWANCE-W1 — every free-allowance figure README.md states is the pinned one.

WHY THIS EXISTS. README.md is public (GitHub) and ships with every deploy (the deploy manifest
lists it), and it hand-states the free allowance in prose. The 2026-08-27 parity wave fixed five
of those statements by hand; this wave would have been the second round of the same hand sweep.
Prose cannot import a constant, so this test is the binding: a README figure that disagrees with
`quota.FREE_TIER_MONTHLY_QUOTA` / `FREE_TIER_DAILY_QUOTA` fails the suite, and the pinned constants
are themselves held to the live ladder by `tests/test_ladder_client.py`.

SCOPE. The FREE allowance only. Starter / Pro figures (and anything on a line naming them or the
API's calls) are the paid ladder, rendered from the mirror elsewhere — out of scope by ruling.
Burn-rate estimates ("~5 alerts / mo per pair") are not allowance statements and match no pattern.

Each pattern names the phrase it binds. The vacuity guard below fails the test if either class
finds nothing: a README reworded past every pattern would otherwise pass by checking zero figures.
"""
from __future__ import annotations

import re
from pathlib import Path

from algovault_bot.quota import FREE_TIER_DAILY_QUOTA, FREE_TIER_MONTHLY_QUOTA

README = Path(__file__).resolve().parent.parent / "README.md"

#: Monthly-allowance phrases — every number captured must equal FREE_TIER_MONTHLY_QUOTA.
MONTHLY = (
    re.compile(r"(\d[\d,]*) alerts/month"),             # "your free **N alerts/month**"
    re.compile(r"\byour (\d[\d,]*)/mo\b"),               # "count toward your N/mo"
    re.compile(r"(\d[\d,]*) alerts / rolling 30 days"),  # the Anti-abuse meter
    re.compile(r"\babove (\d[\d,]*)\b"),                 # the daily qualifier's "above N"
)
#: Daily-allowance phrases — every number captured must equal FREE_TIER_DAILY_QUOTA.
DAILY = (
    re.compile(r"(\d[\d,]*) per UTC day"),
    re.compile(r"(\d[\d,]*) alerts / UTC day"),
    re.compile(r"(\d[\d,]*)/day\b"),
)
#: Lines that state the PAID ladder or the API's unit are out of scope.
PAID_LINE = re.compile(r"Starter|Pro\b|API calls")


def _figures(patterns: tuple[re.Pattern[str], ...]) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for lineno, line in enumerate(README.read_text(encoding="utf-8").splitlines(), start=1):
        if PAID_LINE.search(line):
            continue
        for pat in patterns:
            for m in pat.finditer(line):
                out.append((lineno, int(m.group(1).replace(",", "")), line.strip()[:90]))
    return out


def test_every_monthly_allowance_figure_is_the_pinned_one() -> None:
    found = _figures(MONTHLY)
    assert found, "no monthly allowance phrase matched — the README was reworded past this test"
    wrong = [f for f in found if f[1] != FREE_TIER_MONTHLY_QUOTA]
    assert not wrong, (
        f"README states a monthly free allowance other than {FREE_TIER_MONTHLY_QUOTA}: {wrong}"
    )


def test_every_daily_allowance_figure_is_the_pinned_one() -> None:
    found = _figures(DAILY)
    assert found, "no daily allowance phrase matched — the README was reworded past this test"
    wrong = [f for f in found if f[1] != FREE_TIER_DAILY_QUOTA]
    assert not wrong, (
        f"README states a daily free allowance other than {FREE_TIER_DAILY_QUOTA}: {wrong}"
    )


def test_the_paid_ladder_is_out_of_scope() -> None:
    """The exclusion is real, not decorative: the Starter upgrade line carries figures that would
    otherwise be misread (a price, an API-call count)."""
    paid = [
        line for line in README.read_text(encoding="utf-8").splitlines() if PAID_LINE.search(line)
    ]
    assert paid, "expected at least one paid-ladder line to exclude"
