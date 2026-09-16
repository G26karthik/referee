"""Addressable references into a parsed paper, and the quantities they report.

`python -m harness.claims` runs the self-check.

**The assumption this module removes.** A printed result used to need to be an
extractable TABLE CELL before anything downstream could check it. `^T\\d+:r\\d+:c\\d+$`
appeared in three files and gated both halves of the evidence path: `grounded_claimed_delta`
returned None for anything else, and `ProbeSpec.table_ref` was only ever set from a cell
address, so `reconcile` never ran on a prose-stated result. A paper whose central
quantitative claim is a sentence was structurally unreachable, and the difference between
"we could not check this" and "there was nothing to check" was invisible in the report.

**The invariant that replaces it, and why it is safe.** A lens supplies a QUOTE; the
HARNESS mints the ADDRESS. `mint()` searches the parsed document for the quote and
refuses unless it occurs exactly once — an ambiguous quote yields no address rather than
its first occurrence, because a stable id for a span the lens may not have meant is worse
than no id. A lens that writes a `P<i>:<a>-<b>` address itself gains nothing: `resolve()`
re-reads the span off the document and returns `span_mismatch` when the text there is not
the quote. So the model can direct attention and cannot manufacture provenance, which is
the same split `stages/audit.verify_evidence` already enforces for table cells.

**Two coordinate systems, kept apart on purpose.** Addresses are minted in FLATTENED
coordinates (whitespace removed, lowercased) because a PDF breaks a sentence across lines
wherever the column happens to end, and an address that moved when the extractor
re-wrapped a line would not be stable. The `quote` handed back is sliced from the
ORIGINAL section text through an index map, so what a reader is shown is verbatim.

Flattening is per SECTION, never across sections — the same reason
`stages/audit.source_units` is per section. A concatenated corpus lets a string
straddling a section seam verify, and the harness then attests in its own voice to a
quote that does not occur in the paper.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

from .artifacts import ClaimRef, PaperDoc, ReportedQuantity

_WS = re.compile(r"\s+")
_CELL_REF = re.compile(r"^T(\d+):r(\d+):c(\d+)$")
_FIG_REF = re.compile(r"^F(\d+)$")
_EQ_REF = re.compile(r"^E(\d+)$")
_SEC_REF = re.compile(r"^S(\d+)$")
_PROSE_REF = re.compile(r"^P(\d+):(\d+)-(\d+)$")

# A quote shorter than this addresses nothing in particular: "the" occurs everywhere, so
# it can never be unique, and a four-character quote that happens to be unique is unique
# by accident. `stages/audit` uses the same floor for the same reason.
_QUOTE_MIN = 8

# Only MULTIPLICATIVE composition is recognised. `-` cannot be told apart from a sign
# without parsing the sentence, `+` has the same problem, and `/` is ambiguous between
# division and "per". Refusing them costs a composition this module could otherwise have
# checked; accepting them would let it check one wrongly, and a wrong `arithmetic_ok` is
# a false machine attestation of exactly the kind this harness exists to catch.
_MULT = {"*": "*", "×": "*", "·": "*", "∗": "*"}
# A lone `x` is the multiplication sign in half of all extracted PDFs — either the author
# typed it or the extractor lost U+00D7 — so refusing it would refuse the common case.
# It is admitted ONLY when it stands alone between non-alphanumerics, so the `x` in
# `max`, `x_i` and `2x` is not an operator, and the strict-alternation check below still
# has to hold before any composition is recognised at all.
_LONE_X = re.compile(r"(?<![A-Za-z0-9_])[xX](?![A-Za-z0-9_])")
# A single non-alphanumeric glyph standing ALONE between two numbers. This is what a
# multiplication sign looks like after a PDF has been through a font encoding that does
# not name it — see `_composition` for the tier this belongs to and why it may only be
# read as multiplication when the paper's own arithmetic confirms it. Digits, letters,
# `=`, `.` and `,` are excluded: `=` is the equality this sits to the left of, and the
# other three are parts of numbers.
_LONE_SEPARATOR = re.compile(r"(?<=[\s\d])\s*([^\sA-Za-z0-9=.,()\[\]])\s*(?=[\s\d])")
_UNSIGNED = re.compile(r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_SIGNED = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
# 2,900 -> 2900. Only between digits, so "Table 2, column 3" is untouched.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")


# --------------------------------------------------------------------------- #
# Flattening, with an index back to the original text
# --------------------------------------------------------------------------- #
def flatten(text: str) -> tuple[str, list[int]]:
    """(flattened, original_index_per_flattened_char).

    The map is what makes an address in flattened coordinates able to hand back a
    verbatim quote. Without it this module would have to return the normalised form,
    and a reviewer would be shown text that appears nowhere in the paper.
    """
    flat_chars: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text or ""):
        if ch.isspace():
            continue
        flat_chars.append(ch.lower())
        offsets.append(i)
    return "".join(flat_chars), offsets


def soft_hyphen_projection(flat: str, offsets: list[int],
                           original: str) -> tuple[str, list[int]]:
    """(text with LINE-BREAK hyphens removed, index of each kept char in `flat`).

    THE DEFECT THIS CLOSES, measured. A typesetter breaking "generation" across a line
    leaves "gener-" and "ation" in the PDF, and `flatten` — which removes whitespace and
    keeps punctuation — turns that into `gener-ation`. A reader quoting the sentence the
    way any human reads it writes `generation`, and the harness refuses the quotation as
    not present in the paper. Measured on one real abstract (`cvpr`), that refused 8 of
    11 correctly-quoted sentences.

    A SOFT HYPHEN IS DECIDABLE, and only from the original text. In the flattened text a
    line-break hyphen and a real compound hyphen are the same character; in the original,
    a line-break hyphen is the one immediately FOLLOWED BY WHITESPACE. So this projection
    drops exactly those and keeps `diverse-weather` intact.

    `flatten` itself is deliberately NOT changed. Every `P<i>:<a>-<b>` address in this
    repository is a pair of offsets into its output, so changing it would silently move
    every stored reference — the `extraction_version` problem, caused rather than avoided.
    This is a second projection used for SEARCHING, and the offsets it returns are indices
    back into `flat`, so an address minted through it is in the same coordinate system as
    every address minted before it.
    """
    kept_chars: list[str] = []
    kept_index: list[int] = []
    for i, ch in enumerate(flat):
        if ch == "-":
            at = offsets[i]
            nxt = original[at + 1] if at + 1 < len(original) else ""
            if nxt and nxt.isspace():
                continue                          # a hyphen the line break inserted
        kept_chars.append(ch)
        kept_index.append(i)
    return "".join(kept_chars), kept_index


def section_units(doc: PaperDoc) -> list[tuple[int, str, list[int], str, int]]:
    """(section_idx, flat, offsets, original_text, page) per non-empty section."""
    out = []
    for s in doc.sections:
        if not (s.text or "").strip():
            continue
        flat, offsets = flatten(s.text)
        out.append((s.section_idx, flat, offsets, s.text, s.page_start))
    return out


def _verbatim(original: str, offsets: list[int], start: int, end: int) -> str:
    """The original text spanned by flattened [start, end), including its whitespace."""
    if not offsets or start >= end or end > len(offsets):
        return ""
    return original[offsets[start]:offsets[end - 1] + 1]


# --------------------------------------------------------------------------- #
# Quantities
# --------------------------------------------------------------------------- #
# A magnitude the number itself does not carry. `2.9K` is 2,900 and `3.0 billion` is
# 3,000,000,000, and `_SIGNED` reads both as the bare mantissa because it stops at the
# first non-numeric character. Scaling them here would be inventing a quantity the span
# does not state in the units it states it in, so they are REFUSED instead.
_MAGNITUDE_LETTER = re.compile(r"\d\s*[kKmMbBgGtT](?![A-Za-z0-9])")
_MAGNITUDE_WORD = re.compile(r"\d\s*(?:thousand|million|billion|trillion|bn|mn)\b", re.I)
_BRACKETED_CITATION = re.compile(r"\[\s*\d+\s*\]")


def _magnitude_suffixed(rhs: str, number: str) -> bool:
    """Does the number on this right-hand side carry a magnitude the float does not?

    The defect this closes, measured: a paper writing "58 topics x 5 templates x 10
    instances = 2.9K test cases" had its total read as **2.9**, the product 2,900
    compared against it, and the mismatch published as `PAPER_ARITHMETIC_CONTRADICTION`.
    That disposition is admissible by itself — it deliberately bypasses the provenance
    ceiling, because paper-internal arithmetic needs no execution to be checked — so it
    reached `claim_status = VERIFIED_FAILURE` and RED with no model, no execution and no
    grading in the chain. The printed reason then read "the paper prints 58*5*10 = 2.9",
    naming a quantity the paper does not contain. Zero of the eight corpus papers trip
    it, so nothing shipped is wrong; the path was live and would convict the first paper
    that rounds its own total, which is a thing papers do constantly.

    Only the suffix ON the parsed number matters, so `= 2900 test cases` is untouched:
    a trailing unit word is not a magnitude, and refusing it would refuse the common case.
    """
    idx = rhs.find(number)
    if idx < 0:
        return False
    tail = rhs[idx:idx + len(number) + 12]
    return bool(_MAGNITUDE_LETTER.search(tail) or _MAGNITUDE_WORD.search(tail))


def _printed_half_width(number: str) -> float:
    """Half the rounding interval the printed digits imply, 0.0 when undeterminable.

    `local_exec.printed_precision_half_width` is the same reasoning on the execution
    side. It is not imported because that module imports this one.
    """
    try:
        exponent = Decimal(number).as_tuple().exponent
    except (InvalidOperation, ValueError, TypeError):
        return 0.0
    return 0.5 * (10 ** exponent) if isinstance(exponent, int) else 0.0


def parse_quantity(text: str) -> ReportedQuantity | None:
    """The number a span reports, or None when no single number is unambiguous.

    Three rules, in order, and the third is a REFUSAL:

      1. One `=` in the span, exactly one number to its right  =>  that is the value.
         The left-hand side is additionally read as a composition when its numbers and
         multiplication signs strictly alternate (`58 topics x 5 templates x 10
         instances`), and the composition is then RE-EVALUATED here rather than trusted.
      2. No `=`, exactly one number in the span  =>  that number is the value.
      3. Anything else  =>  None.

    Rule 3 is why a table caption listing six numbers yields nothing. Picking one would
    be positional coincidence, which is the same defect `local_exec.json_metric`'s
    last-object-wins fallback is criticised for; a span that reports two unrelated
    numbers reports no single quantity, and saying so is the honest answer.
    """
    raw = _THOUSANDS.sub("", (text or "").strip())
    if not raw:
        return None
    if raw.count("=") > 1:
        return None

    if "=" in raw:
        lhs, rhs = raw.split("=", 1)
        rhs_nums = _SIGNED.findall(rhs)
        if len(rhs_nums) != 1:
            return None
        if _magnitude_suffixed(rhs, rhs_nums[0]):
            return None
        value = float(rhs_nums[0])
        operands, expression = _composition(lhs, value)
        ok: bool | None = None
        if expression:
            product = 1.0
            for o in operands:
                product *= o
            # The paper is credited the precision it actually printed, exactly as
            # `local_exec.reconcile` credits a reproduced measurement. A paper writing
            # "0.33 x 3 = 0.99" against a printed 1.0 has rounded, not erred, and
            # `math.isclose` at rel_tol 1e-9 called that a contradiction. The half-width
            # of the last printed digit is the honest tolerance: it is what the authors
            # claimed, and nothing more is being asserted by them or required of them.
            half = _printed_half_width(rhs_nums[0])
            ok = math.isclose(product, value, rel_tol=1e-9, abs_tol=max(half, 1e-9))
        return ReportedQuantity(value=value, raw=rhs_nums[0], operands=operands,
                                expression=expression, arithmetic_ok=ok)

    nums = _SIGNED.findall(raw)
    if len(nums) != 1:
        return None
    # A lone scholarly reference such as ``U-Net [25]`` is an address to another work,
    # not a reported experimental value. Treating it as 25 minted a reproduction target
    # whose expected result was literally the bibliography index.
    if _BRACKETED_CITATION.search(raw):
        return None
    return ReportedQuantity(value=float(nums[0]), raw=nums[0])


def _tokens(lhs: str, separators: bool) -> list[tuple[int, str, str]]:
    """Numbers and operators in source order. `separators` admits the unknown-glyph tier."""
    out: list[tuple[int, str, str]] = [
        (m.start(), "num", m.group()) for m in _UNSIGNED.finditer(lhs)]
    for i, ch in enumerate(lhs):
        if ch in _MULT:
            out.append((i, "op", "*"))
    for m in _LONE_X.finditer(lhs):
        out.append((m.start(), "op", "*"))
    if separators:
        for m in _LONE_SEPARATOR.finditer(lhs):
            out.append((m.start(), "op", "*"))
    out.sort()
    return out


def _alternates(tokens: list[tuple[int, str, str]]) -> list[float] | None:
    """The operands of the composition immediately left of the `=`, or None.

    Read from the RIGHT, not over the whole left-hand side. A composition sits against
    the equality and prose sits in front of it, and a real sentence has both:

        We construct the benchmark by sampling 10 instances per symbolic template with
        distinct random seeds, yielding 58 topics x 5 templates x 10 instances = 2,900

    Requiring the whole left side to alternate rejects that, because of the `10` in
    "10 instances per symbolic template" — which is prose about the method, with no
    operator joining it to what follows. Taking the maximal ALTERNATING RUN THAT ENDS AT
    THE EQUALITY keeps the strictness where it matters (a chain of numbers joined by
    operators, uninterrupted) and stops demanding that the author write no prose.

    Two numbers with no operator between them still break the chain, which is what stops
    "3 seeds and 4 datasets = 12" from being read as a composition: the chain is one
    number long and states nothing.
    """
    if not tokens or tokens[-1][1] != "num":
        return None
    run = [tokens[-1]]
    expect = "op"
    for tok in reversed(tokens[:-1]):
        if tok[1] != expect:
            break
        run.append(tok)
        expect = "num" if expect == "op" else "op"
    if run[-1][1] != "num":                      # a run may not begin with an operator
        run.pop()
    run.reverse()
    if len(run) < 3:
        return None
    return [float(v) for _, kind, v in run if kind == "num"]


def _composition(lhs: str, total: float | None = None) -> tuple[list[float], str]:
    """([operands], 'a*b*c') when the left-hand side states a multiplicative composition.

    **Two tiers, and the asymmetry between them is the safety argument.**

    A PDF does not preserve `×`. The multiplication sign is whatever glyph the embedded
    font's encoding happens to name, and extraction returns that glyph: the FinChain
    benchmark paper's `58 × 5 × 10 = 2,900` arrives as `58 ϵ 5 ϵ 10 = 2,900`, a Greek
    lunate epsilon. Refusing every glyph not on a fixed list means refusing the real
    composition claims this module exists to reach.

    So an unrecognised single-character separator standing alone between two numbers is
    admitted as multiplication — but ONLY when the operands actually multiply to the
    printed total. The reasoning is the operator's identity is being INFERRED, and the
    only evidence for the inference is that the paper's own arithmetic then works out.
    Where it does not work out, there are two readings — a different operator, or a
    genuine arithmetic error — and nothing in the glyph distinguishes them. Choosing the
    second would be accusing a paper of bad arithmetic on the strength of a font
    encoding.

    A RECOGNISED operator carries no such ambiguity, so it is admitted either way, and a
    composition that does not evaluate is a finding.
    """
    known = _alternates(_tokens(lhs, separators=False))
    if known is not None:
        return known, "*".join(str(int(o)) if o.is_integer() else str(o) for o in known)

    inferred = _alternates(_tokens(lhs, separators=True))
    if inferred is None or total is None:
        return [], ""
    product = 1.0
    for o in inferred:
        product *= o
    if not math.isclose(product, total, rel_tol=1e-9, abs_tol=1e-9):
        return [], ""
    return inferred, "*".join(str(int(o)) if o.is_integer() else str(o) for o in inferred)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _unresolved(ref: str, kind: str, state: str, detail: str) -> ClaimRef:
    return ClaimRef(ref=ref, kind=kind, resolution=state, detail=detail)


def resolve(doc: PaperDoc, ref: str, quote: str = "") -> ClaimRef:
    """Re-derive an address against the parsed paper. Never trusts the caller.

    When `quote` is supplied it is checked against what actually sits at the address, so
    a reference and a quotation that disagree resolve to `span_mismatch` rather than to
    whichever half the caller preferred.
    """
    ref = (ref or "").strip()

    m = _CELL_REF.match(ref)
    if m:
        t, r, c = (int(g) for g in m.groups())
        for table in doc.tables:
            if table.table_idx == t:
                cell = table.cell(r, c)
                if not cell:
                    return _unresolved(ref, "table_cell", "not_found",
                                       f"table {t} has no cell at r{r}:c{c}")
                if quote and _flat(quote) not in _flat(cell):
                    return _unresolved(ref, "table_cell", "span_mismatch",
                                       f"the cell holds {cell!r}, which does not contain the quote")
                return ClaimRef(ref=ref, kind="table_cell", quote=cell, page=table.page,
                                resolution="resolved", quantity=parse_quantity(cell))
        return _unresolved(ref, "table_cell", "not_found", f"no table {t} was extracted")

    m = _FIG_REF.match(ref)
    if m:
        idx = int(m.group(1))
        for fig in doc.figures:
            if fig.figure_idx == idx:
                return ClaimRef(ref=ref, kind="figure", quote=fig.caption, page=fig.page,
                                resolution="resolved")
        return _unresolved(ref, "figure", "not_found", f"no figure {idx} was extracted")

    m = _EQ_REF.match(ref)
    if m:
        idx = int(m.group(1))
        for eq in doc.equations:
            if eq.equation_idx == idx:
                return ClaimRef(ref=ref, kind="equation", quote=eq.text, page=eq.page,
                                resolution="resolved")
        return _unresolved(ref, "equation", "not_found", f"no equation {idx} was extracted")

    m = _SEC_REF.match(ref)
    if m:
        idx = int(m.group(1))
        for s in doc.sections:
            if s.section_idx == idx:
                return ClaimRef(ref=ref, kind="section_span", quote=s.title or s.text[:120],
                                section_idx=idx, page=s.page_start, resolution="resolved")
        return _unresolved(ref, "section_span", "not_found", f"no section {idx}")

    m = _PROSE_REF.match(ref)
    if m:
        idx, start, end = (int(g) for g in m.groups())
        for section_idx, flat, offsets, original, page in section_units(doc):
            if section_idx != idx:
                continue
            if start >= end or end > len(flat):
                return _unresolved(ref, "prose_claim", "malformed",
                                   f"span {start}-{end} is outside section {idx} "
                                   f"({len(flat)} characters)")
            text = _verbatim(original, offsets, start, end)
            if quote and _flat(quote) != flat[start:end]:
                return _unresolved(ref, "prose_claim", "span_mismatch",
                                   f"the span holds {text!r}, which is not the quote")
            return ClaimRef(ref=ref, kind="prose_claim", quote=text, section_idx=idx,
                            span=(start, end), page=page, resolution="resolved",
                            quantity=parse_quantity(text))
        return _unresolved(ref, "prose_claim", "not_found", f"no section {idx}")

    return _unresolved(ref, "", "malformed", f"{ref!r} is not an address in any known form")


def _self_projection(flat_quote: str) -> tuple[str, list[int], str]:
    """A quote as its own (flat, offsets, original), so the same projection applies to it.

    The quote has already been flattened, so its "original" is itself and its offsets are
    the identity — which means a hyphen inside it is never followed by whitespace and none
    are dropped. That is correct: the READER wrote the quote as continuous text, so any
    hyphen it contains is one the reader meant. The projection is applied to it anyway so
    the two sides go through one function rather than two rules.
    """
    return flat_quote, list(range(len(flat_quote))), flat_quote


def mint(doc: PaperDoc, quote: str) -> ClaimRef:
    """Find `quote` in the paper and mint the address for it. The harness's half.

    Refuses on anything but exactly one occurrence. An ambiguous quote is a real quote
    at an address nobody can name; taking its first occurrence would produce a stable id
    for a span the lens may not have meant, and every downstream artifact would then
    carry that guess as if it were a fact.

    A quote that matches a table cell resolves as a `table_cell`, not as prose, so the
    existing cell-verified evidence class and everything keyed on it are unaffected.
    """
    flat_quote = _flat(quote)
    if len(flat_quote) < _QUOTE_MIN:
        return _unresolved("", "", "malformed",
                           f"a {len(flat_quote)}-character quote addresses nothing in particular")

    hits: list[ClaimRef] = []
    for table in doc.tables:
        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row):
                if cell and _flat(cell) == flat_quote:
                    hits.append(ClaimRef(ref=table.ref(r, c), kind="table_cell", quote=cell,
                                         page=table.page, resolution="resolved",
                                         quantity=parse_quantity(cell)))
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return _unresolved("", "table_cell", "ambiguous",
                           f"the quote is the whole contents of {len(hits)} different cells")

    units = section_units(doc)
    for section_idx, flat, offsets, original, page in units:
        start = flat.find(flat_quote)
        while start != -1:
            end = start + len(flat_quote)
            text = _verbatim(original, offsets, start, end)
            hits.append(ClaimRef(ref=f"P{section_idx}:{start}-{end}", kind="prose_claim",
                                 quote=text, section_idx=section_idx, span=(start, end),
                                 page=page, resolution="resolved",
                                 quantity=parse_quantity(text)))
            start = flat.find(flat_quote, start + 1)
            if len(hits) > 1:
                break
        if len(hits) > 1:
            break

    if not hits:
        # THE LINE-BREAK HYPHEN, and only then. Tried second rather than first so a quote
        # that matches the paper exactly is never resolved through a normalisation, and so
        # the cheap search stays the common path. The address that comes back is in `flat`
        # coordinates like every other, because `soft_hyphen_projection` returns indices
        # into `flat` rather than into its own output.
        for section_idx, flat, offsets, original, page in units:
            projected, index = soft_hyphen_projection(flat, offsets, original)
            probe, _ = soft_hyphen_projection(*_self_projection(flat_quote))
            if len(probe) < _QUOTE_MIN:
                continue
            start = projected.find(probe)
            while start != -1:
                a, b = index[start], index[start + len(probe) - 1] + 1
                text = _verbatim(original, offsets, a, b)
                hits.append(ClaimRef(ref=f"P{section_idx}:{a}-{b}", kind="prose_claim",
                                     quote=text, section_idx=section_idx, span=(a, b),
                                     page=page, resolution="resolved",
                                     quantity=parse_quantity(text)))
                start = projected.find(probe, start + 1)
                if len(hits) > 1:
                    break
            if len(hits) > 1:
                break

    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return _unresolved("", "prose_claim", "ambiguous",
                           "the quote occurs more than once, so no single span addresses it")
    return _unresolved("", "", "not_found", "the quote does not occur in the parsed paper")


def address(doc: PaperDoc, ref: str = "", quote: str = "") -> ClaimRef:
    """The one entry point the rest of the harness should use.

    An address the caller supplied is re-derived; when it does not resolve — including
    the `p7`-style page citations lenses have always been allowed to write, which name a
    page and not a span — the quote is minted into one instead. So a lens that cites
    loosely still gets a checkable address whenever its quotation is good enough to earn
    one, and gets nothing when it is not.
    """
    if ref:
        got = resolve(doc, ref, quote)
        if got.resolved:
            return got
        if not quote:
            return got
        minted = mint(doc, quote)
        if minted.resolved:
            minted.detail = f"minted from the quote; the supplied ref {ref!r} did not resolve"
            return minted
        return got if got.resolution != "malformed" else minted
    return mint(doc, quote)


def _flat(s: str) -> str:
    return _WS.sub("", (s or "").lower())


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    from .artifacts import Section, Table

    doc = PaperDoc(
        paper_id="selfcheck",
        sections=[Section(section_idx=0, title="Benchmark", page_start=3,
                          text="We construct 58 topics x 5 templates x 10 instances = 2,900 "
                               "test cases in total.\nEach case is checked by hand."),
                  Section(section_idx=1, title="Repeat", page_start=4,
                          text="Each case is checked by hand.")],
        tables=[Table(table_idx=1, page=5, rows=[["method", "59.3"], ["ours", "61.4"]])],
    )

    got = mint(doc, "58 topics x 5 templates x 10 instances = 2,900 test cases")
    assert got.resolved and got.kind == "prose_claim", got
    assert got.quantity and got.quantity.value == 2900.0, got.quantity
    assert got.quantity.expression == "58*5*10" and got.quantity.arithmetic_ok is True

    # the harness re-derives the address it minted
    again = resolve(doc, got.ref, got.quote)
    assert again.resolved and again.quote == got.quote

    # a lens cannot point an address at text that is not its quote
    bad = resolve(doc, got.ref, "something else entirely")
    assert bad.resolution == "span_mismatch", bad

    # a quote occurring in two sections addresses nothing
    assert mint(doc, "Each case is checked by hand.").resolution == "ambiguous"
    assert mint(doc, "not in the paper at all").resolution == "not_found"
    assert mint(doc, "short").resolution == "malformed"

    cell = mint(doc, "59.3")
    assert cell.resolution == "malformed", "a 4-character quote is below the floor"
    assert resolve(doc, "T1:r0:c1").quantity.value == 59.3
    assert resolve(doc, "T9:r0:c0").resolution == "not_found"

    # refusals
    assert parse_quantity("we ran 3 seeds on 4 datasets") is None
    assert parse_quantity("3 seeds and 4 datasets = 12").expression == ""
    assert parse_quantity("58 x 5 x 10 = 2901").arithmetic_ok is False
    assert parse_quantity("accuracy was 59.3") .value == 59.3

    # --- the unknown-glyph tier -------------------------------------------------------
    # A real extraction: FinChain's `58 x 5 x 10 = 2,900` arrives from the PDF with the
    # multiplication sign rendered as a Greek lunate epsilon.
    got = parse_quantity("yielding 58 topics ϵ 5 tem- plates ϵ 10 instances = 2,900 test cases")
    assert got and got.value == 2900.0 and got.expression == "58*5*10"
    assert got.arithmetic_ok is True

    # ...and the asymmetry that makes admitting it safe: an INFERRED operator may never
    # convict a paper of bad arithmetic, because the failure and a different operator are
    # indistinguishable in an unrecognised glyph.
    bad = parse_quantity("58 ϵ 5 ϵ 10 = 2901")
    assert bad and bad.value == 2901.0 and bad.expression == "", \
        "an unrecognised glyph that does not multiply out states no composition"
    assert bad.arithmetic_ok is None
    # a RECOGNISED operator still convicts
    assert parse_quantity("58 * 5 * 10 = 2901").arithmetic_ok is False
    print("harness.claims self-check ok")


if __name__ == "__main__":
    _self_check()
