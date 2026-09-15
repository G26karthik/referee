"""PDF → text + addressable tables. Pure functions: no LLM, no network, no Config.

This is the only module in the harness that opens a PDF. Everything downstream reads
the `PaperDoc` it produces, which is what lets every auditor run with
`allowed_tools=[]` and still see the whole paper.

Two extractors, because one tool does not do both well:
  - PyMuPDF (`fitz`)  → page text, in reading order, cheap and reliable.
  - pdfplumber        → ruled/whitespace table structure with real cell boundaries.

`python -m harness.pdf <file.pdf>` prints a summary — the runnable self-check.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from .artifacts import CrossRef, Equation, Figure, QuantFinding, Section, Table

# Bumped whenever a change RENUMBERS or RE-SCOPES an addressable object — `F<n>`,
# `T<i>:r<r>:c<c>`, `E<n>`, `S<i>`, `P<i>:<a>-<b>`. It is stamped onto every `PaperDoc`
# this module's output is assembled into, so a reference minted against an older parse
# can be recognised as belonging to an older parse instead of silently resolving to a
# different object — which is the same false-attestation failure `_equation_body`
# refuses to make, one layer up.
#
# 2: cross-references stopped being ingested as captions (`_FIGURE_CAPTION` /
#    `_TABLE_CAPTION` now require the delimiter), table captions became verbatim, the
#    geometric table fallback stopped being gated on the ruled path finding nothing, and
#    bibliography lines stopped being read as appendix headings. Each of those changes
#    moves at least one index.
EXTRACTION_VERSION = 2

# ponytail: bounds sized for conference ML papers (8-10pp + appendix). A 400-page
# thesis is truncated, not crashed; raise these if that ever becomes the workload.
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
# Appendices are lettered ("A Appendix", "B.2 Dataset details") as often as numbered,
# and appendix content is where evaluation protocol detail usually hides — so the
# leakage lens loses real evidence if these are not recognised as headings.
_SECNO = r"(?:\d+|[A-Z])(?:\.\d+)*"
_NAMED_HEADING = re.compile(rf"^(?:{_SECNO}\.?\s+)?({_KNOWN_HEADINGS})\b.{{0,40}}$", re.I)
_NUMBERED_HEADING = re.compile(rf"^({_SECNO})\.?\s+([A-Z][^.!?]{{1,68}})$")
# A caption NAMES an object; a cross-reference MENTIONS one. Both begin a line with
# "Figure 4", and in text-extracted output the delimiter after the number is the only
# thing that separates them: a caption prints "Figure 4." or "Figure 4:", a citation
# prints "Figure 4 shows", "Figure 4, this" or "Figure 4a,".
#
# The delimiter used to be OPTIONAL (`[:.—-]?`), and what that cost was a FALSE MACHINE
# ATTESTATION rather than a cosmetic mislabelling. 'Figure 4 shows the visual comparison
# with real-world' — body prose — became a `Figure` object, and
# `stages.audit.verify_evidence` then returned `caption_verified` plus the harness's own
# observation "Figure caption F5 (page 6, 'Figure 4') of the parsed paper contains the
# quoted text verbatim." Reproduced on three separate refs against the shipped
# `projects/cvpr/paper/doc.json`. `_equation_body` already returns '' rather than
# attaching a number to whatever precedes it, for exactly this reason; the figure and
# table paths had no such guard.
#
# Requiring the delimiter classified 63 of 63 caption-shaped lines correctly across the
# three pilot papers, and it can only REMOVE objects — so it cannot manufacture
# provenance. What it also costs is a sentence-final citation ("...as shown in
# Figure 3.") and a subfigure-labelled caption ("Figure 5a: ..."), neither of which is
# distinguishable from the other case in a flattened line. Recovering less is the
# posture this module already takes everywhere else.
_CAPTION_DELIM = r"(?:\s*[:.—-](?=\s|$))"
_TABLE_CAPTION = re.compile(
    rf"^\s*(?:table|tab\.)\s*([IVXLC]+|\d+){_CAPTION_DELIM}\s*(.{{0,200}})", re.I)
# The delimiter-OPTIONAL shape, kept for exactly one job: a line beginning "Table 4" is
# not a row of table data, whether it names a table or merely cites one, so it must not
# be swallowed into a table body. That is a different question from "does this line NAME
# a table?", and collapsing the two into the strict regex cost seven captioned tables on
# APT — in-text reference lines rejoined the body runs and re-cut them, so `_grid` and
# `_pair` saw different bodies. `is_heading`/`_heading_admitted` is split for the same
# reason: one predicate per question.
_TABLE_LINE_SHAPED = re.compile(r"^\s*(?:table|tab\.)\s*(?:[IVXLC]+|\d+)\b", re.I)
# The same strict rule, minus the "delimiter then whitespace" lookahead, for use over
# pdfplumber WORD ROWS rather than pymupdf page lines. On a PDF whose font encoding
# names no space glyph, `extract_words` returns the whole visual line as one token —
# APT page 7 comes back as 'Table2.RoBERTaandT5pruningwithAPT…' — so the lookahead can
# never hold there and the caption row goes unrecognised. That is not a hypothetical:
# it silently cost seven of APT's captioned tables their bodies, because the row-level
# mask and `_page_captions` are zipped BY INDEX and have to select the same lines.
# Whitespace after the delimiter is evidence this coordinate system does not have.
_TABLE_CAPTION_ROW = re.compile(r"^\s*(?:table|tab\.)\s*(?:[IVXLC]+|\d+)\s*[:.—-]", re.I)
# Mirrors `_TABLE_CAPTION` exactly, substituting "figure"/"fig." — same reasoning, same
# shape. `MAX_FIGURES` bounds it the way `MAX_TABLES` bounds table extraction.
_FIGURE_CAPTION = re.compile(
    rf"^\s*(?:figure|fig\.)\s*([IVXLC]+|\d+){_CAPTION_DELIM}\s*(.{{0,300}})", re.I)
# A display equation, text-extracted: a line carrying a relational operator, numbered the
# way LaTeX numbers equations. Deliberately lossy — this is a text-line heuristic, not
# layout geometry; `SOURCE_FIDELITY` in the audit prompt tells the reviewer equation
# extraction is lossy for exactly this reason.
#
# TWO SHAPES, because real PDFs use the second one. An equation number typeset in the
# right margin is a SEPARATE text block from the equation body, so pymupdf's reading order
# emits it on its own line: `_EQUATION_NUMBER_ONLY` matches that, and the body is the
# nearest preceding line carrying a relational operator. Requiring both on one line — which
# is what this did — matched a synthetic "y = mx + b   (7)" fixture and ZERO equations
# across every real paper in the corpus, all of which number their display equations.
_EQUATION_LINE = re.compile(r"^(.{1,220}?[=≤≥∝≈<>].{0,220}?)\s*\((\d{1,3}[a-z]?)\)\s*$")
_EQUATION_NUMBER_ONLY = re.compile(r"^\(\s*(\d{1,3}[a-z]?)\s*\)$")
_EQUATION_BODY = re.compile(r"[=≤≥∝≈<>]")
# How far back to look for the body belonging to a lone number. Small on purpose: an
# equation's own line is normally immediately above its number, and a wide window would
# attach the number to an unrelated sentence several lines up.
_EQUATION_LOOKBACK = 3
# A body line must not be ordinary prose that merely contains a comparison. Prose is long
# and word-dense; a display equation is short and symbol-dense.
_EQUATION_MAX_WORDS = 24
MAX_FIGURES = 40
MAX_EQUATIONS = 60
# A caption line is kept VERBATIM, so it needs its own bound. Truncation keeps a PREFIX
# of the printed line, which is still a substring of the page — a reconstructed string
# is not, and that is the whole point of the field.
MAX_CAPTION_CHARS = 240

# Where the prose CITES a numbered object. Digits only, deliberately: with `re.I` a
# roman-numeral alternative matches the "i" in "figures in the appendix" and mints a
# citation of figure I that nobody wrote. Only the first number of "Figures 3 and 4" is
# recovered; a second pattern for lists would have to decide whether "3-5" is a range or
# a hyphenated identifier, and guessing there is how a citation inventory starts
# reporting citations that are not in the paper.
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
# citation is embedded in, short enough that a runaway span (a section whose sentence
# boundaries were lost to extraction) cannot quote half the paper.
MAX_CROSSREF_QUOTE = 400
_SENTENCE_END = re.compile(r"[.!?]\s")

# A bare known-heading word — `Method`, `Model`, `Training` alone on a line — is what a
# table's first column header looks like once pymupdf has emitted each cell on its own
# line. Measured false headings under the shape rule alone: CVPR 6 of 27 sections, ICLR
# 11 of 25, APT 16 of 59, and the human-facing report prints that count verbatim.
#
# The corroboration is the line that FOLLOWS. A real section heading is followed by the
# first line of a paragraph; a header cell is followed by another cell. Measured over
# the three pilot papers, every false bare heading is followed by 1-4 words and every
# genuine one by 6 or more, so this threshold sits strictly between the two populations
# rather than on the edge of either.
_HEADING_FOLLOWED_BY_WORDS = 5
# Inside the reference range, a section number that is neither a small integer nor a
# letter continuing an appendix sequence is a year or a page number that happened to
# land at the start of a bibliography line. Two digits, because no paper has a hundred
# top-level sections.
_MAX_BARE_SECTION_NUMBER_DIGITS = 2
_REFERENCES_HEADING = re.compile(r"^(?:\d+|[A-Z])?\.?\s*(?:references|bibliography)\b", re.I)
_APPENDIX_LETTER = re.compile(r"^[A-Z]$")

# C0 control characters other than tab and newline. Real prompts carry them today: each
# `projects/iclr/audit/prompts/*.md` holds 3 NUL bytes and 30 other control characters,
# straight out of PDF text extraction and into a reviewer's stdin. Not a live failure on
# this host, but an unhardened boundary whose failure mode on another reader is "the
# command exited without writing anything" — a silent cause, which is the one shape of
# failure this harness is least able to attribute.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").replace("­", "")).strip()


def sanitise_controls(s: str, replacement: str = " ") -> str:
    """Strip C0 control characters, keeping tab and newline.

    A boundary function, not an extraction one: `doc.json` stays byte-faithful to what
    the PDF gave up, and this is applied where the text leaves the harness for another
    process's stdin. See `_CONTROL_CHARS` for the failure it prevents. `replacement` is
    a space rather than '' so two words a NUL happened to sit between do not become one
    word that appears nowhere in the paper.
    """
    return _CONTROL_CHARS.sub(replacement, s or "")


# Publication metadata that a first-page line-scan reaches before the title on a journal
# reprint. A closed list of METADATA words, never of venues or paper titles: three of
# seven corpus papers ended up with a title like 'Accepted: 6 November 2024 / Published
# online: 21 November 2024', which the reviewer report then printed as the paper's name.
_TITLE_METADATA = (
    "received", "accepted", "revised", "published", "submitted", "in press",
    "doi", "https://doi", "volume", "vol.", "issue", "pages", "pp.", "copyright",
    "proceedings of", "preprint", "arxiv", "under review", "keywords", "abstract",
    "correspondence", "editor",
)
# An author-affiliation marker: the asterisk-or-dagger-then-digit that a two-column
# venue prints after each name ("Jayesh Singla * 1 Ananye Agarwal * 1 Deepak Pathak 1").
# This is a rule about a TYPESETTING CONVENTION, not about names: a rule that tried to
# recognise "a comma-separated list of people" would reject real subtitles, and printing
# one wrong title is a smaller cost than dropping a right one.
_AFFILIATION_MARKER = re.compile(r"[*†‡§]\s*\d")


def title_is_plausible(s: str) -> bool:
    """Could this string be a paper's title?

    Pure and report-only: nothing here changes extraction, and the caller decides what
    to do with a False (`stages/report.py` falls back to the paper id). Four real
    failures are recognised, all measured on the shipped corpus: a date line
    ('Accepted: 6 November 2024 / Published online: 21 November 2024'), a venue banner
    ('Proceedings of the 64th Annual Meeting…'), a page range, and an author byline
    carrying affiliation markers ('Jayesh Singla * 1 Ananye Agarwal * 1…').

    What it deliberately does NOT recognise is a byline with no markers: a
    comma-separated list of names is shaped exactly like a subtitle, and a rule that
    guessed would start rejecting real titles — a larger cost than printing one bad one.
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
    # A title carries prose. A date line, a page range and a DOI are mostly digits and
    # punctuation, and this is the only test that catches all three without naming any.
    return letters > digits * 2 and letters >= 8


def is_heading(line: str) -> bool:
    """Does this line look like a section heading?

    ponytail: heuristic tuned for arXiv-style ML PDFs (numbered or conventionally
    named headings). A paper using unnumbered small-caps headings degrades to one
    large section — the auditors still receive the full text, only the section
    LABELS are lost, so this is a labelling ceiling and not a correctness one.
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
    """Lines that repeat verbatim across multiple pages: running headers, footers, venue
    banners ("34th Conference on ... 2024", "Proceedings of ..."). These describe the
    VENUE, not the paper, and every paper from that venue prints the same one — so a
    title guess that lands on a banner line makes two different papers look identical.
    Detected structurally (repetition), never by matching specific venue names, because
    a fixed phrase list would be paper/venue-specific and would miss the next venue.
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
        if len(line) < 8 or line.lower().startswith(("arxiv:", "preprint", "under review")):
            continue
        if line in boilerplate:
            continue
        if _NAMED_HEADING.match(line):  # hit "Abstract" before finding a title
            break
        return line[:300]
    return ""


def _is_bare_known_heading(line: str) -> bool:
    """A known heading WORD with no section number in front of it.

    The form that a table's first column header is indistinguishable from on shape
    alone. A numbered heading ("3. Method") is corroborated by its own number and is
    never subject to the following-line test.
    """
    if not _NAMED_HEADING.match(line):
        return False
    return not re.match(rf"^(?:{_SECNO})\.?\s", line)


def _heading_admitted(line: str, next_line: str, *, in_references: bool,
                      appendix_letter: str) -> bool:
    """Shape said "heading"; may the DOCUMENT overrule it?

    `is_heading` answers a question about one line and is deliberately left alone — it
    is a shape predicate and its callers, including `guess_title`, ask nothing else of
    it. The two overrules below need context a single line does not have, which is why
    they live here and not there.

    1. A bare known-heading word needs the next line to look like prose
       (`_HEADING_FOLLOWED_BY_WORDS`).
    2. Inside the reference range, a single capital at line start is an author's
       initial far more often than an appendix letter — measured, APT's `References`
       section held 694 of ~17,200 characters and 16.5 KB of its bibliography became
       five sections, four of them shaped exactly like appendix headings ('M. Analysis
       of dawnbench, a time-to-accuracy machine', 10,461 chars). The discriminator is
       SEQUENCE: appendix letters run A, B, C… from A, and 'M' before any 'A' has been
       seen is not an appendix. The alternative rule — "the line also contains a year,
       or `pp.`, or `In Proceedings`" — was tried against the real lines and fails on
       all four of them, because `_NUMBERED_HEADING` only matches a line short enough
       to have left the year behind on the next one.

    A rejected heading is not a dropped line: its text joins the preceding section, so
    the failure direction is a bibliography that stays whole under one label.
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

    A boundary INDEX, not a per-section `is_appendix` flag. The flag would have to be
    written onto every section after this one, and it would be wrong on exactly the
    sections a mis-parsed bibliography produces — so it would assert something about the
    paper that is a fact about the parse.
    """
    for s in sections:
        if _REFERENCES_HEADING.match(_norm(s.title)):
            return s.section_idx
    return -1


def split_sections(pages: list[str]) -> list[Section]:
    """Walk every line, starting a new Section at each ADMITTED heading.

    Text before the first heading becomes the front-matter section, so no content is
    ever dropped on the floor — and a heading the document overrules (see
    `_heading_admitted`) contributes its own line to the section it interrupts rather
    than opening a new one.

    Lines are flattened across page boundaries first, because the corroboration test
    looks at the FOLLOWING line and a heading set at the foot of a page has its
    paragraph on the next one. Reading page by page made every such heading fail.
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
    content — this harness has no way to read that — only its caption text, which is
    all `_FIGURE_CAPTION` can see. See `harness.artifacts.Figure` for why that ceiling
    matters downstream."""
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

    Takes SECTIONS AND NOTHING ELSE. That is the guarantee, at the signature: this
    function cannot see `doc.figures`, `doc.tables` or `doc.equations`, so it cannot
    compare a citation against a recovered label set and cannot report that anything is
    missing. Over the shipped corpus a naive "cited but not recovered" check produced
    twelve claims that a table or an equation was absent from a paper, and all twelve of
    those objects are printed in the paper and absent only from what extraction
    recovered. Whoever wants that comparison has to build it somewhere that can say
    whose property the answer is — see `harness.artifacts.CrossRef`.

    Each reference carries a `P<i>:<a>-<b>` span in the same flattened coordinates
    `harness.claims` mints, so the citing sentence is itself checkable rather than
    taken on this function's word. `page` is the section's first page, because
    `Section` records no intra-section page offsets — an honest ceiling, not an
    estimate.

    A caption occurrence is skipped: 'Figure 3. (a) Spider mamba…' names the figure, it
    does not cite it, and counting a caption as a citation would make an orphan-object
    check conclude the prose refers to a figure when only the figure's own caption did.
    The same test drops a genuine sentence-final citation ('…as shown in Figure 3.'),
    which is indistinguishable from a caption opening in flattened text.
    """
    # Imported inside the function so the extractor and the addressing layer stay
    # separable: `claims` reads a parsed document, `pdf` produces one, and a module-level
    # edge would make an import cycle the next person's problem rather than mine.
    from .claims import flatten

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

    `offsets` (flattened index -> original index) is strictly increasing, so this is one
    bisect and it is the correct answer for BOTH ends of a half-open range: the count of
    kept characters before `left` is the span's start, and the count before `right` is
    its exclusive end. Computing the two ends by different rules is what would let a
    span start after it finished on a run of pure whitespace.
    """
    import bisect

    return bisect.bisect_left(offsets, original)


def _verbatim_span(text: str, offsets: list[int], start: int, end: int) -> str:
    """The original text spanned by flattened [start, end), whitespace included.

    Mirrors `claims._verbatim` so a `CrossRef.quote` is exactly what `claims.resolve`
    reads back off the same span. A quote assembled any other way would agree with the
    address only by luck.
    """
    if not offsets or start >= end or end > len(offsets):
        return ""
    return text[offsets[start]:offsets[end - 1] + 1]


def _equation_body(lines: list[str], at: int) -> str:
    """The equation body belonging to a lone `(n)` on line `at`, or ''.

    Walks back up to `_EQUATION_LOOKBACK` lines for the nearest short, symbol-bearing
    line. Returns '' rather than guessing when the preceding lines are prose — a wrong
    body would be worse than no equation, because `verify_evidence` would then certify a
    quote as `equation_verified` against text that is not the equation.
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
    shapes — body-and-number on one line, and a right-margin number that text extraction
    emitted on its own. Lossy by construction: see `_EQUATION_LINE` and
    `harness.artifacts.Equation`."""
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

    Two fields rather than one reconstructed string. This used to be
    `f"Table {n}: {rest}"`, which INSERTS a colon the paper does not print: the real
    line 'Table 2 lists the quantitative comparison between Weath-' came back as
    'Table 2: lists the quantitative…'. That string becomes `Table.caption`, and
    through it `QuantFinding.benchmark` and `DiscoveredObject.experiment`, both of
    which reach a human in `targets.json` and in the ledger — and unlike an
    `evidence_quote` it is never re-verified against the document, so nothing would
    have caught it. A quotation the paper does not contain is what invariant 1 exists
    to prevent.

    Keeping the number separately is also the prerequisite for saying "this extractor
    could not label this table" instead of giving it its neighbour's number.
    """

    label: str
    text: str


_NO_CAPTION = _Caption("", "")


def _page_captions(text: str) -> list[_Caption]:
    """Every 'Table N. …' / 'Table N: …' line on a page, in order of appearance.

    The delimiter is required (`_TABLE_CAPTION`), so an in-text 'Table 10, using fully
    fine-tuned models as the teacher…' is no longer collected as a caption and can no
    longer be attached to a table body as its name.
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

    Column count cannot be read off any single row — a header may run its words
    together where a data row splits cleanly, and vice versa. So anchors are derived
    from every row's cell starts at once, and each cell then lands in the nearest one.
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
    return grid


def _body_runs(gapped: list[bool], excluded: list[bool]) -> list[tuple[int, int]]:
    """Half-open row ranges that look like a table body, found WITHOUT reference to captions.

    `excluded` marks rows that cannot be table data whatever else they are — see
    `_TABLE_LINE_SHAPED`.

    Finding bodies independently is the whole point. Anchoring the scan on a caption and
    reading forward assumes the caption precedes its table, which is true for some venues
    and false for others; where it is false the scan walks into the NEXT table's rows and
    drops the first table on the page entirely, shifting every label after it.
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

    Measured in PAGE COORDINATES, not row indices, because row indices cannot answer the
    question. Tables stacked back to back put a caption directly after one body and
    directly before the next, so both readings are exactly one row away and the tie is
    unbreakable. Typography breaks it: a caption is set tight against the table it belongs
    to and separated from the next by a full inter-float gap. On the page that exposed this
    bug the winning margin was 14.5pt against 90pt.

    Ties still resolve to 'above', the more common convention, so a degenerate page
    behaves as it did before.
    """
    above = below = 0
    for top, bottom in cap_spans:
        # Distance down to the nearest body below, and up to the nearest body above.
        # `None` means there is no body on that side at all, which is not a distance of
        # zero — scoring it as one made a page whose last caption had nothing beneath it
        # vote for the wrong layout.
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
    down the page. A body that ends up with no caption keeps its cells; only the label is
    unknown, and an unlabelled table is far less damaging than a mislabelled one.
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

    Column gaps are the signal: a prose line or a wrapped caption has no wide
    inter-word gap, a table row always does. Bodies are located first, on that signal
    alone; captions are attached afterwards on measured proximity, so the recovery works
    whether the venue prints captions above its tables or below them.
    """
    rows = _word_rows(page)
    gapped = [any(b["x0"] - a["x1"] > 12 for a, b in zip(r, r[1:])) for r in rows]
    text = [_norm(" ".join(w["text"] for w in r)) for r in rows]
    # Two predicates, two questions. `excluded` keeps any "Table N…" line out of a body
    # (`_TABLE_LINE_SHAPED`); `is_caption` marks only the lines that actually NAME a
    # table (`_TABLE_CAPTION`), which is what may be paired with a body.
    excluded = [bool(_TABLE_LINE_SHAPED.match(t)) for t in text]
    is_caption = [bool(_TABLE_CAPTION_ROW.match(t)) for t in text]
    span = [(min(w["top"] for w in r), max(w["bottom"] for w in r)) for r in rows]

    runs = _body_runs(gapped, excluded)
    labels = _pair([(span[s][0], span[e - 1][1]) for s, e in runs],
                   [span[i] for i, c in enumerate(is_caption) if c], captions)

    out: list[tuple[_Caption, list[list[str]]]] = []
    for (start, end), label in zip(runs, labels):
        # An unlabelled run is discarded. Detecting bodies without reference to captions
        # is what fixes the ordering bug, but it also admits any run of wide-gapped prose
        # - equation blocks, figure legends, two-column body text - as a candidate table.
        # Requiring a caption is the filter that was previously doing that work implicitly,
        # and it is the honest one to keep: a table nobody can name is a table nobody can
        # cite, and admitting it would displace real tables under MAX_TABLES.
        if not label.text:
            continue
        grid = _grid([_chunks(rows[i]) for i in range(start, end)])
        if grid and len(grid[0]) >= 2:
            out.append((label, grid))
    return out


# Two extractors tokenise the same table differently — ruled cells come from the line
# grid, geometric ones from word gaps — so a duplicate will not have an identical row.
# What it will have is most of the same cell CONTENTS. Below this fraction the candidate
# is a different table that happens to share a column header.
_DUP_CELL_OVERLAP = 0.6


def _already_extracted(cap: _Caption, rows: Sequence[Sequence[str]],
                       already: Sequence[Table]) -> bool:
    """Would admitting this candidate give a reviewer two addresses for one table?

    The check that makes running the geometric recovery over a page the ruled path
    already touched safe. That recovery used to be skipped entirely whenever pdfplumber
    found anything at all on the page, and the cost was measured: CVPR page 6 prints
    Tables 1 and 2, pdfplumber keeps one ruled body, and Table 2's body was therefore
    never looked for. ICLR's Tables 12 and 13 have recovered caption lines on page 22
    and no bodies at all. Those are real printed results a reviewer cannot cite, and
    they are also what makes a "the paper is missing Table 12" signal false.

    Two nets. The LABEL is the exact one: a page prints "Table 1" once, so a second body
    carrying label 1 is one table extracted twice, and two `T<i>:r:c` addresses for one
    printed table double-count its numbers and let a citation name either. The CONTENT
    overlap catches the same duplication when the ruled body was paired positionally
    with a different label than the geometric pairing chose.
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
    ruled path accounted for — the page's statement of how many tables it prints,
    against how many were found. It used to run only when the ruled path found nothing
    at all, which is why a page printing two tables and yielding one ruled body never
    had its second body looked for. Candidates that would duplicate a body already
    recovered are dropped by `_already_extracted`.

    A table needs >=2 rows and >=2 columns to be worth auditing; anything smaller is
    a layout artifact, not data. Row 0 becomes `header` when it contains no digits.

    ponytail: ruled captions are paired positionally — the i-th ruled body on a page
    gets the i-th caption line on that page — and `caption_source` records that, so a
    reader can tell a positional guess from the geometric pairing `_pair` performs.
    An unpaired body keeps its cells and records `label=""`: a table nobody can name is
    still citable by address, and a MISlabelled one is not.
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

    Deterministic by construction: the value IS the cell, so there is no extraction
    step that could round it, re-unit it, or invent it. Column 0 is treated as the row
    label (the arm/method) and the header row as the metric names — the near-universal
    layout for an ML results table.
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
    """Numeric claims stated in the running text, each with its whole sentence.

    These are the other half of the contradiction lens's job: a narrative "+4.2%" only
    becomes a finding when it can be set beside the cell it claims to summarize.
    """
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
    """Tables as text an auditor can cite, every cell tagged with its address.

    Addresses are the whole point: a finding that says "T2:r3:c4 reads 76.4 but the
    abstract claims +4.2%" is checkable; "the table disagrees" is not.
    """
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
    Conclusions and Limitations, which is exactly where the contradiction lens finds
    the concession that undercuts the abstract.
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

    The ceiling on any recall claim this system makes, and unmeasured until this
    existed: `render_sections` divides a character budget equally across sections, so a
    long paper is TRUNCATED before any lens reads a word of it. Measured presented
    fraction over the corpus: 0.384, 0.411, 0.489, 0.589 — while the review's scope
    section said "4 independent lens(es) read the paper".

    Reported, never enforced: nothing here changes what is rendered. It is computed by
    the same arithmetic `render_sections` uses so the two cannot disagree.
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


if __name__ == "__main__":  # self-check: python -m harness.pdf <file.pdf>
    import sys

    # The only self-check in this harness that needs an argument, so it is also the only
    # one that can be run the way all the others are run and crash. It used to raise a
    # bare IndexError, which reads like a defect in the extractor rather than like a
    # missing argument — an unhelpful first impression from the module a new reader is
    # most likely to try, because "does it parse my PDF" is the first question.
    if len(sys.argv) < 2:
        print(f"usage: python -m harness.pdf <file.pdf>\n"
              f"  parses one PDF and prints its sections, tables, figures and equations.\n"
              f"  Unlike every other `python -m harness.<module>` self-check, this one "
              f"needs a file.", file=sys.stderr)
        raise SystemExit(2)

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
    # A caption must be text the paper actually contains. This is the property the
    # reconstructed `f"Table {n}: {rest}"` broke, and it is asserted here against the
    # real page text rather than against a fixture, because the injected colon only
    # showed up on a paper whose caption line had no colon in it.
    flat_pages = [_norm(p) for p in pg]
    for t in tbls:
        assert not t.caption or any(t.caption in fp for fp in flat_pages), \
            f"T{t.table_idx}'s caption is not printed in this paper: {t.caption!r}"
    # A cross-reference must be re-derivable from its own address, or the address is
    # decoration. `claims.resolve` is the same resolver a finding's evidence goes
    # through, so this is the real check and not a parallel one.
    from .artifacts import PaperDoc as _PaperDoc
    from .claims import resolve as _resolve
    probe_doc = _PaperDoc(paper_id="selfcheck", sections=secs)
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
