# Controlled known-defect benchmark — protocol, predeclared

Written 2026-09-18, before any defect was injected or any paper selected for injection.

## Purpose

Section 6 of the closure mandate: build the strongest bounded, non-simulated evidence
that REFEREE detects real categories of scientific defect, with a defensible
false-positive story on clean controls — not a claim about naturally-occurring accuracy,
and never a simulated or LLM-as-human evaluation.

## Design

**Source papers.** Six papers already ingested by this harness, so `paper/doc.json`
already exists and is authoritative: the four repository papers from the main corpus
(`acl`, `apt-icml`, `cvpr`, `iclr`) plus two of the five Section 5 candidate papers
(chosen after that study completes, picked as the two with the LARGEST parsed surface,
a structural fact unrelated to any defect-detection outcome).

**Injection point.** A defect is injected by editing a COPY of the paper's already-parsed
`doc.json` directly, not by regenerating a modified PDF. This is disclosed rather than
hidden: every downstream stage (quotation verification, addressing, audit) reads
`doc.json`, never the PDF again after ingest, so an edit at this layer is exactly as
consequential to the pipeline as a matching PDF edit would be, without the extraction
noise a recompiled, re-extracted PDF would add as a confound. Each injected copy lives at
a NEW project id (`<original>-defect-<n>`), so the clean project is never touched and
both variants can be reviewed independently.

**One frozen defect per case, chosen from the paper's OWN content** (never invented from
outside the paper — a defect must be a real edit to real numbers/sentences already in the
document, so a "detected" defect is never an artifact of an implausible insertion):

| # | source paper | defect family | the edit |
|---|---|---|---|
| 1 | `acl` | arithmetic/table inconsistency | change one already-printed table cell so the row's own stated total no longer sums to what the same table's other cells imply |
| 2 | `apt-icml` | prose-table contradiction | change a prose-stated percentage so it contradicts the table cell the same sentence cites |
| 3 | `cvpr` | overclaim relative to a stated comparison | strengthen a comparative claim's wording (e.g. "comparable to" -> "substantially outperforms") against an unchanged, weaker table margin |
| 4 | `iclr` | missing-control / confound | delete the one sentence naming a control condition, leaving the surrounding claim's dependence on it textually orphaned |
| 5 | (Section 5 candidate, TBD) | specification omission | delete the sentence naming a stated hyperparameter/seed policy the results section's claim depends on |
| 6 | (Section 5 candidate, TBD) | citation/evidence mismatch | change an in-text reference to a table/figure number to point at a table/figure that does not contain the cited quantity |

Each edit is ONE localized change, recorded (before running anything) as: file, byte
span or table cell, original text, injected text, and which finding category a correct
detection should land in. This record is written to
`docs/SECTION6_INJECTED_DEFECTS.json` before the first defect-variant review runs.

**Clean controls.** The SAME six papers' unmodified `doc.json` (already reviewed as part
of the main corpus, or freshly reviewed for the two new ones) serve as the clean arm —
same paper, same pipeline, absence of the one injected sentence/cell. This directly
measures the false-positive rate: does the clean run also flag the same location for
the same reason, which would mean the "detection" on the defect arm is not really
tracking the injected change.

## Anti-leakage controls (Section 19)

- The defect is injected by THIS orchestrating session, in a project directory the
  audit-lens subagents never see the sibling of. A lens dispatched against the
  defect-injected copy has no access to the clean copy's project directory or to this
  protocol document (subagents are launched with `--add-dir` scoped to the papers
  directory only, per `audit_driver.py`'s existing sandboxing — nothing new is opened
  for this study).
- Ground truth (which case, which location, which category) is written to
  `SECTION6_INJECTED_DEFECTS.json` in the ORCHESTRATOR's own docs/ directory, never
  copied into any project directory a lens or grader can read.
- Attribution is separated: the defect specification above was written before any
  review ran; scoring is done by comparing the harness's OWN typed output
  (`findings`, `scientific_class`, `evidence_ref`) against this frozen table, never by
  asking a model whether it thinks it caught the defect.
- The six clean-arm and six defect-arm reviews are run through the ordinary pipeline,
  with no case-specific prompt, threshold, or rule — the same discipline invariant 10
  already requires for the main corpus.

## Metrics reported

For each of the 6 cases: whether ANY kept finding's `evidence_ref` resolves inside the
injected span (grounding), whether its `scientific_class`/route matches the predeclared
category, and whether the clean arm produced a finding at the same location (a
false-positive risk if so). Aggregate: detection recall (how many of 6 were caught),
false-positive count on the 6 clean controls at the SAME locations, and a plain
statement of what this does and does not establish — six cases is a controlled capability
demonstration, not a prevalence or accuracy claim on naturally occurring errors.
