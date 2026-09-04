"""Consolidate finished per-paper reports into one executive dossier.

This module reads only `projects/<pid>/reports/<pid>.json` — the machine-readable
artifact S4 already wrote. It re-derives nothing and re-judges nothing: every verdict,
count, quote and probe number in the dossier is copied from a report that was produced
by the deterministic threshold table in `stages/report.py`. If a paper is not finished,
it does not appear; it is listed as missing instead, because a dossier that silently
omits a paper reads as a dossier of everything.

Two outputs from the same content:

  * `Executive_Review_Dossier.md`  — the canonical artifact, diffable and greppable.
  * `Executive_Review_Dossier.pdf` — rendered with PyMuPDF's Story API, which is
    already a dependency (the ingest stage parses PDFs with it). No pandoc, no
    headless browser, no new package. PDF generation is best-effort: if it fails the
    markdown is still written and the failure is reported rather than swallowed.

The per-paper page deliberately carries the probe's provenance caveat verbatim. A
`synthesized` or `template` probe cannot convict or acquit a printed cell (see the
provenance ceiling in `local_exec.reconcile`), and an executive summary is exactly the
document where that distinction is most likely to be lost.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import state
from .config import Config

BADGE = {"RED": "🔴 RED", "YELLOW": "🟡 YELLOW", "GREEN": "🟢 GREEN"}
SEVERITIES = ("FATAL", "MAJOR", "MINOR")
# Venue is not a field the extractor produces — a paper's own text rarely names its
# venue in a parseable place, and guessing from the title is how "Published as a
# conference paper at ICLR 2024" becomes a title. The case id is the operator's own
# label, so it is the honest source, and anything unrecognised is left blank.
VENUES = {"acl": "ACL", "iclr": "ICLR", "cvpr": "CVPR", "neurips": "NeurIPS",
          "emnlp": "EMNLP", "icml": "ICML"}
# How many findings the one-page breakdown shows before it stops. The full list is in
# the per-paper report; the dossier is a summary and says so where it truncates.
CRITICAL_LIMIT = 6


def venue_of(pid: str) -> str:
    return VENUES.get(pid.split("-")[0].lower(), "")


def load(cfg: Config, pid: str) -> dict | None:
    """The finished report for `pid`, or None when S4 has not run for it."""
    path = state.project_dir(cfg, pid) / "reports" / f"{pid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def counted_severity(f: dict) -> str:
    """The severity `stages.report.counted()` actually counted — falls back to the
    lens's own `severity` when `counted_severity` is unset, exactly like the report-time
    function this mirrors. Reading the raw field here (not re-deriving it) is what
    'reads only, re-derives nothing' (see module docstring) actually means in practice."""
    return f.get("counted_severity") or f.get("severity") or ""


def counts(report: dict) -> dict[str, int]:
    findings = report.get("findings") or []
    return {s: sum(1 for f in findings if counted_severity(f) == s) for s in SEVERITIES}


def probe_status(report: dict) -> str:
    """One cell describing what S3 actually did, with provenance attached.

    `verdict` alone is not enough: "detectable" from a synthesized probe and
    "detectable" from the authors' own checkout are different claims, and the
    provenance is what separates them.
    """
    p = report.get("probe") or {}
    if not p:
        return "not run"
    verdict = p.get("verdict") or "?"
    prov = p.get("provenance") or "template"
    rec = (p.get("reconciliation") or {}).get("status")
    bits = [f"{verdict} ({prov})"]
    if rec:
        bits.append(rec)
    return " · ".join(bits)


def repo_status(report: dict) -> str:
    p = report.get("probe") or {}
    repo = p.get("repo") or {}
    status = repo.get("status") or "not_attempted"
    audit = p.get("code_audit") or {}
    n = len(audit.get("findings") or [])
    return f"{status}" + (f", {n} static finding(s)" if n else "")


def calibration_line(report: dict) -> str:
    """The hardware numbers: device, seeds and the seed-noise band everything divides by."""
    p = report.get("probe") or {}
    if not p:
        return "no probe was run for this paper."
    seeds = p.get("seeds_run") or []
    failed = p.get("seeds_failed") or []
    bits = [f"device `{p.get('device', '?')}`", f"{len(seeds)} seed(s) completed"]
    if failed:
        bits.append(f"{len(failed)} seed(s) failed")
    if p.get("measured_std") is not None:
        bits.append(f"seed-noise σ {p['measured_std']:.4f}")
    if p.get("noise_band") is not None:
        bits.append(f"2σ band {p['noise_band']:.4f}")
    if p.get("measured_delta") is not None:
        bits.append(f"measured Δ {p['measured_delta']:+.4f}")
    if p.get("claimed_delta") is not None:
        bits.append(f"claimed Δ {p['claimed_delta']:+.4f}")
    if p.get("claim_within_noise") is not None:
        bits.append("claimed Δ sits INSIDE this machine's noise band"
                    if p["claim_within_noise"] else
                    "claimed Δ exceeds this machine's noise band")
    if p.get("seconds") is not None:
        bits.append(f"{p['seconds']:.0f}s wall clock")
    return ", ".join(bits) + "."


def provenance_caveat(report: dict) -> str:
    """The standing caveat for a probe that is not entitled to a reproduction verdict."""
    p = report.get("probe") or {}
    prov = p.get("provenance")
    rec_status = ((p.get("reconciliation") or {}) or {}).get("status")
    # Provenance alone says WHOSE code would be entitled to a real verdict; it says
    # nothing about whether a process actually ran and reached one. A `repo_exec`/`driver`
    # spec refused by `authorize` (verdict `blocked`, no seeds run) or one whose run ended
    # INCONCLUSIVE still had the right provenance — asserting "a real reproduction verdict"
    # for either would claim an execution, or a resolution, that did not happen.
    executed = bool(p.get("seeds_run"))
    resolved = rec_status in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION")
    if prov in ("repo_exec", "driver") and executed and resolved:
        whose = ("the authors' own checkout" if prov == "repo_exec"
                else "a hand-written reproduction of the paper's setup")
        return (f"This probe ran {whose}, so its reconciliation against "
                f"the cited cell is a real reproduction verdict.")
    if prov in ("repo_exec", "driver"):
        why = (f"verdict `{p.get('verdict') or '?'}`, no process executed" if not executed
              else f"reconciliation `{rec_status or 'not recorded'}`")
        return (f"⚠️ This probe's provenance ({prov}) would admit a real reproduction "
                f"verdict, but none was reached ({why}). Nothing about the paper's own "
                f"code follows from this.")
    if prov == "synthesized":
        if p.get("mechanism") == "placebo":
            # The placebo is the only synthesized template — probe_synth.plan has no
            # mechanism dispatch — and it is paper-independent BY CONSTRUCTION.
            return ("⚠️ This probe is a generic, paper-independent placebo control — it "
                    "was NOT derived from this paper's formulation. It measures how much "
                    "an auxiliary term with no hypothesis moves the metric, and it may "
                    "neither convict nor acquit a printed cell, and it cannot drive this "
                    "paper's verdict.")
        return ("⚠️ This probe was written by the harness from the paper's own published "
                "formulation and run at toy scale. It is evidence about the MECHANISM, "
                "not about the paper's tables — it may neither convict nor acquit a "
                "printed cell, and it cannot drive this paper's verdict.")
    return ("⚠️ This probe used the identical-arms template, which measures this machine's "
            "seed-noise floor and nothing about the paper. It reproduces no claim.")


def matrix(reports: list[dict]) -> list[list[str]]:
    """The multi-paper evaluation matrix, header row first."""
    rows = [["Paper", "Venue", "Validity Threats", "Repo / Code", "Probe Status", "Verdict"]]
    for r in reports:
        c = counts(r)
        rows.append([
            f"`{r['paper_id']}`",
            venue_of(r["paper_id"]) or "—",
            f"{c['FATAL']} FATAL / {c['MAJOR']} MAJOR / {c['MINOR']} MINOR",
            repo_status(r),
            probe_status(r),
            BADGE.get(r.get("verdict") or "", r.get("verdict") or "?"),
        ])
    return rows


def _md_table(rows: list[list[str]]) -> list[str]:
    head, body = rows[0], rows[1:]
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in body]
    return out


def _finding_lines(report: dict) -> list[str]:
    """FATAL and MAJOR findings, in the report's own ranked order, with their evidence.

    The report JSON already stores findings ranked most-severe-first, so this preserves
    that order rather than re-sorting and risking a different answer to "what is worst".
    """
    critical = [f for f in (report.get("findings") or [])
                if counted_severity(f) in ("FATAL", "MAJOR")]
    if not critical:
        return ["No FATAL or MAJOR findings were raised.", ""]

    out = []
    for f in critical[:CRITICAL_LIMIT]:
        ref = f.get("evidence_ref") or "—"
        sev = counted_severity(f)
        sev_label = f"{sev} (lens asserted {f.get('severity')})" if f.get("counted_severity") else sev
        out += [
            f"- **{sev_label} · {f.get('lens')}** — {f.get('title')}",
            f"  {f.get('statement')}",
            f"  Evidence `{ref}`: “{f.get('evidence_quote')}”",
            "",
        ]
    if len(critical) > CRITICAL_LIMIT:
        out.append(f"…and {len(critical) - CRITICAL_LIMIT} further FATAL/MAJOR finding(s); "
                   f"the complete list is in `projects/{report['paper_id']}/reports/"
                   f"{report['paper_id']}.md`.")
        out.append("")
    return out


def render_markdown(reports: list[dict], missing: list[str]) -> str:
    L = [
        "# Executive Review Dossier",
        "",
        f"{len(reports)} paper(s) reviewed by single-harness. Every verdict below is the "
        "deterministic output of the threshold table in `stages/report.py`; no model call "
        "decides a verdict. Every quoted line was re-verified against the parsed PDF at "
        "report time, and findings whose evidence did not check out were dropped before "
        "this document was written.",
        "",
    ]
    if missing:
        L += [
            f"**Not included ({len(missing)}):** " + ", ".join(f"`{m}`" for m in missing)
            + " — no finished report on disk. Run `python run.py review --paper <id>` "
              "for each before regenerating.",
            "",
        ]

    L += ["## Evaluation matrix", ""] + _md_table(matrix(reports)) + [""]

    total = {s: sum(counts(r)[s] for r in reports) for s in SEVERITIES}
    dropped = sum(r.get("dropped_findings", 0) for r in reports)
    L += [
        f"Corpus totals: **{total['FATAL']} FATAL**, **{total['MAJOR']} MAJOR**, "
        f"**{total['MINOR']} MINOR** across {len(reports)} paper(s); "
        f"**{dropped}** finding(s) dropped as unsubstantiated.",
        "",
        "---",
        "",
    ]

    for r in reports:
        c = counts(r)
        L += [
            f"## {BADGE.get(r.get('verdict',''), r.get('verdict','?'))} — `{r['paper_id']}`",
            "",
            f"**{r.get('title') or r['paper_id']}**",
            "",
            f"> {r.get('verdict_reason', '')}",
            "",
            f"{r.get('n_pages', 0)} pages · {r.get('n_sections', 0)} sections · "
            f"{r.get('n_tables', 0)} tables · {r.get('n_numbers', 0)} reported numbers · "
            f"lenses: {', '.join(r.get('lenses_run') or []) or '(none)'}",
            "",
            f"Findings: {c['FATAL']} FATAL / {c['MAJOR']} MAJOR / {c['MINOR']} MINOR"
            + (f" · {r.get('dropped_findings')} dropped as unsubstantiated"
               if r.get("dropped_findings") else " · 0 dropped"),
            "",
            "### Critical validity threats",
            "",
        ]
        L += _finding_lines(r)
        L += [
            "### The unasked obvious question",
            "",
            r.get("unasked_question") or "_(none recorded)_",
            "",
            "### Hardware calibration",
            "",
            calibration_line(r),
            "",
            provenance_caveat(r),
            "",
            "---",
            "",
        ]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# PDF rendering
# --------------------------------------------------------------------------- #
_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<b>\1</b>"),
]
# The base-14 PDF fonts have no emoji coverage, so a badge that reads fine in the
# markdown comes out as a tofu box in the PDF. Substituting on the way into the PDF keeps
# the markdown readable where emoji work and the PDF readable where they do not, rather
# than degrading both to the lowest common denominator.
_PDF_GLYPHS = {"🔴 ": "", "🟡 ": "", "🟢 ": "",
               "⚠️": "(!)", "⚠": "(!)", "“": '"', "”": '"', "’": "'", "—": "-",
               "σ": "sigma", "Δ": "delta", "±": "+/-", "…": "..."}


def _ascii_for_pdf(text: str) -> str:
    for bad, good in _PDF_GLYPHS.items():
        text = text.replace(bad, good)
    return text


def _inline_html(text: str) -> str:
    out = html.escape(_ascii_for_pdf(text))
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def markdown_to_html(md: str) -> str:
    """A deliberately small renderer for the subset of markdown this module emits.

    Not a general markdown implementation and not trying to be one: this function only
    ever sees `render_markdown`'s output, so supporting headings, tables, list items,
    blockquotes, rules and paragraphs is sufficient and keeps the dossier free of a
    markdown dependency the rest of the harness does not have.
    """
    body: list[str] = []
    table: list[list[str]] = []

    def flush_table() -> None:
        if not table:
            return
        head, rows = table[0], table[1:]
        cells = "".join(f"<th>{_inline_html(c)}</th>" for c in head)
        body.append(f"<table><tr>{cells}</tr>")
        for row in rows:
            cells = "".join(f"<td>{_inline_html(c)}</td>" for c in row)
            body.append(f"<tr>{cells}</tr>")
        body.append("</table>")
        table.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not all(set(c) <= set("-: ") for c in cells):     # skip the separator row
                table.append(cells)
            continue
        flush_table()

        if not line.strip():
            continue
        if line.startswith("### "):
            body.append(f"<h3>{_inline_html(line[4:])}</h3>")
        elif line.startswith("## "):
            body.append(f"<h2>{_inline_html(line[3:])}</h2>")
        elif line.startswith("# "):
            body.append(f"<h1>{_inline_html(line[2:])}</h1>")
        elif line.startswith("> "):
            body.append(f"<p class='quote'>{_inline_html(line[2:])}</p>")
        elif line.strip() == "---":
            body.append("<hr/>")
        elif line.startswith("- "):
            body.append(f"<p class='item'>{_inline_html(line[2:])}</p>")
        elif line.startswith("  "):
            body.append(f"<p class='sub'>{_inline_html(line.strip())}</p>")
        else:
            body.append(f"<p>{_inline_html(line)}</p>")
    flush_table()

    css = """
    body { font-family: sans-serif; font-size: 9.5pt; line-height: 1.45; }
    h1 { font-size: 19pt; margin: 0 0 6pt 0; }
    h2 { font-size: 13pt; margin: 14pt 0 4pt 0; }
    h3 { font-size: 10.5pt; margin: 10pt 0 3pt 0; }
    p  { margin: 0 0 5pt 0; }
    p.quote { font-style: italic; margin-left: 10pt; }
    p.item  { margin-left: 8pt; }
    p.sub   { margin-left: 18pt; }
    code { font-family: monospace; font-size: 9pt; }
    table { width: 100%; }
    th { text-align: left; font-weight: bold; font-size: 8.5pt; }
    td { font-size: 8.5pt; }
    """
    return f"<html><head><style>{css}</style></head><body>{''.join(body)}</body></html>"


def write_pdf(md: str, out: Path) -> str:
    """Render the dossier to PDF with PyMuPDF's Story. Returns '' on success, else why not."""
    try:
        import pymupdf
    except ImportError as e:                                    # pragma: no cover
        return f"PyMuPDF is not importable: {e}"
    try:
        page = pymupdf.paper_rect("a4")
        frame = page + (54, 54, -54, -54)
        story = pymupdf.Story(html=markdown_to_html(md))
        writer = pymupdf.DocumentWriter(str(out))
        more = True
        while more:
            device = writer.begin_page(page)
            more, _ = story.place(frame)
            story.draw(device)
            writer.end_page()
        writer.close()
    except Exception as e:                                      # pragma: no cover
        # Never let a rendering failure destroy the markdown that already succeeded.
        return f"{type(e).__name__}: {e}"
    return ""


def build(cfg: Config, pids: list[str], out_dir: Path | None = None,
          stem: str = "Executive_Review_Dossier") -> dict:
    """Write the dossier for `pids`. Papers without a finished report are listed, not faked."""
    reports: list[dict] = []
    missing: list[str] = []
    for pid in pids:
        r = load(cfg, pid)
        if r:
            reports.append(r)
        else:
            missing.append(pid)

    out_dir = out_dir or (cfg.projects_dir.parent / "reports")

    if not reports:
        # A dossier over nothing is not a small dossier; it is a document asserting that a
        # corpus was reviewed and found to contain no findings. Written to the fixed path
        # `reports/Executive_Review_Dossier.md`, it replaced the real one: a batch of two
        # papers that both stopped at S2 turned a finished three-paper dossier into
        # "0 paper(s) reviewed ... Corpus totals: 0 FATAL, 0 MAJOR, 0 MINOR". Nothing about
        # that output is true of the papers named in it, and nothing recoverable is gained
        # by writing it, so it is not written and whatever is on disk is left alone.
        #
        # Only the EMPTY case is refused. A one-paper dossier still overwrites a
        # three-paper one — that is the documented `--out` limitation and it is a
        # judgement about what an operator meant, not a correctness bug.
        return {
            "papers": [], "missing": missing, "markdown": None, "pdf": None,
            "pdf_error": None, "totals": {s: 0 for s in SEVERITIES}, "dropped": 0,
            "skipped": f"no finished report among {len(missing)} paper(s), so no dossier was "
                       f"written; any existing dossier is left as it was",
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(reports, missing)
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(md, encoding="utf-8")

    pdf_path = out_dir / f"{stem}.pdf"
    pdf_error = write_pdf(md, pdf_path)

    return {
        "papers": [r["paper_id"] for r in reports],
        "missing": missing,
        "markdown": str(md_path),
        "pdf": None if pdf_error else str(pdf_path),
        "pdf_error": pdf_error or None,
        "skipped": "",
        "totals": {s: sum(counts(r)[s] for r in reports) for s in SEVERITIES},
        "dropped": sum(r.get("dropped_findings", 0) for r in reports),
    }


if __name__ == "__main__":       # self-check: python -m harness.dossier
    import tempfile

    fake = {
        "paper_id": "demo", "title": "A Demonstration", "verdict": "YELLOW",
        "verdict_reason": "one MAJOR finding", "n_pages": 8, "n_sections": 6,
        "n_tables": 2, "n_numbers": 11, "lenses_run": ["overclaim"], "dropped_findings": 0,
        "unasked_question": "Why was the obvious baseline not run?",
        "findings": [
            {"finding_id": "overclaim-01", "lens": "overclaim", "severity": "MAJOR",
             "title": "A headline gain sits inside the seed band",
             "statement": "The claimed gain is smaller than the reported spread.",
             "evidence_ref": "T1:r2:c3", "evidence_quote": "12.196 ± 0.207"},
            {"finding_id": "overclaim-02", "lens": "overclaim", "severity": "MINOR",
             "title": "A rounding slip", "statement": "0.033% is printed as 0.04%.",
             "evidence_ref": "p4", "evidence_quote": "rank AUC differs by 0.04%"},
        ],
        "probe": {"verdict": "within_noise", "provenance": "synthesized", "device": "cuda",
                  "seeds_run": [0, 1, 2], "seeds_failed": [], "measured_std": 0.0048,
                  "noise_band": 0.0096, "measured_delta": 0.001, "claimed_delta": 0.02,
                  "claim_within_noise": False, "seconds": 42.0,
                  "repo": {"status": "cloned"},
                  "code_audit": {"findings": [{"rule_id": "x"}]},
                  "reconciliation": {"status": "INCONCLUSIVE"}},
    }

    rows = matrix([fake])
    assert rows[0][0] == "Paper" and len(rows) == 2
    assert rows[1][2] == "0 FATAL / 1 MAJOR / 1 MINOR", rows[1]
    assert rows[1][4] == "within_noise (synthesized) · INCONCLUSIVE", rows[1]
    assert rows[1][5] == "🟡 YELLOW"

    md = render_markdown([fake], ["ghost"])
    assert "Executive Review Dossier" in md
    assert "`ghost`" in md, "a paper with no finished report must be named, not omitted"
    assert "12.196 ± 0.207" in md, "evidence quotes are carried verbatim"
    assert "may neither convict nor acquit" in md, "synthesized probes carry their caveat"
    assert "Why was the obvious baseline not run?" in md
    assert "seed-noise σ 0.0048" in md and "2σ band 0.0096" in md
    assert "overclaim-02" not in md, "MINOR findings do not reach the one-page breakdown"

    doc_html = markdown_to_html(md)
    assert "<table>" in doc_html and "<th>" in doc_html
    assert "---" not in doc_html.replace("<hr/>", "")
    # The PDF fonts carry no emoji; badges must survive as readable text, not tofu.
    assert "🟡" not in doc_html, "no emoji may reach the PDF renderer"
    assert ">YELLOW - <" in doc_html.replace("</h2>", ""), "the badge word survives the strip"
    assert "sigma" in doc_html and "σ" not in doc_html
    assert "🟡 YELLOW" in md, "the markdown keeps the emoji badge"

    with tempfile.TemporaryDirectory() as td:
        err = write_pdf(md, Path(td) / "d.pdf")
        assert not err, err
        assert (Path(td) / "d.pdf").stat().st_size > 1000
        import pymupdf
        with pymupdf.open(Path(td) / "d.pdf") as pdf:
            assert pdf.page_count >= 1
            assert "Executive Review Dossier" in pdf[0].get_text()

    print(json.dumps({"self_check": "ok", "matrix_rows": len(rows),
                      "markdown_chars": len(md)}, indent=2))
