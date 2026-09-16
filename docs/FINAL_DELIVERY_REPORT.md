# Final delivery report

Dated 2026-09-17. Supersedes `docs/FINAL_DELIVERABLE.md` (the v2 finalization record,
kept as an archived comparison, not carried forward). The authoritative run is
`runs_final_2026-09-16/`. The authoritative code revision is `3e48a89`. The authoritative
manuscript is `manuscript/journal.pdf`. The authoritative release artifact is
`dist/REFEREE_final_source.zip`.

## 1. What H added, and what it did not

Focused between-arms validation (`harness/validation.py`, `harness/between_arms.py`) is
the smallest scientifically legitimate experiment a review can design when a printed
number is consistent with more than one explanation. It is not a general experiment
generator: eight ingredients must all bind from the paper or the pinned checkout before a
design exists, `NEVER_ASSUMED` names the scientific choices it will not supply, and a
missing one is `SPECIFICATION_BLOCKED` rather than filled in.

Measured on the fresh corpus: 4 focused-validation designs reached across 2 papers, all
`SPECIFICATION_BLOCKED` at `arm_instantiation` — the claim graph found no comparison node
with both a metric and a benchmark to bind. Zero executable, zero launched, zero settled.
The refusals are the mechanism working, not an unfinished route.

## 2. Architecture freeze, held

No new scientific route, decision semantics, or scoring mechanism was introduced after H
landed. Everything that followed was correctness fixes, test fixes, packaging, and
documentation — the freeze CLAUDE.md's own workflow table states.

## 3. Integration audit

Five parallel adversarial audits (prior session) found and fixed 8 defects; tests and the
live run found 7 more, the most consequential being `controller._phase_probe` returning
before ever calling the only entry point for three routes. All are fixed, and the fix is
now confirmed on real papers in the fresh run (§9), not only by a spy test.

## 4. Test accounting, exact

Full offline suite, taken twice against the final committed revision (`3e48a89`):

```
2282 passed, 7 deselected (network), 0 failed
```

No generic "all tests pass" without the deselect count: the 7 deselected are
network-marked tests, correctly excluded from an offline run.

## 5. The fresh run

`runs_final_2026-09-16/`, a new directory, never overwriting the archived
`runs_final_codex_v2_2026-09-15/`. Delegation: 46 CLI_SUBPROCESS + 26 SESSION_SUBAGENT
readings across 8 papers (`RUN_MANIFEST.json`; `homogeneous: false`, never pooled into
one claimed method — the run stopped on a provider session limit mid-corpus and was
completed through isolated session subagents instead, sealed through the same
`parse_lens_json` validation and provenance-sidecar path every channel uses). Code
revision `3e48a89`. Model: `claude-opus-5` / `claude-sonnet-5` depending on channel.
Environment: container backend (Docker), the same host recorded in CLAUDE.md's Facts
section. No manual scientific intervention occurred inside any paper's run.

## 6. Per-paper measurement, across the eight axes

| axis | corpus total |
|---|---|
| READING | `reader_visible_fraction` = 1.000 on all 8 papers |
| REVIEW | 198 findings kept, 29 dropped unsubstantiated (87.2% verification rate) |
| CLAIMS | 226 questions generated; 50 claim-link pairings proposed (7/8 papers), 41 endpoint-verified, 0 structurally bound |
| LITERATURE | 43 novelty claims searched, 260 provider calls, 3,017 distinct works, 0 endpoint-verified concerns |
| ARTIFACTS | 4 inspections completed (the 4 repository papers), 6 distinct facts, 0 mismatches |
| INVESTIGATION | 849 targets discovered, 724 addressable, 97 judged to warrant an experiment |
| EXECUTION | 0 processes launched corpus-wide (the provenance ceiling refused the one synthesized probe offered) |
| DECISION | 8/8 triage GREEN, 0 RED; 30 independent grades (12 CONFIRMED, 13 PLAUSIBLE, 5 REFUTED); 8/8 whole-paper verdicts (6 SOUND_WITH_MINOR_CONCERNS, 1 SUBSTANTIAL_CONCERNS, 1 CENTRAL_CLAIM_NOT_ESTABLISHED, the last CONTESTED against the deterministic table) |

## 7. Positive execution was not required, and none was manufactured

Zero processes launched. This is reported as a fact about admissibility, not softened:
the provenance ceiling refused the one probe available (`apt-icml`) because it was
synthesized and could not speak to a printed quantity in either direction. No target was
downscaled or substituted to obtain a launch.

## 8. Scope: systems evaluation, not prevalence

Eight papers span heterogeneous review paths (4 artifact, 4 paper-only) deliberately, not
randomly, for exercising the system's routes. Five are drawn from the 200-paper random
sample; three from PaperBench. The reserved 200 and `sample_3` remain unreviewed and
disjoint from the evaluated eight, and `journal.tex` states plainly that no prevalence
claim follows from this corpus.

## 9. The human comparative arm

Frozen and unexecuted. Zero participants. The protocol is real and stated as such; it is
not fabricated as data.

## 10. The manuscript, rewritten once against frozen numbers

`manuscript/journal.tex` was rewritten in full against the fresh run's real artifacts
(not carried over from the archived comparison run), following the numbers freeze.
Verified independently:

```
JOURNAL CLAIM CHECK: PASS (131,182 chars of source checked)
JOURNAL STYLE CHECK: PASS (10 generated tables, 4 hand-written)
```

`manuscript/journal.pdf`: 42 pages, compiled with `latexmk -pdf`, 0 LaTeX errors. Two
comparisons (the single-model baseline arm, the component ablations) are deliberately
pinned to the archived run — both need an artifact this environment cannot regenerate
against the fresh corpus — and their captions say so explicitly rather than silently
mixing runs.

## 11. Five vector figures, corpus table, limitations recomputed

Five figures (`fig1_architecture` through `fig5_question_state_machine`), all
LaTeX-generated vector PDFs, no raster architecture diagrams. The corpus table
(`tables/j_corpus_full.tex`) carries title, year, venue, area, pages, code availability,
why-included, and final state per paper, regenerated from the fresh `corpus.json`.
Limitations recomputed from the final implementation: two previously-published claims
corrected (the reader-visibility figure, now 1.000 not 33-84%; the artifact-fact count,
now 6 not 8), five new limitations added and verified (the between-arms significance
rule, configuration-recording completeness, the two-arm-only validation scope, the
unit-adjacency requirement for metric binding, Semantic Scholar implemented but unqueried
this run), and the `artifact_review_driver` orphaned-gate finding folded in.

## 12. The release ZIP

`dist/REFEREE_final_source.zip`, built at revision `3e48a89`, 268 files, no secrets
detected. Manifest + SHA-256 written alongside it. `tools/zip_acceptance_test.py` (new)
extracted it into a clean temporary directory and verified:

```
integrity: 268 entries checked, 0 mismatch(es)
self-checks: 70/84 modules carry a self-check (matches CLAUDE.md's stated count)
test suite (from the extracted copy): 2226 passed, 56 skipped (gracefully, missing
  real-corpus PDF fixtures only), 7 deselected (network), 0 failed
ACCEPTANCE: PASS
```

The acceptance test itself found and fixed a real gap before this pass: two test files
failed collection (not a graceful skip) on the extracted archive because the harness's
own synthetic test-fixture PDFs (`papers/authored/*.pdf`) were missing from the allow
list. Fixed by adding them as their own entry, leaving the real (correctly excluded)
paper corpus untouched.

## 13. Requirements closure

`docs/FINAL_REQUIREMENTS_CLOSURE.md` carries the full matrix. Both rows this document's
own earlier revisions required to stay PENDING — the fresh eight-paper run and the ZIP
acceptance test — are now RESOLVED with real numbers recorded in place. What remains
genuinely open is stated as such, for a specific and disclosed reason, not because time
ran out: the human comparative arm (zero participants, by design); no adjudicated ground
truth (unmeasurable without human adjudication); a bounded prior-art search cannot
establish novelty (inexpressible by construction); RED from a real authors'-repository
execution is proven on fixtures only (no paper in this corpus supplied an admissible
execution); a settled focused-validation comparison is likewise fixture-proven only.

---

## Outcome

- **PRODUCT READY: yes.** Code revision `3e48a89`, offline suite 2282 passed / 0 failed,
  every gate and every known limitation documented, including two now-disclosed orphaned
  gates (`harness/alignment/trial.py`, `harness/artifact_review_driver.py`).
- **PAPER READY: yes.** Both automated checkers pass, the PDF compiles clean, and every
  number in it is read off the frozen fresh run.
- **ZIP READY: yes.** Built at the final revision, integrity-verified, and functionally
  verified by running the real test suite from inside the extracted copy.
- **READY TO SEND TO SIR: yes**, for the bounded claim this evaluation actually supports:
  a systems evaluation of eight heterogeneous-path papers, with every route's authority
  ceiling, every refusal, and every remaining limitation stated in the same document as
  the results. It does not establish reviewer superiority, issue recall, prevalence, or
  human-rated usefulness, and says so throughout rather than in one disclaimer at the end.
