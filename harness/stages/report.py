"""S4 — rank the findings and render the evaluation report.

Two hard rules, both inherited from what the research pipeline learned:

  1. NO LLM DECIDES THE VERDICT. Severity ordering and the binary RED/GREEN call are
     a lexicographic sort and a materiality table in plain Python. A model that ranks
     its own findings will rank them differently on Tuesday; an editor needs the same
     paper to get the same verdict every time, and needs to be able to argue with the
     rule rather than with a mood.

  2. THE RENDERER COPIES, IT DOES NOT WRITE. `render_eval_report` is a pure function
     over the artifacts — no LLM, no network, no Config. Every number and every quote
     in the output was already in a `Finding`, put there by a lens that had to quote
     it from the paper. Nothing is generated at render time, so nothing can be
     hallucinated at render time.

`python -m harness.stages.report` runs the self-check.
"""
from __future__ import annotations

import re
from collections import Counter

from .. import coverage as coverage_mod
from .. import disposition as disposition_mod
from .. import docintegrity
from .. import guarantees as guarantees_mod
from .. import materiality
from .. import outcome as outcome_mod
from .. import provenance as provenance_mod
from .. import taxonomy
from .. import selfaudit, state
from .. import artifact_evidence
from .. import artifacts as artifacts_mod
from ..artifacts import (CodeAudit, CodeAuditFinding, EvalReport, ExperimentalChain, Finding,
                         LensReport, PaperDoc, ProbeResult, Reconciliation, RepoAcquisition)
from ..config import Config
from . import audit as audit_stage

# Severity → rank. Also the display order of the findings table. NOTE ranks below
# MINOR and, deliberately, is not one of the keys `overall_verdict` sums over — a NOTE
# can be displayed but can never cross a threshold (see `harness.artifacts.SEVERITIES`).
_SEVERITY_RANK = {"FATAL": 2, "MAJOR": 1, "MINOR": 0, "NOTE": -1}

# Tiebreak only, applied AFTER severity and evidence strength. Lenses that cite the
# paper against itself sort above lenses that argue about methodology, because an
# editor can check the first kind in seconds.
_LENS_RANK = {"overclaim": 3, "contradiction": 2, "confound": 1, "protocol": 0}

# The materiality table. Stated as data so the rule is inspectable and arguable, exactly
# as the RED/YELLOW/GREEN threshold table it replaces was.
#
# WHAT CHANGED, AND WHY COUNTING HAD TO GO. The old table reached RED by ACCUMULATION:
# three MAJORs from one lens, or ten across all four. That makes the paper-level decision
# a function of how many things a panel of readers chose to write down, which is a
# property of the panel, not of the paper. Two lenses that happen to phrase the same
# doubt separately move a verdict; one that phrases two doubts together does not. There
# is no scale on which "10 MAJORs" is a discovery and "9" is not.
#
# The decision is now binary and definitional, not cumulative:
#
#   RED   = a scientifically material failure has been ESTABLISHED, by evidence strong
#           enough to REJECT the relevant claim.
#   GREEN = no such failure has been established WITHIN THE AUDITED SCOPE.
#
# GREEN is therefore not a certificate. It does not mean the paper is correct, it means
# this audit did not establish that it is wrong — which is why `claim_status` keeps
# VERIFIED_SUPPORT and NOT_VERIFIED apart underneath, and why the report prints the
# reproduction status beside the colour instead of folding it in.
#
# Only two things clear the bar, and neither is a count:
#
#   1. A failed reproduction, from a provenance the ceiling admits. Arithmetic against
#      a printed cell, not a judgement.
# A model-assigned severity is an attention signal, not rejection authority. Quotation
# verification establishes that the cited span exists, and grading can cap severity, but
# neither machine-establishes a paper-owned dependency from that span to a central claim.
# Only `material_target_failure` may establish a paper-level failure.
MATERIAL_SEVERITY: tuple[str, ...] = ()
CONCERN_SEVERITY = ("FATAL", "MAJOR")

# The internal epistemic states. RED/GREEN is a projection of these, not a replacement
# for them: only VERIFIED_FAILURE is RED, and BOTH of the other two are GREEN, because
# "we checked and it held" and "we could not check" are the same decision about the
# paper even though they are opposite states of knowledge. The report must never let
# them look alike, so it prints this field verbatim.
CLAIM_STATUSES = ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED")

SUPPORT_LANGUAGE = ("verified", "supported", "confirmed", "validated", "corroborated")
EVIDENCED_SUPPORT = ("VERIFIED_SUPPORT",)

# How much experimental evidence stands behind the decision. NOT_ATTEMPTED and
# NOT_VERIFIED are kept apart deliberately — "no experiment was run" and "an experiment
# ran and settled nothing" are different facts about the audit, and collapsing them is
# how a report starts implying it tried harder than it did.
REPRODUCTION_STATUSES = ("REPRODUCED", "FAILED_REPRODUCTION", "NOT_VERIFIED", "NOT_ATTEMPTED")


def support_is_evidenced(claim_status: str) -> bool:
    """May this report describe the paper as verified/supported/confirmed?

    True only when something was positively checked and held. Absence of an established
    failure is not support, and this predicate is what keeps the two apart everywhere the
    report speaks about the paper as a whole.
    """
    return claim_status in EVIDENCED_SUPPORT


def unearned_support_language(text: str, claim_status: str) -> list[str]:
    """Every support word used in `text` that `claim_status` has not earned.

    Scoped to DECISION-LEVEL prose — the badge, the reason, the gloss, the Decision
    table. It is deliberately not run over the whole report, because "cell_verified",
    "(not verified)" and "confirmed findings" are legitimate elsewhere: they describe an
    evidence class, a disclaimer, and a finding bucket respectively, none of which is a
    claim that the PAPER was verified. Empty when support is evidenced.
    """
    if support_is_evidenced(claim_status):
        return []
    # This scans PROSE, so the harness's own status tokens come out first. `NOT_VERIFIED`,
    # `VERIFIED_FAILURE` and `FAILED_REPRODUCTION` all contain a support word while
    # claiming no support whatever — they are the vocabulary the Decision block prints
    # verbatim on purpose, and a detector that flags the disclaimer it exists to require
    # is just a broken detector. "not verified" is likewise the report saying so.
    low = text.lower()
    for token in (*CLAIM_STATUSES, *REPRODUCTION_STATUSES):
        low = low.replace(token.lower(), " ")
    return [w for w in SUPPORT_LANGUAGE if re.search(rf"(?<!not ){w}", low)]


# GREEN MAY NOT BORROW THE WORDS OF EVIDENCE IT DOES NOT HAVE.
#
# Two very different papers are both GREEN: one whose printed cell an executed metric
# actually reconciled with, and one nothing could be established about at all. Calling
# either of them "verified", "supported" or "confirmed" is only true of the first, and
# the second is the overwhelmingly common case — so the failure mode is not rare, it is
# the default. A GREEN misread as a clean bill of health is the single most consequential
# misreading this system can produce, because it is the one that gets quoted.
#
# `VERIFIED_SUPPORT` is the ONLY state that has earned this vocabulary, and it earns it
# from an executed reconciliation, never from an absence of findings. The words are named
# here rather than left implicit so a test can sweep every state and prove the decision
# text uses them nowhere else.
# The provenance ceiling, enforced a SECOND time here. `local_exec.reconcile` already
# refuses to emit a verdict for anything outside this set, so in a correct system no
# inadmissible reconciliation ever reaches this module — which is precisely why the check
# belongs here too. It was not here, and a `FAILED_REPRODUCTION` carrying `synthesized`,
# `template` or a garbage provenance returned RED: a paper-independent placebo, or a
# hand-edited probe_results.json, convicting a paper. The old code even detected the case
# and printed "treat it as a harness defect" while still returning the conviction, so the
# report contradicted its own verdict. One upstream bug was all that stood between a
# diagnostic and a published RED.
ADMISSIBLE_REPRODUCTION_PROVENANCE = provenance_mod.ADMISSIBLE_REPRODUCTION_PROVENANCE

# What actually ran, in the vocabulary a reader needs rather than the internal token.
# Fails closed: anything unrecognised reads as a diagnostic, never as author code, so a
# provenance this map has not been taught about cannot be reported as a reproduction.
# THE SAME OBJECT as `harness.provenance.PROVENANCE_LABEL`, not a copy — the label table
# and the ceiling it labels have to agree, and two tables agree only by luck.
PROVENANCE_LABEL = provenance_mod.PROVENANCE_LABEL

_CELL_REF = re.compile(r"^T\d+:r\d+:c\d+$")
# Salvaged from the retired grounding tournament: pull the leading magnitude out of
# free text an LLM wrote ("+3-5% top-1", "0.8pp", "~2.5 points") and DEGRADE TO 0.0
# on anything unparseable. Never raises — a bad string must sort last, never invert
# the order and never sink the report.
_MAGNITUDE = re.compile(r"[-+]?\d*\.?\d+")

MAX_TABLE_ROWS = 14
MAX_THREAT_BULLETS = 6
_QUOTE_CHARS = 240

# The two sentences a reader most often gets wrong, written once so every report says
# them the same way. Both spell out what the state does NOT mean, because that is the
# half that gets dropped when a colour is quoted out of context.
_DECISION_GLOSS = {
    "VERIFIED_FAILURE": "A material failure was **established**: the evidence below is strong "
                        "enough to reject the claim it addresses. This is not a count of concerns.",
    "VERIFIED_SUPPORT": "Something was **positively checked and held** — an executed metric "
                        "reconciled with a printed cell. That is support for the cell that was "
                        "tested, not for the paper as a whole.",
    "NOT_VERIFIED": "**Nothing was established in either direction** within the audited scope. "
                    "This is not a finding that the paper is correct, and it is not a finding "
                    "that it is wrong — it is the honest state when the evidence available did "
                    "not settle the question.",
}

# What positive evidence stands behind the decision, stated as its own row so a GREEN can
# never appear without it. `NOT_VERIFIED` says "none" in the first three words, because a
# reader skimming one line is the reader most likely to take GREEN for a clean bill of
# health. Only the VERIFIED_SUPPORT entry is allowed to use the word "verified" at all —
# see SUPPORT_LANGUAGE.
_SUPPORT_ROW = {
    "VERIFIED_FAILURE": "n/a — this decision rests on an established failure, below",
    "VERIFIED_SUPPORT": "yes — an executed metric reconciled with a printed cell",
    "NOT_VERIFIED": "**none** — this decision rests on the ABSENCE of an established "
                    "failure, not on evidence that the paper is sound",
}
_REPRO_GLOSS = {
    "REPRODUCED": "An experiment ran and its metric reconciled with the paper's printed value.",
    "FAILED_REPRODUCTION": "An experiment ran, started successfully, and its metric did not "
                           "reconcile with the paper's printed value.",
    "NOT_VERIFIED": "An experiment was attempted and settled nothing — see the chain below for "
                    "the link that broke. An infrastructure or capability limit never counts "
                    "against the paper.",
    "NOT_ATTEMPTED": "No reproduction was attempted, so no experimental evidence stands behind "
                     "this decision either way.",
}


def parse_magnitude(text: str) -> float:
    if not text:
        return 0.0
    m = _MAGNITUDE.search(str(text))
    if not m:
        return 0.0
    try:
        return abs(float(m.group()))
    except (TypeError, ValueError):
        return 0.0


def counted(f: Finding) -> str:
    """The severity `overall_verdict` actually counts for `f`.

    Falls back to the lens's own `severity` whenever `counted_severity` is empty —
    which is exactly what makes turning grading off (or never running it) reproduce
    the pre-grading verdict byte for byte: nothing capped it, so there is nothing to
    fall back FROM. See `harness/grading.py:derive` for what can set it, and its one
    safety property (`RANK[counted_severity] <= RANK[severity]` always) for why this
    fallback can never be a promotion in disguise.
    """
    return f.counted_severity or f.severity


def finding_key(f: Finding) -> tuple:
    """Lexicographic sort key, most-severe-first under `reverse=True`.

    Order: COUNTED severity (so display order matches what the verdict actually used),
    then evidence strength (a cited table cell beats a page reference beats nothing),
    then whether a reproduction could settle it, then the lens tiebreak, then the id so
    the sort is total and therefore reproducible.
    """
    ref = (f.evidence_ref or "").strip()
    evidence = 2 if _CELL_REF.match(ref) else (1 if ref else 0)
    return (
        _SEVERITY_RANK.get(counted(f), 0),
        evidence,
        1 if f.verifiable_by_experiment else 0,
        _LENS_RANK.get(f.lens, 0),
        f.finding_id,
    )


def rank(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=finding_key, reverse=True)


def material_failures(findings: list[Finding]) -> list[Finding]:
    """Model findings with paper-level rejection authority: intentionally none.

    A finding may propose a dependency and prioritize investigation. It may not establish
    its own scientific truth or materiality. Deterministic paper checks and admissible
    bound executions enter through `material_target_failure` instead.
    """
    return []


def material_concerns(findings: list[Finding]) -> list[Finding]:
    """Counted FATAL/MAJOR model concerns, prominent but never rejection authority."""
    return [f for f in findings if counted(f) in CONCERN_SEVERITY]


def is_calibration(probe: ProbeResult | None) -> bool:
    """Did the thing that ran measure this MACHINE rather than the paper?

    The identical-arms noise-floor template. Its two arms are the same program, so its
    "measured delta" is this host's seed noise and its accuracy is the accuracy of
    `load_digits`. Nothing about any paper follows from it in either direction.

    Read from the runner's own `calibration` flag, and falling back to the verdict for a
    result written before that flag existed. `local_exec.run_probe` now refuses a spec
    that would reach the template under an admissible provenance, so this is the second
    of two guards on the same hole: a calibration reconciliation reaching this layer means
    an artifact was hand-edited or an upstream branch is wrong, and neither is a reason to
    convict a paper.
    """
    if probe is None:
        return False
    if probe.calibration is not None:
        return bool(probe.calibration)
    return probe.verdict == "calibration"


def claim_status(findings: list[Finding],
                 reconciliation: Reconciliation | None = None,
                 *, outcomes: list | None = None, objects: list | None = None,
                 probe: ProbeResult | None = None) -> tuple[str, str]:
    """The epistemic state underneath the colour: what this audit ESTABLISHED.

    `objects` carries the materiality context and is REQUIRED for any established target
    defect to reach VERIFIED_FAILURE. Omitting it does not fall back to the old
    "any established failure convicts" behaviour: a paper-level stop may not depend on
    whether a caller passed an optional argument, so an unjoinable defect is Tier 1 only.
    Callers that are exercising Tier 1 alone (does this route establish a defect at all)
    legitimately pass no objects and legitimately get NOT_VERIFIED here.

    Three states, and the distinction the binary decision cannot carry on its own:

      VERIFIED_FAILURE  a MATERIAL failure was established — an established defect on a
                        target carrying a machine-defensible paper-owned dependency.
      VERIFIED_SUPPORT  something was positively checked and held: a reproduction that
                        reconciled against the printed cell.
      NOT_VERIFIED      nothing was established in either direction. The honest state
                        for most papers, and the one a research auditor is allowed to
                        report rather than resolving it into a fabricated conclusion.

    INCONCLUSIVE is deliberately NOT a failure. A missing dataset, a units mismatch, an
    unparsed metric, a shut execution gate and an 8 GiB card facing a 24 GiB demand all
    land in NOT_VERIFIED, and none of them is evidence against a paper.
    """
    # A calibration run settles nothing, in EITHER direction, whatever provenance its spec
    # asserted. Checked before every other branch so no reconciliation derived from the
    # identical-arms template can reach a claim status at all.
    if is_calibration(probe):
        return "NOT_VERIFIED", (
            "the only thing that ran was the identical-arms noise-floor calibration, which "
            "measures this machine and reconciles nothing about the paper")
    # A paper has a SET of targets, and the primary one is not privileged: a MATERIAL
    # failure established on any of them is established, whatever order they were
    # attempted in. What changed is the second half — establishing a defect is Tier 1 and
    # no longer sufficient on its own. Both branches below come out of ONE call, so the
    # binary verdict cannot disagree with this function about what happened.
    source, kind = material_target_failure(objects, outcomes, reconciliation)
    if kind == "reconciliation":
        return "VERIFIED_FAILURE", "a reproduction attempt failed against the printed cell"
    if kind == "target":
        if getattr(source, "disposition", "") == "PAPER_ARITHMETIC_CONTRADICTION":
            # Not "failed reproduction" — nothing was executed and nothing is being
            # attributed to the authors' code. Say what actually happened: the paper's
            # own printed composition was recomputed and did not evaluate.
            return "VERIFIED_FAILURE", (
                f"target {getattr(source, 'target_id', '?')}: the paper's own "
                f"printed composition does not evaluate to the total it states "
                f"({getattr(source, 'reason', '') or 'see the ledger'})")
        return "VERIFIED_FAILURE", (
            f"target {getattr(source, 'target_id', '?')} failed reproduction on "
            f"{getattr(source, 'provenance', '?')} provenance")
    if (reconciliation is not None and reconciliation.status == "RESOLVED_VERIFIED"
            # THE CEILING, IN THE ACQUITTING DIRECTION. Invariant 3 says "in either
            # direction" and this branch used to check nothing, so a hand-edited or
            # upstream-buggy probe_results.json carrying RESOLVED_VERIFIED on synthesized
            # provenance would have yielded VERIFIED_SUPPORT — the one state that unlocks
            # the words "verified", "supported" and "confirmed" in the decision block.
            # Safe until now only because `local_exec.reconcile` refuses to EMIT that
            # status on an inadmissible provenance, which makes the acquitting half
            # single-enforced while the convicting half is enforced twice.
            and provenance_mod.admits(reconciliation.provenance)):
        return "VERIFIED_SUPPORT", "an executed metric reconciled with the printed cell"
    return "NOT_VERIFIED", "no material failure established, and nothing positively reproduced"


def reproduction_status(probe: ProbeResult | None) -> str:
    """REPRODUCED | FAILED_REPRODUCTION | NOT_VERIFIED | NOT_ATTEMPTED — reported beside
    the verdict, never folded into it.

    NOT_ATTEMPTED and NOT_VERIFIED are kept apart on purpose: "no experiment was run"
    and "an experiment ran and settled nothing" are different facts about the audit, and
    collapsing them is how a report starts implying it tried harder than it did.
    """
    if probe is None:
        return "NOT_ATTEMPTED"
    rec = probe.reconciliation
    if rec is None or rec.status == "NOT_ATTEMPTED":
        return "NOT_ATTEMPTED"
    if rec.status == "RESOLVED_VERIFIED":
        return "REPRODUCED"
    if rec.status == "FAILED_REPRODUCTION":
        return "FAILED_REPRODUCTION"
    return "NOT_VERIFIED"


def provenance_label(provenance: str) -> str:
    """Internal provenance token → the reader-facing vocabulary. Fails closed to
    SYNTHESIZED_DIAGNOSTIC, so an unrecognised token can never be reported as the
    authors' own code. Delegates to `harness.provenance`, which owns both the ceiling and
    its labels: a label table that could disagree with the ceiling would let a provenance
    the ceiling refuses be printed as the authors' repository."""
    return provenance_mod.label(provenance)


def overall_verdict(findings: list[Finding],
                    reconciliation: Reconciliation | None = None,
                    *, outcomes: list | None = None, objects: list | None = None,
                    probe: ProbeResult | None = None) -> tuple[str, str]:
    """The binary paper-level decision, plus the rule that produced it.

    RED iff `claim_status` is VERIFIED_FAILURE. Nothing else — no count, no accumulation,
    no second mechanism. See MATERIAL_SEVERITY for why counting was removed, and
    `claim_status` for the states this projects from.

    `objects` is the materiality context, and it is passed to the SAME
    `material_target_failure` call `claim_status` makes. The two functions used to reach
    the target-failure question through separate calls to `admissible_target_failure`;
    once materiality entered the decision that would have been two materiality rules, and
    two rules that can disagree are two verdicts.
    """
    # The calibration guard, ahead of everything. A run whose two arms are the same
    # program measures this host, so it can convict nobody; and `overall_verdict` read
    # nothing about calibration at all, which is how the harness's own noise floor became
    # able to drive RED through a hand-written spec asserting an admissible provenance.
    if is_calibration(probe):
        return "GREEN", (
            "the only thing that ran was the identical-arms noise-floor calibration, "
            "which measures this machine and reconciles nothing about the paper")
    source, kind = material_target_failure(objects, outcomes, reconciliation)
    if reconciliation is not None and reconciliation.status == "FAILED_REPRODUCTION":
        if reconciliation.provenance not in ADMISSIBLE_REPRODUCTION_PROVENANCE:
            # The ceiling, held at the verdict gate as well as at the reconciler. Reaching
            # here means an upstream bug or an edited artifact; either way a program that
            # is not entitled to reconcile a printed cell does not get to convict a paper,
            # so this reports the harness defect INSTEAD of the conviction rather than
            # alongside it.
            return "GREEN", (
                f"A reconciliation reported FAILED_REPRODUCTION with provenance "
                f"'{reconciliation.provenance or '(none)'}', which the provenance ceiling "
                f"does not admit as a reproduction of a printed cell. That status should "
                f"have been unreachable, so it is treated as a harness defect and NOT as "
                f"evidence about the paper. Nothing here counts against the authors.")
        if kind != "reconciliation":
            # Tier 1 held — the ceiling admits this — and Tier 2 did not: no central claim
            # of the paper is established to depend on the target this reconciled against.
            # An established defect, and not one that stops the paper. It is still printed
            # under `## Established failures` and still recorded in the ledger.
            return "GREEN", (
                f"A reproduction failed at "
                f"{reconciliation.target_id or reconciliation.table_ref or 'a target'} and "
                f"the failure is established, but this review did not establish that a "
                f"central scientific claim of the paper depends on it, so it does not "
                f"reject the paper. It is reported in full below.")
        where = f" at {reconciliation.table_ref}" if reconciliation.table_ref else ""
        # WHO ran decides what the failure means, so the sentence is derived from
        # provenance rather than asserted. Only `repo_exec` is the authors' own checkout;
        # this used to claim "the paper's own code does not reproduce the number it prints"
        # for a `driver` script the operator wrote — which the harness's own vocabulary
        # defines as NOT the authors' code, and which `authorize` reports as
        # `not_repo_execution`. Attributing our script's failure to them is the one
        # accusation this system must never make by accident.
        if reconciliation.provenance == "repo_exec":
            blame = ("The audited repository's own code does not reproduce the number it "
                     "prints, so the central claim does not stand on the evidence the "
                     "authors supplied.")
        elif reconciliation.provenance == "driver":
            # `driver` is a human-written faithful reproduction, which the provenance ceiling
            # deliberately admits in BOTH directions — so this verdict stands, and the
            # sentence has to say what stands rather than disclaim it. The first version of
            # this fix said "not established as a failure of the paper's code" while still
            # returning RED, so the report contradicted its own verdict.
            blame = ("The program that ran was a human-written reproduction of the paper's "
                     "method, not the authors' checkout. Whether it is faithful is not "
                     "machine-checked, so read the script before relying on this verdict.")
        else:
            blame = (f"The program that ran was '{reconciliation.provenance}', which the "
                     f"provenance ceiling does not admit as a reproduction of a printed cell, "
                     f"so this status should not have been reachable — treat it as a harness "
                     f"defect rather than as evidence about the paper.")
        return "RED", f"Failed code reproduction{where}: {reconciliation.reason} {blame}"

    # A MATERIAL failure established on a NON-primary target reaches the same conclusion by
    # the same rule — from the same `material_target_failure` call `claim_status` makes, so
    # the ceiling AND the materiality gate are each applied in one place for both paths.
    if kind == "target":
        where = getattr(source, "target_id", "?")
        prov = getattr(source, "provenance", "")
        if getattr(source, "disposition", "") == "PAPER_ARITHMETIC_CONTRADICTION":
            blame = ("The paper's own printed composition does not evaluate to the total it "
                     "states. Nothing was executed and no artifact was required.")
            return "RED", (f"Paper-internal arithmetic contradiction at target {where}: "
                           f"{getattr(source, 'reason', '')} {blame}")
        blame = ("The audited repository's own code does not reproduce the number it prints "
                 "for this target." if prov == "repo_exec" else
                 "The program that ran was a human-written reproduction of the paper's "
                 "method, not the authors' checkout; whether it is faithful is not "
                 "machine-checked.")
        return "RED", (f"Failed code reproduction at target {where}: "
                       f"{getattr(source, 'reason', '')} {blame}")

    concerns = material_concerns(findings)
    n_minor = sum(1 for f in findings if counted(f) == "MINOR")
    # Tier 1 without Tier 2: a defect this review really did establish, on a target no
    # central claim was established to depend on. Said out loud rather than left to the
    # reader to notice in `## Established failures`, because "no material failure" and
    # "nothing was established" are different sentences.
    established = [o for o in (outcomes or []) if getattr(o, "establishes_failure", False)]
    non_material = (f" {len(established)} established defect(s) are reported below and none "
                    f"of them is on a target this review established a central claim to "
                    f"depend on, so none rejects the paper." if established else "")
    if concerns:
        return "GREEN", (f"No material failure established. {len(concerns)} MAJOR concern(s) and "
                         f"{n_minor} MINOR were recorded and are printed in full — a concern "
                         f"weakens a claim, it does not reject one, and no number of them "
                         f"accumulates into a rejection.{non_material}")
    return "GREEN", (f"No material failure established; {n_minor} MINOR finding(s). This is the "
                     f"absence of an established failure within the audited scope, not a "
                     f"certificate of correctness.{non_material}")


def material_target_failure(objects: list | None, outcomes: list | None,
                            reconciliation: Reconciliation | None = None
                            ) -> tuple[object | None, str]:
    """THE ONE materiality decision on the paper-level path. `(source, kind)`.

    `kind` is "target" | "reconciliation" | "", so a caller can word its own sentence
    without re-deciding anything. `claim_status` and `overall_verdict` both call THIS and
    nothing else, which is what makes it impossible for them to disagree about whether a
    paper failed — invariant 8, held by construction rather than by two matching
    implementations.

    TWO TIERS, in order, and neither can be skipped:

      Tier 1  `TargetOutcome.establishes_failure` — did this target's own evidence route
              establish a defect at all. Owns the provenance ceiling for the execution
              routes and the deterministic-recompute precondition for the arithmetic one.
      Tier 2  `harness.materiality` — is a CENTRAL scientific claim of the paper
              established to depend on that target.

    Route-blind: there is no branch here on provenance, disposition or route, so an
    arithmetic contradiction and a failed `repo_exec` reproduction are gated identically.

    The reconciliation argument is the legacy single-target path, and it is joined the same
    way — through `Reconciliation.target_id` against the same objects. A reconciliation
    carrying no joinable target establishes no materiality, which is the fail-closed
    answer DECISION 1 requires: a paper-level stop may never depend on whether a caller
    happened to pass the materiality context.
    """
    hit = materiality.material_target_failure(objects, outcomes)
    if hit is not None:
        return hit, "target"
    if (reconciliation is not None and reconciliation.status == "FAILED_REPRODUCTION"
            and reconciliation.provenance in ADMISSIBLE_REPRODUCTION_PROVENANCE
            and materiality.is_material(
                materiality.basis_for_target(
                    getattr(reconciliation, "target_id", "") or "", objects) or "NONE")):
        return reconciliation, "reconciliation"
    return None, ""


def admissible_target_failure(outcomes: list | None) -> object | None:
    """The first target outcome entitled to establish a material failure, or None.

    `TargetOutcome.establishes_failure` is a property on the artifact rather than a test
    written here, so both ceilings are applied at the type: a target that was blocked,
    that ran an unauthorised program, or that reconciled from a synthesized probe can
    never be counted by this function however it is called — and neither can a paper
    whose arithmetic merely differs by a model's say-so, since a
    `PAPER_ARITHMETIC_CONTRADICTION` disposition only ever exists when
    `claims.parse_quantity` deterministically recomputed it.
    """
    for o in (outcomes or []):
        if getattr(o, "establishes_failure", False):
            return o
    return None


# A central target that was ATTEMPTED and settled nothing. This is the only non-finding
# state that colours a paper, and the restriction is the whole point.
#
# The first version of this flagged every central target that had not reached a settled
# disposition. Run over the real corpus that made all seven papers YELLOW, for the same
# reason on every one: the execution gates are shut by default, so no central claim was
# ever settled, so the colour was a property of this harness's configuration rather than
# of any paper. That is precisely the defect the old `RED_MAJOR_TOTAL=10` threshold had —
# a decision that moves when the reviewer's own settings move — and it is not fixed by
# choosing a different set of dispositions to count.
#
# What survives is the one case where evidence really was gathered and really did not
# answer the question. "We were not permitted to check this", "the artifact does not
# contain it" and "this host cannot host it" are facts about the review, and they belong
# in the report's scope section, which states them plainly. They do not belong in a colour.
_ATTEMPTED_AND_UNSETTLED = ("INCONCLUSIVE",)


def unresolved_central(objects: list | None, outcomes: list | None) -> list:
    """Central, addressable targets that were pursued ADMISSIBLY and settled nothing.

    Not "not settled" — pursued by something entitled to settle it, and not settled. Both
    halves of that were learned the hard way, on the same corpus, twice:

      1. Flagging every unsettled central target made all seven papers YELLOW because the
         execution gates are shut by default.
      2. Restricting it to INCONCLUSIVE made all seven YELLOW again the moment the gates
         were OPENED — because what ran for most targets was a synthesized diagnostic,
         which the provenance ceiling never entitled to settle anything. Its INCONCLUSIVE
         does not mean "we tried and learned nothing"; it means "the thing that ran was
         never going to answer this."

    Both versions measured the harness's own configuration rather than the paper, which
    is the defect the removed count-of-findings threshold had. The admissibility
    requirement is what makes this a fact about the evidence: only the authors' own code,
    or a human-written reproduction the ceiling admits, can produce an inconclusive result
    that says something about the paper's checkability rather than about our probe.
    """
    by_id = {getattr(o, "target_id", ""): o for o in (outcomes or [])}
    out = []
    for obj in (objects or []):
        if getattr(obj, "centrality", "") != "CENTRAL" or not getattr(obj, "harness_addressable", False):
            continue
        outcome = by_id.get(getattr(obj, "target_id", ""))
        if (outcome is not None
                and getattr(outcome, "disposition", "") in _ATTEMPTED_AND_UNSETTLED
                and getattr(outcome, "provenance", "") in ADMISSIBLE_REPRODUCTION_PROVENANCE):
            out.append(obj)
    return out


def unchecked_central(objects: list | None, outcomes: list | None) -> list:
    """Central, addressable targets nothing was ever run for. Reported, never counted.

    The other half of the split above, kept as its own function so the reviewer report can
    say how much of the paper went unchecked without that fact leaking into the triage.
    """
    settled = {"REPRODUCED", "PAPER_ONLY_RESOLVED", "FAILED_REPRODUCTION", "INCONCLUSIVE",
              "PAPER_ARITHMETIC_CONTRADICTION"}
    by_id = {getattr(o, "target_id", ""): o for o in (outcomes or [])}
    out = []
    for obj in (objects or []):
        if getattr(obj, "centrality", "") != "CENTRAL" or not getattr(obj, "harness_addressable", False):
            continue
        outcome = by_id.get(getattr(obj, "target_id", ""))
        if outcome is None or getattr(outcome, "disposition", "") not in settled:
            out.append(obj)
    return out


def triage(findings: list[Finding], reconciliation: Reconciliation | None = None,
           *, outcomes: list | None = None, objects: list | None = None,
           probe: ProbeResult | None = None) -> tuple[str, str]:
    """RED | YELLOW | GREEN — the REVIEW-level decision, mechanically folded.

    RED is exactly `claim_status == VERIFIED_FAILURE`, reachable only from a
    machine-established material target failure and never by accumulating model concerns.
    What this adds is a split INSIDE the old GREEN,
    because "no material failure was established" was being asked to carry two different
    messages to a human reviewer:

        YELLOW  something needs your attention: a verified MAJOR concern, or a central
                claim this system could address and could not settle.
        GREEN   nothing needed attention within the audited scope — and the scope is
                stated, because GREEN is not a certificate of correctness.

    Neither YELLOW nor GREEN is an accusation and the reasons say so. YELLOW in particular
    is routinely the honest answer for a paper whose artifact would not run: invariants 4
    to 7 forbid reading that as a failure, and §17's rule forbids reading it as GREEN
    correctness. It is the state between them.

    A model cannot write any of the three. Every input here is harness-derived:
    `counted_severity` from `harness.grading.derive`, `disposition` from the execution
    path, `centrality` from `harness.discovery`, `harness_addressable` from whether a
    reference resolved.
    """
    verdict, reason = overall_verdict(findings, reconciliation, outcomes=outcomes,
                                      objects=objects, probe=probe)
    if verdict == "RED":
        return "RED", reason

    concerns = material_concerns(findings)
    stalled = unresolved_central(objects, outcomes)
    if concerns or stalled:
        parts = []
        if concerns:
            lenses = ", ".join(sorted({f.lens for f in concerns}))
            parts.append(f"{len(concerns)} MAJOR concern(s) from {lenses} survived verification")
        if stalled:
            parts.append(f"{len(stalled)} central claim(s) an experiment was run for and which "
                         f"it did not settle")
        return "YELLOW", (
            "Needs a human reviewer's attention: " + "; ".join(parts) + ". No material "
            "failure was established — a concern weakens a claim and does not reject one, "
            "and an unsettled check is a limit of this review, not a defect in the paper.")

    n_minor = sum(1 for f in findings if counted(f) == "MINOR")
    # An ESTABLISHED defect that is not material must not disappear into "no established
    # problem". It did not reject the paper and it is not nothing, and this is the line a
    # reader of a batch summary sees.
    established = [o for o in (outcomes or []) if getattr(o, "establishes_failure", False)]
    if established:
        return "GREEN", (
            f"No material failure: {len(established)} established defect(s) are reported "
            f"below and none of them is on a target this review established a central "
            f"claim to depend on, so none rejects the paper. {n_minor} MINOR finding(s) "
            f"recorded. Read the established defects — this is not a clean bill of health.")
    return "GREEN", (
        f"No material failure and no unresolved central concern within the audited scope; "
        f"{n_minor} MINOR finding(s) recorded. This is the absence of an established "
        f"problem in what was checked, not a certificate of correctness.")


def severity_review(findings: list[Finding]) -> list[str]:
    """Currently-COUNTED FATAL/MAJOR findings whose evidence is not a checkable cell
    citation — i.e. this looks at `counted(f)`, not the lens's raw `severity`, so a
    finding graded down below MAJOR before this runs no longer appears here even if the
    lens itself asserted MAJOR. What remains is exactly what still carries weight in the
    verdict on prose alone.

    This FLAGS and does not demote further. `harness.grading.derive` is where earning
    happens now — a real subsystem, not a speculative one (see `harness/grading.py` and
    `harness/prompts/grade.py`) — but even a blinded second reviewer is still a model,
    and what IS machine-checked is only its citation and the derivation table, not its
    judgement. This list is what remains for a human to check first.
    """
    return [f"[{counted(f)}] {f.finding_id or f.title[:40]} ({f.lens}) — evidence is "
            f"{f.evidence_class.replace('_', ' ')}, not a cited table cell"
            for f in findings
            if counted(f) in ("FATAL", "MAJOR") and f.evidence_class != "cell_verified"]


def questions(findings: list[Finding]) -> list[Finding]:
    """The findings that are QUESTIONS rather than defects, from either reader's answer.

    Kept as its own predicate because two sections need the same set — the report's
    `## Open review questions` and the whole-paper prompt's own question block — and a
    strong paper's review is mostly this list. `harness.grading.CANDIDATE_CAP` already
    holds them to NOTE, so nothing here affects a threshold; what matters is that they
    are PRINTED as questions instead of quietly folded in among the defects.
    """
    return [f for f in findings
            if f.finding_class == "OPEN_QUESTION"
            or (f.finding_class == "UNGRADED" and f.candidate_class == "OPEN_QUESTION")]


def grading_review(findings: list[Finding]) -> list[str]:
    """Every finding where grading actually changed what counts — `counted(f) !=
    f.severity`. The direct answer to "where did grading move this verdict", one line
    per finding, naming the cap that bound it."""
    return [f"{f.finding_id or f.title[:40]} ({f.lens}): lens asserted {f.severity}, "
            f"counted as {counted(f)} — capped by `{f.binding_cap or 'unknown'}` "
            f"({f.finding_class})"
            for f in findings if f.counted_severity and f.counted_severity != f.severity]


def verdict_sensitivity(findings: list[Finding],
                        reconciliation: Reconciliation | None = None,
                        *, outcomes: list | None = None,
                        objects: list | None = None) -> str:
    """The same verdict, over only the findings whose evidence is a verified table cell.

    NOT a second grader and not a second opinion. It is `overall_verdict` — the identical
    materiality table — applied to a subset of the identical findings, chosen by
    `evidence_class`, which the harness wrote. Nothing here judges anything.

    What it answers is the question `severity_review` could only gesture at. That function
    lists the FATAL/MAJOR findings resting on prose; it cannot say whether they MATTER. On
    the pilot corpus they do: 15 of 29 MAJOR findings are prose-backed, and for two of the
    three papers the RED verdict softens to GREEN without them. "Severity is not
    machine-verified" and "this RED depends on grades that are not machine-verified" are
    very different statements to hand an editor, and only the second is actionable.

    A reproduction failure is passed through unchanged, because it is not a graded finding
    at all — it is arithmetic against a cell, and dropping it here would report a
    sensitivity to severity for a verdict that never depended on severity.
    """
    cell_backed = [f for f in findings if f.evidence_class == "cell_verified"]
    return overall_verdict(cell_backed, reconciliation, outcomes=outcomes, objects=objects)[0]


def verdict_if_lens_severity_only(findings: list[Finding],
                                  reconciliation: Reconciliation | None = None,
                                  *, outcomes: list | None = None,
                                  objects: list | None = None) -> str:
    """The verdict this paper would have received before independent grading existed —
    the mirror of `verdict_sensitivity`, chosen by `counted_severity` instead of
    `evidence_class`. Not a second opinion: `overall_verdict`, the identical threshold
    table, over the identical findings with `counted_severity` erased so every count
    falls back to the lens's own raw assertion. The only way a reader can see WHERE
    grading moved a verdict, same as `verdict_if_cell_backed_only` already shows where
    the evidence class does."""
    lens_only = [f.model_copy(update={"counted_severity": ""}) for f in findings]
    return overall_verdict(lens_only, reconciliation, outcomes=outcomes, objects=objects)[0]


def build_chain(findings: list[Finding], probe: ProbeResult | None) -> ExperimentalChain | None:
    """Link the reproduction attempt back to the finding it was about. Copies, never derives.

    Every field comes from an artifact some earlier stage wrote. The one thing computed
    here is `broken_link`: the FIRST precondition that was not established, so a reader
    of an INCONCLUSIVE sees which link failed instead of having to re-walk the chain.
    Naming the first one matters — later links are not assessed once an earlier one
    fails, so listing them all would imply checks that never ran.
    """
    if probe is None:
        return None
    rec, acq = probe.reconciliation, probe.repo
    anchor = next((f for f in findings if f.finding_id and f.finding_id == probe.finding_id), None)

    chain = ExperimentalChain(
        finding_id=probe.finding_id or "",
        claim=(anchor.as_claim() if anchor else probe.claim or ""),
        evidence_refs=[anchor.evidence_ref] if anchor and anchor.evidence_ref else (
            [rec.table_ref] if rec and rec.table_ref else []),
        repo_url=(acq.url if acq else ""),
        commit=(acq.commit if acq else ""),
        backend=probe.backend or "",
        provenance=probe.provenance or "",
        executed=bool(probe.seeds_run),
        executions=probe.executions,
        execution_log=probe.execution_log,
        reconciliation=(rec.status if rec else ""),
        failure_class=(rec.failure_class if rec else "") or "",
    )
    # The fresh execution-time check when there was one, else what planning established.
    # A probe that never executed has only the second, and reporting it as `unassessed`
    # would name the wrong broken link — the commit was checked, something later failed.
    chain.commit_state = (probe.commit_verification.state
                          if probe.commit_verification is not None
                          else (probe.commit_state or "unassessed"))
    if probe.backend_selection:
        chain.backend = f"{probe.backend} ({probe.backend_selection})"
    if probe.experiment is not None:
        chain.experiment_state = probe.experiment.state
    if probe.metric_identity is not None:
        chain.metric_state = probe.metric_identity.state
    if probe.configuration is not None:
        chain.configuration_state = probe.configuration.state
    if probe.capability is not None:
        chain.capability_code = probe.capability.reason_code
    if probe.resources is not None:
        chain.resource_state = probe.resources.state
    if probe.authorization is not None:
        chain.authorization = probe.authorization.decision

    for label, ok in (
            ("repository", bool(chain.repo_url)),
            ("audited commit", chain.commit_state == "verified"),
            ("experiment identity", chain.experiment_state == "established"),
            ("metric identity", chain.metric_state == "established"),
            ("configuration identity", chain.configuration_state == "established"),
            ("execution capability", chain.capability_code == "established"),
            ("resource sufficiency", chain.resource_state == "satisfied"),
            ("authorization", chain.authorization == "authorized"),
            ("execution", chain.executed)):
        if not ok:
            chain.broken_link = label
            break
    return chain


def _chain_block(c: ExperimentalChain) -> list[str]:
    """The experimental chain as a table a reader can walk link by link."""
    rows = [("finding", f"`{c.finding_id}`" if c.finding_id else "— none targeted"),
            ("paper claim", _cell(c.claim, 160) or "—"),
            ("evidence", ", ".join(f"`{r}`" for r in c.evidence_refs) or "—"),
            ("repository", f"`{c.repo_url}`" if c.repo_url else "— none advertised"),
            ("audited commit", f"`{c.commit[:12]}` ({c.commit_state})" if c.commit else "— none"),
            ("experiment identity", c.experiment_state),
            ("metric identity", c.metric_state),
            ("configuration identity", c.configuration_state),
            ("execution capability", c.capability_code),
            ("resource sufficiency", c.resource_state),
            ("backend", c.backend or "—"),
            ("authorization", c.authorization or "—"),
            ("code that ran", c.provenance or "—"),
            ("executed", f"yes — {c.executions} process(es)" if c.executed else "no"),
            ("execution log", f"`{c.execution_log}`" if c.execution_log else "—"),
            ("reconciliation", c.reconciliation or "—")]
    out = ["| link | state |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]
    if c.broken_link:
        out += ["", f"⛔ The chain breaks at **{c.broken_link}**. Nothing after that link was "
                    f"established, so no reproduction verdict follows — this is a limit of "
                    f"what could be proven here, not a finding about the paper."]
    elif c.reconciliation in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION"):
        out += ["", f"🟢 Every link established, and the reconciliation reached "
                    f"**{c.reconciliation}** — a real reproduction verdict."]
    else:
        # No broken link is not the same as a verdict. `reconcile` refuses for reasons that
        # break no chain link at all — a units mismatch, a zero noise band, an unparseable
        # cell — and this branch used to print "the reconciliation above is a real
        # reproduction verdict" over an INCONCLUSIVE, which the same file defines as
        # "no reproduction verdict can be drawn". Complete-chain does not imply verdict.
        out += ["", f"⚪ Every link was established, but the reconciliation is "
                    f"**{c.reconciliation or 'not recorded'}**, so no reproduction verdict "
                    f"follows. The chain shows the attempt was legitimate; the arithmetic "
                    f"did not settle the cell."]
    return out


def pick_unasked_question(reports: list[LensReport]) -> str:
    """First non-empty question in lens-priority order. Deterministic, verbatim."""
    for r in sorted(reports, key=lambda r: _LENS_RANK.get(r.lens, 0), reverse=True):
        if r.unasked_question.strip():
            return r.unasked_question.strip()
    return ""


def _probe_heading(p: ProbeResult) -> str:
    """Name the section after what actually ran, so the contents are not oversold."""
    if p.provenance == "synthesized":
        return f"Autonomous mechanism probe — `{p.mechanism}`"
    if p.provenance == "repo_exec":
        return "Measured reproduction (the paper's own code)"
    return "Measured reproduction (local)"


def _probe_block(p: ProbeResult) -> list[str]:
    """The measured numbers, stated plainly. Every figure comes from the probe JSON."""
    if p.verdict == "blocked":
        # Deliberately worded as a fact about this harness. "The probe did not produce
        # measurements" would read, next to a paper, as though the paper's code had been
        # tried and found wanting; nothing was tried.
        auth = p.authorization
        why = auth.detail if auth else p.reason
        return [f"⛔ **The repository was not executed** — this harness declined to run it "
                f"(`{auth.decision if auth else 'unauthorized'}`). {why} "
                f"Nothing about the paper follows from this."]
    if p.verdict == "failed":
        return [f"The reproduction probe did not produce usable measurements. {p.reason}"]
    if p.verdict == "degenerate":
        return [f"⚠️ The probe ran but its noise estimate is unusable. {p.reason}"]

    # A probe with no paper-specific script did not reproduce anything — it calibrated
    # this machine's seed noise. Saying otherwise would dress a calibration run up as
    # evidence about the paper, which is the exact move this harness exists to catch
    # other people making. `calibration` is recorded on the result by the runner; the
    # fallback is only for probe_results.json written before that field existed.
    calibration = (p.calibration if p.calibration is not None
                   else (p.verdict == "calibration" or p.claimed_delta is None))
    synthesized = p.provenance == "synthesized"
    if calibration:
        head = (f"⚪ **Hardware Noise-Floor Calibration** (σ = {p.measured_std:.4f}, "
                f"2σ = {p.noise_band:.4f}) — No paper-specific script evaluated.")
    elif synthesized:
        placebo = p.mechanism == "placebo"
        if placebo:
            # Inverted on purpose relative to a genuine mechanism probe below: a placebo
            # arm has no hypothesis, so its clearing the noise band is the CONCERNING
            # result — an arbitrary perturbation moves the metric as much as the paper's
            # claimed mechanism did — and its staying inside the band is the benign,
            # expected one. Marking "detectable" 🟢 here read as the harness endorsing an
            # auxiliary term with no hypothesis as if it were a positive finding.
            head = {"within_noise": ("🟢 **Placebo control — an auxiliary term with no "
                                     "hypothesis did NOT clear the noise band.**"),
                    "detectable": ("⚠️ **Placebo control — an auxiliary term with no "
                                   "hypothesis ALSO clears the noise band.**")}.get(p.verdict, "")
        else:
            head = {"within_noise": (f"🔴 **Synthesized mechanism probe (`{p.mechanism}`) — the effect "
                                     f"sits INSIDE the noise band.**"),
                    "detectable": (f"🟢 **Synthesized mechanism probe (`{p.mechanism}`) — the effect "
                                   f"CLEARS the noise band.**")}.get(p.verdict, "")
    else:
        head = {"within_noise": "🔴 **The measured effect sits inside the noise band.**",
                "detectable": "🟢 **The measured effect clears the noise band.**"}.get(p.verdict, "")
    out = [head, "", f"Ran on `{p.device}` over {len(p.seeds_run)} seeds"
                     f"{f' ({len(p.seeds_failed)} failed)' if p.seeds_failed else ''}"
                     f" in {p.seconds:.0f}s.", "",
           "| quantity | value |", "|---|---|"]
    # A calibration run's arms are identical by construction, so its "measured delta" is
    # a property of the template, not a measurement of anything. Printing it next to a
    # claimed delta is what made the old block read as a reproduction.
    if not calibration:
        out.append(f"| measured delta | {p.measured_delta:+.4f} |")
    out += [f"| seed noise (1σ) | {p.measured_std:.4f} |",
            f"| detectability band (2σ) | {p.noise_band:.4f} |"]
    for arm, st in p.arms.items():
        out.append(f"| {arm} mean (n={st.n}) | {st.mean:.4f} ± {st.std:.4f} |")
    if p.claimed_delta is not None:
        out.append(f"| **claimed delta** | **{p.claimed_delta:+.4f}** |")
    out += ["", p.reason]

    # Secondary measurements. A headline metric that moved is only half the answer; the
    # other half is whether anything else moved with it.
    if p.aux:
        arms = list(p.arms)
        out += ["", "**Secondary measurements** (same runs, same seeds):", "",
                "| quantity | " + " | ".join(arms) + " | delta |", "|---" * (len(arms) + 2) + "|"]
        for key, per_arm in p.aux.items():
            cells = [f"{per_arm[a].mean:.4f} ± {per_arm[a].std:.4f}" if a in per_arm else "—"
                     for a in arms]
            if len(arms) == 2 and all(a in per_arm for a in arms):
                delta = f"{per_arm[arms[-1]].mean - per_arm[arms[0]].mean:+.4f}"
            else:
                delta = "—"
            out.append(f"| `{key}` | " + " | ".join(cells) + f" | {delta} |")

    if synthesized:
        if p.mechanism == "placebo":
            # The placebo is the ONLY synthesized template — probe_synth.plan has no
            # mechanism dispatch — and it is paper-independent BY CONSTRUCTION: the same
            # script runs for every paper. Calling it "written from the paper's own
            # published formulation" was false for every synthesized probe this harness
            # has ever produced against a real paper.
            out += ["", f"**What authored this probe.** {p.rationale}", "",
                    f"⚠️ **This is a generic control, not a reproduction of the paper's "
                    f"mechanism.** `runs/{p.paper_id}/probe.py` is a paper-independent "
                    f"placebo control this harness runs whenever no mechanism-specific "
                    f"template matches — it was NOT derived from this paper's formulation. "
                    f"It measures how much an auxiliary term with no hypothesis moves the "
                    f"metric under the same budget, and it is **not** evidence about any "
                    f"number printed in the paper's tables, nor about the paper's own "
                    f"claimed mechanism specifically." + (" The reconciliation below is "
                    f"capped at INCONCLUSIVE for that reason." if p.reconciliation else "")]
        else:
            out += ["", f"**What authored this probe.** {p.rationale}", "",
                    f"⚠️ **This is a reimplementation, not a reproduction.** `runs/{p.paper_id}/probe.py` "
                    f"was written by the harness from the paper's own published formulation and run at "
                    f"toy scale on synthetic data. It is evidence about whether the stated mechanism "
                    f"behaves as described, and it is **not** evidence about any number printed in the "
                    f"paper's tables — those were produced at a scale and on datasets this probe does "
                    f"not touch." + (" The reconciliation below is capped at INCONCLUSIVE for that "
                                     "reason." if p.reconciliation else "")]
    if calibration:
        out += ["", f"Read this as a detectability floor: on this hardware and metric, any claimed "
                    f"gain below {p.noise_band:.4f} could not be distinguished from run-to-run "
                    f"variation at {len(p.seeds_run)} seeds. It is a measurement of this machine, "
                    f"not of the paper. Supply a reproduction script at "
                    f"`runs/<paper_id>/spec.json` to test an actual claim."]
    if p.claim_within_noise:
        out += ["", f"**The paper's claimed delta of {p.claimed_delta:+.4f} is smaller than this "
                    f"machine's 2σ seed-noise band of {p.noise_band:.4f}.** A gain that size cannot "
                    f"be distinguished from run-to-run variation at this seed count."]
    return out


_REPO_HEAD = {
    "cloned": "🟢 **Cloned and inspected.**",
    "cached": "🟢 **Inspected from an existing checkout.**",
    "synthesized": "⚪ **No repository advertised** — a standalone scaffold was written instead.",
    "unavailable": "⚪ **The paper advertises no code repository.**",
    "blocked": "⚪ **Not fetched — the network gate is closed.**",
    "failed": "⚠️ **Acquisition failed.**",
    "not_attempted": "⚪ **Code acquisition did not run.**",
}
_RECONCILE_HEAD = {
    "RESOLVED_VERIFIED": "🟢 **RESOLVED / VERIFIED** — the executed metric matches the printed cell.",
    "FAILED_REPRODUCTION": "🔴 **FATAL — FAILED CODE REPRODUCTION.**",
    "INCONCLUSIVE": "⚪ **Inconclusive** — no reproduction verdict can be drawn.",
    "NOT_ATTEMPTED": "⚪ **Not attempted.**",
}
MAX_CODE_ROWS = 12


def _repo_block(a: RepoAcquisition) -> list[str]:
    """Where the code came from. States plainly when the answer is 'we did not look'."""
    out = [_REPO_HEAD.get(a.status, f"**{a.status}**"), ""]
    rows = [("source", f"`{a.url}`" if a.url else "— none found in the paper"),
            ("status", a.status)]
    if a.commit:
        rows.append(("commit", f"`{a.commit}`"))
    if a.path:
        rows.append(("path", f"`{a.path}`"))
    if a.frameworks:
        rows.append(("frameworks", ", ".join(a.frameworks)))
    if a.dependency_files:
        rows.append(("dependency files", ", ".join(f"`{f}`" for f in a.dependency_files)))
    if a.dependencies:
        shown = ", ".join(a.dependencies[:12])
        extra = f" _(+{len(a.dependencies) - 12} more)_" if len(a.dependencies) > 12 else ""
        rows.append(("declared packages", shown + extra))
    if a.entrypoint:
        rows.append(("entrypoint", f"`{a.entrypoint}`"))
    rows.append(("environment", a.env_status))
    out += ["| item | value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    if a.reason:
        out += ["", a.reason]
    if a.reimplementation is not None:
        # A paper with no code is where a reader most needs to see a DECISION rather than
        # a stop, so the ingredient table is printed in both directions: it is the
        # evidence for "we could have rebuilt this and here is the brief", and equally the
        # evidence for "we could not, and these are the sentences the paper never wrote".
        r = a.reimplementation
        out += ["", "### Independent reimplementation (PATH B)", "",
                ("🟢 **Eligible** — the paper specifies enough to rebuild the experiment."
                 if r.established else
                 "⚪ **Not eligible** — the paper does not specify enough to rebuild the "
                 "experiment."), "", r.reason, "",
                "| ingredient | required | supplied | found at |", "|---|---|---|---|"]
        out += [f"| {i.kind} | {'yes' if i.required else 'no'} | "
                f"{'yes' if i.present else '**no**'} | {('`' + i.ref + '`') if i.ref else '—'} |"
                for i in r.ingredients]
        out += ["", "This is an ELIGIBILITY assessment about the paper's completeness. It "
                    "concludes nothing about whether the paper's claims are correct, and a "
                    "reimplementation — if one is written — is reported as "
                    "`INDEPENDENT_REIMPLEMENTATION`, never as the authors' own code."]
    return out


def _runtime_block(c: CodeAudit) -> list[str]:
    """What the checkout says it needs. Observations with citations, and nothing more.

    Deliberately separate from the findings block above it, and worded so it cannot be read
    as an accusation: these are requirements, not defects, and not one of them gates
    execution. Six detection rules were designed for this and each was attacked; all six
    came back saying the evidence supports an observation rather than a conclusion, so what
    is printed is a file, a line and a verbatim token for a human to judge.
    """
    if c.skipped or not c.declarations_scanned:
        return []
    if not c.runtime:
        return [f"⚪ **No runtime demand declared** in {len(c.declarations_scanned)} "
                f"declaration file(s). Absence of a declaration, not evidence that the "
                f"experiment needs nothing."]
    by_kind: dict[str, list] = {}
    for d in c.runtime:
        by_kind.setdefault(d.kind, []).append(d)
    out = [f"Read out of {len(c.declarations_scanned)} declaration file(s) and "
           f"{c.files_scanned} Python file(s). **None of this gates execution** — a demand "
           f"here can neither permit a reproduction nor refuse one.", ""]
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        out.append(f"- **{kind.replace('_', ' ')}** ({len(rows)})")
        for d in rows[:4]:
            where = f"`{d.file}:{d.line}`" if d.line else f"`{d.file}`"
            value = f" `{_cell(d.value, 70)}`" if d.value else ""
            out.append(f"  - {d.state} · {d.scope} · {where}{value}")
        if len(rows) > 4:
            out.append(f"  - _…and {len(rows) - 4} more._")
    return out


def _code_audit_block(c: CodeAudit) -> list[str]:
    """Static findings, each with a file, a line and the source line itself.

    **Filtered by rule AUTHORITY, not by severity.** `artifact_evidence.RULE_AUTHORITY`
    sorts each detector into what it can carry, and only classes A and B reach a reviewer;
    an unaudited rule defaults to invisible, because the audit licenses a rule rather than
    its existence. Measured over the four repository papers the detectors produced six hits
    and five are false — four from "eval" containing "val" on a line calling an evaluation
    FUNCTION named `test`, one from `new_transform_r` containing both an arm token and an
    augmentation token — so printing all of them would cost a referee five investigations
    to recover one fact. The suppressed hits stay in `probe_results.json`, where the machine
    trace is complete and nobody is being asked to act on them.
    """
    if c.skipped:
        return [f"⚪ **Not run.** {c.skipped}"]
    shown = [f for f in c.findings if artifact_evidence.reviewer_visible(f.rule_id)]
    hidden = len(c.findings) - len(shown)
    suppressed = ([f"", f"_{hidden} further hit(s) came from detectors this harness has "
                   f"audited as unsafe for reviewer output — they match substrings of "
                   f"identifiers and were measured false on the evaluated corpus. They "
                   f"remain in the machine trace._"] if hidden else [])
    if not shown:
        return [f"🟢 **No cheat patterns matched** across {c.files_scanned} Python file(s) "
                f"({c.lines_scanned:,} lines). This is the absence of a signature, not a "
                f"clean bill of health: the detectors cover baseline crippling, split "
                f"leakage and metric redefinition, and nothing else."] + suppressed
    counts = Counter(f.category for f in shown)
    out = [f"⚠️ **{len(shown)} pattern(s)** across {c.files_scanned} Python file(s) "
           f"({c.lines_scanned:,} lines): "
           + ", ".join(f"{n} {cat.replace('_', ' ')}" for cat, n in sorted(counts.items())) + ".",
           "", "Static hits are suspicions with line numbers, never verdicts — each is "
           "listed with the counter-explanation that would clear it.", ""]
    for f in shown[:MAX_CODE_ROWS]:
        out.append(f"- **[{f.severity}] {f.title}** — `{f.file}:{f.line}` · `{f.rule_id}`")
        out.append(f"  {f.statement}")
        if f.code_quote:
            out.append(f"  > `{_cell(f.code_quote, _QUOTE_CHARS)}`")
        if f.counter_explanations:
            out.append(f"  Alternative explanation: {_cell(f.counter_explanations[0], 200)}")
    if len(shown) > MAX_CODE_ROWS:
        out.append(f"- _…and {len(shown) - MAX_CODE_ROWS} further pattern(s); "
                   f"full set in `runs/<paper_id>/probe_results.json`._")
    out += suppressed
    if c.unparseable:
        out += ["", f"_{len(c.unparseable)} file(s) could not be parsed and were skipped._"]
    return out


def _reconciliation_block(r: Reconciliation) -> list[str]:
    """Executed number against the printed cell, with the arithmetic shown."""
    out = [_RECONCILE_HEAD.get(r.status, f"**{r.status}**"), "", "| quantity | value |", "|---|---|"]
    out.append(f"| cited cell | `{r.table_ref or '—'}` |")
    out.append(f"| cell contents (verbatim) | `{r.claimed_raw or '—'}` |")
    out.append(f"| claimed value | "
               f"{f'{r.claimed_value:g}' if r.claimed_value is not None else '— unparseable'} |")
    out.append(f"| reproduced value | "
               f"{f'{r.reproduced_value:g}' if r.reproduced_value is not None else '— none produced'} |")
    if r.reproduced_std is not None:
        out.append(f"| reproduced spread (1σ) | {r.reproduced_std:.4f} |")
    if r.delta_error is not None:
        out.append(f"| **Δ_error \\|reproduced − claimed\\|** | **{r.delta_error:.4f}** |")
    out.append(f"| hardware noise band (2σ) | {r.noise_band:.4f} |")
    if r.seeds_run:
        out.append(f"| seeds | {len(r.seeds_run)} ({', '.join(str(s) for s in r.seeds_run)}) |")
    out += ["", r.reason]
    return out


def _cell(s: str, n: int) -> str:
    """One markdown table cell: pipes escaped, newlines flattened, length capped."""
    s = " ".join((s or "").split()).replace("|", "\\|")
    return (s[: n - 1] + "…") if len(s) > n else s


def _scientific_block(r: EvalReport) -> list[str]:
    """The primary output, in the machine report's own dense table form.

    The reviewer report groups and caps these; here every one appears, because this file
    is the trace and a trace that omits the result is not one.
    """
    if not r.scientific_findings:
        return []
    L = ["## Scientific findings", "",
         "| Category | Sev | Lens | Finding | Address | Resolution | Evidence |",
         "|---|---|---|---|---|---|---|"]
    for sf in r.scientific_findings:
        L.append(f"| {sf.scientific_class} | {sf.severity or '—'} | {sf.lens} "
                 f"| {_cell(sf.title, 60)} | `{sf.claim_ref or '—'}` "
                 f"| {sf.resolution_status} | {sf.evidence_state} |")
    return L + [""]


def render_eval_report(r: EvalReport) -> str:
    """EvalReport → markdown. Pure: no LLM, no network, no Config.

    Target is 1-2 pages, so the findings table is capped and the threat bullets are
    capped — but a truncation is always STATED, never silent. A report that quietly
    drops findings reads as a clean bill of health for the ones it dropped.
    """
    ranked = rank(r.findings)
    # GREEN is deliberately not "PASS". The decision is "no material failure was
    # ESTABLISHED", which is a statement about this audit's reach, not a certificate
    # about the paper — and a badge reading PASS is exactly how that gets misread.
    badge = {"RED": "🔴 RED — a material failure was established",
             "GREEN": "🟢 GREEN — no material failure established"}.get(r.verdict, r.verdict)
    counts = {s: sum(1 for f in r.findings if counted(f) == s) for s in _SEVERITY_RANK}
    lens_counts = {s: sum(1 for f in r.findings if f.severity == s) for s in _SEVERITY_RANK}
    graded_delta = counts != lens_counts

    L = [
        f"# First-Round Review — {r.title or r.paper_id}",
        "",
        f"**Verdict: {badge}**",
    ]
    if r.verdict_contested:
        L += ["", f"🚩 **CONTESTED** — the independent substantive read below "
                 f"(`{r.substantive_verdict.verdict if r.substantive_verdict else ''}`) disagrees "
                 f"sharply with this deterministic verdict. Neither is overruled; this needs a "
                 f"human look before the verdict above is relied on as-is."]
    executed = bool(r.probe and ((r.probe.executions or 0) > 0 or r.probe.seeds_run))
    route_label = "What ran" if executed else "Prepared route"
    route_value = (r.execution_provenance or "SYNTHESIZED_DIAGNOSTIC") if r.probe else "NONE"
    L += [
        "",
        f"> {r.verdict_reason}",
        "",
        f"`{r.paper_id}` · {r.n_pages} pages · {r.n_sections} sections · {r.n_tables} tables · "
        f"{r.n_numbers} reported numbers",
        f"Lenses run: {', '.join(r.lenses_run) or '(none)'} · "
        f"Counted: {counts['FATAL']} FATAL / {counts['MAJOR']} MAJOR / {counts['MINOR']} MINOR"
        + (f" · lens-asserted: {lens_counts['FATAL']} FATAL / {lens_counts['MAJOR']} MAJOR / "
           f"{lens_counts['MINOR']} MINOR" if graded_delta else "")
        + (f" · {r.dropped_findings} dropped as unsubstantiated" if r.dropped_findings else ""),
        "",
        "## Decision",
        "",
        "| | |",
        "|---|---|",
        f"| **Paper decision** | {r.verdict} |",
        f"| **Claim status** | `{r.claim_status or 'NOT_VERIFIED'}` |",
        f"| **Supporting evidence** | "
        f"{_SUPPORT_ROW.get(r.claim_status, _SUPPORT_ROW['NOT_VERIFIED'])} |",
        f"| **Reproduction** | `{r.reproduction_status or 'NOT_ATTEMPTED'}` |",
        f"| **{route_label}** | `{route_value}` |",
        "",
        _DECISION_GLOSS.get(r.claim_status, _DECISION_GLOSS["NOT_VERIFIED"]),
        "",
        _REPRO_GLOSS.get(r.reproduction_status, _REPRO_GLOSS["NOT_ATTEMPTED"]),
        "",
        "## Critical validity threats",
        "",
    ]

    # COUNTED, not asserted — a MAJOR a grader could not confirm no longer belongs among
    # "critical" threats even though the lens still says MAJOR on its own file.
    threats = [f for f in ranked if counted(f) in ("FATAL", "MAJOR")]
    if not threats:
        L.append("None. No finding rises above MINOR.")
    else:
        for f in threats[:MAX_THREAT_BULLETS]:
            where = f" — `{f.evidence_ref}`" if f.evidence_ref else ""
            conf = f" ({f.confidence} confidence)" if f.confidence else ""
            sev_label = (f"{counted(f)} (lens asserted {f.severity})"
                        if f.counted_severity and f.counted_severity != f.severity else f.severity)
            L.append(f"- **[{sev_label}] {f.title}**{conf}{where}")
            # Evidence and inference are printed as separate, labelled lines. They are
            # different kinds of thing: the quote and the observation were checked by the
            # harness against the parsed paper, and the reasoning is a model's argument
            # from them. A reader who cannot tell which is which cannot audit either.
            L.append(f"  > \"{_cell(f.evidence_quote, _QUOTE_CHARS)}\"")
            if f.verified_observation:
                L.append(f"  ✅ *Verified* ({f.evidence_class}): "
                         f"{_cell(f.verified_observation, 260)}")
            L.append(f"  🧠 *Inference (model reasoning, not verified):* "
                     f"{_cell(f.as_reasoning(), 400)}")
            if f.alternative_interpretation:
                L.append(f"  🔍 *Falsification attempted (not verified):* "
                         f"{_cell(f.alternative_interpretation, 300)}"
                         + (f" — did not resolve it: {_cell(f.why_alternative_fails, 200)}"
                            if f.why_alternative_fails else ""))
            if f.steelman:
                L.append(f"  🛡 *Steelman (not verified):* {_cell(f.steelman, 300)}")
            if f.counter_explanations:
                L.append(f"  Alternative explanation: {_cell(f.counter_explanations[0], 200)}")
        if len(threats) > MAX_THREAT_BULLETS:
            L.append(f"- _…and {len(threats) - MAX_THREAT_BULLETS} further "
                     f"FATAL/MAJOR finding(s) — see the table below._")
    L.append("")

    if qs := questions(ranked):
        L += ["## Open review questions", "",
              "Questions a reviewer should ask, not evidence of a flaw — these count "
              "toward no threshold. A strong paper legitimately has several.", ""]
        for f in qs[:MAX_THREAT_BULLETS]:
            L.append(f"- {_cell(f.title or f.statement, 200)}")
        L.append("")

    refuted = [f for f in ranked if f.finding_class == "REFUTED"]
    if refuted:
        L += ["## Refuted candidates", "",
              "A blinded second reviewer specifically refuted these — withdrawn, not "
              "deleted, so the accusation and its refutation both stay visible.", ""]
        for f in refuted[:MAX_THREAT_BULLETS]:
            reason = f.grade.falsification if f.grade else ""
            L.append(f"- **{f.title}** (lens asserted {f.severity}) — "
                     f"{_cell(reason, 240) or 'no reason recorded'}")
        L.append("")

    dismissed = [f for f in ranked if f.candidate_class == "DISMISSED"]
    if dismissed:
        L += ["## Candidates raised and withdrawn", "",
              "The lens itself considered these and a reasonable reading resolved them. "
              "Printed so a reader can see the question was asked and answered rather "
              "than never asked.", ""]
        for f in dismissed[:MAX_THREAT_BULLETS]:
            L.append(f"- **{_cell(f.title, 110)}** — resolved by: "
                     f"{_cell(f.alternative_interpretation, 240) or 'no reading recorded'}")
        L.append("")

    if r.grade_coverage.get("candidates"):
        graded_findings = [f for f in ranked if f.grade_state == "graded"]
        L += ["## Independent grading", "",
              f"{r.grade_coverage.get('graded', 0)} of {r.grade_coverage.get('candidates', 0)} "
              f"serious candidate(s) independently graded by a second, blinded reviewer that saw "
              f"none of: the lens's severity, the lens's name, any other finding, or how findings "
              f"are counted (see `harness/prompts/grade.py`).", ""]
        if r.verdict_if_lens_severity_only and r.verdict_if_lens_severity_only != r.verdict:
            L += [f"**Grading moved this verdict.** On the lenses' own asserted severities alone, "
                  f"the same materiality table would have yielded "
                  f"**{r.verdict_if_lens_severity_only}** rather than **{r.verdict}**.", ""]
        moved = grading_review(r.findings)
        for line in moved[:MAX_THREAT_BULLETS]:
            L.append(f"- {line}")
        if moved:
            L.append("")
        for f in graded_findings[:MAX_THREAT_BULLETS]:
            g = f.grade
            if g is None:
                continue
            L.append(f"- `{f.finding_id}` grader verdict **{g.verdict}** / {g.severity} / "
                     f"{g.confidence} confidence → counted {counted(f)}")
            if g.falsification:
                L.append(f"  🔍 *Grader falsification (not verified):* {_cell(g.falsification, 260)}")
            if g.steelman:
                L.append(f"  🛡 *Grader steelman (not verified):* {_cell(g.steelman, 260)}")
            if g.impact_statement:
                L.append(f"  🧠 *Grader impact statement (not verified):* {_cell(g.impact_statement, 260)}")
            if g.independent_evidence_quote:
                label = "✅ *Verified*" if f.grader_evidence_class != "unverified" else "⚠️ *Unverified*"
                L.append(f"  {label} ({f.grader_evidence_class}): grader cited "
                         f"`{g.independent_evidence_ref}` — {_cell(f.grader_verified_observation or g.independent_evidence_quote, 220)}")
        L.append("")
    elif r.grade_coverage:
        L += ["## Independent grading", "",
              "No serious candidate met the independent-grading scope (0 candidates); "
              "no grader call was needed. Severity for the remaining findings is "
              "lens-asserted.", ""]

    if r.substantive_verdict:
        sv = r.substantive_verdict
        L += ["## Whole-paper assessment (model opinion, not counted)", "",
              "Reasoned independently rather than derived from the counts above — the one "
              "judgement a materiality table structurally cannot make. It moves no colour: "
              f"agreement with the deterministic verdict is `{r.verdict_agreement}`.", "",
              f"**{sv.verdict}**"
              + (f" · core contribution stands: **{sv.core_contribution_stands}**"
                 if sv.core_contribution_stands else "")
              + f" · weaknesses are {sv.weaknesses_are or 'unstated'}", "",
              f"> {_cell(sv.reason, 500)}", ""]
        for label, value in (
                ("Real contribution", sv.real_contribution or sv.strongest_contribution),
                ("Strongest support", sv.strongest_support),
                ("Strongest threat", sv.strongest_threat or sv.weakest_link)):
            if value:
                L.append(f"- **{label}:** {_cell(value, 300)}")
        if sv.claims_well_supported:
            L.append("- **Claims that stand as stated:**")
            L += [f"  - {_cell(c, 200)}" for c in sv.claims_well_supported[:5]]
        if sv.claims_needing_qualification:
            L.append("- **Claims needing qualification:**")
            L += [f"  - {_cell(c, 200)}" for c in sv.claims_needing_qualification[:5]]
        L.append("")

    if r.probe:
        L += [f"## {_probe_heading(r.probe)}", "", *_probe_block(r.probe), ""]
        if r.probe.repo is not None:
            L += ["## Code acquisition", "", *_repo_block(r.probe.repo), ""]
        if r.probe.code_audit is not None:
            L += ["## Static code audit", "", *_code_audit_block(r.probe.code_audit), ""]
            if runtime := _runtime_block(r.probe.code_audit):
                L += ["## Declared runtime demands", "", *runtime, ""]
        if r.probe.reconciliation is not None:
            L += ["## Table-cell reconciliation", "",
                  *_reconciliation_block(r.probe.reconciliation), ""]
        if r.experimental_chain is not None:
            L += ["## Experimental evidence chain", "",
                  *_chain_block(r.experimental_chain), ""]

    ungraded = severity_review(r.findings)
    if ungraded:
        L += ["## Severity caveat", "",
              f"{len(ungraded)} of {sum(1 for f in r.findings if f.severity in ('FATAL', 'MAJOR'))} "
              f"FATAL/MAJOR finding(s) rest on prose rather than a cited table cell. Severity is "
              f"assigned by the lens and is not machine-verified, and it is what the verdict "
              f"counts \u2014 check these first:", ""]
        L += [f"- {u}" for u in ungraded]
        L += [""]
        # How much the verdict actually leans on those grades. The same materiality table
        # over the cell-verified findings alone — no second judgement, no demotion.
        if r.verdict_if_cell_backed_only and r.verdict_if_cell_backed_only != r.verdict:
            L += [f"**This verdict depends on them.** Counting only findings whose evidence is a "
                  f"verified table cell, the same materiality table yields "
                  f"**{r.verdict_if_cell_backed_only}** rather than **{r.verdict}**. The "
                  f"difference is carried entirely by grades the harness cannot check.", ""]
        elif r.verdict_if_cell_backed_only:
            L += [f"The verdict does not depend on them: counting only cell-verified findings, the "
                  f"same materiality table still yields **{r.verdict_if_cell_backed_only}**.", ""]

    # Blockquoted, not interpolated bare: this is the one lens-supplied field the schema
    # invites to be a paragraph or two, so it cannot be collapsed to one line the way
    # `title` and `finding_id` are — but a bare embed lets a line starting with '#' forge
    # a heading, or a stray ``` open an unclosed code fence that swallows the rest of the
    # report. A blockquote renders every line as quoted prose regardless of what it starts
    # with, which neutralises both without touching legitimate multi-paragraph text.
    L += ["## The unasked obvious question", ""]
    if r.unasked_question.strip():
        L += ["> " + ln for ln in r.unasked_question.strip().splitlines()]
    else:
        L.append("_No lens identified a conspicuously missing comparison._")
    L.append("")

    if r.self_audit is not None:
        sa = r.self_audit
        L += ["## Reviewer self-audit", "",
              ("🟢 " if sa.complete else "⚠️ ") + f"**{sa.summary}**", "",
              "Machine-checked against harness-written fields, never against a reviewer's "
              "own assessment of its diligence. **A failed check does not change the "
              "verdict** — the materiality table is the verdict — it refuses to let this "
              "review call itself complete, and names what was not done.", ""]
        for i in sa.items:
            mark = {"pass": "✅", "fail": "❌", "not_applicable": "—"}.get(i.state, "?")
            scope = (f" ({i.n_offenders}/{i.n_in_scope})" if i.state == "fail"
                    else (f" ({i.n_in_scope})" if i.state == "pass" else ""))
            L.append(f"- {mark} {i.question}{scope}")
            if i.state == "fail":
                L.append(f"  {i.detail} — {', '.join(i.offenders)}"
                        + (f" _(+{i.n_offenders - len(i.offenders)} more)_"
                           if i.n_offenders > len(i.offenders) else ""))
        L.append("")

    L += _scientific_block(r)
    L += ["## Audit lens findings", "",
          "| Severity | Lens | Target | Finding | Evidence |",
          "|---|---|---|---|---|"]
    for f in ranked[:MAX_TABLE_ROWS]:
        sev_cell = (f"{counted(f)} (was {f.severity})"
                   if f.counted_severity and f.counted_severity != f.severity else f.severity)
        L.append(
            f"| {sev_cell} | {f.lens} | {_cell(f.target, 60) or '—'} | "
            f"{_cell(f.title, 110)} | {_cell(f.evidence_ref, 24) or '—'} |"
        )
    if not ranked:
        L.append("| — | — | — | No findings returned | — |")
    elif len(ranked) > MAX_TABLE_ROWS:
        L.append(f"| … | … | … | _{len(ranked) - MAX_TABLE_ROWS} further finding(s) "
                 f"omitted for length; full set in `audit/`_ | … |")

    verifiable = [f for f in ranked if f.verifiable_by_experiment]
    if verifiable:
        L += ["", "## Settleable by reproduction", "",
              "These findings are claims a targeted rerun could confirm or kill:", ""]
        L += [f"- `{f.finding_id}` {_cell(f.title, 110)}" for f in verifiable[:6]]

    return "\n".join(L).rstrip() + "\n"


# The reviewer report exists to REDUCE someone's workload, so it is bounded by
# construction rather than by hoping the paper is short. A first-pass reviewer who has to
# read sixty-seven findings has been handed the machine trace with a nicer title.
_MAX_RED, _MAX_YELLOW, _MAX_HELD, _MAX_OPEN = 5, 4, 3, 3
# Per scientific CATEGORY, not per report: a paper with issues in six
# categories should show a few of each rather than six of one. The report
# stays bounded because the vocabulary is closed and every category is capped.
_MAX_PER_CATEGORY = 3
# And a cap across all categories, because ten categories of three is thirty findings —
# a per-category cap alone bounds the shape of the report and not its length. Invariant
# 19 is a length guarantee, so it needs a length bound.
_MAX_FINDINGS_SHOWN = 8
# And the escalation record, which otherwise grows with `SH_MAX_TARGETS`. Every target
# that ran is in the ledger; this section exists to show a reader WHAT the system chose to
# spend an execution on, and three is enough to show that.
_MAX_TRIGGERED = 3
_LINE = 240

# The artifact axis in one clause each. Only the first is a fact about the paper.
_ARTIFACT_GLOSS = {
    "NO_ARTIFACT_ADVERTISED": "the paper advertises no code repository",
    "ARTIFACT_NOT_FETCHED": "a repository is advertised and a gate in this harness did "
                            "not permit fetching it",
    "ARTIFACT_UNOBTAINABLE": "a repository is advertised and could not be obtained from "
                             "here — a fact about the network or this host",
    "ARTIFACT_PRESENT_UNUSABLE": "the repository was obtained and this host could not be "
                                 "shown able to give it a fair run",
    "ARTIFACT_PRESENT_USABLE": "the repository was obtained and is runnable here",
    "ARTIFACT_UNASSESSED": "acquisition did not run for this review",
}


def _rate(value) -> str:
    """A rate, or the word that must appear where one cannot be computed. `None` is not
    zero and is not one: an empty surface has no coverage, it has no denominator."""
    return "not computable" if value is None else f"{value:.0%}"


_TRIAGE_GLOSS = {
    "RED": "a material problem was established on evidence this system re-verified",
    "YELLOW": "needs a human reviewer's attention; nothing was established either way",
    "GREEN": "no material problem established within the scope that was actually checked",
}


def _targets_summary(outcomes: list | None) -> dict:
    """How many targets ended in each disposition. Counts, not a judgement.

    A paper has a SET of targets and they end differently; "one blocked, one reproduced,
    one failed" is a real and common shape, and no single reproduction status can carry
    it. Collapsing it to whichever target happened to be primary is how a mixed result
    becomes a clean-looking one.
    """
    out: dict[str, int] = {}
    for o in (outcomes or []):
        d = getattr(o, "disposition", "") or "NOT_ATTEMPTED"
        out[d] = out.get(d, 0) + 1
    return out


def _short(s: str, n: int = _LINE) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


# The order a reviewer reads them in: what the paper says that conflicts with itself,
# then what it claims beyond its evidence, then what its design leaves open, then what the
# artifact and the specification could not support, then everything still open.
CATEGORY_ORDER = (
    "CONTRADICTION", "OVERSTATED_CLAIM", "CONFOUND", "PROTOCOL_ISSUE",
    "MISSING_CONTROL", "MISSING_VALIDATION", "IMPLEMENTATION_ISSUE",
    "SPECIFICATION_GAP", "UNRESOLVED_QUESTION", "NO_MATERIAL_ISSUE_FOUND",
)

CATEGORY_GLOSS = {
    "CONTRADICTION": "the paper disagrees with itself",
    "OVERSTATED_CLAIM": "the conclusion is stronger than the evidence offered",
    "CONFOUND": "an alternative explanation for the reported gain remains open",
    "PROTOCOL_ISSUE": "the evaluation design does not isolate what it is cited for",
    "MISSING_CONTROL": "a comparison the claim depends on is absent",
    "MISSING_VALIDATION": "a property is asserted on evidence of a narrower one",
    "IMPLEMENTATION_ISSUE": "the released artifact conflicts with the described method",
    "SPECIFICATION_GAP": "the paper does not say enough for this to be checkable",
    "UNRESOLVED_QUESTION": "a question this review raised and could not close",
    "NO_MATERIAL_ISSUE_FOUND": "nothing of the above, within what was checked",
}

_RESOLUTION_GLOSS = {
    "RESOLVED_FROM_PAPER": "settled against the paper itself",
    "RESOLVED_FROM_ARTIFACT": "settled by reading the released code",
    "RESOLVED_BY_EXECUTION": "settled by running the authors' code",
    "UNRESOLVED": "open",
    "NOT_INVESTIGATED": "not investigated",
}


def scientific_findings(report: EvalReport, target_set=None) -> list:
    """Join every kept finding to the question it raised and the target that pursued it.

    A PROJECTION and nothing more. Each field is copied from an artifact that already
    decided it — `counted_severity` from grading, `scientific_class` from `taxonomy`,
    `resolution_status` / `evidence_state` from the outcome via `TargetOutcome`'s derived
    properties, so the provenance ceiling arrives intact. This function judges nothing; it
    exists so the report and the ledger cannot disagree about what was found, which is
    what re-deriving the join at render time eventually produces.
    """
    from ..artifacts import ScientificFinding

    ts = target_set
    questions = list(getattr(ts, "questions", []) or [])
    objects = list(getattr(ts, "objects", []) or [])
    outcomes = {o.target_id: o for o in (getattr(ts, "outcomes", []) or [])}
    plans = {p.target_id: p for p in (getattr(ts, "plans", []) or [])}

    q_by_finding = {}
    for q in questions:
        for fid in (q.source_finding_ids or ([q.from_finding] if q.from_finding else [])):
            q_by_finding.setdefault(fid, q)
    obj_by_question: dict[str, object] = {}
    for obj in objects:
        if obj.question_id and obj.question_id not in obj_by_question:
            obj_by_question[obj.question_id] = obj
    lens_by_finding = {f.finding_id: f.lens for f in report.findings}

    out = []
    for f in report.findings:
        q = q_by_finding.get(f.finding_id)
        obj = obj_by_question.get(q.question_id) if q else None
        outcome = outcomes.get(getattr(obj, "target_id", "")) if obj else None
        plan = plans.get(getattr(obj, "target_id", "")) if obj else None
        others = [lens_by_finding.get(fid, "") for fid in (q.source_finding_ids if q else [])
                  if fid != f.finding_id]
        out.append(ScientificFinding(
            finding_id=f.finding_id,
            scientific_class=f.scientific_class or "UNRESOLVED_QUESTION",
            title=f.title, statement=f.statement, lens=f.lens,
            corroborating_lenses=sorted({x for x in others if x and x != f.lens}),
            severity=counted(f), confidence=f.confidence,
            claim_ref=f.evidence_ref, evidence_quote=f.evidence_quote,
            evidence_class=f.evidence_class,
            question_id=getattr(q, "question_id", ""),
            question=getattr(q, "question", ""),
            why_material=(getattr(plan, "why_material", "")
                          or getattr(q, "why_it_matters", "")),
            evidence_needed=getattr(q, "what_would_settle_it", ""),
            resolution_status=getattr(q, "resolution_status", "NOT_INVESTIGATED"),
            evidence_state=getattr(q, "evidence_state", "NOT_INVESTIGATED"),
            experiment_necessity=(
                outcome.necessity(bool(getattr(plan, "requires_execution", False)))
                if outcome is not None else
                getattr(plan, "necessity", "NO_EXPERIMENT_NEEDED")),
            evidence_refs=list(getattr(q, "evidence_refs", []) or []),
            conclusion=getattr(q, "conclusion", ""),
        ))
    return out


def coverage_numerators(target_set=None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(addressed, examined) as plain ADDRESS STRINGS, for `coverage.measure`.

    Strings and not objects, deliberately: `coverage.measure` must not be able to see the
    harness's own object list, because the defect it replaces was
    `targets_addressable / targets_discovered` — the same list divided by itself, which a
    worse extractor scores higher on.

    Two numerators and not one. "We minted an address for this unit" and "we pursued a
    route for it" are different claims, and corpus-wide 629 of 867 targets ended
    NOT_INVESTIGATED, so reporting the first as coverage would claim ~93% of papers where
    nearly three quarters of the targets were never looked at.
    """
    objects = list(getattr(target_set, "objects", []) or [])
    by_target = {o.target_id: o for o in (getattr(target_set, "outcomes", []) or [])}
    addressed, examined = [], []
    for obj in objects:
        ref = getattr(getattr(obj, "ref", None), "ref", "")
        if not ref:
            continue
        addressed.append(ref)
        outcome = by_target.get(getattr(obj, "target_id", ""))
        if outcome is not None and outcome.evidence_state != "NOT_INVESTIGATED":
            examined.append(ref)
    return tuple(addressed), tuple(examined)


def artifact_axis(probe: ProbeResult | None) -> tuple[str, str]:
    """(artifact_state, review_path) for this paper — see `taxonomy.artifact_state`.

    Answered from the acquisition and the capability the probe stage already recorded, so
    a review can state in one line whether the authors published code and whether it could
    be given a fair run. `execution_provenance` reads SYNTHESIZED_DIAGNOSTIC for a paper
    that published nothing, for one whose clone a gate refused, for one whose clone failed,
    and for one whose cloned repository was never authorized — four opposite facts under
    one token, and the reviewer report mentioned the artifact nowhere at all.
    """
    acq = getattr(probe, "repo", None) if probe is not None else None
    status = getattr(acq, "status", "") or ""
    cap = getattr(probe, "capability", None) if probe is not None else None
    established = bool(getattr(cap, "established", False))
    state = taxonomy.artifact_state(status, capability_established=established)
    return state, taxonomy.review_path(state)


def by_category(findings: list) -> dict:
    """`ScientificFinding`s grouped by scientific class, in `CATEGORY_ORDER`."""
    groups: dict[str, list] = {}
    for sf in findings:
        groups.setdefault(sf.scientific_class, []).append(sf)
    return {k: groups[k] for k in CATEGORY_ORDER if k in groups}


# THE LENGTH GUARANTEE, ENFORCED. Invariant 19 says the reviewer report is bounded BY
# CONSTRUCTION, and it was asserted as `len(text) < 9000` over SYNTHETIC reports whose
# worst case is ~3.2 KB while real reviews already ran 6.4-9.4 KB. So the bound was
# "short on our fixtures", and every section added since has been added against a test
# that could not feel it.
#
# `_bounded` makes it literal: sections are dropped from the LOWEST priority upward until
# the text fits, and the report says which ones it dropped. Four sections are never
# droppable, because a report that fits by discarding what it established is not a
# shorter report, it is a different one.
_MAX_REVIEW_CHARS = 12000

# Lowest priority first: this is the order things are given up in. The reader's question
# order is the inverse, which is why the report is not simply truncated from the end.
_DROPPABLE_ORDER = (
    "## What held up",
    "## Experiments triggered",
    "## Central claims an experiment ran for and did not settle",
    "## Open questions for the reviewer",
    "## Central claims this review did not check",
)
# Never dropped, whatever the length: what was established, what failed, how much was
# looked at, and what the review does not promise.
#
# The assertion below is the whole point of naming them. Before it, "never dropped" held
# only because `_DROPPABLE_ORDER` happened not to list these five, and an edit that added
# one would have violated invariant 19 with nothing catching it: `_bounded` reads
# `_DROPPABLE_ORDER` and has never read this tuple. It is checked at import so the failure
# lands on whoever made the edit rather than on a review that quietly lost its outcome
# block.
_UNDROPPABLE = ("## Review outcome", "## Established failures", "## Scientific findings",
                "## Scope of this review",
                "## What this review guarantees, and what it does not")
assert not (set(_UNDROPPABLE) & set(_DROPPABLE_ORDER)), (
    "a load-bearing section was made droppable: "
    f"{sorted(set(_UNDROPPABLE) & set(_DROPPABLE_ORDER))}")


def _split_sections(text: str) -> list[tuple[str, str]]:
    """[(heading, body)] with the preamble under heading ''. Splits on `## ` at line
    start only, so a `##` inside a quoted finding cannot open a section."""
    out: list[tuple[str, str]] = []
    head, buf = "", []
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            out.append((head, "".join(buf)))
            head, buf = line.rstrip("\n"), [line]
        else:
            buf.append(line)
    out.append((head, "".join(buf)))
    return out


def _bounded(text: str, limit: int = _MAX_REVIEW_CHARS) -> str:
    """Drop whole sections, lowest priority first, until the text fits — and say so.

    Whole sections rather than characters, because half of `## Open questions for the
    reviewer` reads as the complete list. A dropped section's content is in the ledger and
    the target set, which the surviving scope section already points at.
    """
    if len(text) <= limit:
        return text
    sections = _split_sections(text)
    dropped: list[str] = []
    for heading in _DROPPABLE_ORDER:
        if len(text) <= limit:
            break
        idx = next((i for i, (h, _) in enumerate(sections) if h.startswith(heading)), -1)
        if idx < 0:
            continue
        dropped.append(heading.removeprefix("## "))
        sections[idx] = (sections[idx][0], "")
        text = "".join(body for _, body in sections)
    if dropped:
        note = (f"\n> This review was over its {limit}-character bound, so "
                f"{len(dropped)} section(s) were dropped from it and not from the record: "
                f"{', '.join(dropped)}. Every one of them is in "
                f"`reports/<paper>.ledger.json` and `discovery/targets.json`.\n")
        # After the outcome block, where a reader meets it before trusting the rest.
        anchor = text.find("## Scientific findings")
        text = text[:anchor] + note + "\n" + text[anchor:] if anchor > 0 else text + note
    return text


def _reading_clause(cov) -> str:
    """How much of the paper the readers were carried, and how it was traversed.

    Says the fraction AND the mechanism, because the two answer different questions a
    reader of a review actually has. "Shown 38% of the extracted section text" was the
    honest sentence while one budget was divided across every section and each was
    hard-sliced; it is the wrong sentence now, and printing it would understate a review
    exactly as badly as the old scope line overstated one.

    The anchor cost is named rather than hidden. A paper read in parts re-carries a small
    packet — title, abstract, conclusion, outline — in every pass so that a cross-section
    comparison stays available to a part-local reader, and that repetition is a real cost
    this design pays. A coverage figure that quietly excluded it would be a coverage
    figure with a subsidy in it.
    """
    if cov is None or cov.prose_presented_fraction is None:
        return ""
    rec = getattr(cov, "reading", None)
    parts = getattr(rec, "number_of_parts", 0) or 0
    clause = (f", and were carried {cov.prose_presented_fraction:.0%} of the extracted "
              f"section text")
    if not rec or parts <= 1:
        return clause + " in a single pass"
    overhead = rec.anchor_repeat_fraction
    return (clause + f" in {parts} bounded parts, each repeating the paper's title, "
            f"abstract, conclusion and section outline"
            + (f" at a cost of {overhead:.1%} of the prose" if overhead is not None else "")
            + f"; {rec.lens_syntheses_completed} of {rec.lens_syntheses_required} "
              f"cross-part syntheses completed")


def render_reviewer_report(report: EvalReport, target_set=None) -> str:
    """The one-to-two page report a human reviewer reads. NOT the evidence ledger.

    Everything here is selected from artifacts that already exist and is capped by the
    constants above. Nothing is re-judged: `scientific_class`, `counted_severity`,
    dispositions, resolution states and provenance all arrive decided, and this function's
    only authority is over what fits.

    **The primary output is a set of scientific findings, each with a resolution state.**
    Not a colour. A colour answers one question — should a human look at this — and a
    referee needs three: what kind of problem is this, was it settled, and by what. The
    report therefore leads with the findings grouped by scientific category, follows with
    the questions still open, and states the triage level in `## Scope of this review`,
    where it belongs: it is a routing decision about this review, not a conclusion about
    the paper.
    """
    ts = target_set
    objects = list(getattr(ts, "objects", []) or [])
    outcomes = list(getattr(ts, "outcomes", []) or [])
    by_target = {o.target_id: o for o in outcomes}

    sfs = report.scientific_findings or scientific_findings(report, target_set)
    groups = by_category(sfs)

    unchecked = unchecked_central(objects, outcomes)

    L = [f"# First-pass review — {report.title or report.paper_id}",
         "",
         f"`{report.paper_id}` · {len(sfs)} evidence-verified finding(s) across "
         f"{len(groups)} scientific categor{'y' if len(groups) == 1 else 'ies'}",
         ""]

    # --- what a reviewer must be able to read in seconds -------------------------------
    # FIRST, and as four separate rows, because the three readings a reader used to be
    # invited into — the paper is correct / the paper failed / the paper is inconclusive —
    # all came from meeting a single token before meeting any of the four facts it was
    # standing in for. See `harness/outcome.py`: the rows are folded over disjoint inputs,
    # so the execution row cannot move the findings row.
    outcome_block = report.outcome or outcome_mod.derive(
        report, target_set, unchecked_central=len(unchecked))
    L += outcome_mod.render(outcome_block)

    failed = [o for o in outcomes if getattr(o, "establishes_failure", False)]
    if failed:
        L.append("## Established failures")
        for o in failed[:_MAX_RED]:
            obj = next((x for x in objects if x.target_id == o.target_id), None)
            # WHETHER IT STOPPED THE PAPER, per entry. The section lists what this review
            # ESTABLISHED (Tier 1); only a target a central claim is established to depend
            # on also rejects the paper (Tier 2). Printing the first without the second
            # leaves a reader to infer a rejection the decision did not make.
            _basis = getattr(obj, "materiality_basis", "NONE") or "NONE"
            _material_line = (
                f"  - materiality: `{_basis}` — "
                f"{materiality.BASIS_REASON.get(_basis, 'a paper-owned dependency was established')}; "
                "therefore this failure rejects the paper."
                if materiality.is_material(_basis) else
                "  - materiality: `NONE` — this review did not establish that a central "
                "scientific claim depends on this target, so the defect stands and the "
                "paper is NOT rejected on it. That is a limit of this harness's "
                "materiality model, not a judgement that the defect is unimportant.")
            if o.disposition == "PAPER_ARITHMETIC_CONTRADICTION":
                # NEVER "the authors' own artifact" — nothing ran, nothing is being said
                # about their code, and saying so would be exactly the mislabelling
                # invariant 16's own history warns about, aimed at a different evidence
                # type this time.
                L += ["", f"- **Paper-internal arithmetic contradiction — {o.target_id}**",
                      f"  - claim: {_short(getattr(obj, 'claim_text', ''))}",
                      f"  - evidence: {_short(o.reason)}",
                      f"  - why it matters: the paper's own printed composition does not "
                      f"evaluate to the total it states. No code was executed and no "
                      f"artifact was required to reach this conclusion.",
                      _material_line]
            else:
                L += ["", f"- **Failed reproduction — {o.target_id}**",
                      f"  - claim: {_short(getattr(obj, 'claim_text', ''))}",
                      f"  - evidence: {_short(o.reason)}",
                      f"  - why it matters: the authors' own artifact does not produce the "
                      f"quantity the paper prints for this target.",
                      _material_line]
        L.append("")

    # --- the findings themselves, by scientific category -------------------------------
    L.append("## Scientific findings")
    if groups:
        # The category table belongs WITH the findings it summarises, not above the
        # outcome block: a count of categories is not one of the four things a reader
        # needs first, and putting it there made the report open with arithmetic.
        L += ["", "| Category | Findings | Settled | Open |", "|---|---:|---:|---:|"]
        for cat, items in groups.items():
            settled = sum(1 for x in items if x.resolution_status.startswith("RESOLVED"))
            L.append(f"| {cat.replace('_', ' ').lower()} | {len(items)} | {settled} "
                     f"| {len(items) - settled} |")
    else:
        L += ["", "No finding survived evidence verification. That is a statement about "
                  "this review, not about the paper."]
    shown, elided = 0, 0
    for cat, items in groups.items():
        items = sorted(items, key=lambda x: _SEVERITY_RANK.get(x.severity, 0), reverse=True)
        room = min(_MAX_PER_CATEGORY, max(0, _MAX_FINDINGS_SHOWN - shown))
        # max(0, ...) per category, NOT summed raw: a category with one finding and three
        # slots contributes -2 to a raw sum, which silently cancels an over-full category
        # elsewhere and suppresses the "…and N more" line entirely. A report that drops
        # findings without saying so reads as a report that found none.
        elided += max(0, len(items) - room)
        if not room:
            continue
        L += ["", f"### {cat.replace('_', ' ').title()} — {CATEGORY_GLOSS.get(cat, '')}"]
        for sf in items[:room]:
            corro = (f" · also {', '.join(sf.corroborating_lenses)}"
                     if sf.corroborating_lenses else "")
            L += ["", f"- **{_short(sf.title, 100)}** [{sf.severity or 'ungraded'}] "
                      f"({sf.lens}{corro}) @ `{sf.claim_ref or 'n/a'}`",
                  f"  - {_short(sf.question or sf.statement, 200)}"]
            status = _RESOLUTION_GLOSS.get(sf.resolution_status, sf.resolution_status)
            # Shorter than it was, because `## Review outcome` now states the aggregate
            # once at the top: eight findings each repeating the same 190-character
            # "why this is open" sentence was 1.5 KB of a two-page report saying one thing
            # eight times. What is per-finding is `status`; the reason is per-review.
            if sf.conclusion:
                L.append(f"  - **{status}** — {_short(sf.conclusion, 110)}")
            elif sf.evidence_needed:
                L.append(f"  - **{status}**; would be settled by: "
                         f"{_short(sf.evidence_needed, 150)}")
            else:
                L.append(f"  - **{status}**")
            shown += 1
    if elided > 0:
        L.append("")
        L.append(f"…and {elided} further finding(s) across these categories; the complete "
                 f"set is in `reports/{report.paper_id}.ledger.json`.")
    L.append("")

    stalled = unresolved_central(objects, outcomes)
    if stalled:
        L += ["## Central claims an experiment ran for and did not settle"]
        for obj in stalled[:_MAX_YELLOW]:
            o = by_target.get(obj.target_id)
            L += ["", f"- **{obj.target_id}** — {_short(getattr(obj, 'claim_text', ''))}",
                  f"  - {_short(getattr(o, 'reason', '') or 'not attempted', 200)}"]
        L.append("")

    held = [o for o in outcomes if o.disposition in ("REPRODUCED", "PAPER_ONLY_RESOLVED")]
    L += ["", "## What held up"]
    if not held:
        L += ["", "Nothing was positively verified. GREEN here would mean 'not checked', "
                  "and this report does not say that."]
    for o in held[:_MAX_HELD]:
        obj = next((x for x in objects if x.target_id == o.target_id), None)
        L += ["", f"- **{o.target_id}** ({o.disposition.replace('_', ' ').lower()}) — "
                  f"{_short(getattr(obj, 'claim_text', ''), 120)}",
              f"  - {_short(o.reason, 200)}"]
    if len(held) > _MAX_HELD:
        L.append(f"- …and {len(held) - _MAX_HELD} more; see `discovery/targets.json`.")

    triggered = [o for o in outcomes if o.action in
                 ("AUTHOR_CODE_REPRODUCTION", "INDEPENDENT_RECONSTRUCTION",
                  "FOCUSED_VALIDATION_EXPERIMENT", "MECHANISM_TEST_ONLY")]
    L += ["", "## Experiments triggered"]
    if not triggered:
        eff = report.review_efficiency or {}
        L += ["", f"None. {eff.get('questions_generated', 0)} review question(s) were raised; "
                  f"{eff.get('targets_resolved_without_execution', 0)} were settled against the "
                  f"paper itself and {eff.get('targets_blocked_before_execution', 0)} could not "
                  f"be pursued. No experiment was run that the review did not consider justified."]
    for o in triggered[:_MAX_TRIGGERED]:
        obj = next((x for x in objects if x.target_id == o.target_id), None)
        # No `result:` line: the exact reason is in the `## Review outcome` execution row,
        # in full and cut only at a sentence boundary. It used to be repeated here at 200
        # characters, which took the disclaimer off the end of a sentence that opened with
        # a large delta — the single most misreadable string the report emitted.
        # PLANNED and RAN are two facts and this line used to print only the first, in a
        # section headed "Experiments triggered". Over the shipped corpus that rendered
        # "author code reproduction" nine times and "independent reconstruction" seven
        # times for sixteen runs of a harness-authored diagnostic — the plan's intent in
        # the grammatical position of the run's identity, and "independent reconstruction"
        # is additionally the exact phrase `provenance.PROVENANCE_LABEL` reserves for a
        # sealed reimplementation. The disposition was already honest; the subject of the
        # sentence was not.
        from ..provenance import label as _prov_label
        _ran = (f"ran: `{_prov_label(o.provenance)}`" if int(getattr(o, "launched", 0) or 0)
                else "nothing ran")
        L += ["", f"- **{o.target_id}** — planned: {o.action.replace('_', ' ').lower()} · "
                  f"{_ran}, ended `{o.disposition}`",
              f"  - question: {_short(getattr(obj, 'claim_text', ''), 140)}"]
    if len(triggered) > _MAX_TRIGGERED:
        L.append(f"- …and {len(triggered) - _MAX_TRIGGERED} further target(s) pursued; see "
                 f"`discovery/targets.json`.")

    # `## Reproduction status` used to sit here: a bold `**NOT_VERIFIED**` followed by
    # `Targets: 5 addressing blocked, 1 inconclusive, 25 not attempted`. Three separate
    # defects in four lines — `NOT_VERIFIED` is a value of two different vocabularies and
    # read as a verdict; `inconclusive` read as a property of the paper; and the gloss for
    # NOT_VERIFIED said "see the chain below" in an artifact that has no chain, which was
    # true of six of the seven corpus reviews. The state itself is now the `## Review
    # outcome` execution row, worded so it cannot be read as a judgement, and the
    # per-disposition counts are in `## Scope of this review`, where a denominator belongs.

    # `questions.derive` already merges by (question, address), so two lenses reaching the
    # same question about the same number arrive here as ONE question carrying both — the
    # agreement is recorded, not printed twice. What remains to deduplicate is the same
    # question asked about two different addresses, which really is two questions and is
    # collapsed only for the reader's sake, with the count kept.
    seen_q: dict[str, int] = {}
    open_qs = []
    for q in getattr(ts, "questions", []) or []:
        if not q.is_open:
            continue
        if q.question in seen_q:
            seen_q[q.question] += 1
            continue
        seen_q[q.question] = 1
        open_qs.append(q)
    if open_qs:
        L += ["", "## Open questions for the reviewer", "",
              "Each is a question this review raised, could not close, and states the "
              "evidence that would close it."]
        for q in open_qs[:_MAX_OPEN]:
            n = seen_q.get(q.question, 1)
            at = f" @ `{q.claim_ref.ref}`" if q.claim_ref and q.claim_ref.ref else ""
            L += ["", f"- **{_short(q.question, 180)}**{at}"
                      + (f" (raised at {n} addresses)" if n > 1 else "")]
            if q.what_would_settle_it:
                L.append(f"  - what would settle it: {_short(q.what_would_settle_it, 170)}")
            elif q.why_it_matters:
                L.append(f"  - why it matters: {_short(q.why_it_matters, 170)}")
            if q.conclusion:
                L.append(f"  - where it stands: {_short(q.conclusion, 170)}")
        if len(open_qs) > _MAX_OPEN:
            L.append(f"- …and {len(open_qs) - _MAX_OPEN} more; see `discovery/targets.json`.")

    if unchecked:
        # Stated as a limit of the review, prominently, precisely BECAUSE it does not move
        # the colour. A fact that changes no decision is the easiest one for a report to
        # let disappear, and this is the one a reader most needs in order to know how much
        # the colour is worth.
        L += ["", "## Central claims this review did not check", "",
              f"{len(unchecked)} central claim(s) were structurally checkable and nothing was "
              f"run for them. This is the boundary of the assessment above, not a criticism "
              f"of the paper:"]
        for obj in unchecked[:_MAX_OPEN]:
            o = by_target.get(obj.target_id)
            L.append(f"- **{obj.target_id}** — {_short(getattr(obj, 'claim_text', ''), 140)}"
                     f" ({_short(getattr(o, 'reason', '') or 'no experiment was run', 160)})")
        if len(unchecked) > _MAX_OPEN:
            L.append(f"- …and {len(unchecked) - _MAX_OPEN} more; see `discovery/targets.json`.")

    eff = report.review_efficiency or {}
    triage_level = report.triage or report.verdict or "GREEN"
    # `CaseLedger.efficiency` carries the funnel as FLAT keys; only `evaluation.per_paper`
    # nests them under "funnel". Reading the nested form here printed six zeros beside a
    # ledger that had the numbers — a report contradicting its own trace, which is the one
    # failure the two-artifact split exists to prevent.
    fun = {
        "discovered": eff.get("targets_discovered", 0),
        "checkable": eff.get("targets_addressable", 0),
        "warranting_experiment": eff.get("targets_warranting_experiment", 0),
        "launched": eff.get("targets_launched", 0),
        "completed": eff.get("executions_completed", 0),
        "resolved": eff.get("targets_resolved", 0),
    }
    if isinstance(eff.get("funnel"), dict):
        fun.update({k: v for k, v in eff["funnel"].items() if k in fun})
    cov = report.coverage
    obs = list(report.document_observations or [])
    obs_paper = [o for o in obs if o.about == "PAPER"]
    obs_extraction = [o for o in obs if o.about == "EXTRACTION"]
    art_state, art_path = (report.artifact_state or "ARTIFACT_UNASSESSED",
                           report.review_path or "PAPER_ONLY")

    L += ["", "## Scope of this review", "",
          # WHICH PATH, first, because it decides what evidence was reachable at all.
          f"- review path: **{art_path}** — {_ARTIFACT_GLOSS.get(art_state, art_state)}",
          f"- {len(report.lenses_run)} independent lens(es) read the paper: "
          f"{', '.join(report.lenses_run) or 'none'}"
          + _reading_clause(cov),
          f"- {report.dropped_findings} finding(s) were dropped because their evidence could "
          f"not be re-verified against the paper",
          # The funnel, in the reader's words, with the two terms that used to be conflated
          # kept apart: what this system JUDGED worth running, and what it actually ran.
          f"- {fun.get('discovered', eff.get('targets_discovered', 0))} target(s) discovered, "
          f"{fun.get('checkable', eff.get('targets_addressable', 0))} structurally checkable, "
          f"{fun.get('warranting_experiment', 0)} judged to warrant an experiment, "
          f"{fun.get('launched', 0)} actually launched a process, "
          f"{fun.get('resolved', 0)} settled a question about the paper",
          # What ran, in the reader's vocabulary, WITHOUT the bare `reproduction_status`
          # token: `NOT_VERIFIED` belongs to two different vocabularies in this codebase
          # and reads as a verdict in both. The state is the outcome block's execution
          # row; this line says only what program was executed.
          f"- what was executed: {report.execution_provenance or 'nothing'}"
          + (f" ({fun.get('launched', 0)} process(es) launched, "
             f"{fun.get('completed', 0)} reached a reconciliation)"
             if fun.get('launched', 0) else ""),
          # The per-disposition breakdown, here rather than under a heading that looked
          # like a verdict. It is a denominator: how the discovered targets ended.
          "- how the targets ended: " + (", ".join(
              f"{v} {k.replace('_', ' ').lower()}"
              for k, v in sorted((report.targets_summary or {}).items()) if v)
              or "no target reached an outcome"),
          # DEMOTED, deliberately. The triage level routes a reviewer's attention and says
          # nothing a category and a resolution state do not say better; leading with it
          # was the report telling a reader a colour when it had three axes to tell them.
          f"- triage for routing: **{triage_level}** — {_TRIAGE_GLOSS.get(triage_level, '')}",
          f"  {_short(report.triage_reason or report.verdict_reason, 400)}",
          # STRUCTURAL coverage, with the paper's own surface as the denominator, and
          # named so it cannot be read as issue recall — which this system cannot measure,
          # because no paper here carries adjudicated ground truth.
          (f"- review-surface coverage: an address was minted for "
           f"{cov.addressed}/{cov.surface_size} addressable unit(s) of the paper "
           f"({_rate(cov.addressed_rate)}), and a route was pursued for "
           f"{cov.examined} ({_rate(cov.examined_rate)}). Structural coverage, not issue "
           f"recall: whether the review found the issues that matter is "
           f"{cov.semantic_coverage.replace('_', ' ')}."
           if cov is not None and cov.surface_size else
           "- review-surface coverage: not computed for this review"),
          # DOCUMENT INTEGRITY, in the scope section and never among the findings, and
          # split by whose property each observation is. A naive existence check over this
          # corpus produced twelve claims that a table or equation was missing, and all
          # twelve of those objects are in the papers and absent only from our extraction.
          (f"- document integrity: {len(obs_paper)} observation(s) about the paper and "
           f"{len(obs_extraction)} about this review's own extraction. These are "
           f"structural observations, not scientific findings, and none of them counts "
           f"toward any threshold." if obs else
           "- document integrity: no mechanically determinable observation"),
          f"- full evidence trace: `reports/{report.paper_id}.ledger.json`, "
          f"target set: `discovery/targets.json`, machine report: `reports/{report.paper_id}.md`",
          "",
          "An empty findings table is never a certificate of correctness — it means "
          "nothing material was established within the scope above — and an unresolved "
          "question is never an accusation. This is a first-pass screen, not a substitute "
          "for expert review.", ""]

    # WHAT THIS REVIEW PROMISES AND WHAT IT DOES NOT, last and explicitly. A process
    # guarantee that held is checkable from an artifact; a scientific guarantee is not
    # made at all. Listing both under one heading is what invites the second to be read
    # off the first.
    if report.guarantees is not None:
        L += guarantees_mod.render(report.guarantees)
    return _bounded("\n".join(L))


def run_report(cfg: Config, pid: str) -> dict:
    """Assemble the ranked report from the ingested doc + whichever lenses ran."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))

    # Verified load: a finding whose evidence is not really in the paper is dropped
    # here, whoever wrote it. The driver is a language model; the harness checks.
    reports, dropped, invalid = audit_stage.load_reports(cfg, pid, doc)
    if not reports:
        return {"error": f"no audit lenses have run for '{pid}' — run audit_paper first, "
                         f"then write audit/<lens>.json for each prompt"}
    if invalid:
        # C9 — a missing, unparseable, empty, `null`, or malformed lens file must never
        # render as "ran clean, zero findings". Refused the same way `not reports` is:
        # no report is written, and the caller is told exactly which lens(es) to redo.
        # This guard stands even when `run_report` is invoked directly (the CLI path, or
        # a script), independent of whether the controller's own `collect` gate ran first.
        return {"error": f"{len(invalid)} lens(es) produced no usable result for '{pid}' and "
                         f"cannot be counted as run: {', '.join(invalid)}. Write a valid "
                         f"audit/<lens>.json for each, then re-run synthesize_report."}
    state.set_phase(cfg, pid, "report")

    probe_path = root / "runs" / pid / "probe_results.json"
    probe = ProbeResult(**state.read_json(probe_path)) if probe_path.exists() else None

    # Local import: `stages.grade` imports `rank` from this module, so a module-level
    # import here would be a cycle. By the time `run_report` is actually CALLED both
    # modules are fully loaded, so the cycle only exists at parse time, not at call time.
    from . import grade as grade_stage
    grade_stage.attach(cfg, pid, doc, reports)     # in-place: sets counted_severity etc.
    coverage = grade_stage.coverage(cfg, pid)
    if "error" in coverage:
        coverage = {"paper_id": pid, "candidates": 0, "graded": 0, "pending": 0}

    # The target set is optional on purpose. A project reviewed before the discovery phase
    # existed still renders, with an empty set — the folds below all take `outcomes=None`
    # and reduce to exactly the pre-discovery behaviour, which is the same guarantee
    # `counted()` makes about grading being off.
    from . import discover as discover_stage
    target_set = discover_stage.load(cfg, pid)
    if target_set is not None:
        # Re-fold route accounting here as well as at discovery/probe.  This upgrades a
        # resumed pre-accounting target set before the ledger copies it and ensures the
        # report and discovery artifact expose the identical durable rows.
        from .. import exhaustion as exhaustion_mod
        exhaustion_mod.refresh(target_set, cfg)
        state.write_json(discover_stage.targets_path(cfg, pid), target_set.model_dump())
    outcomes = list(target_set.outcomes) if target_set else []
    objects = list(target_set.objects) if target_set else []

    findings = rank([f for r in reports for f in r.findings])
    rec = probe.reconciliation if probe else None
    # `objects` is the MATERIALITY CONTEXT, and the production path passes it explicitly to
    # every function that can reach a paper-level stop. It is not optional here: a target
    # defect may only convict when a central claim is established to depend on it, and
    # that join lives on the objects.
    verdict, reason = overall_verdict(findings, rec, outcomes=outcomes, objects=objects,
                                      probe=probe)
    # The reason is CARRIED now rather than discarded. "Nothing was established" and "a
    # reproduction failed" are the same field with opposite reasons, and the reader-facing
    # outcome block has to be able to say which.
    status, status_why = claim_status(findings, rec, outcomes=outcomes, objects=objects,
                                      probe=probe)
    triage_level, triage_why = triage(findings, rec, outcomes=outcomes, objects=objects,
                                      probe=probe)
    # `TRIAGE_LEVELS` was a declared vocabulary nothing checked against: `triage` returns
    # string literals. A fourth colour, or a typo, would have reached a reader and a
    # batch summary unremarked.
    assert triage_level in artifacts_mod.TRIAGE_LEVELS, triage_level
    repro = reproduction_status(probe)
    ran_as = provenance_label(probe.provenance) if probe else "SYNTHESIZED_DIAGNOSTIC"

    from .. import verdict_driver
    # A SEALED whole-paper read wins over calling the subprocess: it is already produced,
    # already attributed, and re-running would spend a call to overwrite it. This is also
    # the only channel that works at all when the CLI is unreachable — see
    # `verdict_driver.accept_verdict`.
    substantive = verdict_driver.load_accepted(cfg, pid)

    # COUNTED severity, and the questions block alongside it. A whole-paper read shown
    # only the defects is being asked to weigh one side of the evidence: what a reviewer
    # asked and could not settle, and what it raised and then withdrew, are part of
    # judging the paper, and a reader deciding "are the weaknesses local or systemic"
    # needs both columns.
    findings_summary = "\n".join(
        f"- [{counted(f)}] ({f.lens}) {f.title}: {f.statement}" for f in findings[:30])
    withdrawn = [f for f in findings
                 if f.finding_class in ("REFUTED", "OPEN_QUESTION")
                 or f.candidate_class in ("OPEN_QUESTION", "DISMISSED")]
    questions_summary = "\n".join(
        f"- [{f.finding_class}/{f.candidate_class or 'unsorted'}] {f.title}"
        for f in withdrawn[:20])
    grading_summary = (f"{coverage['graded']} of {coverage['candidates']} candidate(s) "
                       f"independently graded.")
    from ..prompts import verdict as verdict_prompts
    verdict_prompt = verdict_prompts.build(
        doc.title, findings_summary, grading_summary, probe.reason if probe else "",
        questions_summary)
    # ALWAYS PERSISTED, gate open or shut. The audit and grading phases both write their
    # prompts to disk and stop, which is what makes a reader able to pick one up and do
    # the work by hand; this phase built its prompt inline and passed it straight to a
    # subprocess, so when that subprocess was unreachable there was nothing to hand
    # anyone — the only phase of the review with no manual entry point at all.
    prompt_path = root / "reports" / "verdict_prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(verdict_prompt, encoding="utf-8")

    ok, _why = verdict_driver.available(cfg)
    if substantive is None and ok:
        substantive = verdict_driver.run(cfg, verdict_prompt,
                                         timeout_s=cfg.verdict_timeout_s)

    agreement, contested = "unavailable", False
    if substantive is not None:
        if substantive.verdict == "CENTRAL_CLAIM_NOT_ESTABLISHED" and verdict == "GREEN":
            agreement, contested = "contested", True
        elif substantive.verdict in ("STRONG", "SOUND_WITH_MINOR_CONCERNS") and verdict == "RED":
            agreement = "harness_harsher"
        elif substantive.verdict in ("SUBSTANTIAL_CONCERNS", "CENTRAL_CLAIM_NOT_ESTABLISHED") \
                and verdict == "GREEN":
            agreement = "model_harsher"
        else:
            agreement = "agree"

    report = EvalReport(
        paper_id=pid, title=doc.title, verdict=verdict, verdict_reason=reason,
        claim_status=status, claim_status_reason=status_why,
        reproduction_status=repro, execution_provenance=ran_as,
        verdict_if_cell_backed_only=verdict_sensitivity(findings, rec, outcomes=outcomes,
                                                        objects=objects),
        verdict_if_lens_severity_only=verdict_if_lens_severity_only(
            findings, rec, outcomes=outcomes, objects=objects),
        findings=findings, unasked_question=pick_unasked_question(reports),
        n_pages=doc.n_pages, n_sections=len(doc.sections), n_tables=len(doc.tables),
        n_numbers=len(doc.reported_numbers),
        n_figures=len(doc.figures), n_equations=len(doc.equations),
        lenses_run=[r.lens for r in reports], dropped_findings=dropped, probe=probe,
        experimental_chain=build_chain(findings, probe),
        grade_coverage={k: v for k, v in coverage.items() if k != "paper_id"},
        substantive_verdict=substantive, verdict_agreement=agreement, verdict_contested=contested,
        triage=triage_level, triage_reason=triage_why,
        targets_summary=_targets_summary(outcomes),
    )
    # The PRIMARY output, assembled once and stored, so the reviewer report, the machine
    # report and the evaluation layer all read the same join rather than three re-derivations
    # of it that can drift apart.
    report.scientific_findings = scientific_findings(report, target_set)

    # The four reader-facing rows, STORED rather than re-derived at render time, for the
    # same reason `scientific_findings` is: a block a reader sees and a trace a reader
    # checks it against must be one join, not two derivations of it. `unchecked_central`
    # is the tier-4 input and is computed from the same objects and outcomes the report
    # will print, so the scope row and the `## Central claims this review did not check`
    # section cannot disagree about how many there are.
    # --- the four layers that describe the review rather than the paper --------------
    # All four are read off artifacts that already exist, and none of them is consulted by
    # `overall_verdict`, `claim_status` or `triage` — asserted by parameter absence, so a
    # coverage number or a document observation cannot become a colour.
    report.artifact_state, report.review_path = artifact_axis(probe)
    report.document_observations = list(docintegrity.observe(doc))
    surf = coverage_mod.surface(doc)
    addressed, examined = coverage_numerators(target_set)
    # HOW THE PAPER WAS READ, produced by the stage that built the prompts and carried
    # through rather than re-derived here. `measure` cannot compute it: which lens
    # syntheses were actually completed is a fact about artifacts on disk, and a coverage
    # layer that went looking for them would be a coverage layer reading the review.
    report.coverage = coverage_mod.measure(
        surf, addressed=addressed, examined=examined,
        reading=artifacts_mod.ReadingRecord(**audit_stage.reading_record(cfg, pid, doc)))

    report.outcome = outcome_mod.derive(
        report, target_set, unchecked_central=len(unchecked_central(objects, outcomes)))

    # --- WHAT HAPPENS TO THE PAPER ----------------------------------------------------
    # Folded, never decided here. Every input is an artifact this function already holds,
    # and `harness.disposition` asserts the identity that keeps it honest:
    # STOP_MATERIAL_FAILURE is exactly `claim_status == VERIFIED_FAILURE`, which is
    # exactly what makes a paper RED. Two mechanisms that could disagree about whether a
    # paper failed would be two verdicts, and invariant 8 says there is one.
    #
    # The blockers are read from CENTRAL targets only. A blocked SUPPORTING target is real
    # information and belongs in the scope section; it is not a reason to tell a caller
    # the paper could not be checked, because the paper's conclusion does not rest on it.
    # THE MATERIAL one, not merely an established one: `basis` names the evidence that
    # carried a paper-level stop, so a defect that did not stop the paper must not appear
    # as the basis for one. Same call, same rule, same answer as `claim_status` above.
    _failed = material_target_failure(objects, outcomes, rec)[0]
    _by_id = {getattr(o, "target_id", ""): o for o in outcomes}
    _central_dispositions, _centralities = [], []
    for _obj in objects:
        _out = _by_id.get(getattr(_obj, "target_id", ""))
        if _out is None:
            continue
        _central_dispositions.append(getattr(_out, "disposition", ""))
        _centralities.append(getattr(_obj, "centrality", ""))
    report.disposition, report.disposition_basis, report.disposition_reason = \
        disposition_mod.derive(
            review_complete=True,
            claim_status=status,
            # Read from the MATERIAL failure alone — whichever kind it was. The old
            # `rec.provenance` fallback fired on any failed reconciliation, which would
            # now name a basis for a stop that did not happen.
            basis=disposition_mod.basis_for(
                failed_target_provenance=getattr(_failed, "provenance", ""),
                paper_arithmetic_failed=(
                    getattr(_failed, "disposition", "") == "PAPER_ARITHMETIC_CONTRADICTION")),
            central_blockers=disposition_mod.blockers_from(
                _central_dispositions, _centralities),
            central_unresolved=len(unresolved_central(objects, outcomes)),
            counted_major=len(material_concerns(findings)),
            # TIER 1 WITHOUT TIER 2, counted separately from `counted_major` because the
            # two are different evidence: a counted MAJOR is a lens's asserted severity,
            # this is a defect the harness itself established on a target no central claim
            # was established to depend on. Both hand the paper to a human; only this one
            # says the system PROVED something. Without it such a paper read CLEAN.
            established_non_material=(
                0 if _failed is not None
                else len(materiality.established_defects(outcomes))))

    # LAST, over the finished report: the self-audit reads `substantive_verdict` and
    # `grade`, so it has to run after both are attached. It writes nothing else and
    # changes no verdict — see `harness/selfaudit.py`.
    report.self_audit = selfaudit.audit(report, counted)

    md_path, json_path = root / "reports" / f"{pid}.md", root / "reports" / f"{pid}.json"
    # Two layers, deliberately. `<pid>.md` is the complete machine trace and `<pid>.json`
    # its structured form; `<pid>.review.md` is the one-to-two page report a human reads,
    # and `<pid>.ledger.json` is the traceability record that stands behind it. Handing a
    # reviewer the full trace and calling it a review is what this split ends.
    from .. import ledger as ledger_mod
    case_ledger = ledger_mod.build(report, target_set, probe)
    report.review_efficiency = case_ledger.efficiency
    ledger_path = root / "reports" / f"{pid}.ledger.json"
    review_path = root / "reports" / f"{pid}.review.md"
    report.ledger_path = str(ledger_path)
    report.reviewer_report_path = str(review_path)

    # LAST, because it reads everything else. A guarantee about accounting is checked
    # AGAINST the accounting, so it cannot be derived before the ledger is attached: doing
    # that reported "no ledger path; no efficiency accounting" as a defect in the harness
    # on every paper, which is what the `unmet` list is for and exactly why it is checked
    # from an artifact rather than asserted.
    report.guarantees = guarantees_mod.derive(report, target_set)

    state.write_json(ledger_path, case_ledger.model_dump())
    state.write_json(json_path, report.model_dump())
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_eval_report(report), encoding="utf-8")
    review_path.write_text(render_reviewer_report(report, target_set), encoding="utf-8")
    classes: dict[str, int] = {}
    for sf in report.scientific_findings:
        classes[sf.scientific_class] = classes.get(sf.scientific_class, 0) + 1
    classes = {k: classes[k] for k in CATEGORY_ORDER if k in classes}
    state.append_log(
        cfg, pid, artifact_type="eval_report", phase="report",
        headers={"verdict": verdict, "claim_status": status, "reproduction_status": repro,
                 "review_path": report.review_path, "artifact_state": report.artifact_state,
                 "document_observations": len(report.document_observations),
                 "surface_addressed": (report.coverage.addressed if report.coverage else 0),
                 "surface_size": (report.coverage.surface_size if report.coverage else 0),
                 "guarantees_unmet": (list(report.guarantees.unmet)
                                      if report.guarantees else []),
                 "scientific_classes": classes,
                 "ran_as": ran_as, "findings": len(findings), "dropped": dropped,
                 "severities": {s: sum(1 for f in findings if counted(f) == s) for s in _SEVERITY_RANK},
                 "lenses": report.lenses_run, "probe": probe.verdict if probe else None,
                 "grade_coverage": report.grade_coverage, "verdict_contested": contested,
                 "self_audit_failed": report.self_audit.failed if report.self_audit else []},
        path=str(md_path),
    )
    return {"paper_id": pid, "verdict": verdict, "reason": reason,
            "scientific_classes": classes,
            "review_path": report.review_path, "artifact_state": report.artifact_state,
            "document_observations": len(report.document_observations),
            "guarantees_unmet": (list(report.guarantees.unmet)
                                 if report.guarantees else []),
            "triage": triage_level, "triage_reason": triage_why,
            "disposition": report.disposition, "disposition_basis": report.disposition_basis,
            "disposition_reason": report.disposition_reason,
            "targets": report.targets_summary,
            "claim_status": status, "reproduction_status": repro, "ran_as": ran_as,
            "findings": len(findings), "dropped_unsubstantiated": dropped,
            "lenses": report.lenses_run, "probe": probe.verdict if probe else None,
            "report_md": str(md_path), "report": f"reports/{pid}.json",
            "reviewer_report_md": str(review_path), "ledger": f"reports/{pid}.ledger.json",
            "verdict_contested": contested,
            "self_audit_complete": bool(report.self_audit and report.self_audit.complete),
            "self_audit_failed": report.self_audit.failed if report.self_audit else []}


if __name__ == "__main__":  # self-check: python -m harness.stages.report
    def _f(fid, lens, sev, ref="", verifiable=False):
        return Finding(finding_id=fid, lens=lens, severity=sev, title=f"{fid} title",
                       statement="s", evidence_quote="q", evidence_ref=ref,
                       verifiable_by_experiment=verifiable)

    assert parse_magnitude("+3-5% top-1") == 3.0
    assert parse_magnitude("not a number") == 0.0
    assert parse_magnitude("") == 0.0

    # Model severity cannot grant itself rejection authority.
    assert overall_verdict([_f("a", "overclaim", "FATAL")])[0] == "GREEN"
    assert overall_verdict([_f(str(i), "protocol", "MAJOR") for i in range(3)])[0] == "GREEN"
    assert overall_verdict([_f(str(i), "protocol", "MAJOR") for i in range(40)])[0] == "GREEN"
    assert overall_verdict([_f(str(i), "protocol", "MINOR") for i in range(99)])[0] == "GREEN"
    assert overall_verdict([])[0] == "GREEN"
    assert overall_verdict([_f("a", "protocol", "MAJOR")])[0] == "GREEN"

    # ... and the epistemic state underneath keeps apart what the colour cannot.
    assert claim_status([_f("a", "overclaim", "FATAL")])[0] == "NOT_VERIFIED"
    assert claim_status([_f("a", "protocol", "MAJOR")])[0] == "NOT_VERIFIED"
    assert claim_status([])[0] == "NOT_VERIFIED"
    assert reproduction_status(None) == "NOT_ATTEMPTED"
    # Fails closed: an unknown provenance is never reported as the authors' own code.
    assert provenance_label("repo_exec") == "AUTHOR_REPOSITORY"
    assert provenance_label("driver") == "INDEPENDENT_REIMPLEMENTATION"
    assert provenance_label("nonsense") == "SYNTHESIZED_DIAGNOSTIC"

    # severity dominates; cell evidence outranks page evidence at equal severity
    order = rank([_f("m", "overclaim", "MINOR", "T0:r0:c0"),
                  _f("j", "protocol", "FATAL"),
                  _f("k", "contradiction", "MAJOR", "p3"),
                  _f("l", "contradiction", "MAJOR", "T1:r2:c3")])
    assert [f.finding_id for f in order] == ["j", "l", "k", "m"], order
    assert rank(order) == order, "rank must be stable/idempotent"

    rep = EvalReport(paper_id="p", title="T", verdict="RED", verdict_reason="because",
                     findings=order, unasked_question="why no baseline?", lenses_run=["protocol"])
    md = render_eval_report(rep)
    assert "🔴 RED — a material failure was established" in md and "why no baseline?" in md
    assert md.count("\n|") >= 4 and md.endswith("\n")
    assert _cell("a|b\nc", 99) == "a\\|b c", "pipes must be escaped or the table breaks"

    # --- S3 code reproduction ---------------------------------------------------------
    def _rec(status, **kw):
        kw.setdefault("provenance", "repo_exec")   # the ceiling is enforced here too
        return Reconciliation(table_ref="T1:r0:c1", claimed_raw="59.28", claimed_value=59.28,
                              noise_band=0.1, status=status, reason="r", **kw)

    # A failed reproduction ON A MATERIAL TARGET is RED on its own, outranking an
    # otherwise-clean panel. The materiality context is explicit: an established defect is
    # Tier 1, and only a target a central claim is established to depend on may stop the
    # paper (see `harness.materiality`). The join is `Reconciliation.target_id` -> object.
    from ..artifacts import DiscoveredObject
    _material = [DiscoveredObject(target_id="T1", materiality_basis="ABSTRACT_CLAIM")]
    _incidental = [DiscoveredObject(target_id="T1", materiality_basis="NONE")]
    _failed_rec = _rec("FAILED_REPRODUCTION", reproduced_value=64.1, delta_error=4.82,
                       target_id="T1")
    v, why = overall_verdict([], _failed_rec, objects=_material)
    assert v == "RED" and "Failed code reproduction" in why, why
    # The SAME established failure, on a target whose materiality was not established:
    # still established, still reported, and it does not reject the paper.
    v2, why2 = overall_verdict([], _failed_rec, objects=_incidental)
    assert v2 == "GREEN" and "did not establish" in why2, why2
    # And with NO materiality context at all it is not material either — a paper-level
    # stop may never depend on whether a caller passed an optional argument.
    assert overall_verdict([], _failed_rec)[0] == "GREEN"
    # claim_status and overall_verdict come out of ONE call and cannot disagree.
    for _objs in (_material, _incidental, None):
        _v, _ = overall_verdict([], _failed_rec, objects=_objs)
        _s, _ = claim_status([], _failed_rec, objects=_objs)
        assert (_v == "RED") == (_s == "VERIFIED_FAILURE"), (_objs, _v, _s)
    # …but the softer statuses never escalate, because none of them is evidence.
    assert overall_verdict([], _rec("INCONCLUSIVE"))[0] == "GREEN"
    assert overall_verdict([], _rec("RESOLVED_VERIFIED"))[0] == "GREEN"
    assert overall_verdict([], _rec("NOT_ATTEMPTED"))[0] == "GREEN"
    assert overall_verdict([_f("a", "overclaim", "FATAL")], _rec("RESOLVED_VERIFIED"))[0] == "GREEN", \
        "model severity cannot grant rejection authority even beside a verified cell"

    blocked = _repo_block(RepoAcquisition(
        url="https://github.com/x/y", status="blocked", reason="network gate closed"))
    assert "network gate is closed" in "\n".join(blocked)
    clean = _code_audit_block(CodeAudit(repo_path="r", files_scanned=3, lines_scanned=90))
    assert "not a clean bill of health" in "\n".join(clean), \
        "an empty static audit must not read as an endorsement"
    assert "Not run." in "\n".join(_code_audit_block(CodeAudit(skipped="nothing acquired")))
    # A CLASS-A HIT IS RENDERED; a class-B one is not. The rule is the authority audit
    # in `artifact_evidence.RULE_AUTHORITY`, not the severity: `leak-fit-on-test`
    # carries MAJOR and is class B, because what a `.fit` on a test-shaped name MEANS
    # needs a reading, and it reaches a referee through the authors'-code auditor
    # instead of through the rule.
    hit = "\n".join(_code_audit_block(CodeAudit(
        repo_path="r", files_scanned=1, lines_scanned=10, findings=[
            CodeAuditFinding(finding_id="code-01", rule_id="leak-unseeded-split",
                             category="data_leakage", severity="MINOR", title="t",
                             statement="s", file="a.py", line=7,
                             code_quote="train, test = random_split(ds, [9, 1])"),
            CodeAuditFinding(finding_id="code-02", rule_id="leak-fit-on-test",
                             category="data_leakage", severity="MAJOR", title="t",
                             statement="s", file="b.py", line=3,
                             code_quote="scaler.fit(X_test)")])))
    assert "`a.py:7`" in hit and "random_split(ds, [9, 1])" in hit
    assert "b.py" not in hit and "audited as unsafe for reviewer output" in hit

    body = "\n".join(_reconciliation_block(_rec("FAILED_REPRODUCTION", reproduced_value=64.1,
                                                delta_error=4.82, seeds_run=[0, 1, 2])))
    assert "FAILED CODE REPRODUCTION" in body and "0.1000" in body and "4.8200" in body

    full = render_eval_report(EvalReport(
        paper_id="p", title="T", verdict="RED", verdict_reason="because", findings=order,
        lenses_run=["protocol"],
        probe=ProbeResult(paper_id="p", verdict="calibration", calibration=True,
                          measured_std=0.0048, noise_band=0.0096, seeds_run=[0, 1],
                          repo=RepoAcquisition(url="https://github.com/x/y", status="cloned",
                                               entrypoint="eval.py", frameworks=["pytorch"]),
                          code_audit=CodeAudit(repo_path="r", files_scanned=2, lines_scanned=40),
                          reconciliation=_rec("RESOLVED_VERIFIED", reproduced_value=59.30,
                                              delta_error=0.02))))
    for heading in ("## Code acquisition", "## Static code audit", "## Table-cell reconciliation"):
        assert heading in full, f"missing section: {heading}"
    print("report self-check OK")
