# What must be known before an environment may be chosen

An epistemic audit of one question: **for an arbitrary paper, does this harness know
enough about the exact experiment to decide whether a backend can legitimately reproduce
it?**

Every row below was traced to a function and checked against the three pilot artifacts.
Where a requirement is unrepresented that is stated as unrepresented, not as satisfied.

---

## 1. The chain, and where information is lost

```
paper text ─┐
            ├─► resources.require_resources ─► ResourceRequirement ─┐
repo files ─┘   (5 regex extractors)           (7 fields + evidence) │
                                                                     │
backend ────► BackendProfile / BackendResources ─────────────────────┤
                                                                     ▼
                                          backends.select_for   (matching)
                                                                     │
                                          resources.assess_resources │
                                                                     ▼
                                          backends.authorize    (the only yes)
                                                                     ▼
                                          backend.provision → execute
```

**Four losses, in order of severity.**

| # | loss | consequence |
|---|---|---|
| L1 | the requirement model has 7 quantity fields; ~27 requirements can affect legitimate reproduction | 20 requirements have nowhere to be recorded, so they are never compared with anything |
| L2 | `ResourceRequirement.stated` was an OR over six fields and gated both selection and assessment | **fixed** — see §4. One stated field used to vouch for five unstated ones |
| L3 | `BackendProfile.network_at_runtime` and `.reproducibility` are declared and consulted by nothing | no requirement encodes the matching half, so the fields cannot be used. Fails safe, wastefully |
| L4 | `require_resources` reads `doc` only, so `ResourceRequirement` still holds nothing read from the checkout | **partly addressed** — the checkout is now read into `CodeAudit.runtime` (§8), but as report-only observations. No repository-declared figure feeds the quantities `select_for` and `authorize` compare |

L1 is the honest headline: **the requirement model is a memory-and-count model, not an
environment model.** It is correct about what it covers.

---

## 2. Matrix — evidence and representation

`extraction` names the actual function. "—" means no code reads this.

| requirement | source of evidence | extraction mechanism | structured as |
|---|---|---|---|
| OS / platform | repo `environment.yml`, `requirements.txt` | `repo.declared_platform` (`_LINUX_ENV_MARKERS`) | `ExecCapability.declared_platform` |
| CPU architecture | — | — | — |
| CPU count | paper prose | *none* — field exists, no extractor | `ResourceRequirement.cpu_count` |
| RAM | paper prose | *none* — field exists, no extractor | `ResourceRequirement.ram_bytes` |
| GPU availability | paper prose | `resources.declared_hardware` | `gpu_model` (evidence only) |
| GPU model | paper prose | `resources.declared_hardware` (`_GPU_NAMES`) | `gpu_model` |
| GPU count | paper prose | `resources.declared_hardware` (`_GPU_COUNT`) | `gpu_count` |
| VRAM | paper prose; cell caption | `declared_memory_cost` (`_MEM_COST`), `model_weight_floor` | `vram_bytes` |
| disk | — | *none* — field exists, no extractor | `disk_bytes` |
| Python version | conda `python=`, `python_requires`, `requires-python` | `code_audit._declared_demands` | `RuntimeDemand(kind="python_version")` |
| CUDA / runtime | a CUDA-tagged wheel or `nvidia-*-cuNN` pin | `code_audit._declared_demands` | `RuntimeDemand(kind="cuda_runtime")` |
| package / dependency | repo dependency files | `repo.inspect_dependencies`, `entrypoint_imports`, `missing_imports` | `ExecCapability.missing_dependencies` |
| environment manager | repo dependency filenames | `repo.inspect_dependencies` (`.txt` installable, `.yml` not) | `RepoAcquisition.dependency_files` |
| model availability | `from_pretrained` with a **string literal** | `code_audit.runtime_from_source` | `RuntimeDemand(kind="model_artifact")` — identity only |
| dataset availability | `load_dataset` with a **string literal** | `code_audit.runtime_from_source` | `RuntimeDemand(kind="dataset_artifact")` — identity only |
| checkpoint availability | — (a reference is not an availability) | — | — |
| network access | — | — | `BackendProfile.network_at_runtime` (supply side only) |
| external downloads | a `wget`/`curl` **command**, or `hf_hub_download`/`snapshot_download`/`urlopen`/`urlretrieve` | `code_audit._shell_demands`, `runtime_from_source` | `RuntimeDemand(kind="external_download")` |
| credentials | backend declaration | `BackendProfile.requires_credentials` | supply side only |
| filesystem | host | `resources.host_disk_bytes` | `disk_bytes` |
| wall time | paper prose | `resources.declared_walltime` (`_WALLTIME`) | `walltime_s` |
| distributed execution | `#SBATCH` in the entrypoint's own submission script | `code_audit._shell_demands` | `RuntimeDemand(kind="scheduler_allocation")` |
| multi-GPU | paper prose | `declared_hardware` | `gpu_count` |
| determinism / reproducibility | paper + repo seed handling | `experiment_id.resolve_configuration` | `ConfigurationIdentity.seed_policy_*` |
| seed policy | paper + repo | same, plus `repo.accepts_argument("seed")` | `seed_policy_match`, `accepts_seed_argument` |
| precision | cell caption (fp16 assumed for the floor only) | `model_weight_floor` | not a requirement |
| accelerator-specific | — | — | — |
| compiler / system libraries | repo `environment.yml` linux markers | `repo.declared_platform` | folded into platform |

## 3. Matrix — enforcement

`select` = read by `backends.select_for`. `auth` = read by `backends.authorize` (via
`ResourceCapability.established`, `ExecCapability.established` or
`identities_established`).

| requirement | select | auth | UNKNOWN ⇒ INCONCLUSIVE | mismatch ⇒ INCONCLUSIVE | can be silently ignored |
|---|---|---|---|---|---|
| OS / platform | ✅ | ✅ | ✅ (empty declaration = no constraint) | ✅ | no |
| CPU architecture | ❌ | ❌ | ❌ | ❌ | **yes** |
| CPU count | ✅ | ✅ | via `memory_stated` | ✅ | no |
| RAM | ✅ | ✅ | ✅ | ✅ | no |
| GPU availability | ✅ | ✅ | via VRAM | ✅ | no |
| GPU model | ❌ | ❌ | n/a — recorded as evidence, deliberately not a constraint | n/a | by design |
| GPU count | ✅ | ✅ | via `memory_stated` | ✅ | no |
| VRAM | ✅ | ✅ | ✅ | ✅ | no |
| disk | ✅ | ✅ | via `memory_stated` | ✅ | no |
| Python version | ❌ | ❌ | report-only | report-only | no — recorded with file:line |
| CUDA / runtime | ❌ | ❌ | report-only | report-only | no — recorded with file:line |
| package / dependency | ❌ | ✅ | ✅ | ✅ | no |
| environment manager | ❌ | ✅ | ✅ (a `.yml`-only stack cannot be provisioned) | ✅ | no |
| model availability | ❌ | ❌ | report-only | report-only | identity recorded; availability stays unknown |
| dataset availability | ❌ | ❌ | report-only | report-only | identity recorded; availability stays unknown |
| checkpoint availability | ❌ | ❌ | ❌ | ❌ | **yes** — not establishable from source |
| network access | ❌ | ❌ | ❌ | ❌ | **yes** (fails safe at runtime) |
| external downloads | ❌ | ❌ | report-only | report-only | no — recorded with file:line |
| credentials | ✅ | ✅ | n/a | ✅ | no |
| filesystem | ✅ | ✅ | via `memory_stated` | ✅ | no |
| wall time | ✅ | ✅ | via `memory_stated` | ✅ | no |
| distributed execution | ❌ | ❌ | report-only | report-only | only when bound to an entrypoint |
| multi-GPU | ✅ | ✅ | via `memory_stated` | ✅ | no |
| determinism | ❌ | ✅ | ✅ | ✅ | no |
| seed policy | ❌ | ✅ | ✅ | ✅ | no |
| precision | ❌ | ❌ | ❌ | ❌ | **yes** |
| accelerator-specific | ❌ | ❌ | ❌ | ❌ | **yes** |
| compiler / system libs | ✅ (as platform) | ✅ | ✅ | ✅ | partly |

Seven of the previously-invisible ten are now **recorded with a file and a line** —
report-only, gating nothing (§8). Three remain unrepresented: CPU architecture, precision,
and accelerator-specific requirements. None can produce a false FAILED_REPRODUCTION: each,
if violated, crashes before or during startup, `reached_experiment` is False, and the
result is INCONCLUSIVE. `precision` stays the worst of them in principle — a repository
defaulting to a different dtype than the paper used produces a number, and nothing here
would notice.

---

## 4. The four states, and what the harness does with each

`UNKNOWN` is never permission. That was true of the all-unknown case and is now true of
the partial case.

| state | meaning | what happens |
|---|---|---|
| ESTABLISHED | a source states it, with a re-verifiable quote (`ResourceEvidence`, `IdentityEvidence`) | compared against the backend; a shortfall blocks |
| AMBIGUOUS | several readings fit equally (`IDENTITY_STATES.ambiguous`) | blocks. Picking one would be a guess presented as a fact |
| UNKNOWN | no source establishes it | blocks where represented; **invisible where unrepresented** — the L1 gap |
| NOT_APPLICABLE | the experiment does not use it (no GPU, single process) | absent from `unstated`; nothing to compare |

The system cannot distinguish UNKNOWN from NOT_APPLICABLE for an unrepresented
requirement, because there is no field to hold either. For a represented one it can:
`ResourceRequirement.unstated` names what no source established.

### The leak that was here, and is closed

`stated` is an OR over six fields, and it was the sole gate on both `select_for` and
`assess_resources`. Every unstated field then matched vacuously, because `_fits` reads
`need is None` as "fits". Measured before the fix:

```
requirement                     stated  select_for            assess_resources
only cpu_count=2 stated         True    selected              satisfied
only walltime_s=60 stated       True    selected              satisfied
only disk_bytes=1GiB stated     True    selected              satisfied
```

and with identity, capability and commit satisfied, `authorize` returned
`allowed=True / authorized`, on the reason string *"every declared requirement fits the
backend: CPU 2 <= 16"* — for an experiment whose memory demand had never been compared
with anything.

SAPG is a live instance rather than a hypothetical: its requirement is
`stated=True` on a single scraped walltime of 60 hours, with `vram`, `ram`, `disk` and
`cpu` all unestablished. It refuses today only because it advertises no repository.

**Fix.** `ResourceRequirement.memory_stated` — is `vram_bytes` or `ram_bytes`
established? Memory is singled out because it is the quantity the check exists for: a
process launches happily on an 8 GiB card and dies loading a 7B checkpoint, with a stderr
indistinguishable from broken code. Disk and CPU shortfalls announce themselves. Either
figure counts, because a CPU-only experiment's binding constraint is host RAM.

`assess_resources` returns `unknown`, and `select_for` returns `requirement_unknown`,
when memory was never established. Both checks sit **after** the shortfall comparison, so
an experiment that provably exceeds the backend still gets that specific refusal rather
than being flattened into ignorance. After the fix:

```
only cpu_count=2 stated         True    requirement_unknown   unknown
authorize                       →  allowed=False / resources_unproven
```

---

## 5. Network and reproducibility: why no field was added

`BackendProfile.network_at_runtime` and `.reproducibility` exist and nothing reads them.
The question is whether a matching requirement should be built.

**Where the evidence would come from.** Not the paper — papers do not say "this script
downloads a checkpoint". The repository does, and the evidence is concrete: a
`from_pretrained("meta-llama/…")`, a `datasets.load_dataset`, an `hf_hub_download`, a
`torch.hub.load`, a `wget` in a launch script. Every one carries a file and a line, which
is the same shape of evidence every other requirement here carries.

**Whether it exists today: no.** `code_audit` has ten rules and none of them is about
downloads. There is no extractor, so a `network_required` field would be a field with
nothing behind it — and an empty field reads as "no network needed", turning an unknown
demand into permission. That is precisely the failure `memory_stated` was added to close,
so adding the field first and the extractor later would reintroduce it in a new place.

**Where it belongs, when it exists.** Not in `ResourceRequirement`, which is quantities
in bytes and counts. A runtime download is a *capability* question — can this environment
give the code a fair run — which is `ExecCapability`, along
`missing_dependencies`. `declared_platform` is the precedent: extracted from the
checkout, judged against what the backend offers.

**Whether UNKNOWN must block: no, and this is the one place it should not.** Almost no
repository declares itself network-free. Blocking on unknown network need would block
every paper, which is a refusal that carries no information. The asymmetry that justifies
blocking on memory does not hold here: an unmet network need raises during startup,
`reached_experiment` is False, and the outcome is INCONCLUSIVE. It fails safe. It fails
*wastefully* — after provisioning — and that is a cost, not a correctness hole.

`reproducibility` is different again: the seed half of it is already represented, in
`ConfigurationIdentity.seed_policy_paper` / `seed_policy_repo` / `seed_policy_match`, and
is enforced through `identities_established`. The profile field is a session-volatility
tag with no requirement counterpart and no consumer.

Both fields are left in place, marked in `backends.py` with a `ponytail:` comment naming
the ceiling and the upgrade path — an extractor, not a field.

---

## 6. Severity: what is structurally missing

The chain the report needs is `verified evidence → observation → inference → impact →
severity`. Traced against `Finding`:

| link | field | author | verified |
|---|---|---|---|
| verified evidence | `evidence_quote`, `evidence_ref` | lens | ✅ `verify_evidence` |
| observation | `verified_observation`, `evidence_class` | **harness** | machine-written |
| inference | `reasoning` | lens | ❌ |
| impact | `conclusion` — "what the lens concludes follows" | lens | ❌ |
| severity | `severity`, `severity_rationale` | lens | ❌ **and it is what the verdict counts** |

Impact is present, as `conclusion`. Adding a separate `impact` field would split one
unverified assertion into two unverified assertions and represent nothing new. A
per-finding `severity_support` field would be worse: it would be a pure function of
`evidence_class`, which is already on the finding.

**What was genuinely missing is not a field on the finding — it is the load.**
`severity_review` lists the FATAL/MAJOR findings resting on prose. It cannot say whether
they matter. On this corpus they matter decisively:

```
                     all findings   cell-verified only
2024-icml-sapg       RED            YELLOW      (15 → 8 findings)
apt-icml             RED            YELLOW      (17 → 5 findings)
sanchez24a-icml      RED            RED         (15 → 4 findings)

MAJOR findings:  14 cell_verified · 15 prose_verified
```

Two of three RED verdicts rest on grades the harness cannot check.

`stages/report.verdict_sensitivity` applies **the same threshold table** to the
cell-verified subset of **the same findings**, selected by `evidence_class`, which the
harness wrote. No second grader, no second opinion, no demotion, no threshold change. It
is recorded as `EvalReport.verdict_if_cell_backed_only` and rendered in the existing
severity caveat. A reproduction failure passes through unchanged, because it is
arithmetic against a cell rather than a graded finding.

**What this still does not do.** It does not make severity earned. A lens that quotes a
table cell accurately and grades it generously still moves a paper to RED, and the
sensitivity line will report that the verdict is *not* dependent on prose — truthfully,
and while still resting on an unchecked grade. `ponytail:` the ceiling is that severity
remains asserted; making it earned needs a second independent grader over the same
evidence, which is a real subsystem and not one to build speculatively.

---

## 8. Runtime demands read from the checkout

`code_audit` reads the tree it already walks for defects and records what the checkout says
it NEEDS, in `CodeAudit.runtime` as `RuntimeDemand{kind, state, value, scope, file, line,
code_quote, note}`. Seven kinds, each with a rigid grammar behind it. `CodeAudit
.declarations_scanned` lists the non-Python files read, so "no demand found" is
distinguishable from "nothing was read".

**All of it is report-only, and that is a finding rather than a shortcut.** Six rules were
designed against the APT checkout and then adversarially attacked. Every one came back
`needs_narrowing`, with 19 false positives rated fatal between them, and all six narrowing
recommendations were the same: the evidence supports an *observation* and not a
*conclusion*. So nothing here reaches `select_for` or `authorize` — pinned by
`test_no_runtime_demand_reaches_selection_or_authorization`, which asserts it against the
two function signatures rather than against intent.

Four of the rules that were killed, because each one looked sound:

| rule | why it was wrong |
|---|---|
| compare declared Python against the backend's | conda `=` is fuzzy, not PEP 440: `python=3.9` is the 3.9 *series*. And `environment.yml` here is a `conda env export` of one developer's machine (`prefix: /home/bowen/...`), so a mismatch would rest on a normalisation this module invented |
| a `cuNN` pin means a GPU is required | `nvidia-cublas-cu11` is a payload of .so files. It installs and imports on a CPU-only host |
| co-present cu11 and cu12 pins conflict | they are torch 2.0.1's transitive closure and `cupy-cuda12x`'s. Two libraries, two closures, no contradiction — reading one reports a conflict that is not in the file |
| a `load_dataset` literal is a dataset | `csv`, `json`, `parquet`, `imagefolder` … are builders inside the `datasets` wheel. Their demand is "this local path must exist" |

What the grammars refuse, by construction: a demand from a **comment** (this harness's own
`requirements.txt` carries `cu126`, `cu121` and "Python 3.13" on commented lines and yields
nothing); a Python version from a package whose name merely starts with `python`
(`python-dateutil==2.9.0.post0` is in APT's own file); a checkpoint from a **variable**
(21 of APT's 25 `from_pretrained` sites — a rule that guessed from same-file literals
offered `'right'`, `'epoch'` and `'max_length'`); an absolute path from **shell text** (a
regex for it produced 68 hits over 172 real scripts of which 3 were real, slicing
`/elastictuning` out of a relative `output/${model}/…` and turning `~/miniconda3/x` into a
`/miniconda3` that exists nowhere) — only Python string literals are read.

**Scope.** A demand is `experiment`-scoped only when its file is the one an *established*
experiment identity names, via `CandidateCommand.source_ref`; `repository` otherwise.
Identity resolves after the static audit, so `scope_demands` re-labels in `plan_execution`
once there is a file to bind against. This matters concretely: APT holds 74 submission
scripts asking for 32G, 40G or 64G and for 24 to 500 hours, so an unbound scheduler block
is summarised as **one** `ambiguous` row rather than 592 `established` ones — no aggregate
of those figures describes an experiment anyone ran.

**On APT** (the only pilot with a checkout; SAPG and CFG advertise no repository, so the
audit is `skipped` and there is nothing to read): 19 demands, all `repository`-scoped
because experiment identity is `no_candidate`. `python_version` established at
`environment.yml:16`; 4 `cuda_runtime` families; 4 `external_download` at
`scripts/prepare_data.sh:5,10,17,18`; 2 `model_artifact`; 6 `dataset_artifact`; 1
`absolute_path` at `post_analysis.py:468`; 1 `ambiguous` scheduler row.

One thing the extraction surfaced that no requirement field holds: `requirements.txt` pins
`torch==1.10.2+cu113` and `environment.yml` pins `torch==2.0.1`. Two different stacks, a
major version apart, and nothing in the tree records which produced the published numbers.

---

## 7. Answers

**Can a backend be selected because a requirement was never represented?** Before this
audit, yes, and it reached `authorize` — one stated field vouched for five unstated ones,
memory among them. That is closed. It remains true for the ten requirements of §3 that
have no field at all: selection proceeds because there is nothing to check. The
difference is that those all fail safe at startup, and the memory leak did not — it
permitted a run that would OOM and reconcile as the authors' code failing.

**Does the harness know enough about an arbitrary experiment?** For memory, platform,
dependencies, commit, identity and seed policy: yes, or it refuses. For Python version,
CUDA, dataset and checkpoint availability, downloads, precision and distribution: no, and
it does not know that it does not know.
