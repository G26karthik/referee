# Codebase claim map

Every load-bearing claim the journal manuscript makes, mapped to the code that implements
it, the test that pins it, and the artifact in `runs_final_codex_v2_2026-09-15` that
demonstrates it. Built from three independent audits (production-path tracing, test and
invariant coverage, dead-code and duplication) plus direct verification of every finding
that changed what the manuscript says.

Verdicts:

- **SUPPORTED** — code, test and artifact all present.
- **CODE ONLY** — implemented and tested; no artifact in this run exercises it.
- **WEAKENED** — the manuscript was changed because the code did not support the original
  claim. The change is recorded here.

Line numbers are against the `evaluated-v2` tag, commit `1c5bbdcf`.

---

## 1. Architecture

| # | Manuscript claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 1.1 | Nine phases, resumable, artifact-to-artifact | `harness/controller.py:547-556` (`_HANDLERS`), `artifacts.py:3219` (`PHASES`) | `test_controller.py`; `test_reasoning_architecture.py:484` | `projects/*/controller.json` `attempts` shows 8 phases attempted once each | SUPPORTED |
| 1.2 | Four readers run in independent contexts, one process each | `stages/audit.py:291` (`run_audit`), `audit_driver.py:882` (`run_lens`), `delegation.py` | `test_delegation_modes.py`, `test_delegation_path.py` | 32 sealed lens artifacts under `projects/*/audit/` | SUPPORTED |
| 1.3 | A reader cannot certify its own reasoning: verification fields are harness-written | `stages/audit.py:381` (`verify_evidence`), `:513-536` | `test_finding_traceability.py:120`, `:134`, `:439` | every kept finding carries a harness-written `evidence_class` | SUPPORTED |
| 1.4 | The grader is blinded and has no filesystem access | `grade_driver.py:98` (confinement), `:340` | `test_delegation_path.py:719` | `projects/*/audit/grade/*.json` | SUPPORTED |
| 1.5 | The whole-paper reader's opinion is printed and consumed by no threshold | `verdict_driver.py`, `assessment.py` | `test_guarantees.py:358` | `projects/*/reports/*.substantive.json`, 8 of 8 | SUPPORTED |
| 1.6 | Four pure report layers, each restricted by signature | `outcome.py:317`, `coverage.py:193`, `docintegrity.py:517`, `guarantees.py:677`; called from `stages/report.py:2260-2337` | `test_architecture_guarantees.py:195`, `:213`, `:253`, `:451` | every `reports/*.review.md` | SUPPORTED |
| 1.7 | The human is outside the automated boundary | architectural: no `input()` or operator prompt on the review path; `run.py` only supplies papers and gates | not directly testable | `RUN_AUTHORIZATION.json` records the only human act, which is authorising model calls | SUPPORTED |

## 2. Paper to questions

| # | Claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 2.1 | The reader supplies a quotation; the harness mints the address | `claims.py:395` (`mint`), `:321` (`resolve`) | `test_first_review.py:70`, `:76`, `:87` | `discovery/targets.json` addresses | SUPPORTED |
| 2.2 | A quotation occurring twice yields no address | `claims.mint` duplicate refusal | `test_first_review.py:87` | | SUPPORTED |
| 2.3 | A reader-written address gains nothing; the span is re-read | `claims.resolve` `span_mismatch` | `test_caption_and_evidence_ref.py:136`, `:146` | | SUPPORTED |
| 2.4 | Unverified concerns are dropped and counted | `stages/audit.py:333` (`load_reports`) | `test_finding_traceability.py:105`, `:143` | 9 dropped, reported per paper in `system_evaluation.json` | SUPPORTED |
| 2.5 | Prose claims are addressable, not only table cells | `claims.py` prose addressing | `test_first_review.py` | `acl` target `TGT-REP-P60-155` at `P6:0-155` | SUPPORTED |
| 2.6 | Question construction is a table over closed vocabulary, not a model call | `questions.py:293` (`derive`) | `test_question_centric_routing.py` | 122 questions in `system_evaluation.json` | SUPPORTED |
| 2.7 | Questions merge on the harness-minted address, not the reader's reference | `stages/discover.py` map over `source_finding_ids` | `test_question_centric_routing.py` | `sanchez24a-icml` `Q-protocol-protocol-01` lists two findings | SUPPORTED |
| 2.8 | Priority is lexicographic with cost last | `priority.py:65` (`score`), `:92` (`order`) | `test_architecture_guarantees.py:340`, `:350` | `priority_reason` on every object | SUPPORTED |
| 2.9 | A reader's `verifiable_by_experiment` boolean decides nothing | `discovery.py` carries it as `proposed_by_lens` | `test_architecture_guarantees.py:340`, `:379` | | SUPPORTED |

## 3. Deterministic paper integrity

| # | Claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 3.1 | Ten declared checks, nine computed, one named as not computed | `artifacts.py:2329` (`INTEGRITY_CHECKS`), `docintegrity.py:NOT_COMPUTED` | `test_document_integrity.py` | | SUPPORTED |
| 3.2 | Only `TABLE_ARITHMETIC` may claim anything about the paper | `docintegrity.CLAIMABLE_ABOUT_THE_PAPER` | `test_document_integrity.py:150`, `:184` | | SUPPORTED |
| 3.3 | `about` is a required field with no default | `artifacts.py:2369` | `test_document_integrity.py:131`, `test_architecture_guarantees.py:213` | every observation carries it | SUPPORTED |
| 3.4 | An observation has no severity, confidence, class or verdict field | `artifacts.DocumentObservation` | `test_document_integrity.py:131` (8 banned names), `test_architecture_guarantees.py:195` (7 decision functions) | | SUPPORTED |
| 3.5 | 85 observations, all about extraction, zero about any paper | `docintegrity.observe` | | `system_evaluation.json` `document_observations` | SUPPORTED |
| 3.6 | **The paper-facing half has never fired on a real document** | | fixtures only | `about_paper: 0` | **CODE ONLY**, stated as such in the manuscript and in the feedback closure |

## 4. Evidence routes

| # | Claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 4.1 | Eight declared routes, four implemented | `artifacts.py:2518` (`VERIFICATION_ROUTES`), `exhaustion.py:85` (`UNIMPLEMENTED_ROUTES`), `:96` (`implemented_routes`) | `test_route_exhaustion.py` | `unimplemented_routes_excluded` on every paper's `route_exhaustion` | SUPPORTED |
| 4.2 | A settling route is taken before a running one | `planner.py:63` (`_RESOLVING`), `:86` | `test_scientific_taxonomy.py:712` | `blocked_by_gate` counters | SUPPORTED |
| 4.3 | `PAPER_INTERNAL_CHECK` settles nothing and leaves the question open | `planner.py:68` (`_CITATION_ONLY`) | `test_scientific_taxonomy.py:712` | `targets_citation_verified_only: 14`, `targets_settled: 0` | SUPPORTED |
| 4.4 | A supporting target does not earn an execution while a central one is pursued | `stages/discover.py:196` (`_demote_when_a_central_target_is_being_pursued`) | **added by this pass**: `test_question_centric_routing.py`, three tests | `blocked_by_gate.outranked_by_a_central_target: 524` | SUPPORTED |
| 4.5 | The artifact chain: acquire, audit, commit, identity, capability, authorize | `repo.py:614`, `code_audit.py:753`, `repo.py:501`, `experiment_id.py:664`, `backends.py:1313` | `test_code_reproduction.py`, `test_commit_pinning.py`, `test_experiment_identity.py` | `projects/acl/runs/acl/probe_results.json` shows clone, 61 files / 15,015 lines audited, then refusal | SUPPORTED |
| 4.6 | Reconstruction eligibility never infers a missing ingredient | `reimplement.py:105` (`assess`) | `test_reimplementation_path.py` | 5 reconstruction routes attempted | SUPPORTED |
| 4.7 | Generated code cannot verify its own conformance; generator and verifier are distinct contexts | `reimplement_driver.py:411`, `artifacts.ReimplementationConformance` | `test_reimplementation_conformance.py` | 5 rejections, decision `conformance_unproven` | SUPPORTED |

## 5. Admissibility

| # | Claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 5.1 | One conjunction permits execution, with ten conditions | `backends.py:1313-1529` (`authorize`) | `test_execution_backend.py:227-318` (each condition separately, then together) | `authorization` block on every probe result | SUPPORTED |
| 5.2 | Repository execution requires container or remote session | `isolation.py:74`, enforced `backends.py:1466` | `test_isolation_boundary.py:169`, `:194`, `:201` | `backend: container` on all seven probed papers | SUPPORTED |
| 5.3 | The authority ceiling is applied in the reconciler AND at the report layer | `local_exec.py:714`; `taxonomy.py:230`; `stages/report.py:356`, `:416`, `:538`; `artifacts.py:3023` | `test_probe_synthesis.py:227-250`, `test_scientific_taxonomy.py:94`, `:104` | `cvpr` refusal names the ceiling explicitly | SUPPORTED |
| 5.4 | Three admissible sources, not two | `provenance.py:43` | `test_reimplementation_path.py:303-309` (identity across re-exports) | `provenance: reimpl_exec` on six probe results | SUPPORTED. **CLAUDE.md invariant 3 said two and was corrected by this pass.** |
| 5.5 | Unknown resource demand is insufficient, not small | `resources.py` | `test_resource_preflight.py:341`, `:475` | | SUPPORTED |
| 5.6 | Infrastructure failure never convicts | `local_exec.py`, `backends.py` | `test_execution_capability.py:71-514` (11 tests, 4 signals, OOM, ENOSPC) | | SUPPORTED |
| 5.7 | The metric parser refuses when its winning tier disagrees with itself | `local_exec.parse_metric` | `test_first_review.py:331-363` | | SUPPORTED |

## 6. Materiality and decision

| # | Claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 6.1 | Red requires an established defect AND materiality | `stages/report.py:506`, `materiality.py:333` | `test_reasoning_architecture.py:280` (swept over severity x count) | 8 green decisions | SUPPORTED |
| 6.2 | No count of findings reaches red | `stages/report.MATERIAL_SEVERITY = ()` | `test_reasoning_architecture.py:262`, `test_first_review.py:466` (50 majors stay yellow) | | SUPPORTED |
| 6.3 | Every severity mechanism only caps | `grading.py:277` (`derive`) | `test_reasoning_architecture.py:193` (whole reachable input space), `test_architecture_guarantees.py:492` | `counted_severity` on every finding | SUPPORTED |
| 6.4 | `grading.derive` admits vocabulary strings and booleans only | `grading.py:277`, 15 parameters | `test_reasoning_architecture.py:601` (signature + banned name segments) | | SUPPORTED |
| 6.5 | `planner.classify` likewise | `planner.py:141`, keyword-only, all `str`/`bool` | `test_reasoning_architecture.py` | | SUPPORTED |
| 6.6 | The four reader rows are folded over disjoint inputs | `outcome.py:182` (`finding_state`, signature is exactly `{claim_status, kept_findings}`) | `test_architecture_guarantees.py:451`, `:462` (21 dispositions x 6 sources x 4 statuses x 3 counts) | | SUPPORTED |
| 6.7 | A blocked target ends that target, never the paper | `artifacts.TargetOutcome` | `test_architecture_guarantees.py:114`, `:130` | `apt-icml` reports a blocked target and 7 findings | SUPPORTED |
| 6.8 | A colour is never a property of configuration | `stages/report.triage` | `test_first_review.py:502` (shut gate, 6 blockers, all green), `test_guarantees.py:392` | `open_because_of_this_harness: 0` | SUPPORTED |
| 6.9 | An execution that settled nothing says so in those words, with the reason | `outcome.py`, `outcome.clip` | `test_architecture_guarantees.py:77` | every `*.review.md` execution row | SUPPORTED |

## 7. Route exhaustion

| # | Claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 7.1 | Three-way partition; only discharged counts | `exhaustion.py:60-83` | `test_route_exhaustion.py` | `route_exhaustion` on every paper | SUPPORTED |
| 7.2 | Ten discharging blockers, three non-discharging | `exhaustion.DISCHARGING_BLOCKERS`, `NON_DISCHARGING_BLOCKERS` | `test_route_exhaustion.py` | | SUPPORTED |
| 7.3 | An unimplemented route is excluded from the denominator and named | `exhaustion.py:85`, `:96` | `test_route_exhaustion.py` | `unimplemented_routes_excluded` | SUPPORTED |
| 7.4 | Discharge by running requires an admissible source | `exhaustion.py`, via `provenance.admits` | `test_route_exhaustion.py` | | SUPPORTED |
| 7.5 | 12 of 12 questions exhausted, 16 of 16 routes, 0 open because of this harness | | | `reports/material_question_routes.md`, `route_exhaustion` | SUPPORTED |

## 8. Accounting

| # | Claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 8.1 | Six funnel terms, each from a different artifact | `ledger.py:191`, `evaluation.py` | `test_scientific_taxonomy.py:348`, `:359`, `:369` | `funnel` | SUPPORTED |
| 8.2 | `launched` is copied from the runner's process count | `TargetOutcome.launched` from `ProbeResult.executions` | `test_scientific_taxonomy.py:359` | `executions: 0` on all seven probe results | SUPPORTED |
| 8.3 | `probe_stage_seconds` is never called execution cost | `evaluation.py` | `test_scientific_taxonomy.py:369` (asserts `"execution_seconds" not in e`) | `probe_stage_seconds: 16.1` | SUPPORTED |
| 8.4 | Every requested paper reaches exactly one terminal state | `corpus.py:45`, assertion at `:82-85` | `test_reasoning_architecture.py:390`, `:399`, `:484` | `reports/corpus.json` | SUPPORTED |
| 8.5 | The conservation law is asserted | `evaluation.py:129` (`assert_conservation`) | module self-check only; no test in `tests/` | `conservation_violations: 0` | **CODE ONLY** for the test half; the assertion runs on every `run.py evaluate` |
| 8.6 | Coverage denominator counted off the paper | `coverage.py:193` (`surface`, signature is exactly `{doc}`) | `test_architecture_guarantees.py:253`, `:285` (degrading the doc cannot raise the rate) | `off_surface: 0` | SUPPORTED |
| 8.7 | The report is bounded by construction and says what it dropped | `stages/report.py:1728` (`_bounded`) | `test_scientific_taxonomy.py:486`, `:494` | `compaction_ratio: 0.069` | SUPPORTED. **The `_UNDROPPABLE` set was declared and unchecked; this pass added the import-time assertion that it is disjoint from `_DROPPABLE_ORDER`.** |

## 9. Guarantees

| # | Claim | Code | Test | v2 artifact | Verdict |
|---|---|---|---|---|---|
| 9.1 | Nine process guarantees, each established from a named field | `guarantees.py:PROCESS_GUARANTEES`, `ESTABLISHED_BY` | `test_guarantees.py:103`, `:127`, `:152` | `guarantees_unmet: []` | SUPPORTED |
| 9.2 | Six conditional properties are non-guarantees, not defects | `guarantees.CONDITIONAL_PROPERTIES` | `test_guarantees.py:392` (swept) | | SUPPORTED |
| 9.3 | Six scientific non-guarantees, false on every input | `guarantees.SCIENTIFIC_NON_GUARANTEES` | `test_guarantees.py:254` | printed in every review | SUPPORTED |
| 9.4 | The invariant list itself is machine-checked against the code | `tests/test_guarantees.py:610-677` parses CLAUDE.md's invariant list | | | SUPPORTED |

## 10. Evaluation claims

| # | Claim | Source | Verdict |
|---|---|---|---|
| 10.1 | 8 requested, 8 terminal reports | `reports/corpus.json`, `system_evaluation.json` | SUPPORTED |
| 10.2 | 64 proposed, 55 kept, 9 dropped | `system_evaluation.json` | SUPPORTED |
| 10.3 | 706 / 657 / 72 / 0 / 0 / 0 | `funnel` | SUPPORTED |
| 10.4 | 12 material questions, 16/16 routes | `route_exhaustion`, `material_question_routes.md` | SUPPORTED |
| 10.5 | 2,045 surface, 682 addressed, 62 examined, 0 off-surface | `review_surface_coverage` | SUPPORTED |
| 10.6 | 24 baseline proposed, 8 kept | `single_model_baseline.json` | SUPPORTED |
| 10.7 | 0 conservation violations, 0 authority-ceiling violations | `system_evaluation.json` | SUPPORTED |
| 10.8 | 1,982 tests pass, 0 fail | this machine, 2026-09-16 | SUPPORTED |
| 10.9 | The zero-launch outcome is admissibility, not configuration | `open_because_of_this_harness: 0`; every `authorization.decision` is `conformance_unproven`; `backend: container` on all seven | SUPPORTED |

---

## Claims that were WEAKENED because the code did not support them

These are the ones that matter. Each was in the six-page manuscript or in this
repository's own documentation, and each was changed rather than defended.

| Original claim | What the code says | What the journal manuscript now says |
|---|---|---|
| Routes "include paper-internal checks, static inspection of an author artifact, execution of that artifact, independently generated reconstruction, and focused validation" | `exhaustion.UNIMPLEMENTED_ROUTES` excludes artifact inspection, focused validation and literature search: none has an executor that could discharge | Table~\ref{tab:routelist} lists all eight routes with four marked not implemented, and the text says naming them is what stops an advertised capability inflating the numerator |
| "A preflight check answers, per requested file ..." (implying a pipeline gate) | `harness/controller.py` contains zero references to `preflight`; `review_papers` never calls it | Section 10.1 says preflight is a separate operator command that was run against this batch, and that a batch submitted straight to `review` would not be refused |
| Eight phases | `artifacts.PHASES` has nine, including `assess`, which supplies `investigation_open` to the planner | Section 3.1 lists nine and describes `assess` |
| "RED iff `claim_status` is VERIFIED_FAILURE" | `materiality.material_target_failure` is a second necessary condition; an admissible failed reproduction on a non-material target is GREEN | Section 8.2 states both conditions and the consequence |
| Thirteen evidence states | `taxonomy.EVIDENCE_STATES` has fourteen | Appendix A says fourteen and names the four that concern the paper |
| "only `driver` or `repo_exec` provenance may reconcile" (CLAUDE.md invariant 3) | `provenance.ADMISSIBLE_REPRODUCTION_PROVENANCE` has three members | CLAUDE.md corrected; the manuscript says three and states the extra condition on the third |
| "1571 tests" | 1,982 collected | 1,982, with 21 requiring the remote-sandbox client |
| "No WSL, no Docker, no virtualization on this host" | Docker 29.7.2 is installed; the container backend was selected on every probed paper | The manuscript uses this to make the zero-launch result interpretable: execution was not blocked for want of a place to run |

## Known gaps, recorded rather than closed

1. `harness/alignment/trial.py` is orphaned from the production pipeline, so
   `SH_ALLOW_ALIGNMENT_TRIAL` gates nothing today. Not claimed anywhere in the manuscript.
2. `evaluation.assert_conservation` has no test in `tests/`; it is covered by the module's
   own self-check, which `tests/test_self_checks.py` runs as a subprocess.
3. `harness.preflight` has no test file in `tests/`; the same self-check mechanism runs its
   18 assertions.
4. The paper-facing half of the document-integrity layer is exercised by fixtures only.
5. Red from a real published paper is proven on fixtures, not on a paper.
