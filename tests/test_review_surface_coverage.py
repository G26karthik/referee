"""Review-surface coverage — and, mostly, the ways a coverage number can be fabricated.

A coverage number is the easiest quantity in this system to fabricate, and one already
was: `evaluation.target_addressability_rate = targets_addressable / targets_discovered`
divides the harness's own object list by itself, and because `discovery.discover` mints an
object per `reported_number` only when its reference resolves, a unit extraction loses
leaves BOTH terms. The first test in section 2 below reproduces that on the real
sanchez24a-icml document: eight of its sixteen tables removed, 376 addressable units gone,
and the published rate does not move by a single digit.

Every test whose name begins `test_a_`/`test_an_`/`test_no_` and reads as a prohibition
pins a fabrication route that a naive implementation takes:

  a denominator that can see the numerator — measured by signature, not by inspection of
  intent, because a coverage function that CAN read a `TargetSet` eventually will;

  a numerator that arrives as an object — `str(obj)` would file a caller error as a defect
  in the paper's enumeration;

  padding counted as surface — 556 of sanchez24a-icml's 1,474 row/column slots are empty
  and `pdf.render_tables` never showed them to a lens, so counting them inflates the
  denominator by ragged extraction;

  one numerator instead of two — 629 of the corpus's 867 targets ended NOT_INVESTIGATED,
  so "an address was minted" and "a route was pursued" are different claims and reporting
  the first as coverage claims ~93% of papers nobody looked at three quarters of;

  an empty surface reading as a perfect one — "0 of 0" must be None, never 1.0;

  a rate that could reach a colour — invariant 17: a colour may not be a property of this
  harness's configuration, and coverage is a property of exactly that.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from harness import claims, coverage, discovery, grading, pdf, planner, priority
from harness.artifacts import (CoverageReport, PaperDoc, ReadingRecord, ReviewSurface,
                               SURFACE_KINDS, Equation, Figure, QuantFinding, Section,
                               Table)
from harness.stages import audit as audit_stage
from harness.stages import report as report_stage

# The real corpus documents, used where a fixture cannot show that a denominator survives
# 1,474 table slots and 105k characters of prose. Skipped rather than faked when absent.
_REAL = Path("projects/sanchez24a-icml/paper/doc.json")
_HAS_REAL = _REAL.is_file()
needs_real = pytest.mark.skipif(not _HAS_REAL,
                                reason="the sanchez24a-icml doc is not present")

# THE BASELINE, and no longer what a review reports. This is the fraction of each
# paper's extracted prose that `pdf.render_sections` placed in a lens prompt — the
# truncating renderer `harness.reading` replaced. It is kept and still asserted because it
# is the "before" half of a measured claim: the effect of reading a paper in bounded parts
# is the difference between these numbers and 1.0, and a claim about that effect needs
# both halves computable rather than one of them remembered.
#
# AND SOMEONE DID. These were first measured under extraction version 1 as
# {sanchez 0.384, acl 0.411, iclr 0.489, apt 0.589}. The v2 re-parse changed how many
# sections each paper has — it rejoined a shattered bibliography and dropped bare
# known-heading words, so acl went 25 -> 29 sections and iclr 25 -> 13 — and the budget is
# divided per section, so every fraction moved. This test caught that, which is what it is
# for: the numbers below are keyed to EXTRACTION VERSION 2 and the version is asserted
# alongside them, so a future re-parse fails here rather than silently changing what the
# scope bullet is bounded by.
_MEASURED_PRESENTED_V2 = {
    "0c06a98d7c818f6f": 0.668,
    "2024-icml-sapg": 0.841,
    "5993d35ff0996b52": 0.525,
    "acl": 0.335,
    "apt-icml": 0.611,
    "cvpr": 0.794,
    "iclr": 0.547,
    "sanchez24a-icml": 0.407,
}
# Kept under its original name so no other test in this file has to change.
_MEASURED_PRESENTED = _MEASURED_PRESENTED_V2
_MEASURED_UNDER_EXTRACTION_VERSION = 2


def _decision_source(fn) -> str:
    """The source of one decision function, read off DISK and located by name.

    `inspect.getsource` resolves a function's body by the line number recorded at import
    time, so it returns a mangled span — or raises `TokenError` — whenever the file has
    been edited since. Two agents editing this repo at once made that a real failure of a
    real guard, which is the worst kind: a prohibition that goes green or red for reasons
    unrelated to the prohibition.
    """
    path = Path(inspect.getfile(fn))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == fn.__name__:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                # CODE only. A decision function's own docstring is free to discuss
                # coverage; it is the code that may not consult it.
                body = body[1:] or [ast.Pass()]
            return ast.unparse(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{fn.__name__} is no longer defined in {path}")


def _real_doc(pid: str = "sanchez24a-icml") -> PaperDoc:
    p = Path(f"projects/{pid}/paper/doc.json")
    return PaperDoc(**json.loads(p.read_text(encoding="utf-8")))


def _paper() -> PaperDoc:
    """A small paper with one of every unit, plus one of every excluded shape."""
    return PaperDoc(
        paper_id="fixture", content_sha="0123456789ab", n_pages=3,
        sections=[Section(section_idx=0, title="Method", page_start=1,
                          text="We sample 4 topics x 5 templates x 10 instances = 200 "
                               "cases in total, one per template."),
                  Section(section_idx=1, title="Blank", page_start=2, text="")],
        tables=[Table(table_idx=1, page=2, header=["method", "acc"],
                      rows=[["ours", "61.4"], ["base", ""]])],
        figures=[Figure(figure_idx=1, page=3, label="Figure 1",
                        caption="Accuracy by seed."),
                 Figure(figure_idx=2, page=3, label="Figure 2", caption="")],
        equations=[Equation(equation_idx=1, page=3, number="(1)", text="L = a + b"),
                   Equation(equation_idx=2, page=3, number="(2)", text="")],
        reported_numbers=[
            QuantFinding(value="200", page=1,
                         source_quote="We sample 4 topics x 5 templates x 10 instances = "
                                      "200 cases in total, one per template."),
            QuantFinding(value="61.4", page=2, table_ref="T1:r0:c1", source_quote="61.4"),
        ],
    )


# --------------------------------------------------------------------------- #
# 1. The denominator cannot see the numerator — by signature
# --------------------------------------------------------------------------- #
def test_the_surface_denominator_cannot_see_the_harnesss_own_objects():
    """Prevents the published defect: a denominator computed from the objects the harness
    minted is the numerator's own set, so a failed extraction removes denominator rows and
    RAISES the rate. `surface` takes a PaperDoc and there is no second parameter to pass a
    TargetSet through."""
    params = inspect.signature(coverage.surface).parameters
    assert list(params) == ["doc"], params
    assert "PaperDoc" in str(params["doc"].annotation)

    src = inspect.getsource(coverage)
    for review_type in ("TargetSet", "DiscoveredObject", "PlanDecision", "TargetOutcome",
                        "ReviewQuestion", "LensReport"):
        assert f"import {review_type}" not in src, review_type
        assert f"{review_type}(" not in src, review_type
    # nor the predicate the published rate divided by itself. Checked over the CODE and
    # not the prose: `surface`'s docstring names `harness_addressable` as the thing it
    # must not read, and a naive substring test over the file would forbid saying so.
    body = inspect.getsource(coverage.surface).split('"""')[2]
    assert "harness_addressable" not in body
    assert "reported_number" not in body or "doc.reported_numbers" in body


def test_the_coverage_module_imports_nothing_that_could_hand_it_a_finding():
    """Prevents slow re-entry of the defect: a pure measurement module that imports the
    discovery, grading or reporting layers acquires access to the review's own objects one
    refactor later, and the signature guarantee above becomes advisory."""
    src = inspect.getsource(coverage)
    for module in ("discovery", "planner", "priority", "grading", "ledger", "evaluation",
                   "taxonomy", "questions", "stages"):
        assert f"from . import {module}" not in src, module
        assert f"from .{module} import" not in src, module
    assert "from .stages" not in src, "a pure measurement module may not import a stage"
    # The whole import surface, pinned: `Finding`, `TargetSet` and the rest of the
    # review's own vocabulary are not in it, and adding one is a visible edit here.
    assert "from .artifacts import (CoverageReport, PaperDoc, ReadingRecord, ReviewSurface,\n" \
           "                        Section, SURFACE_KINDS)" in src
    assert "from . import claims, pdf" in src, "claims for the address grammar, pdf for "\
                                               "the lens budget, and nothing else"
    # `harness.reading` is reached, function-locally, exactly as `pdf` is. It is admissible
    # here for the reason `pdf` is: its whole input is a `PaperDoc` and its whole output is
    # a traversal of that document, so it can no more hand this module a finding than the
    # extractor can. Pinned so that adding a second, freer import is a visible edit.
    assert "from .reading import plan as reading_plan" in src
    for banned in ("Finding", "TargetSet", "DiscoveredObject", "TargetOutcome"):
        assert f"import {banned}" not in src and f", {banned}" not in src, banned


def test_a_numerator_reaches_the_denominator_only_as_an_address_string():
    """Prevents the numerator redefining the denominator: an object in a numerator could
    be asked whether it was addressable, which is how `targets_addressable /
    targets_discovered` came to divide one list by itself. Addresses are strings, and an
    object is refused rather than stringified into an off-surface row."""
    params = inspect.signature(coverage.measure).parameters
    assert list(params) == ["surf", "addressed", "examined", "reading"], params
    for name in ("addressed", "examined"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert "tuple[str" in str(params[name].annotation), params[name].annotation
    # The one argument that is neither numerator nor denominator. It describes HOW THE
    # PAPER WAS READ and is carried through untouched; it is a `ReadingRecord` and not a
    # `TargetSet`, so no count derived from the review's own results can reach the
    # denominator through it. A record is a fact about the reading, not about the findings.
    assert params["reading"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "ReadingRecord" in str(params["reading"].annotation)
    assert not any(field in ReadingRecord.model_fields
                   for field in ("findings", "targets", "addressed", "examined",
                                 "severity", "verdict"))

    doc = _paper()
    surf = coverage.surface(doc)
    obj = discovery.discover(doc, [], [])[0][0]
    with pytest.raises(TypeError, match="address string"):
        coverage.measure(surf, addressed=(obj,))
    with pytest.raises(TypeError, match="address string"):
        coverage.measure(surf, examined=(7,))


def test_no_coverage_value_can_reach_a_threshold_a_gate_or_a_colour():
    """Invariant 17: a colour may not be a property of this harness's configuration, and
    coverage is exactly such a property — it moves with `SH_MAX_TARGETS`, with the audit
    budget and with extraction quality. A paper whose surface is large must not become
    YELLOW for being large."""
    for fn in (grading.derive, planner.classify, priority.score,
               report_stage.overall_verdict, report_stage.claim_status, report_stage.triage):
        names = set(inspect.signature(fn).parameters)
        assert not (names & {"coverage", "surface", "addressed_rate", "examined_rate",
                             "surface_size", "coverage_report"}), (fn.__name__, names)
        src = _decision_source(fn)
        assert "coverage" not in src and "surface" not in src, fn.__name__

    for model in (ReviewSurface, CoverageReport):
        for banned in ("severity", "counted_severity", "verdict", "triage", "claim_status",
                       "confidence", "scientific_class", "resolution_status"):
            assert banned not in model.model_fields, (model.__name__, banned)


# --------------------------------------------------------------------------- #
# 2. The denominator is the paper, not our object list
# --------------------------------------------------------------------------- #
@needs_real
def test_an_extraction_failure_cannot_move_the_published_rate_and_does_move_the_surface():
    """The defect this module replaces, reproduced on a real paper. Removing eight of
    sanchez24a-icml's sixteen tables destroys 376 addressable units; the published
    `targets_addressable / targets_discovered` does not change by one digit, because both
    of its terms lost the same rows. The surface denominator falls 1002 -> 626 and the 147
    addresses the review can no longer reach are named."""
    doc = _real_doc()
    surf = coverage.surface(doc)
    addressed = tuple(surf.addresses)

    worse = doc.model_copy(deep=True)
    worse.tables = doc.tables[:len(doc.tables) // 2]
    surf2 = coverage.surface(worse)

    _objs, cov = discovery.discover(doc, [], [])
    _objs2, cov2 = discovery.discover(worse, [], [])
    published = cov["objects_addressable"] / cov["objects_discovered"]
    published_worse = cov2["objects_addressable"] / cov2["objects_discovered"]
    assert published == published_worse == 1.0, (published, published_worse)
    assert cov2["objects_discovered"] < cov["objects_discovered"], \
        "the lost cells left the DENOMINATOR, which is why the rate could not fall"

    before = coverage.measure(surf, addressed=addressed)
    after = coverage.measure(surf2, addressed=addressed)
    assert after.surface_size < before.surface_size
    assert after.addressed < before.addressed
    assert after.addressed_rate <= before.addressed_rate
    assert len(after.off_surface) == before.addressed - after.addressed > 0, \
        "every unit the review can no longer reach is named, not silently dropped"


def test_degrading_the_document_never_raises_a_rate_the_review_had_earned():
    """The monotonicity theorem this layer guarantees, swept over all sixteen degradations
    of a document rather than checked on one.

    Losing a unit the review ADDRESSED removes it from the numerator and the denominator
    together, so no rate can rise from the extractor — or the review — failing to reach
    something. That is exactly what `target_addressability_rate` cannot say: there, a
    reference that fails to resolve never becomes an object, so the loss leaves both terms
    and the rate does not fall.

    The condition is computed per degradation rather than assumed, and where it does not
    hold the test asserts the alternative guarantee: the denominator visibly fell."""
    doc = _paper()
    surf = coverage.surface(doc)
    units = set(surf.addresses)
    # every unit of two kinds, so that some degradations lose only addressed units
    addressed = tuple(a for a in surf.addresses if coverage.kind_of(a) in
                      ("table_cell", "figure"))
    base = coverage.measure(surf, addressed=addressed, examined=addressed)
    assert 0.0 < base.addressed_rate < 1.0, "a rate with room to move in either direction"

    seen_theorem = seen_residual = 0
    for drop_tables in (False, True):
        for drop_figures in (False, True):
            for drop_equations in (False, True):
                for drop_sections in (False, True):
                    worse = doc.model_copy(deep=True)
                    if drop_tables:
                        worse.tables = []
                    if drop_figures:
                        worse.figures = []
                    if drop_equations:
                        worse.equations = []
                    if drop_sections:
                        worse.sections = []
                    new = coverage.surface(worse)
                    got = coverage.measure(new, addressed=addressed, examined=addressed)
                    where = (drop_tables, drop_figures, drop_equations, drop_sections)
                    assert got.surface_size <= base.surface_size, where
                    assert got.addressed <= base.addressed, where
                    assert got.examined <= base.examined, where
                    lost = units - set(new.addresses)
                    if lost <= set(addressed):
                        seen_theorem += 1
                        assert got.addressed_rate is None \
                            or got.addressed_rate <= base.addressed_rate, where
                        assert got.examined_rate is None \
                            or got.examined_rate <= base.examined_rate, where
                    elif lost:
                        seen_residual += 1
                        assert got.surface_size < base.surface_size, where
    assert seen_theorem >= 4 and seen_residual >= 4, (seen_theorem, seen_residual)


def test_a_rate_that_rises_because_unaddressed_content_vanished_shows_a_smaller_surface():
    """The one channel this measure does NOT close, pinned so it cannot be forgotten. Any
    ratio whose denominator is what extraction recovered rises when content the review
    never addressed is removed, and no quantity on `PaperDoc` except `n_pages` survives
    such a deletion — two orders of magnitude too small to anchor a real surface. What is
    guaranteed instead: `surface_size` falls whenever this happens, so the rate is never
    quotable without a visibly smaller denominator beside it."""
    doc = _paper()
    surf = coverage.surface(doc)
    addressed = ("T1:r0:c0",)
    before = coverage.measure(surf, addressed=addressed)

    lost = doc.model_copy(deep=True)
    lost.figures = []          # a unit nothing addressed
    after = coverage.measure(coverage.surface(lost), addressed=addressed)

    assert after.addressed == before.addressed, "the numerator did not change"
    assert after.addressed_rate > before.addressed_rate, \
        "this is the residual channel; if it ever stops happening, the docstring is stale"
    assert after.surface_size < before.surface_size, \
        "and it is never invisible: the denominator is printed and it fell"


def test_empty_padding_cells_are_not_addressable_surface():
    """Prevents a denominator inflated by ragged extraction: 556 of sanchez24a-icml's
    1,474 row/column slots hold nothing and `pdf.render_tables` skips them, so no lens was
    ever shown them. A denominator that counts them is 60% padding, and an implementation
    is free to pick whichever of the two numbers flatters the ratio."""
    ragged = PaperDoc(paper_id="ragged", n_pages=1, tables=[
        Table(table_idx=1, page=1, header=["a", "b", "c"],
              rows=[["1", "", ""], ["2", "3", ""], ["", "", ""]])])
    surf = coverage.surface(ragged)
    assert surf.table_cells_total == 9 and surf.table_cells_nonempty == 3
    assert surf.by_kind["table_cell"] == 3
    assert sorted(surf.addresses) == ["T1:r0:c0", "T1:r1:c0", "T1:r1:c1"]

    # ...and the surface is exactly what the lens prompt presents
    shown = {line.split("=")[0].strip()
             for block in pdf.render_tables(ragged.tables).splitlines()
             for line in block.split(" | ") if line.strip().startswith("T1:")}
    assert shown == set(surf.addresses), (shown, surf.addresses)


def test_a_table_header_cell_is_not_counted_as_surface_because_nothing_can_address_it():
    """Prevents counting an unaddressable unit: `Table.ref` indexes `rows`, which excludes
    the header, so a header cell has no address `claims.resolve` can re-derive — 23 such
    cells on sanchez24a-icml, 56 on apt-icml. A denominator containing them can never be
    fully addressed by any review, however good."""
    doc = PaperDoc(paper_id="hdr", n_pages=1, tables=[
        Table(table_idx=1, page=1, header=["method", "acc"], rows=[["ours", "61.4"]])])
    surf = coverage.surface(doc)
    assert surf.by_kind["table_cell"] == 2
    assert claims.resolve(doc, "T1:r0:c0").quote == "ours", "row 0 is the first DATA row"
    for addr in surf.addresses:
        assert claims.resolve(doc, addr).resolved


def test_a_reported_number_bound_to_a_cell_is_counted_once():
    """Prevents double counting across kinds: every `QuantFinding` with a `table_ref` is
    also a table cell, so `cells + numbers` over-counts the paper — 391 numbers against
    918 cells on sanchez24a-icml, overlapping. The surface is a SET of addresses."""
    doc = _paper()
    surf = coverage.surface(doc)
    assert len(surf.addresses) == len(set(surf.addresses))
    assert surf.addresses.count("T1:r0:c1") == 1, "the cell the quantity names, once"
    assert surf.by_kind["reported_quantity"] == 1, "only the PROSE quantity adds a unit"
    assert sum(surf.by_kind.values()) == len(surf.addresses)


@needs_real
def test_every_counted_unit_has_an_address_claims_can_re_resolve():
    """Prevents a denominator of things nobody could ever cite: a coverage unit is only a
    unit if the harness can re-derive its address against the same document, which is the
    same rule invariant 1 applies to evidence."""
    doc = _real_doc()
    surf = coverage.surface(doc)
    assert len(surf.addresses) > 900
    for addr in surf.addresses:
        got = claims.resolve(doc, addr)
        assert got.resolved, (addr, got.resolution, got.detail)
        assert coverage.kind_of(addr) in SURFACE_KINDS, addr


@needs_real
def test_the_prose_quantity_shortcut_agrees_with_the_authoritative_minter():
    """Prevents a second address rule: the surface finds a printed quantity's span with
    one prepared index instead of calling `claims.mint` 391 times over 105k characters,
    and a cache that disagrees with the authority mints addresses nothing else can
    resolve."""
    doc = _real_doc()
    prose = [a for a in coverage.surface(doc).addresses if a.startswith("P")]
    assert prose, "sanchez24a-icml prints quantities in its running text"
    quotes = {claims.resolve(doc, a).quote for a in prose}
    for quote in list(quotes)[:25]:
        assert claims.mint(doc, quote).ref in prose, quote


def test_a_surface_is_stamped_with_the_document_it_was_computed_over():
    """Prevents a coverage number that can be quoted beside any paper: no structural
    denominator in this system was pinned to a document identity, so a re-ingest silently
    changed every one of them."""
    doc = _paper()
    surf = coverage.surface(doc)
    assert surf.content_sha == doc.content_sha == "0123456789ab"
    assert coverage.measure(surf).content_sha == doc.content_sha
    assert coverage.measure(surf).paper_id == doc.paper_id


def test_measuring_a_paper_does_not_change_it():
    """Prevents a measurement that alters what it measures: `surface` walks the document's
    own tables and sections, and a layer that normalised a cell or renumbered a section in
    passing would make every later address in the run resolve against a document nobody
    parsed."""
    doc = _paper()
    before = doc.model_dump_json()
    first = coverage.surface(doc)
    second = coverage.surface(doc)
    assert doc.model_dump_json() == before
    assert first.model_dump() == second.model_dump()
    assert coverage.measure(first, addressed=("S0",)).model_dump() == \
        coverage.measure(second, addressed=("S0",)).model_dump()


def test_the_address_set_is_a_function_of_the_document_and_not_of_the_lens_budget():
    """Prevents a denominator that moves with this harness's configuration: the audit
    budget bounds how the paper is traversed, and reporting it as part of the paper's
    surface would make coverage a property of `SH_AUDIT_BUDGET_CHARS`.

    WHAT THE BUDGET BOUNDS HAS CHANGED, and the assertion changed with it. It used to
    bound how much of the paper a reader was shown, so a narrow budget lowered
    `prose_chars_presented`; it now bounds how many bounded passes the paper is read in,
    so a narrow budget raises `reading_parts` and the prose carried is the same. Both
    facts are asserted, because "the denominator does not move" is only half of what this
    test is for — the other half is that the budget still moves SOMETHING, or it has
    stopped being a budget.
    """
    import os
    doc = _paper()
    doc.sections.append(Section(section_idx=2, title="Long", page_start=3, text="z" * 9000))
    wide = coverage.surface(doc)
    os.environ["SH_AUDIT_BUDGET_CHARS"] = "1200"
    try:
        narrow = coverage.surface(doc)
    finally:
        del os.environ["SH_AUDIT_BUDGET_CHARS"]
    assert narrow.addresses == wide.addresses
    assert narrow.by_kind == wide.by_kind and narrow.sections == wide.sections
    assert narrow.prose_chars_presented == wide.prose_chars_presented == \
        wide.prose_chars_total, "a narrower budget must not cost a reader any of the paper"
    assert narrow.reading_parts > wide.reading_parts == 1


# --------------------------------------------------------------------------- #
# 3. Two numerators, never one
# --------------------------------------------------------------------------- #
@needs_real
def test_minting_an_address_is_not_examining_it():
    """Prevents the overclaim a single numerator makes: on sanchez24a-icml the review
    minted addresses for 390 of 1,002 units and pursued a route on 6. Corpus-wide 629 of
    867 targets ended NOT_INVESTIGATED, so a rate over `harness_addressable` would report
    a third of the paper as covered where 1 unit in 167 was looked at."""
    doc = _real_doc()
    ts_path = Path("projects/sanchez24a-icml/discovery/targets.json")
    if not ts_path.is_file():
        pytest.skip("the shipped target set is not present")
    from harness.artifacts import TargetSet
    ts = TargetSet(**json.loads(ts_path.read_text(encoding="utf-8")))
    by_id = {o.target_id: o for o in ts.objects}
    addressed = tuple(o.ref.ref for o in ts.objects if o.ref and o.ref.ref)
    examined = tuple(by_id[o.target_id].ref.ref for o in ts.outcomes
                     if o.target_id in by_id and by_id[o.target_id].ref
                     and by_id[o.target_id].ref.ref
                     and o.resolution_state not in ("", "NOT_INVESTIGATED"))

    rep = coverage.measure(coverage.surface(doc), addressed=addressed, examined=examined)
    assert rep.examined < rep.addressed < rep.surface_size
    assert rep.examined_rate < 0.05 < rep.addressed_rate, rep
    assert rep.off_surface == [], "a real review's minted addresses are all on the surface"


def test_a_run_that_pursues_nothing_reports_no_examined_coverage():
    """Prevents "we addressed it" reading as "we checked it": with the execution gates
    shut — the default — every route is unpursued, and the examined rate must be 0.0
    while the addressed rate is not."""
    surf = coverage.surface(_paper())
    rep = coverage.measure(surf, addressed=tuple(surf.addresses), examined=())
    assert rep.examined == 0 and rep.examined_rate == 0.0
    assert rep.addressed_rate == 1.0


def test_a_pursued_route_is_never_reported_as_more_than_was_addressed():
    """Prevents an arithmetic impossibility reaching a reader as a defect in the paper: a
    unit whose route was pursued was addressed by construction, so a caller that lists one
    and not the other must not produce `examined > addressed`."""
    surf = coverage.surface(_paper())
    rep = coverage.measure(surf, addressed=(), examined=("S0", "T1:r0:c0"))
    assert rep.examined == 2 and rep.addressed == 2
    assert rep.examined <= rep.addressed <= rep.surface_size


def test_an_address_outside_the_surface_is_reported_rather_than_counted():
    """Prevents silent counting of a numerator the denominator does not contain — which
    means the two were computed over different documents, or the enumeration is
    incomplete. Either is a defect and neither is coverage."""
    surf = coverage.surface(_paper())
    rep = coverage.measure(surf, addressed=("T9:r0:c0", "F9", "S9", "T1:r0:c0"),
                           examined=("E9",))
    assert rep.addressed == 1 and rep.examined == 0
    assert rep.off_surface == ["E9", "F9", "S9", "T9:r0:c0"]
    assert rep.addressed_rate == 1 / rep.surface_size


def test_a_lens_page_citation_is_not_a_coverage_unit():
    """Prevents the raw model string entering the measurement: 75 of the corpus's 98
    findings wrote a `p<N>` page reference, which matches none of the five address
    grammars. Counting one would credit coverage to a location nobody can check."""
    surf = coverage.surface(_paper())
    rep = coverage.measure(surf, addressed=("p7", "page 7", "Table 1"))
    assert rep.addressed == 0 and rep.addressed_rate == 0.0
    assert rep.off_surface == ["Table 1", "p7", "page 7"]


def test_a_prose_span_counts_as_the_section_that_holds_it_and_never_twice():
    """Prevents the off-surface channel filling with ordinary citations: a finding minted
    at `P44:0-156` addressed something in section 44, so it counts as that section. If it
    were reported off-surface instead, the defect channel would carry 75 of 98 findings
    and would name nothing."""
    surf = coverage.surface(_paper())
    rep = coverage.measure(surf, addressed=("P0:0-20", "P0:30-50"))
    assert rep.addressed == 1 and rep.off_surface == []
    assert rep.by_kind_addressed["section_span"] == 1
    # a span in a section the document does not have is still a defect
    assert coverage.measure(surf, addressed=("P9:0-20",)).off_surface == ["P9:0-20"]


def test_the_repository_claim_is_not_part_of_the_papers_surface():
    """Prevents counting this harness's own artifact as paper: `discovery` mints an
    IMPLEMENTATION_CLAIM with `ref=None` and force-sets `harness_addressable=True`,
    inflating both terms of any object-based rate by one per paper with a repo. It is a
    claim about a repository, not a unit of the paper, and an empty address is not an
    off-surface defect either."""
    surf = coverage.surface(_paper())
    rep = coverage.measure(surf, addressed=("", None, "T1:r0:c0"))
    assert rep.addressed == 1 and rep.off_surface == []


def test_no_numerator_can_exceed_the_denominator_however_it_is_fed():
    """Prevents a rate above 1.0 from duplicate or repeated numerator rows — the shape a
    coverage layer takes when it counts minted addresses instead of surface units."""
    surf = coverage.surface(_paper())
    fed = tuple(surf.addresses) * 4 + ("P0:0-10", "P0:11-20", "T9:r0:c0")
    rep = coverage.measure(surf, addressed=fed, examined=fed)
    assert rep.addressed == rep.examined == rep.surface_size
    assert rep.addressed_rate == rep.examined_rate == 1.0


# --------------------------------------------------------------------------- #
# 4. An empty surface, and the arithmetic a reader can redo
# --------------------------------------------------------------------------- #
def test_an_empty_surface_is_unmeasurable_and_never_perfect():
    """Prevents "0 of 0 = 100%", the shape `compaction_ratio` already has in the opposite
    direction (a missing review scores 0.0, the best value). A paper extraction recovered
    nothing from must be distinguishable from a paper that was fully addressed."""
    empty = coverage.surface(PaperDoc(paper_id="nothing"))
    assert empty.surface_empty is True and empty.addresses == []
    rep = coverage.measure(empty, addressed=("T1:r0:c0",), examined=("T1:r0:c0",))
    assert rep.addressed_rate is None and rep.examined_rate is None
    assert rep.addressed == 0 and rep.surface_size == 0
    assert rep.off_surface == ["T1:r0:c0"]

    # ...and a real surface nobody addressed is 0.0, WITH surface_empty False
    real = coverage.surface(_paper())
    assert real.surface_empty is False
    assert coverage.measure(real).addressed_rate == 0.0


def test_a_surface_that_declares_itself_empty_yields_no_rate_even_if_it_carries_rows():
    """Prevents a rate over a denominator whose own producer says there was nothing to
    measure — the stale-artifact shape `evaluation.py` already has, where a missing ledger
    becomes an empty `CaseLedger` and its zeros are averaged in as data."""
    stale = ReviewSurface(paper_id="p", addresses=["T1:r0:c0"], surface_empty=True)
    rep = coverage.measure(stale, addressed=("T1:r0:c0",))
    assert rep.addressed_rate is None and rep.examined_rate is None


def test_both_rates_are_recomputable_by_hand_from_the_counts_printed_beside_them():
    """Prevents a rate a reader cannot audit: every other ratio in this system is
    checkable from two printed integers, and a coverage rate whose denominator is not the
    printed `surface_size` is a number only its author can reproduce."""
    surf = coverage.surface(_real_doc()) if _HAS_REAL else coverage.surface(_paper())
    rep = coverage.measure(surf, addressed=tuple(surf.addresses[:3]),
                           examined=tuple(surf.addresses[:1]))
    assert rep.addressed_rate == rep.addressed / rep.surface_size
    assert rep.examined_rate == rep.examined / rep.surface_size
    assert rep.surface_size == len(set(surf.addresses))


def test_structural_coverage_never_claims_to_be_issue_recall():
    """Prevents the only reading of this number that would be a lie: no paper in this
    corpus carries adjudicated ground truth, so whether the review found the issues that
    matter is not machine-detectable — unconditionally, on every report, including a
    perfect one."""
    surf = coverage.surface(_paper())
    for addressed in ((), ("T1:r0:c0",), tuple(surf.addresses)):
        rep = coverage.measure(surf, addressed=addressed)
        assert rep.semantic_coverage == "not_machine_detectable"
    assert coverage.measure(coverage.surface(PaperDoc(paper_id="x"))).semantic_coverage \
        == "not_machine_detectable"


# --------------------------------------------------------------------------- #
# 5. The truncation ceiling — what the lenses were actually shown
# --------------------------------------------------------------------------- #
def test_the_presented_prose_budget_is_the_one_the_audit_stage_actually_uses():
    """Prevents a truncation ceiling measured against a budget nobody applied. The
    constant is mirrored rather than imported — a pure module must not import a stage —
    so the drift is guarded here instead of trusted."""
    assert coverage._BUDGET_DEFAULT == audit_stage.SECTION_BUDGET_CHARS
    assert coverage.budget_chars() == audit_stage.SECTION_BUDGET_CHARS
    assert coverage._MIN_PER_SECTION == 400, "pdf.render_sections' own per-section floor"


def test_the_presented_prose_arithmetic_reproduces_what_the_lens_prompt_contains():
    """Prevents a fabricated ceiling: the fraction is derived from
    `pdf.render_sections`' budget rule, and if that rule changes the number becomes a
    statement about a rule nobody applies. Checked against the renderer's real output."""
    sections = [Section(section_idx=i, title=f"S{i}", page_start=1,
                        text="x" * (500 * (i + 1))) for i in range(5)]
    presented, total, cut = coverage.prose_presented(sections, 2500)
    rendered = pdf.render_sections(sections, 2500)
    text = rendered[0] if isinstance(rendered, tuple) else rendered
    assert cut == text.count("…[truncated]")
    assert total == sum(len(s.text) for s in sections)
    assert presented == sum(min(len(s.text), 500) for s in sections)
    for s in sections:
        assert s.text[:500] in text, "each section's slice really is in the prompt"


@needs_real
@pytest.mark.parametrize("pid,baseline", sorted(_MEASURED_PRESENTED.items()))
def test_the_whole_paper_now_reaches_a_reader_on_every_corpus_document(pid, baseline):
    """The measurement this work exists for, on the real documents, in both directions.

    BEFORE: `pdf.render_sections` divides one character budget across every section and
    hard-slices each, so on `acl` the four lenses were shown 34% of the extracted prose
    while the review's scope bullet said they read the paper. Those fractions are pinned
    in `_MEASURED_PRESENTED_V2` and are still asserted here, because a claim about an
    improvement needs its baseline to be computable rather than remembered.

    AFTER: `harness.reading.plan` traverses the same document in bounded parts that tile
    it, so every character of every extracted section reaches some pass. The assertion is
    exact — 1.0, not "higher than before" — because the guarantee is coverage and a
    guarantee stated as an inequality is a guarantee nobody can fail.

    The pinned baselines belong to an EXTRACTION VERSION, which is checked below. They
    were first measured under v1 and every one moved under v2, because v2 changed how many
    sections a paper has and the old budget was divided per section. A number pinned
    without its version silently stops meaning what it says.
    """
    if not Path(f"projects/{pid}/paper/doc.json").is_file():
        pytest.skip(f"{pid} is not present")
    doc = _real_doc(pid)
    if int(doc.extraction_version or 1) != _MEASURED_UNDER_EXTRACTION_VERSION:
        pytest.skip(f"{pid} is extraction v{doc.extraction_version}; these fractions were "
                    f"measured under v{_MEASURED_UNDER_EXTRACTION_VERSION}")
    # the baseline, still reproducible from the renderer it describes
    old, total, cut = coverage.prose_presented(list(doc.sections), coverage.budget_chars())
    assert total and old / total == pytest.approx(baseline, abs=0.002)
    assert cut > 0, "the baseline is a truncation, so something must have been truncated"

    rep = coverage.measure(coverage.surface(doc))
    assert rep.prose_presented_fraction == 1.0, (
        f"{pid}: a reader must be carried the whole of what extraction recovered")
    assert coverage.surface(doc).reading_parts >= 1


def test_lowering_the_lens_budget_costs_passes_and_never_paper():
    """Prevents the budget quietly becoming a recall ceiling again.

    The old assertion here was that the presented fraction RISES with the budget, which
    was the correct check while the budget decided how much of a paper a reader saw. It is
    the wrong check now and keeping it would have been worse than deleting it: it would
    have passed on any implementation that still truncated. What must hold instead is that
    the fraction is 1.0 at every budget — including one far too small for the paper — and
    that the cost of a small budget shows up as more passes.
    """
    import os
    doc = PaperDoc(paper_id="long", n_pages=2, sections=[
        Section(section_idx=i, title=f"S{i}", page_start=1, text="y" * 4000)
        for i in range(8)])
    fractions, parts = [], []
    for budget in ("4000", "16000", "64000"):
        os.environ["SH_AUDIT_BUDGET_CHARS"] = budget
        try:
            surf = coverage.surface(doc)
            fractions.append(coverage.measure(surf).prose_presented_fraction)
            parts.append(surf.reading_parts)
        finally:
            del os.environ["SH_AUDIT_BUDGET_CHARS"]
    assert fractions == [1.0, 1.0, 1.0], fractions
    assert parts == sorted(parts, reverse=True) and parts[0] > parts[-1] == 1, parts


def test_a_paper_with_no_prose_reports_no_presented_fraction_rather_than_a_full_one():
    """Prevents "0 of 0 characters" reading as "the lens saw all of it" — the same
    zero-denominator defect as the empty surface, on the axis a reader is most likely to
    quote."""
    assert coverage.measure(coverage.surface(PaperDoc(paper_id="x"))) \
        .prose_presented_fraction is None
    only_tables = PaperDoc(paper_id="t", tables=[
        Table(table_idx=1, page=1, rows=[["1"]])])
    rep = coverage.measure(coverage.surface(only_tables))
    assert rep.prose_presented_fraction is None and rep.surface_size == 1
