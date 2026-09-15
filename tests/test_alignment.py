"""Step 7 — `harness.alignment`: telling fitting candidates apart by CONFIGURATION.

**The regression this whole package exists to fix.** `experiment_id.resolve_experiment`
refuses `ambiguous` when several advertised commands emit the cited cell's quantity — 84
of 84 on a real paper, none chosen. What none of those checks asked is whether the
SCRIPTS THEMSELVES declare which configuration they run under, against what the cited
row asks for. These tests pin the fix end to end: two candidate scripts differing only by
a hardcoded `--sparsity` value, disambiguated by the value the cited ROW's own cells
state, with the excluded candidate's contradiction named in the record.

**And the constraint the fix must hold to.** `backends.authorize()` and
`experiment_id.identities_established` are asserted UNCHANGED — every module here is
upstream evidence feeding the SAME three-state decision, never a fourth state, a
confidence score, or a second place the decision is made.
"""
from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest

from harness import experiment_id as eid
from harness.alignment import argparse_surface, candidates, configs, configuration, evaluator, trial
from harness.artifacts import (ArgSpec, CandidateCommand, PaperDoc, Table, TrialResult)
from harness.backends import BackendResources, ExecCapability, ExecOutcome, ExecRequest, ExecutionBackend
from harness.config import Config


# --------------------------------------------------------------------------- #
# argparse_surface
# --------------------------------------------------------------------------- #
def test_argparse_surface_reads_flags_defaults_and_choices():
    src = ('p.add_argument("--sparsity", type=float, default=0.5, choices=[0.2, 0.5])\n'
          'p.add_argument("--base_model", required=True)\n')
    specs = argparse_surface.parse_source(src, source_name="eval.py")
    by = argparse_surface.by_flag(specs)
    assert by["--sparsity"].default == "0.5" and by["--sparsity"].choices == ["0.2", "0.5"]
    assert by["--base_model"].required is True


def test_argparse_surface_never_executes_anything():
    """Purely a text parser: an unparseable or hostile-looking file yields no crash and
    no flags, never an attempt to import or run it."""
    assert argparse_surface.parse_source("import os; os.system('rm -rf /')") == []
    assert argparse_surface.parse_file(Path("/does/not/exist.py")) == []


# --------------------------------------------------------------------------- #
# configs
# --------------------------------------------------------------------------- #
def test_configs_has_no_yaml_dependency_and_stays_flat():
    assert configs.parse_flat("sparsity: 0.5\nmodel: llama-7b\n") == {
        "sparsity": "0.5", "model": "llama-7b"}
    assert configs.parse_flat("nested:\n  a: 1\n") == {}, "nesting is outside this parser's claim"


def test_configs_only_reads_files_the_command_references_or_a_convention():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "configs").mkdir()
        (repo / "configs" / "x.yaml").write_text("dataset: alpaca\n", encoding="utf-8")
        cmd = CandidateCommand(argv=["bash", "s.sh"])
        fields, _ = configs.declared_fields(
            repo, cmd, script_text="--config configs/x.yaml", include_conventional=False)
        assert fields == {"dataset": "alpaca"}
        # without the reference AND without the conventional sweep, nothing is found
        empty, _ = configs.declared_fields(repo, cmd, script_text="", include_conventional=False)
        assert empty == {}


# --------------------------------------------------------------------------- #
# candidates: three tiers, filename NEVER a source
# --------------------------------------------------------------------------- #
def test_candidates_prefers_script_text_over_config_over_argparse_default():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "r.sh").write_text(
            "python eval.py --sparsity 0.5\n", encoding="utf-8")
        cmd = CandidateCommand(argv=["bash", "scripts/r.sh"], source_ref="scripts/r.sh:1")
        candidates.declared_configuration(repo, cmd)
        assert cmd.declared_args["sparsity"] == "0.5"


def test_candidates_never_treats_an_unresolved_shell_variable_as_a_literal():
    """The regression found against the real corpus: `--model_name_or_path
    "${model_name}"` is not evidence the model is the literal string '${model_name}' —
    it is set elsewhere, and reading it as a literal made 76 of 162 real candidates on
    one paper appear to CONTRADICT the cited row's actual model name."""
    assert candidates.script_text_fields(
        'python eval.py --model_name_or_path "${model_name}"') == {}
    assert candidates.script_text_fields(
        'python eval.py --sparsity 0.5 --model_name_or_path "${model_name}"') == {
        "sparsity": "0.5"}


def test_candidates_never_infers_from_a_filename():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "sparsity80.sh").write_text("python eval.py\n", encoding="utf-8")
        cmd = CandidateCommand(argv=["bash", "scripts/sparsity80.sh"], source_ref="x:1")
        candidates.declared_configuration(repo, cmd)
        assert "sparsity" not in cmd.declared_args


def test_candidates_is_idempotent_and_never_overwrites():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "s.sh").write_text("python eval.py --sparsity 0.9\n", encoding="utf-8")
        cmd = CandidateCommand(argv=["bash", "s.sh"], source_ref="s.sh:1",
                              declared_args={"sparsity": "PRESET"})
        candidates.declared_configuration(repo, cmd)
        assert cmd.declared_args["sparsity"] == "PRESET"


# --------------------------------------------------------------------------- #
# configuration.narrow — contradiction eliminates, silence does not
# --------------------------------------------------------------------------- #
def test_narrow_eliminates_on_contradiction_and_prefers_genuine_agreement():
    a = CandidateCommand(argv=["bash", "a.sh"], source_ref="a.sh:1",
                        declared_args={"sparsity": "0.2"})
    b = CandidateCommand(argv=["bash", "b.sh"], source_ref="b.sh:1",
                        declared_args={"sparsity": "0.5"})
    c = CandidateCommand(argv=["bash", "c.sh"], source_ref="c.sh:1")     # silent
    survivors, reasons = configuration.narrow([a, b, c], {"sparsity": "0.5"})
    assert survivors == [b]
    assert "c.sh:1" not in reasons


def test_narrow_reports_still_ambiguous_when_nothing_positively_agrees():
    d = CandidateCommand(argv=["bash", "d.sh"], source_ref="d.sh:1")
    e = CandidateCommand(argv=["bash", "e.sh"], source_ref="e.sh:1")
    survivors, _ = configuration.narrow([d, e], {"sparsity": "0.5"})
    assert survivors == [d, e]


@pytest.mark.parametrize("a,b", [("50%", "0.5"), ("50", "0.5"), ("LLaMA-7B", "llama_7b")])
def test_expected_fields_normalisation_equates_honest_spellings(a, b):
    field = "sparsity" if a in ("50%", "50") else "model"
    assert configuration._normalize(field, a) == configuration._normalize(field, b)


def test_expected_fields_row_wins_over_caption():
    """The root cause of a real paper's per-row ambiguity: a caption naming ONE overall
    sparsity while each row of the same table states its own, different one."""
    t = Table(table_idx=2, caption="Table 2: accuracy at 50% sparsity",
             header=["Method", "Ratio", "Acc"], rows=[["Ours", "20%", "61.4"]])
    fields, evidence = configuration.expected_fields(t, row=0, col=2)
    assert fields["sparsity"] == "20"
    assert evidence["sparsity"] == "T2:r0"


def test_a_density_column_is_not_read_as_sparsity():
    """The real APT table that produced SEVEN false identity bindings.

    `Density` 10% IS `sparsity` 90%. Reading the first as the second matched a command
    declaring `sparsity=10` against a row meaning the opposite, and that match narrowed 84
    candidate commands to exactly one — so `resolve_experiment` returned `established` on
    seven targets of the shipped corpus.

    An established identity AUTHORISES running that command against that cell, so this is
    not a missed check: it is a measurement of the wrong experiment, carrying the weight of
    an admissible provenance. The rule that stops it is that a row value is accepted only
    where the table's own headers or the row itself NAME the field, and `density` is
    deliberately not a naming token for sparsity because it is the inverse quantity.
    """
    t = Table(table_idx=11,
              caption="Table 7. Comparison of APT to existing unstructured pruning "
                      "baseline with using PEFT in conjunction.",
              header=[],
              rows=[["Density", "", "Method", "MNLI", "QQP"],
                    ["", "10%", "PST", "79.6", "86.1"]])
    fields, evidence = configuration.expected_fields(t, row=1, col=3)
    assert "sparsity" not in fields, (
        f"a density column must yield no sparsity, got {fields!r}")
    assert not evidence


def test_a_bare_percentage_with_nothing_naming_the_field_is_not_a_configuration_value():
    """The general form of the defect above: a number of the right shape is not a value.

    Neither the caption nor any header names sparsity, so the `30%` in this row is just a
    number. Accepting it would let any table of percentages narrow a candidate set.
    """
    t = Table(table_idx=4, caption="Table 4: throughput by configuration",
              header=["System", "Share", "Score"], rows=[["A", "30%", "12.5"]])
    fields, _ = configuration.expected_fields(t, row=0, col=2)
    assert "sparsity" not in fields, fields


def test_a_metric_value_cannot_supply_a_sparsity_from_inside_itself():
    """`4523.5%` used to yield a sparsity of 5, displacing a caption's correct 60%."""
    assert configuration.FIELD_VALUE_PATTERNS["sparsity"].search("4523.5%") is None
    assert configuration.FIELD_VALUE_PATTERNS["sparsity"].search("484.7%") is None
    assert configuration.FIELD_VALUE_PATTERNS["sparsity"].search("60% sparsity").group(1) == "60"


# --------------------------------------------------------------------------- #
# evaluator — a real call site, never a bare quoted key
# --------------------------------------------------------------------------- #
def test_evaluator_requires_a_real_call_shape():
    assert evaluator.find_in_text('help="reports accuracy for you"')[0] == set()
    assert evaluator.find_in_text("acc = accuracy_score(y, p)")[0] == {"accuracy"}


def test_evaluator_corroborates_only_when_the_key_search_found_nothing():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "e.py").write_text("acc = accuracy_score(y, p)\n", encoding="utf-8")
        already = CandidateCommand(argv=["python", "e.py"], source_ref="e.py:1",
                                   emits=["model_flops"], label="flops")
        evaluator.corroborate(repo, already)
        assert already.label == "flops", "never overrides an existing classification"

        blank = CandidateCommand(argv=["python", "e.py"], source_ref="e.py:1")
        evaluator.corroborate(repo, blank)
        assert blank.emits == ["accuracy"]


# --------------------------------------------------------------------------- #
# THE INTEGRATION FIX: an APT-shaped repository resolves by row-level configuration
# --------------------------------------------------------------------------- #
def _apt_shaped_repo(td: str) -> Path:
    repo = Path(td)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "r20.sh").write_text(
        "python eval_apt.py --sparsity 0.2 --base_model llama-7b\n", encoding="utf-8")
    (repo / "scripts" / "r50.sh").write_text(
        "python eval_apt.py --sparsity 0.5 --base_model llama-7b\n", encoding="utf-8")
    (repo / "eval_apt.py").write_text(
        'def evaluate():\n    acc = accuracy_score(y, p)\n    print({"accuracy": acc})\n',
        encoding="utf-8")
    return repo


def _apt_shaped_doc() -> PaperDoc:
    return PaperDoc(
        paper_id="apt", repo_url="https://example.invalid/apt",
        tables=[Table(table_idx=2, caption="Table 2: accuracy on LLaMA-7B",
                      header=["Method", "Ratio", "Acc"],
                      rows=[["APT", "20%", "61.4"], ["APT", "50%", "58.2"]])])


def test_the_regression_this_package_exists_to_fix():
    """Two candidates, both emitting accuracy, both implementing the cited method — the
    exact shape that reached `ambiguous` before this package existed. Each of the two
    cited rows now resolves to its OWN script, correctly, because the row's own sparsity
    value disambiguates them and the other script's DIFFERENT value contradicts it."""
    with tempfile.TemporaryDirectory() as td:
        repo = _apt_shaped_repo(td)
        doc = _apt_shaped_doc()

        row0 = eid.resolve_experiment(doc, repo, "T2:r0:c2")
        assert row0.state == "established"
        assert row0.command.argv == ["bash", "scripts/r20.sh"]
        assert "narrowed" in row0.reason and "sparsity=20" in row0.reason

        row1 = eid.resolve_experiment(doc, repo, "T2:r1:c2")
        assert row1.state == "established"
        assert row1.command.argv == ["bash", "scripts/r50.sh"]
        assert row1.command.argv != row0.command.argv, (
            "two different rows must resolve to two different scripts")


def test_without_a_declared_configuration_it_stays_honestly_ambiguous():
    """No sparsity anywhere in either script: nothing to disambiguate by, so the refusal
    stands — this package narrows only when the repository and the paper both say
    enough, never by inventing a preference."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "a.sh").write_text(
            "python eval_apt.py --seed 1\n", encoding="utf-8")
        (repo / "scripts" / "b.sh").write_text(
            "python eval_apt.py --seed 2\n", encoding="utf-8")
        (repo / "eval_apt.py").write_text(
            'acc = accuracy_score(y, p)\nprint({"accuracy": acc})\n', encoding="utf-8")
        doc = _apt_shaped_doc()
        ident = eid.resolve_experiment(doc, repo, "T2:r0:c2")
        assert ident.state == "ambiguous"
        assert ident.command is None


def test_two_candidates_agreeing_identically_is_still_reported_as_ambiguous():
    """Two candidates both matching the row's OWN configuration exactly — nothing
    distinguishes them, so refusing is still correct: narrowing works by ELIMINATION,
    and nothing here was eliminated."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "scripts").mkdir()
        for name in ("a.sh", "b.sh"):
            (repo / "scripts" / name).write_text(
                "python eval_apt.py --sparsity 0.2 --base_model llama-7b\n", encoding="utf-8")
        (repo / "eval_apt.py").write_text(
            'acc = accuracy_score(y, p)\nprint({"accuracy": acc})\n', encoding="utf-8")
        doc = _apt_shaped_doc()
        ident = eid.resolve_experiment(doc, repo, "T2:r0:c2")     # row asks for 20%
        assert ident.state == "ambiguous"
        assert ident.command is None


def test_a_genuine_narrowing_that_still_falls_short_of_one_is_recorded():
    """Three candidates: one contradicts the row and is eliminated, two remain agreeing
    identically. The refusal still stands, and the record says a narrowing happened
    rather than silently reproducing the pre-Step-7 sentence."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "wrong.sh").write_text(
            "python eval_apt.py --sparsity 0.9 --base_model llama-7b\n", encoding="utf-8")
        for name in ("a.sh", "b.sh"):
            (repo / "scripts" / name).write_text(
                "python eval_apt.py --sparsity 0.2 --base_model llama-7b\n", encoding="utf-8")
        (repo / "eval_apt.py").write_text(
            'acc = accuracy_score(y, p)\nprint({"accuracy": acc})\n', encoding="utf-8")
        doc = _apt_shaped_doc()
        ident = eid.resolve_experiment(doc, repo, "T2:r0:c2")     # row asks for 20%
        assert ident.state == "ambiguous"
        assert ident.command is None
        assert "narrowed 3 to 2" in ident.reason
        assert "0.9" in ident.reason, "the excluded candidate's contradiction is named"


# --------------------------------------------------------------------------- #
# THE CONSTRAINT: authorize() and identities_established are UNCHANGED
# --------------------------------------------------------------------------- #
def test_identities_established_signature_and_behaviour_are_unchanged():
    sig = inspect.signature(eid.identities_established)
    assert list(sig.parameters) == ["experiment", "metric", "configuration"]
    ok, cls, _ = eid.identities_established(None, None, None)
    assert not ok and cls == "experiment_unidentified"


def test_backends_authorize_is_untouched_by_this_package():
    import harness.backends as backends_mod

    assert not hasattr(backends_mod, "alignment")
    src = inspect.getsource(backends_mod.authorize)
    assert "alignment" not in src, (
        "authorize() must not gain a dependency on this package — every alignment "
        "module feeds evidence INTO the existing identity decision, never a new gate "
        "inside authorize() itself")


# --------------------------------------------------------------------------- #
# trial — optional, gated, same isolation floor as repository execution
# --------------------------------------------------------------------------- #
class _StubBackend(ExecutionBackend):
    name = "trial-test-stub"
    isolation = "NONE"

    def resources(self) -> BackendResources:
        return BackendResources(name=self.name, platform="linux", python="/usr/bin/python3")

    def capability(self, acq, interpreter, harness_python, flag="seed") -> ExecCapability:
        return ExecCapability(established=False, reason_code="not_attempted")

    def provision(self, cfg, root, pid, acq):
        return acq

    def execute(self, req: ExecRequest) -> ExecOutcome:
        return ExecOutcome(launched=True, completed=True, returncode=0,
                          stdout="usage: [-h] [--sparsity S]\n", argv=req.argv, backend=self.name)

    def cleanup(self, root, pid) -> list[str]:
        return []


def _cfg(allow: bool) -> Config:
    c = Config.load()
    c.allow_alignment_trial = allow
    return c


def test_trial_requires_both_the_gate_and_sufficient_isolation():
    confined = _StubBackend()
    confined.isolation = "REMOTE_SESSION"
    unconfined = _StubBackend()

    assert trial.may_trial(_cfg(False), confined) == (False, trial.may_trial(_cfg(False), confined)[1])
    assert not trial.may_trial(_cfg(False), confined)[0]
    assert not trial.may_trial(_cfg(True), unconfined)[0]
    assert trial.may_trial(_cfg(True), confined)[0]


def test_trial_never_runs_without_both_conditions_even_when_asked():
    unconfined = _StubBackend()
    cmd = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1")
    r = trial.run_trial(_cfg(True), unconfined, cwd=".", interpreter="python",
                        cmd=cmd, declared=[ArgSpec(flag="--sparsity")])
    assert isinstance(r, TrialResult) and not r.attempted


def test_trial_confirms_and_distinguishes_declared_flags():
    confined = _StubBackend()
    confined.isolation = "REMOTE_SESSION"
    cmd = CandidateCommand(argv=["python", "eval.py"], source_ref="eval.py:1")
    declared = [ArgSpec(flag="--sparsity"), ArgSpec(flag="--seed")]
    r = trial.run_trial(_cfg(True), confined, cwd=".", interpreter="python",
                        cmd=cmd, declared=declared)
    assert r.attempted and r.ran
    assert r.confirmed_flags == ["--sparsity"]
    assert r.unconfirmed_flags == ["--seed"]


def test_trial_is_optional_identity_resolution_never_requires_it():
    """The whole package's central promise: identity resolution never calls `trial`.
    Static evidence alone must be able to resolve `ambiguous`, and it did in the test
    above with the gate never even mentioned."""
    src = inspect.getsource(eid.resolve_experiment) + inspect.getsource(eid._prose_experiment)
    assert "trial" not in src
