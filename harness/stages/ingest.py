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

import hashlib
from pathlib import Path

from .. import pdf, repo, state
from ..artifacts import PaperDoc
from ..config import Config


def content_sha(path: str | Path) -> str:
    """The paper's identity as a document: sha256 of its bytes, first 12 hex.

    `paper_id` is a slug of the filename, which is readable and is not an identity —
    `APT _ ICML.pdf`, `APT-ICML.pdf` and `apt icml.pdf` all slugify to `apt-icml`. Two
    unrelated papers landing on the same slug used to share one project directory, and
    the second was reported as `cached`, so it was reviewed against the first paper's
    parsed text under its own name. Nothing errored and nothing in the report said so.
    """
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def paper_id_for(path: str | Path) -> str:
    return state.slugify(Path(path).stem, 40)


def _same_paper_by_content(doc_path: Path, src: Path) -> bool:
    """Can a legacy project (no content_sha) be shown to be THIS document?

    Only from content. The title the parser recovers from the PDF is compared with the
    title the legacy artifact recorded; agreement is content-level evidence that the two
    are the same paper, and anything else — a missing title, a parse failure, a different
    title — is not. Deliberately conservative: a wrong "different paper" answer costs a
    duplicate project directory, and a wrong "same paper" answer reviews one paper against
    another paper's evidence.
    """
    try:
        recorded = str(state.read_json(doc_path).get("title") or "").strip()
    except (OSError, ValueError):
        return False
    if not recorded:
        return False
    try:
        title = (pdf.guess_title(pdf.page_texts(src)) or "").strip()
    except Exception:
        return False
    return bool(title) and _norm_title(title) == _norm_title(recorded)


def _norm_title(s: str) -> str:
    return " ".join((s or "").lower().split())


def allocate_paper_id(cfg: Config, src: Path, sha: str) -> tuple[str, bool]:
    """(paper_id, is_same_paper) — the slug, disambiguated if it is already taken.

    The readable slug is kept whenever it is free or already belongs to this document.
    Only a genuine collision — same slug, different bytes — gets a suffix, so the common
    case still produces `apt-icml` and the pathological one produces `apt-icml-9f3c1a`
    instead of silently merging two papers.
    """
    base = paper_id_for(src)
    for pid in (base, f"{base}-{sha[:6]}"):
        doc = state.project_dir(cfg, pid) / "paper" / "doc.json"
        if not doc.exists():
            return pid, False
        try:
            recorded = str(state.read_json(doc).get("content_sha") or "")
        except (OSError, ValueError):
            recorded = ""
        if recorded == sha:
            return pid, True                       # provably the same document
        if not recorded:
            # A legacy project with no recorded hash. It is PRESERVED — never overwritten,
            # never deleted — but it may not absorb a document whose identity cannot be
            # matched against it, because an absent hash is the absence of evidence and not
            # evidence of sameness. Reproduced: a legacy `apt-icml` with content_sha=None
            # accepted an unrelated PDF whose filename slugified the same way, reported
            # `cached: True`, and drove a complete review of the new paper against the old
            # doc.json, the old lens files and the old findings.
            #
            # Identity is established from the paper's own bytes where the parser can do it:
            # if the legacy doc.json records a title and the new PDF parses to the same
            # title, that is content-level agreement and the case is reused. Otherwise the
            # new document gets its own id, and the legacy project stays exactly as it is.
            if _same_paper_by_content(doc, src):
                return pid, True
            continue
    # Both taken by other documents: fall back to the full hash, which cannot collide
    # without the papers being byte-identical.
    return f"{base}-{sha}", False


def run_ingest(cfg: Config, paper_path: str) -> dict:
    """Parse a PDF into `paper/doc.json`. Idempotent."""
    src = Path(paper_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"no such paper: {src}")
    sha = content_sha(src)
    pid, same = allocate_paper_id(cfg, src, sha)

    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if same and doc_path.exists():
        d = state.read_json(doc_path)
        # A cached document is NOT re-parsed, deliberately. Every `F<n>`, `T<i>:r<r>:c<c>`
        # and `P<i>:<a>-<b>` in this project's lens files was minted against exactly this
        # doc.json, and re-parsing it under a newer extractor would leave those references
        # pointing at different objects — the harness would then attest to the contents of
        # something nobody cited. So the version is REPORTED and nothing is rebuilt: the
        # caller learns the parse is older than the current extractor and decides.
        recorded = int(d.get("extraction_version") or 1)
        return {"cached": True, "paper_id": pid, "title": d.get("title", ""),
                "content_sha": d.get("content_sha", "") or sha,
                "sections": len(d.get("sections", [])), "tables": len(d.get("tables", [])),
                "numbers": len(d.get("reported_numbers", [])),
                "extraction_version": recorded,
                "stale_extraction": recorded != pdf.EXTRACTION_VERSION,
                "repo_url": d.get("repo_url", "") or None, "doc": "paper/doc.json"}

    pages = pdf.page_texts(src)
    sections = pdf.split_sections(pages)
    tables = pdf.extract_tables(src, pages)
    figures = pdf.extract_figures(pages)
    equations = pdf.extract_equations(pages)
    # What the prose CITES, never what the paper contains: `extract_crossrefs` is handed
    # the sections and nothing else, so it cannot compare a citation against a recovered
    # label set. See `harness.artifacts.CrossRef`.
    crossrefs = pdf.extract_crossrefs(sections)
    numbers = pdf.table_numbers(tables) + pdf.prose_numbers(sections)
    title = pdf.guess_title(pages) or src.stem

    if not root.exists():
        # A case is one paper under review. `direction` carries the title so the
        # existing dashboard labels the case correctly with no changes.
        state.create_project(cfg, "", title, pid=pid)
    meta = state.load_meta(cfg, pid)
    meta["paper_path"], meta["content_sha"] = str(src), sha
    state.save_meta(cfg, pid, meta)
    state.set_phase(cfg, pid, "ingest")

    doc = PaperDoc(paper_id=pid, title=title, source_path=str(src), content_sha=sha,
                   n_pages=len(pages),
                   sections=sections, tables=tables, figures=figures, equations=equations,
                   reported_numbers=numbers, crossrefs=crossrefs,
                   body_end_section_idx=pdf.references_boundary(sections),
                   extraction_version=pdf.EXTRACTION_VERSION)
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
                 "figures": len(figures), "equations": len(equations),
                 "crossrefs": len(crossrefs),
                 "extraction_version": pdf.EXTRACTION_VERSION,
                 "table_numbers": sum(1 for n in numbers if n.table_ref)},
        path=str(doc_path),
    )
    return {"paper_id": pid, "title": title, "pages": len(pages), "content_sha": sha,
            "sections": len(sections), "tables": len(tables), "numbers": len(numbers),
            "figures": len(figures), "equations": len(equations),
            "crossrefs": len(crossrefs),
            "body_end_section_idx": doc.body_end_section_idx,
            "extraction_version": pdf.EXTRACTION_VERSION, "stale_extraction": False,
            "table_numbers": sum(1 for n in numbers if n.table_ref),
            "repo_url": doc.repo_url or None, "doc": "paper/doc.json"}
