"""The SECOND half of early stopping: stopping once evidence establishes a failure.

`harness/assessment.py` stops BEFORE the investigation, from findings already counted at
grade time. It runs exactly once, between `grade` and `discover`, and it therefore knows
nothing about what execution goes on to establish. So a paper whose FIRST target proved a
material failure against the authors' own code still cloned, planned, authorized and
launched for every remaining target: `stages/probe.py` contained no reference to
`materiality`, `establishes_failure` or `assessment` anywhere in its execution loop.

These tests drive the REAL loop in `stages/probe._review` — the same `for obj, plan in
pairs[1:]` body that runs in production, with only the filesystem and the runner stubbed —
because the guard is a property of that loop and a test of `materiality` alone would pass
whether or not the loop ever calls it.

Four rules, and three of them are refusals:

  * once a MATERIAL failure is established, no further expensive target is started;
  * a BLOCKER never triggers the stop, because a blocker establishes nothing;
  * an established NON-MATERIAL defect never triggers it, for the same reason the
    paper-level decision distinguishes the two at all;
  * what is skipped is recorded as skipped, with `launched == 0`, and is neither a
    refusal nor an attempt.
"""
from __future__ import annotations

import pytest

from harness import ledger, materiality
from harness.artifacts import (CodeAudit, DiscoveredObject, PlanDecision, ProbeResult,
                               EvalReport, ProbeSpec, Reconciliation, RepoAcquisition,
                               TargetOutcome, TargetSet)
from harness.config import Config
from harness.stages import probe as probe_stage


# --------------------------------------------------------------------------- #
# Fixtures: three executable targets, the first of which will end how the test says
# --------------------------------------------------------------------------- #
def _obj(tid: str, *, material: bool) -> DiscoveredObject:
    return DiscoveredObject(
        target_id=tid, kind="REPRODUCTION_TARGET", centrality="CENTRAL",
        harness_addressable=True, routes=["AUTHOR_CODE_EXECUTION"],
        materiality_basis=("ABSTRACT_CLAIM" if material else "NONE"),
        claim_text=f"claim for {tid}")


def _plan(tid: str) -> PlanDecision:
    return PlanDecision(target_id=tid, action="AUTHOR_CODE_REPRODUCTION",
                        route="AUTHOR_CODE_EXECUTION", requires_execution=True,
                        reason="worth an experiment")


def _result(pid: str, *, status: str, provenance: str) -> ProbeResult:
    """A finished run whose reconciliation ends `status` on `provenance`."""
    rec = Reconciliation(table_ref="T1:r0:c0", status=status, provenance=provenance,
                         reason=f"reconciled to {status}")
    return ProbeResult(paper_id=pid, verdict="done", provenance=provenance,
                       reason="ran", executions=10, script_path="probe.py",
                       reconciliation=rec)


@pytest.fixture
def loop(monkeypatch, tmp_path):
    """Drive `_review`'s real loop with the filesystem and the runner stubbed out.

    Everything the loop itself does — the guard, the `continue`, the accumulation of
    outcomes, the ordering — is production code. Only acquisition, the static audit,
    spec synthesis and the subprocess are replaced, because those reach the network,
    the disk and a python interpreter and none of them decides anything this tests.
    """
    def build(first_status: str, first_provenance: str, *, material_first: bool,
              n: int = 3):
        pid = "p"
        objs = [_obj(f"T{i}", material=(material_first and i == 1)) for i in range(1, n + 1)]
        plans = [_plan(o.target_id) for o in objs]
        ts = TargetSet(paper_id=pid, objects=objs, plans=plans)
        pairs = list(zip(objs, plans))

        monkeypatch.setattr(probe_stage.state, "project_dir", lambda cfg, p: tmp_path)
        monkeypatch.setattr(probe_stage.state, "write_json", lambda *a, **k: None)
        monkeypatch.setattr(probe_stage.state, "set_phase", lambda *a, **k: None)
        monkeypatch.setattr(probe_stage.state, "read_json",
                            lambda *a, **k: {"paper_id": pid, "title": "t", "sections": []})
        (tmp_path / "paper").mkdir(parents=True, exist_ok=True)
        (tmp_path / "paper" / "doc.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(probe_stage, "_executable_targets",
                            lambda cfg, p: (ts, pairs, []))
        monkeypatch.setattr(probe_stage, "acquire_and_audit",
                            lambda *a, **k: (_ACQ, _AUDIT))
        monkeypatch.setattr(probe_stage, "synthesize_probe", lambda cfg, d, s, a: s)
        monkeypatch.setattr(probe_stage, "plan_execution",
                            lambda cfg, s, a, d, au, root=None: s)
        monkeypatch.setattr(probe_stage, "establish_comparison", lambda s, r, **kw: s)
        monkeypatch.setattr(probe_stage, "may_be_compared", lambda s: (True, ""))
        monkeypatch.setattr(probe_stage, "admissible_if_it_succeeds",
                            lambda cfg, s: (True, ""))
        monkeypatch.setattr(probe_stage, "replan_after_author_code_exhausted",
                            lambda o, p, s: None)

        calls: list[str] = []

        def fake_run(cfg, root, spec, out_dir=None):
            calls.append(spec.target_id or "primary")
            if len(calls) == 1:
                return _result(pid, status=first_status, provenance=first_provenance)
            return _result(pid, status="REPRODUCED", provenance="repo_exec")

        monkeypatch.setattr(probe_stage, "_run", fake_run)
        out = probe_stage._review(Config(projects_dir=tmp_path.parent), pid)
        return ts, out, calls

    return build


_ACQ = RepoAcquisition(status="unavailable", reason="no repository in this fixture")
_AUDIT = CodeAudit(status="not_attempted")


def _outcomes(ts: TargetSet) -> dict:
    return {o.target_id: o for o in ts.outcomes}


# --------------------------------------------------------------------------- #
# 1. The stop
# --------------------------------------------------------------------------- #
def test_a_material_failure_stops_the_remaining_expensive_targets(loop):
    """Target 1 convicts on the authors' code. Targets 2 and 3 must not start."""
    ts, _out, calls = loop("FAILED_REPRODUCTION", "repo_exec", material_first=True)
    got = _outcomes(ts)
    assert got["T1"].establishes_failure, "the premise: target 1 established a failure"
    assert materiality.is_material(
        materiality.basis_for_target("T1", ts.objects) or "NONE"), (
        "and the premise's other half: that failure is material")

    for tid in ("T2", "T3"):
        assert got[tid].disposition == "SUPERSEDED_BY_ESTABLISHED_FAILURE", got[tid].disposition
        assert got[tid].launched == 0, "a superseded target started nothing"
        assert "material failure was already established" in got[tid].reason
        assert "T1" in got[tid].reason, "and it names which target established it"

    assert calls == ["primary"], f"only the first target ran a process, got {calls}"


def test_what_is_skipped_is_neither_a_refusal_nor_an_attempt(loop):
    """`SUPERSEDED_BY_ESTABLISHED_FAILURE` is its own state for a reason.

    It must not read as a blocker — nothing about the artifact, the host or a gate
    stopped it — and it must not read as something that was tried and settled nothing.
    """
    from harness.artifacts import BLOCKED_DISPOSITIONS
    ts, _out, _calls = loop("FAILED_REPRODUCTION", "repo_exec", material_first=True)
    skipped = _outcomes(ts)["T2"]
    assert skipped.disposition not in BLOCKED_DISPOSITIONS, (
        "the review chose not to spend more, which is not a blocker")
    assert skipped.reason and "not refused" in skipped.reason
    assert skipped.reconciliation is None


# --------------------------------------------------------------------------- #
# 2. The three refusals to stop
# --------------------------------------------------------------------------- #
def test_a_blocker_never_triggers_the_stop(loop):
    """A target that could not be checked has established nothing about the paper."""
    ts, _out, calls = loop("INCONCLUSIVE", "repo_exec", material_first=True)
    got = _outcomes(ts)
    assert not got["T1"].establishes_failure
    for tid in ("T2", "T3"):
        assert got[tid].disposition != "SUPERSEDED_BY_ESTABLISHED_FAILURE"
    assert len(calls) == 3, "every target still got its run"


def test_an_inadmissible_failure_never_triggers_the_stop(loop):
    """A synthesized diagnostic that 'failed' is refused by the ceiling, so it stops nothing.

    This is the case the shipped corpus actually produced 16 times. If the guard read the
    disposition rather than `establishes_failure`, every one of those runs would have
    stopped its paper's remaining investigation on evidence that may conclude nothing.
    """
    ts, _out, calls = loop("FAILED_REPRODUCTION", "synthesized", material_first=True)
    got = _outcomes(ts)
    assert not got["T1"].establishes_failure, "the ceiling refuses it"
    for tid in ("T2", "T3"):
        assert got[tid].disposition != "SUPERSEDED_BY_ESTABLISHED_FAILURE"
    assert len(calls) == 3


def test_an_established_but_non_material_defect_never_triggers_the_stop(loop):
    """Tier 1 without Tier 2 is a defect worth reporting, not a reason to stop reviewing.

    The paper-level decision keeps "we established something objectively wrong" apart from
    "and it rejects the paper". The stop belongs to the second, and this pins that it
    cannot be reached by the first.
    """
    ts, _out, calls = loop("FAILED_REPRODUCTION", "repo_exec", material_first=False)
    got = _outcomes(ts)
    assert got["T1"].establishes_failure, "it did establish a defect"
    assert not materiality.is_material(
        materiality.basis_for_target("T1", ts.objects) or "NONE")
    for tid in ("T2", "T3"):
        assert got[tid].disposition != "SUPERSEDED_BY_ESTABLISHED_FAILURE", (
            "a non-material established defect must not stop the investigation")
    assert len(calls) == 3


# --------------------------------------------------------------------------- #
# 3. What the stop may never suppress
# --------------------------------------------------------------------------- #
def test_every_target_still_reports_an_outcome(loop):
    """The referee record stays complete. Stopping spends less; it reports no less."""
    ts, _out, _calls = loop("FAILED_REPRODUCTION", "repo_exec", material_first=True)
    assert len(ts.outcomes) == 3, "one outcome per target, whatever was spent"
    assert all(o.reason for o in ts.outcomes), "and each says why it ended as it did"


def test_discovery_paper_failure_stops_before_primary_acquisition_and_execution(
        monkeypatch, tmp_path):
    """A discovery-time arithmetic contradiction stops the whole expensive branch.

    This is the mixed case the between-target guard cannot cover: discovery has already
    established one material paper-only failure, but two other targets still have valid
    execution plans.  Use the real target-set loader, budget split, persistence fold and
    ledger builder.  Both the in-budget primary and budget-deferred target must become
    superseded before even repository acquisition is attempted.
    """
    pid = "p"
    project = tmp_path / pid
    (project / "paper").mkdir(parents=True)
    (project / "discovery").mkdir(parents=True)
    (project / "paper" / "doc.json").write_text(
        '{"paper_id":"p","title":"mixed stop","sections":[]}', encoding="utf-8")

    paper_obj = DiscoveredObject(
        target_id="P0", kind="PRINTED_QUANTITY", centrality="CENTRAL",
        harness_addressable=True, routes=["ARITHMETIC_RECHECK"],
        materiality_basis="ABSTRACT_CLAIM", claim_text="printed total",
        status="PAPER_ARITHMETIC_CONTRADICTION")
    executable = [_obj(tid, material=False) for tid in ("T1", "T2")]
    paper_plan = PlanDecision(
        target_id="P0", action="PAPER_ONLY_RESOLUTION", route="ARITHMETIC_RECHECK",
        requires_execution=False, reason="deterministically recompute printed operands")
    plans = [paper_plan, *[_plan(obj.target_id) for obj in executable]]
    failure = TargetOutcome(
        target_id="P0", disposition="PAPER_ARITHMETIC_CONTRADICTION",
        action=paper_plan.action, route=paper_plan.route, provenance="paper",
        reason="the printed arithmetic is deterministically inconsistent")
    original = TargetSet(
        paper_id=pid, objects=[paper_obj, *executable], plans=plans, outcomes=[failure],
        extraction_coverage={"targets_superseded_by_established_failure": 0})
    probe_stage.state.write_json(project / "discovery" / "targets.json",
                                 original.model_dump())

    monkeypatch.setattr(probe_stage.state, "set_phase", lambda *a, **k: None)

    def forbidden(name):
        def call(*args, **kwargs):
            raise AssertionError(f"{name} entered after a material paper-only failure")
        return call

    monkeypatch.setattr(probe_stage, "build_spec", forbidden("build_spec"))
    monkeypatch.setattr(probe_stage, "acquire_and_audit", forbidden("acquire_and_audit"))
    monkeypatch.setattr(probe_stage, "_run", forbidden("_run"))

    result = probe_stage._review(Config(projects_dir=tmp_path, max_targets=1), pid)
    saved = TargetSet(**probe_stage.state.read_json(
        project / "discovery" / "targets.json"))
    got = _outcomes(saved)

    assert result["executions"] == 0
    assert result["superseded_targets"] == 2
    assert got["P0"].disposition == "PAPER_ARITHMETIC_CONTRADICTION"
    assert got["P0"].establishes_failure
    for tid in ("T1", "T2"):
        assert got[tid].disposition == "SUPERSEDED_BY_ESTABLISHED_FAILURE"
        assert got[tid].launched == 0
        assert "P0" in got[tid].reason
    assert {obj.target_id: obj.status for obj in saved.objects} == {
        "P0": "PAPER_ARITHMETIC_CONTRADICTION",
        "T1": "SUPERSEDED_BY_ESTABLISHED_FAILURE",
        "T2": "SUPERSEDED_BY_ESTABLISHED_FAILURE",
    }
    assert saved.extraction_coverage["targets_superseded_by_established_failure"] == 2

    # The report's target summary and machine ledger see all three terminal outcomes even
    # though the probe stage intentionally produced no execution artifact.
    from harness.stages import report as report_stage
    assert report_stage._targets_summary(saved.outcomes) == {
        "PAPER_ARITHMETIC_CONTRADICTION": 1,
        "SUPERSEDED_BY_ESTABLISHED_FAILURE": 2,
    }
    trace = ledger.build(EvalReport(paper_id=pid), saved)
    assert {entry.target_id for entry in trace.entries} == {"P0", "T1", "T2"}
    assert trace.efficiency["targets_discovered"] == 3
    assert trace.efficiency["targets_warranting_experiment"] == 2
    assert trace.efficiency["targets_launched"] == 0
    assert trace.efficiency["targets_settled"] == 1


def test_material_target_beyond_numeric_budget_is_still_pursued(monkeypatch, tmp_path):
    """The target cap limits ordinary work, not whether a material claim is checked."""
    pid = "p"
    project = tmp_path / pid
    (project / "paper").mkdir(parents=True)
    (project / "discovery").mkdir(parents=True)
    (project / "paper" / "doc.json").write_text(
        '{"paper_id":"p","title":"budget bypass","sections":[]}', encoding="utf-8")

    objects = [_obj(tid, material=(tid == "M4"))
               for tid in ("N1", "N2", "N3", "M4", "N5")]
    plans = [_plan(obj.target_id) for obj in objects]
    initial = TargetSet(paper_id=pid, objects=objects, plans=plans)
    probe_stage.state.write_json(project / "discovery" / "targets.json", initial.model_dump())

    monkeypatch.setattr(probe_stage.state, "set_phase", lambda *a, **k: None)
    monkeypatch.setattr(probe_stage.state, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        probe_stage, "build_spec",
        lambda cfg, p, doc, target=None: ProbeSpec(
            paper_id=p, target_id=target.target_id if target else ""))
    monkeypatch.setattr(probe_stage, "acquire_and_audit",
                        lambda *a, **k: (_ACQ, _AUDIT))
    monkeypatch.setattr(probe_stage, "synthesize_probe", lambda cfg, d, s, a: s)
    monkeypatch.setattr(probe_stage, "plan_execution",
                        lambda cfg, s, a, d, au, root=None: s)
    monkeypatch.setattr(probe_stage, "establish_comparison", lambda s, route, **kw: s)
    monkeypatch.setattr(probe_stage, "may_be_compared", lambda s: (True, ""))
    monkeypatch.setattr(probe_stage, "admissible_if_it_succeeds", lambda cfg, s: (True, ""))
    monkeypatch.setattr(probe_stage, "replan_after_author_code_exhausted",
                        lambda obj, plan, spec: None)

    launched = []

    def fake_run(cfg, root, spec, out_dir=None):
        launched.append(spec.target_id)
        return _result(pid, status="RESOLVED_VERIFIED", provenance="repo_exec")

    monkeypatch.setattr(probe_stage, "_run", fake_run)
    probe_stage._review(Config(projects_dir=tmp_path, max_targets=3), pid)
    saved = TargetSet(**probe_stage.state.read_json(
        project / "discovery" / "targets.json"))
    got = _outcomes(saved)

    assert launched == ["N1", "N2", "N3", "M4"]
    assert got["M4"].disposition == "REPRODUCED"
    assert got["M4"].disposition != "BUDGET_DEFERRED"
    assert got["N5"].disposition == "BUDGET_DEFERRED"
