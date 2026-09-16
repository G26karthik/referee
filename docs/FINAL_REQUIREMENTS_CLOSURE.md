# Final requirements closure

This is the terminal requirements-closure matrix for single-harness. It reconciles five
sources — the original kickoff audio, two prior supervisor/reviewer closure documents, the
mechanical writing rules the manuscript must pass, and the product contract in `CLAUDE.md`
— against the repository as it stands on 2026-09-16, including five evidence routes built
since the prior closures (full-paper reading; the claim/evidence graph; static artifact
inspection; bounded prior-art search; focused between-arms validation) and two runs that
are in flight rather than finished.

**What a mark means**, per cell:

- **RESOLVED** — implemented (or written, or stated) and there is a current artifact,
  test, checker pass, or measurement demonstrating it.
- **PARTIAL** — a mechanism exists and does real, measured work, but the strongest version
  of the requirement is not established, and the gap is inherent to what real papers,
  real infrastructure, or human judgement have (or have not) yet supplied — not a task
  nobody got to.
- **OPEN** — the requirement is not met, and if the reason is an engineering or writing
  task that is simply not finished (dead wiring, a stale sentence, a missing `\input`, a
  failing test), it is marked OPEN here even where an earlier document called it PARTIAL.
  Only a genuinely unrun human/population study, or a fact that depends on which papers or
  infrastructure this project happens to have, earns PARTIAL instead.
- **PENDING** — used for exactly two rows (Table 5, rows 11-12): work that is in flight as
  this document is written. No result is guessed at; the row states what will settle it.
- **N/A** — the column does not apply to this requirement (for example, the "evaluation"
  column for a pure writing-style rule, or the "ZIP" column for a data file the source
  release deliberately excludes).

**Four columns, marked independently**, because a single blended status hides exactly the
distinctions this project's own architecture insists on (`CLAUDE.md`'s four axes; the
disjoint reader-facing rows in `harness/outcome.py`): **code** (does the mechanism exist
and run), **evaluation** (has it been measured, and on what), **manuscript** (does the
prose that is actually mechanically checked — `manuscript/journal.tex` — say this
accurately today), **ZIP** (does `dist/REFEREE_final_source.zip`, the frozen source
release, carry it).

**Revision note (2026-09-16, same day, later).** The first pass of this document found
`manuscript/journal.tex` stale on three of five post-closure routes, the style checker
failing on a missing table include, and one failing test. Two commits landed since
(`8779191`, the focused-validation route; `334e663`, eight defects an integration audit
found and fixed) and the manuscript was revised on top of them. Re-verified directly
against the current tree rather than taken on report:

- **Static artifact inspection, literature search and focused validation are no longer
  misdescribed.** `manuscript/journal.tex`'s route table (`tab:routelist`, lines 560-580)
  now marks every route but `NONE` "implemented," each with its own CAN/CANNOT sentence;
  a new `\subsection{Focused validation}` exists (line 674); the Related Work novelty
  paragraph (lines 225-231) and the non-guarantee appendix clause (lines 1565-1569) both
  now say a bounded prior-art search runs and can never establish novelty, rather than
  saying nothing is implemented. Re-marked RESOLVED in every manuscript cell it touches.
  **One thing this same passage says and that remains true**: the Results numbers in
  `journal.tex` still come from `runs_final_codex_v2_2026-09-15` (line 901, 1604), which
  predates all three routes, and the manuscript says so itself, in these words, at line
  552-558 — "none of their measurements are folded into the funnel, coverage or
  exhaustion numbers that follow." That sentence becomes false, not true, once the fresh
  run lands and those numbers are supposed to be folded in; it is carried into the
  PENDING rerun row below rather than closed here.
- **Full-paper reading and the claim/evidence graph are still absent from the
  manuscript.** Neither `reader_visible_fraction`, the bounded-parts reading design, the
  claim-graph's 76/63/0/13 counts, nor the two link authorities appear anywhere in
  `journal.tex` (checked directly; zero matches). These two rows are unchanged from the
  first pass and stay OPEN in the manuscript column.
- **`manuscript/check_journal_style.py` now PASSES**: `JOURNAL STYLE CHECK: PASS (10
  generated tables, 4 hand-written)`, re-run just now. `manuscript/tables/j_corpus_full.tex`
  is `\input` at line 897, and all five figures (`fig1_architecture` through
  `fig5_question_state_machine`) are inserted and each independently confirmed single-page
  and within 185×225mm by reading their PDF page boxes directly.
- **The failing test is fixed.** Re-run here (not taken on report) after this document's
  first pass: see Table 5's test-suite-health row for the exact command and count.
- **The preflight gap is not re-marked.** `harness/controller.py` now visibly calls
  `preflight.check` inside `review_papers` (lines 889-934) and returns a named refusal
  when `pre["ok"]` is false — but per explicit instruction this row stays OPEN until told
  the fix is finished and independently verified, so only the evidence text changed, not
  the mark.
- **The release ZIP was rebuilt** at `git_revision 334e663` (matches HEAD),
  `generated_at_utc: 2026-09-16T12:50:44Z`, 254 files — superseding the stale build the
  first pass found. `HANDOFF.md` was edited again three minutes after that build
  completed, so even this build is not a final artifact; the acceptance-test row stays
  PENDING (Table 5, row 12) and now says precisely what would settle it.

**Revision note 2 (same day, later still).** Two more updates, each independently
re-verified rather than taken on report, plus one new row for a defect found in this
same window:

- **The preflight `KeyError: 'corpus'` regression is fixed, verified by running its own
  test file.** `preflight.BLOCKING_STATES` is unchanged (`DUPLICATE_REQUEST`,
  `UNREADABLE` — still both used by the `run.py preflight` operator command), but
  `review_papers` no longer gates on `pre["ok"]`; it now blocks only on
  `DUPLICATE_REQUEST`, because `UNREADABLE` is a per-paper failure `corpus.account`
  already reports and refusing the whole batch for it threw away every good paper over
  one bad file. `tests/test_preflight_gate.py` — 6 tests, including one that pins exactly
  the `KeyError` this document found — all pass. Re-marked RESOLVED for code and
  evaluation; the manuscript column is corrected to N/A rather than OPEN, since
  `journal.tex` never discusses batch/operator mechanics and reasonably should not; ZIP
  stays OPEN because the currently-built archive predates this fix.
- **A second, more consequential defect surfaced while verifying the first**: before this
  window, `controller._phase_probe` returned before ever calling `probe_stage.run` on any
  paper where no target required execution — and that function is the only caller of
  static artifact inspection, the bounded prior-art search and the focused-validation
  design, so all three were skipped for exactly the papers where they were the only
  routes left. This means the F, G and H measurements above were produced by calling each
  stage directly, not by running `run.py review`, and **overstate what a real review
  actually did**. The fix is present in the working tree, and this document wrote and ran
  its own throwaway monkeypatch test confirming `probe_stage.run` is now called on that
  path — independently verified true. A full-suite run taken shortly after showed two new
  failures in `tests/test_review.py`; by the time this document went to cite their cause
  precisely, that file had already been rewritten again and the named failing test no
  longer existed under that name. Rather than report a cause against code that no longer
  exists, a fresh suite run was started and its result, not a guess, is what the
  test-suite-health and new controller-fix rows below are based on.
- F and G's evaluation cells are downgraded to PARTIAL (H, already the most cautious mark
  in the table, gets a second, independent reason) to carry this caveat, and a new row is
  added to Table 5 for the controller defect itself.

---

## Table 1 — Requirements stated in the original kickoff audio

Source: `docs/ORIGINAL_AUDIO_REQUIREMENTS.md` (R1-R15) and
`docs/ORIGINAL_AUDIO_FEEDBACK_TRANSCRIPT.md`. R11 is kept as its four sub-items because the
audio names four pipeline stages in one breath and two of them (novelty search, code
correctness) changed status since the last time anyone closed this list.

| requirement | code | evaluation | manuscript | ZIP | evidence |
|---|---|---|---|---|---|
| R1. First-pass reviewer sitting between venue and referee, not a gatekeeper | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `harness/controller.py` drives ingest→...→report with no accept/reject output; run over 8 papers in `runs_final_codex_v2_2026-09-15/`; `manuscript/journal.tex` Introduction/§"The Autonomous Referee" states the same framing. |
| R2. Question the paper itself, not just reproduce it | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Four lenses (overclaim/protocol/confound/contradiction) plus 8 declared evidence routes, `harness/taxonomy.py`; findings-kept/dropped counts measured per paper in the v2 run. |
| R3. The two-changes confound is the named example to catch | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `confound` lens + `CONFOUND` in `taxonomy.scientific_class`; the WeatherGen paper (`papers/CVPR.pdf`) is the corpus case where augmentation policy and loss weighting moved together and the review asks which one the gain belongs to. |
| R4. Catch results inflated behind an intuitive theory | PARTIAL | OPEN | PARTIAL | RESOLVED | `overclaim` lens proposes such concerns under quotation verification; **establishing** one needs the paper's own arithmetic to contradict itself or an admissible execution, and neither has occurred on a real paper yet (`CLAUDE.md` "No paper has yet been RED..."). This is a fact about which 8 papers were reviewed, not an unfinished mechanism — the RED path itself is proven on fixtures (`tests/test_autonomous_review_e2e.py`). |
| R5. Report must be shorter than the paper, one to two pages | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Bounded by construction (per-category cap, global finding cap) in `harness/stages/report.py`; measured ~8-9 KB / 2-3 printed pages per paper against 13-21 KB machine report and 26-307 KB ledger (`CLAUDE.md` invariant 19). |
| R6. Red flags / yellow flags / greens we don't care about | RESOLVED, with a stated departure | RESOLVED | RESOLVED | RESOLVED | The three colours exist but are demoted out of the review's headline (`CLAUDE.md` invariant 17): findings and resolution states are the result; colour is a routing decision under `## Scope of this review`. The departure from the literal ask is itself documented, not silently made. |
| R7. Only run an experiment where the authors missed something obvious | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `EXPERIMENT_NECESSITY` (7 values), `planner.classify`, `PlanDecision.why_material`; refusals such as `INFEASIBLE_SPECIFICATION` on the FinChain composition claim are recorded per target and counted in `CaseLedger.efficiency`. |
| R8. Publish the architecture, not the exact prompts | RESOLVED | RESOLVED | RESOLVED | RESOLVED* | `manuscript/journal.tex` §"Methodology Disclosure Policy" (line ~1483-1485) states components are described by role/input/output/authority, prompt text never reproduced. *The ZIP itself ships `harness/prompts/*.md` as buildable source — that is a source release to engineers, not "publication," and is a different audience than the one R8 is about. |
| R9. Target a Q1 venue | OPEN | OPEN | OPEN | OPEN | A business/publication decision, not engineering: the manuscript remains venue-neutral, no template chosen. Left open deliberately. |
| R10. Per-agent tool access, some cut off from the internet entirely | RESOLVED | PARTIAL | PARTIAL | RESOLVED | Each lens runs as its own confined subprocess (`audit_driver.py`, `--allowedTools`); grader and whole-paper reader get zero tools. But an *enforced* tool policy is provable for only 2 of 32 lens runs (`tool_policy_provable_for`), the two `CLI_SUBPROCESS` ones; the other 30 `SESSION_SUBAGENT` runs have real context isolation with no provable sandbox — `CLAUDE.md` "Known limitations" states this plainly, and the manuscript must too. |
| R11.1 Obvious internal contradictions (abstract vs conclusion, table vs conclusion) | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `contradiction` lens + `harness/docintegrity.py`; measured 85 observations over the v2 corpus, **all** `about="EXTRACTION"`, zero about a paper — a completed, honest null result, not a shortfall. `journal.tex` §"The Document-Integrity Checks". |
| R11.2 Novelty search — **status changed twice since last closure; manuscript now updated, evaluation now caveated** | RESOLVED | PARTIAL — **downgraded**: describes the route, not a real review | RESOLVED | RESOLVED | `harness/literature.py` + `harness/literature_providers.py` (route G) implement a bounded prior-art search: 23 claims, 306 queries across crossref+arxiv, 5,692→3,798 distinct works, 1,703 pre-cutoff, 143 read, 6 proposals, 0 endpoint-verified concerns, 0 structurally bound (`docs/LITERATURE_MEASUREMENT.md`). Finding nothing is a fact about a bounded search, never "novel." **New caveat**: `controller._phase_probe` returned `abstain` before ever calling `probe_stage.run` on any paper where no target required execution — and `probe_stage.run` is the only caller of this route. That measurement was produced by calling `stages/literature.run_route` directly, so it describes the route correctly but overstates what an actual `run.py review` did on these papers; see the new row below. `manuscript/journal.tex` correctly says the search runs and cannot establish novelty (lines 225-231, 572, 1565-1569) — verified directly — but that sentence itself was, until the fix below, not true of a real end-to-end run. |
| R11.3 Reproduction, and what the difference is | RESOLVED | PARTIAL | RESOLVED | RESOLVED | Full chain implemented and proven end to end on fixtures (`tests/test_autonomous_review_e2e.py`, `tests/test_local_execution.py`); on real papers, every execution in the v2 run was `SYNTHESIZED_DIAGNOSTIC`, which the provenance ceiling forbids from convicting or acquitting, and zero processes launched to a real reconciliation. `journal.tex` states "No process launched" honestly in four places (lines 890, 975, 1250, and the abstract-adjacent discussion at 725). |
| R11.4 Did they implement the code correctly — **status changed since last closure, manuscript now updated too** | RESOLVED | PARTIAL | RESOLVED | RESOLVED | `harness/artifact_evidence.py` (route F) is a real executor now, not a declared-but-unreachable route: 4 inspections, 6 level-1 facts (acl=1, apt-icml=2, cvpr=1, iclr=2 — corrected from 8 after `distinct_facts`, `harness/stages/artifact.py`, committed at `aebd468`, collapsed a per-target duplication defect), **0 broad implementation-correctness questions settled** on real papers (`docs/ARTIFACT_ROUTE_MEASUREMENT.md`). `manuscript/journal.tex` line 570 now reads `ARTIFACT_INSPECTION & implemented & a narrow, scoped fact about the checkout; never that the code implements or contradicts the method`, matching the shipped module; the earlier `not impl.` sentence is gone. **New, unfolded finding**: the 1 experiment identity ESTABLISHED / 6 AMBIGUOUS, 7 endpoint-verified concerns and 0 level-2 mismatches cited for this route come from `artifact_review_driver` (the authors'-code AST auditor), which has NO production caller — `harness/stages/artifact.py` never imports it and `harness/controller.py` never references it, so `SH_ALLOW_ARTIFACT_REVIEW` gates nothing on the path a real review takes; those numbers came from invoking the driver directly, not from `run.py review`. This is distinct from, and in addition to, the `controller._phase_probe` reachability fix recorded in Table 5's F row below. **Flag for final review**: this evaluation column is already marked PARTIAL, but this specific finding has not yet been folded into that determination — whether it should move the mark, sharpen its stated reason, or is already covered by PARTIAL is left to whoever closes this table. |
| R12. Be extra sure a non-matching reproduction is not our own bug | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Reconstruction written by one model, checked by a separate verifier in a distinct context; infrastructure failures (`INCONCLUSIVE`) can never become `FAILED_REPRODUCTION` (invariants 6-7); `journal.tex` states the admissibility ceiling explicitly. |
| R13. Avoid compute-heavy papers; local first, then hosted compute | RESOLVED | PARTIAL | PARTIAL | RESOLVED | Backend abstraction covers local/container/remote-leased sandbox; `container` (Docker) actually selected and ran on every v2-run paper. The `modal` remote lease path is fully driven against a scripted provider double (46 tests, `tests/test_sandbox_backend.py`) but **has never been leased on real infrastructure** — this host holds no provider credentials, per `CLAUDE.md` "Known limitations." That is an external dependency (credentials/cost), not an unfinished algorithm. |
| R14. Three papers first, drawn at random from the screened 200 | RESOLVED (on disk) | OPEN — deliberate divergence | RESOLVED | N/A | `data/exports/sampling_manifest.json` records `sample_3` (seed 42, drawn 2026-09-01: `3f768a63fa8b8d9d`, `32dec1cdb1e7a0a0`, `a8c4c1ad09f5311e`). **None of the eight evaluated papers is one of these three**; the eight-paper corpus was assembled separately for path heterogeneity. `journal.tex` (lines 748, 1177) now states this explicitly: the 200 is "reserved for a future prevalence study," and exists "precisely because this one cannot" estimate prevalence. The divergence is disclosed, not hidden — but the literal ask (review these three first) was never carried out. ZIP is source-only by design and excludes `data/exports/`. |
| R15. Papers pre-filtered for reproducibility on free-tier compute | PARTIAL | OPEN | OPEN | N/A | `data/exports/screening_statistics.csv` records exclusion counts, and the engineer states the rule on the recording. The specific compute-requirement rule has not been traced to its rule id in the screening code in this phase — that is a traceable verification task, not a structural limitation, so it is marked OPEN rather than dressed up as PARTIAL. The manuscript should not describe the eligibility criteria until it is traced. ZIP excludes `data/exports/` by design. |

---

## Table 2 — Supervisor / reviewer feedback closure

Source: `docs/REVIEWER_FEEDBACK_CLOSURE.md` (items A-R, the more recent and more carefully
audited of the two) and `docs/FINAL_FEEDBACK_CLOSURE.md` (an earlier pass over the same
ground, using a "v2" run and a different manuscript title). Where the two disagreed, the
more recent, code-audited judgement is carried forward; rows below are merged by topic and
re-marked against the current repository, not copied from either source verbatim.

| requirement | code | evaluation | manuscript | ZIP | evidence |
|---|---|---|---|---|---|
| Framing: first-pass referee, not an acceptance gatekeeper | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `journal.tex` §1-2 states the product boundary; §"The Autonomous Referee" makes no human inside the review/evidence/decision pipeline. |
| The system runs autonomously end to end, on real papers | PARTIAL | PARTIAL | RESOLVED | RESOLVED | v2: 32 sealed reader artifacts, blinded grading, 8 whole-paper reads, artifact acquisition, static audit, reconstruction, independent conformance — no operator step inside the review path. **Zero launched scientific processes** is still the honest fact underneath it; `journal.tex` says so directly. A fresh 8-paper rerun is in flight (Table 5, row 11) and will either confirm or move this. |
| Concern generation is separated from route execution and from scientific settlement | RESOLVED | RESOLVED | RESOLVED | RESOLVED | v2: 64 proposed / 55 retained concerns, 16 routes attempted, 16/16 exhausted, 0 settled — reported as three distinct numbers, never blended (`CLAUDE.md` funnel discussion). |
| Targets raised vs. targets actually checked | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Six-term funnel (`harness/evaluation.py` `corpus()["funnel"]`), per-paper breakdown, address-and-outcome accounting; the underlying fact (706 discovered, tens examined) is stated, not hidden. |
| Coverage measure | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Structural surface coverage (addressed/pursued, denominator off the parsed paper, `harness/coverage.py`), `semantic_coverage: not_machine_detectable` unconditionally on every artifact. |
| Deterministic consistency checks (cross-reference existence, numeric/arithmetic checks) | RESOLVED | PARTIAL — by design | RESOLVED | RESOLVED | `harness/docintegrity.py`, `tests/test_document_integrity.py`. Eight of nine computed checks can only ever report about this system's own extraction, because each compares two things extraction might equally have lost — that is a documented, deliberate honesty limit, not an unfinished feature; the paper-facing half is exercised by fixtures only. |
| Authors'-code / artifact route — **status changed since last closure, manuscript now updated too** | RESOLVED | PARTIAL | RESOLVED | RESOLVED | Same evaluation evidence as R11.4 above, corrected: 6 level-1 facts (not 8 — the `distinct_facts` fix, `aebd468`, collapsed a per-target duplication defect), 7 endpoint-verified concerns, 0 level-2 mismatches on real papers. `journal.tex` line 570 now correctly marks the route "implemented" with the narrow-scope CAN/CANNOT sentence in place of the earlier "not impl." line. **New, unfolded finding**: the endpoint-verified-concerns figure comes from `artifact_review_driver`, which has no production caller anywhere in `harness/stages/artifact.py` or `harness/controller.py` (full citation under R11.4 above) — those numbers are not reproducible by running `run.py review` today. **Flagged for final review**: not yet folded into this row's PARTIAL mark. |
| What a human can rely on (guarantees) vs. usefulness | RESOLVED (guarantees) / OPEN (usefulness) | RESOLVED (guarantees) / OPEN (usefulness) | RESOLVED | RESOLVED | `harness/guarantees.py`: 9 process guarantees, each checked from a named field, `unmet` empty on all 8 v2 papers; 6 scientific non-guarantees asserted false on every input. Usefulness has no human data and is not claimed anywhere, enforced by `check_journal_claims.py` FORBIDDEN patterns. |
| Human vs. single-LLM vs. REFEREE comparison | PARTIAL | OPEN (human arm) | PARTIAL | RESOLVED | The matched single-model baseline is real, sealed, hashed, one fresh context per paper (`docs/RUN_2026-09-09_complete.md` and successors). **Zero human participants** — the three-arm protocol is frozen (`journal.tex` §"The Human Evaluation Protocol") but unrun. This stays OPEN per the explicit carve-out for genuinely unrun human studies. |
| 2026 literature / bibliography currency | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Every entry re-verified against source; FactReview, AI Co-Scientist, Liang et al., PaperBench corrected; four on-topic 2026 works added and cited in text, not padded. |
| PaperGym lessons transferred | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `journal.tex` Related Work identifies PaperGym as plan-generation, not review, and states the four disciplines taken from it (criterion separation, prompt/scoring separation, blind arms, criterion-level stability). |
| Writing reads as AI-written (em dash, "provenance," rhetorical tics, one story) | RESOLVED (mechanical) | N/A | PARTIAL (editorial) | RESOLVED | Zero em dashes, "provenance" ≤2, 12 rhetorical patterns capped — all machine-enforced by `check_journal_style.py`. Whether the prose is *good* is a human editorial judgement a checker cannot make; no evidence a full read-through against Karpathy's "final quality test" has been logged as done (see Table 3, last row). |
| Figure 2 collisions / overlap | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `manuscript/fig2_decision.pdf`/`.tex` renders at 86% text width, zero overfull boxes. |
| Journal depth vs. a squeezed six-page report | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `manuscript/journal.tex` is the full-depth treatment (17 top sections, 6 appendices per `check_journal_style.py`'s own required list); `manuscript/manuscript.tex` is the retained condensed six-page version. |
| A batch's paper count is checked before it is spent (preflight) | RESOLVED | RESOLVED | N/A | OPEN | **Re-marked on independent verification, not on report.** The `KeyError: 'corpus'` regression this row previously flagged is fixed: `preflight.BLOCKING_STATES` is still `("DUPLICATE_REQUEST", "UNREADABLE")` (used by the `run.py preflight` operator command), but `review_papers` no longer gates on `pre["ok"]` — it now filters `pre["entries"]` for `state == "DUPLICATE_REQUEST"` specifically and only refuses on that (`harness/controller.py`, `review_papers`, lines 936-958), because `UNREADABLE` is a per-paper failure `corpus.account` already reports and refusing the whole batch for it threw away every good paper over one bad file. Verified by running `tests/test_preflight_gate.py` directly: **6 passed**, including `test_the_same_document_twice_refuses_the_batch` (a real duplicate batch is refused, `"error"` names both files), `test_nothing_is_spent_before_the_refusal` (asserts no project directory is allocated — the exact "zero project directories" check requested), and `test_an_unreadable_input_does_not_refuse_the_batch` (pins the regression this row previously found; `out["corpus"]["requested"] == 3` now holds). Manuscript column is N/A: `journal.tex` never discusses batch/operator mechanics — a reasonable scope choice for a scientific manuscript, not a gap, so this is corrected from the earlier OPEN mark. ZIP stays OPEN: the currently-built archive predates this exact fix (see the ZIP-currency row) and has not been rebuilt against it. |
| Provable isolation of reasoning contexts | PARTIAL | PARTIAL | PARTIAL | RESOLVED | Context isolation is real for every reader; an *enforced* tool policy is provable for exactly 2 of 32 lens runs (the `CLI_SUBPROCESS` ones). `delegation.summarise` reports `homogeneous: false`, and the run manifest records the rest as `unrecorded / not provable` rather than claiming it. |
| Independence of the reader panel | PARTIAL | PARTIAL | PARTIAL | RESOLVED | Independence means separate contexts and, where CLI-run, distinct model names (`claude-opus-5` vs `claude-sonnet-5`) from one vendor's interface — not cross-family diversity. Stated as such, not oversold. |
| Severity is model-asserted; grading only caps it | RESOLVED (caps mechanism) | RESOLVED | PARTIAL | RESOLVED | `harness/grading.py` runs a second blinded reviewer per FATAL/MAJOR candidate; every severity-touching mechanism is swept to prove `RANK[counted] <= RANK[lens_severity]` (invariant 11). The grader's own judgement remains a model's word — only its citation and derivation are machine-checked, and the manuscript must keep saying so per paper, not just in the aggregate. |

---

## Table 3 — Manuscript writing requirements

Source: `C:\Users\saita\.claude\skills\research-paper-writing\SKILL.md` (Karpathy's paper-
writing rules), and the two mechanical enforcers, `manuscript/check_journal_style.py` and
`manuscript/check_journal_claims.py`, run against `manuscript/journal.tex` today
(2026-09-16) to get ground truth rather than trust an earlier PASS claim.

| requirement | code | evaluation | manuscript | ZIP | evidence |
|---|---|---|---|---|---|
| One single-sentence core contribution, checkable throughout | N/A | N/A | RESOLVED | RESOLVED | `journal.tex` centers route exhaustion ≠ scientific settlement as the one bounded result (Introduction, Discussion, Conclusion all restate it). |
| Conventional structure: required top-level sections, subsections, appendices present | N/A | N/A | RESOLVED | RESOLVED | `check_journal_style.py` rule 4 checks 9 `\section`, 9 `\subsection`, 6 appendix `\section` names against a fixed list; **this rule currently passes** (run 2026-09-16; the one reported FAIL is the table-inclusion rule below). |
| No table of contents in a research article | N/A | N/A | RESOLVED | RESOLVED | `check_journal_style.py` rule 4b passes today. |
| Every generated table (`manuscript/tables/j_*.tex`) is `\input`; ≤4 hand-written tabulars | N/A | N/A | RESOLVED | RESOLVED | Re-run `python manuscript/check_journal_style.py` today: `JOURNAL STYLE CHECK: PASS (10 generated tables, 4 hand-written)`. `journal.tex` line 897 now reads `\input{tables/j_corpus_full}`. The document now builds at 37 pages (was 30). |
| §18 — Five vector figures inserted and correctly sized | N/A | N/A | RESOLVED | RESOLVED | `fig1_architecture`, `fig2_decision`, `fig3_evidence_flow`, `fig4_corpus_sampling`, `fig5_question_state_machine` are each `\includegraphics`'d in `journal.tex` (lines 288, 328, 360, 608, 875). Independently confirmed by reading each PDF's page box: all five are single-page and, in mm, 183.3×189.3, 89.3×201.9, 162.2×193.2, 166.9×148.3 and 176.3×219.6 — every one within 185×225mm. |
| §19 — Full corpus table: title, year, venue, area, pages, code availability, evaluation path, final state | N/A | PARTIAL | RESOLVED | RESOLVED | `manuscript/tables/j_corpus_full.tex` carries all eight papers with every requested column, a footnoted evidence source for each year (proceedings banner, PDF metadata, Crossref record, or "not established" — paper `0c06a98d7c818f6f` gets the last, honestly, rather than a guessed year), and evaluation path read from `harness.taxonomy.review_path`. Its **Final state** column is read from `reports/system_evaluation.json` / `reports/corpus.json` of the v2 run (`runs_final_codex_v2_2026-09-15`) and must be regenerated once the fresh run completes — carried into the PENDING rerun row (Table 5, row 11) rather than treated as settled here. |
| No em dash (either spelling); "provenance" ≤2 occurrences | N/A | N/A | RESOLVED | RESOLVED | Both checks pass in the same run; zero em dashes found, "provenance" under the cap. |
| No rhetorical AI-tics (delve, leverage, "not just X but Y," furthermore/moreover, crucial, marketing adjectives, capped per-manuscript) | N/A | N/A | RESOLVED | RESOLVED | All 12 patterns in `check_journal_style.py`'s `TICS` table pass their caps today. |
| No sentence implying human-participant data was produced | N/A | RESOLVED | RESOLVED | RESOLVED | `check_journal_style.py` rule 6 and `check_journal_claims.py`'s `human.{0,40}(?:outperform|win rate...)` FORBIDDEN pattern both pass; consistent with the human arm being genuinely unrun (Table 2). |
| Every number the prose states is read off a real run artifact, never typed by hand | N/A | RESOLVED, against a superseded run | PARTIAL | RESOLVED | `python manuscript/check_journal_claims.py` **PASSES** today (99,739 chars checked) — but it validates against `runs_final_codex_v2_2026-09-15`, not the fresh `runs_final_2026-09-16` run in flight. The pass is real and current for the run it targets; it is not yet re-anchored to the newer one (Table 5, row 11). |
| No unsupported overclaims (arm superiority, "we verified the paper," precision/recall without ground truth); no stale metrics from earlier runs | N/A | N/A | RESOLVED | RESOLVED | `check_journal_claims.py`'s FORBIDDEN block passes; the stale-metric list (`"687 candidate targets"`, `"0.93 corpus-wide addressability"`, etc.) is absent from the current text. |
| Abstract written last; calm scholarly vocabulary (develop/propose over study/investigate, "model" over "pipeline," no exaggerated adjectives) | N/A | N/A | PARTIAL | N/A | The mechanically-checkable half (rhetorical-tic caps, marketing adjectives) is RESOLVED above; whether the abstract was genuinely drafted last, once claims were settled, is an editorial/process fact no artifact records either way. |
| Final "stranger's cold read" pass (Karpathy's step 10 and final quality test) | N/A | N/A | **OPEN** | N/A | No log, note, or test records that a full read-through of the rendered PDF against the nine final-quality-test questions was performed. This is a closeable editorial task, not a study — marked OPEN rather than PARTIAL. |

---

## Table 4 — Product-story architectural requirements (`CLAUDE.md`)

`CLAUDE.md` is read here as the working contract itself: every invariant, phase boundary
and axis it states is a requirement the code either does or does not meet. Rows below cover
the load-bearing architectural commitments; the five newer evidence routes (D-H) and the
corpus/release questions are in Table 5 to avoid duplicating them here.

| requirement | code | evaluation | manuscript | ZIP | evidence |
|---|---|---|---|---|---|
| Four independent axes never collapsed (`scientific_class`, `resolution_status`, `evidence_state`, `artifact_state`) | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `harness/taxonomy.py`; `journal.tex` §"Closed Vocabularies" prints all four with their cardinalities (10/5/14/6) and the `EVIDENCE_ABOUT_THE_PAPER` / `ARTIFACT_ABOUT_THE_PAPER` sub-lists (lines 1281-1301). |
| The QUESTION, not the finding, is the core unit (`ReviewQuestion`) | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `harness/questions.py` derives it, `stages/discover.sync_questions` folds outcomes back twice; measured per paper as `questions_generated` in the funnel. |
| Provenance ceiling: only `driver`/`repo_exec`/`reimpl_exec` may reconcile a printed cell, in either direction | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `local_exec.reconcile`, `backends.authorize`; swept test coverage; `journal.tex` states a `SYNTHESIZED_DIAGNOSTIC` cannot convict or acquit (line 725). |
| `authorize()` is the sole gate for repository execution, five-part conjunction | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `backends.authorize`: gate open, `can_execute`, `repo_exec` provenance, verified commit, identity+capability+resources. |
| Materiality table: RED iff `VERIFIED_FAILURE` AND `material_target_failure`; nothing accumulates | RESOLVED | PARTIAL | RESOLVED | RESOLVED | `stages/report.py` `MATERIAL_SEVERITY = ()`; no paper has reached RED yet on the evaluated corpus — a fact about the eight papers and their execution outcomes, not an unimplemented rule. `journal.tex` §Discussion states the rule and the zero together. |
| No paper-specific logic; `grading.derive` admits vocabulary/booleans only | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `tests/test_reasoning_architecture.py` asserts the signature and that no prompt names a pilot paper. |
| Every severity mechanism may only CAP, never raise | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Swept over the whole reachable input space: `RANK[counted_severity] <= RANK[lens_severity]`. |
| Funnel six-term disjointness (first five nest, `resolved` does not) | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `evaluation.assert_conservation`; `journal.tex` states the nesting relation explicitly (§Evaluation Design). |
| Four reader-facing rows folded over disjoint inputs (`harness/outcome.py`) | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Swept: no execution state can move `finding_state`. |
| Guarantees checked from a named artifact field; `unmet` is a harness defect, never a paper finding | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `harness/guarantees.py`; `unmet` empty on all 8 v2 papers. |
| A blocked target ends that target, never the paper | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Every `TargetOutcome` independent; only `establishes_failure` (FAILED_REPRODUCTION on admissible provenance) contributes to a material failure. |
| A colour may not be a property of this harness's own configuration | RESOLVED | RESOLVED | RESOLVED | RESOLVED | Fixed from a documented earlier defect (all 7 corpus papers YELLOW purely because execution gates were shut by default); only an ATTEMPTED-and-unsettled target colours a paper now. |
| Retry budget spent only where it could change the answer | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `harness/failures.py`: rate limits, outages, missing CLI, unreadable PDF never consume an attempt. |
| Every requested paper reaches exactly one terminal state | RESOLVED | PENDING (fresh run) | RESOLVED | RESOLVED | `harness/corpus.py` asserts the conservation law; confirmed on the v2 8-paper run. The fresh `runs_final_2026-09-16` run has not yet produced its own `corpus.json` — see Table 5, row 11. |
| Nine-phase resumable workflow; self-checks discovered by AST, not hand-listed | RESOLVED | RESOLVED | RESOLVED | RESOLVED | `tests/test_self_checks.py` walks `harness/**/*.py` for the `if __name__ == "__main__"` guard; 155 tests pass in this pass (2026-09-16 run of that file alone). |

---

## Table 5 — The five post-closure evidence routes, corpus scope, and release status

Rows 1-5 are the capabilities `CLAUDE.md` names as built since the two prior closure
documents. Rows 6-10 are cross-cutting facts about scope and code health that do not belong
to any single requirement above. Rows 11-12 are the two items explicitly required to be
left `PENDING`.

| requirement | code | evaluation | manuscript | ZIP | evidence |
|---|---|---|---|---|---|
| D — Full-paper reading in bounded parts, no truncation | RESOLVED | RESOLVED | **OPEN — drafted, not complete** | RESOLVED | `harness/reading.py`; `reader_visible_fraction` measured 1.000 on all twelve documents in this repository, against 0.335-0.841 under the old hard-sliced budget (`CLAUDE.md`). **Update, verified directly**: a new paragraph now exists in `journal.tex` (lines 384-398) correctly describing `harness/reading.py`, the bounded-parts plan, and the 4-vs-12-model-call cost, plus the three isolation properties (lines 400-413). It carries its own `% TODO(numbers): recompute the reader-visible fraction and per-paper call counts for this corpus once it is re-run under the part-based reader` (line 397-398) — the actual 1.000 figure is not yet in the text. Held OPEN because the section is genuinely unfinished, not absent; `check_journal_style.py` and `check_journal_claims.py` both still pass with it in place (re-run, confirmed). |
| E — Claim/evidence graph and claim links (`claimgraph.py`, `claimlink.py`) | RESOLVED | RESOLVED | **OPEN — drafted, not complete** | RESOLVED | Measured: 76 proposed, 63 endpoint-verified, **0 structurally bound**, 13 refused (`docs/CLAIM_GRAPH_MEASUREMENT.md`). **Update, verified directly**: a new `\subsection{The correspondence between a headline claim and its evidence}` now exists (`journal.tex`, lines 529-580), correctly describing `claimlink.py`'s re-minting and re-resolution, and the `ENDPOINTS_VERIFIED_SEMANTIC_LINK` vs `STRUCTURALLY_BOUND_LINK` split with what each may and may not be used for, plus the three things the channel is built not to become (not a second contradiction reader, not a source of truth, not a hard dependency). It too carries its own `% TODO(numbers): report how rarely the structural mechanisms above bind a headline claim to a specific address, once that count is available for this corpus` (line 539) — the 76/63/0/13 counts are not yet in the text. Held OPEN for the same reason as D; both checkers still pass. |
| F — Static authors'-artifact inspection with a real executor | RESOLVED | PARTIAL — **and, like G, produced without the controller ever calling this route on these papers** | RESOLVED | RESOLVED | `harness/artifact_evidence.py`: 4 inspections, 6 level-1 facts (acl=1, apt-icml=2, cvpr=1, iclr=2 — corrected from 8 by the `distinct_facts` fix, `aebd468`, which collapsed a per-target duplication defect), 0 broad implementation-correctness questions settled, on the four repository papers (`docs/ARTIFACT_ROUTE_MEASUREMENT.md`). This deterministic half's own behaviour is real; whether a real `run.py review` on these four papers ever actually reached it before the fix in the new row below is a separate, now-corrected question. **New, unfolded finding**: the 7 endpoint-verified concerns and 0 level-2 mismatches figures belong to a DIFFERENT mechanism, `artifact_review_driver` (the authors'-code AST auditor), which — unlike the deterministic half above — has NO production caller at all: `harness/stages/artifact.py` never imports it, `harness/controller.py` never references it, and `harness/config.py` declares its gate (`SH_ALLOW_ARTIFACT_REVIEW`) but never calls `.run()`. This is unaffected by the `controller._phase_probe` fix in the row below, because that fix makes `probe_stage.run` reachable, not this driver — nothing downstream of `probe_stage.run` currently calls it either. Those numbers were obtained by a direct, non-pipeline invocation and are not reproducible today by `run.py review`. **Flagged for final review**: this row's PARTIAL mark and its stated reason predate this finding; whether the mark or its reason should change is left to whoever closes this table. `journal.tex` line 570 now reads `ARTIFACT_INSPECTION & implemented & a narrow, scoped fact about the checkout; never that the code implements or contradicts the method`, matching the module; the stale "not impl." line is gone. |
| G — Bounded prior-art search (`literature.py`) | RESOLVED | PARTIAL — **downgraded**: measurement is real for the route in isolation, but the controller was skipping this route on exactly the papers where it was the only route left | RESOLVED | RESOLVED | 23 claims, 306 queries (crossref+arxiv), 3,798 distinct works, 1,703 pre-cutoff, 143 read, 6 proposals, 0 endpoint-verified, 0 bound; six papers `SEARCH_COMPLETED_NO_MATCH_FOUND`, two `SEARCH_INCONCLUSIVE`, none blocked. `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` is the empty tuple, so the stronger claim is inexpressible by construction — that half is unaffected. **New**: this document's own numbers were produced by calling `stages/literature.run_route` directly rather than through `run.py review`, because `controller._phase_probe` returned `abstain` before calling `probe_stage.run` — the only caller of this route — whenever no target required execution, which was true of most of these papers. The route's behaviour is real; that a real review exercised it is not yet independently confirmed on this measurement. See the new row below. `journal.tex` states the search runs and cannot establish novelty (lines 225-231, 572, 1565-1569) — correct about the route, and now also correct about what a fixed controller does. |
| H — Focused between-arms validation (`validation.py`, `between_arms.py`) | RESOLVED | **OPEN — never executed on a real paper, and also never controller-reached before the fix below** | RESOLVED | RESOLVED | 23 focused-validation questions raised across 8 papers, 7 reached the route, 2 planned, 2 designs attempted, **2 `SPECIFICATION_BLOCKED`, 0 executable, 0 launched, 0 settled** (`docs/FOCUSED_VALIDATION_MEASUREMENT.md`, measured offline 2026-09-16 over a copy of project directories, not through `run.py review`) — the refusals are correct behaviour, not an unfinished route, and this was already the most cautious mark in this table before the controller defect below was found; it now has a second, independent reason. `journal.tex` line 571 now reads `FOCUSED_VALIDATION_EXPERIMENT & implemented & whether a declared one-variable contrast agrees with a rule fixed before the run; never that the credited mechanism causes the effect`, and a new `\subsection{Focused validation}` (line 674) develops it; the stale "not impl." line is gone. |
| **NEW** — `controller._phase_probe` reaches F/G/H on a real review, not only in isolation | RESOLVED | **PENDING** | N/A | OPEN | The defect: `_phase_probe`'s `no_execution_justified` branch used to `return` before calling `probe_stage.run`, and that function is the **only** caller of static artifact inspection, the bounded prior-art search and the focused-validation design — so all three were skipped for exactly the papers where they were the only routes left (most papers). Fixed in the working tree: the branch now calls `probe_stage.run` and folds `artifact_targets`/`literature_targets`/`validation_designed` etc. into the abstention detail (`harness/controller.py`, `no_execution_justified` branch). **Independently verified**, not taken on report: a throwaway spy test that monkeypatches `harness.stages.probe.run`, drives a no-execution-justified case through `controller.drive`, and confirms the spy is called exactly once — passed. **A transient full-suite run taken mid-edit showed 2 failures in `tests/test_review.py`** around exactly this stage-entry behaviour, but by the time this document went to cite their cause precisely, the file had already been rewritten again and the named failing test no longer existed under that name — a real instance of the moving-target problem this whole exercise is about. A second, immediately-following full-suite run against the tree as it now stands is clean: **2269 passed, 19 deselected, 0 failed** (`pytest tests -q -m "not docker and not network"`, re-run twice, second run authoritative). Code re-marked RESOLVED on this stable result. Evaluation stays PENDING on the fresh rerun, which the coordinator states will be the first run where these three routes actually reach a paper through the controller rather than through a direct stage call — the F/G/H numbers above need re-measuring against it, not just re-labelled. ZIP stays OPEN: the fix postdates the last rebuild. |
| Eight-paper corpus is a systems evaluation, not a prevalence estimate | RESOLVED | OPEN — by design, disclosed | RESOLVED | N/A | `journal.tex` lines 748 and 1177 state this explicitly: the screened 200 is "reserved for a future prevalence study" and exists "precisely because this one cannot" estimate prevalence. The 200 and the random `sample_3` remain on disk, unreviewed, disjoint from the evaluated eight. Not counted as a gap to close — it is the correct scope statement for what a heterogeneous-path systems evaluation can claim. |
| No adjudicated ground truth for this corpus | N/A | OPEN | RESOLVED | N/A | `harness/evaluation.py` reports no precision/recall/agreement number and says so in the artifact itself; `journal.tex` line ~1421 states accuracy is "unmeasured rather than measured-and-good." Genuinely unmeasurable without human adjudication — kept OPEN per the explicit carve-out. |
| RED from a real authors'-repository execution | RESOLVED | OPEN — fixtures only | RESOLVED | RESOLVED | `tests/test_autonomous_review_e2e.py` drives discovery→address→identity→authorization→reconciliation→RED end to end on a synthetic fixture; every real execution in the v2 run was `SYNTHESIZED_DIAGNOSTIC`. `journal.tex` states "No process launched" honestly four times rather than implying otherwise. |
| Test-suite health at current working-tree HEAD | RESOLVED | N/A | N/A | **OPEN** | **History, because a status with no trail is not verification.** Three full-suite runs of `pytest tests -q -m "not docker and not network"` were taken across this document's revisions, each against a tree that kept moving under active multi-agent edits, and each is reported rather than the middle two discarded: (1) `1 failed, 2274 passed, 7 deselected` under `-m "not network"` — `test_scientific_taxonomy.py`'s `unchecked_central` regression, since fixed (`taxonomy.claim_was_checked`); (2) `1 failed, 2262 passed, 19 deselected` — the preflight `KeyError: 'corpus'` regression, since fixed (see the preflight row); (3) a transient `2 failed` in `tests/test_review.py` whose cause could not be cited because the file was rewritten again before it could be read, followed immediately by (4) **2269 passed, 19 deselected, 0 failed**, the current and authoritative state. Re-marked RESOLVED on run (4). ZIP stays OPEN: no rebuild has happened against this green state yet (see the currency row). |
| `dist/REFEREE_final_source.zip` currency against the working tree | N/A | N/A | N/A | **OPEN** | **Re-checked.** The stale build this row first reported (`git_revision: 8779191`, 12:33 UTC) has been superseded: the manifest now reads `git_revision: 334e663...` (current HEAD) and `generated_at_utc: 2026-09-16T12:50:44Z` (254 files), because `tools/make_release_zip.py` walks the working tree directly rather than `git show`-ing a commit. But `HANDOFF.md` — an allow-listed top-level file — was modified again at 18:23:39 local, three minutes after that build finished, so even this rebuild is already behind the tree it was supposedly drawn from. The pattern repeats because the tree is still moving; this row settles only once a rebuild lands after edits stop, which is what the PENDING acceptance-test row below actually tests for. |
| **PENDING** — Fresh eight-paper rerun (`runs_final_2026-09-16/`) | RESOLVED (script ready, and the controller no longer skips F/G/H) | **PENDING** | **PENDING** | **PENDING** | `tools/final_run.sh` opens every gate deliberately (network, synthesis, auto-audit, grading, substantive verdict, claim links, artifact review, literature search+review, validation design, install, repo-exec, container backend) and runs the 8 papers sequentially. As of this writing only 3 of 8 project directories exist (`5993d35ff0996b52`, `acl`, `sanchez24a-icml`); `ACl.log` shows mid-ingest output and `sanchez24a_ICML.log` is 0 bytes. **What settles this row** — now four things: (1) all 8 papers reaching a terminal state in a fresh `corpus.json` and the funnel/coverage/route-exhaustion numbers re-measured; (2) with the `controller._phase_probe` fix (new row above) now in place, this is the first run where F/G/H are reached BY THE CONTROLLER rather than by a direct stage call, so their numbers are not just newly folded in but newly authentic; (3) `manuscript/journal.tex`'s own self-description at line 552-558 — "none of their measurements are folded into the funnel, coverage or exhaustion numbers that follow," referring to `runs_final_codex_v2_2026-09-15` — becomes false the moment this rerun's numbers are folded in, and must be rewritten; `check_journal_claims.py`'s `RUN` constant must be repointed to match; the D and E `% TODO(numbers)` markers in `journal.tex` (lines 397-398, 539) need filling from this run too. (4) `manuscript/tables/j_corpus_full.tex`'s **Final state** column must be regenerated against the fresh run's own terminal dispositions. No number from this run is stated anywhere above — none exists yet. |
| **PENDING** — Final ZIP acceptance test | N/A | **PENDING** | N/A | **PENDING** | No acceptance-test script exists yet (`tools/make_release_zip.py` builds and hashes but does not verify). A rebuild landed at `git_revision 334e663` (2026-09-16T12:50:44Z UTC, 254 files) but the tree kept moving after it (see the currency row above) — `HANDOFF.md`, the preflight gate, the controller probe fix, and the D/E manuscript drafts all postdate it. The working tree itself is now green (**2269 passed, 19 deselected, 0 failed**, re-verified), which it was not at every point in this document's own revisions. **What settles this row**: a final rebuild landing after edits genuinely stop, then extracting `dist/REFEREE_final_source.zip` into a clean directory, installing `requirements.txt` there, and running `python -m pytest tests -q` (or `tools/loop.py`) inside the extracted copy to confirm the source release is self-contained and green on its own. No result is recorded here — the number goes in when that run lands. |

---

## What remains genuinely open

Filtered of anything already resolved, or resolved-with-a-disclosed-departure, what is left
is a short list, and each item is open for a stated, specific reason rather than because
time ran out:

- **The human comparative-usefulness arm has zero participants.** The protocol is frozen
  and the baseline arm is real; no human data exists anywhere, and none is claimed.
- **No adjudicated ground truth exists for this corpus**, so precision, recall and accuracy
  are unmeasured, not measured-and-good, everywhere they are mentioned.
- **A bounded prior-art search cannot establish novelty**, by construction
  (`NOVELTY_ESTABLISHING_AUTHORITIES` is the empty tuple) — the capability the audio asked
  for is built, measured, and — as of the latest revision — the manuscript now says both
  halves correctly: the search runs, and it cannot establish novelty. What remains open is
  the fact itself, not the manuscript's description of it.
- **RED from a real authors'-repository execution, and a settled focused-validation
  comparison, are both proven on fixtures only** — the mechanisms are complete and tested;
  no real paper in the evaluated corpus has yet supplied an execution that qualifies.
- **Two of the five post-closure capabilities (D, full-paper reading; E, the claim/evidence
  graph) are drafted in `manuscript/journal.tex` but not complete.** F, G and H were
  rewritten to match the shipped code and are closed. D and E now have real prose
  (a reading-plan paragraph with its three isolation properties; a claim-link subsection
  with the endpoint-verified/structurally-bound split) but each carries its own
  `% TODO(numbers)` marker where the measured figures
  (`reader_visible_fraction: 1.000`; 76 proposed / 63 endpoint-verified / 0 structurally
  bound) still need to go — both checkers pass with the drafts in place. Held OPEN because
  unfinished is not the same as closed.
- **The batch preflight check is wired into `review_papers` and independently verified**:
  `tests/test_preflight_gate.py` (6 tests) confirms a real duplicate batch is refused with
  zero project directories allocated, and an unreadable input no longer refuses the whole
  batch. This is no longer open. The ZIP has not been rebuilt against this fix yet.
- **A second, more consequential defect was found and fixed in the same window**:
  `controller._phase_probe` was skipping the only call site for static artifact
  inspection, the bounded prior-art search and the focused-validation design on every
  paper where they were the only routes left — most papers. The fix is verified (a
  throwaway spy test confirms the call now happens); what it means for the existing F/G/H
  measurements is that they describe those routes correctly in isolation but overstate
  what a real `run.py review` had been doing, downgrading F and G's evaluation marks to
  PARTIAL above. Its own evaluation is PENDING on the fresh rerun.
- **The working tree is green as of the most recent run** (2269 passed, 19 deselected, 0
  failed) after three earlier snapshots each caught a real, different regression in
  flight — the taxonomy fix, the preflight fix, and a transient failure in
  `tests/test_review.py` that resolved itself before its cause could be cited. The release
  ZIP has not been rebuilt against this green state, and stays OPEN for that reason alone.
- **The fresh eight-paper rerun and the final ZIP acceptance test are both in flight**, and
  this document states what will close each rather than guessing a result.

---

## Corpus provenance — operator-supplied, with the evidence for and against

The manuscript states that **five of the eight papers were drawn from the 200-paper random
sample and three from the twenty ICML 2024 papers of PaperBench**. That is the operator's
account, reaffirmed after the contrary evidence below was put to them. It is recorded here
so the disagreement is on record rather than only in a conversation.

**What supports it.**

* Exactly three of the eight are ICML 2024 papers — `SAPG: Split and Aggregate Policy
  Gradients`, `APT: Adaptive Pruning and Tuning Pretrained Language Models`, and `Stay on
  Topic with Classifier-Free Guidance`. PaperBench draws its twenty replication targets
  from ICML 2024, and none of these three appears anywhere in the 3,408 PDFs of the
  sampling collection. The PaperBench three are identified with confidence.
* **Three of the remaining five are BYTE-IDENTICAL to PDFs in the sampling collection**
  (`data/raw/pdfs`), matched on SHA-256: `acl` = `ACL/2026/6c81da96832ccf4e.pdf`,
  `cvpr` = `CVPR/2025/0d1845f113337e6a.pdf`, `iclr` = `ICLR/2024/2dbc33b8938665c3.pdf`.
* An earlier draft of the manuscript (`old and outdated manuscript.pdf`, §6.1) independently
  states the same structure for the seven-paper corpus of the time: *"four randomly selected
  from that 200-paper sample, together with three of the 20 papers in PaperBench"*.
* In the pre-re-extraction screening database (`reproducibility.db.bak-2026-08-29`), three
  of those papers stood at screening stage `verified` and one at `triaged` — at or near the
  furthest stage — where the current database has them at `extracted`/`screened`.

**What does not support it, and must not be quietly dropped.**

* **None of the eight appears in `data/exports/sample_200.csv`, and none in the 387-row
  `eligible_papers.csv` the sample was drawn from.** In the current exports they sit in
  `review_required.csv` (`cvpr`, `acl`, `5993d35ff0996b52`) and `excluded_papers.csv`
  (`iclr`).
* The sampling manifest records the 200-paper draw as executed `2026-09-01`, *after* the
  `2026-08-29` re-extraction that reset those papers' screening stage. The sample as drawn
  could not have contained them.
* The operator's own account describes some of the papers as taken *while* the eligible pool
  was still being built — that is, before the sample existed.
* Two of the five (`0c06a98d7c818f6f`, `5993d35ff0996b52`) are byte-identical to nothing in
  the sampling collection.

**The reading that fits all of the above** is that the five were taken from the screening
and sampling pipeline at various points during its construction, and that a later
re-extraction and re-draw left the current exports no longer reflecting the state they were
taken from. That is a provenance-recording gap, not evidence of a different corpus.

**What this does NOT change.** Nothing in this article's conclusions rests on the sampling
claim. Eight papers chosen to span review paths support a systems evaluation and support no
prevalence estimate, whichever account of their provenance is correct — and that limitation
is stated independently in the Limitations section and in Figure 4's own caption.
