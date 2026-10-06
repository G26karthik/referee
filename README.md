# REFEREE

Autonomous scientific claim verification from research papers, with reproducible experiments and evidence-grounded
reviewer reports.

REFEREE reads a paper, lists its main claims, tests them, and writes a short reviewer page: for each claim, Verified or
Not verified with one reason, and what was run on what. It is an aid for a human reviewer, not an accept or reject
system.

## How it works

1. **Read.** The claims are extracted from the paper before any test is planned.
2. **Plan.** Each claim gets a test: a run of the authors' code, a recomputation from released files, a fresh run of
   the experiment, or exact-arithmetic cases for a theorem.
3. **Run.** Tests run in Docker. Every process is recorded (command, image, commit, hashes, output).
4. **Check.** Independent readers verify each test script, audit what it actually computes, and audit every failure
   against the paper's own words.
5. **Decide.** Code, not a model, computes every decision from the recorded runs. Model-written text is published only
   if its quotes, numbers and status words match the record.

Language-model workers do the reading and writing; each works on one task in isolation, and the harness validates
every answer before it is stored.

## Use

Requirements: Python 3.12 or newer, git, Docker, Claude Code. A CUDA GPU is used when present.

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt   # .venv/bin/python on Linux
python tests/test_kernel.py                                                      # the harness's own rules
cp referee.env.example referee.env                                               # this host's settings
```

Review papers (in Claude Code):

```
Workflow({scriptPath: ".claude/workflows/referee.js",
          args: {papers: ["papers/a.pdf"], repo: "<this folder>", python: "<venv python>"}})
```

Resume after any stop (closed session, reboot): run `/referee-resume`, or `python run.py resume` to see what would
continue. Reviews continue from their records on disk; finished runs are reused.

Other commands: `python run.py status` (state of each review), `python run.py pack <out.zip> --clean` (archive a run).

## Settings

`referee.env` holds budgets, gates and caps (see `referee.env.example`). Each review records the settings it ran under.
On a larger machine raise the time budgets; GPUs are detected and each run holds one.

## Output per paper (`projects/<paper-id>/`)

- `reviewer.md`: the reviewer page (two printed pages or fewer).
- `trace.md`: every claim and test with its numbers, bands, readings and limits.
- `ledger.json`, `execution.jsonl`, `checks/`: the machine record behind both.

## Limits

- No ground truth: a decision says what this run's tests showed.
- Theorem checks evaluate finite cases; they never prove a universal statement.
- A test that needs more time, memory or data than the host allows ends as a documented blocker, never shrunk to fit.
- Only public data is acquired.
