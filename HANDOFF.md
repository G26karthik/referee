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
# The whole suite — start here. Current tree: 2282 tests collected, 2263 of them under
# `-m "not docker and not network"` (measured with --collect-only: docker needs a reachable
# container runtime, network reaches third-party indexes — see pytest.ini). Earlier "1571
# passed" / "1027 passed, 2 skipped" claims for this command are stale: the suite has grown
# substantially since (three whole evidence routes' worth), and the `docker` marker did not
# exist when they were written.
PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests -q

# every module also self-checks in isolation; each is a fast readable spec
PYTHONUTF8=1 ../.venv/Scripts/python.exe -m harness.taxonomy
PYTHONUTF8=1 ../.venv/Scripts/python.exe -m harness.sandbox      # leases nothing
```

The self-checks are worth your time before the tests. Each one is a compressed statement
of what its module guarantees, and running `python -m harness.<name>` for the module you
are about to touch is the cheapest way to learn its contract. There are **70** of them.
Do not go looking for a hand-maintained list here — an earlier version of this document
tried to name them and drifted stale as modules kept landing; `tests/test_self_checks.py`
DISCOVERS every module under `harness/**/*.py` carrying an `if __name__ == "__main__"`
guard by parsing for it with `ast`, so a module that loses its self-check fails the suite.
`python -m harness.stages.<report|grade|discover|artifact|literature|validation>` runs a
stage's own self-check the same way.

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
papers → pipeline → ingest → audit → collect → grade → assess → discover → probe → report
                                                              └ questions · targets · priority · plan
```

> **v4 note (2026-09-21):** this section previously named the reference implementation's
> module set (`stages/*.py`, `*_driver.py`, `grading.py`, etc). 89 modules were
> consolidated into 12 new ones plus a smaller kept-satellite set; the table below names
> the current module for each responsibility. `harness/pipeline.py` is the v4 name for
> what this file used to call `controller.py`.

| phase | module | input → output | decides | refuses by |
|---|---|---|---|---|
| ingest | `stages/ingest.py` | PDF → `paper/doc.json` | nothing (deterministic) | `error` on an unreadable PDF |
| audit | `harness/audit.py`, dispatched via `harness/agent.py` | doc → `audit/<lens>.json` ×4 | the four lenses judge | `waiting`, resumable |
| collect | `harness/audit.load_reports` | lens files → verified findings | quote ≟ paper | drops the finding, counts it |
| grade | `harness/audit.py` + `harness/agent.py`'s grade role | serious findings → `audit/grade/<slug>.json` | a second, blinded reviewer per candidate | partial coverage; never blocks a report by default |
| **discover** | `harness/discover.py` | doc + findings → `discovery/targets.json` | what is addressable, what it is worth, whether an experiment is justified | a NAMED refusal per target |
| verify | `harness/routes.py` | doc + repo → `ProbeSpec` per target | identity, capability, resources, commit, backend | leaves the spec unpromoted |
| execute | `harness/execute.py` | spec → `ProbeResult` | `authorize()` alone | `verdict: blocked` |
| reconcile | `harness/execute.reconcile` | metric vs the addressed quantity | arithmetic only | `INCONCLUSIVE` |
| report | `harness/report.py` | everything → `reports/<pid>.review.md` | threshold table over `counted_severity` | — |

`harness/pipeline.py` drives; the deterministic code below it decides what may be
concluded. **Neither side may overrule the other.** A model may direct attention. It may
not manufacture provenance.

### The discover phase consults no model at all

This is where the four questions a first reviewer actually asks are separated, and each
gets its own pure module (or function group within one) with a deliberately narrow
signature:

| question | module | may read |
|---|---|---|
| where does the paper say this? | `harness/locate.py` | the parsed doc — a lens supplies a quote, the harness mints the address |
| what would settle this concern? | `harness/discover.py` | a finding's own closed-vocabulary self-classification |
| what KIND of problem is this? | `harness/taxonomy.py` | the same closed vocabulary; never a number, a name or a paper |
| what is checkable, and how central? | `harness/discover.py` | structure: abstract, cited addresses, parsed quantities |
| is it worth it, and is it justified? | `harness/decide.py` (`classify_plan`, `plan`) | vocabulary strings and booleans only |

**Those signatures are the invariant.** `harness/audit.derive`, `harness/decide.classify_plan`
and `harness/taxonomy.classify` take vocabulary strings and booleans and
nothing else — no count, no metric name, no paper identity. That makes "no std dev =
MAJOR", "if epsilon=0.05 never flag" and "if <paper> appears, soften" *inexpressible*
rather than merely absent. If you widen
one of those signatures you have removed the guarantee, whatever the new parameter is for.

### Three questions, never one

The reviewer's whole job is keeping these apart:

| question | where it lives | what it may do |
|---|---|---|
| Is there an issue? | `candidate_class` (lens) + `grade.verdict` (blinded grader) | only CONFIRMED_FINDING is eligible for FATAL/MAJOR |
| How sure are we? | `confidence`, bounded by `harness/audit.evidence_support` | evidence TYPE caps confidence; a second independent check LIFTS the cap |
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

### One route that hangs off discover/probe, and the rung it cannot reach

A further module answers a question the four-lens audit cannot. It is bounded by a
named **empty-tuple device** rather than a threshold, so its top rung is not merely
unreached — there is no value that could reach it, which a sweep over the whole vocabulary
asserts rather than a docstring promises.

**Static artifact inspection** (`harness/artifact_evidence.py`, `harness/stages/artifact.py`
— both kept as their own files, unchanged, in the v4 redesign)
asks what the RELEASED CODE establishes, for a question whose SCOPE a bounded probe answers
— `FILE_PRESENCE`, `ENTRYPOINT_PRESENCE`, `DEPENDENCY_DECLARED`, `MANIFEST_PRESENCE`,
`CONFIG_LITERAL`, `COMMAND_PRESENCE`. With a paper span that also relocates and an experiment
identity a deterministic source establishes, it can report a `PAPER_ARTIFACT_MISMATCH`. It
cannot settle `IMPLEMENTATION_CORRESPONDENCE` — "does this code implement the method" — which
is excluded from what a bounded fact may discharge by construction, and it cannot say the
paper's result is false: `harness/schema.ARTIFACT_AUTHORITY` has exactly three members and
that rung is not one of them.

> Two sibling routes described here in earlier revisions of this document — bounded
> prior-art search and focused (between-arms) validation — were deleted 2026-09-20 for
> measuring zero endpoint-verified value over the eight-paper corpus (0 structurally-bound
> relations; 0 of 7 targets reaching an executable design). See `CLAUDE.md`'s "Known
> limitations" for the measurements. They are not part of the current system.

---

## 3b. Four layers that describe the REVIEW, not the paper

These run inside `harness/report.py` (consolidated from four separate reference-
implementation modules into distinct function groups within one file), read only
artifacts that already exist, and decide nothing. Each stays a distinct group so that what
it may see is a signature rather than a convention — the same discipline
`harness/audit.derive` uses to make "no variance = MAJOR" inexpressible rather than merely
absent.

```
harness/report.py — outcome layer        the four rows a reviewer reads first
harness/report.py — coverage layer       how much of the PAPER this review addressed and examined
harness/report.py — document-integrity   mechanically determined document facts — observations, not findings
harness/report.py — guarantees layer     what the review promises, what it does not, and which held
```

**The outcome layer — four rows over disjoint inputs.** `## Review outcome` opens every
review with what was established, what was resolved, what execution produced, and what
the whole thing is an assessment of. `finding_state` reads `claim_status` and a count of
kept findings and *nothing* about execution, so a blocked or refused or inadmissible run
cannot move what the review established.

**The coverage layer — a denominator you cannot fabricate.** `surface(doc)` takes a
`PaperDoc` and nothing derived from the review; `measure` takes its numerators as address
**strings**. The rate this replaced divided the harness's own object list by itself, so a
worse extractor scored higher on it.

**The document-integrity layer — observations, and whose property each one is.** Every
`DocumentObservation` says `PAPER` or `EXTRACTION`, required, no default. A naive
"cited but not recovered" check over the shipped corpus produced twelve claims that a table
or an equation was missing, and all twelve of those objects are in the papers. The layer
gates nothing, carries no severity field, and is unreachable from every decision function.

**The guarantees layer — a guarantee checked from an artifact, or not claimed.** Three
groups: PROCESS guarantees enforced in every run (a failure here is a **harness defect**
and is the only thing reaching `unmet`); CONDITIONAL properties true only when a gate was
open; SCIENTIFIC non-guarantees, false on every input by sweep. If you add an invariant to
`CLAUDE.md`, decide whether a review can report it — and write down the reason if it
cannot.

And one runs before the pipeline: **`harness/pipeline.preflight_check`** answers "is this
batch N distinct papers" from the PDFs' bytes and refuses a batch containing the same
document twice.

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

## 5. Execution: gates and backends

Everything that costs money or runs someone else's code is a separate, named, off-by-default
gate. They are graded by risk rather than bundled, because fetching code and running code
are different acts.

> **v4 note (2026-09-21):** the prior-art search, literature review, validation design and
> remote-sandbox gates/rows this section described in earlier revisions were deleted
> 2026-09-20, before this rewrite — each measured zero endpoint-verified value (prior-art
> and literature) or zero executable designs (validation) over the eight-paper corpus, and
> the sandbox backend had never been leased on this host in any revision. See `CLAUDE.md`'s
> "Known limitations" for the measurements. This table lists what remains.

| gate | env var | default | permits |
|---|---|---|---|
| network | `SH_ALLOW_NETWORK` | **on** | `git clone --depth 1` of the URL the paper advertises, read-only |
| synthesis | `SH_ALLOW_SYNTHESIS` | **on** | `harness/decide.py` authors `runs/<pid>/probe.py` from the paper |
| auto-audit | `SH_ALLOW_AUTO_AUDIT` | off | shelling out to a reviewer for the four lenses |
| grading | `SH_ALLOW_GRADING` | off | a second, blinded reviewer per FATAL/MAJOR finding — zero tools, no filesystem |
| substantive verdict | `SH_ALLOW_SUBSTANTIVE_VERDICT` | off | one whole-paper opinion; printed, counted by nothing |
| authors' code | `SH_ALLOW_ARTIFACT_REVIEW` | off | one read-only pass over the pinned checkout, `Read`+`Grep` only, every citation relocated |
| install | `SH_ALLOW_INSTALL` | off | building the repository's declared stack |
| execute | `SH_ALLOW_REPO_EXEC` | off | running the repository's own entrypoint |

**The artifact-review gate runs to a terminal state with the gate closed**, because it is a
deterministic half sitting in front of an optional model reading: `stages/artifact.py`
produces its level-1 facts with `SH_ALLOW_ARTIFACT_REVIEW=0`. That property is what makes
the model channel MEASURABLE at all: closing the gate removes the one step a model could
have taken, not the route.

`SH_MAX_TARGETS` (default 3) is a **budget, not a gate**: which targets are worth pursuing
is `harness/decide.py`'s decision (`classify_plan`) and the order is set alongside it, so
lowering it drops the least useful targets rather than an arbitrary subset. Raising it costs
compute and changes no rule.

### Backends

| name | what it is | `can_execute` |
|---|---|---|
| `local` | this machine, subprocesses | yes |
| `container` | a Docker container (`harness/container.py`) — this host's real, credentialed isolation path | yes |
| `kaggle`, `colab` | declarations of published hardware | **no** — `execute()` raises |

The declarations are not stubs to be filled in. They exist so a refusal can say *"a 16 GiB
T4 would fit this experiment but cannot be provisioned from here"*, which tells an operator
what to do next where a bare "no backend" does not.

`execute.select_for` is a **matching** problem, not a config lookup: it compares the cited
experiment's declared demand against every registered profile. Among backends that all meet
every stated requirement, the one the operator NAMED wins, then the smallest sufficient.

Code **this harness authored** — a generated probe, a noise-floor calibration — always runs
on `local` whatever `SH_EXEC_BACKEND` says, because running it elsewhere would measure a
different machine and answer a different question. Only third-party repository execution is
subject to backend selection.

> A remote-sandbox backend (`harness/sandbox.py`, leased-per-paper Linux execution via
> Modal) existed here and was deleted 2026-09-20: never leased on this host in any
> revision (no provider credentials were ever present). `container` above is this host's
> real isolation path for running a paper's own code and is unaffected by that deletion.

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
13. **Every requested paper reaches exactly one terminal state**, and `harness/pipeline.account`
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
21. **A completed search that matched nothing establishes nothing.**
    `SEARCH_COMPLETED_NO_MATCH_FOUND` is a fact about a bounded protocol, never about the
    paper, and `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` is the empty tuple so no later
    contributor can wire a match count to "novel".
22. **A relocated source line proves the line exists, never that it sets the number
    somebody says it sets.** `ENDPOINTS_VERIFIED_ARTIFACT_CONCERN` is two real locations and
    a model's reading of the correspondence between them; only an experiment identity a
    DETERMINISTIC source establishes, itself relocated in the pinned tree, may lift a
    concern to `PAPER_ARTIFACT_MISMATCH`.
23. **An experiment whose scientific choices this harness supplied is not the paper's
    experiment.** All eight `artifacts.VALIDATION_INGREDIENTS` must bind from the paper or
    the pinned artifact; a design that would need this harness to invent an optimizer,
    split, threshold or schedule is `SPECIFICATION_BLOCKED`, not completed from convention.
24. **A focused validation is never reconciled against a printed cell.** The paper printed
    neither arm of a between-arms comparison, so `local_exec.reconcile` refuses a
    `BETWEEN_ARMS` spec outright; what the comparison may say is bounded separately by
    `between_arms.authority_for`, and "the credited mechanism causes the effect" has no
    value in `artifacts.CAUSAL_ATTRIBUTION_AUTHORITIES`, the empty tuple.

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
- **No adjudicated ground truth exists for this corpus.** `harness/summarize.py` therefore
  reports no precision, recall, or agreement-with-humans number, and says so inside the
  artifact. Reviewer accuracy is **unmeasured**, not measured-and-good.
- **`scientific_class` falls back to the lens name** when a finding declared neither a
  `discrepancy_type` nor a `baseline_class`. In those cases the count is partly a count of
  what each lens chose to write.
- **The artifact route has established 0 `PAPER_ARTIFACT_MISMATCH` on the current
  `runs_final_2026-09-16` corpus.** The gated authors'-code auditor
  (`harness/artifact_review_driver.py`, behind `SH_ALLOW_ARTIFACT_REVIEW`) is genuinely
  wired into the production route: `harness/stages/artifact.py`'s `run_route` calls it once
  per paper, gated on the env var and on an audited, cleanly pinned checkout, and what it
  returns is merged through the same `ArtifactFact` channel the deterministic probes
  already use — `tests/test_artifact_route.py` proves the wiring. All four repository
  papers were inspected and produced 7 distinct narrow, scoped facts about the checkouts
  (`acl=2, apt-icml=2, cvpr=1, iclr=2`) — entry points, declared dependencies, one source
  pattern needing a human read — and 0 reached a bound mismatch or even an endpoint-verified
  concern this run. Broad implementation-correctness questions remain excluded from what a
  bounded fact may discharge by construction regardless. The driver is a live model
  subprocess with no fixed seed, so this count can shift across regenerations of the same
  run directory; see `docs/ARTIFACT_ROUTE_MEASUREMENT.md` and the manuscript's own
  artifact-route paragraph for the currently-checked-in numbers.
- **The prior-art route runs and establishes no novelty, by construction.**
  `harness/literature.py` searches public indexes for work predating the paper, against
  the paper's own novelty sentences. It can raise a concern a referee must adjudicate; it
  can never report that a contribution is new, because
  `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` is the empty tuple and a completed search
  with no match is a fact about the search. Over the eight-paper corpus it produced **0
  concerns that survived** — the only candidate that reached endpoint verification, an
  arXiv posting matching `acl`'s own title, benchmark name and figures, turned out to be
  that paper's own preprint and was refused rather than reported, because a paper is not
  prior art for itself. See `docs/LITERATURE_MEASUREMENT.md` for why each zero is the right
  number, and for the three limits (undatable papers, a metered index, a bounded protocol)
  that are ours rather than the papers'.
- **The focused-validation route has never executed on a real paper.** Re-running discovery
  over the eight-paper corpus offline: 23 focused-validation questions raised, 7 reached the
  route, 2 designs attempted, and both `SPECIFICATION_BLOCKED` at the first ingredient,
  `arm_instantiation` — the two targets (one ATTRIBUTION, one CONTROL_PRESENCE) carry an
  empty metric and an empty benchmark, so no `COMPARISON` node from the paper's own claim
  graph (74 exist across the corpus) can bind to either. Opening
  `SH_ALLOW_VALIDATION_DESIGN` changes nothing: the deterministic half refuses first and the
  designer is never called. See `docs/FOCUSED_VALIDATION_MEASUREMENT.md`.
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
