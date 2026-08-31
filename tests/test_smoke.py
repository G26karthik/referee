"""Step 0 — the suite imports, and the local pipeline is wired end to end.

`single-harness` is not an installed package; this only passes because `conftest.py`
puts the harness root on sys.path. Nothing here opens a network connection, and
nothing needs a credential — that is the point of the local build.
"""
from __future__ import annotations

import importlib

from harness import local_exec, nodes, pdf
from harness.artifacts import (
    ArmStats, Claim, EvalReport, Finding, LensReport, PaperDoc, ProbeResult, ProbeSpec,
    QuantFinding, Table,
)
from harness.config import Config
from harness.prompts import audit as audit_prompts
from harness.stages import audit as audit_stage


def test_every_cli_node_exists_and_is_callable():
    import run

    for name in run.NODES:
        assert callable(getattr(nodes, name)), f"run.py advertises node '{name}' but it is missing"


def test_the_four_cli_nodes_are_the_documented_ones():
    import run

    assert run.NODES == ("ingest_paper", "audit_paper", "run_probe", "synthesize_report")


def test_config_loads_without_any_credential():
    """The whole point of the local build: no token, no key, no cloud account."""
    cfg = Config.load()
    assert cfg.projects_dir.exists()
    assert cfg.python and cfg.seeds >= 1


def test_no_module_depends_on_the_agent_sdk_or_modal():
    for name in ("harness.config", "harness.nodes", "harness.local_exec", "harness.pdf",
                 "harness.stages.ingest", "harness.stages.audit",
                 "harness.stages.probe", "harness.stages.report"):
        src = importlib.import_module(name).__file__
        assert src
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        assert "claude_agent_sdk" not in text, f"{name} still imports the Agent SDK"
        assert "import modal" not in text, f"{name} still imports modal"
        assert "ANTHROPIC_API_KEY" not in text and "OAUTH" not in text.upper(), \
            f"{name} still references a credential"


def test_the_panel_is_exactly_the_four_lenses():
    assert set(audit_prompts.LENSES) == {"overclaim", "protocol", "confound", "contradiction"}
    assert audit_stage.LENSES == tuple(audit_prompts.LENSES)


def test_every_lens_prompt_carries_the_shared_discipline():
    for lens in audit_prompts.LENSES:
        prompt = audit_prompts.build(lens, "T", "sections", "tables", "claims", "numbers")
        assert audit_prompts.SECURITY in prompt, "missing prompt-injection guard"
        assert audit_prompts.PROVENANCE in prompt, "missing provenance rule"
        assert audit_prompts.CALIBRATION in prompt, "missing severity calibration"
        assert audit_prompts.FIRST_PRINCIPLES in prompt, "missing first-principles check"
        assert f'"lens": "{lens}"' in prompt


def test_lens_prompt_embeds_the_paper_rather_than_a_path():
    prompt = audit_prompts.build("protocol", "T", "SECTION-BODY", "TABLE-BODY", "C", "N")
    assert "SECTION-BODY" in prompt and "TABLE-BODY" in prompt


def test_artifacts_degrade_instead_of_raising_on_partial_input():
    assert PaperDoc(paper_id="p").title == ""
    assert LensReport(lens="protocol").findings == []
    assert EvalReport(paper_id="p").verdict == ""
    assert EvalReport(paper_id="p").probe is None
    assert Finding().severity == "MINOR"
    assert QuantFinding().page == 0
    assert Claim(claim_id="c1").page == 0
    assert ProbeSpec(paper_id="p").seeds == [0, 1, 2, 3, 4]
    assert ProbeResult(paper_id="p").is_overclaimed is None
    assert ArmStats().n == 0


def test_artifacts_accept_unknown_fields():
    """extra="allow" is what lets a stage add a field without breaking the pipeline."""
    assert Finding(surprise="value").surprise == "value"


def test_table_cell_addressing_round_trips():
    t = Table(table_idx=2, rows=[["a", "b"], ["c", "d"]])
    assert t.ref(1, 0) == "T2:r1:c0"
    assert t.cell(1, 0) == "c"
    assert t.cell(9, 9) == "", "out-of-range must return empty, not raise"


def test_pdf_module_exposes_the_pure_helpers():
    for fn in ("page_texts", "split_sections", "extract_tables", "guess_title",
               "render_tables", "render_sections", "is_heading",
               "table_numbers", "prose_numbers"):
        assert callable(getattr(pdf, fn))


def test_probe_stdout_contract_is_parsed():
    m = local_exec._METRIC.match("SH_METRIC arm=baseline seed=3 value=0.9412")
    assert m and m.group(1) == "baseline" and m.group(2) == "3" and m.group(3) == "0.9412"
    assert local_exec._DEVICE.match("SH_DEVICE cuda").group(1) == "cuda"
    assert local_exec._METRIC.match("some other log line") is None


def test_default_probe_template_renders_to_valid_python():
    body = local_exec.render_default(
        ProbeSpec(paper_id="p", claim='a claim with """ triple quotes', epochs=5))
    compile(body, "probe.py", "exec")
    assert "SH_METRIC" in body and "SH_DEVICE" in body
    assert "__" not in body.replace("__main__", ""), "an unsubstituted placeholder remains"


def test_default_probe_uses_the_gpu_when_torch_is_present():
    body = local_exec.render_default(ProbeSpec(paper_id="p"))
    assert "torch.cuda.is_available()" in body
    assert ".to(DEVICE)" in body, "the model must actually move to the detected device"
