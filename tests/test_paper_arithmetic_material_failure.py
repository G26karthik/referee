"""A deterministic paper-internal arithmetic contradiction, and the materiality gate.

**Two defects, closed in order.**

1. `harness.claims.parse_quantity` could always recompute a printed composition and say
   whether it evaluates (`arithmetic_ok`), and nothing downstream consumed a `False`:
   `_paper_only_outcome` filed it under the same disposition an AGREEING composition gets,
   `establishes_failure` only recognised `FAILED_REPRODUCTION`, `claim_status` never read
   an arithmetic signal, and `disposition`'s `PAPER_ARITHMETIC` basis was never fed by the
   one real call site. A deterministic, no-model, no-execution contradiction of the
   paper's own arithmetic was computed correctly and thrown away.

2. Wiring it up exposed a SECOND defect, older and wider: `claim_status` treated ANY
   established failure as a paper-level material failure. An appendix footnote and the
   paper's headline number reached `STOP_MATERIAL_FAILURE` by the same path — and that was
   already true for `repo_exec`/`reimpl_exec` before arithmetic existed. Established defect
   and material failure are different claims, and only one of them stops a paper.

**The three tiers these tests pin.**

    Tier 1  TargetOutcome.establishes_failure   this target's route established a defect
    Tier 2  harness.materiality                 a CENTRAL claim is established to depend on it
    Tier 3  claim_status / overall_verdict / disposition -> STOP_MATERIAL_FAILURE

Tier 2 is a CONSERVATIVE SUFFICIENT CONDITION, not a materiality model: the only basis
implemented is `ABSTRACT_CLAIM`. Everything that clears Tier 1 and fails Tier 2 stays
established, visible, ledger-recorded and reportable — it only loses the authority to
stop the paper. Under-stopping is the accepted direction; over-stopping is not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import controller, disposition as disposition_mod, materiality, state
from harness.artifacts import DiscoveredObject, PaperDoc, Section, TargetOutcome
from harness.config import Config
from harness.provenance import ADMISSIBLE_REPRODUCTION_PROVENANCE
from harness.stages import audit as audit_stage
from harness.stages import report as report_stage

CONTRADICTED = "58 topics x 5 templates x 10 instances = 2,901 test cases"
CONSISTENT = "58 topics x 5 templates x 10 instances = 2,900 test cases"
INCIDENTAL = "12 x 3 = 40 scratch annotation files"


# --------------------------------------------------------------------------- #
# Fixtures — no repository and no findings, so the ONLY thing that can establish
# anything is the deterministic recheck of the paper's own printed arithmetic.
#
# Section 0 is the untitled front-matter block, exactly as real extraction produces it
# (title and authors), so these fixtures also prove the materiality locator finds the
# Abstract by its own HEADING rather than by position.
# --------------------------------------------------------------------------- #
def _doc(pid: str, *, abstract_composition: str = "", appendix_composition: str = "") -> PaperDoc:
    abstract = "We introduce a benchmark and evaluate every method on it."
    if abstract_composition:
        abstract = (f"We introduce a benchmark. We construct it by generating "
                    f"{abstract_composition}, and evaluate every method on it.")
    sections = [
        Section(section_idx=0, title="", page_start=1,
                text="A Synthetic Benchmark Paper\nA. Author, B. Author"),
        Section(section_idx=1, title="Abstract", page_start=1, text=abstract),
        Section(section_idx=2, title="1. Introduction", page_start=2,
                text="Benchmarks for this task are scarce."),
    ]
    if appendix_composition:
        sections.append(Section(
            section_idx=3, title="Appendix D: incidental bookkeeping", page_start=9,
            text=f"For completeness, the pilot annotation round used "
                 f"{appendix_composition}, which are not used anywhere in the evaluation."))
    return PaperDoc(paper_id=pid, title="A Synthetic Benchmark Paper", n_pages=9,
                    content_sha="c" * 12, sections=sections)


def _plant_empty_lenses(cfg: Config, pid: str) -> None:
    """Every lens accepted, every lens finding nothing. No Finding, no counted FATAL, and
    no lens opinion of any kind can be the thing that established a failure below."""
    for lens in audit_stage.LENSES:
        audit_stage.accept_lens(cfg, pid, lens, json.dumps({"lens": lens, "findings": []}))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path / "projects")


def _drive(cfg: Config, pid: str, doc: PaperDoc):
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    state.create_project(cfg, "", doc.title, pid=pid)
    state.write_json(cfg.projects_dir / pid / "paper" / "doc.json", doc.model_dump())
    _plant_empty_lenses(cfg, pid)
    case = controller.drive(cfg, controller.open_case(cfg, pid))
    assert case.status == "complete", case.blocked_reason
    read = lambda name: json.loads(                                    # noqa: E731
        (cfg.projects_dir / pid / "reports" / name).read_text(encoding="utf-8"))
    return case, read(f"{pid}.json"), read(f"{pid}.ledger.json")


def _arithmetic_entry(ledger):
    return next(e for e in ledger["entries"]
                if e.get("disposition") == "PAPER_ARITHMETIC_CONTRADICTION")


# --------------------------------------------------------------------------- #
# 1 — MATERIAL: the contradiction is in the paper's own Abstract
# --------------------------------------------------------------------------- #
def test_an_abstract_arithmetic_contradiction_stops_the_paper(cfg: Config):
    case, report, ledger = _drive(cfg, "arith-abstract",
                                  _doc("arith-abstract", abstract_composition=CONTRADICTED))

    # Tier 3 — the terminal decision, from the real pipeline, nothing hand-fed
    assert case.disposition == "STOP_MATERIAL_FAILURE", case.disposition
    assert case.disposition_basis == "PAPER_ARITHMETIC", case.disposition_basis
    assert report["disposition"] == "STOP_MATERIAL_FAILURE"
    assert report["claim_status"] == "VERIFIED_FAILURE"
    assert case.verdict == "RED"
    assert report["outcome"]["finding_state"] == "MATERIAL_FAILURE_ESTABLISHED"

    # Tier 1 and Tier 2, both visible in the trace
    entry = _arithmetic_entry(ledger)
    assert entry["materiality_basis"] == "ABSTRACT_CLAIM"
    assert entry["provenance"] == "paper"
    assert entry["evidence_state"] == "PAPER_INTERNAL_EVIDENCE"
    assert entry["resolution_state"] == "RESOLVED_FROM_PAPER"
    assert entry["concerns_the_paper"] is True

    # zero execution anywhere in the run
    assert entry["launched"] == 0
    assert ledger["efficiency"]["processes_launched"] == 0
    assert ledger["efficiency"]["executions_completed"] == 0

    # and the report says what happened, in the right words
    review = Path(report["reviewer_report_path"]).read_text(encoding="utf-8")
    assert "## Established failures" in review
    assert "Paper-internal arithmetic contradiction" in review
    established = review.split("## Established failures", 1)[1].split("## Scientific", 1)[0]
    assert "the authors' own artifact" not in established
    assert "materiality: `ABSTRACT_CLAIM`" in established
    assert "rejects the paper" in established
    assert "reproduction" not in (report.get("claim_status_reason") or "").lower()

    # THE EXECUTION ROW MUST NOT CLAIM AN EXECUTION. Both states that say anything about
    # the paper name the authors' own code having RUN, and nothing ran here.
    assert report["outcome"]["execution_state"] != "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY"
    assert "own code ran" not in review.split("## Review outcome", 1)[1].split("##", 1)[0]


# --------------------------------------------------------------------------- #
# 2 — NON-MATERIAL: the same defect, in an appendix
# --------------------------------------------------------------------------- #
def test_an_appendix_arithmetic_contradiction_is_established_but_does_not_stop(cfg: Config):
    case, report, ledger = _drive(cfg, "arith-appendix",
                                  _doc("arith-appendix", appendix_composition=INCIDENTAL))

    # Tier 1 held: the defect is real, established, and reported
    entry = _arithmetic_entry(ledger)
    assert entry["materiality_basis"] == "NONE"
    assert entry["concerns_the_paper"] is True, "it is still evidence about the paper"
    assert entry["resolution_state"] == "RESOLVED_FROM_PAPER"
    assert "NOT consistent" in entry["observed"]
    assert ledger["efficiency"]["targets_settled"] >= 1

    review = Path(report["reviewer_report_path"]).read_text(encoding="utf-8")
    assert "## Established failures" in review, "an established defect is always reported"
    assert "Paper-internal arithmetic contradiction" in review
    # …and the entry says, per target, that it did NOT reject the paper, so the section
    # heading cannot be read as a rejection.
    established = review.split("## Established failures", 1)[1].split("## Scientific", 1)[0]
    assert "materiality: `NONE`" in established
    assert "paper is NOT rejected on it" in established
    assert report["outcome"]["execution_state"] != "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY"

    # Tier 2 failed, so Tier 3 must not fire — from THIS defect or at all
    assert report["claim_status"] != "VERIFIED_FAILURE"
    assert case.verdict != "RED"
    assert case.disposition != "STOP_MATERIAL_FAILURE"
    assert report["disposition_basis"] != "PAPER_ARITHMETIC"
    assert report["outcome"]["finding_state"] != "MATERIAL_FAILURE_ESTABLISHED"

    # …and it is NOT CLEAN. CLEAN may never mean "we established something objectively
    # wrong and it was not central enough to reject."
    assert case.disposition == "PASS_TO_HUMAN_CONCERNS", case.disposition
    assert "did establish a defect" in report["disposition_reason"]
    assert "NOT a clean paper" in report["disposition_reason"]


# --------------------------------------------------------------------------- #
# 3 — correct arithmetic never stops the paper
# --------------------------------------------------------------------------- #
def test_correct_arithmetic_never_stops_the_paper(cfg: Config):
    case, report, ledger = _drive(cfg, "arith-consistent",
                                  _doc("arith-consistent", abstract_composition=CONSISTENT))

    assert case.disposition != "STOP_MATERIAL_FAILURE"
    assert case.verdict != "RED"
    assert report["claim_status"] == "NOT_VERIFIED", (
        "a correct composition proves that ONE relation holds, never the paper's "
        "correctness — it must not read as VERIFIED_SUPPORT either")
    assert not [e for e in ledger["entries"]
                if e.get("disposition") == "PAPER_ARITHMETIC_CONTRADICTION"]
    review = Path(report["reviewer_report_path"]).read_text(encoding="utf-8")
    assert "## Established failures" not in review


def test_an_agreeing_composition_settles_nothing_positive_even_when_reached():
    """`discovery._routes` only offers ARITHMETIC_RECHECK when the arithmetic is already
    broken, so the agreeing branch of `_paper_only_outcome` is not reachable from the
    routing table. Where it IS reached directly, it must never read as VERIFIED_SUPPORT:
    that state requires an admissible RECONCILIATION and the paper-only route produces
    none."""
    from harness.artifacts import ClaimRef, PlanDecision, ReportedQuantity
    from harness.stages import discover as discover_stage

    obj = DiscoveredObject(
        target_id="T", ref=ClaimRef(kind="prose_claim", ref="P1:0-20", quote=CONSISTENT,
                                    quantity=ReportedQuantity(raw="2900", expression="58*5*10",
                                                              arithmetic_ok=True)))
    plan = PlanDecision(target_id="T", action="PAPER_ONLY_RESOLUTION", route="ARITHMETIC_RECHECK")
    out = discover_stage._paper_only_outcome(obj, plan)
    assert out.disposition == "PAPER_ONLY_RESOLVED"
    assert not out.establishes_failure
    assert report_stage.claim_status([], outcomes=[out], objects=[obj])[0] == "NOT_VERIFIED"


# --------------------------------------------------------------------------- #
# 4 & 5 — the SAME materiality primitive governs every route
# --------------------------------------------------------------------------- #
def _outcome(disposition: str, provenance: str = "", target_id: str = "T") -> TargetOutcome:
    return TargetOutcome(target_id=target_id, disposition=disposition,
                         provenance=provenance, reason="fixture")


MATERIAL = [DiscoveredObject(target_id="T", materiality_basis="ABSTRACT_CLAIM")]
INCIDENTAL_OBJ = [DiscoveredObject(target_id="T", materiality_basis="NONE")]

EVERY_ESTABLISHING_ROUTE = [
    ("PAPER_ARITHMETIC_CONTRADICTION", "paper"),
    ("FAILED_REPRODUCTION", "repo_exec"),
    ("FAILED_REPRODUCTION", "reimpl_exec"),
    ("FAILED_REPRODUCTION", "driver"),
]


@pytest.mark.parametrize("disposition,provenance", EVERY_ESTABLISHING_ROUTE)
def test_every_route_is_gated_by_the_same_materiality_rule(disposition, provenance):
    o = _outcome(disposition, provenance)
    assert o.establishes_failure, "Tier 1 must hold for all four"

    assert report_stage.claim_status([], outcomes=[o], objects=MATERIAL)[0] == "VERIFIED_FAILURE"
    assert report_stage.overall_verdict([], None, outcomes=[o], objects=MATERIAL)[0] == "RED"

    assert report_stage.claim_status([], outcomes=[o], objects=INCIDENTAL_OBJ)[0] == "NOT_VERIFIED"
    assert report_stage.overall_verdict([], None, outcomes=[o],
                                        objects=INCIDENTAL_OBJ)[0] == "GREEN"


@pytest.mark.parametrize("disposition,provenance", EVERY_ESTABLISHING_ROUTE)
def test_a_non_material_established_defect_is_still_reported_as_established(
        disposition, provenance):
    """Tier 1 survives Tier 2 failing. The defect is not downgraded, hidden or renamed —
    it loses only the authority to stop the paper."""
    o = _outcome(disposition, provenance)
    assert o.establishes_failure
    assert o.concerns_the_paper or disposition == "PAPER_ARITHMETIC_CONTRADICTION"
    _, why = report_stage.overall_verdict([], None, outcomes=[o], objects=INCIDENTAL_OBJ)
    assert "established defect" in why and "none rejects the paper" in why


@pytest.mark.parametrize("disposition,provenance", EVERY_ESTABLISHING_ROUTE)
def test_a_non_material_established_defect_never_reaches_clean(disposition, provenance):
    """Tier 1 without Tier 2 routes to the human-concern disposition, for EVERY route —
    `repo_exec` and `reimpl_exec` included. CLEAN is reserved for a review that
    established nothing."""
    o = _outcome(disposition, provenance)
    assert o.establishes_failure
    established = materiality.established_defects([o])
    assert established == [o] and materiality.has_established_defect([o])
    assert materiality.material_target_failure(INCIDENTAL_OBJ, [o]) is None

    d, basis, why = disposition_mod.derive(
        claim_status="NOT_VERIFIED", established_non_material=len(established))
    assert d == "PASS_TO_HUMAN_CONCERNS", (disposition, provenance, d)
    assert d != "PASS_TO_HUMAN_CLEAN" and basis == "NONE"
    assert "NOT a clean paper" in why


def test_clean_requires_that_nothing_at_all_was_established():
    """The invariant, at the disposition boundary."""
    assert disposition_mod.derive()[0] == "PASS_TO_HUMAN_CLEAN"
    for kw in ({"established_non_material": 1}, {"central_unresolved": 1},
               {"counted_major": 1}, {"central_blockers": ("ARTIFACT",)},
               {"claim_status": "VERIFIED_FAILURE"}):
        assert disposition_mod.derive(**kw)[0] != "PASS_TO_HUMAN_CLEAN", kw


def test_blocker_precedence_is_unchanged_by_the_established_defect_rule():
    """A BLOCKED_* central target still outranks an established non-material defect: "we
    could not check the paper's central claim" is what a referee needs first."""
    for cls, expected in disposition_mod._BLOCKER_PRECEDENCE:
        got, _, _ = disposition_mod.derive(central_blockers=(cls,),
                                           established_non_material=5,
                                           central_unresolved=5, counted_major=5)
        assert got == expected, (cls, got)


def test_an_unresolved_central_claim_without_an_established_defect_is_unresolved():
    """Rule 3 of the four-way distinction. Reached here through `disposition.derive`; see
    the module docstring of this file for what is and is not proven end to end."""
    assert disposition_mod.derive(central_unresolved=1)[0] == "PASS_TO_HUMAN_UNRESOLVED"
    assert disposition_mod.derive(central_unresolved=1,
                                  counted_major=3)[0] == "PASS_TO_HUMAN_UNRESOLVED"
    # …and an established defect outranks it, because proven outranks open
    assert disposition_mod.derive(central_unresolved=1,
                                  established_non_material=1)[0] == "PASS_TO_HUMAN_CONCERNS"


def test_a_missing_materiality_join_never_falls_through_to_clean():
    """Both guards fire: the orphan is not material (so no STOP), it IS an established
    defect (so not CLEAN), and the inconsistency is reported as a harness defect."""
    from harness import guarantees as guarantees_mod
    from harness.artifacts import EvalReport, TargetSet

    orphan = _outcome("FAILED_REPRODUCTION", "repo_exec", target_id="GHOST")
    assert materiality.material_target_failure(MATERIAL, [orphan]) is None
    assert materiality.has_established_defect([orphan])
    d, _, _ = disposition_mod.derive(
        claim_status="NOT_VERIFIED",
        established_non_material=len(materiality.established_defects([orphan])))
    assert d == "PASS_TO_HUMAN_CONCERNS" and d != "PASS_TO_HUMAN_CLEAN"

    report = EvalReport(paper_id="p", title="t", verdict="GREEN", claim_status="NOT_VERIFIED",
                        ledger_path="reports/p.ledger.json", review_efficiency={"a": 1})
    g = guarantees_mod.derive(report, TargetSet(paper_id="p", objects=list(MATERIAL),
                                                outcomes=[orphan]))
    assert "ACCOUNTING_REPRODUCIBLE" in g.unmet


def test_the_materiality_primitive_has_no_route_specific_branch():
    """One rule for arithmetic and another for execution would be two rules. Asserted on
    the primitive's own source, with its docstring removed."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(materiality.material_target_failure).lstrip())
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = "\n".join(ast.unparse(n) for n in body)
    for token in ("repo_exec", "reimpl_exec", "PAPER_ARITHMETIC", "provenance", "disposition"):
        assert token not in code, token


# --------------------------------------------------------------------------- #
# 6 — the missing join is an internal invariant failure, never "material"
# --------------------------------------------------------------------------- #
def test_an_established_failure_with_no_object_is_never_material():
    orphan = _outcome("PAPER_ARITHMETIC_CONTRADICTION", "paper", target_id="GHOST")
    assert orphan.establishes_failure
    assert materiality.basis_for_target("GHOST", MATERIAL) is None
    assert report_stage.claim_status([], outcomes=[orphan], objects=MATERIAL)[0] == "NOT_VERIFIED"
    assert report_stage.overall_verdict([], None, outcomes=[orphan],
                                        objects=MATERIAL)[0] == "GREEN"


def test_a_missing_materiality_join_is_reported_as_a_harness_defect():
    """Through the mechanism this codebase already uses for a count it cannot re-derive
    from artifacts: a PROCESS guarantee that does not hold, which reaches `unmet` and is
    printed as a defect in this harness — never as evidence about the paper."""
    from harness import guarantees as guarantees_mod
    from harness.artifacts import EvalReport, TargetSet

    orphan = _outcome("PAPER_ARITHMETIC_CONTRADICTION", "paper", target_id="GHOST")
    report = EvalReport(paper_id="p", title="t", verdict="GREEN", claim_status="NOT_VERIFIED",
                        ledger_path="reports/p.ledger.json", review_efficiency={"a": 1})
    ts = TargetSet(paper_id="p", objects=list(MATERIAL), outcomes=[orphan])

    g = guarantees_mod.derive(report, ts)
    assert "ACCOUNTING_REPRODUCIBLE" in g.unmet
    detail = next(x for x in g.guarantees if getattr(x, "key", "") == "ACCOUNTING_REPRODUCIBLE")
    assert "GHOST" in detail.evidence or "materiality" in detail.evidence

    # and with the object present the same review is clean on that guarantee
    ok = guarantees_mod.derive(report, TargetSet(
        paper_id="p", objects=[DiscoveredObject(target_id="GHOST")], outcomes=[orphan]))
    assert "ACCOUNTING_REPRODUCIBLE" not in ok.unmet


# --------------------------------------------------------------------------- #
# 7 — claim_status and overall_verdict cannot disagree
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("objects", [MATERIAL, INCIDENTAL_OBJ, [], None])
@pytest.mark.parametrize("disposition,provenance", EVERY_ESTABLISHING_ROUTE)
def test_claim_status_and_overall_verdict_cannot_disagree(objects, disposition, provenance):
    o = _outcome(disposition, provenance)
    status, _ = report_stage.claim_status([], outcomes=[o], objects=objects)
    verdict, _ = report_stage.overall_verdict([], None, outcomes=[o], objects=objects)
    assert (verdict == "RED") == (status == "VERIFIED_FAILURE"), (objects, status, verdict)


def test_the_two_deciders_share_one_primitive():
    """Not two implementations that happen to agree: one call, made twice."""
    import inspect

    for fn in (report_stage.claim_status, report_stage.overall_verdict):
        src = inspect.getsource(fn)
        assert "material_target_failure(" in src, fn.__name__
        assert "admissible_target_failure(" not in src, (
            f"{fn.__name__} must not reach the Tier-1 helper directly any more")


# --------------------------------------------------------------------------- #
# Semantic boundaries
# --------------------------------------------------------------------------- #
def test_arithmetic_mismatch_is_not_an_execution_failure():
    o = _outcome("PAPER_ARITHMETIC_CONTRADICTION", "paper")
    assert o.provenance not in ADMISSIBLE_REPRODUCTION_PROVENANCE
    status, why = report_stage.claim_status([], outcomes=[o], objects=MATERIAL)
    assert status == "VERIFIED_FAILURE"
    assert "reproduction" not in why.lower()


def test_arithmetic_mismatch_is_not_citation_verification():
    contradiction = _outcome("PAPER_ARITHMETIC_CONTRADICTION", "paper")
    citation = _outcome("CITATION_VERIFIED_ONLY", "paper")
    assert contradiction.establishes_failure and not citation.establishes_failure
    assert contradiction.resolution_state == "RESOLVED_FROM_PAPER"
    assert citation.resolution_state == "UNRESOLVED"


def test_arithmetic_mismatch_is_neither_an_artifact_nor_a_specification_failure():
    from harness.artifacts import BLOCKED_DISPOSITIONS

    assert "PAPER_ARITHMETIC_CONTRADICTION" not in BLOCKED_DISPOSITIONS
    for blocked in ("ARTIFACT_BLOCKED", "SPECIFICATION_BLOCKED"):
        o = _outcome(blocked)
        assert not o.establishes_failure
        assert report_stage.claim_status([], outcomes=[o], objects=MATERIAL)[0] == "NOT_VERIFIED"


def test_inadmissible_execution_cannot_become_material_by_its_number_alone():
    """Tier 1 first: a provenance the ceiling refuses establishes nothing, and sitting on
    a material target cannot rescue it."""
    for prov in ("synthesized", "template", "", "unknown"):
        o = _outcome("FAILED_REPRODUCTION", prov)
        assert not o.establishes_failure, prov
        assert report_stage.claim_status([], outcomes=[o], objects=MATERIAL)[0] == "NOT_VERIFIED"


def test_a_lens_asserting_bad_arithmetic_cannot_bypass_deterministic_verification():
    """`discrepancy_type=ARITHMETIC_ERROR` is a finding's SCIENTIFIC_CLASS. It feeds
    `taxonomy.classify`, but remains a model concern even if its counted severity is
    FATAL. It is a different mechanism from `PAPER_ARITHMETIC`, which exists only when
    `claims.parse_quantity` recomputed a printed composition."""
    from harness.artifacts import Finding

    f = Finding(finding_id="f1", lens="contradiction", severity="FATAL",
                candidate_class="CONFIRMED_FINDING", discrepancy_type="ARITHMETIC_ERROR",
                title="t", statement="s")
    assert report_stage.claim_status([f], outcomes=[], objects=[])[0] == "NOT_VERIFIED"
    assert disposition_mod.basis_for() == "NONE"


def test_infrastructure_and_resource_problems_cannot_become_a_paper_failure():
    for disposition in ("RESOURCE_BLOCKED", "ENVIRONMENT_BLOCKED", "AUTHORIZATION_BLOCKED",
                        "IDENTITY_BLOCKED", "COMPARISON_BLOCKED", "ADDRESSING_BLOCKED"):
        o = _outcome(disposition)
        assert not o.establishes_failure, disposition
        assert not o.concerns_the_paper, disposition
        assert report_stage.claim_status([], outcomes=[o], objects=MATERIAL)[0] == "NOT_VERIFIED"


# --------------------------------------------------------------------------- #
# The locator itself: by heading, never by position
# --------------------------------------------------------------------------- #
def test_materiality_finds_the_abstract_by_its_heading_not_by_position():
    doc = _doc("p", abstract_composition=CONTRADICTED, appendix_composition=INCIDENTAL)
    assert materiality.abstract_section_idx(doc) == 1
    assert doc.sections[0].section_idx == 0 and not doc.sections[0].title, (
        "section 0 is the untitled front-matter block, which is why the legacy positional "
        "locator in discovery._centrality fired on 0 of 878 corpus objects")


def test_centrality_is_untouched_and_is_no_longer_the_stop_gate(cfg: Config):
    """The appendix contradiction is still CENTRAL — `self_checking` still earns
    attention — and it is no longer material. That separation is the whole fix."""
    _, _, ledger = _drive(cfg, "arith-sep", _doc("arith-sep", appendix_composition=INCIDENTAL))
    entry = _arithmetic_entry(ledger)
    assert entry["materiality_basis"] == "NONE"

    from harness.stages import discover as discover_stage
    ts = discover_stage.load(cfg, "arith-sep")
    assert ts is not None
    obj = next(o for o in ts.objects if o.target_id == entry["target_id"])
    assert obj.centrality == "CENTRAL", "centrality is deliberately unchanged by this work"
    assert obj.materiality_basis == "NONE"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
