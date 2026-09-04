"""Architecture-level properties of the reviewer's REASONING, not of any one paper.

Every test here is about a structural guarantee that must hold for a paper nobody has
seen. There is deliberately not one assertion keyed on a value from the pilot corpus: no
epsilon, no metric name, no paper id, no expected finding count. A test that encoded the
right answer for SAPG or APT would pass while the architecture regressed, which is the
failure mode this file exists to avoid — so several of the tests below assert what the
code CANNOT express rather than what it does.

Grouped by the question each property protects:

  1. Is there an issue?          finding/concern/question separation, no direct promotion
  2. How confident are we?       confidence independent of severity, bounded by evidence
  3. How much does it matter?    severity non-escalation, self-consistency caps
  4. Did we do the work?         self-falsification, alternative interpretation, self-audit
  5. Is the accounting honest?   corpus completeness, non-retryable failures, isolation
"""
from __future__ import annotations

import inspect
import itertools
import json
import sys
from pathlib import Path

import pytest

from harness import controller, corpus, failures, grading, selfaudit
from harness.artifacts import (BASELINE_CLASSES, CANDIDATE_CLASSES, CONFIDENCES,
                               CORPUS_STATES, PRIOR_ART_BASES, SEVERITIES, CaseState,
                               EvalReport, Finding, Grade, PaperDoc, Section,
                               SubstantiveVerdict)
from harness.config import Config
from harness.prompts import audit as audit_prompts
from harness.prompts import grade as grade_prompts
from harness.stages import audit as audit_stage
from harness.stages import report as report_stage

RANK = grading.RANK

# The smallest real PDF in the corpus. Used ONLY as a thing that ingests successfully —
# no assertion below reads its content, so this is not a paper-specific test.
_PAPER = Path(__file__).resolve().parent.parent / "papers" / "paper1_grokking.pdf"
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _flat(s: str) -> str:
    """Whitespace-normalised, because a prompt block is hard-wrapped and a phrase that
    reads as one sentence is split across a newline plus four spaces of indent."""
    return " ".join(str(s or "").split())


# Words that turn a severity rule from a prescription into a prohibition. The current
# prompt QUOTES several mechanical floors in order to forbid them ("There is no rule of
# the form 'no seeds = MAJOR'…", "Did I avoid grading anything by checklist ('no seeds,
# therefore MAJOR')?"), so a flat substring ban would fail on exactly the text that fixes
# the problem. What must not exist is a floor stated as an instruction.
_NEGATORS = ("no rule", "not ", "never", "avoid", "do not", "don't", "isn't", "rather than",
             "instead", "forbid", "must not", "cannot", "checklist")


def _negated_everywhere(prompt: str, phrase: str, window: int = 200) -> bool:
    """True when `phrase` either does not appear at all, or appears only inside a
    negating context. Case-insensitive; `window` chars of preceding text are examined."""
    low, needle = prompt.lower(), phrase.lower()
    at = low.find(needle)
    while at != -1:
        before = low[max(0, at - window):at]
        if not any(neg in before for neg in _NEGATORS):
            return False
        at = low.find(needle, at + 1)
    return True


def _derive(**kw):
    base = dict(lens_severity="FATAL", verification_state="complete",
                calc_class="not_applicable", graded=False, evidence_class="cell_verified")
    return grading.derive(**{**base, **kw})


# ══════════════════════════════════════════════════════════════════════════════
# 1. Is there an issue? — finding / concern / question separation
# ══════════════════════════════════════════════════════════════════════════════
def test_the_candidate_taxonomy_is_four_way_and_non_collapsible():
    assert set(CANDIDATE_CLASSES) == {"CONFIRMED_FINDING", "PLAUSIBLE_CONCERN",
                                      "OPEN_QUESTION", "DISMISSED"}
    # Each bucket earns a strictly different ceiling, or the taxonomy is decoration.
    ceilings = [grading.CANDIDATE_CAP[c] for c in CANDIDATE_CLASSES]
    assert ceilings == ["FATAL", "MINOR", "NOTE", "NOTE"], ceilings


@pytest.mark.parametrize("klass,ceiling", [
    ("CONFIRMED_FINDING", "FATAL"), ("PLAUSIBLE_CONCERN", "MINOR"),
    ("OPEN_QUESTION", "NOTE"), ("DISMISSED", "NOTE")])
def test_a_suspicion_is_never_promoted_straight_to_a_confirmed_finding(klass, ceiling):
    """"Never promote a suspicion directly to a confirmed finding" is a CAP, not an
    instruction. A lens that sorted its own candidate below CONFIRMED_FINDING cannot have
    it counted as one, whatever severity it also asserted."""
    _, counted, _, _ = _derive(lens_severity="FATAL", candidate_class=klass)
    assert RANK[counted] <= RANK[ceiling], (klass, counted)


def test_an_unsorted_finding_is_not_silently_treated_as_confirmed():
    """A lens that never answered the question gets no cap from it — that is what
    `''` means — but the self-audit then reports the gap rather than passing over it."""
    _, counted, _, _ = _derive(lens_severity="FATAL", candidate_class="")
    assert counted == "FATAL", "an absent answer must not itself demote"
    res = selfaudit.audit(EvalReport(paper_id="p", findings=[Finding(
        finding_id="x", severity="FATAL", candidate_class="", finding_class="UNGRADED")]),
        report_stage.counted)
    assert "questions_separated_from_findings" in res.failed


def test_questions_are_reported_as_questions_and_count_toward_no_threshold():
    q = Finding(finding_id="q1", lens="protocol", severity="MAJOR", title="why not X?",
                candidate_class="OPEN_QUESTION", counted_severity="NOTE",
                finding_class="UNGRADED")
    assert report_stage.questions([q]) == [q]
    assert report_stage.overall_verdict([q])[0] == "GREEN", \
        "a question must not be able to move a verdict"


def test_a_strong_paper_with_only_questions_and_minor_notes_is_green():
    """"A strong paper may legitimately have zero MAJOR findings, several minor concerns
    and useful reviewer questions. That should be an acceptable outcome." — and it must
    not be reachable only by luck: twenty open questions from one lens stay GREEN."""
    fs = [Finding(finding_id=f"q{i}", lens="protocol", severity="MAJOR",
                  candidate_class="OPEN_QUESTION", counted_severity="NOTE")
          for i in range(20)]
    assert report_stage.overall_verdict(fs)[0] == "GREEN"


# ══════════════════════════════════════════════════════════════════════════════
# 2. How confident are we? — independent of severity, bounded by evidence
# ══════════════════════════════════════════════════════════════════════════════
def test_confidence_and_severity_are_independent_axes():
    """Every (severity, confidence) pair must be REPRESENTABLE — collapsing them is the
    failure this separation exists to prevent. What the derivation does with a given
    pair is a separate question, tested below."""
    for sev, conf in itertools.product(SEVERITIES, CONFIDENCES):
        f = Finding(severity=sev, confidence=conf)
        assert (f.severity, f.confidence) == (sev, conf)


def test_a_low_confidence_suspicion_cannot_casually_become_major():
    for sev in ("FATAL", "MAJOR"):
        _, counted, cap, _ = _derive(lens_severity=sev, lens_confidence="LOW")
        assert counted == "MINOR", (sev, counted, cap)


def test_a_low_confidence_minor_is_coherent_and_not_demoted_further():
    _, counted, _, _ = _derive(lens_severity="MINOR", lens_confidence="LOW")
    assert counted == "MINOR"


def test_evidence_type_bounds_confidence_and_never_severity_directly():
    """The §18 property. `derive` must reach a weak citation's ceiling THROUGH confidence,
    so the binding cap it names is `confidence` — never a cap keyed on evidence type."""
    _, _, cap, _ = _derive(lens_severity="FATAL", evidence_class="caption_verified")
    assert cap == "confidence", cap
    assert not any("evidence" in k for k in grading._CAP_ORDER), \
        "no cap may be keyed on evidence type; that is what made caption->NOTE arbitrary"


def test_corroborated_evidence_collectively_supports_a_stronger_finding():
    weak = grading.CONFIDENCE_RANK[grading.evidence_support("caption_verified")[0]]
    for extra in ({"calc_class": "recomputed_ok"}, {"grader_evidence_class": "cell_verified"}):
        lifted = grading.CONFIDENCE_RANK[
            grading.evidence_support("caption_verified", **extra)[0]]
        assert lifted > weak, extra


def test_the_evidence_ceiling_is_monotone_in_the_number_of_checks():
    """Adding an independent check can never LOWER the ceiling. Without this, a grader
    that cited something weak could silently weaken a finding the lens had cited well."""
    classes = list(grading.EVIDENCE_CONFIDENCE_CEILING)
    for ec in classes:
        base = grading.CONFIDENCE_RANK[grading.evidence_support(ec)[0]]
        for gc, cc in itertools.product(classes, ("not_applicable", "recomputed_ok")):
            got = grading.CONFIDENCE_RANK[grading.evidence_support(ec, gc, cc)[0]]
            assert got >= base, (ec, gc, cc, got, base)


def test_two_unverified_citations_do_not_manufacture_confidence():
    assert grading.evidence_support("unverified", "unverified")[0] == "LOW"


# ══════════════════════════════════════════════════════════════════════════════
# 3. How much does it matter? — severity non-escalation, self-consistency
# ══════════════════════════════════════════════════════════════════════════════
def test_severity_can_only_be_demoted_over_the_whole_reachable_input_space():
    """THE load-bearing property. Nothing the harness derives — not the grader, not the
    lens's own pass-B work, not the evidence ceiling, not any self-consistency cap — may
    raise a lens's asserted severity. Swept over every axis, not spot-checked."""
    axes = itertools.product(
        ("FATAL", "MAJOR", "MINOR", "NOTE"),          # lens_severity
        ("complete", "incomplete", "legacy"),          # verification_state
        ("recomputed_ok", "recomputed_mismatch", "not_applicable"),
        list(grading.EVIDENCE_CONFIDENCE_CEILING),     # evidence_class
        ("", *CANDIDATE_CLASSES),
        ("", *BASELINE_CLASSES),
        ("", *PRIOR_ART_BASES),
        ("", *CONFIDENCES),                            # lens_confidence
    )
    for sev, vs, cc, ec, cand, base, art, lc in axes:
        _, counted, cap, _ = grading.derive(
            lens_severity=sev, verification_state=vs, calc_class=cc, graded=False,
            evidence_class=ec, candidate_class=cand, baseline_class=base,
            prior_art_basis=art, lens_confidence=lc)
        assert RANK[counted] <= RANK[sev], (sev, vs, cc, ec, cand, base, art, lc, counted, cap)


def test_the_graded_path_can_only_demote_too():
    for sev, verdict, gsev, conf, ec, gec in itertools.product(
            ("FATAL", "MAJOR", "MINOR"), ("CONFIRMED", "PLAUSIBLE", "REFUTED", "INSUFFICIENT"),
            ("FATAL", "MAJOR", "MINOR", "NONE"), CONFIDENCES,
            ("cell_verified", "prose_verified"), ("cell_verified", "unverified")):
        _, counted, _, _ = grading.derive(
            lens_severity=sev, verification_state="complete", calc_class="not_applicable",
            graded=True, grade_verdict=verdict, grade_severity=gsev, confidence=conf,
            evidence_class=ec, grader_evidence_class=gec)
        assert RANK[counted] <= RANK[sev], (sev, verdict, gsev, conf, ec, gec, counted)


def test_a_missing_comparison_is_graded_by_what_its_absence_costs():
    """§10 — absence is easy to establish and says nothing on its own. Each class earns a
    strictly different ceiling, and the OPTIONAL end cannot reach a validity threat."""
    got = [grading.derive(lens_severity="FATAL", verification_state="complete",
                          calc_class="not_applicable", graded=False,
                          evidence_class="cell_verified", baseline_class=b)[1]
           for b in ("OPTIONAL_COMPARISON", "USEFUL_CONTROL",
                     "IMPORTANT_MISSING_BASELINE", "CENTRAL_VALIDITY_THREAT")]
    assert got == ["NOTE", "MINOR", "MAJOR", "FATAL"], got


def test_prior_art_is_separated_by_where_the_claim_comes_from():
    """§10 — the harness can re-verify a quote from THIS paper and cannot re-verify a
    reviewer's recollection of another one, so the two cannot earn the same weight."""
    internal = _derive(prior_art_basis="PAPER_INTERNAL")[1]
    external = _derive(prior_art_basis="EXTERNAL_VERIFIED")[1]
    recalled = _derive(prior_art_basis="REVIEWER_INFERENCE")[1]
    assert RANK[internal] > RANK[external] > RANK[recalled], (internal, external, recalled)


def test_a_fatal_needs_two_independent_checks():
    def _graded(**kw):
        return grading.derive(
            lens_severity="FATAL", verification_state="complete", graded=True,
            grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH",
            **{"calc_class": "not_applicable", "evidence_class": "cell_verified",
               "grader_evidence_class": "cell_verified", **kw})[1]
    assert _graded() == "FATAL", "two cells is the canonical pair"
    assert _graded(calc_class="recomputed_ok",
                   grader_evidence_class="prose_verified") == "FATAL", \
        "a machine-reproduced calculation is admissible as the second check"
    assert _graded(grader_evidence_class="prose_verified") == "MAJOR", \
        "one cell and one prose quote is not two independent checks"


def test_the_verdict_thresholds_are_unchanged_data():
    """Invariant #8, as a literal. This whole change adds refusals and derivations; it
    does not renegotiate the table."""
    assert (report_stage.RED_FATAL, report_stage.RED_MAJOR_ONE_LENS,
            report_stage.RED_MAJOR_TOTAL, report_stage.YELLOW_MAJOR,
            report_stage.YELLOW_MINOR) == (1, 3, 10, 1, 4)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Did we do the work? — falsification, alternatives, the self-audit
# ══════════════════════════════════════════════════════════════════════════════
def test_a_blank_falsification_is_capped_rather_than_believed():
    blank = {"schema_version": 2, "statement": "s"}
    assert grading.pass_b_state(blank, "MAJOR") == "incomplete"
    _, counted, _, _ = _derive(lens_severity="FATAL", verification_state="incomplete")
    assert counted == "NOTE"


def test_a_degenerate_alternative_interpretation_does_not_pass_as_one():
    """Echoing the statement back, or writing "n/a", is not an attempt at falsification.
    Checked structurally — the harness cannot judge whether an alternative is any GOOD,
    only that one was written and is not a restatement or a stoplisted placeholder."""
    echo = {"schema_version": 2, "statement": "x" * 60,
            "alternative_interpretation": "x" * 60, "why_alternative_fails": "y" * 60,
            "steelman": "z" * 60}
    assert grading.pass_b_state(echo, "MAJOR") == "incomplete"
    for placeholder in ("n/a", "none", "see above", ""):
        stub = {"schema_version": 2, "statement": "s",
                "alternative_interpretation": placeholder,
                "why_alternative_fails": "y" * 60, "steelman": "z" * 60}
        assert grading.pass_b_state(stub, "MAJOR") == "incomplete", placeholder


def test_a_grader_that_concedes_its_own_falsification_cannot_stay_confirmed():
    finding_class, counted, _, _ = grading.derive(
        lens_severity="FATAL", verification_state="complete", calc_class="not_applicable",
        graded=True, grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH",
        falsification_survived=False, evidence_class="cell_verified",
        grader_evidence_class="cell_verified")
    assert finding_class == "PLAUSIBLE_CONCERN" and counted == "MINOR"


def test_an_arithmetic_claim_the_harness_cannot_reproduce_is_capped():
    _, counted, _, _ = _derive(lens_severity="FATAL", calc_class="recomputed_mismatch")
    assert counted == "MINOR"


def test_every_self_audit_item_is_individually_reachable_as_a_failure():
    """A checklist whose items cannot fail is decoration. `harness.selfaudit`'s own
    self-check drives each one; this asserts the SET is complete and that nothing was
    added without a way to fail it."""
    complete = Finding(
        finding_id="a", severity="MAJOR", evidence_class="cell_verified",
        verified_observation="ok", verification_state="complete", calc_class="recomputed_ok",
        evidence_origin="PAPER_TABLE", candidate_class="CONFIRMED_FINDING",
        confidence="HIGH", severity_rationale="impact", steelman="defense",
        discrepancy_type="ARITHMETIC_ERROR",
        grade=Grade(verdict="CONFIRMED", reached_independently=True))
    clean = selfaudit.audit(
        EvalReport(paper_id="p", findings=[complete],
                   substantive_verdict=SubstantiveVerdict(verdict="STRONG")),
        report_stage.counted)
    assert clean.complete and len(clean.items) == 12, clean.summary
    assert {i.key for i in clean.items} == {
        "serious_findings_verified", "serious_findings_cell_backed",
        "numbers_independently_recomputed", "alternative_interpretation_tested",
        "authors_steelmanned", "questions_separated_from_findings",
        "confidence_separate_from_severity", "severity_argued_by_impact",
        "evidence_provenance_recorded", "graded_by_a_second_reader",
        "previous_conclusions_not_inherited", "whole_paper_judged_independently"}


def test_a_failed_self_audit_does_not_change_the_verdict():
    """The self-audit refuses to let a review call itself complete. It does not get a
    second path to a colour — that would make the threshold table advisory."""
    sloppy = Finding(finding_id="a", lens="protocol", severity="FATAL",
                     evidence_class="prose_verified", verification_state="legacy")
    rep = EvalReport(paper_id="p", findings=[sloppy])
    rep.self_audit = selfaudit.audit(rep, report_stage.counted)
    assert not rep.self_audit.complete and rep.self_audit.failed
    assert report_stage.overall_verdict(rep.findings)[0] == "RED", \
        "an unmet diligence check must neither soften nor harden the verdict"


def test_a_previous_readers_conclusion_is_not_inherited():
    """§17 — a grader that reports it worked from the first reader's argument rather than
    from the paper is not independent corroboration, and the self-audit says so."""
    inherited = Finding(finding_id="a", severity="MAJOR", evidence_class="cell_verified",
                        verified_observation="ok", verification_state="complete",
                        evidence_origin="PAPER_TABLE", candidate_class="CONFIRMED_FINDING",
                        confidence="HIGH", severity_rationale="r", steelman="s",
                        calc_class="not_applicable", discrepancy_type="NOT_APPLICABLE",
                        grade=Grade(verdict="CONFIRMED", reached_independently=False))
    res = selfaudit.audit(
        EvalReport(paper_id="p", findings=[inherited],
                   substantive_verdict=SubstantiveVerdict(verdict="STRONG")),
        report_stage.counted)
    assert "previous_conclusions_not_inherited" in res.failed


# ══════════════════════════════════════════════════════════════════════════════
# 5. Is the accounting honest? — corpus, failures, isolation
# ══════════════════════════════════════════════════════════════════════════════
def test_no_requested_paper_is_ever_silently_omitted():
    done = CaseState(paper_id="a", source="a.pdf", status="complete", verdict="GREEN")
    acct = corpus.account(["a.pdf", "b.pdf", "c.pdf"], [done])
    assert acct.requested == 3
    assert sum(acct.counts.values()) == 3, acct.counts
    assert {e.source for e in acct.entries} == {"a.pdf", "b.pdf", "c.pdf"}
    assert not acct.complete


def test_corpus_states_partition_the_requests():
    """The conservation law, over every reachable case status rather than a sample."""
    statuses = ("pending", "running", "waiting", "complete", "error")
    cases = [CaseState(paper_id=f"p{i}", source=f"p{i}.pdf", status=s)
             for i, s in enumerate(statuses)]
    acct = corpus.account([c.source for c in cases], list(cases))
    assert sum(acct.counts.values()) == len(cases)
    assert set(acct.counts) == set(CORPUS_STATES)
    assert all(e.state in CORPUS_STATES for e in acct.entries)


def test_two_requests_that_slugify_alike_are_both_still_listed():
    a = CaseState(paper_id="dup", source="one/x.pdf", status="complete", verdict="RED")
    b = CaseState(paper_id="dup", source="two/x.pdf", status="complete", verdict="GREEN")
    acct = corpus.account(["one/x.pdf", "two/x.pdf"], [a, b])
    assert acct.counts["completed"] == 2, "a paper_id-keyed summary collapses these"


def test_a_non_retryable_failure_does_not_consume_a_retry():
    for text in ("Error: Unauthorized. Please run `claude login`.",
                 "error: unknown option '--allowedTools'",
                 "'claude' is not recognized as an internal or external command",
                 "cannot open broken.pdf: damaged"):
        kind, policy, _ = failures.classify(text)
        assert policy == "never", (text, kind, policy)
        assert not failures.consumes_attempt(policy)


def test_a_retry_later_failure_does_not_consume_a_retry_either():
    for text in ("You've hit your session limit · resets 3:20pm",
                 "HTTP 429 Too Many Requests", "503 Service Unavailable"):
        kind, policy, _ = failures.classify(text)
        assert policy == "later" and not failures.consumes_attempt(policy), (text, kind)


def test_only_a_genuinely_transient_failure_spends_the_budget():
    for text in ("the request timed out", "ConnectionResetError: connection reset by peer",
                 "the reviewer printed no JSON at all", "a novel unclassified problem"):
        _, policy, _ = failures.classify(text)
        assert failures.consumes_attempt(policy), text


def test_classification_is_total_and_never_raises():
    for text in ("", None, "\x00\x01", "x" * 10_000, "429", "資源制限"):
        kind, policy, _ = failures.classify(text)
        assert kind in failures.FAILURE_KINDS and policy in failures.RETRY_POLICIES


def test_the_report_level_schema_version_reaches_the_non_degeneracy_check():
    """The integration bug the unit tests could not see. `prompts/audit._RETURN` puts
    `schema_version: 2` at the TOP LEVEL of the lens report, beside `findings`;
    `grading.pass_b_state` read it off the FINDING dict, found nothing on every real lens
    file, and returned `legacy` — which is deliberately uncapped — so the whole
    falsification/steelman non-degeneracy check was inert in production while passing its
    own unit tests, which construct the flag inside the finding."""
    doc = PaperDoc(paper_id="p", n_pages=4, sections=[
        Section(section_idx=0, title="Results", page_start=2, text="x" * 40 + "the quoted"
                                                                             " sentence lives here" + "y" * 40)])
    finding = {"finding_id": "a-01", "severity": "MAJOR", "statement": "a defect",
               "evidence_quote": "the quoted sentence lives here", "evidence_ref": "p2",
               "candidate_class": "CONFIRMED_FINDING", "confidence": "HIGH",
               "alternative_interpretation": "A" * 60, "why_alternative_fails": "B" * 60,
               "steelman": "C" * 60}
    with_version, _, _ = audit_stage._coerce(
        "overclaim", {"schema_version": 2, "findings": [finding]},
        audit_stage.source_units(doc), {}, doc.n_pages)
    assert with_version.findings, "the fixture's evidence must verify, or this proves nothing"
    assert with_version.findings[0].verification_state == "complete"

    without, _, _ = audit_stage._coerce(
        "overclaim", {"findings": [finding]},
        audit_stage.source_units(doc), {}, doc.n_pages)
    assert without.findings[0].verification_state == "legacy", \
        "a file with no contract version is legacy, and legacy is uncapped by design"

    # And a version-2 file with a BLANK triple is `incomplete`, i.e. the check now bites.
    stripped = {**finding, "alternative_interpretation": "", "why_alternative_fails": "",
                "steelman": ""}
    biting, _, _ = audit_stage._coerce(
        "overclaim", {"schema_version": 2, "findings": [stripped]},
        audit_stage.source_units(doc), {}, doc.n_pages)
    assert biting.findings[0].verification_state == "incomplete"
    assert biting.findings[0].counted_severity == "NOTE"


def test_a_batch_accounts_for_every_request_through_the_real_controller(tmp_path):
    """§19 end to end: one good paper, one absent file, one meaningless id. All three
    appear, each with the failure kind that stopped it, and the accounting is persisted."""
    cfg = Config(projects_dir=tmp_path / "projects")
    cfg.projects_dir.mkdir(parents=True)
    papers = [str(_PAPER), str(_PAPER.parent / "definitely_not_here.pdf"), "not-a-case-id"]
    res = controller.review_papers(cfg, papers, dossier_out=tmp_path / "reports",
                                   skip_probe=True)
    acct = res["corpus"]
    assert acct["requested"] == 3
    assert sum(acct["counts"].values()) == 3, acct["counts"]
    assert {e["source"] for e in acct["entries"]} == set(papers), "a request vanished"
    by_source = {e["source"]: e for e in acct["entries"]}
    assert by_source[papers[1]]["failure_kind"] == "extraction_failed"
    assert by_source[papers[2]]["failure_kind"] == "bad_invocation"
    assert not acct["complete"]
    persisted = json.loads(Path(res["corpus_path"]).read_text(encoding="utf-8"))
    assert persisted["requested"] == 3 and len(persisted["entries"]) == 3


@pytest.mark.parametrize("label,message,want_attempts", [
    # `attempts` is decremented back to 0 for a SPARED failure: `controller.step`
    # increments it unconditionally before the handler runs, so "did not consume an
    # attempt" is the handler undoing that increment. 3 == the full budget (retries=2).
    ("rate limit", "You've hit your session limit - resets 3:20pm (Asia/Kolkata)", 0),
    ("unknown flag", "error: unknown option '--allowedTools'", 0),
    ("unauthenticated", "Error: Unauthorized. Please run claude login.", 0),
    ("transient", "the request timed out while contacting the service", 3),
])
def test_only_a_transient_delegation_failure_spends_the_retry_budget(
        tmp_path, label, message, want_attempts):
    """§20 through the REAL controller, against a fake reviewer that fails each way.
    Before `harness/failures.py`, only the hand-carved rate-limit regex was spared and
    everything else burned all three attempts in seconds — which is how a paper ended up
    permanently `waiting` against a wall that needed an operator, not a retry."""
    script = tmp_path / "fake_reviewer.py"
    script.write_text(f"import sys\nsys.stderr.write({message!r})\nsys.exit(1)\n",
                      encoding="utf-8")
    cfg = Config(projects_dir=tmp_path / "projects", allow_auto_audit=True,
                 audit_cmd=f'"{sys.executable}" "{script}" "{{prompt}}" "{{out}}"',
                 audit_retries=2, audit_timeout_s=60)
    cfg.projects_dir.mkdir(parents=True)
    case = controller.drive(cfg, controller.open_case(cfg, str(_PAPER)),
                            auto_audit=True, skip_probe=True)
    assert case.attempts.get("audit", 0) == want_attempts, (label, case.attempts)
    assert case.failure_kind, f"{label}: the reason was not persisted onto the case"
    if want_attempts == 0:
        assert case.retry_policy in ("later", "never"), (label, case.retry_policy)


def test_a_rate_limit_records_when_it_is_worth_retrying(tmp_path):
    script = tmp_path / "limited.py"
    script.write_text("import sys\n"
                      "sys.stderr.write(\"session limit reached - resets 3:20pm (Asia/Kolkata)\")\n"
                      "sys.exit(1)\n", encoding="utf-8")
    cfg = Config(projects_dir=tmp_path / "projects", allow_auto_audit=True,
                 audit_cmd=f'"{sys.executable}" "{script}" "{{prompt}}" "{{out}}"',
                 audit_retries=2, audit_timeout_s=60)
    cfg.projects_dir.mkdir(parents=True)
    case = controller.drive(cfg, controller.open_case(cfg, str(_PAPER)),
                            auto_audit=True, skip_probe=True)
    assert case.failure_kind == "rate_limited" and case.retry_policy == "later"
    assert "3:20pm" in case.resume_after, case.resume_after
    assert case.status == "waiting", "the paper stays resumable, not terminally failed"


def test_a_lens_gets_the_minimum_filesystem_access_it_needs():
    """§21 — a lens may Read (to open the PDF that `SOURCE_FIDELITY` sends it to) and
    nothing else; only `overclaim` additionally searches for prior art. Nothing may
    write, and no lens is granted a tool it has no stated use for."""
    for lens, spec in audit_prompts.LENSES.items():
        assert spec["tools"], f"{lens} declares no tool policy at all"
        assert set(spec["tools"]) <= {"Read", "WebSearch"}, (lens, spec["tools"])
        assert not {"Write", "Edit", "Bash", "Glob", "Grep"} & set(spec["tools"]), lens
    assert audit_prompts.LENSES["overclaim"]["tools"] == ["Read", "WebSearch"]
    for lens in ("protocol", "confound", "contradiction"):
        assert audit_prompts.LENSES[lens]["tools"] == ["Read"], lens


def test_the_grader_is_blinded_from_everything_that_would_anchor_it():
    """The grader must not see the lens's severity, the lens's name, any other finding,
    or how findings are counted. Asserted over the BUILT prompt, with distinctive
    sentinels — a docstring promising blinding is not blinding."""
    prompt = grade_prompts.build(
        claim="SENTINEL_CLAIM", statement="SENTINEL_STATEMENT", target="SENTINEL_TARGET",
        reasoning="r", conclusion="c", counter_explanations=["ce"], evidence_quote="q",
        evidence_ref="T1:r0:c0", evidence_class="cell_verified",
        verified_observation="obs", sections_text="body", tables_text="cells",
        withheld_note="rest withheld")
    assert "SENTINEL_CLAIM" in prompt and "SENTINEL_STATEMENT" in prompt
    for leak in ("RED_MAJOR_ONE_LENS", "RED_MAJOR_TOTAL", "YELLOW_MINOR", "YELLOW_MAJOR",
                 "counted_severity", "severity_rationale",
                 "overclaim", "confound", "contradiction"):
        assert leak not in prompt, f"the grade prompt leaks {leak!r}"
    # FATAL/MAJOR DO appear — they are the grader's own vocabulary, since it assigns its
    # own severity. What must not appear is the LENS's assertion of one, and the prompt
    # cannot carry a value `build` was never given: `severity` is not a parameter.
    assert "severity" not in inspect.signature(grade_prompts.build).parameters
    # And the grader is told it does not know how its answer will be counted, which is
    # the part that makes the blinding matter rather than merely hold.
    assert "counted toward any verdict" in _flat(grade_prompts.INDEPENDENCE)
    # The driver records what it withheld, so a reader can check the claim.
    from harness import grade_driver
    withheld = inspect.getsource(grade_driver.fill)
    for field in ("severity", "severity_rationale", "lens", "other_findings",
                  "verdict_thresholds", "derivation_table"):
        assert f'"{field}"' in withheld, f"the driver does not record withholding {field!r}"


# ══════════════════════════════════════════════════════════════════════════════
# What the architecture CANNOT express — the anti-paper-specific guarantees
# ══════════════════════════════════════════════════════════════════════════════
def test_the_derivation_signature_admits_no_paper_specific_input():
    """"No standard deviation = MAJOR", "if epsilon=0.05 never flag", "if SAPG appears
    lower severity" must be INEXPRESSIBLE, not merely absent. `derive` takes vocabulary
    strings and booleans; there is no count, no number, no metric name, no identity it
    could branch on."""
    sig = inspect.signature(grading.derive)
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
    # `from __future__ import annotations` makes these strings, which is why this compares
    # against names rather than the types themselves.
    for name, p in sig.parameters.items():
        assert str(p.annotation) in ("str", "bool"), f"{name}: {p.annotation!r}"
    # Checked per underscore-separated SEGMENT rather than as a substring: "n" appears
    # inside "verification_state" and a substring test flags it, which would make this
    # assertion noise instead of a guarantee.
    banned = {"count", "n", "seeds", "seed", "delta", "value", "values", "epsilon",
              "metric", "paper", "paper_id", "threshold", "score", "num", "size"}
    segments = {seg for name in sig.parameters for seg in name.split("_")}
    assert not (segments & banned), segments & banned


def test_no_prompt_carries_a_mechanical_severity_floor():
    """The old CALIBRATION block encoded severity as floors — "if the paper reports NO
    variance at all for a headline claim, that is a MAJOR finding". These are the
    PRESCRIPTIVE forms; the current prompt quotes several such rules in order to forbid
    them, which is why the banned list is the imperative wording rather than any string
    containing "MAJOR"."""
    floors = ("that is a FATAL finding", "that is a MAJOR finding", "that is at least MAJOR",
              "therefore MAJOR", "automatically MAJOR", "grade it MAJOR", "must be MAJOR",
              "no seeds = MAJOR", "no variance = MAJOR", "no CI = MAJOR")
    for lens in audit_prompts.LENSES:
        prompt = _flat(audit_prompts.build(lens, "T", "s", "t", "c", "n"))
        for phrase in floors:
            assert _negated_everywhere(prompt, phrase), \
                f"{lens}: severity floor {phrase!r} appears PRESCRIPTIVELY"
        # And the forbidding half is actually present, or the absence above proves nothing.
        assert "There is no rule of the form" in prompt, lens
        assert "ABSENCE OF EVIDENCE IS NOT AUTOMATICALLY A FINDING" in prompt, lens
        assert "STATISTICAL WEAKNESS IS JUDGED, NOT PATTERN-MATCHED" in prompt, lens


def test_no_prompt_names_a_pilot_paper_or_a_pilot_value():
    """The pilot corpus is evaluation material, not source code rules. A prompt that
    named one would generalise to nothing."""
    names = ("SAPG", "FINCHAIN", "WeatherGen", "LDReg", "Sanchez", "sanchez24a",
             "apt-icml", "1241.66")
    for lens in audit_prompts.LENSES:
        prompt = audit_prompts.build(lens, "T", "s", "t", "c", "n")
        for name in names:
            assert name not in prompt, f"{lens}: paper-specific reference {name!r}"


def test_no_prompt_asks_for_a_particular_number_of_findings():
    for lens in audit_prompts.LENSES:
        prompt = _flat(audit_prompts.build(lens, "T", "s", "t", "c", "n"))
        assert "do not aim for any particular count" in prompt
        assert "may earn zero" in prompt
        for demand in ("find at least", "report at least", "identify at least",
                       "you must find", "aim for at least", "aim for three",
                       "at least one finding", "at least two findings"):
            assert demand not in prompt.lower(), f"{lens}: {demand!r}"


def test_every_lens_prompt_carries_the_full_reasoning_pipeline():
    """The blocks are the pipeline (see `harness/prompts/audit.py`'s docstring). A lens
    missing one is a lens reasoning differently from its three siblings."""
    for lens in audit_prompts.LENSES:
        prompt = audit_prompts.build(lens, "T", "s", "t", "c", "n")
        for name in ("SECURITY", "STANCE", "FIRST_PRINCIPLES", "TWO_PASS", "GRADING",
                     "RECOMPUTE", "CAUSAL", "SELECTION", "BASELINES", "SCOPE",
                     "SOURCE_FIDELITY", "PROVENANCE", "PRIOR_FINDINGS", "SELF_AUDIT"):
            assert getattr(audit_prompts, name) in prompt, f"{lens} is missing {name}"


def test_the_reviewer_is_told_it_is_a_researcher_not_a_prosecutor():
    """§16 — the stance is load-bearing enough to pin. An adversarial flaw detector and a
    skeptical scientist do not produce the same review."""
    stance = _flat(audit_prompts.STANCE)
    assert "not a prosecutor" in stance
    assert "quota of flaws" in stance
    assert "Do not assume the authors are wrong" in stance
    assert "zero findings above MINOR" in stance
    assert "must contain a major flaw" in stance
