"""The four things a human reviewer must be able to read off the top of a review.

`python -m harness.outcome` runs the self-check.

**The defect this module exists to fix.** A first-pass review used to hand a reader a
colour, a category table, and — eight sections later — a bold `**NOT_VERIFIED**` under a
heading called `## Reproduction status`, beside a line reading
`Targets: 5 addressing blocked, 1 inconclusive, 25 not attempted`. Every one of those
tokens is correct internally and none of them answers the question the reader actually
has, which is *what did this system establish about this paper*. Worse, three of them
invite the wrong answer: `NOT_VERIFIED` reads as a verdict, `inconclusive` reads as a
property of the paper, and a colour reads as all three at once. The words INCONCLUSIVE and
NOT_VERIFIED are facts about an *execution* and about a *host*; the paper is not
inconclusive, our attempt to check it was.

**The fix is four independent folds over DISJOINT inputs.**

    tier 1  FINDING STATE     what this review established about the paper's science
                              reads: claim_status, the kept findings
    tier 2  QUESTION STATE    what became of the questions it raised
                              reads: the review questions' resolution states
    tier 3  EXECUTION STATE   what execution was attempted and what it produced
                              reads: the target outcomes
    tier 4  SCOPE STATE       how much of the paper this is an assessment of
                              reads: the targets and their centrality

Disjointness is the guarantee, not a tidiness preference. `finding_state` is a function of
`claim_status` and the kept findings and of NOTHING in tier 3, so no execution outcome —
blocked, inconclusive, refused by a gate, or run on a provenance the ceiling does not
admit — can move it. `_self_check` asserts that by sweeping every execution state against
every finding state and confirming the first cannot change the second. That is invariants
4 to 7 and 16 restated where a reader can see them: infrastructure failure is not
scientific failure, and here it is not even in the same column.

**Nothing here decides anything.** `claim_status` already applied the provenance ceiling
(`stages/report.claim_status`), `TargetOutcome.evidence_state` applied it a second time
(`taxonomy.evidence_state`), and severity was capped long before either. This module reads
those decisions and chooses ENGLISH. It consults no model, and it cannot reach a state its
inputs do not already support.
"""
from __future__ import annotations

# --- tier 1: what was established about the paper's science ---------------------------
# Three states, and the top one is exactly `claim_status == VERIFIED_FAILURE`, so this
# axis is a relabelling of a decision the threshold table already made rather than a
# second route to it.
FINDING_STATES = (
    "MATERIAL_FAILURE_ESTABLISHED",       # a counted FATAL, or an admissible failed reproduction
    "CONCERNS_RECORDED",                  # verified concerns, none of them material
    "NO_CONCERN_SURVIVED_VERIFICATION",   # nothing was raised, or nothing survived
)

# --- tier 2: what became of the questions the review raised ---------------------------
QUESTION_STATES = (
    "ALL_QUESTIONS_SETTLED",
    "SOME_QUESTIONS_SETTLED",
    "NO_QUESTION_SETTLED",
    "NO_QUESTION_RAISED",
)

# --- tier 3: what execution was attempted, and what it produced ------------------------
# The two that name a quantity are the ONLY ones that say anything about the paper. The
# other four are facts about a plan, a gate, a host or an artifact, and each is worded so
# that it cannot be read as a property of the paper.
EXECUTION_STATES = (
    "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY",
    "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY",
    # TWO MORE, AND THEY ARE NOT THE TWO ABOVE. `establishes_failure` admits
    # VALIDATION_DEFECT_ESTABLISHED, whose experiment THIS REVIEW designed — so reaching
    # the first row printed "the authors' own code ran and did not produce a quantity the
    # paper prints" for a contrast the authors never published and a quantity they never
    # printed. That is the same accidental attribution `EXECUTION_ACTOR` was added to
    # prevent for `reimpl_exec`, one route further along, and the fix is the same shape:
    # a state of its own with a sentence that names what actually ran.
    "EXECUTION_CONTRADICTED_A_PREDICTED_CONTRAST",
    "EXECUTION_CONFIRMED_A_PREDICTED_CONTRAST",
    "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE",  # it ran; nothing admissible came out
    "EXECUTION_BLOCKED_BEFORE_IT_STARTED",        # warranted, refused before a process began
    "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED",      # warranted, nothing started, nothing refused
    "NO_EXECUTION_WARRANTED",                     # the referee judged none would settle anything
)

EXECUTION_ABOUT_THE_PAPER = ("EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY",
                             "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY",
                             "EXECUTION_CONTRADICTED_A_PREDICTED_CONTRAST",
                             "EXECUTION_CONFIRMED_A_PREDICTED_CONTRAST")

# --- tier 4: how much of the paper this is an assessment of ----------------------------
SCOPE_STATES = (
    "CENTRAL_CLAIMS_LEFT_UNCHECKED",   # the limitation a reader most needs, so it wins
    "SOME_TARGETS_PURSUED",
    "NO_TARGET_PURSUED",
)

# The reader-facing clause for each state, held here as ONE table rather than
# interpolated at three call sites: a gloss that can drift between the review, the machine
# report and the dossier is a gloss that will. Deliberately terse — one clause each. The
# long-form explanation of why the axis exists lives on `artifacts.ReviewOutcome`'s field
# descriptions, where length costs a reader nothing.
FINDING_GLOSS = {
    "MATERIAL_FAILURE_ESTABLISHED":
        "a material failure was established on evidence this system re-verified",
    "CONCERNS_RECORDED": "none was established as a material failure, within the scope checked",
    "NO_CONCERN_SURVIVED_VERIFICATION":
        "a statement about this review, not a certificate of correctness",
}

QUESTION_GLOSS = {
    "ALL_QUESTIONS_SETTLED": "every question this review raised was settled",
    "SOME_QUESTIONS_SETTLED": "the rest are printed below as open questions",
    "NO_QUESTION_SETTLED": "the concerns stand as questions for a human reviewer",
    "NO_QUESTION_RAISED": "no question was raised",
}

# The wording requirement, met literally: an execution that settled nothing must read as
# an execution that did not produce admissible evidence, never as a paper that is
# inconclusive. The exact reason follows in `execution_detail`.
# WHICH CODE RAN, in the reader's words, keyed on the provenance that ran it. The two
# states below say something about the paper, and what they are entitled to say depends
# entirely on whose program produced the number.
#
# The defect this closes: the two glosses read "the authors' own code ran" unconditionally,
# and the set they are reached from is `provenance.admits`, which is three provenances,
# not one. So an INDEPENDENT REIMPLEMENTATION that disagreed with the paper printed, in
# the top block of the review, "the authors' own code ran and did not produce a quantity
# the paper prints" — the single accusation `harness/provenance.py` exists to prevent, and
# the one `local_exec` already appends a disclaimer about one layer down. `driver` did the
# same. The states stay as they are; the sentence now names the actor.
EXECUTION_ACTOR = {
    "repo_exec": "the authors' own code",
    "driver": "an operator-supplied reproduction",
    "reimpl_exec": "an independent reimplementation (not the authors' code)",
}
_DEFAULT_ACTOR = "code this review's ceiling admits"

EXECUTION_GLOSS = {
    "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY":
        "{actor} ran and did not produce a quantity the paper prints",
    "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY":
        "{actor} ran and re-derived a quantity the paper prints",
    # NEITHER SENTENCE SAYS "reproduce", and neither says the paper printed anything: a
    # focused validation compares two arms and the paper printed neither of them. What
    # each says is bounded to the one contrast that ran, because that is all a single
    # controlled comparison establishes.
    "EXECUTION_CONTRADICTED_A_PREDICTED_CONTRAST":
        "{actor} ran a controlled contrast this review designed, and it came out the "
        "opposite way to what the claim predicts",
    "EXECUTION_CONFIRMED_A_PREDICTED_CONTRAST":
        "{actor} ran a controlled contrast this review designed, and it came out the way "
        "the claim predicts",
    "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE":
        "execution was attempted and did not produce admissible evidence, so nothing "
        "about the paper follows from it",
    "EXECUTION_BLOCKED_BEFORE_IT_STARTED":
        "warranted and refused before any process started - a fact about the artifact, "
        "this host or a gate in this harness",
    "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED":
        "warranted, and no process was started for it in this run",
    "NO_EXECUTION_WARRANTED":
        "nothing runnable would have settled an open question here, so nothing was run",
}

SCOPE_GLOSS = {
    "CENTRAL_CLAIMS_LEFT_UNCHECKED":
        "the rows above assess less than the paper's main argument",
    "SOME_TARGETS_PURSUED": "some of the paper's checkable claims were pursued",
    "NO_TARGET_PURSUED": "the rows above rest on the paper's text alone",
}

# What the block must say in its own voice, once. The three readings it forecloses are the
# three a single token invites.
DISCLAIMER = (
    "No row above says the paper is correct, that it failed, or that it is inconclusive. "
    "They say what was established, what was settled, what execution produced, and how "
    "much was looked at."
)

# The exact reason must survive to the reader — that is the requirement this axis exists
# to meet — but a reviewer report is a bounded artifact (invariant 19), so it is cut at a
# SENTENCE boundary when it must be cut at all. The old renderer cut at 200 characters
# mid-clause, which took the words "is evidence about the mechanism, not a reproduction of
# the value printed at ..." off the end of a line that began with a large delta, and left
# a skimming reader looking at what appeared to be a catastrophic reproduction failure.
_DETAIL_CHARS = 460


def clip(text: str, limit: int = _DETAIL_CHARS) -> str:
    """Whitespace-collapsed, and cut only after a sentence that fits."""
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    head = s[:limit]
    cut = max(head.rfind(". "), head.rfind("; "), head.rfind(": "))
    if cut > limit // 3:
        return head[:cut + 1] + " (full reason in the ledger)"
    return head[:head.rfind(" ") if " " in head else limit] + "… (full reason in the ledger)"


def _settled(status: str) -> bool:
    return (status or "").strip().upper().startswith("RESOLVED")


def finding_state(*, claim_status: str = "", kept_findings: int = 0) -> str:
    """Tier 1. Reads `claim_status` and a count; reads nothing about execution.

    `claim_status` is where the provenance ceiling has already been applied twice, so
    this axis inherits it rather than re-deciding it. An execution state is deliberately
    NOT a parameter: a blocked, refused or inadmissible execution must be structurally
    unable to change what the review established, and leaving it out of the signature
    makes that unable rather than merely unlikely — the same discipline `grading.derive`
    uses to make "a CONFOUND is always MAJOR" inexpressible.
    """
    if (claim_status or "").strip().upper() == "VERIFIED_FAILURE":
        return "MATERIAL_FAILURE_ESTABLISHED"
    return "CONCERNS_RECORDED" if kept_findings > 0 else "NO_CONCERN_SURVIVED_VERIFICATION"


def question_state(*, raised: int = 0, settled: int = 0) -> str:
    """Tier 2. Two counts, both read off the question set."""
    if raised <= 0:
        return "NO_QUESTION_RAISED"
    if settled >= raised:
        return "ALL_QUESTIONS_SETTLED"
    return "SOME_QUESTIONS_SETTLED" if settled > 0 else "NO_QUESTION_SETTLED"


def execution_actor(outcomes: list | None = None) -> str:
    """Whose program produced the evidence the execution row is about to describe.

    Reads provenance and nothing else, so it cannot be swayed by a disposition. When the
    admissible outcomes disagree about whose code ran, the row names both rather than
    picking one: a review that ran the authors' code on one target and a reimplementation
    on another has done two different things, and flattening them is how the sentence
    this function exists to fix got written in the first place.
    """
    from .provenance import admits
    seen = []
    for o in (outcomes or []):
        p = getattr(o, "provenance", "")
        if admits(p) and p not in seen:
            seen.append(p)
    if not seen:
        return _DEFAULT_ACTOR
    if len(seen) == 1:
        return EXECUTION_ACTOR.get(seen[0], _DEFAULT_ACTOR)
    return " and ".join(EXECUTION_ACTOR.get(p, _DEFAULT_ACTOR) for p in seen)


def execution_state(outcomes: list | None = None, plans: list | None = None) -> tuple[str, str]:
    """Tier 3: (state, the exact reason). Reads target outcomes and plans, nothing else.

    Ordered by how much each state establishes, not by how bad it sounds. A paper with one
    reproduced target and one failed one reports the failure, because that is the stronger
    statement and tier 1 has already counted it; the detail names both so the mixed shape
    is not lost.

    `establishes_failure` and `evidence_state` are the outcome's own derived properties,
    so the provenance ceiling arrives with them: a FAILED_REPRODUCTION on a provenance the
    ceiling does not admit has already become INCONCLUSIVE_EXECUTION upstream and reaches
    here as "ran and produced nothing admissible", which is what it is.
    """
    outs = list(outcomes or [])
    warranted = {getattr(p, "target_id", "") for p in (plans or [])
                 if getattr(p, "requires_execution", False)}

    # THIS ROW IS ABOUT EXECUTION, so only an outcome whose evidence CAME from an
    # execution may fill it. `establishes_failure` is broader than that now: a
    # PAPER_ARITHMETIC_CONTRADICTION establishes a defect with nothing launched and
    # provenance "paper", and letting it through here printed "the authors' own code ran
    # and did not produce a quantity the paper prints" for a paper whose arithmetic was
    # recomputed and whose code was never fetched. That is the exact attribution this
    # system must never make by accident, so the filter is the reproduction ceiling
    # itself rather than a second list of dispositions.
    from .provenance import admits
    executed = [o for o in outs if admits(getattr(o, "provenance", ""))]

    # THE VALIDATION ROUTE FIRST, and keyed on its own dispositions rather than on
    # `establishes_failure`, which is broader than this row. A contrast this review
    # designed is not a reproduction in either direction, so both of its settled outcomes
    # get their own state and neither borrows the reproduction sentence.
    contradicted = [o for o in executed
                    if getattr(o, "disposition", "") == "VALIDATION_DEFECT_ESTABLISHED"]
    confirmed = [o for o in executed
                 if getattr(o, "disposition", "") == "VALIDATION_SUPPORTS_CLAIM"]
    failed = [o for o in executed if getattr(o, "establishes_failure", False)
              and getattr(o, "disposition", "") != "VALIDATION_DEFECT_ESTABLISHED"]
    reproduced = [o for o in executed
                  if getattr(o, "evidence_state", "") == "REPRODUCTION_SUCCESS"]
    if contradicted:
        return "EXECUTION_CONTRADICTED_A_PREDICTED_CONTRAST", (
            f"target {getattr(contradicted[0], 'target_id', '?')}: "
            f"{getattr(contradicted[0], 'reason', '') or 'no reason recorded'}")
    if failed:
        detail = (f"target {getattr(failed[0], 'target_id', '?')}: "
                  f"{getattr(failed[0], 'reason', '') or 'no reason recorded'}")
        if reproduced:
            detail += (f" ({len(reproduced)} other target(s) reproduced; a paper's targets "
                       f"end independently)")
        return "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY", detail
    if reproduced:
        return "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY", (
            f"target {getattr(reproduced[0], 'target_id', '?')}: "
            f"{getattr(reproduced[0], 'reason', '') or 'no reason recorded'}")
    if confirmed:
        # AFTER the reproduction row, deliberately. Re-deriving a quantity the paper
        # actually printed is the stronger statement of the two, and a review that did
        # both should lead with it.
        return "EXECUTION_CONFIRMED_A_PREDICTED_CONTRAST", (
            f"target {getattr(confirmed[0], 'target_id', '?')}: "
            f"{getattr(confirmed[0], 'reason', '') or 'no reason recorded'}")

    ran = [o for o in outs if int(getattr(o, "launched", 0) or 0) > 0]
    if ran:
        return "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE", (
            f"target {getattr(ran[0], 'target_id', '?')}: "
            f"{getattr(ran[0], 'reason', '') or 'no reason recorded'}")

    # Nothing started. Was one warranted, and was it refused or simply not reached?
    blocked = [o for o in outs
               if getattr(o, "target_id", "") in warranted
               and getattr(o, "disposition", "") in _BLOCKED]
    if blocked:
        return "EXECUTION_BLOCKED_BEFORE_IT_STARTED", (
            f"target {getattr(blocked[0], 'target_id', '?')} "
            f"({getattr(blocked[0], 'disposition', '').replace('_', ' ').lower()}): "
            f"{getattr(blocked[0], 'reason', '') or 'no reason recorded'}")
    if warranted:
        return "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED", (
            f"{len(warranted)} target(s) were judged to warrant an experiment and no "
            f"process was started for them in this run")
    return "NO_EXECUTION_WARRANTED", (
        "no target's question turned on anything this system could run and reconcile")


def scope_state(*, unchecked_central: int = 0, pursued: int = 0) -> str:
    """Tier 4. The limitation wins when there is one, because a fact that changes no
    decision is the easiest one for a report to let disappear and this is the fact a
    reader most needs in order to know what the other three rows are worth."""
    if unchecked_central > 0:
        return "CENTRAL_CLAIMS_LEFT_UNCHECKED"
    return "SOME_TARGETS_PURSUED" if pursued > 0 else "NO_TARGET_PURSUED"


# A target whose experiment was refused before anything ran. Imported lazily inside
# `_blocked` rather than at module scope so `harness.outcome` stays importable from
# `harness.artifacts` without a cycle.
def _blocked_dispositions() -> tuple[str, ...]:
    from .artifacts import BLOCKED_DISPOSITIONS
    return BLOCKED_DISPOSITIONS


_BLOCKED = ("SPECIFICATION_BLOCKED", "ADDRESSING_BLOCKED", "REPORTING_BLOCKED",
            "NO_ROUTE_AVAILABLE", "ARTIFACT_BLOCKED", "ENVIRONMENT_BLOCKED",
            "RESOURCE_BLOCKED", "IDENTITY_BLOCKED", "AUTHORIZATION_BLOCKED",
            "COMPARISON_BLOCKED")


def derive(report, target_set=None, *, unchecked_central: int = 0):
    """The whole four-tier outcome for one review, as a `ReviewOutcome`.

    A pure projection: every input is a field some earlier stage already decided, and this
    function's only authority is over which English sentence names it.
    """
    from .artifacts import ReviewOutcome

    ts = target_set
    outs = list(getattr(ts, "outcomes", []) or [])
    plans = list(getattr(ts, "plans", []) or [])
    qs = list(getattr(ts, "questions", []) or [])

    kept = len(getattr(report, "scientific_findings", None) or
               getattr(report, "findings", None) or [])
    raised = len(qs)
    settled = sum(1 for q in qs if _settled(getattr(q, "resolution_status", "")))

    f_state = finding_state(claim_status=getattr(report, "claim_status", ""),
                            kept_findings=kept)
    q_state = question_state(raised=raised, settled=settled)
    e_state, e_detail = execution_state(outs, plans)
    e_actor = execution_actor(outs)
    pursued = sum(1 for o in outs if getattr(o, "evidence_state", "NOT_INVESTIGATED")
                  != "NOT_INVESTIGATED")
    s_state = scope_state(unchecked_central=unchecked_central, pursued=pursued)

    return ReviewOutcome(
        paper_id=getattr(report, "paper_id", ""),
        finding_state=f_state,
        finding_detail=(f"{kept} evidence-verified concern(s)" if kept else
                        "no concern survived evidence verification"),
        question_state=q_state,
        question_detail=f"{raised} raised · {settled} settled · {raised - settled} open",
        execution_state=e_state,
        execution_detail=e_detail,
        execution_actor=e_actor,
        scope_state=s_state,
        scope_detail=(f"{unchecked_central} central claim(s) checkable and unchecked"
                      if unchecked_central else
                      f"{pursued} of {len(outs)} target(s) reached an evidence route"),
        claim_status=getattr(report, "claim_status", "") or "NOT_VERIFIED",
        triage=getattr(report, "triage", "") or getattr(report, "verdict", "") or "GREEN",
    )


def render(o) -> list[str]:
    """The `## Review outcome` block: four labelled rows and one disclaimer.

    A list rather than a table because the four rows carry sentences of very different
    lengths — the execution row has to hold an exact reason — and a markdown table with
    one 400-character cell is less readable than four lines, not more.
    """
    return [
        "## Review outcome", "",
        f"- **what was established** — `{o.finding_state}`: {o.finding_detail}; "
        f"{FINDING_GLOSS.get(o.finding_state, '')}.",
        f"- **what was resolved** — `{o.question_state}`: {o.question_detail}; "
        f"{QUESTION_GLOSS.get(o.question_state, '')}.",
        f"- **what execution produced** — `{o.execution_state}`: "
        f"{EXECUTION_GLOSS.get(o.execution_state, '').format(actor=o.execution_actor or _DEFAULT_ACTOR)}. "
        f"Exact reason: {clip(o.execution_detail)}",
        f"- **what this is an assessment of** — `{o.scope_state}`: {o.scope_detail}; "
        f"{SCOPE_GLOSS.get(o.scope_state, '')}.",
        "", DISCLAIMER, "",
    ]


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    from .artifacts import (BLOCKED_DISPOSITIONS, EvalReport, PlanDecision, ReviewOutcome,
                            ReviewQuestion, TargetOutcome, TargetSet)

    # --- the blocked set is not a second copy of the vocabulary ------------------------
    assert set(_BLOCKED) == set(BLOCKED_DISPOSITIONS), (
        "the blocked dispositions here have drifted from artifacts.BLOCKED_DISPOSITIONS")
    assert set(_BLOCKED) == set(_blocked_dispositions())

    # --- the signature is the invariant: tier 1 cannot see execution ------------------
    params = set(inspect.signature(finding_state).parameters)
    assert params == {"claim_status", "kept_findings"}, params
    for bad in ("execution_state", "disposition", "reconciliation", "provenance",
                "outcomes", "probe", "launched"):
        assert bad not in params, f"tier 1 must not be able to read {bad}"

    # --- and the sweep: no execution state can move tier 1 ----------------------------
    # Every reachable combination of (what ran, what it produced) against every claim
    # status, asserting the finding state depends on the second alone. This is invariants
    # 4-7 as an exhaustive check rather than as a comment.
    for cs in ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED", ""):
        for kept in (0, 1, 9):
            base = finding_state(claim_status=cs, kept_findings=kept)
            for disp in ("INCONCLUSIVE", "ENVIRONMENT_BLOCKED", "AUTHORIZATION_BLOCKED",
                         "RESOURCE_BLOCKED", "FAILED_REPRODUCTION", "REPRODUCED",
                         "NOT_ATTEMPTED", "BUDGET_DEFERRED", "CITATION_VERIFIED_ONLY"):
                for prov in ("repo_exec", "driver", "synthesized", "template", "paper", ""):
                    o = TargetOutcome(target_id="T", disposition=disp, provenance=prov,
                                      launched=1, reason="r")
                    st, _ = execution_state([o], [PlanDecision(target_id="T",
                                                               requires_execution=True)])
                    assert st in EXECUTION_STATES, st
                    assert finding_state(claim_status=cs, kept_findings=kept) == base, (
                        f"{disp}/{prov} changed the finding state")

    # --- tier 1 ------------------------------------------------------------------------
    assert finding_state(claim_status="VERIFIED_FAILURE") == "MATERIAL_FAILURE_ESTABLISHED"
    assert finding_state(claim_status="NOT_VERIFIED", kept_findings=3) == "CONCERNS_RECORDED"
    assert finding_state(claim_status="NOT_VERIFIED") == "NO_CONCERN_SURVIVED_VERIFICATION"
    # VERIFIED_SUPPORT is evidence, not a finding: it must not read as "no concerns"
    # merely because something reproduced.
    assert finding_state(claim_status="VERIFIED_SUPPORT", kept_findings=2) == "CONCERNS_RECORDED"

    # --- tier 2 ------------------------------------------------------------------------
    assert question_state() == "NO_QUESTION_RAISED"
    assert question_state(raised=4, settled=4) == "ALL_QUESTIONS_SETTLED"
    assert question_state(raised=4, settled=1) == "SOME_QUESTIONS_SETTLED"
    assert question_state(raised=4) == "NO_QUESTION_SETTLED"

    # --- tier 3 ------------------------------------------------------------------------
    assert execution_state([], [])[0] == "NO_EXECUTION_WARRANTED"
    warrant = [PlanDecision(target_id="T", requires_execution=True)]
    assert execution_state([TargetOutcome(target_id="T", disposition="NOT_ATTEMPTED")],
                           warrant)[0] == "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED"
    for d in BLOCKED_DISPOSITIONS:
        st, why = execution_state([TargetOutcome(target_id="T", disposition=d,
                                                 reason="the gate refused")], warrant)
        assert st == "EXECUTION_BLOCKED_BEFORE_IT_STARTED", d
        assert "the gate refused" in why
    st, why = execution_state([TargetOutcome(target_id="T", disposition="INCONCLUSIVE",
                                             launched=1, reason="units did not match")],
                              warrant)
    assert st == "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE" and "units" in why
    # the ceiling: a failed reproduction the ceiling does not admit is not a contradiction
    st, _ = execution_state([TargetOutcome(target_id="T", disposition="FAILED_REPRODUCTION",
                                           provenance="synthesized", launched=1)], warrant)
    assert st == "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE", (
        "a synthesized probe may not contradict a printed quantity")
    # AND THE SAME RULE FROM THE OTHER SIDE: a defect established with nothing launched is
    # not an execution result. A paper-internal arithmetic contradiction establishes a
    # failure (`establishes_failure` is True) and this row must still say that no
    # execution produced anything, because the sentence for the top state names the
    # AUTHORS' OWN CODE and no code ran.
    arith = TargetOutcome(target_id="T", disposition="PAPER_ARITHMETIC_CONTRADICTION",
                          provenance="paper", launched=0, reason="12*3 is not 40")
    assert arith.establishes_failure, "Tier 1 still holds for it"
    st, _ = execution_state([arith], warrant)
    assert st != "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY", (
        "nothing ran, so the execution row may not claim the authors' code produced this")
    assert st == "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED", st
    assert execution_state([arith], [])[0] == "NO_EXECUTION_WARRANTED"
    for state in EXECUTION_ABOUT_THE_PAPER:
        assert "{actor} ran" in EXECUTION_GLOSS[state], (
            "both states that say anything about the paper attribute a RUN, which is why "
            "a no-execution outcome may never reach them - and they name WHOSE run, "
            "because they are reachable from every admissible provenance and not only "
            "from the authors' own repository")
    # The actor is read off provenance and nothing else, and a reimplementation may never
    # be described as the authors' code. This is the reader-facing half of the rule
    # `harness/provenance.py` enforces on the deciding side.
    assert execution_actor([]) == _DEFAULT_ACTOR
    assert execution_actor([TargetOutcome(target_id="T", disposition="FAILED_REPRODUCTION",
                                          provenance="synthesized")]) == _DEFAULT_ACTOR, (
        "an inadmissible run names no actor, because it is entitled to describe none")
    for prov, expect in (("repo_exec", "the authors' own code"),
                         ("reimpl_exec", "an independent reimplementation (not the authors' code)"),
                         ("driver", "an operator-supplied reproduction")):
        got = execution_actor([TargetOutcome(target_id="T", disposition="FAILED_REPRODUCTION",
                                             provenance=prov, launched=1)])
        assert got == expect, (prov, got)
        if prov != "repo_exec":
            assert "authors' own code" not in EXECUTION_GLOSS[
                "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY"].format(actor=got), (
                f"{prov} must never be reported as the authors' own code")
    mixed = execution_actor([
        TargetOutcome(target_id="A", disposition="FAILED_REPRODUCTION",
                      provenance="repo_exec", launched=1),
        TargetOutcome(target_id="B", disposition="REPRODUCED",
                      provenance="reimpl_exec", launched=1)])
    assert "authors' own code" in mixed and "reimplementation" in mixed, mixed
    st, _ = execution_state([TargetOutcome(target_id="T", disposition="FAILED_REPRODUCTION",
                                           provenance="repo_exec", launched=1)], warrant)
    assert st == "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY"
    # a mixed paper reports the failure and does not hide the reproduction
    st, why = execution_state(
        [TargetOutcome(target_id="A", disposition="REPRODUCED", provenance="repo_exec",
                       launched=1),
         TargetOutcome(target_id="C", disposition="FAILED_REPRODUCTION",
                       provenance="repo_exec", launched=1, reason="0.71 vs 0.83")], warrant)
    assert st == "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY"
    assert "other target(s) reproduced" in why
    # FOUR execution states say something about the paper, and they are exactly the ones
    # naming what was HELD AGAINST WHAT: a quantity the paper printed, or a contrast the
    # claim predicts. Every other state names a fact about an attempt — it produced nothing
    # admissible, it was refused, it was never started, none was warranted — and those are
    # facts about an artifact, this host, or a gate.
    for st in EXECUTION_STATES:
        about = st in EXECUTION_ABOUT_THE_PAPER
        assert about == ("PRINTED_QUANTITY" in st or "PREDICTED_CONTRAST" in st), st

    # A CONTRAST THIS REVIEW DESIGNED IS NOT A REPRODUCTION, in either direction, and
    # neither of its sentences may borrow the reproduction wording. This is the same
    # correction `EXECUTION_ACTOR` made for `reimpl_exec`, one route further along.
    st, why = execution_state(
        [TargetOutcome(target_id="V", disposition="VALIDATION_DEFECT_ESTABLISHED",
                       provenance="repo_exec", launched=2,
                       reason="the regulariser arm did not move the metric.")], warrant)
    assert st == "EXECUTION_CONTRADICTED_A_PREDICTED_CONTRAST", st
    assert "V" in why
    sentence = EXECUTION_GLOSS[st].format(actor=execution_actor(
        [TargetOutcome(target_id="V", disposition="VALIDATION_DEFECT_ESTABLISHED",
                       provenance="repo_exec", launched=2)]))
    for forbidden in ("reproduce", "re-derived", "a quantity the paper prints"):
        assert forbidden not in sentence, (forbidden, sentence)
    assert "this review designed" in sentence

    st, _ = execution_state(
        [TargetOutcome(target_id="V", disposition="VALIDATION_SUPPORTS_CLAIM",
                       provenance="repo_exec", launched=2)], warrant)
    assert st == "EXECUTION_CONFIRMED_A_PREDICTED_CONTRAST", st

    # AND A REPRODUCTION STILL LEADS. Re-deriving a quantity the paper actually printed is
    # the stronger of the two statements, so a review that did both says that first.
    st, _ = execution_state(
        [TargetOutcome(target_id="V", disposition="VALIDATION_SUPPORTS_CLAIM",
                       provenance="repo_exec", launched=2),
         TargetOutcome(target_id="A", disposition="REPRODUCED", provenance="repo_exec",
                       launched=1)], warrant)
    assert st == "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY", st

    # An inadmissible provenance reaches NEITHER new state: `executed` is filtered by the
    # reproduction ceiling before any of this.
    st, _ = execution_state(
        [TargetOutcome(target_id="V", disposition="VALIDATION_DEFECT_ESTABLISHED",
                       provenance="synthesized", launched=2)], warrant)
    assert st != "EXECUTION_CONTRADICTED_A_PREDICTED_CONTRAST", st

    # --- tier 4 ------------------------------------------------------------------------
    assert scope_state() == "NO_TARGET_PURSUED"
    assert scope_state(pursued=2) == "SOME_TARGETS_PURSUED"
    assert scope_state(unchecked_central=1, pursued=9) == "CENTRAL_CLAIMS_LEFT_UNCHECKED"

    # --- every state has a gloss, and every gloss names a state -----------------------
    for tbl, vocab in ((FINDING_GLOSS, FINDING_STATES), (QUESTION_GLOSS, QUESTION_STATES),
                       (EXECUTION_GLOSS, EXECUTION_STATES), (SCOPE_GLOSS, SCOPE_STATES)):
        assert set(tbl) == set(vocab), (set(tbl) ^ set(vocab))
        for k, v in tbl.items():
            assert v and len(v) < 120, f"{k}: a review-block gloss is one clause"

    # --- the whole fold, and the rendered block ----------------------------------------
    ts = TargetSet(
        paper_id="p",
        questions=[ReviewQuestion(question_id="Q1", resolution_status="UNRESOLVED"),
                   ReviewQuestion(question_id="Q2", resolution_status="RESOLVED_FROM_PAPER")],
        plans=[PlanDecision(target_id="T", requires_execution=True)],
        outcomes=[TargetOutcome(target_id="T", disposition="INCONCLUSIVE", launched=1,
                                provenance="synthesized", reason="the metric was unparsed")])
    rep = EvalReport(paper_id="p", title="t", verdict="GREEN", triage="YELLOW",
                     claim_status="NOT_VERIFIED",
                     findings=[], scientific_findings=[])
    o = derive(rep, ts, unchecked_central=1)
    assert isinstance(o, ReviewOutcome)
    assert o.finding_state == "NO_CONCERN_SURVIVED_VERIFICATION"
    assert o.question_state == "SOME_QUESTIONS_SETTLED"
    assert o.execution_state == "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE"
    assert o.scope_state == "CENTRAL_CLAIMS_LEFT_UNCHECKED"
    assert "the metric was unparsed" in o.execution_detail

    text = "\n".join(render(o))
    assert text.startswith("## Review outcome")
    assert "did not produce admissible evidence" in text
    assert "Exact reason: target T: the metric was unparsed" in text
    assert "is inconclusive" in DISCLAIMER, "the block must foreclose that reading"
    for word in ("correct", "failed", "inconclusive"):
        assert word in DISCLAIMER, word

    # `clip` cuts after a sentence, never mid-clause, and says that it cut
    long = ("the probe that ran was synthesized, not the paper's own code. Its result is "
            "evidence about the mechanism. " + "x" * 500)
    got = clip(long)
    assert got.endswith("(full reason in the ledger)")
    assert "evidence about the mechanism." in got
    assert clip("short enough") == "short enough"
    print("harness.outcome self-check ok")


if __name__ == "__main__":
    _self_check()
