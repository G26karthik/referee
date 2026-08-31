"""The deterministic half of S4: ranking, the verdict rule, and the pure renderer.

These are the functions that must NOT involve a model. If an LLM ever starts
deciding severity or the verdict, these tests are what should start failing.
"""
from __future__ import annotations

import re

import pytest

from harness.artifacts import EvalReport, Finding, LensReport
from harness.stages.report import (
    finding_key,
    overall_verdict,
    parse_magnitude,
    pick_unasked_question,
    rank,
    render_eval_report,
)


def f(fid: str, lens: str = "protocol", severity: str = "MINOR", ref: str = "",
      verifiable: bool = False, **kw) -> Finding:
    base = dict(finding_id=fid, lens=lens, severity=severity, title=f"{fid} title",
                statement="a statement", evidence_quote="a quote", evidence_ref=ref,
                verifiable_by_experiment=verifiable)
    return Finding(**{**base, **kw})


# --------------------------------------------------------------------------- #
# parse_magnitude — salvaged from the retired tournament; must never raise
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("+3-5% top-1", 3.0),
    ("0.8pp", 0.8),
    ("~2.5 points", 2.5),
    ("-1.25", 1.25),
    ("std 1.2", 1.2),
    ("negligible", 0.0),
    ("", 0.0),
    (None, 0.0),
])
def test_parse_magnitude_degrades_never_raises(text, expected):
    assert parse_magnitude(text) == expected


# --------------------------------------------------------------------------- #
# verdict rule
# --------------------------------------------------------------------------- #
def test_one_fatal_is_red():
    assert overall_verdict([f("a", severity="FATAL")])[0] == "RED"


def test_three_majors_from_one_lens_is_red():
    """One dimension failing repeatedly is a pattern, not isolated weaknesses."""
    same = [f(str(i), lens="protocol", severity="MAJOR") for i in range(3)]
    assert overall_verdict(same)[0] == "RED"
    assert "protocol" in overall_verdict(same)[1]


def test_majors_spread_across_lenses_stay_yellow():
    """Calibration regression. A four-lens panel returns ~2 MAJORs per lens on a GOOD
    paper; a real run on a pre-registered null result produced 8 MAJORs across 4 lenses
    with no FATAL, and the earlier rule called that RED. It is a YELLOW."""
    spread = [f(f"{lens}-{i}", lens=lens, severity="MAJOR")
              for lens in ("overclaim", "protocol", "confound", "contradiction")
              for i in range(2)]
    assert len(spread) == 8
    verdict, reason = overall_verdict(spread)
    assert verdict == "YELLOW", reason
    assert "no" in reason.lower() and "fatal" in reason.lower()


def test_an_overwhelming_total_is_still_red():
    many = [f(f"{lens}-{i}", lens=lens, severity="MAJOR")
            for lens in ("overclaim", "protocol", "confound", "contradiction", "x")
            for i in range(2)]
    assert overall_verdict(many)[0] == "RED"


def test_two_majors_in_one_lens_is_yellow():
    assert overall_verdict([f(str(i), lens="protocol", severity="MAJOR") for i in range(2)])[0] == "YELLOW"


def test_minors_accumulate_to_yellow_at_four():
    assert overall_verdict([f(str(i)) for i in range(3)])[0] == "GREEN"
    assert overall_verdict([f(str(i)) for i in range(4)])[0] == "YELLOW"


def test_no_findings_is_green():
    verdict, reason = overall_verdict([])
    assert verdict == "GREEN" and reason


def test_verdict_always_explains_itself():
    for findings in ([], [f("a")], [f("a", severity="MAJOR")], [f("a", severity="FATAL")]):
        assert overall_verdict(findings)[1].strip()


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #
def test_severity_dominates_every_other_term():
    # the MINOR has the strongest evidence, is verifiable, and is the top-priority
    # lens — severity must still put it last.
    order = rank([
        f("minor", lens="overclaim", severity="MINOR", ref="T0:r0:c0", verifiable=True),
        f("fatal", lens="protocol", severity="FATAL"),
    ])
    assert [x.finding_id for x in order] == ["fatal", "minor"]


def test_cell_evidence_outranks_page_outranks_none_at_equal_severity():
    order = rank([f("none", severity="MAJOR"),
                  f("page", severity="MAJOR", ref="p3"),
                  f("cell", severity="MAJOR", ref="T1:r2:c3")])
    assert [x.finding_id for x in order] == ["cell", "page", "none"]


def test_ranking_is_total_and_idempotent():
    findings = [f("b", severity="MAJOR"), f("a", severity="MAJOR"), f("c")]
    once = rank(findings)
    assert rank(once) == once
    assert rank(list(reversed(findings))) == once, "sort must not depend on input order"


def test_verifiable_breaks_ties_before_lens():
    order = rank([f("plain", lens="overclaim", severity="MAJOR", ref="p1"),
                  f("verif", lens="protocol", severity="MAJOR", ref="p1", verifiable=True)])
    assert [x.finding_id for x in order] == ["verif", "plain"]


def test_finding_key_is_a_plain_tuple():
    assert isinstance(finding_key(f("a")), tuple)


# --------------------------------------------------------------------------- #
# unasked question — deterministic pick, verbatim
# --------------------------------------------------------------------------- #
def test_unasked_question_follows_lens_priority():
    reports = [LensReport(lens="protocol", unasked_question="protocol asks this"),
               LensReport(lens="overclaim", unasked_question="overclaim asks this")]
    assert pick_unasked_question(reports) == "overclaim asks this"


def test_unasked_question_skips_empty_lenses():
    reports = [LensReport(lens="overclaim", unasked_question="   "),
               LensReport(lens="confound", unasked_question="confound asks this")]
    assert pick_unasked_question(reports) == "confound asks this"
    assert pick_unasked_question([LensReport(lens="overclaim")]) == ""


# --------------------------------------------------------------------------- #
# renderer — pure, copies verbatim, never breaks the markdown table
# --------------------------------------------------------------------------- #
def _report(**kw) -> EvalReport:
    base = dict(paper_id="pid", title="A Title", verdict="RED", verdict_reason="because",
                findings=[f("a", severity="FATAL", ref="T0:r1:c2")],
                unasked_question="why was the obvious baseline not run?",
                lenses_run=["overclaim", "protocol"])
    return EvalReport(**{**base, **kw})


def test_render_copies_quote_and_question_verbatim():
    quote = "we observe a 4.2% improvement over the baseline"
    md = render_eval_report(_report(findings=[f("a", severity="FATAL", evidence_quote=quote)]))
    assert quote in md
    assert "why was the obvious baseline not run?" in md


def test_render_shows_the_verdict_badge_and_reason():
    md = render_eval_report(_report())
    assert "🔴 REJECT / RED FLAG" in md and "because" in md
    assert "🟢" in render_eval_report(_report(verdict="GREEN", findings=[]))


def test_render_escapes_pipes_so_the_table_survives():
    md = render_eval_report(_report(findings=[f("a", severity="MAJOR", title="x | y | z")]))
    header = next(line for line in md.splitlines() if line.startswith("| Severity"))
    row = next(line for line in md.splitlines() if line.startswith("| MAJOR"))
    # Only UNescaped pipes are cell delimiters; the ones from the title must be escaped.
    delimiters = lambda s: len(re.findall(r"(?<!\\)\|", s))
    assert delimiters(row) == delimiters(header), "a raw pipe in a title broke the table"
    assert r"x \| y \| z" in row


def test_render_states_truncation_instead_of_hiding_it():
    many = [f(f"f{i:02d}", severity="MAJOR", ref="p1") for i in range(30)]
    md = render_eval_report(_report(findings=many))
    assert "further finding(s) omitted" in md
    assert "further" in md and "FATAL/MAJOR finding(s)" in md


def test_render_handles_zero_findings():
    md = render_eval_report(_report(verdict="GREEN", verdict_reason="clean", findings=[],
                                    unasked_question=""))
    assert "No finding rises above MINOR" in md
    assert "No findings returned" in md
    assert md.endswith("\n")


def test_render_is_pure_and_deterministic():
    r = _report()
    assert render_eval_report(r) == render_eval_report(r)
