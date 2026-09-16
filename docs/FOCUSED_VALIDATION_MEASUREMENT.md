# The focused-validation route, measured on the eight-paper corpus

`harness/validation.py`, `harness/between_arms.py`, `harness/stages/validation.py`,
`harness/validation_driver.py`, `harness/prompts/validation.py`. Measured 2026-09-16 by
re-running DISCOVERY under the new code over a copy of the eight papers' project
directories from `runs_final_codex_v2_2026-09-15`, offline, nothing executed, nothing
fetched.

**The headline, at the authority it actually has:**

```
23 focused-validation questions raised across the eight papers
 7 reached the route, 2 planned onto it, 5 refused INFEASIBLE_SPECIFICATION
 2 designs attempted, 0 DESIGNED, 2 SPECIFICATION_BLOCKED
 0 arms instantiated, 0 executions, 0 comparisons, 0 settled
74 COMPARISON nodes recovered from the same eight papers' claim graphs — 0 bound to a
   validation target
```

---

## 1. What the route is, and what it is deliberately not

**This is not a general experiment generator.** For a question a printed number cannot
settle — two variables moved at once, the control the claim needs was never run, the
protocol that produced the number is not the protocol the claim is about — the route
derives the smallest ONE-VARIABLE contrast the paper and its pinned checkout support, runs
both arms in the AUTHORS' OWN code, and holds one arm against the other against a rule
declared before either of them ran. The question it asks is always the same one:

    what is the SMALLEST scientifically legitimate experiment, derivable from the paper
    and its pinned artifact, that discriminates between the competing explanations?

**It exists because a reconciliation cannot ask it.** Every reconciliation this harness
performs holds one measured quantity against a quantity the paper printed — the right
arithmetic for "is 61.4 the number their code produces" and no arithmetic at all for "is the
gain attributable to the augmentation or to the loss weight", which holds one ARM against
another ARM and the paper printed neither side. `comparison.RECONCILABLE` used to be one
entry long for exactly that reason, and `comparison.derive` returned `unsupported` for
BETWEEN_ARMS with a sentence saying a run would produce two numbers and no verdict.
`harness/between_arms.py` is that missing arithmetic; `comparison.RECONCILABLE` now reads
`("AGAINST_PRINTED_VALUE", "BETWEEN_ARMS")`.

**The comparison comes from the claim graph, never from a second reading of it.**
`claimgraph.build` already derives one COMPARISON node per (metric, benchmark) the paper
reports for two or more methods, carrying each arm's address, label and printed value.
`stages.validation.comparison_for` looks that node up by the key `comparison:<metric>|
<benchmark>` and reuses it; nothing here re-parses the paper's own table. Rebuilding it
would be a second inventory of the paper's contrasts, free to disagree with the first.

**Three halves are deterministic and one is a proposal.** The question and its address, the
arms and their addresses, the metric and its unit and the benchmark's stated split are all
read off the document by `harness/validation.py` with no model call. What a model MAY
propose — through `harness/prompts/validation.py` and `harness/validation_driver.py` — is
the configuration of each arm, which variable the experiment changes, which it holds fixed,
and the settlement rule with its tolerance and basis. Every proposed value must then
RELOCATE: the quotation it came from must re-mint to an address in the paper (or occur
verbatim in the pinned checkout text this harness printed into the prompt), or the
ingredient is unbound and the design blocks. The designer is granted no tools at all — not
`Read`, not `Grep` — because everything it needs is already printed into the prompt; a
designer that could open a file could quote something `claims.mint` would never find.

---

## 2. The contract — eight ingredients, and a blocked design is the correct answer

`artifacts.VALIDATION_INGREDIENTS` names what a focused-validation experiment needs before
it may be built. Every one is a REQUIREMENT with no default; `ValidationDesign.state` is
DERIVED from `missing` rather than set, so a design cannot be reported DESIGNED while
something it needs is absent:

| ingredient | what it is |
|---|---|
| `addressed_question` | a review question with an address that re-resolves against the paper |
| `competing_explanations` | at least two readings the run would discriminate between |
| `arm_instantiation` | the paper/artifact says enough to BUILD both arms |
| `metric_identity` | the quantity, its basis and its unit, on both arms |
| `dataset_identity` | the benchmark AND the split, on both arms — never defaulted to "test" |
| `controlled_variables` | what is held fixed, named rather than assumed |
| `changed_variable` | exactly what differs, named — exactly one, never two |
| `settlement_condition` | what observation would settle it, DECLARED before the run |

**`NEVER_ASSUMED` is the same discipline as data rather than as a habit.** Fourteen
scientific choices — `optimizer`, `learning_rate`, `schedule`, `split`,
`augmentation_strength`, `preprocessing`, `threshold`, `batch_size`, `epochs`,
`seed_policy`, `early_stopping`, `weight_decay`, `tokenizer`, `normalisation` — this harness
will not supply however conventional the value is. A design that would need one of these
and does not have it from the paper or the artifact reports `SPECIFICATION_BLOCKED` with the
key named, not completed from convention. `validation_driver.HARNESS_OWNED_DESIGN_KEYS`
additionally strips anything a proposer writes that would make its own answer a verdict —
`state`, `conformance`, `authority`, `answers_question`, `established`, and the four spelled
`settled`, `defect`, `severity`, `paper_decision`, none of which is a field this system has.

**A blocked design is not a shortfall; it is the route working.** An experiment that
answers a question by choosing an optimizer, a split, an augmentation strength or a
threshold the paper never stated measures OUR choice wearing the paper's clothes. Its
result would be a fact about this review's reconstruction, not about the paper, and the
same discipline the prompt states to the model (`prompts.validation._DISCIPLINE`) is
enforced again independently of whether the model obeyed it: every proposed value is
re-relocated, and a design is blocked whether the model volunteered a missing ingredient or
declined to.

---

## 3. The authority ladder — four rungs, and the top one has no spelling

`artifacts.VALIDATION_AUTHORITY` has three real members and a floor:

| level | authority | what it needs |
|---|---|---|
| 1 | `ARM_MEASUREMENT` | one arm produced a number — a fact about that run and nothing else |
| 2 | `CONTROLLED_OBSERVATION` | two arms compared; the design's conformance was NOT established |
| 3 | `CONFORMANT_CONTROLLED_RESULT` | + every arm instantiated from the paper or the pinned artifact, no `NEVER_ASSUMED` choice supplied by this harness, an admissible provenance, and the fired rule answers the question the design was built for |
| 4 | *the credited mechanism causes the effect* | **there is no value for this** |

`artifacts.CAUSAL_ATTRIBUTION_AUTHORITIES` is the empty tuple, and
`ArmComparison.establishes_attribution` is a membership test in it — so the question "did
this experiment prove the mechanism" has one answer, False, for every input this system can
construct, and no later contributor can relax a threshold to change it. That is the same
device `artifacts.ARTIFACT_AUTHORITY` and `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` use
to keep their own unreachable rungs unreachable, on a third channel.

**What a controlled comparison CAN establish, stated positively.** One contrast, on one
benchmark, at one scale, in the authors' own code, held against a rule declared before it
ran: that under THIS comparison the effect the claim predicts did or did not appear. That is
a bounded observation a referee can act on. It is not, and this route never claims it is, a
demonstration that the credited mechanism is what produces the gain in general — a second
benchmark, a different scale or a different seed range could show something else, and
nothing here pretends to have run those. `authority_for` grants level 3 only when the
design is `CONFORMANT` and the provenance is one `harness.provenance.admits` already lets a
reconciliation say something about the paper; either alone leaves a scientific choice this
review made and the authors did not, and the result stays `CONTROLLED_OBSERVATION` — real,
persisted, and about the experiment rather than the paper.

---

## 4. The between-arms arithmetic

`between_arms.compare` takes a design, the two arms' measurements, a provenance and whether
the fired rule answers the design's own question, and computes:

| quantity | how |
|---|---|
| `difference` | `treatment.value - control.value`, in the metric's own unit |
| `relative_difference` | `difference / |control.value|`, or **None** when the control measured zero — never infinity, never a large number: a relative change against zero is undefined, not enormous |
| `direction` | `INCREASE` / `DECREASE` / `NO_CHANGE`, with a declared tolerance as the dead band; with no tolerance the test is the exact sign, because inventing a dead band is inventing the threshold this module refuses to choose |
| `intervals_overlap` | whether the arms' one-sigma intervals overlap — **None**, not False, when either arm has fewer than two repetitions or no reported uncertainty, because False would read as a separation nobody measured |

**Four settlement rules, and the rule and its tolerance are DECLARED BEFORE THE RUN.**
`SettlementCondition.declared_before_execution` is written by the harness at the moment the
design is persisted, never by a proposer, and `compare` refuses a comparison that arrives
without it: a rule chosen after the numbers arrived settles whatever the author of the rule
wanted it to settle.

| rule | fires on | needs |
|---|---|---|
| `DIRECTION_AGREES` | the observed direction against the predicted one | nothing extra |
| `EFFECT_EXCEEDS_TOLERANCE` | `|difference|` against a declared margin | `tolerance`, with a stated basis |
| `EFFECT_WITHIN_EQUIVALENCE_MARGIN` | non-inferiority / equivalence against a declared margin | `tolerance`, with a stated basis |
| `ARMS_INDISTINGUISHABLE` | whether the one-sigma intervals overlap | per-arm `uncertainty` from at least two repetitions each |

**There is no universal percentage anywhere in this module.** `RULES_REQUIRING_TOLERANCE`
and `RULES_REQUIRING_UNCERTAINTY` name exactly which rules need which input, and a rule
missing what it needs is refused rather than defaulted — a tolerance silently set to zero
would settle every comparison in whichever direction the noise happened to fall.

`artifacts.BETWEEN_ARM_REFUSALS` is closed at twelve entries — the empty string, meaning
"not refused", and eleven real refusals — and every one is a fact about the experiment or
about this harness, never a finding about the paper:

| refusal | why it is the right answer |
|---|---|
| *(empty)* | not a refusal — the comparison reached a state and an authority |
| `arm_missing` | fewer than two arms produced a measurement; one arm is a measurement, not a comparison |
| `arm_produced_no_value` | an arm ran and reported no value for the metric — the run happened and the metric did not parse, which is a fact worth keeping distinct from never running at all |
| `metric_identity_mismatch` | the arms' metric, basis or unit disagree; subtracting two numbers that are not the same quantity produces a units error dressed as a result |
| `dataset_identity_mismatch` | the arms ran on different benchmarks or splits; the same disagreement, on the population side |
| `statistical_unit_mismatch` | the arms aggregate over different units (an example vs. a seed vs. a fold) — a mean over incompatible units is not comparable to another mean |
| `not_a_controlled_comparison` | more than one configuration key differs between the arms, or the declared changed variable is not what actually differs; a contrast in which two things moved discriminates between neither |
| `settlement_condition_undeclared` | no rule was declared before the run — the one refusal that exists purely to keep a threshold from being chosen after the numbers arrived |
| `tolerance_undeclared` | the declared rule compares an effect to a margin and the design carries none; defaulting it to zero would settle every comparison in whichever direction the noise fell |
| `uncertainty_unavailable` | the declared rule compares the arms' intervals and at least one arm ran too few times to have one; a spread computed from a single value is zero, which would read as a separation nobody measured |
| `relative_difference_undefined` | the control arm measured zero — recorded as its own defect string even though it never blocks the comparison, because a zero control is a fact a reader tracing a percentage needs to see named rather than silently divided |
| `design_not_established` | the design itself never bound — `SPECIFICATION_BLOCKED` one layer up, arriving here as the reason there is nothing to compare |

---

## 5. The measurement

Re-running discovery under the current code over the eight papers' existing project
state — offline, nothing executed, nothing fetched:

| paper | discovered objects | focused-validation questions | reached the route | planned | INFEASIBLE_SPECIFICATION | COMPARISON nodes in the paper's graph | designed | SPECIFICATION_BLOCKED |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `0c06a98d7c818f6f` | 36 | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| `2024-icml-sapg` | 47 | 2 | 0 | 0 | 0 | 5 | 0 | 0 |
| `5993d35ff0996b52` | 10 | 4 | 0 | 0 | 0 | 0 | 0 | 0 |
| `acl` | 24 | 4 | 3 | 1 | 2 | 4 | 0 | 1 |
| `apt-icml` | 152 | 2 | 1 | 1 | 0 | 21 | 0 | 1 |
| `cvpr` | 44 | 4 | 3 | 0 | 3 | 14 | 0 | 0 |
| `iclr` | 44 | 1 | 0 | 0 | 0 | 3 | 0 | 0 |
| `sanchez24a-icml` | 353 | 3 | 0 | 0 | 0 | 27 | 0 | 0 |
| **total** | **710** | **23** | **7** | **2** | **5** | **74** | **0** | **2** |

Executable: 0. Launched: 0. Reconciled: 0. Scientifically settled: 0.

### The central finding

**The first unbound ingredient is `arm_instantiation`, and both designs that were attempted
were missing ALL SIX ingredients past it: `metric_identity`, `dataset_identity`,
`controlled_variables`, `changed_variable`, `settlement_condition`, and
`arm_instantiation` itself.** Only `addressed_question` and `competing_explanations` bound
on both. The two targets:

* `acl` / `TGT-CLM-P181762-1916-2` — question kind ATTRIBUTION, `metric=''`, `experiment=''`
* `apt-icml` / `TGT-CLM-P20205-429` — question kind CONTROL_PRESENCE, `metric=''`,
  `experiment=''`

Both carry an EMPTY metric and an EMPTY benchmark. `ValidationDesign` reuses the claim
graph's own COMPARISON node, keyed `comparison:<metric>|<benchmark>`; with neither field
on the target, `stages.validation.comparison_for` has no key to look up, no node binds, and
`validation.arms_from_comparison` returns nothing before a model is ever asked to propose
anything — which is exactly why `metric_identity`, `dataset_identity`,
`controlled_variables`, `changed_variable` and `settlement_condition` are ALL unbound
together: none of them can be checked against arms that do not exist.

**The papers themselves are not short of contrasts.** The eight papers' claim graphs carry
74 COMPARISON nodes between them — the paper's own results-table contrasts, recovered
perfectly well by `claimgraph.build`. None of them binds to the two targets that raised an
attribution or a control-presence question, because those targets come from PROSE
findings that name no metric and no benchmark. This is the same shape as the correspondence
CLAUDE.md records under "The one correspondence a paper does not print": the link between a
prose concern and a results-table contrast is SEMANTIC and the document does not state it,
so no amount of parsing recovers it — a fifth heuristic on `discovery._centrality` would not
help here any more than a fourth one helped there.

### The gate comparison

**Opening the design gate changes nothing on this corpus.** With
`SH_ALLOW_VALIDATION_DESIGN` set and a working command, both designs still block at
`arm_instantiation`, with the driver record reading `failure=no_arms` — the model designer
is never called, because `stages.validation._proposal` returns before it reaches
`validation_driver.call` whenever `validation.arms_from_comparison` yields fewer than two
arms. This was verified by running both gate states over the same two targets and comparing
the resulting records side by side: the deterministic half refuses first, in both states,
identically.

---

## 6. What the four zeros mean, and what they do not

**Zero executable, zero launched, zero reconciled, zero scientifically settled is not "the
route does not work".** Every module in this route has a passing, exercised self-check
(§8), the arithmetic in `between_arms.compare` is proven correct on fixtures across every
settlement rule and every refusal, and the deterministic half — reading the document,
building arms from the claim graph, checking every ingredient — ran to completion on all
eight papers with no error and no exception.

**And it is not "these eight papers are fine".** A zero here says nothing about whether an
attribution concern or a missing control on any of these eight papers is real; it says the
route that would test one could not be built, for a specific, named reason.

**The honest sentence is the narrow one.** On eight papers, the targets that raised the
kinds of question a controlled experiment answers — ATTRIBUTION, CONTROL_PRESENCE,
PROTOCOL_CONFORMANCE — carry no metric and no benchmark on the object this route reads, so
no COMPARISON node could bind and no arm could be instantiated. That is a fact about what
`discovery` currently attaches to a PROSE-sourced finding, not a fact about whether these
papers' experiments are sound, and not a fact about whether the arithmetic in
`between_arms.py` is trustworthy once it is fed two real arms.

---

## 7. The limits, and all four are ours

* **The prose-to-comparison correspondence is semantic and unrecovered.** A finding that
  raises an attribution question quotes prose; the paper's own comparison of the two methods
  sits in a table with a metric column and a benchmark caption. Nothing connects the two
  without a reading, and `discovery` does not currently attach one to the object this route
  consumes. This is the same gap the claim-link and materiality work already measured
  between an abstract sentence and a table cell, on a third connection.
* **A focused validation needs a repository, so most of the 23 questions never reached a
  route at all.** 16 of the 23 focused-validation questions across the eight papers did not
  reach this route: with no pinned checkout to run two arms in, the question is left open by
  a different, earlier refusal than the one this route names.
* **`planner` refused 5 of the 7 that did reach the route, on `specification_complete`.**
  `INFEASIBLE_SPECIFICATION` is the planner declining to invent the half of an experiment the
  paper did not specify — a limit of the authors' method section, not of this harness's
  extraction, and it is counted apart from `INFEASIBLE_ADDRESSING` for exactly that reason.
* **The route has never executed on a real paper.** The between-arms arithmetic —
  `difference`, `relative_difference`, `direction`, `intervals_overlap`, all four settlement
  rules, all twelve refusals — is proven correct on fixtures in `between_arms._self_check`
  and `stages.validation.__main__`, with synthetic arms and a synthetic `ProbeResult`. No
  design on this corpus reached `arm_instantiation`, so no process was ever started, no
  provenance was ever admitted, and `authority_for`'s top rung has never been earned by a
  real run. That the arithmetic is correct on fixtures is not evidence it has been exercised
  end to end on a published paper, and this document does not claim it has.

---

## 8. Tests and artifacts on disk

`tests/test_focused_validation.py` carries **41 deterministic tests** for this route — the
same place `test_artifact_route.py` and `test_literature_route.py` hold the artifact and
literature routes. They pin, among others: a two-change confound decomposing into two arms
differing in exactly one declared variable; the ingredient blocks in isolation; the
between-arms arithmetic including the undefined relative difference against a zero control;
the metric, dataset and statistical-unit identity refusals; that an execution failure
becomes `INCONCLUSIVE` and never a defect; that a conformant, admissible, settled
comparison reaches `VALIDATION_DEFECT_ESTABLISHED` and `TargetOutcome.establishes_failure`;
that materiality still decides independently whether it stops the paper; that
`parse_design` strips every member of `HARNESS_OWNED_DESIGN_KEYS`; and a SWEEP over every
reachable (state x authority x conformance x provenance) asserting
`establishes_attribution` is False throughout. None is marked `network` or `docker`: the
whole route is deterministic and offline.

Each module also carries its own self-check, discovered and run by
`tests/test_self_checks.py`:

| module | self-check covers |
|---|---|
| `harness.validation` | document reading (`unit_at`, `stated_split`, `arms_from_comparison`), every one of the eight ingredients blocking in isolation, conformance derivation, the four dispositions |
| `harness.between_arms` | the happy path and its authority, all twelve refusals, both non-conformant and inadmissible-provenance paths, zero-control relative difference, all four settlement rules, interval overlap with one and with many repetitions |
| `harness.stages.validation` | `planned` (the route owns only its own plans), method-text extraction, a gate-shut blocked design, a full design through `adjudicate` to `VALIDATION_SUPPORTS_CLAIM`, the same run on `synthesized` provenance reading `VALIDATION_OBSERVATION_ONLY`, an arm that produced no value, and `summarise`'s totals |
| `harness.validation_driver` | the gate/command availability checks, that a designer cannot sign the harness's own vocabulary (`HARNESS_OWNED_DESIGN_KEYS` stripped), settlement parsing rejecting an unknown rule, configuration relocation against both the paper and the pinned checkout, and a malformed response never producing a partial design |
| `harness.prompts.validation` | the discipline text is present, every settlement rule and every `NEVER_ASSUMED` key appears in the prompt, and the reader's signature admits no finding, severity, verdict, grade, outcome, decision, conformance or authority |

The route's routing into the wider system is exercised from the discovery/planning side in
`tests/test_question_centric_routing.py` (a question with no printed quantity still reaching
the route, the planner warranting an implemented focused experiment, the between-arms
comparison states as four different facts, a declared settlement letting a run start) and
from the artifact side in `tests/test_artifact_route.py`
(`test_an_artifact_finding_may_trigger_execution_and_focused_validation`).

Per paper, under `projects/<pid>/validation/`:

| file | what it holds |
|---|---|
| `<pid>.validation.json` | the sealed `FocusedValidation` — design, measurements, comparison, disposition |
| `<pid>.validation.driver.json` | the seal: content hash, the model-call record (command, tool policy, return code, seconds, and the failure when there was one), and the disposition and launch count read straight from the sealed record |

On this corpus both files exist for the two targets that reached a design, and both record
`disposition: SPECIFICATION_BLOCKED` with `launched: 0`.
