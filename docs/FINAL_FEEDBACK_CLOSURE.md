# Final feedback closure

This table closes the original Aakib, Vineet, and sir feedback against the current
repository state. `RESOLVED` means the requested correction is implemented and has a
current evidence record. `PARTIAL` means a mechanism or protocol exists but the requested
empirical demonstration or final writing pass does not. `OPEN` means the current final
run or manuscript does not answer the feedback. A protocol, test fixture, or planned route
is not counted as an executed scientific result.

| Feedback | Status | Current evidence and remaining gap |
|---|---|---|
| Framing and positioning are unclear | **RESOLVED** | `manuscript/manuscript.tex` now centers one evidence-closed decision-boundary contribution and reports the fresh eight-paper result without treating GREEN as verification. `docs/HARNESS_ARCHITECTURE.md` supplies the matching implementation account. |
| The old system did not actually run autonomously | **PARTIAL** | v2 has 32 sealed lens artifacts, five grades, eight paper-only assessments, eight terminal reports, and eight matched baseline contexts. Scheduler completion is demonstrated, but zero scientific processes launched. |
| Results are inconclusive | **PARTIAL** | v2 exhausted 16/16 applicable routes for 12/12 material questions with zero harness-open routes. The scientific claims remain unresolved because eight paper checks were inconclusive, three author-code identities did not bind, and five reconstructions failed conformance. |
| Questions are not answered | **PARTIAL** | Every applicable implemented route reached a defensible terminal boundary, but route exhaustion is not claim settlement. The v2 ledger records zero scientifically settled questions. |
| Targets are mostly unverified | **PARTIAL** | v2 records 706 discovered, 657 structurally checkable, 72 experiment-warranting, zero launches, and zero reconciliations. The improvement is complete route pursuit, not verification of target values. |
| No coverage measure | **RESOLVED** | v2 reports 682 of 2,045 paper-owned addressable units minted, 33%, and 62 pursued, 3%; semantic coverage remains explicitly `not_machine_detectable`. |
| Deterministic consistency checks are missing | **RESOLVED** | The bounded integrity layer is implemented in `harness/docintegrity.py`, represented separately as `DocumentObservation` in `harness/artifacts.py`, and covered by positive, negative, and authority-ceiling cases in `tests/test_document_integrity.py` and `tests/test_architecture_guarantees.py`. The final run reports zero paper observations and 85 extraction observations in `runs_final_codex_2026-09-15/reports/system_evaluation.md`; this is an executed zero, not a claim that defects were found. |
| Reviewer usefulness is unclear | **OPEN** | The fresh v2 baseline is sealed and the human packet is frozen, but there are zero real participants and no adjudicated ground truth. Usefulness and arm superiority remain unmeasured. |
| Guarantees are unclear | **RESOLVED** | Authority ceilings and leakage rules are explicit in `AGENTS.md`; admissibility and STOP invariants are exercised in `tests/test_architecture_guarantees.py`, `tests/test_guarantees.py`, `tests/test_paper_disposition.py`, and `tests/test_dynamic_early_stop.py`. `manuscript/manuscript.tex` now states the same narrow authority boundary and records unprovable isolation as such. |
| Human versus single LLM versus REFEREE comparison | **PARTIAL** | The fresh eight-paper `SINGLE-MODEL CODEX CRITIQUE` arm is sealed under v2 and uses matched paper rendering. The human protocol is frozen, but HUMAN COMPARISON remains OPEN with zero participants. |
| Authors' code route | **PARTIAL** | Three material author-code routes completed exhaustive identity alignment: ACL examined three advertised commands and found none that emitted the claimed count; two APT targets examined 162 candidates each and narrowed 84 accuracy commands to nine without obtaining a unique binding. No author-code process launched. |
| 2026 literature | **RESOLVED** | The current submission discusses FactReview, EquiReview-R, the ICML 2026 evaluation position, and the ICML 2026 reviewing policy in `manuscript/manuscript.tex`, with checked entries in `manuscript/refs.bib`. |
| PaperGym learnings | **RESOLVED** | `manuscript/manuscript.tex` identifies PaperGym as research-plan generation rather than reviewing and records the transferred discipline: criterion separation, leakage control, blind arms, held-out scoring, and stability without scalar collapse. The implementation rules remain frozen in `AGENTS.md`. |
| Writing is too AI-like | **PARTIAL** | `manuscript/manuscript.tex` was rewritten around one final-run account with short claim-focused paragraphs and no repetitive `not X but Y` construction. Stylistic quality remains a human editorial judgment, so mechanical cleanup cannot fully close it. |
| Repeated provenance tic | **RESOLVED** | The final source uses `provenance` four times where it names the authority concept and otherwise uses source, record, identity, evidence, and binding. The title now uses `Evidence-Closed Decision Boundaries`. |
| Em dashes | **RESOLVED** | The final source `manuscript/manuscript.tex` and Figure 2 source `manuscript/fig2_decision.tex` contain neither Unicode em dashes nor TeX `---` constructions. Legacy drafts are retained but are not submission sources. |
| Story is unclear | **RESOLVED** | The manuscript centers one bounded result: all 16 applicable routes for 12 material questions reached defensible terminals, while zero launches and zero settlements show that method exhaustion is not scientific proof. |
| Information dump rather than one contribution | **PARTIAL** | The rewritten six-page `manuscript/manuscript.pdf` removes stale case inventories and organizes results around the decision boundary. Final editorial compression can still improve the discussion and related-work density. |
| Figure 2 has visual problems | **RESOLVED** | `manuscript/fig2_decision.tex` places the human outside the autonomous boundary, uses a full-width fit boundary, contains no result numbers or em dashes, and says `fresh reasoning context` without claiming filesystem isolation. It rebuilt halt-on-error to `fig2_decision.pdf` and the rendered page was visually inspected without overlap, clipping, or ambiguous flow. The figure is integrated in `manuscript/manuscript.tex`. |

## Closure summary

| Status | Count |
|---|---:|
| RESOLVED | 10 |
| PARTIAL | 8 |
| OPEN | 1 |

The engineering and manuscript claims are closed at the route-accounting boundary. The
only open feedback item is reviewer usefulness: no real human comparison or adjudicated
ground truth exists. The paper may report that absence, but it may not claim superiority.
