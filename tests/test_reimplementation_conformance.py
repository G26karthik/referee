"""Step 8 — `reimpl_exec` provenance: decision 1 (full ReimplementationConformance,
never phrased as the authors' code) and decision 10 (no third-party code execution
without CONTAINER/REMOTE_SESSION isolation, enforced and tested).

Three seams, each pinned independently, because each collapsing produces its own lie:

  ADMISSIBLE BUT NOT TRUSTED    `reimpl_exec` is in the provenance ceiling
                                 (`harness.provenance.admits`) but `local_exec.reconcile`
                                 and `backends.authorize` both additionally require
                                 `ReimplementationConformance.established` — an unbound
                                 reconstruction settles nothing even though its provenance
                                 alone would otherwise qualify.

  NEVER THE AUTHORS' CODE        `provenance_label('reimpl_exec') ==
                                 'INDEPENDENT_REIMPLEMENTATION'`, never
                                 `AUTHOR_REPOSITORY`, and `reconcile`'s own reason text
                                 says so explicitly for every verdict it draws.

  NO EXECUTION WITHOUT A BOUNDARY  `backends.authorize`'s `reimpl_exec` branch requires the
                                 SAME isolation floor `repo_exec` does
                                 (CONTAINER/REMOTE_SESSION), so a model-authored
                                 reconstruction can never run on this dev host's `local`
                                 backend (isolation VENV) regardless of how every other
                                 gate is set — proven directly, not assumed.
"""
from __future__ import annotations

import json
import sys

import pytest

from harness.artifacts import (ExecCapability, PaperDoc, ProbeSpec,
                               ReimplementationBinding, ReimplementationConformance,
                               ReimplementationReadiness, Section, Table)
from harness.backends import (BackendAvailability, BackendProfile, BackendResources,
                              ExecutionBackend, authorize)
from harness.config import Config
from harness import local_exec, provenance, reimplement, reimplement_driver
from harness.stages import probe as probe_stage

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
_CLAUSES = {
    "method": "We propose a method whose objective is defined as a sum.",
    "training": "We train with the Adam optimizer for 30 epochs at a learning rate of 1e-3.",
    "dataset": "We evaluate on the CIFAR-100 dataset.",
    "metric": "We report accuracy.",
}


def _doc(omit: str = "") -> PaperDoc:
    text = " ".join(c for k, c in _CLAUSES.items() if k != omit)
    return PaperDoc(
        paper_id="p", title="T",
        sections=[Section(section_idx=0, title="Method", text=text)],
        tables=[Table(table_idx=0, caption="Table 1", rows=[["method", "acc"], ["ours", "91.4"]])])


def _readiness(established: bool = True) -> ReimplementationReadiness:
    return reimplement.assess(_doc() if established else _doc(omit="training"))


def _script() -> str:
    return ("def train():\n    pass\n"
           "print('SH_METRIC arm=reproduction seed=0 value=0.9')\n"
           "# dataset: CIFAR-100\n# metric: accuracy\n# target: Table 1\n")


def _bound_conformance(readiness: ReimplementationReadiness | None = None) -> ReimplementationConformance:
    readiness = readiness or _readiness()
    script = _script()
    bindings = [
        {"kind": "method", "impl_ref": "line 1", "impl_quote": "def train():"},
        {"kind": "training", "impl_ref": "line 2", "impl_quote": "    pass"},
        {"kind": "dataset", "impl_ref": "line 4", "impl_quote": "# dataset: CIFAR-100"},
        {"kind": "metric", "impl_ref": "line 5", "impl_quote": "# metric: accuracy"},
        {"kind": "comparison_target", "impl_ref": "line 6", "impl_quote": "# target: Table 1"},
    ]
    return reimplement_driver.conformance(
        readiness, script, bindings,
        generated_by="generator-context", verified_by="verifier-context")


def _spec(conf: ReimplementationConformance | None, **over) -> ProbeSpec:
    # `claimed_cell_value` is deliberately a FRACTION (0.914), matching the fraction-scale
    # values the reconciliation tests below reproduce — the cited row in `_doc()`'s table
    # prints "91.4" (a percentage), which is a separate, valid fixture for a different
    # concern (units display) and not what these tests are reconciling against.
    spec = ProbeSpec(paper_id="p", provenance="reimpl_exec", script=_script(),
                     metric="accuracy", table_ref="T0:r1:c1", claimed_cell_value="0.914",
                     seeds=[0], arms=["reproduction"], reimplementation_conformance=conf)
    for k, v in over.items():
        setattr(spec, k, v)
    return spec


def test_driver_uses_distinct_generator_and_verifier_processes(tmp_path):
    helper = tmp_path / "delegate.py"
    payload = {"script": _script(), "bindings": [
        {"kind": "method", "impl_ref": "line 1", "impl_quote": "def train():"},
        {"kind": "training", "impl_ref": "line 2", "impl_quote": "    pass"},
        {"kind": "dataset", "impl_ref": "line 4", "impl_quote": "# dataset: CIFAR-100"},
        {"kind": "metric", "impl_ref": "line 5", "impl_quote": "# metric: accuracy"},
        {"kind": "comparison_target", "impl_ref": "line 6", "impl_quote": "# target: Table 1"},
    ], "notes": ""}
    helper.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "p=Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        f"g={json.dumps(payload)!r}\n"
        "v=json.dumps({'approved': True, 'approved_kinds': "
        "['method','training','dataset','metric','comparison_target'], 'notes': ''})\n"
        "Path(sys.argv[2]).write_text(v if 'independent verifier' in p else g, "
        "encoding='utf-8')\n", encoding="utf-8")
    cfg = Config(
        projects_dir=tmp_path / "projects", allow_reimplementation_driver=True,
        reimplementation_cmd=f'"{sys.executable}" "{helper}" "{{prompt}}" "{{out}}"')
    got = reimplement_driver.run(
        cfg, "generator brief", _readiness(), pid="p", target_id="T1")
    assert got is not None and got[1].established
    assert got[1].generated_by == "reimplementation_generator"
    assert got[1].verified_by == "reimplementation_verifier"
    sidecar = json.loads((cfg.projects_dir / "p" / "runs" / "p" /
                          "reimplementation" / "T1.driver.json").read_text())
    assert sidecar["generator"]["raw_sha256"] != sidecar["verifier"]["raw_sha256"]


class _StubBackend(ExecutionBackend):
    """A real `ExecutionBackend` subclass, not a bare dataclass — the same discipline
    `test_alignment.py`'s `_StubBackend` and `test_execution_backend.py`'s
    `LinuxStubBackend` already apply, so this exercises the actual profile/authorize seam
    rather than a shape that merely looks like one."""

    name = "stub"

    def __init__(self, cfg=None, *, isolation: str = "VENV", can_execute: bool = True,
                usable: bool = True):
        super().__init__(cfg)
        self.isolation = isolation
        self._can_execute = can_execute
        self._usable = usable

    def resources(self) -> BackendResources:
        return BackendResources(name=self.name, platform="linux", python="/usr/bin/python3")

    def profile(self) -> BackendProfile:
        # Built directly rather than via the base implementation: `BackendProfile` is a
        # frozen dataclass, so the base's measured-default profile cannot be mutated
        # afterward — a stub has to declare what it offers up front, the same discipline
        # a real remote backend's own `profile()` override already follows.
        return BackendProfile(name=self.name, platform="linux",
                              can_execute=self._can_execute, isolation=self.isolation)

    def available(self) -> BackendAvailability:
        return BackendAvailability(usable=self._usable, detail="stub")

    def capability(self, acq, interpreter, harness_python, flag="seed") -> ExecCapability:
        raise AssertionError("capability is not assessed for reimpl_exec")

    def provision(self, cfg, root, pid, acq):
        return acq

    def execute(self, req):
        raise AssertionError("the stub backend must never actually run anything in this suite")

    def cleanup(self, root, pid):
        return []


class _RemoteStubBackend(_StubBackend):
    """A registerable CLASS (not a configured instance) presenting REMOTE_SESSION
    isolation, for the one test that needs `backends.register_backend` — which takes a
    type, not a factory closure."""

    def __init__(self, cfg=None):
        super().__init__(cfg, isolation="REMOTE_SESSION")


# --------------------------------------------------------------------------- #
# A. conformance() — the harness verifies, it does not trust
# --------------------------------------------------------------------------- #
def test_a_fully_bound_reconstruction_is_established():
    conf = _bound_conformance()
    assert conf.established
    assert conf.unbound == []
    assert all(b.bound and b.verified for b in conf.bindings)


def test_generated_code_cannot_self_certify_conformance():
    readiness = _readiness()
    script = _script()
    bindings = [b.model_dump() for b in _bound_conformance(readiness).bindings]
    unreviewed = reimplement_driver.conformance(
        readiness, script, bindings, generated_by="same", verified_by="same")
    assert not unreviewed.established
    assert not unreviewed.independently_verified
    assert "cannot certify its own" in unreviewed.reason


def test_an_impl_quote_at_the_wrong_locator_is_not_verified():
    readiness = _readiness()
    script = _script()
    bindings = [b.model_dump() for b in _bound_conformance(readiness).bindings]
    bindings[0]["impl_ref"] = "line 6"  # method quote exists, but not at line 6
    conf = reimplement_driver.conformance(
        readiness, script, bindings,
        generated_by="generator-context", verified_by="verifier-context")
    assert not conf.established
    assert "method" in conf.unbound


def test_provenance_admits_reimpl_exec_but_only_conformance_lets_it_settle_anything():
    assert provenance.admits("reimpl_exec")
    assert provenance.label("reimpl_exec") == "INDEPENDENT_REIMPLEMENTATION"
    assert provenance.label("reimpl_exec") != "AUTHOR_REPOSITORY"


# --------------------------------------------------------------------------- #
# B. local_exec.reconcile — the conformance precondition
# --------------------------------------------------------------------------- #
def test_a_nonconformant_reconstruction_settles_nothing():
    nonconformant = ReimplementationConformance(
        established=False, unbound=["training"],
        bindings=[ReimplementationBinding(kind="training", paper_ref="s0",
                                          paper_quote="q", bound=False)],
        reason="not bound: training")
    spec = _spec(nonconformant)
    rec = local_exec.reconcile(spec, [0.91], 0.02, [0])
    assert rec.status == "INCONCLUSIVE"
    assert rec.failure_class == "reimplementation_nonconformant"
    assert "not fully conformant" in rec.reason


def test_a_spec_with_no_conformance_object_at_all_settles_nothing():
    spec = _spec(None)
    rec = local_exec.reconcile(spec, [0.91], 0.02, [0])
    assert rec.status == "INCONCLUSIVE"
    assert rec.failure_class == "reimplementation_nonconformant"
    assert "no ReimplementationConformance was recorded" in rec.reason


def test_a_conformant_reconstruction_reconciles_and_never_blames_the_authors():
    conf = _bound_conformance()
    spec = _spec(conf)
    # within the noise band -> RESOLVED_VERIFIED
    rec = local_exec.reconcile(spec, [0.914, 0.913], 0.02, [0, 1])
    assert rec.status == "RESOLVED_VERIFIED"
    assert "governed reconstruction" in rec.reason
    assert "never a statement that the authors' code failed" in rec.reason

    # outside the noise band -> FAILED_REPRODUCTION, still never blaming the authors
    rec2 = local_exec.reconcile(spec, [0.5, 0.51], 0.02, [0, 1])
    assert rec2.status == "FAILED_REPRODUCTION"
    assert "stated method reproduces" in rec2.reason
    assert "never a statement that the authors' code failed" in rec2.reason


def test_identities_established_is_skipped_for_reimpl_exec():
    """There is no repository command to bind for a from-scratch reconstruction — the
    conformance check upstream is what stands in identity's place. A spec with
    `experiment=None` (the legacy 'unmapped' default) must not be refused for that reason
    once conformance already established the reconstruction is bound."""
    conf = _bound_conformance()
    spec = _spec(conf)
    assert spec.experiment is None       # never assessed, deliberately
    rec = local_exec.reconcile(spec, [0.914], 0.02, [0])
    assert rec.status == "RESOLVED_VERIFIED"


# --------------------------------------------------------------------------- #
# C. backends.authorize — decision 10's boundary, proven directly
# --------------------------------------------------------------------------- #
def test_gate_closed_refuses_before_anything_else():
    spec = _spec(_bound_conformance())
    a = authorize(Config(allow_reimplementation_exec=False), spec, _StubBackend(isolation="CONTAINER"))
    assert not a.allowed and a.decision == "gate_closed"


def test_insufficient_isolation_refuses_even_with_every_other_gate_open():
    """THE decision-10 test. Gate open, fully conformant, backend can execute — and it is
    STILL refused, because the backend's isolation is a VENV, not a container or a leased
    remote session. This is what makes decision 10 an enforced boundary rather than a
    comment: on THIS dev host (`local`, isolation VENV, see harness.isolation), a
    `reimpl_exec` spec can never execute, full stop."""
    spec = _spec(_bound_conformance())
    cfg = Config(allow_reimplementation_exec=True)
    a = authorize(cfg, spec, _StubBackend(isolation="VENV"))
    assert not a.allowed and a.decision == "isolation_insufficient"
    assert "CONTAINER or REMOTE_SESSION" in a.detail


@pytest.mark.parametrize("isolation", ["CONTAINER", "REMOTE_SESSION"])
def test_a_nonconformant_reconstruction_is_refused_even_with_a_sufficient_backend(isolation):
    spec = _spec(None)
    cfg = Config(allow_reimplementation_exec=True)
    a = authorize(cfg, spec, _StubBackend(isolation=isolation))
    assert not a.allowed and a.decision == "conformance_unproven"


@pytest.mark.parametrize("isolation", ["CONTAINER", "REMOTE_SESSION"])
def test_a_conformant_reconstruction_on_a_sufficient_backend_is_authorized(isolation):
    spec = _spec(_bound_conformance())
    cfg = Config(allow_reimplementation_exec=True)
    a = authorize(cfg, spec, _StubBackend(isolation=isolation))
    assert a.allowed and a.decision == "authorized"


def test_no_backend_refuses_rather_than_substituting_local():
    spec = _spec(_bound_conformance())
    a = authorize(Config(allow_reimplementation_exec=True), spec, None)
    assert not a.allowed and a.decision == "no_backend"


def test_a_declared_backend_that_cannot_execute_is_refused():
    spec = _spec(_bound_conformance())
    a = authorize(Config(allow_reimplementation_exec=True), spec,
                 _StubBackend(isolation="REMOTE_SESSION", can_execute=False))
    assert not a.allowed and a.decision == "backend_cannot_execute"


def test_backend_for_lets_reimpl_exec_reach_a_configured_non_local_backend():
    """The companion fix: `backend_for` used to force EVERY spec with no `.command` onto
    `local_backend()`, which — since a `reimpl_exec` spec always uses `.script`, never
    `.command` — would have made it structurally impossible to ever reach a sufficient
    isolation level, even for an operator who deliberately configured the sandbox."""
    from harness.backends import backend_for, register_backend, _REGISTRY

    register_backend("stub-reimpl", _RemoteStubBackend)
    try:
        cfg = Config(exec_backend="stub-reimpl")
        spec = _spec(_bound_conformance())
        b = backend_for(cfg, spec)
        assert b is not None and b.name == "stub"
    finally:
        _REGISTRY.pop("stub-reimpl", None)

    # every OTHER harness-authored provenance is unaffected: still forced local
    from harness.backends import local_backend
    template_spec = ProbeSpec(paper_id="p")
    forced = backend_for(Config(exec_backend="stub-reimpl"), template_spec)
    assert forced is not None and forced.name == local_backend().name


# --------------------------------------------------------------------------- #
# D. stages.probe.attempt_reimplementation_fallback — the pipeline seam
# --------------------------------------------------------------------------- #
def _plan(requires_execution=True, route="INDEPENDENT_RECONSTRUCTION"):
    from harness.artifacts import PlanDecision
    return PlanDecision(target_id="T1", action=route, route=route,
                        requires_execution=requires_execution, attempt=2)


def test_no_fallback_plan_yields_none(tmp_path):
    assert probe_stage.attempt_reimplementation_fallback(
        Config(), tmp_path, "p", _doc(), ProbeSpec(paper_id="p"), None) is None


def test_a_refused_fallback_plan_yields_none(tmp_path):
    assert probe_stage.attempt_reimplementation_fallback(
        Config(), tmp_path, "p", _doc(), ProbeSpec(paper_id="p"),
        _plan(requires_execution=False)) is None


def test_an_underspecified_paper_yields_none_without_spending_a_call(tmp_path):
    """`reimplement.assess` refuses (the fixture omits `training`), so the driver's own
    `available()` gate — and the delegate — are never even consulted."""
    under = ProbeSpec(paper_id="p")
    result = probe_stage.attempt_reimplementation_fallback(
        Config(allow_reimplementation_driver=True), tmp_path, "p",
        _doc(omit="training"), under, _plan())
    assert result is None


def test_no_driver_available_and_nothing_sealed_yields_none(tmp_path):
    cfg = Config(projects_dir=tmp_path, allow_reimplementation_driver=False)
    result = probe_stage.attempt_reimplementation_fallback(
        cfg, tmp_path, "p", _doc(), ProbeSpec(paper_id="p", target_id="T1"), _plan())
    assert result is None


def test_a_sealed_conformant_reconstruction_is_picked_up_and_run(tmp_path):
    """End to end, using the REAL `_run` path: a reconstruction sealed ahead of time (as
    `run.py accept`'s manual channel would leave it) is found, turned into a ProbeSpec,
    and actually executed through `local_exec.run_probe` — which, on this host's `local`
    backend (isolation VENV), refuses at the isolation boundary. That refusal, not a
    fabricated success, is the correct end-to-end behaviour: decision 10 holds even when
    every other condition — eligibility, a sealed reconstruction, full conformance — is
    satisfied.
    """
    cfg = Config(projects_dir=tmp_path, allow_reimplementation_exec=True)
    readiness = _readiness()
    conf = _bound_conformance(readiness)
    reimplement_driver.accept_reimplementation(
        cfg, "p", "T1", __import__("json").dumps({
            "script": _script(),
            "bindings": [b.model_dump() for b in conf.bindings],
        }), readiness, reviewer="verifier-test", generated_by="generator-test")

    base_spec = ProbeSpec(paper_id="p", target_id="T1", table_ref="T0:r1:c1",
                          claimed_cell_value="91.4", claim="the method reaches 0.91")
    result = probe_stage.attempt_reimplementation_fallback(
        cfg, tmp_path / "p", "p", _doc(), base_spec, _plan())
    assert result is not None
    assert result.provenance == "reimpl_exec"
    assert result.verdict == "blocked"
    assert result.authorization is not None
    assert result.authorization.decision == "isolation_insufficient"
    assert result.executions == 0
    # the spec this attempt built is persisted, so it is auditable after the fact
    assert (tmp_path / "p" / "runs" / "p" / "reimplementation" / "T1.spec.json").exists()


def test_fallback_note_names_the_actual_reason_once_step_8_exists():
    """`_fallback_note` used to make a fixed, now-false claim ('no driver for that route
    exists yet'). It has to say something true about THIS run's actual gates instead."""
    note_closed_driver = probe_stage._fallback_note(
        Config(allow_reimplementation_driver=False), _plan())
    assert "no reviewer is configured" in note_closed_driver

    note_closed_exec = probe_stage._fallback_note(
        Config(allow_reimplementation_driver=True, reimplementation_cmd="x {prompt} {out}",
              allow_reimplementation_exec=False), _plan())
    assert "SH_ALLOW_REIMPLEMENTATION_EXEC" in note_closed_exec
