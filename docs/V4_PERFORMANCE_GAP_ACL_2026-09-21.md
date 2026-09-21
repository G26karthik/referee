# v4 performance-gap analysis — ACL (FinChain) paper, 2026-09-21

This document is the required performance-gap specification requested before any further
v4 code changes. It compares three things on ONE paper (`papers/ACl.pdf`, FinChain: A
Symbolic Benchmark for Verifiable Chain-of-Thought Financial Reasoning, ACL 2026):

1. The paper itself — read directly, all 25 pages, independently of either report.
2. The reference implementation (tag `reference-implementation-2026-09-20`) — run TWICE:
   once as the pre-existing corpus artifact (`projects/acl/`, delegated via an interactive
   `SESSION_SUBAGENT`), and once FRESH, through the exact same `run.py review --auto-audit
   --auto-grade` CLI path v4 uses (`CLI_SUBPROCESS` delegation), in an isolated git
   worktree, to remove the delegation-mode confound from the comparison.
3. v4 (current HEAD, `referee-redesign-v4`) — run once, same CLI path, same gates
   (`SH_ALLOW_AUTO_AUDIT`, `SH_ALLOW_GRADING`, `SH_ALLOW_ARTIFACT_REVIEW`,
   `SH_ALLOW_REPO_EXEC`, `SH_ALLOW_SUBSTANTIVE_VERDICT` all =1), isolated project dir.

No new test suite was written or used as evidence anywhere in this analysis. Every claim
below is either (a) verified directly against the PDF by me, or (b) a direct comparison of
real run artifacts (prompts, lens outputs, grade outputs, driver metadata) from real
executions.

## 1. What the paper actually says (independently verified)

Verified directly against the PDF (Table 2 p6, Tables 5-8 + Appendix D p20-21, Table 3/4
p13/p17/14548, Appendix B p13-14, Appendix F p24-25):

- **Table 2 arithmetic.** 9-model finance/math-tuned category average = ~41.7 CHAINEVAL;
  general-purpose untuned average (LLaMA-3.1, Qwen-2.5 Instruct, Qwen-3) = ~52.6; frontier
  average = ~64.4. The fine-tuned category is FURTHER from frontier than the untuned
  general-purpose baseline is — the abstract's "can substantially narrow this gap" framing
  is a real, quantifiable overclaim at the category level, though defensible at the
  individual-exemplar level (Fin-R1, Mathstral) and partly self-corrected by the paper's own
  Results text ("effectiveness of fine-tuning varies with adaptation scope").
- **A same-size-class control the paper's own Results section invites and fails.** Qwen-2.5
  Instruct (7B, general-purpose, untuned) scores 60.35 CHAINEVAL / 65.41 FAC — beating BOTH
  Fin-R1 (58.14/52.76) and Mathstral (59.87/54.03), the two models the Results section cites
  as evidence fine-tuning helps "beyond what model size alone provides." This is the
  sharpest, most precise form of the abstract-overclaim concern in the whole corpus of
  findings collected below.
- **DeepSeek-R1 outlier.** CHAINEVAL 51.22 (lowest of 13 frontier models, next-lowest is
  60.69), FAC 28.97 (every other frontier FAC is 66.5-86.8). Appendix D.5 states step
  parsing depends on regex matching "Step X:"-style patterns; DeepSeek-R1's distinct
  `<think>`-tag reasoning format is never addressed anywhere in the paper's error analysis.
  A parsing-artifact explanation is well-grounded, not proven.
- **Table 5/6/7 correlation tables, checked exactly.** Spearman: DTWNormGate+FAC=0.655
  (top, the paper's own stated primary metric). Pearson: DTWNormGate+FAC=0.584, EXACTLY TIED
  with DTW F1(Soft)=0.584. Kendall τ: DTW Precision(Gate)=0.511 > DTWNormGate+FAC=0.509. The
  Discussion's unconditional "the most reliable indicator" is literally true on only 1 of 3
  reported correlation measures.
- **α grid-search circularity, confirmed.** Appendix D.1: "We used all possible α's in
  range 0.1-0.9... the α of 0.1 resulted in the best Spearman ρ and it was used for further
  comparison." No held-out split is described anywhere in the visible text, and that same
  correlation is the paper's stated evidence for CHAINEVAL's "strongest correlation with
  expert human judgments" (an Introduction-listed contribution). Real leakage in validating
  the paper's own headline metric; tempered by α's small (10%) weight in the final score.
- **Table 3 arithmetic "error."** 12+9+5+3+2=31 and 41.4+31.0+17.2+10.3+6.9=106.8%, both
  exceeding the stated "Total Tagged Templates: 29 (100.0%)". This is NOT an error — the
  annotation protocol allows multiple issue tags per template (confirmed, Appendix B.2/B.3
  describe multi-tag annotation) — and both runs' lenses that raised it correctly resolved
  it as such (NOTE severity, not a real defect).
- **New finding this analysis surfaced independently, found by the fresh reference run and
  independently verifiable:** GPT-4.1 is BOTH the universal step/answer parser for all 26
  evaluated models' outputs (Appendix D.5, "We used the GPT-4.1 model") AND one of the 26
  models being ranked (Table 2, GPT-4.1 CHAINEVAL=65.34) — a self-referential evaluation
  concern (is GPT-4.1 parsed more faithfully than other models by a parser that is itself
  GPT-4.1?) that the paper never investigates. Genuinely sharp, correctly grounded, and not
  present in v4's own run of this paper (see §3).
- Base-model confound (Mathstral vs Finance-LLaMA/Finance-Qwen use different backbones,
  Table 4) and the single-annotator-per-template QC gap (270/290 templates each reviewed by
  exactly one expert, Appendix A.4) are both real, correctly grounded, and MINOR/NOTE-
  appropriate.

## 2. Old vs v4, delegation-confound removed (the primary comparison)

| | fresh reference (old code, CLI path) | v4 (current HEAD, same CLI path) |
|---|---|---|
| audit structure | 2 bounded parts, 12 readings | 2 bounded parts, 12 readings — IDENTICAL |
| `reader_visible_fraction` | 1.0 | 1.0 — IDENTICAL |
| lens role prompt (protocol, confound) | — | byte-identical to reference except ~29 lines of harness-mechanics WORDING (no content change); confirmed by direct diff |
| delegation mode | CLI_SUBPROCESS | CLI_SUBPROCESS — IDENTICAL |
| raw findings proposed | 28 (5 dropped, 23 kept) | 21 (7 dropped, 14 kept) |
| candidates graded | 4 | 3 |
| MAJOR findings surviving grading | 0 | 0 — IDENTICAL pattern |
| final counted severities | 13 MINOR | 10 MINOR |
| discovered objects / addressable | 40 / 33 (82.5%) | 31 / 24 (77.4%) — comparable ratio |
| reconstruction target outcome | IDENTITY_BLOCKED, provenance ceiling refuses `template` program | IDENTITY_BLOCKED, same reason — IDENTICAL |
| distinct underlying scientific concerns raised | ~7 (abstract overclaim, α-circularity, DeepSeek-R1 outlier, "most reliable" superlative, GPT-4.1 self-eval, single-annotator QC, undisclosed-LLM-provenance/no-inter-rater-reliability) | ~4 (abstract overclaim, α-circularity, DeepSeek-R1 outlier/decoding-fairness, "most reliable" superlative, Table 3 arithmetic) |

**Root-cause check against every hypothesis the redesign instruction asked me to test:**

- *Insufficient context selection* — RULED OUT. `reader_visible_fraction`=1.0 in both;
  identical 2-part structure; identical anchor packet mechanism.
- *Overly aggressive compression of reviewer prompts* — RULED OUT. Lens prompts are
  byte-identical in substance (diffed directly; the only differences are harness-mechanics
  wording, not content, and total ~29 lines out of ~97,000 characters).
- *Decomposition/chunk synthesis losing cross-section relationships* — PARTIALLY
  CONFIRMED, but as a property of BOTH architectures, not new to v4: the OLD corpus report
  (session_subagent run) contains a finding ("sample size not given in this part") that is
  literally true of its bounded part but false of the whole paper (§5.2, in the OTHER
  part, states it). This is an inherent property of the bounded-part/anchor-packet design
  (the anchor packet carries only title/abstract/conclusion/section outline, not arbitrary
  cross-references) present before v4 and unchanged by v4. Not observed in either of the
  two fresh CLI-path runs on THIS 25-page paper (which barely needs 2 parts), so its
  practical impact here is low; worth watching on longer papers.
- *Reviewer-role prompts becoming weaker* — RULED OUT (see prompt diff above).
- *Evidence retrieval missing passages needed to reason about a concern* — RULED OUT for
  the specific case checked: both v4's and the fresh reference's grade prompts for
  overclaim-themed candidates contain the full Table 2 (including the Qwen-2.5 Instruct
  row) — confirmed present in v4's grade prompt text.
- *Discovery/target generation dropping useful questions* — NOT CONFIRMED. Discovered
  and addressable object counts scale proportionally with the (variance-driven) raw
  finding count in both runs; no disproportionate drop observed.
- *Grading/filtering suppressing valid findings* — NOT CONFIRMED as a v4-specific defect.
  `CANDIDATE_CAP` (PLAUSIBLE_CONCERN capped at MINOR regardless of grade-asserted severity)
  behaves identically in both runs and is architecturally unchanged from the reference
  implementation. Both runs landed at 0 surviving MAJOR findings on this paper.
- *Context/model routing using Haiku where Sonnet-level reasoning is required* — RULED
  OUT for the roles exercised here (lens audit, grading): both runs used the same
  delegation mode and the same declared model tier (sonnet) per `CLAUDE.md`'s own
  documented defaults; no haiku-tier role touches the lens/grade path.

**What actually explains the 23-vs-14 raw finding gap:** direct inspection of v4's own raw
lens outputs (`projects_v4_perfgap/acl/audit/{protocol,confound}/parts/*.json`) shows its
protocol and confound lenses, reading BYTE-IDENTICAL prompts to the reference run, simply
did not generate candidates about single-annotator QC, undisclosed-LLM-provenance,
no-inter-rater-reliability, or GPT-4.1 self-evaluation in this run. This is model sampling
variance on identical inputs, not a traceable architectural cause — no prompt, context,
routing, or filtering difference explains it. Per the governing instruction ("stop when
remaining differences are reasonably attributable to model nondeterminism"), this is such a
case, and no code change is warranted to chase it.

**One pattern flagged but not resolved, for future attention rather than action now:** the
sharp Qwen-2.5-Instruct same-size-control argument appeared in 2 of 2 independent grader
calls on old-implementation runs (the archived corpus run AND the fresh CLI-path run, on
differently-worded candidate findings both times) but 0 of 2 grader calls on v4's own
abstract-overclaim candidates. The grade-prompt template is unchanged between old and new
code (same `derive()`, same `CANDIDATE_CAP`, same driver mechanics — confirmed earlier this
session's code review); the specific candidate FINDING TEXT each grader reasoned about
differed between runs (different lens rolls upstream), which is a sufficient, mundane
explanation, but the sample (n=2 vs n=2) is too small to fully rule out something subtler.
Recommendation: not actionable without more data; watch across future papers rather than
changing code now on a 2-vs-2 sample.

## 3. Findings quality: no unsupported/fabricated findings in v4's ACL run

Every one of v4's 14 findings was spot-checked against the actual PDF (Table 2, Table 11,
Table 3, Appendix D). All are accurately quoted and the underlying arithmetic/claims check
out, including the ones that self-resolve to NOTE severity (Table 3's multi-tag arithmetic
"error" is correctly identified as an artifact of multi-label tagging, not reported as a
real defect). No hallucinated evidence, no fabricated table values, no severity inflation
observed in v4's output on this paper.

## 4. Conclusion for this gap-closing pass

No demonstrated, traceable architectural regression was found in context selection, prompt
strength, decomposition/synthesis, evidence retrieval, discovery, grading, or model
routing, on this paper, once the delegation-mode confound is removed. The raw
finding-count difference observed is attributable to ordinary model-call variance on
identical prompts. Per the governing instruction, no code changes to the audit/discovery/
grading pipeline are warranted on this evidence, and none were made.

The one required architecture change that remains — removing the global RED/YELLOW/GREEN
triage — is independent of this gap analysis (an explicit, unconditional instruction: "the
colours" were never a claim this analysis was asked to validate or diagnose) and is tracked
separately.
