"""Figures and equations as citable evidence, and what a weak citation actually bounds.

Governing rule, stated in `harness.artifacts.PaperDoc`'s docstring: PDF inspection can
KILL a candidate or point at the right unit to cite; it can never BE the evidence. What
makes that true in code is narrower than it sounds — a figure caption or an extracted
equation line CAN verify a quote, so a visual claim is not simply unciteable.

What it earns is a CONFIDENCE ceiling, not a severity one: a caption reports none of a
figure's plotted values and equation extraction is lossy, so neither can support HIGH
confidence alone — and confidence is what caps severity. Additional independent checks
(the grader's own citation, a harness-reproduced calculation) LIFT that ceiling. See
`harness.grading.EVIDENCE_CONFIDENCE_CEILING` and `evidence_support`, and the block
comment below for what this replaced and why.
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


def test_a_right_margin_equation_number_on_its_own_line_is_still_matched():
    """The shape EVERY real paper in the corpus actually uses. A LaTeX equation number is
    typeset in the right margin as a separate text block, so text extraction emits it on
    its own line — and requiring body-and-number together, which the first version did,
    matched the synthetic fixture above and ZERO equations across four real papers that
    all number their display equations."""
    # ONE page holding several lines — `extract_equations` takes a list of PAGES and the
    # lookback is within a page, which is how a real paper arrives from `page_texts`.
    eqs = pdf.extract_equations(["Some prose introducing it:\n"
                                 "L(x) = Lon(x) + lambda * Loff(x)\n"
                                 "(4)\n"
                                 "and the discussion continues."])
    assert len(eqs) == 1, eqs
    assert eqs[0].number == "4"
    assert eqs[0].text == "L(x) = Lon(x) + lambda * Loff(x)"


def test_a_blank_line_between_body_and_number_does_not_break_the_match():
    eqs = pdf.extract_equations(["y = mx + b\n\n(9)"])
    assert len(eqs) == 1 and eqs[0].number == "9" and eqs[0].text == "y = mx + b"


def test_a_lone_number_above_prose_attaches_to_nothing():
    """Returning '' rather than guessing is the load-bearing half. A wrongly attached body
    would make `verify_evidence` certify a quote as `equation_verified` against text that
    is not the equation — a false machine attestation, which is worse than no equation at
    all. Real case: ICLR's equations extract as per-glyph fragments ('F iw ,', 'ln LID*')
    with no operator on the nearest line, and this is why that paper yields zero."""
    assert pdf.extract_equations(
        ["We now show that the estimator is consistent under mild assumptions.\n(1)"]) == []
    assert pdf.extract_equations(["F iw ,\n(1)"]) == [], "no relational operator, no body"
    assert pdf.extract_equations(["(1)\n(2)"]) == [], "a number is not another's body"
    assert pdf.extract_equations(["z = 1\na\nb\nc\n(5)"]) == [], \
        "the lookback window is small on purpose"


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
# Evidence strength bounds CONFIDENCE, not severity — and corroboration lifts it
# --------------------------------------------------------------------------- #
# WHAT CHANGED HERE, AND WHY. This block used to assert a universal, evidence-type-keyed
# SEVERITY ceiling: `caption_verified` could never exceed NOTE, `equation_verified` never
# MINOR, whatever the finding actually established. That is structurally the same move as
# "no standard deviation = MAJOR" — a rule that decides IMPACT from a fact about the
# citation — and it produced the same kind of wrong answer: a caption naming the exact
# quantity a headline claim rests on, whose arithmetic the harness then independently
# reproduced, was flattened to a NOTE and counted for nothing.
#
# What a weak citation genuinely bounds is HOW SURE anyone can be. So evidence type now
# caps CONFIDENCE (`grading.EVIDENCE_CONFIDENCE_CEILING`), confidence caps severity, and
# additional independent checks LIFT the ceiling. The tests below pin that, in both
# directions — the cap still binds on a lone weak citation, and it stops binding when the
# concern is corroborated.
def test_a_caption_only_finding_is_held_to_low_confidence():
    """A lone caption supports LOW confidence, which caps severity at MINOR — not the
    blanket NOTE the old evidence-type severity ceiling imposed."""
    ceiling, sources = grading.evidence_support("caption_verified")
    assert ceiling == "LOW" and sources == ("caption_verified",)
    _, counted, cap, why = grading.derive(
        lens_severity="FATAL", verification_state="complete", calc_class="not_applicable",
        graded=False, evidence_class="caption_verified")
    assert counted == "MINOR", (counted, cap, why)
    assert cap == "confidence", "the cap must bind through confidence, not through severity"


def test_a_lone_equation_is_held_to_medium_confidence():
    """Display-equation extraction is lossy, so a lone equation citation supports MEDIUM
    confidence and no more — which caps severity at MAJOR, short of FATAL."""
    assert grading.evidence_support("equation_verified")[0] == "MEDIUM"
    _, counted, _, _ = grading.derive(
        lens_severity="FATAL", verification_state="complete", calc_class="not_applicable",
        graded=False, evidence_class="equation_verified")
    assert counted == "MAJOR"


def test_a_recomputed_caption_finding_is_no_longer_caption_only():
    """The rule the old universal cap made inexpressible: corroborated evidence
    collectively supports a stronger finding. A figure caption whose arithmetic the
    HARNESS re-verified — both operands re-checked against the parsed paper, the
    expression re-evaluated to the lens's own stated result — is two independent checks,
    not one weak one, and the ceiling lifts a step accordingly."""
    ceiling, sources = grading.evidence_support("caption_verified", calc_class="recomputed_ok")
    assert ceiling == "MEDIUM" and set(sources) == {"caption_verified", "harness_recomputed"}
    _, counted, _, _ = grading.derive(
        lens_severity="MAJOR", verification_state="complete", calc_class="recomputed_ok",
        graded=False, evidence_class="caption_verified")
    assert counted == "MAJOR", "a machine-reproduced calculation is corroboration"


def test_a_failed_recomputation_corroborates_nothing():
    """Only `recomputed_ok` lifts the ceiling. A calculation the harness could not
    reproduce, or whose operands were not in the paper, is not a second check — it is a
    reason to doubt the first one."""
    for bad in ("recomputed_mismatch", "operands_unverified", "unparseable", "not_attempted"):
        assert grading.evidence_support("caption_verified", calc_class=bad)[0] == "LOW", bad


def test_a_cell_corroborated_figure_finding_is_not_capped_by_the_figure():
    """The escape hatch: a figure-based concern that ALSO cites a table cell (via the
    GRADER's independent citation) has its confidence ceiling lifted — the lens is not
    silenced, it is told the only way a picture moves a verdict is by finding the number
    behind it. Still bound at MAJOR by `fatal_needs_corroboration`, since only one of the
    two citations is cell-verified and no calculation was reproduced."""
    finding_class, counted, cap, _ = grading.derive(
        lens_severity="FATAL", verification_state="complete", calc_class="not_applicable",
        graded=True, grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH",
        falsification_survived=True, has_impact_statement=True, has_steelman=True,
        evidence_class="caption_verified", grader_evidence_class="cell_verified")
    assert counted == "MAJOR", (finding_class, counted, cap)
    assert cap == "fatal_needs_corroboration", cap
