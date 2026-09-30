{{security}}

You are writing ONE check script for a published paper's review. An independent verifier will
read your script against the paper, and only then will the harness run it in a sandbox
(no network; the authors' checkout, if any, mounted read-only as the working directory).

Paper: {{title}}   (full text under READ; page PNGs in {{pages_dir}})
Check {{check_id}} — {{kind}}
The claim it bears on: {{claim}}
{{spec}}

{{contract}}

THE HOST (measured): {{host}}

THE SCRIPT CONTRACT
  - Environment: {{environment}}. A package beyond it (e.g. a baseline's library) is declared on
    ONE line `# REFEREE_PACKAGES: name==version other` (plain pip requirements, at most 10): drafts
    and the evidence run then use a copy of the environment with them. Scratch files: /tmp only.
    Data the harness acquired for this check is read-only under /work/data (see the manifest
    above, if any); open files there by path.
  - It accepts `--seed <int>`; the harness runs it once per seed 0..runs-1.
  - It prints `REFEREE_RESULT {"<name>": <number>, ...}` (JSON) lines to stdout carrying at least
    {{metric}}. Values are read by NAME only. Where the claim spans several units (datasets,
    settings, panels), print one result line PER UNIT with `"stage": "<unit name>"`, as soon as that
    unit finishes (flush): the harness decides each stage over the seeds, and keeps a finished
    stage even if a later one fails.
  - At the start, declare the units that will each print a result line, once:
    `REFEREE_PROGRESS {"units": ["<unit>", ...]}` — a declared unit that prints no result counts as
    NOT COMPLETED. Before each step, print `REFEREE_PROGRESS {"stage": "<step>", "t": <seconds so
    far>}` (either stream): it shows how far a failed run got and times the pilot.
  - DATA IDENTITY: for each dataset you load, print `REFEREE_DATA {"dataset": "<name>", "source":
    "<file path>", "observed": {...counts, shape, distinct labels...}, "expected": {...what the
    paper prints, with its quote...}, "matches": true|false}`. A mismatch is a finding: report it
    and CONTINUE with the other units — never exit on it (that would discard what completed).
  - It never reads the paper's printed result to produce its own, and never compares with it:
    it computes; the harness compares.
  - Where the checkout ships the code that computed the printed quantity (an analysis script, a
    metrics module), compute it that way: import or follow that code and name its path in a
    binding. Another estimator, probability or pooling is a claim-changing deviation.
  - Keep one run within the host's per-run limit. The harness times the first run as a pilot and
    stops with a documented blocker if the stated run count cannot finish; a long run may save
    progress under /work/ckpt (a per-seed scratch volume) and resume from it after a restart. Use
    the GPU when the host has one and the method trains a network (e.g. pass the authors' own
    GPU switch); say which device ran: `REFEREE_PROGRESS {"note": "device <name>"}`.
  - For a claim over several units, the result lines carry `"stage": "<unit>"` (the declared name).

BINDINGS. For each required kind below, give `paper_quote` (the paper's own words, copied
verbatim from the parsed text — the harness re-finds it) and `impl_quote` (the literal line(s)
of YOUR script that realize it — the harness checks they occur in the script). Required:
{{required}}.
If a required ingredient cannot be written without inventing a detail the paper omits, say so
in `notes` and leave its quotes empty: an honest refusal is a correct outcome; a fabricated
binding is not.

DEVIATIONS. Every departure from the paper's printed text goes in `deviations`, each as
{"printed": the paper's words verbatim ("" if the paper is silent), "used": what the script
does instead, "why": ..., "changes_claim": true|false}: a changed index range or convention
(0- vs 1-based), a hypothesis added, dropped or strengthened, a substituted function or
constant, a different dataset version, split, training subset, tuning, baseline implementation,
metric or aggregation, a detail the paper leaves open that you had to fix. `changes_claim` is
true when the deviation changes WHAT IS COMPARED or what the claim says (a premise, the
conclusion, an index, a definition, the data or split the result is on, untuned methods where the
paper tuned them, a baseline rebuilt differently, a different aggregation) and false when it only
fixes a detail the claim leaves open (a distribution, a threshold, a seed) — those are protocol
choices REFEREE supplied and are reported as such. An unlisted departure gets the check rejected;
a result obtained under claim-changing deviations is reported as being about the changed claim.
For a CERTIFICATE with a claim-changing deviation, ALSO evaluate the text exactly as printed on
the same instance and add `"literal": "holds" | "fails" | "undefined" | "premise_not_met"` to the
result line (fails = its printed premises hold and its printed conclusion fails; undefined = the
printed text is not well-defined there): the harness records both readings and never picks one.

REPLICATION. If the paper states how many seeds/runs/instances this used, set `runs` to it and
copy the sentence into `runs_quote`; never fewer than the paper used. A RECONSTRUCTION is run at least 3 times (seeds
0, 1, 2): each seed must be an independent replicate (all randomness drawn from `--seed`).

TESTING. You may run a draft (at most {{max_tries}} times; result lines are masked; drafts never
count as evidence): write the script to a file, then run
    {{try_cmd}} <path-to-your-script.py>
Whatever you do, the harness itself runs your final script once (seed 0, results masked) before
any verifier sees it; if that run fails, the script comes back to you with the error.
{{revision}}
Write ONLY this JSON to the output path you were given:
{"script": "the full Python source as one JSON string", "runs": 1, "runs_quote": "",
 "metric": "the compared output's name (not for CERTIFICATE or a relation target)",
 "outputs": ["every name the result line carries"],
 "deviations": [],
 "bindings": [{"kind": "...", "paper_quote": "...", "impl_quote": "..."}],
 "checked_statement": "conclusion|proof_step (CERTIFICATE only)",
 "premise_argument": "CERTIFICATE only: a general argument, if you have one, that a printed premise can never hold (the verifier checks it; it is reported as your reasoning, not as a result)",
 "notes": ""}
