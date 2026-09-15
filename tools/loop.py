#!/usr/bin/env python
"""The one command to run after a change. Everything this repo can check about itself.

**The defect this exists to prevent.** The acceptance rule for a change here is "the loop
is green, the test count strictly increased, and no test was deleted" — and before this
file it was six commands a human ran from memory. What that actually cost, measured:
`pytest tests` was green at 1039 passed while `python -m harness.outcome` had been failing
for a whole workstream, because the module self-checks are a second specification that
`pytest` did not reach. A checklist nobody can run in one keystroke is a checklist that
gets run in part.

    ../.venv/Scripts/python.exe tools/loop.py [--fast] [--scratch DIR]

Six steps, each reported with its own status, its own wall time, and its own exit code:

    1. pytest tests -q                     the suite, with a floor under the count
    2. python -m harness.<module>          every module self-check, including the stages
    3. python run.py evaluate --out DIR    the corpus metrics, into a SCRATCH directory
    4. python manuscript/check_claims.py   every manuscript number against its artifact
    5. git diff --stat                     the change must be the change described
    6. the write guard                     nothing under projects/, manuscript/, reports/

**PASS / FAIL / BLOCKED / SKIP, because three of those are not the same thing.** A step
that ran and returned non-zero (FAIL) is a finding about the code. A step that could not
run at all (BLOCKED — no `git` on PATH, `manuscript/check_claims.py` absent, a timeout) is
a finding about the machine, and reporting it as a failure sends a reader looking for a
defect that is not there. SKIP is `--fast` declining to spend the time, and is printed so
that a fast loop can never be mistaken for a full one. Exit 1 on any FAIL, exit 2 when
nothing failed but something was BLOCKED, exit 0 otherwise.

**`manuscript/check_claims.py` asserts numbers that other work legitimately changes.** Its
exit code is reported as its own row with its own count of failed checks; it is not
special-cased into passing, and it is not treated as a defect in this script. When it goes
red the summary says which checks, and the answer is usually `run.py evaluate` (writing to
`reports/`, deliberately not something this script does) followed by a manuscript edit.

**This script writes nothing the repository tracks.** `run.py evaluate` is pointed at a
scratch directory, never at `reports/`, because a loop that rewrites the artifact
`check_claims.py` reads would make step 4 a check of step 3 rather than of the manuscript.
Step 6 verifies that promise by stat-walking `projects/`, `manuscript/` and `reports/`
before and after, rather than asserting it in a comment.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # .../single-harness

# The trees this script promises not to touch. `reports/` is in the list even though the
# task brief names only the first two: `reports/system_evaluation.json` is the artifact
# `manuscript/check_claims.py` reads, so silently refreshing it would turn step 4 into a
# tautology.
_PROTECTED = ("projects", "manuscript", "reports")

# The floor under the suite. Only a DROP fails; the number is the count at the time of
# writing and is meant to be raised, never lowered. Overridable with --min-tests for a
# checkout where a legitimate consolidation removed tests on purpose.
_MIN_TESTS = 1041

PASS, FAIL, BLOCKED, SKIP = "PASS", "FAIL", "BLOCKED", "SKIP"


@dataclass
class Result:
    name: str
    status: str
    seconds: float = 0.0
    detail: str = ""
    exit_code: int | None = None
    output: str = ""           # kept for the failure report, not for the table

    @property
    def row_detail(self) -> str:
        if self.exit_code is None:
            return self.detail
        return f"{self.detail}  (exit {self.exit_code})"


@dataclass
class Loop:
    scratch: Path
    fast: bool = False
    write_guard: bool = True
    min_tests: int = _MIN_TESTS
    results: list[Result] = field(default_factory=list)

    # ----------------------------------------------------------------- plumbing
    def _run(self, argv: list[str],
             timeout: int = 1800) -> tuple[int | None, str, str, float]:
        """Run one command from the repo root, as (code, stdout, stderr, seconds).

        `None` for the code means it could not run at all, and the reason is in stderr.
        The two streams are kept apart rather than concatenated: `git diff --stat` prints
        its summary on stdout and a CRLF warning on stderr, and a step that read the last
        line of the merged text reported the warning as the diff.
        """
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        started = time.time()
        try:
            proc = subprocess.run(argv, cwd=str(ROOT), env=env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except FileNotFoundError as exc:
            return None, "", f"cannot run {argv[0]!r}: {exc}", time.time() - started
        except subprocess.TimeoutExpired:
            return None, "", f"timed out after {timeout}s", time.time() - started
        return proc.returncode, proc.stdout or "", proc.stderr or "", time.time() - started

    def _record(self, r: Result) -> Result:
        self.results.append(r)
        print(f"  {r.status:<8} {r.seconds:6.1f}s  {r.name} — {r.row_detail}",
              file=sys.stderr, flush=True)
        return r

    # ----------------------------------------------------------------- step 1
    def step_pytest(self) -> Result:
        name = "pytest tests -q"
        if self.fast:
            return self._record(Result(name, SKIP, detail="--fast"))
        code, out, err, secs = self._run([sys.executable, "-m", "pytest", "tests", "-q"])
        if code is None:
            return self._record(Result(name, BLOCKED, secs, err))
        out += err
        counts = {k: int(v) for v, k in
                  re.findall(r"(\d+) (passed|failed|error|errors|skipped|xfailed)", out)}
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        detail = (f"{passed} passed, {failed} failed, "
                  f"{counts.get('skipped', 0)} skipped")
        if code != 0 or failed:
            return self._record(Result(name, FAIL, secs, detail, code, out))
        # A green suite that shrank is the failure mode the acceptance rule names, and it
        # is invisible in the exit code: deleting a test makes `pytest` greener, not redder.
        if passed < self.min_tests:
            return self._record(Result(
                name, FAIL, secs, f"{detail} — below the floor of {self.min_tests}", code,
                out))
        return self._record(Result(name, PASS, secs, detail, code, out))

    # ----------------------------------------------------------------- step 2
    def step_self_checks(self) -> Result:
        """Every `python -m harness.<module>` self-check, run through the suite's own
        discovery.

        `tests/test_self_checks.py` is imported rather than reimplemented here. Two copies
        of "which modules have a self-check, and which one needs a file argument" is one
        rule and one place for it to drift — and the copy in `tests/` is the one that is
        itself under test, so it is the copy that must be authoritative.
        """
        name = "module self-checks"
        spec_path = ROOT / "tests" / "test_self_checks.py"
        try:
            spec = importlib.util.spec_from_file_location("_loop_self_checks", spec_path)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            # Registered BEFORE exec_module: `dataclasses` resolves a field annotation by
            # looking its own module up in `sys.modules`, so a frozen dataclass in a
            # module that is not there yet dies with `'NoneType' has no attribute
            # '__dict__'` — an error that says nothing about the real cause.
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        except Exception as exc:      # noqa: BLE001 - any import problem blocks the step
            return self._record(Result(
                name, BLOCKED, 0.0,
                f"cannot import {spec_path.relative_to(ROOT)}: {type(exc).__name__}: {exc}"))

        modules = list(mod._discover())
        if not modules:
            return self._record(Result(name, BLOCKED, 0.0,
                                       "no module with a self-check was discovered"))
        started = time.time()
        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(8, os.cpu_count() or 1))
        order = sorted(modules, key=lambda m: -(ROOT / Path(*m.split("."))
                                                ).with_suffix(".py").stat().st_size)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            runs = list(pool.map(mod._run_one, order))
        secs = time.time() - started

        stages = sum(1 for m in modules if m.startswith("harness.stages."))
        skipped = [r for r in runs if r.skipped]
        bad = [r for r in runs if not r.skipped and r.returncode != 0]
        detail = (f"{len(runs) - len(bad) - len(skipped)} of {len(runs)} ok "
                  f"({stages} stage module(s), {len(skipped)} not runnable here)")
        if bad:
            report = "\n\n".join(
                f"### {r.module} exited {r.returncode}\n{(r.stdout + r.stderr)[-3000:]}"
                for r in bad)
            return self._record(Result(
                name, FAIL, secs,
                f"{detail} — failed: {', '.join(r.module for r in bad)}",
                output=report))
        if skipped and not bad:
            detail += f" — skipped: {'; '.join(r.skipped for r in skipped)}"
        return self._record(Result(name, PASS, secs, detail, 0))

    # ----------------------------------------------------------------- step 3
    def step_evaluate(self) -> Result:
        name = "run.py evaluate"
        code, out, err, secs = self._run(
            [sys.executable, "run.py", "evaluate", "--out", str(self.scratch)])
        if code is None:
            return self._record(Result(name, BLOCKED, secs, err))
        measured = re.search(r"(\d+) of (\d+) requested paper", out)
        detail = (f"{measured.group(1)} of {measured.group(2)} papers measured"
                  if measured else "ran")
        detail += f" -> {self.scratch}"
        status = PASS if code == 0 else FAIL
        return self._record(Result(name, status, secs, detail, code, out + err))

    # ----------------------------------------------------------------- step 4
    def step_check_claims(self) -> Result:
        name = "manuscript/check_claims.py"
        script = ROOT / "manuscript" / "check_claims.py"
        if not script.is_file():
            return self._record(Result(name, BLOCKED, 0.0, "script is not present"))
        code, out, err, secs = self._run([sys.executable, "manuscript/check_claims.py"])
        if code is None:
            return self._record(Result(name, BLOCKED, secs, err))
        out += err
        tally = re.search(r"(\d+)/(\d+) checks passed", out)
        detail = f"{tally.group(0)}" if tally else "ran"
        if code != 0:
            # Reported, never re-interpreted. This script asserts published numbers, and a
            # change that legitimately moves one makes it red until the manuscript catches
            # up. The row says which checks; it does not say whose fault they are.
            failed = re.findall(r"^  - (.+)$", out, flags=re.M)
            detail += f" — {len(failed)} failing check(s)"
            return self._record(Result(name, FAIL, secs, detail, code, out))
        return self._record(Result(name, PASS, secs, detail, code, out))

    # ----------------------------------------------------------------- step 5
    def step_git_diff(self) -> Result:
        name = "git diff --stat"
        code, out, err, secs = self._run(["git", "diff", "--stat"])
        if code is None:
            return self._record(Result(name, BLOCKED, secs, err))
        tail = [ln for ln in out.strip().splitlines() if ln.strip()]
        summary = tail[-1].strip() if tail else "no unstaged change"
        status = PASS if code == 0 else FAIL
        return self._record(Result(name, status, secs, summary, code, out + err))

    # ----------------------------------------------------------------- step 6
    def step_write_guard(self, before: dict[str, tuple[int, int]] | None) -> Result:
        name = "write guard"
        if not self.write_guard or before is None:
            return self._record(Result(name, SKIP, detail="--no-write-guard"))
        started = time.time()
        after = _snapshot()
        secs = time.time() - started
        changed = sorted(set(before) ^ set(after)) + \
            sorted(k for k in set(before) & set(after) if before[k] != after[k])
        trees = ", ".join(f"{t}/" for t in _PROTECTED)
        if changed:
            return self._record(Result(
                name, FAIL, secs, f"{len(changed)} file(s) under {trees} changed",
                output="\n".join(changed[:50])))
        return self._record(Result(name, PASS, secs, f"{trees} unchanged", 0))

    # ----------------------------------------------------------------- driver
    def run(self) -> int:
        print(f"single-harness loop\n  root:        {ROOT}\n"
              f"  interpreter: {sys.executable}\n  scratch:     {self.scratch}\n"
              f"  mode:        {'fast' if self.fast else 'full'}\n",
              file=sys.stderr, flush=True)
        before = _snapshot() if self.write_guard else None
        self.step_pytest()
        self.step_self_checks()
        self.step_evaluate()
        self.step_check_claims()
        self.step_git_diff()
        self.step_write_guard(before)
        return self.report()

    def report(self) -> int:
        width = max(len(r.name) for r in self.results)
        print("\n" + "-" * (width + 34))
        print(f"{'step'.ljust(width)}  status    seconds  detail")
        print("-" * (width + 34))
        for r in self.results:
            print(f"{r.name.ljust(width)}  {r.status:<8} {r.seconds:7.1f}  {r.row_detail}")
        print("-" * (width + 34))

        failed = [r for r in self.results if r.status == FAIL]
        blocked = [r for r in self.results if r.status == BLOCKED]
        skipped = [r for r in self.results if r.status == SKIP]
        for r in failed:
            if r.output:
                print(f"\n===== {r.name} =====\n{r.output[-6000:]}")
        print(f"\n{len(self.results) - len(failed) - len(blocked) - len(skipped)} passed, "
              f"{len(failed)} failed, {len(blocked)} blocked, {len(skipped)} skipped")
        if skipped:
            print("a loop with a skipped step is not a full loop: "
                  + ", ".join(r.name for r in skipped))
        return _exit_code(self.results)


def _exit_code(results: list[Result]) -> int:
    """The exit code, as a pure fold over the statuses so it can be swept exhaustively.

    2 is distinct from 1 on purpose. "It broke" and "it could not be checked here" send a
    reader to two different places, and one exit code for both sends them to the wrong one
    half the time. A SKIP never changes the code — `--fast` is the operator's own choice —
    but the summary always names what was skipped, so a green fast loop cannot be read as
    a green full one.
    """
    statuses = {r.status for r in results}
    if FAIL in statuses:
        return 1
    if BLOCKED in statuses:
        return 2
    return 0


def _snapshot() -> dict[str, tuple[int, int]]:
    """(size, mtime_ns) for every file under the protected trees.

    Cheap enough to run twice (measured 1.3 s per pass over 9,085 files on the development
    host) and it is the only form of this check that cannot be talked out of: an assertion
    that the script writes nowhere is a claim, a before/after stat walk is a measurement.
    """
    out: dict[str, tuple[int, int]] = {}
    for tree in _PROTECTED:
        base = ROOT / tree
        if not base.is_dir():
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                p = Path(root) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[str(p.relative_to(ROOT))] = (st.st_size, st.st_mtime_ns)
    return out


def _scratch_dir(raw: str | None) -> Path:
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        path = Path(tempfile.gettempdir()).resolve() / "single-harness-loop"
    # Refusing rather than rewriting: a scratch directory inside a protected tree would
    # make step 6 fail on this script's own output, and quietly relocating the operator's
    # explicit path would put the artifacts somewhere they did not ask for.
    for tree in _PROTECTED:
        base = (ROOT / tree).resolve()
        if path == base or base in path.parents:
            raise SystemExit(
                f"refusing --scratch {path}: it is inside {tree}/, which this loop must "
                f"not write to. Pick a path outside {', '.join(_PROTECTED)}.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _self_check() -> None:
    """The specification of this script, in the form the rest of the repo uses.

    `python tools/loop.py --self-check` runs nothing and checks nothing about the harness;
    it checks THIS FILE — the exit-code fold by exhaustive sweep, the scratch refusal
    against every protected tree, and that every `step_*` method is actually wired into
    `run`. The last one is the defect worth guarding: a step added and never called would
    make the loop quietly narrower while every row it does print stays green.
    """
    import inspect
    import itertools

    # --- the exit code is a fold, swept over the whole status space -------------------
    every = (PASS, FAIL, BLOCKED, SKIP)
    for combo in itertools.product(every, repeat=3):
        rs = [Result(f"s{i}", s) for i, s in enumerate(combo)]
        got = _exit_code(rs)
        if FAIL in combo:
            assert got == 1, combo
        elif BLOCKED in combo:
            assert got == 2, combo
        else:
            assert got == 0, combo
    assert _exit_code([]) == 0, "an empty loop is not a failing loop"
    # A skip alone never turns the loop red; it is the operator's own choice.
    assert _exit_code([Result("a", PASS), Result("b", SKIP)]) == 0

    # --- every step is wired in --------------------------------------------------------
    steps = {n for n, _ in inspect.getmembers(Loop, inspect.isfunction)
             if n.startswith("step_")}
    body = inspect.getsource(Loop.run)
    unwired = sorted(s for s in steps if f"self.{s}(" not in body)
    assert not unwired, f"step method(s) never called from Loop.run: {unwired}"
    assert len(steps) >= 6, f"only {len(steps)} step(s) found: {sorted(steps)}"

    # --- the scratch directory may not be inside a tree we promise not to write -------
    for tree in _PROTECTED:
        for candidate in (str(ROOT / tree), str(ROOT / tree / "deep" / "nested")):
            try:
                _scratch_dir(candidate)
            except SystemExit as exc:
                assert tree in str(exc), (tree, str(exc))
            else:                                     # pragma: no cover - the failure
                raise AssertionError(f"--scratch {candidate} was accepted")
    outside = _scratch_dir(None)
    assert outside.is_dir() and ROOT not in outside.parents

    # --- the two vocabularies do not overlap and the table can render every one -------
    assert len({PASS, FAIL, BLOCKED, SKIP}) == 4
    assert Result("x", FAIL, 1.0, "d", 3).row_detail == "d  (exit 3)"
    assert Result("x", SKIP, 0.0, "--fast").row_detail == "--fast", (
        "a step that never ran must not print an exit code it does not have")

    print("tools.loop self-check ok")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tools/loop.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", action="store_true",
                    help="skip the step(s) marked slow — currently the full pytest run "
                         "(~130 s on the development host). Every other step still runs, "
                         "and the summary says the loop was not full.")
    ap.add_argument("--scratch", metavar="DIR",
                    help="where `run.py evaluate` writes (default: a directory under the "
                         "system temp dir). Refused if it is inside projects/, "
                         "manuscript/ or reports/.")
    ap.add_argument("--no-write-guard", action="store_true",
                    help="skip the before/after stat walk of the protected trees "
                         "(saves ~2.6 s).")
    ap.add_argument("--min-tests", type=int, default=_MIN_TESTS,
                    help=f"floor under the passing test count (default {_MIN_TESTS}). "
                         f"Raise it as the suite grows.")
    ap.add_argument("--self-check", action="store_true",
                    help="check this script's own invariants and exit; runs no step.")
    args = ap.parse_args(argv)
    if args.self_check:
        _self_check()
        return 0
    loop = Loop(scratch=_scratch_dir(args.scratch), fast=args.fast,
                write_guard=not args.no_write_guard, min_tests=args.min_tests)
    return loop.run()


if __name__ == "__main__":
    raise SystemExit(main())
