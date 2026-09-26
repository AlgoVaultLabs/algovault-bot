#!/usr/bin/env python3
"""OPS-BOT-DEAD-SURFACE-SWEEP-W1 CH3 R8 — the gate that finds the next dead module on day 1.

WHY THIS EXISTS (read before "simplifying" it)

`paywall.py` sat in this repo unreachable for ~80 days. Nothing went red, because nothing
was asking. It was fully built, fully unit-tested, and imported by exactly nobody — and the
tests kept passing the whole time, since a unit test calling a helper directly cannot prove
anything CALLS it (`verification-gates.md` records that exact lesson, and it is how both of
this repo's dark primitives shipped green).

CLAUDE.md's generator rule: the 4th same-class fix must build a gate making the bug class
structurally impossible. Deleting `paywall.py` retires ONE INSTANCE. This retires the CLASS
for this repo: a module under `src/algovault_bot/` that stops being reachable from a declared
entrypoint fails CI and `hooks/pre-push`, BY NAME, on the day it dies.

WHAT IT CATCHES — AND, HONESTLY, WHAT IT DOES NOT
─────────────────────────────────────────────────
Had this gate existed, `paywall.py` would have failed on 2026-09-08, the moment
GROWTH-TG-NOTICE-COMPOSER-AND-WALL-CADENCE-W1-V2 CH1 R2c emptied it of importers.

It would NOT have caught `unlock.py`. That module was reachable from `handlers.py` the entire
time it was dark; its defect was a WRITER WITH NO READER — `tg_pro_grants` and `unlock_status`
were written and nothing in the metering path ever read them. That is a different defect
(unread STATE, not an unreferenced MODULE) and this gate does not claim to cover it. Stating
the limit is the point: a gate whose scope is overstated gets trusted for work it cannot do.

ENTRYPOINTS ARE DECLARED, NEVER INFERRED
────────────────────────────────────────
Same discipline as `ops/deploy/algovault-bot.manifest`: an inferred root set produces phantom
findings on its first run, and a guard that cries wolf once is ignored forever. Every root
below carries a reason. `scripts/*.py` roots are DISCOVERED (any `algovault_bot.X` import in a
committed script makes X reachable) rather than listed, because scripts come and go and a
hand-list would rot into exactly the stale-registry defect this gate exists to find.

FUNCTION-LEVEL IMPORTS COUNT
────────────────────────────
`handlers.py` uses deferred imports heavily to break cycles, so a walker that only reads
module-scope `import` statements would report most of this package as orphaned. The AST walk
visits EVERY `Import` / `ImportFrom` node at any depth.

CONTRACT
────────
Prints exactly one terminal `ORPHAN_MODULES_VERDICT=PASS|FAIL|INDETERMINATE`. Callers gate on
the TOKEN, never the exit code. Exit 0=PASS, 1=FAIL, 3=INDETERMINATE — the token-law default
for a NEW gate (`check_test_baseline.sh` keeps 2 only because it already deployed 2, and that
divergence must not be "aligned"). INDETERMINATE means a file could not be parsed: we were
HANDED input and could not read it, which is never a silent pass.

This module imports NOTHING from `src/` at module scope, deliberately: it must be able to run
on a tree it is about to report as broken.

Self-test: `python3 scripts/check-orphan-modules.py --self-test`.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import tempfile
import warnings

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = "algovault_bot"

# ── DECLARED ENTRYPOINTS, one reason per entry ──────────────────────────────────────────────
# A module is REACHABLE if it is one of these or is imported (transitively) from one.
# Each root names the systemd unit that justifies it, read from the LIVE host on 2026-09-09 —
# `systemctl cat` on every `algovault-bot*` unit — because that is where "what actually runs" is
# declared. The units are NOT discoverable from this repo: three live in the signal-MCP repo
# (`ops/systemd/`, frozen this wave) and `algovault-bot-digest.{service,timer}` is host-only with
# no committed ancestor anywhere. That gap is recorded by OPS-BOT-DEAD-SURFACE-SWEEP-W1 and is
# NOT this gate's to fix; it is why the list below is hand-declared rather than parsed.
DECLARED_ENTRYPOINTS: dict[str, str] = {
    "__main__": "`algovault-bot.service` ExecStart — `python -m algovault_bot`",
    "bot": "the polling loop `__main__` calls; the process users talk to",
    "alert_engine": "`algovault-bot-cron.service` ExecStart — `python -m algovault_bot.alert_engine`",
    "digest": (
        "`algovault-bot-digest.service` ExecStart — `python -m algovault_bot.digest`, fired daily "
        "at 03:00 UTC by `algovault-bot-digest.timer`. Reached by NO import in `src/` or "
        "`scripts/`, which is exactly why it must be declared: the gate's first live run named it "
        "an orphan, and it is a live operator surface"
    ),
    "db": (
        "schema owner. Its migrations run at startup and its CRUD is reached through the "
        "`Database` object rather than by importing the module by name from every lane"
    ),
}

TOKEN = "ORPHAN_MODULES_VERDICT="
VERDICT_PASS = "ORPHAN_MODULES_VERDICT=PASS"
VERDICT_FAIL = "ORPHAN_MODULES_VERDICT=FAIL"
VERDICT_INDETERMINATE = "ORPHAN_MODULES_VERDICT=INDETERMINATE"


class Unparseable(Exception):
    """A file we were handed and could not read. Never a silent pass."""


def _imports_of(path: pathlib.Path, pkg: str) -> set[str]:
    """Every `pkg` submodule this file imports, at ANY depth (function-level included)."""
    try:
        with warnings.catch_warnings():
            # We are READING these files, not running them: a module's own SyntaxWarning
            # (e.g. an unescaped `\s` inside a docstring) is not this gate's finding.
            warnings.simplefilter("ignore")
            tree = ast.parse(path.read_text(), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        raise Unparseable(f"{path}: {exc}") from exc

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from .x import y` (level>0) or `from algovault_bot.x import y` (level==0)
            if node.level and node.module:
                found.add(node.module.split(".")[0])
            elif node.level and node.module is None:
                # `from . import a, b` — each alias names a submodule
                found.update(a.name.split(".")[0] for a in node.names)
            elif node.module and node.module.split(".")[0] == pkg:
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
                else:
                    # `from algovault_bot import a, b`
                    found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == pkg and len(parts) > 1:
                    found.add(parts[1])
    return found


def _script_roots(scripts_dir: pathlib.Path, pkg: str) -> dict[str, str]:
    """Modules reached from a committed `scripts/*.py`. Discovered, not hand-listed."""
    roots: dict[str, str] = {}
    if not scripts_dir.is_dir():
        return roots
    for script in sorted(scripts_dir.glob("*.py")):
        for mod in sorted(_imports_of(script, pkg)):
            roots.setdefault(mod, f"imported by scripts/{script.name}")
    return roots


def evaluate(repo: pathlib.Path, pkg: str = PKG) -> tuple[str, list[str], list[str]]:
    """Return (verdict, orphans, notes). Pure: no printing, no exit."""
    pkg_dir = repo / "src" / pkg
    notes: list[str] = []

    modules = {
        p.stem: p
        for p in sorted(pkg_dir.glob("*.py"))
        if p.stem != "__init__" and "egg-info" not in p.parts
    }
    if not modules:
        # The corpus is the WORLD's, not ours — but a package with no modules means we were
        # pointed at the wrong tree, which we could not verify. Never a silent pass.
        return VERDICT_INDETERMINATE, [], [f"no modules found under {pkg_dir}"]

    edges: dict[str, set[str]] = {}
    try:
        for name, path in modules.items():
            edges[name] = _imports_of(path, pkg) & modules.keys()
        roots = dict(DECLARED_ENTRYPOINTS)
        for mod, why in _script_roots(repo / "scripts", pkg).items():
            if mod in modules:
                roots.setdefault(mod, why)
    except Unparseable as exc:
        return VERDICT_INDETERMINATE, [], [f"unparseable: {exc}"]

    missing_roots = [r for r in DECLARED_ENTRYPOINTS if r not in modules]
    if missing_roots:
        return (
            VERDICT_INDETERMINATE,
            [],
            [f"declared entrypoint(s) absent from the package: {', '.join(missing_roots)}"],
        )

    reached: set[str] = set()
    stack = [r for r in roots if r in modules]
    while stack:
        cur = stack.pop()
        if cur in reached:
            continue
        reached.add(cur)
        stack.extend(edges.get(cur, ()) - reached)

    orphans = sorted(modules.keys() - reached)
    notes.append(f"{len(modules)} modules, {len(roots)} roots, {len(reached)} reachable")
    return (VERDICT_FAIL if orphans else VERDICT_PASS), orphans, notes


def emit(verdict: str, orphans: list[str], notes: list[str]) -> int:
    for n in notes:
        print(f"[orphan-modules] {n}")
    if verdict == VERDICT_FAIL:
        print("[orphan-modules] UNREACHABLE from any declared entrypoint:")
        for o in orphans:
            print(f"[orphan-modules]   src/{PKG}/{o}.py")
        print(
            "[orphan-modules] Either delete the module, or add a DECLARED_ENTRYPOINTS row "
            "with a reason if it is genuinely reached another way."
        )
    print(verdict)
    return {VERDICT_PASS: 0, VERDICT_FAIL: 1}.get(verdict, 3)


# ── self-test ───────────────────────────────────────────────────────────────────────────────
def _fixture(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    """Build a fixture tree that ALWAYS satisfies the real root set.

    Every module in `DECLARED_ENTRYPOINTS` is materialised (empty unless the scenario
    overrides it), so a fixture can never drift from the live declaration. Measured worth:
    widening the roots from 3 to 5 turned 4 of 9 assertions red until this was derived from
    `DECLARED_ENTRYPOINTS` instead of hand-listed — a hand-written fixture asserts against a
    root set that no longer exists, which is the vacuity shape this repo keeps paying for.
    """
    for name in DECLARED_ENTRYPOINTS:
        files.setdefault(f"src/{PKG}/{name}.py", "")
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def self_test() -> int:
    """Three scenarios, and the failure of any is reported as a VERDICT, never a traceback."""
    results: list[tuple[str, bool, str]] = []

    def check(label: str, got: object, want: object) -> None:
        # Only the terminal line may carry the token. The (g) labels quoted it whole, and so does
        # the got/want of any failed verdict-valued check — so a FAILING run printed
        # `ORPHAN_MODULES_VERDICT=PASS` above its real verdict, where an unanchored reader takes it
        # as the result. Stripped here, once, for every line (OPS-BOT-PREPUSH-NOTOKEN-ORPHAN-CI-W1);
        # `tests/test_orphan_modules.py` counts the token in this mode.
        why = f"got {got!r}, want {want!r}"
        results.append((label.replace(TOKEN, ""), got == want, why.replace(TOKEN, "")))

    roots_src = "\n".join(
        f"import {PKG}.{r}" for r in DECLARED_ENTRYPOINTS
    )  # keeps fixtures honest if the root set changes

    with tempfile.TemporaryDirectory() as td:
        # (a) a genuine orphan → FAIL, named
        r = _fixture(
            pathlib.Path(td) / "a",
            {
                f"src/{PKG}/bot.py": "from .helper import x\n",
                f"src/{PKG}/helper.py": "",
                f"src/{PKG}/ghost.py": "# reached by nobody\n",
            },
        )
        v, orph, _ = evaluate(r)
        check("(a) orphan → FAIL", v, VERDICT_FAIL)
        check("(a) orphan named", orph, ["ghost"])

        # (b) reached ONLY by a function-level import → PASS
        r = _fixture(
            pathlib.Path(td) / "b",
            {
                f"src/{PKG}/bot.py": "def go():\n    from .deferred import thing\n    return thing\n",
                f"src/{PKG}/deferred.py": "thing = 1\n",
            },
        )
        v, orph, _ = evaluate(r)
        check("(b) function-level import → PASS", v, VERDICT_PASS)
        check("(b) no orphans", orph, [])

        # (c) an unparseable file → INDETERMINATE, never a pass
        r = _fixture(
            pathlib.Path(td) / "c",
            {
                f"src/{PKG}/broken.py": "def (((\n",
            },
        )
        v, _, notes = evaluate(r)
        check("(c) unparseable → INDETERMINATE", v, VERDICT_INDETERMINATE)
        check("(c) names the file", any("broken.py" in n for n in notes), True)

        # (d) reached only by a scripts/*.py import → PASS (roots are DISCOVERED)
        r = _fixture(
            pathlib.Path(td) / "d",
            {
                f"src/{PKG}/tool.py": "",
                "scripts/do-a-thing.py": f"from {PKG}.tool import run\n",
            },
        )
        v, orph, _ = evaluate(r)
        check("(d) scripts/ root discovered → PASS", v, VERDICT_PASS)

        # (e) THE SEAM THE FIXTURES BYPASS: the real tree, with the real root set.
        # A hermetic self-test is structurally blind to exactly what it replaces, so assert
        # the live corpus too — this is the leg that catches a root that stopped existing.
        v_real, orph_real, _ = evaluate(REPO)
        check("(e) real tree parses (not INDETERMINATE)", v_real != VERDICT_INDETERMINATE, True)
        check(
            "(e) every declared root exists in the real package",
            sorted(
                r
                for r in DECLARED_ENTRYPOINTS
                if not (REPO / "src" / PKG / f"{r}.py").is_file()
            ),
            [],
        )
        # (f) a DECLARED root that no longer exists → INDETERMINATE, never a quiet PASS.
        # This is the leg that fires when someone deletes a module the units still run.
        r = pathlib.Path(td) / "f"
        for name in DECLARED_ENTRYPOINTS:
            f = r / f"src/{PKG}/{name}.py"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("")
        next(iter((r / f"src/{PKG}").glob("*.py"))).unlink()
        v, _, notes = evaluate(r)
        check("(f) missing declared root → INDETERMINATE", v, VERDICT_INDETERMINATE)
        check("(f) names the root", any("entrypoint" in n for n in notes), True)

        # (g) THE TOKEN→EXIT-CODE MAPPING ITSELF. `verification-gates.md` records a gate whose
        # self-test asserted verdict TOKENS but never the mapping, so re-coding INDETERMINATE
        # to 0 left it fully green. Found here the same way — by mutation — after (a)-(f) all
        # stayed green with `emit` hardcoded to `return 0`.
        import contextlib
        import io

        for verdict, want in ((VERDICT_PASS, 0), (VERDICT_FAIL, 1), (VERDICT_INDETERMINATE, 3)):
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                got = emit(verdict, ["ghost"] if verdict == VERDICT_FAIL else [], [])
            check(f"(g) {verdict} -> exit {want}", got, want)
            check(f"(g) {verdict} printed once", buf.getvalue().count("ORPHAN_MODULES_VERDICT="), 1)

        _ = roots_src  # documented above; not executed

    failed = [(lbl, why) for lbl, ok, why in results if not ok]
    for lbl, ok, why in results:
        print(f"[self-test] {'PASS' if ok else 'FAIL'} {lbl}" + ("" if ok else f" — {why}"))
    if not results:
        print("[self-test] corpus is empty — the test built nothing")
        print(VERDICT_INDETERMINATE)
        return 3
    print(f"[self-test] {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print(VERDICT_FAIL)
        return 1
    print(VERDICT_PASS)
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    try:
        verdict, orphans, notes = evaluate(REPO)
    except Exception as exc:  # noqa: BLE001 — a crash must still emit a verdict
        print(f"[orphan-modules] unexpected: {exc!r}")
        print(VERDICT_INDETERMINATE)
        return 3
    return emit(verdict, orphans, notes)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
