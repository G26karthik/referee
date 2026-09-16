"""A recovered quotation is an entry condition, not a result — and nothing may say otherwise.

The soft-hyphen projection restored three candidate findings the evidence gate had been
dropping for a hyphen the typesetter inserted. The temptation in describing that is to say
"three real findings came back", and it is exactly wrong: quotation verification establishes
that a concern CITES THE PAPER ACCURATELY and nothing else. That is the distinction
`CITATION_VERIFIED` exists to hold, and the one invariant 20 says a review must never
collapse — every one of the shipped corpus's twenty paper-only "resolutions" was this
confusion, printed under `## What held up` beside the findings disputing it.

These tests keep the distinction out of the code and out of the report, not just out of
prose about them.
"""
from __future__ import annotations

import re
from pathlib import Path

from harness import taxonomy
from harness.artifacts import PaperDoc, Section, TargetOutcome
from harness.stages import audit as audit_stage

ROOT = Path(__file__).resolve().parents[1]

# A sentence broken across a line: the PDF holds "gener-" and "ation", so the flattened
# section text holds `gener-ation`, and a reader quoting it writes `generation`.
_BROKEN = ("This paper presents a unified diverse-weather LiDAR data diffusion gener- "
           "ation framework, significantly improving fidelity over prior work.")
_AS_READ = ("This paper presents a unified diverse-weather LiDAR data diffusion "
            "generation framework, significantly improving fidelity over prior work.")


def _doc() -> PaperDoc:
    return PaperDoc(paper_id="p", title="T", n_pages=1, sections=[
        Section(section_idx=0, title="Abstract", page_start=1, text=_BROKEN)])


def test_a_recovered_match_does_not_claim_to_be_verbatim():
    """Claiming verbatim there would be the false attestation `source_units` prevents."""
    units = audit_stage.source_units(_doc())
    klass, observation = audit_stage.verify_evidence(_AS_READ, "p1", units, {})
    assert klass == "prose_verified"
    assert "verbatim" not in observation, observation
    assert "hyphen" in observation and "NOT a character-for-character match" in observation


def test_an_exact_match_still_says_verbatim():
    """The exact search runs FIRST, so a character-for-character quote is never
    resolved through a normalisation and never loses the stronger word for it."""
    units = audit_stage.source_units(_doc())
    klass, observation = audit_stage.verify_evidence(_BROKEN, "p1", units, {})
    assert klass == "prose_verified"
    assert "occurs verbatim inside" in observation
    assert "hyphen" not in observation


def test_a_recovered_quotation_earns_no_privileged_evidence_class():
    """Recovery changes WHETHER the concern is kept, never what its evidence is worth.

    Both routes return the same `evidence_class`, so the confidence ceiling in
    `grading.EVIDENCE_CONFIDENCE_CEILING` — and therefore the severity cap above it —
    cannot differ between a quotation matched exactly and one matched after the hyphen.
    """
    units = audit_stage.source_units(_doc())
    exact, _ = audit_stage.verify_evidence(_BROKEN, "p1", units, {})
    recovered, _ = audit_stage.verify_evidence(_AS_READ, "p1", units, {})
    assert exact == recovered == "prose_verified"


def test_verifying_a_quotation_resolves_nothing_about_the_concern():
    """The state machine, restated where the restored findings land.

    A restored candidate is subject to every stage that follows — the evidence ceiling, the
    candidate cap, blinded grading, the materiality table — and re-verifying its quotation
    settles none of them.
    """
    assert taxonomy.evidence_state("CITATION_VERIFIED_ONLY") == "CITATION_VERIFIED"
    assert taxonomy.resolution_state("CITATION_VERIFIED") == "UNRESOLVED"
    assert "CITATION_VERIFIED" not in taxonomy.EVIDENCE_ABOUT_THE_PAPER
    out = TargetOutcome(target_id="t1", disposition="CITATION_VERIFIED_ONLY",
                        provenance="paper")
    assert out.establishes_failure is False
    assert out.resolution_state == "UNRESOLVED"


def test_no_shipped_prose_calls_a_restored_candidate_a_real_finding():
    """The wording guard, over the documents and the source a reader actually receives.

    A restored candidate is a CANDIDATE. Saying "three real findings came back" would
    assert a scientific result that quotation verification does not establish, and that
    sentence is the one this whole file exists to keep out of the report.
    """
    # QUOTED OCCURRENCES DO NOT COUNT. A document that quotes the wording in order to
    # forbid it is doing the opposite of asserting it, and a guard that could not tell
    # those apart would forbid the correction from explaining itself.
    banned = re.compile(r"real findings? (?:came back|were recovered|returned)", re.I)
    newline = chr(10)
    checked = 0
    for path in [*(ROOT / "docs").glob("*.md"), ROOT / "CLAUDE.md",
                 *(ROOT / "harness").rglob("*.py")]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for hit in banned.finditer(text):
            line_start = text.rfind(newline, 0, hit.start()) + 1
            line_end = text.find(newline, hit.end())
            line = text[line_start:line_end if line_end > 0 else len(text)]
            quoted = line.count(chr(34)) >= 2 or chr(8220) in line
            assert quoted, f"{path.name} asserts it, unquoted: {line.strip()[:100]}"
        checked += 1
    assert checked > 20, "the guard must actually be reading the repository"


def test_the_report_renderer_cannot_call_a_citation_check_a_resolution():
    """`stages/report` renders from the derived axes, so the distinction is structural."""
    from harness.stages import report as report_stage
    src = (ROOT / "harness" / "stages" / "report.py").read_text(encoding="utf-8")
    # No literal remapping of the citation state onto a resolution anywhere in the renderer.
    assert "CITATION_VERIFIED_ONLY\": \"PAPER_ONLY_RESOLVED" not in src
    assert hasattr(report_stage, "counted")
