# single-harness

An autonomous **first-pass scientific reviewer** for machine-learning papers.

Give it PDFs. It reads each one with four independent adversarial lenses, verifies every
quoted piece of evidence against the parsed paper, turns the surviving concerns into
answerable questions, and then decides for itself what is actually checkable — not from a
model's say-so, but from whether it can re-derive an address in the paper and a quantity
at that address. It prioritises those targets by how much the paper's conclusion rests on
them, and only then asks whether an experiment is *justified*. When one is, it acquires
the authors' repository, pins it to the commit it audited, and refuses to execute anything
until it can prove it is running the right code, measuring the right quantity, under the
right configuration, on hardware that fits.

Out comes a short **first-pass review** that opens with four rows, and they are four rows
because they answer four different questions:

```
## Review outcome
- what was established        CONCERNS_RECORDED         9 verified concern(s); none material
- what was resolved           NO_QUESTION_SETTLED       6 raised · 0 settled · 6 open
- what execution produced     ..._NO_ADMISSIBLE_EVIDENCE  + the exact reason
- what this is an assessment of  CENTRAL_CLAIMS_LEFT_UNCHECKED
```

The four are folded over **disjoint** inputs, so no blocked, refused or inadmissibly-
provenanced run can move what the review established. An execution that settled nothing
reads *"execution was attempted and did not produce admissible evidence"* with the reason
attached — never *"the paper is inconclusive"*, because the paper is not inconclusive; our
attempt to check it was. The complete machine trace sits beside the review in a file a
human never has to open, and the RED / YELLOW / GREEN triage is still deterministic and
still sits under the scope of the review, because it routes attention and is not the result.

**Four axes, and nothing may collapse them** (`harness/taxonomy.py`):

| axis | field | a property of |
|---|---|---|
| what kind of problem is this | `scientific_class` | the paper's argument |
| was the question settled, by what | `resolution_status` | the review process |
| what did the evidence route produce | `evidence_state` | the world |
| which review path was this on | `artifact_state` | the paper's artifact |

A confound settled against the paper and a confound left open because no artifact exists
are the same kind of problem in opposite states. Of the thirteen evidence states, four say
anything about the paper at all; the other nine describe an artifact, a host, or one of
this harness's own gates, and the review prints them in those words. Of the six artifact
states, exactly one — "the paper advertises no code" — is about the paper; the other five
are about a gate, a network or this host. They were one token, `SYNTHESIZED_DIAGNOSTIC`,
and four opposite facts read identically.

Reproduction is one evidence route inside that reviewer, not the definition of it. The
funnel has six terms and each is read off a different artifact — discovered, checkable,
judged to warrant an experiment, actually launched a process, ran to a reconciliation,
settled a question — because reporting the third in place of the fourth would present a
judgement as a measurement.

Its most useful property is still what it refuses to say. An experiment that does not fit
the machine, a repository whose code does not produce the cited number, a metric that
cannot be bound to what was printed, a paper that does not specify enough to rebuild the
experiment — each yields a named refusal, never a verdict against the paper.

## Architecture

```
papers
  │
  ▼
controller ──────── per-paper state in projects/<pid>/controller.json
  │                 bounded retries · resumable · papers isolated
  ├─ S1 ingest      PDF → PaperDoc (sections, tables, every number with provenance)
  ├─ S2 audit       four lenses: overclaim · protocol · confound · contradiction
  ├─   collect      every quote re-verified against the paper; failures dropped
  ├─   grade        a second, blinded reviewer per serious candidate (can only demote)
  ├─ S2b discover   claims.mint · questions.derive · taxonomy · discovery · priority
  │                      · planner · sync_questions
  │                      → discovery/targets.json — what is checkable, what it is worth,
  │                        whether an experiment is justified, and WHY. Pure; no model.
  ├─ S3 verify      per target: repo → commit → experiment/metric/configuration identity
  │                      → capability → resources → backend → authorization
  ├─   execute      only if authorization passed. Otherwise: blocked
  ├─   reconcile    reproduced metric vs the addressed quantity, arithmetically
  └─ S4 report      scientific findings × resolution states → reports/<pid>.review.md
                    threshold table → triage (printed under scope, counts nothing)
                                              + reports/<pid>.ledger.json
```

Only S2 and the optional grading step need judgement, and the judge is a language model —
either the Claude Code session driving the CLI, or, under `--auto-audit`, one subprocess
per lens. Everything else, including every decision about what to check and whether to run
it, is deterministic Python.

`docs/HARNESS_ARCHITECTURE.md` is the authoritative map, with the per-stage contract, the
address grammar and the full evidence chain.

## Usage

```bash
# the normal invocation
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py review \
    --paper papers/ICLR.pdf papers/CVPR.pdf papers/ACl.pdf --auto-audit
```

Exit codes: `0` every paper complete · `2` some paper waits on lens evidence · `1` error.

Without `--auto-audit` the run stops after writing `projects/<pid>/audit/prompts/*.md`
and exits 2; write `audit/<lens>.json` for each and re-run to resume.

```bash
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py evaluate   # system metrics over the corpus
```

### Running a paper's code somewhere other than this machine

Every gate is off by default and each grants one thing. To run a repository's own
entrypoint in a leased Linux sandbox instead of on the host, copy
`.env.sandbox.example` to `.env.sandbox`, add a provider token, and set:

```bash
SH_EXEC_BACKEND=modal      # route third-party code to the sandbox
SH_ALLOW_SANDBOX=1         # permit leasing a machine at all — this one costs money
SH_ALLOW_NETWORK=1         # fetch the audited commit
SH_ALLOW_INSTALL=1         # build the repository's declared stack there
SH_ALLOW_REPO_EXEC=1       # and permit running the authors' code
```

Leaving `SH_ALLOW_REPO_EXEC=0` is the useful first run: leasing, staging, in-place commit
verification and release all happen, and nothing executes. One sandbox is leased per
paper and released in a `finally`; `run.py sandbox [--release]` shows and terminates
anything that outlived its run. Code this harness authored — a generated probe, a
noise-floor calibration — always runs locally whatever `SH_EXEC_BACKEND` says, because
moving it would measure the sandbox instead of the host.

Per paper, four artifacts that should not be confused with one another:

| file | who reads it |
|---|---|
| `reports/<pid>.review.md` | **a human reviewer.** One to two pages. |
| `reports/<pid>.ledger.json` | anyone tracing a line of that report back to an artifact |
| `discovery/targets.json` | anyone asking what else was considered, and why it was not pursued |
| `reports/<pid>.md` / `.json` | the complete machine trace |

## Example — a first-pass review

The opening of a real one, `projects/2024-icml-sapg/reports/2024-icml-sapg.review.md`:

```
`2024-icml-sapg` · YELLOW — needs a human reviewer's attention

## Critical
Nothing was established as a material failure.

## Unresolved — needs a human
- Entropy-variant gain claimed; Reorientation-only contradicts the ShadowHand row (contradiction)
- Headline margins over PQL/PBT/DexPBT reflect per-task, post-hoc selection (confound)

## Experiments triggered
None. 4 review question(s) were raised; 2 were settled against the paper itself and
21 could not be pursued. No experiment was run that the review did not consider justified.

## Central claims this review did not check
1 central claim(s) were structurally checkable and nothing was run for them. This is the
boundary of the assessment above, not a criticism of the paper.
```

48 targets were discovered for that paper and 27 were structurally checkable. The review
above is 4.3 KB; the trace behind it is 111 KB.

## Example — a refusal

A real refusal, from the APT pilot paper. The audit targeted cell `T2:r3:c11`, and the
report shows exactly where the chain broke:

```
| audited commit        | 56eaf8bc8624 (verified)              |
| experiment identity   | no_candidate                         |
| metric identity       | unmapped                             |
| resource sufficiency  | insufficient                         |
| backend               | local (resources_insufficient)       |
| reconciliation        | INCONCLUSIVE                         |

⛔ The chain breaks at experiment identity.
```

The cited row reports `LLMPruner` — a third-party baseline the authors' code does not
produce — so no execution of their repository could settle it. Separately, the experiment
declares 24 GiB of VRAM against 8 GiB present, and no registered backend can host it.
Both facts are recorded; neither is a finding about the paper.

> **The seven reviews under `projects/` predate the current code.** Five of their corpus
> numbers are known to be wrong — see the table at the top of `CLAUDE.md`'s known
> limitations, and `docs/REVISION_2026-09.md` for how each was measured. Quote them for
> what the pipeline emitted in September 2026 and nothing else.

## Scientific guarantees

- Every finding carries a verbatim quote and a location (`p7` or `T2:r3:c4`), re-verified
  against the parsed paper. Unsubstantiated findings are dropped and the count printed.
- What the harness verified and what the model inferred are rendered separately. A lens
  cannot mark its own reasoning as confirmed.
- **Provenance ceiling**: only the authors' own checkout, or a human-written faithful
  reproduction, may reconcile against a printed cell — in either direction. A probe this
  harness synthesised can neither convict nor acquit.
- Execution is permitted by exactly one function, `backends.authorize`, and only when
  commit, experiment identity, metric identity, configuration identity, capability and
  resources all hold.
- Infrastructure failure is never scientific failure. A missing dependency, an
  incompatible platform, a shut gate and an experiment too large all yield
  `INCONCLUSIVE`.
- Nothing is shrunk to fit. There is no code path that reduces a model, batch, precision,
  sequence length, schedule or seed count to make an experiment run.
- The RED/YELLOW/GREEN call is a threshold table, not a judgement — and it is not the
  review's result. The result is the findings and their resolution states; the triage
  routes attention and is printed under the scope of the review.
- The three axes may not be collapsed. `scientific_class` is deliberately not a parameter
  of `grading.derive`: "a CONFOUND is always MAJOR" decides impact from something that is
  not impact, exactly as "no variance = MAJOR" did.
- The provenance ceiling is enforced twice — at the reconciler, and again where the
  evidence state is derived. A failed reproduction on inadmissible provenance renders as
  an inconclusive execution, so a synthesized diagnostic cannot convict through the report
  after being refused by the reconciler.
- What was judged worth running and what actually ran are two numbers. `launched` is read
  off the runner's own process count and is never inferred from a disposition, and
  `probe_stage_seconds` is named for what it measures rather than called execution cost.

## Current execution limitations

Real hardware: Windows, RTX 4060 Laptop (8 GiB VRAM), Ryzen 9, ~15 GiB RAM. No WSL, no
Docker, no virtualization.

`LocalBackend` really executes: it starts processes, verifies the audited commit and a
clean tree immediately beforehand, and records every attempt whole in
`runs/<pid>/execution.jsonl`. Both reproduction verdicts are proven end to end through
that path against a synthetic git fixture (`tests/test_local_execution.py`) — a fixture
that is plumbing evidence and never a paper reproduction.

**No pilot paper can execute here.** SAPG and CFG advertise no repository at all. APT has
five independent blockers: its cited row reports a third-party baseline its own code does
not produce, the metric cannot be bound to the cell, the stack is `linux-64` against a
`win32` host, the experiment declares 24 GiB against 8 GiB present, and no registered
backend can host it. The first two are properties of the citation; the rest are limits of
this machine.

Provisioning is `python -m venv` plus pip requirement files, verified on this host (~9 s,
isolated, no conda, no host mutation). A repository whose stack is a conda
`environment.yml` cannot be provisioned — itself an abstention, not a paper failure.

Kaggle and Colab are registered as **declarations** — real published specs,
`can_execute=False`, `execute()` raises — so selection can report that a T4 would fit
while stating it cannot be provisioned from here. They are not integrations.

## Repository

```
run.py                 CLI. Formats; decides nothing.
harness/
  controller.py        the driver: phase machine, retries, batch scheduling, entry points
  artifacts.py         every typed artifact that crosses a stage boundary
  config.py            paths and gates, all from the environment
  stages/
    ingest.py          S1
    audit.py           S2 prompts + evidence verification
    probe.py           S3 planning
    report.py          S4 ranking, thresholds, rendering
  audit_driver.py      optional lens delegation, one subprocess per lens
  backends.py          execution backends, requirement matching, authorization
  sandbox.py           the remote-sandbox driver — lease, stage a commit, run, release
  experiment_id.py     experiment / metric / configuration identity
  resources.py         what the cited experiment costs vs what a backend offers
  repo.py              acquisition, commit pinning, capability
  code_audit.py        static AST audit of the checkout — never imports, never runs
  probe_synth.py       synthesises a probe from the paper's own formulation
  local_exec.py        runs probes, parses metrics, reconciles
  pdf.py               PDF → sections, tables, numbers
  dossier.py           cross-paper consolidation
  prompts/audit.py     the four lens prompts, and the per-lens model each one runs on
  outcome.py           the four reader-facing rows, folded over disjoint inputs
  coverage.py          structural review-surface coverage, denominator read off the PAPER
  docintegrity.py      deterministic document integrity — observations, never findings
  guarantees.py        what a review promises, what it does not, and which held
  provenance.py        the provenance ceiling, in one object
  preflight.py         is this batch N distinct papers? Answered before anything is spent
tests/                 1982 tests
docs/                  the architecture map
papers/                source PDFs
projects/<pid>/        per-paper state: paper, audit, runs, reports, controller.json
```

## The manuscript

| file | what it is |
|---|---|
| `manuscript/journal.tex` / `.pdf` | **The journal treatment.** 35 pages, 20 sections, 5 appendices, 12 tables. Every table is generated from the frozen run by `manuscript/tables/make_journal_tables.py`; no measurement in it is typed by hand. |
| `manuscript/manuscript.tex` / `.pdf` | The six-page condensed version of the same result. |
| `manuscript/fig2_decision.tex` / `.pdf` | Figure 2, the decision structure, as standalone vector art. |
| `manuscript/check_journal_claims.py` | Fails the build when a number in the prose disagrees with the run, when a comparative claim this evaluation cannot support appears, or when an em dash does. |
| `manuscript/check_journal_style.py` | Fails the build on em dashes, on `provenance` used more than twice, on twelve rhetorical patterns that read as machine-written, on a missing section, and on a measurement written as a hand-made table. |
| `manuscript/running_is_not_verifying.tex` | **Stale.** A longer draft against an earlier run, kept for comparison. Its header says so. |

Both current sources build with zero overfull boxes, zero undefined references and zero
undefined citations, and every bibliography entry is cited exactly once in the text.

The claims in the manuscript are mapped to code, tests and artifacts in
`docs/CODEBASE_CLAIM_MAP.md`; reviewer feedback is closed in
`docs/REVIEWER_FEEDBACK_CLOSURE.md`. The revision that produced the evaluation is tagged
`evaluated-v2`; the revision to check out is tagged `release-2026-09-16`.
