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
  - Environment: {{environment}}. A package beyond it (e.g. a baseline's library) is declared on
    ONE line `# REFEREE_PACKAGES: name==version other` (plain pip requirements, at most 10): drafts
    and the evidence run then use a copy of the environment with them. Scratch files: /tmp only.
  - It accepts `--seed <int>`; the harness runs it once per seed 0..runs-1.
  - It prints `REFEREE_RESULT {"<name>": <number>, ...}` (JSON) lines — one per run, or one
    per replication the paper states when one run performs them all — carrying at least
    {{metric}}. Values are read by NAME only.
  - It never reads the paper's printed result to produce its own, and never compares with it:
    it computes; the harness compares.
  - Keep one run under about 30 minutes.

BINDINGS. For each required kind below, give `paper_quote` (the paper's own words, copied
verbatim from the parsed text — the harness re-finds it) and `impl_quote` (the literal line(s)
of YOUR script that realize it — the harness checks they occur in the script). Required:
{{required}}.
If a required ingredient cannot be written without inventing a detail the paper omits, say so
in `notes` and leave its quotes empty: an honest refusal is a correct outcome; a fabricated
binding is not.

DEVIATIONS. Every departure from the paper's printed text goes in `deviations`, each as
{"printed": the paper's words verbatim ("" if the paper is silent), "used": what the script
does instead, "why": ...}: a changed index range or convention (0- vs 1-based), a hypothesis
added, dropped or strengthened, a substituted function or constant, a detail the paper leaves
open that you had to fix. An unlisted departure gets the check rejected. For a CERTIFICATE
whose deviation changes the printed statement or step itself (not merely fills a gap), ALSO
evaluate the text exactly as printed on the same instance and add `"literal_violated": 1 or 0`
to the result line (1 if as printed it fails or is undefined, e.g. an index out of range):
the harness records both readings side by side and never picks one.

REPLICATION. If the paper states how many seeds/runs/instances this used, set `runs` to it and
copy the sentence into `runs_quote`; never fewer than the paper used. A RECONSTRUCTION is run at least 3 times (seeds
0, 1, 2): each seed must be an independent replicate (all randomness drawn from `--seed`).

TESTING. You may run a draft (at most {{max_tries}} times; result lines are masked; drafts never
count as evidence): write the script to a file, then run
    {{try_cmd}} <path-to-your-script.py>
{{revision}}
Write ONLY this JSON to the output path you were given:
{"script": "the full Python source as one JSON string", "runs": 1, "runs_quote": "",
 "metric": "the compared output's name (not for CERTIFICATE or a relation target)",
 "outputs": ["every name the result line carries"],
 "deviations": [],
 "bindings": [{"kind": "...", "paper_quote": "...", "impl_quote": "..."}],
 "checked_statement": "conclusion|proof_step (CERTIFICATE only)", "notes": ""}
