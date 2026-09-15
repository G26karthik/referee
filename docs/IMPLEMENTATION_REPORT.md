# Implementation report — the first-review layer

What changed, what was preserved, what is now genuinely autonomous, and what is not.
Written to be checkable: every claim below names the module, the test, or the artifact
that stands behind it.

**Tests: 858 before → 980 after, all passing.** The 858 is the count at the moment the
`discover` phase was introduced (845 passing, 13 failing on the phase change); every
pre-existing test either passes unchanged or was updated because the property it pinned
genuinely moved — those are listed in §4.

> **§0 — the scientific-taxonomy layer (second pass).** A later revision moved the
> review's primary output off the RED/YELLOW/GREEN triage and onto a set of scientific
> findings with resolution states. What that turned into is `harness/taxonomy.py`, a
> widened `ReviewQuestion`, a first-class experiment-necessity axis, a six-term funnel and
> a reorganised reviewer report. It is described in §7, together with the six defects and
> three overclaims the accompanying self-review found. `tests/test_scientific_taxonomy.py`
> is the regression file for all of it.

---

## 1. What changed

### 1.1 New modules — all pure, all self-checking

| module | what it decides | why it is a table or a total function |
|---|---|---|
| `harness/claims.py` | where in the paper a claim lives | a lens supplies a QUOTE, the harness mints the ADDRESS; refuses on anything but exactly one occurrence |
| `harness/questions.py` | what would settle a concern | templated on the finding's own closed-vocabulary self-classification; reads no number, no metric, no paper identity |
| `harness/discovery.py` | what is checkable, and how central | centrality from structure (abstract, cited address, self-checking composition), never from a model |
| `harness/priority.py` | which target to pursue first | lexicographic; `score`'s signature admits vocabulary strings and booleans only |
| `harness/planner.py` | whether an experiment is justified | ditto; decides whether to TRY, while `authorize()` still alone decides whether it may RUN |
| `harness/ledger.py` | the traceable chain per conclusion | copies from artifacts; derives nothing |
| `harness/evaluation.py` | system metrics over a corpus | counts files; reports no number the corpus cannot ground |
| `harness/stages/discover.py` | the phase that runs the five above | writes `discovery/targets.json` |

### 1.2 The three structural gaps, closed

**Target discovery.** `stages/probe.build_spec` used to pick a target as a side effect —
`[f for f in rank(findings) if f.verifiable_by_experiment][0]` — one target, from an
unchecked model boolean, ordered by a *report display* sort, with no fallback when it
blocked. That line survives only as `finding_target`, reached when no target set exists on
disk. What decides now is `DiscoveredObject.harness_addressable`.

**Target prioritization.** Its own module and its own function, with cost at the bottom of
the key order so a cheap peripheral target can never outrank an expensive central one.

**Evidence graph.** `TargetSet` + `TargetOutcome` + `CaseLedger`. Every target keeps
independent state; a blocked target ends that target and not the paper.

### 1.3 Addressable claims beyond table cells

`^T\d+:r\d+:c\d+$` in three files was the whole reason a prose-stated result was
unreachable. The address grammar now admits `table_cell`, `prose_claim`, `figure`,
`equation` and `section_span`, and `claims.resolve` re-derives each from the parsed paper.
`parse_quantity` refuses every span reporting more than one number, and re-evaluates a
stated multiplicative composition rather than trusting it.

`experiment_id` gained a prose branch (`_prose_experiment`, `_prose_metric`,
`_prose_configuration`) so a prose-stated COUNT can bind to a command that emits one.
`ProbeSpec` and `Reconciliation` gained `claim_ref` / `claim_kind` / `target_id`;
`table_ref` stays cell-only so nothing keyed on a cell address silently starts matching
prose.

### 1.4 Target-aware metric binding

`local_exec.parse_metric` prefers a target-bound output object → a self-naming one → a
structured one → a bare bound key, and **refuses when the winning tier disagrees with
itself**. `json_metric`'s last-JSON-wins scan is now documented as diagnostic-only and is
not on the execution path.

One narrow addition to `reconcile`: a metric identity established as a `count`, measured
identically across more than one seed, is compared against the printed precision instead
of a zero-width 2σ band. A count has no seed-to-seed distribution; for a stochastic metric
zero measured variance means the seed never reached the model, and that case still refuses
unchanged.

### 1.5 Two report layers, and a three-valued triage

`reports/<pid>.review.md` is the reviewer's short report — 6.3-8.7 KB over the
current corpus, two to three printed pages — bounded by
construction. `reports/<pid>.ledger.json` is the trace. `stages/report.triage` folds
RED/YELLOW/GREEN over typed evidence; RED is exactly the pre-existing binary verdict, and
the split happens inside the old GREEN.

### 1.6 What running it against a real paper with the gates OPEN exposed

Four defects, all found by pointing the finished pipeline at FinChain with
`SH_ALLOW_REPO_EXEC` on and watching where it stopped.

- **A multiplication sign is not `×`.** FinChain's `58 × 5 × 10 = 2,900` arrives from the
  PDF as `58 ϵ 5 ϵ 10 = 2,900` — the embedded font encodes the operator as a glyph the
  text layer reports as a Greek lunate epsilon. A fixed operator list refuses the real
  composition claims the prose path was built for. `claims._composition` now has two
  tiers: a recognised operator is admitted either way, and an unrecognised single-glyph
  separator is admitted **only when the operands multiply out to the printed total** —
  because where they do not, a different operator and a genuine arithmetic error are
  indistinguishable, and choosing the second would accuse a paper of bad arithmetic on
  the strength of a font encoding.
- **A composition is the tail of a sentence, not the whole of one.** Requiring the entire
  left-hand side to alternate rejected *"...by sampling 10 instances per template ...,
  yielding 58 × 5 × 10 = 2,900"* over the leading `10`. The rule is now the maximal
  alternating run ending at the equality: strict where it matters, and it stops demanding
  that authors write no prose.
- **Targets with no comparable quantity were being routed to execution.** The
  focused-validation route did not require a parsed quantity, so the top-priority
  executable target on FinChain was a claim with nothing to reconcile against. The
  reconciler refuses such a run correctly — but only after it has been spent.
- **`--force-probe` rewound past `discover`.** It resumed at `probe`, so a forced re-run
  consumed the *previous* run's target set. A discovery fix therefore had no effect on a
  forced re-run, which is the stale-target defect `ProbeSpec.written_by` exists to
  prevent, reintroduced one level up at the phase boundary.

And one deliberate relaxation, which is a correction rather than a weakening: capability
required the repository to accept a `--seed` the harness would not be passing. For a
deterministic count there is no seed, so the check was about an invocation that would
never be made. It is now conditioned on whether a seed is actually sent.

Opening the gates then exposed two more, both of the same family — a rule that measured
the harness's own configuration rather than the paper:

- **The triage flagged a paper because our own probe could not answer.** Restricting the
  YELLOW flag to targets that were *attempted* and settled nothing made all seven papers
  YELLOW again the moment execution was enabled, because what ran for most targets was a
  synthesized diagnostic the provenance ceiling never entitled to settle anything. Its
  inconclusive result says nothing about the paper. `unresolved_central` now additionally
  requires an admissible provenance. This is the *second* time this defect appeared on
  this corpus, and the second time an all-seven-identical column was what revealed it.
- **144 targets through one command counted as 144 experiments.** On APT every printed
  number from one benchmark under one metric was independently judged to justify an
  execution, and all of them routed through the same entrypoint — a list, not a plan, and
  one that pushed the execution-trigger rate back up by sheer count. Targets are now
  grouped by (route, experiment, metric) and the highest-priority member of each group is
  pursued, the rest reported alongside it. This is a de-duplication, not a budget: nothing
  is dropped for being expensive.

### 1.7 Two real bugs found and fixed on the way

- **Target id collision.** `_target_id` was (kind, address); two findings citing the same
  cell produced the same id, so one target appeared twice under one id and a lookup
  returned whichever was last. Now suffixed on collision only.
- **`drive_all` never re-derived a finished case.** A batch re-review returned whatever
  reports were on disk — including after the code that produced them had changed, and
  including with `--force-probe` — while re-running the same papers one at a time
  re-derived them. Two entry points to one operation disagreeing about whether it
  re-derives is the kind of difference nobody notices until a batch silently reports the
  previous release's verdicts.

---

## 2. Invariants preserved

Every invariant in `CLAUDE.md` §Immutable invariants holds unchanged. The ones most at
risk from this work, and what keeps them:

| invariant | still holds because |
|---|---|
| 1 — evidence re-verified | prose is no exception: `mint` refuses an ambiguous quote, `resolve` re-reads the span (`test_unverified_prose_cannot_be_promoted_to_verified_evidence`) |
| 2 — a lens cannot certify itself | untouched; `verifiable_by_experiment` was demoted, not promoted |
| 3 — provenance ceiling | keyed on provenance, which knows nothing about address kind (`test_prose_and_table_references_obey_the_same_provenance_ceiling`) |
| 4 — only `authorize()` may permit execution | the planner decides whether to TRY and deliberately re-implements none of identity, capability, commit or resources |
| 6, 7 — infeasibility is not failure | `TargetOutcome.establishes_failure` is a property on the type; every blocked disposition fails it (`test_a_blocked_target_can_never_establish_a_failure`) |
| 8 — RED is a table, not a count | `triage` is a strict projection: `test_triage_red_is_exactly_the_binary_verdict` sweeps it |
| 10 — no paper-specific logic | `priority.score` and `planner.classify` signatures admit vocabulary strings and booleans only, asserted in their own tests |
| 11 — nothing may raise a severity | nothing added here touches severity at all |

Three invariants were **added** (15–20 in `CLAUDE.md`), covering what may open the
execution path, per-target independence, the ban on a colour that tracks the harness's own
configuration, positional metric coincidence, the two report layers, and counted
escalation.

---

## 3. Tests added (66)

`tests/test_first_review.py` (47) — prose addressing and its refusals; the demotion of
`verifiable_by_experiment`; question generation reading no number; the trigger gate and
each named infeasibility; prioritisation ordering and determinism; multi-target
independence; target-aware metric binding including ambiguity refusal; ledger provenance;
the RED/YELLOW/GREEN fold; reviewer-report compaction.

`tests/test_autonomous_review_e2e.py` (17) — the acceptance path with no operator
choosing anything; a prose claim reaching a `ProbeSpec` and an identity binding; author
code missing a prose-stated total establishing RED, and matching it reproducing; one paper
with mixed target outcomes; an under-specified paper yielding YELLOW and never RED; the
two report layers staying two; and mutation-style attacks — wrong commit, fabricated prose
reference, malicious severity, unauthorized experiment, stale target state, and two JSON
metrics under one key.

`tests/test_controller.py` (+2) — a lens boolean alone no longer opens the execution path;
a shut gate does not colour a paper.

**Updated, not weakened** — `test_controller.py`'s phase-order test, pipeline-history
test, and abstention-class fixtures. The last needed a paper that *reaches* the probe now
that the gate is the planner's rather than a lens boolean; the fixture gained a repository
and an addressed number, which makes the test exercise the path it always claimed to.

---

## 4. What is genuinely autonomous now

Verified on real papers (`run.py evaluate`, seven papers):

- **Claim addressing.** 867 targets discovered, 809 structurally
  checkable, each with an address the harness re-derived from the parsed paper.
- **Question generation.** 48 review questions, from the findings'
  own classifications; 38 remain open and are handed to the
  referee as questions.
- **Scientific classification.** Every kept finding carries one of ten scientific classes,
  derived from its own closed-vocabulary self-classification:
  {"CONTRADICTION": 22, "UNRESOLVED_QUESTION": 20, "CONFOUND": 6, "OVERSTATED_CLAIM": 5, "MISSING_CONTROL": 4, "SPECIFICATION_GAP": 4, "MISSING_VALIDATION": 3, "PROTOCOL_ISSUE": 3}.
- **Prioritisation.** Deterministic and total; the ordering in `discovery/targets.json` is
  reproducible across runs.
- **Conditional escalation, with the terms kept apart.**
  61 of 867 targets judged to WARRANT an
  experiment; 12 actually launched a process
  (120 processes started);
  12 ran to a reconciliation; 20 settled a question about a
  paper on admissible evidence. 20 settled against
  the papers' own printed content with nothing running;
  206 stopped before execution with the gate that
  stopped each recorded.
- **Two-layer reporting.** 57.5 KB of reviewer reports
  against 1107 KB of machine
  artifacts.
- **Triage.** Mechanically folded, no model input, 5 YELLOW / 2 GREEN — printed under the scope of
  each review rather than as its headline.

Verified end to end on synthetic git fixtures, not yet on a published paper:

- **A prose-stated composition → address → quantity → identity binding → authorized
  execution → reconciliation → RED**, and the same chain reaching a reproduction.
- **Mixed outcomes on one paper**: a failed target, a reproduced target and a blocked one
  reported together.

---

## 5. What remains unavailable

| capability | status |
|---|---|
| the prose path on a published paper | **achieved up to identity.** With gates open, FinChain's prose composition is discovered, addressed, arithmetically re-verified, prioritised, and pursued against a commit-verified checkout. `experiment_id` then refuses — *"none of the 3 advertised command(s) emits a count"* — which is true of that repository and is the guarantee working: producing the benchmark means looping all 58 generators and no advertised command does. The harness will not write that loop |
| RED from an autonomous execution on a published paper | not achieved. Proven end to end on git fixtures (`tests/test_autonomous_review_e2e.py`), in both directions |
| prose identity for anything but a COUNT | not implemented, deliberately: a prose-stated accuracy has no column header, no basis and no baseline row to bind against |
| novelty / prior-art checking | architectural only. `LITERATURE_SEARCH` is in the route vocabulary and nothing implements it. No novelty conclusion is produced |
| reviewer accuracy, precision, recall | not measurable: the corpus carries no adjudicated ground truth. `harness/evaluation.LIMITATIONS` says so inside the artifact |
| scalability | not established. Seven papers is an initial systems study |
| independent reimplementation sealed end to end | still eligibility-only (`harness/reimplement.py`) |

---

## 6. Manuscript claims weakened or removed

The manuscript was rewritten around the system contribution. Statements that changed
because the evidence did not support them as written:

- **The central result is no longer "1 RED / 6 GREEN".** That is an output distribution,
  not a contribution. The primary table is now what the reviewer found, by scientific
  category, generated from the reports by `manuscript/tables/make_findings_table.py`.
- **The FinChain generator result is attributed to a separate, purpose-written run**
  everywhere it appears, and the manuscript states that it "is \emph{not} a
  pipeline-emitted result". The pipeline's own status for that claim is stated beside it:
  the composition target is discovered, addressed, prioritised, planned and pursued
  autonomously, and its reconciliation is INCONCLUSIVE because the only program that ran
  under it was a synthesized diagnostic the provenance ceiling does not admit.
  `check_claims.py` asserts the attribution, the INCONCLUSIVE status, and — against
  `discovery/targets.json` itself — the target's necessity, evidence state and launch
  count.
- **No claim of scalability.** §Limitations states that seven papers establishes nothing
  about it, and that the 387-paper eligible pool and 200-paper sample exist for an
  evaluation that has not been run.
- **No claim of representativeness.** §Corpus states the seven papers are not
  representative of the literature, the venues, or the 200-paper sample.
- **The PaperBench overlap is explicitly contextual, not comparable.**
- **"Only one paper's repository could be inspected" was wrong and was corrected.** Four
  of the seven advertise a repository and all four were statically inspected; one produced
  findings. Finding nothing in three checkouts is a weaker statement than finding six
  defects in the fourth, and the manuscript now says so.
- **An earlier triage rule was reported as a negative result rather than quietly fixed.**
  Flagging every unsettled central target made all seven papers YELLOW because the gates
  are shut by default — a colour tracking the harness's configuration. §Evaluation reports
  the defect, the corrected rule, and the corrected distribution.
- **Sampling numbers are pinned in `check_claims.py`** as supplied constants, because the
  upstream pooling and screening pipeline is not part of this codebase and nothing here
  can re-derive them. An edit that changes 387 to 400 fails the checker.

`python manuscript/check_claims.py` — every check passes, and the checker now also
asserts the three overclaims of §7.6 against the artifacts rather than against
anyone's memory of them.


---

## 7. The scientific-taxonomy layer

### 7.1 Why a colour was the wrong primary output

A referee's output has three independent dimensions, and a single label of any kind
destroys two of them:

| axis | field | a property of |
|---|---|---|
| what KIND of problem this is | `scientific_class` — 10 values | the paper's argument |
| was the question settled, by what | `resolution_status` — 5 values | the review process |
| what the evidence route produced | `evidence_state` — 10 values | the world |

A CONFOUND settled from the paper and a CONFOUND left open by a missing artifact are the
same kind of problem in opposite states. `harness/taxonomy.py` derives all three from
closed vocabulary alone — no number, no metric name, no paper identity — and
`EVIDENCE_ABOUT_THE_PAPER` names the only four evidence states that say anything about the
paper at all. The triage still exists, is still deterministic, and is now printed under
`## Scope of this review`, because it routes attention and is not the result.

`scientific_class` is deliberately NOT a parameter of `grading.derive`. "A CONFOUND is
always MAJOR" is the same defect as "no variance = MAJOR": it decides impact from
something that is not impact. Keeping it out of the signature makes the rule inexpressible
rather than merely absent (`test_scientific_class_is_not_an_input_to_severity`).

### 7.2 The question became the unit

`ReviewQuestion` now carries its own `claim_ref` (minted by the harness, never copied from
a lens), `source_finding_ids`, `possible_resolution_routes`, `resolution_status`,
`evidence_state`, `evidence_refs` and `conclusion`. `stages/discover.sync_questions` is a
FOLD over the target set that writes every one of those, and it runs twice — once when
targets are planned and again from `stages/probe` once anything has executed. It is
idempotent, so a question can never keep a resolution the evidence behind it has lost.

`questions.derive` merges by `(question text, address)`: two lenses reaching the same
question about the same number is ONE question with two sources, and the merge takes the
MAXIMUM materiality its sources asserted and never more.

### 7.3 Experiment necessity became a decision, and "no" became a success

`EXPERIMENT_NECESSITY` has seven values. `NO_EXPERIMENT_NEEDED` is a successful review
outcome and is never counted beside "we needed one and could not run it" — and it settles
nothing on the evidence axis, because declining to run something does not close a
missing-control question. `PlanDecision` gained the why-YES half beside the `gates` that
were already the why-not half: `why_material`, `paper_only_insufficient_because`,
`inspection_insufficient_because`, `competing_explanations` (carried verbatim from the
finding, never invented) and `expected_observation`.

`INFEASIBLE_SPECIFICATION` was split. It now means only "the PAPER does not specify enough
to build the experiment"; `INFEASIBLE_ADDRESSING` means "WE could not build an address for
the claim". The first is a limit of the authors' method section and the second is partly a
limit of our own extraction.

### 7.4 The funnel is six terms from six artifacts

```
discovered → checkable → warranting an experiment → launched → completed → resolved
  objects      objects           plans               run record  outcomes   outcomes
```

`TargetOutcome.launched` is copied from `ProbeResult.executions` — the runner's own count
of processes it started — and is never inferred from a disposition. A target this run's
budget did not reach is `BUDGET_DEFERRED`, which is neither a refusal nor a block.
`probe_stage_seconds` is named for what it measures.

### 7.5 Six defects this layer's self-review found

| defect | why it mattered |
|---|---|
| `INFEASIBLE_SPECIFICATION` was one label for two refusals | a limit of our extraction read as a gap in the authors' method section |
| the elision counter summed `len - room` raw | a one-finding category contributed −2 and silently cancelled a truncated one, suppressing the "…and N more" line |
| the scope funnel read `eff["funnel"]` | only `evaluation.per_paper` nests it; the ledger uses flat keys, so the report printed six zeros beside a ledger holding the numbers |
| `INFEASIBLE_ADDRESSING` mapped to `ARTIFACT_BLOCKED` | a paper whose repository WAS cloned was told "no usable artifact reached this question"; now `ADDRESSING_BLOCKED` → `EXTRACTION_LIMITATION` |
| `dossier.badge` was applied to the triage | every YELLOW paper rendered as "⚠️ STALE — re-run this paper" |
| `prompts/audit.LENSES["model"]` was inert | the declared opus/sonnet panel diversity never reached the invocation |

### 7.6 Three overclaims caught against the artifacts

1. **"escalates to execution for 61."** 61 targets had a PLAN that warranted an
   experiment. That is a judgement, and the sentence read as a launch count.
2. **"Execution accounts for 1,211 seconds."** That is `probe.seconds` — the wall time of
   a stage covering acquisition, static audit, planning and every gate.
3. **What actually ran on this corpus.** All 28 lens files record
   `written_by: manual_accept` and `tool_policy: unrecorded (general-purpose subagent, no
   sandbox restriction, full tool access incl. WebSearch)`. Per-lens isolation held; the
   sandbox, the `--allowedTools` restriction and the per-lens model selection did not
   apply. And `grade_coverage` shows **0 of 15 serious candidates graded** with **no
   whole-paper assessment on any paper** — the grader and the assessor are capabilities
   here, not results. The harness recorded all of this correctly; the manuscript now
   repeats it rather than describing the delegation path as the method.

`manuscript/check_claims.py` asserts each of the three against the artifacts, so a future
edit that reintroduces one fails there rather than reaching a reader.
