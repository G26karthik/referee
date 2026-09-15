# The eight-paper run, 2026-09-08 — attempted, blocked, resumable

**Headline: the run did NOT complete. 0 of 8 papers produced a review.** It stopped in the
S2 audit phase on a provider session limit. Every case is `waiting`, which is resumable;
none is `error`. No review, no finding, no number in this document comes from this run,
because it produced none.

Four states are reported separately below because they are four different facts about
four different things, and conflating any two of them would misdescribe the system.

---

## State 1 — the implementation phase's adversarial agents (an EARLIER, FINISHED phase)

Seven agents in the implementation workflow failed with a provider session limit whose
window reset at 9pm (Asia/Kolkata):

```
build:delegation      verify:extraction   verify:docintegrity   verify:coverage
verify:guarantees     verify:selfchecks   verify:delegation
```

Five of the six BUILD agents completed and landed their work. The sixth
(`build:delegation`) landed substantial work before the limit — the per-lens model, the
deny-list, the confinement flags, the replay record and 112 tests — and its subject was
verified by hand afterwards.

**This is a fact about the implementation phase.** It is not a fact about the run below,
it happened before the run was launched, and it is a different limit window.

## State 2 — did anything hit a limit DURING the eight-paper run? YES

A **second, distinct** provider session limit, window resetting at **3am (Asia/Kolkata)**.
Recorded verbatim by the harness, per lens:

```
confound: You've hit your session limit · resets 3am (Asia/Kolkata)
contradiction: You've hit your session limit · resets 3am (Asia/Kolkata)
```

16 limit signals across all eight papers. **Zero delegated calls failed for any other
reason** — no parse failure, no non-zero exit, no API error status, no reviewer-quality
problem. The two lens calls that ran before the limit both succeeded.

The harness classified this correctly and that classification is invariant 14 working:

- `failure_kind=rate_limited`, and a rate limit is **not** a transient failure, so it
  **consumed no retry attempt** (`"attempt": 1` on every case).
- Every case is `waiting`, not `error` — blocked on evidence that must arrive from
  outside the process.
- The reason string says *"blocked by a rate_limited failure, **not by reviewer
  quality**"* and names the reset time, so a reader cannot mistake a provider limit for a
  finding about a paper or for a defect in the review.
- It refused to emit reviews. Eight reviews built on 0-2 of 4 lenses would have been
  eight artifacts claiming a four-lens panel they did not have.

## State 3 — did the run complete fully or partially? PARTIALLY: 0 of 8

| paper_id | phase reached | status | lens files | review |
|---|---|---|---:|---|
| `0c06a98d7c818f6f` | S2 audit | waiting | 2/4 | none |
| `2024-icml-sapg` | S2 audit | waiting | 0/4 | none |
| `5993d35ff0996b52` | S2 audit | waiting | 0/4 | none |
| `acl` | S2 audit | waiting | 0/4 | none |
| `apt-icml` | S2 audit | waiting | 0/4 | none |
| `cvpr` | S2 audit | waiting | 0/4 | none |
| `iclr` | S2 audit | waiting | 0/4 | none |
| `sanchez24a-icml` | S2 audit | waiting | 0/4 | none |

`reports/corpus.json`, written by this run:
`{"requested": 0, "started": 0, "completed": 0, "failed": 0, "inconclusive": 8}`,
`complete: false`, with `failure_kind=rate_limited` on all eight entries. The run's own
stdout ends with `⚠️ 8 of 8 requested paper(s) did NOT complete.`

**What DID complete, and it is not nothing:**

- All eight papers ingested under **extraction version 2** — the v2 transition is done for
  the whole corpus, and no stale v1 reference is carried forward anywhere.
- 2 of 32 lens artifacts written, both for `0c06a98d7c818f6f`, both through the real
  `audit_driver` (`written_by: audit_driver`, not `manual_accept`).
- The delegation path verified on real output for the first time (below).
- Recorded delegated cost: **$2.36** for two lens calls, so the full audit phase is
  roughly $30-40 at this rate.

**Nothing reached** the grade, discover, probe or report phases. Repository execution was
authorised and never got the chance to run, so this run says nothing about it.

## State 4 — exactly which papers were processed, and their content hashes

| paper_id | content_sha | extraction | pages | sec / tab / fig / eq / xref | repo | title |
|---|---|---|---:|---|---|---|
| `0c06a98d7c818f6f` | `d6295997a2ed` | v2 | 11 | 24 / 3 / 3 / 2 / 11 | no | Bulk RNA-seq Guided Multi-modal Detection of Anomalous Regions… |
| `2024-icml-sapg` | `a0dd4d4db8ae` | v2 | 15 | 29 / 3 / 8 / 1 / 6 | no | SAPG (title line is the author block) |
| `5993d35ff0996b52` | `cb791453c637` | v2 | 31 | 42 / 0 / 0 / 0 / 23 | no | (title line is the acceptance banner) |
| `acl` | `20a4d354b7e7` | v2 | 25 | 30 / 9 / 12 / 3 / 21 | yes | (title line is the proceedings banner) |
| `apt-icml` | `7f8c6fb10765` | v2 | 20 | 37 / 17 / 5 / 4 / 36 | yes | APT: Adaptive Pruning and Tuning Pretrained Language Models |
| `cvpr` | `7bd4c8336fb9` | v2 | 10 | 21 / 6 / 8 / 5 / 23 | yes | WeatherGen: A Unified Diverse Weather Generator for LiDAR Point Clouds |
| `iclr` | `c324730f007e` | v2 | 26 | 13 / 18 / 5 / 0 / 46 | yes | LDREG: LOCAL DIMENSIONALITY REGULARIZED |
| `sanchez24a-icml` | `5b32bee704c7` | v2 | 38 | 47 / 15 / 24 / 3 / 64 | no | (title line is the author block) |

**8 files · 8 distinct content hashes · 8 distinct paper ids · all extraction version 2.**
`run.py preflight` confirmed this before the run and exited 0.

### The two CVPR papers, and why both are in the set

`papers/CVPR.pdf` (`7bd4c8336fb9`) and `papers/0c06a98d7c818f6f.pdf` (`d6295997a2ed`) are
**both CVPR papers**, and both are in the set deliberately. Each carries the identical
CVF watermark:

> This CVPR paper is the Open Access version, provided by the Computer Vision Foundation.

They are distinct documents by hash, by title, by page count and by proceedings folio
(17019 vs 41815). `0c06a98d7c818f6f` cites work up to 2025 and sits at the much higher
folio, which is evidence that it is from the later and far larger volume; the watermark
carries no year, so that is evidence and not proof.

An earlier draft of this work described `0c06a98d7c818f6f` as "not a CVPR paper" on the
strength of its subject matter being spatial transcriptomics. That was an inference from
topic to venue, which is exactly the kind of unfounded step this system exists to catch,
and the document itself contradicts it.

---

## How to resume

The limit window resets at **3am (Asia/Kolkata)**. The run is resumable and re-runs
nothing it already did: the two completed lens artifacts are kept, and the eight parsed
documents are cached, so `ingest` reports `cached` and the audit phase asks only for the
30 lens files that are missing.

```bash
PYTHONUTF8=1 SH_ALLOW_AUTO_AUDIT=1 SH_ALLOW_GRADING=1 SH_ALLOW_SUBSTANTIVE_VERDICT=1 \
  SH_ALLOW_REPO_EXEC=1 SH_ALLOW_INSTALL=1 SH_ALLOW_NETWORK=1 \
  ../.venv/Scripts/python.exe run.py review \
    --paper papers/0c06a98d7c818f6f.pdf papers/2024_icml_sapg.pdf \
            papers/5993d35ff0996b52.pdf "papers/ACl.pdf" "papers/APT _ ICML.pdf" \
            papers/CVPR.pdf papers/ICLR.pdf papers/sanchez24a_ICML.pdf \
    --auto-audit --auto-grade
```

Exit 2 means some paper is still waiting on lens evidence, which is the resumable state,
not an error. `reports/corpus.json` is the machine-readable answer to "did it finish".

At roughly 5 minutes and $1.20 per lens call, the remaining 30 lens calls are about 2.5
hours and $35, before grading, the assessor and repository execution. A limit is likely to
be hit again inside one window; the run is designed to be re-entered until
`reports/corpus.json` reports `complete: true`.

---

## What this run did establish

One thing, and it is the claim the implementation phase most needed to check on real
output rather than on a fixture: **the panel is now genuinely heterogeneous, and the
confinement is genuinely per-lens.**

| lens | model requested | model the CLI **reported** | tools allowed | tools denied |
|---|---|---|---|---:|
| `overclaim` | `opus` | `claude-haiku-4-5-20251001, claude-opus-5` | `Read`, `WebSearch` | 11 |
| `protocol` | `sonnet` | `claude-sonnet-5` | `Read` | 12 |

In the archived September corpus, all four lenses reported `claude-sonnet-5` — including
`overclaim`, whose declared model has always been `opus` — because
`prompts/audit.LENSES[lens]["model"]` was declared and never read. Two lens calls from
this run are enough to show that specific defect closed.

Each artifact also records what a replay needs: the argv, `prompt_sha256`, `raw_sha256`,
`content_sha256`, a content-addressed prompt copy, the raw response, the session id, the
cost, the wall time, the enforced tool policy, and `harness_keys_stripped: 0` /
`unknown_keys_dropped: 0` — invariant 2's guarantee that a lens cannot certify its own
reasoning, now recorded per call rather than assumed.

## Where the September run went

`projects/_run_2026-09_v1/` (seven project directories) and
`reports/_run_2026-09_v1/` (the batch artifacts). Archived, not deleted, with a README
saying what they are. They are the record of the earlier run under extraction v1 and the
shut gates, and nothing in them may be reported as coming from this one.
