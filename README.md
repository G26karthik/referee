# single-harness

An autonomous replication auditor for machine-learning papers.

Give it PDFs. It reads each one, runs a four-lens adversarial audit, verifies every
quoted piece of evidence against the parsed paper, then tries to settle whichever
findings an experiment could settle — acquiring the authors' repository, pinning it to
the commit it audited, and refusing to execute anything until it can prove it is running
the right code, measuring the right quantity, under the right configuration, on hardware
that fits. Out comes a RED/YELLOW/GREEN report per paper and a consolidated dossier.

Its most useful property is what it refuses to say. An experiment that does not fit the
machine, a repository whose code does not produce the cited number, a metric that cannot
be bound to the cell — each yields `INCONCLUSIVE` with a named reason, never a verdict
against the paper.

## Architecture

```
papers
  │
  ▼
controller ──────── per-paper state in projects/<pid>/controller.json
  │                 bounded retries · resumable · papers isolated
  ├─ S1 ingest      PDF → PaperDoc (sections, tables, every number with provenance)
  ├─ S2 audit       four lenses: overclaim · protocol · confound · contradiction
  ├─   collect      every quote re-verified against the paper; failures dropped
  ├─ S3 verify      repo → commit → experiment/metric/configuration identity
  │                      → capability → resources → backend → authorization
  ├─   execute      only if authorization passed. Otherwise: blocked
  ├─   reconcile    reproduced metric vs the printed cell, arithmetically
  └─ S4 report      deterministic threshold table → reports/<pid>.md
```

Three of the four stages are plain deterministic Python. Only S2 needs judgement, and
the judge is a language model — either the Claude Code session driving the CLI, or, under
`--auto-audit`, one subprocess per lens.

`docs/HARNESS_ARCHITECTURE.md` is the authoritative map, with the per-stage contract and
the full evidence chain.

## Usage

```bash
# the normal invocation
PYTHONUTF8=1 ../.venv/Scripts/python.exe run.py review \
    --paper papers/a.pdf papers/b.pdf papers/c.pdf --auto-audit
```

Exit codes: `0` every paper complete · `2` some paper waits on lens evidence · `1` error.

Without `--auto-audit` the run stops after writing `projects/<pid>/audit/prompts/*.md`
and exits 2; write `audit/<lens>.json` for each and re-run to resume.

## Example

A real refusal, from the APT pilot paper. The audit targeted cell `T2:r3:c11`, and the
report shows exactly where the chain broke:

```
| audited commit        | 56eaf8bc8624 (verified)              |
| experiment identity   | no_candidate                         |
| metric identity       | unmapped                             |
| resource sufficiency  | insufficient                         |
| backend               | local (resources_insufficient)       |
| reconciliation        | INCONCLUSIVE                         |

⛔ The chain breaks at experiment identity.
```

The cited row reports `LLMPruner` — a third-party baseline the authors' code does not
produce — so no execution of their repository could settle it. Separately, the experiment
declares 24 GiB of VRAM against 8 GiB present, and no registered backend can host it.
Both facts are recorded; neither is a finding about the paper.

## Scientific guarantees

- Every finding carries a verbatim quote and a location (`p7` or `T2:r3:c4`), re-verified
  against the parsed paper. Unsubstantiated findings are dropped and the count printed.
- What the harness verified and what the model inferred are rendered separately. A lens
  cannot mark its own reasoning as confirmed.
- **Provenance ceiling**: only the authors' own checkout, or a human-written faithful
  reproduction, may reconcile against a printed cell — in either direction. A probe this
  harness synthesised can neither convict nor acquit.
- Execution is permitted by exactly one function, `backends.authorize`, and only when
  commit, experiment identity, metric identity, configuration identity, capability and
  resources all hold.
- Infrastructure failure is never scientific failure. A missing dependency, an
  incompatible platform, a shut gate and an experiment too large all yield
  `INCONCLUSIVE`.
- Nothing is shrunk to fit. There is no code path that reduces a model, batch, precision,
  sequence length, schedule or seed count to make an experiment run.
- The RED/YELLOW/GREEN call is a threshold table, not a judgement.

## Current execution limitations

Real hardware: Windows, RTX 4060 Laptop (8 GiB VRAM), Ryzen 9, ~15 GiB RAM. No WSL, no
Docker, no virtualization.

`LocalBackend` really executes: it starts processes, verifies the audited commit and a
clean tree immediately beforehand, and records every attempt whole in
`runs/<pid>/execution.jsonl`. Both reproduction verdicts are proven end to end through
that path against a synthetic git fixture (`tests/test_local_execution.py`) — a fixture
that is plumbing evidence and never a paper reproduction.

**No pilot paper can execute here.** SAPG and CFG advertise no repository at all. APT has
five independent blockers: its cited row reports a third-party baseline its own code does
not produce, the metric cannot be bound to the cell, the stack is `linux-64` against a
`win32` host, the experiment declares 24 GiB against 8 GiB present, and no registered
backend can host it. The first two are properties of the citation; the rest are limits of
this machine.

Provisioning is `python -m venv` plus pip requirement files, verified on this host (~9 s,
isolated, no conda, no host mutation). A repository whose stack is a conda
`environment.yml` cannot be provisioned — itself an abstention, not a paper failure.

Kaggle and Colab are registered as **declarations** — real published specs,
`can_execute=False`, `execute()` raises — so selection can report that a T4 would fit
while stating it cannot be provisioned from here. They are not integrations.

## Repository

```
run.py                 CLI. Formats; decides nothing.
harness/
  controller.py        the driver: phase machine, retries, batch scheduling, entry points
  artifacts.py         every typed artifact that crosses a stage boundary
  config.py            paths and gates, all from the environment
  stages/
    ingest.py          S1
    audit.py           S2 prompts + evidence verification
    probe.py           S3 planning
    report.py          S4 ranking, thresholds, rendering
  audit_driver.py      optional lens delegation, one subprocess per lens
  backends.py          execution backends, requirement matching, authorization
  experiment_id.py     experiment / metric / configuration identity
  resources.py         what the cited experiment costs vs what a backend offers
  repo.py              acquisition, commit pinning, capability
  code_audit.py        static AST audit of the checkout — never imports, never runs
  probe_synth.py       synthesises a probe from the paper's own formulation
  local_exec.py        runs probes, parses metrics, reconciles
  pdf.py               PDF → sections, tables, numbers
  dossier.py           cross-paper consolidation
  prompts/audit.py     the four lens prompts
tests/                 647 tests
docs/                  the architecture map
papers/                source PDFs
projects/<pid>/        per-paper state: paper, audit, runs, reports, controller.json
```
