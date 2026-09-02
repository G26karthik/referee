"""A reader must be able to tell what was checked from what was argued.

A finding has three layers and the report is only auditable if they stay apart:

  the lens quoted something          evidence_quote + evidence_ref, re-verified
  the harness confirmed it           verified_observation + evidence_class, machine-written
  the lens argued from it            claim, reasoning, conclusion, severity — inference

Before this split, `Finding.statement` carried all three at once and flowed straight into
`overall_verdict`. The quote was checked; the argument from it never was; and the report
printed both in the same voice, so an accurate quote attached to an unsupported inference
read exactly like a verified defect.

The load-bearing test here is `test_a_lens_cannot_certify_its_own_reasoning`. If a lens
could set `verified_observation` or `evidence_class`, it could assert that the harness had
confirmed its argument — which is precisely the confusion the split exists to prevent.
"""
from __future__ import annotations

import pytest

from harness.artifacts import (EVIDENCE_CLASSES, CommitVerification, ConfigurationIdentity,
                               ExecAuthorization, ExecCapability, ExperimentIdentity, Finding,
                               MetricIdentity, PaperDoc, ProbeResult, Reconciliation,
                               RepoAcquisition, ResourceCapability, Section, Table)
from harness.stages.audit import _coerce, verify_evidence
from harness.stages.report import build_chain, render_eval_report
from harness.artifacts import EvalReport

CELL = "91.4"
PROSE = "The proposed method reaches 91.4 accuracy on the held-out split."


def _doc() -> PaperDoc:
    return PaperDoc(paper_id="p", title="T",
                    sections=[Section(section_idx=0, title="Results", page_start=3, text=PROSE)],
                    tables=[Table(table_idx=0, page=3, caption="Table 1",
                                  rows=[["method", "acc"], ["ours", CELL]])])


def _ctx():
    doc = _doc()
    corpus = "".join(s.text for s in doc.sections).lower().replace(" ", "")
    return corpus, {t.table_idx: t for t in doc.tables}


def _coerced(raw: dict) -> tuple[list[Finding], int]:
    corpus, by_idx = _ctx()
    report, dropped = _coerce("overclaim", {"findings": [raw]}, corpus, by_idx)
    return report.findings, dropped


# --------------------------------------------------------------------------- #
# The machine half
# --------------------------------------------------------------------------- #
def test_a_matched_cell_is_classified_and_described_by_the_harness():
    corpus, by_idx = _ctx()
    klass, observation = verify_evidence(CELL, "T0:r1:c1", corpus, by_idx)
    assert klass == "cell_verified"
    assert "T0:r1:c1" in observation and CELL in observation
    assert "match" in observation.lower()


def test_a_matched_prose_quote_is_classified_separately_from_a_cell():
    """A cell citation is checkable in seconds; a prose quote needs a search. The report
    must not present them as the same strength of evidence."""
    corpus, by_idx = _ctx()
    klass, observation = verify_evidence(PROSE, "p3", corpus, by_idx)
    assert klass == "prose_verified" and "verbatim" in observation
    assert klass != "cell_verified"


@pytest.mark.parametrize("quote,ref", [
    ("a number the paper never printed", "T0:r1:c1"),   # wrong cell contents
    (CELL, "T9:r0:c0"),                                  # no such table
    (CELL, "figure 3"),                                  # malformed ref
    (CELL, ""),                                          # no ref at all
    ("", "T0:r1:c1"),                                    # no quote
    ("91.4", "p3"),                                      # prose quote below the length floor
])
def test_anything_unverified_is_classified_as_such(quote, ref):
    corpus, by_idx = _ctx()
    klass, observation = verify_evidence(quote, ref, corpus, by_idx)
    assert klass == "unverified" and observation == ""


def test_every_class_the_model_declares_is_one_of_the_known_ones():
    corpus, by_idx = _ctx()
    for quote, ref in ((CELL, "T0:r1:c1"), (PROSE, "p3"), ("x", "")):
        assert verify_evidence(quote, ref, corpus, by_idx)[0] in EVIDENCE_CLASSES


# --------------------------------------------------------------------------- #
# The lens cannot forge the machine half
# --------------------------------------------------------------------------- #
def test_a_lens_cannot_certify_its_own_reasoning():
    """The whole point of the split. A lens supplying these two fields is overwritten."""
    kept, _ = _coerced({
        "finding_id": "overclaim-01", "severity": "MAJOR", "statement": "a defect",
        "evidence_quote": CELL, "evidence_ref": "T0:r1:c1",
        "verified_observation": "The harness independently confirmed my entire argument.",
        "evidence_class": "cell_verified",
    })
    assert len(kept) == 1
    f = kept[0]
    assert "confirmed my entire argument" not in f.verified_observation
    assert f.verified_observation.startswith("Cell T0:r1:c1")


def test_a_lens_cannot_upgrade_prose_evidence_to_a_cell_citation():
    kept, _ = _coerced({
        "finding_id": "overclaim-01", "severity": "MAJOR", "statement": "a defect",
        "evidence_quote": PROSE, "evidence_ref": "p3",
        "evidence_class": "cell_verified",
    })
    assert kept[0].evidence_class == "prose_verified"


def test_an_unverifiable_finding_is_dropped_however_it_is_labelled():
    kept, dropped = _coerced({
        "finding_id": "overclaim-01", "severity": "FATAL", "statement": "a defect",
        "evidence_quote": "the paper says the moon is square", "evidence_ref": "T0:r1:c1",
        "evidence_class": "cell_verified",
        "verified_observation": "Verified beyond doubt.",
    })
    assert kept == [] and dropped == 1


# --------------------------------------------------------------------------- #
# The layers stay apart, and old files still load
# --------------------------------------------------------------------------- #
def test_the_three_lens_layers_are_kept_separate():
    kept, _ = _coerced({
        "finding_id": "overclaim-01", "severity": "MAJOR", "statement": "fallback text",
        "claim": "the paper claims 95 accuracy",
        "reasoning": "the cited cell says 91.4, so the abstract overstates it",
        "conclusion": "the headline number is not supported",
        "evidence_quote": CELL, "evidence_ref": "T0:r1:c1",
    })
    f = kept[0]
    assert f.as_claim() == "the paper claims 95 accuracy"
    assert f.as_reasoning().startswith("the cited cell says")
    assert f.as_conclusion() == "the headline number is not supported"
    assert f.as_reasoning() != f.as_conclusion()


def test_a_lens_file_written_before_the_split_still_loads():
    """Backward compatibility is not a nicety here: the pilot corpus's lens files predate
    these fields, and a schema change that invalidated them would silently reduce the
    evidence base rather than fail loudly."""
    kept, dropped = _coerced({
        "finding_id": "overclaim-01", "severity": "MAJOR",
        "statement": "the only prose this file carries",
        "evidence_quote": CELL, "evidence_ref": "T0:r1:c1",
    })
    assert dropped == 0 and len(kept) == 1
    f = kept[0]
    assert f.as_reasoning() == f.as_conclusion() == "the only prose this file carries"
    assert f.evidence_class == "cell_verified"
    assert f.verified_observation, "the machine half is filled even for an old file"


def test_the_report_labels_inference_as_inference():
    f = Finding(finding_id="overclaim-01", lens="overclaim", severity="MAJOR",
                title="headline overstates the table", statement="s",
                evidence_quote=CELL, evidence_ref="T0:r1:c1",
                evidence_class="cell_verified",
                verified_observation="Cell T0:r1:c1 contains '91.4', which matches.",
                reasoning="the abstract rounds this up to 95")
    md = render_eval_report(EvalReport(paper_id="p", title="T", verdict="YELLOW",
                                       verdict_reason="one MAJOR", findings=[f],
                                       lenses_run=["overclaim"]))
    assert "*Verified*" in md and "Cell T0:r1:c1 contains" in md
    assert "not verified" in md and "the abstract rounds this up to 95" in md
    verified_at = md.index("*Verified*")
    inference_at = md.index("Inference")
    assert verified_at < inference_at, "evidence is shown before the argument from it"


# --------------------------------------------------------------------------- #
# The experimental chain
# --------------------------------------------------------------------------- #
def _probe(**over) -> ProbeResult:
    base = dict(
        paper_id="p", finding_id="overclaim-01", claim="the cited claim",
        provenance="synthesized", backend="local",
        repo=RepoAcquisition(url="https://github.com/a/b", status="cached", commit="c" * 40),
        experiment=ExperimentIdentity(state="no_candidate", reason="third-party baseline"),
        metric_identity=MetricIdentity(state="unmapped"),
        configuration=ConfigurationIdentity(state="unmapped"),
        capability=ExecCapability(reason_code="environment_incompatible"),
        resources=ResourceCapability(state="insufficient"),
        commit_verification=CommitVerification(state="verified", expected="c" * 40,
                                               actual="c" * 40),
        authorization=ExecAuthorization(allowed=False, decision="identity_unproven"),
        reconciliation=Reconciliation(status="INCONCLUSIVE", table_ref="T0:r1:c1",
                                      failure_class="experiment_unidentified"),
    )
    base.update(over)
    return ProbeResult(**base)


def test_the_chain_links_a_finding_to_repo_commit_identity_and_reconciliation():
    f = Finding(finding_id="overclaim-01", lens="overclaim", evidence_ref="T0:r1:c1",
                claim="the paper claims a 70% reduction")
    chain = build_chain([f], _probe())
    assert chain is not None
    assert chain.finding_id == "overclaim-01"
    assert chain.claim == "the paper claims a 70% reduction"
    assert chain.evidence_refs == ["T0:r1:c1"]
    assert chain.repo_url == "https://github.com/a/b" and chain.commit == "c" * 40
    assert chain.commit_state == "verified"
    assert chain.experiment_state == "no_candidate"
    assert chain.reconciliation == "INCONCLUSIVE"
    assert chain.failure_class == "experiment_unidentified"


def test_the_chain_names_the_first_broken_link_not_every_one():
    """Later links are not assessed once an earlier one fails, so listing them all would
    imply checks that never ran."""
    chain = build_chain([], _probe())
    assert chain.broken_link == "experiment identity"


@pytest.mark.parametrize("field,value,expected", [
    ("repo", RepoAcquisition(status="unavailable"), "repository"),
    ("commit_verification", CommitVerification(state="mismatch"), "audited commit"),
])
def test_an_earlier_break_outranks_a_later_one(field, value, expected):
    chain = build_chain([], _probe(**{field: value}))
    assert chain.broken_link == expected


def test_a_fully_established_chain_reports_no_break():
    est = dict(state="established", established=True)
    chain = build_chain([], _probe(
        experiment=ExperimentIdentity(**est), metric_identity=MetricIdentity(**est),
        configuration=ConfigurationIdentity(**est),
        capability=ExecCapability(established=True, reason_code="established"),
        resources=ResourceCapability(state="satisfied"),
        authorization=ExecAuthorization(allowed=True, decision="authorized"),
        provenance="repo_exec", seeds_run=[0, 1, 2],
        reconciliation=Reconciliation(status="RESOLVED_VERIFIED", table_ref="T0:r1:c1")))
    assert chain.broken_link == "" and chain.executed is True
    assert chain.reconciliation == "RESOLVED_VERIFIED"


def test_no_probe_means_no_chain_rather_than_an_empty_one():
    assert build_chain([], None) is None


def test_the_chain_is_rendered_with_its_break_named():
    chain = build_chain([], _probe())
    md = render_eval_report(EvalReport(
        paper_id="p", title="T", verdict="GREEN", verdict_reason="none",
        lenses_run=["overclaim"], probe=_probe(), experimental_chain=chain))
    assert "Experimental evidence chain" in md
    assert "experiment identity" in md
    assert "not a finding about the paper" in md


# --------------------------------------------------------------------------- #
# Severity is the link the harness cannot check
# --------------------------------------------------------------------------- #
def test_severity_on_prose_evidence_is_flagged_not_demoted():
    """The evidence behind a finding is machine-checked; the GRADE attached to it is a
    model's word, and the grade is what `overall_verdict` counts. Flagging leaves the
    call with an editor; demoting would change verdicts on something the harness cannot
    itself assess, which is the unearned inference it exists to catch."""
    from harness.stages.report import severity_review

    prose = Finding(finding_id="a-01", lens="protocol", severity="MAJOR",
                    evidence_class="prose_verified", title="t")
    cell = Finding(finding_id="a-02", lens="overclaim", severity="FATAL",
                   evidence_class="cell_verified", title="t")
    minor = Finding(finding_id="a-03", lens="confound", severity="MINOR",
                    evidence_class="prose_verified", title="t")

    flagged = severity_review([prose, cell, minor])
    assert len(flagged) == 1 and "a-01" in flagged[0]
    assert prose.severity == "MAJOR", "flagging must not change the grade"


def test_the_severity_caveat_reaches_the_report():
    f = Finding(finding_id="a-01", lens="protocol", severity="MAJOR", title="t",
                statement="s", evidence_quote=PROSE, evidence_ref="p3",
                evidence_class="prose_verified", verified_observation="found verbatim")
    md = render_eval_report(EvalReport(paper_id="p", title="T", verdict="YELLOW",
                                       verdict_reason="one MAJOR", findings=[f],
                                       lenses_run=["protocol"]))
    assert "Severity caveat" in md and "not machine-verified" in md and "a-01" in md


def test_no_caveat_when_every_serious_finding_cites_a_cell():
    f = Finding(finding_id="a-01", lens="overclaim", severity="FATAL", title="t",
                statement="s", evidence_quote=CELL, evidence_ref="T0:r1:c1",
                evidence_class="cell_verified", verified_observation="cell matches")
    md = render_eval_report(EvalReport(paper_id="p", title="T", verdict="RED",
                                       verdict_reason="one FATAL", findings=[f],
                                       lenses_run=["overclaim"]))
    assert "Severity caveat" not in md
