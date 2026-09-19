# Checkpoint C — after route fallback, alignment, and governed reconstruction

Run identity: `runs_postfix/_step8_checkpoint_c/` (verified, then deleted — the record
below is what matters, not a scratch directory).
Pre-fix artifacts: `projects/` — **unchanged**, `d5ea07b8c761bd778d865cb19ed397758d8315a396da9ecbebae89dfc000e4fb`
before and after, exactly as Checkpoints A and B recorded.

Per decision 6: this is the full rerun required after Step 6 (route fallback), Step 7
(alignment) and Step 8 (governed reconstruction / `reimpl_exec`) all landed. Same recipe
as A and B: the parsed document, the 32 lens files, the 38 grades and the 8 whole-paper
assessments are copied verbatim; only the deterministic layer — discovery, planning,
probing, reconciliation, reporting — is re-derived under the current code.

Gates: `repo_exec=False`, `network=True`, `install=False`, `synthesis=True`,
`diagnostic_mode=False`, `max_targets=3`. Both of Step 8's own gates
(`allow_reimplementation_driver`, `allow_reimplementation_exec`) are OFF, their defaults.

## The funnel, B to C

| term | B | C | why |
|---|---:|---:|---|
| discovered | 878 | 878 | extraction unchanged |
| structurally checkable | 764 | 764 | unchanged |
| warranting an experiment | 88 | 88 | unchanged |
| targets that launched | 0 | 0 | unchanged — see below |
| processes started | 0 | 0 | unchanged |
| admitted as evidence | 0 | 0 | unchanged |
| warranted targets naming a question | 88/88 | 88/88 | unchanged |
| comparison blocked | 6 | 6 | unchanged |

**Every disposition is unchanged from Checkpoint B's own corrected state:**
`0c06a98d7c818f6f`/`2024-icml-sapg`/`acl` → `BLOCKED_METHOD`,
`5993d35ff0996b52`/`cvpr` → `BLOCKED_SPECIFICATION`,
`apt-icml`/`iclr`/`sanchez24a-icml` → `BLOCKED_ARTIFACT`. Zero regressions across three
steps of new capability.

## Why zero movement is the expected, correct result

Steps 6-8 each add a MECHANISM, not a new claim about these eight papers:

- **Step 6** (route fallback) only changes what happens once `AUTHOR_CODE_EXECUTION`
  identity genuinely FAILS to bind against a real checkout. Whether that happens is a
  fact the corpus already had; Checkpoint B already measured its effect (the `PlanDecision`
  ordered log, the funnel double-count fix). Nothing about Step 7 or 8 changes when a
  fallback is offered — only what happens once one is.
- **Step 7** (alignment) narrows an `ambiguous` identity's CANDIDATE SET using declared
  configuration evidence (`apt-icml`'s 84 → 9, demonstrated in `docs/STEP_7_ALIGNMENT.md`).
  Narrowing candidates is not the same as RESOLVING to one — `apt-icml` remains
  `ambiguous` after narrowing, exactly as before, so its disposition (`BLOCKED_ARTIFACT`)
  is unaffected. A narrower `ambiguous` is better evidence inside the same target
  outcome, not a different outcome.
- **Step 8** (`reimpl_exec` / `ReimplementationConformance`) only executes anything when
  BOTH new gates are open. Both are off by default, and no operator action in this
  checkpoint opened them — so `attempt_reimplementation_fallback` returns `None` for
  every target it is offered to, and the corpus falls through to exactly the refusal text
  Checkpoint B already produced (now naming the CURRENT reason — see
  `docs/STEP_8_REIMPLEMENTATION.md` — rather than the pre-Step-8 "no driver exists yet").

**The one difference that is not in the funnel**: the `NO_ROUTE_AVAILABLE`/fallback text
on any target where `INDEPENDENT_RECONSTRUCTION` was offered now cites the actual closed
gate (`SH_ALLOW_REIMPLEMENTATION_DRIVER`/`SH_ALLOW_REIMPLEMENTATION_EXEC`) instead of the
retired claim that no driver for the route existed. Prose only; no disposition, count, or
evidence state moves.

## Suite

1867 passed, 0 failed, 0 skipped — `tests/test_reimplementation_conformance.py` (21 new
tests: the provenance ceiling admitting `reimpl_exec` but requiring
`ReimplementationConformance.established`, `backends.authorize`'s isolation boundary
proven directly with a real `ExecutionBackend` stub at every isolation level, and the
`stages.probe` pipeline seam end to end) plus updated tests in `tests/test_route_fallback.py`,
`tests/test_paper_disposition.py` and `tests/test_architecture_guarantees.py` (a hardcoded
2-tuple in each, updated to the new 3-tuple — the exact "one rule, many holes" pattern
`harness/provenance.py`'s own docstring warns about) plus one new module self-check
(`harness.reimplement_driver`, 493 lines, auto-discovered by `tests/test_self_checks.py`).

## What this checkpoint does NOT show

- **`reimpl_exec` has never produced a real verdict.** Every path that could reach one is
  proven with `_StubBackend` at CONTAINER/REMOTE_SESSION isolation
  (`tests/test_reimplementation_conformance.py`), never with a real container or a leased
  `modal` sandbox. `local`, the only backend this host can reach, always refuses it at
  `isolation_insufficient` — proven directly, not assumed, by
  `test_a_sealed_conformant_reconstruction_is_picked_up_and_run`.
- **The driver has never called a real reviewer.** `SH_ALLOW_REIMPLEMENTATION_DRIVER` is
  off in this run and in every test except the self-check's scripted subprocess double.
- Nothing about whether the concerns are correct. No adjudicated ground truth exists.
- Nothing about the authors'-code route working. It still binds on no paper here.
- Nothing about the isolation boundary in production. No sandbox has been leased.
