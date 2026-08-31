#!/usr/bin/env python
"""single-harness CLI — AI first-round peer reviewer. Fully local, no API keys.

    python run.py review --paper papers/some_paper.pdf     # the whole pipeline

    python run.py node ingest_paper      --paper papers/some_paper.pdf   # S1
    python run.py node audit_paper       --paper some_paper              # S2
    python run.py node run_probe         --paper some_paper              # S3
    python run.py node synthesize_report --paper some_paper              # S4
    python run.py list
    python run.py status <paper-id>

S1, S3 and S4 are deterministic Python. S2 only WRITES the four lens prompts —
the Claude Code session performs the audits and writes `audit/<lens>.json`, then
re-runs `review` to finish. Exit code 2 means "waiting on those audits".

Run with the repo .venv:  ../.venv/Scripts/python.exe run.py ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness import dossier, nodes, review, state
from harness.config import Config

NODES = ("ingest_paper", "audit_paper", "run_probe", "synthesize_report")


def cmd_review(args: argparse.Namespace) -> int:
    """Run every stage that can run, then either stop for the audits or print the report."""
    res = review.review(Config.load(), args.paper,
                        force_probe=args.force_probe, skip_probe=args.skip_probe,
                        auto_audit=args.auto_audit)

    for step in res.get("steps", []):
        detail = {k: v for k, v in step.items() if k != "stage"}
        print(f"[{step['stage']}] {json.dumps(detail, default=str)[:400]}", file=sys.stderr)

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


def cmd_review_suite(args: argparse.Namespace) -> int:
    """Run the whole pipeline over several papers, then write one consolidated dossier.

    Exit 0 when every paper finished, 2 when at least one is still waiting on audits,
    1 when any paper errored — so a caller can branch on the batch the same way it
    branches on a single `review`.
    """
    out = Path(args.out) if args.out else None
    res = review.review_suite(Config.load(), args.papers, force_probe=args.force_probe,
                              skip_probe=args.skip_probe, auto_audit=args.auto_audit,
                              dossier_out=out)

    for r in res["results"]:
        for step in r.get("steps", []):
            detail = {k: v for k, v in step.items() if k != "stage"}
            print(f"[{r.get('paper_id', r['input'])}] [{step['stage']}] "
                  f"{json.dumps(detail, default=str)[:300]}", file=sys.stderr)

    print(f"\n=== SUITE — {res['papers']} paper(s) ===")
    print(f"complete    : {', '.join(res['complete']) or '(none)'}")
    if res["needs_audit"]:
        print("needs audit :")
        for pid, lenses in res["needs_audit"].items():
            print(f"  {pid:<28} {len(lenses)} lens(es) pending: {', '.join(lenses)}")
            print(f"    prompts in projects/{pid}/audit/prompts/ — write "
                  f"projects/{pid}/audit/<lens>.json, one lens per turn, then re-run")
    if res["errors"]:
        print("errors      :")
        for pid, err in res["errors"].items():
            print(f"  {pid:<28} {err}")

    d = res.get("dossier")
    if d:
        print(f"\ndossier     : {d['markdown']}")
        pdf_line = d["pdf"] or f"(PDF not written — {d['pdf_error']})"
        print(f"              {pdf_line}")
        print(f"totals      : {d['totals']['FATAL']} FATAL / {d['totals']['MAJOR']} MAJOR / "
              f"{d['totals']['MINOR']} MINOR · {d['dropped']} dropped as unsubstantiated")
        if d["missing"]:
            print(f"not in matrix: {', '.join(d['missing'])}")

    if res["errors"]:
        return 1
    return 2 if res["needs_audit"] else 0


def cmd_dossier(args: argparse.Namespace) -> int:
    """Consolidate already-finished reports into one dossier. Runs no stage."""
    cfg = Config.load()
    pids = args.papers or [m["id"] for m in state.list_projects(cfg)]
    res = dossier.build(cfg, pids, Path(args.out) if args.out else None)
    print(json.dumps(res, indent=2))
    return 0 if res["papers"] else 1


def cmd_node(args: argparse.Namespace) -> int:
    """Run ONE node and print its compact result as JSON."""
    fn = getattr(nodes, args.name, None)
    if fn is None or args.name not in NODES:
        print(json.dumps({"error": f"unknown node '{args.name}'", "known": list(NODES)}))
        return 1
    # `--paper` is a PDF path for ingest_paper and a case id everywhere else; ingest
    # derives the id from the filename, so one flag covers the whole pipeline.
    print(json.dumps(fn(Config.load(), args.paper), indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = state.list_projects(Config.load())
    if not rows:
        print("(no papers ingested yet)")
        return 0
    for m in rows:
        print(f"{m['id']:<44} {m.get('phase','?'):<10} {m.get('direction','')[:64]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    meta = state.load_meta(cfg, args.paper_id)
    print(f"paper    : {meta['id']}")
    print(f"title    : {meta.get('direction')}")
    print(f"source   : {meta.get('paper_path', '(unknown)')}")
    print(f"phase    : {meta.get('phase')}   status: {meta.get('status')}")
    print("--- log ---")
    for rec in state.read_log(cfg, meta["id"]):
        print(f"  {rec['ts']}  {rec['type']:<16} {rec['phase']:<8} {rec.get('headers', {})}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="single-harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_rv = sub.add_parser("review", help="run the whole pipeline on one paper (the normal entrypoint)")
    p_rv.add_argument("--paper", required=True, help="PDF path, or a case id to resume")
    p_rv.add_argument("--force-probe", action="store_true",
                      help="run the GPU probe even when no finding is settleable (noise-floor calibration)")
    p_rv.add_argument("--skip-probe", action="store_true", help="never run the GPU probe")
    p_rv.add_argument("--auto-audit", action="store_true",
                      help="fill pending lenses by running SH_AUDIT_CMD instead of stopping "
                           "(requires SH_ALLOW_AUTO_AUDIT=1; see harness/audit_driver.py)")
    p_rv.set_defaults(func=cmd_review)

    p_sv = sub.add_parser("review-suite",
                          help="run the pipeline over several papers, then write one dossier")
    p_sv.add_argument("papers", nargs="+", help="PDF paths and/or already-ingested case ids")
    p_sv.add_argument("--force-probe", action="store_true")
    p_sv.add_argument("--skip-probe", action="store_true")
    p_sv.add_argument("--auto-audit", action="store_true",
                      help="fill pending lenses by running SH_AUDIT_CMD instead of stopping")
    p_sv.add_argument("--out", help="directory for the dossier (default: reports/)")
    p_sv.set_defaults(func=cmd_review_suite)

    p_ds = sub.add_parser("dossier",
                          help="consolidate finished reports into one executive dossier")
    p_ds.add_argument("papers", nargs="*",
                      help="case ids (default: every ingested paper)")
    p_ds.add_argument("--out", help="directory for the dossier (default: reports/)")
    p_ds.set_defaults(func=cmd_dossier)

    p_nd = sub.add_parser("node", help="run ONE pipeline node, print compact JSON")
    p_nd.add_argument("name", choices=NODES)
    p_nd.add_argument("--paper", required=True,
                      help="PDF path (ingest_paper) or case id (every other node)")
    p_nd.set_defaults(func=cmd_node)

    sub.add_parser("list", help="list ingested papers").set_defaults(func=cmd_list)

    p_st = sub.add_parser("status", help="show one paper's state + log")
    p_st.add_argument("paper_id")
    p_st.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
