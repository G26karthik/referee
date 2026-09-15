# Checkpoint A — after the no-known-invalid-execution rule, before question-centric routing

Run identity: `runs_postfix/A_post_no_invalid_execution/`
Pre-fix artifacts: `projects/` — **unchanged**, verified by digest before and after
(`94722fc8ca2d31b006ff9337d01b27e5` both times) and by a clean `git status`.

> **Two corrections, made at Checkpoint B.**
>
> 1. **The `git status` half of that check was vacuous.** `.gitignore` line 4 ignores
>    `projects/` outright, so that directory can never show as modified whatever happens
>    to it. A check that cannot fail is not a check, and it should not have been offered
>    as one. The digest is the real evidence and it stands.
> 2. **The digest's recipe was not recorded, so the value above cannot be recomputed.**
>    It is kept as the record of that run and it is not comparable with anything.
>    `tools/corpus_digest.py` is now that recipe, fixed and re-runnable; Checkpoint B
>    establishes immutability with it, before and after.

## What was re-run, and what was not

`tools/checkpoint_rerun.py` copies the expensive, model-produced half of each case into a
new run root — the parsed document, the four sealed lens files, the 38 sealed grades, the
8 sealed whole-paper assessments — and re-derives everything the deterministic layer owns:
assessment, discovery, planning, probing, reconciliation, reporting.

Copying rather than re-delegating is the point. **The reasoning half of the review is
identical between the two runs by construction**, so every difference below is attributable
to the code that changed and to nothing else. Re-running the lenses would spend tokens and
would also change the panel, which would make the comparison uninterpretable.

Gates for both runs: `repo_exec=True`, `network=True`, `install=False`, `synthesis=True`,
`diagnostic_mode=False`, `max_targets=3`.

## The funnel, before and after

| term | pre-fix (Sept 2026, immutable) | post-fix (Checkpoint A) | changed by |
|---|---:|---:|---|
| discovered | 878 | 878 | — |
| structurally checkable | 716 | 716 | — |
| warranting an experiment | 43 | 43 | — |
| **targets that launched** | **16** | **0** | Step 4 |
| **processes started** | **160** | **0** | Step 4 |
| reconciliations completed | 16 | 0 | Step 4 |
| resolved | 0 | 0 | — |
| admitted as evidence about a paper | 0 | 0 | — |

The reasoning half is unchanged to the object: 878 discovered, 716 checkable, 43
warranting. What changed is that **nothing is started whose result could not speak.**

### Why the number is zero

`stages.probe.admissible_if_it_succeeds` starts a process only when
`provenance.admits(spec.provenance)` — the same ceiling the reconciler applies. On this
corpus identity never bound on any of the 43 warranted targets, so `plan_execution` never
promoted a spec to `repo_exec`, so every spec remained `synthesized`, so nothing ran.

Previously those 16 targets ran 160 processes of the same harness-authored diagnostic and
were then refused, correctly, at the reconciler. The refusals were right; the spend was
not. **This is the same conclusion reached without the compute, and without producing the
sixteen plausible-looking deltas that the admissibility rule then had to catch.**

The isolation boundary (Step 0) did not contribute to this result: it is checked inside
`authorize()`, which is reached only for a spec that was already promoted to `repo_exec`,
and none was. The change is attributable to Step 4 alone.

## What the new disposition field says

| paper | triage | disposition |
|---|---|---|
| `0c06a98d7c818f6f` | GREEN | PASS_TO_HUMAN_CLEAN |
| `2024-icml-sapg` | GREEN | BLOCKED_ARTIFACT |
| `5993d35ff0996b52` | GREEN | BLOCKED_SPECIFICATION |
| `acl` | YELLOW | BLOCKED_ARTIFACT |
| `apt-icml` | GREEN | BLOCKED_ARTIFACT |
| `cvpr` | GREEN | BLOCKED_ARTIFACT |
| `iclr` | GREEN | BLOCKED_ARTIFACT |
| `sanchez24a-icml` | GREEN | BLOCKED_ARTIFACT |

Six of the eight papers read GREEN and had a **central claim that was never checkable
against the artifact**. That fact existed in the pre-fix run too — it was simply not
expressible: `unresolved_central` requires admissible provenance, so a central claim
nobody could check has never been able to colour a paper, and the scope section was the
only place it appeared. `disposition` is where it now lives.

None of the BLOCKED values is an accusation. Each says a limit of this review, and
`disposition.passes_to_human` returns True for all of them.

> **Third correction, made after Checkpoint B.** `0c06a98d7c818f6f`'s PASS_TO_HUMAN_CLEAN
> above was itself an instance of the defect this table exists to catch, one level down:
> its seven CENTRAL targets in this very run include seven carrying `NO_ROUTE_AVAILABLE`
> (`TGT-BAS-P16580-694` and six `TGT-CLM-*`), and `disposition.BLOCKER_FOR_DISPOSITION` did
> not classify that disposition at the time, so it raised no blocker and the paper fell
> through to CLEAN. Recomputing this paper's disposition under the fixed table (see
> `docs/CHECKPOINT_B.md`, "A defect this exposed, and its resolution") gives
> `BLOCKED_METHOD`, not `PASS_TO_HUMAN_CLEAN`. The bug predates Step 5 — `NO_ROUTE_AVAILABLE`
> was already reachable here — so this checkpoint's own table was wrong on this point from
> the start, not merely stale.

## Early stopping

`assess` ran on all eight papers and stopped none: no finding counted FATAL after every
cap. `5993d35ff0996b52` is the only paper in the corpus that ever carried a lens-asserted
FATAL, and the blinded grader demoted it — so the early gate reads the same
`counted_severity` the verdict does and correctly declines to stop. The stop path is
therefore exercised by fixtures on this corpus, not by a corpus paper, and
`tests/test_early_stop_and_no_invalid_execution.py` is what pins it.

## Suite

1695 passed, 21 skipped, 0 failed. The 21 skips all require the `modal` client, which was
not installed in this interpreter at the time of this run.

> **Stale as of Checkpoint B.** `modal` 1.5.4 is present now, so those 21 tests run and
> pass rather than skipping. Nothing about this checkpoint's result depends on them: no
> sandbox has been leased, then or since.

## What this checkpoint does NOT show

- Nothing about whether the 227 concerns are correct. No adjudicated ground truth exists
  for this corpus and none was created.
- Nothing about the authors'-code route working. It still does not bind on any paper here;
  Step 7 (alignment) is what addresses that, and Checkpoint C is where it is measured.
- Nothing about the isolation boundary in production use. No sandbox has been leased.
