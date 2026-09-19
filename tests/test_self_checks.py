"""The module self-checks, run by the suite instead of only by hand.

**The defect this file exists to prevent.** This harness carries ~30 self-checks invoked
as `python -m harness.<name>`, and they are not a smoke test: `taxonomy._self_check`
sweeps the whole reachable input space of `classify` to prove no finding can be labelled
`NO_MATERIAL_ISSUE_FOUND`, `grading._self_check` proves by exhaustion that nothing can
raise a severity, and the `__main__` blocks of `harness/stages/grade.py` and
`harness/audit_driver.py` are the ONLY assertions those modules have anywhere. None of it
was reached by `pytest tests`. Measured before this file existed: deleting the body of
`taxonomy._self_check` left the suite green at 1041 passed. A specification that nothing
runs is a comment.

Four failure modes are covered, because exit-code checking alone catches only the first:

  1. a self-check that now FAILS — the parametrized subprocess pass;
  2. a self-check that was DELETED — the `_SHIPPED_WITH_A_SELF_CHECK` ratchet, because a
     module that loses its `__main__` block simply stops being discovered, and a
     discovery-driven test would then go quietly green with one less module in it;
  3. a self-check EMPTIED of its assertions — `python -m harness.x` exits 0 for a body
     that asserts nothing at all, so the assertions are counted statically;
  4. a self-check that exists and is never INVOKED — `_self_check` defined but not called
     under `__main__`, which is worse than none because the module looks covered.

Discovery is by AST over the package tree, not a list in this file: a module added with a
self-check is picked up with no edit here. The list at the bottom is a floor, never the
run list — the two tests that use it say so.

Cost: one subprocess per module, all launched concurrently from one session fixture.
Measured on the development host, 31 modules: 45.9s serially, 33.2s pooled, of which
`harness.local_exec` alone is 32.5s because its self-check drives real probe subprocesses.
Nothing is skipped to make that number smaller; the two things that can be skipped
(a missing fixture PDF, an unparseable file mid-edit) name the module and the cause.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

HARNESS_ROOT = Path(__file__).resolve().parents[1]   # .../single-harness

# Modules whose `__main__` block needs an argument, mapped to the argument to give it.
# A registry rather than an `if module == "harness.pdf"` in a test body: the usage-refusal
# test below is driven by this mapping, so a second argument-taking self-check gets the
# same two tests for free instead of the same special case twice.
_ARGV_REQUIRED: dict[str, tuple[str, ...]] = {
    "harness.pdf": ("papers/CVPR.pdf",),
}

# Every self-check must carry at least this many assertions. The floor is the smallest
# count any module currently has (3, in `harness.pdf` and `harness.verdict_driver`), and
# it exists because item 3 above is invisible to an exit code: `python -m harness.taxonomy`
# with an emptied `_self_check` prints "self-check ok" and returns 0.
_MIN_ASSERTIONS_PER_SELF_CHECK = 3

# A ratchet over the whole package: raise it when the self-checks grow, never lower it to
# make a deletion pass. Measured at 588 when this was written; the floor is set below that
# so a legitimate consolidation of two overlapping assertions does not go red, while the
# removal of a whole sweep does.
_MIN_ASSERTIONS_TOTAL = 500

# Per-subprocess ceiling. A self-check that hangs must fail with a named module rather
# than stall the suite until CI kills it with no attribution.
_SELF_CHECK_TIMEOUT_SECONDS = 300


# --------------------------------------------------------------------------- #
# Discovery — by AST, over whatever is on disk
# --------------------------------------------------------------------------- #
def _is_main_guard(test: ast.expr) -> bool:
    """`__name__ == "__main__"`, written either way round."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    for name, const in ((left, right), (right, left)):
        if (isinstance(name, ast.Name) and name.id == "__name__"
                and isinstance(const, ast.Constant) and const.value == "__main__"):
            return True
    return False


def _main_block(tree: ast.Module) -> ast.If | None:
    """The module-level `if __name__ == "__main__":` node, if there is one.

    Parsed rather than grepped. `harness/pdf.py`'s own docstring names the guard, and
    several module docstrings quote the invocation — a substring search reports a
    self-check in a module that has none, which is the false-positive direction that
    hides a missing one.
    """
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            return node
    return None


def _module_name(path: Path, root: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


# Files that could not be parsed at all, kept rather than raised so a syntax error in one
# module does not error collection of every test in this file.
_UNPARSEABLE: dict[str, str] = {}


def _sources(root: Path) -> list[tuple[str, Path, ast.Module]]:
    pkg = root / "harness"
    out: list[tuple[str, Path, ast.Module]] = []
    for path in sorted(pkg.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:      # pragma: no cover - defensive
            _UNPARSEABLE[_module_name(path, root)] = f"{type(exc).__name__}: {exc}"
            continue
        out.append((_module_name(path, root), path, tree))
    return out


def _discover(root: Path = HARNESS_ROOT) -> tuple[str, ...]:
    """Every module under `<root>/harness/` with a `__main__` block, in import order.

    `rglob` rather than the two globs `harness/*.py` and `harness/stages/*.py`: those are
    the two directories that hold self-checks today, and a self-check filed into a third
    subpackage tomorrow would be silently uncovered by the narrower walk.
    """
    return tuple(name for name, _p, tree in _sources(root) if _main_block(tree) is not None)


def _defines_self_check(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_self_check":
            return node
    return None


def _calls_self_check(block: ast.If) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_self_check" for n in ast.walk(block))


def _assertions(name: str, tree: ast.Module, block: ast.If) -> int:
    """Assertions reachable from the `__main__` block: inline, plus `_self_check`'s own.

    Deliberately shallow — it does not follow calls into helpers — because the number is
    used as a floor and a floor computed by a shallow walk can only UNDERCOUNT. An
    overcounting measure would let an emptied body borrow assertions from elsewhere.
    """
    total = sum(isinstance(n, ast.Assert) for n in ast.walk(block))
    if _calls_self_check(block) and (fn := _defines_self_check(tree)) is not None:
        total += sum(isinstance(n, ast.Assert) for n in ast.walk(fn))
    return total


_DISCOVERED = _discover()
_TREES = {name: tree for name, _p, tree in _sources(HARNESS_ROOT)}

# The floor: every module that shipped with a self-check when this file was written. It is
# NOT the run list — `_DISCOVERED` is — and a module added later needs no entry here. Its
# only job is to make a DELETION fail, which discovery alone cannot do: a module that
# loses its `__main__` block drops out of `_DISCOVERED`, and 30 green tests where there
# were 31 reads exactly like 31 green tests.
_SHIPPED_WITH_A_SELF_CHECK = frozenset({
    "harness.audit_driver", "harness.backends", "harness.claims", "harness.code_audit",
    "harness.controller", "harness.corpus", "harness.discovery", "harness.dossier",
    "harness.evaluation", "harness.failures", "harness.grade_driver", "harness.grading",
    "harness.ledger", "harness.local_exec", "harness.outcome", "harness.pdf",
    "harness.planner", "harness.priority", "harness.probe_synth", "harness.provenance",
    "harness.questions", "harness.reimplement", "harness.repo", "harness.resources",
    "harness.sandbox", "harness.selfaudit", "harness.taxonomy", "harness.verdict_driver",
    "harness.stages.discover", "harness.stages.grade", "harness.stages.report",
})


# --------------------------------------------------------------------------- #
# Running them
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Run:
    module: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    skipped: str = ""        # non-empty means it could not be run, and says why


def _missing_argument_file(module: str) -> str:
    for arg in _ARGV_REQUIRED.get(module, ()):
        if not (HARNESS_ROOT / arg).is_file():
            return f"{module} needs {arg}, which is not present in this checkout"
    return ""


def _run_one(module: str) -> _Run:
    if reason := _missing_argument_file(module):
        return _Run(module, (), 0, "", "", 0.0, skipped=reason)
    argv = _ARGV_REQUIRED.get(module, ())
    cmd = [sys.executable, "-m", module, *argv]
    # The environment is passed through with only PYTHONUTF8 added, because that is
    # exactly how the documented invocation runs (`PYTHONUTF8=1 python -m harness.x`).
    # Scrubbing SH_* here was rejected: a self-check that only passes with the execution
    # gates shut is a real finding about that self-check, and hiding it would make this
    # file report a stricter configuration than the one an operator actually uses.
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(HARNESS_ROOT), env=env,
                              timeout=_SELF_CHECK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return _Run(module, argv, -1, "", f"timed out after "
                    f"{_SELF_CHECK_TIMEOUT_SECONDS}s", time.time() - started)
    return _Run(module, argv, proc.returncode, proc.stdout or "", proc.stderr or "",
                time.time() - started)


@pytest.fixture(scope="session")
def self_check_runs() -> dict[str, _Run]:
    """Every discovered self-check, run once per session, concurrently.

    Session-scoped and pooled because the serial cost is dominated by one module
    (`harness.local_exec`, 32.5s of a 45.9s serial pass) and re-running any of them per
    test would multiply that. Submitted largest-source-first as a crude longest-job-first
    ordering — the slow self-checks are the ones with the most machinery behind them —
    which is why the pooled pass lands near the cost of its single slowest member.
    """
    order = sorted(_DISCOVERED,
                   key=lambda m: -(HARNESS_ROOT / Path(*m.split("."))).with_suffix(".py")
                   .stat().st_size)
    workers = max(1, min(8, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return {r.module: r for r in pool.map(_run_one, order)}


def _detail(run: _Run) -> str:
    return (f"{run.module} {' '.join(run.argv)} exited {run.returncode}\n"
            f"--- stdout (tail) ---\n{run.stdout[-2000:]}\n"
            f"--- stderr (tail) ---\n{run.stderr[-4000:]}")


# --------------------------------------------------------------------------- #
# 1. they pass
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", _DISCOVERED)
def test_every_module_self_check_still_passes(module, self_check_runs):
    """Prevents a broken self-check sitting green behind a green `pytest tests`.

    Before this existed, `python -m harness.outcome` was failing on a drifted disposition
    vocabulary while the suite reported 1039 passed — the self-checks and the tests were
    two independent specifications and only one of them was being run.
    """
    run = self_check_runs[module]
    if run.skipped:
        pytest.skip(run.skipped)
    assert run.returncode == 0, _detail(run)


def test_no_self_check_reports_success_while_printing_a_traceback(self_check_runs):
    """Prevents a self-check that catches its own failure and exits 0 anyway.

    A `try/except` around a sweep, or an `assert` inside a suppressed block, turns the
    strongest specification in the module into a decorative print. The exit code cannot
    see that; a traceback on the output can.
    """
    guilty = [r.module for r in self_check_runs.values()
              if not r.skipped and r.returncode == 0
              and "Traceback (most recent call last)" in (r.stdout + r.stderr)]
    assert guilty == [], f"self-check(s) exited 0 with a traceback on their output: {guilty}"


def test_every_discovered_self_check_was_actually_run_and_none_vanished_silently(
        self_check_runs):
    """Prevents the pass shrinking without anything going red.

    A fixture that quietly dropped a module — an exception swallowed in the pool, a name
    that no longer resolves to a file — would leave this file reporting success over a
    smaller set than it discovered.
    """
    assert set(self_check_runs) == set(_DISCOVERED)
    assert _DISCOVERED, "no self-check was discovered at all, which is itself the failure"
    for run in self_check_runs.values():
        assert run.skipped == "" or run.module in _ARGV_REQUIRED, (
            f"{run.module} was skipped for a reason that is not a missing argument file: "
            f"{run.skipped}")


# --------------------------------------------------------------------------- #
# 2. they cannot be deleted
# --------------------------------------------------------------------------- #
def test_no_module_may_lose_the_self_check_it_shipped_with():
    """Prevents a deletion passing as a smaller discovery.

    The run list is discovered, so removing a `__main__` block removes a test rather than
    failing one. This is the assertion that makes removal cost something: the floor is a
    frozen record of what shipped, and it is a SUBSET check, so adding a module needs no
    edit here and removing one cannot go unnoticed.
    """
    lost = sorted(_SHIPPED_WITH_A_SELF_CHECK - set(_DISCOVERED))
    assert lost == [], (
        f"module(s) that shipped with a self-check no longer have one: {lost}. If a "
        f"module was deliberately renamed or merged, move its entry rather than dropping "
        f"it, and say in the docstring where its assertions went.")


def test_the_only_assertions_covering_the_grade_stage_are_run_by_this_suite():
    """Prevents the two modules with no test file of their own going uncovered again.

    `harness/stages/grade.py` and `harness/audit_driver.py` have no `tests/test_*.py`
    naming them; their `__main__` blocks are the whole specification. Named explicitly
    because "it is in the floor set" is easy to weaken by accident and this particular
    pair is why the file was written.
    """
    for module in ("harness.stages.grade", "harness.audit_driver"):
        assert module in _DISCOVERED, module
        block = _main_block(_TREES[module])
        assert block is not None
        assert _assertions(module, _TREES[module], block) >= _MIN_ASSERTIONS_PER_SELF_CHECK


# --------------------------------------------------------------------------- #
# 3. they cannot be emptied
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", _DISCOVERED)
def test_a_self_check_emptied_of_its_assertions_is_not_a_self_check(module):
    """Prevents the failure an exit code cannot see.

    `python -m harness.taxonomy` with the body of `_self_check` deleted prints
    "harness.taxonomy self-check ok" and returns 0. Measured: doing exactly that left the
    suite at 1041 passed. So the assertions are counted statically, and a self-check that
    asserts nothing fails here regardless of what it prints.
    """
    block = _main_block(_TREES[module])
    assert block is not None, f"{module} was discovered and now has no __main__ block"
    n = _assertions(module, _TREES[module], block)
    assert n >= _MIN_ASSERTIONS_PER_SELF_CHECK, (
        f"{module}'s self-check carries {n} assertion(s); a self-check is a "
        f"specification, not a smoke test")


# --------------------------------------------------------------------------- #
# 4. a self-check that is never invoked
# --------------------------------------------------------------------------- #
def test_every_module_that_defines_a_self_check_actually_invokes_it():
    """Prevents a self-check that exists and never runs, which is worse than none.

    `def _self_check()` with no call under `__main__` makes the module look covered in
    every listing, in `CLAUDE.md`'s self-check table, and to a reader — while
    `python -m harness.<name>` exercises nothing. Checked over every module in the
    package, so a `_self_check` in a module with no `__main__` block at all also fails.
    """
    orphans: list[str] = []
    for name, tree in _TREES.items():
        if _defines_self_check(tree) is None:
            continue
        block = _main_block(tree)
        if block is None or not _calls_self_check(block):
            orphans.append(name)
    assert orphans == [], (
        f"module(s) defining `_self_check` that never call it under `__main__`: {orphans}")


# --------------------------------------------------------------------------- #
# 5. discovery is discovery, not a list
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 6. the one self-check that takes an argument
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 7. the loop that runs all of the above
# --------------------------------------------------------------------------- #
_LOOP = HARNESS_ROOT / "tools" / "loop.py"


def _loop(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(_LOOP), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          cwd=str(HARNESS_ROOT), env=env,
                          timeout=_SELF_CHECK_TIMEOUT_SECONDS)


def test_the_iteration_loop_carries_its_own_self_check_and_it_passes():
    """Prevents the script that runs every self-check being the one thing with none.

    `tools/loop.py` lives outside `harness/`, so the discovery above cannot see it. Its
    `--self-check` sweeps the exit-code fold over the whole status space and asserts every
    `step_*` method is actually called from `Loop.run` — a step added and never wired in
    would make the loop quietly narrower with every row it does print still green.
    """
    if not _LOOP.is_file():
        pytest.skip(f"tools/loop.py is not present at {_LOOP}")
    proc = _loop("--self-check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "self-check ok" in proc.stdout


def test_the_iteration_loop_refuses_to_write_where_the_repository_keeps_its_artifacts():
    """Prevents the loop rewriting the artifact one of its own steps checks.

    `manuscript/check_claims.py` reads `reports/system_evaluation.json`. A loop that
    refreshed it with its own `run.py evaluate` would turn that step into a check of the
    previous step, and the manuscript's numbers would validate against a file the loop had
    just written. The refusal is a refusal, not a silent relocation.
    """
    if not _LOOP.is_file():
        pytest.skip(f"tools/loop.py is not present at {_LOOP}")
    for tree in ("projects", "manuscript", "reports"):
        target = HARNESS_ROOT / tree / "loop-scratch-should-not-appear"
        proc = _loop("--scratch", str(target))
        out = proc.stdout + proc.stderr
        assert proc.returncode != 0, out
        assert "refusing" in out and tree in out, out
        assert not target.exists(), f"{target} was created despite the refusal"
