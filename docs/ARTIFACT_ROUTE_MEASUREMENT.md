# F — the static artifact route, measured on the four papers that published code

`harness/artifact_evidence.py`, `harness/stages/artifact.py`,
`harness/artifact_review_driver.py`, `harness/prompts/artifact_review.py`.
Measured 2026-09-16, deterministically: no model call, no execution, no gate opened.

## 1. What was broken

`ARTIFACT_EVIDENCE` and `RESOLVED_FROM_ARTIFACT` were in their vocabularies and **no
`TARGET_DISPOSITIONS` value mapped to either**. The state machine could not reach them from
any input. Meanwhile `harness/code_audit.py` had been running on every cloned paper since
the first version — parsing the checkout, applying ten rules, writing `CodeAudit.findings`
into the machine report under `## Static code audit` — and nothing consumed them. The
harness read the authors' code and threw the scientific result away.

`planner` made this concrete: `ARTIFACT_INSPECTION` appeared as a `fallback` route that
produced an `INFEASIBLE_*` action and nothing else. The route was declared, ordered in
`VERIFICATION_ROUTES`, counted in `exhaustion`, and unreachable.

## 2. Three levels, and the third has no spelling

    level 1  ARTIFACT_FACT            the checkout contains this, at this span, at this SHA
    level 2  PAPER_ARTIFACT_MISMATCH  the paper states X for experiment E; the pinned
                                      artifact sets Y for experiment E
    level 3  the reported scientific result is false

`artifacts.ARTIFACT_AUTHORITY` has exactly three members and level 3 is not one of them.
That is the encoding of the rule rather than a note about it: **an AST warning may not
become RED.** A code or configuration inconsistency may create a verified concern, establish
a reproducibility defect, trigger execution, trigger focused validation, and become material
where a central claim provably depends on it — every one of those is a downstream decision
by a downstream module, and none is reachable by writing a stronger string in this layer.

Level 2 needs all three of a precisely addressed paper statement, a precisely located
artifact fact, and an established experiment identity. Each failure has its own name —
`paper_statement_unaddressed`, `artifact_fact_unlocated`, `experiment_identity_unbound` —
and the third is the one that matters: **"some config somewhere says 32" contradicts
nothing**, and `bind_mismatch` refuses rather than guessing which of a dozen config files
the paper meant. A fourth outcome, `no_disagreement`, records the case where all three bind
and the two values AGREE, which is a real result and not "nothing found".

## 3. Every fact is tied to an immutable snapshot, and the snapshot fails closed

`ArtifactSnapshot.audited` requires a commit, a tree hash and a clean working tree, and the
type defaults to `dirty=True` — an unknown tree is not an audited tree. `repo.dirty_files`
RAISES when `git status` could not run, and that raise is caught and recorded as dirty,
because "we could not look" and "we looked and it was clean" must never produce the same
artifact.

The TREE sha is compared, not only the commit: an amended commit with identical content is
the same code, and two commits sharing a message and differing in content are not.

**The mutable-checkout question stays open, and this is what was done in the meantime.**
The pre-run snapshot is recorded before anything executes. If a later execution mutates the
tree, the post-run identity check retracts the EXECUTION's evidence exactly as it already
did, and the pre-run static facts stay tied to the pre-run snapshot and are not rewritten —
they are different evidence about different moments. Separating an immutable audited source
from writable run outputs is the eventual answer and is deliberately not designed yet: what
real repositories require has not been observed.

## 4. Relocation — the `claims.mint` of this module

**A model statement about code is not artifact evidence.** `artifact_evidence.relocate`
takes a file and a quotation and the HARNESS finds it: the file must be in the checkout, the
quoted text must be in the file, and it must occur exactly once. What survives carries the
file's own SHA-256 alongside the pinned commit.

Three rules, mirroring `claims.mint`: a path that escapes the checkout is not a citation
into it (checked after resolution, so `../` and a symlink fail the same test); the EXACT
search runs first; and the whitespace-collapsed fallback maps its offsets back through the
original, so the span is a span of the FILE and not of a normalisation of it.

## 5. The ten AST rules, audited by authority

**Not all ten are exposed, and "it exists" is not a reason to expose one.** Each is sorted
into one of four classes, and the classification is a MEASUREMENT over the four repository
papers rather than a reading of the rule's docstring.

| class | meaning | rules |
|---|---|---|
| **A** deterministic artifact fact | true of the checkout by construction, whatever it means | `leak-unseeded-split` |
| **B** candidate concern | it located something real; what that means needs interpretation | `cripple-per-arm-budget`, `cripple-config-table`, `leak-fit-before-split`, `leak-fit-on-test`, `metric-shadows-standard`, `metric-best-of-n`, `metric-filters-ground-truth` |
| **C** diagnostic only | kept in the machine trace, never shown to a reviewer | — |
| **D** unsafe for reviewer output | measured false positives with no bounded reading | `leak-model-selection-on-test`, `cripple-augmentation-one-arm` |

Only A and B are reviewer-visible, and an unaudited rule defaults to D: the audit licenses
a rule, not its existence.

**The corpus produced six hits and five of them are false**, every one for the same reason —
the rules match SUBSTRINGS of identifiers, and an identifier is not a semantic category.

* `leak-model-selection-on-test` fired four times on `apt-icml/run_pruning.py`. Its first
  regex alternative is ``val(idation)?[\w\[\]'". ]*=\s*[\w\.]*test``. The source lines read
  `rescaled_eval_metrics = test(model, eval_dataloader, ...)` — **"eval" contains "val"**,
  and `test` is the name of the evaluation FUNCTION. The rule reported that the test split
  drives model selection in a file where it does not, four times, on lines that call an
  evaluator on `eval_dataloader`. Nothing on the line is a split, a checkpoint or a
  selection, so there is no narrower reading that rescues the hit.
* `cripple-augmentation-one-arm` fired once, on
  `if new_transform_r > model.layer_transformation.r and ...` — `new_` is the arm token and
  `transform` is the augmentation token, in a branch that resizes a LoRA rank. The rule's
  own statement ("data augmentation is applied on the proposed arm only") is false of the
  branch it points at.
* `leak-unseeded-split` fired once, on
  `torch.utils.data.random_split(total_dataset, [n, m])` in `utils/utils.py:600`, and the
  FACT is correct: that call site passes no `generator`, `seed`, `random_state` or
  `stratify`. Whether the split is reproducible depends on a global seed set elsewhere,
  which is why it is class A — a fact about the call — and not a finding.

**Reviewer-visible AST hits across the four repository papers: 1 of 6.**

## 6. The measurement (§13)

Four papers, four checkouts already on disk, every one clean and pinned.

| paper | repository | commit | tree | dirty | objects | routed to inspection | outcome |
|---|---|---|---|---|---:|---:|---|
| `acl` | mbzuai-nlp/finchain | `146eaa8225` | `05cb5cde3f` | no | 43 | 1 | ARTIFACT_RESOLVED |
| `apt-icml` | ROIM1998/APT | `56eaf8bc86` | `869fe7f5c5` | no | 185 | 1 | ARTIFACT_RESOLVED |
| `cvpr` | wuyang98/weathergen | `1462374ef6` | `87820905fc` | no | 68 | 1 | ARTIFACT_RESOLVED |
| `iclr` | HanxunH/LDReg | `48956d25dc` | `8f0486e8aa` | no | 67 | 1 | ARTIFACT_RESOLVED |
| **total** | | | | | **363** | **4** | **4 settled** |

### Deterministic artifact facts established

| | count |
|---|---:|
| level 1 `ARTIFACT_FACT` | 8 |
| **level 2 `PAPER_ARTIFACT_MISMATCH`** | **0** |
| statements examined | 4 |
| code-auditor concerns proposed | 0 (gate closed) |
| concerns surviving code-location verification | 0 |

The eight, in full, because a route that reports a count and not its content is a route
nobody can check:

```
acl       entrypoint_present     advertises data/templates/investment_analysis/npv.py
apt-icml  entrypoint_present     advertises scripts/adaptpruning/roberta_base_sst2_momentum.sh
apt-icml  dependency_declared    declares `transformers` in requirements.txt, environment.yml
cvpr      entrypoint_present     advertises evaluate.py
iclr      entrypoint_present     advertises main_simclr.py
iclr      dependency_undeclared  no manifest declares `pytorch`  (no manifest was found)
iclr      dependency_undeclared  no manifest declares `torch`    (no manifest was found)
iclr      dependency_undeclared  no manifest declares `transformers` (no manifest was found)
```

The three `iclr` lines are one fact stated three ways: that checkout publishes **no
dependency manifest at all**. That is a genuine reproducibility observation about the
artifact and it is level 1 — a fact about the code, not a statement about the paper, and
certainly not a claim that the paper's numbers are wrong.

### Zero paper↔artifact mismatches, and why

`bind_mismatch` requires an experiment identity, and the deterministic probes produce none:
`entrypoint_present` and `dependency_declared` answer questions about the artifact that no
paper statement is addressed against. **The channel that could propose one is the gated
authors'-code auditor, and it was not run for this measurement** — the gate
(`SH_ALLOW_ARTIFACT_REVIEW`) is off by default and this measurement spends no model calls.
So the honest statement is: the route now reaches `ARTIFACT_EVIDENCE` on all four papers and
has established zero level-2 mismatches, of which zero were proposed.

**No scientific defect is reported because the route produces more output.** Eight true
statements about four checkouts is what was established, and it is reported as eight true
statements about four checkouts.

### How the planner routed everything else

| paper | PAPER_ONLY_RESOLUTION | AUTHOR_CODE_REPRODUCTION | INFEASIBLE_ROUTE | ARTIFACT_INSPECTION_ONLY |
|---|---:|---:|---:|---:|
| `acl` | 4 | 25 | 13 | 1 |
| `apt-icml` | 8 | 147 | 29 | 1 |
| `cvpr` | 8 | 42 | 17 | 1 |
| `iclr` | 6 | 43 | 17 | 1 |

One target per paper, and it is the right one: the IMPLEMENTATION_CLAIM object —
*"the released repository <url> implements the described method"* — which is the only object
`discovery` gives `routes == ["ARTIFACT_INSPECTION"]`. Everything with an executable route
keeps it. **Static inspection suppressed no execution on any of the four**, which is the
property the route ordering exists to guarantee and the specific failure
`PAPER_INTERNAL_CHECK` had when it sat in `planner._RESOLVING` and cancelled 100% of the
shipped corpus's escalations by being cheap rather than by settling anything.

One honest caveat on that ordering: every object in these four stored target sets carries
`question_kind = ""`, so `artifact_evidence.requires_execution` — which refuses REPRODUCTION,
PRINTED_QUANTITY, COMPOSITION and ATTRIBUTION questions outright — gated nothing on this
corpus. It is exercised by `tests/test_artifact_route.py` and will gate a fresh run, where
`discovery` populates the field.

## 7. Route order (§10)

    paper-internal
      -> static artifact inspection
        -> authors' code identity/alignment
          -> authors' code execution
            -> reconstruction / focused validation

`ARTIFACT_INSPECTION_ONLY` is reachable only from the arm where NO executable route
applies, which is what makes the first claim of that ordering safe. Within that arm it is
tried BEFORE the citation re-check, because it can settle an artifact-only question and the
citation re-check settles nothing by construction.

## 8. What discharges, and what does not

`artifact_evidence.discharge` requires all three of: an AUDITED snapshot; at least one
statement the route was asked about; and at least one fact carrying authority. Anything else
is `COMPARISON_BLOCKED` — a route applied and its result had nothing to be held against —
or `ARTIFACT_BLOCKED` where there was no usable checkout.

**"The repository cloned successfully" is not artifact evidence**, and that is enforced
rather than stated: an inspection with facts and no statement discharges nothing.

`ARTIFACT_RESOLVED` is excluded from `TargetOutcome.establishes_failure`, so no artifact
observation can bypass materiality. Saying the code disagrees with the method section is not
saying the reported number is false; which configuration produced the reported number is a
question for execution.

## 9. The authors'-code auditor (§7)

`harness/prompts/artifact_review.py` + `harness/artifact_review_driver.py`, gated by
`SH_ALLOW_ARTIFACT_REVIEW`, default off.

Access: the paper's METHOD text, the file tree, the pinned SHA, and `Read` + `Grep` over the
checkout. No `Bash`, no `Write`, no network — the confinement is the enforcement, not the
prompt, and an auditor that could run the code would be an execution path with none of
`authorize()`'s preconditions in front of it.

It may propose eight closed kinds (paper/code mismatch, missing experiment path, metric
mismatch, split leakage, baseline implementation, dead claimed mechanism, hard-coded result,
other). It is not shown any finding, severity, grade or verdict, and the `build` signature
is what makes that so. `accepted`, `refusal`, `span`, `snapshot`, `paper_ref`, `fact_id` and
`probe` are stripped at the driver boundary: a reader cannot award itself the only authority
that could ever matter.

Every citation is relocated; what does not relocate is dropped whole and counted. A
relocated citation is an ARTIFACT_FACT whose statement carries the reader's reading marked
UNVERIFIED — the harness established the location, not the reading.

## 10. Tests

`tests/test_artifact_route.py`, 20 tests, every one against a REAL git checkout: a fact
about a pinned tree that was never pinned would be the exact defect this route exists to
avoid. Plus four module self-checks (`artifact_evidence`, `artifact_review_driver`,
`prompts.artifact_review`, `stages.artifact`).

The rule audit is enforced at the RENDERER and not only recorded: `stages/report.
_code_audit_block` prints only class-A and class-B hits and says how many it suppressed,
so a referee is never asked to investigate the four `eval`-contains-`val` false positives.
The machine trace keeps all of them — suppression is a rendering decision.

## 11. What this route still cannot do

* **It has established zero level-2 mismatches on real papers.** The deterministic probes
  cannot bind an experiment identity, and the channel that could propose one is a gated
  model channel that has not been run against the corpus.
* **`_paths_named` searches a fixed list of conventional filenames** rather than parsing
  paths out of prose, because an extracted "path" that is really a hyphenated word would
  make this route report a missing file nobody claimed.
* **`config_values` and `argparse_default` have no production caller.** Both are real,
  tested probes — "what does this file set for this key" and "what default does this flag
  carry" — and they are the natural input to a level-2 mismatch. Nothing on the route calls
  them yet, because binding one to a paper statement needs the experiment identity that the
  route cannot produce on its own. They are listed here for the same reason
  `alignment/trial.py` is listed in CLAUDE.md's limitations: so nobody reports them as a
  capability this system exercises.
* **Class-B rules are reviewer-visible and unmeasured.** Seven of the ten rules did not fire
  on any of the four repositories, so their classification rests on their structure rather
  than on a measurement. A rule that fires for the first time on a fifth paper should be
  re-audited before its output is believed.
