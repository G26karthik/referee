"""S3 identity — the executed program must be bound to the cited cell before it may convict.

Capability answers "can this machine run the code". It cannot answer "is this the right
program, emitting the right quantity, under the right settings", and a CAPABLE run of the
wrong program is more dangerous than a crash: a crash is visible, a confident irrelevant
number is not.

The case these tests are built from is real. `find_entrypoint` preferred `eval.py` then
`evaluate.py` and selected APT's `evaluate.py`, a DeepSpeed FLOPs/latency profiler, to be
reconciled against `T2:r3:c11` — relative training memory for the LLMPruner baseline row,
normalised to LoRA = 100%. Wrong program, wrong quantity, wrong basis, and a row the
repository does not implement at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.artifacts import (CandidateCommand, ConfigurationIdentity, ExperimentIdentity,
                               MetricIdentity, PaperDoc, ProbeSpec, QuantFinding, Section,
                               Table)
from harness.experiment_id import (cell_basis, harvest_candidates, identities_established,
                                   repo_implements, resolve_configuration, resolve_experiment,
                                   resolve_metric)
from harness.local_exec import StartupEvidence, reconcile
from harness.stages.report import overall_verdict
from conftest import incidental_objects, material_objects

# A results table shaped like APT's Table 3: a ratio column carrying its own 100% row.
RELATIVE = Table(
    table_idx=2, caption="Table 3: LLaMA 2 7B 30% sparsity pruning results with Alpaca",
    header=["Method", "Avg", "Train Mem."],
    rows=[["LoRA", "57.9", "100.0%"],
          ["LLMPruner", "42.9", "253.6%"],
          ["APT", "50.0", "75.8%"]])
ABSOLUTE = Table(
    table_idx=1, caption="Table 1: RoBERTa accuracy on SST-2",
    header=["Method", "Accuracy"],
    rows=[["LoRA", "94.1"], ["APT", "93.5"]])


def _doc(*tables: Table, numbers: list[QuantFinding] | None = None) -> PaperDoc:
    return PaperDoc(paper_id="p", title="T", tables=list(tables),
                    sections=[Section(section_idx=0, text="body")],
                    reported_numbers=numbers or [])


def _repo(tmp_path: Path, *, files: dict[str, str], readme: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if readme:
        (repo / "README.md").write_text(readme, encoding="utf-8")
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return repo


# --------------------------------------------------------------------------- #
# 1. candidate discovery — never from filenames
# --------------------------------------------------------------------------- #
def test_candidates_come_from_the_readme_with_a_source_ref(tmp_path: Path):
    repo = _repo(tmp_path, files={"train.py": "x=1\n"},
                 readme="## Training\n```bash\nbash scripts/run_sst2.sh\n```\n")
    found = harvest_candidates(repo)
    assert ["bash", "scripts/run_sst2.sh"] in [c.argv for c in found]
    readme_cmd = next(c for c in found if c.source == "readme")
    assert readme_cmd.source_ref.startswith("README.md:")


def test_candidates_come_from_run_sh_and_scripts_dir(tmp_path: Path):
    repo = _repo(tmp_path, files={"run.sh": "bash scripts/a.sh\n",
                                  "scripts/a.sh": "echo hi\n", "scripts/b.sh": "echo ho\n"})
    found = harvest_candidates(repo)
    assert {"run_script", "scripts_dir"} <= {c.source for c in found}
    argvs = [" ".join(c.argv) for c in found]
    assert len(argvs) == len(set(argvs)), "a command advertised twice is listed once"


def test_a_filename_alone_is_never_a_candidate(tmp_path: Path):
    """`evaluate.py` existing says nothing about what it does or whether to run it."""
    repo = _repo(tmp_path, files={"evaluate.py": "import deepspeed\n"})
    assert harvest_candidates(repo) == []


def test_a_repository_advertising_nothing_yields_no_candidates(tmp_path: Path):
    assert harvest_candidates(_repo(tmp_path, files={"README.md": "# no commands\n"})) == []


# --------------------------------------------------------------------------- #
# 2. metric identity — quantity AND basis
# --------------------------------------------------------------------------- #
def test_a_ratio_column_is_detected_by_its_own_100_percent_row():
    assert cell_basis(RELATIVE, 2) == ("relative_to_baseline", "%")


def test_an_absolute_column_is_not_mistaken_for_a_ratio():
    assert cell_basis(ABSOLUTE, 1)[0] == "absolute"


def test_flops_output_is_refused_against_an_accuracy_cell():
    """The APT `evaluate.py` case, reduced."""
    profiler = CandidateCommand(argv=["python", "evaluate.py"], source_ref="README.md:1",
                                emits=["model_flops", "model_macs", "latency"], label="flops, latency")
    ident = resolve_metric(_doc(ABSOLUTE), "T1:r1:c1", profiler)
    assert ident.state == "no_candidate" and not ident.established
    assert "different quantities" in ident.reason


def test_memory_in_mb_is_refused_against_a_ratio_to_baseline_cell():
    mem = CandidateCommand(argv=["python", "train.py"], source_ref="README.md:1",
                           emits=["peak_memory"], label="memory")
    ident = resolve_metric(_doc(RELATIVE), "T2:r2:c2", mem)
    assert ident.state == "unsupported"
    assert ident.cell_basis == "relative_to_baseline" and ident.output_basis == "absolute"


def test_a_relative_cell_records_that_it_needs_both_arms():
    mem = CandidateCommand(argv=["python", "t.py"], emits=["peak_memory"], source_ref="r:1")
    assert resolve_metric(_doc(RELATIVE), "T2:r2:c2", mem).requires_arms == ["baseline", "method"]


def test_a_matching_absolute_quantity_is_established():
    acc = CandidateCommand(argv=["python", "train.py"], source_ref="README.md:3",
                           emits=["eval_accuracy"], label="accuracy")
    ident = resolve_metric(_doc(ABSOLUTE), "T1:r1:c1", acc)
    assert ident.established and ident.output_key == "eval_accuracy"
    assert ident.evidence and all(e.source_ref for e in ident.evidence)


def test_a_metric_with_no_command_is_not_bound():
    assert resolve_metric(_doc(ABSOLUTE), "T1:r1:c1", None).state == "no_candidate"


# --------------------------------------------------------------------------- #
# 3. experiment identity — the row's method must be implemented here
# --------------------------------------------------------------------------- #
def test_a_baseline_the_repo_does_not_implement_has_no_candidate(tmp_path: Path):
    """APT's cell reports LLMPruner, a third-party baseline its code never implements."""
    repo = _repo(tmp_path, files={"train.py": "print('apt')\n"},
                 readme="```bash\npython train.py\n```\n")
    ident = resolve_experiment(_doc(RELATIVE), repo, "T2:r1:c2")
    assert ident.state == "no_candidate" and ident.row_method == "LLMPruner"
    assert "third-party baseline" in ident.reason


def test_a_method_named_only_in_a_string_literal_is_not_implemented(tmp_path: Path):
    """A matplotlib label beside hardcoded literals is a citation, not an implementation."""
    repo = _repo(tmp_path, files={
        "plot/plot.py": "axs.scatter([1.148], [42.9/53.4*100], label='LLMPruner')\n"})
    assert repo_implements(repo, "LLMPruner") is False


def test_a_method_present_as_a_module_or_identifier_is_implemented(tmp_path: Path):
    repo = _repo(tmp_path, files={"prune/llmpruner.py": "def run(): pass\n"})
    assert repo_implements(repo, "LLMPruner") is True


def test_ambiguous_candidates_abstain(tmp_path: Path):
    repo = _repo(tmp_path, files={
        "apt/__init__.py": "# the cited row's method is implemented here\n",
        "a.py": "json.dump({'eval_accuracy': 1}, f)\n",
        "b.py": "json.dump({'eval_accuracy': 2}, f)\n",
        "run.sh": "python a.py\npython b.py\n"})
    ident = resolve_experiment(_doc(ABSOLUTE), repo, "T1:r1:c1")
    assert ident.state == "ambiguous" and ident.command is None
    assert "would be a guess" in ident.reason


def test_an_established_experiment_carries_reverifiable_evidence(tmp_path: Path):
    repo = _repo(tmp_path, files={"apt/__init__.py": "# the cited row's method lives here\n",
                                  "train.py": "json.dump({'eval_accuracy': 1}, f)\n"},
                 readme="```bash\npython train.py\n```\n")
    ident = resolve_experiment(_doc(ABSOLUTE), repo, "T1:r1:c1")
    assert ident.established and ident.command is not None
    assert ident.evidence and all(e.source_ref for e in ident.evidence)


# --------------------------------------------------------------------------- #
# 4. configuration identity and seed policy
# --------------------------------------------------------------------------- #
def test_a_fixed_repo_seed_against_a_multi_seed_harness_is_unsupported():
    cmd = CandidateCommand(argv=["bash", "s.sh"], seed_flag="--seed", seed_values=["128"])
    ident = resolve_configuration(_doc(RELATIVE), "T2:r2:c2", cmd, harness_seeds=5)
    assert ident.state == "unsupported"
    assert "different protocol" in ident.reason
    assert ident.seed_policy_repo == "fixed --seed 128"


def test_unrecoverable_configuration_fields_are_named_not_guessed():
    """`sparsity` moved from REQUIRED to REPORTED in the correction pass.

    It exists only in pruning papers, and requiring it made every paper outside that
    domain `unmapped` on a field its caption could not carry — a domain vocabulary
    promoted into a universal gate. Model and dataset are what identify a configuration
    in general; a sparsity that IS stated still lands in `matched` as evidence.
    """
    cmd = CandidateCommand(argv=["bash", "s.sh"], seed_flag="--seed", seed_values=["1", "2", "3"])
    ident = resolve_configuration(_doc(ABSOLUTE), "T1:r1:c1", cmd, harness_seeds=3)
    assert ident.matched.get("dataset"), "what IS recoverable is still recovered"
    assert "sparsity" not in ident.unrecoverable, "pruning vocabulary is not universal"
    assert set(ident.unrecoverable) <= {"model", "dataset"}


def test_a_repo_with_no_seed_argument_is_recorded_as_such():
    cmd = CandidateCommand(argv=["bash", "s.sh"])
    ident = resolve_configuration(_doc(RELATIVE), "T2:r2:c2", cmd, harness_seeds=5)
    assert ident.seed_policy_repo == "no seed argument found"


# --------------------------------------------------------------------------- #
# 5. the gate, and its interaction with reconciliation
# --------------------------------------------------------------------------- #
def _established() -> tuple[ExperimentIdentity, MetricIdentity, ConfigurationIdentity]:
    return (ExperimentIdentity(state="established",
                               command=CandidateCommand(argv=["python", "t.py"], source_ref="r:1")),
            MetricIdentity(state="established", output_key="eval_accuracy"),
            ConfigurationIdentity(state="established"))


def _spec(experiment=None, metric=None, config=None, provenance="repo_exec") -> ProbeSpec:
    return ProbeSpec(paper_id="p", table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     target_id="T1",   # stamped onto the reconciliation, as in production
                     provenance=provenance, command=["python", "t.py"],
                     experiment=experiment, metric_identity=metric, configuration=config)


@pytest.mark.parametrize("state,cls", [("no_candidate", "experiment_unidentified"),
                                       ("ambiguous", "experiment_unidentified"),
                                       ("unmapped", "experiment_unidentified")])
def test_an_unproven_experiment_blocks_reconciliation(state: str, cls: str):
    e, m, c = _established()
    r = reconcile(_spec(ExperimentIdentity(state=state), m, c), [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "INCONCLUSIVE" and r.failure_class == cls


def test_an_unbound_metric_after_a_SUCCESSFUL_run_is_inconclusive():
    """The dangerous case: the process succeeded, so nothing looks wrong."""
    e, m, c = _established()
    r = reconcile(_spec(e, MetricIdentity(state="unsupported", reason="ratio needs both arms"), c),
                  [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "INCONCLUSIVE" and r.failure_class == "metric_unbound"
    assert overall_verdict([], r)[0] == "GREEN"


def test_a_configuration_mismatch_blocks_reconciliation():
    e, m, c = _established()
    r = reconcile(_spec(e, m, ConfigurationIdentity(state="unsupported", reason="seed policy")),
                  [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "INCONCLUSIVE" and r.failure_class == "configuration_unmatched"


def test_full_identity_plus_capability_plus_runtime_failure_still_convicts():
    from harness.artifacts import ExecCapability
    e, m, c = _established()
    spec = _spec(e, m, c)
    spec.capability = ExecCapability(established=True, reason_code="established")
    r = reconcile(spec, [], 0.10, [], failure="exit 1: RuntimeError: NaN loss",
                  evidence=StartupEvidence(saw_contract_line=True, stdout_lines=30,
                                           ran_seconds=120.0))
    assert r.status == "FAILED_REPRODUCTION" and r.failure_class == "runtime_failure"
    assert overall_verdict([], r, objects=material_objects(r.target_id))[0] == "RED"


def test_full_identity_with_a_matching_result_verifies():
    e, m, c = _established()
    r = reconcile(_spec(e, m, c), [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "RESOLVED_VERIFIED"
    assert r.experiment_state == "established" and r.metric_state == "established"


def test_full_identity_with_a_mismatching_result_fails_reproduction():
    e, m, c = _established()
    r = reconcile(_spec(e, m, c), [64.10, 64.20], 0.10, [0, 1])
    assert r.status == "FAILED_REPRODUCTION"


def test_synthesized_provenance_still_cannot_reach_a_verdict():
    """The provenance ceiling precedes identity and is unchanged."""
    e, m, c = _established()
    r = reconcile(_spec(e, m, c, provenance="synthesized"), [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "INCONCLUSIVE" and "not the paper's own code" in r.reason


def test_a_driver_script_with_no_command_still_needs_an_identity_chain():
    """C4 — a driver spec can carry a hand-written `script` with no `command` at all,
    since a human wrote spec.json and pointed it at real code. Gating the identity check
    on `spec.command` (the old condition) let exactly that spec reconcile with ZERO
    identity established: not the experiment, not the metric, not the configuration.
    `driver` is trusted as to WHO wrote the code, never as a substitute for showing the
    executed output answers the cited cell — the same bar `repo_exec` has to clear.
    """
    spec = ProbeSpec(paper_id="p", table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     provenance="driver", script="print(1)")
    r = reconcile(spec, [59.30, 59.26], 0.10, [0, 1])
    assert r.status == "INCONCLUSIVE", "an arbitrary driver spec must not acquit"
    assert r.failure_class == "experiment_unidentified"
    convict = reconcile(spec, [1.0, 1.1], 0.10, [0, 1])
    assert convict.status == "INCONCLUSIVE", "nor may an arbitrary driver spec convict"

    e, m, c = _established()
    spec.experiment, spec.metric_identity, spec.configuration = e, m, c
    # Once the SAME chain a repo_exec spec needs is actually established, the trusted
    # channel still works — this is the "legitimate trusted-channel semantics" that
    # must survive the fix, not a blanket ban on driver reconciliation.
    assert reconcile(spec, [59.30, 59.26], 0.10, [0, 1]).status == "RESOLVED_VERIFIED"


def test_identities_established_reports_the_first_unproven_link():
    e, m, c = _established()
    ok, cls, why = identities_established(e, MetricIdentity(state="ambiguous"), c)
    assert ok is False and cls == "metric_unbound" and "ambiguous" in why
