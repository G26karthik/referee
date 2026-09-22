"""What kind of scientific problem a finding is — kept apart from what was done about it.

Three independent axes, each a property of a different thing:

    SCIENTIFIC CLASS   what kind of problem this is (overstated claim, contradiction,
                       confound...) — a property of the paper's argument.
    RESOLUTION STATE   whether the question it raises was settled, and by what — a
                       property of the review process.
    EVIDENCE STATE     what the evidence route actually produced — a property of the world.

A single label of any kind would collapse two of these: a paper can carry a CONFOUND that
is RESOLVED_FROM_PAPER with evidence NOT_INVESTIGATED, and another that is UNRESOLVED
because the artifact was missing.

`classify` is most-specific-first: a finding that classified its own discrepancy or
missing baseline yields a class derived from that; one that did neither falls through to
`_BY_LENS`, so its class is little more than a restatement of which lens raised it. That
ceiling is real and visible in the artifact, and kept because the alternative — an
unclassified bucket — hides the same fact.

Nothing here is a severity or a verdict: this reads only closed-vocabulary fields the
harness has already derived or clamped, no prose/number/metric name/paper identity, so a
paper-specific or threshold-shaped rule is inexpressible here (as in `harness.grading
.derive` and `harness.priority.score`).

`python -m harness.taxonomy` runs the self-check.
"""
from __future__ import annotations

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
    # PAPER-level, deliberately unreachable from `classify` (no finding may carry it; a
    # finding is by definition something a lens raised). Kept in the vocabulary so a
    # report/aggregate can say "nothing material" in one word; asserted unreachable below.
    "NO_MATERIAL_ISSUE_FOUND",  # nothing of the above, within what was checked
)

# --- axis 2: was the question settled, and by what ------------------------------------
RESOLUTION_STATES = (
    "RESOLVED_FROM_PAPER",      # the paper's own printed content settled it
    "RESOLVED_FROM_ARTIFACT",   # static inspection of the released code settled it
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
    # A bounded fact about the CHECKOUT, not the paper — its own state because
    # ARTIFACT_EVIDENCE is in EVIDENCE_ABOUT_THE_PAPER and this must not be (e.g.
    # "the checkout advertises evaluate.py" settles nothing about the document).
    "ARTIFACT_PROPERTY_ESTABLISHED",
    # Both locations verified, the relationship not — the artifact-route analogue of
    # CITATION_VERIFIED, for the same reason: ARTIFACT_EVIDENCE would claim the paper was
    # checked, NOT_INVESTIGATED would claim nobody looked.
    "ARTIFACT_ENDPOINTS_VERIFIED",
    "ARTIFACT_LIMITATION",      # no usable artifact for this question
    "EXTRACTION_LIMITATION",    # this harness could not build an address for the claim
    "REPORTING_LIMITATION",     # the paper prints no unambiguous quantity to compare against
    "NO_ROUTE_AVAILABLE",       # addressed and quantified; no route this system has applies
    # A route applied and its comparison did not — its own state because NO_ROUTE_AVAILABLE
    # would report a limit of our arithmetic as a limit of our method inventory instead.
    "COMPARISON_LIMITATION",    # a route applied; its result had nothing to be held against
    # The CITATION was checked, not the claim — its own state since PAPER_INTERNAL_EVIDENCE
    # would overclaim and NOT_INVESTIGATED would underclaim.
    "CITATION_VERIFIED",        # the concern quotes the paper accurately; the concern stands
    "ENVIRONMENT_LIMITATION",   # this host could not mount the experiment
    "SPECIFICATION_LIMITATION",  # the paper does not specify enough to build it
    "INCONCLUSIVE_EXECUTION",   # it ran and settled nothing
    "NOT_INVESTIGATED",         # nothing was pursued
    # EXACT_CERTIFICATE's own pair. VIOLATION is about the paper — an admissible
    # counterexample, exactly as REPRODUCTION_FAILURE is for a printed cell.
    # NO_VIOLATION is checking finitely many instances, never a statement the bound holds,
    # so it stays out of `EVIDENCE_ABOUT_THE_PAPER` below.
    "CERTIFICATE_VIOLATION",
    "CERTIFICATE_NO_VIOLATION",
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
    # Re-verifying the quotation behind a concern establishes only that it cites the paper
    # accurately, nothing about whether the concern is right — so CITATION_VERIFIED, not
    # PAPER_INTERNAL_EVIDENCE (which would double-count invariant 1's precondition as a
    # resolution).
    "CITATION_VERIFIED_ONLY": "CITATION_VERIFIED",
    # The paper's own printed composition was recomputed and did not evaluate — real
    # paper-internal evidence, unlike the citation recheck above.
    "PAPER_ARITHMETIC_CONTRADICTION": "PAPER_INTERNAL_EVIDENCE",
    # A bound paper/artifact mismatch IS a statement about the document (still excluded
    # from `establishes_failure`: code disagreeing with the method section is not the
    # reported number being false).
    "ARTIFACT_MISMATCH_ESTABLISHED": "ARTIFACT_EVIDENCE",
    # A bounded fact about the checkout, not the paper (e.g. a file exists).
    "ARTIFACT_FACT_ESTABLISHED": "ARTIFACT_PROPERTY_ESTABLISHED",
    # Both ends located, the correspondence the auditor's reading — resolves to
    # UNRESOLVED, exactly as CITATION_VERIFIED does.
    "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS": "ARTIFACT_ENDPOINTS_VERIFIED",
    "ARTIFACT_INSPECTION_INCONCLUSIVE": "COMPARISON_LIMITATION",
    "SPECIFICATION_BLOCKED": "SPECIFICATION_LIMITATION",
    "ARTIFACT_BLOCKED": "ARTIFACT_LIMITATION",
    # NOT ARTIFACT_LIMITATION: "could not build an address for this claim" is a limit of
    # our own extraction, not a missing artifact.
    "ADDRESSING_BLOCKED": "EXTRACTION_LIMITATION",
    "REPORTING_BLOCKED": "REPORTING_LIMITATION",
    "NO_ROUTE_AVAILABLE": "NO_ROUTE_AVAILABLE",
    "ENVIRONMENT_BLOCKED": "ENVIRONMENT_LIMITATION",
    "RESOURCE_BLOCKED": "ENVIRONMENT_LIMITATION",
    "IDENTITY_BLOCKED": "ARTIFACT_LIMITATION",
    "COMPARISON_BLOCKED": "COMPARISON_LIMITATION",
    "AUTHORIZATION_BLOCKED": "ENVIRONMENT_LIMITATION",
    "INCONCLUSIVE": "INCONCLUSIVE_EXECUTION",
    # A necessity decision ("no experiment needed") is not evidence about the paper — it
    # must not leak into the evidence axis and quietly resolve a question nobody checked.
    "NO_EXPERIMENT_NEEDED": "NOT_INVESTIGATED",
    "SUPERSEDED_BY_ESTABLISHED_FAILURE": "NOT_INVESTIGATED",
    "BUDGET_DEFERRED": "NOT_INVESTIGATED",
    "NOT_ATTEMPTED": "NOT_INVESTIGATED",
    "PENDING": "NOT_INVESTIGATED",
    # An admissible exact-arithmetic counterexample IS evidence about the paper's stated
    # theorem — maps like FAILED_REPRODUCTION/PAPER_ARITHMETIC_CONTRADICTION above.
    "COUNTEREXAMPLE_ESTABLISHED": "CERTIFICATE_VIOLATION",
    # Checked, no violation among tested instances — NEVER support; CITATION_VERIFIED's
    # analogue, not PAPER_INTERNAL_EVIDENCE's.
    "NO_COUNTEREXAMPLE_FOUND": "CERTIFICATE_NO_VIOLATION",
}

# Only these five evidence states are statements ABOUT THE PAPER; everything else in
# `EVIDENCE_STATES` describes an artifact, a host, or a gate — invariants 4 to 7.
# Membership here decides what a review may say it examined, never what it may conclude.
EVIDENCE_ABOUT_THE_PAPER = ("REPRODUCTION_SUCCESS", "REPRODUCTION_FAILURE",
                            "PAPER_INTERNAL_EVIDENCE", "ARTIFACT_EVIDENCE",
                            "CERTIFICATE_VIOLATION")

# WAS THIS TARGET'S CLAIM CHECKED AT ALL? Derived here, rather than kept as a hand-written
# set of dispositions elsewhere, so it cannot drift as routes are added. The predicate is
# `EVIDENCE_ABOUT_THE_PAPER` plus `INCONCLUSIVE_EXECUTION`/`CERTIFICATE_NO_VIOLATION`: both
# are routes that genuinely ran and checked something (a citation's quotation, a
# certificate's tested instances) without settling anything ABOUT THE PAPER — checked, but
# not evidence about the paper, which is a different sentence from "nothing was run".
_CHECKED_EVIDENCE = EVIDENCE_ABOUT_THE_PAPER + ("INCONCLUSIVE_EXECUTION",
                                                "CERTIFICATE_NO_VIOLATION")


def claim_was_checked(evidence: str = "") -> bool:
    """Did anything bear on THE PAPER'S claim here, or is this a target nothing reached?
    Deliberately NOT "was evidence gathered" and NOT "was the question settled" — some
    routes gather evidence that resolves nothing about the paper."""
    return (evidence or "").strip().upper() in _CHECKED_EVIDENCE


def classify(*, lens: str = "", discrepancy_type: str = "", baseline_class: str = "",
             candidate_class: str = "", is_artifact_finding: bool = False) -> str:
    """The scientific class of one finding, from its own typed self-classification.
    Vocabulary strings and booleans only — no count, number, metric name or paper
    identity, so a paper-specific or threshold rule is inexpressible. Most-specific-first:
    a finding that classified its own discrepancy wins over one that only carries a lens
    name, which is the fallback."""
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
    """What the evidence route produced, from a target's terminal disposition. The
    provenance ceiling is applied HERE too, not just at the reconciler: a
    FAILED_REPRODUCTION on inadmissible provenance is an inconclusive execution."""
    d = (disposition or "").strip().upper()
    state = _EVIDENCE_FOR_DISPOSITION.get(d, "NOT_INVESTIGATED")
    if state in ("REPRODUCTION_SUCCESS", "REPRODUCTION_FAILURE", "CERTIFICATE_VIOLATION") \
            and not _admits(provenance or ""):
        return "INCONCLUSIVE_EXECUTION"
    return state


def resolution_state(evidence: str = "") -> str:
    """Whether the question is closed, given what the evidence route produced.

    Deliberately derived rather than stored: a resolution that could be set independently
    of the evidence behind it is a resolution nobody checked.
    """
    e = (evidence or "").strip().upper()
    if e in ("REPRODUCTION_SUCCESS", "REPRODUCTION_FAILURE", "CERTIFICATE_VIOLATION"):
        return "RESOLVED_BY_EXECUTION"
    if e == "PAPER_INTERNAL_EVIDENCE":
        return "RESOLVED_FROM_PAPER"
    if e in ("ARTIFACT_EVIDENCE", "ARTIFACT_PROPERTY_ESTABLISHED"):
        # Both are resolutions — the narrow artifact question really is closed — and
        # differ on the OTHER axis: only ARTIFACT_EVIDENCE is in EVIDENCE_ABOUT_THE_PAPER.
        return "RESOLVED_FROM_ARTIFACT"
    if e == "NOT_INVESTIGATED":
        return "NOT_INVESTIGATED"
    # CITATION_VERIFIED lands here: looked at far enough to confirm the quote, still open.
    return "UNRESOLVED"


# --- the fourth axis: WHICH REVIEW PATH this paper was on ------------------------------
# Whether the authors published anything to check. Only the FIRST state below is a fact
# about the paper; the other five are facts about a gate, a network, or this host, and
# none of them may colour a paper (invariant 17).
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
    # `synthesized` is NOT an artifact of the authors' — it is this harness's own probe, so
    # the paper stays on the paper-only path however much code is on disk.
    "synthesized": "NO_ARTIFACT_ADVERTISED",
    "not_attempted": "ARTIFACT_UNASSESSED",
    "": "ARTIFACT_UNASSESSED",
}


def artifact_state(acquisition_status: str = "", capability_established: bool = False) -> str:
    """Which review path this paper was on, from the acquisition and the capability.
    Derived rather than stored, so it cannot drift from what it describes.
    `ARTIFACT_PRESENT_USABLE` requires the capability check to have been ESTABLISHED, not
    merely to have not failed."""
    # EXACT lookup, no stripping/case-folding — an unrecognised token falls to "we do not
    # know" rather than being repaired into "present".
    state = _ARTIFACT_FOR_ACQUISITION.get(acquisition_status or "", "ARTIFACT_UNASSESSED")
    if state == "ARTIFACT_PRESENT_UNUSABLE" and capability_established:
        return "ARTIFACT_PRESENT_USABLE"
    return state


def artifact_concerns_the_paper(state: str) -> bool:
    """Does this artifact state say anything about the PAPER, as opposed to about a gate,
    a network or this host? Only one of the six does."""
    return (state or "") in ARTIFACT_ABOUT_THE_PAPER


def review_path(state: str) -> str:
    """PAPER_ONLY | PAPER_AND_ARTIFACT — the two paths, named. The difference decides what
    evidence is reachable at all: only the artifact path can produce an admissible
    reproduction; only the paper-only path can reach the governed reconstruction route."""
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

    # --- EXACT_CERTIFICATE: a counterexample gated by the SAME provenance ceiling as a
    # repo/reimpl reproduction failure; a checked-but-clean certificate never counts as
    # support, exactly like CITATION_VERIFIED ------------------------------------------
    assert evidence_state("COUNTEREXAMPLE_ESTABLISHED", "cert_exec") == "CERTIFICATE_VIOLATION"
    assert concerns_the_paper(evidence_state("COUNTEREXAMPLE_ESTABLISHED", "cert_exec"))
    assert resolution_state(evidence_state("COUNTEREXAMPLE_ESTABLISHED", "cert_exec")) \
        == "RESOLVED_BY_EXECUTION"
    for prov in ("synthesized", "template", "", "paper"):
        assert evidence_state("COUNTEREXAMPLE_ESTABLISHED", prov) == "INCONCLUSIVE_EXECUTION", prov
    for prov in ("paper", "", "driver", "repo_exec", "cert_exec"):
        assert evidence_state("NO_COUNTEREXAMPLE_FOUND", prov) == "CERTIFICATE_NO_VIOLATION", prov
        assert not concerns_the_paper(evidence_state("NO_COUNTEREXAMPLE_FOUND", prov)), prov
        assert resolution_state(evidence_state("NO_COUNTEREXAMPLE_FOUND", prov)) == "UNRESOLVED", prov
        assert claim_was_checked(evidence_state("NO_COUNTEREXAMPLE_FOUND", prov)), (
            "a certificate that genuinely ran must count as CHECKED even though it is not "
            "support")

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
    from .schema import TARGET_DISPOSITIONS
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
