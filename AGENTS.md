# REFEREE evaluation and writing contract

This file applies to the whole `single-harness` tree.

## Scientific authority

- Treat source code, tests, paper bytes, and persisted artifacts as authoritative. A
  narrative checkpoint is not evidence when the underlying record is missing.
- Never let a model-assigned severity or materiality judgement establish a paper-level
  failure. Model output may propose an investigation. Only deterministic paper evidence or
  an admissible bound execution on a machine-defensible paper-owned dependency may STOP.
- Exhaust every applicable, implemented, affordable, non-inventive route for each material
  question before reporting it unresolved or blocked. A closed gate or target cap is a
  harness limitation, not a discharged route.
- Do not invent missing method, training, dataset, metric, configuration, or comparison
  details. Use typed blockers.
- Generated reconstruction code cannot verify its own conformance. Record distinct
  generator and verifier contexts, and bind every required ingredient to both a paper
  locator and a verified implementation locator.

## Evaluation identity and leakage control

- Keep the completed historical corpus, interrupted Claude pilot, and final Codex run in
  separate directories and tables. Never pool their quantitative results.
- A scientific lens receives one paper, its own role, and permitted source-fidelity access.
  It receives no other lens output, prior finding, historical outcome, manuscript claim, or
  baseline response. Use one fresh reasoning context per lens.
- Graders and whole-paper assessors use fresh contexts and do not see target outcomes that
  would reveal the desired decision.
- The single-model baseline receives the same rendered paper context as a REFEREE lens and
  no REFEREE artifact. Record the actual controller/model identity or `unrecorded / not
  provable`.
- Human results must come from real participants. A model simulating a human is another
  model arm.

## Rubric discipline

PaperGym's transferable lesson is evaluation structure, not its research-plan-generation
task. Keep criterion-level records, separate prompt information from held-out scoring
information, retain blind arm labels, report scorer stability per criterion, and avoid
compressing unlike dimensions into one scalar. Freeze prompts and rubrics before the final
run. Do not tune the system against the eight evaluation outcomes.

## Artifact hygiene

- Preserve historical run directories. Create a new timestamped directory for a new run.
- Record full paper hashes, code diff/revision, prompt hashes, controller/delegation
  mechanism, model configuration, route attempts, execution logs, reports, ledgers, and a
  reproducible digest.
- Treat filesystem/tool isolation as `unrecorded / not provable` unless an artifact proves
  it. Fresh reasoning contexts do not imply filesystem isolation.
- Do not call a run final until all requested papers have terminal reports and a complete
  corpus manifest whose counts conserve.

## Manuscript

- Write the abstract last from final-run artifacts.
- Keep the systems-evaluation eight separate from the random 200 reserved for future work;
  do not estimate prevalence from eight papers.
- Distinguish running, reproducing, verifying, scientifically settling, establishing a
  defect, and establishing material failure.
- Use one main claim per paragraph. Avoid em dashes, repeated rhetorical contrasts,
  inflated adjectives, and long inventory paragraphs.
- Run claim checkers, a halt-on-error two-pass LaTeX build, reference/citation checks, page
  count, overflow checks, and rendered Figure 2 inspection before `READY TO SEND`.

