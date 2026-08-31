# single-harness — architecture

An automated first-round peer reviewer for ML papers, running entirely on one machine.
In: one PDF. Out: a 1–2 page evaluation report with a Red / Yellow / Green verdict and a
verbatim evidence pointer behind every finding.

There is no API client, no cloud SDK and no network call anywhere in the harness. Three
of the four stages are deterministic Python. The fourth needs judgement, and the judge is
the Claude Code session driving the CLI.

---

## 1. The trust model

The harness is built around one assumption: **the thing supplying judgement is a language
model, and a language model will occasionally state something the paper does not say.**
Every design decision below follows from taking that seriously rather than politely.

**Layer 1 — the PDF is data, never instruction.** Each lens prompt opens with an explicit
notice that the paper text is untrusted and that anything in it resembling a command is to
be ignored. The paper is embedded in the prompt; the driver is never asked to go and fetch
it, so a hostile PDF has no action to induce.

**Layer 2 — provenance is structural where it can be.** S1 is deterministic. A reported
number *is* a table cell, addressed `T<t>:r<r>:c<c>`, or *is* a whole sentence lifted
verbatim from the prose. There is no extraction step in which a number could be rounded,
re-unit-ed or invented, so there is nothing to verify after the fact.

**Layer 3 — findings are re-verified where it cannot.** S2's output comes from a model, so
`audit.load_reports` re-checks every finding before it can reach the report: the
`evidence_quote` must actually appear in the parsed paper, and if the finding cites a cell
it must match *that cell's* contents. Anything else is dropped and counted, and the report
prints the count. The driver's word is not evidence.

**Layer 4 — no model decides the verdict.** Severity ranking and RED/YELLOW/GREEN are a
lexicographic sort and a threshold table. `render_eval_report` is a pure function that only
copies from artifacts. Nothing is generated at render time, so nothing can be hallucinated
at render time.

---

## 2. The entrypoint and the one unavoidable pause

`python run.py review --paper <pdf>` runs the whole pipeline. It stops exactly once —
when the four lens results are missing — prints the prompt paths and exits `2`. The driver
performs the audits, writes `audit/<lens>.json`, and re-runs; every stage is idempotent, so
the second pass only does what the first could not.

That pause is not an oversight and it is not automatable away. S2 needs judgement, and the
judge is the Claude Code session invoking the CLI — not a subprocess it can spawn. The
honest design surfaces the boundary rather than hiding it behind a call that would need a
credential the local build deliberately does not have. `CLAUDE.md` is what makes the pause
invisible in practice: it tells Claude Code to run the command, handle the pause, and print
the report.

---

## 3. End-to-end flow

```
                    papers/<paper>.pdf
                            │
         ┌──────────────────▼──────────────────┐
   S1    │ ingest_paper                        │  stages/ingest.py
         │  pdf.py: pages → sections → tables  │  DETERMINISTIC
         │  numbers = cells + prose sentences  │  no model involved
         └──────────────────┬──────────────────┘
                            │  paper/doc.json  (PaperDoc)
         ┌──────────────────▼──────────────────┐
   S2    │ audit_paper                         │  stages/audit.py
         │  writes audit/prompts/<lens>.md ×4  │  writes prompts; runs nothing
         └──────────────────┬──────────────────┘
                            │
                   ┌────────▼────────┐
                   │  THE DRIVER     │  the Claude Code session,
                   │  one lens per   │  one lens per turn
                   │  turn           │
                   └────────┬────────┘
                            │  audit/<lens>.json  (LensReport ×4)
         ┌──────────────────▼──────────────────┐
   S3    │ run_probe                           │  stages/probe.py → local_exec.py
         │  probe.py, one subprocess per       │  local CUDA / MPS / CPU
         │  (arm, seed) → noise floor          │  results BY VALUE
         └──────────────────┬──────────────────┘
                            │  runs/<pid>/probe_results.json
         ┌──────────────────▼──────────────────┐
   S4    │ synthesize_report                   │  stages/report.py
         │  re-verify → rank → threshold       │  pure functions
         └──────────────────┬──────────────────┘
                            │
              reports/<paper_id>.md + .json
```

---

## 4. Ingestion (S1) — deterministic

`harness/pdf.py` is the only module that opens a PDF. Two extractors, because no single
one does both jobs well: **PyMuPDF** for page text in reading order, **pdfplumber** for
table structure with real cell boundaries.

**Sections.** Every line is tested against a heading heuristic (numbered, lettered, or
conventionally named). Text before the first heading survives as a front-matter section,
so no content is dropped. *Ceiling:* a paper with unnumbered small-caps headings collapses
to one large section — the auditors still get the full text, only the labels are lost.

**Tables.** Ruled tables come straight from pdfplumber. Most ML papers ship `booktabs`
tables with no vertical rules at all — all four sample papers here returned **zero** tables
from the line-based extractor — so a page where that finds nothing falls back to
word-geometry recovery: cluster words into rows by `top`; find the run of rows carrying
wide inter-word gaps after a `Table N` caption (prose and wrapped captions have none); snap
ragged rows onto column anchors derived from *all* rows at once, since a header may run its
words together where a data row splits cleanly.

**Addressing is the product.** A finding that says *"T0:r1:c1 reads 12.196 ± 0.207 but the
abstract claims a ≥20% gain"* is checkable in seconds. *"The table disagrees"* is not.

**Numbers.** `table_numbers` emits every numeric cell with its arm (column 0) and metric
(header row). `prose_numbers` emits sentences carrying a magnitude *and* comparative
language — `improves by 4.2%` is a claim, `20 tasks` is a count.

---

## 5. The audit panel (S2)

Four lenses, defined in `prompts/audit.py`: `overclaim`, `protocol`, `confound`,
`contradiction`. Each prompt carries the same four blocks — the first-principles reality
check, the lens focus, the severity calibration ladder, and the provenance rule — plus the
untrusted-data notice.

`run_audit` renders the prompts and stops. The driver performs each audit and writes
`audit/<lens>.json`.

**Independence is now a discipline, not a guarantee.** The predecessor ran each lens in its
own SDK session, so cross-contamination was physically impossible. A single Claude Code
session can remember the previous lens, so the prompt says to run each in a separate turn
and not to let one lens's findings influence another. This is the one property that got
*weaker* in the move to local execution, and it is stated plainly rather than papered over.

**Calibration is load-bearing.** A four-lens panel returns roughly two MAJOR findings per
lens on a *good* paper. The ladder tells each lens that FATAL means the central claim does
not stand, that a rigorous null result is legitimate work, and not to pad.

---

## 6. The local probe (S3)

**S3 is conditional.** It fires only when some lens marked a finding
`verifiable_by_experiment`; otherwise `review` skips it and records why. Firing a GPU probe
with nothing to test would burn minutes measuring a noise floor nobody asked for and — worse
— put a "Measured reproduction" section in the report that reads like evidence about the
paper when it is nothing of the kind. `--force-probe` overrides, and the report then labels
the result as calibration.

`harness/local_exec.py` writes `runs/<paper_id>/probe.py`, runs it once per (arm, seed) via
`subprocess.run`, parses a two-line stdout contract, and writes `probe_results.json` **by
value** — no git branch, no commit, no push, nothing to have write access to.

```
SH_DEVICE <cuda|mps|cpu>
SH_METRIC arm=<name> seed=<int> value=<float>
```

Any script honouring those lines plugs in; a faithful reproduction goes in
`runs/<paper_id>/spec.json`. With no script supplied the default template runs **identical
arms**, measuring this machine's seed-noise floor — the denominator every "is this gain
real?" question divides by. The report labels that case as calibration rather than dressing
it up as evidence about the paper.

**One subprocess per seed** is deliberate. A seed is only independent if CUDA state, cuDNN
autotune caches and library RNG all start fresh; re-seeding in-process leaves enough shared
state that the measured spread under-reports the true seed noise, which is the one number
this stage exists to get right.

**The statistics.** Noise is the spread of *per-seed differences* when both arms share
seeds — shared variance cancels, so the band does not inflate and hide a real effect —
falling back to the wider arm spread otherwise. Detectability is
`|measured_delta| < 2 × measured_std`; magnitude, because an effect in the wrong direction
is not evidence of a gain either.

**Degenerate cases return no verdict.** A paired spread of exactly zero means the seed is
not perturbing the arms independently, not that the measurement is infinitely precise;
dividing by it would make any nonzero delta look overwhelming. Zero paired variance falls
back to the arm spread, and a zero noise estimate overall yields `degenerate`. A probe that
could not run yields `failed` with `is_overclaimed = None` — a broken probe never accuses
anyone. *(The first two of those shipped broken and were caught by the module self-check.)*

---

## 7. Ranking and verdict (S4)

```python
finding_key(f) = (severity, evidence_strength, verifiable, lens_tiebreak, finding_id)
```

sorted `reverse=True`. Evidence strength is 2 for a cited cell, 1 for a page, 0 for
nothing. The id makes the sort total, so the same findings always produce the same order.

| Verdict | Rule |
|---|---|
| RED | ≥1 FATAL, or ≥3 MAJOR **from one lens**, or ≥10 MAJOR overall |
| YELLOW | ≥1 MAJOR, or ≥4 MINOR |
| GREEN | otherwise |

RED means a central claim does not stand. Three MAJORs from a *single* lens is a coherent
pattern — one dimension failing repeatedly — where the same count spread across four lenses
is just what a thorough panel produces. An earlier "3 MAJOR anywhere" rule returned RED on a
pre-registered null result with positive controls and a matched-budget design, which is
exactly the reflexive rejection the lens calibration exists to prevent.

Truncation for length is always **stated**: a report that silently drops findings reads as
a clean bill of health for the ones it dropped.

---

## 8. Directory layout

```
run.py                     CLI: node / list / status
dashboard.py               stdlib-only viewer over research_log.jsonl
harness/
  pdf.py                   PDF → sections, addressable tables, numbers (pure)
  local_exec.py            probe generation, seed loop, stdout parsing, statistics
  artifacts.py             PaperDoc, LensReport, ProbeResult, EvalReport, Finding…
  config.py                paths + local execution settings. No credentials.
  state.py                 case directories, append-only research_log.jsonl
  nodes.py                 the node surface run.py drives
  stages/  ingest · audit · probe · report
  prompts/ ingest · audit
projects/<paper_id>/
  project.json  research_log.jsonl
  paper/doc.json
  audit/prompts/<lens>.md   audit/<lens>.json
  runs/<paper_id>/probe.py  runs/<paper_id>/probe_results.json
  reports/<paper_id>.md + .json
```

---

## 9. Conventions

- **Pydantic `extra="allow"` everywhere.** A stage can add a field without breaking
  downstream, and a partial response degrades instead of raising.
- **Explicit `encoding="utf-8"` on every file operation.** Paper text is full of em
  dashes, Greek letters and math; Windows defaults to `cp1252` and will mangle them. Set
  `PYTHONUTF8=1` when running.
- **Every node is idempotent.** Checks disk first. `audit_paper` reports which lenses
  already have results so a single failed lens costs one lens, not four.
- **A silent stage yields an empty artifact, never a crash**, and the report names which
  lenses actually ran — so an empty lens can never be mistaken for a clean bill of health.
- **Non-trivial pure logic carries a runnable self-check.** `harness.pdf`,
  `harness.local_exec` and `harness.stages.report` each run standalone via `-m`; the
  probe-statistics bug above was caught by one.
