"""A probe must never present a scraped percentage as the delta the paper claimed.

The failure this file pins down: `claimed_delta` used to be regex-scraped from a
finding's free text, and the report inferred "this was only a calibration run" from
`claimed_delta is None`. A successful scrape therefore flipped an identical-arms
calibration into something the report rendered as a reproduction of the paper, with a
"claimed delta" row whose number came from whatever percentage happened to appear
first in the finding — an expert-audit rate, a wall-clock figure, anything.

Two rules make that unrepresentable:

  1. No script (`spec.script == ""`) means no claim under test. `claimed_delta` is
     None and the run is flagged `calibration`, whatever the finding text says.
  2. With a script, a delta is only accepted when it is GROUNDED: supplied outright by
     the driver in `spec.json`, or read off an addressed table cell's own
     `QuantFinding.delta`. Prose is never mined.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness import state
from harness.artifacts import (ArmStats, Finding, PaperDoc, ProbeResult, QuantFinding,
                               Table)
from harness.config import Config
from harness.stages import probe as probe_stage
from harness.stages.report import _probe_block

PID = "grounding"
CELL = "T1:r0:c1"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path)


def make_doc(delta: str = "", table_ref: str = CELL) -> PaperDoc:
    """A one-number paper. `delta` empty models a cell with no reported improvement.

    The table has to be real: findings citing a cell are re-verified against it by
    `audit.load_reports`, and an unsubstantiated finding never reaches `build_spec`.
    T1:r0:c1 is the value cell, so `tables[1].rows[0][1]` must read "59.28".
    """
    return PaperDoc(
        paper_id=PID, title="t", n_pages=1,
        tables=[Table(table_idx=0, page=1), Table(
            table_idx=1, page=6, caption="Table 1", header=["method", "acc"],
            rows=[["ours", "59.28"]])],
        reported_numbers=[QuantFinding(
            benchmark="b", metric="accuracy", method="ours", value="59.28",
            delta=delta, table_ref=table_ref, source_quote="59.28", page=6)],
    )


def write_lens(cfg: Config, evidence_ref: str, statement: str) -> None:
    """One verifiable finding, so build_spec has a target to aim at."""
    state.write_json(
        state.project_dir(cfg, PID) / "audit" / "overclaim.json",
        {"lens": "overclaim", "findings": [{
            "finding_id": "overclaim-01", "severity": "MAJOR", "title": "t",
            "statement": statement, "target": statement,
            "evidence_quote": "59.28", "evidence_ref": evidence_ref,
            "verifiable_by_experiment": True}],
            "unasked_question": "", "notes": ""})


def write_spec(cfg: Config, **fields) -> None:
    state.write_json(state.project_dir(cfg, PID) / "runs" / PID / "spec.json",
                     {"paper_id": PID, **fields})


# --------------------------------------------------------------------------- #
# 1. Calibration runs are decoupled from claim scrapes
# --------------------------------------------------------------------------- #
def test_calibration_probe_does_not_scrape_a_percentage_from_the_finding(cfg: Config):
    """The exact regression: '80.5%' in a finding must not become a claimed delta."""
    doc = make_doc()
    write_lens(cfg, CELL, "expert audit records 80.5%(161) No Error for GPT-5-mini")
    spec = probe_stage.build_spec(cfg, PID, doc)

    assert spec.script == "", "no override was written, so this is the default template"
    assert spec.claimed_delta is None, "a calibration run has no claim under test"
    assert spec.finding_id == "overclaim-01", "it still targets the finding it probed"


def test_a_stale_spec_with_no_script_has_its_claimed_delta_cleared(cfg: Config):
    """Specs written by the OLD scraper are on disk. Loading one must not resurrect it."""
    write_lens(cfg, CELL, "irrelevant")
    write_spec(cfg, script="", claimed_delta=0.805, finding_id="overclaim-02")
    spec = probe_stage.build_spec(cfg, PID, make_doc())

    assert spec.claimed_delta is None, "an empty script cannot have a claim under test"


def test_prose_percentages_are_never_mined_even_with_a_script(cfg: Config):
    """A page-referenced finding is prose. Prose is not a grounded source for a delta."""
    write_lens(cfg, "p8", "achieved a 7.38% performance improvement")
    write_spec(cfg, script="print('SH_DEVICE cpu')\n")
    spec = probe_stage.build_spec(cfg, PID, make_doc(delta="+7.38%", table_ref=CELL))

    assert spec.claimed_delta is None, "the finding cites p8, not the cell that owns +7.38%"


# --------------------------------------------------------------------------- #
# 2. Strict grounding for targeted probes
# --------------------------------------------------------------------------- #
def test_a_scripted_spec_grounds_its_delta_from_the_addressed_cell(cfg: Config):
    write_lens(cfg, CELL, "the headline gain")
    write_spec(cfg, script="print('SH_DEVICE cpu')\n")
    spec = probe_stage.build_spec(cfg, PID, make_doc(delta="+4.2%"))

    assert spec.claimed_delta == 0.042, "QuantFinding.delta on the cited cell is grounded"


def test_a_driver_supplied_delta_is_honoured_verbatim(cfg: Config):
    """CLAUDE.md documents `{'script': ..., 'claimed_delta': 0.042}`. Keep that contract."""
    write_lens(cfg, CELL, "the headline gain")
    write_spec(cfg, script="print('SH_DEVICE cpu')\n", claimed_delta=0.042)
    spec = probe_stage.build_spec(cfg, PID, make_doc(delta="+9.9%"))

    assert spec.claimed_delta == 0.042, "an explicit driver value outranks the cell lookup"


def test_a_cell_with_no_reported_delta_grounds_nothing(cfg: Config):
    write_lens(cfg, CELL, "the headline gain")
    write_spec(cfg, script="print('SH_DEVICE cpu')\n")
    spec = probe_stage.build_spec(cfg, PID, make_doc(delta=""))

    assert spec.claimed_delta is None, "no delta printed for that cell means no claim to test"


def test_a_bare_unitless_delta_is_refused_rather_than_guessed(cfg: Config):
    """'+0.5' could be half a point or fifty. Guessing is the thing this harness bans."""
    write_lens(cfg, CELL, "the headline gain")
    write_spec(cfg, script="print('SH_DEVICE cpu')\n")
    spec = probe_stage.build_spec(cfg, PID, make_doc(delta="+0.5"))

    assert spec.claimed_delta is None, "only %/pp deltas carry an unambiguous scale"


def test_grounded_delta_helper_reads_percent_and_pp(cfg: Config):
    doc = make_doc(delta="+2.1 pp")
    f = Finding(finding_id="x", evidence_ref=CELL)
    assert probe_stage.grounded_claimed_delta(doc, f) == 0.021

    f_prose = Finding(finding_id="x", evidence_ref="p7")
    assert probe_stage.grounded_claimed_delta(doc, f_prose) is None
    assert probe_stage.grounded_claimed_delta(doc, None) is None


# --------------------------------------------------------------------------- #
# 3. The result carries the flag, and the report renders it honestly
# --------------------------------------------------------------------------- #
def _result(*, calibration: bool, claimed: float | None) -> ProbeResult:
    return ProbeResult(
        paper_id=PID, finding_id="overclaim-01", device="cuda",
        seeds_run=[0, 1, 2, 3, 4], arms={
            "baseline": ArmStats(values=[0.9684] * 5, mean=0.9684, std=0.0048, n=5),
            "treatment": ArmStats(values=[0.9684] * 5, mean=0.9684, std=0.0048, n=5)},
        measured_delta=0.0, measured_std=0.004818, noise_band=0.009635,
        calibration=calibration, claimed_delta=claimed, is_overclaimed=True,
        verdict="calibration" if calibration else "within_noise",
        reason="measured delta +0.0000 vs 2-sigma noise band 0.0096", seconds=57.0,
    )


def test_report_renders_the_requested_calibration_line(cfg: Config):
    body = "\n".join(_probe_block(_result(calibration=True, claimed=None)))

    assert "⚪ **Hardware Noise-Floor Calibration** (σ = 0.0048, 2σ = 0.0096) — " \
           "No paper-specific script evaluated." in body
    assert "claimed delta" not in body, "a calibration run has no claimed delta to show"
    assert "🔴" not in body, "a noise floor is not a red flag against the paper"
    assert "seed noise (1σ) | 0.0048" in body


def test_a_scraped_delta_can_no_longer_dress_calibration_up_as_a_reproduction(cfg: Config):
    """Belt and braces: even handed a delta, a calibration run must not claim more."""
    body = "\n".join(_probe_block(_result(calibration=True, claimed=0.805)))

    assert "Hardware Noise-Floor Calibration" in body
    assert "The measured effect sits inside the noise band" not in body


def test_a_real_reproduction_still_gets_its_verdict_headline(cfg: Config):
    body = "\n".join(_probe_block(_result(calibration=False, claimed=0.042)))

    assert "🔴 **The measured effect sits inside the noise band.**" in body
    assert "| **claimed delta** | **+0.0420** |" in body
    assert "Hardware Noise-Floor Calibration" not in body


def test_legacy_results_without_the_flag_fall_back_to_the_old_inference(cfg: Config):
    """probe_results.json written before this change has no `calibration` key."""
    legacy = ProbeResult(**{k: v for k, v in _result(calibration=True, claimed=None)
                            .model_dump().items() if k != "calibration"})
    assert legacy.calibration is None
    assert "Hardware Noise-Floor Calibration" in "\n".join(_probe_block(legacy))
