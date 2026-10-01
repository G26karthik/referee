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
  - ACQUIRED DATA IS THE EXPERIMENT'S DATA. If the manifest above lists acquired files, the script reads
    them from /work/data (the harness refuses a script that never does). A simulation, a smaller stand-in
    or a different dataset in their place is a different experiment: never write one to make a check
    "work"; if the files are not what the paper describes, print the REFEREE_DATA mismatch and continue.
  - REPLICATES ARE DIFFERENT RUNS. Every random generator (sampling, splits, simulation, initialisation)
    is seeded from `--seed`; the harness refuses a stochastic script whose seed reaches none. A stochastic
    script adds `"data_fingerprint": "<sha256 of the random draws THIS stage consumed>"` to EVERY result line
    (the generated data, the sampled indices or split, the initial weights — whatever the seed changed in
    that stage). Equal summary numbers (a recovery ratio of 1.0, zero false alarms) are still different runs
    if the draws differ; identical lines with identical fingerprints are one run repeated, never replicates.
  - A PROPORTION OF COUNTED TRIALS (a false-alarm rate over 400 simulated trials, a coverage over 100 test
    points, an error rate over n items): also print `"binomial": {"<output>": <number of independent trials>}`
    on the result line, so the harness decides it with an exact binomial interval — zero events included —
    instead of a standard error that is zero. The trials must be independent draws.
  - PAPER VS CODE. Where the checkout ships code that computes the compared quantity (an
    analysis script, a metrics module), or the acquired record does (files listed under `record_src` in
    the manifest: its code, notebooks or README, kept as text you may Read but never run; a precomputed
    column; rows the paper says it discarded that the files still hold), read it and compare it with the
    paper's definition (the estimator, which items, positions or candidates are pooled, the selection,
    the aggregation). A record's file is cited as `"source": "record:<n>/<path>"`.
    If the spec lists READINGS, or you find that they differ, compute EVERY reading in this one
    script, on the same loaded data and the same cohort (the same models, items, rows), and print
    one result line per reading (and stage) carrying `"reading": "<name>"` and `"cohort": [the ids
    of the items it was computed over]`. List a reading you found yourself in `readings`
    ({"name", "source": "paper" or the tracked file path, "quote": the paper's words verbatim or
    the literal code lines}). Never choose one; which reading matches the printed number is no
    reason to prefer it.
  - RESULT SCHEMA. The harness runs your final script once (seed 0) and returns it to you if a
    declared unit prints no result line with that exact `stage`, if a result line carries a stage
    name you did not declare (or none, when you declared units), if a declared reading is missing
    in a stage, or if the readings' cohorts differ. Declare units only for real units (datasets,
    settings), never the metric's name.
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
binding is not. The one ingredient an experiment may not HAVE is `training`: a simulation, a
sequential test, an optimisation or an exact computation trains nothing. Then give it as
{"kind": "training", "not_applicable": "<why nothing is trained or fitted here>"} (at least a sentence; the
verifier checks it) — do not refuse an experiment whose other ingredients are all bound, and do not
invent a training quote.

DEVIATIONS (at most 16; merge choices of one kind into one entry — more is refused, never cut). Every
departure from the paper's printed text goes in `deviations`, each as
{"printed": the paper's words verbatim ("" if the paper is silent), "used": what the script
does instead, "why": ..., "changes_claim": true|false}: a changed index range or convention
(0- vs 1-based), a hypothesis added, dropped or strengthened, a substituted function or
constant, a different dataset version, split, training subset, tuning, baseline implementation,
metric or aggregation, a MODEL other than the paper's (another architecture, size, depth or checkpoint
— a smaller network or LLM in its place changes what is compared: `changes_claim` true), a detail the
paper leaves open that you had to fix. `changes_claim` is
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

OPEN DETAILS ARE DECLARED BEFORE THE VERIFIER FINDS THEM. Every choice the paper leaves open ("in some fixed
order", a radius, a bounding box, a filter, an order of extension, a tie-break) and every place the printed text
is inconsistent with itself (a cell size that contradicts the grid size; a count that the filter must reproduce)
is a `deviation` with the paper's words, what you used and `changes_claim`. Run a draft and compare what your
REFEREE_DATA reports (counts, sizes) with the numbers the paper prints before you submit: a cohort twice the
printed size is a wrong filter, not a finding. Each round the verifier finds one more undeclared choice costs
one of your few revisions.

REPLICATION. If the paper states how many seeds/runs/instances this used, set `runs` to it and
copy the sentence into `runs_quote`; never fewer than the paper used. A RECONSTRUCTION is run at least 3 times (seeds
0, 1, 2): each seed must be an independent replicate (all randomness drawn from `--seed`). Set
`stochastic` honestly: true when `--seed` drives randomness (training, sampling, simulation,
data splits), false when the computation is deterministic (fixed data through a fixed pipeline):
the harness then runs it twice and requires identical results, and identical reruns are one
measurement, never replicates. A recomputation from released files runs once. A COMPATIBILITY
test keeps the planner's condition and output definitions exactly: one stated configuration,
the component swapped in, no baselines to beat, no tuning sweep, no grid of settings; the
harness sets its run count and its 30-minute budget.

TESTING. You may run a draft (at most {{max_tries}} times; result lines are masked; drafts never
count as evidence): write the script to a file, then run
    {{try_cmd}} <path-to-your-script.py>
Whatever you do, the harness itself runs your final script once (seed 0, results masked) before
any verifier sees it; if that run fails, the script comes back to you with the error.
{{revision}}
Write ONLY this JSON to the output path you were given:
{"script": "the full Python source as one JSON string", "runs": 1, "runs_quote": "",
 "stochastic": true, "readings": [],
 "metric": "the compared output's name (not for CERTIFICATE or a relation target)",
 "outputs": ["every name the result line carries"],
 "deviations": [],
 "bindings": [{"kind": "...", "paper_quote": "...", "impl_quote": "..."}],
 "checked_statement": "conclusion|proof_step (CERTIFICATE only)",
 "premise_argument": "CERTIFICATE only: a general argument, if you have one, that a printed premise can never hold (the verifier checks it; it is reported as your reasoning, not as a result)",
 "notes": ""}
