---
description: Continue every unfinished REFEREE review from where it stopped
---
Continue the REFEREE reviews that stopped (a closed session, a reboot, a crash). Nothing finished is redone: each review
continues from its records on disk, completed seeds are reused, and runs still going in Docker are picked up.

1. From this repository run `run.py resume` with `PYTHONUTF8=1` (Python: `../.venv/Scripts/python` on Windows,
   `../.venv/bin/python` elsewhere). It prints JSON and changes nothing.
2. If `integrity` lists papers, do not resume them. Tell the user which sealed records changed after sealing: the
   record must be restored, or its task reopened with `run.py reopen`.
3. If `resume` is empty, say so and stop.
4. Otherwise launch the workflow (this command is the user's request to run it):
   `Workflow({scriptPath: ".claude/workflows/referee.js", args: <the JSON's workflow_args>})`.
   Budgets, caps and gates come from `referee.env` (see `referee.env.example`).
5. Tell the user which papers were resumed; the workflow reports when each one finishes.
