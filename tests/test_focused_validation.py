"""The FOCUSED_VALIDATION_EXPERIMENT route — the controlled comparison this system designs
and runs when a printed number cannot settle a question by itself.

Three modules, three different jobs, and these tests pin the seam between them:

    `harness.validation`          the smallest legitimate experiment, derivable from the
                                   paper and the pinned artifact — never a scientific
                                   choice this harness invented (`NEVER_ASSUMED`)
    `harness.between_arms`        the arithmetic: one arm held against another, against a
                                   rule declared BEFORE either of them ran
    `harness.validation_driver`   a model may PROPOSE a configuration; every proposed
                                   value must RELOCATE against the parsed paper or the
                                   pinned checkout, and the fields that would make this
                                   channel a verdict are the harness's alone to write

**The authority ladder, and the rung that is unreachable by construction.**

    ARM_MEASUREMENT              level 1: about one run of one arm, nothing else
    CONTROLLED_OBSERVATION       level 2: two arms compared; conformance NOT established
    CONFORMANT_CONTROLLED_RESULT level 3: + conformance, bound identity, answers the
                                  question this design was built for
    (no value)                   level 4, "the credited mechanism causes the effect" —
                                  `CAUSAL_ATTRIBUTION_AUTHORITIES` is the empty tuple

Every test below is PURE: no network, no subprocess, no docker, no model. Fixtures are
built directly, following the shapes each module's own `if __name__ == "__main__"`
self-check already uses.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harness import between_arms, disposition as D, materiality, taxonomy, validation, validation_driver
from harness.artifacts import (ArmComparison, ArmMeasurement, BETWEEN_ARM_STATES,
                               CAUSAL_ATTRIBUTION_AUTHORITIES, DiscoveredObject,
                               FocusedValidation, PaperDoc, PlanDecision, Section,
                               SettlementCondition, TargetOutcome, VALIDATION_AUTHORITY,
                               VALIDATION_CONFORMANCE_STATES, ValidationArm, ValidationDesign)
from harness.config import Config
from harness.stages import validation as validation_stage

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# --------------------------------------------------------------------------------------
# fixtures — the same shapes `between_arms._self_check` and `validation._self_check` use
# --------------------------------------------------------------------------------------
def _arm(role: str, value: float | None, **kw) -> ArmMeasurement:
    base = dict(role=role, label=role, metric="accuracy", metric_basis="absolute",
                unit="%", benchmark="cifar-100", split="test",
                statistical_unit="example", value=value, n=3, uncertainty=0.1,
                configuration={"regulariser": "on" if role == "treatment" else "off",
                               "epochs": "200"})
    base.update(kw)
    return ArmMeasurement(**base)


def _design(rule: str = "DIRECTION_AGREES", tolerance: float | None = None,
            tolerance_basis: str = "", predicted: str = "INCREASE",
            changed=("regulariser",), conformance: str = "CONFORMANT",
            state: str = "DESIGNED", declared: bool = True) -> ValidationDesign:
    return ValidationDesign(
        design_id="fv:t1", state=state, metric="accuracy", metric_unit="%",
        benchmark="cifar-100", split="test", statistical_unit="example",
        changed_variables=list(changed), controlled_variables=["epochs"],
        conformance=conformance,
        arms=[ValidationArm(role="control"), ValidationArm(role="treatment")],
        settlement=SettlementCondition(rule=rule, tolerance=tolerance,
                                       tolerance_basis=tolerance_basis,
                                       predicted_direction=predicted,
                                       declared_before_execution=declared))


_PROSE = ("We evaluate every method on the CIFAR-100 test set and report top-1 "
          "accuracy. Our method LDReg improves over the AugOnly baseline. The gain "
          "could come from the regulariser or from the longer schedule.")


def _doc() -> PaperDoc:
    return PaperDoc(paper_id="p", title="t",
                    sections=[Section(section_idx=0, title="Setup", text=_PROSE, page=1)])


def _node(method: str, addr: str = ""):
    """A `claimgraph.ResultNode` stand-in — only the attributes `validation` reads."""
    return SimpleNamespace(method=method, address=addr, label=method, value=None)


def _settlement(rule="DIRECTION_AGREES", tol=None, basis="", pred="INCREASE"):
    return SettlementCondition(rule=rule, tolerance=tol, tolerance_basis=basis,
                               predicted_direction=pred, declared_before_execution=True,
                               statement="the regulariser arm should score higher.")


def _build_design(doc: PaperDoc, **kw) -> ValidationDesign:
    members = kw.pop("members", [_node("AugOnly"), _node("LDReg")])
    base = dict(target_id="t1", question_id="q1", question_kind="ATTRIBUTION",
                question_ref="", question_text="is the gain the regulariser?",
                competing_explanations=["the regulariser", "the longer schedule"],
                comparison_id="comparison:accuracy|cifar-100", metric="accuracy",
                benchmark="CIFAR-100", members=members, treatment_method="LDReg",
                changed_variables=["regulariser"], controlled_variables=["schedule"],
                settlement=_settlement(), statistical_unit="example")
    base.update(kw)
    return validation.design(doc, **base)


# --------------------------------------------------------------------------------------
# 1. decomposition — a two-change confound isolates one declared variable
# --------------------------------------------------------------------------------------
def test_two_change_confound_decomposes_into_arms_differing_in_exactly_one_declared_variable():
    """A lens may raise a confound where the paper moved two things at once — the
    regulariser and the schedule. The DESIGN this route builds isolates one of them and
    holds the other fixed, so the two arms it actually measures differ in exactly the
    declared variable and nothing else, and the comparison is not refused for it."""
    control = _arm("control", 80.0, configuration={"regulariser": "off", "schedule": "cosine"})
    treatment = _arm("treatment", 82.0, configuration={"regulariser": "on", "schedule": "cosine"})
    assert between_arms.differing_keys(control, treatment) == ["regulariser"]

    design = ValidationDesign(
        design_id="fv:t1", state="DESIGNED", metric="accuracy", metric_unit="%",
        benchmark="cifar-100", split="test", statistical_unit="example",
        changed_variables=["regulariser"], controlled_variables=["schedule"],
        conformance="CONFORMANT",
        arms=[ValidationArm(role="control"), ValidationArm(role="treatment")],
        settlement=_settlement())
    got = between_arms.compare(design, [control, treatment], provenance="repo_exec",
                               answers_question=True)
    assert got.state != "REFUSED", got.reason
    assert got.refusal == ""


# --------------------------------------------------------------------------------------
# 2. missing control — the design names the control arm the paper never ran
# --------------------------------------------------------------------------------------
def test_missing_control_question_names_the_control_arm_and_the_changed_variable():
    """A CONTROL_PRESENCE question's control arm is usually one the paper never ran, and
    the design must be able to say so — an empty address is normal, not a defect — while
    still naming which variable the experiment would change."""
    doc = _doc()
    members = [_node("NoDropout", ""), _node("WithDropout", "T1:r1:c1")]
    out = validation.design(
        doc, target_id="t2", question_id="q2", question_kind="CONTROL_PRESENCE",
        question_ref="", question_text="was the no-dropout control ever run?",
        competing_explanations=["dropout helps", "dropout does nothing"],
        comparison_id="", metric="accuracy", benchmark="CIFAR-100",
        members=members, treatment_method="WithDropout",
        changed_variables=["dropout"], controlled_variables=["epochs"], settlement=None)
    assert out.control is not None and out.control.role == "control"
    assert out.control.label == "NoDropout" and out.control.address == ""
    assert out.changed_variables == ["dropout"]


# --------------------------------------------------------------------------------------
# 3. an exact one-variable intervention is accepted; a two-variable one is refused
# --------------------------------------------------------------------------------------
def test_exact_one_variable_intervention_is_accepted():
    control = _arm("control", 80.0, configuration={"regulariser": "off", "epochs": "200"})
    treatment = _arm("treatment", 82.0, configuration={"regulariser": "on", "epochs": "200"})
    got = between_arms.compare(_design(), [control, treatment], provenance="repo_exec",
                               answers_question=True)
    assert got.state == "SETTLED_AS_PREDICTED"
    assert got.refusal == ""


def test_two_variable_intervention_is_refused_not_a_controlled_comparison():
    control = _arm("control", 80.0, configuration={"regulariser": "off", "epochs": "200"})
    treatment = _arm("treatment", 82.0, configuration={"regulariser": "on", "epochs": "300"})
    got = between_arms.compare(_design(), [control, treatment], provenance="repo_exec",
                               answers_question=True)
    assert got.state == "REFUSED"
    assert got.refusal == "not_a_controlled_comparison"


def test_a_configuration_key_present_on_only_one_arm_is_a_difference():
    """Comparing only the keys both arms happen to carry would call two arms controlled
    precisely because the thing that differs was recorded on one of them."""
    control = _arm("control", 80.0, configuration={"regulariser": "off", "epochs": "200",
                                                    "warmup_epochs": "5"})
    treatment = _arm("treatment", 82.0)
    got = between_arms.compare(_design(), [control, treatment], provenance="repo_exec")
    assert got.refusal == "not_a_controlled_comparison"


# --------------------------------------------------------------------------------------
# 5. between-arm arithmetic
# --------------------------------------------------------------------------------------
def test_between_arm_arithmetic_difference_relative_difference_and_direction():
    control = _arm("control", 80.0)
    treatment = _arm("treatment", 82.0)
    got = between_arms.compare(_design(), [control, treatment], provenance="repo_exec",
                               answers_question=True)
    assert got.difference == 2.0
    assert got.relative_difference is not None and abs(got.relative_difference - 0.025) < 1e-12
    assert got.direction == "INCREASE"


def test_relative_difference_is_none_not_infinite_when_control_measures_zero():
    control = _arm("control", 0.0)
    treatment = _arm("treatment", 4.0)
    got = between_arms.compare(_design(), [control, treatment], provenance="repo_exec",
                               answers_question=True)
    assert got.difference == 4.0
    assert got.relative_difference is None
    assert got.state == "SETTLED_AS_PREDICTED", "the arithmetic still settles; only the ratio is undefined"


# --------------------------------------------------------------------------------------
# 6. a metric identity mismatch (quantity, basis, or unit) blocks the comparison
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("field,value", [("metric", "f1"), ("metric_basis", "relative"),
                                         ("unit", "points")])
def test_metric_identity_mismatch_blocks_on_quantity_basis_or_unit(field, value):
    control = _arm("control", 80.0, **{field: value})
    treatment = _arm("treatment", 82.0)
    got = between_arms.compare(_design(), [control, treatment], provenance="repo_exec")
    assert got.state == "REFUSED"
    assert got.refusal == "metric_identity_mismatch"


# --------------------------------------------------------------------------------------
# 7. a dataset/split mismatch blocks with dataset_identity_mismatch
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("field,value", [("benchmark", "imagenet"), ("split", "val")])
def test_dataset_identity_mismatch_blocks_on_benchmark_or_split(field, value):
    control = _arm("control", 80.0, **{field: value})
    treatment = _arm("treatment", 82.0)
    got = between_arms.compare(_design(), [control, treatment], provenance="repo_exec")
    assert got.state == "REFUSED"
    assert got.refusal == "dataset_identity_mismatch"


def test_statistical_unit_mismatch_is_its_own_refusal():
    control = _arm("control", 80.0, statistical_unit="seed")
    treatment = _arm("treatment", 82.0)
    got = between_arms.compare(_design(), [control, treatment], provenance="repo_exec")
    assert got.refusal == "statistical_unit_mismatch"


# --------------------------------------------------------------------------------------
# 8. source-authority failure: an unrelocatable quotation is dropped, a harness-sourced
#    arm makes conformance DEVIATE
# --------------------------------------------------------------------------------------
def test_relocate_configuration_drops_a_value_whose_quotation_does_not_relocate():
    doc = PaperDoc(paper_id="p", title="T", sections=[Section(
        section_idx=0, title="Setup", text="All models train for 200 epochs.")])
    cfg, basis, dropped = validation_driver.relocate_configuration(doc, {
        "epochs": {"value": "200", "quote": "All models train for 200 epochs."},
        "learning_rate": {"value": "0.1", "quote": "We use a learning rate of 0.1."}})
    assert cfg == {"epochs": "200"}
    assert basis["epochs"].startswith("P0:")
    assert any("learning_rate" in d for d in dropped)


def test_arm_sourced_from_harness_makes_conformance_deviate():
    paper_arm = ValidationArm(role="control", source="paper")
    harness_arm = ValidationArm(role="treatment", source="harness",
                                configuration={"learning_rate": "0.1"})
    state, basis = validation.conformance(ValidationDesign(arms=[paper_arm, harness_arm]))
    assert state == "DEVIATES"
    assert basis


# --------------------------------------------------------------------------------------
# 9. execution failure does not become scientific failure
# --------------------------------------------------------------------------------------
def test_arm_that_produced_no_value_is_inconclusive_never_a_defect():
    control = _arm("control", None, reason="the run emitted no metric.")
    treatment = _arm("treatment", 82.0)
    comparison = between_arms.compare(_design(), [control, treatment], provenance="repo_exec",
                                      answers_question=True)
    assert comparison.refusal == "arm_produced_no_value"
    assert not comparison.establishes_defect

    fv = FocusedValidation(paper_id="p", target_id="t1", design=_design(),
                           comparison=comparison)
    got, reason = validation.outcome_disposition(fv)
    assert got == "INCONCLUSIVE"
    assert got != "VALIDATION_DEFECT_ESTABLISHED"
    assert reason


def test_arm_missing_refusal_when_only_one_arm_present():
    got = between_arms.compare(_design(), [_arm("control", 80.0)], provenance="repo_exec")
    assert got.refusal == "arm_missing"


# --------------------------------------------------------------------------------------
# 10. a legitimate controlled result settles the review question
# --------------------------------------------------------------------------------------
def test_conformant_repo_exec_result_against_prediction_establishes_a_defect():
    design = _design(predicted="INCREASE")
    control = _arm("control", 80.0)
    treatment = _arm("treatment", 78.0)  # moves the wrong way
    comparison = between_arms.compare(design, [control, treatment], provenance="repo_exec",
                                      answers_question=True)
    assert comparison.state == "SETTLED_AGAINST_PREDICTION"
    assert comparison.authority == "CONFORMANT_CONTROLLED_RESULT"
    assert comparison.establishes_defect is True

    fv = FocusedValidation(paper_id="p", target_id="t1", design=design, comparison=comparison)
    fv.disposition, fv.reason = validation.outcome_disposition(fv)
    assert fv.disposition == "VALIDATION_DEFECT_ESTABLISHED"

    outcome = TargetOutcome(target_id="t1", disposition=fv.disposition, provenance="repo_exec")
    assert outcome.establishes_failure is True


# --------------------------------------------------------------------------------------
# 11 & 12. materiality: established is not material; only a paper-owned basis stops
# --------------------------------------------------------------------------------------
def test_non_material_settled_defect_cannot_stop_the_paper():
    obj = DiscoveredObject(target_id="t1", materiality_basis="NONE")
    outcome = TargetOutcome(target_id="t1", disposition="VALIDATION_DEFECT_ESTABLISHED",
                            provenance="repo_exec")
    assert outcome.establishes_failure is True
    assert materiality.material_target_failure([obj], [outcome]) is None


def test_material_admissible_defect_can_stop_the_paper():
    obj = DiscoveredObject(target_id="t1", materiality_basis="ABSTRACT_CLAIM")
    outcome = TargetOutcome(target_id="t1", disposition="VALIDATION_DEFECT_ESTABLISHED",
                            provenance="repo_exec")
    assert materiality.material_target_failure([obj], [outcome]) is outcome


# --------------------------------------------------------------------------------------
# 13. focused validation cannot self-certify its own conformance
# --------------------------------------------------------------------------------------
def test_parse_design_strips_every_harness_owned_key():
    proposal_json = {k: "x" for k in validation_driver.HARNESS_OWNED_DESIGN_KEYS}
    proposal_json.update({"buildable": True, "changed_variable": "regulariser"})
    proposal, meta = validation_driver.parse_design(json.dumps(proposal_json))
    for key in validation_driver.HARNESS_OWNED_DESIGN_KEYS:
        assert key not in proposal, key
    assert meta["harness_keys_stripped"] == len(validation_driver.HARNESS_OWNED_DESIGN_KEYS)


def test_designer_asserted_conformance_state_and_severity_do_not_survive():
    raw = json.dumps({
        "buildable": True, "changed_variable": "regulariser",
        "state": "DESIGNED", "conformance": "CONFORMANT", "severity": "FATAL",
        "paper_decision": "RED", "established": True, "answers_question": True})
    proposal, meta = validation_driver.parse_design(raw)
    assert "state" not in proposal and "conformance" not in proposal
    assert "severity" not in proposal and "paper_decision" not in proposal
    assert "established" not in proposal and "answers_question" not in proposal
    assert meta["harness_keys_stripped"] == 6


# --------------------------------------------------------------------------------------
# 14. the empty-tuple rule — level 4 is unreachable, over every reachable input
# --------------------------------------------------------------------------------------
def test_causal_attribution_authorities_is_the_empty_tuple():
    assert CAUSAL_ATTRIBUTION_AUTHORITIES == ()


def test_establishes_attribution_is_false_over_every_reachable_combination():
    provenances = ("", "template", "synthesized", "driver", "repo_exec", "reimpl_exec", "garbage")
    for state in BETWEEN_ARM_STATES:
        for conformance in VALIDATION_CONFORMANCE_STATES:
            for provenance in provenances:
                authority = between_arms.authority_for(state=state, conformance=conformance,
                                                       provenance=provenance)
                assert authority in VALIDATION_AUTHORITY, authority
                comparison = ArmComparison(state=state, authority=authority,
                                          conformance=conformance, provenance=provenance)
                assert comparison.establishes_attribution is False

    # And directly: no member of the whole authority vocabulary can carry it either.
    for authority in VALIDATION_AUTHORITY:
        assert ArmComparison(authority=authority).establishes_attribution is False


# --------------------------------------------------------------------------------------
# 15. the provenance ceiling
# --------------------------------------------------------------------------------------
def test_provenance_ceiling_blocks_synthesized_and_template_from_the_top_authority():
    design = _design(predicted="INCREASE")
    control = _arm("control", 80.0)
    treatment = _arm("treatment", 82.0)
    for provenance in ("synthesized", "template"):
        comparison = between_arms.compare(design, [control, treatment], provenance=provenance,
                                          answers_question=True)
        assert comparison.state == "SETTLED_AS_PREDICTED", provenance
        assert comparison.authority == "CONTROLLED_OBSERVATION", provenance
        assert not comparison.establishes_defect and not comparison.supports_claim


# --------------------------------------------------------------------------------------
# 16. a settlement rule not declared before the run refuses
# --------------------------------------------------------------------------------------
def test_settlement_rule_not_declared_before_the_run_is_refused():
    design = _design(declared=False)
    got = between_arms.compare(design, [_arm("control", 80.0), _arm("treatment", 82.0)],
                               provenance="repo_exec")
    assert got.state == "REFUSED"
    assert got.refusal == "settlement_condition_undeclared"


def test_tolerance_undeclared_refusal_for_a_rule_that_needs_one():
    design = _design(rule="EFFECT_EXCEEDS_TOLERANCE", tolerance=None)
    got = between_arms.compare(design, [_arm("control", 80.0), _arm("treatment", 82.0)],
                               provenance="repo_exec")
    assert got.refusal == "tolerance_undeclared"


def test_uncertainty_unavailable_refusal_for_the_arms_indistinguishable_rule():
    design = _design(rule="ARMS_INDISTINGUISHABLE", predicted="NO_CHANGE")
    control = _arm("control", 80.0, uncertainty=None)
    treatment = _arm("treatment", 82.0)
    got = between_arms.compare(design, [control, treatment], provenance="repo_exec")
    assert got.refusal == "uncertainty_unavailable"


# --------------------------------------------------------------------------------------
# 17. every new TARGET_DISPOSITIONS member has an evidence row and exactly one
#     classification (blocker XOR deliberately-unclassified)
# --------------------------------------------------------------------------------------
_NEW_VALIDATION_DISPOSITIONS = ("VALIDATION_DEFECT_ESTABLISHED", "VALIDATION_SUPPORTS_CLAIM",
                                "VALIDATION_OBSERVATION_ONLY", "VALIDATION_INCONCLUSIVE")


def test_every_new_validation_disposition_has_an_evidence_row_and_one_classification():
    for d in _NEW_VALIDATION_DISPOSITIONS:
        assert d in taxonomy._EVIDENCE_FOR_DISPOSITION, d
        in_blocker = d in D.BLOCKER_FOR_DISPOSITION
        in_unclassified = d in D.DELIBERATELY_UNCLASSIFIED
        assert in_blocker != in_unclassified, (
            f"{d} must be classified exactly once: blocker={in_blocker} "
            f"unclassified={in_unclassified}")
        # None of the four focused-validation outcomes is a blocker: three of them settled
        # something and the fourth (VALIDATION_INCONCLUSIVE) is a route that ran and
        # settled nothing, kept apart from COMPARISON_BLOCKED for that reason.
        assert not in_blocker, d


# --------------------------------------------------------------------------------------
# 18. gate-closed determinism
# --------------------------------------------------------------------------------------
def test_gate_closed_route_still_runs_and_every_design_is_specification_blocked():
    cfg = Config(allow_validation_design=False)
    doc = _doc()
    obj = DiscoveredObject(target_id="t1", question_id="q1", question_kind="ATTRIBUTION",
                           metric="accuracy", experiment="CIFAR-100",
                           claim_text="LDReg improves over AugOnly.")
    plan = PlanDecision(target_id="t1", action="FOCUSED_VALIDATION_EXPERIMENT",
                        route="FOCUSED_VALIDATION_EXPERIMENT", requires_execution=True,
                        competing_explanations=["the regulariser", "the schedule"])
    design, record = validation_stage.build_design(cfg, doc, obj, plan, None)
    assert design.state == "SPECIFICATION_BLOCKED"
    assert design.missing
    assert not design.established


def test_available_reports_which_gate_is_closed():
    ok, why = validation_driver.available(Config(allow_validation_design=False))
    assert ok is False
    assert "SH_ALLOW_VALIDATION_DESIGN" in why


# --------------------------------------------------------------------------------------
# extra coverage — the ingredient checks, and the surrounding contract each module states
# --------------------------------------------------------------------------------------
def test_missing_settlement_condition_blocks_specification_and_names_it():
    got = _build_design(_doc(), settlement=None)
    assert got.state == "SPECIFICATION_BLOCKED"
    assert "settlement_condition" in got.missing
    assert not got.established


def test_missing_competing_explanations_blocks_specification_and_names_it():
    got = _build_design(_doc(), competing_explanations=["only one reading"])
    assert got.state == "SPECIFICATION_BLOCKED"
    assert "competing_explanations" in got.missing


def test_two_changed_variables_blocks_specification_and_names_changed_variable():
    got = _build_design(_doc(), changed_variables=["regulariser", "schedule"])
    assert got.state == "SPECIFICATION_BLOCKED"
    assert "changed_variable" in got.missing


def test_arms_from_comparison_refuses_a_control_chosen_by_position():
    """A control picked by list order is a scientific choice made by an enumeration, and
    this route refuses to make it: nothing is returned unless the caller NAMED the
    treatment."""
    doc = _doc()
    members = [_node("AugOnly"), _node("LDReg")]
    assert validation.arms_from_comparison(doc, members, treatment_method="") == []


def test_discharge_requires_authority_answers_question_and_a_settled_state():
    design = _design()
    settled = ArmComparison(state="SETTLED_AS_PREDICTED", authority="CONFORMANT_CONTROLLED_RESULT",
                            answers_question=True)
    unanswering = ArmComparison(state="SETTLED_AS_PREDICTED",
                                authority="CONFORMANT_CONTROLLED_RESULT", answers_question=False)
    observation = ArmComparison(state="SETTLED_AS_PREDICTED", authority="CONTROLLED_OBSERVATION",
                                answers_question=True)
    assert validation.discharge(FocusedValidation(paper_id="p", target_id="t1", design=design,
                                                  comparison=settled))
    assert not validation.discharge(FocusedValidation(paper_id="p", target_id="t1", design=design,
                                                       comparison=unanswering))
    assert not validation.discharge(FocusedValidation(paper_id="p", target_id="t1", design=design,
                                                       comparison=observation))
    assert not validation.discharge(None)


def test_outcome_disposition_is_specification_blocked_when_design_never_established():
    blocked_design = _design(state="SPECIFICATION_BLOCKED")
    blocked_design.reason = "the paper does not state a control."
    fv = FocusedValidation(paper_id="p", target_id="t1", design=blocked_design)
    got, reason = validation.outcome_disposition(fv)
    assert got == "SPECIFICATION_BLOCKED"
    assert reason == "the paper does not state a control."


def test_outcome_disposition_is_not_attempted_when_no_route_ran_at_all():
    assert validation.outcome_disposition(None)[0] == "NOT_ATTEMPTED"


def test_measurement_authority_is_about_one_arm_and_nothing_else():
    assert validation.measurement_authority(None) == "NONE"
    assert validation.measurement_authority(ArmMeasurement(role="control")) == "NONE"
    got = validation.measurement_authority(ArmMeasurement(role="control", value=1.0))
    assert got == "ARM_MEASUREMENT"


def test_summarise_counts_are_never_a_sum_of_two_different_things():
    design = _design()
    settled = ArmComparison(state="SETTLED_AS_PREDICTED", authority="CONFORMANT_CONTROLLED_RESULT",
                            answers_question=True)
    fv = FocusedValidation(paper_id="p", target_id="t1", design=design, comparison=settled,
                           launched=3)
    got = validation.summarise(fv)
    assert got["designed"] == 1 and got["compared"] == 1 and got["settled"] == 1
    assert got["launched"] == 3
    assert validation.summarise(None) == {"designed": 0, "specification_blocked": 0,
                                          "arms_measured": 0, "compared": 0, "settled": 0,
                                          "launched": 0}


def test_authority_for_the_three_rungs_and_the_floor():
    assert between_arms.authority_for(state="REFUSED", conformance="CONFORMANT",
                                      provenance="repo_exec") == "NONE"
    assert between_arms.authority_for(state="SETTLED_AS_PREDICTED", conformance="CONFORMANT",
                                      provenance="repo_exec") == "CONFORMANT_CONTROLLED_RESULT"
    assert between_arms.authority_for(state="SETTLED_AS_PREDICTED", conformance="DEVIATES",
                                      provenance="repo_exec") == "CONTROLLED_OBSERVATION"
    assert between_arms.authority_for(state="SETTLED_AS_PREDICTED", conformance="CONFORMANT",
                                      provenance="synthesized") == "CONTROLLED_OBSERVATION"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
