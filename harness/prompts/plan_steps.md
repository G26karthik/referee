## extracted
STEP 1 — THE MAIN CLAIMS were extracted from the paper before this plan, by an independent reader (listed above,
with ids). They are fixed: you never add, drop, requote or retype one. In `central_claims` give ONE entry per claim id:
{"id": "K1", "checks": [...], "omitted": [...], "why_unchecked": "", "blocker": ""} — leave `quote`, `claim_type` and
`scope` out; the harness fills them from the extraction. Group under each claim every check that tests it: the seeds,
datasets, baselines, panels and exact cases of one claim are the stages and `covers` of its checks, never new claims. A
comparison you choose to make a qualitative claim testable (a rival, a threshold, a grid) is a DIAGNOSTIC of yours
(`criterion`: "supplied"), reported as such and never as a claim of the paper. Where a claim lists `interpretations`,
test every reading (as `readings` of one check, or one check per reading) or say in `why_unchecked` why a reading cannot
be computed: never choose one. A claim's `assumptions` are premises the test must keep (a counterexample must satisfy
them); its `required_evidence` says what would decide it. A costly experiment never shares a check with a cheap one
that tests something else (a proof step, an integration test, one feasible dataset): give each its own check, so one
that cannot finish never takes the others with it.
## legacy
STEP 1 — CENTRAL CLAIMS. No claim extraction is on record, so you list the claims: every distinct claim the abstract,
the contribution list and the conclusion say the paper shows (at most 8, most important first; more is refused), each
as a verbatim quote: its headline experiments, its theorems, and its engineering claims (e.g. "can replace a layer of an
existing model" is exercised by swapping it in and training — it is NOT a qualitative claim). For each give `scope`:
every method, dataset, setting and metric the claim names or compares, ONE per entry (e.g. ["Method A", "Baseline B",
"Dataset D1", "Dataset D2"] — never "D1, D2 and D3" in one entry: the harness refuses a listing entry).
