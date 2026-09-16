# F — the static artifact route, measured on the four papers that published code

`harness/artifact_evidence.py`, `harness/stages/artifact.py`,
`harness/artifact_review_driver.py`, `harness/prompts/artifact_review.py`.
Measured 2026-09-16. The deterministic half spends no model call; §7 onward is one gated,
isolated authors'-code reading per repository.

**`artifact_review_driver` has no production caller.** `harness/stages/artifact.py` imports
only `artifact_evidence`, `claims` and `state` — never `artifact_review_driver` — and
`harness/controller.py` has zero references to it. `harness/config.py` declares the
`SH_ALLOW_ARTIFACT_REVIEW` gate and its settings fields but never calls `.run()`; the only
production-adjacent mention is a comment in `harness/artifact_evidence.py`; the only actual
caller is `tests/test_artifact_route.py`, as a unit test. So §7's numbers below were
obtained by invoking the driver DIRECTLY, outside the pipeline, and are not reproducible
today by running `run.py review` — a route this document otherwise treats as measured end
to end is, for its model half, measured only in isolation. §6's numbers are unaffected: the
deterministic half runs from `harness/stages/artifact.py` on every review already.

**The headline, at the authority it actually has:**

```
4 artifact inspections completed
6 narrow artifact facts established (acl=1, apt-icml=2, cvpr=1, iclr=2)
0 broad implementation-correctness questions settled
8 authors'-code concerns proposed, 7 code citations and 7 paper citations relocated
  (direct invocation of artifact_review_driver, not via run.py review — see above)
1 experiment identity ESTABLISHED, 6 AMBIGUOUS
7 endpoint-verified artifact concerns
0 PAPER_ARTIFACT_MISMATCH
```

## 1. What was broken, twice

**First: the route could not produce evidence at all.** `ARTIFACT_EVIDENCE` and
`RESOLVED_FROM_ARTIFACT` were in their vocabularies and no `TARGET_DISPOSITIONS` value
mapped to either. Meanwhile `harness/code_audit.py` had been parsing every cloned paper
since the first version and writing its hits into the machine report, where nothing
consumed them. The harness read the authors' code and threw the scientific result away.

**Second, and this one shipped in the fix: the route settled questions it had not
answered.** `discharge` asked three things — an audited snapshot, at least one statement,
at least one fact carrying authority — and returned a boolean. Any fact could therefore
settle any statement. On all four repository papers the routed target is

> *"the released repository &lt;url&gt; implements the described method"*

and it was discharged by facts like *"the checkout advertises evaluate.py"*. **Four papers
were reported as having had a claim about their implementation settled by the presence of
a file.** An entrypoint existing is supporting evidence for that question; it is not its
answer, and reporting it as one violates the authority hierarchy this route exists to
state.

## 2. Four rungs, and the top one has no spelling

| level | what it is about | what it needs |
|---|---|---|
| `ARTIFACT_FACT` | the CHECKOUT | an audited snapshot, a relocated span, and a question whose SCOPE it answers |
| `ENDPOINTS_VERIFIED_ARTIFACT_CONCERN` | two real locations | + a paper statement that MINTS; the CORRESPONDENCE is the auditor's reading |
| `PAPER_ARTIFACT_MISMATCH` | the paper AND the checkout | + an identity ESTABLISHED from a deterministic source, + two comparable quantities, + a paper value the harness can re-read from the span |
| *the reported result is false* | — | **there is no value for this** |

`artifacts.ARTIFACT_AUTHORITY` has four members and the last is not one of them. That is
the encoding of the rule rather than a note about it: **an AST warning may not become RED.**
A code or configuration inconsistency may create a verified concern, establish a
reproducibility defect, trigger execution, trigger focused validation, and become material
where a central claim provably depends on it — every one of those is a downstream decision
by a downstream module.

**A relocated code quotation proves that this code exists at this location in this audited
snapshot. It does not prove that the code contradicts the paper.** That is the same
correction the claim-link channel needed, on a second channel.

## 3. Scope — a bounded fact settles only a matching bounded question

Every probe declares which bounded question it answers:

    FILE_PRESENCE  ENTRYPOINT_PRESENCE  DEPENDENCY_DECLARED
    MANIFEST_PRESENCE  CONFIG_LITERAL  COMMAND_PRESENCE

and `IMPLEMENTATION_CORRESPONDENCE` — "does the released code implement the described
method", "is the implementation faithful", "does this reproduce the paper" — is excluded
from `SETTLEABLE_BY_ARTIFACT_FACT` by construction, so no accumulation of level-1 facts can
reach it. `question_scope` defaults to it for anything it cannot recognise, so the rule
fails closed.

A broad question is now **decomposed rather than discharged**: the route answers the narrow
questions it can, records each against its own scope, names them in the outcome, and leaves
the broad claim open. A referee reads *"these four things about the artifact are
established, and whether the code implements the method is still open"*, which is what was
true all along.

## 4. Five terminal states, and only one is about the paper

| disposition | evidence state | resolution | about the paper |
|---|---|---|---|
| `ARTIFACT_MISMATCH_ESTABLISHED` | `ARTIFACT_EVIDENCE` | RESOLVED_FROM_ARTIFACT | **yes** |
| `ARTIFACT_FACT_ESTABLISHED` | `ARTIFACT_PROPERTY_ESTABLISHED` | RESOLVED_FROM_ARTIFACT | no — about the CODE |
| `ARTIFACT_CONCERN_VERIFIED_ENDPOINTS` | `ARTIFACT_ENDPOINTS_VERIFIED` | UNRESOLVED | no |
| `ARTIFACT_INSPECTION_INCONCLUSIVE` | `COMPARISON_LIMITATION` | UNRESOLVED | no |
| `ARTIFACT_BLOCKED` | `ARTIFACT_LIMITATION` | UNRESOLVED | no |

None is in `TargetOutcome.establishes_failure`: no artifact observation can bypass
materiality. A question whose answer is a MEASUREMENT (`requires_execution`) is refused
outright rather than allowed to appear settled by a reading of the source.

## 5. Experiment identity, classified rather than believed

`classify_identity` returns ESTABLISHED only when the auditor names a DETERMINISTIC source
for the experiment-to-file link **and that source relocates in the pinned tree**:

| basis | evidence |
|---|---|
| `paper_names_the_command` | the paper prints the command or path itself |
| `readme_maps_the_experiment` | the checkout's README maps the experiment to the file |
| `script_passes_the_config` | a committed script names both |
| `authors_experiment_table` | the repository documents its experiments |
| `auditor_assertion` | the auditor's reading — **AMBIGUOUS, never ESTABLISHED** |

PARTIAL is a deterministic basis whose evidence did not relocate; UNBOUND is nothing
offered at all. **Only ESTABLISHED may support level 2.** A model saying "this looks like
the right config" is a guess with a citation on it.

## 6. The deterministic route, on the four repositories (§13)

Every checkout clean and pinned; no model call.

| paper | repository | commit | tree | objects | routed | facts | outcome |
|---|---|---|---|---:|---:|---:|---|
| `acl` | mbzuai-nlp/finchain | `146eaa8225` | `05cb5cde3f` | 43 | 1 | 1 | ARTIFACT_INSPECTION_INCONCLUSIVE |
| `apt-icml` | ROIM1998/APT | `56eaf8bc86` | `869fe7f5c5` | 185 | 1 | 2 | ARTIFACT_INSPECTION_INCONCLUSIVE |
| `cvpr` | wuyang98/weathergen | `1462374ef6` | `87820905fc` | 68 | 1 | 1 | ARTIFACT_INSPECTION_INCONCLUSIVE |
| `iclr` | HanxunH/LDReg | `48956d25dc` | `8f0486e8aa` | 67 | 1 | 2 | ARTIFACT_INSPECTION_INCONCLUSIVE |
| **total** | | | | **363** | **4** | **6** | **0 broad questions settled** |

**This is the corrected result, re-measured on the 2026-09-16 fresh run after
`distinct_facts` (`harness/stages/artifact.py`, committed at `aebd468`) collapsed a
per-target duplication defect** — a paper's dependency check emitted one fact per
UNDECLARED dependency name checked, so `iclr`'s three separately-named-but-identical
"no manifest at all" observations counted as three facts instead of one, inflating the
corpus total from 6 to 8. Six facts, in full:

```
acl       entrypoint_present     advertises data/templates/investment_analysis/npv.py
apt-icml  entrypoint_present     advertises scripts/adaptpruning/roberta_base_sst2_momentum.sh
apt-icml  dependency_declared    declares `transformers` in requirements.txt, environment.yml
cvpr      entrypoint_present     advertises evaluate.py
iclr      entrypoint_present     advertises main_simclr.py
iclr      dependency_undeclared  no manifest declares `pytorch`  (no manifest was found)
```

`iclr`'s single remaining dependency fact stands for what were three duplicated lines
before the fix: that checkout publishes **no dependency manifest at all**. A genuine
reproducibility observation about the artifact, at level 1 — about the code, not about the
paper.

Every inspection also records what it buys a later route: on each paper, the advertised
entrypoint narrows the candidate commands for any execution route. Recorded and acted on by
nothing here; narrowing a command is `experiment_id`'s to use and execution is `probe`'s to
authorise. **Static inspection suppressed no execution on any of the four**:
`ARTIFACT_INSPECTION_ONLY` is reachable only where no executable route applies.

## 7. The authors'-code auditor, run on all four (§5)

**This section's numbers come from a direct, non-pipeline invocation of
`artifact_review_driver`, not from `run.py review`.** As noted at the top of this
document, nothing in `harness/stages/artifact.py` or `harness/controller.py` calls this
driver, so `SH_ALLOW_ARTIFACT_REVIEW` gates nothing on the path a review actually takes
today. What follows is real evidence that the mechanism works when invoked, not a result a
reviewer running this harness end to end currently obtains.

One fresh isolated context per repository, `claude` CLI, `sonnet`, gated by
`SH_ALLOW_ARTIFACT_REVIEW`. Enforced policy identical on all four: `tools=Read,Grep`,
11 tools denied, one added directory (the checkout), restricted, strict MCP, pinned
settings file. No Bash, no Write, no network. No finding, severity, grade, verdict, target
outcome or paper decision reaches it — the `build` signature has no parameter that could
carry one. Prompt, response hash, session id, turn count, wall time and the full tool
policy are persisted per paper under `projects/<pid>/artifact/`.

| paper | turns | seconds | proposed | code relocated | paper relocated | identity | concerns | mismatches |
|---|---:|---:|---:|---:|---:|---|---:|---:|
| `acl` | 8 | 74 | 1 | 0 | 0 | — | 0 | 0 |
| `apt-icml` | 42 | 218 | 2 | 2 | 2 | 1 ESTABLISHED, 1 AMBIGUOUS | 2 | 0 |
| `cvpr` | 14 | 113 | 2 | 2 | 2 | 2 AMBIGUOUS | 2 | 0 |
| `iclr` | 10 | 117 | 3 | 3 | 3 | 3 AMBIGUOUS | 3 | 0 |
| **total** | | | **8** | **7** | **7** | **1 / 6** | **7** | **0** |

`acl`'s single proposal was dropped whole: its code citation did not relocate in the pinned
tree. That is the gate working — not softened, not reported.

### What the concerns actually say

Substantive, openable, and every one a REFEREE'S QUESTION rather than a demonstrated
inconsistency:

* **`cvpr`, `evaluate_weather.py:342`** — the paper says real samples are drawn from the
  matching weather's test split; the auditor reads the code as always drawing from `snow`
  regardless of the weather being evaluated. AMBIGUOUS identity.
* **`cvpr`, `evaluate_weather.py:340`** — the paper says 200 real-world samples; the auditor
  reads `12 * batch_size` (96 at the file's default).
* **`iclr`, `losses/ntxent_lid_reg.py:174`** — the paper presents LL1 and LL2 as two
  distinct regularisation formulations; the auditor reads `reg_type='l1'` and `'l2'` as
  algebraically identical at that line.
* **`iclr`, two config paths** — configurations the paper's Table 1 and its
  transfer-learning section imply are not present in the checkout.
* **`apt-icml`, `scripts/adaptpruning/roberta_base_cola_momentum.sh:59`** — the paper's
  Table 6 GLUE-small column against `num_train_epochs=120, distill_epoch=96`.

**None of these is reported as a paper defect, and that is correct.** Their identity is the
auditor's reading of which config belongs to which experiment, and two prose descriptions
that differ is a semantic judgement, not a comparison of stated quantities.

### The one that came closest, and why it was refused

`apt-icml`, `scripts/adaptpruning/t5_base_lm_adapt_cnndm_momentum.sh:49`. Everything bound:

* the paper span `P25:834-964` is Table 6's hyperparameter block, minted;
* the checkout's own `README.md:62` says *"For finetuning T5-base models with APT, please
  run: `bash scripts/adaptpruning/t5_base_lm_adapt_cnndm_momentum.sh`"* — identity
  **ESTABLISHED**, relocated;
* the script line reads `num_train_epochs=12`, **re-read from the file** rather than taken
  from the auditor;
* the auditor reported the paper value as `"Epochs 16 (CNN/DM column)"`.

It was accepted as a `PAPER_ARTIFACT_MISMATCH`, and hand-checking it found the last defect
this document reports. The span really does say 16 — and it also says 40, 32, 15 and 6:

```
Learning rate 2e-4 2e-4 2e-4 1e-4 1e-4 Batch size 32 32 32 16 32
Epochs 40 40 40 16 15 Distill epochs 20 20 20 6 -
```

**Which column is CNN/DM's is a reading of a table layout that extraction flattened away.**
`_derivable_from` now requires the paper value to be re-derivable from the quoted span by
the harness itself — the span reports exactly one quantity and it is this one, or the number
occurs in the span exactly once. Quote the cell and it binds; quote the table and it does
not. The concern is kept at `ENDPOINTS_VERIFIED_ARTIFACT_CONCERN` with the refusal
`paper_value_not_derivable` and the sentence *"Quote the cell, not the table."*

That refusal took the corpus from 1 established mismatch to 0. It is the right number.

### Two smaller defects the live run exposed

* **A shadowed vocabulary.** `IDENTITY_STATES` was defined twice in `artifacts.py` — once
  for `ProbeSpec`'s experiment-identity resolution, once (later, mine) for the artifact
  route's four states. The second won at import, so the auditor run's identity histogram
  came back keyed on five values the route never writes, and every bucket read zero. Renamed
  to `ARTIFACT_IDENTITY_STATES`; `tests/test_artifact_route.py` now fails the suite on any
  duplicated module-level vocabulary. **A miscount that looks like a measurement is worse
  than a crash.**
* **A silent failure.** Two of the four auditor calls first returned nothing and left no
  record at all, so "the auditor found nothing" and "the auditor never answered" were
  indistinguishable. Every early return in `run()` now writes a failure sidecar naming the
  reason, the command, the return code and the captured stderr.

### What the inspection buys a later route (§12)

Derived from the PROBE, never from the reading — what an observation ENABLES is a property
of what was looked at, and an auditor's prose may not decide what runs next any more than
it may decide what is established. Recorded into `ArtifactInspection.escalations`, a list
of sentences no planner, gate or disposition reads:

```
apt-icml  scripts/adaptpruning/roberta_base_cola_momentum.sh:59
          scripts/adaptpruning/t5_base_lm_adapt_cnndm_momentum.sh:49
            identify a configuration whose two candidate values a RUN could discriminate
            between — which is an execution question, and is why this route refuses it
cvpr      evaluate_weather.py:340
            identifies the metric implementation an execution route would measure against
iclr      configs/simclr/in1k/vit_base_bs2048_e200/pretrain.yaml:16
          detectron2/configs/coco_R_50_C4_2x_simclr_e100.yaml:11
            narrow what an execution route could attempt: the configuration the paper
            implies was not found in the checkout
```

**Finding something interesting statically is not a reason to stop measuring**, and there
is no field here through which it could become one.

## 8. The ten AST rules, re-audited (§14)

**Only class A may appear in the referee report. Class B feeds the auditor.** That a source
pattern matched is deterministic; the scientific interpretation of the match is not, and a
class-B rule is defined by needing one.

| rule | class | fired on these four | measured precision | in the report? | feeds the auditor? |
|---|---|---|---|---|---|
| `leak-unseeded-split` | A | **yes**, 1× (`apt-icml utils/utils.py:600`) | 1/1 as a FACT about the call site | **yes** | no |
| `cripple-per-arm-budget` | B | no | unmeasured | no | yes |
| `cripple-config-table` | B | no | unmeasured | no | yes |
| `leak-fit-before-split` | B | no | unmeasured | no | yes |
| `leak-fit-on-test` | B | no | unmeasured | no | yes |
| `metric-best-of-n` | B | no | unmeasured | no | yes |
| `metric-filters-ground-truth` | B | no | unmeasured | no | yes |
| `metric-shadows-standard` | B | no | unmeasured | no | yes |
| `leak-model-selection-on-test` | **D** | **yes**, 4× (`apt-icml run_pruning.py`) | **0/4** | no | no |
| `cripple-augmentation-one-arm` | **D** | **yes**, 1× (`apt-icml param_control.py:819`) | **0/1** | no | no |

Seven of the ten have never fired on a real repository, so their classification rests on
their structure rather than on a measurement — which is precisely why a first-ever class-B
hit must not reach a referee wearing a measured detector's clothes.

The two class-D rules and why no narrower reading rescues them:

* `leak-model-selection-on-test`'s first regex alternative is
  ``val(idation)?[\w\[\]'". ]*=\s*[\w\.]*test``. The source lines read
  `rescaled_eval_metrics = test(model, eval_dataloader, ...)` — **"eval" contains "val"**,
  and `test` is the name of the evaluation FUNCTION. Nothing on those lines is a split, a
  checkpoint or a selection.
* `cripple-augmentation-one-arm` fired on
  `if new_transform_r > model.layer_transformation.r and ...` — `new_` is the arm token and
  `transform` is the augmentation token, in a branch that resizes a LoRA rank.

**Reviewer-visible AST hits across the four repositories: 1 of 6.** The renderer enforces
this (`stages/report._code_audit_block`), prints how many it suppressed, and keeps all of
them in the machine trace.

## 9. The authors'-code auditor, as built (§7)

Access: the paper's METHOD and EXPERIMENT text, the file tree, the pinned SHA, `Read` and
`Grep` over the checkout. It is deliberately NOT shown the abstract's headline claims: a
reader given the result reads the code looking for the reason it might be wrong.

Eight closed concern kinds. `authority`, `refusal`, `span`, `snapshot`, `paper_ref`,
`fact_id`, `probe`, `settles`, `identity_state` and `identity_span` are stripped at the
driver boundary — **a reader cannot award itself the only judgement that separates a
mismatch from a concern.**

Where the auditor names a config key, the harness **re-reads the value from the file**
(`config_values` / `argparse_default`, which until now had no production caller). A key set
in more than one place REFUSES rather than picking; the auditor's value is kept and the
ambiguity is recorded. On this corpus that path fired once.

## 10. Tests

`tests/test_artifact_route.py` — 32 tests, every one against a real git checkout — plus four
module self-checks (`artifact_evidence`, `artifact_review_driver`, `prompts.artifact_review`,
`stages.artifact`).

## 11. What this route still cannot do

* **Zero level-2 mismatches on real papers, and the reason is not extraction.** Six of the
  seven concerns have an AMBIGUOUS identity, and the seventh had an ESTABLISHED identity and
  a paper value the harness could not re-read from the span. Both refusals are correct.
* **`decompose` answers four bounded questions and the broad one stays open**, which is the
  honest state of "does this code implement the method": nothing in a static reading
  answers it.
* **`question_kind` is empty on every object in these four stored target sets**, so
  `requires_execution` gated nothing on this corpus. It is exercised by tests and will gate
  a fresh run, where `discovery` populates the field.
* **Class-B rules are unmeasured**, and a rule firing for the first time on a fifth paper
  should be re-audited before its output is believed.
