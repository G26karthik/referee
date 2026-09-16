"""One arm held against another, against a rule declared before either of them ran.

`python -m harness.between_arms` runs the self-check.

**The comparison this system could not perform.** Every reconciliation in this harness
holds a measured quantity against one the paper printed. That is the right arithmetic for
"is 61.4 the number their code produces" and it is not a comparison at all for the
questions a focused validation exists to answer: "is the gain attributable to the
augmentation or to the loss weight" holds one ARM against another ARM, and the paper
printed neither side. `comparison.RECONCILABLE` was one entry long for exactly that
reason, and `comparison.derive` returned `unsupported` for BETWEEN_ARMS with a sentence
saying a run would produce two numbers and no verdict. This module is that missing
arithmetic.

**Three things it deliberately does not do.**

*It does not choose a threshold.* There is no percentage anywhere in this file. The
settlement rule and, where the rule needs one, the tolerance, are carried on the
`ValidationDesign` and declared before anything ran; a comparison that arrives without one
is REFUSED rather than settled against a number chosen once the results were in. That is
the whole difference between a predeclared condition and a post-hoc one, and it is the
only way a controlled result can honestly be said to have settled anything.

*It does not decide whether the arms were comparable.* It CHECKS. Metric, basis, unit,
benchmark, split and statistical unit must agree between the two arms, and exactly the
declared variables — no more — may differ in their configurations. Each failure is its own
refusal, because "the arms measured different things" and "the arms differ in two ways"
are different defects in the design and a reader tracing a refusal needs them apart.

*It does not decide what its own result is entitled to say.* `state` is arithmetic.
`authority` is a separate field derived from conformance and provenance, and a comparison
can be perfectly computed and still carry `CONTROLLED_OBSERVATION` — two arms really ran,
they really were compared, and what they were compared was this harness's experiment
rather than the authors'. Level 3 needs the design to be conformant AND the provenance to
clear the ceiling `harness.provenance` already enforces for reproductions, and level 4 —
"the credited mechanism is what produces the effect" — has no spelling at all.
"""
from __future__ import annotations

from .artifacts import (ArmComparison, ArmMeasurement, RULES_REQUIRING_TOLERANCE,
                        RULES_REQUIRING_UNCERTAINTY, SETTLEMENT_RULES, ValidationDesign)
from .provenance import admits

# A rule needs at least this many repetitions on each arm before an interval means
# anything. One measurement has no spread, and a spread computed from one value is zero —
# which would read as a separation nobody established.
_MIN_REPETITIONS_FOR_INTERVAL = 2

# The identity fields that must AGREE between two arms before their numbers may be
# subtracted, grouped by which refusal an disagreement produces. Exact string comparison
# after stripping: a unit recorded as "%" on one arm and "percent" on the other is a
# disagreement this module must report rather than repair, because repairing it is exactly
# how a units error becomes a verdict.
_IDENTITY_GROUPS = (
    (("metric", "metric_basis", "unit"), "metric_identity_mismatch"),
    (("benchmark", "split"), "dataset_identity_mismatch"),
    (("statistical_unit",), "statistical_unit_mismatch"),
)


def _refused(design_id: str, refusal: str, reason: str, **kw) -> ArmComparison:
    return ArmComparison(design_id=design_id, state="REFUSED", refusal=refusal,
                         authority="NONE", reason=reason, **kw)


def _agree(a: ArmMeasurement, b: ArmMeasurement, fields: tuple[str, ...]) -> str:
    """The first field of `fields` on which the two arms disagree, or ''."""
    for f in fields:
        if (getattr(a, f, "") or "").strip() != (getattr(b, f, "") or "").strip():
            return f
    return ""


def differing_keys(a: ArmMeasurement, b: ArmMeasurement) -> list[str]:
    """Every configuration key whose value differs between the arms, including one the
    only one arm sets.

    A key PRESENT on one arm and absent on the other is a difference, not a match. The
    alternative — comparing only the keys both happen to carry — would call two arms
    controlled because the thing that differs between them was recorded on one side.
    """
    keys = set(a.configuration) | set(b.configuration)
    return sorted(k for k in keys
                  if (a.configuration.get(k, "") or "").strip()
                  != (b.configuration.get(k, "") or "").strip())


def _direction(difference: float, tolerance: float | None) -> str:
    """Which way the metric moved, with a declared tolerance as the dead band.

    With no tolerance the test is the exact sign, because inventing a dead band here is
    inventing the threshold this module refuses to choose.
    """
    if tolerance is not None and abs(difference) <= tolerance:
        return "NO_CHANGE"
    if difference > 0:
        return "INCREASE"
    if difference < 0:
        return "DECREASE"
    return "NO_CHANGE"


def _overlap(a: ArmMeasurement, b: ArmMeasurement) -> bool | None:
    """Do the arms' one-sigma intervals overlap? None when either arm has no spread.

    None rather than False, deliberately: False reads as a separation this review
    established, and an arm that ran once established no spread at all.
    """
    if a.uncertainty is None or b.uncertainty is None:
        return None
    if a.n < _MIN_REPETITIONS_FOR_INTERVAL or b.n < _MIN_REPETITIONS_FOR_INTERVAL:
        return None
    lo_a, hi_a = a.value - abs(a.uncertainty), a.value + abs(a.uncertainty)
    lo_b, hi_b = b.value - abs(b.uncertainty), b.value + abs(b.uncertainty)
    return not (hi_a < lo_b or hi_b < lo_a)


def _settle(rule: str, predicted: str, observed: str, difference: float,
            tolerance: float | None, overlap: bool | None) -> tuple[str, str]:
    """(state, one sentence). The arithmetic of the four declared rules, and nothing else.

    Every branch reads a PREDECLARED rule and a PREDECLARED prediction against a measured
    difference. No branch consults the paper's printed value, and no branch chooses a
    number: `tolerance` arrives from the design or the rule was refused before this
    function was reached.
    """
    if rule == "DIRECTION_AGREES":
        if observed == predicted:
            return ("SETTLED_AS_PREDICTED",
                    f"the metric moved {observed.lower()}, which is the direction the "
                    f"claim predicts.")
        return ("SETTLED_AGAINST_PREDICTION",
                f"the claim predicts {predicted.lower().replace('_', ' ')} and the "
                f"controlled comparison moved {observed.lower().replace('_', ' ')}.")

    if rule == "EFFECT_EXCEEDS_TOLERANCE":
        big_enough = abs(difference) > tolerance
        if not big_enough:
            return ("SETTLED_AGAINST_PREDICTION",
                    f"the claim predicts an effect larger than the declared tolerance of "
                    f"{tolerance:g} and the controlled comparison produced {difference:g}.")
        if predicted in ("INCREASE", "DECREASE") and observed != predicted:
            return ("SETTLED_AGAINST_PREDICTION",
                    f"the effect exceeds the declared tolerance of {tolerance:g} and runs "
                    f"{observed.lower()}, which is the opposite of what the claim predicts.")
        return ("SETTLED_AS_PREDICTED",
                f"the controlled comparison produced {difference:g}, which exceeds the "
                f"declared tolerance of {tolerance:g} in the predicted direction.")

    if rule == "EFFECT_WITHIN_EQUIVALENCE_MARGIN":
        if abs(difference) <= tolerance:
            return ("SETTLED_AS_PREDICTED",
                    f"the arms differ by {difference:g}, inside the declared equivalence "
                    f"margin of {tolerance:g}.")
        return ("SETTLED_AGAINST_PREDICTION",
                f"the arms differ by {difference:g}, outside the declared equivalence "
                f"margin of {tolerance:g}.")

    # ARMS_INDISTINGUISHABLE. `overlap` is not None here — the rule is refused above when
    # either arm reported no spread, so this branch never reads an interval nobody measured.
    if overlap:
        state = ("SETTLED_AS_PREDICTED" if predicted == "NO_CHANGE"
                 else "SETTLED_AGAINST_PREDICTION")
        return (state, "the arms' one-sigma intervals overlap, so this comparison did not "
                       "separate them.")
    if predicted == "NO_CHANGE":
        return ("SETTLED_AGAINST_PREDICTION",
                "the arms' one-sigma intervals do not overlap, and the claim predicts no "
                "difference between them.")
    if predicted in ("INCREASE", "DECREASE") and observed != predicted:
        return ("SETTLED_AGAINST_PREDICTION",
                f"the arms separate and the metric moved {observed.lower()}, which is the "
                f"opposite of what the claim predicts.")
    return ("SETTLED_AS_PREDICTED",
            "the arms' one-sigma intervals do not overlap and the separation runs in the "
            "predicted direction.")


def authority_for(*, state: str, conformance: str, provenance: str) -> str:
    """What a comparison in this state is ENTITLED to say. Vocabulary strings only.

    Three rungs and a floor, and the ordering is the whole point: an arithmetic that ran
    cleanly still carries only `CONTROLLED_OBSERVATION` unless the experiment was the
    paper's experiment. `CONFORMANT_CONTROLLED_RESULT` needs BOTH halves — a design whose
    every arm derives from the paper or the pinned artifact, and a provenance the
    reproduction ceiling already admits — because either one alone leaves a scientific
    choice that this review made and the authors did not.
    """
    if state == "REFUSED":
        return "NONE"
    if conformance == "CONFORMANT" and admits(provenance):
        return "CONFORMANT_CONTROLLED_RESULT"
    return "CONTROLLED_OBSERVATION"


def compare(design: ValidationDesign | None,
            measurements: list[ArmMeasurement] | None = None,
            *, provenance: str = "", answers_question: bool = False) -> ArmComparison:
    """Hold the treatment arm against the control arm, against the declared rule.

    Every refusal below is a fact about the experiment or about this harness, and not one
    of them is a finding about the paper. They are checked in the order a reader would:
    is there a design, is there a rule, are there two arms, did both produce a number,
    were they measuring the same thing, do they differ in only the declared way, and does
    the declared rule have the inputs its own arithmetic needs.
    """
    measurements = list(measurements or [])
    did = design.design_id if design is not None else ""

    if design is None or not design.established:
        return _refused(did, "design_not_established",
                        "the experiment was never established as buildable, so there is "
                        "nothing to compare: " + ((design.reason if design else "") or
                                                  "no design was produced."))

    settlement = design.settlement
    if (settlement is None or settlement.rule not in SETTLEMENT_RULES
            or not settlement.declared_before_execution
            or settlement.predicted_direction in ("", "UNSTATED")):
        return _refused(did, "settlement_condition_undeclared",
                        "no settlement condition was declared before this experiment ran. "
                        "A rule chosen after the numbers arrived settles whatever the "
                        "author of the rule wanted it to, so the comparison is reported "
                        "rather than adjudicated.")
    rule = settlement.rule
    tolerance = settlement.tolerance
    predicted = settlement.predicted_direction

    control = next((m for m in measurements if m.role == "control"), None)
    treatment = next((m for m in measurements if m.role == "treatment"), None)
    if control is None or treatment is None:
        have = sorted({m.role for m in measurements})
        return _refused(did, "arm_missing",
                        f"a between-arms comparison needs a control arm and a treatment "
                        f"arm; this run produced {have or 'neither'}. One arm is a "
                        f"measurement, not a comparison.")

    base = dict(control_label=control.label, treatment_label=treatment.label,
                metric=design.metric, unit=design.metric_unit,
                control_value=control.value, treatment_value=treatment.value,
                settlement_rule=rule, tolerance=tolerance, provenance=provenance,
                conformance=design.conformance)

    if control.value is None or treatment.value is None:
        empty = [m.role for m in (control, treatment) if m.value is None]
        why = " ".join(m.reason for m in (control, treatment) if m.value is None and m.reason)
        return _refused(did, "arm_produced_no_value",
                        f"the {' and '.join(empty)} arm ran and reported no value for "
                        f"{design.metric or 'the metric'}. {why}".strip(), **base)

    for fields, refusal in _IDENTITY_GROUPS:
        bad = _agree(control, treatment, fields)
        if bad:
            return _refused(
                did, refusal,
                f"the arms disagree on {bad}: the control arm reports "
                f"'{getattr(control, bad, '')}' and the treatment arm "
                f"'{getattr(treatment, bad, '')}'. Subtracting two numbers that are not "
                f"the same quantity, on the same basis, over the same population produces "
                f"a units error dressed as a result.", **base)

    differing = differing_keys(control, treatment)
    declared = sorted({v.strip() for v in design.changed_variables if v.strip()})
    if differing != declared:
        extra = [k for k in differing if k not in declared]
        absent = [k for k in declared if k not in differing]
        detail = []
        if extra:
            detail.append(f"the arms also differ in {', '.join(extra)}")
        if absent:
            detail.append(f"the declared change to {', '.join(absent)} is not present in "
                          f"what actually ran")
        return _refused(
            did, "not_a_controlled_comparison",
            "this comparison is designed to change exactly "
            f"{', '.join(declared) or 'nothing'} and " + "; ".join(detail) +
            ". A contrast in which two things moved discriminates between neither.", **base)

    if rule in RULES_REQUIRING_TOLERANCE and tolerance is None:
        return _refused(did, "tolerance_undeclared",
                        f"the declared rule {rule} compares the effect against a margin "
                        f"and the design carries none. Defaulting it to zero would settle "
                        f"every comparison in whichever direction the noise fell.", **base)

    overlap = _overlap(control, treatment)
    if rule in RULES_REQUIRING_UNCERTAINTY and overlap is None:
        return _refused(did, "uncertainty_unavailable",
                        f"the declared rule {rule} compares the arms' intervals and at "
                        f"least one arm ran too few times to have one. A spread computed "
                        f"from a single value is zero, which would read as a separation "
                        f"nobody measured.", **base)

    difference = treatment.value - control.value
    relative = None if control.value == 0 else difference / abs(control.value)
    observed = _direction(difference, tolerance)
    state, sentence = _settle(rule, predicted, observed, difference, tolerance, overlap)
    authority = authority_for(state=state, conformance=design.conformance,
                              provenance=provenance)

    statement = (f"under a controlled comparison that changed "
                 f"{', '.join(declared) or 'nothing'} and held "
                 f"{', '.join(design.controlled_variables) or 'the remaining choices'} "
                 f"fixed, {sentence}")
    if authority != "CONFORMANT_CONTROLLED_RESULT":
        statement += (" This is an observation about the experiment this review "
                      "constructed; it is not held against the paper, because the "
                      "experiment is not established to be the paper's.")

    return ArmComparison(
        design_id=did, difference=difference, relative_difference=relative,
        direction=observed, intervals_overlap=overlap, state=state, refusal="",
        authority=authority, answers_question=bool(answers_question),
        statement=statement, reason=sentence, **base)


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    from .artifacts import (BETWEEN_ARM_REFUSALS, BETWEEN_ARM_STATES,
                            SettlementCondition, VALIDATION_AUTHORITY, ValidationArm)

    for name, p in inspect.signature(authority_for).parameters.items():
        assert str(p.annotation) == "str", f"{name}: {p.annotation}"

    def arm(role, value, **kw):
        base = dict(role=role, label=role, metric="accuracy", metric_basis="absolute",
                    unit="%", benchmark="cifar-100", split="test",
                    statistical_unit="example", value=value, n=3, uncertainty=0.1,
                    configuration={"augmentation": "on" if role == "treatment" else "off",
                                   "lr": "0.1"})
        base.update(kw)
        return ArmMeasurement(**base)

    def plan(rule="DIRECTION_AGREES", tolerance=None, predicted="INCREASE",
             changed=("augmentation",), conformance="CONFORMANT", state="DESIGNED"):
        return ValidationDesign(
            design_id="d1", state=state, metric="accuracy", metric_unit="%",
            benchmark="cifar-100", split="test", statistical_unit="example",
            changed_variables=list(changed), controlled_variables=["lr"],
            conformance=conformance,
            arms=[ValidationArm(role="control"), ValidationArm(role="treatment")],
            settlement=SettlementCondition(rule=rule, tolerance=tolerance,
                                           predicted_direction=predicted,
                                           declared_before_execution=True))

    # --- the happy path, and the authority it earns -----------------------------------
    got = compare(plan(), [arm("control", 80.0), arm("treatment", 82.0)],
                  provenance="repo_exec", answers_question=True)
    assert got.state == "SETTLED_AS_PREDICTED", got.state
    assert got.difference == 2.0 and abs(got.relative_difference - 0.025) < 1e-12
    assert got.direction == "INCREASE" and got.authority == "CONFORMANT_CONTROLLED_RESULT"
    assert got.establishes_defect is False and got.supports_claim is True

    against = compare(plan(), [arm("control", 80.0), arm("treatment", 78.0)],
                      provenance="repo_exec", answers_question=True)
    assert against.state == "SETTLED_AGAINST_PREDICTION" and against.establishes_defect

    # --- LEVEL 4 IS UNREACHABLE. Not a threshold, a membership test in an empty tuple. --
    for c in (got, against):
        assert c.establishes_attribution is False

    # --- a non-conformant design is an observation, whatever the arithmetic said -------
    obs = compare(plan(conformance="DEVIATES"), [arm("control", 80.0), arm("treatment", 78.0)],
                  provenance="repo_exec", answers_question=True)
    assert obs.state == "SETTLED_AGAINST_PREDICTION"
    assert obs.authority == "CONTROLLED_OBSERVATION" and not obs.establishes_defect
    assert "not established to be the paper's" in obs.statement

    # ... and so is an inadmissible provenance, with conformance perfect
    synth = compare(plan(), [arm("control", 80.0), arm("treatment", 78.0)],
                    provenance="synthesized", answers_question=True)
    assert synth.authority == "CONTROLLED_OBSERVATION" and not synth.establishes_defect

    # ... and so is a conformant, admissible result that answers a DIFFERENT question
    other = compare(plan(), [arm("control", 80.0), arm("treatment", 78.0)],
                    provenance="repo_exec", answers_question=False)
    assert other.authority == "CONFORMANT_CONTROLLED_RESULT" and not other.establishes_defect

    # --- every refusal ----------------------------------------------------------------
    cases = {
        "design_not_established": compare(plan(state="SPECIFICATION_BLOCKED"),
                                          [arm("control", 80.0), arm("treatment", 82.0)]),
        "settlement_condition_undeclared": compare(
            plan(predicted="UNSTATED"), [arm("control", 80.0), arm("treatment", 82.0)]),
        "arm_missing": compare(plan(), [arm("control", 80.0)]),
        "arm_produced_no_value": compare(
            plan(), [arm("control", None, reason="the run emitted no metric."),
                     arm("treatment", 82.0)]),
        "metric_identity_mismatch": compare(
            plan(), [arm("control", 80.0, unit="points"), arm("treatment", 82.0)]),
        "dataset_identity_mismatch": compare(
            plan(), [arm("control", 80.0, split="val"), arm("treatment", 82.0)]),
        "statistical_unit_mismatch": compare(
            plan(), [arm("control", 80.0, statistical_unit="seed"), arm("treatment", 82.0)]),
        "not_a_controlled_comparison": compare(
            plan(), [arm("control", 80.0),
                     arm("treatment", 82.0, configuration={"augmentation": "on",
                                                           "lr": "0.3"})]),
        "tolerance_undeclared": compare(
            plan(rule="EFFECT_EXCEEDS_TOLERANCE"),
            [arm("control", 80.0), arm("treatment", 82.0)]),
        "uncertainty_unavailable": compare(
            plan(rule="ARMS_INDISTINGUISHABLE", predicted="NO_CHANGE"),
            [arm("control", 80.0, uncertainty=None), arm("treatment", 82.0)]),
    }
    for want, c in cases.items():
        assert c.state == "REFUSED" and c.refusal == want, (want, c.state, c.refusal)
        assert c.authority == "NONE" and c.reason, want
        assert not c.establishes_defect and not c.supports_claim, want
    assert set(cases) | {"", "relative_difference_undefined"} == set(BETWEEN_ARM_REFUSALS)

    # A CONFIGURATION KEY ON ONE SIDE ONLY IS A DIFFERENCE. Comparing only the keys both
    # arms carry would call two arms controlled precisely because the thing that differs
    # between them was recorded on one of them.
    lop = compare(plan(), [arm("control", 80.0, configuration={"augmentation": "off",
                                                               "lr": "0.1",
                                                               "warmup_epochs": "5"}),
                           arm("treatment", 82.0)])
    assert lop.refusal == "not_a_controlled_comparison", lop.refusal

    # --- a zero control makes the RELATIVE difference undefined, never enormous --------
    zero = compare(plan(), [arm("control", 0.0), arm("treatment", 2.0)],
                   provenance="repo_exec", answers_question=True)
    assert zero.difference == 2.0 and zero.relative_difference is None
    assert zero.state == "SETTLED_AS_PREDICTED"

    # --- the tolerance rules ----------------------------------------------------------
    tol = plan(rule="EFFECT_EXCEEDS_TOLERANCE", tolerance=1.0)
    big = compare(tol, [arm("control", 80.0), arm("treatment", 82.0)],
                  provenance="repo_exec", answers_question=True)
    assert big.state == "SETTLED_AS_PREDICTED"
    small = compare(tol, [arm("control", 80.0), arm("treatment", 80.5)],
                    provenance="repo_exec", answers_question=True)
    assert small.state == "SETTLED_AGAINST_PREDICTION" and small.direction == "NO_CHANGE"
    assert small.establishes_defect
    wrong = compare(tol, [arm("control", 80.0), arm("treatment", 76.0)],
                    provenance="repo_exec", answers_question=True)
    assert wrong.state == "SETTLED_AGAINST_PREDICTION" and "opposite" in wrong.reason

    equiv = plan(rule="EFFECT_WITHIN_EQUIVALENCE_MARGIN", tolerance=1.0,
                 predicted="NO_CHANGE")
    assert compare(equiv, [arm("control", 80.0), arm("treatment", 80.4)],
                   provenance="repo_exec").state == "SETTLED_AS_PREDICTED"
    assert compare(equiv, [arm("control", 80.0), arm("treatment", 84.0)],
                   provenance="repo_exec").state == "SETTLED_AGAINST_PREDICTION"

    # --- intervals --------------------------------------------------------------------
    ind = plan(rule="ARMS_INDISTINGUISHABLE", predicted="INCREASE")
    close = compare(ind, [arm("control", 80.0, uncertainty=1.0),
                          arm("treatment", 80.5, uncertainty=1.0)],
                    provenance="repo_exec", answers_question=True)
    assert close.intervals_overlap is True and close.state == "SETTLED_AGAINST_PREDICTION"
    far = compare(ind, [arm("control", 80.0, uncertainty=0.1),
                        arm("treatment", 90.0, uncertainty=0.1)],
                  provenance="repo_exec", answers_question=True)
    assert far.intervals_overlap is False and far.state == "SETTLED_AS_PREDICTED"

    # A SINGLE REPETITION HAS NO INTERVAL, and None is not False.
    one = _overlap(ArmMeasurement(role="control", value=1.0, uncertainty=0.0, n=1),
                   ArmMeasurement(role="treatment", value=9.0, uncertainty=0.0, n=1))
    assert one is None

    # --- the vocabularies are closed --------------------------------------------------
    seen = [got, against, obs, synth, other, zero, big, small, wrong, close, far,
            *cases.values()]
    assert {c.state for c in seen} <= set(BETWEEN_ARM_STATES)
    assert {c.refusal for c in seen} <= set(BETWEEN_ARM_REFUSALS)
    assert {c.authority for c in seen} <= set(VALIDATION_AUTHORITY)

    # NO COMPARISON IS BOTH REFUSED AND AUTHORITATIVE, over the whole reachable space.
    for c in seen:
        assert (c.state == "REFUSED") == (c.authority == "NONE"), c.refusal
        assert c.establishes_defect <= (c.authority == "CONFORMANT_CONTROLLED_RESULT")
        assert not c.establishes_attribution

    # --- and NOTHING here reads a printed value ---------------------------------------
    src = inspect.getsource(compare) + inspect.getsource(_settle)
    for forbidden in ("claimed_value", "claimed_delta", "claimed_cell_value", "table_ref"):
        assert forbidden not in src, forbidden
    print("harness.between_arms self-check ok")


if __name__ == "__main__":
    _self_check()
