"""`disposition` — the one field that says what happens to the paper.

A finished review produced `verdict`, `triage`, `claim_status`, `reproduction_status` and
a `CorpusEntry.state` in which RED and GREEN both map to `completed`. None of them
answers "what do I do with this paper now", and the two facts a referee most needs kept
apart — "we established a failure" and "we could not check the central claim at all" —
both landed in GREEN with a sentence in the scope section.

These tests pin four things:

  * the IDENTITY with the binary decision. STOP_MATERIAL_FAILURE happens exactly when
    `claim_status == VERIFIED_FAILURE`, which is exactly what makes a paper RED. Two
    mechanisms that could disagree about whether a paper failed would be two verdicts,
    and invariant 8 says there is one.
  * the PRECEDENCE, including the one that is easy to get backwards: a central claim
    nobody could check outranks a list of verified concerns.
  * that a BLOCKED disposition is never an accusation and still passes the paper on.
  * **the fix this file was extended for**: a central claim this review's method
    inventory cannot investigate — `NO_ROUTE_AVAILABLE`, `COMPARISON_BLOCKED`,
    `ADDRESSING_BLOCKED`, `REPORTING_BLOCKED` — must produce `BLOCKED_METHOD`, never
    `PASS_TO_HUMAN_CLEAN`. Those four were unclassified in `BLOCKER_FOR_DISPOSITION`
    after Step 5 added the first two, and a paper whose only central target carried one
    fell through every rule to CLEAN — indistinguishable from a paper actually checked
    and found to hold. `test_a_central_claim_this_review_has_no_method_for_is_never_clean`
    and its neighbours below are the regression tests for exactly that corpus defect
    (`2024-icml-sapg` at Checkpoint B), and `test_the_totality_check_would_catch_a_future_
    omission` pins the mechanism that is now supposed to make it impossible to reintroduce.
"""
from __future__ import annotations

import itertools

import pytest

from harness import disposition as D
from harness.stages import report as report_stage


# --------------------------------------------------------------------------- #
# Totality and the identity
# --------------------------------------------------------------------------- #
def test_derive_is_total_over_its_whole_input_space():
    seen = set()
    for complete, cs, basis, blk, unres, major in itertools.product(
            (True, False),
            ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED", ""),
            D.DISPOSITION_BASIS,
            ((), ("SPECIFICATION",), ("ARTIFACT",), ("RESOURCE",), ("METHOD",),
             ("SPECIFICATION", "ARTIFACT"), ("ARTIFACT", "RESOURCE"),
             ("SPECIFICATION", "ARTIFACT", "RESOURCE", "METHOD")),
            (0, 1, 4), (0, 1, 9)):
        d, b, why = D.derive(review_complete=complete, claim_status=cs, basis=basis,
                             central_blockers=blk, central_unresolved=unres,
                             counted_major=major)
        assert d in D.PAPER_DISPOSITIONS
        assert b in D.DISPOSITION_BASIS
        assert why, "every disposition carries its own reason"
        seen.add(d)
    assert seen == set(D.PAPER_DISPOSITIONS), sorted(set(D.PAPER_DISPOSITIONS) - seen)


def test_a_stop_happens_exactly_when_a_material_failure_was_established():
    """THE IDENTITY. Swept, not spot-checked."""
    for complete, cs, blk, unres, major in itertools.product(
            (True, False),
            ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED", ""),
            ((), ("SPECIFICATION",), ("ARTIFACT",), ("RESOURCE",)), (0, 2), (0, 2)):
        d, _b, _w = D.derive(review_complete=complete, claim_status=cs,
                             central_blockers=blk, central_unresolved=unres,
                             counted_major=major)
        assert D.stops_the_paper(d) is (complete and cs == "VERIFIED_FAILURE"), (cs, d)


def test_a_stop_and_a_block_are_mutually_exclusive():
    """"We established a failure" and "we could not check it" cannot both be the answer."""
    for cs in ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED"):
        d, _b, _w = D.derive(claim_status=cs,
                             central_blockers=("SPECIFICATION", "ARTIFACT", "RESOURCE"))
        assert not (D.stops_the_paper(d) and d.startswith("BLOCKED")), d


def test_every_disposition_either_stops_the_paper_or_passes_it_on():
    for d in D.PAPER_DISPOSITIONS:
        assert D.stops_the_paper(d) or D.passes_to_human(d) or d == "NOT_REVIEWED", d


def test_a_blocked_paper_still_passes_to_a_human():
    """A limit of this review is not a rejection. Invariants 4 to 7, at the disposition."""
    for d in ("BLOCKED_SPECIFICATION", "BLOCKED_ARTIFACT", "BLOCKED_RESOURCES",
              "BLOCKED_METHOD"):
        assert D.passes_to_human(d), d
        assert not D.stops_the_paper(d), d


@pytest.mark.parametrize("d", ["BLOCKED_SPECIFICATION", "BLOCKED_ARTIFACT",
                               "BLOCKED_RESOURCES", "BLOCKED_METHOD",
                               "PASS_TO_HUMAN_UNRESOLVED", "PASS_TO_HUMAN_CLEAN"])
def test_no_non_stop_disposition_reads_as_an_accusation(d):
    reason = D._REASON[d]
    assert "established" not in reason or "not an established" in reason \
        or "no material failure" in reason or "absence of an established" in reason, reason


def test_green_is_not_described_as_correctness():
    assert "not a certificate of correctness" in D._REASON["PASS_TO_HUMAN_CLEAN"]


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #
def test_an_established_failure_outranks_every_limit_and_every_concern():
    d, b, _w = D.derive(claim_status="VERIFIED_FAILURE", basis="PAPER_ARITHMETIC",
                        central_blockers=("SPECIFICATION", "ARTIFACT", "RESOURCE"),
                        central_unresolved=7, counted_major=9)
    assert d == "STOP_MATERIAL_FAILURE" and b == "PAPER_ARITHMETIC"


def test_a_central_claim_nobody_could_check_outranks_a_list_of_concerns():
    """The precedence that was previously invisible: a blocked central claim read GREEN."""
    d, _b, _w = D.derive(central_blockers=("RESOURCE",), counted_major=5)
    assert d == "BLOCKED_RESOURCES"


@pytest.mark.parametrize("blockers,expected", [
    (("SPECIFICATION", "ARTIFACT", "RESOURCE", "METHOD"), "BLOCKED_SPECIFICATION"),
    (("ARTIFACT", "RESOURCE", "METHOD"), "BLOCKED_ARTIFACT"),
    (("RESOURCE", "METHOD"), "BLOCKED_RESOURCES"),
    (("METHOD",), "BLOCKED_METHOD"),
])
def test_blockers_are_ordered_most_about_the_paper_first(blockers, expected):
    """METHOD is least fixable by anyone but this system, so it is last: a paper with
    both an artifact gap and a method gap is reported for the artifact gap first."""
    assert D.derive(central_blockers=blockers)[0] == expected


def test_unresolved_outranks_concerns_and_concerns_outrank_clean():
    assert D.derive(central_unresolved=1, counted_major=5)[0] == "PASS_TO_HUMAN_UNRESOLVED"
    assert D.derive(counted_major=1)[0] == "PASS_TO_HUMAN_CONCERNS"
    assert D.derive()[0] == "PASS_TO_HUMAN_CLEAN"


def test_verified_support_does_not_stop_a_paper():
    assert D.derive(claim_status="VERIFIED_SUPPORT")[0] == "PASS_TO_HUMAN_CLEAN"


def test_an_unfinished_review_has_no_disposition_about_the_paper():
    assert D.derive(review_complete=False, claim_status="VERIFIED_FAILURE")[0] \
        == "NOT_REVIEWED"


# --------------------------------------------------------------------------- #
# The basis
# --------------------------------------------------------------------------- #
def test_the_basis_is_decided_by_the_provenance_ceiling_and_not_re_listed():
    assert D.basis_for(failed_target_provenance="repo_exec") == "AUTHOR_CODE_REPRODUCTION"
    assert D.basis_for(failed_target_provenance="driver") == "INDEPENDENT_REIMPLEMENTATION"
    # Step 8: a governed reconstruction is admissible now, and reads the SAME basis as
    # `driver` — both are INDEPENDENT_REIMPLEMENTATION, never AUTHOR_CODE_REPRODUCTION,
    # because neither is the authors' own checkout. This function does not distinguish
    # them further: `local_exec.reconcile` and `backends.authorize` are what additionally
    # require `ReimplementationConformance.established` before a `reimpl_exec` FAILED_
    # REPRODUCTION can even be produced to hand to this function in the first place.
    assert D.basis_for(failed_target_provenance="reimpl_exec") == "INDEPENDENT_REIMPLEMENTATION"


@pytest.mark.parametrize("refused", ["synthesized", "template", "", "  ", "REPO_EXEC",
                                     " repo_exec", "repo_exec ", "REIMPL_EXEC",
                                     " reimpl_exec", "reimpl-exec"])
def test_a_provenance_the_ceiling_refuses_can_never_become_a_basis(refused):
    assert D.basis_for(failed_target_provenance=refused) == "NONE"


def test_a_measurement_outranks_a_judgement_as_a_basis():
    assert D.basis_for(failed_target_provenance="repo_exec",
                       paper_arithmetic_failed=True) == "AUTHOR_CODE_REPRODUCTION"
    assert D.basis_for(paper_arithmetic_failed=True) == "PAPER_ARITHMETIC"
    assert D.basis_for() == "NONE"


def test_the_basis_is_none_for_every_disposition_that_is_not_a_stop():
    for blk, unres, major in itertools.product(
            ((), ("ARTIFACT",)), (0, 1), (0, 1)):
        d, b, _w = D.derive(claim_status="NOT_VERIFIED", basis="PAPER_ARITHMETIC",
                            central_blockers=blk, central_unresolved=unres,
                            counted_major=major)
        assert not D.stops_the_paper(d)
        assert b == "NONE", (d, b)


# --------------------------------------------------------------------------- #
# Blockers are read from CENTRAL targets, and extraction limits are not blockers
# --------------------------------------------------------------------------- #
def test_only_central_targets_raise_a_blocker():
    assert D.blockers_from(["ARTIFACT_BLOCKED"], ["CENTRAL"]) == ("ARTIFACT",)
    assert D.blockers_from(["ARTIFACT_BLOCKED"], ["SUPPORTING"]) == ()
    assert D.blockers_from(["ARTIFACT_BLOCKED"], ["PERIPHERAL"]) == ()


def test_identity_blocked_is_an_artifact_blocker():
    """The code exists and this review could not establish which part produces the cited
    quantity. That is a fact about what the artifact makes checkable."""
    assert D.blockers_from(["IDENTITY_BLOCKED"], ["CENTRAL"]) == ("ARTIFACT",)


# --------------------------------------------------------------------------- #
# THE FIX: a central claim this review has no method for is never clean
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ours", ["NO_ROUTE_AVAILABLE", "COMPARISON_BLOCKED",
                                  "ADDRESSING_BLOCKED", "REPORTING_BLOCKED"])
def test_a_limit_of_this_reviews_own_method_inventory_is_a_method_blocker(ours):
    """The regression this file was extended to pin. All four say the same thing in
    different words: this review had NOTHING to check the central claim against — no
    address, no unambiguous quantity, no route, or no arithmetic for the route's
    comparison. None is a fact about the paper, the artifact or this host, and none of
    them may fall through to PASS_TO_HUMAN_CLEAN the way `NO_ROUTE_AVAILABLE` and
    `COMPARISON_BLOCKED` did after Step 5 added them without registering here."""
    assert D.blockers_from([ours], ["CENTRAL"]) == ("METHOD",)
    d, _b, _w = D.derive(central_blockers=D.blockers_from([ours], ["CENTRAL"]))
    assert d == "BLOCKED_METHOD"
    assert d != "PASS_TO_HUMAN_CLEAN"


def test_a_central_claim_this_review_has_no_method_for_is_never_clean():
    """The corpus defect, reproduced directly. `2024-icml-sapg` at Checkpoint B had a
    CENTRAL target on the FOCUSED_VALIDATION_EXPERIMENT route with no arithmetic behind
    its comparison, no other blocker, no unresolved central target, and no counted
    concern — every input to `derive` except the fixed one said CLEAN."""
    blockers = D.blockers_from(["COMPARISON_BLOCKED"], ["CENTRAL"])
    d, _b, reason = D.derive(central_blockers=blockers, central_unresolved=0,
                             counted_major=0)
    assert d == "BLOCKED_METHOD"
    assert "method inventory" in reason
    assert "checked and found clean" in reason or "not checked at all" in reason


def test_a_supporting_target_with_no_method_does_not_block_the_paper():
    """The CENTRAL-only rule still applies to the new blocker. A SUPPORTING target this
    review has no method for is real information for the scope section, not a reason to
    tell a caller the paper's central claim could not be checked."""
    assert D.blockers_from(["NO_ROUTE_AVAILABLE"], ["SUPPORTING"]) == ()
    assert D.derive(central_blockers=())[0] == "PASS_TO_HUMAN_CLEAN"


def test_the_distinction_between_paper_clean_and_review_unresolved_survives_the_fix():
    """The other half of the ask: a paper genuinely investigated and found to hold must
    still read CLEAN, distinctly from one this review could not investigate at all, and
    distinctly again from one that was pursued and stayed open."""
    clean = D.derive()[0]
    method_blocked = D.derive(central_blockers=("METHOD",))[0]
    unresolved = D.derive(central_unresolved=1)[0]
    assert len({clean, method_blocked, unresolved}) == 3, (clean, method_blocked, unresolved)
    assert clean == "PASS_TO_HUMAN_CLEAN"
    assert method_blocked == "BLOCKED_METHOD"
    assert unresolved == "PASS_TO_HUMAN_UNRESOLVED"


def test_authorization_blocked_remains_the_one_deliberate_exclusion():
    """A closed gate is this run's configuration (`SH_ALLOW_REPO_EXEC=0`), not an absence
    of method: turning the gate on can resolve it with no change to this system's method
    inventory, which is what keeps it apart from BLOCKED_METHOD."""
    assert D.blockers_from(["AUTHORIZATION_BLOCKED"], ["CENTRAL"]) == ()
    assert "AUTHORIZATION_BLOCKED" in D.DELIBERATELY_UNCLASSIFIED
    assert "AUTHORIZATION_BLOCKED" not in D.BLOCKER_FOR_DISPOSITION


def test_every_target_disposition_is_classified_or_named_excluded():
    """THE MECHANISM meant to make this regression impossible to reintroduce: a future
    disposition that is never classified here must fail this assertion rather than
    silently read PASS_TO_HUMAN_CLEAN on a central target, which is exactly how
    `NO_ROUTE_AVAILABLE` and `COMPARISON_BLOCKED` shipped.

    It now covers EVERY target disposition rather than only those `artifacts` already
    calls BLOCKED. The narrower version could not see the five dispositions most likely
    to be forgotten — `BUDGET_DEFERRED`, `NOT_ATTEMPTED`, `PENDING`,
    `CITATION_VERIFIED_ONLY` and `SUPERSEDED_BY_ESTABLISHED_FAILURE` — every one of which
    produced no blocker on a central target and fell through to CLEAN, because none of
    them is in `BLOCKED_DISPOSITIONS`. Deciding that something is NOT a blocker is a
    decision, and it now has to be written down.
    """
    from harness.artifacts import BLOCKED_DISPOSITIONS, TARGET_DISPOSITIONS

    classified = set(D.BLOCKER_FOR_DISPOSITION) | set(D.DELIBERATELY_UNCLASSIFIED)
    assert classified == set(TARGET_DISPOSITIONS), (
        f"unclassified: {set(TARGET_DISPOSITIONS) - classified}")
    assert set(BLOCKED_DISPOSITIONS) <= classified, "the old contract still holds"
    assert not (set(D.BLOCKER_FOR_DISPOSITION) & set(D.DELIBERATELY_UNCLASSIFIED))
    # Every exclusion carries a REASON, not just a name: the point is that a future
    # reader can tell a considered non-blocker from an oversight.
    for name, why in D.DELIBERATELY_UNCLASSIFIED.items():
        assert why and len(why) > 30, name


# --------------------------------------------------------------------------- #
# The wiring: the report agrees with the binary decision
# --------------------------------------------------------------------------- #
def test_the_report_stage_exposes_the_disposition_fields():
    from harness.artifacts import CaseState, CorpusEntry, EvalReport

    for cls in (EvalReport, CorpusEntry, CaseState):
        fields = cls.model_fields
        assert "disposition" in fields, cls.__name__
        assert "disposition_basis" in fields, cls.__name__


def test_stop_and_red_are_the_same_condition_in_the_report_layer():
    """`overall_verdict` returns RED iff `claim_status` is VERIFIED_FAILURE; the
    disposition stops iff the same. Asserted across the shared input space rather than
    trusted to two independent readings of one rule."""
    from harness.artifacts import Finding, Reconciliation

    fatal = Finding(finding_id="f1", lens="overclaim", severity="FATAL",
                    counted_severity="FATAL", candidate_class="CONFIRMED_FINDING",
                    title="t", statement="s")
    minor = Finding(finding_id="f2", lens="protocol", severity="MINOR",
                    counted_severity="MINOR", candidate_class="CONFIRMED_FINDING",
                    title="t", statement="s")
    failed = Reconciliation(status="FAILED_REPRODUCTION", provenance="repo_exec",
                            reason="did not reproduce")

    for findings, rec in (([fatal], None), ([minor], None), ([], None),
                          ([minor], failed), ([], failed)):
        verdict, _ = report_stage.overall_verdict(findings, rec)
        status, _ = report_stage.claim_status(findings, rec)
        d, _b, _w = D.derive(claim_status=status)
        assert (verdict == "RED") == D.stops_the_paper(d), (verdict, status, d)
