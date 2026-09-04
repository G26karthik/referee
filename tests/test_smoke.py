"""Step 0 — the suite imports, and the pipeline is wired end to end.

`single-harness` is not an installed package; this only passes because `conftest.py`
puts the harness root on sys.path.

The invariant worth pinning here is that the HARNESS holds no credential of its own: no
Agent SDK, no cloud client, no key in any module. Two things do reach the network, and
neither is a key this repository stores — the read-only shallow clone in S3a, and, only
under `--auto-audit`, a reviewer subprocess that authenticates itself.
"""
from __future__ import annotations

import importlib

from harness import local_exec, pdf
from harness.artifacts import (
    ArmStats, EvalReport, Finding, LensReport, PaperDoc, ProbeResult, ProbeSpec,
    QuantFinding, Table,
)
from harness.config import Config
from harness.prompts import audit as audit_prompts
from harness.stages import audit as audit_stage


def test_every_cli_stage_exists_and_is_callable():
    import run

    assert sorted(run.STAGES) == ["audit", "grade", "ingest", "probe", "report"]
    for fn in run.STAGES.values():
        assert callable(fn)


def test_config_loads_without_any_credential():
    """`Config.load()` cannot fail for want of a token, because it never wants one."""
    cfg = Config.load()
    assert cfg.projects_dir.exists()
    assert cfg.python and cfg.seeds >= 1


def test_no_module_depends_on_the_agent_sdk_or_modal():
    for name in ("harness.config", "harness.controller", "harness.local_exec", "harness.pdf",
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
        assert audit_prompts.GRADING in prompt, "missing severity-earned-by-impact rule"
        assert audit_prompts.TWO_PASS in prompt, "missing discovery/verification discipline"
        assert audit_prompts.RECOMPUTE in prompt, "missing recompute-before-claiming rule"
        assert audit_prompts.FIRST_PRINCIPLES in prompt, "missing first-principles check"
        assert '"schema_version": 2' in prompt
        assert f'"lens": "{lens}"' in prompt


def test_no_lens_prompt_carries_a_mechanical_severity_floor():
    """The old CALIBRATION block hard-coded rules like "no variance = MAJOR" — exactly
    what the reviewer spec forbids ("Do not use simplistic rules such as: 'No standard
    deviation = MAJOR.'"). GRADING replaces every one of them with an impact judgement;
    this pins that none crept back in."""
    banned = ("that is a FATAL finding", "that is a MAJOR finding", "that is at least MAJOR")
    for lens in audit_prompts.LENSES:
        prompt = audit_prompts.build(lens, "T", "sections", "tables", "claims", "numbers")
        for phrase in banned:
            assert phrase not in prompt, f"{lens}: mechanical severity floor reappeared: {phrase!r}"


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
