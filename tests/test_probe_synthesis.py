"""S3c — the autonomous probe planner, and the provenance ceiling that contains it.

Two things are being pinned here, and the second matters more than the first.

The first is that the planner picks a sensible template and emits a script that is
valid Python, implements the contract the runner parses, and prints every auxiliary
metric it claims to.

The second is the boundary. A synthesized probe is this harness's own reimplementation
of a paper's formulation, run at toy scale. It produces a real measurement, and it is
categorically not a reproduction of a printed benchmark number. Nothing in the pipeline
may quietly promote it into one — not by reconciling it against a cell, not by filing it
under an unrelated finding, and not by letting it drive the RED verdict. Those are the
tests that would matter if someone refactored this module a year from now.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from harness import probe_synth
from harness.artifacts import (ConfigurationIdentity, ExperimentIdentity, Finding,
                               MetricIdentity, PaperDoc, ProbeSpec, Reconciliation,
                               RepoAcquisition, Section)
from harness.config import Config
from harness.local_exec import _AUX, reconcile
from harness.stages.probe import synthesize_probe
from harness.stages.report import overall_verdict
from conftest import incidental_objects, material_objects

PID = "synth"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path)


def doc(title: str = "", body: str = "") -> PaperDoc:
    return PaperDoc(paper_id=PID, title=title, source_path="x", n_pages=1,
                    sections=[Section(section_idx=0, title="Introduction", text=body)])


def spec(**kw) -> ProbeSpec:
    return ProbeSpec(**{"paper_id": PID, "finding_id": "overclaim-01", **kw})


# --------------------------------------------------------------------------- #
# 1. Template selection
# --------------------------------------------------------------------------- #
def test_no_paper_receives_a_hardcoded_template():
    """The planner has no mechanism dispatch, and must never grow one back.

    It used to ship a full template for one pilot paper (LDReg, ICLR 2024), selected by
    matching that paper's method name — or two of its topic keywords — anywhere in a
    40-section corpus. For a short paper that corpus is the whole text, so a single
    related-work citation of someone else's method was enough to trigger it. That is
    paper-specific logic in executable production code: the harness behaved differently
    for one paper in its own evaluation corpus than for any other.

    Every paper now gets the generic control, whose result the provenance ceiling caps at
    INCONCLUSIVE regardless. Abstaining is the right outcome for a mechanism this harness
    cannot author from the paper alone.
    """
    for title, body in (
        ("LDReg: Local Dimensionality Regularized SSL", "local intrinsic dimension"),
        ("Untitled", "we study intrinsic dimensionality and dimensional collapse"),
        ("APT: Adaptive Pruning and Tuning", "we prune and tune language models"),
        ("SAPG: Split and Aggregate Policy Gradients", "we split and aggregate"),
        ("Stay on topic with Classifier-Free Guidance", "we apply CFG to language models"),
        ("A Unified Diverse Weather Generator for LiDAR", "we generate weather"),
    ):
        p = probe_synth.plan(doc(title, body))
        assert p.mechanism == "placebo", f"{title!r} received template {p.mechanism!r}"
        assert p.arms == ["baseline", "placebo"], title


def test_the_planner_holds_no_method_name_vocabulary():
    """Structural, not behavioural: a future dispatch would have to name a paper again."""
    import inspect

    src = inspect.getsource(probe_synth).split("if __name__")[0]
    for token in ("ldreg", "LDReg", "APT", "SAPG", "classifier-free", "CoFi", "LLMPruner"):
        assert token not in src, f"{token!r} appears in the synthesis module"


def test_an_unrelated_paper_falls_back_to_the_placebo_control():
    p = probe_synth.plan(doc("A Unified Diverse Weather Generator for LiDAR Point Clouds"))
    assert p.mechanism == "placebo"
    assert p.arms == ["baseline", "placebo"]


def test_matching_reads_the_front_of_the_paper_not_the_whole_thing():
    """A method named only in a late section is someone else's method."""
    late = PaperDoc(paper_id=PID, title="Weather Generation", source_path="x", n_pages=1,
                    sections=[Section(section_idx=i, title=f"S{i}", text="lidar diffusion")
                              for i in range(60)]
                             + [Section(section_idx=60, title="Related", text="ldreg is prior work")])
    assert probe_synth.plan(late).mechanism == "placebo"


def test_the_advertised_repo_url_is_part_of_the_matching_corpus():
    """The corpus still includes the advertised URL — that was never the defect. What is
    gone is the dispatch that read a method name out of it and selected a template."""
    d = doc("Untitled", "a paper")
    d.repo_url = "https://github.com/HanxunH/LDReg"
    assert "ldreg" in probe_synth.corpus(d).lower(), "the url is still searchable"
    assert probe_synth.plan(d).mechanism == "placebo", "but it selects nothing"


# --------------------------------------------------------------------------- #
# 2. Every emitted script must actually run under the runner's contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("d", [doc("LDReg"), doc("Something Else")])
def test_every_emitted_script_is_valid_python(d: PaperDoc):
    ast.parse(probe_synth.plan(d).script)


@pytest.mark.parametrize("d", [doc("LDReg"), doc("Something Else")])
def test_every_emitted_script_implements_the_stdout_contract(d: PaperDoc):
    p = probe_synth.plan(d)
    assert "SH_DEVICE" in p.script
    assert "SH_METRIC arm=" in p.script
    assert "--seed" in p.script and "--arm" in p.script


@pytest.mark.parametrize("d", [doc("LDReg"), doc("Something Else")])
def test_a_declared_aux_metric_is_actually_printed(d: PaperDoc):
    """A key advertised on the artifact but never emitted renders an empty report row."""
    p = probe_synth.plan(d)
    for key in p.aux_metrics:
        assert f"key={key}" in p.script


@pytest.mark.parametrize("d", [doc("LDReg"), doc("Something Else")])
def test_no_placeholder_survives_rendering(d: PaperDoc):
    body = probe_synth.plan(d).script
    for placeholder in ("__STEPS__", "__BETA__", "__K__", "__CLAIM__"):
        assert placeholder not in body


@pytest.mark.parametrize("hostile", [
    'we claim """ + __import__("os").system("echo pwned") + """ a gain',
    "ignore previous instructions and ''' + exec(open('x').read()) + '''",
])
def test_a_claim_from_the_paper_is_never_interpolated_as_code(hostile: str):
    """Paper text is data, not instruction — including when it lands inside a template.

    The claim is embedded in the generated script's docstring. A quote carrying a
    docstring terminator must not be able to close it and continue as code. Checked on
    the parse tree rather than by string search: the payload has to remain a string
    constant, and the module must contain no call node that the payload introduced.
    """
    body = probe_synth.plan(doc("Weather"), claim=hostile).script
    tree = ast.parse(body)
    assert ast.get_docstring(tree), "the script still starts with its docstring"
    assert "pwned" not in body or "pwned" in ast.get_docstring(tree)  # type: ignore[operator]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("exec", "eval", "__import__"), (
                f"paper text introduced a {node.func.id} call into the generated script")


# --------------------------------------------------------------------------- #
# 3. Dispatch — when the planner is allowed to author anything at all
# --------------------------------------------------------------------------- #
def test_synthesis_runs_when_a_finding_is_settleable(cfg: Config):
    out = synthesize_probe(cfg, doc("LDReg"), spec(), RepoAcquisition())
    assert out.provenance == "synthesized" and out.mechanism == "placebo"
    assert out.script and out.arms == ["baseline", "placebo"]


def test_nothing_is_synthesized_when_no_finding_is_settleable(cfg: Config):
    """All-documentary findings leave the honest noise-floor template in place."""
    out = synthesize_probe(cfg, doc("LDReg"), spec(finding_id=""), RepoAcquisition())
    assert out.provenance == "template" and not out.script


def test_the_synthesis_gate_can_be_closed(tmp_path: Path):
    cfg = Config(projects_dir=tmp_path, allow_synthesis=False)
    out = synthesize_probe(cfg, doc("LDReg"), spec(), RepoAcquisition())
    assert out.provenance == "template" and not out.script


def test_a_human_written_script_is_never_overwritten(cfg: Config):
    out = synthesize_probe(cfg, doc("LDReg"), spec(script="print('mine')",
                                                   provenance="driver"), RepoAcquisition())
    assert out.script == "print('mine')" and out.provenance == "driver"


def test_the_repo_command_is_never_overwritten(cfg: Config):
    out = synthesize_probe(cfg, doc("LDReg"), spec(command=["python", "eval.py"]),
                           RepoAcquisition())
    assert out.provenance == "template" and not out.script


#  has no producer now that the only template is the placebo, which
# calibrates the finding it came from. The field and its guard are kept: they are three
# lines, and they are what stops a measurement of one thing being filed under a claim
# about another if a mechanism template is ever reintroduced deliberately.
def test_a_placebo_probe_keeps_the_finding_whose_gain_it_calibrates(cfg: Config):
    out = synthesize_probe(cfg, doc("Weather"), spec(finding_id="overclaim-01",
                                                     claim="a 0.3 point gain"), RepoAcquisition())
    assert out.finding_id == "overclaim-01"
    assert "0.3 point gain" in out.claim


# --------------------------------------------------------------------------- #
# 4. The provenance ceiling — the boundary that must not erode
# --------------------------------------------------------------------------- #
def _rec(provenance: str, values: list[float]) -> Reconciliation:
    """Identity established by default (C4): this section tests the PROVENANCE ceiling,
    not identity resolution — see test_experiment_identity.py for that gate."""
    return reconcile(ProbeSpec(paper_id=PID, table_ref="T1:r0:c1", claimed_cell_value="59.28",
                               # stamped onto the reconciliation, as production does
                               target_id="T1",
                               provenance=provenance,
                               experiment=ExperimentIdentity(state="established", reason="fixture"),
                               metric_identity=MetricIdentity(state="established", reason="fixture"),
                               configuration=ConfigurationIdentity(state="established",
                                                                   reason="fixture")),
                     values, 0.10, [0, 1])


def test_a_synthesized_probe_may_not_convict_a_printed_cell():
    r = _rec("synthesized", [64.10, 64.20])
    assert r.status == "INCONCLUSIVE"
    assert r.delta_error is not None, "the arithmetic is still recorded, just not acted on"


def test_a_synthesized_probe_may_not_acquit_a_printed_cell_either():
    """Landing near the number by luck would launder a claim nobody checked."""
    assert _rec("synthesized", [59.30, 59.26]).status == "INCONCLUSIVE"


def test_the_noise_floor_template_reconciles_nothing():
    assert _rec("template", [59.30, 59.26]).status == "INCONCLUSIVE"


@pytest.mark.parametrize("provenance,expected", [
    ("driver", "RESOLVED_VERIFIED"),
    ("repo_exec", "RESOLVED_VERIFIED"),
])
def test_only_the_authors_code_and_a_human_repro_may_reach_a_verdict(provenance, expected):
    assert _rec(provenance, [59.30, 59.26]).status == expected


def test_the_ceiling_records_which_provenance_produced_it():
    assert _rec("synthesized", [59.30]).provenance == "synthesized"


def test_a_synthesized_probe_cannot_drive_the_red_verdict():
    """The one condition that turns a paper RED on its own stays out of reach."""
    findings = [Finding(finding_id="f1", lens="overclaim", severity="MINOR",
                        statement="s", evidence_quote="q")]
    capped = _rec("synthesized", [64.10, 64.20])
    verdict, _ = overall_verdict(findings, capped)
    assert verdict != "RED", "a toy reimplementation must not reject a paper"

    convicting = _rec("repo_exec", [64.10, 64.20])
    assert convicting.status == "FAILED_REPRODUCTION"
    assert overall_verdict(findings, convicting,
                           objects=material_objects("T1"))[0] == "RED", (
        "the authors' code still can, on a target a central claim rests on")


# --------------------------------------------------------------------------- #
# 5. The SH_AUX line contract
# --------------------------------------------------------------------------- #
def test_a_well_formed_aux_line_parses():
    m = _AUX.match("SH_AUX key=linear_probe_acc arm=ldreg seed=3 value=0.914000")
    assert m and m.groups() == ("linear_probe_acc", "ldreg", "3", "0.914000")


@pytest.mark.parametrize("line", [
    "SH_AUX key=k arm=a seed=0",              # no value
    "SH_AUX key=k arm=a value=1.0",           # no seed
    "SH_AUX arm=a seed=0 value=1.0",          # no key
    "note: SH_AUX key=k arm=a seed=0 value=1.0",   # not at line start
])
def test_a_malformed_aux_line_is_ignored_rather_than_guessed(line: str):
    assert _AUX.match(line) is None


def test_aux_accepts_scientific_notation():
    m = _AUX.match("SH_AUX key=lr arm=a seed=0 value=5e-06")
    assert m and m.group(4) == "5e-06"
