# REFEREE

Autonomous scientific claim verification from research papers, with reproducible experiments and evidence-grounded
reviewer reports.

REFEREE reads a paper, lists its main claims, tests them, and writes a short reviewer page: for each claim, Verified or
Not verified with a reason. It assists a human reviewer; it does not accept or reject papers.

## How it works

1. The paper's main claims are extracted.
2. Each claim gets a test, which runs in Docker and is recorded.
3. Decisions are computed from the recorded results, and the page reports them with their limits.

## Use

Requirements: Python 3.12 or newer, git, Docker, Claude Code. A CUDA GPU is used when present.

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt   # .venv/bin/python on Linux
python tests/test_kernel.py
cp referee.env.example referee.env                                               # this machine's settings
```

Review papers in Claude Code:

```
Workflow({scriptPath: ".claude/workflows/referee.js",
          args: {papers: ["papers/a.pdf"], repo: "<this folder>", python: "<venv python>"}})
```

Continue after any stop with `/referee-resume` (`python run.py resume` lists what would continue).
`python run.py status` shows the state of each review.

## Output

Per paper, in `projects/<paper-id>/`: `reviewer.md` (the reviewer page), `trace.md` (every test and number), and the
machine records behind them.

## Limits

- A decision says what this run's tests showed; there is no ground truth.
- Theorem checks evaluate finite cases and never prove a universal statement.
- A test that needs more time, memory or data than the machine allows ends as a documented blocker.
- Only public data is used.
