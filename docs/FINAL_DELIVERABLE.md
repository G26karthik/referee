# REFEREE v2 finalization record

## Outcome

**READY TO SEND for the bounded systems-evaluation claim.** The final evidence supports
the implemented decision boundary and route-accounting result. It does not establish
reviewer superiority, issue recall, or human usefulness.

## Final run

The authoritative run is `runs_final_codex_v2_2026-09-15`. Historical results,
the interrupted Claude pilot, the earlier Codex run, and v2 were kept in separate
directories and were not pooled.

All eight papers reached terminal reports. The system retained 55 of 64 candidate
findings and counted 0 FATAL, 0 MAJOR, and 30 MINOR findings. The conserved funnel is
706 discovered, 657 structurally checkable, 72 judged to warrant an experiment, zero
launched, zero reconciled, and zero scientifically settled. These terms are not
interchangeable.

All 12 material questions exhausted every applicable implemented route: 16 applicable,
16 attempted, 16 completed, and 16 exhausted. The terminal outcomes were eight
completed-inconclusive paper checks, three author-code identity blocks, and five
independently verified reconstruction conformance blocks. Exhaustion is not settlement
and did not establish a paper failure.

## Fresh model evaluations

The user authorization is preserved in `RUN_AUTHORIZATION.json`. Eight fresh matched
SINGLE-MODEL CODEX CRITIQUE reviews and eight fresh paper-only whole-paper assessments
were run in independent contexts. No historical findings, REFEREE output, other-paper
output, target outcome, ledger, manuscript claim, credential, or unrelated file was
included in those contexts.

The baseline proposed 24 concerns; the deterministic evidence gate kept eight and
dropped sixteen. Retained model severities were five MAJOR, two MINOR, and one NOTE.
Whole-paper assessments were three SOUND_WITH_MINOR_CONCERNS, four
SUBSTANTIAL_CONCERNS, and one CENTRAL_CLAIM_NOT_ESTABLISHED. The last category applied
to `5993d35ff0996b52` and contests the machine GREEN triage without converting model
judgement into deterministic paper failure.

The recorded controller was `codex exec v0.154.0-alpha.6.2`; provider and model were
reported as `openai` and `gpt-5.6-luna`. Reasoning effort was high for the first baseline,
low for the other seven baselines, and low for all eight whole-paper assessments.
Filesystem isolation remains `unrecorded / not provable`.

## Evaluation and ablations

The artifact-grounded ablations show nine unsupported concerns removed by quotation
verification, five serious model assertions subjected to grading/caps, three ambiguous
author-code identities refused, and five generated reconstructions refused after
independent conformance verification. These are safety and accounting effects, not a
quality comparison.

No ground-truth precision, recall, or human agreement is reported. The human study
packet is ready, but there were zero real participants. The eight-paper systems sample
is kept separate from the reserved random 200 and is not used for prevalence claims.

## Reproducibility and verification

`RUN_MANIFEST.json` preserves full SHA-256 hashes for every source PDF, rendered paper,
matched baseline prompt/response, and whole-paper prompt/response. `SHA256SUMS` covers
all regular run files except transient virtual-environment dependency caches under
`runs/*/env`. Conservation and provenance violations are both zero.

The final full test suite passed with 1961 tests, 21 Modal-only skips, and zero failures.
The final claim checker passed. The bibliography-aware halt-on-error LaTeX build passed,
with no undefined citations/references, overfull boxes, LaTeX errors, emergency stops,
or fatal errors. The manuscript is six pages. All manuscript pages, standalone Figure 2,
and the corrected five-page executive dossier were rendered and inspected.

## Deliverables

- Manuscript: `manuscript/manuscript.pdf`
- Figure 2: `manuscript/fig2_decision.pdf`
- Executive dossier: `runs_final_codex_v2_2026-09-15/reports/Executive_Review_Dossier.pdf`
- System evaluation: `runs_final_codex_v2_2026-09-15/reports/system_evaluation.md`
- Route accounting: `runs_final_codex_v2_2026-09-15/reports/material_question_routes.md`
- Ablations: `runs_final_codex_v2_2026-09-15/reports/component_ablations.md`
- Feedback closure: `docs/FINAL_FEEDBACK_CLOSURE.md`

The only material external validation still open is reviewer usefulness/human comparison.
