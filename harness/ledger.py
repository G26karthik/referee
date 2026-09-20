"""The traceable chain behind every conclusion, and the cost of getting there.

`python -m harness.ledger` runs the self-check.

**Why this is separate from the report.** A reviewer-facing first-pass report is one to
two pages and exists to reduce someone's workload; an audit trace is however long it needs
to be and exists so that any line of that report can be traced to an artifact. Making one
document do both jobs produces a document that does neither, which is what the previous
`reports/<pid>.md` had become — the complete machine trace, handed to a human as if it
were a review.

For each material conclusion the ledger must be able to answer, without re-deriving
anything: what claim, where the paper stated it, what question was asked, which target was
selected, why that one, which route, what code at what commit, what happened, what was
observed, which published quantity it was compared with, why that comparison is
admissible, and what follows. Every field below is copied from an artifact that already
exists — nothing here is re-reasoned, because a trace that re-reasons is a second opinion
wearing a trace's clothes.

**Efficiency is measured, not asserted.** `CaseLedger.efficiency` counts what the pipeline
actually did: how many questions were generated, how many were closed without running
anything, how many escalated, how many were stopped by a gate and by which one, and how
many seconds were spent. A system claiming to escalate only when justified, with no count
of how often it escalated, is claiming nothing.
"""
from __future__ import annotations

from .artifacts import (BLOCKED_DISPOSITIONS, CaseLedger, EvalReport, LedgerEntry,
                        ProbeResult, TargetSet)
from . import exhaustion
from . import planner as planner_mod
from . import provenance as provenance_mod

# The comparison is admissible only from these, in either direction — the provenance
# ceiling, stated in the trace so a reader does not have to know it to check the entry.
_ADMISSIBLE = provenance_mod.ADMISSIBLE_REPRODUCTION_PROVENANCE

_ADMISSIBILITY = {
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

# What each disposition means for the paper, stated once. Only two of these are statements
# about the paper at all, which is the distinction invariants 4-7 exist to keep.
_IMPLICATION = {
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


def _entry(idx: int, obj, plan, outcome, probe: ProbeResult | None) -> LedgerEntry:
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
        admissibility=_ADMISSIBILITY.get(
            provenance, f"provenance '{provenance or 'none'}' is not one the ceiling admits"),
        disposition=disposition,
        implication=_IMPLICATION.get(disposition, ""),
        # WHY a failure here would (or would not) be material to a central claim, copied
        # off the object so the paper-level decision is re-derivable from the trace alone.
        # An established defect with NONE here is real, reported, and not paper-stopping.
        materiality_basis=getattr(obj, "materiality_basis", "NONE") or "NONE",
        # The three axes, carried onto the trace so a reader can sort it by any of them
        # without re-deriving. All three are properties on `TargetOutcome` — derived from
        # the disposition and the provenance, never stored beside them — so an entry
        # cannot claim an evidence state its own disposition does not support.
        evidence_state=getattr(outcome, "evidence_state", "NOT_INVESTIGATED"),
        resolution_state=getattr(outcome, "resolution_state", "NOT_INVESTIGATED"),
        necessity=(outcome.necessity(bool(getattr(plan, "requires_execution", False)))
                   if outcome is not None else
                   getattr(plan, "necessity", "NO_EXPERIMENT_NEEDED")),
        concerns_the_paper=bool(getattr(outcome, "concerns_the_paper", False)),
        launched=int(getattr(outcome, "launched", 0) or 0),
        why_material=getattr(plan, "why_material", ""),
    )


def _question_entry(idx: int, q) -> LedgerEntry:
    """One trace line per REVIEW QUESTION, whether or not a target was ever built for it.

    Targets are the things the pipeline can act on; questions are the things a reviewer
    reads. A ledger with an entry per target silently loses every question that had no
    addressable target — which is most of them on most papers, and precisely the set a
    first-pass reviewer most needs to hand over. So both are traced, and a question entry
    says plainly that nothing was pursued rather than not appearing.
    """
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


def build(report: EvalReport, target_set: TargetSet | None,
          probe: ProbeResult | None = None, *, seconds: float = 0.0) -> CaseLedger:
    """The whole trace for one paper. Copies; derives nothing."""
    ts = target_set or TargetSet(paper_id=report.paper_id)
    # THE CURRENT plan for each target, not the raw log. A re-plan after Step 6's
    # AUTHOR_CODE_EXECUTION fallback appends a SECOND `PlanDecision` for the same
    # target_id rather than replacing the first — see `planner.current_plans` — so
    # summing or counting over `ts.plans` directly double-counts any target that was
    # re-planned. Every count below reads the de-duplicated list.
    live_plans = planner_mod.current_plans(ts.plans)
    plans = {p.target_id: p for p in live_plans}
    outcomes = {o.target_id: o for o in ts.outcomes}

    entries: list[LedgerEntry] = []
    for obj in ts.objects:
        outcome = outcomes.get(obj.target_id)
        plan = plans.get(obj.target_id)
        if outcome is None and plan is None:
            continue
        entries.append(_entry(len(entries) + 1, obj, plan, outcome, probe))
    for i, q in enumerate(ts.questions, start=1):
        entries.append(_question_entry(i, q))

    blocked_by_gate: dict[str, int] = {}
    for p in live_plans:
        if p.blocking_gate:
            blocked_by_gate[p.blocking_gate] = blocked_by_gate.get(p.blocking_gate, 0) + 1

    efficiency = {
        # --- the six-term funnel, and every term is counted from a different artifact ---
        # These six were one number in three different places before, and the collapse
        # was a real overclaim: "escalates to execution for 61 targets" was the count of
        # targets a PLAN warranted, printed as though 61 processes had started. They are
        # kept apart here, in order, and each says which artifact it is read off.
        #
        #   discovered   every object discovery built                       (objects)
        #   checkable    those with an address and a route                  (objects)
        #   warranted    those a plan judged worth an experiment            (plans)
        #   launched     those that actually STARTED a process              (execution record)
        #   completed    those that ran to a reconciliation attempt         (outcomes)
        #   resolved     those that settled the question, admissibly        (outcomes)
        #
        # `launched` reads `TargetOutcome.launched`, which `stages/probe.outcome_for`
        # copies off `ProbeResult.executions` — the runner's own count of processes it
        # started. Nothing infers it from a disposition.
        "targets_discovered": len(ts.objects),
        "targets_addressable": sum(1 for o in ts.objects if o.harness_addressable),
        "targets_warranting_experiment": sum(1 for p in live_plans if p.requires_execution),
        "targets_launched": sum(1 for o in ts.outcomes if o.launched > 0),
        "processes_launched": sum(o.launched for o in ts.outcomes),
        "executions_completed": sum(1 for o in ts.outcomes
                                    if o.launched > 0 and o.reconciliation is not None),
        "targets_resolved": sum(1 for o in ts.outcomes if o.concerns_the_paper),

        # --- what the funnel does NOT say, said separately -----------------------------
        "questions_generated": len(ts.questions),
        "questions_resolved_without_execution":
            sum(1 for q in ts.questions if q.resolved_without_execution),
        "questions_open": sum(1 for q in ts.questions if q.is_open),
        "targets_requiring_execution": sum(1 for p in live_plans if p.requires_execution),
        "targets_resolved_without_execution":
            sum(1 for o in ts.outcomes if o.disposition in
               ("PAPER_ONLY_RESOLVED", "PAPER_ARITHMETIC_CONTRADICTION")),
        # Its own term, never summed into the one above. Verifying that a concern quotes
        # the paper accurately is a precondition invariant 1 already guarantees for every
        # kept finding, not a second resolution — and reporting it as one made all seven
        # corpus papers claim settled questions they had not settled.
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
        # WALL TIME OF THE PROBE STAGE, which includes acquisition, static audit, planning
        # and every gate — not the cost of running experiments. Named for what it is: when
        # `processes_launched` is zero this number is entirely setup, and calling it
        # "execution cost" would be a fabricated measurement.
        "probe_stage_seconds": round(float(seconds or (probe.seconds if probe else 0.0)), 3),
        "findings_after_verification": len(report.findings),
        "findings_dropped_unsubstantiated": report.dropped_findings,
    }
    # Every axis, counted. A distribution the reviewer report can render without deriving
    # anything itself, and the evaluation layer can aggregate across papers.
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
    efficiency["route_exhaustion"] = exhaustion.coverage(
        ts.route_attempts, tuple(ts.route_exhaustion_question_ids))
    return CaseLedger(paper_id=report.paper_id, entries=entries,
                      route_attempts=list(ts.route_attempts), efficiency=efficiency)


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    from .artifacts import (ClaimRef, DiscoveredObject, PlanDecision, Reconciliation,
                            ReviewQuestion, TargetOutcome)

    ts = TargetSet(
        paper_id="p",
        questions=[ReviewQuestion(question_id="Q1", resolved_without_execution=True),
                   ReviewQuestion(question_id="Q2")],
        objects=[
            DiscoveredObject(target_id="A", claim_text="a printed accuracy",
                             harness_addressable=True, priority_reason="centrality=CENTRAL",
                             ref=ClaimRef(ref="T1:r0:c1", kind="table_cell", quote="61.4",
                                          resolution="resolved")),
            DiscoveredObject(target_id="B", claim_text="a composition",
                             harness_addressable=True, question_id="Q1",
                             ref=ClaimRef(ref="P0:1-40", kind="prose_claim",
                                          quote="12 x 3 = 40", resolution="resolved")),
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
    assert ts.outcomes[1].establishes_failure, (
        "a contradicted printed composition is a material failure at the type level")
    led = build(EvalReport(paper_id="p", title="t", verdict="RED"), ts)
    targets = [e for e in led.entries if e.entry_id.startswith("L")]
    assert [e.target_id for e in targets] == ["A", "B", "C"]
    a = targets[0]
    assert a.source_ref == "T1:r0:c1" and a.compared_with == "61.4"
    assert "52.0" in a.observed and "authors' own checkout" in a.admissibility
    assert "did not produce the printed quantity" in a.implication
    assert a.evidence_state == "REPRODUCTION_FAILURE" and a.concerns_the_paper
    assert a.resolution_state == "RESOLVED_BY_EXECUTION" and a.launched == 3
    b = targets[1]
    assert b.compared_with == "12 x 3 = 40" and "12*3 is not 40" in b.observed
    assert "material failure" in b.implication and "no model judgement" in b.implication
    assert b.evidence_state == "PAPER_INTERNAL_EVIDENCE" and b.concerns_the_paper
    assert b.resolution_state == "RESOLVED_FROM_PAPER" and b.launched == 0
    c = targets[2]
    assert "not a defect in the result" in c.implication, (
        "a blocked target must never read as a statement about the paper")
    assert not c.concerns_the_paper and c.resolution_state == "UNRESOLVED"

    # --- every question is traced, including the ones no target was built for ----------
    qs = [e for e in led.entries if e.entry_id.startswith("Q")]
    assert [e.question for e in qs] == ["Q1", "Q2"], (
        "a question with no addressable target must still appear in the trace")

    e = led.efficiency
    assert e["questions_generated"] == 2 and e["questions_resolved_without_execution"] == 1
    assert e["targets_discovered"] == 3 and e["targets_addressable"] == 2
    assert e["targets_requiring_execution"] == 1 and e["executions_completed"] == 1
    assert e["blocked_by_gate"] == {"specification_complete": 1}
    assert e["targets_settled"] == 2
    # "resolved without execution" counts a paper-arithmetic CONTRADICTION exactly like an
    # agreeing one: both are settled without running anything, whichever direction the
    # arithmetic came out.
    assert e["targets_resolved_without_execution"] == 1

    # --- the six-term funnel is six DIFFERENT numbers, from different artifacts --------
    assert e["targets_discovered"] == 3
    assert e["targets_addressable"] == 2
    assert e["targets_warranting_experiment"] == 1
    assert e["targets_launched"] == 1 and e["processes_launched"] == 3, (
        "launched is read off the execution record, not inferred from a disposition")
    assert e["executions_completed"] == 1
    assert e["targets_resolved"] == 2, "the failure and the paper-only resolution"
    assert "probe_stage_seconds" in e and "execution_seconds" not in e, (
        "the probe stage's wall time is not the cost of running experiments")

    # --- and the axis distributions -----------------------------------------------------
    assert e["targets_by_evidence_state"]["REPRODUCTION_FAILURE"] == 1
    assert e["targets_by_evidence_state"]["SPECIFICATION_LIMITATION"] == 1
    assert e["targets_by_resolution_state"]["RESOLVED_FROM_PAPER"] == 1
    assert e["targets_by_experiment_necessity"]["EXPERIMENT_RESOLVED"] == 1

    # a synthesized run is traced, and its trace says it settles nothing
    ts.outcomes[0].provenance = "synthesized"
    led2 = build(EvalReport(paper_id="p", title="t", verdict="GREEN"), ts)
    syn = [x for x in led2.entries if x.target_id == "A"][0]
    assert "NOT admissible" in syn.admissibility
    assert led2.efficiency["executions_completed"] == 1, (
        "it still RAN; what the ceiling changes is what may be concluded from it")
    assert syn.evidence_state == "INCONCLUSIVE_EXECUTION" and not syn.concerns_the_paper, (
        "the provenance ceiling reaches the trace, not only the reconciler")
    assert led2.efficiency["targets_resolved"] == 1, "only the paper-only resolution survives"
    print("harness.ledger self-check ok")


if __name__ == "__main__":
    _self_check()
