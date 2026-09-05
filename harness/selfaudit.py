"""Did the review actually do the work it claims to have done? A deterministic checklist
over the finished report. Pure: artifacts in, verdict per item out. No model, no I/O.

The reviewer spec ends with a self-audit — "did I verify every serious finding, did I
recompute the numbers, did I try to falsify my strongest criticism, did I separate
questions from findings" — and instructs that if any answer is no, the analysis
continues. A prompt cannot honestly ask a model to grade its own diligence: the model
that skipped the falsification step is the model answering "yes, I falsified it". So
every item here is checked against a HARNESS-WRITTEN field or the mere PRESENCE of a
lens-written one, never against a model's assessment of itself:

  CHECKED FROM A HARNESS-WRITTEN FIELD   evidence_class, verification_state, calc_class,
                                         evidence_origin, counted_severity, grade_state
  CHECKED AS PRESENT, NOT AS TRUE        steelman, confidence, severity_rationale — the
                                         harness cannot judge whether a steelman is any
                                         good, only that one was written and is not
                                         degenerate (`grading.pass_b_state` already does
                                         the non-degeneracy half)

WHAT A FAILED ITEM DOES, AND DOES NOT DO. It does NOT move the verdict. The RED/
GREEN call is a threshold table over `counted_severity` and nothing here touches it —
adding a second, softer path to a color would make the table advisory. What it does is
refuse to let the review present itself as complete: `ReviewSelfAudit.complete` is False,
the report prints every failed item by name with the findings responsible, and a reader
can see exactly which part of the discipline this particular review did not exercise.

`python -m harness.selfaudit` runs the self-check.
"""
from __future__ import annotations

from .artifacts import EvalReport, Finding, SelfAuditItem, ReviewSelfAudit

# The numeric discrepancy types whose whole content IS an arithmetic claim. A finding of
# one of these kinds that never produced a machine-reproducible calculation is asserting
# something about numbers on a reading rather than on the arithmetic (spec §6).
_NUMERIC = ("ARITHMETIC_ERROR", "DIFFERENT_DENOMINATOR", "GENUINE_CONTRADICTION")

# What counts as "serious" for the purposes of this checklist: what the verdict actually
# counted, not what a lens asserted. A MAJOR that grading already capped to MINOR is not
# something this review is still resting on, so holding it to the serious-finding bar
# would report a diligence gap the review does not have.
_SERIOUS = ("FATAL", "MAJOR")


def _label(f: Finding) -> str:
    return f.finding_id or (f.title[:40] if f.title else "(unnamed)")


def _item(key: str, question: str, offenders: list[str], total: int, detail: str,
          applicable: bool = True) -> SelfAuditItem:
    if not applicable or total == 0:
        return SelfAuditItem(key=key, question=question, state="not_applicable",
                             detail="nothing in scope for this check")
    if offenders:
        return SelfAuditItem(key=key, question=question, state="fail", detail=detail,
                             offenders=offenders[:12], n_offenders=len(offenders),
                             n_in_scope=total)
    return SelfAuditItem(key=key, question=question, state="pass", n_in_scope=total,
                         detail=f"all {total} in scope")


def audit(report: EvalReport, counted) -> ReviewSelfAudit:
    """The checklist over one finished `EvalReport`.

    `counted` is passed in rather than imported — it lives in `stages/report.py`, which
    imports THIS module, and a module that imported it back would be a cycle for the
    convenience of one two-line function.
    """
    fs = list(report.findings)
    serious = [f for f in fs if counted(f) in _SERIOUS]
    numeric = [f for f in fs if f.discrepancy_type in _NUMERIC]
    graded = [f for f in serious if f.grade is not None]

    items = [
        _item("serious_findings_verified",
              "Did I verify every serious finding against the paper?",
              [_label(f) for f in serious if not f.verified_observation],
              len(serious),
              "a counted FATAL/MAJOR carries no harness-written verified_observation, so "
              "nothing machine-checked its evidence"),
        _item("serious_findings_cell_backed",
              "Is every serious finding anchored in a checkable table cell?",
              [f"{_label(f)} ({f.evidence_class})" for f in serious
               if f.evidence_class != "cell_verified"],
              len(serious),
              "a counted FATAL/MAJOR rests on prose, a caption or an equation rather than "
              "an addressed cell — checkable, but not in seconds"),
        _item("numbers_independently_recomputed",
              "Did I independently recompute the numbers a numeric finding turns on?",
              [f"{_label(f)} ({f.calc_class or 'no calculation'})" for f in numeric
               if f.calc_class != "recomputed_ok"],
              len(numeric),
              "a finding whose claim IS an arithmetic claim produced no calculation the "
              "harness could re-verify operand by operand"),
        _item("alternative_interpretation_tested",
              "Did I test whether an alternative interpretation resolves the issue?",
              [f"{_label(f)} ({f.verification_state or 'not assessed'})" for f in serious
               if f.verification_state != "complete"],
              len(serious),
              "the falsification/steelman triple on a counted FATAL/MAJOR is blank, "
              "degenerate, or predates the schema that requires it"),
        _item("authors_steelmanned",
              "Did I construct the strongest defense of the authors?",
              [_label(f) for f in serious if not f.steelman.strip()],
              len(serious),
              "a counted FATAL/MAJOR carries no steelman at all"),
        _item("questions_separated_from_findings",
              "Did I separate reviewer questions from confirmed findings?",
              [_label(f) for f in fs if not f.candidate_class and f.finding_class == "UNGRADED"],
              len(fs),
              "a finding was never sorted into CONFIRMED_FINDING / PLAUSIBLE_CONCERN / "
              "OPEN_QUESTION / DISMISSED by either reader, so it is counted as a defect "
              "without anyone having said it is one"),
        _item("confidence_separate_from_severity",
              "Did I state confidence separately from severity?",
              [_label(f) for f in serious if not f.confidence],
              len(serious),
              "a counted FATAL/MAJOR states no confidence, so how sure anyone is and how "
              "bad it is have collapsed into one number"),
        _item("severity_argued_by_impact",
              "Did I argue severity by impact rather than by a checklist?",
              [_label(f) for f in serious if not f.severity_rationale.strip()],
              len(serious),
              "a counted FATAL/MAJOR gives no reason for that grade over the one below it"),
        _item("evidence_provenance_recorded",
              "Did I distinguish internal paper evidence from inference?",
              [_label(f) for f in fs if not f.evidence_origin],
              len(fs),
              "a finding's evidence_ref names no shape the harness recognises, so what "
              "KIND of evidence it is was never established"),
        _item("graded_by_a_second_reader",
              "Did a second, independent reader weigh every serious candidate?",
              [_label(f) for f in serious if f.grade is None],
              len(serious),
              "a counted FATAL/MAJOR carries only the asserting lens's own judgement "
              "(grading off, out of scope, or it did not complete)"),
        _item("previous_conclusions_not_inherited",
              "Did the second reader reach its answer from the paper, not from the first?",
              [_label(f) for f in graded
               if f.grade is not None and not f.grade.reached_independently],
              len(graded),
              "the grader reports it worked from the first reader's argument rather than "
              "from the paper, so its agreement is not independent evidence"),
        _item("whole_paper_judged_independently",
              "Did I judge the paper as a whole, separately from counting findings?",
              [] if report.substantive_verdict is not None else ["(no substantive read)"],
              1,
              "no whole-paper assessment was produced, so the only judgement on record is "
              "the threshold table's count"),
    ]
    failed = [i.key for i in items if i.state == "fail"]
    return ReviewSelfAudit(
        items=items, failed=failed, complete=not failed,
        summary=(f"{sum(1 for i in items if i.state == 'pass')} of "
                 f"{sum(1 for i in items if i.state != 'not_applicable')} applicable "
                 f"check(s) passed"
                 + (f"; unmet: {', '.join(failed)}" if failed else "")),
    )


if __name__ == "__main__":  # self-check: python -m harness.selfaudit
    from .artifacts import Grade, SubstantiveVerdict

    def _counted(f):
        return f.counted_severity or f.severity

    # A review that did everything the discipline asks.
    good = Finding(
        finding_id="a-01", lens="contradiction", severity="MAJOR", title="t",
        statement="s", evidence_quote="q", evidence_ref="T1:r0:c0",
        evidence_class="cell_verified", verified_observation="checked",
        verification_state="complete", calc_class="recomputed_ok",
        evidence_origin="PAPER_TABLE", candidate_class="CONFIRMED_FINDING",
        confidence="HIGH", severity_rationale="because the headline claim rests on it",
        steelman="the authors could reasonably say the split was disclosed",
        discrepancy_type="ARITHMETIC_ERROR",
        grade=Grade(verdict="CONFIRMED", severity="MAJOR", confidence="HIGH",
                    reached_independently=True))
    clean = audit(EvalReport(paper_id="p", findings=[good],
                             substantive_verdict=SubstantiveVerdict(verdict="STRONG")),
                  _counted)
    assert clean.complete and not clean.failed, clean.summary
    assert all(i.state in ("pass", "not_applicable") for i in clean.items)

    # Every item must be individually reachable as a failure, or it is decoration.
    for key, mutate in [
            ("serious_findings_verified", {"verified_observation": ""}),
            ("serious_findings_cell_backed", {"evidence_class": "prose_verified"}),
            ("numbers_independently_recomputed", {"calc_class": "not_attempted"}),
            ("alternative_interpretation_tested", {"verification_state": "legacy"}),
            ("authors_steelmanned", {"steelman": ""}),
            ("confidence_separate_from_severity", {"confidence": ""}),
            ("severity_argued_by_impact", {"severity_rationale": ""}),
            ("evidence_provenance_recorded", {"evidence_origin": ""}),
            ("graded_by_a_second_reader", {"grade": None}),
            ("previous_conclusions_not_inherited",
             {"grade": Grade(verdict="CONFIRMED", reached_independently=False)}),
    ]:
        res = audit(EvalReport(paper_id="p", findings=[good.model_copy(update=mutate)],
                               substantive_verdict=SubstantiveVerdict(verdict="STRONG")),
                    _counted)
        assert key in res.failed, (key, res.failed)
        assert not res.complete

    # `questions_separated_from_findings` needs an unsorted finding, which the fixture is not.
    unsorted = audit(EvalReport(
        paper_id="p", findings=[good.model_copy(update={"candidate_class": "",
                                                        "finding_class": "UNGRADED"})],
        substantive_verdict=SubstantiveVerdict(verdict="STRONG")), _counted)
    assert "questions_separated_from_findings" in unsorted.failed, unsorted.failed

    # No substantive read is itself an unmet check.
    no_whole = audit(EvalReport(paper_id="p", findings=[good]), _counted)
    assert "whole_paper_judged_independently" in no_whole.failed

    # A paper with NO serious findings is not thereby a lazy review: every
    # serious-finding check is out of scope, and the checklist says so rather than
    # passing vacuously or failing.
    minor = good.model_copy(update={"severity": "MINOR", "discrepancy_type": "NOT_APPLICABLE"})
    empty = audit(EvalReport(paper_id="p", findings=[minor],
                             substantive_verdict=SubstantiveVerdict(verdict="STRONG")),
                  _counted)
    assert empty.complete, empty.summary
    assert any(i.state == "not_applicable" for i in empty.items)
    # A grading-capped MAJOR is not held to the serious bar — it is not what the verdict
    # counted, so demanding a steelman for it would report a gap the review does not have.
    capped = good.model_copy(update={"severity": "MAJOR", "counted_severity": "MINOR",
                                     "steelman": "", "discrepancy_type": "NOT_APPLICABLE"})
    assert "authors_steelmanned" not in audit(
        EvalReport(paper_id="p", findings=[capped],
                   substantive_verdict=SubstantiveVerdict(verdict="STRONG")), _counted).failed

    print("selfaudit self-check OK")
