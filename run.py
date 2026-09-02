#!/usr/bin/env python
"""single-harness CLI — an autonomous replication auditor for ML papers.

    python run.py review --paper a.pdf b.pdf c.pdf [--auto-audit]
    python run.py dossier [<paper-id> ...]
    python run.py stage <ingest|audit|probe|report> --paper <pdf-or-id>
    python run.py list
    python run.py status <paper-id>

`review` is the entrypoint. Everything else is a way to look at what it did, or to
re-run one stage by hand while debugging.

Exit codes:  0 complete · 2 waiting on lens evidence · 1 error.

This file formats; `harness/controller.py` decides. Run with the repo venv:
    ../.venv/Scripts/python.exe run.py ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness import controller, dossier, state
from harness.config import Config
from harness.stages import audit as audit_stage
from harness.stages import ingest as ingest_stage
from harness.stages import probe as probe_stage
from harness.stages import report as report_stage

STAGES = {"ingest": ingest_stage.run_ingest, "audit": audit_stage.run_audit,
          "probe": probe_stage.run, "report": report_stage.run_report}


def _echo_steps(prefix: str, res: dict) -> None:
    for step in res.get("steps", []):
        detail = {k: v for k, v in step.items() if k != "stage"}
        print(f"{prefix}[{step['stage']}] {json.dumps(detail, default=str)[:300]}",
              file=sys.stderr)


def cmd_review(args: argparse.Namespace) -> int:
    """Review one or many papers. More than one runs the batch and writes a dossier."""
    cfg = Config.load()
    opts = dict(force_probe=args.force_probe, skip_probe=args.skip_probe,
                auto_audit=args.auto_audit)

    if len(args.paper) == 1:
        res = controller.review(cfg, args.paper[0], **opts)
        _echo_steps("", res)
        if res["status"] == "error":
            print(json.dumps(res, indent=2, default=str))
            return 1
        if res["status"] == "needs_audit":
            print(f"\n=== AUDIT REQUIRED — {res['title']} ===\n")
            print(f"{len(res['awaiting'])} of 4 lenses pending: {', '.join(res['awaiting'])}\n")
            for lens, path in res["prompts"].items():
                print(f"  {lens:<14} {path}")
            print(f"\n{res['next']}")
            return 2
        print(f"\n=== {res['verdict']} — {res['title']} ===")
        print(f"{res['reason']}\n")
        print(Path(res["report_md"]).read_text(encoding="utf-8"))
        return 0

    out = Path(args.out) if args.out else None
    res = controller.review_papers(cfg, args.paper, dossier_out=out, **opts)
    for r in res["results"]:
        _echo_steps(f"[{r.get('paper_id', r['input'])}] ", r)

    print(f"\n=== {res['papers']} paper(s) ===")
    print(f"complete    : {', '.join(res['complete']) or '(none)'}")
    for pid, lenses in res["needs_audit"].items():
        print(f"needs audit : {pid:<28} {len(lenses)} lens(es): {', '.join(lenses)}")
    for pid, err in res["errors"].items():
        print(f"error       : {pid:<28} {err}")
    for pid, why in res.get("reproduction", {}).items():
        print(f"reproduction: {pid:<28} {why}")

    if d := res.get("dossier"):
        print(f"\ndossier     : {d['markdown']}")
        pdf_line = d["pdf"] or f"(PDF not written - {d['pdf_error']})"
        print(f"              {pdf_line}")
        print(f"totals      : {d['totals']['FATAL']} FATAL / {d['totals']['MAJOR']} MAJOR / "
              f"{d['totals']['MINOR']} MINOR · {d['dropped']} dropped as unsubstantiated")
        if d["missing"]:
            print(f"not in matrix: {', '.join(d['missing'])}")

    return 1 if res["errors"] else (2 if res["needs_audit"] else 0)


def cmd_dossier(args: argparse.Namespace) -> int:
    """Consolidate finished reports. Runs no stage."""
    cfg = Config.load()
    res = dossier.build(cfg, args.papers or reviewed_papers(cfg),
                        Path(args.out) if args.out else None)
    print(json.dumps(res, indent=2))
    return 0 if res["papers"] else 1


def reviewed_papers(cfg: Config) -> list[str]:
    """Every case that has an ingested paper.

    `projects/` also holds directories from an earlier research pipeline that were never
    papers under review. Listing by `project.json` alone swept those into the dossier as
    "missing", which is noise about work this harness never did.
    """
    return [m["id"] for m in state.list_projects(cfg)
            if (state.project_dir(cfg, m["id"]) / "paper" / "doc.json").exists()]


def cmd_stage(args: argparse.Namespace) -> int:
    """Run ONE stage and print its compact result. For debugging, not for review."""
    print(json.dumps(STAGES[args.name](Config.load(), args.paper), indent=2, default=str))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = Config.load()
    pids = reviewed_papers(cfg)
    if not pids:
        print("(no papers ingested yet)")
        return 0
    for pid in pids:
        case = controller.load_case(cfg, pid)
        meta = state.load_meta(cfg, pid)
        status = f"{case.status}/{case.phase}" if case else "-"
        print(f"{pid:<30} {status:<18} {case.verdict if case else '':<7} "
              f"{meta.get('direction', '')[:50]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    meta = state.load_meta(cfg, args.paper_id)
    case = controller.load_case(cfg, args.paper_id)
    print(f"paper   : {meta['id']}\ntitle   : {meta.get('direction')}")
    print(f"source  : {meta.get('paper_path', '(unknown)')}")
    if case:
        print(f"phase   : {case.phase}   status: {case.status}   verdict: {case.verdict or '-'}")
        if case.reproduction_class:
            print(f"repro   : {case.reproduction_class}")
        if case.blocked_reason:
            print(f"blocked : {case.blocked_reason}")
        print("--- history ---")
        for e in case.history:
            print(f"  {e.ts}  {e.phase:<8} {e.outcome:<8} (attempt {e.attempt})  {e.reason[:90]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="single-harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    rv = sub.add_parser("review", help="review one or more papers end to end")
    # `action="extend"` so BOTH shapes work and neither loses a paper: `--paper a --paper b`
    # and `--paper a b` both arrive as a list.
    rv.add_argument("--paper", required=True, action="extend", nargs="+",
                    help="PDF path(s) or case id(s); more than one writes a dossier")
    rv.add_argument("--auto-audit", action="store_true",
                    help="delegate the four lenses to a reviewer instead of pausing "
                         "(needs SH_ALLOW_AUTO_AUDIT=1; spends tokens)")
    rv.add_argument("--force-probe", action="store_true",
                    help="run the reproduction probe even with nothing settleable")
    rv.add_argument("--skip-probe", action="store_true", help="never run the probe")
    rv.add_argument("--out", help="directory for the dossier (default: reports/)")
    rv.set_defaults(func=cmd_review)

    ds = sub.add_parser("dossier", help="consolidate finished reports")
    ds.add_argument("papers", nargs="*", help="case ids (default: every reviewed paper)")
    ds.add_argument("--out")
    ds.set_defaults(func=cmd_dossier)

    st = sub.add_parser("stage", help="run ONE stage by hand (debugging)")
    st.add_argument("name", choices=sorted(STAGES))
    st.add_argument("--paper", required=True, help="PDF path (ingest) or case id")
    st.set_defaults(func=cmd_stage)

    sub.add_parser("list", help="list reviewed papers").set_defaults(func=cmd_list)

    sp = sub.add_parser("status", help="one paper's controller state and history")
    sp.add_argument("paper_id")
    sp.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
