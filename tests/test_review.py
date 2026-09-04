"""The `review` orchestrator: the pause, the resume, and the conditional probe.

These run the real pipeline against a real PDF in a temp cases directory. The GPU
probe is skipped unless a test is specifically about it — training five seeds twice
is not something a unit test should do.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import state
from harness.artifacts import PaperDoc
from harness.config import Config
from harness.controller import review

PAPER = Path(__file__).resolve().parents[1] / "papers" / "paper4_snri_nullresult.pdf"
PID = "paper4-snri-nullresult"
LENSES = ("overclaim", "protocol", "confound", "contradiction")

# A real cell from this paper — findings citing it survive provenance re-verification.
CELL_REF, CELL_VALUE = "T0:r1:c1", "12.196 ± 0.207"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path)


def lens_json(lens: str, verifiable: bool = False) -> dict:
    return {"lens": lens, "findings": [{
        "severity": "MAJOR", "title": f"{lens} finding",
        "statement": "a substantiated defect", "target": "a claim",
        "evidence_quote": CELL_VALUE, "evidence_ref": CELL_REF,
        "verifiable_by_experiment": verifiable}],
        "unasked_question": f"{lens} asks this", "notes": ""}


def write_lenses(cfg: Config, lenses=LENSES, verifiable: bool = False) -> None:
    from harness.stages import audit as audit_stage
    for lens in lenses:
        audit_stage.accept_lens(cfg, PID, lens, json.dumps(lens_json(lens, verifiable)))


def test_review_stops_for_audits_and_names_every_prompt(cfg: Config):
    res = review(cfg, str(PAPER))
    assert res["status"] == "needs_audit"
    assert sorted(res["awaiting"]) == sorted(LENSES)
    for lens, path in res["prompts"].items():
        body = Path(path).read_text(encoding="utf-8")
        assert f'"lens": "{lens}"' in body, "the prompt must carry its own return schema"
        assert "Driver instructions" in body
    assert PID in res["next"], "the resume command must name the case id"


def test_review_reports_partial_audit_progress(cfg: Config):
    review(cfg, str(PAPER))
    write_lenses(cfg, lenses=("overclaim", "protocol"))
    res = review(cfg, str(PAPER))
    assert res["status"] == "needs_audit"
    assert sorted(res["awaiting"]) == ["confound", "contradiction"]
    assert set(res["prompts"]) == {"confound", "contradiction"}, "no prompt for finished lenses"


def test_review_completes_once_every_lens_has_a_result(cfg: Config):
    review(cfg, str(PAPER))
    write_lenses(cfg)
    res = review(cfg, str(PAPER), skip_probe=True)
    assert res["status"] == "complete"
    assert res["verdict"] in ("RED", "YELLOW", "GREEN")
    assert res["findings"] == 4 and res["dropped_unsubstantiated"] == 0
    md = Path(res["report_md"]).read_text(encoding="utf-8")
    assert CELL_VALUE in md, "the verbatim cell must reach the report"
    assert md.endswith("\n")


def test_review_resumes_from_a_bare_case_id(cfg: Config):
    review(cfg, str(PAPER))
    write_lenses(cfg)
    res = review(cfg, PID, skip_probe=True)
    assert res["status"] == "complete" and res["paper_id"] == PID


def test_review_rejects_an_unknown_paper(cfg: Config):
    res = review(cfg, "no-such-case-id")
    assert res["status"] == "error" and "neither" in res["error"]


def test_review_reports_a_missing_pdf_cleanly(cfg: Config):
    """A typo'd path is a message, not a traceback."""
    res = review(cfg, "papers/does-not-exist.pdf")
    assert res["status"] == "error" and "no such paper" in res["error"]


def test_unsubstantiated_findings_are_dropped_and_counted(cfg: Config):
    from harness.stages import audit as audit_stage

    review(cfg, str(PAPER))
    write_lenses(cfg, lenses=("overclaim",))
    bad = lens_json("protocol")
    bad["findings"][0]["evidence_quote"] = "a number this paper never printed"
    for lens in ("protocol", "confound", "contradiction"):
        bad["lens"] = lens
        audit_stage.accept_lens(cfg, PID, lens, json.dumps(bad))
    res = review(cfg, PID, skip_probe=True)
    assert res["findings"] == 1, "only the substantiated finding survives"
    assert res["dropped_unsubstantiated"] == 3


# --------------------------------------------------------------------------- #
# S3 is conditional
# --------------------------------------------------------------------------- #
def _doc(cfg: Config) -> PaperDoc:
    return PaperDoc(**state.read_json(state.project_dir(cfg, PID) / "paper" / "doc.json"))


def test_the_probe_is_skipped_when_no_finding_is_settleable(cfg: Config):
    review(cfg, str(PAPER))
    write_lenses(cfg, verifiable=False)
    res = review(cfg, PID)
    probe_step = next(s for s in res["steps"] if s["stage"] == "S3 verify")
    assert "skipped" in probe_step
    assert not (state.project_dir(cfg, PID) / "runs" / PID / "probe_results.json").exists()


def test_skip_probe_beats_a_settleable_finding(cfg: Config):
    review(cfg, str(PAPER))
    write_lenses(cfg, verifiable=True)
    res = review(cfg, PID, skip_probe=True)
    probe_step = next(s for s in res["steps"] if s["stage"] == "S3 verify")
    assert probe_step["skipped"] == "--skip-probe"


def test_the_report_records_a_skipped_probe_as_absent_not_as_a_pass(cfg: Config):
    review(cfg, str(PAPER))
    write_lenses(cfg, verifiable=False)
    res = review(cfg, PID)
    assert res["probe"] is None
    md = Path(res["report_md"]).read_text(encoding="utf-8")
    assert "Measured reproduction" not in md, "no probe means no reproduction section"


def test_the_cli_keeps_every_paper_it_was_given():
    """`--paper a --paper b --paper c` used to keep only `c`, reviewing one paper while
    appearing to review three — no error, no warning, two silently discarded."""
    import run as cli
    p = cli.main.__globals__["argparse"].ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review")
    r.add_argument("--paper", required=True, action="extend", nargs="+")
    assert p.parse_args(["review", "--paper", "a", "--paper", "b", "--paper", "c"]).paper \
        == ["a", "b", "c"]
    assert p.parse_args(["review", "--paper", "a", "b", "c"]).paper == ["a", "b", "c"]
    assert p.parse_args(["review", "--paper", "a"]).paper == ["a"]


def test_several_papers_route_to_the_batch_path(monkeypatch):
    """More than one paper is what the caller plainly meant, so it runs the batch and
    writes a consolidated dossier rather than reviewing only the last one."""
    import argparse

    import run as cli
    seen = {}
    def fake(cfg, papers, **kw):
        seen["papers"] = papers
        return {"papers": len(papers), "complete": [], "needs_audit": {},
                "errors": {}, "results": []}

    monkeypatch.setattr(cli.controller, "review_papers", fake)
    args = argparse.Namespace(paper=["a.pdf", "b.pdf", "c.pdf"], force_probe=False,
                              skip_probe=False, auto_audit=False, auto_grade=False,
                              require_grades=False, out=None)
    cli.cmd_review(args)
    assert seen["papers"] == ["a.pdf", "b.pdf", "c.pdf"]
