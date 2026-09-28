"""PDF -> `PaperDoc`, and the bounded-part reading plan that traverses it. Pure
functions: no LLM, no network, no `Config`. This is the only module in the harness that
opens a PDF (PyMuPDF for page text, pdfplumber for ruled table structure).

Repository URL discovery (`find_repo_urls`/`official_repo_url`) stays in `harness/repo.py`
alongside acquire/pin/verify_commit, which is the only caller. `abstract_section_idx`/
`conclusion_section_idx` are `harness.decide`'s; `flatten`/`resolve` are `harness.locate`'s.

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

# Bumped whenever a change RENUMBERS or RE-SCOPES an addressable object -- `F<n>`,
# `T<i>:r<r>:c<c>`, `E<n>`, `S<i>`, `P<i>:<a>-<b>`. Stamped onto every `PaperDoc` this
# module produces, so a reference minted against an older parse is recognised as stale
# instead of silently resolving to a different object. A cached `doc.json` is never
# re-parsed under a bumped version; see `stages/ingest.py`.
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
# Appendices are lettered ("A Appendix", "B.2 Dataset details") as often as numbered.
_SECNO = r"(?:\d+|[A-Z])(?:\.\d+)*"
_NAMED_HEADING = re.compile(rf"^(?:{_SECNO}\.?\s+)?({_KNOWN_HEADINGS})\b.{{0,40}}$", re.I)
_NUMBERED_HEADING = re.compile(rf"^({_SECNO})\.?\s+([A-Z][^.!?]{{1,68}})$")

# A caption NAMES an object; a cross-reference MENTIONS one. Both begin a line with
# "Figure 4"; the delimiter after the number is what separates them in flattened text: a
# caption prints "Figure 4." or "Figure 4:", a citation prints "Figure 4 shows". Requiring
# the delimiter can only REMOVE objects, never manufacture provenance; the cost is a
# sentence-final citation ("...as shown in Figure 3.") or a subfigure caption
# ("Figure 5a: ..."), neither distinguishable from the other case in flattened text.
_CAPTION_DELIM = r"(?:\s*[:.—-](?=\s|$))"
_TABLE_CAPTION = re.compile(
    rf"^\s*(?:table|tab\.)\s*([IVXLC]+|\d+){_CAPTION_DELIM}\s*(.{{0,200}})", re.I)
# Delimiter-optional: a line beginning "Table 4" is not table-body data whether it names
# or merely cites a table, so it must not be swallowed into a table body.
_TABLE_LINE_SHAPED = re.compile(r"^\s*(?:table|tab\.)\s*(?:[IVXLC]+|\d+)\b", re.I)
# Same strict rule for pdfplumber WORD ROWS, minus the whitespace lookahead: a PDF with no
# space glyph returns a whole visual line as one token, so the lookahead can never hold.
_TABLE_CAPTION_ROW = re.compile(r"^\s*(?:table|tab\.)\s*(?:[IVXLC]+|\d+)\s*[:.—-]", re.I)
# Mirrors `_TABLE_CAPTION`, substituting "figure"/"fig." -- same reasoning, same shape.
_FIGURE_CAPTION = re.compile(
    rf"^\s*(?:figure|fig\.)\s*([IVXLC]+|\d+){_CAPTION_DELIM}\s*(.{{0,300}})", re.I)

# A display equation, text-extracted: a line carrying a relational operator, numbered the
# way LaTeX numbers equations. Deliberately lossy -- a text-line heuristic, not layout
# geometry; `SOURCE_FIDELITY` in the audit prompt says so.
#
# TWO SHAPES, because real PDFs use both: a margin-typeset number is a separate text
# block on its own line (`_EQUATION_NUMBER_ONLY`), whose body is the nearest preceding
# line carrying a relational operator.
_EQUATION_LINE = re.compile(r"^(.{1,220}?[=≤≥∝≈<>].{0,220}?)\s*\((\d{1,3}[a-z]?)\)\s*$")
_EQUATION_NUMBER_ONLY = re.compile(r"^\(\s*(\d{1,3}[a-z]?)\s*\)$")
_EQUATION_BODY = re.compile(r"[=≤≥∝≈<>]")
_EQUATION_LOOKBACK = 3         # lines to search back for a lone number's body
# A body line must not be ordinary prose that merely contains a comparison: prose is long
# and word-dense, a display equation is short and symbol-dense.
_EQUATION_MAX_WORDS = 24
MAX_FIGURES = 40
MAX_EQUATIONS = 60
# A caption line is kept VERBATIM. Truncation keeps a PREFIX of the printed line, still a
# substring of the page -- a reconstructed string is not.
MAX_CAPTION_CHARS = 240

# Where the prose CITES a numbered object. Digits only: with `re.I` a roman-numeral
# alternative matches the "i" in "figures in the appendix" and mints a citation nobody
# wrote. Only the first number of "Figures 3 and 4" is recovered.
_CROSSREF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("figure", re.compile(r"\b(?:figures?|figs?\.)\s*(\d+)", re.I)),
    ("table", re.compile(r"\b(?:tables?|tabs?\.)\s*(\d+)", re.I)),
    ("equation", re.compile(r"\b(?:equations?|eqs?\.|eqn\.?)\s*\(?(\d+)", re.I)),
    ("section", re.compile(r"\b(?:sections?|secs?\.|§)\s*(\d+(?:\.\d+)*)", re.I)),
    # Not `re.I` on the letter: a case-insensitive group would read "appendix and" as a
    # citation of appendix A.
    ("appendix", re.compile(r"(?i:appendix)\s*([A-Z](?:\.\d+)*)")),
)
MAX_CROSSREFS = 400
# The citing sentence, in flattened characters: long enough to carry the claim, short
# enough that a runaway span cannot quote half the paper.
MAX_CROSSREF_QUOTE = 400
_SENTENCE_END = re.compile(r"[.!?]\s")

# A bare known-heading word -- `Method`, `Model` alone on a line -- is what a table's
# first column header looks like once pymupdf emits each cell on its own line. The
# corroboration is the line that FOLLOWS: a real heading precedes a paragraph (>=6 words);
# a header cell precedes another cell (1-4 words).
_HEADING_FOLLOWED_BY_WORDS = 5
# Inside the reference range, a section number that is neither a small integer nor an
# appendix letter is a year/page number at the start of a bibliography line.
_MAX_BARE_SECTION_NUMBER_DIGITS = 2
_REFERENCES_HEADING = re.compile(r"^(?:\d+|[A-Z])?\.?\s*(?:references|bibliography)\b", re.I)
_APPENDIX_LETTER = re.compile(r"^[A-Z]$")

# C0 control characters other than tab and newline -- real prompts carry them (straight
# out of PDF text extraction) and must be stripped at any boundary leaving the harness.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").replace("­", "")).strip()


def sanitise_controls(s: str, replacement: str = " ") -> str:
    """Strip C0 control characters, keeping tab and newline. A boundary function: `doc.json`
    stays byte-faithful; this applies only where text leaves the harness. `replacement` is
    a space, not '', so two words a NUL sat between do not become one word."""
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
    """Could this string be a paper's title? Rejects a date line, a venue banner, a page
    range, and an author byline carrying affiliation markers. Deliberately does NOT
    recognise a byline with no markers -- a comma-separated name list is shaped like a
    subtitle, and guessing would start rejecting real titles."""
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
    # A title carries prose; a date line, a page range and a DOI are mostly digits/punctuation.
    return letters > digits * 2 and letters >= 8


def is_heading(line: str) -> bool:
    """Does this line look like a section heading? Tuned for arXiv-style ML PDFs: a paper
    using unnumbered small-caps headings degrades to one large section."""
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
    Detected structurally, never by a venue name list."""
    if len(pages) < 2:
        return frozenset()
    counts: dict[str, int] = {}
    for p in pages:
        for line in {_norm(raw) for raw in p.splitlines() if _norm(raw)}:
            counts[line] = counts.get(line, 0) + 1
    return frozenset(line for line, n in counts.items() if n >= 2)


def title_from_layout(path: str | Path) -> str:
    """The title as typeset: the largest horizontal text on page 1, every line of it (a
    title that wraps is still one title). '' when the layout gives no plausible answer,
    so the caller falls back to `guess_title`. A rotated margin stamp is not horizontal."""
    try:
        import pymupdf
        with pymupdf.open(str(path)) as d:
            blocks = d[0].get_text("dict")["blocks"] if d.page_count else []
    except Exception:
        return ""
    spans = []
    for block in blocks:
        for line in block.get("lines", []):
            if tuple(round(x) for x in line.get("dir", (1, 0))) != (1, 0):
                continue
            for s in line.get("spans", []):
                text = _norm(s.get("text", ""))
                if len(text) >= 2 and not text.lower().startswith("arxiv:"):
                    spans.append((s.get("size", 0.0), s["bbox"][1], s["bbox"][0], text))
    if not spans:
        return ""
    top = max(size for size, *_ in spans)
    title = _norm(" ".join(t for size, _y, _x, t in sorted(spans, key=lambda s: (s[1], s[2]))
                           if abs(size - top) < 0.6))
    return title if title_is_plausible(title) and len(title) <= 300 else ""


def guess_title(pages: list[str]) -> str:
    """First substantial line of page 1 that is not a header/arXiv stamp/venue banner."""
    boilerplate = _running_boilerplate(pages)
    for raw in (pages[0] if pages else "").splitlines()[:25]:
        line = _norm(raw)
        if len(line) < 8 or "©" in line or line.lower().startswith(
                ("arxiv:", "preprint", "under review", "proceedings of")):
            continue
        # ICML-style templates repeat the TITLE as the running header, so a repeated line
        # is skipped only when it is not title-shaped (a banner, a `Journal | Vol |`
        # footer, or anything carrying a year, as venue running headers do).
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
    """Shape said "heading"; may the DOCUMENT overrule it? (1) A bare known-heading word
    needs the next line to look like prose. (2) Inside the reference range, a single
    capital at line start is an author's initial far more often than an appendix letter --
    the discriminator is SEQUENCE (appendix letters run A, B, C... from A). A rejected
    heading's text joins the preceding section rather than being dropped."""
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
    """The index of the References/Bibliography heading, or -1. A boundary INDEX, not a
    per-section `is_appendix` flag -- the flag would be wrong on exactly the sections a
    mis-parsed bibliography produces."""
    for s in sections:
        if _REFERENCES_HEADING.match(_norm(s.title)):
            return s.section_idx
    return -1


def split_sections(pages: list[str]) -> list[Section]:
    """Walk every line, starting a new Section at each ADMITTED heading. Text before the
    first heading becomes the front-matter section; a heading the document overrules
    (`_heading_admitted`) joins the section it interrupts rather than opening a new one.
    Lines are flattened across page boundaries first, since the corroboration test looks
    at the FOLLOWING line and a heading at the foot of a page has its paragraph on the next."""
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
    """Every 'Figure N: ...' caption line, in reading order. Only the caption text -- a
    best-effort PNG crop is a separate, advisory-only step, see `extract_figure_images`."""
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


def _page_object_rects(page) -> list:
    """Every image placement and vector-drawing cluster on this page, as Rects — the raw
    material a figure's plotted region is assembled from."""
    rects = []
    for xref, *_rest in page.get_images(full=True):
        rects.extend(page.get_image_rects(xref))
    try:
        rects.extend(page.cluster_drawings())
    except Exception:
        for d in page.get_drawings():
            r = d.get("rect")
            if r:
                rects.append(r)
    return rects


def _caption_rect(page, fig: Figure):
    """Where THIS figure's caption sits on its page, or None if it cannot be found."""
    hits = page.search_for((fig.caption or "")[:40].strip()) if fig.caption else []
    if not hits and fig.label:
        hits = page.search_for(fig.label)
    return hits[0] if hits else None


_ROW_TOL = 4.0  # points of caption-baseline slop still counted as "the same row"


def _rows(caps: list) -> list[list]:
    """Group (fig, cap_rect) pairs into visual ROWS, top to bottom -- a multi-panel grid
    puts two or more captions on the same text line, and treating that line as ONE row
    keeps a same-row sibling from stealing the NEXT row's plot."""
    ordered = sorted(caps, key=lambda t: t[1].y0 if t[1] is not None else 1e9)
    rows: list[list] = []
    for item in ordered:
        cap = item[1]
        prev = rows[-1][-1][1] if rows else None
        if cap is not None and prev is not None and abs(cap.y0 - prev.y0) <= _ROW_TOL:
            rows[-1].append(item)
        else:
            rows.append([item])
    return rows


def _in_band(rects: list, top: float, bottom: float) -> list:
    return [r for r in rects if not r.is_empty and r.y1 > top and r.y0 < bottom]


def _union(rects: list):
    band = None
    for r in rects:
        band = r if band is None else band | r
    return band


def _nearest_by_x(rects: list, caps_in_row: list) -> dict[int, list]:
    """Split a ROW's objects across that row's own captions by nearest x-center -- a row
    with two or more captions (side-by-side grid) routes each object to whichever caption
    sits closest in x, so neighbouring panels are not unioned into one crop."""
    centers = [((c.x0 + c.x1) / 2, fig) for fig, c in caps_in_row]
    out: dict[int, list] = {fig.figure_idx: [] for fig, _ in caps_in_row}
    for r in rects:
        cx = (r.x0 + r.x1) / 2
        _, fig = min(centers, key=lambda t: abs(t[0] - cx))
        out[fig.figure_idx].append(r)
    return out


def extract_figure_images(pdf_path: str | Path, figures: list[Figure], out_dir: str | Path,
                          max_figures: int = MAX_FIGURES, dpi: int = 150,
                          max_bytes: int = 3 * 1024 * 1024) -> None:
    """Render each figure's plotted region to `out_dir/F<idx>.png`, IN PLACE on
    `fig.image_path`. ADVISORY ONLY (see `schema.Figure`) -- a geometric crop heuristic,
    never itself the evidence a finding may cite.

    Captions on one page are grouped into visual ROWS first (`_rows`). For each row, the
    plotted region is whatever image placements and vector-drawing clusters lie ABOVE it
    (tried BELOW when nothing is found above), split across that row's own captions by
    nearest x-position (`_nearest_by_x`). Falls back to the whole page when a figure's
    caption cannot be found, or its row has no object to crop. Any failure is swallowed
    for THAT figure only and leaves its `image_path` empty.

    A geometric heuristic tuned on conference ML PDFs with captions adjacent to their
    figures in a regular grid; an irregular layout may still crop wrong or fall back to
    the whole page.
    """
    import pymupdf

    out = Path(out_dir)
    rel_dir = f"{out.parent.name}/{out.name}" if out.parent.name else out.name
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        return
    try:
        by_page: dict[int, list[Figure]] = {}
        for fig in figures[:max_figures]:
            by_page.setdefault(fig.page, []).append(fig)
        if not by_page:
            return
        out.mkdir(parents=True, exist_ok=True)
        for pno, on_page in by_page.items():
            if not (1 <= pno <= doc.page_count):
                continue
            page = doc[pno - 1]
            rows = _rows([(fig, _caption_rect(page, fig)) for fig in on_page])
            objects = _page_object_rects(page)
            top = page.rect.y0
            for ri, row in enumerate(rows):
                capped = [(fig, c) for fig, c in row if c is not None]
                by_fig: dict[int, list] = {}
                if capped:
                    row_top = min(c.y0 for _, c in capped)
                    row_bottom = max(c.y1 for _, c in capped)
                    nxt = next((c.y0 for fig, c in rows[ri + 1] if c is not None),
                              page.rect.y1) if ri + 1 < len(rows) else page.rect.y1
                    in_row = _in_band(objects, top, row_top)
                    if not in_row:
                        in_row = _in_band(objects, row_bottom, nxt)
                    by_fig = _nearest_by_x(in_row, capped)
                    top = row_bottom
                for fig, _cap in row:
                    region = _union(by_fig.get(fig.figure_idx, []))
                    clip = (region + (-8, -8, 8, 8)) & page.rect if region is not None \
                        else page.rect
                    if clip.is_empty:
                        clip = page.rect
                    path = out / f"F{fig.figure_idx}.png"
                    try:
                        pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72),
                                              clip=clip)
                        pix.save(str(path))
                        if path.stat().st_size > max_bytes:
                            path.unlink(missing_ok=True)
                            continue
                    except Exception:
                        continue
                    fig.image_path = f"{rel_dir}/{path.name}"
    finally:
        doc.close()


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The character range of the sentence containing [start, end) in `text`."""
    left = 0
    for m in _SENTENCE_END.finditer(text, 0, start):
        left = m.end()
    m = _SENTENCE_END.search(text, end)
    return left, (m.end() if m else len(text))


def extract_crossrefs(sections: Sequence[Section]) -> list[CrossRef]:
    """Every place the prose CITES a numbered object, with a re-verifiable address.

    Takes SECTIONS AND NOTHING ELSE -- this function cannot see `doc.figures`,
    `doc.tables` or `doc.equations`, so it cannot compare a citation against a recovered
    label set and report anything "missing" (see `harness.schema.CrossRef`).

    Each reference carries a `P<i>:<a>-<b>` span in the same flattened coordinates
    `harness.locate` mints, so the citing sentence is itself checkable. `page` is the
    section's first page -- an honest ceiling, not an estimate.

    A caption occurrence is skipped: 'Figure 3. (a) Spider mamba...' names the figure, it
    does not cite it -- indistinguishable from a genuine sentence-final citation once
    flattened, so the same test drops both.
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
    """How many flattened characters lie before original offset `original`. `offsets` is
    strictly increasing, so one bisect is correct for BOTH ends of a half-open range."""
    return bisect.bisect_left(offsets, original)


def _verbatim_span(text: str, offsets: list[int], start: int, end: int) -> str:
    """The original text spanned by flattened [start, end), whitespace included. Mirrors
    `locate._verbatim` so a `CrossRef.quote` is exactly what `locate.resolve` reads back."""
    if not offsets or start >= end or end > len(offsets):
        return ""
    return text[offsets[start]:offsets[end - 1] + 1]


def _equation_body(lines: list[str], at: int) -> str:
    """The equation body belonging to a lone `(n)` on line `at`, or ''. Walks back up to
    `_EQUATION_LOOKBACK` lines for the nearest short, symbol-bearing line. Returns ''
    rather than guessing when the preceding lines are prose -- a wrong body would let
    `verify_evidence` certify a quote as `equation_verified` against non-equation text."""
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
    """One caption line: the printed number, and the line itself VERBATIM. Two fields
    rather than one reconstructed string, which would INSERT punctuation the paper does
    not print into `Table.caption`/`QuantFinding.benchmark` -- neither re-verified against
    the document the way `evidence_quote` is. Keeping the number separately also lets an
    extractor say "could not label this table" instead of guessing its neighbour's."""

    label: str
    text: str


_NO_CAPTION = _Caption("", "")


def _page_captions(text: str) -> list[_Caption]:
    """Every 'Table N. …' / 'Table N: …' line on a page, in order of appearance. The
    delimiter is required (`_TABLE_CAPTION`), so an in-text 'Table 10, using fully
    fine-tuned models...' is not collected as a caption."""
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
    """Snap ragged (x0, text) rows onto shared column anchors. Column count cannot be read
    off any single row (a header may run words together where a data row splits cleanly,
    and vice versa), so anchors are derived from every row's cell starts at once."""
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
    # Right-aligned numbers under a centred header start a few points apart, opening
    # phantom columns. Neighbours no row (header included) fills together are one printed
    # column; two columns that each carry a header never merge.
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
    Finding bodies independently avoids assuming the caption precedes its table, which is
    false for some venues (and would walk into the NEXT table's rows there)."""
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
    """Whether this page's captions sit 'above' or 'below' the bodies they name. Measured
    in PAGE COORDINATES, not row indices, since stacked tables put a caption exactly one
    row from both neighbours by index. Typography breaks the tie: a caption is set tight
    against its own table. Ties resolve to 'above', the more common convention."""
    above = below = 0
    for top, bottom in cap_spans:
        # `None` (no body on that side) is not a distance of zero.
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
    down the page."""
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
    """Recover LaTeX-style tables that have no ruling lines for pdfplumber to find. Column
    gaps are the signal: a prose line has no wide inter-word gap, a table row always does.
    Bodies are located first on that signal alone; captions are attached afterwards on
    measured proximity, so recovery works whether captions sit above or below."""
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
        # An unlabelled run is discarded: a table nobody can name is a table nobody can
        # cite, and admitting it would displace real tables under MAX_TABLES.
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
    """Would admitting this candidate give a reviewer two addresses for one table? Makes
    it safe to run geometric recovery over a page the ruled path already touched. Two
    nets: the LABEL is exact (a page prints "Table 1" once); the CONTENT overlap catches
    the same duplication when the ruled body was paired with a different label than the
    geometric pairing chose."""
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
    ruled path accounted for (so a page printing two tables and yielding one ruled body
    still gets its second body looked for). Candidates duplicating an already-recovered
    body are dropped by `_already_extracted`.

    A table needs >=2 rows and >=2 columns to be worth auditing. Row 0 becomes `header`
    when it contains no digits.

    Ruled captions are paired positionally -- the i-th ruled body on a page gets the i-th
    caption line -- and `caption_source` records that, so a reader can tell a positional
    guess from the geometric pairing `_pair` performs. An unpaired body keeps its cells
    and records `label=""`: nameless is safer than mislabelled.
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


# WHAT A TABLE IS. Only a RESULT table's cells are measurements a reproduction can be held
# against. A table that SPECIFIES the experiment (its settings, data-generating functions,
# hyper-parameters, notation, an analogy) prints inputs, not outcomes; a table describing a
# dataset prints statistics of that data — checkable against released data, never a result
# of the method. Decided from the paper's own caption, never from a paper name.
_SPEC_CAPTION = re.compile(
    r"\b(hyper-?\s?parameters?|notation|analogy|used to generate|data[- ]generating|"
    r"search space|architecture details|glossary|symbols)\b", re.I)
_DATA_CAPTION = re.compile(
    r"\b(statistics of|dataset statistics|summary of (?:the )?datasets?|datasets? summary|"
    r"data summary|characteristics of|datasets? used|data ?sets? (?:used|considered))\b", re.I)
# The metric NOUN a caption names (for `metric_name`), from the one shared vocabulary.
_METRIC_WORD = re.compile(
    r"\b(RMSE|MSE|MAE|PEHE|ATE|AUC|AUROC|AUPRC|F1|accuracy|error|loss|precision|recall|"
    r"BLEU|ROUGE|METEOR|perplexity|reward|return|regret|delay|ECE|calibration|coverage|"
    r"likelihood|NLL|score|bias|variance|FID|mAP|IoU|mIoU|PSNR|SSIM|LPIPS|WER|CER|NDCG|MRR|"
    r"CRPSS?|R2|correlation|win rate|success rate|runtime|latency|throughput|memory|"
    r"speed-?up)\b", re.I)


def table_role(table: Table) -> str:
    """'text' | 'specification' | 'data_statistics' | 'result' — from the grid's own cells
    and caption, never from a paper name."""
    cap = table.caption or ""
    # A boxed theorem, example or equation the extractor read as a grid: its cells are prose
    # and math fragments, and a lone "1" or "2" among them is a subscript, not a measurement.
    cells = [_norm(c) for row in table.rows for c in row if _norm(c)]
    words = [c for c in cells if not is_numeric_cell(c)]
    numbers = [c for c in cells if is_numeric_cell(c)]
    digits_only = all(re.fullmatch(r"\d", n) for n in numbers)
    # A column of real numbers (two or more, not just single digits) is a measured column,
    # however much prose sits beside it: "Model | Training data | Decoding | Score".
    numeric_column = any(
        sum(1 for r in table.rows if c < len(r) and is_numeric_cell(r[c])) >= 2
        and any(c < len(r) and is_numeric_cell(r[c]) and not re.fullmatch(r"\d", _norm(r[c]))
                for r in table.rows)
        for c in range(max((len(r) for r in table.rows), default=0)))
    if (len(cells) >= 4 and not numeric_column
            and sum(len(w) for w in words) > 12 * max(1, len(words))
            and (len(numbers) * 3 < len(cells) or digits_only)):
        return "text"
    # Only single digits, and neither caption nor header names what they measure: indices,
    # subscripts or counts of a worked example — nothing a reproduction could be held to.
    if (words and numbers and digits_only
            and not _METRIC_WORD.search(" ".join([cap, *table.header]))):
        return "text"
    # A caption naming a measured quantity ("RMSE under different hyper-parameters") is a
    # result table whatever else it mentions.
    if _SPEC_CAPTION.search(cap) and not _METRIC_WORD.search(cap):
        return "specification"
    if _DATA_CAPTION.search(cap) and not _METRIC_WORD.search(cap):
        return "data_statistics"
    return "result"


def _index_row(row: list[str]) -> bool:
    """A row of consecutive small integers ("1 2 3", "(i) (ii)") labels columns; it
    measures nothing."""
    vals = [_norm(c) for c in row if _norm(c)]
    if len(vals) < 2 or not all(re.fullmatch(r"\d{1,2}", v) for v in vals):
        return False
    nums = [int(v) for v in vals]
    return nums == list(range(nums[0], nums[0] + len(nums)))


def column_header(table: Table, col: int) -> str:
    """The column's label: the extracted header, else a first row that carries no number
    while a later row carries one in this column (a header the extractor left in the body)."""
    if col < len(table.header) and _norm(table.header[col]):
        return table.header[col].strip()
    rows = table.rows
    if (rows and col < len(rows[0]) and _norm(rows[0][col])
            and not any(is_numeric_cell(c) for c in rows[0] if _norm(c))
            and any(col < len(r) and is_numeric_cell(r[col]) for r in rows[1:])):
        return rows[0][col].strip()
    return ""


def metric_name(table: Table, col: int) -> str:
    """The metric a column reports, as the paper names it — its header, else the metric the
    caption names — or '' when the paper names none. A header that is not a metric word
    ("IHDP", "Ours") names a dataset or method; the caption's metric then wins."""
    header = column_header(table, col)
    if header and _METRIC_WORD.search(header):
        return header
    if m := _METRIC_WORD.search(table.caption or ""):
        return m.group(1)
    return header if re.search(r"[^\W\d_]", _norm(header)) else ""


def metric_label(table: Table, col: int) -> str:
    """The quantity a column measures: its header, else the metric the caption names,
    else an explicit 'unnamed' label — never a default like 'accuracy'."""
    if header := column_header(table, col):
        return header
    name = metric_name(table, col)
    label = f"Table {table.label or table.table_idx}"
    return f"{name} ({label}, column {col})" if name else f"unnamed quantity ({label}, column {col})"


def table_numbers(tables: list[Table]) -> list[QuantFinding]:
    """Every numeric cell, as a QuantFinding addressed back to its exact coordinates.
    Deterministic by construction: the value IS the cell. Column 0 is the row label
    (arm/method), the header row the metric names."""
    out: list[QuantFinding] = []
    for t in tables:
        role = table_role(t)
        if role in ("specification", "text"):
            continue                       # inputs to the experiment, not its outcomes
        for r, row in enumerate(t.rows):
            if _index_row(row):
                continue                   # column labels, not measurements
            method = row[0] if row else ""
            for c, cell in enumerate(row):
                if c == 0 or not is_numeric_cell(cell):
                    continue
                spread = _SPREAD.search(cell)
                metric = metric_label(t, c)
                if metric.startswith("unnamed") and _norm(method) and role == "data_statistics":
                    # A statistics table names its quantity per ROW ("#Frames").
                    metric = f"{_norm(method)} (Table {t.label or t.table_idx}, column {c})"
                out.append(QuantFinding(
                    benchmark=(("[data statistic] " if role == "data_statistics" else "")
                               + t.caption)[:120], method=method,
                    metric=metric,
                    value=cell, seeds_or_variance=spread.group(1) if spread else "",
                    source_quote=cell, page=t.page, table_ref=t.ref(r, c),
                ))
    return out


def prose_numbers(sections: list[Section], limit: int = MAX_NUMBERS) -> list[QuantFinding]:
    """Numeric claims stated in the running text, each with its whole sentence -- a
    narrative "+4.2%" only becomes a finding once set beside the cell it summarizes."""
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
    """Figure captions as text an auditor can cite by `F<n>` address. A caption names a
    figure, it does not report the values in it — `(image: <path>)` is appended when a
    best-effort PNG crop exists, advisory only (see `schema.Figure`)."""
    return "\n".join(
        f"[F{fig.figure_idx}] page {fig.page} — {fig.label}: {fig.caption}"
        + (f" (image: {fig.image_path})" if fig.image_path else "")
        for fig in figures)


def render_equations(equations: list[Equation]) -> str:
    """Extracted display equations as text an auditor can cite by `E<n>` address."""
    return "\n".join(
        f"[E{eq.equation_idx}] page {eq.page}{f' ({eq.number})' if eq.number else ''} — {eq.text}"
        for eq in equations)


def render_sections(sections: list[Section], budget_chars: int) -> str:
    """Sections as text, each allotted an equal slice of the context budget. Equal
    slices, not first-N-wins: truncating from the end would silently drop Conclusions
    and Limitations, exactly where the contradiction lens finds its concessions."""
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
    """How much of the extracted prose a lens was actually shown -- the ceiling on any
    recall claim this system makes, since `render_sections` divides a character budget
    equally across sections and a long paper is TRUNCATED before any lens reads it.
    Reported, never enforced -- computed by the same arithmetic `render_sections` uses.
    `plan_reading`/`plan` below is what removes the cut."""

    total_chars: int
    presented_chars: int
    sections: int
    sections_truncated: int

    @property
    def fraction(self) -> float | None:
        """None when there is no prose at all -- never 1.0 about a document nothing was
        extracted from."""
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
# otherwise unquotable by either part (`locate.mint` refuses a quotation it cannot
# relocate). Cannot create a false address: minting searches the PARSED DOCUMENT, not the
# prompt, so the uniqueness rule is untouched.
SPLIT_OVERLAP_CHARS = 600


class ReadingPart(NamedTuple):
    """One pass of a paper that fits in a single prompt. A plan of these covers EVERY
    character of every extracted section, which `render_sections` alone cannot do (it
    hard-slices each section against one shared budget)."""

    number: int                # 1-based ("part 2 of 3"); `index` would shadow tuple.index
    total: int                 # parts in this plan
    sections: list             # Section objects, whole or sliced
    chars: int                 # characters of section text in this part
    split_sections: int        # sections in this part that are a slice of a larger one
    # (section_idx, start, end) per section, as offsets into that section's ORIGINAL text
    # -- a section whose text repeats defeats substring search, so offsets make the
    # coverage union checkable arithmetically instead.
    slices: list

    @property
    def label(self) -> str:
        return f"part {self.number} of {self.total}"


def _split_section(s: Section, budget: int) -> list[tuple[Section, int, int]]:
    """One oversized section as a sequence of slices, cut at whitespace where possible.
    Returns each slice with the offsets it was taken from, so a caller can prove the
    slices tile the original."""
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
    """One part's sections as prompt text, with no truncation anywhere. Deliberately not
    `render_sections`, which fits a budget by cutting -- reusing it would reintroduce the
    slice this exists to remove. `render_part_with_anchor` below prepends the anchor
    packet for a lens that needs the whole-paper comparison too."""
    return "\n\n".join(
        f"## {s.title or f'(section {s.section_idx})'}  [p{s.page_start}]\n{s.text}"
        for s in part.sections)


# --------------------------------------------------------------------------------------- #
# Anchors, coverage accounting and within-lens cross-part synthesis
# --------------------------------------------------------------------------------------- #
# Three things, kept apart on purpose. ANCHORS are a small, deterministic packet repeated
# identically in every part (title, abstract, conclusion, outline) that restores the
# whole-paper comparison splitting would otherwise take from the contradiction lens --
# extracted, never generated, and carrying NO model output. PARTS are blind to one
# another: a lens reading part 2 does not see what it wrote about part 1. SYNTHESIS is
# where cross-part reasoning happens, once per lens, after it has read every part; its
# input is the anchors plus that lens's OWN quotation-grounded candidates, never another
# lens's output. Independence is BETWEEN lenses, not within one lens's own parts.

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
    """The packet every part carries. The abstract/conclusion locators are
    `harness.decide`'s, deliberately -- a second spelling is how the two drift."""
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
    """Four numbers answering four different questions, reported separately -- collapsing
    them is how "the readers saw the paper" gets printed about a run that saw a third of
    it. The anchor figures are a COST, not a coverage claim."""

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
    """The whole reading strategy for one paper. The anchor packet is charged against the
    budget before the paper is packed, since every part carries it."""
    anchor = anchors(doc)
    # A paper that fits WHOLE is read whole and pays nothing for the anchor packet: on a
    # paper nothing was taken away from, restoring the comparison is pure cost.
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
            # Zero on a paper read whole: the packet is not sent, so a cost nobody paid
            # must not appear in the accounting.
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
    """The whole input to one lens's cross-part synthesis. Nothing else reaches it: no
    other lens's output, no hidden reasoning (only quotation-carrying candidates), and no
    decision, grade or outcome -- so it cannot be steered by what the harness concluded."""

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
    Both shapes are real: a part artifact on disk is JSON, and re-inflating it into a
    model would mean carrying every harness-written field not yet computed."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def synthesis_brief(paper_id: str, lens: str, anchor: AnchorPacket,
                    per_part_findings: dict[int, list]) -> SynthesisBrief:
    """Assemble one lens's own grounded observations across its own parts.
    `per_part_findings` maps a part number to that part's findings. A finding with no
    evidence quotation is dropped here: the synthesis reasons over what can be relocated
    in the paper."""
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
    """One part's prompt body: the anchors, then this part's own sections. The order is
    deliberate: anchors come first so a reader meets the paper's claims before the span
    it is being asked to examine, the order a referee reads in."""
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

    # A caption must be text the paper actually contains.
    flat_pages = [_norm(p) for p in pg]
    for t in tbls:
        assert not t.caption or any(t.caption in fp for fp in flat_pages), \
            f"T{t.table_idx}'s caption is not printed in this paper: {t.caption!r}"

    # A cross-reference must be re-derivable from its own address, via the same
    # resolver a finding's evidence goes through.
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
