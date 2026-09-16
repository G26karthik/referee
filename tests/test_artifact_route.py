"""F — the static artifact route: what the released code can establish, and what it cannot.

Seventeen properties, each named in the brief that asked for this route, each asserted
against a REAL git checkout rather than a mock: a fact about a pinned tree that was never
pinned would be the exact defect this whole route exists to avoid.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness import artifact_evidence, artifact_review_driver, planner
from harness.artifacts import (ClaimRef, DiscoveredObject, PaperDoc, PlanDecision, Section,
                               TargetOutcome, TargetSet)
from harness.config import Config
from harness.stages import artifact as artifact_stage

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A small, real, committed repository. Skipped where git is unavailable."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config.yaml").write_text("batch_size: 32\nepochs: 100\n", encoding="utf-8")
    (root / "train.py").write_text(
        "import argparse\n"
        "\n"
        "def build():\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--batch-size', type=int, default=32)\n"
        "    return p\n", encoding="utf-8")
    (root / "requirements.txt").write_text("torch==2.1.0\n", encoding="utf-8")
    try:
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@e")
        _git(root, "config", "user.name", "t")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "initial")
    except (OSError, subprocess.CalledProcessError):     # pragma: no cover
        pytest.skip("git is not available on this host")
    return root


def _doc() -> PaperDoc:
    return PaperDoc(paper_id="p", title="A Paper", n_pages=2, sections=[
        Section(section_idx=0, title="Method", page_start=1,
                text="We train every model with a batch size of 128 for 100 epochs. "
                     "The released code includes requirements.txt for the environment."),
        Section(section_idx=1, title="Results", page_start=2,
                text="Our method reaches 91.4 accuracy."),
    ])


# --------------------------------------------------------------------------- #
# 1-3. The snapshot: a fact is tied to a pinned tree or it is not a fact
# --------------------------------------------------------------------------- #
def test_a_static_fact_resolves_to_an_exact_pinned_file_and_span(checkout):
    snap = artifact_evidence.snapshot(checkout, "https://example.invalid/r")
    assert snap.audited and len(snap.commit) == 40 and snap.tree_sha

    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    assert span is not None
    assert span.file == "config.yaml" and span.line == 1
    assert span.quote == "batch_size: 32"
    assert len(span.file_sha256) == 64
    # The span is re-derivable by hand: open the file at that SHA and read those bytes.
    text = (checkout / "config.yaml").read_text(encoding="utf-8")
    assert span.char_span is not None
    assert text[span.char_span[0]:span.char_span[1]] == span.quote


def test_a_changed_commit_invalidates_artifact_evidence_identity(checkout):
    before = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")

    (checkout / "config.yaml").write_text("batch_size: 64\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "second")
    after = artifact_evidence.snapshot(checkout)

    assert after.audited and after.commit != before.commit
    assert not artifact_evidence.same_snapshot(before, after)
    # The citation no longer holds against the new tree, and says so by re-reading.
    assert not artifact_evidence.span_still_holds(checkout, span)


def test_a_dirty_checkout_cannot_masquerade_as_the_audited_checkout(checkout):
    clean = artifact_evidence.snapshot(checkout)
    assert clean.audited

    (checkout / "config.yaml").write_text("batch_size: 999\n", encoding="utf-8")
    dirty = artifact_evidence.snapshot(checkout)
    assert dirty.dirty and not dirty.audited
    assert dirty.commit == clean.commit, "the SHA is unchanged; the TREE is not"
    assert "differs from HEAD" in dirty.note

    # And nothing established against it carries authority.
    fact = artifact_evidence.file_fact(checkout, dirty, "train.py")
    assert fact.authority == "NONE"


# --------------------------------------------------------------------------- #
# 4-5. Absence: a file the paper names, and one nobody claimed
# --------------------------------------------------------------------------- #
def test_a_missing_file_is_established_when_the_paper_explicitly_names_it(checkout):
    snap = artifact_evidence.snapshot(checkout)
    quote = "The released code includes requirements.txt for the environment."
    fact = artifact_evidence.file_fact(checkout, snap, "environment.yml",
                                       named_by_paper=quote)
    assert fact.authority == "ARTIFACT_FACT"
    assert "is not in the checkout" in fact.statement
    # STILL NOT A STATEMENT ABOUT THE PAPER. Level 2 needs an addressed statement and a
    # bound experiment identity, neither of which a missing file supplies.
    assert fact.about_the_paper is False


def test_an_arbitrary_absent_file_is_not_a_paper_defect(checkout):
    snap = artifact_evidence.snapshot(checkout)
    fact = artifact_evidence.file_fact(checkout, snap, "run_everything.sh")
    assert fact.authority == "NONE"
    assert "nobody claimed" in fact.statement
    assert fact.about_the_paper is False


# --------------------------------------------------------------------------- #
# 6-7. Level 2 needs experiment identity, and says so when it does not have one
# --------------------------------------------------------------------------- #
def test_a_config_mismatch_establishes_a_paper_mismatch_only_when_identity_binds(checkout):
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = artifact_evidence.bind_mismatch(
        _doc(), snap, span=span,
        paper_quote="We train every model with a batch size of 128 for 100 epochs.",
        paper_value="128", artifact_value="32",
        experiment_id="the only training entrypoint, train.py, reads this file")
    assert fact.authority == "PAPER_ARTIFACT_MISMATCH"
    assert fact.about_the_paper and fact.paper_ref.startswith("P0:")
    assert fact.refusal == ""
    # AND IT STILL DOES NOT SAY THE RESULT IS WRONG.
    assert "does NOT establish that the reported result is wrong" in fact.statement


def test_a_config_mismatch_with_ambiguous_experiment_identity_stays_unresolved(checkout):
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = artifact_evidence.bind_mismatch(
        _doc(), snap, span=span,
        paper_quote="We train every model with a batch size of 128 for 100 epochs.",
        paper_value="128", artifact_value="32", experiment_id="")
    assert fact.authority == "NONE"
    assert fact.refusal == "experiment_identity_unbound"
    assert fact.about_the_paper is False
    assert "contradicts\nnothing" in fact.statement or "contradicts" in fact.statement


def test_two_values_that_agree_are_a_result_and_not_a_mismatch(checkout):
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "epochs: 100")
    fact = artifact_evidence.bind_mismatch(
        _doc(), snap, span=span,
        paper_quote="We train every model with a batch size of 128 for 100 epochs.",
        paper_value="100", artifact_value="100", experiment_id="train.py")
    assert fact.authority == "ARTIFACT_FACT" and fact.refusal == "no_disagreement"
    assert "they agree" in fact.statement


# --------------------------------------------------------------------------- #
# 8. An AST warning is not a scientific failure, and there is no way to say it is
# --------------------------------------------------------------------------- #
def test_an_ast_warning_alone_cannot_establish_scientific_failure():
    from harness.artifacts import ARTIFACT_AUTHORITY
    assert set(ARTIFACT_AUTHORITY) == {"ARTIFACT_FACT", "PAPER_ARTIFACT_MISMATCH", "NONE"}
    # There is no third level to write, which is the encoding of the rule.
    assert not any("FAIL" in a or "FALSE" in a or "SCIENTIF" in a
                   for a in ARTIFACT_AUTHORITY)

    # And the rule audit keeps the measured-false detectors out of reviewer output.
    assert artifact_evidence.rule_authority("leak-model-selection-on-test") == "D"
    assert not artifact_evidence.reviewer_visible("leak-model-selection-on-test")
    assert not artifact_evidence.reviewer_visible("cripple-augmentation-one-arm")
    assert artifact_evidence.reviewer_visible("leak-unseeded-split")
    # An unaudited rule is not visible by default: the audit licenses a rule, not its
    # existence.
    assert not artifact_evidence.reviewer_visible("some-rule-added-later")


# --------------------------------------------------------------------------- #
# 9-10. The code auditor proposes; the harness relocates
# --------------------------------------------------------------------------- #
def _payload(rows, notes="n") -> str:
    return json.dumps({"concerns": rows, "notes": notes})


def test_a_code_auditor_proposal_with_an_invalid_source_span_is_dropped(checkout, tmp_path):
    cfg = Config(projects_dir=tmp_path / "projects")
    from harness import state
    state.create_project(cfg, "", "T", pid="p")
    got = artifact_review_driver.accept(cfg, "p", _doc(), checkout, _payload([
        {"kind": "HARD_CODED_RESULT", "title": "t", "statement": "s",
         "file": "config.yaml", "code_quote": "accuracy = 0.914  # hard-coded"},
        {"kind": "PAPER_CODE_MISMATCH", "title": "t", "statement": "s",
         "file": "nowhere.py", "code_quote": "batch_size: 32"},
    ]), statements=["We train every model with a batch size of 128 for 100 epochs."])
    assert got.proposed == 2 and got.relocated == 0
    assert got.facts == [], "a citation that does not relocate is dropped whole"


def test_a_valid_code_quotation_survives_deterministic_relocation(checkout, tmp_path):
    cfg = Config(projects_dir=tmp_path / "projects")
    from harness import state
    state.create_project(cfg, "", "T", pid="p")
    got = artifact_review_driver.accept(cfg, "p", _doc(), checkout, _payload([
        {"kind": "PAPER_CODE_MISMATCH", "title": "batch size", "statement": "s",
         "file": "train.py",
         "code_quote": "p.add_argument('--batch-size', type=int, default=32)",
         "paper_quote": "We train every model with a batch size of 128 for 100 epochs.",
         "paper_value": "128", "artifact_value": "32",
         "experiment_id": "the only parser in the only entrypoint",
         "counter_explanations": ["a launcher may override the default"]},
    ]), statements=["We train every model with a batch size of 128 for 100 epochs."])
    assert got.proposed == 1 and got.relocated == 1
    fact = got.facts[0]
    assert fact.authority == "PAPER_ARTIFACT_MISMATCH"
    assert fact.span is not None and fact.span.file == "train.py" and fact.span.line == 5
    assert fact.counter_explanations, "the innocent reading is carried, never dropped"


def test_a_reader_cannot_award_its_own_proposal_any_authority():
    proposals, _notes, meta = artifact_review_driver.parse_concerns(_payload([
        {"kind": "PAPER_CODE_MISMATCH", "file": "a.py", "code_quote": "x = 1",
         "authority": "PAPER_ARTIFACT_MISMATCH", "refusal": "", "paper_ref": "P0:0-9",
         "span": {"file": "a.py"}},
    ]))
    assert len(proposals) == 1
    for owned in artifact_review_driver.HARNESS_OWNED_FACT_KEYS:
        assert owned not in proposals[0], owned
    assert meta["harness_keys_stripped"] == 4, meta


# --------------------------------------------------------------------------- #
# 11-13. The state machine: the two states that had no way in, and the one bound
# --------------------------------------------------------------------------- #
def _target(tid: str, kind: str, claim: str = "the repository implements the method",
            centrality: str = "CENTRAL"):
    return DiscoveredObject(
        target_id=tid, question_kind=kind, claim_text=claim, centrality=centrality,
        ref=ClaimRef(ref="P0:0-20", kind="prose_claim", resolution="resolved"),
        routes=["ARTIFACT_INSPECTION"], harness_addressable=True)


def _plan(tid: str) -> PlanDecision:
    return PlanDecision(target_id=tid, action="ARTIFACT_INSPECTION_ONLY",
                        route="ARTIFACT_INSPECTION", requires_execution=False)


def test_artifact_evidence_is_reachable_and_so_is_resolved_from_artifact(checkout, tmp_path):
    from harness import state
    cfg = Config(projects_dir=tmp_path / "projects")
    state.create_project(cfg, "", "T", pid="p")
    ts = TargetSet(paper_id="p", objects=[_target("t1", "SPECIFICATION")],
                   plans=[_plan("t1")])
    outcomes, whole = artifact_stage.run_route(cfg, "p", _doc(), ts, checkout,
                                               url="https://example.invalid/r")
    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.disposition == "ARTIFACT_RESOLVED"
    assert out.evidence_state == "ARTIFACT_EVIDENCE"
    assert out.resolution_state == "RESOLVED_FROM_ARTIFACT"
    assert whole is not None and whole.discharged


def test_artifact_inspection_cannot_resolve_a_question_that_requires_execution(
        checkout, tmp_path):
    from harness import state
    cfg = Config(projects_dir=tmp_path / "projects")
    state.create_project(cfg, "", "T", pid="p")
    ts = TargetSet(paper_id="p", objects=[_target("t1", "PRINTED_QUANTITY")],
                   plans=[_plan("t1")])
    outcomes, _whole = artifact_stage.run_route(cfg, "p", _doc(), ts, checkout)
    assert outcomes[0].disposition == "COMPARISON_BLOCKED"
    assert outcomes[0].evidence_state != "ARTIFACT_EVIDENCE"
    assert "whose answer is a measured result" in outcomes[0].reason
    for kind in ("REPRODUCTION", "PRINTED_QUANTITY", "COMPOSITION", "ATTRIBUTION"):
        assert artifact_evidence.requires_execution(kind), kind


def test_cloning_a_repository_is_not_artifact_evidence(checkout, tmp_path):
    """A route that obtained a checkout and was asked nothing discharges nothing."""
    from harness import state
    cfg = Config(projects_dir=tmp_path / "projects")
    state.create_project(cfg, "", "T", pid="p")
    snap = artifact_evidence.snapshot(checkout)
    nothing_asked = artifact_evidence.inspect(
        _doc(), checkout, facts=[artifact_evidence.file_fact(checkout, snap, "train.py")])
    assert not nothing_asked.discharged
    assert "obtaining an artifact is not evidence" in nothing_asked.reason
    assert artifact_evidence.outcome_disposition(nothing_asked) == "COMPARISON_BLOCKED"


# --------------------------------------------------------------------------- #
# 14-16. What an artifact finding may and may not set in motion
# --------------------------------------------------------------------------- #
def test_static_inspection_never_suppresses_an_execution():
    """The route is reachable ONLY where no executable route applies."""
    runnable = _target("t1", "PRINTED_QUANTITY")
    runnable.routes = ["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"]
    runnable.expected_value = 91.4
    decided = planner.plan(runnable, artifact_available=True)
    assert decided.action != "ARTIFACT_INSPECTION_ONLY"
    assert decided.route == "AUTHOR_CODE_EXECUTION"
    assert decided.requires_execution

    # And with the execution route removed, the same object DOES reach inspection —
    # so the reason above is the presence of an executable route, not an accident.
    inspect_only = _target("t2", "SPECIFICATION")
    assert planner.plan(inspect_only, artifact_available=True).action \
        == "ARTIFACT_INSPECTION_ONLY"
    # ...and never when the authors published nothing to read.
    assert planner.plan(inspect_only, artifact_available=False).action \
        != "ARTIFACT_INSPECTION_ONLY"


def test_an_artifact_finding_may_trigger_execution_and_focused_validation():
    """An artifact concern leaves every executable route available to the same object."""
    obj = _target("t1", "ATTRIBUTION")
    obj.routes = ["ARTIFACT_INSPECTION", "FOCUSED_VALIDATION_EXPERIMENT"]
    decided = planner.plan(obj, artifact_available=True, specification_complete=True)
    assert decided.route == "FOCUSED_VALIDATION_EXPERIMENT"
    assert decided.requires_execution

    obj2 = _target("t2", "PRINTED_QUANTITY")
    obj2.routes = ["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"]
    assert planner.plan(obj2, artifact_available=True).requires_execution


def test_no_artifact_observation_can_bypass_materiality():
    """ARTIFACT_RESOLVED is not a material failure, whatever the inspection found."""
    out = TargetOutcome(target_id="t1", disposition="ARTIFACT_RESOLVED",
                        provenance="artifact")
    assert out.establishes_failure is False
    # Not even with a provenance the reproduction ceiling admits: the ceiling governs
    # EXECUTION, and this is not execution evidence.
    assert TargetOutcome(target_id="t1", disposition="ARTIFACT_RESOLVED",
                         provenance="repo_exec").establishes_failure is False

    from harness import materiality
    assert materiality.material_target_failure(
        [_target("t1", "SPECIFICATION")], [out]) is None


# --------------------------------------------------------------------------- #
# 17. Mutation after execution retracts the execution, not the history
# --------------------------------------------------------------------------- #
def test_mutation_after_execution_does_not_rewrite_the_pre_run_inspection(checkout):
    pre = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = artifact_evidence.bind_mismatch(
        _doc(), pre, span=span,
        paper_quote="We train every model with a batch size of 128 for 100 epochs.",
        paper_value="128", artifact_value="32", experiment_id="train.py")
    assert fact.authority == "PAPER_ARTIFACT_MISMATCH"

    # An execution mutates the working tree, as a training run writing checkpoints does.
    (checkout / "config.yaml").write_text("batch_size: 8\n", encoding="utf-8")
    (checkout / "results.json").write_text("{}", encoding="utf-8")
    post = artifact_evidence.snapshot(checkout)

    # The EXECUTION's identity is gone: the tree is no longer the audited one.
    assert not post.audited and post.dirty
    assert not artifact_evidence.same_snapshot(pre, post)

    # THE PRE-RUN FACT IS UNTOUCHED. It is tied to its own snapshot, which still names
    # the tree it was established against, and its statement still says what it said.
    assert fact.snapshot is not None and fact.snapshot.audited
    assert fact.snapshot.tree_sha == pre.tree_sha
    assert fact.authority == "PAPER_ARTIFACT_MISMATCH"
    # And a NEW fact against the mutated tree carries nothing, so the two cannot be
    # confused for each other.
    assert artifact_evidence.file_fact(checkout, post, "train.py").authority == "NONE"
    assert not artifact_evidence.span_still_holds(checkout, span)


# --------------------------------------------------------------------------- #
# 18. The rule audit is enforced at the renderer, not only recorded
# --------------------------------------------------------------------------- #
def test_the_review_does_not_print_a_detector_the_audit_classified_unsafe():
    """Four of the corpus's six hits are false. A referee is not asked to investigate them."""
    from harness.artifacts import CodeAudit, CodeAuditFinding
    from harness.stages import report as report_stage

    audit = CodeAudit(repo_path="r", files_scanned=165, lines_scanned=40_000, findings=[
        CodeAuditFinding(
            finding_id="code-01", rule_id="leak-model-selection-on-test",
            category="data_leakage", severity="MAJOR", title="selection on test",
            statement="s", file="run_pruning.py", line=157,
            code_quote="rescaled_eval_metrics = test(model, eval_dataloader, head_mask)"),
        CodeAuditFinding(
            finding_id="code-02", rule_id="leak-unseeded-split", category="data_leakage",
            severity="MINOR", title="unseeded split", statement="s",
            file="utils/utils.py", line=600,
            code_quote="train_dataset, eval_dataset = torch.utils.data.random_split("),
    ])
    rendered = "\n".join(report_stage._code_audit_block(audit))
    assert "utils/utils.py" in rendered, "the class-A fact is reported"
    assert "run_pruning.py" not in rendered, "the measured-false detector is not"
    assert "audited as unsafe for reviewer output" in rendered
    # AND THE MACHINE TRACE IS UNTOUCHED: suppression is a rendering decision, so both
    # hits are still on the artifact a reader can trace the review through.
    assert len(audit.findings) == 2
