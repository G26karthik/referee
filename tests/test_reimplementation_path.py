"""PATH B — what may be rebuilt, whose code actually ran, and what was actually established.

Three separations, pinned independently, because each one produces its own specific
scientific lie the moment it collapses:

  ELIGIBILITY vs INVENTION   `harness.reimplement.assess` decides only whether the paper
                             SAYS enough to rebuild its experiment. The moment it infers a
                             missing ingredient from a present one — an omitted optimizer
                             becoming Adam, an omitted dataset becoming the obvious one —
                             any number the resulting program prints is a statement about
                             the reimplementer's choices rather than about the paper, and
                             the whole value of PATH B is gone.

  WHOSE CODE RAN             `report.provenance_label` maps the internal token into the
                             reader's vocabulary and FAILS CLOSED. A PATH B script, or an
                             unrecognised token, reported as AUTHOR_REPOSITORY attributes
                             our own script's failure to the authors — the one accusation
                             this system must never make by accident.

  DECISION vs KNOWLEDGE      `report.claim_status` and `report.reproduction_status` keep
                             "checked and it held", "could not check" and "never tried"
                             apart underneath a binary colour that structurally cannot
                             carry the distinction. GREEN is not a certificate, and
                             NOT_ATTEMPTED is not NOT_VERIFIED.
"""
from __future__ import annotations

import pytest

from harness.artifacts import PaperDoc, ProbeResult, Reconciliation, Section, Table
from harness.reimplement import INGREDIENTS, assess
from harness.stages.report import (CLAIM_STATUSES, PROVENANCE_LABEL, claim_status,
                                   overall_verdict, provenance_label, reproduction_status)

# One clause per REQUIRED text ingredient, deliberately disjoint: no clause below matches
# any other ingredient's vocabulary, so dropping one drops exactly one ingredient. That is
# what makes "removed it and it was reported missing" mean something.
_CLAUSES: dict[str, str] = {
    "method": "We propose a method whose objective is defined as a sum.",
    "training": "We train with the Adam optimizer for 30 epochs at a learning rate of 1e-3.",
    "dataset": "We evaluate on the CIFAR-100 dataset.",
    "metric": "We report accuracy.",
}

_REQUIRED_TEXT_KINDS = tuple(kind for kind, required, _ in INGREDIENTS if required)
# `comparison_target` is required too but is established by a printed table, not by prose.
_REQUIRED_KINDS = _REQUIRED_TEXT_KINDS + ("comparison_target",)


def _paper(omit: str = "", table: bool = True) -> PaperDoc:
    """A paper that supplies every required ingredient, minus `omit` (and minus its
    printed table when `table` is False)."""
    text = " ".join(clause for kind, clause in _CLAUSES.items() if kind != omit)
    return PaperDoc(
        paper_id="p", title="T",
        sections=[Section(section_idx=0, title="Method", page_start=2, text=text)],
        tables=[Table(table_idx=0, page=3, caption="Table 1: main results",
                      rows=[["method", "acc"], ["ours", "91.4"]])] if table else [])


def _probe(rec: Reconciliation | None) -> ProbeResult:
    return ProbeResult(paper_id="p", provenance="repo_exec", reconciliation=rec)


# --------------------------------------------------------------------------- #
# A. Reimplementation eligibility — an ELIGIBILITY decision, never a claim
# --------------------------------------------------------------------------- #
def test_the_fixture_covers_every_required_prose_ingredient():
    """A guard on the tests below, not on the harness. `test_removing_any_required_...`
    proves a property one ingredient at a time; if a new REQUIRED ingredient is added to
    `INGREDIENTS` and no clause here supplies it, every other paper in this file silently
    becomes under-specified and those tests start passing for the wrong reason."""
    assert set(_CLAUSES) == set(_REQUIRED_TEXT_KINDS)


def test_a_fully_specified_paper_is_eligible_with_nothing_missing():
    """If a paper that states its method, training, data, metric and a printed target is
    still refused, PATH B never opens and "the authors published no code" goes back to
    ending the investigation — the exact defect `reimplement.py` exists to fix."""
    r = assess(_paper())
    assert r.established is True
    assert r.missing == []
    assert "every required ingredient" in r.reason


@pytest.mark.parametrize("kind", _REQUIRED_KINDS)
def test_removing_any_required_ingredient_refuses_and_names_that_ingredient(kind):
    """Each required ingredient is something the program cannot be written without. If
    removing one still reads as eligible, a reimplementer is sent off to build an
    experiment the paper does not describe; if it reads as ineligible without NAMING the
    gap, the refusal is an unexplained stop rather than a finding about the paper."""
    doc = _paper(table=False) if kind == "comparison_target" else _paper(omit=kind)
    r = assess(doc)
    assert r.established is False
    assert kind in r.missing, f"{kind} was removed but not reported missing: {r.missing}"
    assert kind in r.reason, "the refusal must name the gap, not merely register one"


def test_a_present_ingredient_never_supplies_an_absent_one():
    """The rule PATH B lives or dies by: no gap is filled from context. A paper stating
    its dataset and its metric plainly implies it was trained somehow — inferring the
    training procedure from that implication is how a reimplementation quietly becomes a
    new paper that resembles this one, and any number it then prints is about the
    invention rather than about the paper under audit."""
    r = assess(_paper(omit="training"))
    by_kind = {i.kind: i for i in r.ingredients}
    assert "training" in r.missing
    assert by_kind["dataset"].present and by_kind["metric"].present, \
        "the neighbours that could have been used to infer it are genuinely present"
    absent = by_kind["training"]
    assert absent.present is False
    assert absent.ref == "" and absent.quote == "", \
        "an absent ingredient must carry no locator and no quote — there is nothing to show"


def test_every_present_ingredient_carries_a_locator_a_reader_can_open():
    """`present` is a judgement about the paper, and this harness's rule is that a
    judgement no one can re-check is an assertion. A locator makes the eligibility
    decision auditable the same way `evidence_ref` makes a finding auditable."""
    doc = _paper()
    r = assess(doc)
    present = [i for i in r.ingredients if i.present]
    assert present, "the fully specified fixture must find something"
    locatable = ({f"s{s.section_idx}" for s in doc.sections} |
                 {f"T{t.table_idx}" for t in doc.tables})
    for i in present:
        assert i.ref, f"{i.kind} was reported present with no locator"
        assert i.ref in locatable, f"{i.kind} cites {i.ref!r}, which is not in this document"


def test_assess_is_pure_and_leaves_the_document_untouched():
    """`assess` runs before anything is executed and its answer decides whether a
    reimplementation is attempted at all. A non-deterministic answer would make PATH B
    open on Tuesday and shut on Wednesday for the same paper; a mutated `PaperDoc` would
    corrupt the one artifact every finding's evidence is re-verified against."""
    doc = _paper()
    before = doc.model_dump_json()
    first, second = assess(doc), assess(doc)
    assert first.model_dump() == second.model_dump()
    assert doc.model_dump_json() == before


# --------------------------------------------------------------------------- #
# B. Provenance vocabulary — never call a PATH B script the authors' code
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provenance,label", [
    ("repo_exec", "AUTHOR_REPOSITORY"),
    ("driver", "INDEPENDENT_REIMPLEMENTATION"),
    ("synthesized", "SYNTHESIZED_DIAGNOSTIC"),
    ("template", "SYNTHESIZED_DIAGNOSTIC"),
])
def test_each_provenance_reads_as_what_actually_ran(provenance, label):
    """`driver` is a human-written reimplementation the provenance ceiling admits in both
    directions — so its result stands, and the report must say a reimplementation
    produced it. Labelling it AUTHOR_REPOSITORY would report "the authors' code fails" on
    the strength of a script the operator wrote; labelling a synthesized probe as either
    of the other two would let a toy diagnostic read as a reproduction."""
    assert provenance_label(provenance) == label


def test_no_provenance_other_than_repo_exec_may_read_as_author_code():
    """Only a checkout of the authors' own repository is the authors' own code. A second
    token mapping to AUTHOR_REPOSITORY would reintroduce the misattribution one entry at
    a time, without any test above going red."""
    author = [p for p, label in PROVENANCE_LABEL.items() if label == "AUTHOR_REPOSITORY"]
    assert author == ["repo_exec"]


@pytest.mark.parametrize("junk", [
    "", "   ", "REPO_EXEC", "repo-exec", " repo_exec", "repo_exec ", "repo exec",
    "author_repository", "AUTHOR_REPOSITORY", "unknown", "Driver", "reimplementation",
    "None", "0", "\n", "🙂",
])
def test_an_unrecognised_provenance_fails_closed_to_a_diagnostic(junk):
    """Fail-closed is the whole design of this map. A provenance token it has not been
    taught about — a typo, a renamed backend, a field that arrived empty — must degrade
    to the weakest reading, because the failure mode in the other direction is reporting
    someone else's program as the authors' code and convicting them with it."""
    assert provenance_label(junk) == "SYNTHESIZED_DIAGNOSTIC"


# --------------------------------------------------------------------------- #
# C. Decision separation — a colour cannot carry a state of knowledge
# --------------------------------------------------------------------------- #
def test_no_probe_at_all_is_reported_as_never_attempted():
    """With no probe there is no experimental evidence in either direction. Any other
    answer would put experimental weight behind a verdict that has none."""
    assert reproduction_status(None) == "NOT_ATTEMPTED"


@pytest.mark.parametrize("status,expected", [
    ("RESOLVED_VERIFIED", "REPRODUCED"),
    ("FAILED_REPRODUCTION", "FAILED_REPRODUCTION"),
    ("INCONCLUSIVE", "NOT_VERIFIED"),
    ("NOT_ATTEMPTED", "NOT_ATTEMPTED"),
])
def test_reproduction_status_names_exactly_what_the_run_settled(status, expected):
    """INCONCLUSIVE → NOT_VERIFIED is the load-bearing row. A missing dataset, a shut
    gate or an 8 GiB card facing a 24 GiB demand settles nothing about the paper; reading
    any of them as a failed reproduction would convict a paper for this machine's
    limits."""
    assert reproduction_status(_probe(Reconciliation(status=status, table_ref="T0:r1:c1",
                                                     provenance="repo_exec"))) == expected


def test_never_attempted_and_attempted_but_unsettled_are_distinct_outcomes():
    """"No experiment was run" and "an experiment ran and settled nothing" are different
    facts about the audit. Collapsing them lets a report imply it tried harder than it
    did — or, in the other direction, hides that a real attempt reached a real blocker
    that a reader could go and remove."""
    ran = _probe(Reconciliation(status="INCONCLUSIVE", table_ref="T0:r1:c1",
                                failure_class="dependency_missing",
                                reason="the repository's requirements could not be built"))
    never = _probe(None)
    assert reproduction_status(ran) == "NOT_VERIFIED"
    assert reproduction_status(never) == "NOT_ATTEMPTED"
    assert reproduction_status(ran) != reproduction_status(never)


def test_a_positively_verified_claim_is_green_and_only_claim_status_says_so():
    """GREEN covers two opposite states of knowledge — "we checked and it held" and "we
    could not check" — because both are the same DECISION about the paper. That is only
    honest while `claim_status` keeps them apart underneath: a reader who sees GREEN on a
    reproduced cell and GREEN on an unexecutable paper must be able to tell which is
    which, or GREEN starts reading as a certificate of correctness."""
    verified = Reconciliation(status="RESOLVED_VERIFIED", table_ref="T0:r1:c1",
                              provenance="repo_exec")
    status, why = claim_status([], verified)
    assert status == "VERIFIED_SUPPORT" and status in CLAIM_STATUSES
    assert overall_verdict([], verified)[0] == "GREEN"
    assert why, "the state must name the ground it stands on"

    unchecked, _ = claim_status([], None)
    assert unchecked == "NOT_VERIFIED" and unchecked in CLAIM_STATUSES
    assert overall_verdict([], None)[0] == "GREEN"
    assert unchecked != status, "the same colour, two different states of knowledge"


# --------------------------------------------------------------------------- #
# D. the provenance ceiling, enforced at the VERDICT gate as well as the reconciler
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provenance", ["synthesized", "template", "nonsense", "", "  ",
                                        "SYNTHESIZED_DIAGNOSTIC", "repo-exec"])
def test_an_inadmissible_provenance_can_never_convict_a_paper(provenance):
    """A placebo must never be able to publish a RED.

    `local_exec.reconcile` already refuses to emit FAILED_REPRODUCTION for a provenance
    outside the ceiling, so in a correct system this state is unreachable — which is
    exactly why the verdict gate has to check it too. It did not, and any upstream bug or
    hand-edited `probe_results.json` carrying a `synthesized` FAILED_REPRODUCTION returned
    RED: a paper-independent control convicting a paper. The old code even recognised the
    case and printed "treat it as a harness defect" while still returning the conviction.
    """
    from harness.artifacts import Reconciliation
    from harness.stages.report import claim_status, overall_verdict

    rec = Reconciliation(status="FAILED_REPRODUCTION", provenance=provenance,
                         reason="metric did not match")
    verdict, why = overall_verdict([], rec)
    assert verdict == "GREEN", f"{provenance!r} is not entitled to reconcile a printed cell"
    assert claim_status([], rec)[0] == "NOT_VERIFIED"
    assert "harness defect" in why and "not" in why.lower()


@pytest.mark.parametrize("provenance", ["repo_exec", "driver"])
def test_an_admissible_provenance_still_convicts(provenance):
    """The guard on the test above: tightening the ceiling must not make RED unreachable
    through the one route that is meant to reach it."""
    from harness.artifacts import Reconciliation
    from harness.stages.report import claim_status, overall_verdict

    rec = Reconciliation(status="FAILED_REPRODUCTION", provenance=provenance, reason="x")
    assert overall_verdict([], rec)[0] == "RED"
    assert claim_status([], rec)[0] == "VERIFIED_FAILURE"


def test_the_two_ceilings_name_the_same_set():
    """`report.ADMISSIBLE_REPRODUCTION_PROVENANCE` and the reconciler's own check must not
    drift apart — two ceilings that disagree are one ceiling and one hole."""
    import inspect

    from harness import local_exec
    from harness.stages.report import ADMISSIBLE_REPRODUCTION_PROVENANCE

    src = inspect.getsource(local_exec.reconcile)
    assert 'spec.provenance not in ("driver", "repo_exec")' in src, \
        "the reconciler's ceiling moved; update report.ADMISSIBLE_REPRODUCTION_PROVENANCE too"
    assert set(ADMISSIBLE_REPRODUCTION_PROVENANCE) == {"driver", "repo_exec"}


# --------------------------------------------------------------------------- #
# E. GREEN may not borrow the words of evidence it does not have
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("claim_status", ["NOT_VERIFIED", "VERIFIED_FAILURE"])
def test_a_decision_without_positive_evidence_never_claims_support(claim_status):
    """The most consequential misreading this system can produce.

    Two very different papers are both GREEN: one whose printed cell an executed metric
    reconciled with, and one that nothing could be established about. Only the first has
    been verified/supported/confirmed, and the second is the overwhelmingly common case —
    so a decision block that borrows those words for it is not a rare slip, it is the
    default output being wrong in the direction that gets quoted.
    """
    from harness.artifacts import EvalReport
    from harness.stages.report import (SUPPORT_LANGUAGE, render_eval_report,
                                       unearned_support_language)

    rep = EvalReport(paper_id="p", title="T", verdict="GREEN",
                     verdict_reason="No material failure established; 3 MINOR finding(s).",
                     claim_status=claim_status, reproduction_status="NOT_ATTEMPTED",
                     execution_provenance="SYNTHESIZED_DIAGNOSTIC", lenses_run=["protocol"])
    md = render_eval_report(rep)
    decision = md[:md.index("## Critical validity threats")]
    assert unearned_support_language(decision, claim_status) == [], decision
    # and the reader is told, positively, that there is none
    assert "none" in decision.lower() and "ABSENCE of an established failure" in decision \
        or claim_status == "VERIFIED_FAILURE"
    assert set(SUPPORT_LANGUAGE) >= {"verified", "supported", "confirmed"}


def test_only_a_positively_checked_claim_may_use_the_word_verified():
    """The guard on the test above: the rule must not gag the one state that earned it."""
    from harness.artifacts import EvalReport
    from harness.stages.report import render_eval_report, support_is_evidenced

    assert support_is_evidenced("VERIFIED_SUPPORT")
    assert not support_is_evidenced("NOT_VERIFIED")
    assert not support_is_evidenced("VERIFIED_FAILURE")

    rep = EvalReport(paper_id="p", title="T", verdict="GREEN", verdict_reason="r",
                     claim_status="VERIFIED_SUPPORT", reproduction_status="REPRODUCED",
                     execution_provenance="AUTHOR_REPOSITORY", lenses_run=["protocol"])
    decision = render_eval_report(rep)
    decision = decision[:decision.index("## Critical validity threats")]
    assert "reconciled with a printed cell" in decision
    assert "REPRODUCED" in decision


def test_the_support_row_is_present_for_every_claim_status():
    """A GREEN must never appear without the row that says what stands behind it — the
    row is the answer to the one question a skimmed verdict provokes."""
    from harness.artifacts import EvalReport
    from harness.stages.report import CLAIM_STATUSES, render_eval_report

    for status in CLAIM_STATUSES:
        rep = EvalReport(paper_id="p", title="T", verdict="GREEN", verdict_reason="r",
                         claim_status=status, lenses_run=["protocol"])
        assert "**Supporting evidence**" in render_eval_report(rep), status
