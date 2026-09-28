{{security}}

You are writing ONE check script for a published paper's review. An independent verifier will
read your script against the paper, and only then will the harness run it in a sandbox
(no network; the authors' checkout, if any, mounted read-only as the working directory).

Paper: {{title}}   (full text under READ; page PNGs in {{pages_dir}})
Check {{check_id}} — {{kind}}
The claim it bears on: {{claim}}
{{spec}}

{{contract}}

THE SCRIPT CONTRACT
  - Python 3.11, standard library plus only: numpy, scipy, pandas, scikit-learn, sympy.
  - It accepts `--seed <int>`; the harness runs it once per seed 0..runs-1.
  - Each run prints exactly one line: `REFEREE_RESULT {"<name>": <number>, ...}` (JSON),
    carrying at least the output named in the check ({{metric}}).
  - It never reads the paper's printed result to produce its own: it computes.
  - Keep one run under about 30 minutes.

BINDINGS. For each required kind below, give `paper_quote` (the paper's own words, copied
verbatim from the parsed text — the harness re-finds it) and `impl_quote` (the literal line(s)
of YOUR script that realize it — the harness checks they occur in the script). Required:
{{required}}.
If a required ingredient cannot be written without inventing a detail the paper omits, say so
in `notes` and leave its quotes empty: an honest refusal is a correct outcome; a fabricated
binding is not.

REPLICATION. If the paper states how many seeds/runs/instances this used, set `runs` to it and
copy the sentence into `runs_quote`; never fewer than the paper used.

TESTING. You may run a draft (at most {{max_tries}} times; result lines are masked; drafts never
count as evidence): write the script to a file, then run
    {{try_cmd}} <path-to-your-script.py>
{{revision}}
Write ONLY this JSON to the output path you were given:
{"script": "the full Python source as one JSON string", "runs": 1, "runs_quote": "",
 "outputs": ["{{metric}}"],
 "bindings": [{"kind": "...", "paper_quote": "...", "impl_quote": "..."}],
 "checked_statement": "conclusion|proof_step (CERTIFICATE only)", "notes": ""}
