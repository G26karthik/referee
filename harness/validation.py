"""The smallest legitimate experiment that would discriminate the explanations.

`python -m harness.validation` runs the self-check.

**What this route is for, and what it is not.** A referee reading a results table asks a
small number of questions that re-running a published number cannot answer: two things
changed between the arms and the paper credits one of them; the control the claim needs
was never run; the arms saw different data budgets; the protocol that produced the number
is not the protocol the claim is about. Each of those is settled by a NEW experiment that
varies exactly one thing — and by nothing else, because the number the paper printed is
consistent with every one of the competing explanations at once.

So this is not a general experiment generator. The question it asks is always:

    what is the SMALLEST scientifically legitimate experiment, derivable from the paper
    and its pinned artifact, that discriminates between the competing explanations?

**"Derivable" is the entire discipline, and `NEVER_ASSUMED` is how it is enforced.** An
experiment that answers the question by choosing an optimizer, a split, an augmentation
strength, a threshold or a schedule the paper never stated measures OUR choice. Its result
would be a fact about this review's reconstruction wearing the clothes of a fact about the
paper. Every such choice is named, and a design that would need one this harness had to
supply is `SPECIFICATION_BLOCKED` with the key printed — never completed from convention,
however conventional the value is.

**The comparison comes from the claim graph, not from a second representation of it.**
`claimgraph.build` already derives COMPARISON nodes — one per (metric, benchmark) the
paper reports for two or more methods — and those nodes carry the arms, their addresses
and their printed values. Rebuilding that here would be a second inventory of the paper's
own contrasts, free to disagree with the first.

**Three halves are deterministic and one is a proposal.** The question and its address,
the arms and their addresses, the metric and its unit, the benchmark and the stated split
are all read off the document. What a model may PROPOSE is the configuration of each arm,
which variable the experiment changes, which it holds fixed, and the settlement rule with
its tolerance and basis. Every proposed value must then RELOCATE — the quotation it came
from must re-mint to an address in this paper — or the ingredient is unbound and the
design is blocked.

**The settlement condition is declared before anything runs.** There is no universal
percentage anywhere in this route. A rule chosen after the numbers arrived settles
whatever the author of the rule wanted it to settle, so `SettlementCondition` carries
`declared_before_execution`, the harness writes it when the design is persisted, and
`between_arms.compare` refuses a comparison that does not have it.
"""
from __future__ import annotations

import re

from . import claims
from .artifacts import (ArmComparison, ArmMeasurement, FocusedValidation, NEVER_ASSUMED,
                        PaperDoc, SETTLEMENT_RULES, SettlementCondition,
                        VALIDATION_INGREDIENTS, ValidationArm, ValidationDesign)

# The review-question kinds a controlled experiment answers. Deliberately three, and
# deliberately not PRINTED_QUANTITY or COMPOSITION: a question about what a number IS is
# settled by producing that number again, which is a reproduction and already has a route.
# Running a new contrast to answer it would be a more expensive way of learning less.
VALIDATION_QUESTION_KINDS = ("ATTRIBUTION", "CONTROL_PRESENCE", "PROTOCOL_CONFORMANCE")

# WHICH SETTLEMENT RULES EACH QUESTION ADMITS. `between_arms.compare` computes whatever
# rule it is given; this table decides whether the rule that fired answers the question the
# design was built for, which is a different question and the one `answers_question` is.
# An equivalence margin settles "are these two protocols interchangeable" and settles
# nothing at all about "which of two changes produced the gain".
RULES_FOR_QUESTION = {
    "ATTRIBUTION": ("DIRECTION_AGREES", "EFFECT_EXCEEDS_TOLERANCE"),
    "CONTROL_PRESENCE": ("DIRECTION_AGREES", "EFFECT_EXCEEDS_TOLERANCE"),
    "PROTOCOL_CONFORMANCE": ("EFFECT_EXCEEDS_TOLERANCE",
                             "EFFECT_WITHIN_EQUIVALENCE_MARGIN",
                             "ARMS_INDISTINGUISHABLE"),
}

# A comparison needs two arms. Three would be a study.
_MIN_ARMS = 2

# At least two readings, or there is nothing to discriminate between. One explanation is a
# hypothesis; an experiment that can only confirm it is not a discriminating experiment.
_MIN_EXPLANATIONS = 2

# The split words a paper actually writes. Matched only inside a sentence that also names
# the benchmark, so "we report test accuracy" three sections away from the table does not
# become this comparison's split.
_SPLIT_WORDS = {
    "test set": "test", "test split": "test", "the test": "test", "testing set": "test",
    "validation set": "validation", "validation split": "validation", "dev set": "validation",
    "development set": "validation", "held-out set": "held-out", "heldout set": "held-out",
    "held out set": "held-out", "training set": "train", "train split": "train",
}

# A unit the paper printed beside the number, taken from the cell itself. A bare number has
# no unit and that is an unbound ingredient, not a licence to assume percent: an accuracy
# reported as a fraction and a cell printed as a percentage differ by a hundred, which is
# the exact units error `local_exec.reconcile`'s scale guard exists to catch one layer down.
_UNIT = re.compile(r"(?:^|\d)\s*(%|pp|MB|GB|GiB|MiB|ms|s|min|h|x|×|FLOPs|GFLOPs|M|B)\s*$")


def applicable(question_kind: str = "") -> bool:
    """Is this a question a controlled experiment could answer? Vocabulary string only."""
    return (question_kind or "") in VALIDATION_QUESTION_KINDS


def rules_for(question_kind: str = "") -> tuple[str, ...]:
    return RULES_FOR_QUESTION.get(question_kind or "", ())


def answers_question(design: ValidationDesign | None, rule: str = "") -> bool:
    """Does a comparison settled by `rule` answer the question this design was built for?

    WRITTEN BY THE HARNESS and never by the layer that computed the comparison. A
    perfectly computed equivalence result answers an equivalence question; carrying it
    into an attribution question would settle a question nobody asked with a number
    nobody disputes.
    """
    if design is None or not design.established:
        return False
    return (rule or "") in rules_for(design.question_kind)


# --------------------------------------------------------------------------- #
# Reading the document — the deterministic half
# --------------------------------------------------------------------------- #
def unit_at(doc: PaperDoc, ref: str = "") -> str:
    """The unit the paper printed beside the number at `ref`, or ''.

    Read off the cell, never inferred from the metric's name. "accuracy" is reported as a
    percentage in most papers and as a fraction in some, and a route that guessed would be
    wrong by a factor of a hundred on the papers that do it the other way.
    """
    if not (ref or "").strip():
        return ""
    got = claims.resolve(doc, ref)
    if got.resolution != "resolved":
        return ""
    m = _UNIT.search((got.quote or "").strip())
    return m.group(1) if m else ""


def stated_split(doc: PaperDoc, benchmark: str = "") -> tuple[str, str]:
    """(split, address) the paper states FOR THIS BENCHMARK, or ('', '').

    Both halves matter. A split named in a sentence that does not name the benchmark is a
    different experiment's split, and a split this harness could not find an address for
    is a split nobody can check — so the address is minted here and an ambiguous quotation
    (one `claims.mint` refuses because it occurs twice) yields nothing rather than a
    reference a reader cannot open.
    """
    needle = " ".join((benchmark or "").split()).lower()
    if not needle:
        return "", ""
    for _idx, _title, _offsets, flat, _page in claims.section_units(doc):
        low = flat.lower()
        if needle not in low:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", flat):
            slow = sentence.lower()
            if needle not in slow:
                continue
            for phrase, split in _SPLIT_WORDS.items():
                if phrase in slow:
                    got = claims.mint(doc, sentence.strip())
                    if got.resolution == "resolved":
                        return split, got.ref
    return "", ""


def arms_from_comparison(doc: PaperDoc, members: list, *, treatment_method: str = ""
                         ) -> list[ValidationArm]:
    """The paper's own comparison, as a control arm and a treatment arm.

    `members` are `claimgraph.ResultNode`s of one COMPARISON node — the same objects the
    graph built, not a second reading of the table. The treatment arm is the method the
    claim credits; the control is the one it is compared against. When more than two
    methods share the comparison, the arms are the credited method and the member it most
    directly displaces — and picking that is a judgement, so this returns nothing unless
    the caller NAMED the treatment: a control chosen by position is a control nobody chose.
    """
    named = " ".join((treatment_method or "").split()).lower()
    if not named:
        return []
    treatment = next((m for m in members
                      if " ".join((m.method or "").split()).lower() == named), None)
    if treatment is None:
        return []
    others = [m for m in members if m is not treatment and (m.method or "").strip()]
    if len(others) != 1:
        # Zero: the paper reports one method, which is a measurement and not a comparison.
        # Two or more: which one this arm displaces is a reading, and a control picked by
        # list order would be a scientific choice made by an enumeration.
        return []
    control = others[0]

    def arm(role: str, node) -> ValidationArm:
        return ValidationArm(role=role, label=node.method or node.label or role,
                             address=node.address or "", method=node.method or "",
                             source="paper",
                             instantiation_basis=f"the paper reports this arm at "
                                                 f"{node.address or 'an unrecovered address'}")

    return [arm("control", control), arm("treatment", treatment)]


# --------------------------------------------------------------------------- #
# Building the design
# --------------------------------------------------------------------------- #
def _assumed_keys(arms: list[ValidationArm]) -> list[str]:
    """Every `NEVER_ASSUMED` choice an arm carries that came from this harness.

    Keyed on the ARM'S OWN `source`, not on the value: a learning rate the paper states
    and a learning rate this review picked are the same string, and only one of them may
    reach an experiment held against the paper.
    """
    out: set[str] = set()
    for a in arms:
        if (a.source or "") == "harness":
            out.update(k for k in a.configuration if k in NEVER_ASSUMED)
    return sorted(out)


def conformance(design: ValidationDesign) -> tuple[str, str]:
    """(state, basis). Is this the PAPER's experiment, or this review's?

    Derived, never proposed. Two conditions: every arm was instantiated from the paper or
    the pinned artifact, and no `NEVER_ASSUMED` choice was supplied by this harness. A
    design failing either is `DEVIATES` — which is not a defect and not a refusal. It is
    the honest label for an experiment worth running whose result is about the experiment.
    """
    if not design.arms:
        return "UNASSESSED", "no arms were instantiated."
    mine = [a for a in design.arms if (a.source or "") not in ("paper", "artifact")]
    if mine:
        where = ", ".join(f"the {a.role} arm" for a in mine)
        return ("DEVIATES",
                f"{where} was assembled by this review rather than read from the paper or "
                f"the pinned checkout, so what the comparison varies is partly our choice.")
    assumed = _assumed_keys(design.arms)
    if assumed:
        return ("DEVIATES",
                f"this review would have had to supply {', '.join(assumed)}, which the "
                f"paper does not state.")
    return ("CONFORMANT",
            "every arm's configuration was read from the paper or the pinned checkout, "
            "and no scientific choice in it is this review's.")


def design(doc: PaperDoc, *, target_id: str = "", question_id: str = "",
           question_kind: str = "", question_ref: str = "", question_text: str = "",
           competing_explanations: list[str] | None = None,
           comparison_id: str = "", metric: str = "", benchmark: str = "",
           members: list | None = None, treatment_method: str = "",
           changed_variables: list[str] | None = None,
           controlled_variables: list[str] | None = None,
           arm_configuration: dict[str, dict[str, str]] | None = None,
           configuration_source: str = "",
           settlement: SettlementCondition | None = None,
           statistical_unit: str = "") -> ValidationDesign:
    """The design, or the named reason one cannot be built.

    Every ingredient in `VALIDATION_INGREDIENTS` is checked and the unbound ones are
    listed. `state` is DERIVED from `missing` and `assumed` rather than set, so a design
    cannot be reported DESIGNED while something it needs is absent — the same reason
    `taxonomy.resolution_state` is derived from the evidence state rather than stored
    beside it.
    """
    explanations = [e.strip() for e in (competing_explanations or []) if e.strip()]
    changed = sorted({v.strip() for v in (changed_variables or []) if v.strip()})
    controlled = sorted({v.strip() for v in (controlled_variables or []) if v.strip()})
    configs = dict(arm_configuration or {})
    source = (configuration_source or "").strip()

    out = ValidationDesign(
        design_id=f"fv:{target_id or question_id or 'unbound'}",
        paper_id=doc.paper_id, target_id=target_id, question_id=question_id,
        question_kind=question_kind, question_text=question_text,
        competing_explanations=explanations, comparison_id=comparison_id,
        metric=metric, benchmark=benchmark, changed_variables=changed,
        controlled_variables=controlled, settlement=settlement,
        statistical_unit=statistical_unit)

    missing: list[str] = []

    # 1. THE ADDRESSED QUESTION. Re-resolved here rather than trusted: a question whose
    #    address does not re-derive is a question a referee cannot open.
    if applicable(question_kind) and (question_ref or "").strip():
        got = claims.resolve(doc, question_ref)
        if got.resolution == "resolved":
            out.question_ref = got.ref
        else:
            missing.append("addressed_question")
    else:
        missing.append("addressed_question")

    # 2. COMPETING EXPLANATIONS, carried from the finding's own counter-explanations.
    if len(explanations) < _MIN_EXPLANATIONS:
        missing.append("competing_explanations")

    # 3. THE ARMS.
    arms = arms_from_comparison(doc, list(members or []),
                                treatment_method=treatment_method)
    for a in arms:
        cfg = configs.get(a.role) or configs.get(a.label) or {}
        if cfg:
            a.configuration = {str(k): str(v) for k, v in cfg.items()}
            a.source = source or "harness"
        a.varies = list(changed) if a.role == "treatment" else []
    out.arms = arms
    if len(arms) < _MIN_ARMS:
        missing.append("arm_instantiation")

    # 4. METRIC IDENTITY — the quantity, and the unit the paper printed beside it. Both
    #    arms, because a unit recovered on one side proves nothing about the other.
    units = {a.role: unit_at(doc, a.address) for a in arms}
    out.metric_unit = next((u for u in units.values() if u), "")
    if not metric.strip() or not out.metric_unit or len(set(units.values())) > 1:
        missing.append("metric_identity")

    # 5. DATASET IDENTITY — the benchmark AND the split. NEVER defaulted to 'test'.
    split, split_ref = stated_split(doc, benchmark)
    out.split = split
    if not benchmark.strip() or not split:
        missing.append("dataset_identity")
    elif split_ref:
        out.metric_ref = out.metric_ref or split_ref

    # 6/7. WHAT IS HELD FIXED AND WHAT MOVES. Exactly one changed variable: an experiment
    #      that varies two things discriminates between neither, which is the very defect
    #      an attribution question is raised about.
    if not controlled:
        missing.append("controlled_variables")
    if len(changed) != 1:
        missing.append("changed_variable")

    # 8. THE SETTLEMENT CONDITION, and it has to be one this question admits.
    if (settlement is None or settlement.rule not in SETTLEMENT_RULES
            or settlement.predicted_direction in ("", "UNSTATED")
            or settlement.rule not in rules_for(question_kind)):
        missing.append("settlement_condition")
    elif settlement.rule in ("EFFECT_EXCEEDS_TOLERANCE",
                             "EFFECT_WITHIN_EQUIVALENCE_MARGIN") and (
            settlement.tolerance is None or not settlement.tolerance_basis.strip()):
        # A TOLERANCE WITH NO BASIS IS A NUMBER THIS REVIEW INVENTED. Blocked with the
        # settlement ingredient rather than silently defaulted, because a margin chosen
        # here decides every comparison this design will ever produce.
        missing.append("settlement_condition")

    out.missing = sorted(set(missing), key=VALIDATION_INGREDIENTS.index)
    out.assumed = _assumed_keys(arms)
    out.conformance, out.conformance_basis = conformance(out)

    if out.missing or out.assumed:
        out.state = "SPECIFICATION_BLOCKED"
        parts = []
        if out.missing:
            parts.append("the paper does not bind " + ", ".join(out.missing))
        if out.assumed:
            parts.append("this review would have had to choose " + ", ".join(out.assumed))
        out.reason = ("; ".join(parts) +
                      ". Inventing the missing half would produce a result about our "
                      "experiment rather than about the paper, so the question is "
                      "reported open.")
        return out

    out.state = "DESIGNED"
    control, treatment = out.control, out.treatment
    out.statement = (
        f"holding {', '.join(controlled)} fixed and changing only {changed[0]}, compare "
        f"{treatment.label} against {control.label} on {metric} over the {split} split of "
        f"{benchmark}. {settlement.statement or ''}").strip()
    return out


# --------------------------------------------------------------------------- #
# What the route made of it
# --------------------------------------------------------------------------- #
def measurement_authority(m: ArmMeasurement | None) -> str:
    """Level 1. One arm that produced a number is a fact about that run and nothing else."""
    return "ARM_MEASUREMENT" if (m is not None and m.value is not None) else "NONE"


def outcome_disposition(fv: FocusedValidation | None) -> tuple[str, str]:
    """(disposition, reason) for one focused-validation route attempt.

    Four terminal states, and the split that matters is between an observation and a
    result. `VALIDATION_OBSERVATION_ONLY` is the honest default: two arms really ran and
    really were compared, and the conformance that would let the comparison say something
    about the PAPER was not established. It resolves nothing and says so.
    """
    if fv is None:
        return ("NOT_ATTEMPTED", "the focused-validation route did not run.")
    d = fv.design
    if d is None or not d.established:
        return ("SPECIFICATION_BLOCKED",
                (d.reason if d else "no design was produced.") or
                "the experiment could not be derived from the paper.")
    c = fv.comparison
    if c is None or c.state == "REFUSED":
        why = (c.reason if c else "") or "the arms were never compared."
        refusal = (c.refusal if c else "") or "no comparison was performed"
        if refusal in ("arm_missing", "arm_produced_no_value"):
            return ("INCONCLUSIVE", f"the experiment was built and ran and {why}")
        return ("COMPARISON_BLOCKED", f"the arms could not be held against each other: {why}")
    if c.establishes_defect:
        return ("VALIDATION_DEFECT_ESTABLISHED", c.statement)
    if c.supports_claim:
        return ("VALIDATION_SUPPORTS_CLAIM", c.statement)
    if c.state == "NOT_SETTLED":
        return ("VALIDATION_INCONCLUSIVE",
                "the controlled comparison ran and the rule declared for it fired in "
                "neither direction, so the question stays open: " + c.statement)
    return ("VALIDATION_OBSERVATION_ONLY", c.statement)


def discharge(fv: FocusedValidation | None) -> bool:
    """Did this route settle the question it was taken for?

    A comparison that reached an authority AND answers the question. A design that was
    blocked, a run whose arms could not be compared, and a controlled observation whose
    conformance was never established all leave this False — the same property
    `literature.discharge` has, and for the same reason: a route that discharged by
    failing to conclude is an exhaustion number that rises fastest where least was
    established.
    """
    if fv is None or fv.comparison is None:
        return False
    c = fv.comparison
    return (c.state in ("SETTLED_AS_PREDICTED", "SETTLED_AGAINST_PREDICTION")
            and c.answers_question
            and c.authority in ("CONFORMANT_CONTROLLED_RESULT",))


def summarise(fv: FocusedValidation | None) -> dict:
    """Counts a reader can check, and never one that sums two different things."""
    if fv is None:
        return {"designed": 0, "specification_blocked": 0, "arms_measured": 0,
                "compared": 0, "settled": 0, "launched": 0}
    d = fv.design
    c = fv.comparison
    return {
        "designed": int(bool(d is not None and d.established)),
        "specification_blocked": int(bool(d is not None and not d.established)),
        "arms_measured": sum(1 for m in fv.measurements if m.value is not None),
        "compared": int(bool(c is not None and c.state != "REFUSED")),
        "settled": int(discharge(fv)),
        "launched": fv.launched,
    }


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect
    from types import SimpleNamespace

    from .artifacts import (Section, TARGET_DISPOSITIONS, VALIDATION_CONFORMANCE_STATES,
                            VALIDATION_DESIGN_STATES)
    from . import between_arms

    for name, p in inspect.signature(applicable).parameters.items():
        assert str(p.annotation) == "str", f"{name}: {p.annotation}"
    assert set(RULES_FOR_QUESTION) == set(VALIDATION_QUESTION_KINDS)
    for rules in RULES_FOR_QUESTION.values():
        assert set(rules) <= set(SETTLEMENT_RULES)
    assert not applicable("PRINTED_QUANTITY") and not applicable("")
    assert applicable("ATTRIBUTION")

    prose = ("We evaluate every method on the CIFAR-100 test set and report top-1 "
             "accuracy. Our method LDReg improves over the AugOnly baseline. The gain "
             "could come from the regulariser or from the longer schedule.")
    doc = PaperDoc(paper_id="p", title="t",
                   sections=[Section(section_idx=0, title="Setup", text=prose, page=1)])
    node = lambda method, addr: SimpleNamespace(  # noqa: E731 — a ResultNode stand-in
        method=method, address=addr, label=method, value=None)
    members = [node("AugOnly", ""), node("LDReg", "")]

    def settle(rule="DIRECTION_AGREES", tol=None, basis="", pred="INCREASE"):
        return SettlementCondition(rule=rule, tolerance=tol, tolerance_basis=basis,
                                   predicted_direction=pred, declared_before_execution=True,
                                   statement="the regulariser arm should score higher.")

    def build(**kw):
        base = dict(target_id="t1", question_id="q1", question_kind="ATTRIBUTION",
                    question_ref="", question_text="is the gain the regulariser?",
                    competing_explanations=["the regulariser", "the longer schedule"],
                    comparison_id="comparison:accuracy|cifar-100", metric="accuracy",
                    benchmark="CIFAR-100", members=members, treatment_method="LDReg",
                    changed_variables=["regulariser"], controlled_variables=["schedule"],
                    settlement=settle(), statistical_unit="example")
        base.update(kw)
        return design(doc, **base)

    # --- THE DOCUMENT IS READ, NOT ASSUMED --------------------------------------------
    assert stated_split(doc, "CIFAR-100")[0] == "test"
    assert stated_split(doc, "ImageNet") == ("", ""), "a split for another benchmark is not ours"
    assert stated_split(doc, "") == ("", "")

    # --- THE ARMS ---------------------------------------------------------------------
    arms = arms_from_comparison(doc, members, treatment_method="LDReg")
    assert [a.role for a in arms] == ["control", "treatment"]
    assert arms[0].label == "AugOnly" and arms[1].label == "LDReg"
    assert arms_from_comparison(doc, members, treatment_method="") == [], (
        "a control chosen by list order is a scientific choice made by an enumeration")
    assert arms_from_comparison(doc, members + [node("Third", "")],
                                treatment_method="LDReg") == []
    assert arms_from_comparison(doc, [node("Only", "")], treatment_method="Only") == []

    # --- EVERY INGREDIENT IS REQUIRED, AND THE UNBOUND ONE IS NAMED --------------------
    blocked = {
        "addressed_question": build(question_kind="PRINTED_QUANTITY"),
        "competing_explanations": build(competing_explanations=["only one"]),
        "arm_instantiation": build(treatment_method="NotAMethod"),
        "dataset_identity": build(benchmark="ImageNet"),
        "controlled_variables": build(controlled_variables=[]),
        "changed_variable": build(changed_variables=["regulariser", "schedule"]),
        "settlement_condition": build(settlement=None),
    }
    for want, got in blocked.items():
        assert got.state == "SPECIFICATION_BLOCKED", want
        assert want in got.missing, (want, got.missing)
        assert got.reason and not got.established, want
    assert set(blocked) | {"metric_identity"} == set(VALIDATION_INGREDIENTS)

    # A RULE THE QUESTION DOES NOT ADMIT IS NOT A SETTLEMENT CONDITION.
    wrong_rule = build(settlement=settle(rule="EFFECT_WITHIN_EQUIVALENCE_MARGIN", tol=1.0,
                                         basis="the paper's own reported variance",
                                         pred="NO_CHANGE"))
    assert "settlement_condition" in wrong_rule.missing, (
        "an equivalence margin settles nothing about which of two changes caused a gain")

    # A TOLERANCE WITH NO BASIS IS A NUMBER THIS REVIEW INVENTED.
    no_basis = build(question_kind="ATTRIBUTION",
                     settlement=settle(rule="EFFECT_EXCEEDS_TOLERANCE", tol=1.0))
    assert "settlement_condition" in no_basis.missing

    # --- THE UNIT COMES OFF THE CELL, NEVER OFF THE METRIC'S NAME ---------------------
    assert build().state == "SPECIFICATION_BLOCKED"
    assert "metric_identity" in build().missing, (
        "these arms have no recovered address, so no unit was printed beside either")
    assert unit_at(doc, "") == "" and unit_at(doc, "T9:r1:c1") == ""

    # --- CONFORMANCE IS DERIVED, AND A HARNESS CHOICE IS NEVER CONFORMANT --------------
    paper_arm = ValidationArm(role="control", source="paper")
    mine = ValidationArm(role="treatment", source="harness",
                         configuration={"learning_rate": "0.1"})
    assert conformance(ValidationDesign(arms=[paper_arm]))[0] == "CONFORMANT"
    assert conformance(ValidationDesign(arms=[paper_arm, mine]))[0] == "DEVIATES"
    assert conformance(ValidationDesign())[0] == "UNASSESSED"
    assert _assumed_keys([mine]) == ["learning_rate"]
    assert _assumed_keys([ValidationArm(role="t", source="paper",
                                        configuration={"learning_rate": "0.1"})]) == [], (
        "a learning rate the PAPER states is not an assumption")

    # --- THE FULL DESIGN, once every ingredient really is bound ------------------------
    ok = build()
    ok.arms[0].address = "T1:r1:c1"
    full = ValidationDesign(**{**ok.model_dump(), "metric_unit": "%", "split": "test",
                               "missing": [], "assumed": [], "state": "DESIGNED"})
    assert full.established and full.control.role == "control"

    # --- DISPOSITIONS -----------------------------------------------------------------
    def fv(comparison=None, d=full):
        return FocusedValidation(paper_id="p", target_id="t1", design=d,
                                 comparison=comparison)

    assert outcome_disposition(None)[0] == "NOT_ATTEMPTED"
    assert outcome_disposition(fv(d=build()))[0] == "SPECIFICATION_BLOCKED"
    assert outcome_disposition(fv())[0] == "COMPARISON_BLOCKED"
    assert outcome_disposition(fv(ArmComparison(state="REFUSED", refusal="arm_missing",
                                                reason="one arm."))) [0] == "INCONCLUSIVE"
    for state, authority, answers, want in (
            ("SETTLED_AGAINST_PREDICTION", "CONFORMANT_CONTROLLED_RESULT", True,
             "VALIDATION_DEFECT_ESTABLISHED"),
            ("SETTLED_AS_PREDICTED", "CONFORMANT_CONTROLLED_RESULT", True,
             "VALIDATION_SUPPORTS_CLAIM"),
            ("SETTLED_AGAINST_PREDICTION", "CONTROLLED_OBSERVATION", True,
             "VALIDATION_OBSERVATION_ONLY"),
            ("SETTLED_AGAINST_PREDICTION", "CONFORMANT_CONTROLLED_RESULT", False,
             "VALIDATION_OBSERVATION_ONLY"),
            ("NOT_SETTLED", "CONTROLLED_OBSERVATION", True, "VALIDATION_INCONCLUSIVE")):
        c = ArmComparison(state=state, authority=authority, answers_question=answers,
                          statement="s.")
        got, why = outcome_disposition(fv(c))
        assert got == want, (state, authority, answers, got)
        assert got in TARGET_DISPOSITIONS and why
        assert discharge(fv(c)) == (want in ("VALIDATION_DEFECT_ESTABLISHED",
                                             "VALIDATION_SUPPORTS_CLAIM"))

    # A BLOCKED DESIGN DISCHARGES NOTHING, and neither does a refused comparison.
    assert not discharge(None) and not discharge(fv()) and not discharge(fv(d=build()))

    # --- `answers_question` IS THE HARNESS'S, AND IT IS KEYED ON THE QUESTION ---------
    assert answers_question(full, "DIRECTION_AGREES")
    assert not answers_question(full, "EFFECT_WITHIN_EQUIVALENCE_MARGIN")
    assert not answers_question(build(), "DIRECTION_AGREES"), "a blocked design answers nothing"
    assert not answers_question(None, "DIRECTION_AGREES")

    # --- and the arithmetic layer agrees about what it may say ------------------------
    assert between_arms.authority_for(state="SETTLED_AS_PREDICTED", conformance="CONFORMANT",
                                      provenance="repo_exec") == "CONFORMANT_CONTROLLED_RESULT"
    assert between_arms.authority_for(state="SETTLED_AS_PREDICTED", conformance="CONFORMANT",
                                      provenance="synthesized") == "CONTROLLED_OBSERVATION"

    assert measurement_authority(None) == "NONE"
    assert measurement_authority(ArmMeasurement(role="control")) == "NONE"
    assert measurement_authority(ArmMeasurement(role="control", value=1.0)) == "ARM_MEASUREMENT"

    # --- vocabularies -----------------------------------------------------------------
    assert {d.state for d in [full, *blocked.values()]} <= set(VALIDATION_DESIGN_STATES)
    assert {d.conformance for d in [full, *blocked.values()]} <= set(
        VALIDATION_CONFORMANCE_STATES)
    assert set(summarise(None)) == set(summarise(fv()))

    # --- NOTHING IN THIS MODULE DEFAULTS A SCIENTIFIC CHOICE --------------------------
    src = inspect.getsource(design) + inspect.getsource(stated_split)
    for never in NEVER_ASSUMED:
        assert f'"{never}"' not in src or never == "split", never
    print("harness.validation self-check ok")


if __name__ == "__main__":
    _self_check()
