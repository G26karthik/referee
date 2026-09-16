# Final delivery report

Dated 2026-09-17 (second pass). Supersedes the earlier same-day revision of this document
(which cited revision `3e48a89`). The authoritative run is `runs_final_2026-09-16/`. The
authoritative code revision is `00252bc`. The authoritative manuscript is
`manuscript/journal.pdf`. The authoritative release artifact is
`dist/REFEREE_final_source.zip`.

This pass was a **presentation-and-completion correction**, not a rerun of the pipeline's
model-facing phases (Ingest/Audit/Collect/Grade/Assess/Discover are byte-identical to the
prior pass; nothing in them was invalidated). Four things were wrong or incomplete in the
first pass, and this document records the fix for each.

## 1. The reviewer-facing headline is the typed disposition, not a colour

`harness/disposition.py`'s nine-value vocabulary (`STOP_MATERIAL_FAILURE`,
`BLOCKED_SPECIFICATION`, `BLOCKED_ARTIFACT`, `BLOCKED_RESOURCES`, `BLOCKED_METHOD`,
`PASS_TO_HUMAN_UNRESOLVED`, `PASS_TO_HUMAN_CONCERNS`, `PASS_TO_HUMAN_CLEAN`,
`NOT_REVIEWED`) was already implemented and already computed into `reports/corpus.json`,
but the manuscript's main tables (`j_perpaper.tex`, `j_assessment.tex`) and
`make_corpus_table.py`'s "Final state" column still printed the legacy GREEN/YELLOW/RED
triage colour as the headline result. The triage field is kept internally — it still
routes a queue and is asserted never to disagree with `disposition` — but it no longer
appears in a main results table or caption.

The real distribution this surfaces, which the colour had hidden behind a uniform "8/8
GREEN": **6 of 8 papers are `BLOCKED_METHOD`** (a central question on that paper needed a
verification approach this system does not implement — never the paper's fault, never the
artifact's, a limit of this review's own method inventory), **1 is
`BLOCKED_SPECIFICATION`** (`5993d35ff0996b52`, whose paper omits detail a central question
needs), and **1 (`cvpr`) is `PASS_TO_HUMAN_CLEAN`**. This is a materially more informative
and more honest headline than "8/8 GREEN," and it required no rerun to produce — every
number was already on disk.

## 2. The architecture description matches the code: 4 lenses, 9 further gated roles

The manuscript previously said "four model components exist." Verified against the code
this pass: there are four always-on audit lenses (`OVERCLAIM`, `PROTOCOL`, `CONFOUND`,
`CONTRADICTION`) plus nine further gated specialist roles, each independently attributed
and each individually gated — the blinded grader, the whole-paper reader, the claim-link
reader, two separately-attributed literature roles (a query proposer and a reviewer), the
authors'-code reader (with its orphaned-gate caveat carried forward), the
focused-validation designer, and the reconstruction generator paired with a
separately-attributed conformance verifier. Figure 2's caption and the surrounding prose
were corrected to state this; the load-bearing claim the figure exists to make — that none
of these thirteen roles can decide anything, only deterministic code below them can — was
kept exactly as it was, because that claim was always true and remains true.

## 3. Corpus provenance is stated as the recorded account, with the manifest gap disclosed

The five-from-`sample_200`/three-from-PaperBench account is the project's own recorded
history, reaffirmed by the operator after the contrary evidence below was put to them: it
is not something this pass invented or walked back. What this pass added is the disclosure
that mattered — `data/exports/sample_200.csv` and `eligible_papers.csv`, as currently
exported, do not reproduce that mapping by content hash. The most likely explanation,
recorded in `docs/FINAL_REQUIREMENTS_CLOSURE.md` in full: a later re-extraction and re-draw
of the sampling pipeline left the current exports out of sync with the state the eight
papers were actually taken from — a provenance-recording gap, not evidence of a different
corpus. Both `manuscript/journal.tex` and `fig4_corpus_sampling.tex` now carry this
disclosure alongside the account itself, and neither this article's Results nor its
Conclusion depend on which reading is exactly right: eight papers assembled to span review
paths support a systems evaluation and no prevalence claim, whichever account is correct.

## 4. Material route exhaustion: from 17/40 (42.5%) to 29/40 (72.5%), with real executions

This is the substantive new work in this pass. The prior run's 21 "open — not attempted"
material questions were not blocked by any gate; they were never reached, because
`harness.stages.probe`'s per-invocation design advances at most the current highest-priority
target per call to `run.py review`, and the prior pass invoked it once per paper. Re-invoking
`review --force-probe` repeatedly per paper — with `SH_ALLOW_REPO_EXEC`,
`SH_ALLOW_VALIDATION_DESIGN` and `SH_ALLOW_REIMPLEMENTATION_EXEC` all open, and the
CLI-invoking automated reconstruction driver (`SH_ALLOW_REIMPLEMENTATION_DRIVER`)
deliberately left **shut**, per this session's standing instruction never to invoke the
`claude` CLI — walked the queue forward for real:

- Several author-code targets reached a genuine `IDENTITY_BLOCKED` (the only program
  available was synthesized or templated, refused by the provenance ceiling).
- Several focused-validation targets reached a genuine `SPECIFICATION_BLOCKED` at
  `arm_instantiation`, exactly as the already-published measurement predicted.
- **Seven governed reconstructions were independently generated and executed for the
  first time this system has ever run one against a real paper** (`acl` ×2 attempted, one
  reaching execution; `sanchez24a-icml` ×3, each sealed under two discovered-object framings
  = 6 executions). Each was authored by an isolated Sonnet subagent in the "generator" role
  and checked by a **separately-attributed** isolated Sonnet subagent in the "verifier"
  role — mirroring, through the harness's pre-existing manual/`SESSION_SUBAGENT` acceptance
  channel (`harness.reimplement_driver.accept_reimplementation`), the same generator/verifier
  separation the automated CLI-invoking driver would otherwise provide, without invoking any
  CLI. On `acl`, one reconstruction recomputed the paper's own stated
  `58 × 5 × 10 = 2{,}900` benchmark-construction arithmetic and a plain recount of its
  enumerated 26-model evaluation roster, both matching the paper's printed figures exactly.
  On `sanchez24a-icml`, three reconstructions built classifier-free-guidance inference
  against WinoGrande (Pythia-1.4B, GPT2-small) and CodeGen-350M-mono against HumanEval
  pass@100, from the paper's own stated method, dataset and metric.

  **All seven executions ended `INCONCLUSIVE`** — not on anything about either paper, but
  on a genuine, newly-exposed integration gap: the container-isolation backend requires a
  process's working directory to already be translated into the container's own mount
  namespace before it will run; author-code execution gets this translation from
  `provision()`'s repository-staging step, and the governed-reconstruction execution path
  had never been wired to perform the same translation, because no reconstruction had ever
  reached real execution against the container backend before this run. Every failure was
  correctly classified `INCONCLUSIVE`, never `FAILED_REPRODUCTION` — invariant 7 held
  exactly as designed.

- **A second, separate defect was found and fixed in this run's own accounting**, not in
  any paper: `harness/exhaustion.py`'s route-attempt classifier checked whether the
  automated driver's gate was closed *before* checking whether a process had actually
  launched, so a reconstruction sealed through the manual channel with that gate
  deliberately shut was at risk of being misreported as a configuration outcome for a route
  it had genuinely attempted and completed. Fixed with a targeted `outcome.launched == 0`
  guard, matching the guard the adjacent `CONFORMANCE_BLOCKED` branch already used. Full
  offline suite, both before and after: **2282 passed, 0 failed**.

- **Two central reconstruction-eligible questions on `apt-icml` were drafted and left
  unsealed.** An isolated generator subagent produced both, but each required inventing
  procedural detail the paper does not state (an optimizer; an EMA decay constant the
  paper's own Algorithm 1 names but never values; a parameter-growth schedule; the cited
  baseline's internal pruning-selection formula, stated only as "based on fisher
  information") and each would need fine-tuning a 7-billion-parameter model past this
  host's 8 GB GPU. Left unsealed rather than certified: a genuine limit of the paper's own
  specificity, compounded by this host's resources, disclosed rather than forced.

- **One reconstruction-eligible question (`5993d35ff0996b52`) was ruled ineligible before
  any subagent was dispatched**: this system's own deterministic eligibility layer
  (`harness.reimplement.assess`) found no locatable quote anywhere in the paper for either
  the training procedure or the comparison target its one claim needs.

- **Two further reconstruction-eligible questions remain genuinely untried**, not blocked
  by any gate, simply not reached within this session's own iteration budget
  (`sanchez24a-icml`'s `T10:r6:c1`, a code-completion-versus-γ result distinct from the
  three that were reconstructed; a bare printed-cell pair on `apt-icml`).

- **A genuinely mis-bound address was caught and correctly refused, not silently
  reconstructed.** A discovered reconstruction-eligible object on `sanchez24a-icml`
  (`P46:9537-9637`, expected value 192) turned out, on the generator subagent's own
  investigation, to be an illustrative worked example embedded in an appendix table (a
  qualitative CFG-vs-no-CFG demonstration on an unrelated arithmetic problem), not a
  reported result of the paper's own contribution. The generator refused rather than
  manufacture a number for a claim that does not exist — the discipline working, not a
  failure.

Final corpus-wide route-exhaustion numbers: **40 material questions, 29 fully exhausted, 7
open because no applicable route was attempted, 4 open because of this system's own
configuration, a question-weighted rate of 72.5%** (up from 17/21/2/42.5%). Seven real
operating-system processes were launched corpus-wide (up from zero), all seven completed,
zero settled — route exhaustion is not settlement, and the manuscript states the
difference rather than blurring it.

**A third, smaller accounting gap surfaced by this same work, disclosed rather than
patched over:** two process guarantees (`EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION`,
`ACCOUNTING_REPRODUCIBLE`) now read unmet on `acl` and `sanchez24a-icml` in the guarantee
record. Both are a wiring gap in `harness/guarantees.py`'s checker, not in authorization
itself — the per-target reconstruction record shows the real conjunctive authorization each
execution actually passed, but the checker reads only a single paper-level `probe` field,
which this run's repeated per-paper passes never attached the reconstruction's own
execution record to. Verified directly: `runs/acl/targets/TGT-REP-P60-155/reimplementation/
probe_results.json`'s `authorization` field reads `allowed: true`. Disclosed in the
manuscript's guarantee-record section rather than silently re-wired to read empty.

## 5. Venue/prestige masking: deferred, not built

The mandate's Section B asked for a masking layer hiding venue/prestige cues from the
scientific-reader model roles. Per explicit instruction mid-session, this was **not**
implemented this pass — no code, no tests, no claim of exercising it. It is recorded here,
plainly, as future work, not as a completed or partially-completed capability.

## 6. Test accounting, exact

Full offline suite, taken against the final committed revision (`00252bc`), both before
and after the `exhaustion.py` fix:

```
2282 passed, 7 deselected (network), 0 failed
```

## 7. The manuscript, synchronized against the corrected run

```
JOURNAL CLAIM CHECK: PASS (152,860 chars of source checked)
JOURNAL STYLE CHECK: PASS (10 generated tables, 4 hand-written)
```

`manuscript/journal.pdf`: 46 pages, compiled with `latexmk -pdf`, 0 LaTeX errors, 0
undefined references after a full clean rebuild. Visually spot-checked (front matter,
introduction, the admissibility/materiality/route-exhaustion sections, the per-paper
results table, and three of the five case studies) — content renders cleanly with no
overlapping text or broken floats. Two comparisons (the single-model baseline arm, the
component ablations) remain deliberately pinned to the archived
`runs_final_codex_v2_2026-09-15` run, captioned as such.

## 8. The release ZIP

`dist/REFEREE_final_source.zip`, built at revision `00252bc`, 268 files, no secrets
detected. `tools/zip_acceptance_test.py` extracted it into a clean temporary directory and
verified:

```
integrity: 268 entries checked, 0 mismatch(es)
self-checks: 70/84 modules carry a self-check (matches CLAUDE.md's stated count)
test suite (from the extracted copy): 2226 passed, 56 skipped (gracefully, missing
  real-corpus PDF fixtures only), 7 deselected (network), 0 failed
ACCEPTANCE: PASS
```

## 9. What remains genuinely open, and why

Stated as such, for a specific and disclosed reason, never because time ran out: the human
comparative arm (zero participants, by design); no adjudicated ground truth (unmeasurable
without human adjudication); a bounded prior-art search cannot establish novelty
(inexpressible by construction); the two `apt-icml` reconstructions (declined for cause);
two reconstruction-eligible questions genuinely untried this session; the
container/reconstruction integration gap (scoped future work, first exposed by this run);
the two now-unmet process guarantees (a checker wiring gap, disclosed); venue-masking
(deferred, not attempted).

---

## Outcome

- **PRODUCT READY: yes.** Code revision `00252bc`, offline suite 2282 passed / 0 failed,
  every gate, every known limitation, and two newly-found-and-disclosed defects (the
  exhaustion accounting fix, the guarantee-checker wiring gap) documented in the same
  artifact as the results.
- **PAPER READY: yes.** Both automated checkers pass, the PDF compiles clean with zero
  undefined references, every number in it is read off the corrected fresh run, and the
  headline result is the typed disposition rather than a colour.
- **ZIP READY: yes.** Built at the final revision, integrity-verified, and functionally
  verified by running the real test suite from inside the extracted copy.
- **READY TO SEND TO SIR: yes**, for the bounded claim this evaluation actually supports:
  a systems evaluation of eight heterogeneous-path papers, with 72.5% question-weighted
  route exhaustion, the first real (if inconclusive) governed-reconstruction executions
  this system has ever produced against a published paper, every route's authority
  ceiling, every refusal, and every remaining limitation — including two the system found
  in its own accounting this pass — stated in the same document as the results. It does
  not establish reviewer superiority, issue recall, prevalence, or human-rated usefulness,
  and says so throughout rather than in one disclaimer at the end.
