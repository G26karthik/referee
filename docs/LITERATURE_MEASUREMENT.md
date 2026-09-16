# The prior-art route, measured on the eight-paper corpus

**One rule shapes this whole route, and it is not symmetric:**

```
finding prior art may support a novelty concern;
failing to find prior art does NOT establish novelty.
```

That is encoded, not asserted. `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` is the **empty
tuple**, no authority or disposition anywhere in the route spells "novel", and
`PriorArtFact.establishes_novelty` is a membership test in that empty tuple — so the
question is not expressible rather than merely discouraged. A completed search with no
match is `SEARCH_COMPLETED_NO_MATCH_FOUND` → `BOUNDED_SEARCH_NO_MATCH`, which is not in
`EVIDENCE_ABOUT_THE_PAPER`, resolves to UNRESOLVED, and does not discharge the route.

This document reports what the route actually did on all eight papers, with both gates
open, on 2026-09-16.

---

## 1. The declared protocol

"Route exhausted" for this route means THE DECLARED PROTOCOL COMPLETED. It cannot mean the
literature was searched — the literature is not enumerable — so every bound is published
in `SearchProtocol` and printed in each paper's record.

| bound | value for this measurement | why it is a bound and not a detail |
|---|---|---|
| claims searched per paper | ≤ 3 (`SH_LITERATURE_MAX_CLAIMS`) | the paper's own novelty/contribution sentences, minted by `claims.mint` |
| query families | `EXACT_METHOD_NAME`, `CONTRIBUTION_DECOMPOSITION`, `TASK_AND_MECHANISM` deterministically; the proposer may add `ACRONYM_EXPANSION`, `BENCHMARK_AND_MECHANISM`, `TERMINOLOGY_VARIANT` | a family outside the closed list is dropped, or "the protocol completed" means nothing |
| queries per claim | ≤ 4 deterministic, ≤ 8 with the proposer | |
| indexes required | `crossref`, `arxiv` | **OpenAlex was dropped from this measurement — see §6** |
| depth per query | top 20 | "no match in the top 20" says nothing about rank 21 |
| candidates read per claim | ≤ 12, in the indexes' own ranking order | a reader shown 160 abstracts for one sentence has not read 160 abstracts |

Every provider call is cached under `sha256(provider|query|top_k)`, so the measurement
re-derives from disk with no network. `from_cache` is recorded per call.

---

## 2. The eight papers

Both gates open, `crossref` + `arxiv`, 2026-09-16.

| paper | cutoff | source / state | claims | queries | raw | distinct works | pre-cutoff | shown | proposals | concerns | bound | route state |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `0c06a98d7c818f6f` | — | none / UNKNOWN | 3 | 50 | 960 | 598 | 0 | 0 | 0 | 0 | 0 | SEARCH_COMPLETED_NO_MATCH_FOUND |
| `2024-icml-sapg` | — | none / UNKNOWN | 3 | 22 | 440 | 328 | 0 | 0 | 0 | 0 | 0 | SEARCH_COMPLETED_NO_MATCH_FOUND |
| `5993d35ff0996b52` | 2024-11-21 | crossref_published / ESTABLISHED | 3 | 29 | 560 | 476 | 299 | 36 | 0 | 0 | 0 | SEARCH_COMPLETED_NO_MATCH_FOUND |
| `acl` | 2026 | proceedings_year / YEAR_ONLY | 3 | 42 | 840 | 555 | 357 | 35 | 3 | 0 | 0 | SEARCH_INCONCLUSIVE |
| `apt-icml` | 2024-01-22 | arxiv_submission_v1 / ESTABLISHED | 2 | 33 | 600 | 283 | 111 | 12 | 3 | 0 | 0 | SEARCH_INCONCLUSIVE |
| `cvpr` | 2025-06-10 | crossref_published / ESTABLISHED | 3 | 48 | 809 | 528 | 369 | 12 | 0 | 0 | 0 | SEARCH_COMPLETED_NO_MATCH_FOUND |
| `iclr` | 2024 | proceedings_year / YEAR_ONLY | 3 | 48 | 803 | 515 | 363 | 12 | 0 | 0 | 0 | SEARCH_COMPLETED_NO_MATCH_FOUND |
| `sanchez24a-icml` | 2024 | proceedings_year / YEAR_ONLY | 3 | 34 | 680 | 515 | 204 | 36 | 0 | 0 | 0 | SEARCH_COMPLETED_NO_MATCH_FOUND |
| **total** | 3 ESTABLISHED, 3 YEAR_ONLY, 2 UNKNOWN | | **23** | **306** | **5,692** | **3,798** | **1,703** | **143** | **6** | **0** | **0** | 6 no-match, 2 inconclusive, **0 blocked** |

**0 endpoint-verified concerns and 0 structurally bound prior-art relations**, and no paper
received a novelty conclusion of any kind.

### The deterministic half, with the review gate CLOSED

The same protocol with `SH_ALLOW_LITERATURE_REVIEW=0`, entirely from cache:

| | claims | queries | raw | distinct works | pre-cutoff | shown | proposals |
|---|---:|---:|---:|---:|---:|---:|---:|
| gated (reviewer on) | 23 | 306 | 5,692 | 3,798 | 1,703 | 143 | 6 |
| deterministic only | 23 | 144 | 2,452 | 1,810 | 892 | 0 | 0 |

All eight papers still reach `SEARCH_COMPLETED_NO_MATCH_FOUND`, in about a tenth of a
second each. That is the property every model channel in this system has and the reason
this one can be assessed at all: **the route runs, and adjudicates nothing semantically,
with no model in it.** The claim count is identical because the claims are the paper's own
sentences; the query count doubles because the proposer adds families, and the candidate
count follows the queries.

### And the reviewer is not reproducible, which is measured rather than assumed

Two full gated runs, hours apart, same code except the self-identity refusal:

| | run A | run B (reported above) |
|---|---|---|
| proposals received | 4 | 6 |
| `apt-icml` on arXiv:2308.03449 (K-prune) | `POSSIBLE_OMITTED_BASELINE` -> an endpoint-verified concern | `RELATED_BUT_MATERIALLY_DIFFERENT` -> `relation_not_concerning` |

**The same candidate, both ends verified identically, and two different relations.** What
varied is the reading, which is exactly why `relation` is recorded as the reviewer's
proposal and why `ENDPOINTS_VERIFIED_LITERATURE_CONCERN` says the overlap is a reading.
A referee reading run A would have been asked a fair question — K-prune really is earlier
work on structured pruning of pretrained encoder LMs — and a system that printed it as a
fact about the paper would have been printing a coin flip.

---

## 3. What the numbers mean, term by term

- **cutoff** — the target's earliest defensible public date. Sources in fixed precedence:
  its own arXiv stamp on page one, then a Crossref DOI lookup, then a title match against
  an index, then — YEAR ONLY — a publication statement on page one. Every offer is recorded
  beside the one chosen.
- **pre-cutoff** — distinct works, after canonicalisation, established as public BEFORE
  that cutoff. Only these are eligible to be prior art, and only these are shown to a
  reader.
- **shown** — distinct works a reader actually saw. Different from *proposals*: a reader
  that read twelve abstracts and proposed nothing has done the work and found nothing.
- **concerns** — `ENDPOINTS_VERIFIED_LITERATURE_CONCERN`: two real works, a passage located
  in the text this harness retrieved, a verified chronology, and an overlap that is still
  the reviewer's reading.
- **bound** — `STRUCTURALLY_BOUND_PRIOR_ART`. Needs a deterministic binding basis AND a
  priority claim to bind. **Zero over this corpus, printed as zero.**

---

## 4. Concrete examples

### 1. A paper is not prior art for itself — `acl`

The corpus's first endpoint-verified concern was wrong, and hand-checking it is what found
the defect. Against *"We introduced FINCHAIN, a symbolic benchmark for verifiable
Chain-of-Thought financial reason- ing, spanning 58 topics across 12 domains and three
difficulty levels"* (`P7:0-144`), the reviewer proposed:

> **FinChain: A Symbolic Benchmark for Verifiable Chain-of-Thought Financial Reasoning**
> — arXiv:2506.02515, 2025-06-03 · `LIKELY_DIRECT_PREDECESSOR`
> *"Same title, same benchmark name, same exact figures (58 topics, 12 domains) … this
> arXiv posting predates the proceedings and appears to be this paper's own preprint."*

Every endpoint verified: the claim re-minted, the passage was in the retrieved abstract,
and 2025-06-03 really does predate a 2026 proceedings. **It is the paper's own preprint.**
Reported as prior art it would have been an accusation about the authors' scholarship.

`literature.same_work` now refuses a candidate carrying an identifier the paper prints on
its own first page, or the paper's own title. That does not catch this one — `acl`'s
extracted title is the proceedings header, so there is no title to compare — so a second
rule does: with no title this harness will stand behind, a candidate whose title carries a
name the claim sentence says the paper INTRODUCES is refused as
`candidate_may_be_the_target_itself`, with the reason said out loud. A paper WITH a usable
title is not subject to that rule, because there identity is already settled and a shared
name is a terminology collision or a real conflict — a referee's call.

### 2. A name collision is not prior art — `sanchez24a-icml`

Against a claim about Pythia, the LLM suite, the search returned:

> **PYTHIA 6.4 Physics and Manual** — 2006-03-22 · `TERMINOLOGY_COLLISION_ONLY`
> *"Both use the name 'Pythia'; the claim's Pythia is Biderman et al.'s LLM suite, while
> this candidate's PYTHIA is an unrelated high-energy-physics event generator."*

Refused as `relation_not_concerning`. The vocabulary earning its keep: a closed list with
`TERMINOLOGY_COLLISION_ONLY` in it lets a reader say "same word, different thing", and
that answer costs a human nothing.

### 3. A real earlier work, and a reading that did not hold still — `apt-icml`

> **Accurate Retraining-free Pruning for Pretrained Encoder-based Language Models**
> — arXiv:2308.03449, 2023-08-07, public five months before this paper's own arXiv v1

Genuinely earlier, genuinely adjacent, and reported with `POSSIBLE_OMITTED_BASELINE` on one
run and `RELATED_BUT_MATERIALLY_DIFFERENT` on the next. See the variance table above.

### 4. A quotation that was not in the retrieved text — `apt-icml`

> **Towards Robust Pruning: An Adaptive Knowledge-Retention …** · `INSUFFICIENT_EVIDENCE`

Refused `candidate_quote_unresolved`: the passage the reviewer quoted is not in the
abstract this harness retrieved. `claims.mint` for a candidate work — the writer supplies
the passage, the harness finds it, and a search-engine snippet or a recollection is not
the work.

---

## 5. Refusals, and why each is the right one

Six proposals, six refusals, and the reason each is right:

| refusal | n | why it is the right answer |
|---|---:|---|
| `candidate_may_be_the_target_itself` | 2 | the candidate is almost certainly this paper's own preprint, and this harness cannot prove it either way without a title it trusts. Reporting it as prior art is an accusation; the refusal names what a human should glance at |
| `relation_not_concerning` | 3 | the reviewer's own answer — a terminology collision, or related-but-different work — raises no prior-art question. These are real answers, and the commonest right ones |
| `candidate_quote_unresolved` | 1 | the quoted passage is not in the text this harness retrieved |

**Zero refusals for chronology**, because chronology filters earlier: a reader is only ever
shown pre-cutoff candidates, so `candidate_not_earlier` cannot arise in a gated run. It is
exercised on fixtures instead
(`test_candidate_later_than_target_cannot_support_a_prior_art_concern`).

**And zero level-3 bindings.** `acl`'s and `apt-icml`'s claims are contribution statements
("We introduced FINCHAIN…", "We design APT…"), not priority claims, so even a deterministic
binding basis could not lift them: `STRUCTURALLY_BOUND_PRIOR_ART` needs the paper to have
claimed to be FIRST at something narrow. That is the rule refusing to be satisfied, and the
number is printed as zero rather than improved.

---

## 6. Three limits, all of them ours

**The cutoff cannot always be established.** Two of the eight papers could not be dated at
all, and for them no chronological claim is made in either direction: every candidate is
`TARGET_CUTOFF_UNKNOWN` and nothing is shown to a reader. The cause is extraction, not the
papers: `doc.title` comes back as an author line (`"Jayesh Singla * 1 Ananye Agarwal * 1
..."`) or as publication boilerplate (`"Proceedings of the 64th Annual Meeting of the
Association..."`), and `literature.target_title` refuses such a string rather than
searching an index with it. **Searching for the wrong title does not fail — it succeeds,
with somebody else's paper, and then dates this one.**

**OpenAlex now meters its API.** Asked with a polite-pool `mailto`, it answered for a while
and then returned

> `HTTP 429 {"error":"Rate limit exceeded","message":"Insufficient budget. This request
> costs $0.001 but you only have $0 remaining. Resets at midnight UTC..."}`

A 429 is two different facts and the body says which: "slow down" is transient, and
"insufficient budget" is this host lacking a paid credential, which no amount of waiting
inside one run fixes. `literature_providers._fetch` reads the body and records the second
as `UNAUTHENTICATED`, so the record does not tell a reader to retry something that cannot
succeed today. The measurement's declared protocol is therefore **crossref + arxiv**, two
independent indexes, which is what §4 of the design asks for where practical.

**The search is bounded, and the bound is the whole meaning of the result.** Three claims,
at most eight queries each, two indexes, top 20, twelve candidates read. A paper whose
nearest predecessor sits at rank 21 of a query nobody issued is not reported, and nothing
in this route pretends otherwise.

---

## 7. What no paper got

No paper received a novelty conclusion, and none can: there is no code path that produces
one. `guarantees.NOVELTY_ASSESSED` remains a SCIENTIFIC NON-GUARANTEE and holds on no
input, which is asserted by a sweep over every boolean the module accepts
(`tests/test_guarantees.py::test_novelty_is_never_reported_as_checked`). The reader-facing
sentence is *"a bounded prior-art search runs and may raise a concern a referee must
adjudicate; novelty itself is never established, because a search that completed and
matched nothing is a fact about the search."*

---

## 8. Tests

| kind | file | count | reaches a third party |
|---|---|---|---|
| pure | `tests/test_literature_route.py` | 55 | no |
| live | `tests/test_literature_live.py` | 6 (+1 skipped) | yes, marked `network` |

The split is deliberate and the line is drawn where it matters: every RULE — chronology,
canonicalisation, endpoint verification, what a silence means, what the driver strips —
is checked on fixtures with no network, because those must hold whether an index is up or
down. The live tests check only that the parsers still match what the indexes actually
send, and they SKIP on a network failure and FAIL on a parse failure: an index being
unreachable is a fact about this host, and an index answering while our parser does not
understand the answer is a defect in this repository.

---

## 9. Artifacts on disk

Per paper, under `projects/<pid>/literature/`:

| file | what it holds |
|---|---|
| `<pid>.search.json` | the whole record: cutoff and every date offered, protocol, claims with their addresses and exact queries, every provider call, the deduplicated candidate set, and every adjudicated fact with its refusal |
| `<pid>.search.driver.json` | the seal: content hash, disposition, the summary counts, and one record per model call (command, tool policy, return code, seconds, and the failure when there was one) |
| `cache/<key>.json` | the raw provider responses, so the measurement re-derives with no network |

The candidate records keep every work's IDENTITY and keep the retrieved TEXT only for
works a reader was shown — `literature.trim_for_record`. One paper's file came to 976 KB
before that, almost all of it abstracts nobody looked at. The caches are large (2–11 MB
per paper) and `projects/` is gitignored; they are a reproducibility store, not a
deliverable, and belong in the release ZIP's exclusions.
