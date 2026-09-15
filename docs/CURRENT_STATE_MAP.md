# Current-state architecture and failure-surface map

> **Superseded in part, 2026-09-05.** The three structural gaps this document identified —
> no target set, no target prioritization, no evidence graph — have since been closed, and
> so has the cell-only claim address in §2.2. `docs/HARNESS_ARCHITECTURE.md` and `CLAUDE.md`
> describe the system as it now stands; this file is kept because the diagnosis is what the
> work was built from, and because several of its verdicts (items 4, 5, 7, 9, 10 already
> closed) still hold unchanged. Where it says "does not exist", read "did not exist".
> `docs/IMPLEMENTATION_REPORT.md` says what changed and what did not.

Written for an external reviewer who asked for the codebase rather than the manuscript,
and who intends to reconstruct the system from code. This document is the reconstruction,
done from the code, with anchors so every claim in it can be checked in one jump.

**Authority.** `manuscript/manuscript.pdf` is the published description of the system.
This file and the code are the implementation. Where they disagree, the code is right and
this file says so explicitly.

**Companion artifact.** `single-harness-codebase.zip` — 95 files: `harness/`, `tests/`,
`docs/`, `run.py`, the campaign's own JSON evidence (`manuscript/*.json`), the manuscript
source, and the top-level dossier. Excluded: `projects/` (126 MB of run outputs),
`papers/` (22 MB of PDFs), `reports/archive` (44 MB), caches and build products. Nothing
excluded is load-bearing for reading the system; `projects/<pid>/` can be regenerated and
`reports/corpus.json` is included so the batch accounting is visible.

---

## 0. The requested pipeline vs. the implemented one

The reviewer's proposed 12-stage diagram is not what runs. Mapping it onto the code:

| requested stage | implemented? | where |
|---|---|---|
| INGEST | yes | `harness/stages/ingest.py`, `harness/pdf.py` |
| AUDIT | yes | `harness/stages/audit.py`, `harness/audit_driver.py` |
| **TARGET DISCOVERY** | **no — not a stage** | one target is picked as a side effect at `harness/stages/probe.py:79-81` |
| **TARGET PRIORITIZATION** | **no — not a stage** | reuses the *report display* sort, `report.rank` / `finding_key` (`harness/stages/report.py:231-252`) |
| CODE / RECONSTRUCTION | partial | acquisition `harness/repo.py:550`; PATH B is **eligibility-only**, `harness/reimplement.py` |
| AUTHORIZATION | yes, strong | `harness/backends.py:703` `authorize()` |
| EXECUTION | yes | `harness/local_exec.py`, `harness/backends.py` |
| OBSERVATION | yes | `runs/<pid>/execution.jsonl`; startup evidence `harness/local_exec.py:333` |
| RECONCILIATION | yes, single-target | `harness/local_exec.py:422` `reconcile()` |
| **EVIDENCE GRAPH** | **no** | `ProbeResult.reconciliation` is one optional object (`harness/artifacts.py:1270+`) |
| SCIENTIFIC DECISION | yes, mechanical | `harness/stages/report.py:278` `claim_status`, `:334` `overall_verdict` |
| FINAL RED/GREEN | yes | same two functions |

The actual phase list is six, and it is a literal constant the controller walks:

```
ingest → audit → collect → grade → probe → report        # harness/artifacts.py PHASES
```

`TARGET DISCOVERY`, `TARGET PRIORITIZATION` and `EVIDENCE GRAPH` are the three missing
boxes. Everything the reviewer diagnosed in items 1–4 of their list is a consequence of
those three absences, not four independent defects.

---

## 1. Stage-by-stage failure surface

Columns as requested: supposed to do → what the code does → admissible evidence → silent
failure → LLM bypass surface → scientific or engineering.

### INGEST — `harness/stages/ingest.py`, `harness/pdf.py`

- **Supposed to**: PDF → structured `PaperDoc` (sections, tables with addressable cells,
  figure captions, extracted equations, reported numbers).
- **Does**: exactly that, deterministically. No model involved.
- **Admissible evidence downstream**: only what it emitted. Sections are kept as
  *separate* units (`harness/stages/audit.py:101` `source_units`) — a deliberate fix: a
  concatenated corpus let a quote straddling a section seam verify, and the harness then
  attested in its own voice that a string which does not occur in the PDF occurs
  verbatim.
- **Silent failure**: equation extraction returns nothing rather than mis-assembling
  (documented in `CLAUDE.md`). Table extraction failure is *not* silent downstream —
  it degrades into "no cell citations exist", which then silently caps the whole
  pipeline (see §2.2). **This is the biggest un-instrumented silent failure in the
  system**: there is no per-paper extraction-coverage metric saying "this paper printed
  N results and I addressed M of them".
- **LLM bypass**: none. No model runs here.
- **Class**: engineering, with scientific consequences.

### AUDIT — `harness/stages/audit.py`, `harness/audit_driver.py`, `harness/prompts/audit.py`

- **Supposed to**: four independent lenses (overclaim, contradiction, confound, protocol)
  read the paper and write findings.
- **Does**: one subprocess per lens, sandboxed empty cwd, `--allowedTools`, `--add-dir`
  scoped to the papers directory. A lens can `Read` the PDF but not its siblings' files
  nor the harness source.
- **Admissible evidence**: a quote the harness can re-verify against the parsed doc.
  `evidence_class` and `verified_observation` are **harness-written, never read from a
  lens file** — asserted by `tests/test_finding_traceability.py:119`
  `test_a_lens_cannot_certify_its_own_reasoning`. Evidence *origin* is derived from the
  ref shape, not trusted (`harness/stages/audit.py:69` `_origin_from_ref`;
  `tests/test_finding_traceability.py:476`).
- **Silent failure**: a lens that returns a well-formed empty finding list is
  indistinguishable from a lens that read a clean paper. Counted, not diagnosed.
- **LLM bypass surface — the real one**: `verifiable_by_experiment` is a **plain boolean
  the lens writes** (`harness/prompts/audit.py:356`, ingested at
  `harness/stages/audit.py:546`). Nothing checks it. A lens that fails to set it on a
  reproducible claim removes that claim from the reproduction pipeline permanently and
  invisibly. This single unchecked boolean is the gate on the entire execution half of
  the system.
- **Class**: scientific.

### COLLECT — `harness/stages/audit.load_reports`

- **Does**: re-verify every `evidence_quote` against the parsed paper; drop
  unsubstantiated findings and count the drops. Clamp every lens-supplied enum to a
  closed vocabulary (`_enum`, `harness/stages/audit.py:57`). Collapse `finding_id` and
  `title` to one line (`_oneline`, `:88`) so a lens cannot forge a markdown heading —
  e.g. a fake `## Verdict: GREEN` — in the rendered report.
- **LLM bypass**: closed here for evidence and formatting. Not closed for
  `verifiable_by_experiment`, which is a boolean and so survives `_enum`.
- **Class**: engineering; this stage is the trust anchor and is well built.

### GRADE — `harness/stages/grade.py`, `harness/grade_driver.py`, `harness/grading.py`

- **Does**: a second, blinded reviewer per FATAL/MAJOR candidate, **zero tools, no
  filesystem access**. Its output feeds `grading.derive` — a pure table, no I/O, no model
  call (`harness/grading.py:277`).
- **The safety property**: `RANK[counted_severity] <= RANK[lens_severity]`, swept over the
  whole reachable input space. Grading can only **demote**. Turning it off reproduces the
  ungraded verdict byte for byte, because `counted()` falls back to `severity`
  (`harness/stages/report.py:218`).
- **`derive`'s signature is the invariant**, not a convention: it admits vocabulary
  strings and booleans only — no count, no number, no metric name, no paper identity. So
  "no std dev ⇒ MAJOR", "if epsilon=0.05 never flag" and "if <paper> appears, soften" are
  *inexpressible*, not merely absent. `tests/test_reasoning_architecture.py` asserts the
  signature.
- **Residual LLM authority — state it plainly**: with grading OFF (the default), the
  lens's own asserted `severity` is what `counted()` returns, and a lens-asserted FATAL
  on a re-verified cell quote is sufficient for RED. The caps in `derive` still apply
  (candidate_class, evidence→confidence→severity ceiling, self-contradictory confidence),
  but the *floor* is a model's word.
- **Class**: scientific. This is the honest answer to the reviewer's item 8.

### PROBE / TARGET SELECTION — `harness/stages/probe.py`

This is where the reviewer's items 1, 2, 3 and 4 all actually live.

```python
# harness/stages/probe.py:78-81
candidates = [f for f in report_stage.rank([f for r in reports for f in r.findings])
              if f.verifiable_by_experiment]
target = candidates[0] if candidates else None
```

- **One target. Per paper. Per run.** No set, no enumeration, no coverage, no fallback to
  target #2 when target #1 blocks. When it blocks, the paper's reproduction is over.
- The ranking is `report.finding_key` — a **display sort** built for report ordering
  (severity, then evidence strength, then `verifiable_by_experiment`, then a lens
  tiebreak, then id). It is not a reproduction-feasibility ranking: it knows nothing about
  cost, dependency availability, dataset presence, or whether the repo implements the
  baseline.
- **Admissible claim source is a table cell and nothing else.** `_CELL_REF =
  ^T\d+:r\d+:c\d+$` gates both halves: `grounded_claimed_delta` returns `None` for
  anything that is not a cell address (`:55`), and `spec.table_ref` is only auto-set from
  a cell address (`:134`). A prose-stated result cannot become a reconciliation target on
  the autonomous path. **The reviewer's item 3 is confirmed exactly, at one regex.**
- **Silent failure**: a paper whose central claim is prose reaches `report` with
  `reproduction_status = NOT_ATTEMPTED` and no statement anywhere that a reproducible
  claim existed and was structurally unreachable. That is the difference between "we
  could not" and "we did not look", and the report currently prints the former.
- **Guarded well**: stale-spec feedback. `written_by == "harness"` marks the harness's own
  previous output so a stale run's `finding_id`/`table_ref` cannot freeze the target after
  the audit moved (`:82-96`). The by-id map is keyed `(lens, finding_id)` because four
  lenses independently emit `overclaim-01` and a bare-id map silently kept whichever lens
  loaded last (`:112-116`). Both are real bugs already fixed.
- **Class**: scientific.

### ACQUISITION / IDENTITY / RESOURCES — `harness/repo.py`, `harness/experiment_id.py`, `harness/resources.py`

- **Does**: clone (depth-1, unpinned on first acquisition — pinning is second-run-onward),
  `verify_commit` (`harness/repo.py:446`), dirty-tree detection, entrypoint discovery
  (`:272`) including hearing a repo advertise its own entrypoint, dependency inspection,
  capability assessment (`:795`), platform declaration (`:780`).
- **Identity is three separate questions**: experiment, metric, configuration
  (`harness/experiment_id.py:462` `identities_established`). A capable run of the wrong
  program produces a confident irrelevant number, which is more dangerous than a crash.
- **Resources**: insufficiency *including unknown demand* ⇒ `INCONCLUSIVE`. A paper's
  silence about its own cost is not evidence the cost is small.
- **Silent failure**: low. These stages are loud and produce named failure classes.
- **Class**: engineering, correctly scoped.

### AUTHORIZATION — `harness/backends.py:703` `authorize()`

The strongest part of the system, and the reviewer should not spend time here.

Requires **all** of: gate open, backend that `can_execute`, `repo_exec` provenance,
verified commit, experiment + metric + configuration identity, capability, sufficient
resources. Nothing else may permit execution. The controller explicitly cannot overrule
it (`harness/controller.py` docstring, lines 14-19: *"A controller that could overrule a
gate would make every gate advisory."*).

Adversarial coverage already exists — `tests/test_execution_backend.py`:
`test_a_closed_gate_refuses_before_anything_else_is_considered`,
`test_a_command_that_is_not_the_authors_code_may_not_run_as_theirs`,
`test_unproven_identity_blocks_a_fully_capable_run`,
`test_capability_that_was_never_assessed_is_not_capability`,
`test_a_handwritten_spec_cannot_run_a_repository_past_a_closed_gate`,
`test_a_backend_may_not_edit_the_command_it_was_handed`.

### EXECUTION + OBSERVATION — `harness/local_exec.py`

- Every started process recorded whole in `runs/<pid>/execution.jsonl`: command, cwd,
  commit, timestamps, exit code, full stdout/stderr, parsed metric. A reproduction verdict
  must be re-derivable from that file by hand.
- **The `process started → experiment actually ran` boundary the reviewer asked about
  already exists**: `StartupEvidence` (`:333`) with `_STARTUP_WINDOW_S = 30.0` and
  `_MIN_OUTPUT_LINES = 5`, plus `classify_setup_error` (`:369`) and
  `classify_infra_failure` (`:378`) over signature lists and signal return codes
  (SIGKILL/SIGSEGV/SIGABRT/…). Covered by
  `test_a_command_that_never_starts_is_distinguishable_from_one_that_failed` and
  `test_a_timeout_is_a_launch_not_a_failure_to_launch`.
- **Weakest link in that chain is `metric produced → metric corresponds to the requested
  target`**: the `SH_METRIC` contract is strict, but the fallback `json_metric` (`:220`)
  scans stdout for the last JSON object carrying any of
  `("value","metric","score","result","accuracy","acc",…)`. On a repo that prints several
  JSON summaries, the *last* one wins by position, not by identity. Identity gating
  upstream mitigates this; it does not make the parse target-aware.
- **Class**: mostly engineering; the metric-parse ambiguity is scientific.

### RECONCILIATION — `harness/local_exec.py:422` `reconcile()`

Arithmetic, not judgement, with four preconditions checked in this order:

1. **authorization** — if the run was refused, nothing ran, so the refusal is reported as
   a fact about *this harness's gates*, never as a finding about the paper.
2. **provenance ceiling** — only `driver` or `repo_exec` may reconcile, in *either*
   direction. A synthesized probe can neither convict nor acquit. Letting it acquit would
   be worse than letting it convict: a toy landing near the printed number would launder a
   claim nobody checked.
3. **identity** — applied to `driver` and `repo_exec` alike, not gated on `spec.command`.
4. **arithmetic** — `delta_error <= 2σ ⇒ RESOLVED_VERIFIED`. Three situations refuse a
   verdict: nothing parsed; zero noise band (`<= 2σ` degenerates into demanding
   exactness); a ~100× / ~0.01× ratio, which is a fraction-vs-percentage units mismatch in
   *this harness*, not a doctored claim in the paper.

Printed precision is respected: `printed_precision_half_width` (`:197`) reads the digit
count with `Decimal`, so a paper printing "59.3" is not judged against precision it never
claimed.

**Class**: engineering, and it is the best-specified module in the repository.

### DECISION — `harness/stages/report.py`

Fully mechanical, exactly as the reviewer demands in item 4:

```python
MATERIAL_SEVERITY = ()                                             # model findings cannot reject
ADMISSIBLE_REPRODUCTION_PROVENANCE = ("driver", "repo_exec")      # :145
```

`claim_status` (`:294`) → {VERIFIED_FAILURE, VERIFIED_SUPPORT, NOT_VERIFIED}.
`overall_verdict` (`:337`) → RED **iff** `claim_status` is VERIFIED_FAILURE, established
by deterministic arithmetic or admissible execution. Model severity has no rejection
authority. There is no count, accumulation, or second mechanism. The old
`RED_MAJOR_ONE_LENS=3` / `RED_MAJOR_TOTAL=10`
thresholds were removed because they made the decision a property of how many things a
panel chose to write down.

The provenance ceiling is **held twice** — at the reconciler and again at the verdict gate
— and a FAILED_REPRODUCTION arriving with inadmissible provenance is reported as a
*harness defect* instead of a conviction, explicitly not alongside it. The blame sentence
is derived from provenance rather than asserted, because a `driver` script the operator
wrote is not the authors' code and saying otherwise is "the one accusation this system
must never make by accident."

`unearned_support_language` (`:101`) prevents the report using the words "verified /
supported / confirmed / validated / corroborated" when `claim_status` does not earn them.

**The one model-authored output** is `## Whole-paper assessment` (`harness/verdict_driver.py`),
gated off by default, **printed and counted by nothing**. Its only consequence is a
CONTESTED flag and `run.py` exit 3.

**Class**: this is done. Item 4 is closed.

### ACCOUNTING — `harness/corpus.py`, `harness/selfaudit.py`, `harness/failures.py`

- `corpus.account` (`:45`) is built from the **request list**, not from the cases that
  happen to exist, and asserts a conservation law: every requested paper in exactly one
  terminal state. `summarize` in the controller is explicitly documented as *not*
  authoritative because it keys on `paper_id or source` and cannot see a request that lost
  its case or two requests that slugified to the same id.
- `selfaudit.py` — 12 machine-checked items over the finished report, verdict-inert.
- `failures.classify` (`:99`) — a retry budget is spent only where spending it could
  change the answer. A rate limit, an outage, a missing CLI, a revoked credential, an
  unknown flag and an unreadable PDF are **not** transient and consume no attempt.

---

## 2. The three structural gaps

Everything in the reviewer's items 1–4 reduces to these.

### 2.1 There is no target set — `harness/stages/probe.py:79-81`

`ProbeResult.reconciliation` is a single optional object. `CaseState` has one
`reproduction_class` string. There is no per-target state, no per-target disposition, no
coverage denominator. The reviewer's desired output shape —

```
Target A → BLOCKED: specification
Target B → EXECUTED → FAILED
Target C → EXECUTED → REPRODUCED
Target D → BLOCKED: environment
```

— is not expressible in the current artifact types. This is a data-model change
(`ProbeSpec` → `list[ProbeSpec]`, `Reconciliation` → `list[Reconciliation]`, plus a
target-disposition enum), not a prompt change.

**The campaign's target sets exist — outside the pipeline.** `manuscript/target_sets.json`
(42 KB) records, per paper, every plausible executable target considered, ranked, with its
final disposition and shared-blocker relationships. **No harness module reads or writes
it.** Its only consumers are `manuscript/check_claims.py`, `manuscript/tables/make_tables.py`
and `manuscript/figures/make_figures.py`. So "enumerate every plausible target" is, as the
reviewer suspected, a methodology claim backed by hand-curated JSON, executed by an
operator. Their item 2 is confirmed, and the file is the specification of what the missing
stage should produce.

### 2.2 A claim must be a table cell to be reconcilable — one regex, three places

`^T\d+:r\d+:c\d+$` at `harness/stages/probe.py:40`, `harness/stages/report.py:157`,
`harness/stages/audit.py:38`.

Consequences, in order of severity:

1. A prose-stated result can never be an autonomous reconciliation target.
2. A paper with unextractable tables silently drops to zero eligible targets.
3. `evidence_class != "cell_verified"` caps confidence, and confidence caps severity — so a
   prose-stated *finding* is also structurally capped below FATAL. Both halves of the RED
   path are gated on table extraction.

**The operator escape hatch is real and is exactly the autonomous-verdict gap.** A
hand-written `runs/<pid>/spec.json` may set `claimed_cell_value` directly, is promoted from
`template` to `driver` provenance when it carries a script (`harness/stages/probe.py:100-103`),
and `driver` is admissible in both directions. So the *machinery* to reconcile a
prose-stated claim exists and is sound; only the *autonomous path to it* is missing. That
is precisely why the RED the reviewer refers to was operator-driven rather than
pipeline-emitted. Nothing needs to be weakened to close it — a prose claim needs an
addressable identity of its own (a `claim_ref` alongside `table_ref`, resolvable to a
verbatim span with a parsed magnitude) so `build_spec` can ground a delta in it the same
way `grounded_claimed_delta` grounds one in a cell.

### 2.3 `verifiable_by_experiment` is an unchecked model boolean

`harness/prompts/audit.py:356` → `harness/stages/audit.py:546` → `harness/stages/probe.py:80`.

Every enum from a lens is clamped to a closed vocabulary; every quote is re-verified; every
derived class is harness-written. This boolean is none of those. It is the sole gate on the
execution half of the system and nothing corroborates it. A target-discovery stage that
derives candidacy from the paper's own structure — a printed number, an addressable
identity, a repo that implements the method — would make it advisory instead of
authoritative.

---

## 3. The reviewer's ten priorities, adjudicated against the code

| # | their claim | verdict | where |
|---|---|---|---|
| 1 | autonomous verdict gap — prose claim blocked the reconciler | **confirmed, root-caused** | §2.2; `probe.py:40,55,134` |
| 2 | target completeness is a methodology claim | **confirmed** | §2.1; `target_sets.json` read by no harness module |
| 3 | printed result should not need to be a table cell | **confirmed, one regex, three files** | §2.2 |
| 4 | verdict must be mechanical, not LLM/operator | **already closed** | `report.py:278,334`; `verdict_driver` is printed-only |
| 5 | reconstruction must not turn "mine differs" into "paper is wrong" | **already closed** | provenance ceiling held twice, `local_exec.py:482`, `report.py:352` |
| 6 | test the started→ran→metric→target→reconciled boundary | **mostly closed; one real gap** | `StartupEvidence:333` closed; `json_metric:220` last-JSON-wins is target-blind |
| 7 | specification / artifact / environment / disagreement must stay distinct | **already closed** | `failure_class` taxonomy, `classify_setup_error`, `classify_infra_failure`, invariants 6–7 |
| 8 | any place Claude says "good enough" is an integrity bug | **one remains, by design** | ungraded `severity` fallback, `report.py:218` — see AUDIT/GRADE above |
| 9 | multi-paper isolation | **closed** | `drive_all` round-robin, per-pid paths, `corpus.account` conservation law |
| 10 | adversarial testing beyond unit tests | **substantially exists** | 229 of 666 test functions are negative-path by name; see below |

On item 10 specifically — the suite is stronger than the reviewer assumes. 229 of the 666
test functions are negative-path by name (`…_cannot_…`, `…_may_not_…`, `…_is_not_…`,
`…_refuses_…`, `…_never_…`), which is the shape of a suite written to attack the system
rather than to demonstrate it:

- convict an innocent paper: `test_a_synthesized_probe_may_not_convict_a_printed_cell`,
  `test_a_refusal_cannot_drive_the_paper_red`, `test_a_blocked_run_is_not_reported_as_a_failed_one`
- acquit a broken one: `test_a_synthesized_probe_may_not_acquit_a_printed_cell_either`
- partial run read as success: `test_a_command_that_never_starts_is_distinguishable_from_one_that_failed`,
  `test_a_timeout_is_a_launch_not_a_failure_to_launch`
- wrong commit / dirty tree: `tests/test_commit_pinning.py`
- wrong metric: `test_flops_output_is_refused_against_an_accuracy_cell`,
  `test_memory_in_mb_is_refused_against_a_ratio_to_baseline_cell`,
  `test_an_absolute_column_is_not_mistaken_for_a_ratio`
- LLM self-certification: `test_a_lens_cannot_certify_its_own_reasoning`,
  `test_a_lens_cannot_upgrade_prose_evidence_to_a_cell_citation`,
  `test_a_lens_cannot_write_its_own_verification_state_or_calc_class`
- injection: `test_a_claim_from_the_paper_is_never_interpolated_as_code`

What is **not** adversarially tested is the part that does not exist: there are no tests for
multi-target dispositions, target-set coverage, or prose-claim reconciliation, because
there is no such code.

---

## 4. Minimal change set to reach the desired output shape

Ordered by dependency, not by appeal. Each is a data-model or stage change; none requires
weakening an invariant, which is the constraint that matters (`CLAUDE.md` §Immutable
invariants).

1. **Addressable prose claims.** Extend the ref vocabulary with a claim address that
   resolves to a verbatim span carrying a parsed magnitude. Feeds `_origin_from_ref`,
   `grounded_claimed_delta` and `spec.table_ref` alike. Unblocks §2.2 without touching the
   provenance ceiling. *This is the single highest-value change and it is the reviewer's
   item 1 and 3 simultaneously.*
2. **A target-discovery stage** between collect and probe, deriving candidates from the
   paper's structure (printed results with addressable identity) rather than from
   `verifiable_by_experiment`. Emits a target set with a coverage denominator; writes the
   same shape as `manuscript/target_sets.json`, which already specifies the format.
3. **Plural artifacts**: `ProbeResult.reconciliations: list[...]`, per-target disposition
   enum, per-target `failure_class`. `overall_verdict` becomes a fold over the set — RED
   iff *any* admissible reconciliation is FAILED_REPRODUCTION, which preserves invariant 8
   exactly.
4. **Feasibility-ranked prioritization**, distinct from `report.finding_key`. The display
   sort should stay a display sort.
5. **Target-aware metric parsing** to close the `json_metric` gap in item 6.
6. **Extraction-coverage instrumentation** so "no eligible targets" is reported as a
   measured fact about extraction rather than as silence.

Items 4–8 of the reviewer's own list need no work. Item 8's residual — the ungraded
`severity` fallback — is a deliberate settlement (turning grading off must reproduce the
ungraded verdict exactly) and should be argued with, not silently changed.

---

## 5. Reading order for the recipient

1. `CLAUDE.md` — the guarantees the system claims, and the 14 immutable invariants. Read
   the invariants first; they are the specification everything else answers to.
2. `harness/stages/report.py:71-160, 278-400` — the decision, which is a table.
3. `harness/local_exec.py:422-560` — reconciliation and its four preconditions.
4. `harness/backends.py:703` — `authorize()`, the seven-way conjunction.
5. `harness/grading.py:277` — `derive`, and why its *signature* is the invariant.
6. `harness/stages/probe.py:40-140` — the three gaps, all visible in one screen.
7. `manuscript/target_sets.json` — what the missing stage is supposed to produce.
8. `tests/test_reasoning_architecture.py` — the tests that assert the architecture rather
   than the behaviour.

`docs/HARNESS_ARCHITECTURE.md` and `docs/EXECUTION_REQUIREMENTS.md` are the existing
long-form docs; §6 of the former records why all pilot papers refuse execution.
