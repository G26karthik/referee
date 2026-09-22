"""Consolidate finished reviews: the executive dossier, and system-wide evaluation metrics.

Reads ONLY the persisted `reports/<pid>.json` / `.ledger.json` artifacts S4 already wrote
— re-derives and re-judges nothing, so this module depends only on the stable JSON shape
`schema.EvalReport`/`CaseLedger` define, never on `report.py`'s internals.

`python -m harness.summarize` runs the self-check.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import provenance as provenance_mod
from . import state
from .config import Config
from .schema import PAPER_DISPOSITIONS, CaseLedger, EvalReport

# ========================================================================================
# DOSSIER: two outputs from the same content — a markdown dossier (canonical, diffable) and a best-
# effort PDF rendered with PyMuPDF's Story API.
# ========================================================================================
SEVERITIES = ("FATAL", "MAJOR", "MINOR")
VENUES = {"acl": "ACL", "iclr": "ICLR", "cvpr": "CVPR", "neurips": "NeurIPS",
         "emnlp": "EMNLP", "icml": "ICML"}
CRITICAL_LIMIT = 6

def disposition_heading(report: dict) -> str:
    """The heading a per-paper section is headed with: the disposition, plain — never a
    colour or accept/reject score. Falls closed to an explicit STALE marker rather than
    passing an unrecognised string through."""
    d = (report.get("disposition") or "").strip()
    return f"`{d}`" if d else "`NOT_REVIEWED` — STALE, re-run this paper"

def venue_of(pid: str) -> str:
    return VENUES.get(pid.split("-")[0].lower(), "")

def load(cfg: Config, pid: str) -> dict | None:
    path = state.project_dir(cfg, pid) / "reports" / f"{pid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def counted_severity(f: dict) -> str:
    """The severity `report.counted()` actually counted — falls back to the lens's own
    `severity` when `counted_severity` is unset, mirroring that function exactly rather
    than re-deriving it (this module copies and never classifies)."""
    return f.get("counted_severity") or f.get("severity") or ""

def counts(report: dict) -> dict[str, int]:
    findings = report.get("findings") or []
    return {s: sum(1 for f in findings if counted_severity(f) == s) for s in SEVERITIES}

def probe_status(report: dict) -> str:
    """`verdict` alone is not enough: 'detectable' from a synthesized probe and from the
    authors' own checkout are different claims, and provenance is what separates them."""
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
    """The standing caveat for a probe not entitled to a reproduction verdict — provenance
    alone says WHOSE code would be entitled to one, never whether a process actually ran
    and reached one."""
    p = report.get("probe") or {}
    prov = p.get("provenance")
    rec_status = ((p.get("reconciliation") or {}) or {}).get("status")
    executed = bool(p.get("seeds_run")) or int(p.get("executions") or 0) > 0
    if not executed:
        return ("⚠️ A probe/reimplementation route was prepared but no process "
                "executed. It produced no measurement and cannot support a conclusion "
                "about the paper.")
    resolved = rec_status in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION")
    if provenance_mod.admits(prov) and executed and resolved:
        whose = ("the authors' own checkout (AUTHOR_REPOSITORY)" if prov == "repo_exec"
                else "an INDEPENDENT_REIMPLEMENTATION of the paper's method — not "
                     "the authors' code")
        return (f"This probe ran {whose}, so its reconciliation against "
                f"the cited cell is a real reproduction verdict.")
    if provenance_mod.admits(prov):
        why = f"reconciliation `{rec_status or 'not recorded'}`"
        return (f"⚠️ This probe's provenance ({prov}) would admit a real "
                f"reproduction verdict, but none was reached ({why}). Nothing about the "
                f"paper's own code follows from this.")
    if prov == "synthesized":
        if p.get("mechanism") == "placebo":
            return ("⚠️ This probe is a generic, paper-independent placebo "
                    "control — it was NOT derived from this paper's formulation. It "
                    "measures how much an auxiliary term with no hypothesis moves the "
                    "metric, and it may neither convict nor acquit a printed cell, and it "
                    "cannot drive this paper's verdict.")
        return ("⚠️ This probe was written by the harness from the paper's own "
                "published formulation and run at toy scale. It is evidence about the "
                "MECHANISM, not about the paper's tables.")
    return ("⚠️ This probe used the identical-arms template, which measures "
            "this machine's seed-noise floor and nothing about the paper. It reproduces "
            "no claim.")

def category_counts(report: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for sf in report.get("scientific_findings") or []:
        k = sf.get("scientific_class") or ""
        if k:
            out[k] = out.get(k, 0) + 1
    return out

def resolution_counts(report: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for sf in report.get("scientific_findings") or []:
        k = sf.get("resolution_status") or "NOT_INVESTIGATED"
        out[k] = out.get(k, 0) + 1
    return out

def _abbrev(counter: dict[str, int]) -> str:
    if not counter:
        return "—"
    return ", ".join(f"{n} {k.replace('_', ' ').lower()}"
                     for k, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))

def matrix(reports: list[dict]) -> list[list[str]]:
    """The multi-paper evaluation matrix. Leads with SCIENTIFIC CATEGORIES — what the
    review produces — with disposition as a routing column, not a colour."""
    rows = [["Paper", "Venue", "Findings by category", "Settled", "Repo / Code",
            "Probe Status", "Disposition"]]
    for r in reports:
        res = resolution_counts(r)
        settled = sum(n for k, n in res.items() if k.startswith("RESOLVED"))
        rows.append([
            f"`{r['paper_id']}`", venue_of(r["paper_id"]) or "—",
            _abbrev(category_counts(r)),
            f"{settled}/{sum(res.values())}" if res else "—",
            repo_status(r), probe_status(r),
            disposition_heading(r)])
    return rows

def _md_table(rows: list[list[str]]) -> list[str]:
    head, body = rows[0], rows[1:]
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in body]
    return out

def _finding_lines(report: dict) -> list[str]:
    critical = [f for f in (report.get("findings") or [])
               if counted_severity(f) in ("FATAL", "MAJOR")]
    if not critical:
        return ["No FATAL or MAJOR findings were raised.", ""]
    out = []
    for f in critical[:CRITICAL_LIMIT]:
        ref = f.get("evidence_ref") or "—"
        sev = counted_severity(f)
        sev_label = f"{sev} (lens asserted {f.get('severity')})" if f.get("counted_severity") else sev
        out += [f"- **{sev_label} · {f.get('lens')}** — {f.get('title')}",
               f"  {f.get('statement')}",
               f"  Evidence `{ref}`: “{f.get('evidence_quote')}”", ""]
    if len(critical) > CRITICAL_LIMIT:
        out.append(f"…and {len(critical) - CRITICAL_LIMIT} further FATAL/MAJOR "
                   f"finding(s); the complete list is in `projects/{report['paper_id']}/"
                   f"reports/{report['paper_id']}.md`.")
        out.append("")
    return out

def render_markdown(reports: list[dict], missing: list[str]) -> str:
    L = ["# Executive Review Dossier", "",
        f"{len(reports)} paper(s) reviewed by single-harness. Every disposition below is "
        "the deterministic output of the materiality table and `decide.derive_disposition` "
        "in `report.py`/`decide.py`; no model call decides one. STOP_MATERIAL_FAILURE means "
        "a material failure was ESTABLISHED; the PASS_TO_HUMAN_* dispositions mean one was "
        "not within the audited scope, which is not a certificate of correctness. Every "
        "quoted line was re-verified against the parsed PDF at report time.", ""]
    if missing:
        L += [f"**Not included ({len(missing)}):** " + ", ".join(f"`{m}`" for m in missing)
             + " — no finished report on disk.", ""]
    L += ["## Evaluation matrix", ""] + _md_table(matrix(reports)) + [""]
    total = {s: sum(counts(r)[s] for r in reports) for s in SEVERITIES}
    dropped = sum(r.get("dropped_findings", 0) for r in reports)
    L += [f"Corpus totals: **{total['FATAL']} FATAL**, **{total['MAJOR']} MAJOR**, "
          f"**{total['MINOR']} MINOR** across {len(reports)} paper(s); "
          f"**{dropped}** finding(s) dropped as unsubstantiated.", "", "---", ""]
    for r in reports:
        c = counts(r)
        L += [f"## {disposition_heading(r)} — `{r['paper_id']}`", "",
             f"**{r.get('title') or r['paper_id']}**", "",
             f"> {r.get('disposition_reason', '')}", "",
             f"{r.get('n_pages', 0)} pages · {r.get('n_sections', 0)} sections · "
             f"{r.get('n_tables', 0)} tables · {r.get('n_numbers', 0)} reported numbers · "
             f"lenses: {', '.join(r.get('lenses_run') or []) or '(none)'}", "",
             f"Findings by category: {_abbrev(category_counts(r))}", "",
             f"Resolution: {_abbrev(resolution_counts(r))}", "",
             f"Severity: {c['FATAL']} FATAL / {c['MAJOR']} MAJOR / {c['MINOR']} MINOR"
             + (f" · {r.get('dropped_findings')} dropped as unsubstantiated"
                if r.get("dropped_findings") else " · 0 dropped"), "",
             "### Critical validity threats", ""]
        L += _finding_lines(r)
        L += ["### The unasked obvious question", "",
             r.get("unasked_question") or "_(none recorded)_", "",
             "### Hardware calibration", "", calibration_line(r), "",
             provenance_caveat(r), "", "---", ""]
    return "\n".join(L)

_INLINE = [(re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
          (re.compile(r"\*\*([^*]+)\*\*"), r"<b>\1</b>")]
_PDF_GLYPHS = {"⚠️": "(!)", "⚠": "(!)", "“": '"', "”": '"',
              "’": "'", "—": "-", "σ": "sigma", "Δ": "delta",
              "±": "+/-", "…": "..."}

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
    """A deliberately small renderer for the subset of markdown `render_markdown` emits —
    not a general implementation, which keeps the dossier free of a markdown dependency."""
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
            if not all(set(c) <= set("-: ") for c in cells):
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
    css = ("body{font-family:sans-serif;font-size:9.5pt;line-height:1.45}"
           "h1{font-size:19pt;margin:0 0 6pt 0}h2{font-size:13pt;margin:14pt 0 4pt 0}"
           "h3{font-size:10.5pt;margin:10pt 0 3pt 0}p{margin:0 0 5pt 0}"
           "p.quote{font-style:italic;margin-left:10pt}p.item{margin-left:8pt}"
           "p.sub{margin-left:18pt}code{font-family:monospace;font-size:9pt}"
           "table{width:100%}th{text-align:left;font-weight:bold;font-size:8.5pt}"
           "td{font-size:8.5pt}")
    return f"<html><head><style>{css}</style></head><body>{''.join(body)}</body></html>"

def write_pdf(md: str, out: Path) -> str:
    """Render with PyMuPDF's Story. Returns '' on success, else why not — a rendering
    failure must never destroy the markdown that already succeeded."""
    try:
        import pymupdf
    except ImportError as e:
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
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return ""

def build_dossier(cfg: Config, pids: list[str], out_dir: Path | None = None,
                  stem: str = "Executive_Review_Dossier") -> dict:
    """Write the dossier for `pids`. Papers without a finished report are listed, not
    faked, and an EMPTY dossier is refused outright: a batch that stopped early must not
    silently replace a prior finished dossier with 'no findings'."""
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
        return {"papers": [], "missing": missing, "markdown": None, "pdf": None,
               "pdf_error": None, "totals": {s: 0 for s in SEVERITIES}, "dropped": 0,
               "skipped": f"no finished report among {len(missing)} paper(s), so no "
                          f"dossier was written; any existing dossier is left as it was"}

    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(reports, missing)
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(md, encoding="utf-8")
    pdf_path = out_dir / f"{stem}.pdf"
    pdf_error = write_pdf(md, pdf_path)
    return {"papers": [r["paper_id"] for r in reports], "missing": missing,
           "markdown": str(md_path), "pdf": None if pdf_error else str(pdf_path),
           "pdf_error": pdf_error or None, "skipped": "",
           "totals": {s: sum(counts(r)[s] for r in reports) for s in SEVERITIES},
           "dropped": sum(r.get("dropped_findings", 0) for r in reports)}

# ========================================================================================
# EVALUATION: system metrics over a reviewed corpus, counted from artifacts, never asserted.
# No adjudicated ground truth exists for this corpus, so accuracy/recall/agreement-with-humans are
# absent here rather than estimated.
# ========================================================================================
LIMITATIONS = [
    "No paper in this corpus carries adjudicated ground truth for its findings, so "
    "precision, recall and agreement with human reviewers are not computed.",
    "The corpus is small and was assembled opportunistically; none of these rates should "
    "be read as an estimate of behaviour on a different distribution of papers.",
    "Execution gates are shut by default, so execution-derived rates describe how often "
    "the system JUDGED an experiment necessary, not how often one ran.",
    "`funnel.warranting_experiment` is a judgement and `funnel.launched` is a count of "
    "processes actually started. Neither substitutes for the other.",
    "`probe_stage_seconds` is the wall time of the probe stage — acquisition, static "
    "audit, planning and gate evaluation. It is not the cost of running experiments.",
    "`findings_by_scientific_class` falls back to WHICH LENS raised a finding when it "
    "classified neither its discrepancy nor its missing baseline.",
    "`severity` is the lens's own assertion wherever grading did not run.",
    "`objects_addressable_rate` is SELF-REFERENTIAL and is not a coverage measure — "
    "both terms are this harness's own object list, so a worse extractor scores higher. "
    "The measure with an external denominator is `review_surface_coverage`.",
    "`review_surface_coverage` is STRUCTURAL coverage and is not issue recall; "
    "`semantic_coverage` reads `not_machine_detectable` unconditionally.",
    "`prose_presented_fraction` is the ceiling on any recall claim: the lens prompt "
    "divides a character budget across sections, so a long paper is read truncated.",
    "`document_observations` are mechanically determined and are NOT findings; they gate "
    "nothing and count toward no threshold.",
]

def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None

def _pct(value) -> str:
    return "not computable" if value is None else f"{value:.0%}"

def _surface_totals(papers: list[dict]) -> dict:
    """Corpus coverage from the SUMS, never averaged per-paper rates — averaging weights
    a two-table paper the same as a forty-table one."""
    rows: list[dict] = [x["review_surface_coverage"] for x in papers
                       if x.get("review_surface_coverage")]
    if not rows:
        return {"papers_measured": 0, "surface_size": 0, "addressed": 0, "examined": 0,
                "addressed_rate": None, "examined_rate": None,
                "semantic_coverage": "not_machine_detectable"}
    size = sum(int(r.get("surface_size") or 0) for r in rows)
    addressed = sum(int(r.get("addressed") or 0) for r in rows)
    examined = sum(int(r.get("examined") or 0) for r in rows)
    fractions: list[float] = [r["prose_presented_fraction"] for r in rows
                              if r.get("prose_presented_fraction") is not None]
    return {"papers_measured": len(rows), "surface_size": size, "addressed": addressed,
            "examined": examined, "addressed_rate": _ratio(addressed, size),
            "examined_rate": _ratio(examined, size),
            "off_surface": sum(int(r.get("off_surface") or 0) for r in rows),
            "prose_presented_fraction_min": min(fractions) if fractions else None,
            "prose_presented_fraction_max": max(fractions) if fractions else None,
            "semantic_coverage": "not_machine_detectable"}

def merge_states(papers: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in papers:
        v = x.get(key) or ""
        if v:
            out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

def assert_conservation(summary: dict) -> list[str]:
    """Every requested paper counted exactly once, no total exceeding its own term.
    Returns violations rather than raising, so a broken accounting appears IN the
    artifact."""
    bad: list[str] = []
    measured = int(summary.get("papers_measured") or 0)
    for field in ("triage", "binary_verdict", "review_paths"):
        counted = sum(int(v) for v in (summary.get(field) or {}).values())
        if counted != measured:
            bad.append(f"{field} counts {counted} paper(s) against papers_measured={measured}")
    if measured + len(summary.get("papers_without_a_report") or []) \
           != int(summary.get("papers_requested") or 0):
        bad.append("papers_measured + papers_without_a_report != papers_requested")
    f = summary.get("funnel") or {}
    # THE CHAIN IS FIVE TERMS, NOT SIX: discovered>=checkable>=warranting>=launched>=
    # completed is a real nesting; `resolved` is NOT a subset of `completed` (a question
    # settled from the paper's own arithmetic is resolved with nothing launched at all).
    chain = ("discovered", "checkable", "warranting_experiment", "launched", "completed")
    for a, b in zip(chain, chain[1:]):
        if int(f.get(b) or 0) > int(f.get(a) or 0):
            bad.append(f"funnel term {b}={f.get(b)} exceeds {a}={f.get(a)}")
    if int(f.get("resolved") or 0) > int(f.get("discovered") or 0):
        bad.append(f"funnel term resolved={f.get('resolved')} exceeds discovered={f.get('discovered')}")
    cov = summary.get("review_surface_coverage") or {}
    if int(cov.get("examined") or 0) > int(cov.get("addressed") or 0):
        bad.append("coverage: examined exceeds addressed")
    if int(cov.get("addressed") or 0) > int(cov.get("surface_size") or 0):
        bad.append("coverage: addressed exceeds the surface it is measured against")
    rex = summary.get("route_exhaustion") or {}
    classified = (int(rex.get("exhausted") or 0) + int(rex.get("open_because_untried") or 0)
                 + int(rex.get("open_because_of_this_harness") or 0))
    if classified != int(rex.get("questions") or 0):
        bad.append("route exhaustion: question states do not conserve the denominator")
    if int(rex.get("routes_exhausted") or 0) > int(rex.get("routes_applicable") or 0):
        bad.append("route exhaustion: exhausted routes exceed applicable routes")
    return bad

def per_paper(cfg: Config, pid: str) -> dict | None:
    root = cfg.projects_dir / pid / "reports"
    report_path = root / f"{pid}.json"
    if not report_path.exists():
        return None
    report = EvalReport(**json.loads(report_path.read_text(encoding="utf-8")))
    ledger_path = root / f"{pid}.ledger.json"
    led = (CaseLedger(**json.loads(ledger_path.read_text(encoding="utf-8")))
          if ledger_path.exists() else CaseLedger(paper_id=pid))
    from . import discover as discover_stage
    ts = discover_stage.load(cfg, pid)
    eff = led.efficiency or {}

    review_md, machine_md = root / f"{pid}.review.md", root / f"{pid}.md"
    review_chars = len(review_md.read_text(encoding="utf-8")) if review_md.exists() else 0
    machine_chars = len(machine_md.read_text(encoding="utf-8")) if machine_md.exists() else 0
    ledger_chars = len(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else 0

    kept, dropped = len(report.findings), report.dropped_findings
    discovered = eff.get("targets_discovered", 0)

    violations = 0
    for o in (ts.outcomes if ts else []):
        rec = o.reconciliation
        if rec and rec.status in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION") \
               and not provenance_mod.admits(rec.provenance):
            violations += 1

    return {
        "paper_id": pid, "disposition": report.disposition,
        "reproduction_status": report.reproduction_status,
        "lenses_run": len(report.lenses_run), "findings_kept": kept,
        "findings_dropped_unsubstantiated": dropped,
        "finding_verification_rate": _ratio(kept, kept + dropped),
        "questions_generated": eff.get("questions_generated", 0),
        "targets_discovered": discovered,
        "targets_addressable": eff.get("targets_addressable", 0),
        "objects_addressable_rate": _ratio(eff.get("targets_addressable", 0), discovered),
        "targets_requiring_execution": eff.get("targets_requiring_execution", 0),
        "execution_trigger_rate": _ratio(eff.get("targets_requiring_execution", 0), discovered),
        "targets_resolved_without_execution": eff.get("targets_resolved_without_execution", 0),
        "targets_blocked_before_execution": eff.get("targets_blocked_before_execution", 0),
        "targets_deferred_by_budget": eff.get("targets_deferred_by_budget", 0),
        "targets_settled": eff.get("targets_settled", 0),
        "targets_citation_verified_only": eff.get("targets_citation_verified_only", 0),
        "route_exhaustion": eff.get("route_exhaustion", {
            "questions": 0, "exhausted": 0, "open_because_untried": 0,
            "open_because_of_this_harness": 0, "rate": None, "routes_applicable": 0,
            "routes_attempted": 0, "routes_completed": 0, "routes_exhausted": 0}),
        "review_surface_coverage": ({
            "surface_size": report.coverage.surface_size, "addressed": report.coverage.addressed,
            "examined": report.coverage.examined, "addressed_rate": report.coverage.addressed_rate,
            "examined_rate": report.coverage.examined_rate,
            "off_surface": len(report.coverage.off_surface),
            "prose_presented_fraction": report.coverage.prose_presented_fraction,
            "semantic_coverage": report.coverage.semantic_coverage,
        } if report.coverage is not None else None),
        "review_path": report.review_path, "artifact_state": report.artifact_state,
        "document_observations": {
            "about_paper": sum(1 for o in (report.document_observations or []) if o.about == "PAPER"),
            "about_extraction": sum(1 for o in (report.document_observations or [])
                                    if o.about == "EXTRACTION")},
        "guarantees_unmet": (list(report.guarantees.unmet) if report.guarantees else []),
        "funnel": {"discovered": discovered, "checkable": eff.get("targets_addressable", 0),
                  "warranting_experiment": eff.get("targets_warranting_experiment", 0),
                  "launched": eff.get("targets_launched", 0),
                  "completed": eff.get("executions_completed", 0),
                  "resolved": eff.get("targets_resolved", 0)},
        "processes_launched": eff.get("processes_launched", 0),
        "executions_completed": eff.get("executions_completed", 0),
        "findings_by_scientific_class": eff.get("findings_by_scientific_class", {}),
        "targets_by_resolution_state": eff.get("targets_by_resolution_state", {}),
        "targets_by_evidence_state": eff.get("targets_by_evidence_state", {}),
        "targets_by_experiment_necessity": eff.get("targets_by_experiment_necessity", {}),
        "questions_open": eff.get("questions_open", 0),
        "blocked_by_gate": eff.get("blocked_by_gate", {}), "provenance_violations": violations,
        "probe_stage_seconds": eff.get("probe_stage_seconds", 0.0),
        "reviewer_report_chars": review_chars, "machine_report_chars": machine_chars,
        "ledger_chars": ledger_chars,
        "compaction_ratio": _ratio(review_chars, machine_chars + ledger_chars),
        "targets_summary": report.targets_summary,
    }

def corpus(cfg: Config, pids: list[str]) -> dict:
    papers = [p for p in (per_paper(cfg, pid) for pid in pids) if p is not None]
    missing = [pid for pid in pids if per_paper(cfg, pid) is None]

    def total(key: str) -> int:
        return sum(int(p.get(key) or 0) for p in papers)

    gates: dict[str, int] = {}
    for p in papers:
        for gate, n in (p.get("blocked_by_gate") or {}).items():
            gates[gate] = gates.get(gate, 0) + int(n)

    def merge(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in papers:
            for k, n in (p.get(key) or {}).items():
                out[k] = out.get(k, 0) + int(n)
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    funnel = {k: sum(int((p.get("funnel") or {}).get(k) or 0) for p in papers)
             for k in ("discovered", "checkable", "warranting_experiment", "launched",
                       "completed", "resolved")}
    kept, dropped = total("findings_kept"), total("findings_dropped_unsubstantiated")
    discovered = total("targets_discovered")
    route_fields = ("questions", "exhausted", "open_because_untried",
                    "open_because_of_this_harness", "routes_applicable", "routes_attempted",
                    "routes_completed", "routes_exhausted")
    route_exhaustion: dict[str, object] = {
        key: sum(int((p.get("route_exhaustion") or {}).get(key) or 0) for p in papers)
        for key in route_fields}
    route_exhaustion["rate"] = _ratio(int(route_exhaustion["exhausted"]),  # type: ignore[arg-type]
                                      int(route_exhaustion["questions"]))  # type: ignore[arg-type]
    route_exhaustion["note"] = ("question-weighted corpus rate; gates, budgets and "
                                "set-level policies never count as exhausted")
    summary = {
        "papers_requested": len(pids), "papers_measured": len(papers),
        "papers_without_a_report": missing,
        "dispositions": {d: sum(1 for p in papers if p.get("disposition") == d)
                        for d in PAPER_DISPOSITIONS},
        "findings_kept": kept, "findings_dropped_unsubstantiated": dropped,
        "finding_verification_rate": _ratio(kept, kept + dropped),
        "questions_generated": total("questions_generated"), "targets_discovered": discovered,
        "targets_addressable": total("targets_addressable"),
        "objects_addressable_rate": _ratio(total("targets_addressable"), discovered),
        "targets_requiring_execution": total("targets_requiring_execution"),
        "execution_trigger_rate": _ratio(total("targets_requiring_execution"), discovered),
        "targets_resolved_without_execution": total("targets_resolved_without_execution"),
        "targets_blocked_before_execution": total("targets_blocked_before_execution"),
        "targets_settled": total("targets_settled"),
        "targets_citation_verified_only": total("targets_citation_verified_only"),
        "targets_deferred_by_budget": total("targets_deferred_by_budget"),
        "route_exhaustion": route_exhaustion,
        "review_surface_coverage": _surface_totals(papers),
        "review_paths": {k: sum(1 for x in papers if x.get("review_path") == k)
                        for k in ("PAPER_ONLY", "PAPER_AND_ARTIFACT")},
        "artifact_states": merge_states(papers, "artifact_state"),
        "document_observations": {
            "about_paper": sum((x.get("document_observations") or {}).get("about_paper", 0)
                              for x in papers),
            "about_extraction": sum((x.get("document_observations") or {}).get("about_extraction", 0)
                                    for x in papers)},
        "guarantees_unmet": sorted({g for x in papers for g in (x.get("guarantees_unmet") or [])}),
        "funnel": funnel, "processes_launched": total("processes_launched"),
        "executions_completed": total("executions_completed"),
        "probe_stage_seconds": round(sum(float(p.get("probe_stage_seconds") or 0.0)
                                         for p in papers), 3),
        "findings_by_scientific_class": merge("findings_by_scientific_class"),
        "targets_by_resolution_state": merge("targets_by_resolution_state"),
        "targets_by_evidence_state": merge("targets_by_evidence_state"),
        "targets_by_experiment_necessity": merge("targets_by_experiment_necessity"),
        "questions_open": total("questions_open"), "blocked_by_gate": gates,
        "provenance_violations": total("provenance_violations"),
        "reviewer_report_chars": total("reviewer_report_chars"),
        "machine_report_chars": total("machine_report_chars"),
        "ledger_chars": total("ledger_chars"),
        "compaction_ratio": _ratio(total("reviewer_report_chars"),
                                   total("machine_report_chars") + total("ledger_chars")),
        "limitations": LIMITATIONS, "per_paper": papers,
    }
    summary["conservation_violations"] = assert_conservation(summary)
    return summary

def render_evaluation(summary: dict) -> str:
    L = ["# System evaluation", "",
        f"{summary['papers_measured']} of {summary['papers_requested']} requested paper(s) "
        f"have a report and are measured below.", "",
        "| Paper | Questions | Targets | Addressable | Needed an experiment | "
        "Settled w/o execution | Blocked | Disposition |",
        "|---|---:|---:|---:|---:|---:|---:|---|"]
    for p in summary["per_paper"]:
        L.append(f"| `{p['paper_id']}` | {p['questions_generated']} | "
                 f"{p['targets_discovered']} | {p['targets_addressable']} | "
                 f"{p['targets_requiring_execution']} | "
                 f"{p['targets_resolved_without_execution']} | "
                 f"{p['targets_blocked_before_execution']} | {p.get('disposition') or '—'} |")
    f = summary.get("funnel") or {}
    L += ["", "## From discovery to a settled question", "",
         "Six terms, each read off a different artifact and none interchangeable.", "",
         "| Stage | Targets |", "|---|---:|",
         f"| discovered | {f.get('discovered', 0)} |",
         f"| structurally checkable | {f.get('checkable', 0)} |",
         f"| judged to warrant an experiment | {f.get('warranting_experiment', 0)} |",
         f"| targets with execution launched | {f.get('launched', 0)} |",
         f"| ran to a reconciliation | {f.get('completed', 0)} |",
         f"| settled the question about the paper | {f.get('resolved', 0)} |",
         f"| (underlying seed/process launches) | {summary.get('processes_launched', 0)} |"]
    rex = summary.get("route_exhaustion") or {}
    if rex.get("questions"):
        L += ["", "## Material-question route exhaustion", "",
             "| Term | Count |", "|---|---:|",
             f"| material questions | {rex.get('questions', 0)} |",
             f"| questions with every applicable route exhausted | {rex.get('exhausted', 0)} |",
             f"| open because a route was untried or inconclusive | {rex.get('open_because_untried', 0)} |",
             f"| open because of this harness | {rex.get('open_because_of_this_harness', 0)} |",
             f"| applicable / attempted / completed / exhausted routes | "
             f"{rex.get('routes_applicable', 0)} / {rex.get('routes_attempted', 0)} / "
             f"{rex.get('routes_completed', 0)} / {rex.get('routes_exhausted', 0)} |",
             f"| route-exhaustion coverage | {_pct(rex.get('rate'))} |"]
    cov = summary.get("review_surface_coverage") or {}
    if cov.get("surface_size"):
        L += ["", "## Review-surface coverage", "",
             f"Structural coverage over the paper's OWN addressable surface. Not issue "
             f"recall: `semantic_coverage` reads `{cov.get('semantic_coverage')}` "
             f"unconditionally.", "", "| Term | Units |", "|---|---:|",
             f"| addressable surface (the denominator) | {cov.get('surface_size', 0)} |",
             f"| an address was minted | {cov.get('addressed', 0)} ({_pct(cov.get('addressed_rate'))}) |",
             f"| a route was pursued | {cov.get('examined', 0)} ({_pct(cov.get('examined_rate'))}) |",
             f"| addresses outside the surface (a defect if non-zero) | {cov.get('off_surface', 0)} |"]
        lo, hi = cov.get("prose_presented_fraction_min"), cov.get("prose_presented_fraction_max")
        if lo is not None:
            L += ["", f"Lenses were shown between {lo:.0%} and {hi:.0%} of each paper's "
                      f"extracted section text — the ceiling on any recall claim."]
    paths = summary.get("review_paths") or {}
    if any(paths.values()):
        L += ["", "## Which review path each paper was on", "", "| Path | Papers |", "|---|---:|"]
        L += [f"| {k} | {v} |" for k, v in paths.items()]
    dobs = summary.get("document_observations") or {}
    if dobs.get("about_paper") or dobs.get("about_extraction"):
        L += ["", "## Document-integrity observations", "",
             f"{dobs.get('about_paper', 0)} about the papers and "
             f"{dobs.get('about_extraction', 0)} about this harness's own extraction. "
             f"Observations, not findings — none counts toward any threshold."]
    cats = summary.get("findings_by_scientific_class") or {}
    if cats:
        L += ["", "## Findings by scientific category", "", "| Category | Findings |", "|---|---:|"]
        L += [f"| {k} | {n} |" for k, n in cats.items()]
    res = summary.get("targets_by_resolution_state") or {}
    if res:
        L += ["", "## What became of the questions", "", "| Resolution | Targets |", "|---|---:|"]
        L += [f"| {k} | {n} |" for k, n in res.items()]
    L += ["", "## Aggregate", "",
         f"- finding verification rate: {summary['finding_verification_rate']} "
         f"({summary['findings_kept']} kept, {summary['findings_dropped_unsubstantiated']} dropped)",
         f"- objects this harness minted that were addressable: "
         f"{summary['objects_addressable_rate']} "
         f"({summary['targets_addressable']}/{summary['targets_discovered']}) - "
         f"self-referential, NOT coverage",
         f"- execution-trigger rate: {summary['execution_trigger_rate']}",
         f"- settled without execution: {summary['targets_resolved_without_execution']}",
         f"- provenance violations: {summary['provenance_violations']} (must be 0)",
         f"- conservation violations: {len(summary.get('conservation_violations') or []) or 0} (must be 0)"
         + ("".join(f" - {v}" for v in (summary.get('conservation_violations') or []))),
         f"- process guarantees unmet on at least one paper: "
         f"{', '.join(summary.get('guarantees_unmet') or []) or 'none'}",
         f"- reviewer report vs machine artifacts: {summary['reviewer_report_chars']} chars "
         f"against {summary['machine_report_chars'] + summary['ledger_chars']} "
         f"(ratio {summary['compaction_ratio']})", "", "## Not measured", ""]
    L += [f"- {x}" for x in summary["limitations"]]
    L.append("")
    return "\n".join(L)

def run_evaluate(cfg: Config, pids: list[str], out: Path | None = None) -> dict:
    summary = corpus(cfg, pids)
    out = out or (cfg.projects_dir.parent / "reports")
    out.mkdir(parents=True, exist_ok=True)
    (out / "system_evaluation.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "system_evaluation.md").write_text(render_evaluation(summary), encoding="utf-8")
    summary["paths"] = {"json": str(out / "system_evaluation.json"),
                        "md": str(out / "system_evaluation.md")}
    return summary

# --------------------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    fake = {
        "paper_id": "demo", "title": "A Demonstration", "disposition": "PASS_TO_HUMAN_CONCERNS",
        "disposition_reason": "one MAJOR finding", "n_pages": 8, "n_sections": 6,
        "n_tables": 2, "n_numbers": 11, "lenses_run": ["overclaim"], "dropped_findings": 0,
        "unasked_question": "Why was the obvious baseline not run?",
        "findings": [
            {"finding_id": "overclaim-01", "lens": "overclaim", "severity": "MAJOR",
            "title": "A headline gain sits inside the seed band",
            "statement": "The claimed gain is smaller than the reported spread.",
            "evidence_ref": "T1:r2:c3", "evidence_quote": "12.196 ± 0.207"},
            {"finding_id": "overclaim-02", "lens": "overclaim", "severity": "MINOR",
            "title": "A rounding slip", "statement": "0.033% is printed as 0.04%.",
            "evidence_ref": "p4", "evidence_quote": "rank AUC differs by 0.04%"}],
        "probe": {"verdict": "within_noise", "provenance": "synthesized", "device": "cuda",
                 "seeds_run": [0, 1, 2], "seeds_failed": [], "measured_std": 0.0048,
                 "noise_band": 0.0096, "measured_delta": 0.001, "claimed_delta": 0.02,
                 "claim_within_noise": False, "seconds": 42.0, "repo": {"status": "cloned"},
                 "code_audit": {"findings": [{"rule_id": "x"}]},
                 "reconciliation": {"status": "INCONCLUSIVE"}},
    }
    rows = matrix([fake])
    cols = {name: i for i, name in enumerate(rows[0])}
    assert rows[0][0] == "Paper" and len(rows) == 2
    assert rows[1][cols["Findings by category"]] == "—"
    assert rows[1][cols["Disposition"]] == "`PASS_TO_HUMAN_CONCERNS`"
    assert disposition_heading({"disposition": "STOP_MATERIAL_FAILURE"}) == "`STOP_MATERIAL_FAILURE`"
    assert "STALE" in disposition_heading({})

    md = render_markdown([fake], ["ghost"])
    assert "Executive Review Dossier" in md
    assert "`ghost`" in md
    assert "12.196" in md
    assert "evidence about the MECHANISM" in md
    doc_html = markdown_to_html(md)
    assert "<table>" in doc_html and "<th>" in doc_html
    assert "sigma" in doc_html

    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td) / "projects")
        (cfg.projects_dir / "p" / "reports").mkdir(parents=True)
        report = EvalReport(paper_id="p", title="t", disposition="PASS_TO_HUMAN_CONCERNS",
                            dropped_findings=3, findings=[], reproduction_status="NOT_ATTEMPTED")
        (cfg.projects_dir / "p" / "reports" / "p.json").write_text(
            json.dumps(report.model_dump()), encoding="utf-8")
        (cfg.projects_dir / "p" / "reports" / "p.ledger.json").write_text(
            json.dumps(CaseLedger(paper_id="p", efficiency={
                "targets_discovered": 10, "targets_addressable": 6,
                "targets_requiring_execution": 2, "questions_generated": 4,
                "targets_resolved_without_execution": 1,
                "targets_blocked_before_execution": 3}).model_dump()), encoding="utf-8")
        (cfg.projects_dir / "p" / "reports" / "p.review.md").write_text("short", encoding="utf-8")
        (cfg.projects_dir / "p" / "reports" / "p.md").write_text("x" * 100, encoding="utf-8")

        s = corpus(cfg, ["p", "absent"])
        assert s["papers_requested"] == 2 and s["papers_measured"] == 1
        assert s["papers_without_a_report"] == ["absent"]
        assert s["dispositions"]["PASS_TO_HUMAN_CONCERNS"] == 1
        assert s["dispositions"]["STOP_MATERIAL_FAILURE"] == 0
        assert s["objects_addressable_rate"] == 0.6
        assert s["provenance_violations"] == 0
        text = render_evaluation(s)
        assert "| `p` |" in text and "Not measured" in text

    print("harness.summarize self-check ok")

if __name__ == "__main__":
    _self_check()
