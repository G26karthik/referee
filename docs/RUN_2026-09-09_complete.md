# The eight-paper run, 2026-09-09 — completed end to end

**This run completed. All 8 papers reached a terminal state through every stage:
ingest → audit → collect → grade → discover/prioritise → probe/execution →
reconcile → outcome/coverage/document-integrity/guarantees → report → dossier.**
Nothing was skipped, nothing was pooled across delegation modes, and the same six
states this document tracks (from `docs/RUN_2026-09-09_boundary.md`) are reported
separately below because conflating any two of them would misdescribe the system.

This document supersedes `RUN_2026-09-09_boundary.md` as the record of *this* run.
The boundary document remains accurate for the state it describes — 13/32 lenses,
stopped at the audit stage — and is kept as the record of that earlier, partial
attempt.

---

## 1. Implementation-phase session-limit events (an EARLIER, FINISHED phase)

Unchanged from the boundary report: seven build/verify agents from the delegation
implementation hit a provider session limit that reset at 9pm (Asia/Kolkata), before
this run was ever launched. Their subjects were verified by hand at the time. This
says nothing about the run below — different phase, different window.

## 2. Limits encountered during THIS run

**None.** Both windows that blocked the prior attempt (CLI account, reset 3am;
session subagent dispatch, reset 2am) had reset by the time this run resumed. Across
the full run — 3 lens subagents to close the last paper, 46 subagents in the
grade+assess workflow, and every deterministic stage — zero rate-limit or session-
limit events occurred. No third mechanism was needed and none exists in this
environment.

## 3. Execution mode actually used, per stage

| stage | mode used | status |
|---|---|---|
| preflight | deterministic, no delegation | complete — 8 files, 8 distinct hashes |
| ingest (v2 extraction) | deterministic, no delegation | complete — all 8 at `extraction_version: 2` |
| audit (32 lenses) | `CLI_SUBPROCESS` ×2, `SESSION_SUBAGENT` ×30 | **complete, 32/32** |
| collect | deterministic | complete |
| grade (38 candidates) | `SESSION_SUBAGENT` ×38 | **complete, 38/38** |
| discover / prioritise | deterministic, no delegation | complete |
| probe / execution | harness's own path — `local` backend, `SYNTHESIZED_DIAGNOSTIC` provenance | ran on all 8; see §6 for what this means and does not mean |
| reconcile | deterministic (arithmetic only) | ran on all 8; every outcome `within_noise` or `no_execution_justified` |
| substantive whole-paper assessment (8) | `SESSION_SUBAGENT` ×8 | **complete, 8/8** |
| outcome / coverage / document-integrity / guarantees | deterministic, no delegation | complete for all 8 |
| report / dossier | deterministic | complete — `reports/Executive_Review_Dossier.{md,pdf}` |

`delegation.summarise` over each artifact class, kept separate rather than pooled:

```
LENS    {"SESSION_SUBAGENT": 30, "CLI_SUBPROCESS": 2}   homogeneous: false   tool_policy_provable_for: 2
GRADE   {"SESSION_SUBAGENT": 38}                        homogeneous: true    tool_policy_provable_for: 0
VERDICT {"SESSION_SUBAGENT": 8}                         homogeneous: true    tool_policy_provable_for: 0
```

The lens layer is genuinely mixed (2 artifacts survive from the earlier CLI attempt,
recorded as CLI_SUBPROCESS exactly as produced); grading and the whole-paper read ran
entirely through this session's own subagent dispatch, because that is what was
available and unspent when those stages ran. Nothing here is presented as one
homogeneous mode.

## 4. Papers and stages: completed, waiting, blocked, failed

All eight papers **completed**. Zero waiting, zero blocked, zero failed.

| paper_id | content_sha | lenses | grade coverage | substantive read | verdict | triage | contested |
|---|---|---:|---:|---|---|---|---|
| `0c06a98d7c818f6f` | `d6295997a2ed` | 4/4 | 6/6 | SUBSTANTIAL_CONCERNS | GREEN | GREEN | no |
| `2024-icml-sapg` | `a0dd4d4db8ae` | 4/4 | 2/2 | SUBSTANTIAL_CONCERNS | GREEN | GREEN | no |
| `5993d35ff0996b52` | `cb791453c637` | 4/4 | 12/12 | CENTRAL_CLAIM_NOT_ESTABLISHED | GREEN | GREEN | **yes** |
| `acl` | `20a4d354b7e7` | 4/4 | 5/5 | SUBSTANTIAL_CONCERNS | GREEN | **YELLOW** | no |
| `apt-icml` | `7f8c6fb10765` | 4/4 | 4/4 | SUBSTANTIAL_CONCERNS | GREEN | GREEN | no |
| `cvpr` | `7bd4c8336fb9` | 4/4 | 4/4 | SUBSTANTIAL_CONCERNS | GREEN | GREEN | no |
| `iclr` | `c324730f007e` | 4/4 | 2/2 | CENTRAL_CLAIM_NOT_ESTABLISHED | GREEN | GREEN | **yes** |
| `sanchez24a-icml` | `5b32bee704c7` | 4/4 | 3/3 | SUBSTANTIAL_CONCERNS | GREEN | GREEN | no |

Totals over the corpus: **0 FATAL / 2 MAJOR / 126 MINOR**, 4 dropped as
unsubstantiated. 0 papers RED, 1 YELLOW (`acl` — a MAJOR concern was attempted and
settled nothing, per invariant 17), 7 GREEN. 2 papers CONTESTED — the independent
whole-paper read disagrees sharply with the table (printed, never counted; `run.py`
exits 3 for exactly this reason, which it did).

**One transient RED is worth recording rather than hiding.** Before grading ran,
`5993d35ff0996b52` counted a lens-asserted FATAL (a CONTRADICTION) and the corpus
table read RED. Once the blinded grader independently read that same candidate and
returned `PLAUSIBLE`/non-FATAL, `counted_severity` demoted it and the paper moved to
GREEN. This is invariant 11 (grading may only CAP severity, never promote it)
working exactly as designed on a real paper for the first time — not a defect, and
not the grader overruling the lens upward, which nothing in this system can do.

## 5. What evidence this run actually produced

- **All 32 lens audits**, non-pooled by provenance (§3).
- **All 38 grade candidates independently, blindly re-read** by an isolated
  subagent per candidate, each producing its own severity, confidence,
  falsification/steelman pair, and independent evidence citation — the first time
  `grading.py`'s discipline has run against a real corpus rather than fixtures.
  `grade_coverage` is 100% for every paper (0 pending anywhere).
- **All 8 whole-paper substantive reads**, also the first time this axis has run
  against a real corpus. Two of the eight (`5993d35ff0996b52`, `iclr`) disagree
  sharply enough with the deterministic table to flag CONTESTED — printed as a
  disagreement, consumed by no threshold, exactly as designed.
- **Real execution and reconciliation on all 8 papers** — 16 processes launched across
  7 papers (1 abstained, `no_execution_justified`), correctly counted after the §6a fix;
  see §6 for exactly what "real" means and does not mean here.
- **Finished reports, ledgers, and one dossier** for all 8 papers:
  `reports/Executive_Review_Dossier.{md,pdf}` plus `reports/corpus.json`.
- **1597 tests passing** after this run (up from 1593 before it, 1596 before the §6a
  regression test was added), with no
  regression in the suite that predates this session.

## 6a. A real bookkeeping defect this run found and fixed in itself

Re-running `review` a second time (to pick up the 38 independent grades after the audit
layer completed) exposed a genuine state-consistency bug, not a paper defect:

`harness.stages.discover` recomputes `discovery/targets.json` from scratch on every
`review` invocation — correctly, since a new grade can change what is checkable — but it
runs BEFORE `probe` and knows nothing about a prior execution. `controller._phase_probe`
finds the cached `runs/<pid>/probe_results.json` from the FIRST pass and (rightly) skips
re-executing anything — but until this run, that cache guard also skipped re-attaching the
already-produced `TargetOutcome` onto the freshly recomputed target set, so the executed
target silently vanished from `discovery/targets.json`'s outcomes.

**Measured effect:** `run.py evaluate`'s funnel read `actually launched a process: 0` and
`ran to a reconciliation: 0` for a corpus that had genuinely executed 16 processes across
7 of 8 papers (verified directly against `runs/<pid>/probe_results.json` and, for the
second and third pursued target per paper, `runs/<pid>/targets/<target_id>/probe_results.json`
— all present and valid on disk, just orphaned from the funnel). The reviewer-facing report
text was unaffected, because `report.py` reads the probe result directly rather than through
`target_set.outcomes` — this was purely a bookkeeping gap in the funnel and coverage counts
`evaluate` and `CaseLedger.efficiency` read.

**Fix:** `harness.stages.probe.resync_cached_outcomes(cfg, pid)` — re-derives which targets
were pursued and deferred this discover pass (`_executable_targets`, pure, no execution),
reads back whichever cached `ProbeResult`s already exist on disk for them, and re-attaches
their outcomes exactly as a fresh `_review` would, spending no new execution. Called from
`controller._phase_probe`'s cached branch. Regression test added:
`test_a_second_pass_does_not_lose_the_first_executions_outcome` in
`tests/test_autonomous_review_e2e.py`, which fails without the fix and passes with it.
After the fix, re-running `evaluate` over this corpus reads `actually launched a process: 16`
/ `ran to a reconciliation: 16` — matching the real, on-disk execution records — with every
paper's final verdict, triage, and CONTESTED flag unchanged (the fix repairs bookkeeping,
not the review's conclusions). Full suite: 1596 → 1597 passing.

## 6. What was NOT exercised — read this before quoting a RED or a repro number

**Repository execution did not run this run, even though `SH_ALLOW_REPO_EXEC=1` was
open the entire time.** Every probe this run produced carries
`provenance: "synthesized"`, `backend: "local"` — a harness-authored diagnostic
script, not a clone-and-run of any paper's own repository. The targets that reached
execution this run were CONFOUND-type noise-band checks (`measured delta vs 2-sigma
noise band, paired over 5 seeds`), which is the class of question the synthesis
route answers on its own terms — no paper's repository is needed to ask "is this
delta distinguishable from seed noise," so the planner never reached for one.
`5993d35ff0996b52` alone returned `no_execution_justified` outright: 30 objects
discovered, none warranted a run.

Under the provenance ceiling (invariant 3), `SYNTHESIZED_DIAGNOSTIC` can neither
convict nor acquit a target, which is exactly why every reconciled target this run
reads `within_noise` rather than a verdict that moved a paper's colour. **The RED
path from a real, authors'-repository execution remains proven on fixtures only**
(`tests/test_autonomous_review_e2e.py`), same as it was before this run — this run
added real grading and real whole-paper assessment on a live corpus, and neither of
those required nor exercised `repo_exec`. Opening the gate was necessary but not
sufficient: nothing this run discovered pointed a CENTRAL, addressable target at a
route that needed cloning a repository.

- **The remote sandbox has still never been leased** — unrelated to this run,
  no provider credentials on this host.
- **Novelty / prior-art checking remains architectural only.**
- **No adjudicated ground truth exists**, so this run adds no precision/recall
  number, only more machine-checked structure.

---

## How this changes what `CLAUDE.md`'s Known Limitations section can claim

- The eight-paper run is no longer "attempted and did not complete" — it completed.
- The corpus lens files are no longer all `manual_accept` — 30/32 are
  `SESSION_SUBAGENT`, 2/32 `CLI_SUBPROCESS`, non-pooled.
- Grading and the substantive assessor are no longer unexercised on a corpus — both
  ran to 100% coverage on all 8 papers.
- Repository execution (`repo_exec` provenance) is still unexercised on a real paper
  from this corpus — that limitation is unchanged and is restated precisely in §6
  rather than implied by an incomplete run.
