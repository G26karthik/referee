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

from .artifacts import Equation, Figure, QuantFinding, Section, Table

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
_TABLE_CAPTION = re.compile(r"^\s*(?:table|tab\.)\s*([IVXLC]+|\d+)\s*[:.—-]?\s*(.{0,200})", re.I)
# Mirrors `_TABLE_CAPTION` exactly, substituting "figure"/"fig." — same reasoning, same
# shape. `MAX_FIGURES` bounds it the way `MAX_TABLES` bounds table extraction.
_FIGURE_CAPTION = re.compile(r"^\s*(?:figure|fig\.)\s*([IVXLC]+|\d+)\s*[:.—-]?\s*(.{0,300})", re.I)
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
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").replace("­", "")).strip()


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


def split_sections(pages: list[str]) -> list[Section]:
    """Walk every line, starting a new Section at each heading.

    Text before the first heading becomes the front-matter section, so no content is
    ever dropped on the floor.
    """
    sections: list[Section] = []
    title, buf, start = "", [], 1

    def flush(end_page: int) -> None:
        body = _norm(" ".join(buf))[:MAX_SECTION_CHARS]
        if body or title:
            sections.append(Section(section_idx=len(sections), title=title,
                                    page_start=start, page_end=end_page, text=body))

    for pno, text in enumerate(pages, start=1):
        for raw in text.splitlines():
            if is_heading(raw):
                flush(pno)
                title, buf, start = _norm(raw), [], pno
            elif _norm(raw):
                buf.append(_norm(raw))
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


def _page_captions(text: str) -> list[str]:
    """Every 'Table N: ...' line on a page, in order of appearance."""
    out = []
    for raw in text.splitlines():
        m = _TABLE_CAPTION.match(_norm(raw))
        if m:
            out.append(_norm(f"Table {m.group(1)}: {m.group(2)}").rstrip(": "))
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


def _body_runs(gapped: list[bool], is_caption: list[bool]) -> list[tuple[int, int]]:
    """Half-open row ranges that look like a table body, found WITHOUT reference to captions.

    Finding bodies independently is the whole point. Anchoring the scan on a caption and
    reading forward assumes the caption precedes its table, which is true for some venues
    and false for others; where it is false the scan walks into the NEXT table's rows and
    drops the first table on the page entirely, shifting every label after it.
    """
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(gapped):
        if gapped[i] and not is_caption[i]:
            j = i
            while j < len(gapped) and gapped[j] and not is_caption[j]:
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
          captions: Sequence[str]) -> list[str]:
    """One caption per body, nearest-first on the side this page actually uses.

    Nearest-first rather than positional, so a table whose caption sits on the previous
    page leaves a blank label instead of stealing its neighbour's and cascading the error
    down the page. A body that ends up with no caption keeps its cells; only the label is
    unknown, and an unlabelled table is far less damaging than a mislabelled one.
    """
    side = _caption_side(run_spans, cap_spans)
    labels = [""] * len(run_spans)
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


def _unruled_tables(page, captions: list[str]) -> list[tuple[str, list[list[str]]]]:
    """Recover LaTeX-style tables that have no ruling lines for pdfplumber to find.

    Column gaps are the signal: a prose line or a wrapped caption has no wide
    inter-word gap, a table row always does. Bodies are located first, on that signal
    alone; captions are attached afterwards on measured proximity, so the recovery works
    whether the venue prints captions above its tables or below them.
    """
    rows = _word_rows(page)
    gapped = [any(b["x0"] - a["x1"] > 12 for a, b in zip(r, r[1:])) for r in rows]
    text = [_norm(" ".join(w["text"] for w in r)) for r in rows]
    is_caption = [bool(_TABLE_CAPTION.match(t)) for t in text]
    span = [(min(w["top"] for w in r), max(w["bottom"] for w in r)) for r in rows]

    runs = _body_runs(gapped, is_caption)
    labels = _pair([(span[s][0], span[e - 1][1]) for s, e in runs],
                   [span[i] for i, c in enumerate(is_caption) if c], captions)

    out: list[tuple[str, list[list[str]]]] = []
    for (start, end), label in zip(runs, labels):
        # An unlabelled run is discarded. Detecting bodies without reference to captions
        # is what fixes the ordering bug, but it also admits any run of wide-gapped prose
        # - equation blocks, figure legends, two-column body text - as a candidate table.
        # Requiring a caption is the filter that was previously doing that work implicitly,
        # and it is the honest one to keep: a table nobody can name is a table nobody can
        # cite, and admitting it would displace real tables under MAX_TABLES.
        if not label:
            continue
        grid = _grid([_chunks(rows[i]) for i in range(start, end)])
        if grid and len(grid[0]) >= 2:
            out.append((label, grid))
    return out


def extract_tables(path: str | Path, pages: list[str], max_tables: int = MAX_TABLES) -> list[Table]:
    """Structured tables with (table_idx, row_idx, col_idx) addressing.

    Ruled tables are read by pdfplumber directly; a page where that finds nothing
    falls back to word-geometry recovery, because most ML papers ship booktabs
    tables with no vertical rules at all.

    A table needs >=2 rows and >=2 columns to be worth auditing; anything smaller is
    a layout artifact, not data. Row 0 becomes `header` when it contains no digits.

    ponytail: captions are paired positionally — the i-th table found on a page gets
    the i-th "Table N:" line on that page. Wrong only when a page's tables and their
    captions appear in different orders, which costs a label, never a cell value.
    """
    import pdfplumber

    tables: list[Table] = []

    def add(pno: int, caption: str, raw: Sequence[Sequence[str | None]]) -> None:
        rows = _clean_rows(raw)
        if len(rows) < 2 or len(rows[0]) < 2:
            return
        head, body = ([], rows)
        if not any(re.search(r"\d", c) for c in rows[0]):
            head, body = rows[0], rows[1:]
        if body:
            tables.append(Table(table_idx=len(tables), page=pno, caption=caption,
                                header=head, rows=body))

    with pdfplumber.open(str(path)) as pdf:
        for pno, page in enumerate(pdf.pages[: len(pages)], start=1):
            captions = _page_captions(pages[pno - 1])
            before = len(tables)
            for found in page.extract_tables():
                if len(tables) >= max_tables:
                    return tables
                idx = sum(1 for t in tables if t.page == pno)
                add(pno, captions[idx] if idx < len(captions) else "", found)
            if len(tables) == before:
                for caption, grid in _unruled_tables(page, captions):
                    if len(tables) >= max_tables:
                        return tables
                    add(pno, caption, grid)
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


if __name__ == "__main__":  # self-check: python -m harness.pdf <file.pdf>
    import sys

    src = Path(sys.argv[1])
    pg = page_texts(src)
    secs, tbls = split_sections(pg), extract_tables(src, pg)
    figs, eqs = extract_figures(pg), extract_equations(pg)
    print(f"{src.name}: {len(pg)} pages, {len(secs)} sections, {len(tbls)} tables, "
         f"{len(figs)} figure caption(s), {len(eqs)} equation(s)")
    print(f"title: {guess_title(pg)!r}")
    assert pg, "no pages extracted"
    assert secs, "no sections extracted"
    assert sum(len(s.text) for s in secs) > 500, "suspiciously little text extracted"
    for s in secs[:12]:
        print(f"  §{s.section_idx} p{s.page_start}-{s.page_end} {s.title!r} ({len(s.text)} chars)")
    for t in tbls[:5]:
        print(f"  [T{t.table_idx}] p{t.page} {len(t.rows)}x{len(t.rows[0])} {t.caption[:60]!r}")
    for fig in figs[:5]:
        print(f"  [F{fig.figure_idx}] p{fig.page} {fig.label!r} {fig.caption[:60]!r}")
    for eq in eqs[:5]:
        print(f"  [E{eq.equation_idx}] p{eq.page} ({eq.number}) {eq.text[:60]!r}")
