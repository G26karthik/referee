# Route-exhaustion decision point before the v2 run

The manuscript remains frozen. The preserved diagnostic evaluation is
`runs_final_codex_2026-09-15`; none of its evidence is copied into v2.

## A. Eleven material questions

The complete one-row-per-question table, including all 19 route records, paper addresses,
source findings, targets, attempt/completion/exhaustion flags, blocker ownership, and next
route is in `docs/FORENSIC_ZERO_LAUNCH_AUDIT.md`. Its machine-readable source is
`runs_final_codex_2026-09-15/reports/forensic_zero_launch_audit.json`. Both assert the
11-question denominator.

## B. Seventy-five warranted targets

The same artifacts list all 75 targets individually. The first-stop classes conserve:

| Class | Count | Meaning in this run |
|---|---:|---|
| D | 4 | author-code identity reported ambiguous/no candidate |
| I | 58 | run-owned three-target cap |
| J | 4 | target minting, routing, alignment, or cached-outcome defect |
| K | 9 | disabled reconstruction generation / missing durable route outcome |
| all other A-L classes | 0 | no first-stop target in that class |

## C. Root cause of zero execution

The production path was discovery → one-route planning → capped target selection → spec
construction → acquisition/alignment → authorization → backend → launch → reconciliation.
Fifty-eight targets stopped before planning work at the cap. Nine reached a reconstruction
need with its driver disabled. Four carried defective target/routing state. Four author-code
targets recorded no unique runnable identity. The run selected the Win32 local backend and
left author-code and reconstruction execution gates closed. It therefore never reached an
authorized process. This was an interaction of configuration, scheduling, persistence,
target binding, and genuine identity uncertainty, not expected evidence conservatism.

Docker Desktop 4.90.0 exposes a reachable Linux engine (29.7.2), so v2 can use the
`container` backend without lowering the isolation floor.

## D. Engineering changes

- Central material questions now bypass the numeric target cap at the question level.
- Every secondary pre-launch outcome is persisted and cached reviews explicitly conserve
  a legacy missing outcome rather than deleting it.
- A selected target now overrides unrelated globally ranked finding text, id, address, and
  value; a lone bracketed citation number is no longer minted as an experimental quantity.
- Paper-internal checks are recorded even when a later execution route is selected.
- Completed admissible inconclusive work exhausts that route without claiming the
  scientific question was settled.
- `FOCUSED_VALIDATION_EXPERIMENT` is no longer advertised by discovery while its
  between-arms evidence executor is absent.
- Automated reconstruction now invokes a fresh generator and a distinct fresh verifier;
  all five required bindings still undergo deterministic locator verification.
- The dossier branches on actual process count and never says a zero-process probe ran.

## E. Genuine external blockers

The diagnostic artifacts contain four author-code identity endpoints, but three APT rows
were contaminated by wrong target bindings and must be re-evaluated. ACL's three advertised
commands did not emit the cited 2,900 count. CVPR lacks enough paper-owned training detail
for independent reconstruction. ICLR's declared four-A100, roughly 40-hour experiment and
APT's declared 24-GiB demand may become measured RESOURCE blockers on available hardware.
No diagnostic target first stopped at a proved paper, artifact, resource, or isolation
boundary because run-owned gates and caps intervened earlier.

## F. Remaining method limitations

Static `ARTIFACT_INSPECTION` and literature search have no route executors and remain
explicitly excluded from the applicable implemented-route denominator. Focused validation
is likewise unimplemented but is no longer advertised by discovery. These limitations are
visible; none can be counted as a discharged route.

## G. Tests

The first complete post-change run exposed one stale self-check assertion after the driver
refactor; it was corrected and the focused regressions passed 49/49. The final complete
pre-v2 suite passed **1,956 tests**, with 21 explicit Modal-only skips because that optional
client is not installed. There were zero failures.

## H. Expected route-exhaustion change

V2 should no longer classify a material route as open merely because a target cap, closed
gate, lost cached outcome, unscheduled paper check, or missing generator/verifier call
prevented work. Each implemented route should end as executed evidence, completed
inconclusive evidence, or a typed paper/artifact/identity/resource boundary. This predicts
better exhaustion accounting, not a required launch, failure, or rejection count.
