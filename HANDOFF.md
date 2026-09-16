# single-harness — handoff

> **Read `docs/REVISION_2026-09.md` before you quote any number under `projects/`.** The
> seven reviews stored there were produced by older code and five of their corpus numbers
> are now known to be wrong — including the funnel's terminal term, which read
> "20 targets settled a question about the paper" where the honest value is 0. That
> document lists each one, how it was measured, and what a rerun produces instead. The
> current code is correct; the stored outputs are a record of September 2026.

You have the code and one run's outputs. This document is what you need before you change
anything: what the system is for, what its parts are, how to run it, where the outputs
live, and — most importantly — the small number of rules that everything else is built to
protect. Read §1 and §6 even if you read nothing else.

`CLAUDE.md` is the working contract in condensed form. `docs/HARNESS_ARCHITECTURE.md` is
the per-stage specification, the address grammar and the full evidence chain.
`docs/IMPLEMENTATION_REPORT.md` says what changed in each revision and what each
revision's own self-review found wrong with it.

---

## 1. What this is

An autonomous **first-pass scientific reviewer** for papers. In: PDFs. Out: a two-page
reviewer-facing report per paper — a set of scientific findings, each with a category, a
resolution state, and a machine-verified evidence pointer — backed by a complete machine
ledger a human never has to read in order to trust the conclusion.

It is **not** a reproduction harness that happens to write prose. Reproduction is one
evidence route inside the reviewer, and most questions never reach it. The system reads
the paper with four independent critical lenses, turns their concerns into *questions*,
decides for itself which questions are structurally checkable, ranks them, decides whether
an experiment is **justified**, runs the authors' code when one is, reconciles what it
measured against what the paper printed, and hands a human two pages instead of sixty.

### The thing to internalise first

The system's whole value is that it **knows the difference between what it observed, what
it checked, and what it may conclude** — and can show all three. Almost every design
decision in the codebase exists to stop those three collapsing into one. If you find
yourself writing code that makes a number available in a place that has not earned it, you
are working against the grain and something will refuse you. That refusal is the product.

Concretely, three axes are kept apart and no single label may replace them
(`harness/taxonomy.py`):

| axis | field | is a property of |
|---|---|---|
| what KIND of problem is this | `scientific_class` — 10 values | the paper's argument |
| was the question settled, and by what | `resolution_status` — 5 values | the review process |
| what did the evidence route produce | `evidence_state` — 9 values | the world |

A confound settled from the paper and a confound left open because no artifact exists are
the *same kind of problem in opposite states*. One label for both tells a referee neither.

RED / YELLOW / GREEN still exist, but they are a **routing** decision printed under
`## Scope of this review`, not the result. The findings and their resolution states are
the result. RED means a material failure was ESTABLISHED; YELLOW means a human should
look and is never an accusation; GREEN means neither, *within the scope actually checked*,
and is not a certificate of correctness.

**A clean paper is a real result.** Zero FATAL/MAJOR findings, a few MINOR concerns and
some open questions is a complete, correct review of good work — not a reviewer that
failed to try.

---

## 2. Getting it running

Python 3.13. Always `PYTHONUTF8=1` — paper text is full of em dashes and mathematics.

Nothing here is platform-specific: the harness measures the hardware it is on rather than
assuming any, and the one place a platform is compared against a requirement
(`assess_capability`) asks the *backend* what platform the run will see. The commands
below use Windows venv paths because that is the environment it was built in; substitute
`.venv/bin/python` elsewhere. `CLAUDE.md`'s `## Facts` section describes that development
host and is not a requirement.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r single-harness/requirements.txt
```

That single install is enough to run everything below, tests included — verified from a
clean extract of the handoff zip into an empty directory with a fresh interpreter.

Then, from `single-harness/`:

```bash
# The whole suite — start here. In the development tree: 1571 passed. In an older handoff package: 1027 passed, 2 skipped.
# (1029 pass in the development tree; the two that skip here read the cloned
# third-party checkout, 26 MB, which is deliberately not shipped. They skip rather
# than fail, which is what you want from a test whose fixture is someone else's repo.)
# Verified from a clean extract of the zip and a fresh venv built from requirements.txt.
PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests -q

# every module also self-checks in isolation; each is a fast readable spec
PYTHONUTF8=1 ../.venv/Scripts/python.exe -m harness.taxonomy
PYTHONUTF8=1 ../.venv/Scripts/python.exe -m harness.sandbox      # leases nothing
```

The self-checks are worth your time before the tests. Each one is a compressed statement
of what its module guarantees, and running `python -m harness.<name>` for the module you
are about to touch is the cheapest way to learn its contract. Modules with one:
`claims questions taxonomy discovery priority planner ledger evaluation local_exec repo
code_audit probe_synth dossier audit_driver grade_driver verdict_driver grading failures
selfaudit corpus backends resources sandbox controller`, plus
`harness.stages.<report|grade|discover>`.

### Reviewing a paper

```bash
# the entrypoint
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py review \
    --paper papers/ICLR.pdf papers/CVPR.pdf --auto-audit --auto-grade

PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py status <paper-id>
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py list
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py dossier      # consolidate finished reports
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py evaluate     # system metrics over the corpus
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py sandbox      # leased remote machines
```

Exit codes: `0` complete · `2` waiting on lens evidence · `1` error · `3` complete but
CONTESTED (the independent whole-paper read disagrees sharply with the threshold table).

**Exit 2 is normal, not a failure.** Without `--auto-audit` the run writes
`projects/<pid>/audit/prompts/<lens>.md`, stops, and resumes when the lens files exist.
If you fill them in by hand, **run each lens in a separate session**: four independent
readings are four pieces of evidence, and one context that remembers the previous three is
one reading echoed four times.

---

## 3. The pipeline

```
papers → controller → ingest → audit → collect → grade → discover → probe → report
                                                    └ questions · targets · priority · plan
```

| phase | module | input → output | decides | refuses by |
|---|---|---|---|---|
| ingest | `stages/ingest.py` | PDF → `paper/doc.json` | nothing (deterministic) | `error` on an unreadable PDF |
| audit | `stages/audit.py` + `audit_driver.py` | doc → `audit/<lens>.json` ×4 | the four lenses judge | `waiting`, resumable |
| collect | `stages/audit.load_reports` | lens files → verified findings | quote ≟ paper | drops the finding, counts it |
| grade | `stages/grade.py` + `grade_driver.py` | serious findings → `audit/grade/<slug>.json` | a second, blinded reviewer per candidate | partial coverage; never blocks a report by default |
| **discover** | `stages/discover.py` | doc + findings → `discovery/targets.json` | what is addressable, what it is worth, whether an experiment is justified | a NAMED refusal per target |
| verify | `stages/probe.py` | doc + repo → `ProbeSpec` per target | identity, capability, resources, commit, backend | leaves the spec unpromoted |
| execute | `backends.py` + `local_exec.py` + `sandbox.py` | spec → `ProbeResult` | `authorize()` alone | `verdict: blocked` |
| reconcile | `local_exec.reconcile` | metric vs the addressed quantity | arithmetic only | `INCONCLUSIVE` |
| report | `stages/report.py` | everything → `reports/<pid>.review.md` | threshold table over `counted_severity` | — |

`harness/controller.py` drives; the deterministic code below it decides what may be
concluded. **Neither side may overrule the other.** A model may direct attention. It may
not manufacture provenance.

### The discover phase consults no model at all

This is where the four questions a first reviewer actually asks are separated, and each
gets its own pure module with a deliberately narrow signature:

| question | module | may read |
|---|---|---|
| where does the paper say this? | `claims.py` | the parsed doc — a lens supplies a quote, the harness mints the address |
| what would settle this concern? | `questions.py` | a finding's own closed-vocabulary self-classification |
| what KIND of problem is this? | `taxonomy.py` | the same closed vocabulary; never a number, a name or a paper |
| what is checkable, and how central? | `discovery.py` | structure: abstract, cited addresses, parsed quantities |
| is it worth it, and is it justified? | `priority.py`, `planner.py` | vocabulary strings and booleans only |

**Those signatures are the invariant.** `grading.derive`, `priority.score`,
`planner.classify` and `taxonomy.classify` take vocabulary strings and booleans and
nothing else — no count, no metric name, no paper identity. That makes "no std dev =
MAJOR", "if epsilon=0.05 never flag" and "if <paper> appears, soften" *inexpressible*
rather than merely absent. `tests/test_reasoning_architecture.py` asserts it. If you widen
one of those signatures you have removed the guarantee, whatever the new parameter is for.

### Three questions, never one

The reviewer's whole job is keeping these apart:

| question | where it lives | what it may do |
|---|---|---|
| Is there an issue? | `candidate_class` (lens) + `grade.verdict` (blinded grader) | only CONFIRMED_FINDING is eligible for FATAL/MAJOR |
| How sure are we? | `confidence`, bounded by `grading.evidence_support` | evidence TYPE caps confidence; a second independent check LIFTS the cap |
| How much does it matter? | `severity` → `counted_severity` | nothing here SETS it; every mechanism only caps it |

**Every mechanism that touches severity may only CAP it.** Nothing in the reviewer can
raise a grade — not the grader, not the lens's own second pass, not the evidence ceiling.
Asserted by sweeping the whole reachable input space:
`RANK[counted_severity] <= RANK[lens_severity]`.

### The funnel is six terms and none substitutes for another

```
discovered → checkable → warranting an experiment → launched → completed → resolved
  objects      objects         plans                 run record  outcomes   outcomes
```

`warranting_experiment` is a JUDGEMENT this system made. `launched` counts processes it
actually started, read off the runner's own count. Reporting the first as though it were
the second is the specific overclaim the split exists to prevent. Likewise
`probe_stage_seconds` is named for what it measures — acquisition, static audit, planning
and gate evaluation — and is **not** the cost of running experiments.

---

## 3b. Four layers that describe the REVIEW, not the paper

These run inside the report stage, read only artifacts that already exist, and decide
nothing. Each is its own module so that what it may see is a signature rather than a
convention — the same discipline `grading.derive` uses to make "no variance = MAJOR"
inexpressible rather than merely absent.

```
harness/outcome.py        the four rows a reviewer reads first
harness/coverage.py       how much of the PAPER this review addressed and examined
harness/docintegrity.py   mechanically determined document facts — observations, not findings
harness/guarantees.py     what the review promises, what it does not, and which held
```

**`outcome.py` — four rows over disjoint inputs.** `## Review outcome` opens every review
with what was established, what was resolved, what execution produced, and what the whole
thing is an assessment of. `finding_state` reads `claim_status` and a count of kept
findings and *nothing* about execution, so a blocked or refused or inadmissible run cannot
move what the review established. If you add a parameter to `finding_state`, a sweep in
`tests/test_architecture_guarantees.py` will tell you why not.

**`coverage.py` — a denominator you cannot fabricate.** `surface(doc)` takes a `PaperDoc`
and nothing derived from the review; `measure` takes its numerators as address **strings**.
The rate this replaced divided the harness's own object list by itself, so a worse
extractor scored higher on it. Read the module docstring: it is the clearest statement in
the repository of what an honest measurement of one's own coverage costs.

**`docintegrity.py` — observations, and whose property each one is.** Every
`DocumentObservation` says `PAPER` or `EXTRACTION`, required, no default. A naive
"cited but not recovered" check over the shipped corpus produced twelve claims that a table
or an equation was missing, and all twelve of those objects are in the papers. The layer
gates nothing, carries no severity field, and is unreachable from every decision function.

**`guarantees.py` — a guarantee checked from an artifact, or not claimed.** Three groups:
PROCESS guarantees enforced in every run (a failure here is a **harness defect** and is the
only thing reaching `unmet`); CONDITIONAL properties true only when a gate was open;
SCIENTIFIC non-guarantees, false on every input by sweep. If you add an invariant to
`CLAUDE.md`, `tests/test_guarantees.py` requires you to decide whether a review can report
it — and to write down the reason if it cannot.

And one runs before the pipeline: **`preflight.py`** answers "is this batch N distinct
papers" from the PDFs' bytes and refuses a batch containing the same document twice.

```bash
../.venv/Scripts/python.exe run.py preflight      # do this before any corpus run
../.venv/Scripts/python.exe tools/loop.py         # the whole acceptance rule, one command
```

`tools/loop.py` is what to run after any change: the suite with a floor under the count,
every module self-check, `run.py evaluate` into a scratch directory,
`manuscript/check_claims.py`, `git diff --stat`, and a write guard asserting that
`projects/`, `manuscript/` and `reports/` were not touched. The acceptance rule here is
**the loop is green, the test count strictly increased, and no test was deleted** — a
superseded assertion keeps its name and gets its history written into its docstring.

## 4. Where the outputs are

Per paper, `projects/<pid>/` holds four things a reader should not confuse:

| file | who reads it |
|---|---|
| `reports/<pid>.review.md` | **a human reviewer.** Two pages. |
| `reports/<pid>.ledger.json` | anyone tracing a line of that report to an artifact |
| `discovery/targets.json` | anyone asking what else was considered, and why not pursued |
| `reports/<pid>.md` / `.json` | the complete machine trace |

Every started process is recorded whole in `runs/<pid>/execution.jsonl` — command, cwd,
commit, timestamps, exit code, full untruncated stdout/stderr, parsed metric. **A
reproduction verdict must be re-derivable from that file by hand.** That is the acceptance
test for any change to the execution path.

The `outputs/` directory shipped alongside this document is one run's results for seven
papers, with the same layout. `outputs/README.md (present in a packaged handoff zip; the development tree keeps its reviews under projects/ instead)` says what each file is.

Batch completeness is a machine-readable artifact, not a printed count:
`reports/corpus.json` lists every *requested* paper with its terminal state. A summary may
say "6 requested · 5 completed · 1 failed". It may never say "5 reviewed" when six were
asked for.

---

## 5. Execution: gates, backends, and the remote sandbox

Everything that costs money or runs someone else's code is a separate, named, off-by-default
gate. They are graded by risk rather than bundled, because fetching code and running code
are different acts.

| gate | env var | default | permits |
|---|---|---|---|
| network | `SH_ALLOW_NETWORK` | **on** | `git clone --depth 1` of the URL the paper advertises, read-only |
| synthesis | `SH_ALLOW_SYNTHESIS` | **on** | the planner authors `runs/<pid>/probe.py` from the paper |
| auto-audit | `SH_ALLOW_AUTO_AUDIT` | off | shelling out to a reviewer for the four lenses |
| grading | `SH_ALLOW_GRADING` | off | a second, blinded reviewer per FATAL/MAJOR finding — zero tools, no filesystem |
| substantive verdict | `SH_ALLOW_SUBSTANTIVE_VERDICT` | off | one whole-paper opinion; printed, counted by nothing |
| install | `SH_ALLOW_INSTALL` | off | building the repository's declared stack |
| **sandbox** | `SH_ALLOW_SANDBOX` | off | **leasing a remote Linux machine** |
| execute | `SH_ALLOW_REPO_EXEC` | off | running the repository's own entrypoint |

`SH_MAX_TARGETS` (default 3) is a **budget, not a gate**: which targets are worth pursuing
is `planner`'s decision and the order is `priority`'s, so lowering it drops the least
useful targets rather than an arbitrary subset. Raising it costs compute and changes no rule.

### Backends

| name | what it is | `can_execute` |
|---|---|---|
| `local` | this machine, subprocesses | yes |
| `modal` | a leased Linux sandbox (`harness/sandbox.py`) | yes, with credentials + the gate |
| `kaggle`, `colab` | declarations of published hardware | **no** — `execute()` raises |

The declarations are not stubs to be filled in. They exist so a refusal can say *"a 16 GiB
T4 would fit this experiment but cannot be provisioned from here"*, which tells an operator
what to do next where a bare "no backend" does not.

`backends.select_for` is a **matching** problem, not a config lookup: it compares the cited
experiment's declared demand against every registered profile. Among backends that all meet
every stated requirement, the one the operator NAMED wins, then the smallest sufficient.

Code **this harness authored** — a generated probe, a noise-floor calibration — always runs
on `local` whatever `SH_EXEC_BACKEND` says, because running it elsewhere would measure a
different machine and answer a different question. Only third-party repository execution is
subject to backend selection.

### Running a paper's code remotely

Copy `.env.sandbox.example` to `.env.sandbox`, add a provider token
(`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`, or `modal token new`), and set
`SH_EXEC_BACKEND=modal`, `SH_ALLOW_SANDBOX=1`, `SH_ALLOW_NETWORK=1`, `SH_ALLOW_INSTALL=1`,
and `SH_ALLOW_REPO_EXEC=1`.

**Do your first run with `SH_ALLOW_REPO_EXEC=0`.** That exercises leasing, staging by SHA,
in-place commit verification, capability assessment and release, while executing nothing.

One sandbox is leased **per paper**, not per command: `plan_execution` runs once per target
and a review runs one checkout many times, so a provider call per command would re-clone and
re-install for each one. `provision` fetches the audited commit, interrogates the machine,
refuses it if it is smaller than the reservation, and builds the repository's stack.
`stages/probe.run` releases the lease in a `finally`; `SH_SANDBOX_IDLE_TIMEOUT` makes a
leaked one terminate itself; `run.py sandbox --release` catches the rest. **A sandbox
nobody released bills until its own timeout** — that is the one failure mode in this seam
that costs money rather than accuracy, and it accrues silently.

Four things about the remote path are asserted rather than assumed
(`tests/test_sandbox_backend.py`):

- **No silent local fallback.** Every failure to obtain or stage a machine refuses, and a
  host path in an argv is refused rather than rewritten. A backend that ran a Linux
  repository on a Windows host because the provider was down would produce a startup crash
  whose stderr is indistinguishable from the authors' code being broken.
- **E2 is verified inside the sandbox, by the same function.** `repo.GitTree` abstracts the
  three reads commit verification needs, so `verify_commit` is written once and certifies a
  local checkout and a remote one identically. A missing session yields a tree that cannot
  be read → `unknown` → blocked, rather than falling back to certifying this disk.
- **The import check is delegated** to the interpreter that will run it.
- **A reservation is not a measurement.** `resources()` is the request; `execute()` stamps
  what the machine reported.

There is deliberately **no agent in the sandbox**. The earlier system this capability was
restored from ran a coding agent inside the pod to author and run experiments from a brief.
That is the right shape for generating research and the wrong shape for reviewing it: a
number produced by code an agent wrote in a container measures the agent, not the paper.

---

## 6. The rules you must not weaken

These are not style preferences. Each one closed a specific defect that had produced a
wrong or unearned conclusion. Do not relax them to make more papers executable or more
findings reportable. The full list with its reasoning is in `CLAUDE.md`; these are the ones
you are most likely to trip over.

1. **Every `evidence_quote` is re-verified against the parsed paper.** A cell citation must
   match that cell. Unsubstantiated findings are dropped and counted. A prose address is no
   exception: `claims.mint` refuses a quote occurring twice, and `claims.resolve` re-reads
   the span off the document, so a lens writing its own address gains nothing.
2. **`verified_observation` and `evidence_class` are written by the harness, never read
   from a lens file.** A lens cannot certify its own reasoning.
3. **The provenance ceiling.** Only `driver` or `repo_exec` provenance may reconcile a
   printed cell, in *either* direction. A synthesized probe can neither convict nor acquit.
   Enforced twice — at the reconciler, and again where `evidence_state` is derived — so a
   synthesized diagnostic cannot convict through the report after being refused by the
   reconciler.
4. **Only `authorize()` may permit repository execution**, and it requires all of: the gate
   open, a backend that `can_execute`, `repo_exec` provenance, a verified commit,
   experiment + metric + configuration identity, capability, and sufficient resources.
5. **Commit mismatch, a dirty tree, or an unverifiable commit blocks execution.** An
   uninspectable tree is not a clean tree.
6. **Resource insufficiency — including *unknown* demand — yields `INCONCLUSIVE`.** A
   paper's silence about its own cost is not evidence the cost is small.
7. **Capability, environment, dependency and platform failures yield `INCONCLUSIVE`, never
   `FAILED_REPRODUCTION`.** Only a crash *after* the experiment demonstrably started may
   convict.
8. **The paper decision is a table, not a judgement and not a count.**
   `MATERIAL_SEVERITY = ("FATAL",)`. Nothing accumulates: no number of MAJORs or MINORs
   ever reaches RED, because a concern weakens a claim and does not reject one. Turning
   grading off reproduces the ungraded decision exactly, by construction.
9. **No experiment is shrunk, substituted or downscaled to make it fit.** There is no
   function that does this, deliberately.
10. **No paper-specific logic.** The pilot papers are evaluation cases, not special cases.
11. **Every mechanism that touches severity may only CAP it.**
12. **Nothing may collapse the three questions into one.** In particular no cap is keyed on
    *evidence type* — a weak citation bounds CONFIDENCE, and confidence bounds severity.
13. **Every requested paper reaches exactly one terminal state**, and `harness/corpus.py`
    asserts it.
14. **A retry budget is spent only where spending it could change the answer.** A rate
    limit, an outage, a missing CLI, a revoked credential, an unknown flag and an unreadable
    PDF are not transient failures.
15. **What may be pursued is decided by the harness, never by a lens.**
    `Finding.verifiable_by_experiment` is an unchecked model boolean and is metadata.
16. **A blocked target ends that target, never the paper.** One paper reporting a blocked
    target, a reproduced one and a failed one must report all three.
17. **A colour may not be a property of this harness's configuration.** The first triage
    made all seven corpus papers YELLOW because the execution gates are shut by default.
    Only a target that was ATTEMPTED and settled nothing colours a paper.
18. **Positional coincidence may not establish a reconciliation.** `parse_metric` prefers a
    target-bound output object over a named one over a structured one over a bare key, and
    REFUSES when the winning tier disagrees with itself.
19. **The reviewer's report and the machine trace are two artifacts.** The report is bounded
    BY CONSTRUCTION — a per-category cap, a global finding cap, a cap on every other
    section — not by hoping the paper is short. What it drops, it says it dropped.
20. **A necessity decision is not evidence.** `NO_EXPERIMENT_NEEDED` is a *successful*
    review outcome on the necessity axis and resolves nothing on the evidence axis:
    declining to run something does not settle a missing control.

---

## 7. What is honestly not proven

Read this before you quote a capability to anyone.

- **The remote sandbox has never been leased.** `harness/sandbox.py` is driven end to end —
  lease, stage by SHA, interrogate, refuse a short machine, build the stack, verify the
  commit in place, run, release — against a scripted provider double, and every call it
  makes was checked against the installed `modal` 1.5.4 signatures. No live sandbox has been
  created, because the development host holds no provider credentials. The logic is tested;
  the integration is not. **The seven results in `outputs/` were all produced on the `local`
  backend, before the remote backend existed.**
- **No paper has been RED under the binary rule.** RED needs a counted FATAL or a failed
  reproduction from an admissible provenance, and the seven papers produced neither. That is
  the intended conservatism, but it means the RED path is proven by fixtures
  (`tests/test_autonomous_review_e2e.py`) rather than by a real paper.
- **Neither the grader nor the assessor ran on the corpus.** `grade_coverage` shows 0 of 15
  serious candidates graded, and no paper carries a `substantive_verdict`: both gates are off
  by default and were off. Every counted severity in those seven reviews is the asserting
  lens's own.
- **The corpus lens files were not produced through `audit_driver`.** All 28 are
  `written_by: manual_accept`, one subagent per lens on one model, each recording
  `tool_policy: unrecorded`. Per-lens isolation held — four processes, no shared context —
  but the sandboxed cwd, the `--allowedTools` restriction and per-lens model selection did
  not apply.
- **No adjudicated ground truth exists for this corpus.** `harness/evaluation.py` therefore
  reports no precision, recall, or agreement-with-humans number, and says so inside the
  artifact. Reviewer accuracy is **unmeasured**, not measured-and-good.
- **`scientific_class` falls back to the lens name** when a finding declared neither a
  `discrepancy_type` nor a `baseline_class`. In those cases the count is partly a count of
  what each lens chose to write.
- **The prior-art route runs and establishes no novelty, by construction.**
  `harness/literature.py` searches public indexes for work predating the paper, against
  the paper's own novelty sentences. It can raise a concern a referee must adjudicate; it
  can never report that a contribution is new, because
  `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` is the empty tuple and a completed search
  with no match is a fact about the search. Over the eight-paper corpus it produced 0
  concerns and 0 bound relations — see `docs/LITERATURE_MEASUREMENT.md` for why each zero
  is the right number, and for the three limits (undatable papers, a metered index, a
  bounded protocol) that are ours rather than the papers'.
- **PATH B is eligibility-only.** `harness/reimplement.py` decides whether a paper with no
  published code says enough to rebuild, and writes the brief when it does. No independent
  reimplementation has been written and sealed through `run.py accept`.
- **Prose identity binds COUNTS only.** An accuracy claimed only in text is discovered and
  is not executable, because a prose-stated accuracy has no column header, no basis and no
  baseline row to bind against.
- **Commit pinning is second-run-onward.** A paper's first acquisition is an unpinned
  depth-1 clone of the default branch.

### The refusal that best explains the system

With the gates open, FinChain's composition claim — which exists only in the paper's prose —
is discovered, addressed, its arithmetic re-verified (58 × 5 × 10 = 2,900), prioritised, and
pursued against a cloned, commit-verified, statically audited checkout. `experiment_id` then
refuses: *"none of the 3 advertised command(s) emits a count, so nothing in this checkout
produces the total the paper states."*

That is true of the repository. It ships the generators; producing the advertised benchmark
means looping all 58 of them, and no command it publishes does that. **The refusal is the
guarantee working.** Assembling that loop is one line and the harness will not write it,
because a number from a command the authors never published measures our loop rather than
their artifact.

---

## 8. Working on it

- **Read the module self-check before the module.** `python -m harness.<name>` is a
  compressed spec and runs in seconds.
- **Match the surrounding style.** The comment density is deliberate: comments here carry
  the reasoning and the defect each branch closed, because the alternative is that the next
  person removes a check that looks redundant. If you add a guard, say what it caught.
- **Never edit `projects/<pid>/audit/prompts/*.md`.** Regenerated every run.
- **Line endings are LF** (`.gitattributes`). Writing files from Python on Windows needs
  `newline=""` or you will reintroduce CRLF.
- **A test that asserts a wrong thing is worse than no test.** Several tests here exist to
  pin a *refusal*; if one starts failing, first establish whether the refusal was correct.
  Two defects during the remote-backend work were found by a self-check assertion failing,
  and in both cases the assertion was right and the code was wrong.
- **`pytest.ini` is load-bearing.** It makes `single-harness/` its own rootdir so the
  repo-root `conftest.py` — which opens a Postgres connection — is never imported. Without
  it the whole suite dies on `connection refused`.
