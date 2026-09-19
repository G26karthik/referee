"""S2b — what this paper offers to check, in priority order, with a plan for each.

`python -m harness.stages.discover` runs the self-check.

This phase is entirely deterministic and consults no model. It runs after the lenses have
read the paper and before anything is executed, which is where it has to be: the cheap
reasoning is already done, and the expensive part has not started, so this is the only
point at which "is an experiment justified?" can be asked before the cost is incurred.

**What it replaces.** `stages/probe.build_spec` used to pick a target as a side effect:

    candidates = [f for f in rank(findings) if f.verifiable_by_experiment]
    target = candidates[0] if candidates else None

One target, chosen by an unchecked model boolean, ordered by a *report display* sort, with
no fallback when it blocked. Everything that survives from that line is the idea that
there is a target; the set, the ordering, the routes and the reason are all derived here
now, and `verifiable_by_experiment` survives only as `DiscoveredObject.proposed_by_lens`.

**Three artifacts, one file.** `discovery/targets.json` holds the questions, the objects,
the plans and the outcomes together, because they are only meaningful as a set: a target
with no plan is an unexplained omission, and a plan with no outcome is an unfinished one.
`TargetSet.extraction_coverage` carries the denominator, so an empty target set caused by
failed table extraction can be told apart from a paper with nothing to check.
"""
from __future__ import annotations

from pathlib import Path

from .. import (claims, discovery, exhaustion, planner, priority,
                questions as questions_mod, reimplement, state)
from ..artifacts import (DiscoveredObject, PaperDoc, PlanDecision, TargetOutcome,
                         TargetSet)
from ..config import Config
from . import audit as audit_stage

# What a plan that will never execute means for its target. Every one of these is a fact
# about the paper's specification, the artifact, or this host — never a finding about the
# paper, which is why `stages/report` reads none of them as evidence against it.
_DISPOSITION_FOR_ACTION = {
    # The conservative half of the paper-only route, and deliberately not the resolving
    # one. `build` dispatches PAPER_ONLY_RESOLUTION to `_paper_only_outcome`, which is the
    # only place entitled to decide WHICH of the two branches applies, so this entry is
    # reached only if that dispatch is ever bypassed. It names the branch that establishes
    # nothing, because a fallback that asserts a resolution nobody computed is worse than
    # one that asserts too little.
    "PAPER_ONLY_RESOLUTION": "CITATION_VERIFIED_ONLY",
    "INFEASIBLE_SPECIFICATION": "SPECIFICATION_BLOCKED",
    # Distinct from the line above on purpose: SPECIFICATION_BLOCKED says the PAPER did
    # not specify the experiment, ARTIFACT_BLOCKED here says WE could not build an
    # address for the claim. Collapsing them would let a limit of our own extraction be
    # read as a gap in the authors' method section.
    "INFEASIBLE_ADDRESSING": "ADDRESSING_BLOCKED",
    # The two that used to arrive here as INFEASIBLE_ADDRESSING. See
    # `planner._ADDRESSING_REFUSAL`: one is a limit of the paper's reporting and one of
    # this review's method inventory, and neither is a limit of our extraction.
    "INFEASIBLE_REPORTING": "REPORTING_BLOCKED",
    "INFEASIBLE_ROUTE": "NO_ROUTE_AVAILABLE",
    "DEFERRED_TO_ANOTHER_TARGET": "NO_EXPERIMENT_NEEDED",
    "OUTRANKED_BY_CENTRAL_TARGET": "NO_EXPERIMENT_NEEDED",
    # The ASSESS phase established a material failure from the paper's own evidence, so
    # the investigation branch stopped. Its own disposition, not folded into
    # NOT_ATTEMPTED: "we reached a conclusion and stopped" and "nothing was pursued" are
    # different facts, and only the first is a review outcome.
    "SUPERSEDED_BY_ESTABLISHED_FAILURE": "SUPERSEDED_BY_ESTABLISHED_FAILURE",
    "INFEASIBLE_ARTIFACT": "ARTIFACT_BLOCKED",
    "INFEASIBLE_ENVIRONMENT": "ENVIRONMENT_BLOCKED",
    "NO_EXPERIMENT_NEEDED": "NOT_ATTEMPTED",
}


def targets_path_in(root: Path):
    """Where the target set lives inside a project directory. The ONE spelling.

    `probe` holds a project root and `discover` holds a (cfg, pid); before this
    they spelled the same three-component path five times between them.
    """
    return root / "discovery" / "targets.json"


def targets_path(cfg: Config, pid: str):
    return targets_path_in(state.project_dir(cfg, pid))


def load(cfg: Config, pid: str) -> TargetSet | None:
    p = targets_path(cfg, pid)
    if not p.exists():
        return None
    try:
        return TargetSet(**state.read_json(p))
    except (OSError, ValueError):
        return None


def _paper_only_outcome(obj: DiscoveredObject, plan: PlanDecision) -> TargetOutcome:
    """Pursue a target against the paper's own printed content. Nothing runs.

    Two routes reach here, they establish DIFFERENT THINGS, and they therefore end in
    different dispositions:

      ARITHMETIC_RECHECK    the composition the paper printed was re-evaluated by
                            `harness.claims.parse_quantity`, operand by operand. Either it
                            agrees with the printed total or it does not, and both are
                            resolutions -> PAPER_ONLY_RESOLVED.
      PAPER_INTERNAL_CHECK  the quotation behind the concern was re-verified against the
                            parsed document, so the concern rests on text that is really
                            there. Whether the concern is CORRECT remains the findings
                            pipeline's question and this route does not answer it, so it
                            resolves NOTHING -> CITATION_VERIFIED_ONLY.

    The two shared PAPER_ONLY_RESOLVED until the seven-paper corpus made the consequence
    visible: all 20 of its paper-only outcomes were PAPER_INTERNAL_CHECK, so every review
    printed the very sentence its findings disputed under `## What held up`, and every
    funnel reported those targets as having "settled a question about the paper". The
    honest count was zero. Invariant 1 already guarantees a kept finding's quote was
    re-verified; restating that precondition per target as a resolution counted an
    entry requirement as a result.
    """
    q = obj.ref.quantity if (obj.ref and obj.ref.quantity) else None
    if plan.route == "ARITHMETIC_RECHECK" and q and q.expression:
        agrees = q.arithmetic_ok
        # `agrees is False` ESTABLISHES A DEFECT — Tier 1, `TargetOutcome.
        # establishes_failure`. Whether that defect is MATERIAL to a central scientific
        # claim is a separate question decided later, by `harness.materiality`, from where
        # the claim sits in the paper; a contradicted composition in an appendix is
        # established and does not stop the paper. It is its own disposition, not a second
        # reading of PAPER_ONLY_RESOLVED, for exactly the reason CITATION_VERIFIED_ONLY is
        # its own disposition: two opposite conclusions sharing one label told a reader
        # neither.
        disposition = ("PAPER_ONLY_RESOLVED" if agrees
                       else "PAPER_ARITHMETIC_CONTRADICTION")
        return TargetOutcome(
            target_id=obj.target_id, disposition=disposition,
            action=plan.action, route=plan.route, provenance="paper",
            reason=(f"the paper prints {q.expression} = {q.raw}; re-evaluated here that is "
                    f"{'consistent' if agrees else 'NOT consistent'} with the printed "
                    f"total. No execution was required to establish this."))
    return TargetOutcome(
        target_id=obj.target_id, disposition="CITATION_VERIFIED_ONLY",
        action=plan.action, route=plan.route, provenance="paper",
        reason=("the quotation behind this concern was re-verified against the parsed "
                "paper, so the concern cites the paper accurately. That is all this route "
                "establishes: whether the concern is correct was not investigated here, "
                "and this target settles nothing about the paper."))


def _one_per_experiment(objects: list[DiscoveredObject],
                        plans: list[PlanDecision]) -> list[PlanDecision]:
    """Targets answered by the SAME run are one experiment, not many.

    A paper printing 140 numbers from one benchmark under one metric does not pose 140
    questions an execution could answer separately: one run of that command produces the
    evidence for all of them, and pursuing each in turn re-runs the same thing. Measured
    on APT, where 144 targets were independently judged to justify an experiment and every
    one of them routed through the same entrypoint — a list, not a plan, and one that made
    the execution-trigger rate meaningless again by sheer count.

    Grouped by (route, experiment, metric), which is what a run is actually determined by,
    and the highest-priority member of each group is kept. `harness.priority` has already
    ordered them, so the representative is the one whose claim the paper's conclusion most
    rests on. This is a DE-DUPLICATION, not a budget: nothing is dropped for being
    expensive, and the campaign's own target records make the same distinction when they
    note that a set of targets shares one blocker.
    """
    seen: dict[tuple[str, str, str], str] = {}
    out: list[PlanDecision] = []
    for obj, plan in zip(objects, plans):
        if not plan.requires_execution:
            out.append(plan)
            continue
        # Two targets are the same experiment only when they SAY they are. Grouping on
        # `("", "")` treats "we know nothing about this one" as "these are identical",
        # which is the opposite of what the metadata means — it collapsed FinChain's
        # 2,900-test-case composition claim into an unrelated prose claim whose value was
        # 10, purely because neither carried a benchmark or a metric name. A target with
        # no declared experiment and no declared metric is grouped with nothing.
        if not (obj.experiment and obj.metric):
            out.append(plan)
            continue
        key = (plan.route, obj.experiment, obj.metric)
        first = seen.get(key)
        if first is None:
            seen[key] = obj.target_id
            out.append(plan)
            continue
        out.append(PlanDecision(
            target_id=plan.target_id, action="NO_EXPERIMENT_NEEDED", route=plan.route,
            reason=(f"the same run answers this and {first}, which is being pursued: same "
                    f"route, same experiment, same metric. Reported alongside it rather "
                    f"than re-run."),
            gates=dict(plan.gates, answered_by_another_target=True),
            blocking_gate="answered_by_another_target", requires_execution=False))
    return out


def _demote_when_a_central_target_is_being_pursued(
        objects: list[DiscoveredObject], plans: list[PlanDecision]) -> list[PlanDecision]:
    """A SUPPORTING target does not earn an execution while a CENTRAL one is available.

    `harness.planner` decides one target at a time, which is what keeps it a pure function
    over closed vocabulary — and it is therefore blind to the fact that a paper has 393
    printed numbers, every one of which is in principle re-derivable. Over the real corpus
    that produced an execution-trigger rate of 0.75: three quarters of everything
    discovered "justified" an experiment, and what actually limited the work was
    `cfg.max_targets`, a budget. A budget doing the planner's job is the planner not doing
    it.

    The rule is a SET-level fact and so it lives here rather than in the planner: when at
    least one CENTRAL target is going to be pursued, a SUPPORTING one is reported as an
    open question instead. When none is, SUPPORTING targets stand — a paper whose central
    claims are unaddressable must not silently become a paper nothing is checked on.

    Deterministic given the ordering, and the ordering is `harness.priority`'s.
    """
    if not any(p.requires_execution and o.centrality == "CENTRAL"
               for o, p in zip(objects, plans)):
        return plans
    out: list[PlanDecision] = []
    for obj, plan in zip(objects, plans):
        if plan.requires_execution and obj.centrality != "CENTRAL":
            out.append(PlanDecision(
                target_id=plan.target_id, action="NO_EXPERIMENT_NEEDED", route=plan.route,
                reason=(f"a {obj.centrality.lower()} result, and at least one CENTRAL target "
                        f"is being pursued. Reported as an open question rather than run: "
                        f"spending an execution here would buy less than the central one "
                        f"already costs."),
                gates=dict(plan.gates, outranked_by_a_central_target=True),
                blocking_gate="outranked_by_a_central_target", requires_execution=False))
        else:
            out.append(plan)
    return out


# Dispositions that mean the question was actually SETTLED, not merely that a route
# reached a terminal state. Kept in sync with the resolving branches `exhaustion._attempt_for`
# recognises (`PAPER_ONLY_RESOLVED` and `PAPER_ARITHMETIC_CONTRADICTION` off the paper-only
# route; `REPRODUCED`, `FAILED_REPRODUCTION`, `VALIDATION_DEFECT_ESTABLISHED` and
# `VALIDATION_SUPPORTS_CLAIM` off an admissible, launched execution) — deliberately not the
# same list as "reached a terminal state", which every disposition here is.
_SETTLING_DISPOSITIONS = (
    "PAPER_ONLY_RESOLVED", "PAPER_ARITHMETIC_CONTRADICTION",
    "REPRODUCED", "FAILED_REPRODUCTION",
    "VALIDATION_DEFECT_ESTABLISHED", "VALIDATION_SUPPORTS_CLAIM",
)


def _undefer_when_the_central_target_did_not_settle(
        objects: list[DiscoveredObject], plans: list[PlanDecision],
        prior_outcomes: dict[str, TargetOutcome] | None) -> list[PlanDecision]:
    """Give a deferred SUPPORTING target its own turn once its CENTRAL sibling's prior
    attempt is known to have settled nothing.

    `_demote_when_a_central_target_is_being_pursued` is right on the first pass, where no
    outcome for the central target exists yet and spending the budget there first is the
    only defensible order. It stops being right once a prior run's own recorded outcome
    shows every central sibling reached a terminal, NON-settling disposition (refused,
    blocked, inconclusive -- never one that actually resolved the question): the budget
    that outranked the supporting target has already been spent and produced nothing, and
    holding the supporting target back forever turns a resource-ordering heuristic into a
    standing refusal with no scientific content, the exact "open only because of this
    harness's own configuration" gap Section 3 of a first-pass review must not leave
    unexamined when the route itself remains affordable.

    Absent a prior run (`prior_outcomes` empty or None, i.e. this paper's first discover
    pass), nothing here changes and the original demotion stands untouched.
    """
    if not prior_outcomes:
        return plans
    by_question: dict[str, list[DiscoveredObject]] = {}
    for obj in objects:
        if obj.question_id:
            by_question.setdefault(obj.question_id, []).append(obj)
    out: list[PlanDecision] = []
    for obj, plan in zip(objects, plans):
        if plan.blocking_gate != "outranked_by_a_central_target":
            out.append(plan)
            continue
        centrals = [o for o in by_question.get(obj.question_id, [])
                    if o.centrality == "CENTRAL" and o.target_id != obj.target_id]
        central_outcomes = [prior_outcomes[o.target_id] for o in centrals
                            if o.target_id in prior_outcomes]
        # Un-defer only once EVERY central sibling has a known prior outcome (nobody is
        # still pending, so nothing is jumping the queue) and NONE of them settled.
        all_known = bool(centrals) and len(central_outcomes) == len(centrals)
        any_settled = any(o.disposition in _SETTLING_DISPOSITIONS for o in central_outcomes)
        if all_known and not any_settled:
            dispositions = ", ".join(sorted({o.disposition for o in central_outcomes}))
            out.append(plan.model_copy(update=dict(
                reason=("a supporting result, previously deferred while a central target "
                        "for this question was pursued; that central target's own prior "
                        f"attempt reached a terminal, non-settling disposition "
                        f"({dispositions}), so this target is no longer outranked and "
                        "gets its own attempt."),
                gates=dict(plan.gates, outranked_by_a_central_target=False,
                          central_target_did_not_settle=True),
                blocking_gate="", requires_execution=True)))
        else:
            out.append(plan)
    return out


def _question_for_every_executable_target(
        objects: list[DiscoveredObject], plans: list[PlanDecision],
        qs: list) -> list:
    """Mint a ReviewQuestion for every target that will EXECUTE and has none.

    **The question is the semantic unit of investigation, so an execution that cannot name
    one is compute nobody can account for.** `harness.questions.derive` runs over FINDINGS,
    and the two object sources that actually reach execution on this corpus come from the
    extractor instead — a printed table result and a prose composition. Both arrived with
    `question_id=""`, so the funnel could report sixteen launches and no question they were
    answering, and `sync_questions` skipped them entirely.

    Minted for the executable targets ONLY, and deliberately not for all 878 discovered
    objects. A question the harness asks about every printed number in a paper is not a
    referee's open question, and filing hundreds of them under `## Open questions for the
    reviewer` would undo the bound invariant 19 puts on that report. What earns a question
    here is that this run is about to spend something on it.

    Nothing is invented: `questions.for_object` states, from the object's own
    DISCOVERY_KIND and centrality, what checking it would settle. `lens` and
    `from_finding` are left empty, so a reader can always tell a question the harness
    asked from one a lens raised.
    """
    out = list(qs)
    for obj, plan in zip(objects, plans):
        if not plan.requires_execution or obj.question_id:
            continue
        q = questions_mod.for_object(obj)
        obj.question_id = q.question_id
        out.append(q)
    return out


def sync_questions(ts: TargetSet) -> TargetSet:
    """Re-derive every ReviewQuestion's harness-written half from the target set.

    Called at the END of `build`, and again by `stages/probe` once executions have moved
    targets off PENDING. It is idempotent and it is a FOLD, not an accumulation: every
    field it writes is recomputed from the objects, plans and outcomes as they stand, so
    running it twice on the same target set gives the same answer, and a question can
    never keep a resolution the evidence behind it has since lost.

    Why it exists at all: `harness.questions.derive` runs before discovery, so a question
    is born with no address, no routes and no idea what happened to it. Those three are
    structural facts about the paper and the run — invariant 15's rule, applied to the
    question layer — and none of them may be read from the lens that raised it.

    The three axes stay apart here as everywhere else. `resolution_status` and
    `evidence_state` are `taxonomy`'s derivations off the outcome's disposition and
    provenance, which means the provenance ceiling applies to a question exactly as it
    applies to a target: a synthesized probe that "failed" leaves the question UNRESOLVED
    with evidence INCONCLUSIVE_EXECUTION, and cannot write a conclusion about the paper.
    """
    by_question: dict[str, DiscoveredObject] = {}
    all_targets: dict[str, list[str]] = {}
    for obj in ts.objects:
        if not obj.question_id:
            continue
        # First wins for the BINDING: `priority.order` has already run, so the object a
        # question resolves against is the highest-priority one that came from it.
        #
        # A merged question has one object per contributing finding, at each finding's own
        # address, and those can end differently. The rule here is deliberately the
        # conservative one rather than the flattering one: the question reports the
        # highest-priority target's state, so a question whose central target is blocked
        # stays open even if a supporting one settled. Taking the best available outcome
        # would let a question be answered by whichever of its addresses happened to be
        # reachable, which is the shape of cherry-picking this system exists to catch.
        # Nothing is hidden by it — `evidence_refs` lists every target below.
        by_question.setdefault(obj.question_id, obj)
        # But every target is kept in `evidence_refs`. A merged question has one entry per
        # contributing finding and therefore several objects; binding to one and dropping
        # the rest would make a target disappear from the trace of the question it was
        # built for.
        all_targets.setdefault(obj.question_id, []).append(obj.target_id)
    plans = {p.target_id: p for p in ts.plans}
    outcomes = {o.target_id: o for o in ts.outcomes}

    for q in ts.questions:
        obj = by_question.get(q.question_id)
        if obj is None:
            # A question nobody could build a target for. Still reported — that is the
            # point of a question — but with nothing claimed about it.
            q.possible_resolution_routes = []
            q.route = "NONE"
            q.resolution_status, q.evidence_state = "NOT_INVESTIGATED", "NOT_INVESTIGATED"
            q.evidence_refs = []
            continue

        # The ADDRESS. Minted by `harness.claims` during discovery and carried on the
        # object; never re-parsed here and never taken from the lens's own `evidence_ref`.
        q.claim_ref = obj.ref
        q.possible_resolution_routes = [r for r in obj.routes if r != "NONE"]
        q.route = obj.routes[0] if obj.routes else "NONE"

        out = outcomes.get(obj.target_id)
        if out is None:
            plan = plans.get(obj.target_id)
            q.resolution_status = "NOT_INVESTIGATED"
            q.evidence_state = "NOT_INVESTIGATED"
            q.evidence_refs = list(all_targets.get(q.question_id, []))
            q.resolution = ""
            q.conclusion = ("an experiment is warranted for this question and has not "
                            "concluded in this run."
                            if plan is not None and plan.requires_execution else "")
            continue

        q.evidence_state = out.evidence_state
        q.resolution_status = out.resolution_state
        q.resolved_without_execution = out.disposition == "PAPER_ONLY_RESOLVED"
        q.evidence_refs = [r for r in
                           all_targets.get(q.question_id, []) + [out.execution_ref] if r]
        # `resolution` holds an ANSWER and nothing else. It used to hold `out.reason` for
        # any state but NOT_INVESTIGATED, which meant an UNRESOLVED question carried the
        # reason it was blocked in the field a reader takes for its answer. The blocking
        # reason is still reported — in `conclusion`, which is where a limit belongs.
        q.resolution = out.reason if q.resolution_status.startswith("RESOLVED") else ""
        q.conclusion = _conclusion(out)
    return ts


# What may be said about the PAPER, per evidence state. Every entry that is not one of the
# four in `taxonomy.EVIDENCE_ABOUT_THE_PAPER` says something about an artifact, a host or a
# gate — and says so in those words, because "we could not check it" printed as a
# conclusion about the paper is the failure mode this whole layer exists to prevent.
_CONCLUSION = {
    "REPRODUCTION_SUCCESS": "the paper's stated value was re-derived from the authors' own "
                            "code; this question is closed in the paper's favour.",
    "REPRODUCTION_FAILURE": "the authors' own code, run at a verified commit, did not "
                            "produce the value the paper states.",
    "PAPER_INTERNAL_EVIDENCE": "settled against the paper's own printed content, with "
                               "nothing executed.",
    "ARTIFACT_EVIDENCE": "settled by reading the released code.",
    "ARTIFACT_LIMITATION": "no usable artifact reached this question. Nothing about the "
                           "paper follows from that.",
    "ENVIRONMENT_LIMITATION": "this host could not mount the experiment. A fact about the "
                              "machine, not about the paper.",
    "SPECIFICATION_LIMITATION": "the paper does not specify enough to build the experiment "
                                "that would answer this. Reported unresolved rather than "
                                "answered with an invented one.",
    "EXTRACTION_LIMITATION": "this review could not build a re-derivable address for the "
                             "claim, so there was nothing an execution could be reconciled "
                             "against. A limit of what extraction recovered, not of the "
                             "paper and not of its artifact.",
    "REPORTING_LIMITATION": "the claim is addressed and the paper prints no single "
                            "unambiguous quantity there to compare a reproduction against. "
                            "A limit of how the result was reported.",
    "NO_ROUTE_AVAILABLE": "the claim is addressed and quantified, and no verification route "
                          "this system has would settle the question. A limit of this "
                          "review's method inventory, not of the paper.",
    "COMPARISON_LIMITATION": "a route applies to this question and this review could not "
                             "carry out the comparison at the end of it, so nothing was "
                             "run. A limit of this review's arithmetic, not of the paper "
                             "and not of its artifact.",
    "CITATION_VERIFIED": "the concern's quotation was re-verified against the paper, so it "
                         "cites the paper accurately; whether the concern is correct was "
                         "not investigated and this route could not investigate it.",
    "INCONCLUSIVE_EXECUTION": "something ran and settled nothing admissible; the question "
                              "stands open for a human reviewer.",
    "NOT_INVESTIGATED": "",
}


def _conclusion(out: TargetOutcome) -> str:
    # One disposition-keyed line on top of the evidence-state table. Two dispositions
    # share NOT_INVESTIGATED because neither settles anything, and a reader still needs to
    # know which of them happened: nothing was pursued at all, or the concern's citation
    # was checked and the concern itself was left open.
    if out.disposition == "CITATION_VERIFIED_ONLY":
        return ("the concern's quotation was re-verified against the paper, so it cites "
                "the paper accurately; whether the concern is correct was not "
                "investigated and this route could not investigate it.")
    return _CONCLUSION.get(out.evidence_state, "")


def build(cfg: Config, pid: str, doc: PaperDoc, *,
          investigation_open: bool = True,
          prior_outcomes: dict[str, TargetOutcome] | None = None) -> TargetSet:
    """The whole target set for one paper. Pure with respect to what is on disk.

    `investigation_open` comes from the ASSESS phase and is ONE BIT. When it is False the
    paper's own evidence has already established a material failure, and no execution can
    change what the review concluded — so the planner refuses every executable route with
    `SUPERSEDED_BY_ESTABLISHED_FAILURE`.

    `prior_outcomes` is this paper's OWN previous discover pass, keyed by target_id, and is
    the one piece of history this otherwise-pure rebuild is allowed to consult: it lets
    `_undefer_when_the_central_target_did_not_settle` tell "nobody has tried the central
    target yet" apart from "the central target was tried and settled nothing", which a
    from-scratch rebuild cannot otherwise know. Passing None (the default) reproduces the
    prior, purely-current-pass behaviour exactly.

    Everything else here still runs, deliberately and in full: questions, addresses,
    routes, centrality, the coverage denominators. The referee record has to be complete
    whatever the outcome, and a paper that stops early is not a paper reviewed less
    carefully — it is one where the remaining work could not change the answer.
    """
    reports, _, _ = audit_stage.load_reports(cfg, pid, doc)
    findings = [f for r in reports for f in r.findings]

    # The HARNESS-minted address per finding, computed here because this is where the
    # parsed document is in hand, and handed to `derive` so questions merge on an address
    # the harness can re-derive rather than on the `p<N>` page reference a lens was asked
    # to write. See `questions.derive`'s `minted` parameter: two findings about different
    # sentences on one page were becoming one question, and the merge took the maximum
    # materiality of its sources, so a page-number coincidence promoted a concern.
    minted = {}
    for f in findings:
        if not f.finding_id:
            continue
        ref = claims.address(doc, f.evidence_ref or "", f.evidence_quote or "")
        if ref is not None and ref.resolved and ref.ref:
            minted[f.finding_id] = ref.ref
    qs = questions_mod.derive(findings, minted=minted)

    # Computed BEFORE discovery now, not after — Step 6's fallback needs
    # `readiness.established` at ROUTE-DERIVATION time so an object whose repository
    # exists can ALSO carry INDEPENDENT_RECONSTRUCTION as a route, for the planner to fall
    # back to once `stages/probe.py` finds AUTHOR_CODE_EXECUTION's identity did not bind.
    # Both facts are pure over the doc alone, so moving them earlier changes no answer for
    # either of them — only what discovery gets to see before it runs.
    repo_available = bool((doc.repo_url or "").strip())
    readiness = reimplement.assess(doc)
    objects, coverage = discovery.discover(
        doc, findings, qs, repo_available=repo_available,
        specification_complete=readiness.established)

    ordered = priority.order(objects, artifact_available=repo_available)

    proposed = [planner.plan(obj, artifact_available=repo_available,
                             specification_complete=readiness.established,
                             investigation_open=investigation_open)
                for obj in ordered]
    proposed = _demote_when_a_central_target_is_being_pursued(ordered, proposed)
    proposed = _undefer_when_the_central_target_did_not_settle(ordered, proposed, prior_outcomes)
    proposed = _one_per_experiment(ordered, proposed)
    # AFTER the two set-level demotions, so a target that will not run does not acquire a
    # question purely to be reported as unanswered.
    qs = _question_for_every_executable_target(ordered, proposed, qs)

    plans: list[PlanDecision] = []
    outcomes: list[TargetOutcome] = []
    for obj, plan in zip(ordered, proposed):
        plans.append(plan)
        if plan.requires_execution:
            obj.status = "PENDING"
            continue
        if plan.action == "PAPER_ONLY_RESOLUTION":
            outcome = _paper_only_outcome(obj, plan)
        else:
            outcome = TargetOutcome(
                target_id=obj.target_id,
                disposition=_DISPOSITION_FOR_ACTION.get(plan.action, "NOT_ATTEMPTED"),
                action=plan.action, route=plan.route, reason=plan.reason)
        obj.status = outcome.disposition
        outcomes.append(outcome)

    # Both are resolved without execution — one agrees with the paper, one contradicts
    # it, and "resolved" here means "settled", not "settled in the paper's favour".
    resolved_ids = {o.target_id for o in outcomes
                    if o.disposition in ("PAPER_ONLY_RESOLVED", "PAPER_ARITHMETIC_CONTRADICTION")}
    ts_partial = TargetSet(paper_id=pid, objects=ordered, questions=qs, plans=plans,
                           outcomes=outcomes)
    sync_questions(ts_partial)

    coverage["investigation_open"] = investigation_open
    coverage["targets_superseded_by_established_failure"] = sum(
        1 for o in outcomes if o.disposition == "SUPERSEDED_BY_ESTABLISHED_FAILURE")
    coverage["specification_complete"] = readiness.established
    coverage["specification_missing"] = readiness.missing
    coverage["repository_advertised"] = repo_available
    coverage["targets_requiring_execution"] = sum(1 for p in plans if p.requires_execution)
    # The invariant, counted rather than asserted, so a rerun can show it held. Every
    # target this run would spend something on names the question it is spending it on.
    by_id = {o.target_id: o for o in ordered}
    coverage["executable_targets_with_a_question"] = sum(
        1 for p in plans if p.requires_execution
        and getattr(by_id.get(p.target_id), "question_id", ""))
    # What KIND of question the executable work is about, which is the routing decision
    # this phase now makes. A funnel that says only "43 warranting an experiment" cannot
    # show that the set changed from "43 printed quantities" to something a referee asks.
    kinds: dict[str, int] = {}
    for p in plans:
        if not p.requires_execution:
            continue
        k = getattr(by_id.get(p.target_id), "question_kind", "") or "UNCLASSIFIED"
        kinds[k] = kinds.get(k, 0) + 1
    coverage["executable_question_kinds"] = kinds
    coverage["targets_resolved_without_execution"] = len(resolved_ids)
    # Counted and reported SEPARATELY, never added to the line above. "The concern quotes
    # the paper accurately" and "the question is settled" were one number until this
    # corpus showed the first was 20 and the second was 0.
    coverage["targets_citation_verified_only"] = sum(
        1 for o in outcomes if o.disposition == "CITATION_VERIFIED_ONLY")
    coverage["targets_blocked_before_execution"] = sum(
        1 for o in outcomes if o.disposition in
        ("SPECIFICATION_BLOCKED", "ARTIFACT_BLOCKED", "ENVIRONMENT_BLOCKED"))
    coverage["targets_outranked_by_a_central_one"] = sum(
        1 for p in plans if p.blocking_gate == "outranked_by_a_central_target")
    coverage["targets_answered_by_another_run"] = sum(
        1 for p in plans if p.blocking_gate == "answered_by_another_target")

    result = TargetSet(paper_id=pid, objects=ordered, questions=qs, plans=plans,
                       outcomes=outcomes, extraction_coverage=coverage)
    return exhaustion.refresh(result, cfg)


def run(cfg: Config, pid: str, *, investigation_open: bool = True) -> dict:
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))
    state.set_phase(cfg, pid, "discover")

    prior = load(cfg, pid)
    prior_outcomes = {o.target_id: o for o in prior.outcomes} if prior else None
    ts = exhaustion.refresh(
        build(cfg, pid, doc, investigation_open=investigation_open,
              prior_outcomes=prior_outcomes), cfg)
    path = targets_path(cfg, pid)
    state.write_json(path, ts.model_dump())

    executable = [p for p in ts.plans if p.requires_execution]
    state.append_log(
        cfg, pid, artifact_type="target_set", phase="discover",
        headers={"objects": len(ts.objects), "questions": len(ts.questions),
                 "addressable": sum(1 for o in ts.objects if o.harness_addressable),
                 "requires_execution": len(executable),
                 "resolved_without_execution":
                     ts.extraction_coverage.get("targets_resolved_without_execution", 0)},
        path=str(path))

    return {"paper_id": pid, "objects": len(ts.objects), "questions": len(ts.questions),
            "addressable": sum(1 for o in ts.objects if o.harness_addressable),
            "requires_execution": len(executable),
            "resolved_without_execution":
                ts.extraction_coverage.get("targets_resolved_without_execution", 0),
            "blocked_before_execution":
                ts.extraction_coverage.get("targets_blocked_before_execution", 0),
            "top_target": ts.objects[0].target_id if ts.objects else None,
            "targets": f"discovery/targets.json"}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # self-check: python -m harness.stages.discover
    from ..artifacts import Finding, QuantFinding, Section, Table

    doc = PaperDoc(
        paper_id="p", repo_url="https://example.invalid/r",
        sections=[Section(section_idx=0, title="Abstract",
                          text="We build 58 topics x 5 templates x 10 instances = 2,900 test "
                               "cases. We train with Adam for 30 epochs at a learning rate of "
                               "1e-3 on the CIFAR-100 dataset and report accuracy on the test "
                               "set. The objective is defined as a weighted sum."),
                  Section(section_idx=1, title="Results",
                          text="We report 12 x 3 = 40 configurations in the appendix.")],
        tables=[Table(table_idx=1, rows=[["ours", "61.4"]])],
        reported_numbers=[QuantFinding(value="61.4", metric="accuracy",
                                       source_quote="61.4", table_ref="T1:r0:c0")])
    fs = [Finding(finding_id="c-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                  evidence_ref="T1:r0:c0", evidence_quote="ours")]

    qs = questions_mod.derive(fs)
    objs, cov = discovery.discover(doc, fs, qs)
    ordered = priority.order(objs, artifact_available=True)
    assert ordered[0].centrality == "CENTRAL"
    plans = [planner.plan(o, artifact_available=True, specification_complete=True)
             for o in ordered]
    assert any(p.requires_execution for p in plans), "a central printed quantity must be pursued"
    assert any(p.action == "PAPER_ONLY_RESOLUTION" for p in plans), (
        "the broken composition must be settled without running anything")
    broken = next(o for o in ordered if o.expected_value == 40.0)
    p = next(p for p in plans if p.target_id == broken.target_id)
    out = _paper_only_outcome(broken, p)
    assert "NOT consistent" in out.reason
    assert out.disposition == "PAPER_ARITHMETIC_CONTRADICTION", out.disposition
    assert out.establishes_failure, (
        "a contradicted printed composition ESTABLISHES a defect (Tier 1). Whether it is "
        "material to a central claim is `harness.materiality`'s separate question.")
    print("harness.stages.discover self-check ok")
