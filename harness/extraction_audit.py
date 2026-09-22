"""Vision check of the PARSER, never of the paper. `python -m harness.extraction_audit`
runs the self-check.

This module renders page images and a comparison prompt (`build_prompt`), then validates
and seals whatever a reader answers (`seal`). It never calls a model itself — the harness
only renders prompts and validates/seals outputs; something else (a session subagent, an
operator) does the reading and hands the raw JSON text back to `seal`.

**What this may and may not establish.** A MISMATCH/UNSURE verdict is a flag that
EXTRACTION may be wrong for one table or the title — it is never a scientific finding, a
severity, or a disposition input (invariant 13: document integrity is not a paper
finding). `observations()` returns plain dicts rather than `schema.DocumentObservation`
instances on purpose: their `check` values ("VISION_TABLE_MISMATCH", "VISION_TABLE_UNSURE",
"VISION_TITLE_MISMATCH") are not in the closed `schema.INTEGRITY_CHECKS` vocabulary, and
this module's file ownership does not extend to widening that vocabulary or to
`report.py`. See `observations()`'s docstring for the exact 3-line hook that would wire
these in once someone owns those two files.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import sealing, state
from .config import Config
from .prompts import extraction_audit as EA
from .schema import PaperDoc

# Matches `delegation.WRITTEN_BY["SESSION_SUBAGENT"]` — this module never spawns a CLI
# subprocess itself, so `seal()` only ever writes this one token.
WRITTEN_BY = "session_subagent"
REVIEWER = "session subagent extraction auditor"

MAX_TABLES = 12
MIN_ROWS = 2
PAGE_DPI = 110
_VERDICTS = ("MATCH", "MISMATCH", "UNSURE")


def _doc(cfg: Config, pid: str) -> PaperDoc:
    return PaperDoc(**state.read_json(state.project_dir(cfg, pid) / "paper" / "doc.json"))


def _qualifying_tables(doc: PaperDoc) -> list:
    """Tables worth a vision check: at least `MIN_ROWS` rows, capped at `MAX_TABLES` —
    a one-row table is usually a caption-only fragment the parser had little to get
    wrong, and an unbounded prompt would spend the auditor's budget on the least
    informative tables in a long paper."""
    return [t for t in doc.tables if len(t.rows) >= MIN_ROWS][:MAX_TABLES]


def _table_markdown(t) -> str:
    lines = []
    if t.header:
        lines.append("| " + " | ".join(t.header) + " |")
        lines.append("| " + " | ".join("---" for _ in t.header) + " |")
    for row in t.rows[:8]:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def render_pages(pdf_path: str | Path, page_numbers, out_dir: str | Path) -> dict[int, str]:
    """{page_no: path} — one PNG per page, rendered lazily (an existing file is reused).
    Best-effort: an unreadable PDF or a bad page number is skipped, never raised."""
    import pymupdf

    out = Path(out_dir)
    paths: dict[int, str] = {}
    try:
        pdf = pymupdf.open(str(pdf_path))
    except Exception:
        return paths
    try:
        out.mkdir(parents=True, exist_ok=True)
        for pno in sorted(set(page_numbers)):
            if not (1 <= pno <= pdf.page_count):
                continue
            path = out / f"p{pno}.png"
            if not path.exists():
                try:
                    pix = pdf[pno - 1].get_pixmap(
                        matrix=pymupdf.Matrix(PAGE_DPI / 72, PAGE_DPI / 72))
                    pix.save(str(path))
                except Exception:
                    continue
            paths[pno] = str(path)
    finally:
        pdf.close()
    return paths


def build_prompt(cfg: Config, pid: str) -> Path:
    """Write `projects/<pid>/tasks/extraction_audit.md` (and the page PNGs it points at)
    and return its path. Regenerated every call — never hand-edited, like the audit-lens
    prompts under `audit/prompts/`."""
    root = state.project_dir(cfg, pid)
    doc = _doc(cfg, pid)
    tables = _qualifying_tables(doc)
    pages_dir = root / "paper" / "pages"
    page_nums = {1} | {t.page for t in tables}
    src = Path(doc.source_path) if doc.source_path else None
    images = render_pages(src, page_nums, pages_dir) if src and src.is_file() else {}
    rows = [{"table_idx": t.table_idx, "page": t.page,
             "image_path": images.get(t.page, ""), "markdown": _table_markdown(t)}
           for t in tables]
    body = EA.build(title=doc.title, page1_image=images.get(1, ""), tables=rows)
    tasks_dir = root / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / "extraction_audit.md"
    path.write_text(body, encoding="utf-8")
    return path


def _paths(cfg: Config, pid: str) -> tuple[Path, Path]:
    out = state.project_dir(cfg, pid) / "paper" / "extraction_audit.json"
    return out, out.with_suffix(".driver.json")


def _clean(cfg: Config, pid: str, raw_json_text: str) -> dict:
    """Validate shape: drop unknown keys, drop any `table_idx` not among this paper's own
    qualifying tables (the harness's own list, never the reader's say-so), coerce an
    unrecognised verdict to UNSURE rather than dropping the row silently."""
    valid_idx = {t.table_idx for t in _qualifying_tables(_doc(cfg, pid))}
    data = json.loads(raw_json_text)
    if not isinstance(data, dict):
        raise ValueError("extraction-audit output is not a JSON object")
    tables_out = []
    for row in data.get("tables") or []:
        if not isinstance(row, dict):
            continue
        idx = row.get("table_idx")
        if not isinstance(idx, int) or idx not in valid_idx:
            continue
        verdict = str(row.get("verdict") or "").strip().upper()
        if verdict not in _VERDICTS:
            verdict = "UNSURE"
        issues = [str(x).strip()[:200] for x in (row.get("issues") or [])
                 if str(x).strip()][:8]
        tables_out.append({"table_idx": idx, "verdict": verdict, "issues": issues})
    return {"tables": tables_out, "title_ok": bool(data.get("title_ok", True)),
            "notes": str(data.get("notes") or "").strip()[:1000]}


def seal(cfg: Config, pid: str, raw_json_text: str) -> dict:
    """Validate `raw_json_text` and seal it to `paper/extraction_audit.json`, sealed
    SESSION_SUBAGENT — this module never spawns its own CLI process."""
    payload = _clean(cfg, pid, raw_json_text)
    out, _sidecar = _paths(cfg, pid)
    return sealing.seal(out, payload, mode="SESSION_SUBAGENT", reviewer=REVIEWER,
                        tool_policy="unrecorded",
                        extra={"paper_id": pid, "tables_checked": len(payload["tables"])})


def load(cfg: Config, pid: str) -> dict | None:
    """The sealed audit payload, or None if it was never sealed or was tampered with."""
    out, _sidecar = _paths(cfg, pid)
    ok, _why = sealing.verify_seal(out, accepted_writers=(WRITTEN_BY,))
    if not ok:
        return None
    try:
        return state.read_json(out)
    except Exception:
        return None


def observations(cfg: Config, pid: str) -> list[dict]:
    """MISMATCH/UNSURE verdicts as plain dicts, `DocumentObservation`-SHAPED but not the
    type itself; `report` converts them next to `observe()`'s own observations.

    Why plain dicts: `check` values here ("VISION_TABLE_MISMATCH", "VISION_TABLE_UNSURE",
    "VISION_TITLE_MISMATCH") are not members of `schema.INTEGRITY_CHECKS`, and this
    module's ownership does not extend to widening that closed vocabulary or to
    `report.py`. Wiring this in needs: (1) `schema.py` — add the three strings above to
    `INTEGRITY_CHECKS`, so `report.observe()`'s per-check grouping stays consistent with
    what actually exists; (2) `report.py`'s `assemble_report`, right after it sets
    `report.document_observations` — append `DocumentObservation(**o) for o in
    extraction_audit.observations(cfg, pid)` there.

    NEVER a finding, NEVER a severity, NEVER anything that reaches `claim_status` or
    `disposition` — a flag that the PARSE may be wrong, nothing about the paper's science.
    """
    data = load(cfg, pid)
    if not data:
        return []
    try:
        doc = _doc(cfg, pid)
    except Exception:
        return []
    by_idx = {t.table_idx: t for t in doc.tables}
    out: list[dict] = []
    for row in data.get("tables") or []:
        verdict = row.get("verdict")
        if verdict not in ("MISMATCH", "UNSURE"):
            continue
        idx = row.get("table_idx")
        table = by_idx.get(idx)
        issues = "; ".join(row.get("issues") or []) or "(no detail given)"
        out.append({
            "about": "EXTRACTION", "check": f"VISION_TABLE_{verdict}",
            "ref": f"T{idx}", "quote": "", "page": table.page if table else 0,
            "detail": (f"extraction-audit (vision) flagged table {idx} as {verdict} "
                      f"against the rendered page image: {issues}"),
        })
    if data.get("title_ok") is False:
        out.append({
            "about": "EXTRACTION", "check": "VISION_TITLE_MISMATCH", "ref": "",
            "quote": doc.title, "page": 1,
            "detail": (f"extraction-audit (vision) flagged the parsed title against the "
                      f"page-1 image: {data.get('notes') or '(no detail given)'}"),
        })
    return out


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    from .schema import Table

    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td) / "projects")
        state.create_project(cfg, "", "T", pid="p")
        root = state.project_dir(cfg, "p")
        doc = PaperDoc(paper_id="p", title="A Paper", n_pages=1, tables=[
            Table(table_idx=0, page=2, header=["a", "b"], rows=[["1", "2"], ["3", "4"]]),
            Table(table_idx=1, page=3, header=["x"], rows=[["1"]]),   # 1 row: not qualifying
        ])
        (root / "paper").mkdir(parents=True, exist_ok=True)
        state.write_json(root / "paper" / "doc.json", doc.model_dump())

        assert [t.table_idx for t in _qualifying_tables(doc)] == [0]

        # seal() validates: an unknown table_idx is dropped, an unrecognised verdict is
        # coerced to UNSURE, unknown top-level keys are ignored.
        raw = json.dumps({
            "tables": [
                {"table_idx": 0, "verdict": "MISMATCH", "issues": ["row 2 reads 4 not 40"]},
                {"table_idx": 1, "verdict": "MATCH"},          # not a qualifying table
                {"table_idx": 9, "verdict": "MATCH"},          # does not exist at all
                {"table_idx": 0, "verdict": "bogus"},          # kept, coerced to UNSURE
            ],
            "title_ok": False, "notes": "page 1 reads a different subtitle",
            "invented_field": "ignored",
        })
        rec = seal(cfg, "p", raw)
        assert rec["written_by"] == "session_subagent"
        assert rec["delegation_mode"] == "SESSION_SUBAGENT"
        assert rec["reviewer"] == REVIEWER

        loaded = load(cfg, "p")
        assert loaded is not None and len(loaded["tables"]) == 2, loaded
        assert loaded["tables"][0] == {
            "table_idx": 0, "verdict": "MISMATCH", "issues": ["row 2 reads 4 not 40"]}
        assert loaded["tables"][1]["verdict"] == "UNSURE"
        assert loaded["title_ok"] is False

        obs = observations(cfg, "p")
        checks = {o["check"] for o in obs}
        assert checks == {"VISION_TABLE_MISMATCH", "VISION_TABLE_UNSURE",
                          "VISION_TITLE_MISMATCH"}, checks
        assert all(o["about"] == "EXTRACTION" for o in obs)
        assert any(o["check"] == "VISION_TABLE_MISMATCH" and o["ref"] == "T0" for o in obs)

        # A file edited after sealing is not the file that was sealed.
        out, _sidecar = _paths(cfg, "p")
        out.write_text(out.read_text(encoding="utf-8").replace('"title_ok": false',
                                                                '"title_ok": true'),
                       encoding="utf-8")
        assert load(cfg, "p") is None
        assert observations(cfg, "p") == []

        # build_prompt: no source PDF on disk, so images are skipped but the file is
        # still written with the qualifying table's markdown inline.
        path = build_prompt(cfg, "p")
        assert path.exists() and path.name == "extraction_audit.md"
        body = path.read_text(encoding="utf-8")
        assert "Table 0 (page 2)" in body and "| a | b |" in body
        assert "Table 1" not in body, "the 1-row table must not qualify"

    print("harness.extraction_audit self-check ok")


if __name__ == "__main__":
    _self_check()
