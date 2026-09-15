"""The first-review layer: addressable claims, questions, targets, plans, triage.

Before this layer, the pipeline's whole reproduction universe was one line:

    candidates = [f for f in rank(findings) if f.verifiable_by_experiment]
    target = candidates[0] if candidates else None

One target, selected by an unchecked model boolean, ordered by a *report display* sort,
with no fallback when it blocked and no record of what else existed. These tests pin the
properties that replace it, and — more importantly — the properties that must NOT change
because of it: nothing here may raise a severity, promote a verdict, or turn an inability
to check into a statement about a paper.
"""
from __future__ import annotations

import inspect

import pytest

from harness import claims, discovery, ledger, planner, priority, questions
from harness.artifacts import (ClaimRef, DiscoveredObject, EvalReport, Finding, PaperDoc,
                               PlanDecision, QuantFinding, Reconciliation, ReviewQuestion, Section,
                               Table,
                               TargetOutcome, TargetSet)
from harness.local_exec import parse_metric
from harness.stages.report import (render_reviewer_report, triage, unchecked_central,
                                   unresolved_central)
from conftest import incidental_objects, material_objects

# The shape the old pipeline could not reach: a result stated in a sentence, whose
# composition the paper spells out and whose total an execution could re-derive.
COMPOSITION = "58 topics x 5 templates x 10 instances = 2,900 test cases"


def _doc(*, repo: str = "", extra_sections: list[Section] | None = None) -> PaperDoc:
    return PaperDoc(
        paper_id="p", title="A Paper", repo_url=repo,
        sections=[Section(section_idx=0, title="Abstract", page_start=1,
                          text=f"We build a benchmark. {COMPOSITION}. "
                               f"Accuracy improves over the baseline."),
                  Section(section_idx=1, title="Setup", page_start=2,
                          text="We train with the Adam optimizer for 30 epochs at a learning "
                               "rate of 1e-3 on the CIFAR-100 dataset and report accuracy on "
                               "the test set. The objective is a weighted sum of two terms."),
                  *(extra_sections or [])],
        tables=[Table(table_idx=1, page=4, caption="Table 1: accuracy",
                      header=["method", "accuracy"],
                      rows=[["ours", "61.4"], ["baseline", "59.3"]])],
        reported_numbers=[QuantFinding(value="61.4", metric="accuracy", method="ours",
                                       source_quote="61.4", page=4, table_ref="T1:r0:c1")])


# --------------------------------------------------------------------------- #
# §5 — a printed result no longer has to be a table cell
# --------------------------------------------------------------------------- #
def test_a_prose_result_becomes_a_legitimate_target():
    """The FinChain shape, reachable without an operator writing spec.json by hand."""
    doc = _doc(repo="https://example.invalid/r")
    objs, _ = discovery.discover(doc, [])
    prose = [o for o in objs if o.kind == "REPRODUCTION_TARGET"]
    assert len(prose) == 1
    t = prose[0]
    assert t.harness_addressable and t.expected_value == 2900.0
    assert t.ref is not None and t.ref.kind == "prose_claim" and t.ref.resolved
    assert t.ref.quantity.expression == "58*5*10"
    assert t.ref.quantity.arithmetic_ok is True
    assert "AUTHOR_CODE_EXECUTION" in t.routes


def test_a_malformed_prose_reference_cannot_become_a_target():
    doc = _doc()
    for bad in ("P9:0-10", "P0:100000-100010", "P0:40-10", "PX:1-2", "", "p7"):
        assert not claims.resolve(doc, bad).resolved, bad


def test_unverified_prose_cannot_be_promoted_to_verified_evidence():
    """A lens supplies a QUOTE; the harness mints the ADDRESS. A lens that writes an
    address of its own gains nothing, because the span is re-read off the document."""
    doc = _doc()
    minted = claims.mint(doc, COMPOSITION)
    assert minted.resolved
    assert claims.resolve(doc, minted.ref, "a sentence the paper does not contain") \
        .resolution == "span_mismatch"
    assert claims.mint(doc, "a sentence the paper does not contain").resolution == "not_found"


def test_an_ambiguous_quote_addresses_nothing_rather_than_its_first_occurrence():
    doc = _doc(extra_sections=[Section(section_idx=2, title="Repeat",
                                       text="Accuracy improves over the baseline.")])
    assert claims.mint(doc, "Accuracy improves over the baseline.").resolution == "ambiguous"


def test_prose_and_table_references_obey_the_same_provenance_ceiling():
    """A prose target is not a cheaper way to convict. The ceiling is in `reconcile` and
    is keyed on PROVENANCE, which knows nothing about what kind of address was cited."""
    from harness.artifacts import ProbeSpec
    from harness.local_exec import reconcile
    for ref, kind in (("T1:r0:c1", "table_cell"), ("P0:20-60", "prose_claim")):
        spec = ProbeSpec(paper_id="p", provenance="synthesized", claim_ref=ref,
                         claim_kind=kind, claimed_cell_value="2900", script="x")
        rec = reconcile(spec, [10.0, 11.0], 2.0, [0, 1])
        assert rec.status == "INCONCLUSIVE", f"{kind} escaped the ceiling"
        assert "not the paper's own code" in rec.reason


def test_extraction_failure_is_explicit_rather_than_silent():
    """An empty target set caused by failed extraction must not read like a paper with
    nothing to check. The denominator is recorded either way."""
    blank = PaperDoc(paper_id="p", sections=[Section(section_idx=0, text="Prose only.")])
    objs, cov = discovery.discover(blank, [])
    assert objs == []
    assert cov["tables_extracted"] == 0 and cov["table_cells"] == 0
    assert cov["reported_numbers"] == 0 and cov["objects_addressable"] == 0
    assert cov["sections"] == 1


def test_a_span_reporting_two_unrelated_numbers_reports_no_quantity():
    assert claims.parse_quantity("we ran 3 seeds on 4 datasets") is None
    assert claims.parse_quantity("3 seeds and 4 datasets = 12").expression == ""
    assert claims.parse_quantity("58 x 5 x 10 = 2901").arithmetic_ok is False
    assert claims.parse_quantity("we replace it with a U-Net [25]") is None


# --------------------------------------------------------------------------- #
# §6 — discovery, and the demotion of an unchecked lens boolean
# --------------------------------------------------------------------------- #
def test_multiple_targets_are_discovered_from_one_paper():
    doc = _doc(repo="https://example.invalid/r")
    fs = [Finding(finding_id="c-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                  evidence_ref="T1:r0:c1", evidence_quote="61.4", claim="the gain is real"),
          Finding(finding_id="p-01", lens="protocol", candidate_class="PLAUSIBLE_CONCERN",
                  baseline_class="CENTRAL_VALIDITY_THREAT",
                  evidence_ref="T1:r1:c1", evidence_quote="59.3", claim="weak baseline")]
    objs, cov = discovery.discover(doc, fs, questions.derive(fs))
    kinds = {o.kind for o in objs}
    assert {"EXPERIMENTAL_RESULT", "REPRODUCTION_TARGET", "IMPLEMENTATION_CLAIM",
            "SCIENTIFIC_CLAIM", "BASELINE_COMPARISON"} <= kinds
    assert cov["objects_discovered"] >= 5
    assert len({o.target_id for o in objs}) == len(objs), "target ids are unique"


def test_the_lens_boolean_is_metadata_and_the_harness_decides_addressability():
    doc = _doc(repo="https://example.invalid/r")
    said_no = Finding(finding_id="a", lens="overclaim", evidence_ref="T1:r0:c1",
                      evidence_quote="61.4", verifiable_by_experiment=False)
    said_yes = Finding(finding_id="b", lens="overclaim", evidence_ref="p9",
                       evidence_quote="a sentence not in this paper",
                       verifiable_by_experiment=True)
    objs, _ = discovery.discover(doc, [said_no, said_yes])
    grounded = next(o for o in objs if o.ref and o.ref.ref == "T1:r0:c1")
    assert grounded.proposed_by_lens is False and grounded.harness_addressable is True, (
        "the lens said this could not be settled by experiment; the harness disagrees on "
        "structural grounds and its answer is the one that counts")
    ghost = next(o for o in objs if o.proposed_by_lens is True)
    assert ghost.harness_addressable is False, "a lens saying yes does not make it so"


def test_discovery_can_say_a_question_is_relevant_with_no_route():
    doc = _doc()                                    # no repository
    f = Finding(finding_id="q", lens="protocol", candidate_class="OPEN_QUESTION",
                evidence_ref="T1:r0:c1", evidence_quote="61.4",
                claim="is the protocol appropriate")
    objs, _ = discovery.discover(doc, [f])
    q = next(o for o in objs if o.kind == "UNANSWERED_REVIEW_QUESTION")
    assert q.routes and q.routes != ["NONE"], "a printed number can still be reconstructed"
    lonely = DiscoveredObject(target_id="X", centrality="CENTRAL", routes=["NONE"],
                              harness_addressable=False)
    d = planner.plan(lonely)
    # An UNADDRESSABLE target and an UNDERSPECIFIED experiment are different refusals and
    # no longer share a label: this one is a limit of what we could address, the one in
    # `test_an_underspecified_experiment_is_refused_rather_than_invented` is a limit of
    # what the paper specified. A reviewer needs those apart.
    assert d.action == "INFEASIBLE_ADDRESSING" and not d.requires_execution
    assert d.blocking_gate == "structurally_addressable"
    assert d.necessity == "EXPERIMENT_NOT_EXECUTABLE"


# --------------------------------------------------------------------------- #
# §7 — findings become questions
# --------------------------------------------------------------------------- #
def test_a_methodological_concern_becomes_an_explicit_question():
    f = Finding(finding_id="c-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                statement="the paper changes both the augmentation policy and the loss weight",
                recommended_resolution="run with one modification at a time")
    q = questions.derive([f])[0]
    assert q.question.endswith("?") and "attributable" in q.question
    assert q.what_would_settle_it == "run with one modification at a time"
    assert q.materiality == "CENTRAL" and q.route == "NONE"


def test_question_generation_reads_no_number_no_metric_and_no_paper_identity():
    """The same discipline `harness.grading.derive` is held to: a paper-specific or
    threshold-shaped question rule must be inexpressible, not merely absent."""
    src = inspect.getsource(questions)
    for table in (questions._BY_DISCREPANCY, questions._BY_BASELINE, questions._BY_LENS):
        for text in (t for pair in table.values() for t in pair):
            assert "{" not in text and "%" not in text
    assert "paper_id" not in src and "counted_severity" not in src


# --------------------------------------------------------------------------- #
# §8 — the experiment-trigger gate
# --------------------------------------------------------------------------- #
def test_a_model_suggestion_is_not_an_authorization():
    """`classify`'s signature admits vocabulary and booleans only, so nothing a model
    wrote can reach it except through a harness-derived enum."""
    for name, p in inspect.signature(planner.classify).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"


def test_an_unnecessary_experiment_is_suppressed():
    cheap = DiscoveredObject(target_id="T", centrality="CENTRAL", harness_addressable=True,
                             routes=["ARITHMETIC_RECHECK", "AUTHOR_CODE_EXECUTION"])
    d = planner.plan(cheap, artifact_available=True, specification_complete=True)
    assert d.action == "PAPER_ONLY_RESOLUTION" and not d.requires_execution


def test_a_peripheral_target_does_not_earn_an_execution():
    obj = DiscoveredObject(target_id="T", centrality="PERIPHERAL", harness_addressable=True,
                           routes=["AUTHOR_CODE_EXECUTION"])
    assert planner.plan(obj, artifact_available=True).action == "NO_EXPERIMENT_NEEDED"


@pytest.mark.parametrize("kw,expected", [
    ({"artifact_available": False}, "INFEASIBLE_ARTIFACT"),
    ({"artifact_available": True, "environment_state": "blocked"}, "INFEASIBLE_ENVIRONMENT"),
])
def test_each_blocker_is_named_and_none_of_them_is_about_the_paper(kw, expected):
    obj = DiscoveredObject(target_id="T", centrality="CENTRAL", harness_addressable=True,
                           routes=["AUTHOR_CODE_EXECUTION"])
    d = planner.plan(obj, **kw)
    assert d.action == expected and not d.requires_execution
    assert ("Nothing about the paper follows" in d.reason
            or "never a failed reproduction" in d.reason)


def test_an_underspecified_experiment_is_refused_rather_than_invented():
    obj = DiscoveredObject(target_id="T", centrality="CENTRAL", harness_addressable=True,
                           routes=["FOCUSED_VALIDATION_EXPERIMENT"])
    d = planner.plan(obj, artifact_available=True, specification_complete=False)
    assert d.action == "INFEASIBLE_SPECIFICATION"
    assert d.necessity == "EXPERIMENT_UNDERSPECIFIED"
    assert "inventing the missing half" in d.reason


# --------------------------------------------------------------------------- #
# §10 — prioritization is its own function
# --------------------------------------------------------------------------- #
def test_a_cheap_peripheral_target_never_outranks_an_expensive_central_one():
    hard, _ = priority.score(centrality="CENTRAL", addressable=True,
                             cheapest_route="AUTHOR_CODE_EXECUTION")
    easy, _ = priority.score(centrality="PERIPHERAL", addressable=True,
                             cheapest_route="ARITHMETIC_RECHECK")
    assert hard > easy


def test_cost_only_decides_between_scientifically_equivalent_targets():
    a, _ = priority.score(centrality="CENTRAL", addressable=True,
                          cheapest_route="ARITHMETIC_RECHECK")
    b, _ = priority.score(centrality="CENTRAL", addressable=True,
                          cheapest_route="PAPER_INTERNAL_CHECK")
    assert a == b


def test_prioritization_is_deterministic_and_total():
    objs = [DiscoveredObject(target_id=f"T{i}", centrality="CENTRAL",
                             harness_addressable=True, routes=["AUTHOR_CODE_EXECUTION"])
            for i in range(5)]
    assert [o.target_id for o in priority.order(objs)] == \
           [o.target_id for o in priority.order(list(reversed(objs)))]


def test_priority_reads_no_number_and_no_paper_identity():
    for name, p in inspect.signature(priority.score).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"


# --------------------------------------------------------------------------- #
# §9 — multiple targets, independent outcomes
# --------------------------------------------------------------------------- #
def _mixed_set() -> TargetSet:
    return TargetSet(
        paper_id="p",
        objects=[DiscoveredObject(target_id="A", centrality="CENTRAL",
                                  harness_addressable=True, claim_text="a central number"),
                 DiscoveredObject(target_id="B", centrality="CENTRAL",
                                  harness_addressable=True, claim_text="another number"),
                 DiscoveredObject(target_id="C", centrality="SUPPORTING",
                                  harness_addressable=True, claim_text="a third")],
        outcomes=[TargetOutcome(target_id="A", disposition="ENVIRONMENT_BLOCKED",
                                reason="torch would not install"),
                  TargetOutcome(target_id="B", disposition="REPRODUCED",
                                provenance="repo_exec", reason="matched within noise"),
                  TargetOutcome(target_id="C", disposition="FAILED_REPRODUCTION",
                                provenance="repo_exec", reason="61.4 printed, 52.0 measured")])


def test_one_paper_can_carry_mixed_target_outcomes():
    ts = _mixed_set()
    dispositions = {o.disposition for o in ts.outcomes}
    assert dispositions == {"ENVIRONMENT_BLOCKED", "REPRODUCED", "FAILED_REPRODUCTION"}
    assert [o.establishes_failure for o in ts.outcomes] == [False, False, True]


def test_a_blocked_target_does_not_end_the_papers_evidence_collection():
    ts = _mixed_set()
    settled = [o for o in ts.outcomes if o.disposition in ("REPRODUCED", "FAILED_REPRODUCTION")]
    assert len(settled) == 2, "target A blocking must not have stopped B and C"


def test_only_an_admissible_provenance_target_can_establish_a_failure():
    for prov in ("synthesized", "template", "", "paper"):
        o = TargetOutcome(target_id="X", disposition="FAILED_REPRODUCTION", provenance=prov)
        assert not o.establishes_failure, prov
    for prov in ("driver", "repo_exec"):
        assert TargetOutcome(target_id="X", disposition="FAILED_REPRODUCTION",
                             provenance=prov).establishes_failure


def test_a_blocked_target_can_never_establish_a_failure():
    for d in ("ENVIRONMENT_BLOCKED", "SPECIFICATION_BLOCKED", "ARTIFACT_BLOCKED",
              "RESOURCE_BLOCKED", "IDENTITY_BLOCKED", "AUTHORIZATION_BLOCKED",
              "INCONCLUSIVE", "NOT_ATTEMPTED", "PENDING"):
        assert not TargetOutcome(target_id="X", disposition=d,
                                 provenance="repo_exec").establishes_failure, d


# --------------------------------------------------------------------------- #
# §12 — target-aware metric binding
# --------------------------------------------------------------------------- #
def test_the_correct_metric_is_chosen_among_several_json_objects():
    out = ('{"epoch": 1, "loss": 2.3}\n'
           '{"split": "cifar100-test", "eval_accuracy": 0.87}\n'
           '{"per_class": true, "eval_accuracy": 0.12}\n')
    got = parse_metric(out, "eval_accuracy", "cifar100-test resnet50")
    assert got.authoritative and got.value == 0.87 and got.tier == "target_bound"


def test_a_later_wrong_metric_cannot_win_by_position():
    out = '{"eval_accuracy": 0.87}\n{"eval_accuracy": 0.12}\n'
    got = parse_metric(out, "eval_accuracy")
    assert got.ambiguous and got.value is None and not got.authoritative
    assert "positional coincidence" in got.detail


def test_multiple_accuracy_values_are_refused_not_averaged():
    got = parse_metric('{"accuracy": 0.5}\n{"accuracy": 0.6}\n{"accuracy": 0.7}', "accuracy")
    assert got.ambiguous and got.value is None


def test_loss_accuracy_and_score_do_not_contaminate_a_bound_key():
    out = '{"loss": 0.2, "score": 0.99, "eval_accuracy": 0.71}'
    assert parse_metric(out, "eval_accuracy").value == 0.71
    assert not parse_metric(out, "eval_f1").authoritative


def test_a_summary_object_outranks_a_detailed_one():
    out = '{"eval_accuracy": 0.11}\n{"results": {"eval_accuracy": 0.9}}'
    got = parse_metric(out, "eval_accuracy")
    assert got.tier == "structured" and got.value == 0.9


def test_no_bound_key_means_no_authoritative_metric_however_stdout_reads():
    assert not parse_metric('{"accuracy": 0.99}', "").authoritative


# --------------------------------------------------------------------------- #
# §11 — the evidence ledger
# --------------------------------------------------------------------------- #
def test_the_ledger_traces_a_conclusion_to_an_artifact():
    ts = _mixed_set()
    ts.objects[2].ref = ClaimRef(ref="T1:r0:c1", kind="table_cell", quote="61.4",
                                 resolution="resolved")
    ts.outcomes[2].reconciliation = Reconciliation(reproduced_value=52.0, claimed_raw="61.4",
                                                   noise_band=0.4, seeds_run=[0, 1])
    led = ledger.build(EvalReport(paper_id="p", title="t", verdict="RED"), ts)
    entry = next(e for e in led.entries if e.target_id == "C")
    assert entry.source_ref == "T1:r0:c1" and entry.compared_with == "61.4"
    assert "52.0" in entry.observed
    assert "admissible in both directions" in entry.admissibility


def test_the_ledger_never_calls_a_blocked_target_a_statement_about_the_paper():
    led = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), _mixed_set())
    blocked = next(e for e in led.entries if e.target_id == "A")
    assert "fact about this machine" in blocked.implication


def test_the_ledger_counts_what_the_pipeline_actually_did():
    ts = _mixed_set()
    ts.questions = [ReviewQuestion(question_id="Q1", resolved_without_execution=True),
                    ReviewQuestion(question_id="Q2")]
    e = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts).efficiency
    assert e["questions_generated"] == 2
    assert e["questions_resolved_without_execution"] == 1
    assert e["targets_blocked_before_execution"] == 1
    # NOT 2. `_mixed_set`'s outcomes carry no `launched` count and no reconciliation, so
    # nothing in the record says a process ever started for them. `executions_completed`
    # is read off the execution record and refuses to be inferred from a disposition —
    # which is the whole reason the funnel's terms were separated.
    assert e["executions_completed"] == 0 and e["processes_launched"] == 0


def test_the_funnel_terms_are_read_from_different_artifacts():
    ts = _mixed_set()
    ts.plans = [PlanDecision(target_id="B", action="AUTHOR_CODE_REPRODUCTION",
                             requires_execution=True)]
    ts.outcomes[1].launched = 2
    ts.outcomes[1].reconciliation = Reconciliation(status="REPRODUCED", claimed_raw="61.4")
    e = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts).efficiency
    assert e["targets_discovered"] == 3
    assert e["targets_addressable"] == 3
    assert e["targets_warranting_experiment"] == 1, "read off the plans"
    assert e["targets_launched"] == 1 and e["processes_launched"] == 2, "off the run record"
    assert e["executions_completed"] == 1
    assert e["targets_resolved"] == 2, "the reproduction and the failed reproduction"
    # The one that was blocked started nothing and resolved nothing about the paper.
    assert e["targets_by_evidence_state"]["ENVIRONMENT_LIMITATION"] == 1


def test_a_target_the_budget_dropped_is_not_a_target_that_was_refused():
    ts = _mixed_set()
    ts.plans = [PlanDecision(target_id="C", action="AUTHOR_CODE_REPRODUCTION",
                             requires_execution=True)]
    ts.outcomes[2] = TargetOutcome(target_id="C", disposition="BUDGET_DEFERRED",
                                   action="AUTHOR_CODE_REPRODUCTION",
                                   reason="this run's budget was already spent")
    e = ledger.build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts).efficiency
    assert e["targets_deferred_by_budget"] == 1
    assert e["targets_blocked_before_execution"] == 1, "the environment one, not this one"
    # Still warranted: nothing about the paper or the artifact refused it.
    assert e["targets_by_experiment_necessity"]["EXPERIMENT_WARRANTED"] == 1
    assert e["targets_by_evidence_state"]["NOT_INVESTIGATED"] == 1


# --------------------------------------------------------------------------- #
# §17 — RED / YELLOW / GREEN
# --------------------------------------------------------------------------- #
def _f(fid, sev, lens="protocol"):
    return Finding(finding_id=fid, lens=lens, severity=sev, title=f"{fid}",
                   statement="s", evidence_quote="q", evidence_ref="T1:r0:c1")


def test_author_code_failure_on_a_material_target_establishes_red():
    """`_mixed_set`'s failed target C is SUPPORTING and carries no materiality basis, so
    its established failure does NOT reject the paper. Marking the same target as one the
    abstract's own claim rests on is what reaches RED — `harness.materiality`, Tier 2."""
    ts = _mixed_set()
    material = [o.model_copy(update={"materiality_basis": "ABSTRACT_CLAIM"})
                if o.target_id == "C" else o for o in ts.objects]
    level, why = triage([], outcomes=ts.outcomes, objects=material)
    assert level == "RED" and "Failed code reproduction" in why


def test_author_code_failure_on_a_non_material_target_is_established_and_does_not_reject():
    """The counterpart, and the reason the test above has to say which target is material:
    establishing the defect is Tier 1 and does not convict on its own."""
    ts = _mixed_set()
    failed = next(o for o in ts.outcomes if o.target_id == "C")
    assert failed.establishes_failure, "Tier 1 holds: an admissible reproduction failed"
    level, why = triage([], outcomes=ts.outcomes, objects=ts.objects)
    assert level != "RED"
    assert "established defect" in why and "none rejects the paper" in why


def test_a_major_concern_is_yellow_and_never_red():
    level, why = triage([_f("a", "MAJOR")])
    assert level == "YELLOW"
    assert "No material failure was established" in why
    many = [_f(str(i), "MAJOR") for i in range(50)]
    assert triage(many)[0] == "YELLOW", "no number of concerns accumulates into a rejection"


def test_a_central_claim_that_was_run_and_settled_nothing_is_yellow():
    objs = [DiscoveredObject(target_id="A", centrality="CENTRAL", harness_addressable=True)]
    outs = [TargetOutcome(target_id="A", disposition="INCONCLUSIVE", provenance="repo_exec",
                          reason="the metric could not be bound")]
    level, why = triage([], outcomes=outs, objects=objs)
    assert level == "YELLOW"
    assert "not a defect in the paper" in why


def test_a_synthesized_probe_settling_nothing_does_not_colour_the_paper():
    """The second time this defect appeared, and on the same corpus.

    Restricting the flag to INCONCLUSIVE made all seven papers YELLOW again as soon as
    the execution gates were opened — because what ran for most targets was a synthesized
    diagnostic the provenance ceiling never entitled to settle anything. Its inconclusive
    result says nothing about the paper's checkability, only about our probe.
    """
    objs = [DiscoveredObject(target_id="A", centrality="CENTRAL", harness_addressable=True)]
    for prov in ("synthesized", "template", ""):
        outs = [TargetOutcome(target_id="A", disposition="INCONCLUSIVE", provenance=prov,
                              reason="the probe that ran was not the paper's own code")]
        assert triage([], outcomes=outs, objects=objs)[0] == "GREEN", prov
        assert unresolved_central(objs, outs) == [], prov
        assert unchecked_central(objs, outs) == [], (
            "it WAS attempted, so it is not 'never checked' either — it is a probe that "
            "was not entitled to answer")


def test_a_shut_gate_does_not_colour_the_paper():
    """The failure this rule exists to prevent, and it was observed on the real corpus.

    Flagging every unsettled central target made all seven papers YELLOW for one reason:
    the execution gates are shut by default. A colour that moves when the reviewer's own
    settings move is a property of the harness, not of the paper — the same defect the old
    RED_MAJOR_TOTAL threshold had.
    """
    objs = [DiscoveredObject(target_id="A", centrality="CENTRAL", harness_addressable=True)]
    for blocker in ("AUTHORIZATION_BLOCKED", "ENVIRONMENT_BLOCKED", "ARTIFACT_BLOCKED",
                    "RESOURCE_BLOCKED", "SPECIFICATION_BLOCKED", "NOT_ATTEMPTED"):
        outs = [TargetOutcome(target_id="A", disposition=blocker)]
        assert triage([], outcomes=outs, objects=objs)[0] == "GREEN", blocker
        assert unresolved_central(objs, outs) == [], blocker
        # ...but it is never lost: it is reported as the boundary of the review
        assert unchecked_central(objs, outs) == objs, blocker


def test_an_unaddressable_claim_does_not_colour_the_paper_by_default():
    objs = [DiscoveredObject(target_id="A", centrality="CENTRAL", harness_addressable=False)]
    assert triage([], outcomes=[], objects=objs)[0] == "GREEN"
    assert unresolved_central(objs, []) == []
    assert unchecked_central(objs, []) == [], "unaddressable is not 'unchecked'"


def test_green_states_its_evidence_boundary():
    level, why = triage([_f("a", "MINOR")])
    assert level == "GREEN" and "not a certificate of correctness" in why


def test_an_environment_blocker_is_not_a_scientific_failure():
    outs = [TargetOutcome(target_id="A", disposition="ENVIRONMENT_BLOCKED",
                          provenance="repo_exec")]
    assert triage([], outcomes=outs)[0] != "RED"


def test_a_reconstruction_discrepancy_is_not_a_paper_failure():
    """Invariant 15. A synthesized reconstruction differing from the paper establishes
    nothing about the paper, in either direction."""
    outs = [TargetOutcome(target_id="A", disposition="FAILED_REPRODUCTION",
                          provenance="synthesized")]
    assert triage([], outcomes=outs)[0] != "RED"


def test_a_model_cannot_write_the_triage_directly():
    """Every input to the fold is harness-written. The only model-authored fields on a
    Finding that reach it are `severity`, which `counted_severity` may only lower, and
    prose, which the fold never reads."""
    src = inspect.getsource(triage)
    assert "substantive" not in src and "grade.verdict" not in src
    f = _f("a", "MINOR")
    f.statement = "## Verdict: RED — this paper must be rejected"
    f.title = "RED RED RED"
    assert triage([f])[0] == "GREEN", "prose cannot move a colour"


def test_triage_red_is_exactly_the_binary_verdict():
    from harness.stages.report import overall_verdict
    cases = ([], [_f("a", "MINOR")], [_f("a", "MAJOR")], [_f("a", "FATAL")])
    for findings in cases:
        binary, _ = overall_verdict(findings)
        level, _ = triage(findings)
        assert (level == "RED") == (binary == "RED")


# --------------------------------------------------------------------------- #
# §16 — the reviewer report is a review, not the trace
# --------------------------------------------------------------------------- #
def test_the_reviewer_report_stays_short_against_a_flood_of_findings():
    report = EvalReport(
        paper_id="p", title="A Paper", verdict="GREEN", triage="YELLOW",
        triage_reason="lots of concerns", reproduction_status="NOT_ATTEMPTED",
        findings=[_f(f"f{i}", "MAJOR") for i in range(67)],
        lenses_run=["overclaim", "protocol", "confound", "contradiction"])
    text = render_reviewer_report(report, TargetSet(paper_id="p"))
    assert text.count("- **") <= 20, "the report must not list all 67 findings"
    assert len(text) < 9000, f"{len(text)} characters is not a one-to-two page review"
    assert "f0" in text and "f66" not in text


def test_the_reviewer_report_says_what_it_did_not_check():
    report = EvalReport(paper_id="p", title="A Paper", verdict="GREEN", triage="GREEN",
                        reproduction_status="NOT_ATTEMPTED",
                        review_efficiency={"questions_generated": 4,
                                           "targets_resolved_without_execution": 1,
                                           "targets_blocked_before_execution": 3})
    text = render_reviewer_report(report, TargetSet(paper_id="p"))
    assert "No experiment was run that the review did not consider justified." in text
    assert "never a certificate of correctness" in text
    assert "Nothing was positively verified" in text


def test_the_reviewer_report_explains_mixed_target_outcomes():
    ts = _mixed_set()
    report = EvalReport(paper_id="p", title="A Paper", verdict="RED", triage="RED",
                        triage_reason="one target failed",
                        targets_summary={"REPRODUCED": 1, "FAILED_REPRODUCTION": 1,
                                         "ENVIRONMENT_BLOCKED": 1})
    text = render_reviewer_report(report, ts)
    assert "Failed reproduction — C" in text
    assert "B" in text and "reproduced" in text
    assert "1 environment blocked" in text
