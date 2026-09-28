"""Regressions for the failures that left REFEREE with paper-only reviews: no numbered
result ever became a verification target, a data-only release never reached the
reconstruction fallback, a reconstruction of released data demanded a training recipe,
proofs outside the statement's own section were never shown to a checker, and a central
claim nobody checked still folded into PASS_TO_HUMAN_CLEAN.

Synthetic documents only — no paper names, numbers or expected verdicts.
Run: `python -m pytest tests/test_autonomy_regressions.py -q`
"""
from __future__ import annotations

from pathlib import Path

from harness import certificate, decide, discover, execute, locate, provenance, reimplement_driver, routes
from harness.config import Config
from harness.schema import (
    DiscoveredObject, PaperDoc, PlanDecision, ProbeSpec, ReimplementationConformance,
    ReimplementationIngredient, ReimplementationReadiness, RepoAcquisition, Section,
    TargetOutcome,
)


def _theory_doc() -> PaperDoc:
    return PaperDoc(paper_id="t", title="A bound", sections=[
        Section(section_idx=0, title="Abstract",
                text="We prove a new regret bound, developed in Section 2 of this work."),
        Section(section_idx=1, title="1. Introduction",
                text="Prior bounds are loose. As shown in Theorem 9.9. Then we move on."),
        Section(section_idx=2, title="2. Main result",
                text="We now state it. Theorem 2.1. For every integer n at least one, the "
                     "estimator satisfies E[X_n] <= 3 sqrt(n) under the stated assumptions. "
                     "Proof of Theorem 2.1 can be found in Appendix A. Theorem 2.1 yields "
                     "the rate."),
        Section(section_idx=3, title="A. Proofs",
                text="Proof of Theorem 2.1. By the first inequality, X_n <= 2 sqrt(n) + 1, "
                     "hence the claim."),
    ])


def test_numbered_results_are_found_and_citations_are_not():
    labels = [st["label"] for st in discover.formal_statements(_theory_doc())]
    assert labels == ["Theorem 2.1"], labels      # "Theorem 9.9. Then" is a citation


def test_numbered_result_becomes_a_central_certificate_target():
    ts = discover.build("t", _theory_doc(), [])
    obj = next(o for o in ts.objects if o.question_kind == "MATHEMATICAL_BOUND")
    plan = next(p for p in ts.plans if p.target_id == obj.target_id)
    assert obj.centrality == "CENTRAL"            # its section is cited by the abstract
    assert plan.route == "EXACT_CERTIFICATE" and plan.requires_execution
    assert ts.extraction_coverage["formal_statements_found"] == 1


def test_formal_target_ceiling_is_counted_not_hidden():
    ts = discover.build("t", _theory_doc(), [], max_formal=0)
    assert not any(o.question_kind == "MATHEMATICAL_BOUND" for o in ts.objects)
    assert ts.extraction_coverage["formal_statements_found"] == 1
    assert ts.extraction_coverage["formal_statements_targeted"] == 0


def test_proof_pointer_is_skipped_for_the_proof_itself():
    doc = _theory_doc()
    assert certificate.proof_location(doc, "Theorem 2.1") == (3, 0)
    assert "hence the claim" in certificate.proof_excerpt(doc, "Theorem 2.1")


def test_data_only_checkout_exhausts_the_author_route(tmp_path):
    acq = RepoAcquisition(status="cloned", path=str(tmp_path), entrypoint="")
    spec = routes.plan_execution(Config(), ProbeSpec(paper_id="p", provenance="synthesized"),
                                 acq)
    assert spec.experiment is not None and spec.experiment.state == "no_candidate"
    obj = DiscoveredObject(target_id="T", routes=["AUTHOR_CODE_EXECUTION",
                                                  "INDEPENDENT_RECONSTRUCTION"],
                           centrality="CENTRAL", harness_addressable=True)
    plan = PlanDecision(target_id="T", action="AUTHOR_CODE_REPRODUCTION",
                        route="AUTHOR_CODE_EXECUTION", requires_execution=True)
    fallback = routes.replan_after_author_code_exhausted(obj, plan, spec)
    assert fallback is not None and fallback.route == "INDEPENDENT_RECONSTRUCTION"
    ok, why = routes.admissible_if_it_succeeds(Config(diagnostic_mode=False), spec)
    assert not ok and "entrypoint" in why          # the precise blocker is named


def _readiness() -> ReimplementationReadiness:
    ings = [ReimplementationIngredient(kind=k, required=True, present=True, ref="s1", quote=q)
            for k, q in (("method", "we count"), ("training", ""), ("dataset", "our dataset"),
                         ("metric", "the count"), ("comparison_target", "6"))]
    return ReimplementationReadiness(established=True, ingredients=ings)


_SCRIPT = "import csv\nrows = list(csv.reader(open('data/labels.csv')))\nprint(len(rows))\n"
_BIND = [{"kind": "method", "impl_ref": "line 3", "impl_quote": "print(len(rows))"},
         {"kind": "dataset", "impl_ref": "line 2",
          "impl_quote": "rows = list(csv.reader(open('data/labels.csv')))"},
         {"kind": "metric", "impl_ref": "line 3", "impl_quote": "print(len(rows))"},
         {"kind": "comparison_target", "impl_ref": "line 3", "impl_quote": "print(len(rows))"}]


def test_released_data_recomputation_needs_no_training_recipe(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "labels.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    released = reimplement_driver.released_files(tmp_path)
    assert [r["path"] for r in released] == ["data/labels.csv"]
    conf = reimplement_driver.conformance(_readiness(), _SCRIPT, _BIND, generated_by="g",
                                          verified_by="v", released=released)
    assert conf.established and conf.released_inputs[0].startswith("data/labels.csv@sha256:")


def test_training_stays_required_when_no_released_file_is_opened():
    conf = reimplement_driver.conformance(_readiness(), _SCRIPT, _BIND, generated_by="g",
                                          verified_by="v", released=[])
    assert not conf.established and "training" in conf.unbound


def test_reconstruction_ingredients_are_scoped_to_the_target():
    from harness.schema import Table
    doc = PaperDoc(paper_id="s", title="t", tables=[
        Table(table_idx=0, label="1", caption="Other experiment", header=["a"], rows=[["9"]]),
        Table(table_idx=5, label="2", caption="Accuracy of each judge", header=["j", "acc"],
              rows=[["x", "0.61"]])], sections=[
        Section(section_idx=0, title="2. Other", text="We propose an attack. We train with "
                "Adam at learning rate 0.1 on the benchmark dataset and report accuracy."),
        Section(section_idx=1, title="4. Judges", text="Table 2 reports each judge's "
                "accuracy on the released benchmark dataset.")])
    r = discover.reimplementation_readiness(doc, "T5:r0:c1")
    by = {i.kind: i for i in r.ingredients}
    assert by["method"].ref == "s1" and by["dataset"].ref == "s1" and by["metric"].ref == "s1"
    assert by["comparison_target"].ref == "T5:r0:c1"
    assert discover.reimplementation_readiness(doc).ingredients[0].ref == "s0"   # unscoped


def test_prose_target_ingredients_quote_the_claim_itself():
    doc = PaperDoc(paper_id="p", title="t", sections=[
        Section(section_idx=0, title="2. Attack", text="We propose an attack and report "
                "accuracy of it on the benchmark dataset."),
        Section(section_idx=1, title="3. Data", text="We release a corpus of 1,234 labelled "
                "samples for future work.")])
    ref = locate.mint(doc, "a corpus of 1,234 labelled samples")
    r = discover.reimplementation_readiness(doc, ref.ref)
    by = {i.kind: i for i in r.ingredients}
    assert by["method"].ref == ref.ref and by["metric"].ref == ref.ref
    assert by["comparison_target"].ref == ref.ref


def test_synthesized_probe_never_rewrites_a_target_bound_claim(monkeypatch):
    class _Plan:
        keeps_finding, claim, script, arms, metric = True, "a paraphrase", "x", ["a"], "accuracy"
        mechanism = rationale = ""
        aux_metrics: list = []
    monkeypatch.setattr(routes.probe_synth, "plan", lambda *a, **k: _Plan())
    spec = ProbeSpec(paper_id="p", finding_id="f", claim="the paper's own words",
                     claim_ref="P1:0-20")
    out = routes.synthesize_probe(Config(allow_synthesis=True), PaperDoc(paper_id="p"),
                                  spec, RepoAcquisition())
    assert out.claim == "the paper's own words" and "metric" not in out.model_fields_set


def test_constant_name_is_an_implementation_locator():
    script = "PATH = 'd.csv'\n\ndef f():\n    return 1\n"
    assert reimplement_driver._implementation_locator_matches(
        script, "PATH module-level constant", "PATH = 'd.csv'")
    assert not reimplement_driver._implementation_locator_matches(
        script, "PATH module-level constant", "return 1")


def test_quote_checks_tolerate_extraction_glyphs_and_skipped_comments():
    assert certificate._normalize_ws("a \x12 b\x13 2 = c") == certificate._normalize_ws("a b 2 = c")
    assert certificate._normalize_ws("x <= 1") != certificate._normalize_ws("x < 1")
    script = "def f(s):\n    a = s + 1\n    # why\n    b = a * a\n    return b\n"
    assert reimplement_driver._implementation_locator_matches(
        script, "f()", "    a = s + 1\n    b = a * a")
    assert not reimplement_driver._implementation_locator_matches(
        script, "f()", "    a = s + 2\n    b = a * a")


def test_verifier_approval_follows_the_same_required_kinds(tmp_path):
    released = [{"path": "data/labels.csv", "sha256": "0" * 64}]
    need = reimplement_driver.required_kinds(_SCRIPT, _BIND, released)
    raw = ('{"approved": true, "approved_kinds": ["method","dataset","metric",'
           '"comparison_target"], "notes": ""}')
    assert reimplement_driver._parse_verification(raw, need)[0]
    assert not reimplement_driver._parse_verification(raw)[0]   # ML experiment: training needed


def test_five_evidence_kinds_stay_distinct_and_diagnostics_never_admit():
    kinds = {provenance.evidence_kind("repo_exec"),
             provenance.evidence_kind("reimpl_exec", released_inputs=True),
             provenance.evidence_kind("reimpl_exec"),
             provenance.evidence_kind("cert_exec", scope="proof_step"),
             provenance.evidence_kind("synthesized")}
    assert len(kinds) == 5
    assert provenance.evidence_kind("synthesized", released_inputs=True) == "DIAGNOSTIC_ONLY"
    assert not provenance.admits("synthesized")
    spec = ProbeSpec(paper_id="p", provenance="cert_exec",
                     certificate_conformance=ReimplementationConformance(scope="proof_step"))
    assert execute.spec_evidence_kind(spec) == "PROOF_AUDIT"


def test_recomputation_runs_inside_the_checkout(tmp_path):
    spec = ProbeSpec(paper_id="p", provenance="reimpl_exec", script="print(1)",
                     cwd=str(tmp_path))
    _argv, cwd = execute.resolve_command(Config(), spec, tmp_path / "x" / "probe.py", 0, "a")
    assert Path(cwd) == tmp_path


def test_proof_step_counterexample_is_a_concern_never_a_stop():
    obj = DiscoveredObject(target_id="T", centrality="CENTRAL", materiality_basis="ABSTRACT_CLAIM")
    out = TargetOutcome(target_id="T", disposition="COUNTEREXAMPLE_ESTABLISHED",
                        provenance="cert_exec", evidence_kind="PROOF_AUDIT")
    assert out.establishes_failure
    assert decide.material_target_failure([obj], [out]) is None
    assert decide.established_defects([out]) == [out]
    conclusion = out.model_copy(update={"evidence_kind": "INSTANCE_CHECK"})
    assert decide.material_target_failure([obj], [conclusion]) is conclusion


def test_unchecked_central_claim_is_never_clean():
    disp, _basis, why = decide.derive_disposition(central_unchecked=1)
    assert disp == "PASS_TO_HUMAN_UNRESOLVED" and "NOT a clean paper" in why
    assert decide.derive_disposition()[0] == "PASS_TO_HUMAN_CLEAN"


if __name__ == "__main__":
    import tempfile
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
    print("autonomy regressions ok")
