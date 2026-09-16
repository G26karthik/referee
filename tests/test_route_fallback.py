"""Step 6 — route fallback: AUTHOR_CODE_EXECUTION identity failure re-plans.

**What this closes.** `discovery._routes` used to treat AUTHOR_CODE_EXECUTION and
INDEPENDENT_RECONSTRUCTION as mutually exclusive on `repo_available` alone: a repository
existing meant reconstruction was never even offered, however the identity binding
against that repository turned out. A target whose cited cell bound to none of the
repository's commands — `no_candidate`, `ambiguous`, `unsupported` — ended IDENTITY_BLOCKED
with nothing else considered, even on a paper whose method section says enough to attempt
an independent rebuild.

**Discovery cannot fix this alone**, because whether identity binds is a fact only a real
checkout establishes, and discovery runs before one exists. So the correction is two
pieces working together:

  1. `discovery._routes` offers INDEPENDENT_RECONSTRUCTION ALONGSIDE
     AUTHOR_CODE_EXECUTION when the paper's own specification is complete — a structural
     fact discovery CAN know in advance.
  2. `stages.probe.replan_after_author_code_exhausted` is consulted once a real checkout
     has actually tried and failed to bind identity, and re-invokes
     `planner.plan(..., author_code_exhausted=True)` to pick the fallback route FROM
     the routes discovery already offered.

Neither piece invents a route the other did not already sanction: the planner's re-plan
only ever reaches a route discovery put in `obj.routes`, and discovery's widened offer
never runs anything by itself — nothing executes differently until `stages.probe` asks.

**The bookkeeping is an ORDERED LOG, not an overwrite.** A re-plan produces a SECOND
`PlanDecision` for the same `target_id`, appended after the first; the first's
`superseded_by` is set to record what replaced it. Every existing reader that resolves
"the plan currently in force for a target" via `{p.target_id: p for p in ts.plans}` keeps
the LAST entry for a repeated key, so nothing downstream needs to change to keep working.
"""
from __future__ import annotations

import inspect

import pytest

from harness import discovery, planner
from harness.config import Config
from harness.artifacts import (DiscoveredObject, ExperimentIdentity, MetricIdentity,
                               ConfigurationIdentity, PlanDecision, ProbeSpec)
from harness.stages import probe as probe_stage


def _routes(question_kind: str, *, repo: bool, value: bool = True,
            spec_complete: bool = False) -> list[str]:
    return discovery._routes(
        "SCIENTIFIC_CLAIM", None, repo_available=repo, has_value=value,
        arithmetic_broken=False, lens="", question_kind=question_kind,
        specification_complete=spec_complete)


# --------------------------------------------------------------------------- #
# discovery._routes: offer BOTH when the repo exists and the paper specifies enough
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question_kind", ["PRINTED_QUANTITY", "COMPOSITION"])
def test_reconstruction_is_offered_alongside_author_code_when_spec_is_complete(
        question_kind):
    got = _routes(question_kind, repo=True, spec_complete=True)
    assert "AUTHOR_CODE_EXECUTION" in got and "INDEPENDENT_RECONSTRUCTION" in got
    assert got.index("AUTHOR_CODE_EXECUTION") < got.index("INDEPENDENT_RECONSTRUCTION"), (
        "the authors' own code is always preferred — invariant 15")


@pytest.mark.parametrize("question_kind", ["PRINTED_QUANTITY", "COMPOSITION"])
def test_without_a_complete_specification_the_pre_step_6_behaviour_is_unchanged(
        question_kind):
    """`specification_complete` defaults to False. Every existing caller that does not
    pass it gets exactly the route list it always did."""
    got = _routes(question_kind, repo=True, spec_complete=False)
    assert "AUTHOR_CODE_EXECUTION" in got
    assert "INDEPENDENT_RECONSTRUCTION" not in got, (
        "inventing the missing half of an unspecified experiment measures our "
        "reconstruction, not the paper")


@pytest.mark.parametrize("spec_complete", [True, False])
def test_with_no_repository_reconstruction_is_still_the_unconditional_fallback(
        spec_complete):
    """The one case Step 6 must NOT change: no artifact at all, so reconstruction is not
    a fallback alongside anything — it is the only route there ever was."""
    got = _routes("PRINTED_QUANTITY", repo=False, spec_complete=spec_complete)
    assert got == ["INDEPENDENT_RECONSTRUCTION"]


@pytest.mark.parametrize("question_kind",
                         ["ATTRIBUTION", "CONTROL_PRESENCE", "PROTOCOL_CONFORMANCE"])
def test_the_three_widened_question_kinds_are_unaffected(question_kind):
    """These kinds already carried INDEPENDENT_RECONSTRUCTION as a no-artifact fallback
    from Step 5's own widening (BETWEEN_ARMS routes need no printed value). Step 6 must
    not change their behaviour — it is scoped to PRINTED_QUANTITY/COMPOSITION, the two
    kinds whose routes are AGAINST_PRINTED_VALUE and therefore exclusive by construction
    before this fix."""
    with_spec = _routes(question_kind, repo=True, value=False, spec_complete=True)
    without_spec = _routes(question_kind, repo=True, value=False, spec_complete=False)
    assert with_spec == without_spec, (
        "specification_complete must only matter where Step 6 says it does")


def test_discover_threads_specification_complete_through_to_the_object():
    from harness.artifacts import Finding

    f = Finding(finding_id="c-01", lens="overclaim", candidate_class="CONFIRMED_FINDING",
               evidence_ref="p1", evidence_quote="the accuracy is 61.4")
    from harness.artifacts import PaperDoc, Section

    doc = PaperDoc(paper_id="p", repo_url="https://example.invalid/r",
                   sections=[Section(section_idx=0, title="Abstract",
                                     text="the accuracy is 61.4. Nothing else follows.")])
    without = discovery.discover(doc, [f])[0]
    with_spec = discovery.discover(doc, [f], specification_complete=True)[0]
    obj_without = next(o for o in without if o.kind == "SCIENTIFIC_CLAIM")
    obj_with = next(o for o in with_spec if o.kind == "SCIENTIFIC_CLAIM")
    assert obj_without.question_kind == obj_with.question_kind == "PRINTED_QUANTITY"
    assert "INDEPENDENT_RECONSTRUCTION" not in obj_without.routes
    assert "INDEPENDENT_RECONSTRUCTION" in obj_with.routes


# --------------------------------------------------------------------------- #
# planner.plan(..., author_code_exhausted=True): the re-plan primitive
# --------------------------------------------------------------------------- #
def _obj(routes: list[str], **kw) -> DiscoveredObject:
    base = dict(target_id="T1", centrality="CENTRAL", harness_addressable=True,
               routes=routes)
    base.update(kw)
    return DiscoveredObject(**base)


def test_the_first_plan_prefers_author_code_when_both_routes_are_offered():
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    d = planner.plan(obj, artifact_available=True, specification_complete=True,
                     environment_state="ok")
    assert d.action == "AUTHOR_CODE_REPRODUCTION" and d.route == "AUTHOR_CODE_EXECUTION"
    assert d.attempt == 1 and d.superseded_by == ""


def test_author_code_exhausted_falls_back_to_reconstruction():
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    d = planner.plan(obj, artifact_available=True, specification_complete=True,
                     environment_state="ok", author_code_exhausted=True, attempt=2)
    assert d.action == "INDEPENDENT_RECONSTRUCTION" and d.route == "INDEPENDENT_RECONSTRUCTION"
    assert d.requires_execution
    assert d.attempt == 2, "the caller numbers attempts; plan() only carries the number"


def test_author_code_exhausted_never_invents_a_route_discovery_did_not_offer():
    """The re-plan is a FILTER over `obj.routes`, never a second opinion about whether
    reconstruction applies at all. An object discovery never gave the route to still has
    none after exhaustion."""
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"])  # no reconstruction
    d = planner.plan(obj, artifact_available=True, specification_complete=True,
                     environment_state="ok", author_code_exhausted=True)
    assert d.route != "AUTHOR_CODE_EXECUTION"
    assert d.action != "INDEPENDENT_RECONSTRUCTION"


def test_a_re_plan_without_the_flag_reproduces_the_original_decision():
    """`author_code_exhausted` is a per-call filter, not a mutation. Calling `plan()`
    again on the same object without it must reproduce the FIRST decision exactly."""
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    first = planner.plan(obj, artifact_available=True, specification_complete=True,
                         environment_state="ok")
    again = planner.plan(obj, artifact_available=True, specification_complete=True,
                         environment_state="ok")
    assert first.action == again.action and first.route == again.route


def test_attempt_is_never_decided_by_plan_itself():
    """`plan()` carries whatever `attempt` it is given and decides nothing about
    numbering — the caller (`stages.probe`) owns that."""
    obj = _obj(["AUTHOR_CODE_EXECUTION"])
    for n in (1, 2, 5):
        d = planner.plan(obj, artifact_available=True, attempt=n)
        assert d.attempt == n


def test_the_signature_admits_the_new_parameters_without_widening_the_discipline():
    sig = inspect.signature(planner.plan)
    assert "author_code_exhausted" in sig.parameters
    assert "attempt" in sig.parameters
    assert str(sig.parameters["author_code_exhausted"].annotation) == "bool"
    assert str(sig.parameters["attempt"].annotation) == "int"


# --------------------------------------------------------------------------- #
# stages.probe.identity_failed: a DECIDED failure, never a merely-unassessed one
# --------------------------------------------------------------------------- #
def _spec(**idents) -> ProbeSpec:
    return ProbeSpec(paper_id="p", **idents)


@pytest.mark.parametrize("state", ["ambiguous", "no_candidate", "unsupported"])
def test_a_decided_identity_failure_on_any_axis_counts(state):
    assert probe_stage.identity_failed(_spec(experiment=ExperimentIdentity(state=state)))
    assert probe_stage.identity_failed(_spec(metric_identity=MetricIdentity(state=state)))
    assert probe_stage.identity_failed(
        _spec(configuration=ConfigurationIdentity(state=state)))


@pytest.mark.parametrize("state", ["unmapped", "established"])
def test_unmapped_and_established_are_not_failures(state):
    """`unmapped` means identity was never reached at all — no checkout, a shut gate, an
    earlier precondition failing first — and is not grounds for a fallback: the fallback
    is for a route this review TRIED and could not bind, not one it never got to."""
    assert not probe_stage.identity_failed(_spec(experiment=ExperimentIdentity(state=state)))


def test_a_spec_with_no_identity_assessed_at_all_is_not_a_failure():
    assert not probe_stage.identity_failed(_spec())


# --------------------------------------------------------------------------- #
# stages.probe.replan_after_author_code_exhausted: the wiring, and its three guards
# --------------------------------------------------------------------------- #
def _failed_spec() -> ProbeSpec:
    return _spec(experiment=ExperimentIdentity(state="ambiguous"))


def test_the_regression_this_step_exists_to_fix():
    """APT-ICML's shape: 84 candidate commands, none of them bound. Before Step 6 this
    target ended IDENTITY_BLOCKED with nothing else considered, even though the object
    already offered INDEPENDENT_RECONSTRUCTION."""
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    plan = planner.plan(obj, artifact_available=True, specification_complete=True,
                        environment_state="ok")
    assert plan.route == "AUTHOR_CODE_EXECUTION"

    fallback = probe_stage.replan_after_author_code_exhausted(obj, plan, _failed_spec())
    assert fallback is not None
    assert fallback.route == "INDEPENDENT_RECONSTRUCTION"
    assert fallback.requires_execution
    assert fallback.attempt == plan.attempt + 1


def test_the_original_attempt_is_kept_not_overwritten():
    """`ts.plans` becomes an ordered log: the first attempt's own record survives, marked
    with what replaced it, rather than being discarded."""
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    plan = planner.plan(obj, artifact_available=True, specification_complete=True,
                        environment_state="ok")
    assert plan.superseded_by == ""
    fallback = probe_stage.replan_after_author_code_exhausted(obj, plan, _failed_spec())
    assert fallback is not None
    assert plan.superseded_by == fallback.route == "INDEPENDENT_RECONSTRUCTION", (
        "the ORIGINAL PlanDecision is mutated to point at what replaced it")


def test_no_fallback_when_the_original_route_was_not_author_code():
    obj = _obj(["ARTIFACT_INSPECTION", "FOCUSED_VALIDATION_EXPERIMENT"])
    plan = PlanDecision(target_id="T1", action="FOCUSED_VALIDATION_EXPERIMENT",
                        route="FOCUSED_VALIDATION_EXPERIMENT", requires_execution=True)
    assert probe_stage.replan_after_author_code_exhausted(
        obj, plan, _failed_spec()) is None
    assert plan.superseded_by == "", "an attempt that was never re-planned stays untouched"


def test_no_fallback_when_discovery_never_offered_reconstruction():
    """The re-plan is a filter over routes discovery already sanctioned. An object with
    no repository specification never had INDEPENDENT_RECONSTRUCTION to fall back to."""
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"])
    plan = PlanDecision(target_id="T1", action="AUTHOR_CODE_REPRODUCTION",
                        route="AUTHOR_CODE_EXECUTION", requires_execution=True)
    assert probe_stage.replan_after_author_code_exhausted(
        obj, plan, _failed_spec()) is None


def test_no_fallback_when_identity_never_actually_failed():
    """A capable, resource-sufficient run that simply has not been attempted yet — or one
    whose identity DID bind — must not trigger a fallback nobody asked for."""
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    plan = PlanDecision(target_id="T1", action="AUTHOR_CODE_REPRODUCTION",
                        route="AUTHOR_CODE_EXECUTION", requires_execution=True)
    assert probe_stage.replan_after_author_code_exhausted(
        obj, plan, _spec()) is None, "unmapped identity is not a failure"
    assert probe_stage.replan_after_author_code_exhausted(
        obj, plan, _spec(experiment=ExperimentIdentity(state="established"))) is None


def test_a_fallback_that_cannot_execute_either_still_carries_its_own_reason():
    """A paper whose reconstruction route itself refuses (e.g. the environment is
    blocked) still gets a real, informative `PlanDecision` rather than a bare None —
    `_fallback_note` reads its `.reason` directly."""
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    plan = planner.plan(obj, artifact_available=True, specification_complete=True,
                        environment_state="ok")
    fallback = probe_stage.replan_after_author_code_exhausted(obj, plan, _failed_spec())
    assert fallback is not None and fallback.reason


# --------------------------------------------------------------------------- #
# _fallback_note: honest about whichever gate stopped it, never a fixed claim
# --------------------------------------------------------------------------- #
def test_fallback_note_is_empty_when_there_is_no_fallback():
    assert probe_stage._fallback_note(Config(), None) == ""


def test_fallback_note_is_honest_about_the_closed_driver_gate():
    """Step 8's driver exists now, but its gate is closed by default — the note must say
    THAT, not the pre-Step-8 claim that no driver for the route exists at all."""
    fallback = PlanDecision(target_id="T1", action="INDEPENDENT_RECONSTRUCTION",
                            route="INDEPENDENT_RECONSTRUCTION", requires_execution=True,
                            attempt=2)
    note = probe_stage._fallback_note(Config(allow_reimplementation_driver=False), fallback)
    assert "INDEPENDENT_RECONSTRUCTION" in note
    assert "no reviewer is configured" in note
    assert "did not run" in note


def test_fallback_note_is_honest_about_the_closed_exec_gate():
    """Even with a reviewer configured to WRITE one, the separate execution gate —
    decision 10's boundary — is what actually stops it, and the note must name that gate
    specifically rather than a generic 'no driver' claim."""
    fallback = PlanDecision(target_id="T1", action="INDEPENDENT_RECONSTRUCTION",
                            route="INDEPENDENT_RECONSTRUCTION", requires_execution=True,
                            attempt=2)
    cfg = Config(allow_reimplementation_driver=True, reimplementation_cmd="x {prompt} {out}",
                allow_reimplementation_exec=False)
    note = probe_stage._fallback_note(cfg, fallback)
    assert "SH_ALLOW_REIMPLEMENTATION_EXEC" in note


def test_fallback_note_carries_the_planners_own_refusal_reason():
    fallback = PlanDecision(target_id="T1", action="INFEASIBLE_ENVIRONMENT",
                            route="INDEPENDENT_RECONSTRUCTION", requires_execution=False,
                            reason="the experiment cannot be set up on this host.",
                            attempt=2)
    note = probe_stage._fallback_note(Config(), fallback)
    assert "considered and refused" in note
    assert "cannot be set up on this host" in note


# --------------------------------------------------------------------------- #
# PlanDecision.attempt / superseded_by — defaults and closed behaviour
# --------------------------------------------------------------------------- #
def test_plan_decision_defaults_to_a_single_untouched_attempt():
    d = PlanDecision(target_id="T1")
    assert d.attempt == 1
    assert d.superseded_by == ""


# --------------------------------------------------------------------------- #
# THE DEFECT A RE-PLANNED TARGET COULD OTHERWISE CAUSE: double-counting the funnel
# --------------------------------------------------------------------------- #
# `TargetSet.plans` becomes an ORDERED LOG the moment a re-plan happens: a target whose
# identity failed gets a SECOND `PlanDecision` appended after the first, which is what
# makes the "current plan" a per-target LOOKUP rather than a 1:1 list. Every count that
# used to read `ts.plans` directly now has to read `planner.current_plans(ts.plans)`
# instead, or a re-planned target is counted twice — discovered on the real corpus, where
# `targets_warranting_experiment` moved 88 -> 89 for a corpus of unchanged papers the
# moment one target's identity genuinely failed and a fallback was recorded.
def test_current_plans_deduplicates_a_re_planned_target():
    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    first = planner.plan(obj, artifact_available=True, specification_complete=True,
                         environment_state="ok")
    fallback = probe_stage.replan_after_author_code_exhausted(obj, first, _failed_spec())
    assert fallback is not None

    raw = [first, fallback]
    assert sum(1 for p in raw if p.requires_execution) == 2, (
        "the raw list double-counts one target as two — this is the bug, reproduced")
    deduped = planner.current_plans(raw)
    assert len(deduped) == 1
    assert deduped[0] is fallback, "the LAST entry for a repeated target_id wins"
    assert sum(1 for p in deduped if p.requires_execution) == 1


def test_the_ledgers_funnel_terms_do_not_double_count_a_fallback():
    """An integration check on the actual regression: `harness.ledger.build`'s
    `targets_warranting_experiment` and `targets_requiring_execution` must read the
    de-duplicated plan list, not `ts.plans` directly."""
    from harness import ledger as ledger_mod
    from harness.artifacts import EvalReport, TargetSet

    obj = _obj(["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"])
    first = planner.plan(obj, artifact_available=True, specification_complete=True,
                         environment_state="ok")
    fallback = probe_stage.replan_after_author_code_exhausted(obj, first, _failed_spec())
    assert fallback is not None

    ts = TargetSet(paper_id="p", objects=[obj], plans=[first, fallback])
    eff = ledger_mod.build(EvalReport(paper_id="p"), ts).efficiency
    assert eff["targets_warranting_experiment"] == 1, eff
    assert eff["targets_requiring_execution"] == 1, eff


# --------------------------------------------------------------------------- #
# Direct PATH B routing: an initial reconstruction plan is itself executable
# --------------------------------------------------------------------------- #
def test_review_attempts_direct_reconstruction_for_primary_and_later_targets(
        monkeypatch, tmp_path):
    """Drive the real `_review` dispatch for the no-repository PATH B shape.

    Discovery already chose INDEPENDENT_RECONSTRUCTION for both targets.  Before this
    regression, `_review` only passed the *fallback* returned after AUTHOR_CODE identity
    failure to `attempt_reimplementation_fallback`; a direct plan therefore passed None
    and never reached the reconstruction driver.  Make the base specs look admissible on
    purpose: the selected route must still win, and `_run` must never execute those base
    specs instead.
    """
    from harness.artifacts import (CodeAudit, PaperDoc, ProbeResult, Reconciliation,
                                   RepoAcquisition, Section, TargetSet)

    pid = "p"
    objects = [
        _obj(["INDEPENDENT_RECONSTRUCTION"], target_id=tid,
             kind="REPRODUCTION_TARGET", claim_text=f"claim for {tid}")
        for tid in ("T1", "T2")
    ]
    plans = [
        PlanDecision(target_id=o.target_id, action="INDEPENDENT_RECONSTRUCTION",
                     route="INDEPENDENT_RECONSTRUCTION", requires_execution=True)
        for o in objects
    ]
    target_set = TargetSet(paper_id=pid, objects=objects, plans=plans)
    pairs = list(zip(objects, plans))
    doc = PaperDoc(
        paper_id=pid, title="Direct reconstruction",
        sections=[Section(section_idx=0, title="Method", text="A complete method.")])

    project = tmp_path / pid
    (project / "paper").mkdir(parents=True)
    (project / "paper" / "doc.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(probe_stage.state, "project_dir", lambda cfg, p: project)
    monkeypatch.setattr(probe_stage.state, "read_json", lambda path: doc.model_dump())
    monkeypatch.setattr(probe_stage.state, "write_json", lambda *a, **k: None)
    monkeypatch.setattr(probe_stage.state, "set_phase", lambda *a, **k: None)
    monkeypatch.setattr(probe_stage.state, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(probe_stage, "_executable_targets",
                        lambda cfg, p: (target_set, pairs, []))
    monkeypatch.setattr(
        probe_stage, "build_spec",
        lambda cfg, p, d, target=None: ProbeSpec(
            paper_id=p, target_id=target.target_id if target else ""))
    monkeypatch.setattr(
        probe_stage, "acquire_and_audit",
        lambda *a, **k: (RepoAcquisition(status="unavailable"),
                         CodeAudit(status="not_attempted")))
    monkeypatch.setattr(probe_stage, "synthesize_probe", lambda cfg, d, s, a: s)
    monkeypatch.setattr(probe_stage, "plan_execution",
                        lambda cfg, s, a, d, au, root=None: s)
    monkeypatch.setattr(probe_stage, "establish_comparison", lambda s, route, **kw: s)
    monkeypatch.setattr(probe_stage, "may_be_compared", lambda s: (True, ""))
    monkeypatch.setattr(probe_stage, "admissible_if_it_succeeds", lambda cfg, s: (True, ""))

    attempted = []

    def fake_reconstruction(cfg, root, paper_id, paper, base_spec, reconstruction_plan,
                            out_dir=None):
        attempted.append((base_spec.target_id, reconstruction_plan.route, out_dir))
        return ProbeResult(
            paper_id=paper_id, provenance="reimpl_exec", verdict="done",
            reason="reconstruction ran", executions=1, script_path="reimpl.py",
            reconciliation=Reconciliation(
                status="RESOLVED_VERIFIED", provenance="reimpl_exec",
                reason="independent reconstruction matched"))

    monkeypatch.setattr(probe_stage, "attempt_reimplementation_fallback",
                        fake_reconstruction)

    def wrong_base_run(*args, **kwargs):
        raise AssertionError("a direct reconstruction plan ran the base probe instead")

    monkeypatch.setattr(probe_stage, "_run", wrong_base_run)

    probe_stage._review(Config(projects_dir=tmp_path), pid)

    assert [(tid, route) for tid, route, _ in attempted] == [
        ("T1", "INDEPENDENT_RECONSTRUCTION"),
        ("T2", "INDEPENDENT_RECONSTRUCTION"),
    ]
    assert attempted[0][2] is None, "the primary uses the primary reconstruction directory"
    assert attempted[1][2] == project / "runs" / pid / "targets" / "T2" / "reimplementation"
    assert len(target_set.plans) == 2, "a direct plan is not appended again as a fallback"
    assert [o.route for o in target_set.outcomes] == [
        "INDEPENDENT_RECONSTRUCTION", "INDEPENDENT_RECONSTRUCTION"]
    assert all(o.provenance == "reimpl_exec" for o in target_set.outcomes)
    assert all(o.disposition == "REPRODUCED" for o in target_set.outcomes)
