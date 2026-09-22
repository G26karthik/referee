# single-harness — REFEREE

An autonomous **first-pass scientific reviewer** for machine-learning papers.

Give it PDFs. Four independent model readers propose concerns; deterministic code decides
what may be concluded from each one. Every surviving concern becomes a **review question**
with a stated settlement condition, the system enumerates which evidence routes could
answer it, pursues them from cheapest to most demanding, and permits a route to speak
about the paper only when the identity of what ran binds to the quantity that was printed.
A human referee sits outside that boundary, receives the finished review and the machine
ledger, and keeps the venue decision — REFEREE produces no accept/reject recommendation.

Reproduction is one evidence route inside the reviewer, not its definition. The system
also checks the paper's own arithmetic, statically inspects a released repository for
narrow configuration facts, and — when no code was published but the paper specifies
enough — drafts and independently verifies a governed reconstruction. (Two further routes,
a bounded prior-art search behind novelty claims and a focused one-variable-contrast
designer for attribution claims, existed in an earlier revision and were deleted
2026-09-20 for measuring zero endpoint-verified value over the eight-paper corpus; see
`CLAUDE.md`'s "Known limitations".) Nothing here is a score or a colour: the
output is a complete, typed account of what was asked, what was tried, and exactly where
each attempt stopped.

## What actually reads the paper

Four lenses run on every review, each in its own context, each blind to the other three
and to anything the harness has already decided:

| lens | looks for |
|---|---|
| `overclaim` | a headline claim the reported evidence does not support |
| `protocol` | a missing control, an unstated assumption, a protocol gap |
| `confound` | a comparison that changes more than the one thing it credits |
| `contradiction` | two places in the paper (or paper vs. artifact) that disagree |

Beyond the four lenses, further specialist roles are gated individually and run
only where their own gate is open, each confined to the one bounded question it exists to
answer: a **blinded grader** re-examines serious candidates with zero tools, deciding only
confirmed-finding vs. plausible-concern; a **whole-paper reader** gives a qualitative
opinion with no decision authority, printed beside the machine result but never fed back
into it; an **authors'-code reader** (`Read`+`Grep` only) has every citation it returns
relocated to a real line before it is trusted; and a **reconstruction generator** paired
with a **separately-attributed conformance verifier** drafts and checks an independent
reimplementation when no code was published, so generated code can never certify its own
conformance. (A claim-link reader, a literature query proposer/reviewer, and a
focused-validation designer existed in an earlier revision and were deleted 2026-09-20 —
each measured zero structurally-useful output over the eight-paper corpus; see
`CLAUDE.md`'s "Known limitations".)

**What every one of these roles returns is a proposal and nothing more.** None of them
decides anything: quotation verification, evidence-authority ceilings, admissibility,
materiality and route exhaustion are deterministic, paper-agnostic, and read only
closed-vocabulary strings and booleans — never a count, a metric name, or a paper identity.
`docs/HARNESS_ARCHITECTURE.md` is the authoritative per-stage contract; `manuscript/journal.tex`
is the full research-paper treatment of the design and the eight-paper evaluation.

## What comes out

A review opens with four rows, and they are four rows because they answer four different
questions, folded over disjoint inputs so that no execution outcome can move what was
established:

```
## Review outcome
- what was established        CONCERNS_RECORDED         9 verified concern(s); none material
- what was resolved           NO_QUESTION_SETTLED       6 raised · 0 settled · 6 open
- what execution produced     ..._NO_ADMISSIBLE_EVIDENCE  + the exact reason
- what this is an assessment of  CENTRAL_CLAIMS_LEFT_UNCHECKED
```

The reader-facing result is a **typed disposition** — `PASS_TO_HUMAN_CLEAN`,
`BLOCKED_METHOD`, `BLOCKED_SPECIFICATION`, and so on (`harness/decide.py`,
`reports/corpus.json`) — the action a human should take on this paper, printed under the
review's *scope* section, never as a headline verdict. The global RED/YELLOW/GREEN triage
and the binary RED/GREEN paper verdict were removed outright in the 2026-09-21 de-triage
pass: REFEREE is a reviewer copilot, not an acceptance-decision system, and no colour or
disguised accept/reject score replaced them. `STOP_MATERIAL_FAILURE` means a material
failure was established from an admissible source on a target a central claim depends on;
`PASS_TO_HUMAN_UNRESOLVED`/`_CONCERNS` mean something needs a human's attention and is
never itself an accusation; `PASS_TO_HUMAN_CLEAN` means neither, within the scope actually
checked, and is explicitly not a certificate of correctness.

An experiment that does not fit the machine, a repository whose code does not produce the
cited number, a metric that cannot be bound to what was printed, a paper that does not
specify enough to rebuild the experiment — each yields a named, typed refusal, never a
verdict against the paper. A refused experiment is a result: the system exhausted its
applicable evidence routes for a question and says exactly where each one stopped.

## Architecture

```
papers → ingest → audit → collect → grade → assess → discover → probe → report
                                                 └ questions · targets · priority · plan
  │
  ├─ ingest    PDF → PaperDoc (sections, tables, every number with an address)
  ├─ audit     four lenses, each blind to the other three and to prior findings
  ├─ collect   every quote re-verified against the parsed paper; failures dropped, counted
  ├─ grade     a second, blinded reviewer per serious candidate (can only demote a grade)
  ├─ assess    has a material failure already been established; is the investigation open
  ├─ discover  questions, targets, priority, experiment necessity — pure, no model
  ├─ probe     repo → commit → experiment/metric/configuration identity → capability →
  │              resources → backend → one conjunctive authorization
  ├─ execute   only if authorization passed; otherwise a named, typed refusal
  ├─ reconcile measured quantity vs. the addressed printed quantity — arithmetic only
  └─ report    reader-facing review + machine report + ledger, bounded by construction
```

The nine gated specialist roles sit beside `audit`, `discover` and `probe` and are each
dispatched only when their own environment gate is open; every one of them writes a
proposal that the harness independently re-verifies before anything downstream may read it.

## Usage

```bash
# the normal invocation
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py review \
    --paper papers/ICLR.pdf papers/CVPR.pdf papers/ACL.pdf --auto-audit --auto-grade
```

`--auto-audit` and `--auto-grade` select a delegation mode; they do **not** open the gate
that permits it. Add `SH_ALLOW_AUTO_AUDIT=1` and `SH_ALLOW_GRADING=1` (or the run stops at
`waiting` and says so). Exit codes: `0` every paper complete · `2` some paper waits on lens
evidence · `3` a whole-paper opinion disagreed sharply enough with the deterministic
decision to be flagged contested · `1` error.

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py status <paper-id>   # controller state + history
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py list                # reviewed papers
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py dossier             # consolidate finished reports
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py evaluate            # system metrics over the corpus
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py preflight           # is this batch N distinct papers?
```

Every execution-adjacent gate (`SH_ALLOW_NETWORK`, `SH_ALLOW_INSTALL`, `SH_ALLOW_SANDBOX`,
`SH_ALLOW_REPO_EXEC`, `SH_ALLOW_REIMPLEMENTATION_DRIVER`, `SH_ALLOW_REIMPLEMENTATION_EXEC`,
`SH_ALLOW_ARTIFACT_REVIEW`, `SH_ALLOW_LITERATURE_SEARCH`, `SH_ALLOW_LITERATURE_REVIEW`,
`SH_ALLOW_VALIDATION_DESIGN`, `SH_DIAGNOSTIC_MODE`) is off by default and grants exactly one
thing; `CLAUDE.md`'s "Execution gates" table lists what each one permits. Repository
execution additionally requires a container or a remote sandbox session — there is no
environment variable that lowers that requirement.

### Running a paper's code somewhere other than this machine

To run a repository's own entrypoint in a leased Linux sandbox instead of on the host, copy
`.env.sandbox.example` to `.env.sandbox`, add a provider token, and set:

```bash
SH_EXEC_BACKEND=modal      # route third-party code to the sandbox
SH_ALLOW_SANDBOX=1         # permit leasing a machine at all — this one costs money
SH_ALLOW_NETWORK=1         # fetch the audited commit
SH_ALLOW_INSTALL=1         # build the repository's declared stack there
SH_ALLOW_REPO_EXEC=1       # and permit running the authors' code
```

Leaving `SH_ALLOW_REPO_EXEC=0` is the useful first run: leasing, staging, in-place commit
verification and release all happen, and nothing executes. One sandbox is leased per paper
and released in a `finally`; `run.py sandbox [--release]` shows and terminates anything
that outlived its run. Code this harness authored — a generated probe, a noise-floor
calibration — always runs locally whatever `SH_EXEC_BACKEND` says, because moving it would
measure the sandbox instead of the host.

Per paper, four artifacts that should not be confused with one another:

| file | who reads it |
|---|---|
| `reports/<pid>.review.md` | **a human reviewer.** One to two pages. |
| `reports/<pid>.ledger.json` | anyone tracing a line of that report back to an artifact |
| `discovery/targets.json` | anyone asking what else was considered, and why it was not pursued |
| `reports/<pid>.md` / `.json` | the complete machine trace |

## Requirements

Python 3.11+ in the repo's own virtual environment (`../.venv` relative to this
directory on the machine this was built on — substitute your own interpreter path).
`pip install -r requirements.txt`. Always run with `PYTHONUTF8=1` on Windows. A container
runtime (Docker) or a configured remote sandbox (Modal) is required only to execute a
repository's own code or a governed reconstruction; every other stage runs with neither.

## Example — a refusal

A real refusal, from one of the evaluated papers. The audit targeted a printed cell, and
the report shows exactly where the chain broke:

```
| audited commit        | 56eaf8bc8624 (verified)              |
| experiment identity   | no_candidate                         |
| metric identity       | unmapped                              |
| resource sufficiency  | insufficient                          |
| backend               | local (resources_insufficient)        |
| reconciliation        | INCONCLUSIVE                          |

The chain breaks at experiment identity.
```

The cited row reports a third-party baseline the authors' own code does not produce, so no
execution of their repository could settle it. Both facts are recorded; neither is a
finding about the paper.

## Where the evaluation artifacts live

`runs_final_2026-09-16/` is the frozen, authoritative eight-paper systems evaluation this
project's manuscript and delivery report are checked against: per-paper projects, reports,
and a run manifest binding every source PDF, rendered paper and model prompt/response to a
SHA-256 digest. **It is not part of this source release** — it is a large, multi-hundred-file
run directory, not source, and is distributed separately. `manuscript/check_journal_claims.py`
needs it on disk at `../runs_final_2026-09-16` (relative to `manuscript/`) to run; without
it, run the manuscript build and its own test suite only (`pytest tests -q -m "not network"`),
which need no run directory at all. Do not treat a `FileNotFoundError` from that one checker
script, run outside its authoritative run directory, as a defect in the released source.

`projects/` in this release holds the harness's own smaller worked examples and fixtures,
not the evaluation corpus.

## Scientific guarantees

- Every finding carries a verbatim quote and a location, re-verified against the parsed
  paper. Unsubstantiated findings are dropped and the count printed.
- What the harness verified and what a model inferred are rendered separately. A reader
  cannot mark its own reasoning as confirmed.
- **Authority ceiling**: only the authors' own checkout, a sealed human-written
  reproduction, or a governed reconstruction an independent verifier bound ingredient by
  ingredient, may reconcile against a printed cell — in either direction. A probe this
  harness synthesised can neither convict nor acquit, and the ceiling is enforced twice:
  at the reconciler, and again where the evidence state is derived.
- Execution is permitted by exactly one function, requiring all at once: an open gate, an
  admissible source, a verified commit, established experiment/metric/configuration
  identity, sufficient capability and resources, and container- or sandbox-grade isolation.
- Infrastructure failure is never scientific failure. A missing dependency, an incompatible
  platform, a shut gate, or an experiment too large all yield `INCONCLUSIVE`, never a
  failed reproduction.
- Nothing is shrunk to fit. There is no code path that reduces a model, batch, precision,
  sequence length, schedule or seed count to make an experiment run.
- The typed disposition is a materiality table, not a model judgement and not a count: no
  number of MAJOR or MINOR findings ever reaches a paper-stopping state, because a concern
  weakens a claim and does not by itself reject one.
- The four axes — what kind of problem, was it settled, what did the evidence produce,
  which review path was this paper on — may never be collapsed into one, and none of them
  is a colour.
- Route exhaustion and settlement are two different numbers: exhausting the applicable
  methods for a question is not the same as answering it, and the reader-facing accounting
  keeps them apart rather than letting one stand in for the other.

## Repository

> **v4 note (2026-09-21):** 89 reference-implementation modules (43,756 production lines)
> were consolidated into the 12 new modules below plus a smaller set of kept
> single-purpose "satellite" files, landing at 32,174 production lines. The tree below is
> current; see `CLAUDE.md`'s "Known limitations" for the redesign's own record.

```
run.py                 CLI. Formats; decides nothing.
harness/
  schema.py             every typed artifact that crosses a stage boundary
  locate.py              the address grammar: mint/resolve a quote-grounded reference
  paper.py               PDF → PaperDoc; the bounded-part reading plan
  agent.py                the one model-delegation runner: lens, grade and verdict roles
  prompts/                prompt text per role
  audit.py                lens dispatch, evidence verification, dedup, severity derivation,
                           blinded grading
  discover.py             questions, targets, priority, experiment necessity
  decide.py               taxonomy, materiality, provenance ceiling, disposition, route
                           exhaustion, plan gates — what may be concluded
  routes.py               per-target orchestration: acquisition, static audit, planning
  execute.py              backends, the authorization ladder, the runner, reconciliation
  report.py               ranking, thresholds, the four reader-facing rows, coverage,
                           document integrity, guarantees, self-audit, the ledger
  pipeline.py             the driver: phase machine, retries, batch scheduling, preflight,
                           corpus accounting, entry points
  summarize.py            dossier consolidation + corpus-wide evaluation metrics
  taxonomy.py             the four closed-vocabulary axes and their derivations (satellite)
  config.py               paths and gates, all from the environment
  stages/
    ingest.py             PDF → PaperDoc (kept satellite)
    artifact.py            static artifact inspection + the authors'-code auditor route
  artifact_evidence.py, artifact_review_driver.py, reimplement_driver.py,
  reviewer_cli.py, delegation.py, sealing.py, failures.py
                          kept single-purpose satellites: relocate/discharge logic,
                          the authors'-code auditor, the reconstruction generator +
                          conformance verifier, CLI confinement mechanics, delegation-mode
                          vocabulary, write-then-verify sealing, retry classification
  experiment_id.py, resources.py, repo.py, code_audit.py, probe_synth.py,
  container.py, provenance.py, isolation.py, assessment.py, alignment/
                          kept single-purpose satellites: identity resolution, resource
                          assessment, git acquisition, the one surviving AST cheat-pattern
                          rule, probe authoring, the container backend, the provenance
                          ceiling and isolation-floor constants, material-failure
                          prioritization, experiment-identity narrowing
tests/                 99 collected, 60 files fail to import as of 2026-09-21 (stale
                        references to modules the v4 consolidation removed — a known gap,
                        not yet repointed; see `CLAUDE.md`'s "Known limitations")
docs/                  the architecture map and measurement records
  archive/             historical build logs and superseded drafts — see its own README
papers/                source PDFs (this release ships only its own small fixtures)
projects/<pid>/        per-paper state: paper, audit, runs, reports, controller.json
manuscript/            the research-paper treatment, its generators and its checkers
```

## The manuscript

| file | what it is |
|---|---|
| `manuscript/journal.tex` / `.pdf` | **The full treatment.** Every table is generated from a frozen run directory by `manuscript/tables/make_journal_tables.py` and `make_corpus_table.py`; no measurement in it is typed by hand. |
| `manuscript/manuscript.tex` / `.pdf` | An earlier, shorter condensed version of the same material. |
| `manuscript/check_journal_claims.py` | Fails the build when a number in the prose disagrees with the run, when a comparative claim this evaluation cannot support appears, or when an em dash does. Needs `runs_final_2026-09-16` — see above. |
| `manuscript/check_journal_style.py` | Fails the build on em dashes, on `provenance` used more than twice, on rhetorical patterns that read as machine-written, on a missing required section, or on a measurement written as a hand-made table. |
| `manuscript/running_is_not_verifying.tex` | **Stale**, kept only for comparison against an earlier run; its own header says so. |

Both current sources build with zero overfull-box errors that clip content, zero undefined
references and zero undefined citations, and every bibliography entry is cited at least
once in the text.
