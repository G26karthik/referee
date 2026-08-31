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

from .artifacts import QuantFinding, Section, Table

# ponytail: bounds sized for conference ML papers (8-10pp + appendix). A 400-page
# thesis is truncated, not crashed; raise these if that ever becomes the workload.
MAX_PAGES = 60
MAX_TABLES = 40
MAX_ROWS = 60
MAX_COLS = 12
MAX_SECTION_CHARS = 40_000

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


def guess_title(pages: list[str]) -> str:
    """First substantial line of page 1 that is not a header/arXiv stamp."""
    for raw in (pages[0] if pages else "").splitlines()[:25]:
        line = _norm(raw)
        if len(line) < 8 or line.lower().startswith(("arxiv:", "preprint", "under review")):
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


def _unruled_tables(page, captions: list[str]) -> list[tuple[str, list[list[str]]]]:
    """Recover LaTeX-style tables that have no ruling lines for pdfplumber to find.

    Column gaps are the signal: a prose line or a wrapped caption has no wide
    inter-word gap, a table row always does. So each table is the run of gapped rows
    that follows a "Table N" caption, ending at the first ungapped row after it.
    """
    rows = _word_rows(page)
    gapped = [any(b["x0"] - a["x1"] > 12 for a, b in zip(r, r[1:])) for r in rows]
    text = [_norm(" ".join(w["text"] for w in r)) for r in rows]

    out: list[tuple[str, list[list[str]]]] = []
    i = 0
    while i < len(rows):
        if not _TABLE_CAPTION.match(text[i]):
            i += 1
            continue
        j = i + 1
        while j < len(rows) and not gapped[j] and not _TABLE_CAPTION.match(text[j]):
            j += 1  # wrapped caption lines
        body = []
        while j < len(rows) and gapped[j] and not _TABLE_CAPTION.match(text[j]):
            body.append(_chunks(rows[j]))
            j += 1
        if len(body) >= 2:
            grid = _grid(body)
            if grid and len(grid[0]) >= 2:
                out.append((captions[len(out)] if len(out) < len(captions) else "", grid))
        i = max(j, i + 1)
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
    print(f"{src.name}: {len(pg)} pages, {len(secs)} sections, {len(tbls)} tables")
    print(f"title: {guess_title(pg)!r}")
    assert pg, "no pages extracted"
    assert secs, "no sections extracted"
    assert sum(len(s.text) for s in secs) > 500, "suspiciously little text extracted"
    for s in secs[:12]:
        print(f"  §{s.section_idx} p{s.page_start}-{s.page_end} {s.title!r} ({len(s.text)} chars)")
    for t in tbls[:5]:
        print(f"  [T{t.table_idx}] p{t.page} {len(t.rows)}x{len(t.rows[0])} {t.caption[:60]!r}")
