# v4 final report — performance gap, de-triage, and freeze readiness (2026-09-22)

This closes the loop opened by the governing instruction: verify v4 against the reference
implementation on a real paper with claim-by-claim rigor, fix only demonstrated failures,
remove the RED/YELLOW/GREEN triage, and report whether v4 can now replace the old
implementation. Full detail on the comparison methodology and every claim independently
checked against the paper is in `docs/V4_PERFORMANCE_GAP_ACL_2026-09-21.md`; this document
summarizes it plus the de-triage work that followed.

## 1. Performance-gap finding

Compared v4 against the reference implementation (tag `reference-implementation-2026-09-20`)
on the real FinChain/ACL paper, twice: once reusing the existing corpus report (delegated via
`SESSION_SUBAGENT`), once fresh through the identical `--auto-audit --auto-grade` CLI path v4
uses (`CLI_SUBPROCESS`) — the second run exists specifically to remove the delegation-mode
confound the first comparison had.

Every claim in both reports was checked directly against the actual PDF (Table 2, Tables
5-8/Appendix D, Table 3, Table 4, Appendix B). All were accurately grounded. One genuinely
sharp finding — an untuned, same-size baseline (Qwen-2.5 Instruct) beating both models the
paper cites as fine-tuning success stories, on both its headline metric and FAC — appeared
via independent grader reasoning in the reference runs but not in v4's. Every mechanical
hypothesis for why (context truncation, weaker prompts, decomposition losing cross-section
info, evidence missing from the grader's context, discovery/grading suppression, model
routed to a weaker tier) was checked directly and ruled out: lens prompts are byte-identical
between old and new code (diffed directly, ~29 lines of harness-mechanics wording only),
`reader_visible_fraction` is 1.0 in both, `CANDIDATE_CAP` grading behavior is identical, the
full Table 2 including the relevant row is confirmed present in both grade prompts, and both
runs use the same `CLI_SUBPROCESS` delegation at the same declared `sonnet` model tier.

**Conclusion: no demonstrated architectural regression.** The raw finding-count difference
traces to ordinary model sampling variance on identical inputs — confirmed by inspecting
v4's own raw lens output directly, which shows its lenses simply proposing different
candidate concerns on this run, not being blocked from seeing something. Per the governing
instruction, no pipeline code changes were made on this evidence, and none were warranted.

## 2. What changed: de-triage

Independent of the gap analysis, per an explicit, unconditional instruction: **the global
RED/YELLOW/GREEN triage and the binary RED/GREEN paper verdict were removed from the
architecture and the report.** REFEREE is a reviewer copilot, not an acceptance-decision
system.

- `harness/report.py`'s `overall_verdict()` (binary RED/GREEN) and `triage()`
  (RED/YELLOW/GREEN) were deleted outright.
- `claim_status()` (VERIFIED_FAILURE / VERIFIED_SUPPORT / NOT_VERIFIED — an epistemic state,
  never a colour) is unchanged and remains the substantive decision function.
- `harness/decide.py`'s `derive_disposition()` — pre-existing, computed by every review
  already, simply never surfaced to a reader before — is now the sole paper-level routing
  signal, over the 9 `PAPER_DISPOSITIONS` values: `STOP_MATERIAL_FAILURE`,
  `BLOCKED_SPECIFICATION`/`_ARTIFACT`/`_RESOURCES`/`_METHOD`, `PASS_TO_HUMAN_UNRESOLVED`/
  `_CONCERNS`/`_CLEAN`, `NOT_REVIEWED`.
- Every finding, severity, evidence class, provenance label, verification/reproduction
  state and unresolved question is unchanged — only the colour layer above them is gone.
- Fields deleted: `EvalReport.verdict`/`.verdict_reason`/`.triage`/`.triage_reason`,
  `CaseState.verdict`, `schema.TRIAGE_LEVELS`, `schema.VERDICTS`, `ReviewOutcome.triage`
  (set but never rendered — dead weight). `EvalReport.verdict_if_cell_backed_only`/
  `.verdict_if_lens_severity_only` were renamed to `claim_status_if_cell_backed_only`/
  `claim_status_if_lens_severity_only` and now hold claim_status values.
- The reviewer-facing report's "triage for routing: **YELLOW**" line became "disposition:
  **{report.disposition}**"; the machine report's 🔴/🟢 badge and "Paper decision: RED/GREEN"
  row became "Disposition: {report.disposition}". `run.py dossier`/`evaluate` lost their
  🔴🟡🟢 badges; per-paper headings are the plain disposition string, and the corpus rollup
  key is `"dispositions": {d: n for d in PAPER_DISPOSITIONS}`.
- The CONTESTED mechanism (`verdict_contested`, exit code 3) is unchanged in behavior — it
  still flags sharp disagreement between the model's independent whole-paper read and the
  deterministic conclusion, now phrased against claim_status/disposition instead of a colour.
- `CLAUDE.md` invariants 8 and 17 reworded to state the identical rules in
  disposition/claim_status vocabulary; every other invariant and all historical
  corpus-measurement prose left untouched.

Commit: `fea0d22`.

## 3. Verification (no new test suite)

- `python -m harness.report` / `.schema` / `.pipeline` / `.summarize` — all self-checks pass.
- A real end-to-end review (`run.py review --paper papers/ACl.pdf --auto-audit --auto-grade`,
  same gates as before) completed and rendered cleanly: `disposition : BLOCKED_METHOD` prints
  correctly in the terminal output and the `.review.md`/`.md` files; a full grep of both
  rendered files for RED/GREEN/YELLOW/triage turned up zero real hits (only substring
  false-positives inside unrelated words like "warranted").
- Zero regressions: stashed the de-triage edits, re-ran the 5 currently-collectible test
  files against clean HEAD, and got the identical 4 pre-existing failures
  (`test_self_checks.py`'s stale module-list assertions, unrelated to this change — they
  reference old pre-v4 module names and fail the same way with or without today's edits).
  95 of 99 collectible tests pass, same as before this pass.
- CLAUDE.md's rewritten invariants 8/17 and its new dated Known-Limitations entry were
  independently spot-checked against the actual code (`decide.derive_disposition`,
  `schema.PAPER_DISPOSITIONS`, `report.unearned_support_language`'s real signature) before
  being trusted.

## 4. Before / reference / after

| | reference (old code) | v4 before this pass | v4 after this pass |
|---|---|---|---|
| paper-level signal | binary RED/GREEN `overall_verdict` | binary RED/GREEN `overall_verdict` + RED/YELLOW/GREEN `triage` | 9-value `disposition` (`decide.derive_disposition`) |
| epistemic state | `claim_status` (3 values) | `claim_status` (3 values) | `claim_status` (3 values) — unchanged |
| findings/severity/evidence/provenance | full | full | full — unchanged |
| ACL run result (fresh, same CLI path) | 23 findings, 13 MINOR counted, `PASS_TO_HUMAN_CONCERNS`-equivalent | 14 findings, 10 MINOR counted | 15 findings, `BLOCKED_METHOD` (reconstruction target correctly refused, same as reference) |
| wall time (same paper, same gates) | 22.4 min | 19.2 min | ~same (not separately timed; architecture unchanged in this dimension) |
| model calls | 12 audit + 4 grade + 1 verdict = 17 | 12 audit + 3 grade + 1 verdict = 16 | same shape |

## 5. Final LOC

`harness/` (45 modules) + `run.py`: **30,538 production lines** — down from the pre-v4
reference implementation's 43,756 lines across 89 modules, and down further from the
32,229/32,174 lines reported at the end of the prior compression-and-verification pass (the
gap is the subsequent alignment/execute.py consolidation plus today's de-triage net -95
lines).

## 6. Remaining quality uncertainty

- **The test suite gap, unchanged from the last report.** 60 of 65 `tests/*.py` files still
  fail to import (stale references to pre-v4 module names). This was not touched — repointing
  60 test files would itself be the large self-authored-verification effort the governing
  instruction rejected. It remains the single largest gap between "verified by real execution
  and independent review" and "continuously self-checking."
- **The n=2-vs-n=2 grading pattern**, flagged in the gap doc: a sharp same-size-baseline
  argument appeared in both independent old-code grader calls but neither of v4's. The
  grade-prompt template is confirmed identical; the specific candidate text each grader saw
  differed (upstream lens-roll variance). Not large enough a sample to call systematic, and
  not actioned per the "don't change code without demonstrated failure" instruction — worth
  watching across future papers, not fixing now.
- **`test_self_checks.py`'s 4 pre-existing failures** (harness.agent, harness.report, and two
  generic tests naming pre-v4 module names) are a real, live gap in that specific test file's
  own currency — confirmed pre-existing, not introduced by this pass, but also not fixed by it.

## 7. Can the old implementation be replaced?

**Yes, with the same qualification as the last report, now narrower.** The core decision
logic (materiality/disposition, provenance ceiling, severity caps, evidence verification) is
verified equivalent-or-stronger than the reference implementation via real differential
execution and independent code review (prior pass) plus a fresh, confound-controlled
performance comparison finding no regression (this pass). The RED/YELLOW/GREEN removal is a
net simplification that reduces surface area (one fewer redundant decision axis, one
self-documented pre-existing bug — `CaseState.verdict` carrying an invalid "YELLOW" on real
past runs — retired along with the field it lived on) without touching any finding-level
substance. The one honest caveat carried forward unchanged: the automated test suite does not
currently run end-to-end, so any single review's output still deserves one human read before
being trusted unattended. Everything else checked out clean.
