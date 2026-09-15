"""How much of THE PAPER this review could address, and how much of it it examined.

`python -m harness.coverage` runs the self-check.

**The defect this module exists to replace, which is published.**
`evaluation.target_addressability_rate = targets_addressable / targets_discovered` reads
0.9873 (sanchez24a-icml), 0.9490 (apt-icml), 0.9509 (5993d35ff0996b52) and 0.9331
corpus-wide, and both of its terms are the same `TargetSet.objects` list. That list is
pre-filtered by the very predicate the rate claims to measure: `discovery.discover` mints
an object per `reported_number` only `if ref.resolved`, so a reference the extractor
cannot resolve never becomes an object and therefore never becomes a denominator row. The
rate does not fall when extraction fails — it RISES. A worse extractor scores higher, and
the number is quoted in `manuscript.tex` and asserted by `manuscript/check_claims.py`.

**What replaces it.** The denominator is the paper's own addressable surface, enumerated
from a `PaperDoc` and from nothing the review produced: every non-empty table cell, every
figure that carries a caption, every equation whose body was recovered, every section with
text, and every printed quantity whose sentence occurs exactly once. Each entry is an
address `claims.resolve` can re-derive, and the set is deduplicated — a reported number
bound to a cell is that cell, counted once, not a second unit beside it.

`surface(doc)` takes a `PaperDoc` and nothing else, and `measure(...)` takes its
numerators as plain address STRINGS. That is the guarantee, and it is the same kind
`grading.derive` makes by refusing to admit a count: a numerator that cannot reach the
denominator's construction cannot redefine it. Fabrication is inexpressible here rather
than merely absent, and `tests/test_review_surface_coverage.py` asserts the signatures.

**Two numerators, never one.** `addressed` counts surface units this review minted an
address for; `examined` counts units where a route was actually pursued. Corpus-wide 629
of 867 targets ended `NOT_INVESTIGATED`, so reporting the first as coverage would claim
~93% of a paper on which 72.5% of the targets were never looked at. On sanchez24a-icml the
two read 390 and 6 against a surface of 1,002.

**The ceiling nobody had measured.** `pdf.render_sections` divides a character budget
across sections, so the four lenses read a TRUNCATED paper: 0.384 of the extracted prose
on sanchez24a-icml, 0.411 on acl, 0.489 on iclr, 0.589 on apt-icml — while the review's
scope section says "4 independent lens(es) read the paper". `prose_chars_presented` and
`prose_presented_fraction` are that ceiling, and they bound what any lens could have
found.

**What this module does NOT measure, and cannot.** Issue recall. There is no adjudicated
ground truth for any paper in this corpus, so `CoverageReport.semantic_coverage` is the
constant `not_machine_detectable` — a field rather than a paragraph, because a clean
structural number must not be readable as evidence that the review found what mattered.

**The one place this measure is still extraction-dependent, stated as a limit.** The
denominator is what extraction recovered from the document. Removing a unit the review
ADDRESSED lowers both terms, so no rate can rise from the review failing to address
something (that is the inversion of the published defect, and it is a theorem here). But
removing a unit the review never addressed lowers only the denominator, and any ratio
whose denominator is derived from recovered content rises when unaddressed content is
removed. No quantity on `PaperDoc` except `n_pages` survives such a deletion, and
`n_pages` is two orders of magnitude smaller than a real surface, so it cannot serve as
the anchor. What is guaranteed instead: `surface_size` falls visibly whenever this
happens, and it is reported beside every rate, so the rate is never quotable alone.
"""
from __future__ import annotations

import os
import re

from . import claims, pdf
from .artifacts import (CoverageReport, PaperDoc, ReadingRecord, ReviewSurface,
                        Section, SURFACE_KINDS)

# The lens prompt's own budget, mirrored rather than imported. `stages/audit.py` is a
# STAGE — it pulls in the drivers, the config and the prompt package — and a pure
# measurement module that imports a stage inverts the dependency and invites the import
# cycle the coordinator's wiring would create the moment `audit` records this number.
# The duplication is a real cost, so it is guarded twice instead of trusted:
# `tests/test_review_surface_coverage.py` asserts this default equals
# `stages.audit.SECTION_BUDGET_CHARS`, and `_self_check` asserts the arithmetic below
# reproduces what `pdf.render_sections` actually emits.
_BUDGET_ENV = "SH_AUDIT_BUDGET_CHARS"
_BUDGET_DEFAULT = 70000
# `pdf.render_sections`' own floor: no section is allotted less than this, however many
# sections there are. Losing it would understate the presented fraction on a paper with
# many sections, i.e. overstate the truncation ceiling.
_MIN_PER_SECTION = 400

# The address grammars, exactly as `claims.resolve` re-derives them. A sixth shape is not
# a coverage unit: an address this harness cannot re-resolve is not an address.
_CELL = re.compile(r"^T(\d+):r(\d+):c(\d+)$")
_FIG = re.compile(r"^F(\d+)$")
_EQ = re.compile(r"^E(\d+)$")
_SEC = re.compile(r"^S(\d+)$")
_PROSE = re.compile(r"^P(\d+):(\d+)-(\d+)$")

# `claims.mint` refuses a quote shorter than this, so a quantity whose sentence is shorter
# addresses nothing in particular and is not a unit of the surface. Same floor, same
# reason: a four-character quote that happens to be unique is unique by accident.
_QUOTE_MIN = 8


def budget_chars() -> int:
    """The lens prompt's section budget, read at call time.

    Read from the environment on every call rather than captured at import: the whole
    point of `prose_presented_fraction` is that it MOVES when the budget moves, and a
    module-level constant would report the budget of whichever process imported first.
    """
    raw = (os.environ.get(_BUDGET_ENV) or "").strip()
    try:
        return int(raw) if raw else _BUDGET_DEFAULT
    except ValueError:
        # A malformed budget is not a reason to invent one silently in the OTHER
        # direction: `pdf.render_sections` would receive the same string and fail, so the
        # honest fallback is the documented default.
        return _BUDGET_DEFAULT


def kind_of(address: str = "") -> str:
    """Which `SURFACE_KINDS` unit a SURFACE address names, or '' for none.

    A prose span is `reported_quantity` because the only prose spans a surface holds are
    the quantities the paper printed in its running text. A lens's arbitrary prose
    quotation is not a unit of the surface — it is folded onto the section that contains
    it by `measure`, or reported as off-surface.
    """
    a = (address or "").strip()
    if _CELL.match(a):
        return "table_cell"
    if _FIG.match(a):
        return "figure"
    if _EQ.match(a):
        return "equation"
    if _SEC.match(a):
        return "section_span"
    if _PROSE.match(a):
        return "reported_quantity"
    return ""


# --------------------------------------------------------------------------- #
# The denominator
# --------------------------------------------------------------------------- #
def prose_presented(sections: list[Section], budget: int) -> tuple[int, int, int]:
    """(presented, total, sections_truncated) UNDER THE OLD TRUNCATING RENDERER.

    Kept, and no longer on the path that produces a review's coverage number. It measures
    `pdf.render_sections`, which divides one budget across every section and hard-slices
    each — the mechanism `harness.reading` replaced, and the one that showed readers 33%
    to 84% of the evaluated corpus's prose. `prose_visible` below is what a review now
    reports.

    Retained rather than deleted because it is the BASELINE: the difference between what
    these two functions return on the same paper is the whole measured effect of reading
    in parts, and a claim about that effect needs both halves computable. It still
    delegates to `pdf.section_presentation` so it cannot drift from the renderer it
    describes.
    """
    from .pdf import section_presentation
    p = section_presentation(list(sections or []), int(budget))
    return p.presented_chars, p.total_chars, p.sections_truncated


def prose_visible(doc: PaperDoc, budget: int) -> tuple[int, int, int]:
    """(visible, total, parts) — what a reader was ACTUALLY carried, under the plan.

    Delegates to `harness.reading.plan`, the same call `stages/audit` makes to build the
    prompts, so the number a review PRINTS is computed from the same traversal the readers
    were given rather than from a second model of it. That is the property
    `prose_presented` had against `render_sections` and it is the property that matters:
    a coverage figure derived independently of the prompts is a figure about nothing.
    """
    from .reading import plan as reading_plan
    cov = reading_plan(doc, int(budget)).coverage
    return cov.part_local_chars, cov.extracted_prose_chars, cov.parts


def _prose_quantity_addresses(doc: PaperDoc) -> list[str]:
    """The paper's printed quantities that are NOT already table cells, at their minted
    prose addresses.

    Two rules, both borrowed from `claims.mint` rather than invented here:
      * a quantity carrying a `table_ref` that resolves IS that cell, and the cell is
        already enumerated — counting it again would double-count the same unit, which is
        the arithmetic that makes `cells + numbers + sections` exceed the paper;
      * a sentence occurring more than once addresses nothing, so it is not a unit.

    The occurrence search runs over `claims.section_units(doc)` computed ONCE. Calling
    `claims.mint` per quantity re-flattens every section per call — 391 quantities against
    105k characters of prose on sanchez24a-icml — and a denominator nobody can afford to
    compute is a denominator nobody computes. `_self_check` asserts this agrees with
    `claims.mint` address for address, so the shortcut is a cache and not a second rule.
    """
    units = claims.section_units(doc)
    out: list[str] = []
    for num in doc.reported_numbers:
        ref = (num.table_ref or "").strip()
        if ref and claims.resolve(doc, ref).resolved:
            continue
        flat_quote = claims.flatten(num.source_quote or "")[0]
        if len(flat_quote) < _QUOTE_MIN:
            continue
        hits: list[str] = []
        for section_idx, flat, _offsets, _original, _page in units:
            start = flat.find(flat_quote)
            while start != -1 and len(hits) < 2:
                hits.append(f"P{section_idx}:{start}-{start + len(flat_quote)}")
                start = flat.find(flat_quote, start + 1)
            if len(hits) > 1:
                break
        if len(hits) == 1:
            out.append(hits[0])
    return out


def surface(doc: PaperDoc) -> ReviewSurface:
    """The paper's addressable surface. A `PaperDoc` ONLY.

    No `TargetSet`, no `DiscoveredObject`, no `Finding`, no `PlanDecision` and no count
    derived from any of them may reach this function, and the signature is what says so:
    the denominator cannot be a function of the numerator if it cannot see it.

    Excluded, deliberately, each for a stated reason:
      * empty padding cells — 556 of sanchez24a-icml's 1,474 row/column slots, 65% of
        `2024-icml-sapg`'s. `pdf.render_tables` skips them, so no lens was ever shown
        them; a denominator that counts them would be inflated by ragged extraction.
        `table_cells_total` records them anyway, so the gap is visible.
      * table header cells — `Table.ref` indexes `rows`, which excludes the header, so a
        header cell has no address `claims.resolve` can re-derive. 23 cells on
        sanchez24a-icml, 56 on apt-icml. Counting an unaddressable unit as surface would
        make the denominator include rows nothing could ever address.
      * a figure with no caption and an equation with no recovered body — the address
        resolves, but the unit carries no citable text, and the surface is what could be
        quoted.
      * the repository. `discovery` mints an `IMPLEMENTATION_CLAIM` object with `ref=None`
        and force-sets `harness_addressable=True`; it is a claim about an artifact, not a
        unit of the paper, and it inflates every object-based rate by exactly one per
        paper with a repo.
    """
    # Insertion-ordered dict, not a list: the surface is a SET of addresses. Two tables
    # extracted with the same `table_idx`, or a quantity minted at a span another quantity
    # already occupies, would otherwise each add a row that resolves to the same unit.
    addresses: dict[str, str] = {}
    cells_total = 0

    for table in doc.tables:
        for r, row in enumerate(table.rows):
            for c, value in enumerate(row):
                cells_total += 1
                if (value or "").strip():
                    addresses.setdefault(table.ref(r, c), "table_cell")

    for fig in doc.figures:
        if (fig.caption or "").strip():
            addresses.setdefault(fig.ref(), "figure")

    for eq in doc.equations:
        if (eq.text or "").strip():
            addresses.setdefault(eq.ref(), "equation")

    sections = 0
    for sec in doc.sections:
        if (sec.text or "").strip():
            sections += 1
            addresses.setdefault(f"S{sec.section_idx}", "section_span")

    for ref in _prose_quantity_addresses(doc):
        addresses.setdefault(ref, "reported_quantity")

    by_kind = {k: 0 for k in SURFACE_KINDS}
    for kind in addresses.values():
        by_kind[kind] = by_kind.get(kind, 0) + 1

    presented, total, parts = prose_visible(doc, budget_chars())
    return ReviewSurface(
        paper_id=doc.paper_id, content_sha=doc.content_sha,
        addresses=list(addresses), by_kind=by_kind,
        table_cells_nonempty=by_kind.get("table_cell", 0), table_cells_total=cells_total,
        sections=sections, prose_chars_total=total, prose_chars_presented=presented,
        reading_parts=parts,
        surface_empty=not addresses,
    )


def truncated_sections(doc: PaperDoc) -> int:
    """How many sections the OLD truncating renderer would cut at the current budget.

    A measurement of the alternative, not of this run: nothing is truncated under the
    reading plan. Kept because it is the other half of the comparison — "38% of the prose,
    and 24 of 56 sections cut" says whether the loss was spread or local, and that is what
    a reader needs to judge how much the replacement was worth. It is deliberately not
    printed in a review, where it would read as a property of the review that produced it.
    """
    return prose_presented(list(doc.sections), budget_chars())[2]


# --------------------------------------------------------------------------- #
# The numerators
# --------------------------------------------------------------------------- #
def _fold(address: str, units: set[str]) -> str:
    """The surface unit an address names, or '' when the surface has no such unit.

    Exact membership first, then a prose span onto the section that contains it: a
    finding minted at `P44:0-156` addressed something in section 44, and the section is
    the unit of the surface that holds it. Without the fold, 75 of the corpus's 98
    findings would land in `off_surface` — the channel would be full of ordinary prose
    citations and would no longer name the defect it exists to name.

    The order matters and is deliberate: a span that IS a surface unit (a printed
    quantity) counts as that quantity, not as its whole section, so the finer unit is
    never coarsened when it exists.
    """
    a = (address or "").strip()
    if not a:
        return ""
    if a in units:
        return a
    m = _PROSE.match(a)
    if m:
        section = f"S{int(m.group(1))}"
        if section in units:
            return section
    return ""


def _fold_all(addresses: tuple[str, ...], units: set[str]) -> tuple[set[str], list[str]]:
    """(units named, addresses that name none). An empty address is neither: it is not an
    address at all — `IMPLEMENTATION_CLAIM` carries `ref=None` — and filing it as
    off-surface would report the repository as a defect in the paper's enumeration."""
    named: set[str] = set()
    off: list[str] = []
    for raw in addresses or ():
        if raw is not None and not isinstance(raw, str):
            # Refused, not coerced. `str(some_object)` would turn a DiscoveredObject into
            # a numerator row that names no unit, and the report would carry it as an
            # off-surface defect instead of as the caller error it is. The numerators are
            # address strings; that is the whole guarantee of this layer's signature.
            raise TypeError(
                f"a coverage numerator is an address string, not {type(raw).__name__}: "
                "pass the minted address, never the object it came from")
        a = (raw or "").strip()
        if not a:
            continue
        unit = _fold(a, units)
        if unit:
            named.add(unit)
        elif a not in off:
            off.append(a)
    return named, off


def measure(surf: ReviewSurface, *, addressed: tuple[str, ...] = (),
            examined: tuple[str, ...] = (),
            reading: ReadingRecord | None = None) -> CoverageReport:
    """Two numerators over one denominator, from plain address STRINGS.

    The numerators arrive as strings and never as objects, so nothing about how they were
    produced can reach the denominator: `measure` cannot consult a `TargetSet`, cannot ask
    whether a reference resolved, and cannot add a row to the surface. That is why a
    failed extraction lowers a rate here instead of raising one.

    `examined` is folded into `addressed`: a unit whose route was pursued was addressed by
    construction, and a caller that lists one and not the other would otherwise produce
    `examined > addressed` — an arithmetic impossibility that reads as a defect in the
    paper rather than in the caller.

    An address the surface does not contain raises NEITHER numerator. It is named in
    `off_surface`, because it means the two halves were computed over different documents
    or the enumeration is incomplete, and a coverage layer that silently counted it would
    be measuring its own inputs again.
    """
    units = [a for a in (surf.addresses or ()) if (a or "").strip()]
    unit_set = set(units)
    # A surface that declares itself empty is treated as empty even if it carries rows:
    # `surface_empty` is written by the enumerator, and a rate computed against a
    # denominator whose own producer says there was nothing to measure is not auditable.
    empty = bool(surf.surface_empty) or not unit_set

    named_addressed, off_a = _fold_all(tuple(addressed or ()), unit_set)
    named_examined, off_e = _fold_all(tuple(examined or ()), unit_set)
    named_addressed |= named_examined
    off = sorted(set(off_a) | set(off_e))

    size = len(unit_set)
    by_kind_addressed = {k: 0 for k in SURFACE_KINDS}
    for unit in named_addressed:
        kind = kind_of(unit)
        if kind:
            by_kind_addressed[kind] = by_kind_addressed.get(kind, 0) + 1

    fraction = (surf.prose_chars_presented / surf.prose_chars_total
                if surf.prose_chars_total else None)
    return CoverageReport(
        paper_id=surf.paper_id, content_sha=surf.content_sha, surface_size=size,
        addressed=len(named_addressed), examined=len(named_examined),
        off_surface=off,
        # None, never 1.0. "0 of 0" is unmeasurable, and the difference between a paper
        # nothing could be extracted from and a paper fully addressed is the whole
        # question this layer was added to answer.
        addressed_rate=None if empty else len(named_addressed) / size,
        examined_rate=None if empty else len(named_examined) / size,
        prose_presented_fraction=fraction,
        # Carried through, not recomputed. It is produced by `stages/audit.reading_record`,
        # which can see which readings were actually completed — a fact about the review,
        # and therefore a fact this function must receive rather than derive, for the same
        # reason its numerators arrive as strings.
        reading=reading,
        by_kind_addressed=by_kind_addressed,
    )


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    from .artifacts import Equation, Figure, QuantFinding, Section, Table

    # --- the signature IS the guarantee ------------------------------------------------
    params = inspect.signature(surface).parameters
    assert list(params) == ["doc"], params
    assert "PaperDoc" in str(params["doc"].annotation), params["doc"].annotation
    src = inspect.getsource(inspect.getmodule(surface))
    for forbidden in ("TargetSet", "DiscoveredObject", "PlanDecision", "TargetOutcome",
                      "harness_addressable"):
        # Named in the docstring as the thing NOT read; never imported, never touched.
        assert f"import {forbidden}" not in src and f"{forbidden}(" not in src, forbidden

    mp = inspect.signature(measure).parameters
    assert list(mp) == ["surf", "addressed", "examined", "reading"], mp
    for name in ("addressed", "examined"):
        assert mp[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert "tuple[str" in str(mp[name].annotation), mp[name].annotation
    # `reading` is the one argument that is neither a numerator nor a denominator: it is a
    # record of HOW THE PAPER WAS READ, which this function carries through untouched. It
    # is a typed record and not a `TargetSet`, so the guarantee above is unchanged — no
    # count derived from the review's own results can reach the denominator through it.
    assert mp["reading"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "ReadingRecord" in str(mp["reading"].annotation), mp["reading"].annotation

    # --- an empty surface is unmeasurable, and never perfect ---------------------------
    empty = surface(PaperDoc(paper_id="empty"))
    assert empty.surface_empty is True and empty.addresses == []
    zero = measure(empty, addressed=("T1:r0:c0",), examined=())
    assert zero.addressed_rate is None and zero.examined_rate is None, zero
    assert zero.addressed == 0 and zero.off_surface == ["T1:r0:c0"], zero
    assert zero.semantic_coverage == "not_machine_detectable"

    # --- a real, small paper -----------------------------------------------------------
    doc = PaperDoc(
        paper_id="selfcheck", content_sha="abc123abc123", n_pages=4,
        sections=[Section(section_idx=0, title="Method", page_start=1,
                          text="We sample 58 topics x 5 templates x 10 instances = 2,900 "
                               "cases in total, one per template."),
                  Section(section_idx=1, title="Empty", page_start=2, text="")],
        tables=[Table(table_idx=1, page=2, header=["method", "acc"],
                      rows=[["ours", "61.4"], ["base", ""]])],
        figures=[Figure(figure_idx=1, page=3, label="Figure 1",
                        caption="Accuracy by seed."),
                 Figure(figure_idx=2, page=3, label="Figure 2", caption="")],
        equations=[Equation(equation_idx=1, page=3, number="(1)", text="L = a + b"),
                   Equation(equation_idx=2, page=3, number="(2)", text="")],
        reported_numbers=[
            QuantFinding(value="2,900", page=1,
                         source_quote="We sample 58 topics x 5 templates x 10 instances "
                                      "= 2,900 cases in total, one per template."),
            QuantFinding(value="61.4", page=2, table_ref="T1:r0:c1", source_quote="61.4"),
        ],
    )
    surf = surface(doc)
    assert surf.content_sha == "abc123abc123", "a surface with no document identity can "\
                                               "be quoted beside any paper"
    assert surf.table_cells_nonempty == 3 and surf.table_cells_total == 4, surf.by_kind
    assert surf.by_kind["figure"] == 1, "a caption-less figure carries no citable text"
    assert surf.by_kind["equation"] == 1, "an equation with no recovered body is not a unit"
    assert surf.by_kind["section_span"] == 1, "an empty section is not addressable"
    assert surf.by_kind["reported_quantity"] == 1, surf.by_kind
    assert len(surf.addresses) == len(set(surf.addresses)), "the surface is a SET"
    assert surf.surface_empty is False and len(surf.addresses) == 7, surf.addresses

    # a quantity bound to a cell is that cell, counted ONCE
    assert "T1:r0:c1" in surf.addresses
    assert sum(1 for a in surf.addresses if a.startswith("T1:r0:c1")) == 1
    # ...and every counted unit has an address `claims` can re-resolve
    for addr in surf.addresses:
        got = claims.resolve(doc, addr)
        assert got.resolved, (addr, got.resolution, got.detail)
        assert kind_of(addr) in SURFACE_KINDS, addr

    # the prose-quantity shortcut is a CACHE of `claims.mint`, not a second rule
    (prose_addr,) = [a for a in surf.addresses if a.startswith("P")]
    assert claims.mint(doc, doc.reported_numbers[0].source_quote).ref == prose_addr

    # --- two numerators, never one -----------------------------------------------------
    rep = measure(surf, addressed=("T1:r0:c0", "T1:r0:c1", "S0"), examined=("S0",))
    assert rep.surface_size == 7 and rep.addressed == 3 and rep.examined == 1, rep
    assert rep.examined <= rep.addressed <= rep.surface_size
    assert abs(rep.addressed_rate - 3 / 7) < 1e-12
    assert abs(rep.examined_rate - 1 / 7) < 1e-12
    assert rep.by_kind_addressed["table_cell"] == 2
    assert rep.by_kind_addressed["section_span"] == 1
    # minting an address is not examining it
    assert rep.addressed != rep.examined, "the two claims are different claims"

    # a route pursued on a unit the caller forgot to list as addressed is still addressed
    folded = measure(surf, addressed=(), examined=("S0",))
    assert folded.addressed == 1 and folded.examined == 1

    # a real surface with nothing addressed reports 0.0, WITH surface_empty False
    none_addressed = measure(surf)
    assert none_addressed.addressed_rate == 0.0 and none_addressed.surface_size == 7

    # --- off-surface is a defect channel, not a bucket ---------------------------------
    off = measure(surf, addressed=("T9:r0:c0", "p7", "", "S0"))
    assert off.addressed == 1 and off.off_surface == ["T9:r0:c0", "p7"], off
    assert "" not in off.off_surface, "the repository claim carries ref=None and is not an "\
                                      "address; it is in neither term"
    # an empty padding cell was never shown to a lens and is not surface
    assert _fold("T1:r1:c1", set(surf.addresses)) == ""

    # an object in a numerator is a caller error and is refused, never stringified
    try:
        measure(surf, addressed=(doc,))
    except TypeError as exc:
        assert "address string" in str(exc), exc
    else:
        raise AssertionError("a non-address numerator must be refused")

    # a prose span folds onto the section that holds it, and a finer unit still wins
    assert _fold("P0:5-40", set(surf.addresses)) == "S0"
    assert _fold(prose_addr, set(surf.addresses)) == prose_addr
    assert _fold("P7:0-10", set(surf.addresses)) == "", "a section the paper does not have"

    # --- NOT self-healing, in the direction that is a theorem -------------------------
    # Removing a unit the review ADDRESSED removes it from both terms, so no rate can
    # rise from the review or the extractor failing to produce an address. This is the
    # exact inversion of `target_addressability_rate`, where an unresolvable reference
    # leaves the DENOMINATOR and the rate goes up.
    addressed_all = tuple(surf.addresses)
    before = measure(surf, addressed=addressed_all)
    worse = doc.model_copy(deep=True)
    worse.tables = []
    after = measure(surface(worse), addressed=addressed_all)
    assert after.addressed_rate <= before.addressed_rate, (before, after)
    assert after.surface_size < before.surface_size, "the denominator is the PAPER"
    assert len(after.off_surface) == 3, "the cells the review addressed are named as lost"

    # ...and a numerator can never exceed the surface, however it is fed
    flood = measure(surf, addressed=tuple(surf.addresses) * 3 + ("T1:r0:c0",))
    assert flood.addressed == flood.surface_size == 7

    # --- the truncation ceiling --------------------------------------------------------
    # The arithmetic must reproduce what `pdf.render_sections` really emits, or the
    # fraction is a number about a rule nobody applied.
    many = [Section(section_idx=i, title=f"S{i}", page_start=1, text="x" * 900)
            for i in range(6)]
    presented, total, cut = prose_presented(many, 3000)
    rendered = pdf.render_sections(many, 3000)
    text = rendered[0] if isinstance(rendered, tuple) else rendered
    assert cut == text.count("…[truncated]") == 6, (cut, text.count("…[truncated]"))
    assert presented == 6 * 500 and total == 6 * 900, (presented, total)
    # the per-section floor is `render_sections`' own: a budget of 1 still shows 400
    # characters per section, and dropping the floor here would report a presented
    # fraction smaller than the one the lens was actually given.
    assert prose_presented(many, 1) == (6 * _MIN_PER_SECTION, 6 * 900, 6)
    # WHAT THE BUDGET NOW BOUNDS. It used to bound how much of the paper a reader saw;
    # it bounds how many passes the paper takes. The fraction stays 1.0 at every budget,
    # which is the whole point, so the thing that must still MOVE with the budget is the
    # part count — and a check that no longer moves with its input is a check that has
    # stopped testing anything.
    os.environ[_BUDGET_ENV] = "1000"
    try:
        tight = surface(PaperDoc(paper_id="t", sections=many))
        assert tight.reading_parts > 1, "a small budget buys more passes, not less paper"
        assert measure(tight).prose_presented_fraction == 1.0, (
        "the budget bounds how many passes a paper takes, and no longer how much of it "
        "a reader is shown")
    finally:
        del os.environ[_BUDGET_ENV]
    assert surface(PaperDoc(paper_id="t", sections=many)).reading_parts < \
        tight.reading_parts, "raising the budget must lower the number of passes"
    assert measure(surf).prose_presented_fraction is not None
    assert measure(empty).prose_presented_fraction is None, "0 of 0 chars is not 1.0"

    # --- nothing here may decide anything ---------------------------------------------
    for model in (ReviewSurface, CoverageReport):
        for banned in ("severity", "counted_severity", "verdict", "triage", "colour",
                       "color", "confidence", "claim_status", "scientific_class"):
            assert banned not in model.model_fields, (model.__name__, banned)
    # ...and no function on the measurement path so much as mentions a decision. Checked
    # over the functions rather than over the file, because THIS list is in the file.
    decide = inspect.getsource(surface) + inspect.getsource(measure) + \
        inspect.getsource(_fold) + inspect.getsource(_fold_all) + \
        inspect.getsource(prose_presented) + inspect.getsource(kind_of)
    for banned in ("severity", "verdict", "triage", "GREEN", "YELLOW", "RED"):
        assert banned not in decide, banned

    print("harness.coverage self-check ok")


if __name__ == "__main__":
    _self_check()
