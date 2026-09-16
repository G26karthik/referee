"""What kind of scientific problem a finding is — kept apart from what was done about it.

`python -m harness.taxonomy` runs the self-check.

**The three axes this module exists to stop collapsing.** A referee's output has three
independent dimensions, and a single label of any kind destroys two of them:

    SCIENTIFIC CLASS   what kind of problem this is — an overstated claim, a contradiction,
                       a confound. A property of the paper's argument.
    RESOLUTION STATE   whether the question it raises was settled, and by what. A property
                       of the review process.
    EVIDENCE STATE     what the evidence route actually produced — an execution that
                       reproduced, one that failed, an artifact that was missing, an
                       environment that could not be built. A property of the world.

A paper can carry a CONFOUND that was RESOLVED_FROM_PAPER with evidence state
NOT_INVESTIGATED; another can carry a CONFOUND that is UNRESOLVED because the artifact was
missing. Those are different things to tell a referee, and a colour cannot say either.

**What the fallback means, stated rather than hidden.** `classify` is most-specific-first,
and a finding that classified its own discrepancy or its own missing baseline yields a
class derived from that self-classification. A finding that did neither falls through to
`_BY_LENS` — which means its `scientific_class` is, in that case, little more than a
restatement of WHICH LENS raised it. That is a real ceiling on this axis and it is visible
in the artifact: over the current corpus the classes come from a mix of both paths, and a
reader counting CONFOUNDs is partly counting how many findings the confound lens wrote.
The fallback is kept because the alternative is an unclassified bucket that hides the same
fact, and it is documented here because a taxonomy whose derivation is not stated invites
being read as more than it is.

**Nothing here is a severity and nothing here is a verdict.** This module answers "what
kind of thing did the referee find", and it answers it from fields the harness has already
derived or clamped — a lens's own closed-vocabulary self-classification, the address shape,
the grader's outcome. It reads no prose, no number, no metric name and no paper identity,
so a paper-specific or threshold-shaped rule is inexpressible here exactly as it is in
`harness.grading.derive` and `harness.priority.score`.
"""
from __future__ import annotations

# Invariant 3 lives in ONE object; this is the same tuple, not a second copy of the
# rule. `tests/test_reimplementation_path.py` asserts the identity across every site.
from .provenance import ADMISSIBLE_REPRODUCTION_PROVENANCE
from .provenance import admits as _admits

# --- axis 1: what kind of scientific problem -----------------------------------------
# Ordered most-specific first; `classify` returns the first that applies, so a finding
# that carries a discrepancy classification is a CONTRADICTION rather than falling through
# to whichever lens happened to raise it.
SCIENTIFIC_CLASSES = (
    "CONTRADICTION",        # incompatible statements, numbers or configurations in one paper
    "OVERSTATED_CLAIM",     # the conclusion drawn is stronger than the evidence offered
    "CONFOUND",             # an alternative explanation remains compatible with the gain
    "PROTOCOL_ISSUE",       # the evaluation design does not test the claim it supports
    "MISSING_CONTROL",      # a control or ablation is absent, leaving a question open
    "MISSING_VALIDATION",   # a claimed property is asserted on evidence of a narrower one
    "IMPLEMENTATION_ISSUE",  # the released artifact conflicts with the paper's method
    "SPECIFICATION_GAP",    # the paper does not say enough for the claim to be checkable
    "UNRESOLVED_QUESTION",  # a scientific question the referee raised and could not close
    # PAPER-level, and deliberately unreachable from `classify`. A FINDING is by
    # definition something a lens raised, so no finding can carry it; it describes the
    # state of a paper for which nothing material was established within the scope
    # checked. Kept in the vocabulary so a report or an aggregate can say that in one
    # word, and asserted unreachable in `_self_check` so it cannot quietly become a
    # bucket that findings fall into.
    "NO_MATERIAL_ISSUE_FOUND",  # nothing of the above, within what was checked
)

# --- axis 2: was the question settled, and by what ------------------------------------
RESOLUTION_STATES = (
    "RESOLVED_FROM_PAPER",      # the paper's own printed content settled it
    "RESOLVED_FROM_ARTIFACT",   # static inspection of the released code settled it
    # A BOUNDED PRIOR-ART QUESTION SETTLED FROM THE PUBLISHED LITERATURE. Reachable only
    # from `PRIOR_ART_EVIDENCE`, which needs a bound relation and not a silence: a search
    # that completed and matched nothing resolves NOTHING, and lands on UNRESOLVED like
    # every other state that looked without settling.
    "RESOLVED_FROM_LITERATURE",
    "RESOLVED_BY_EXECUTION",    # something ran, admissibly, and settled it
    "UNRESOLVED",               # still open
    "NOT_INVESTIGATED",         # no route was pursued; see the evidence state for why
)

# --- axis 3: what the evidence route produced -----------------------------------------
# This is about the WORLD, not about the paper. Every entry except the two reproduction
# outcomes is a fact about an artifact, a host, or this harness's own gates.
EVIDENCE_STATES = (
    "REPRODUCTION_SUCCESS",     # admissible execution re-derived the printed quantity
    "REPRODUCTION_FAILURE",     # admissible execution did not, having reached the experiment
    "PAPER_INTERNAL_EVIDENCE",  # settled against the paper's own printed content
    "ARTIFACT_EVIDENCE",        # settled by reading the released code — about the PAPER
    # A BOUNDED FACT ABOUT THE CHECKOUT, and its own state because ARTIFACT_EVIDENCE is
    # in EVIDENCE_ABOUT_THE_PAPER and this is not. "The checkout advertises evaluate.py"
    # settles a question about the artifact and says nothing whatever about the document;
    # the first version of this route mapped it to ARTIFACT_EVIDENCE and so reported four
    # papers as having had a claim about their implementation settled by the presence of
    # a file. The RESOLUTION is real — the narrow question is closed — which is why it
    # still resolves to RESOLVED_FROM_ARTIFACT.
    "ARTIFACT_PROPERTY_ESTABLISHED",
    # BOTH LOCATIONS VERIFIED, THE RELATIONSHIP NOT. The artifact-route analogue of
    # CITATION_VERIFIED, and its own state for the same reason: the alternatives both lie.
    # ARTIFACT_EVIDENCE would say the paper had been checked; NOT_INVESTIGATED would say
    # nobody looked.
    "ARTIFACT_ENDPOINTS_VERIFIED",
    # A PRIOR-ART RELATION THE BOUNDED EVIDENCE ITSELF BINDS. About the paper, because a
    # target claiming to be first at X and an earlier work stating that same narrow X is a
    # statement about this document. Deliberately absent from `establishes_failure` all
    # the same: novelty is a scholarly judgement, and a strong prior-art match is a
    # serious question for a referee rather than a verdict this system may reach.
    "PRIOR_ART_EVIDENCE",
    # BOTH WORKS REAL, THE OVERLAP A READING. The third channel to need this state, after
    # CITATION_VERIFIED and ARTIFACT_ENDPOINTS_VERIFIED, and it resolves to UNRESOLVED for
    # the same reason they do.
    "LITERATURE_ENDPOINTS_VERIFIED",
    # THE ASYMMETRY, ON THE EVIDENCE AXIS. A bounded search that completed and matched
    # nothing is a fact about the SEARCH — the declared protocol ran to its declared
    # bounds — and it is not in `EVIDENCE_ABOUT_THE_PAPER`, so no amount of it can resolve
    # anything about the document. There is no evidence state meaning "novel", and this is
    # the one a completed empty search gets instead: the encoding of
    # "failing to find prior art does not establish novelty".
    "BOUNDED_SEARCH_NO_MATCH",
    # No index answered, or none could be reached from here. A fact about this host's
    # configuration in exactly the way ENVIRONMENT_LIMITATION is.
    "LITERATURE_LIMITATION",
    "ARTIFACT_LIMITATION",      # no usable artifact for this question
    "EXTRACTION_LIMITATION",    # this harness could not build an address for the claim
    "REPORTING_LIMITATION",     # the paper prints no unambiguous quantity to compare against
    "NO_ROUTE_AVAILABLE",       # addressed and quantified; no route this system has applies
    # A route applied and the comparison at the end of it did not. Its own state because
    # NO_ROUTE_AVAILABLE says the opposite thing: an attribution question that reached a
    # focused-validation route and could not be given a second arm HAS a route, and
    # telling a referee otherwise would report a limit of our arithmetic as a limit of
    # our method inventory. Never a statement about the paper, like every entry below
    # the first four.
    "COMPARISON_LIMITATION",    # a route applied; its result had nothing to be held against
    # Something was checked and it was the CITATION, not the claim. Its own state because
    # the alternatives are both wrong: PAPER_INTERNAL_EVIDENCE said the paper had been
    # checked, and NOT_INVESTIGATED said nothing had been looked at.
    "CITATION_VERIFIED",        # the concern quotes the paper accurately; the concern stands
    "ENVIRONMENT_LIMITATION",   # this host could not mount the experiment
    "SPECIFICATION_LIMITATION",  # the paper does not specify enough to build it
    "INCONCLUSIVE_EXECUTION",   # it ran and settled nothing
    "NOT_INVESTIGATED",         # nothing was pursued
)

# A lens's own `discrepancy_type` values that mean "the paper disagrees with itself".
_CONTRADICTION_TYPES = ("ARITHMETIC_ERROR", "GENUINE_CONTRADICTION", "DIFFERENT_DENOMINATOR")
# `baseline_class` values that mean an absent comparison actually costs the paper
# something. OPTIONAL_COMPARISON and NOT_APPLICABLE deliberately do not appear: the lens
# has already said the absence is not load-bearing.
_MISSING_MATTERS = ("USEFUL_CONTROL", "IMPORTANT_MISSING_BASELINE", "CENTRAL_VALIDITY_THREAT")

# Which lens raised it, when nothing more specific applies. A fallback, not a definition.
_BY_LENS = {
    "contradiction": "CONTRADICTION",
    "overclaim": "OVERSTATED_CLAIM",
    "confound": "CONFOUND",
    "protocol": "PROTOCOL_ISSUE",
}

# A candidate the lens itself declined to stand behind is a question, not a defect.
_QUESTION_CLASSES = ("OPEN_QUESTION", "DISMISSED")

# Target dispositions -> the evidence state each represents. Only the first two say
# anything about the paper; the rest are facts about an artifact, a host or this harness.
_EVIDENCE_FOR_DISPOSITION = {
    "REPRODUCED": "REPRODUCTION_SUCCESS",
    "FAILED_REPRODUCTION": "REPRODUCTION_FAILURE",
    "PAPER_ONLY_RESOLVED": "PAPER_INTERNAL_EVIDENCE",
    # NOT_INVESTIGATED, and for the same reason NO_EXPERIMENT_NEEDED is. Re-verifying the
    # quotation behind a concern establishes that the concern cites the paper accurately;
    # it establishes nothing about whether the concern is right. Mapping it to
    # PAPER_INTERNAL_EVIDENCE — which `EVIDENCE_ABOUT_THE_PAPER` admits — made
    # `concerns_the_paper` true and `resolution_state` RESOLVED_FROM_PAPER for a route
    # whose own reason string says it decided nothing, so a paper's disputed sentence was
    # printed under "What held up" beside the findings disputing it. Invariant 1 already
    # guarantees every kept finding's quote was re-verified; saying it again per target as
    # a RESOLUTION double-counted a precondition as a result.
    "CITATION_VERIFIED_ONLY": "CITATION_VERIFIED",
    # The paper's own printed composition was recomputed and did not evaluate. This IS
    # evidence about the paper — deterministic, paper-internal, and settled either way —
    # so it maps like PAPER_ONLY_RESOLVED above, not like CITATION_VERIFIED_ONLY: the
    # arithmetic recheck decides something, the quotation recheck does not.
    "PAPER_ARITHMETIC_CONTRADICTION": "PAPER_INTERNAL_EVIDENCE",
    # FIVE TERMINAL STATES, and only ONE of them is about the paper.
    #
    # A bound paper/artifact mismatch IS a statement about the document, so it maps to
    # ARTIFACT_EVIDENCE, which `EVIDENCE_ABOUT_THE_PAPER` admits — and it is still
    # deliberately absent from `establishes_failure`, because saying the code disagrees
    # with the method section is not saying the reported number is false.
    "ARTIFACT_MISMATCH_ESTABLISHED": "ARTIFACT_EVIDENCE",
    # A bounded fact about the checkout settles its own bounded question and nothing about
    # the paper. Its own state so that a review cannot report "settled from the artifact"
    # about a paper when what was settled was that a file exists.
    "ARTIFACT_FACT_ESTABLISHED": "ARTIFACT_PROPERTY_ESTABLISHED",
    # Both ends located, the correspondence between them the auditor's reading. Resolves
    # to UNRESOLVED, exactly as CITATION_VERIFIED does, and for the same reason.
    "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS": "ARTIFACT_ENDPOINTS_VERIFIED",
    # The route ran and settled nothing. A limit of what reading can establish, reported
    # as such rather than as a missing artifact.
    "ARTIFACT_INSPECTION_INCONCLUSIVE": "COMPARISON_LIMITATION",
    # THE FIVE LITERATURE OUTCOMES, and the one that matters is the third.
    "PRIOR_ART_RELATION_STRUCTURALLY_BOUND": "PRIOR_ART_EVIDENCE",
    "LITERATURE_MATCH_VERIFIED_ENDPOINTS": "LITERATURE_ENDPOINTS_VERIFIED",
    # A COMPLETED SEARCH THAT MATCHED NOTHING RESOLVES NOTHING. Mapping this to anything
    # in `EVIDENCE_ABOUT_THE_PAPER` would make a search budget into a novelty finding,
    # which is the single rule this route is built around; mapping it to NOT_INVESTIGATED
    # would say nobody looked, which is the CITATION_VERIFIED defect in a new costume.
    "SEARCH_COMPLETED_NO_MATCH_FOUND": "BOUNDED_SEARCH_NO_MATCH",
    # Candidates were found and the evidence did not reach them — no retrievable abstract,
    # a quotation that did not resolve, a date no index knew. A limit of the retrieval,
    # reported as one.
    "SEARCH_INCONCLUSIVE": "LITERATURE_LIMITATION",
    "LITERATURE_BLOCKED": "LITERATURE_LIMITATION",
    "SPECIFICATION_BLOCKED": "SPECIFICATION_LIMITATION",
    "ARTIFACT_BLOCKED": "ARTIFACT_LIMITATION",
    # NOT ARTIFACT_LIMITATION. "We could not build an address for this claim" is a limit
    # of our own extraction, and reporting it as a missing artifact told readers of a
    # paper WITH a cloned repository that no usable artifact reached the question.
    "ADDRESSING_BLOCKED": "EXTRACTION_LIMITATION",
    # And the two refusals that used to arrive here wearing EXTRACTION_LIMITATION's label.
    # "The paper prints no unambiguous quantity" is a limit of the paper's reporting;
    # "no route this system has applies" is a limit of our method inventory. Neither is a
    # failure of our extraction, and over the shipped corpus the third was 52 of 52.
    "REPORTING_BLOCKED": "REPORTING_LIMITATION",
    "NO_ROUTE_AVAILABLE": "NO_ROUTE_AVAILABLE",
    "ENVIRONMENT_BLOCKED": "ENVIRONMENT_LIMITATION",
    "RESOURCE_BLOCKED": "ENVIRONMENT_LIMITATION",
    "IDENTITY_BLOCKED": "ARTIFACT_LIMITATION",
    "COMPARISON_BLOCKED": "COMPARISON_LIMITATION",
    "AUTHORIZATION_BLOCKED": "ENVIRONMENT_LIMITATION",
    "INCONCLUSIVE": "INCONCLUSIVE_EXECUTION",
    # Both of the following are NOT_INVESTIGATED on purpose. "We judged no experiment
    # necessary" is a decision about the ROUTE, not evidence about the paper: a missing
    # control is not settled by our declining to run something, it is settled by the
    # authors adding one. Mapping it to anything else would let the necessity axis leak
    # into the evidence axis and quietly resolve questions nobody checked.
    "NO_EXPERIMENT_NEEDED": "NOT_INVESTIGATED",
    # Same reasoning, one step earlier: the review reached a conclusion from the paper and
    # the investigation branch stopped. That is a fact about the REVIEW, not evidence
    # about this target, so it settles nothing on the evidence axis.
    "SUPERSEDED_BY_ESTABLISHED_FAILURE": "NOT_INVESTIGATED",
    "BUDGET_DEFERRED": "NOT_INVESTIGATED",
    "NOT_ATTEMPTED": "NOT_INVESTIGATED",
    "PENDING": "NOT_INVESTIGATED",
}

# Only these two evidence states are statements ABOUT THE PAPER. Everything else in
# `EVIDENCE_STATES` describes an artifact, a host, or a gate — invariants 4 to 7.
# PRIOR_ART_EVIDENCE is the fifth and the newest. A target claiming to be first at X
# and an earlier work stating that same narrow X is a statement about THIS document, so it
# belongs here — and it still reaches no stop, because `TargetOutcome.establishes_failure`
# names two dispositions and this is neither. Membership here decides what a review may
# say it examined, never what it may conclude.
EVIDENCE_ABOUT_THE_PAPER = ("REPRODUCTION_SUCCESS", "REPRODUCTION_FAILURE",
                            "PAPER_INTERNAL_EVIDENCE", "ARTIFACT_EVIDENCE",
                            "PRIOR_ART_EVIDENCE")


def classify(*, lens: str = "", discrepancy_type: str = "", baseline_class: str = "",
             candidate_class: str = "", is_artifact_finding: bool = False) -> str:
    """The scientific class of one finding, from its own typed self-classification.

    Vocabulary strings and booleans only, as in `harness.grading.derive` — no count, no
    number, no metric name, no paper identity, so "if <paper> appears, soften" and "if the
    delta exceeds 5 points, escalate" are inexpressible rather than merely absent.

    Order is most-specific-first. A finding that classified its own discrepancy has said
    more about itself than one that only carries a lens name, so that classification wins;
    the lens is the fallback.
    """
    if is_artifact_finding:
        return "IMPLEMENTATION_ISSUE"
    if (candidate_class or "") in _QUESTION_CLASSES:
        return "UNRESOLVED_QUESTION"

    d = (discrepancy_type or "").strip().upper()
    if d in _CONTRADICTION_TYPES:
        return "CONTRADICTION"
    if d == "UNCLEAR_REPORTING":
        # The paper's own reporting cannot be recovered well enough to check the number.
        # That is a gap in what was specified, not a disagreement between two statements.
        return "SPECIFICATION_GAP"
    if d == "DEFINITIONAL_MISMATCH":
        # It evaluated one quantity and drew the conclusion about another.
        return "MISSING_VALIDATION"

    b = (baseline_class or "").strip().upper()
    if b in _MISSING_MATTERS:
        return "MISSING_CONTROL"

    return _BY_LENS.get((lens or "").strip().lower(), "UNRESOLVED_QUESTION")


def evidence_state(disposition: str = "", provenance: str = "") -> str:
    """What the evidence route produced, from a target's terminal disposition.

    The provenance ceiling is applied HERE as well as at the reconciler, because this is
    the value a report renders. A FAILED_REPRODUCTION carrying a provenance the ceiling
    does not admit is not a reproduction failure at all — it is an inconclusive execution,
    and calling it anything else would let a synthesized diagnostic convict a paper
    through the reporting layer after being refused by the evidence layer.
    """
    d = (disposition or "").strip().upper()
    state = _EVIDENCE_FOR_DISPOSITION.get(d, "NOT_INVESTIGATED")
    if state in ("REPRODUCTION_SUCCESS", "REPRODUCTION_FAILURE") \
            and not _admits(provenance or ""):
        return "INCONCLUSIVE_EXECUTION"
    return state


def resolution_state(evidence: str = "") -> str:
    """Whether the question is closed, given what the evidence route produced.

    Deliberately derived rather than stored: a resolution that could be set independently
    of the evidence behind it is a resolution nobody checked.
    """
    e = (evidence or "").strip().upper()
    if e in ("REPRODUCTION_SUCCESS", "REPRODUCTION_FAILURE"):
        return "RESOLVED_BY_EXECUTION"
    if e == "PAPER_INTERNAL_EVIDENCE":
        return "RESOLVED_FROM_PAPER"
    if e == "PRIOR_ART_EVIDENCE":
        return "RESOLVED_FROM_LITERATURE"
    if e in ("ARTIFACT_EVIDENCE", "ARTIFACT_PROPERTY_ESTABLISHED"):
        # BOTH are resolutions — the narrow artifact question really is closed — and they
        # differ on the OTHER axis: only ARTIFACT_EVIDENCE is in EVIDENCE_ABOUT_THE_PAPER.
        # A review that reads only `resolution_status` sees a settled question either way,
        # which is correct; one that asks what was settled ABOUT THE PAPER gets the truth.
        return "RESOLVED_FROM_ARTIFACT"
    if e == "NOT_INVESTIGATED":
        return "NOT_INVESTIGATED"
    # CITATION_VERIFIED lands here, on UNRESOLVED, and that is the point of it: the
    # concern was looked at far enough to confirm it quotes the paper, and it is still
    # open. NOT_INVESTIGATED would say nobody looked; RESOLVED_* would say it was settled.
    return "UNRESOLVED"


# --- the fourth axis: WHICH REVIEW PATH this paper was on ------------------------------
# A referee's first question about a paper's checkability is whether the authors published
# anything to check, and the review could not answer it. `execution_provenance` reads
# SYNTHESIZED_DIAGNOSTIC for a paper that published no code, for a paper whose clone was
# refused by a gate, for a paper whose clone failed, and for a paper whose cloned repository
# was never authorized. Four opposite facts, one token — and `render_reviewer_report`
# mentioned the artifact nowhere at all, so the distinction was buried in a 21 KB trace.
#
# Only the FIRST of these is a fact about the paper. The other four are facts about a gate,
# a network, or this host, and none of them may colour a paper (invariant 17).
ARTIFACT_STATES = (
    "NO_ARTIFACT_ADVERTISED",     # the paper advertises no repository — about the PAPER
    "ARTIFACT_NOT_FETCHED",       # a gate was shut, so we did not look — about this harness
    "ARTIFACT_UNOBTAINABLE",      # the clone or the pin failed — about the network or the host
    "ARTIFACT_PRESENT_UNUSABLE",  # obtained; capability not established — about this host
    "ARTIFACT_PRESENT_USABLE",    # obtained and capable
    "ARTIFACT_UNASSESSED",        # acquisition never ran
)

# The one state that says something about the paper's own publication practice. Kept as
# its own tuple for the same reason `EVIDENCE_ABOUT_THE_PAPER` is.
ARTIFACT_ABOUT_THE_PAPER = ("NO_ARTIFACT_ADVERTISED",)

_ARTIFACT_FOR_ACQUISITION = {
    "unavailable": "NO_ARTIFACT_ADVERTISED",
    "blocked": "ARTIFACT_NOT_FETCHED",
    "failed": "ARTIFACT_UNOBTAINABLE",
    "cloned": "ARTIFACT_PRESENT_UNUSABLE",
    "cached": "ARTIFACT_PRESENT_UNUSABLE",
    # `synthesized` is NOT an artifact of the authors'. It is this harness's own probe
    # standing in for one, so the paper is still on the paper-only path however much code
    # is on disk — and calling it present would let a synthesized probe be reported as the
    # authors having published something.
    "synthesized": "NO_ARTIFACT_ADVERTISED",
    "not_attempted": "ARTIFACT_UNASSESSED",
    "": "ARTIFACT_UNASSESSED",
}


def artifact_state(acquisition_status: str = "", capability_established: bool = False) -> str:
    """Which review path this paper was on, from the acquisition and the capability.

    Vocabulary strings and a boolean only, as everywhere else in this module. Derived
    rather than stored, like `resolution_state`, so it cannot drift from the acquisition
    and capability it describes — a stored artifact state could say a repository was
    usable beside an acquisition that says it was never fetched.

    `ARTIFACT_PRESENT_USABLE` requires the capability check to have been ESTABLISHED, not
    merely to have not failed: "we obtained the code" and "the code can be given a fair
    run here" are different claims, and eleven independent refusals sit between them.
    """
    # EXACT lookup, no stripping and no case folding, for the same reason
    # `provenance.admits` is exact: the answer decides a reader-facing sentence about
    # whether the authors published code, and a token this table does not recognise must
    # fall to "we do not know" rather than be repaired into "present".
    state = _ARTIFACT_FOR_ACQUISITION.get(acquisition_status or "", "ARTIFACT_UNASSESSED")
    if state == "ARTIFACT_PRESENT_UNUSABLE" and capability_established:
        return "ARTIFACT_PRESENT_USABLE"
    return state


def artifact_concerns_the_paper(state: str) -> bool:
    """Does this artifact state say anything about the PAPER, as opposed to about a gate,
    a network or this host? Only one of the six does."""
    return (state or "") in ARTIFACT_ABOUT_THE_PAPER


def review_path(state: str) -> str:
    """PAPER_ONLY | PAPER_AND_ARTIFACT — the two paths, named.

    The A/B distinction requirement 2 asks to be explicit. A paper whose artifact was
    obtained is on a different review path from one whose artifact does not exist or could
    not be reached, and the difference decides what evidence is reachable at all: only the
    artifact path can ever produce an admissible reproduction, and only the paper-only path
    can reach the governed reconstruction route.
    """
    return ("PAPER_AND_ARTIFACT"
            if (state or "") in ("ARTIFACT_PRESENT_USABLE", "ARTIFACT_PRESENT_UNUSABLE")
            else "PAPER_ONLY")


def concerns_the_paper(evidence: str) -> bool:
    """Does this evidence state say anything about the PAPER, as opposed to about an
    artifact, a host, or one of this harness's own gates? Invariants 4 to 7 in one line."""
    return (evidence or "").strip().upper() in EVIDENCE_ABOUT_THE_PAPER


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    # --- the signature is the invariant ------------------------------------------------
    for name, p in inspect.signature(classify).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"

    # --- axis 1: most specific wins ----------------------------------------------------
    assert classify(lens="protocol", discrepancy_type="ARITHMETIC_ERROR") == "CONTRADICTION"
    assert classify(lens="overclaim") == "OVERSTATED_CLAIM"
    assert classify(lens="confound") == "CONFOUND"
    assert classify(lens="protocol") == "PROTOCOL_ISSUE"
    assert classify(lens="protocol", baseline_class="CENTRAL_VALIDITY_THREAT") == "MISSING_CONTROL"
    assert classify(lens="overclaim", discrepancy_type="DEFINITIONAL_MISMATCH") == "MISSING_VALIDATION"
    assert classify(lens="contradiction", discrepancy_type="UNCLEAR_REPORTING") == "SPECIFICATION_GAP"
    assert classify(is_artifact_finding=True) == "IMPLEMENTATION_ISSUE"
    assert classify(lens="overclaim", candidate_class="OPEN_QUESTION") == "UNRESOLVED_QUESTION"
    assert classify(lens="overclaim", candidate_class="DISMISSED") == "UNRESOLVED_QUESTION"
    assert classify() == "UNRESOLVED_QUESTION", "an unclassifiable finding is a question"
    # a lens saying the absence does not matter must NOT become MISSING_CONTROL
    assert classify(lens="protocol", baseline_class="OPTIONAL_COMPARISON") == "PROTOCOL_ISSUE"
    assert set(_BY_LENS.values()) <= set(SCIENTIFIC_CLASSES)
    # NO_MATERIAL_ISSUE_FOUND is a paper-level state and no finding may be given it
    for lens in list(_BY_LENS) + ["", "unknown"]:
        for d in list(_CONTRADICTION_TYPES) + ["UNCLEAR_REPORTING", "DEFINITIONAL_MISMATCH", ""]:
            for b in list(_MISSING_MATTERS) + ["OPTIONAL_COMPARISON", ""]:
                for c in list(_QUESTION_CLASSES) + ["CONFIRMED_FINDING", ""]:
                    for art in (True, False):
                        got = classify(lens=lens, discrepancy_type=d, baseline_class=b,
                                       candidate_class=c, is_artifact_finding=art)
                        assert got in SCIENTIFIC_CLASSES, got
                        assert got != "NO_MATERIAL_ISSUE_FOUND", (lens, d, b, c, art)

    # --- axis 3: the provenance ceiling is enforced at the reporting layer too ---------
    assert evidence_state("REPRODUCED", "repo_exec") == "REPRODUCTION_SUCCESS"
    assert evidence_state("FAILED_REPRODUCTION", "driver") == "REPRODUCTION_FAILURE"
    for prov in ("synthesized", "template", "", "paper"):
        assert evidence_state("FAILED_REPRODUCTION", prov) == "INCONCLUSIVE_EXECUTION", prov
        assert evidence_state("REPRODUCED", prov) == "INCONCLUSIVE_EXECUTION", prov
    assert evidence_state("ENVIRONMENT_BLOCKED") == "ENVIRONMENT_LIMITATION"
    assert evidence_state("SPECIFICATION_BLOCKED") == "SPECIFICATION_LIMITATION"
    assert evidence_state("ARTIFACT_BLOCKED") == "ARTIFACT_LIMITATION"
    assert evidence_state("ADDRESSING_BLOCKED") == "EXTRACTION_LIMITATION", (
        "a limit of our extraction is not a missing artifact")
    assert not concerns_the_paper("EXTRACTION_LIMITATION")
    assert evidence_state("NOT_ATTEMPTED") == "NOT_INVESTIGATED"
    # a necessity decision is not evidence: declining to run something resolves nothing
    assert evidence_state("NO_EXPERIMENT_NEEDED") == "NOT_INVESTIGATED"
    assert evidence_state("BUDGET_DEFERRED") == "NOT_INVESTIGATED"
    # nor is verifying that a concern quotes the paper accurately. A precondition every
    # kept finding already meets (invariant 1) is not a second, per-target resolution —
    # and it is not "nothing was pursued" either, so it has its own state and that state
    # leaves the question OPEN.
    for prov in ("paper", "", "driver", "repo_exec"):
        assert evidence_state("CITATION_VERIFIED_ONLY", prov) == "CITATION_VERIFIED", prov
        assert resolution_state(evidence_state("CITATION_VERIFIED_ONLY", prov)) \
            == "UNRESOLVED", prov
        assert not concerns_the_paper(evidence_state("CITATION_VERIFIED_ONLY", prov))

    # A contradicted printed composition is the OPPOSITE of a verified citation, and the
    # two must never derive the same way: this one IS evidence about the paper, and IS
    # resolved by it — unconditionally, by disposition alone, never gated on provenance
    # the way REPRODUCTION_SUCCESS/FAILURE are, because "paper" provenance is not and must
    # never become a member of the reproduction ceiling.
    for prov in ("paper", "", "driver", "repo_exec", "reimpl_exec", "synthesized"):
        assert evidence_state("PAPER_ARITHMETIC_CONTRADICTION", prov) \
            == "PAPER_INTERNAL_EVIDENCE", prov
        assert resolution_state(evidence_state("PAPER_ARITHMETIC_CONTRADICTION", prov)) \
            == "RESOLVED_FROM_PAPER", prov
        assert concerns_the_paper(evidence_state("PAPER_ARITHMETIC_CONTRADICTION", prov)), prov

    # The three refusals that used to share EXTRACTION_LIMITATION are three states, and
    # only the first is a limit of our own extraction.
    assert evidence_state("ADDRESSING_BLOCKED") == "EXTRACTION_LIMITATION"
    assert evidence_state("REPORTING_BLOCKED") == "REPORTING_LIMITATION"
    assert evidence_state("NO_ROUTE_AVAILABLE") == "NO_ROUTE_AVAILABLE"
    assert len({evidence_state(d) for d in
                ("ADDRESSING_BLOCKED", "REPORTING_BLOCKED", "NO_ROUTE_AVAILABLE")}) == 3
    for d in ("REPORTING_BLOCKED", "NO_ROUTE_AVAILABLE"):
        assert resolution_state(evidence_state(d)) == "UNRESOLVED", d
        assert not concerns_the_paper(evidence_state(d)), d
    assert set(_EVIDENCE_FOR_DISPOSITION.values()) <= set(EVIDENCE_STATES)
    # every disposition the artifacts vocabulary admits has an entry here, so a new one
    # cannot reach the reporting layer through the `NOT_INVESTIGATED` default unnoticed.
    from .artifacts import TARGET_DISPOSITIONS
    missing = [d for d in TARGET_DISPOSITIONS if d not in _EVIDENCE_FOR_DISPOSITION]
    assert not missing, f"dispositions with no evidence state: {missing}"

    # --- only two states say anything about the paper ----------------------------------
    for e in ("ENVIRONMENT_LIMITATION", "ARTIFACT_LIMITATION", "SPECIFICATION_LIMITATION",
              "EXTRACTION_LIMITATION", "INCONCLUSIVE_EXECUTION", "NOT_INVESTIGATED"):
        assert not concerns_the_paper(e), e
    for e in ("REPRODUCTION_SUCCESS", "REPRODUCTION_FAILURE"):
        assert concerns_the_paper(e), e

    # --- axis 4: which review path, and only one of six is about the paper ------------
    assert artifact_state("unavailable") == "NO_ARTIFACT_ADVERTISED"
    assert artifact_state("blocked") == "ARTIFACT_NOT_FETCHED"
    assert artifact_state("failed") == "ARTIFACT_UNOBTAINABLE"
    assert artifact_state("cloned") == "ARTIFACT_PRESENT_UNUSABLE"
    assert artifact_state("cloned", capability_established=True) == "ARTIFACT_PRESENT_USABLE"
    assert artifact_state("cached", capability_established=True) == "ARTIFACT_PRESENT_USABLE"
    assert artifact_state("") == "ARTIFACT_UNASSESSED"
    assert artifact_state("not_attempted") == "ARTIFACT_UNASSESSED"
    # a probe this harness synthesised is not the authors publishing code
    assert artifact_state("synthesized") == "NO_ARTIFACT_ADVERTISED"
    assert artifact_state("synthesized", capability_established=True) \
        == "NO_ARTIFACT_ADVERTISED"
    # an unrecognised acquisition status must not read as an artifact being present
    for junk in ("who knows", "CLONED", " cloned"):
        assert artifact_state(junk) == "ARTIFACT_UNASSESSED", junk
        assert artifact_state(junk, capability_established=True) == "ARTIFACT_UNASSESSED"
    assert set(_ARTIFACT_FOR_ACQUISITION.values()) <= set(ARTIFACT_STATES)
    # exactly one of the six says anything about the paper
    about = [s for s in ARTIFACT_STATES if artifact_concerns_the_paper(s)]
    assert about == ["NO_ARTIFACT_ADVERTISED"], about
    # and the two paths partition the vocabulary
    assert {review_path(s) for s in ARTIFACT_STATES} == {"PAPER_ONLY", "PAPER_AND_ARTIFACT"}
    assert review_path("ARTIFACT_PRESENT_USABLE") == "PAPER_AND_ARTIFACT"
    assert review_path("NO_ARTIFACT_ADVERTISED") == "PAPER_ONLY"
    assert review_path("ARTIFACT_UNOBTAINABLE") == "PAPER_ONLY", (
        "a repository we could not reach leaves the review on the paper-only path")
    for name, p in inspect.signature(artifact_state).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"

    # --- axis 2 follows axis 3 and cannot be set apart from it -------------------------
    assert resolution_state("REPRODUCTION_FAILURE") == "RESOLVED_BY_EXECUTION"
    assert resolution_state("PAPER_INTERNAL_EVIDENCE") == "RESOLVED_FROM_PAPER"
    assert resolution_state("ARTIFACT_EVIDENCE") == "RESOLVED_FROM_ARTIFACT"
    assert resolution_state("ENVIRONMENT_LIMITATION") == "UNRESOLVED"
    assert resolution_state("NOT_INVESTIGATED") == "NOT_INVESTIGATED"
    assert set(RESOLUTION_STATES) >= {resolution_state(e) for e in EVIDENCE_STATES}

    # --- the three axes are independent ------------------------------------------------
    # the same scientific class under opposite evidence states, and vice versa
    assert classify(lens="confound") == "CONFOUND"
    assert evidence_state("PAPER_ONLY_RESOLVED", "paper") != evidence_state(
        "ENVIRONMENT_BLOCKED")
    print("harness.taxonomy self-check ok")


if __name__ == "__main__":
    _self_check()
