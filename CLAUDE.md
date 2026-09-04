# single-harness

An autonomous replication auditor for papers. In: PDFs. Out: a RED/YELLOW/GREEN
report per paper, with a machine-verified evidence pointer behind every finding, plus a
reproduction verdict when — and only when — one can be earned.

You are the controller's reviewer. `harness/controller.py` drives; deterministic code
below it decides what may be concluded. Neither side may overrule the other.

## Workflow

```
papers → controller → ingest → audit → collect → grade → verify → execute → reconcile → report
```

| phase | module | input → output | decides | refuses by |
|---|---|---|---|---|
| ingest | `stages/ingest.py` | PDF → `paper/doc.json` | nothing (deterministic) | `error` on an unreadable PDF |
| audit | `stages/audit.py` + `audit_driver.py` | doc → `audit/<lens>.json` ×4 | the four lenses judge | `waiting`, resumable |
| collect | `stages/audit.load_reports` | lens files → verified findings | quote ≟ paper | drops the finding, counts it |
| grade | `stages/grade.py` + `grade_driver.py` | serious findings → `audit/grade/<slug>.json` | a second, blinded reviewer per candidate | `ok` with partial coverage — never blocks a report by default |
| verify | `stages/probe.py` | doc + repo → `ProbeSpec` | identity, capability, resources, commit, backend | leaves the spec unpromoted |
| execute | `backends.py` + `local_exec.py` | spec → `ProbeResult` | `authorize()` alone | `verdict: blocked` |
| reconcile | `local_exec.reconcile` | metric vs cell | arithmetic only | `INCONCLUSIVE` |
| report | `stages/report.py` | everything → `reports/<pid>.md` | threshold table, over `counted_severity` | — |

Every started process is recorded whole in `runs/<pid>/execution.jsonl` — command, cwd,
commit, timestamps, exit code, full stdout/stderr, parsed metric. A reproduction verdict
must be re-derivable from that file by hand.

## Commands

```bash
python run.py review --paper a.pdf b.pdf c.pdf --auto-audit --auto-grade  # the entrypoint
python run.py review --paper a.pdf                            # exit 2 → lenses pending
python run.py status <paper-id>                               # controller state + history
python run.py list                                            # reviewed papers
python run.py dossier                                         # consolidate finished reports
python -m pytest tests -q                                     # 711 tests
```

Always `PYTHONUTF8=1` on Windows (paper text is full of em dashes and math) and always
the repo venv: `../.venv/Scripts/python.exe`.

Self-checks, one per module: `python -m harness.<pdf|local_exec|repo|code_audit|
probe_synth|dossier|audit_driver|grade_driver|verdict_driver|grading|backends|resources|
controller>` and `python -m harness.stages.<report|grade>`.

## Immutable invariants

Do not weaken these to make more papers executable or more findings reportable.

1. Every `evidence_quote` is re-verified against the parsed paper; a cell citation must
   match that cell. Unsubstantiated findings are dropped and counted.
2. `verified_observation` and `evidence_class` are written by the harness, never read
   from a lens file. A lens cannot certify its own reasoning.
3. **Provenance ceiling** — only `driver` or `repo_exec` provenance may reconcile a
   printed cell, in *either* direction. A synthesized probe can neither convict nor
   acquit.
4. Only `authorize()` may permit repository execution, and it requires all of: gate
   open, a backend that `can_execute`, `repo_exec` provenance, a verified commit,
   experiment + metric + configuration identity, capability, and sufficient resources.
5. Commit mismatch, a dirty tree, or an unverifiable commit blocks execution.
6. Resource insufficiency — including *unknown* demand — yields `INCONCLUSIVE`. A
   paper's silence about its own cost is not evidence the cost is small.
7. Capability, environment, dependency and platform failures yield `INCONCLUSIVE`, never
   `FAILED_REPRODUCTION`. Only a crash *after* the experiment demonstrably started may
   convict.
8. Verdict thresholds are a table in `stages/report.py`, not a judgement:
   `RED_FATAL=1`, `RED_MAJOR_ONE_LENS=3`, `RED_MAJOR_TOTAL=10`, `YELLOW_MAJOR=1`,
   `YELLOW_MINOR=4`. Independent grading (`harness/grading.py`) does not touch these
   numbers — it changes what is *eligible* to be counted at each severity, and it can
   only demote a lens's own asserted grade, never promote one. Turning grading off, or
   never running it, reproduces the pre-grading verdict exactly: `counted_severity`
   stays empty and `stages.report.counted()` falls back to `severity`.
9. No experiment is shrunk, substituted or downscaled to make it fit. There is no
   function that does this, deliberately.
10. No paper-specific logic. The pilot papers are evaluation cases, not special cases.

## Operating autonomously

`--auto-audit` delegates each lens to a reviewer — `SH_AUDIT_CMD`, or the `claude` CLI
discovered on PATH — as **one subprocess per lens**, which is stronger isolation than
four lenses read in one session. It is opt-in because it spends tokens.

Without it, `review` writes `audit/prompts/<lens>.md`, exits 2, and resumes when the
lens files exist. If you fill them yourself, **run each lens in a separate turn**: four
independent readings are four pieces of evidence; one context that remembers the
previous three is one reading echoed four times.

Abstention is an outcome, not a failure. A paper with no repository, an ambiguous
experiment or a 24 GiB demand on an 8 GiB card still gets a complete review;
`CaseState.reproduction_class` names why reproduction did not conclude.

Never edit `projects/<pid>/audit/prompts/*.md` — regenerated every run.

## Execution gates

| gate | env var | default | permits |
|---|---|---|---|
| network | `SH_ALLOW_NETWORK` | on | `git clone --depth 1` of the URL the paper advertises |
| synthesis | `SH_ALLOW_SYNTHESIS` | on | the planner authors `runs/<pid>/probe.py` from the paper |
| auto-audit | `SH_ALLOW_AUTO_AUDIT` | off | shelling out to a reviewer for the lenses |
| grading | `SH_ALLOW_GRADING` | off | a second, blinded reviewer per FATAL/MAJOR finding — zero tools, no filesystem access at all (`grade_driver.py`) |
| substantive verdict | `SH_ALLOW_SUBSTANTIVE_VERDICT` | off | one best-effort, never-retried, whole-paper opinion — printed, consumed by no threshold (`verdict_driver.py`) |
| install | `SH_ALLOW_INSTALL` | off | building `runs/<pid>/env` from the repo's requirements |
| execute | `SH_ALLOW_REPO_EXEC` | off | running the repository's own entrypoint |

The audit lenses themselves run with `--allowedTools`, `--add-dir <papers dir>`, and a
sandboxed empty cwd (`audit_driver.py`) — a lens can `Read` the original PDF to settle a
table/figure/equation-dependent ambiguity (`SOURCE_FIDELITY` in the prompt), but not its
sibling lenses' files or the harness's own source. PDF inspection can KILL a candidate
or point at the right unit to cite; it can never BE the evidence — `verify_evidence`
still only accepts a quote it can re-check against the parsed doc (a table cell, a
section, a figure caption, or an extracted equation line), each capped by
`harness.grading.EVIDENCE_CEILING` at what that citation strength can actually earn.

Backends: `local` (real), `kaggle` and `colab` (declarations — published specs,
`can_execute=False`, `execute()` raises). `backends.select_for` matches the experiment's
declared demand against every profile, so a refusal can say *"a 16 GiB T4 would fit but
cannot be provisioned from here"*.

## Known limitations

- `severity` is still model-asserted by the lens that wrote it. What changed:
  `harness/grading.py` now runs a second, blinded reviewer over every FATAL/MAJOR
  candidate (`--auto-grade`) and independently re-verifies its own pass-B work
  (falsification/steelman non-degeneracy, recomputed arithmetic) even with grading off.
  Neither model certifies itself — `Finding.counted_severity`, `finding_class`,
  `grader_evidence_class` etc. are harness-written from a pure derivation table, never
  read from a lens or grader file. The ceiling that remains: the grader is still a
  model, and its own judgement is not machine-checked, only its citation and the
  derivation are. `stages/report.py`'s `## Independent grading` / `## Severity caveat`
  sections say, per paper, how much of the verdict still rests on ungraded or
  prose-only assertions.
- Both reproduction verdicts are reachable and proven end to end through the real
  execution path (`tests/test_local_execution.py`, synthetic git fixture). Neither has
  been produced from a real paper: all three pilot papers refuse, APT on five independent
  blockers (see `docs/HARNESS_ARCHITECTURE.md` §6).
- Commit pinning is second-run-onward — the first acquisition of a paper is an unpinned
  depth-1 clone of the default branch.

## Facts

Torch is cu126 (cu121 publishes no wheels for Python 3.13). GPU: RTX 4060 Laptop, 8 GB,
sm_89. ~15 GB RAM. No WSL, no Docker, no virtualization on this host.
