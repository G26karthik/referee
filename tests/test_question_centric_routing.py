"""The question is the unit of investigation, and the comparison is not always a cell.

**What this replaces.** Routing keyed on `has_value` — had the extractor parsed a number
at this address? A target with one could reach an executable route and a target without
one could reach nothing, whatever question it raised. That is the right requirement for
"is 61.4 the number their code produces" and it is the wrong requirement, or no
requirement at all, for the questions a referee actually asks:

  * ATTRIBUTION           one arm against another; the paper printed neither
  * CONTROL_PRESENCE      is the comparison the claim needs there, or is it not
  * PROTOCOL_CONFORMANCE  the observed procedure against the specified one

All three were reported as "no verification route this system has would settle the
question", which is a statement about `local_exec.reconcile` dressed as a statement about
this review's method inventory. These tests pin the three apart: the QUESTION decides which
routes it can reach, the ROUTE decides what its result is compared against, and the
COMPARISON decides whether a process may start at all.

The last of those is the same rule `admissible_if_it_succeeds` applies to provenance,
asked one step earlier. Widening what can be routed must not widen what is executed while
this system can only perform one of the four comparisons, and the tests below assert that
it does not.
"""
from __future__ import annotations

import itertools

import pytest

from harness import comparison, discovery, planner, questions
from harness.artifacts import (COMPARISON_KINDS, COMPARISON_STATES, QUESTION_KINDS,
                               VERIFICATION_ROUTES, DiscoveredObject, Finding, PaperDoc,
                               PlanDecision, ProbeSpec, QuantFinding, Section, Table)
from harness.stages import discover as discover_stage
from harness.stages import probe as probe_stage


def _routes(question_kind: str, *, repo: bool = True, value: bool = False,
            kind: str = "SCIENTIFIC_CLAIM") -> list[str]:
    return discovery._routes(kind, None, repo_available=repo, has_value=value,
                             arithmetic_broken=False, lens="", question_kind=question_kind)


# --------------------------------------------------------------------------- #
# The vocabulary
# --------------------------------------------------------------------------- #
def test_the_seven_kinds_the_architecture_names_are_all_present():
    for k in ("PRINTED_QUANTITY", "COMPOSITION", "ATTRIBUTION", "CONTROL_PRESENCE",
              "PROTOCOL_CONFORMANCE", "SPECIFICATION", "PRIOR_ART"):
        assert k in QUESTION_KINDS, k


def test_the_eighth_is_the_old_behaviour_and_never_a_new_capability():
    """UNCLASSIFIED is what a finding that classified itself as nothing gets. Its routes
    must be exactly what `has_value` gave every target before this vocabulary existed, so
    the default of the vocabulary can never be a widening nobody asked for."""
    for repo, value in itertools.product((True, False), (True, False)):
        expected = ((["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"] if repo
                     else ["INDEPENDENT_RECONSTRUCTION"]) if value else ["NONE"])
        assert _routes("UNCLASSIFIED", repo=repo, value=value) == expected, (repo, value)


def test_every_kind_says_which_routes_it_can_reach():
    assert set(discovery.ROUTES_FOR_QUESTION) == set(QUESTION_KINDS)
    for qk, rs in discovery.ROUTES_FOR_QUESTION.items():
        assert set(rs) <= set(VERIFICATION_ROUTES), qk


# --------------------------------------------------------------------------- #
# The kind is derived from closed vocabulary and from nothing else
# --------------------------------------------------------------------------- #
def test_the_kind_is_derived_most_specific_first():
    assert questions.kind_for_finding(
        Finding(lens="confound", discrepancy_type="ARITHMETIC_ERROR")) == "COMPOSITION"
    assert questions.kind_for_finding(
        Finding(lens="confound", baseline_class="USEFUL_CONTROL")) == "CONTROL_PRESENCE"
    assert questions.kind_for_finding(Finding(lens="confound")) == "ATTRIBUTION"


def test_the_derivation_reads_no_number_no_name_and_no_paper():
    """Same discipline as `grading.derive` and `planner.classify`: the signature is what
    makes 'if <paper> appears, route it differently' inexpressible rather than absent."""
    import inspect

    sig = inspect.signature(discovery._routes)
    for name in ("repo_available", "has_value", "arithmetic_broken", "lens",
                 "question_kind"):
        assert name in sig.parameters, name
    for name, p in sig.parameters.items():
        if name in ("kind", "ref"):
            continue
        assert str(p.annotation) in ("bool", "str"), (name, p.annotation)


@pytest.mark.parametrize("lens,expected", [
    ("confound", "ATTRIBUTION"), ("protocol", "PROTOCOL_CONFORMANCE"),
    ("overclaim", "PRINTED_QUANTITY"), ("contradiction", "PRINTED_QUANTITY"),
    ("", "UNCLASSIFIED"),
])
def test_every_lens_maps_to_a_kind(lens, expected):
    assert questions.kind_for_finding(Finding(lens=lens)) == expected


def test_a_derived_question_carries_its_kind():
    qs = questions.derive([Finding(finding_id="c-01", lens="confound",
                                   candidate_class="CONFIRMED_FINDING")])
    assert qs[0].kind == "ATTRIBUTION"


# --------------------------------------------------------------------------- #
# Unimplemented focused validation is not advertised as an applicable route
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question_kind",
                         ["ATTRIBUTION", "CONTROL_PRESENCE", "PROTOCOL_CONFORMANCE"])
def test_a_question_with_no_printed_quantity_does_not_advertise_focused_validation(question_kind):
    got = _routes(question_kind, repo=True, value=False)
    assert got == ["ARTIFACT_INSPECTION"]
    assert "FOCUSED_VALIDATION_EXPERIMENT" not in got


@pytest.mark.parametrize("question_kind",
                         ["ATTRIBUTION", "CONTROL_PRESENCE", "PROTOCOL_CONFORMANCE"])
def test_planner_does_not_warrant_an_unimplemented_focused_experiment(question_kind):
    obj = DiscoveredObject(target_id="T1", centrality="CENTRAL", harness_addressable=True,
                           question_kind=question_kind,
                           routes=_routes(question_kind, repo=True, value=False))
    d = planner.plan(obj, artifact_available=True, specification_complete=True,
                     environment_state="ok")
    assert not d.requires_execution
    assert d.route == "ARTIFACT_INSPECTION"


@pytest.mark.parametrize("question_kind",
                         ["ATTRIBUTION", "CONTROL_PRESENCE", "PROTOCOL_CONFORMANCE"])
def test_with_no_artifact_it_falls_back_to_reconstruction_only_where_a_value_exists(
        question_kind):
    """No repository means nothing to inspect and no arm to vary. The governed
    reconstruction route is what remains, and it compares against a printed value, so it
    is offered only where the paper printed one."""
    assert _routes(question_kind, repo=False, value=True) == ["INDEPENDENT_RECONSTRUCTION"]
    assert _routes(question_kind, repo=False, value=False) == ["NONE"]


def test_a_question_about_a_printed_quantity_still_needs_the_quantity():
    """The requirement `has_value` expressed is narrowed, not removed. A run reconciled
    against a cell with no cell to reconcile against produces a number in a vacuum."""
    for qk in ("PRINTED_QUANTITY", "COMPOSITION", "UNCLASSIFIED"):
        assert _routes(qk, repo=True, value=False) == ["NONE"], qk
        assert _routes(qk, repo=True, value=True) != ["NONE"], qk


def test_a_specification_question_is_read_and_never_run():
    """Producing the same number again cannot establish that it was the right quantity to
    produce: a proxy measured perfectly is still a proxy."""
    assert _routes("SPECIFICATION", repo=True, value=True) == ["ARTIFACT_INSPECTION"]
    obj = DiscoveredObject(target_id="T1", centrality="CENTRAL", harness_addressable=True,
                           question_kind="SPECIFICATION", routes=["ARTIFACT_INSPECTION"])
    assert not planner.plan(obj, artifact_available=True).requires_execution


def test_prior_art_reaches_the_route_that_is_architectural_only():
    """`VERIFICATION_ROUTES` carries LITERATURE_SEARCH and nothing implements it. The
    question reaching it rather than 'no route applies' is the honest statement: a route
    exists in the design and this system has not built it."""
    assert _routes("PRIOR_ART", repo=True, value=True) == ["LITERATURE_SEARCH"]
    obj = DiscoveredObject(target_id="T1", centrality="CENTRAL", harness_addressable=True,
                           question_kind="PRIOR_ART", routes=["LITERATURE_SEARCH"])
    assert not planner.plan(obj, artifact_available=True).requires_execution


# --------------------------------------------------------------------------- #
# The comparison is a property of the ROUTE
# --------------------------------------------------------------------------- #
def test_every_route_says_what_its_result_is_compared_against():
    assert set(comparison.COMPARISON_FOR_ROUTE) == set(VERIFICATION_ROUTES)
    assert set(comparison.COMPARISON_FOR_ROUTE.values()) <= set(COMPARISON_KINDS) | {""}


def test_an_attribution_experiment_is_not_compared_against_a_printed_cell():
    """The assumption this layer removes. A focused validation experiment varies one thing
    and holds the two runs against each other; reconciling either against the paper's cell
    answers a question nobody asked."""
    assert comparison.kind_for_route("FOCUSED_VALIDATION_EXPERIMENT") == "BETWEEN_ARMS"
    assert comparison.kind_for_route("AUTHOR_CODE_EXECUTION") == "AGAINST_PRINTED_VALUE"


def test_only_one_comparison_has_arithmetic_behind_it_and_it_says_so():
    assert comparison.RECONCILABLE == ("AGAINST_PRINTED_VALUE",)
    for k in set(COMPARISON_KINDS) - {"AGAINST_PRINTED_VALUE"}:
        assert not comparison.reconcilable(k), k


@pytest.mark.parametrize("route,printed,arms,state", [
    ("AUTHOR_CODE_EXECUTION", True, 1, "established"),
    ("AUTHOR_CODE_EXECUTION", False, 1, "no_reference"),
    ("FOCUSED_VALIDATION_EXPERIMENT", True, 1, "arms_unspecified"),
    ("FOCUSED_VALIDATION_EXPERIMENT", True, 2, "unsupported"),
    ("ARTIFACT_INSPECTION", True, 2, "unsupported"),
    ("NONE", True, 2, "unmapped"),
])
def test_the_comparison_states_are_four_different_facts(route, printed, arms, state):
    c = comparison.derive(route, printed_value_available=printed, arms_specified=arms)
    assert c.state == state
    assert c.state in COMPARISON_STATES


# --------------------------------------------------------------------------- #
# Widening what is ROUTED must not widen what is EXECUTED
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("arms,state", [(["reproduction"], "arms_unspecified"),
                                        (["baseline", "treatment"], "unsupported")])
def test_a_run_whose_result_could_not_be_compared_is_never_started(arms, state):
    """The same rule `admissible_if_it_succeeds` applies to provenance, one question
    earlier. A between-arms comparison means the process is not started, however
    impeccable the program that would have run — and the two ways it can fail are two
    different facts: the second arm could not be BUILT, or it could and this review has no
    arithmetic that reads the result."""
    spec = probe_stage.establish_comparison(
        ProbeSpec(paper_id="p", provenance="repo_exec", claimed_cell_value="61.4",
                  arms=arms),
        "FOCUSED_VALIDATION_EXPERIMENT")
    assert spec.comparison is not None and spec.comparison.state == state
    ok, why = probe_stage.may_be_compared(spec)
    assert not ok
    assert "nothing to be held against" in why
    assert spec.comparison.reason and spec.comparison.reason in why, (
        "the refusal must carry WHY, not an empty clause")


def test_no_comparison_is_reported_established_and_then_refused():
    """The defect this split closed. A BETWEEN_ARMS comparison with two arms read
    `established`, `admits_verdict` refused it anyway, and the refusal interpolated an
    empty `reason` — six corpus targets carried a sentence with a hole in it."""
    for route in comparison.COMPARISON_FOR_ROUTE:
        for arms in (0, 1, 2):
            c = comparison.derive(route, printed_value_available=True,
                                  arms_specified=arms)
            assert c.established == comparison.admits_verdict(c), (route, arms)
            assert c.established or c.reason, (route, arms)


def test_and_it_is_reported_as_a_comparison_block_not_an_identity_one():
    """Two different facts. IDENTITY_BLOCKED says the program that would have run was not
    bound to what was printed — a statement about the artifact. COMPARISON_BLOCKED says it
    was bound and this review could not compare what it produced, which is a statement
    about this review."""
    obj = DiscoveredObject(target_id="T1")
    plan = PlanDecision(target_id="T1", action="FOCUSED_VALIDATION_EXPERIMENT",
                        route="FOCUSED_VALIDATION_EXPERIMENT", requires_execution=True)
    out = probe_stage._not_started(obj, plan, "why", "COMPARISON_BLOCKED")
    assert out.disposition == "COMPARISON_BLOCKED"
    assert out.launched == 0 and not out.establishes_failure
    assert not out.concerns_the_paper, (
        "a comparison this harness could not perform says nothing about the paper")
    assert out.evidence_state == "COMPARISON_LIMITATION"


def test_an_established_reconcilable_comparison_still_starts():
    spec = probe_stage.establish_comparison(
        ProbeSpec(paper_id="p", provenance="repo_exec", claimed_cell_value="61.4"),
        "AUTHOR_CODE_EXECUTION")
    assert probe_stage.may_be_compared(spec) == (True, "")


def test_a_spec_with_no_comparison_recorded_behaves_exactly_as_it_did():
    """Hand-written `spec.json` files and every fixture predate this layer. The gates
    already in front of them are what must decide, unchanged."""
    assert probe_stage.may_be_compared(ProbeSpec(paper_id="p")) == (True, "")
    assert comparison.admits_verdict(None) is True


def test_the_reconciler_refuses_a_comparison_it_cannot_perform():
    """A backstop for a spec that reached the reconciler anyway. It reports the comparison
    rather than silently performing the AGAINST_PRINTED_VALUE one on a result that is not
    about a printed value."""
    from harness import local_exec

    spec = probe_stage.establish_comparison(
        ProbeSpec(paper_id="p", provenance="repo_exec", claimed_cell_value="61.4",
                  claim_ref="T1:r0:c1", claim_kind="table_cell", arms=["a"]),
        "FOCUSED_VALIDATION_EXPERIMENT")
    rec = local_exec.reconcile(spec, [60.0, 60.1], 0.5, [0, 1])
    assert rec.status == "INCONCLUSIVE"
    assert rec.failure_class == "comparison_unestablished"
    assert rec.comparison_kind == "BETWEEN_ARMS"
    assert rec.comparison_state == "arms_unspecified"


def test_the_provenance_ceiling_is_applied_before_the_comparison_and_identically():
    """A synthesized probe reports the CEILING, whichever comparison was intended. Which
    comparison was meant cannot rescue a program that was never entitled to reconcile
    anything, and the ceiling is the more fundamental refusal."""
    from harness import local_exec

    for route in ("AUTHOR_CODE_EXECUTION", "FOCUSED_VALIDATION_EXPERIMENT",
                  "ARTIFACT_INSPECTION"):
        spec = probe_stage.establish_comparison(
            ProbeSpec(paper_id="p", provenance="synthesized", claimed_cell_value="61.4",
                      claim_ref="T1:r0:c1", claim_kind="table_cell"),
            route)
        rec = local_exec.reconcile(spec, [60.0], 0.5, [0])
        assert rec.status == "INCONCLUSIVE", route
        assert "not the paper's own code" in rec.reason, route
        assert rec.failure_class != "comparison_unestablished", route


def test_the_reconciler_records_which_comparison_it_performed():
    from harness import local_exec

    spec = probe_stage.establish_comparison(
        ProbeSpec(paper_id="p", provenance="repo_exec", claimed_cell_value="61.4",
                  claim_ref="T1:r0:c1", claim_kind="table_cell"),
        "AUTHOR_CODE_EXECUTION")
    rec = local_exec.reconcile(spec, [60.0], 0.5, [0])
    assert rec.comparison_kind == "AGAINST_PRINTED_VALUE"
    assert rec.comparison_state == "established"


# --------------------------------------------------------------------------- #
# Every executable target carries a question_id
# --------------------------------------------------------------------------- #
def _paper_doc() -> PaperDoc:
    return PaperDoc(
        paper_id="qc", repo_url="https://example.invalid/r",
        sections=[Section(section_idx=0, title="Abstract",
                          text="We build 58 topics x 5 templates x 10 instances = 2,900 "
                               "test cases. Accuracy improves markedly."),
                  Section(section_idx=1, title="Results", text="We report gains.")],
        tables=[Table(table_idx=1, rows=[["ours", "61.4"]])],
        reported_numbers=[QuantFinding(value="61.4", metric="accuracy",
                                       source_quote="61.4", table_ref="T1:r0:c0")])


def test_an_extractor_found_target_gets_a_question_when_it_will_execute():
    """The two object sources that actually reach execution on this corpus come from the
    extractor, not from a lens, so they carried no question at all — and the funnel could
    report a launch with no question it was answering."""
    doc = _paper_doc()
    objs, _ = discovery.discover(doc, [])
    plans = [planner.plan(o, artifact_available=True, specification_complete=True,
                          environment_state="ok") for o in objs]
    assert any(p.requires_execution for p in plans), "the fixture must reach execution"
    assert not any(o.question_id for o in objs), "and start with no questions"

    qs = discover_stage._question_for_every_executable_target(objs, plans, [])
    for obj, plan in zip(objs, plans):
        if plan.requires_execution:
            assert obj.question_id, obj.target_id
    assert qs, "and the questions themselves are recorded"
    for q in qs:
        assert q.kind in QUESTION_KINDS
        assert q.from_finding == "" and q.source_finding_ids == [], (
            "a question the harness asked must not claim a lens raised it")


def test_a_target_that_will_not_run_is_not_given_a_question_to_pad_the_report():
    """Bounded by construction, as invariant 19 requires of everything the review prints.
    A question about every printed number in a paper is not a referee's open question."""
    doc = _paper_doc()
    objs, _ = discovery.discover(doc, [])
    plans = [PlanDecision(target_id=o.target_id, requires_execution=False) for o in objs]
    assert discover_stage._question_for_every_executable_target(objs, plans, []) == []
    assert not any(o.question_id for o in objs)


def test_the_object_and_its_question_never_disagree_about_what_is_being_asked():
    f = Finding(finding_id="c-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                evidence_quote="Accuracy improves markedly.")
    doc = _paper_doc()
    qs = questions.derive([f])
    objs, _ = discovery.discover(doc, [f], qs)
    by_id = {q.question_id: q for q in qs}
    for obj in objs:
        if obj.question_id in by_id:
            assert obj.question_kind == by_id[obj.question_id].kind, obj.target_id


# --------------------------------------------------------------------------- #
# INVARIANT 23, second clause: a SUPPORTING target does not earn an execution
# while a CENTRAL one is being pursued.
#
# The first clause ("a route that settles with nothing running is taken first") was
# already pinned. This clause was not: the demotion existed, the counter existed, and
# nothing asserted either, so a change that spent an execution on a peripheral target
# while a central one waited would have passed the suite.
# --------------------------------------------------------------------------- #
def _obj(target_id: str, centrality: str) -> DiscoveredObject:
    return DiscoveredObject(target_id=target_id, centrality=centrality,
                            harness_addressable=True, materiality_basis="NONE")


def _executable_plan(target_id: str) -> PlanDecision:
    return PlanDecision(target_id=target_id, action="AUTHOR_CODE_REPRODUCTION",
                        route="AUTHOR_CODE_EXECUTION", reason="fixture",
                        requires_execution=True)


def test_a_supporting_target_is_demoted_while_a_central_one_is_pursued():
    objects = [_obj("CEN", "CENTRAL"), _obj("SUP", "SUPPORTING"), _obj("PER", "PERIPHERAL")]
    plans = [_executable_plan(o.target_id) for o in objects]

    out = discover_stage._demote_when_a_central_target_is_being_pursued(objects, plans)

    by_id = {p.target_id: p for p in out}
    assert by_id["CEN"].requires_execution is True, "the central target still runs"
    for tid in ("SUP", "PER"):
        assert by_id[tid].requires_execution is False, tid
        # A NAMED refusal, never silence: the gate is what the funnel and the review print.
        assert by_id[tid].blocking_gate == "outranked_by_a_central_target", tid
        assert by_id[tid].gates.get("outranked_by_a_central_target") is True, tid
        # And it is not recorded as "no experiment was needed here" without saying why.
        assert "CENTRAL target" in by_id[tid].reason, tid


def test_supporting_targets_stand_when_no_central_target_is_being_pursued():
    """The other half of the rule, and the reason it is a SET-level fact.

    A paper whose central claims are all unaddressable must not silently become a paper
    on which nothing at all is checked."""
    objects = [_obj("SUP", "SUPPORTING"), _obj("PER", "PERIPHERAL")]
    plans = [_executable_plan(o.target_id) for o in objects]

    out = discover_stage._demote_when_a_central_target_is_being_pursued(objects, plans)

    assert [p.requires_execution for p in out] == [True, True]
    assert all(p.blocking_gate != "outranked_by_a_central_target" for p in out)


def test_a_central_target_that_does_not_run_does_not_outrank_anything():
    """The trigger is a central target being PURSUED, not one merely existing. A central
    target the planner refused cannot suppress the supporting work that is left."""
    objects = [_obj("CEN", "CENTRAL"), _obj("SUP", "SUPPORTING")]
    plans = [PlanDecision(target_id="CEN", action="INFEASIBLE_SPECIFICATION",
                          route="AUTHOR_CODE_EXECUTION", reason="fixture",
                          requires_execution=False),
             _executable_plan("SUP")]

    out = discover_stage._demote_when_a_central_target_is_being_pursued(objects, plans)

    assert {p.target_id: p.requires_execution for p in out} == {"CEN": False, "SUP": True}
