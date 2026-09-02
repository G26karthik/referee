"""S3 as a code-audit and reproduction engine: acquire, inspect, run, reconcile.

The property this file defends is that the engine is honest about what it did NOT do.
Cloning, installing and executing a paper's repository are gated off by default, and
every path that stops early has to say so in a way the report can distinguish from a
finding. A harness that reported "no problems found" when it never fetched the code
would be worse than one with no code audit at all.

The second property is the FATAL boundary. Static pattern matches are suspicions and
top out at MAJOR; only a reproduction that was actually attempted and actually failed
reaches FATAL, and only through `overall_verdict`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness import code_audit, repo as repo_mod
from harness.artifacts import (PaperDoc, ProbeSpec, Reconciliation,
                               RepoAcquisition, Section, Table)
from harness.config import Config
from harness.local_exec import StartupEvidence, json_metric, parse_cell_number, reconcile, resolve_command
from harness.stages.probe import cell_contents, plan_execution
from harness.stages.report import overall_verdict

PID = "coderepro"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path)


def doc_with(text: str) -> PaperDoc:
    return PaperDoc(paper_id=PID, sections=[Section(section_idx=0, text=text)])


# --------------------------------------------------------------------------- #
# 1. Acquisition — URL extraction survives PDF mangling, and gates hold
# --------------------------------------------------------------------------- #
def test_urls_survive_the_space_pdf_extraction_injects():
    doc = doc_with("Code is available: https: //github.com/wuyang98/weathergen")
    assert repo_mod.find_repo_urls(doc) == ["https://github.com/wuyang98/weathergen"]


def test_trailing_sentence_punctuation_and_dot_git_are_stripped():
    doc = doc_with("This project is available at https: //github.com/mbzuai-nlp/finchain.git.")
    assert repo_mod.find_repo_urls(doc) == ["https://github.com/mbzuai-nlp/finchain"]


def test_a_footnote_to_someone_elses_repo_ranks_below_the_papers_own():
    doc = doc_with("2https://github.com/facebookresearch/vissl is the baseline. "
                   "Open source code is available here: https://github.com/HanxunH/LDReg.")
    urls = repo_mod.find_repo_urls(doc)
    assert urls[0] == "https://github.com/HanxunH/LDReg", urls
    assert urls[-1] == "https://github.com/facebookresearch/vissl", urls


def test_a_paper_with_no_repository_yields_nothing_rather_than_a_guess():
    assert repo_mod.find_repo_urls(doc_with("We release nothing. See doi:10.1000/x")) == []


def test_the_network_gate_blocks_cloning_and_says_so(tmp_path: Path):
    """Network defaults ON, but the gate must still exist and must still be honoured."""
    cfg = Config(projects_dir=tmp_path, allow_network=False)
    acq = repo_mod.acquire(cfg, tmp_path, PID,
                           doc_with("Code is available: https://github.com/x/y"))
    assert acq.status == "blocked"
    assert "SH_ALLOW_NETWORK" in acq.reason, "the report must be able to say why nothing happened"
    assert not (tmp_path / "runs" / PID / "repo").exists(), "a blocked clone must touch nothing"


def test_no_advertised_repo_is_unavailable_not_blocked(cfg: Config, tmp_path: Path):
    acq = repo_mod.acquire(cfg, tmp_path, PID, doc_with("no code here"))
    assert acq.status == "unavailable", "'we looked and found none' differs from 'we did not look'"


def test_install_is_a_second_gate_independent_of_cloning(tmp_path: Path):
    cfg = Config(projects_dir=tmp_path, allow_network=True, allow_install=False)
    acq = RepoAcquisition(url="u", status="cloned", path=str(tmp_path))
    assert repo_mod.build_env(cfg, tmp_path, PID, acq).env_status == "blocked"


# --------------------------------------------------------------------------- #
# 2. Dependency and entrypoint inspection — pure parsing, always safe
# --------------------------------------------------------------------------- #
def test_dependency_inspection_reads_every_declaration_format(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text(
        "torch>=2.0  # comment\n--index-url http://x\n\nscikit-learn==1.4\n", encoding="utf-8")
    (tmp_path / "environment.yml").write_text(
        "name: e\ndependencies:\n  - python=3.11\n  - pip:\n    - jax==0.4\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["sympy>=1.12"]\n', encoding="utf-8")

    files, deps, frameworks = repo_mod.inspect_dependencies(tmp_path)
    assert set(files) == {"requirements.txt", "environment.yml", "pyproject.toml"}
    assert {"torch", "scikit-learn", "jax", "sympy"} <= set(deps), deps
    assert {"pytorch", "sklearn", "jax", "sympy"} <= set(frameworks), frameworks


def test_entrypoint_prefers_an_evaluation_script_over_training(tmp_path: Path):
    (tmp_path / "train.py").write_text("", encoding="utf-8")
    (tmp_path / "eval.py").write_text("", encoding="utf-8")
    assert repo_mod.find_entrypoint(tmp_path) == "eval.py"


def test_no_entrypoint_is_empty_not_an_invention(tmp_path: Path):
    assert repo_mod.find_entrypoint(tmp_path) == ""


def test_the_standalone_scaffold_refuses_to_produce_a_number(tmp_path: Path):
    """A scaffold that silently returned a constant would manufacture a verdict."""
    acq = repo_mod.synthesize_standalone(tmp_path, PID, "the claim", "T1:r0:c1", "59.28")
    assert acq.status == "synthesized"
    script = Path(acq.path)
    assert script.name == "standalone_probe.py" and script.is_file()

    p = subprocess.run([Config.load().python, str(script), "--seed", "0"],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode != 0, "the scaffold must fail until compute() is implemented"
    assert "NotImplementedError" in (p.stderr or "")
    assert "SH_DEVICE" in (p.stdout or ""), "it still declares its device honestly"


# --------------------------------------------------------------------------- #
# 3. Static auditing — the three cheat classes, and the FATAL boundary
# --------------------------------------------------------------------------- #
BASELINE_CRIPPLED = '''
def train(arm):
    if arm == "baseline":
        epochs = 5
    else:
        epochs = 200
    return epochs
'''

LEAKY = '''
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

def go(X, y):
    Xs = StandardScaler().fit_transform(X)
    return train_test_split(Xs, y, random_state=0)
'''

ORACLE_METRIC = '''
def accuracy(y_true, y_pred, scores):
    return max(scores)
'''


def test_baseline_crippling_is_detected_with_both_numbers():
    hits = code_audit.audit_source("t.py", BASELINE_CRIPPLED)
    assert [f.rule_id for f in hits] == ["cripple-branch-budget"], hits
    assert hits[0].category == "baseline_crippling" and hits[0].severity == "MAJOR"
    assert "epochs = 5" in hits[0].code_quote, "the quote must be the source line, verbatim"


def test_a_symmetric_budget_is_not_flagged():
    symmetric = BASELINE_CRIPPLED.replace("epochs = 5", "epochs = 200")
    assert code_audit.audit_source("t.py", symmetric) == [], \
        "equal budgets are the correct design and must never be reported"


def test_a_baseline_given_the_larger_budget_is_not_flagged():
    generous = BASELINE_CRIPPLED.replace("epochs = 5", "epochs = 400")
    assert [f.rule_id for f in code_audit.audit_source("t.py", generous)] == []


def test_scaling_before_the_split_is_detected():
    hits = {f.rule_id for f in code_audit.audit_source("t.py", LEAKY)}
    assert "leak-fit-before-split" in hits, hits


def test_a_split_done_before_scaling_is_clean():
    fixed = '''
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

def go(X, y):
    Xtr, Xte, ytr, yte = train_test_split(X, y, random_state=0)
    scaler = StandardScaler().fit(Xtr)
    return scaler.transform(Xtr), scaler.transform(Xte)
'''
    assert code_audit.audit_source("t.py", fixed) == [], code_audit.audit_source("t.py", fixed)


def test_an_oracle_metric_is_detected():
    hits = {f.rule_id for f in code_audit.audit_source("t.py", ORACLE_METRIC)}
    assert "metric-best-of-n" in hits, hits


def test_static_analysis_never_returns_fatal():
    """FATAL belongs to a reproduction that was run and failed, never to a grep."""
    every = BASELINE_CRIPPLED + LEAKY + ORACLE_METRIC
    hits = code_audit.audit_source("t.py", every)
    assert hits, "the combined fixture must trip something"
    assert all(f.severity in ("MAJOR", "MINOR") for f in hits), [f.severity for f in hits]


def test_unparseable_source_is_survived_not_raised():
    assert code_audit.audit_source("broken.py", "def (:\n") == []


def test_auditing_a_missing_checkout_reports_skipped(tmp_path: Path):
    result = code_audit.audit_repo(tmp_path / "nope")
    assert result.skipped and result.findings == []


def test_vendored_third_party_code_is_not_audited(tmp_path: Path):
    (tmp_path / "third_party").mkdir()
    (tmp_path / "third_party" / "x.py").write_text(BASELINE_CRIPPLED, encoding="utf-8")
    assert code_audit.audit_repo(tmp_path).findings == [], \
        "a vendored library's code is not the paper's contribution"


def test_findings_are_uniquely_numbered_and_severity_ordered(tmp_path: Path):
    (tmp_path / "a.py").write_text(BASELINE_CRIPPLED + LEAKY, encoding="utf-8")
    result = code_audit.audit_repo(tmp_path)

    ids = [f.finding_id for f in result.findings]
    assert ids and len(ids) == len(set(ids)), f"ids must be unique: {ids}"
    severities = [f.severity for f in result.findings]
    assert severities == sorted(severities, key=lambda s: {"MAJOR": 1, "MINOR": 0}[s],
                                reverse=True), severities
    assert result.files_scanned == 1 and result.lines_scanned > 0


# --------------------------------------------------------------------------- #
# 4. Reconciliation — arithmetic, with the guards that prevent a false conviction
# --------------------------------------------------------------------------- #
def _spec(cell: str = "59.28", provenance: str = "repo_exec") -> ProbeSpec:
    return ProbeSpec(paper_id=PID, table_ref="T1:r0:c1", claimed_cell_value=cell,
                     provenance=provenance)


def test_a_match_inside_the_noise_band_is_verified():
    r = reconcile(_spec(), [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "RESOLVED_VERIFIED" and r.delta_error is not None
    assert r.delta_error <= r.noise_band


def test_a_miss_outside_the_noise_band_is_a_failed_reproduction():
    r = reconcile(_spec(), [64.10, 64.20], 0.10, [0, 1])
    assert r.status == "FAILED_REPRODUCTION"
    assert r.delta_error is not None and r.delta_error > r.noise_band


def test_code_that_will_not_START_is_not_a_failed_reproduction():
    """Superseded contract. This test previously asserted the opposite.

    A `ModuleNotFoundError` is the signature of a dependency that was never installed —
    a fact about this machine, not about the paper. Reading it as FAILED_REPRODUCTION let
    `overall_verdict` escalate to RED with "the paper's own code does not reproduce the
    number it prints", an accusation no measurement supported. The crash must be
    classified, and only a crash after the experiment demonstrably began may convict; see
    tests/test_execution_capability.py for the full matrix.
    """
    r = reconcile(_spec(), [], 0.10, [], failure="exit 1: ModuleNotFoundError: no module 'mamba'",
                  evidence=StartupEvidence(setup_error="modulenotfounderror"))
    assert r.status == "INCONCLUSIVE"
    assert r.failure_class == "startup_failure" and r.reached_experiment is False


def test_code_that_breaks_after_starting_is_still_a_failed_reproduction():
    """The capability work must not cost the harness its real convictions."""
    r = reconcile(_spec(), [], 0.10, [], failure="exit 1: RuntimeError: loss became NaN",
                  evidence=StartupEvidence(saw_contract_line=True, stdout_lines=30,
                                           ran_seconds=120.0))
    assert r.status == "FAILED_REPRODUCTION" and r.failure_class == "runtime_failure"


def test_a_units_mismatch_is_inconclusive_not_an_accusation():
    """97.0 in the cell against 0.9684 measured is percent-vs-fraction, not fraud."""
    r = reconcile(_spec("97.0"), [0.9684, 0.9690], 0.01, [0, 1])
    assert r.status == "INCONCLUSIVE" and "units mismatch" in r.reason


def test_no_parsed_metric_is_inconclusive():
    assert reconcile(_spec(), [], 0.10, []).status == "INCONCLUSIVE"


def test_a_zero_noise_band_cannot_convict():
    r = reconcile(_spec(), [64.0, 64.0], 0.0, [0, 1])
    assert r.status == "INCONCLUSIVE", "`<= 2 sigma` with sigma=0 would demand exactness"


def test_an_unparseable_cell_is_inconclusive():
    assert reconcile(_spec("n/a"), [1.0, 1.1], 0.5, [0, 1]).status == "INCONCLUSIVE"


def test_cell_and_json_parsing_take_the_reported_value():
    assert parse_cell_number("12.196 ± 0.207") == 12.196
    assert parse_cell_number("80.5%(161)") == 80.5
    assert parse_cell_number("—") is None
    assert json_metric('{"epoch": 3}\n{"accuracy": 0.91}') == 0.91
    assert json_metric('{"batch_size": 64}') is None


# --------------------------------------------------------------------------- #
# 5. Escalation and execution planning
# --------------------------------------------------------------------------- #
def _rec(status: str) -> Reconciliation:
    return Reconciliation(table_ref="T1:r0:c1", status=status, reason="r", noise_band=0.1)


def test_a_failed_reproduction_turns_the_verdict_red_on_its_own():
    verdict, reason = overall_verdict([], _rec("FAILED_REPRODUCTION"))
    assert verdict == "RED" and "Failed code reproduction" in reason


@pytest.mark.parametrize("status", ["INCONCLUSIVE", "RESOLVED_VERIFIED", "NOT_ATTEMPTED"])
def test_no_other_reconciliation_status_escalates(status: str):
    assert overall_verdict([], _rec(status))[0] == "GREEN", \
        "only a demonstrated failure may drive the verdict"


def test_execution_is_not_planned_while_the_gate_is_shut(cfg: Config):
    acq = RepoAcquisition(url="u", status="cloned", path="/tmp/r", entrypoint="eval.py")
    assert plan_execution(cfg, ProbeSpec(paper_id=PID), acq).command == []


def test_execution_is_planned_once_every_condition_holds(tmp_path: Path, monkeypatch):
    """Planning now has a fourth condition: this machine must be able to run it fairly.

    The fixture below is a real checkout with a real entrypoint that really accepts
    `--seed`, because the capability check reads the repository rather than trusting the
    acquisition record. `assess_capability` is stubbed to `established` so this test stays
    about PLANNING; the capability decision itself is covered in
    tests/test_execution_capability.py.
    """
    from harness import repo as repo_mod
    from harness.artifacts import ExecCapability
    from harness.stages import probe as probe_stage

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "eval.py").write_text('import os\np.add_argument("--seed")\n', encoding="utf-8")
    monkeypatch.setattr(probe_stage.repo_mod, "assess_capability",
                        lambda *a, **k: ExecCapability(established=True, reason_code="established"))
    # Identity is covered in tests/test_experiment_identity.py; stub it so this test stays
    # about planning rather than about resolution.
    from harness.artifacts import (CandidateCommand, ConfigurationIdentity,
                                   ExperimentIdentity, MetricIdentity)
    monkeypatch.setattr(probe_stage.experiment_id, "resolve", lambda *a, **k: (
        ExperimentIdentity(state="established",
                           command=CandidateCommand(argv=["python", "eval.py"],
                                                    source_ref="README.md:1")),
        MetricIdentity(state="established"), ConfigurationIdentity(state="established")))
    # Two more preconditions, stubbed for the same reason as the two above: E1 resource fit
    # is covered in tests/test_resource_preflight.py and E2 commit pinning in
    # tests/test_commit_pinning.py. This test is about what PLANNING does once every
    # precondition holds, so it has to be able to reach that state.
    from harness.artifacts import CommitVerification, ResourceCapability
    monkeypatch.setattr(probe_stage.resources_mod, "assess_resources",
                        lambda *a, **k: ResourceCapability(state="satisfied", reason="stub"))
    monkeypatch.setattr(probe_stage.repo_mod, "verify_commit",
                        lambda *a, **k: CommitVerification(state="verified", expected="a" * 40,
                                                           actual="a" * 40))

    cfg = Config(projects_dir=tmp_path, allow_repo_exec=True)
    acq = RepoAcquisition(url="u", status="cloned", path=str(repo), entrypoint="eval.py",
                          env_status="ready", env_path="/tmp/env/python")
    spec = plan_execution(cfg, ProbeSpec(paper_id=PID), acq, doc_with("x"))
    assert spec.command == ["python", "eval.py"], "the argv comes from the repo, not a filename"
    assert spec.cwd == str(repo) and spec.interpreter == "/tmp/env/python"
    assert spec.provenance == "repo_exec"
    assert repo_mod is not None


def test_execution_is_not_planned_without_an_entrypoint(tmp_path: Path):
    cfg = Config(projects_dir=tmp_path, allow_repo_exec=True)
    acq = RepoAcquisition(url="u", status="cloned", path="/tmp/r", entrypoint="")
    assert plan_execution(cfg, ProbeSpec(paper_id=PID), acq).command == []


def test_the_command_is_substituted_per_seed_and_uses_the_isolated_interpreter(tmp_path: Path):
    spec = ProbeSpec(paper_id=PID, command=["python", "eval.py", "--seed", "{seed}"],
                     cwd="/tmp/r", interpreter="/tmp/env/python", arms=["reproduction"])
    argv, cwd = resolve_command(Config(projects_dir=tmp_path), spec,
                                tmp_path / "probe.py", 3, "reproduction")
    assert argv == ["/tmp/env/python", "eval.py", "--seed", "3"]
    assert cwd == Path("/tmp/r")


def test_cell_contents_resolves_an_address_and_refuses_a_bad_one():
    doc = PaperDoc(paper_id=PID, tables=[
        Table(table_idx=0), Table(table_idx=1, rows=[["ours", "59.28"]])])
    assert cell_contents(doc, "T1:r0:c1") == "59.28"
    assert cell_contents(doc, "T9:r0:c0") == ""
    assert cell_contents(doc, "p7") == ""
