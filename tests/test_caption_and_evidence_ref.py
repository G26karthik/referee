"""Regressions for two defects found while auditing real ICML papers.

1. Table captions BELOW their bodies (`harness/pdf.py`). The unruled-table recovery
   anchored its scan on a caption and read forward, which silently assumed captions
   precede tables. On a page laid out the other way it captured the NEXT table's rows
   under each caption and dropped the first table entirely, shifting every label after
   it — so a cell citation named the wrong table while still verifying.

2. A finding that loses `evidence_ref` (`harness/stages/audit.py`). An absent reference
   fell through to the prose branch and was re-checked as a free-floating substring
   against the whole paper, which the text of a table cell generally satisfies. The
   finding survived with its provenance deleted rather than being dropped and counted.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from harness.audit_driver import AuditDriverError, parse_lens_json
from harness.pdf import _body_runs, _caption_side, _pair
from harness.stages.audit import _flat, _substantiated


# --------------------------------------------------------------------------- #
# 1. caption/body association
# --------------------------------------------------------------------------- #
# Three tables on one page with captions BELOW each body — the sanchez24a-icml layout,
# with its real page-26 geometry. A caption is set tight under its own table (~14pt) and
# separated from the next table by a full inter-float gap (~90pt); that asymmetry is the
# only thing that distinguishes the two layouts, and row indices discard it.
BELOW_GAPPED = [True, True, False, True, True, False, True, True, False]
BELOW_CAPTION = [False, False, True, False, False, True, False, False, True]
BELOW_SPAN = [(110.0, 120.0), (144.0, 214.0), (228.0, 238.0),      # body A, caption 7
              (328.0, 338.0), (362.0, 432.0), (447.0, 457.0),      # body B, caption 8
              (547.0, 557.0), (581.0, 651.0), (666.0, 676.0)]      # body C, caption 9

# The same three tables with captions ABOVE, the more common layout: the caption now
# hugs the table that FOLLOWS it, and the wide gap falls after each table instead.
ABOVE_GAPPED = [False, True, True, False, True, True, False, True, True]
ABOVE_CAPTION = [True, False, False, True, False, False, True, False, False]
ABOVE_SPAN = [(110.0, 120.0), (134.0, 144.0), (148.0, 214.0),      # caption 7, body A
              (304.0, 314.0), (328.0, 338.0), (342.0, 432.0),      # caption 8, body B
              (523.0, 533.0), (547.0, 557.0), (561.0, 651.0)]      # caption 9, body C

CAPTIONS = ["Table 7: A", "Table 8: B", "Table 9: C"]


def _spans(runs, is_caption, span):
    """(body spans, caption spans) in the shape `_pair` consumes."""
    return ([(span[s][0], span[e - 1][1]) for s, e in runs],
            [span[i] for i, c in enumerate(is_caption) if c])


def test_bodies_are_found_without_reference_to_captions():
    """All three bodies are recovered under either layout — none is swallowed."""
    assert _body_runs(BELOW_GAPPED, BELOW_CAPTION) == [(0, 2), (3, 5), (6, 8)]
    assert _body_runs(ABOVE_GAPPED, ABOVE_CAPTION) == [(1, 3), (4, 6), (7, 9)]


def test_caption_side_is_detected_not_assumed():
    runs, caps = _spans(_body_runs(BELOW_GAPPED, BELOW_CAPTION), BELOW_CAPTION, BELOW_SPAN)
    assert _caption_side(runs, caps) == "below"
    runs, caps = _spans(_body_runs(ABOVE_GAPPED, ABOVE_CAPTION), ABOVE_CAPTION, ABOVE_SPAN)
    assert _caption_side(runs, caps) == "above"


def test_captions_below_pair_with_the_body_above_them():
    """The regression itself: with captions below, label i must stay on body i."""
    runs, caps = _spans(_body_runs(BELOW_GAPPED, BELOW_CAPTION), BELOW_CAPTION, BELOW_SPAN)
    assert _pair(runs, caps, CAPTIONS) == CAPTIONS


def test_captions_above_still_pair_correctly():
    """The previously-working layout must not regress."""
    runs, caps = _spans(_body_runs(ABOVE_GAPPED, ABOVE_CAPTION), ABOVE_CAPTION, ABOVE_SPAN)
    assert _pair(runs, caps, CAPTIONS) == CAPTIONS


def test_an_uncaptioned_body_is_left_unlabelled_rather_than_stealing_a_neighbours():
    """A table whose caption is on the previous page must not cascade a shift.

    Two bodies, one caption below the SECOND one. The first body has no caption of its
    own; it must come back blank, and the caption must land on the body it names.
    """
    gapped = [True, True, False, True, True, False]
    caption = [False, False, False, False, False, True]
    span = [(110.0, 150.0), (150.0, 214.0), (220.0, 226.0),
            (362.0, 400.0), (400.0, 432.0), (447.0, 457.0)]
    runs = _body_runs(gapped, caption)
    assert runs == [(0, 2), (3, 5)]
    body_spans = [(span[s][0], span[e - 1][1]) for s, e in runs]
    assert _pair(body_spans, [span[5]], ["Table 8: B"]) == ["", "Table 8: B"]


def test_each_caption_is_used_at_most_once():
    runs, caps = _spans(_body_runs(BELOW_GAPPED, BELOW_CAPTION), BELOW_CAPTION, BELOW_SPAN)
    labels = _pair(runs, caps, CAPTIONS)
    assert len(labels) == len(set(labels)), "a caption must not be attached to two bodies"


# --------------------------------------------------------------------------- #
# 2. evidence_ref integrity
# --------------------------------------------------------------------------- #
class _FakeTable:
    def __init__(self, value: str) -> None:
        self.value = value

    def cell(self, r: int, c: int) -> str:
        return self.value


CORPUS = _flat("the table reports 12.196 ± 0.207 for arm B on this benchmark")
BY_IDX = {1: _FakeTable("12.196 ± 0.207")}


def test_a_correct_cell_citation_verifies():
    assert _substantiated("12.196 ± 0.207", "T1:r0:c1", CORPUS, BY_IDX) is True


def test_a_correct_page_citation_verifies():
    assert _substantiated("12.196 ± 0.207 for arm B", "p4", CORPUS, BY_IDX) is True


@pytest.mark.parametrize("ref", ["", "   ", "figure 3", "section 5", "Table 7", "7"])
def test_a_finding_without_a_wellformed_ref_is_not_substantiated(ref: str):
    """The core regression: a lost reference must DROP the finding, not downgrade it.

    Every quote here is genuinely present in the corpus, so before the fix each of these
    passed through the prose branch and kept the finding alive with no locatable
    provenance at all.
    """
    assert _substantiated("12.196 ± 0.207", ref, CORPUS, BY_IDX) is False


def test_a_cell_citation_that_names_the_wrong_cell_still_fails():
    assert _substantiated("99.9 ± 0.1", "T1:r0:c1", CORPUS, BY_IDX) is False


def test_a_cell_citation_to_a_missing_table_still_fails():
    assert _substantiated("12.196 ± 0.207", "T9:r0:c1", CORPUS, BY_IDX) is False


def test_load_reports_drops_and_counts_a_ref_less_finding(tmp_path):
    """End to end through the real loader: the drop is counted, not silent."""
    from harness import state
    from harness.artifacts import PaperDoc, Section
    from harness.config import Config
    from harness.stages import audit as audit_stage

    cfg = Config(projects_dir=tmp_path)
    pid = "demo"
    doc = PaperDoc(paper_id=pid, title="Demo", sections=[Section(section_idx=0, title="Results",
                                                   text="the table reports 12.196 ± 0.207 for arm B")])
    audit_dir = tmp_path / pid / "audit"
    audit_dir.mkdir(parents=True)
    state.write_json(audit_dir / "overclaim.json", {
        "lens": "overclaim",
        "findings": [
            {"finding_id": "keep", "severity": "MAJOR", "statement": "s",
             "evidence_quote": "12.196 ± 0.207 for arm B", "evidence_ref": "p1"},
            {"finding_id": "lost-its-ref", "severity": "MAJOR", "statement": "s",
             "evidence_quote": "12.196 ± 0.207 for arm B", "evidence_ref": ""},
        ],
    })
    reports, dropped = audit_stage.load_reports(cfg, pid, doc)
    kept = [f.finding_id for r in reports for f in r.findings]
    assert kept == ["keep"]
    assert dropped == 1, "the finding whose reference was lost must be counted as dropped"


def test_audit_driver_refuses_a_lens_whose_findings_lost_their_refs():
    """The write path must not normalise an unlocatable finding into a valid-looking file."""
    body = ('{"lens":"overclaim","findings":[{"finding_id":"o-1","severity":"MAJOR",'
            '"statement":"s","evidence_quote":"a real quote from the paper"}],'
            '"unasked_question":"","notes":"n"}')
    with pytest.raises(AuditDriverError) as e:
        parse_lens_json(body, "overclaim")
    assert "evidence_ref" in str(e.value)


def test_audit_driver_still_accepts_a_finding_with_no_quote_and_no_ref():
    """Only a finding that CLAIMS evidence needs a location for it."""
    body = ('{"lens":"overclaim","findings":[{"finding_id":"o-1","severity":"MINOR",'
            '"statement":"a purely structural observation"}],'
            '"unasked_question":"","notes":"n"}')
    assert len(parse_lens_json(body, "overclaim").findings) == 1


# --------------------------------------------------------------------------- #
# 3. end-to-end on the PDF that exposed the bug
# --------------------------------------------------------------------------- #
def test_captions_below_on_the_real_pdf_that_exposed_this():
    """Page 26 of sanchez24a carries three tables with captions set BELOW each body.

    Pinned to the source PDF rather than to a fixture, because the failure was a
    disagreement between what the page looks like and what the parser assumed. Table 7
    must hold 11.0% and Tables 8 and 9 must hold 19.5% — that Tables 8 and 9 are
    identical is a property of the paper, not of this parser, and the test asserts it so
    that a future change cannot quietly "fix" the duplication by mislabelling again.
    """
    pdfplumber = pytest.importorskip("pdfplumber")
    paper = Path(__file__).resolve().parent.parent / "papers" / "sanchez24a_ICML.pdf"
    if not paper.exists():
        pytest.skip("papers/sanchez24a_ICML.pdf is not present in this checkout")

    from harness.pdf import _page_captions, _unruled_tables, page_texts

    pages = page_texts(str(paper))
    with pdfplumber.open(str(paper)) as pdf:
        found = _unruled_tables(pdf.pages[25], _page_captions(pages[25]))

    labels = [c for c, _ in found]
    assert labels == ["Table 7: CodeGen-350M-mono results",
                      "Table 8: CodeGen-2B-mono results",
                      "Table 9: CodeGen-6B-mono results"]
    first = [g[2][1] for _, g in found]      # gamma=1.0, temperature=0.2, k=1
    assert first == ["11.0%", "19.5%", "19.5%"], (
        "Table 7 must not be swallowed and its label must not shift onto Table 8")
