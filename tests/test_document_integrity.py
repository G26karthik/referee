"""Document-integrity observations — and, mostly, what must NEVER be read out of one.

The doctrine is `tests/test_runtime_requirements.py`'s, stated there for a static code
audit and holding here for the same reason: *the evidence supports an OBSERVATION and not
a CONCLUSION. So this layer is report-only.* It puts a page, an address and a verbatim
quote in front of a reader, and it gates nothing — no severity derivation, no verdict, no
colour can receive one.

Every test whose name says `never`, `cannot` or `is not` pins a rule that looked sound and
was fatal:

  "the prose cites Table 12 and no Table 12 exists" is the check anyone writes first, and
  over the eleven shipped `paper/doc.json` documents it produces twelve claims of an absent
  object and not one of them is true. CVPR tables 2 and 4 and equations 1 and 6, ICLR
  tables 12 and 13, APT tables 5 and 12 and equations 1, 3, 5 and 6 are all printed in
  their papers; they are missing only from what extraction recovered, whose equation label
  sets across those three papers are {2,3,7,8,9}, {} and {2,10,11,12}. Reporting those as
  defects would make a review's colour a property of this harness's extractor, which is
  invariant 17's defect and invariant 20's axis collapse in one move.

  a label set with a hole in it is a hole in OUR RECORD. Every corpus equation set has
  holes and every one of them is ours, so the gap check is tables-only and even there it
  is an admission.

  a paper whose headings are unnumbered (ICLR: all-caps, no numbers) cannot have an
  unresolved section reference. The answer is "cannot determine", never "missing".

  an average column may be weighted, may average a subset, or may average down the page,
  and a header cannot say which. So the column's meaning has to be established by a row of
  its own table before any other row of it may be called wrong.

The eleven shipped documents are used where they are present, because a synthetic fixture
cannot show that a rule survives a real extractor. The BEHAVIOURAL assertions are all on
synthetic `PaperDoc`s: `harness/pdf.py` is being hardened in parallel and every corpus
figure, table and section count is expected to move.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from harness import docintegrity as di
from harness.artifacts import (EVIDENCE_CLASSES, INTEGRITY_ABOUT, INTEGRITY_CHECKS,
                               CrossRef, DocumentObservation, Equation, EvalReport, Figure,
                               PaperDoc, Section, Table, TargetSet)

_CORPUS = sorted(Path("projects").glob("*/paper/doc.json"))
needs_corpus = pytest.mark.skipif(not _CORPUS, reason="no ingested documents on this disk")


def _doc(**kw) -> PaperDoc:
    fields: dict = {"paper_id": "t", "n_pages": 3}
    fields.update(kw)
    return PaperDoc.model_validate(fields)


def _corpus_docs() -> list[PaperDoc]:
    return [PaperDoc.model_validate_json(p.read_text(encoding="utf-8")) for p in _CORPUS]


def _checks(doc: PaperDoc, check: str) -> list[DocumentObservation]:
    return [o for o in di.observe(doc) if o.check == check]


def _source(obj) -> str:
    """The source of a module or function, read off DISK and located by name.

    `inspect.getsource` resolves a body by the line number recorded at import time, so it
    returns a mangled span — or raises `TokenError` — whenever the file has been edited
    since. Several agents edit this repo at once, and that turned a real prohibition into
    a guard that went red for reasons unrelated to the prohibition. Locating the node in a
    freshly parsed tree cannot drift.
    """
    path = Path(inspect.getfile(obj))
    text = path.read_text(encoding="utf-8")
    if inspect.ismodule(obj):
        return text
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == obj.__name__:
            return ast.unparse(node)
    raise AssertionError(f"{obj.__name__} is no longer defined in {path}")


# --------------------------------------------------------------------------- #
# The three guard rails. These landed before the second check did.
# --------------------------------------------------------------------------- #
def test_a_document_observation_cannot_reach_the_severity_derivation():
    """The load-bearing test. Nothing this layer produces may set, cap or colour anything.

    Verified structurally rather than by assertion about intent, exactly as
    `test_no_runtime_demand_reaches_selection_or_authorization` does it: the seven
    functions that decide what a finding counts as, what kind of problem it is, whether a
    target is worth pursuing and what colour the paper gets take vocabulary strings,
    booleans and finding lists. None of their signatures can name an observation, and none
    of their source modules imports this one — so "a duplicated table label is a MAJOR"
    is inexpressible rather than merely absent.
    """
    from harness import grading, planner, priority, taxonomy
    from harness.stages import report

    deciders = (grading.derive, taxonomy.classify, taxonomy.evidence_state,
                planner.classify, priority.score,
                report.overall_verdict, report.claim_status, report.triage)
    for fn in deciders:
        params = " ".join(inspect.signature(fn).parameters).lower()
        for banned in ("observation", "integrity", "docint", "crossref", "caption",
                       "numbering", "document_obs"):
            assert banned not in params, (
                f"{fn.__qualname__} can receive {banned!r}: {params}")

    for module in (grading, taxonomy, planner, priority):
        assert "docintegrity" not in _source(module), (
            f"{module.__name__} imports the document-integrity layer; a module that "
            "decides severity or centrality may not see an observation")

    # `stages.report` is allowed to IMPORT it, because the report is where observations are
    # printed. What it may not do is let one reach a decision: the three deciders above are
    # the whole decision surface and their own sources must not name it either.
    for fn in (report.overall_verdict, report.claim_status, report.triage):
        src = _source(fn)
        assert "docintegrity" not in src, fn.__qualname__
        assert "observation" not in src.lower(), fn.__qualname__


def test_a_document_observation_has_no_severity_to_be_read():
    """A field that exists is a field something eventually reads.

    `DocumentObservation` is `extra="allow"` like every artifact here, so absence from
    `model_fields` is the guarantee that nothing DECLARES one — and the sweep over real
    documents is the guarantee that nothing SETS one either.
    """
    for banned in ("severity", "counted_severity", "confidence", "scientific_class",
                   "candidate_class", "verdict", "triage", "route", "finding_class",
                   "resolution_status", "evidence_state"):
        assert banned not in DocumentObservation.model_fields, banned

    docs = _corpus_docs() or [_doc()]
    for doc in docs:
        for o in di.observe(doc):
            for banned in ("severity", "counted_severity", "confidence", "verdict"):
                assert not getattr(o, banned, None), (banned, o.check)


def test_a_document_observation_never_renders_inside_the_scientific_findings_section():
    """`## Scientific findings` is the defect channel a referee reads as an accusation.

    The same boundary `code_audit`'s `RuntimeDemand`s hold: its own list, never `findings`.
    This test is written so it holds BOTH before the renderer prints observations and
    after — if a detail sentence appears at all, it appears after the scope heading, never
    between the findings heading and the next section.
    """
    obs = di.observe(_doc(
        figures=[Figure(figure_idx=0, page=1, label="Figure 3", caption="Figure 3: a"),
                 Figure(figure_idx=1, page=1, label="Figure 3", caption="Figure 3: a")]))
    assert obs, "the fixture really does produce an observation"

    report = EvalReport(paper_id="p", title="A Paper", verdict="GREEN", triage="GREEN",
                        reproduction_status="NOT_ATTEMPTED",
                        document_observations=list(obs),
                        lenses_run=["overclaim", "protocol", "confound", "contradiction"])
    from harness.stages.report import render_reviewer_report
    text = render_reviewer_report(report, TargetSet(paper_id="p"))

    findings_at = text.find("## Scientific findings")
    scope_at = text.find("## Scope of this review")
    for o in obs:
        for sentence in (o.detail, o.check):
            at = text.find(sentence)
            if at < 0:
                continue
            assert not (0 <= findings_at < at < scope_at), (
                f"{sentence!r} rendered inside the scientific-findings section")


# --------------------------------------------------------------------------- #
# The governing constraint: an extraction limit is never a defect in the paper
# --------------------------------------------------------------------------- #
def test_an_extraction_limit_is_never_reported_as_a_defect_in_the_paper():
    """The twelve measured false positives, each one an object that IS in its paper.

    Mirrors `test_an_unaddressable_target_is_not_reported_as_a_missing_artifact`: the
    label has to survive, and so does the SENTENCE, because a reader skims the bullet and
    not the `about` field. So an EXTRACTION observation must say "extraction recovered"
    in its own words and must not contain the phrase "the paper".
    """
    doc = _doc(sections=[Section(
        section_idx=0, title="4. Experiments", page_start=1,
        text="Table 1 lists the main comparison. Table 2 reports the ablation and "
             "Table 3 the transfer results, and Eq. (6) defines the loss.")],
        tables=[Table(table_idx=0, page=1, caption="Table 1: Comparison.",
                      rows=[["method", "1.0"]]),
                Table(table_idx=1, page=1, caption="Table 3: Transfer.",
                      rows=[["method", "2.0"]])],
        equations=[Equation(equation_idx=0, page=1, number="2", text="y = Wx + b")])

    unresolved = _checks(doc, "CROSSREF_UNRESOLVED")
    assert {o.about for o in unresolved} == {"EXTRACTION"}
    assert len(unresolved) == 2, [o.detail for o in unresolved]   # table 2, equation 6
    for o in unresolved:
        assert "extraction recovered" in o.detail.lower(), o.detail
        assert "the paper" not in o.detail, o.detail
        assert "missing" not in o.detail.lower(), o.detail

    # No check may put a document-integrity absence claim about the paper anywhere.
    for o in di.observe(doc):
        if o.about == "EXTRACTION":
            assert "the paper" not in o.detail, o.detail


@needs_corpus
def test_the_twelve_measured_false_positives_all_come_back_as_extraction():
    """The real documents, not a fixture. This is the number the design was built from.

    CVPR tables 2 and 4 and equations 1 and 6; ICLR tables 12 and 13; APT tables 5 and 12
    and equations 1, 3, 5 and 6. Every one exists in its paper. Asserted as a property —
    zero PAPER-side absence claims — rather than as the count 12, because `pdf.py` is
    being hardened and recovering one of those objects legitimately removes a row.
    """
    for doc in _corpus_docs():
        for o in di.observe(doc):
            if o.check in ("CROSSREF_UNRESOLVED", "NUMBERING_GAP",
                           "SECTION_REF_UNRESOLVED", "BODY_UNCAPTIONED"):
                assert o.about == "EXTRACTION", (doc.paper_id, o.check, o.detail)


def test_an_absence_check_can_never_be_configured_to_speak_about_the_paper():
    """Not "does not today" — cannot.

    `evidence_floor` is the only place the answer comes from, its inputs are vocabulary
    strings, and every absence-shaped check returns EXTRACTION for every one of them.
    Swept over the closed cross product, so widening it requires editing the tuple that
    the next test pins the size of.
    """
    absence = ("CROSSREF_UNRESOLVED", "NUMBERING_GAP", "SECTION_REF_UNRESOLVED",
               "BODY_UNCAPTIONED", "OBJECT_UNCITED", "LABEL_DUPLICATED",
               "LABEL_OUT_OF_ORDER", "CAPTION_LABEL_CONFLICT")
    for check in absence:
        assert check in INTEGRITY_CHECKS, check
        for cls in EVIDENCE_CLASSES + ("", "made_up", "cell_verified"):
            assert di.evidence_floor(check, cls) == "EXTRACTION", (check, cls)


def test_only_one_check_may_speak_about_the_paper_and_it_is_named():
    """A one-member list is a design decision and has to be visible as one.

    Widening it is not adding a rule: every other candidate compares something extraction
    recovered against something extraction may equally have lost, and an absence in a
    lossy record is not an absence in the document.
    """
    assert di.CLAIMABLE_ABOUT_THE_PAPER == ("TABLE_ARITHMETIC",)
    for check in INTEGRITY_CHECKS:
        floors = {di.evidence_floor(check, c) for c in EVIDENCE_CLASSES + ("",)}
        if check in di.CLAIMABLE_ABOUT_THE_PAPER:
            assert "PAPER" in floors, check
        else:
            assert "PAPER" not in floors, check


# --------------------------------------------------------------------------- #
# Refusing rather than computing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("weak", ["caption_verified", "equation_verified", "prose_verified",
                                  "unverified", ""])
def test_a_check_over_a_weak_input_refuses_rather_than_computing(weak):
    """The hotfix this borrows, pinned.

    The one external system with a deterministic internal-consistency audit refuses the
    date arithmetic when the provenance behind the dates is low-confidence and emits a
    metadata-missing record instead — and that guard shipped as a HOTFIX, because the
    first version never threaded the provenance through and, in its own words, defeated
    the safety check. So the input's own class is a parameter of `evidence_floor`, not a
    caller's responsibility: a caption reports none of a figure's plotted values and
    equation extraction is lossy, so neither may carry arithmetic about the paper.
    """
    assert di.evidence_floor("TABLE_ARITHMETIC", weak) == "NOT_INVESTIGATED"


def test_an_unknown_check_and_an_unknown_class_both_fail_closed():
    for check in ("", "MADE_UP", "table_arithmetic", "CROSSREF"):
        assert di.evidence_floor(check, "cell_verified") == "NOT_INVESTIGATED", check
    for check in INTEGRITY_CHECKS:
        assert di.evidence_floor(check, "made_up") in INTEGRITY_ABOUT + ("NOT_INVESTIGATED",)


def test_a_check_the_layer_does_not_compute_says_so_instead_of_returning_nothing():
    """An empty result and an unimplemented check are not the same answer.

    `PROSE_CELL_MISMATCH` needs to know WHICH cell a sentence means, and choosing one by
    position is the coincidence invariant 18 forbids on the execution path. Silence would
    read as "we compared and found nothing".
    """
    assert di.NOT_COMPUTED == ("PROSE_CELL_MISMATCH",)
    for check in di.NOT_COMPUTED:
        assert check in INTEGRITY_CHECKS
        for cls in EVIDENCE_CLASSES + ("",):
            assert di.evidence_floor(check, cls) == "NOT_INVESTIGATED", (check, cls)
        assert di.determinations(_doc())[check] == "NOT_INVESTIGATED"


def test_a_paper_with_no_recovered_labels_says_it_cannot_determine_numbering():
    """Every corpus equation label set has holes and every hole is ours.

    Measured: {2,3,7,8,9} on CVPR, {} on ICLR, {2,10,11,12} on APT. So the gap check is
    tables-only, and a document with fewer than two integer table labels answers
    NOT_INVESTIGATED rather than reporting its own record as the paper's numbering.
    """
    holes = _doc(equations=[Equation(equation_idx=i, page=1, number=n, text=f"z = {n}")
                            for i, n in enumerate(("2", "3", "7", "8", "9"))])
    assert di.determinations(holes)["NUMBERING_GAP"] == "NOT_INVESTIGATED"
    assert _checks(holes, "NUMBERING_GAP") == []

    figures = _doc(figures=[Figure(figure_idx=i, page=1, label=f"Figure {n}",
                                   caption=f"Figure {n}: x")
                            for i, n in enumerate(("1", "2", "5"))])
    assert di.determinations(figures)["NUMBERING_GAP"] == "NOT_INVESTIGATED", (
        "a figure label set polluted by duplicate recoveries cannot carry gap arithmetic")
    assert _checks(figures, "NUMBERING_GAP") == []

    tables = _doc(tables=[Table(table_idx=i, page=1, caption=f"Table {n}: x",
                                rows=[["m", "1"]])
                          for i, n in enumerate(("1", "3"))])
    gaps = _checks(tables, "NUMBERING_GAP")
    assert len(gaps) == 1 and gaps[0].about == "EXTRACTION"
    assert "2" in gaps[0].detail


def test_a_paper_with_unnumbered_headings_cannot_have_an_unresolved_section_reference():
    """ICLR's headings are all-caps and unnumbered. "Cannot determine", never "missing"."""
    unnumbered = _doc(sections=[Section(
        section_idx=0, title="EXPERIMENTS", page_start=1,
        text="As shown in Section 4.2 the improvement is consistent.")])
    assert di.determinations(unnumbered)["SECTION_REF_UNRESOLVED"] == "NOT_INVESTIGATED"
    assert _checks(unnumbered, "SECTION_REF_UNRESOLVED") == []

    numbered = _doc(sections=[
        Section(section_idx=0, title="4. Experiments", page_start=1,
                text="As shown in Section 9.7 the improvement is consistent."),
        Section(section_idx=1, title="4.1. Datasets", page_start=1, text="We use CIFAR.")])
    got = _checks(numbered, "SECTION_REF_UNRESOLVED")
    assert len(got) == 1 and got[0].about == "EXTRACTION"
    assert "9.7" in got[0].detail and "the paper" not in got[0].detail


# --------------------------------------------------------------------------- #
# The one check that may speak about the paper
# --------------------------------------------------------------------------- #
def test_an_average_column_no_row_reproduces_convicts_nothing():
    """A weighted average, an average of a subset, or an average down the page.

    A header saying "Avg." says none of those apart, so a table whose column no row
    reproduces is a table this layer does not understand — and reporting every row of it
    would convict the whole table on one misread layout.
    """
    doc = _doc(tables=[Table(table_idx=0, page=2, caption="Table 1: r",
                             header=["Method", "A", "B", "Avg."],
                             rows=[["m1", "10", "20", "99"], ["m2", "30", "40", "98"]])])
    assert di.determinations(doc)["TABLE_ARITHMETIC"] == "NOT_INVESTIGATED"
    assert _checks(doc, "TABLE_ARITHMETIC") == []


def test_a_correctly_rounded_average_is_not_an_arithmetic_error():
    """55.6, 79.3, 46.9 and 49.9 average to 57.925 and APT prints 57.9.

    Comparing a rounded average at full precision convicts every correct row in every
    paper, so the tolerance is read off the printed token rather than fixed.
    """
    doc = _doc(tables=[Table(table_idx=0, page=7, caption="Table 3: r",
                             header=["Method", "ARC", "HS", "MMLU", "TQA", "Avg."],
                             rows=[["base", "53.1", "77.7", "43.8", "39.0", "53.4"],
                                   ["lora", "55.6", "79.3", "46.9", "49.9", "57.9"]])])
    assert _checks(doc, "TABLE_ARITHMETIC") == []
    assert di.determinations(doc)["TABLE_ARITHMETIC"] == "PAPER", (
        "the column's meaning IS established here — both rows reproduce it")


def test_a_row_averaged_over_a_different_number_of_cells_is_not_compared():
    """An extractor that loses one cell changes the denominator.

    Averaging four numbers where the establishing row averaged five and calling the
    difference the paper's error would report our own loss as their arithmetic.
    """
    doc = _doc(tables=[Table(table_idx=0, page=7, caption="Table 3: r",
                             header=["Method", "A", "B", "C", "Avg."],
                             rows=[["m1", "10", "20", "30", "20.0"],
                                   ["m2", "10", "20", "", "20.0"]])])
    assert _checks(doc, "TABLE_ARITHMETIC") == []


def test_an_established_column_reports_the_row_that_does_not_average():
    """The one PAPER-side observation this layer can make, and its shape."""
    doc = _doc(tables=[Table(table_idx=0, page=5, caption="Table 2: r",
                             header=["Method", "A", "B", "Avg."],
                             rows=[["m1", "10", "20", "15.0"],
                                   ["m2", "30", "40", "99.0"]])])
    (o,) = _checks(doc, "TABLE_ARITHMETIC")
    assert o.about == "PAPER" and o.check == "TABLE_ARITHMETIC"
    assert o.ref == "T0:r1:c3" and o.quote == "99.0" and o.page == 5
    assert "35" in o.detail, o.detail
    from harness import claims
    assert claims.resolve(doc, o.ref, o.quote).resolved


@pytest.mark.parametrize("cell", ["64.3 64.8", "100.0%", "38.6/17.0/35.8", "n/a", "-",
                                  "1.2 ± 0.3"])
def test_a_cell_that_is_not_exactly_one_number_is_never_an_operand(cell):
    """Two numbers a ruled-table extractor merged, a unit, three metrics, a spread.

    Averaging any of them is arithmetic over something this module cannot claim to have
    understood, and the result would be printed as the paper's error.
    """
    assert di._as_number(cell) is None, cell
    doc = _doc(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                             header=["Method", "A", "B", "Avg."],
                             rows=[["m1", "10", "20", "15.0"],
                                   ["m2", cell, "20", "99.0"]])])
    assert _checks(doc, "TABLE_ARITHMETIC") == [], cell


# --------------------------------------------------------------------------- #
# Addresses, ordering, and the caps
# --------------------------------------------------------------------------- #
@needs_corpus
def test_every_observation_carries_an_address_a_reader_can_re_resolve():
    """An observation whose pointer does not resolve is the unverifiable claim
    invariant 1 exists to refuse. `''` is allowed and a wrong address is not."""
    from harness import claims

    seen = 0
    for doc in _corpus_docs():
        for o in di.observe(doc):
            seen += 1
            if not o.ref:
                continue
            got = claims.resolve(doc, o.ref, o.quote)
            assert got.resolved, (doc.paper_id, o.check, o.ref, got.resolution, got.detail)
    assert seen, "the corpus really does produce observations"


def test_no_document_observation_colours_a_paper():
    """Invariant 17: a colour may not be a property of this harness's configuration.

    Ten checks by two abouts, put on a report next to no finding and no reconciliation.
    The triage cannot see them — that is the point — so every one of the twenty stays
    GREEN, and the assertion is over the whole cross product rather than one example.
    """
    from harness.stages.report import claim_status, overall_verdict, triage

    for check in INTEGRITY_CHECKS:
        for about in INTEGRITY_ABOUT:
            o = DocumentObservation(check=check, about=about, ref="", quote="",
                                    page=1, detail="something structural")
            level, _why = triage([], None, outcomes=[], objects=[])
            binary, _ = overall_verdict([])
            status, _ = claim_status([])
            assert level == "GREEN", (check, about, level)
            assert binary == "GREEN" and status == "NOT_VERIFIED"
            assert o.about in INTEGRITY_ABOUT


def test_the_observation_count_is_bounded_and_says_what_it_dropped():
    """A table of contents citing sixty tables must not become two pages.

    Bounded BY CONSTRUCTION — a per-check cap and a per-document cap, both named — not by
    hoping documents are small, and what is dropped is said rather than silently lost.
    """
    assert isinstance(di._MAX_PER_CHECK, int) and 0 < di._MAX_PER_CHECK < 25
    assert isinstance(di._MAX_OBSERVATIONS, int) and 0 < di._MAX_OBSERVATIONS < 100

    flood = _doc(
        sections=[Section(section_idx=0, title="1. Results", page_start=1,
                          text=" ".join(f"Table {n} reports another split."
                                        for n in range(1, 80)))],
        tables=[Table(table_idx=0, page=1, caption="Table 1: the only one recovered",
                      rows=[["method", "1.0"]])])
    got = di.observe(flood)
    assert len(got) <= di._MAX_OBSERVATIONS
    per_check = sum(1 for o in got if o.check == "CROSSREF_UNRESOLVED")
    assert per_check == di._MAX_PER_CHECK, per_check
    assert any("not listed" in o.detail for o in got)
    assert any(str(di._MAX_PER_CHECK) in o.detail for o in got)


def test_observations_are_ordered_by_the_declared_vocabulary_not_by_discovery():
    """Two runs over one document produce the same list, and a renderer that groups by
    check does not have to sort."""
    doc = _doc(
        sections=[Section(section_idx=0, title="1. R", page_start=1,
                          text="Table 1 and Table 9 both matter here.")],
        tables=[Table(table_idx=0, page=1, caption="Table 1: a", rows=[["m", "1"]]),
                Table(table_idx=1, page=1, caption="Table 1: b", rows=[["m", "2"]])])
    order = [o.check for o in di.observe(doc)]
    assert order == sorted(order, key=INTEGRITY_CHECKS.index), order
    assert [o.check for o in di.observe(doc)] == order, "not deterministic"


def test_the_two_halves_are_counted_separately_and_never_summed():
    """"3 about the paper and 6 about our extraction" and "9 observations" say different
    things to a referee, and only the first is true."""
    doc = _doc(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                             header=["Method", "A", "B", "Avg."],
                             rows=[["m1", "10", "20", "15.0"],
                                   ["m2", "30", "40", "99.0"]])])
    counts = di.summarise(di.observe(doc))
    assert set(counts) == set(INTEGRITY_ABOUT)
    assert counts["PAPER"] == 1 and counts["EXTRACTION"] == 0
    assert "total" not in counts


# --------------------------------------------------------------------------- #
# Determinations: an empty list means two opposite things without them
# --------------------------------------------------------------------------- #
def test_an_empty_document_determines_nothing_rather_than_reporting_it_clean():
    empty = _doc()
    assert di.observe(empty) == ()
    determined = di.determinations(empty)
    assert set(determined) == set(INTEGRITY_CHECKS), "every check, always present"
    assert set(determined.values()) == {"NOT_INVESTIGATED"}


@needs_corpus
def test_every_corpus_document_gets_a_determination_for_every_check():
    for doc in _corpus_docs():
        determined = di.determinations(doc)
        assert set(determined) == set(INTEGRITY_CHECKS), doc.paper_id
        for check, floor in determined.items():
            assert floor in INTEGRITY_ABOUT + ("NOT_INVESTIGATED",), (check, floor)
        # A check that produced an observation must have been determined available, or the
        # emitter and the gate disagree and the gate is decorative.
        for o in di.observe(doc):
            assert determined[o.check] == o.about, (
                doc.paper_id, o.check, determined[o.check])


# --------------------------------------------------------------------------- #
# Reading labels off two extractor generations
# --------------------------------------------------------------------------- #
def test_a_label_is_read_from_the_field_first_and_the_caption_second():
    """`pdf.py` is being hardened in parallel, in the direction of populating
    `Table.label` and stripping the number out of `Table.caption`.

    Both generations have to work: today every shipped table has `label == ''` with the
    number sitting at the head of the caption, and an extractor that starts filling the
    field must not silently lose every label here.
    """
    assert di._printed_label("", "Table 5: Comparison with simulation") == "5"
    assert di._printed_label("5", "") == "5"
    assert di._printed_label("Table 5", "") == "5"
    assert di._printed_label("Figure 12", "Figure 12: the architecture") == "12"
    assert di._printed_label("", "") == ""
    assert di._printed_label("", "We introduce a new class of algorithm") == ""

    old = _doc(tables=[Table(table_idx=0, page=1, caption="Table 5: x", rows=[["m", "1"]]),
                       Table(table_idx=1, page=1, caption="Table 5: y", rows=[["m", "2"]])])
    new = _doc(tables=[
        Table(table_idx=0, page=1, label="5", caption="x", rows=[["m", "1"]]),
        Table(table_idx=1, page=1, label="5", caption="y", rows=[["m", "2"]])])
    assert len(_checks(old, "CAPTION_LABEL_CONFLICT")) == 1
    assert len(_checks(new, "CAPTION_LABEL_CONFLICT")) == 1


def test_an_appendix_label_is_never_read_as_a_descent_or_a_gap():
    """"Table A.1" after "Table 11" is not a descent, and tables 1-11 plus A.1-A.3 have
    no hole. The two sequences do not share a number line."""
    doc = _doc(tables=[
        Table(table_idx=0, page=1, caption="Table 10: x", rows=[["m", "1"]]),
        Table(table_idx=1, page=1, caption="Table 11: y", rows=[["m", "2"]]),
        Table(table_idx=2, page=9, caption="Table A.1: z", rows=[["m", "3"]])])
    assert _checks(doc, "LABEL_OUT_OF_ORDER") == []
    assert _checks(doc, "NUMBERING_GAP") == []


def test_a_duplicate_recovery_and_a_mis_pairing_are_two_different_admissions():
    """One caption recovered twice is not two captions claiming one number.

    The first is a two-column layout whose caption spans both columns; the second means at
    most one of the two pairings is right, and which one needs the page geometry this
    module does not read. A single "duplicated label" label would tell a reader neither.
    """
    twice = _doc(figures=[
        Figure(figure_idx=0, page=4, label="Figure 3", caption="Figure 3: x"),
        Figure(figure_idx=1, page=4, label="Figure 3", caption="Figure 3: x")])
    assert len(_checks(twice, "LABEL_DUPLICATED")) == 1
    assert _checks(twice, "CAPTION_LABEL_CONFLICT") == []

    clash = _doc(tables=[Table(table_idx=0, page=19, caption="Table 10: Ablation study of",
                               rows=[["m", "1"]]),
                         Table(table_idx=1, page=19, caption="Table 10: , using the teacher",
                               rows=[["m", "2"]])])
    assert len(_checks(clash, "CAPTION_LABEL_CONFLICT")) == 1
    assert _checks(clash, "LABEL_DUPLICATED") == []


def test_a_caption_line_surviving_in_the_prose_is_not_a_citation_of_the_object():
    """`OBJECT_UNCITED` over a text whose caption lines survive in the section body would
    find every object cited and could never fire. `CROSSREF_UNRESOLVED` wants exactly
    those occurrences. So both readings are kept and each check says which it uses."""
    doc = _doc(sections=[Section(
        section_idx=0, title="1. R", page_start=1,
        text="The main comparison is in Table 1. "
             "Table 4: Hyperparameters used in every run of the ablation study.")],
        tables=[Table(table_idx=0, page=1, caption="Table 1: Comparison",
                      rows=[["m", "1.0"]]),
                Table(table_idx=1, page=1, caption="Table 4: Hyperparameters",
                      rows=[["lr", "0.001"]])])
    uncited = _checks(doc, "OBJECT_UNCITED")
    assert [o.ref for o in uncited] == ["T1:r0:c0"], (
        "the object's own caption line is not the prose referring to it")

    cited = _doc(sections=[Section(
        section_idx=0, title="1. R", page_start=1,
        text="The settings we used are listed in Table 4 of the appendix.")],
        tables=[Table(table_idx=0, page=1, caption="Table 4: Hyperparameters",
                      rows=[["lr", "0.001"]])])
    assert _checks(cited, "OBJECT_UNCITED") == []


def test_a_citation_wrapped_across_a_line_or_parenthesised_is_still_found():
    """"Table\\n5" is what a two-column PDF does to every reference near a column edge,
    and an equation is cited as "Eq. (3)" at least as often as "Eq. 3". Four of the twelve
    measured false-positive rows were invisible to the scan without the parenthesis."""
    doc = _doc(sections=[Section(
        section_idx=0, title="1. R", page_start=1,
        text="The results in Table\n5 follow from Eq. (3) and Eqn. 4 directly.")],
        tables=[Table(table_idx=0, page=1, caption="Table 1: x", rows=[["m", "1"]])],
        equations=[Equation(equation_idx=0, page=1, number="1", text="y = x")])
    cited = {(c.kind, c.number) for c in di.citations(doc)}
    assert ("table", "5") in cited and ("equation", "3") in cited
    assert ("equation", "4") in cited


def test_back_matter_is_not_scanned_for_the_papers_own_cross_references():
    """A bibliography entry naming a volume number is not a cross-reference, and reading
    one as a citation of the paper's own Table 12 is how a reference list becomes a
    document-integrity claim."""
    doc = _doc(sections=[
        Section(section_idx=0, title="1. R", page_start=1, text="See Table 1."),
        Section(section_idx=1, title="References", page_start=9,
                text="A. Author. Some title. Table 12 of the proceedings, 2020.")],
        tables=[Table(table_idx=0, page=1, caption="Table 1: x", rows=[["m", "1"]])],
        body_end_section_idx=1)
    numbers = {c.number for c in di.citations(doc)}
    assert "12" not in numbers, numbers
    assert _checks(doc, "CROSSREF_UNRESOLVED") == []


def test_the_extraction_layers_own_crossrefs_win_when_it_supplies_them():
    """Two readings of one document that disagree is a defect a reader cannot see.

    `doc.crossrefs` is the ingest stage's own reading; this module's scan is a fallback
    for documents ingested before the field existed, and which one was used is reported
    rather than inferred.
    """
    doc = _doc(sections=[Section(section_idx=0, title="1. R", page_start=1,
                                 text="Nothing here cites anything at all.")],
               tables=[Table(table_idx=0, page=1, caption="Table 1: x", rows=[["m", "1"]])])
    assert di.citation_source(doc) == "prose_scan"
    assert di.citations(doc) == ()

    with_refs = _doc(sections=list(doc.sections), tables=list(doc.tables),
                     crossrefs=[CrossRef(kind="table", number="7", page=1, span="",
                                         quote="the split reported in Table 7")])
    assert di.citation_source(with_refs) == "crossrefs"
    assert {(c.kind, c.number) for c in di.citations(with_refs)} == {("table", "7")}
    (o,) = _checks(with_refs, "CROSSREF_UNRESOLVED")
    assert o.about == "EXTRACTION" and "7" in o.detail


# --------------------------------------------------------------------------- #
# The non-capabilities, printed rather than implied
# --------------------------------------------------------------------------- #
def test_the_layer_names_what_it_does_not_do():
    """Figure content, equation re-assembly, and a caption/body agreement score.

    The third is the one worth naming twice: a number on how well a caption matches a
    body, computed without the page geometry, would be a guess dressed as a measurement —
    and unlike the other two it would look like a finding.
    """
    subjects = {name for name, _why in di.NOT_ATTEMPTED}
    assert subjects == {"figure content", "equation re-assembly",
                        "caption/body agreement score"}
    for _name, why in di.NOT_ATTEMPTED:
        assert len(why) > 40, why
    src = _source(di)
    assert "confidence_score" not in src and "match_score" not in src


def test_the_reader_facing_sentence_never_claims_a_broken_reference():
    """"3 broken references" and "3 references this review could not follow in what it
    recovered" are the same number and opposite claims.

    So the wording lives in the module that knows which of the two it is entitled to say,
    the two halves are named separately in one sentence, and the second bullet states that
    none of it counts. A renderer cannot get this wrong by paraphrasing.
    """
    doc = _doc(sections=[Section(section_idx=0, title="1. R", page_start=1,
                                 text="The headline is Table 1; Table 9 has the rest.")],
               tables=[Table(table_idx=0, page=1, caption="Table 1: a", rows=[["m", "1"]])])
    lines = di.scope_lines(di.observe(doc))
    assert len(lines) == 2
    assert "about the paper" in lines[0] and "own extraction" in lines[0]
    for word in ("broken", "missing", "error", "incorrect", "wrong", "defect"):
        assert word not in " ".join(lines).lower(), word
    assert "not scientific findings" in lines[1]
    assert "counts toward any threshold" in lines[1]
    assert di.scope_lines(()) == (), "absence is not a result"


def test_no_check_is_declared_without_being_reachable():
    """The mirror of "no evidence state is glossed without being reachable".

    A check in the vocabulary that nothing can produce is worse than an absent one: it
    reads as a capability, and its absence from every report reads as a clean document. So
    every declared check either fires on some document or is named in `NOT_COMPUTED`.
    """
    fixtures = [
        # CROSSREF_UNRESOLVED
        _doc(sections=[Section(section_idx=0, title="1. R", page_start=1,
                               text="Table 1 and Table 9 both matter.")],
             tables=[Table(table_idx=0, page=1, caption="Table 1: a", rows=[["m", "1"]])]),
        # OBJECT_UNCITED
        _doc(sections=[Section(section_idx=0, title="1. R", page_start=1,
                               text="Only Table 1 is discussed anywhere.")],
             tables=[Table(table_idx=0, page=1, caption="Table 1: a", rows=[["m", "1"]]),
                     Table(table_idx=1, page=9, caption="Table 2: b", rows=[["m", "2"]])]),
        # LABEL_DUPLICATED
        _doc(figures=[Figure(figure_idx=0, page=1, label="Figure 3", caption="F3: x"),
                      Figure(figure_idx=1, page=1, label="Figure 3", caption="F3: x")]),
        # LABEL_OUT_OF_ORDER
        _doc(tables=[Table(table_idx=0, page=1, caption="Table 4: a", rows=[["m", "1"]]),
                     Table(table_idx=1, page=2, caption="Table 2: b", rows=[["m", "2"]])]),
        # BODY_UNCAPTIONED
        _doc(tables=[Table(table_idx=0, page=1, caption="", rows=[["m", "1"]])]),
        # NUMBERING_GAP
        _doc(tables=[Table(table_idx=0, page=1, caption="Table 1: a", rows=[["m", "1"]]),
                     Table(table_idx=1, page=1, caption="Table 3: b", rows=[["m", "2"]])]),
        # SECTION_REF_UNRESOLVED
        _doc(sections=[Section(section_idx=0, title="4. Experiments", page_start=1,
                               text="The derivation is in Section 9.7 of this paper."),
                       Section(section_idx=1, title="4.1. Setup", page_start=1,
                               text="We use CIFAR.")]),
        # TABLE_ARITHMETIC
        _doc(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                           header=["Method", "A", "B", "Avg."],
                           rows=[["m1", "10", "20", "15.0"],
                                 ["m2", "30", "40", "99.0"]])]),
        # CAPTION_LABEL_CONFLICT
        _doc(tables=[Table(table_idx=0, page=1, caption="Table 10: a", rows=[["m", "1"]]),
                     Table(table_idx=1, page=1, caption="Table 10: b", rows=[["m", "2"]])]),
    ]
    reachable = {o.check for doc in fixtures for o in di.observe(doc)}
    assert set(INTEGRITY_CHECKS) - reachable == set(di.NOT_COMPUTED), sorted(
        set(INTEGRITY_CHECKS) - reachable)


def test_observe_takes_a_parsed_document_and_nothing_else():
    """The signature is the invariant, as it is for `coverage.surface`.

    No `TargetSet`, no `Finding`, no `DiscoveredObject`, no gate state and no severity can
    reach this function, so an observation cannot be conditioned on how the review turned
    out or on which paper it is.
    """
    params = inspect.signature(di.observe).parameters
    assert list(params) == ["doc"]
    assert str(params["doc"].annotation) == "PaperDoc"
    for fn in (di.observe, di.determinations, di.citations, di.objects):
        text = " ".join(inspect.signature(fn).parameters)
        for banned in ("finding", "target", "severity", "gate", "verdict", "outcome"):
            assert banned not in text, (fn.__name__, banned)


def test_no_paper_in_the_corpus_is_named_anywhere_in_the_layer():
    """Invariant 10. A check tuned to one document is a check that measures the document.

    The identifiers are read off the disk rather than listed, so a new paper is covered by
    the same assertion without anyone remembering to add it.
    """
    src = _source(di) + Path(__file__).read_text(encoding="utf-8")
    for path in _CORPUS:
        pid = path.parent.parent.name
        assert pid not in src, pid
    for title in ("WeatherGen", "LDReg", "SAPG", "LLaMA", "RoBERTa"):
        assert title not in _source(di), title


@needs_corpus
def test_the_layer_is_stable_across_a_reingest_of_the_same_bytes():
    """`observe` is a pure function of a parsed document, and a reader who re-runs it has
    to get the same list — otherwise a ledger entry cannot be checked by hand."""
    for path in _CORPUS[:4]:
        raw = path.read_text(encoding="utf-8")
        one = di.observe(PaperDoc.model_validate_json(raw))
        two = di.observe(PaperDoc.model_validate(json.loads(raw)))
        assert [o.model_dump() for o in one] == [o.model_dump() for o in two], path
