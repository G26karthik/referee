"""The per-paper measurement the final run is for. Reads artifacts, decides nothing.

`python tools/final_run_measure.py [run_dir]` prints the six blocks of section 13 and
writes `<run_dir>/reports/final_measurement.json`.

**Every number here is read off one artifact and is never a sum of two different ones.**
That is the same discipline `CaseLedger.efficiency` keeps and for the same reason: the
funnel's six terms come from six different files, and a measurement that adds a judgement
to a count reports an intention as a result.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAPERS = ["0c06a98d7c818f6f", "2024-icml-sapg", "5993d35ff0996b52", "acl",
          "apt-icml", "cvpr", "iclr", "sanchez24a-icml"]


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                              # noqa: BLE001 — a missing file is a fact
        return None


def measure(run_dir: Path, pid: str) -> dict:
    root = run_dir / "projects" / pid
    doc = _load(root / "paper" / "doc.json") or {}
    ts = _load(root / "discovery" / "targets.json") or {}
    rep = _load(root / "reports" / f"{pid}.json") or {}
    ledger = _load(root / "reports" / f"{pid}.ledger.json") or {}
    ctrl = _load(root / "controller.json") or {}
    search = _load(root / "literature" / f"{pid}.search.json")
    inspection = _load(root / "artifact" / f"{pid}.inspection.json")

    objects = ts.get("objects") or []
    plans = ts.get("plans") or []
    outcomes = ts.get("outcomes") or []
    questions = ts.get("questions") or []
    findings = rep.get("findings") or []
    # THE PLAN CURRENTLY IN FORCE PER TARGET, never the whole log: a re-planned target has
    # two entries and counting both reports one judgement twice.
    current = {p.get("target_id"): p for p in plans}.values()
    by_disp: dict[str, int] = {}
    for o in outcomes:
        by_disp[o.get("disposition", "?")] = by_disp.get(o.get("disposition", "?"), 0) + 1

    cov = ts.get("extraction_coverage") or {}
    links = _load(root / "links" / f"{pid}.claimlinks.json") or {}
    accepted = [l for l in (links.get("links") or []) if l.get("accepted")]

    return {
        "paper_id": pid,
        "terminal": {"phase": ctrl.get("phase"), "status": ctrl.get("status"),
                     "failure_kind": ctrl.get("failure_kind") or "",
                     "disposition": ctrl.get("disposition"),
                     "verdict": rep.get("verdict") or ctrl.get("verdict") or ""},
        # READING
        "reading": {
            "pages": doc.get("n_pages") or 0,
            "sections": len(doc.get("sections") or []),
            "reader_visible_fraction": cov.get("reader_visible_fraction"),
            "prose_presented": cov.get("prose_presented"),
            "parts": cov.get("reading_parts"),
            "syntheses": cov.get("reading_syntheses"),
        },
        # REVIEW
        "review": {
            "proposals": rep.get("findings_proposed"),
            "retained": len(findings),
            "dropped": rep.get("dropped_findings"),
            "graded": sum(1 for f in findings if (f.get("grade") or {}).get("verdict")),
        },
        # CLAIMS
        "claims": {
            "objects": len(objects),
            "questions": len(questions),
            "links_proposed": links.get("proposed"),
            "links_endpoints_verified": sum(
                1 for l in accepted if l.get("authority") == "ENDPOINTS_VERIFIED_SEMANTIC_LINK"),
            "links_structurally_bound": sum(
                1 for l in accepted if l.get("authority") == "STRUCTURALLY_BOUND_LINK"),
        },
        # LITERATURE
        "literature": {
            "claims_searched": len((search or {}).get("claims_searched") or []),
            # `ProviderCall.completed` is a derived PROPERTY and is not in the JSON; the
            # serialised field is `outcome`. Reading the absent key gave False on a
            # protocol that had in fact completed, which is the direction this number
            # must never fail in — `protocol_completed` is what makes "exhausted" mean
            # anything, and understating it is as misleading as overstating it.
            "protocol_completed": bool((search or {}).get("provider_calls")
                                       and all(c.get("outcome") == "COMPLETED"
                                               for c in (search or {}).get("provider_calls") or [])),
            "provider_calls": len((search or {}).get("provider_calls") or []),
            "raw_records": (search or {}).get("raw_results", 0),
            "candidates_reviewed": (search or {}).get("candidates_reviewed", 0),
            "proposals": (search or {}).get("proposed", 0),
            "discharged": bool((search or {}).get("discharged")),
            "works": len((search or {}).get("works") or []),
            "concerns": sum(1 for f in ((search or {}).get("facts") or [])
                            if f.get("authority") in ("ENDPOINTS_VERIFIED_LITERATURE_CONCERN",
                                                      "STRUCTURALLY_BOUND_PRIOR_ART")),
            "bound": sum(1 for f in ((search or {}).get("facts") or [])
                         if f.get("authority") == "STRUCTURALLY_BOUND_PRIOR_ART"),
        },
        # ARTIFACTS
        "artifacts": {
            "code_available": bool(doc.get("repo_url")),
            "facts": len((inspection or {}).get("facts") or []),
            "concerns": sum(1 for f in ((inspection or {}).get("facts") or [])
                            if f.get("authority") == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"),
            "mismatches": sum(1 for f in ((inspection or {}).get("facts") or [])
                              if f.get("authority") == "PAPER_ARTIFACT_MISMATCH"),
        },
        # INVESTIGATION
        "investigation": {
            "material_questions": sum(1 for q in questions
                                      if q.get("materiality") == "CENTRAL"),
            "routes_applicable": len(ts.get("route_attempts") or []),
            "routes_completed": sum(1 for a in (ts.get("route_attempts") or [])
                                    if a.get("completed")),
            "external_blockers": sum(1 for a in (ts.get("route_attempts") or [])
                                     if a.get("state") == "DISCHARGED_BLOCKED"),
            "configuration_blockers": sum(1 for a in (ts.get("route_attempts") or [])
                                          if a.get("state") == "GATE_CLOSED"),
        },
        # EXECUTION — judged and actual are two numbers and are never the same field.
        "execution": {
            "warranting": sum(1 for p in current if p.get("requires_execution")),
            "author_code_launches": sum(o.get("launched", 0) for o in outcomes
                                        if o.get("route") == "AUTHOR_CODE_EXECUTION"),
            "reconstruction_launches": sum(o.get("launched", 0) for o in outcomes
                                           if o.get("route") == "INDEPENDENT_RECONSTRUCTION"),
            "validation_launches": sum(o.get("launched", 0) for o in outcomes
                                       if o.get("route") == "FOCUSED_VALIDATION_EXPERIMENT"),
            "measurements": sum(1 for o in outcomes if (o.get("reconciliation") or {}).get(
                "reproduced_value") is not None),
            "reconciliations": sum(1 for o in outcomes
                                   if (o.get("reconciliation") or {}).get("status")
                                   in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION")),
        },
        # DECISION
        "decision": {
            "established_defects": sum(1 for o in outcomes if o.get("disposition") in (
                "FAILED_REPRODUCTION", "PAPER_ARITHMETIC_CONTRADICTION",
                "VALIDATION_DEFECT_ESTABLISHED")),
            "claim_status": rep.get("claim_status"),
            "triage": rep.get("triage"),
            "overall_verdict": rep.get("verdict"),
            "unresolved_material": len(rep.get("unresolved_central") or []),
        },
        "dispositions": dict(sorted(by_disp.items())),
        "efficiency": (ledger.get("efficiency") or {}),
    }


def main(argv: list[str]) -> int:
    run_dir = Path(argv[1] if len(argv) > 1 else "runs_final_2026-09-16")
    rows = [measure(run_dir, pid) for pid in PAPERS]
    for r in rows:
        t = r["terminal"]
        e = r["execution"]
        print(f"{r['paper_id']:20s} {str(t['phase']):9s} {str(t['status']):9s} "
              f"findings={r['review']['retained'] or 0:3} "
              f"objects={r['claims']['objects']:4} "
              f"lit={r['literature']['claims_searched']:2} "
              f"art={r['artifacts']['facts']:2} "
              f"launched={e['author_code_launches'] + e['reconstruction_launches'] + e['validation_launches']:3} "
              f"triage={r['decision']['triage']}")
    out = run_dir / "reports" / "final_measurement.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"run": run_dir.name, "papers": rows}, indent=2),
                   encoding="utf-8")
    print("written", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
