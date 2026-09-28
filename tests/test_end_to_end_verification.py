"""Regressions for the second round of "why REFEREE never verified or refuted a headline
claim": one-shot refusals with no revision path, a single model-picked proof step, quotes
left to the model, missing cited definitions, dead-end central concerns, and a case marked
complete while verification was still owed.

Synthetic documents only — no paper names, numbers or expected verdicts.
Run: `python -m pytest tests/test_end_to_end_verification.py -q`
"""
from __future__ import annotations

import json
from pathlib import Path

from harness import certificate, decide, discover, pipeline, reimplement_driver, routes, state
from harness.config import Config
from harness.schema import (
    CaseState, DiscoveredObject, Equation, PaperDoc, PlanDecision, ReimplementationIngredient,
    ReimplementationReadiness, ReviewQuestion, Section, Table, TargetOutcome,
)

STEP = "hence S_n <= 3 n holds termwise"


def _doc() -> PaperDoc:
    return PaperDoc(paper_id="t", title="A bound", sections=[
        Section(section_idx=0, title="Abstract",
                text="We prove a bound, developed in Section 2 of this work."),
        Section(section_idx=1, title="2. Main result",
                text="Definition 2.1. A sequence is admissible when every term is positive "
                     "and at most three. Theorem 2.2. For every admissible sequence of length "
                     "n, the partial sum satisfies S_n <= 3 n as in Eq. (4). Proof. By "
                     "Definition 2.1 each term is at most 3, " + STEP + "."),
    ], equations=[Equation(equation_idx=0, number="(4)", text="S_n = x_1 + ... + x_n")],
       tables=[Table(table_idx=0, label="1", caption="Accuracy", header=["m", "acc"],
                     rows=[["ours", "0.61"]])])


def _project(tmp_path: Path) -> tuple[Config, str]:
    cfg, pid = Config(projects_dir=tmp_path), "t"
    (state.project_dir(cfg, pid) / "paper").mkdir(parents=True, exist_ok=True)
    state.write_json(state.project_dir(cfg, pid) / "paper" / "doc.json", _doc().model_dump())
    return cfg, pid


def _claim() -> str:
    return next(s["text"] for s in discover.formal_statements(_doc()) if s["label"] == "Theorem 2.2")


# --- 1. critique becomes a bounded revision, never an open-ended loop ---------------------
def test_verdict_is_categorical_and_defaults_to_revise():
    assert reimplement_driver.parse_verdict('{"verdict": "APPROVE"}', True) == ("APPROVE", "")
    assert reimplement_driver.parse_verdict('{"verdict": "UNCHECKABLE"}', False)[0] == "UNCHECKABLE"
    assert reimplement_driver.parse_verdict('{"approved": false, "notes": "x"}', False)[0] == "REVISE"
    assert reimplement_driver.parse_verdict('{"verdict": "APPROVE"}', False)[0] == "REVISE"


def test_revision_rounds_are_bounded_and_keyed_to_the_base_brief(tmp_path):
    p = tmp_path / "r.json"
    kw = dict(base_sha="b", verdict="REVISE", required="fix x", notes="n", max_revisions=2)
    assert reimplement_driver.record_attempt(p, script="s1", **kw) == 1
    assert reimplement_driver.record_attempt(p, script="s2", **kw) == 2
    assert reimplement_driver.record_attempt(p, script="s3", **kw) == 3      # budget spent
    rev = reimplement_driver.read_revision(p, "b")
    assert rev["attempt"] == 2 and rev["script"] == "s2"                   # untouched: loop ends
    assert reimplement_driver.read_revision(p, "changed-upstream") == {}


def test_revision_brief_carries_the_rejected_attempt_and_changes_the_sha():
    doc, claim = _doc(), _claim()
    base = certificate.build_brief(doc, claim=claim)
    rev = certificate.build_brief(doc, claim=claim, revision={
        "attempt": 1, "script": "print('old')", "required_changes": "assert the strict bound"})
    assert base != rev and "print('old')" in rev and "assert the strict bound" in rev


def test_uncheckable_is_terminal_and_a_spent_budget_says_so(tmp_path):
    cfg, pid = _project(tmp_path)
    obj = DiscoveredObject(target_id="T")
    plan = PlanDecision(target_id="T", action="EXACT_CERTIFICATE", route="EXACT_CERTIFICATE",
                        requires_execution=True)
    sidecar = certificate._paths(cfg, pid, "T")[1]
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    state.write_json(sidecar, {"established": False, "verdict": "UNCHECKABLE", "attempt": 1,
                               "verifier_notes": "asymptotic, unstated constants"})
    out = routes._certificate_refusal(cfg, pid, obj, plan)
    assert out.disposition == "NO_ROUTE_AVAILABLE" and "UNCHECKABLE" in out.reason
    state.write_json(sidecar, {"established": False, "verdict": "REVISE",
                               "attempt": 1 + cfg.max_revisions})
    out = routes._certificate_refusal(cfg, pid, obj, plan)
    assert out.disposition == "NOT_ATTEMPTED" and "revision budget is spent" in out.reason


def test_reconstruction_verifier_reason_is_kept_not_discarded(tmp_path):
    cfg, pid = _project(tmp_path)
    ings = [ReimplementationIngredient(kind=k, required=True, present=True, ref="s1", quote="q")
            for k in reimplement_driver.REQUIRED_KINDS]
    reimplement_driver.accept_reimplementation(
        cfg, pid, "R", json.dumps({"script": "print(1)", "bindings": []}),
        ReimplementationReadiness(established=True, ingredients=ings),
        generated_by="g", mode="SESSION_SUBAGENT", verdict="REVISE",
        required_changes="apply the paper's leave-one-out split", verifier_notes="no split",
        attempt=1)
    rec = reimplement_driver.sealed_record(cfg, pid, "R")
    assert rec["verdict"] == "REVISE" and "leave-one-out" in rec["required_changes"]
    assert rec["verifier_notes"] == "no split"


# --- 2/3. quotes from the harness; cited definitions in the brief -------------------------
def test_brief_carries_verbatim_text_and_the_definitions_it_cites():
    b = certificate.build_brief(_doc(), claim=_claim())
    assert "VERBATIM PARSED TEXT" in b and "[Definition 2.1]" in b and "[Eq. (4)]" in b


def test_inline_proof_is_found_without_a_proof_of_heading():
    doc = _doc()
    assert certificate.proof_location(doc, "Theorem 2.2")[0] == 1
    assert "termwise" in certificate.proof_excerpt(doc, "Theorem 2.2")


def test_proof_map_keeps_only_steps_refound_in_the_proof_with_constants():
    proof = certificate.proof_excerpt(_doc(), "Theorem 2.2")
    raw = json.dumps({"steps": [
        {"quote": STEP, "explicit_constants": True},
        {"quote": "a sentence that is not in the proof at all", "explicit_constants": True},
        {"quote": "each term is at most 3", "explicit_constants": False}]})
    kept, dropped = certificate.parse_proof_map(raw, proof, max_steps=4)
    assert [k["quote"] for k in kept] == [STEP] and len(dropped) == 2


def test_each_mapped_step_becomes_its_own_bound_certificate_target():
    doc = _doc()
    parent = next(o for o in discover.build("t", doc, []).objects
                  if o.question_kind == "MATHEMATICAL_BOUND")
    ts = discover.build("t", doc, [], proof_maps={parent.target_id: {"steps": [{"quote": STEP}]}})
    child = next(o for o in ts.objects if o.parent_target == parent.target_id)
    assert child.prebound_quote == STEP
    assert certificate.statement_label(child.claim_text) == "Theorem 2.2"
    plan = next(p for p in ts.plans if p.target_id == child.target_id)
    assert plan.route == "EXACT_CERTIFICATE" and plan.requires_execution


def test_a_bound_step_is_what_gets_checked_whatever_the_generator_retyped(tmp_path):
    cfg, pid = _project(tmp_path)
    raw = json.dumps({"script": "print(1)\n", "bindings": [],
                      "paper_quotes": {"claimed_bound": "a retyped version"},
                      "checked_statement": "conclusion"})
    certificate.accept(cfg, pid, "T", raw, '{"approved": false, "verdict": "REVISE"}',
                       generated_by="g", reviewer="v", brief_sha256="bs", prebound=STEP,
                       base_sha="bs", max_revisions=2)
    conf = state.read_json(certificate._paths(cfg, pid, "T")[0])["conformance"]
    assert conf["scope"] == "proof_step"
    assert next(b for b in conf["bindings"] if b["kind"] == "claimed_bound")["paper_quote"] == STEP
    assert certificate.sealed_record(cfg, pid, "T")["attempt"] == 1
    assert certificate.revision_path(cfg, pid, "T").is_file()


# --- 4. dead-end central claims get a planned, harness-resolved check ---------------------
def test_check_plan_keeps_only_resolved_printed_quantities(tmp_path):
    cfg, pid = _project(tmp_path)
    routes.accept_check_plan(cfg, pid, "abstract", json.dumps({"proposals": [
        {"ref": "T0:r0:c1", "why": "headline accuracy"},
        {"quote": "a sentence the paper never prints 12", "why": "x"}]}),
        question_id=discover.ABSTRACT_QUESTION, brief="b")
    plan = routes.load_check_plans(cfg, pid)[0]
    assert [r["ref"] for r in plan["refs"]] == ["T0:r0:c1"] and len(plan["dropped"]) == 1


def test_a_planned_link_raises_priority_never_materiality():
    ts = discover.build("t", _doc(), [], check_plans=[
        {"question_id": discover.ABSTRACT_QUESTION, "refs": [{"ref": "T0:r0:c1"}]}])
    obj = next(o for o in ts.objects if o.planned_for)
    assert obj.question_id == discover.ABSTRACT_QUESTION and obj.materiality_basis == "NONE"
    q = next(q for q in ts.questions if q.question_id == discover.ABSTRACT_QUESTION)
    assert q.materiality == "CENTRAL"


# --- 5. end to end means end to end ---------------------------------------------------
def test_certificate_route_accounting_reads_what_actually_happened():
    q = ReviewQuestion(question_id="Q", materiality="CENTRAL")
    obj = DiscoveredObject(target_id="T", routes=["EXACT_CERTIFICATE"])
    plan = PlanDecision(target_id="T", action="EXACT_CERTIFICATE", route="EXACT_CERTIFICATE",
                        requires_execution=True)

    def state_of(**kw):
        out = TargetOutcome(target_id="T", route="EXACT_CERTIFICATE", **kw)
        return decide._attempt_for(q, "EXACT_CERTIFICATE", [obj], [plan], [out], Config())

    assert state_of(disposition="COUNTEREXAMPLE_ESTABLISHED", provenance="cert_exec",
                    launched=3).state == "DISCHARGED_RAN"
    assert state_of(disposition="NO_COUNTEREXAMPLE_FOUND", provenance="cert_exec",
                    launched=3).state == "COMPLETED_INCONCLUSIVE"
    refused = state_of(disposition="NO_ROUTE_AVAILABLE", reason=(
        "the independent verifier did not approve the certificate generated for this target "
        "(UNCHECKABLE; not finitely checkable)"))
    assert refused.state == "DISCHARGED_BLOCKED" and refused.blocker == "UNCHECKABLE"


def test_a_case_with_owed_verification_is_never_complete(tmp_path, monkeypatch):
    cfg = Config(projects_dir=tmp_path)
    case = CaseState(paper_id="p", phase="done", status="waiting")
    monkeypatch.setattr(pipeline, "open_verification", lambda cfg, pid: ["cert_gen:T"])
    held = pipeline.step(cfg, case)
    assert held.status == "waiting" and "cert_gen:T" in held.blocked_reason
    monkeypatch.setattr(pipeline, "open_verification", lambda cfg, pid: [])
    assert pipeline.step(cfg, held).status == "complete"


def test_released_data_brief_shows_rows_not_only_the_header(tmp_path):
    (tmp_path / "d.csv").write_text("label,score\n1,0.9\n0,0.2\n", encoding="utf-8")
    first = reimplement_driver.released_files(tmp_path)[0]["first_line"]
    assert "label,score" in first and "1,0.9" in first
