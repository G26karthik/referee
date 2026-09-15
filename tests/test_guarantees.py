"""The guarantees layer REPORTS; it must be structurally unable to decide.

Every test whose name begins `test_a_`, `test_no_` or `test_the_` and reads as a
prohibition pins one way this section could have become the thing it describes:

  * a guarantee asserted in an artifact instead of read off one, so the reassuring half
    survives the change that stopped enforcing it;
  * a non-guarantee list that empties out on a good run, which is the only run whose
    reader most needs it;
  * a "harness defect" bucket that fills up with gates the operator deliberately left
    shut, which teaches a reader to ignore both halves;
  * a statement that could be softened for one paper, or a `holds` that could be raised
    by how the review turned out;
  * a threshold, a colour or a severity that could read this layer.

The architecture-level tests here are signature introspection, field absence, import
absence, and exhaustive sweeps over the closed vocabulary — not happy paths.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from harness import guarantees
from harness.artifacts import (BLOCKED_DISPOSITIONS, EvalReport, Finding, GUARANTEE_KINDS,
                               Guarantee, ProbeResult, Reconciliation, ReviewGuarantees,
                               SEVERITIES, TARGET_DISPOSITIONS, TargetOutcome, TargetSet)
from harness.stages import report as report_stage

HARNESS_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# fixtures — two reviews that are as different as this system's outputs get
# --------------------------------------------------------------------------- #
def _finding(**kw) -> Finding:
    base = dict(finding_id="f-01", lens="overclaim", severity="MINOR", title="t",
                statement="s", evidence_quote="q", evidence_ref="T1:r0:c0",
                evidence_class="cell_verified")
    base.update(kw)
    return Finding(**base)


def _clean_report(**kw) -> EvalReport:
    base = dict(paper_id="alpha", verdict="GREEN", claim_status="NOT_VERIFIED",
                findings=[_finding()], dropped_findings=1,
                ledger_path="reports/alpha.ledger.json",
                review_efficiency={"targets_discovered": 8})
    base.update(kw)
    return EvalReport(**base)


def _target_set(**kw) -> TargetSet:
    base = dict(paper_id="alpha", outcomes=[
        TargetOutcome(target_id="T1", disposition="AUTHORIZATION_BLOCKED",
                      failure_class="execution_unauthorized",
                      reason="the execution gate was closed"),
        TargetOutcome(target_id="T2", disposition="NOT_ATTEMPTED")])
    base.update(kw)
    return TargetSet(**base)


def _red_report() -> tuple[EvalReport, TargetSet]:
    """A review that established a material failure through the admissible route.

    The target carries its DiscoveredObject, with the materiality basis that makes a
    central claim depend on it. Both halves are required now: `establishes_failure` is
    Tier 1, and a paper-level stop additionally needs `harness.materiality` — and an
    established failure whose target has no object at all is a state inconsistency this
    harness reports under ACCOUNTING_REPRODUCIBLE rather than treating as material.
    """
    from harness.artifacts import DiscoveredObject

    rec = Reconciliation(status="FAILED_REPRODUCTION", provenance="repo_exec",
                         table_ref="T2:r1:c3", target_id="T1")
    ts = TargetSet(paper_id="omega",
                   objects=[DiscoveredObject(target_id="T1", centrality="CENTRAL",
                                             materiality_basis="ABSTRACT_CLAIM")],
                   outcomes=[TargetOutcome(
                       target_id="T1", disposition="FAILED_REPRODUCTION",
                       provenance="repo_exec", launched=3, reconciliation=rec,
                       reason="0.71 measured against 0.83 printed", failure_class="none")])
    rep = EvalReport(
        paper_id="omega", verdict="RED", claim_status="VERIFIED_FAILURE",
        findings=[_finding(severity="FATAL")], ledger_path="reports/omega.ledger.json",
        review_efficiency={"targets_discovered": 4},
        probe=ProbeResult(paper_id="omega", executions=3, provenance="repo_exec",
                          execution_log="runs/omega/execution.jsonl",
                          reconciliation=rec,
                          authorization={"allowed": True, "decision": "authorized"}))
    return rep, ts


ALL_BOOL_PARAMS = list(inspect.signature(guarantees.assess).parameters)


# --------------------------------------------------------------------------- #
# 1. A GUARANTEE IS DERIVED, NEVER ASSERTED
# --------------------------------------------------------------------------- #
def test_a_hand_asserted_guarantee_is_overwritten_by_what_the_artifacts_say():
    """Prevents the failure this module exists for: a stale promise outliving the
    mechanism. A `ReviewGuarantees` hand-built with every `holds=True` must not survive
    `derive` on a report whose artifacts contradict it — otherwise the honest paragraph
    and the run it describes can drift apart, and the half that drifts first is always
    the reassuring one."""
    lie = ReviewGuarantees(
        paper_id="alpha",
        guarantees=[Guarantee(key=k, kind="PROCESS", statement=guarantees.STATEMENT[k],
                              holds=True, evidence="trust me")
                    for k in guarantees.ALL_KEYS],
        non_guarantees=[], unmet=[])
    assert all(g.holds for g in lie.guarantees)

    rep = _clean_report(findings=[_finding(evidence_class="")])
    derived = guarantees.derive(rep, _target_set())
    by_key = {g.key: g for g in derived.guarantees}
    assert by_key["EVERY_EVIDENCE_POINTER_RE_VERIFIED"].holds is False
    assert derived.unmet == ["EVERY_EVIDENCE_POINTER_RE_VERIFIED"]
    # and the entry says which field settled it, not that someone said so
    assert "evidence_class" in by_key["EVERY_EVIDENCE_POINTER_RE_VERIFIED"].evidence
    assert "trust me" not in by_key["EVERY_EVIDENCE_POINTER_RE_VERIFIED"].evidence


def test_every_guarantee_names_the_artifact_and_field_that_establishes_it():
    """A guarantee whose evidence is prose is a guarantee nobody can check. Each entry's
    `evidence` must begin with the artifact-and-field pointer, so a reader who doubts an
    entry knows where to look before knowing what it claimed."""
    g = guarantees.derive(*(_clean_report(), _target_set()))
    assert len(g.guarantees) == len(guarantees.ALL_KEYS)
    for item in g.guarantees:
        pointer = guarantees.ESTABLISHED_BY[item.key]
        assert item.evidence.startswith(pointer), item.key
        # a "no artifact" pointer is only allowed where the property is unattainable
        if pointer.startswith("no artifact"):
            assert item.key in guarantees.SCIENTIFIC_NON_GUARANTEES, item.key


@pytest.mark.parametrize("key,mutation", [
    ("EVERY_EVIDENCE_POINTER_RE_VERIFIED", "unverified_finding"),
    ("PROVENANCE_CEILING_HELD", "synthesized_conviction"),
    ("EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION", "unauthorized_run"),
    ("INFRASTRUCTURE_FAILURE_NEVER_CONVICTED", "infra_conviction"),
    ("EVERY_UNRESOLVED_STATE_NAMED", "silent_refusal"),
    ("ACCOUNTING_REPRODUCIBLE", "no_ledger"),
    ("SEVERITY_ONLY_CAPPED", "promoted_severity"),
    ("NO_MODEL_WROTE_THE_DECISION", "unprojected_verdict"),
    ("NO_EXPERIMENT_DOWNSCALED", "resource_blocked_but_ran"),
])
def test_every_process_guarantee_can_individually_fail(key, mutation):
    """A check that cannot fail is decoration. Nine process guarantees are printed as
    machine-enforced on every review; if one of them held unconditionally, the section
    would be advertising a mechanism nothing exercises."""
    rep, ts = _clean_report(), _target_set()
    if mutation == "unverified_finding":
        rep = _clean_report(findings=[_finding(evidence_class="")])
    elif mutation == "synthesized_conviction":
        rep = _clean_report(probe=ProbeResult(
            paper_id="alpha",
            reconciliation=Reconciliation(status="FAILED_REPRODUCTION",
                                          provenance="synthesized")))
    elif mutation == "unauthorized_run":
        rep = _clean_report(probe=ProbeResult(
            paper_id="alpha", executions=2, execution_log="runs/alpha/execution.jsonl"))
    elif mutation == "infra_conviction":
        rep = _clean_report(verdict="RED", claim_status="VERIFIED_FAILURE",
                            findings=[_finding(severity="FATAL")])
        ts = _target_set(outcomes=[TargetOutcome(
            target_id="T1", disposition="FAILED_REPRODUCTION", provenance="repo_exec",
            failure_class="dependency_missing", launched=1, reason="a wheel was missing")])
    elif mutation == "silent_refusal":
        ts = _target_set(outcomes=[TargetOutcome(
            target_id="T1", disposition="RESOURCE_BLOCKED", reason="")])
    elif mutation == "no_ledger":
        rep = _clean_report(ledger_path="")
    elif mutation == "promoted_severity":
        rep = _clean_report(findings=[_finding(severity="MINOR",
                                               counted_severity="FATAL")])
    elif mutation == "unprojected_verdict":
        rep = _clean_report(verdict="RED", claim_status="NOT_VERIFIED")
    elif mutation == "resource_blocked_but_ran":
        ts = _target_set(outcomes=[TargetOutcome(
            target_id="T1", disposition="RESOURCE_BLOCKED", launched=1,
            reason="24 GiB demanded, 8 GiB present")])
    assert key in guarantees.derive(rep, ts).unmet


def test_a_vacuous_guarantee_says_that_it_was_never_exercised():
    """"Every process was authorized" is trivially true when no process ran, and printing
    it the same way as a run that was actually gated would be the strongest sentence in
    the section resting on nothing. The evidence has to disclose the vacuity."""
    g = guarantees.derive(_clean_report(), _target_set())
    item = next(i for i in g.guarantees
                if i.key == "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION")
    assert item.holds is True
    assert "without being exercised" in item.evidence


def test_no_serious_candidate_does_not_earn_the_independently_graded_property():
    """A clean paper raises no FATAL/MAJOR candidate. Reading that as "grading held"
    would let the review with the least evidence behind it claim the strongest process
    property in the list; nothing was established either way, so it does not hold."""
    g = guarantees.derive(_clean_report(grade_coverage={"candidates": 0, "graded": 0}),
                          _target_set())
    item = next(i for i in g.guarantees if i.key == "SEVERITY_INDEPENDENTLY_GRADED")
    assert item.holds is False
    assert "no serious candidate" in item.evidence
    # and a partially graded review does not round up
    partly = guarantees.derive(
        _clean_report(grade_coverage={"candidates": 3, "graded": 1}), _target_set())
    assert next(i for i in partly.guarantees
                if i.key == "SEVERITY_INDEPENDENTLY_GRADED").holds is False


# --------------------------------------------------------------------------- #
# 2. THE HONEST HALF IS NEVER EMPTY
# --------------------------------------------------------------------------- #
def test_the_non_guarantee_list_is_never_empty_for_any_reachable_input():
    """Prevents the single worst outcome for this section: a review that reads as though
    nothing were missing. Reviewer accuracy is unmeasured on every paper because no
    adjudicated ground truth exists, so at least one non-guarantee stands on every input
    — swept over the whole 2**15 boolean space `assess` accepts."""
    n = len(ALL_BOOL_PARAMS)
    for mask in range(1 << n):
        kwargs = {p: bool(mask >> i & 1) for i, p in enumerate(ALL_BOOL_PARAMS)}
        items = guarantees.assess(**kwargs)
        assert any(not g.holds for g in items), kwargs


def test_a_perfect_review_still_publishes_every_scientific_non_guarantee():
    """The derived artifact, not just the pure table: a run in which every boolean is
    true must still carry all six scientific non-guarantees, because none of them is
    reachable by running better."""
    rep, ts = _red_report()
    g = guarantees.derive(rep, ts, lens_policy_enforced=True)
    for key in guarantees.SCIENTIFIC_NON_GUARANTEES:
        assert guarantees.NOT_HELD[key] in g.non_guarantees, key


def test_novelty_is_never_reported_as_checked():
    """`VERIFICATION_ROUTES` carries LITERATURE_SEARCH and nothing implements it. There
    must be no input — no gate, no backend, no coverage number — that lets this system
    report a novelty or prior-art conclusion."""
    n = len(ALL_BOOL_PARAMS)
    for mask in range(1 << n):
        kwargs = {p: bool(mask >> i & 1) for i, p in enumerate(ALL_BOOL_PARAMS)}
        item = next(g for g in guarantees.assess(**kwargs) if g.key == "NOVELTY_ASSESSED")
        assert item.holds is False
    assert "NOVELTY_ASSESSED" not in guarantees._PARAM_FOR_KEY


def test_no_scientific_guarantee_can_hold_on_any_input():
    """`artifacts.GUARANTEE_KINDS` says this system makes no SCIENTIFIC guarantee. That
    is a comment until something sweeps it: a key that could be flipped by a caller would
    let "the paper is correct" be published from a report."""
    n = len(ALL_BOOL_PARAMS)
    for mask in range(1 << n):
        kwargs = {p: bool(mask >> i & 1) for i, p in enumerate(ALL_BOOL_PARAMS)}
        for g in guarantees.assess(**kwargs):
            if g.kind == "SCIENTIFIC":
                assert g.holds is False, g.key
                assert g.key in guarantees.SCIENTIFIC_NON_GUARANTEES


def test_grading_off_names_the_gate_and_says_whose_word_the_severity_is():
    """With `SH_ALLOW_GRADING` off, every counted severity is the asserting lens's own.
    A reader who is not told which gate was shut cannot tell "graded clean" from
    "not graded", which is the exact confusion `EvalReport.grade_coverage` exists for."""
    g = guarantees.derive(_clean_report(), _target_set())
    sentence = guarantees.NOT_HELD["SEVERITY_INDEPENDENTLY_GRADED"]
    assert sentence in g.non_guarantees
    assert "SH_ALLOW_GRADING" in sentence
    assert "lens's own" in sentence
    assert "SH_ALLOW_GRADING" in "\n".join(guarantees.render(g))


# --------------------------------------------------------------------------- #
# 3. NOTHING HERE IS PAPER-SPECIFIC OR OUTCOME-SPECIFIC
# --------------------------------------------------------------------------- #
def test_the_derivation_signature_admits_no_count_and_no_paper_identity():
    """"This paper gets a stronger guarantee", "if the delta is small claim less" and
    "if this venue, soften" must be INEXPRESSIBLE. `assess` takes booleans, keyword-only,
    each defaulting False so a forgotten term under-claims rather than promotes."""
    sig = inspect.signature(guarantees.assess)
    assert sig.parameters, "a derivation with no inputs cannot be per-review"
    for name, p in sig.parameters.items():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, name
        # `from __future__ import annotations` makes these strings.
        assert str(p.annotation) == "bool", (name, p.annotation)
        assert p.default is False, name
    # Per underscore-separated SEGMENT, the convention
    # tests/test_reasoning_architecture.py established. `lens` is deliberately NOT on the
    # list: `lens_policy_enforced` is a run-level boolean about which tool policy was
    # enforced, it carries no lens's name and no lens's judgement, and banning the segment
    # would forbid the one honest way to say "their isolation is unrecorded".
    banned = {"count", "n", "seeds", "seed", "delta", "value", "values", "epsilon",
              "metric", "paper", "paper_id", "threshold", "score", "num", "size",
              "title", "id", "venue", "author", "authors"}
    segments = {s for name in sig.parameters for s in name.split("_")}
    assert not (segments & banned), segments & banned


def test_no_guarantee_sentence_can_mention_a_paper_or_a_number():
    """The list describes the SYSTEM, so its text is quotable and comparable across
    papers. A statement that interpolated a count, a title or an outcome would make two
    reviews' guarantee sections incomparable, and would let a good run advertise more
    than the mechanism provides."""
    for table in (guarantees.STATEMENT, guarantees.NOT_HELD, guarantees.SHORT):
        for key, text in table.items():
            assert "{" not in text and "%" not in text, (key, "no interpolation slot")
            # a bare digit in a fixed statement is either a paper number or a threshold
            assert not re.search(r"\d", text.replace("2026", "")), (key, text)
    # and derive over two very different reviews yields identical statements
    left = guarantees.derive(_clean_report(), _target_set())
    right = guarantees.derive(*_red_report())
    assert ([g.statement for g in left.guarantees]
            == [g.statement for g in right.guarantees])
    assert ([g.key for g in left.guarantees] == list(guarantees.ALL_KEYS)
            == [g.key for g in right.guarantees])


def test_the_key_order_is_the_closed_vocabulary_and_never_the_review_outcome():
    """A section that reordered itself by what went well would let a reader infer the
    result from the layout. The order is `ALL_KEYS`, on every input."""
    n = len(ALL_BOOL_PARAMS)
    for mask in (0, 1, 5, 1023, (1 << n) - 1):
        kwargs = {p: bool(mask >> i & 1) for i, p in enumerate(ALL_BOOL_PARAMS)}
        assert [g.key for g in guarantees.assess(**kwargs)] == list(guarantees.ALL_KEYS)


def test_the_vocabulary_is_closed_disjoint_and_completely_tabulated():
    """Five tables are keyed on the same vocabulary. A key present in one and missing
    from another would render as a blank clause or crash at report time — and the tables
    are exactly where a new entry gets added without deciding what establishes it."""
    groups = (guarantees.PROCESS_GUARANTEES, guarantees.CONDITIONAL_PROPERTIES,
              guarantees.SCIENTIFIC_NON_GUARANTEES)
    seen: set[str] = set()
    for group in groups:
        assert len(set(group)) == len(group)
        assert not (seen & set(group)), seen & set(group)
        seen |= set(group)
    assert seen == set(guarantees.ALL_KEYS)
    for table in (guarantees.KIND, guarantees.STATEMENT, guarantees.NOT_HELD,
                  guarantees.SHORT, guarantees.ESTABLISHED_BY):
        assert set(table) == set(guarantees.ALL_KEYS)
        assert all(v.strip() for v in table.values())
    assert set(guarantees.KIND.values()) <= set(GUARANTEE_KINDS)
    assert (set(guarantees._PARAM_FOR_KEY)
            == set(guarantees.PROCESS_GUARANTEES) | set(guarantees.CONDITIONAL_PROPERTIES))
    assert set(guarantees._PARAM_FOR_KEY.values()) == set(ALL_BOOL_PARAMS)


# --------------------------------------------------------------------------- #
# 4. AN UNMET GUARANTEE IS A HARNESS DEFECT AND REACHES NO THRESHOLD
# --------------------------------------------------------------------------- #
def test_an_unmet_guarantee_is_never_a_finding_about_the_paper():
    """Invariant 16's shape, one level up: a defect in this harness must not become
    evidence against the authors. An unmet process guarantee adds no finding, moves no
    severity, and the section says in its own words that it is not about the paper."""
    rep = _clean_report(findings=[_finding(evidence_class="")])
    ts = _target_set()
    g = guarantees.derive(rep, ts)
    assert g.unmet
    # the report is untouched: same verdict, same claim status, same findings
    assert rep.verdict == "GREEN" and rep.claim_status == "NOT_VERIFIED"
    assert report_stage.overall_verdict(list(rep.findings), None,
                                        outcomes=list(ts.outcomes))[0] == "GREEN"
    text = "\n".join(guarantees.render(g))
    assert "DEFECT IN THIS HARNESS" in text
    assert "not a finding about the paper" in text
    for severity in SEVERITIES:
        assert severity not in text


def test_unmet_holds_only_unconditional_process_guarantees():
    """`ReviewGuarantees.unmet` is documented as a harness defect. A conditional property
    — grading off, no whole-submission read, the authors' code not run — is a
    CONFIGURATION, and the first version of this module listed those under `unmet` on all
    seven corpus papers. That is invariant 17's defect: a property of this harness's
    configuration reported as a finding."""
    g = guarantees.derive(_clean_report(), _target_set())
    assert set(g.unmet) <= set(guarantees.PROCESS_GUARANTEES)
    for key in guarantees.CONDITIONAL_PROPERTIES:
        assert key not in g.unmet, key
        assert guarantees.NOT_HELD[key] in g.non_guarantees, key
    for key in guarantees.SCIENTIFIC_NON_GUARANTEES:
        assert key not in g.unmet, key


def test_a_closed_gate_can_never_produce_an_unmet_guarantee():
    """Swept: no combination of conditional properties being false puts anything in
    `unmet`, so no operator choice about gates can make this harness look broken."""
    conditional = [guarantees._PARAM_FOR_KEY[k]
                   for k in guarantees.CONDITIONAL_PROPERTIES]
    process = [guarantees._PARAM_FOR_KEY[k] for k in guarantees.PROCESS_GUARANTEES]
    for mask in range(1 << len(conditional)):
        kwargs = {p: True for p in process}
        kwargs.update({p: bool(mask >> i & 1) for i, p in enumerate(conditional)})
        items = guarantees.assess(**kwargs)
        unmet = [g.key for g in items
                 if not g.holds and g.key in guarantees.PROCESS_GUARANTEES]
        assert unmet == [], (mask, unmet)


def test_no_decision_function_can_read_the_guarantees():
    """Parameter absence, the way `outcome.finding_state` proves an execution cannot move
    a finding state. If a threshold could read this layer, every gate under it would
    become advisory — the settlement invariant 8 and the whole-paper assessment
    already turn on."""
    for fn in (report_stage.overall_verdict, report_stage.claim_status,
               report_stage.triage, report_stage.counted,
               report_stage.material_failures, report_stage.reproduction_status):
        params = set(inspect.signature(fn).parameters)
        for bad in ("guarantees", "guarantee", "non_guarantees", "unmet", "held"):
            assert bad not in params, (fn.__name__, bad)


def test_the_modules_that_decide_do_not_import_the_module_that_reports():
    """Import absence. `grading.derive`, `taxonomy.classify`, `planner.classify` and
    `priority.score` decide what may be concluded; a dependency on this layer would be a
    route for a reporting change to move a decision, and it would also be a cycle
    waiting to happen."""
    # An IMPORT, not the word: `taxonomy.py`'s comments discuss what a re-verified
    # quotation guarantees, and a substring test would flag prose while missing
    # `importlib.import_module("harness.guarantees")`.
    imports = re.compile(
        r"(?m)^\s*(?:from\s+(?:\.|harness)\s+import\s+[^\n]*\bguarantees\b"
        r"|from\s+(?:\.|harness\.)guarantees\s+import"
        r"|import\s+harness\.guarantees"
        r"|import_module\(\s*['\"]harness\.guarantees)")
    for name in ("grading", "taxonomy", "planner", "priority", "discovery", "questions",
                 "claims", "provenance", "outcome"):
        src = (HARNESS_ROOT / "harness" / f"{name}.py").read_text(encoding="utf-8")
        assert not imports.search(src), f"harness/{name}.py imports guarantees"


def test_no_guarantee_field_carries_a_severity_or_a_colour():
    """Field absence on the models this layer writes. A `severity` or `verdict` field
    here would eventually be read: a field that exists is a field something will read."""
    for model in (Guarantee, ReviewGuarantees):
        for banned in ("severity", "counted_severity", "verdict", "triage", "colour",
                       "color", "claim_status", "confidence"):
            assert banned not in model.model_fields, (model.__name__, banned)
    assert set(Guarantee.model_fields) == {"kind", "statement", "holds", "evidence"}


# --------------------------------------------------------------------------- #
# 5. THE RENDERED SECTION
# --------------------------------------------------------------------------- #
def test_the_section_uses_no_support_word_the_review_has_not_earned():
    """`stages.report.unearned_support_language` is the "GREEN may not borrow the words
    of evidence it does not have" guard. This section is prose a NOT_VERIFIED review
    prints, and it is the newest and longest such prose in the artifact, so it is exactly
    where "verified" would creep back in."""
    for rep, ts in ((_clean_report(), _target_set()), _red_report()):
        text = "\n".join(guarantees.render(guarantees.derive(rep, ts)))
        for status in report_stage.CLAIM_STATUSES:
            assert report_stage.unearned_support_language(text, status) == [], status


def test_the_section_is_bounded_by_construction_and_says_what_it_dropped():
    """Invariant 19: bounded by a named cap, not by hoping twenty-one clauses stay short.
    Flooding the item list past every budget must still produce a bounded section, and
    the elision must be visible."""
    g = guarantees.derive(_clean_report(), _target_set())
    text = "\n".join(guarantees.render(g))
    assert len(text) <= guarantees._MAX_SECTION_CHARS, len(text)
    assert isinstance(guarantees._MAX_HELD_CHARS, int)
    assert isinstance(guarantees._MAX_NOT_HELD_CHARS, int)
    # each cap bites independently of total length
    joined = guarantees._clauses(["a" * 40 for _ in range(20)],
                                 guarantees._MAX_HELD_CHARS)
    assert len(joined) <= guarantees._MAX_HELD_CHARS + 40
    assert "more, in the ledger)" in joined
    # a widened cap cannot let the section grow past the section cap
    flooded = ReviewGuarantees(
        paper_id="alpha",
        guarantees=[Guarantee(key=k, kind=guarantees.KIND[k],
                              statement=guarantees.STATEMENT[k], holds=False,
                              evidence="e") for k in guarantees.ALL_KEYS],
        non_guarantees=list(guarantees.NOT_HELD.values()),
        unmet=list(guarantees.PROCESS_GUARANTEES))
    assert len("\n".join(guarantees.render(flooded))) <= guarantees._MAX_SECTION_CHARS


def test_the_section_prints_no_verdict_no_colour_and_no_severity():
    """The section reports; it must not be readable as the outcome. A colour or a severity
    token in it would be read as one, whatever the surrounding sentence said."""
    for rep, ts in ((_clean_report(), _target_set()), _red_report()):
        text = "\n".join(guarantees.render(guarantees.derive(rep, ts)))
        for token in ("RED", "GREEN", "YELLOW", "VERIFIED_FAILURE", "VERIFIED_SUPPORT",
                      *SEVERITIES):
            assert token not in text, token


def test_a_clean_review_and_a_red_review_print_the_same_process_guarantees():
    """The process half describes mechanisms, so it must not vary with the result. If a
    RED review printed more machine-enforced clauses than a clean one, a reader could
    infer the outcome from the section that exists to state the system's limits."""
    clean = guarantees.derive(_clean_report(), _target_set())
    red = guarantees.derive(*_red_report())
    held_clean = {g.key for g in clean.guarantees
                  if g.holds and g.key in guarantees.PROCESS_GUARANTEES}
    held_red = {g.key for g in red.guarantees
                if g.holds and g.key in guarantees.PROCESS_GUARANTEES}
    assert held_clean == held_red == set(guarantees.PROCESS_GUARANTEES)


def test_the_reader_facing_clauses_are_globally_distinct():
    """Twenty-one clauses in two paragraphs. Two that read the same would put a reader in
    the position of guessing which axis a sentence belonged to — the collision
    `harness/labels.py` exists to prevent one level down."""
    shorts = list(guarantees.SHORT.values())
    assert len(set(shorts)) == len(shorts)
    statements = list(guarantees.STATEMENT.values())
    assert len(set(statements)) == len(statements)
    not_held = list(guarantees.NOT_HELD.values())
    assert len(set(not_held)) == len(not_held)


def test_the_section_states_the_four_things_a_referee_would_otherwise_assume():
    """The four claims a 2026 reviewer rejects on sight if left unstated: measured
    accuracy, novelty checking, correctness, and standing in for a referee. Each must
    appear in the rendered section of every review, not only in the ledger."""
    for rep, ts in ((_clean_report(), _target_set()), _red_report()):
        text = "\n".join(guarantees.render(guarantees.derive(rep, ts)))
        for clause in ("accuracy unmeasured", "novelty and prior art not checked",
                       "nothing here says the paper is correct",
                       "not a substitute for a referee"):
            assert clause in text, clause


# --------------------------------------------------------------------------- #
# 6. DRIFT GUARDS AGAINST THE VOCABULARIES THIS MODULE COPIES
# --------------------------------------------------------------------------- #
def test_the_copied_material_severity_has_not_drifted_from_the_table_that_decides():
    """`stages.report` renders this section, so this module cannot import it and holds a
    copy of `MATERIAL_SEVERITY`. A copy needs a guard or it is a second rule."""
    assert set(guarantees._MATERIAL_SEVERITY) == set(report_stage.MATERIAL_SEVERITY)


def test_the_infrastructure_failure_list_leaves_the_one_conviction_route_open():
    """Invariant 7 admits exactly one conviction: a crash after the experiment
    demonstrably started. A check that treated `runtime_failure` as infrastructure would
    report a harness defect for the one case the harness allows."""
    from harness.artifacts import FAILURE_CLASSES
    assert set(guarantees._INFRASTRUCTURE_FAILURE) <= set(FAILURE_CLASSES)
    for allowed in ("runtime_failure", "timeout", "none"):
        assert allowed not in guarantees._INFRASTRUCTURE_FAILURE, allowed


def test_every_recognised_disposition_keeps_the_refusal_guarantee_intact():
    """Sweep over the closed disposition vocabulary: a named disposition with a reason
    must never be reported as an unexplained absence, and only a refusal is required to
    carry one — the guarantee's own wording."""
    for disposition in TARGET_DISPOSITIONS:
        ts = _target_set(outcomes=[TargetOutcome(
            target_id="T1", disposition=disposition, reason="a stated reason")])
        assert "EVERY_UNRESOLVED_STATE_NAMED" not in guarantees.derive(
            _clean_report(), ts).unmet, disposition
    for disposition in BLOCKED_DISPOSITIONS:
        ts = _target_set(outcomes=[TargetOutcome(target_id="T1",
                                                 disposition=disposition, reason="")])
        assert "EVERY_UNRESOLVED_STATE_NAMED" in guarantees.derive(
            _clean_report(), ts).unmet, disposition


def test_every_provenance_outside_the_ceiling_breaks_the_ceiling_guarantee():
    """Invariant 3, swept. Any provenance the ceiling does not admit, carrying a status
    that settles a printed quantity, must be reported as an unmet guarantee — in both
    directions, convicting and acquitting."""
    from harness.provenance import ADMISSIBLE_REPRODUCTION_PROVENANCE
    for status in ("FAILED_REPRODUCTION", "RESOLVED_VERIFIED"):
        for prov in ("synthesized", "template", "paper", "", "DRIVER", " repo_exec"):
            rep = _clean_report(probe=ProbeResult(
                paper_id="alpha",
                reconciliation=Reconciliation(status=status, provenance=prov)))
            assert "PROVENANCE_CEILING_HELD" in guarantees.derive(
                rep, _target_set()).unmet, (status, prov)
        for prov in ADMISSIBLE_REPRODUCTION_PROVENANCE:
            rep = _clean_report(probe=ProbeResult(
                paper_id="alpha",
                reconciliation=Reconciliation(status=status, provenance=prov)))
            assert "PROVENANCE_CEILING_HELD" not in guarantees.derive(
                rep, _target_set()).unmet, (status, prov)


def test_a_severity_promotion_at_any_rank_breaks_the_capping_guarantee():
    """Invariant 11, swept over every ordered pair of severities. The guarantee is that
    nothing raises a grade, so every pair where the counted rank exceeds the lens's must
    be reported."""
    from harness.grading import RANK
    for lens_sev in SEVERITIES:
        for counted in SEVERITIES:
            rep = _clean_report(findings=[_finding(severity=lens_sev,
                                                   counted_severity=counted)])
            unmet = guarantees.derive(rep, _target_set()).unmet
            promoted = RANK[counted] > RANK[lens_sev]
            assert ("SEVERITY_ONLY_CAPPED" in unmet) is promoted, (lens_sev, counted)


# --------------------------------------------------------------------------- #
# 7. THE INVARIANT LEDGER — adding an invariant forces a decision
# --------------------------------------------------------------------------- #
# Each of `CLAUDE.md`'s immutable invariants is either reportable per review (it maps to a
# guarantee key) or explicitly not (with the reason). Maintained HERE rather than in
# CLAUDE.md so the decision is checked by the suite rather than described in prose.
INVARIANT_DISPOSITION: dict[int, str] = {
    1: "EVERY_EVIDENCE_POINTER_RE_VERIFIED",
    2: "EVERY_EVIDENCE_POINTER_RE_VERIFIED",
    3: "PROVENANCE_CEILING_HELD",
    4: "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION",
    5: "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION",
    6: "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED",
    7: "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED",
    8: "NO_MODEL_WROTE_THE_DECISION",
    9: "NO_EXPERIMENT_DOWNSCALED",
    10: "not reportable: a source-level property (no paper-specific logic exists), "
        "asserted by signature in tests/test_reasoning_architecture.py and unobservable "
        "in any single review's artifacts",
    11: "SEVERITY_ONLY_CAPPED",
    12: "not reportable: the three-questions non-collapse is a property of the module "
        "graph, not of a run; a per-review boolean for it would be a tautology",
    13: "not reportable here: corpus conservation is asserted in harness/corpus.py over "
        "a BATCH, and this artifact is per paper",
    14: "not reportable here: the retry budget is spent inside harness/failures.py and "
        "leaves no field on an EvalReport",
    15: "EVERY_UNRESOLVED_STATE_NAMED",
    16: "EVERY_UNRESOLVED_STATE_NAMED",
    17: "not reportable as a guarantee: it is the rule this whole module obeys - "
        "unmet vs non_guarantees exists so a shut gate is never a defect. Asserted by "
        "test_a_closed_gate_can_never_produce_an_unmet_guarantee",
    18: "not reportable: parse_metric's tier preference is exercised by the reconciler "
        "and leaves no per-review field once a metric is bound or refused",
    19: "not reportable as a guarantee, obeyed as one: this section is itself bounded, "
        "asserted by test_the_section_is_bounded_by_construction_and_says_what_it_dropped",
    20: "not reportable: the three axes are derived properties, so a boolean saying they "
        "did not collapse would be a restatement of taxonomy.py",
    21: "ACCOUNTING_REPRODUCIBLE",
    22: "not reportable: a necessity decision is not evidence, which is a fact about the "
        "axes rather than a mechanism a review can fail to apply",
    23: "not reportable here: conditional escalation is counted in CaseLedger.efficiency, "
        "which ACCOUNTING_REPRODUCIBLE reports the existence of and not the content of",
    # --- added with the four-row outcome model, the integrity layer and coverage -------
    24: "not reportable: the disjointness of the four reader-facing rows is a property of "
        "harness/outcome.py's signatures, asserted by sweep in "
        "tests/test_architecture_guarantees.py. A per-review boolean saying the columns "
        "did not leak into each other would be a restatement of the signature",
    25: "not reportable: whether an inconclusive execution was WORDED correctly is a "
        "property of outcome.EXECUTION_GLOSS and of outcome.clip, not of a run. What a "
        "review can report is the execution state itself, which it does, in the row",
    26: "not reportable as a guarantee: a document observation gating nothing is enforced "
        "by field absence and parameter absence, so a review cannot fail to apply it. "
        "The observation COUNTS are reported in the scope section, split by whose "
        "property each one is",
    27: "not reportable as a guarantee: that the coverage denominator comes from the "
        "PAPER rather than from our own object list is enforced by coverage.surface's "
        "signature, so a review cannot fail to apply it. What a review reports instead "
        "is the coverage itself, plus off_surface (a per-review defect channel) and "
        "semantic_coverage. SURFACE_FULLY_EXAMINED already carries the conditional half",
    28: "not reportable as a guarantee: this invariant IS this module. A boolean saying "
        "the guarantees were checked from artifacts would be checked from nothing",
    29: "not reportable here: a batch's paper count is checked by harness/preflight.py "
        "BEFORE the pipeline runs, and this artifact is per paper and per run. A "
        "preflight refusal means no review exists to carry a guarantee",
}


def _claude_md_invariants() -> list[int]:
    text = (HARNESS_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    section = text.split("## Immutable invariants", 1)[1].split("\n## ", 1)[0]
    return [int(m.group(1)) for m in re.finditer(r"(?m)^(\d+)\. ", section)]


def test_a_new_invariant_without_a_reportability_decision_fails_the_suite():
    """Prevents the drift this module was built to stop, at the source. An invariant added
    to CLAUDE.md with no decision about whether a review can report it would leave the
    guarantee list quietly incomplete — and an incomplete list of guarantees reads as a
    complete one."""
    numbered = _claude_md_invariants()
    assert numbered == list(range(1, len(numbered) + 1)), numbered
    assert set(numbered) == set(INVARIANT_DISPOSITION), (
        set(numbered) ^ set(INVARIANT_DISPOSITION))
    for number, disposition in INVARIANT_DISPOSITION.items():
        if disposition.startswith("not reportable"):
            assert len(disposition) > 40, f"invariant {number}: give the actual reason"
        else:
            assert disposition in guarantees.PROCESS_GUARANTEES, (number, disposition)


def test_every_process_guarantee_traces_back_to_an_invariant():
    """The mirror direction: a process guarantee this system advertises that no invariant
    requires is a promise with no rule behind it."""
    claimed = {d for d in INVARIANT_DISPOSITION.values()
               if not d.startswith("not reportable")}
    assert claimed == set(guarantees.PROCESS_GUARANTEES), (
        claimed ^ set(guarantees.PROCESS_GUARANTEES))
