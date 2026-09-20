"""What may be concluded: materiality, disposition, planning, priority, route exhaustion.

Consolidates the reference implementation's `harness/{disposition,planner,priority,
materiality,exhaustion,comparison}.py` (tag `reference-implementation-2026-09-20`) into
one file. Function bodies are unchanged; docstrings are cut to what a reader needs at the
call site — see `docs/INVARIANT_MAP.md` for the corpus incidents that shaped each rule.

`harness/taxonomy.py`, `harness/provenance.py` and `harness/isolation.py` stay SEPARATE
files rather than joining this one: all three are pure vocabulary/predicate modules with
NO dependency on `harness.schema`, and merging them here would make this module need
`schema` for its planner/materiality functions while schema-adjacent modules need this
module's vocabulary constants — a real circular import, not a style choice.

`python -m harness.decide` runs the self-check.
"""
from __future__ import annotations

import re

from .provenance import admits
from .schema import (
    ClaimRef, Comparison, DiscoveredObject, PaperDoc, PlanDecision, RouteAttempt,
    Section, TargetSet, TargetOutcome,
    COMPARISON_KINDS, COMPARISON_STATES, DISPOSITION_BASIS, EXPERIMENT_NECESSITY,
    MATERIALITY_BASES, NECESSITY_FOR_ACTION, PAPER_DISPOSITIONS, PLAN_ACTIONS,
    ROUTE_ATTEMPT_STATES, VERIFICATION_ROUTES,
)

# =============================================================================
# DISPOSITION — the one action a caller takes on a finished paper
# =============================================================================
# Precedence, strongest fact first: (1) the review did not finish, (2) a material failure
# was established, (3) a CENTRAL question could not be checked at all, (4) one was checked
# and stayed open, (5) a concern was verified but rejects nothing, (6) neither of the above.

# ON WHAT a material failure was established. Never a severity, never a colour.
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

# Which blocker dominates when a paper has several. Most-about-the-paper first: a referee
# acts on the paper in front of them, so "the paper does not specify this" is theirs to
# raise while "this host has no GPU" is ours to solve. METHOD is placed LAST — it is this
# review's own gap, closable by nobody but this system being extended.
_BLOCKER_PRECEDENCE: tuple[tuple[str, str], ...] = (
    ("SPECIFICATION", "BLOCKED_SPECIFICATION"),
    ("ARTIFACT", "BLOCKED_ARTIFACT"),
    ("RESOURCE", "BLOCKED_RESOURCES"),
    ("METHOD", "BLOCKED_METHOD"),
)

# Target dispositions that BLOCK a central question, mapped to the blocker class above.
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

# Target dispositions DELIBERATELY absent from the table above, with why. Every value in
# `schema.TARGET_DISPOSITIONS` must appear either here or above — the self-check asserts
# it — so an added disposition that is never classified fails the suite instead of
# silently reading PASS_TO_HUMAN_CLEAN on a central target.
DELIBERATELY_UNCLASSIFIED: dict[str, str] = {
    "AUTHORIZATION_BLOCKED":
        "a gate in THIS RUN refused by configuration, not by an absence of method — turning "
        "the gate on can resolve it with no change to this review's method inventory.",
    "REPRODUCED": "the target settled; there is nothing blocked about it.",
    "FAILED_REPRODUCTION": "the target settled, against the paper.",
    "PAPER_ARITHMETIC_CONTRADICTION": "as FAILED_REPRODUCTION, by the paper-internal route.",
    "PAPER_ONLY_RESOLVED": "the target settled from the paper's own printed content.",
    "ARTIFACT_FACT_ESTABLISHED": "settled, about the ARTIFACT rather than the paper.",
    "ARTIFACT_MISMATCH_ESTABLISHED":
        "the paper and the pinned artifact disagree, with identity ESTABLISHED. Not a "
        "blocker and not a material failure: which configuration produced the reported "
        "number is a question for execution.",
    "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS":
        "both locations verified; the correspondence between them is the auditor's "
        "reading. The question stays open and is counted as open, not blocked.",
    "ARTIFACT_INSPECTION_INCONCLUSIVE":
        "the artifact route completed and settled no question. Reading the code is a "
        "route that ran, not a missing one.",
    "INCONCLUSIVE": "pursued and settled nothing. Handled by `unresolved_central`, which "
                    "additionally requires an ADMISSIBLE provenance.",
    "NOT_ATTEMPTED": "no experiment was judged necessary, or none was started this run.",
    "PENDING": "the probe phase did not complete for this target.",
    "BUDGET_DEFERRED": "this run's per-paper target budget did not reach it — ours, not "
                       "a fact about the paper.",
    "SUPERSEDED_BY_ESTABLISHED_FAILURE":
        "the paper already stopped on an established material failure.",
    "CITATION_VERIFIED_ONLY":
        "the quotation behind a concern was re-verified, settling nothing about whether "
        "the concern is right. Its question stays open, not blocked.",
    "NO_EXPERIMENT_NEEDED": "declining to run something is a successful review outcome on "
                            "the necessity axis and resolves nothing on the evidence axis.",
}

PAPER_DISPOSITIONS_ = PAPER_DISPOSITIONS  # re-exported for callers importing from here

_REASON = {
    "STOP_MATERIAL_FAILURE":
        "a material failure was established, so there is nothing further to check: the "
        "paper's central claim does not stand on the evidence examined.",
    "BLOCKED_SPECIFICATION":
        "a central claim could not be checked because the paper does not specify enough "
        "to build the experiment that would settle it.",
    "BLOCKED_ARTIFACT":
        "a central claim could not be checked because no released code reached it, or "
        "this review could not establish which part of it produces the cited quantity.",
    "BLOCKED_RESOURCES":
        "a central claim could not be checked because the experiment it needs cannot be "
        "hosted from here — a fact about this runner, never about the paper.",
    "BLOCKED_METHOD":
        "a central claim could not be checked because this review has no verification "
        "approach for it. A limit of this system's own method inventory, never a finding "
        "about the paper, and never to be read as the paper having been checked and "
        "found clean: it was not checked at all.",
    "PASS_TO_HUMAN_UNRESOLVED":
        "a central claim was pursued with evidence entitled to settle it and remained "
        "open. The question is handed over as a question.",
    "PASS_TO_HUMAN_CONCERNS":
        "no material failure was established. Verified concerns were recorded, and a "
        "concern weakens a claim rather than rejecting one.",
    "PASS_TO_HUMAN_CLEAN":
        "no material failure, no unresolved central claim and no verified major concern "
        "within the audited scope. Not a certificate of correctness.",
    "NOT_REVIEWED":
        "the review did not reach a report, so no disposition about the paper is "
        "available.",
}

_ESTABLISHED_NON_MATERIAL_REASON = (
    "no material failure was established, and this review did establish a defect: it sits "
    "on a target no central scientific claim was established to depend on, so it weakens "
    "the paper rather than rejecting it. It is handed over for a human to weigh, and it is "
    "reported in full under `## Established failures`. This is NOT a clean paper.")


def disposition_basis_for(*, failed_target_provenance: str = "",
                          paper_arithmetic_failed: bool = False) -> str:
    """WHICH evidence carried a material failure. Only deterministic arithmetic or an
    admissible execution may — model-assigned severity is deliberately absent."""
    if admits(failed_target_provenance):
        return ("AUTHOR_CODE_REPRODUCTION" if failed_target_provenance == "repo_exec"
                else "INDEPENDENT_REIMPLEMENTATION")
    if paper_arithmetic_failed:
        return "PAPER_ARITHMETIC"
    return "NONE"


def blockers_from(dispositions, centralities) -> tuple[str, ...]:
    """The blocker classes raised by CENTRAL targets only, in precedence order. A
    SUPPORTING target that was blocked belongs in the scope section, not here."""
    raised = set()
    for disp, centrality in zip(dispositions, centralities):
        if (centrality or "").strip().upper() != "CENTRAL":
            continue
        cls = BLOCKER_FOR_DISPOSITION.get((disp or "").strip().upper())
        if cls:
            raised.add(cls)
    return tuple(cls for cls, _ in _BLOCKER_PRECEDENCE if cls in raised)


def derive_disposition(*, review_complete: bool = True, claim_status: str = "NOT_VERIFIED",
                       basis: str = "NONE", central_blockers: tuple[str, ...] = (),
                       central_unresolved: int = 0, counted_major: int = 0,
                       established_non_material: int = 0) -> tuple[str, str, str]:
    """(disposition, basis, reason). Total over its inputs and deterministic.

    Precedence: PROVEN (an established defect) outranks OPEN (unresolved central claim)
    outranks ASSERTED (a counted MAJOR), and every BLOCKED_* outranks all three — see
    `docs/INVARIANT_MAP.md` for the two shipped-corpus defects this ordering fixes
    (a central question with no admissible route reading PASS_TO_HUMAN_CLEAN; a proven
    arithmetic contradiction on a non-central target reading CLEAN because nothing else
    matched).
    """
    if not review_complete:
        return "NOT_REVIEWED", "NONE", _REASON["NOT_REVIEWED"]

    if (claim_status or "").strip().upper() == "VERIFIED_FAILURE":
        why = _BASIS_REASON.get(basis, "")
        return ("STOP_MATERIAL_FAILURE",
                basis if basis in DISPOSITION_BASIS else "NONE",
                _REASON["STOP_MATERIAL_FAILURE"] + (f" Established by: {why}." if why else ""))

    for cls, disposition in _BLOCKER_PRECEDENCE:
        if cls in central_blockers:
            return disposition, "NONE", _REASON[disposition]

    if established_non_material > 0:
        return "PASS_TO_HUMAN_CONCERNS", "NONE", _ESTABLISHED_NON_MATERIAL_REASON
    if central_unresolved > 0:
        return "PASS_TO_HUMAN_UNRESOLVED", "NONE", _REASON["PASS_TO_HUMAN_UNRESOLVED"]
    if counted_major > 0:
        return "PASS_TO_HUMAN_CONCERNS", "NONE", _REASON["PASS_TO_HUMAN_CONCERNS"]
    return "PASS_TO_HUMAN_CLEAN", "NONE", _REASON["PASS_TO_HUMAN_CLEAN"]


def stops_the_paper(disposition: str = "") -> bool:
    return (disposition or "") == "STOP_MATERIAL_FAILURE"


def passes_to_human(disposition: str = "") -> bool:
    """Every disposition except a stop and a non-review does — including BLOCKED, which
    is a limit of this review rather than a finding about the paper."""
    return (disposition or "") in (
        "BLOCKED_SPECIFICATION", "BLOCKED_ARTIFACT", "BLOCKED_RESOURCES", "BLOCKED_METHOD",
        "PASS_TO_HUMAN_UNRESOLVED", "PASS_TO_HUMAN_CONCERNS", "PASS_TO_HUMAN_CLEAN")


# =============================================================================
# PLANNER — whether a target is worth an experiment, and which kind, deterministically
# =============================================================================
# This module decides whether to TRY. `execute.authorize` decides whether it may RUN.
# Identity, capability, commit verification and resource sufficiency all live there —
# only `authorize()` may permit repository execution — and this module never duplicates
# any of it.

# Which refusal each named addressing blocker earns.
_ADDRESSING_REFUSAL = {
    "ADDRESS_UNRESOLVED": ("INFEASIBLE_ADDRESSING",
        "this review could not build an address in the parsed paper that it can re-derive "
        "for this claim. A limit of what extraction recovered, not a finding about the paper."),
    "QUANTITY_UNPARSED": ("INFEASIBLE_REPORTING",
        "the claim is addressed, and the paper prints no single unambiguous quantity at "
        "that address for a reproduction to be compared against. A limit of reporting, "
        "not of extraction and not a finding about the paper."),
    "NO_ROUTE": ("INFEASIBLE_ROUTE",
        "the claim is addressed and quantified, and no verification route this system has "
        "would settle the question it raises. A limit of this review's method inventory."),
}

# Routes that CLOSE a question without anything running. ONE route: `PAPER_INTERNAL_CHECK`
# was here once and should not have been — it re-verifies a QUOTATION and settles nothing,
# so having it here let a citation check suppress an experiment 100% of the time over the
# shipped corpus. It is still taken when nothing better exists (`_CITATION_ONLY`), and no
# longer outranks anything.
_RESOLVING = ("ARITHMETIC_RECHECK",)
_CITATION_ONLY = ("PAPER_INTERNAL_CHECK",)

_ACTION_FOR_ROUTE = {
    "AUTHOR_CODE_EXECUTION": "AUTHOR_CODE_REPRODUCTION",
    "INDEPENDENT_RECONSTRUCTION": "INDEPENDENT_RECONSTRUCTION",
}
# Most decisive first — the authors' own code is always preferred over a reconstruction.
_EXECUTABLE_ORDER = ("AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION")
_WORTH_PURSUING = ("CENTRAL", "SUPPORTING")


def _necessity(action: str) -> str:
    return NECESSITY_FOR_ACTION.get(action, "NO_EXPERIMENT_NEEDED")


def route_that_would_be_taken(routes: tuple[str, ...] = ()) -> str:
    """The route `plan()` would choose for an object offering `routes`. Pure, no object —
    exists because scoring on `routes[0]` (the first route discovery happened to append)
    disagreed systematically with what the planner actually chooses."""
    rs = tuple(routes or ())
    for r in _RESOLVING:
        if r in rs:
            return r
    for r in _EXECUTABLE_ORDER:
        if r in rs:
            return r
    for r in _CITATION_ONLY:
        if r in rs:
            return r
    return rs[0] if rs else "NONE"


def current_plans(plans: list[PlanDecision]) -> list[PlanDecision]:
    """The plan CURRENTLY IN FORCE for each target — the LAST entry for a repeated
    `target_id`. Read this, never a raw `TargetSet.plans` list, for anything that COUNTS:
    a re-plan appends a second `PlanDecision` rather than replacing the first, so a naive
    sum over the raw list double-counts a target that was re-planned."""
    by_id: dict[str, PlanDecision] = {}
    for p in plans:
        by_id[p.target_id] = p
    return list(by_id.values())


def classify_plan(*, centrality: str, addressable: bool, route: str,
                  artifact_available: bool, specification_complete: bool,
                  environment_state: str, addressing_blocker: str = "NONE",
                  investigation_open: bool = True) -> tuple[str, str, dict, str]:
    """(action, reason, gates, blocking_gate) for ONE route on ONE target. Vocabulary
    strings and booleans only — no count, number, metric name or paper identity, so a
    paper-specific rule cannot be written here even by accident."""
    gates = {
        "investigation_open": bool(investigation_open),
        "worth_pursuing": centrality in _WORTH_PURSUING,
        "structurally_addressable": bool(addressable),
        "route_exists": route in _ACTION_FOR_ROUTE,
        "specification_complete": bool(specification_complete),
        "artifact_available": bool(artifact_available),
        "environment_usable": environment_state != "blocked",
    }

    if not gates["investigation_open"]:
        return ("SUPERSEDED_BY_ESTABLISHED_FAILURE",
                "a material failure is already established from the paper's own evidence, "
                "so no execution can change what this review concluded. This is a property "
                "of the review's conclusion and says nothing about this target.",
                gates, "investigation_open")

    if not gates["worth_pursuing"]:
        return ("NO_EXPERIMENT_NEEDED",
                f"centrality is {centrality or 'UNASSESSED'}: the paper's conclusion does not "
                f"rest on this, so spending an execution on it would buy nothing.",
                gates, "worth_pursuing")
    if not gates["structurally_addressable"]:
        action, sentence = _ADDRESSING_REFUSAL.get(
            addressing_blocker, _ADDRESSING_REFUSAL["ADDRESS_UNRESOLVED"])
        return (action, sentence, gates, "structurally_addressable")
    if not gates["route_exists"]:
        return ("NO_EXPERIMENT_NEEDED",
                f"no executable route applies to this target (route '{route}'), so it is "
                f"reported as an open question rather than pursued.", gates, "route_exists")

    if route == "AUTHOR_CODE_EXECUTION" and not gates["artifact_available"]:
        return ("INFEASIBLE_ARTIFACT",
                "the authors' code is the only admissible route to this printed quantity and "
                "no repository was found. Nothing about the paper follows from that.",
                gates, "artifact_available")
    if route == "INDEPENDENT_RECONSTRUCTION" and not gates["specification_complete"]:
        return ("INFEASIBLE_SPECIFICATION",
                "the paper does not specify enough to build the experiment that would answer "
                "this, and inventing the missing half would produce a result about our "
                "reconstruction rather than about the paper. Reported unresolved.",
                gates, "specification_complete")
    if not gates["environment_usable"]:
        return ("INFEASIBLE_ENVIRONMENT",
                "the experiment cannot be set up on this host. A failed environment is a "
                "fact about this machine and never a failed reproduction.",
                gates, "environment_usable")

    action = _ACTION_FOR_ROUTE[route]
    return (action, f"an experiment is justified and a {route.lower().replace('_', ' ')} route "
                    f"exists; authorization is decided separately by `execute.authorize`.",
            gates, "")


def _classify_for(obj: DiscoveredObject, **kw) -> tuple[str, str, dict, str]:
    kw.setdefault("addressing_blocker", obj.addressing_blocker)
    action, reason, gates, blocking = classify_plan(**kw)
    if blocking == "structurally_addressable" and obj.evidence_requirements:
        reason = f"{reason} What is missing: {'; '.join(obj.evidence_requirements)}."
    return action, reason, gates, blocking


def _why_material(obj: DiscoveredObject) -> str:
    return {
        "CENTRAL": "the paper's headline conclusion rests on this quantity",
        "SUPPORTING": "this quantity supports the paper's conclusion without carrying it",
        "PERIPHERAL": "the paper's conclusion does not rest on this",
    }.get(obj.centrality, "the paper's structure does not say what rests on this")


def _cheaper_routes_ruled_out(obj: DiscoveredObject) -> tuple[str, str]:
    paper_only = (
        "" if any(r in obj.routes for r in _RESOLVING) else
        "the question is not a check against the paper's own printed content: no arithmetic "
        "recheck applies, so nothing in the paper settles it.")
    inspection = (
        "reading the released code can raise a concern about how a number was produced and "
        "cannot establish what the number is, so it cannot settle whether the printed value "
        "is reproducible"
        if "ARTIFACT_INSPECTION" in obj.routes else
        "no artifact-inspection route applies to this target either")
    return paper_only, inspection


def plan(obj: DiscoveredObject, *, artifact_available: bool = False,
        specification_complete: bool = False, environment_state: str = "unassessed",
        investigation_open: bool = True, author_code_exhausted: bool = False,
        attempt: int = 1) -> PlanDecision:
    """The decision for one discovered object: resolve it cheaply, escalate, or refuse.

    Order: (1) a route that can close the question with nothing running -> take it,
    (2) otherwise the most decisive executable route -> gate it, (3) otherwise -> no
    experiment. `author_code_exhausted` removes AUTHOR_CODE_EXECUTION from consideration
    for THIS call only, for the re-plan primitive after identity resolution failed
    against a real checkout — `obj.routes` itself is never mutated.
    """
    why_material = _why_material(obj)
    paper_only_no, inspection_no = _cheaper_routes_ruled_out(obj)
    executable_order = tuple(
        r for r in _EXECUTABLE_ORDER
        if not (author_code_exhausted and r == "AUTHOR_CODE_EXECUTION"))

    if not investigation_open and not any(r in obj.routes for r in _RESOLVING):
        action, reason, gates, blocking = _classify_for(
            obj, centrality=obj.centrality, addressable=bool(obj.harness_addressable),
            route=next((r for r in executable_order if r in obj.routes), "NONE"),
            artifact_available=artifact_available,
            specification_complete=specification_complete,
            environment_state=environment_state, investigation_open=False)
        return PlanDecision(
            target_id=obj.target_id, action=action, route="NONE", reason=reason,
            gates=gates, blocking_gate=blocking, requires_execution=False,
            necessity=_necessity(action), why_material=why_material,
            paper_only_insufficient_because=paper_only_no,
            competing_explanations=list(obj.counter_explanations), attempt=attempt)

    competing = list(obj.counter_explanations)
    cheap = next((r for r in obj.routes if r in _RESOLVING), "")
    if cheap:
        _, _, gates, _ = classify_plan(
            centrality=obj.centrality, addressable=bool(obj.harness_addressable),
            route=cheap, artifact_available=artifact_available,
            specification_complete=specification_complete,
            environment_state=environment_state, addressing_blocker=obj.addressing_blocker,
            investigation_open=investigation_open)
        gates["resolvable_without_execution"] = True
        return PlanDecision(
            target_id=obj.target_id, action="PAPER_ONLY_RESOLUTION", route=cheap,
            reason="the paper's own printed composition can be re-evaluated here, so no "
                   "execution is warranted to settle whether it holds.",
            gates=gates, requires_execution=False, necessity="NO_EXPERIMENT_NEEDED",
            why_material=why_material, inspection_insufficient_because=inspection_no,
            competing_explanations=competing, attempt=attempt)

    route = next((r for r in executable_order if r in obj.routes), "")
    if not route:
        from .artifact_evidence import requires_execution
        if ("ARTIFACT_INSPECTION" in obj.routes and artifact_available
                and not requires_execution(obj.question_kind)):
            _, _, gates, _ = classify_plan(
                centrality=obj.centrality, addressable=bool(obj.harness_addressable),
                route="ARTIFACT_INSPECTION", artifact_available=artifact_available,
                specification_complete=specification_complete,
                environment_state=environment_state, addressing_blocker=obj.addressing_blocker,
                investigation_open=investigation_open)
            gates["resolvable_without_execution"] = False
            gates["answer_is_a_measurement"] = False
            return PlanDecision(
                target_id=obj.target_id, action="ARTIFACT_INSPECTION_ONLY",
                route="ARTIFACT_INSPECTION",
                reason="no executable route applies and the authors published code, so "
                       "the pinned checkout is read for what it can establish. Reading the "
                       "artifact cannot establish that a reported result is wrong; it can "
                       "establish what the released code does.",
                gates=gates, requires_execution=False, necessity="NO_EXPERIMENT_NEEDED",
                why_material=why_material, paper_only_insufficient_because=paper_only_no,
                competing_explanations=competing, attempt=attempt)

        cite = next((r for r in obj.routes if r in _CITATION_ONLY), "")
        if cite:
            _, _, gates, _ = classify_plan(
                centrality=obj.centrality, addressable=bool(obj.harness_addressable),
                route=cite, artifact_available=artifact_available,
                specification_complete=specification_complete,
                environment_state=environment_state, addressing_blocker=obj.addressing_blocker,
                investigation_open=investigation_open)
            gates["resolvable_without_execution"] = False
            return PlanDecision(
                target_id=obj.target_id, action="PAPER_ONLY_RESOLUTION", route=cite,
                reason="no executable route applies, so the only thing available is to "
                       "re-verify the quotation behind the concern. That establishes the "
                       "concern cites the paper accurately and settles nothing else.",
                gates=gates, requires_execution=False, necessity="NO_EXPERIMENT_NEEDED",
                why_material=why_material, paper_only_insufficient_because=paper_only_no,
                competing_explanations=competing, attempt=attempt)
        fallback = "ARTIFACT_INSPECTION" if "ARTIFACT_INSPECTION" in obj.routes else "NONE"
        action, reason, gates, blocking = _classify_for(obj,
            centrality=obj.centrality, addressable=bool(obj.harness_addressable),
            route=fallback, artifact_available=artifact_available,
            specification_complete=specification_complete, environment_state=environment_state,
            investigation_open=investigation_open)
        return PlanDecision(target_id=obj.target_id, action=action, route=fallback,
                            reason=reason, gates=gates, blocking_gate=blocking,
                            requires_execution=False, necessity=_necessity(action),
                            why_material=why_material,
                            paper_only_insufficient_because=paper_only_no,
                            competing_explanations=competing, attempt=attempt)

    action, reason, gates, blocking = _classify_for(
        obj, centrality=obj.centrality, addressable=bool(obj.harness_addressable), route=route,
        artifact_available=artifact_available, specification_complete=specification_complete,
        environment_state=environment_state, investigation_open=investigation_open)
    expected = (f"the run reproduces the paper's stated value of {obj.expected_value:g} for "
                f"{obj.metric or 'this quantity'}"
                if obj.expected_value is not None else
                f"the run produces a value for {obj.metric or 'this quantity'} that the "
                f"paper's own statement can be reconciled against")
    return PlanDecision(
        target_id=obj.target_id, action=action, route=route, reason=reason, gates=gates,
        blocking_gate=blocking,
        requires_execution=action in ("AUTHOR_CODE_REPRODUCTION", "INDEPENDENT_RECONSTRUCTION",
                                      "MECHANISM_TEST_ONLY"),
        attempt=attempt, necessity=_necessity(action), why_material=why_material,
        paper_only_insufficient_because=paper_only_no,
        inspection_insufficient_because=inspection_no,
        competing_explanations=competing, expected_observation=expected)


# =============================================================================
# PRIORITY — which target to pursue first, as a function, not a display sort
# =============================================================================
# Lexicographic, and the order of the keys is the policy: centrality > addressability >
# decisiveness > identity > artifact-presence > cheapness (tiebreak only). Cheapness sits
# at the bottom deliberately — putting cost any higher lets the system pick an easy
# peripheral target over a hard central one.

_CENTRALITY = {"CENTRAL": 3, "SUPPORTING": 2, "PERIPHERAL": 1, "UNASSESSED": 0}
_DECISIVENESS = {"PAPER_INTERNAL_CHECK": 3, "ARITHMETIC_RECHECK": 3,
                 "AUTHOR_CODE_EXECUTION": 3, "INDEPENDENT_RECONSTRUCTION": 2,
                 "ARTIFACT_INSPECTION": 1, "NONE": 0}
_CHEAPNESS = {"PAPER_INTERNAL_CHECK": 3, "ARITHMETIC_RECHECK": 3, "ARTIFACT_INSPECTION": 2,
             "AUTHOR_CODE_EXECUTION": 1, "INDEPENDENT_RECONSTRUCTION": 0, "NONE": 0}
_IDENTITY_SCORE = {"established": 3, "ambiguous": 1, "unmapped": 0, "": 0}
_BASE, _FIELDS = 8, 6          # base > any field's max, so the sum is provably lexicographic


def score(*, centrality: str, addressable: bool, cheapest_route: str,
         identity_state: str = "", artifact_available: bool = False) -> tuple[float, str]:
    """(priority, reason). A model may argue a target matters; that reaches here only as
    `centrality`, derived from structure, never from a lens's own opinion."""
    values = [
        _CENTRALITY.get(centrality, 0), 1 if addressable else 0,
        _DECISIVENESS.get(cheapest_route, 0), _IDENTITY_SCORE.get(identity_state, 0),
        1 if artifact_available else 0, _CHEAPNESS.get(cheapest_route, 0),
    ]
    total = 0.0
    for i, v in enumerate(values):
        total += min(v, _BASE - 1) * (_BASE ** (_FIELDS - 1 - i))
    reason = (f"centrality={centrality or 'UNASSESSED'}; "
              f"addressable={'yes' if addressable else 'no'}; route={cheapest_route or 'NONE'}; "
              f"identity={identity_state or 'unmapped'}; "
              f"artifact={'yes' if artifact_available else 'no'}")
    return round(total / (_BASE ** _FIELDS), 6), reason


def order(objects, *, identity_state: str = "", artifact_available: bool = False):
    """Score every discovered object in place, highest priority first. Scores on the
    route the PLANNER would take (`route_that_would_be_taken`), not `routes[0]` — the
    first route discovery happened to append, which ranked every corpus target by a
    route nobody would ever choose."""
    for o in objects:
        o.priority, o.priority_reason = score(
            centrality=o.centrality, addressable=bool(o.harness_addressable),
            cheapest_route=route_that_would_be_taken(tuple(o.routes or ())),
            identity_state=identity_state, artifact_available=artifact_available)
    return sorted(objects, key=lambda o: (-o.priority, o.target_id))


# =============================================================================
# MATERIALITY — is an ESTABLISHED defect material to a CENTRAL scientific claim?
# =============================================================================
# A CONSERVATIVE SUFFICIENT CONDITION, not a materiality model. A target is material when
# it resolves inside the Abstract or Conclusion, or one of those sections explicitly
# cites its unique printed table/figure/equation/Result identity. Everything else is
# NONE — "not machine-established as material", never "does not matter". Under-stopping
# is the accepted direction; over-stopping is not.

_ABSTRACT_HEADING = re.compile(r"^(?:\d+|[A-Z])?\.?\s*abstract\b", re.I)
_CONCLUSION_HEADING = re.compile(
    r"^(?:\d+|[A-Z])?\.?\s*(?:(?:discussion|summary)\s+(?:and|&)\s+)?"
    r"(?:conclusions?|concluding remarks)\b", re.I)
_TARGET_REFS: tuple[tuple[str, re.Pattern[str], str, str, str], ...] = (
    ("table", re.compile(r"^T(\d+):r\d+:c\d+$"), "tables", "table_idx", "label"),
    ("figure", re.compile(r"^F(\d+)$"), "figures", "figure_idx", "label"),
    ("equation", re.compile(r"^E(\d+)$"), "equations", "equation_idx", "number"),
)
_CITATION_WORD = {"table": r"(?:tables?|tabs?\.)", "figure": r"(?:figures?|figs?\.)",
                  "equation": r"(?:equations?|eqs?\.|eqn\.?)", "result": r"results?"}
_RESULT_AT_SENTENCE_START = re.compile(
    r"(?:^|(?<=[.!?])\s+)result\s+(\d+(?:\.\d+)*)\b", re.I)

BASIS_GLOSS: dict[str, str] = {
    "ABSTRACT_CLAIM": "the claim resolves inside the paper's own Abstract",
    "CONCLUSION_CLAIM": "the claim resolves inside the paper's own Conclusion",
    "ABSTRACT_TABLE_REFERENCE": "the paper's Abstract explicitly cites this uniquely labelled table",
    "CONCLUSION_TABLE_REFERENCE": "the paper's Conclusion explicitly cites this uniquely labelled table",
    "ABSTRACT_FIGURE_REFERENCE": "the paper's Abstract explicitly cites this uniquely labelled figure",
    "CONCLUSION_FIGURE_REFERENCE": "the paper's Conclusion explicitly cites this uniquely labelled figure",
    "ABSTRACT_EQUATION_REFERENCE": "the paper's Abstract explicitly cites this uniquely numbered equation",
    "CONCLUSION_EQUATION_REFERENCE": "the paper's Conclusion explicitly cites this uniquely numbered equation",
    "ABSTRACT_RESULT_REFERENCE": "the paper's Abstract explicitly cites this uniquely defined numbered Result",
    "CONCLUSION_RESULT_REFERENCE": "the paper's Conclusion explicitly cites this uniquely defined numbered Result",
    "NONE": "this review did not machine-establish that a central claim depends on this "
           "target — a limit of the materiality model, never a statement it does not matter",
}


def _unique_section_idx(doc, heading: re.Pattern[str]) -> int:
    hits: list[int] = []
    for s in (getattr(doc, "sections", None) or []):
        title = " ".join((getattr(s, "title", "") or "").split())
        if title and heading.match(title):
            hits.append(int(getattr(s, "section_idx", -1)))
    return hits[0] if len(hits) == 1 and hits[0] >= 0 else -1


def abstract_section_idx(doc) -> int:
    """The Abstract's `section_idx`, or -1 when no heading names one uniquely."""
    return _unique_section_idx(doc, _ABSTRACT_HEADING)


def conclusion_section_idx(doc) -> int:
    return _unique_section_idx(doc, _CONCLUSION_HEADING)


def _section(doc, section_idx: int):
    hits = [s for s in (getattr(doc, "sections", None) or [])
            if int(getattr(s, "section_idx", -1)) == section_idx]
    return hits[0] if len(hits) == 1 else None


def _norm(text: str) -> str:
    return " ".join((text or "").replace("­", "").split())


def _label(text: str, kind: str) -> str:
    value = _norm(text)
    if not value:
        return ""
    prefix = re.compile(rf"^(?:{_CITATION_WORD[kind]})\s*\(?\s*", re.I)
    return prefix.sub("", value).strip().strip("()").rstrip(".:").upper()


def _quote_cites(kind: str, label: str, quote: str) -> bool:
    if not label or not _norm(quote):
        return False
    pattern = re.compile(
        rf"\b{_CITATION_WORD[kind]}\s*\(?\s*{re.escape(label)}(?!\w|\.\d)", re.I)
    return bool(pattern.search(_norm(quote)))


def _verified_citation(doc, section_idx: int, kind: str, label: str) -> bool:
    section = _section(doc, section_idx)
    if section is None:
        return False
    text = _norm(getattr(section, "text", "") or "")
    if not text:
        return False
    for xr in (getattr(doc, "crossrefs", None) or []):
        quote = _norm(getattr(xr, "quote", "") or "")
        if (str(getattr(xr, "kind", "") or "").lower() != kind
                or int(getattr(xr, "section_idx", -1)) != section_idx
                or _label(getattr(xr, "number", "") or "", kind) != label
                or not quote or text.count(quote) != 1
                or not _quote_cites(kind, label, quote)):
            continue
        return True
    return False


def _object_identity(doc, ref) -> tuple[str, str] | None:
    address = str(getattr(ref, "ref", "") or "")
    for kind, pattern, collection_name, idx_name, label_name in _TARGET_REFS:
        match = pattern.fullmatch(address)
        if not match:
            continue
        idx = int(match.group(1))
        collection = list(getattr(doc, collection_name, None) or [])
        targets = [o for o in collection if int(getattr(o, idx_name, -1)) == idx]
        if len(targets) != 1:
            return None
        printed = _label(getattr(targets[0], label_name, "") or "", kind)
        if not printed:
            return None
        same_label = [o for o in collection
                      if _label(getattr(o, label_name, "") or "", kind) == printed]
        return (kind, printed) if len(same_label) == 1 else None
    return None


def _result_identity(doc, ref, central_idxs: set[int]) -> tuple[str, str] | None:
    if getattr(ref, "kind", "") not in ("prose_claim", "section_span"):
        return None
    target_idx = int(getattr(ref, "section_idx", -1))
    if target_idx < 0 or target_idx in central_idxs:
        return None
    quote = _norm(getattr(ref, "quote", "") or "")
    match = _RESULT_AT_SENTENCE_START.match(quote)
    section = _section(doc, target_idx)
    if not match or section is None:
        return None
    target_text = _norm(getattr(section, "text", "") or "")
    if not target_text or target_text.count(quote) != 1:
        return None
    label = _label(match.group(1), "result")
    definitions: list[tuple[int, str]] = []
    for s in (getattr(doc, "sections", None) or []):
        idx = int(getattr(s, "section_idx", -1))
        if idx in central_idxs:
            continue
        for found in _RESULT_AT_SENTENCE_START.finditer(_norm(getattr(s, "text", "") or "")):
            found_label = _label(found.group(1), "result")
            if found_label == label:
                definitions.append((idx, found_label))
    return ("result", label) if definitions == [(target_idx, label)] else None


def _central_result_citation(doc, section_idx: int, label: str) -> bool:
    section = _section(doc, section_idx)
    if section is None:
        return False
    text = _norm(getattr(section, "text", "") or "")
    return _quote_cites("result", label, text)


def basis_for_ref(ref, abstract_idx: int, *, doc=None) -> str:
    """The materiality basis for one resolved address. Total, and fails closed."""
    if ref is None:
        return "NONE"
    if not getattr(ref, "resolved", False):
        return "NONE"
    if doc is not None:
        abstract_idx = abstract_section_idx(doc)
    section_idx = int(getattr(ref, "section_idx", -1))
    if abstract_idx >= 0 and section_idx == abstract_idx:
        return "ABSTRACT_CLAIM"
    if doc is None:
        return "NONE"
    conclusion_idx = conclusion_section_idx(doc)
    if conclusion_idx >= 0 and section_idx == conclusion_idx:
        return "CONCLUSION_CLAIM"

    central = (("ABSTRACT", abstract_idx), ("CONCLUSION", conclusion_idx))
    central_idxs = {idx for _, idx in central if idx >= 0}
    identity = _object_identity(doc, ref) or _result_identity(doc, ref, central_idxs)
    if identity is None:
        return "NONE"
    kind, label = identity
    for central_name, idx in central:
        if idx < 0:
            continue
        cited = (_central_result_citation(doc, idx, label) if kind == "result"
                 else _verified_citation(doc, idx, kind, label))
        if cited:
            return f"{central_name}_{kind.upper()}_REFERENCE"
    return "NONE"


def is_material(basis: str = "") -> bool:
    return (basis or "").strip().upper() in set(MATERIALITY_BASES) - {"NONE"}


def basis_for_target(target_id: str, objects) -> str | None:
    """The stored basis for a target, or None when the target has NO object at all.
    `None` and `"NONE"` are different answers a caller must be able to tell apart."""
    if not (target_id or "").strip():
        return None
    for o in (objects or []):
        if getattr(o, "target_id", "") == target_id:
            return (getattr(o, "materiality_basis", "") or "NONE")
    return None


def material_target_failure(objects, outcomes):
    """THE ONE PRIMITIVE. The first outcome that both establishes a defect AND is
    material. Route-blind by construction — no branch on provenance or disposition here.
    A target whose object is missing is NOT material by default; see
    `unjoinable_established_failures`."""
    for o in (outcomes or []):
        if not getattr(o, "establishes_failure", False):
            continue
        if is_material(basis_for_target(getattr(o, "target_id", ""), objects) or "NONE"):
            return o
    return None


def established_defects(outcomes) -> list:
    """TIER 1 ALONE: every outcome whose own evidence route established a defect,
    deliberately blind to materiality, objects and route."""
    return [o for o in (outcomes or []) if getattr(o, "establishes_failure", False)]


def has_established_defect(outcomes) -> bool:
    return bool(established_defects(outcomes))


def unjoinable_established_failures(objects, outcomes) -> list[str]:
    """Target ids that established a defect and have NO object to assess materiality
    from — a reviewer implementation defect, never evidence about the paper."""
    return [getattr(o, "target_id", "") for o in (outcomes or [])
            if getattr(o, "establishes_failure", False)
            and basis_for_target(getattr(o, "target_id", ""), objects) is None]


# =============================================================================
# COMPARISON — what a result would be held against, and whether that exists
# =============================================================================
# The comparison is a property of the ROUTE, not of the question — keeping them apart is
# what stops "this is an attribution question" silently becoming "so its number may be
# reconciled against a cell". `RECONCILABLE` is one entry long, which is the honest state
# of this system: only AGAINST_PRINTED_VALUE has arithmetic behind it.

COMPARISON_FOR_ROUTE = {
    "AUTHOR_CODE_EXECUTION": "AGAINST_PRINTED_VALUE",
    "INDEPENDENT_RECONSTRUCTION": "AGAINST_PRINTED_VALUE",
    "ARITHMETIC_RECHECK": "AGAINST_PRINTED_VALUE",
    "ARTIFACT_INSPECTION": "AGAINST_EXISTENCE",
    "PAPER_INTERNAL_CHECK": "AGAINST_SPECIFICATION",
    "NONE": "",
}
RECONCILABLE = ("AGAINST_PRINTED_VALUE",)
_UNSUPPORTED_DETAIL = {
    "AGAINST_EXISTENCE":
        "this comparison asks whether something the claim requires is present, which is a "
        "reading of the artifact rather than an arithmetic against a printed value. No "
        "route in this system produces that answer as admissible evidence yet.",
    "AGAINST_SPECIFICATION":
        "this comparison asks whether an observed procedure matches a specified one, which "
        "is not an arithmetic against a printed value. No route in this system produces "
        "that answer as admissible evidence yet.",
}


def kind_for_route(route: str = "") -> str:
    return COMPARISON_FOR_ROUTE.get(route or "", "")


def reconcilable(kind: str = "") -> bool:
    return (kind or "") in RECONCILABLE


def needs_printed_value(route: str = "") -> bool:
    """Read by `discover.py`: a route whose comparison is against a printed value is not
    offered for an object that has none."""
    return kind_for_route(route) == "AGAINST_PRINTED_VALUE"


def derive_comparison(route: str = "", *, printed_value_available: bool = False) -> Comparison:
    """The comparison for one route, and whether this run could carry it out. Three
    states, none a finding about the paper: `established`, `no_reference` (the paper
    printed nothing here to compare against), `unsupported` (no arithmetic for this kind
    at all)."""
    kind = kind_for_route(route)
    if not kind:
        return Comparison(kind="", state="unmapped",
                          reason=f"route '{route or 'unset'}' produces nothing to compare.")
    if kind == "AGAINST_PRINTED_VALUE":
        if printed_value_available:
            return Comparison(kind=kind, state="established",
                              measured="the quantity the executed program reports",
                              reference="the quantity the paper printed at the cited address",
                              reason="")
        return Comparison(
            kind=kind, state="no_reference",
            measured="the quantity the executed program reports",
            reference="the quantity the paper printed at the cited address",
            reason=("the paper prints no single unambiguous quantity at the cited address, "
                    "so a run of this route would produce a number with nothing to "
                    "reconcile it against."))
    return Comparison(kind=kind, state="unsupported",
                      measured="what this route would observe",
                      reference="what the claim requires to be there",
                      reason=_UNSUPPORTED_DETAIL[kind])


def admits_verdict(comparison: Comparison | None) -> bool:
    """May a process be started for a spec carrying this comparison? `None` is True — a
    spec built before this layer existed must keep working exactly as it did."""
    if comparison is None:
        return True
    return comparison.established and reconcilable(comparison.kind)


# =============================================================================
# EXHAUSTION — which evidence routes were applicable, tried, and genuinely closed
# =============================================================================
# Three-way partition, not two, because the naive version is gameable: with
# SH_ALLOW_REPO_EXEC=0 every authors'-code route is "blocked", and counting a blocked
# route as discharged would read 1.0 on every paper — a perfect score measuring the
# operator's .env file.
#
#   DISCHARGED     ran and produced admissible evidence, or ended at a blocker that is a
#                  fact about the PAPER, the ARTIFACT or the WORLD
#   CONFIGURATION  a gate this harness owns was shut, a budget ran out, or a policy
#                  demoted it — reported in its own column, never summed into DISCHARGED
#   OPEN           applicable, affordable, and nothing was done about it

DISCHARGING_STATES = ("DISCHARGED_RAN", "DISCHARGED_COMPLETED",
                      "DISCHARGED_BLOCKED", "COMPLETED_INCONCLUSIVE")
CONFIGURATION_STATES = ("GATE_CLOSED", "DEFERRED_BUDGET", "DEFERRED_POLICY")
OPEN_STATES = ("NOT_TRIED",)

DISCHARGING_BLOCKERS = (
    "SPECIFICATION_BLOCKED", "REPORTING_BLOCKED", "ARTIFACT_BLOCKED", "IDENTITY_BLOCKED",
    "RESOURCE_BLOCKED", "ENVIRONMENT_BLOCKED", "NO_ROUTE_AVAILABLE", "COMPARISON_BLOCKED",
    "ADDRESSING_BLOCKED", "CONFORMANCE_BLOCKED",
)
NON_DISCHARGING_BLOCKERS = ("AUTHORIZATION_BLOCKED", "BUDGET_DEFERRED",
                           "SUPERSEDED_BY_ESTABLISHED_FAILURE")
# Routes with no executor. Named rather than silently dropped — the exclusion IS the
# limitation. The literature-search, focused-validation and claim-link routes are simply
# gone from VERIFICATION_ROUTES (deleted 2026-09-20), not listed here.
UNIMPLEMENTED_ROUTES = ("ARTIFACT_INSPECTION", "NONE")


def implemented_routes() -> tuple[str, ...]:
    """Routes that could actually discharge, derived rather than listed."""
    settling = ("PAPER_INTERNAL_CHECK", "ARITHMETIC_RECHECK")
    runnable = tuple(r for r in VERIFICATION_ROUTES
                     if r in PLAN_ACTIONS or r.replace("EXECUTION", "REPRODUCTION") in PLAN_ACTIONS)
    return tuple(r for r in VERIFICATION_ROUTES
                if (r in settling or r in runnable) and r not in UNIMPLEMENTED_ROUTES)


def applicable_routes(routes: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The subset of a target's routes this metric will hold the review to — derived
    from what is implemented, never from what happened."""
    impl = implemented_routes()
    return tuple(r for r in (routes or ()) if r in impl)


def state_for(*, ran: bool = False, provenance: str = "", blocker: str = "",
             gate_closed: bool = False, budget_deferred: bool = False,
             policy_demoted: bool = False) -> str:
    """What became of one route, from plain facts. A gate being shut outranks a typed
    blocker, because the gate is WHY the blocker was reached."""
    if ran and admits(provenance):
        return "DISCHARGED_RAN"
    if gate_closed:
        return "GATE_CLOSED"
    if budget_deferred:
        return "DEFERRED_BUDGET"
    if policy_demoted:
        return "DEFERRED_POLICY"
    b = (blocker or "").strip().upper()
    if b in NON_DISCHARGING_BLOCKERS:
        return "GATE_CLOSED" if b == "AUTHORIZATION_BLOCKED" else "DEFERRED_BUDGET"
    if b in DISCHARGING_BLOCKERS:
        return "DISCHARGED_BLOCKED"
    return "NOT_TRIED"


def route_coverage(attempts: list | None = None, question_ids: tuple[str, ...] = ()) -> dict:
    """Route-exhaustion coverage, with every denominator printed beside it. A question is
    EXHAUSTED when every applicable route attached to it is discharged; `rate` is None —
    never 1.0 — for an empty denominator."""
    by_q: dict[str, list] = {}
    for a in (attempts or []):
        by_q.setdefault(str(getattr(a, "question_id", "") or ""), []).append(a)

    qids = tuple(question_ids) if question_ids else tuple(by_q)
    exhausted, open_q, config_q = [], [], []
    for q in qids:
        rows = by_q.get(q) or []
        if not rows:
            open_q.append(q)
            continue
        states = [str(getattr(r, "state", "")) for r in rows]
        if all(s in DISCHARGING_STATES for s in states):
            exhausted.append(q)
        elif any(s in CONFIGURATION_STATES for s in states):
            config_q.append(q)
        else:
            open_q.append(q)

    measured = len(qids)
    rows = list(attempts or [])
    return {
        "questions": measured, "exhausted": len(exhausted),
        "open_because_untried": len(open_q), "open_because_of_this_harness": len(config_q),
        "rate": (round(len(exhausted) / measured, 4) if measured else None),
        "routes_applicable": len(rows),
        "routes_attempted": sum(bool(getattr(a, "attempted", False)) for a in rows),
        "routes_completed": sum(bool(getattr(a, "completed", False)) for a in rows),
        "routes_exhausted": sum(bool(getattr(a, "exhausted", False)) for a in rows),
        "route_attempts": len(rows),
        "unimplemented_routes_excluded": list(UNIMPLEMENTED_ROUTES),
        "note": ("a route ended by a gate, a budget or a set-level policy is counted in "
                 "open_because_of_this_harness and is NEVER discharged, so this rate "
                 "cannot be raised by shutting a gate"),
    }


def _material_question_ids(ts: TargetSet) -> tuple[str, ...]:
    """Questions in scope, provided at least one implemented route applies. The union of
    `ReviewQuestion.materiality == CENTRAL` (review-priority) and a bound target's own
    `materiality_basis` (machine-owned) — neither may make a question disappear merely
    because the other is conservative."""
    objects: dict[str, list] = {}
    for obj in ts.objects:
        if obj.question_id:
            objects.setdefault(obj.question_id, []).append(obj)
    out: list[str] = []
    for q in ts.questions:
        bound = objects.get(q.question_id, [])
        routes = {r for obj in bound for r in applicable_routes(tuple(obj.routes))}
        in_scope = (q.materiality == "CENTRAL"
                    or any(is_material(obj.materiality_basis) for obj in bound))
        if in_scope and routes:
            out.append(q.question_id)
    return tuple(out)


def _gate_closed(cfg, route: str) -> bool:
    if cfg is None:
        return False
    if route == "AUTHOR_CODE_EXECUTION":
        return not bool(getattr(cfg, "allow_repo_exec", False))
    if route == "INDEPENDENT_RECONSTRUCTION":
        return (not bool(getattr(cfg, "allow_reimplementation_driver", False))
                or not bool(getattr(cfg, "allow_reimplementation_exec", False)))
    return False


def _refs(q, objs: list, plan_, outcome) -> list[str]:
    refs: list[str] = []
    qref = getattr(getattr(q, "claim_ref", None), "ref", "")
    if qref:
        refs.append(qref)
    for obj in objs:
        oref = getattr(getattr(obj, "ref", None), "ref", "")
        refs.extend(x for x in (oref, obj.target_id) if x)
    if plan_ is not None:
        refs.append(f"plan:{plan_.target_id}:{plan_.attempt}:{plan_.route}")
    if outcome is not None:
        refs.append(f"outcome:{outcome.target_id}:{outcome.disposition}")
    execution_ref = getattr(outcome, "execution_ref", "") if outcome else ""
    if execution_ref:
        refs.append(execution_ref)
    return list(dict.fromkeys(refs))


def _attempt_for(q, route: str, objs: list, plans: list, outcomes: list, cfg) -> RouteAttempt:
    target_ids = {o.target_id for o in objs}
    route_plans = [p for p in plans if p.target_id in target_ids and p.route == route]
    route_outcomes = [o for o in outcomes if o.target_id in target_ids and o.route == route]
    plan_ = route_plans[-1] if route_plans else None
    outcome = route_outcomes[-1] if route_outcomes else None

    state, blocker, reason = "NOT_TRIED", "", ""
    attempted = completed = exhausted = False

    if (outcome is None and route == "PAPER_INTERNAL_CHECK"
            and getattr(getattr(q, "claim_ref", None), "resolved", False)):
        state, attempted, completed, exhausted = "COMPLETED_INCONCLUSIVE", True, True, True
        reason = ("the paper locator and quotation were deterministically rechecked; this "
                  "exhausts the paper-internal citation check but does not settle the "
                  "scientific question")
    elif outcome is not None:
        disposition = outcome.disposition
        reason = outcome.reason
        if route == "ARITHMETIC_RECHECK" and disposition in (
                "PAPER_ONLY_RESOLVED", "PAPER_ARITHMETIC_CONTRADICTION"):
            state, attempted, completed, exhausted = "DISCHARGED_COMPLETED", True, True, True
        elif route == "PAPER_INTERNAL_CHECK" and disposition in (
                "CITATION_VERIFIED_ONLY", "PAPER_ONLY_RESOLVED"):
            state, attempted, completed, exhausted = "COMPLETED_INCONCLUSIVE", True, True, True
        elif outcome.launched > 0 and admits(outcome.provenance) and disposition in (
                "REPRODUCED", "FAILED_REPRODUCTION"):
            state, attempted, completed, exhausted = "DISCHARGED_RAN", True, True, True
        elif disposition == "BUDGET_DEFERRED":
            state, blocker = "DEFERRED_BUDGET", disposition
        elif disposition == "SUPERSEDED_BY_ESTABLISHED_FAILURE":
            state, blocker = "DEFERRED_POLICY", disposition
        elif (route == "INDEPENDENT_RECONSTRUCTION"
              and disposition in ("INCONCLUSIVE", "AUTHORIZATION_BLOCKED")
              and outcome.launched == 0
              and "conformance_unproven" in (outcome.reason or "")):
            state, blocker = "DISCHARGED_BLOCKED", "CONFORMANCE_BLOCKED"
            attempted, completed, exhausted = True, True, True
        elif (route == "INDEPENDENT_RECONSTRUCTION" and disposition == "SPECIFICATION_BLOCKED"
              and outcome.launched == 0):
            # Checked NARROWLY, before the general gate-closed fallback: this disposition
            # is written by `plan()` at DISCOVER time from the paper's own eligibility,
            # before ANY execution gate is consulted, so a paper that simply never states
            # a training procedure must not be attributed to this harness's `.env` file.
            state, blocker = "DISCHARGED_BLOCKED", disposition
            attempted, completed, exhausted = True, True, True
        elif (disposition == "AUTHORIZATION_BLOCKED" or _gate_closed(cfg, route)) \
                and outcome.launched == 0:
            state, blocker, attempted = "GATE_CLOSED", "AUTHORIZATION_BLOCKED", True
        elif disposition in DISCHARGING_BLOCKERS:
            state, blocker = "DISCHARGED_BLOCKED", disposition
            attempted, completed, exhausted = True, True, True
        elif disposition == "INCONCLUSIVE" and outcome.launched > 0:
            state, attempted, completed, exhausted = "COMPLETED_INCONCLUSIVE", True, True, True
            if route == "INDEPENDENT_RECONSTRUCTION":
                low = (reason or "").lower()
                if "modulenotfounderror" in low or "importerror" in low:
                    blocker = "DEPENDENCY_MISSING"
                elif "seed-to-seed noise measured as zero" in low or "no band to test against" in low:
                    blocker = "ZERO_VARIANCE"
        elif (disposition == "NOT_ATTEMPTED" and plan_ is not None
              and plan_.blocking_gate in ("outranked_by_a_central_target",
                                          "answered_by_another_target")):
            state, blocker = "DEFERRED_POLICY", plan_.blocking_gate
        elif disposition not in ("PENDING", "NOT_ATTEMPTED"):
            attempted = True
    elif plan_ is not None:
        reason = plan_.reason
        if plan_.superseded_by:
            state, blocker = "DISCHARGED_BLOCKED", "IDENTITY_BLOCKED"
            attempted, completed, exhausted = True, True, True
        elif plan_.blocking_gate in ("outranked_by_a_central_target",
                                     "answered_by_another_target"):
            state, blocker = "DEFERRED_POLICY", plan_.blocking_gate
        elif plan_.blocking_gate:
            state, blocker, attempted = "GATE_CLOSED", plan_.blocking_gate, True

    return RouteAttempt(
        question_id=q.question_id, route=route,
        target_ids=[o.target_id for o in objs], attempted=attempted, completed=completed,
        exhausted=exhausted, state=state, blocker=blocker,
        source_refs=_refs(q, objs, plan_, outcome), reason=reason)


def refresh_route_attempts(ts: TargetSet, cfg=None) -> TargetSet:
    """Rebuild the durable route rows from the TargetSet's current artifacts. A fold, so
    a cached second pass cannot retain a flattering row after the outcome changed."""
    qids = _material_question_ids(ts)
    q_by_id = {q.question_id: q for q in ts.questions}
    by_q: dict[str, list] = {}
    for obj in ts.objects:
        if obj.question_id:
            by_q.setdefault(obj.question_id, []).append(obj)

    rows: list[RouteAttempt] = []
    for qid in qids:
        q = q_by_id[qid]
        bound = by_q.get(qid, [])
        for route in VERIFICATION_ROUTES:
            route_objs = [o for o in bound if route in applicable_routes(tuple(o.routes))]
            if route_objs:
                rows.append(_attempt_for(q, route, route_objs, ts.plans, ts.outcomes, cfg))
    ts.route_attempts = rows
    ts.route_exhaustion_question_ids = list(qids)
    return ts


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    """A construction smoke test. Exhaustive sweeps (the itertools.product cross-products
    over `derive_disposition`, `classify_plan`, `score` etc.) live in the invariant
    suite (`tests/test_decide_invariants.py`), not here."""
    import inspect

    # --- disposition --------------------------------------------------------------------
    assert set(_REASON) == set(PAPER_DISPOSITIONS)
    assert set(BLOCKER_FOR_DISPOSITION) & set(DELIBERATELY_UNCLASSIFIED) == set()
    d, b, why = derive_disposition(claim_status="VERIFIED_FAILURE", basis="PAPER_ARITHMETIC")
    assert d == "STOP_MATERIAL_FAILURE" and "Established by" in why
    assert derive_disposition(central_blockers=("METHOD",), counted_major=5)[0] == "BLOCKED_METHOD"
    assert derive_disposition(established_non_material=1)[0] == "PASS_TO_HUMAN_CONCERNS"
    assert derive_disposition()[0] == "PASS_TO_HUMAN_CLEAN"
    assert derive_disposition(review_complete=False)[0] == "NOT_REVIEWED"
    assert disposition_basis_for(failed_target_provenance="repo_exec") == "AUTHOR_CODE_REPRODUCTION"
    assert disposition_basis_for(failed_target_provenance="synthesized") == "NONE"
    assert blockers_from(["NO_ROUTE_AVAILABLE"], ["CENTRAL"]) == ("METHOD",)

    # --- planner --------------------------------------------------------------------------
    for name, p in inspect.signature(classify_plan).parameters.items():
        assert str(p.annotation) in ("str", "bool"), name
    central = DiscoveredObject(target_id="T1", centrality="CENTRAL", harness_addressable=True,
                               routes=["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"])
    got = plan(central, artifact_available=True, environment_state="ok")
    assert got.action == "AUTHOR_CODE_REPRODUCTION" and got.requires_execution
    cheap = DiscoveredObject(target_id="T2", centrality="CENTRAL", harness_addressable=True,
                             routes=["ARITHMETIC_RECHECK", "AUTHOR_CODE_EXECUTION"])
    assert plan(cheap, artifact_available=True).action == "PAPER_ONLY_RESOLUTION", (
        "a question answerable from the paper must never escalate to an execution")
    both_routes = DiscoveredObject(
        target_id="T6", centrality="CENTRAL", harness_addressable=True,
        routes=["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    first = plan(both_routes, artifact_available=True, specification_complete=True, environment_state="ok")
    fallback = plan(both_routes, artifact_available=True, specification_complete=True,
                    environment_state="ok", author_code_exhausted=True, attempt=2)
    first.superseded_by = fallback.route
    deduped = current_plans([first, fallback])
    assert len(deduped) == 1 and deduped[0] is fallback, (
        "current_plans keeps exactly one entry per target: the LAST one")

    # --- priority ---------------------------------------------------------------------
    hard, _ = score(centrality="CENTRAL", addressable=True, cheapest_route="AUTHOR_CODE_EXECUTION")
    free, _ = score(centrality="PERIPHERAL", addressable=True, cheapest_route="ARITHMETIC_RECHECK")
    assert hard > free, "a cheap peripheral target must never outrank an expensive central one"

    # --- materiality --------------------------------------------------------------------
    doc = PaperDoc(paper_id="p", sections=[
        Section(section_idx=0, title="", text="Title\nAuthor"),
        Section(section_idx=1, title="Abstract", text="We report 58 x 5 x 10 = 2,901 cases."),
    ])
    assert abstract_section_idx(doc) == 1
    in_abs = ClaimRef(ref="P1:0-20", kind="prose_claim", section_idx=1, resolution="resolved")
    assert basis_for_ref(in_abs, 1) == "ABSTRACT_CLAIM"
    material = DiscoveredObject(target_id="A", materiality_basis="ABSTRACT_CLAIM")
    incidental = DiscoveredObject(target_id="B", materiality_basis="NONE")
    hit = TargetOutcome(target_id="A", disposition="PAPER_ARITHMETIC_CONTRADICTION", provenance="paper")
    miss = TargetOutcome(target_id="B", disposition="PAPER_ARITHMETIC_CONTRADICTION", provenance="paper")
    assert material_target_failure([material, incidental], [hit]) is hit
    assert material_target_failure([material, incidental], [miss]) is None
    # Swept over the code with the docstring removed, so the primitive stays route-blind.
    import ast
    tree = ast.parse(inspect.getsource(material_target_failure).lstrip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    for forbidden in ("repo_exec", "reimpl_exec", "PAPER_ARITHMETIC", "FAILED_REPRODUCTION",
                      "provenance", "disposition"):
        assert forbidden not in code, f"{forbidden} would make materiality route-specific"

    # --- comparison ---------------------------------------------------------------------
    assert reconcilable("AGAINST_PRINTED_VALUE") and not reconcilable("AGAINST_EXISTENCE")
    ok = derive_comparison("AUTHOR_CODE_EXECUTION", printed_value_available=True)
    assert ok.established and admits_verdict(ok)
    assert admits_verdict(None), "a spec built before this layer must behave as it did"

    # --- exhaustion: THE ANTI-GAMING RULE -----------------------------------------------
    assert state_for(ran=True, provenance="repo_exec") == "DISCHARGED_RAN"
    assert state_for(ran=True, provenance="synthesized") != "DISCHARGED_RAN"
    assert state_for(gate_closed=True) == "GATE_CLOSED"
    assert state_for(gate_closed=True, blocker="IDENTITY_BLOCKED") == "GATE_CLOSED", (
        "with the gate shut, identity is refused as a side effect; reporting "
        "IDENTITY_BLOCKED attributes our own setting to the artifact")
    assert route_coverage([])["rate"] is None

    class _A:
        def __init__(self, q, s): self.question_id, self.state = q, s

    gated = route_coverage([_A("q1", "GATE_CLOSED"), _A("q2", "GATE_CLOSED")])
    assert gated["rate"] == 0.0, "shutting a gate must not produce a perfect score"
    print("harness.decide self-check ok")


if __name__ == "__main__":
    _self_check()
