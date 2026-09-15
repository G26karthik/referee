# The eight-paper run, 2026-09-09 — stopped at the audit boundary

**The run did not resume to completion. It stopped at the S2 audit stage because BOTH
delegation mechanisms available in this environment are exhausted.** No review was
produced. 13 of 32 lens artifacts exist. Nothing below is a finding about any paper.

This document reports the six states separately, because they are six different facts and
conflating any two of them would misdescribe the system.

---

## 1. Implementation-phase session-limit events (an EARLIER, FINISHED phase)

Seven agents in the implementation workflow failed on a provider session limit whose
window reset at **9pm (Asia/Kolkata)**: `build:delegation`, `verify:extraction`,
`verify:docintegrity`, `verify:coverage`, `verify:guarantees`, `verify:selfchecks`,
`verify:delegation`.

Five of six build agents completed. The sixth landed substantial work before the limit and
its subject was verified by hand. **This says nothing about the run below** — different
phase, different window, before the run was launched.

## 2. Limits encountered during this eight-paper run — THREE separate events

| # | when | mechanism exhausted | window resets | effect |
|---|---|---|---|---|
| A | 2026-09-08 | **account** — `claude` CLI subprocesses (`--auto-audit`) | 3am | 2 of 32 lenses produced, then all 8 papers `waiting` |
| B | 2026-09-09 | **this session** — subagent dispatch | 2am | 11 further lenses produced, 22 tasks failed |
| C | — | — | — | no third mechanism exists in this environment |

Event A was recorded by the harness as `failure_kind=rate_limited` on all eight papers,
verbatim: `You've hit your session limit · resets 3am (Asia/Kolkata)`.

Event B was reported by 22 of 30 subagents: `You've hit your session limit · resets 2am
(Asia/Kolkata)`.

**Both delegation modes this environment offers are now spent.** `delegation.choose` over
`modes_available(cli_gate_open=True, session_can_delegate=True)` returns a mode, and
neither can currently execute. That is the boundary, and it is a capacity limit, not a
defect in the pipeline and not a property of any paper.

## 3. Execution mode actually used, per stage

| stage | mode used | status |
|---|---|---|
| preflight | deterministic, no delegation | **complete** — 8 files, 8 distinct hashes, exit 0 |
| ingest (v2 extraction) | deterministic, no delegation | **complete** — all 8 at `extraction_version: 2` |
| audit (delegated lenses) | `CLI_SUBPROCESS` ×2, `SESSION_SUBAGENT` ×11 | **BLOCKED at 13/32** |
| collect | deterministic | not reached |
| grade | needs delegation | not reached — prompts are written by `collect`, which has not run |
| discover / prioritise | deterministic, no delegation | not reached |
| substantive assessment | needs delegation | not reached — prompt is written at `report` |
| execution + reconciliation | harness's own path, **no delegation applies** | not reached; unaffected by the limits |
| outcome / coverage / integrity / guarantees | deterministic, no delegation | not reached |
| report | deterministic | not reached |

Repository execution deserves a line of its own: it is **not** a reasoning task, so no
delegation mode applies to it. It runs through `backends.authorize` → `local_exec.run_probe`
on this host, gated exactly as before, and it is unaffected by either limit. It has not run
because the pipeline never reached it.

## 4. Papers and stages: completed, waiting, blocked, failed

| paper_id | content_sha | ingest | lenses | by mode | state |
|---|---|---:|---:|---|---|
| `0c06a98d7c818f6f` | `d6295997a2ed` | v2 | **4/4** | 2 CLI + 2 subagent | audit complete, downstream not run |
| `2024-icml-sapg` | `a0dd4d4db8ae` | v2 | 3/4 | 3 subagent | **waiting** — 1 lens short |
| `5993d35ff0996b52` | `cb791453c637` | v2 | **4/4** | 4 subagent | audit complete, downstream not run |
| `acl` | `20a4d354b7e7` | v2 | 2/4 | 2 subagent | **waiting** — 2 lenses short |
| `apt-icml` | `7f8c6fb10765` | v2 | 0/4 | — | **waiting** |
| `cvpr` | `7bd4c8336fb9` | v2 | 0/4 | — | **waiting** |
| `iclr` | `c324730f007e` | v2 | 0/4 | — | **waiting** |
| `sanchez24a-icml` | `5b32bee704c7` | v2 | 0/4 | — | **waiting** |

**0 papers failed. 0 errored.** Every case is `waiting`, which is the resumable state.
19 lens tasks remain.

Two papers have a complete four-lens audit. Neither was advanced further, deliberately: a
review produced with the grading and assessment stages unreachable would be a partial
pipeline presented as a complete one, which is the downgrade this was told not to make.

## 5. What evidence this run actually produced

- **The v2 extraction transition, complete for all eight papers.** No stale v1 reference
  is carried forward anywhere. This is real, verified evidence and it is the run's main
  product so far.
- **13 lens artifacts**, each recording its own production mode, with the argv, prompt and
  response hashes for the two CLI ones. Findings counts range 6–9 per lens.
- **The per-lens model heterogeneity fix, verified on live output**: `overclaim` requested
  `opus` and the CLI reported `claude-opus-5`; `protocol` requested `sonnet` and reported
  `claude-sonnet-5`, under different enforced tool policies (11 vs 12 tools denied). In the
  archived September corpus all four lenses reported `claude-sonnet-5`.
- **The environment-agnostic delegation interface, exercised on real artifacts**:
  `delegation.summarise` over the 13 reports `homogeneous: false`,
  `{"SESSION_SUBAGENT": 11, "CLI_SUBPROCESS": 2}`, `tool_policy_provable_for: 2`. The
  corpus structurally cannot describe itself as one homogeneous run.
- **`SESSION_SUBAGENT` is sealed as itself, not as `manual_accept`.** Every one of the 11
  records `delegation_mode: SESSION_SUBAGENT`, `written_by: session_subagent`, and
  `tool_policy: unrecorded` — forced to `unrecorded` by `provenance_record` even though the
  seal was invoked with a policy string, because a subagent cannot prove a sandbox.

## 6. What was NOT exercised

- **19 of 32 lens audits.** Six papers have an incomplete panel; four have none.
- **The grading stage.** Never reached. Its prompts are written by `collect`. So
  `counted_severity` is still unexercised on a corpus and every severity in the 13
  artifacts is the asserting lens's own.
- **The substantive whole-paper assessment.** Never reached.
- **Question/target discovery, prioritisation, planning** on the new lens output.
- **Repository execution and reconciliation** — authorised by the operator, gated
  unchanged, never reached. So this run says nothing about execution, and the RED path
  remains proven on fixtures only.
- **The four report layers** (outcome, coverage, document integrity, guarantees) on any
  paper from this run. They are verified against the *archived* v1 documents and against
  fixtures, not against a v2 review.
- **Any corpus number.** `reports/corpus.json` from this run reports no completed paper.

---

## How to resume

Both windows reset overnight (2am and 3am, Asia/Kolkata). The run is resumable and
re-does nothing: the 13 sealed artifacts and the 8 parsed documents are cached, so only
the 19 missing lens tasks are asked for.

Whichever mechanism is used, the mode must be declared, because the harness cannot detect
it:

```bash
# CLI mode — spends the ACCOUNT's session
PYTHONUTF8=1 SH_ALLOW_AUTO_AUDIT=1 SH_ALLOW_GRADING=1 SH_ALLOW_SUBSTANTIVE_VERDICT=1 \
  SH_ALLOW_REPO_EXEC=1 SH_ALLOW_INSTALL=1 \
  ../.venv/Scripts/python.exe run.py review --paper papers/*.pdf --auto-audit --auto-grade

# SESSION_SUBAGENT mode — spends the controlling session; three rounds, because grade
# prompts are written by `collect` and the assessor prompt at `report`
../.venv/Scripts/python.exe run.py review --paper papers/*.pdf     # writes prompts, exit 2
#   ... controller dispatches one subagent per prompt ...
../.venv/Scripts/python.exe run.py accept --paper <pid> --mode SESSION_SUBAGENT
```

A mixed run is legitimate and is what the corpus already is. What is not legitimate is
reporting it as one mode: `delegation.summarise` exists so that the report has to say
`{"SESSION_SUBAGENT": 11, "CLI_SUBPROCESS": 2}` rather than "the delegation path".
