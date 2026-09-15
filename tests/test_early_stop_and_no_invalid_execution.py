"""Two rules that decide when the expensive half of the review runs at all.

**The ASSESS phase (early stop).** The pipeline was strictly linear, and `claim_status` —
the only function that computes "a material failure was established" — runs LAST. So a
paper whose lenses raised a concern that survived quotation checking, survived the blinded
grader and counted FATAL still cloned a repository, read it statically, resolved identity,
assessed resources and launched processes before anything asked whether the answer could
still change. These tests pin that it stops, and pin just as hard that it stops ONLY the
investigation branch: discovery, the report and the ledger still run in full, because the
referee record has to be complete whatever the outcome.

**No known-invalid execution.** `provenance.admits` refuses `synthesized` and `template`
in both directions, so a harness-authored probe can never settle a printed quantity. The
probe stage ran one anyway whenever identity failed — which is exactly when it was already
known that nothing admissible could come of it. Over the eight-paper corpus that was 160
processes across 16 targets, every one refused afterwards at the ceiling. These tests pin
the rule that a process starts only when its result could speak.
"""
from __future__ import annotations

import itertools

import pytest

from harness import assessment, planner
from harness.artifacts import (PHASES, DiscoveredObject, Finding, PaperAssessment,
                               ProbeSpec)
from harness.config import Config
from harness.stages import probe as probe_stage


def _f(fid: str, severity: str, counted: str = "") -> Finding:
    return Finding(finding_id=fid, lens="overclaim", severity=severity,
                   counted_severity=counted, candidate_class="CONFIRMED_FINDING",
                   title=f"t{fid}", statement="s")


# --------------------------------------------------------------------------- #
# Where the phase sits
# --------------------------------------------------------------------------- #
def test_assess_sits_between_grading_and_discovery():
    """It needs COUNTED severity final (so after grade) and nothing expensive spent
    (so before discover)."""
    assert PHASES == ("ingest", "audit", "collect", "grade", "assess", "discover",
                      "probe", "report", "done")
    assert PHASES.index("grade") < PHASES.index("assess") < PHASES.index("discover")


# --------------------------------------------------------------------------- #
# The assessment reads findings and nothing else
# --------------------------------------------------------------------------- #
def test_model_severity_cannot_stop_the_investigation():
    assert not assessment.assess([_f("a", "FATAL")]).material_failure_established
    assert not assessment.assess([_f("a", "FATAL", counted="MINOR")]) \
        .material_failure_established, "a grader demotion must un-stop a paper"
    assert not assessment.assess([_f("a", "MAJOR")]).material_failure_established


def test_the_assessment_records_that_no_model_finding_stopped_the_paper():
    a = assessment.assess([_f("keeper", "FATAL"), _f("other", "MINOR")])
    assert a.counted_fatal_ids == []
    assert a.basis == "NONE"


def test_a_missing_assessment_leaves_the_investigation_open():
    """Defaults to OPEN, unlike every gate that protects a conclusion. This one saves
    work; degrading it closed would silently suppress every execution in the system."""
    assert assessment.investigation_open(None) is True


def test_the_assessment_agrees_with_the_reports_own_rule():
    """One table, two call sites. Two mechanisms that could disagree about whether a
    paper failed would be two verdicts, and invariant 8 says there is one."""
    from harness.stages.report import claim_status, material_failures

    for findings in ([], [_f("a", "MINOR")], [_f("a", "FATAL")],
                     [_f("a", "FATAL", counted="NOTE")],
                     [_f("a", "MAJOR"), _f("b", "FATAL")]):
        established = assessment.assess(findings).material_failure_established
        assert not material_failures(findings)
        status, _ = claim_status(findings, None)
        assert established == (status == "VERIFIED_FAILURE"), findings


# --------------------------------------------------------------------------- #
# What the stop stops, and what it does not
# --------------------------------------------------------------------------- #
def _obj(**kw) -> DiscoveredObject:
    base = dict(target_id="T1", centrality="CENTRAL", harness_addressable=True,
                routes=["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"])
    base.update(kw)
    return DiscoveredObject(**base)


@pytest.mark.parametrize("routes", [
    ["AUTHOR_CODE_EXECUTION"], ["INDEPENDENT_RECONSTRUCTION"],
    ["FOCUSED_VALIDATION_EXPERIMENT"], ["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"],
    ["NONE"],
])
def test_nothing_executes_once_a_failure_is_established(routes):
    d = planner.plan(_obj(routes=routes), artifact_available=True,
                     specification_complete=True, environment_state="ok",
                     investigation_open=False)
    assert not d.requires_execution
    assert d.action == "SUPERSEDED_BY_ESTABLISHED_FAILURE"
    assert d.necessity == "NO_EXPERIMENT_NEEDED"


def test_a_free_route_that_settles_something_is_still_taken():
    """Only EXECUTION is suppressed. An arithmetic recheck costs nothing and the referee
    record is better for it."""
    d = planner.plan(_obj(routes=["ARITHMETIC_RECHECK", "AUTHOR_CODE_EXECUTION"]),
                     artifact_available=True, investigation_open=False)
    assert d.action == "PAPER_ONLY_RESOLUTION" and not d.requires_execution


def test_every_gate_is_still_reported_when_the_investigation_stops():
    """"We did not run this" is only trustworthy when a reader can see what would have
    been checked."""
    d = planner.plan(_obj(), artifact_available=True, environment_state="ok",
                     investigation_open=False)
    assert d.gates["investigation_open"] is False
    for gate in ("worth_pursuing", "structurally_addressable", "route_exists",
                 "artifact_available", "environment_usable", "specification_complete"):
        assert gate in d.gates, gate


def test_the_stop_is_not_an_accusation_and_says_nothing_about_the_target():
    d = planner.plan(_obj(), artifact_available=True, investigation_open=False)
    assert "says nothing about" in d.reason
    assert "buys a referee nothing" in d.reason


def test_the_stop_settles_nothing_on_the_evidence_axis():
    """Invariant 22: declining to run something does not close a question."""
    from harness import taxonomy

    ev = taxonomy.evidence_state("SUPERSEDED_BY_ESTABLISHED_FAILURE", "")
    assert ev == "NOT_INVESTIGATED"
    assert not taxonomy.concerns_the_paper(ev)


def test_an_open_investigation_reproduces_the_previous_behaviour_exactly():
    """The default is the old pipeline. Swept over the reachable plan space."""
    for routes, artifact, spec_ok, env in itertools.product(
            (["AUTHOR_CODE_EXECUTION"], ["INDEPENDENT_RECONSTRUCTION"],
             ["ARITHMETIC_RECHECK"], ["PAPER_INTERNAL_CHECK"], ["NONE"]),
            (True, False), (True, False), ("ok", "blocked")):
        obj = _obj(routes=list(routes))
        explicit = planner.plan(obj, artifact_available=artifact,
                                specification_complete=spec_ok, environment_state=env,
                                investigation_open=True)
        default = planner.plan(obj, artifact_available=artifact,
                               specification_complete=spec_ok, environment_state=env)
        assert explicit.action == default.action
        assert explicit.requires_execution == default.requires_execution


# --------------------------------------------------------------------------- #
# No known-invalid execution
# --------------------------------------------------------------------------- #
def _cfg(diagnostic: bool = False) -> Config:
    cfg = Config.load()
    cfg.diagnostic_mode = diagnostic
    return cfg


@pytest.mark.parametrize("provenance", ["repo_exec", "driver"])
def test_an_admissible_spec_may_start_a_process(provenance):
    ok, why = probe_stage.admissible_if_it_succeeds(
        _cfg(), ProbeSpec(paper_id="p", provenance=provenance))
    assert ok and why == ""


@pytest.mark.parametrize("provenance", ["synthesized", "template", "", "unknown"])
def test_an_inadmissible_spec_starts_nothing(provenance):
    ok, why = probe_stage.admissible_if_it_succeeds(
        _cfg(), ProbeSpec(paper_id="p", provenance=provenance))
    assert not ok
    assert "could not have produced evidence about this paper" in why
    assert "SH_DIAGNOSTIC_MODE" in why, "the refusal names how to get the diagnostic back"


@pytest.mark.parametrize("provenance", ["synthesized", "template"])
def test_diagnostic_mode_re_enables_it_explicitly(provenance):
    ok, why = probe_stage.admissible_if_it_succeeds(
        _cfg(diagnostic=True), ProbeSpec(paper_id="p", provenance=provenance))
    assert ok and why == "diagnostic mode"


def test_diagnostic_mode_is_off_by_default():
    assert Config.load().diagnostic_mode is False


def test_the_rule_is_exactly_the_provenance_ceiling():
    """Not a second list. Whatever the ceiling admits may run, and nothing else."""
    from harness import provenance

    for p in ("repo_exec", "driver", "synthesized", "template", "", "  ", "REPO_EXEC",
              " repo_exec", "reimpl_exec"):
        ok, _ = probe_stage.admissible_if_it_succeeds(_cfg(), ProbeSpec(paper_id="p",
                                                                       provenance=p))
        assert ok == provenance.admits(p), p


def test_a_target_that_never_started_reports_zero_launches_and_not_inconclusive():
    """INCONCLUSIVE means "it ran and settled nothing". Claiming that for a run that
    never existed is the same overstatement `launched` exists to prevent."""
    from harness.artifacts import PlanDecision

    obj = _obj()
    plan = PlanDecision(target_id="T1", action="AUTHOR_CODE_REPRODUCTION",
                        route="AUTHOR_CODE_EXECUTION", requires_execution=True)
    out = probe_stage._not_started(obj, plan, "nothing bound")
    assert out.launched == 0
    assert out.disposition == "IDENTITY_BLOCKED"
    assert not out.establishes_failure
    assert not out.concerns_the_paper, "a run that never happened says nothing about the paper"
