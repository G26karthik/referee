"""What a machine enforced in THIS review, and what this system does not promise at all.

`python -m harness.guarantees` runs the self-check.

**The defect this module exists to prevent.** This information already existed and was
scattered across four places a referee never opens: `stages/report`'s `## Independent
grading` / `## Severity caveat` / `## Reviewer self-audit` sections, `evaluation.
LIMITATIONS`, `CLAUDE.md`'s Known limitations, and the assertions in
`manuscript/check_claims.py`. None of it was DERIVED. So the honest paragraph and the run
it described could drift apart silently, and the first thing to drift is always the
reassuring half: a sentence saying "every evidence pointer is re-verified" survives a
change that stops re-verifying one. A guarantee nobody checks per review is a marketing
claim with a citation.

So every entry here carries `holds`, established from a harness-written field on an
artifact a reader can open, plus `evidence` naming that field. A guarantee that stops
holding stops being printed as held, and appears instead under `unmet` — which is a defect
in this harness and never a finding about the paper.

**Three groups, because one list would teach a reader to ignore all of it.**

    PROCESS_GUARANTEES         enforced unconditionally, in every run, by deterministic
                               code. `holds=False` here is a HARNESS DEFECT and is the
                               only thing that reaches `unmet`.
    CONDITIONAL_PROPERTIES     true only when a gate was open or a surface was reachable.
                               Grading being off is a configuration, not a defect, so
                               these never reach `unmet` — they are printed as
                               non-guarantees, with the gate named.
    SCIENTIFIC_NON_GUARANTEES  properties this system does not have and cannot acquire by
                               running better. `holds` is False on EVERY input, asserted
                               by exhaustive sweep. `artifacts.GUARANTEE_KINDS` already
                               says the system makes no SCIENTIFIC guarantee; this is
                               where that becomes checkable rather than a comment.

The first version of this module had two groups, and on all seven corpus papers `unmet`
listed "severity was not independently graded" beside "the provenance ceiling held" — so
the artifact reported a harness defect for a gate the operator deliberately left shut.
That is the same shape as invariant 17: a property of this harness's configuration
presented as a finding.

**Nothing here decides anything.** `assess` takes booleans and returns sentences. It sets
no severity, writes no verdict and touches no colour; the section it renders sits BELOW
`## Review outcome` precisely so it cannot be read as the outcome. `evaluation.py`,
`grading.derive`, `taxonomy.classify`, `planner.classify`, `priority.score` and
`stages.report`'s `overall_verdict` / `claim_status` / `triage` do not and must not read
it, and `tests/test_guarantees.py` asserts that by parameter absence.

**On what the prose may claim.** Executing an author's code inside review, checking
references, and screening figures are all done elsewhere and are not novel; autonomy is
not an achievement and every serious 2026 venue keeps the human. The statements below
therefore describe mechanisms and their limits and claim priority over nothing. The
non-guarantee list is deliberately the sharper half.
"""
from __future__ import annotations

from . import materiality
from .artifacts import BLOCKED_DISPOSITIONS, Guarantee, ReviewGuarantees
from .grading import RANK
from .provenance import admits

# --------------------------------------------------------------------------- #
# THE CLOSED, PAPER-AGNOSTIC VOCABULARY
# --------------------------------------------------------------------------- #
# Enforced in every run by deterministic code. A False here means a mechanism this system
# claims to enforce did not, on this paper — which is a bug in the harness, so it is the
# only group that reaches `unmet`.
PROCESS_GUARANTEES: tuple[str, ...] = (
    "EVERY_EVIDENCE_POINTER_RE_VERIFIED",        # invariant 1
    "PROVENANCE_CEILING_HELD",                   # invariant 3
    "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION",   # invariants 4, 5
    "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED",    # invariants 6, 7
    "EVERY_UNRESOLVED_STATE_NAMED",              # invariants 16, 21
    "ACCOUNTING_REPRODUCIBLE",                   # invariants 13, 21
    "SEVERITY_ONLY_CAPPED",                      # invariant 11
    "NO_MODEL_WROTE_THE_DECISION",               # invariant 8
    "NO_EXPERIMENT_DOWNSCALED",                  # invariant 9
)

# True only when a gate was open, an artifact existed, or a surface was reachable. Their
# absence is a limit on this run, printed with the gate named — never a defect.
CONDITIONAL_PROPERTIES: tuple[str, ...] = (
    "SEVERITY_INDEPENDENTLY_GRADED",
    "PANEL_ISOLATION_RECORDED",
    "HOLISTIC_JUDGEMENT_PRESENT",
    "ARTIFACT_EXECUTED",
    "SURFACE_FULLY_EXAMINED",
    "PROSE_FULLY_PRESENTED",
)

# Properties this system does not have. `holds` is False for every input — running better,
# opening every gate and leasing every machine does not reach any of them, because each
# needs something this system has no route to: adjudicated ground truth, a literature
# index, or a definition of "important" it is not entitled to invent.
SCIENTIFIC_NON_GUARANTEES: tuple[str, ...] = (
    "COMPLETE_ISSUE_RECALL",
    "REFEREE_ACCURACY_MEASURED",
    "PAPER_CORRECTNESS",
    "NOVELTY_ASSESSED",
    "EVERY_IMPORTANT_EXPERIMENT_EXECUTABLE",
    "HUMAN_REFEREE_SUBSTITUTE",
)

ALL_KEYS: tuple[str, ...] = (
    PROCESS_GUARANTEES + CONDITIONAL_PROPERTIES + SCIENTIFIC_NON_GUARANTEES)

# `Guarantee.kind` for each key. Only the third group is SCIENTIFIC: a conditional
# property is still a property of the PROCESS, so "grading did not run" must not read as
# a statement about the paper's science.
KIND: dict[str, str] = {
    **{k: "PROCESS" for k in PROCESS_GUARANTEES},
    **{k: "PROCESS" for k in CONDITIONAL_PROPERTIES},
    **{k: "SCIENTIFIC" for k in SCIENTIFIC_NON_GUARANTEES},
}

# The positive statement of the property, in a referee's words. FIXED TEXT: it describes
# the system, so nothing here interpolates a paper, a count or an outcome. That is what
# makes the list quotable — and what makes `holds` the only thing a review can move.
STATEMENT: dict[str, str] = {
    "EVERY_EVIDENCE_POINTER_RE_VERIFIED":
        "Every finding kept in this review cites a passage the harness re-read off the "
        "parsed document. A finding whose citation could not be re-verified was discarded "
        "and the discard counted.",
    "PROVENANCE_CEILING_HELD":
        "Only the authors' own commit-verified checkout, or a human-written reproduction "
        "an operator sealed, may reconcile against a quantity the paper prints - in "
        "either direction. A probe this harness synthesised can neither convict nor "
        "acquit.",
    "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION":
        "Every process this review started was permitted by one conjunctive "
        "authorization over the gate, the backend, the provenance, a verified commit, "
        "experiment/metric/configuration identity, capability and resources. Failure of "
        "any conjunct refuses and names itself.",
    "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED":
        "A capability, environment, dependency, platform or resource failure yields an "
        "inconclusive result and can never contribute a failure of the paper. Only a "
        "crash after the experiment demonstrably started may convict.",
    # Worded to match exactly what `_check_refusals` reads. The first draft said "every
    # target that did not settle its question carries a named refusal", which is more
    # than the check establishes: a target this run's budget never reached is
    # NOT_ATTEMPTED and carries no reason, and claiming otherwise would make the
    # guarantee a sentence the artifact does not support.
    "EVERY_UNRESOLVED_STATE_NAMED":
        "Every target carries a named disposition from the closed vocabulary, and every "
        "refusal carries the gate that produced it. A blocked target ends that target "
        "and never the paper.",
    "ACCOUNTING_REPRODUCIBLE":
        "Every count this review prints is re-derivable from artifacts on disk: a ledger "
        "recording the funnel term by term, and one execution record per process "
        "actually started.",
    "SEVERITY_ONLY_CAPPED":
        "No mechanism in this review raised a severity. Every counted severity is at or "
        "below the severity the asserting lens wrote; grading, the evidence ceiling and "
        "every self-consistency cap can only demote.",
    "NO_MODEL_WROTE_THE_DECISION":
        "The paper-level decision is a deterministic projection of what was established, "
        "and what was established has a machine-checkable cause. A model's whole-paper "
        "opinion is printed and consumed by no threshold.",
    "NO_EXPERIMENT_DOWNSCALED":
        "No experiment was shrunk, substituted or downscaled to make it fit this host. A "
        "resource shortfall is a refusal, never a smaller run reported as the paper's.",
    "SEVERITY_INDEPENDENTLY_GRADED":
        "Every serious candidate was weighed by a second, blinded reader before its "
        "severity was counted.",
    "PANEL_ISOLATION_RECORDED":
        "Each audit lens records the tool policy that was enforced on it, so per-lens "
        "isolation is established rather than asserted.",
    "HOLISTIC_JUDGEMENT_PRESENT":
        "The submission was also read as a whole, separately from counting findings.",
    "ARTIFACT_EXECUTED":
        "The authors' own code was obtained and run, so at least one printed quantity "
        "was checked against what their artifact produces.",
    "SURFACE_FULLY_EXAMINED":
        "Every addressable unit of the paper had a verification route pursued.",
    "PROSE_FULLY_PRESENTED":
        "The audit lenses were shown all of the extracted prose, so no finding was "
        "foreclosed by a prompt budget.",
    "COMPLETE_ISSUE_RECALL":
        "Every issue in the paper that matters was found.",
    "REFEREE_ACCURACY_MEASURED":
        "This system's precision, recall or agreement with human referees is measured.",
    "PAPER_CORRECTNESS":
        "The paper is correct.",
    "NOVELTY_ASSESSED":
        "The paper's novelty and its relation to prior art were checked.",
    "EVERY_IMPORTANT_EXPERIMENT_EXECUTABLE":
        "Every experiment that would matter to this paper's argument can be executed "
        "from what the paper and its artifact publish.",
    "HUMAN_REFEREE_SUBSTITUTE":
        "This review can stand in for a human referee's judgement.",
}

# The reader-facing sentence when the property does NOT hold. A separate table rather than
# a negated `STATEMENT`, because "not every issue was found" is a weaker and vaguer
# sentence than the one a referee needs, and because the useful half of a non-guarantee is
# WHY it cannot hold. Also fixed text: the per-review part is the count in `evidence`.
NOT_HELD: dict[str, str] = {
    "EVERY_EVIDENCE_POINTER_RE_VERIFIED":
        "a kept finding carries no harness-written evidence class, so its citation was "
        "not machine re-verified - a defect in this harness",
    "PROVENANCE_CEILING_HELD":
        "a reconciliation settled a printed quantity on a provenance the ceiling does "
        "not admit - a defect in this harness",
    "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION":
        "a process was started without a recorded authorization - a defect in this "
        "harness",
    "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED":
        "an infrastructure failure was recorded as a failed reproduction - a defect in "
        "this harness",
    "EVERY_UNRESOLVED_STATE_NAMED":
        "a target ended with no named refusal - a defect in this harness",
    "ACCOUNTING_REPRODUCIBLE":
        "a printed count has no artifact behind it - a defect in this harness",
    "SEVERITY_ONLY_CAPPED":
        "a counted severity is above the severity the asserting lens wrote - a defect in "
        "this harness",
    "NO_MODEL_WROTE_THE_DECISION":
        "the decision does not project from what was established - a defect in this "
        "harness",
    "NO_EXPERIMENT_DOWNSCALED":
        "a resource-blocked target started a process anyway - a defect in this harness",
    "SEVERITY_INDEPENDENTLY_GRADED":
        "severity was not independently graded (`SH_ALLOW_GRADING`): every counted "
        "severity is the asserting lens's own word",
    "PANEL_ISOLATION_RECORDED":
        "the audit lenses' tool policy is unrecorded, so their isolation is asserted and "
        "not established",
    "HOLISTIC_JUDGEMENT_PRESENT":
        "the submission was not judged as a whole (`SH_ALLOW_SUBSTANTIVE_VERDICT`)",
    "ARTIFACT_EXECUTED":
        "the authors' own code was not run, so no printed quantity was checked against "
        "their artifact",
    "SURFACE_FULLY_EXAMINED":
        "most of the paper's addressable surface had no route pursued",
    "PROSE_FULLY_PRESENTED":
        "the lenses were shown only part of the extracted prose, which bounds what any "
        "of them could have found",
    "COMPLETE_ISSUE_RECALL":
        "issue recall is neither claimed nor measurable here: structural coverage says "
        "nothing about whether the issues that matter were found",
    "REFEREE_ACCURACY_MEASURED":
        "this system's accuracy is unmeasured, not measured-and-good: no adjudicated "
        "ground truth exists for this corpus",
    "PAPER_CORRECTNESS":
        "nothing here says the paper is correct; the absence of an established failure is "
        "not support",
    "NOVELTY_ASSESSED":
        "this system has no prior-art search route; novelty and its relation to earlier "
        "work are not assessed at all",
    "EVERY_IMPORTANT_EXPERIMENT_EXECUTABLE":
        "not every experiment that would matter is executable from what a paper "
        "publishes, and the set that ran is not the set that matters",
    "HUMAN_REFEREE_SUBSTITUTE":
        "this is a first pass whose derivation a human can re-check, not a substitute "
        "for a referee's judgement",
}

# The compact clause for the rendered section. The long forms above are for the artifact,
# where length costs a reader nothing; a bounded markdown section cannot carry twenty-one
# of them (invariant 19), so each key owns one short clause here and `render` joins them.
SHORT: dict[str, str] = {
    # "re-read off the paper", not "re-verified": `stages.report.SUPPORT_LANGUAGE` bans
    # "verified" from prose a NOT_VERIFIED review prints, and this section is prose a
    # NOT_VERIFIED review prints. The word describes an evidence pointer here and not the
    # paper, so it would have been defensible and it would also have been the one
    # sentence in this section a skimming referee could misread as a result.
    "EVERY_EVIDENCE_POINTER_RE_VERIFIED": "every evidence pointer re-read off the paper",
    "PROVENANCE_CEILING_HELD": "the provenance ceiling held both ways",
    "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION": "one conjunctive gate authorized every run",
    "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED": "no infrastructure failure convicted",
    "EVERY_UNRESOLVED_STATE_NAMED": "every refusal named per target",
    "ACCOUNTING_REPRODUCIBLE": "every count re-derivable from the ledger",
    "SEVERITY_ONLY_CAPPED": "no mechanism raised a severity",
    "NO_MODEL_WROTE_THE_DECISION": "no model wrote the decision",
    "NO_EXPERIMENT_DOWNSCALED": "no experiment downscaled to fit",
    "SEVERITY_INDEPENDENTLY_GRADED":
        "severity not independently graded (`SH_ALLOW_GRADING`): it is the lens's own",
    "PANEL_ISOLATION_RECORDED": "lens tool policy unrecorded",
    "HOLISTIC_JUDGEMENT_PRESENT": "the submission not judged as a whole",
    "ARTIFACT_EXECUTED": "the authors' code was not run",
    "SURFACE_FULLY_EXAMINED": "the addressable surface not fully examined",
    "PROSE_FULLY_PRESENTED": "the lenses saw part of the extracted prose",
    "COMPLETE_ISSUE_RECALL": "issue recall neither claimed nor measurable",
    "REFEREE_ACCURACY_MEASURED": "accuracy unmeasured: no adjudicated ground truth",
    "PAPER_CORRECTNESS": "nothing here says the paper is correct",
    "NOVELTY_ASSESSED": "no prior-art search route; novelty not assessed",
    "EVERY_IMPORTANT_EXPERIMENT_EXECUTABLE": "not every important experiment is executable",
    "HUMAN_REFEREE_SUBSTITUTE": "a first pass, not a substitute for a referee",
}

# WHICH FIELD ON WHICH ARTIFACT settles `holds`. Paper-agnostic, and the reason this is a
# table rather than an f-string at the call site: an entry with no artifact behind it
# cannot be added without noticing, because `_self_check` sweeps this map against
# `ALL_KEYS` and refuses a blank. The per-review numbers are appended by `derive`.
ESTABLISHED_BY: dict[str, str] = {
    "EVERY_EVIDENCE_POINTER_RE_VERIFIED":
        "EvalReport.findings[*].evidence_class + EvalReport.dropped_findings",
    "PROVENANCE_CEILING_HELD":
        "Reconciliation.provenance on ProbeResult.reconciliation and "
        "TargetOutcome.reconciliation, against provenance.admits",
    "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION":
        "ProbeResult.authorization.allowed + TargetOutcome.launched",
    "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED":
        "TargetOutcome.failure_class against TargetOutcome.establishes_failure",
    "EVERY_UNRESOLVED_STATE_NAMED":
        "TargetOutcome.disposition + TargetOutcome.reason",
    "ACCOUNTING_REPRODUCIBLE":
        "EvalReport.ledger_path + EvalReport.review_efficiency + ProbeResult.execution_log "
        "+ every established failure joinable to a DiscoveredObject",
    "SEVERITY_ONLY_CAPPED":
        "Finding.counted_severity against Finding.severity, ranked by grading.RANK",
    "NO_MODEL_WROTE_THE_DECISION":
        "EvalReport.verdict against EvalReport.claim_status and its "
        "machine-checkable cause",
    "NO_EXPERIMENT_DOWNSCALED":
        "TargetOutcome.launched on every RESOURCE_BLOCKED target",
    "SEVERITY_INDEPENDENTLY_GRADED": "EvalReport.grade_coverage",
    "PANEL_ISOLATION_RECORDED": "the lens sidecar's tool_policy, passed in by the caller",
    "HOLISTIC_JUDGEMENT_PRESENT": "EvalReport.substantive_verdict",
    "ARTIFACT_EXECUTED": "TargetOutcome.launched + TargetOutcome.provenance",
    "SURFACE_FULLY_EXAMINED": "CoverageReport.examined / CoverageReport.surface_size",
    "PROSE_FULLY_PRESENTED": "CoverageReport.prose_presented_fraction",
    "COMPLETE_ISSUE_RECALL":
        "CoverageReport.semantic_coverage, which is not_machine_detectable unconditionally",
    "REFEREE_ACCURACY_MEASURED":
        "no artifact: no adjudicated ground truth exists for this corpus",
    "PAPER_CORRECTNESS":
        "no artifact: GREEN is the absence of an established failure, not support",
    # The prior-art search route was deleted (2026-09-20 simplification pass) after
    # producing zero structurally-bound results across the whole corpus it ever ran
    # against. Novelty is now simply not assessed, which is a plainer sentence than the
    # route it replaces, not a weaker one: even a bounded search that ran and matched
    # nothing was never entitled to establish novelty either.
    "NOVELTY_ASSESSED":
        "no artifact: this system has no route to prior-art search",
    "EVERY_IMPORTANT_EXPERIMENT_EXECUTABLE":
        "no artifact: 'important' is not a property this system may assign",
    "HUMAN_REFEREE_SUBSTITUTE":
        "no artifact: a scope statement about this system, not a measurement",
}

# Failure classes that are facts about a host, an artifact or one of this harness's own
# gates. `runtime_failure` and `timeout` are DELIBERATELY ABSENT: invariant 7 admits
# exactly one conviction route, a crash after the experiment demonstrably started, and a
# check that forbade it would report a harness defect for the one case the harness allows.
_INFRASTRUCTURE_FAILURE: tuple[str, ...] = (
    "environment_incompatible", "dependency_missing", "invalid_invocation",
    "startup_failure", "experiment_unidentified", "metric_unbound",
    "configuration_unmatched", "execution_unauthorized", "resources_insufficient",
    "commit_mismatch", "backend_unavailable", "credentials_unavailable",
)

# Mirrors `stages.report.MATERIAL_SEVERITY`, which cannot be imported here: `stages.report`
# renders this module's section, so the dependency runs the other way. `_self_check`
# asserts the two are the same set, which is the drift guard the copy needs.
_MATERIAL_SEVERITY: tuple[str, ...] = ()

# Every evidence class except the one that means "nothing checked out". `unverified`
# findings are dropped upstream and never reach a report; an EMPTY class is the case this
# check exists for, because a finding assembled by hand or by an older schema carries one.
_VERIFIED_EVIDENCE = ("cell_verified", "prose_verified", "caption_verified",
                      "equation_verified")

# The section is bounded BY CONSTRUCTION, not by hoping twenty-one clauses stay short.
# The target was 900 characters; twenty-one closed clauses do not fit, and the half that
# would have been elided to reach 900 is the honest half - which is the one defect this
# whole section exists to prevent. So the cap is 1100 and the two halves have their own
# budgets, with the larger one going to what was NOT established.
_MAX_SECTION_CHARS = 1100
_MAX_HELD_CHARS = 400
_MAX_NOT_HELD_CHARS = 620


def _clauses(items: list[str], budget: int) -> str:
    """Join short clauses with `·`, stopping at `budget` and saying how many were dropped.

    Truncating silently was the alternative and it is worse than printing nothing: a
    reader cannot tell a system that guarantees six things from one whose seventh clause
    did not fit.
    """
    out: list[str] = []
    used = 0
    for i, clause in enumerate(items):
        cost = len(clause) + (3 if out else 0)
        if used + cost > budget and out:
            return " · ".join(out) + f" (+{len(items) - i} more, in the ledger)"
        out.append(clause)
        used += cost
    return " · ".join(out)


def assess(*, every_pointer_verified: bool = False,
           provenance_clean: bool = False,
           execution_authorized: bool = False,
           infrastructure_isolated: bool = False,
           every_refusal_named: bool = False,
           accounting_traceable: bool = False,
           severity_only_capped: bool = False,
           decision_is_the_table: bool = False,
           no_downscaled_experiment: bool = False,
           grading_complete: bool = False,
           lens_policy_enforced: bool = False,
           holistic_judgement: bool = False,
           artifact_executed: bool = False,
           surface_fully_examined: bool = False,
           prose_fully_presented: bool = False) -> tuple[Guarantee, ...]:
    """The derivation table: booleans in, one `Guarantee` per key of `ALL_KEYS`, in order.

    BOOLEANS ONLY, keyword-only, and every default False. The plan for this module allowed
    vocabulary strings as well; none turned out to be needed, and every parameter being a
    boolean is strictly stronger - "a paper with a CONFOUND gets a weaker guarantee", "if
    this venue, claim more" and "if the delta is small, claim less" are inexpressible here
    rather than merely absent, the same discipline `grading.derive` uses. The defaults are
    False so a caller that forgets a term under-claims; a default of True would make
    forgetting one a silent promotion.

    No SCIENTIFIC key is a parameter, because none of them can be satisfied by any input.
    The exhaustive sweep in `_self_check` asserts that over the whole 2**15 input space.
    """
    held = {
        "EVERY_EVIDENCE_POINTER_RE_VERIFIED": every_pointer_verified,
        "PROVENANCE_CEILING_HELD": provenance_clean,
        "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION": execution_authorized,
        "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED": infrastructure_isolated,
        "EVERY_UNRESOLVED_STATE_NAMED": every_refusal_named,
        "ACCOUNTING_REPRODUCIBLE": accounting_traceable,
        "SEVERITY_ONLY_CAPPED": severity_only_capped,
        "NO_MODEL_WROTE_THE_DECISION": decision_is_the_table,
        "NO_EXPERIMENT_DOWNSCALED": no_downscaled_experiment,
        "SEVERITY_INDEPENDENTLY_GRADED": grading_complete,
        "PANEL_ISOLATION_RECORDED": lens_policy_enforced,
        "HOLISTIC_JUDGEMENT_PRESENT": holistic_judgement,
        "ARTIFACT_EXECUTED": artifact_executed,
        "SURFACE_FULLY_EXAMINED": surface_fully_examined,
        "PROSE_FULLY_PRESENTED": prose_fully_presented,
    }
    # `key` is carried as an extra field (`artifacts._Base` is extra="allow") because
    # `Guarantee` declares none, and a consumer reading the JSON needs to match an entry
    # back to the vocabulary rather than to a sentence that may be reworded.
    return tuple(
        Guarantee(key=k, kind=KIND[k], statement=STATEMENT[k],
                  holds=bool(held.get(k, False)), evidence=ESTABLISHED_BY[k])
        for k in ALL_KEYS)


# --------------------------------------------------------------------------- #
# READING THE ARTIFACTS
# --------------------------------------------------------------------------- #
# Each `_check_*` returns (holds, the per-review detail that goes in `evidence`). They are
# separate functions so a test can drive one at a time, and so a check that cannot be made
# from an artifact has nowhere to hide: it would have no function here, and a key with no
# function is caught by `_self_check`.

def _reconciliations(report, outcomes) -> list:
    recs = []
    probe = getattr(report, "probe", None)
    if probe is not None and getattr(probe, "reconciliation", None) is not None:
        recs.append(probe.reconciliation)
    for o in outcomes:
        rec = getattr(o, "reconciliation", None)
        if rec is not None:
            recs.append(rec)
    return recs


def _launched(report, outcomes) -> int:
    """Processes this review actually started (invariant 21), without double-counting.

    `TargetOutcome.launched` is COPIED from `ProbeResult.executions`, so adding the two
    reported 20 processes for the ten `iclr` actually started. `max` is right for both
    real shapes: a per-target run has both terms equal, and the legacy single-probe path
    has only the second.
    """
    probe = getattr(report, "probe", None)
    per_target = sum(int(getattr(o, "launched", 0) or 0) for o in outcomes)
    probe_runs = int(getattr(probe, "executions", 0) or 0) if probe is not None else 0
    return max(per_target, probe_runs)


def _check_pointers(report) -> tuple[bool, str]:
    """EVERY pointer, which is what the guarantee is called.

    A concern can depend on more than one location — the abstract against the conclusion,
    the prose against the cell it summarises — and each side carries its own quotation,
    its own reference and its own harness-written evidence class. Checking the primary
    citation alone would let a cross-section finding satisfy a guarantee whose name
    promises all of them, which is the exact shape of overclaim this module exists to
    refuse. `stages/audit._coerce` already drops such a finding whole, so a failure here
    means that drop did not happen.
    """
    fs = list(getattr(report, "findings", None) or [])
    bad, sides = [], 0
    for f in fs:
        classes = [getattr(f, "evidence_class", "")]
        for side in (getattr(f, "additional_evidence", None) or []):
            sides += 1
            classes.append(getattr(side, "evidence_class", ""))
        if any(c not in _VERIFIED_EVIDENCE for c in classes):
            bad.append(getattr(f, "finding_id", "") or "(unnamed)")
    dropped = int(getattr(report, "dropped_findings", 0) or 0)
    if bad:
        return False, (f"{len(bad)} of {len(fs)} kept finding(s) carry an unverified "
                       f"evidence pointer: {', '.join(bad[:4])}")
    return True, (f"{len(fs)} kept finding(s) carrying {len(fs) + sides} evidence "
                  f"pointer(s), all re-verified; {dropped} discarded")


def _check_provenance(report, outcomes) -> tuple[bool, str]:
    recs = _reconciliations(report, outcomes)
    settling = [r for r in recs
                if getattr(r, "status", "") in ("FAILED_REPRODUCTION", "RESOLVED_VERIFIED")]
    bad = [r for r in settling if not admits(getattr(r, "provenance", ""))]
    if bad:
        return False, (f"{len(bad)} reconciliation(s) settled a printed quantity on an "
                       f"inadmissible provenance")
    if settling:
        return True, (f"{len(settling)} settling reconciliation(s), all on admitted "
                      f"provenance")
    return True, ("no reconciliation settled a printed quantity, so the ceiling was not "
                  "reached")


def _check_authorization(report, outcomes) -> tuple[bool, str]:
    """Fold EVERY execution record for this paper, not only the paper-level probe.

    A paper can execute more than one target — a secondary target, a reconstruction's own
    run — and each carries its OWN `authorization` (`TargetOutcome.authorized`, copied in
    `stages.probe.outcome_for` from that target's own `ProbeResult`). Reading only
    `report.probe.authorization` checked whichever ONE execution happens to be attached
    to the paper-level field and reported the guarantee held whenever that one was fine,
    even if a different target's own execution ran with no authorization at all. Per-
    target outcomes are checked when they exist; the paper-level field is the fallback
    only for the legacy shape that never split targets — a single top-level `probe` and no
    `outcomes` at all.
    """
    launched = _launched(report, outcomes)
    if launched == 0:
        # Vacuous, and the evidence says so. A vacuous guarantee printed as though it had
        # been exercised is the second-worst thing this module can do.
        return True, "no process was started, so this held without being exercised"
    checks: list[tuple[str, bool | None]] = []
    if outcomes:
        for o in outcomes:
            if int(getattr(o, "launched", 0) or 0) > 0:
                checks.append((getattr(o, "target_id", "") or "?",
                               getattr(o, "authorized", None)))
    else:
        probe = getattr(report, "probe", None)
        if probe is not None and int(getattr(probe, "executions", 0) or 0) > 0:
            auth = getattr(probe, "authorization", None)
            checks.append(("(paper-level)",
                           bool(getattr(auth, "allowed", False)) if auth is not None else None))
    if not checks:
        # `launched` counted via `_launched`'s own `probe.executions` fallback but no
        # per-target outcome and no paper-level probe carried a record to check — an
        # accounting gap this guarantee must not read as authorized-by-default.
        return False, f"{launched} process(es) started with no authorization record to check"
    bad = [tid for tid, ok in checks if ok is not True]
    if bad:
        return False, (f"{len(bad)} of {len(checks)} execution record(s) started with no "
                       f"authorization, or one that did not allow: {', '.join(bad[:4])}")
    return True, f"{len(checks)} execution record(s), every one authorized"


def _check_infrastructure(report, outcomes) -> tuple[bool, str]:
    infra = [o for o in outcomes
             if getattr(o, "failure_class", "none") in _INFRASTRUCTURE_FAILURE]
    bad = [getattr(o, "target_id", "?") for o in infra
           if getattr(o, "establishes_failure", False)
           or getattr(o, "disposition", "") == "FAILED_REPRODUCTION"]
    if bad:
        return False, (f"target(s) {', '.join(bad[:4])} recorded an infrastructure "
                       f"failure as a failed reproduction")
    return True, (f"{len(infra)} target(s) failed on infrastructure; none contributed a "
                  f"failure of the paper")


def _check_refusals(report, outcomes) -> tuple[bool, str]:
    from .artifacts import TARGET_DISPOSITIONS
    unnamed = [getattr(o, "target_id", "?") for o in outcomes
               if getattr(o, "disposition", "") not in TARGET_DISPOSITIONS]
    silent = [getattr(o, "target_id", "?") for o in outcomes
              if getattr(o, "disposition", "") in BLOCKED_DISPOSITIONS
              and not (getattr(o, "reason", "") or "").strip()]
    if unnamed or silent:
        return False, (f"{len(unnamed)} target(s) with an unrecognised disposition, "
                       f"{len(silent)} refusal(s) with no reason")
    blocked = sum(1 for o in outcomes
                  if getattr(o, "disposition", "") in BLOCKED_DISPOSITIONS)
    return True, f"{len(outcomes)} target outcome(s), {blocked} of them named refusals"


def _check_accounting(report, outcomes, objects=None) -> tuple[bool, str]:
    ledger = (getattr(report, "ledger_path", "") or "").strip()
    efficiency = dict(getattr(report, "review_efficiency", None) or {})
    probe = getattr(report, "probe", None)
    # EVERY execution record this paper produced, not only the paper-level probe's. A
    # secondary target or a reconstruction writes its OWN `execution.jsonl`
    # (`stages.probe.outcome_for` carries it as `TargetOutcome.execution_ref`) that
    # `report.probe.execution_log` never points to — reading only the latter reported
    # "every count re-derivable" whenever the PRIMARY target happened to log one, even
    # while a different target that launched processes left none.
    logged = 0
    unlogged: list[str] = []
    if outcomes:
        for o in outcomes:
            if int(getattr(o, "launched", 0) or 0) <= 0:
                continue
            if (getattr(o, "execution_ref", "") or "").strip():
                logged += 1
            else:
                unlogged.append(getattr(o, "target_id", "") or "?")
    elif probe is not None and int(getattr(probe, "executions", 0) or 0) > 0:
        if (getattr(probe, "execution_log", "") or "").strip():
            logged = 1
        else:
            unlogged.append("(paper-level)")
    missing = []
    if not ledger:
        missing.append("no ledger path")
    if not efficiency:
        missing.append("no efficiency accounting")
    if unlogged:
        missing.append(f"{len(unlogged)} of {logged + len(unlogged)} execution record(s) "
                       f"with no execution log: {', '.join(unlogged[:4])}")
    # A target that ESTABLISHED a defect and has no object in the set it is being folded
    # over. Materiality cannot be assessed for it, so it is not material (never material
    # by default) — and the disagreement between the outcomes and the objects is a state
    # inconsistency in this harness, which is exactly what this guarantee is for. It is
    # never evidence about the paper and reaches no threshold.
    orphans = materiality.unjoinable_established_failures(objects, outcomes)
    if orphans:
        missing.append(
            f"{len(orphans)} established failure(s) with no discovered object to assess "
            f"materiality against ({', '.join(orphans[:5])})")
    if missing:
        return False, "; ".join(missing)
    return True, (f"ledger at {ledger}, {len(efficiency)} accounting term(s)"
                  + (f", {logged} execution record(s) logged" if logged else ""))


def _check_severity_caps(report) -> tuple[bool, str]:
    fs = list(getattr(report, "findings", None) or [])
    raised = []
    capped = 0
    for f in fs:
        lens_sev = getattr(f, "severity", "") or ""
        counted = getattr(f, "counted_severity", "") or ""
        if not counted:
            continue
        if RANK.get(counted, 0) > RANK.get(lens_sev, 0):
            raised.append(getattr(f, "finding_id", "") or "(unnamed)")
        elif RANK.get(counted, 0) < RANK.get(lens_sev, 0):
            capped += 1
    if raised:
        return False, f"{len(raised)} finding(s) counted above the lens's own severity"
    return True, (f"{len(fs)} finding(s); {capped} capped below the lens's own severity, "
                  f"none above it")


def _check_decision(report, outcomes) -> tuple[bool, str]:
    verdict = (getattr(report, "verdict", "") or "").strip().upper()
    claim = (getattr(report, "claim_status", "") or "").strip().upper()
    established = claim == "VERIFIED_FAILURE"
    if (verdict == "RED") != established:
        return False, (f"verdict {verdict or '(none)'} does not project from claim status "
                       f"{claim or '(none)'}")
    if established:
        fatal = sum(1 for f in (getattr(report, "findings", None) or [])
                    if (getattr(f, "counted_severity", "") or getattr(f, "severity", ""))
                    in _MATERIAL_SEVERITY)
        target = [getattr(o, "target_id", "?") for o in outcomes
                  if getattr(o, "establishes_failure", False)]
        recs = [r for r in _reconciliations(report, outcomes)
                if getattr(r, "status", "") == "FAILED_REPRODUCTION"
                and admits(getattr(r, "provenance", ""))]
        if not (fatal or target or recs):
            return False, ("a material failure was established with no deterministic "
                           "failing target or admissible failed reproduction behind it")
        return True, (f"{verdict} from claim status {claim}: "
                      f"{len(target)} failing target(s), {len(recs)} admissible failed "
                      f"reconciliation(s)")
    return True, (f"{verdict or 'GREEN'} from claim status {claim or 'NOT_VERIFIED'}, "
                  f"with no material failure established")


def _check_no_downscale(report, outcomes) -> tuple[bool, str]:
    short = [o for o in outcomes if getattr(o, "disposition", "") == "RESOURCE_BLOCKED"
             or getattr(o, "failure_class", "") == "resources_insufficient"]
    ran = [getattr(o, "target_id", "?") for o in short
           if int(getattr(o, "launched", 0) or 0) > 0]
    if ran:
        return False, (f"target(s) {', '.join(ran[:4])} were short of resources and "
                       f"started a process anyway")
    return True, (f"{len(short)} target(s) refused for resources; none was run at a "
                  f"reduced size")


def _check_grading(report) -> tuple[bool, str]:
    cov = dict(getattr(report, "grade_coverage", None) or {})
    candidates = int(cov.get("candidates", 0) or 0)
    graded = int(cov.get("graded", 0) or 0)
    if candidates == 0:
        # NO SERIOUS CANDIDATE IS NOT A GRADED REVIEW. The tempting reading is "nothing
        # needed grading, so this holds"; it does not, because the property is that a
        # second reader weighed what the verdict counts, and here nothing established
        # that either way. Reporting it as held would let a review with no findings claim
        # the strongest process property in the list.
        return False, "no serious candidate was raised, so no independent grade exists"
    return graded >= candidates, f"{graded} of {candidates} serious candidate(s) graded"


def _check_coverage(report) -> tuple[tuple[bool, str], tuple[bool, str]]:
    cov = getattr(report, "coverage", None)
    if cov is None:
        unmeasured = (False, "review-surface coverage was not measured for this review")
        return unmeasured, unmeasured
    examined = getattr(cov, "examined_rate", None)
    prose = getattr(cov, "prose_presented_fraction", None)
    surface = (
        bool(examined is not None and examined >= 1.0),
        (f"{getattr(cov, 'examined', 0)} of {getattr(cov, 'surface_size', 0)} addressable "
         f"unit(s) had a route pursued") if examined is not None else
        "the examined rate is undefined, so nothing establishes this",
    )
    prose_item = (
        bool(prose is not None and prose >= 1.0),
        (f"the lenses were shown {prose:.0%} of the extracted prose")
        if prose is not None else
        "the presented prose fraction is unmeasured, so nothing establishes this",
    )
    return surface, prose_item


def _check_artifact_executed(report, outcomes) -> tuple[bool, str]:
    ran = [o for o in outcomes if int(getattr(o, "launched", 0) or 0) > 0
           and admits(getattr(o, "provenance", ""))]
    if ran:
        return True, (f"{len(ran)} target(s) ran on a provenance the ceiling admits")
    launched = _launched(report, outcomes)
    return False, (f"{launched} process(es) started, none of them the authors' own "
                   f"commit-verified code")


def derive(report, target_set=None, *, lens_policy_enforced: bool = False
           ) -> ReviewGuarantees:
    """The whole guarantee set for one review, read off artifacts.

    `lens_policy_enforced` is a parameter rather than a field lookup because the enforced
    tool policy lives on the lens sidecar (`audit/<lens>.drive.json`), which no
    `EvalReport` carries. It defaults False, which fails closed: an unrecorded policy is
    reported as unrecorded, never as enforced. Over the shipped corpus that is the true
    answer for all 28 lens files.
    """
    outcomes = list(getattr(target_set, "outcomes", None) or [])
    objects = list(getattr(target_set, "objects", None) or [])

    pointers = _check_pointers(report)
    prov = _check_provenance(report, outcomes)
    auth = _check_authorization(report, outcomes)
    infra = _check_infrastructure(report, outcomes)
    refusals = _check_refusals(report, outcomes)
    accounting = _check_accounting(report, outcomes, objects)
    caps = _check_severity_caps(report)
    decision = _check_decision(report, outcomes)
    downscale = _check_no_downscale(report, outcomes)
    grading = _check_grading(report)
    holistic = (getattr(report, "substantive_verdict", None) is not None,
                "a whole-submission read is on record" if
                getattr(report, "substantive_verdict", None) is not None else
                "no whole-submission read was produced")
    executed = _check_artifact_executed(report, outcomes)
    surface, prose = _check_coverage(report)
    policy = (bool(lens_policy_enforced),
              "the caller reports the lens tool policy as enforced" if lens_policy_enforced
              else "the lens files record no enforced tool policy")

    detail = {
        "EVERY_EVIDENCE_POINTER_RE_VERIFIED": pointers,
        "PROVENANCE_CEILING_HELD": prov,
        "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION": auth,
        "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED": infra,
        "EVERY_UNRESOLVED_STATE_NAMED": refusals,
        "ACCOUNTING_REPRODUCIBLE": accounting,
        "SEVERITY_ONLY_CAPPED": caps,
        "NO_MODEL_WROTE_THE_DECISION": decision,
        "NO_EXPERIMENT_DOWNSCALED": downscale,
        "SEVERITY_INDEPENDENTLY_GRADED": grading,
        "PANEL_ISOLATION_RECORDED": policy,
        "HOLISTIC_JUDGEMENT_PRESENT": holistic,
        "ARTIFACT_EXECUTED": executed,
        "SURFACE_FULLY_EXAMINED": surface,
        "PROSE_FULLY_PRESENTED": prose,
    }

    items = assess(**{p: detail[k][0] for k, p in _PARAM_FOR_KEY.items()})
    out: list[Guarantee] = []
    for g in items:
        key = getattr(g, "key", "")
        why = detail.get(key, (None, ""))[1]
        out.append(g.model_copy(update={
            # The artifact pointer FIRST and the per-review number after it, so a reader
            # who doubts an entry knows where to look before knowing what it said.
            "evidence": f"{ESTABLISHED_BY[key]} - {why}" if why else ESTABLISHED_BY[key]}))

    return ReviewGuarantees(
        paper_id=getattr(report, "paper_id", "") or "",
        guarantees=out,
        non_guarantees=[NOT_HELD[getattr(g, "key", "")] for g in out
                        if not g.holds and getattr(g, "key", "") not in PROCESS_GUARANTEES],
        unmet=[getattr(g, "key", "") for g in out
               if not g.holds and getattr(g, "key", "") in PROCESS_GUARANTEES],
    )


# key -> the `assess` parameter that carries it. Held as a table so `_self_check` can
# assert the two vocabularies match exactly: a key with no parameter would silently
# under-claim, and a parameter with no key would be dead.
_PARAM_FOR_KEY: dict[str, str] = {
    "EVERY_EVIDENCE_POINTER_RE_VERIFIED": "every_pointer_verified",
    "PROVENANCE_CEILING_HELD": "provenance_clean",
    "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION": "execution_authorized",
    "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED": "infrastructure_isolated",
    "EVERY_UNRESOLVED_STATE_NAMED": "every_refusal_named",
    "ACCOUNTING_REPRODUCIBLE": "accounting_traceable",
    "SEVERITY_ONLY_CAPPED": "severity_only_capped",
    "NO_MODEL_WROTE_THE_DECISION": "decision_is_the_table",
    "NO_EXPERIMENT_DOWNSCALED": "no_downscaled_experiment",
    "SEVERITY_INDEPENDENTLY_GRADED": "grading_complete",
    "PANEL_ISOLATION_RECORDED": "lens_policy_enforced",
    "HOLISTIC_JUDGEMENT_PRESENT": "holistic_judgement",
    "ARTIFACT_EXECUTED": "artifact_executed",
    "SURFACE_FULLY_EXAMINED": "surface_fully_examined",
    "PROSE_FULLY_PRESENTED": "prose_fully_presented",
}


# --------------------------------------------------------------------------- #
# RENDERING
# --------------------------------------------------------------------------- #
HEADING = "## What this review guarantees, and what it does not"


def render(g) -> list[str]:
    """The bounded markdown section, as lines.

    Two paragraphs and nothing else. Deliberately NOT a table: a reader who skims a table
    reads the left column, and the left column of this one would be a list of capitalised
    tokens that look like verdicts. The honest half gets the larger budget.

    `unmet` gets its own line and its own word - DEFECT - because a process guarantee that
    did not hold is a bug in this harness and a reader must not file it beside "grading was
    off". It is printed even though it counts toward nothing.
    """
    items = list(getattr(g, "guarantees", None) or [])
    held = [SHORT[k] for it in items
            if (k := getattr(it, "key", "")) in SHORT and it.holds]
    missing = [SHORT[k] for it in items
               if (k := getattr(it, "key", "")) in SHORT and not it.holds
               and k not in PROCESS_GUARANTEES]
    unmet = list(getattr(g, "unmet", None) or [])

    lines = [HEADING, ""]
    lines.append("Machine-enforced here: " + (_clauses(held, _MAX_HELD_CHARS) or "nothing")
                 + ".")
    lines.append("")
    lines.append("Not established here: "
                 + (_clauses(missing, _MAX_NOT_HELD_CHARS) or "nothing") + ".")
    if unmet:
        lines.append("")
        lines.append("DEFECT IN THIS HARNESS, not a finding about the paper: "
                     + _clauses([SHORT.get(k, k) for k in unmet], 240) + ".")
    lines.append("")

    # Bounded by construction. If the caps above were widened past the section cap, this
    # drops whole lines rather than letting the section grow - and says which.
    text = "\n".join(lines)
    if len(text) > _MAX_SECTION_CHARS:
        keep = [HEADING, "", lines[2], "",
                f"(the rest of this section exceeded {_MAX_SECTION_CHARS} characters and "
                f"is in the ledger)", ""]
        return keep
    return lines


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    from .artifacts import (EvalReport, Finding, GUARANTEE_KINDS, Guarantee, ProbeResult,
                            Reconciliation, TargetOutcome, TargetSet)

    # --- the vocabulary is closed, disjoint and fully tabulated -----------------------
    groups = (PROCESS_GUARANTEES, CONDITIONAL_PROPERTIES, SCIENTIFIC_NON_GUARANTEES)
    for i, a in enumerate(groups):
        assert len(set(a)) == len(a), "a group repeats a key"
        for b in groups[i + 1:]:
            assert not (set(a) & set(b)), set(a) & set(b)
    assert len(set(ALL_KEYS)) == len(ALL_KEYS) == 21, len(ALL_KEYS)
    for table, name in ((KIND, "KIND"), (STATEMENT, "STATEMENT"), (NOT_HELD, "NOT_HELD"),
                        (SHORT, "SHORT"), (ESTABLISHED_BY, "ESTABLISHED_BY")):
        assert set(table) == set(ALL_KEYS), (name, set(table) ^ set(ALL_KEYS))
        for k, v in table.items():
            assert v and v.strip(), f"{name}[{k}] is blank"
    assert set(KIND.values()) <= set(GUARANTEE_KINDS), set(KIND.values())
    # Every checkable key has a parameter and every parameter has a key. A key with no
    # parameter would be permanently False and would read as a system that does not
    # enforce something it does.
    assert set(_PARAM_FOR_KEY) == set(PROCESS_GUARANTEES) | set(CONDITIONAL_PROPERTIES)
    assert set(_PARAM_FOR_KEY.values()) == set(inspect.signature(assess).parameters)
    # A SCIENTIFIC key must NOT have a parameter: if it did, some caller could set it.
    for k in SCIENTIFIC_NON_GUARANTEES:
        assert k not in _PARAM_FOR_KEY, k

    # --- the signature is the guarantee: booleans only, keyword-only, default False ----
    sig = inspect.signature(assess)
    for name, p in sig.parameters.items():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert str(p.annotation) == "bool", (name, p.annotation)
        assert p.default is False, name
    # Checked per underscore-separated SEGMENT, the convention
    # `tests/test_reasoning_architecture.py` established: a substring test flags "n"
    # inside "every_pointer_verified" and would be noise instead of a guarantee.
    banned = {"count", "n", "seeds", "seed", "delta", "value", "values", "epsilon",
              "metric", "paper", "paper_id", "threshold", "score", "num", "size", "title",
              "id", "venue", "author", "authors"}
    segments = {s for name in sig.parameters for s in name.split("_")}
    assert not (segments & banned), segments & banned

    # --- no input satisfies a SCIENTIFIC non-guarantee, over the whole input space -----
    # 2**15 combinations of every boolean `assess` accepts. This is the assertion that
    # makes "the system makes no scientific guarantee" a property rather than a comment.
    names = list(sig.parameters)
    for mask in range(1 << len(names)):
        kwargs = {n: bool(mask >> i & 1) for i, n in enumerate(names)}
        items = assess(**kwargs)
        assert [getattr(g, "key", "") for g in items] == list(ALL_KEYS), "order drifted"
        for g in items:
            if getattr(g, "key", "") in SCIENTIFIC_NON_GUARANTEES:
                assert g.holds is False, getattr(g, "key", "")
                assert g.kind == "SCIENTIFIC"
        # and at least one entry never holds, so the honest half is never empty
        assert any(not g.holds for g in items)

    # --- all-True still leaves every scientific non-guarantee unheld -------------------
    every = assess(**{n: True for n in names})
    assert sum(1 for g in every if g.holds) == len(names)
    assert sum(1 for g in every if not g.holds) == len(SCIENTIFIC_NON_GUARANTEES)

    # --- the material-severity copy has not drifted from the table that decides ---------
    from .stages import report as report_stage
    assert set(_MATERIAL_SEVERITY) == set(report_stage.MATERIAL_SEVERITY), (
        "the copy of MATERIAL_SEVERITY here has drifted from stages.report")
    # nor has the infrastructure list wandered outside the failure vocabulary
    from .artifacts import FAILURE_CLASSES
    assert set(_INFRASTRUCTURE_FAILURE) <= set(FAILURE_CLASSES)
    for allowed in ("runtime_failure", "timeout", "none"):
        assert allowed not in _INFRASTRUCTURE_FAILURE, allowed

    # --- a clean review: every process guarantee holds, nothing is unmet ----------------
    good = Finding(finding_id="f-01", lens="overclaim", severity="MINOR", title="t",
                   statement="s", evidence_quote="q", evidence_ref="T1:r0:c0",
                   evidence_class="cell_verified")
    rep = EvalReport(paper_id="p", verdict="GREEN", claim_status="NOT_VERIFIED",
                     findings=[good], dropped_findings=2,
                     ledger_path="reports/p.ledger.json",
                     review_efficiency={"targets_discovered": 12})
    ts = TargetSet(paper_id="p", outcomes=[
        TargetOutcome(target_id="T1", disposition="AUTHORIZATION_BLOCKED",
                      reason="the execution gate was closed",
                      failure_class="execution_unauthorized"),
        TargetOutcome(target_id="T2", disposition="NOT_ATTEMPTED")])
    g = derive(rep, ts)
    assert isinstance(g, ReviewGuarantees) and g.paper_id == "p"
    assert not g.unmet, g.unmet
    assert len(g.guarantees) == len(ALL_KEYS)
    # the honest half is never empty, and every entry in it is a real sentence
    assert g.non_guarantees, "a review that claims nothing is missing is a broken review"
    for s in g.non_guarantees:
        assert s in set(NOT_HELD.values())
    # every SCIENTIFIC non-guarantee is present, on a review that went perfectly
    for k in SCIENTIFIC_NON_GUARANTEES:
        assert NOT_HELD[k] in g.non_guarantees, k
    # the evidence names the artifact BEFORE the number
    for it in g.guarantees:
        assert it.evidence.startswith(ESTABLISHED_BY[getattr(it, "key", "")])
    # vacuity is disclosed rather than dressed up
    auth = next(it for it in g.guarantees
                if getattr(it, "key", "") == "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION")
    assert auth.holds and "without being exercised" in auth.evidence

    # --- each process check is individually reachable as a failure ---------------------
    # A check that cannot fail is decoration, the same argument `selfaudit._self_check`
    # makes about its twelve items.
    unverified = rep.model_copy(update={
        "findings": [good.model_copy(update={"evidence_class": ""})]})
    assert derive(unverified, ts).unmet == ["EVERY_EVIDENCE_POINTER_RE_VERIFIED"]

    synth = rep.model_copy(update={"probe": ProbeResult(
        paper_id="p", reconciliation=Reconciliation(status="FAILED_REPRODUCTION",
                                                    provenance="synthesized"))})
    assert "PROVENANCE_CEILING_HELD" in derive(synth, ts).unmet

    unauth = rep.model_copy(update={"probe": ProbeResult(
        paper_id="p", executions=2, execution_log="runs/p/execution.jsonl")})
    assert "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION" in derive(unauth, ts).unmet

    convicted = TargetSet(paper_id="p", outcomes=[TargetOutcome(
        target_id="T1", disposition="FAILED_REPRODUCTION", provenance="repo_exec",
        failure_class="dependency_missing", launched=1, reason="a wheel was missing")])
    bad = derive(rep.model_copy(update={"verdict": "RED",
                                        "claim_status": "VERIFIED_FAILURE"}), convicted)
    assert "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED" in bad.unmet

    silent = TargetSet(paper_id="p", outcomes=[
        TargetOutcome(target_id="T1", disposition="RESOURCE_BLOCKED", reason="")])
    assert "EVERY_UNRESOLVED_STATE_NAMED" in derive(rep, silent).unmet

    assert "ACCOUNTING_REPRODUCIBLE" in derive(
        rep.model_copy(update={"ledger_path": ""}), ts).unmet

    promoted = rep.model_copy(update={"findings": [
        good.model_copy(update={"severity": "MINOR", "counted_severity": "FATAL"})]})
    assert "SEVERITY_ONLY_CAPPED" in derive(promoted, ts).unmet

    # RED with nothing behind it, and GREEN that does not project - both are defects
    assert "NO_MODEL_WROTE_THE_DECISION" in derive(
        rep.model_copy(update={"verdict": "RED", "claim_status": "VERIFIED_FAILURE"}),
        ts).unmet
    assert "NO_MODEL_WROTE_THE_DECISION" in derive(
        rep.model_copy(update={"verdict": "RED", "claim_status": "NOT_VERIFIED"}), ts).unmet

    shrunk = TargetSet(paper_id="p", outcomes=[TargetOutcome(
        target_id="T1", disposition="RESOURCE_BLOCKED", launched=1,
        reason="24 GiB demanded, 8 GiB present")])
    assert "NO_EXPERIMENT_DOWNSCALED" in derive(rep, shrunk).unmet

    # --- a conditional property NEVER reaches `unmet` -----------------------------------
    # Grading off, no whole-paper read, no execution and no coverage all hold on the
    # fixture above, and none of them is a harness defect.
    for k in CONDITIONAL_PROPERTIES:
        assert k not in g.unmet, k
        assert NOT_HELD[k] in g.non_guarantees, k
    assert set(g.unmet) <= set(PROCESS_GUARANTEES)

    # --- grading: a review with no serious candidate does not claim the property --------
    ungraded = next(it for it in g.guarantees
                    if getattr(it, "key", "") == "SEVERITY_INDEPENDENTLY_GRADED")
    assert not ungraded.holds and "no serious candidate" in ungraded.evidence
    covered = derive(rep.model_copy(update={
        "grade_coverage": {"candidates": 2, "graded": 2, "pending": 0}}), ts)
    assert NOT_HELD["SEVERITY_INDEPENDENTLY_GRADED"] not in covered.non_guarantees
    partly = derive(rep.model_copy(update={
        "grade_coverage": {"candidates": 3, "graded": 1}}), ts)
    assert NOT_HELD["SEVERITY_INDEPENDENTLY_GRADED"] in partly.non_guarantees

    # --- the rendered section --------------------------------------------------------- #
    text = "\n".join(render(g))
    assert text.startswith(HEADING)
    assert len(text) <= _MAX_SECTION_CHARS, len(text)
    assert "Machine-enforced here:" in text and "Not established here:" in text
    # the four things a 2026 referee would otherwise assume
    for clause in ("accuracy unmeasured", "novelty not assessed",
                   "nothing here says the paper is correct",
                   "not a substitute for a referee"):
        assert clause in text, clause
    # nothing in the section is a verdict, a colour or a severity
    for forbidden in ("RED", "GREEN", "YELLOW", "FATAL", "MAJOR", "VERIFIED_FAILURE"):
        assert forbidden not in text, forbidden
    # an unmet process guarantee is printed, and printed as a defect
    defect_text = "\n".join(render(derive(promoted, ts)))
    assert "DEFECT IN THIS HARNESS" in defect_text
    assert "not a finding about the paper" in defect_text

    # `_clauses` says what it dropped rather than dropping it silently
    trimmed = _clauses(["aaaa", "bbbb", "cccc", "dddd"], 12)
    assert trimmed.endswith("more, in the ledger)"), trimmed
    assert _clauses(["aaaa"], 400) == "aaaa"

    # --- the section cannot become the outcome ---------------------------------------- #
    # `derive` reads a report; nothing that decides reads `derive`. Parameter absence,
    # the same way `outcome.finding_state` proves execution cannot move a finding state.
    for fn in (report_stage.overall_verdict, report_stage.claim_status,
               report_stage.triage):
        params = set(inspect.signature(fn).parameters)
        for bad_param in ("guarantees", "guarantee", "non_guarantees", "unmet"):
            assert bad_param not in params, (fn.__name__, bad_param)

    print("harness.guarantees self-check ok")


if __name__ == "__main__":
    _self_check()
