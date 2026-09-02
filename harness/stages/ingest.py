"""S1 — ingest one submitted paper into a `PaperDoc`. Fully deterministic.

No model runs here. Sections and tables come from `harness/pdf.py`; every reported
number is either a table cell (the value IS the cell) or a whole sentence lifted
verbatim from the prose. Nothing is summarized, rounded, or re-unit-ed on the way in.

That is a stronger guarantee than the extraction-plus-verification design it replaces:
provenance is not checked after the fact, it is structural. Re-running ingestion on
the same PDF always produces the same PaperDoc.

Interpretation — what counts as a *claim* — is deliberately left to the audit lenses,
which read the sections themselves.
"""
from __future__ import annotations

from pathlib import Path

from .. import pdf, repo, state
from ..artifacts import PaperDoc
from ..config import Config


def paper_id_for(path: str | Path) -> str:
    return state.slugify(Path(path).stem, 40)


def run_ingest(cfg: Config, paper_path: str) -> dict:
    """Parse a PDF into `paper/doc.json`. Idempotent."""
    src = Path(paper_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"no such paper: {src}")
    pid = paper_id_for(src)

    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if doc_path.exists():
        d = state.read_json(doc_path)
        return {"cached": True, "paper_id": pid, "title": d.get("title", ""),
                "sections": len(d.get("sections", [])), "tables": len(d.get("tables", [])),
                "numbers": len(d.get("reported_numbers", [])),
                "repo_url": d.get("repo_url", "") or None, "doc": "paper/doc.json"}

    pages = pdf.page_texts(src)
    sections = pdf.split_sections(pages)
    tables = pdf.extract_tables(src, pages)
    numbers = pdf.table_numbers(tables) + pdf.prose_numbers(sections)
    title = pdf.guess_title(pages) or src.stem

    if not root.exists():
        # A case is one paper under review. `direction` carries the title so the
        # existing dashboard labels the case correctly with no changes.
        state.create_project(cfg, "", title, pid=pid)
    meta = state.load_meta(cfg, pid)
    meta["paper_path"] = str(src)
    state.save_meta(cfg, pid, meta)
    state.set_phase(cfg, pid, "ingest")

    doc = PaperDoc(paper_id=pid, title=title, source_path=str(src), n_pages=len(pages),
                   sections=sections, tables=tables, reported_numbers=numbers)
    # The repository the paper advertises, ranked by how strongly the surrounding text
    # marks it as the authors' own. Recorded at ingest so S3 never re-opens the PDF, and
    # so a reader can see which URL the harness would clone before anything is fetched.
    doc.repo_urls = repo.find_repo_urls(doc)
    # NOT repo_urls[0]. The candidate list is every repository URL the paper mentions;
    # `repo_url` is the one it advertises as its own, and a paper that advertises none
    # must end up with "" rather than with the top-ranked link it happened to cite.
    doc.repo_url = repo.official_repo_url(doc)
    state.write_json(doc_path, doc.model_dump())
    state.append_log(
        cfg, pid, artifact_type="paper_ingested", phase="ingest",
        headers={"title": title[:80], "pages": len(pages), "sections": len(sections),
                 "tables": len(tables), "numbers": len(numbers),
                 "table_numbers": sum(1 for n in numbers if n.table_ref)},
        path=str(doc_path),
    )
    return {"paper_id": pid, "title": title, "pages": len(pages),
            "sections": len(sections), "tables": len(tables), "numbers": len(numbers),
            "table_numbers": sum(1 for n in numbers if n.table_ref),
            "repo_url": doc.repo_url or None, "doc": "paper/doc.json"}
