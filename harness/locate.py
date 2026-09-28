"""Addressable references into a parsed paper, and the quantities they report.

`python -m harness.locate` runs the self-check.

THE INVARIANT. A lens supplies a QUOTE; the harness mints the ADDRESS. `mint()` searches
the parsed document for the quote and refuses unless it occurs exactly once -- an
ambiguous quote yields no address rather than its first occurrence, because a stable id
for a span the lens may not have meant is worse than no id. A lens that writes a
`P<i>:<a>-<b>` address itself gains nothing: `resolve()` re-reads the span off the
document and returns `span_mismatch` when the text there is not the quote.

TWO COORDINATE SYSTEMS, KEPT APART ON PURPOSE. Addresses are minted in FLATTENED
coordinates (whitespace removed, lowercased) because a PDF breaks a sentence across lines
wherever the column happens to end, and an address that moved when the extractor
re-wrapped a line would not be stable. The `quote` handed back is sliced from the
ORIGINAL section text through an index map, so what a reader is shown is verbatim.
Flattening is per SECTION, never across sections, so a string straddling a section seam
can never verify.

THE SOFT-HYPHEN PROJECTION. A typesetter breaking "generation" across a line leaves
"gener-" and "ation"; `flatten` turns that into `gener-ation`, and a reader quoting the
sentence normally writes `generation` -- refused as not present without this. A soft
hyphen is decidable only from the ORIGINAL text: a line-break hyphen is the one
immediately followed by whitespace. Tried only after an exact match fails, so a
character-for-character quotation is never resolved through a normalisation.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

from .schema import ClaimRef, PaperDoc, ReportedQuantity

_WS = re.compile(r"\s+")
_CELL_REF = re.compile(r"^T(\d+):r(\d+):c(\d+)$")
_FIG_REF = re.compile(r"^F(\d+)$")
_EQ_REF = re.compile(r"^E(\d+)$")
_SEC_REF = re.compile(r"^S(\d+)$")
_PROSE_REF = re.compile(r"^P(\d+):(\d+)-(\d+)$")

# Below this, a quote addresses nothing in particular: "the" occurs everywhere.
_QUOTE_MIN = 8

# Only MULTIPLICATIVE composition is recognised — `-`/`+`/`/` cannot be told apart from a
# sign or from "per" without parsing the sentence, and a wrong `arithmetic_ok` is a false
# machine attestation.
_MULT = {"*": "*", "×": "*", "·": "*", "∗": "*"}
# A lone `x` is the multiplication sign in half of all extracted PDFs. Admitted only
# between non-alphanumerics, so `max`/`x_i`/`2x` are untouched.
_LONE_X = re.compile(r"(?<![A-Za-z0-9_])[xX](?![A-Za-z0-9_])")
# A single non-alphanumeric glyph alone between two numbers — what a multiplication sign
# looks like after a font encoding loses its name. See `_composition` for why this tier
# may only be read as multiplication when the paper's own arithmetic confirms it.
_LONE_SEPARATOR = re.compile(r"(?<=[\s\d])\s*([^\sA-Za-z0-9=.,()\[\]])\s*(?=[\s\d])")
_UNSIGNED = re.compile(r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
# An operand stands alone: a digit glued to an identifier, operator or norm bar is a
# subscript/exponent (`f2`, `n−1`, `∥A∥2`), and a percentage composes, it does not multiply.
_OPERAND = re.compile(r"(?<![^\s(\[$])\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\d.A-Za-z_(%′'])")
# Relational or analysis notation marks a mathematical statement (an inequality chain, a
# norm identity), not a flat numeric composition; its `=` is never re-evaluated as one.
_MATH_NOTATION = re.compile("[≤≥<>≠≈∝∥∇∑∏∫∂∈∀∃→]")
_SIGNED = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")               # 2,900 -> 2900


# --------------------------------------------------------------------------- #
# Flattening, with an index back to the original text
# --------------------------------------------------------------------------- #
def flatten(text: str) -> tuple[str, list[int]]:
    """(flattened, original_index_per_flattened_char)."""
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
    `flatten` itself is deliberately NOT changed -- every `P<i>:<a>-<b>` address in this
    repository is a pair of offsets into its output. This is a second projection used
    for SEARCHING; its offsets are indices back into `flat`."""
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
# A magnitude the number itself does not carry: `2.9K` is 2,900. Scaling it here would
# invent a quantity the span does not state in the units it states it in — refused.
_MAGNITUDE_LETTER = re.compile(r"\d\s*[kKmMbBgGtT](?![A-Za-z0-9])")
_MAGNITUDE_WORD = re.compile(r"\d\s*(?:thousand|million|billion|trillion|bn|mn)\b", re.I)
_BRACKETED_CITATION = re.compile(r"\[\s*\d+\s*\]")
# A cross-reference label is an ADDRESS, not a measurement: "as Table 3 shows" reports no
# quantity. Narrow deliberately — fires only when the span's SOLE number is the label.
_OBJECT_CITATION = re.compile(
    r"\b(?:table|tab|figure|fig|equation|eq|section|sec|appendix|app|algorithm|alg)"
    r"\s*\.?\s*\d", re.I)
# A reported spread ("0.024 ± 0.003", "12.1 +/- 0.4") qualifies the value before it; it is
# not a second quantity. Only the value is compared; the spread is the paper's own noise.
_SPREAD = re.compile(r"\s*(?:±|\+/-|\+-)\s*\d+(?:\.\d+)?\s*%?")
# An author-year citation names another work's publication year, never a measurement:
# "(Smith et al., 2020)", "Alfaro et al. (2023a)", "[Chen, 2019]".
_CITATION_YEAR = re.compile(
    r"(?:\bet\s+al\.?,?\s*\(?|[A-Z][A-Za-z\-]+,\s*|\(\s*)(?:19|20)\d{2}[a-z]?\b")
# A digit glued to an identifier or operator is a subscript, exponent or index ("N−1",
# "x_2", "f2", "k-1"), not a reported number.
_GLUED = re.compile(r"[A-Za-z_][−\-+^_]?$")
# Words that mark a number as an outcome. Shared with `paper.table_role`, so a caption and
# a sentence are judged by one vocabulary. Open-ended by design: it only EXEMPTS a span from
# the settings rule below, it never has to be complete for a metric to be read.
MEASURE_WORDS = re.compile(
    r"%|×|\b(?:RMSE|MSE|MAE|PEHE|ATE|AUC|AUROC|AUPRC|F1|accuracy|error|errors|loss|"
    r"precision|recall|BLEU|ROUGE|METEOR|perplexity|reward|return|regret|delay|ECE|"
    r"calibration|coverage|likelihood|NLL|score|bias|variance|FID|mAP|AP|IoU|mIoU|PSNR|SSIM|"
    r"LPIPS|WER|CER|NDCG\S*|MRR|hits@\S+|pass@\S+|EM|CRPS\w*|R2|R²|correlation|win rate|"
    r"success rate|runtime|latency|throughput|memory|speed-?up|faster|slower|improv\w*|"
    r"outperform\w*|reduc\w*|increas\w*|decreas\w*|times (?:smaller|larger|faster|lower|"
    r"higher)|gain|drop)\b", re.I)
# A number that SETS the experiment up: "we use 5 seeds", "3 layers", "learning rate of
# 0.01", "we set C = 10". Rejected unless the same span also reports an outcome.
_SETTING = re.compile(
    r"\b\d[\d,.]*\s*(?:random\s+|independent\s+)?(?:seeds?|layers?|epochs?|iterations?|"
    r"steps?|heads?|trees?|folds?|runs?|trials?|repetitions?|dimensions?|neurons?|units?|"
    r"GPUs?|nodes?|batch(?:es)?|hours?|minutes?)\b"
    r"|\b(?:seed|depth|width|batch size|learning rate|lr|temperature|horizon|window|budget|"
    r"dimension|length|set|fix|choose|use)\s*(?:of|is|to|=|as)?\s*[A-Za-z]?\s*=?\s*-?\d", re.I)


def measurement_context(text: str) -> bool:
    """May a prose number be read as a reported outcome? Yes unless its span only sets the
    experiment up; a span that also reports an outcome word stays readable."""
    text = text or ""
    return bool(MEASURE_WORDS.search(text)) or not _SETTING.search(text)


def _magnitude_suffixed(rhs: str, number: str) -> bool:
    """Does the number on this right-hand side carry a magnitude the float does not?
    ("58 x 5 x 10 = 2.9K test cases" must not have its total read as bare 2.9.) Only the
    suffix ON the parsed number counts, so "= 2900 test cases" is untouched."""
    idx = rhs.find(number)
    if idx < 0:
        return False
    tail = rhs[idx:idx + len(number) + 12]
    return bool(_MAGNITUDE_LETTER.search(tail) or _MAGNITUDE_WORD.search(tail))


def _printed_half_width(number: str) -> float:
    """Half the rounding interval the printed digits imply, 0.0 when undeterminable."""
    try:
        exponent = Decimal(number).as_tuple().exponent
    except (InvalidOperation, ValueError, TypeError):
        return 0.0
    return 0.5 * (10 ** exponent) if isinstance(exponent, int) else 0.0


def parse_quantity(text: str) -> ReportedQuantity | None:
    """The number a span reports, or None when no single number is unambiguous.

    Three rules, and the third is a REFUSAL: (1) one `=`, exactly one number to its
    right => that value, with the left-hand side additionally read as a re-evaluated
    composition when its numbers and multiplication signs strictly alternate; (2) no
    `=`, exactly one number in the span => that number; (3) anything else => None. A
    table caption listing six numbers yields nothing — picking one would be positional
    coincidence.
    """
    raw = _THOUSANDS.sub("", (text or "").strip())
    raw = _SPREAD.sub("", raw)
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
        operands, expression = ([], "") if _MATH_NOTATION.search(raw) else _composition(lhs, value)
        ok: bool | None = None
        if expression:
            product = 1.0
            for o in operands:
                product *= o
            # Credited the precision it actually printed, as `execute.reconcile` credits
            # a reproduced measurement: "0.33 x 3 = 0.99" against a printed 1.0 has
            # rounded, not erred.
            half = _printed_half_width(rhs_nums[0])
            ok = math.isclose(product, value, rel_tol=1e-9, abs_tol=max(half, 1e-9))
        return ReportedQuantity(value=value, raw=rhs_nums[0], operands=operands,
                                expression=expression, arithmetic_ok=ok)

    raw = _CITATION_YEAR.sub(" ", raw)
    found = list(_SIGNED.finditer(raw))
    if len(found) != 1:
        return None
    # A lone scholarly reference such as "U-Net [25]" is an address to another work, not
    # a reported value — treating it as 25 minted a reproduction target whose expected
    # result was literally the bibliography index.
    if _BRACKETED_CITATION.search(raw) or _OBJECT_CITATION.search(raw):
        return None
    if _GLUED.search(raw[:found[0].start()]):
        return None
    return ReportedQuantity(value=float(found[0].group()), raw=found[0].group())


def _tokens(lhs: str, separators: bool) -> list[tuple[int, str, str]]:
    """Numbers and operators in source order. `separators` admits the unknown-glyph tier."""
    out: list[tuple[int, str, str]] = [
        (m.start(), "num", m.group()) for m in _OPERAND.finditer(lhs)]
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

    Read from the RIGHT: the maximal alternating run ENDING AT THE EQUALITY, which keeps
    strictness where it matters (a chain of numbers joined by operators, uninterrupted)
    without demanding the author write no prose before it.
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

    Two tiers. A RECOGNISED operator (`*`, `x`, `×`, `·`, `∗`) is admitted unconditionally
    -- a composition using it that does not evaluate is a finding. An UNRECOGNISED glyph
    standing alone between numbers (a font-encoding artifact) is admitted ONLY when the
    operands actually multiply to the printed total, since the operator's identity is
    being inferred and the sole evidence for the inference is that the arithmetic works.
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
    """Re-derive an address against the parsed paper. Never trusts the caller: a
    supplied `quote` that disagrees with what actually sits at `ref` resolves to
    `span_mismatch` rather than to whichever half the caller preferred."""
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
    """A quote as its own (flat, offsets, original) so the same projection applies to it —
    its offsets are the identity, so a hyphen it contains is never dropped, which is
    correct since the reader wrote it as continuous text."""
    return flat_quote, list(range(len(flat_quote))), flat_quote


def mint(doc: PaperDoc, quote: str) -> ClaimRef:
    """Find `quote` in the paper and mint the address for it -- the harness's half.
    Refuses on anything but exactly one occurrence. A quote matching a table cell
    resolves as `table_cell`, not as prose."""
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
        # THE LINE-BREAK HYPHEN, tried only after the exact search fails, so a
        # character-for-character quotation is never resolved through a normalisation.
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


def mint_in(doc: PaperDoc, quote: str, section_idx: int) -> ClaimRef:
    """`mint`, restricted to ONE section: a proof step may repeat words the paper states
    elsewhere (a restated theorem, a recalled bound), and the section the harness found it
    in is the one it is about. Still refuses on anything but exactly one occurrence there."""
    flat_quote = _flat(quote)
    if len(flat_quote) < _QUOTE_MIN:
        return _unresolved("", "", "malformed",
                           f"a {len(flat_quote)}-character quote addresses nothing in particular")
    hits: list[ClaimRef] = []
    for idx, flat, offsets, original, page in section_units(doc):
        if idx != section_idx:
            continue
        start = flat.find(flat_quote)
        while start != -1 and len(hits) < 2:
            end = start + len(flat_quote)
            text = _verbatim(original, offsets, start, end)
            hits.append(ClaimRef(ref=f"P{idx}:{start}-{end}", kind="prose_claim", quote=text,
                                 section_idx=idx, span=(start, end), page=page,
                                 resolution="resolved", quantity=parse_quantity(text)))
            start = flat.find(flat_quote, start + 1)
    if len(hits) == 1:
        return hits[0]
    return _unresolved("", "prose_claim", "ambiguous" if hits else "not_found",
                       f"the quote occurs {len(hits)} time(s) in section {section_idx}")


def address(doc: PaperDoc, ref: str = "", quote: str = "") -> ClaimRef:
    """The one entry point the rest of the harness should use. A supplied address is
    re-derived; when it does not resolve (including a `p7`-style page citation, which
    names a page and not a span) the quote is minted into one instead."""
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
    from .schema import Section, Table

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

    again = resolve(doc, got.ref, got.quote)
    assert again.resolved and again.quote == got.quote

    bad = resolve(doc, got.ref, "something else entirely")
    assert bad.resolution == "span_mismatch", bad

    assert mint(doc, "Each case is checked by hand.").resolution == "ambiguous"
    assert mint(doc, "not in the paper at all").resolution == "not_found"
    assert mint(doc, "short").resolution == "malformed"

    cell = mint(doc, "59.3")
    assert cell.resolution == "malformed", "a 4-character quote is below the floor"
    assert resolve(doc, "T1:r0:c1").quantity.value == 59.3
    assert resolve(doc, "T9:r0:c0").resolution == "not_found"

    assert parse_quantity("we ran 3 seeds on 4 datasets") is None
    assert parse_quantity("3 seeds and 4 datasets = 12").expression == ""
    assert parse_quantity("58 x 5 x 10 = 2901").arithmetic_ok is False
    assert parse_quantity("accuracy was 59.3").value == 59.3
    # A proof line is not printed arithmetic: identifier digits (`f2`) and exponents
    # (`∥A∥2`) are not operands, and an inequality chain is never re-evaluated.
    assert not parse_quantity("for any x, y, ∥∇f2(x) −∇f2(y)∥= A [Ax]+ "
                              "≤∥A∥2 ∥x −y∥").expression
    assert not parse_quantity("f2 x 2 = 4").expression
    assert not parse_quantity("50% from sharing × 50% from INT8 = 75% total").expression

    # the unknown-glyph tier, and the asymmetry that makes admitting it safe
    got = parse_quantity("yielding 58 topics ϵ 5 tem- plates ϵ 10 instances = 2,900 test cases")
    assert got and got.value == 2900.0 and got.expression == "58*5*10"
    assert got.arithmetic_ok is True
    bad = parse_quantity("58 ϵ 5 ϵ 10 = 2901")
    assert bad and bad.value == 2901.0 and bad.expression == "", \
        "an unrecognised glyph that does not multiply out states no composition"
    assert bad.arithmetic_ok is None
    assert parse_quantity("58 * 5 * 10 = 2901").arithmetic_ok is False  # a RECOGNISED operator still convicts
    print("harness.locate self-check ok")


if __name__ == "__main__":
    _self_check()
