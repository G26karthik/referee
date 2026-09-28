"""Ingest: PDF -> page texts, visual rows, page images, identifiers, the paper's own repo.

Workers read `paper.md`, which is exactly the text quotes are re-found in, so a faithful
quote resolves by construction. There is no table parser: a printed number is cited by the
visual row it sits on (`evidence.Paper.cell`), and readers see the page images.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import repo, state
from .evidence import Paper

_ARXIV = re.compile(r"arXiv\s*:\s*(\d{4}\.\d{4,5})(v\d+)?", re.I)
_TABLE_PAGE = re.compile(r"\bTable\s+\d", re.I)


def _rows(page) -> list[str]:
    """Words grouped into visual rows by baseline (a table row reads left to right)."""
    words = sorted(page.get_text("words"), key=lambda w: (round(w[3]), w[0]))
    rows, cur, y = [], [], None
    for w in words:
        if y is not None and abs(w[3] - y) > 2.5:
            rows.append(" ".join(cur))
            cur = []
        cur.append(w[4])
        y = w[3]
    return rows + ([" ".join(cur)] if cur else [])


def _title(doc, first_page: str) -> str:
    """The largest horizontal text on page 1, else its first substantial line."""
    spans = [(s["size"], s["bbox"][1], s["text"].strip())
             for b in doc[0].get_text("dict")["blocks"] for line in b.get("lines", [])
             if tuple(round(x) for x in line.get("dir", (1, 0))) == (1, 0)
             for s in line.get("spans", []) if len(s["text"].strip()) > 1]
    if spans:
        top = max(s[0] for s in spans)
        title = " ".join(t for size, _, t in sorted(spans, key=lambda s: s[1]) if abs(size - top) < 0.6)
        if 12 <= len(title) <= 300 and not title.lower().startswith("arxiv"):
            return re.sub(r"\s+", " ", title)
    return next((ln.strip() for ln in first_page.splitlines() if len(ln.strip()) > 12), "")


def load(cfg: state.Config, pid: str) -> tuple[dict, Paper]:
    meta = state.read_json(state.pdir(cfg, pid) / "paper" / "doc.json")
    if not meta:
        raise FileNotFoundError(f"no ingested paper '{pid}'")
    return meta, Paper(meta["pages"], meta["rows"])


def ingest(cfg: state.Config, pdf: Path) -> str:
    """The project id for `pdf`, ingesting it on first sight. A PDF already ingested (by
    content) is the same project, never a second one."""
    import pymupdf

    data = Path(pdf).read_bytes()
    sha = state.sha256(data)
    for existing in cfg.projects.glob("*/paper/doc.json"):
        if (state.read_json(existing) or {}).get("sha256") == sha:
            return existing.parent.parent.name
    pid = state.slug(Path(pdf).stem)
    if (state.pdir(cfg, pid) / "paper").exists():
        pid = f"{pid}-{sha[:6]}"
    out = state.pdir(cfg, pid) / "paper"
    (out / "pages").mkdir(parents=True, exist_ok=True)
    with pymupdf.open(str(pdf)) as doc:
        pages = [p.get_text("text") for p in doc]
        rows = [_rows(p) for p in doc]
        for i, p in enumerate(doc, 1):
            p.get_pixmap(dpi=110).save(str(out / "pages" / f"p{i:03d}.png"))
        title = _title(doc, pages[0] if pages else "")
    m = _ARXIV.search("\n".join(pages[:2]))
    meta = {"pid": pid, "title": title, "sha256": sha, "source": str(Path(pdf).resolve()),
            "arxiv_id": m.group(1) if m else "", "arxiv_version": (m.group(2) or "") if m else "",
            "pages": pages, "rows": rows}
    state.write_json(out / "doc.json", meta)
    (out / "paper.md").write_text(
        f"# {title}\n\n" + "".join(f"\n=== PAGE {i} ===\n{t}" for i, t in enumerate(pages, 1)),
        encoding="utf-8")
    # Tables are read by their visual rows: one line per printed row, per page with a table.
    (out / "rows.md").write_text("".join(
        f"\n=== PAGE {i} (visual rows) ===\n" + "\n".join(r) + "\n"
        for i, (t, r) in enumerate(zip(pages, rows), 1) if _TABLE_PAGE.search(t)), encoding="utf-8")
    repo.acquire(cfg, pid, meta)
    state.append_jsonl(state.pdir(cfg, pid) / "log.jsonl", {"event": "ingest", "pages": len(pages)})
    return pid
