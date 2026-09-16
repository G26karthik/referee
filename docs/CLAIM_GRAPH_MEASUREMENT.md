# The claim/evidence graph, measured on all eight papers before anything is rewired

Status: **built, measured, and NOT wired into any decision.** `harness/claimgraph.py`
exists, its self-check passes, and `materiality.basis_for_ref` and
`discovery._centrality` are untouched. This document is the measurement the brief asked
for before the decision rule is replaced, and the measurement changed what the right
answer is.

## 1. What was built

Two node kinds and a small edge vocabulary, derived from `PaperDoc` and nothing else. No
lens output, no finding, no target, no grade and no outcome can reach any function in the
module, and `build(doc)`'s signature is what says so.

| node | what it is |
|---|---|
| `CLAIM` | a sentence the paper prints that states a number, makes a comparative assertion, or cites a numbered object. `HEADLINE` in the Abstract or Conclusion, `SUPPORTING` elsewhere — a position in the document, not a judgement |
| `RESULT` | a table cell, a reported quantity, a table, a figure, an equation |
| `COMPARISON` | an experimental comparison the paper set up: one metric, one benchmark, two or more methods |

| edge | from → to | meaning |
|---|---|---|
| `STATES` | claim → result | this sentence printed this number |
| `CITES` | claim → result | this sentence cites Table 3 / Figure 2 / Equation 4 |
| `IN_TABLE` | table → cell | this cell belongs to that table |
| `MEASURED_BY` / `MEASURES` | result ↔ comparison | this number is one arm of that comparison |

`dependency(address)` returns the **shortest path from a headline claim**, or None. A path
is an explanation a referee can disagree with — "the abstract states 91.4; that number is
one arm of the accuracy-on-CIFAR-100 comparison; the baseline is the other arm" — which is
what a boolean could never give.

## 2. Two defects the corpus found, both fixed

**A comparison must be a node, not a clique of edges.** The first version joined every
pair of results sharing a metric and a benchmark. On `sanchez24a-icml` that produced
**4,458 edges over 2,229 pairs** — quadratic in one results table, and a structure in
which every number is one hop from every other. A dependency relation that reaches
everything distinguishes nothing. One `COMPARISON` node per (metric, benchmark) is linear
in its members, says what the comparison *is*, and keeps any two arms exactly two hops
apart. The corpus now yields **74 comparisons**, 0 to 27 per paper.

**A sentence boundary does not exist in flattened coordinates.** `claims.flatten` removes
every space, so splitting on "full stop followed by whitespace" matched nothing and each
section became a single span — one or two headline claims per paper. The split now runs on
the original text and the offsets are mapped across with `bisect`. Headline claims per
paper went from 1–2 to 2–7.

An earlier version also minted claim addresses with `claims.mint`, which refuses a
quotation occurring twice. A paper whose abstract sentence reappears almost verbatim in
its introduction is ordinary, and minting refused it: **zero headline claims on seven of
the eight papers**. Claim addresses are now built from offsets the harness already knows,
and every one still resolves through `claims.resolve`.

## 3. The graph, on the eight-paper corpus

| paper | claims | headline | results | comparisons | STATES | CITES | edges |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0c06a98d7c818f6f | 7 | 2 | 59 | 0 | 0 | 5 | 56 |
| 2024-icml-sapg | 7 | 2 | 118 | 5 | 5 | 0 | 135 |
| 5993d35ff0996b52 | 9 | 7 | 2 | 0 | 2 | 0 | 2 |
| acl | 13 | 4 | 167 | 4 | 3 | 6 | 175 |
| apt-icml | 44 | 7 | 416 | 21 | 24 | 17 | 490 |
| cvpr | 10 | 4 | 90 | 14 | 2 | 4 | 126 |
| iclr | 13 | 3 | 175 | 3 | 1 | 9 | 178 |
| sanchez24a-icml | 23 | 5 | 926 | 27 | 6 | 13 | 1463 |

`5993d35ff0996b52` has 2 results because extraction recovered no tables and no figures
from it at all — an extraction fact, recorded here so the row is not read as a property of
the paper.

## 4. The semantic delta, against the rule in force

Compared against `materiality.basis_for_ref` rather than against `discovery._centrality`,
deliberately: `_centrality`'s dominant input is whether a lens attacked the address, so
comparing against it would compare a property of the paper with a property of the panel.

| paper | addresses | graph CENTRAL | graph SUPPORTING | graph PERIPHERAL | rule material | agree |
|---|---:|---:|---:|---:|---:|---:|
| 0c06a98d7c818f6f | 51 | 0 | 51 | 0 | 0 | 51 |
| 2024-icml-sapg | 101 | 0 | 1 | 100 | 0 | 101 |
| 5993d35ff0996b52 | 2 | 0 | 0 | 2 | 0 | 2 |
| acl | 141 | 0 | 128 | 13 | 0 | 141 |
| apt-icml | 369 | 0 | 180 | 189 | 1 | 368 |
| cvpr | 58 | 0 | 18 | 40 | 0 | 58 |
| iclr | 149 | 0 | 87 | 62 | 0 | 149 |
| sanchez24a-icml | 858 | 0 | 655 | 203 | 0 | 858 |
| **total** | **1,729** | **0** | **1,120** | **609** | **1** | **1,728** |

Agreement 0.999 — and both sides of that agreement are empty. The graph establishes a
headline dependency for **0 of 1,729** addresses; the rule in force establishes
materiality for **1**.

## 5. Why, and it is not an implementation defect

The link from a paper's headline claims to its numbers **is not printed in the paper**.
Measured over all eight documents:

```
cross-references appearing in the Abstract .................. 0
cross-references appearing in the Conclusion ................ 1
reported numbers resolving into the Abstract ................ 0
reported numbers resolving into the Conclusion .............. 1
distinct non-trivial numbers printed by headline sentences .. 21
of those, occurring in exactly one recovered table cell ..... 0
```

This is the ordinary convention of scientific writing, not a deficiency of these eight
papers. An abstract states a result in prose — "reduces training memory by 40%" — and does
not write "see Table 3"; the number it prints is rounded, renamed, or expressed as a delta
that appears in no cell. The correspondence between the abstract's claim and the cell that
supports it is **semantic**, and nothing in the document states it.

So it is not only `in_abstract` that fires on nothing. Every deterministic, model-free
representation of headline dependency fires on nothing, for the same reason:

| mechanism | hits |
|---|---|
| `discovery._centrality`'s `in_abstract`, legacy locator | 0 / 706 |
| `discovery._centrality`'s `in_abstract`, corrected locator | 0 / 706 |
| `materiality.basis_for_ref` | 1 / 1,729 |
| this graph's headline dependency | 0 / 1,729 |
| value matching from a headline sentence to a unique cell | 0 / 21 |

The last row is worth stating plainly because it forecloses the obvious next move: adding
a "the abstract prints 40.2 and this cell contains 40.2" edge yields nothing on this
corpus. The numbers an abstract prints are not the numbers a table prints.

## 6. What the graph does establish, which the previous rule did not

The null result is on the CENTRAL axis only. Deterministically, and new:

- **1,120 of 1,729 addresses are reachable from a body claim.** The previous machinery had
  no representation of this at all, and it is a real ordering signal that does not depend
  on which addresses a lens chose to attack.
- **74 experimental comparisons**, each naming its metric, its benchmark and its methods.
  Nothing in the harness previously represented "what is being compared with what", and
  two pieces of pending work need exactly that: reconciliation has to bind an experiment,
  a config, a split, a metric and a protocol, and a focused validation experiment has to
  compare arms.
- **Every dependency is a path, and every path is an explanation.** The current rule
  answers material/not-material; this answers *why*, in hops a referee can open.

## 7. The decision this needed, and what was done

Replacing centrality or materiality with headline dependency **as it stood would have made
the system strictly worse**: 0 material addresses where the rule in force reports 1, over
1,729. Nothing was rewired. The choice was how materiality gets the link the paper does
not print, and the answer taken was: **a reader proposes the pairing and the harness
verifies it** — the arrangement every other model-supplied fact in this system already has.

`harness/claimlink.py` does the verifying. The reader supplies two quotations; the harness
re-mints the claim quotation, requires it to land in the Abstract or the Conclusion,
re-resolves the evidence address against the quotation given for it, and where both sides
carry a number re-derives the relationship to the claim's own printed precision. Nothing
the reader says about its own pairing is read back: `accepted`, `refusal`,
`numeric_relation`, `claim_ref` and `verified_observation` are stripped at the driver
boundary and overwritten at the verifier.

Three refusals that matter:

- **A numeric disagreement is refused, not reported.** A claim and a cell whose numbers
  disagree may be a real inconsistency or a reader citing the wrong cell, and nothing here
  can tell them apart. Raising a contradiction belongs to a lens, under quotation
  verification, an evidence ceiling and independent grading.
- **A body sentence dressed as a headline claim is refused.** The dependency this
  establishes is of the paper's own summary of itself, so it has to start there.
- **With the gate closed nothing changes at all.** No link is established, the graph has
  no `SUPPORTED_BY` edges, `materiality` falls back to its own rule, and the decision is
  bit-identical to what it was before the channel existed.

## 8. The channel, run on all eight papers

One reading per paper, `claude` CLI, `sonnet`, read-only over the papers directory.

| paper | proposed | accepted | refused |
|---|---:|---:|---|
| 0c06a98d7c818f6f | 7 | 5 | claim_unresolved 2 |
| 2024-icml-sapg | 8 | 8 | — |
| 5993d35ff0996b52 | 2 | 0 | evidence_unresolved 2 |
| acl | 8 | 8 | — |
| apt-icml | 6 | 4 | evidence_unresolved 2 |
| cvpr | 14 | 13 | evidence_unresolved 1 |
| iclr | 14 | 13 | evidence_unresolved 1 |
| sanchez24a-icml | 17 | 12 | evidence_unresolved 5 |
| **total** | **76** | **63** | **13** |

Of the 63 accepted, **59 are `NO_NUMBER_IN_CLAIM` and 4 are `NO_NUMBER_AT_EVIDENCE`** —
not one is an arithmetic agreement. That is the same fact §5 measures from the other side:
headline sentences in these papers print 21 numbers between them, and the claims that
actually carry the argument are qualitative. A link with no arithmetic is still a
dependency; it locates the evidence and asserts nothing about the number.

## 9. The delta the brief asked for

| paper | addresses | CENTRAL (no links) | CENTRAL (with links) | rule material |
|---|---:|---:|---:|---:|
| 0c06a98d7c818f6f | 51 | 0 | 0 | 0 |
| 2024-icml-sapg | 101 | 0 | 8 | 0 |
| 5993d35ff0996b52 | 2 | 0 | 0 | 0 |
| acl | 141 | 0 | 0 | 0 |
| apt-icml | 369 | 0 | 8 | 1 |
| cvpr | 58 | 0 | 7 | 0 |
| iclr | 149 | 0 | 7 | 0 |
| sanchez24a-icml | 858 | 0 | 1 | 0 |
| **total** | **1,729** | **0** | **31** | **1** |

**31 addresses now carry a machine-checked dependency on the paper's own summary of
itself, against 1 before.** Every one is a PATH rather than a boolean, and the paths are
openable end to end. One of them, from `apt-icml`:

```
T1:r0:c5, depth 3
  a headline claim in the abstract or conclusion (P22:124-193);
  a reader paired that claim with T1:r1:c5, and the harness verified both halves;
  the paper reports column 5 on table 2 — RoBERTa and T5 pruning with APT compared to
  baselines under 60% sparsity — for two methods, so the two arms are one comparison
```

That is the shape the previous machinery could not produce: the headline claim rests on
one arm of a comparison, so the OTHER arm is material too, and a referee can see why.

### Where it still gets nothing, and why

Three papers gain no CENTRAL address. `5993d35ff0996b52` has no recovered tables or
figures at all, so there is nothing to pair with. On `0c06a98d7c818f6f` and `acl` the
reader paired headline claims with FIGURES, and a figure caption carries no cells — this
harness cannot read a figure's plotted values, so a claim supported by a figure cannot be
made checkable this way. That is a real ceiling and it is the extraction layer's, not the
channel's.

## 10. What is still NOT rewired, deliberately

`materiality.basis_for_ref` and `discovery._centrality` are unchanged, and nothing in this
work moves a decision. Replacing the rule that gates a paper-level material failure with a
channel whose first hop is a model proposal is a larger decision than adding the channel,
and the argument for it now has numbers behind it rather than only an intention. The next
step, when it is taken, is to run the graph's answer BESIDE the existing rule over a full
review and compare what each would have stopped — not to swap one for the other on the
strength of 31 against 1.

## 11. A defect found by running it, worth more than the channel

The first live reading returned 11 pairings for `cvpr` and the harness refused 8 of them
as quotations not present in the paper. They were present. A PDF breaking "generation"
across a line leaves `gener-` and `ation`, which flattens to `gener-ation`; the reader
quoted the sentence the way a human reads it and wrote `generation`.

**The same defect was costing the evidence gate findings.** Measured over the evaluated
corpus, **4 of the 5 findings `verify_evidence` dropped were correctly-quoted sentences
refused for a hyphen the typesetter inserted** — 80% of every drop in the corpus. Dropped
findings across the eight papers went from 5 to 2 once it was fixed, and three real
findings came back.

`claims.soft_hyphen_projection` removes only hyphens the ORIGINAL text shows were followed
by whitespace, so `diverse-weather` survives and `gener-ation` does not. Three rules keep
it honest:

- the exact search runs FIRST, so a character-for-character quotation is never resolved
  through a normalisation;
- `claims.flatten` is unchanged, so every `P<i>:<a>-<b>` address in the repository still
  means exactly what it meant — changing the flattening would have caused the
  `extraction_version` problem rather than avoided it;
- the machine-written observation drops the word "verbatim" and says the match was
  recovered, because claiming verbatim there would be the false attestation
  `stages.audit.source_units` itself exists to prevent.
