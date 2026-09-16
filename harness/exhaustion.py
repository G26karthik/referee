"""Which evidence routes were applicable, which were tried, and which are genuinely closed.

`python -m harness.exhaustion` runs the self-check.

**What this exists to answer.** The product rule is that a material question must have
every applicable, affordable, non-inventive route either carried out or ended at a real
typed blocker before the review may call it unresolved. Nothing in the harness could say
whether that held: `PlanDecision` records the ONE route the planner chose, the planner
stops at the first admissible one, and routes two through n leave no trace at all. So
"we exhausted the alternatives" and "we took the first thing that fitted and stopped" were
the same artifact.

**The trap this metric walks into if built naively, and the rule that prevents it.** With
`SH_ALLOW_REPO_EXEC=0`, every authors'-code route is "blocked". Count a blocked route as
discharged and exhaustion reads 1.0 on every paper — a perfect score measuring the
operator's `.env` file. That is invariant 17's defect ("a colour may not be a property of
this harness's configuration") reappearing as a rate, and it is the natural failure mode
here rather than an exotic one.

So the states are partitioned THREE ways, not two:

    DISCHARGED      the route ran and produced admissible evidence, or it ended at a
                    blocker that is a fact about the PAPER, the ARTIFACT or the WORLD
    CONFIGURATION   a gate this harness owns was shut, a budget ran out, or a set-level
                    policy demoted it. Neither discharged nor a fact about the paper, and
                    reported in its own column so it can never be mistaken for either
    OPEN            applicable, affordable, and nothing was done about it

Only DISCHARGED counts toward exhaustion. CONFIGURATION is published beside it, because a
review that could have checked more had a gate been open is a different object from one
that checked everything it could — and the reader is entitled to tell them apart.

**Three further rules, each closing a way to make the number flattering.**

*A route is APPLICABLE only if it is implemented.* `ARTIFACT_INSPECTION` appears on 261
corpus objects and has no `PLAN_ACTIONS` member, so it can never discharge.
Counting such a route as applicable pins exhaustion below 1.0 forever
for a reason that has nothing to do with any paper. They are excluded from the denominator
and named in `UNIMPLEMENTED_ROUTES`, so the ceiling is visible rather than absorbed.

*Discharge-by-running requires an admissible provenance.* A synthesized diagnostic that ran
is not the authors'-code route carried out. The shipped corpus has 16 outcomes whose route
says `AUTHOR_CODE_EXECUTION` and whose provenance says `synthesized`; without this rule all
16 would count as that route discharged.

*Unknown affordability is OPEN, never ruled out.* Invariant 6 already refuses to treat an
unstated demand as a small one. Carrying that here stops "we declared it unaffordable"
becoming the escape hatch that empties the denominator.
"""
from __future__ import annotations

from .artifacts import (PLAN_ACTIONS, ROUTE_ATTEMPT_STATES, VERIFICATION_ROUTES,
                        RouteAttempt, TargetSet)
from . import materiality
from .provenance import admits

# THE PARTITION. Exhaustion counts the first group and nothing else.
DISCHARGING_STATES = ("DISCHARGED_RAN", "DISCHARGED_COMPLETED",
                      "DISCHARGED_BLOCKED", "COMPLETED_INCONCLUSIVE")
# Ours, not the paper's. Published in its own column and never summed into the numerator.
CONFIGURATION_STATES = ("GATE_CLOSED", "DEFERRED_BUDGET", "DEFERRED_POLICY")
OPEN_STATES = ("NOT_TRIED",)

# Blockers that DISCHARGE a route: each is a fact about the paper, its artifact, or the
# world, and none of them is resolvable by changing this harness's configuration.
DISCHARGING_BLOCKERS = (
    "SPECIFICATION_BLOCKED",   # the paper does not specify enough to build the experiment
    "REPORTING_BLOCKED",       # the paper prints no unambiguous quantity to compare against
    "ARTIFACT_BLOCKED",        # no usable released artifact
    "IDENTITY_BLOCKED",        # which command produces the cited number cannot be established
    "RESOURCE_BLOCKED",        # measured, with a machine cited — see `affordability_basis`
    "ENVIRONMENT_BLOCKED",     # the declared stack cannot be constructed anywhere available
    "NO_ROUTE_AVAILABLE",      # this review has no method for this question
    "COMPARISON_BLOCKED",      # nothing the result could be held against
    "ADDRESSING_BLOCKED",      # no re-derivable address could be built
    "CONFORMANCE_BLOCKED",     # generated reconstruction failed independent binding checks
)

# Blockers that do NOT discharge, because this harness caused them.
NON_DISCHARGING_BLOCKERS = ("AUTHORIZATION_BLOCKED", "BUDGET_DEFERRED",
                            "SUPERSEDED_BY_ESTABLISHED_FAILURE")

# Routes with no executor. Named rather than silently dropped: the exclusion IS the
# limitation, and a reader who cannot see it cannot judge the rate.
UNIMPLEMENTED_ROUTES = (
    "ARTIFACT_INSPECTION",
    # FOCUSED_VALIDATION_EXPERIMENT used to be here and no longer is. It was listed
    # because the only executor authored a ``synthesized`` probe whose result the
    # provenance ceiling cannot admit — a route that could not possibly discharge, so
    # advertised rather than implemented. It has an executor now (`stages/validation.py`,
    # over `validation.design` and `between_arms.compare`), and what it discharges is
    # bounded in the same way the literature route's is: `validation.discharge` requires a
    # comparison that reached CONFORMANT_CONTROLLED_RESULT and answers the question it was
    # designed for, so a blocked design, a refused comparison and a controlled observation
    # whose conformance was never established all leave it undischarged.
    # LITERATURE_SEARCH used to be here and no longer is: it has a `PLAN_ACTIONS` member
    # (`LITERATURE_SEARCH_ONLY`) and an executor (`stages/literature.py`), so it can
    # discharge. What it discharges is bounded — a verified prior-art concern or a bound
    # relation — and a search that completed with no match does NOT discharge, which is
    # `literature.discharge`'s whole job. A route that could be discharged by finding
    # nothing would be an exhaustion number that rises fastest on the papers nobody
    # searched properly.
    "NONE",
)


def implemented_routes() -> tuple[str, ...]:
    """Routes that could actually discharge, derived rather than listed.

    A route discharges by running (so it needs a `PLAN_ACTIONS` member) or by settling from
    the paper alone. Anything else is advertised capability, and advertising a route this
    harness cannot take is what made `ARTIFACT_INSPECTION` appear on 261 objects and be
    chosen by 4 plans that then did nothing.
    """
    settling = ("PAPER_INTERNAL_CHECK", "ARITHMETIC_RECHECK")
    runnable = tuple(r for r in VERIFICATION_ROUTES
                     if r in PLAN_ACTIONS or r.replace("EXECUTION", "REPRODUCTION") in PLAN_ACTIONS)
    out = tuple(r for r in VERIFICATION_ROUTES
                if (r in settling or r in runnable) and r not in UNIMPLEMENTED_ROUTES)
    return out


def applicable_routes(routes: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The subset of a target's routes that this metric will hold the review to.

    Derived from what is implemented, never from what happened — the same discipline
    `coverage.surface` follows in taking a `PaperDoc` and nothing from the review. A
    denominator that can shrink in response to an outcome is the `objects_addressable_rate`
    defect, where a worse extractor scored higher.
    """
    impl = implemented_routes()
    return tuple(r for r in (routes or ()) if r in impl)


def state_for(*, ran: bool = False, provenance: str = "", blocker: str = "",
              gate_closed: bool = False, budget_deferred: bool = False,
              policy_demoted: bool = False) -> str:
    """What became of one route, from plain facts about it. Booleans and strings only.

    Order matters and encodes the precedence: something that RAN admissibly is discharged
    whatever else is true of it; a configuration cause outranks a typed blocker, because
    a gate being shut is WHY the blocker was reached and reporting the blocker would
    attribute our own setting to the artifact — which is exactly what `_not_started`'s
    default `IDENTITY_BLOCKED` does when `SH_ALLOW_REPO_EXEC` is 0.
    """
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


def coverage(attempts: list | None = None, question_ids: tuple[str, ...] = ()) -> dict:
    """Route-exhaustion coverage, with every denominator it depends on printed beside it.

    A question is EXHAUSTED when every applicable route attached to it is discharged.
    `rate` is None — never 1.0 — for an empty denominator, the same refusal
    `coverage.measure` makes for an empty surface: a rate over no questions is not a
    perfect score, it is an absent measurement.

    The three-way split of what is NOT exhausted is returned because it is the whole
    point: a question left open by a shut gate, by a budget, and by nobody trying are
    three different failures and only the last two are ours to fix by working harder.
    """
    by_q: dict[str, list] = {}
    for a in (attempts or []):
        by_q.setdefault(str(getattr(a, "question_id", "") or ""), []).append(a)

    qids = tuple(question_ids) if question_ids else tuple(by_q)
    exhausted, open_q, config_q = [], [], []
    for q in qids:
        rows = by_q.get(q) or []
        if not rows:
            # The denominator is question-owned, not row-owned.  Losing a row must make
            # coverage worse and visible, never silently shrink the denominator.
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
        "questions": measured,
        "exhausted": len(exhausted),
        "open_because_untried": len(open_q),
        "open_because_of_this_harness": len(config_q),
        # None, not 1.0, on an empty denominator.
        "rate": (round(len(exhausted) / measured, 4) if measured else None),
        "routes_applicable": len(rows),
        "routes_attempted": sum(bool(getattr(a, "attempted", False)) for a in rows),
        "routes_completed": sum(bool(getattr(a, "completed", False)) for a in rows),
        "routes_exhausted": sum(bool(getattr(a, "exhausted", False)) for a in rows),
        # Backward-friendly explicit alias: these are durable attempt rows, not target
        # outcomes and not planner intentions.
        "route_attempts": len(rows),
        "unimplemented_routes_excluded": list(UNIMPLEMENTED_ROUTES),
        "note": ("a route ended by a gate, a budget or a set-level policy is counted in "
                 "open_because_of_this_harness and is NEVER discharged, so this rate "
                 "cannot be raised by shutting a gate"),
    }


def _material_question_ids(ts: TargetSet) -> tuple[str, ...]:
    """Questions in scope, provided at least one implemented route applies.

    ``ReviewQuestion.materiality == CENTRAL`` is the review-priority signal.  A target's
    ``materiality_basis`` is the independent machine-owned sufficient condition.  The
    union is deliberate: neither signal is allowed to make a question disappear from the
    accounting merely because the other is conservative or model-proposed.
    """
    objects: dict[str, list] = {}
    for obj in ts.objects:
        if obj.question_id:
            objects.setdefault(obj.question_id, []).append(obj)
    out: list[str] = []
    for q in ts.questions:
        bound = objects.get(q.question_id, [])
        routes = {r for obj in bound for r in applicable_routes(tuple(obj.routes))}
        in_scope = (q.materiality == "CENTRAL"
                    or any(materiality.is_material(obj.materiality_basis) for obj in bound))
        if in_scope and routes:
            out.append(q.question_id)
    return tuple(out)


def _gate_closed(cfg, route: str) -> bool:
    """Whether this harness's own configuration prevented the route from completing."""
    if cfg is None:
        return False
    if route == "AUTHOR_CODE_EXECUTION":
        return not bool(getattr(cfg, "allow_repo_exec", False))
    if route == "INDEPENDENT_RECONSTRUCTION":
        return (not bool(getattr(cfg, "allow_reimplementation_driver", False))
                or not bool(getattr(cfg, "allow_reimplementation_exec", False)))
    if route == "FOCUSED_VALIDATION_EXPERIMENT":
        # BOTH gates, because either one alone stops the route completing for a reason
        # that is ours. With the design gate shut, every design is SPECIFICATION_BLOCKED —
        # which without this branch scores DISCHARGED_BLOCKED, i.e. "the paper does not
        # say enough", when the truth is that nothing asked. With the execution gate shut,
        # a bound design cannot run. This is the defect the module's own docstring warns
        # about — exhaustion reading 1.0 while measuring the operator's environment —
        # arriving on a new route.
        return (not bool(getattr(cfg, "allow_validation_design", False))
                or not bool(getattr(cfg, "allow_repo_exec", False)))
    return False


def _refs(q, objs: list, plan, outcome) -> list[str]:
    refs: list[str] = []
    qref = getattr(getattr(q, "claim_ref", None), "ref", "")
    if qref:
        refs.append(qref)
    for obj in objs:
        oref = getattr(getattr(obj, "ref", None), "ref", "")
        refs.extend(x for x in (oref, obj.target_id) if x)
    if plan is not None:
        refs.append(f"plan:{plan.target_id}:{plan.attempt}:{plan.route}")
    if outcome is not None:
        refs.append(f"outcome:{outcome.target_id}:{outcome.disposition}")
    execution_ref = getattr(outcome, "execution_ref", "") if outcome else ""
    if execution_ref:
        refs.append(execution_ref)
    # Stable order and no duplicated target/address in a machine ledger.
    return list(dict.fromkeys(refs))


def _attempt_for(q, route: str, objs: list, plans: list, outcomes: list, cfg) -> RouteAttempt:
    target_ids = {o.target_id for o in objs}
    route_plans = [p for p in plans if p.target_id in target_ids and p.route == route]
    route_outcomes = [o for o in outcomes if o.target_id in target_ids and o.route == route]
    plan = route_plans[-1] if route_plans else None
    outcome = route_outcomes[-1] if route_outcomes else None

    state, blocker, reason = "NOT_TRIED", "", ""
    attempted = completed = exhausted = False

    if (outcome is None and route == "PAPER_INTERNAL_CHECK"
            and getattr(getattr(q, "claim_ref", None), "resolved", False)):
        # The route's implemented operation is the deterministic locator/quotation
        # recheck already performed while minting the resolved ClaimRef. It can finish
        # inconclusively while the scientific question remains open; route exhaustion
        # records that the method reached its boundary, not that the claim was settled.
        state, attempted, completed, exhausted = (
            "COMPLETED_INCONCLUSIVE", True, True, True)
        reason = ("the paper locator and quotation were deterministically rechecked; "
                  "this exhausts the paper-internal citation check but does not settle "
                  "the scientific question")
    elif outcome is not None:
        disposition = outcome.disposition
        reason = outcome.reason
        if route == "ARITHMETIC_RECHECK" and disposition in (
                "PAPER_ONLY_RESOLVED", "PAPER_ARITHMETIC_CONTRADICTION"):
            state, attempted, completed, exhausted = "DISCHARGED_COMPLETED", True, True, True
        elif route == "PAPER_INTERNAL_CHECK" and disposition in (
                "CITATION_VERIFIED_ONLY", "PAPER_ONLY_RESOLVED"):
            # Historical artifacts may still carry PAPER_ONLY_RESOLVED for this route.  The
            # route only checked the citation, so neither spelling may discharge it.
            state, attempted, completed, exhausted = (
                "COMPLETED_INCONCLUSIVE", True, True, True)
        elif outcome.launched > 0 and admits(outcome.provenance) and disposition in (
                "REPRODUCED", "FAILED_REPRODUCTION",
                # THE TWO SETTLED FOCUSED-VALIDATION OUTCOMES, under exactly the same three
                # conditions: a process really started, the provenance clears the
                # reproduction ceiling, and the disposition is one that settles. Without
                # them a genuinely run, genuinely settled controlled experiment fell
                # through every branch below to NOT_TRIED, so `validation.discharge`'s
                # judgement was unreachable from this module's view and the route sat in
                # the denominator unable to discharge through any of its real outcomes.
                "VALIDATION_DEFECT_ESTABLISHED", "VALIDATION_SUPPORTS_CLAIM"):
            state, attempted, completed, exhausted = "DISCHARGED_RAN", True, True, True
        elif disposition in ("VALIDATION_OBSERVATION_ONLY", "VALIDATION_INCONCLUSIVE"):
            # It RAN and it settled nothing — an observation whose conformance was never
            # established, or a declared rule that fired in neither direction. Completed
            # and inconclusive, never discharged: a route that discharged by failing to
            # conclude is an exhaustion number that rises fastest where least was learned.
            state, attempted, completed, exhausted = (
                "COMPLETED_INCONCLUSIVE", True, True, True)
        elif disposition == "BUDGET_DEFERRED":
            state, blocker = "DEFERRED_BUDGET", disposition
        elif disposition == "SUPERSEDED_BY_ESTABLISHED_FAILURE":
            state, blocker = "DEFERRED_POLICY", disposition
        elif (route == "INDEPENDENT_RECONSTRUCTION"
              and disposition in ("INCONCLUSIVE", "AUTHORIZATION_BLOCKED")
              and outcome.launched == 0
              and "conformance_unproven" in (outcome.reason or "")):
            # The implemented generator and distinct verifier both ran and the proposed
            # reconstruction failed the non-invention/binding contract. Older cached
            # outcomes inherited AUTHORIZATION_BLOCKED from the backend's refusal to run
            # the rejected script; the recorded decision distinguishes that scientific
            # boundary from an execution gate.
            state, blocker = "DISCHARGED_BLOCKED", "CONFORMANCE_BLOCKED"
            attempted, completed, exhausted = True, True, True
        elif disposition == "AUTHORIZATION_BLOCKED" or _gate_closed(cfg, route):
            state, blocker, attempted = "GATE_CLOSED", "AUTHORIZATION_BLOCKED", True
        elif disposition in DISCHARGING_BLOCKERS:
            state, blocker = "DISCHARGED_BLOCKED", disposition
            attempted, completed, exhausted = True, True, True
        elif disposition == "INCONCLUSIVE" and outcome.launched > 0:
            state, attempted, completed, exhausted = (
                "COMPLETED_INCONCLUSIVE", True, True, True)
        elif disposition not in ("PENDING", "NOT_ATTEMPTED"):
            attempted = True
    elif plan is not None:
        reason = plan.reason
        if plan.superseded_by:
            # The only current re-plan is author code -> reconstruction, and it is entered
            # only after identity resolution conclusively failed on the checkout.
            state, blocker = "DISCHARGED_BLOCKED", "IDENTITY_BLOCKED"
            attempted, completed, exhausted = True, True, True
        elif plan.blocking_gate in ("outranked_by_a_central_target",
                                    "answered_by_another_target"):
            state, blocker = "DEFERRED_POLICY", plan.blocking_gate
        elif plan.blocking_gate:
            state, blocker, attempted = "GATE_CLOSED", plan.blocking_gate, True

    return RouteAttempt(
        question_id=q.question_id, route=route,
        target_ids=[o.target_id for o in objs], attempted=attempted, completed=completed,
        exhausted=exhausted, state=state, blocker=blocker,
        source_refs=_refs(q, objs, plan, outcome), reason=reason)


def refresh(ts: TargetSet, cfg=None) -> TargetSet:
    """Rebuild the durable route rows from the TargetSet's current production artifacts.

    Called after discovery and after every probe/resync update.  It is a fold, so a cached
    second pass cannot retain a flattering row after the underlying outcome changed.
    """
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
    import inspect

    # --- the partition is total, disjoint, and covers the vocabulary -------------------
    parts = DISCHARGING_STATES + CONFIGURATION_STATES + OPEN_STATES
    assert set(parts) == set(ROUTE_ATTEMPT_STATES), set(ROUTE_ATTEMPT_STATES) ^ set(parts)
    assert len(parts) == len(set(parts)), "a state cannot be in two groups"
    assert not (set(DISCHARGING_BLOCKERS) & set(NON_DISCHARGING_BLOCKERS))

    # --- signatures admit plain data only ----------------------------------------------
    for name, p in inspect.signature(state_for).parameters.items():
        assert str(p.annotation) in ("str", "bool"), name

    # --- running discharges ONLY on an admissible provenance ---------------------------
    assert state_for(ran=True, provenance="repo_exec") == "DISCHARGED_RAN"
    assert state_for(ran=True, provenance="reimpl_exec") == "DISCHARGED_RAN"
    for bad in ("synthesized", "template", "paper", ""):
        assert state_for(ran=True, provenance=bad) != "DISCHARGED_RAN", (
            f"{bad} ran but may conclude nothing, so the route was not carried out")

    # --- THE ANTI-GAMING RULE: a shut gate never discharges ----------------------------
    assert state_for(gate_closed=True) == "GATE_CLOSED"
    assert state_for(blocker="AUTHORIZATION_BLOCKED") == "GATE_CLOSED"
    assert state_for(budget_deferred=True) == "DEFERRED_BUDGET"
    assert state_for(policy_demoted=True) == "DEFERRED_POLICY"
    for s in CONFIGURATION_STATES:
        assert s not in DISCHARGING_STATES
    # and it outranks a typed blocker, because the gate is WHY the blocker was reached
    assert state_for(gate_closed=True, blocker="IDENTITY_BLOCKED") == "GATE_CLOSED", (
        "with the execution gate shut, identity is refused as a side effect; reporting "
        "IDENTITY_BLOCKED there attributes our own setting to the artifact")

    # --- a real blocker discharges -----------------------------------------------------
    for b in DISCHARGING_BLOCKERS:
        assert state_for(blocker=b) == "DISCHARGED_BLOCKED", b
    assert state_for() == "NOT_TRIED"

    # --- unimplemented routes are excluded from the denominator, and named -------------
    impl = implemented_routes()
    for r in UNIMPLEMENTED_ROUTES:
        assert r not in impl, r
    assert "AUTHOR_CODE_EXECUTION" in impl and "ARITHMETIC_RECHECK" in impl
    assert applicable_routes(("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION")) \
        == ("AUTHOR_CODE_EXECUTION",)
    assert applicable_routes(()) == () and applicable_routes(("NONE",)) == ()

    # --- coverage refuses an empty denominator rather than scoring 1.0 -----------------
    assert coverage([])["rate"] is None
    missing_row = coverage([], ("q1",))
    assert missing_row["rate"] == 0.0 and missing_row["open_because_untried"] == 1, (
        "a missing attempt row must stay in the question-owned denominator")

    class _A:
        def __init__(self, q, s): self.question_id, self.state = q, s

    both_done = coverage([_A("q1", "DISCHARGED_RAN"), _A("q1", "DISCHARGED_BLOCKED")])
    assert both_done["rate"] == 1.0 and both_done["exhausted"] == 1

    # one route open => the question is not exhausted
    partial = coverage([_A("q1", "DISCHARGED_RAN"), _A("q1", "NOT_TRIED")])
    assert partial["rate"] == 0.0 and partial["open_because_untried"] == 1

    # THE CASE THE WHOLE MODULE EXISTS FOR: a gate shut everything. Not 1.0.
    gated = coverage([_A("q1", "GATE_CLOSED"), _A("q2", "GATE_CLOSED")])
    assert gated["rate"] == 0.0, "shutting a gate must not produce a perfect score"
    assert gated["open_because_of_this_harness"] == 2
    assert gated["open_because_untried"] == 0

    mixed = coverage([_A("q1", "DISCHARGED_BLOCKED"), _A("q2", "GATE_CLOSED"),
                      _A("q3", "NOT_TRIED")])
    assert mixed == {**mixed, "questions": 3, "exhausted": 1,
                     "open_because_untried": 1, "open_because_of_this_harness": 1}
    assert mixed["rate"] == round(1 / 3, 4)
    print("harness.exhaustion self-check ok")


if __name__ == "__main__":
    _self_check()
