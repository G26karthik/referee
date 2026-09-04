"""The execution backend seam, and the one invariant it exists to hold.

Three layers were already in place to stop an unearned reproduction verdict: the
provenance ceiling (only the authors' code may reconcile a printed cell), the identity
chain (the program that ran must be the one that produces that cell), and the capability
check (this machine must be able to give it a fair run). Each was enforced somewhere
along the path that BUILDS a spec.

None of them was enforced at the point that RUNS one. `stages/probe.plan_execution`
applies them all, but a hand-written `runs/<pid>/spec.json` never passes through
`plan_execution` — `build_spec` loads it verbatim. A spec.json carrying `command` and
`"provenance": "repo_exec"` therefore executed a third-party command with the repo-exec
gate shut and no identity established, and its crash was eligible to reconcile as
FAILED_REPRODUCTION, which drives RED on its own. That is the gap these tests close.

The rest pins the interface itself: selection refuses an unknown name rather than
substituting a host, capability is judged against the platform the RUN will see rather
than the one this process is on, and the four endings a process can have stay
distinguishable. The platform test matters most for what comes next — a Linux-only
repository is incompatible under the local Windows backend and capable under a backend
that reports 'linux', with no change to the capability logic. That is the whole claim
being made about adding WSL/container execution later, so it is tested now against a
stub rather than asserted in a comment.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness.artifacts import (CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExperimentIdentity, MetricIdentity, ProbeSpec,
                               RepoAcquisition, ResourceCapability)
from harness.backends import (BackendAvailability, BackendResources, ExecOutcome, ExecRequest,
                              ExecutionBackend, LocalBackend, UnknownBackend, authorize,
                              backend_for, local_backend, register_backend,
                              registered_backends, select_backend)
from harness.config import Config
from harness.local_exec import reconcile, run_probe
from harness.repo import assess_capability
from harness.stages.probe import plan_execution
from harness.stages.report import overall_verdict


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _cfg(**over) -> Config:
    cfg = Config.load()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _established(cls):
    return cls(state="established", established=True, reason="test fixture")


def _identified(spec: ProbeSpec) -> ProbeSpec:
    spec.experiment = _established(ExperimentIdentity)
    spec.metric_identity = _established(MetricIdentity)
    spec.configuration = _established(ConfigurationIdentity)
    return spec


def _capable() -> ExecCapability:
    return ExecCapability(established=True, reason_code="established",
                          detail="fixture", interpreter_is_repo_env=True)


def _repo_spec(**over) -> ProbeSpec:
    spec = ProbeSpec(paper_id="p", command=["python", "eval.py"], provenance="repo_exec",
                     table_ref="T1:r0:c1", claimed_cell_value="59.28")
    for k, v in over.items():
        setattr(spec, k, v)
    return spec


def _qualified() -> ProbeSpec:
    """Every precondition satisfied EXCEPT the commit, which is not a spec field.

    Commit verification is passed to `authorize` as an argument rather than read off the
    spec, precisely so a stale planning-time answer cannot stand in for a fresh one — so
    the fixture for it is `_verified()` below, supplied per call.
    """
    spec = _identified(_repo_spec())
    spec.capability = _capable()
    spec.resources = ResourceCapability(state="satisfied", reason="fixture: the experiment fits")
    return spec


def _verified() -> CommitVerification:
    return CommitVerification(state="verified", expected="a" * 40, actual="a" * 40,
                              reason="fixture: HEAD is the audited commit")


class LinuxStubBackend(ExecutionBackend):
    """A backend that presents Linux without providing one.

    It cannot execute anything — `execute` refuses — which is the point: it exercises the
    platform seam without this test suite ever gaining a way to run third-party code.
    """

    name = "linux-stub"

    def resources(self) -> BackendResources:
        return BackendResources(name=self.name, platform="linux", python="/usr/bin/python3")

    def capability(self, acq, interpreter, harness_python, flag="seed") -> ExecCapability:
        cap = assess_capability(acq, interpreter, harness_python, flag, platform=self.platform)
        cap.backend = self.name
        return cap

    def provision(self, cfg, root, pid, acq):
        acq.env_status = "blocked"
        return acq

    def execute(self, req: ExecRequest) -> ExecOutcome:
        raise AssertionError("the stub backend must never actually run anything")

    def cleanup(self, root, pid) -> list[str]:
        return []


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
def test_the_default_backend_is_the_local_machine():
    backend = select_backend(_cfg())
    assert isinstance(backend, LocalBackend) and backend.name == "local"


def test_an_unknown_backend_name_is_refused_rather_than_substituted():
    """A typo must not silently run a Linux repository on this Windows host.

    Falling back to local would be the same class of substitution the interpreter
    fallback was removed for: an environment nobody chose, producing a crash that looks
    like the repository's fault.
    """
    with pytest.raises(UnknownBackend) as e:
        select_backend(_cfg(exec_backend="dokcer"))
    assert "dokcer" in str(e.value) and "local" in str(e.value)


def test_a_registered_backend_becomes_selectable_without_touching_the_caller():
    register_backend("linux-stub", LinuxStubBackend)
    try:
        assert "linux-stub" in registered_backends()
        assert isinstance(select_backend(_cfg(exec_backend="linux-stub")), LinuxStubBackend)
    finally:
        from harness import backends as b
        b._REGISTRY.pop("linux-stub", None)


def test_harness_authored_code_always_runs_locally():
    """A generated probe measures THIS machine. Running it elsewhere answers a different
    question, so backend selection does not apply to it — even a broken selection."""
    own = ProbeSpec(paper_id="p")
    assert backend_for(_cfg(exec_backend="nonsense"), own).name == "local"
    assert local_backend().name == "local"


def test_an_unresolvable_backend_yields_no_backend_for_repository_code():
    assert backend_for(_cfg(exec_backend="nonsense"), _repo_spec()) is None


def test_the_local_backend_reports_this_machine():
    res = LocalBackend().resources()
    assert res.platform == sys.platform and res.python and res.name == "local"
    assert LocalBackend().available().usable


# --------------------------------------------------------------------------- #
# Capability gating — judged against the backend's platform, not this process's
# --------------------------------------------------------------------------- #
def _linux_repo(tmp_path: Path) -> RepoAcquisition:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "environment.yml").write_text(
        "name: x\nchannels: [defaults]\ndependencies:\n  - libgcc-ng=11\n  - linux-64\n",
        encoding="utf-8")
    (repo / "main.py").write_text("import os\n", encoding="utf-8")
    return RepoAcquisition(status="cloned", path=str(repo), entrypoint="main.py",
                           env_status="ready", env_path=str(repo / "env" / "python"))


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="needs a non-Linux host")
def test_a_linux_only_repository_is_incompatible_under_the_local_backend(tmp_path):
    cap = LocalBackend().capability(_linux_repo(tmp_path), "x", "y")
    assert not cap.established and cap.reason_code == "environment_incompatible"
    assert cap.declared_platform == "linux" and cap.current_platform == sys.platform
    assert cap.backend == "local"


def test_the_same_repository_clears_the_platform_check_on_a_linux_backend(tmp_path):
    """The seam, demonstrated. Nothing in `assess_capability` changed; only the platform
    it was told the run would see. This is the whole mechanism by which a container
    backend added later makes Linux repositories runnable."""
    cap = LinuxStubBackend().capability(_linux_repo(tmp_path), "x", "y")
    assert cap.current_platform == "linux"
    # It fails LATER, on the interpreter check — not on the platform, which is the point.
    assert cap.reason_code != "environment_incompatible" or "platform" not in cap.detail
    assert cap.backend == "linux-stub"


def test_capability_still_defaults_to_this_process_when_no_platform_is_given(tmp_path):
    cap = assess_capability(_linux_repo(tmp_path), "x", "y")
    assert cap.current_platform == sys.platform


def test_an_unknown_backend_leaves_the_spec_unpromoted(tmp_path):
    """`plan_execution` must not raise into the middle of a review, and must not promote."""
    acq = _linux_repo(tmp_path)
    spec = plan_execution(_cfg(allow_repo_exec=True, exec_backend="nonsense"),
                          ProbeSpec(paper_id="p"), acq, None)
    assert spec.provenance != "repo_exec" and not spec.command


# --------------------------------------------------------------------------- #
# Authorization — the only thing that may say yes
# --------------------------------------------------------------------------- #
def test_our_own_probe_is_not_repository_execution():
    auth = authorize(_cfg(), ProbeSpec(paper_id="p"), local_backend())
    assert auth.allowed and auth.decision == "not_repo_execution"


def test_a_closed_gate_refuses_before_anything_else_is_considered():
    auth = authorize(_cfg(allow_repo_exec=False), _qualified(), local_backend(), commit=_verified())
    assert not auth.allowed and auth.decision == "gate_closed"
    assert auth.failure_class == "execution_unauthorized"


def test_a_command_that_is_not_the_authors_code_may_not_run_as_theirs():
    spec = _qualified()
    spec.provenance = "synthesized"
    auth = authorize(_cfg(allow_repo_exec=True), spec, local_backend(), commit=_verified())
    assert not auth.allowed and auth.decision == "provenance_insufficient"


def test_unproven_identity_blocks_a_fully_capable_run():
    spec = _repo_spec()
    spec.capability = _capable()
    auth = authorize(_cfg(allow_repo_exec=True), spec, local_backend(), commit=_verified())
    assert not auth.allowed and auth.decision == "identity_unproven"
    assert auth.failure_class == "experiment_unidentified"


@pytest.mark.parametrize("missing,expected", [
    ("experiment", "experiment_unidentified"),
    ("metric_identity", "metric_unbound"),
    ("configuration", "configuration_unmatched"),
])
def test_every_link_of_the_identity_chain_is_independently_required(missing, expected):
    """Metric binding in particular is not optional. A run that succeeds while the metric
    is unbound has measured something — just not the quantity the cell reports."""
    spec = _qualified()
    setattr(spec, missing, None)
    auth = authorize(_cfg(allow_repo_exec=True), spec, local_backend(), commit=_verified())
    assert not auth.allowed and auth.failure_class == expected


def test_an_incapable_environment_blocks_an_otherwise_identified_run():
    spec = _identified(_repo_spec())
    spec.capability = ExecCapability(established=False, reason_code="dependency_missing",
                                     detail="torch is not importable")
    auth = authorize(_cfg(allow_repo_exec=True), spec, local_backend(), commit=_verified())
    assert not auth.allowed and auth.decision == "capability_unproven"
    assert "torch" in auth.detail


def test_capability_that_was_never_assessed_is_not_capability():
    spec = _identified(_repo_spec())
    spec.capability = None
    auth = authorize(_cfg(allow_repo_exec=True), spec, local_backend(), commit=_verified())
    assert not auth.allowed and auth.decision == "capability_unproven"


def test_no_backend_means_no_execution():
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), None, commit=_verified())
    assert not auth.allowed and auth.decision == "no_backend"


def test_an_unavailable_backend_refuses_but_is_not_an_absent_one():
    """A runner that is down and no runner at all are both refusals, and not the same one.

    This test previously asserted they were identical, which is what the collapse looked
    like from inside: both reported `no_backend`. An operator reading that goes looking
    for a backend to add, when the registry already holds one that would have run the
    experiment and was merely unreachable — the only refusal in this module that a later
    attempt can resolve without anything about the paper changing.
    """
    class Down(LinuxStubBackend):
        def available(self):
            return BackendAvailability(False, "the daemon is not running")

    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), Down(), commit=_verified())
    assert not auth.allowed and auth.decision == "backend_offline"
    assert "daemon" in auth.detail
    # Both are INCONCLUSIVE-shaped, and neither convicts the paper.
    absent = authorize(_cfg(allow_repo_exec=True), _qualified(), None, commit=_verified())
    assert auth.failure_class == absent.failure_class == "backend_unavailable"
    assert auth.decision != absent.decision


def test_every_condition_together_is_what_authorizes():
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), local_backend(), commit=_verified())
    assert auth.allowed and auth.decision == "authorized" and auth.failure_class == "none"
    assert auth.backend == "local"


# --------------------------------------------------------------------------- #
# The invariant: an unqualified execution can reach NEITHER verdict
# --------------------------------------------------------------------------- #
_UNAUTHORIZED = ("gate_closed", "no_backend", "backend_cannot_execute",
                 "resources_unproven", "commit_unverified", "provenance_insufficient",
                 "identity_unproven", "capability_unproven")


@pytest.mark.parametrize("values", [
    [59.30, 59.26],        # would RESOLVED_VERIFIED under a qualified run
    [64.10, 64.20],        # would FAILED_REPRODUCTION under a qualified run
])
def test_a_refusal_produces_neither_verdict_however_the_numbers_fall(values):
    """Both directions. Acquitting a cell without authority is as wrong as convicting
    one: a number that arrived outside the gates has no standing to settle either."""
    auth = authorize(_cfg(allow_repo_exec=False), _qualified(), local_backend(), commit=_verified())
    rec = reconcile(_qualified(), values, 0.10, [0, 1], authorization=auth)
    assert rec.status == "INCONCLUSIVE", rec.reason
    assert rec.failure_class == "execution_unauthorized"
    assert rec.reached_experiment is False


def test_no_unauthorized_decision_can_convict_or_acquit():
    """Exhaustive over the refusal decisions rather than a sample, so a new one added
    later cannot quietly default to a verdict."""
    from harness.artifacts import ExecAuthorization
    for decision in _UNAUTHORIZED:
        auth = ExecAuthorization(allowed=False, decision=decision, backend="local",
                                 failure_class="execution_unauthorized", detail="refused")
        for values in ([59.28, 59.28], [999.0, 999.0], []):
            rec = reconcile(_qualified(), values, 0.10, [0, 1], authorization=auth)
            assert rec.status == "INCONCLUSIVE", f"{decision} / {values}: {rec.reason}"


def test_an_authorized_run_still_reconciles_normally():
    """The gate adds a precondition; it does not disable the arithmetic behind it."""
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), local_backend(), commit=_verified())
    assert reconcile(_qualified(), [59.30, 59.26], 0.10, [0, 1],
                     authorization=auth).status == "RESOLVED_VERIFIED"
    assert reconcile(_qualified(), [64.10, 64.20], 0.10, [0, 1],
                     authorization=auth).status == "FAILED_REPRODUCTION"


def test_a_refusal_cannot_drive_the_paper_red():
    auth = authorize(_cfg(allow_repo_exec=False), _qualified(), local_backend(), commit=_verified())
    rec = reconcile(_qualified(), [64.10, 64.20], 0.10, [0, 1], authorization=auth)
    verdict, _ = overall_verdict([], rec)
    assert verdict == "GREEN", "declining to run someone's code says nothing about it"


# --------------------------------------------------------------------------- #
# run_probe refuses at the point of execution, not only at planning
# --------------------------------------------------------------------------- #
def test_a_handwritten_spec_cannot_run_a_repository_past_a_closed_gate(tmp_path):
    """The hole this layer closes. `build_spec` loads `runs/<pid>/spec.json` verbatim and
    `plan_execution` returns early when the gate is shut, so a spec.json that already
    carried `command` and `repo_exec` reached `run_probe` intact and executed."""
    spec = _qualified()
    spec.command = [sys.executable, "-c", "print('SH_METRIC arm=reproduction seed=0 value=64.1')"]
    result = run_probe(_cfg(allow_repo_exec=False), tmp_path, spec)

    assert result.verdict == "blocked"
    assert result.authorization is not None and result.authorization.decision == "gate_closed"
    assert result.seeds_run == [] and not result.arms, "nothing may have been measured"
    assert result.reconciliation is not None
    assert result.reconciliation.status == "INCONCLUSIVE"
    assert not (tmp_path / "runs" / "p" / "probe.py").exists(), "no probe was written"
    assert result.script_path == "", "naming a file that was never written misleads a reader"


def test_a_blocked_run_is_not_reported_as_a_failed_one(tmp_path):
    """'failed' means the code ran and produced nothing usable. Reusing it here would
    read, next to a paper, as though their code had been tried and found wanting."""
    spec = _qualified()
    spec.command = [sys.executable, "-c", "raise SystemExit(1)"]
    result = run_probe(_cfg(allow_repo_exec=False), tmp_path, spec)
    assert result.verdict == "blocked" != "failed"
    assert "not authorized" in result.reason


def test_the_blocked_result_is_still_written_to_disk(tmp_path):
    spec = _qualified()
    spec.command = ["python", "eval.py"]
    run_probe(_cfg(allow_repo_exec=False), tmp_path, spec)
    assert (tmp_path / "runs" / "p" / "probe_results.json").is_file()


def test_the_local_template_is_unaffected_by_any_of_this(tmp_path):
    """The calibration path must not have acquired a new way to fail."""
    spec = ProbeSpec(paper_id="p", seeds=[0, 1, 2], epochs=4)
    result = run_probe(_cfg(), tmp_path, spec)
    assert result.verdict == "calibration", result.reason
    assert result.authorization is not None
    assert result.authorization.decision == "not_repo_execution"
    assert result.backend == "local"


# --------------------------------------------------------------------------- #
# Execution and output collection
# --------------------------------------------------------------------------- #
def test_stdout_is_collected_verbatim():
    out = LocalBackend().execute(ExecRequest([sys.executable, "-c", "print('hello')"], "", 60))
    assert out.launched and out.completed and out.ok and "hello" in out.stdout


def test_a_non_zero_exit_is_an_outcome_not_an_exception():
    out = LocalBackend().execute(ExecRequest([sys.executable, "-c", "raise SystemExit(7)"], "", 60))
    assert out.launched and out.completed and out.returncode == 7 and not out.ok


def test_a_command_that_never_starts_is_distinguishable_from_one_that_failed():
    out = LocalBackend().execute(ExecRequest(["no-such-binary-anywhere-xyz"], "", 60))
    assert not out.launched and not out.completed and "could not start" in out.error


def test_a_timeout_is_a_launch_not_a_failure_to_launch():
    """The distinction reconciliation depends on: a process killed after doing work is
    evidence the experiment was reached; one that never started is the opposite."""
    out = LocalBackend().execute(
        ExecRequest([sys.executable, "-c", "import time; time.sleep(10)"], "", 1))
    assert out.launched and not out.completed and out.timed_out


def test_the_environment_is_passed_through():
    out = LocalBackend().execute(ExecRequest(
        [sys.executable, "-c", "import os; print(os.environ['SH_TEST_MARKER'])"], "", 60,
        env={"SH_TEST_MARKER": "present"}))
    assert "present" in out.stdout


def test_a_backend_may_not_edit_the_command_it_was_handed():
    req = ExecRequest(["python"], "", 60)
    with pytest.raises(Exception):
        req.argv = ["rm"]                                   # frozen dataclass


# --------------------------------------------------------------------------- #
# Provisioning and cleanup
# --------------------------------------------------------------------------- #
def test_provisioning_respects_the_install_gate(tmp_path):
    acq = RepoAcquisition(status="cloned", path=str(tmp_path / "repo"))
    out = LocalBackend().provision(_cfg(allow_install=False), tmp_path, "p", acq)
    assert out.env_status == "blocked"
    assert not (tmp_path / "runs" / "p" / "env").exists(), "nothing may be built"


def test_provisioning_does_nothing_without_a_checkout(tmp_path):
    acq = RepoAcquisition(status="unavailable")
    out = LocalBackend().provision(_cfg(allow_install=True), tmp_path, "p", acq)
    assert out.env_status == "not_attempted"


def test_cleanup_removes_only_what_the_backend_built(tmp_path):
    env_dir = tmp_path / "runs" / "p" / "env"
    env_dir.mkdir(parents=True)
    evidence = tmp_path / "runs" / "p" / "probe_results.json"
    evidence.write_text("{}", encoding="utf-8")
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir()

    removed = LocalBackend().cleanup(tmp_path, "p")
    assert removed == [str(env_dir)] and not env_dir.exists()
    assert evidence.is_file() and checkout.is_dir(), "the evidence a reader checks stays"


def test_cleanup_is_a_no_op_when_nothing_was_provisioned(tmp_path):
    assert LocalBackend().cleanup(tmp_path, "p") == []


# --------------------------------------------------------------------------- #
# The provider-neutral contract
# --------------------------------------------------------------------------- #
# What a future Kaggle, Colab, container or VM backend must satisfy, asserted against
# every backend the registry holds rather than against the one that happens to run here.
# The point is that a new provider is a class and a profile, and nothing above this seam
# changes: no test below names `local`, and none of them executes third-party code.
def test_every_registered_backend_satisfies_the_interface():
    from harness import backends as b

    for name in registered_backends():
        backend = b._REGISTRY[name]()
        assert isinstance(backend, ExecutionBackend), name
        assert backend.name == name, "a backend's registered key is its own name"
        r, p = backend.resources(), backend.profile()
        assert r.name and r.platform, f"{name} must say what platform it presents"
        assert p.name == name and isinstance(p.can_execute, bool)
        assert backend.environment(), f"{name} must be able to say where a run happens"
        assert isinstance(backend.available(), BackendAvailability)
        for op in ("capability", "provision", "execute", "cleanup"):
            assert callable(getattr(backend, op)), f"{name}.{op}"


def test_can_execute_and_availability_are_separate_questions():
    """Permanent incapacity and a transient outage are different facts.

    A declaration will never run anything; a real runner whose provider is down will run
    something later. Both make `available()` false, so `can_execute` is what tells them
    apart — and it is `can_execute` that `authorize` consults first.
    """
    class Down(LocalBackend):
        name = "down"

        def available(self):
            return BackendAvailability(False, "the provider API returned 503")

    down = Down()
    assert down.profile().can_execute is True and not down.available().usable
    from harness.backends import KaggleBackend
    declared = KaggleBackend()
    assert declared.profile().can_execute is False and not declared.available().usable


def test_an_execution_record_says_where_it_ran():
    """`backend: "kaggle"` names a provider, not a machine.

    On this host the hardware is implicit — there is only one. A remote run's is not, and
    a reproduction verdict from hardware nobody can identify afterwards is not evidence.
    So the backend stamps its own account of the environment onto every outcome, and
    `run_probe` copies it verbatim onto the record.
    """
    backend = local_backend()
    out = backend.execute(ExecRequest([_cfg().python, "-c", "print('x')"], "", 60))
    assert out.environment == backend.environment()
    assert out.backend in out.environment, "the environment names the backend that ran it"
    assert sys.platform in out.environment


def test_the_environment_stamp_survives_into_the_execution_log(tmp_path):
    spec = ProbeSpec(paper_id="p", seeds=[0], arms=["a"], provenance="template",
                     script="print('SH_METRIC arm=a seed=0 value=1.0')")
    result = run_probe(_cfg(), tmp_path, spec)
    assert result.executions == 1 and result.execution_log
    import json

    rec = json.loads(Path(result.execution_log).read_text(encoding="utf-8").splitlines()[0])
    assert rec["environment"] and rec["backend"] == "local"
    assert rec["environment"] == local_backend().environment()
