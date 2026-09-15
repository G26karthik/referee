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

## 7. The decision this needs

Replacing centrality or materiality with headline dependency **as it currently stands would
make the system strictly worse**: it would report 0 material addresses where the rule in
force reports 1, on a corpus of 1,729. So nothing is rewired, and the choice is what to do
about the missing link.

**Option A — deterministic only.** Keep the graph for what it measures: supporting
structure, the comparison inventory, and explanations. Leave CENTRAL unestablished and say
so in the guarantees, where "novelty is not checked at all" already lives. Honest, costs
nothing, and leaves materiality exactly as conservative as it is today.

**Option B — model-proposed, harness-verified links.** A reader proposes the pairing the
document does not print — "the abstract's claim at `P1:82-224` is supported by `T3:r2:c4`"
— and the harness verifies it the way it verifies everything else: both addresses must
resolve, the claim sentence must really be in the Abstract or Conclusion, and where the
claim prints a number the harness re-derives the relationship to the cell's value. The
model supplies the pairing; it certifies nothing. This is the architecture the rest of the
system already runs on, and invariant 2 is unchanged by it.

Option B is the one that makes materiality mean what the brief asks it to mean. It is also
a new model-proposal channel on the always-on path, which is a decision about what this
system is, not an implementation detail — so it is not being taken unilaterally.
