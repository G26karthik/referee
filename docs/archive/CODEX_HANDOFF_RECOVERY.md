# Codex handoff recovery

Recovered on 2026-09-15 from the interrupted Claude-controlled evaluation described in
the external handoff. This record is grounded in the working tree and generated artifacts.
It will be amended as the recovery audit closes.

## Safety snapshot

Before substantive changes, the working state was preserved under
`docs/handoff_snapshot_20260915-021252/`:

- `working_tree.patch`: binary-capable `git diff` snapshot;
- `status-short.txt`: complete `git status --short -uall` output;
- `untracked-files.txt`: complete `git ls-files --others --exclude-standard` output;
- `top-level-run-dirs.txt`: top-level run-directory inventory.

No reset, checkout, clean, stash, deletion, or overwrite was performed.

## Repository state found

- Working directory: `C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness`.
- The checkout is heavily modified: 54 tracked files differ from `HEAD`, with about
  49,070 inserted and 1,354 deleted lines in the initial diff stat, plus many untracked
  source, test, documentation, manuscript, and run artifacts.
- The modified files include the core controller, evidence and execution paths, report
  path, manuscript, requirements, and tests. The untracked files include the alignment,
  disposition, exhaustion, materiality, reconstruction, document-integrity, guarantee,
  and evaluation modules plus their tests.
- Existing work is therefore treated as user or previous-agent work and is retained
  unless a test or call-path audit proves it invalid.
- No `AGENTS.md` was present. `CLAUDE.md`, `HANDOFF.md`, and `README.md` are the applicable
  repository instructions found at takeover.
- No active lens-review, `run.py`, LaTeX, or evaluation worker was found. Two legacy
  Claude Code session shells remain alive but idle: a three-second sample showed no
  meaningful CPU or I/O activity, and neither has a lens-review child or mentions the
  interrupted run path. They were left untouched. The recovery pytest workers belong to
  this Codex takeover.

## Run identities found

- `projects/`: historical evaluated artifacts. These are preserved.
- `runs_postfix/`: earlier deterministic checkpoint reruns. These are preserved.
- `runs_final_2026-09-14/`: present with eight project directories and is classified as
  the **interrupted Claude pilot**, regardless of its original name. It is not admissible
  as the final eight-paper evaluation and will not be merged with a Codex-controlled run.
- A final Codex-controlled run did not exist at takeover. Any final run will use a new
  directory and a single declared controller/model stack.

The eight project identifiers found in the interrupted pilot are:

1. `0c06a98d7c818f6f`
2. `2024-icml-sapg`
3. `5993d35ff0996b52`
4. `acl`
5. `apt-icml`
6. `cvpr`
7. `iclr`
8. `sanchez24a-icml`

The preflight requests eight distinct documents, marks all eight `NEW`, and its full PDF
SHA-256 values agree with the project/controller/document records:

| project | PDF SHA-256 |
|---|---|
| `0c06a98d7c818f6f` | `d6295997a2edf814e5908db3dff179f24cb588b97991a23f7b81ddafe0de000e` |
| `2024-icml-sapg` | `a0dd4d4db8ae96c77ef8895383b408a8ed32e985680a3d5399e2fdc960a08580` |
| `5993d35ff0996b52` | `cb791453c637b58cff0c1c62be53c747829ba66bf7dc0344556d3ea7ac87625c` |
| `acl` | `20a4d354b7e76b6ca7ce5de7ee29b60f831eae9b1d1ab6fcac3aadb77f7f11c9` |
| `apt-icml` | `7f8c6fb10765b79a9e0b57f138074f899b6971e23697c622c79a2211ea3f7655` |
| `cvpr` | `7bd4c8336fb9e8f1378fb326291726bbb657c63402248d546909982b1131fb75` |
| `iclr` | `c324730f007ecff88d906e632b08b5c1c15c2d26877465abd8d0a870d69a0e48` |
| `sanchez24a-icml` | `5b32bee704c72a7d4e13063b797c232460f094700a7e51cb3f15e18ab2e43bb6` |

## Handoff claims verified so far

- `runs_final_2026-09-14/` exists and contains all eight project identities. All eight
  have a fresh `doc.json` and four fresh prompt files, for 32 prompts total.
- `tools/baseline_prompt.py` exists and constructs the single-model arm from the same
  rendered audit context while withholding REFEREE results.
- `runs_final_2026-09-14/HUMAN_STUDY_PACKET.md` exists and explicitly forbids synthetic
  human data. It contains a fixed 60-minute review protocol and a blinded criterion-level
  adjudication rubric.
- `manuscript/fig2_decision.tex` and a compiled one-page
  `manuscript/fig2_decision.pdf` exist. A fresh 180 dpi render was visually inspected on
  takeover: no text overlap, connector collision, clipping, or ambiguous boundary was
  visible; the human referee is outside the dashed autonomous boundary.
- The current tree collects **1,936 tests**, not the approximately 1,933 stated in the
  interrupted handoff. The complete suite finished with **1,936 passed, 0 failed,
  0 skipped in 409.82 seconds**.
- `harness/exhaustion.py` exists, but the initial production-reference search found no
  call site outside that module. Until the call-path audit closes, route exhaustion is
  classified as a partial helper rather than an end-to-end capability.

## Work known to be partial or interrupted

- Of the 32 pilot lens prompts, only the four ACL outputs were accepted and sealed. Six
  more JSON outputs are syntactically valid but staged and unsealed: two for
  `0c06a98d7c818f6f`, three for `2024-icml-sapg`, and one for `5993d35ff0996b52`.
  Twenty-two lens outputs are absent. No staged output has a driver sidecar or trustworthy
  writer/model/controller attribution, so none is adopted into a final run.
- The seven non-ACL controllers remain at `phase=audit`, `status=waiting`, with empty
  report and run directories. They have no grade or whole-paper assessment output.
- ACL alone reached a completed controller state. Its four lens sidecars name
  `claude-sonnet-5 via session subagent`, `SESSION_SUBAGENT`, and an unrecorded,
  unprovable tool policy. The pipeline kept 10 of 11 findings after quotation checking,
  generated 9 questions, settled 0, identified 27 targets with 25 addressable, warranted
  2 experiments, launched 0 processes, and finished `claim_status=NOT_VERIFIED`,
  `verdict=GREEN`, `disposition=BLOCKED_ARTIFACT`. Its whole-paper
  `substantive_verdict` is null and its self-audit fails
  `whole_paper_judged_independently`, so even ACL is not a complete scientific panel result.
- The pilot root `reports/corpus.json` requests only seven papers, omits ACL, and records
  `complete=false`. There is no run-root manifest recording parent controller, code
  revision/diff, prompt versions, uniform model configuration, or a reproducible digest.
- The prior 147/25/43/42 author-code identity sweep with zero established identities is
  preserved only as a narrative claim in `docs/REVISION_2026-09-14_final.md`. No durable
  per-target sweep log makes it independently re-derivable. The interrupted ACL pilot
  considered three advertised commands for one selected target, not 25 targets.
- The fresh single-stack Codex eight-paper run, baseline outputs, honest ablations,
  stabilized metrics, manuscript rewrite, claim checks, final build, and feedback closure
  table were not complete at takeover.

## Interrupted-pilot artifact hashes

Selected SHA-256 values recorded during recovery:

| artifact | SHA-256 |
|---|---|
| `reports/preflight.json` | `f7651ce7f25ee2c26d50c80dd9775b96db1856ec52b00b6d0faf7d78efa123e8` |
| stale root `reports/corpus.json` | `116450e601a2bcc192c8e6b91f670d1805f4c20c6848e5d92828a7172e8ad316` |
| ACL `controller.json` | `5e12bdadc950d1c60fc94c0f09abb86fe1c05c9bd5639741b47388d9cc471523` |
| ACL `discovery/targets.json` | `396514d1eb6d833d88bc0979c7010ec7d90c40edf2bcacc56f9f216f75c25d61` |
| ACL `probe_results.json` | `4394970b65eb32e0a374c85f9ee76b5125d13ac51c7b0d5e7ffba28e97aeaadd` |
| ACL report JSON | `d3b00eb2d47f3fa6cbaadf00aee87fba58968c4dfbcb91c4f7dde78c710d46fd` |
| ACL ledger | `0dcb1b4cb50c4c42a7eda9a564d4ce85df5a158d3340e414df8f818edaef2ea7` |
| ACL reviewer report | `b0b6149549166f879de70f08c3b39575cb0ad66caec2175b6260dba32971157f` |

## Retention decision

Retain the existing implementation, tests, documentation, baseline prompt, human packet,
and Figure 2 pending their focused audits. Preserve every historical and interrupted run.
Do not use interrupted Claude lens outputs, grades, assessments, questions, or outcomes in
the final Codex corpus.
