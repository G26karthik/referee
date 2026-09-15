"""What happens to this paper — the one field a caller acts on.

`python -m harness.disposition` runs the self-check.

**The gap this closes.** A finished review produced `verdict` (RED | GREEN), `triage`
(RED | YELLOW | GREEN), `claim_status` (VERIFIED_FAILURE | VERIFIED_SUPPORT |
NOT_VERIFIED), `reproduction_status`, and a `CorpusEntry.state` in which RED and GREEN
both map to `completed`. Not one of them answers "what should be done with this paper
now", and a caller wanting that had to re-derive it from four fields and a set of target
outcomes. Worse, the two facts a referee most needs kept apart — "we established a
failure" and "we could not check the central claim at all" — both landed in GREEN with a
sentence in the scope section.

**This is NOT a fifth verdict and does not replace the colour.** `triage` routes a queue;
`verdict` is the binary claim decision and is unchanged; `claim_status` is the epistemic
state underneath it. `disposition` is the ACTION, folded deterministically over those and
over the target outcomes. `STOP_MATERIAL_FAILURE` is exactly `claim_status ==
VERIFIED_FAILURE`, asserted here as an identity so the two can never disagree — invariant
8 restated where a caller reads it.

**Precedence is the design.** The order below is not the order the fields happen to be
checked in; it is a claim about which fact dominates:

    1. the review did not finish              — nothing else is knowable
    2. a material failure was established     — the strongest thing a review can say
    3. a CENTRAL question could not be checked — a limit, and a referee must know it
    4. a CENTRAL question was checked and stayed open
    5. a concern was verified but rejects nothing
    6. none of the above, within the audited scope

Rule 3 outranks 4 and 5 because "we could not check the paper's central claim" is
information a referee needs BEFORE a list of minor concerns, and until now it was invisible
in the colour: `unresolved_central` requires admissible provenance, so a paper whose
central claim was never checkable at all reads GREEN.

**A second version of that exact defect was found at Checkpoint B, and this file is the
fix.** Question-centric routing (Step 5) introduced `NO_ROUTE_AVAILABLE` and
`COMPARISON_BLOCKED` — a central claim this review's method inventory has no way to
investigate, addressed and quantified though it may be. Neither disposition was in
`BLOCKER_FOR_DISPOSITION`, so a paper whose ONLY central target carried one of them
produced `central_blockers=()`, and fell straight through rule 3 to
`PASS_TO_HUMAN_CLEAN` — the SAME defect rule 3 exists to catch, wearing a new
disposition string. `2024-icml-sapg` read exactly this way at Checkpoint B: a central
claim on the FOCUSED_VALIDATION_EXPERIMENT route this system cannot yet arithmetically
resolve, and the paper came out indistinguishable from one whose central claim was
checked and held.

**`BLOCKED_METHOD` is rule 3's fourth blocker, and it says something none of the other
three do: this is a limit of THIS REVIEW, not of the paper, the artifact, or this host.**
`SPECIFICATION` is the paper's method section; `ARTIFACT` is the released code; `RESOURCE`
is this runner's hardware. `METHOD` covers every target disposition where this review had
NOTHING to check the central claim against — no address it could build
(`ADDRESSING_BLOCKED`), no unambiguous quantity to compare against
(`REPORTING_BLOCKED`), no verification route it has implemented (`NO_ROUTE_AVAILABLE`), or
a route with no arithmetic behind its comparison (`COMPARISON_BLOCKED`). All four are
symptoms of one fact: this review's method inventory ran out, and a bigger machine, a
released repository or a more detailed method section would not fix it — only extending
this system would. It is placed LAST in precedence, after RESOURCE, for exactly that
reason: unlike a missing GPU, which someone else could supply, a missing method is this
review's own gap to close.

**None of the four BLOCKED values is an accusation**, and the reasons say so in those
words. They are facts about the artifact, the paper's specification, this host, or this
review's own method inventory — the same distinction invariants 4 to 7 draw everywhere
else.
"""
from __future__ import annotations

from .provenance import admits as _admits

# What happens to the paper. Closed, and ordered by the precedence above.
PAPER_DISPOSITIONS: tuple[str, ...] = (
    "STOP_MATERIAL_FAILURE",       # a material failure was established; see `basis`
    "BLOCKED_SPECIFICATION",       # a central question needed detail the paper omits
    "BLOCKED_ARTIFACT",            # a central question needed code that is absent or unbindable
    "BLOCKED_RESOURCES",           # a central question needed hardware not obtainable here
    # A central question needed a verification approach this system does not implement —
    # no address, no printed quantity to compare against, no route, or a route with no
    # arithmetic behind its comparison. Never the paper's fault, never the artifact's,
    # never the host's: this review's own method inventory ran out.
    "BLOCKED_METHOD",
    "PASS_TO_HUMAN_UNRESOLVED",    # a central question was pursued admissibly and stayed open
    "PASS_TO_HUMAN_CONCERNS",      # verified concerns that weaken a claim without rejecting it
    "PASS_TO_HUMAN_CLEAN",         # nothing of the above, within the audited scope
    "NOT_REVIEWED",                # the pipeline did not complete
)

# ON WHAT a material failure was established. Never a severity and never a colour: this
# says which KIND of evidence carried the decision, because "four lenses agreed and the
# blinded grader sustained it" and "the authors' own code did not produce the number" are
# different things to hand a referee.
DISPOSITION_BASIS: tuple[str, ...] = (
    "AUTHOR_CODE_REPRODUCTION",      # the authors' own checkout, at a verified commit
    "INDEPENDENT_REIMPLEMENTATION",  # a reproduction the ceiling admits that is NOT theirs
    "PAPER_ARITHMETIC",              # the paper's own printed composition does not evaluate
    "NONE",
)

# Which blocker dominates when a paper has several. MOST-ABOUT-THE-PAPER FIRST, which is
# the opposite of most-fixable-first and is deliberate: a referee acts on the paper in
# front of them, and "the paper does not specify this" is theirs to raise while "this host
# has no GPU" is ours to solve. METHOD is placed LAST and is the least fixable of the
# four: a missing GPU is someone else's to supply and a missing artifact is the authors'
# to release, but a missing verification method is this review's own gap, closable by
# nobody but this system being extended.
_BLOCKER_PRECEDENCE: tuple[tuple[str, str], ...] = (
    ("SPECIFICATION", "BLOCKED_SPECIFICATION"),
    ("ARTIFACT", "BLOCKED_ARTIFACT"),
    ("RESOURCE", "BLOCKED_RESOURCES"),
    ("METHOD", "BLOCKED_METHOD"),
)

# Target dispositions that BLOCK a central question, mapped to the blocker class above.
# `IDENTITY_BLOCKED` is an ARTIFACT blocker: the code exists and this review could not
# establish which part of it produces the cited quantity, which is a fact about what the
# artifact makes checkable.
#
# The four METHOD entries are the fix for the defect this module's docstring describes:
# each says this review had NOTHING to check the central claim against, for a different
# reason, and none of the four reasons is the paper's, the artifact's or this host's.
# `ADDRESSING_BLOCKED` (no address this review could build) and `REPORTING_BLOCKED` (no
# unambiguous quantity to compare against) used to be absent from this table on the theory
# that naming them a blocker would misattribute an extraction or reporting gap to the
# paper — but BLOCKED_METHOD's own reason text says the opposite: it names the gap as
# THIS REVIEW's, which is the fix, not the return of the mistake the exclusion was meant
# to prevent.
BLOCKER_FOR_DISPOSITION: dict[str, str] = {
    "SPECIFICATION_BLOCKED": "SPECIFICATION",
    "ARTIFACT_BLOCKED": "ARTIFACT",
    "IDENTITY_BLOCKED": "ARTIFACT",
    "RESOURCE_BLOCKED": "RESOURCE",
    "ENVIRONMENT_BLOCKED": "RESOURCE",
    "ADDRESSING_BLOCKED": "METHOD",
    "REPORTING_BLOCKED": "METHOD",
    "NO_ROUTE_AVAILABLE": "METHOD",
    "COMPARISON_BLOCKED": "METHOD",
}

# Target dispositions that are DELIBERATELY absent from the table above, and why. Every
# value in `artifacts.BLOCKED_DISPOSITIONS` must appear either in `BLOCKER_FOR_DISPOSITION`
# or here — the self-check asserts it — so a disposition added to that vocabulary and never
# classified fails the suite instead of silently reading PASS_TO_HUMAN_CLEAN on a central
# target, which is exactly how `NO_ROUTE_AVAILABLE` and `COMPARISON_BLOCKED` shipped
# unclassified after Step 5.
DELIBERATELY_UNCLASSIFIED: dict[str, str] = {
    "AUTHORIZATION_BLOCKED":
        "a gate in THIS RUN refused — by configuration, not by an absence of method. "
        "SH_ALLOW_REPO_EXEC=0 blocks a central AUTHOR_CODE_EXECUTION target exactly as "
        "hard as a missing repository does, but turning the gate on can resolve it with "
        "no change to this review's method inventory, which is what distinguishes "
        "'we were not permitted to check this run' from BLOCKED_METHOD's 'this review has "
        "no way to check this at all'.",

    # --- SETTLED, or on their way to being settled. Not blockers by construction. -------
    "REPRODUCED": "the target settled; there is nothing blocked about it.",
    "FAILED_REPRODUCTION": "the target settled, against the paper. Tier 1 and the "
                           "materiality gate decide what follows, not this table.",
    "PAPER_ARITHMETIC_CONTRADICTION": "as FAILED_REPRODUCTION, by the paper-internal route.",
    "PAPER_ONLY_RESOLVED": "the target settled from the paper's own printed content.",
    "INCONCLUSIVE": "pursued and settled nothing. Handled by `unresolved_central`, which "
                    "additionally requires an ADMISSIBLE provenance — a synthesized probe's "
                    "INCONCLUSIVE says nothing about the paper's checkability and must not "
                    "colour it. Making this a blocker here would restore exactly that.",

    # --- NOT a blocker: this review did not finish, which is not the paper's fault ------
    # None of these four says anything was in the way. They say the review stopped, chose
    # not to spend, or settled something smaller. They are reported in the scope row
    # (`outcome.scope_state`) and by `unchecked_central`, which is where "how much of this
    # paper went unchecked" belongs — deliberately NOT in the colour, because a budget, a
    # phase that did not run and a per-run cap are all properties of this harness's
    # configuration, and invariant 17 forbids those from colouring a paper.
    "NOT_ATTEMPTED": "no experiment was judged necessary, or none was started this run.",
    "PENDING": "the probe phase did not complete for this target; an incomplete review is "
               "not a blocked one, and `corpus.py` accounts for the paper's own state.",
    "BUDGET_DEFERRED": "this run's per-paper target budget did not reach it. A budget is "
                       "ours, so it may not read as a fact about the paper; the honest fix "
                       "is to spend more or to exhaust routes for material questions, not "
                       "to report our own cap as a blocker.",
    "SUPERSEDED_BY_ESTABLISHED_FAILURE":
        "the paper already stopped on an established material failure, so "
        "STOP_MATERIAL_FAILURE outranks everything here anyway.",
    "CITATION_VERIFIED_ONLY":
        "the quotation behind a concern was re-verified, which settles nothing about "
        "whether the concern is right. Its question stays open and is counted as open; "
        "calling it a blocker would say a route was unavailable when one was taken.",
    "NO_EXPERIMENT_NEEDED": "declining to run something is a successful review outcome on "
                            "the necessity axis and resolves nothing on the evidence axis.",
}

_REASON = {
    "STOP_MATERIAL_FAILURE":
        "a material failure was established, so there is nothing further to check: the "
        "paper's central claim does not stand on the evidence examined.",
    "BLOCKED_SPECIFICATION":
        "a central claim could not be checked because the paper does not specify enough "
        "to build the experiment that would settle it. That is a limit of what was "
        "reported, not an established defect, and a referee should read it as an open "
        "question about the paper's method section.",
    "BLOCKED_ARTIFACT":
        "a central claim could not be checked because no released code reached it, or "
        "because this review could not establish which part of the released code produces "
        "the quantity cited. Nothing about the paper's correctness follows from that.",
    "BLOCKED_RESOURCES":
        "a central claim could not be checked because the experiment it needs cannot be "
        "hosted from here. That is a fact about this runner and never about the paper.",
    "BLOCKED_METHOD":
        "a central claim could not be checked because this review has no verification "
        "approach for it — no address it could build, no unambiguous quantity to compare "
        "against, no route it has implemented, or a route with no arithmetic behind its "
        "comparison. That is a limit of this system's own method inventory. It is never a "
        "finding about the paper, and it must never be read as the paper having been "
        "checked and found clean: it was not checked at all.",
    "PASS_TO_HUMAN_UNRESOLVED":
        "a central claim was pursued with evidence entitled to settle it and remained "
        "open. The question is handed over as a question.",
    "PASS_TO_HUMAN_CONCERNS":
        "no material failure was established. Verified concerns were recorded, and a "
        "concern weakens a claim rather than rejecting one, so they are handed over for a "
        "human to weigh.",
    "PASS_TO_HUMAN_CLEAN":
        "no material failure, no unresolved central claim and no verified major concern "
        "within the audited scope. This is the absence of an established problem in what "
        "was checked, and it is not a certificate of correctness.",
    "NOT_REVIEWED":
        "the review did not reach a report, so no disposition about the paper is "
        "available. This is a fact about the run.",
}

# The SAME disposition as a counted-MAJOR hand-over, and a different sentence, because the
# two triggers are different evidence. A counted MAJOR is a lens's asserted severity after
# every cap; this is something the harness itself established and then found not to rest
# under a central claim. Held beside `_REASON` rather than inside it, because `_REASON` is
# keyed by disposition and its self-check asserts that mapping is exactly one per value.
_ESTABLISHED_NON_MATERIAL_REASON = (
    "no material failure was established, and this review did establish a defect: it sits "
    "on a target no central scientific claim was established to depend on, so it weakens "
    "the paper rather than rejecting it. It is handed over for a human to weigh, and it is "
    "reported in full under `## Established failures`. This is NOT a clean paper.")

_BASIS_REASON = {
    "AUTHOR_CODE_REPRODUCTION":
        "the authors' own checkout, at a verified commit, did not produce the value the "
        "paper prints",
    "INDEPENDENT_REIMPLEMENTATION":
        "a reproduction the provenance ceiling admits, built independently of the authors' "
        "code, did not produce the value the paper prints. This is evidence about the "
        "paper's STATED METHOD and is never a statement about the authors' implementation",
    "PAPER_ARITHMETIC":
        "the paper's own printed composition does not evaluate to the total it states",
    "NONE": "",
}


def basis_for(*, failed_target_provenance: str = "",
              paper_arithmetic_failed: bool = False) -> str:
    """WHICH evidence carried a material failure. Ordered strongest-evidence-first.

    Model-assigned severity is deliberately absent: only deterministic arithmetic or an
    admissible execution may carry a paper-level material-failure disposition.
    """
    if _admits(failed_target_provenance):
        return ("AUTHOR_CODE_REPRODUCTION" if failed_target_provenance == "repo_exec"
                else "INDEPENDENT_REIMPLEMENTATION")
    if paper_arithmetic_failed:
        return "PAPER_ARITHMETIC"
    return "NONE"


def blockers_from(dispositions, centralities) -> tuple[str, ...]:
    """The blocker classes raised by CENTRAL targets only, in precedence order.

    `centralities` is positional against `dispositions`. A SUPPORTING target that was
    blocked is real information and belongs in the report's scope section; it is not a
    reason to tell a caller the paper could not be checked, because the paper's conclusion
    does not rest on it.
    """
    raised = set()
    for disp, centrality in zip(dispositions, centralities):
        if (centrality or "").strip().upper() != "CENTRAL":
            continue
        cls = BLOCKER_FOR_DISPOSITION.get((disp or "").strip().upper())
        if cls:
            raised.add(cls)
    return tuple(cls for cls, _ in _BLOCKER_PRECEDENCE if cls in raised)


def derive(*, review_complete: bool = True, claim_status: str = "NOT_VERIFIED",
           basis: str = "NONE", central_blockers: tuple[str, ...] = (),
           central_unresolved: int = 0, counted_major: int = 0,
           established_non_material: int = 0) -> tuple[str, str, str]:
    """(disposition, basis, reason). Total over its inputs and deterministic.

    Vocabulary strings, booleans and counts only — no paper identity, no metric name, no
    printed number, so a paper-specific or threshold-shaped rule is inexpressible here for
    the same reason it is in `grading.derive` and `priority.score`. The counts are counts
    of THIS REVIEW's own objects, never quantities read out of the paper.

    **`established_non_material` closes a defect in CLEAN.** A target whose own evidence
    route ESTABLISHED a defect (Tier 1, `TargetOutcome.establishes_failure`) on a target
    no central claim was established to depend on (Tier 2 failed, `harness.materiality`)
    used to reach no rule here at all and fell through to PASS_TO_HUMAN_CLEAN. So a paper
    containing a deterministically proven arithmetic contradiction was handed to a human
    as "clean" — the same shape of defect as the NO_ROUTE_AVAILABLE fall-through above,
    and worse, because here the system had PROVED something was wrong and then said
    nothing was. CLEAN must never mean "we established something objectively wrong and it
    was not central enough to reject".

    It reuses PASS_TO_HUMAN_CONCERNS rather than adding a disposition, because that state
    already means exactly this: no material failure was established, something real was
    recorded, a concern weakens a claim rather than rejecting one, and a human should
    weigh it. What was narrow was its only TRIGGER (`counted_major`), not its meaning — so
    it gains a second, separately-counted trigger instead of having the first overloaded.
    A model-asserted MAJOR and a deterministically established defect must stay countable
    apart.

    PRECEDENCE, and why an established defect sits above an unresolved question: it is
    something this review PROVED about the paper, and an unresolved central claim is a
    question it could not close. Proven outranks open; open outranks asserted. The
    existing order between those last two (`central_unresolved` above `counted_major`) is
    unchanged, and every BLOCKED_* stays above all of them.
    """
    if not review_complete:
        return "NOT_REVIEWED", "NONE", _REASON["NOT_REVIEWED"]

    # THE IDENTITY WITH THE BINARY DECISION. `claim_status == VERIFIED_FAILURE` is exactly
    # what makes a paper RED (`stages.report.overall_verdict`), so it is exactly what
    # stops one here. Two mechanisms that could disagree about whether a paper failed
    # would be two verdicts, and invariant 8 says there is one.
    if (claim_status or "").strip().upper() == "VERIFIED_FAILURE":
        why = _BASIS_REASON.get(basis, "")
        return ("STOP_MATERIAL_FAILURE",
                basis if basis in DISPOSITION_BASIS else "NONE",
                _REASON["STOP_MATERIAL_FAILURE"] + (f" Established by: {why}." if why else ""))

    for cls, disposition in _BLOCKER_PRECEDENCE:
        if cls in central_blockers:
            return disposition, "NONE", _REASON[disposition]

    # PROVEN outranks OPEN. See the docstring: a defect this review established is a fact
    # about the paper, and it may not be reported as a clean paper because the target it
    # sits on is not one a central claim was established to rest on.
    if established_non_material > 0:
        return "PASS_TO_HUMAN_CONCERNS", "NONE", _ESTABLISHED_NON_MATERIAL_REASON
    if central_unresolved > 0:
        return "PASS_TO_HUMAN_UNRESOLVED", "NONE", _REASON["PASS_TO_HUMAN_UNRESOLVED"]
    if counted_major > 0:
        return "PASS_TO_HUMAN_CONCERNS", "NONE", _REASON["PASS_TO_HUMAN_CONCERNS"]
    return "PASS_TO_HUMAN_CLEAN", "NONE", _REASON["PASS_TO_HUMAN_CLEAN"]


def stops_the_paper(disposition: str = "") -> bool:
    """Is this the disposition that ends the investigation? Exactly one value is."""
    return (disposition or "") == "STOP_MATERIAL_FAILURE"


def passes_to_human(disposition: str = "") -> bool:
    """May this paper go to a human referee as a reviewable submission?

    Every disposition except a stop and a non-review does — including the BLOCKED ones,
    which are limits of this review rather than findings about the paper. A caller that
    treated a blocked paper as rejected would be doing exactly what invariants 4 to 7
    forbid, so the predicate says so rather than leaving it to be inferred.
    """
    return (disposition or "") in (
        "BLOCKED_SPECIFICATION", "BLOCKED_ARTIFACT", "BLOCKED_RESOURCES", "BLOCKED_METHOD",
        "PASS_TO_HUMAN_UNRESOLVED", "PASS_TO_HUMAN_CONCERNS", "PASS_TO_HUMAN_CLEAN")


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect
    import itertools

    assert set(_REASON) == set(PAPER_DISPOSITIONS), "every disposition needs a sentence"
    assert set(_BASIS_REASON) == set(DISPOSITION_BASIS)
    assert set(d for _, d in _BLOCKER_PRECEDENCE) <= set(PAPER_DISPOSITIONS)
    assert set(BLOCKER_FOR_DISPOSITION.values()) == {c for c, _ in _BLOCKER_PRECEDENCE}

    # --- TOTALITY: every blocked target disposition is classified or named as excluded ---
    # THE DEFECT THIS CLOSES. `NO_ROUTE_AVAILABLE` and `COMPARISON_BLOCKED` were added to
    # `artifacts.BLOCKED_DISPOSITIONS` and never added here, so a paper whose only central
    # target carried either one fell through every rule below to PASS_TO_HUMAN_CLEAN — a
    # central claim this review never investigated, indistinguishable from one it checked
    # and found nothing wrong with. Deferred import: `artifacts` imports THIS module, so
    # importing it at module load time would be circular; by the time this function runs,
    # both modules have already finished loading.
    from . import artifacts as _artifacts

    classified = set(BLOCKER_FOR_DISPOSITION) | set(DELIBERATELY_UNCLASSIFIED)
    # Asserted over EVERY target disposition, not only the ones `artifacts` already calls
    # BLOCKED. Restricting it to `BLOCKED_DISPOSITIONS` meant the check could not see the
    # dispositions most likely to be forgotten: `BUDGET_DEFERRED`, `NOT_ATTEMPTED`,
    # `PENDING`, `CITATION_VERIFIED_ONLY` and `SUPERSEDED_BY_ESTABLISHED_FAILURE` all
    # produced NO blocker on a central target and fell through to PASS_TO_HUMAN_CLEAN,
    # and the mechanism written to catch exactly that was blind to them because none is in
    # that tuple. Every one of the 21 must now be a blocker or carry a written reason why
    # it is not, so adding a disposition and forgetting it fails the suite.
    assert classified == set(_artifacts.TARGET_DISPOSITIONS), (
        f"unclassified: {set(_artifacts.TARGET_DISPOSITIONS) - classified} — every target "
        f"disposition must be a blocker here or a named, justified exclusion above; "
        f"not-a-blocker is a decision and has to be written down")
    assert set(_artifacts.BLOCKED_DISPOSITIONS) <= classified
    assert not (set(BLOCKER_FOR_DISPOSITION) & set(DELIBERATELY_UNCLASSIFIED)), (
        "a disposition cannot be both classified and excluded")

    # --- the signature admits no paper ------------------------------------------------
    for name, p in inspect.signature(derive).parameters.items():
        assert str(p.annotation) in ("str", "bool", "int", "tuple[str, ...]"), (name, p.annotation)

    # --- total and deterministic over the whole reachable input space -----------------
    seen = set()
    for complete, cs, basis, blk, unres, major in itertools.product(
            (True, False), ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED", ""),
            DISPOSITION_BASIS, ((), ("SPECIFICATION",), ("ARTIFACT",), ("RESOURCE",),
                                ("METHOD",),
                                ("SPECIFICATION", "ARTIFACT", "RESOURCE", "METHOD")),
            (0, 1), (0, 3)):
        d, b, why = derive(review_complete=complete, claim_status=cs, basis=basis,
                           central_blockers=blk, central_unresolved=unres,
                           counted_major=major)
        assert d in PAPER_DISPOSITIONS and b in DISPOSITION_BASIS and why, (d, b)
        assert derive(review_complete=complete, claim_status=cs, basis=basis,
                      central_blockers=blk, central_unresolved=unres,
                      counted_major=major) == (d, b, why), "derive must be a function"
        seen.add(d)
        # THE IDENTITY: a stop happens exactly when a material failure was established.
        assert stops_the_paper(d) == (complete and cs == "VERIFIED_FAILURE"), (d, cs)
        # A stop and a block are mutually exclusive: "we established a failure" and "we
        # could not check it" cannot both be the answer.
        assert not (stops_the_paper(d) and d.startswith("BLOCKED")), d
        assert stops_the_paper(d) or passes_to_human(d) or d == "NOT_REVIEWED", d
    assert seen == set(PAPER_DISPOSITIONS), sorted(set(PAPER_DISPOSITIONS) - seen)

    # --- precedence, stated as cases --------------------------------------------------
    assert derive(claim_status="VERIFIED_FAILURE", basis="PAPER_ARITHMETIC",
                  central_blockers=("SPECIFICATION",), counted_major=9)[0] \
        == "STOP_MATERIAL_FAILURE", "an established failure outranks every limit"
    assert derive(central_blockers=("SPECIFICATION", "ARTIFACT", "RESOURCE", "METHOD"))[0] \
        == "BLOCKED_SPECIFICATION", "most-about-the-paper first"
    assert derive(central_blockers=("ARTIFACT", "RESOURCE", "METHOD"))[0] == "BLOCKED_ARTIFACT"
    assert derive(central_blockers=("RESOURCE", "METHOD"))[0] == "BLOCKED_RESOURCES"
    assert derive(central_blockers=("METHOD",), counted_major=5)[0] == "BLOCKED_METHOD", (
        "a central claim nobody could check outranks a list of concerns, even when the "
        "reason is that this review has no method for it"
    )
    assert derive(central_blockers=("RESOURCE",), counted_major=5)[0] == "BLOCKED_RESOURCES", (
        "a central claim nobody could check outranks a list of concerns")
    assert derive(central_unresolved=1, counted_major=5)[0] == "PASS_TO_HUMAN_UNRESOLVED"
    assert derive(counted_major=1)[0] == "PASS_TO_HUMAN_CONCERNS"
    assert derive()[0] == "PASS_TO_HUMAN_CLEAN"

    # --- AN ESTABLISHED DEFECT IS NEVER CLEAN -----------------------------------------
    # The defect this parameter closes: Tier 1 held, Tier 2 did not, no rule matched, and
    # a paper with a proven arithmetic contradiction in it was handed over as clean.
    d, b, why = derive(established_non_material=1)
    assert d == "PASS_TO_HUMAN_CONCERNS" and b == "NONE"
    assert "did establish a defect" in why and "NOT a clean paper" in why
    # PROVEN outranks OPEN outranks ASSERTED, and the last two keep their old order.
    assert derive(established_non_material=1, central_unresolved=9,
                  counted_major=9)[0] == "PASS_TO_HUMAN_CONCERNS"
    assert derive(central_unresolved=1, counted_major=9)[0] == "PASS_TO_HUMAN_UNRESOLVED"
    # …and every BLOCKED_* still outranks all three.
    for cls, disposition in _BLOCKER_PRECEDENCE:
        assert derive(central_blockers=(cls,), established_non_material=3,
                      central_unresolved=3, counted_major=3)[0] == disposition, cls
    # …and a MATERIAL failure still outranks everything below NOT_REVIEWED.
    assert derive(claim_status="VERIFIED_FAILURE", established_non_material=3,
                  central_blockers=("ARTIFACT",))[0] == "STOP_MATERIAL_FAILURE"
    assert derive(review_complete=False, established_non_material=3)[0] == "NOT_REVIEWED"

    # THE INVARIANT, swept: CLEAN implies nothing was established, nothing is open, and
    # nothing was blocked. Not spot-checked — every reachable combination.
    for est, unres, major in itertools.product((0, 1, 4), repeat=3):
        for blockers in ((), ("ARTIFACT",), ("SPECIFICATION", "METHOD")):
            for cs in ("NOT_VERIFIED", "VERIFIED_SUPPORT", "VERIFIED_FAILURE"):
                got, _, _ = derive(claim_status=cs, central_blockers=blockers,
                                   established_non_material=est, central_unresolved=unres,
                                   counted_major=major)
                if got == "PASS_TO_HUMAN_CLEAN":
                    assert est == 0 and unres == 0 and major == 0 and not blockers, (
                        cs, blockers, est, unres, major)
                    assert cs != "VERIFIED_FAILURE"
                if est > 0 and cs != "VERIFIED_FAILURE" and not blockers:
                    assert got == "PASS_TO_HUMAN_CONCERNS", (cs, est, unres, major)
    assert derive(review_complete=False, claim_status="VERIFIED_FAILURE")[0] == "NOT_REVIEWED"

    # --- THE FIX ITSELF: the two dispositions that used to fall through to CLEAN --------
    assert blockers_from(["NO_ROUTE_AVAILABLE"], ["CENTRAL"]) == ("METHOD",), (
        "this is the exact defect: a central claim with no admissible verification route "
        "used to raise no blocker at all and read PASS_TO_HUMAN_CLEAN")
    assert blockers_from(["COMPARISON_BLOCKED"], ["CENTRAL"]) == ("METHOD",)
    assert derive(central_blockers=blockers_from(["NO_ROUTE_AVAILABLE"], ["CENTRAL"]))[0] \
        == "BLOCKED_METHOD"
    assert derive(central_blockers=blockers_from(["COMPARISON_BLOCKED"], ["CENTRAL"]))[0] \
        == "BLOCKED_METHOD"
    # and the distinction the fix must preserve: a paper genuinely checked and clean is
    # still reachable and still reads differently from one this review could not check.
    assert derive()[0] == "PASS_TO_HUMAN_CLEAN"
    assert derive()[0] != derive(central_blockers=("METHOD",))[0]

    # --- VERIFIED_SUPPORT does not stop a paper ---------------------------------------
    assert derive(claim_status="VERIFIED_SUPPORT")[0] == "PASS_TO_HUMAN_CLEAN", (
        "something checked and held is not a reason to stop")

    # --- basis: the ceiling decides, and is not re-listed here ------------------------
    assert basis_for(failed_target_provenance="repo_exec") == "AUTHOR_CODE_REPRODUCTION"
    assert basis_for(failed_target_provenance="driver") == "INDEPENDENT_REIMPLEMENTATION"
    for refused in ("synthesized", "template", "", "  ", "REPO_EXEC", " repo_exec"):
        assert basis_for(failed_target_provenance=refused) == "NONE", refused
        assert basis_for(failed_target_provenance=refused) == "NONE"
    assert basis_for(paper_arithmetic_failed=True) == "PAPER_ARITHMETIC"
    # a measurement outranks a judgement
    assert basis_for(failed_target_provenance="repo_exec",
                     paper_arithmetic_failed=True) == "AUTHOR_CODE_REPRODUCTION"

    # --- blockers are CENTRAL-only ------------------------------------------------------
    assert blockers_from(["ARTIFACT_BLOCKED"], ["CENTRAL"]) == ("ARTIFACT",)
    assert blockers_from(["ARTIFACT_BLOCKED"], ["SUPPORTING"]) == ()
    # ADDRESSING_BLOCKED and REPORTING_BLOCKED are now METHOD, not silently excluded: a
    # limit of THIS REVIEW's extraction or reporting inventory still means a central claim
    # went uninvestigated, which must never read as clean. See the module docstring.
    assert blockers_from(["ADDRESSING_BLOCKED"], ["CENTRAL"]) == ("METHOD",)
    assert blockers_from(["REPORTING_BLOCKED", "NO_ROUTE_AVAILABLE"],
                         ["CENTRAL", "CENTRAL"]) == ("METHOD",)
    assert blockers_from(["IDENTITY_BLOCKED"], ["CENTRAL"]) == ("ARTIFACT",)
    assert blockers_from(["RESOURCE_BLOCKED", "SPECIFICATION_BLOCKED"],
                         ["CENTRAL", "CENTRAL"]) == ("SPECIFICATION", "RESOURCE")
    assert blockers_from(["METHOD_BLOCKED_DOES_NOT_EXIST"], ["CENTRAL"]) == (), (
        "an unrecognised token raises nothing, exactly as invariant discipline requires "
        "elsewhere in this codebase")
    # AUTHORIZATION_BLOCKED remains the one deliberate, justified exclusion: a closed gate
    # is a configuration choice this run made, not an absence of method.
    assert blockers_from(["AUTHORIZATION_BLOCKED"], ["CENTRAL"]) == (), (
        "a closed gate is this run's configuration, not this review's method inventory")
    assert blockers_from([], []) == ()
    print("harness.disposition self-check ok")


if __name__ == "__main__":
    _self_check()
