"""PDF -> `PaperDoc`, and the bounded-part reading plan that traverses it. Pure
functions: no LLM, no network, no `Config`. This is the only module in the harness that
opens a PDF.

Consolidates two reference-implementation modules (tag `reference-implementation-
2026-09-20`) into one:
  - `harness/pdf.py`       — PyMuPDF for page text, pdfplumber for ruled table structure.
  - `harness/reading.py`   — splitting a long paper into bounded, blind parts per lens,
                             with a deterministic anchor packet carried across them.

Repository URL discovery (`find_repo_urls`/`official_repo_url`) stays in `harness/repo.py`
alongside acquire/pin/verify_commit, which is the only caller (`stages/ingest.py` reaches
it through `repo`, not through this module) — an earlier revision of this module carried
its own duplicate copy of both functions plus a `parse()` entry point that called them;
neither was ever wired to a caller, and both were removed 2026-09-21.

Same models as the reference (`PaperDoc`, `Section`, `Table`, `Figure`, `Equation`,
`CrossRef`, `QuantFinding`), now defined once in `harness/schema.py` and imported here
rather than redefined. `abstract_section_idx`/`conclusion_section_idx` are `harness.decide`'s
(the v4 home of the old `harness/materiality.py` locators); `flatten`/`resolve` are
`harness.locate`'s (the v4 home of the old `harness/claims.py`).

`python -m harness.paper <file.pdf>` runs the self-check; omit the path to run only the
fixture-based half (reading plan needs no PDF).
"""
from __future__ import annotations

import bisect
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from .decide import abstract_section_idx, conclusion_section_idx
from .locate import flatten
from .schema import CrossRef, Equation, Figure, PaperDoc, QuantFinding, Section, Table

# =========================================================================================
# Extraction
# =========================================================================================

# Bumped whenever a change RENUMBERS or RE-SCOPES an addressable object — `F<n>`,
# `T<i>:r<r>:c<c>`, `E<n>`, `S<i>`, `P<i>:<a>-<b>`. Stamped onto every `PaperDoc` this
# module produces, so a reference minted against an older parse can be recognised as
# stale instead of silently resolving to a different object — the same false-attestation
# failure `_equation_body` refuses to make, one layer up. A cached `doc.json` is never
# re-parsed under a bumped version; see `stages/ingest.py`.
#
# 2: cross-references stopped being ingested as captions (`_FIGURE_CAPTION`/
#    `_TABLE_CAPTION` now require the delimiter), table captions became verbatim, the
#    geometric table fallback stopped being gated on the ruled path finding nothing, and
#    bibliography lines stopped being read as appendix headings.
EXTRACTION_VERSION = 2

# ponytail: bounds sized for conference ML papers (8-10pp + appendix). A long paper is
# truncated, not crashed; raise these if that ever becomes the workload.
MAX_PAGES = 60
MAX_TABLES = 40
MAX_ROWS = 60
MAX_COLS = 12
MAX_SECTION_CHARS = 40_000
# A caption is set within a line or two of the table it names. Beyond this the two are
# unrelated floats that merely share a page, and pairing them would invent provenance.
MAX_CAPTION_GAP = 120.0

_KNOWN_HEADINGS = (
    r"abstract|introduction|related work|background|preliminaries|notation|"
    r"method(?:s|ology)?|approach|architecture|model|training|"
    r"experimental setup|experiments?|setup|results?|evaluation|analysis|"
    r"ablations?(?:\s+stud(?:y|ies))?|discussion|limitations?|conclusions?|"
    r"future work|acknowledgements?|acknowledgments?|references|bibliography|"
    r"appendix|broader impact|ethics statement|reproducibility statement"
)
# Appendices are lettered ("A Appendix", "B.2 Dataset details") as often as numbered, and
# appendix content is where evaluation protocol detail usually hides.
_SECNO = r"(?:\d+|[A-Z])(?:\.\d+)*"
_NAMED_HEADING = re.compile(rf"^(?:{_SECNO}\.?\s+)?({_KNOWN_HEADINGS})\b.{{0,40}}$", re.I)
_NUMBERED_HEADING = re.compile(rf"^({_SECNO})\.?\s+([A-Z][^.!?]{{1,68}})$")

# A caption NAMES an object; a cross-reference MENTIONS one. Both begin a line with
# "Figure 4", and the delimiter after the number is the only thing that separates them
# in flattened text: a caption prints "Figure 4." or "Figure 4:", a citation prints
# "Figure 4 shows" or "Figure 4, this". The delimiter used to be OPTIONAL, and body prose
# ("Figure 4 shows the visual comparison...") became a fabricated `Figure` object that
# `verify_evidence` then certified as caption-verified — the `_FIGURE_CAPTION` bug. It can
# only REMOVE objects, never manufacture provenance; the cost is a sentence-final citation
# ("...as shown in Figure 3.") and a subfigure caption ("Figure 5a: ..."), neither
# distinguishable from the other case in flattened text.
_CAPTION_DELIM = r"(?:\s*[:.—-](?=\s|$))"
_TABLE_CAPTION = re.compile(
    rf"^\s*(?:table|tab\.)\s*([IVXLC]+|\d+){_CAPTION_DELIM}\s*(.{{0,200}})", re.I)
# The delimiter-OPTIONAL shape, kept for one job: a line beginning "Table 4" is not a row
# of table data, whether it names a table or merely cites one, so it must not be swallowed
# into a table body. `is_heading`/`_heading_admitted` is split the same way, one predicate
# per question.
_TABLE_LINE_SHAPED = re.compile(r"^\s*(?:table|tab\.)\s*(?:[IVXLC]+|\d+)\b", re.I)
# The same strict rule, minus the "delimiter then whitespace" lookahead, for pdfplumber
# WORD ROWS: a PDF with no space glyph in its font encoding returns a whole visual line as
# one token, so the lookahead can never hold there and the caption row goes unrecognised —
# not hypothetical, it silently cost seven captioned tables their bodies on one paper.
_TABLE_CAPTION_ROW = re.compile(r"^\s*(?:table|tab\.)\s*(?:[IVXLC]+|\d+)\s*[:.—-]", re.I)
# Mirrors `_TABLE_CAPTION`, substituting "figure"/"fig." — same reasoning, same shape.
_FIGURE_CAPTION = re.compile(
    rf"^\s*(?:figure|fig\.)\s*([IVXLC]+|\d+){_CAPTION_DELIM}\s*(.{{0,300}})", re.I)

# A display equation, text-extracted: a line carrying a relational operator, numbered the
# way LaTeX numbers equations. Deliberately lossy — a text-line heuristic, not layout
# geometry; `SOURCE_FIDELITY` in the audit prompt says so.
#
# TWO SHAPES, because real PDFs use both. An equation number typeset in the right margin
# is a SEPARATE text block, so pymupdf's reading order emits it on its own line:
# `_EQUATION_NUMBER_ONLY` matches that, and the body is the nearest preceding line
# carrying a relational operator. Requiring both on one line matched a synthetic fixture
# and zero equations across every real paper in the corpus.
_EQUATION_LINE = re.compile(r"^(.{1,220}?[=≤≥∝≈<>].{0,220}?)\s*\((\d{1,3}[a-z]?)\)\s*$")
_EQUATION_NUMBER_ONLY = re.compile(r"^\(\s*(\d{1,3}[a-z]?)\s*\)$")
_EQUATION_BODY = re.compile(r"[=≤≥∝≈<>]")
# How far back to look for the body belonging to a lone number. Small on purpose: an
# equation's own line is normally immediately above its number, and a wide window would
# attach the number to an unrelated sentence several lines up.
_EQUATION_LOOKBACK = 3
# A body line must not be ordinary prose that merely contains a comparison: prose is long
# and word-dense, a display equation is short and symbol-dense.
_EQUATION_MAX_WORDS = 24
MAX_FIGURES = 40
MAX_EQUATIONS = 60
# A caption line is kept VERBATIM, so it needs its own bound. Truncation keeps a PREFIX of
# the printed line, still a substring of the page — a reconstructed string is not.
MAX_CAPTION_CHARS = 240

# Where the prose CITES a numbered object. Digits only, deliberately: with `re.I` a
# roman-numeral alternative matches the "i" in "figures in the appendix" and mints a
# citation nobody wrote. Only the first number of "Figures 3 and 4" is recovered.
_CROSSREF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("figure", re.compile(r"\b(?:figures?|figs?\.)\s*(\d+)", re.I)),
    ("table", re.compile(r"\b(?:tables?|tabs?\.)\s*(\d+)", re.I)),
    ("equation", re.compile(r"\b(?:equations?|eqs?\.|eqn\.?)\s*\(?(\d+)", re.I)),
    ("section", re.compile(r"\b(?:sections?|secs?\.|§)\s*(\d+(?:\.\d+)*)", re.I)),
    # Not `re.I` on the letter: an appendix is lettered with a CAPITAL, and a
    # case-insensitive group would read "appendix and" as a citation of appendix A.
    ("appendix", re.compile(r"(?i:appendix)\s*([A-Z](?:\.\d+)*)")),
)
MAX_CROSSREFS = 400
# The citing sentence, in flattened characters. Long enough to carry the claim the
# citation is embedded in, short enough that a runaway span cannot quote half the paper.
MAX_CROSSREF_QUOTE = 400
_SENTENCE_END = re.compile(r"[.!?]\s")

# A bare known-heading word — `Method`, `Model`, `Training` alone on a line — is what a
# table's first column header looks like once pymupdf emits each cell on its own line.
# The corroboration is the line that FOLLOWS: a real heading is followed by a paragraph's
# first line (>=6 words, measured); a header cell by another cell (1-4 words, measured).
_HEADING_FOLLOWED_BY_WORDS = 5
# Inside the reference range, a section number that is neither a small integer nor a
# letter continuing an appendix sequence is a year or page number at the start of a
# bibliography line. Two digits, because no paper has a hundred top-level sections.
_MAX_BARE_SECTION_NUMBER_DIGITS = 2
_REFERENCES_HEADING = re.compile(r"^(?:\d+|[A-Z])?\.?\s*(?:references|bibliography)\b", re.I)
_APPENDIX_LETTER = re.compile(r"^[A-Z]$")

# C0 control characters other than tab and newline — real prompts carry them (straight
# out of PDF text extraction), and an unhardened boundary's failure mode on a reader is
# "the command exited without writing anything": a silent cause.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").replace("­", "")).strip()


def sanitise_controls(s: str, replacement: str = " ") -> str:
    """Strip C0 control characters, keeping tab and newline.

    A boundary function, not an extraction one: `doc.json` stays byte-faithful to what
    the PDF gave up; this is applied only where text leaves the harness for another
    process's stdin. `replacement` is a space, not '', so two words a NUL sat between do
    not become one word that appears nowhere in the paper.
    """
    return _CONTROL_CHARS.sub(replacement, s or "")


# Publication metadata a first-page line-scan reaches before the title on a journal
# reprint. A closed list of METADATA words, never of venues or titles.
_TITLE_METADATA = (
    "received", "accepted", "revised", "published", "submitted", "in press",
    "doi", "https://doi", "volume", "vol.", "issue", "pages", "pp.", "copyright",
    "proceedings of", "preprint", "arxiv", "under review", "keywords", "abstract",
    "correspondence", "editor",
)
# An author-affiliation marker: the asterisk-or-dagger-then-digit a two-column venue
# prints after each name. A rule about a TYPESETTING CONVENTION, not about names.
_AFFILIATION_MARKER = re.compile(r"[*†‡§]\s*\d")


def title_is_plausible(s: str) -> bool:
    """Could this string be a paper's title? Pure and report-only.

    Four real failures, all measured: a date line, a venue banner, a page range, and an
    author byline carrying affiliation markers. Deliberately does NOT recognise a byline
    with no markers — a comma-separated name list is shaped like a subtitle, and guessing
    would start rejecting real titles, a larger cost than printing one bad one.
    """
    line = _norm(s)
    if len(line) < 12 or len(line.split()) < 3:
        return False
    low = line.lower()
    if any(low.startswith(w) for w in _TITLE_METADATA):
        return False
    if _AFFILIATION_MARKER.search(line):
        return False
    digits = sum(c.isdigit() for c in line)
    letters = sum(c.isalpha() for c in line)
    # A title carries prose; a date line, a page range and a DOI are mostly digits and
    # punctuation — the only test here that catches all three without naming any.
    return letters > digits * 2 and letters >= 8


def is_heading(line: str) -> bool:
    """Does this line look like a section heading?

    ponytail: tuned for arXiv-style ML PDFs. A paper using unnumbered small-caps headings
    degrades to one large section — labels are lost, not correctness.
    """
    s = _norm(line)
    if not s or len(s) > 80 or len(s.split()) > 10:
        return False
    if s.endswith((".", ",", ";", ":")) and not _NAMED_HEADING.match(s):
        return False
    return bool(_NAMED_HEADING.match(s) or _NUMBERED_HEADING.match(s))


def page_texts(path: str | Path, max_pages: int = MAX_PAGES) -> list[str]:
    """Per-page text in reading order. Index 0 == page 1."""
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        return [str(doc[i].get_text("text")) for i in range(min(doc.page_count, max_pages))]


def _running_boilerplate(pages: list[str]) -> frozenset[str]:
    """Lines repeating verbatim across pages: running headers, footers, venue banners.
    These describe the VENUE, not the paper, so a title guess landing on one makes two
    different papers look identical. Detected structurally, never by a venue name list.
    """
    if len(pages) < 2:
        return frozenset()
    counts: dict[str, int] = {}
    for p in pages:
        for line in {_norm(raw) for raw in p.splitlines() if _norm(raw)}:
            counts[line] = counts.get(line, 0) + 1
    return frozenset(line for line, n in counts.items() if n >= 2)


def guess_title(pages: list[str]) -> str:
    """First substantial line of page 1 that is not a header/arXiv stamp/venue banner."""
    boilerplate = _running_boilerplate(pages)
    for raw in (pages[0] if pages else "").splitlines()[:25]:
        line = _norm(raw)
        if len(line) < 8 or "©" in line or line.lower().startswith(
                ("arxiv:", "preprint", "under review", "proceedings of")):
            continue
        # ICML-style templates repeat the TITLE as the running header, so a repeated line
        # is skipped only when it is not title-shaped (a banner, a `Journal | Vol |` footer,
        # or anything carrying a year, as venue running headers do).
        # Measured on 186 corpus PDFs: 47 changed (ACL banner, ICML byline -> title), 0 worse.
        if line in boilerplate and (not title_is_plausible(line) or "|" in line
                                    or re.search(r"\b(?:19|20)\d\d\b", line)):
            continue
        if _NAMED_HEADING.match(line):  # hit "Abstract" before finding a title
            break
        return line[:300]
    return ""


def _is_bare_known_heading(line: str) -> bool:
    """A known heading WORD with no section number in front — the shape a table's first
    column header is indistinguishable from. A numbered heading is corroborated by its
    own number and never subject to the following-line test."""
    if not _NAMED_HEADING.match(line):
        return False
    return not re.match(rf"^(?:{_SECNO})\.?\s", line)


def _heading_admitted(line: str, next_line: str, *, in_references: bool,
                      appendix_letter: str) -> bool:
    """Shape said "heading"; may the DOCUMENT overrule it?

    1. A bare known-heading word needs the next line to look like prose
       (`_HEADING_FOLLOWED_BY_WORDS`).
    2. Inside the reference range, a single capital at line start is an author's initial
       far more often than an appendix letter — the discriminator is SEQUENCE: appendix
       letters run A, B, C... from A, and a letter before any 'A' has been seen is not one.

    A rejected heading is not dropped: its text joins the preceding section, so the
    failure direction is a bibliography that stays whole under one label.
    """
    if _is_bare_known_heading(line) and len(next_line.split()) < _HEADING_FOLLOWED_BY_WORDS:
        return False
    m = _NUMBERED_HEADING.match(line)
    if not in_references or not m or _NAMED_HEADING.match(line):
        return True
    secno = m.group(1)
    if "." in secno:
        return True                       # 'A.1', '3.2' — a shape no reference line takes
    if _APPENDIX_LETTER.match(secno):
        return secno == "A" if not appendix_letter else secno >= appendix_letter
    return len(secno) <= _MAX_BARE_SECTION_NUMBER_DIGITS


def references_boundary(sections: Sequence[Section]) -> int:
    """The index of the References/Bibliography heading, or -1.

    A boundary INDEX, not a per-section `is_appendix` flag — the flag would be wrong on
    exactly the sections a mis-parsed bibliography produces, asserting a fact about the
    paper that is really a fact about the parse.
    """
    for s in sections:
        if _REFERENCES_HEADING.match(_norm(s.title)):
            return s.section_idx
    return -1


def split_sections(pages: list[str]) -> list[Section]:
    """Walk every line, starting a new Section at each ADMITTED heading.

    Text before the first heading becomes the front-matter section, so nothing is ever
    dropped — a heading the document overrules (`_heading_admitted`) contributes its own
    line to the section it interrupts rather than opening a new one.

    Lines are flattened across page boundaries first, because the corroboration test
    looks at the FOLLOWING line and a heading set at the foot of a page has its paragraph
    on the next one; reading page by page made every such heading fail.
    """
    lines: list[tuple[int, str]] = [
        (pno, _norm(raw))
        for pno, text in enumerate(pages, start=1)
        for raw in text.splitlines()
    ]
    sections: list[Section] = []
    title, buf, start = "", [], 1
    in_references, appendix_letter = False, ""

    def flush(end_page: int) -> None:
        body = _norm(" ".join(buf))[:MAX_SECTION_CHARS]
        if body or title:
            sections.append(Section(section_idx=len(sections), title=title,
                                    page_start=start, page_end=end_page, text=body))

    for i, (pno, line) in enumerate(lines):
        if not line:
            continue
        if is_heading(line):
            nxt = next((t for _, t in lines[i + 1:] if t), "")
            if _heading_admitted(line, nxt, in_references=in_references,
                                 appendix_letter=appendix_letter):
                flush(pno)
                title, buf, start = line, [], pno
                if _REFERENCES_HEADING.match(line):
                    in_references = True
                elif in_references and (m := _NUMBERED_HEADING.match(line)) \
                        and _APPENDIX_LETTER.match(m.group(1)):
                    appendix_letter = m.group(1)
                continue
        buf.append(line)
    flush(len(pages) or 1)
    return sections


def extract_figures(pages: list[str], max_figures: int = MAX_FIGURES) -> list[Figure]:
    """Every 'Figure N: ...' caption line, in reading order. NOT the figure's plotted
    content — this harness has no way to read that — only its caption text."""
    out: list[Figure] = []
    for pno, text in enumerate(pages, start=1):
        for raw in text.splitlines():
            m = _FIGURE_CAPTION.match(_norm(raw))
            if not m:
                continue
            label, caption = f"Figure {m.group(1)}", _norm(m.group(2))
            out.append(Figure(figure_idx=len(out), page=pno, label=label, caption=caption))
            if len(out) >= max_figures:
                return out
    return out


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The character range of the sentence containing [start, end) in `text`."""
    left = 0
    for m in _SENTENCE_END.finditer(text, 0, start):
        left = m.end()
    m = _SENTENCE_END.search(text, end)
    return left, (m.end() if m else len(text))


def extract_crossrefs(sections: Sequence[Section]) -> list[CrossRef]:
    """Every place the prose CITES a numbered object, with a re-verifiable address.

    Takes SECTIONS AND NOTHING ELSE — this function cannot see `doc.figures`,
    `doc.tables` or `doc.equations`, so it cannot compare a citation against a recovered
    label set and report anything "missing". A naive "cited but not recovered" check on
    the shipped corpus produced twelve false "missing table/equation" claims, all of them
    printed in the paper and absent only from what extraction recovered — see
    `harness.schema.CrossRef`.

    Each reference carries a `P<i>:<a>-<b>` span in the same flattened coordinates
    `harness.locate` mints, so the citing sentence is itself checkable. `page` is the
    section's first page — an honest ceiling, not an estimate.

    A caption occurrence is skipped: 'Figure 3. (a) Spider mamba...' names the figure, it
    does not cite it. The same test drops a genuine sentence-final citation
    ('...as shown in Figure 3.'), indistinguishable from a caption opening once flattened.
    """
    out: list[CrossRef] = []
    for s in sections:
        text = s.text or ""
        if not text:
            continue
        flat, offsets = flatten(text)
        seen: set[tuple[str, str, int]] = set()
        for kind, pattern in _CROSSREF_PATTERNS:
            for m in pattern.finditer(text):
                if re.match(_CAPTION_DELIM, text[m.end():]):
                    continue                      # the caption itself, not a citation
                left, right = _sentence_bounds(text, m.start(), m.end())
                a = _flat_index(offsets, left)
                b = min(_flat_index(offsets, right), a + MAX_CROSSREF_QUOTE)
                if b <= a:
                    continue
                key = (kind, m.group(1), a)
                if key in seen:
                    continue
                seen.add(key)
                out.append(CrossRef(
                    kind=kind, number=m.group(1), page=s.page_start,
                    section_idx=s.section_idx, span=f"P{s.section_idx}:{a}-{b}",
                    quote=_verbatim_span(text, offsets, a, b)))
                if len(out) >= MAX_CROSSREFS:
                    return out
    return out


def _flat_index(offsets: list[int], original: int) -> int:
    """How many flattened characters lie before original offset `original`.

    `offsets` is strictly increasing, so one bisect is the correct answer for BOTH ends
    of a half-open range — computing the two ends by different rules is what would let a
    span start after it finished on a run of pure whitespace.
    """
    return bisect.bisect_left(offsets, original)


def _verbatim_span(text: str, offsets: list[int], start: int, end: int) -> str:
    """The original text spanned by flattened [start, end), whitespace included.

    Mirrors `locate._verbatim` so a `CrossRef.quote` is exactly what `locate.resolve`
    reads back off the same span.
    """
    if not offsets or start >= end or end > len(offsets):
        return ""
    return text[offsets[start]:offsets[end - 1] + 1]


def _equation_body(lines: list[str], at: int) -> str:
    """The equation body belonging to a lone `(n)` on line `at`, or ''.

    Walks back up to `_EQUATION_LOOKBACK` lines for the nearest short, symbol-bearing
    line. **Returns '' rather than guessing** when the preceding lines are prose — a
    wrong body would be worse than no equation, because `verify_evidence` would then
    certify a quote as `equation_verified` against text that is not the equation.
    """
    for back in range(1, _EQUATION_LOOKBACK + 1):
        j = at - back
        if j < 0:
            return ""
        cand = lines[j]
        if not cand:
            continue                              # a blank line between body and number
        if _EQUATION_NUMBER_ONLY.match(cand):
            return ""                             # two numbers in a row: not our body
        if _EQUATION_BODY.search(cand) and len(cand.split()) <= _EQUATION_MAX_WORDS:
            return cand
        return ""                                 # nearest non-blank line is prose
    return ""


def extract_equations(pages: list[str], max_equations: int = MAX_EQUATIONS) -> list[Equation]:
    """Every display equation the text-line heuristic recognises, in both real-world
    shapes — body-and-number on one line, and a right-margin number on its own. Lossy by
    construction; see `_EQUATION_LINE` and `harness.schema.Equation`."""
    out: list[Equation] = []
    for pno, text in enumerate(pages, start=1):
        lines = [_norm(raw) for raw in text.splitlines()]
        for i, line in enumerate(lines):
            if m := _EQUATION_LINE.match(line):
                number, body = m.group(2), _norm(m.group(1))
            elif n := _EQUATION_NUMBER_ONLY.match(line):
                number, body = n.group(1), _equation_body(lines, i)
            else:
                continue
            if not body:
                continue
            out.append(Equation(equation_idx=len(out), page=pno, number=number, text=body))
            if len(out) >= max_equations:
                return out
    return out


def _clean_rows(raw: Sequence[Sequence[str | None]]) -> list[list[str]]:
    """Normalize a pdfplumber table: drop empty rows, pad ragged ones, cap size."""
    rows = [[_norm(c or "") for c in row][:MAX_COLS] for row in raw[:MAX_ROWS]]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


class _Caption(NamedTuple):
    """One caption line: the printed number, and the line itself VERBATIM.

    Two fields rather than one reconstructed string — this used to be
    `f"Table {n}: {rest}"`, which INSERTS a colon the paper does not print (a real line
    'Table 2 lists the...' became 'Table 2: lists the...'). That string flows into
    `Table.caption`, `QuantFinding.benchmark` and `DiscoveredObject.experiment`, none of
    which is re-verified against the document the way `evidence_quote` is — a quotation
    the paper does not contain is exactly what invariant 1 forbids. Keeping the number
    separately is also the prerequisite for saying "this extractor could not label this
    table" instead of giving it its neighbour's number.
    """

    label: str
    text: str


_NO_CAPTION = _Caption("", "")


def _page_captions(text: str) -> list[_Caption]:
    """Every 'Table N. …' / 'Table N: …' line on a page, in order of appearance.

    The delimiter is required (`_TABLE_CAPTION`), so an in-text 'Table 10, using fully
    fine-tuned models...' is no longer collected as a caption and cannot be attached to a
    table body as its name.
    """
    out: list[_Caption] = []
    for raw in text.splitlines():
        line = _norm(raw)
        if m := _TABLE_CAPTION.match(line):
            out.append(_Caption(label=m.group(1), text=line[:MAX_CAPTION_CHARS]))
    return out


def _word_rows(page, y_tol: float = 3.0) -> list[list[dict]]:
    """Words grouped into visual rows by their `top` coordinate, each row x-sorted."""
    buckets: dict[int, list[dict]] = {}
    for w in page.extract_words():
        buckets.setdefault(int(w["top"] / y_tol), []).append(w)
    return [sorted(buckets[k], key=lambda w: w["x0"]) for k in sorted(buckets)]


def _chunks(row: list[dict], gap: float = 6.0) -> list[tuple[float, str]]:
    """Split one row into (x0, text) cells wherever the inter-word gap exceeds `gap`."""
    out: list[tuple[float, str]] = []
    cur, x0 = [], 0.0
    for i, w in enumerate(row):
        if not cur or w["x0"] - row[i - 1]["x1"] <= gap:
            if not cur:
                x0 = w["x0"]
            cur.append(w["text"])
        else:
            out.append((x0, " ".join(cur)))
            cur, x0 = [w["text"]], w["x0"]
    if cur:
        out.append((x0, " ".join(cur)))
    return out


def _grid(rows: list[list[tuple[float, str]]], tol: float = 10.0) -> list[list[str]]:
    """Snap ragged (x0, text) rows onto shared column anchors.

    Column count cannot be read off any single row — a header may run its words together
    where a data row splits cleanly, and vice versa — so anchors are derived from every
    row's cell starts at once, and each cell lands in the nearest one.
    """
    starts = sorted(x for row in rows for x, _ in row)
    if not starts:
        return []
    anchors = [starts[0]]
    for x in starts[1:]:
        if x - anchors[-1] > tol:
            anchors.append(x)
    anchors = anchors[:MAX_COLS]
    grid: list[list[str]] = []
    for row in rows:
        cells = [""] * len(anchors)
        for x, text in row:
            i = min(range(len(anchors)), key=lambda j: abs(anchors[j] - x))
            cells[i] = f"{cells[i]} {text}".strip()
        grid.append(cells)
    # Right-aligned numbers under a centred header start a few points apart, which opens
    # phantom columns. Neighbours that no row (header included) fills together are one
    # printed column; two columns that each carry a header never merge. Measured on 74
    # corpus PDFs: 125/406 tables re-aligned, no cell lost or concatenated.
    cols = [list(c) for c in zip(*grid)]
    merged = cols[:1]
    for c in cols[1:]:
        if any(a and b for a, b in zip(merged[-1], c)):
            merged.append(c)
        else:
            merged[-1] = [a or b for a, b in zip(merged[-1], c)]
    return [list(r) for r in zip(*merged)]


def _body_runs(gapped: list[bool], excluded: list[bool]) -> list[tuple[int, int]]:
    """Half-open row ranges that look like a table body, found WITHOUT reference to
    captions. `excluded` marks rows that cannot be table data whatever else they are.

    Finding bodies independently is the whole point: anchoring the scan on a caption and
    reading forward assumes the caption precedes its table, which is false for some
    venues, and where it is false the scan walks into the NEXT table's rows and drops the
    first table on the page entirely, shifting every label after it.
    """
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(gapped):
        if gapped[i] and not excluded[i]:
            j = i
            while j < len(gapped) and gapped[j] and not excluded[j]:
                j += 1
            if j - i >= 2:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def _caption_side(run_spans: Sequence[tuple[float, float]],
                  cap_spans: Sequence[tuple[float, float]]) -> str:
    """Whether this page's captions sit 'above' or 'below' the bodies they name.

    Measured in PAGE COORDINATES, not row indices: tables stacked back to back put a
    caption directly after one body and directly before the next, so both readings are
    exactly one row away and the tie is unbreakable by index. Typography breaks it — a
    caption is set tight against the table it belongs to and separated from the next by a
    full inter-float gap. Ties resolve to 'above', the more common convention.
    """
    above = below = 0
    for top, bottom in cap_spans:
        # `None` means no body on that side at all, which is not a distance of zero —
        # scoring it as one made a page whose last caption had nothing beneath it vote
        # for the wrong layout.
        d_below = min((t - bottom for t, _ in run_spans if t >= bottom), default=None)
        d_above = min((top - b for _, b in run_spans if b <= top), default=None)
        if d_below is None and d_above is None:
            continue
        if d_above is None or (d_below is not None and d_below <= d_above):
            above += 1          # this caption sits above the body it names
        else:
            below += 1          # this caption sits below it
    return "below" if below > above else "above"


def _pair(run_spans: Sequence[tuple[float, float]], cap_spans: Sequence[tuple[float, float]],
          captions: Sequence[_Caption]) -> list[_Caption]:
    """One caption per body, nearest-first on the side this page actually uses.

    Nearest-first rather than positional, so a table whose caption sits on the previous
    page leaves a blank label instead of stealing its neighbour's and cascading the error
    down the page. A body with no caption keeps its cells; only the label is unknown, and
    an unlabelled table is far less damaging than a mislabelled one.
    """
    side = _caption_side(run_spans, cap_spans)
    labels = [_NO_CAPTION] * len(run_spans)
    taken: set[int] = set()
    for ci, (top, bottom) in enumerate(cap_spans):
        if ci >= len(captions):
            break
        best, best_d = None, None
        for ri, (r_top, r_bottom) in enumerate(run_spans):
            if ri in taken:
                continue
            d = (r_top - bottom) if side == "above" else (top - r_bottom)
            if d < 0 or d > MAX_CAPTION_GAP:
                continue
            if best_d is None or d < best_d:
                best, best_d = ri, d
        if best is not None:
            labels[best] = captions[ci]
            taken.add(best)
    return labels


def _unruled_tables(page, captions: list[_Caption]) -> list[tuple[_Caption, list[list[str]]]]:
    """Recover LaTeX-style tables that have no ruling lines for pdfplumber to find.

    Column gaps are the signal: a prose line or a wrapped caption has no wide inter-word
    gap, a table row always does. Bodies are located first, on that signal alone; captions
    are attached afterwards on measured proximity, so recovery works whether the venue
    prints captions above its tables or below them.
    """
    rows = _word_rows(page)
    gapped = [any(b["x0"] - a["x1"] > 12 for a, b in zip(r, r[1:])) for r in rows]
    text = [_norm(" ".join(w["text"] for w in r)) for r in rows]
    # Two predicates, two questions. `excluded` keeps any "Table N…" line out of a body
    # (`_TABLE_LINE_SHAPED`); `is_caption` marks only lines that actually NAME a table
    # (`_TABLE_CAPTION`), which is what may be paired with a body.
    excluded = [bool(_TABLE_LINE_SHAPED.match(t)) for t in text]
    is_caption = [bool(_TABLE_CAPTION_ROW.match(t)) for t in text]
    span = [(min(w["top"] for w in r), max(w["bottom"] for w in r)) for r in rows]

    runs = _body_runs(gapped, excluded)
    labels = _pair([(span[s][0], span[e - 1][1]) for s, e in runs],
                   [span[i] for i, c in enumerate(is_caption) if c], captions)

    out: list[tuple[_Caption, list[list[str]]]] = []
    for (start, end), label in zip(runs, labels):
        # An unlabelled run is discarded: requiring a caption is the filter that used to
        # be done implicitly by the caption-first scan, and it is the honest one to keep —
        # a table nobody can name is a table nobody can cite, and admitting it would
        # displace real tables under MAX_TABLES.
        if not label.text:
            continue
        grid = _grid([_chunks(rows[i]) for i in range(start, end)])
        if grid and len(grid[0]) >= 2:
            out.append((label, grid))
    return out


# Two extractors tokenise the same table differently — ruled cells from the line grid,
# geometric ones from word gaps — so a duplicate will not have an identical row, but it
# will have most of the same cell CONTENTS. Below this fraction the candidate is a
# different table that happens to share a column header.
_DUP_CELL_OVERLAP = 0.6


def _already_extracted(cap: _Caption, rows: Sequence[Sequence[str]],
                       already: Sequence[Table]) -> bool:
    """Would admitting this candidate give a reviewer two addresses for one table?

    The check that makes running geometric recovery over a page the ruled path already
    touched safe. That recovery used to be skipped whenever pdfplumber found ANYTHING on
    the page — cost measured: a page printing two ruled tables kept only the first
    body, so the second's real printed result was never citable, and a bare "the paper is
    missing Table N" signal off that is false.

    Two nets. The LABEL is exact: a page prints "Table 1" once, so a second body carrying
    label 1 is one table extracted twice. The CONTENT overlap catches the same
    duplication when the ruled body was paired positionally with a different label than
    the geometric pairing chose.
    """
    if cap.label and any(t.label == cap.label for t in already):
        return True
    cells = {c for row in rows for c in row if c}
    if not cells:
        return True                      # nothing to add; not a table either way
    for t in already:
        seen = {c for row in [t.header, *t.rows] for c in row if c}
        if len(cells & seen) >= _DUP_CELL_OVERLAP * len(cells):
            return True
    return False


def extract_tables(path: str | Path, pages: list[str], max_tables: int = MAX_TABLES) -> list[Table]:
    """Structured tables with (table_idx, row_idx, col_idx) addressing.

    Ruled tables are read by pdfplumber directly, and word-geometry recovery then runs
    over the same page whenever the page's OWN CAPTION LINES outnumber the bodies the
    ruled path accounted for. It used to run only when the ruled path found nothing at
    all, which is why a page printing two tables and yielding one ruled body never had
    its second body looked for. Candidates that would duplicate a body already recovered
    are dropped by `_already_extracted`.

    A table needs >=2 rows and >=2 columns to be worth auditing. Row 0 becomes `header`
    when it contains no digits.

    ponytail: ruled captions are paired positionally — the i-th ruled body on a page gets
    the i-th caption line on that page — and `caption_source` records that, so a reader
    can tell a positional guess from the geometric pairing `_pair` performs. An unpaired
    body keeps its cells and records `label=""`: nameless is safer than mislabelled.
    """
    import pdfplumber

    tables: list[Table] = []

    def add(pno: int, cap: _Caption, source: str,
            raw: Sequence[Sequence[str | None]]) -> None:
        rows = _clean_rows(raw)
        if len(rows) < 2 or len(rows[0]) < 2:
            return
        head, body = ([], rows)
        if not any(re.search(r"\d", c) for c in rows[0]):
            head, body = rows[0], rows[1:]
        if body:
            tables.append(Table(table_idx=len(tables), page=pno, caption=cap.text,
                                label=cap.label,
                                caption_source=source if cap.text else "none",
                                header=head, rows=body))

    with pdfplumber.open(str(path)) as pdf:
        for pno, page in enumerate(pdf.pages[: len(pages)], start=1):
            captions = _page_captions(pages[pno - 1])
            for found in page.extract_tables():
                if len(tables) >= max_tables:
                    return tables
                idx = sum(1 for t in tables if t.page == pno)
                add(pno, captions[idx] if idx < len(captions) else _NO_CAPTION,
                    "ruled_positional", found)
            if len(captions) <= sum(1 for t in tables if t.page == pno):
                continue                 # every caption on this page already has a body
            for cap, grid in _unruled_tables(page, captions):
                if len(tables) >= max_tables:
                    return tables
                if _already_extracted(cap, grid, [t for t in tables if t.page == pno]):
                    continue
                add(pno, cap, "geometric_paired", grid)
    return tables


# A cell counts as a measurement if it leads with a number, optionally signed, with an
# optional ± spread. "12.196 ± 0.207" yes; "ResNet-18" no; "20 tasks" no (trailing word).
_NUMERIC_CELL = re.compile(r"^[-+]?\d+(?:\.\d+)?\s*(?:%|pp)?(?:\s*(?:±|\+/-)\s*\d+(?:\.\d+)?\s*%?)?$")
_SPREAD = re.compile(r"(?:±|\+/-)\s*(\d+(?:\.\d+)?)")
# A prose number worth auditing: a magnitude attached to comparative language. Bare
# counts ("20 tasks", "5 seeds") are not claims; "improves by 4.2%" is.
_COMPARATIVE = re.compile(
    r"\b(improv\w*|gain\w*|increas\w*|decreas\w*|reduc\w*|outperform\w*|better|worse|"
    r"higher|lower|faster|slower|beat\w*|exceed\w*|boost\w*|drop\w*|by)\b", re.I)
_PROSE_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?\s*(?:%|pp|×|x\b)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
MAX_NUMBERS = 120


def is_numeric_cell(text: str) -> bool:
    return bool(_NUMERIC_CELL.match(_norm(text)))


def table_numbers(tables: list[Table]) -> list[QuantFinding]:
    """Every numeric cell, as a QuantFinding addressed back to its exact coordinates.

    Deterministic by construction: the value IS the cell, so no extraction step can
    round it, re-unit it, or invent it. Column 0 is the row label (arm/method), the
    header row the metric names — the near-universal layout for an ML results table.
    """
    out: list[QuantFinding] = []
    for t in tables:
        for r, row in enumerate(t.rows):
            method = row[0] if row else ""
            for c, cell in enumerate(row):
                if c == 0 or not is_numeric_cell(cell):
                    continue
                spread = _SPREAD.search(cell)
                out.append(QuantFinding(
                    benchmark=t.caption[:120], method=method,
                    metric=t.header[c] if c < len(t.header) else f"column {c}",
                    value=cell, seeds_or_variance=spread.group(1) if spread else "",
                    source_quote=cell, page=t.page, table_ref=t.ref(r, c),
                ))
    return out


def prose_numbers(sections: list[Section], limit: int = MAX_NUMBERS) -> list[QuantFinding]:
    """Numeric claims stated in the running text, each with its whole sentence — the
    other half of the contradiction lens's job: a narrative "+4.2%" only becomes a
    finding once it can be set beside the cell it claims to summarize."""
    out: list[QuantFinding] = []
    for s in sections:
        for sentence in _SENTENCE.split(s.text):
            if len(out) >= limit:
                return out
            sentence = _norm(sentence)
            m = _PROSE_NUMBER.search(sentence)
            if m and _COMPARATIVE.search(sentence):
                out.append(QuantFinding(
                    metric=s.title or "prose", value=_norm(m.group()),
                    source_quote=sentence[:400], page=s.page_start,
                ))
    return out


def render_tables(tables: list[Table]) -> str:
    """Tables as text an auditor can cite, every cell tagged with its address — a finding
    that says "T2:r3:c4 reads 76.4 but the abstract claims +4.2%" is checkable; "the
    table disagrees" is not."""
    out: list[str] = []
    for t in tables:
        out.append(f"[T{t.table_idx}] page {t.page} — {t.caption or '(no caption)'}")
        if t.header:
            out.append("  header: " + " | ".join(f"c{i}={h}" for i, h in enumerate(t.header) if h))
        for r, row in enumerate(t.rows):
            cells = " | ".join(f"{t.ref(r, c)}={v}" for c, v in enumerate(row) if v)
            if cells:
                out.append(f"  {cells}")
        out.append("")
    return "\n".join(out)


def render_figures(figures: list[Figure]) -> str:
    """Figure captions as text an auditor can cite by `F<n>` address. Never the plotted
    content — a caption names a figure, it does not report the values in it."""
    return "\n".join(f"[F{fig.figure_idx}] page {fig.page} — {fig.label}: {fig.caption}"
                     for fig in figures)


def render_equations(equations: list[Equation]) -> str:
    """Extracted display equations as text an auditor can cite by `E<n>` address."""
    return "\n".join(
        f"[E{eq.equation_idx}] page {eq.page}{f' ({eq.number})' if eq.number else ''} — {eq.text}"
        for eq in equations)


def render_sections(sections: list[Section], budget_chars: int) -> str:
    """Sections as text, each allotted an equal slice of the context budget.

    Equal slices, not first-N-wins: truncating from the end would silently drop
    Conclusions and Limitations, exactly where the contradiction lens finds the
    concession that undercuts the abstract.
    """
    if not sections:
        return ""
    per = max(400, budget_chars // len(sections))
    out = []
    for s in sections:
        body = s.text[:per]
        cut = " …[truncated]" if len(s.text) > per else ""
        out.append(f"## {s.title or f'(section {s.section_idx})'}  [p{s.page_start}]\n{body}{cut}")
    return "\n\n".join(out)


class SectionPresentation(NamedTuple):
    """How much of the extracted prose a lens was actually shown.

    The ceiling on any recall claim this system makes: `render_sections` divides a
    character budget equally across sections, so a long paper is TRUNCATED before any
    lens reads a word of it — measured presented fraction over the corpus: 0.384, 0.411,
    0.489, 0.589, while the review's scope section said "4 independent lens(es) read the
    paper". Reported, never enforced — computed by the same arithmetic `render_sections`
    uses so the two cannot disagree. `plan_reading`/`plan` below is what removes the cut.
    """

    total_chars: int
    presented_chars: int
    sections: int
    sections_truncated: int

    @property
    def fraction(self) -> float | None:
        """None when there is no prose at all — never 1.0, which would read as 'all of
        it was shown' about a document nothing was extracted from."""
        return (self.presented_chars / self.total_chars) if self.total_chars else None


def section_presentation(sections: list[Section], budget_chars: int) -> SectionPresentation:
    """The measurement of what `render_sections` places in a prompt, without rendering."""
    if not sections:
        return SectionPresentation(0, 0, 0, 0)
    per = max(400, budget_chars // len(sections))
    total = sum(len(s.text) for s in sections)
    presented = sum(min(len(s.text), per) for s in sections)
    return SectionPresentation(total, presented, len(sections),
                               sum(1 for s in sections if len(s.text) > per))


# =========================================================================================
# Reading the whole paper — bounded parts that together cover it, no per-section cut
# =========================================================================================

# When a split section's text is cut, this many characters of the previous part are
# repeated at the start of the next one. A concern whose sentence straddles a cut is
# otherwise unquotable by either part, and an unquotable concern is a dropped one:
# `locate.mint` refuses a quotation it cannot relocate. It cannot create a false address —
# minting searches the PARSED DOCUMENT, not the prompt, so repeated text still occurs
# exactly as often in `doc` as before, and the uniqueness rule is untouched.
SPLIT_OVERLAP_CHARS = 600


class ReadingPart(NamedTuple):
    """One pass of a paper that fits in a single prompt.

    A plan of these covers EVERY character of every extracted section, which
    `render_sections` alone cannot do (it hard-slices each section against one shared
    budget). Over the evaluated corpus that showed readers between 34% and 84% of the
    prose while the scope line still said four lenses read the paper.
    """

    # 1-based: shown to a reader ("part 2 of 3"); `index` would shadow tuple.index.
    number: int
    total: int                 # parts in this plan
    sections: list             # Section objects, whole or sliced
    chars: int                 # characters of section text in this part
    split_sections: int        # sections in this part that are a slice of a larger one
    # (section_idx, start, end) per section in this part, as offsets into that section's
    # ORIGINAL text. A guarantee checked by searching for a slice's text is not checked at
    # all — a section whose text repeats defeats substring search — so offsets make the
    # coverage union arithmetic instead.
    slices: list

    @property
    def label(self) -> str:
        return f"part {self.number} of {self.total}"


def _split_section(s: Section, budget: int) -> list[tuple[Section, int, int]]:
    """One oversized section as a sequence of slices, cut at whitespace where possible.

    Returns each slice with the offsets it was taken from, so a caller can prove the
    slices tile the original.
    """
    text, out, start = s.text, [], 0
    step = max(1, budget - SPLIT_OVERLAP_CHARS)
    while start < len(text):
        end = min(start + budget, len(text))
        if end < len(text):
            # Prefer a whitespace boundary in the last tenth, so a cut lands between
            # words rather than inside one — a word split in half is a quotation neither
            # part can supply.
            window = text.rfind(" ", start + budget - budget // 10, end)
            if window > start:
                end = window
        out.append((s.model_copy(update={"text": text[start:end]}), start, end))
        if end >= len(text):
            break
        start = max(start + step, end - SPLIT_OVERLAP_CHARS)
    return out or [(s, 0, len(text))]


def plan_reading(sections: list[Section], budget_chars: int) -> list[ReadingPart]:
    """Divide a paper into the fewest prompts that show all of it.

    Whole sections are packed greedily, in document order, into parts no larger than the
    budget; a section larger than the budget on its own is sliced with an overlap. The
    guarantee is coverage, asserted rather than intended: every character of every
    section appears in at least one part.

    Why not simply raise the budget: a constant large enough for the longest paper in one
    corpus is not a property of papers, and the failure it produces is silent — the
    prompt overflows somewhere downstream and the reader sees a truncation nobody
    measured. Packing is correct at any length.

    Order is preserved: a reader that meets Limitations before Methods is reading a
    different document, and parts are numbered so it can be told where it is.
    """
    live = [s for s in sections if (s.text or "")]
    if not live:
        return []
    budget = max(400, budget_chars)

    packed: list[list[tuple[Section, int, int]]] = [[]]
    size = 0
    for s in live:
        pieces = ([(s, 0, len(s.text))] if len(s.text) <= budget
                  else _split_section(s, budget))
        for piece, a, b in pieces:
            n = len(piece.text)
            if packed[-1] and size + n > budget:
                packed.append([])
                size = 0
            packed[-1].append((piece, a, b))
            size += n
    packed = [p for p in packed if p]

    whole = {s.section_idx: len(s.text) for s in live}
    total = len(packed)
    return [
        ReadingPart(number=i + 1, total=total,
                    sections=[x for x, _, _ in p],
                    chars=sum(len(x.text) for x, _, _ in p),
                    split_sections=sum(1 for x, _, _ in p
                                       if len(x.text) < whole.get(x.section_idx, 0)),
                    slices=[(x.section_idx, a, b) for x, a, b in p])
        for i, p in enumerate(packed)
    ]


def render_part(part: ReadingPart) -> str:
    """One part's sections as prompt text, with no truncation anywhere.

    Deliberately not `render_sections`: that function fits a budget by cutting, and this
    one shows what the plan already made fit. Reusing it would reintroduce the slice this
    exists to remove. Named-argument sibling below (`render_part_with_anchor`) prepends
    the anchor packet for the case a lens needs the whole-paper comparison too.
    """
    return "\n\n".join(
        f"## {s.title or f'(section {s.section_idx})'}  [p{s.page_start}]\n{s.text}"
        for s in part.sections)


# --------------------------------------------------------------------------------------- #
# Anchors, coverage accounting and within-lens cross-part synthesis
# --------------------------------------------------------------------------------------- #
# **Three things, kept apart on purpose.**
#
# *Anchors* are a small, deterministic packet repeated identically in every part: title,
# abstract, conclusion, section outline. They restore the whole-paper comparison ("the
# abstract says one thing, the conclusion another") that splitting a paper into parts
# would otherwise take away from the contradiction lens. Extracted, never generated, and
# carrying NO model output — a part must not inherit another part's findings, or four
# independent readings become one reading echoed forward.
#
# *Parts* are blind to one another: a lens reading part 2 does not see what it wrote
# about part 1, so a concern is never anchored by an earlier pass's framing.
#
# *Synthesis* is where cross-part reasoning happens, once, per lens, after that lens has
# read every part. Its input is the anchors plus that lens's OWN quotation-grounded
# candidates — not another lens's output, not hidden reasoning. It may merge, connect,
# propose, or withdraw; everything it returns passes the same verification, evidence
# ceiling and grading as a part-local candidate.
#
# What independence does NOT mean: that one scientific reader must forget the first half
# of a paper before reading the second. Independence is BETWEEN lenses (`overclaim` never
# sees `protocol`'s output, and so on) — a reviewer who cannot reason across sections
# cannot review a paper, and claiming whole-paper review while forbidding it would be the
# overclaim this system exists to catch.

# How much of a part's budget the repeated anchor packet may take. Above this the
# anchors are trimmed rather than the paper: a packet that crowds out the text it was
# meant to contextualise has inverted its own purpose.
ANCHOR_BUDGET_FRACTION = 0.25
# Longest abstract or conclusion carried whole. Beyond it the head is kept, because an
# abstract states its claims first and a conclusion restates them first.
ANCHOR_SECTION_CHARS = 6_000


class AnchorPacket(NamedTuple):
    """Deterministic, identical in every part, and free of model output."""

    title: str
    abstract: str
    conclusion: str
    outline: list[tuple[int, str, int]]        # (section_idx, title, chars)
    abstract_idx: int
    conclusion_idx: int

    @property
    def chars(self) -> int:
        return len(self.render())

    def render(self) -> str:
        out = [f"# {self.title}"] if self.title else []
        if self.abstract:
            out.append(f"## Abstract  [section {self.abstract_idx}]\n{self.abstract}")
        if self.conclusion:
            out.append(f"## Conclusion  [section {self.conclusion_idx}]\n{self.conclusion}")
        if self.outline:
            rows = "\n".join(f"- [{i}] {t or '(untitled)'}  ({n:,} chars)"
                             for i, t, n in self.outline)
            out.append("## Section outline of the whole paper\n" + rows)
        return "\n\n".join(out)


def _clip(text: str, limit: int = ANCHOR_SECTION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + " …[anchor clipped]"


def anchors(doc: PaperDoc) -> AnchorPacket:
    """The packet every part carries.

    The abstract/conclusion locators are `harness.decide`'s, deliberately — a second
    spelling of "which section is the abstract" is how the two drift.
    """
    a_idx = abstract_section_idx(doc)
    c_idx = conclusion_section_idx(doc)
    by_idx = {s.section_idx: s for s in doc.sections}
    return AnchorPacket(
        title=doc.title or "",
        abstract=_clip((by_idx[a_idx].text if a_idx in by_idx else "") or ""),
        conclusion=_clip((by_idx[c_idx].text if c_idx in by_idx else "") or ""),
        outline=[(s.section_idx, s.title, len(s.text or "")) for s in doc.sections
                 if (s.text or "") or s.title],
        abstract_idx=a_idx,
        conclusion_idx=c_idx,
    )


class ReadingCoverage(NamedTuple):
    """Four numbers answering four different questions, reported separately.

    Collapsing them is how "the readers saw the paper" gets printed about a run in which
    they saw a third of it. `part_local_fraction` replaces the old presented fraction;
    the anchor figures are a COST, not a coverage claim.
    """

    extracted_prose_chars: int          # what extraction recovered
    part_local_chars: int               # of that, what some part carried (union, no double count)
    anchor_chars: int                   # size of the packet, once
    anchor_repeat_chars: int            # what repeating it across parts costs
    parts: int
    synthesis_required: bool

    @property
    def part_local_fraction(self) -> float | None:
        """None when there is no prose, never 1.0, for the reason SectionPresentation gives."""
        if not self.extracted_prose_chars:
            return None
        return self.part_local_chars / self.extracted_prose_chars

    @property
    def anchor_overhead_fraction(self) -> float | None:
        """Repeated anchor characters as a share of the prose. The cost of the design."""
        if not self.extracted_prose_chars:
            return None
        return self.anchor_repeat_chars / self.extracted_prose_chars


class ReadingPlan(NamedTuple):
    parts: list                          # ReadingPart
    anchor: AnchorPacket
    coverage: ReadingCoverage


def plan(doc: PaperDoc, budget_chars: int) -> ReadingPlan:
    """The whole reading strategy for one paper.

    The anchor packet is charged against the budget before the paper is packed, because
    every part carries it — not charging it is how a part that measured as fitting
    arrives over length.
    """
    anchor = anchors(doc)
    # A paper that fits WHOLE is read whole, and pays nothing for the anchor packet: the
    # packet exists to restore a comparison that splitting takes away, so on a paper
    # nothing was taken away from it is pure cost.
    parts = plan_reading(list(doc.sections), max(400, budget_chars))
    whole = len(parts) <= 1
    if not whole:
        room = max(400, budget_chars - min(anchor.chars,
                                           int(budget_chars * ANCHOR_BUDGET_FRACTION)))
        parts = plan_reading(list(doc.sections), room)

    live = [s for s in doc.sections if (s.text or "")]
    total = sum(len(s.text) for s in live)
    covered = 0
    for s in live:
        spans = sorted((a, b) for p in parts for (i, a, b) in p.slices
                       if i == s.section_idx)
        reach = 0
        for a, b in spans:
            if b > reach:
                covered += b - max(a, reach)
                reach = b
    n = len(parts)
    return ReadingPlan(
        parts=parts, anchor=anchor,
        coverage=ReadingCoverage(
            extracted_prose_chars=total,
            # Zero on a paper read whole, because the packet is not sent: `anchor.chars`
            # is what it WOULD cost, and a cost nobody paid must not appear in an
            # accounting of what this design cost.
            anchor_chars=0 if n <= 1 else anchor.chars,
            part_local_chars=covered,
            anchor_repeat_chars=anchor.chars * max(0, n - 1),
            parts=n,
            synthesis_required=n > 1,
        ))


# What the synthesis pass is asked to look for. Named rather than left to the model,
# because an open-ended "find anything else" invites invention, and every one of these is
# a relationship BETWEEN two spans a part-local reader structurally could not see.
SYNTHESIS_TARGETS = (
    "cross-section contradictions",
    "abstract or conclusion inconsistent with the body",
    "a table disagreeing with the prose that cites it",
    "a method described one way and evaluated another",
    "an appendix result inconsistent with the main text",
    "duplicated concerns that are one concern and should merge",
    "a concern whose significance changes once another section is taken into account",
)


class SynthesisBrief(NamedTuple):
    """The whole input to one lens's cross-part synthesis. Nothing else reaches it.

    Three exclusions make this a synthesis rather than a second opinion: it sees no
    other lens's output (cross-lens independence untouched), no hidden reasoning (only
    candidates that already carry a quotation), and no decision, grade or outcome (so it
    cannot be steered by what the harness has concluded so far).
    """

    paper_id: str
    lens: str
    anchor: AnchorPacket
    candidates: list          # dicts: part, section_idx, page, ref, quote, statement
    parts_read: int

    def render(self) -> str:
        rows = []
        for c in self.candidates:
            where = f"part {c.get('part', '?')} · section {c.get('section_idx', '?')}"
            if c.get("page"):
                where += f" · p{c['page']}"
            if c.get("ref"):
                where += f" · {c['ref']}"
            rows.append(f"- [{where}] {(c.get('statement') or '').strip()}\n"
                        f"  quoted: \"{(c.get('quote') or '').strip()}\"")
        asks = "\n".join(f"  - {t}" for t in SYNTHESIS_TARGETS)
        return (f"{self.anchor.render()}\n\n"
                f"## Your own observations across {self.parts_read} parts of this paper\n"
                + ("\n".join(rows) if rows else "- (none)")
                + f"\n\n## What to look for now\n{asks}\n")


def _field(obj, name, default=None):
    """Read one field off a `Finding` or off the plain dict a persisted part artifact is.

    Both shapes are real: the rest of the pipeline passes `Finding` objects around, but a
    part artifact on disk is JSON, and re-inflating it into a model would mean carrying
    every harness-written field that has not been computed yet.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def synthesis_brief(paper_id: str, lens: str, anchor: AnchorPacket,
                    per_part_findings: dict[int, list]) -> SynthesisBrief:
    """Assemble one lens's own grounded observations across its own parts.

    `per_part_findings` maps a part number to that part's findings. A finding with no
    evidence quotation is dropped here rather than passed on: the synthesis reasons over
    what can be relocated in the paper, and an unquoted observation is exactly the
    fluent assertion this system refuses everywhere else.
    """
    out = []
    for part in sorted(per_part_findings):
        for f in per_part_findings[part] or []:
            quote = (_field(f, "evidence_quote", "") or "").strip()
            if not quote:
                continue
            out.append({
                "part": part,
                "section_idx": _field(f, "section_idx"),
                "page": _field(f, "page"),
                "ref": _field(f, "evidence_ref", "") or "",
                "quote": quote,
                # `statement` is the finding's own prose; `title` is its short form and
                # is the fallback, because a finding with neither says nothing at all.
                "statement": (_field(f, "statement", "") or _field(f, "title", "") or ""),
                "finding_id": _field(f, "finding_id", "") or "",
            })
    return SynthesisBrief(paper_id=paper_id, lens=lens, anchor=anchor,
                          candidates=out, parts_read=len(per_part_findings))


def render_part_with_anchor(part, anchor: AnchorPacket) -> str:
    """One part's prompt body: the anchors, then this part's own sections.

    Named distinctly from `render_part` above — the reference implementation had these
    as `pdf.render_part(part)` and `reading.render_part(part, anchor)` in two separate
    modules, where the module prefix disambiguated them; merged into one namespace here,
    only one name can be `render_part`, so the anchor-carrying wrapper takes this longer
    name. Behavior of both is unchanged.

    The order is deliberate: anchors come first so a reader meets the paper's claims
    before the span it is being asked to examine, the order a referee reads in.
    """
    head = anchor.render()
    body = render_part(part)
    where = (f"\n\n## The span you are reading now — {part.label}\n"
             f"Sections below are the portion of the paper assigned to this pass. The "
             f"abstract, conclusion and outline above describe the whole paper.\n")
    return f"{head}{where}\n{body}" if head else body


# =========================================================================================
if __name__ == "__main__":  # self-check: python -m harness.paper [file.pdf]
    import sys

    # --- fixture-based half: no PDF needed (reading plan + repo-URL discovery) ---------
    doc = PaperDoc(
        paper_id="p", title="A Paper",
        sections=[
            Section(section_idx=0, title="", page_start=1, text="front matter"),
            Section(section_idx=1, title="Abstract", page_start=1, text="we claim X."),
            Section(section_idx=2, title="Method", page_start=2, text="m" * 5000),
            Section(section_idx=3, title="Results", page_start=4, text="r" * 5000),
            Section(section_idx=4, title="Conclusion", page_start=6, text="we showed Y."),
        ])
    a = anchors(doc)
    assert a.abstract == "we claim X." and a.conclusion == "we showed Y." and a.title == "A Paper"
    assert len(a.outline) == 5
    assert "Abstract" in a.render() and "Conclusion" in a.render()

    p = plan(doc, 4000)
    assert p.coverage.parts > 1, "this fixture must need more than one part"
    assert p.coverage.part_local_fraction == 1.0, p.coverage
    assert p.coverage.anchor_repeat_chars == a.chars * (p.coverage.parts - 1)
    assert p.coverage.synthesis_required is True
    rendered = render_part_with_anchor(p.parts[0], a)
    assert "we claim X." in rendered and "we showed Y." in rendered and "part 1 of" in rendered

    one = plan(PaperDoc(paper_id="q", title="T",
                        sections=[Section(section_idx=0, title="Abstract", text="a")]), 70_000)
    assert one.coverage.parts == 1 and one.coverage.synthesis_required is False
    assert one.coverage.anchor_repeat_chars == 0
    assert one.coverage.anchor_chars == 0, "a paper read whole is not sent the packet"
    assert one.coverage.part_local_fraction == 1.0

    empty = plan(PaperDoc(paper_id="e"), 70_000)
    assert empty.coverage.part_local_fraction is None, "no prose is not full coverage"

    print("harness.paper fixture self-check ok")

    # --- PDF-based half: only when a file is given ------------------------------------
    if len(sys.argv) < 2:
        print("(no PDF given; skipping extraction self-check — "
              "usage: python -m harness.paper <file.pdf>)")
        raise SystemExit(0)

    src = Path(sys.argv[1])
    if not src.is_file():
        print(f"no such file: {src}", file=sys.stderr)
        raise SystemExit(2)

    pg = page_texts(src)
    secs, tbls = split_sections(pg), extract_tables(src, pg)
    figs, eqs = extract_figures(pg), extract_equations(pg)
    xrefs, boundary = extract_crossrefs(secs), references_boundary(secs)
    pres = section_presentation(secs, 70_000)
    print(f"{src.name}: {len(pg)} pages, {len(secs)} sections, {len(tbls)} tables, "
         f"{len(figs)} figure caption(s), {len(eqs)} equation(s), "
         f"{len(xrefs)} cross-reference(s) [extraction v{EXTRACTION_VERSION}]")
    print(f"title: {guess_title(pg)!r} (plausible: {title_is_plausible(guess_title(pg))})")
    print(f"back matter begins at section {boundary}; "
          f"prose shown to a lens: {pres.presented_chars}/{pres.total_chars} chars, "
          f"{pres.sections_truncated}/{pres.sections} section(s) truncated")
    assert pg, "no pages extracted"
    assert secs, "no sections extracted"
    assert sum(len(s.text) for s in secs) > 500, "suspiciously little text extracted"

    # A caption must be text the paper actually contains — the property the reconstructed
    # `f"Table {n}: {rest}"` broke, asserted against the real page text rather than a
    # fixture, because the injected colon only showed up on a paper with no colon in it.
    flat_pages = [_norm(p) for p in pg]
    for t in tbls:
        assert not t.caption or any(t.caption in fp for fp in flat_pages), \
            f"T{t.table_idx}'s caption is not printed in this paper: {t.caption!r}"

    # A cross-reference must be re-derivable from its own address, or the address is
    # decoration. `locate.resolve` is the same resolver a finding's evidence goes
    # through, so this is the real check and not a parallel one.
    from .locate import resolve as _resolve
    probe_doc = PaperDoc(paper_id="selfcheck", sections=secs)
    for xr in xrefs[:25]:
        got = _resolve(probe_doc, xr.span)
        assert got.resolution == "resolved", (xr.span, got.resolution, got.detail)
        assert got.quote == xr.quote, (xr.span, got.quote[:60], xr.quote[:60])

    for s in secs[:12]:
        print(f"  §{s.section_idx} p{s.page_start}-{s.page_end} {s.title!r} ({len(s.text)} chars)")
    for t in tbls[:5]:
        print(f"  [T{t.table_idx}] p{t.page} {len(t.rows)}x{len(t.rows[0])} {t.caption[:60]!r}")
    for fig in figs[:5]:
        print(f"  [F{fig.figure_idx}] p{fig.page} {fig.label!r} {fig.caption[:60]!r}")
    for eq in eqs[:5]:
        print(f"  [E{eq.equation_idx}] p{eq.page} ({eq.number}) {eq.text[:60]!r}")
    print("harness.paper PDF self-check ok")
