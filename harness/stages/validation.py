"""The FOCUSED_VALIDATION_EXPERIMENT route: design one contrast, run it, compare the arms.

`python -m harness.stages.validation` runs the self-check (offline, nothing executes).

**What this stage is, stated so it cannot be read as more.** For a question a printed
number cannot settle — two variables moved at once, the control the claim needs was never
run, the protocol that produced the number is not the protocol the claim is about — it
derives the smallest one-variable contrast the paper and its pinned checkout support, runs
both arms of it in the AUTHORS' OWN code, and holds one arm against the other against a
rule declared before either of them ran.

**It is not a general experiment generator, and `SPECIFICATION_BLOCKED` is the expected
answer.** Every ingredient in `artifacts.VALIDATION_INGREDIENTS` must bind from the
document or the checkout; a design that would need this harness to choose an optimizer, a
split, a schedule or a threshold is refused and names the key. That refusal is
`planner.INFEASIBLE_SPECIFICATION`'s argument arriving one layer lower, with the missing
ingredient printed instead of implied.

**Two hooks, not a parallel pipeline.** `prepare` runs between `plan_execution` and
`establish_comparison` and decides whether a process may be started at all; `adjudicate`
runs after `local_exec.run_probe` and turns the per-arm statistics that function already
produces into a between-arms comparison. Everything between the two — identity, capability,
resources, commit verification, `backends.authorize`, the isolation boundary — is the
SAME code an author-code reproduction goes through, unchanged. A focused validation that
skipped one of those gates would be a second execution path with weaker rules.

**What it may conclude is bounded twice over.** `between_arms.compare` computes the
contrast; `between_arms.authority_for` decides what the contrast is entitled to say, and
the top rung needs BOTH a conformant design and a provenance the reproduction ceiling
already admits. A controlled observation is the honest and expected outcome of a run whose
experiment is partly this review's, and it resolves nothing.
"""
from __future__ import annotations

from .. import between_arms, claimgraph, validation, validation_driver
from ..artifacts import (ArmMeasurement, DiscoveredObject, FocusedValidation, PaperDoc,
                         PlanDecision, ProbeResult, ProbeSpec, SettlementCondition,
                         TargetOutcome, TargetSet, ValidationDesign)
from ..config import Config
from ..prompts import validation as VP

ACTION = "FOCUSED_VALIDATION_EXPERIMENT"

# How much of the paper's own method text one design may be shown. A bound, published
# here rather than buried in a call: a designer handed the whole paper is being asked to
# find the method section, which is not the task.
_METHOD_CHARS = 6000


def planned(target_set: TargetSet | None) -> list[tuple[DiscoveredObject, PlanDecision]]:
    """The (object, plan) pairs this route owns."""
    if target_set is None:
        return []
    by_id = {o.target_id: o for o in target_set.objects}
    out = []
    for plan in target_set.plans:
        if plan.action != ACTION:
            continue
        obj = by_id.get(plan.target_id)
        if obj is not None:
            out.append((obj, plan))
    return out


def method_text(doc: PaperDoc, labels: list[str]) -> str:
    """The paper's own sentences about how these arms were run.

    Sections that NAME an arm, in document order, truncated at a published bound. A
    designer shown the whole paper is being asked to find the method section, and one
    shown nothing is being asked to invent it.
    """
    wanted = [l.strip().lower() for l in labels if l and l.strip()]
    if not wanted:
        return ""
    out: list[str] = []
    for section in doc.sections:
        text = section.text or ""
        low = text.lower()
        if any(w in low for w in wanted):
            out.append(f"[{section.title or f'section {section.section_idx}'}] {text}")
    return "\n\n".join(out)[:_METHOD_CHARS]


def comparison_for(graph, metric: str = "", benchmark: str = "") -> tuple[str, list]:
    """(comparison_id, members) — the claim graph's own COMPARISON node for this pair.

    REUSED, never rebuilt. `claimgraph.build` already derives one node per (metric,
    benchmark) the paper reports for two or more methods, carrying each arm's address,
    label and printed value. A second inventory here would be free to disagree with the
    first, and then a reader could not tell which of them the review acted on.
    """
    if graph is None or not (metric or "").strip() or not (benchmark or "").strip():
        return "", []
    want = f"comparison:{metric.strip().lower()}|{benchmark.strip().lower()}"
    for cid, member_ids, _why in claimgraph.comparisons(graph):
        if cid == want:
            return cid, [graph.results[m] for m in member_ids if m in graph.results]
    return "", []


def _proposal(cfg: Config, doc: PaperDoc, obj: DiscoveredObject, plan: PlanDecision,
              members: list, metric: str, benchmark: str, treatment_method: str,
              *, artifact_text: str = "") -> tuple[dict, dict]:
    """(proposal, record). ({}, record) whenever the gate is shut or the call fails.

    An empty proposal is not an error: the deterministic half still runs and the design
    reports which ingredients the paper does not bind. What must never happen is a silent
    empty — every early return records which failure produced it.
    """
    arms = validation.arms_from_comparison(doc, members,
                                           treatment_method=treatment_method)
    if len(arms) < 2:
        return {}, {"tag": "validation", "failure": "no_arms",
                    "detail": "the paper's own comparison did not yield two named arms"}
    control, treatment = arms[0], arms[1]
    prompt = VP.design(
        doc.title or doc.paper_id,
        plan.why_material or obj.claim_text or "",
        (obj.ref.quote if obj.ref else "") or obj.claim_text or "",
        "\n".join(f"{i}. {e}" for i, e in enumerate(plan.competing_explanations, 1)),
        f"{control.label} ({control.address or 'no recovered address'})",
        f"{treatment.label} ({treatment.address or 'no recovered address'})",
        metric, benchmark, method_text(doc, [control.label, treatment.label]),
        artifact_text=artifact_text)
    raw, record = validation_driver.call(cfg, prompt)
    if not raw:
        return {}, record
    try:
        proposal, meta = validation_driver.parse_design(raw)
    except validation_driver.ValidationDriverError as exc:
        record["failure"] = f"unusable_output: {exc}"
        return {}, record
    record.update(meta)
    record["buildable"] = bool(proposal.get("buildable"))
    return proposal, record


def build_design(cfg: Config, doc: PaperDoc, obj: DiscoveredObject, plan: PlanDecision,
                 graph=None, *, artifact_text: str = "") -> tuple[ValidationDesign, dict]:
    """The design for one target, and the record of how it was reached.

    The deterministic half runs whatever the gate says. What the gate buys is the half a
    document does not print: which variable the arms differ in, which are held fixed, and
    the settlement condition — each of which is then relocated against the parsed paper
    before it may enter the design.
    """
    metric = (obj.metric or "").strip()
    benchmark = (obj.experiment or "").strip()
    cid, members = comparison_for(graph, metric, benchmark)
    treatment_method = ""
    if members:
        # THE TREATMENT ARM IS THE METHOD THE CLAIM CREDITS, and the claim's own sentence
        # is where that is named. A treatment chosen by position would be a scientific
        # choice made by a list order.
        text = (obj.claim_text or "") + " " + ((obj.ref.quote if obj.ref else "") or "")
        low = text.lower()
        named = [m.method for m in members
                 if (m.method or "").strip() and m.method.strip().lower() in low]
        if len(named) == 1:
            treatment_method = named[0]

    proposal, record = _proposal(cfg, doc, obj, plan, members, metric, benchmark,
                                 treatment_method, artifact_text=artifact_text)

    configuration: dict[str, dict[str, str]] = {}
    basis: list[str] = []
    dropped: list[str] = []
    for role in ("control", "treatment"):
        got, where, gone = validation_driver.relocate_configuration(
            doc, (proposal.get("arms") or {}).get(role) or {},
            artifact_text=artifact_text)
        configuration[role] = got
        basis += [f"{role}.{k} from {v}" for k, v in sorted(where.items())]
        dropped += [f"{role}.{d}" for d in gone]

    settlement: SettlementCondition | None = validation_driver.settlement_from(
        proposal.get("settlement") or {})
    changed = [proposal["changed_variable"]] if proposal.get("changed_variable") else []

    got = validation.design(
        doc, target_id=obj.target_id, question_id=obj.question_id,
        question_kind=obj.question_kind,
        question_ref=(obj.ref.ref if obj.ref else ""),
        question_text=plan.why_material or obj.claim_text or "",
        competing_explanations=list(plan.competing_explanations),
        comparison_id=cid, metric=metric, benchmark=benchmark, members=members,
        treatment_method=treatment_method, changed_variables=changed,
        controlled_variables=list(proposal.get("controlled_variables") or []),
        arm_configuration=configuration,
        configuration_source="paper" if configuration else "",
        settlement=settlement, statistical_unit="")

    record.update({"target_id": obj.target_id, "design_state": got.state,
                   "missing": got.missing, "assumed": got.assumed,
                   "relocated": basis, "dropped": dropped,
                   "unstated_by_designer": list(proposal.get("unstated") or [])})
    return got, record


def prepare(cfg: Config, doc: PaperDoc, obj: DiscoveredObject, plan: PlanDecision,
            spec: ProbeSpec, graph=None, *, artifact_text: str = ""
            ) -> tuple[ProbeSpec, FocusedValidation, dict]:
    """Decide whether a process may be started, and with which arms.

    Called between `plan_execution` and `establish_comparison`. A blocked design leaves
    `spec.arms` untouched and `fv.design.established` False; the caller then writes a
    SPECIFICATION_BLOCKED outcome and starts nothing. A design that binds names the arms
    on the spec, so `establish_comparison` derives an ESTABLISHED between-arms comparison
    and `may_be_compared` lets the run proceed to the gates every execution goes through.
    """
    design, record = build_design(cfg, doc, obj, plan, graph, artifact_text=artifact_text)
    fv = FocusedValidation(paper_id=doc.paper_id, target_id=obj.target_id, design=design)
    if design.established:
        control, treatment = design.control, design.treatment
        spec.arms = [control.label, treatment.label]
        # The arm's own command wins where the checkout advertises one per arm; otherwise
        # `local_exec.resolve_command` substitutes `{arm}` into the single command
        # identity already bound, which is how one entrypoint runs both arms.
        if treatment.command:
            spec.command = list(treatment.command)
    fv.disposition, fv.reason = validation.outcome_disposition(fv)
    return spec, fv, record


def measurements_from(design: ValidationDesign | None, result: ProbeResult | None,
                      spec: ProbeSpec | None = None) -> list[ArmMeasurement]:
    """Per-arm statistics `local_exec.run_probe` already produced, as typed measurements.

    PERSISTED PER ARM rather than folded straight into a delta. `ProbeResult.measured_delta`
    kept only the difference, and a referee tracing a between-arms verdict needs each
    side's identity, unit, split and repetition count to check that the two were comparable
    at all — which is exactly what `between_arms.compare` refuses without.

    `uncertainty` is None rather than 0.0 for an arm that ran once. A spread computed from
    a single value is zero, and zero reads as a separation nobody measured.
    """
    if design is None or result is None:
        return []
    out: list[ArmMeasurement] = []
    for arm in design.arms:
        stats = (result.arms or {}).get(arm.label)
        value = stats.mean if (stats is not None and stats.n) else None
        out.append(ArmMeasurement(
            design_id=design.design_id, role=arm.role, label=arm.label,
            experiment_id=((spec.experiment.command.label
                            if spec is not None and spec.experiment is not None
                            and spec.experiment.command is not None else '') or ''),
            configuration=dict(arm.configuration),
            metric=design.metric, metric_basis=design.metric_basis,
            unit=design.metric_unit, benchmark=design.benchmark, split=design.split,
            statistical_unit=design.statistical_unit, value=value,
            uncertainty=(stats.std if (stats is not None and stats.n > 1) else None),
            n=(stats.n if stats is not None else 0), seeds=list(result.seeds_run),
            provenance=result.provenance, execution_ref=result.execution_log,
            reason=("" if value is not None else
                    f"the {arm.role} arm produced no value for "
                    f"{design.metric or 'the metric'}: {result.reason or 'no metric was parsed'}")))
    return out


def adjudicate(fv: FocusedValidation, spec: ProbeSpec, result: ProbeResult
               ) -> FocusedValidation:
    """Turn the run into a between-arms comparison and a terminal disposition.

    `answers_question` is computed HERE, by the harness, from the design's question kind
    and the rule that fired — never by the layer that computed the arithmetic and never by
    anything a model wrote. A perfectly computed equivalence result answers an equivalence
    question, and carrying it into an attribution question would settle a question nobody
    asked with a number nobody disputes.
    """
    fv.measurements = measurements_from(fv.design, result, spec)
    fv.launched = result.executions
    rule = (fv.design.settlement.rule
            if fv.design is not None and fv.design.settlement is not None else "")
    fv.comparison = between_arms.compare(
        fv.design, fv.measurements, provenance=result.provenance,
        answers_question=validation.answers_question(fv.design, rule))
    fv.disposition, fv.reason = validation.outcome_disposition(fv)
    fv.discharged = validation.discharge(fv)
    return fv


def outcome_for(fv: FocusedValidation, plan: PlanDecision, *, provenance: str = "",
                execution_ref: str = "") -> TargetOutcome:
    """One target's terminal state, from what the route made of it.

    `launched` is copied from the run's own process count, never inferred from the
    disposition — the same rule `stages.probe.outcome_for` follows, and for the same
    reason: judged-worth-running and actually-ran are two numbers.
    """
    return TargetOutcome(
        target_id=fv.target_id, disposition=fv.disposition, action=plan.action,
        route=plan.route, provenance=provenance, launched=fv.launched,
        execution_ref=execution_ref, attempts=1, reason=fv.reason)


def summarise(runs: list[FocusedValidation]) -> dict:
    """Counts a reader can check. NONE of them is a sum of two different things."""
    totals = {"designs_attempted": len(runs), "designed": 0, "specification_blocked": 0,
              "arms_measured": 0, "compared": 0, "settled": 0, "launched": 0}
    for fv in runs:
        got = validation.summarise(fv)
        for key in ("designed", "specification_blocked", "arms_measured", "compared",
                    "settled", "launched"):
            totals[key] += got[key]
    return totals


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.stages.validation
    from types import SimpleNamespace

    from ..artifacts import (ArmStats, ClaimRef, Section, TARGET_DISPOSITIONS,
                             ValidationArm)

    doc = PaperDoc(paper_id="p", title="LDReg", sections=[Section(
        section_idx=0, title="Setup",
        text=("We evaluate on the CIFAR-100 test set. AugOnly is our baseline and LDReg "
              "is the proposed method. All arms train for 200 epochs."))])

    _node = lambda m: SimpleNamespace(method=m, address="", label=m, value=None)  # noqa: E731

    obj = DiscoveredObject(target_id="t1", question_id="q1", question_kind="ATTRIBUTION",
                           metric="accuracy", experiment="CIFAR-100",
                           claim_text="LDReg improves over AugOnly.",
                           ref=ClaimRef(ref="P0:0-40", kind="prose", resolution="resolved"))
    plan = PlanDecision(target_id="t1", action=ACTION, route=ACTION,
                        requires_execution=True,
                        competing_explanations=["the regulariser", "the schedule"],
                        why_material="is the gain the regulariser or the schedule?")

    # THE ROUTE OWNS ITS OWN PLANS AND NOBODY ELSE'S.
    ts = TargetSet(paper_id="p", objects=[obj], plans=[plan])
    assert [o.target_id for o, _ in planned(ts)] == ["t1"]
    assert planned(None) == []
    assert planned(TargetSet(paper_id="p", objects=[obj], plans=[
        PlanDecision(target_id="t1", action="AUTHOR_CODE_REPRODUCTION")])) == []

    # THE METHOD TEXT IS THE SECTIONS THAT NAME THE ARMS, and nothing when they name none.
    assert "200 epochs" in method_text(doc, ["AugOnly", "LDReg"])
    assert method_text(doc, []) == "" and method_text(doc, ["NotHere"]) == ""

    # WITH THE GATE SHUT THE DESIGN IS BLOCKED AND NAMES WHAT IS MISSING.
    cfg = Config(allow_validation_design=False)
    design, record = build_design(cfg, doc, obj, plan, None)
    assert design.state == "SPECIFICATION_BLOCKED" and design.missing
    assert record["failure"] == "no_arms", record
    # ... and with a graph but the gate shut, the refusal is the GATE and not the arms.
    # Two different silences, and the record says which.
    _, gated = _proposal(cfg, doc, obj, plan,
                         [_node("AugOnly"), _node("LDReg")], "accuracy", "CIFAR-100",
                         "LDReg")
    assert gated["failure"] == "gate_or_command", gated
    assert "SH_ALLOW_VALIDATION_DESIGN" in gated["detail"]
    assert "changed_variable" in design.missing and "settlement_condition" in design.missing

    spec = ProbeSpec(paper_id="p", arms=["baseline", "treatment"])
    spec, fv, _ = prepare(cfg, doc, obj, plan, spec, None)
    assert spec.arms == ["baseline", "treatment"], "a blocked design renames nothing"
    assert fv.disposition == "SPECIFICATION_BLOCKED"
    assert not validation.discharge(fv)
    assert outcome_for(fv, plan).disposition == "SPECIFICATION_BLOCKED"
    assert outcome_for(fv, plan).launched == 0

    # A DESIGN THAT BINDS NAMES THE ARMS ON THE SPEC, so the comparison can be established.
    full = ValidationDesign(
        design_id="fv:t1", paper_id="p", target_id="t1", question_kind="ATTRIBUTION",
        state="DESIGNED", metric="accuracy", metric_unit="%", benchmark="CIFAR-100",
        split="test", statistical_unit="example", conformance="CONFORMANT",
        changed_variables=["regulariser"], controlled_variables=["epochs"],
        arms=[ValidationArm(role="control", label="AugOnly", source="paper",
                            configuration={"regulariser": "off", "epochs": "200"}),
              ValidationArm(role="treatment", label="LDReg", source="paper",
                            configuration={"regulariser": "on", "epochs": "200"})],
        settlement=SettlementCondition(rule="DIRECTION_AGREES",
                                       predicted_direction="INCREASE",
                                       declared_before_execution=True))

    result = ProbeResult(paper_id="p", provenance="repo_exec", executions=6,
                         seeds_run=[0, 1, 2], execution_log="runs/p/execution.jsonl",
                         arms={"AugOnly": ArmStats(values=[80.0, 80.2, 79.8], mean=80.0,
                                                   std=0.2, n=3),
                               "LDReg": ArmStats(values=[82.0, 82.1, 81.9], mean=82.0,
                                                 std=0.1, n=3)})
    got = adjudicate(FocusedValidation(paper_id="p", target_id="t1", design=full),
                     ProbeSpec(paper_id="p"), result)
    assert [m.role for m in got.measurements] == ["control", "treatment"]
    assert got.measurements[0].value == 80.0 and got.measurements[1].n == 3
    assert got.comparison is not None and got.comparison.difference == 2.0
    assert got.disposition == "VALIDATION_SUPPORTS_CLAIM", got.disposition
    assert got.discharged and got.launched == 6

    # THE SAME RUN ON AN INADMISSIBLE PROVENANCE IS AN OBSERVATION, NOT A RESULT.
    synth = adjudicate(FocusedValidation(paper_id="p", target_id="t1", design=full),
                       ProbeSpec(paper_id="p"),
                       ProbeResult(**{**result.model_dump(), "provenance": "synthesized"}))
    assert synth.disposition == "VALIDATION_OBSERVATION_ONLY", synth.disposition
    assert not synth.discharged

    # AN ARM THAT RAN ONCE HAS NO INTERVAL, and None is not zero.
    once = measurements_from(full, ProbeResult(
        paper_id="p", arms={"AugOnly": ArmStats(values=[80.0], mean=80.0, std=0.0, n=1),
                            "LDReg": ArmStats(values=[82.0], mean=82.0, std=0.0, n=1)}))
    assert all(m.uncertainty is None and m.n == 1 for m in once)

    # AN ARM THAT PRODUCED NOTHING SAYS SO, and the comparison refuses rather than settles.
    empty = adjudicate(FocusedValidation(paper_id="p", target_id="t1", design=full),
                       ProbeSpec(paper_id="p"),
                       ProbeResult(paper_id="p", provenance="repo_exec", executions=2,
                                   reason="the command emitted no metric.",
                                   arms={"AugOnly": ArmStats(), "LDReg": ArmStats()}))
    assert empty.comparison.refusal == "arm_produced_no_value", empty.comparison.refusal
    assert empty.disposition == "INCONCLUSIVE" and not empty.discharged

    for fvx in (fv, got, synth, empty):
        assert fvx.disposition in TARGET_DISPOSITIONS, fvx.disposition

    totals = summarise([fv, got, synth, empty])
    assert totals == {"designs_attempted": 4, "designed": 3, "specification_blocked": 1,
                      "arms_measured": 4, "compared": 2, "settled": 1, "launched": 14}, totals
    assert summarise([]) == {"designs_attempted": 0, "designed": 0,
                             "specification_blocked": 0, "arms_measured": 0,
                             "compared": 0, "settled": 0, "launched": 0}
    print("harness.stages.validation self-check ok")
