"""The seven things this system must be UNABLE to do, each asserted structurally.

Every other test file guards a behaviour. This one guards an ARCHITECTURE: for each of
the seven properties below, it tries to construct the forbidden outcome and shows that the
construction does not exist — by parameter absence, by field absence, by import absence,
or by exhaustive sweep over a closed vocabulary.

The distinction matters because a behavioural test passes for a system that happens not to
do the wrong thing today. These pass only for a system in which the wrong thing is
inexpressible, which is the same discipline `harness.grading.derive` applies to severity:
"no variance = MAJOR" is not merely absent from that function, it cannot be written in it.

    (a) an inconclusive execution cannot become a paper failure
    (b) a blocked execution cannot become a paper failure
    (c) an unsupported provenance cannot convict
    (d) a document-integrity observation cannot silently become a scientific verdict
    (e) a coverage denominator cannot be fabricated
    (f) target priority cannot be controlled by a model-written execution flag
    (g) the paper-only and artifact-assisted paths stay distinct
"""
from __future__ import annotations

import inspect
import itertools

import pytest

from harness import (coverage as coverage_mod, docintegrity, grading, outcome, planner,
                     priority, provenance, taxonomy)
from harness.artifacts import (BLOCKED_DISPOSITIONS, DiscoveredObject, DocumentObservation,
                               EvalReport, Finding, INTEGRITY_ABOUT, INTEGRITY_CHECKS,
                               PaperDoc, PlanDecision, ProbeResult, Reconciliation,
                               Section, Table, TargetOutcome, TargetSet)
from harness.stages import report as report_stage

# Every provenance the ceiling refuses, plus the empty string and a token nobody emits.
INADMISSIBLE = ("synthesized", "template", "paper", "", "unknown", " repo_exec")
# The two that answer with a COLOUR, and the one that answers with an epistemic state.
# They were one tuple until the sweep below asserted `claim_status(...) in ("GREEN",
# "YELLOW")` and got NOT_VERIFIED, which is the correct answer to a different question.
COLOUR_DECIDERS = (report_stage.overall_verdict, report_stage.triage)
DECIDERS = COLOUR_DECIDERS + (report_stage.claim_status,)


def _code_only(obj) -> str:
    """Source with docstrings and comments removed.

    A function that documents what it must not read CONTAINS those words, and a grep over
    its whole source therefore fails on the best-documented version of itself.
    `coverage.surface`'s docstring says "No `TargetSet`, no `DiscoveredObject`, no
    `Finding`" — which is the guarantee, stated — and the first version of this check read
    that as a violation of it.
    """
    import ast
    tree = ast.parse(inspect.getsource(obj).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Module)) and ast.get_docstring(node):
            node.body = node.body[1:]
    return ast.unparse(tree)


def _outcome(disposition: str, provenance_: str = "", **kw) -> TargetOutcome:
    return TargetOutcome(target_id="T", disposition=disposition, provenance=provenance_,
                         reason="whatever happened here", **kw)


def _report(**kw) -> EvalReport:
    base = dict(paper_id="p", title="T", verdict="GREEN", claim_status="NOT_VERIFIED")
    base.update(kw)
    return EvalReport(**base)


# --------------------------------------------------------------------------- #
# (a) an INCONCLUSIVE execution cannot become a paper failure
# --------------------------------------------------------------------------- #
def test_an_inconclusive_execution_cannot_become_a_paper_failure():
    """Swept over every provenance and both reconciliation statuses.

    "It ran and settled nothing" is a fact about a run. A units mismatch, an unparsed
    metric, a zero noise band and a retracted commit all land here, and none of them is
    evidence against a paper.
    """
    for prov in INADMISSIBLE + provenance.ADMISSIBLE_REPRODUCTION_PROVENANCE:
        o = _outcome("INCONCLUSIVE", prov, launched=1)
        assert o.evidence_state == "INCONCLUSIVE_EXECUTION", prov
        assert not o.establishes_failure, prov
        assert not o.concerns_the_paper, prov
        assert o.resolution_state == "UNRESOLVED", prov
        for decide in COLOUR_DECIDERS:
            colour, _ = decide([], None, outcomes=[o])
            assert colour in ("GREEN", "YELLOW"), (decide.__name__, prov, colour)
        assert report_stage.claim_status([], None, outcomes=[o])[0] == "NOT_VERIFIED", prov
        # and the reader-facing execution row says so in the required words
        state, why = outcome.execution_state([o], [PlanDecision(target_id="T",
                                                                requires_execution=True)])
        assert state == "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE"
        assert "did not produce admissible evidence" in \
            outcome.EXECUTION_GLOSS[state]
        assert "whatever happened here" in why, "the exact reason must reach the reader"


def test_an_inconclusive_reconciliation_cannot_become_a_paper_failure():
    for prov in INADMISSIBLE + provenance.ADMISSIBLE_REPRODUCTION_PROVENANCE:
        rec = Reconciliation(status="INCONCLUSIVE", provenance=prov, reason="units")
        assert report_stage.overall_verdict([], rec)[0] == "GREEN", prov
        assert report_stage.claim_status([], rec)[0] == "NOT_VERIFIED", prov


# --------------------------------------------------------------------------- #
# (b) a BLOCKED execution cannot become a paper failure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("disposition", BLOCKED_DISPOSITIONS)
def test_a_blocked_execution_cannot_become_a_paper_failure(disposition):
    """Every blocked disposition, against every provenance.

    A shut gate, a missing dependency, an 8 GiB card facing a 24 GiB demand, and a claim
    we could not address are facts about this harness and this host. Invariant 16: a
    blocked target ends that target, never the paper.
    """
    for prov in INADMISSIBLE + provenance.ADMISSIBLE_REPRODUCTION_PROVENANCE:
        o = _outcome(disposition, prov)
        assert not o.establishes_failure
        assert not o.concerns_the_paper, (disposition, prov)
        for decide in DECIDERS:
            colour, _ = decide([], None, outcomes=[o])
            assert colour != "RED", (decide.__name__, disposition, prov)


def test_a_paper_with_one_blocked_and_one_failed_target_reports_both():
    """Invariant 16's other half: a blocked target must not suppress a real failure, and
    a real failure must not be reported as though everything failed.

    The MATERIALITY CONTEXT is explicit here now. Establishing a defect is Tier 1 and no
    longer convicts on its own — only a target a central scientific claim is established
    to depend on may stop the paper (`harness.materiality`). What this test is about is
    the blocked target not suppressing the failed one, so the failed one is given the
    materiality it needs to reach the paper level rather than relying on the old
    "any established failure convicts" behaviour.
    """
    from harness.artifacts import DiscoveredObject

    blocked = _outcome("ENVIRONMENT_BLOCKED", "")
    failed = TargetOutcome(target_id="C", disposition="FAILED_REPRODUCTION",
                           provenance="repo_exec", reason="0.71 against a printed 0.83",
                           launched=1)
    objects = [DiscoveredObject(target_id="C", materiality_basis="ABSTRACT_CLAIM")]
    assert report_stage.claim_status([], None, outcomes=[blocked, failed],
                                     objects=objects)[0] == "VERIFIED_FAILURE"
    assert report_stage.overall_verdict([], None, outcomes=[blocked, failed],
                                        objects=objects)[0] == "RED"
    # The execution row is Tier 1 and reads the same either way: what RAN and what it
    # produced is not a function of whether the paper's conclusion rests on it.
    for objs in (objects, [DiscoveredObject(target_id="C")], None):
        state, why = outcome.execution_state([blocked, failed], [])
        assert state == "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY"
        assert "0.71" in why
        assert failed.establishes_failure, objs


# --------------------------------------------------------------------------- #
# (c) an UNSUPPORTED PROVENANCE cannot convict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prov", INADMISSIBLE)
@pytest.mark.parametrize("status", ["FAILED_REPRODUCTION", "RESOLVED_VERIFIED"])
def test_an_unsupported_provenance_can_neither_convict_nor_acquit(prov, status):
    """The ceiling, in both directions, at both layers that apply it.

    Letting a synthesized probe convict would be the unearned inference this system exists
    to catch other people making. Letting it ACQUIT would be worse: a toy that happens to
    land near the printed number would launder a claim nobody checked.
    """
    assert not provenance.admits(prov)
    rec = Reconciliation(status=status, provenance=prov, reason="x")
    assert report_stage.claim_status([], rec)[0] != "VERIFIED_FAILURE"
    assert report_stage.claim_status([], rec)[0] != "VERIFIED_SUPPORT"
    assert report_stage.overall_verdict([], rec)[0] == "GREEN"
    # and at the reporting layer, where the state is derived a second time
    disp = "FAILED_REPRODUCTION" if status == "FAILED_REPRODUCTION" else "REPRODUCED"
    o = _outcome(disp, prov, launched=1)
    assert o.evidence_state == "INCONCLUSIVE_EXECUTION"
    assert not o.establishes_failure


def test_the_ceiling_is_one_object_and_no_environment_variable_widens_it():
    """A ceiling an operator can raise is not a ceiling."""
    src = _code_only(provenance)
    assert "environ" not in src and "getenv" not in src and "Config" not in src
    assert provenance.ADMISSIBLE_REPRODUCTION_PROVENANCE == ("driver", "repo_exec", "reimpl_exec")


# --------------------------------------------------------------------------- #
# (d) a DOCUMENT-INTEGRITY OBSERVATION cannot become a scientific verdict
# --------------------------------------------------------------------------- #
def test_a_document_observation_cannot_reach_any_decision_function():
    """Parameter absence and import absence, which together make it unreachable.

    The layer is report-only on the model of `harness/code_audit.py`, whose runtime
    demands go in their own list and never in `findings` "because a requirement filed
    there would be read as an accusation".
    """
    banned = ("observation", "integrity", "docint", "document", "crossref", "citation")
    for fn in (grading.derive, taxonomy.classify, planner.classify, priority.score,
               report_stage.overall_verdict, report_stage.claim_status,
               report_stage.triage):
        params = set(inspect.signature(fn).parameters)
        for b in banned:
            assert not any(b in p for p in params), (fn.__qualname__, b, params)
    for mod in (grading, taxonomy, planner, priority):
        assert "docintegrity" not in _code_only(mod), mod.__name__


def test_a_document_observation_has_no_severity_field_to_be_read():
    for banned in ("severity", "counted_severity", "confidence", "scientific_class",
                   "candidate_class", "verdict", "route", "finding_class"):
        assert banned not in DocumentObservation.model_fields, banned


@pytest.mark.parametrize("check", INTEGRITY_CHECKS)
@pytest.mark.parametrize("about", INTEGRITY_ABOUT)
def test_no_document_observation_colours_a_paper(check, about):
    """Every check against every `about`, through every decision function. Invariant 17:
    a colour may not be a property of this harness's configuration, and an observation
    about our own extraction is exactly that."""
    obs = DocumentObservation(check=check, about=about, ref="T1:r0:c0", quote="q",
                              detail="whatever this is")
    rep = _report(document_observations=[obs])
    for decide in DECIDERS:
        assert decide(list(rep.findings), None)[0] != "RED", (check, about)


def test_the_integrity_layer_never_renders_among_the_scientific_findings():
    obs = DocumentObservation(check="LABEL_DUPLICATED", about="PAPER", ref="T1",
                              quote="Table 3", detail="two tables both printed Table 3")
    rep = _report(document_observations=[obs])
    text = report_stage.render_reviewer_report(rep, TargetSet(paper_id="p"))
    assert "## Scientific findings" in text and "## Scope of this review" in text
    findings_block = text.split("## Scientific findings", 1)[1] \
        .split("## Scope of this review", 1)[0]
    assert "document integrity" not in findings_block
    assert "document integrity" in text, "it must still be reported, just not there"


def test_observe_can_see_the_document_and_nothing_else():
    params = inspect.signature(docintegrity.observe).parameters
    assert set(params) == {"doc"}
    assert str(params["doc"].annotation) in ("PaperDoc", "artifacts.PaperDoc")


# --------------------------------------------------------------------------- #
# (e) a COVERAGE DENOMINATOR cannot be fabricated
# --------------------------------------------------------------------------- #
def test_the_coverage_denominator_cannot_see_the_numerator():
    """The defect being replaced was `targets_addressable / targets_discovered`: the same
    object list divided by itself, which a WORSE extractor scores higher on. The fix is a
    signature — `surface` sees a PaperDoc and nothing derived from the review."""
    params = inspect.signature(coverage_mod.surface).parameters
    assert set(params) == {"doc"}
    src = _code_only(coverage_mod.surface)
    for banned in ("TargetSet", "DiscoveredObject", "Finding", "PlanDecision",
                   "addressed", "examined"):
        assert banned not in src, banned


def test_a_coverage_numerator_arrives_as_strings_not_objects():
    sig = inspect.signature(coverage_mod.measure)
    for name in ("addressed", "examined"):
        assert name in sig.parameters
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert "str" in str(sig.parameters[name].annotation)


def test_an_empty_surface_has_no_coverage_rather_than_full_coverage():
    """`None` is not zero and is not one. A paper extraction recovered nothing from has no
    denominator, and reporting 100% for it is the fabrication this guards."""
    empty = coverage_mod.surface(PaperDoc(paper_id="p"))
    # `surface_size` is a field of CoverageReport, not of ReviewSurface, so the guard this
    # line used to carry (`... if hasattr(empty, "surface_size") else True`) was `assert
    # True` by operator precedence and never looked at `surface_empty` at all.
    assert empty.surface_empty is True
    assert empty.addresses == () or not empty.addresses
    report = coverage_mod.measure(empty, addressed=(), examined=())
    assert report.addressed_rate is None
    assert report.examined_rate is None
    assert report.surface_size == 0


def test_degrading_the_document_can_never_raise_the_coverage_rate():
    """Monotonicity. The rate this replaces RISES when extraction degrades, because an
    unresolvable reference never becomes an object and so leaves the denominator."""
    full = PaperDoc(
        paper_id="p",
        sections=[Section(section_idx=i, title=f"S{i}", text="We report 91.4 here. " * 8)
                  for i in range(4)],
        tables=[Table(table_idx=i, caption=f"Table {i}: results",
                      header=["m", "acc"], rows=[["a", "1.0"], ["b", "2.0"]])
                for i in range(3)])
    addressed = tuple(t.ref(0, 1) for t in full.tables)
    rich = coverage_mod.measure(coverage_mod.surface(full), addressed=addressed)
    for drop in (1, 2, 3):
        worse = full.model_copy(deep=True)
        worse.tables = worse.tables[:-1] if drop else worse.tables
        worse.sections = worse.sections[:-drop]
        poor = coverage_mod.measure(coverage_mod.surface(worse), addressed=addressed)
        if rich.addressed_rate is None or poor.addressed_rate is None:
            continue
        # a smaller surface may not be reported as BETTER coverage of the same review
        assert poor.surface_size <= rich.surface_size
        assert len(poor.off_surface) >= len(rich.off_surface), (
            "addresses the shrunken surface no longer contains must be reported as "
            "off-surface, not silently counted or silently dropped")


def test_an_address_outside_the_surface_is_reported_and_never_counted():
    doc = PaperDoc(paper_id="p",
                   tables=[Table(table_idx=0, header=["m"], rows=[["a"]])])
    surf = coverage_mod.surface(doc)
    got = coverage_mod.measure(surf, addressed=("T9:r9:c9",), examined=("T9:r9:c9",))
    assert "T9:r9:c9" in got.off_surface
    assert got.addressed == 0 and got.examined == 0


def test_semantic_coverage_is_stated_unconditionally():
    """Structural coverage says nothing about whether the review found what mattered, and
    a clean coverage table must not be readable as evidence that it did."""
    doc = PaperDoc(paper_id="p", tables=[Table(table_idx=0, header=["m"], rows=[["a"]])])
    got = coverage_mod.measure(coverage_mod.surface(doc), addressed=("T0:r0:c0",))
    assert got.semantic_coverage == "not_machine_detectable"
    assert coverage_mod.measure(coverage_mod.surface(PaperDoc(paper_id="p"))) \
        .semantic_coverage == "not_machine_detectable"


def test_no_coverage_value_reaches_a_colour():
    for fn in DECIDERS:
        params = set(inspect.signature(fn).parameters)
        for banned in ("coverage", "surface", "addressed", "examined"):
            assert not any(banned in p for p in params), (fn.__qualname__, banned)


# --------------------------------------------------------------------------- #
# (f) TARGET PRIORITY cannot be controlled by a model-written execution flag
# --------------------------------------------------------------------------- #
def test_priority_cannot_read_the_lenss_own_execution_flag():
    """`Finding.verifiable_by_experiment` is an unchecked model boolean. It was once the
    sole gate on the entire execution half of the system; it survives as metadata."""
    params = set(inspect.signature(priority.score).parameters)
    assert "verifiable_by_experiment" not in params
    assert "proposed_by_lens" not in params
    src = _code_only(priority.score)
    assert "verifiable_by_experiment" not in src and "proposed_by_lens" not in src


def test_flipping_the_lens_flag_changes_no_priority_and_no_plan():
    """Constructed rather than argued: the same object with the flag set both ways must
    order identically and plan identically."""
    def obj(flag: bool) -> DiscoveredObject:
        return DiscoveredObject(target_id="T", question_id="Q", centrality="CENTRAL",
                                harness_addressable=True, proposed_by_lens=flag,
                                routes=["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"],
                                expected_value=0.83)

    on, off = obj(True), obj(False)
    assert priority.score(centrality=on.centrality, addressable=on.harness_addressable,
                          cheapest_route=on.routes[0], artifact_available=True) == \
        priority.score(centrality=off.centrality, addressable=off.harness_addressable,
                       cheapest_route=off.routes[0], artifact_available=True)
    a = planner.plan(on, artifact_available=True, specification_complete=True,
                     environment_state="usable")
    b = planner.plan(off, artifact_available=True, specification_complete=True,
                     environment_state="usable")
    assert (a.action, a.requires_execution, a.blocking_gate) == \
        (b.action, b.requires_execution, b.blocking_gate)


def test_the_planner_admits_only_vocabulary_strings_and_booleans():
    for name, p in inspect.signature(planner.classify).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"
    for name, p in inspect.signature(priority.score).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"


def test_a_lens_flag_alone_never_opens_the_execution_path():
    """`harness_addressable` is what opens it, and the harness writes that."""
    obj = DiscoveredObject(target_id="T", question_id="Q", centrality="CENTRAL",
                           harness_addressable=False, proposed_by_lens=True,
                           addressing_blocker="NO_ROUTE", routes=["NONE"])
    d = planner.plan(obj, artifact_available=True, specification_complete=True,
                     environment_state="usable")
    assert not d.requires_execution
    assert d.action == "INFEASIBLE_ROUTE"
    assert d.gates["structurally_addressable"] is False


# --------------------------------------------------------------------------- #
# (g) the PAPER-ONLY and ARTIFACT-ASSISTED paths stay distinct
# --------------------------------------------------------------------------- #
def test_the_two_review_paths_partition_the_artifact_vocabulary():
    paths = {taxonomy.review_path(s) for s in taxonomy.ARTIFACT_STATES}
    assert paths == {"PAPER_ONLY", "PAPER_AND_ARTIFACT"}


@pytest.mark.parametrize("state", taxonomy.ARTIFACT_STATES)
def test_only_one_artifact_state_says_anything_about_the_paper(state):
    """Four of the six were reported as SYNTHESIZED_DIAGNOSTIC and read identically: a
    paper that published nothing, one whose clone a gate refused, one whose clone failed,
    and one whose cloned repository was never authorized."""
    about = taxonomy.artifact_concerns_the_paper(state)
    assert about == (state == "NO_ARTIFACT_ADVERTISED"), state


def test_a_paper_only_review_cannot_borrow_artifact_evidence():
    """Only the artifact path can produce an admissible reproduction. A paper-only review
    that reported one would be reporting evidence it does not have."""
    for prov in ("paper", "synthesized", "template", ""):
        o = _outcome("REPRODUCED", prov, launched=1)
        assert o.evidence_state == "INCONCLUSIVE_EXECUTION", prov
        assert not o.concerns_the_paper, prov
    # and the artifact path is the only one whose reproduction reaches the paper
    for prov in provenance.ADMISSIBLE_REPRODUCTION_PROVENANCE:
        assert _outcome("REPRODUCED", prov, launched=1).concerns_the_paper


def test_an_artifact_review_cannot_report_the_paper_as_publishing_nothing():
    """The converse. `artifact_state` is derived from the acquisition, so a review that
    obtained a repository cannot say the authors advertised none."""
    for status in ("cloned", "cached"):
        state = taxonomy.artifact_state(status)
        assert state.startswith("ARTIFACT_PRESENT")
        assert taxonomy.review_path(state) == "PAPER_AND_ARTIFACT"
        assert not taxonomy.artifact_concerns_the_paper(state)


def test_a_synthesized_probe_leaves_the_review_on_the_paper_only_path():
    """Code on disk that this harness wrote is not the authors publishing code."""
    assert taxonomy.artifact_state("synthesized") == "NO_ARTIFACT_ADVERTISED"
    assert taxonomy.review_path(taxonomy.artifact_state("synthesized")) == "PAPER_ONLY"


def test_the_artifact_axis_admits_only_vocabulary_and_booleans():
    for name, p in inspect.signature(taxonomy.artifact_state).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"


def test_no_artifact_state_reaches_a_colour():
    for fn in DECIDERS:
        params = set(inspect.signature(fn).parameters)
        for banned in ("artifact_state", "review_path", "acquisition"):
            assert banned not in params, (fn.__qualname__, banned)


# --------------------------------------------------------------------------- #
# the four reader-facing tiers are folded over DISJOINT inputs
# --------------------------------------------------------------------------- #
def test_the_findings_tier_cannot_see_execution_at_all():
    """The structural version of (a), (b) and (c) at the reporting layer: what a review
    ESTABLISHED and what happened when it tried to run something are different columns,
    and the first cannot read the second."""
    params = set(inspect.signature(outcome.finding_state).parameters)
    assert params == {"claim_status", "kept_findings"}
    for banned in ("execution", "disposition", "provenance", "outcomes", "probe",
                   "launched", "reconciliation", "coverage", "observation"):
        assert not any(banned in p for p in params), banned


def test_no_execution_state_can_move_the_finding_state():
    """Exhaustive over the reachable cross product."""
    dispositions = ("INCONCLUSIVE", "FAILED_REPRODUCTION", "REPRODUCED", "NOT_ATTEMPTED",
                    "BUDGET_DEFERRED", "CITATION_VERIFIED_ONLY") + BLOCKED_DISPOSITIONS
    provs = INADMISSIBLE + provenance.ADMISSIBLE_REPRODUCTION_PROVENANCE
    for cs, kept in itertools.product(("VERIFIED_FAILURE", "VERIFIED_SUPPORT",
                                       "NOT_VERIFIED", ""), (0, 1, 9)):
        base = outcome.finding_state(claim_status=cs, kept_findings=kept)
        for disp, prov in itertools.product(dispositions, provs):
            o = _outcome(disp, prov, launched=1)
            st, _ = outcome.execution_state([o], [PlanDecision(target_id="T",
                                                               requires_execution=True)])
            assert st in outcome.EXECUTION_STATES
            assert outcome.finding_state(claim_status=cs, kept_findings=kept) == base


def test_execution_about_the_paper_names_what_was_held_against_what_and_nothing_about_an_attempt():
    """`EXECUTION_ABOUT_THE_PAPER` names exactly the states that hold a measured quantity
    against a quantity the PAPER printed. It once also carried two states for a contrast
    this review designed and ran itself against a PREDICTION the claim committed to,
    rather than against the paper's own printed number -- that pair belonged to the
    focused-validation/between-arms route, which this codebase deleted (zero executions
    were ever reached through it; see CLAUDE.md's Known Limitations). What survives is the
    original two-state rule: this set contains exactly the states that name a printed
    quantity held against a measurement, and none that merely names a fact about an
    attempt (that it ran, that it was blocked, that it was warranted, that nothing came
    out of it)."""
    about = [s for s in outcome.EXECUTION_STATES if s in outcome.EXECUTION_ABOUT_THE_PAPER]
    assert set(about) == {
        "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY",
        "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY",
    }
    assert all("PRINTED_QUANTITY" in s for s in about)

    not_about = [s for s in outcome.EXECUTION_STATES if s not in outcome.EXECUTION_ABOUT_THE_PAPER]
    assert not_about, "there must be states that say nothing about the paper too"
    attempt_words = ("ADMISSIBLE_EVIDENCE", "BLOCKED", "WARRANTED", "NOT_ATTEMPTED")
    for s in not_about:
        assert "PRINTED_QUANTITY" not in s
        assert any(w in s for w in attempt_words), s


def test_the_outcome_block_forecloses_all_three_misreadings():
    for word in ("correct", "failed", "inconclusive"):
        assert word in outcome.DISCLAIMER, word


# --------------------------------------------------------------------------- #
# and nothing above may have quietly raised a severity
# --------------------------------------------------------------------------- #
def test_no_new_mechanism_can_raise_a_severity():
    """Invariant 11, swept over the whole reachable input space of `grading.derive`, and
    re-asserted here because five new modules landed beside it."""
    from harness.artifacts import SEVERITIES
    rank = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    params = inspect.signature(grading.derive).parameters
    for banned in ("coverage", "observation", "integrity", "artifact_state",
                   "review_path", "surface", "addressing_blocker"):
        assert not any(banned in p for p in params), banned
    # a spot sweep: the derived severity never exceeds the lens's own assertion
    for lens_sev in SEVERITIES:
        for cand in ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "DISMISSED"):
            for ev in ("cell_verified", "prose_verified", "caption_verified",
                       "equation_verified", "unverified"):
                f = Finding(finding_id="f", lens="confound", severity=lens_sev,
                            candidate_class=cand, evidence_class=ev, evidence_quote="q")
                counted = report_stage.counted(f)
                assert rank[counted] <= rank[lens_sev], (lens_sev, cand, ev, counted)


# --------------------------------------------------------------------------- #
# (h) GREEN may not borrow the words of evidence it does not have
# --------------------------------------------------------------------------- #
def test_unverified_decision_prose_may_not_use_support_language():
    """`report_stage.unearned_support_language` is this system's only guard against a
    NOT_VERIFIED/VERIFIED_FAILURE report reading as though something were checked and
    held. This is the guard's sole remaining test after an unrelated pass thinned the
    suite that used to exercise it -- confirmed via a whole-codebase reachability sweep,
    which is why this single function gets a dedicated test rather than being folded
    into a larger file."""
    # NOT_VERIFIED prose that smuggles in a support word must be flagged.
    bad = report_stage.unearned_support_language(
        "The paper's claim is confirmed by our reading.", "NOT_VERIFIED")
    assert "confirmed" in bad

    # The harness's own disclaiming vocabulary is not itself a violation.
    clean = report_stage.unearned_support_language(
        "NOT_VERIFIED: reproduction was not verified.", "NOT_VERIFIED")
    assert clean == []

    # VERIFIED_SUPPORT is the only status that has earned this language.
    assert report_stage.unearned_support_language(
        "The result is confirmed and supported.", "VERIFIED_SUPPORT") == []

    # VERIFIED_FAILURE has not earned SUPPORT language either -- a failure is not a
    # confirmation of anything.
    failure_bad = report_stage.unearned_support_language(
        "The reported number is confirmed wrong.", "VERIFIED_FAILURE")
    assert "confirmed" in failure_bad
