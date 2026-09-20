# Bounded positive end-to-end capability study — result

Protocol: `docs/SECTION5_CAPABILITY_STUDY_PROTOCOL.md`, written 2026-09-18 before any
candidate was inspected.

## What was screened

15 candidates inspected against criteria 1-4, in ascending `paper_id` order (a content
hash, uncorrelated with outcome), from `data/extracted/*.json` intersected with
`data/exports/eligible_papers.csv`:

| paper_id | passed 1-4? | excluding reason |
|---|---|---|
| 006c425ba843580a | no | (criteria 2-4 failed) |
| `00b5c2c912c26772` | **yes** | — |
| 0140759d5543a70b | no | (criteria 2-4 failed) |
| 016c65c9c6dfa242 | no | (criteria 2-4 failed) |
| 026cfc94ec0f8d27 | no | (criteria 2-4 failed) |
| `034578c40c29c76a` | **yes** | — |
| 03eeb1e35c78aa8e | no | (criteria 2-4 failed) |
| 048f891e8972dc94 | no | (criteria 2-4 failed) |
| 04cb182ca9437055 | no | (criteria 2-4 failed) |
| `052d985dd25d5a7f` | **yes** | — |
| 0557d854eda02783 | no | (criteria 2-4 failed) |
| `0691b97b3118940a` | **yes** | — |
| 06b5ef4b1e44750e | no | (criteria 2-4 failed) |
| `070306ab301ce561` | **yes** | — |
| 074270801e497c3b | no | `code_availability.status == "none"` (criterion 2) |

5 of 15 passed criteria 1-4 and proceeded to a full review (the criterion-5 check, run
directly rather than pre-guessed, per the protocol). All 5 completed end to end.

## Outcome, all 5, from each review's own `## Scope of this review`

| paper_id | title | review path | targets discovered | warranting an experiment | **executions launched** |
|---|---|---|---:|---:|---:|
| 00b5c2c912c26772 | Semantic-Aware Multi-Label Adversarial Attacks | PAPER_ONLY (no repo advertised) | 13 | 2 | **0** |
| 034578c40c29c76a | Is Score Matching Suitable for Estimating Point (Process parameters) | PAPER_AND_ARTIFACT | 32 | 3 | **0** |
| 052d985dd25d5a7f | FedSaC | PAPER_AND_ARTIFACT | 197 | 5 | **0** |
| 0691b97b3118940a | Vector Quantization Prompting (VQ-Prompt) | PAPER_AND_ARTIFACT | 55 | 5 | **0** |
| 070306ab301ce561 | TACO (clustering-guided contrastive FEC) | PAPER_ONLY (no repo advertised) | 87 | 2 | **0** |

Zero of five produced an admissible execution. This is not five identical refusals for
one reason:

- **00b5c2c912c26772** initially misclassified as PAPER_ONLY by a genuine harness bug —
  the paper's own footnote reads `available at https://github.com/hassan- mahmood/...`,
  a line-break inside the owner's real hyphenated name that the URL extractor did not
  tolerate. Fixed (`harness/repo.py`, see `CLAUDE.md`), re-run: the repository was found
  and cloned (review path correctly flips to PAPER_AND_ARTIFACT, 6 artifact facts
  established), but the one target that reached identity resolution ended
  `IDENTITY_BLOCKED` — "the only program available for this target was synthesized,"
  i.e. no explicit repository command binds to the paper's stated quantity. Genuinely
  ineligible, for the reason the protocol exists to surface.
- **034578c40c29c76a**, **052d985dd25d5a7f**, **0691b97b3118940a** all reached
  PAPER_AND_ARTIFACT (repository obtained) and all three report the identical scope-line
  reason: *"the repository was obtained and this host could not be shown able to give it
  a fair run"* — a resource/capability refusal at this host, not an identity or
  specification refusal. `0` targets reached execution in any of the three.
- **070306ab301ce561** stayed PAPER_ONLY. Its one GitHub URL in the extracted text is a
  bare footnote-style citation (`1https://github.com/Alcyoneus87/TACO`, digit fused
  directly to the URL, no recoverable availability phrase within the extraction window)
  — structurally identical to the harness's own `vissl`-footnote example of a citation to
  someone else's work, which `official_repo_url`'s digit-prefix heuristic is deliberately
  built to suppress. Investigated and left as-is: this is the harness correctly declining
  to guess, not a bug — see `CLAUDE.md`'s repo.py notes.

Two of the five (`00b5c2c9`, `070306ab`) turn on the same class of ambiguity — a
citation-shaped URL — and were each investigated individually rather than patched by a
blanket rule, per the "never fix by weakening the discipline that caught it" mandate.

## What this establishes, and what it does not

This is a second, independently-drawn, bounded, non-cherry-picked sample, and it produced
the same result as the eight-paper corpus: **zero positive end-to-end reconstructions**.
That doubles the evidence behind the corpus's own honest limitation rather than
contradicting it. It does not mean this harness cannot ever bind an experiment identity —
`tests/test_autonomous_review_e2e.py` proves the mechanism on fixtures — only that, across
13 real papers now (8 corpus + 5 here), none has offered the harness an explicit,
identity-bindable command this host could execute.

Per the protocol's own predeclared stopping rule: *"this protocol runs once, against this
fixed 15-candidate window. It is not repeated with a larger window if it finds nothing."*
This study is complete. No further candidates were or will be screened under this
protocol.

## Incidental yield

Screening this window surfaced two genuine repository-URL-extraction bugs
(`harness/repo.py`: a hyphen-broken owner name, a dot-broken host literal), fixed with
regression tests, and produced 5 complete, real reviews with substantive, independently
re-verified findings (several MAJOR) against 5 real, recently published papers — usable
material for Section 6's controlled-benchmark work and as additional real-world evidence
of this harness's issue-detection behavior, separate from the eight-paper corpus.
