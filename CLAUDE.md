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
| what did the evidence route produce | `evidence_state` — 13 values | the world |
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
papers → controller → ingest → audit → collect → grade → discover → probe → report
                                                     └ questions · targets · priority · plan
```

| phase | module | input → output | decides | refuses by |
|---|---|---|---|---|
| ingest | `stages/ingest.py` | PDF → `paper/doc.json` | nothing (deterministic) | `error` on an unreadable PDF |
| audit | `stages/audit.py` + `audit_driver.py` | doc → `audit/<lens>.json` ×4 | the four lenses judge | `waiting`, resumable |
| collect | `stages/audit.load_reports` | lens files → verified findings | quote ≟ paper | drops the finding, counts it |
| grade | `stages/grade.py` + `grade_driver.py` | serious findings → `audit/grade/<slug>.json` | a second, blinded reviewer per candidate | `ok` with partial coverage — never blocks a report by default |
| **discover** | `stages/discover.py` | doc + findings → `discovery/targets.json` | what is addressable, what it is worth, whether an experiment is justified | records a NAMED refusal per target |
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
| where does the paper say this? | `claims.py` | the parsed doc — a lens supplies a quote, the harness mints the address |
| what would settle this concern? | `questions.py` | a finding's own closed-vocabulary self-classification |
| what KIND of problem is this? | `taxonomy.py` | the same closed vocabulary; never a number, a name or a paper |
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
python run.py review --paper a.pdf b.pdf c.pdf --auto-audit --auto-grade  # the entrypoint
python run.py review --paper a.pdf                            # exit 2 → lenses pending
python run.py status <paper-id>                               # controller state + history
python run.py list                                            # reviewed papers
python run.py dossier                                         # consolidate finished reports
python run.py evaluate                                        # system metrics over the corpus
python run.py sandbox [--release]                             # leased remote machines
python run.py preflight                                       # is this batch N distinct papers?
python -m pytest tests -q                                     # 1571 tests
```

Always `PYTHONUTF8=1` on Windows (paper text is full of em dashes and math) and always
the repo venv: `../.venv/Scripts/python.exe`.

Per paper, `projects/<pid>/` holds four things a reader should not confuse:

| file | who reads it |
|---|---|
| `reports/<pid>.review.md` | **a human reviewer.** One to two pages. |
| `reports/<pid>.ledger.json` | anyone tracing a line of that report to an artifact |
| `discovery/targets.json` | anyone asking what else was considered, and why it was not pursued |
| `reports/<pid>.md` / `.json` | the complete machine trace |

Self-checks, one per module: `python -m harness.<claims|questions|taxonomy|discovery|
priority|planner|ledger|evaluation|local_exec|repo|code_audit|probe_synth|dossier|
audit_driver|grade_driver|verdict_driver|grading|failures|selfaudit|corpus|backends|
resources|sandbox|controller|provenance|outcome|coverage|docintegrity|guarantees|
preflight|reimplement>` and `python -m harness.stages.<report|grade|discover>`.
`tests/test_self_checks.py` DISCOVERS them rather than listing them, so a module that
loses its self-check fails the suite; `tools/loop.py` runs the whole loop in one command.
`harness.sandbox`'s self-check leases nothing and needs no credentials.
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
3. **Provenance ceiling** — only `driver` or `repo_exec` provenance may reconcile a
   printed cell, in *either* direction. A synthesized probe can neither convict nor
   acquit.
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
   admissible provenance. A model-assigned FATAL is an attention signal with no rejection
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
    scan (`json_metric`) is diagnostic-only and is not on the execution path.
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
29. **A batch's paper count is checked before it is spent.** `harness/preflight.py`
    answers, per requested file, which `paper_id` it will get, whether that id already
    holds a DIFFERENT document, and whether another requested file is the SAME document —
    all from the PDF's bytes. Two different papers that slugify identically are fine and
    are reported with their distinct ids; the same paper submitted twice REFUSES the batch,
    because `allocate_paper_id` correctly resumes its project, which is right per paper and
    invisible in aggregate. An eight-file request that is seven documents produces seven
    reviews and a claim about eight papers.

## Operating autonomously

`--auto-audit` delegates each lens to a reviewer — `SH_AUDIT_CMD`, or the `claude` CLI
discovered on PATH — as **one subprocess per lens**, which is stronger isolation than
four lenses read in one session. It is opt-in because it spends tokens.

Without it, `review` writes `audit/prompts/<lens>.md`, exits 2, and resumes when the
lens files exist. If you fill them yourself, **run each lens in a separate turn**: four
independent readings are four pieces of evidence; one context that remembers the
previous three is one reading echoed four times.

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

## Execution gates

| gate | env var | default | permits |
|---|---|---|---|
| network | `SH_ALLOW_NETWORK` | on | `git clone --depth 1` of the URL the paper advertises |
| synthesis | `SH_ALLOW_SYNTHESIS` | on | the planner authors `runs/<pid>/probe.py` from the paper |
| auto-audit | `SH_ALLOW_AUTO_AUDIT` | off | shelling out to a reviewer for the lenses |
| grading | `SH_ALLOW_GRADING` | off | a second, blinded reviewer per FATAL/MAJOR finding — zero tools, no filesystem access at all (`grade_driver.py`) |
| substantive verdict | `SH_ALLOW_SUBSTANTIVE_VERDICT` | off | one best-effort, never-retried, whole-paper opinion — printed, consumed by no threshold (`verdict_driver.py`) |
| install | `SH_ALLOW_INSTALL` | off | building `runs/<pid>/env` from the repo's requirements |
| sandbox | `SH_ALLOW_SANDBOX` | off | **leasing a remote Linux machine** and staging the audited commit into it (`harness/sandbox.py`) |
| execute | `SH_ALLOW_REPO_EXEC` | off | running the repository's own entrypoint |

`SH_MAX_TARGETS` (default 3) is a BUDGET, not a gate: which targets are worth pursuing is
`planner`'s decision and the order is `priority`'s, so lowering it drops the least useful
targets rather than an arbitrary subset. Raising it costs compute and changes no rule.

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
- **Novelty / prior-art checking is architectural only.** `VERIFICATION_ROUTES` carries
  `LITERATURE_SEARCH` and nothing implements it: no literature tooling is wired in, so no
  novelty conclusion is produced. The route exists so that adding one later has a place to
  attach with its own provenance — not as a capability the system has.
- **No adjudicated ground truth exists for this corpus.** `harness/evaluation.py`
  therefore reports no precision, recall, or agreement-with-humans number, and says so in
  the artifact itself. Reviewer accuracy is unmeasured, not measured-and-good.
- **PATH B is eligibility-only so far.** `harness/reimplement.py` decides whether a paper
  with no published code says enough to rebuild, and writes the brief when it does. No
  independent reimplementation has actually been written and sealed through
  `run.py accept`, so the INDEPENDENT_REIMPLEMENTATION provenance is exercised by tests
  and by the `driver` path, not yet end to end from a real no-code paper.
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
- **The lenses read a truncated paper, and now say so.** `pdf.render_sections` divides
  `SECTION_BUDGET_CHARS` across sections, so a long paper is cut before any lens reads a
  word of it. Measured presented fraction over the shipped corpus: 0.384, 0.411, 0.489,
  0.589, up to 0.745. The review's scope line said "4 independent lens(es) read the paper"
  and now adds the fraction. This is the ceiling on any recall claim this system makes and
  it was unmeasured until `pdf.section_presentation` existed. Nothing raises the budget:
  the number is reported, not fixed.
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
sm_89. ~15 GB RAM. No WSL, no Docker, no virtualization on this host — which is exactly
why the `modal` backend exists: a Linux-only repository is `environment_incompatible`
here and clears the same check unchanged when the platform the run will SEE is linux.
