"""Whether a target is worth an experiment, and which kind — deterministically.

`python -m harness.planner` runs the self-check.

**"A model suggested an experiment" is not "an experiment is authorized."** That sentence
is the entire reason this module is a pure function over typed structural facts. A lens
can argue that a question matters; it reaches here only as `centrality`, which
`harness.discovery._centrality` derives from the paper's structure. Nothing a model wrote
is read.

**This module decides whether to TRY. `backends.authorize` decides whether it may RUN.**
They are kept apart deliberately and neither duplicates the other. Identity, capability,
commit verification and resource sufficiency all live in `authorize()` — CLAUDE.md
invariant 4 says only `authorize()` may permit repository execution, and a planner that
re-implemented half of those checks would become a second place where that decision is
made, which is how a gate turns advisory.

**Escalation is a property of the route ordering, not of a policy someone remembers.**
`_RESOLVING` names the two routes that can close a question with nothing running. If a
target has one, the planner takes it and no experiment happens. Artifact inspection is
deliberately NOT in that set: reading a repository can raise a concern, and it cannot
settle whether a printed number is reproducible, so a target whose question is about a
printed number escalates past it.
"""
from __future__ import annotations

from .artifacts import NECESSITY_FOR_ACTION, DiscoveredObject, PlanDecision


def _necessity(action: str) -> str:
    """The necessity judgement an action embodies. A pure re-labelling of a decision
    already made above: nothing here decides anything, and an unmapped action falls to
    NO_EXPERIMENT_NEEDED rather than inventing a warrant."""
    return NECESSITY_FOR_ACTION.get(action, "NO_EXPERIMENT_NEEDED")

# Which refusal each named blocker earns, and the sentence a reader gets. A table rather
# than three branches, so a blocker cannot be added to the vocabulary without a sentence
# and so the whole mapping is visible at once.
_ADDRESSING_REFUSAL = {
    "ADDRESS_UNRESOLVED": ("INFEASIBLE_ADDRESSING",
        "this review could not build an address in the parsed paper that it can re-derive "
        "for this claim, so there was nothing an execution could be reconciled against. A "
        "limit of what extraction recovered, not a finding about the paper."),
    "QUANTITY_UNPARSED": ("INFEASIBLE_REPORTING",
        "the claim is addressed, and the paper prints no single unambiguous quantity at "
        "that address for a reproduction to be compared against. A limit of how the result "
        "was reported, not a finding about the paper and not a limit of our extraction."),
    "NO_ROUTE": ("INFEASIBLE_ROUTE",
        "the claim is addressed and quantified, and no verification route this system has "
        "would settle the question it raises. A limit of this review's method inventory. "
        "The question is reported open rather than answered by a method that does not fit."),
}

# Routes that CLOSE a question without anything being executed. ONE route, not two.
#
# `PAPER_INTERNAL_CHECK` was here and should not have been: it re-verifies the QUOTATION
# behind a concern and establishes only that the concern cites the paper accurately.
# Having it in this set meant a citation check SUPPRESSED an experiment while settling
# nothing, which is the shape invariant 23 forbids in the opposite direction. Over the
# shipped corpus it was 100% of the paper-only resolutions, so every one of them was a
# suppressed escalation dressed as a settled question. The route is still taken when
# nothing better exists (`_CITATION_ONLY` below); it no longer outranks anything.
_RESOLVING = ("ARITHMETIC_RECHECK",)

# Taken only when no executable route exists. It costs nothing and does establish
# something small and real, so refusing it would discard information; its outcome is
# CITATION_VERIFIED_ONLY, which resolves nothing and leaves the question open.
_CITATION_ONLY = ("PAPER_INTERNAL_CHECK",)

# route -> the action taken when that route is chosen and its gates pass.
_ACTION_FOR_ROUTE = {
    "AUTHOR_CODE_EXECUTION": "AUTHOR_CODE_REPRODUCTION",
    "INDEPENDENT_RECONSTRUCTION": "INDEPENDENT_RECONSTRUCTION",
    "FOCUSED_VALIDATION_EXPERIMENT": "FOCUSED_VALIDATION_EXPERIMENT",
}

# Executable routes, most decisive first. The planner escalates in this order, so the
# authors' own code is always preferred over a reconstruction — invariant 15 exists
# because a reconstruction that differs is not evidence that the paper is wrong.
_EXECUTABLE_ORDER = ("AUTHOR_CODE_EXECUTION", "FOCUSED_VALIDATION_EXPERIMENT",
                     "INDEPENDENT_RECONSTRUCTION")

_WORTH_PURSUING = ("CENTRAL", "SUPPORTING")


def route_that_would_be_taken(routes: tuple[str, ...] = ()) -> str:
    """The route `plan()` would choose for an object offering `routes`. Pure, no object.

    Exists because `priority.order` was scoring every object on `routes[0]` — the FIRST
    route the discovery layer happened to append — and `plan()` chooses by the ladder
    below. Those disagree systematically rather than occasionally: measured over the
    eight-paper corpus, `routes[0]` is `ARTIFACT_INSPECTION` 254 times,
    `INDEPENDENT_RECONSTRUCTION` 405 times, `PAPER_INTERNAL_CHECK` 57 times and `NONE`
    162 times, and it is `AUTHOR_CODE_EXECUTION` exactly NEVER. So a target the planner
    would run against the authors' code was scored at the decisiveness of a static
    inspection, and a `PAPER_INTERNAL_CHECK` target — which this module states settles
    nothing — was scored as both the most decisive and the cheapest thing available.

    That ordering is what `SH_MAX_TARGETS` then truncates, so the budget was dropping
    targets ranked by a route nobody would take.

    Same precedence as `plan()`: a route that SETTLES with nothing running wins, then the
    executable ladder, then the citation-only fallback, then whatever is left.
    """
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
    `target_id`, one per target.

    **Read this, never `TargetSet.plans` directly, for anything that COUNTS.**
    `TargetSet.plans` became an ORDERED LOG rather than one entry per target the moment a
    re-plan could happen (`stages.probe.replan_after_author_code_exhausted`): a target
    whose AUTHOR_CODE_EXECUTION identity failed to bind gets a SECOND `PlanDecision`
    appended after the first, and the first stays in the list with `superseded_by` set
    rather than being removed. A `sum(1 for p in ts.plans if p.requires_execution)` over
    that list counts such a target TWICE — once for the superseded attempt, once for the
    one that replaced it — which is exactly the kind of overclaim the six-term funnel
    exists to prevent everywhere else. A LOOKUP by target_id (`{p.target_id: p for p in
    ts.plans}`) already gets this right for free, because a dict comprehension keeps the
    last value for a repeated key; this function is for callers that need the same
    de-duplication as a LIST, to sum or filter over.
    """
    by_id: dict[str, PlanDecision] = {}
    for p in plans:
        by_id[p.target_id] = p
    return list(by_id.values())


def classify(*, centrality: str, addressable: bool, route: str,
             artifact_available: bool, specification_complete: bool,
             environment_state: str, addressing_blocker: str = "NONE",
             investigation_open: bool = True,
             ) -> tuple[str, str, dict, str]:
    """(action, reason, gates, blocking_gate) for ONE route on ONE target.

    Vocabulary strings and booleans only, as in `harness.grading.derive` and
    `harness.priority.score`: no count, no number, no metric name, no paper identity, so
    a paper-specific or threshold-shaped rule cannot be written here even by accident.
    `investigation_open` is a BOOL for exactly that reason — the ASSESS phase does the
    reasoning and hands down one bit, so no severity, no finding and no count enters here.

    Every gate consulted is returned, not just the one that failed. "We did not run this"
    is only trustworthy when a reader can see which condition was not met.
    """
    gates = {
        "investigation_open": bool(investigation_open),
        "worth_pursuing": centrality in _WORTH_PURSUING,
        "structurally_addressable": bool(addressable),
        "route_exists": route in _ACTION_FOR_ROUTE,
        "specification_complete": bool(specification_complete),
        "artifact_available": bool(artifact_available),
        "environment_usable": environment_state != "blocked",
    }

    # FIRST, before every other gate, because it is prior to all of them: if the paper's
    # own evidence already establishes a material failure, whether this target is central,
    # addressable or executable cannot change the answer. Every gate is still REPORTED, so
    # the record shows what would have been checked had the question still been open.
    #
    # Not a refusal and not a blocker. The referee reached a conclusion; there is nothing
    # left for an experiment to add. That is `NO_EXPERIMENT_NEEDED` on the necessity axis
    # and `NOT_INVESTIGATED` on the evidence axis, because declining to run something
    # settles nothing (invariant 22).
    if not gates["investigation_open"]:
        return ("SUPERSEDED_BY_ESTABLISHED_FAILURE",
                "a material failure is already established from the paper's own evidence, "
                "so no execution can change what this review concluded. Reproducing a "
                "claim that has been disproved from the paper buys a referee nothing. "
                "This is a property of the review's conclusion and says nothing about "
                "this target.", gates, "investigation_open")

    if not gates["worth_pursuing"]:
        return ("NO_EXPERIMENT_NEEDED",
                f"centrality is {centrality or 'UNASSESSED'}: the paper's conclusion does not "
                f"rest on this, so spending an execution on it would buy nothing a reviewer "
                f"needs.", gates, "worth_pursuing")
    if not gates["structurally_addressable"]:
        # THREE REFUSALS, not one. `addressable` is a conjunction of three requirements and
        # this branch used to answer for all three with one sentence: "this review could
        # not build a re-derivable address for the claim". Over the shipped corpus that
        # sentence was printed 39 times across six of seven reviews and was false every
        # time. All 52 targets carrying it had a RESOLVED address, and in one review it sat
        # two lines under the address it claimed not to have; the real blocker in 52 of 52
        # was the third requirement, that no route applies.
        #
        # Each of the three is a limit of something different and a reviewer needs to know
        # which: this harness's extraction, the paper's reporting, or this harness's method
        # inventory. The parameter is a vocabulary STRING like every other parameter here,
        # so the signature discipline that makes a paper-specific rule inexpressible holds.
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
    if route in ("INDEPENDENT_RECONSTRUCTION", "FOCUSED_VALIDATION_EXPERIMENT") \
            and not gates["specification_complete"]:
        return ("INFEASIBLE_SPECIFICATION",
                "the paper does not specify enough to build the experiment that would answer "
                "this, and inventing the missing half would produce a result about our "
                "reconstruction rather than about the paper. Reported unresolved.",
                gates, "specification_complete")
    if not gates["environment_usable"]:
        return ("INFEASIBLE_ENVIRONMENT",
                "the experiment cannot be set up on this host. A failed environment is a fact "
                "about this machine and is never a failed reproduction.",
                gates, "environment_usable")

    action = _ACTION_FOR_ROUTE[route]
    return (action, f"an experiment is justified and a {route.lower().replace('_', ' ')} route "
                    f"exists; authorization is decided separately by backends.authorize.",
            gates, "")


def _classify_for(obj: DiscoveredObject, **kw) -> tuple[str, str, dict, str]:
    """`classify`, with the object's OWN account of what it lacks appended to the reason.

    `classify` stays pure over closed vocabulary — that is the property that makes a
    paper-specific rule inexpressible in it. But its refusal has to be readable, and
    "not structurally checkable" is not readable without naming which requirement was
    unmet. `DiscoveredObject.evidence_requirements` is written by `harness.discovery` from
    the same structural facts, so quoting it here adds information without adding
    authority.
    """
    kw.setdefault("addressing_blocker", obj.addressing_blocker)
    action, reason, gates, blocking = classify(**kw)
    if blocking == "structurally_addressable" and obj.evidence_requirements:
        reason = f"{reason} What is missing: {'; '.join(obj.evidence_requirements)}."
    return action, reason, gates, blocking


def _why_material(obj: DiscoveredObject) -> str:
    """What turns on this target's answer, in the paper's own structural terms.

    Assembled from `centrality` — which `harness.discovery._centrality` derives from where
    the quantity appears, not from anything a lens argued — so it states a fact about the
    paper's layout rather than repeating a model's opinion of importance.
    """
    return {
        "CENTRAL": "the paper's headline conclusion rests on this quantity",
        "SUPPORTING": "this quantity supports the paper's conclusion without carrying it",
        "PERIPHERAL": "the paper's conclusion does not rest on this",
    }.get(obj.centrality, "the paper's structure does not say what rests on this")


def _cheaper_routes_ruled_out(obj: DiscoveredObject) -> tuple[str, str]:
    """(paper_only_insufficient_because, inspection_insufficient_because).

    Both are statements about which routes this target does NOT have, read off `routes` —
    which `harness.discovery._routes` derived structurally. This is the counted half of
    invariant 20: a cheaper route is taken when one exists, and when an execution happens
    anyway the record says, per target, which cheaper routes were absent and why that
    absence matters.
    """
    paper_only = (
        "" if any(r in obj.routes for r in _RESOLVING) else
        "the question is not a check against the paper's own printed content: no arithmetic "
        "recheck applies, so nothing in the paper settles it. A PAPER_INTERNAL_CHECK, where "
        "one exists, re-verifies the concern's quotation and does not settle the concern")
    inspection = (
        "reading the released code can raise a concern about how a number was produced and "
        "cannot establish what the number is, so it cannot settle whether the printed value "
        "is reproducible"
        if "ARTIFACT_INSPECTION" in obj.routes else
        "no artifact-inspection route applies to this target either")
    return paper_only, inspection


def plan(obj: DiscoveredObject, *, artifact_available: bool = False,
         specification_complete: bool = False,
         environment_state: str = "unassessed",
         investigation_open: bool = True,
         author_code_exhausted: bool = False,
         attempt: int = 1) -> PlanDecision:
    """The decision for one discovered object: resolve it cheaply, escalate, or refuse.

    Order of consideration, and it is the escalation policy in three lines:

      1. a route that can close the question with nothing running  ->  take it
      2. otherwise the most decisive executable route              ->  gate it
      3. otherwise                                                 ->  no experiment

    Every branch also records the WHY-YES half — `why_material`, which cheaper routes were
    ruled out and on what grounds, what the run would discriminate between, and what it
    should produce if the paper is right. `gates` is the why-not half and was already
    complete; a system that spends compute owes both, and "the gates passed" is not an
    answer to "why did you spend it on this?".

    **`author_code_exhausted` is the re-plan primitive Step 6 adds.** Identity resolution
    against the AUTHOR_CODE_EXECUTION route is a fact only `stages/probe.py` can establish
    — it requires an actual checkout — so a first `plan()` call here cannot know whether
    the authors' code will bind. When it tried and did not, the caller re-invokes `plan()`
    with this set, which removes AUTHOR_CODE_EXECUTION from consideration for THIS call
    only; the object's own `routes` still decide what remains, so a re-plan reaches
    INDEPENDENT_RECONSTRUCTION only where discovery already put it there — which, per
    `discovery._routes`, is only when the paper also specifies enough to attempt one. This
    is a per-call filter, not a mutation: `obj.routes` is never changed, and a fresh
    `plan()` call without the flag reproduces the original decision exactly.

    `attempt` is carried straight onto `PlanDecision.attempt` and decided by nothing here;
    the caller numbers its own attempts, and `plan()` stays a pure function of what it is
    told.
    """
    why_material = _why_material(obj)
    paper_only_no, inspection_no = _cheaper_routes_ruled_out(obj)
    executable_order = tuple(r for r in _EXECUTABLE_ORDER
                             if not (author_code_exhausted and r == "AUTHOR_CODE_EXECUTION"))

    # THE EARLY STOP, ahead of the route ladder. A cheap route that SETTLES something is
    # still worth taking when the paper has already been disproved -- it costs nothing and
    # the referee record is better for it -- but nothing that would EXECUTE is, so the
    # branch is taken before `executable_order` is consulted at all.
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
    # Never invented here: the readings a run would discriminate between are the finding's
    # own counter-explanations, carried through discovery untouched.
    competing = list(obj.counter_explanations)

    cheap = next((r for r in obj.routes if r in _RESOLVING), "")
    if cheap:
        # The FULL gate dict, not `{"resolvable_without_execution": True}`. This branch
        # returned before `classify` was ever called, so centrality and addressability were
        # never consulted for the branch that produced every paper-only resolution in the
        # corpus, and a PERIPHERAL, unaddressable object could reach RESOLVED_FROM_PAPER
        # with `concerns_the_paper` true. One such target shipped.
        _, _, gates, _ = classify(
            centrality=obj.centrality, addressable=bool(obj.harness_addressable),
            route=cheap, artifact_available=artifact_available,
            specification_complete=specification_complete,
            environment_state=environment_state,
            addressing_blocker=obj.addressing_blocker,
            investigation_open=investigation_open)
        gates["resolvable_without_execution"] = True
        return PlanDecision(
            target_id=obj.target_id, action="PAPER_ONLY_RESOLUTION", route=cheap,
            reason="the paper's own printed composition can be re-evaluated here, so no "
                   "execution is warranted to settle whether it holds.",
            gates=gates, requires_execution=False,
            necessity="NO_EXPERIMENT_NEEDED", why_material=why_material,
            inspection_insufficient_because=inspection_no, competing_explanations=competing,
            attempt=attempt)

    route = next((r for r in executable_order if r in obj.routes), "")
    if not route:
        # A citation check beats ARTIFACT_INSPECTION and NONE here, because it is free and
        # establishes something. It beats NEITHER of the executable routes above, which is
        # the whole point of moving it out of `_RESOLVING`: it no longer suppresses an
        # escalation, it only fills a gap where there was nothing to escalate to.
        # STATIC ARTIFACT INSPECTION, and only here. Three conditions, each load-bearing:
        #
        #   * this arm is reached only when NO executable route applies, so inspection can
        #     never suppress an execution. That is the defect `PAPER_INTERNAL_CHECK` had
        #     from inside `_RESOLVING`, where it cancelled 100% of the corpus's
        #     escalations by being cheap rather than by settling anything;
        #   * the authors must have published something — `artifact_available`. Reading a
        #     repository that was never acquired is not a route;
        #   * the question's answer must not be a MEASUREMENT. "Does this code produce
        #     91.4?" is not answerable by reading it, and a route that claimed otherwise
        #     would let a reading of the source acquit or convict a number.
        #
        # It is tried BEFORE the citation re-check because it can settle an artifact-only
        # question and the citation re-check settles nothing by construction.
        from .artifact_evidence import requires_execution
        if ("ARTIFACT_INSPECTION" in obj.routes and artifact_available
                and not requires_execution(obj.question_kind)):
            _, _, gates, _ = classify(
                centrality=obj.centrality, addressable=bool(obj.harness_addressable),
                route="ARTIFACT_INSPECTION", artifact_available=artifact_available,
                specification_complete=specification_complete,
                environment_state=environment_state,
                addressing_blocker=obj.addressing_blocker,
                investigation_open=investigation_open)
            gates["resolvable_without_execution"] = False
            gates["answer_is_a_measurement"] = False
            return PlanDecision(
                target_id=obj.target_id, action="ARTIFACT_INSPECTION_ONLY",
                route="ARTIFACT_INSPECTION",
                reason="no executable route applies and the authors published code, so "
                       "the pinned checkout is read for what it can establish about this "
                       "question. Reading the artifact cannot establish that a reported "
                       "result is wrong; it can establish what the released code does.",
                gates=gates, requires_execution=False,
                necessity="NO_EXPERIMENT_NEEDED", why_material=why_material,
                paper_only_insufficient_because=paper_only_no,
                competing_explanations=competing, attempt=attempt)

        cite = next((r for r in obj.routes if r in _CITATION_ONLY), "")
        if cite:
            _, _, gates, _ = classify(
                centrality=obj.centrality, addressable=bool(obj.harness_addressable),
                route=cite, artifact_available=artifact_available,
                specification_complete=specification_complete,
                environment_state=environment_state,
                addressing_blocker=obj.addressing_blocker,
                investigation_open=investigation_open)
            gates["resolvable_without_execution"] = False
            return PlanDecision(
                target_id=obj.target_id, action="PAPER_ONLY_RESOLUTION", route=cite,
                reason="no executable route applies, so the only thing available is to "
                       "re-verify the quotation behind the concern. That establishes the "
                       "concern cites the paper accurately and settles nothing else.",
                gates=gates, requires_execution=False,
                necessity="NO_EXPERIMENT_NEEDED", why_material=why_material,
                paper_only_insufficient_because=paper_only_no,
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
    expected = (f"the run reproduces the paper’s stated value of {obj.expected_value:g} for "
                f"{obj.metric or 'this quantity'}"
                if obj.expected_value is not None else
                f"the run produces a value for {obj.metric or 'this quantity'} that the paper’s "
                f"own statement can be reconciled against")
    return PlanDecision(
        target_id=obj.target_id, action=action, route=route, reason=reason, gates=gates,
        blocking_gate=blocking,
        requires_execution=action in ("AUTHOR_CODE_REPRODUCTION", "INDEPENDENT_RECONSTRUCTION",
                                      "FOCUSED_VALIDATION_EXPERIMENT", "MECHANISM_TEST_ONLY"),
        attempt=attempt,
        necessity=_necessity(action), why_material=why_material,
        paper_only_insufficient_because=paper_only_no,
        inspection_insufficient_because=inspection_no,
        competing_explanations=competing, expected_observation=expected)


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    for name, p in inspect.signature(classify).parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation}"

    central = DiscoveredObject(target_id="T1", centrality="CENTRAL", harness_addressable=True,
                               routes=["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"])
    d = plan(central, artifact_available=True, environment_state="ok")
    assert d.action == "AUTHOR_CODE_REPRODUCTION" and d.requires_execution

    # no repository: refused as an ARTIFACT limit, never as a paper failure
    d = plan(central, artifact_available=False)
    assert d.action == "INFEASIBLE_ARTIFACT" and not d.requires_execution
    assert "Nothing about the paper follows" in d.reason

    cheap = DiscoveredObject(target_id="T2", centrality="CENTRAL", harness_addressable=True,
                             routes=["ARITHMETIC_RECHECK", "AUTHOR_CODE_EXECUTION"])
    d = plan(cheap, artifact_available=True)
    assert d.action == "PAPER_ONLY_RESOLUTION" and not d.requires_execution, (
        "a question answerable from the paper must never escalate to an execution")

    peripheral = DiscoveredObject(target_id="T3", centrality="PERIPHERAL",
                                  harness_addressable=True, routes=["AUTHOR_CODE_EXECUTION"])
    assert plan(peripheral, artifact_available=True).action == "NO_EXPERIMENT_NEEDED"

    vague = DiscoveredObject(target_id="T4", centrality="CENTRAL", harness_addressable=True,
                             routes=["FOCUSED_VALIDATION_EXPERIMENT"])
    d = plan(vague, artifact_available=True, specification_complete=False)
    assert d.action == "INFEASIBLE_SPECIFICATION" and not d.requires_execution
    assert "inventing the missing half" in d.reason
    d = plan(vague, artifact_available=True, specification_complete=True, environment_state="ok")
    assert d.action == "FOCUSED_VALIDATION_EXPERIMENT" and d.requires_execution

    blocked = plan(central, artifact_available=True, environment_state="blocked")
    assert blocked.action == "INFEASIBLE_ENVIRONMENT" and not blocked.requires_execution

    unaddressable = DiscoveredObject(target_id="T5", centrality="CENTRAL",
                                     harness_addressable=False, routes=["AUTHOR_CODE_EXECUTION"])
    d = plan(unaddressable, artifact_available=True)
    assert d.action == "INFEASIBLE_ADDRESSING" and d.blocking_gate == "structurally_addressable"
    assert d.necessity == "EXPERIMENT_NOT_EXECUTABLE"

    # --- the why-yes half is recorded on every branch ---------------------------------
    d = plan(central, artifact_available=True, environment_state="ok")
    assert d.necessity == "EXPERIMENT_WARRANTED"
    assert d.why_material and d.paper_only_insufficient_because and d.expected_observation
    assert "cannot establish what the number is" in d.inspection_insufficient_because, (
        "artifact inspection was available and must be recorded as ruled out, not absent")
    assert plan(cheap, artifact_available=True).necessity == "NO_EXPERIMENT_NEEDED"
    assert plan(vague, artifact_available=True,
                specification_complete=False).necessity == "EXPERIMENT_UNDERSPECIFIED"
    assert plan(central, artifact_available=False).necessity == "EXPERIMENT_NOT_EXECUTABLE"
    # counter-explanations are carried, never invented
    with_alts = DiscoveredObject(target_id="T6", centrality="CENTRAL", harness_addressable=True,
                                 routes=["AUTHOR_CODE_EXECUTION"],
                                 counter_explanations=["a different split", "a different seed"])
    assert plan(with_alts, artifact_available=True,
                environment_state="ok").competing_explanations == ["a different split",
                                                                   "a different seed"]
    assert plan(central, artifact_available=True,
                environment_state="ok").competing_explanations == []
    # every necessity a plan can produce is in the closed vocabulary
    from .artifacts import EXPERIMENT_NECESSITY
    for obj in (central, cheap, peripheral, vague, unaddressable, with_alts):
        assert plan(obj).necessity in EXPERIMENT_NECESSITY

    # every action a plan can produce is in the closed vocabulary
    from .artifacts import PLAN_ACTIONS
    for obj in (central, cheap, peripheral, vague, unaddressable):
        assert plan(obj).action in PLAN_ACTIONS

    # --- THE EARLY STOP ---------------------------------------------------------------
    # With the investigation closed, NOTHING executes, whatever the target looks like.
    for obj in (central, peripheral, vague, unaddressable, with_alts):
        d = plan(obj, artifact_available=True, specification_complete=True,
                 environment_state="ok", investigation_open=False)
        assert not d.requires_execution, obj.target_id
        assert d.action == "SUPERSEDED_BY_ESTABLISHED_FAILURE", (obj.target_id, d.action)
        assert d.necessity == "NO_EXPERIMENT_NEEDED"
        assert d.gates["investigation_open"] is False
        # every gate is still REPORTED, so the record shows what would have been checked
        assert set(d.gates) >= {"worth_pursuing", "structurally_addressable",
                                "route_exists", "artifact_available"}
        assert "buys a referee nothing" in d.reason
        assert "says nothing about" in d.reason, "a stop is never an accusation"

    # A route that SETTLES something for free is still taken: it costs nothing and the
    # referee record is better for it. Only execution is suppressed.
    d = plan(cheap, artifact_available=True, investigation_open=False)
    assert d.action == "PAPER_ONLY_RESOLUTION" and not d.requires_execution

    # And with the investigation OPEN nothing changed — the default is the old behaviour.
    assert plan(central, artifact_available=True,
                environment_state="ok").action == "AUTHOR_CODE_REPRODUCTION"

    # --- STEP 6: author_code_exhausted re-plans, and current_plans never double-counts -
    both_routes = DiscoveredObject(
        target_id="T6", centrality="CENTRAL", harness_addressable=True,
        routes=["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    first = plan(both_routes, artifact_available=True, specification_complete=True,
                environment_state="ok")
    assert first.route == "AUTHOR_CODE_EXECUTION" and first.attempt == 1
    fallback = plan(both_routes, artifact_available=True, specification_complete=True,
                    environment_state="ok", author_code_exhausted=True, attempt=2)
    assert fallback.route == "INDEPENDENT_RECONSTRUCTION" and fallback.attempt == 2
    first.superseded_by = fallback.route     # what the caller (`stages.probe`) records

    # THE DOUBLE-COUNT THIS FUNCTION EXISTS TO PREVENT. Both PlanDecisions share a
    # target_id and both have `requires_execution=True`; a naive sum over the raw list
    # would count this ONE target as TWO warranting an experiment.
    assert sum(1 for p in [first, fallback] if p.requires_execution) == 2, (
        "the raw list DOES double-count — that is the defect, reproduced")
    deduped = current_plans([first, fallback])
    assert len(deduped) == 1 and deduped[0] is fallback, (
        "current_plans keeps exactly one entry per target: the LAST one")
    assert sum(1 for p in deduped if p.requires_execution) == 1

    # No re-plan at all still yields exactly the one entry it always did.
    assert current_plans([first]) == [first]
    assert current_plans([]) == []
    print("harness.planner self-check ok")


if __name__ == "__main__":
    _self_check()
