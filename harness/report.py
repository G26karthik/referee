"""S4 — rank the findings, decide the verdict, and render both reports.

Consolidates the reference implementation's `harness/stages/report.py`, `outcome.py`,
`guarantees.py`, `coverage.py`, `docintegrity.py`, `selfaudit.py` and the ledger-assembly
half of `ledger.py` (tag `reference-implementation-2026-09-20`) into one file. Function
BODIES are unchanged; cross-references were repointed at `decide.py` / `locate.py` /
`schema.py` (the v4 replacements for `materiality`/`disposition`/`exhaustion`/`planner`/
`claims`/`artifacts`), duplicate vocabulary already declared in `schema.py` was removed
rather than re-declared, and narrative docstrings were cut to what a reader needs at the
call site — see `docs/INVARIANT_MAP.md` for the corpus incidents that shaped each rule.

Two hard rules carried over unchanged:

  1. NO LLM DECIDES THE VERDICT. Severity ordering and the binary RED/GREEN call are a
     lexicographic sort and a materiality table in plain Python.
  2. THE RENDERER COPIES, IT DOES NOT WRITE. Every render function here is pure over
     artifacts some earlier stage already produced.

**Scope, deliberately narrower than the old `run_report`.** `assemble_report` below takes
already-produced, already-verified, already-graded artifacts (a `PaperDoc`, a list of
`LensReport`, a `ProbeResult | None`, a `TargetSet | None`) and does everything from
`findings = rank(...)` onward — the part that is this stage's own job per CLAUDE.md's phase
table, and the part that is entirely pure. All file I/O and subprocess dispatch (reading
`paper/doc.json`, writing the four report files, calling `agent.run_verdict`) is
`pipeline.run_report_stage`'s job, not this module's — kept apart so this file stays a pure
function of artifacts a caller already produced, never a place a network or filesystem
call can smuggle a decision in unverified.

`python -m harness.report` runs the self-check.
"""
from __future__ import annotations

import bisect
import os
import re
from collections import Counter

from . import artifact_evidence, decide, locate, paper, taxonomy
from . import provenance as provenance_mod
from .audit import RANK
from .schema import (
    BLOCKED_DISPOSITIONS, CLAIM_STATUSES, CaseLedger, CodeAudit, CodeAuditFinding,
    CoverageReport, DiscoveredObject, DocumentObservation, EVIDENCE_CLASSES, EvalReport,
    EXECUTION_STATES, ExperimentalChain, FINDING_STATES, Finding, GUARANTEE_KINDS, Guarantee,
    INTEGRITY_ABOUT, INTEGRITY_CHECKS, LedgerEntry, LensReport, PaperDoc, PlanDecision,
    ProbeResult, QUESTION_STATES, REPRODUCTION_STATUSES, Reconciliation, ReadingRecord,
    RepoAcquisition, ReviewGuarantees, ReviewOutcome, ReviewQuestion, ReviewSelfAudit,
    ReviewSurface, SCOPE_STATES, SURFACE_KINDS, ScientificFinding, SelfAuditItem,
    SubstantiveVerdict, TARGET_DISPOSITIONS, TRIAGE_LEVELS, TargetOutcome, TargetSet,
)

# Nothing inside decide.py itself renders prose -- this is the one module that does, so
# the materiality-basis gloss table is referenced from its real home rather than
# duplicated. A second copy of a prose table is exactly the "driver/route duplication"
# shape this consolidation removes everywhere else.
_MATERIALITY_GLOSS: dict[str, str] = decide.BASIS_GLOSS


# =============================================================================
# PART 1 — DECISION: verdict, claim status, disposition, triage
#   (from stages/report.py)
# =============================================================================

# Severity -> rank, and the findings table's display order. NOTE ranks below MINOR and is
# not one of the keys any threshold sums over (`schema.SEVERITIES`).
_SEVERITY_RANK = {"FATAL": 2, "MAJOR": 1, "MINOR": 0, "NOTE": -1}

# Tiebreak only, applied AFTER severity and evidence strength: a lens that cites the paper
# against itself sorts above one that argues methodology, because an editor can check the
# first kind in seconds.
_LENS_RANK = {"overclaim": 3, "contradiction": 2, "confound": 1, "protocol": 0}

# THE MATERIALITY TABLE, stated as data (invariant 8, CLAUDE.md). RED iff a material
# failure was ESTABLISHED — a failed reproduction from an admissible provenance, or the
# paper's own arithmetic failing — never by accumulating model concerns. The EMPTY TUPLE
# is the invariant held by the type system: no severity, however asserted, may reach RED.
MATERIAL_SEVERITY: tuple[str, ...] = ()
CONCERN_SEVERITY = ("FATAL", "MAJOR")

SUPPORT_LANGUAGE = ("verified", "supported", "confirmed", "validated", "corroborated")
EVIDENCED_SUPPORT = ("VERIFIED_SUPPORT",)

ADMISSIBLE_REPRODUCTION_PROVENANCE = provenance_mod.ADMISSIBLE_REPRODUCTION_PROVENANCE
# Reader vocabulary for what actually ran. Fails closed: an unrecognised provenance reads
# as a diagnostic, never as author code. THE SAME OBJECT as `provenance.PROVENANCE_LABEL`.
PROVENANCE_LABEL = provenance_mod.PROVENANCE_LABEL

_CELL_REF = re.compile(r"^T\d+:r\d+:c\d+$")
# Pull the leading magnitude out of free text an LLM wrote ("+3-5% top-1", "0.8pp") and
# degrade to 0.0 on anything unparseable — never raises, never inverts, never sinks the run.
_MAGNITUDE = re.compile(r"[-+]?\d*\.?\d+")

MAX_TABLE_ROWS = 14
MAX_THREAT_BULLETS = 6
_QUOTE_CHARS = 240

# The two sentences a reader most often gets wrong, spelled out once so every report says
# them identically.
_DECISION_GLOSS = {
    "VERIFIED_FAILURE": "A material failure was **established**: the evidence below is strong "
                        "enough to reject the claim it addresses. This is not a count of concerns.",
    "VERIFIED_SUPPORT": "Something was **positively checked and held** — an executed metric "
                        "reconciled with a printed cell. That is support for the cell that was "
                        "tested, not for the paper as a whole.",
    "NOT_VERIFIED": "**Nothing was established in either direction** within the audited scope. "
                    "This is not a finding that the paper is correct, and it is not a finding "
                    "that it is wrong.",
}
# What positive evidence stands behind the decision — a GREEN can never appear without this
# row. Only VERIFIED_SUPPORT may use the word "verified" at all; see SUPPORT_LANGUAGE.
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
    "NOT_VERIFIED": "An experiment was attempted and settled nothing — an infrastructure or "
                    "capability limit never counts against the paper.",
    "NOT_ATTEMPTED": "No reproduction was attempted, so no experimental evidence stands "
                     "behind this decision either way.",
}


def support_is_evidenced(claim_status: str) -> bool:
    """May this report describe the paper as verified/supported/confirmed? True only when
    something was positively checked and held — absence of an established failure is not
    support, and this predicate is what keeps the two apart everywhere the report speaks
    about the paper as a whole."""
    return claim_status in EVIDENCED_SUPPORT


def unearned_support_language(text: str, claim_status: str) -> list[str]:
    """Every support word used in `text` that `claim_status` has not earned. Scoped to
    DECISION-LEVEL prose (the badge, the reason, the gloss, the Decision table) — legitimate
    elsewhere ("cell_verified", "confirmed findings" describe an evidence class or a finding
    bucket, not a claim the PAPER was verified). Empty when support is evidenced.

    Enforced only by `tests/test_guarantees.py` / `tests/test_reimplementation_path.py`
    reading RENDERED OUTPUT, not by the renderer calling it — a linter over the renderer
    beats a filter inside it, and that shape is preserved here unchanged.
    """
    if support_is_evidenced(claim_status):
        return []
    low = text.lower()
    for token in (*CLAIM_STATUSES, *REPRODUCTION_STATUSES):
        low = low.replace(token.lower(), " ")
    return [w for w in SUPPORT_LANGUAGE if re.search(rf"(?<!not ){w}", low)]


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
    """The severity `overall_verdict` actually counts for `f` — falls back to the lens's
    own `severity` when `counted_severity` is empty, which is what makes grading-off
    reproduce the pre-grading verdict byte for byte. `grading.RANK[counted_severity] <=
    RANK[severity]` always, so the fallback can never be a promotion in disguise."""
    return f.counted_severity or f.severity


def finding_key(f: Finding) -> tuple:
    """Lexicographic sort key, most-severe-first under `reverse=True`: COUNTED severity,
    then evidence strength (cell beats page beats nothing), then experiment-settleability,
    then the lens tiebreak, then the id so the sort is total."""
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
    """Model findings with paper-level rejection authority: intentionally NONE. Dead by
    construction — invariant 8 as a fact the type system enforces rather than a comment. A
    finding may propose a dependency and prioritize investigation; it may not establish its
    own scientific truth. Deterministic paper checks and admissible bound executions enter
    through `material_target_failure` instead."""
    return []


def material_concerns(findings: list[Finding]) -> list[Finding]:
    """Counted FATAL/MAJOR model concerns — prominent, and never rejection authority."""
    return [f for f in findings if counted(f) in CONCERN_SEVERITY]


def is_calibration(probe: ProbeResult | None) -> bool:
    """Did the thing that ran measure this MACHINE rather than the paper? The identical-
    arms noise-floor template's "measured delta" is this host's seed noise; nothing about
    any paper follows from it in either direction. Read from the runner's own flag, falling
    back to the verdict for a result written before that flag existed."""
    if probe is None:
        return False
    if probe.calibration is not None:
        return bool(probe.calibration)
    return probe.verdict == "calibration"


def material_target_failure(objects: list | None, outcomes: list | None,
                            reconciliation: Reconciliation | None = None
                            ) -> tuple[object | None, str]:
    """THE ONE materiality decision on the paper-level path. `(source, kind)`, `kind` is
    "target" | "reconciliation" | "" — `claim_status` and `overall_verdict` both call
    THIS and nothing else, which is what makes it impossible for them to disagree about
    whether a paper failed. Wraps `decide.material_target_failure` (Tier 1: did the
    target's own route establish a defect; Tier 2: is a CENTRAL claim established to depend
    on it) and joins the legacy single-reconciliation path the same way, through
    `Reconciliation.target_id` — a reconciliation carrying no joinable target establishes
    no materiality, the fail-closed answer required when a caller passes no objects.
    """
    hit = decide.material_target_failure(objects, outcomes)
    if hit is not None:
        return hit, "target"
    if (reconciliation is not None and reconciliation.status == "FAILED_REPRODUCTION"
            and reconciliation.provenance in ADMISSIBLE_REPRODUCTION_PROVENANCE
            and decide.is_material(decide.basis_for_target(
                getattr(reconciliation, "target_id", "") or "", objects) or "NONE")):
        return reconciliation, "reconciliation"
    return None, ""


def claim_status(findings: list[Finding], reconciliation: Reconciliation | None = None,
                 *, outcomes: list | None = None, objects: list | None = None,
                 probe: ProbeResult | None = None) -> tuple[str, str]:
    """The epistemic state underneath the colour. `objects` is the materiality context and
    is REQUIRED for any established target defect to reach VERIFIED_FAILURE — omitting it
    does not fall back to "any established failure convicts"; it legitimately yields
    NOT_VERIFIED, because a paper-level stop may not depend on whether a caller passed an
    optional argument.

      VERIFIED_FAILURE  a MATERIAL failure was established.
      VERIFIED_SUPPORT  something was positively checked and held (reconciled against a
                        printed cell).
      NOT_VERIFIED      nothing was established in either direction — the honest state for
                        most papers.

    INCONCLUSIVE is deliberately NOT a failure: a missing dataset, a units mismatch, an
    unparsed metric, a shut gate and an 8 GiB card facing a 24 GiB demand all land here.
    """
    if is_calibration(probe):
        return "NOT_VERIFIED", (
            "the only thing that ran was the identical-arms noise-floor calibration, which "
            "measures this machine and reconciles nothing about the paper")
    source, kind = material_target_failure(objects, outcomes, reconciliation)
    if kind == "reconciliation":
        return "VERIFIED_FAILURE", "a reproduction attempt failed against the printed cell"
    if kind == "target":
        if getattr(source, "disposition", "") == "PAPER_ARITHMETIC_CONTRADICTION":
            return "VERIFIED_FAILURE", (
                f"target {getattr(source, 'target_id', '?')}: the paper's own "
                f"printed composition does not evaluate to the total it states "
                f"({getattr(source, 'reason', '') or 'see the ledger'})")
        return "VERIFIED_FAILURE", (
            f"target {getattr(source, 'target_id', '?')} failed reproduction on "
            f"{getattr(source, 'provenance', '?')} provenance")
    if (reconciliation is not None and reconciliation.status == "RESOLVED_VERIFIED"
            # THE CEILING, IN THE ACQUITTING DIRECTION TOO (invariant 3 says "either
            # direction"). Safe today only because `local_exec.reconcile` refuses to emit
            # RESOLVED_VERIFIED on an inadmissible provenance — checked here as well so a
            # hand-edited artifact cannot unlock "verified"/"supported"/"confirmed".
            and provenance_mod.admits(reconciliation.provenance)):
        return "VERIFIED_SUPPORT", "an executed metric reconciled with the printed cell"
    return "NOT_VERIFIED", "no material failure established, and nothing positively reproduced"


def reproduction_status(probe: ProbeResult | None) -> str:
    """REPRODUCED | FAILED_REPRODUCTION | NOT_VERIFIED | NOT_ATTEMPTED, reported beside the
    verdict, never folded into it. NOT_ATTEMPTED and NOT_VERIFIED are kept apart: "no
    experiment was run" and "an experiment ran and settled nothing" are different facts."""
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
    """Internal token -> reader vocabulary, failing closed to SYNTHESIZED_DIAGNOSTIC."""
    return provenance_mod.label(provenance)


def overall_verdict(findings: list[Finding], reconciliation: Reconciliation | None = None,
                    *, outcomes: list | None = None, objects: list | None = None,
                    probe: ProbeResult | None = None) -> tuple[str, str]:
    """The binary paper-level decision, plus the rule that produced it. RED iff
    `claim_status` is VERIFIED_FAILURE — no count, no accumulation, no second mechanism.
    Reaches the SAME `material_target_failure` call `claim_status` makes, so the two cannot
    disagree about whether a paper failed."""
    if is_calibration(probe):
        return "GREEN", (
            "the only thing that ran was the identical-arms noise-floor calibration, "
            "which measures this machine and reconciles nothing about the paper")
    source, kind = material_target_failure(objects, outcomes, reconciliation)
    if reconciliation is not None and reconciliation.status == "FAILED_REPRODUCTION":
        if reconciliation.provenance not in ADMISSIBLE_REPRODUCTION_PROVENANCE:
            # The ceiling, held at the verdict gate too. Reaching here means an upstream
            # bug or an edited artifact; a program not entitled to reconcile a printed cell
            # does not get to convict a paper — reported as a harness defect INSTEAD of the
            # conviction, never alongside it.
            return "GREEN", (
                f"A reconciliation reported FAILED_REPRODUCTION with provenance "
                f"'{reconciliation.provenance or '(none)'}', which the provenance ceiling "
                f"does not admit. That status should have been unreachable, so it is "
                f"treated as a harness defect and NOT as evidence about the paper.")
        if kind != "reconciliation":
            # Tier 1 held; Tier 2 did not — an established defect that does not stop the
            # paper. Still printed under `## Established failures` and still in the ledger.
            return "GREEN", (
                f"A reproduction failed at "
                f"{reconciliation.target_id or reconciliation.table_ref or 'a target'} and "
                f"the failure is established, but this review did not establish that a "
                f"central scientific claim of the paper depends on it, so it does not "
                f"reject the paper. It is reported in full below.")
        where = f" at {reconciliation.table_ref}" if reconciliation.table_ref else ""
        # WHO ran decides what the failure means. Only `repo_exec` is the authors' own
        # checkout; attributing a `driver` script's failure to them would be the one
        # accusation this system must never make by accident.
        if reconciliation.provenance == "repo_exec":
            blame = ("The audited repository's own code does not reproduce the number it "
                     "prints, so the central claim does not stand on the evidence the "
                     "authors supplied.")
        elif reconciliation.provenance == "driver":
            blame = ("The program that ran was a human-written reproduction of the paper's "
                     "method, not the authors' checkout. Whether it is faithful is not "
                     "machine-checked, so read the script before relying on this verdict.")
        else:
            blame = (f"The program that ran was '{reconciliation.provenance}', which the "
                     f"provenance ceiling does not admit, so this status should not have "
                     f"been reachable — treat it as a harness defect.")
        return "RED", f"Failed code reproduction{where}: {reconciliation.reason} {blame}"

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


# A central target that was ATTEMPTED and settled nothing — the ONLY non-finding state that
# colours a paper (invariant 17, CLAUDE.md). Anything less specific measures this harness's
# own configuration rather than the paper.
_ATTEMPTED_AND_UNSETTLED = ("INCONCLUSIVE",)


def unresolved_central(objects: list | None, outcomes: list | None) -> list:
    """Central, addressable targets pursued ADMISSIBLY and settled nothing — pursued by
    something entitled to settle it, and not settled. Only the authors' own code, or a
    human-written reproduction the ceiling admits, can produce an inconclusive result that
    says something about the paper's checkability rather than about our probe."""
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
    Derived from `taxonomy.claim_was_checked` rather than a hand-written disposition set —
    a list like that drifts every time a route is added."""
    by_id = {getattr(o, "target_id", ""): o for o in (outcomes or [])}
    out = []
    for obj in (objects or []):
        if getattr(obj, "centrality", "") != "CENTRAL" or not getattr(obj, "harness_addressable", False):
            continue
        outcome = by_id.get(getattr(obj, "target_id", ""))
        if outcome is None or not taxonomy.claim_was_checked(
                getattr(outcome, "evidence_state", "")):
            out.append(obj)
    return out


def triage(findings: list[Finding], reconciliation: Reconciliation | None = None,
           *, outcomes: list | None = None, objects: list | None = None,
           probe: ProbeResult | None = None) -> tuple[str, str]:
    """RED | YELLOW | GREEN — the REVIEW-level routing decision. RED is exactly
    `claim_status == VERIFIED_FAILURE`. What this adds is a split inside the old GREEN:

        YELLOW  something needs your attention: a verified MAJOR concern, or a central
                claim this system could address and could not settle.
        GREEN   nothing needed attention within the audited scope — stated, because GREEN
                is not a certificate of correctness.

    Neither YELLOW nor GREEN is an accusation. Every input is harness-derived; a model
    cannot write any of the three.
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
    citation. Flags only; `harness.grading.derive` is where earning happens, and even a
    blinded second reviewer is still a model whose judgement (not its citation) stays
    unchecked."""
    return [f"[{counted(f)}] {f.finding_id or f.title[:40]} ({f.lens}) — evidence is "
            f"{f.evidence_class.replace('_', ' ')}, not a cited table cell"
            for f in findings
            if counted(f) in ("FATAL", "MAJOR") and f.evidence_class != "cell_verified"]


def questions(findings: list[Finding]) -> list[Finding]:
    """Findings that are QUESTIONS rather than defects, from either reader's answer. Held
    to NOTE by `grading.CANDIDATE_CAP`, so nothing here affects a threshold — but they are
    PRINTED as questions rather than folded in among the defects."""
    return [f for f in findings
            if f.finding_class == "OPEN_QUESTION"
            or (f.finding_class == "UNGRADED" and f.candidate_class == "OPEN_QUESTION")]


def grading_review(findings: list[Finding]) -> list[str]:
    """Every finding where grading actually changed what counts, naming the cap."""
    return [f"{f.finding_id or f.title[:40]} ({f.lens}): lens asserted {f.severity}, "
            f"counted as {counted(f)} — capped by `{f.binding_cap or 'unknown'}` "
            f"({f.finding_class})"
            for f in findings if f.counted_severity and f.counted_severity != f.severity]


def verdict_sensitivity(findings: list[Finding], reconciliation: Reconciliation | None = None,
                        *, outcomes: list | None = None, objects: list | None = None) -> str:
    """The identical materiality table, applied only to findings whose evidence is a
    verified table cell. Not a second opinion — a reproduction failure passes through
    unchanged, because it is arithmetic against a cell rather than a graded finding."""
    cell_backed = [f for f in findings if f.evidence_class == "cell_verified"]
    return overall_verdict(cell_backed, reconciliation, outcomes=outcomes, objects=objects)[0]


def verdict_if_lens_severity_only(findings: list[Finding],
                                  reconciliation: Reconciliation | None = None,
                                  *, outcomes: list | None = None,
                                  objects: list | None = None) -> str:
    """The verdict this paper would have received before independent grading existed — the
    identical table over the identical findings with `counted_severity` erased."""
    lens_only = [f.model_copy(update={"counted_severity": ""}) for f in findings]
    return overall_verdict(lens_only, reconciliation, outcomes=outcomes, objects=objects)[0]


def build_chain(findings: list[Finding], probe: ProbeResult | None) -> ExperimentalChain | None:
    """Link the reproduction attempt back to the finding it was about. Copies, never
    derives; the one thing computed is `broken_link`, the FIRST unestablished precondition —
    later links are not assessed once an earlier one fails, so listing them all would imply
    checks that never ran."""
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
    out = _kv_table(rows, ("link", "state"))
    if c.broken_link:
        out += ["", f"⛔ The chain breaks at **{c.broken_link}**. Nothing after that link was "
                    f"established, so no reproduction verdict follows — a limit of what "
                    f"could be proven here, not a finding about the paper."]
    elif c.reconciliation in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION"):
        out += ["", f"🟢 Every link established, and the reconciliation reached "
                    f"**{c.reconciliation}** — a real reproduction verdict."]
    else:
        # No broken link is not the same as a verdict: `reconcile` refuses for reasons that
        # break no chain link at all (a units mismatch, a zero noise band, an unparseable
        # cell). Complete-chain does not imply verdict.
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
        auth = p.authorization
        why = auth.detail if auth else p.reason
        return [f"⛔ **The repository was not executed** — this harness declined to run it "
                f"(`{auth.decision if auth else 'unauthorized'}`). {why} "
                f"Nothing about the paper follows from this."]
    if p.verdict == "failed":
        return [f"The reproduction probe did not produce usable measurements. {p.reason}"]
    if p.verdict == "degenerate":
        return [f"⚠️ The probe ran but its noise estimate is unusable. {p.reason}"]

    # A probe with no paper-specific script did not reproduce anything — it calibrated this
    # machine's seed noise. `calibration` is recorded by the runner; the fallback is only
    # for probe_results.json written before that field existed.
    calibration = (p.calibration if p.calibration is not None
                   else (p.verdict == "calibration" or p.claimed_delta is None))
    synthesized = p.provenance == "synthesized"
    if calibration:
        head = (f"⚪ **Hardware Noise-Floor Calibration** (σ = {p.measured_std:.4f}, "
                f"2σ = {p.noise_band:.4f}) — No paper-specific script evaluated.")
    elif synthesized:
        placebo = p.mechanism == "placebo"
        if placebo:
            # Inverted relative to a genuine mechanism probe: a placebo arm has no
            # hypothesis, so clearing the noise band is the CONCERNING result.
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
    if not calibration:
        out.append(f"| measured delta | {p.measured_delta:+.4f} |")
    out += [f"| seed noise (1σ) | {p.measured_std:.4f} |",
            f"| detectability band (2σ) | {p.noise_band:.4f} |"]
    for arm, st in p.arms.items():
        out.append(f"| {arm} mean (n={st.n}) | {st.mean:.4f} ± {st.std:.4f} |")
    if p.claimed_delta is not None:
        out.append(f"| **claimed delta** | **{p.claimed_delta:+.4f}** |")
    out += ["", p.reason]

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
            out += ["", f"**What authored this probe.** {p.rationale}", "",
                    f"⚠️ **This is a generic control, not a reproduction of the paper's "
                    f"mechanism.** `runs/{p.paper_id}/probe.py` is a paper-independent "
                    f"placebo control run whenever no mechanism-specific template matches — "
                    f"it was NOT derived from this paper's formulation. It is **not** "
                    f"evidence about any number printed in the paper's tables, nor about "
                    f"the paper's own claimed mechanism specifically." + (
                    " The reconciliation below is capped at INCONCLUSIVE for that reason."
                    if p.reconciliation else "")]
        else:
            out += ["", f"**What authored this probe.** {p.rationale}", "",
                    f"⚠️ **This is a reimplementation, not a reproduction.** `runs/{p.paper_id}/probe.py` "
                    f"was written by the harness from the paper's own published formulation and run at "
                    f"toy scale on synthetic data. It is **not** evidence about any number printed in "
                    f"the paper's tables — those were produced at a scale and on datasets this probe "
                    f"does not touch." + (" The reconciliation below is capped at INCONCLUSIVE for "
                                          "that reason." if p.reconciliation else "")]
    if calibration:
        out += ["", f"Read this as a detectability floor: on this hardware and metric, any claimed "
                    f"gain below {p.noise_band:.4f} could not be distinguished from run-to-run "
                    f"variation at {len(p.seeds_run)} seeds. It is a measurement of this machine, "
                    f"not of the paper."]
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


def _head(table: dict[str, str], status: str) -> str:
    """A status-keyed headline, falling back to the bare token bolded rather than KeyError."""
    return table.get(status, f"**{status}**")


def _repo_block(a: RepoAcquisition) -> list[str]:
    """Where the code came from. States plainly when the answer is 'we did not look'."""
    out = [_head(_REPO_HEAD, a.status), ""]
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
    out += _kv_table(rows)
    if a.reason:
        out += ["", a.reason]
    if a.reimplementation is not None:
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
    """What the checkout says it needs — observations with citations, never an accusation:
    none of this gates execution."""
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
           f"{c.files_scanned} Python file(s). **None of this gates execution.**", ""]
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
    """Static findings, each with a file, a line and the source line itself. Filtered by
    rule AUTHORITY (`artifact_evidence.RULE_AUTHORITY`), not by severity: only classes A
    and B reach a reviewer, and an unaudited rule defaults to invisible."""
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
                f"clean bill of health."] + suppressed
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
                   f"full set in `control/probe_results.json`._")
    out += suppressed
    if c.unparseable:
        out += ["", f"_{len(c.unparseable)} file(s) could not be parsed and were skipped._"]
    return out


def _reconciliation_block(r: Reconciliation) -> list[str]:
    """Executed number against the printed cell, with the arithmetic shown."""
    out = [_head(_RECONCILE_HEAD, r.status), "", "| quantity | value |", "|---|---|"]
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


def _kv_table(rows: list[tuple[str, str]], headers: tuple[str, str] = ("item", "value")) -> list[str]:
    return [f"| {headers[0]} | {headers[1]} |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]


def _scientific_block(r: EvalReport) -> list[str]:
    """The primary output, in the machine report's own dense table form. The reviewer
    report groups and caps these; here every one appears, because this file is the trace."""
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
    """EvalReport -> markdown, the complete machine trace. Pure: no LLM, no network."""
    ranked = rank(r.findings)
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
              "The lens itself considered these and a reasonable reading resolved them.", ""]
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
              f"are counted.", ""]
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
              "Reasoned independently rather than derived from the counts above. It moves "
              f"no colour: agreement with the deterministic verdict is `{r.verdict_agreement}`.",
              "", f"**{sv.verdict}**"
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
              f"counts — check these first:", ""]
        L += [f"- {u}" for u in ungraded]
        L += [""]
        if r.verdict_if_cell_backed_only and r.verdict_if_cell_backed_only != r.verdict:
            L += [f"**This verdict depends on them.** Counting only findings whose evidence is a "
                  f"verified table cell, the same materiality table yields "
                  f"**{r.verdict_if_cell_backed_only}** rather than **{r.verdict}**. The "
                  f"difference is carried entirely by grades the harness cannot check.", ""]
        elif r.verdict_if_cell_backed_only:
            L += [f"The verdict does not depend on them: counting only cell-verified findings, the "
                  f"same materiality table still yields **{r.verdict_if_cell_backed_only}**.", ""]

    # Blockquoted, not interpolated bare, so a stray '#' or '```' in the lens's own prose
    # cannot forge a heading or open an unclosed fence that swallows the rest of the report.
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
              "verdict** — it refuses to let this review call itself complete.", ""]
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


# =============================================================================
# PART 2 — the ONE-TO-TWO PAGE REVIEWER REPORT, bounded BY CONSTRUCTION (invariant 19)
# =============================================================================
# Twelve distinct caps, none collapsible into another: each bounds a DIFFERENT section, so
# a paper with issues in six categories still shows a few of each rather than emptying one
# budget into another silently.
_MAX_RED, _MAX_YELLOW, _MAX_HELD, _MAX_OPEN = 5, 4, 3, 3
_MAX_READING = 4
_MAX_PER_CATEGORY = 3
_MAX_FINDINGS_SHOWN = 8
_MAX_TRIGGERED = 3
_LINE = 240

# What the two reading routes (artifact inspection, now; literature/validation deleted
# 2026-09-20) ESTABLISHED — the only outcomes of theirs a referee is shown. An inspection
# that settled nothing, or a concern whose relation is still the reader's own reading, stay
# in the scope counts and the ledger; only an outcome that established something is news.
_READING_OUTCOMES = {
    "ARTIFACT_MISMATCH_ESTABLISHED":
        "the paper and the released code disagree, with the experiment identity "
        "established from a deterministic source. It does NOT establish that the reported "
        "number is wrong: which of the two configurations produced it is a question for "
        "execution.",
    "ARTIFACT_FACT_ESTABLISHED":
        "a bounded fact about the released code, matching the bounded question it was "
        "asked. About the CHECKOUT, not about the paper's result.",
    "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS":
        "both locations are real and what connects them is the auditor's reading. A "
        "question for a referee, not a demonstrated inconsistency.",
}

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
    zero and is not one: an empty surface has no denominator."""
    return "not computable" if value is None else f"{value:.0%}"


_TRIAGE_GLOSS = {
    "RED": "a material problem was established on evidence this system re-verified",
    "YELLOW": "needs a human reviewer's attention; nothing was established either way",
    "GREEN": "no material problem established within the scope that was actually checked",
}


def _targets_summary(outcomes: list | None) -> dict:
    """How many targets ended in each disposition. A paper has a SET of targets and they
    end differently; collapsing to whichever target happened to be primary is how a mixed
    result becomes a clean-looking one."""
    out: dict[str, int] = {}
    for o in (outcomes or []):
        d = getattr(o, "disposition", "") or "NOT_ATTEMPTED"
        out[d] = out.get(d, 0) + 1
    return out


def _short(s: str, n: int = _LINE) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


# The order a reviewer reads them in: self-contradiction, then overclaim, then design gaps,
# then artifact/specification failures, then everything still open.
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


def scientific_findings(report: EvalReport, target_set: TargetSet | None = None) -> list:
    """Join every kept finding to the question it raised and the target that pursued it —
    a PROJECTION, nothing more. Each field is copied from an artifact that already decided
    it, so the report and the ledger cannot disagree about what was found."""
    ts = target_set
    qs = list(getattr(ts, "questions", []) or [])
    objects = list(getattr(ts, "objects", []) or [])
    outcomes = {o.target_id: o for o in (getattr(ts, "outcomes", []) or [])}
    plans = {p.target_id: p for p in (getattr(ts, "plans", []) or [])}

    q_by_finding = {}
    for q in qs:
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


def coverage_numerators(target_set: TargetSet | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(addressed, examined) as plain ADDRESS STRINGS, for `measure`. Strings, never
    objects: `measure` must not be able to see the harness's own object list, because the
    defect it replaces (`targets_addressable / targets_discovered`) was the same list
    divided by itself."""
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
    """(artifact_state, review_path) — see `taxonomy.artifact_state`. `execution_provenance`
    alone reads SYNTHESIZED_DIAGNOSTIC for a paper with no code, a refused clone, a failed
    clone, and an unauthorized clone: four opposite facts under one token."""
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


# THE LENGTH GUARANTEE, ENFORCED. Sections are dropped from the LOWEST priority upward
# until the text fits, and the report SAYS which ones it dropped. Four sections are never
# droppable: a report that fits by discarding what it established is a different report.
_MAX_REVIEW_CHARS = 12000

_DROPPABLE_ORDER = (
    "## What held up",
    "## Experiments triggered",
    "## Central claims an experiment ran for and did not settle",
    "## Open questions for the reviewer",
    "## Central claims this review did not check",
)
_UNDROPPABLE = ("## Review outcome", "## Established failures", "## Scientific findings",
                "## Scope of this review",
                "## What this review guarantees, and what it does not")
assert not (set(_UNDROPPABLE) & set(_DROPPABLE_ORDER)), (
    "a load-bearing section was made droppable: "
    f"{sorted(set(_UNDROPPABLE) & set(_DROPPABLE_ORDER))}")


def _split_sections(text: str) -> list[tuple[str, str]]:
    """[(heading, body)], preamble under heading ''. Splits on `## ` at line start only,
    so a `##` inside a quoted finding cannot open a section."""
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
    """Drop whole sections, lowest priority first, until the text fits — and say so."""
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
        anchor = text.find("## Scientific findings")
        text = text[:anchor] + note + "\n" + text[anchor:] if anchor > 0 else text + note
    return text


def _reading_clause(cov: CoverageReport | None) -> str:
    """How much of the paper the readers were carried, and how it was traversed."""
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


def render_reviewer_report(report: EvalReport, target_set: TargetSet | None = None) -> str:
    """The one-to-two page report a human reviewer reads. NOT the evidence ledger.

    Everything here is selected from artifacts that already exist and capped by the
    constants above; nothing is re-judged. The primary output is a set of scientific
    findings, each with a resolution state — not a colour. The triage level is stated in
    `## Scope of this review`, where it belongs: a routing decision about this review, not
    a conclusion about the paper.
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

    # FIRST, and as four separate rows (`## Review outcome`), so the three readings a
    # reader used to be invited into — correct / failed / inconclusive — never arrive
    # before the four disjoint facts they were standing in for.
    outcome_block = report.outcome or derive_outcome(
        report, target_set, unchecked_central=len(unchecked))
    L += render_outcome(outcome_block)

    failed = [o for o in outcomes if getattr(o, "establishes_failure", False)]
    if failed:
        L.append("## Established failures")
        for o in failed[:_MAX_RED]:
            obj = next((x for x in objects if x.target_id == o.target_id), None)
            _basis = getattr(obj, "materiality_basis", "NONE") or "NONE"
            _material_line = (
                f"  - materiality: `{_basis}` — "
                f"{_MATERIALITY_GLOSS.get(_basis, 'a paper-owned dependency was established')}; "
                "therefore this failure rejects the paper."
                if decide.is_material(_basis) else
                "  - materiality: `NONE` — this review did not establish that a central "
                "scientific claim depends on this target, so the defect stands and the "
                "paper is NOT rejected on it. That is a limit of this harness's "
                "materiality model, not a judgement that the defect is unimportant.")
            if o.disposition == "PAPER_ARITHMETIC_CONTRADICTION":
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

    L.append("## Scientific findings")
    if groups:
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
        # max(0, ...) per category, NOT summed raw: an over-full category elsewhere must
        # not silently cancel against a category with room to spare.
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

    reading = [o for o in outcomes if o.disposition in _READING_OUTCOMES]
    if reading:
        L += ["", "## What reading the artifact established"]
        for o in reading[:_MAX_READING]:
            obj = next((x for x in objects if x.target_id == o.target_id), None)
            L += ["", f"- **{o.target_id}** ({o.disposition.replace('_', ' ').lower()}) — "
                      f"{_short(getattr(obj, 'claim_text', ''), 120)}",
                  f"  - {_short(o.reason, 200)}",
                  f"  - what this establishes: {_READING_OUTCOMES[o.disposition]}"]
        if len(reading) > _MAX_READING:
            L.append(f"- …and {len(reading) - _MAX_READING} more; see the ledger.")
        L += ["", "This route runs nothing and cannot reject a paper: an inconsistency "
                  "with released code is a real result and not a demonstration that a "
                  "reported number is wrong."]

    triggered = [o for o in outcomes if o.action in
                 ("AUTHOR_CODE_REPRODUCTION", "INDEPENDENT_RECONSTRUCTION",
                  "MECHANISM_TEST_ONLY")]
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
        # cut only at a sentence boundary — the disclaimer must never fall off a line that
        # opens with a large delta. PLANNED and RAN are two facts, kept apart, so a
        # synthesized diagnostic is never described in the grammatical position of a run
        # whose provenance is `INDEPENDENT_RECONSTRUCTION`.
        _ran = (f"ran: `{provenance_label(o.provenance)}`" if int(getattr(o, "launched", 0) or 0)
                else "nothing ran")
        L += ["", f"- **{o.target_id}** — planned: {o.action.replace('_', ' ').lower()} · "
                  f"{_ran}, ended `{o.disposition}`",
              f"  - question: {_short(getattr(obj, 'claim_text', ''), 140)}"]
    if len(triggered) > _MAX_TRIGGERED:
        L.append(f"- …and {len(triggered) - _MAX_TRIGGERED} further target(s) pursued; see "
                 f"`discovery/targets.json`.")

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
          f"- review path: **{art_path}** — {_ARTIFACT_GLOSS.get(art_state, art_state)}",
          f"- {len(report.lenses_run)} independent lens(es) read the paper: "
          f"{', '.join(report.lenses_run) or 'none'}"
          + _reading_clause(cov),
          f"- {report.dropped_findings} finding(s) were dropped because their evidence could "
          f"not be re-verified against the paper",
          f"- {fun.get('discovered', eff.get('targets_discovered', 0))} target(s) discovered, "
          f"{fun.get('checkable', eff.get('targets_addressable', 0))} structurally checkable, "
          f"{fun.get('warranting_experiment', 0)} judged to warrant an experiment, "
          f"{fun.get('launched', 0)} target(s) with execution launched, "
          f"{fun.get('resolved', 0)} settled a question about the paper",
          f"- what was executed: {report.execution_provenance or 'nothing'}"
          + (f" ({fun.get('launched', 0)} target(s) launched, "
             f"{eff.get('processes_launched', 0)} process(es) across them, "
             f"{fun.get('completed', 0)} reached a reconciliation)"
             if fun.get('launched', 0) else ""),
          "- how the targets ended: " + (", ".join(
              f"{v} {k.replace('_', ' ').lower()}"
              for k, v in sorted((report.targets_summary or {}).items()) if v)
              or "no target reached an outcome"),
          f"- triage for routing: **{triage_level}** — {_TRIAGE_GLOSS.get(triage_level, '')}",
          f"  {_short(report.triage_reason or report.verdict_reason, 400)}",
          (f"- review-surface coverage: an address was minted for "
           f"{cov.addressed}/{cov.surface_size} addressable unit(s) of the paper "
           f"({_rate(cov.addressed_rate)}), and a route was pursued for "
           f"{cov.examined} ({_rate(cov.examined_rate)}). Structural coverage, not issue "
           f"recall: whether the review found the issues that matter is "
           f"{cov.semantic_coverage.replace('_', ' ')}."
           if cov is not None and cov.surface_size else
           "- review-surface coverage: not computed for this review"),
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

    if report.guarantees is not None:
        L += render_guarantees(report.guarantees)
    return _bounded("\n".join(L))


# =============================================================================
# PART 3 — OUTCOME: the four disjoint-fold reader-facing rows
#   (from outcome.py; `derive`/`render` renamed `derive_outcome`/`render_outcome` to avoid
#   colliding with `guarantees.py`'s identically-named functions in this merged module)
# =============================================================================
# Four independent folds over DISJOINT inputs (invariant 24, CLAUDE.md — replaces a single
# colour plus a `## Reproduction status` section that read a verdict where none was meant):
#
#     tier 1  FINDING STATE     what this review established — reads claim_status + kept
#                               findings, NOTHING from tier 3
#     tier 2  QUESTION STATE    what became of the questions it raised
#     tier 3  EXECUTION STATE   what execution was attempted and what it produced
#     tier 4  SCOPE STATE       how much of the paper this is an assessment of
#
# `finding_state`'s signature is the guarantee: no execution outcome can move it, because
# the parameter to move it with does not exist. `_self_check_outcome` sweeps every
# execution state against every finding state to prove it.

EXECUTION_ABOUT_THE_PAPER = ("EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY",
                             "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY")

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
# WHICH CODE RAN, in the reader's words, keyed on the provenance that ran it — the states
# below say something about the paper, and what they may say depends entirely on whose
# program produced the number. An INDEPENDENT REIMPLEMENTATION that disagreed with the
# paper used to print "the authors' own code ran and did not produce..." unconditionally;
# the sentence now names the actor.
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
DISCLAIMER = (
    "No row above says the paper is correct, that it failed, or that it is inconclusive. "
    "They say what was established, what was settled, what execution produced, and how "
    "much was looked at.")

# The exact reason must survive to the reader, but a report is bounded (invariant 19), so
# it is cut at a SENTENCE boundary — never mid-clause. The old renderer cut at 200
# characters flat and once took the words "...is evidence about the mechanism, not a
# reproduction..." off the end of a line that opened with a large delta.
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
    """Tier 1. Reads `claim_status` and a count; reads NOTHING about execution. An
    execution state is deliberately not a parameter — the same discipline
    `grading.derive` uses to make "a CONFOUND is always MAJOR" inexpressible."""
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
    """Whose program produced the evidence the execution row describes. Reads provenance
    and nothing else. When admissible outcomes disagree about whose code ran, names both
    rather than picking one."""
    seen = []
    for o in (outcomes or []):
        p = getattr(o, "provenance", "")
        if provenance_mod.admits(p) and p not in seen:
            seen.append(p)
    if not seen:
        return _DEFAULT_ACTOR
    if len(seen) == 1:
        return EXECUTION_ACTOR.get(seen[0], _DEFAULT_ACTOR)
    return " and ".join(EXECUTION_ACTOR.get(p, _DEFAULT_ACTOR) for p in seen)


def execution_state(outcomes: list | None = None, plans: list | None = None) -> tuple[str, str]:
    """Tier 3: (state, exact reason). Ordered by how much each state ESTABLISHES, not by
    how bad it sounds. `establishes_failure`/`evidence_state` are the outcome's own derived
    properties, so the provenance ceiling arrives with them — a FAILED_REPRODUCTION on a
    provenance the ceiling does not admit already became INCONCLUSIVE_EXECUTION upstream
    and reaches here as "ran and produced nothing admissible"."""
    outs = list(outcomes or [])
    warranted = {getattr(p, "target_id", "") for p in (plans or [])
                 if getattr(p, "requires_execution", False)}

    # THIS ROW IS ABOUT EXECUTION: only an outcome whose evidence CAME from an execution
    # may fill it. `establishes_failure` is broader — a PAPER_ARITHMETIC_CONTRADICTION
    # establishes a defect with nothing launched at all — so the filter here is the
    # reproduction ceiling itself, not a second list of dispositions.
    executed = [o for o in outs if provenance_mod.admits(getattr(o, "provenance", ""))]

    failed = [o for o in executed if getattr(o, "establishes_failure", False)]
    reproduced = [o for o in executed
                  if getattr(o, "evidence_state", "") == "REPRODUCTION_SUCCESS"]
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

    ran = [o for o in outs if int(getattr(o, "launched", 0) or 0) > 0]
    if ran:
        return "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE", (
            f"target {getattr(ran[0], 'target_id', '?')}: "
            f"{getattr(ran[0], 'reason', '') or 'no reason recorded'}")

    blocked = [o for o in outs
               if getattr(o, "target_id", "") in warranted
               and getattr(o, "disposition", "") in BLOCKED_DISPOSITIONS]
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
    """Tier 4. The limitation wins when there is one — the fact a reader most needs, and
    the easiest one for a report to let disappear because it changes no decision."""
    if unchecked_central > 0:
        return "CENTRAL_CLAIMS_LEFT_UNCHECKED"
    return "SOME_TARGETS_PURSUED" if pursued > 0 else "NO_TARGET_PURSUED"


def derive_outcome(report: EvalReport, target_set: TargetSet | None = None, *,
                   unchecked_central: int = 0) -> ReviewOutcome:
    """The whole four-tier outcome for one review. A pure projection: every input is a
    field some earlier stage already decided; this function's only authority is the
    English sentence that names it."""
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


def render_outcome(o: ReviewOutcome) -> list[str]:
    """The `## Review outcome` block: four labelled rows and one disclaimer. A list, not a
    table — the execution row must hold an exact reason, and a markdown table with a
    400-character cell is less readable than four lines."""
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


# =============================================================================
# PART 4 — GUARANTEES: what a machine enforced in THIS review, and what it never promises
#   (from guarantees.py; `derive`/`render` renamed `derive_guarantees`/`render_guarantees`)
# =============================================================================
# Three groups, because one list teaches a reader to ignore all of it (invariant 28,
# CLAUDE.md): PROCESS_GUARANTEES (enforced unconditionally; `holds=False` is a HARNESS
# DEFECT, the only thing reaching `unmet`), CONDITIONAL_PROPERTIES (true only when a gate
# was open or a surface reachable — off is a configuration, never a defect), and
# SCIENTIFIC_NON_GUARANTEES (properties this system cannot acquire by running better;
# `holds` is False on every input, by an EMPTY membership tuple rather than a comment).
# Every entry carries `holds` established from a harness-written field plus `evidence`
# naming it — a guarantee nobody checks per review is a marketing claim with a citation.

PROCESS_GUARANTEES: tuple[str, ...] = (
    "EVERY_EVIDENCE_POINTER_RE_VERIFIED", "PROVENANCE_CEILING_HELD",
    "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION", "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED",
    "EVERY_UNRESOLVED_STATE_NAMED", "ACCOUNTING_REPRODUCIBLE", "SEVERITY_ONLY_CAPPED",
    "NO_MODEL_WROTE_THE_DECISION", "NO_EXPERIMENT_DOWNSCALED",
)
CONDITIONAL_PROPERTIES: tuple[str, ...] = (
    "SEVERITY_INDEPENDENTLY_GRADED", "PANEL_ISOLATION_RECORDED", "HOLISTIC_JUDGEMENT_PRESENT",
    "ARTIFACT_EXECUTED", "SURFACE_FULLY_EXAMINED", "PROSE_FULLY_PRESENTED",
)
SCIENTIFIC_NON_GUARANTEES: tuple[str, ...] = (
    "COMPLETE_ISSUE_RECALL", "REFEREE_ACCURACY_MEASURED", "PAPER_CORRECTNESS",
    "NOVELTY_ASSESSED", "EVERY_IMPORTANT_EXPERIMENT_EXECUTABLE", "HUMAN_REFEREE_SUBSTITUTE",
)
ALL_KEYS: tuple[str, ...] = (
    PROCESS_GUARANTEES + CONDITIONAL_PROPERTIES + SCIENTIFIC_NON_GUARANTEES)

KIND: dict[str, str] = {
    **{k: "PROCESS" for k in PROCESS_GUARANTEES},
    **{k: "PROCESS" for k in CONDITIONAL_PROPERTIES},
    **{k: "SCIENTIFIC" for k in SCIENTIFIC_NON_GUARANTEES},
}

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

# The compact clause for the rendered section — a bounded markdown section (invariant 19)
# cannot carry twenty-one long-form sentences, so each key owns one short clause here.
SHORT: dict[str, str] = {
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

# WHICH FIELD ON WHICH ARTIFACT settles `holds` — a table rather than an f-string at the
# call site, so an entry with no artifact behind it cannot be added without `_self_check_
# guarantees` sweeping this map against `ALL_KEYS` and refusing a blank.
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
    "NOVELTY_ASSESSED":
        "no artifact: this system has no route to prior-art search",
    "EVERY_IMPORTANT_EXPERIMENT_EXECUTABLE":
        "no artifact: 'important' is not a property this system may assign",
    "HUMAN_REFEREE_SUBSTITUTE":
        "no artifact: a scope statement about this system, not a measurement",
}

# Failure classes that are facts about a host, an artifact or one of this harness's own
# gates. `runtime_failure`/`timeout` are DELIBERATELY ABSENT: invariant 7 admits exactly
# one conviction route (a crash after the experiment demonstrably started).
_INFRASTRUCTURE_FAILURE: tuple[str, ...] = (
    "environment_incompatible", "dependency_missing", "invalid_invocation",
    "startup_failure", "experiment_unidentified", "metric_unbound",
    "configuration_unmatched", "execution_unauthorized", "resources_insufficient",
    "commit_mismatch", "backend_unavailable", "credentials_unavailable",
)
_VERIFIED_EVIDENCE = ("cell_verified", "prose_verified", "caption_verified",
                      "equation_verified")

# The section is bounded BY CONSTRUCTION: twenty-one closed clauses do not fit in 900
# characters, so the cap is 1100 and the two halves have their own budgets, with the
# larger one going to what was NOT established (the honest half).
_MAX_SECTION_CHARS = 1100
_MAX_HELD_CHARS = 400
_MAX_NOT_HELD_CHARS = 620


def _clauses(items: list[str], budget: int) -> str:
    """Join short clauses with `·`, stopping at `budget` and saying how many were dropped —
    truncating silently would leave a reader unable to tell a system that guarantees six
    things from one whose seventh clause did not fit."""
    out: list[str] = []
    used = 0
    for i, clause in enumerate(items):
        cost = len(clause) + (3 if out else 0)
        if used + cost > budget and out:
            return " · ".join(out) + f" (+{len(items) - i} more, in the ledger)"
        out.append(clause)
        used += cost
    return " · ".join(out)


def assess(*, every_pointer_verified: bool = False, provenance_clean: bool = False,
           execution_authorized: bool = False, infrastructure_isolated: bool = False,
           every_refusal_named: bool = False, accounting_traceable: bool = False,
           severity_only_capped: bool = False, decision_is_the_table: bool = False,
           no_downscaled_experiment: bool = False, grading_complete: bool = False,
           lens_policy_enforced: bool = False, holistic_judgement: bool = False,
           artifact_executed: bool = False, surface_fully_examined: bool = False,
           prose_fully_presented: bool = False) -> tuple[Guarantee, ...]:
    """The derivation table: booleans in, one `Guarantee` per key of `ALL_KEYS`, in order.
    BOOLEANS ONLY, keyword-only, every default False, so "a paper with a CONFOUND gets a
    weaker guarantee" is inexpressible rather than merely absent — the same discipline
    `grading.derive` uses. No SCIENTIFIC key is a parameter, because none can be satisfied
    by any input (`_self_check_guarantees` sweeps the whole 2**15 input space)."""
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
    # `key` rides as an extra field (`schema._Base` is `extra="allow"`), since `Guarantee`
    # declares none, so a consumer can match an entry back to the vocabulary rather than to
    # a sentence that may be reworded.
    return tuple(
        Guarantee(key=k, kind=KIND[k], statement=STATEMENT[k],
                  holds=bool(held.get(k, False)), evidence=ESTABLISHED_BY[k])
        for k in ALL_KEYS)


# --- reading the artifacts: each `_check_*` returns (holds, the per-review detail) -------
def _reconciliations(report: EvalReport, outcomes: list) -> list:
    recs = []
    probe = getattr(report, "probe", None)
    if probe is not None and getattr(probe, "reconciliation", None) is not None:
        recs.append(probe.reconciliation)
    for o in outcomes:
        rec = getattr(o, "reconciliation", None)
        if rec is not None:
            recs.append(rec)
    return recs


def _launched(report: EvalReport, outcomes: list) -> int:
    """Processes this review actually started (invariant 21), without double-counting.
    `max` is right for both real shapes: a per-target run has both terms equal, and the
    legacy single-probe path has only the second."""
    probe = getattr(report, "probe", None)
    per_target = sum(int(getattr(o, "launched", 0) or 0) for o in outcomes)
    probe_runs = int(getattr(probe, "executions", 0) or 0) if probe is not None else 0
    return max(per_target, probe_runs)


def _check_pointers(report: EvalReport) -> tuple[bool, str]:
    """EVERY pointer: a cross-section concern carries a side citation of its own, each with
    its own harness-written evidence class, so checking only the primary citation would let
    such a finding satisfy a guarantee whose name promises all of them."""
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


def _check_provenance(report: EvalReport, outcomes: list) -> tuple[bool, str]:
    recs = _reconciliations(report, outcomes)
    settling = [r for r in recs
                if getattr(r, "status", "") in ("FAILED_REPRODUCTION", "RESOLVED_VERIFIED")]
    bad = [r for r in settling if not provenance_mod.admits(getattr(r, "provenance", ""))]
    if bad:
        return False, (f"{len(bad)} reconciliation(s) settled a printed quantity on an "
                       f"inadmissible provenance")
    if settling:
        return True, (f"{len(settling)} settling reconciliation(s), all on admitted "
                      f"provenance")
    return True, ("no reconciliation settled a printed quantity, so the ceiling was not "
                  "reached")


def _check_authorization(report: EvalReport, outcomes: list) -> tuple[bool, str]:
    """Fold EVERY execution record for this paper. Per-target outcomes are checked when
    they exist; the paper-level probe is the fallback only for the legacy shape that never
    split targets — reading only `report.probe.authorization` would hold this guarantee
    whenever THAT execution happened to be fine, even if a different target ran with no
    authorization at all."""
    launched = _launched(report, outcomes)
    if launched == 0:
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
        return False, f"{launched} process(es) started with no authorization record to check"
    bad = [tid for tid, ok in checks if ok is not True]
    if bad:
        return False, (f"{len(bad)} of {len(checks)} execution record(s) started with no "
                       f"authorization, or one that did not allow: {', '.join(bad[:4])}")
    return True, f"{len(checks)} execution record(s), every one authorized"


def _check_infrastructure(report: EvalReport, outcomes: list) -> tuple[bool, str]:
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


def _check_refusals(report: EvalReport, outcomes: list) -> tuple[bool, str]:
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


def _check_accounting(report: EvalReport, outcomes: list, objects: list | None = None
                      ) -> tuple[bool, str]:
    ledger = (getattr(report, "ledger_path", "") or "").strip()
    efficiency = dict(getattr(report, "review_efficiency", None) or {})
    probe = getattr(report, "probe", None)
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
    # A target that ESTABLISHED a defect with no object in the set it is being folded
    # over — never material by default, and a state inconsistency in this harness rather
    # than evidence about the paper.
    orphans = decide.unjoinable_established_failures(objects, outcomes)
    if orphans:
        missing.append(
            f"{len(orphans)} established failure(s) with no discovered object to assess "
            f"materiality against ({', '.join(orphans[:5])})")
    if missing:
        return False, "; ".join(missing)
    return True, (f"ledger at {ledger}, {len(efficiency)} accounting term(s)"
                  + (f", {logged} execution record(s) logged" if logged else ""))


def _check_severity_caps(report: EvalReport) -> tuple[bool, str]:
    fs = list(getattr(report, "findings", None) or [])
    raised = []
    capped = 0
    for f in fs:
        lens_sev = getattr(f, "severity", "") or ""
        counted_sev = getattr(f, "counted_severity", "") or ""
        if not counted_sev:
            continue
        if RANK.get(counted_sev, 0) > RANK.get(lens_sev, 0):
            raised.append(getattr(f, "finding_id", "") or "(unnamed)")
        elif RANK.get(counted_sev, 0) < RANK.get(lens_sev, 0):
            capped += 1
    if raised:
        return False, f"{len(raised)} finding(s) counted above the lens's own severity"
    return True, (f"{len(fs)} finding(s); {capped} capped below the lens's own severity, "
                  f"none above it")


def _check_decision(report: EvalReport, outcomes: list) -> tuple[bool, str]:
    verdict = (getattr(report, "verdict", "") or "").strip().upper()
    claim = (getattr(report, "claim_status", "") or "").strip().upper()
    established = claim == "VERIFIED_FAILURE"
    if (verdict == "RED") != established:
        return False, (f"verdict {verdict or '(none)'} does not project from claim status "
                       f"{claim or '(none)'}")
    if established:
        fatal = sum(1 for f in (getattr(report, "findings", None) or [])
                    if (getattr(f, "counted_severity", "") or getattr(f, "severity", ""))
                    in MATERIAL_SEVERITY)
        target = [getattr(o, "target_id", "?") for o in outcomes
                  if getattr(o, "establishes_failure", False)]
        recs = [r for r in _reconciliations(report, outcomes)
                if getattr(r, "status", "") == "FAILED_REPRODUCTION"
                and provenance_mod.admits(getattr(r, "provenance", ""))]
        if not (fatal or target or recs):
            return False, ("a material failure was established with no deterministic "
                           "failing target or admissible failed reproduction behind it")
        return True, (f"{verdict} from claim status {claim}: "
                      f"{len(target)} failing target(s), {len(recs)} admissible failed "
                      f"reconciliation(s)")
    return True, (f"{verdict or 'GREEN'} from claim status {claim or 'NOT_VERIFIED'}, "
                  f"with no material failure established")


def _check_no_downscale(report: EvalReport, outcomes: list) -> tuple[bool, str]:
    short = [o for o in outcomes if getattr(o, "disposition", "") == "RESOURCE_BLOCKED"
             or getattr(o, "failure_class", "") == "resources_insufficient"]
    ran = [getattr(o, "target_id", "?") for o in short
           if int(getattr(o, "launched", 0) or 0) > 0]
    if ran:
        return False, (f"target(s) {', '.join(ran[:4])} were short of resources and "
                       f"started a process anyway")
    return True, (f"{len(short)} target(s) refused for resources; none was run at a "
                  f"reduced size")


def _check_grading(report: EvalReport) -> tuple[bool, str]:
    cov = dict(getattr(report, "grade_coverage", None) or {})
    candidates = int(cov.get("candidates", 0) or 0)
    graded = int(cov.get("graded", 0) or 0)
    if candidates == 0:
        # NO SERIOUS CANDIDATE IS NOT A GRADED REVIEW: the property is that a second
        # reader weighed what the verdict counts, and nothing established that here either.
        return False, "no serious candidate was raised, so no independent grade exists"
    return graded >= candidates, f"{graded} of {candidates} serious candidate(s) graded"


def _check_coverage(report: EvalReport) -> tuple[tuple[bool, str], tuple[bool, str]]:
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


def _check_artifact_executed(report: EvalReport, outcomes: list) -> tuple[bool, str]:
    ran = [o for o in outcomes if int(getattr(o, "launched", 0) or 0) > 0
           and provenance_mod.admits(getattr(o, "provenance", ""))]
    if ran:
        return True, (f"{len(ran)} target(s) ran on a provenance the ceiling admits")
    launched = _launched(report, outcomes)
    return False, (f"{launched} process(es) started, none of them the authors' own "
                   f"commit-verified code")


# key -> the `assess` parameter that carries it — a table so `_self_check_guarantees` can
# assert the two vocabularies match exactly.
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


def derive_guarantees(report: EvalReport, target_set: TargetSet | None = None, *,
                      lens_policy_enforced: bool = False) -> ReviewGuarantees:
    """The whole guarantee set for one review, read off artifacts. `lens_policy_enforced`
    is a parameter rather than a field lookup because the enforced tool policy lives on the
    lens sidecar, which no `EvalReport` carries — it defaults False, fail-closed."""
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
        "EVERY_EVIDENCE_POINTER_RE_VERIFIED": pointers, "PROVENANCE_CEILING_HELD": prov,
        "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION": auth,
        "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED": infra, "EVERY_UNRESOLVED_STATE_NAMED": refusals,
        "ACCOUNTING_REPRODUCIBLE": accounting, "SEVERITY_ONLY_CAPPED": caps,
        "NO_MODEL_WROTE_THE_DECISION": decision, "NO_EXPERIMENT_DOWNSCALED": downscale,
        "SEVERITY_INDEPENDENTLY_GRADED": grading, "PANEL_ISOLATION_RECORDED": policy,
        "HOLISTIC_JUDGEMENT_PRESENT": holistic, "ARTIFACT_EXECUTED": executed,
        "SURFACE_FULLY_EXAMINED": surface, "PROSE_FULLY_PRESENTED": prose,
    }

    items = assess(**{p: detail[k][0] for k, p in _PARAM_FOR_KEY.items()})
    out: list[Guarantee] = []
    for g in items:
        key = getattr(g, "key", "")
        why = detail.get(key, (None, ""))[1]
        out.append(g.model_copy(update={
            "evidence": f"{ESTABLISHED_BY[key]} - {why}" if why else ESTABLISHED_BY[key]}))

    return ReviewGuarantees(
        paper_id=getattr(report, "paper_id", "") or "",
        guarantees=out,
        non_guarantees=[NOT_HELD[getattr(g, "key", "")] for g in out
                        if not g.holds and getattr(g, "key", "") not in PROCESS_GUARANTEES],
        unmet=[getattr(g, "key", "") for g in out
               if not g.holds and getattr(g, "key", "") in PROCESS_GUARANTEES],
    )


HEADING = "## What this review guarantees, and what it does not"


def render_guarantees(g: ReviewGuarantees) -> list[str]:
    """The bounded markdown section, as lines. Two paragraphs, deliberately NOT a table —
    a reader who skims a table reads the left column, and this one's left column would be
    capitalised tokens that look like verdicts. `unmet` gets its own word, DEFECT, because
    a process guarantee that did not hold is a bug in this harness."""
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

    text = "\n".join(lines)
    if len(text) > _MAX_SECTION_CHARS:
        keep = [HEADING, "", lines[2], "",
                f"(the rest of this section exceeded {_MAX_SECTION_CHARS} characters and "
                f"is in the ledger)", ""]
        return keep
    return lines


# =============================================================================
# PART 5 — COVERAGE: how much of THE PAPER this review could address and examined
#   (from coverage.py; `claims.*` repointed at `locate.*`, the v4 replacement)
# =============================================================================
# Replaces `targets_addressable / targets_discovered` (invariant 27, CLAUDE.md — that rate
# divides the harness's own object list by itself and RISES when extraction fails; it read
# 0.93 corpus-wide). The denominator is the paper's own addressable surface, enumerated from
# a `PaperDoc` and NOTHING the review produced: `surface(doc)` takes a `PaperDoc` and
# nothing else, `measure(...)` takes its numerators as plain address STRINGS so a numerator
# cannot reach the denominator's construction. Two numerators, never one: `addressed` (an
# address was minted) and `examined` (a route was pursued) are different claims.

_BUDGET_ENV = "SH_AUDIT_BUDGET_CHARS"
_BUDGET_DEFAULT = 70000
# `paper.render_sections`' own floor: no section is allotted less than this.
_MIN_PER_SECTION = 400

# The address grammars, exactly as `locate.resolve` re-derives them.
_CELL = re.compile(r"^T(\d+):r(\d+):c(\d+)$")
_FIG = re.compile(r"^F(\d+)$")
_EQ = re.compile(r"^E(\d+)$")
_SEC = re.compile(r"^S(\d+)$")
_PROSE = re.compile(r"^P(\d+):(\d+)-(\d+)$")

# `locate.mint` refuses a quote shorter than this; a shorter one addresses nothing unique.
_QUOTE_MIN = 8


def budget_chars() -> int:
    """The lens prompt's section budget, read at call time — a module-level constant
    would report the budget of whichever process imported first."""
    raw = (os.environ.get(_BUDGET_ENV) or "").strip()
    try:
        return int(raw) if raw else _BUDGET_DEFAULT
    except ValueError:
        return _BUDGET_DEFAULT


def kind_of(address: str = "") -> str:
    """Which `SURFACE_KINDS` unit a SURFACE address names, or '' for none."""
    a = (address or "").strip()
    if _CELL.match(a):
        return "table_cell"
    if _FIG.match(a):
        return "figure"
    if _EQ.match(a):
        return "equation"
    if _SEC.match(a):
        return "section_span"
    if _PROSE.match(a):
        return "reported_quantity"
    return ""


def prose_presented(sections: list, budget: int) -> tuple[int, int, int]:
    """(presented, total, sections_truncated) UNDER THE OLD TRUNCATING RENDERER. Kept as
    the BASELINE that `prose_visible` is measured against — it is `paper.render_sections`,
    which divides one budget across every section and hard-slices each, the mechanism
    `harness.reading` replaced."""
    from .paper import section_presentation
    p = section_presentation(list(sections or []), int(budget))
    return p.presented_chars, p.total_chars, p.sections_truncated


def prose_visible(doc: PaperDoc, budget: int) -> tuple[int, int, int]:
    """(visible, total, parts) — what a reader was ACTUALLY carried, under the plan.
    Delegates to `reading.plan`, the same call the audit stage makes to build the prompts."""
    from .paper import plan as reading_plan
    cov = reading_plan(doc, int(budget)).coverage
    return cov.part_local_chars, cov.extracted_prose_chars, cov.parts


def _prose_quantity_addresses(doc: PaperDoc) -> list[str]:
    """The paper's printed quantities that are NOT already table cells, at their minted
    prose addresses. A quantity carrying a `table_ref` that resolves IS that cell — counted
    once — and a sentence occurring more than once addresses nothing."""
    units = locate.section_units(doc)
    out: list[str] = []
    for num in doc.reported_numbers:
        ref = (num.table_ref or "").strip()
        if ref and locate.resolve(doc, ref).resolved:
            continue
        flat_quote = locate.flatten(num.source_quote or "")[0]
        if len(flat_quote) < _QUOTE_MIN:
            continue
        hits: list[str] = []
        for section_idx, flat, _offsets, _original, _page in units:
            start = flat.find(flat_quote)
            while start != -1 and len(hits) < 2:
                hits.append(f"P{section_idx}:{start}-{start + len(flat_quote)}")
                start = flat.find(flat_quote, start + 1)
            if len(hits) > 1:
                break
        if len(hits) == 1:
            out.append(hits[0])
    return out


def surface(doc: PaperDoc) -> ReviewSurface:
    """The paper's addressable surface. A `PaperDoc` ONLY — no `TargetSet`, no
    `DiscoveredObject`, no count derived from a review may reach this function.

    Excluded, each for a stated reason: empty padding cells (never shown to a lens); table
    header cells (`Table.ref` indexes `rows`, so a header has no address); a figure with no
    caption / an equation with no recovered body (the address resolves but nothing is
    citable); the repository (`IMPLEMENTATION_CLAIM` carries `ref=None` and is a claim
    about an artifact, not a unit of the paper).
    """
    addresses: dict[str, str] = {}
    cells_total = 0
    for table in doc.tables:
        for r, row in enumerate(table.rows):
            for c, value in enumerate(row):
                cells_total += 1
                if (value or "").strip():
                    addresses.setdefault(table.ref(r, c), "table_cell")
    for fig in doc.figures:
        if (fig.caption or "").strip():
            addresses.setdefault(fig.ref(), "figure")
    for eq in doc.equations:
        if (eq.text or "").strip():
            addresses.setdefault(eq.ref(), "equation")
    sections = 0
    for sec in doc.sections:
        if (sec.text or "").strip():
            sections += 1
            addresses.setdefault(f"S{sec.section_idx}", "section_span")
    for ref in _prose_quantity_addresses(doc):
        addresses.setdefault(ref, "reported_quantity")

    by_kind = {k: 0 for k in SURFACE_KINDS}
    for kind in addresses.values():
        by_kind[kind] = by_kind.get(kind, 0) + 1

    presented, total, parts = prose_visible(doc, budget_chars())
    return ReviewSurface(
        paper_id=doc.paper_id, content_sha=doc.content_sha,
        addresses=list(addresses), by_kind=by_kind,
        table_cells_nonempty=by_kind.get("table_cell", 0), table_cells_total=cells_total,
        sections=sections, prose_chars_total=total, prose_chars_presented=presented,
        reading_parts=parts,
        surface_empty=not addresses,
    )


def _fold(address: str, units: set[str]) -> str:
    """The surface unit an address names, or '' when the surface has no such unit. Exact
    membership first, then a prose span onto the section that contains it: a finding minted
    at `P44:0-156` addressed section 44's unit; the finer unit wins when it exists."""
    a = (address or "").strip()
    if not a:
        return ""
    if a in units:
        return a
    m = _PROSE.match(a)
    if m:
        section = f"S{int(m.group(1))}"
        if section in units:
            return section
    return ""


def _fold_all(addresses: tuple[str, ...], units: set[str]) -> tuple[set[str], list[str]]:
    """(units named, addresses that name none). An empty address (the repository's
    `ref=None`) is neither: filing it as off-surface would report the repository as a
    defect in the paper's enumeration."""
    named: set[str] = set()
    off: list[str] = []
    for raw in addresses or ():
        if raw is not None and not isinstance(raw, str):
            raise TypeError(
                f"a coverage numerator is an address string, not {type(raw).__name__}: "
                "pass the minted address, never the object it came from")
        a = (raw or "").strip()
        if not a:
            continue
        unit = _fold(a, units)
        if unit:
            named.add(unit)
        elif a not in off:
            off.append(a)
    return named, off


def measure(surf: ReviewSurface, *, addressed: tuple[str, ...] = (),
            examined: tuple[str, ...] = (),
            reading: ReadingRecord | None = None) -> CoverageReport:
    """Two numerators over one denominator, from plain address STRINGS. `examined` is
    folded into `addressed` (a pursued unit was addressed by construction). An address the
    surface does not contain raises NEITHER numerator — it is named in `off_surface`."""
    units = [a for a in (surf.addresses or ()) if (a or "").strip()]
    unit_set = set(units)
    empty = bool(surf.surface_empty) or not unit_set

    named_addressed, off_a = _fold_all(tuple(addressed or ()), unit_set)
    named_examined, off_e = _fold_all(tuple(examined or ()), unit_set)
    named_addressed |= named_examined
    off = sorted(set(off_a) | set(off_e))

    size = len(unit_set)
    by_kind_addressed = {k: 0 for k in SURFACE_KINDS}
    for unit in named_addressed:
        kind = kind_of(unit)
        if kind:
            by_kind_addressed[kind] = by_kind_addressed.get(kind, 0) + 1

    fraction = (surf.prose_chars_presented / surf.prose_chars_total
                if surf.prose_chars_total else None)
    return CoverageReport(
        paper_id=surf.paper_id, content_sha=surf.content_sha, surface_size=size,
        addressed=len(named_addressed), examined=len(named_examined),
        off_surface=off,
        # None, never 1.0: "0 of 0" is unmeasurable.
        addressed_rate=None if empty else len(named_addressed) / size,
        examined_rate=None if empty else len(named_examined) / size,
        prose_presented_fraction=fraction,
        reading=reading,
        by_kind_addressed=by_kind_addressed,
    )


# =============================================================================
# PART 6 — DOCUMENT INTEGRITY: OBSERVATIONS about a parsed paper, never conclusions
#   (from docintegrity.py; `claims.*` repointed at `locate.*`)
# =============================================================================
# Prevents the defect in invariant 26 (CLAUDE.md): a naive "referenced but missing" check
# produced TWELVE false "missing table/equation" claims over the shipped corpus. So `about`
# is REQUIRED with no default: of the ten checks, eight can only fill it with EXTRACTION,
# one with PAPER, one (`PROSE_CELL_MISMATCH`) is not computed at all. `TABLE_ARITHMETIC` is
# the ONE check that may speak about the paper: operands and result are verbatim cells of
# one recovered row, re-derived operand by operand, refusing unless another row of the SAME
# table establishes what the column means. Report-only, on the model of `code_audit.py`:
# `observe` takes a `PaperDoc` and nothing else, and no decision function has a parameter
# that could receive a severity, confidence or scientific class from it.

INTEGRITY_NOT_ATTEMPTED: tuple[tuple[str, str], ...] = (
    ("figure content",
     "no figure's plotted values are read — `Figure` is an extracted CAPTION and this "
     "harness has no way to see the marks in the image."),
    ("equation re-assembly",
     "an equation that arrives as per-glyph fragments is left alone, for the same reason "
     "`pdf._equation_body` refuses to attach a number to whatever text precedes it: a "
     "wrongly assembled body would earn a false machine attestation."),
    ("caption/body agreement score",
     "no number is put on how well a caption matches a body — detecting a real "
     "mis-pairing needs page geometry this harness does not have, and a score computed "
     "without it would be a guess dressed as a measurement."),
)

# The only check whose output may be a statement about the document the authors published.
CLAIMABLE_ABOUT_THE_PAPER: tuple[str, ...] = ("TABLE_ARITHMETIC",)

# A member of the vocabulary this landing does not compute, and why: comparing a prose
# number against "the cell it means" needs knowing WHICH cell it means, and choosing one by
# position is the exact coincidence invariant 18 forbids on the execution path.
NOT_COMPUTED: tuple[str, ...] = ("PROSE_CELL_MISMATCH",)

# The confidence an INPUT must carry before a check may compute arithmetic over it.
_STRONG_INPUT: tuple[str, ...] = ("cell_verified",)

# Caps, named, so a pathological document cannot turn two bullets into two pages. What is
# dropped is SAID in the last observation kept for that check.
_MAX_PER_CHECK = 8
_MAX_OBSERVATIONS = 40

# `Table.label` is '' whenever the extractor could not pair a caption with a body, so the
# label is read from `label` FIRST and the caption prefix second.
_LABEL_PREFIX = re.compile(
    r"^\s*(?:table|tab\.|figure|fig\.?|equation|eq\.?)\s*([A-Za-z]?\.?\d+(?:\.\d+)*)",
    re.IGNORECASE)
_BARE_LABEL = re.compile(r"^\s*([A-Za-z]?\.?\d+(?:\.\d+)*)\s*$")
_INTEGER_LABEL = re.compile(r"^\d+$")

# Run against the ORIGINAL section text and mapped into flattened coordinates — the
# flattened form destroys word boundaries ("in Figure 1" -> "infigure1"), so `\b` and a
# negative lookbehind are load-bearing, not decoration.
_CITATION = re.compile(
    r"\b(Tables?|Tabs?\.?|Figs?\.?|Figures?|Eqs?\.?|Eqns?\.?|Equations?|Secs?\.?|"
    r"Sections?|Appendices|Appendix)\s*\(?\s*(\d+(?:\.\d+)*)", re.IGNORECASE)
_CITATION_KIND = {
    "table": "table", "tables": "table", "tab": "table", "tabs": "table",
    "fig": "figure", "figs": "figure", "figure": "figure", "figures": "figure",
    "eq": "equation", "eqs": "equation", "eqn": "equation", "eqns": "equation",
    "equation": "equation", "equations": "equation",
    "sec": "section", "secs": "section", "section": "section", "sections": "section",
    "appendix": "appendix", "appendices": "appendix",
}
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S")
_WINDOW_BEFORE, _WINDOW_AFTER = 40, 90

_AVERAGE_HEADER = re.compile(r"^(avg|avg\.|average|mean|overall|all)\b", re.IGNORECASE)
_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("­", "")).strip()


def _printed_label(label: str = "", caption: str = "") -> str:
    """The number the paper printed for an object, or '' when none was recovered."""
    for source in (label, caption):
        source = _norm(source)
        if not source:
            continue
        if m := _LABEL_PREFIX.match(source):
            return m.group(1)
        if m := _BARE_LABEL.match(source):
            return m.group(1)
    return ""


def _as_number(cell: str) -> float | None:
    """A cell's value, or None when the cell is not exactly one plain number."""
    text = _norm(cell).replace(",", "")
    return float(text) if _NUMBER.match(text) else None


def _tolerance(printed: str) -> float:
    """Half of the last place the paper actually printed."""
    text = _norm(printed).replace(",", "")
    decimals = len(text.split(".", 1)[1]) if "." in text else 0
    return 0.5 * (10.0 ** -decimals) + 1e-9


class _DocObject:
    """One recovered numbered object, with an address a reader can re-resolve."""

    __slots__ = ("kind", "idx", "label", "ref", "quote", "page", "caption")

    def __init__(self, kind: str, idx: int, label: str, ref: str, quote: str,
                 page: int, caption: str) -> None:
        self.kind, self.idx, self.label = kind, idx, label
        self.ref, self.quote, self.page, self.caption = ref, quote, page, caption


def _first_cell(doc: PaperDoc, table) -> tuple[str, str]:
    """(ref, quote) for the first non-empty cell of a table, or ('', ''). A table has no
    address of its own — only its cells do — so a table-level observation borrows one that
    actually round-trips through `locate.resolve`."""
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row):
            if not _norm(cell):
                continue
            ref = table.ref(r, c)
            if locate.resolve(doc, ref, cell).resolved:
                return ref, cell
    return "", ""


def doc_objects(doc: PaperDoc) -> tuple[_DocObject, ...]:
    """Every recovered table, figure and equation, in document order, with its label."""
    out: list[_DocObject] = []
    for table in doc.tables:
        ref, quote = _first_cell(doc, table)
        out.append(_DocObject("table", table.table_idx,
                              _printed_label(table.label, table.caption),
                              ref, quote, table.page, _norm(table.caption)))
    for fig in doc.figures:
        out.append(_DocObject("figure", fig.figure_idx,
                              _printed_label(fig.label, fig.caption),
                              fig.ref(), fig.caption, fig.page, _norm(fig.caption)))
    for eq in doc.equations:
        out.append(_DocObject("equation", eq.equation_idx, _printed_label(eq.number, ""),
                              eq.ref(), eq.text, eq.page, _norm(eq.text)))
    return tuple(out)


class _Citation:
    """One place the prose cites a numbered object, at a re-resolvable prose address."""

    __slots__ = ("kind", "number", "ref", "quote", "page", "caption_shaped")

    def __init__(self, kind: str, number: str, ref: str, quote: str, page: int,
                 caption_shaped: bool = False) -> None:
        self.kind, self.number, self.page = kind, number, page
        self.ref, self.quote = ref, quote
        # "Table 5:" is the object naming ITSELF, not the prose pointing at it — both
        # readings are kept and each check says which it uses.
        self.caption_shaped = caption_shaped


def citations(doc: PaperDoc) -> tuple[_Citation, ...]:
    """Where the prose cites a numbered object — WHAT IT CITES, never what exists.
    `doc.crossrefs` is preferred when the ingest stage populated it; the fallback scans the
    section text itself. Which one was used is not hidden: `citation_source` says."""
    if doc.crossrefs:
        out: list[_Citation] = []
        for cr in doc.crossrefs:
            kind = (cr.kind or "").strip().lower()
            if kind not in ("table", "figure", "equation", "section", "appendix"):
                continue
            got = locate.resolve(doc, cr.span, cr.quote) if cr.span else None
            ok = got is not None and got.resolved
            out.append(_Citation(kind, _norm(cr.number), cr.span if ok else "",
                                 got.quote if ok else "", cr.page,
                                 _caption_shaped(cr.quote or "", cr.number or "")))
        return tuple(out)

    body_end = doc.body_end_section_idx
    found: list[_Citation] = []
    for section_idx, flat, offsets, original, page in locate.section_units(doc):
        # Back matter cites nothing of the paper's own: a bibliography entry naming a
        # volume number is not a cross-reference to this paper's Table 12.
        if 0 <= body_end <= section_idx:
            continue
        for m in _CITATION.finditer(original):
            kind = _CITATION_KIND.get(m.group(1).lower().rstrip("."))
            if not kind:
                continue
            start = max(0, _flat_index(offsets, m.start()) - _WINDOW_BEFORE)
            end = min(len(flat), _flat_index(offsets, m.end()) + _WINDOW_AFTER)
            if start >= end:
                continue
            found.append(_Citation(kind, m.group(2), f"P{section_idx}:{start}-{end}",
                                   original[offsets[start]:offsets[end - 1] + 1], page,
                                   original[m.end():m.end() + 3].lstrip().startswith(":")))
    return tuple(found)


def _flat_index(offsets: list[int], original_index: int) -> int:
    """The flattened coordinate for an index into the original text (`locate.flatten`'s
    inverse, by bisection)."""
    return bisect.bisect_left(offsets, original_index)


def _caption_shaped(quote: str, number: str) -> bool:
    """Does this citing text name the object as its own caption ("Table 5: ...")?"""
    text = _norm(quote)
    m = re.search(rf"\b\w+\s*{re.escape(_norm(number))}\s*:", text) if number else None
    return bool(m)


def citation_source(doc: PaperDoc) -> str:
    """`crossrefs` when the ingest stage supplied them, `prose_scan` when this module did."""
    return "crossrefs" if doc.crossrefs else "prose_scan"


def evidence_floor(check: str = "", input_evidence_class: str = "") -> str:
    """The strongest thing an observation of `check` computed over this input may claim:
    PAPER, EXTRACTION, or NOT_INVESTIGATED (meaning DO NOT COMPUTE). Fails closed in both
    arguments — an unknown check claims nothing, and a paper-claiming check claims nothing
    over an input it cannot re-verify as a cell."""
    if (check or "").strip() not in INTEGRITY_CHECKS:
        return "NOT_INVESTIGATED"
    if check in NOT_COMPUTED:
        return "NOT_INVESTIGATED"
    if check not in CLAIMABLE_ABOUT_THE_PAPER:
        return "EXTRACTION"
    return "PAPER" if (input_evidence_class or "").strip() in _STRONG_INPUT \
        else "NOT_INVESTIGATED"


def determinations(doc: PaperDoc) -> dict[str, str]:
    """Per check, what THIS document permitted it to claim — every check, always present.
    An empty observation list means two opposite things without this map: "clean" and
    "this document does not carry what the check needs" — both answer NOT_INVESTIGATED."""
    return _determine(doc, doc_objects(doc), citations(doc))


def _determine(doc: PaperDoc, objs: tuple[_DocObject, ...],
               cites: tuple[_Citation, ...]) -> dict[str, str]:
    by_kind: dict[str, list[_DocObject]] = {"table": [], "figure": [], "equation": []}
    for o in objs:
        by_kind[o.kind].append(o)
    labelled = {k: [o for o in v if o.label] for k, v in by_kind.items()}
    referring_kinds = {c.kind for c in cites if not c.caption_shaped}
    cited_kinds = {c.kind for c in cites}
    headings = _numbered_headings(doc)

    out: dict[str, str] = {}
    for check in INTEGRITY_CHECKS:
        out[check] = evidence_floor(
            check, "cell_verified" if check in CLAIMABLE_ABOUT_THE_PAPER else "")
    if not any(labelled.values()) or not cites:
        out["CROSSREF_UNRESOLVED"] = "NOT_INVESTIGATED"
    if not referring_kinds or not any(labelled.values()):
        out["OBJECT_UNCITED"] = "NOT_INVESTIGATED"
    if sum(len(v) for v in labelled.values()) < 2:
        out["LABEL_DUPLICATED"] = "NOT_INVESTIGATED"
        out["CAPTION_LABEL_CONFLICT"] = "NOT_INVESTIGATED"
        out["LABEL_OUT_OF_ORDER"] = "NOT_INVESTIGATED"
    if not doc.tables:
        out["BODY_UNCAPTIONED"] = "NOT_INVESTIGATED"
    # Tables only: measured recovered equation label sets are riddled with gaps that are
    # gaps in extraction, and table caption LINES survive more completely than bodies.
    if len(_integer_labelled(labelled["table"])) < 2:
        out["NUMBERING_GAP"] = "NOT_INVESTIGATED"
    if not headings or "section" not in cited_kinds:
        out["SECTION_REF_UNRESOLVED"] = "NOT_INVESTIGATED"
    if not _established_average_columns(doc):
        out["TABLE_ARITHMETIC"] = "NOT_INVESTIGATED"
    for check in NOT_COMPUTED:
        out[check] = "NOT_INVESTIGATED"
    return out


def _numbered_headings(doc: PaperDoc) -> frozenset[str]:
    return frozenset(m.group(1) for s in doc.sections
                     if (m := _NUMBERED_HEADING.match(_norm(s.title))))


def _integer_labelled(objs: list[_DocObject]) -> list[_DocObject]:
    """Only labels that are a bare integer — an appendix label ("A.1") does not share a
    number line with the main sequence, so it plays no part in ordering or gap arithmetic."""
    return [o for o in objs if _INTEGER_LABEL.match(o.label)]


def _average_rows(table) -> list[tuple[int, int, list[float], float, str]]:
    """(row, col, operands, printed, printed_raw) for every checkable average cell."""
    header = [_norm(h) for h in table.header]
    out: list[tuple[int, int, list[float], float, str]] = []
    for col, head in enumerate(header):
        if col == 0 or not _AVERAGE_HEADER.match(head):
            continue
        for r, row in enumerate(table.rows):
            if col >= len(row):
                continue
            printed = _as_number(row[col])
            if printed is None:
                continue
            # Strictly left of the average column and right of column 0 (the row label).
            operands = [v for c in range(1, col) if (v := _as_number(row[c])) is not None]
            if len(operands) < 2:
                continue
            out.append((r, col, operands, printed, _norm(row[col])))
    return out


def _established_average_columns(doc: PaperDoc) -> dict[tuple[int, int], int]:
    """{(table_idx, col): operand_count} for columns a row of their own table PROVES. A
    column headed "Avg." might average a subset or a weighted mix, so its meaning is
    ESTABLISHED by a row whose plain mean reproduces the printed value, and only then may a
    different row over the SAME operand count be reported as not matching."""
    established: dict[tuple[int, int], int] = {}
    for table in doc.tables:
        for r, col, operands, printed, raw in _average_rows(table):
            mean = sum(operands) / len(operands)
            if abs(mean - printed) <= _tolerance(raw):
                established.setdefault((table.table_idx, col), len(operands))
    return established


def _obs(check: str, about: str, ref: str, quote: str, page: int,
         detail: str) -> DocumentObservation:
    return DocumentObservation(check=check, about=about, ref=ref, quote=quote,
                               page=page, detail=detail)


def observe(doc: PaperDoc) -> tuple[DocumentObservation, ...]:
    """Every document-integrity observation this parsed paper supports. A `PaperDoc` ONLY —
    no `TargetSet`, no `Finding`, no severity and no gate state may reach this function."""
    objs = doc_objects(doc)
    cites = citations(doc)
    allowed = _determine(doc, objs, cites)
    by_kind: dict[str, list[_DocObject]] = {"table": [], "figure": [], "equation": []}
    for o in objs:
        by_kind[o.kind].append(o)
    groups: dict[str, list[DocumentObservation]] = {c: [] for c in INTEGRITY_CHECKS}
    unresolved_refs: set[tuple[str, str]] = set()

    if allowed["CROSSREF_UNRESOLVED"] == "EXTRACTION":
        have = {(o.kind, o.label) for o in objs if o.label}
        # Gated PER KIND: a document with no equation label recovered and six equation
        # citations carries ONE fact, not six unresolved-reference bullets.
        kinds_labelled = {o.kind for o in objs if o.label}
        seen: set[tuple[str, str]] = set()
        for c in cites:
            key = (c.kind, c.number)
            if c.kind not in kinds_labelled or key in seen or key in have:
                continue
            seen.add(key)
            unresolved_refs.add(key)
            groups["CROSSREF_UNRESOLVED"].append(_obs(
                "CROSSREF_UNRESOLVED", "EXTRACTION", c.ref, c.quote, c.page,
                f"the prose cites {c.kind} {c.number}. Extraction recovered no {c.kind} "
                f"labelled {c.number}, so this review could not address it — a limit of "
                f"what this harness recovered, not an established absence."))

    if allowed["BODY_UNCAPTIONED"] == "EXTRACTION":
        # Tables only — a `Figure` object IS an extracted caption, so it cannot lack one.
        for table in doc.tables:
            if _norm(table.caption):
                continue
            ref, quote = _first_cell(doc, table)
            groups["BODY_UNCAPTIONED"].append(_obs(
                "BODY_UNCAPTIONED", "EXTRACTION", ref, quote, table.page,
                f"extraction recovered a table body on page {table.page} (T"
                f"{table.table_idx}) with no caption paired to it, so nothing names it "
                f"and no citation of it can be matched. `Table.label` is documented as "
                f"empty in exactly this case."))

    if allowed["OBJECT_UNCITED"] == "EXTRACTION":
        referring = tuple(c for c in cites if not c.caption_shaped)
        cited = {(c.kind, c.number) for c in referring}
        kinds_cited = {c.kind for c in referring}
        for o in objs:
            if not o.label or o.kind not in kinds_cited or (o.kind, o.label) in cited:
                continue
            groups["OBJECT_UNCITED"].append(_obs(
                "OBJECT_UNCITED", "EXTRACTION", o.ref, o.quote, o.page,
                f"extraction recovered {o.kind} {o.label} and found no sentence citing it "
                f"in the prose it recovered. Either nothing refers to it or the referring "
                f"sentence was lost; this record cannot tell which."))

    if "EXTRACTION" in (allowed["LABEL_DUPLICATED"], allowed["CAPTION_LABEL_CONFLICT"]):
        by_label: dict[tuple[str, str], list[_DocObject]] = {}
        for o in objs:
            if o.label:
                by_label.setdefault((o.kind, o.label), []).append(o)
        for (kind, label), members in by_label.items():
            if len(members) < 2:
                continue
            first = members[0]
            texts = {m.caption for m in members}
            others = ", ".join(m.ref or f"{kind} index {m.idx}" for m in members[1:])
            if len(texts) == 1 and allowed["LABEL_DUPLICATED"] == "EXTRACTION":
                groups["LABEL_DUPLICATED"].append(_obs(
                    "LABEL_DUPLICATED", "EXTRACTION", first.ref, first.quote, first.page,
                    f"extraction recovered {len(members)} {kind}s all labelled {label} with "
                    f"identical text, so one {kind} was almost certainly recovered more "
                    f"than once (also at {others})."))
            elif len(texts) > 1 and allowed["CAPTION_LABEL_CONFLICT"] == "EXTRACTION":
                groups["CAPTION_LABEL_CONFLICT"].append(_obs(
                    "CAPTION_LABEL_CONFLICT", "EXTRACTION", first.ref, first.quote,
                    first.page,
                    f"extraction paired the label {label} with {len(texts)} different "
                    f"{kind} captions (also at {others}), so at most one of those pairings "
                    f"is right and this record does not say which."))

    if allowed["LABEL_OUT_OF_ORDER"] == "EXTRACTION":
        for kind, members in by_kind.items():
            ordered = _integer_labelled(members)
            for prev, cur in zip(ordered, ordered[1:]):
                if int(cur.label) >= int(prev.label):
                    continue
                groups["LABEL_OUT_OF_ORDER"].append(_obs(
                    "LABEL_OUT_OF_ORDER", "EXTRACTION", cur.ref, cur.quote, cur.page,
                    f"extraction recovered {kind} {prev.label} before {kind} {cur.label}, "
                    f"so the recovered order of this document's {kind}s does not ascend "
                    f"and the labels cannot all be paired correctly."))

    if allowed["NUMBERING_GAP"] == "EXTRACTION":
        ordered = _integer_labelled(by_kind["table"])
        present = {int(o.label) for o in ordered}
        after = {int(o.label): o for o in reversed(ordered)}
        already = {int(num) for kind, num in unresolved_refs
                   if kind == "table" and _INTEGER_LABEL.match(num)}
        for missing in range(min(present) + 1, max(present)):
            if missing in present or missing in already:
                continue
            anchor = after.get(missing + 1) or ordered[-1]
            groups["NUMBERING_GAP"].append(_obs(
                "NUMBERING_GAP", "EXTRACTION", anchor.ref, anchor.quote, anchor.page,
                f"extraction recovered table labels between {min(present)} and "
                f"{max(present)} with no table labelled {missing} among them. A hole in "
                f"this record is not a hole in the numbering."))

    if allowed["SECTION_REF_UNRESOLVED"] == "EXTRACTION":
        headings = _numbered_headings(doc)
        seen_sec: set[str] = set()
        for c in cites:
            if c.kind != "section" or c.number in seen_sec or c.number in headings:
                continue
            seen_sec.add(c.number)
            groups["SECTION_REF_UNRESOLVED"].append(_obs(
                "SECTION_REF_UNRESOLVED", "EXTRACTION", c.ref, c.quote, c.page,
                f"the prose cites section {c.number}. Extraction recovered "
                f"{len(headings)} numbered heading(s) and none of them is {c.number}, so "
                f"this review could not follow the reference."))

    if allowed["TABLE_ARITHMETIC"] == "PAPER":
        established = _established_average_columns(doc)
        for table in doc.tables:
            for r, col, operands, printed, raw in _average_rows(table):
                want = established.get((table.table_idx, col))
                if want is None or want != len(operands):
                    continue
                mean = sum(operands) / len(operands)
                if abs(mean - printed) <= _tolerance(raw):
                    continue
                ref = table.ref(r, col)
                cell = table.cell(r, col)
                if not locate.resolve(doc, ref, cell).resolved:
                    continue
                shown = ", ".join(f"{v:g}" for v in operands)
                groups["TABLE_ARITHMETIC"].append(_obs(
                    "TABLE_ARITHMETIC", "PAPER", ref, cell, table.page,
                    f"the cell under the average column prints {raw}; the plain mean of "
                    f"the {len(operands)} numeric cells to its left ({shown}) is "
                    f"{mean:.6g}. Another row of this table reproduces its own average "
                    f"exactly, so the column does average across."))

    out: list[DocumentObservation] = []
    for check in INTEGRITY_CHECKS:
        kept = groups[check][:_MAX_PER_CHECK]
        dropped = len(groups[check]) - len(kept)
        if dropped and kept:
            kept[-1].detail += (f" (and {dropped} further {check} observation(s) not "
                                f"listed — the per-check cap is {_MAX_PER_CHECK}.)")
        out.extend(kept)
    if len(out) > _MAX_OBSERVATIONS:
        dropped = len(out) - _MAX_OBSERVATIONS
        out = out[:_MAX_OBSERVATIONS]
        out[-1].detail += (f" (and {dropped} further observation(s) not listed — the "
                           f"per-document cap is {_MAX_OBSERVATIONS}.)")
    return tuple(out)


def summarise(observations: tuple[DocumentObservation, ...]) -> dict[str, int]:
    """Counts per `about` — two keys rather than a total, because "3 about the paper and 6
    about our own extraction" and "9 observations" say different things and only the first
    is true."""
    return {about: sum(1 for o in observations if o.about == about)
            for about in INTEGRITY_ABOUT}


def scope_lines(observations: tuple[DocumentObservation, ...]) -> tuple[str, ...]:
    """The reviewer-facing bullets, minted HERE: "3 broken references" and "3 references
    this review could not follow in what it recovered" are the same number and opposite
    claims, so the wording lives in the module that knows which is true. Empty when there
    is nothing to report — an empty list means `determinations` is where to look."""
    counts = summarise(observations)
    if not observations:
        return ()
    per_check: dict[str, int] = {}
    for o in observations:
        per_check[o.check] = per_check.get(o.check, 0) + 1
    breakdown = ", ".join(f"{per_check[c]} {c.lower().replace('_', ' ')}"
                          for c in INTEGRITY_CHECKS if c in per_check)
    return (
        f"document integrity: {counts['PAPER']} observation(s) about the paper and "
        f"{counts['EXTRACTION']} about this review's own extraction ({breakdown}) — "
        f"the full list is in the machine report",
        "these are structural observations, not scientific findings; none of them counts "
        "toward any threshold and none of them moved the review's outcome",
    )


# =============================================================================
# PART 7 — SELF-AUDIT: did the review do the work it claims? (from selfaudit.py)
# =============================================================================
# Pure: artifacts in, verdict per item out — no model, no I/O. Every item is checked
# against a HARNESS-WRITTEN field, never a model's assessment of its own diligence. A
# failed item does NOT move the verdict; it refuses to let the review call itself complete.

# Numeric discrepancy types whose whole content IS an arithmetic claim.
_NUMERIC = ("ARITHMETIC_ERROR", "DIFFERENT_DENOMINATOR", "GENUINE_CONTRADICTION")
# What counts as "serious": what the verdict actually COUNTED, not what a lens asserted.
_SERIOUS = ("FATAL", "MAJOR")


def _finding_label(f: Finding) -> str:
    return f.finding_id or (f.title[:40] if f.title else "(unnamed)")


def _self_audit_item(key: str, question: str, offenders: list[str], total: int, detail: str,
                     applicable: bool = True) -> SelfAuditItem:
    if not applicable or total == 0:
        return SelfAuditItem(key=key, question=question, state="not_applicable",
                             detail="nothing in scope for this check")
    if offenders:
        return SelfAuditItem(key=key, question=question, state="fail", detail=detail,
                             offenders=offenders[:12], n_offenders=len(offenders),
                             n_in_scope=total)
    return SelfAuditItem(key=key, question=question, state="pass", n_in_scope=total,
                         detail=f"all {total} in scope")


def self_audit(report: EvalReport, counted_fn) -> ReviewSelfAudit:
    """The checklist over one finished `EvalReport`. `counted_fn` is passed in (rather than
    calling `counted` directly) to keep this function's dependency explicit and testable
    the way the reference implementation's cross-module wiring required; in this merged
    module it is simply `counted`."""
    fs = list(report.findings)
    serious = [f for f in fs if counted_fn(f) in _SERIOUS]
    numeric = [f for f in fs if f.discrepancy_type in _NUMERIC]
    graded = [f for f in serious if f.grade is not None]

    items = [
        _self_audit_item("serious_findings_verified",
              "Did I verify every serious finding against the paper?",
              [_finding_label(f) for f in serious if not f.verified_observation],
              len(serious),
              "a counted FATAL/MAJOR carries no harness-written verified_observation, so "
              "nothing machine-checked its evidence"),
        _self_audit_item("serious_findings_cell_backed",
              "Is every serious finding anchored in a checkable table cell?",
              [f"{_finding_label(f)} ({f.evidence_class})" for f in serious
               if f.evidence_class != "cell_verified"],
              len(serious),
              "a counted FATAL/MAJOR rests on prose, a caption or an equation rather than "
              "an addressed cell — checkable, but not in seconds"),
        _self_audit_item("numbers_independently_recomputed",
              "Did I independently recompute the numbers a numeric finding turns on?",
              [f"{_finding_label(f)} ({f.calc_class or 'no calculation'})" for f in numeric
               if f.calc_class != "recomputed_ok"],
              len(numeric),
              "a finding whose claim IS an arithmetic claim produced no calculation the "
              "harness could re-verify operand by operand"),
        _self_audit_item("alternative_interpretation_tested",
              "Did I test whether an alternative interpretation resolves the issue?",
              [f"{_finding_label(f)} ({f.verification_state or 'not assessed'})" for f in serious
               if f.verification_state != "complete"],
              len(serious),
              "the falsification/steelman triple on a counted FATAL/MAJOR is blank, "
              "degenerate, or predates the schema that requires it"),
        _self_audit_item("authors_steelmanned",
              "Did I construct the strongest defense of the authors?",
              [_finding_label(f) for f in serious if not f.steelman.strip()],
              len(serious),
              "a counted FATAL/MAJOR carries no steelman at all"),
        _self_audit_item("questions_separated_from_findings",
              "Did I separate reviewer questions from confirmed findings?",
              [_finding_label(f) for f in fs if not f.candidate_class and f.finding_class == "UNGRADED"],
              len(fs),
              "a finding was never sorted into CONFIRMED_FINDING / PLAUSIBLE_CONCERN / "
              "OPEN_QUESTION / DISMISSED by either reader, so it is counted as a defect "
              "without anyone having said it is one"),
        _self_audit_item("confidence_separate_from_severity",
              "Did I state confidence separately from severity?",
              [_finding_label(f) for f in serious if not f.confidence],
              len(serious),
              "a counted FATAL/MAJOR states no confidence, so how sure anyone is and how "
              "bad it is have collapsed into one number"),
        _self_audit_item("severity_argued_by_impact",
              "Did I argue severity by impact rather than by a checklist?",
              [_finding_label(f) for f in serious if not f.severity_rationale.strip()],
              len(serious),
              "a counted FATAL/MAJOR gives no reason for that grade over the one below it"),
        _self_audit_item("evidence_provenance_recorded",
              "Did I distinguish internal paper evidence from inference?",
              [_finding_label(f) for f in fs if not f.evidence_origin],
              len(fs),
              "a finding's evidence_ref names no shape the harness recognises, so what "
              "KIND of evidence it is was never established"),
        _self_audit_item("graded_by_a_second_reader",
              "Did a second, independent reader weigh every serious candidate?",
              [_finding_label(f) for f in serious if f.grade is None],
              len(serious),
              "a counted FATAL/MAJOR carries only the asserting lens's own judgement "
              "(grading off, out of scope, or it did not complete)"),
        _self_audit_item("previous_conclusions_not_inherited",
              "Did the second reader reach its answer from the paper, not from the first?",
              [_finding_label(f) for f in graded
               if f.grade is not None and not f.grade.reached_independently],
              len(graded),
              "the grader reports it worked from the first reader's argument rather than "
              "from the paper, so its agreement is not independent evidence"),
        _self_audit_item("whole_paper_judged_independently",
              "Did I judge the paper as a whole, separately from counting findings?",
              [] if report.substantive_verdict is not None else ["(no substantive read)"],
              1,
              "no whole-paper assessment was produced, so the only judgement on record is "
              "the threshold table's count"),
    ]
    failed = [i.key for i in items if i.state == "fail"]
    return ReviewSelfAudit(
        items=items, failed=failed, complete=not failed,
        summary=(f"{sum(1 for i in items if i.state == 'pass')} of "
                 f"{sum(1 for i in items if i.state != 'not_applicable')} applicable "
                 f"check(s) passed"
                 + (f"; unmet: {', '.join(failed)}" if failed else "")),
    )


# =============================================================================
# PART 8 — LEDGER: the traceable chain behind every conclusion, and its cost
#   (from the ledger-assembly half of ledger.py; `exhaustion`/`planner` repointed at
#   `decide.route_coverage` / `decide.current_plans`)
# =============================================================================
# A reviewer-facing report is one to two pages; the trace below is however long it needs to
# be so any line can be traced to an artifact. Every field is copied — nothing re-reasoned.

_LEDGER_ADMISSIBILITY = {
    "repo_exec": "the authors' own checkout at a verified commit — admissible in both "
                 "directions by the provenance ceiling",
    "driver": "a human-written reproduction of the paper's method — admissible, but its "
              "faithfulness is not machine-checked",
    "synthesized": "a mechanism reimplementation at toy scale — NOT admissible against a "
                   "printed quantity, in either direction",
    "template": "the identical-arms noise-floor template — measures this machine, not the "
                "paper, and reconciles nothing",
    "paper": "settled against the paper's own printed content; nothing was executed",
}

# What each disposition means for the paper. Only two of these are statements about the
# paper at all — invariants 4-7 kept visible in the trace, not only in the vocabulary.
_LEDGER_IMPLICATION = {
    "REPRODUCED": "the printed quantity was re-derived and agrees within the measured "
                  "noise band.",
    "FAILED_REPRODUCTION": "the executed program did not produce the printed quantity, "
                           "and the run reached the experiment before failing.",
    "PAPER_ONLY_RESOLVED": "settled from the paper itself; no execution was warranted.",
    "CITATION_VERIFIED_ONLY": "the concern's quotation was re-verified against the paper, "
                              "so it cites the paper accurately. Nothing further follows: "
                              "whether the concern is correct was not investigated.",
    "PAPER_ARITHMETIC_CONTRADICTION": "the paper's own printed composition was "
                              "deterministically recomputed and does not evaluate to the "
                              "total it states. A material failure, established from the "
                              "paper alone: no execution, no artifact, no model judgement.",
    "SPECIFICATION_BLOCKED": "the paper does not specify enough to check this. A limit of "
                             "the specification, not a defect in the result.",
    "ARTIFACT_BLOCKED": "no usable artifact exists for this target. Nothing about the "
                        "paper follows.",
    "ADDRESSING_BLOCKED": "this review could not build a re-derivable address for the "
                          "claim, so nothing could be reconciled against it. A limit of "
                          "what extraction recovered, not of the paper.",
    "REPORTING_BLOCKED": "the claim is addressed and the paper prints no single unambiguous "
                         "quantity there to compare against. A limit of how the result was "
                         "reported, not of our extraction.",
    "NO_ROUTE_AVAILABLE": "the claim is addressed and quantified, and no verification route "
                          "this system has would settle the question it raises. A limit of "
                          "this review's method inventory.",
    "ENVIRONMENT_BLOCKED": "the experiment could not be set up on this host. A fact about "
                           "this machine.",
    "RESOURCE_BLOCKED": "the hardware this experiment requires is not available here. A "
                        "fact about this machine.",
    "IDENTITY_BLOCKED": "what would have run was not bound to what was printed, so its "
                        "output could not have answered the question.",
    "COMPARISON_BLOCKED": "a route applies to this question and this review could not "
                          "carry out the comparison at the end of it, so nothing was "
                          "started. A limit of this review's arithmetic, not of the "
                          "paper and not of its artifact.",
    "AUTHORIZATION_BLOCKED": "a gate in this harness refused. A fact about this harness.",
    "INCONCLUSIVE": "it ran and settled nothing.",
    "NO_EXPERIMENT_NEEDED": "the review judged that no experiment would settle this, and "
                            "reports it as an open question rather than pretending to have "
                            "tried. Not a failure to check — a decision not to.",
    "BUDGET_DEFERRED": "warranted and ordered, and this run's target budget was spent on "
                       "higher-priority targets first. A limit of this run.",
    "NOT_ATTEMPTED": "not pursued; the review did not consider an experiment justified here.",
    "PENDING": "planned and not yet carried out.",
}


def _ledger_entry(idx: int, obj, plan, outcome, probe: ProbeResult | None) -> LedgerEntry:
    ref = getattr(obj, "ref", None)
    provenance = getattr(outcome, "provenance", "") or getattr(plan, "route", "")
    rec = getattr(outcome, "reconciliation", None)
    disposition = getattr(outcome, "disposition", "NOT_ATTEMPTED")

    observed = ""
    compared = ""
    if rec is not None:
        if rec.reproduced_value is not None:
            observed = (f"{rec.reproduced_value} over seeds {rec.seeds_run} "
                        f"(2 sigma = {rec.noise_band})")
        compared = rec.claimed_raw or ""
    elif disposition in ("PAPER_ONLY_RESOLVED", "CITATION_VERIFIED_ONLY",
                        "PAPER_ARITHMETIC_CONTRADICTION"):
        observed = getattr(outcome, "reason", "")
        compared = getattr(ref, "quote", "") if ref else ""

    return LedgerEntry(
        entry_id=f"L{idx:03d}",
        claim_text=getattr(obj, "claim_text", ""),
        source_ref=getattr(ref, "ref", "") if ref else "",
        source_quote=getattr(ref, "quote", "") if ref else "",
        question=getattr(obj, "question_id", ""),
        target_id=getattr(obj, "target_id", ""),
        selection_reason=getattr(obj, "priority_reason", ""),
        route=getattr(outcome, "route", "") or getattr(plan, "route", ""),
        action=getattr(outcome, "action", "") or getattr(plan, "action", ""),
        provenance=getattr(outcome, "provenance", ""),
        commit=(probe.repo.commit if (probe and probe.repo) else ""),
        command=list(getattr(probe, "command", []) or []) if probe else [],
        observed=observed,
        compared_with=compared,
        admissibility=_LEDGER_ADMISSIBILITY.get(
            provenance, f"provenance '{provenance or 'none'}' is not one the ceiling admits"),
        disposition=disposition,
        implication=_LEDGER_IMPLICATION.get(disposition, ""),
        materiality_basis=getattr(obj, "materiality_basis", "NONE") or "NONE",
        evidence_state=getattr(outcome, "evidence_state", "NOT_INVESTIGATED"),
        resolution_state=getattr(outcome, "resolution_state", "NOT_INVESTIGATED"),
        necessity=(outcome.necessity(bool(getattr(plan, "requires_execution", False)))
                   if outcome is not None else
                   getattr(plan, "necessity", "NO_EXPERIMENT_NEEDED")),
        concerns_the_paper=bool(getattr(outcome, "concerns_the_paper", False)),
        launched=int(getattr(outcome, "launched", 0) or 0),
        why_material=getattr(plan, "why_material", ""),
    )


def _ledger_question_entry(idx: int, q: ReviewQuestion) -> LedgerEntry:
    """One trace line per REVIEW QUESTION, whether or not a target was ever built for it —
    an entry-per-target ledger silently loses every question with no addressable target,
    which is most of them on most papers."""
    ref = q.claim_ref
    return LedgerEntry(
        entry_id=f"Q{idx:03d}",
        claim_text=q.question,
        source_ref=getattr(ref, "ref", "") if ref else "",
        source_quote=getattr(ref, "quote", "") if ref else "",
        question=q.question_id,
        target_id="",
        selection_reason=q.why_it_matters,
        route=q.route,
        action="",
        provenance="",
        observed=q.resolution,
        compared_with="",
        admissibility="a review question; nothing is compared unless a target resolved it",
        disposition="",
        implication=q.conclusion,
        evidence_state=q.evidence_state,
        resolution_state=q.resolution_status,
        necessity="",
        concerns_the_paper=False,
        launched=0,
        why_material=q.why_it_matters,
    )


def build_ledger(report: EvalReport, target_set: TargetSet | None,
                 probe: ProbeResult | None = None, *, seconds: float = 0.0) -> CaseLedger:
    """The whole trace for one paper. Copies; derives nothing."""
    ts = target_set or TargetSet(paper_id=report.paper_id)
    # THE CURRENT plan for each target, not the raw log — a re-plan appends a SECOND
    # `PlanDecision` for the same target_id rather than replacing the first.
    live_plans = decide.current_plans(ts.plans)
    plans = {p.target_id: p for p in live_plans}
    outcomes = {o.target_id: o for o in ts.outcomes}

    entries: list[LedgerEntry] = []
    for obj in ts.objects:
        outcome = outcomes.get(obj.target_id)
        plan = plans.get(obj.target_id)
        if outcome is None and plan is None:
            continue
        entries.append(_ledger_entry(len(entries) + 1, obj, plan, outcome, probe))
    for i, q in enumerate(ts.questions, start=1):
        entries.append(_ledger_question_entry(i, q))

    blocked_by_gate: dict[str, int] = {}
    for p in live_plans:
        if p.blocking_gate:
            blocked_by_gate[p.blocking_gate] = blocked_by_gate.get(p.blocking_gate, 0) + 1

    efficiency = {
        # The six-term funnel, each counted from a DIFFERENT artifact — these were one
        # number in three places before, and the collapse was a real overclaim.
        "targets_discovered": len(ts.objects),
        "targets_addressable": sum(1 for o in ts.objects if o.harness_addressable),
        "targets_warranting_experiment": sum(1 for p in live_plans if p.requires_execution),
        "targets_launched": sum(1 for o in ts.outcomes if o.launched > 0),
        "processes_launched": sum(o.launched for o in ts.outcomes),
        "executions_completed": sum(1 for o in ts.outcomes
                                    if o.launched > 0 and o.reconciliation is not None),
        "targets_resolved": sum(1 for o in ts.outcomes if o.concerns_the_paper),
        "questions_generated": len(ts.questions),
        "questions_resolved_without_execution":
            sum(1 for q in ts.questions if q.resolved_without_execution),
        "questions_open": sum(1 for q in ts.questions if q.is_open),
        "targets_requiring_execution": sum(1 for p in live_plans if p.requires_execution),
        "targets_resolved_without_execution":
            sum(1 for o in ts.outcomes if o.disposition in
               ("PAPER_ONLY_RESOLVED", "PAPER_ARITHMETIC_CONTRADICTION")),
        "targets_citation_verified_only":
            sum(1 for o in ts.outcomes if o.disposition == "CITATION_VERIFIED_ONLY"),
        "targets_blocked_before_execution":
            sum(1 for o in ts.outcomes if o.disposition in BLOCKED_DISPOSITIONS),
        "targets_deferred_by_budget":
            sum(1 for o in ts.outcomes if o.disposition == "BUDGET_DEFERRED"),
        "targets_settled": sum(1 for o in ts.outcomes if o.disposition in
                               ("REPRODUCED", "FAILED_REPRODUCTION", "PAPER_ONLY_RESOLVED",
                                "PAPER_ARITHMETIC_CONTRADICTION")),
        "blocked_by_gate": blocked_by_gate,
        # Wall time of the PROBE STAGE — acquisition, static audit, planning, every gate —
        # never the cost of running experiments.
        "probe_stage_seconds": round(float(seconds or (probe.seconds if probe else 0.0)), 3),
        "findings_after_verification": len(report.findings),
        "findings_dropped_unsubstantiated": report.dropped_findings,
    }
    by_scientific: dict[str, int] = {}
    for f in report.findings:
        k = getattr(f, "scientific_class", "") or "UNRESOLVED_QUESTION"
        by_scientific[k] = by_scientific.get(k, 0) + 1
    by_resolution: dict[str, int] = {}
    by_evidence: dict[str, int] = {}
    by_necessity: dict[str, int] = {}
    plans_by_id = {p.target_id: p for p in live_plans}
    for o in ts.outcomes:
        by_resolution[o.resolution_state] = by_resolution.get(o.resolution_state, 0) + 1
        by_evidence[o.evidence_state] = by_evidence.get(o.evidence_state, 0) + 1
        pl = plans_by_id.get(o.target_id)
        n = o.necessity(bool(pl.requires_execution) if pl else False)
        by_necessity[n] = by_necessity.get(n, 0) + 1
    efficiency["findings_by_scientific_class"] = by_scientific
    efficiency["targets_by_resolution_state"] = by_resolution
    efficiency["targets_by_evidence_state"] = by_evidence
    efficiency["targets_by_experiment_necessity"] = by_necessity
    efficiency["route_exhaustion"] = decide.route_coverage(
        ts.route_attempts, tuple(ts.route_exhaustion_question_ids))
    return CaseLedger(paper_id=report.paper_id, entries=entries,
                      route_attempts=list(ts.route_attempts), efficiency=efficiency)


# =============================================================================
# PART 9 — assemble_report: the pure equivalent of the old `run_report`
# =============================================================================

def _verdict_agreement(substantive: SubstantiveVerdict | None, verdict: str
                       ) -> tuple[str, bool]:
    """Compare the whole-paper model read against the deterministic verdict. Printed,
    never counted — its only consequence is the CONTESTED flag."""
    if substantive is None:
        return "unavailable", False
    if substantive.verdict == "CENTRAL_CLAIM_NOT_ESTABLISHED" and verdict == "GREEN":
        return "contested", True
    if substantive.verdict in ("STRONG", "SOUND_WITH_MINOR_CONCERNS") and verdict == "RED":
        return "harness_harsher", False
    if (substantive.verdict in ("SUBSTANTIAL_CONCERNS", "CENTRAL_CLAIM_NOT_ESTABLISHED")
            and verdict == "GREEN"):
        return "model_harsher", False
    return "agree", False


def assemble_report(*, pid: str, title: str, doc: PaperDoc, reports: list[LensReport],
                    dropped_findings: int, probe: ProbeResult | None,
                    target_set: TargetSet | None, grade_coverage: dict,
                    substantive_verdict: SubstantiveVerdict | None = None,
                    reading_record: ReadingRecord | None = None,
                    lens_policy_enforced: bool = False, probe_stage_seconds: float = 0.0,
                    cfg=None) -> tuple[EvalReport, CaseLedger]:
    """The pure assembly a caller runs once ingest, collect (quote-verified `reports`),
    grade (`counted_severity` already attached) and discover/probe (`target_set`) have
    produced their artifacts. Everything here is deterministic; the one optional side
    channel is `cfg`, passed straight through to `decide.refresh_route_attempts` so a
    caller that HAS a live `Config` gets gate-aware route accounting and a caller that does
    not (`cfg=None`) still gets a complete, if slightly less gate-aware, result.

    Returns `(report, ledger)`; the caller renders with `render_eval_report` /
    `render_reviewer_report` and is responsible for all file I/O.
    """
    findings = rank([f for r in reports for f in r.findings])
    lenses_run = [r.lens for r in reports]

    if target_set is not None:
        decide.refresh_route_attempts(target_set, cfg)
    outcomes = list(target_set.outcomes) if target_set else []
    objects = list(target_set.objects) if target_set else []

    rec = probe.reconciliation if probe else None
    verdict, reason = overall_verdict(findings, rec, outcomes=outcomes, objects=objects,
                                      probe=probe)
    status, status_why = claim_status(findings, rec, outcomes=outcomes, objects=objects,
                                      probe=probe)
    triage_level, triage_why = triage(findings, rec, outcomes=outcomes, objects=objects,
                                      probe=probe)
    assert triage_level in TRIAGE_LEVELS, triage_level
    repro = reproduction_status(probe)
    ran_as = provenance_label(probe.provenance) if probe else "SYNTHESIZED_DIAGNOSTIC"
    agreement, contested = _verdict_agreement(substantive_verdict, verdict)

    report = EvalReport(
        paper_id=pid, title=title, verdict=verdict, verdict_reason=reason,
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
        lenses_run=lenses_run, dropped_findings=dropped_findings, probe=probe,
        experimental_chain=build_chain(findings, probe),
        grade_coverage={k: v for k, v in grade_coverage.items() if k != "paper_id"},
        substantive_verdict=substantive_verdict, verdict_agreement=agreement,
        verdict_contested=contested,
        triage=triage_level, triage_reason=triage_why,
        targets_summary=_targets_summary(outcomes),
    )
    report.scientific_findings = scientific_findings(report, target_set)
    report.artifact_state, report.review_path = artifact_axis(probe)
    report.document_observations = list(observe(doc))
    surf = surface(doc)
    addressed, examined = coverage_numerators(target_set)
    report.coverage = measure(surf, addressed=addressed, examined=examined,
                              reading=reading_record)
    report.outcome = derive_outcome(
        report, target_set, unchecked_central=len(unchecked_central(objects, outcomes)))

    # WHAT HAPPENS TO THE PAPER — folded, never decided here. The blockers are read from
    # CENTRAL targets only; a blocked SUPPORTING target is scope information, not a reason
    # to say the paper could not be checked.
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
        decide.derive_disposition(
            review_complete=True, claim_status=status,
            basis=decide.disposition_basis_for(
                failed_target_provenance=getattr(_failed, "provenance", ""),
                paper_arithmetic_failed=(
                    getattr(_failed, "disposition", "") == "PAPER_ARITHMETIC_CONTRADICTION")),
            central_blockers=decide.blockers_from(_central_dispositions, _centralities),
            central_unresolved=len(unresolved_central(objects, outcomes)),
            counted_major=len(material_concerns(findings)),
            established_non_material=(
                0 if _failed is not None
                else len(decide.established_defects(outcomes))))

    report.self_audit = self_audit(report, counted)

    case_ledger = build_ledger(report, target_set, probe, seconds=probe_stage_seconds)
    report.review_efficiency = case_ledger.efficiency
    report.ledger_path = f"reports/{pid}.ledger.json"
    report.reviewer_report_path = f"reports/{pid}.review.md"

    # LAST, because it reads everything else, including the just-attached ledger.
    report.guarantees = derive_guarantees(report, target_set,
                                          lens_policy_enforced=lens_policy_enforced)
    return report, case_ledger


# =============================================================================
# SELF-CHECK
# =============================================================================
def _f(fid, lens, sev, ref="", verifiable=False, **kw):
    return Finding(finding_id=fid, lens=lens, severity=sev, title=f"{fid} title",
                   statement="s", evidence_quote="q", evidence_ref=ref,
                   verifiable_by_experiment=verifiable, **kw)


def _self_check_decision() -> None:
    assert parse_magnitude("+3-5% top-1") == 3.0
    assert parse_magnitude("not a number") == 0.0
    assert parse_magnitude("") == 0.0

    # invariant 8: MATERIAL_SEVERITY is the empty tuple, and material_failures is dead by
    # construction — no severity, however asserted, may ever reach RED through it.
    assert MATERIAL_SEVERITY == ()
    for sevs in ([], ["FATAL"], ["FATAL"] * 40, ["MAJOR"] * 99):
        assert material_failures([_f(str(i), "protocol", s) for i, s in enumerate(sevs)]) == []

    # model severity cannot grant itself rejection authority
    assert overall_verdict([_f("a", "overclaim", "FATAL")])[0] == "GREEN"
    assert overall_verdict([_f(str(i), "protocol", "MAJOR") for i in range(40)])[0] == "GREEN"
    assert overall_verdict([])[0] == "GREEN"
    assert claim_status([_f("a", "overclaim", "FATAL")])[0] == "NOT_VERIFIED"
    assert claim_status([])[0] == "NOT_VERIFIED"
    assert reproduction_status(None) == "NOT_ATTEMPTED"
    assert provenance_label("repo_exec") == "AUTHOR_REPOSITORY"
    assert provenance_label("driver") == "INDEPENDENT_REIMPLEMENTATION"
    assert provenance_label("nonsense") == "SYNTHESIZED_DIAGNOSTIC"

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

    def _rec(status, **kw):
        kw.setdefault("provenance", "repo_exec")
        return Reconciliation(table_ref="T1:r0:c1", claimed_raw="59.28", claimed_value=59.28,
                              noise_band=0.1, status=status, reason="r", **kw)

    # A failed reproduction on a MATERIAL target is RED on its own; the same defect on a
    # target whose materiality was not established is GREEN but still reported.
    material = [DiscoveredObject(target_id="T1", materiality_basis="ABSTRACT_CLAIM")]
    incidental = [DiscoveredObject(target_id="T1", materiality_basis="NONE")]
    failed_rec = _rec("FAILED_REPRODUCTION", reproduced_value=64.1, delta_error=4.82,
                      target_id="T1")
    v, why = overall_verdict([], failed_rec, objects=material)
    assert v == "RED" and "Failed code reproduction" in why, why
    v2, why2 = overall_verdict([], failed_rec, objects=incidental)
    assert v2 == "GREEN" and "did not establish" in why2, why2
    assert overall_verdict([], failed_rec)[0] == "GREEN", (
        "a paper-level stop may never depend on whether a caller passed materiality context")
    for objs in (material, incidental, None):
        v3, _ = overall_verdict([], failed_rec, objects=objs)
        s3, _ = claim_status([], failed_rec, objects=objs)
        assert (v3 == "RED") == (s3 == "VERIFIED_FAILURE"), (objs, v3, s3)
    assert overall_verdict([], _rec("INCONCLUSIVE"))[0] == "GREEN"
    assert overall_verdict([], _rec("RESOLVED_VERIFIED"))[0] == "GREEN"
    assert overall_verdict([_f("a", "overclaim", "FATAL")], _rec("RESOLVED_VERIFIED"))[0] == "GREEN"

    # THE PROVENANCE CEILING, APPLIED A SECOND TIME AT THE REPORTING LAYER. The reconciler
    # (`local_exec.reconcile`) already refused to let a `synthesized` provenance settle a
    # printed cell; this asserts the SAME refusal happens again here, independently, if a
    # FAILED_REPRODUCTION carrying an inadmissible provenance ever reaches this layer
    # anyway (an edited artifact, an upstream bug) — it must read as a harness defect
    # (GREEN, "should have been unreachable"), never as a paper-level conviction, and the
    # matching execution-row state is EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE, not
    # EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY.
    inadmissible_rec = _rec("FAILED_REPRODUCTION", provenance="synthesized",
                           reproduced_value=1.0, delta_error=99, target_id="T1")
    v4, why4 = overall_verdict([], inadmissible_rec, objects=material)
    assert v4 == "GREEN" and "should have been unreachable" in why4, why4
    s4, _ = claim_status([], inadmissible_rec, objects=material)
    assert s4 == "NOT_VERIFIED"
    warrant = [PlanDecision(target_id="T1", requires_execution=True)]
    inadmissible_outcome = TargetOutcome(target_id="T1", disposition="FAILED_REPRODUCTION",
                                        provenance="synthesized", launched=1,
                                        reason="a synthesized probe disagreed")
    e_state, _ = execution_state([inadmissible_outcome], warrant)
    assert e_state == "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE", (
        "a synthesized probe must never read as having contradicted a printed quantity")

    blocked = _repo_block(RepoAcquisition(
        url="https://github.com/x/y", status="blocked", reason="network gate closed"))
    assert "network gate is closed" in "\n".join(blocked)
    clean = _code_audit_block(CodeAudit(repo_path="r", files_scanned=3, lines_scanned=90))
    assert "not a clean bill of health" in "\n".join(clean)
    assert "Not run." in "\n".join(_code_audit_block(CodeAudit(skipped="nothing acquired")))
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
    print("report/decision self-check ok")


def _self_check_outcome() -> None:
    import inspect

    params = set(inspect.signature(finding_state).parameters)
    assert params == {"claim_status", "kept_findings"}, params
    for bad in ("execution_state", "disposition", "reconciliation", "provenance",
                "outcomes", "probe", "launched"):
        assert bad not in params, f"tier 1 must not be able to read {bad}"

    for cs in ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED", ""):
        for kept in (0, 1, 9):
            base = finding_state(claim_status=cs, kept_findings=kept)
            for disp in ("INCONCLUSIVE", "ENVIRONMENT_BLOCKED", "AUTHORIZATION_BLOCKED",
                         "FAILED_REPRODUCTION", "REPRODUCED", "NOT_ATTEMPTED"):
                for prov in ("repo_exec", "driver", "synthesized", "paper", ""):
                    o = TargetOutcome(target_id="T", disposition=disp, provenance=prov,
                                      launched=1, reason="r")
                    st, _ = execution_state([o], [PlanDecision(target_id="T",
                                                               requires_execution=True)])
                    assert st in EXECUTION_STATES, st
                    assert finding_state(claim_status=cs, kept_findings=kept) == base, (
                        f"{disp}/{prov} changed the finding state")

    assert finding_state(claim_status="VERIFIED_FAILURE") == "MATERIAL_FAILURE_ESTABLISHED"
    assert finding_state(claim_status="NOT_VERIFIED", kept_findings=3) == "CONCERNS_RECORDED"
    assert finding_state(claim_status="NOT_VERIFIED") == "NO_CONCERN_SURVIVED_VERIFICATION"
    assert finding_state(claim_status="VERIFIED_SUPPORT", kept_findings=2) == "CONCERNS_RECORDED"

    assert question_state() == "NO_QUESTION_RAISED"
    assert question_state(raised=4, settled=4) == "ALL_QUESTIONS_SETTLED"
    assert question_state(raised=4, settled=1) == "SOME_QUESTIONS_SETTLED"
    assert question_state(raised=4) == "NO_QUESTION_SETTLED"

    assert execution_state([], [])[0] == "NO_EXECUTION_WARRANTED"
    warrant = [PlanDecision(target_id="T", requires_execution=True)]
    assert execution_state([TargetOutcome(target_id="T", disposition="NOT_ATTEMPTED")],
                           warrant)[0] == "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED"
    for d in BLOCKED_DISPOSITIONS:
        st, why = execution_state([TargetOutcome(target_id="T", disposition=d,
                                                 reason="the gate refused")], warrant)
        assert st == "EXECUTION_BLOCKED_BEFORE_IT_STARTED", d
        assert "the gate refused" in why
    arith = TargetOutcome(target_id="T", disposition="PAPER_ARITHMETIC_CONTRADICTION",
                          provenance="paper", launched=0, reason="12*3 is not 40")
    assert arith.establishes_failure
    st, _ = execution_state([arith], warrant)
    assert st == "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED", (
        "a paper-arithmetic contradiction establishes a failure with nothing launched, so "
        "the EXECUTION row — which names an ACTOR that ran — may not claim it")
    assert execution_state([arith], [])[0] == "NO_EXECUTION_WARRANTED"
    for state in EXECUTION_ABOUT_THE_PAPER:
        assert "{actor} ran" in EXECUTION_GLOSS[state]
    assert execution_actor([]) == _DEFAULT_ACTOR
    for prov, expect in (("repo_exec", "the authors' own code"),
                         ("reimpl_exec", "an independent reimplementation (not the authors' code)"),
                         ("driver", "an operator-supplied reproduction")):
        got = execution_actor([TargetOutcome(target_id="T", disposition="FAILED_REPRODUCTION",
                                             provenance=prov, launched=1)])
        assert got == expect, (prov, got)

    assert scope_state() == "NO_TARGET_PURSUED"
    assert scope_state(pursued=2) == "SOME_TARGETS_PURSUED"
    assert scope_state(unchecked_central=1, pursued=9) == "CENTRAL_CLAIMS_LEFT_UNCHECKED"

    for tbl, vocab in ((FINDING_GLOSS, FINDING_STATES), (QUESTION_GLOSS, QUESTION_STATES),
                       (EXECUTION_GLOSS, EXECUTION_STATES), (SCOPE_GLOSS, SCOPE_STATES)):
        assert set(tbl) == set(vocab), (set(tbl) ^ set(vocab))

    # THE FOUR-ROW FOLD, over representative (claim_status, kept, execution, scope) shapes.
    ts = TargetSet(
        paper_id="p",
        questions=[ReviewQuestion(question_id="Q1", resolution_status="UNRESOLVED"),
                   ReviewQuestion(question_id="Q2", resolution_status="RESOLVED_FROM_PAPER")],
        plans=[PlanDecision(target_id="T", requires_execution=True)],
        outcomes=[TargetOutcome(target_id="T", disposition="INCONCLUSIVE", launched=1,
                                provenance="synthesized", reason="the metric was unparsed")])
    rep = EvalReport(paper_id="p", title="t", verdict="GREEN", triage="YELLOW",
                     claim_status="NOT_VERIFIED", findings=[], scientific_findings=[])
    o = derive_outcome(rep, ts, unchecked_central=1)
    assert o.finding_state == "NO_CONCERN_SURVIVED_VERIFICATION"
    assert o.question_state == "SOME_QUESTIONS_SETTLED"
    assert o.execution_state == "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE"
    assert o.scope_state == "CENTRAL_CLAIMS_LEFT_UNCHECKED"
    text = "\n".join(render_outcome(o))
    assert text.startswith("## Review outcome")
    assert "did not produce admissible evidence" in text
    assert "Exact reason: target T: the metric was unparsed" in text

    # a second shape: a material failure with kept findings, all questions settled, some
    # targets pursued
    ts2 = TargetSet(paper_id="p2",
                    questions=[ReviewQuestion(question_id="Q1",
                                              resolution_status="RESOLVED_FROM_PAPER")],
                    outcomes=[TargetOutcome(target_id="T1", disposition="FAILED_REPRODUCTION",
                                            provenance="repo_exec", launched=1,
                                            reason="0.61 vs 0.42")])
    rep2 = EvalReport(paper_id="p2", verdict="RED", claim_status="VERIFIED_FAILURE",
                      findings=[_f("a", "protocol", "MAJOR")])
    o2 = derive_outcome(rep2, ts2)
    assert o2.finding_state == "MATERIAL_FAILURE_ESTABLISHED"
    assert o2.question_state == "ALL_QUESTIONS_SETTLED"
    assert o2.execution_state == "EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY"
    assert o2.scope_state == "SOME_TARGETS_PURSUED"

    long = ("the probe that ran was synthesized, not the paper's own code. Its result is "
            "evidence about the mechanism. " + "x" * 500)
    got = clip(long)
    assert got.endswith("(full reason in the ledger)")
    assert "evidence about the mechanism." in got
    assert clip("short enough") == "short enough"
    print("outcome self-check ok")


def _self_check_guarantees() -> None:
    import inspect

    groups = (PROCESS_GUARANTEES, CONDITIONAL_PROPERTIES, SCIENTIFIC_NON_GUARANTEES)
    for i, a in enumerate(groups):
        assert len(set(a)) == len(a)
        for b in groups[i + 1:]:
            assert not (set(a) & set(b))
    assert len(set(ALL_KEYS)) == len(ALL_KEYS) == 21, len(ALL_KEYS)
    for table, name in ((KIND, "KIND"), (STATEMENT, "STATEMENT"), (NOT_HELD, "NOT_HELD"),
                        (SHORT, "SHORT"), (ESTABLISHED_BY, "ESTABLISHED_BY")):
        assert set(table) == set(ALL_KEYS), (name, set(table) ^ set(ALL_KEYS))
    assert set(KIND.values()) <= set(GUARANTEE_KINDS)
    assert set(_PARAM_FOR_KEY) == set(PROCESS_GUARANTEES) | set(CONDITIONAL_PROPERTIES)
    assert set(_PARAM_FOR_KEY.values()) == set(inspect.signature(assess).parameters)
    for k in SCIENTIFIC_NON_GUARANTEES:
        assert k not in _PARAM_FOR_KEY, k

    sig = inspect.signature(assess)
    for name, p in sig.parameters.items():
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert str(p.annotation) == "bool"
        assert p.default is False

    names = list(sig.parameters)
    for mask in range(1 << len(names)):
        kwargs = {n: bool(mask >> i & 1) for i, n in enumerate(names)}
        items = assess(**kwargs)
        assert [getattr(g, "key", "") for g in items] == list(ALL_KEYS)
        for g in items:
            if getattr(g, "key", "") in SCIENTIFIC_NON_GUARANTEES:
                assert g.holds is False
                assert g.kind == "SCIENTIFIC"
        assert any(not g.holds for g in items)

    every = assess(**{n: True for n in names})
    assert sum(1 for g in every if g.holds) == len(names)
    assert sum(1 for g in every if not g.holds) == len(SCIENTIFIC_NON_GUARANTEES)

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
    g = derive_guarantees(rep, ts)
    assert isinstance(g, ReviewGuarantees) and g.paper_id == "p"
    assert not g.unmet, g.unmet
    assert len(g.guarantees) == len(ALL_KEYS)
    assert g.non_guarantees
    for k in SCIENTIFIC_NON_GUARANTEES:
        assert NOT_HELD[k] in g.non_guarantees, k
    for it in g.guarantees:
        assert it.evidence.startswith(ESTABLISHED_BY[getattr(it, "key", "")])

    unverified = rep.model_copy(update={
        "findings": [good.model_copy(update={"evidence_class": ""})]})
    assert derive_guarantees(unverified, ts).unmet == ["EVERY_EVIDENCE_POINTER_RE_VERIFIED"]
    synth = rep.model_copy(update={"probe": ProbeResult(
        paper_id="p", reconciliation=Reconciliation(status="FAILED_REPRODUCTION",
                                                    provenance="synthesized"))})
    assert "PROVENANCE_CEILING_HELD" in derive_guarantees(synth, ts).unmet
    unauth = rep.model_copy(update={"probe": ProbeResult(
        paper_id="p", executions=2, execution_log="runs/p/execution.jsonl")})
    assert "EXECUTION_AUTHORIZED_BY_ONE_CONJUNCTION" in derive_guarantees(unauth, ts).unmet
    convicted = TargetSet(paper_id="p", outcomes=[TargetOutcome(
        target_id="T1", disposition="FAILED_REPRODUCTION", provenance="repo_exec",
        failure_class="dependency_missing", launched=1, reason="a wheel was missing")])
    bad = derive_guarantees(rep.model_copy(update={"verdict": "RED",
                                                   "claim_status": "VERIFIED_FAILURE"}),
                            convicted)
    assert "INFRASTRUCTURE_FAILURE_NEVER_CONVICTED" in bad.unmet
    silent = TargetSet(paper_id="p", outcomes=[
        TargetOutcome(target_id="T1", disposition="RESOURCE_BLOCKED", reason="")])
    assert "EVERY_UNRESOLVED_STATE_NAMED" in derive_guarantees(rep, silent).unmet
    assert "ACCOUNTING_REPRODUCIBLE" in derive_guarantees(
        rep.model_copy(update={"ledger_path": ""}), ts).unmet
    promoted = rep.model_copy(update={"findings": [
        good.model_copy(update={"severity": "MINOR", "counted_severity": "FATAL"})]})
    assert "SEVERITY_ONLY_CAPPED" in derive_guarantees(promoted, ts).unmet
    assert "NO_MODEL_WROTE_THE_DECISION" in derive_guarantees(
        rep.model_copy(update={"verdict": "RED", "claim_status": "VERIFIED_FAILURE"}),
        ts).unmet
    shrunk = TargetSet(paper_id="p", outcomes=[TargetOutcome(
        target_id="T1", disposition="RESOURCE_BLOCKED", launched=1,
        reason="24 GiB demanded, 8 GiB present")])
    assert "NO_EXPERIMENT_DOWNSCALED" in derive_guarantees(rep, shrunk).unmet

    for k in CONDITIONAL_PROPERTIES:
        assert k not in g.unmet
        assert NOT_HELD[k] in g.non_guarantees
    assert set(g.unmet) <= set(PROCESS_GUARANTEES)

    covered = derive_guarantees(rep.model_copy(update={
        "grade_coverage": {"candidates": 2, "graded": 2, "pending": 0}}), ts)
    assert NOT_HELD["SEVERITY_INDEPENDENTLY_GRADED"] not in covered.non_guarantees
    partly = derive_guarantees(rep.model_copy(update={
        "grade_coverage": {"candidates": 3, "graded": 1}}), ts)
    assert NOT_HELD["SEVERITY_INDEPENDENTLY_GRADED"] in partly.non_guarantees

    text = "\n".join(render_guarantees(g))
    assert text.startswith(HEADING)
    assert len(text) <= _MAX_SECTION_CHARS
    assert "Machine-enforced here:" in text and "Not established here:" in text
    for clause in ("accuracy unmeasured", "novelty not assessed",
                   "nothing here says the paper is correct",
                   "not a substitute for a referee"):
        assert clause in text, clause
    for forbidden in ("RED", "GREEN", "YELLOW", "FATAL", "MAJOR", "VERIFIED_FAILURE"):
        assert forbidden not in text, forbidden
    defect_text = "\n".join(render_guarantees(derive_guarantees(promoted, ts)))
    assert "DEFECT IN THIS HARNESS" in defect_text

    trimmed = _clauses(["aaaa", "bbbb", "cccc", "dddd"], 12)
    assert trimmed.endswith("more, in the ledger)")

    # the section cannot become the outcome: parameter absence, same discipline as tier 1
    for fn in (overall_verdict, claim_status, triage):
        params = set(inspect.signature(fn).parameters)
        for bad_param in ("guarantees", "guarantee", "non_guarantees", "unmet"):
            assert bad_param not in params, (fn.__name__, bad_param)
    print("guarantees self-check ok")


def _self_check_coverage() -> None:
    import inspect

    from .schema import Equation, Figure, QuantFinding, Section, Table

    params = inspect.signature(surface).parameters
    assert list(params) == ["doc"], params
    assert "PaperDoc" in str(params["doc"].annotation)
    mp = inspect.signature(measure).parameters
    assert list(mp) == ["surf", "addressed", "examined", "reading"], mp
    for name in ("addressed", "examined"):
        assert mp[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert "tuple[str" in str(mp[name].annotation)

    # coverage.None-not-1.0: an empty surface has no denominator, so a rate over it is
    # NEVER 1.0 — it is None, and "0 of 0" stays unmeasurable rather than perfect.
    empty = surface(PaperDoc(paper_id="empty"))
    assert empty.surface_empty is True and empty.addresses == []
    zero = measure(empty, addressed=("T1:r0:c0",), examined=())
    assert zero.addressed_rate is None and zero.examined_rate is None, zero
    assert zero.addressed == 0 and zero.off_surface == ["T1:r0:c0"], zero
    assert zero.semantic_coverage == "not_machine_detectable"

    doc = PaperDoc(
        paper_id="selfcheck", content_sha="abc123abc123", n_pages=4,
        sections=[Section(section_idx=0, title="Method", page_start=1,
                          text="We sample 58 topics x 5 templates x 10 instances = 2,900 "
                               "cases in total, one per template."),
                  Section(section_idx=1, title="Empty", page_start=2, text="")],
        tables=[Table(table_idx=1, page=2, header=["method", "acc"],
                      rows=[["ours", "61.4"], ["base", ""]])],
        figures=[Figure(figure_idx=1, page=3, label="Figure 1",
                        caption="Accuracy by seed."),
                 Figure(figure_idx=2, page=3, label="Figure 2", caption="")],
        equations=[Equation(equation_idx=1, page=3, number="(1)", text="L = a + b"),
                   Equation(equation_idx=2, page=3, number="(2)", text="")],
        reported_numbers=[
            QuantFinding(value="2,900", page=1,
                         source_quote="We sample 58 topics x 5 templates x 10 instances "
                                      "= 2,900 cases in total, one per template."),
            QuantFinding(value="61.4", page=2, table_ref="T1:r0:c1", source_quote="61.4"),
        ],
    )
    surf = surface(doc)
    assert surf.content_sha == "abc123abc123"
    assert surf.table_cells_nonempty == 3 and surf.table_cells_total == 4, surf.by_kind
    assert surf.by_kind["figure"] == 1, "a caption-less figure carries no citable text"
    assert surf.by_kind["equation"] == 1, "an equation with no recovered body is not a unit"
    assert surf.by_kind["section_span"] == 1, "an empty section is not addressable"
    assert surf.by_kind["reported_quantity"] == 1, surf.by_kind
    assert len(surf.addresses) == len(set(surf.addresses)), "the surface is a SET"
    assert surf.surface_empty is False and len(surf.addresses) == 7, surf.addresses
    assert "T1:r0:c1" in surf.addresses
    for addr in surf.addresses:
        got = locate.resolve(doc, addr)
        assert got.resolved, (addr, got.resolution, got.detail)
        assert kind_of(addr) in SURFACE_KINDS, addr
    (prose_addr,) = [a for a in surf.addresses if a.startswith("P")]
    assert locate.mint(doc, doc.reported_numbers[0].source_quote).ref == prose_addr

    rep = measure(surf, addressed=("T1:r0:c0", "T1:r0:c1", "S0"), examined=("S0",))
    assert rep.surface_size == 7 and rep.addressed == 3 and rep.examined == 1, rep
    assert rep.examined <= rep.addressed <= rep.surface_size
    assert abs(rep.addressed_rate - 3 / 7) < 1e-12
    assert abs(rep.examined_rate - 1 / 7) < 1e-12
    assert rep.by_kind_addressed["table_cell"] == 2
    assert rep.addressed != rep.examined

    folded = measure(surf, addressed=(), examined=("S0",))
    assert folded.addressed == 1 and folded.examined == 1
    none_addressed = measure(surf)
    assert none_addressed.addressed_rate == 0.0 and none_addressed.surface_size == 7

    off = measure(surf, addressed=("T9:r0:c0", "p7", "", "S0"))
    assert off.addressed == 1 and off.off_surface == ["T9:r0:c0", "p7"], off
    assert _fold("T1:r1:c1", set(surf.addresses)) == "", "an empty padding cell is not surface"
    try:
        measure(surf, addressed=(doc,))
        raise AssertionError("a non-address numerator must be refused")
    except TypeError as exc:
        assert "address string" in str(exc)
    assert _fold("P0:5-40", set(surf.addresses)) == "S0"
    assert _fold(prose_addr, set(surf.addresses)) == prose_addr

    addressed_all = tuple(surf.addresses)
    before = measure(surf, addressed=addressed_all)
    worse = doc.model_copy(deep=True)
    worse.tables = []
    after = measure(surface(worse), addressed=addressed_all)
    assert after.addressed_rate <= before.addressed_rate, (before, after)
    assert after.surface_size < before.surface_size, "the denominator is the PAPER"
    flood = measure(surf, addressed=tuple(surf.addresses) * 3 + ("T1:r0:c0",))
    assert flood.addressed == flood.surface_size == 7

    many = [Section(section_idx=i, title=f"S{i}", page_start=1, text="x" * 900)
            for i in range(6)]
    presented, total, cut = prose_presented(many, 3000)
    rendered = paper.render_sections(many, 3000)
    rendered_text = rendered[0] if isinstance(rendered, tuple) else rendered
    assert cut == rendered_text.count("…[truncated]") == 6
    assert presented == 6 * 500 and total == 6 * 900, (presented, total)
    assert prose_presented(many, 1) == (6 * _MIN_PER_SECTION, 6 * 900, 6)
    os.environ[_BUDGET_ENV] = "1000"
    try:
        tight = surface(PaperDoc(paper_id="t", sections=many))
        assert tight.reading_parts > 1, "a small budget buys more passes, not less paper"
        assert measure(tight).prose_presented_fraction == 1.0
    finally:
        del os.environ[_BUDGET_ENV]
    assert surface(PaperDoc(paper_id="t", sections=many)).reading_parts < tight.reading_parts
    assert measure(surf).prose_presented_fraction is not None
    assert measure(empty).prose_presented_fraction is None, "0 of 0 chars is not 1.0"

    for model in (ReviewSurface, CoverageReport):
        for banned in ("severity", "counted_severity", "verdict", "triage", "colour",
                       "color", "confidence", "claim_status", "scientific_class"):
            assert banned not in model.model_fields, (model.__name__, banned)
    print("coverage self-check ok")


def _doc_fixture(**kw) -> PaperDoc:
    fields: dict = {"paper_id": "selfcheck", "n_pages": 2}
    fields.update(kw)
    return PaperDoc.model_validate(fields)


def _self_check_docintegrity() -> None:
    import inspect

    from .schema import Equation, Figure, Section, Table

    params = inspect.signature(observe).parameters
    assert list(params) == ["doc"], params
    assert str(params["doc"].annotation) == "PaperDoc", params["doc"].annotation

    # about-required-no-default: `DocumentObservation.about` has NO default, so a naive
    # "cited but not recovered" check cannot silently fall back to an unstated value.
    assert "about" in DocumentObservation.model_fields
    assert DocumentObservation.model_fields["about"].is_required(), (
        "`about` must have no default: a missing one would fall to whichever value is safer")
    for banned in ("severity", "confidence", "scientific_class", "counted_severity",
                   "candidate_class", "verdict", "route", "resolution_status"):
        assert banned not in DocumentObservation.model_fields, banned

    for check in INTEGRITY_CHECKS + ("", "MADE_UP", "table_arithmetic"):
        for cls in EVIDENCE_CLASSES + ("", "made_up"):
            got = evidence_floor(check, cls)
            assert got in INTEGRITY_ABOUT + ("NOT_INVESTIGATED",), (check, cls, got)
            if check not in INTEGRITY_CHECKS or check in NOT_COMPUTED:
                assert got == "NOT_INVESTIGATED", (check, cls, got)
            elif check not in CLAIMABLE_ABOUT_THE_PAPER:
                assert got == "EXTRACTION", (check, cls, got)
            elif cls in _STRONG_INPUT:
                assert got == "PAPER", (check, cls, got)
            else:
                assert got == "NOT_INVESTIGATED", (
                    f"{check} over {cls!r} must refuse rather than compute")
    assert evidence_floor("TABLE_ARITHMETIC", "caption_verified") == "NOT_INVESTIGATED"
    assert evidence_floor("TABLE_ARITHMETIC", "cell_verified") == "PAPER"
    assert len(CLAIMABLE_ABOUT_THE_PAPER) == 1

    empty = _doc_fixture()
    assert observe(empty) == ()
    assert set(determinations(empty)) == set(INTEGRITY_CHECKS)
    assert set(determinations(empty).values()) == {"NOT_INVESTIGATED"}

    # the twelve measured false positives (12 "missing table/equation" claims, all false,
    # over the shipped corpus) can only ever be EXTRACTION, never PAPER
    cited = _doc_fixture(sections=[Section(section_idx=0, title="4. Experiments", page_start=1,
                                   text="Table 1 lists the comparison. Table 2 reports the "
                                        "ablation, and Table 3 the transfer results.")],
                 tables=[Table(table_idx=0, page=1, caption="Table 1: Comparison.",
                               rows=[["m", "1.0"]]),
                         Table(table_idx=1, page=1, caption="Table 3: Transfer.",
                               rows=[["m", "2.0"]])])
    crossref = [o for o in observe(cited) if o.check == "CROSSREF_UNRESOLVED"]
    assert len(crossref) == 1 and crossref[0].about == "EXTRACTION", crossref
    for o in observe(cited):
        if o.about == "EXTRACTION":
            assert "the paper" not in o.detail
        assert not o.ref or locate.resolve(cited, o.ref, o.quote).resolved, o.ref

    holes = _doc_fixture(equations=[Equation(equation_idx=i, page=1, number=n, text=f"x = {n}")
                            for i, n in enumerate(("2", "3", "7", "8", "9"))])
    assert determinations(holes)["NUMBERING_GAP"] == "NOT_INVESTIGATED"
    assert not [o for o in observe(holes) if o.check == "NUMBERING_GAP"]
    gapped = _doc_fixture(tables=[
        Table(table_idx=0, page=1, caption="Table 1: a", rows=[["m", "1"]]),
        Table(table_idx=1, page=1, caption="Table 3: b", rows=[["m", "2"]])])
    gaps = [o for o in observe(gapped) if o.check == "NUMBERING_GAP"]
    assert len(gaps) == 1 and gaps[0].about == "EXTRACTION" and "2" in gaps[0].detail

    unnumbered = _doc_fixture(sections=[Section(section_idx=0, title="EXPERIMENTS", page_start=1,
                                        text="As shown in Section 4.2 the gain holds.")])
    assert determinations(unnumbered)["SECTION_REF_UNRESOLVED"] == "NOT_INVESTIGATED"

    twice = _doc_fixture(figures=[
        Figure(figure_idx=0, page=1, label="Figure 3", caption="Figure 3: x"),
        Figure(figure_idx=1, page=1, label="Figure 3", caption="Figure 3: x")])
    dup = [o for o in observe(twice) if o.check == "LABEL_DUPLICATED"]
    assert len(dup) == 1 and not [o for o in observe(twice) if o.check == "CAPTION_LABEL_CONFLICT"]
    clash = _doc_fixture(tables=[Table(table_idx=0, page=1, caption="Table 10: ablation",
                               rows=[["m", "1"]]),
                         Table(table_idx=1, page=1, caption="Table 10: , using the teacher",
                               rows=[["m", "2"]])])
    conflict = [o for o in observe(clash) if o.check == "CAPTION_LABEL_CONFLICT"]
    assert len(conflict) == 1 and not [o for o in observe(clash) if o.check == "LABEL_DUPLICATED"]

    unproven = _doc_fixture(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                                  header=["Method", "A", "B", "Avg."],
                                  rows=[["m1", "10", "20", "99"],
                                        ["m2", "30", "40", "98"]])])
    assert determinations(unproven)["TABLE_ARITHMETIC"] == "NOT_INVESTIGATED"
    proven = _doc_fixture(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                                header=["Method", "A", "B", "Avg."],
                                rows=[["m1", "10", "20", "15.0"],
                                      ["m2", "30", "40", "99.0"]])])
    arith = [o for o in observe(proven) if o.check == "TABLE_ARITHMETIC"]
    assert len(arith) == 1 and arith[0].about == "PAPER" and arith[0].ref == "T0:r1:c3"
    assert locate.resolve(proven, arith[0].ref, arith[0].quote).resolved
    rounded = _doc_fixture(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                                 header=["Method", "A", "B", "C", "D", "Avg."],
                                 rows=[["m1", "10", "20", "30", "40", "25.0"],
                                       ["m2", "55.6", "79.3", "46.9", "49.9", "57.9"]])])
    assert not [o for o in observe(rounded) if o.check == "TABLE_ARITHMETIC"], (
        "57.925 printed as 57.9 is a correctly rounded average, not an arithmetic error")

    flood = _doc_fixture(sections=[Section(
        section_idx=0, title="1. Results", page_start=1,
        text=" ".join(f"Table {n} reports another split of the same benchmark."
                      for n in range(1, 60)))],
        tables=[Table(table_idx=0, page=1, caption="Table 1: the only one recovered",
                      rows=[["method", "1.0"]])])
    got = observe(flood)
    assert len(got) <= _MAX_OBSERVATIONS
    assert sum(1 for o in got if o.check == "CROSSREF_UNRESOLVED") <= _MAX_PER_CHECK
    assert any("not listed" in o.detail for o in got)

    for doc in (cited, gapped, twice, clash, proven, flood):
        for o in observe(doc):
            assert o.check in INTEGRITY_CHECKS and o.about in INTEGRITY_ABOUT
            assert o.detail
            assert not getattr(o, "severity", None)
            if o.about == "PAPER":
                assert o.check in CLAIMABLE_ABOUT_THE_PAPER
            assert not o.ref or locate.resolve(doc, o.ref, o.quote).resolved
        counts = summarise(observe(doc))
        assert sum(counts.values()) == len(observe(doc))
        lines = scope_lines(observe(doc))
        assert len(lines) == 2 and "counts toward any threshold" in lines[1]
    assert scope_lines(()) == ()

    ordered = _doc_fixture(tables=[
        Table(table_idx=0, page=1, caption="Table 4: later", rows=[["m", "1"]]),
        Table(table_idx=1, page=2, caption="Table 2: earlier", rows=[["m", "2"]])])
    uncited = _doc_fixture(
        sections=[Section(section_idx=0, title="1. Results", page_start=1,
                          text="The comparison in Table 1 is the headline result.")],
        tables=[Table(table_idx=0, page=1, caption="Table 1: headline", rows=[["m", "1"]]),
                Table(table_idx=1, page=9, caption="Table 2: never mentioned",
                      rows=[["m", "2"]])])
    uncaptioned = _doc_fixture(tables=[Table(table_idx=0, page=1, caption="", rows=[["m", "1"]])])
    numbered = _doc_fixture(sections=[
        Section(section_idx=0, title="4. Experiments", page_start=1,
                text="The derivation is in Section 9.7 of this paper."),
        Section(section_idx=1, title="4.1. Setup", page_start=1, text="We use CIFAR.")])
    reachable = {o.check for doc in (cited, gapped, twice, clash, proven, ordered,
                                     uncited, uncaptioned, numbered)
                 for o in observe(doc)}
    unreachable = set(INTEGRITY_CHECKS) - reachable - set(NOT_COMPUTED)
    assert not unreachable, f"declared but produced by nothing: {sorted(unreachable)}"
    assert len(INTEGRITY_NOT_ATTEMPTED) == 3
    print("docintegrity self-check ok")


def _self_check_selfaudit() -> None:
    from .schema import Grade, SubstantiveVerdict as _SV

    def _counted(f):
        return f.counted_severity or f.severity

    good = Finding(
        finding_id="a-01", lens="contradiction", severity="MAJOR", title="t",
        statement="s", evidence_quote="q", evidence_ref="T1:r0:c0",
        evidence_class="cell_verified", verified_observation="checked",
        verification_state="complete", calc_class="recomputed_ok",
        evidence_origin="PAPER_TABLE", candidate_class="CONFIRMED_FINDING",
        confidence="HIGH", severity_rationale="because the headline claim rests on it",
        steelman="the authors could reasonably say the split was disclosed",
        discrepancy_type="ARITHMETIC_ERROR",
        grade=Grade(verdict="CONFIRMED", severity="MAJOR", confidence="HIGH",
                    reached_independently=True))
    clean = self_audit(EvalReport(paper_id="p", findings=[good],
                                 substantive_verdict=_SV(verdict="STRONG")), _counted)
    assert clean.complete and not clean.failed, clean.summary

    for key, mutate in [
            ("serious_findings_verified", {"verified_observation": ""}),
            ("serious_findings_cell_backed", {"evidence_class": "prose_verified"}),
            ("numbers_independently_recomputed", {"calc_class": "not_attempted"}),
            ("alternative_interpretation_tested", {"verification_state": "legacy"}),
            ("authors_steelmanned", {"steelman": ""}),
            ("confidence_separate_from_severity", {"confidence": ""}),
            ("severity_argued_by_impact", {"severity_rationale": ""}),
            ("evidence_provenance_recorded", {"evidence_origin": ""}),
            ("graded_by_a_second_reader", {"grade": None}),
            ("previous_conclusions_not_inherited",
             {"grade": Grade(verdict="CONFIRMED", reached_independently=False)}),
    ]:
        res = self_audit(EvalReport(paper_id="p", findings=[good.model_copy(update=mutate)],
                                    substantive_verdict=_SV(verdict="STRONG")), _counted)
        assert key in res.failed, (key, res.failed)

    unsorted = self_audit(EvalReport(
        paper_id="p", findings=[good.model_copy(update={"candidate_class": "",
                                                        "finding_class": "UNGRADED"})],
        substantive_verdict=_SV(verdict="STRONG")), _counted)
    assert "questions_separated_from_findings" in unsorted.failed

    no_whole = self_audit(EvalReport(paper_id="p", findings=[good]), _counted)
    assert "whole_paper_judged_independently" in no_whole.failed

    minor = good.model_copy(update={"severity": "MINOR", "discrepancy_type": "NOT_APPLICABLE"})
    empty = self_audit(EvalReport(paper_id="p", findings=[minor],
                                  substantive_verdict=_SV(verdict="STRONG")), _counted)
    assert empty.complete, empty.summary
    assert any(i.state == "not_applicable" for i in empty.items)
    capped = good.model_copy(update={"severity": "MAJOR", "counted_severity": "MINOR",
                                     "steelman": "", "discrepancy_type": "NOT_APPLICABLE"})
    assert "authors_steelmanned" not in self_audit(
        EvalReport(paper_id="p", findings=[capped],
                  substantive_verdict=_SV(verdict="STRONG")), _counted).failed
    print("selfaudit self-check ok")


def _self_check_ledger() -> None:
    ts = TargetSet(
        paper_id="p",
        questions=[ReviewQuestion(question_id="Q1", resolved_without_execution=True),
                   ReviewQuestion(question_id="Q2")],
        objects=[
            DiscoveredObject(target_id="A", claim_text="a printed accuracy",
                             harness_addressable=True, priority_reason="centrality=CENTRAL"),
            DiscoveredObject(target_id="B", claim_text="a composition",
                             harness_addressable=True, question_id="Q1"),
            DiscoveredObject(target_id="C", claim_text="an unbuildable ablation")],
        plans=[PlanDecision(target_id="A", action="AUTHOR_CODE_REPRODUCTION",
                            route="AUTHOR_CODE_EXECUTION", requires_execution=True),
               PlanDecision(target_id="B", action="PAPER_ONLY_RESOLUTION",
                            route="ARITHMETIC_RECHECK"),
               PlanDecision(target_id="C", action="INFEASIBLE_SPECIFICATION",
                            blocking_gate="specification_complete")],
        outcomes=[
            TargetOutcome(target_id="A", disposition="FAILED_REPRODUCTION",
                          provenance="repo_exec", route="AUTHOR_CODE_EXECUTION",
                          reason="61.4 was printed; 52.0 was measured",
                          reconciliation=Reconciliation(reproduced_value=52.0,
                                                        claimed_raw="61.4", noise_band=0.4,
                                                        seeds_run=[0, 1, 2])),
            TargetOutcome(target_id="B", disposition="PAPER_ARITHMETIC_CONTRADICTION",
                          provenance="paper", route="ARITHMETIC_RECHECK",
                          reason="12*3 is not 40"),
            TargetOutcome(target_id="C", disposition="SPECIFICATION_BLOCKED")])
    ts.outcomes[0].launched = 3
    assert ts.outcomes[1].establishes_failure

    led = build_ledger(EvalReport(paper_id="p", title="t", verdict="RED"), ts)
    targets = [e for e in led.entries if e.entry_id.startswith("L")]
    assert [e.target_id for e in targets] == ["A", "B", "C"]
    a = targets[0]
    assert "52.0" in a.observed and "authors' own checkout" in a.admissibility
    assert a.evidence_state == "REPRODUCTION_FAILURE" and a.concerns_the_paper
    assert a.resolution_state == "RESOLVED_BY_EXECUTION" and a.launched == 3
    b = targets[1]
    assert "material failure" in b.implication and "no model judgement" in b.implication
    assert b.evidence_state == "PAPER_INTERNAL_EVIDENCE" and b.concerns_the_paper
    c = targets[2]
    assert "not a defect in the result" in c.implication
    assert not c.concerns_the_paper and c.resolution_state == "UNRESOLVED"

    qs = [e for e in led.entries if e.entry_id.startswith("Q")]
    assert [e.question for e in qs] == ["Q1", "Q2"]

    e = led.efficiency
    assert e["questions_generated"] == 2 and e["questions_resolved_without_execution"] == 1
    assert e["targets_discovered"] == 3 and e["targets_addressable"] == 2
    assert e["targets_requiring_execution"] == 1 and e["executions_completed"] == 1
    assert e["blocked_by_gate"] == {"specification_complete": 1}
    assert e["targets_settled"] == 2
    assert e["targets_resolved_without_execution"] == 1
    assert e["targets_launched"] == 1 and e["processes_launched"] == 3
    assert e["targets_resolved"] == 2
    assert "probe_stage_seconds" in e and "execution_seconds" not in e

    ts.outcomes[0].provenance = "synthesized"
    led2 = build_ledger(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts)
    syn = [x for x in led2.entries if x.target_id == "A"][0]
    assert "NOT admissible" in syn.admissibility
    assert led2.efficiency["executions_completed"] == 1
    assert syn.evidence_state == "INCONCLUSIVE_EXECUTION" and not syn.concerns_the_paper
    assert led2.efficiency["targets_resolved"] == 1
    print("ledger self-check ok")


def _self_check_render_caps() -> None:
    """Invariant 19, exercised on a synthetic paper deliberately over EVERY cap at once —
    not merely asserting the constants exist, but confirming the RENDERED text is actually
    bounded by them."""
    # --- render_eval_report: MAX_TABLE_ROWS, MAX_THREAT_BULLETS, MAX_CODE_ROWS -----------
    many_findings = [_f(f"m{i}", "protocol", "MAJOR" if i < 40 else "MINOR",
                        ref=f"T{i}:r0:c0")
                     for i in range(200)]
    assert len(many_findings) > MAX_TABLE_ROWS and len(many_findings) > MAX_THREAT_BULLETS
    eval_rep = EvalReport(paper_id="cap-test", title="T", verdict="GREEN",
                          verdict_reason="none established", findings=many_findings,
                          lenses_run=["protocol"])
    eval_md = render_eval_report(eval_rep)
    assert eval_md.count("further FATAL/MAJOR finding(s)") == 1, (
        "MAX_THREAT_BULLETS must trigger and say so exactly once")
    assert "further finding(s) omitted for length" in eval_md, (
        "MAX_TABLE_ROWS must trigger and say so")
    assert eval_md.count("| MAJOR |") + eval_md.count("| MINOR |") <= MAX_TABLE_ROWS + 5

    many_code_hits = [CodeAuditFinding(finding_id=f"c{i}", rule_id="leak-unseeded-split",
                                       category="data_leakage", severity="MINOR",
                                       title="t", statement="s", file=f"f{i}.py", line=i)
                      for i in range(30)]
    assert len(many_code_hits) > MAX_CODE_ROWS
    code_block = "\n".join(_code_audit_block(CodeAudit(
        repo_path="r", files_scanned=30, lines_scanned=900, findings=many_code_hits)))
    assert "further pattern(s)" in code_block, "MAX_CODE_ROWS must trigger and say so"
    assert sum(1 for ln in code_block.splitlines() if ln.startswith("- **[")) == MAX_CODE_ROWS

    # --- render_reviewer_report: the remaining eight caps --------------------------------
    objects, outcomes, questions_, sfs = [], [], [], []
    for i in range(20):
        tid = f"F{i}"
        objects.append(DiscoveredObject(target_id=tid, claim_text=f"claim {i}",
                                        centrality="CENTRAL", harness_addressable=True,
                                        materiality_basis="NONE"))
        outcomes.append(TargetOutcome(target_id=tid, disposition="PAPER_ARITHMETIC_CONTRADICTION",
                                      provenance="paper", reason=f"arithmetic {i} fails"))
    for i in range(10):
        tid = f"Y{i}"
        objects.append(DiscoveredObject(target_id=tid, claim_text=f"unsettled {i}",
                                        centrality="CENTRAL", harness_addressable=True))
        outcomes.append(TargetOutcome(target_id=tid, disposition="INCONCLUSIVE",
                                      provenance="repo_exec", reason=f"noise {i}"))
    for i in range(10):
        tid = f"H{i}"
        outcomes.append(TargetOutcome(target_id=tid, disposition="REPRODUCED",
                                      provenance="repo_exec", reason=f"held {i}"))
    for i in range(10):
        tid = f"R{i}"
        outcomes.append(TargetOutcome(
            target_id=tid, disposition="ARTIFACT_FACT_ESTABLISHED", reason=f"fact {i}"))
    for i in range(10):
        tid = f"X{i}"
        outcomes.append(TargetOutcome(target_id=tid, disposition="REPRODUCED",
                                      provenance="repo_exec", launched=1,
                                      action="AUTHOR_CODE_REPRODUCTION", reason=f"ran {i}"))
    for i in range(10):
        objects.append(DiscoveredObject(target_id=f"U{i}", claim_text=f"unchecked {i}",
                                        centrality="CENTRAL", harness_addressable=True))
    for i in range(10):
        questions_.append(ReviewQuestion(question_id=f"Q{i}", question=f"open question {i}?",
                                         resolution_status="UNRESOLVED"))
    for cat_i, cat in enumerate(CATEGORY_ORDER[:5]):
        for j in range(5):
            sfs.append(ScientificFinding(finding_id=f"sf-{cat_i}-{j}", scientific_class=cat,
                                         title=f"{cat} finding {j}", severity="MAJOR",
                                         resolution_status="UNRESOLVED"))
    assert len(sfs) > _MAX_FINDINGS_SHOWN
    assert len(by_category(sfs)[CATEGORY_ORDER[0]]) > _MAX_PER_CATEGORY

    cap_ts = TargetSet(paper_id="cap-test", objects=objects, outcomes=outcomes,
                       questions=questions_)
    cap_rep = EvalReport(paper_id="cap-test", title="T", verdict="GREEN",
                         verdict_reason="see established failures", triage="RED",
                         triage_reason="material failures established",
                         claim_status="VERIFIED_FAILURE", scientific_findings=sfs,
                         lenses_run=["protocol"],
                         targets_summary=_targets_summary(outcomes))
    review_md = render_reviewer_report(cap_rep, cap_ts)
    sections = dict(_split_sections(review_md))

    established_body = sections.get("## Established failures", "")
    assert established_body.count("**Paper-internal arithmetic contradiction") == _MAX_RED, (
        "_MAX_RED must bound how many established failures are printed")

    stalled_body = sections.get(
        "## Central claims an experiment ran for and did not settle", "")
    assert stalled_body.count("- **Y") == _MAX_YELLOW, "_MAX_YELLOW must bound this section"

    held_body = sections.get("## What held up", "")
    assert held_body.count("- **H") == _MAX_HELD
    assert "more; see `discovery/targets.json`." in held_body, (
        "_MAX_HELD must trigger and say so")

    reading_body = sections.get("## What reading the artifact established", "")
    assert reading_body.count("- **R") == _MAX_READING
    assert "more; see the ledger." in reading_body, "_MAX_READING must trigger and say so"

    triggered_body = sections.get("## Experiments triggered", "")
    assert triggered_body.count("- **X") == _MAX_TRIGGERED
    assert "further target(s) pursued; see" in triggered_body, (
        "_MAX_TRIGGERED must trigger and say so")

    open_body = sections.get("## Open questions for the reviewer", "")
    assert open_body.count("- **open question") == _MAX_OPEN
    assert "more; see `discovery/targets.json`." in open_body, (
        "_MAX_OPEN must trigger (open questions) and say so")

    unchecked_body = sections.get("## Central claims this review did not check", "")
    assert unchecked_body.count("- **U") == _MAX_OPEN
    assert "more; see `discovery/targets.json`." in unchecked_body, (
        "_MAX_OPEN must trigger (unchecked central) and say so")

    scientific_body = sections.get("## Scientific findings", "")
    top_level_bullets = sum(1 for ln in scientific_body.splitlines()
                            if ln.startswith("- **"))
    assert top_level_bullets == _MAX_FINDINGS_SHOWN, (
        "_MAX_FINDINGS_SHOWN must bound the total shown across all categories",
        top_level_bullets)
    first_category = by_category(sfs)[CATEGORY_ORDER[0]]
    assert len(first_category) > _MAX_PER_CATEGORY, "the fixture must over-fill one category"
    assert "further finding(s) across these categories" in scientific_body, (
        "the elided count must be stated, not silently dropped")

    assert len(review_md) < 40000, (
        "a synthetic worst case must still be a bounded document, not merely capped in "
        "theory")
    print("render-caps self-check ok (twelve distinct caps confirmed present and triggering)")


if __name__ == "__main__":
    _self_check_decision()
    _self_check_outcome()
    _self_check_guarantees()
    _self_check_coverage()
    _self_check_docintegrity()
    _self_check_selfaudit()
    _self_check_ledger()
    _self_check_render_caps()
    print("harness.report self-check ok")
