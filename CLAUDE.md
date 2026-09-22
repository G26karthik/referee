# REFEREE / single-harness

## Mission

REFEREE is an autonomous first-pass scientific-review copilot.

**Input:** paper PDFs  
**Output:** a concise reviewer-facing report containing scientific concerns, exact paper evidence, unresolved questions, verification/reproduction status, and a machine trace.

REFEREE is **not** an accept/reject system. Do not add RED/YELLOW/GREEN, paper scores, or another disguised venue-decision signal. The human reviewer makes the final decision.

The core unit is the **question**: a finding proposes a concern; a `ReviewQuestion` states what would settle it, which routes can answer it, what evidence was obtained, and what follows scientifically.

Pipeline:

`PDF -> ingest -> audit -> collect -> grade -> assess -> discover -> verify/execute -> reconcile -> report`

Models propose scientific judgments. Deterministic code controls evidence identity, provenance, admissibility, execution authorization, reconciliation, materiality, and reporting guarantees.

## Start here

The current code is the source of truth. Read code before making claims about it. If documentation conflicts with live code, investigate and update the stale documentation rather than guessing.

Read only what the task needs:

- `HANDOFF.md` — setup and current handoff.
- `docs/HARNESS_ARCHITECTURE.md` — stage contracts, evidence chain, address grammar.
- `docs/V4_FINAL_REPORT_2026-09-22.md` — v4 redesign result and remaining caveats.
- `docs/V4_PERFORMANCE_GAP_ACL_2026-09-21.md` — reference-vs-v4 ACL comparison.
- Git tag `reference-implementation-2026-09-20` — frozen pre-v4 implementation.

Do not preload historical revision notes into context unless the task is specifically historical.

## Canonical architecture

Core modules:

- `harness/schema.py` — shared models and closed vocabularies.
- `harness/locate.py` — mint/resolve paper evidence locations.
- `harness/paper.py` — paper representation, reading plan, long-paper parts/anchors.
- `harness/agent.py` — model-role orchestration.
- `harness/audit.py` — four lenses, grounding, grading, finding derivation.
- `harness/discover.py` — questions and verification targets.
- `harness/decide.py` — experiment necessity, materiality, disposition.
- `harness/routes.py` — verification-route planning/spec construction.
- `harness/execute.py` — authorization, execution backends, metric parsing, reconciliation.
- `harness/report.py` — bounded human report, machine report, coverage/integrity/guarantees.
- `harness/pipeline.py` — phase machine, preflight, retries, orchestration, accounting.
- `harness/summarize.py` — corpus/dossier summaries.
- `run.py` — CLI.

Important satellites include `taxonomy.py`, `provenance.py`, `repo.py`, `experiment_id.py`, `resources.py`, `container.py`, `artifact_evidence.py`, `artifact_review_driver.py`, `reimplement_driver.py`, `reviewer_cli.py`, `delegation.py`, `sealing.py`, `failures.py`, `assessment.py`, `stages/ingest.py`, and `stages/artifact.py`.

Deleted capabilities are intentionally absent: claim-link channel, bounded literature/prior-art route, focused-validation/between-arms route, and remote Modal sandbox. Do not resurrect them merely because historical docs mention them. Novelty is not assessed. Container execution is the current isolation path.

## Scientific state model

Keep these concepts separate:

- `scientific_class`: what kind of scientific problem this is.
- `resolution_status`: whether/how the review question was settled.
- `evidence_state`: what the evidence route actually produced.
- `artifact_state`: what artifact path was available.
- `claim_status`: `VERIFIED_FAILURE`, `VERIFIED_SUPPORT`, or `NOT_VERIFIED`.
- `disposition`: routing from `decide.derive_disposition`; see `schema.PAPER_DISPOSITIONS`.

`disposition` is routing metadata, not a paper score or venue recommendation.

The reviewer-facing outcome has four disjoint rows: `finding_state`, `question_state`, `execution_state`, `scope_state`.

Infrastructure/execution state must never silently alter what the review established about the paper.

## Immutable invariants

Do not weaken these to make more papers executable, produce more findings, or simplify code.

1. **Evidence must resolve.** Every serious paper quotation/cell is re-resolved against the parsed paper by `locate.py`. If any required side of a concern does not resolve, drop the concern and record the drop. A model-written locator is never trusted by itself.

2. **No self-certification.** Harness-owned fields such as verification state, provenance authority, acceptance/sealing state, and derived classifications are computed by the harness, not trusted from model output.

3. **Provenance ceiling.** Only admissible provenance may establish or refute a printed result. Synthesized/model-only diagnostics can neither convict nor acquit. Independent reimplementation additionally requires established conformance before it can settle a claim.

4. **Execution has one gate.** Only `execute.authorize()` may permit repository execution. It requires the execution gate, executable backend, admissible provenance, pinned/verified commit, experiment+metric+configuration identity, capability, and sufficient resources.

5. **Environment failure is not scientific failure.** Dirty/mismatched commits, unknown or insufficient resources, dependency/platform/capability problems, or failure before the experiment demonstrably starts produce a refusal/blocked/inconclusive state, never `FAILED_REPRODUCTION`.

6. **No experiment downscaling.** Never shrink, substitute, or simplify a paper's experiment merely to fit available hardware. Refuse instead.

7. **Material failure is structural.** `STOP_MATERIAL_FAILURE` requires an established admissible failure and a demonstrated dependency of a central claim on that target. Model-assigned severity alone cannot stop a paper; counts of MAJOR/MINOR findings never accumulate into a material failure.

8. **Severity only moves downward.** Grading, evidence ceilings, confidence rules, and self-consistency checks may cap/demote a lens severity; nothing may raise it above the lens's own assertion. Evidence type bounds confidence, not scientific importance directly.

9. **Questions drive verification.** A lens may propose that something is experimentally verifiable, but the harness decides addressability, route, priority, necessity, authorization, and settlement. A necessity decision is not evidence.

10. **Cheap checks cannot launder authority.** Re-verifying that a quotation exists (`CITATION_VERIFIED`) does not prove the quoted scientific claim and must not suppress a stronger route that is actually required.

11. **Targets are independent.** A blocked target ends that target, not the paper. Report blocked, reproduced, failed, unresolved, and not-attempted targets separately.

12. **Artifact evidence is bounded.** Static code inspection may establish what released code/configuration contains. It does not by itself establish that the paper's reported scientific result is false. Experiment identity must be established, not guessed by a model.

13. **Document integrity is not a paper finding.** Extraction/recovery observations have their own type and never change scientific disposition. Coverage denominators come from the paper, not from the harness's own discovered-object list.

14. **Long-paper isolation holds.** Lenses are independent. Parts of one lens are blind to other parts' model findings; only deterministic anchors cross part boundaries. Lens-local synthesis sees only that lens's already-grounded observations and remains subject to the same grounding/caps/grading.

15. **Batch accounting is exact.** `pipeline.preflight_check` refuses duplicate documents before spending model calls. Every requested paper reaches exactly one terminal state. Retry budget is spent only on failures that can plausibly change on retry.

16. **Execution is auditable.** Every started process records command, cwd, commit, timestamps, exit code, stdout/stderr, and parsed metric so a reconciliation can be re-derived by hand. Metric reconciliation must bind to the intended target; positional/last-JSON coincidence is not evidence.

17. **No paper-specific logic.** Never add paper names, pilot-paper exceptions, metric-specific severity floors, or special thresholds that encode evaluation cases into production behavior.

18. **Reviewer report != machine trace.** Keep the human report bounded and explicit about omissions; preserve the detailed ledger/machine artifacts separately.

19. **No acceptance layer.** Do not reintroduce global colors, accept/reject verdicts, or a substitute score. Whole-paper model assessment may be displayed/compared, but it cannot override deterministic evidence/disposition logic.

## Model roles and context

Four independent audit lenses:

- overclaim
- protocol
- confound
- contradiction

They are proposal streams, not four pieces of evidence.

Respect the current tiered routing in code.

Use cheaper models for mechanical/simple work and Sonnet-level reasoning for difficult scientific judgment.

Do not upgrade every role or introduce Opus without a demonstrated need.

Minimize context per role. Prefer targeted sections, anchors, evidence snippets, and structured state over repeatedly sending entire papers/history.

Preserve full-paper coverage through the reading-plan mechanism rather than truncating prose.

## Commands

Use the repository virtual environment.

On Windows set `PYTHONUTF8=1`.

```bash
python run.py tasks <paper.pdf|paper-id> --json   # advance deterministic stages; list pending tasks
python run.py seal <paper-id> <task-id> <file>     # validate + seal one subagent answer
python run.py status <paper-id>
python run.py list
python run.py preflight
python run.py dossier
python run.py evaluate
```

**Delegation has exactly one channel.** The harness never spawns a `claude` CLI. Model work
(lens parts/syntheses, grades, verdict, reconstruction generator + verifier, certificate
generator + verifier, artifact review, extraction audit) is done by isolated subagents of
the controlling session: each reads its task's `prompt`, writes JSON to `out`, and runs
`run.py seal`; the harness validates and seals (mode `SESSION_SUBAGENT`). The saved
workflow `.claude/workflows/referee.js` runs this loop autonomously (Haiku controller,
Sonnet workers, explicit model on every agent — never Opus). Execution gates:
`SH_ALLOW_REIMPLEMENTATION_EXEC`, `SH_ALLOW_CERTIFICATE_EXEC`, `SH_ALLOW_REPO_EXEC`,
`SH_EXEC_BACKEND=container` (Docker).

Never edit:

```text
projects/<pid>/audit/prompts/*.md
```

They are generated.

## Working rules for Claude Code

- Before modifying anything, inspect `git status`, current branch/recent commits, the relevant files, and their live call sites.
- Never speculate about code you have not opened.
- The architecture is currently frozen for experiments. Fix demonstrated problems; do not start another broad redesign, compatibility layer, framework migration, or LOC campaign unless explicitly asked.
- Prefer deletion/simple code over abstractions when behavior is unchanged, but never trade away the invariants above for LOC.
- Subagents are **read-only by default**.
- A writing subagent must have explicit, non-overlapping file ownership.
- The main session integrates and commits shared architectural files.
- Preserve unrelated dirty work.
- Never force-reset, force-push, or delete unrelated files/worktrees.
- Use normal Git checkpoints for consequential changes.
- Clean up temporary scripts/logs/worktrees created for a task.
- Do not run the whole corpus unless explicitly asked.
- For engineering verification, prefer one relevant paper and cached deterministic stages where possible.
- Do not repeatedly run large legacy test suites while iterating.
- Use compile/import/static checks and focused real reproductions.
- For high-stakes logic, prefer differential execution and an independent cold review over “the same agent wrote code and then wrote tests that pass.”
- The legacy `tests/` tree predates v4 and much of it is stale/incompatible.
- Do not repair or replace it wholesale unless explicitly requested.
- Do not use passing self-authored tests as the sole evidence that a scientific/trust invariant is correct.
- When claiming a regression or improvement, give the concrete paper evidence or reproduction.
- Raw finding counts are not enough because model outputs are nondeterministic.
- Keep token/model spend visible.
- Avoid duplicate model calls, unnecessary reruns, and sending irrelevant history to subagents.

## Current validation posture

V4 is the canonical implementation.

The old implementation is preserved at Git tag:

`reference-implementation-2026-09-20`

Use it only for archaeology or differential checks.

The ACL differential study found **no demonstrated architectural scientific-quality regression** after paper-grounded comparison.

That is not proof of universal equality or superiority.

Treat new-paper experiments as the source of future performance evidence.

There is no adjudicated human ground truth in this repository, so do not claim reviewer precision, recall, or accuracy.

External human comparison belongs to the later research evaluation, not normal system execution.

Model-authored severity and scientific interpretation remain model judgments even when their citations and deterministic derivations are checked.

Extraction quality also bounds what can be grounded.

State uncertainty/refusal explicitly rather than filling gaps.

## Output artifacts

For `projects/<pid>/`:

- `reports/<pid>.review.md` — bounded human-facing review.
- `reports/<pid>.ledger.json` — trace from report claims to machine artifacts.
- `discovery/targets.json` — considered questions/targets and routing decisions.
- `reports/<pid>.md` / `.json` — full machine report/trace.

A complete review may contain no material failure.

Abstention, blocked execution, or `NOT_VERIFIED` are valid outcomes when evidence is insufficient.