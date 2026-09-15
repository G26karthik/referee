# Final product gap audit

Written 2026-09-16, at the start of the v3 phase, **before any behaviour was changed**.
Branch: `final-product-completion-v3`, forked from `97419cf` on `master`.

Every statement here was established by reading production code or frozen run artifacts.
Where the manuscript, `CLAUDE.md` or an earlier document disagrees with the code, the code
wins and the disagreement is recorded. Five independent read-only audits were run in
parallel and **one of their conclusions was wrong; it is corrected in §2.4 rather than
repeated.**

Preserved and untouched by this phase: tag `evaluated-v2` (`1c5bbdcf`), tag
`release-2026-09-16`, tag `journal-v2-restructured` (`97419cf`), the run directory
`runs_final_codex_v2_2026-09-15`, the current journal manuscript, and every document under
`docs/`.

---

## 1. The model components: there are SIX, not four

The manuscript says *"Four model components exist and none of them can decide anything"*
and then describes four audit readers, a grader and a whole-paper reader. That is an
internal contradiction and the code settles it: **four is the number of audit lenses; six
is the number of configured model roles on the production review path.**

### 1.1 The four audit lenses

`LENSES` is defined once, at `harness/prompts/audit.py:412`, and
`harness/stages/audit.py:35` derives its list from it (`LENSES = tuple(P.LENSES)`), so the
two cannot drift. The four keys, verbatim:

| lens | declared model | defined at |
|---|---|---|
| `overclaim` | `opus` | `harness/prompts/audit.py:413-414` |
| `protocol` | `sonnet` | `harness/prompts/audit.py:445-446` |
| `confound` | `sonnet` | `harness/prompts/audit.py:466-467` |
| `contradiction` | `sonnet` | `harness/prompts/audit.py:495-496` |

The names in the handoff prompt (OVERCLAIM, PROTOCOL, CONFOUND, CONTRADICTION) are
confirmed correct against the code, which the prompt asked not to be assumed.

**The declared model is read and reaches the command line.**
`harness/audit_driver.py:349` looks the lens up in `P.LENSES`;
`harness/audit_driver.py:359` copies `spec["model"]` onto `Confinement.model`; and
`harness/audit_driver.py:290-291` appends `--model {self.model}` to the subprocess argv.
This holds only for the built-in CLI template: when an operator supplies `SH_AUDIT_CMD`,
`Confinement.enforced` is `False` and nothing is appended
(`harness/audit_driver.py:265-266`).

### 1.2 The grader

`harness/prompts/grade.py:40-47`: one role, `model: opus`, `tools: ()`, `bare: True`.
Invoked once per FATAL/MAJOR candidate, not once per lens.

Withheld by name at `harness/grade_driver.py:346-347`: `severity`,
`severity_rationale`, `lens`, `other_findings`, `prior_grades`, `verdict_thresholds`,
`derivation_table`. It gets `--allowedTools ""`, no `--add-dir`, and `--bare`
(`harness/grade_driver.py:99-113`); a test asserts `"--add-dir" not in _built`
(`harness/grade_driver.py:393`). This is the strictest confinement in the system.

It can only demote. `harness/grading.py:277-360` combines caps with `min(...)` over
`RANK`, and `harness/grading.py:299` asserts `RANK[counted_severity] <= RANK[lens_severity]`
over the whole reachable input space.

### 1.3 The whole-paper reader

`harness/prompts/verdict.py:24-30`: one role, `model: opus`, zero tools, `bare: True`.
Unlike the grader it is shown everything — every finding, every grade, the probe result
(`harness/prompts/verdict.py:4-8`). Its only consequence is a printed CONTESTED flag and
`run.py` exit 3 (`harness/stages/report.py:1215-1216`, `run.py:76-103`). Its self-check
AST-walks its own imports and asserts it never imports `grading`, `taxonomy`, `stages`,
`report` or `planner` (`harness/verdict_driver.py:400-418`), so it cannot reach a decision
path even accidentally.

### 1.4 The reconstruction generator and the reconstruction verifier

`harness/prompts/reimplement.py:15-20`: `model: sonnet`. The module makes **two** subprocess
calls with different attributed roles — a generator at
`harness/reimplement_driver.py:411-433` and an independent verifier at
`harness/reimplement_driver.py:434-441` — and `conformance()` requires
`generator != verifier` before `established` can be true
(`harness/reimplement_driver.py:286-337`).

### 1.5 The count

| # | role | model | gate |
|---|---|---|---|
| 1 | lens `overclaim` | opus | `SH_ALLOW_AUTO_AUDIT` |
| 2 | lens `protocol` | sonnet | `SH_ALLOW_AUTO_AUDIT` |
| 3 | lens `confound` | sonnet | `SH_ALLOW_AUTO_AUDIT` |
| 4 | lens `contradiction` | sonnet | `SH_ALLOW_AUTO_AUDIT` |
| 5 | grader | opus | `SH_ALLOW_GRADING` |
| 6 | whole-paper reader | opus | `SH_ALLOW_SUBSTANTIVE_VERDICT` |
| 7 | reconstruction generator | sonnet | `SH_ALLOW_REIMPLEMENTATION_DRIVER` |
| 8 | reconstruction verifier | sonnet | `SH_ALLOW_REIMPLEMENTATION_DRIVER` |

**Six roles are on the always-on review path (1-6). Two more (7-8) are on the conditional
reconstruction fallback and did run in the v2 evaluation** (§2.4). The correct sentence for
the manuscript is therefore neither "four model components" nor "six": it is *four audit
lenses, a grader and a whole-paper reader on every review, plus a generator and a separate
verifier whenever the reconstruction fallback is reached.*

**Decision authority, resolved.** No model writes the paper-level decision. But the claim
"none of them can decide anything" is too strong in one place: a lens's own asserted
`severity` flows through `grading.derive` into `counted_severity`, and
`harness/stages/report.py:270` + `:634-690` turns a non-empty concern set into **YELLOW**.
So an ungraded lens severity does move the triage colour, while RED remains unreachable
from any model output (invariant 8). The manuscript must say that precisely.

---

## 2. The evidence routes

`VERIFICATION_ROUTES` is declared at `harness/artifacts.py:2518-2527`.

| route | selectable | executor | can discharge | verdict |
|---|---|---|---|---|
| `PAPER_INTERNAL_CHECK` | yes | quote recheck, no execution | no, deliberately | IMPLEMENTED |
| `ARITHMETIC_RECHECK` | yes | real arithmetic over printed operands | yes | IMPLEMENTED |
| `ARTIFACT_INSPECTION` | fallback label only | none on the route | **no** | **DECLARED ONLY** |
| `AUTHOR_CODE_EXECUTION` | yes | full pipeline | yes | IMPLEMENTED |
| `INDEPENDENT_RECONSTRUCTION` | yes | full pipeline | yes | IMPLEMENTED |
| `FOCUSED_VALIDATION_EXPERIMENT` | yes, rarely offered | runs, forced `synthesized` | **never** | **CANNOT DISCHARGE** |
| `LITERATURE_SEARCH` | yes, `PRIOR_ART` only | none | never | **DECLARED ONLY** |
| `NONE` | sentinel | n/a | n/a | IMPLEMENTED as sentinel |

### 2.1 Artifact inspection is a third thing, neither preparatory nor a route

`harness/code_audit.py:753` (`audit_repo`) is **real** static analysis: nine AST-based
rules covering baseline-crippling, data leakage and metric deviation, plus runtime-demand
extraction. It runs unconditionally during probe (`harness/stages/probe.py:301`).

It is then walled off completely. Its output type `CodeAuditFinding`
(`harness/artifacts.py:1045`) is distinct from `Finding` (`harness/artifacts.py:491`) and
is read only by the report renderer's own `## Static code audit` section
(`harness/stages/report.py:1118-1141`, `:1387-1389`) and by `dossier.py:126`. There are
zero references to `code_audit` in `taxonomy.py`, `questions.py` or `stages/discover.py`.

Two consequences, both defects against the clarified product:

- `ARTIFACT_INSPECTION` is not a key of `_ACTION_FOR_ROUTE`
  (`harness/planner.py:71-75`), so `classify`'s `route_exists` gate
  (`harness/planner.py:161`) is always `False` for it and the plan always returns
  `NO_EXPERIMENT_NEEDED` (`harness/planner.py:205-208`).
- The evidence states `ARTIFACT_EVIDENCE` (`harness/taxonomy.py:84`) and
  `RESOLVED_FROM_ARTIFACT` (`harness/taxonomy.py:71`) are **silently unreachable**: no
  entry in `_EVIDENCE_FOR_DISPOSITION` (`harness/taxonomy.py:126-172`) produces them, for
  any of the 21 target dispositions. This differs from `NO_MATERIAL_ISSUE_FOUND`, which is
  documented and self-check-asserted as deliberately unreachable
  (`harness/taxonomy.py:59-65`). This one is an accident.

So the system already **has** a static artifact analyser whose findings a referee would
want, and throws them away before they can become evidence. Closing this is §7 of the
brief and is the cheapest real capability gain available.

### 2.2 Focused validation runs and can never conclude

`harness/stages/probe.py:398` (`synthesize_probe`) sets `spec.provenance = "synthesized"`
unconditionally at `harness/stages/probe.py:429`. That spec really executes — `authorize`
returns `allowed=True, decision="not_repo_execution"` at `harness/backends.py:1406-1413`
because it carries no `spec.command`. It is then refused at reconciliation by the
authority ceiling: `harness/local_exec.py:706-725` sets `INCONCLUSIVE` because
`provenance.admits` allows only `("driver", "repo_exec", "reimpl_exec")`
(`harness/provenance.py:43`).

There is a second, independent blocker: its comparison kind is `BETWEEN_ARMS`
(`harness/comparison.py:51`) and `RECONCILABLE = ("AGAINST_PRINTED_VALUE",)`
(`harness/comparison.py:58-61`). **No between-arms arithmetic exists anywhere in the
codebase.** Making focused validation real therefore needs a new comparison semantics, not
just a provenance change. `harness/discovery.py:129-134` already removed it from the three
between-arms question kinds for exactly this reason.

### 2.3 Literature search has no code at all

Zero executor, no network client, no retrieval. Stated in
`harness/discovery.py:149-151` and as a formal non-guarantee in
`harness/guarantees.py:246-248`.

### 2.4 CORRECTION: the reconstruction driver is NOT orphaned

One audit concluded that `harness/reimplement_driver.py` has no production caller. **That
is wrong**, and both the code and the frozen run disprove it.

- Call sites: `harness/stages/probe.py:1103` (`load_accepted`), `:1105` and `:1026`/`:1054`
  (`available`), `:1108` (`build_brief`), `:1112` (`run`). It is reached from the route
  fallback after author-code identity fails.
- The v2 run exercised it. `runs_final_codex_v2_2026-09-15` contains **11
  `*.driver.json` records** across six papers, each recording a distinct generator
  subprocess and a distinct verifier subprocess, both real
  `codex exec --ephemeral --sandbox read-only -m gpt-5.6-luna` invocations with separate
  temp prompt directories, and each carrying `established: false`.

The manuscript's reconstruction claims are therefore sound. What remains true is that
`harness/alignment/trial.py` **is** orphaned (`may_trial` / `run_trial` called only from
its own self-check and `tests/test_alignment.py`), which was already recorded in
`CLAUDE.md`'s Known limitations.

---

## 3. Paper reading: the system has never read a whole paper

Two independent ceilings, and the smaller one is not the budget.

### 3.1 The prompt budget

`SECTION_BUDGET_CHARS = int(os.environ.get("SH_AUDIT_BUDGET_CHARS", "70000"))`
(`harness/stages/audit.py:36`) is the total for **all section prose combined, per lens
invocation**. `pdf.render_sections` (`harness/pdf.py:988-1003`) divides it **equally
across sections** with a 400-character floor, then hard-slices `s.text[:per]` and appends
a literal `…[truncated]` marker.

Equal division is deliberate and correct: cutting from the end of the document would drop
Conclusions and Limitations, which is where a contradiction lens finds the concession that
undercuts the abstract (`harness/pdf.py:989-993`).

**Tables, figures and equations are not budget-limited at all.**
`render_tables`/`render_figures`/`render_equations` (`harness/pdf.py:955, 974, 981`) take
no budget argument (`harness/stages/audit.py:152, 157, 158`). The 33-84% loss the
manuscript reports is a loss of running prose only.

### 3.2 The extraction ceiling, which is hard and earlier

These apply before anything is rendered, so raising the budget cannot recover them:

| cap | value | site |
|---|---:|---|
| `MAX_PAGES` | 60 | `harness/pdf.py:38` |
| `MAX_SECTION_CHARS` | 40,000 | `harness/pdf.py:42`, applied `:390` |
| `MAX_TABLES` / `MAX_ROWS` / `MAX_COLS` | 40 / 60 / 12 | `harness/pdf.py:39-41` |
| `MAX_FIGURES` | 40 | `harness/pdf.py:127` |
| `MAX_EQUATIONS` | 60 | `harness/pdf.py:128` |
| `MAX_CROSSREFS` | 400 | `harness/pdf.py:149` |
| `MAX_NUMBERS` | 120 | `harness/pdf.py:902` |

### 3.3 One context, four lenses

`run_audit` computes `ctx = context(doc)` **once** at `harness/stages/audit.py:300` and
reuses the same truncated string for all four lenses in the loop at `:303`. There is no
chunk loop, no pagination and no per-section traversal anywhere in `stages/audit.py`,
`prompts/audit.py` or `audit_driver.py`.

### 3.4 What full coverage would actually cost

Not a constant bump. Four things have to move together:

1. `harness/stages/audit.py:36` and `harness/pdf.py:988-1003` need a genuine
   no-truncation or chunked path.
2. `harness/stages/audit.py:300-312` becomes N calls per lens instead of one, which
   multiplies token cost by N and needs its own budget knob.
3. `harness/pdf.py:1031-1039` and `harness/coverage.py:137-152, 251` must change in
   lockstep — `harness/coverage.py:511-518` asserts the presentation arithmetic reproduces
   the render arithmetic exactly.
4. **Merging findings across passes is an unsolved problem this codebase has already been
   burned by once.** Merging across lenses on one prompt already produced false merges when
   keyed on the lens-written reference; chunking one lens across passes reopens the same
   seam, plus a new question nobody has decided: does pass 2 see pass 1's findings (risking
   anchoring) or read blind (risking duplicates that then need reconciling under the
   cap-only rule)?
5. `lens_is_accepted` (`harness/stages/audit.py:201-230`) validates one file with one
   sidecar per lens per run. A chunked lens produces N partial results that must be
   combined before the existing sealing logic applies. That combining step does not exist.

### 3.5 Geometry is extracted and then discarded

pdfplumber word coordinates (`x0`, `x1`, `top`) are computed and used inside the
unruled-table reconstruction (`harness/pdf.py:621-668, 697-755`). PyMuPDF is called only
as `get_text("text")` (`harness/pdf.py:269`) — plain reading order, no boxes.

**None of `Table`, `Figure`, `Equation`, `Section` or `CrossRef`
(`harness/artifacts.py:86-203`) carries any coordinate field.** Geometry exists
transiently during extraction and is thrown away before `PaperDoc` is serialised, so no
downstream check can use it. This is the direct cause of three refused capabilities:
heading detection is text-shape only and degrades a small-caps paper to one giant section
(`harness/pdf.py:251-254`); figure captions have no spatial pairing at all; and
`PROSE_CELL_MISMATCH` cannot be computed safely (`harness/docintegrity.py:87-94`).

---

## 4. There is no claim graph

There is no `Claim` class and no graph structure — no node type, no edge type, no
traversal function. What exists is five flat record types linked by string id:

`ClaimRef` (an address) → `Finding` → `ReviewQuestion` → `DiscoveredObject` (a target) →
`TargetOutcome`. "What depends on what" is reconstructed at report time by matching ids
across four separate lists, e.g. `harness/materiality.py:313-330` linear-scans for
`target_id ==`.

### 4.1 Centrality is positional and one of its inputs never fires

`_centrality` (`harness/discovery.py:76-102`) reads four booleans:

- `in_abstract` — read the untitled front-matter block rather than the abstract
  (`ref.section_idx == doc.sections[0].section_idx`). **Fixed**: `discovery` now uses
  `materiality.abstract_section_idx`, the harness's single abstract locator.

  **AND THE FIX CHANGED NOTHING, WHICH IS THE INTERESTING PART.** The re-derivation was
  run over the eight-paper corpus before the locators were unified:

  ```
  legacy abstract locator (doc.sections[0]):     0 / 706 discovered objects
  corrected abstract locator (materiality):      0 / 706 discovered objects
  ```

  No object's centrality moves in either spelling, so **the defect did not materially
  affect the v2 run** and nothing in that corpus is restated because of it. The reason it
  fires on nothing is structural: discovered objects come from table cells and parsed
  quantities, and an abstract carries prose. `in_abstract` contributes nothing to
  centrality on real papers however it is spelled.

  What the null result actually demonstrates is larger than the bug it closes.
  **Object-level centrality is not a reliable representation of claim importance.** Of
  four inputs, one fires on nothing, one is a sentence-shape test, one is extractor
  bookkeeping, and the remaining one is a count of which addresses a lens chose to attack
  — so on this corpus centrality is very close to a record of where the panel looked.
  24 of 706 objects are CENTRAL.

  **The response is not another heuristic weight.** Adding a fifth boolean would move the
  number without making it mean anything, and the measurement above is the evidence that
  tuning this family of inputs is the wrong move: the family measures the wrong thing.
  What importance actually is — which claims the paper's argument depends on — is a
  relation between claims, and representing it is §12 of the brief, the claim/evidence
  graph. Until that exists and has been measured against the current rule on all eight
  papers, the decision rule stays where it is.
- `anchored_by_confirmed` / `anchored_by_any` — whether a lens cited this address. A
  citation count, not an importance measure. `harness/materiality.py:20-23` records that
  **105 of 112 CENTRAL objects are CENTRAL only because a lens attacked the address.**
- `self_checking` — the span has the form `a*b*c=d`. Sentence form, not importance.
- `is_reported_result` — an extractor bookkeeping flag.

So today "central" mostly means "a lens wrote about it".

### 4.2 Materiality is one narrow citation test

`materiality.basis_for_ref` (`harness/materiality.py:267-305`) calls a target material only
if its address resolves inside the paper's Abstract or Conclusion, or if the
Abstract/Conclusion carries a verified structured citation of the target's uniquely
labelled table/figure/equation (`harness/materiality.py:187-204`).

This is a dependency representation, but only of the shape *Abstract/Conclusion cites
object X*. **There is no representation of "the headline number depends on this ablation"
or "this comparison depends on that split."** The module says so itself
(`harness/materiality.py:29-34`): a conservative sufficient condition, not a materiality
model.

---

## 5. Decision semantics are already close to what the brief asks for

`PAPER_DISPOSITIONS` (`harness/disposition.py:72-86`) is the single terminal vocabulary,
and it maps almost one-to-one onto the states §15 of the brief requests:

| existing | brief asks for | gap |
|---|---|---|
| `STOP_MATERIAL_FAILURE` | `MATERIAL_FAILURE_ESTABLISHED` | rename |
| `PASS_TO_HUMAN_CLEAN` | `FIRST_PASS_CLEAR_WITHIN_SCOPE` | rename **and tighten**: today it does not require route exhaustion |
| `PASS_TO_HUMAN_CONCERNS` | `PASS_TO_HUMAN_WITH_CONCERNS` | rename |
| `BLOCKED_SPECIFICATION` / `_ARTIFACT` / `_RESOURCES` / `_METHOD`, `PASS_TO_HUMAN_UNRESOLVED` | `UNRESOLVED_EXTERNAL_BLOCKER` | the existing four are **finer** than the brief's one; keep them and add the umbrella |
| `NOT_REVIEWED` | — | keep |

**"Approved" is already absent from every reader-facing artifact.** A repo-wide search
finds `approved` only as an internal JSON field in the reconstruction verifier's response
schema (`harness/reimplement_driver.py:222-238`) and in one code comment
(`harness/stages/probe.py:1020`). `harness/stages/report.py` and every file in
`harness/prompts/` have zero matches. No change needed there.

The real gap is the **meaning** of a clean pass. Today `PASS_TO_HUMAN_CLEAN` means "no
material failure established". The brief wants it to additionally require that every
material question's routes were exhausted. That is a genuine tightening and must be
implemented, not renamed.

---

## 6. Execution: the container backend is real code that nothing has ever tested

### 6.1 What exists

| backend | `can_execute` | isolation | `execute()` |
|---|---|---|---|
| `local` | True | `VENV` | real `subprocess.run` (`harness/backends.py:450-467`) |
| `container` | True (`harness/backends.py:570`) | `CONTAINER` | **real `docker run`** (`harness/backends.py:709-743`) |
| `modal` | True | `REMOTE_SESSION` | real, delegates to a leased session (`harness/backends.py:1024-1051`) |
| `kaggle`, `colab` | False | — | raise (`harness/backends.py:802-806`) |

`ContainerBackend` is not a declaration. It builds
`docker run --rm -v <host>:/work -w <dir> [--gpus all] [--network none] ... <image> <argv>`
(`harness/container.py:182-205`) and really executes it with `subprocess.run`
(`harness/backends.py:729-731`). `provision()` really runs `python -m venv` and
`pip install -r` inside the container (`harness/backends.py:667-682`). `capability()`
delegates the import probe to the container's own interpreter
(`harness/container.py:595-618`). `gpu_available()` starts a throwaway container with
`--gpus all` (`harness/container.py:101-116`).

### 6.2 What is missing

**`tests/` contains zero references to `ContainerBackend`, `harness.container`, or
`container_mod`.** The module's only exercise is its own `_self_check`
(`harness/container.py:209-261`), which tests path translation and argv construction and
probes `daemon_status()` — it never asserts that a container actually ran a payload.

Every real process this system has ever launched came from `LocalBackend.execute()` under
the `confined_local` fixture (`tests/conftest.py:33-56`), which
`monkeypatch.setattr(backends.LocalBackend, "isolation", "REMOTE_SESSION")` — it **lies
about the isolation level** so `authorize()` passes, then runs against a two-line synthetic
git fixture. That is a legitimate way to test the reconciliation logic and it is not
evidence that the product can execute anything.

### 6.3 Environment state on this host, 2026-09-16

Docker CLI 29.7.2 is installed; **the daemon is not running**
(`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`). No
container can start until Docker Desktop is launched.

### 6.4 Reconciliation is stronger than the brief assumes

`local_exec.reconcile` (`harness/local_exec.py:632-988`) is not numeric proximity. Six
preconditions run before any arithmetic: authorization, the authority ceiling,
reconstruction conformance, comparison-kind admissibility, identity establishment, and
capability/infrastructure gating. Then the arithmetic itself carries a units-mismatch guard
(`:905-912`), printed-precision credit (`:264-284`, `:922-928`), and a deterministic-count
branch with zero tolerance (`:941-959`).

The tolerance is **empirical, not a constant**: `noise_band = 2 * measured_std`, from
paired seed-to-seed differences where arms share seeds (`harness/local_exec.py:997-1018`),
over 3 to 5 seeds (`harness/stages/probe.py:165`), one fresh subprocess per seed per arm
(`harness/local_exec.py:1149-1298`).

What is genuinely absent: **no formal statistical test** — no t-test, no confidence
interval, no p-value. The only rule is `effective_delta <= 2σ`. §13 of the brief asks for
"statistically justified tolerance"; a 2σ band from 3-5 seeds is a defensible rule but the
paper must not call it a statistical test.

---

## 7. Corpus selection, verified from disk

| quantity | value | source |
|---|---:|---|
| papers collected | 36,404 | `data/exports/screening_statistics.csv` |
| decided eligible | 387 | same |
| decided excluded | 1,194 | same |
| decided review_required | 2,551 | same |
| triage-held, releasable | 2,113 | same |
| triaged only, never screened | 30,125 | same |
| reserved random sample | 200, seed 42, drawn 2026-09-01 | `data/exports/sampling_manifest.json` |
| second-stage sample | 3, same RNG stream | same |

**Screening mode was `shuffled_stop` with `eligible_target: 500`, and it stopped at 387.**
So 30,125 of the 36,404 were only triaged, never screened. `387 / 36,404` is therefore
**not** an eligibility rate and the paper must not present it as one.

**The eight evaluated papers are not from any of these sets.** Checked directly: none of
`0c06a98d7c818f6f`, `2024-icml-sapg`, `5993d35ff0996b52`, `acl`, `apt-icml`, `cvpr`,
`iclr`, `sanchez24a-icml` appears in `eligible_papers.csv`, in `sample_200`, or in
`sample_3` (whose members are `3f768a63fa8b8d9d`, `32dec1cdb1e7a0a0`, `a8c4c1ad09f5311e`).
The harness assigns its own slug or content-hash ids, so the sets are also not directly
comparable by id — which is itself a reason the figure must show two disjoint tracks
rather than one funnel.

---

## 8. The original feedback audio

**Located**: `C:\Users\saita\Downloads\WhatsApp Video 2026-08-26 at 10.51.35 PM.mp3`,
13,212,402 bytes, mp3, 48 kHz stereo, **duration 1,178.24 s (19 min 38 s)**.

It is not in the repository or in any of the three project zips. Transcription capability
on this host: `ffmpeg`/`ffprobe` on PATH, `torch 2.13.0+cu126`, `transformers` importable,
RTX 4060. No Whisper package was installed; the user authorised downloading
`openai/whisper-large-v3-turbo` and transcribing locally, so the audio never leaves this
machine. Status of that transcription is recorded in
`docs/ORIGINAL_AUDIO_FEEDBACK_TRANSCRIPT.md`.

---

## 9. The gap list, ordered by what the product claim needs

| # | gap | brief | severity |
|---|---|---|---|
| 1 | No reader ever sees a whole paper; 33-84% of prose, hard extraction caps behind that | §3 | **blocking the product story** |
| 2 | `ARTIFACT_INSPECTION` cannot produce evidence; `ARTIFACT_EVIDENCE` unreachable | §6, §7 | **blocking** |
| 3 | `FOCUSED_VALIDATION_EXPERIMENT` runs but can never conclude; no between-arms arithmetic exists | §6, §10 | **blocking** |
| 4 | `ContainerBackend` has zero tests and has never run; Docker daemon down | §11, §25 | **blocking** |
| 5 | No claim/evidence graph; centrality is a citation count; `in_abstract` fires on 0 of 878 | §4 | **blocking materiality quality** |
| 6 | `FIRST_PASS_CLEAR` semantics do not require route exhaustion | §15 | high |
| 7 | Geometry discarded before `PaperDoc`; blocks prose-vs-cell and caption pairing | §5 | high |
| 8 | `LITERATURE_SEARCH` declared with no code | §6 | scope decision needed |
| 9 | Manuscript says four model components; there are six on the always-on path | §1 | manuscript |
| 10 | No statistical test behind the 2σ tolerance | §13 | manuscript wording |
| 11 | `harness/alignment/trial.py` orphaned; `SH_ALLOW_ALIGNMENT_TRIAL` gates nothing | §26 | low |
| 12 | Corpus provenance must show two disjoint tracks, and 387/36,404 is not a rate | §17 | manuscript |

Gap 8 needs a product decision rather than engineering: novelty checking is either in scope
and must be built, or out of scope and must be removed from the declared vocabulary. It
cannot stay declared-and-empty.
