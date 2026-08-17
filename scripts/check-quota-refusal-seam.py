#!/usr/bin/env python3
"""BOT-QUOTA-REFUSAL-SEAM-W1 — the gate that makes a silent quota refusal unwritable.

WHY THIS EXISTS (read before "simplifying" it)

Three push lanes each re-derived "is this user out of quota?" and drifted to three
different answers: the watch lane refused silently (its notice site sat BEHIND the
scheduler's pre-skip and was unreachable), the scanwatch lane refused silently AND
wrote no telemetry, and the regime lane charged quota but never refused at all.
Measured 2026-08-16: two free subscribers were refused ~10,000 times over 7 days
without ever being told, and a third took 11 regime alerts while 10 units past the
wall. The repo ALSO carried a second, unrelated instance of the same class —
`paywall.py`, fully built and unit-tested, whose predicate the bot's own traffic
could never satisfy, dark for ~80 days.

CLAUDE.md's generator rule (`build-and-runtime.md`): the 4th same-class fix must
build a gate making the bug class structurally impossible. A unit test cannot do it —
`verification-gates.md` records that exact lesson ("a unit test calling a helper
directly cannot prove anything CALLS it"), which is how both dark primitives above
shipped green. So this gate reasons over the AST of real source, not over behaviour.

THE INVARIANT

`quota.REFUSAL_LANES` is the SoT: it maps the name of each function that reads the
quota decision to HOW that lane refuses.
  push — the user is ABSENT; refusing silently is invisible, so the lane MUST route
         through `refuse_and_notify`.
  pull — the user is PRESENT and waiting; the returned message IS the notice, so the
         refusal branch MUST return a value.

L1  every `.exhausted` read outside quota.py sits in a declared lane
L2  each lane honours the shape its declaration promises
L2b every declared lane still resolves to a real function with a real read
    (a stale entry rots into a permission slip — this is the leg that catches a
     `paywall.py`: a lane that stopped being reachable while looking wired)
L3  bot-facing copy states the BOT's unit, never the API's
    (`docs/METERING-DIVERGENCE.md` Rule 1, which failed as prose for its whole life —
     the Completeness Standard requires retiring such a rule into a gate)

CONTRACT: prints exactly one terminal `QUOTA_REFUSAL_SEAM_VERDICT=PASS|FAIL|
INDETERMINATE`. Callers gate on the TOKEN, never the exit code. Exit 0=PASS, 1=FAIL,
3=INDETERMINATE (the token-law default for a NEW gate; `check_test_baseline.sh` keeps
2 only because it already deployed 2 — that divergence must not be "aligned").
"""
from __future__ import annotations

import ast
import re
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "src" / "algovault_bot"
SEAM_MODULE = "quota.py"
REFUSAL_CALL = "refuse_and_notify"
DECIDE_CALL = "evaluate_delivery"
# A lane "reads the decision" if it touches EITHER surface of it: the raw
# `.exhausted` predicate (pull lanes still read it straight off QuotaState) or the
# seam's `evaluate_delivery()` → `.allowed` projection (push lanes). Tracking only
# `.exhausted` would have gone blind to every lane this wave migrated — the gate
# would pass by measuring nothing, which is the failure mode it exists to prevent.
DECISION_ATTRS = ("exhausted", "allowed")

# L3: the bot's own allowance (100) paired with the API's noun ("call"). Bans
# "100 free calls a month" but NOT "{tier} calls/mo", which correctly names the
# API ladder for a linked user.
COPY_BANS = (
    # the bot's own allowance (100) wearing the API's noun
    re.compile(r"\b100\b(?!\s*%)[^.\n]{0,24}\bcalls?\b", re.IGNORECASE),
    # ...and the same collision without the literal number ("5 free calls left").
    # "API calls" stays legal: that phrase names the API ladder, which IS in calls.
    re.compile(r"\bfree\s+calls?\b", re.IGNORECASE),
)
# Scanned STRUCTURALLY — every module in the package, never a hand-listed subset.
# A maintained allowlist is the shape `verification-gates.md` warns about, and it
# already failed here once: the first cut listed messages/handlers/cta and was
# blind to the identical string in `alert_engine.py`, `alert_image.py` and
# `referral.py` — a gate reporting PASS over copy it never looked at.


# ── L4: the bot may not hand-type the plan ladder ────────────────────────────
# PRICING-BOT-DELIVERY-METERING-W1 CH6b. `messages._TIER_QUOTA` hard-typed
# {"starter": 3_000, "pro": 15_000, "enterprise": 100_000} while the live ladder was
# 10,000/100,000/100,000 — wrong for every linked subscriber from the day the ladder moved, with
# nothing able to notice. Plan figures now come from the server mirror; a literal is the defect.
PAID_TIER_NAMES = frozenset({"starter", "pro", "enterprise", "x402"})

# L4b: an `ast` walk CANNOT SEE A COMMENT, and the stale ladder also lived in one
# ("real Stripe-backed quota (3K/15K/100K)"). L4 alone would have left it. Matches on the SHAPE of
# a ladder — two or more grouped figures separated by / or · — so `3K/15K/100K` and
# `3,000/15,000/100,000` both fail. Scanning source but not comments is the same partial-corpus
# near-miss L3's first cut already made; it is not repeated here.
LADDER_IN_COMMENT = re.compile(
    r"\b\d{1,3}(?:[,_]?\d{3}|K)\b(?:\s*[/·]\s*\b\d{1,3}(?:[,_]?\d{3}|K)\b){1,}"
)


@dataclass
class Findings:
    undeclared: list[str] = field(default_factory=list)
    wrong_shape: list[str] = field(default_factory=list)
    orphan_lanes: list[str] = field(default_factory=list)
    copy_violations: list[str] = field(default_factory=list)
    ladder_violations: list[str] = field(default_factory=list)
    lanes_seen: dict[str, list[str]] = field(default_factory=dict)
    corpus_files: int = 0

    @property
    def failures(self) -> list[str]:
        return (
            self.undeclared + self.wrong_shape + self.orphan_lanes + self.copy_violations
            + self.ladder_violations
        )


def _enclosing_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _reads_decision(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in DECISION_ATTRS:
            return True
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == DECIDE_CALL:
                return True
            if isinstance(f, ast.Attribute) and f.attr == DECIDE_CALL:
                return True
    return False


def _calls(node: ast.AST, name: str) -> bool:
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == name:
            return True
        if isinstance(f, ast.Attribute) and f.attr == name:
            return True
    return False


def _refusal_branch_returns_value(fn: ast.AST) -> bool:
    """A pull lane must RETURN something from the branch guarded by the decision.

    `return None` / a bare `return` does not satisfy it: the user is waiting on a
    reply, and returning nothing is the silent refusal this gate exists to forbid.
    """
    for n in ast.walk(fn):
        if not isinstance(n, ast.If) or not _reads_decision(n.test):
            continue
        for stmt in ast.walk(ast.Module(body=n.body, type_ignores=[])):
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                if not (
                    isinstance(stmt.value, ast.Constant) and stmt.value.value is None
                ):
                    return True
    return False


def scan(pkg_dir: Path, lanes: dict[str, str]) -> Findings:
    f = Findings()
    for path in sorted(pkg_dir.glob("*.py")):
        if path.name == SEAM_MODULE:
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as e:  # unparseable input we were HANDED → indeterminate
            raise RuntimeError(f"cannot parse {path.name}: {e}") from e
        f.corpus_files += 1
        for fn in _enclosing_functions(tree):
            if not _reads_decision(fn):
                continue
            where = f"{path.name}:{fn.lineno} {fn.name}()"
            f.lanes_seen.setdefault(fn.name, []).append(where)
            shape = lanes.get(fn.name)
            if shape is None:
                f.undeclared.append(
                    f"L1 {where} reads the quota decision but is not in REFUSAL_LANES"
                )
                continue
            if shape == "push" and not _calls(fn, REFUSAL_CALL):
                f.wrong_shape.append(
                    f"L2 {where} is declared 'push' but never calls {REFUSAL_CALL}() "
                    f"— a refused user would be told nothing"
                )
            if shape == "pull" and not _refusal_branch_returns_value(fn):
                f.wrong_shape.append(
                    f"L2 {where} is declared 'pull' but its refusal branch returns no "
                    f"message — the waiting user gets silence"
                )
    for name in lanes:
        if name not in f.lanes_seen:
            f.orphan_lanes.append(
                f"L2b REFUSAL_LANES declares '{name}' but no function by that name "
                f"reads the quota decision — stale entry, or the lane went dark"
            )
    return f


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id()s of every docstring Constant, so prose ABOUT the rule is not judged BY it."""
    out: set[int] = set()
    for n in ast.walk(tree):
        if not isinstance(
            n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(n, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def scan_copy(pkg_dir: Path) -> list[str]:
    """L3 over STRING LITERALS ONLY, via the AST.

    A line-based grep judges comments and docstrings as if they were copy, and the
    docblock EXPLAINING a banned form is the most valuable line in the file —
    `verification-gates.md` records that gate-writing bug verbatim. Stripping `#`
    is not enough (module docstrings survive it), so extract exactly what ships to
    a user: non-docstring string constants.
    """
    out: list[str] = []
    for path in sorted(pkg_dir.glob("*.py")):
        name = path.name
        tree = ast.parse(path.read_text(), filename=str(path))
        skip = _docstring_nodes(tree)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Constant) or not isinstance(n.value, str):
                continue
            if id(n) in skip:
                continue
            if any(b.search(n.value) for b in COPY_BANS):
                out.append(
                    f"L3 {name}:{n.lineno} states the bot's allowance in the API's "
                    f"unit ('calls') — METERING-DIVERGENCE Rule 1: "
                    f"{n.value.strip()[:70]}"
                )
    return out


def scan_ladder(pkg_dir: Path) -> list[str]:
    """L4 (AST) + L4b (tokenize) — no hand-typed plan ladder, in code OR in a comment."""
    out: list[str] = []
    for path in sorted(pkg_dir.glob("*.py")):
        name = path.name
        src = path.read_text()
        # L4 — a dict whose string keys intersect the paid tiers and whose values are numbers.
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as e:
            raise RuntimeError(f"cannot parse {name}: {e}") from e
        for n in ast.walk(tree):
            if not isinstance(n, ast.Dict):
                continue
            keys = {k.value for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if not (keys & PAID_TIER_NAMES):
                continue
            if any(isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) for v in n.values):
                out.append(
                    f"L4 {name}:{n.lineno} hand-types the plan ladder — plan figures come from the "
                    f"server mirror, never a literal: {sorted(keys & PAID_TIER_NAMES)}"
                )
        # L4b — the same ladder hiding in a comment, which the AST above is blind to.
        with path.open("rb") as fh:
            try:
                for tok in tokenize.tokenize(fh.readline):
                    if tok.type != tokenize.COMMENT:
                        continue
                    m = LADDER_IN_COMMENT.search(tok.string)
                    if m:
                        out.append(
                            f"L4b {name}:{tok.start[0]} states a plan ladder in a COMMENT — a "
                            f"restated number goes stale with nothing able to notice: {m.group(0)!r}"
                        )
            except tokenize.TokenError:
                pass
    return out


def load_lanes() -> dict[str, str]:
    sys.path.insert(0, str(REPO / "src"))
    from algovault_bot.quota import REFUSAL_LANES  # noqa: PLC0415

    return dict(REFUSAL_LANES)


def report(f: Findings, *, corpus_label: str) -> str:
    # Print the corpus size beside every result: a scan that searched nothing must
    # never be indistinguishable from a clean one.
    print(
        f"{corpus_label}: {f.corpus_files} files, {len(f.lanes_seen)} lanes reading "
        f" the quota decision"
    )
    for name, sites in sorted(f.lanes_seen.items()):
        print(f"  lane {name}: {', '.join(sites)}")
    for bad in f.failures:
        print(f"  ✗ {bad}")
    return "FAIL" if f.failures else "PASS"


# ── self-test ────────────────────────────────────────────────────────────────
# Fixtures are built and then scanned by the REAL extractor (`scan`), never by a
# hand-written stand-in: `verification-gates.md` records a gate that passed its own
# property test because the fixture used a shape the extractor has never emitted.

_GOOD = '''
async def process_one_row(bot, db, row):
    d = evaluate_delivery(db, row.chat_id)
    if not d.allowed:
        await refuse_and_notify(db, row.chat_id, "watch", send=s, decision=d)
    return {}

def handle_scan(db, chat_id, args):
    state = get_quota_state(db, chat_id)
    if state.exhausted:
        return "You are out of alerts."
    return "ok"
'''

_SILENT_PUSH = '''
async def process_one_row(bot, db, row):
    if evaluate_delivery(db, row.chat_id).exhausted:
        return {}
    return {}
'''

_SILENT_PULL = '''
def handle_scan(db, chat_id, args):
    state = get_quota_state(db, chat_id)
    if state.exhausted:
        return None
    return "ok"
'''

_UNDECLARED = '''
async def process_webhook_batch(bot, db, row):
    if evaluate_delivery(db, row.chat_id).exhausted:
        return {}
    return {}
'''


def self_test(tmp: Path) -> bool:
    lanes = {"process_one_row": "push", "handle_scan": "pull"}
    cases: list[tuple[str, dict[str, str], dict[str, str], str]] = [
        ("both shapes correct", {"a.py": _GOOD}, lanes, "PASS"),
        ("push lane refuses silently", {"a.py": _SILENT_PUSH}, {"process_one_row": "push"}, "FAIL"),
        ("pull lane returns None", {"a.py": _SILENT_PULL}, {"handle_scan": "pull"}, "FAIL"),
        ("new lane not declared", {"a.py": _UNDECLARED}, {}, "FAIL"),
        ("declared lane does not exist", {"a.py": _GOOD}, {**lanes, "ghost_lane": "push"}, "FAIL"),
    ]
    passed = failed = 0
    for label, files, lane_map, expected in cases:
        d = tmp / label.replace(" ", "_")
        d.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (d / name).write_text(body)
        try:
            got = "FAIL" if scan(d, lane_map).failures else "PASS"
        except Exception as e:  # an assertion that RAISES is not an assertion
            got = f"CRASH({e})"
        if got == expected:
            passed += 1
            print(f"  ✓ {label}: {got}")
        else:
            failed += 1
            print(f"  ✗ {label}: expected {expected}, got {got}")

    # ── L4 / L4b: the plan ladder, in code and in comments ──────────────────
    ladder_cases = [
        ("L4  a hand-typed tier->allowance dict", 'X = {"starter": 3000, "pro": 15000}\n', 1),
        ("L4  a dict with no tier keys is fine", 'X = {"alpha": 3000, "beta": 15000}\n', 0),
        ("L4  tier keys with non-numeric values are fine", 'X = {"starter": "a", "pro": "b"}\n', 0),
        ("L4b a ladder in a COMMENT (K form)", "# real quota (3K/15K/100K)\n", 1),
        ("L4b a ladder in a COMMENT (comma form)", "# quota 3,000/15,000/100,000\n", 1),
        ("L4b ordinary prose with one figure is fine", "# about 10,000 alerts\n", 0),
    ]
    for label, body, expected in ladder_cases:
        ld = tmp / ("ladder_" + label.replace(" ", "_").replace("(", "").replace(")", "").replace("-", "").replace(">", ""))
        ld.mkdir(parents=True, exist_ok=True)
        (ld / "m.py").write_text(body)
        try:
            got = len(scan_ladder(ld))
        except Exception as e:
            got = f"CRASH({e})"
        if got == expected:
            passed += 1
            print(f"  \u2713 {label}: {got} finding(s)")
        else:
            failed += 1
            print(f"  \u2717 {label}: expected {expected}, got {got}")

    # Vacuity guard, at the CONSTRUCTION site: in --self-test WE build the corpus,
    # so an empty scan means the test built nothing — a defect in the test itself.
    empty = tmp / "empty"
    empty.mkdir(exist_ok=True)
    if scan(empty, lanes).corpus_files != 0 or not scan(empty, lanes).orphan_lanes:
        pass
    probe = scan(d, lanes)
    if probe.corpus_files == 0:
        print("  ✗ vacuity: fixture corpus is empty — the self-test verified nothing")
        failed += 1
    else:
        passed += 1
        print(f"  ✓ vacuity: fixture corpus non-empty ({probe.corpus_files} files)")

    # The seam this self-test replaces is the parse of REAL source, so no fixture
    # scenario ever executes it. Assert the bypassed artifact directly.
    try:
        real = scan(PKG, load_lanes())
        if real.corpus_files >= 3 and len(real.lanes_seen) >= 3:
            passed += 1
            print(
                f"  ✓ bypassed artifact: real package parses "
                f"({real.corpus_files} files, {len(real.lanes_seen)} lanes)"
            )
        else:
            failed += 1
            print(
                f"  ✗ bypassed artifact: real scan implausibly small "
                f"({real.corpus_files} files, {len(real.lanes_seen)} lanes)"
            )
    except Exception as e:
        failed += 1
        print(f"  ✗ bypassed artifact: real scan raised {e}")

    print(f"SELF-TEST: {'PASS' if failed == 0 else 'FAIL'} ({passed} passed, {failed} failed)")
    return failed == 0


def main() -> int:
    if "--self-test" in sys.argv:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            ok = self_test(Path(td))
        print(f"QUOTA_REFUSAL_SEAM_VERDICT={'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    try:
        lanes = load_lanes()
        f = scan(PKG, lanes)
        f.copy_violations = scan_copy(PKG)
        f.ladder_violations = scan_ladder(PKG)
    except Exception as e:
        # Input we were HANDED and could not parse is INDETERMINATE, always.
        print(f"could not evaluate: {e}")
        print("QUOTA_REFUSAL_SEAM_VERDICT=INDETERMINATE")
        return 3

    # Runtime vacuity: the world builds this corpus, but a package known to carry
    # several lanes yielding none means the EXTRACTOR broke, not that the repo is
    # clean. Never PASS on that.
    if f.corpus_files == 0 or not f.lanes_seen:
        print(
            f"scanned {f.corpus_files} files and found {len(f.lanes_seen)} lanes — "
            f"the extractor is broken, not the repo"
        )
        print("QUOTA_REFUSAL_SEAM_VERDICT=INDETERMINATE")
        return 3

    verdict = report(f, corpus_label="scanned src/algovault_bot")
    print(f"QUOTA_REFUSAL_SEAM_VERDICT={verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
