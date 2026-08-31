# single-harness — handover

Status as of 2026-08-31. 196/196 tests passing, 5/5 module self-checks passing,
0 findings dropped across every paper reviewed to date.

## Architecture

Four deterministic-by-default stages, one of which pauses for human judgement:

| stage | module | what it does |
|---|---|---|
| S1 ingest | `harness/stages/ingest.py` | Parses a PDF into a `PaperDoc`: sections, tables, every reported number with provenance (table cell or verbatim sentence). Fully deterministic — same PDF in, same `paper/doc.json` out. Also extracts and ranks candidate repo URLs (`harness/repo.py:find_repo_urls`). |
| S2 audit | `harness/stages/audit.py` | Four independent lenses — `overclaim`, `protocol`, `confound`, `contradiction` — each run in its own context so the four readings are independent evidence, not one reading echoed four times. This is the one manual step: `review` writes `audit/prompts/<lens>.md`, a human (or the driving Claude Code session) writes `audit/<lens>.json`, and every `evidence_quote` is re-verified against the parsed corpus at report time — an unsubstantiated finding is silently dropped and the count is printed. |
| S3 probe | `harness/stages/probe.py` (orchestrator) + `harness/repo.py`, `harness/code_audit.py`, `harness/probe_synth.py`, `harness/local_exec.py` | Conditional: fires only when a lens marked something `verifiable_by_experiment`. Four sub-stages, S3a–S3d, below. |
| S4 report | `harness/stages/report.py` | Deterministic verdict (RED/YELLOW/GREEN) from a fixed threshold table — no model call decides it. Renders `projects/<pid>/reports/<pid>.md`. |

### S3 sub-stages

- **S3a acquisition** (`harness/repo.py`) — clones the URL the paper itself advertises
  (ranked by proximity to an availability cue, so a footnote citing someone else's repo
  isn't cloned instead). No URL → a scaffold at `runs/<pid>/standalone_probe.py` whose
  `compute()` raises `NotImplementedError` until written, so it can never manufacture a
  verdict.
- **S3b static audit** (`harness/code_audit.py`) — AST-only, never imports or executes.
  10 rules across three classes: baseline crippling, data-split leakage, metric
  redefinition. **Never emits FATAL** — a pattern match is a suspicion with a line
  number and a counter-explanation, not a demonstration.
- **S3c probe synthesis** (`harness/probe_synth.py`) — the autonomous planner. Reads the
  paper's own published formulation and writes a self-contained, runnable probe to
  `runs/<pid>/probe.py`. Two templates today: `ldreg` (implements the paper's own
  Algorithm 1 `lid_mom_est` and Algorithm 2 loss term when the paper is LDReg-shaped)
  and `placebo` (a general auxiliary-term control for any other paper with a small
  claimed gain and no reported variance). Template matching requires two independent
  textual signals, never one keyword. Every constant is pre-registered from the paper
  in the template source — nothing is tuned against the probe's own output. A
  mechanism template that doesn't test the finding it was dispatched from clears
  `finding_id` rather than misattributing its result.
- **S3d reconciliation** (`local_exec.reconcile`) — `Δ_error = |reproduced − claimed|`
  against the cited table cell, judged arithmetically against a `2σ` seed-noise band.
  See the provenance boundary below — this is where it's enforced.

## Execution policy

Four gates, graded by risk (`harness/config.py`):

| gate | env var | default | permits |
|---|---|---|---|
| network | `SH_ALLOW_NETWORK` | **on** | read-only `git clone --depth 1` of the paper's advertised URL |
| synthesis | `SH_ALLOW_SYNTHESIS` | **on** | the planner authors and runs `runs/<pid>/probe.py` |
| install | `SH_ALLOW_INSTALL` | **off** | build `runs/<pid>/env`, pip-install the repo's own requirements |
| execute | `SH_ALLOW_REPO_EXEC` | **off** | run the repo's own entrypoint across the seed loop |

Network and synthesis default on because neither runs untrusted third-party code: a
shallow clone is inert until something executes it, and a synthesized probe is code
this harness wrote itself from the paper's public text. Install and execute stay off
by default because they mean running a stranger's build hooks and entrypoint — set
either per invocation (`SH_ALLOW_INSTALL=1`, `SH_ALLOW_REPO_EXEC=1`).

## S3 boundary rule — the provenance ceiling

`ProbeSpec.provenance` (and the mirrored field on `Reconciliation`) records who wrote
the code that ran: `template` (identical arms, measures this machine only), `synthesized`
(the S3c planner, from the paper's formulation, at toy scale), `driver` (a human's
faithful `spec.json`), or `repo_exec` (the paper's own checkout).

**Only `driver` and `repo_exec` may reach `RESOLVED_VERIFIED` or `FAILED_REPRODUCTION`.**
A `synthesized` probe is capped at `INCONCLUSIVE` in both directions — it may not
convict a printed cell (a toy reimplementation isn't entitled to accuse a real paper),
and it may not acquit one either (landing near the number by luck would launder a claim
nobody actually checked). The arithmetic is still computed and recorded on the
artifact; it just never becomes a verdict, and it cannot turn a paper RED on its own.
Its report section is headed "Autonomous mechanism probe," not "Measured reproduction,"
and carries a standing caveat to that effect.

The only way to reach a real FATAL/RED reproduction verdict is `SH_ALLOW_REPO_EXEC=1`
running the paper's own code, or a human-authored `spec.json` (`provenance: "driver"`).

## CLI entrypoints

```bash
# Full pipeline. Exit 2 = audits pending (write projects/<pid>/audit/<lens>.json and re-run).
# Exit 0 = complete, report printed. Exit 1 = error. Always PYTHONUTF8=1 on Windows.
python run.py review --paper papers/<name>.pdf
python run.py review --paper <paper_id>          # resume after audits are written

# Individual nodes
python run.py node ingest_paper   --paper <path_or_id>
python run.py node audit_paper    --paper <paper_id>
python run.py node run_probe      --paper <paper_id> [--force-probe] [--skip-probe]
python run.py node synthesize_report --paper <paper_id>

# Verification
python -m pytest tests -q
python -m harness.pdf <pdf>            # self-check
python -m harness.local_exec           # self-check
python -m harness.stages.report        # self-check
python -m harness.repo                 # self-check
python -m harness.code_audit           # self-check
python -m harness.probe_synth          # self-check
```

Reports land at `projects/<paper_id>/reports/<paper_id>.md`, with the machine-readable
form at `projects/<paper_id>/reports/<paper_id>.json`. Run artifacts (`probe.py`, cloned
repos, `probe_results.json`) live under `projects/<paper_id>/runs/<paper_id>/`.
`projects/` is gitignored — it is generated state, not source.

## Papers reviewed to date

All three re-run against the current pipeline (delta-grounding fix, S3a–S3d, autonomous
synthesis). 0 findings dropped for unsubstantiated evidence across all three — every
`evidence_quote` verified against the parsed PDF.

| paper | verdict | FATAL | MAJOR | MINOR | S3 probe | repo cloned |
|---|---|---|---|---|---|---|
| ACL (FinChain / ChainEval) | 🔴 RED | 0 | 16 | 4 | synthesized `placebo`, within noise | `mbzuai-nlp/finchain` |
| ICLR (LDReg) | 🔴 RED | 0 | 14 | 7 | synthesized `ldreg`, **detectable** (LID 4.20→14.11, Δ +9.91 vs 2σ 4.77) | `HanxunH/LDReg` |
| CVPR (WeatherGen) | 🔴 RED | **2** | 14 | 6 | synthesized `placebo`, within noise | `wuyang98/weathergen` |

**63/63 findings verified** (20 + 21 + 22, 0 dropped in each case).

CVPR is the only paper with FATAL findings: Table 2's fidelity comparison fine-tunes
the proposed method on real target-domain data while every baseline simulator never
sees it — a confound the paper's own ablation confirms (unfine-tuned model scores
worse than the weakest baseline on the split it was developed against).

ICLR's synthesized probe is the one case where S3c produced a real, detectable
mechanism result: implementing the paper's own Algorithms 1 and 2 on synthetic data
shows LDReg does raise local intrinsic dimensionality by more than 2× the seed-noise
band. Per the provenance ceiling above, this corroborates the mechanism — it does not
verify or refute any ImageNet number in the paper's tables.

## What's still open

- No `spec.json` has been hand-written for any of the three papers, so no `driver` or
  `repo_exec` reconciliation has ever run — every reconciliation attempt to date has
  been correctly capped at `INCONCLUSIVE` by provenance, not by a failed comparison.
- `SH_ALLOW_INSTALL` / `SH_ALLOW_REPO_EXEC` have never been turned on. Doing so would
  execute the authors' own code and is a deliberate per-invocation decision, not a
  default this harness should carry.
