# REFEREE

A first-pass scientific-review copilot. Give it paper PDFs; it returns, per paper, a short
reviewer-facing `review.md` and a machine `ledger.json`: concerns grounded in the paper's own
words, the checks it ran (the authors' code, recomputations from released data, exact-arithmetic
certificates of theorems or proof steps, the paper's own arithmetic), what each established,
and what remains open. It never recommends accept or reject.

## Setup

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt   # Windows
# Docker Desktop (or any docker daemon) for every execution
```

## Run

In Claude Code, run the saved workflow:

```
Workflow({scriptPath: ".claude/workflows/referee.js", args: {
  papers: ["papers/a.pdf", "papers/b.pdf"], repo: "<abs path of this folder>",
  python: "../.venv/Scripts/python",
  env: "PYTHONUTF8=1 SH_PROJECTS_DIR=<runs>/projects SH_ALLOW_REPO_EXEC=1 SH_ALLOW_SCRIPT_EXEC=1 SH_ALLOW_INSTALL=1"}})
```

Then `python run.py pack <runs>.zip --clean` to zip the results and delete clones and venvs.

Per paper, `projects/<pid>/` holds `review.md`, `ledger.json`, `execution.jsonl` (every process
run), `source.json` (the paper's repo, commit, attribution), `checks/<id>/` (scripts, outcomes),
`sealed/` (validated answers) and `paper/` (parsed text, page images).

See `CLAUDE.md` for the architecture and the invariants.
