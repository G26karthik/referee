# Bounded positive end-to-end capability study — protocol, predeclared

Written 2026-09-18, before any candidate paper's repository, entrypoint, or reproduction
outcome was inspected. Only `data/extracted/*.json`'s FIELD NAMES have been seen at this
point (structural JSON keys, not values) — no candidate has been read.

## Purpose

The eight-paper corpus this manuscript reports on established zero positive end-to-end
reconstructions: every governed reconstruction that reached execution ended
`INCONCLUSIVE`, and no author-code route ever bound a command. This is a bounded,
non-cherry-picked attempt to find at least one case in the reserved candidate pool where
this harness's execution machinery can bind identity and produce an admissible
reconciliation, honestly reported whichever way it goes.

## Eligibility criteria (fixed, checked mechanically, in this order)

A candidate is ELIGIBLE only if ALL of the following hold, checked from structural facts
about the paper and its advertised artifact — never from whether a number reproduces:

1. The paper is in `data/exports/eligible_papers.csv` (already screened into this
   project's reserved pool) and its PDF is retrievable.
2. `data/extracted/<paper_id>.json`'s `code_availability` field indicates code is
   available (not "no code" / "unavailable").
3. `repositories` names at least one URL that resolves to a real, public git host
   (github.com, gitlab.com, or similar) — not a placeholder, an anonymized link, or a
   dead link.
4. `documentation_quality` is not the lowest tier this field records (i.e., the
   repository is not flagged as undocumented/unusable).
5. The harness's own `reimplement.assess`/identity machinery must be ABLE to find an
   explicit entrypoint once the repo is cloned — checked by actually running ingest +
   discovery against the candidate, not guessed from the metadata.

Candidates are read from `data/extracted/` in this fixed order: ascending by `paper_id`
(a content hash, uncorrelated with any outcome). Screening stops once **15 candidates**
have been inspected against criteria 1-4, whether or not any pass. This bound (15) is
fixed before inspection begins. Every candidate that passes 1-4 proceeds to criterion 5
and, if it also passes, gets a full review run. No candidate is excluded after criteria
1-4 pass for a reason not listed above.

## What happens with the result

- If zero candidates are eligible after criteria 1-4: report that honestly as the result
  of this bounded study — evidence about current reach, not a negative finding about the
  harness's correctness.
- If one or more are eligible: run the FULL review pipeline (ingest, audit, discover,
  probe, report) on ALL of them, not a hand-picked subset.
- Report every eligible candidate's outcome, admissible or not. A candidate that reaches
  execution and fails is reported as a failed reproduction under the same provenance and
  materiality rules as the main corpus, not discarded.
- No re-scanning: this protocol runs once, against this fixed 15-candidate window. It is
  not repeated with a larger window if it finds nothing.
