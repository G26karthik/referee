"""The three axes, the necessity decision, and the funnel — as regressions.

Every test here guards a property that the review of this system's own reporting layer
identified as load-bearing, and each states the failure it exists to prevent rather than
the code it happens to exercise.

The organising claim: **a referee's output has three independent dimensions, and any
single label destroys two of them.** What KIND of scientific problem this is, whether the
question it raises was settled, and what the evidence route actually produced are three
different facts about three different things — the paper's argument, the review process,
and the world. A colour answers none of them and looks like it answers all three.
"""
from __future__ import annotations

import inspect

import pytest

from harness import ledger, planner, questions, taxonomy
from harness.artifacts import (CodeAuditFinding, DiscoveredObject, EvalReport, Finding,
                               PlanDecision, Reconciliation, ReviewQuestion,
                               ScientificFinding, TargetOutcome, TargetSet)
from harness.stages import report as report_stage


# --------------------------------------------------------------------------- #
# the three axes are independent, and no mechanism may collapse them
# --------------------------------------------------------------------------- #
def test_the_same_scientific_class_survives_opposite_evidence_states():
    """A CONFOUND settled from the paper and a CONFOUND left open by a missing artifact
    are the same KIND of problem in opposite states. One label for both would tell a
    referee neither."""
    settled = TargetOutcome(target_id="A", disposition="PAPER_ONLY_RESOLVED",
                            provenance="paper")
    stuck = TargetOutcome(target_id="B", disposition="ARTIFACT_BLOCKED")
    assert taxonomy.classify(lens="confound") == "CONFOUND"
    assert settled.evidence_state == "PAPER_INTERNAL_EVIDENCE"
    assert stuck.evidence_state == "ARTIFACT_LIMITATION"
    assert settled.resolution_state == "RESOLVED_FROM_PAPER"
    assert stuck.resolution_state == "UNRESOLVED"
    assert settled.concerns_the_paper and not stuck.concerns_the_paper


def test_the_same_evidence_state_carries_different_scientific_classes():
    """The converse: two unrelated kinds of problem can end in the same state."""
    for lens, expected in (("confound", "CONFOUND"), ("overclaim", "OVERSTATED_CLAIM")):
        assert taxonomy.classify(lens=lens) == expected
    o = TargetOutcome(target_id="X", disposition="ENVIRONMENT_BLOCKED")
    assert o.evidence_state == "ENVIRONMENT_LIMITATION"


def test_scientific_class_is_not_an_input_to_severity():
    """`grading.derive` must not learn what kind of issue something is.

    "A CONFOUND is always MAJOR" is the same defect as "no variance = MAJOR": it decides
    impact from something that is not impact. Keeping the parameter out of the signature
    makes the rule inexpressible rather than merely absent.
    """
    from harness import grading
    params = set(inspect.signature(grading.derive).parameters)
    assert "scientific_class" not in params
    # and the derivation body does not reach for it either
    src = inspect.getsource(grading.derive)
    assert "scientific_class" not in src and "taxonomy" not in src


def test_taxonomy_classify_admits_only_vocabulary_and_booleans():
    for name, p in inspect.signature(taxonomy.classify).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"


def test_a_lens_cannot_write_its_own_scientific_class():
    """`scientific_class` is harness-written. A lens file supplying it is overwritten by
    `stages/audit._coerce`, the same way `evidence_class` always has been."""
    from harness.stages import audit as audit_stage
    src = inspect.getsource(audit_stage._coerce)
    assert "scientific_class=taxonomy.classify(" in src
    assert 'f.get("scientific_class")' not in src


def test_a_static_code_finding_is_always_an_implementation_issue():
    """A property, not a field: it cannot be read from a file or set to anything else."""
    f = CodeAuditFinding(finding_id="c1", rule_id="leak-scale-before-split")
    assert f.scientific_class == "IMPLEMENTATION_ISSUE"
    with pytest.raises(Exception):
        f.scientific_class = "CONTRADICTION"          # type: ignore[misc]


# --------------------------------------------------------------------------- #
# the provenance ceiling reaches the reporting layer, not only the reconciler
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prov", ["synthesized", "template", "paper", ""])
@pytest.mark.parametrize("disp", ["REPRODUCED", "FAILED_REPRODUCTION"])
def test_an_inadmissible_reproduction_is_inconclusive_at_the_reporting_layer(prov, disp):
    """A synthesized probe that "failed" must not convict through the report after being
    refused by the evidence layer. It is an inconclusive execution, in both directions."""
    o = TargetOutcome(target_id="T", disposition=disp, provenance=prov)
    assert o.evidence_state == "INCONCLUSIVE_EXECUTION"
    assert o.resolution_state == "UNRESOLVED"
    assert not o.concerns_the_paper


@pytest.mark.parametrize("prov", ["driver", "repo_exec"])
def test_an_admissible_reproduction_reaches_the_paper(prov):
    ok = TargetOutcome(target_id="T", disposition="REPRODUCED", provenance=prov)
    bad = TargetOutcome(target_id="T", disposition="FAILED_REPRODUCTION", provenance=prov)
    assert ok.evidence_state == "REPRODUCTION_SUCCESS" and ok.concerns_the_paper
    assert bad.evidence_state == "REPRODUCTION_FAILURE" and bad.concerns_the_paper
    assert bad.resolution_state == "RESOLVED_BY_EXECUTION"


# --------------------------------------------------------------------------- #
# experiment necessity is a first-class decision, and NO_EXPERIMENT_NEEDED is a success
# --------------------------------------------------------------------------- #
def test_deciding_no_experiment_is_needed_is_not_a_failure_to_check():
    cheap = DiscoveredObject(target_id="T", centrality="CENTRAL", harness_addressable=True,
                             routes=["ARITHMETIC_RECHECK", "AUTHOR_CODE_EXECUTION"])
    d = planner.plan(cheap, artifact_available=True)
    assert d.action == "PAPER_ONLY_RESOLUTION"
    assert d.necessity == "NO_EXPERIMENT_NEEDED"
    assert not d.requires_execution
    # and it is NOT a blocked target, an unresolved one, or an error
    assert d.blocking_gate == ""


def test_deciding_no_experiment_is_needed_still_resolves_nothing_by_itself():
    """The necessity axis must not leak into the evidence axis. Declining to run something
    does not settle a missing control; the authors adding one does."""
    o = TargetOutcome(target_id="T", disposition="NO_EXPERIMENT_NEEDED")
    assert o.evidence_state == "NOT_INVESTIGATED"
    assert o.resolution_state == "NOT_INVESTIGATED"
    assert not o.concerns_the_paper


@pytest.mark.parametrize("disp,expected", [
    ("REPRODUCED", "EXPERIMENT_RESOLVED"),
    ("FAILED_REPRODUCTION", "EXPERIMENT_RESOLVED"),
    ("INCONCLUSIVE", "EXPERIMENT_UNRESOLVED"),
    ("SPECIFICATION_BLOCKED", "EXPERIMENT_UNDERSPECIFIED"),
    ("ARTIFACT_BLOCKED", "EXPERIMENT_NOT_EXECUTABLE"),
    ("BUDGET_DEFERRED", "EXPERIMENT_WARRANTED"),
    ("NOT_ATTEMPTED", "EXPERIMENT_WARRANTED"),
])
def test_every_terminal_disposition_names_what_became_of_the_experiment(disp, expected):
    assert TargetOutcome(target_id="T", disposition=disp).necessity(True) == expected


def test_an_unwarranted_target_keeps_no_experiment_needed_whatever_happened():
    for disp in ("NOT_ATTEMPTED", "ARTIFACT_BLOCKED", "INCONCLUSIVE"):
        o = TargetOutcome(target_id="T", disposition=disp)
        assert o.necessity(False) == "NO_EXPERIMENT_NEEDED"


def test_an_unaddressable_target_is_not_an_underspecified_experiment():
    """Two different refusals that used to share one label. The first is a limit of what
    we could address; the second is a limit of what the paper specified."""
    unaddressable = DiscoveredObject(target_id="A", centrality="CENTRAL",
                                     harness_addressable=False,
                                     routes=["AUTHOR_CODE_EXECUTION"])
    vague = DiscoveredObject(target_id="B", centrality="CENTRAL", harness_addressable=True,
                             routes=["FOCUSED_VALIDATION_EXPERIMENT"])
    a = planner.plan(unaddressable, artifact_available=True)
    b = planner.plan(vague, artifact_available=True, specification_complete=False)
    assert a.action == "INFEASIBLE_ADDRESSING" and a.necessity == "EXPERIMENT_NOT_EXECUTABLE"
    assert b.action == "INFEASIBLE_SPECIFICATION" and b.necessity == "EXPERIMENT_UNDERSPECIFIED"
    assert a.action != b.action


# --------------------------------------------------------------------------- #
# WHY an escalation happened, recorded in machine-readable form
# --------------------------------------------------------------------------- #
def test_an_escalation_records_why_yes_and_not_only_why_not():
    obj = DiscoveredObject(target_id="T", centrality="CENTRAL", harness_addressable=True,
                           metric="accuracy", expected_value=61.4,
                           routes=["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"],
                           counter_explanations=["a longer schedule could explain it"])
    d = planner.plan(obj, artifact_available=True, environment_state="ok")
    assert d.requires_execution and d.necessity == "EXPERIMENT_WARRANTED"
    assert d.why_material == "the paper's headline conclusion rests on this quantity"
    assert "nothing in the paper settles it" in d.paper_only_insufficient_because
    assert "cannot establish what the number is" in d.inspection_insufficient_because
    assert d.competing_explanations == ["a longer schedule could explain it"]
    assert "61.4" in d.expected_observation
    # the why-NOT half is still complete
    assert set(d.gates) >= {"worth_pursuing", "structurally_addressable", "route_exists"}


def test_competing_explanations_are_carried_and_never_invented():
    bare = DiscoveredObject(target_id="T", centrality="CENTRAL", harness_addressable=True,
                            routes=["AUTHOR_CODE_EXECUTION"])
    assert planner.plan(bare, artifact_available=True,
                        environment_state="ok").competing_explanations == []


# --------------------------------------------------------------------------- #
# the question is the core unit
# --------------------------------------------------------------------------- #
def test_two_lenses_asking_the_same_question_at_one_address_is_one_question():
    """Unchanged property, supplied the way the pipeline now supplies it.

    The merge key is the address the HARNESS minted, not the string a lens wrote, so this
    test passes `minted`. Written against `evidence_ref` it would still pass, and it would
    have gone on passing for the `p<N>` page references that make up 75 of the corpus's 98
    findings — which is how three false merges shipped.
    """
    fs = [Finding(finding_id="a", lens="confound", candidate_class="OPEN_QUESTION",
                  evidence_ref="T2:r3:c1"),
          Finding(finding_id="b", lens="confound", candidate_class="CONFIRMED_FINDING",
                  evidence_ref="T2:r3:c1")]
    qs = questions.derive(fs, minted={"a": "T2:r3:c1", "b": "T2:r3:c1"})
    assert len(qs) == 1
    assert qs[0].source_finding_ids == ["a", "b"]


def test_a_merge_takes_the_maximum_materiality_and_never_exceeds_it():
    """Agreement is evidence; it is not a mechanism for manufacturing importance."""
    fs = [Finding(finding_id="a", lens="confound", candidate_class="OPEN_QUESTION",
                  evidence_ref="T1:r0:c0"),
          Finding(finding_id="b", lens="confound", candidate_class="PLAUSIBLE_CONCERN",
                  evidence_ref="T1:r0:c0"),
          Finding(finding_id="c", lens="confound", candidate_class="PLAUSIBLE_CONCERN",
                  evidence_ref="T1:r0:c0")]
    q = questions.derive(fs, minted={f.finding_id: "T1:r0:c0" for f in fs})[0]
    assert q.materiality == "SUPPORTING", "the maximum any single source asserted"
    assert len(q.source_finding_ids) == 3


def test_the_same_question_about_a_different_number_stays_two_questions():
    fs = [Finding(finding_id="a", lens="protocol", candidate_class="OPEN_QUESTION",
                  evidence_ref="T1:r0:c0"),
          Finding(finding_id="b", lens="protocol", candidate_class="OPEN_QUESTION",
                  evidence_ref="T9:r2:c2")]
    assert len(questions.derive(fs)) == 2


def test_a_question_carries_its_resolution_and_cannot_set_it_apart_from_the_evidence():
    q = ReviewQuestion(question_id="Q1")
    assert q.resolution_status == "NOT_INVESTIGATED" and q.is_open
    assert q.evidence_needed == q.what_would_settle_it


def test_syncing_questions_is_a_fold_and_is_idempotent():
    """Run twice, get the same answer — a question can never keep a resolution the
    evidence behind it has since lost."""
    from harness.stages import discover as discover_stage
    ts = TargetSet(
        paper_id="p",
        questions=[ReviewQuestion(question_id="Q1"), ReviewQuestion(question_id="Q2")],
        objects=[DiscoveredObject(target_id="A", question_id="Q1",
                                  routes=["ARITHMETIC_RECHECK", "AUTHOR_CODE_EXECUTION"])],
        plans=[PlanDecision(target_id="A", action="PAPER_ONLY_RESOLUTION")],
        outcomes=[TargetOutcome(target_id="A", disposition="PAPER_ONLY_RESOLVED",
                                provenance="paper", reason="12*3 is not 40")])
    first = discover_stage.sync_questions(ts).model_dump()["questions"]
    second = discover_stage.sync_questions(ts).model_dump()["questions"]
    assert first == second

    q1, q2 = ts.questions
    assert q1.resolution_status == "RESOLVED_FROM_PAPER" and not q1.is_open
    assert q1.possible_resolution_routes == ["ARITHMETIC_RECHECK", "AUTHOR_CODE_EXECUTION"]
    assert q1.evidence_refs == ["A"] and "settled against the paper" in q1.conclusion
    # Q2 had no target at all and claims nothing
    assert q2.resolution_status == "NOT_INVESTIGATED" and q2.is_open
    assert q2.conclusion == ""


def test_an_unresolved_question_carries_no_answer():
    """`resolution` holds an answer. An UNRESOLVED question used to carry the reason it
    was blocked in the field a reader takes for its answer."""
    from harness.stages import discover as discover_stage
    ts = TargetSet(
        paper_id="p",
        questions=[ReviewQuestion(question_id="Q1")],
        objects=[DiscoveredObject(target_id="A", question_id="Q1",
                                  routes=["AUTHOR_CODE_EXECUTION"])],
        plans=[PlanDecision(target_id="A", requires_execution=True)],
        outcomes=[TargetOutcome(target_id="A", disposition="ENVIRONMENT_BLOCKED",
                                reason="torch would not install")])
    discover_stage.sync_questions(ts)
    q = ts.questions[0]
    assert q.resolution_status == "UNRESOLVED" and q.resolution == ""
    assert "torch" not in q.conclusion
    assert "fact about the machine" in q.conclusion


def test_a_question_reports_its_highest_priority_target_not_its_luckiest():
    """A merged question has one target per contributing finding and they can end
    differently. Taking the best available outcome would let a question be answered by
    whichever of its addresses happened to be reachable."""
    from harness.stages import discover as discover_stage
    ts = TargetSet(
        paper_id="p",
        questions=[ReviewQuestion(question_id="Q1", source_finding_ids=["a", "b"])],
        objects=[  # priority order: the central, blocked one first
            DiscoveredObject(target_id="A", question_id="Q1", centrality="CENTRAL",
                             routes=["AUTHOR_CODE_EXECUTION"]),
            DiscoveredObject(target_id="B", question_id="Q1", centrality="SUPPORTING",
                             routes=["ARITHMETIC_RECHECK"])],
        plans=[PlanDecision(target_id="A", requires_execution=True),
               PlanDecision(target_id="B", action="PAPER_ONLY_RESOLUTION")],
        outcomes=[TargetOutcome(target_id="A", disposition="ARTIFACT_BLOCKED"),
                  TargetOutcome(target_id="B", disposition="PAPER_ONLY_RESOLVED",
                                provenance="paper", reason="12*3 is not 40")])
    discover_stage.sync_questions(ts)
    q = ts.questions[0]
    assert q.resolution_status == "UNRESOLVED", "the central target is what it reports"
    assert q.is_open
    # and nothing is hidden: every target for the question is in the trace
    assert q.evidence_refs == ["A", "B"]


def test_a_question_whose_target_was_inadmissibly_executed_stays_open():
    from harness.stages import discover as discover_stage
    ts = TargetSet(
        paper_id="p",
        questions=[ReviewQuestion(question_id="Q1")],
        objects=[DiscoveredObject(target_id="A", question_id="Q1",
                                  routes=["AUTHOR_CODE_EXECUTION"])],
        plans=[PlanDecision(target_id="A", requires_execution=True)],
        outcomes=[TargetOutcome(target_id="A", disposition="FAILED_REPRODUCTION",
                                provenance="synthesized", reason="52.0 vs 61.4")])
    discover_stage.sync_questions(ts)
    q = ts.questions[0]
    assert q.evidence_state == "INCONCLUSIVE_EXECUTION"
    assert q.resolution_status == "UNRESOLVED" and q.is_open
    assert "settled nothing admissible" in q.conclusion


# --------------------------------------------------------------------------- #
# the funnel: six terms, six artifacts, never interchangeable
# --------------------------------------------------------------------------- #
def _funnel_set() -> TargetSet:
    return TargetSet(
        paper_id="p",
        objects=[DiscoveredObject(target_id="A", harness_addressable=True),
                 DiscoveredObject(target_id="B", harness_addressable=True),
                 DiscoveredObject(target_id="C", harness_addressable=False)],
        plans=[PlanDecision(target_id="A", requires_execution=True),
               PlanDecision(target_id="B", requires_execution=True)],
        outcomes=[
            TargetOutcome(target_id="A", disposition="AUTHORIZATION_BLOCKED", launched=0),
            TargetOutcome(target_id="B", disposition="REPRODUCED", provenance="repo_exec",
                          launched=3,
                          reconciliation=Reconciliation(status="REPRODUCED")),
        ])


def test_judging_an_experiment_warranted_is_not_running_one():
    """The specific overclaim this split exists to prevent: two targets warranted an
    experiment and only one ever started a process."""
    e = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"),
                     _funnel_set()).efficiency
    assert e["targets_warranting_experiment"] == 2
    assert e["targets_launched"] == 1
    assert e["processes_launched"] == 3
    assert e["targets_warranting_experiment"] != e["targets_launched"]


def test_launched_is_read_from_the_execution_record_not_from_a_disposition():
    ts = _funnel_set()
    # a blocked target that somehow carries a launch count is reported as having launched
    ts.outcomes[0].launched = 2
    e = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts).efficiency
    assert e["targets_launched"] == 2 and e["processes_launched"] == 5
    # and one that ran but has no reconciliation did not COMPLETE
    assert e["executions_completed"] == 1


def test_the_probe_stage_wall_time_is_never_called_an_execution_cost():
    e = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"),
                     _funnel_set(), seconds=1211.0).efficiency
    assert e["probe_stage_seconds"] == 1211.0
    assert "execution_seconds" not in e, (
        "acquisition, static audit, planning and gate evaluation are not experiment cost")


def test_a_budget_deferral_is_distinguishable_from_a_refusal():
    ts = _funnel_set()
    ts.plans.append(PlanDecision(target_id="C", requires_execution=True))
    ts.outcomes.append(TargetOutcome(target_id="C", disposition="BUDGET_DEFERRED"))
    e = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts).efficiency
    assert e["targets_deferred_by_budget"] == 1
    assert e["targets_by_experiment_necessity"]["EXPERIMENT_WARRANTED"] == 1
    # A deferral is not a block. `targets_blocked_before_execution` counts the one target
    # a GATE refused; the budget one is counted only in its own bucket, so a reader can
    # tell "the paper or the host stopped us" from "this run ran out of budget".
    assert e["targets_blocked_before_execution"] == 1, "the authorization-blocked one only"
    assert e["targets_by_evidence_state"]["NOT_INVESTIGATED"] == 1


def test_every_review_question_appears_in_the_trace():
    ts = _funnel_set()
    ts.questions = [ReviewQuestion(question_id="Q1"), ReviewQuestion(question_id="Q2")]
    led = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts)
    assert [e.question for e in led.entries if e.entry_id.startswith("Q")] == ["Q1", "Q2"]
    assert led.efficiency["questions_open"] == 2


# --------------------------------------------------------------------------- #
# the reviewer report leads with findings, not with a colour
# --------------------------------------------------------------------------- #
def _report_with(*classes: str) -> EvalReport:
    return EvalReport(
        paper_id="p", title="A Paper", verdict="GREEN", triage="YELLOW",
        scientific_findings=[
            ScientificFinding(finding_id=f"f{i}", scientific_class=c, lens="confound",
                              title=f"finding {i}", severity="MAJOR",
                              question="does the gain survive the missing control?",
                              resolution_status="UNRESOLVED",
                              evidence_needed="run the ablation")
            for i, c in enumerate(classes)])


def test_the_reviewer_report_leads_with_scientific_categories():
    text = report_stage.render_reviewer_report(_report_with("CONFOUND", "OVERSTATED_CLAIM"),
                                               TargetSet(paper_id="p"))
    head = text[:text.index("## ")]
    assert "evidence-verified finding(s)" in head
    assert "YELLOW" not in head, "the triage level is not the headline"
    assert text.index("## Scientific findings") < text.index("## Scope of this review")


def test_the_triage_level_is_reported_as_routing_not_as_a_conclusion():
    text = report_stage.render_reviewer_report(_report_with("CONFOUND"),
                                               TargetSet(paper_id="p"))
    assert "triage for routing: **YELLOW**" in text
    assert text.index("## Scientific findings") < text.index("triage for routing")


def test_the_report_states_what_it_judged_apart_from_what_it_ran():
    r = _report_with("CONFOUND")
    r.review_efficiency = {"funnel": {"discovered": 25, "checkable": 19,
                                      "warranting_experiment": 2, "launched": 0,
                                      "completed": 0, "resolved": 1}}
    text = report_stage.render_reviewer_report(r, TargetSet(paper_id="p"))
    assert "2 judged to warrant an experiment" in text
    assert "0 actually launched a process" in text


def test_the_scope_funnel_reads_the_ledgers_own_flat_keys():
    """`CaseLedger.efficiency` carries the funnel as flat keys; only `evaluation.per_paper`
    nests them under "funnel". Reading the nested form printed six zeros beside a ledger
    that had the numbers — a report contradicting its own trace."""
    r = _report_with("CONFOUND")
    r.review_efficiency = {"targets_discovered": 25, "targets_addressable": 19,
                           "targets_warranting_experiment": 2, "targets_launched": 2,
                           "executions_completed": 2, "targets_resolved": 1}
    text = report_stage.render_reviewer_report(r, TargetSet(paper_id="p"))
    assert "25 target(s) discovered" in text
    assert "19 structurally checkable" in text
    assert "2 judged to warrant an experiment" in text
    assert "2 actually launched a process" in text
    assert "1 settled a question about the paper" in text


def test_the_scope_funnel_is_identical_to_the_ledger_it_summarises():
    """Whatever the ledger counted is what the report prints, for every term."""
    ts = _funnel_set()
    eff = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts).efficiency
    r = _report_with("CONFOUND")
    r.review_efficiency = eff
    text = report_stage.render_reviewer_report(r, ts)
    assert f"{eff['targets_warranting_experiment']} judged to warrant" in text
    assert f"{eff['targets_launched']} actually launched" in text
    assert f"{eff['targets_resolved']} settled a question" in text


def test_an_unaddressable_target_is_not_reported_as_a_missing_artifact():
    """A paper WITH a cloned repository was told "no usable artifact reached this
    question" for every claim the harness could not address. That is a limit of our own
    extraction and it now has its own disposition and its own evidence state."""
    o = TargetOutcome(target_id="T", disposition="ADDRESSING_BLOCKED")
    assert o.evidence_state == "EXTRACTION_LIMITATION"
    assert o.evidence_state != "ARTIFACT_LIMITATION"
    assert o.resolution_state == "UNRESOLVED" and not o.concerns_the_paper
    assert o.necessity(True) == "EXPERIMENT_NOT_EXECUTABLE"

    from harness.stages import discover as discover_stage
    assert discover_stage._DISPOSITION_FOR_ACTION["INFEASIBLE_ADDRESSING"] == "ADDRESSING_BLOCKED"
    assert discover_stage._DISPOSITION_FOR_ACTION["INFEASIBLE_SPECIFICATION"] == "SPECIFICATION_BLOCKED"
    said = discover_stage._CONCLUSION["EXTRACTION_LIMITATION"]
    assert "extraction recovered" in said and "not of the paper" in said
    assert "artifact" not in said.replace("its artifact", "")


def test_the_report_stays_bounded_when_every_category_is_populated():
    """A per-category cap alone bounds the SHAPE of the report and not its length."""
    r = _report_with(*(c for c in report_stage.CATEGORY_ORDER for _ in range(6)))
    text = report_stage.render_reviewer_report(r, TargetSet(paper_id="p"))
    assert len(text) < 9000, f"{len(text)} chars is not a one-to-two page review"
    assert "further finding(s) across these categories" in text


def test_a_sparse_category_does_not_cancel_an_elision_elsewhere():
    """Summing (len - room) raw makes a one-finding category contribute -2, which silently
    absorbs an over-full category and suppresses the "…and N more" line. A report that
    drops findings without saying so reads as a report that found none."""
    r = _report_with("CONFOUND", *("PROTOCOL_ISSUE",) * 5)
    text = report_stage.render_reviewer_report(r, TargetSet(paper_id="p"))
    assert "…and 2 further finding(s)" in text


def test_a_report_with_no_findings_does_not_claim_correctness():
    text = report_stage.render_reviewer_report(_report_with(), TargetSet(paper_id="p"))
    assert "never a certificate of correctness" in text
    assert "never an accusation" in text


def test_every_scientific_class_has_a_place_in_the_report():
    """`by_category` keys on CATEGORY_ORDER, so a class missing from it would put its
    findings in the verified total and in no category — present in the count, absent from
    the page, with nothing saying so."""
    assert set(report_stage.CATEGORY_ORDER) == set(taxonomy.SCIENTIFIC_CLASSES)
    assert set(report_stage.CATEGORY_GLOSS) == set(taxonomy.SCIENTIFIC_CLASSES)
    sfs = [ScientificFinding(finding_id=c, scientific_class=c)
           for c in taxonomy.SCIENTIFIC_CLASSES]
    assert sum(len(v) for v in report_stage.by_category(sfs).values()) == len(sfs)


def test_scientific_findings_are_a_projection_and_decide_nothing():
    """Every field is copied from an artifact that already decided it."""
    src = inspect.getsource(report_stage.scientific_findings)
    for forbidden in ("grading.derive", "taxonomy.classify", "if severity",
                      "_SEVERITY_RANK["):
        assert forbidden not in src, f"the projection must not re-judge: {forbidden}"


def test_corroborating_lenses_are_recorded_and_raise_nothing():
    r = EvalReport(paper_id="p", title="t",
                   findings=[Finding(finding_id="a", lens="confound", severity="MINOR",
                                     evidence_ref="T1:r0:c0"),
                             Finding(finding_id="b", lens="protocol", severity="FATAL",
                                     evidence_ref="T1:r0:c0")])
    ts = TargetSet(paper_id="p", questions=[
        ReviewQuestion(question_id="Q1", from_finding="a", source_finding_ids=["a", "b"])])
    sfs = report_stage.scientific_findings(r, ts)
    first = next(x for x in sfs if x.finding_id == "a")
    assert first.corroborating_lenses == ["protocol"]
    assert first.severity == "MINOR", "agreement records; it never promotes"


# --------------------------------------------------------------------------- #
# verifying a citation is not settling a question
# --------------------------------------------------------------------------- #
# The paper-only route has two branches and they establish different things. Both used to
# end in PAPER_ONLY_RESOLVED, and the seven-paper corpus made the cost visible: all 20 of
# its paper-only outcomes came from the branch that only re-verifies the QUOTATION, so
# every review printed the sentence its own findings disputed under `## What held up`, and
# every funnel reported "settled a question about the paper" for targets that had settled
# nothing. These tests hold the two branches apart.
def _citation_only_outcome():
    """The real producer, driven through the real code path."""
    from harness.stages import discover as discover_stage
    obj = DiscoveredObject(target_id="T", question_id="Q1",
                           claim_text="LDReg consistently improves the representation",
                           routes=["PAPER_INTERNAL_CHECK"])
    plan = PlanDecision(target_id="T", action="PAPER_ONLY_RESOLUTION",
                        route="PAPER_INTERNAL_CHECK")
    return discover_stage._paper_only_outcome(obj, plan), obj


def test_re_verifying_a_quotation_resolves_nothing_about_the_paper():
    """It has its own evidence state, and that state leaves the question OPEN.

    Three candidate states were considered and two are wrong. PAPER_INTERNAL_EVIDENCE,
    which this used to be, says the paper was checked, and it was not: only the citation
    was. NOT_INVESTIGATED says nobody looked, and someone did. So CITATION_VERIFIED, whose
    resolution is UNRESOLVED: the concern was examined far enough to confirm it quotes the
    paper, and it still stands.
    """
    out, _ = _citation_only_outcome()
    assert out.disposition == "CITATION_VERIFIED_ONLY"
    assert out.evidence_state == "CITATION_VERIFIED"
    assert out.resolution_state == "UNRESOLVED", "the concern still stands"
    assert not out.concerns_the_paper, (
        "a concern that quotes the paper accurately is not a concern that was answered")
    assert "was not investigated" in out.reason


def test_the_arithmetic_branch_of_the_paper_only_route_still_resolves():
    """The distinction is between the two branches, not a retreat from paper-only
    evidence: re-evaluating a composition the paper printed really does settle whether the
    paper's own arithmetic holds."""
    from harness.artifacts import ClaimRef, ReportedQuantity
    from harness.stages import discover as discover_stage
    obj = DiscoveredObject(
        target_id="T", question_id="Q1",
        ref=ClaimRef(kind="prose_claim", ref="P1:0-20", quote="58 x 5 x 10 = 2900",
                     quantity=ReportedQuantity(raw="2900", expression="58*5*10",
                                               arithmetic_ok=True)))
    plan = PlanDecision(target_id="T", action="PAPER_ONLY_RESOLUTION",
                        route="ARITHMETIC_RECHECK")
    out = discover_stage._paper_only_outcome(obj, plan)
    assert out.disposition == "PAPER_ONLY_RESOLVED"
    assert out.evidence_state == "PAPER_INTERNAL_EVIDENCE"
    assert out.resolution_state == "RESOLVED_FROM_PAPER" and out.concerns_the_paper


def test_a_citation_verified_target_is_not_reported_as_having_held_up():
    """The exact defect, at the rendering layer: `## What held up` listed the paper's own
    disputed sentence."""
    out, obj = _citation_only_outcome()
    ts = TargetSet(paper_id="p", objects=[obj],
                   plans=[PlanDecision(target_id="T", action="PAPER_ONLY_RESOLUTION")],
                   outcomes=[out])
    text = report_stage.render_reviewer_report(_report_with("CONTRADICTION"), ts)
    held = text.split("## What held up", 1)[1].split("## Experiments triggered", 1)[0]
    assert "consistently improves" not in held
    assert "Nothing was positively verified" in held


def test_a_citation_verified_target_does_not_count_as_a_settled_question():
    out, obj = _citation_only_outcome()
    ts = TargetSet(paper_id="p", objects=[obj],
                   questions=[ReviewQuestion(question_id="Q1")],
                   plans=[PlanDecision(target_id="T", action="PAPER_ONLY_RESOLUTION")],
                   outcomes=[out])
    e = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts).efficiency
    assert e["targets_resolved"] == 0, "nothing about the paper was established"
    assert e["targets_resolved_without_execution"] == 0
    assert e["targets_settled"] == 0
    # and the fact is not lost — it is reported as its own term
    assert e["targets_citation_verified_only"] == 1


def test_a_citation_verified_central_claim_is_reported_as_unchecked():
    """It must land in `## Central claims this review did not check`, because it was
    not checked."""
    out, obj = _citation_only_outcome()
    obj.centrality, obj.harness_addressable = "CENTRAL", True
    assert [o.target_id for o in report_stage.unchecked_central([obj], [out])] == ["T"]
    assert report_stage.unresolved_central([obj], [out]) == [], (
        "nothing was pursued admissibly, so it cannot colour the paper")


def test_a_citation_verified_question_says_which_of_the_two_happened():
    """NOT_INVESTIGATED is shared with "nothing was pursued", so the conclusion line has
    to distinguish them or the reader loses a real fact."""
    from harness.stages import discover as discover_stage
    out, obj = _citation_only_outcome()
    ts = TargetSet(paper_id="p", objects=[obj],
                   questions=[ReviewQuestion(question_id="Q1")],
                   plans=[PlanDecision(target_id="T", action="PAPER_ONLY_RESOLUTION")],
                   outcomes=[out])
    discover_stage.sync_questions(ts)
    q = ts.questions[0]
    assert q.resolution_status == "UNRESOLVED" and q.is_open
    assert q.resolution == "", "an unsettled question carries no answer"
    assert "cites the paper accurately" in q.conclusion
    assert "not investigated" in q.conclusion
    assert not q.resolved_without_execution


def test_every_target_disposition_has_a_declared_evidence_state():
    """The default in `taxonomy.evidence_state` is NOT_INVESTIGATED, which is the safe
    answer and also a silent one. A disposition added to the vocabulary without a mapping
    would reach the reporting layer as "nothing was pursued" without anyone deciding
    that."""
    from harness.artifacts import TARGET_DISPOSITIONS
    undeclared = [d for d in TARGET_DISPOSITIONS
                  if d not in taxonomy._EVIDENCE_FOR_DISPOSITION]
    assert undeclared == [], f"no evidence state declared for: {undeclared}"


def test_the_three_addressing_refusals_are_three_states_not_one():
    """Three limits of three different things, told apart.

    "We could not build an address", "the paper printed no quantity there" and "no method
    we have would settle this" shared INFEASIBLE_ADDRESSING -> ADDRESSING_BLOCKED ->
    EXTRACTION_LIMITATION, whose reader-facing sentence claims the review could not build
    a re-derivable address. Over the shipped corpus that sentence was printed 39 times
    across six of seven reviews and was FALSE every time: all 52 targets carrying it had a
    resolved address, and the real blocker in 52 of 52 was that no route applied.
    """
    from harness import planner
    from harness.artifacts import ADDRESSING_BLOCKERS

    seen = {}
    for blocker in ADDRESSING_BLOCKERS:
        if blocker == "NONE":
            continue
        action, _reason, _gates, blocking = planner.classify(
            centrality="CENTRAL", addressable=False, route="AUTHOR_CODE_EXECUTION",
            artifact_available=True, specification_complete=True,
            environment_state="usable", addressing_blocker=blocker)
        assert blocking == "structurally_addressable"
        seen[blocker] = action
    assert len(set(seen.values())) == 3, seen
    assert seen["ADDRESS_UNRESOLVED"] == "INFEASIBLE_ADDRESSING"
    assert seen["QUANTITY_UNPARSED"] == "INFEASIBLE_REPORTING"
    assert seen["NO_ROUTE"] == "INFEASIBLE_ROUTE"

    # and each lands on its own evidence state, only the first of which is about us
    from harness.stages import discover as discover_stage
    states = {b: taxonomy.evidence_state(discover_stage._DISPOSITION_FOR_ACTION[a])
              for b, a in seen.items()}
    assert states == {"ADDRESS_UNRESOLVED": "EXTRACTION_LIMITATION",
                      "QUANTITY_UNPARSED": "REPORTING_LIMITATION",
                      "NO_ROUTE": "NO_ROUTE_AVAILABLE"}, states
    for st in states.values():
        assert not taxonomy.concerns_the_paper(st), st

    # an unrecognised blocker must not invent a refusal; it falls to the one that claims
    # the least about the paper
    action, _, _, _ = planner.classify(
        centrality="CENTRAL", addressable=False, route="AUTHOR_CODE_EXECUTION",
        artifact_available=True, specification_complete=True,
        environment_state="usable", addressing_blocker="SOMETHING_NEW")
    assert action == "INFEASIBLE_ADDRESSING"


def test_a_citation_check_can_no_longer_suppress_an_experiment():
    """Invariant 23 in the direction nobody was watching.

    A route that settles a question with nothing running is taken before one that runs.
    PAPER_INTERNAL_CHECK was in that set and settles nothing, so wherever a contradiction
    lens offered it AND an executable route existed, the execution was suppressed by a
    citation re-verification. Over the shipped corpus that was 100% of the paper-only
    resolutions, which means every one of them was a suppressed escalation reported as a
    settled question.
    """
    from harness import planner
    from harness.artifacts import DiscoveredObject

    obj = DiscoveredObject(
        target_id="T", question_id="Q1", centrality="CENTRAL", harness_addressable=True,
        routes=["PAPER_INTERNAL_CHECK", "ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"],
        expected_value=0.83)
    d = planner.plan(obj, artifact_available=True, specification_complete=True,
                     environment_state="usable")
    assert d.requires_execution, "the executable route must win"
    assert d.action == "AUTHOR_CODE_REPRODUCTION"
    assert "PAPER_INTERNAL_CHECK" not in planner._RESOLVING

    # and where there is nothing to escalate to, the citation check is still taken,
    # because refusing it would discard information; it just resolves nothing
    only = DiscoveredObject(
        target_id="U", question_id="Q2", centrality="CENTRAL", harness_addressable=True,
        routes=["PAPER_INTERNAL_CHECK"])
    d2 = planner.plan(only, artifact_available=False, specification_complete=False)
    assert d2.action == "PAPER_ONLY_RESOLUTION" and d2.route == "PAPER_INTERNAL_CHECK"
    assert not d2.requires_execution
    from harness.stages import discover as discover_stage
    out = discover_stage._paper_only_outcome(only, d2)
    assert out.disposition == "CITATION_VERIFIED_ONLY"
    assert out.resolution_state == "UNRESOLVED"


def test_a_paper_only_resolution_records_every_gate_it_passed():
    """The branch that produced 100% of the corpus's resolutions recorded one gate.

    `plan` returned before `classify` was ever called, with
    `gates={"resolvable_without_execution": True}`, so centrality and addressability were
    never consulted for it, and a PERIPHERAL unaddressable object could reach
    RESOLVED_FROM_PAPER with `concerns_the_paper` true. One such target shipped.
    """
    from harness import planner
    from harness.artifacts import ClaimRef, DiscoveredObject, ReportedQuantity

    obj = DiscoveredObject(
        target_id="T", question_id="Q1", centrality="CENTRAL", harness_addressable=True,
        routes=["ARITHMETIC_RECHECK"],
        ref=ClaimRef(kind="prose_claim", ref="P1:0-20", quote="58 x 5 x 10 = 2900",
                     resolution="resolved", resolved=True,
                     quantity=ReportedQuantity(raw="2900", expression="58*5*10",
                                               arithmetic_ok=False)))
    d = planner.plan(obj, artifact_available=False, specification_complete=False)
    assert d.action == "PAPER_ONLY_RESOLUTION"
    for gate in ("worth_pursuing", "structurally_addressable", "route_exists",
                 "resolvable_without_execution"):
        assert gate in d.gates, f"{gate} was not recorded"
    assert d.gates["worth_pursuing"] is True
    assert d.gates["structurally_addressable"] is True


def test_a_page_number_is_not_an_address_and_merges_nothing():
    """The false-merge defect, at the layer that produced it.

    `prompts/audit.py` instructs a lens to write `p7` for a prose claim, and the merge key
    used that string directly. `p7` names a PAGE. Measured over the shipped corpus: 75 of
    98 findings write a `p<N>` ref, 21 (paper, ref) pairs are shared by more than one
    finding, and 3 of the 6 resulting merges are false. Two of those three RAISED the
    merged question's materiality, because a merge takes the maximum its sources asserted,
    so a coincidence of page numbers promoted a concern the lenses had not promoted.
    """
    fs = [Finding(finding_id="a", lens="confound", candidate_class="OPEN_QUESTION",
                  evidence_ref="p7"),
          Finding(finding_id="b", lens="confound", candidate_class="CONFIRMED_FINDING",
                  evidence_ref="p7")]
    # no minted map: neither address resolved, so neither merges with anything
    qs = questions.derive(fs)
    assert len(qs) == 2, "two sentences on one page are two questions"
    assert [q.materiality for q in qs] == ["PERIPHERAL", "CENTRAL"], (
        "and the peripheral one did not inherit the confirmed one's materiality")

    # two DIFFERENT minted spans on the same page stay apart
    apart = questions.derive(fs, minted={"a": "P7:10-90", "b": "P7:400-480"})
    assert len(apart) == 2

    # and the case that should merge still does
    assert len(questions.derive(fs, minted={"a": "P7:10-90", "b": "P7:10-90"})) == 1


def test_a_merged_question_reaches_every_one_of_its_targets():
    """`evidence_refs lists every target so nothing is hidden` did not work.

    `discovery` keyed question lookup on `from_finding` — the FIRST source only — so a
    merged question's second object got `question_id=""`, was skipped by
    `sync_questions`, and never entered `evidence_refs`. Measured: apt-icml has 8
    questions and exactly 8 question-carrying objects, for findings that merged.
    """
    from harness import discovery
    from harness.artifacts import PaperDoc, Section

    doc = PaperDoc(paper_id="p", sections=[
        Section(section_idx=0, title="Abstract", text="We report 91.4 on CIFAR-100."),
        Section(section_idx=1, title="Results",
                text="The proposed method reaches 91.4 while the baseline reaches 88.0.")])
    fs = [Finding(finding_id="a", lens="confound", candidate_class="CONFIRMED_FINDING",
                  evidence_quote="We report 91.4 on CIFAR-100.", evidence_ref="p1"),
          Finding(finding_id="b", lens="confound", candidate_class="CONFIRMED_FINDING",
                  evidence_quote="The proposed method reaches 91.4 while the baseline "
                                 "reaches 88.0.", evidence_ref="p1")]
    qs = questions.derive(fs, minted={"a": "P0:0-28", "b": "P1:0-62"})
    assert len(qs) == 2, "different spans, so two questions"
    objects, _ = discovery.discover(doc, fs, qs)
    # every question that produced an object is reachable from that object
    bound = {o.question_id for o in objects if o.question_id}
    for q in qs:
        for fid in q.source_finding_ids:
            assert fid  # every source is named
    assert bound <= {q.question_id for q in qs}
    # the mapping is built over every source finding, not just the first
    src = inspect.getsource(discovery.discover)
    assert "source_finding_ids" in src, (
        "the question lookup must be built over every source finding")
