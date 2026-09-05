# single-harness

An autonomous replication auditor for papers. In: PDFs. Out: a binary RED/GREEN
decision per paper, with a machine-verified evidence pointer behind every finding, plus a
reproduction status reported separately and earned — or refused — on its own evidence.

RED means a material failure was ESTABLISHED. GREEN means one was not, within the audited
scope — it is not a certificate of correctness, and `claim_status` keeps VERIFIED_SUPPORT
("checked and held") apart from NOT_VERIFIED ("could not check") underneath it.

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

**Three questions, never one.** The reviewer's whole job is keeping these apart, and
every pure module below exists to stop one of them collapsing into another:

| question | where it lives | what it may do |
|---|---|---|
| Is there an issue? | `candidate_class` (lens) + `grade.verdict` (blinded grader) | CONFIRMED_FINDING is the only bucket eligible for FATAL/MAJOR — `grading.CANDIDATE_CAP` enforces it |
| How sure are we? | `confidence`, bounded by `grading.evidence_support` | evidence TYPE caps confidence; a second independent check LIFTS the cap |
| How much does it matter? | `severity` → `counted_severity` | nothing here SETS it; every mechanism only caps it |

Plus, over the finished report: `harness/selfaudit.py` (did the review exercise its own
discipline — 12 machine-checked items, verdict-inert) and `harness/corpus.py` (every
requested paper in exactly one terminal state, conservation law asserted).

## Commands

```bash
python run.py review --paper a.pdf b.pdf c.pdf --auto-audit --auto-grade  # the entrypoint
python run.py review --paper a.pdf                            # exit 2 → lenses pending
python run.py status <paper-id>                               # controller state + history
python run.py list                                            # reviewed papers
python run.py dossier                                         # consolidate finished reports
python -m pytest tests -q                                     # 766 tests
```

Always `PYTHONUTF8=1` on Windows (paper text is full of em dashes and math) and always
the repo venv: `../.venv/Scripts/python.exe`.

Self-checks, one per module: `python -m harness.<local_exec|repo|code_audit|probe_synth|
dossier|audit_driver|grade_driver|verdict_driver|grading|failures|selfaudit|corpus|
backends|resources|controller>` and `python -m harness.stages.<report|grade>`.
`python -m harness.pdf <file.pdf>` takes a PDF path.

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
8. The paper decision is a materiality TABLE in `stages/report.py`, not a judgement and
   not a count: `MATERIAL_SEVERITY = ("FATAL",)`. RED iff `claim_status` is
   VERIFIED_FAILURE — a failed reproduction from an admissible provenance, or a counted
   FATAL. Nothing accumulates: no number of MAJORs or MINORs ever reaches RED, because a
   concern weakens a claim and does not reject one, and the old `RED_MAJOR_ONE_LENS=3` /
   `RED_MAJOR_TOTAL=10` thresholds made the decision a property of how many things a
   panel chose to write down rather than of the paper. Independent grading
   (`harness/grading.py`) still cannot promote: it changes what is *eligible* to be
   counted at each severity and can only demote a lens's own asserted grade. Turning
   grading off, or never running it, reproduces the ungraded decision exactly:
   `counted_severity` stays empty and `stages.report.counted()` falls back to `severity`.
9. No experiment is shrunk, substituted or downscaled to make it fit. There is no
   function that does this, deliberately.
10. No paper-specific logic. The pilot papers are evaluation cases, not special cases.
    `grading.derive`'s signature admits vocabulary strings and booleans only — no count,
    no number, no metric name, no paper identity — so "no std dev = MAJOR", "if
    epsilon=0.05 never flag" and "if <paper> appears, soften" are *inexpressible*, not
    merely absent. `tests/test_reasoning_architecture.py` asserts the signature, and
    that no prompt names a pilot paper or states a severity floor prescriptively.
11. **Every mechanism that touches severity may only CAP it.** Nothing in the reviewer
    can raise a grade: not the grader, not the lens's pass-B work, not the evidence
    ceiling, not any self-consistency cap. Asserted by sweep over the whole reachable
    input space, `RANK[counted_severity] <= RANK[lens_severity]`.
12. Nothing may collapse the three questions above into one. In particular no cap is
    keyed on *evidence type* — a weak citation bounds CONFIDENCE, and confidence bounds
    severity. `caption → NOTE` used to be such a cap and was wrong for the same reason
    `no variance → MAJOR` was: both decide impact from something that is not impact.
13. Every requested paper reaches exactly one terminal state, and the accounting asserts
    it (`harness/corpus.py`). A summary may say "6 requested · 5 completed · 1 failed";
    it may never say "5 reviewed" when six were asked for.
14. A retry budget is spent only where spending it could change the answer
    (`harness/failures.py`). A rate limit, an outage, a missing CLI, a revoked
    credential, an unknown flag and an unreadable PDF are not transient failures and
    must not consume an attempt.

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

**A clean paper is a real result.** Zero FATAL/MAJOR findings, several MINOR concerns and
a handful of open review questions is a complete, correct review of good work — not a
reviewer that failed to try. `prompts/audit.STANCE` says so to the lens, `## Open review
questions` prints the questions as questions, and `grading.CANDIDATE_CAP` makes sure they
count toward no threshold. Do not read a GREEN as a missed finding.

A batch's completeness is a machine-readable artifact, not a printed count: `reports/
corpus.json` lists every requested paper with its terminal state and, when it did not
finish, the `failure_kind` that stopped it.

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
section, a figure caption, or an extracted equation line). What a weaker citation bounds
is CONFIDENCE, in `harness.grading.EVIDENCE_CONFIDENCE_CEILING` — a caption reports none
of a figure's plotted values (LOW), equation extraction is lossy (MEDIUM), a cell or a
verbatim section quote is checkable as-is (HIGH) — and confidence is what caps severity.
A second independent check LIFTS the ceiling one step: the grader's own citation, or a
`recomputed_ok` calculation the harness re-verified operand by operand. So a figure
concern is neither silenced nor free — it earns what it can corroborate.

Backends: `local` (real), `kaggle` and `colab` (declarations — published specs,
`can_execute=False`, `execute()` raises). `backends.select_for` matches the experiment's
declared demand against every profile, so a refusal can say *"a 16 GiB T4 would fit but
cannot be provisioned from here"*.

## Known limitations

- **No paper has yet been RED under the binary rule.** RED needs a counted FATAL or a
  failed reproduction from an admissible provenance; the seven papers reviewed so far
  produced neither, and all are GREEN with concerns printed. That is the intended
  conservatism, but it means the RED path is proven only by fixtures, not by a real
  paper.
- **PATH B is eligibility-only so far.** `harness/reimplement.py` decides whether a paper
  with no published code says enough to rebuild, and writes the brief when it does. No
  independent reimplementation has actually been written and sealed through
  `run.py accept`, so the INDEPENDENT_REIMPLEMENTATION provenance is exercised by tests
  and by the `driver` path, not yet end to end from a real no-code paper.
- `severity` is still model-asserted by the lens that wrote it. What changed:
  `harness/grading.py` now runs a second, blinded reviewer over every FATAL/MAJOR
  candidate (`--auto-grade`) and independently re-verifies its own pass-B work
  (falsification/steelman non-degeneracy, recomputed arithmetic) even with grading off.
  Neither model certifies itself — `Finding.counted_severity`, `finding_class`,
  `grader_evidence_class` etc. are harness-written from a pure derivation table, never
  read from a lens or grader file. The ceiling that remains: the grader is still a
  model, and its own judgement is not machine-checked, only its citation and the
  derivation are. `stages/report.py`'s `## Independent grading` / `## Severity caveat` /
  `## Reviewer self-audit` sections say, per paper, how much of the verdict still rests
  on ungraded or prose-only assertions, and which parts of the discipline this particular
  review did not exercise.
- The whole-paper judgement (`## Whole-paper assessment`) is reasoned rather than counted
  — the one thing a threshold table structurally cannot do — and it is **printed, never
  counted**. Its only consequence is a CONTESTED flag and `run.py` exit 3 when it
  disagrees sharply with the table. That is the deliberate settlement between "do not
  derive the verdict by counting findings" and invariant #8; a model that could write the
  colour would make every gate under it advisory.
- Extraction still bounds what can be cited, and says so rather than guessing. Display
  equations are recovered in both real-world shapes (body-and-number on one line, and a
  right-margin number text extraction put on its own), but a paper whose equations
  shatter into per-glyph fragments yields zero — `pdf._equation_body` returns nothing
  rather than attaching a number to whatever text precedes it, because a wrongly
  assembled body would produce a false `equation_verified` attestation.
- Both reproduction verdicts are reachable and proven end to end through the real
  execution path (`tests/test_local_execution.py`, synthetic git fixture). Neither has
  been produced from a real paper: all three pilot papers refuse, APT on five independent
  blockers (see `docs/HARNESS_ARCHITECTURE.md` §6).
- Commit pinning is second-run-onward — the first acquisition of a paper is an unpinned
  depth-1 clone of the default branch.

## Facts

Torch is cu126 (cu121 publishes no wheels for Python 3.13). GPU: RTX 4060 Laptop, 8 GB,
sm_89. ~15 GB RAM. No WSL, no Docker, no virtualization on this host.
