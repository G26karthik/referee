"""F — the static artifact route: what the released code can establish, and what it cannot.

Every test runs against a REAL git checkout rather than a mock: a fact about a pinned tree
that was never pinned would be the exact defect this route exists to avoid.

The authority ladder these assert, and the distance between its rungs:

    ARTIFACT_FACT                        a bounded fact about the checkout
    ENDPOINTS_VERIFIED_ARTIFACT_CONCERN  both locations real, the relation model-proposed
    PAPER_ARTIFACT_MISMATCH              the two disagree, experiment identity ESTABLISHED
    (no value)                           the reported scientific result is false
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness import artifact_evidence, artifact_review_driver, planner, taxonomy
from harness.artifacts import (ArtifactFact, ArtifactInspection, ClaimRef,
                               DiscoveredObject, PaperDoc, PlanDecision, Section,
                               SourceSpan, TargetOutcome, TargetSet)
from harness.config import Config
from harness.stages import artifact as artifact_stage

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_PAPER_BATCH = "We train every model with a batch size of 128 for 100 epochs."
_README = "Table 2 of the paper is produced by `python train.py --config config.yaml`."


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
    (root / "README.md").write_text(_README + "\n", encoding="utf-8")
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
                text=_PAPER_BATCH + " The released code includes requirements.txt for "
                                    "the environment."),
        Section(section_idx=1, title="Results", page_start=2,
                text="Our method reaches 91.4 accuracy."),
    ])


def _bind(checkout, snap, span, **kw):
    """`bind_mismatch` with the fixture's deterministic README identity, unless overridden."""
    args = dict(paper_quote=_PAPER_BATCH, paper_value="128", artifact_value="32",
                experiment_id="Table 2", root=checkout,
                identity_basis="readme_maps_the_experiment", identity_file="README.md",
                identity_quote="Table 2 of the paper is produced by")
    args.update(kw)
    return artifact_evidence.bind_mismatch(_doc(), snap, span=span, **args)


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
    assert not artifact_evidence.span_still_holds(checkout, span)


def test_a_dirty_checkout_cannot_masquerade_as_the_audited_checkout(checkout):
    clean = artifact_evidence.snapshot(checkout)
    assert clean.audited

    (checkout / "config.yaml").write_text("batch_size: 999\n", encoding="utf-8")
    dirty = artifact_evidence.snapshot(checkout)
    assert dirty.dirty and not dirty.audited
    assert dirty.commit == clean.commit, "the SHA is unchanged; the TREE is not"
    assert "differs from HEAD" in dirty.note
    assert artifact_evidence.file_fact(checkout, dirty, "train.py").authority == "NONE"


# --------------------------------------------------------------------------- #
# 4-5. Absence: a file the paper names, and one nobody claimed
# --------------------------------------------------------------------------- #
def test_a_missing_file_is_established_when_the_paper_explicitly_names_it(checkout):
    snap = artifact_evidence.snapshot(checkout)
    quote = "The released code includes requirements.txt for the environment."
    fact = artifact_evidence.file_fact(checkout, snap, "environment.yml",
                                       named_by_paper=quote)
    assert fact.authority == "ARTIFACT_FACT"
    assert fact.settles == "FILE_PRESENCE"
    assert "is not in the checkout" in fact.statement
    assert fact.about_the_paper is False


def test_an_arbitrary_absent_file_is_not_a_paper_defect(checkout):
    snap = artifact_evidence.snapshot(checkout)
    fact = artifact_evidence.file_fact(checkout, snap, "run_everything.sh")
    assert fact.authority == "NONE" and fact.settles == ""
    assert "nobody claimed" in fact.statement


# --------------------------------------------------------------------------- #
# 6-11. Experiment identity, classified rather than believed
# --------------------------------------------------------------------------- #
def test_a_config_mismatch_establishes_a_paper_mismatch_only_when_identity_is_deterministic(
        checkout):
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = _bind(checkout, snap, span)
    assert fact.authority == "PAPER_ARTIFACT_MISMATCH"
    assert fact.identity_state == "ESTABLISHED"
    assert fact.identity_basis == "readme_maps_the_experiment"
    assert fact.identity_span is not None and fact.identity_span.file == "README.md"
    assert fact.about_the_paper and fact.paper_ref.startswith("P0:")
    assert "does NOT establish that the reported result is wrong" in fact.statement


def test_the_auditors_reading_is_not_an_experiment_identity(checkout):
    """A model saying "this looks like the right config" is a guess with a citation on it."""
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = _bind(checkout, snap, span, identity_basis="auditor_assertion",
                 identity_file="", identity_quote="")
    assert fact.identity_state == "AMBIGUOUS"
    assert fact.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"
    assert fact.refusal == "experiment_identity_not_deterministic"
    assert fact.about_the_paper is False and fact.endpoints_only
    # BOTH ENDS ARE STILL REAL, and the sentence says so — this is a referee's question.
    assert "BOTH LOCATIONS" in fact.statement and "ARE VERIFIED" in fact.statement


def test_a_deterministic_basis_that_does_not_relocate_is_partial(checkout):
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = _bind(checkout, snap, span, identity_basis="script_passes_the_config",
                 identity_file="scripts/run_table2.sh",
                 identity_quote="python train.py --config config.yaml")
    assert fact.identity_state == "PARTIAL"
    assert fact.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"


def test_a_config_mismatch_with_no_identity_at_all_stays_unresolved(checkout):
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = _bind(checkout, snap, span, experiment_id="", identity_basis="",
                 identity_file="", identity_quote="")
    assert fact.identity_state == "UNBOUND"
    assert fact.authority == "NONE"
    assert fact.refusal == "experiment_identity_unbound"
    assert "contradicts nothing" in fact.statement


def test_two_values_that_agree_are_a_result_and_not_a_mismatch(checkout):
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "epochs: 100")
    fact = _bind(checkout, snap, span, paper_value="100", artifact_value="100")
    assert fact.authority == "ARTIFACT_FACT" and fact.refusal == "no_disagreement"
    assert "they agree" in fact.statement


def test_values_of_different_kinds_disagree_about_nothing(checkout):
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = _bind(checkout, snap, span, paper_value="AdamW")
    assert fact.refusal == "values_not_comparable"
    assert fact.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"


# --------------------------------------------------------------------------- #
# 12-13. An AST warning is not a scientific failure; class B is not review evidence
# --------------------------------------------------------------------------- #
def test_an_ast_warning_alone_cannot_establish_scientific_failure():
    from harness.artifacts import ARTIFACT_AUTHORITY
    assert ARTIFACT_AUTHORITY == ("ARTIFACT_FACT", "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN",
                                  "PAPER_ARTIFACT_MISMATCH", "NONE")
    assert not any("FAIL" in a or "FALSE" in a or "SCIENTIF" in a
                   for a in ARTIFACT_AUTHORITY)


def test_only_class_a_rules_are_reviewer_visible():
    """That a source pattern matched is deterministic; what it MEANS is not."""
    assert artifact_evidence.REVIEWER_VISIBLE == ("A",)

    assert artifact_evidence.rule_authority("leak-unseeded-split") == "A"
    assert artifact_evidence.reviewer_visible("leak-unseeded-split")

    # An unaudited rule (the nine deleted ones, or any rule added later) defaults to D:
    # the audit licenses a rule, not its existence.
    for unaudited in ("some-rule-added-later", "leak-model-selection-on-test",
                      "cripple-augmentation-one-arm"):
        assert artifact_evidence.rule_authority(unaudited) == "D", unaudited
        assert not artifact_evidence.reviewer_visible(unaudited), unaudited


# --------------------------------------------------------------------------- #
# 14-17. The code auditor proposes; the harness relocates and re-reads
# --------------------------------------------------------------------------- #
def _payload(rows, notes="n") -> str:
    return json.dumps({"concerns": rows, "notes": notes})


def _accept(cfg, checkout, rows):
    from harness import state
    state.create_project(cfg, "", "T", pid="p")
    return artifact_review_driver.accept(
        cfg, "p", _doc(), checkout, _payload(rows), statements=[_PAPER_BATCH])


def test_a_code_auditor_proposal_with_an_invalid_source_span_is_dropped(checkout, tmp_path):
    got = _accept(Config(projects_dir=tmp_path / "projects"), checkout, [
        {"kind": "HARD_CODED_RESULT", "title": "t", "statement": "s",
         "file": "config.yaml", "code_quote": "accuracy = 0.914  # hard-coded"},
        {"kind": "PAPER_CODE_MISMATCH", "title": "t", "statement": "s",
         "file": "nowhere.py", "code_quote": "batch_size: 32"},
    ])
    assert got.proposed == 2 and got.relocated == 0
    assert got.facts == [], "a citation that does not relocate is dropped whole"


def test_a_valid_code_quotation_survives_deterministic_relocation(checkout, tmp_path):
    got = _accept(Config(projects_dir=tmp_path / "projects"), checkout, [{
        "kind": "PAPER_CODE_MISMATCH", "title": "batch size", "statement": "s",
        "file": "train.py",
        "code_quote": "p.add_argument('--batch-size', type=int, default=32)",
        "paper_quote": _PAPER_BATCH, "paper_value": "128", "artifact_value": "32",
        "config_key": "batch-size", "experiment_id": "Table 2",
        "identity_basis": "readme_maps_the_experiment", "identity_file": "README.md",
        "identity_quote": "Table 2 of the paper is produced by",
        "counter_explanations": ["a launcher may override the default"]}])
    assert got.proposed == 1 and got.relocated == 1
    fact = got.facts[0]
    assert fact.authority == "PAPER_ARTIFACT_MISMATCH"
    assert fact.span is not None and fact.span.file == "train.py" and fact.span.line == 5
    assert fact.counter_explanations, "the innocent reading is carried, never dropped"
    # THE VALUE WAS RE-READ FROM THE FILE, not taken from the auditor.
    assert any("re-read from the file" in c for c in fact.counter_explanations)


def test_the_config_value_is_read_from_the_file_and_not_from_the_auditor(checkout, tmp_path):
    """The auditor names a key; what the FILE says is the fact."""
    got = _accept(Config(projects_dir=tmp_path / "projects"), checkout, [{
        "kind": "PAPER_CODE_MISMATCH", "title": "t", "statement": "s",
        "file": "config.yaml", "code_quote": "batch_size: 32",
        "paper_quote": _PAPER_BATCH, "paper_value": "128",
        # The auditor MISREPORTS the value; the harness re-reads 32 off the file.
        "artifact_value": "999", "config_key": "batch_size", "experiment_id": "Table 2",
        "identity_basis": "readme_maps_the_experiment", "identity_file": "README.md",
        "identity_quote": "Table 2 of the paper is produced by"}])
    assert got.facts[0].artifact_value == "32", "the auditor's 999 is not the file's 32"


def test_a_reader_cannot_award_its_own_proposal_any_authority():
    proposals, _notes, meta = artifact_review_driver.parse_concerns(_payload([
        {"kind": "PAPER_CODE_MISMATCH", "file": "a.py", "code_quote": "x = 1",
         "authority": "PAPER_ARTIFACT_MISMATCH", "refusal": "", "paper_ref": "P0:0-9",
         "identity_state": "ESTABLISHED", "span": {"file": "a.py"}},
    ]))
    assert len(proposals) == 1
    for owned in artifact_review_driver.HARNESS_OWNED_FACT_KEYS:
        assert owned not in proposals[0], owned
    assert meta["harness_keys_stripped"] == 5, meta


# --------------------------------------------------------------------------- #
# 18-23. The five terminal states, and the broad question none of them settles
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


def _route(cfg, checkout, objects):
    from harness import state
    state.create_project(cfg, "", "T", pid="p")
    ts = TargetSet(paper_id="p", objects=objects,
                   plans=[_plan(o.target_id) for o in objects])
    return artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")


def test_a_broad_implementation_question_is_not_settled_by_bounded_artifact_facts(
        checkout, tmp_path):
    """THE BUG THIS RELEASE FIXES.

    All four repository papers routed the same target — "the released repository <url>
    implements the described method" — and it was discharged by facts like "the checkout
    advertises evaluate.py". An entrypoint existing is supporting evidence for that
    question and is not its answer.
    """
    outcomes, whole = _route(
        Config(projects_dir=tmp_path / "projects"), checkout,
        [_target("t1", "", claim="The released repository implements the described method.")])
    out = outcomes[0]
    assert out.disposition == "ARTIFACT_INSPECTION_INCONCLUSIVE"
    assert out.evidence_state != "ARTIFACT_EVIDENCE"
    assert out.resolution_state == "UNRESOLVED"
    assert "supporting evidence for it and are not its answer" in out.reason
    assert "semantic correspondence question" in out.reason
    # The bounded questions it DID answer are named, so the inspection is not reported
    # as having produced nothing.
    for scope in ("ENTRYPOINT_PRESENCE", "MANIFEST_PRESENCE", "DEPENDENCY_DECLARED",
                  "FILE_PRESENCE"):
        assert scope in out.reason, scope
    assert whole is not None and whole.facts, "facts were still established"


def test_a_bounded_artifact_question_is_settled_and_is_not_about_the_paper(
        checkout, tmp_path):
    outcomes, _whole = _route(
        Config(projects_dir=tmp_path / "projects"), checkout,
        [_target("t1", "ENTRYPOINT_PRESENCE",
                 claim="is there a runnable entrypoint the repository advertises?")])
    out = outcomes[0]
    assert out.disposition == "ARTIFACT_FACT_ESTABLISHED"
    assert out.evidence_state == "ARTIFACT_PROPERTY_ESTABLISHED"
    assert out.resolution_state == "RESOLVED_FROM_ARTIFACT"
    assert out.evidence_state not in taxonomy.EVIDENCE_ABOUT_THE_PAPER
    assert out.establishes_failure is False


def test_only_a_bound_mismatch_reaches_artifact_evidence():
    """The one artifact-route state `EVIDENCE_ABOUT_THE_PAPER` admits."""
    assert taxonomy.evidence_state("ARTIFACT_MISMATCH_ESTABLISHED") == "ARTIFACT_EVIDENCE"
    assert "ARTIFACT_EVIDENCE" in taxonomy.EVIDENCE_ABOUT_THE_PAPER
    for other in ("ARTIFACT_FACT_ESTABLISHED", "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS",
                  "ARTIFACT_INSPECTION_INCONCLUSIVE"):
        assert taxonomy.evidence_state(other) not in taxonomy.EVIDENCE_ABOUT_THE_PAPER
    assert taxonomy.resolution_state(
        taxonomy.evidence_state("ARTIFACT_CONCERN_VERIFIED_ENDPOINTS")) == "UNRESOLVED"
    assert taxonomy.resolution_state(
        taxonomy.evidence_state("ARTIFACT_FACT_ESTABLISHED")) == "RESOLVED_FROM_ARTIFACT"


def test_a_scope_mismatch_is_not_a_discharge(checkout):
    snap = artifact_evidence.snapshot(checkout)
    got = artifact_evidence.inspect(
        _doc(), checkout, scope="CONFIG_LITERAL",
        statements=["does config key K equal V?"],
        facts=[artifact_evidence.entrypoint_fact(checkout, snap)])
    assert not got.discharged
    assert "none answers the CONFIG_LITERAL question" in got.reason


def test_cloning_a_repository_is_not_artifact_evidence(checkout):
    snap = artifact_evidence.snapshot(checkout)
    nothing_asked = artifact_evidence.inspect(
        _doc(), checkout, facts=[artifact_evidence.file_fact(checkout, snap, "train.py")])
    assert not nothing_asked.discharged
    assert "obtaining an artifact is not evidence" in nothing_asked.reason
    assert artifact_evidence.outcome_disposition(nothing_asked) \
        == "ARTIFACT_INSPECTION_INCONCLUSIVE"


def test_artifact_inspection_cannot_resolve_a_question_that_requires_execution(
        checkout, tmp_path):
    outcomes, _whole = _route(Config(projects_dir=tmp_path / "projects"), checkout,
                              [_target("t1", "PRINTED_QUANTITY")])
    assert outcomes[0].disposition == "COMPARISON_BLOCKED"
    assert outcomes[0].evidence_state != "ARTIFACT_EVIDENCE"
    assert "whose answer is a measured result" in outcomes[0].reason
    for kind in ("REPRODUCTION", "PRINTED_QUANTITY", "COMPOSITION", "ATTRIBUTION"):
        assert artifact_evidence.requires_execution(kind), kind


# --------------------------------------------------------------------------- #
# 24-27. What an artifact finding may and may not set in motion
# --------------------------------------------------------------------------- #
def test_static_inspection_never_suppresses_an_execution():
    runnable = _target("t1", "PRINTED_QUANTITY")
    runnable.routes = ["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"]
    runnable.expected_value = 91.4
    decided = planner.plan(runnable, artifact_available=True)
    assert decided.action != "ARTIFACT_INSPECTION_ONLY"
    assert decided.route == "AUTHOR_CODE_EXECUTION"
    assert decided.requires_execution

    inspect_only = _target("t2", "SPECIFICATION")
    assert planner.plan(inspect_only, artifact_available=True).action \
        == "ARTIFACT_INSPECTION_ONLY"
    assert planner.plan(inspect_only, artifact_available=False).action \
        != "ARTIFACT_INSPECTION_ONLY"


def test_an_artifact_finding_may_trigger_execution_and_focused_validation():
    obj = _target("t1", "ATTRIBUTION")
    obj.routes = ["ARTIFACT_INSPECTION", "FOCUSED_VALIDATION_EXPERIMENT"]
    decided = planner.plan(obj, artifact_available=True, specification_complete=True)
    assert decided.route == "FOCUSED_VALIDATION_EXPERIMENT"
    assert decided.requires_execution

    obj2 = _target("t2", "PRINTED_QUANTITY")
    obj2.routes = ["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"]
    assert planner.plan(obj2, artifact_available=True).requires_execution


def test_an_inspection_records_what_it_buys_a_later_route(checkout, tmp_path):
    """Recorded, and acted on by nothing here: narrowing a command is `experiment_id`'s."""
    _outcomes, whole = _route(
        Config(projects_dir=tmp_path / "projects"), checkout,
        [_target("t1", "", claim="The released repository implements the described method.")])
    assert whole is not None
    assert any("narrows the candidate commands" in e for e in whole.escalations)


def test_no_artifact_observation_can_bypass_materiality():
    from harness import materiality
    for disposition in ("ARTIFACT_FACT_ESTABLISHED", "ARTIFACT_MISMATCH_ESTABLISHED",
                        "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS",
                        "ARTIFACT_INSPECTION_INCONCLUSIVE"):
        out = TargetOutcome(target_id="t1", disposition=disposition,
                            provenance="artifact")
        assert out.establishes_failure is False, disposition
        # Not even with a provenance the reproduction ceiling admits: that ceiling
        # governs EXECUTION, and none of this is execution evidence.
        assert TargetOutcome(target_id="t1", disposition=disposition,
                             provenance="repo_exec").establishes_failure is False
        assert materiality.material_target_failure(
            [_target("t1", "SPECIFICATION")], [out]) is None


# --------------------------------------------------------------------------- #
# 28. Mutation after execution retracts the execution, not the history
# --------------------------------------------------------------------------- #
def test_mutation_after_execution_does_not_rewrite_the_pre_run_inspection(checkout):
    pre = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = _bind(checkout, pre, span)
    assert fact.authority == "PAPER_ARTIFACT_MISMATCH"

    (checkout / "config.yaml").write_text("batch_size: 8\n", encoding="utf-8")
    (checkout / "results.json").write_text("{}", encoding="utf-8")
    post = artifact_evidence.snapshot(checkout)

    assert not post.audited and post.dirty
    assert not artifact_evidence.same_snapshot(pre, post)

    assert fact.snapshot is not None and fact.snapshot.audited
    assert fact.snapshot.tree_sha == pre.tree_sha
    assert fact.authority == "PAPER_ARTIFACT_MISMATCH"
    assert artifact_evidence.file_fact(checkout, post, "train.py").authority == "NONE"
    assert not artifact_evidence.span_still_holds(checkout, span)


# --------------------------------------------------------------------------- #
# 29. The rule audit is enforced at the renderer, not only recorded
# --------------------------------------------------------------------------- #
def test_the_review_does_not_print_a_detector_the_audit_classified_unsafe():
    from harness.artifacts import CodeAudit, CodeAuditFinding
    from harness.stages import report as report_stage

    audit = CodeAudit(repo_path="r", files_scanned=165, lines_scanned=40_000, findings=[
        # A rule id that no longer exists in code_audit.py's rule set (deleted along with
        # the other eight measured-false/never-fired heuristics) must still default to
        # class D here rather than being trusted because it once had a name.
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
    assert "run_pruning.py" not in rendered, "an unclassified/retired rule id is not"
    assert "audited as unsafe for reviewer output" in rendered
    # The machine trace keeps both: suppression is a rendering decision.
    assert len(audit.findings) == 2


# --------------------------------------------------------------------------- #
# 30. No vocabulary in `artifacts.py` may be defined twice
# --------------------------------------------------------------------------- #
def test_no_module_level_vocabulary_is_defined_twice():
    """Found by running the auditor: a shadowed constant counts the wrong things silently.

    `IDENTITY_STATES` was defined twice — once for `ProbeSpec`'s experiment-identity
    resolution (established / ambiguous / no_candidate / unmapped / unsupported) and once,
    later, for the artifact route's own four. The second definition won at import time, so
    the auditor run's identity histogram came back keyed on five values none of which the
    artifact route ever writes, and every bucket read zero. A miscount that looks like a
    measurement is worse than a crash.
    """
    import ast
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "harness" / "artifacts.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    duplicates = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id.isupper()):
                continue
            if target.id in seen:
                duplicates.append(f"{target.id} (lines {seen[target.id]} and {node.lineno})")
            seen[target.id] = node.lineno
    assert not duplicates, f"shadowed vocabularies in artifacts.py: {duplicates}"


# --------------------------------------------------------------------------- #
# 31. A level-2 mismatch may not rest on a number the auditor picked
# --------------------------------------------------------------------------- #
def test_a_paper_value_the_harness_cannot_re_read_from_the_span_is_not_a_mismatch(
        tmp_path, checkout):
    """Found by hand-checking this corpus's first level-2 mismatch.

    The auditor quoted the whole of `apt-icml`'s Table 6 and reported the paper value as
    "Epochs 16 (CNN/DM column)". The span really does say 16 — and it also says 40, 32, 15
    and 6, and which column is CNN/DM's is a reading of a table layout extraction flattened
    away. Quote the cell and it binds; quote the table and it does not.
    """
    table_row = ("Learning rate 2e-4 2e-4 2e-4 1e-4 1e-4 Batch size 32 32 32 16 32 "
                 "Epochs 40 40 40 16 15 Distill epochs 20 20 20 6 -")
    doc = PaperDoc(paper_id="p", title="T", n_pages=1, sections=[
        Section(section_idx=0, title="Appendix", page_start=1, text=table_row)])
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "epochs: 100")

    # `artifact_value` is set to what the relocated span itself cleanly derives (100, the
    # committed `config.yaml`'s `epochs:` line) rather than an arbitrary number, so this
    # test isolates the PAPER-side rule it was written for: with change 2
    # (`_derivable_from` applied symmetrically) an artifact side the harness cannot
    # re-read would trip its OWN refusal first and this test would stop meaning what its
    # docstring says. The mirror rule, on the artifact side, is asserted by
    # `test_an_artifact_value_the_harness_cannot_re_read_from_the_span_caps_at_endpoints_verified`
    # below.
    picked = artifact_evidence.bind_mismatch(
        doc, snap, paper_quote=table_row, paper_value="Epochs 16 (CNN/DM column)",
        span=span, artifact_value="100", root=checkout, experiment_id="Table 6, CNN/DM",
        identity_basis="readme_maps_the_experiment", identity_file="README.md",
        identity_quote="Table 2 of the paper is produced by")
    assert picked.identity_state == "ESTABLISHED", "the identity itself is fine"
    assert picked.refusal == "paper_value_not_derivable"
    assert picked.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"
    assert picked.paper_ref, "the paper citation DID relocate, and the record says so"
    assert "Quote the cell, not the table" in picked.statement

    # The same value quoted at the CELL binds, because the harness can re-read it.
    cell = PaperDoc(paper_id="p", title="T", n_pages=1, sections=[
        Section(section_idx=0, title="Appendix", page_start=1,
                text="For CNN/DM we train T5-base for 16 epochs.")])
    bound = artifact_evidence.bind_mismatch(
        cell, snap, paper_quote="For CNN/DM we train T5-base for 16 epochs.",
        paper_value="16", span=span, artifact_value="100", root=checkout,
        experiment_id="Table 6, CNN/DM", identity_basis="readme_maps_the_experiment",
        identity_file="README.md", identity_quote="Table 2 of the paper is produced by")
    assert bound.authority == "PAPER_ARTIFACT_MISMATCH", bound.refusal


def test_an_artifact_value_the_harness_cannot_re_read_from_the_span_caps_at_endpoints_verified(
        checkout):
    """The mirror of the rule above, on the ARTIFACT side (change 2).

    Relocating a `code_quote` proves the LINE exists at the pinned commit; it proves
    nothing about which NUMBER on that line the auditor's `artifact_value` refers to. A
    span quoting a whole block that states the same numeral more than once is exactly as
    unreadable as a table quoting six numbers — the harness cannot tell which "16" the
    auditor means — so this must cap at ENDPOINTS_VERIFIED_ARTIFACT_CONCERN and never
    reach PAPER_ARTIFACT_MISMATCH, even though the paper side here is cleanly derivable.
    """
    block = "lr_first: 16\nlr_second: 32\nlr_third: 16\n"
    (checkout / "config.yaml").write_text(block, encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "a block that repeats a numeral")
    snap = artifact_evidence.snapshot(checkout)
    assert snap.audited
    span = artifact_evidence.relocate(checkout, "config.yaml", block)
    assert span is not None

    doc = PaperDoc(paper_id="p", title="T", n_pages=1, sections=[
        Section(section_idx=0, title="Method", page_start=1,
                text="We use a batch size of 16.")])
    picked = artifact_evidence.bind_mismatch(
        doc, snap, paper_quote="We use a batch size of 16.", paper_value="16",
        span=span, artifact_value="16 (first stage)", root=checkout,
        experiment_id="lr schedule", identity_basis="readme_maps_the_experiment",
        identity_file="README.md", identity_quote="Table 2 of the paper is produced by")
    assert picked.identity_state == "ESTABLISHED", "the identity itself is fine"
    assert picked.refusal == "artifact_value_not_derivable"
    assert picked.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"
    assert picked.paper_ref, "the paper citation DID relocate, and the record says so"
    assert "Quote the assignment, not the block" in picked.statement


def test_two_prose_descriptions_are_a_concern_and_never_a_mismatch(checkout):
    """A level-2 mismatch compares QUANTITIES. Two texts that differ is a reading."""
    doc = PaperDoc(paper_id="p", title="T", n_pages=1, sections=[
        Section(section_idx=0, title="Method", page_start=1,
                text="Real samples are drawn from the matching weather's test split.")])
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    fact = artifact_evidence.bind_mismatch(
        doc, snap, paper_quote="Real samples are drawn from the matching weather's "
                               "test split.",
        paper_value="the matching weather's split", span=span,
        artifact_value="always the snow split", root=checkout,
        experiment_id="Table 2", identity_basis="readme_maps_the_experiment",
        identity_file="README.md", identity_quote="Table 2 of the paper is produced by")
    assert fact.refusal == "values_not_comparable"
    assert fact.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"


# --------------------------------------------------------------------------- #
# 33. An escalation is derived from the probe, and reaches no decision
# --------------------------------------------------------------------------- #
def test_an_escalation_is_keyed_on_the_probe_and_not_on_the_reading(checkout):
    """What an observation ENABLES is a property of what was looked at."""
    snap = artifact_evidence.snapshot(checkout)
    span = artifact_evidence.relocate(checkout, "config.yaml", "batch_size: 32")
    concern = _bind(checkout, span=span, snap=snap, identity_basis="auditor_assertion",
                    identity_file="", identity_quote="",
                    probe="code_review:METRIC_MISMATCH")
    assert concern.endpoints_only
    lines = artifact_evidence.escalations_from([
        artifact_evidence.entrypoint_fact(checkout, snap), concern])
    assert any("narrows the candidate commands" in x for x in lines)
    assert any("metric implementation" in x for x in lines)

    # A fact carrying no authority enables nothing.
    assert artifact_evidence.escalations_from(
        [artifact_evidence.file_fact(checkout, snap, "absent.sh")]) == []

    # AND AN ESCALATION IS A SENTENCE. There is no field on the inspection through which
    # an interesting static reading could suppress a measurement route.
    got = artifact_evidence.inspect(_doc(), checkout, statements=[_PAPER_BATCH],
                                    scope="CONFIG_LITERAL", facts=[concern])
    assert all(isinstance(x, str) for x in got.escalations)
    assert artifact_evidence.outcome_disposition(got) \
        == "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS"


def test_one_fact_about_one_checkout_is_counted_once(tmp_path):
    """Found on a live run, not by reading the code.

    `facts_for` runs PER TARGET, and a fact about the CHECKOUT does not vary with which
    paper statement it is being held against. So `acl`'s nine artifact-route targets each
    produced the identical `ENTRYPOINT_PRESENCE` fact about the same file at the same
    commit, and the route's own persisted record read *"9 bounded fact(s) were established
    about the checkout"*. One was. It was checked against nine statements and answered
    none of them — a different sentence, and the true one.

    The count matters because it is the number this route reports about itself, and a
    route that inflates what it established is the failure mode the whole authority ladder
    exists to prevent, arriving through arithmetic instead of through a verdict.
    """
    from harness.stages.artifact import distinct_facts

    def fact(file: str, line: int, probe: str = "entrypoint_present",
             settles: str = "ENTRYPOINT_PRESENCE", ref: str = "P0:0-10") -> ArtifactFact:
        return ArtifactFact(
            probe=probe, settles=settles, authority="ARTIFACT_FACT", paper_ref=ref,
            span=SourceSpan(file=file, line=line, end_line=line + 10,
                            file_sha256="a" * 64, quote=""),
            statement=f"The checkout advertises `{file}`.")

    same = [fact("eval.py", 1) for _ in range(9)]
    assert len(distinct_facts(same)) == 1, "one fact, established nine times, is one fact"

    # AND IT MUST NOT MERGE WHAT IS GENUINELY DIFFERENT. A different file, a different
    # line, a different probe, a different bounded question and a different paper
    # reference are each a different fact.
    varied = [fact("eval.py", 1), fact("train.py", 1), fact("eval.py", 44),
              fact("eval.py", 1, probe="file_present"),
              fact("eval.py", 1, settles="FILE_PRESENCE"),
              fact("eval.py", 1, ref="P2:0-10")]
    assert len(distinct_facts(varied)) == len(varied), [f.span.file for f in varied]

    # Order is the order the route worked in, and the FIRST establishment is the one kept.
    kept = distinct_facts([fact("b.py", 1), fact("a.py", 1), fact("b.py", 1)])
    assert [f.span.file for f in kept] == ["b.py", "a.py"]
    assert distinct_facts([]) == []


# --------------------------------------------------------------------------- #
# 34-39. THE MISSING CALLER: `artifact_review_driver.run` is actually dispatched from
# `stages/artifact.py`, and only under the preconditions this route itself checks.
#
# Before this section, `harness/stages/artifact.py` imported only `artifact_evidence`,
# `claims` and `state` — never `artifact_review_driver` — so every `route.json` this
# stage ever wrote had `"proposed": 0` because the reviewer was never dispatched, not
# because it ran and found nothing. These tests call `artifact_stage.run_route` itself
# (the production entry point `stages/probe.py` calls), never the driver directly.
# --------------------------------------------------------------------------- #
def _reader_cmd(tmp_path: Path, payload: dict, name: str = "fake_reader") -> str:
    """A REAL subprocess that writes a fixed JSON payload to `{out}` and ignores `{prompt}`.

    Exercises `artifact_review_driver.run`'s actual subprocess path — `Popen`, the pinned
    tool-policy file, `parse_concerns`, `locate_all`'s relocate-or-drop — rather than
    standing in for it, so a test built on this is a test of the production wiring and
    not of a stand-in for it.
    """
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import sys, json\n"
        f"payload = {payload!r}\n"
        "with open(sys.argv[2], 'w', encoding='utf-8') as fh:\n"
        "    json.dump(payload, fh)\n",
        encoding="utf-8")
    return f'"{sys.executable}" "{script}" "{{prompt}}" "{{out}}"'


_BROAD_CLAIM = "The released repository implements the described method."


def test_gate_closed_never_dispatches_the_reviewer(checkout, tmp_path, monkeypatch):
    """`SH_ALLOW_ARTIFACT_REVIEW` unset (the default) means the driver is never called —
    not called-and-ignored, never called at all."""
    calls: list[object] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run",
                        lambda *a, **k: calls.append(1) or None)
    cfg = Config(projects_dir=tmp_path / "projects")
    assert cfg.allow_artifact_review is False
    outcomes, whole = _route(cfg, checkout, [_target("t1", "", claim=_BROAD_CLAIM)])
    assert calls == []
    # The route still runs, on the deterministic probes alone — the gate is closed, not
    # the route.
    assert outcomes and whole is not None


def test_gate_open_with_a_real_checkout_dispatches_the_reviewer(checkout, tmp_path,
                                                                monkeypatch):
    """The gate open and an audited checkout present is the ONE condition that dispatches
    it, and its relocated fact reaches the paper-level record through the ordinary
    `ArtifactFact` channel — no second channel was added for it."""
    calls: list[tuple] = []

    def fake_run(cfg, pid, doc, root, prompt_text, *, url="", statements=None, facts=None):
        calls.append((pid, url, bool(prompt_text), root))
        return ArtifactInspection(paper_id=pid, facts=[ArtifactFact(
            probe="code_review:SUSPICIOUS_IMPLEMENTATION", authority="ARTIFACT_FACT",
            statement="a code-only observation the auditor located and the harness "
                      "relocated")])

    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run", fake_run)
    cfg = Config(projects_dir=tmp_path / "projects", allow_artifact_review=True)
    _outcomes, whole = _route(cfg, checkout, [_target("t1", "", claim=_BROAD_CLAIM)])
    assert len(calls) == 1, "one best-effort call per paper, not one per target"
    assert calls[0][0] == "p" and calls[0][2] is True
    assert whole is not None and any(
        f.probe == "code_review:SUSPICIOUS_IMPLEMENTATION" for f in whole.facts)


def test_no_checkout_never_dispatches_the_reviewer_even_with_the_gate_open(tmp_path,
                                                                           monkeypatch):
    """No directory at all: the route's own top-of-function guard returns before a
    snapshot is even taken, so the reviewer is never reached."""
    calls: list[object] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run",
                        lambda *a, **k: calls.append(1) or None)
    cfg = Config(projects_dir=tmp_path / "projects", allow_artifact_review=True)
    result = _route(cfg, tmp_path / "nope", [_target("t1", "", claim=_BROAD_CLAIM)])
    assert calls == []
    assert result == ([], None)


def test_a_dirty_checkout_never_dispatches_the_reviewer_even_with_the_gate_open(
        checkout, tmp_path, monkeypatch):
    """A directory exists and is a git repository, but its working tree is not clean:
    `snap.audited` is False, which is exactly "not a pinned checkout" and gates the
    reviewer the same way a missing directory does."""
    (checkout / "config.yaml").write_text("batch_size: 999\n", encoding="utf-8")  # uncommitted
    calls: list[object] = []
    monkeypatch.setattr(artifact_stage.artifact_review_driver, "run",
                        lambda *a, **k: calls.append(1) or None)
    cfg = Config(projects_dir=tmp_path / "projects", allow_artifact_review=True)
    assert not artifact_evidence.snapshot(checkout).audited
    _route(cfg, checkout, [_target("t1", "", claim=_BROAD_CLAIM)])
    assert calls == []


def test_an_unlocatable_citation_is_dropped_through_the_production_path(checkout, tmp_path):
    """One relocatable citation and one that is nowhere in the pinned tree: the second is
    DROPPED WHOLE by `artifact_review_driver.locate_all`, and the route this stage
    produces reflects only the first — proving the wiring does not bypass relocation on
    its way from the driver into this stage's own facts."""
    payload = {"concerns": [
        {"kind": "HARD_CODED_RESULT", "title": "t", "statement": "s",
         "file": "config.yaml", "code_quote": "this exact string is nowhere in the file"},
        {"kind": "SUSPICIOUS_IMPLEMENTATION", "title": "t2", "statement": "s2",
         "file": "config.yaml", "code_quote": "batch_size: 32"},
    ], "notes": "n"}
    cfg = Config(projects_dir=tmp_path / "projects", allow_artifact_review=True,
                artifact_review_cmd=_reader_cmd(tmp_path, payload))
    _outcomes, whole = _route(cfg, checkout, [_target("t1", "", claim=_BROAD_CLAIM)])
    assert whole is not None
    reviewer_facts = [f for f in whole.facts if f.probe.startswith("code_review:")]
    assert len(reviewer_facts) == 1, "the unlocatable citation must not survive"
    assert reviewer_facts[0].span is not None
    assert reviewer_facts[0].span.file == "config.yaml"


def test_a_concern_cannot_self_certify_a_mismatch_through_the_production_path(
        checkout, tmp_path):
    """Only a DETERMINISTIC identity basis that itself relocates reaches
    `PAPER_ARTIFACT_MISMATCH`; the auditor's own reading, however confident, caps at
    `ARTIFACT_CONCERN_VERIFIED_ENDPOINTS` — asserted at the production entry point, not
    only inside `artifact_evidence.bind_mismatch` itself."""
    ambiguous = {"concerns": [{
        "kind": "PAPER_CODE_MISMATCH", "title": "batch size", "statement": "s",
        "file": "config.yaml", "code_quote": "batch_size: 32",
        "paper_quote": _PAPER_BATCH, "paper_value": "128", "artifact_value": "32",
        "experiment_id": "probably Table 2", "identity_basis": "auditor_assertion"}],
        "notes": "n"}
    cfg_amb = Config(projects_dir=tmp_path / "projects-amb", allow_artifact_review=True,
                     artifact_review_cmd=_reader_cmd(tmp_path, ambiguous, "amb"))
    out_amb, _whole = _route(cfg_amb, checkout, [_target("t1", "", claim=_BROAD_CLAIM)])
    assert out_amb[0].disposition == "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS"
    assert out_amb[0].disposition != "ARTIFACT_MISMATCH_ESTABLISHED"
    assert out_amb[0].evidence_state not in taxonomy.EVIDENCE_ABOUT_THE_PAPER
    assert out_amb[0].establishes_failure is False

    # The contrast: the SAME shape of concern, with a deterministic identity basis that
    # DOES relocate (the fixture's own README, exactly as `_bind`'s defaults use it), does
    # reach the top of the ladder — proving the cap above is a real ceiling and not a bug
    # that would have capped everything regardless of what the auditor supplied.
    bound = {"concerns": [{
        "kind": "PAPER_CODE_MISMATCH", "title": "batch size", "statement": "s",
        "file": "config.yaml", "code_quote": "batch_size: 32",
        "paper_quote": _PAPER_BATCH, "paper_value": "128", "artifact_value": "32",
        "config_key": "batch_size", "experiment_id": "Table 2",
        "identity_basis": "readme_maps_the_experiment", "identity_file": "README.md",
        "identity_quote": "Table 2 of the paper is produced by"}], "notes": "n"}
    cfg_bound = Config(projects_dir=tmp_path / "projects-bound", allow_artifact_review=True,
                       artifact_review_cmd=_reader_cmd(tmp_path, bound, "bound"))
    out_bound, _whole2 = _route(cfg_bound, checkout, [_target("t1", "", claim=_BROAD_CLAIM)])
    assert out_bound[0].disposition == "ARTIFACT_MISMATCH_ESTABLISHED"
    assert out_bound[0].evidence_state == "ARTIFACT_EVIDENCE"
    # AND EVEN HERE — a real, harness-verified paper/code disagreement — it is never a
    # material failure on its own. That is invariant 8 and requirement 7 of this wiring,
    # not a property this test is free to assume: it is asserted directly.
    assert out_bound[0].establishes_failure is False


def test_a_verified_concern_never_touches_a_target_this_route_does_not_own(
        checkout, tmp_path):
    """A verified concern reaches the artifact-inspection target that actually raises the
    broad question it bears on; a target planned for EXECUTION is invisible to this route
    by construction — `planned()` only ever returns `ARTIFACT_INSPECTION_ONLY` pairs — so
    nothing this route does can suppress it, whatever the auditor found in the same
    checkout. This is what "may narrow or inform execution planning but must never
    suppress a required execution" means operationally: this route returns NO outcome at
    all for the executing target, so `stages/probe.py`'s own merge
    (`target_set.outcomes = [... not in replaced ...] + artifact_outcomes`) leaves that
    target's real outcome untouched.
    """
    from harness import state

    payload = {"concerns": [{
        "kind": "PAPER_CODE_MISMATCH", "title": "batch size", "statement": "s",
        "file": "config.yaml", "code_quote": "batch_size: 32",
        "paper_quote": _PAPER_BATCH, "paper_value": "128", "artifact_value": "32",
        "config_key": "batch_size", "experiment_id": "Table 2",
        "identity_basis": "readme_maps_the_experiment", "identity_file": "README.md",
        "identity_quote": "Table 2 of the paper is produced by"}], "notes": "n"}
    cfg = Config(projects_dir=tmp_path / "projects", allow_artifact_review=True,
                artifact_review_cmd=_reader_cmd(tmp_path, payload))
    state.create_project(cfg, "", "T", pid="p")

    inspection_target = _target("t1", "", claim=_BROAD_CLAIM)
    executing_target = _target("t2", "PRINTED_QUANTITY", claim="our method reaches "
                                                               "91.4 accuracy")
    ts = TargetSet(
        paper_id="p", objects=[inspection_target, executing_target],
        plans=[_plan("t1"),
              PlanDecision(target_id="t2", action="AUTHOR_CODE_EXECUTION",
                          route="AUTHOR_CODE_EXECUTION", requires_execution=True)])
    outcomes, _whole = artifact_stage.run_route(cfg, "p", _doc(), ts, checkout, url="u")

    # The concern was found and DID reach the target whose own claim is the broad one.
    assert len(outcomes) == 1 and outcomes[0].target_id == "t1"
    assert outcomes[0].disposition == "ARTIFACT_MISMATCH_ESTABLISHED"
    # The executing target has no outcome here AT ALL — this route does not own it and
    # cannot suppress, defer, or overwrite it.
    assert all(o.target_id != "t2" for o in outcomes)
