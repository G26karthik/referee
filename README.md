# REFEREE

Autonomous scientific claim verification from research papers, with reproducible experiments and evidence-grounded
reviewer reports.

REFEREE reads a paper, extracts its main claims, plans tests for them, runs those tests in Docker, and writes a
two-page reviewer page per paper: each main claim with one decision (Verified, or Not verified with a specific
reason), what ran on what data, and what remains open. It is a first-pass aid for a human referee. It never
recommends accept or reject.

## How it works

Isolated model workers make the judgments that need reading: they extract the claims, plan the tests, write the test
scripts, verify them, audit what each finished script computes, and write the plain-language text. Deterministic code
does everything that needs trust:

- every quote a worker gives is re-found in the paper, and every code line it cites is re-found in the script;
- one execution gate decides what may run, and every process leaves an execution record;
- statuses and decisions are computed from the recorded runs (Student-t bands, exact sign and binomial tests, exact
  arithmetic for theorem checks), never taken from a model;
- model text is published only if each number is in the record or the paper and each status word is earned.

Pipeline per paper:

```
paper PDF -> claims + concern lenses -> critic -> plan -> per test: bind | write script -> verify -> run
          -> reconcile -> scope audit (what the test computed) -> failure audit -> follow-up plan -> report
          -> comparison with another reproduction record (optional, after the decisions are sealed)
```

`CLAUDE.md` holds the architecture and the 32 invariants the code enforces.

## What a decision means

Each main claim has a logical form, classified when the claims are extracted:

| Form | What can show it |
|---|---|
| universal (a bound, an implication) | never finite cases; tests can only find a counterexample that meets every premise |
| existential ("can be", "does not imply") | one valid example that meets every printed premise |
| universal over a parameter, with an example inside | examples show only the parameter values they cover |
| empirical | the experiment the claim names, over independent replicates |
| deterministic measurement (a parameter count) | one exact computation |
| engineering compatibility | a per-run condition (the component integrates and trains) |

A claim is **Verified** only when the requested test ran as specified, an independent audit found that the test's code
computes what the claim states, and the result supports the claim within the tested scope. Otherwise it is **Not
verified**, with one reason. Examples: contradicted; a proof step or the paper's construction fails as printed; a premise
that can never hold or that no tested case met; a changed protocol; a definition the readings disagree on; only part of
the scope tested; missing data or credentials; a time or memory limit of this run. Missing evidence is never read as
falsity.

## Setup

Requirements: Python 3.11+, Docker (Docker Desktop on Windows), and Claude Code for the model workers. A CUDA GPU is
used when present.

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt   # Windows
python tests/test_kernel.py                                                      # the harness's own rules
```

## Running

In Claude Code, run the saved workflow (each task goes to an isolated worker; the harness seals every answer):

```
Workflow({scriptPath: ".claude/workflows/referee.js", args: {
  papers: ["papers/a.pdf"], repo: "<absolute path of this folder>", python: ".venv/Scripts/python",
  env: "PYTHONUTF8=1 SH_PROJECTS_DIR=<runs>/projects SH_ALLOW_REPO_EXEC=1 SH_ALLOW_SCRIPT_EXEC=1 SH_ALLOW_INSTALL=1"}})
```

The same loop runs by hand: `python run.py tasks <paper> --json` lists the owed tasks, a worker writes each answer, and
`python run.py seal <paper-id> <task-id> <answer.json>` validates and stores it. `python run.py status` recomputes each
review's state from disk. After a review is sealed, `python run.py reference <paper-id> <file>` registers another
reproduction record (for example a Hugging Face logbook) for comparison; it never changes a decision.
`python run.py pack <out.zip> --clean` archives a run and deletes clones and environments.

## Outputs per paper (`projects/<paper-id>/`)

- `reviewer.md`: the two-page reviewer page, on the one-paper comparison template. `tools/reviewer_pdf.py` prints it
  and reports each paper's page count.
- `trace.md`: every claim (K1..) and test (C1..) by code, with every number, 95% band, reconciled count, reading,
  deviation, audit and blocker.
- `ledger.json` (the machine record), `review.md` (statuses), `execution.jsonl` (every process: argv, image, commit,
  script hash, times, exit code, output), `checks/<id>/` (scripts, outcomes, data manifests), `sealed/` (validated
  worker answers).

## Limits

- There is no ground truth. A decision says what this run's tests showed. The kernel tests check the harness's own
  rules, not whether any scientific claim is right, and REFEREE reports no precision or recall.
- Workers are language models. The harness re-finds their quotes and code lines and checks their numbers, but a worker
  can still misread a paper or write a weak test; independent verifiers and auditors reduce this, they do not remove it.
- One host runs every test. Each run and each check has a time limit, and a GPU run holds the GPU alone. Experiments that
  need more end as documented resource blockers, never as findings, and are never shrunk to fit.
- Only public data is acquired. A gated, deleted or moved dataset ends the test that needs it as a data blocker.
- Theorem checks evaluate finite exact cases. They can find a counterexample, show an existence statement by a valid
  example, or show a gap in a printed proof step. They never prove a universal statement.
- PDF text extraction can garble formulas; an arithmetic finding is confirmed against the page image first.
- Developed and run on Windows 11 with Docker Desktop; other platforms are untested.

## Layout

| Path | What it holds |
|---|---|
| `run.py` | the command line |
| `harness/` | the protocol (`tasks.py`), execution gate and runner (`execute.py`), data acquisition (`fetcher.py`, `discover.py`), decisions (`reconcile.py`, `report.py`, `reviewer.py`), and every worker prompt (`prompts/`) |
| `tests/test_kernel.py` | the trust-kernel tests |
| `tools/` | the PDF printer, a replay of recorded runs, token accounting |
| `.claude/` | the workflow and the worker definitions |
