# Step 8 — `reimpl_exec`: a governed reconstruction, never trusted on its own word

Followed by Checkpoint C (`docs/CHECKPOINT_C.md`) — the full rerun decision 6 requires
after fallback + alignment + reconstruction all landed. No manuscript edits (decision 9).

## The defect this closes

Before this step, PATH B (a paper with no published code, but enough specification to
rebuild the experiment — `harness.reimplement.assess`) had exactly one channel:
`stages.probe.write_reimplementation_prompt` writes a brief, an implementer fills
`runs/<pid>/spec.json` by hand, and `run.py accept` seals it under the pre-existing
`driver` provenance — **the same provenance a human-authored mechanism script for a
`FOCUSED_VALIDATION_EXPERIMENT` uses**, with nothing checking that a REIMPLEMENTATION
specifically stayed inside what the paper actually specified. The brief asked nicely;
nothing verified the answer.

Decision 1: *"`reimpl_exec` provenance requires full `ReimplementationConformance` — every
required ingredient bound to both a paper locator and an implementation locator. A
conformant disagreement may establish a failure of the stated method, never phrased as
'authors' code failed.'"*

## The new provenance, and what gates it

`reimpl_exec` joins `driver`/`repo_exec` in `harness.provenance.ADMISSIBLE_REPRODUCTION_PROVENANCE`
— admissible by MEMBERSHIP, exactly like the other two — but two independent sites refuse
to let it settle anything without `ReimplementationConformance.established`:

| site | refuses on |
|---|---|
| `local_exec.reconcile` | `spec.reimplementation_conformance is None or not .established` → `INCONCLUSIVE`, `failure_class="reimplementation_nonconformant"` |
| `backends.authorize` | the same condition, as a precondition to ANY execution at all |

`identities_established` — the check that asks whether a REPO COMMAND is bound to the
cited cell — is skipped for `reimpl_exec`: there is no repository command, which is the
reason this route exists. `ReimplementationConformance` is what stands in its place, and
it is at least as strict: every required ingredient bound, not merely one command among
several.

## `ReimplementationConformance` — verified, not asserted

`harness.reimplement_driver.conformance(readiness, script, bindings)` pairs each REQUIRED
ingredient's PAPER locator (already established by `reimplement.assess`, before the driver
is ever consulted) with the delegate's IMPLEMENTATION locator — and **re-checks the
delegate's own claim**: `impl_quote` must occur verbatim in the `script` this call actually
returned. A binding the delegate asserts but that is not literally present in its own
script is recorded (so the claim is not silently dropped) but never trusted — `verified`
and `bound` are both `False`. This is the same discipline `claims.verify_evidence` applies
to a lens's citation, applied to a delegate's claim about its own code.

Pinned directly: `test_a_fully_bound_reconstruction_is_established` (every binding
verifies) against a matching case where one `impl_quote` is fabricated
(`reimplement_driver`'s own self-check, "THE REGRESSION this module exists to fix") —
`established` flips to `False` and the fabricated binding's `impl_ref` is still recorded,
naming what was claimed, while `bound`/`verified` correctly read `False`.

## Decision 10, enforced and proven directly

A governed reconstruction is code a MODEL wrote, never reviewed by a human before running
— the same third-party-code-execution risk `sufficient_for_repo_exec` exists to confine
for `repo_exec`, and `backends.authorize`'s new branch requires the identical floor
(CONTAINER or REMOTE_SESSION), behind its OWN gate (`SH_ALLOW_REIMPLEMENTATION_EXEC`,
separate from `SH_ALLOW_REPO_EXEC`, because granting one must never grant the other).

`reimplement_driver.py` itself only ever produces TEXT — zero tools, `--bare`, mirroring
`verdict_driver`'s confinement exactly — and never executes anything it writes; a source
sweep in its own self-check asserts it imports none of `backends`/`local_exec`/`stages`.
WHETHER a sealed reconstruction may ever run is decided entirely downstream, by
`backends.authorize`.

**Proven, not assumed**: `tests/test_reimplementation_conformance.py` builds a real
`ExecutionBackend` subclass (mirroring `test_execution_backend.py`'s `LinuxStubBackend`)
and sweeps every isolation level. A fully conformant reconstruction is `authorized` at
CONTAINER/REMOTE_SESSION and refused with `isolation_insufficient` at VENV — and an
end-to-end test (`test_a_sealed_conformant_reconstruction_is_picked_up_and_run`) drives a
REAL sealed reconstruction through the ACTUAL `local_exec.run_probe` path with every other
gate open, and it refuses at the isolation boundary on this host's `local` backend, exactly
as it must.

**A companion fix this surfaced**: `backends.backend_for` forced EVERY spec without
`.command` onto `local_backend()` — correct for `template`/`synthesized`/`driver`-without-
command, which really are harness- or operator-authored code meant to run here, but wrong
for `reimpl_exec`, which never carries `.command` (it runs as `.script`, like the others)
and would therefore have been structurally unable to EVER reach a sufficient isolation
level, even for an operator who deliberately configured the sandbox and accepted the risk.
Fixed: `reimpl_exec` is the one exception, routed through ordinary backend selection.
Pinned by `test_backend_for_lets_reimpl_exec_reach_a_configured_non_local_backend`.

## The pipeline seam

`stages.probe.attempt_reimplementation_fallback` is what Step 6's
`INDEPENDENT_RECONSTRUCTION` fallback now calls, for both the primary target and every
target in the `pairs[1:]` loop. It spends no execution deciding whether to try:
`reimplement.assess` is pure, `reimplement_driver.load_accepted` only reads disk, and
`reimplement_driver.run` degrades to `None` with its gate shut exactly like every other
delegation driver in this pipeline. A produced spec still goes through the ordinary
`_run` → `authorize` path, so a sealed-but-nonconformant or conformant-but-underisolated
reconstruction is refused there like any other inadmissible spec.

`_fallback_note` — which used to make a fixed, now-false claim ("no driver for that route
exists yet") — names the CURRENT reason instead: the driver gate closed, the exec gate
closed, or (once both are open) that a conformant reconstruction could not be produced or
this backend's isolation is insufficient.

## Suite and corpus

1867 passed, 0 failed, 0 skipped. Full 8-paper Checkpoint C rerun: funnel and every
disposition **unchanged** from Checkpoint B — see `docs/CHECKPOINT_C.md` for why zero
movement is the expected result of a mechanism that adds a capability nothing in this
corpus's default configuration exercises. Corpus digest unchanged:
`d5ea07b8c761bd778d865cb19ed397758d8315a396da9ecbebae89dfc000e4fb`.

## Next

Domain expansion / evaluation, per the approved order's final item. No manuscript edits —
decision 9 — until the corpus reruns this checkpoint represents were done, which they now
are.
