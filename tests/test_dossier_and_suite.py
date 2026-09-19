"""Tests for the batch entrypoint, the optional S2 autonomy hook, and the dossier.

The property under test throughout is that automation did not buy convenience with
honesty: a paper that did not finish must be named rather than omitted, a probe that is
not entitled to a reproduction verdict must still carry its caveat once it reaches an
executive summary, and an auto-filled lens must be refused when it is not a real lens
report rather than degrading into an empty findings list.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness import audit_driver, dossier
from harness.audit_driver import AuditDriverError
from harness.config import Config


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _report(pid: str, verdict: str = "RED", *, fatal: int = 0, major: int = 1,
            minor: int = 1, provenance: str = "synthesized",
            reconciliation: str | None = "INCONCLUSIVE") -> dict:
    findings = []
    for severity, n in (("FATAL", fatal), ("MAJOR", major), ("MINOR", minor)):
        for i in range(n):
            findings.append({
                "finding_id": f"{severity.lower()}-{i:02d}", "lens": "overclaim",
                "severity": severity, "title": f"{severity} title {i}",
                "statement": f"{severity} statement {i}",
                "evidence_quote": f"quote-{severity}-{i}", "evidence_ref": "T1:r0:c1",
            })
    return {
        "paper_id": pid, "title": f"Title of {pid}", "verdict": verdict,
        "verdict_reason": "because the threshold table said so",
        "n_pages": 9, "n_sections": 7, "n_tables": 3, "n_numbers": 12,
        "lenses_run": ["overclaim", "protocol", "confound", "contradiction"],
        "dropped_findings": 0, "unasked_question": f"the question {pid} avoided",
        "findings": findings,
        "probe": {
            "verdict": "within_noise", "provenance": provenance, "device": "cuda",
            "seeds_run": [0, 1, 2, 3, 4], "seeds_failed": [], "measured_std": 0.0048,
            "noise_band": 0.0096, "measured_delta": 0.0012, "claimed_delta": 0.042,
            "claim_within_noise": False, "seconds": 61.0,
            "repo": {"status": "cloned"}, "code_audit": {"findings": []},
            "reconciliation": {"status": reconciliation} if reconciliation else None,
        },
    }


@pytest.fixture()
def projects(tmp_path: Path) -> Config:
    cfg = Config(projects_dir=tmp_path / "projects")
    cfg.projects_dir.mkdir(parents=True)
    return cfg


def _write(cfg: Config, report: dict) -> None:
    d = cfg.projects_dir / report["paper_id"] / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{report['paper_id']}.json").write_text(json.dumps(report), encoding="utf-8")


# --------------------------------------------------------------------------- #
# matrix
# --------------------------------------------------------------------------- #


def test_a_yellow_paper_is_not_reported_as_a_stale_artifact():
    """YELLOW is a live triage level. Routing it through the binary-verdict badge table
    rendered five of the seven corpus papers as "STALE — re-run this paper"."""
    r = _report("iclr")
    r["triage"] = "YELLOW"
    row = dossier.matrix([r])[1]
    assert row[6] == "🟡 YELLOW"
    assert "STALE" not in " ".join(row)
    assert dossier.triage_badge("YELLOW") == "🟡 YELLOW"
    # and a report predating the triage says so, rather than claiming GREEN
    assert dossier.triage_badge("") == "—"


def test_probe_status_carries_provenance_and_reconciliation():
    assert dossier.probe_status(_report("x")) == "within_noise (synthesized) · INCONCLUSIVE"
    assert dossier.probe_status(_report("x", reconciliation=None)) == "within_noise (synthesized)"


def test_probe_status_distinguishes_repo_exec_from_synthesized():
    """The same verdict word means different things depending on who wrote the code."""
    synth = dossier.probe_status(_report("x", provenance="synthesized"))
    real = dossier.probe_status(_report("x", provenance="repo_exec"))
    assert synth != real and "synthesized" in synth and "repo_exec" in real


# --------------------------------------------------------------------------- #
# the honesty properties
# --------------------------------------------------------------------------- #
def test_unfinished_papers_are_named_not_silently_dropped(projects: Config):
    _write(projects, _report("acl"))
    res = dossier.build(projects, ["acl", "never-reviewed"], projects.projects_dir / "out")
    assert res["papers"] == ["acl"]
    assert res["missing"] == ["never-reviewed"]
    assert "never-reviewed" in Path(res["markdown"]).read_text(encoding="utf-8")


def test_synthesized_probe_keeps_its_caveat_in_the_executive_summary():
    md = dossier.render_markdown([_report("iclr", provenance="synthesized")], [])
    assert "may neither convict nor acquit" in md
    assert "cannot drive this paper's verdict" in md


def test_a_placebo_probe_is_not_claimed_to_be_from_the_papers_formulation():
    """Major #5 — probe_synth.plan has no mechanism dispatch; the placebo is the only
    synthesized template and is paper-independent by construction. The dossier must not
    say it was written from the paper's own published formulation."""
    r = _report("x", provenance="synthesized")
    r["probe"]["mechanism"] = "placebo"
    md = dossier.render_markdown([r], [])
    assert "NOT derived from this paper's formulation" in md
    assert "written by the harness from the paper's own published" not in md


def test_template_probe_is_labelled_as_measuring_the_machine():
    md = dossier.render_markdown([_report("x", provenance="template")], [])
    assert "reproduces no claim" in md


def test_driver_and_repo_exec_probes_are_allowed_a_real_verdict():
    """Provenance alone is not enough — a real verdict also requires that a process
    actually ran and the reconciliation actually resolved. See the next two tests."""
    for prov in ("driver", "repo_exec"):
        md = dossier.render_markdown(
            [_report("x", provenance=prov, reconciliation="RESOLVED_VERIFIED")], [])
        assert "real reproduction verdict" in md
        assert "neither convict nor acquit" not in md


def test_a_refused_probe_is_not_claimed_as_a_real_verdict():
    """Major #2 — `repo_exec`/`driver` provenance is what a real verdict needs, not what
    it IS. A spec refused before anything ran (verdict `blocked`, no seeds) must not be
    described as having reconciled against the cited cell."""
    for prov in ("driver", "repo_exec"):
        r = _report("x", provenance=prov, reconciliation=None)
        r["probe"]["verdict"] = "blocked"
        r["probe"]["seeds_run"] = []
        md = dossier.render_markdown([r], [])
        assert "is a real reproduction verdict" not in md
        assert "no process executed" in md


def test_not_started_synthesized_probe_never_claims_it_ran_at_toy_scale():
    r = _report("x", provenance="synthesized", reconciliation=None)
    r["probe"]["verdict"] = "not_started"
    r["probe"]["seeds_run"] = []
    r["probe"]["executions"] = 0
    md = dossier.render_markdown([r], [])
    assert "A probe/reimplementation route was prepared but no process executed" in md
    for false_claim in ("run at toy scale", "ran at toy scale", "returned"):
        assert false_claim not in md


def test_an_inconclusive_reconciliation_is_not_claimed_as_a_real_verdict():
    """The other half of #2: a process CAN run under the right provenance and still not
    resolve (INCONCLUSIVE) — that is not a real reproduction verdict either."""
    for prov in ("driver", "repo_exec"):
        md = dossier.render_markdown(
            [_report("x", provenance=prov, reconciliation="INCONCLUSIVE")], [])
        assert "is a real reproduction verdict" not in md
        assert "reconciliation `INCONCLUSIVE`" in md


def test_evidence_quotes_are_reproduced_verbatim():
    r = _report("acl", major=1, minor=0)
    md = dossier.render_markdown([r], [])
    assert r["findings"][0]["evidence_quote"] in md


def test_calibration_line_reports_the_noise_band():
    line = dossier.calibration_line(_report("x"))
    assert "device `cuda`" in line and "5 seed(s) completed" in line
    assert "seed-noise σ 0.0048" in line and "2σ band 0.0096" in line
    assert "claimed Δ exceeds this machine's noise band" in line


def test_calibration_line_when_no_probe_ran():
    r = _report("x")
    r["probe"] = {}
    assert "no probe was run" in dossier.calibration_line(r)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def test_pdf_is_written_and_contains_the_matrix(projects: Config):
    _write(projects, _report("acl"))
    _write(projects, _report("iclr"))
    res = dossier.build(projects, ["acl", "iclr"], projects.projects_dir / "out")
    assert res["pdf_error"] is None, res["pdf_error"]
    pymupdf = pytest.importorskip("pymupdf")
    with pymupdf.open(res["pdf"]) as doc:
        text = "".join(page.get_text() for page in doc)
    assert "Executive Review Dossier" in text
    assert "Evaluation matrix" in text and "acl" in text and "iclr" in text


# --------------------------------------------------------------------------- #
# S2 autonomy hook
# --------------------------------------------------------------------------- #
def test_auto_audit_gate_is_closed_by_default():
    ok, why = audit_driver.available(Config(audit_cmd="x {prompt} {out}"))
    assert ok is False and "SH_ALLOW_AUTO_AUDIT" in why


def test_auto_audit_refuses_a_template_missing_its_placeholders():
    ok, why = audit_driver.available(Config(allow_auto_audit=True, audit_cmd="review {prompt}"))
    assert ok is False and "{out}" in why


def test_auto_audit_accepts_a_well_formed_template():
    assert audit_driver.available(
        Config(allow_auto_audit=True, audit_cmd="cp {prompt} {out}")) == (True, "")


@pytest.mark.parametrize("text,expected", [
    ("", "empty file"),
    ("sorry, I could not do that", "no JSON object"),
    ("{not json}", "not valid JSON"),
    ('{"lens": "overclaim"}', "no 'findings' key"),
    ('{"findings": "none"}', "not a list"),
])
def test_bad_auto_audit_output_is_rejected(text: str, expected: str):
    """A malformed lens must fail loudly; an empty findings list reads as 'paper is clean'."""
    with pytest.raises(AuditDriverError) as e:
        audit_driver.parse_lens_json(text, "overclaim")
    assert expected in str(e.value)


def test_auto_audit_extracts_json_from_a_fenced_reply():
    body = ('Sure. ```json\n{"lens":"overclaim","findings":[{"finding_id":"o-1",'
            '"severity":"MAJOR","title":"t","statement":"s","evidence_quote":"q",'
            '"evidence_ref":"T1:r0:c1"}],"unasked_question":"u","notes":"n"}\n``` done')
    report = audit_driver.parse_lens_json(body, "overclaim")
    assert len(report.findings) == 1 and report.findings[0].evidence_ref == "T1:r0:c1"


def test_auto_audit_fails_when_the_command_writes_nothing(tmp_path: Path):
    prompt = tmp_path / "overclaim.md"
    prompt.write_text("the prompt", encoding="utf-8")
    cfg = Config(projects_dir=tmp_path, allow_auto_audit=True,
                 audit_cmd='python -c "pass" {prompt} {out}')
    with pytest.raises(AuditDriverError) as e:
        audit_driver.run_lens(cfg, "p", "overclaim", prompt, tmp_path / "overclaim.json")
    assert "without writing" in str(e.value)


def test_auto_audit_writes_a_provenance_sidecar(tmp_path: Path):
    prompt = tmp_path / "overclaim.md"
    prompt.write_text("the prompt", encoding="utf-8")
    out = tmp_path / "overclaim.json"
    # A stand-in reviewer, as a script rather than `python -c`: the command goes through
    # a shell, and inline JSON loses its quotes to shell quoting on Windows.
    fake = tmp_path / "fake_reviewer.py"
    fake.write_text(
        "import json, sys, pathlib\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps("
        "{'lens': 'overclaim', 'findings': [], 'unasked_question': '', 'notes': 'n'}"
        "), encoding='utf-8')\n", encoding="utf-8")
    cfg = Config(projects_dir=tmp_path, allow_auto_audit=True,
                 audit_cmd=f'{sys.executable} "{fake}" {{prompt}} {{out}}')
    rec = audit_driver.run_lens(cfg, "p", "overclaim", prompt, out)
    sidecar = json.loads(out.with_suffix(".driver.json").read_text(encoding="utf-8"))
    assert sidecar["written_by"] == "audit_driver"
    assert sidecar["lens"] == "overclaim" and sidecar["returncode"] == 0
    assert rec["findings"] == 0


def test_a_stale_output_file_cannot_be_mistaken_for_success(tmp_path: Path):
    """A previous run's JSON must not be read back when this run's command writes nothing."""
    prompt = tmp_path / "overclaim.md"
    prompt.write_text("the prompt", encoding="utf-8")
    out = tmp_path / "overclaim.json"
    out.write_text('{"lens":"overclaim","findings":[]}', encoding="utf-8")
    cfg = Config(projects_dir=tmp_path, allow_auto_audit=True,
                 audit_cmd='python -c "pass" {prompt} {out}')
    with pytest.raises(AuditDriverError):
        audit_driver.run_lens(cfg, "p", "overclaim", prompt, out)
    assert not out.exists(), "the stale file must have been removed, not reused"


def test_a_batch_with_no_finished_report_does_not_overwrite_a_real_dossier(projects: Config):
    """Found in the pre-release audit, reproduced against the real tree.

    `review --paper a b` on two papers that both stopped at S2 called `build` with zero
    finished reports, and `build` wrote unconditionally. The completed three-paper dossier
    at the fixed path `reports/Executive_Review_Dossier.md` became:

        0 paper(s) reviewed ... Corpus totals: 0 FATAL, 0 MAJOR, 0 MINOR across 0 paper(s)

    Nothing in that document is true of the papers it names, and it silently replaced one
    that was. A dossier over nothing is not a small dossier — it is an assertion that a
    corpus was reviewed and found empty — so it is not written at all.

    Only the EMPTY case is refused here. A one-paper dossier still replaces a three-paper
    one; that is the documented `--out` limitation, a judgement about what an operator
    meant rather than a correctness bug.
    """
    out = projects.projects_dir / "out"
    out.mkdir(parents=True)
    real = out / "Executive_Review_Dossier.md"
    real.write_text("# REAL DOSSIER\n3 papers, 29 MAJOR findings.\n", encoding="utf-8")
    before = real.read_text(encoding="utf-8")

    res = dossier.build(projects, ["never-reviewed", "also-never-reviewed"], out)

    assert real.read_text(encoding="utf-8") == before, "an existing dossier must survive"
    assert res["markdown"] is None and res["papers"] == []
    assert res["missing"] == ["never-reviewed", "also-never-reviewed"]
    assert res["skipped"], "the refusal must say why, not fail silently"


def test_one_finished_report_is_still_written(projects: Config):
    """The guard must not have stopped the dossier from working at all."""
    _write(projects, _report("acl"))
    res = dossier.build(projects, ["acl", "never-reviewed"], projects.projects_dir / "out2")
    assert res["markdown"] and res["papers"] == ["acl"] and not res["skipped"]
