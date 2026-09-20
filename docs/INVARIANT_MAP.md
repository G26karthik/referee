# Invariant map — v4 redesign

Produced before any v4 module was written, per the redesign plan's governing constraint:
**the report may differ from the reference implementation; the trust boundary may not get
weaker.** This document is the artifact that constraint requires: every one of CLAUDE.md's
37 numbered invariants, where it lives today, where it lands in the 16-module v4
architecture, and the specific way a well-intentioned consolidation could lose it while
still compiling and still passing every test that does not assert the invariant itself.

Produced by an exhaustive audit against the live code (not the docs) and 1,737 real
artifacts under `projects/`. Two corrections to the plan surfaced during that audit and are
folded in below rather than in a separate errata:

- **`NOVELTY_ESTABLISHING_AUTHORITIES` and `CAUSAL_ATTRIBUTION_AUTHORITIES` no longer
  exist.** They went with invariants 36/37 (literature search, focused validation), deleted
  2026-09-20. Do not resurrect them, and do not resurrect `ENDPOINTS_VERIFIED_SEMANTIC_LINK`
  (invariant 31, also deleted). The surviving no-spelling constructs are
  `MATERIAL_SEVERITY = ()`, `material_failures() -> return []`, and `ARTIFACT_AUTHORITY`'s
  absent level-3 member.
- **`harness/isolation.py` is a real execution-safety boundary with no invariant number.**
  An invariant-driven port will not look for it unless it is named explicitly. See the
  dedicated section at the end.

`E` = enforced at runtime. `D` = documented only. ⚠ = the mapping is not obvious, or two
invariants that currently share one function land in different v4 modules.

---

## Part 1 — All 37 invariants

| # | live? | current enforcement | v4 module | how consolidation silently breaks it |
|---|---|---|---|---|
| 1 | yes | E `claims.py:457 mint` (refuses a quote occurring twice) + `claims.py:371 resolve` (re-reads span off doc → `span_mismatch`) + `stages/audit.py:896 verify_evidence` + drop-and-count at `:1183-1184` | `locate.py` + `audit.py` ⚠ | Splits across two modules. If `audit.py`'s compose returns findings without the `dropped` count, `report.py`'s "## Dropped findings" silently reports 0 while `guarantees._check_pointers` still passes (it only checks survivors). |
| 2 | yes | E `audit_driver.py:190-219 HARNESS_OWNED_*_KEYS` + `:222 strip_harness_keys` (walks nested `additional_evidence`) + overwrite at `stages/audit.py:1259-1260` | `agent.py` + `audit.py` ⚠ | The strip list is DERIVED: `_FINDING_ALLOWED = frozenset(Finding.model_fields) - frozenset(HARNESS_OWNED_FINDING_KEYS)`. A schema rename or a new field is trusted from the model by default unless explicitly added to the strip tuple. `evidence_origin` is deliberately absent from the allowed set — deleting that omission deletes the only way `origin_consistency=="corrected"` can fire. |
| 3 | yes | E `provenance.py:43,56,67` applied SIX times: `local_exec.py:684` (reconcile), `taxonomy.py:294-296`, `artifacts.py:3409`, `stages/report.py:358` (acquitting) / `:418` (convicting) / `:594`, `outcome.py:254`, `backends.py:1345` | `execute.py` + `decide.py` + `report.py` ⚠⚠ | Six applications across three new modules. `provenance.admits` is EXACT — no strip, no casefold. A "normalisation helper" in the merged module that adds `.strip().upper()` turns a malformed token into `AUTHOR_REPOSITORY`. |
| 4 | yes | E `backends.py:1186 authorize` — 11 ordered rungs across 3 branches (not 8 — see "Two ladders" below); `commit` defaults `None` and refuses | `execute.py` ⚠⚠ | `backends.py:1278-1285` is a free pass when `not spec.command`. The separate `reimpl_exec` branch exists ONLY to stop a model-written reconstruction falling into that free pass and running unconfined. Unifying "the two branches are nearly identical" restores exactly that hole. |
| 5 | yes | E `backends.py:1352-1358` (commit must be `established` or refuse) + `artifacts.py:1281 ArtifactSnapshot.dirty` defaults **True** | `execute.py` | A refactor that treats a `git status` raise as "unknown" rather than "dirty" flips an uninspectable tree from refused to audited. |
| 6 | yes | E `resources.py:415 assess_resources` — `state="unknown"` is NOT `established`, so `backends.py:1376` refuses | `execute.py` | A merged `if demand and demand > capacity: refuse` lets `None` (unknown) through via the falsy-check idiom. |
| 7 | yes | E `local_exec.py:432+ _SETUP_FAILURE_SIGNATURES` + `StartupEvidence` ("demonstrably started") | `execute.py` | Dropping `StartupEvidence` as "diagnostics" makes every non-zero exit convictable. |
| 8 | yes | E `stages/report.py:79 MATERIAL_SEVERITY=()` + `:508 material_target_failure` (the ONE call both `claim_status` and `overall_verdict` make) + `materiality.py:339` + `artifacts.py:3386 establishes_failure` | `decide.py` + `report.py` ⚠⚠ | Two tiers in two modules. `overall_verdict` and `claim_status` deliberately share ONE call so they cannot disagree — splitting materiality from verdict re-creates the two-implementation shape already removed once. `counted()` falling back to bare `severity` when `counted_severity==""` is the "grading off reproduces the ungraded decision exactly" property. |
| 9 | yes | D — enforced by ABSENCE of a function; checked by `guarantees.py:686 _check_no_downscale` | `execute.py` | A new `execute.py` that adds a `fit_to_budget()` breaks it and nothing fails unless the check is ported. |
| 10 | yes | E signature `grading.py:277 derive` — 15 keyword-only params, all vocabulary strings/bools | `audit.py` | See Part 2. |
| 11 | yes | E `grading.py:46 RANK` + every cap is a `min` over RANK; property asserted by a TEST sweep, not production code | `audit.py` | `RANK[counted] <= RANK[lens]` is not asserted in production. If the sweep test is not ported, a `max()` in a merged cap loop is invisible. |
| 12 | yes | E `grading.py:197 EVIDENCE_CONFIDENCE_CEILING` (evidence→confidence) THEN `:166 _CONFIDENCE_CAP` (confidence→severity) — two tables in sequence, never composed | `audit.py` | Composing them into one `evidence_class→severity` dict reintroduces `caption→NOTE` and gets the same corpus numbers, so no test catches it unless the test asserts the two tables exist separately. |
| 13 | yes | E `corpus.py account` — bare `assert total == requested` (stripped under `-O`) + `evaluation.py`'s independent conservation check (currently 1 known violation, see below) | `pipeline.py` | — |
| 14 | yes | E `failures.py:35 FAILURE_KINDS` + `:52 RETRY_POLICIES` + regex table → `consumes_attempt` | `agent.py`(‡) | The pattern table decides `never` for rate-limit/outage/missing-CLI/revoked-credential/unknown-flag/unreadable-PDF. Merging retry into the phase machine makes the phase machine's default the answer, and that default is usually "retry". |
| 15 | yes | E split — `harness_addressable` in `discovery.py` (resolved ref + parsed quantity + route≠NONE) and `planner.py:139 classify`'s signature | `discover.py` + `decide.py` ⚠⚠ | Split BY DESIGN in v4 too. `Finding.verifiable_by_experiment` is an unchecked model bool kept only as metadata; if `decide.py` gains a parameter for it, `classify` can be swayed by a lens's own opinion of itself. |
| 16 | yes | E `artifacts.py:3386 establishes_failure` — a DERIVED PROPERTY, never a field | `schema.py` + `decide.py` ⚠ | If "one evidence object" stores `establishes_failure` as a settable field (natural when flattening), a blocked target can be written failed. |
| 17 | yes | E `stages/report.py:563 _ATTEMPTED_AND_UNSETTLED=("INCONCLUSIVE",)` PLUS the provenance conjunct 30 lines away at `:594` | `report.py` | Got wrong twice on this corpus already. Keeping the tuple and dropping the conjunct reproduces failure #2 — all papers YELLOW — and only shows up with gates OPEN. |
| 18 | yes | E `local_exec.py:370 parse_metric` — tier order + refusal on intra-tier disagreement | `execute.py` | A merged parser returning the first tier hit (dropping the `distinct=sorted(set(values))` check) restores last-JSON-wins with a tier label on it. |
| 19 | yes | E **nine separate caps**: `_MAX_RED/_YELLOW/_HELD/_OPEN=5,4,3,3`, `_MAX_READING=4`, `_MAX_PER_CATEGORY=3`, `_MAX_FINDINGS_SHOWN=8`, `_MAX_TRIGGERED=3`, `MAX_TABLE_ROWS=14`, `MAX_THREAT_BULLETS=6`, `MAX_CODE_ROWS=12`, `_MAX_REVIEW_CHARS=12000` | `report.py` | "ONE assembly path" is exactly the refactor that collapses nine caps into one `MAX_ITEMS` and loses the per-section bound; the 67-finding report returns. |
| 20 | yes | E `taxonomy.py:248 classify` / `:283 evidence_state` / `:300 resolution_state` — `resolution_status` DERIVED, never stored; ceiling applied a second time at `:294-296` | `decide.py` ⚠ | `scientific_class` is deliberately NOT a param of `grading.derive` — a fact about two signatures in two different v4 modules with no shared code enforcing it. |
| 21 | yes | E `artifacts.py:3365 TargetOutcome.launched` copied from the runner's own process count; `resync_cached_outcomes` exists because this broke once (16 real executions read as 0) | `execute.py` + `report.py` ⚠ | If `execute.py` returns a dict and `report.py` infers `launched = 1 if disposition != NOT_ATTEMPTED`, every number stays plausible while being wrong. |
| 22 | yes | E `taxonomy.py:206` `SUPERSEDED_BY_ESTABLISHED_FAILURE → NOT_INVESTIGATED` — necessity axis untouched | `decide.py` | Merging necessity and evidence into one enum makes NO_EXPERIMENT_NEEDED look like a resolution. |
| 23 | yes | E `planner.py:63 _RESOLVING=("ARITHMETIC_RECHECK",)` — `PAPER_INTERNAL_CHECK` deliberately NOT in it, sits at `:68 _CITATION_ONLY` | `decide.py` + `report.py` ⚠ | Two adjacent one-element tuples that look like an oversight. Merging suppresses every executable route behind a citation re-check — measured at 100% of paper-only resolutions before the split existed. |
| 24 | yes | E signature `outcome.py:182 finding_state(*, claim_status, kept_findings)` — TWO params | `report.py` | See Part 2. |
| 25 | yes | E `outcome.py:166 clip()` cuts at `head.rfind(" ")`, not a character count | `report.py` | A generic `truncate()` restores the exact defect: disclaimer lost off the end of a line opening with a large delta. |
| 26 | yes | E `docintegrity.py:513 DocumentObservation` — no severity/confidence/class/verdict FIELD, `about` required no default | `report.py` ⚠ | A "one evidence object" that gives every observation an optional `severity=""` makes this expressible again. |
| 27 | yes | E signatures `coverage.py:209 surface(doc)` and `:333 measure(surf, *, addressed: tuple[str,...], examined: tuple[str,...])` | `report.py` + `paper.py` ⚠ | See Part 2. Also: `coverage.py:74 budget_chars()` is the SOLE reader of `SH_AUDIT_BUDGET_CHARS`; `stages/audit.py` delegates to it at call time so the printed budget and the built prompts cannot differ — that delegation link must survive the module split. |
| 28 | yes | E `guarantees.py:391 assess` + 13 `_check_*` fns each naming a field + `:355 _MATERIAL_SEVERITY` drift-checked against `stages/report.MATERIAL_SEVERITY` | `report.py` | A merged `guarantees = {...literal dict...}` satisfies the shape and asserts nothing; each `_check_*` must keep naming its field. |
| 29 | yes | E `stages/audit.py:1175-1178` (every side of a cross-section concern verified independently) + `:1035 weakest_evidence_class` (a `min`, never passed to `grading.evidence_support` as a corroborating source) | `audit.py` | Adjacent in one merged `audit.py`, "pass extras to `evidence_support`" is the obvious improvement that lets one reader lift its own ceiling by citing twice. |
| 30 | yes | E `stages/audit.py:1098 deduplicate` keyed on `_address_identity` + `_concern_identity` (5 closed-vocab fields), errs toward UNDER-merging | `audit.py` | Measured 0/227 merges on the corpus — no corpus test can catch a bad key. Keying on `scientific_class` alone or on text similarity deletes a real concern silently. |
| 31 | **DELETED** | none — `claimlink*`/`claimgraph`/`link_authority`: 0 matches repo-wide | — | Do not resurrect `ENDPOINTS_VERIFIED_SEMANTIC_LINK` as a schema value. |
| 32 | yes | E `claims.py:98 soft_hyphen_projection` (drops only hyphens the ORIGINAL shows followed by whitespace) + `claims.py:81 flatten` UNCHANGED | `locate.py` | `flatten` must not change — every `P<i>:<a>-<b>` in the repo is a pair of offsets into its output. "Also stripping soft hyphens in flatten" (the natural unification) silently moves every stored reference. |
| 33 | yes | E `artifacts.py:1149 ARTIFACT_AUTHORITY` — no level-3 member; `:1212 SETTLEABLE_BY_ARTIFACT_FACT` excludes `IMPLEMENTATION_CORRESPONDENCE` by comprehension | `routes.py` ⚠ | `SETTLEABLE_BY_ARTIFACT_FACT`'s only consumer is `artifact_evidence.py:692 discharge` — delete that and the tuple lints away as dead, silently removing the scope restriction it encodes. |
| 34 | yes | E `artifact_evidence.py:97 RULE_AUTHORITY.get(rule_id, "D")` — unclassified defaults INVISIBLE | `routes.py` + `agent.py` ⚠ | A merged rule engine whose default is "surface it" makes the audit license a rule's existence rather than its measured authority. `cripple-branch-budget`'s real-world near-miss (a misclassified rule id that fell through to fail-closed D and was never caught) is the precedent. |
| 35 | yes | E `preflight.py:116 check` + gate at `controller.py:902` returning `"results": []` and starting nothing — this was a documented HOLE for two revisions before the gate existed | `pipeline.py` | `corpus.account`'s conservation law CANNOT catch a re-opened hole here: the duplicate never becomes a second case to conserve. |
| 36 | **DELETED** | none — `literature*`: 0 matches. `NOVELTY_ESTABLISHING_AUTHORITIES` gone. | — | 8 literature-vocabulary values (`SEARCH_COMPLETED_NO_MATCH_FOUND`×6, `SEARCH_INCONCLUSIVE`×2) are still on disk in `projects/*/literature/*.driver.json` — see schema pre-work. |
| 37 | **DELETED** | none — `validation*`/`between_arms`: 0 matches. `CAUSAL_ATTRIBUTION_AUTHORITIES`, `VALIDATION_INGREDIENTS`, `NEVER_ASSUMED` gone. | — | 66 occurrences of `FOCUSED_VALIDATION_EXPERIMENT` are still on disk in two papers' `discovery/targets.json` — the single clearest thing enforced Literals would reject today. See schema pre-work. |

---

## Part 2 — Enforced by a SIGNATURE, not a check (exact current text)

A rewrite that widens any of these breaks the invariant and still compiles, and still
passes every test that does not assert the signature itself.

**`grading.derive`** (`harness/grading.py:277-284`) — invariants 10, 12, 20
```python
def derive(*, lens_severity: str, verification_state: str, calc_class: str,
          graded: bool, grade_verdict: str = "", grade_severity: str = "",
          confidence: str = "", falsification_survived: bool = True,
          has_impact_statement: bool = True, has_steelman: bool = True,
          evidence_class: str = "unverified",
          grader_evidence_class: str = "unverified",
          lens_confidence: str = "", candidate_class: str = "",
          baseline_class: str = "", prior_art_basis: str = "") -> tuple[str, str, str, str]:
```
15 keyword-only params, every one a vocabulary string or a bool. No count, no float, no
metric name, no paper id — and NO `scientific_class`.

**`planner.classify`** (`harness/planner.py:139-143`) — invariants 15, 22, 23
```python
def classify(*, centrality: str, addressable: bool, route: str,
             artifact_available: bool, specification_complete: bool,
             environment_state: str, addressing_blocker: str = "NONE",
             investigation_open: bool = True) -> tuple[str, str, dict, str]:
```
`investigation_open` is one bool on purpose: the ASSESS phase does the reasoning and hands
down a single bit, so no severity, no finding and no count enters here.

**`taxonomy.classify`** (`harness/taxonomy.py:248-249`) — invariant 20
```python
def classify(*, lens: str = "", discrepancy_type: str = "", baseline_class: str = "",
             candidate_class: str = "", is_artifact_finding: bool = False) -> str:
```

**`outcome.finding_state`** (`harness/outcome.py:182`) — invariant 24
```python
def finding_state(*, claim_status: str = "", kept_findings: int = 0) -> str:
```
Two parameters. No disposition, provenance, reconciliation, launch count, coverage number
or document observation may reach it. Sibling rows that must stay disjoint:
`question_state(*, raised, settled)`, `execution_state(outcomes, plans)`,
`scope_state(*, unchecked_central, pursued)`.

**`coverage.surface`** (`harness/coverage.py:209`) — invariant 27
```python
def surface(doc: PaperDoc) -> ReviewSurface:
```
One parameter. No `TargetSet`, no `DiscoveredObject`, no `Finding`, no `PlanDecision` and
no count derived from any of them may reach this function.

**`coverage.measure`** (`harness/coverage.py:333-334`) — invariant 27
```python
def measure(surf: ReviewSurface, *, addressed: tuple[str, ...] = (),
            examined: tuple[str, ...] = (), ...)
```
Numerators are plain address STRINGS, not objects, so a numerator cannot redefine the
denominator.

**`provenance.admits`** (`harness/provenance.py:56`) — invariant 3
```python
def admits(provenance: str = "") -> bool:
    return (provenance or "") in ADMISSIBLE_REPRODUCTION_PROVENANCE
```
No direction parameter — the ceiling is symmetric by construction, asserted via
`inspect.signature` in its own self-check.

**`backends.authorize`** (`harness/backends.py:1187`) — invariant 4
```python
def authorize(cfg, spec, backend: ExecutionBackend | None,
              commit: CommitVerification | None = None) -> ExecAuthorization:
```
`commit` defaults to `None`, which refuses — a caller that forgets to pass it fails closed
rather than inheriting a verification made minutes earlier.

**`audit_driver`'s AST purity asserts** (`audit_driver.py:974-992`) — invariant 2
The delegation layer may not import `grading|taxonomy|stages|planner|priority|report`, may
not assign `severity|counted_severity|verdict|triage|claim_status|finding_class|binding_cap`.
`delegation.py` carries a sibling test restricting its own imports to
`{shutil, inspect, __future__}`.

**`DocumentObservation`** (`harness/docintegrity.py:513`) — invariant 26
Has no severity/confidence/class/verdict field, and no decision function has a parameter
that could receive one. `about` is required with no default.

---

## Part 3 — Applied TWICE at different layers on purpose

A consolidation that merges layers silently removes the second application.

| rule | layer 1 | layer 2 |
|---|---|---|
| Provenance ceiling, **convicting** | `local_exec.py:684` | `taxonomy.py:294-296`, `artifacts.py:3409`, `stages/report.py:418`, `outcome.py:254`, `stages/report.py:594` |
| Provenance ceiling, **acquitting** | `local_exec.py:684` | `stages/report.py:349-358` — added later; was single-enforced while convicting was double |
| `ReimplementationConformance.established` | `local_exec.py:708-721` (reconcile) | `backends.py:1262-1270` (authorize) |
| `identities_established` | `backends.py:1360-1364` (before execution) | `local_exec.py:778-789` (before reconciliation) |
| Isolation floor | `backends.py:1339-1343` (repo_exec branch) | `backends.py:1257-1261` (reimpl_exec branch — deliberately the IDENTICAL floor, not a weaker one) |
| `MATERIAL_SEVERITY = ()` | `stages/report.py:79` | `guarantees.py:355` copy + `:951-952` drift assert |
| Evidence verification | `stages/audit.py:1162` (compose) | `grading.py:123 recheck_calculation` (arithmetic) + `guarantees.py:479 _check_pointers` (report time) |
| Severity caps | `stages/audit.py:1213` (`graded=False` baseline) | `stages/grade.attach` re-derives strictly downward |
| Preflight duplicate check | `run.py cmd_preflight` (operator command) | `controller.py:902` (gate inside `review_papers`) — invariant 35 requires **both** |
| Conservation law | `corpus.py assert total == requested` | `evaluation.py`'s independent "must be 0" check |
| docintegrity gate | `docintegrity.py:389 determinations → _determine` | `docintegrity.py:526 observe` re-applies `_determine` over the same readings |
| Cross-section side verification | `stages/audit.py:1175-1178` (each side independently) | `:1191-1192 weakest_evidence_class` re-derives the ceiling from all sides |
| "GREEN may not borrow evidence's words" | *none at run time* — `stages/report.py:109 unearned_support_language` has NO caller in the renderer | `tests/test_guarantees.py` + `tests/test_reimplementation_path.py` run it over rendered output — **the "second layer" is the only layer, and it is a test** |

⚠ The opposite case, which must ALSO survive: `stages/report.py:508 material_target_failure`
was deliberately COLLAPSED from two calls into one so `claim_status` and `overall_verdict`
cannot disagree. Re-splitting it during a `decide.py`/`report.py` separation restores the
two-verdict shape the reference already fixed once.

---

## Part 4 — Fail-closed by construction (must survive verbatim)

**Membership tests that can only narrow**
- `provenance.py:64` — exact, no normalisation (self-check pins `" repo_exec"`,
  `"repo_exec "`, `"DRIVER"`, `"Repo_Exec"`, `None` as refusals)
- `provenance.py:73 PROVENANCE_LABEL.get(p, "SYNTHESIZED_DIAGNOSTIC")` — unknown reads as
  diagnostic, never as author code
- `isolation.py:80` — `in ISOLATION_SUFFICIENT_FOR_REPO_EXEC`
- `artifact_evidence.py:692` — `f.settles in SETTLEABLE_BY_ARTIFACT_FACT`
- `taxonomy.py:245 claim_was_checked` — `in _CHECKED_EVIDENCE`
- `materiality.py:314 is_material`

**Values with no spelling**
- `ARTIFACT_AUTHORITY` (`artifacts.py:1149`) — no "the reported result is false" member
- `MATERIAL_SEVERITY = ()` (`stages/report.py:79`)
- `material_failures() -> return []` (`stages/report.py:259`)
- `grading.derive`'s signature — no `scientific_class`, no count, no metric name
- `outcome.finding_state`'s signature — no execution parameter
- `coverage.surface`'s signature — no review-derived parameter
- `DocumentObservation` — no severity field, and no decision function has a slot for one

**Defaults that mean "we did not establish this" (the load-bearing ones)**

| field | default |
|---|---|
| `ArtifactSnapshot.dirty` | **`True`** |
| `Finding.evidence_class` / `.grader_evidence_class` | `"unverified"` |
| `Finding.counted_severity` | `""` (= ungraded; the grading-off fallback) |
| identity states (`ExperimentIdentity` etc.) | `"unmapped"` |
| `Reconciliation.status` | `"NOT_ATTEMPTED"` |
| `ProbeSpec.commit_state`, `CommitVerification.state`, `ResourceCapability.state` | `"unassessed"` |
| `RuntimeDemand.state` | `"unknown"` |
| `ArtifactFact.authority` | `"NONE"` |
| `TargetOutcome.disposition` / `.route` | `"NOT_ATTEMPTED"` / `"NONE"` |
| `EvalReport.disposition` / `CaseState.disposition` / `CorpusEntry.disposition` | `"NOT_REVIEWED"` |
| `DiscoveredObject.centrality` / `.materiality_basis` / `.status` / `.addressing_blocker` | `"UNASSESSED"` / `"NONE"` / `"PENDING"` / `"NONE"` |
| `ReviewQuestion.resolution_status` / `.evidence_state` | `"NOT_INVESTIGATED"` |
| `ExecCapability.reason_code` | `"not_attempted"` |

`local_exec.py:658-674` is the RECORDED case where this discipline was got wrong: three
identity states were assigned BELOW the ceiling's early return, so all 16 corpus
reconciliations reported the default `"unmapped"` while the specs held `no_candidate`,
`ambiguous` and `unmapped` — three different findings flattened into one word by the order
of two blocks. This is why `execute.py`'s reconcile-ladder design keeps that assignment as
an explicit RECORDER between the ceiling and the preconditions, not as a rung.

**`_determine`-shaped gates**
- `docintegrity.py:392 _determine` — an unrunnable check answers `NOT_INVESTIGATED`, which
  is the answer, not a silence
- `artifact_evidence.py:97 RULE_AUTHORITY.get(rule_id, "D")`
- `local_exec.py:391-394 parse_metric` with no key — refuses rather than guessing
- `backends.py:1187 commit=None` refuses
- `stages/report.py:530-534` — a reconciliation with no joinable target establishes no
  materiality
- `preflight.py:50 BLOCKING_STATES` + `controller.py:918` returns `"results": []`
- `stages/audit.py:1144-1147` — a lens file that is not a dict, or has no `findings` list,
  yields `valid=False`, never zero findings

---

## Unnumbered, and therefore at greatest risk

**`harness/isolation.py`** — `ISOLATION_LEVELS`, `ISOLATION_SUFFICIENT_FOR_REPO_EXEC =
("CONTAINER", "REMOTE_SESSION")`, `sufficient_for_repo_exec`, applied twice inside
`authorize` (`backends.py:1257` reimpl branch, `:1339` repo branch). No invariant number, so
an invariant-driven port will not look for it by name. Its own docstring records that
before it existed, `SH_ALLOW_REPO_EXEC=1` ran a paper's repository under the operator's
account with their filesystem, network and credentials in reach. `REMOTE_SESSION` is now
unreachable in practice (the sandbox backend that declared it was deleted 2026-09-20, and
the two remaining profiles that still declare it — Kaggle, Colab — both have
`can_execute=False`, so the isolation check is never reached for them). v4 must keep the
LEVEL name for the "the set and the floor would diverge if a level were added" property
even while only `CONTAINER` is reachable — see the redesign plan's "Trust boundaries" row 6.

**`state.add_cost` / `append_log`'s `cost_usd` threading** — no production caller today, and
the direct subject of four concurrency regression tests (`tests/test_state_locking.py`: a
lost-update race and a reentrant-self-deadlock proof). A dead-code sweep of `state.py`
deletes it and the proofs with it. Keep both.

---

## Schema pre-work this map requires (before `schema.py` is written)

From the governing-constraint audit, concrete and enumerated:

1. Declare the five previously-undeclared tokens in `EXEC_DECISIONS`/`FAILURE_CLASSES` —
   **done** (this step; see `tests/test_vocabulary_ratchet.py`, red-before/green-after
   confirmed).
2. Fix `Finding.severity`'s description to reference `SEVERITIES` (which includes `NOTE`,
   carried by 454 stored findings), not the incomplete `"FATAL | MAJOR | MINOR"` inline
   string. Reconcile against `Grade.severity`'s separate `GRADE_SEVERITIES` (which has
   `NONE`, not `NOTE`) as a discriminated type, not a union — unioning them puts `NONE`
   into a `grading.RANK` lookup with no such key.
3. Resolve the nine field-name collisions (`kind` carries 6 different vocabularies across
   different models, `state` carries 7, plus `disposition`, `status`, `verdict`,
   `resolution`, `identity_state`, `severity`, `scope`) into per-model discriminated types.
4. Name the 34+ prose-only vocabularies (`RepoAcquisition.status`, `ProbeResult.verdict`,
   `SelfAuditItem.state`, …) as real constants before generating a `Literal` from them —
   the prose is already incomplete relative to what the code emits (e.g. `not_started`).
5. Build the enumerated `LEGACY_VALUES` map for values from deleted routes still on disk:
   66× `FOCUSED_VALIDATION_EXPERIMENT` (two papers' `discovery/targets.json`, invariant 37),
   `SEARCH_COMPLETED_NO_MATCH_FOUND`/`SEARCH_INCONCLUSIVE` (literature route, invariant 36),
   plus `paper4-snri-nullresult`'s `verdict: "YELLOW"` in a `RED|GREEN` field (the known,
   worse-than-recorded conservation-law violation).
6. Replace `exhaustion.py:320`'s `"conformance_unproven" in (outcome.reason or "")`
   substring match with a typed field read — the type system was supposed to carry this,
   and prose-matching for it is exactly the kind of open vocabulary this map exists to close.

Enforcement is WRITE-SIDE only; reads tolerate legacy values through the map above, never
through a blanket `extra="allow"`.
