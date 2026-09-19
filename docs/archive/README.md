# Archived development history

Everything in this directory is historical: checkpoints, step-by-step build logs, closure
notes on specific feedback passes, one-off audits of bugs that were found and fixed in the
same development cycle, and earlier drafts of documents superseded by later ones. None of
it describes the current state of the system, and none of it is read by any test, script,
or the manuscript build.

It is kept for provenance rather than deleted, and it is excluded from the release ZIP
(`tools/make_release_zip.py` prunes this directory) so that a reader unpacking the release
sees only documentation that is true of the shipped system today.

For the current, authoritative account of the system, start with `CLAUDE.md` (the working
contract), `HANDOFF.md` (onboarding), and `docs/HARNESS_ARCHITECTURE.md` and
`docs/FINAL_DELIVERY_REPORT.md` (the current architecture and delivery record). The specific
defects some of these archived documents describe as open — the container-execution staging
gap, the route-exhaustion accounting gap, the guarantee-accounting gap, the "no production
caller" state of the authors'-code auditor, the zero-launch diagnostic run — were each found
and fixed in a later pass; `CLAUDE.md`'s "Known limitations" section is the up-to-date record
of what, if anything, remains open.

| file | what it was |
|---|---|
| `CHECKPOINT_A.md`, `CHECKPOINT_B.md`, `CHECKPOINT_C.md` | build checkpoints taken mid-development |
| `CODEX_HANDOFF_RECOVERY.md` | a recovery note from one agent handoff |
| `FINAL_DELIVERABLE.md` | an earlier finalization record, superseded by `FINAL_DELIVERY_REPORT.md` |
| `FINAL_FEEDBACK_CLOSURE.md`, `REVIEWER_FEEDBACK_CLOSURE.md` | closure notes on specific feedback passes |
| `FINAL_HANDOFF_AUDIT.md` | an audit taken at a specific handoff point |
| `FINAL_PRODUCT_GAP_AUDIT.md` | a gap audit against requirements, since closed |
| `FINAL_REQUIREMENTS_CLOSURE.md` | a requirements closure note |
| `FORENSIC_ZERO_LAUNCH_AUDIT.md` | a forensic audit of a run that launched zero processes, before the container-staging fix |
| `ORIGINAL_AUDIO_FEEDBACK_TRANSCRIPT.md`, `ORIGINAL_AUDIO_REQUIREMENTS.md` | raw transcripts of early requirements conversations |
| `PONYTAIL_REFACTOR_REPORT.md` | a report on one refactor pass |
| `PRE_RERUN_ROUTE_EXHAUSTION_REPORT.md` | route-exhaustion numbers from before the corrected rerun |
| `REVISION_2026-09-14_final.md` | an earlier "final" revision pass, superseded by later revisions and `FINAL_DELIVERY_REPORT.md` |
| `STEP_6_ROUTE_FALLBACK.md`, `STEP_7_ALIGNMENT.md`, `STEP_8_REIMPLEMENTATION.md` | sequential build-step logs |
