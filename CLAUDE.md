# REFEREE / single-harness

## Mission

REFEREE is an autonomous first-pass scientific-review copilot: paper PDFs in, a bounded
reviewer-facing report out (concerns with the paper's own words, what was checked and how,
what remains open) plus a machine ledger. It is **not** an accept/reject system: no colors,
scores or disguised venue decisions. The human reviewer decides.

It is a **lightweight, prompt-driven harness**. Isolated subagents make the scientific and
routing judgments (what is wrong, what to check, how to check it). Deterministic code exists
only where trust needs it: re-finding every quote, the execution gate, the container runner,
reconciling numbers, and computing every status.

## Pipeline

`ingest -> 4 lenses -> critic -> planner -> per check: bind | gen -> verify (1 revision) -> execute + reconcile -> report writer -> done`

| File | Responsibility |
|---|---|
| `run.py` | CLI: `tasks`, `seal`, `try`, `exec`, `status`, `pack` |
| `harness/paper.py` | PDF -> page texts, visual rows, page PNGs, identifiers; `paper.md` for workers |
| `harness/evidence.py` | quote re-finding, printed numbers, same-row cell rule, exact arithmetic |
| `harness/repo.py` | author-repo attribution, clone, `verify_commit`, listing, released-data hashes |
| `harness/tasks.py` | the protocol: which task is owed, seal validators (what a model may set) |
| `harness/execute.py` | `authorize` (the one gate), container runner, env build, failure classes, metric parse |
| `harness/reconcile.py` | executed value vs printed value; statuses |
| `harness/report.py` | ledger, deterministic status table, earned-language check, `review.md` |
| `harness/state.py` | config/gates, atomic JSON, project lock |
| `harness/prompts/*.md` | every model instruction (lenses, critic, planner, bind, gen, verify, report) |
| `tests/test_kernel.py` | the trust kernel only |
| `.claude/workflows/referee.js` | the autonomous loop (Haiku controller, Sonnet workers) |

Old implementation: git tags `v4-final-2026-09-28` (v4) and `reference-implementation-2026-09-20`.

## Invariants (do not weaken)

1. **Evidence must resolve.** Every model quote is re-found in the parsed paper (unique
   occurrence) or checkout; an unresolved quote is refused, then its item dropped and recorded.
2. **No self-certification.** Seal validators copy only harness-derived fields; statuses,
   provenance, identity and approval are computed by the harness.
3. **Provenance ceiling.** Only `execute.EVIDENCE` kinds settle a printed result; a draft
   `try`, a model's reasoning or an unapproved script settles nothing.
4. **One execution gate.** Only `execute.authorize` permits a process: explicit env gate,
   container runtime, and for author code an attributed repo, a clean pinned commit and
   established identity; for model scripts an independent approval of that exact sha.
5. **Environment failure is not scientific failure.** Refusal = BLOCKED; setup, infra,
   timeout or unproven start = INCONCLUSIVE; a model script crashing is INCONCLUSIVE.
6. **No downscaling.** The harness runs the documented command as documented (only the value
   of its own seed flag varies); refuse rather than shrink.
7. **Experiment identity** is established by two independent keys (planner and binding
   verifier name the same verbatim command and metric key at the pinned commit).
8. **Severity only moves down** (evidence-class caps; the critic can only lower).
9. **Checks are independent**; a blocked check ends that check, not the paper.
10. **Execution is auditable**: every process leaves an ExecutionRecord (argv, image,
    commit, script sha, times, exit code, stdout/stderr) in `execution.jsonl`.
11. **No paper-specific logic**, names, thresholds or special cases.
12. **Report != trace; no acceptance layer.** The status table is deterministic; model prose
    is published only if every status word it uses is earned by a cited check.

## Commands (repo venv; on Windows set PYTHONUTF8=1)

```bash
python run.py tasks <paper.pdf|paper-id> --json [--wait 540]
python run.py seal <paper-id> <task-id> <answer.json>
python run.py try <paper-id> <gen-task-id> <script.py>      # draft run, masked, never evidence
python run.py status [<paper-id>]
python run.py pack <out.zip> [<paper-id> ...] --clean      # zip artifacts, then delete clones/venvs
python tests/test_kernel.py
```

Gates: `SH_ALLOW_REPO_EXEC`, `SH_ALLOW_SCRIPT_EXEC`, `SH_ALLOW_INSTALL`, `SH_ALLOW_NETWORK`
(default on, for cloning), `SH_ALLOW_SOURCE_SEARCH`. Caps: `SH_MAX_CHECKS` (3),
`SH_MAX_REVISIONS` (1), `SH_MAX_TRIES` (3). Docker is the only execution backend.

Delegation has exactly one channel: the workflow's isolated subagents read a task's files,
write JSON to `out`, and run `run.py seal`. The harness never spawns a model. Launch an
edited workflow with `Workflow({scriptPath: ".claude/workflows/referee.js", args})` — a named
launch uses the copy cached at session start.

## Working rules

- Read code before claiming anything about it; the code is the source of truth.
- Keep it small: new behavior goes into a prompt unless it is one of the invariants above.
  Every cap gets a `ponytail:` comment naming its ceiling. Scale assumption: one host, a
  few papers per run.
- Inspect `git status` first; preserve unrelated dirty work; never force-reset or force-push.
- Subagents are read-only unless given explicit, non-overlapping file ownership.
- Keep token spend visible (`scratchpad/wfusage.py` on a workflow dir); check brief sizes
  (`ls -l projects/*/tasks`) before a run spends tokens on them.
- After a run: `run.py pack <zip> --clean` so clones, venvs and data do not accumulate.
- There is no adjudicated ground truth here: never claim precision/recall; a self-written
  test passing is not evidence that a scientific invariant holds.
