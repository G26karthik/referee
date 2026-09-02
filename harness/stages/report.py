"""S4 — rank the findings and render the evaluation report.

Two hard rules, both inherited from what the research pipeline learned:

  1. NO LLM DECIDES THE VERDICT. Severity ordering and the RED/YELLOW/GREEN call are
     a lexicographic sort and a threshold table in plain Python. A model that ranks
     its own findings will rank them differently on Tuesday; an editor needs the same
     paper to get the same verdict every time, and needs to be able to argue with the
     rule rather than with a mood.

  2. THE RENDERER COPIES, IT DOES NOT WRITE. `render_eval_report` is a pure function
     over the artifacts — no LLM, no network, no Config. Every number and every quote
     in the output was already in a `Finding`, put there by a lens that had to quote
     it from the paper. Nothing is generated at render time, so nothing can be
     hallucinated at render time.

`python -m harness.stages.report` runs the self-check.
"""
from __future__ import annotations

import re
from collections import Counter

from .. import state
from ..artifacts import (CodeAudit, CodeAuditFinding, EvalReport, Finding, LensReport, PaperDoc,
                         ProbeResult, Reconciliation, RepoAcquisition)
from ..config import Config
from . import audit as audit_stage

# Severity → rank. Also the display order of the findings table.
_SEVERITY_RANK = {"FATAL": 2, "MAJOR": 1, "MINOR": 0}

# Tiebreak only, applied AFTER severity and evidence strength. Lenses that cite the
# paper against itself sort above lenses that argue about methodology, because an
# editor can check the first kind in seconds.
_LENS_RANK = {"overclaim": 3, "contradiction": 2, "confound": 1, "protocol": 0}

# Verdict thresholds. Stated as data so the rule is inspectable and arguable.
#
# Calibrated against a real run. A four-lens panel returns roughly two MAJORs per lens
# on a *good* paper — that is what a MAJOR means, a claim needing work, not a claim
# that falls over. An earlier "3 MAJOR anywhere => RED" rule therefore returned RED on
# a pre-registered null result with positive controls and a matched-budget design,
# which is exactly the reflexive-rejection failure the lens calibration exists to stop.
#
# RED now means a central claim does not stand. That is a FATAL, or three MAJORs from
# ONE lens (a single dimension failing repeatedly is a coherent pattern, not noise),
# or an overwhelming total.
RED_FATAL, RED_MAJOR_ONE_LENS, RED_MAJOR_TOTAL = 1, 3, 10
YELLOW_MAJOR, YELLOW_MINOR = 1, 4

_CELL_REF = re.compile(r"^T\d+:r\d+:c\d+$")
# Salvaged from the retired grounding tournament: pull the leading magnitude out of
# free text an LLM wrote ("+3-5% top-1", "0.8pp", "~2.5 points") and DEGRADE TO 0.0
# on anything unparseable. Never raises — a bad string must sort last, never invert
# the order and never sink the report.
_MAGNITUDE = re.compile(r"[-+]?\d*\.?\d+")

MAX_TABLE_ROWS = 14
MAX_THREAT_BULLETS = 6
_QUOTE_CHARS = 240


def parse_magnitude(text: str) -> float:
    if not text:
        return 0.0
    m = _MAGNITUDE.search(str(text))
    if not m:
        return 0.0
    try:
        return abs(float(m.group()))
    except (TypeError, ValueError):
        return 0.0


def finding_key(f: Finding) -> tuple:
    """Lexicographic sort key, most-severe-first under `reverse=True`.

    Order: severity, then evidence strength (a cited table cell beats a page
    reference beats nothing), then whether a reproduction could settle it, then the
    lens tiebreak, then the id so the sort is total and therefore reproducible.
    """
    ref = (f.evidence_ref or "").strip()
    evidence = 2 if _CELL_REF.match(ref) else (1 if ref else 0)
    return (
        _SEVERITY_RANK.get(f.severity, 0),
        evidence,
        1 if f.verifiable_by_experiment else 0,
        _LENS_RANK.get(f.lens, 0),
        f.finding_id,
    )


def rank(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=finding_key, reverse=True)


def overall_verdict(findings: list[Finding],
                    reconciliation: Reconciliation | None = None) -> tuple[str, str]:
    """Deterministic RED / YELLOW / GREEN plus the rule that produced it.

    A failed CODE reproduction outranks every paper-side rule. If the authors' own
    published code, run on their own advertised entrypoint, does not produce the number
    printed in the cell — or does not run at all — then the central claim is not
    supported by the artifact the authors themselves offered as support, and no count
    of MAJORs is needed to reach RED.

    Note what is deliberately NOT escalated: INCONCLUSIVE. A missing dataset, a units
    mismatch, an unparsed metric and a shut execution gate all land there, and none of
    them is evidence against a paper.
    """
    if reconciliation is not None and reconciliation.status == "FAILED_REPRODUCTION":
        where = f" at {reconciliation.table_ref}" if reconciliation.table_ref else ""
        return "RED", (f"Failed code reproduction{where}: {reconciliation.reason} The paper's own "
                       f"code does not reproduce the number it prints, so the central claim does "
                       f"not stand on the evidence the authors supplied.")

    n = {s: sum(1 for f in findings if f.severity == s) for s in _SEVERITY_RANK}
    per_lens = Counter(f.lens for f in findings if f.severity == "MAJOR")
    worst_lens, worst_n = per_lens.most_common(1)[0] if per_lens else ("", 0)

    if n["FATAL"] >= RED_FATAL:
        return "RED", f"{n['FATAL']} FATAL finding(s): a central claim does not stand as argued."
    if worst_n >= RED_MAJOR_ONE_LENS:
        return "RED", (f"{worst_n} MAJOR findings from the '{worst_lens}' lens alone: one dimension "
                       f"fails repeatedly, which is a pattern rather than isolated weaknesses.")
    if n["MAJOR"] >= RED_MAJOR_TOTAL:
        return "RED", f"{n['MAJOR']} MAJOR findings across all lenses (>= {RED_MAJOR_TOTAL})."
    if n["MAJOR"] >= YELLOW_MAJOR:
        return "YELLOW", (f"{n['MAJOR']} MAJOR finding(s), at most {worst_n} in any one lens, and no "
                          f"FATAL: headline claims need work, but none is shown not to stand.")
    if n["MINOR"] >= YELLOW_MINOR:
        return "YELLOW", f"{n['MINOR']} MINOR findings (>= {YELLOW_MINOR}) accumulate into real doubt."
    return "GREEN", f"No FATAL or MAJOR findings; {n['MINOR']} MINOR."


def pick_unasked_question(reports: list[LensReport]) -> str:
    """First non-empty question in lens-priority order. Deterministic, verbatim."""
    for r in sorted(reports, key=lambda r: _LENS_RANK.get(r.lens, 0), reverse=True):
        if r.unasked_question.strip():
            return r.unasked_question.strip()
    return ""


def _probe_heading(p: ProbeResult) -> str:
    """Name the section after what actually ran, so the contents are not oversold."""
    if p.provenance == "synthesized":
        return f"Autonomous mechanism probe — `{p.mechanism}`"
    if p.provenance == "repo_exec":
        return "Measured reproduction (the paper's own code)"
    return "Measured reproduction (local)"


def _probe_block(p: ProbeResult) -> list[str]:
    """The measured numbers, stated plainly. Every figure comes from the probe JSON."""
    if p.verdict == "blocked":
        # Deliberately worded as a fact about this harness. "The probe did not produce
        # measurements" would read, next to a paper, as though the paper's code had been
        # tried and found wanting; nothing was tried.
        auth = p.authorization
        why = auth.detail if auth else p.reason
        return [f"⛔ **The repository was not executed** — this harness declined to run it "
                f"(`{auth.decision if auth else 'unauthorized'}`). {why} "
                f"Nothing about the paper follows from this."]
    if p.verdict == "failed":
        return [f"The reproduction probe did not produce usable measurements. {p.reason}"]
    if p.verdict == "degenerate":
        return [f"⚠️ The probe ran but its noise estimate is unusable. {p.reason}"]

    # A probe with no paper-specific script did not reproduce anything — it calibrated
    # this machine's seed noise. Saying otherwise would dress a calibration run up as
    # evidence about the paper, which is the exact move this harness exists to catch
    # other people making. `calibration` is recorded on the result by the runner; the
    # fallback is only for probe_results.json written before that field existed.
    calibration = (p.calibration if p.calibration is not None
                   else (p.verdict == "calibration" or p.claimed_delta is None))
    synthesized = p.provenance == "synthesized"
    if calibration:
        head = (f"⚪ **Hardware Noise-Floor Calibration** (σ = {p.measured_std:.4f}, "
                f"2σ = {p.noise_band:.4f}) — No paper-specific script evaluated.")
    elif synthesized:
        head = {"within_noise": (f"🔴 **Synthesized mechanism probe (`{p.mechanism}`) — the effect "
                                 f"sits INSIDE the noise band.**"),
                "detectable": (f"🟢 **Synthesized mechanism probe (`{p.mechanism}`) — the effect "
                               f"CLEARS the noise band.**")}.get(p.verdict, "")
    else:
        head = {"within_noise": "🔴 **The measured effect sits inside the noise band.**",
                "detectable": "🟢 **The measured effect clears the noise band.**"}.get(p.verdict, "")
    out = [head, "", f"Ran on `{p.device}` over {len(p.seeds_run)} seeds"
                     f"{f' ({len(p.seeds_failed)} failed)' if p.seeds_failed else ''}"
                     f" in {p.seconds:.0f}s.", "",
           "| quantity | value |", "|---|---|"]
    # A calibration run's arms are identical by construction, so its "measured delta" is
    # a property of the template, not a measurement of anything. Printing it next to a
    # claimed delta is what made the old block read as a reproduction.
    if not calibration:
        out.append(f"| measured delta | {p.measured_delta:+.4f} |")
    out += [f"| seed noise (1σ) | {p.measured_std:.4f} |",
            f"| detectability band (2σ) | {p.noise_band:.4f} |"]
    for arm, st in p.arms.items():
        out.append(f"| {arm} mean (n={st.n}) | {st.mean:.4f} ± {st.std:.4f} |")
    if p.claimed_delta is not None:
        out.append(f"| **claimed delta** | **{p.claimed_delta:+.4f}** |")
    out += ["", p.reason]

    # Secondary measurements. A headline metric that moved is only half the answer; the
    # other half is whether anything else moved with it.
    if p.aux:
        arms = list(p.arms)
        out += ["", "**Secondary measurements** (same runs, same seeds):", "",
                "| quantity | " + " | ".join(arms) + " | delta |", "|---" * (len(arms) + 2) + "|"]
        for key, per_arm in p.aux.items():
            cells = [f"{per_arm[a].mean:.4f} ± {per_arm[a].std:.4f}" if a in per_arm else "—"
                     for a in arms]
            if len(arms) == 2 and all(a in per_arm for a in arms):
                delta = f"{per_arm[arms[-1]].mean - per_arm[arms[0]].mean:+.4f}"
            else:
                delta = "—"
            out.append(f"| `{key}` | " + " | ".join(cells) + f" | {delta} |")

    if synthesized:
        out += ["", f"**What authored this probe.** {p.rationale}", "",
                f"⚠️ **This is a reimplementation, not a reproduction.** `runs/{p.paper_id}/probe.py` "
                f"was written by the harness from the paper's own published formulation and run at "
                f"toy scale on synthetic data. It is evidence about whether the stated mechanism "
                f"behaves as described, and it is **not** evidence about any number printed in the "
                f"paper's tables — those were produced at a scale and on datasets this probe does "
                f"not touch." + (" The reconciliation below is capped at INCONCLUSIVE for that "
                                 "reason." if p.reconciliation else "")]
    if calibration:
        out += ["", f"Read this as a detectability floor: on this hardware and metric, any claimed "
                    f"gain below {p.noise_band:.4f} could not be distinguished from run-to-run "
                    f"variation at {len(p.seeds_run)} seeds. It is a measurement of this machine, "
                    f"not of the paper. Supply a reproduction script at "
                    f"`runs/<paper_id>/spec.json` to test an actual claim."]
    if p.claim_within_noise:
        out += ["", f"**The paper's claimed delta of {p.claimed_delta:+.4f} is smaller than this "
                    f"machine's 2σ seed-noise band of {p.noise_band:.4f}.** A gain that size cannot "
                    f"be distinguished from run-to-run variation at this seed count."]
    return out


_REPO_HEAD = {
    "cloned": "🟢 **Cloned and inspected.**",
    "cached": "🟢 **Inspected from an existing checkout.**",
    "synthesized": "⚪ **No repository advertised** — a standalone scaffold was written instead.",
    "unavailable": "⚪ **The paper advertises no code repository.**",
    "blocked": "⚪ **Not fetched — the network gate is closed.**",
    "failed": "⚠️ **Acquisition failed.**",
    "not_attempted": "⚪ **Code acquisition did not run.**",
}
_RECONCILE_HEAD = {
    "RESOLVED_VERIFIED": "🟢 **RESOLVED / VERIFIED** — the executed metric matches the printed cell.",
    "FAILED_REPRODUCTION": "🔴 **FATAL — FAILED CODE REPRODUCTION.**",
    "INCONCLUSIVE": "⚪ **Inconclusive** — no reproduction verdict can be drawn.",
    "NOT_ATTEMPTED": "⚪ **Not attempted.**",
}
MAX_CODE_ROWS = 12


def _repo_block(a: RepoAcquisition) -> list[str]:
    """Where the code came from. States plainly when the answer is 'we did not look'."""
    out = [_REPO_HEAD.get(a.status, f"**{a.status}**"), ""]
    rows = [("source", f"`{a.url}`" if a.url else "— none found in the paper"),
            ("status", a.status)]
    if a.commit:
        rows.append(("commit", f"`{a.commit}`"))
    if a.path:
        rows.append(("path", f"`{a.path}`"))
    if a.frameworks:
        rows.append(("frameworks", ", ".join(a.frameworks)))
    if a.dependency_files:
        rows.append(("dependency files", ", ".join(f"`{f}`" for f in a.dependency_files)))
    if a.dependencies:
        shown = ", ".join(a.dependencies[:12])
        extra = f" _(+{len(a.dependencies) - 12} more)_" if len(a.dependencies) > 12 else ""
        rows.append(("declared packages", shown + extra))
    if a.entrypoint:
        rows.append(("entrypoint", f"`{a.entrypoint}`"))
    rows.append(("environment", a.env_status))
    out += ["| item | value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    if a.reason:
        out += ["", a.reason]
    return out


def _code_audit_block(c: CodeAudit) -> list[str]:
    """Static findings, each with a file, a line and the source line itself."""
    if c.skipped:
        return [f"⚪ **Not run.** {c.skipped}"]
    if not c.findings:
        return [f"🟢 **No cheat patterns matched** across {c.files_scanned} Python file(s) "
                f"({c.lines_scanned:,} lines). This is the absence of a signature, not a "
                f"clean bill of health: the detectors cover baseline crippling, split "
                f"leakage and metric redefinition, and nothing else."]
    counts = Counter(f.category for f in c.findings)
    out = [f"⚠️ **{len(c.findings)} pattern(s)** across {c.files_scanned} Python file(s) "
           f"({c.lines_scanned:,} lines): "
           + ", ".join(f"{n} {cat.replace('_', ' ')}" for cat, n in sorted(counts.items())) + ".",
           "", "Static hits are suspicions with line numbers, never verdicts — each is "
           "listed with the counter-explanation that would clear it.", ""]
    for f in c.findings[:MAX_CODE_ROWS]:
        out.append(f"- **[{f.severity}] {f.title}** — `{f.file}:{f.line}` · `{f.rule_id}`")
        out.append(f"  {f.statement}")
        if f.code_quote:
            out.append(f"  > `{_cell(f.code_quote, _QUOTE_CHARS)}`")
        if f.counter_explanations:
            out.append(f"  Alternative explanation: {_cell(f.counter_explanations[0], 200)}")
    if len(c.findings) > MAX_CODE_ROWS:
        out.append(f"- _…and {len(c.findings) - MAX_CODE_ROWS} further pattern(s); "
                   f"full set in `runs/<paper_id>/probe_results.json`._")
    if c.unparseable:
        out += ["", f"_{len(c.unparseable)} file(s) could not be parsed and were skipped._"]
    return out


def _reconciliation_block(r: Reconciliation) -> list[str]:
    """Executed number against the printed cell, with the arithmetic shown."""
    out = [_RECONCILE_HEAD.get(r.status, f"**{r.status}**"), "", "| quantity | value |", "|---|---|"]
    out.append(f"| cited cell | `{r.table_ref or '—'}` |")
    out.append(f"| cell contents (verbatim) | `{r.claimed_raw or '—'}` |")
    out.append(f"| claimed value | "
               f"{f'{r.claimed_value:g}' if r.claimed_value is not None else '— unparseable'} |")
    out.append(f"| reproduced value | "
               f"{f'{r.reproduced_value:g}' if r.reproduced_value is not None else '— none produced'} |")
    if r.reproduced_std is not None:
        out.append(f"| reproduced spread (1σ) | {r.reproduced_std:.4f} |")
    if r.delta_error is not None:
        out.append(f"| **Δ_error \\|reproduced − claimed\\|** | **{r.delta_error:.4f}** |")
    out.append(f"| hardware noise band (2σ) | {r.noise_band:.4f} |")
    if r.seeds_run:
        out.append(f"| seeds | {len(r.seeds_run)} ({', '.join(str(s) for s in r.seeds_run)}) |")
    out += ["", r.reason]
    return out


def _cell(s: str, n: int) -> str:
    """One markdown table cell: pipes escaped, newlines flattened, length capped."""
    s = " ".join((s or "").split()).replace("|", "\\|")
    return (s[: n - 1] + "…") if len(s) > n else s


def render_eval_report(r: EvalReport) -> str:
    """EvalReport → markdown. Pure: no LLM, no network, no Config.

    Target is 1-2 pages, so the findings table is capped and the threat bullets are
    capped — but a truncation is always STATED, never silent. A report that quietly
    drops findings reads as a clean bill of health for the ones it dropped.
    """
    ranked = rank(r.findings)
    badge = {"RED": "🔴 REJECT / RED FLAG",
             "YELLOW": "🟡 BORDERLINE",
             "GREEN": "🟢 PASS"}.get(r.verdict, r.verdict)
    counts = {s: sum(1 for f in r.findings if f.severity == s) for s in _SEVERITY_RANK}

    L = [
        f"# First-Round Review — {r.title or r.paper_id}",
        "",
        f"**Verdict: {badge}**",
        "",
        f"> {r.verdict_reason}",
        "",
        f"`{r.paper_id}` · {r.n_pages} pages · {r.n_sections} sections · {r.n_tables} tables · "
        f"{r.n_numbers} reported numbers",
        f"Lenses run: {', '.join(r.lenses_run) or '(none)'} · "
        f"Findings: {counts['FATAL']} FATAL / {counts['MAJOR']} MAJOR / {counts['MINOR']} MINOR"
        + (f" · {r.dropped_findings} dropped as unsubstantiated" if r.dropped_findings else ""),
        "",
        "## Critical validity threats",
        "",
    ]

    threats = [f for f in ranked if f.severity in ("FATAL", "MAJOR")]
    if not threats:
        L.append("None. No finding rises above MINOR.")
    else:
        for f in threats[:MAX_THREAT_BULLETS]:
            where = f" — `{f.evidence_ref}`" if f.evidence_ref else ""
            L.append(f"- **[{f.severity}] {f.title}**{where}")
            L.append(f"  {f.statement}")
            L.append(f"  > \"{_cell(f.evidence_quote, _QUOTE_CHARS)}\"")
            if f.counter_explanations:
                L.append(f"  Alternative explanation: {_cell(f.counter_explanations[0], 200)}")
        if len(threats) > MAX_THREAT_BULLETS:
            L.append(f"- _…and {len(threats) - MAX_THREAT_BULLETS} further "
                     f"FATAL/MAJOR finding(s) — see the table below._")
    L.append("")

    if r.probe:
        L += [f"## {_probe_heading(r.probe)}", "", *_probe_block(r.probe), ""]
        if r.probe.repo is not None:
            L += ["## Code acquisition", "", *_repo_block(r.probe.repo), ""]
        if r.probe.code_audit is not None:
            L += ["## Static code audit", "", *_code_audit_block(r.probe.code_audit), ""]
        if r.probe.reconciliation is not None:
            L += ["## Table-cell reconciliation", "",
                  *_reconciliation_block(r.probe.reconciliation), ""]

    L += ["## The unasked obvious question", "",
          r.unasked_question or "_No lens identified a conspicuously missing comparison._", ""]

    L += ["## Audit lens findings", "",
          "| Severity | Lens | Target | Finding | Evidence |",
          "|---|---|---|---|---|"]
    for f in ranked[:MAX_TABLE_ROWS]:
        L.append(
            f"| {f.severity} | {f.lens} | {_cell(f.target, 60) or '—'} | "
            f"{_cell(f.title, 110)} | {_cell(f.evidence_ref, 24) or '—'} |"
        )
    if not ranked:
        L.append("| — | — | — | No findings returned | — |")
    elif len(ranked) > MAX_TABLE_ROWS:
        L.append(f"| … | … | … | _{len(ranked) - MAX_TABLE_ROWS} further finding(s) "
                 f"omitted for length; full set in `audit/`_ | … |")

    verifiable = [f for f in ranked if f.verifiable_by_experiment]
    if verifiable:
        L += ["", "## Settleable by reproduction", "",
              "These findings are claims a targeted rerun could confirm or kill:", ""]
        L += [f"- `{f.finding_id}` {_cell(f.title, 110)}" for f in verifiable[:6]]

    return "\n".join(L).rstrip() + "\n"


def run_report(cfg: Config, pid: str) -> dict:
    """Assemble the ranked report from the ingested doc + whichever lenses ran."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))

    # Verified load: a finding whose evidence is not really in the paper is dropped
    # here, whoever wrote it. The driver is a language model; the harness checks.
    reports, dropped = audit_stage.load_reports(cfg, pid, doc)
    if not reports:
        return {"error": f"no audit lenses have run for '{pid}' — run audit_paper first, "
                         f"then write audit/<lens>.json for each prompt"}
    state.set_phase(cfg, pid, "report")

    probe_path = root / "runs" / pid / "probe_results.json"
    probe = ProbeResult(**state.read_json(probe_path)) if probe_path.exists() else None

    findings = rank([f for r in reports for f in r.findings])
    verdict, reason = overall_verdict(findings, probe.reconciliation if probe else None)
    report = EvalReport(
        paper_id=pid, title=doc.title, verdict=verdict, verdict_reason=reason,
        findings=findings, unasked_question=pick_unasked_question(reports),
        n_pages=doc.n_pages, n_sections=len(doc.sections), n_tables=len(doc.tables),
        n_claims=len(doc.claims), n_numbers=len(doc.reported_numbers),
        lenses_run=[r.lens for r in reports], dropped_findings=dropped, probe=probe,
    )

    md_path, json_path = root / "reports" / f"{pid}.md", root / "reports" / f"{pid}.json"
    state.write_json(json_path, report.model_dump())
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_eval_report(report), encoding="utf-8")
    state.append_log(
        cfg, pid, artifact_type="eval_report", phase="report",
        headers={"verdict": verdict, "findings": len(findings), "dropped": dropped,
                 "severities": {s: sum(1 for f in findings if f.severity == s) for s in _SEVERITY_RANK},
                 "lenses": report.lenses_run, "probe": probe.verdict if probe else None},
        path=str(md_path),
    )
    return {"paper_id": pid, "verdict": verdict, "reason": reason,
            "findings": len(findings), "dropped_unsubstantiated": dropped,
            "lenses": report.lenses_run, "probe": probe.verdict if probe else None,
            "report_md": str(md_path), "report": f"reports/{pid}.json"}


if __name__ == "__main__":  # self-check: python -m harness.stages.report
    def _f(fid, lens, sev, ref="", verifiable=False):
        return Finding(finding_id=fid, lens=lens, severity=sev, title=f"{fid} title",
                       statement="s", evidence_quote="q", evidence_ref=ref,
                       verifiable_by_experiment=verifiable)

    assert parse_magnitude("+3-5% top-1") == 3.0
    assert parse_magnitude("not a number") == 0.0
    assert parse_magnitude("") == 0.0

    assert overall_verdict([_f("a", "overclaim", "FATAL")])[0] == "RED"
    assert overall_verdict([_f(str(i), "protocol", "MAJOR") for i in range(3)])[0] == "RED"
    assert overall_verdict([_f("a", "protocol", "MAJOR")])[0] == "YELLOW"
    assert overall_verdict([_f(str(i), "protocol", "MINOR") for i in range(4)])[0] == "YELLOW"
    assert overall_verdict([_f("a", "protocol", "MINOR")])[0] == "GREEN"
    assert overall_verdict([])[0] == "GREEN"

    # severity dominates; cell evidence outranks page evidence at equal severity
    order = rank([_f("m", "overclaim", "MINOR", "T0:r0:c0"),
                  _f("j", "protocol", "FATAL"),
                  _f("k", "contradiction", "MAJOR", "p3"),
                  _f("l", "contradiction", "MAJOR", "T1:r2:c3")])
    assert [f.finding_id for f in order] == ["j", "l", "k", "m"], order
    assert rank(order) == order, "rank must be stable/idempotent"

    rep = EvalReport(paper_id="p", title="T", verdict="RED", verdict_reason="because",
                     findings=order, unasked_question="why no baseline?", lenses_run=["protocol"])
    md = render_eval_report(rep)
    assert "🔴 REJECT / RED FLAG" in md and "why no baseline?" in md
    assert md.count("\n|") >= 4 and md.endswith("\n")
    assert _cell("a|b\nc", 99) == "a\\|b c", "pipes must be escaped or the table breaks"

    # --- S3 code reproduction ---------------------------------------------------------
    def _rec(status, **kw):
        return Reconciliation(table_ref="T1:r0:c1", claimed_raw="59.28", claimed_value=59.28,
                              noise_band=0.1, status=status, reason="r", **kw)

    # A failed reproduction is RED on its own, outranking an otherwise-clean panel.
    v, why = overall_verdict([], _rec("FAILED_REPRODUCTION", reproduced_value=64.1,
                                      delta_error=4.82))
    assert v == "RED" and "Failed code reproduction" in why, why
    # …but the softer statuses never escalate, because none of them is evidence.
    assert overall_verdict([], _rec("INCONCLUSIVE"))[0] == "GREEN"
    assert overall_verdict([], _rec("RESOLVED_VERIFIED"))[0] == "GREEN"
    assert overall_verdict([], _rec("NOT_ATTEMPTED"))[0] == "GREEN"
    assert overall_verdict([_f("a", "overclaim", "FATAL")], _rec("RESOLVED_VERIFIED"))[0] == "RED", \
        "a verified cell does not clear a FATAL the lenses found in the paper"

    blocked = _repo_block(RepoAcquisition(
        url="https://github.com/x/y", status="blocked", reason="network gate closed"))
    assert "network gate is closed" in "\n".join(blocked)
    clean = _code_audit_block(CodeAudit(repo_path="r", files_scanned=3, lines_scanned=90))
    assert "not a clean bill of health" in "\n".join(clean), \
        "an empty static audit must not read as an endorsement"
    assert "Not run." in "\n".join(_code_audit_block(CodeAudit(skipped="nothing acquired")))
    hit = _code_audit_block(CodeAudit(repo_path="r", files_scanned=1, lines_scanned=10, findings=[
        CodeAuditFinding(finding_id="code-01", rule_id="leak-fit-on-test",
                         category="data_leakage", severity="MAJOR", title="t", statement="s",
                         file="a.py", line=7, code_quote="scaler.fit(X_test)")]))
    assert "`a.py:7`" in "\n".join(hit) and "scaler.fit(X_test)" in "\n".join(hit)

    body = "\n".join(_reconciliation_block(_rec("FAILED_REPRODUCTION", reproduced_value=64.1,
                                                delta_error=4.82, seeds_run=[0, 1, 2])))
    assert "FAILED CODE REPRODUCTION" in body and "0.1000" in body and "4.8200" in body

    full = render_eval_report(EvalReport(
        paper_id="p", title="T", verdict="RED", verdict_reason="because", findings=order,
        lenses_run=["protocol"],
        probe=ProbeResult(paper_id="p", verdict="calibration", calibration=True,
                          measured_std=0.0048, noise_band=0.0096, seeds_run=[0, 1],
                          repo=RepoAcquisition(url="https://github.com/x/y", status="cloned",
                                               entrypoint="eval.py", frameworks=["pytorch"]),
                          code_audit=CodeAudit(repo_path="r", files_scanned=2, lines_scanned=40),
                          reconciliation=_rec("RESOLVED_VERIFIED", reproduced_value=59.30,
                                              delta_error=0.02))))
    for heading in ("## Code acquisition", "## Static code audit", "## Table-cell reconciliation"):
        assert heading in full, f"missing section: {heading}"
    print("report self-check OK")
