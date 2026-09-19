"""The orchestration loop as a program: sequencing, retries, abstention, isolation.

Before `harness/controller.py`, the workflow between stages was not code. `review()` ran
the deterministic stages, wrote four lens prompts, returned `status="needs_audit"`, and
the process exited; resuming depended on an actor outside the repository following prose
in CLAUDE.md. Every stage worked and the pipeline did not exist.

These tests pin the three properties that make the controller more than a `for` loop over
the stage functions:

  1. It knows where it is. State is persisted per paper, so a run that stops for evidence
     resumes at the phase it stopped in rather than from the beginning.
  2. It distinguishes a transient failure from a decision. A lens whose reviewer timed out
     is retried, bounded and recorded; an identity, resource, commit or authorization
     refusal is never retried, because re-asking a deterministic gate is asking it to
     change its mind.
  3. It treats abstention as an outcome, not a crash. A paper with no repository, an
     ambiguous experiment or a 24 GiB requirement on an 8 GiB card still gets a complete
     review; the reproduction abstains and the case reaches `complete`. Only a paper that
     could not be read, or one where no lens produced anything, is an error — which is
     what keeps the harness from failing hardest on the papers that most need care.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import audit_driver, controller
from harness.artifacts import (CaseState, PaperDoc, ProbeResult, QuantFinding, Reconciliation,
                               Section, Table)
from harness.config import Config
from harness.controller import (PHASES, RETRYABLE, PhaseOutcome, drive, drive_all, load_case,
                                open_case, step, summarize)
from harness.stages import audit as audit_stage

PID = "case-paper"


# --------------------------------------------------------------------------- #
# Fixtures — a fully ingested paper on disk, without touching a PDF
# --------------------------------------------------------------------------- #
def _doc(pid: str = PID) -> PaperDoc:
    return PaperDoc(
        paper_id=pid, title=f"Title of {pid}", n_pages=3, content_sha="a" * 12,
        sections=[Section(section_idx=0, title="Results", page_start=3,
                          text="The proposed method reaches 91.4 accuracy on the held-out "
                               "split, an improvement we attribute to the new regulariser.")],
        tables=[Table(table_idx=0, page=3, caption="Table 1: results",
                      rows=[["method", "acc"], ["ours", "91.4"]])])


def _doc_with_artifact(pid: str = PID) -> PaperDoc:
    """The same paper, but one whose printed result a discovered target can legitimately
    pursue: a repository is advertised and the extractor addressed the reported number.

    Needed because the execution gate is no longer `any(f.verifiable_by_experiment)` — an
    unchecked lens boolean — but a target that survived discovery, prioritisation and
    `harness.planner`. A paper with no artifact and no addressed quantity now abstains
    BEFORE the probe, correctly, so a test about how the PROBE abstains has to hand the
    pipeline a paper that reaches it.
    """
    doc = _doc(pid)
    doc.repo_url = "https://example.invalid/repo"
    doc.repo_urls = [doc.repo_url]
    doc.reported_numbers = [QuantFinding(value="91.4", metric="accuracy", method="ours",
                                         source_quote="91.4", page=3, table_ref="T0:r1:c1")]
    return doc


def _plant(cfg: Config, pid: str = PID, doc: PaperDoc | None = None) -> str:
    """An ingested paper on disk, without going through a PDF.

    `create_project` deliberately refuses to overwrite an existing case directory, so the
    directory is left for it to create rather than pre-made here.
    """
    from harness import state
    doc = doc or _doc(pid)
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    if not (cfg.projects_dir / pid / "project.json").exists():
        state.create_project(cfg, "", doc.title, pid=pid)
    state.write_json(cfg.projects_dir / pid / "paper" / "doc.json", doc.model_dump())
    return pid


def _lens(cfg: Config, pid: str, lens: str, *, quote="91.4", ref="T0:r1:c1",
          severity="MINOR", verifiable=False, findings=None) -> None:
    """Writes a lens result the way a HAND-WRITTEN one is expected to arrive: through
    `accept_lens`, which is the only path that leaves the `.driver.json` provenance
    sidecar `lens_is_accepted` now requires — see that function's docstring for why a
    file dropped directly onto disk must not be picked up as a completed lens."""
    body = {"lens": lens, "findings": findings if findings is not None else [{
        "finding_id": f"{lens}-01", "severity": severity, "title": f"{lens} title",
        "statement": "a defect", "evidence_quote": quote, "evidence_ref": ref,
        "verifiable_by_experiment": verifiable}]}
    audit_stage.accept_lens(cfg, pid, lens, json.dumps(body))


def _all_lenses(cfg: Config, pid: str, **kw) -> None:
    for lens in audit_stage.LENSES:
        _lens(cfg, pid, lens, **kw)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path / "projects")


# --------------------------------------------------------------------------- #
# The machine
# --------------------------------------------------------------------------- #
def test_the_phase_order_is_the_pipeline():
    """Two positions are policy, not convenience.

    `assess` sits between grading and discovery. It asks "has the paper's own evidence
    already settled this?", which needs COUNTED severity final (so after `grade`) and
    nothing expensive yet spent (so before `discover`).

    `discover` sits between `assess` and the probe. It is the only point at which "is an
    experiment justified?" can be asked with the cheap reasoning already done and the
    expensive part not yet begun. Moving it earlier would decide before the lenses had
    read the paper; moving it later would decide after the cost had been paid.
    """
    assert PHASES == ("ingest", "audit", "collect", "grade", "assess", "discover",
                      "probe", "report", "done")
    assert PHASES.index("grade") < PHASES.index("assess") < PHASES.index("discover")


def test_only_a_delegated_lens_may_be_retried():
    """The retry policy IS the safety property. Re-running a deterministic refusal is
    asking a gate the same question until it answers differently. A delegated GRADE
    joins a delegated lens for the identical reason: a rate-limited or transiently
    failed grader may well succeed on the same candidate a second time."""
    assert RETRYABLE == ("audit", "grade")


def test_a_case_persists_its_position(cfg: Config):
    _plant(cfg)
    case = drive(cfg, open_case(cfg, PID))
    assert case.status == "waiting"
    reloaded = load_case(cfg, PID)
    assert reloaded is not None
    assert reloaded.phase == case.phase and reloaded.status == case.status
    assert reloaded.awaiting == case.awaiting


def test_a_run_resumes_at_the_phase_it_stopped_in(cfg: Config):
    """The evidence arrives between invocations; the second run must not start over."""
    _plant(cfg)
    first = drive(cfg, open_case(cfg, PID))
    assert first.phase == "audit" and first.status == "waiting"

    _all_lenses(cfg, PID)
    second = drive(cfg, open_case(cfg, PID), skip_probe=True)
    assert second.status == "complete", second.blocked_reason
    assert second.attempts["ingest"] == 1, "ingest must not have been redone"


def test_every_attempt_is_recorded(cfg: Config):
    _plant(cfg)
    case = drive(cfg, open_case(cfg, PID))
    phases = [e.phase for e in case.history]
    assert phases[:2] == ["ingest", "audit"]
    assert all(e.ts and e.outcome for e in case.history)


def test_step_advances_exactly_one_phase(cfg: Config):
    _plant(cfg)
    case = open_case(cfg, PID)
    assert case.phase == "ingest"
    case = step(cfg, case)
    assert case.phase == "audit" and len(case.history) == 1


def test_a_terminal_case_is_not_advanced_again(cfg: Config):
    case = drive(cfg, open_case(cfg, "no-such-case"))
    assert case.status == "error"
    before = len(case.history)
    assert len(step(cfg, case).history) == before


# --------------------------------------------------------------------------- #
# S2 → S3 continuation, in code
# --------------------------------------------------------------------------- #
def test_the_pipeline_runs_to_a_report_once_every_lens_has_a_result(cfg: Config):
    """The continuation that used to require an actor outside the process."""
    _plant(cfg)
    _all_lenses(cfg, PID)
    case = drive(cfg, open_case(cfg, PID), skip_probe=True)
    assert case.status == "complete"
    assert [e.phase for e in case.history] == [
        "ingest", "audit", "collect", "grade", "assess", "discover", "probe", "report"]
    assert case.verdict in ("RED", "YELLOW", "GREEN")
    assert Path(case.report_path).is_file()


def test_the_controller_waits_rather_than_inventing_lens_results(cfg: Config):
    _plant(cfg)
    case = drive(cfg, open_case(cfg, PID))
    assert case.status == "waiting" and len(case.awaiting) == 4
    assert not (cfg.projects_dir / PID / "reports" / f"{PID}.json").exists()


def test_a_partial_panel_still_produces_a_review(cfg: Config):
    """Three lenses is a weaker panel than four, and it is not nothing. The report names
    which lenses ran, so an absent one can never read as a clean bill of health."""
    _plant(cfg)
    for lens in list(audit_stage.LENSES)[:3]:
        _lens(cfg, PID, lens)
    case = drive(cfg, open_case(cfg, PID), skip_probe=True)
    assert case.status == "waiting", "a missing lens still blocks by default"

    report = json.loads(
        (cfg.projects_dir / PID / "audit" / "overclaim.json").read_text(encoding="utf-8"))
    assert report["lens"] == "overclaim"


def test_zero_lens_output_is_an_error_not_a_clean_paper(cfg: Config, monkeypatch):
    """An empty panel must never reach the report stage. A review with no findings and no
    lenses is indistinguishable in the output from a paper nobody could fault."""
    _plant(cfg)
    monkeypatch.setattr(audit_stage, "load_reports", lambda *a, **k: ([], 0, []))
    _all_lenses(cfg, PID)
    case = drive(cfg, open_case(cfg, PID), skip_probe=True)
    assert case.status == "error"
    assert "no audit lens produced a result" in case.blocked_reason


# --------------------------------------------------------------------------- #
# Audit collection, malformed output, retries
# --------------------------------------------------------------------------- #
def test_a_malformed_lens_file_blocks_collection_rather_than_faking_a_clean_run(cfg: Config):
    """C9 — SUPERSEDED by the correction pass. This asserted the opposite: that a
    malformed lens file "does not sink the panel" and the case still reaches `complete`.

    That let a `{not json` file — indistinguishable from a lens that ran and found
    nothing — flow straight into a GREEN report. `collect` now refuses to advance while
    any lens is missing or invalid, the same resumable `waiting` state a pending lens
    already used; nothing here is a hard failure, because rewriting the one bad file is
    exactly what lets the case proceed.
    """
    _plant(cfg)
    _all_lenses(cfg, PID)
    lens_path = cfg.projects_dir / PID / "audit" / "protocol.json"
    lens_path.write_text("{not json", encoding="utf-8")
    # Keep the provenance sidecar's content hash in sync with the corrupted bytes, so
    # `lens_is_accepted` still passes and it is `load_reports`'s OWN json.loads guard
    # under test here — not the (separate, earlier) provenance seal, which a byte
    # corrupted after real acceptance would otherwise trip first.
    import hashlib
    sidecar = lens_path.with_suffix(".driver.json")
    rec = json.loads(sidecar.read_text(encoding="utf-8"))
    rec["content_sha256"] = hashlib.sha256(lens_path.read_bytes()).hexdigest()
    sidecar.write_text(json.dumps(rec), encoding="utf-8")
    case = drive(cfg, open_case(cfg, PID), skip_probe=True)
    assert case.status == "waiting", case.blocked_reason
    assert "protocol" in case.blocked_reason
    collect = next(e for e in case.history if e.phase == "collect")
    assert collect.detail["invalid"] == ["protocol"]

    # Rewriting the bad file with a valid one and re-driving completes the review.
    _lens(cfg, PID, "protocol")
    case = drive(cfg, case, skip_probe=True)
    assert case.status == "complete", case.blocked_reason
    collect = next(e for e in reversed(case.history) if e.phase == "collect")
    assert "protocol" in collect.detail["lenses"], "the lens is reported as having run"


def test_findings_whose_evidence_is_not_in_the_paper_are_dropped_and_counted(cfg: Config):
    _plant(cfg)
    _all_lenses(cfg, PID)
    _lens(cfg, PID, "overclaim", quote="a number the paper never printed", ref="T0:r1:c1")
    case = drive(cfg, open_case(cfg, PID), skip_probe=True)
    collect = next(e for e in case.history if e.phase == "collect")
    assert collect.detail["dropped"] >= 1


def test_a_reviewer_that_fails_is_retried_up_to_the_bound(cfg: Config, monkeypatch):
    """A timeout or a truncated JSON object is transient; the same prompt may succeed."""
    _plant(cfg)
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, "x {prompt} {out}", 2
    calls = {"n": 0}

    def always_fails(cfg_, pid, awaiting, prompts, **kw):
        calls["n"] += 1
        return {"filled": [], "failed": {ln: "reviewer exploded" for ln in awaiting},
                "rate_limited": {}}

    monkeypatch.setattr(audit_driver, "fill", always_fails)
    case = drive(cfg, open_case(cfg, PID), auto_audit=True)
    assert case.status == "waiting"
    assert calls["n"] == cfg.audit_retries + 1, calls
    assert case.attempts["audit"] == cfg.audit_retries + 1


def test_retry_exhaustion_is_recorded_rather_than_silent(cfg: Config, monkeypatch):
    _plant(cfg)
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, "x {prompt} {out}", 1
    monkeypatch.setattr(audit_driver, "fill", lambda *a, **k: {
        "filled": [], "failed": {"overclaim": "timed out after 900s"}, "rate_limited": {}})
    case = drive(cfg, open_case(cfg, PID), auto_audit=True)
    assert "timed out" in case.blocked_reason
    outcomes = [e.outcome for e in case.history if e.phase == "audit"]
    assert outcomes == ["retry", "waiting"], outcomes


def test_a_reviewer_that_partly_succeeds_keeps_what_worked(cfg: Config, monkeypatch):
    _plant(cfg)
    cfg.allow_auto_audit, cfg.audit_cmd, cfg.audit_retries = True, "x {prompt} {out}", 0

    def fills_all(cfg_, pid, awaiting, prompts, **kw):
        for lens in awaiting:
            _lens(cfg_, pid, lens)
        return {"filled": list(awaiting), "failed": {}, "rate_limited": {}}

    monkeypatch.setattr(audit_driver, "fill", fills_all)
    case = drive(cfg, open_case(cfg, PID), auto_audit=True, skip_probe=True)
    assert case.status == "complete", case.blocked_reason
    audit_event = next(e for e in case.history if e.phase == "audit")
    assert len(audit_event.detail["filled"]) == 4


def test_an_unavailable_reviewer_waits_instead_of_retrying(cfg: Config):
    """An unconfigured reviewer fails identically every time. Retrying would bury the
    reason under repeated attempts rather than surfacing it."""
    _plant(cfg)
    cfg.allow_auto_audit = False
    case = drive(cfg, open_case(cfg, PID), auto_audit=True)
    assert case.status == "waiting"
    assert case.attempts["audit"] == 1, "a configuration fact is not retried"
    assert "gate is closed" in case.blocked_reason


# --------------------------------------------------------------------------- #
# Abstention is not failure
# --------------------------------------------------------------------------- #
def test_a_paper_with_no_execution_justified_still_reaches_a_report(cfg: Config):
    """A paper with no artifact and an under-specified method abstains BEFORE the probe.

    The abstention is now a decision with a stated reason rather than the absence of a
    lens boolean, and the reason is auditable: the target set records what was discovered,
    what was settled against the paper itself, and what was blocked by specification,
    artifact or environment. The review still completes — that is invariant 6 and 7's
    whole point, and the class name says which of the two it was.
    """
    _plant(cfg)
    _all_lenses(cfg, PID, verifiable=False)
    case = drive(cfg, open_case(cfg, PID))
    assert case.status == "complete"
    assert case.reproduction_class == "no_execution_justified"
    assert Path(case.report_path).is_file()


def test_a_lens_boolean_alone_no_longer_opens_the_execution_path(cfg: Config):
    """`verifiable_by_experiment` was the sole gate on the whole execution half.

    Setting it on every finding of a paper that advertises no artifact used to send the
    pipeline into the probe. It no longer does anything on its own: what opens the path
    is a target the harness itself found addressable and a planner decision that an
    experiment is justified.
    """
    _plant(cfg)
    _all_lenses(cfg, PID, verifiable=True)
    case = drive(cfg, open_case(cfg, PID))
    assert case.status == "complete"
    assert case.reproduction_class == "no_execution_justified"


def test_a_probe_that_raises_abstains_rather_than_failing_the_case(cfg: Config, monkeypatch):
    """A fault in this harness is not evidence about the paper, and must not stop a review."""
    from harness.stages import probe as probe_stage
    _plant(cfg, doc=_doc_with_artifact())
    _all_lenses(cfg, PID, verifiable=True)
    monkeypatch.setattr(probe_stage, "run", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("the probe stage exploded")))
    case = drive(cfg, open_case(cfg, PID))
    assert case.status == "complete", case.blocked_reason
    assert case.reproduction_class == "probe_error"
    probe_event = next(e for e in case.history if e.phase == "probe")
    assert probe_event.outcome == "abstain"


@pytest.mark.parametrize("failure_class", [
    "resources_insufficient", "commit_mismatch", "experiment_unidentified",
    "metric_unbound", "configuration_unmatched", "backend_unavailable",
    "credentials_unavailable", "execution_unauthorized", "dependency_missing",
])
def test_every_abstention_class_still_produces_a_review(cfg: Config, monkeypatch,
                                                        failure_class: str):
    """The list of ways reproduction can decline to conclude, each ending in a complete
    review rather than a crashed run. This is the property that lets the harness be
    pointed at arbitrary papers."""
    from harness import state
    from harness.stages import probe as probe_stage
    _plant(cfg, doc=_doc_with_artifact())
    _all_lenses(cfg, PID, verifiable=True)

    def writes_a_blocked_result(cfg_, pid):
        result = ProbeResult(paper_id=pid, verdict="blocked", reconciliation=Reconciliation(
            status="INCONCLUSIVE", failure_class=failure_class))
        state.write_json(state.control_dir(cfg_.projects_dir / pid) / "probe_results.json",
                         result.model_dump())
        return {"paper_id": pid, "verdict": "blocked", "reason": failure_class}

    monkeypatch.setattr(probe_stage, "run", writes_a_blocked_result)
    case = drive(cfg, open_case(cfg, PID))
    assert case.status == "complete", case.blocked_reason
    assert case.reproduction_class == failure_class
    assert Path(case.report_path).is_file()


def test_a_missing_pdf_is_an_error_and_only_that(cfg: Config):
    case = drive(cfg, open_case(cfg, "nowhere/absent.pdf"))
    assert case.status == "error" and case.terminal
    assert [e.phase for e in case.history] == ["ingest"]


# --------------------------------------------------------------------------- #
# Multiple papers
# --------------------------------------------------------------------------- #
def test_papers_advance_independently(cfg: Config):
    _plant(cfg, "paper-a")
    _plant(cfg, "paper-b", _doc("paper-b"))
    _all_lenses(cfg, "paper-a")                      # a is ready; b is not
    cases = drive_all(cfg, ["paper-a", "paper-b"], skip_probe=True)
    by_id = {c.paper_id: c for c in cases}
    assert by_id["paper-a"].status == "complete"
    assert by_id["paper-b"].status == "waiting"


def test_one_papers_error_does_not_stall_the_others(cfg: Config):
    _plant(cfg, "paper-a")
    _all_lenses(cfg, "paper-a")
    cases = drive_all(cfg, ["paper-a", "does-not-exist"], skip_probe=True)
    assert cases[0].status == "complete"
    assert cases[1].status == "error"


def test_cases_share_no_state(cfg: Config):
    _plant(cfg, "paper-a")
    _plant(cfg, "paper-b", _doc("paper-b"))
    _all_lenses(cfg, "paper-a", quote="91.4", severity="FATAL")
    _all_lenses(cfg, "paper-b", quote="91.4", severity="MINOR")
    cases = drive_all(cfg, ["paper-a", "paper-b"], skip_probe=True)
    by_id = {c.paper_id: c for c in cases}
    assert by_id["paper-a"].verdict != "RED", "model FATAL has no rejection authority"
    assert by_id["paper-b"].verdict != "RED", "one paper's severity must not leak"
    for pid in ("paper-a", "paper-b"):
        chain = json.loads((cfg.projects_dir / pid / "reports" / f"{pid}.json")
                           .read_text(encoding="utf-8"))
        assert chain["paper_id"] == pid
        assert all(f["lens"] in audit_stage.LENSES for f in chain["findings"])


def test_a_batch_summary_names_every_outcome(cfg: Config):
    _plant(cfg, "paper-a")
    _all_lenses(cfg, "paper-a")
    _plant(cfg, "paper-b", _doc("paper-b"))
    cases = drive_all(cfg, ["paper-a", "paper-b", "missing-one"], skip_probe=True)
    s = summarize(cases)
    assert s["papers"] == 3
    assert s["complete"] == ["paper-a"]
    assert "paper-b" in s["waiting"] and "missing-one" in s["errors"]


# --------------------------------------------------------------------------- #
# The controller cannot overrule a gate
# --------------------------------------------------------------------------- #
def test_the_controller_reads_the_reproduction_class_it_does_not_decide_it():
    """`_reproduction_class` is a projection of the artifact the gates wrote. If it could
    compute a status, the controller would be able to promote its own abstention into a
    verdict."""
    from harness.controller import _reproduction_class
    assert _reproduction_class(None) == "not_attempted"
    verified = ProbeResult(paper_id="p", reconciliation=Reconciliation(status="RESOLVED_VERIFIED"))
    assert _reproduction_class(verified) == "RESOLVED_VERIFIED"
    inconclusive = ProbeResult(paper_id="p", reconciliation=Reconciliation(
        status="INCONCLUSIVE", failure_class="resources_insufficient"))
    assert _reproduction_class(inconclusive) == "resources_insufficient"


def test_a_non_retryable_phase_asking_to_retry_is_refused(cfg: Config):
    """Defence in depth: if a handler outside RETRYABLE ever returned `retry`, the machine
    converts it to `waiting` rather than looping on a deterministic refusal."""
    _plant(cfg)
    case = CaseState(paper_id=PID, source=PID, phase="collect")
    controller._HANDLERS["collect"] = lambda *a, **k: PhaseOutcome("retry", "please again")
    try:
        out = step(cfg, case)
        assert out.status == "waiting"
        assert "only audit, grade may be re-attempted" in out.blocked_reason
    finally:
        controller._HANDLERS["collect"] = controller._phase_collect


def test_a_handler_that_raises_becomes_a_case_error_not_a_crash(cfg: Config):
    _plant(cfg)
    case = CaseState(paper_id=PID, source=PID, phase="collect")
    controller._HANDLERS["collect"] = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
    try:
        out = step(cfg, case)
        assert out.status == "error" and "ValueError: boom" in out.blocked_reason
    finally:
        controller._HANDLERS["collect"] = controller._phase_collect


# --------------------------------------------------------------------------- #
# A failed delegation must leave nothing that reads as a result
# --------------------------------------------------------------------------- #
# Found by running the real reviewer against a real paper. The command's stdout was
# redirected straight at `audit/<lens>.json`, so the shell created that file before this
# harness could reject its contents. When the reviewer emitted something that was not a
# lens report — in the observed case a rate-limit notice — four files existed, `run_audit`
# reported the panel complete, `load_reports` could not parse them and returned four EMPTY
# reports, and a paper that had never been audited came out GREEN with "4 lenses run, 0
# findings". Every stage behaved correctly on a corrupt input; the corruption was the bug.
def _reviewer_writing(cfg: Config, text: str) -> str:
    """A command template whose 'reviewer' writes exactly `text` to {out}.

    A real script rather than shell redirection, so the same fixture works under cmd.exe
    and a POSIX shell — the platform difference is exactly what `default_cmd` handles and
    exactly what a test should not have to reproduce.
    """
    import sys
    script = cfg.projects_dir.parent / "fake_reviewer.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import sys, pathlib\n"
        "prompt, out = sys.argv[1], sys.argv[2]\n"
        "assert pathlib.Path(prompt).is_file(), prompt\n"
        f"pathlib.Path(out).write_text({text!r}, encoding='utf-8')\n",
        encoding="utf-8")
    return f'"{sys.executable}" "{script}" "{{prompt}}" "{{out}}"'


@pytest.mark.parametrize("junk", [
    "You've hit your session limit",       # the case observed in the wild
    "",                                     # an empty response
    "Sure! Here is the audit you asked for.",
    '{"not": "a lens report"}',            # JSON, but no findings key
])
def test_a_reviewer_that_does_not_produce_a_report_leaves_no_lens_file(cfg: Config, junk):
    _plant(cfg)
    cfg.allow_auto_audit, cfg.audit_retries = True, 0
    cfg.audit_cmd = _reviewer_writing(cfg, junk)
    case = drive(cfg, open_case(cfg, PID), auto_audit=True)

    audit_dir = cfg.projects_dir / PID / "audit"
    assert sorted(p.name for p in audit_dir.glob("*.json")) == [], "nothing may be fabricated"
    assert case.status == "waiting", case.status
    assert case.verdict == "", "a paper nobody audited must not receive a verdict"


def test_the_rejected_output_is_kept_but_not_where_it_reads_as_a_result(cfg: Config):
    _plant(cfg)
    cfg.allow_auto_audit, cfg.audit_retries = True, 0
    cfg.audit_cmd = _reviewer_writing(cfg, "You have hit your session limit")
    drive(cfg, open_case(cfg, PID), auto_audit=True)

    audit_dir = cfg.projects_dir / PID / "audit"
    rejected = sorted(p.name for p in audit_dir.glob("*.rejected.txt"))
    assert rejected, "an unusable response is worth seeing"
    assert all(not n.endswith(".json") for n in rejected)
    assert "session limit" in (audit_dir / "overclaim.rejected.txt").read_text(encoding="utf-8")
    assert not list(audit_dir.glob("*.staged")), "staging files are cleaned up"


def test_a_successful_delegation_still_writes_the_lens(cfg: Config):
    """The staging step must not have broken the path it protects."""
    _plant(cfg)
    cfg.allow_auto_audit, cfg.audit_retries = True, 0
    good = ('{"lens": "x", "findings": [{"finding_id": "f-01", "severity": "MINOR", '
            '"statement": "a defect", "evidence_quote": "91.4", "evidence_ref": "T0:r1:c1"}], '
            '"unasked_question": "", "notes": ""}')
    cfg.audit_cmd = _reviewer_writing(cfg, good)
    case = drive(cfg, open_case(cfg, PID), auto_audit=True, skip_probe=True)

    audit_dir = cfg.projects_dir / PID / "audit"
    assert len(list(audit_dir.glob("*.json"))) >= 4
    assert case.status == "complete", case.blocked_reason
    assert not list(audit_dir.glob("*.rejected.txt"))
    assert not list(audit_dir.glob("*.staged"))
