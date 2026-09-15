# tools/

Development tooling. Nothing here is imported by `harness/` or by `run.py`, and nothing
here may decide anything about a paper — no severity, no verdict, no colour. These are
scripts an author runs, not part of the reviewer.

## `loop.py` — the one command to run after a change

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe tools/loop.py            # full,  ~170 s
PYTHONUTF8=1 ../.venv/Scripts/python.exe tools/loop.py --fast     # no pytest, ~40 s
PYTHONUTF8=1 ../.venv/Scripts/python.exe tools/loop.py --scratch /tmp/x
PYTHONUTF8=1 ../.venv/Scripts/python.exe tools/loop.py --self-check   # checks itself
```

`--self-check` runs no step. It sweeps the exit-code fold over the whole status space,
proves every `step_*` method is wired into `Loop.run`, and proves `--scratch` refuses each
protected tree — the same discipline every module under `harness/` carries, applied to the
script that runs them. `tests/test_self_checks.py` invokes it, because the discovery in
that file walks `harness/` and cannot see this directory.

Six steps, one row each:

| step | what it proves |
|---|---|
| `pytest tests -q` | the suite is green **and did not shrink** (a floor under the passing count, because deleting a test makes `pytest` greener) |
| module self-checks | every `python -m harness.<module>` exits 0, stage modules included — a second specification `pytest` did not reach until `tests/test_self_checks.py` existed |
| `run.py evaluate --out <scratch>` | the corpus metrics still compute, and the conservation checks inside them hold |
| `manuscript/check_claims.py` | every published number still traces to an artifact |
| `git diff --stat` | the change is the change that was described |
| write guard | nothing under `projects/`, `manuscript/` or `reports/` moved while the loop ran |

**Four statuses, because three of them are different facts.** `FAIL` is a finding about
the code. `BLOCKED` is a finding about the machine — `git` is not on PATH,
`manuscript/check_claims.py` is absent, a step timed out — and calling that a failure
sends a reader hunting a defect that is not there. `SKIP` is `--fast` declining to spend
the time, and the summary says so, so a fast loop cannot be reported as a full one.
`PASS` is `PASS`.

Exit codes: **1** if anything FAILed, **2** if nothing failed but something was BLOCKED,
**0** otherwise.

### Things worth knowing before you read a red row

- **`manuscript/check_claims.py` asserts published numbers, and legitimate work moves
  them.** The row reports its exit code and how many of its checks failed; it does not
  interpret them, and a failure there is not evidence of a bug in the loop. The usual fix
  is `run.py evaluate` (with no `--out`, so it refreshes `reports/`) and then a manuscript
  edit.
- **`evaluate` is pointed at a scratch directory.** It never writes `reports/`, because
  `check_claims.py` reads `reports/system_evaluation.json` — a loop that refreshed it
  would make step 4 a check of step 3 rather than of the manuscript. `--scratch` inside
  a protected tree is refused rather than relocated.
- **The self-check step imports `tests/test_self_checks.py`** and uses its discovery
  rather than keeping a second list of module names. Two copies of "which modules have a
  self-check" is one rule and one place for it to drift, and the copy under `tests/` is
  the one that is itself under test.

## Acceptance rule this script implements

Per the implementation plan: **loop green, test count strictly increased, no test
deleted.** A superseded assertion is kept under its original name with its history
rewritten in the docstring, never removed — so a falling test count is a failure even when
every remaining test passes, which is why step 1 carries `--min-tests`.
