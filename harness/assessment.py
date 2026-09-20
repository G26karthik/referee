"""Can model findings stop the expensive branch before target evidence exists?

`python -m harness.assessment` runs the self-check.

At this point the pipeline has model findings and grader output, but no deterministic or
execution-derived target evidence. A model may propose a dependency for investigation;
it may not grant itself paper-level rejection authority. This phase therefore records an
open assessment. Dynamic early stop occurs only after a target outcome establishes a
defect and a paper-owned materiality basis binds it to a central claim.

**What it may read, and what it may not.** Findings only — the same inputs
`outcome.finding_state` is restricted to. It may not read a launch count, a
reconciliation, a recorded run origin, a coverage number or a document observation,
because a gate that could be moved by an execution outcome would let infrastructure decide
what a review established, which is invariants 4 to 7 in the one place they would be
hardest to notice.
"""
from __future__ import annotations

from .schema import Finding, PaperAssessment


def assess(findings: list[Finding]) -> PaperAssessment:
    """Leave investigation open until target-level evidence establishes material failure.

    At this phase the only scientific judgements are model findings and grades. They may
    prioritize a route, but cannot machine-establish the paper-owned dependency required
    for STOP. Dynamic early stop remains in the probe loop after deterministic or
    admissible execution evidence exists.
    """
    return PaperAssessment(
        material_failure_established=False, basis="NONE", counted_fatal_ids=[],
        reason=("model findings and grades may prioritize an investigation but cannot "
                "machine-establish a paper-owned materiality dependency. The investigation "
                "continues until deterministic paper evidence or an admissible bound route "
                "establishes a material target failure."))


def investigation_open(assessment: PaperAssessment | None) -> bool:
    """May the expensive branch proceed? Absent assessment means yes.

    Defaulting to OPEN rather than closed, which is the opposite of how every other gate
    in this harness defaults, and deliberately: this one does not protect a conclusion, it
    saves work. A missing assessment must degrade to the behaviour that existed before it
    — a full investigation — rather than silently suppressing every execution in the
    system. The gates that protect conclusions all still stand below it.
    """
    return assessment is None or not assessment.material_failure_established


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    # --- it reads FINDINGS and nothing else -------------------------------------------
    params = set(inspect.signature(assess).parameters)
    assert params == {"findings"}, params

    def _f(fid: str, severity: str, counted: str = "", lens: str = "overclaim") -> Finding:
        return Finding(finding_id=fid, lens=lens, severity=severity,
                       counted_severity=counted, candidate_class="CONFIRMED_FINDING",
                       title=f"title {fid}", statement="s")

    # --- nothing established -----------------------------------------------------------
    for findings in ([], [_f("a", "MINOR")], [_f("a", "MAJOR"), _f("b", "NOTE")]):
        a = assess(findings)
        assert not a.material_failure_established and a.basis == "NONE"
        assert a.counted_fatal_ids == [] and a.reason
        assert investigation_open(a) is True

    # --- a model FATAL remains an investigation proposal -------------------------------
    a = assess([_f("a", "FATAL"), _f("b", "MINOR")])
    assert not a.material_failure_established and a.basis == "NONE"
    assert a.counted_fatal_ids == []
    assert "cannot machine-establish" in a.reason
    assert investigation_open(a) is True

    # --- COUNTED severity, so every cap is inherited rather than re-litigated ----------
    demoted = _f("c", "FATAL", counted="MINOR")
    assert not assess([demoted]).material_failure_established, (
        "a lens-asserted FATAL the grader demoted must not stop a paper")
    assert investigation_open(assess([demoted])) is True

    # --- findings alone can never close the investigation ------------------------------
    for findings in ([], [_f("a", "FATAL")], [_f("a", "FATAL", counted="NOTE")],
                      [_f("a", "MAJOR"), _f("b", "FATAL")]):
        assert not assess(findings).material_failure_established, findings

    # --- a missing assessment degrades to the pre-existing behaviour --------------------
    assert investigation_open(None) is True

    # --- purity ------------------------------------------------------------------------
    fs = [_f("a", "FATAL")]
    before = [f.model_dump_json() for f in fs]
    assert assess(fs).model_dump() == assess(fs).model_dump()
    assert [f.model_dump_json() for f in fs] == before, "assess must not mutate its input"
    print("harness.assessment self-check ok")


if __name__ == "__main__":
    _self_check()
