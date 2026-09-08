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

L5 (GROWTH-TG-QUOTA-PARITY-W1, 2026-08-27) SUPERSEDES L3's first regex. That leg was
`\b100\b…\bcalls?\b` — a gate that hard-typed the very number it guarded, so it stopped
guarding anything the moment the cap moved 100 -> 200. It is DELETED, not updated to 200; L5
imports its magnitudes from `quota.py` instead. L3's second regex (`\bfree\s+calls?\b`)
survives untouched: it is value-independent and still correct.

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

# GROWTH-TG-QUOTA-PARITY-W1 CH3c — L5's banned magnitudes are IMPORTED, never typed.
# A gate that hand-types the number it guards is the exact bug L5 exists to retire; it must not
# commit that bug itself. Done at module scope (not inside `load_lanes`) because L5's regex is
# built at import time.
sys.path.insert(0, str(REPO / "src"))
from algovault_bot.quota import (  # noqa: E402
    FREE_TIER_DAILY_QUOTA,
    FREE_TIER_MONTHLY_QUOTA,
)
SEAM_MODULE = "quota.py"
REFUSAL_CALL = "refuse_and_notify"
#: The ledger write a `followup` lane must perform inside its delivered-send branch.
LEDGER_CALL = "record_notice"
#: What the `if` guarding a delivered send looks like. The wall's own idiom is
#: `if await send(...)` / `if ok:`; this matches the identifier, never a literal shape, so a
#: caller may name its local `sent`, `ok` or `delivered` without the gate caring.
SEND_HINT = re.compile(r"\b(send|sent|ok|delivered)\b", re.I)
#: L6 — the money-URL legs. See `scan_urls`.
URL_HOST = re.compile(r"(?:api\.)?algovault\.com/")
URL_BUILDERS = ("signup_url", "plan_signup_url")
#: The two modules allowed to CALL a URL builder: one composes the address, one puts it on a
#: button. Anywhere else is prose, which is the class L6 exists to make unwritable.
URL_BUILDER_HOMES = ("messages.py", "keyboards.py")
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
    # ...and the same collision without the literal number ("5 free calls left").
    # "API calls" stays legal: that phrase names the API ladder, which IS in calls.
    re.compile(r"\bfree\s+calls?\b", re.IGNORECASE),
)

# ── L5: a shipped string may not HAND-TYPE the free allowance ────────────────────────────────
# GROWTH-TG-QUOTA-PARITY-W1 CH3c.
#
# L5 SUPERSEDES L3's first regex, which was `\b100\b…\bcalls?\b` — a gate that hard-typed the
# very number it guarded. The moment the cap moved 100 → 200 that leg silently stopped guarding
# anything, which is the defect this leg exists to retire, so it is DELETED above rather than
# updated to 200. L3's second regex survives untouched: it is value-independent and still correct.
#
# NUMERICALLY SELF-UPDATING: the magnitudes are IMPORTED from `quota.py`, never typed here. Move
# the cap and this leg follows it — that is the whole point.
#
# 🛑 THE LOOKAROUNDS ARE LOAD-BEARING. A bare `\b(100|200)\b` is the obvious form and the next
# reader will "simplify" it back. Do not. Measured 2026-08-27 against the live package:
#   • `\b` alone FALSE-POSITIVES on the legal paid ladder — `100,000` contains a `\b`-delimited
#     `100` (a comma is a non-word char), so "Pro gives 100,000 calls a month" would FAIL a gate
#     on copy this same wave ruled correct. `(?![,\d])` is what rejects it.
#   • `\b` must nonetheless STAY: it is what keeps the FROZEN identifier `quota_100_last_fired_at`
#     out, because `_` IS a word char and no boundary exists inside it. A pure `(?<![\d,.])`
#     form flags that column name in four places.
# Both properties are required, and neither implies the other.
_ALLOWANCE_MAGNITUDES = (FREE_TIER_MONTHLY_QUOTA, FREE_TIER_DAILY_QUOTA)
ALLOWANCE_LITERAL = re.compile(
    r"(?<![\d,])\b(" + "|".join(str(n) for n in sorted(set(_ALLOWANCE_MAGNITUDES))) + r")\b(?![,\d])"
)
# The unit words that make a bare number an ALLOWANCE rather than a version, a port or a price.
# `hari`/`次`/`额度`/`每月`/`每日` were added after the original list let `referral.py`'s Indonesian
# and Chinese strings through — the id/zh copy does not use the same nouns the en copy does.
ALLOWANCE_CONTEXT = re.compile(
    r"alert|call|quota|month|day|bulan|hari|提醒|条|次|额度|每月|每日", re.IGNORECASE
)
ALLOWANCE_WINDOW = 40
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
    url_violations: list[str] = field(default_factory=list)
    lanes_seen: dict[str, list[str]] = field(default_factory=dict)
    corpus_files: int = 0

    @property
    def failures(self) -> list[str]:
        return (
            self.undeclared + self.wrong_shape + self.orphan_lanes + self.copy_violations
            + self.ladder_violations + self.url_violations
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


def _records_notice_on_send(fn: ast.AST) -> bool:
    """A `followup` lane must record the notice INSIDE the delivered-send branch.

    🛑 BRANCH-SCOPED ON PURPOSE, and this is the difference between a rule and a slogan. The
    obvious implementation is `_calls(fn, LEDGER_CALL)` — a whole-subtree walk — but that is
    satisfied by a `record_notice` sitting in a dry-run early return, or in a dead branch, or
    anywhere at all in the function. The rule the wave actually needs is the wall's own
    discipline: STAMP ONLY AFTER A DELIVERED SEND, so a blocked or rate-limited subscriber does
    not silently burn their one notice of the episode.

    So: find an `if` whose test mentions the send, and require the ledger call inside its body.
    Mirrors `_refusal_branch_returns_value`, which is the same shape one leg up.
    """
    for n in ast.walk(fn):
        if not isinstance(n, ast.If):
            continue
        if not any(
            isinstance(t, ast.Name) and SEND_HINT.search(t.id)
            or isinstance(t, ast.Attribute) and SEND_HINT.search(t.attr)
            or isinstance(t, ast.Call)
            and isinstance(t.func, ast.Name)
            and SEND_HINT.search(t.func.id)
            for t in ast.walk(n.test)
        ):
            continue
        for stmt in ast.walk(ast.Module(body=n.body, type_ignores=[])):
            if isinstance(stmt, ast.Call) and _calls(stmt, LEDGER_CALL):
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
            if shape == "followup":
                # GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R4 — the THIRD shape.
                # The user is absent AND was already refused in an earlier episode, so there is
                # no refusal to notify: the follow-up IS the notice. What makes that safe is
                # that it is RECORDED, so it cannot repeat — and the record has to happen where
                # the send succeeded, not merely somewhere in the function.
                if not _records_notice_on_send(fn):
                    f.wrong_shape.append(
                        f"L2 {where} is declared 'followup' but no {LEDGER_CALL}() sits inside "
                        f"a branch guarded by the send result — an un-recorded follow-up "
                        f"re-sends every cycle"
                    )
                if _calls(fn, REFUSAL_CALL):
                    f.wrong_shape.append(
                        f"L2 {where} is declared 'followup' but calls {REFUSAL_CALL}() — a "
                        f"follow-up must not re-fire the d0 wall notice or its stamps"
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
            for m in ALLOWANCE_LITERAL.finditer(n.value):
                lo = max(0, m.start() - ALLOWANCE_WINDOW)
                if ALLOWANCE_CONTEXT.search(n.value[lo : m.end() + ALLOWANCE_WINDOW]):
                    out.append(
                        f"L5 {name}:{n.lineno} hand-types the free allowance "
                        f"({m.group(0)}) — it comes from QuotaState, never a literal: "
                        f"{n.value.strip()[:70]}"
                    )
                    break
    return out


def _url_is_schemed(text: str, start: int) -> bool:
    r"""Is the `algovault.com/` occurrence at `start` immediately preceded by `https://`?

    🛑 NOT A LOOKBEHIND, AND THE REASON IS MEASURED. The obvious regex —
    `(?<![/\w])(api\.)?algovault\.com/` — matches the very form the rule DEMANDS. Against
    `https://api.algovault.com/signup` the lookbehind fails at the `api.` offset, so the engine
    restarts at `algovault`, whose preceding character is `.` — neither `/` nor a word char — the
    lookbehind passes, and the leg reds on correct code. A gate that fails on its own correct
    output gets weakened by the next person to hit it.

    So: walk BACK over an optional `api.` first, then require the eight preceding characters to
    be exactly `https://`. No lookbehind, no restart, no ambiguity.
    """
    if text[:start].endswith("api."):
        start -= 4
    return text[max(0, start - 8) : start] == "https://"


def scan_urls(pkg_dir: Path) -> list[str]:
    """L6 — a money CTA is a BUTTON or an explicit `https://` literal. Never prose.

    THE INCIDENT. On 2026-09-07 the operator received the live downgrade notice and its
    `Reactivate any time: api.algovault.com/signup?...` did not work: Telegram auto-links a bare
    domain as `http://`, so the tap depended on an auto-linker's guess plus a 308 hop. Eight
    other money surfaces carried the same shape, each assembling its own body and its own URL.

    Three legs, and each one closes a different door:
      (a) a scheme-less `algovault.com/` in a string literal — the defect itself
      (b) a URL BUILDER interpolated into an f-string — a URL becoming prose one step removed
      (c) a builder CALLED outside `messages.py` / `keyboards.py` — URLs are composed in one
          place and attached to buttons in one place; a call anywhere else is a new surface

    Docstrings are excluded via `_docstring_nodes`, so the prose EXPLAINING the rule — including
    this one — is not judged BY it.
    """
    out: list[str] = []
    for path in sorted(pkg_dir.glob("*.py")):
        name = path.name
        tree = ast.parse(path.read_text(), filename=str(path))
        skip = _docstring_nodes(tree)
        for n in ast.walk(tree):
            # (a) a bare domain in anything that ships to a user
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
                for m in URL_HOST.finditer(n.value):
                    if not _url_is_schemed(n.value, m.start()):
                        out.append(
                            f"L6 {name}:{n.lineno} has a scheme-less money URL — Telegram "
                            f"auto-links it as http:// and the tap can fail: "
                            f"{n.value.strip()[:70]}"
                        )
                        break
            # (b) a builder interpolated into an f-string == a URL pasted into prose
            if isinstance(n, ast.JoinedStr):
                for part in n.values:
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    if isinstance(part.value, ast.Call) and any(
                        _calls(part.value, b) for b in URL_BUILDERS
                    ):
                        out.append(
                            f"L6 {name}:{n.lineno} interpolates a signup URL into text — a "
                            f"money CTA is a plan-picker button, never a link in a sentence"
                        )
                        break
            # (c) a builder called outside its two homes
            if isinstance(n, ast.Call) and name not in URL_BUILDER_HOMES:
                for b in URL_BUILDERS:
                    if isinstance(n.func, (ast.Name, ast.Attribute)) and _calls(n, b):
                        out.append(
                            f"L6 {name}:{n.lineno} calls {b}() — URLs are built in "
                            f"{' / '.join(URL_BUILDER_HOMES)} and attached to buttons, nowhere else"
                        )
                        break
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


_FOLLOWUP_GOOD = '''
def wall_followup_pass(db, chat_id):
    d = evaluate_delivery(db, chat_id)
    if not d.allowed:
        sent = sendDM(chat_id, "x")
        if sent:
            db.record_notice(chat_id, "wall_followup_3d", "k", "sent", "c", "now")
'''

#: The exact vacuous form the branch-scoped rule exists to reject: the ledger call is IN the
#: function, so a whole-subtree `_calls()` check would pass it, but it is on the DRY-RUN path —
#: nothing was delivered, and a real send would re-fire every cycle forever.
_FOLLOWUP_UNRECORDED = '''
def wall_followup_pass(db, chat_id, dry_run):
    d = evaluate_delivery(db, chat_id)
    if not d.allowed:
        if dry_run:
            db.record_notice(chat_id, "wall_followup_3d", "k", "planned", "c", "now")
            return
        sendDM(chat_id, "x")
'''

_FOLLOWUP_REFUSES = '''
def wall_followup_pass(db, chat_id):
    d = evaluate_delivery(db, chat_id)
    if not d.allowed:
        refuse_and_notify(db, chat_id, "followup", send=None)
        sent = sendDM(chat_id, "x")
        if sent:
            db.record_notice(chat_id, "wall_followup_3d", "k", "sent", "c", "now")
'''


def self_test(tmp: Path) -> bool:
    lanes = {"process_one_row": "push", "handle_scan": "pull"}
    cases: list[tuple[str, dict[str, str], dict[str, str], str]] = [
        ("both shapes correct", {"a.py": _GOOD}, lanes, "PASS"),
        ("push lane refuses silently", {"a.py": _SILENT_PUSH}, {"process_one_row": "push"}, "FAIL"),
        ("pull lane returns None", {"a.py": _SILENT_PULL}, {"handle_scan": "pull"}, "FAIL"),
        ("new lane not declared", {"a.py": _UNDECLARED}, {}, "FAIL"),
        ("declared lane does not exist", {"a.py": _GOOD}, {**lanes, "ghost_lane": "push"}, "FAIL"),
        # V2 CH1 R4 — the THIRD shape. These run through the same `cases` harness as the other
        # two, which is why the rule is implemented inside `scan()`: a check living outside it
        # would be orphaned from the only harness that exercises it.
        ("followup records inside the send branch",
         {"a.py": _FOLLOWUP_GOOD}, {"wall_followup_pass": "followup"}, "PASS"),
        ("followup records only on the DRY-RUN path",
         {"a.py": _FOLLOWUP_UNRECORDED}, {"wall_followup_pass": "followup"}, "FAIL"),
        ("followup re-fires the d0 wall notice",
         {"a.py": _FOLLOWUP_REFUSES}, {"wall_followup_pass": "followup"}, "FAIL"),
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

    # L6 — nine fixtures. FOUR positives (the defect in each of its forms) and FIVE negatives,
    # and the negatives are the load-bearing half: the regex this leg REPLACED matched
    # `https://api.algovault.com/signup`, i.e. the exact shape the rule demands, so a leg written
    # the obvious way reds on correct code and gets weakened by the next person to hit it.
    url_cases = [
        ("L6 a bare apex domain", 'X = "algovault.com/track-record"', 1),
        ("L6 a bare api. domain", 'X = "api.algovault.com/signup?plan=starter"', 1),
        # TWO findings, not one, and the fixture asserts that on purpose: an interpolated
        # builder is BOTH prose (leg b) AND a call outside its two homes (leg c). The legs are
        # independent, so a future edit that silently collapses them into one shows up here.
        ("L6 a builder interpolated into an f-string",
         'from .messages import signup_url\nY = f"go {signup_url(\'x\')}"', 2),
        ("L6 a builder called outside its two homes",
         'from .messages import signup_url\nZ = signup_url("x")', 1),
        ("L6 an https literal is FINE", 'X = "https://api.algovault.com/signup?plan=starter"', 0),
        ("L6 an https apex literal is FINE", 'X = "https://algovault.com/track-record"', 0),
        ("L6 a url= on a button is FINE",
         'from telegram import InlineKeyboardButton\n'
         'B = InlineKeyboardButton("x", url="https://api.algovault.com/signup")', 0),
        ("L6 the domain in a COMMENT is FINE", "# see algovault.com/track-record\nX = 1", 0),
        ("L6 the domain in a DOCSTRING is FINE",
         '"""Reactivate at api.algovault.com/signup — the old broken form."""\nX = 1', 0),
    ]
    for label, body, expected in url_cases:
        ud = tmp / ("url_" + str(abs(hash(label))))
        ud.mkdir(parents=True, exist_ok=True)
        (ud / "surface.py").write_text(body)
        try:
            got = len(scan_urls(ud))
        except Exception as e:  # an assertion that RAISES is not an assertion
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
        f.url_violations = scan_urls(PKG)
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
