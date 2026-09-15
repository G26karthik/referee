# Release refactor report

## 0. Ponytail was not run as a tool, and this is the honest account of that

**Ponytail is not installed as a Claude Code plugin or skill in this environment, and no
such tool produced any change in this report.** The evidence:

- `~/.claude/plugins/plugin-catalog-cache.json` (376 KB, the full catalogue) contains zero
  matches for "ponytail".
- `~/.claude/plugins/installed_plugins.json` lists twelve installed plugins:
  `frontend-design`, `github`, `superpowers`, `langchain-skills`, `huggingface-skills`,
  `ecc`, `caveman`, `claude-code-setup`, `typescript-lsp`, `hookify`, `pyright-lsp`,
  `pr-review-toolkit`. None is Ponytail.
- No skill named Ponytail appears in the session's available-skills list.

What exists is `ponytail-enterprise-scale.mdc` at the repository root: a Cursor-format rule
file with `alwaysApply: true`, which Claude Code does not load. Its rules were applied by
hand, and they are the rules this report is scored against:

> Stop at the first rung that holds: does this need to be built at all, does the standard
> library already do it, does a platform feature cover it, does an installed dependency
> solve it, can it be one line, and only then write the minimum code that works. No
> abstractions that were not requested. Deletion over addition. Fewest files possible.
>
> **Not lazy about**: input validation at trust boundaries, error handling that prevents
> data loss, security, and anything explicitly requested. Non-trivial logic leaves one
> runnable check behind.

The complexity review that a tool would have provided was run instead as three independent
read-only audits (production-path tracing, test and invariant coverage, dead-code and
duplication), and every finding acted on below was re-verified by reading the source before
it was touched.

## 1. Headline: this codebase was already near-minimal, and the report says so

| metric | before | after | delta |
|---|---:|---:|---:|
| `harness/` files | 65 | 65 | 0 |
| `harness/` lines | 36,413 | 36,422 | **+9** |
| `tests/` files | 52 | 52 | 0 |
| `tests/` lines | 22,517 | 22,589 | +72 |
| `tools/` lines | 1,308 | 1,308 | 0 |
| `run.py` lines | 472 | 472 | 0 |
| total source lines | 38,193 | 38,202 | **+9** |
| runtime dependencies | 5 (+2 optional) | 5 (+2 optional) | 0 |
| tests passing | 1,982 | 1,985 | +3 |
| tests failing | 0 | 0 | 0 |

Diff against the `evaluated-v2` tag: **18 files, 135 insertions, 54 deletions.**

**No files were deleted and no files were merged.** That is the finding, not a failure to
try. A whole-package dead-module scan found exactly zero unimported modules in 65 files; a
symbol scan of 780 module-level definitions found exactly five with no reference anywhere
outside their own definition, of which three are deliberate (see §4). The two genuine dead
functions came to 16 lines between them. A file-merge review of every plausible pair
concluded DO NOT MERGE in every case, because each boundary corresponds to a distinct
scientific concept the system exists to keep apart.

Net lines went **up by nine** because the deletions were small and the three integrity
assertions added below each carry the comment explaining what hole they close. Reporting a
fabricated reduction would be the same defect this system is built to catch, so the number
is published as it is.

## 2. What was deleted

| what | where | why it was safe |
|---|---|---|
| `audit_driver._reset_hint` (10 lines) | `harness/audit_driver.py` | Its own docstring recorded that its stated reason for existing was false: it claimed `grade_driver` imports it, and `grade_driver` imports `NonRetryable`, `RateLimited` and `_kill_tree` instead. Zero references anywhere, tests included. |
| `state.read_log` (6 lines) | `harness/state.py` | Read `research_log.jsonl`, which `append_log` writes and which nothing in `harness/`, `tests/`, `tools/` or `run.py` ever read back. A write-only log with a never-invoked reader. |

## 3. What was consolidated

| what | before | after |
|---|---|---|
| The UTC timestamp | The literal `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())` at **nine** sites, two of them as byte-identical private functions in different modules (`state._now` and `backends._utc`) | One public `state.now()`. `backends._utc` keeps its name, because it has 37 call sites, and forwards to the one implementation. |
| The target set's path | `root / "discovery" / "targets.json"` written **five** times: four literals in `stages/probe.py` and one inside `stages/discover.targets_path` | One `discover.targets_path_in(root)`; `targets_path(cfg, pid)` now calls it. Probe's four sites call the same function. |
| Order-preserving dedupe | Two hand-written `seen`-set loops in `repo.inspect_dependencies` | `list(dict.fromkeys(...))`, which is the standard-library form of exactly that loop. Rung 2 of the rule file. |
| The warranting-action list | `ACTIONS_REQUIRING_EXECUTION` named four plan actions, and `NECESSITY_FOR_ACTION` independently retyped the same four | The table splices the tuple in. An action added to one and not the other would have changed what `funnel.warranting_experiment` counts without changing what needs execution. |

## 4. What was deliberately NOT removed, and why

The rule file is explicit that laziness stops at integrity checks and at anything
explicitly requested. Each of these looked removable and is not.

| candidate | verdict |
|---|---|
| `stages/report.unearned_support_language` | **KEEP.** No production caller, but it is the "green may not borrow the words of evidence it does not have" guard, run by two test files over rendered report output. A linter over the renderer is a legitimate design and deleting it would silently remove the enforcement. Its status is now documented rather than implied. |
| `stages/report.material_failures` | **KEEP.** Body is `return []` regardless of its argument. It makes invariant 8 ("no model finding has rejection authority") statically checkable and is pinned by a test. A constant function that encodes a rule is not dead code. |
| `backends.register_backend` | **KEEP.** Two lines, used only by tests, and it is the intended dependency-injection seam for three backend test files. |
| `harness/alignment/trial.py` (206 lines) | **KEEP, and document.** A complete, argued, test-covered feature with its own gate that no production path calls, so `SH_ALLOW_ALIGNMENT_TRIAL` currently gates nothing. Deleting a designed capability during a release freeze is the wrong call; claiming it as a capability is worse. It is now named in CLAUDE.md's Known limitations. |
| The two severity rank tables | **DO NOT MERGE.** `grading.RANK` and `report._SEVERITY_RANK` use different numeric scales deliberately: the second ranks NOTE *below* MINOR so it can be displayed and never counted across a threshold. Merging them would either break invariant 11's cap sweep or let NOTE leak into a threshold sum. |
| The three `ADMISSIBLE_REPRODUCTION_PROVENANCE` re-exports | **DO NOT MERGE.** They are the same object, not copies, and a test asserts `is`-identity across all three sites. That is the mechanism by which invariant 3 lives in one place. |
| The four `*_driver` modules' shared confinement boilerplate | **NOT TOUCHED in this pass.** The duplication is real and the consolidation is worth doing, but the shared code constructs the isolation boundary for delegated subprocesses and each driver carries a different gate. A behaviour-preserving release refactor is not where that belongs. Recorded for a future change. |
| Any file merge | **NONE.** Every pair considered (`questions`/`discovery`, `priority`/`planner`, the four pure report layers, `provenance` into a consumer, the four drivers) would blur a distinction the system exists to maintain. The four pure layers in particular are separate modules precisely so that what each may read is a signature rather than a convention. |

## 5. What was added, and why each addition is not bloat

Four of these close holes an audit found. Three are tests.

| addition | the hole it closes |
|---|---|
| `assert not (set(_UNDROPPABLE) & set(_DROPPABLE_ORDER))` in `stages/report.py` | Invariant 19 promises five sections are never dropped from a review however long it is. `_bounded` reads `_DROPPABLE_ORDER` and has **never** read `_UNDROPPABLE`, so the promise held only because the two lists happened not to overlap. An edit adding one of the five to the droppable order would have made a review lose its outcome block with nothing catching it. The check runs at import, so the failure lands on whoever made the edit. |
| `assert triage_level in artifacts_mod.TRIAGE_LEVELS` in `stages/report.py` | `TRIAGE_LEVELS` was a declared vocabulary nothing validated against: `triage` returns string literals. A fourth colour or a typo would have reached a reader and a batch summary unremarked. |
| `assert set(BLOCKING_STATES) <= set(PREFLIGHT_STATES)` in `preflight.py` | Same shape: the vocabulary was declared, the code used literals, and the two could drift. |
| `NECESSITY_FOR_ACTION` splices `ACTIONS_REQUIRING_EXECUTION` | Removes the second copy of the four warranting actions (see §3). |
| Three tests for invariant 23's second clause | `tests/test_question_centric_routing.py`. The rule that a supporting target does not earn an execution while a central one is being pursued was implemented and counted and **not asserted anywhere**, so a change that spent an execution on a peripheral target while a central one waited would have passed the suite. The three tests cover the demotion, the case where no central target is pursued (supporting targets must stand), and the case where a central target exists but was refused (it must not suppress anything). |

## 6. Two test assertions that were true of everything

Both were found by the test audit and both are now real assertions. Neither was a failing
test; both were tests that could not fail.

1. `tests/test_architecture_guarantees.py`, empty-surface coverage. The line read
   `assert A and B if hasattr(empty, "surface_size") else True`. By operator precedence
   that is `assert (X if C else True)`, and `surface_size` is a field of `CoverageReport`
   rather than of `ReviewSurface`, so the condition was False and the statement was
   `assert True`. `surface_empty` was never checked. It is now checked directly.
2. `tests/test_autonomous_review_e2e.py`, resolved-without-execution. The line read
   `assert ts.extraction_coverage["targets_resolved_without_execution"] >= 0` under a
   comment claiming at least one question was resolved with nothing running. Every integer
   satisfies it. It now asserts what the fixture actually establishes: the term is an
   integer, reported separately from the launch count, and bounded by the object count.

## 7. Semantic equivalence

**The refactor is behaviour-preserving, and here is what that rests on.**

- **The full suite passes unchanged.** 1,982 tests passed before the refactor and 1,985
  after, with zero failures both times and no test removed, skipped or weakened. The three
  additional tests are the invariant-23 tests in §5.
- **Every change is either a deletion of something with no callers, a substitution of one
  expression for an identical one, or an assertion that passes today.** The timestamp
  consolidation substitutes one call for a literal that produced the same string from the
  same clock. The dedupe substitutes `dict.fromkeys` for the loop it is the idiom for. The
  path consolidation substitutes a function returning the same three-component path. The
  vocabulary splice produces a dictionary with the same fifteen keys and the same values,
  verified directly.
- **No typed outcome, evidence check, admissibility condition, isolation requirement, audit
  record, integrity check or evidence-source field was changed, widened or removed.** The
  three assertions added can only narrow, and all three pass on the current code.
- **No test was deleted to make anything pass.**

**What this does NOT claim.** The eight-paper evaluation was not re-run, because nothing
here changes a decision the manuscript reports. If a future change does alter a decision,
the rule is the one this report was written under: either revert it or re-run the
evaluation, and never claim equivalence on a change that has neither.

## 8. The two revisions, kept apart

| | |
|---|---|
| evaluated revision | `evaluated-v2`, commit `1c5bbdcf` |
| release revision | the commit containing this report |

The journal manuscript cites the evaluated revision for every empirical claim, because
that is the code that produced the artifacts. The release revision is the one a reader
should check out, and it differs from the evaluated one only by what is listed above.
