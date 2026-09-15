"""System metrics over a reviewed corpus — counted from artifacts, never asserted.

`python -m harness.evaluation` runs the self-check.

**What this module refuses to compute.** Reviewer accuracy, false-positive rate,
agreement with human reviewers, and "issues a human would have missed" all require
ground truth this project does not have: no paper in the corpus carries an adjudicated
label saying which of its findings are real. Every one of those numbers is therefore
absent here rather than estimated, and `LIMITATIONS` states so in the artifact itself so
a reader of the JSON cannot mistake absence for zero.

What IS measurable is the system's own behaviour, and all of it comes from files the
pipeline already wrote: how many findings survived verification against the paper, how
much of each paper was structurally addressable, how often an experiment was judged
necessary, which gate stopped the rest, and how much shorter the reviewer's report is
than the trace behind it. Those are the claims the architecture actually makes, so those
are the ones reported.

**Provenance violations are a rate that must be zero.** It is computed rather than
assumed: a reconciliation carrying a verdict on inadmissible provenance is a defect in
this harness, and a metric that would reveal one is worth more than a metric that
confirms what the tests already assert.
"""
from __future__ import annotations

import json
from pathlib import Path

from .artifacts import CaseLedger, EvalReport
from .config import Config
from . import provenance as provenance_mod
from .stages import discover as discover_stage

LIMITATIONS = [
    "No paper in this corpus carries adjudicated ground truth for its findings, so "
    "precision, recall and agreement with human reviewers are not computed. Their absence "
    "here is not a zero.",
    "The corpus is small and was assembled opportunistically; none of these rates should "
    "be read as an estimate of behaviour on a different distribution of papers.",
    "Execution gates are shut by default, so execution-derived rates describe how often "
    "the system JUDGED an experiment necessary, not how often one ran.",
    "`funnel.warranting_experiment` is a judgement and `funnel.launched` is a count of "
    "processes actually started. They are different numbers and neither substitutes for "
    "the other; when the second is zero, no experiment ran, whatever the first says.",
    "`probe_stage_seconds` is the wall time of the probe stage — acquisition, static "
    "audit, planning and gate evaluation. It is not the cost of running experiments.",
    "`findings_by_scientific_class` is derived most-specific-first from each finding's own "
    "closed-vocabulary self-classification, and falls back to WHICH LENS raised it when a "
    "finding classified neither its discrepancy nor its missing baseline. Counts in the "
    "fallback cases are therefore partly a count of what each lens wrote, not an "
    "independent classification of the paper.",
    "`severity` is the lens's own assertion wherever grading did not run; the "
    "`grade_coverage` field on each report says how much of it was independently graded.",
    "`objects_addressable_rate` is SELF-REFERENTIAL and is not a coverage measure. Both "
    "of its terms are the same list of objects this harness minted, and an unresolvable "
    "reference never becomes an object at all, so a WORSE EXTRACTOR SCORES HIGHER on it. "
    "It is kept because it was published, and it is renamed and labelled here so it "
    "cannot be read as coverage. The measure with an external denominator is "
    "`review_surface_coverage`, whose denominator is the paper's own addressable surface "
    "counted from `PaperDoc`.",
    "`review_surface_coverage` is STRUCTURAL coverage and is not issue recall. It says "
    "how much of the paper's addressable surface a review connected to a question, not "
    "whether the review found the issues that matter, and `semantic_coverage` on every "
    "CoverageReport says `not_machine_detectable` unconditionally for that reason.",
    "`prose_presented_fraction` is the ceiling on any recall claim and was unmeasured "
    "until now: the lens prompt divides a character budget across sections, so on a long "
    "paper the lenses read a TRUNCATED document. A review's own scope line says "
    "\"N independent lens(es) read the paper\"; this number says how much of it.",
    "`document_observations` are mechanically determined document-level facts and are "
    "NOT findings. They gate nothing and count toward no threshold, and each says whether "
    "it is about the PAPER or about this harness's own EXTRACTION - a distinction that "
    "matters because a naive existence check over this corpus produced twelve claims that "
    "a table or equation was missing and every one of those objects is in the paper.",
]


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _pct(value) -> str:
    """A percentage, or the words that must appear where one cannot be computed. `None`
    is not zero and is not one: an empty surface has no coverage, it has no denominator."""
    return "not computable" if value is None else f"{value:.0%}"


def _surface_totals(papers: list[dict]) -> dict:
    """Corpus coverage from the SUMS, not from averaged per-paper rates.

    Averaging rates weights a paper with two tables the same as one with forty, which is
    how a small denominator flatters a corpus number. `None` where the corpus surface is
    empty: an empty surface has no coverage, not full coverage.
    """
    rows = [x.get("review_surface_coverage") for x in papers
            if x.get("review_surface_coverage")]
    if not rows:
        return {"papers_measured": 0, "surface_size": 0, "addressed": 0, "examined": 0,
                "addressed_rate": None, "examined_rate": None,
                "semantic_coverage": "not_machine_detectable"}
    size = sum(int(r.get("surface_size") or 0) for r in rows)
    addressed = sum(int(r.get("addressed") or 0) for r in rows)
    examined = sum(int(r.get("examined") or 0) for r in rows)
    fractions = [r.get("prose_presented_fraction") for r in rows
                 if r.get("prose_presented_fraction") is not None]
    return {
        "papers_measured": len(rows),
        "surface_size": size, "addressed": addressed, "examined": examined,
        "addressed_rate": _ratio(addressed, size),
        "examined_rate": _ratio(examined, size),
        "off_surface": sum(int(r.get("off_surface") or 0) for r in rows),
        "prose_presented_fraction_min": min(fractions) if fractions else None,
        "prose_presented_fraction_max": max(fractions) if fractions else None,
        # ALWAYS present and always this value, so a clean coverage table cannot be read
        # as evidence that the review found what mattered.
        "semantic_coverage": "not_machine_detectable",
    }


def merge_states(papers: list[dict], key: str) -> dict[str, int]:
    """Count one per-paper vocabulary field across the corpus."""
    out: dict[str, int] = {}
    for x in papers:
        v = x.get(key) or ""
        if v:
            out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def assert_conservation(summary: dict) -> list[str]:
    """Every requested paper is counted exactly once, and no total exceeds its own term.

    `evaluation.corpus` had no conservation law, and `papers_measured == 8` beside
    `sum(triage.values()) == 7` is reachable from the default CLI invocation. A summary
    whose parts do not add up is worse than a missing summary, because it is quotable.
    Returns the violations rather than raising: a broken accounting must appear IN the
    artifact, where a reader of the JSON meets it.
    """
    bad: list[str] = []
    measured = int(summary.get("papers_measured") or 0)
    for field in ("triage", "binary_verdict", "review_paths"):
        counted = sum(int(v) for v in (summary.get(field) or {}).values())
        if counted != measured:
            bad.append(f"{field} counts {counted} paper(s) against "
                       f"papers_measured={measured}")
    if measured + len(summary.get("papers_without_a_report") or []) \
            != int(summary.get("papers_requested") or 0):
        bad.append("papers_measured + papers_without_a_report != papers_requested")
    f = summary.get("funnel") or {}
    # THE CHAIN IS FIVE TERMS LONG, NOT SIX, and the sixth is the reason the funnel has
    # six terms in the first place. `discovered >= checkable >= warranting_experiment >=
    # launched >= completed` is a real nesting: each is a subset of the one before it, and
    # `launched > warranting_experiment` would mean something ran that was never judged to
    # warrant it, which is a defect worth catching.
    #
    # `resolved` is NOT a subset of `completed`. A question settled from the paper's own
    # printed arithmetic is resolved with nothing launched at all, which is the whole
    # point of the cheap-route-first policy (invariant 23) and is why CLAUDE.md says the
    # six terms are read off six different artifacts and none substitutes for another.
    # Asserting the chain through the last term reported `resolved=12 exceeds completed=11`
    # as an accounting defect when it was a correct measurement of exactly that policy.
    chain = ("discovered", "checkable", "warranting_experiment", "launched", "completed")
    for a, b in zip(chain, chain[1:]):
        if int(f.get(b) or 0) > int(f.get(a) or 0):
            bad.append(f"funnel term {b}={f.get(b)} exceeds {a}={f.get(a)}")
    # `resolved` is bounded by what was discovered, because it counts targets.
    if int(f.get("resolved") or 0) > int(f.get("discovered") or 0):
        bad.append(f"funnel term resolved={f.get('resolved')} exceeds "
                   f"discovered={f.get('discovered')}")
    cov = summary.get("review_surface_coverage") or {}
    if int(cov.get("examined") or 0) > int(cov.get("addressed") or 0):
        bad.append("coverage: examined exceeds addressed")
    if int(cov.get("addressed") or 0) > int(cov.get("surface_size") or 0):
        bad.append("coverage: addressed exceeds the surface it is measured against")
    rex = summary.get("route_exhaustion") or {}
    classified = (int(rex.get("exhausted") or 0)
                  + int(rex.get("open_because_untried") or 0)
                  + int(rex.get("open_because_of_this_harness") or 0))
    if classified != int(rex.get("questions") or 0):
        bad.append("route exhaustion: question states do not conserve the denominator")
    if int(rex.get("routes_exhausted") or 0) > int(rex.get("routes_applicable") or 0):
        bad.append("route exhaustion: exhausted routes exceed applicable routes")
    return bad


def per_paper(cfg: Config, pid: str) -> dict | None:
    """Everything measurable about one reviewed paper, or None if it has no report."""
    root = cfg.projects_dir / pid / "reports"
    report_path = root / f"{pid}.json"
    if not report_path.exists():
        return None
    report = EvalReport(**json.loads(report_path.read_text(encoding="utf-8")))
    ledger_path = root / f"{pid}.ledger.json"
    led = (CaseLedger(**json.loads(ledger_path.read_text(encoding="utf-8")))
           if ledger_path.exists() else CaseLedger(paper_id=pid))
    ts = discover_stage.load(cfg, pid)
    eff = led.efficiency or {}

    review_md = root / f"{pid}.review.md"
    machine_md = root / f"{pid}.md"
    review_chars = len(review_md.read_text(encoding="utf-8")) if review_md.exists() else 0
    machine_chars = len(machine_md.read_text(encoding="utf-8")) if machine_md.exists() else 0
    ledger_chars = len(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else 0

    kept, dropped = len(report.findings), report.dropped_findings
    discovered = eff.get("targets_discovered", 0)

    # A verdict reached on evidence a ceiling does not admit. Must be zero; computed so a
    # regression shows up as a number rather than as a silently wrong report.
    violations = 0
    for o in (ts.outcomes if ts else []):
        rec = o.reconciliation
        if rec and rec.status in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION") \
                and not provenance_mod.admits(rec.provenance):
            violations += 1

    return {
        "paper_id": pid,
        "verdict": report.verdict,
        "triage": report.triage,
        "reproduction_status": report.reproduction_status,
        "lenses_run": len(report.lenses_run),
        "findings_kept": kept,
        "findings_dropped_unsubstantiated": dropped,
        "finding_verification_rate": _ratio(kept, kept + dropped),
        "questions_generated": eff.get("questions_generated", 0),
        "targets_discovered": discovered,
        "targets_addressable": eff.get("targets_addressable", 0),
        # RENAMED from `target_addressability_rate`. Both terms are `ts.objects`, and an
        # unresolvable reference never becomes an object, so this rate RISES when
        # extraction degrades. It is not coverage and the new name says so.
        "objects_addressable_rate": _ratio(eff.get("targets_addressable", 0), discovered),
        "targets_requiring_execution": eff.get("targets_requiring_execution", 0),
        "execution_trigger_rate": _ratio(eff.get("targets_requiring_execution", 0), discovered),
        "targets_resolved_without_execution": eff.get("targets_resolved_without_execution", 0),
        "targets_blocked_before_execution": eff.get("targets_blocked_before_execution", 0),
        "targets_deferred_by_budget": eff.get("targets_deferred_by_budget", 0),
        "targets_settled": eff.get("targets_settled", 0),
        # Its own term, never added to `targets_resolved_without_execution`. Verifying that
        # a concern quotes the paper accurately is a precondition invariant 1 already
        # guarantees for every kept finding, not a second resolution.
        "targets_citation_verified_only": eff.get("targets_citation_verified_only", 0),
        "route_exhaustion": eff.get("route_exhaustion", {
            "questions": 0, "exhausted": 0, "open_because_untried": 0,
            "open_because_of_this_harness": 0, "rate": None,
            "routes_applicable": 0, "routes_attempted": 0,
            "routes_completed": 0, "routes_exhausted": 0,
        }),

        # --- review-surface coverage, with a denominator read off the PAPER ------------
        "review_surface_coverage": ({
            "surface_size": report.coverage.surface_size,
            "addressed": report.coverage.addressed,
            "examined": report.coverage.examined,
            "addressed_rate": report.coverage.addressed_rate,
            "examined_rate": report.coverage.examined_rate,
            "off_surface": len(report.coverage.off_surface),
            "prose_presented_fraction": report.coverage.prose_presented_fraction,
            "semantic_coverage": report.coverage.semantic_coverage,
        } if report.coverage is not None else None),

        # --- which review path, and what the document-integrity layer observed ---------
        "review_path": report.review_path,
        "artifact_state": report.artifact_state,
        "document_observations": {
            "about_paper": sum(1 for o in (report.document_observations or [])
                               if o.about == "PAPER"),
            "about_extraction": sum(1 for o in (report.document_observations or [])
                                    if o.about == "EXTRACTION"),
        },
        # A non-empty list is a defect in THIS HARNESS for this review, never a finding
        # about the paper. Reported so a regression shows up as a number.
        "guarantees_unmet": (list(report.guarantees.unmet) if report.guarantees else []),

        # --- the six-term funnel, exported as six terms --------------------------------
        # Each is read off a different artifact and none may be substituted for another.
        # `targets_warranting_experiment` is a judgement; `targets_launched` and
        # `processes_launched` are the execution record. Reporting the first as though it
        # were the second is the specific overclaim this split exists to prevent.
        "funnel": {
            "discovered": discovered,
            "checkable": eff.get("targets_addressable", 0),
            "warranting_experiment": eff.get("targets_warranting_experiment", 0),
            "launched": eff.get("targets_launched", 0),
            "completed": eff.get("executions_completed", 0),
            "resolved": eff.get("targets_resolved", 0),
        },
        "processes_launched": eff.get("processes_launched", 0),
        "executions_completed": eff.get("executions_completed", 0),

        # --- the three axes, per paper -------------------------------------------------
        "findings_by_scientific_class": eff.get("findings_by_scientific_class", {}),
        "targets_by_resolution_state": eff.get("targets_by_resolution_state", {}),
        "targets_by_evidence_state": eff.get("targets_by_evidence_state", {}),
        "targets_by_experiment_necessity": eff.get("targets_by_experiment_necessity", {}),
        "questions_open": eff.get("questions_open", 0),

        "blocked_by_gate": eff.get("blocked_by_gate", {}),
        "provenance_violations": violations,
        # Wall time of the PROBE STAGE — acquisition, static audit, planning and every
        # gate. Not the cost of running experiments, and named so it cannot be read as
        # one: when `processes_launched` is zero this number is entirely setup.
        "probe_stage_seconds": eff.get("probe_stage_seconds", 0.0),
        "reviewer_report_chars": review_chars,
        "machine_report_chars": machine_chars,
        "ledger_chars": ledger_chars,
        "compaction_ratio": _ratio(review_chars, machine_chars + ledger_chars),
        "targets_summary": report.targets_summary,
    }


def corpus(cfg: Config, pids: list[str]) -> dict:
    """The whole corpus, per paper and in aggregate."""
    papers = [p for p in (per_paper(cfg, pid) for pid in pids) if p is not None]
    missing = [pid for pid in pids if per_paper(cfg, pid) is None]

    def total(key: str) -> int:
        return sum(int(p.get(key) or 0) for p in papers)

    gates: dict[str, int] = {}
    for p in papers:
        for gate, n in (p.get("blocked_by_gate") or {}).items():
            gates[gate] = gates.get(gate, 0) + int(n)

    def merge(key: str) -> dict[str, int]:
        """Sum one per-paper counter across the corpus, preserving its keys."""
        out: dict[str, int] = {}
        for p in papers:
            for k, n in (p.get(key) or {}).items():
                out[k] = out.get(k, 0) + int(n)
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    funnel = {k: sum(int((p.get("funnel") or {}).get(k) or 0) for p in papers)
              for k in ("discovered", "checkable", "warranting_experiment",
                        "launched", "completed", "resolved")}

    kept, dropped = total("findings_kept"), total("findings_dropped_unsubstantiated")
    discovered = total("targets_discovered")
    route_fields = ("questions", "exhausted", "open_because_untried",
                    "open_because_of_this_harness", "routes_applicable",
                    "routes_attempted", "routes_completed", "routes_exhausted")
    route_exhaustion = {
        key: sum(int((p.get("route_exhaustion") or {}).get(key) or 0) for p in papers)
        for key in route_fields
    }
    route_exhaustion["rate"] = _ratio(
        route_exhaustion["exhausted"], route_exhaustion["questions"])
    route_exhaustion["note"] = (
        "question-weighted corpus rate; gates, budgets and set-level policies never "
        "count as exhausted")
    summary = {
        "papers_requested": len(pids),
        "papers_measured": len(papers),
        "papers_without_a_report": missing,
        "triage": {level: sum(1 for p in papers if p["triage"] == level)
                   for level in ("RED", "YELLOW", "GREEN")},
        "binary_verdict": {level: sum(1 for p in papers if p["verdict"] == level)
                           for level in ("RED", "GREEN")},
        "findings_kept": kept,
        "findings_dropped_unsubstantiated": dropped,
        "finding_verification_rate": _ratio(kept, kept + dropped),
        "questions_generated": total("questions_generated"),
        "targets_discovered": discovered,
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

        # Coverage, summed over the corpus, with the paper-side denominator. Rates are
        # computed from the SUMS rather than averaged, because averaging per-paper rates
        # weights a two-table paper the same as a forty-table one.
        "review_surface_coverage": _surface_totals(papers),
        "review_paths": {k: sum(1 for x in papers if x.get("review_path") == k)
                         for k in ("PAPER_ONLY", "PAPER_AND_ARTIFACT")},
        "artifact_states": merge_states(papers, "artifact_state"),
        "document_observations": {
            "about_paper": sum((x.get("document_observations") or {}).get("about_paper", 0)
                               for x in papers),
            "about_extraction": sum(
                (x.get("document_observations") or {}).get("about_extraction", 0)
                for x in papers),
        },
        "guarantees_unmet": sorted({g for x in papers
                                    for g in (x.get("guarantees_unmet") or [])}),

        # The funnel, in order, each term from its own artifact. `warranting_experiment`
        # is a judgement and `launched` is a count of started processes: when the second
        # is zero, no experiment ran, whatever the first says.
        "funnel": funnel,
        "processes_launched": total("processes_launched"),
        "executions_completed": total("executions_completed"),
        # A float, so it cannot go through `total()`. Named for what it measures — the
        # probe stage's wall time, which covers acquisition, static audit, planning and
        # every gate — because with `processes_launched` small it is mostly setup, and
        # calling it execution cost would be a fabricated measurement.
        "probe_stage_seconds": round(
            sum(float(p.get("probe_stage_seconds") or 0.0) for p in papers), 3),

        # The three axes, aggregated. This is the primary results presentation: scientific
        # findings by category, and what became of the questions they raised — not a
        # colour, which collapses all three into one letter.
        "findings_by_scientific_class": merge("findings_by_scientific_class"),
        "targets_by_resolution_state": merge("targets_by_resolution_state"),
        "targets_by_evidence_state": merge("targets_by_evidence_state"),
        "targets_by_experiment_necessity": merge("targets_by_experiment_necessity"),
        "questions_open": total("questions_open"),

        "blocked_by_gate": gates,
        "provenance_violations": total("provenance_violations"),
        "reviewer_report_chars": total("reviewer_report_chars"),
        "machine_report_chars": total("machine_report_chars"),
        "ledger_chars": total("ledger_chars"),
        "compaction_ratio": _ratio(total("reviewer_report_chars"),
                                   total("machine_report_chars") + total("ledger_chars")),
        "limitations": LIMITATIONS,
        "per_paper": papers,
    }
    # Asserted IN the artifact, not in a comment. `evaluation.corpus` had no conservation
    # law at all, and `papers_measured == 8` beside `sum(triage.values()) == 7` is
    # reachable from the default CLI invocation. A summary whose parts do not add up is
    # worse than a missing summary, because it is quotable.
    summary["conservation_violations"] = assert_conservation(summary)
    return summary


def render(summary: dict) -> str:
    """The corpus table the manuscript uses, and nothing it cannot support."""
    L = ["# System evaluation", "",
         f"{summary['papers_measured']} of {summary['papers_requested']} requested paper(s) "
         f"have a report and are measured below.", "",
         "| Paper | Questions | Targets | Addressable | Needed an experiment | "
         "Settled w/o execution | Blocked | Triage |",
         "|---|---:|---:|---:|---:|---:|---:|---|"]
    for p in summary["per_paper"]:
        L.append(f"| `{p['paper_id']}` | {p['questions_generated']} | "
                 f"{p['targets_discovered']} | {p['targets_addressable']} | "
                 f"{p['targets_requiring_execution']} | "
                 f"{p['targets_resolved_without_execution']} | "
                 f"{p['targets_blocked_before_execution']} | {p['triage'] or '—'} |")
    f = summary.get("funnel") or {}
    L += ["", "## From discovery to a settled question", "",
          "Six terms, each read off a different artifact. They are NOT interchangeable: "
          "`warranting an experiment` is a judgement this system made, `launched` is a "
          "count of processes it actually started.", "",
          "| Stage | Targets |", "|---|---:|",
          f"| discovered | {f.get('discovered', 0)} |",
          f"| structurally checkable | {f.get('checkable', 0)} |",
          f"| judged to warrant an experiment | {f.get('warranting_experiment', 0)} |",
          f"| actually launched a process | {f.get('launched', 0)} |",
          f"| ran to a reconciliation | {f.get('completed', 0)} |",
          f"| settled the question about the paper | {f.get('resolved', 0)} |"]

    rex = summary.get("route_exhaustion") or {}
    if rex.get("questions"):
        L += ["", "## Material-question route exhaustion", "",
              "Every central or machine-material question remains in the denominator. "
              "A gate, target budget or set-level policy cannot discharge a route, and a "
              "paper-internal check that only verifies a citation remains open.", "",
              "| Term | Count |", "|---|---:|",
              f"| material questions | {rex.get('questions', 0)} |",
              f"| questions with every applicable route exhausted | "
              f"{rex.get('exhausted', 0)} |",
              f"| open because a route was untried or inconclusive | "
              f"{rex.get('open_because_untried', 0)} |",
              f"| open because of this harness | "
              f"{rex.get('open_because_of_this_harness', 0)} |",
              f"| applicable / attempted / completed / exhausted routes | "
              f"{rex.get('routes_applicable', 0)} / {rex.get('routes_attempted', 0)} / "
              f"{rex.get('routes_completed', 0)} / {rex.get('routes_exhausted', 0)} |",
              f"| route-exhaustion coverage | {_pct(rex.get('rate'))} |"]

    cov = summary.get("review_surface_coverage") or {}
    if cov.get("surface_size"):
        L += ["", "## Review-surface coverage", "",
              "Structural coverage over the paper's OWN addressable surface, counted from "
              "`PaperDoc`. This is not issue recall and cannot be: no paper here carries "
              "adjudicated ground truth, so `semantic_coverage` reads "
              f"`{cov.get('semantic_coverage')}` unconditionally.", "",
              "| Term | Units |", "|---|---:|",
              f"| addressable surface (the denominator) | {cov.get('surface_size', 0)} |",
              f"| an address was minted | {cov.get('addressed', 0)} "
              f"({_pct(cov.get('addressed_rate'))}) |",
              f"| a route was pursued | {cov.get('examined', 0)} "
              f"({_pct(cov.get('examined_rate'))}) |",
              f"| addresses outside the surface (a defect if non-zero) | "
              f"{cov.get('off_surface', 0)} |"]
        lo, hi = (cov.get("prose_presented_fraction_min"),
                  cov.get("prose_presented_fraction_max"))
        if lo is not None:
            L += ["", f"The audit lenses were shown between {lo:.0%} and {hi:.0%} of each "
                      f"paper's extracted section text: the lens prompt divides a character "
                      f"budget across sections, so on a long paper they read a truncated "
                      f"document. That is the ceiling on any recall claim."]

    paths = summary.get("review_paths") or {}
    if any(paths.values()):
        L += ["", "## Which review path each paper was on", "",
              "| Path | Papers |", "|---|---:|"]
        L += [f"| {k} | {v} |" for k, v in paths.items()]

    dobs = summary.get("document_observations") or {}
    if dobs.get("about_paper") or dobs.get("about_extraction"):
        L += ["", "## Document-integrity observations", "",
              f"{dobs.get('about_paper', 0)} about the papers and "
              f"{dobs.get('about_extraction', 0)} about this harness's own extraction. "
              f"They are observations, not findings: none counts toward any threshold, "
              f"and the split exists because a naive existence check over this corpus "
              f"produced twelve claims that a table or equation was missing while every "
              f"one of those objects is in the paper."]

    cats = summary.get("findings_by_scientific_class") or {}
    if cats:
        L += ["", "## Findings by scientific category", "",
              "| Category | Findings |", "|---|---:|"]
        L += [f"| {k} | {n} |" for k, n in cats.items()]

    res = summary.get("targets_by_resolution_state") or {}
    if res:
        L += ["", "## What became of the questions", "",
              "| Resolution | Targets |", "|---|---:|"]
        L += [f"| {k} | {n} |" for k, n in res.items()]

    L += ["", "## Aggregate", "",
          f"- finding verification rate: {summary['finding_verification_rate']} "
          f"({summary['findings_kept']} kept, {summary['findings_dropped_unsubstantiated']} "
          f"dropped as unsubstantiated)",
          # NOT called coverage, and the sentence says why. Both terms are the same
          # object list, so this rate rises when extraction degrades.
          f"- objects this harness minted that were addressable: "
          f"{summary['objects_addressable_rate']} "
          f"({summary['targets_addressable']}/{summary['targets_discovered']}) - a "
          f"self-referential rate over our own object list, NOT coverage",
          f"- execution-trigger rate: {summary['execution_trigger_rate']}",
          f"- settled without execution: {summary['targets_resolved_without_execution']}",
          f"- provenance violations: {summary['provenance_violations']} (must be 0)",
          f"- conservation violations: "
          f"{len(summary.get('conservation_violations') or []) or 0} (must be 0)"
          + ("".join(f" - {v}" for v in (summary.get('conservation_violations') or []))),
          f"- process guarantees unmet on at least one paper: "
          f"{', '.join(summary.get('guarantees_unmet') or []) or 'none'} (a defect in this "
          f"harness, never a finding about a paper)",
          f"- reviewer report vs machine artifacts: "
          f"{summary['reviewer_report_chars']} chars against "
          f"{summary['machine_report_chars'] + summary['ledger_chars']} "
          f"(ratio {summary['compaction_ratio']})",
          "", "## Not measured", ""]
    L += [f"- {x}" for x in summary["limitations"]]
    L.append("")
    return "\n".join(L)


def run(cfg: Config, pids: list[str], out: Path | None = None) -> dict:
    summary = corpus(cfg, pids)
    out = out or (cfg.projects_dir.parent / "reports")
    out.mkdir(parents=True, exist_ok=True)
    (out / "system_evaluation.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "system_evaluation.md").write_text(render(summary), encoding="utf-8")
    summary["paths"] = {"json": str(out / "system_evaluation.json"),
                        "md": str(out / "system_evaluation.md")}
    return summary


# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # self-check: python -m harness.evaluation
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td) / "projects")
        (cfg.projects_dir / "p" / "reports").mkdir(parents=True)
        report = EvalReport(paper_id="p", title="t", verdict="GREEN", triage="YELLOW",
                            dropped_findings=3,
                            findings=[], reproduction_status="NOT_ATTEMPTED")
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
        assert s["triage"] == {"RED": 0, "YELLOW": 1, "GREEN": 0}
        assert s["finding_verification_rate"] == 0.0, "0 kept of 3 substantiation attempts"
        assert s["objects_addressable_rate"] == 0.6
        assert s["execution_trigger_rate"] == 0.2
        assert s["provenance_violations"] == 0
        assert s["compaction_ratio"] is not None and s["compaction_ratio"] < 0.1
        assert "ground truth" in " ".join(s["limitations"])
        text = render(s)
        assert "| `p` |" in text and "Not measured" in text
        assert "accuracy" not in text.lower().split("Not measured")[0], (
            "no accuracy number may appear above the limitations")
    print("harness.evaluation self-check ok")
