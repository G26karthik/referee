"""Figures and equations as citable-but-capped evidence (Stage 5).

Governing rule, stated in `harness.artifacts.PaperDoc`'s docstring: PDF inspection can
KILL a candidate or point at the right unit to cite; it can never BE the evidence. What
makes that true in code is narrower than it sounds — a figure caption or an extracted
equation line CAN verify a quote (so a visual claim is not simply unciteable), but the
class it earns (`caption_verified` / `equation_verified`) is capped well below what a
table cell earns, in `harness.grading.EVIDENCE_CEILING`.
"""
from __future__ import annotations

from harness import grading, pdf
from harness.artifacts import Equation, Figure
from harness.stages.audit import verify_evidence

CAPTION = "A schematic of the model architecture, showing the encoder and decoder."
EQUATION_TEXT = "y = alpha * x + beta"  # flattened length must clear _QUOTE_MIN (8)


def _doc_units():
    by_figure = {0: Figure(figure_idx=0, page=3, label="Figure 3", caption=CAPTION)}
    by_equation = {0: Equation(equation_idx=0, page=5, number="7", text=EQUATION_TEXT)}
    return by_figure, by_equation


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def test_a_figure_caption_line_is_extracted_and_addressed():
    figs = pdf.extract_figures(["intro text", "Figure 3: " + CAPTION, "more text"])
    assert len(figs) == 1
    assert figs[0].figure_idx == 0 and figs[0].page == 2
    assert figs[0].caption == CAPTION
    assert figs[0].ref() == "F0"


def test_a_short_display_equation_is_extracted():
    """The regression this file exists to pin: `y = mx + b (7)` is a SHORT equation —
    only two characters precede the `=` — and an earlier version of the extraction
    regex required at least four, which silently made every short equation
    unextractable. `x=y (1)` is the minimal case: one character on each side."""
    eqs = pdf.extract_equations(["header", "y = mx + b   (7)", "footer"])
    assert len(eqs) == 1
    assert eqs[0].number == "7" and eqs[0].text == "y = mx + b"
    minimal = pdf.extract_equations(["x=y (1)"])
    assert len(minimal) == 1 and minimal[0].text == "x=y"


def test_a_line_with_no_trailing_number_is_not_an_equation():
    assert pdf.extract_equations(["The result follows since a = b in this regime."]) == []


def test_figures_and_equations_render_with_their_address():
    figs = pdf.extract_figures(["Figure 1: caption one"])
    text = pdf.render_figures(figs)
    assert "[F0]" in text and "caption one" in text
    eqs = pdf.extract_equations(["a = b (2)"])
    text2 = pdf.render_equations(eqs)
    assert "[E0]" in text2 and "(2)" in text2


# --------------------------------------------------------------------------- #
# verify_evidence: citable, but only within the two existing ref shapes' company
# --------------------------------------------------------------------------- #
def test_a_caption_quote_verifies_as_caption_verified():
    by_figure, by_equation = _doc_units()
    klass, obs = verify_evidence(CAPTION, "F0", "", {}, by_figure=by_figure, by_equation=by_equation)
    assert klass == "caption_verified"
    assert "does not report the figure's plotted values" in obs


def test_an_equation_quote_verifies_as_equation_verified():
    by_figure, by_equation = _doc_units()
    klass, obs = verify_evidence(EQUATION_TEXT, "E0", "", {}, by_figure=by_figure, by_equation=by_equation)
    assert klass == "equation_verified"
    assert "lossy" in obs


def test_a_figure_ref_to_an_unknown_index_is_unverified():
    by_figure, by_equation = _doc_units()
    klass, obs = verify_evidence(CAPTION, "F9", "", {}, by_figure=by_figure, by_equation=by_equation)
    assert klass == "unverified" and obs == ""


def test_the_shape_figure_3_is_still_not_a_valid_ref():
    """`F3` is a ref; `"figure 3"` is prose describing one, and remains unverified —
    this is the exact distinction `test_a_finding_without_a_wellformed_ref_is_not_substantiated`
    already pins for the table/page shapes; here for figures too."""
    by_figure, by_equation = _doc_units()
    klass, _ = verify_evidence(CAPTION, "figure 3", "", {}, by_figure=by_figure, by_equation=by_equation)
    assert klass == "unverified"


def test_verify_evidence_without_the_maps_still_behaves_for_everyone_else():
    """The keyword-only, None-defaulted maps must not disturb any positional caller —
    of which there are roughly fifteen across the existing suite."""
    klass, _ = verify_evidence("x", "F0", "", {})
    assert klass == "unverified"


# --------------------------------------------------------------------------- #
# The severity ceiling — citable, but capped
# --------------------------------------------------------------------------- #
def test_a_caption_only_finding_cannot_exceed_note():
    _, counted, _, _ = grading.derive(
        lens_severity="FATAL", verification_state="complete", calc_class="not_applicable",
        graded=False, evidence_class="caption_verified")
    assert counted == "NOTE"


def test_an_equation_only_finding_cannot_exceed_minor():
    _, counted, _, _ = grading.derive(
        lens_severity="FATAL", verification_state="complete", calc_class="not_applicable",
        graded=False, evidence_class="equation_verified")
    assert counted == "MINOR"


def test_a_cell_corroborated_figure_finding_is_not_capped_by_the_figure():
    """The escape hatch: a figure-based concern that ALSO cites a table cell (via the
    GRADER's independent citation) has its ceiling lifted — the lens is not silenced,
    it is told the only way a picture moves a verdict is by finding the number behind
    it. Still bound by `fatal_needs_two_cells` at MAJOR, since only one of the two
    citations is cell-verified."""
    finding_class, counted, cap, _ = grading.derive(
        lens_severity="FATAL", verification_state="complete", calc_class="not_applicable",
        graded=True, grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH",
        falsification_survived=True, has_impact_statement=True, has_steelman=True,
        evidence_class="caption_verified", grader_evidence_class="cell_verified")
    assert counted == "MAJOR", (finding_class, counted, cap)
