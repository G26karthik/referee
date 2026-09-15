from __future__ import annotations

import json

from harness import evaluation, exhaustion, ledger, state
from harness.artifacts import (ClaimRef, DiscoveredObject, EvalReport, ExperimentIdentity,
                               PaperDoc, PlanDecision, ProbeResult, ProbeSpec, Reconciliation,
                               ReviewQuestion, TargetOutcome, TargetSet)
from harness.config import Config
from harness.stages import discover as discover_stage
from harness.stages import probe as probe_stage


def _target_set(route: str, outcome: TargetOutcome | None = None) -> TargetSet:
    ref = ClaimRef(ref="P0:0-20", quote="a central claim", resolution="resolved")
    q = ReviewQuestion(question_id="Q1", question="Does it hold?", materiality="CENTRAL",
                       claim_ref=ref, possible_resolution_routes=[route])
    obj = DiscoveredObject(
        target_id="T1", question_id="Q1", centrality="CENTRAL",
        materiality_basis="ABSTRACT_CLAIM", harness_addressable=True,
        ref=ref, routes=[route])
    plan = PlanDecision(
        target_id="T1", route=route,
        action=("PAPER_ONLY_RESOLUTION" if route == "PAPER_INTERNAL_CHECK"
                else "AUTHOR_CODE_REPRODUCTION"),
        requires_execution=route != "PAPER_INTERNAL_CHECK")
    return TargetSet(paper_id="p", questions=[q], objects=[obj], plans=[plan],
                     outcomes=[outcome] if outcome else [])


def test_missing_attempt_rows_stay_in_the_question_denominator():
    got = exhaustion.coverage([], ("Q1", "Q2"))
    assert got["questions"] == 2
    assert got["exhausted"] == 0
    assert got["open_because_untried"] == 2
    assert got["rate"] == 0.0


def test_citation_only_paper_check_exhausts_its_route_but_not_the_question_claim():
    out = TargetOutcome(
        target_id="T1", route="PAPER_INTERNAL_CHECK",
        action="PAPER_ONLY_RESOLUTION", disposition="CITATION_VERIFIED_ONLY",
        provenance="paper", reason="quotation exists; concern remains open")
    ts = exhaustion.refresh(_target_set("PAPER_INTERNAL_CHECK", out), Config())
    row = ts.route_attempts[0]
    assert row.attempted and row.completed
    assert row.exhausted
    assert row.state == "COMPLETED_INCONCLUSIVE"
    assert exhaustion.coverage(ts.route_attempts, tuple(ts.route_exhaustion_question_ids))["rate"] == 1.0


def test_paper_internal_route_is_not_lost_when_an_execution_route_was_planned():
    ts = _target_set("PAPER_INTERNAL_CHECK")
    ts.plans = []
    got = exhaustion.refresh(ts, Config()).route_attempts[0]
    assert got.attempted and got.completed and got.exhausted
    assert got.state == "COMPLETED_INCONCLUSIVE"


def test_a_shut_execution_gate_can_never_discharge_an_identity_blocker(tmp_path):
    cfg = Config(projects_dir=tmp_path / "projects")
    cfg.allow_repo_exec = False
    out = TargetOutcome(
        target_id="T1", route="AUTHOR_CODE_EXECUTION",
        action="AUTHOR_CODE_REPRODUCTION", disposition="IDENTITY_BLOCKED",
        launched=0, reason="identity was inspected while execution remained disabled")
    ts = exhaustion.refresh(_target_set("AUTHOR_CODE_EXECUTION", out), cfg)
    row = ts.route_attempts[0]
    assert row.state == "GATE_CLOSED"
    assert row.blocker == "AUTHORIZATION_BLOCKED"
    assert not row.completed and not row.exhausted
    got = exhaustion.coverage(ts.route_attempts, tuple(ts.route_exhaustion_question_ids))
    assert got["rate"] == 0.0
    assert got["open_because_of_this_harness"] == 1


def test_independently_rejected_reconstruction_exhausts_the_route():
    out = TargetOutcome(
        target_id="T1", route="INDEPENDENT_RECONSTRUCTION",
        action="INDEPENDENT_RECONSTRUCTION", disposition="INCONCLUSIVE",
        provenance="reimpl_exec", launched=0,
        reason="execution was not authorized ('conformance_unproven'): method not bound")
    cfg = Config(allow_reimplementation_driver=True, allow_reimplementation_exec=True)
    ts = exhaustion.refresh(_target_set("INDEPENDENT_RECONSTRUCTION", out), cfg)
    row = ts.route_attempts[0]
    assert row.state == "DISCHARGED_BLOCKED"
    assert row.blocker == "CONFORMANCE_BLOCKED"
    assert row.attempted and row.completed and row.exhausted


def test_cached_backend_spelling_does_not_turn_conformance_rejection_into_a_gate():
    out = TargetOutcome(
        target_id="T1", route="INDEPENDENT_RECONSTRUCTION",
        action="INDEPENDENT_RECONSTRUCTION", disposition="AUTHORIZATION_BLOCKED",
        provenance="reimpl_exec", launched=0,
        reason="backend refusal decision 'conformance_unproven': dataset not bound")
    cfg = Config(allow_reimplementation_driver=True, allow_reimplementation_exec=True)
    row = exhaustion.refresh(_target_set("INDEPENDENT_RECONSTRUCTION", out), cfg).route_attempts[0]
    assert row.state == "DISCHARGED_BLOCKED"
    assert row.blocker == "CONFORMANCE_BLOCKED"


def test_discovery_run_persists_route_attempt_rows(tmp_path, monkeypatch):
    cfg = Config(projects_dir=tmp_path / "projects")
    state.create_project(cfg, "", "fixture", pid="p")
    state.write_json(cfg.projects_dir / "p" / "paper" / "doc.json",
                     PaperDoc(paper_id="p", title="fixture").model_dump())
    seeded = _target_set("PAPER_INTERNAL_CHECK", TargetOutcome(
        target_id="T1", route="PAPER_INTERNAL_CHECK",
        action="PAPER_ONLY_RESOLUTION", disposition="CITATION_VERIFIED_ONLY",
        provenance="paper"))
    monkeypatch.setattr(discover_stage, "build", lambda *args, **kwargs: seeded)

    discover_stage.run(cfg, "p")
    raw = json.loads((cfg.projects_dir / "p" / "discovery" / "targets.json").read_text())
    assert raw["route_exhaustion_question_ids"] == ["Q1"]
    assert raw["route_attempts"][0]["state"] == "COMPLETED_INCONCLUSIVE"
    assert raw["route_attempts"][0]["source_refs"]


def test_probe_resync_refolds_and_persists_completed_route(tmp_path, monkeypatch):
    cfg = Config(projects_dir=tmp_path / "projects")
    cfg.allow_repo_exec = True
    state.create_project(cfg, "", "fixture", pid="p")
    ts = _target_set("AUTHOR_CODE_EXECUTION")
    state.write_json(cfg.projects_dir / "p" / "discovery" / "targets.json", ts.model_dump())
    result = ProbeResult(
        paper_id="p", provenance="repo_exec", executions=1, script_path="runs/p/execution.jsonl",
        reconciliation=Reconciliation(status="RESOLVED_VERIFIED", provenance="repo_exec"))
    state.write_json(cfg.projects_dir / "p" / "runs" / "p" / "probe_results.json",
                     result.model_dump())
    monkeypatch.setattr(
        probe_stage, "_executable_targets",
        lambda *_: (ts, [(ts.objects[0], ts.plans[0])], []))

    probe_stage.resync_cached_outcomes(cfg, "p")
    loaded = discover_stage.load(cfg, "p")
    assert loaded.route_attempts[0].state == "DISCHARGED_RAN"
    assert loaded.route_attempts[0].exhausted

    case = ledger.build(EvalReport(paper_id="p"), loaded)
    assert case.route_attempts[0].question_id == "Q1"
    assert case.efficiency["route_exhaustion"]["rate"] == 1.0


def test_probe_resync_preserves_runtime_author_code_fallback(tmp_path, monkeypatch):
    cfg = Config(projects_dir=tmp_path / "projects",
                 allow_repo_exec=True, allow_reimplementation_driver=True,
                 allow_reimplementation_exec=True)
    state.create_project(cfg, "", "fixture", pid="p")
    ts = _target_set("AUTHOR_CODE_EXECUTION")
    ts.objects[0].routes.append("INDEPENDENT_RECONSTRUCTION")
    root = cfg.projects_dir / "p"
    state.write_json(root / "discovery" / "targets.json", ts.model_dump())
    result = ProbeResult(paper_id="p", provenance="reimpl_exec", executions=0,
                         reconciliation=Reconciliation(
                             status="INCONCLUSIVE", failure_class="execution_unauthorized",
                             reason="decision 'conformance_unproven': method not bound"))
    state.write_json(root / "runs" / "p" / "probe_results.json", result.model_dump())
    state.write_json(root / "runs" / "p" / "spec.json", ProbeSpec(
        paper_id="p", experiment=ExperimentIdentity(state="no_candidate")).model_dump())
    state.write_json(root / "runs" / "p" / "targets" / "T1" / "outcome.json",
                     TargetOutcome(
                         target_id="T1", route="INDEPENDENT_RECONSTRUCTION",
                         action="INDEPENDENT_RECONSTRUCTION",
                         disposition="AUTHORIZATION_BLOCKED", provenance="reimpl_exec",
                         reason="decision 'conformance_unproven': method not bound").model_dump())
    monkeypatch.setattr(probe_stage, "_executable_targets",
                        lambda *_: (ts, [(ts.objects[0], ts.plans[0])], []))

    probe_stage.resync_cached_outcomes(cfg, "p")
    loaded = discover_stage.load(cfg, "p")
    assert {p.route for p in loaded.plans} == {
        "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"}
    assert all(row.exhausted for row in loaded.route_attempts)
    assert exhaustion.coverage(
        loaded.route_attempts, tuple(loaded.route_exhaustion_question_ids))["rate"] == 1.0


def test_central_question_bypasses_numeric_target_cap(tmp_path):
    cfg = Config(projects_dir=tmp_path / "projects", max_targets=1)
    state.create_project(cfg, "", "fixture", pid="p")
    ts = _target_set("AUTHOR_CODE_EXECUTION")
    second = ts.objects[0].model_copy(deep=True)
    second.target_id = "T2"
    second.materiality_basis = "NONE"
    ts.objects[0].materiality_basis = "NONE"
    p2 = ts.plans[0].model_copy(deep=True)
    p2.target_id = "T2"
    ts.objects.append(second)
    ts.plans.append(p2)
    state.write_json(cfg.projects_dir / "p" / "discovery" / "targets.json", ts.model_dump())
    _, pursued, deferred = probe_stage._executable_targets(cfg, "p")
    assert [o.target_id for o, _ in pursued] == ["T1", "T2"]
    assert deferred == []


def test_cached_secondary_without_legacy_result_gets_explicit_outcome(tmp_path, monkeypatch):
    cfg = Config(projects_dir=tmp_path / "projects")
    state.create_project(cfg, "", "fixture", pid="p")
    ts = _target_set("AUTHOR_CODE_EXECUTION")
    second = ts.objects[0].model_copy(deep=True)
    second.target_id = "T2"
    p2 = ts.plans[0].model_copy(deep=True)
    p2.target_id = "T2"
    ts.objects.append(second)
    ts.plans.append(p2)
    state.write_json(cfg.projects_dir / "p" / "discovery" / "targets.json", ts.model_dump())
    state.write_json(cfg.projects_dir / "p" / "runs" / "p" / "probe_results.json",
                     ProbeResult(paper_id="p", verdict="not_started").model_dump())
    monkeypatch.setattr(probe_stage, "_executable_targets",
                        lambda *_: (ts, list(zip(ts.objects, ts.plans)), []))
    probe_stage.resync_cached_outcomes(cfg, "p")
    loaded = discover_stage.load(cfg, "p")
    assert loaded.outcome_for("T2").disposition == "NOT_ATTEMPTED"
    assert "legacy harness persistence boundary" in loaded.outcome_for("T2").reason


def test_corpus_metrics_sum_question_and_route_denominators(monkeypatch, tmp_path):
    cfg = Config(projects_dir=tmp_path / "projects")
    rows = {
        "a": {"paper_id": "a", "triage": "GREEN", "verdict": "GREEN",
              "review_path": "PAPER_ONLY", "route_exhaustion": {
                  "questions": 2, "exhausted": 1, "open_because_untried": 1,
                  "open_because_of_this_harness": 0, "routes_applicable": 4,
                  "routes_attempted": 3, "routes_completed": 2, "routes_exhausted": 2}},
        "b": {"paper_id": "b", "triage": "YELLOW", "verdict": "GREEN",
              "review_path": "PAPER_AND_ARTIFACT", "route_exhaustion": {
                  "questions": 1, "exhausted": 0, "open_because_untried": 0,
                  "open_because_of_this_harness": 1, "routes_applicable": 2,
                  "routes_attempted": 1, "routes_completed": 0, "routes_exhausted": 0}},
    }
    monkeypatch.setattr(evaluation, "per_paper", lambda _cfg, pid: rows.get(pid))
    got = evaluation.corpus(cfg, ["a", "b"])["route_exhaustion"]
    assert got["questions"] == 3 and got["exhausted"] == 1
    assert got["routes_applicable"] == 6 and got["routes_exhausted"] == 2
    assert got["rate"] == round(1 / 3, 4)
