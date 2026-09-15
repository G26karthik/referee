# Step 6 — route fallback, and the disposition fix that preceded it

Not a checkpoint. Checkpoint C is after Steps 7 (alignment) and 8 (reimplementation) land,
per the approved order. This records what changed between Checkpoint B and Checkpoint C's
start, verified by property tests and a corpus rerun that produced no new run identity.

## Part 1 — the disposition fix

Checkpoint B flagged, and left open, that `disposition.BLOCKER_FOR_DISPOSITION` did not
classify `NO_ROUTE_AVAILABLE` or `COMPARISON_BLOCKED`: a paper whose only central target
carried either read `PASS_TO_HUMAN_CLEAN` — a central claim this review never
investigated, indistinguishable from one it checked and found to hold.

**Fixed.** A fourth blocker class, `BLOCKED_METHOD`, now covers every target disposition
that means "this review had nothing to check the central claim against" —
`ADDRESSING_BLOCKED`, `REPORTING_BLOCKED`, `NO_ROUTE_AVAILABLE`, `COMPARISON_BLOCKED` —
placed last in `_BLOCKER_PRECEDENCE` (after SPECIFICATION, ARTIFACT, RESOURCE), since a
missing method is this review's own gap and nobody else's to close. A totality self-check
in `harness/disposition.py` now asserts every value in `artifacts.BLOCKED_DISPOSITIONS` is
either classified or named in `DELIBERATELY_UNCLASSIFIED` with a reason (currently only
`AUTHORIZATION_BLOCKED` — a closed gate is this run's configuration, not an absence of
method) — so a future disposition added and never registered fails the suite instead of
shipping into a corpus rerun.

Verified against the real corpus: `0c06a98d7c818f6f` and `2024-icml-sapg` moved
`PASS_TO_HUMAN_CLEAN` → `BLOCKED_METHOD`; `acl` moved `PASS_TO_HUMAN_CONCERNS` →
`BLOCKED_METHOD`. The other five were unchanged — each already carried a stronger
SPECIFICATION or ARTIFACT blocker. Full detail and the corrected Checkpoint A/B tables are
in `docs/CHECKPOINT_A.md` and `docs/CHECKPOINT_B.md`.

Tests: `tests/test_paper_disposition.py`, extended with the regression itself
(`test_a_central_claim_this_review_has_no_method_for_is_never_clean`), the paper-clean /
review-unresolved distinction
(`test_the_distinction_between_paper_clean_and_review_unresolved_survives_the_fix`), and
the totality mechanism (`test_every_blocked_disposition_is_classified_or_named_excluded`).

## Part 2 — Step 6: route fallback

**The gap.** `discovery._routes` treated AUTHOR_CODE_EXECUTION and
INDEPENDENT_RECONSTRUCTION as mutually exclusive on `repo_available` alone. A target whose
cited cell bound to none of the repository's commands — `ambiguous`, `no_candidate`,
`unsupported` — ended `IDENTITY_BLOCKED` with nothing else considered, even on a paper
whose method section specifies enough to attempt an independent rebuild. Discovery cannot
fix this alone: whether identity binds is a fact only a real checkout establishes, and
discovery runs before one exists.

**The fix, in two pieces:**

1. `harness/discovery._routes` now offers `INDEPENDENT_RECONSTRUCTION` ALONGSIDE
   `AUTHOR_CODE_EXECUTION` when the repository exists AND the paper's own specification is
   complete (`reimplement.assess(doc).established`, now computed before discovery in
   `stages/discover.build` rather than after, so it can reach the route derivation).
   Author code stays first — invariant 15's preference is unchanged.
2. `harness/planner.plan(..., author_code_exhausted=True)` is the re-plan primitive:
   called from `stages/probe.py` once a real checkout has tried and failed to bind
   identity (`identity_failed`, true only for a DECIDED failure, never for `unmapped`), it
   excludes AUTHOR_CODE_EXECUTION from consideration for that call and lets the object's
   own routes decide what remains.

**The bookkeeping is an ordered log, not an overwrite.** A re-plan produces a second
`PlanDecision` for the same `target_id`, appended after the first
(`PlanDecision.attempt`, `PlanDecision.superseded_by`); the original stays in
`TargetSet.plans`, marked with what replaced it. Every existing reader that resolves "the
plan currently in force" via `{p.target_id: p for p in ts.plans}` already keeps the last
entry for a repeated key, so nothing needed to change there.

**What Step 6 does NOT yet do.** `INDEPENDENT_RECONSTRUCTION` has no execution mechanism
of its own before Step 8 lands `reimplement_driver.py` / `reimpl_exec` provenance — a
fallback that reaches `requires_execution=True` still cannot run, because
`synthesize_probe` produces the same paper-independent toy script regardless of route and
`admissible_if_it_succeeds` correctly refuses its `synthesized` provenance. The fallback's
`TargetOutcome` says so honestly (`_fallback_note`): "considered, and could not run either,
because no driver for that route exists yet." Step 6 lands the planning primitive and its
bookkeeping; Step 8 is what will let the same code path actually produce admissible
evidence.

**A double-counting defect found and fixed inside this step.** Once `TargetSet.plans` could
carry more than one entry per target, three counts that summed `ts.plans` directly —
`harness/ledger.py`'s `targets_warranting_experiment`, `targets_requiring_execution`, and
its `blocked_by_gate` tally — started counting a re-planned target twice. Measured on the
real corpus: `warranting_experiment` read 88 before the fallback ever fired and 89 after,
for a corpus of unchanged papers. `harness/planner.current_plans(plans)` — the last entry
for each `target_id` — is now the required read for any count over plans, used in
`harness/ledger.py` and in `tools/checkpoint_rerun.py`'s own diagnostic funnel. Pinned by
`tests/test_route_fallback.py::test_current_plans_deduplicates_a_re_planned_target` and
`::test_the_ledgers_funnel_terms_do_not_double_count_a_fallback`.

**Verified against the real corpus.** The fallback fires for real on `apt-icml`'s
`TGT-CLM-T1r0c5`: attempt 1 (AUTHOR_CODE_EXECUTION) recorded `superseded_by
INDEPENDENT_RECONSTRUCTION`, attempt 2 is the current plan, and the target's outcome reads:

> the only program available for this target was synthesized, which the provenance
> ceiling does not admit ... A fallback to INDEPENDENT_RECONSTRUCTION (attempt 2) was also
> considered, because the paper specifies enough to attempt one; it could not be run
> either, because no driver for that route exists yet.

Funnel after the fix, corpus-wide: `discovered 878 -> checkable 764 -> warranting 88 ->
launched 0` — identical to Checkpoint B's numbers except for this one target's richer
record; dispositions across all eight papers are unchanged from the disposition-fix state
above. Pre-fix `projects/` digest (`tools/corpus_digest.py`) unchanged throughout:
`d5ea07b8c761bd77...`.

Tests: `tests/test_route_fallback.py` (34 tests) — route offering, the re-plan primitive
and its three guards, `identity_failed`'s DECIDED-vs-unmapped distinction, the ordered-log
bookkeeping, `_fallback_note`'s honesty about the missing driver, and the double-count
regression above.

## Suite

1803 passed, 0 failed, 0 skipped.

## Next

Step 7 — `harness/alignment/` package: candidates (configs, entrypoint argparse surfaces,
evaluator modules), metrics from evaluator code, configuration matching (the fix for APT's
`ambiguous`), optional gated trial execution. Then Step 8 —
`harness/reimplement_driver.py` + `reimpl_exec` provenance + `ReimplementationConformance`
with the strict semantics decision 1 specifies — which is what will let the fallback this
step built actually produce admissible evidence. Then Checkpoint C, full rerun. No
manuscript edits until then.
