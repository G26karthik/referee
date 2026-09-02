"""Provenance, at both boundaries where a number can go wrong.

S1 is now deterministic, so a reported number IS a table cell or IS a whole sentence
— there is no extraction step to hallucinate in. The remaining risk moved to S2,
where the driver (a language model) writes findings. `audit._substantiated` is the
gate that keeps an unquotable finding out of the report.
"""
from __future__ import annotations

import pytest

from harness import pdf
from harness.artifacts import Section, Table
from harness.stages.audit import _coerce, _flat, _substantiated
from harness.stages.ingest import paper_id_for

TABLES = [Table(table_idx=0, page=4, caption="Table 1: results",
                header=["Arm", "Acc", "Rank-drop"],
                rows=[["baseline", "71.2", "12.12"], ["ours", "75.4 ± 0.3", "9.83"]])]
BY_IDX = {t.table_idx: t for t in TABLES}
CORPUS = _flat("We observe a 4.2% improvement over the ResNet-18 baseline on CIFAR-100. "
               "The model was trained for 20 tasks. Accuracy increased by 1.3 pp.")


# --------------------------------------------------------------------------- #
# S1 — deterministic number extraction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cell,expected", [
    ("71.2", True), ("75.4 ± 0.3", True), ("-1.25", True), ("9.83%", True), ("+2 pp", True),
    ("baseline", False), ("20 tasks", False), ("", False), ("ResNet-18", False),
])
def test_numeric_cell_detection(cell, expected):
    assert pdf.is_numeric_cell(cell) is expected


def test_table_numbers_address_every_numeric_cell():
    nums = pdf.table_numbers(TABLES)
    refs = {n.table_ref: n for n in nums}
    assert set(refs) == {"T0:r0:c1", "T0:r0:c2", "T0:r1:c1", "T0:r1:c2"}, refs.keys()
    assert refs["T0:r1:c1"].value == "75.4 ± 0.3"
    assert refs["T0:r1:c1"].method == "ours", "column 0 is the arm label"
    assert refs["T0:r1:c1"].metric == "Acc", "the header row names the metric"
    assert refs["T0:r1:c1"].seeds_or_variance == "0.3", "± spread must be captured"
    assert refs["T0:r0:c1"].seeds_or_variance == "", "no spread means no spread"


def test_table_number_value_is_the_cell_verbatim():
    for n in pdf.table_numbers(TABLES):
        assert n.source_quote == n.value, "the quote IS the cell; nothing is reformatted"


def test_column_zero_is_never_treated_as_a_measurement():
    t = [Table(table_idx=0, rows=[["12.5", "0.9"]])]      # numeric row label
    assert all(n.table_ref != "T0:r0:c0" for n in pdf.table_numbers(t))


def test_prose_numbers_need_comparative_language():
    secs = [Section(section_idx=0, title="Results", page_start=3,
                    text="We observe a 4.2% improvement over the baseline. "
                         "The model was trained for 20 tasks.")]
    nums = pdf.prose_numbers(secs)
    assert len(nums) == 1, "a bare count is not a claim"
    assert nums[0].value == "4.2%"
    assert "improvement over the baseline" in nums[0].source_quote
    assert nums[0].page == 3


def test_prose_numbers_respect_the_cap():
    text = " ".join(["Accuracy improved by 1.0%."] * 50)
    secs = [Section(section_idx=0, text=text)]
    assert len(pdf.prose_numbers(secs, limit=7)) == 7


def test_paper_id_is_a_stable_slug():
    assert paper_id_for("papers/paper4_snri_nullresult.pdf") == "paper4-snri-nullresult"


# --------------------------------------------------------------------------- #
# S2 — the harness does not trust the driver
# --------------------------------------------------------------------------- #
def test_quote_present_in_the_prose_is_substantiated():
    assert _substantiated("a 4.2% improvement over the ResNet-18 baseline", "p3", CORPUS, BY_IDX)


def test_invented_quote_is_rejected():
    assert not _substantiated("the model achieves state of the art", "p3", CORPUS, BY_IDX)


def test_a_prose_quote_with_no_reference_is_no_longer_substantiated():
    """Tightened contract: `evidence_ref` is required, not optional.

    These two cases previously passed an empty ref and relied on the prose branch. That
    was the hole a lost reference fell through — see tests/test_caption_and_evidence_ref.py.
    The quote below is genuinely in the corpus and must still be refused for want of a
    location.
    """
    assert not _substantiated("a 4.2% improvement over the ResNet-18 baseline", "", CORPUS, BY_IDX)


def test_cell_citation_must_match_the_real_cell():
    assert _substantiated("75.4 ± 0.3", "T0:r1:c1", CORPUS, BY_IDX)
    assert not _substantiated("99.9", "T0:r1:c1", CORPUS, BY_IDX), "wrong value for that cell"
    assert not _substantiated("75.4 ± 0.3", "T0:r0:c1", CORPUS, BY_IDX), "right value, wrong cell"
    assert not _substantiated("75.4 ± 0.3", "T9:r0:c0", CORPUS, BY_IDX), "no such table"


def test_cell_match_ignores_whitespace_and_case():
    assert _substantiated("  75.4  ±  0.3 ", "T0:r1:c1", CORPUS, BY_IDX)


def test_too_short_prose_quotes_are_rejected():
    assert not _substantiated("we", "", CORPUS, BY_IDX)
    assert not _substantiated("", "", CORPUS, BY_IDX)


def test_short_cell_values_are_accepted_because_the_address_pins_them():
    """A 4-char cell value is the most checkable evidence there is; the prose length
    floor must not throw away exactly the table numbers this harness audits."""
    tables = {0: Table(table_idx=0, rows=[["arm", "0.0001"]])}
    assert _substantiated("0.0001", "T0:r0:c1", CORPUS, tables)
    assert not _substantiated("0.0009", "T0:r0:c1", CORPUS, tables)


def finding(**kw) -> dict:
    return {"severity": "MAJOR", "statement": "a real defect",
            "evidence_quote": "75.4 ± 0.3", "evidence_ref": "T0:r1:c1", **kw}


def test_coerce_keeps_substantiated_findings_and_counts_the_rest():
    data = {"findings": [finding(),
                         finding(evidence_quote="99.9"),          # cell mismatch
                         finding(statement=" "),                  # no statement
                         "not a dict"],
            "unasked_question": "why no baseline?", "notes": "checked cells"}
    report, dropped = _coerce("contradiction", data, CORPUS, BY_IDX)
    assert len(report.findings) == 1 and dropped == 3
    assert report.findings[0].lens == "contradiction"
    assert report.unasked_question == "why no baseline?"


def test_coerce_assigns_ids_and_clamps_unknown_severity():
    report, _ = _coerce("protocol", {"findings": [finding(severity="CATASTROPHIC")]},
                        CORPUS, BY_IDX)
    assert report.findings[0].finding_id == "protocol-01"
    assert report.findings[0].severity == "MINOR", "unknown severity must not inflate"


def test_coerce_survives_garbage_input():
    for junk in (None, [], "text", {"findings": "nope"}):
        report, _ = _coerce("protocol", junk, CORPUS, BY_IDX)
        assert report.findings == []


# --------------------------------------------------------------------------- #
# pdf structure helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("line", [
    "Abstract", "1 Introduction", "3.2 Main result", "5 Limitations",
    "References", "A Appendix", "4. Discussion", "B.2 Dataset details",
])
def test_headings_are_recognised(line):
    assert pdf.is_heading(line)


@pytest.mark.parametrize("line", [
    "We observe a 4.2% improvement over the baseline on CIFAR-100 across five seeds.",
    "",
    "the disruption in Table 2 accrues over subsequent steps, once weights grow back",
    "x" * 200,
])
def test_prose_is_not_mistaken_for_a_heading(line):
    assert not pdf.is_heading(line)


def test_sections_never_drop_front_matter():
    secs = pdf.split_sections(["Title Line And Authors\n", "1 Introduction\nbody"])
    assert secs[0].title == "", "text before the first heading must survive as a section"
    assert "Title Line And Authors" in secs[0].text


def test_grid_snaps_ragged_rows_onto_shared_columns():
    rows = [[(50.0, "Arm Effective-rank"), (200.0, "Rank-drop")],
            [(50.0, "vanilla"), (200.0, "12.12"), (300.0, "10.23")],
            [(50.0, "ours"), (200.0, "9.83"), (300.0, "9.06")]]
    grid = pdf._grid(rows)
    assert len(grid) == 3 and all(len(r) == 3 for r in grid), grid
    assert grid[1] == ["vanilla", "12.12", "10.23"]
    assert grid[0][2] == "", "a row with no cell at an anchor gets a blank, not a shift"


def test_render_tables_emits_addressable_cells():
    out = pdf.render_tables(TABLES)
    assert "T0:r1:c1=75.4 ± 0.3" in out and "page 4" in out


def test_render_sections_gives_every_section_a_slice():
    secs = pdf.split_sections(["1 Introduction\n" + "a" * 5000,
                               "5 Limitations\n" + "b" * 5000])
    out = pdf.render_sections(secs, budget_chars=1200)
    assert "Introduction" in out and "Limitations" in out, "the tail must not be dropped"
    assert "…[truncated]" in out
