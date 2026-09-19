# single-harness

> **New here, or reading this as an agent in a fresh checkout? Read `HANDOFF.md` first.**
> This file is the working contract — dense, and written from inside the development
> environment. Paths like `../.venv/Scripts/python.exe` and everything under `## Facts`
> describe **the machine this was built on**, not a requirement: substitute your own
> interpreter. `HANDOFF.md` §2 has the platform-neutral setup.

An autonomous **first-pass scientific reviewer** for papers. In: PDFs. Out: a concise
reviewer-facing report per paper — **a set of scientific findings, each with a category, a
resolution state, and a machine-verified evidence pointer** — backed by a complete machine
ledger a human never has to read to understand the conclusion.

Reproduction is one evidence route inside that reviewer, not the definition of it. The
system reads the paper with four independent lenses, turns their concerns into questions,
decides for itself what is structurally checkable, prioritises it, decides whether an
experiment is *justified*, runs the authors' code when one is, reconciles what it measured
against what the paper printed, and hands a human two pages instead of sixty.

**The core unit is the QUESTION, not the finding and not a colour.** A finding says what is
wrong; a question says what would settle it, and the gap between those is the difference
between a critique and a review. `ReviewQuestion` carries its own address, the findings
that raised it, the routes that could close it, what it would take to settle it, what the
evidence route actually produced, and what follows scientifically. `harness/questions.py`
derives it; `stages/discover.sync_questions` folds the outcome back onto it, twice — once
when targets are planned and again once anything has run.

**The four axes, which nothing may collapse** (`harness/taxonomy.py`):

| axis | field | what it is a property of |
|---|---|---|
| what KIND of problem is this | `scientific_class` — 10 values | the paper's argument |
| was the question settled, by what | `resolution_status` — 5 values | the review process |
| what did the evidence route produce | `evidence_state` — 14 values | the world |
| which review path was this paper on | `artifact_state` — 6 values | the paper's artifact |

A CONFOUND settled from the paper and a CONFOUND left open by a missing artifact are the
same kind of problem in opposite states. One label for both tells a referee neither, and a
colour tells them none of the four. `EVIDENCE_ABOUT_THE_PAPER` names the only four
evidence states that say anything about the paper at all — every other one is a fact about
an artifact, a host, or one of this harness's own gates; `ARTIFACT_ABOUT_THE_PAPER` names
the only one of six artifact states that does.

**The fourth axis was added because `execution_provenance` answered four questions with
one token.** `SYNTHESIZED_DIAGNOSTIC` was what a review said for a paper that published no
code, for one whose clone a gate refused, for one whose clone failed, and for one whose
cloned repository was never authorized — four opposite facts, and the reviewer report
mentioned the artifact nowhere at all. `taxonomy.artifact_state` derives the six from the
acquisition and the capability, and `taxonomy.review_path` projects them onto the two paths
requirement 2 asks to be explicit: PAPER_ONLY and PAPER_AND_ARTIFACT. Only the artifact
path can ever produce an admissible reproduction; only the paper-only path reaches the
governed reconstruction route.

**Three of the evidence states were one state, and it was the wrong one.**
`harness_addressable` is a conjunction of three requirements, and every failure of any of
them landed on `EXTRACTION_LIMITATION`, whose sentence claims this review could not build
a re-derivable address. Measured over the shipped corpus that sentence was printed 39 times
across six of seven reviews and was FALSE every time: all 52 targets carrying it had a
RESOLVED address, and in one review it sat two lines under the address it claimed not to
have. The real blocker in 52 of 52 was that no route applied. They are now three states —
`EXTRACTION_LIMITATION` (our extraction), `REPORTING_LIMITATION` (the paper printed no
unambiguous quantity there) and `NO_ROUTE_AVAILABLE` (our method inventory) — and
`DiscoveredObject.addressing_blocker` names which one applies.

**A verified citation is not a verified claim.** `CITATION_VERIFIED` is its own state
because the two alternatives are both wrong. The paper-only route has two branches:
re-evaluating a composition the paper printed settles whether the paper's own arithmetic
holds, and re-verifying the QUOTATION behind a concern establishes only that the concern
cites the paper accurately. Both ended in `PAPER_ONLY_RESOLVED`, and all 20 of the shipped
corpus's paper-only outcomes were the second kind — so every review printed the very
sentence its own findings disputed under `## What held up`, and every funnel reported those
targets as having "settled a question about the paper". The honest count was zero.
`CITATION_VERIFIED` resolves to UNRESOLVED, `concerns_the_paper` is False, and
`PAPER_INTERNAL_CHECK` left `planner._RESOLVING` so a citation check can no longer suppress
an experiment.

**Experiment necessity is its own decision** (`EXPERIMENT_NECESSITY`, 7 values).
`NO_EXPERIMENT_NEEDED` is a SUCCESSFUL review outcome and is never counted beside "we
needed one and could not run it". `PlanDecision` records the why-yes half — `why_material`,
which cheaper routes were ruled out and on what grounds, what the run would discriminate
between (carried verbatim from the finding's own counter-explanations), and what it should
produce if the paper is right — alongside the `gates` that were already the why-not half. A
system that spends compute owes both, and "the gates passed" is not an answer to "why did
you spend it on this?".

**The reader-facing outcome is FOUR ROWS folded over disjoint inputs**
(`harness/outcome.py`, printed as `## Review outcome` at the top of every review):

| row | what it answers | reads |
|---|---|---|
| what was established | `finding_state` — 3 values | `claim_status`, the kept findings |
| what was resolved | `question_state` — 4 values | the review questions |
| what execution produced | `execution_state` — 6 values | the target outcomes |
| what this is an assessment of | `scope_state` — 3 values | the targets and their centrality |

Disjointness is the guarantee, not tidiness. `finding_state` is a function of
`claim_status` and a count of kept findings and of NOTHING in the execution row, so no
blocked, refused, inconclusive or inadmissibly-provenanced run can move what the review
established. Asserted by sweeping every execution state against every finding state.

The row that mattered most: an execution that settled nothing now reads
*"execution was attempted and did not produce admissible evidence, so nothing about the
paper follows from it"*, followed by the EXACT reason, cut only at a sentence boundary.
It used to read `**NOT_VERIFIED**` under a heading called `## Reproduction status`, beside
`Targets: 5 addressing blocked, 1 inconclusive, 25 not attempted` — three defects in four
lines. `NOT_VERIFIED` is a value of two different vocabularies in this codebase and reads
as a verdict in both; `inconclusive` reads as a property of the paper; and that section's
gloss said "see the chain below" in an artifact that has no chain, which was true of six of
the seven shipped reviews. The section is gone. Its state is the execution row and its
counts are in `## Scope of this review`, where a denominator belongs.

**The triage is a ROUTING decision and is printed under `## Scope of this review`.** RED
means a material failure was ESTABLISHED. YELLOW means something needs a human's attention
and is never an accusation. GREEN means neither, within the scope actually checked; it is
not a certificate of correctness, and `claim_status` keeps VERIFIED_SUPPORT ("checked and
held") apart from NOT_VERIFIED ("could not check") underneath it. RED is exactly the binary
`overall_verdict`, unchanged. None of the three is the review's result — the findings and
their resolution states are, and leading with the colour said otherwise.

You are the controller's reviewer. `harness/controller.py` drives; deterministic code
below it decides what may be concluded. Neither side may overrule the other.

## Workflow

```
papers → controller → ingest → audit → collect → grade → assess → discover → probe → report
                                                              └ questions · targets · priority · plan
```

| phase | module | input → output | decides | refuses by |
|---|---|---|---|---|
| ingest | `stages/ingest.py` | PDF → `paper/doc.json` | nothing (deterministic) | `error` on an unreadable PDF |
| audit | `stages/audit.py` + `audit_driver.py` | doc → `audit/<lens>.json` ×4 | the four lenses judge | `waiting`, resumable |
| ↳ parts | `reading.py` + `pdf.plan_reading` | doc → N bounded passes per lens | nothing; a traversal | — |
| ↳ synthesis | `reading.synthesis_brief` | one lens's own verified observations → one cross-part pass | proposes only | — |
| collect | `stages/audit.load_reports` | lens files → verified findings | quote ≟ paper | drops the finding, counts it |
| grade | `stages/grade.py` + `grade_driver.py` | serious findings → `audit/grade/<slug>.json` | a second, blinded reviewer per candidate | `ok` with partial coverage — never blocks a report by default |
| assess | `assessment.py` | findings + grades → `CaseState.assessment` | has a material failure already been established, and is the investigation still open | never blocks; `investigation_open` is an INPUT to `planner.classify` |
| **discover** | `stages/discover.py` | doc + findings → `discovery/targets.json` | what is addressable, what it is worth, whether an experiment is justified | records a NAMED refusal per target |
| ↳ artifact | `stages/artifact.py` + `artifact_evidence.py` | doc + pinned checkout → `ArtifactFact` × N | what the RELEASED CODE establishes, for a question whose SCOPE it answers | `ARTIFACT_INSPECTION_INCONCLUSIVE` when it settles nothing |
| ↳ literature | `stages/literature.py` + `literature.py` | doc + public indexes → `PriorArtFact` × N | what EARLIER PUBLISHED WORK bears on a novelty claim the paper makes about itself | `SEARCH_COMPLETED_NO_MATCH_FOUND`, which settles nothing |
| ↳ validation | `stages/validation.py` + `validation.py` + `between_arms.py` | doc + pinned checkout → one CONTROLLED CONTRAST | what the smallest ONE-VARIABLE experiment the paper supports would discriminate between | `SPECIFICATION_BLOCKED`, naming the ingredient the paper does not state |
| verify | `stages/probe.py` | doc + repo → `ProbeSpec` per target | identity, capability, resources, commit, backend | leaves the spec unpromoted |
| execute | `backends.py` + `local_exec.py` | spec → `ProbeResult` | `authorize()` alone | `verdict: blocked` |
| reconcile | `local_exec.reconcile` | metric vs the addressed quantity | arithmetic only | `INCONCLUSIVE` |
| report | `stages/report.py` | everything → `reports/<pid>.review.md` + `.md` + `.ledger.json` | threshold table, over `counted_severity` | — |

Four PURE layers run inside the report stage, read only artifacts that already exist, and
decide nothing. Each is a separate module so that what it may see is a signature rather
than a convention:

| layer | module | reads | writes | decides |
|---|---|---|---|---|
| review outcome | `outcome.py` | `claim_status`, questions, outcomes | the four reader-facing rows | nothing |
| document integrity | `docintegrity.py` | `PaperDoc` ONLY | `DocumentObservation` × N | nothing; gates nothing |
| surface coverage | `coverage.py` | `PaperDoc` for the denominator, address STRINGS for the numerators | `ReviewSurface` + `CoverageReport` | nothing |
| guarantees | `guarantees.py` | every artifact above | `ReviewGuarantees`, incl. `unmet` | nothing |

And one runs BEFORE the pipeline: `preflight.py` answers "is this batch N distinct papers"
from the PDFs' bytes, and refuses a batch containing the same document twice.

The discover phase is pure and consults no model. It is where the four questions a first
reviewer actually asks get separated:

| question | module | what it may read |
|---|---|---|
| what does the paper's own summary rest on? | `claimlink.py` + `claimgraph.py` | the parsed doc, and address pairs a reader proposed which this harness has already verified |
| where does the paper say this? | `claims.py` | the parsed doc — a lens supplies a quote, the harness mints the address |
| what would settle this concern? | `questions.py` | a finding's own closed-vocabulary self-classification |
| what KIND of problem is this? | `taxonomy.py` | the same closed vocabulary; never a number, a name or a paper |
| has somebody already done this? | `literature.py` + `literature_providers.py` | the paper's own novelty sentences, and bibliographic records a public index returned |
| what is the smallest experiment that would discriminate? | `validation.py` + `between_arms.py` | the claim graph's own COMPARISON nodes, and configuration values quoted from the paper or the pinned checkout |
| what is checkable, and how central? | `discovery.py` | structure: abstract, cited addresses, parsed quantities |
| is it worth it, and is it justified? | `priority.py`, `planner.py` | vocabulary strings and booleans only |

**The funnel is six terms, each read off a different artifact, and none substitutes for
another** (`CaseLedger.efficiency`, `evaluation.corpus()["funnel"]`):

```
discovered → checkable → warranting an experiment → launched → completed     resolved
  objects      objects         plans                 run record  outcomes    outcomes
|------------------ a real nesting: each is a subset ------------------|     NOT nested
```

**The first five nest and the sixth does not**, and the sixth is why there are six.
`discovered >= checkable >= warranting_experiment >= launched >= completed` is a genuine
subset chain, so `launched > warranting_experiment` would mean something ran that was never
judged to warrant it. `resolved` is NOT a subset of `completed`: a question settled from the
paper's own printed arithmetic is resolved with nothing launched at all, which is the whole
point of the cheap-route-first policy. `evaluation.assert_conservation` asserts the chain
through `completed` and bounds `resolved` by `discovered`; asserting it through the last term
reported a correct measurement as an accounting defect.

`warranting_experiment` is a JUDGEMENT this system made; `launched` counts processes it
actually started, read off `TargetOutcome.launched` which copies `ProbeResult.executions`.
Reporting the first as though it were the second is the specific overclaim the split
exists to prevent. `probe_stage_seconds` is named for what it measures — acquisition,
static audit, planning and gate evaluation — and is not the cost of running experiments.

Every started process is recorded whole in `runs/<pid>/execution.jsonl` — command, cwd,
commit, timestamps, exit code, full stdout/stderr, parsed metric. A reproduction verdict
must be re-derivable from that file by hand.

**Three questions, never one.** The reviewer's whole job is keeping these apart, and
every pure module below exists to stop one of them collapsing into another:

| question | where it lives | what it may do |
|---|---|---|
| Is there an issue? | `candidate_class` (lens) + `grade.verdict` (blinded grader) | CONFIRMED_FINDING is the only bucket eligible for FATAL/MAJOR — `grading.CANDIDATE_CAP` enforces it |
| How sure are we? | `confidence`, bounded by `grading.evidence_support` | evidence TYPE caps confidence; a second independent check LIFTS the cap |
| How much does it matter? | `severity` → `counted_severity` | nothing here SETS it; every mechanism only caps it |

Plus, over the finished report: `harness/selfaudit.py` (did the review exercise its own
discipline — 12 machine-checked items, verdict-inert) and `harness/corpus.py` (every
requested paper in exactly one terminal state, conservation law asserted).

## Commands

```bash
SH_ALLOW_AUTO_AUDIT=1 SH_ALLOW_GRADING=1 python run.py review \
    --paper a.pdf b.pdf c.pdf --auto-audit --auto-grade       # the entrypoint
python run.py review --paper a.pdf                            # exit 2 → lenses pending
python run.py status <paper-id>                               # controller state + history
python run.py list                                            # reviewed papers
python run.py dossier                                         # consolidate finished reports
python run.py evaluate                                        # system metrics over the corpus
python run.py sandbox [--release]                             # leased remote machines
python run.py preflight                                       # is this batch N distinct papers?
python -m pytest tests -q                                     # 2332 tests
python -m pytest tests -q -m "not network"                    # 2290 passed, 33 skipped,
                                                                # 7 deselected, 2 failures
                                                                # (confirmed pre-existing/
                                                                # environment-dependent —
                                                                # a container runtime and a
                                                                # second-boundary timing
                                                                # race, neither touched by
                                                                # the 2026-09-19 pass)
```

**The two env vars above are not decoration.** `--auto-audit` and `--auto-grade` select a
delegation mode; they do not OPEN its gate. `run.py cmd_review` calls `Config.load()` and
never calls `Config.open_delegation_gates`, so with the flags alone `audit_driver.available`
returns `(False, 'auto-audit gate is closed: SH_ALLOW_AUTO_AUDIT is not set')` and the run
stops at `waiting`. `run.py --help` says so; this line now says so too.

Always `PYTHONUTF8=1` on Windows (paper text is full of em dashes and math) and always
the repo venv: `../.venv/Scripts/python.exe`.

Per paper, `projects/<pid>/` holds four things a reader should not confuse:

| file | who reads it |
|---|---|
| `reports/<pid>.review.md` | **a human reviewer.** One to two pages. |
| `reports/<pid>.ledger.json` | anyone tracing a line of that report to an artifact |
| `discovery/targets.json` | anyone asking what else was considered, and why it was not pursued |
| `reports/<pid>.md` / `.json` | the complete machine trace |

Self-checks: **every module that carries an `if __name__ == "__main__"` guard has one,
and there are 70 of them.** Do not maintain a list here; the hand-written one drifted to
34 while modules kept landing. `tests/test_self_checks.py` DISCOVERS them by walking
`harness/**/*.py` and parsing for the guard with `ast`, so a module that loses its
self-check fails the suite, and `python -m harness.<module>` runs any one of them
(`python -m harness.stages.<report|grade|discover>` for the stages). `tools/loop.py` runs
the whole loop in one command. `harness.sandbox`'s self-check leases nothing and needs no
credentials.
`python -m harness.pdf <file.pdf>` takes a PDF path.

`docs/HARNESS_ARCHITECTURE.md` is the per-stage contract, the address grammar and the
full evidence chain. **`docs/REVISION_2026-09.md` is the most recent revision's own
review of itself** — the ten defects it found in the shipped corpus, each measured, what
was learned from the two external repositories, and the three decisions the eight-paper
rerun needs first. Read it before quoting any number under `projects/`.
`docs/IMPLEMENTATION_REPORT.md` says what changed in each major revision, what was preserved, and — in §7.5 and §7.6 — the defects and overclaims each
revision's own self-review found. `docs/CURRENT_STATE_MAP.md` is the diagnosis the work
was built from and is superseded in part; it says where.

## Immutable invariants

Do not weaken these to make more papers executable or more findings reportable.

1. Every `evidence_quote` is re-verified against the parsed paper; a cell citation must
   match that cell. Unsubstantiated findings are dropped and counted. A prose address is
   no exception and no shortcut: `claims.mint` refuses a quote that occurs twice, and
   `claims.resolve` re-reads the span off the document, so a lens that writes its own
   `P<i>:<a>-<b>` gains nothing (`span_mismatch`).
2. `verified_observation` and `evidence_class` are written by the harness, never read
   from a lens file. A lens cannot certify its own reasoning.
3. **Provenance ceiling** — only `driver`, `repo_exec` or `reimpl_exec` provenance may
   reconcile a printed cell, in *either* direction. A synthesized probe can neither
   convict nor acquit. `reimpl_exec` is admissible HERE and gated further downstream:
   `local_exec.reconcile` and `backends.authorize` additionally require
   `ReimplementationConformance.established`, so a reconstruction settles a verdict only
   when every required ingredient binds to both a paper locator and a verified
   implementation locator. Its reader-facing label is INDEPENDENT_REIMPLEMENTATION and
   never AUTHOR_REPOSITORY.
4. Only `authorize()` may permit repository execution, and it requires all of: gate
   open, a backend that `can_execute`, `repo_exec` provenance, a verified commit,
   experiment + metric + configuration identity, capability, and sufficient resources.
5. Commit mismatch, a dirty tree, or an unverifiable commit blocks execution.
6. Resource insufficiency — including *unknown* demand — yields `INCONCLUSIVE`. A
   paper's silence about its own cost is not evidence the cost is small.
7. Capability, environment, dependency and platform failures yield `INCONCLUSIVE`, never
   `FAILED_REPRODUCTION`. Only a crash *after* the experiment demonstrably started may
   convict.
8. The paper decision is a materiality TABLE in `stages/report.py`, not a model judgement
   and not a count: `MATERIAL_SEVERITY = ()`. RED iff `claim_status` is
   VERIFIED_FAILURE — deterministic paper arithmetic or a failed reproduction from an
   admissible provenance, AND `materiality.material_target_failure` establishing that a
   central claim depends on that target. Both are necessary: an admissible FAILED
   REPRODUCTION on a target no central claim is established to depend on is GREEN with
   the defect reported, which 'RED iff VERIFIED_FAILURE' alone does not convey. A model-assigned FATAL is an attention signal with no rejection
   authority. Nothing accumulates: no number of MAJORs or MINORs ever reaches RED, because a
   concern weakens a claim and does not reject one, and the old `RED_MAJOR_ONE_LENS=3` /
   `RED_MAJOR_TOTAL=10` thresholds made the decision a property of how many things a
   panel chose to write down rather than of the paper. Independent grading
   (`harness/grading.py`) still cannot promote: it changes what is *eligible* to be
   counted at each severity and can only demote a lens's own asserted grade. Turning
   grading off, or never running it, reproduces the ungraded decision exactly:
   `counted_severity` stays empty and `stages.report.counted()` falls back to `severity`.
9. No experiment is shrunk, substituted or downscaled to make it fit. There is no
   function that does this, deliberately.
10. No paper-specific logic. The pilot papers are evaluation cases, not special cases.
    `grading.derive`'s signature admits vocabulary strings and booleans only — no count,
    no number, no metric name, no paper identity — so "no std dev = MAJOR", "if
    epsilon=0.05 never flag" and "if <paper> appears, soften" are *inexpressible*, not
    merely absent. `tests/test_reasoning_architecture.py` asserts the signature, and
    that no prompt names a pilot paper or states a severity floor prescriptively.
11. **Every mechanism that touches severity may only CAP it.** Nothing in the reviewer
    can raise a grade: not the grader, not the lens's pass-B work, not the evidence
    ceiling, not any self-consistency cap. Asserted by sweep over the whole reachable
    input space, `RANK[counted_severity] <= RANK[lens_severity]`.
12. Nothing may collapse the three questions above into one. In particular no cap is
    keyed on *evidence type* — a weak citation bounds CONFIDENCE, and confidence bounds
    severity. `caption → NOTE` used to be such a cap and was wrong for the same reason
    `no variance → MAJOR` was: both decide impact from something that is not impact.
13. Every requested paper reaches exactly one terminal state, and the accounting asserts
    it (`harness/corpus.py`). A summary may say "6 requested · 5 completed · 1 failed";
    it may never say "5 reviewed" when six were asked for.
14. A retry budget is spent only where spending it could change the answer
    (`harness/failures.py`). A rate limit, an outage, a missing CLI, a revoked
    credential, an unknown flag and an unreadable PDF are not transient failures and
    must not consume an attempt.
15. **What may be pursued is decided by the harness, never by a lens.**
    `Finding.verifiable_by_experiment` is an unchecked model boolean and is metadata
    (`DiscoveredObject.proposed_by_lens`). What opens the execution path is
    `harness_addressable` — a resolved reference, a parsed quantity where the route needs
    one, and a route that is not NONE — followed by `planner.classify`, whose signature
    admits vocabulary strings and booleans only.
16. **A blocked target ends that target, never the paper.** Every target carries its own
    `TargetOutcome`, and only `establishes_failure` — FAILED_REPRODUCTION on `driver` or
    `repo_exec` provenance — may contribute a material failure. One paper reporting a
    blocked target, a reproduced one and a failed one must report all three.
17. **A colour may not be a property of this harness's configuration.** The first version
    of the triage flagged every unsettled central target and made all seven corpus papers
    YELLOW, because the execution gates are shut by default. Only a target that was
    ATTEMPTED and settled nothing colours a paper; "we were not permitted to check this"
    is reported in the review's scope section and counts toward nothing. This is the same
    defect the removed `RED_MAJOR_TOTAL=10` threshold had.
18. **Positional coincidence may not establish a reconciliation.** `parse_metric` prefers
    a target-bound output object over a named one over a structured one over a bare key,
    and REFUSES when the winning tier disagrees with itself. The generic last-JSON-wins
    scan this used to fall back to (`json_metric`) was proven unused — nothing but its own
    self-check and a diagnostic flag ever called it — and was deleted in the 2026-09-19
    simplification pass; positional last-JSON-wins now has no path to a reconciliation at
    all, rather than an unused one.
19. **The reviewer's report and the machine trace are two artifacts.** The report is
    bounded BY CONSTRUCTION — a per-category cap, a global finding cap, and a cap on
    every other section — not by hoping the paper is short: over the current corpus it
    runs 6.3-8.7 KB, two to three printed pages, against 11-21 KB of machine report and
    26-307 KB of ledger. What it drops it says it dropped. A system that hands a reviewer
    sixty-seven findings has not reduced anyone's workload.
20. **The four axes may never be collapsed into one, and no colour is the result.**
    `scientific_class` (what kind of problem), `resolution_status` (was it settled) and
    `evidence_state` (what the route produced) are properties of three different things
    and are derived by `harness/taxonomy.py` from closed vocabulary alone.
    `scientific_class` is deliberately NOT a parameter of `grading.derive` — "a CONFOUND
    is always MAJOR" is the same defect as "no variance = MAJOR", deciding impact from
    something that is not impact. `resolution_status` is derived from `evidence_state`
    rather than stored beside it, because a resolution that could be set independently of
    the evidence behind it is a resolution nobody checked. The provenance ceiling is
    applied a SECOND time here, at the reporting layer: a FAILED_REPRODUCTION on
    inadmissible provenance is an INCONCLUSIVE_EXECUTION, so a synthesized diagnostic
    cannot convict through the report after being refused by the reconciler.
21. **What was judged worth running and what actually ran are two numbers.** The funnel's
    six terms each come from a different artifact and none may be substituted for
    another; `TargetOutcome.launched` is copied from the runner's own process count and
    is never inferred from a disposition. Likewise `probe_stage_seconds` is the wall time
    of a stage that includes acquisition, static audit, planning and every gate — calling
    it execution cost when `processes_launched` is zero would be a fabricated
    measurement. A target this run's budget did not reach is BUDGET_DEFERRED, which is
    neither a refusal nor a block.
22. **A necessity decision is not evidence.** NO_EXPERIMENT_NEEDED is a successful review
    outcome on the necessity axis and resolves nothing on the evidence axis: declining to
    run something does not settle a missing control, the authors adding one does. Its
    evidence state is NOT_INVESTIGATED, deliberately.
23. **Escalation is conditional, and the condition is counted.** A route that settles a
    question with nothing running is taken before one that runs; a SUPPORTING target does
    not earn an execution while a CENTRAL one is being pursued; and
    `CaseLedger.efficiency` records how often each happened. An efficiency claim with no
    count behind it is not a claim. **And the condition is "settles", not "is cheap":**
    `PAPER_INTERNAL_CHECK` sat in `planner._RESOLVING` and settles nothing, so for every
    target where a contradiction lens offered it AND an executable route existed, the
    execution was suppressed by a citation re-verification. Over the shipped corpus that
    was 100% of the paper-only resolutions.
24. **The four reader-facing rows are folded over DISJOINT inputs** (`harness/outcome.py`).
    `finding_state` reads `claim_status` and a count of kept findings; it may not read a
    disposition, a provenance, a reconciliation, a launch count, a coverage number or a
    document observation, and the signature is what makes that so. No execution outcome
    can move what a review established, asserted by sweeping every execution state
    against every finding state. This is invariants 4 to 7 restated where a reader can
    see it: infrastructure failure is not scientific failure, and here it is not even in
    the same column.
25. **An execution that settled nothing says so in those words, with the reason.**
    "The paper is inconclusive" is never printed, because the paper is not inconclusive —
    our attempt to check it was. The reader-facing sentence is "execution was attempted
    and did not produce admissible evidence", followed by the exact reason from the target
    outcome, cut only at a sentence boundary (`outcome.clip`). It used to be cut at 200
    characters mid-clause, which took the disclaimer off the end of a line that opened
    with a large delta and left a skimming reviewer looking at what appeared to be a
    catastrophic reproduction failure.
26. **A document-integrity observation is not a finding and gates nothing.**
    `harness/docintegrity.py` emits `DocumentObservation`s into their own list, never into
    `findings`, on the model `CodeAudit.runtime` already sets. The type carries no
    severity, confidence, scientific class or verdict FIELD, and none of the decision
    functions has a parameter that could receive one. Every observation says whether it is
    about the PAPER or about this harness's EXTRACTION, and that field is required with no
    default: a naive "cited but not recovered" check over the shipped corpus produced
    twelve claims that a table or equation was missing, and all twelve of those objects
    are in the papers and absent only from what extraction recovered. Reporting those as
    defects would make a colour a property of extraction quality, which is the defect
    invariant 17 forbids.
27. **A coverage denominator is counted off the PAPER and cannot be fabricated.**
    `coverage.surface` takes a `PaperDoc` and nothing derived from the review; `measure`
    takes its numerators as plain address STRINGS, so a numerator cannot redefine a
    denominator. The rate this replaces — `targets_addressable / targets_discovered` —
    divides the harness's own object list by itself, and because an unresolvable reference
    never becomes an object at all, a WORSE EXTRACTOR SCORES HIGHER on it. It read 0.93
    corpus-wide and was published. It is kept under the name `objects_addressable_rate`
    with a limitation entry saying what it is; the real measure reads 0.33 addressed and
    0.018 examined over the same corpus. An empty surface yields None and never 1.0, an
    address outside the surface is reported in `off_surface` rather than counted, and
    `semantic_coverage` says `not_machine_detectable` unconditionally so a clean coverage
    table cannot be read as evidence the review found what mattered.
28. **A guarantee is checked from an artifact or it is not claimed.**
    `harness/guarantees.py` splits PROCESS guarantees, which this system enforces and can
    check per review, from SCIENTIFIC guarantees, which it does not make. Every process
    guarantee carries `holds` established from a named field and `evidence` naming it, and
    `unmet` lists those that did not hold FOR THIS REVIEW — a non-empty `unmet` is a defect
    in this harness, never a finding about the paper, and it reaches no threshold. The
    non-guarantees are printed: issue recall is neither claimed nor measurable, accuracy is
    unmeasured for want of adjudicated ground truth, novelty is not checked at all, and
    nothing in a review says the paper is correct.
29. **A cross-section concern cites every side, and every side resolves alone.**
    `Finding.additional_evidence` carries the further locations one concern depends on,
    each with its own quotation and reference, each re-verified against the parsed paper
    independently. A concern with a side that does not resolve is DROPPED WHOLE and
    counted — never kept with the half that happened to check out. Written as one
    quotation plus prose asserting that another section disagrees, such a concern is
    unfalsifiable: the second half names nothing a reader can open, and it would have
    arrived through the cross-part synthesis, which exists to find exactly these. The
    confidence ceiling is computed from the WEAKEST side (`weakest_evidence_class`),
    because a concern is only as checkable as its least checkable half — and deliberately
    NOT by passing the extra citations to `grading.evidence_support` as corroborating
    sources, which would let one reader lift its own ceiling by citing twice.

30. **Deduplication is over resolved addresses and closed vocabulary, never over text.**
    Two concerns are one concern when the same lens raised them, they classify themselves
    identically in every closed vocabulary, and they were established from exactly the
    same ordered set of locations. The key is the lens's own classification and not
    `scientific_class`, which is a SUMMARY of it: on `apt-icml`, `protocol-03` (Table 2
    prints 100.0% where Table 11 shows the same computation) and `protocol-09` (the
    abstract normalises against LoRA+Prune, not fine-tuning) anchor on the same quoted row
    of page 20 and both summarise to CONTRADICTION. Keyed on the summary, the review lost
    one of them. Over-merging DELETES a real concern and leaves a merged id as its only
    trace; under-merging reports one concern twice, which a reader can see and the
    synthesis is asked to fold. The key errs toward the second. Over the evaluated corpus
    it now merges 0 of 227 findings, which is correct: each lens there read once.

31. **A proposed claim link is two quotations, and the harness writes everything else.**
    `harness/claimlink.py` re-mints the claim quotation, requires it to resolve inside the
    Abstract or the Conclusion, re-resolves the evidence address against the quotation
    given for it, and re-derives the arithmetic. `accepted`, `refusal`,
    `numeric_relation`, `claim_ref` and `verified_observation` are stripped at the driver
    boundary and overwritten at the verifier, so a reader cannot certify its own pairing
    any more than a lens can certify its own evidence (invariant 2, on a new channel). A
    numeric disagreement is REFUSED rather than reported: this channel has no grader, no
    evidence ceiling and no severity cap, and cannot tell a real inconsistency from a
    reader citing the wrong cell. An accepted link establishes a DEPENDENCY and never a
    truth, and the observation says so in those words.

    **And an accepted link is not authority for the relationship it names.** Verifying two
    endpoints does not verify that the evidence supports the claim; a model proposed that.
    `link_authority` splits the two — `ENDPOINTS_VERIFIED_SEMANTIC_LINK`, whose support
    relationship is model-proposed and which may inform priority, navigation, coverage and
    explanation but **never a paper-level decision**, and `STRUCTURALLY_BOUND_LINK`, whose
    relationship the DOCUMENT establishes by an explicit cross-reference from the claim
    sentence or by independently established metric, benchmark, comparison-arm and
    statistic identity. Only the second could ever become a materiality input. They are
    counted separately and never summed into one headline, because summing them would
    launder a model's semantic judgement into a deterministic result — invariant 2's
    failure mode one level up. Over the eight-paper corpus the second count is 0 of 63.

32. **A hyphen a line break inserted is not a difference in the quotation.** A PDF
    breaking "generation" across lines leaves `gener-` and `ation`, which flattens to
    `gener-ation`; a reader quoting the sentence writes `generation`, and the evidence gate
    refused a correctly-quoted sentence. Measured over the evaluated corpus, **4 of the 5
    findings that gate dropped were exactly this** — 80% of every drop was punctuation the
    typesetter inserted — and 8 of 11 correctly-quoted abstract sentences were refused on
    the one paper the claim-link reader was first run against. `claims.soft_hyphen_projection`
    removes only hyphens that the ORIGINAL text shows were followed by whitespace, so
    `diverse-weather` survives and `gener-ation` does not. Three rules keep it honest: the
    exact search runs FIRST, so a character-for-character quotation is never resolved
    through a normalisation; `claims.flatten` is unchanged, so every `P<i>:<a>-<b>` address
    in the repository still means what it meant; and the machine-written observation drops
    the word "verbatim" and says the match was recovered, because claiming verbatim there
    would be the false attestation `source_units` itself exists to prevent.

33. **Static artifact inspection establishes what the CODE does and never that a result
    is wrong, and a bounded fact settles only a matching bounded question.**
    `harness/artifact_evidence.py` has four authority rungs and the top one — "the
    reported scientific result is false" — is not a value of `ARTIFACT_AUTHORITY`, which
    makes the rule inexpressible rather than merely documented.

    **The scope rule is the correction that matters.** Every probe declares which bounded
    question it answers, and `IMPLEMENTATION_CORRESPONDENCE` is excluded from
    `SETTLEABLE_BY_ARTIFACT_FACT` by construction. The first version had no notion of
    scope: `discharge` asked only whether SOME fact carried authority, so on all four
    repository papers the target *"the released repository implements the described
    method"* was settled by facts like *"the checkout advertises evaluate.py"* — four
    papers reported as having had a claim about their implementation settled by the
    presence of a file. An entrypoint existing is SUPPORTING EVIDENCE for that question and
    is not its answer. A broad question is now decomposed into the narrow ones the route
    can answer, each recorded against its own scope, and left open with the reason.

    **And the experiment identity is classified, not believed.** `PAPER_ARTIFACT_MISMATCH`
    requires a paper statement that MINTS, an artifact span that RELOCATES in an audited
    tree, and an identity ESTABLISHED from a deterministic source that itself relocates.
    A model saying "this looks like the right config" is AMBIGUOUS and stops at
    `ENDPOINTS_VERIFIED_ARTIFACT_CONCERN` — both ends real, the correspondence unchecked,
    which is a referee's question and not a demonstrated inconsistency.

    Five terminal states, and only `ARTIFACT_MISMATCH_ESTABLISHED` reaches
    `ARTIFACT_EVIDENCE`, the one `EVIDENCE_ABOUT_THE_PAPER` admits. None is in
    `establishes_failure`, so no artifact observation can bypass materiality; a question
    whose answer is a measurement (`requires_execution`) is refused outright; and "the
    repository cloned successfully" discharges nothing, because `discharge` requires a
    statement the route was asked about and a fact whose scope answers it.

34. **A model statement about code is not artifact evidence, and an unaudited rule is not
    reviewer-visible.** `artifact_evidence.relocate` is `claims.mint` for source: the
    writer supplies a file and a quotation, the HARNESS finds it, and a citation that is
    absent, non-unique or outside the checkout is DROPPED — not softened. What survives
    carries the file's SHA-256 beside the pinned commit, so a reader can prove the line has
    not moved. The authors'-code auditor (`SH_ALLOW_ARTIFACT_REVIEW`, off) gets `Read` and
    `Grep` and nothing else; `authority`, `refusal`, `span`, `snapshot`, `paper_ref`,
    `fact_id` and `probe` are stripped at its driver boundary.

    The ten existing AST rules are classified by the authority each can carry
    (`RULE_AUTHORITY`), and an unclassified rule defaults to invisible: **the audit
    licenses a rule, not its existence.** Measured over the four repository papers the
    rules produced six hits of which five are false, every one because they match
    SUBSTRINGS of identifiers and an identifier is not a semantic category.

    **Only class A reaches a referee.** Class B FEEDS THE AUDITOR: the match is
    deterministic and its meaning is not, so a B hit is a place to look and becomes
    reviewer-visible only after exact source relocation and bounded semantic review, at
    the authority that channel earns. Seven of the ten have never fired on a real
    repository, so rendering a first-ever B hit as a reviewer observation would put an
    unmeasured detector in front of a human wearing a measured one's clothes.

35. **A batch's paper count is checked before it is spent.** `harness/preflight.py`
    answers, per requested file, which `paper_id` it will get, whether that id already
    holds a DIFFERENT document, and whether another requested file is the SAME document —
    all from the PDF's bytes. Two different papers that slugify identically are fine and
    are reported with their distinct ids; the same paper submitted twice REFUSES the batch,
    because `allocate_paper_id` correctly resumes its project, which is right per paper and
    invisible in aggregate. An eight-file request that is seven documents produces seven
    reviews and a claim about eight papers.

    **And it is now a gate as well as a command.** For two revisions this invariant had
    to carry a correction reading "`preflight` is a SEPARATE OPERATOR COMMAND, not a gate
    inside `run.py review` — `harness/controller.py` contains no reference to it and
    `review_papers` never calls it", which was honest and left a one-line hole open: a
    batch submitted straight to `review` was not refused, `allocate_paper_id` correctly
    resumed the duplicate's project, and an eight-file request that was seven documents
    produced seven reviews and a claim about eight papers. `corpus.account`'s conservation
    law cannot catch that, because the duplicate never becomes a second case to conserve.
    `review_papers` calls `preflight.check` FIRST now, returns the refusal with the files
    named and starts nothing, and carries the preflight result on every batch it does run
    so a corpus claim can be checked against `distinct_documents` rather than against the
    length of the request. `run.py preflight` remains, for asking before spending.

36. **Finding prior art may support a concern; failing to find it establishes nothing,
    and the vocabulary is what makes that so.** `harness/literature.py` runs a bounded
    prior-art search — the paper's own novelty sentences, minted by `claims.mint`, against
    public indexes with stable identifiers — and there is NO authority, NO disposition and
    NO evidence state anywhere in it that means "novel".
    `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` is the EMPTY TUPLE, so the membership
    test that asks the question can never succeed and there is no threshold a later
    contributor could relax. A completed search that matched nothing is
    `SEARCH_COMPLETED_NO_MATCH_FOUND` → `BOUNDED_SEARCH_NO_MATCH`, which is not in
    `EVIDENCE_ABOUT_THE_PAPER`, resolves to UNRESOLVED, and does not discharge the route.

    **Three deterministic halves and one model half.** The claims are quotations this
    document contains; the candidate set is CANONICALISED (DOI, then arXiv id, then index
    id, then title AND first author AND year — so one paper appearing as a preprint and a
    proceedings record counts once, and two papers sharing a title do not merge); and the
    chronology is decided from dates, never from a reading. A candidate dated after the
    target's cutoff is refused whatever the overlap looks like, and two dates are never
    ordered more precisely than their sources allow — a proceedings YEAR does not put one
    same-year paper before another. Every date any source offered is recorded beside the
    one chosen, so "we picked the date that suited the concern" would be visible.

    What a model may do is propose queries and propose a RELATION. Both ends are then
    verified — the claim re-mints to the same address and actually claims something; the
    candidate carries an identifier; the quoted passage is present in the text THIS HARNESS
    retrieved — and the pairing lands at `ENDPOINTS_VERIFIED_LITERATURE_CONCERN`, whose
    overlap is the reviewer's reading. `STRUCTURALLY_BOUND_PRIOR_ART` additionally needs a
    deterministic basis AND a priority claim, and may be zero over a corpus. **Level 4, "the
    paper is not novel", has no spelling**, and no literature disposition is in
    `establishes_failure`.

37. **A controlled experiment this review designed may say what the experiment did,
    and only a conformant one may say anything about the paper.**
    `harness/validation.py` derives the smallest one-variable contrast the paper and its
    pinned checkout support, and refuses to build one otherwise: all eight entries of
    `artifacts.VALIDATION_INGREDIENTS` must bind, and `NEVER_ASSUMED` names the scientific
    choices — optimizer, split, schedule, threshold, augmentation strength, seed policy and
    seven more — this harness will not supply however conventional the value is. A design
    needing one is `SPECIFICATION_BLOCKED` with the key printed. `ValidationDesign.state`
    is DERIVED from `missing` and `assumed` rather than stored beside them, so a design
    cannot be reported DESIGNED while something it needs is absent.

    **The settlement condition is declared before anything runs**, and there is no
    universal percentage anywhere in the route. `SettlementCondition` carries the rule, the
    predicted direction and — where the rule needs one — a tolerance WITH A BASIS, and
    `declared_before_execution` is written by the harness at design time.
    `between_arms.compare` refuses a comparison without it, because a rule chosen once both
    numbers are in settles whatever its author wanted it to.

    **The arms are checked, not trusted.** Metric, basis, unit, benchmark, split and
    statistical unit must agree between the two arms, and exactly the declared variables —
    no more — may differ in their configurations; a key present on one arm and absent on
    the other is a DIFFERENCE, because comparing only the keys both carry would call two
    arms controlled precisely because the thing that differs was recorded on one side. Each
    failure is its own entry in `BETWEEN_ARM_REFUSALS` and not one of them is a finding
    about the paper.

    **Four rungs, and the top one has no spelling.** `ARM_MEASUREMENT` is about one run;
    `CONTROLLED_OBSERVATION` is two arms compared with conformance unestablished, resolves
    to UNRESOLVED and is the expected commonest outcome; `CONFORMANT_CONTROLLED_RESULT`
    needs BOTH a design whose every arm derives from the paper or the pinned artifact AND a
    provenance `harness.provenance` already admits. Level 4 — "the credited mechanism is
    what produces the effect" — is `CAUSAL_ATTRIBUTION_AUTHORITIES`, the EMPTY TUPLE, so
    `ArmComparison.establishes_attribution` is a membership test that can never succeed.
    One controlled comparison on one benchmark is evidence about that comparison.

    **And `local_exec.reconcile` refuses this comparison outright.** A BETWEEN_ARMS spec
    reaches the reconciler with an established, admissible comparison and is returned
    NOT_ATTEMPTED: everything below that gate holds a measured quantity against one the
    PAPER PRINTED, and the paper printed neither arm. The arithmetic runs in
    `between_arms.compare`, which asserts over its own source that it reads no
    `claimed_value`, `claimed_delta`, `claimed_cell_value` or `table_ref`.

## Operating autonomously

`--auto-audit` delegates each lens to a reviewer — `SH_AUDIT_CMD`, or the `claude` CLI
discovered on PATH — as **one subprocess per lens**, which is stronger isolation than
four lenses read in one session. It is opt-in because it spends tokens.

Without it, `review` writes the prompts, exits 2, and resumes when the reading files
exist. If you fill them yourself, **run each lens in a separate turn**: four independent
readings are four independent PROPOSAL STREAMS, and one context that remembers the
previous three is one reading echoed four times. (They are not four pieces of evidence —
nothing a lens writes is evidence until the harness re-verifies its quotation.) The parts
of ONE lens are a different matter and are blind to each other for a different reason; see
the reading design above.

Abstention is an outcome, not a failure. A paper with no repository, an ambiguous
experiment or a 24 GiB demand on an 8 GiB card still gets a complete review;
`CaseState.reproduction_class` names why reproduction did not conclude.

**A clean paper is a real result.** Zero FATAL/MAJOR findings, several MINOR concerns and
a handful of open review questions is a complete, correct review of good work — not a
reviewer that failed to try. `prompts/audit.STANCE` says so to the lens, `## Open review
questions` prints the questions as questions, and `grading.CANDIDATE_CAP` makes sure they
count toward no threshold. Do not read a GREEN as a missed finding.

**A refused experiment is a result too, and the refusals are not one refusal.**
`planner.plan` returning INFEASIBLE_SPECIFICATION on a central attribution question is the
system declining to invent the half of an experiment the paper did not specify.
INFEASIBLE_ADDRESSING is a different refusal and used to share the label: it says WE could
not build an address for the claim. The first is a limit of the authors' method section,
the second is partly a limit of our own extraction, and a reviewer needs those apart. Every
refusal is recorded per target with the gate that produced it, carries its own
`EXPERIMENT_NECESSITY`, is printed in the review, and is counted in `CaseLedger.efficiency`.
A first reviewer that runs something in every case is not being careful, it is being busy.

A batch's completeness is a machine-readable artifact, not a printed count: `reports/
corpus.json` lists every requested paper with its terminal state and, when it did not
finish, the `failure_kind` that stopped it.

Never edit `projects/<pid>/audit/prompts/*.md` — regenerated every run.

**A LONG PAPER IS READ IN PARTS, AND THE PARTS ARE BLIND TO EACH OTHER.** A paper that
fits in one pass is unchanged: one prompt, one `audit/<lens>.json`, no synthesis, four
model calls. A paper that does not is traversed in N bounded parts per lens, and then one
cross-part synthesis per lens:

```
audit/prompts/<lens>/part-01.md          audit/<lens>/parts/part-01.json
audit/prompts/<lens>/synthesis.md        audit/<lens>/parts/part-01.driver.json
audit/reading/<lens>/part-01.manifest.json   audit/<lens>/synthesis.json
                                         audit/<lens>.json        <- COMPOSED
```

The composed lens file is what every later stage reads, so nothing downstream knows or
cares how the paper was traversed. Its `.driver.json` carries `written_by:
composed_from_parts` and names every part's own sealed sidecar, so provenance is by
reference rather than weakened.

Three properties, and each is checked against the prompt bytes rather than asserted
(`tests/test_part_audit_stage.py`):

  * **A part never sees another part's findings.** What crosses a part boundary is the
    ANCHOR PACKET — title, abstract, conclusion, section outline — extracted, identical in
    every part, and carrying no model output. It exists so the comparison the
    contradiction lens was built for survives splitting.
  * **A lens never sees another lens's anything**, exactly as before. Splitting a paper
    did not cost this and is not permitted to.
  * **The synthesis proposes and decides nothing.** Its input is the anchors plus that
    lens's own candidates that ALREADY passed quotation verification; it may merge,
    connect, propose, or WITHDRAW. Everything it returns passes the same verification,
    the same evidence ceiling, the same caps, and the same blinded grading as a part
    reader's concern — and the grader is never told which pass produced what it is
    weighing.

Stated for a paper: *each scientific lens is isolated from the other lenses; long papers
are traversed in bounded parts without exposing one part's model findings to the next; and
a final lens-local synthesis combines only quotation-grounded observations from that lens
to recover cross-section relationships.*

## The one correspondence a paper does not print

**A referee's first question about a number is "does the conclusion depend on this?", and
until now nothing here could answer it.** Every model-free mechanism fired on almost
nothing, measured over the eight-paper corpus:

| mechanism | hits |
|---|---|
| `discovery._centrality`'s `in_abstract`, legacy locator | 0 / 706 objects |
| `discovery._centrality`'s `in_abstract`, corrected locator | 0 / 706 objects |
| `materiality.basis_for_ref` | 1 / 1,729 addresses |
| `claimgraph`'s deterministic headline dependency | 0 / 1,729 addresses |
| `claimlink`'s STRUCTURALLY BOUND links, with a reader proposing | 0 / 63 accepted |
| value-matching a headline number to a unique cell | 0 / 21 numbers |

The reason is not extraction quality. **There are zero cross-references in any Abstract of
any of the eight papers and one in any Conclusion.** An abstract states a result in prose —
"reduces training memory by 40%" — and does not write "see Table 3"; the number it prints
is a rounding, a rename, or a delta that appears in no cell. The correspondence is
SEMANTIC and the document does not state it, so no amount of parsing recovers it and
adding a fifth heuristic weight to `_centrality` would move a number without making it
mean anything.

**So a reader proposes the pairing and the harness verifies it** — the arrangement every
other model-supplied fact in this system already has. `harness/claimlink.py` re-mints the
claim quotation, requires it to land in the Abstract or the Conclusion, re-resolves the
evidence address against the quotation given for it, and where both sides carry a number
re-derives the relationship to the claim's own printed precision. A pairing whose halves do
not both hold is refused, counted, and kept on disk. `harness/claimgraph.py` turns the
accepted ones into `SUPPORTED_BY` edges, and `dependency()` answers with a PATH — "the
abstract states 91.4; that number is one arm of the accuracy-on-CIFAR-100 comparison; the
baseline is the other arm" — which a boolean never could.

**An accepted link is not one thing, and the difference is the whole of its authority.**
The harness proves both ENDPOINTS: the headline quotation exists, it really is in the
Abstract or the Conclusion, the evidence quotation and address exist and agree, and any
available numeric relationship recomputes. It does NOT prove *this evidence
scientifically supports this headline claim* — that relationship was proposed by a model.
So every accepted link carries one of two authorities, and the two counts are reported
separately and never summed:

| authority | deterministic | may inform |
|---|---|---|
| `ENDPOINTS_VERIFIED_SEMANTIC_LINK` | both endpoints | priority, navigation, coverage, route planning, explanation — **never a paper stop** |
| `STRUCTURALLY_BOUND_LINK` | endpoints **and the relationship** | the above, and materiality, subject to the materiality audit |

`claimlink.structural_binding` admits two bases: `explicit_crossref`, where the claim
sentence itself cites the object the evidence belongs to and that label identifies exactly
one recovered object; and `quantitative_identity`, where metric (the column header),
comparison arm (the row label), benchmark (the caption) and statistic (the values agree to
the claim's own printed precision) are each established independently. **Three of four is
not a binding.** Measured over the eight papers: **76 proposed, 63 endpoint-verified,
0 structurally bound, 13 refused.** The zero is printed as a zero, and
`docs/CLAIM_GRAPH_MEASUREMENT.md` §8.1 says which requirement failed and how often.

**Three things this channel may not become.**

  * *A second contradiction lens.* A claim and a cell whose numbers disagree may be a real
    inconsistency or a reader citing the wrong cell, and nothing here can tell them apart.
    The link is REFUSED. Raising a contradiction belongs to a lens, under quotation
    verification, an evidence ceiling and independent grading; minting one here would
    reach a reader with none of those.
  * *A source of truth.* An accepted link says the paper's summary DEPENDS on an address.
    It says nothing about whether the claim is correct, and the machine-written
    observation says so in those words.
  * *A dependency.* With `SH_ALLOW_CLAIM_LINKS` closed no link is ever established, the
    graph has no `SUPPORTED_BY` edges, `materiality` falls back to its own structural
    rule, and the decision is bit-identical to what it was before this channel existed —
    the same property `allow_grading` has and for the same reason.

`claimgraph` is **wired into no decision.** `materiality.basis_for_ref` and
`discovery._centrality` are untouched; `claimgraph.compare_with_existing` measures the
delta so a rule is never replaced before the replacement has been measured on every paper.

## What the released artifact can establish, and what it cannot

**The harness read the authors' code from the first version and threw the scientific
result away.** `code_audit.py` parsed every cloned checkout, applied ten rules and wrote
its hits into the machine report, where nothing consumed them: `ARTIFACT_EVIDENCE` and
`RESOLVED_FROM_ARTIFACT` sat in their vocabularies with NO disposition mapping to either,
so the state machine could not reach them from any input, and `ARTIFACT_INSPECTION` was a
declared route that produced an `INFEASIBLE_*` action and nothing else.

**Four rungs, and the top one has no spelling.**

| level | what it is about | what it needs |
|---|---|---|
| `ARTIFACT_FACT` | the CHECKOUT | an audited snapshot, a relocated span, and a question whose SCOPE it answers |
| `ENDPOINTS_VERIFIED_ARTIFACT_CONCERN` | two real locations | + an addressed paper statement; the CORRESPONDENCE is the auditor's reading |
| `PAPER_ARTIFACT_MISMATCH` | the paper AND the checkout | + an experiment identity ESTABLISHED from a deterministic source |
| *the reported result is false* | — | **there is no value for this** |

**A relocated code quotation proves that this code exists at this location in this audited
snapshot. It does not prove that the code contradicts the paper** — that is the same
correction the claim-link channel needed, on a second channel, and
`ENDPOINTS_VERIFIED_ARTIFACT_CONCERN` is where a pairing lands when the relationship
between its two verified ends is still a model's reading.

`artifacts.ARTIFACT_AUTHORITY` has exactly three members and level 3 is not one of them.
An AST warning may not become RED. A code or configuration inconsistency may create a
verified concern, establish a reproducibility defect, trigger execution, trigger focused
validation, and become material where a central claim provably depends on it — every one
of those is a downstream decision, and none is reachable by writing a stronger string in
this layer. `ARTIFACT_RESOLVED` is deliberately excluded from
`TargetOutcome.establishes_failure`.

**The experiment identity is the requirement that makes level 2 real, and it is
CLASSIFIED rather than believed.** "Some config somewhere says 32" contradicts nothing: a
repository sets a batch size in a dozen places. `classify_identity` returns ESTABLISHED
only when the auditor names a DETERMINISTIC source for the link — the paper printing the
command, the checkout's README mapping the experiment to the file, a committed script that
passes the config, an authors' experiment table — AND that source RELOCATES in the pinned
tree. PARTIAL is a real source that did not relocate; AMBIGUOUS is the auditor's reading;
UNBOUND is nothing offered. **Only ESTABLISHED may support level 2.** `no_disagreement`
records all three binding and the values AGREEING, which is a result and not "nothing
found", and `values_not_comparable` records a category error rather than manufacturing an
inconsistency out of one.

**A BOUNDED FACT SETTLES ONLY A MATCHING BOUNDED QUESTION.** Every probe declares the
scope it answers (`FILE_PRESENCE`, `ENTRYPOINT_PRESENCE`, `DEPENDENCY_DECLARED`,
`MANIFEST_PRESENCE`, `CONFIG_LITERAL`, `COMMAND_PRESENCE`), and
`IMPLEMENTATION_CORRESPONDENCE` — "does the released code implement the described method",
"is the implementation faithful", "does this reproduce the paper" — is excluded from
`SETTLEABLE_BY_ARTIFACT_FACT` by construction. The first version of this route had no
notion of scope, so on all four repository papers the target *"the released repository
<url> implements the described method"* was DISCHARGED by facts like "the checkout
advertises evaluate.py", and four papers were reported as having had a claim about their
implementation settled by the presence of a file. A broad question is now DECOMPOSED — the
route answers the narrow questions it can and names them — and left open.

**Five terminal states, and only one of them is about the paper:**
`ARTIFACT_MISMATCH_ESTABLISHED` (→ `ARTIFACT_EVIDENCE`, the only one
`EVIDENCE_ABOUT_THE_PAPER` admits), `ARTIFACT_FACT_ESTABLISHED`
(→ `ARTIFACT_PROPERTY_ESTABLISHED`, a settled question about the CODE),
`ARTIFACT_CONCERN_VERIFIED_ENDPOINTS` (→ UNRESOLVED, as `CITATION_VERIFIED` is),
`ARTIFACT_INSPECTION_INCONCLUSIVE`, and `ARTIFACT_BLOCKED`.

**Five of the six AST hits this corpus produced are false, and the rules are audited by
authority rather than exposed because they exist** (`artifact_evidence.RULE_AUTHORITY`,
four classes, unaudited defaults to D-invisible). `leak-model-selection-on-test` fired four
times on `rescaled_eval_metrics = test(model, eval_dataloader, ...)` — "eval" contains
"val" and `test` is the evaluation FUNCTION's name — and `cripple-augmentation-one-arm`
once on a branch that resizes a LoRA rank, because `new_transform_r` contains both an arm
token and an augmentation token.

**Only class A is reviewer-visible; class B FEEDS THE AUDITOR.** That a source pattern
matched is deterministic and what the match MEANS is not, and a class-B rule is defined by
needing a reading — so a B hit is a place to look, and it reaches a referee only after
exact source relocation and bounded semantic review through `artifact_review_driver`, at
the authority that channel earns. Seven of the ten have never fired on any real
repository, so rendering a first-ever B hit as a reviewer observation would put an
unmeasured detector in front of a human wearing a measured one's clothes.
Reviewer-visible hits on this corpus: **1 of 6**.

Measured over the four repository papers: 4 inspections completed, **6 level-1 facts**
(acl=1, apt-icml=2, cvpr=1, iclr=2), **0 broad implementation-correctness questions
settled**, 0 executions suppressed. The authors'-code auditor (`artifact_review_driver`)
has no production caller (see Known limitations) and was never invoked by this route on
any of the four; a DIRECT, non-pipeline invocation of it against all four — not
reproducible today by running `run.py review` — produced 8 concerns proposed, 7 code and 7
paper citations relocated, 1 identity ESTABLISHED / 6 AMBIGUOUS, **7 endpoint-verified
concerns and 0 level-2 mismatches**, which is real evidence the mechanism works and not a
result this route currently produces on its own. `docs/ARTIFACT_ROUTE_MEASUREMENT.md`
prints every fact, every concern, and why each refusal is the right one. **This whole
paragraph describes a PAST measured run, under code where the driver had no production
caller at all** — not the current code, where it is wired in (see above): the fresh
eight-paper run this revision produced dispatched the driver on all four repository papers
through `run.py review` itself and, on `apt-icml`, it once proposed one concern, relocated
both ends, and bound an ESTABLISHED identity, producing the corpus's first
`PAPER_ARTIFACT_MISMATCH` from the production route rather than a direct invocation — a
result since overwritten by later, unrelated reruns of the same route (see the Known
Limitations bullet on this route's missing caching), which cost the corpus a second fact
the same way in the 2026-09 closure pass — an unrelated rerun of `acl` re-dispatched this
route and it established one fact about `acl`'s checkout rather than the two it had
established before; the corpus's current, final state is 6 `ARTIFACT_FACT`s across the
four repository papers and 0 established mismatches.

## What a bounded prior-art search can establish, and what it cannot

**The original brief asked for novelty search, and the honest version of that capability
is asymmetric.** Finding an earlier work that appears to make the same contribution is a
concern a referee must look at. Finding nothing is not the opposite result — it is a
statement about a SEARCH, and the global literature is not enumerable, so no bounded
protocol can ever turn its own silence into evidence that a contribution is new.

**So there is nothing in this route that means "novel".** Not an authority, not a
disposition, not an evidence state. `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` is the
empty tuple and `PriorArtFact.establishes_novelty` is a membership test in it, which makes
the rule inexpressible rather than merely documented — the same device
`ARTIFACT_AUTHORITY` uses to keep "the reported result is false" off the artifact route.

| level | what it is about | what it needs |
|---|---|---|
| `BIBLIOGRAPHIC_FACT` | the WORK | an index record carrying a DOI, an arXiv id or an index id |
| `ENDPOINTS_VERIFIED_LITERATURE_CONCERN` | two real works | + an addressed novelty claim, a passage located in the text THIS harness retrieved, and a verified chronology; the OVERLAP is the reviewer's reading |
| `STRUCTURALLY_BOUND_PRIOR_ART` | the paper AND an earlier work | + a deterministic binding basis AND a priority claim to bind |
| *the paper is not novel* | — | **there is no value for this** |

**Chronology is the half that must be deterministic**, and it is the half a reading can
never move. The target's earliest public date comes from its own arXiv stamp where it
prints one, then Crossref, then OpenAlex, then — year-only — a publication statement on
page one; every offer is recorded beside the one chosen. A candidate later than that
cutoff is `POSTDATES_CUTOFF` and prior art for nothing; two dates known only to the year
are `CONTEMPORANEOUS_UNRESOLVED` rather than quietly ordered; an undated candidate is not
"probably earlier". A paper this harness cannot date makes no chronological claim at all.

**One paper is not four papers.** The same work arrives as an arXiv preprint, a Crossref
record and an OpenAlex work, and `canonical_id` folds them strongest-identifier-first,
keeping every alias and the EARLIEST date — prior art is about when a work became public,
not when a publisher printed it. The title fallback is conjunctive (title AND first author
AND year) because similar titles are common and identity is not.

**And a cited predecessor is a different concern from an omitted one.** §8 of the design,
and the distinction is an accusation: a work the paper already cites may still challenge a
novelty claim, and that is "the stated distinction may be insufficient", not "the authors
omitted prior art". A reference list extraction did not recover is
`BIBLIOGRAPHY_UNAVAILABLE` and never `APPARENTLY_UNCITED`.

**What "route exhausted" means here, precisely.** The DECLARED PROTOCOL completed: these
query families, against these indexes, to this depth, with this many candidates read per
claim, every retained candidate adjudicated to a typed state. All of those bounds are in
`SearchProtocol` and printed in the record. It does not mean the literature was searched,
and a provider this host could not authenticate to does not complete anything — an
unconfigured index is a CONFIGURATION outcome, which is why `protocol_completed` is false
whenever one did not answer.

Measured over the eight papers, both gates open: **23 claims searched, 306 queries across
two indexes, 5,692 records folded to 3,798 distinct works, 1,703 of them pre-cutoff, 143
read by a reviewer, 6 proposals — and 0 endpoint-verified concerns, 0 structurally bound
relations.** Six papers `SEARCH_COMPLETED_NO_MATCH_FOUND`, two `SEARCH_INCONCLUSIVE`, none
blocked. With the review gate CLOSED the same protocol runs and all eight still reach
`SEARCH_COMPLETED_NO_MATCH_FOUND` — which is what makes the model half assessable at all.

**The one concern that reached level 2 was the paper's own preprint**, found by hand and
refused rather than reported: `acl`'s arXiv posting, with every endpoint genuinely verified
and the reviewer's own note saying it looked like the authors' own. A paper is not prior
art for itself, and printing it would have been an accusation about their scholarship.
`docs/LITERATURE_MEASUREMENT.md` prints the per-paper table, the protocol, every refusal,
the run-to-run variance in the reviewer's own readings, and why each zero is right.

## What a controlled experiment this review designed can establish

**A referee's hardest questions are not about a number, and re-running the number cannot
answer them.** Two variables changed between the arms and the paper credits one of them;
the control the claim needs was never run; the arms saw different data budgets; the
protocol that produced the number is not the protocol the claim is about. The printed
value is consistent with every competing explanation at once, so reproducing it settles
nothing. What settles it is a NEW experiment that varies exactly one thing.

**This is the route `comparison.py` named and could not perform.** `COMPARISON_FOR_ROUTE`
has said since Step 5 that a `FOCUSED_VALIDATION_EXPERIMENT` is compared `BETWEEN_ARMS`,
and `RECONCILABLE` was one entry long: the route was declared, planned for, listed in
`UNIMPLEMENTED_ROUTES`, and refused before anything started with the sentence *"a run would
produce two numbers and no verdict"*. That sentence was true and is no longer.
`harness/between_arms.py` is the missing arithmetic and `harness/validation.py` is the
design layer in front of it.

**"Derivable from the paper" is the whole of the discipline.** An experiment that answers
the question by choosing an optimizer, a split, an augmentation strength, a threshold or a
schedule the paper never stated measures OUR choice, and its result would be a fact about
this review's reconstruction wearing the clothes of a fact about the paper.
`artifacts.NEVER_ASSUMED` lists those choices as data, so filling one in is a diff a reader
can see rather than a habit nobody wrote down, and eight ingredients must ALL bind:

| ingredient | where it comes from |
|---|---|
| `addressed_question` | a `ReviewQuestion` whose address re-resolves against the parsed paper |
| `competing_explanations` | the finding's own counter-explanations, carried, never invented |
| `arm_instantiation` | the claim graph's own COMPARISON node — REUSED, never rebuilt |
| `metric_identity` | the quantity, and the unit read off the CELL, never off the metric's name |
| `dataset_identity` | the benchmark AND the split, from a sentence that names the benchmark |
| `controlled_variables` | named, not assumed |
| `changed_variable` | exactly one; two things moving discriminates between neither |
| `settlement_condition` | declared BEFORE the run, with a tolerance that has a basis |

**Three halves are deterministic and one is a proposal.** The question, the arms, the
metric, the unit and the stated split are read off the document. What a document does not
print — and no parser recovers, because it is not there — is which variable the arms differ
in, which are held fixed, and what would settle it. A gated designer
(`SH_ALLOW_VALIDATION_DESIGN`, off) may propose those, and every configuration value it
offers must RELOCATE: `validation_driver.relocate_configuration` is `claims.mint` for a
scientific choice, and a value whose quotation is not in this paper or in the pinned
checkout is dropped and named.

**What the run may then conclude is bounded twice over.** `between_arms.compare` computes
the contrast — difference, relative difference (None rather than infinity when the control
measured zero), direction against the declared dead band, and interval overlap only where
both arms ran more than once — and `between_arms.authority_for` decides separately what the
contrast is entitled to say. A perfectly computed comparison on a design that deviates, or
on a provenance the reproduction ceiling refuses, carries `CONTROLLED_OBSERVATION` and
resolves nothing. `VALIDATION_DEFECT_ESTABLISHED` is its own disposition and not
`FAILED_REPRODUCTION`, because a reproduction re-runs an experiment the authors published
and this one re-runs an experiment they did not — and a referee reading the review has to
be able to see which happened.

Measured over the eight papers, offline, with the design gate shut: **710 discovered
objects, 23 focused-validation questions, 7 that reached the route, 2 planned, 2 designs
attempted and 2 `SPECIFICATION_BLOCKED` — 0 executable, 0 launched, 0 settled.** Both
blocked at `arm_instantiation`, and the reason is measured rather than guessed: both
targets carry an EMPTY metric and an EMPTY benchmark, so no COMPARISON node can bind to
them. The papers contain **74 such nodes** between them — their own contrasts are recovered
perfectly well — and none binds to the targets that raise attribution and control-presence
questions, because those come from PROSE findings naming no metric and no benchmark. That
is the same correspondence the claim-graph section already reports as semantic and
unstated. **Opening the design gate changes nothing**: the deterministic half refuses
first, at `no_arms`, and the designer is never called.
`docs/FOCUSED_VALIDATION_MEASUREMENT.md` prints the per-paper table and every refusal.

## Execution gates

| gate | env var | default | permits |
|---|---|---|---|
| network | `SH_ALLOW_NETWORK` | on | `git clone --depth 1` of the URL the paper advertises |
| synthesis | `SH_ALLOW_SYNTHESIS` | on | the planner authors `runs/<pid>/probe.py` from the paper |
| auto-audit | `SH_ALLOW_AUTO_AUDIT` | off | shelling out to a reviewer for the lenses |
| grading | `SH_ALLOW_GRADING` | off | a second, blinded reviewer per FATAL/MAJOR finding — zero tools, no filesystem access at all (`grade_driver.py`) |
| substantive verdict | `SH_ALLOW_SUBSTANTIVE_VERDICT` | off | one best-effort, never-retried, whole-paper opinion — printed, consumed by no threshold (`verdict_driver.py`) |
| claim links | `SH_ALLOW_CLAIM_LINKS` | off | one best-effort reading per paper pairing each headline claim with the evidence it rests on; both halves verified here (`claimlink_driver.py`) |
| authors' code | `SH_ALLOW_ARTIFACT_REVIEW` | off | one read-only pass over the PINNED checkout asking whether the code does what the paper says; `Read`+`Grep` only, every citation relocated (`artifact_review_driver.py`) |
| prior-art search | `SH_ALLOW_LITERATURE_SEARCH` | off | querying public scholarly indexes (OpenAlex, Crossref, arXiv) for work that predates the paper |
| literature review | `SH_ALLOW_LITERATURE_REVIEW` | off | one reading per novelty claim, comparing it against what those indexes returned; ZERO tools, so it cannot search on its own (`literature_driver.py`) |
| validation design | `SH_ALLOW_VALIDATION_DESIGN` | off | one reading per focused-validation target, naming the variable the arms differ in and the settlement rule; ZERO tools, every value relocated against the paper (`validation_driver.py`). It buys the DESIGN, never a run: execution still passes `SH_ALLOW_REPO_EXEC` and `backends.authorize` |
| install | `SH_ALLOW_INSTALL` | off | building `runs/<pid>/env` from the repo's requirements |
| sandbox | `SH_ALLOW_SANDBOX` | off | **leasing a remote Linux machine** and staging the audited commit into it (`harness/sandbox.py`) |
| execute | `SH_ALLOW_REPO_EXEC` | off | running the repository's own entrypoint |
| reimpl. driver | `SH_ALLOW_REIMPLEMENTATION_DRIVER` | off | delegating the governed reconstruction's authoring and its separate conformance verification |
| reimpl. exec | `SH_ALLOW_REIMPLEMENTATION_EXEC` | off | running a reconstruction that conformance verification bound; checked inside `authorize()` |
| diagnostic | `SH_DIAGNOSTIC_MODE` | off | running an inadmissible probe anyway and recording its result SEPARATELY, where it settles nothing |

`SH_AUDIT_BUDGET_CHARS` lives outside `Config` and now decides how much of the paper one
PASS carries, not how much of the paper a lens is shown — lowering it buys more parts, not
less paper. There is exactly one reader of the variable, `coverage.budget_chars`;
`stages/audit.budget_chars` delegates to it at call time, so the budget a review PRINTS
and the budget its prompts were built at cannot differ. `stages/audit.SECTION_BUDGET_CHARS`
is the default (70,000), not the live value.

`SH_MAX_TARGETS` (default 3) is a BUDGET, not a gate: which targets are worth pursuing is
`planner`'s decision and the order is `priority`'s, so lowering it drops the least useful
targets rather than an arbitrary subset. Raising it costs compute and changes no rule.

**Lens/part dispatch, grading and the artifact+literature routes now run concurrently,
bounded.** Before the 2026-09-19 simplification pass every one of these was a plain
sequential `for` loop over blocking subprocess calls — nothing in `harness/` used
`concurrent.futures`, `threading` (beyond `project_lock`'s own reentrancy bookkeeping) or
`asyncio` at all. `audit_driver.fill` and `grade_driver.fill` now dispatch their pending
units on a bounded `ThreadPoolExecutor` (`SH_AUDIT_CONCURRENCY` / `SH_GRADE_CONCURRENCY`,
default 4 each), and `stages/probe.run_probe` runs `artifact_stage.run_route` and
`literature_stage.run_route` concurrently on a 2-worker pool before applying their two
outcome-merges sequentially. None of this weakens isolation — it strengthens it, in the
same sense running four lenses as four separate operator terminals would: each unit
already gets its own subprocess, its own sandboxed cwd, its own pinned settings, and its
own prompt/output files, and the two routes read only `target_set` and write to disjoint
files. The one real hazard found was `state.append_log`'s plain `open(..., "a").write(...)`,
unsynchronized because every prior caller ran on one thread; it now takes an in-process
`threading.Lock`. Reading-part synthesis units still wait for their own lens's parts to
seal first (`stages/audit.py`'s existing deferred-dispatch bookkeeping), so a synthesis is
never dispatched concurrently with the parts it reads.

The audit lenses themselves run with `--allowedTools`, `--add-dir <papers dir>`, and a
sandboxed empty cwd (`audit_driver.py`) — a lens can `Read` the original PDF to settle a
table/figure/equation-dependent ambiguity (`SOURCE_FIDELITY` in the prompt), but not its
sibling lenses' files or the harness's own source. PDF inspection can KILL a candidate
or point at the right unit to cite; it can never BE the evidence — `verify_evidence`
still only accepts a quote it can re-check against the parsed doc (a table cell, a
section, a figure caption, or an extracted equation line). What a weaker citation bounds
is CONFIDENCE, in `harness.grading.EVIDENCE_CONFIDENCE_CEILING` — a caption reports none
of a figure's plotted values (LOW), equation extraction is lossy (MEDIUM), a cell or a
verbatim section quote is checkable as-is (HIGH) — and confidence is what caps severity.
A second independent check LIFTS the ceiling one step: the grader's own citation, or a
`recomputed_ok` calculation the harness re-verified operand by operand. So a figure
concern is neither silenced nor free — it earns what it can corroborate.

Backends: `local` (this machine), `modal` (a leased Linux sandbox — real, credentialed,
`can_execute=True`), `kaggle` and `colab` (declarations — published specs,
`can_execute=False`, `execute()` raises). `backends.select_for` matches the experiment's
declared demand against every profile, so a refusal can say *"a 16 GiB T4 would fit but
cannot be provisioned from here"*. Among backends that all meet every stated requirement
the one the operator NAMED wins, then the smallest sufficient — otherwise
`SH_EXEC_BACKEND` meant nothing whenever a laptop also fitted.

**Remote execution** (`SH_EXEC_BACKEND=modal`, `SH_ALLOW_SANDBOX=1`, `MODAL_TOKEN_ID` /
`MODAL_TOKEN_SECRET`, or `.env.sandbox` — see `.env.sandbox.example`). One sandbox is
leased **per paper**, not per command: `provision` fetches the audited commit by SHA,
builds the repository's declared stack, measures the machine and refuses it if it is
smaller than the reservation; every (seed, arm) of every target then runs in that same
session; `stages/probe.run` releases it in a `finally` and `run.py sandbox --release`
catches anything that outlived its run.

Nothing above the backend seam changes, and four things are asserted rather than assumed
(`tests/test_sandbox_backend.py`, 46 tests): **no silent local fallback** — every failure
to obtain or stage a machine refuses, and a host path in an argv is refused rather than
rewritten; **E2 is verified inside the sandbox by the same `verify_commit`**, through
`repo.GitTree`, with the same four outcomes and the same fail-closed branches, and a
missing session yields a tree that cannot be read rather than falling back to certifying
this disk; **the import check is delegated** to the interpreter that will run, since the
default resolver against a Linux venv path from Windows reports every dependency missing;
and **a reservation is not a measurement** — `resources()` is the request, `execute()`
stamps what the machine reported. There is deliberately no agent in the pod: the reference
implementation this restores ran one to *author* experiments, which measures the agent
rather than the paper.

## Known limitations

**Found by the 2026-09-16 release audit, each verified by reading the code rather than
by grep:**

- **`harness/alignment/trial.py` was deleted in the 2026-09-19 simplification pass.** It
  gated nothing (`may_trial`/`run_trial` had no caller outside their own self-check and
  `tests/test_alignment.py`), and the 2026-09-16 audit's argument for keeping it — "a
  complete, argued design with test coverage" — was an argument for a future capability,
  not a present one. `SH_ALLOW_ALIGNMENT_TRIAL`, `Config.allow_alignment_trial` and
  `artifacts.TrialResult` were removed with it; `ArgSpec` and the rest of
  `harness/alignment/` (`argparse_surface`, `candidates`, `configs`, `configuration`,
  `evaluator`) are unaffected and remain wired into `experiment_id.resolve_experiment`.
  Re-adding a `--help`-confirmation step is still exactly the ambiguous-candidate path's
  to make, if a future revision wants it.
- **`harness/artifact_review_driver.py` is no longer an orphaned gate.**
  `harness/stages/artifact.py`'s `run_route` now calls `artifact_review_driver.run()` once
  per paper (`_reviewer_facts`, `harness/stages/artifact.py:250`), gated on
  `cfg.allow_artifact_review` AND on `artifact_evidence.snapshot(...).audited` — a real,
  clean, pinned checkout — so a paper with no repository or an unaudited one still gets no
  call. What comes back is merged through the same `ArtifactFact`/`ArtifactInspection`
  channel the deterministic probes already use: unconditionally into the paper-level
  aggregate, and into a per-target inspection only when that target's own scope is
  `IMPLEMENTATION_CORRESPONDENCE`, so a checkout-wide finding can never answer an unrelated
  narrow bounded question. `SH_ALLOW_ARTIFACT_REVIEW` now genuinely gates something on the
  production path, and `route.json`'s `"proposed": 0` again means the reviewer ran and found
  nothing, not that it was never dispatched. `tests/test_artifact_route.py` proves the wiring
  itself (7 new tests). The fresh eight-paper run this revision produced is the first real
  exercise of it: the driver was dispatched on all four repository papers and, on
  `apt-icml`, proposed one config-literal disagreement (the paper's Table 2 / README-mapped
  T5-base CNN/DM run states 16 epochs at `P25:885-901`; the pinned
  `scripts/adaptpruning/t5_base_lm_adapt_cnndm_momentum.sh:49` sets 12), relocated it, and
  bound its experiment identity to the checkout's own README — the corpus's first
  `PAPER_ARTIFACT_MISMATCH` produced by the production route rather than a direct,
  non-pipeline invocation. It settled nothing about which of the two values the paper's
  reported numbers actually used; that would have remained a question for execution.

  **And this route has no caching, which cost the corpus that exact result — twice now.**
  `stages/artifact.run_route` calls `_reviewer_facts` — a live, temperature-bearing model
  subprocess with no fixed seed — on EVERY invocation with the gate open. The SEALING half
  of a governed reconstruction (`accept_reimplementation`/`ReimplementationConformance`,
  which persists a verified binding to disk and is never silently re-authored) is genuinely
  immune to this; nothing checks the artifact-review route the same way, and — discovered
  during the 2026-09 closure pass — nothing checks reconstruction's own EXECUTION the same
  way either (see the Known Limitations bullet on reconstruction execution's own missing
  result-caching). A later pass of this same revision re-ran `run.py review` for `apt-icml`
  three more times for reasons unrelated to artifact review (a reconstruction-sealing
  correction, a wording fix, a cache-resync fix), each with `SH_ALLOW_ARTIFACT_REVIEW=1`
  set, and each silently re-dispatched the driver and overwrote `apt-icml.route.json` with a
  fresh, independent proposal. The 16-vs-12-epochs mismatch above is consequently no longer
  reproducible from this run directory. The 2026-09 closure pass then lost a second fact the
  same way: an unrelated rerun of `acl` (testing a fix to its one open material question)
  re-dispatched this route and it established one fact about `acl`'s checkout rather than
  the two it had established before. The corpus's actual, final artifact-review state (read
  directly from `runs_final_2026-09-16/projects/<pid>/artifact/<pid>.route.json` on every
  repository paper) is **6 `ARTIFACT_FACT`s — `acl`=1, `apt-icml`=2, `cvpr`=1, `iclr`=2 — and
  0 established mismatches**, every `*_MISMATCH`-named probe this final dispatch proposed
  having `authority: NONE` (refused, exactly per the discipline above: the auditor's
  reading did not establish which config belongs to which experiment). Re-running the
  route again in the hope of reproducing the earlier hit would be selecting a result
  rather than reporting one, so this is not attempted; the honest statement is that a real
  mismatch was established once, under this same production wiring, and was lost to an
  unrelated rerun before this document could record it more permanently.

  **This gap is now closed.** `_reviewer_facts` (`harness/stages/artifact.py`) checks
  `artifact_review_driver.load(cfg, pid, commit=snap.commit, prompt_sha256=...)` — a
  read/verify guard that already existed, already had the correct hash+writer+commit
  mechanics, and had zero callers anywhere in the codebase before the 2026-09-19
  consolidation pass — before dispatching a fresh reviewer call. `load()` gained the
  `prompt_sha256` parameter its three siblings (`claimlink_driver.load`,
  `verdict_driver.load_accepted`, `reimplement_driver.load_accepted`) already had, so a
  cached inspection is reused only when both the pinned commit AND the paper-derived
  prompt inputs (title, method text, tree text, url, reported text) are unchanged;
  absent-on-either-side is never treated as a mismatch, matching those three siblings
  exactly. `route.json` itself is not separately sealed — its content is a deterministic
  function of the (now-cached) reviewer facts plus the current invocation's own target
  set, which is legitimately free to vary run to run, and `ArtifactInspection` carries no
  timestamp to seal against. `tests/test_artifact_route_caching.py` (7 tests) proves
  reuse across an identical commit/prompt and invalidation on a changed commit, method
  text, or title, and confirms a changed target-set's `statements` do NOT wrongly bust
  the cache (checked by temporarily forcing the cache lookup to fail: exactly the
  reuse-dependent tests failed, exactly the invalidation tests still passed). The
  reconstruction-execution half of this same gap (next bullet) remains open.
- **`stages/report.unearned_support_language` is enforced at TEST time, not at run time.**
  It is the "GREEN may not borrow the words of evidence it does not have" guard and the
  renderer never calls it: `tests/test_guarantees.py` and `tests/test_reimplementation_path.py`
  run it over rendered output as an independent check. That is a defensible design (a linter
  over the renderer beats a filter inside it) and it is not what "the report refuses to say
  it" would mean. Same for `stages/report.material_failures`, whose body is `return []`: it
  makes invariant 8 statically checkable and is not on any path.
- **`json_metric` was deleted in the 2026-09-19 simplification pass.** It was proven
  unused rather than merely diagnostic-only — nothing consumed its return value, and
  `local_exec.StartupEvidence.saw_json_metric` (a real, separate, still-live raw-stdout
  scan for JSON-shaped startup evidence) is not to be confused with it. Positional
  last-JSON-wins now has no path to a reconciliation.
- **`docs/HARNESS_ARCHITECTURE.md` §3 is stale in three ways** and is superseded by the
  phase table above: it lists six phases rather than nine, says only `audit` is retryable
  when `controller.RETRYABLE` is `("audit", "grade")`, and says `--force-probe` rewinds to
  `probe` when `controller` rewinds to `discover` (with a comment explaining that rewinding
  to `probe` was the bug).
- **`harness/materiality.py`, `harness/disposition.py` and `harness/container.py` are not
  described anywhere in the prose above beyond the invariants that reference them**, even
  though `disposition` supplies the first line `run.py` prints and `container` is the
  backend the v2 run actually selected. `docs/CODEBASE_CLAIM_MAP.md` covers them.
- **Three more modules join that list, added by the 2026-09-19 consolidation pass**:
  `harness/delegation.py` decides which delegation MODE an environment offers and owns the
  provenance vocabulary (`WRITTEN_BY`, `provenance_record`) plus the small per-role
  resolution helpers (`resolve_model`, `resolve_reviewer_exe`, `check_command`) — it
  carries its own hard architectural-purity test restricting its imports to
  `{shutil, inspect, __future__}`, enforcing that it never owns execution mechanics.
  `harness/reviewer_cli.py` is the sibling that owns exactly that execution mechanics half
  for a CLI-subprocess delegate (confinement flags, pinned settings, envelope parsing,
  kill-tree teardown, prompt fingerprinting, failure classification) — all 8
  `*_driver.py` files import it; before this pass, 7 of them imported the same names
  directly from `harness/audit_driver.py`, which had accidentally become the shared
  library `delegation.py` was designed to be. `harness/sealing.py` is the third: the
  write-then-verify content-hash mechanism (`seal`/`verify_seal`) that 9 independent
  accept/seal instances across `stages/audit.py`, `stages/probe.py`, `stages/grade.py`,
  and 5 of the `*_driver.py` files each reimplemented on their own before this pass, each
  now layering its own schema-specific extra check (a prompt-freshness check, a commit
  match, an independent-verification cross-check) on top of the shared core exactly the
  way `stages/audit.py`'s `unit_is_accepted` already layered one on `_sealed`.
- **A round of scattered dead code was found and removed in the 2026-09-19 simplification
  pass, beyond `alignment/trial.py` and `json_metric` above.**
  `harness/validation_driver.py`'s `seal`/`load`/`_paths` and their `WRITERS` constant were
  deleted — the module's own docstring already called `seal` "dead code today," and nothing
  ever called `load`; `FocusedValidation`'s persistence for this route remains exactly what
  it was, the in-memory accumulation `stages/probe.py` already does plus the plain
  diagnostic `validation.driver.json` it already writes. `DiscoveredObject.discovery_confidence`
  (declared, never set by `discovery._object`, never read anywhere) was deleted from
  `artifacts.py`; `_Base`'s `extra="allow"` means old run artifacts that still carry the key
  remain readable. `harness.controller.PhaseOutcome.advances` (an orphaned property nothing
  called) and `harness.config.Config.device` / `.validation_max_designs` (two declared,
  never-read config fields — `SH_DEVICE` and `SH_VALIDATION_MAX_DESIGNS` never gated or
  budgeted anything) were also removed. The `src.suffix.lower() == ".pdf" or src.exists()`
  predicate that `controller.open_case`, `controller._phase_ingest` and `run.py cmd_stage`
  each carried as an identical, independently-typed copy is now the one function
  `controller.is_new_pdf_source`, called from all three. Two items surfaced but were
  deliberately NOT deleted after closer reading found them load-bearing:
  `state.add_cost`/`append_log`'s `cost_usd` threading has no production caller either, but
  is the direct subject of four real concurrency-safety regression tests in
  `tests/test_state_locking.py` (a lost-update race and a reentrant-self-deadlock proof),
  so it stays; `harness/claimgraph.py`'s `compare_with_existing`/`Delta` are reachable only
  from the module's own `python -m harness.claimgraph <pid>` CLI and self-check, but the
  module's own docstring frames them as the deliberately-kept measurement a future
  `discovery._centrality` rewrite would need first, which is a project decision rather than
  dead weight.

**THE SEVEN SHIPPED REVIEWS IN `projects/` PREDATE EVERYTHING BELOW.** They were produced
under older code and are the record of that run. Nothing in this section has been
re-measured on a rerun, and several corpus numbers those reviews print are now known to be
wrong in the paper's favour or in ours:

| what the shipped reviews say | what a rerun produces | why |
|---|---|---|
| 20 targets "settled a question about the paper" | 0 | all 20 were citation re-verifications; see invariant 20 |
| 52 targets "could not build a re-derivable address" | 5 | 39 of those sentences were false; see the axes section |
| `target_addressability_rate` 0.93 | `addressed_rate` 0.33 | the old rate divided our object list by itself |
| "4 independent lens(es) read the paper" | the same, plus the fraction | the lenses were shown 38-75% of the extracted prose |
| a panel of four models | four processes, two model names | `LENSES[lens]["model"]` was declared and never read |

Re-running them is the eight-paper evaluation, and **it has now completed.** It stopped
once, on 2026-09-08, in the S2 audit phase on a provider session limit
(`docs/RUN_2026-09-08_eight_paper.md`); it resumed and stopped again on 2026-09-09 at
13/32 lenses on a second, session-local delegation limit (`docs/RUN_2026-09-09_boundary.md`);
it resumed a second time once both windows reset and ran the full pipeline — all 32 lens
audits, all 38 independent grades, all 8 whole-paper assessments, execution and
reconciliation, and the four report layers — to completion for all 8 papers, with zero
further limit events. See `docs/RUN_2026-09-09_complete.md`, which is now the authoritative
record and reports every one of those states separately, including one real defect this
run found and fixed in its own funnel bookkeeping (§6 of that document). The seven
September reviews remain archived at `projects/_run_2026-09_v1/` and are not superseded by
this corpus — they are a different run, kept for comparison, not carried forward as if they
were current.

- **The eight-paper set contains TWO CVPR papers, deliberately.** `papers/CVPR.pdf`
  (`7bd4c8336fb9`, "WeatherGen") is not a NEW paper — it is the document already reviewed
  as `projects/cvpr`, unmodified since 2026-08-23. The eighth paper is
  `papers/0c06a98d7c818f6f.pdf` (`d6295997a2ed`), and it **is also a CVPR paper**: it
  carries the CVF open-access watermark verbatim, exactly as the other does. They are
  distinct by hash, title, page count and proceedings folio (17019 vs 41815), and neither
  may be deduplicated against the other merely for sharing a venue.

  An earlier note here called `0c06a98d7c818f6f` "a spatial transcriptomics paper, not a
  CVPR selection". That inferred a VENUE from a SUBJECT, which is the shape of unfounded
  inference this whole system exists to catch, and the document contradicts it. The year
  is evidenced rather than proven: it cites work up to 2025 and sits at the far higher
  folio of a much larger volume, and the watermark carries no year.
- **The document-integrity layer claims nothing about any corpus paper.** Measured over
  the seven shipped documents it emits 73 observations and every single one is
  `about="EXTRACTION"` — facts about what this harness recovered. That is the correct and
  conservative answer, and it also means the `about="PAPER"` half of the layer is exercised
  by fixtures only. The route from a PAPER observation with a DEMONSTRATED scientific
  consequence into the findings channel is deliberately not implemented.
- **`CROSSREF_UNRESOLVED` can only ever be an EXTRACTION observation.** A naive existence
  check produced twelve false "missing table/equation" claims and zero true ones on this
  corpus. Until extraction recovers a complete label set for a class, "the prose cites
  Table 12 and we recovered no table labelled 12" is all that can honestly be said, and
  that is what it says.
- **No paper has yet been RED under the binary rule at REPORT time.** RED needs
  deterministic paper arithmetic or a failed reproduction from an admissible provenance;
  the eight-paper corpus
  produced neither in its final report. It came closer than the archived September run
  did include a lens-asserted FATAL, but that concern never had rejection authority.
  RED from a real, authors'-repository
  execution remains proven only by fixtures
  (`tests/test_autonomous_review_e2e.py`): every execution the eight-paper run produced
  was `SYNTHESIZED_DIAGNOSTIC` (a harness-authored noise-band check), which the
  provenance ceiling forbids from convicting or acquitting — see
  `docs/RUN_2026-09-09_complete.md` §6.
- **The corpus lens files are no longer all `manual_accept`.** The eight-paper run's 32
  lens audits record real delegation provenance — 30 `SESSION_SUBAGENT` (an isolated
  subagent the controlling session dispatched, one per lens, real context isolation but no
  provable filesystem sandbox) and 2 `CLI_SUBPROCESS` (a reviewer process this harness
  spawned and confined itself, the only mode entitled to report an enforced tool policy),
  non-pooled: `delegation.summarise` reports `homogeneous: false` rather than describing
  either as the method. The September archive's 28 `manual_accept` lenses are unchanged
  and remain a property of that earlier run, not of this one.

  The per-lens model heterogeneity fix is now verified on live output rather than only in
  code: the two `CLI_SUBPROCESS` lenses ran with `--model` actually reaching the CLI —
  `overclaim` (declared `opus`) reported `claude-opus-5`, `protocol` (declared `sonnet`)
  reported `claude-sonnet-5` — with distinct enforced `--disallowedTools` policies (11 vs
  12 tools denied). **The ceiling, stated as before: this is one vendor's CLI, so
  "independent" means separate contexts and, where CLI-run, two distinct model names. It
  is not cross-family diversity.** The `SESSION_SUBAGENT` lenses cannot report an enforced
  tool policy at all — `tool_policy_provable_for` is 2 of 32, exactly the CLI ones — real
  context isolation without a provable sandbox is what that mode honestly claims.

  **This paragraph describes that archived run and is not the live policy.** As of the
  2026-09-18 closure pass, `overclaim`'s declared model (`prompts/audit.py`), the blinded
  grader's (`prompts/grade.py`) and the whole-paper verdict reader's (`prompts/verdict.py`)
  are all `sonnet` — an explicit operator decision to stop spending the stronger model by
  default, made once it was clear the two-model-names property above was a ceiling
  ("one vendor's CLI") rather than real cross-family diversity, and not worth its cost at
  this system's scale. A future run that still wants that property back can override any
  role's model per invocation (`SH_GRADE_MODEL`, `SH_VERDICT_MODEL`, and the equivalent for
  a lens); nothing about the mechanism above changed, only the declared defaults.

  **This paragraph in turn describes the policy through the 2026-09-18 closure pass and is
  no longer the live one.** The 2026-09-19 simplification pass re-examined the blanket
  "no role defaults to `haiku`" claim above against what each role is actually asked to
  produce, rather than against what its name suggests, and found the claim held for the
  roles with real decision authority but not for two roles this harness's own prompt-file
  docstrings already called mechanical: `prompts.literature.QUERY_ROLE_SPEC` (proposing up
  to six search strings from a closed 6-family taxonomy — "a language task... the expensive
  model buys nothing here that verification does not already guarantee," `literature.py`'s
  own words, predating this pass) and `prompts.claimlink.ROLE_SPEC` ("a location task, not
  a judgement task," `claimlink.py`'s own words). Both now default to `haiku`, each
  re-verified in full downstream exactly as before (a query is checked only by what it
  retrieves; a claim-link pairing is refused outright on any citation, arithmetic or
  chronology mismatch), so nothing about the ADMISSIBILITY of what either role produces
  changed — only which tier reads the text first. `prompts.validation.DESIGN_ROLE_SPEC`
  (naming the variable two arms differ in) moved to `haiku` on the same argument, made in
  its own docstring before this pass and left unacted on until now. Every role that
  decides something no deterministic check later re-derives — the four audit lenses, the
  blinded grader, the whole-paper verdict, the literature COMPARISON reader (real
  reading-comprehension judgement about novelty overlap, not term extraction), the
  authors'-code auditor, and the reconstruction generator (writing code, not classifying
  text, and the one candidate this pass treated with more caution than the others'
  docstrings argued for) — is unchanged at `sonnet`. `SH_LITERATURE_MODEL` still overrides
  only the reader; the query proposer and the validation designer have no override lever
  of their own, by design, matching their own declared defaults rather than an operator's
  ambient config.

  **And two more roles were found to have the identical declared-vs-actual drift the
  2026-09-18 fix above closed for `LENSES[lens]["model"]`.**
  `prompts.claimlink.ROLE_SPEC["tools"]` declared `()` while `claimlink_driver.link_confinement`
  actually granted `("Read",)` — both are now `("Read",)`, and the confinement builder
  reads `tools` from the spec instead of hardcoding it, so the two cannot drift apart
  again. `prompts.grade.ROLE_SPEC`, `prompts.verdict.ROLE_SPEC` and
  `prompts.reimplement.ROLE_SPEC` all declared `tools`/`bare` fields their drivers never
  read (each hardcoded `allowed_tools=()`/`bare=True` directly); all three confinement
  builders now read from the spec too. Harmless today because the hardcoded values
  happened to agree, but it was the same duplicated-source-of-truth shape that produced
  the wrong "a panel of four models" claim once already.
- **The grader and the assessor have now run on a real corpus, to 100% coverage.** All 38
  in-scope candidates across the eight papers were independently, blindly graded
  (`grade_coverage` is `N of N` on every paper, 0 pending anywhere), and all 8 papers
  carry a `substantive_verdict` — both delegated via isolated subagents
  (`SESSION_SUBAGENT`, homogeneous within each class). Two of the eight whole-paper reads
  disagree sharply enough with the deterministic table to flag CONTESTED
  (`5993d35ff0996b52`, `iclr`) — printed, consumed by no threshold, exactly as designed.
  Grading demoting a lens-asserted FATAL to non-FATAL (above) is the first real evidence
  that turning grading ON changes a corpus outcome rather than merely being provable in
  principle.
- **The eight-paper run found and fixed a real bookkeeping defect: a second `review` pass
  over an already-executed paper silently dropped that execution from the funnel.**
  `discover` recomputes `discovery/targets.json` from scratch on every invocation — right,
  since a new grade can change what is checkable — but it runs before `probe` and knows
  nothing about a prior execution; `probe`'s own cache guard then skipped re-attaching the
  executed target's outcome onto the freshly recomputed set. Measured on this corpus:
  `evaluate`'s funnel read `actually launched a process: 0` and `ran to a reconciliation: 0`
  across a run that had genuinely executed 16 processes across 7 papers — the report text
  itself was unaffected (it reads `runs/<pid>/probe_results.json` directly), but every
  count that reads `target_set.outcomes` was wrong. Fixed by
  `harness.stages.probe.resync_cached_outcomes`, called from `controller._phase_probe`'s
  cached branch, which re-attaches cached `ProbeResult`s without spending a new execution;
  regression test: `test_a_second_pass_does_not_lose_the_first_executions_outcome`. Found
  by re-running the real corpus a second time to pick up independent grading — a usage
  pattern the architecture always allowed but had not been exercised end to end before.
- **`scientific_class` falls back to the lens name.** `taxonomy.classify` is
  most-specific-first: a finding that declared its own `discrepancy_type` or
  `baseline_class` yields a class derived from that, and one that declared neither falls
  through to which lens raised it. In those cases the count is partly a count of what
  each lens wrote. `harness/evaluation.LIMITATIONS` carries the same caveat inside the
  artifact, and the question templates inherit the same ordering.
- **A merged question reports its highest-priority target, not its luckiest.** A question
  raised by two findings has one target per finding, and they can end differently. The
  question reports the first in priority order, so it stays open when its central target
  is blocked even if a supporting one settled. Conservative on purpose; `evidence_refs`
  lists every target so nothing is hidden — and that mitigation did NOT work until now,
  because `discovery` keyed the question lookup on `from_finding`, the first source only,
  so a merged question's second object got `question_id=""` and never entered
  `evidence_refs` at all. The map is built over `source_finding_ids` now.

  What merges is also different. The key was the lens's own `evidence_ref`, and the prompt
  instructs a lens to write `p7` for a prose claim — `p7` is a PAGE. Measured over the
  shipped corpus: 75 of 98 findings write a `p<N>` ref, and 3 of the 6 resulting merges are
  FALSE, two of them RAISING the merged question's materiality because a merge takes the
  maximum its sources asserted. The key is now the address `harness.claims` minted, and a
  finding whose address did not resolve merges with nothing.
- **The prose path runs end to end on a real paper, and stops at identity for a
  stateable reason.** With the gates open, FinChain's composition claim — which exists
  only in the paper's prose — is discovered (`P9:0-155`), addressed, its arithmetic
  re-verified (58*5*10 = 2,900), prioritised as an executable target, and pursued against
  a cloned, commit-verified, statically audited checkout. `experiment_id` then refuses:
  *"none of the 3 advertised command(s) emits a count, so nothing in this checkout
  produces the total the paper states."* That is true of the repository — it advertises a
  single-template generator, an evaluation script and an aggregation script, and
  producing the benchmark means looping all 58 generators, which no advertised command
  does. **The refusal is the guarantee working.** Assembling that loop is one line and
  the harness will not write it, because a number from a command the authors never
  published measures our loop rather than their artifact. The campaign's 53/2,650 result
  came from exactly such an operator-supplied loop over the authors' own generators.
- **RED from an autonomous execution is proven on fixtures, not yet on a published
  paper.** `tests/test_autonomous_review_e2e.py` drives discovery → address → identity →
  authorization → reconciliation → RED, and the reproduction direction too.
- **Prose identity binds COUNTS only.** `experiment_id._prose_metric` accepts a
  population a sentence names and refuses everything else, because a prose-stated accuracy
  has no column header, no basis and no baseline row to bind against. An accuracy claimed
  only in text is discovered and is not executable.
- **The artifact route has established ZERO paper/artifact mismatches on real papers, and
  it settles ZERO broad implementation-correctness questions.** Measured over the four
  repository papers: 4 inspections completed, 6 narrow level-1 facts established
  (acl=1, apt-icml=2, cvpr=1, iclr=2), 0 broad questions settled. The authors'-code auditor
  (`artifact_review_driver`) has no production caller (see above) and was never invoked by
  this route on any of the four; a DIRECT, non-pipeline invocation of it against all four —
  not reproducible today by running `run.py review` — produced 8 concerns proposed, 7 code
  citations and 7 paper citations relocated, 1 experiment identity ESTABLISHED and 6
  AMBIGUOUS, **7 endpoint-verified concerns and 0 level-2 mismatches**.
  Six of the seven are refused because which config belongs to which experiment is the
  auditor's reading; the seventh had an ESTABLISHED identity and a paper value the harness
  could not re-read from the quoted span. Both refusals are correct and neither should be
  relaxed to make the route produce an interesting number. Seven of the ten AST rules did
  not fire on any of the four, so their class-B classification rests on their structure
  rather than a measurement; a rule firing for the first time on a fifth paper should be
  re-audited before its output is believed.
  `docs/ARTIFACT_ROUTE_MEASUREMENT.md` has the whole record, including every concern.
  **This bullet, like the paragraph it summarises, describes a PAST measured run under code
  where the driver had no production caller — not the current code.** Now that
  `stages/artifact.run_route` calls it, the fresh eight-paper run genuinely dispatched the
  driver through `run.py review` on all four repository papers and, on `apt-icml`, it once
  established the corpus's first production-route `PAPER_ARTIFACT_MISMATCH`: the paper's
  Table 2 / README-mapped T5-base CNN/DM run states 16 epochs, the pinned
  `t5_base_lm_adapt_cnndm_momentum.sh` sets 12, and the identity bound to the checkout's own
  README. A bounded config-literal disagreement, established once, would still not have been
  a broad implementation-correctness verdict, and would have settled nothing about which
  value the paper's own reported numbers were actually produced under — and this exact
  result no longer holds: the route has no caching (see the Known Limitations bullet above
  on this route's missing caching discipline), and this session's own later, unrelated
  reruns of `run.py review` for `apt-icml` silently re-dispatched the same live, seedless
  model call and overwrote it — and, in the 2026-09 closure pass, a same-session rerun of
  `acl` for an unrelated investigation did the same thing to `acl`'s own count. The corpus's
  current, final artifact-review state, read directly off every repository paper's
  `artifact/<pid>.route.json`, is 6 `ARTIFACT_FACT`s (`acl`=1, `apt-icml`=2, `cvpr`=1,
  `iclr`=2) and 0 established mismatches.
- **The prior-art route runs and produces NO novelty conclusion, which is the design and
  not a shortfall.** Measured over the eight-paper corpus with both gates open: 23 claims
  searched, 306 queries across two indexes, 5,692 records folded to 3,798 distinct works,
  1,703 of them pre-cutoff, 143 read by a reviewer, 6 proposals — and **0 endpoint-verified
  concerns, 0 structurally bound relations**. Six papers SEARCH_COMPLETED_NO_MATCH_FOUND,
  two SEARCH_INCONCLUSIVE, none blocked. That means the declared protocol completed and
  nothing qualified; it does not mean any contribution is new. Four limits bound the route,
  and all four are OURS rather than the papers'.

  **A paper is not prior art for itself**, and the corpus's first endpoint-verified concern
  was `acl`'s own arXiv preprint — every endpoint genuinely verified, the reviewer's own
  note saying it looked like the authors' preprint, and reporting it would have been an
  accusation about their scholarship. `literature.same_work` refuses a candidate carrying
  an identifier the paper prints on its own first page or the paper's own title, and
  `candidate_may_be_the_target_itself` refuses the case this harness cannot decide.

  **The cutoff cannot always be established.** Two of the eight cannot be dated at all, and
  for them no chronological claim is made in either direction. The cause is extraction:
  `doc.title` comes back as an author line or as publication boilerplate, and
  `literature.target_title` refuses to search an index with one — a wrong title does not
  fail, it succeeds with somebody else's paper and then dates this one.

  **OpenAlex now meters its API** and returned "Insufficient budget ... Resets at midnight
  UTC" once this host's free daily allowance was spent. A 429 is two different facts and
  the body says which; that one is recorded as UNAUTHENTICATED, not as a completed query,
  so the measurement's declared protocol is crossref + arxiv.

  **And the reviewer is not reproducible.** Across two runs the same verified candidate —
  K-prune, arXiv:2308.03449, genuinely earlier work on structured pruning of pretrained
  encoder LMs — came back POSSIBLE_OMITTED_BASELINE once and
  RELATED_BUT_MATERIALLY_DIFFERENT the next. Both ends verified identically both times;
  what varied was the reading, which is exactly why a relation is recorded as a reading.
  `docs/LITERATURE_MEASUREMENT.md` has the per-paper table, every refusal and the variance.
- **The focused-validation route has never executed on a real paper, and the reason is
  ours rather than the papers'.** Measured over the eight-paper corpus with the design gate
  shut: 23 focused-validation questions identified, 7 reached the route, 5 of those refused
  by `planner` on `specification_complete`, 2 designs attempted and both
  `SPECIFICATION_BLOCKED` — 0 executable, 0 launched, 0 reconciled, 0 settled. The
  between-arms arithmetic, the authority ladder and the conformance rule are therefore
  proven on FIXTURES, not on a published paper, exactly as RED-from-execution is.

  **The first unbound ingredient is `arm_instantiation`**, and it is unbound because both
  targets carry an empty `metric` and an empty `experiment`: a design reuses the claim
  graph's COMPARISON node, keyed `comparison:<metric>|<benchmark>`, and with neither field
  there is nothing to bind. The papers hold 74 comparison nodes between them, so the
  contrasts ARE recovered; what is not recovered is which prose concern is about which
  contrast, which is the semantic correspondence the claim-graph section already reports
  the document does not print.

  **16 of the 23 questions reached no route at all**, because a focused validation varies
  one thing in the AUTHORS' OWN code and those papers published none. Building both arms
  from nothing is a reconstruction, which is a different route with its own governance.

  **And opening `SH_ALLOW_VALIDATION_DESIGN` changes none of it** — verified by running
  both gate states over the same targets: the deterministic half blocks first with the
  driver record reading `no_arms`, so the designer is never called. A route whose model
  half cannot be reached is a route whose measurement is entirely deterministic, which is
  the honest thing to say about this one today.
- **No adjudicated ground truth exists for this corpus.** `harness/evaluation.py`
  therefore reports no precision, recall, or agreement-with-humans number, and says so in
  the artifact itself. Reviewer accuracy is unmeasured, not measured-and-good.
- **PATH B is eligibility-only so far.** `harness/reimplement.py` decides whether a paper
  with no published code says enough to rebuild, and writes the brief when it does. No
  independent reimplementation has actually been written and sealed through
  `run.py accept`, so the INDEPENDENT_REIMPLEMENTATION provenance is exercised by tests
  and by the `driver` path, not yet end to end from a real no-code paper.
- **A real container-execution staging gap existed and is now fixed.** `ContainerBackend`
  had no translation step between the host paths `local_exec.write_probe`/`resolve_command`
  build and the `/work`-rooted namespace `execute()` requires — every governed
  reconstruction that reached execution refused with `refusing to run: cwd '...' is not
  inside '/work'` (`harness/backends.py`'s `ContainerBackend.execute`), which is why every
  reconstruction this harness had run before this fix ended INCONCLUSIVE for an
  infrastructure reason rather than a scientific one. `ExecutionBackend.stage()` (default
  identity, overridden by `ContainerBackend.stage`) now does this translation, and
  `ContainerBackend.provision()` persists the image and mount it built
  (`self._host_mount, self._image_name, self._pid`) so `execute()` uses the same ones
  instead of always falling back to `DEFAULT_IMAGE`; a reconstruction with no environment of
  its own falls back to the image's bare `python3` rather than this harness's own host
  interpreter. Verified on real reruns of `acl` and `sanchez24a-icml`: reconstructions now
  genuinely execute inside their assigned container (40 processes launched across the two
  papers this run — 5 on `acl`'s one executed reconstruction, 35 across `sanchez24a-icml`'s
  seven) and reach honest `INCONCLUSIVE` dispositions for real reasons — zero seed-to-seed
  variance leaving the reconciler's own noise band with nothing to test against, or a
  dependency (`torch` or `numpy`) missing from the fallback interpreter — never the old
  infrastructure refusal. The fix applies equally to `apt-icml`'s execution path, but no
  `apt-icml` reconstruction reached execution in the run this corpus now reflects: a later
  verification pass found that all five of `apt-icml`'s sealed reconstructions had their
  `comparison_target` wrongly bound, at sealing time, to a table caption unrelated to the
  cited cell plus a dict of the paper's own literal cited values, when each reconstruction
  script's own docstring already states that its output must never be compared against those
  values, because the paper never specifies the third-party LoRA+Prune/Mask-Tuning baseline's
  own training procedure. The conformance checker verifies that a quotation is
  verbatim-present and does not judge what it is being compared against, so it had
  mechanically accepted the binding, and the reconciler went on to compare the
  reconstruction's output against the cited cell anyway — a since-corrected defect in what
  was proposed as a binding, not in the checker, which is working as designed. Correcting the
  binding rather than relaxing the non-invention/binding contract left all five honestly
  `unbound`, and re-running this paper's probe stage lets `authorize()` correctly refuse all
  five at `CONFORMANCE_BLOCKED` before any process starts, an unstated baseline training
  procedure the non-invention/binding contract refuses to invent.
- **The seven `torch`/`numpy` `ModuleNotFoundError`s above are now fixed at the code
  level, and the fix was correctly judged impracticable to exercise against those seven
  targets this pass.** `harness/container.py`'s `RECONSTRUCTION_BASELINE_REQUIREMENTS`
  (`torch`, `numpy`, `transformers`, `datasets` — fixed and declared, never inferred from
  a script's own imports) is now installed by `stages/probe.attempt_reimplementation_fallback`
  into any reconstruction's venv that the checkout itself supplied no environment for,
  and only for `INDEPENDENT_RECONSTRUCTION` — an `AUTHOR_CODE_EXECUTION` venv is untouched,
  still exactly what the checkout declared. `tests/test_reconstruction_environment.py`
  proves the mechanism for real against a live container (5 tests, one of them installing
  a real package into a real venv and importing it back). It was NOT run against the
  seven affected `sanchez24a-icml` targets: each of their scripts loads
  `EleutherAI/pythia-1.4b` (several GB) fresh from HuggingFace Hub, `docker run --rm`
  discards the container filesystem after every invocation, and the model cache is not
  part of the bind-mounted `/work` directory — so five seeds across seven targets would
  re-download the full model 35 times rather than once, an estimated bandwidth and
  wall-clock cost this pass judged genuinely impracticable rather than merely
  inconvenient. Making the cache persist (redirecting `HF_HOME` into the bind-mounted
  `/work` directory, which DOES survive between invocations) is the identified next step
  and is not implemented here.
- **Reconstruction EXECUTION has no result-caching, the same gap the artifact-review
  route already discloses above, discovered the same way: by losing a result to it.**
  `stages/probe.attempt_reimplementation_fallback` reuses a sealed SCRIPT and its
  CONFORMANCE verification (`reimplement_driver.load_accepted`) rather than re-authoring
  either — that half of the "seal-once" discipline is real — but it re-executes that
  sealed script, through the ordinary `backends.authorize()` path, on every probe
  invocation that reaches this branch. A same-session rerun of `acl` for an unrelated
  reason (testing a fix to Section 3's one open material question) re-attempted execution
  for all three of `acl`'s sealed reconstruction targets while this host's Docker daemon
  happened to be transiently unreachable, and `backends.authorize()` reports backend
  reachability before it reports conformance (see its own docstring: "the reported reason
  is the one an operator can act on first"), so all three were overwritten with a generic
  `ENVIRONMENT_BLOCKED`/backend-unavailable disposition — silently discarding the
  previously-established zero-variance execution result and two conformance refusals.
  Recovered by re-deriving each outcome directly from the untouched underlying evidence
  (`execution.jsonl`, the sealed conformance JSON) rather than by re-executing, and
  verified byte-identical to the pre-incident state. The gap this reveals is real and is
  not fixed here: a future revision should give reconstruction EXECUTION the same
  seal-once discipline its SCRIPT and CONFORMANCE already have, so a probe re-invocation
  for an unrelated reason cannot silently overwrite a completed result with a transient
  environment fact.

  **Still open, and the 2026-09-19 consolidation pass found the concrete reason a naive
  fix would be unsound rather than merely unfinished.** The obvious cache key — script
  `content_sha256` + seeds + backend + interpreter + commit — does NOT determine what
  actually executes for the exact case this gap already bit: a container backend whose
  checkout supplies no environment of its own gets `torch`/`numpy`/`transformers`/
  `datasets` installed by `container.install_reconstruction_baseline` with **no version
  pins and no lockfile** (`container.RECONSTRUCTION_BASELINE_REQUIREMENTS` is four bare
  package names). Two provisioning events separated in time can resolve different actual
  releases from PyPI while every field of the proposed key stays byte-identical, and
  nothing records what was actually installed afterward (`ExecutionRecord.environment`
  captures backend/platform/Docker version, not package versions) — so a drift is not
  just possible, it is currently undetectable even in hindsight. A cache keyed this way
  could therefore reuse a result produced under a materially different dependency set and
  report it as the same experiment, which is a worse failure than today's always-
  re-execute behavior. Fixing this for real needs either pinning the baseline
  requirements (a lockfile, not four bare names) or recording the resolved versions
  alongside the execution result so a cache key can include them — neither is implemented
  here; this pass deliberately did not ship a caching key it had concrete evidence could
  alias two different executions as one.
- `severity` is still model-asserted by the lens that wrote it. What changed:
  `harness/grading.py` now runs a second, blinded reviewer over every FATAL/MAJOR
  candidate (`--auto-grade`) and independently re-verifies its own pass-B work
  (falsification/steelman non-degeneracy, recomputed arithmetic) even with grading off.
  Neither model certifies itself — `Finding.counted_severity`, `finding_class`,
  `grader_evidence_class` etc. are harness-written from a pure derivation table, never
  read from a lens or grader file. The ceiling that remains: the grader is still a
  model, and its own judgement is not machine-checked, only its citation and the
  derivation are. `stages/report.py`'s `## Independent grading` / `## Severity caveat` /
  `## Reviewer self-audit` sections say, per paper, how much of the verdict still rests
  on ungraded or prose-only assertions, and which parts of the discipline this particular
  review did not exercise.
- The whole-paper judgement (`## Whole-paper assessment`) is reasoned rather than counted
  — the one thing a threshold table structurally cannot do — and it is **printed, never
  counted**. Its only consequence is a CONTESTED flag and `run.py` exit 3 when it
  disagrees sharply with the table. That is the deliberate settlement between "do not
  derive the verdict by counting findings" and invariant #8; a model that could write the
  colour would make every gate under it advisory.
- The review triage is a fold over typed evidence and no model writes it, but its YELLOW
  is dominated by `counted_severity` — which, with grading off, is the lens's own asserted
  severity. What is machine-checked underneath it is the evidence, the derivation and the
  caps; the grade itself is still a model's word unless `--auto-grade` ran. Demoting the
  triage out of the review's headline limits what this costs a reader, and does not fix
  it: the same assertion still decides which findings the triage folds over.
- Extraction still bounds what can be cited, and says so rather than guessing. Display
  equations are recovered in both real-world shapes (body-and-number on one line, and a
  right-margin number text extraction put on its own), but a paper whose equations
  shatter into per-glyph fragments yields zero — `pdf._equation_body` returns nothing
  rather than attaching a number to whatever text precedes it, because a wrongly
  assembled body would produce a false `equation_verified` attestation.
- Both reproduction verdicts are reachable and proven end to end through the real
  execution path (`tests/test_local_execution.py`, synthetic git fixture). Neither has
  been produced from a real paper: all three pilot papers refuse, APT on five independent
  blockers (see `docs/HARNESS_ARCHITECTURE.md` §6).
- Commit pinning is second-run-onward — the first acquisition of a paper is an unpinned
  depth-1 clone of the default branch.
- **The lenses read a truncated paper. They no longer do, and the replacement has its
  own costs.** `pdf.render_sections` divided `SECTION_BUDGET_CHARS` equally across sections
  and hard-sliced each, so a long paper was cut before any lens read a word of it:
  0.335 on `acl`, 0.407 on `sanchez24a-icml`, up to 0.841, measured. `harness/reading.py`
  replaces the cut with a PLAN — the fewest bounded parts that tile the paper — and
  `reader_visible_fraction` is **1.000 on all twelve documents in this repository**. The
  old number is still computable (`coverage.prose_presented`) because it is the baseline
  the claim is made against.

  What it costs, measured rather than estimated:

  | | before | after |
  |---|---|---|
  | prose a reader is carried | 0.335-0.841 | 1.000 |
  | model calls, paper that fits | 4 | 4 |
  | model calls, paper that does not | 4 | 12 (4 lenses x 2 parts + 4 syntheses) |
  | repeated-anchor overhead, corpus-wide | - | 3.17% of prose |

  Five of the twelve need two parts; none needs three. The ceiling that remains is
  EXTRACTION, which is unmeasured rather than measured-and-good: `extracted_text_fraction`
  reads 1.000 everywhere because it asks only whether prose was recovered from each page,
  and how much of each page was recovered has no denominator this artifact can compute.
- **Structural coverage is not issue recall and the gap is not small.** Over the seven
  shipped documents, 33% of the addressable surface got an address and 1.8% had a route
  pursued. Those are honest numbers about a first-pass screen and they are not a statement
  about how many real problems the reviews found, which remains unmeasurable here for want
  of adjudicated ground truth. `CoverageReport.semantic_coverage` says so in every artifact.
- **The `PAPER_INTERNAL_CHECK` route now settles nothing, which reduces what the system
  claims to have closed.** It is still taken when no executable route exists, because it
  costs nothing and establishes something real, but its outcome is `CITATION_VERIFIED_ONLY`
  and its question stays open. A rerun's `targets_settled` will be lower than the shipped
  reviews'.
- **`extraction_version` is a DECLARATION, not an enforcement.** Ingest stamps it and
  reports `stale_extraction` on a cached older parse, and nothing in `claims.resolve`
  refuses a reference minted under a different version. It is safe today only because a
  cached doc is never re-parsed, so a project's stored refs and its `doc.json` always come
  from one parse. **Deciding whether the eight-paper run re-parses the corpus is therefore
  a deliberate choice, not a detail:** re-parsing under version 2 invalidates the 4 stored
  `F<n>` and 17 `T<i>:r<r>:c<c>` refs in the existing lens files.
- **The seven verification agents for this revision's own work did not run.** The build
  agents landed and the adversarial reviewers hit a session limit. Their subjects were
  verified by hand instead — the false figure-caption attestation reproduced and confirmed
  closed, the per-lens model confirmed reaching the argv, the corpus effect of every
  vocabulary split re-measured, the conservation law confirmed catching a real
  disagreement — but "an independent adversarial pass found nothing" is not something this
  revision can claim.
- **The remote sandbox has never been leased.** `harness/sandbox.py` is driven end to end
  — lease, stage by SHA, interrogate, refuse a short machine, build the stack, verify the
  commit in place, run, release — against a scripted provider double, and every call it
  makes was checked against the installed `modal` 1.5.4 signatures. No live sandbox has
  been created, because this host holds no provider credentials. So the *logic* is tested
  and the *integration* is not: the first real run should be a single paper with
  `SH_ALLOW_REPO_EXEC=0`, which exercises leasing, staging, verification and release
  while executing nothing. `run.py sandbox` reports what is leased, and the seven corpus
  results in `outputs/` were all produced on the `local` backend, before this existed.

## Facts — about the DEVELOPMENT HOST, not requirements

These describe the machine the seven corpus reviews were produced on. On a different
machine they are simply false, and nothing in the harness reads them: hardware is measured
at run time (`resources.host_vram_bytes`, and `sandbox.inventory` for a leased machine).
They are recorded because several refusals in those outputs — a 24 GiB demand against
8 GiB present, a `linux-64` stack against `win32` — are only intelligible if you know what
was underneath.

Torch is cu126 (cu121 publishes no wheels for Python 3.13). GPU: RTX 4060 Laptop, 8 GB,
sm_89. ~15 GB RAM. Docker 29.7.2 is now installed here, which is why `harness/container.py`
exists and why `backends.select_for` chose `container` on every paper of the v2 run: the
isolation requirement for running a paper's own repository is MET on this host, and every
refusal in that run was therefore an identity or conformance refusal rather than an
infrastructural one. When this was written there was no WSL, no Docker and no
virtualization here — which was exactly
why the `modal` backend exists: a Linux-only repository is `environment_incompatible`
here and clears the same check unchanged when the platform the run will SEE is linux.
