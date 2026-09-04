#!/usr/bin/env python
"""single-harness CLI — an autonomous replication auditor for ML papers.

    python run.py review --paper a.pdf b.pdf c.pdf [--auto-audit] [--auto-grade]
    python run.py dossier [<paper-id> ...]
    python run.py stage <ingest|audit|grade|probe|report> --paper <pdf-or-id>
    python run.py list
    python run.py status <paper-id>

`review` is the entrypoint. Everything else is a way to look at what it did, or to
re-run one stage by hand while debugging.

Exit codes:  0 complete · 2 waiting on lens evidence · 1 error ·
             3 complete but CONTESTED (the independent substantive read disagrees
             sharply with the deterministic verdict — see `EvalReport.verdict_contested`).

This file formats; `harness/controller.py` decides. Run with the repo venv:
    ../.venv/Scripts/python.exe run.py ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness import controller, dossier, state, verdict_driver
from harness.config import Config
from harness.stages import audit as audit_stage
from harness.stages import grade as grade_stage
from harness.stages import ingest as ingest_stage
from harness.stages import probe as probe_stage
from harness.stages import report as report_stage

STAGES = {"ingest": ingest_stage.run_ingest, "audit": audit_stage.run_audit,
          "grade": grade_stage.run_grade, "probe": probe_stage.run,
          "report": report_stage.run_report}


def _echo_steps(prefix: str, res: dict) -> None:
    for step in res.get("steps", []):
        detail = {k: v for k, v in step.items() if k != "stage"}
        print(f"{prefix}[{step['stage']}] {json.dumps(detail, default=str)[:300]}",
              file=sys.stderr)


def _detail(res: dict, stage: str) -> dict:
    """The most recent step for one stage label. The step dicts already carry every
    stage's own compact result, so nothing has to be threaded up separately."""
    return next((s for s in reversed(res.get("steps", [])) if s.get("stage") == stage), {})


def cmd_review(args: argparse.Namespace) -> int:
    """Review one or many papers. More than one runs the batch and writes a dossier."""
    cfg = Config.load()
    if args.require_grades:
        cfg.require_grades = True
    opts = dict(force_probe=args.force_probe, skip_probe=args.skip_probe,
                auto_audit=args.auto_audit, auto_grade=args.auto_grade)

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
        if res.get("verdict_contested"):
            print("\n🚩 CONTESTED — the independent substantive read disagrees sharply "
                 "with this verdict; see the report.")
        if unmet := _detail(res, "S4 report").get("self_audit_failed"):
            print(f"\n⚠️  Reviewer self-audit: {len(unmet)} check(s) unmet "
                 f"({', '.join(unmet)}) — see '## Reviewer self-audit' in the report. "
                 f"The verdict is unaffected; the review is not claiming to be complete.")
        print(f"\n=== {res['verdict']} — {res['title']} ===")
        print(f"{res['reason']}\n")
        print(Path(res["report_md"]).read_text(encoding="utf-8"))
        if res.get("verdict_contested"):
            return 3
        return 0

    out = Path(args.out) if args.out else None
    res = controller.review_papers(cfg, args.paper, dossier_out=out, **opts)
    for r in res["results"]:
        _echo_steps(f"[{r.get('paper_id', r['input'])}] ", r)

    # CORPUS ACCOUNTING FIRST, and by REQUEST rather than by result. "5 papers reviewed"
    # when six were asked for is the summary shape this block exists to make impossible:
    # every requested paper appears on exactly one line, and `harness.corpus.account` has
    # already asserted that the states sum to the request count.
    corpus = res.get("corpus") or {}
    if corpus:
        print(f"\n=== CORPUS: {corpus.get('summary', '')} ===")
        for e in corpus.get("entries", []):
            extra = e.get("verdict") or e.get("failure_kind") or ""
            note = f" · {e['resume_after']}" if e.get("resume_after") else ""
            print(f"  {e['state']:<13} {(e.get('paper_id') or e['source'])[:34]:<34} "
                  f"{extra:<20} {e.get('reason', '')[:70]}{note}")
        if not corpus.get("complete"):
            print(f"  ⚠️  {corpus['requested'] - corpus['counts'].get('completed', 0)} of "
                  f"{corpus['requested']} requested paper(s) did NOT complete.")
        if p := res.get("corpus_path"):
            print(f"  machine-readable: {p}")

    print(f"\n=== {res['papers']} paper(s) ===")
    print(f"complete    : {', '.join(res['complete']) or '(none)'}")
    for pid, lenses in res["needs_audit"].items():
        print(f"needs audit : {pid:<28} {len(lenses)} lens(es): {', '.join(lenses)}")
    for pid, err in res["errors"].items():
        print(f"error       : {pid:<28} {err}")
    for pid, why in res.get("reproduction", {}).items():
        print(f"reproduction: {pid:<28} {why}")
    contested = [r.get("paper_id") or r.get("input") for r in res["results"]
                if r.get("verdict_contested")]
    for pid in contested:
        print(f"🚩 contested : {pid:<28} independent read disagrees sharply with the verdict")

    if d := res.get("dossier"):
        if d.get("skipped"):
            print(f"\ndossier     : not written — {d['skipped']}")
        else:
            print(f"\ndossier     : {d['markdown']}")
            pdf_line = d["pdf"] or f"(PDF not written - {d['pdf_error']})"
            print(f"              {pdf_line}")
            print(f"totals      : {d['totals']['FATAL']} FATAL / {d['totals']['MAJOR']} MAJOR / "
                  f"{d['totals']['MINOR']} MINOR · {d['dropped']} dropped as unsubstantiated")
        if d["missing"]:
            print(f"not in matrix: {', '.join(d['missing'])}")

    return 1 if res["errors"] else (2 if res["needs_audit"] else (3 if contested else 0))


def cmd_accept(args: argparse.Namespace) -> int:
    """Validate and seal lens files a reviewer produced OUTSIDE the auto-audit path.

    The manual channel has always been first-class — `review` without `--auto-audit`
    writes `audit/prompts/<lens>.md`, exits 2, and resumes once the lens files exist —
    but the only way to actually SEAL one was to import `stages.audit.accept_lens` from
    Python. So the documented path required writing code, and the obvious alternative
    (drop the JSON straight into `audit/<lens>.json`) is exactly the side-write that
    `lens_is_accepted` refuses, because it skips `parse_lens_json`'s validation and
    leaves no provenance record. This is that gate as a command.

    Reads from a staging directory rather than accepting inline JSON: a lens report runs
    to tens of kilobytes, which does not belong on a command line, and staging-then-
    promoting is the same discipline `audit_driver.run_lens` already uses.
    """
    cfg = Config.load()
    root = state.project_dir(cfg, args.paper)
    accepted, refused = {}, {}

    def take(src: Path, label: str, fn) -> None:
        try:
            accepted[label] = fn(src.read_text(encoding="utf-8"))
        except Exception as e:
            # Kept, not deleted: "the reviewer produced something and it was not a
            # report" is worth reading — the same reasoning `run_lens` applies to its
            # own `.rejected.txt`.
            refused[label] = str(e)
            src.replace(src.with_suffix(".rejected.txt"))
        else:
            # Consumed. `run_lens` unlinks its staging file after promotion for the same
            # reason: a staged file that survives promotion gets re-promoted on the next
            # invocation, rewriting a sealed artifact's sidecar with a fresh timestamp
            # and making a re-run look like new work. The promoted copy is the record.
            src.unlink(missing_ok=True)

    lens_dir = Path(args.staged) if args.staged else root / "audit" / ".staged"
    for lens in sorted(audit_stage.LENSES):
        if (src := lens_dir / f"{lens}.json").exists():
            take(src, f"lens:{lens}",
                 lambda raw, ln=lens: str(audit_stage.accept_lens(
                     cfg, args.paper, ln, raw, reviewer=args.reviewer,
                     tool_policy=args.tool_policy)["findings"]) + " finding(s)")

    # Grades live one level down, keyed by candidate slug rather than by lens name, so
    # this takes whatever is there instead of iterating a known vocabulary.
    grade_dir = root / "audit" / "grade" / ".staged"
    for src in sorted(grade_dir.glob("*.json")) if grade_dir.is_dir() else []:
        take(src, f"grade:{src.stem}",
             lambda raw, s=src.stem: grade_stage.accept_grade(
                 cfg, args.paper, s, raw, grader=args.reviewer,
                 tool_policy=args.tool_policy)["verdict"])

    # The whole-paper read: one per paper, so a single staged file rather than a directory.
    if (src := root / "reports" / ".staged" / "substantive.json").exists():
        take(src, "whole-paper",
             lambda raw: verdict_driver.accept_verdict(
                 cfg, args.paper, raw, reader=args.reviewer,
                 tool_policy=args.tool_policy)["verdict"])

    for label, what in accepted.items():
        print(f"accepted : {label:<34} {what}")
    for label, why in refused.items():
        print(f"REFUSED  : {label:<34} {why[:140]}")
    if not accepted and not refused:
        print(f"nothing staged in {lens_dir} or {grade_dir}")
        return 1
    return 1 if refused else 0


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
    rv.add_argument("--auto-grade", action="store_true",
                    help="independently grade FATAL/MAJOR findings with a second, blinded "
                         "reviewer instead of counting them as asserted (needs "
                         "SH_ALLOW_GRADING=1; spends tokens)")
    rv.add_argument("--require-grades", action="store_true",
                    help="block the report (waiting, not partial) until every in-scope "
                         "candidate is graded, instead of falling back to lens-asserted "
                         "severity for whatever could not be graded this run")
    rv.add_argument("--force-probe", action="store_true",
                    help="run the reproduction probe even with nothing settleable")
    rv.add_argument("--skip-probe", action="store_true", help="never run the probe")
    rv.add_argument("--out", help="directory for the dossier (default: reports/)")
    rv.set_defaults(func=cmd_review)

    ac = sub.add_parser("accept", help="validate + seal lens files written outside --auto-audit")
    ac.add_argument("--paper", required=True, help="case id")
    ac.add_argument("--staged", help="directory holding <lens>.json "
                                     "(default: projects/<pid>/audit/.staged)")
    ac.add_argument("--reviewer", default="", help="what produced these, for the sidecar")
    ac.add_argument("--tool-policy", default="unrecorded",
                    help="what isolation the reviewer actually ran under. Defaults to "
                         "'unrecorded' rather than to anything reassuring: unlike the "
                         "auto-audit path, nothing here can prove a sandbox was enforced.")
    ac.set_defaults(func=cmd_accept)

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
