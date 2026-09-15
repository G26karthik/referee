"""Deterministic document integrity — OBSERVATIONS about a parsed paper, never conclusions.

`python -m harness.docintegrity` runs the self-check.

**The defect this module exists to prevent.** A "referenced but missing" check is the
first thing anyone writes when asked for document integrity, and over the eleven parsed
papers shipped in `projects/*/paper/doc.json` the naive version produces **twelve claims
that a table or an equation is absent, and not one of them is true**: CVPR tables 2 and 4
and equations 1 and 6, ICLR tables 12 and 13, APT tables 5 and 12 and equations 1, 3, 5
and 6. Every one of those objects is printed in the paper. All twelve are absent only from
what extraction recovered — CVPR's equation set arrives as `{2,3,7,8,9}`, APT's as
`{2,10,11,12}`, and no shipped table carries a paired label at all. Filing those as
referee-facing findings would make a review's colour a property of this harness's
extractor, which is the same defect as the removed count-of-findings threshold (invariant
17) and the axis collapse invariant 20 forbids.

So the type this module emits carries `about` as a REQUIRED field with no default, and of
the ten declared checks eight can only ever fill it with `EXTRACTION`, one may fill it with
`PAPER`, and one (`NOT_COMPUTED`) is not computed at all and says so. An extraction
admission is worded as an admission — every `EXTRACTION` observation is forbidden by the
self-check from containing the phrase "the paper", because a reader skimming a bullet list
reads the sentence and not the field.

**An empty result and an unavailable check are different answers.** `determinations` says,
per check, what THIS document permitted it to claim, so "we looked and found nothing" and
"this document does not carry what the check needs" are never confused: ICLR's headings
are unnumbered, so no section reference in it can be called unresolved, and its recovered
equation set is empty, so every numbering gap in it would be a gap in our own record. Both
answer `NOT_INVESTIGATED`, which is the answer rather than a silence.

**Only one check may claim anything about the paper, deliberately.** `TABLE_ARITHMETIC` is
the single comparison in reach whose operands and whose result are all verbatim cells of
one recovered row, each at an address `claims.resolve` re-derives, with the arithmetic
re-done operand by operand — and even it refuses unless another row of the same table
establishes what the column means. Extraction loss cannot manufacture a false positive
there: a lost cell removes an operand and the row is skipped, and a mis-parsed row yields
cells that do not parse as numbers and is skipped too. Every other candidate check compares
something extraction recovered against something extraction may equally have lost, and an
absence in a lossy record is not an absence in the document.

**This layer is report-only, on the model of `harness/code_audit.py`.** That module's
`RuntimeDemand`s go in `CodeAudit.runtime` and never in `findings`, "because a requirement
filed there would be read as an accusation", and its self-check asserts it never emits
FATAL. The same posture holds here and more strongly: a `DocumentObservation` has no
severity, no confidence and no scientific class to be read, `observe` takes a `PaperDoc`
and nothing else, and no function in `grading`, `taxonomy`, `planner`, `priority` or
`stages/report`'s decision path can receive one. A `PAPER` observation with a
*demonstrated* scientific consequence would be the only route into the findings channel and
that route is deliberately not built.

**Named non-capabilities** (`NOT_ATTEMPTED`, printed rather than implied): figure content is
never read, equations are never re-assembled from glyph fragments, and no caption-versus-body
"confidence score" is computed — the third would put a number on a guess, which is worse
than saying nothing.
"""
from __future__ import annotations

import bisect
import re

from . import claims
from .artifacts import (EVIDENCE_CLASSES, INTEGRITY_ABOUT, INTEGRITY_CHECKS,
                        DocumentObservation, PaperDoc)

# --------------------------------------------------------------------------- #
# What this module will not do, stated rather than left to be inferred
# --------------------------------------------------------------------------- #
NOT_ATTEMPTED: tuple[tuple[str, str], ...] = (
    ("figure content",
     "no figure's plotted values are read. `Figure` is an extracted CAPTION and this "
     "harness has no way to see the marks in the image."),
    ("equation re-assembly",
     "an equation that arrives as per-glyph fragments is left alone. `pdf._equation_body` "
     "already refuses to attach a number to whatever text precedes it, for the same "
     "reason: a wrongly assembled body would earn a false machine attestation."),
    ("caption/body agreement score",
     "no number is put on how well a caption matches a body. Detecting a real "
     "mis-pairing needs the page geometry, and a score computed without it would be a "
     "guess dressed as a measurement."),
)

# The only check whose output may be a statement about the document the authors published.
# See the module docstring for why the list has exactly one member and why widening it is
# not a matter of adding a rule.
CLAIMABLE_ABOUT_THE_PAPER: tuple[str, ...] = ("TABLE_ARITHMETIC",)

# A member of the vocabulary this landing does not compute, and why. Kept in the
# vocabulary and named here rather than quietly returning nothing: comparing a number a
# sentence states against "the cell it means" requires knowing WHICH cell it means, and
# choosing one by position is precisely the coincidence invariant 18 forbids on the
# execution path — `parse_metric` refuses when its winning tier disagrees with itself
# rather than taking the nearest match. There is no argument that the same move is safer
# here because the stakes are lower; it would produce a wrong pointer with a verbatim
# quote attached, which is the most convincing kind of wrong.
NOT_COMPUTED: tuple[str, ...] = ("PROSE_CELL_MISMATCH",)

# What an input has to be re-verifiable AS before a check is allowed to compute over it.
# Borrowed from the one external system that got this right: its temporal audit refuses to
# do date arithmetic when the provenance behind the dates is low-confidence and emits a
# metadata-missing record instead — and that guard shipped as a hotfix, because the first
# version never threaded the provenance through and "defeated this safety check". The
# confidence of the INPUT is therefore a parameter of `evidence_floor` rather than a
# downstream caller's responsibility.
_STRONG_INPUT: tuple[str, ...] = ("cell_verified",)

# Caps, named, so the layer cannot flood a report. A pathological document (a table of
# contents that cites two hundred figures, an extractor that recovers a caption per line)
# must not turn two bullets into two pages. What is dropped is SAID, in the detail of the
# last observation kept for that check.
_MAX_PER_CHECK = 8
_MAX_OBSERVATIONS = 40

# --------------------------------------------------------------------------- #
# Reading labels and citations off a parsed document
# --------------------------------------------------------------------------- #
# `Table.label` is documented as '' whenever the extractor could not pair a caption with a
# body, and it is '' for every table in every shipped document; the printed number is
# nonetheless sitting at the head of `Table.caption` ("Table 5: Comparison with ..."). So
# the label is read from `label` FIRST and from the caption prefix second. The order
# matters both ways round: an extractor that starts populating `label` and stripping it out
# of `caption` (which is the direction `pdf.py` is being taken) must not silently lose
# every label here, and one that does neither must not report every table as unlabelled.
_LABEL_PREFIX = re.compile(
    r"^\s*(?:table|tab\.|figure|fig\.?|equation|eq\.?)\s*([A-Za-z]?\.?\d+(?:\.\d+)*)",
    re.IGNORECASE)
_BARE_LABEL = re.compile(r"^\s*([A-Za-z]?\.?\d+(?:\.\d+)*)\s*$")
_INTEGER_LABEL = re.compile(r"^\d+$")

# Run against the ORIGINAL section text and then mapped into flattened coordinates, NOT
# run against the flattened text directly. The flattened form was tried first, on the
# reasoning that "Table\n5" broken across a column boundary flattens to "table5" and needs
# no line-wrap heuristic — which is true, and it destroys every word boundary at the same
# time: flattening turns "in Figure 1" into "infigure1", so a leading `(?<![a-z])` refuses
# the match and dropping the guard accepts "config1" as a citation of Figure 1. Measured
# over two corpus papers: 7 citations recovered against 20, and 34 against 111. `\s*`
# between the keyword and the number recovers the line wrap without giving up `\b`.
# `\(?` is not decoration: an equation is cited as "Eq. (3)" at least as often as "Eq. 3",
# and without it APT's four unresolved equation references — four of the twelve measured
# false "missing object" claims this module exists to keep out of the findings channel —
# were invisible to the scan, so the guard could not be demonstrated on them.
_CITATION = re.compile(
    r"\b(Tables?|Tabs?\.?|Figs?\.?|Figures?|Eqs?\.?|Eqns?\.?|Equations?|Secs?\.?|"
    r"Sections?|Appendices|Appendix)\s*\(?\s*(\d+(?:\.\d+)*)", re.IGNORECASE)
_CITATION_KIND = {
    "table": "table", "tables": "table", "tab": "table", "tabs": "table",
    "fig": "figure", "figs": "figure", "figure": "figure", "figures": "figure",
    "eq": "equation", "eqs": "equation", "eqn": "equation", "eqns": "equation",
    "equation": "equation", "equations": "equation",
    "sec": "section", "secs": "section", "section": "section", "sections": "section",
    "appendix": "appendix", "appendices": "appendix",
}
# A numbered heading as `pdf.is_heading` admits one: "4.2. Ablation study", "5 Experiments".
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S")
# Half a window either side of a citation. Wide enough that the quote is a sentence
# fragment a reader recognises, narrow enough that it is not a paragraph.
_WINDOW_BEFORE, _WINDOW_AFTER = 40, 90

_AVERAGE_HEADER = re.compile(r"^(avg|avg\.|average|mean|overall|all)\b", re.IGNORECASE)
_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("­", "")).strip()


def _printed_label(label: str = "", caption: str = "") -> str:
    """The number the paper printed for an object, or '' when none was recovered."""
    for source in (label, caption):
        source = _norm(source)
        if not source:
            continue
        if m := _LABEL_PREFIX.match(source):
            return m.group(1)
        if m := _BARE_LABEL.match(source):
            return m.group(1)
    return ""


def _as_number(cell: str) -> float | None:
    """A cell's value, or None when the cell is not exactly one plain number.

    Deliberately strict. A cell holding "64.3 64.8" is two numbers a ruled-table extractor
    merged, "100.0%" carries a unit, and "38.6/17.0/35.8" is three metrics; averaging any
    of them would be arithmetic over something this module cannot claim to have understood.
    """
    text = _norm(cell).replace(",", "")
    return float(text) if _NUMBER.match(text) else None


def _tolerance(printed: str) -> float:
    """Half of the last place the paper actually printed.

    An average printed to one decimal is a rounded average, so comparing it to full
    precision would convict every correctly-rounded row in every paper. The tolerance is
    read off the printed token rather than fixed, because "53.4" and "53.42" do not permit
    the same slack.
    """
    text = _norm(printed).replace(",", "")
    decimals = len(text.split(".", 1)[1]) if "." in text else 0
    return 0.5 * (10.0 ** -decimals) + 1e-9


# --------------------------------------------------------------------------- #
# The two structural readings every check draws on
# --------------------------------------------------------------------------- #
class _Object:
    """One recovered numbered object, with an address a reader can re-resolve."""

    __slots__ = ("kind", "idx", "label", "ref", "quote", "page", "caption")

    def __init__(self, kind: str, idx: int, label: str, ref: str, quote: str,
                 page: int, caption: str) -> None:
        # `idx` is the object's OWN declared index (`table_idx` / `figure_idx` /
        # `equation_idx`), not its position in the list. The two coincide today and only
        # one of them is the identity a reader can look up, and a renumbering that made
        # them differ would silently print the wrong object.
        self.kind, self.idx, self.label = kind, idx, label
        self.ref, self.quote, self.page, self.caption = ref, quote, page, caption


def _first_cell(doc: PaperDoc, table) -> tuple[str, str]:
    """(ref, quote) for the first non-empty cell of a table, or ('', '').

    A table has no address of its own in the reference grammar — only its cells do — so a
    table-level observation borrows one. It has to be a cell that actually round-trips:
    `claims.resolve` re-reads the cell and returns `span_mismatch` when the text there is
    not the quote, and an observation carrying an address that does not resolve is exactly
    the unverifiable pointer invariant 1 exists to refuse.
    """
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row):
            if not _norm(cell):
                continue
            ref = table.ref(r, c)
            if claims.resolve(doc, ref, cell).resolved:
                return ref, cell
    return "", ""


def objects(doc: PaperDoc) -> tuple[_Object, ...]:
    """Every recovered table, figure and equation, in document order, with its label."""
    out: list[_Object] = []
    for table in doc.tables:
        ref, quote = _first_cell(doc, table)
        out.append(_Object("table", table.table_idx,
                           _printed_label(table.label, table.caption),
                           ref, quote, table.page, _norm(table.caption)))
    for fig in doc.figures:
        out.append(_Object("figure", fig.figure_idx,
                           _printed_label(fig.label, fig.caption),
                           fig.ref(), fig.caption, fig.page, _norm(fig.caption)))
    for eq in doc.equations:
        out.append(_Object("equation", eq.equation_idx, _printed_label(eq.number, ""),
                           eq.ref(), eq.text, eq.page, _norm(eq.text)))
    return tuple(out)


class _Citation:
    """One place the prose cites a numbered object, at a re-resolvable prose address."""

    __slots__ = ("kind", "number", "ref", "quote", "page", "caption_shaped")

    def __init__(self, kind: str, number: str, ref: str, quote: str, page: int,
                 caption_shaped: bool = False) -> None:
        self.kind, self.number, self.page = kind, number, page
        self.ref, self.quote = ref, quote
        # "Table 5:" is the object naming ITSELF, not the prose pointing at it. The
        # distinction is not decoration: `OBJECT_UNCITED` over a text whose caption lines
        # survive in the section body would find every object cited and could never fire,
        # while `CROSSREF_UNRESOLVED` wants those occurrences precisely — a caption line
        # in the prose for a body extraction never paired is the admission it exists to
        # make. So both readings are kept and each check says which it uses.
        self.caption_shaped = caption_shaped


def citations(doc: PaperDoc) -> tuple[_Citation, ...]:
    """Where the prose cites a numbered object — WHAT IT CITES, never what exists.

    `doc.crossrefs` is preferred whenever the ingest stage populated it, because that is
    the extraction layer's own reading and two readings of one document that disagree is a
    defect a reader cannot see. It is empty on every document ingested before that field
    existed, and this module has to work on both, so the fallback scans the section text
    itself. Which one was used is not hidden: `citation_source` says.
    """
    if doc.crossrefs:
        out: list[_Citation] = []
        for cr in doc.crossrefs:
            kind = (cr.kind or "").strip().lower()
            if kind not in ("table", "figure", "equation", "section", "appendix"):
                continue
            got = claims.resolve(doc, cr.span, cr.quote) if cr.span else None
            ok = got is not None and got.resolved
            out.append(_Citation(kind, _norm(cr.number), cr.span if ok else "",
                                 got.quote if ok else "", cr.page,
                                 _caption_shaped(cr.quote or "", cr.number or "")))
        return tuple(out)

    body_end = doc.body_end_section_idx
    found: list[_Citation] = []
    for section_idx, flat, offsets, original, page in claims.section_units(doc):
        # Back matter cites nothing of the paper's own: a bibliography entry naming a
        # volume number is not a cross-reference, and reading one as a citation of the
        # paper's own Table 12 is how a reference list becomes a document-integrity claim.
        if 0 <= body_end <= section_idx:
            continue
        for m in _CITATION.finditer(original):
            kind = _CITATION_KIND.get(m.group(1).lower().rstrip("."))
            if not kind:
                continue
            start = max(0, _flat_index(offsets, m.start()) - _WINDOW_BEFORE)
            end = min(len(flat), _flat_index(offsets, m.end()) + _WINDOW_AFTER)
            if start >= end:
                continue
            # The quote is sliced here rather than by calling `claims.resolve` per match.
            # Resolve re-flattens every section of the document on each call, and one
            # 38-page paper carries over a thousand citations: the round trip cost 0.6 s
            # per document and the two produce the same string by construction, because
            # this is the same slice `claims._verbatim` takes through the same index map.
            # `tests/test_document_integrity.py` re-resolves every emitted address anyway,
            # so the equivalence is asserted rather than assumed.
            found.append(_Citation(kind, m.group(2), f"P{section_idx}:{start}-{end}",
                                   original[offsets[start]:offsets[end - 1] + 1], page,
                                   original[m.end():m.end() + 3].lstrip().startswith(":")))
    return tuple(found)


def _flat_index(offsets: list[int], original_index: int) -> int:
    """The flattened coordinate for an index into the original text.

    `claims.flatten` hands back flat -> original; this is the inverse, by bisection over a
    list that is sorted by construction. Minting the address in flattened coordinates is
    what makes it round-trip: `claims.resolve` re-reads the span and hands a reader back
    verbatim text, so an address printed here can be checked by hand.
    """
    return bisect.bisect_left(offsets, original_index)


def _caption_shaped(quote: str, number: str) -> bool:
    """Does this citing text name the object as its own caption ("Table 5: ...")?"""
    text = _norm(quote)
    m = re.search(rf"\b\w+\s*{re.escape(_norm(number))}\s*:", text) if number else None
    return bool(m)


def citation_source(doc: PaperDoc) -> str:
    """`crossrefs` when the ingest stage supplied them, `prose_scan` when this module did."""
    return "crossrefs" if doc.crossrefs else "prose_scan"


# --------------------------------------------------------------------------- #
# What each check is ALLOWED to claim
# --------------------------------------------------------------------------- #
def evidence_floor(check: str = "", input_evidence_class: str = "") -> str:
    """The strongest thing an observation of `check` computed over this input may claim.

    One of `PAPER`, `EXTRACTION`, `NOT_INVESTIGATED` — where `NOT_INVESTIGATED` means DO
    NOT COMPUTE and emit nothing, not "compute and label it weakly". Vocabulary strings
    only, as in `grading.derive` and `planner.classify`: no count, no number, no metric
    name and no paper identity, so "if this paper, soften" is inexpressible here too.

    Fails closed in both arguments. An unknown check claims nothing, and a check that may
    speak about the paper claims nothing over an input it cannot re-verify as a cell —
    a caption reports none of a figure's plotted values, and equation extraction is lossy,
    so neither can carry arithmetic.
    """
    if (check or "").strip() not in INTEGRITY_CHECKS:
        return "NOT_INVESTIGATED"
    if check in NOT_COMPUTED:
        return "NOT_INVESTIGATED"
    if check not in CLAIMABLE_ABOUT_THE_PAPER:
        # An admission about this harness's own extraction needs no evidence strength: it
        # is a fact about the record, and the record is what we hold.
        return "EXTRACTION"
    return "PAPER" if (input_evidence_class or "").strip() in _STRONG_INPUT \
        else "NOT_INVESTIGATED"


def determinations(doc: PaperDoc) -> dict[str, str]:
    """Per check, what THIS document permitted it to claim — every check, always present.

    An empty observation list means two opposite things, and without this map a reader
    cannot tell them apart: "we looked and the document is clean" and "this document does
    not carry what the check needs". The second is the ICLR case for two separate checks —
    it has no numbered headings at all, so no section reference can be called unresolved,
    and its recovered equation set is empty, so every numbering gap in it would be a gap in
    our own record. Both answer NOT_INVESTIGATED, which is the answer, not a silence.
    """
    return _determine(doc, objects(doc), citations(doc))


def _determine(doc: PaperDoc, objs: tuple[_Object, ...],
               cites: tuple[_Citation, ...]) -> dict[str, str]:
    """The body of `determinations`, over readings the caller already has.

    Split out because `observe` needs both the gate map and the two readings, and building
    the readings twice per document cost 0.6 s on a 38-page paper — the flattening pass is
    the expensive part and there is no reason to run it twice.
    """
    by_kind: dict[str, list[_Object]] = {"table": [], "figure": [], "equation": []}
    for o in objs:
        by_kind[o.kind].append(o)
    labelled = {k: [o for o in v if o.label] for k, v in by_kind.items()}
    # `OBJECT_UNCITED` reads only the occurrences that REFER to an object, so its
    # determination has to be computed over the same set. Computing it over all of them
    # would report the check as available on a document where every occurrence is a
    # caption line and the check can therefore say nothing.
    referring_kinds = {c.kind for c in cites if not c.caption_shaped}
    cited_kinds = {c.kind for c in cites}
    headings = _numbered_headings(doc)

    out: dict[str, str] = {}
    for check in INTEGRITY_CHECKS:
        # The strongest input any check here computes over is a table cell — every one of
        # them reads a recovered object or a re-resolvable prose span, never a figure's
        # contents. Naming the check rather than the class would put a check name in the
        # gate, which is how a per-paper exception gets written.
        out[check] = evidence_floor(
            check, "cell_verified" if check in CLAIMABLE_ABOUT_THE_PAPER else "")
    if not any(labelled.values()) or not cites:
        out["CROSSREF_UNRESOLVED"] = "NOT_INVESTIGATED"
    if not referring_kinds or not any(labelled.values()):
        out["OBJECT_UNCITED"] = "NOT_INVESTIGATED"
    if sum(len(v) for v in labelled.values()) < 2:
        out["LABEL_DUPLICATED"] = "NOT_INVESTIGATED"
        out["CAPTION_LABEL_CONFLICT"] = "NOT_INVESTIGATED"
        out["LABEL_OUT_OF_ORDER"] = "NOT_INVESTIGATED"
    if not doc.tables:
        out["BODY_UNCAPTIONED"] = "NOT_INVESTIGATED"
    # Tables only, and the reason is measured: the recovered equation label sets are
    # {2,3,7,8,9}, {} and {2,10,11,12} across three corpus papers, so every "gap" in them
    # is a gap in extraction; figure label sets are polluted by duplicate recoveries. Table
    # caption LINES survive more completely than table bodies do, which is what makes the
    # table half worth computing and the other two halves worth refusing.
    if len(_integer_labelled(labelled["table"])) < 2:
        out["NUMBERING_GAP"] = "NOT_INVESTIGATED"
    if not headings or "section" not in cited_kinds:
        out["SECTION_REF_UNRESOLVED"] = "NOT_INVESTIGATED"
    if not _established_average_columns(doc):
        out["TABLE_ARITHMETIC"] = "NOT_INVESTIGATED"
    for check in NOT_COMPUTED:
        out[check] = "NOT_INVESTIGATED"
    return out


def _numbered_headings(doc: PaperDoc) -> frozenset[str]:
    return frozenset(m.group(1) for s in doc.sections
                     if (m := _NUMBERED_HEADING.match(_norm(s.title))))


def _integer_labelled(objs: list[_Object]) -> list[_Object]:
    """Only labels that are a bare integer.

    An appendix label ("A.1", "S2") is excluded from ordering and gap arithmetic because
    the sequences do not share a number line: "Table A.1" following "Table 11" is not a
    descent, and a paper with tables 1-11 plus A.1-A.3 has no gap.
    """
    return [o for o in objs if _INTEGER_LABEL.match(o.label)]


# --------------------------------------------------------------------------- #
# TABLE_ARITHMETIC — the one check that may speak about the paper
# --------------------------------------------------------------------------- #
def _average_rows(table) -> list[tuple[int, int, list[float], float, str]]:
    """(row, col, operands, printed, printed_raw) for every checkable average cell."""
    header = [_norm(h) for h in table.header]
    out: list[tuple[int, int, list[float], float, str]] = []
    for col, head in enumerate(header):
        if col == 0 or not _AVERAGE_HEADER.match(head):
            continue
        for r, row in enumerate(table.rows):
            if col >= len(row):
                continue
            printed = _as_number(row[col])
            if printed is None:
                continue
            # Strictly to the LEFT of the average column and strictly right of column 0,
            # which the extractor treats as the row label. An "Avg." sitting mid-table with
            # efficiency metrics after it is the common layout, and averaging across it
            # would mix metrics that share no unit.
            operands = [v for c in range(1, col) if (v := _as_number(row[c])) is not None]
            if len(operands) < 2:
                continue
            out.append((r, col, operands, printed, _norm(row[col])))
    return out


def _established_average_columns(doc: PaperDoc) -> dict[tuple[int, int], int]:
    """{(table_idx, col): operand_count} for columns a row of their own table PROVES.

    The guard that makes this check safe to point at a paper. A column headed "Avg." might
    average a subset, might be weighted, might average down instead of across, and the
    header cannot say which. So the column's meaning is ESTABLISHED by a row whose plain
    mean of the cells to its left reproduces the printed value to the precision the paper
    printed — and only then may a different row of the same table, over the same number of
    operands, be reported as not matching. Without this guard, one unusual layout would
    convict every row in the table at once.
    """
    established: dict[tuple[int, int], int] = {}
    for table in doc.tables:
        for r, col, operands, printed, raw in _average_rows(table):
            mean = sum(operands) / len(operands)
            if abs(mean - printed) <= _tolerance(raw):
                established.setdefault((table.table_idx, col), len(operands))
    return established


# --------------------------------------------------------------------------- #
# observe
# --------------------------------------------------------------------------- #
def _obs(check: str, about: str, ref: str, quote: str, page: int,
         detail: str) -> DocumentObservation:
    return DocumentObservation(check=check, about=about, ref=ref, quote=quote,
                               page=page, detail=detail)


def observe(doc: PaperDoc) -> tuple[DocumentObservation, ...]:
    """Every document-integrity observation this parsed paper supports.

    `PaperDoc` ONLY — no `TargetSet`, no `Finding`, no `DiscoveredObject`, no severity and
    no gate state may reach this function, and the signature is what asserts it. Ordered by
    `INTEGRITY_CHECKS` so two runs over one document produce the same list.
    """
    objs = objects(doc)
    cites = citations(doc)
    allowed = _determine(doc, objs, cites)
    by_kind: dict[str, list[_Object]] = {"table": [], "figure": [], "equation": []}
    for o in objs:
        by_kind[o.kind].append(o)
    groups: dict[str, list[DocumentObservation]] = {c: [] for c in INTEGRITY_CHECKS}
    unresolved_refs: set[tuple[str, str]] = set()

    # --- what the prose cites and extraction did not recover ---------------------------
    if allowed["CROSSREF_UNRESOLVED"] == "EXTRACTION":
        have = {(o.kind, o.label) for o in objs if o.label}
        # Gated PER KIND. A document where extraction recovered no equation label at all
        # and the prose cites six equations carries ONE fact — "no equation label was
        # recovered" — and printing it six times as six unresolved references turns a
        # single extraction limit into a list that reads like a defect count.
        kinds_labelled = {o.kind for o in objs if o.label}
        seen: set[tuple[str, str]] = set()
        for c in cites:
            key = (c.kind, c.number)
            if c.kind not in kinds_labelled or key in seen or key in have:
                continue
            seen.add(key)
            unresolved_refs.add(key)
            groups["CROSSREF_UNRESOLVED"].append(_obs(
                "CROSSREF_UNRESOLVED", "EXTRACTION", c.ref, c.quote, c.page,
                f"the prose cites {c.kind} {c.number}. Extraction recovered no {c.kind} "
                f"labelled {c.number}, so this review could not address it — a limit of "
                f"what this harness recovered, not an established absence."))

    # --- a recovered body with no caption paired to it --------------------------------
    if allowed["BODY_UNCAPTIONED"] == "EXTRACTION":
        # Tables only. A `Figure` object IS an extracted caption, so a figure with no
        # caption is not a thing that can exist here, and reporting equations this way
        # would report the extractor's own documented willingness to find nothing.
        for table in doc.tables:
            if _norm(table.caption):
                continue
            ref, quote = _first_cell(doc, table)
            groups["BODY_UNCAPTIONED"].append(_obs(
                "BODY_UNCAPTIONED", "EXTRACTION", ref, quote, table.page,
                f"extraction recovered a table body on page {table.page} (T"
                f"{table.table_idx}) with no caption paired to it, so nothing names it "
                f"and no citation of it can be matched. `Table.label` is documented as "
                f"empty in exactly this case."))

    # --- what extraction recovered and found no citation of ---------------------------
    if allowed["OBJECT_UNCITED"] == "EXTRACTION":
        # Caption-shaped occurrences excluded: an object's own caption line surviving in
        # the section text is the object naming itself, not the prose referring to it.
        referring = tuple(c for c in cites if not c.caption_shaped)
        cited = {(c.kind, c.number) for c in referring}
        kinds_cited = {c.kind for c in referring}
        for o in objs:
            if not o.label or o.kind not in kinds_cited or (o.kind, o.label) in cited:
                continue
            groups["OBJECT_UNCITED"].append(_obs(
                "OBJECT_UNCITED", "EXTRACTION", o.ref, o.quote, o.page,
                f"extraction recovered {o.kind} {o.label} and found no sentence citing it "
                f"in the prose it recovered. Either nothing refers to it or the referring "
                f"sentence was lost; this record cannot tell which."))

    # --- one label, two objects -------------------------------------------------------
    # Both checks are read off ONE grouping and each is gated on its OWN determination.
    # Sharing a gate would let a check emit an observation its own determination calls
    # unavailable, which makes the determination map decorative — the two conditions are
    # identical today and that is not a reason to write the coupling in.
    if "EXTRACTION" in (allowed["LABEL_DUPLICATED"], allowed["CAPTION_LABEL_CONFLICT"]):
        by_label: dict[tuple[str, str], list[_Object]] = {}
        for o in objs:
            if o.label:
                by_label.setdefault((o.kind, o.label), []).append(o)
        for (kind, label), members in by_label.items():
            if len(members) < 2:
                continue
            first = members[0]
            texts = {m.caption for m in members}
            others = ", ".join(m.ref or f"{kind} index {m.idx}" for m in members[1:])
            if len(texts) == 1 and allowed["LABEL_DUPLICATED"] == "EXTRACTION":
                # The same caption line recovered more than once — a two-column layout
                # whose caption spans both columns, most often.
                groups["LABEL_DUPLICATED"].append(_obs(
                    "LABEL_DUPLICATED", "EXTRACTION", first.ref, first.quote, first.page,
                    f"extraction recovered {len(members)} {kind}s all labelled {label} with "
                    f"identical text, so one {kind} was almost certainly recovered more "
                    f"than once (also at {others})."))
            elif len(texts) > 1 and allowed["CAPTION_LABEL_CONFLICT"] == "EXTRACTION":
                # Two DIFFERENT texts claiming one number: at most one pairing is right,
                # and which one it is needs the page geometry this module does not read.
                groups["CAPTION_LABEL_CONFLICT"].append(_obs(
                    "CAPTION_LABEL_CONFLICT", "EXTRACTION", first.ref, first.quote,
                    first.page,
                    f"extraction paired the label {label} with {len(texts)} different "
                    f"{kind} captions (also at {others}), so at most one of those pairings "
                    f"is right and this record does not say which."))

    # --- labels that do not ascend ----------------------------------------------------
    if allowed["LABEL_OUT_OF_ORDER"] == "EXTRACTION":
        for kind, members in by_kind.items():
            ordered = _integer_labelled(members)
            for prev, cur in zip(ordered, ordered[1:]):
                if int(cur.label) >= int(prev.label):
                    continue
                groups["LABEL_OUT_OF_ORDER"].append(_obs(
                    "LABEL_OUT_OF_ORDER", "EXTRACTION", cur.ref, cur.quote, cur.page,
                    f"extraction recovered {kind} {prev.label} before {kind} {cur.label}, "
                    f"so the recovered order of this document's {kind}s does not ascend "
                    f"and the labels cannot all be paired correctly."))

    # --- a hole in the recovered table labels -----------------------------------------
    if allowed["NUMBERING_GAP"] == "EXTRACTION":
        ordered = _integer_labelled(by_kind["table"])
        present = {int(o.label) for o in ordered}
        after = {int(o.label): o for o in reversed(ordered)}
        # A hole the prose already pointed at is reported once, as the reference this
        # review could not follow — which is the more informative of the two readings and
        # carries the citing sentence with it. Printing both turns one fact about our
        # record into two bullets and inflates every count a renderer takes from here.
        already = {int(num) for kind, num in unresolved_refs
                   if kind == "table" and _INTEGER_LABEL.match(num)}
        for missing in range(min(present) + 1, max(present)):
            if missing in present or missing in already:
                continue
            anchor = after.get(missing + 1) or ordered[-1]
            groups["NUMBERING_GAP"].append(_obs(
                "NUMBERING_GAP", "EXTRACTION", anchor.ref, anchor.quote, anchor.page,
                f"extraction recovered table labels between {min(present)} and "
                f"{max(present)} with no table labelled {missing} among them. A hole in "
                f"this record is not a hole in the numbering."))

    # --- a section reference with no numbered heading behind it -----------------------
    if allowed["SECTION_REF_UNRESOLVED"] == "EXTRACTION":
        headings = _numbered_headings(doc)
        seen_sec: set[str] = set()
        for c in cites:
            if c.kind != "section" or c.number in seen_sec or c.number in headings:
                continue
            seen_sec.add(c.number)
            groups["SECTION_REF_UNRESOLVED"].append(_obs(
                "SECTION_REF_UNRESOLVED", "EXTRACTION", c.ref, c.quote, c.page,
                f"the prose cites section {c.number}. Extraction recovered "
                f"{len(headings)} numbered heading(s) and none of them is {c.number}, so "
                f"this review could not follow the reference."))

    # --- the one check about the paper ------------------------------------------------
    if allowed["TABLE_ARITHMETIC"] == "PAPER":
        established = _established_average_columns(doc)
        for table in doc.tables:
            for r, col, operands, printed, raw in _average_rows(table):
                want = established.get((table.table_idx, col))
                # Same column, and the SAME number of operands the establishing row used.
                # A row with a cell missing would otherwise be averaged over a different
                # denominator and reported as the paper's error rather than ours.
                if want is None or want != len(operands):
                    continue
                mean = sum(operands) / len(operands)
                if abs(mean - printed) <= _tolerance(raw):
                    continue
                ref = table.ref(r, col)
                cell = table.cell(r, col)
                if not claims.resolve(doc, ref, cell).resolved:
                    continue
                shown = ", ".join(f"{v:g}" for v in operands)
                groups["TABLE_ARITHMETIC"].append(_obs(
                    "TABLE_ARITHMETIC", "PAPER", ref, cell, table.page,
                    f"the cell under the average column prints {raw}; the plain mean of "
                    f"the {len(operands)} numeric cells to its left ({shown}) is "
                    f"{mean:.6g}. Another row of this table reproduces its own average "
                    f"exactly, so the column does average across."))

    # --- caps, applied per check and then overall ------------------------------------
    out: list[DocumentObservation] = []
    for check in INTEGRITY_CHECKS:
        kept = groups[check][:_MAX_PER_CHECK]
        dropped = len(groups[check]) - len(kept)
        if dropped and kept:
            kept[-1].detail += (f" (and {dropped} further {check} observation(s) not "
                                f"listed — the per-check cap is {_MAX_PER_CHECK}.)")
        out.extend(kept)
    if len(out) > _MAX_OBSERVATIONS:
        dropped = len(out) - _MAX_OBSERVATIONS
        out = out[:_MAX_OBSERVATIONS]
        out[-1].detail += (f" (and {dropped} further observation(s) not listed — the "
                           f"per-document cap is {_MAX_OBSERVATIONS}.)")
    return tuple(out)


def summarise(observations: tuple[DocumentObservation, ...]) -> dict[str, int]:
    """Counts per `about`, for a renderer that must never sum the two halves.

    Returned as two keys rather than a total, because "3 observations about the paper and
    6 about our own extraction" and "9 document-integrity observations" say different
    things to a referee and only the first is true.
    """
    return {about: sum(1 for o in observations if o.about == about)
            for about in INTEGRITY_ABOUT}


def scope_lines(observations: tuple[DocumentObservation, ...]) -> tuple[str, ...]:
    """The reviewer-facing bullets, minted HERE rather than in the renderer.

    The wording is the risky part of this layer, not the arithmetic: "3 broken references"
    and "3 references this review could not follow in what it recovered" are the same
    number and opposite claims. So the sentences live in the module that knows which of
    the two it is entitled to say, and the renderer places them under scope — never inside
    `## Scientific findings`, and never beside a support word.

    Empty when there is nothing to report, so a renderer that prints these unconditionally
    cannot produce "0 observations" as though absence were a result: an empty list here
    means `determinations` is where a reader has to look for whether anything was checked.
    """
    counts = summarise(observations)
    if not observations:
        return ()
    per_check: dict[str, int] = {}
    for o in observations:
        per_check[o.check] = per_check.get(o.check, 0) + 1
    breakdown = ", ".join(f"{per_check[c]} {c.lower().replace('_', ' ')}"
                          for c in INTEGRITY_CHECKS if c in per_check)
    return (
        f"document integrity: {counts['PAPER']} observation(s) about the paper and "
        f"{counts['EXTRACTION']} about this review's own extraction ({breakdown}) — "
        f"the full list is in the machine report",
        "these are structural observations, not scientific findings; none of them counts "
        "toward any threshold and none of them moved the review's outcome",
    )


# --------------------------------------------------------------------------- #
# self-check
# --------------------------------------------------------------------------- #
def _doc(**kw) -> PaperDoc:
    fields: dict = {"paper_id": "selfcheck", "n_pages": 2}
    fields.update(kw)
    return PaperDoc.model_validate(fields)


def _self_check() -> None:
    import inspect

    from .artifacts import Equation, Figure, Section, Table

    # --- the signature is the invariant ------------------------------------------------
    params = inspect.signature(observe).parameters
    assert list(params) == ["doc"], params
    assert str(params["doc"].annotation) == "PaperDoc", params["doc"].annotation
    for name, p in inspect.signature(evidence_floor).parameters.items():
        assert str(p.annotation) == "str", f"{name}: {p.annotation}"
    for banned in ("severity", "confidence", "finding", "target", "gate", "verdict",
                   "triage", "outcome"):
        for fn in (observe, evidence_floor, determinations, summarise):
            assert banned not in " ".join(inspect.signature(fn).parameters), (fn, banned)

    # --- the emitted type has nothing to be read as a judgement ------------------------
    for banned in ("severity", "confidence", "scientific_class", "counted_severity",
                   "candidate_class", "verdict", "route", "resolution_status"):
        assert banned not in DocumentObservation.model_fields, banned
    assert "about" in DocumentObservation.model_fields
    assert DocumentObservation.model_fields["about"].is_required(), \
        "`about` must have no default: a missing one would fall to whichever value is safer"

    # --- evidence_floor, swept over the whole closed input space -----------------------
    for check in INTEGRITY_CHECKS + ("", "MADE_UP", "table_arithmetic"):
        for cls in EVIDENCE_CLASSES + ("", "made_up"):
            got = evidence_floor(check, cls)
            assert got in INTEGRITY_ABOUT + ("NOT_INVESTIGATED",), (check, cls, got)
            if check not in INTEGRITY_CHECKS or check in NOT_COMPUTED:
                assert got == "NOT_INVESTIGATED", (check, cls, got)
            elif check not in CLAIMABLE_ABOUT_THE_PAPER:
                assert got == "EXTRACTION", (check, cls, got)
            elif cls in _STRONG_INPUT:
                assert got == "PAPER", (check, cls, got)
            else:
                assert got == "NOT_INVESTIGATED", (
                    f"{check} over {cls!r} must refuse rather than compute")
    assert evidence_floor("TABLE_ARITHMETIC", "caption_verified") == "NOT_INVESTIGATED"
    assert evidence_floor("TABLE_ARITHMETIC", "equation_verified") == "NOT_INVESTIGATED"
    assert evidence_floor("TABLE_ARITHMETIC", "cell_verified") == "PAPER"
    assert len(CLAIMABLE_ABOUT_THE_PAPER) == 1, (
        "widening the PAPER half is a design decision, not a rule change — see the "
        "module docstring for what each candidate check compares against what")

    # --- an empty document determines nothing and observes nothing ---------------------
    empty = _doc()
    assert observe(empty) == ()
    assert set(determinations(empty)) == set(INTEGRITY_CHECKS)
    assert set(determinations(empty).values()) == {"NOT_INVESTIGATED"}

    # --- the twelve measured false positives can only ever be EXTRACTION ---------------
    # The shipped shape: the prose cites tables 1-6 and extraction recovered labels 1 and 3.
    cited = _doc(sections=[Section(section_idx=0, title="4. Experiments", page_start=1,
                                   text="Table 1 lists the comparison. Table 2 reports the "
                                        "ablation, and Table 3 the transfer results.")],
                 tables=[Table(table_idx=0, page=1, caption="Table 1: Comparison.",
                               rows=[["m", "1.0"]]),
                         Table(table_idx=1, page=1, caption="Table 3: Transfer.",
                               rows=[["m", "2.0"]])])
    crossref = [o for o in observe(cited) if o.check == "CROSSREF_UNRESOLVED"]
    assert len(crossref) == 1 and crossref[0].about == "EXTRACTION", crossref
    assert "extraction recovered" in crossref[0].detail.lower()
    for o in observe(cited):
        if o.about == "EXTRACTION":
            assert "the paper" not in o.detail, (
                "an admission about our own record must not be phrased as one about the "
                f"document: {o.detail!r}")
    # and every observation is addressable
    for o in observe(cited):
        assert not o.ref or claims.resolve(cited, o.ref, o.quote).resolved, o.ref

    # --- the gap check is tables only, and refuses a document it cannot read -----------
    holes = _doc(equations=[Equation(equation_idx=i, page=1, number=n, text=f"x = {n}")
                            for i, n in enumerate(("2", "3", "7", "8", "9"))])
    assert determinations(holes)["NUMBERING_GAP"] == "NOT_INVESTIGATED", (
        "an equation label set with holes in it is a hole in extraction, measured")
    assert not [o for o in observe(holes) if o.check == "NUMBERING_GAP"]
    gapped = _doc(tables=[
        Table(table_idx=0, page=1, caption="Table 1: a", rows=[["m", "1"]]),
        Table(table_idx=1, page=1, caption="Table 3: b", rows=[["m", "2"]])])
    gaps = [o for o in observe(gapped) if o.check == "NUMBERING_GAP"]
    assert len(gaps) == 1 and gaps[0].about == "EXTRACTION" and "2" in gaps[0].detail

    # --- a section reference against a paper with no numbered headings -----------------
    unnumbered = _doc(sections=[Section(section_idx=0, title="EXPERIMENTS", page_start=1,
                                        text="As shown in Section 4.2 the gain holds.")])
    assert determinations(unnumbered)["SECTION_REF_UNRESOLVED"] == "NOT_INVESTIGATED", (
        "a paper whose headings are unnumbered cannot have an unresolved section "
        "reference — it must answer 'cannot determine', never 'missing'")
    assert not [o for o in observe(unnumbered) if o.check == "SECTION_REF_UNRESOLVED"]

    # --- duplicates and conflicts are separated, not merged ---------------------------
    twice = _doc(figures=[
        Figure(figure_idx=0, page=1, label="Figure 3", caption="Figure 3: x"),
        Figure(figure_idx=1, page=1, label="Figure 3", caption="Figure 3: x")])
    dup = [o for o in observe(twice) if o.check == "LABEL_DUPLICATED"]
    assert len(dup) == 1 and not [o for o in observe(twice)
                                  if o.check == "CAPTION_LABEL_CONFLICT"]
    clash = _doc(tables=[Table(table_idx=0, page=1, caption="Table 10: ablation",
                               rows=[["m", "1"]]),
                         Table(table_idx=1, page=1, caption="Table 10: , using the teacher",
                               rows=[["m", "2"]])])
    conflict = [o for o in observe(clash) if o.check == "CAPTION_LABEL_CONFLICT"]
    assert len(conflict) == 1 and not [o for o in observe(clash)
                                       if o.check == "LABEL_DUPLICATED"]

    # --- TABLE_ARITHMETIC: refuses unless a row of the table proves the column --------
    unproven = _doc(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                                  header=["Method", "A", "B", "Avg."],
                                  rows=[["m1", "10", "20", "99"],
                                        ["m2", "30", "40", "98"]])])
    assert determinations(unproven)["TABLE_ARITHMETIC"] == "NOT_INVESTIGATED", (
        "no row reproduces the column, so the column's meaning was never established and "
        "nothing here may be called an error in the paper")
    assert not [o for o in observe(unproven) if o.check == "TABLE_ARITHMETIC"]
    proven = _doc(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                                header=["Method", "A", "B", "Avg."],
                                rows=[["m1", "10", "20", "15.0"],
                                      ["m2", "30", "40", "99.0"]])])
    arith = [o for o in observe(proven) if o.check == "TABLE_ARITHMETIC"]
    assert len(arith) == 1 and arith[0].about == "PAPER" and arith[0].ref == "T0:r1:c3"
    assert claims.resolve(proven, arith[0].ref, arith[0].quote).resolved
    rounded = _doc(tables=[Table(table_idx=0, page=1, caption="Table 1: r",
                                 header=["Method", "A", "B", "C", "D", "Avg."],
                                 rows=[["m1", "10", "20", "30", "40", "25.0"],
                                       ["m2", "55.6", "79.3", "46.9", "49.9", "57.9"]])])
    assert not [o for o in observe(rounded) if o.check == "TABLE_ARITHMETIC"], (
        "57.925 printed as 57.9 is a correctly rounded average, not an arithmetic error")

    # --- the caps bite, and say what they dropped -------------------------------------
    flood = _doc(sections=[Section(
        section_idx=0, title="1. Results", page_start=1,
        text=" ".join(f"Table {n} reports another split of the same benchmark."
                      for n in range(1, 60)))],
        tables=[Table(table_idx=0, page=1, caption="Table 1: the only one recovered",
                      rows=[["method", "1.0"]])])
    got = observe(flood)
    assert len(got) <= _MAX_OBSERVATIONS, len(got)
    assert sum(1 for o in got if o.check == "CROSSREF_UNRESOLVED") <= _MAX_PER_CHECK
    assert any("not listed" in o.detail for o in got), "what was dropped must be said"

    # --- nothing emitted carries a severity, ever -------------------------------------
    for doc in (cited, gapped, twice, clash, proven, flood):
        for o in observe(doc):
            assert o.check in INTEGRITY_CHECKS and o.about in INTEGRITY_ABOUT, o
            assert o.detail, "an observation with no sentence is not an observation"
            assert not getattr(o, "severity", None), "observations carry no severity"
            assert not getattr(o, "counted_severity", None)
            if o.about == "PAPER":
                assert o.check in CLAIMABLE_ABOUT_THE_PAPER, o.check
            assert not o.ref or claims.resolve(doc, o.ref, o.quote).resolved, o.ref
        counts = summarise(observe(doc))
        assert set(counts) == set(INTEGRITY_ABOUT)
        assert sum(counts.values()) == len(observe(doc))
        lines = scope_lines(observe(doc))
        assert len(lines) == 2 and "counts toward any threshold" in lines[1]
        assert "about the paper" in lines[0] and "own extraction" in lines[0]
    assert scope_lines(()) == (), "absence is not a result and must not be printed as one"

    # --- no check is declared and unreachable -----------------------------------------
    # The mirror of "no evidence state is glossed without being reachable". A check in the
    # vocabulary that nothing can produce is worse than an absent one: it reads as a
    # capability, and its absence from every report reads as a clean document.
    assert set(CLAIMABLE_ABOUT_THE_PAPER) | set(NOT_COMPUTED) <= set(INTEGRITY_CHECKS)
    ordered = _doc(tables=[
        Table(table_idx=0, page=1, caption="Table 4: later", rows=[["m", "1"]]),
        Table(table_idx=1, page=2, caption="Table 2: earlier", rows=[["m", "2"]])])
    uncited = _doc(
        sections=[Section(section_idx=0, title="1. Results", page_start=1,
                          text="The comparison in Table 1 is the headline result.")],
        tables=[Table(table_idx=0, page=1, caption="Table 1: headline",
                      rows=[["m", "1"]]),
                Table(table_idx=1, page=9, caption="Table 2: never mentioned",
                      rows=[["m", "2"]])])
    uncaptioned = _doc(tables=[Table(table_idx=0, page=1, caption="", rows=[["m", "1"]])])
    numbered = _doc(sections=[
        Section(section_idx=0, title="4. Experiments", page_start=1,
                text="The derivation is in Section 9.7 of this paper."),
        Section(section_idx=1, title="4.1. Setup", page_start=1, text="We use CIFAR.")])
    reachable = {o.check for doc in (cited, gapped, twice, clash, proven, ordered,
                                     uncited, uncaptioned, numbered)
                 for o in observe(doc)}
    unreachable = set(INTEGRITY_CHECKS) - reachable - set(NOT_COMPUTED)
    assert not unreachable, f"declared but produced by nothing: {sorted(unreachable)}"

    assert len(NOT_ATTEMPTED) == 3 and all(w and why for w, why in NOT_ATTEMPTED)
    print("harness.docintegrity self-check ok")


if __name__ == "__main__":
    _self_check()
