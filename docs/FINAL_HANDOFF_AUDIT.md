# Final handoff audit

Written 2026-09-16 at the start of the finalization phase, before any code was changed.
Everything below was read off the repository, the frozen run directories and a full test
run on this machine. Where a number in an existing document disagrees with what the disk
says, the disk wins and the disagreement is recorded.

## 1. The evaluated code revision

**This was the single most serious forensic finding: the evaluated code had no revision.**

`runs_final_codex_v2_2026-09-15` was produced by an uncommitted working tree. The last
commit in the repository at the start of this phase was `884c9b9`, dated 2026-09-05, ten
days before the run. Every module that implements the v2 review boundary — `discovery`,
`questions`, `taxonomy`, `materiality`, `disposition`, `exhaustion`, `guarantees`,
`coverage`, `outcome`, `alignment/`, `reimplement_driver` and twenty others — was
untracked. A manuscript written against that tree would have had nothing to cite, and the
cleanup phase that follows would have silently become "the evaluated system".

The working tree has now been frozen verbatim:

| | |
|---|---|
| evaluated revision | `1c5bbdcfdb463907cafa61f3d832198d3d9e8426` |
| tag | `evaluated-v2` |
| parent | `884c9b9` (2026-09-05) |
| repository root | `C:\Users\saita\OneDrive\Desktop\RamanIQ` (a monorepo; the harness is `single-harness/`) |
| branch | `master` |

**The freeze is verbatim, not a reconstruction.** No `harness/`, `tests/` or `run.py` file
has an mtime later than 19:02 on 2026-09-15, which is when the run's own report artifacts
were written. The newest harness file is `harness/stages/probe.py` at 18:13 and the newest
report is `component_ablations.md` at 19:02. The only Python file touched afterwards is
`tools/seal_final_run.py` (19:18), which seals a finished run and is not on the review
path. So the code committed as `evaluated-v2` is the code that actually ran.

Two per-run caches are excluded from the commit and `.gitignore` states why: the
dependency virtualenv (`runs_*/projects/*/runs/*/env/`, 28 MB for `acl` alone) and the
depth-1 clone of each authors' repository (`.../repo/`, 14 MB for `acl`). Both are
derivable from the commit SHA and interpreter recorded in the committed `spec.json` and
`probe_results.json`. `reimplementation/` is committed: it holds the five generated
reconstructions that conformance verification rejected, which is primary evidence.

A second defect was found while freezing. The pre-existing `.gitignore` rules `projects/`
and `reports/` were unanchored, so they matched at any depth and hid every frozen run's
entire evidence directory, not just the current checkout's working state. They are now
`/projects/` and `/reports/`.

## 2. Current working-tree state

Clean at `evaluated-v2` for everything under `single-harness/`. 1,909 files were added or
modified in the freeze commit (962,122 insertions).

Modifications elsewhere in the monorepo — `data/exports/*.csv`,
`data/extraction_queue/*.json`, `db/`, `reports/pipeline_report.md` — belong to the
upstream screening pipeline, are unrelated to REFEREE, and were deliberately left
uncommitted and untouched.

## 3. Test count

Run on this machine, 2026-09-16, `python -m pytest tests -q`:

```
1982 passed in 392.84s (0:06:32)     # before the release refactor
1985 passed in 212.47s (0:03:32)     # after it, including three tests this phase added
```

**1982 passed, 0 skipped, 0 failed at `evaluated-v2`; 1985 at the release revision.** This is a correction to the figure carried in
`docs/FINAL_DELIVERABLE.md` and `runs_final_codex_v2_2026-09-15/RUN_STATUS.md`, both of
which say "1961 passed, 21 Modal-only skipped". The collected test count is identical;
the difference is that `modal` 1.5.4 is installed in this interpreter, so the 21
Modal-guarded tests execute here instead of skipping. Both records describe the same
suite. The manuscript quotes 1,982 for the evaluated revision and says that 21 of them
require `modal` to be importable. The three additional tests at the release revision pin
invariant 23's second clause and are described in `docs/PONYTAIL_REFACTOR_REPORT.md`.

Interpreter: CPython 3.13.15, `../.venv/Scripts/python.exe`.

## 4. Final-run identity

| | |
|---|---|
| run id | `runs_final_codex_v2_2026-09-15` |
| authorization | `RUN_AUTHORIZATION.json`, dated 2026-09-15 |
| controller | `codex exec v0.154.0-alpha.6.2` |
| provider / model reported | `openai` / `gpt-5.6-luna` |
| filesystem isolation | `unrecorded / not provable` (the manifest's own words) |
| fresh authorized model calls | 16: eight matched single-model baselines, eight paper-only whole-paper assessments |
| papers | `0c06a98d7c818f6f`, `2024-icml-sapg`, `5993d35ff0996b52`, `acl`, `apt-icml`, `cvpr`, `iclr`, `sanchez24a-icml` |
| integrity | `RUN_MANIFEST.json` binds each source PDF, rendered paper, baseline prompt/response and whole-paper prompt/response to SHA-256; `SHA256SUMS` (151.7 KB) covers every regular run file except the `runs/*/env` caches |

Every headline number in the handoff prompt was re-derived from
`reports/system_evaluation.md`, `reports/material_question_routes.md`,
`reports/component_ablations.md` and `reports/single_model_baseline.md` inside that
directory. All of them check out:

| quantity | disk | source |
|---|---:|---|
| terminal paper reports | 8 | `system_evaluation.md` |
| candidate findings | 64 | 55 kept + 9 dropped |
| quotation-verified | 55 | verification rate 0.8594 |
| dropped unsubstantiated | 9 | |
| targets discovered | 706 | funnel |
| structurally checkable | 657 | funnel |
| experiment-warranted | 72 | funnel |
| processes launched | 0 | funnel |
| reconciliations | 0 | funnel |
| scientifically settled | 0 | funnel |
| material questions | 12 | route table |
| applicable routes | 16 | route table |
| attempted / completed / exhausted | 16 / 16 / 16 | route table |
| route-exhaustion coverage | 100% | |
| open because of this harness | 0 | |
| completed-inconclusive paper checks | 8 | route outcomes |
| author-code routes exhausted by identity failure | 3 | route outcomes |
| reconstructions rejected for missing bindings | 5 | route outcomes |
| GREEN deterministic decisions | 8 | per-paper table |
| counted FATAL / MAJOR / MINOR | 0 / 0 / 30 | |
| baseline proposed / kept / dropped | 24 / 8 / 16 | `single_model_baseline.md` |
| addressable surface / addressed / pursued | 2045 / 682 (33%) / 62 (3%) | coverage |
| document observations, PAPER / EXTRACTION | 0 / 85 | |
| conservation violations | 0 | |
| provenance violations | 0 | |

Two numbers in the handoff prompt are worth restating more precisely. The prompt lists "8
completed-inconclusive paper checks" and "16 applicable material-question routes"; the
route table shows those eight checks plus three identity blocks plus five conformance
blocks sum to exactly the 16 routes, spread over 12 questions, so a question can carry
more than one route. And the prompt's "0 harness-limited material questions" is the line
`open because of this harness | 0`: no question was left open by the scheduler, a target
budget, a closed gate or a missing implemented capability.

## 5. Other runs on disk (none deleted)

| directory | size | what it is |
|---|---:|---|
| `runs_final_codex_v2_2026-09-15` | 155 MB | **the authoritative run** |
| `runs_final_codex_2026-09-15` | 42 MB | the earlier Codex run, superseded, kept for comparison |
| `runs_final_2026-09-14` | 46 MB | the interrupted Claude pilot; excluded from every v2 count |
| `runs_postfix/A_post_no_invalid_execution` | | a postfix verification run |
| `runs_postfix/B_post_question_centric_routing` | | a postfix verification run |
| `projects/` | 286 MB | the live working checkout, plus `projects/_run_2026-09_v1/`, the archived September reviews |

Nothing was pooled, and nothing was deleted.

## 6. Manuscript

| | |
|---|---|
| submission source | `manuscript/manuscript.tex`, 533 lines |
| rendered | `manuscript/manuscript.pdf`, **6 pages**, two-column, 10 pt |
| current title | "REFEREE: Evidence-Closed Autonomous First-Pass Scientific Review" |
| figure | `manuscript/fig2_decision.tex` to `fig2_decision.pdf`, standalone vector, 65 KB |
| bibliography | `manuscript/refs.bib` |
| checkers | `check_claims.py`, `check_referee_claims.py`, `check_final_codex_claims.py`, `check_final_style.py` |
| table generators | `manuscript/tables/make_tables.py`, `make_findings_table.py`, `make_system_tables.py` |

`manuscript/running_is_not_verifying.tex` (1,793 lines, 113 KB) is a **stale longer
draft**, not a journal version of v2. Its text reports 16 launched processes and a route
count rising "from 43 to 88", which are figures from the 2026-09-09 run, not from v2. It
must not be used as the journal spine without rewriting every number in it. The six-page
`manuscript.tex` is the only source that describes the v2 run correctly, and it is the
spine the journal version should grow from.

## 7. Unresolved TODOs

Three, and none of them is incomplete harness code:

- `harness/local_exec.py:143` and `:154`, both reading `TODO(driver): branch on arm to apply the paper's mechanism.`
- `harness/repo.py:1002`, reading `TODO(driver): implement the paper's formulation and return its metric.`

All three sit **inside template strings the harness emits** for a human to complete. The
third is followed immediately by a `raise NotImplementedError` whose message says the
scaffold must be implemented "before treating its output as a reproduction". They are the
system refusing to invent an experiment, which is invariant 9, not unfinished work.

## 8. Differences between the evaluated code and the working tree

**None, for `single-harness/`.** The working tree was frozen as `evaluated-v2` before any
edit in this phase. Every subsequent change belongs to the release refactor and will be
measured against this tag.

## 9. Code and test inventory at `evaluated-v2`

| | |
|---|---:|
| `harness/` Python files | 65 |
| `harness/` lines | 36,413 |
| `tests/` Python files | 52 |
| `tests/` lines | 22,517 |
| `tools/` lines | 1,308 |
| `run.py` lines | 472 |
| runtime dependencies | 5 (`pydantic`, `pymupdf`, `pdfplumber`, `numpy`, `scikit-learn`) plus optional `modal` and `pytest` |
| largest modules | `artifacts.py` 3,304, `stages/report.py` 2,493, `backends.py` 1,716, `local_exec.py` 1,574, `stages/probe.py` 1,400, `audit_driver.py` 1,396 |

`numpy` and `scikit-learn` are not imported by the harness process. They are imported by
the probe subprocess from the default template in `harness/code_audit.py`, and
`requirements.txt` already says so.

## 10. Ponytail

**Ponytail is not installed as a Claude Code plugin or skill, and this phase did not use
one.** The evidence: the plugin catalogue cache
(`~/.claude/plugins/plugin-catalog-cache.json`, 376 KB) contains zero matches for
"ponytail"; `~/.claude/plugins/installed_plugins.json` lists twelve plugins and none is
Ponytail; and no skill by that name appears in the session's skill list.

What exists is `ponytail-enterprise-scale.mdc` at the repository root, a Cursor-format
rule file with `alwaysApply: true`, which Claude Code does not load. Its content is the
"lazy senior dev" doctrine: stop at the first rung that holds (YAGNI, then the standard
library, then a platform feature, then an already-installed dependency, then one line,
then minimum code), no unrequested abstractions, deletion over addition, and "not lazy
about" input validation, error handling that prevents data loss, security, and anything
explicitly requested.

The cleanup phase therefore applies that rule file by hand and uses the installed
`pr-review-toolkit:code-simplifier` agent for the complexity review.
`docs/PONYTAIL_REFACTOR_REPORT.md` repeats this so that no reader infers a tool that was
not run.

## 11. What this audit did not establish

- It did not re-run the eight-paper model evaluation. Every v2 number here was read from
  the frozen artifacts, not reproduced.
- It did not verify that the code implements every claim the manuscript makes. That is
  `docs/CODEBASE_CLAIM_MAP.md`, built next from independent audits.
- It did not check the bibliography against the external literature.
