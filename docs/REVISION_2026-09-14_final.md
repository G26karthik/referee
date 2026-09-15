# Final product pass, 2026-09-14 — what was audited, what changed, what was measured

This document is the record of the reopening. It exists because several of its findings
are MEASUREMENTS that cost real effort to obtain and that no artifact under `projects/`
records, and because two of them are negative results that should stop a future revision
from re-attempting the same thing.

Nothing here is a claim about a paper. Everything here is a claim about this harness,
checked against the eight-paper corpus in `projects/` unless stated otherwise.

---

## 0. The audit

Six independent read-only audits traced the source rather than the documentation:
review architecture, evidence and admissibility, deterministic paper checks, the
authors'-code and reconstruction routes, evaluation instrumentation, and the manuscripts.
Their findings were reconciled by hand, and two were REJECTED on re-verification:

* **A claimed double-count of `processes_launched` (160 vs a "real" 70) is wrong.** The
  audit counted only `runs/<pid>/execution.jsonl` and missed the per-target logs at
  `runs/<pid>/targets/<tid>/execution.jsonl`. Counting all of them gives 160, matching
  `sum(TargetOutcome.launched)` exactly, and the three `acl` runs are genuinely distinct
  executions (05:59:13, 06:01:56, 06:04:25, three working directories). The published
  number is correct and `check_referee_claims.py:97` is correct.
* A claim that the `about="PAPER"` half of document integrity fires on the corpus. It
  does not: 85 observations, 85 `about="EXTRACTION"`, 0 `about="PAPER"`.

---

## 1. The isolation ceiling was real, and it has been lifted honestly

Until this revision `repo_exec` was not gated off. It was **unreachable by construction**:
`isolation.sufficient_for_repo_exec` requires CONTAINER or REMOTE_SESSION, `LocalBackend`
declares VENV, and `SandboxBackend` had no credentials. `authorize()` refused with
`isolation_insufficient` regardless of `SH_ALLOW_REPO_EXEC`. That is why every execution
this harness ever performed on a real paper was a harness-authored diagnostic, and why
"RED from the authors' own code" existed only in fixtures.

`harness/container.py` + `backends.ContainerBackend` supply a backend that MEETS the
requirement rather than lowering it. Measured on this host:

```
container  can_execute=True  isolation=CONTAINER  usable=True   repo_exec permitted
local      can_execute=True  isolation=VENV       usable=True   still refused
```

Docker 29.7.2, Linux x86_64, 6 CPUs, GPU passthrough confirmed (`--gpus all` reports the
RTX 4060). `authorize()` now walks past isolation and refuses at the next real gate, which
is the conjunction intact. Nothing in the isolation vocabulary changed.

What a container is worth is stated in the module rather than implied: its own filesystem,
process namespace, user and network stack, carrying none of the operator's credentials.
It is NOT a claim of adversarial containment against a shared kernel.

---

## 2. Two negative results. Do not re-attempt these without new evidence.

### 2.1 Materiality cannot be widened safely on real papers

The `ABSTRACT_CLAIM`-only rule yields **3 material targets of 878**, and **all three have
`routes=['NONE']`** — material and checkable are near-disjoint, so the only live path to a
paper-level rejection is a lens's asserted FATAL. Two candidate widenings were measured
before implementing either:

| candidate paper-owned dependency link | corpus evidence | verdict |
|---|---|---|
| abstract/conclusion explicitly cites "Table N" | **1** occurrence across 8 papers | too rare to carry the product |
| abstract number uniquely binds exactly one cell | 5 matches, **5 false** | unshippable |
| abstract claim carries a checkable quantity | 6 of 68 sentences, all artefacts; **0 compositions** | abstracts state no checkable arithmetic |

All five "unique bindings" were inspected individually and every one is a coincidence:
`26` ("26 leading LLMs") matched a garbage row `'24 mpla 25 26 27 28 (E'`; `58` ("58
topics") matched inside the merged cell `'32.2828.58'`; `30` ("30% pruning memory")
matched inside `'30,469'`; `9` and `1` were section numbers.

**Decision: materiality is unchanged.** This is the "strongest conservative version we can
defend", and the recall limitation is the table above. Route-exhaustion coverage is
therefore reported over CENTRAL questions **and** material ones, with both denominators
printed, because a rate over a denominator of 2 is noise.

### 2.2 Deterministic cross-reference checking is bounded by extraction, not by logic

Measured recovery against the original PDFs: table captions **69 → 56 (81%)**, figure
captions **64 → 64 (100%)**, equation numbers **66 → 18 (27%)**. Adjudicated false-positive
rates when the existing observations are read as claims about the paper:
`CROSSREF_UNRESOLVED` **21/21 false**, `NUMBERING_GAP` **4/4 false**, `OBJECT_UNCITED`
**≥19 of 42 false**. The `about="EXTRACTION"` labelling is the only thing preventing 27
false accusations, and it is holding.

Two unmodelled false-positive classes were found and are worth recording: a citation into
*Supplementary Materials* (a different document) and a **phantom citation** manufactured by
a page-break running header (`"lifted from the table"` + `"5 SAPG: Split and Aggregate…"`
produced a reference to a Table 5 nobody wrote).

The one direction that is defensible: a per-paper, per-kind **completeness witness**
comparing recovered labels against caption lines in the raw PDF. Figure captions recover
100% on all eight papers, so a figure cross-reference check gated on a measured witness of
1.0 is a statement about the paper; the same check on equations (27%) stays refused.

---

## 3. Correctness defects fixed

Each was verified against the real corpus, not asserted.

| # | site | what it did |
|---|---|---|
| 1 | `local_exec.parse_cell_number` | took the FIRST number in a cell with no refusal. `60.357.47` (a mean fused with its std) read as `60.357`, a quantity no paper printed, then reconciled and shown to a reviewer as "the printed value". `253.6% 114.8% 74.2%` read as `253.6`. This was on the path of every execution this harness ever performed. Now refuses fused and multi-quantity cells; measured cost 15 of 2,862 cells |
| 2 | `claims.parse_quantity` | `= 2.9K` read as `2.9`, then "contradicted" by the product 2,900 → `PAPER_ARITHMETIC_CONTRADICTION` → RED, with no model, no execution and no grading, and a printed reason naming a quantity the paper does not contain. Now refuses magnitude suffixes and credits printed precision |
| 3 | `outcome.EXECUTION_GLOSS` | printed "the authors' own code ran" for **every** admissible provenance, so an independent reimplementation that disagreed accused the authors' code. Now names the actor from provenance |
| 4 | `stages/report.py` | printed the PLAN's intent as the run's identity under "Experiments triggered" — "author code reproduction" on 16 of 16 synthesized runs. Now prints planned and ran separately |
| 5 | `ExecutionRecord` | carried no provenance at all while stamping the authors' commit SHA onto harness-authored runs. Now carries provenance; commit is written only for `repo_exec` |
| 6 | `priority.order` | scored every object on `routes[0]`, which is `AUTHOR_CODE_EXECUTION` **never** across 878 corpus objects and `PAPER_INTERNAL_CHECK` (which settles nothing) 57 times at maximum decisiveness. The target budget truncates that ordering. Now scores `planner.route_that_would_be_taken` |
| 7 | `ContainerBackend.provision` | `env_status="ready"` for a venv containing only pip. Now `empty_environment` when declared dependencies could not be installed |

---

## 4. Dynamic early stop

`harness/assessment.py` stops BEFORE investigation, from findings counted at grade time,
and runs once. `stages/probe.py` contained no reference to `materiality`,
`establishes_failure` or `assessment` anywhere in its execution loop, so a target that
established a material failure did not stop the next targets cloning, planning,
authorizing and launching.

The guard is now at the head of the `pairs[1:]` loop and calls
`materiality.material_target_failure(target_set.objects, outcomes)` — **the same call
`claim_status` makes**, so no second rule about what a material failure is now exists.
Three refusals are tested against the real loop in `tests/test_dynamic_early_stop.py`:

* a BLOCKER never triggers it (a blocker establishes nothing);
* an INADMISSIBLE failure never triggers it — the case the corpus produced 16 times, and
  the one that would have stopped seven papers on evidence that may conclude nothing;
* an established NON-MATERIAL defect never triggers it.

Skipped targets are `SUPERSEDED_BY_ESTABLISHED_FAILURE` with `launched == 0`, which is
neither a refusal nor an attempt, and every target still reports an outcome.

---

## 4a. What the container unlocked, and what it did not — measured

With the container backend registered, `SH_ALLOW_REPO_EXEC=1`, install and network open,
and every historical run untouched (`SH_PROJECTS_DIR` pointed at a scratch copy), the four
repository papers were driven through acquisition, static audit, synthesis and
`plan_execution`.

**Isolation is no longer the binding constraint. Experiment identity is.** Across the
executable targets of all four papers, `experiment` reached `established` **zero** times:

| paper | identity outcome | the refusal, and whether it is correct |
|---|---|---|
| `acl` | `no_candidate` ×3 | *"the cited row reports 'Qwen-2.5Instruct', and no .py or .sh file in this checkout implements it — the cell is a third-party baseline"*. **Correct.** The authors' code cannot produce a competitor's number |
| `iclr` | `unmapped` ×4 | prose claims with no cell address; prose identity binds counts only, and an accuracy stated in text has no column header or baseline row. **Correct** |
| `apt-icml` | `ambiguous` ×2 | 84 advertised commands could emit accuracy. **Correct** — choosing among them is a guess |
| `cvpr` | `unmapped` ×1 | — |

EXHAUSTED over every author-code-routed, addressable target on all four repository papers —
not sampled, after the density fix below — `established` is **0 of 257**:

| paper | author-code-routed + addressable | identity outcomes | established |
|---|---:|---|---:|
| `apt-icml` | 147 | `ambiguous` 113, `no_candidate` 34 | 0 |
| `acl` | 25 | `no_candidate` 25 | 0 |
| `iclr` | 43 | `unmapped` 6, `no_candidate` 37 | 0 |
| `cvpr` | 42 | `unmapped` 3, `no_candidate` 39 | 0 |
| **total** | **257** | | **0** |

Sampling 12 per paper gave 0 of 48 and was MISLEADING: it missed the seven false bindings
described below, which lay outside the first twelve of `apt-icml`'s 147. A sample is not a
corpus claim, and this one hid the most dangerous defect in the system.

Three refusals account for all 48, and each is the correct answer rather than a limitation
of this harness: the cited row is a third-party baseline the repository does not implement;
the claim is prose with no cell to bind a metric to; several advertised commands fit the
cited quantity equally and choosing one would be a guess.

### The one place identity DID bind, and why it was wrong

Exhausting `apt-icml`'s full pool of 147 rather than sampling it found **7 targets reaching
`established`** — the first identity bindings this project has produced. All seven were
FALSE, for two independent reasons, and both had to be fixed:

```
r0: ['Density', '', 'Method', 'MNLI', 'QQP', ...]        <- the column header
r8: ['',  '10%', 'PST',  '79.6', '86.1', ...]            <- the bound row
```

1. **`10%` is the DENSITY column, read as SPARSITY.** Density 10% is sparsity 90%, so the
   binding matched a command declaring `sparsity=10` against a row meaning the opposite,
   and narrowed 84 candidates to exactly one on that basis. `DENSITY` is deliberately not
   a naming token for sparsity: it is the inverse quantity, not a synonym.
2. **Row 8's method is `PST`, a third-party baseline** — the same class `acl` correctly
   refuses with "the cited row reports 'Qwen-2.5Instruct', and no file in this checkout
   implements it". The table's `header` list is empty, so the guard did not fire.

An established identity AUTHORISES running that command against that cell. A wrong one is
therefore not a missed check; it is a measurement of the wrong experiment, presented as the
paper's own, with the full weight of an admissible provenance behind it. This is the single
most dangerous failure mode in the system, and the corpus contained it.

The fix: a row value is accepted only where the table's own header rows or the row itself
NAME the field. Verified on the three real shapes — APT's density column yields nothing, a
genuine `Ratio` column still yields the row's own value, and a caption-only sparsity still
stands when no column names one.

This is the honest shape of the result, and it is stronger than "our gates were shut": the
harness removed its own blocker and the remaining blocker is a property of the papers and
their repositories. **Papers do not say which command produces which printed number**, and
no amount of compute or permission fixes that.

`acl`'s entrypoint was also confirmed to be `data/templates/investment_analysis/npv.py` —
the first README-advertised script, a data-generation template rather than the evaluator —
so its `invalid_invocation` capability refusal is about the wrong file.

## 5. Still open

Listed so they are not mistaken for done.

* Route exhaustion is not represented. `PlanDecision` records the one chosen route;
  routes not taken leave no trace. The metric needs a per-(question, route) attempt record
  and the anti-gaming rules — in particular, a route blocked by one of THIS harness's own
  gates must count as neither discharged nor a fact about the paper, or the metric reads
  1.0 and measures the operator's `.env`.
* `SH_MAX_TARGETS` can still drop a material target, and the paper then reads
  `PASS_TO_HUMAN_CLEAN`; `BUDGET_DEFERRED` is in neither `BLOCKER_FOR_DISPOSITION` nor
  `DELIBERATELY_UNCLASSIFIED`.
* Five central dispositions produce no blocker (`PENDING`, `NOT_ATTEMPTED`,
  `BUDGET_DEFERRED`, `CITATION_VERIFIED_ONLY`, `SUPERSEDED_BY_ESTABLISHED_FAILURE`), and
  the totality self-check cannot see them because it asserts only over
  `BLOCKED_DISPOSITIONS`.
* With the gates shut, `_not_started`'s default `IDENTITY_BLOCKED` reports our own closed
  gate as the artifact's fault.
* `ARTIFACT_INSPECTION` is offered on 261 objects and has no `PLAN_ACTIONS` member, so it
  can never execute.
* The alignment layer can promote `ambiguous → established` on a fabricated field value:
  `FIELD_VALUE_PATTERNS["sparsity"]` matched `5` inside the metric value `4523.5%` and
  overrode the caption's correct `60% sparsity`, eliminating 75 of 84 candidates by
  contradiction against a number that does not exist.
* `reproduction_class: "within_noise"` is published in `reports/corpus.json` for 7 of 8
  papers where no authors' code ran, and `CorpusEntry.verdict` holds the triage colour.
* `examined_rate` counts 167 pure refusals as "a route was pursued": published 0.0518
  against 0.0205 (a route actually pursued) and 0.0054 (something actually ran).
* `check_claims.py` fails 15 of 75: `manuscript.tex` is pinned to the seven-paper run
  while the artifact holds the eight-paper run, and the case-study target id moved
  (`TGT-REP-P90-155` → `TGT-REP-P60-155`).
* The fresh eight-paper run, the single-model baseline, the ablations, the human packet,
  Figure 2 and the manuscript rewrite.

---

## 6. Stale facts in the working contract

`CLAUDE.md`'s "Facts — about the DEVELOPMENT HOST" states "No WSL, no Docker, no
virtualization on this host". Both Docker and WSL are present and Docker is running with
GPU passthrough. That section was true when written and is now false; the container
backend depends on it being false.
