# single-harness — AI first-round peer reviewer

Takes a submitted ML paper (PDF) and produces a 1–2 page structured evaluation report
with a Red / Yellow / Green verdict and a verbatim evidence pointer behind every finding.

**Fully local. No API keys, no cloud account, no network calls.** Three of the four
stages are plain deterministic Python. The fourth needs judgement, and the judge is the
Claude Code session you are already sitting in.

## Pipeline

```
S1  ingest_paper       PDF → PaperDoc: sections, addressable tables, reported numbers
S2  audit_paper        writes one prompt per lens; the DRIVER performs the audits
S3  run_probe          conditional local GPU reproduction across seeds → noise floor
S4  synthesize_report  verify → rank → reports/<paper_id>.md
```

All four are driven by one command: `python run.py review --paper <pdf>`.

### The normal way to run it

```bash
python run.py review --paper papers/paper4_snri_nullresult.pdf
```

That runs every deterministic stage and stops **once**, when the four lens results are
missing — printing the prompt paths and exiting `2`. Perform the audits, write
`audit/<lens>.json`, then re-run to finish:

```bash
python run.py review --paper paper4-snri-nullresult      # exit 0, prints the report
```

Exit codes: `0` complete · `2` waiting on audits · `1` error. Every stage is idempotent,
so re-running only does the work that had not finished. `--force-probe` runs the GPU probe
even when nothing is flagged settleable; `--skip-probe` never runs it.

That pause is the one manual step and it is not an oversight: S2 needs judgement, and the
judge is the Claude Code session driving the CLI — there is no subprocess to delegate it
to. `CLAUDE.md` tells Claude Code how to handle the pause automatically.

### Individual stages

```bash
python run.py node ingest_paper      --paper papers/paper4_snri_nullresult.pdf
python run.py node audit_paper       --paper paper4-snri-nullresult
python run.py node run_probe         --paper paper4-snri-nullresult
python run.py node synthesize_report --paper paper4-snri-nullresult
```

`--paper` is a **PDF path** for `ingest_paper` and the **case id** everywhere else. The
case id is the PDF filename slugified; `ingest_paper` prints it back.

## The four lenses

| Lens | Hunts |
|---|---|
| `overclaim` | unsound premises; gains smaller than the paper's own seed noise; undertuned baselines; undeclared prior art |
| `protocol` | label leakage; fit-before-split; tuning or early-stopping on test; grouped/temporal splits done at row level; metric gaming |
| `confound` | variables that moved together with the mechanism; the missing single-variable ablation; unequal tuning budget |
| `contradiction` | narrative vs table cell; abstract vs conclusion vs limitations; baselines that appear in one table and vanish from another |

Each first runs a **first-principles reality check** — why this method at all, what is the
obvious baseline they avoided, why did they not try it — because that is where human
review fails far more often than on subtle statistics.

Run each lens in a **separate turn**. Four independent readings are four pieces of
evidence; one context that remembers the previous three is one reading echoed four times.

## Why you can trust the output

**Provenance is structural, not requested.** S1 is deterministic: a reported number *is*
a table cell or *is* a whole sentence lifted verbatim. There is no extraction step that
could round, re-unit, or invent one.

**The harness does not trust the driver either.** Every finding you write is re-verified
at report time. If `evidence_quote` is not actually in the paper — or cites `T2:r3:c4`
but does not match what that cell says — the finding is **dropped** and the report says
how many were dropped. You are a language model; the harness checks.

**No model decides the verdict.** Ranking and RED/YELLOW/GREEN are a lexicographic sort
and a threshold table in `stages/report.py`. `render_eval_report` is a pure function that
only copies from artifacts, so nothing can be hallucinated at render time.

RED means a central claim does not stand: a FATAL, or three MAJORs from one lens, or ten
overall. It is deliberately hard to reach — a four-lens panel returns roughly two MAJORs
per lens on a *good* paper, and a rule that called that RED would reject everything.

## S3 — the local probe

`harness/local_exec.py` writes `runs/<paper_id>/probe.py`, runs it once per (arm, seed)
via `subprocess.run`, parses a fixed stdout contract, and writes `probe_results.json`
**by value** — no git branch, no commit, no push.

```
SH_DEVICE <cuda|mps|cpu>
SH_METRIC arm=<name> seed=<int> value=<float>
```

Any script honouring those two lines plugs in. Put a faithful reproduction of the paper's
setup in `runs/<paper_id>/spec.json` (`{"script": "..."}`) and it is used verbatim.

With no script supplied the default template runs **identical arms**, which measures this
machine's seed-noise floor — the denominator every "is this gain real?" question divides
by. The report labels that case as calibration, not as a reproduction of the paper.

A separate process per seed is deliberate: re-seeding in-process leaves CUDA state and
autotune caches warm, which under-reports the true seed spread — the one number this
stage exists to get right.

Detectability test: `is_overclaimed = |measured_delta| < 2 × measured_std`, where the std
is the spread of *per-seed differences* when both arms share seeds (shared variance
cancels, so a real effect is not hidden), falling back to the wider arm spread otherwise.
A std of exactly zero yields `degenerate`, never a verdict — you cannot test
detectability with no noise estimate.

## Setup

```bash
uv pip install --python .venv/Scripts/python.exe -r single-harness/requirements.txt
```

`pymupdf` + `pdfplumber` are required (S1). `numpy` + `scikit-learn` are required by the
default probe template. `torch` is **optional** — install it to use a local GPU:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu124
```

The harness never imports torch itself; only the probe subprocess does, and it falls back
to sklearn on CPU when torch is absent.

On Windows set `PYTHONUTF8=1`. Paper text is full of em dashes, Greek letters and math;
the default `cp1252` console encoding mangles them.

| Variable | Default | Effect |
|---|---|---|
| `SH_AUDIT_BUDGET_CHARS` | `70000` | body text embedded in each lens prompt |
| `SH_SEEDS` | `5` | probe seeds (clamped to 3–5) |
| `SH_PROBE_TIMEOUT` | `1800` | seconds per (arm, seed) run |
| `SH_PYTHON` | current interpreter | interpreter used for probe subprocesses |

## Layout

```
run.py                  CLI: node / list / status
dashboard.py            stdlib-only live viewer over research_log.jsonl
harness/
  pdf.py                PDF → sections, addressable tables, reported numbers (pure)
  local_exec.py         probe generation, seed loop, stdout parsing, statistics
  artifacts.py          typed Pydantic artifacts — the only thing crossing a boundary
  config.py             paths + local execution settings. No credentials.
  state.py              case directories + the append-only research log
  nodes.py              the node surface run.py drives
  stages/               ingest · audit · probe · report
  prompts/              ingest · audit — the reasoning half, kept out of the code
projects/<paper_id>/
  paper/doc.json  audit/prompts/<lens>.md  audit/<lens>.json
  runs/<paper_id>/probe.py + probe_results.json  reports/<paper_id>.md
papers/                 sample PDFs to run against
```

## Self-checks

```bash
python -m pytest tests -q          # 94 tests, no credentials, no network
python -m harness.pdf papers/paper1_grokking.pdf    # parser on a real PDF
python -m harness.local_exec                        # probe runner + statistics
python -m harness.stages.report                     # ranking + verdict + renderer
```
