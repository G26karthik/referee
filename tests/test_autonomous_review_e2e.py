"""End to end, with no human choosing what to try next.

The property under test is the one the architecture exists for: a paper enters, several
independent lenses read it, the harness itself decides what is checkable, a dedicated
priority mechanism decides what to pursue, a deterministic gate decides whether an
experiment is justified, the authors' own code runs, the result is bound to the target it
was requested for, and the triage falls out of typed evidence. No step is selected by an
operator and no step is selected by a model.

The counterpart property matters just as much and has its own fixtures below: when the
paper does not specify enough, the system refuses to invent an experiment and reports the
question unresolved rather than resolving it against the paper.

**What the git fixtures are and are not.** They are a throwaway repository this harness
wrote, and they establish that the execution path works. They say nothing about any
paper. A real reproduction additionally needs the paper's own repository, its commit, and
identity resolved from the paper's own claims — none of which a fixture can supply.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness import controller, state
from harness.artifacts import (CaseLedger, ConfigurationIdentity, EvalReport, ExecCapability,
                               ExperimentIdentity, MetricIdentity, PaperDoc, ProbeSpec,
                               QuantFinding, ResourceCapability, Section,
                               Table, TargetSet)
from harness.config import Config
from harness.local_exec import run_probe
from harness.repo import head_commit
from harness.stages import audit as audit_stage
from harness.stages import discover as discover_stage
from harness.stages import probe as probe_stage
from harness.stages.report import triage
from conftest import material_objects

PID = "e2e-paper"
# The paper's prose claim. Its composition is checkable without running anything, and its
# total is a quantity an execution can re-derive by producing the items and counting them.
COMPOSITION = "58 topics x 5 templates x 10 instances = 2,900 test cases"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> None:
    p = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"


# Produces FEWER cases than the paper claims — five of the 58 generators are broken, so
# 53 x 5 x 10 = 2,650 rather than 2,900. Emits a JSON summary rather than the SH_METRIC
# contract, so the target-aware binding in `local_exec.parse_metric` is what has to find it.
GENERATOR = '''\
import argparse, json
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
print("SH_DEVICE cpu", flush=True)
working = 53
print(json.dumps({"epoch": 1, "loss": 0.5}), flush=True)
print(json.dumps({"n_cases": working * 5 * 10}), flush=True)
'''

GENERATOR_AGREES = GENERATOR.replace("working = 53", "working = 58")

README = """\
# Benchmark generators

To build the benchmark, run:

```
python generate.py --seed 0
```
"""


def _repo(tmp_path: Path, body: str, name: str = "gen") -> tuple[Path, str]:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    (repo / "generate.py").write_text(body, encoding="utf-8")
    (repo / "README.md").write_text(README, encoding="utf-8")
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    return repo, head_commit(repo)


def _doc(repo_url: str = "", *, specified: bool = True) -> PaperDoc:
    setup = ("We train with the Adam optimizer for 30 epochs at a learning rate of 1e-3 on "
             "the CIFAR-100 dataset and report accuracy on the test set. The objective is a "
             "weighted sum. Generation requires 1 GB of GPU memory and finishes in under 60 "
             "seconds." if specified else
             "We use our method with the settings described in the appendix.")
    return PaperDoc(
        paper_id=PID, title="A Benchmark Paper", n_pages=6, content_sha="b" * 12,
        repo_url=repo_url, repo_urls=[repo_url] if repo_url else [],
        sections=[Section(section_idx=0, title="Abstract", page_start=1,
                          text=f"We introduce a benchmark. {COMPOSITION}. The benchmark is "
                               f"generated programmatically."),
                  Section(section_idx=1, title="Setup", page_start=2, text=setup)],
        tables=[Table(table_idx=1, page=4, caption="Table 1: accuracy",
                      header=["method", "accuracy"], rows=[["ours", "61.4"]])],
        reported_numbers=[QuantFinding(value="61.4", metric="accuracy", method="ours",
                                       source_quote="61.4", page=4, table_ref="T1:r0:c1")])


def _plant(cfg: Config, doc: PaperDoc) -> None:
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    if not (cfg.projects_dir / PID / "project.json").exists():
        state.create_project(cfg, "", doc.title, pid=PID)
    state.write_json(cfg.projects_dir / PID / "paper" / "doc.json", doc.model_dump())


def _lenses(cfg: Config, *, verifiable: bool = False, quote: str = "61.4",
            ref: str = "T1:r0:c1") -> None:
    """Plant one finding per lens, anchored where the caller says.

    `quote`/`ref` are parameters because WHICH address the panel attacks decides which
    target `harness.priority` ranks first, and some tests need the prose composition to
    be primary rather than the table cell. The default is the cell, so every existing
    caller is unchanged.
    """
    for lens in audit_stage.LENSES:
        audit_stage.accept_lens(cfg, PID, lens, json.dumps({
            "lens": lens, "findings": [{
                "finding_id": f"{lens}-01", "severity": "MINOR",
                "title": f"{lens} observation", "statement": "a concern",
                "candidate_class": "PLAUSIBLE_CONCERN",
                "evidence_quote": quote, "evidence_ref": ref,
                "verifiable_by_experiment": verifiable}]}))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(projects_dir=tmp_path / "projects")
    c.probe_timeout_s = 120
    return c


def _established(cls):
    return cls(state="established", established=True, reason="fixture")


# --------------------------------------------------------------------------- #
# §33 — the acceptance path, driven with no human choosing anything
# --------------------------------------------------------------------------- #
def test_the_pipeline_discovers_prioritises_and_plans_without_an_operator(cfg: Config, tmp_path):
    repo, _ = _repo(tmp_path, GENERATOR)
    _plant(cfg, _doc(repo.as_uri()))
    _lenses(cfg)

    case = controller.drive(cfg, controller.open_case(cfg, PID), skip_probe=True)
    assert case.status == "complete", case.blocked_reason

    ts = discover_stage.load(cfg, PID)
    assert ts is not None

    # multiple scientifically relevant targets exist, and more than one kind
    assert len(ts.objects) >= 4
    assert len({o.kind for o in ts.objects}) >= 3

    # the claim/question set does not come from a single unchecked lens boolean
    assert all(not o.proposed_by_lens for o in ts.objects), "the fixture lenses all said no"
    assert any(o.harness_addressable for o in ts.objects), "the harness found targets anyway"

    # a dedicated priority mechanism ordered them, and it is not the report display sort
    assert ts.objects == sorted(ts.objects, key=lambda o: (-o.priority, o.target_id))
    assert ts.objects[0].priority_reason.startswith("centrality=")

    # The count exists and is a count. `>= 0` was the assertion here and it is true of
    # every integer, so it did not test the comment above it. What this fixture actually
    # establishes is that the term is reported at all, separately from the launch count.
    resolved_without_execution = ts.extraction_coverage["targets_resolved_without_execution"]
    assert isinstance(resolved_without_execution, int)
    assert resolved_without_execution <= len(ts.objects)
    assert ts.extraction_coverage["repository_advertised"] is True

    # at least one target legitimately asked for an execution
    assert any(p.requires_execution for p in ts.plans)

    # ...and every plan that did not is a NAMED refusal, never silence
    for p in ts.plans:
        assert p.action and (p.requires_execution or p.reason)


def test_the_prose_claim_reaches_a_probe_spec_with_no_hand_written_json(cfg: Config, tmp_path):
    """The autonomous-verdict gap, closed.

    A prose-stated result used to be unreachable: `grounded_claimed_delta` refused
    anything that was not a cell address and `ProbeSpec.table_ref` was only ever set from
    one, so `reconcile` never ran on it and the only way to pursue such a claim was for an
    operator to hand-write `runs/<pid>/spec.json`.
    """
    repo, _ = _repo(tmp_path, GENERATOR)
    doc = _doc(repo.as_uri())
    _plant(cfg, doc)
    _lenses(cfg)
    controller.drive(cfg, controller.open_case(cfg, PID), skip_probe=True)

    ts = discover_stage.load(cfg, PID)
    prose = next(o for o in ts.objects if o.ref and o.ref.kind == "prose_claim")
    assert prose.expected_value == 2900.0

    spec = probe_stage.build_spec(cfg, PID, doc, prose)
    assert spec.claim_kind == "prose_claim" and spec.claim_ref.startswith("P0:")
    assert spec.target_id == prose.target_id
    assert spec.table_ref == "", "a prose address must not masquerade as a cell"
    assert "2,900" in spec.claimed_cell_value or "2900" in spec.claimed_cell_value


def test_identity_binds_a_prose_count_to_the_command_that_emits_one(tmp_path):
    """The last gate before execution. Without a prose branch here every prose target
    would answer 'no cell address' and stop, one step short of the thing it was built for.
    """
    from harness import experiment_id
    repo, _ = _repo(tmp_path, GENERATOR)
    doc = _doc(repo.as_uri())
    exp, met, conf = experiment_id.resolve(
        doc, repo, "", claim_ref="P0:1-50", claim_quote=COMPOSITION)
    assert exp.established and exp.command is not None
    assert "generate.py" in " ".join(exp.command.argv)
    assert met.established and met.output_key == "n_cases" and met.cell_quantity == "count"
    assert conf.established and conf.seed_policy_match is None

    ok, cls, why = experiment_id.identities_established(exp, met, conf)
    assert ok, f"{cls}: {why}"


def test_a_prose_claim_naming_no_population_is_refused_not_guessed(tmp_path):
    from harness import experiment_id
    repo, _ = _repo(tmp_path, GENERATOR)
    exp, met, _ = experiment_id.resolve(
        _doc(repo.as_uri()), repo, "", claim_ref="P0:1-50",
        claim_quote="the learning rate 3 x 2 x 1 = 6")
    assert not exp.established and not met.established
    assert "does not name a population" in exp.reason


# --------------------------------------------------------------------------- #
# §20E — the authors' own code failing a genuine target establishes RED
# --------------------------------------------------------------------------- #
def _qualified_spec(repo: Path, commit: str, cfg: Config, claimed: str) -> ProbeSpec:
    """Every precondition genuinely satisfied, addressed at a PROSE claim.

    Identity, capability, resources and the commit are supplied directly here rather than
    resolved, exactly as `tests/test_local_execution.py` does: this test is about what the
    execution and reconciliation layers do once the gates have passed, and resolving them
    is what the tests above cover.
    """
    return ProbeSpec(
        paper_id=PID, provenance="repo_exec",
        command=[cfg.python, "generate.py", "--seed", "{seed}"],
        cwd=str(repo), interpreter=cfg.python, commit=commit,
        arms=["reproduction"], seeds=[0, 1],
        claim_ref="P0:20-70", claim_kind="prose_claim", target_id="TGT-REP-P0.20-70",
        claimed_cell_value=claimed, metric="count",
        experiment=_established(ExperimentIdentity),
        metric_identity=MetricIdentity(state="established", established=True,
                                       reason="fixture", output_key="n_cases",
                                       cell_quantity="count", output_quantity="count",
                                       cell_basis="absolute", output_basis="absolute"),
        configuration=_established(ConfigurationIdentity),
        capability=ExecCapability(established=True, reason_code="established",
                                  interpreter_is_repo_env=True),
        resources=ResourceCapability(state="satisfied", reason="fixture fits"))


def test_author_code_that_misses_a_prose_stated_total_establishes_red(confined_local, cfg: Config, tmp_path):
    repo, commit = _repo(tmp_path, GENERATOR)
    cfg.allow_repo_exec = True
    result = run_probe(cfg, tmp_path / "projects" / PID,
                       _qualified_spec(repo, commit, cfg, "2,900"))

    rec = result.reconciliation
    assert rec is not None, result.reason
    assert rec.claim_kind == "prose_claim" and rec.claim_ref == "P0:20-70"
    assert rec.claimed_value == 2900.0
    assert rec.reproduced_value == 2650.0, "the target-aware parse must find n_cases"
    assert rec.status == "FAILED_REPRODUCTION", rec.reason

    outcome = probe_stage.outcome_for("TGT-REP-P0.20-70", result,
                                      "AUTHOR_CODE_REPRODUCTION", "AUTHOR_CODE_EXECUTION")
    assert outcome.disposition == "FAILED_REPRODUCTION" and outcome.establishes_failure
    # Establishing the failure is Tier 1, and it is what this fixture proves: the authors'
    # own code ran, at a verified commit, bound to the prose total, and missed it. Reaching
    # the PAPER level additionally needs a central claim to depend on the target — the
    # composition is the abstract's own sentence here, so it does (`harness.materiality`).
    level, why = triage([], outcomes=[outcome],
                        objects=material_objects("TGT-REP-P0.20-70"))
    assert level == "RED" and "TGT-REP-P0.20-70" in why


def test_author_code_that_matches_the_prose_total_reproduces(confined_local, cfg: Config, tmp_path):
    repo, commit = _repo(tmp_path, GENERATOR_AGREES, name="gen_ok")
    cfg.allow_repo_exec = True
    result = run_probe(cfg, tmp_path / "projects" / PID,
                       _qualified_spec(repo, commit, cfg, "2,900"))
    rec = result.reconciliation
    assert rec.reproduced_value == 2900.0
    assert rec.status == "RESOLVED_VERIFIED", rec.reason
    outcome = probe_stage.outcome_for("T", result, "AUTHOR_CODE_REPRODUCTION",
                                      "AUTHOR_CODE_EXECUTION")
    assert outcome.disposition == "REPRODUCED" and not outcome.establishes_failure


# --------------------------------------------------------------------------- #
# §20F — one paper, mixed outcomes, explained as such
# --------------------------------------------------------------------------- #
def test_one_paper_reports_a_failure_and_a_reproduction_together(confined_local, cfg: Config, tmp_path):
    """The system must preserve both rather than collapsing them into one score."""
    from harness import ledger
    from harness.artifacts import DiscoveredObject, TargetOutcome
    from harness.stages.report import render_reviewer_report

    bad, bad_commit = _repo(tmp_path, GENERATOR, name="bad")
    good, good_commit = _repo(tmp_path, GENERATOR_AGREES, name="good")
    cfg.allow_repo_exec = True

    failing = run_probe(cfg, tmp_path / "projects" / PID,
                        _qualified_spec(bad, bad_commit, cfg, "2,900"))
    passing = run_probe(cfg, tmp_path / "projects" / PID / "second",
                        _qualified_spec(good, good_commit, cfg, "2,900"))

    outcomes = [probe_stage.outcome_for("A", failing, "AUTHOR_CODE_REPRODUCTION",
                                        "AUTHOR_CODE_EXECUTION"),
                probe_stage.outcome_for("B", passing, "AUTHOR_CODE_REPRODUCTION",
                                        "AUTHOR_CODE_EXECUTION"),
                TargetOutcome(target_id="C", disposition="ENVIRONMENT_BLOCKED",
                              reason="the environment could not be built")]
    objects = [DiscoveredObject(target_id=t, centrality="CENTRAL", harness_addressable=True,
                                # A is the abstract's own claim; the paper-level failure
                                # has to rest on something the paper's argument rests on.
                                materiality_basis="ABSTRACT_CLAIM" if t == "A" else "NONE",
                                claim_text=f"claim {t}") for t in ("A", "B", "C")]
    ts = TargetSet(paper_id=PID, objects=objects, outcomes=outcomes)

    assert {o.disposition for o in outcomes} == {
        "FAILED_REPRODUCTION", "REPRODUCED", "ENVIRONMENT_BLOCKED"}
    level, _ = triage([], outcomes=outcomes, objects=objects)
    assert level == "RED", "an admissible failure on any target is a failure established"

    report = EvalReport(paper_id=PID, title="A Benchmark Paper", verdict="RED", triage=level,
                        targets_summary={"FAILED_REPRODUCTION": 1, "REPRODUCED": 1,
                                         "ENVIRONMENT_BLOCKED": 1})
    text = render_reviewer_report(report, ts)
    assert "Failed reproduction — A" in text
    assert "**B**" in text and "reproduced" in text
    assert "1 environment blocked" in text

    led = ledger.build(report, ts)
    blocked = next(e for e in led.entries if e.target_id == "C")
    assert "fact about this machine" in blocked.implication


# --------------------------------------------------------------------------- #
# §20D — refusal when the paper does not say enough
# --------------------------------------------------------------------------- #
def test_an_underspecified_paper_yields_yellow_and_never_red(cfg: Config, tmp_path):
    """The system must refuse to invent the missing half, and must not punish the paper
    for the refusal. Invariants 6 and 15."""
    _plant(cfg, _doc("", specified=False))
    _lenses(cfg)
    case = controller.drive(cfg, controller.open_case(cfg, PID))
    assert case.status == "complete", case.blocked_reason

    ts = discover_stage.load(cfg, PID)
    assert not any(p.requires_execution for p in ts.plans), (
        "no artifact and no specification: nothing may be executed")
    blocked = [o for o in ts.outcomes if o.disposition in
               ("SPECIFICATION_BLOCKED", "ARTIFACT_BLOCKED", "NOT_ATTEMPTED")]
    assert blocked, "the refusal must be recorded, not silent"

    report = EvalReport(**state.read_json(
        cfg.projects_dir / PID / "reports" / f"{PID}.json"))
    assert report.verdict == "GREEN" and report.triage in ("GREEN", "YELLOW")
    assert report.triage != "RED"
    assert report.reproduction_status in ("NOT_ATTEMPTED", "NOT_VERIFIED")


def test_a_refusal_is_reported_as_a_limit_of_this_review_not_a_defect(cfg: Config, tmp_path):
    _plant(cfg, _doc("", specified=False))
    _lenses(cfg)
    controller.drive(cfg, controller.open_case(cfg, PID))
    review = (cfg.projects_dir / PID / "reports" / f"{PID}.review.md").read_text(encoding="utf-8")
    assert "No experiment was run that the review did not consider justified." in review
    assert "never a certificate of correctness" in review


# --------------------------------------------------------------------------- #
# The two layers stay two layers
# --------------------------------------------------------------------------- #
def test_the_reviewer_report_is_far_shorter_than_the_machine_trace(cfg: Config, tmp_path):
    repo, _ = _repo(tmp_path, GENERATOR)
    _plant(cfg, _doc(repo.as_uri()))
    _lenses(cfg)
    controller.drive(cfg, controller.open_case(cfg, PID), skip_probe=True)

    reports = cfg.projects_dir / PID / "reports"
    review = (reports / f"{PID}.review.md").read_text(encoding="utf-8")
    machine = (reports / f"{PID}.md").read_text(encoding="utf-8")
    trace = (reports / f"{PID}.ledger.json").read_text(encoding="utf-8")
    # THE MACHINE TRACE IS BOTH ARTIFACTS. `<pid>.md` is the structured report and
    # `<pid>.ledger.json` is the traceability record; invariant 19 is about the pair. On
    # this fixture the .md alone is degenerately small — four one-line findings on a
    # three-cell table — while a real corpus paper runs 11-21 KB against a 3-6 KB review.
    assert len(review) < len(machine) + len(trace), (
        "the review must summarise the trace, not be it")
    # The bound that actually carries the guarantee, and it holds regardless of how big
    # or small the trace happens to be: a review that grows with the paper is not a
    # one-to-two page review, whatever its ratio to anything else.
    assert len(review) < 9000, f"{len(review)} characters is not a one-to-two page review"

    led = CaseLedger(**state.read_json(reports / f"{PID}.ledger.json"))
    assert led.entries and led.efficiency["targets_discovered"] >= 4
    assert "questions_generated" in led.efficiency


def test_every_target_in_the_ledger_carries_its_own_admissibility(cfg: Config, tmp_path):
    repo, _ = _repo(tmp_path, GENERATOR)
    _plant(cfg, _doc(repo.as_uri()))
    _lenses(cfg)
    controller.drive(cfg, controller.open_case(cfg, PID), skip_probe=True)
    led = CaseLedger(**state.read_json(
        cfg.projects_dir / PID / "reports" / f"{PID}.ledger.json"))
    for e in led.entries:
        assert e.admissibility, e.target_id
        assert e.implication is not None
        # Two kinds of entry: one per TARGET, which carries a disposition, and one per
        # REVIEW QUESTION, which does not because nothing was pursued for it. Both carry
        # the evidence axis, so neither can be read as saying more than it does.
        if e.entry_id.startswith("L"):
            assert e.disposition, e.target_id
        else:
            assert e.question and not e.disposition
        assert e.evidence_state and e.resolution_state


# --------------------------------------------------------------------------- #
# §20G — mutation-style attacks on the evidence chain
# --------------------------------------------------------------------------- #
def test_a_wrong_commit_blocks_the_run_rather_than_convicting(cfg: Config, tmp_path):
    repo, _ = _repo(tmp_path, GENERATOR)
    cfg.allow_repo_exec = True
    spec = _qualified_spec(repo, "0" * 40, cfg, "2,900")
    result = run_probe(cfg, tmp_path / "projects" / PID, spec)
    assert result.verdict == "blocked"
    rec = result.reconciliation
    assert rec.status == "INCONCLUSIVE"
    assert triage([], outcomes=[probe_stage.outcome_for("A", result, "", "")])[0] != "RED"


def test_a_fabricated_prose_reference_produces_no_target(cfg: Config, tmp_path):
    from harness import claims, discovery
    from harness.artifacts import Finding
    doc = _doc("")
    forged = Finding(finding_id="x", lens="overclaim", evidence_ref="P0:0-9999",
                     evidence_quote="a total the paper never printed")
    assert not claims.resolve(doc, forged.evidence_ref, forged.evidence_quote).resolved
    objs, _ = discovery.discover(doc, [forged])
    ghost = next(o for o in objs if o.claim_text.startswith("a total") or not o.harness_addressable)
    assert not ghost.harness_addressable


def test_a_malicious_severity_cannot_reach_red_without_a_confirmed_finding(cfg: Config):
    """A lens asserting FATAL on an OPEN_QUESTION is capped by `grading.CANDIDATE_CAP`
    before the verdict ever sees it."""
    from harness import grading
    _cls, counted, _cap, _why = grading.derive(
        lens_severity="FATAL", verification_state="complete", calc_class="",
        graded=False, candidate_class="OPEN_QUESTION", evidence_class="cell_verified",
        confidence="HIGH", lens_confidence="HIGH")
    assert grading.RANK[counted] < grading.RANK["FATAL"]


def test_an_unauthorized_experiment_produces_no_measurement(cfg: Config, tmp_path):
    repo, commit = _repo(tmp_path, GENERATOR)
    cfg.allow_repo_exec = False                      # the gate is shut
    result = run_probe(cfg, tmp_path / "projects" / PID,
                       _qualified_spec(repo, commit, cfg, "2,900"))
    assert result.verdict == "blocked" and not result.seeds_run
    rec = result.reconciliation
    assert rec.status == "INCONCLUSIVE" and rec.reached_experiment is False
    outcome = probe_stage.outcome_for("A", result, "AUTHOR_CODE_REPRODUCTION",
                                      "AUTHOR_CODE_EXECUTION")
    assert outcome.disposition == "AUTHORIZATION_BLOCKED"
    assert not outcome.establishes_failure


def test_a_stale_target_state_is_not_carried_into_a_new_run(cfg: Config, tmp_path):
    """`written_by == 'harness'` marks this stage's own previous output, so a target the
    audit has since moved past is never mistaken for an instruction to keep running it.

    Since Task 4 this is refused even more directly: the write below carries no
    `accept_spec` seal, so `spec_is_accepted` refuses it and `build_spec` never reads it
    at all, regardless of `written_by` — a strictly stronger guarantee than the one this
    test was originally written to pin down."""
    repo, _ = _repo(tmp_path, GENERATOR)
    doc = _doc(repo.as_uri())
    _plant(cfg, doc)
    _lenses(cfg)
    controller.drive(cfg, controller.open_case(cfg, PID), skip_probe=True)

    stale = ProbeSpec(paper_id=PID, written_by="harness", finding_id="gone-01",
                      table_ref="T9:r9:c9", claimed_cell_value="999")
    state.write_json(state.control_dir(cfg.projects_dir / PID) / "spec.json", stale.model_dump())
    rebuilt = probe_stage.build_spec(cfg, PID, doc)
    assert rebuilt.finding_id != "gone-01" and rebuilt.table_ref != "T9:r9:c9"


def test_multiple_json_metrics_cannot_select_the_wrong_one(cfg: Config, tmp_path):
    """A repository that prints two different counts under the bound key is ambiguous,
    and an ambiguous metric may not be reconciled against anything."""
    body = GENERATOR.replace(
        'print(json.dumps({"n_cases": working * 5 * 10}), flush=True)',
        'print(json.dumps({"n_cases": 2900}), flush=True)\n'
        'print(json.dumps({"n_cases": 12}), flush=True)')
    repo, commit = _repo(tmp_path, body, name="ambiguous")
    cfg.allow_repo_exec = True
    result = run_probe(cfg, tmp_path / "projects" / PID,
                       _qualified_spec(repo, commit, cfg, "2,900"))
    rec = result.reconciliation
    assert rec.status != "RESOLVED_VERIFIED", "positional coincidence must not verify"
    assert rec.status != "FAILED_REPRODUCTION", "nor convict"


def test_a_second_pass_does_not_lose_the_first_executions_outcome(confined_local, cfg: Config, tmp_path):
    """`discover` recomputes `targets.json` from scratch on every controller pass —
    correctly, since a new grade or a new lens finding can change what is checkable — but
    it runs BEFORE `probe` and knows nothing about a prior execution. `probe`'s own cache
    guard (`done and not force_probe` in `_phase_probe`) then skips `probe_stage.run`
    entirely on a second pass, which used to mean the freshly discovered target set never
    got the executed target's `TargetOutcome` reattached: `runs/<pid>/probe_results.json`
    still held a valid record, but `discovery/targets.json` reported it as never launched,
    and so did every count that reads it (`CaseLedger.efficiency`, `evaluation`'s funnel).

    Found by re-running the real eight-paper corpus a second time (to pick up independent
    grading) and seeing the funnel's `launched` term read 0 across the whole corpus despite
    `runs/<pid>/probe_results.json` showing real executions on seven of the eight papers.
    """
    repo, _ = _repo(tmp_path, GENERATOR)
    _plant(cfg, _doc(repo.as_uri()))
    # Anchored on the PROSE COMPOSITION, which is the claim this repository's advertised
    # command can actually be bound to. Anchoring on the table cell makes that cell the
    # top-priority target, and its row names a third-party baseline the checkout does not
    # implement — a correct `no_candidate`, and nothing to resync.
    _lenses(cfg, verifiable=True, quote=COMPOSITION, ref="p1")
    # A REAL execution, not a diagnostic. This used to pass on the synthesized probe,
    # which ran whenever identity failed — and since `stages.probe.admissible_if_it_
    # succeeds` a process starts only when its result could speak, so the fixture now has
    # to bind the authors' own code for there to be anything to resync at all. That is a
    # better test of the same property: the bookkeeping bug it pins was about losing a
    # real launched outcome.
    cfg.allow_repo_exec = True
    cfg.allow_install = True

    case = controller.drive(cfg, controller.open_case(cfg, PID))
    assert case.status == "complete", case.blocked_reason
    ts_first = discover_stage.load(cfg, PID)
    launched_first = sum(o.launched for o in ts_first.outcomes)
    assert launched_first > 0, "the fixture must actually execute the authors' own code"

    # A second `review` invocation over the same paper — `drive` on an already-complete
    # case rewinds to `collect`, so `discover` runs fresh again exactly as it does on a
    # real second pass, while `probe` finds its cache and must not let that recomputation
    # erase what already ran.
    case2 = controller.drive(cfg, controller.open_case(cfg, PID))
    assert case2.status == "complete", case2.blocked_reason
    ts_second = discover_stage.load(cfg, PID)
    launched_second = sum(o.launched for o in ts_second.outcomes)
    assert launched_second == launched_first, (
        "a second pass over an already-executed paper must not drop the launched outcome")
