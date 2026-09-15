# Revision, September 2026 — the referee's own review of itself

This revision was not a feature pass. Its premise was that the system's reader-facing
output was wrong in ways the system could not see, and the work began with a
reconnaissance of the code, the shipped corpus, two external repositories and the
2025-2026 literature before anything was changed. What the reconnaissance found, and what
it cost to fix, is the substance of this document.

**One number sets the tone.** Over the seven shipped reviews, the funnel's terminal term —
*"N settled a question about the paper"* — read 20. The honest value is 0. All twenty were
citation re-verifications, and every one of the seven reviews printed the very sentence its
own findings disputed under a heading reading `## What held up`.

Tests: **1029 → 1571** (+542). Module self-checks: 29 → 36. Nothing under `projects/`,
`manuscript/` or `reports/` was modified; `tools/loop.py`'s write guard asserts it.

---

## 1. The defects, in order of how badly they misled a reader

Each was measured against the shipped corpus before it was fixed. "Measured" below means a
script ran over `projects/*/` or `papers/*.pdf` and printed the number.

### 1.1 A false machine attestation — the worst of them

`pdf._FIGURE_CAPTION` matched any line *beginning* "Figure N", so in-text
cross-references were ingested as figure captions. CVPR yielded 12 figure objects for 8
real figures, including `'Figure 4 shows the visual comparison with real-world'`.

The consequence is not cosmetic. Against the shipped `projects/cvpr/paper/doc.json`:

```
verify_evidence('shows the visual comparison with real-world', 'F5') -> 'caption_verified'
  "Figure caption F5 (page 6, 'Figure 4') of the parsed paper contains the quoted text
   verbatim. A CAPTION NAMES A FIGURE; it does not report the figure's plotted values..."
```

F5 is body prose. The harness attested, in its own voice, that a sentence of running text
was a figure caption — invariants 1 and 2 both violated, by the one mechanism the whole
system rests on. `pdf._equation_body` had been hardened against exactly this species of
defect and returns `""` rather than attaching a number to whatever text precedes it; the
figure path was never hardened. It now requires a delimiter after the number (measured
63/63 correct across every caption-shaped line in three pilot papers), and the same call
returns `'unverified'`.

The harvested cross-references became `PaperDoc.crossrefs`, a typed list carrying **what
the prose CITES and never what exists** — no `exists` field, no `resolved` field, no
severity. CVPR 23, ICLR 46, APT 36, where there had been none.

### 1.2 Twenty resolutions that resolved nothing

The paper-only route has two branches and they establish different things. Re-evaluating a
composition the paper printed settles whether the paper's arithmetic holds. Re-verifying
the *quotation* behind a concern establishes only that the concern cites the paper
accurately — and `stages/discover._paper_only_outcome` said so in its own reason string
while returning the same disposition for both.

Measured: all 20 paper-only outcomes in the corpus were the second kind, 0 the first. So
`PAPER_INTERNAL_EVIDENCE`, which `EVIDENCE_ABOUT_THE_PAPER` admits, was set for a route
that decided nothing; `concerns_the_paper` was True; `resolution_state` was
`RESOLVED_FROM_PAPER`; the disputed sentence was filed under `## What held up`; and
`targets_settled` counted it.

Fixed by splitting the disposition (`CITATION_VERIFIED_ONLY`), giving it its own evidence
state (`CITATION_VERIFIED`) whose resolution is **UNRESOLVED**, and reporting the count
separately as `targets_citation_verified_only`. Re-rendered against the real `iclr` target
set, the sentence moves from `## What held up` to `## Central claims this review did not
check`, and the funnel line goes from "2 settled a question about the paper" to "0".

A second consequence had to follow. `PAPER_INTERNAL_CHECK` sat in `planner._RESOLVING`,
the set of routes that close a question with nothing running — so wherever a contradiction
lens offered it *and* an executable route existed, the execution was **suppressed by a
citation re-verification**. That is invariant 23 violated in the direction nobody was
watching. It is out of that set now, and still taken when nothing better exists.

### 1.3 Thirty-nine false sentences about our own extraction

`harness_addressable` is a conjunction of three requirements and every failure of any of
them became `INFEASIBLE_ADDRESSING` → `ADDRESSING_BLOCKED` → `EXTRACTION_LIMITATION`,
whose reader-facing sentence is *"this review could not build a re-derivable address for
the claim."*

Measured: that sentence appears 39 times across six of seven reviews, and **all 52 targets
carrying it have a RESOLVED address.** In `apt-icml.review.md` it sits two lines under the
printed address it claims not to have. The real blocker in 52 of 52 was the third
requirement: no verification route applies.

Now three states, because they are limits of three different things — our extraction, the
paper's reporting, our method inventory — with `DiscoveredObject.addressing_blocker` naming
which applies. Re-running the discover phase over the corpus: `EXTRACTION_LIMITATION`
52 → 0, split into 39 `NO_ROUTE_AVAILABLE`, 18 `REPORTING_LIMITATION`, 5
`ADDRESS_UNRESOLVED`. Extraction was in fact fine for 862 of 867 objects.

The same re-run exposed a related misattribution: CVPR's 5 `ARTIFACT_BLOCKED` targets —
"no usable artifact exists for this target" — belong to a paper that **does** publish a
repository (`github.com/wuyang98/weathergen`), which was cloned. They have no route, not
no artifact.

### 1.4 A published coverage rate that rewards a worse extractor

`target_addressability_rate = targets_addressable / targets_discovered`. Both terms are the
same `ts.objects` list, and `discovery` mints an object per reported number **only if the
reference resolved** — so an extraction failure removes rows from the denominator and
*raises* the rate. It read 0.9558 corpus-wide and is published in `manuscript.tex`.

`harness/coverage.py` replaces it with a denominator counted off the paper: `surface(doc)`
takes a `PaperDoc` and nothing derived from the review, and `measure` takes its numerators
as plain address **strings**, so a numerator cannot redefine a denominator. Over the same
documents: **0.33 addressed, 0.018 examined**, against 1908 addressable units. The old
number is kept under the name `objects_addressable_rate` with a limitation entry saying
what it is.

Two numerators, not one, because 629 of 867 corpus targets ended `NOT_INVESTIGATED`:
"we minted an address for this" and "we pursued a route for it" are different claims.

### 1.5 A panel of one model, presented as a panel

`prompts/audit.LENSES[lens]["model"]` was declared and **never read**. All four lenses ran
on whatever the CLI defaulted to. The corpus records the collapse from the other side:
`reviewer: claude-sonnet-5` on all four lens files, including `overclaim`, whose declared
model is `opus`. The key existing is precisely why nobody noticed.

`audit_driver` now passes `--model` per lens, an explicit `--disallowedTools` deny-list
derived from the allow-list, `--restricted --strict-mcp-config` so host plugins and MCP
servers cannot reach a lens, and `--output-format json` so the envelope's *reported* model
is recorded rather than discarded. Verified:

```
overclaim      --allowedTools "Read,WebSearch"  --model opus    --restricted
protocol       --allowedTools "Read"            --model sonnet  --restricted
```

**The ceiling, stated rather than implied:** this is one vendor's CLI, so "independent"
means four processes with four contexts and two distinct model names. It is not
cross-family diversity and no arrangement of that table would make it so.

### 1.6 The lenses read a truncated paper

`pdf.render_sections` divides a character budget across sections. Measured presented
fraction of the extracted section text: **0.384** (sanchez), 0.411 (acl), 0.489 (iclr),
0.589 (apt), up to 0.745 — while every review's scope line said *"4 independent lens(es)
read the paper."* This is the ceiling on any recall claim the system makes and it was
unmeasured. `pdf.section_presentation` measures it, `CoverageReport` carries it, and the
scope line now states it. Nothing raises the budget: the number is reported, not fixed.

### 1.7 Two ways to convict a paper of nothing

- **The calibration hole.** `write_probe` falls back to the identical-arms noise-floor
  template when a spec carries neither script nor command, and a hand-written
  `runs/<pid>/spec.json` declaring `"provenance": "driver"` reached it. `driver` is
  admissible, so the noise floor's own accuracy was reconciled against the paper's printed
  cell and was eligible to produce `FAILED_REPRODUCTION`. `overall_verdict` read nothing
  about `ProbeResult.calibration` at all. Closed on both sides of the seam: `run_probe`
  refuses such a spec (`decision="spec_incomplete"`), and `claim_status` /
  `overall_verdict` / `triage` refuse a calibration reconciliation in either direction.
- **The ceiling was single-enforced in the acquitting direction.** `claim_status`'s
  `RESOLVED_VERIFIED` branch performed no provenance check. It was safe only because
  `local_exec.reconcile` refuses to *emit* that status inadmissibly — so a hand-edited or
  upstream-buggy artifact reached `VERIFIED_SUPPORT`, the one state that unlocks the words
  "verified", "supported", "confirmed". Invariant 3 says "in either direction"; now it is.

### 1.8 The vocabulary that shadowed itself, and eight copies of one rule

`artifacts.py` imported `taxonomy.RESOLUTION_STATES` and then rebound the same name to the
`ClaimRef` address vocabulary, so `ScientificFinding.resolution_status`,
`ReviewQuestion.resolution_status` and `LedgerEntry.resolution_state` — three fields a
reader inspects the schema of — all documented themselves as
`"resolved | not_found | ambiguous | malformed | span_mismatch"`. The renders were right
only because a gloss table hardcoded the real tokens, which means the two definitions could
drift with nothing failing. Renamed to `REFERENCE_RESOLUTIONS`.

The provenance ceiling was written out as a literal `("driver", "repo_exec")` at **eight**
sites, and the guard against drift was a source-text grep over one of them. It is
`harness/provenance.py` now, the test is an identity assertion, and a ninth site that
writes the tuple again fails a source sweep. Fixing it surfaced a fail-open I had just
introduced myself: `.strip()` in `admits()` would have normalised `" repo_exec"` into the
authors' own code. The ceiling is exact.

### 1.9 The question merge keyed on a page number

The merge key was the lens's own `evidence_ref`, and `prompts/audit.py` *instructs* a lens
to write `p7` for a prose claim. `p7` is a page. Measured: 75 of 98 corpus findings write a
`p<N>` ref, 21 (paper, ref) pairs are shared by more than one finding, and **3 of the 6
resulting merges are false** — two of them *raising* the merged question's materiality,
because a merge takes the maximum its sources asserted. A coincidence of page numbers
promoted a concern the lenses had not promoted. The key is the address `claims` minted now,
and a finding whose address did not resolve merges with nothing.

Separately, `discovery` keyed the question lookup on `from_finding` — the first source only
— so a merged question's second object got `question_id=""` and never entered
`evidence_refs`. The documented mitigation *"evidence_refs lists every target so nothing is
hidden"* did not work.

### 1.10 The bibliography, shattered

`_SECNO` admits a single capital, so a reference line whose author initial lands at line
start looks exactly like an appendix heading. APT's References section held 694 of ~17,200
characters; 16.5 KB had become five bogus sections. Sections 59 → 37 on that paper, 27 → 21
on CVPR, 25 → 13 on ICLR.

---

## 2. What is new, and what each thing may see

Six pure modules, each a separate file so that what it may read is a **signature** rather
than a convention.

| module | lines | sees | writes | decides |
|---|---:|---|---|---|
| `provenance.py` | 89 | nothing | the ceiling, once | nothing |
| `outcome.py` | 468 | `claim_status`, questions, outcomes — in four disjoint folds | the four reader-facing rows | nothing |
| `coverage.py` | 554 | `PaperDoc` for the denominator, address strings for the numerators | `ReviewSurface`, `CoverageReport` | nothing |
| `docintegrity.py` | 952 | `PaperDoc` ONLY | `DocumentObservation` × N | nothing; gates nothing |
| `guarantees.py` | 1012 | every artifact above | `ReviewGuarantees`, incl. `unmet` | nothing |
| `preflight.py` | 236 | the PDFs' bytes | a batch's paper accounting | refuses a batch |

Plus `tools/loop.py` (453 lines): the acceptance rule as one command.

### 2.1 The four reader-facing rows

The review now opens with `## Review outcome`:

```
- **what was established** — `CONCERNS_RECORDED`: 9 evidence-verified concern(s); none was
  established as a material failure, within the scope checked.
- **what was resolved** — `NO_QUESTION_SETTLED`: 6 raised · 0 settled · 6 open; the
  concerns stand as questions for a human reviewer.
- **what execution produced** — `EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE`: execution was
  attempted and did not produce admissible evidence, so nothing about the paper follows
  from it. Exact reason: target TGT-CLM-P158528-8590: the probe that ran was synthesized,
  not the paper's own code ...
- **what this is an assessment of** — `CENTRAL_CLAIMS_LEFT_UNCHECKED`: 1 central claim(s)
  checkable and unchecked; the rows above assess less than the paper's main argument.

No row above says the paper is correct, that it failed, or that it is inconclusive.
```

**Disjointness is the guarantee.** `finding_state` reads `claim_status` and a count of kept
findings and *nothing* about execution, so no blocked, refused, inconclusive or
inadmissibly-provenanced run can move what the review established — asserted by sweeping
every execution state against every finding state.

`## Reproduction status` is gone. It printed a bold `**NOT_VERIFIED**` beside
`Targets: 5 addressing blocked, 1 inconclusive, 25 not attempted`: `NOT_VERIFIED` belongs
to two different vocabularies in this codebase and reads as a verdict in both,
`inconclusive` reads as a property of the paper, and that section's gloss said *"see the
chain below"* in an artifact that has no chain — true of six of seven shipped reviews.

The exact reason is now cut at a **sentence boundary**. It used to be cut at 200 characters
mid-clause, which took the words *"is evidence about the mechanism, not a reproduction of
the value printed at..."* off the end of a line that opened with `|delta| 70.2218` and left
a skimming reviewer looking at what appeared to be a catastrophic reproduction failure.

### 2.2 The document-integrity layer, and the constraint that shaped it

A naive "referenced but missing" check over the shipped documents produces **12 false
"broken reference" claims and 0 true ones** — CVPR tables 2, 4 and equations 1, 6; ICLR
tables 12, 13; APT tables 5, 12 and equations 1, 3, 5, 6 — all present in the papers and
absent only from what extraction recovered. Shipping that as referee-facing findings would
make a colour a property of extraction quality, which is invariant 17's defect.

So `DocumentObservation.about` is **required with no default**, one of `PAPER` or
`EXTRACTION`, and `CROSSREF_UNRESOLVED` can only ever be the second — worded as an
admission about this harness. Measured over the seven documents: **73 observations, every
single one `about="EXTRACTION"`.** The layer claims nothing about any corpus paper, which
is both the correct answer and an honest limitation.

The type carries no severity, confidence, scientific class or verdict *field*, and none of
the decision functions has a parameter that could receive one.

### 2.3 Guarantees, checked from artifacts

`guarantees.py` splits three groups, and the split is the design: PROCESS guarantees this
system enforces in every run (a `holds=False` here is a **harness defect** and the only
thing reaching `unmet`); CONDITIONAL properties true only when a gate was open (grading
being off is a configuration, not a defect); and SCIENTIFIC non-guarantees, `holds=False`
on every input by exhaustive sweep.

Rendered, honestly:

> Machine-enforced here: every evidence pointer re-read off the paper · the provenance
> ceiling held both ways · one conjunctive gate authorized every run · no infrastructure
> failure convicted · every refusal named per target · no mechanism raised a severity · no
> model wrote the decision · no experiment downscaled to fit.
>
> Not established here: severity not independently graded · lens tool policy unrecorded ·
> the submission not judged as a whole · the authors' code was not run · the addressable
> surface not fully examined · the lenses saw part of the extracted prose · issue recall
> neither claimed nor measurable · accuracy unmeasured: no adjudicated ground truth ·
> nothing here says the paper is correct · novelty and prior art not checked at all · not
> every important experiment is executable · a first pass, not a substitute for a referee.

It found a defect the moment it ran: `unmet=['ACCOUNTING_REPRODUCIBLE']`, because
`guarantees.derive` was being called before the ledger was attached. That is what a
guarantee checked from an artifact is for.

### 2.4 A conservation law that caught my own wrong assumption

`evaluation.assert_conservation` reported `funnel term resolved=12 exceeds completed=11`.
The funnel was right and the law was wrong: **the first five terms nest and the sixth does
not.** A question settled from the paper's own printed arithmetic is resolved with nothing
launched, which is the whole point of the cheap-route-first policy. The law asserts the
chain through `completed` and bounds `resolved` by `discovered`.

### 2.5 The report's length guarantee, enforced

Invariant 19 claims the report is bounded *by construction* and was asserted as
`len(text) < 9000` over **synthetic** reports whose worst case is 3.2 KB, while real
reviews already ran 6.4-9.4 KB. The guarantee was "short on our fixtures", and every
section added since had been added against a test that could not feel it.
`report._bounded` drops whole sections from the lowest priority upward, names what it
dropped, and never drops the outcome block, the established failures, the findings or the
scope. Measured now: 9.9-11.0 KB, bounded at 12 KB.

---

## 3. What was learned from the two external repositories

### PaperGym (ZJU-REAL)

PaperGym is a benchmark and training environment, not a referee, and most of it is
non-transferable by construction: its rubric generation needs a model, its reward mixing
(`0.2*general + 0.8*combined`) is the scalar collapse invariant 20 forbids, and its
LLM-as-judge scoring needs the ground truth this project does not have. Four ideas
transferred:

1. **Closed-vocabulary desiderata with `passed = len(violations) == 0`** — do not ask a
   model for a score, ask which items of a fixed enumerated failure vocabulary are
   violated, and let deterministic code define pass as the empty set. This is the shape
   this codebase already uses for `taxonomy.classify` and `planner.classify`, and seeing it
   arrived at independently is why the delegation work tightened the same contract on the
   grader rather than loosening it.
2. **Tie / no-majority / parse-error kept as three outcomes rather than one failure**, and
   ties in the denominator so an abstention can never inflate a rate. Independent
   confirmation that the `resolution_status` × `evidence_state` split is what makes an
   aggregate honest; adopted directly in `assert_conservation`.
3. **Two-tier scoring kept separate rather than mixed** — argument-form criteria judged
   apart from content criteria, and reported side by side. What was taken is the
   *separation*; the weighted sum was explicitly not.
4. **Reliability without ground truth** — N-round flip rate and per-criterion agreement
   need no adjudicated labels, and are the one quantitative claim this system could
   legitimately make about its own judgement. **Not implemented**: it costs N× the lens
   spend, so it is a periodic evaluation and not a per-paper measurement. Recorded here as
   the most valuable unbuilt thing the reconnaissance found.

Explicitly rejected: a claim-blind rubric as a second question source would need a model
inside a phase that consults none, and a rubric-derived question counted toward any
threshold would be "no variance = MAJOR" wearing new clothes.

### academic-research-skills

The thing worth knowing first: **it has no executable figure/table checker at all.** The
repository says so itself — its `figure_table_trace[]` block is *"a prose contract … not a
machine-validated schema and adds no lint, no JSON Schema, and no gold fixture"*. So
`docintegrity.py` is entirely ours, with no reference behaviour to diff against and no
inherited false-positive profile. Its prompt-level rules are nevertheless the best
specification of what a referee should check that the reconnaissance found.

What transferred was its **engineering discipline**, which is where its real value is:

1. **Three-class reduction with `unresolvable` never collapsed into `false`.** Its
   citation gate keeps "provably absent" apart from "we could not tell", and biases toward
   refusing to conclude. This is the same posture as the provenance ceiling and it
   validated splitting `EXTRACTION_LIMITATION` into three rather than two.
2. **Signal class bound at the REGISTRY, not at the call site**, so a structurally
   heuristic check is excluded from the strict set *by class* and a forgotten keyword
   cannot fail it open. `DocumentObservation.about` being required with no default is this
   idea: the classification is not something a caller may omit.
3. **A coverage report that carries `semantic_extraction_coverage:
   "not_machine_detectable"` unconditionally**, so a clean report cannot be read as
   evidence that everything was found. Adopted verbatim in spirit as
   `CoverageReport.semantic_coverage`, and it is the single most useful thing borrowed.
4. **Missing / not-run / stale emits UNRESOLVED and never means zero gaps.** An empty
   surface yields `None`, never 1.0.
5. **One tuple as the single source for two checks "so the two can never disagree."** Two
   agents here independently implemented the same prose-budget arithmetic; `coverage`
   delegates to `pdf.section_presentation` for exactly this reason.
6. **Match broadly, then guard narrowly, with named guard windows** and a comment recording
   which review round found each false positive. The regex discipline throughout the
   extraction work follows it.
7. **Asymmetric severity by direction** — an orphan in-text citation fails, an uncited
   reference entry warns. Calibration worth having before any citation check ships.
8. **Exit code 3 = "no failure, but something was not checked."** The same distinction as
   GREEN-versus-NOT_VERIFIED, arrived at independently, in a different domain.

---

## 4. What remains genuinely unresolved

Honest, and in descending order of how much it limits what may be claimed.

1. **The seven shipped reviews predate all of this.** Every number in §1 is a measurement
   of what a *rerun* would change, not a rerun. `CLAUDE.md`'s known-limitations section now
   opens with a table of five corpus numbers that are wrong and what they become. Until the
   run happens, `projects/` is the record of September 2026 and nothing else.
2. **RESOLVED — and this entry was wrong.** The eighth paper is
   `0c06a98d7c818f6f.pdf` (`d6295997a2ed`) and it **is a CVPR paper**: it carries the CVF
   open-access watermark verbatim. This entry originally called it
   "a spatial-transcriptomics paper, not a CVPR selection", which inferred a venue from a
   subject. `papers/CVPR.pdf` is still not a new paper — it is the already-reviewed
   WeatherGen document — so the set is 7 + 1 with two CVPR papers in it, deliberately not
   deduplicated. See `docs/RUN_2026-09-08_eight_paper.md`.
3. **The seven adversarial verification agents for this revision did not run** — they hit a
   session limit. Their subjects were verified by hand (the false attestation reproduced and
   confirmed closed, the per-lens model confirmed reaching the argv, every vocabulary split
   re-measured over the corpus, the conservation law confirmed catching a real
   disagreement) but "an independent adversarial pass found nothing" is not a claim this
   revision can make.
4. **The `about="PAPER"` half of the integrity layer is fixture-only.** 73 of 73 corpus
   observations are about extraction. The route from a PAPER observation with a
   *demonstrated* scientific consequence into the findings channel is deliberately not
   implemented.
5. **`extraction_version` is a declaration, not an enforcement.** Nothing in
   `claims.resolve` refuses a reference minted under a different parse. Safe today only
   because a cached doc is never re-parsed. **Whether the eight-paper run re-parses is
   therefore a deliberate decision:** re-parsing invalidates the 4 stored `F<n>` and 17
   `T<i>:r<r>:c<c>` refs in the existing lens files.
6. **Reliability is unmeasured.** No flip-rate, no per-criterion agreement. It needs no
   ground truth and is the one quantitative claim about judgement quality that is available.
7. **Structural coverage is 33% addressed and 1.8% examined.** Honest numbers about a
   first-pass screen, and not a statement about how many real problems were found — which
   remains unmeasurable here.
8. Unchanged from before: no paper has been RED under the binary rule; neither the grader
   nor the assessor has run on a corpus; the remote sandbox has never been leased;
   `scientific_class` still falls back to the lens name; novelty checking is architectural
   only; CVPR tables 2 and 4 are still not recovered.

---

## 5. The eight-paper rerun: ATTEMPTED, and it did not complete

**Superseded by `docs/RUN_2026-09-08_eight_paper.md`, which is the authoritative record.**
§5b below is the original readiness assessment, kept because its three decisions were the
ones actually taken. All three were made, the run was launched, and it stopped in the S2
audit phase on a provider session limit resetting at 3am (Asia/Kolkata).

**0 of 8 papers produced a review.** Every case is `waiting`, which is resumable; none is
`error`. That run's `reports/corpus.json` reads `{"completed": 0, "inconclusive": 8}` with
`failure_kind=rate_limited` on all eight entries. Nothing anywhere in this repository may
report a finding, a count or a colour as coming from that run, because it produced none.

What the attempt did settle:

- **The v2 extraction transition is done** for all eight papers. No stale v1 reference is
  carried forward; preflight confirmed 8 files, 8 distinct content hashes, exit 0.
- **The eighth paper is a CVPR paper.** `0c06a98d7c818f6f` (`d6295997a2ed`) carries the CVF
  open-access watermark verbatim, as `papers/CVPR.pdf` (`7bd4c8336fb9`, WeatherGen) does.
  Both are in the set and neither is deduplicated against the other.
- **The delegation path is verified on live output**, which no fixture could do: `overclaim`
  requested `opus` and the CLI reported `claude-opus-5`; `protocol` requested `sonnet` and
  reported `claude-sonnet-5`, under different enforced tool policies. In the archived
  September corpus all four lenses reported `claude-sonnet-5`.
- **Invariant 14 worked.** The rate limit consumed no retry attempt, every case is
  resumable, and the recorded reason says "not by reviewer quality" and names the reset
  time. The harness refused to emit eight reviews built on 0-2 of 4 lenses.

Cost and scale, now measured rather than estimated: **$2.36 and ~11 minutes for two lens
calls**, so the audit phase alone is roughly $35 and 2.5 hours before grading, the
assessor and repository execution. One limit window is unlikely to hold the whole run; it
is designed to be re-entered until `reports/corpus.json` says `complete: true`.

## 5b. Was the system ready? (the original assessment, kept)

**Yes for the machinery, with three decisions to make first.**

Ready: 1571 tests and 36 module self-checks pass; `tools/loop.py` runs the whole acceptance
rule in one command and is green; `run.py preflight` verifies the batch is eight distinct
documents before anything is spent; the report renders end to end on five real corpus
papers through the actual `run_report` path with `unmet=[]`; `evaluation` aggregates the new
layers and its conservation law is satisfied.

Decide first:

1. **Which eighth paper.** The intended 2026 CVPR selection is not on disk. Either add it,
   or run with `0c06a98d7c818f6f.pdf` and describe it as what it is.
2. **Whether to re-parse.** The new extraction is version 2 and the corpus is version 1.
   Re-parsing is the right thing — it is what closes the false-attestation path for the
   corpus rather than only for new papers — and it invalidates 21 stored references, which
   means the lens files must be regenerated, which means `--auto-audit`.
3. **Whether to open the grading and assessor gates.** They have never run on a corpus.
   Leaving them shut reproduces the ungraded decision exactly and is safe; opening them is
   the only way the "second, blinded reviewer" architecture stops being a description.

The first run worth making is a single paper with `SH_ALLOW_REPO_EXEC=0` and
`--auto-audit --auto-grade`, which exercises the real delegation path, the new extraction,
all four report layers and the grader, while executing no third-party code.

---

## 6. What the manuscript must change

Listed, not written. No number below has been produced by a run, and none should be
written into the paper until it has.

**Factually wrong as it stands, because of what §1 found:**

- The panel is described as four lenses with per-lens models. The corpus was four processes
  on **one** model, and `manuscript/check_claims.py` must gain an assertion that the
  manuscript says which.
- `target_addressability_rate` 0.93 is published as a coverage-like quantity. It is
  self-referential and a worse extractor scores higher on it. Either drop it or present it
  under its new name with the caveat.
- Any statement that the lenses read the paper needs the presented fraction beside it
  (0.38-0.75).
- The funnel's terminal term and `targets_settled` must move 20 → 0, and the
  citation-verified count reported separately.
- Every reproduction of a review excerpt showing `## Reproduction status` or the
  `EXTRACTION_LIMITATION` sentence is now stale.

**New material the requirements ask for:**

- 2026 positioning. The reconnaissance's literature pass identifies **FactReview**
  (arXiv:2604.04074) as the nearest neighbour — it already executes authors' code inside
  review, separates artifact problems from claim evidence, and abstains conservatively — so
  code execution in review must not be presented as new. What appears unclaimed is the
  **provenance ceiling as an enforced admissibility rule** (applied twice), the
  **conjunctive authorization gate**, the **refusal vocabulary with a separate necessity
  axis**, and the **non-collapse of the axes**. Full autonomy is a *liability* to claim in
  2026, not a contribution: CVPR 2026 bans LLM reviewing outright and ICLR 2026 assigns
  full responsibility to the human reviewer. The defensible framing is that autonomy is the
  **precondition for auditability** — a verdict re-checkable by hand from a ledger is only
  possible if no human judgement entered the chain.
- The artifact-present versus artifact-absent methodology, now a first-class axis.
- The document-integrity layer, with its measured 12-false-positives constraint stated as
  the reason for the `PAPER`/`EXTRACTION` split.
- The coverage definition, with the explicit denominator and the `semantic_coverage` caveat.
- The guarantees / non-guarantees model.
- The revised terminology around inconclusive execution.
- The eight-paper results, **after** they exist.

Every citation the literature pass surfaced is in
`<session scratchpad>/notes/literature.md` with the URL it was fetched from, and each
should be re-verified before it enters `refs.bib`.
