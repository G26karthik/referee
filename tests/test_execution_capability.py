"""S3 execution capability — a crash is only a failed reproduction if the code actually ran.

`reconcile` previously read any non-zero exit from a `repo_exec` spec as
FAILED_REPRODUCTION, which `overall_verdict` escalates to RED on its own with the
sentence "the paper's own code does not reproduce the number it prints". Three things
produce a non-zero exit while saying nothing whatever about the paper: an environment
this machine cannot build, dependencies that were never installed, and an argv this
harness invented that the repository does not accept. Each is a fact about the runner.

The tests below pin the seven outcomes apart. The asymmetry they encode is deliberate:
an unproven start yields INCONCLUSIVE, which accuses nobody, while a wrongly assumed
start yields RED against named authors. Those errors are not equally bad, so the burden
of proof sits on establishing that the experiment was reached.
"""
from __future__ import annotations

import sys

import pytest

from harness.artifacts import ExecCapability, ProbeSpec, RepoAcquisition
from harness.config import Config
from harness.local_exec import (StartupEvidence, classify_setup_error, reached_experiment,
                                reconcile)
from harness.repo import (accepts_argument, assess_capability, declared_platform,
                          entrypoint_imports)
from harness.stages.probe import plan_execution
from harness.stages.report import overall_verdict

CELL = "59.28"


def _spec(provenance: str = "repo_exec", capability: ExecCapability | None = None) -> ProbeSpec:
    return ProbeSpec(paper_id="p", table_ref="T1:r0:c1", claimed_cell_value=CELL,
                     provenance=provenance, capability=capability)


def _capable() -> ExecCapability:
    return ExecCapability(established=True, reason_code="established", detail="ok",
                          env_status="ready", interpreter_is_repo_env=True)


def _incapable(code: str) -> ExecCapability:
    return ExecCapability(established=False, reason_code=code, detail=f"blocked: {code}")


def _ran() -> StartupEvidence:
    """Evidence that the experiment demonstrably began."""
    return StartupEvidence(saw_contract_line=True, stdout_lines=12, ran_seconds=90.0)


# --------------------------------------------------------------------------- #
# 1-4. capability failures must never convict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", ["environment_incompatible", "dependency_missing",
                                  "invalid_invocation"])
def test_a_capability_failure_is_inconclusive_not_a_failed_reproduction(code: str):
    r = reconcile(_spec(capability=_incapable(code)), [], 0.10, [],
                  failure="exit 1: ModuleNotFoundError: No module named 'deepspeed'",
                  evidence=StartupEvidence(setup_error="modulenotfounderror"))
    assert r.status == "INCONCLUSIVE"
    assert r.failure_class == code
    assert r.reached_experiment is False


def test_a_missing_environment_names_the_environment_not_the_paper():
    r = reconcile(_spec(capability=_incapable("environment_incompatible")), [], 0.10, [],
                  failure="exit 1: ImportError", evidence=StartupEvidence())
    assert r.status == "INCONCLUSIVE"
    assert "not evidence about the paper" in r.reason


def test_a_capability_failure_cannot_drive_the_verdict():
    r = reconcile(_spec(capability=_incapable("dependency_missing")), [], 0.10, [],
                  failure="exit 1: ModuleNotFoundError", evidence=StartupEvidence())
    assert overall_verdict([], r)[0] == "GREEN", "an unbuildable environment must not turn a paper RED"


# --------------------------------------------------------------------------- #
# 5. startup failure under a capable environment
# --------------------------------------------------------------------------- #
def test_import_failure_under_a_capable_environment_is_inconclusive():
    """Capable on paper, but the process still died before the experiment."""
    r = reconcile(_spec(capability=_capable()), [], 0.10, [],
                  failure="exit 1: ImportError: cannot import name 'foo'",
                  evidence=StartupEvidence(setup_error="importerror", ran_seconds=2.0))
    assert r.status == "INCONCLUSIVE" and r.failure_class == "startup_failure"


def test_a_silent_crash_with_no_startup_evidence_is_inconclusive():
    """No output, no contract lines, no runtime — nothing establishes that it started."""
    r = reconcile(_spec(capability=_capable()), [], 0.10, [],
                  failure="exit 1: ", evidence=StartupEvidence(ran_seconds=0.4))
    assert r.status == "INCONCLUSIVE" and r.failure_class == "startup_failure"


def test_a_timeout_before_the_startup_window_is_not_a_reproduction_failure():
    r = reconcile(_spec(capability=_capable()), [], 0.10, [], failure="timeout after 1800s",
                  evidence=StartupEvidence(timed_out=True, ran_seconds=3.0))
    assert r.status == "INCONCLUSIVE" and r.failure_class == "timeout"


# --------------------------------------------------------------------------- #
# 6. genuine runtime failure after a valid start
# --------------------------------------------------------------------------- #
def test_a_crash_after_the_experiment_began_is_a_failed_reproduction():
    r = reconcile(_spec(capability=_capable()), [], 0.10, [],
                  failure="exit 1: RuntimeError: CUDA out of memory", evidence=_ran())
    assert r.status == "FAILED_REPRODUCTION"
    assert r.failure_class == "runtime_failure" and r.reached_experiment is True
    assert overall_verdict([], r)[0] == "RED", "a genuine runtime failure must still escalate"


def test_a_long_timeout_after_real_work_counts_as_reaching_the_experiment():
    r = reconcile(_spec(capability=_capable()), [], 0.10, [], failure="timeout after 1800s",
                  evidence=StartupEvidence(timed_out=True, ran_seconds=1800.0, stdout_lines=400))
    assert r.status == "FAILED_REPRODUCTION" and r.failure_class == "runtime_failure"


# --------------------------------------------------------------------------- #
# 7-8. successful execution is untouched
# --------------------------------------------------------------------------- #
def test_success_with_a_matching_result_is_still_verified():
    r = reconcile(_spec(capability=_capable()), [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "RESOLVED_VERIFIED" and r.failure_class == "none"


def test_success_with_a_mismatching_result_is_still_a_failed_reproduction():
    r = reconcile(_spec(capability=_capable()), [64.10, 64.20], 0.10, [0, 1])
    assert r.status == "FAILED_REPRODUCTION"
    assert r.failure_class == "none", "a mismatch is not a crash; it has no failure class"
    assert overall_verdict([], r)[0] == "RED"


def test_a_successful_run_needs_no_capability_record():
    """Capability gates crashes only. A run that produced numbers self-evidently ran."""
    r = reconcile(_spec(capability=None), [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "RESOLVED_VERIFIED"


# --------------------------------------------------------------------------- #
# 9. the provenance ceiling is untouched and still outranks capability
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provenance", ["synthesized", "template"])
def test_synthesized_stays_inconclusive_whatever_the_probe_did(provenance: str):
    for values, failure, ev in (([59.30, 59.26], "", None),          # would have verified
                                ([64.10, 64.20], "", None),          # would have failed
                                ([], "exit 1: RuntimeError", _ran())):  # would have convicted
        r = reconcile(_spec(provenance, capability=_capable()), values, 0.10, [0, 1],
                      failure=failure, evidence=ev)
        assert r.status == "INCONCLUSIVE"
        assert "not the paper's own code" in r.reason


def test_the_ceiling_is_checked_before_capability():
    """A synthesized probe must report the ceiling, not a capability class."""
    r = reconcile(_spec("synthesized", capability=_incapable("dependency_missing")), [], 0.10, [],
                  failure="exit 1", evidence=StartupEvidence())
    assert r.status == "INCONCLUSIVE" and r.failure_class == "none"
    assert "mechanism reimplementation" in r.reason


# --------------------------------------------------------------------------- #
# 10. the reached-experiment decision itself
# --------------------------------------------------------------------------- #
def test_contract_lines_are_sufficient_evidence():
    assert reached_experiment(StartupEvidence(saw_contract_line=True)) is True


def test_contract_lines_are_not_necessary():
    """A third-party repo owes this harness no SH_ markers; real output counts too."""
    assert reached_experiment(StartupEvidence(stdout_lines=40, ran_seconds=600.0)) is True
    assert reached_experiment(StartupEvidence(saw_json_metric=True)) is True


def test_a_setup_signature_overrides_every_other_signal():
    ev = StartupEvidence(saw_contract_line=True, saw_json_metric=True, stdout_lines=999,
                         ran_seconds=9999.0, setup_error="modulenotfounderror")
    assert reached_experiment(ev) is False


@pytest.mark.parametrize("stderr,expected", [
    ("ModuleNotFoundError: No module named 'deepspeed'", "modulenotfounderror"),
    ("ImportError: libcudart.so.11 not found", "importerror"),
    ("error: unrecognized arguments: --seed 0", "unrecognized arguments"),
    ("error: the following arguments are required: --model_name_or_path",
     "the following arguments are required"),
    ("python: can't open file 'evaluate.py'", "can't open file"),
    ("RuntimeError: CUDA out of memory", ""),
    ("AssertionError: accuracy below threshold", ""),
])
def test_setup_signatures_are_classified_from_structure_not_guessed(stderr: str, expected: str):
    assert classify_setup_error(stderr) == expected


# --------------------------------------------------------------------------- #
# 11. capability assessment, and the interpreter fallback that used to hide it
# --------------------------------------------------------------------------- #
def _acq(tmp_path, **kw) -> RepoAcquisition:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "evaluate.py").write_text("import os\nimport json\n", encoding="utf-8")
    defaults = dict(url="u", status="cached", path=str(repo), entrypoint="evaluate.py",
                    env_status="ready", env_path=sys.executable)
    return RepoAcquisition(**{**defaults, **kw})


def test_an_unbuilt_environment_is_not_capable(tmp_path):
    acq = _acq(tmp_path, env_status="blocked", env_path="")
    cap = assess_capability(acq, "", sys.executable)
    assert cap.established is False and cap.reason_code == "environment_incompatible"


def test_running_under_the_harness_interpreter_is_never_capable(tmp_path):
    """The old `acq.env_path or cfg.python` fallback, now named as the defect it is."""
    acq = _acq(tmp_path, env_status="ready", env_path=sys.executable)
    cap = assess_capability(acq, sys.executable, sys.executable)
    assert cap.established is False and cap.reason_code == "environment_incompatible"
    assert "harness's own interpreter" in cap.detail


def test_a_linux_only_environment_is_incompatible_off_linux(tmp_path):
    acq = _acq(tmp_path)
    (tmp_path / "repo" / "environment.yml").write_text(
        "name: apt\ndependencies:\n- ld_impl_linux-64=2.38\n- libgcc-ng=11.2\n", encoding="utf-8")
    cap = assess_capability(acq, "/some/env/python", sys.executable)
    if sys.platform.startswith("linux"):
        pytest.skip("this assertion is about running a linux-only env off linux")
    assert cap.established is False and cap.reason_code == "environment_incompatible"
    assert cap.declared_platform == "linux"


def test_a_missing_dependency_is_reported_as_such(tmp_path):
    acq = _acq(tmp_path, env_path=sys.executable + ".other")
    (tmp_path / "repo" / "evaluate.py").write_text(
        "import os\nimport deepspeed\nimport totally_not_installed\n", encoding="utf-8")
    cap = assess_capability(acq, sys.executable + ".other", sys.executable)
    assert cap.reason_code in ("dependency_missing", "environment_incompatible")


def test_an_entrypoint_that_does_not_take_seed_is_an_invalid_invocation(tmp_path):
    """The harness invents `--seed`; it owes itself a check that the repo accepts it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "evaluate.py").write_text("import os\nprint('hi')\n", encoding="utf-8")
    assert accepts_argument(repo, "evaluate.py") is False
    (repo / "args.py").write_text('p.add_argument("--seed", type=int)\n', encoding="utf-8")
    assert accepts_argument(repo, "evaluate.py") is True


def test_entrypoint_imports_are_parsed_never_executed(tmp_path):
    """If this imported the file, the sentinel would raise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "evaluate.py").write_text(
        "import os, sys\nfrom transformers import AutoModel\n"
        "raise SystemExit('this module must never be executed')\n", encoding="utf-8")
    assert entrypoint_imports(repo, "evaluate.py") == ["os", "sys", "transformers"]


def test_a_conditional_import_is_not_a_startup_requirement(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "evaluate.py").write_text(
        "import os\ndef f():\n    import optional_thing\n    return optional_thing\n",
        encoding="utf-8")
    assert entrypoint_imports(repo, "evaluate.py") == ["os"]


def test_declared_platform_is_blank_without_an_environment_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert declared_platform(repo) == ""


# --------------------------------------------------------------------------- #
# 12. planning refuses to promote an incapable run to repo_exec
# --------------------------------------------------------------------------- #
def test_plan_execution_will_not_promote_an_incapable_repo(tmp_path):
    cfg = Config(projects_dir=tmp_path, allow_repo_exec=True)
    acq = _acq(tmp_path, env_status="blocked", env_path="")
    spec = ProbeSpec(paper_id="p", provenance="synthesized", script="print(1)")
    out = plan_execution(cfg, spec, acq, None)
    assert out.provenance == "synthesized", "an unbuildable env must not become repo_exec"
    assert not out.command, "nothing may be scheduled to run"
    assert out.capability is not None and out.capability.established is False
    assert out.capability.reason_code == "environment_incompatible"


def test_plan_execution_never_substitutes_the_harness_interpreter(tmp_path):
    """The removed `or cfg.python` fallback: it must not reappear."""
    cfg = Config(projects_dir=tmp_path, allow_repo_exec=True, python=sys.executable)
    acq = _acq(tmp_path, env_status="ready", env_path=sys.executable)
    out = plan_execution(cfg, ProbeSpec(paper_id="p"), acq, None)
    assert out.interpreter != sys.executable or out.provenance != "repo_exec"


def test_plan_execution_is_inert_when_the_gate_is_shut(tmp_path):
    cfg = Config(projects_dir=tmp_path, allow_repo_exec=False)
    out = plan_execution(cfg, ProbeSpec(paper_id="p"), _acq(tmp_path), None)
    assert out.provenance == "template" and out.capability is None
