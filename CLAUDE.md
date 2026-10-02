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
| `harness/fetcher.py` | container-side acquisition: final-URL link resolution, failure classes, bounded recovery, content validation |
| `harness/discover.py` | registry search (Zenodo, DataCite, HF datasets) for datasets a paper names; the log a plan's `acquire` is checked against |
| `harness/independence.py` | are seeded replicates different runs? (result-line outputs, data fingerprints, the seed's flow into a generator) |
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
13. **Central claims first; statuses say what was found.** The paper-level status is computed over
    central claims only (FAILURE_FOUND, PROOF_GAP_FOUND — a failed proof step is never a refuted
    statement —, READINGS_DISAGREE, SUPPORT_FOUND, NO_VIOLATION_FOUND, NOTHING_DECIDED, NOT_CHECKED).
    A check no central claim cites is incidental: cut first from the budget, it must say why
    (`incidental_why`), and is reported apart; workflow completion is reported apart again.
14. **Compared values are bound by name, once.** AUTHOR_CODE: the planner's metric key (two
    keys). Scripts: the author's named `metric`, a stated `relation` over named outputs, or
    `violated` for a certificate — never a flag computed against the printed number.
15. **Deviations are recorded; readings are never chosen.** Every departure from the printed
    text is declared, re-found and flagged `changes_claim` (it alters a premise, conclusion,
    index or definition) or not (it fixes a detail the claim leaves open: a protocol choice
    REFEREE supplied, reported as such with runs, seeds and decision rule). Two checks of one
    printed object that disagree are CHECKS_DISAGREE. Where the paper's text and the checkout's
    code define the compared quantity differently, both are declared `readings` (each re-found:
    paper verbatim, code literal in a tracked file) and computed in ONE run on the same data and
    cohort (`reading`, `cohort` per result line); each is decided, neither is presumed right, and
    differing results are READINGS_DIFFER (claim READINGS_DISAGREE). Without a checkout, a released record's
    own code, notebooks or README are kept as quote-only text (`checks/<id>/record_src`, never mounted or run)
    and may be a reading (`record:<n>/<path>`). One printed sentence that several checks applied differently is
    listed side by side (`definition_choices`) and shown to the follow-up round; nothing picks one.
16. **A counterexample satisfies every premise of the exact claim.** Certificates report
    `premises_hold` per instance; a violation on an inadmissible instance is nothing, one found
    only under a changed reading is VIOLATION_UNDER_CHANGED_READING, no admissible instance is
    PREMISE_NOT_MET (claim status READING_CHANGED / PREMISE_NOT_MET, never a failure).
17. **Small samples are decided with Student-t** (two-sided 95%): a relation beyond t·SE; a
    reproduction RESOLVED inside the CI of the mean, FAILED outside the prediction interval.
    Runs are ONE measurement (`n_independent` 1) only when they are identical in every output and
    nothing shows the seed varied them; a recomputation from released files runs once, a pipeline its
    author declares deterministic (`stochastic: false`) runs twice and must repeat exactly. Equal
    SUMMARY values are not identical runs: replicates whose other outputs differ, whose
    `data_fingerprint` differs, or whose `--seed` provably reaches a random generator are independent
    replicates of a zero-variance sample, decided by the statistic that fits the quantity — a compared
    proportion of counted trials (`binomial`, zero events included) by its exact Clopper-Pearson interval,
    anything else by an exact sign test (six replicates reach 95%; a finished check that only more
    replicates can decide — a repeated value, or a margin within t*SE at fewer than six, t(2) being 4.3 — is
    extended to six once, inside its time budget, whichever way the result leans) — never by a t-test whose
    standard error is zero. A stochastic script whose seed reaches no generator is refused at the seal.

18. **No arithmetic error from extracted math without the page image.** An independent
    transcriber reads every number of an ARITHMETIC_CONTRADICTION or a CONFIRMED arithmetic
    concern off the page PNG (masked context); disagreement withdraws the assertion.
19. **Resources end in a documented blocker, never a loop.** The first run is a timed pilot;
    a projection past SH_CHECK_BUDGET_S, an out-of-memory kill of a lone run, or a run past
    SH_RUN_TIMEOUT_S is a BLOCKED "RESOURCE BLOCKER" with the measurement, not repeated and
    never downscaled. The blocker names its kind (`resource`: time_budget — a configured
    setting —, memory — measured —, per_run_timeout, storage). A run's time is the host's awake
    time: hours a sleeping host froze its containers are not run time (`host_slept_s` recorded).
    What a run printed before its limit is kept beside the blocker (`pilot_stages`), deciding
    nothing. A run given the GPU holds it alone (no second GPU run, draft or evidence, starts beside it) and
    is projected one at a time against the budget: a limit measured under another run's load measures
    the sharing, not the protocol. Completed seeds are checkpointed
    (seeds.jsonl) and reused; each seed has a scratch volume at /work/ckpt. Planners and script
    authors are told the measured host (CPUs, container RAM, GPU yes/no).
20. **Completed measurements survive later failures.** A script prints one result line per
    `stage` (dataset, setting) as it finishes. A seed that fails after measuring keeps what it
    measured and the other seeds still run; each stage is decided over its own seeds (a relation
    must hold in every stage); a stage that started without a result, or a failed seed, makes
    the check PARTIAL (`status_on_completed` kept, claim status PARTIAL_EVIDENCE). Only a run
    that measured nothing ends the check. A check's state (NOT_STARTED, RUNNING,
    PARTIALLY_COMPLETED, COMPLETED, FAILED, RESOURCE_LIMITED, NOT_RUN) is how its runs went
    (`execution`: planned, ended, exited cleanly, result-schema defects), recorded apart from
    what they found. A result-schema defect (a declared unit without its line, an undeclared stage
    name, a missing reading or cohort) is the script's: the draft run returns it to its author; in
    evidence it is recorded, and only one completed run that declared one unit and printed it
    unnamed is read as that unit — several units are never matched by guessing.
21. **No verifier approves a script nobody ran.** The harness runs every sealed script once
    (seed 0, results masked, never evidence) before its verifier; a script failing there goes
    back to its author with the error. A certificate's violation within 1e-9 (relative) without
    exact values printed is round-off, never a counterexample.
22. **Public artifacts are found and acquired, not assumed absent.** A check may `acquire` (a) a source
    the paper or a tracked checkout file prints — matched as printed: a scheme the paper omits, a
    line-break hyphen ("zen-\nodo.org"), and a DOI for its record are one citation, and the span as
    printed is kept with the other readings it can mean — or (b) a record the harness's own registry
    search returned for a dataset the paper names (`run.py discover`, logged in `discovery.jsonl`;
    `SH_ALLOW_DATA_SEARCH`, its own gate: it grants no clone, code or execution, and a run without author
    code still needs it), never a denied source (SH_DENY_SOURCES). Harness code downloads them with the
    network on (cap SH_MAX_DATA_GB) inside `fetcher.py`: links resolve against the FINAL url of a
    response (a DOI redirects to the repository), each failure is classified (transient: retried;
    missing; inaccessible; no data on the page; content invalid; a bug of this code), recovery uses
    documented mechanisms only (another printed reading, the repository's records API with its
    checksums, the DOI registry, an HTTP range that continues a cut transfer of the same version — If-Range on
    its ETag or Last-Modified) and is bounded and recorded, and nothing is admitted unvalidated (an
    HTML page, an empty or corrupt file, a bad checksum, executable source code). A cap on the files followed
    is never silent: a landing page follows 20 links, a repository record its own listing (up to 500), and a
    cut is recorded (`truncated`), shown to planner, script author and report, and never shared as a complete
    set. Every attempt, redirect
    and file sha256 is kept (`checks/<id>/data.json`); admitted files mount read-only at /work/data. A
    check whose required data was not admitted ends as a DATA BLOCKER (or INCONCLUSIVE for a network
    fault of this run) before any script is written — a simulation in its place would be a different
    experiment. A script for a check that acquired data must read /work/data. Scripts print a
    REFEREE_DATA identity line per dataset against the paper's own description; a mismatch is a finding.

23. **Results are limited by what changed.** Support or failure obtained under a claim-changing
    deviation (data, split, tuning, rebuilt baseline, aggregation, premise, index) is
    READING_CHANGED, never support or failure of the printed claim. Every scope item a central
    claim names (methods, datasets) is covered by a check or omitted with a reason; a central
    claim still undecided after all checks gets one follow-up plan (plan:2, SH_MAX_FOLLOWUP_CHECKS).
    Superseded outcomes stay visible in the report. Every check names its basis (an audit of
    released result files, a recomputation from released predictions, a fresh run) and the
    report says which. A central claim has a `claim_type`; an engineering claim (a component
    integrates, trains) is tested by a compatibility test — a per-run condition, SH_REPLICATES
    runs, SH_COMPAT_BUDGET_S — that supports only compatibility, and a performance claim by its
    own performance test; the plan seal refuses either standing for the other.
24. **The requested experiment is not replaced, and completion is four things.** An empirical central
    claim (performance, value, engineering) is satisfied only by the experiment on what it names; a
    certificate, a simulation or a stand-in is a `role: supporting` check, reported beside the claim with
    its own outcome and never counted toward it. What is not run names its `blocker` (data, credentials,
    compute, protocol, other); `data` rests on a registry search the harness ran (and, if it returned
    candidates, why none is the dataset) or on a check that failed to acquire it. The ledger's
    `completion` and the report keep apart: the workflow reached a terminal state (any status), the
    requested experiment RAN, its protocol matched (no claim-changing deviation, data identity, scope), and
    what the evidence says. No sentence may imply a paper was reproduced because a report exists. Completion is
    derived from harness facts: only target checks of an experiment kind run an empirical claim's experiment; a
    scope item counts as run only when a target check covering it COMPLETED; a target sibling that did not run
    makes it RAN_PARTIAL (protocol not matched); the acquisition's own gaps (`execute.data_gaps`: a cut listing,
    a named file missing or rejected, an include matching nothing), an identity line that does not affirm the
    paper's description, or no identity line for acquired data make it RAN_WITH_CHANGES. The claim status follows:
    support stands only on RAN_AS_SPECIFIED (RAN_PARTIAL: PARTIAL_EVIDENCE; a changed reading or data:
    READING_CHANGED); a failure found on what ran stands. A supporting check speaks for no claim of any type. A
    support word in model prose is earned only by a check whose claim found support run as specified.
    `run.py status` recomputes from disk and says IN PROGRESS until review.md is newer than every outcome and seal.

25. **Classifications fail closed.** Every field that decides which rule applies is a closed vocabulary
    the seal requires, never a default: a central claim's `claim_type` and `scope`, a check's `role` and
    (except a CERTIFICATE) `criterion`, a RECONSTRUCTION's `test`, a RELEASED_DATA's `basis`, every omission's
    `blocker`. A follow-up round never retypes a claim. On the last attempt an invalid claim is sealed at its
    strictest reading and says so (`sealed_with_errors`): untyped = empirical, a link its kind cannot carry is
    removed, uncovered scope is recorded as omitted with blocker `unstated`, an unverifiable blocker is flagged
    `unverified`. Downstream, an unknown value reads as the strictest (`matches` not true = the data changed).
    A blocker rests on the harness's own records wherever it can: `data` on its registry searches,
    `credentials` on a hosted closed service or the registry record of a gated or private artifact (a public,
    ungated one is acquired), `compute` on a measured RESOURCE BLOCKER, the artifact's registry size against the
    measured host, or the paper's own statement of its compute; `protocol`/`other` are the planner's word,
    reported as such. Deviations are capped (16) by refusal, never by silent truncation.

## Dependency recovery (documented, isolated, recorded)

An environment is built from what the checkout declares: `uv.lock` -> `uv sync --frozen`
(path sources included; `uv run` then runs offline against `/env`), else `requirements*.txt`,
else the package. On failure `execute.recover` allows at most two rebuilds, each from an
empty env dir, network only during install: a build that needed a compiler -> the full image
of the same Python; every release of a dependency needing a newer Python -> that Python, only
if the project's declared range admits it. No requirement is edited, added or dropped. Every
attempt is an ExecutionRecord (`mode=install`, `recovery`); the outcome carries
`environment.recovery`; anything else stays BLOCKED with the pip/uv error. A RECONSTRUCTION
runs in the authors' environment when it builds (started as soon as a plan needs it), else on
the fixed baseline; a script may declare extra packages (`# REFEREE_PACKAGES:`, sha-covered) and
runs with them in a thin layer (`pip --target`, its own volume) over its read-only environment,
never a copy. A run given the GPU uses the full image of the same Python (GPU stacks compile
kernels at run time; a slim image has no compiler), fixed per check. Every long step is a detached, named container that
each `tasks` call polls (no host process must survive); venvs live in Docker named volumes. A
marker (env or data) whose volume Docker no longer holds is set aside and rebuilt, never mounted empty.
A reconstruction runs at least SH_REPLICATES (3) seeds; a script that cannot start goes back to
its author with the error (within SH_MAX_REVISIONS = 3).

## Commands (repo venv; on Windows set PYTHONUTF8=1)

```bash
python run.py tasks <paper.pdf|paper-id> --json [--wait 540]
python run.py seal <paper-id> <task-id> <answer.json>
python run.py try <paper-id> <gen-task-id> <script.py>      # draft run, masked, never evidence
python run.py discover <paper-id> "<dataset name>" [--registry zenodo|datacite|huggingface]   # public data only
python run.py discover <paper-id> --files <record url>     # the files of a cited repository record (names, sizes, md5)
python run.py reopen <paper-id> <check> <why>              # redo a check that ended WITHOUT a finding, after a harness fix
                                                           # (a RESOURCE BLOCKER reruns the same approved script; plan:2 re-plans; report rewrites it)
python run.py env <paper-id>                               # authors' env (the harness starts it)
python run.py status [<paper-id>]                          # read-only, recomputed: FINISHED only if review.md is newest
python run.py pack <out.zip> [<paper-id> ...] --clean      # zip artifacts, then delete clones/venvs
python tests/test_kernel.py
python tools/replay.py <projects dir> [--out f.json]              # re-decide recorded runs, no model or container
```

Gates: `SH_ALLOW_REPO_EXEC`, `SH_ALLOW_SCRIPT_EXEC`, `SH_ALLOW_INSTALL`, `SH_ALLOW_NETWORK`
(default on, for cloning and data downloads), `SH_ALLOW_SOURCE_SEARCH` (the authors' repository only),
`SH_ALLOW_DATA_SEARCH` (default on: registry search for public datasets; separate from every code gate;
data downloads need the network gate alone, a Hugging Face download also the install gate). Caps:
`SH_MAX_DISCOVERIES` (12 searches per paper),
`SH_MAX_CHECKS` (6), `SH_MAX_FOLLOWUP_CHECKS` (3), `SH_MAX_REVISIONS` (3), `SH_MAX_TRIES` (3),
`SH_MAX_DATA_GB` (20), `SH_FETCH_TIMEOUT_S` (the data cap at 1 MB/s), `SH_CHECK_BUDGET_S` (7200), `SH_COMPAT_BUDGET_S` (1800),
`SH_RUN_TIMEOUT_S` (3600; host awake time). Docker is the only
execution backend.

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
- Keep token spend visible (`python tools/wfusage.py <workflow transcript dir>`); check brief sizes
  (`ls -l projects/*/tasks`) before a run spends tokens on them.
- After a run: `run.py pack <zip> --clean` so clones, venvs and data do not accumulate.
- There is no adjudicated ground truth here: never claim precision/recall; a self-written
  test passing is not evidence that a scientific invariant holds.
- Finish end to end before refactoring: get a complete report for a paper; no new phases,
  checkpoints or test scaffolding unless a bug blocks progress.
- "Apply fixes only" means edit code and run `tests/test_kernel.py`; do not run papers.
- Docker is the only backend and other sessions share it: never restart Docker Desktop or
  remove containers/volumes you did not create.
