"""OPS-BOT-DEAD-SURFACE-SWEEP-W1 CH2 R4 (2026-09-09): the ten dead identifiers stay dead.

WHY THIS EXISTS

The wave RETIRED the `/unlock_premium_alerts` mechanic but deliberately did NOT drop its
schema: four `subscribers` column families and the `tg_pro_grants` table survive as dead
migrations, because dropping a column on live SQLite reclaims nothing and risks the file
(the house precedent is `db.py`'s dead `C5_MIGRATIONS`).

Dead schema that nothing enforces is how the next reader wires a new feature onto a column
whose writer no longer exists — a writer-with-no-reader is exactly the defect class this
wave retired, and re-growing a reader-with-no-writer is its mirror image. So the columns
are asserted UNREFERENCED rather than merely uncalled.

The spec's Carried Ruling 2 named FOUR columns. Measured at `origin/main 475ddd7` there are
TEN identifiers, and its four were the wrong four (it swapped `unlock_verified_at` for
`npm_unlock_session_id`). A test over four of ten is the vacuity shape this estate keeps
paying for, so the list below is the measured one.

SCOPE: `src/**` and `scripts/**`. The migration statements in `db.py` that DECLARE these
columns are exempt by path — they are the declaration, not a reader.
"""
from __future__ import annotations

import ast
import pathlib
import warnings

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# The ten identifiers retired by this wave. Six unlock columns + the grant table + the
# three PAYWALL_HOOK_MIGRATIONS columns, whose readers (`has_fired_this_month`,
# `mark_fired`) died with the composer wave.
DEAD_IDENTIFIERS = (
    "unlock_status",
    "unlock_verified_at",
    "unlock_method",
    "unlock_screenshot_path",
    "npm_unlock_session_id",
    "npm_unlock_detected_at",
    "tg_pro_grants",
    "quota_hit_soft_at",
    "quota_hit_hard_at",
    "quota_hit_block_at",
)

# The ONE file allowed to name them: it declares the migrations that keep them alive on disk.
DECLARATION_PATH = REPO / "src" / "algovault_bot" / "db.py"

# Paths exempt for a stated reason, SELF-RETIRING: `test_exemptions_have_not_outlived_their_files`
# below fails the moment an exempt path stops existing, so an exemption cannot quietly outlive
# the thing it excuses. CLAUDE.md: an exemption and its test are a pair — leaving either half
# behind half-disables the guard.
EXEMPT: dict[pathlib.Path, str] = {
    # Empty since CH3 deleted `paywall.py` and this entry in the same commit, exactly as the
    # entry's own reason required. Kept as a declared-empty dict so the next exemption has to
    # state a reason and inherit the self-retiring assertion below.
}


def _source_files() -> list[pathlib.Path]:
    files = sorted(
        p
        for d in ("src", "scripts")
        for p in (REPO / d).rglob("*.py")
        if "egg-info" not in p.parts
    )
    # Vacuity guard: this corpus is CONSTRUCTED here, so an empty one is a defect in the
    # test, never a fact about the world.
    assert files, "corpus is empty — the scan found no source files to check"
    return files


def _code_only(path: pathlib.Path) -> str:
    """The file's CODE, with comments and docstrings removed.

    A MENTION IS NOT A REFERENCE. `scripts/check-orphan-modules.py`'s docstring names
    `tg_pro_grants` and `unlock_status` in the paragraph stating honestly what that gate does
    NOT cover — the most valuable lines in the file. A naive substring scan flags them and
    demands their deletion, which is the trap `verification-gates.md` records for exactly this
    class of guard (`check-canaries-wired.mjs` strips comments for the same reason).

    `ast.unparse` drops comments for free; docstrings are cleared explicitly first. Anything
    surviving is a real reference — a name, an attribute, or a live SQL string literal.
    """
    with warnings.catch_warnings():
        # We are READING these files, not running them: another module's own SyntaxWarning
        # (e.g. `check-quota-refusal-seam.py`'s unescaped `\s` inside a docstring) is not a
        # finding of this test, and that file is out of this wave's scope.
        warnings.simplefilter("ignore")
        tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value.value = ""
    return ast.unparse(tree)


@pytest.mark.parametrize("identifier", DEAD_IDENTIFIERS)
def test_dead_identifier_has_no_reader(identifier: str) -> None:
    """No `src/` or `scripts/` module may REFERENCE a retired identifier.

    `db.py` is exempt by path: it holds the ALTER/CREATE statements that declare them.
    Comments and docstrings are stripped first — see `_code_only`.
    """
    offenders = [
        str(p.relative_to(REPO))
        for p in _source_files()
        if p != DECLARATION_PATH and p not in EXEMPT and identifier in _code_only(p)
    ]
    assert not offenders, (
        f"{identifier!r} was retired by OPS-BOT-DEAD-SURFACE-SWEEP-W1 but is referenced in: "
        f"{', '.join(offenders)}"
    )


def test_prose_mention_is_not_a_reference() -> None:
    """The comment/docstring stripper is itself proven, in BOTH directions.

    Without this, the stripper could silently stop stripping (every assertion above goes
    vacuously green) or over-strip (real references invisible). Both directions asserted.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        prose = pathlib.Path(td) / "prose.py"
        prose.write_text('"""A docstring naming unlock_status."""\n# and a comment: tg_pro_grants\nx = 1\n')
        assert "unlock_status" not in _code_only(prose)
        assert "tg_pro_grants" not in _code_only(prose)

        real = pathlib.Path(td) / "real.py"
        real.write_text('row = data["unlock_status"]\nq = "SELECT * FROM tg_pro_grants"\n')
        assert "unlock_status" in _code_only(real)
        assert "tg_pro_grants" in _code_only(real)


def test_declaration_file_still_declares_them() -> None:
    """The mirror assertion, and the one that makes the test above non-vacuous.

    If a future wave DROPS these columns, the parametrized test starts passing for a
    reason that has nothing to do with what it checks. This fails loudly instead.
    """
    src = DECLARATION_PATH.read_text()
    missing = [i for i in DEAD_IDENTIFIERS if i not in src]
    assert not missing, (
        f"db.py no longer declares {missing} — the no-reader assertions above are now "
        "vacuous. If the drop was intentional, retire this test in the same commit."
    )


def test_dead_migrations_are_annotated() -> None:
    """Each dead migration block carries its `# DEAD since` marker.

    Not decoration: the marker is what tells the next reader the column has no writer,
    which is the only thing standing between them and wiring a feature onto it.
    """
    src = DECLARATION_PATH.read_text()
    marker = "# DEAD since OPS-BOT-DEAD-SURFACE-SWEEP-W1"
    assert src.count(marker) == 4, (
        f"expected 4 annotated migration blocks (PAYWALL_HOOK / UNLOCK_STATE / "
        f"PRO_GRANTS_TABLE / NPM_UNLOCK), found {src.count(marker)}"
    )
    # `referral_code` is LIVE and sits inside UNLOCK_STATE_MIGRATIONS. The annotation must
    # never be placed where it would cover that statement.
    assert "ALTER TABLE subscribers ADD COLUMN referral_code TEXT" in src
    assert "`referral_code`) is LIVE" in src, (
        "the referral_code exemption note is gone — a tuple-level DEAD marker would libel "
        "a column that handlers.py and referral_drain.py read"
    )


def test_exemptions_have_not_outlived_their_files() -> None:
    """An exemption must die with the thing it excuses.

    `paywall.py` is exempt only because CH2's firewall forbids writing it; CH3 deletes the
    file. Without this assertion the exemption would sit in the corpus forever, silently
    excusing a path that no longer exists — the shape CLAUDE.md records as half-disabling a
    guard. This fails the moment CH3 lands, and the fix is to delete the entry.
    """
    stale = [str(p.relative_to(REPO)) for p in EXEMPT if not p.exists()]
    assert not stale, (
        f"exemption(s) for {stale} outlived their file(s) — delete the EXEMPT entry in the "
        "same commit that deleted the path"
    )
