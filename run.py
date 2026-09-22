#!/usr/bin/env python
"""single-harness CLI — an autonomous replication auditor for ML papers.

    python run.py review --paper a.pdf b.pdf c.pdf
    python run.py tasks <paper-id-or-pdf> [--json]
    python run.py seal <paper-id> <task-id> <file>
    python run.py dossier [<paper-id> ...]
    python run.py stage <ingest|audit|grade|probe|report> --paper <pdf-or-id>
    python run.py list
    python run.py status <paper-id>

`review` is the entrypoint for a paper that needs no delegated judgement this run (or one
that already has everything sealed). `tasks`/`seal` are the entrypoint for THE one
delegation channel this harness has: the controlling Claude Code session dispatches its
own isolated subagents, one per delegable unit -- see `harness/tasks.py`'s module
docstring for the full protocol. Everything else is a way to look at what happened, or
to re-run one stage by hand while debugging.

Exit codes:  0 complete · 2 waiting on lens evidence · 1 error ·
             3 complete but CONTESTED (the independent substantive read disagrees
             sharply with the deterministic claim status — see
             `EvalReport.verdict_contested`).

This file formats; `harness/pipeline.py` decides. Run with the repo venv:
    ../.venv/Scripts/python.exe run.py ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness import agent, pipeline, routes, state, tasks as tasks_mod
from harness.audit import run_audit, run_grade
from harness.config import Config
from harness.stages import ingest as ingest_stage

STAGES = {"ingest": ingest_stage.run_ingest, "audit": run_audit, "grade": run_grade,
          "probe": routes.run, "report": pipeline.run_report_stage}


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
    opts = dict(force_probe=args.force_probe, skip_probe=args.skip_probe)

    if len(args.paper) == 1:
        res = pipeline.review(cfg, args.paper[0], **opts)
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
            print("\nCONTESTED - the independent substantive read disagrees sharply "
                 "with this claim status; see the report.")
        if unmet := _detail(res, "S4 report").get("self_audit_failed"):
            print(f"\nWARNING: reviewer self-audit: {len(unmet)} check(s) unmet "
                 f"({', '.join(unmet)}) — see '## Reviewer self-audit' in the report. "
                 f"The verdict is unaffected; the review is not claiming to be complete.")
        cats = _detail(res, "S4 report").get("scientific_classes") or {}
        summary = ", ".join(f"{n} {k.replace('_', ' ').lower()}"
                            for k, n in cats.items()) or "no findings survived verification"
        print(f"\n=== {res['title']} ===")
        # THE DISPOSITION FIRST: the one line a caller acts on, a routing description
        # not an accept/reject score.
        rep = _detail(res, "S4 report")
        if disp := rep.get("disposition"):
            basis = rep.get("disposition_basis") or "NONE"
            print(f"disposition : {disp}" + (f"  (established by {basis})"
                                             if basis != "NONE" else ""))
            print(f"              {rep.get('disposition_reason', '')}")
        print(f"{summary}\n")
        # The REVIEWER report, not the machine trace: the trace is on disk at `report_md`.
        review = res.get("reviewer_report_md") or res.get("report_md")
        print(Path(review).read_text(encoding="utf-8"))
        print(f"machine trace: {res['report_md']}")
        if res.get("verdict_contested"):
            return 3
        return 0

    out = Path(args.out) if args.out else None
    res = pipeline.review_papers(cfg, args.paper, dossier_out=out, **opts)
    # A PREFLIGHT REFUSAL has a different shape than a completed batch -- no
    # "papers"/"complete"/"errors" keys, because nothing was allocated.
    if err := res.get("error"):
        print(f"\n=== BATCH REFUSED — nothing was reviewed ===\n{err}")
        return 1
    for r in res["results"]:
        _echo_steps(f"[{r.get('paper_id', r['input'])}] ", r)

    # CORPUS ACCOUNTING FIRST, and by REQUEST rather than by result: every requested
    # paper appears on exactly one line, and `harness.pipeline.account` has already
    # asserted the states sum to the request count.
    corpus = res.get("corpus") or {}
    if corpus:
        print(f"\n=== CORPUS: {corpus.get('summary', '')} ===")
        for e in corpus.get("entries", []):
            # `state` says how the run ended; `disposition` says what happens to the paper.
            extra = e.get("disposition") or e.get("failure_kind") or ""
            note = f" · {e['resume_after']}" if e.get("resume_after") else ""
            print(f"  {e['state']:<13} {(e.get('paper_id') or e['source'])[:34]:<34} "
                  f"{extra:<26} {e.get('reason', '')[:60]}{note}")
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
        print(f"contested : {pid:<28} independent read disagrees sharply with the verdict")

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
    """Validate and seal a driver-supplied FAITHFUL-REPRODUCTION spec staged OUTSIDE the
    lens/grade/verdict/reconstruction/artifact-review protocol. Every OTHER role is
    sealed through `python run.py seal <paper-id> <task-id> <file>` -- see
    `harness/tasks.py`. This command is what remains for `routes.accept_spec`'s manual
    channel: a human- or driver-authored mechanism script that carries no
    lens/grade/verdict shape of its own."""
    cfg = Config.load()
    root = state.project_dir(cfg, args.paper)
    accepted, refused = {}, {}

    def take(src: Path, label: str, fn) -> None:
        try:
            accepted[label] = fn(src.read_text(encoding="utf-8"))
        except Exception as e:
            # Kept, not deleted: "the driver produced something and it was not a spec" is
            # worth reading.
            refused[label] = str(e)
            src.replace(src.with_suffix(".rejected.txt"))
        else:
            # Consumed: a staged file that survives promotion gets re-promoted on the next
            # invocation, rewriting a sealed artifact's sidecar with a fresh timestamp and
            # making a re-run look like new work. The promoted copy is the record.
            src.unlink(missing_ok=True)

    spec_dir = root / "control" / ".staged"
    if (src := spec_dir / "spec.json").exists():
        take(src, "spec",
             lambda raw: (
                 f"sealed, sha256={routes.accept_spec(cfg, args.paper, raw, reviewer=args.reviewer, tool_policy=args.tool_policy, mode=args.mode)['content_sha256'][:12]}"
             ))

    if accepted:
        print(f"mode     : {args.mode} — {agent.ISOLATION_CLAIM.get(args.mode, '?')}")
    for label, what in accepted.items():
        print(f"accepted : {label:<34} {what}")
    for label, why in refused.items():
        print(f"REFUSED  : {label:<34} {why[:140]}")
    if not accepted and not refused:
        print(f"nothing staged in {spec_dir}. For a lens/grade/verdict/reconstruction/"
              f"artifact-review answer, use `python run.py seal` instead.")
        return 1
    return 1 if refused else 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """Advance the deterministic pipeline as far as it goes, then print what is pending."""
    cfg = Config.load()
    res = tasks_mod.advance(cfg, args.paper)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    print(f"paper   : {res['paper_id']}")
    print(f"phase   : {res['phase']}   status: {res['status']}")
    if res.get("blocked_reason"):
        print(f"blocked : {res['blocked_reason']}")
    if not res["tasks"]:
        print("no delegable tasks pending")
        return 0
    print(f"\n{len(res['tasks'])} task(s) pending:\n")
    for t in res["tasks"]:
        after = f"  after: {', '.join(t['after'])}" if t["after"] else ""
        print(f"  {t['id']:<28} [{t['role']}] {t['model']}/{t['effort']}{after}")
        print(f"      read : {t['prompt']}")
        print(f"      write: {t['out']}")
    print(f"\nFor each: have an isolated subagent read `prompt`, write its JSON answer to "
         f"`out`, then run `python run.py seal {args.paper} <task-id> <out-file>`.")
    return 0


def cmd_seal(args: argparse.Namespace) -> int:
    """Validate and seal one staged task answer — the write half of `harness/tasks.py`."""
    cfg = Config.load()
    try:
        rec = tasks_mod.seal(cfg, args.paper, args.task_id, Path(args.file))
    except ValueError as e:
        print(f"REFUSED: {e}")
        return 1
    print(f"sealed {args.paper}/{args.task_id}: {json.dumps(rec, default=str)[:300]}")
    return 0


def cmd_dossier(args: argparse.Namespace) -> int:
    """Consolidate finished reports. Runs no stage."""
    from harness import summarize
    cfg = Config.load()
    res = summarize.build_dossier(cfg, args.papers or reviewed_papers(cfg),
                                  Path(args.out) if args.out else None)
    print(json.dumps(res, indent=2))
    return 0 if res["papers"] else 1


def cmd_preflight(args: argparse.Namespace) -> int:
    """Is every requested paper a distinct document, and which id will each get? Runs
    before a batch, spends nothing, and reviews nothing. Exit 0 when every requested file
    is a distinct document; exit 2 when the batch is refused (two files byte-identical,
    or a file cannot be read)."""
    from harness.config import BASE_DIR
    cfg = Config.load()
    papers_dir = BASE_DIR / "papers"
    sources = args.paper or sorted(str(p) for p in papers_dir.glob("*.pdf"))
    if not sources:
        print(f"no PDFs to check: pass --paper, or put some in {papers_dir}")
        return 2
    res = pipeline.run_preflight(cfg, sources, Path(args.out) if args.out else None)
    print(pipeline.render_preflight(res))
    print(f"\nwritten: {res['paths']['md']}\n         {res['paths']['json']}")
    return 0 if res["ok"] else 2


def cmd_evaluate(args: argparse.Namespace) -> int:
    """System metrics over the reviewed corpus. Counts artifacts; runs no stage.
    Deliberately separate from `dossier`, which is a reader's summary of the papers: this
    is a summary of the SYSTEM, and it reports nothing it cannot count. See
    `harness/summarize.LIMITATIONS` for what is absent and why."""
    from harness import summarize
    cfg = Config.load()
    res = summarize.run_evaluate(cfg, args.papers or reviewed_papers(cfg),
                                 Path(args.out) if args.out else None)
    print(summarize.render_evaluation(res))
    print(f"\nwritten: {res['paths']['md']}\n         {res['paths']['json']}")
    return 0 if res["papers_measured"] else 1


def reviewed_papers(cfg: Config) -> list[str]:
    """Every case that has an ingested paper. `projects/` also holds directories from an
    earlier research pipeline that were never papers under review, which listing by
    `project.json` alone would sweep into the dossier as noise."""
    return [m["id"] for m in state.list_projects(cfg)
            if (state.project_dir(cfg, m["id"]) / "paper" / "doc.json").exists()]


def cmd_stage(args: argparse.Namespace) -> int:
    """Run ONE stage and print its compact result. For debugging, not for review. Locked
    exactly the way `pipeline.step` locks a phase handler, so this debug entrypoint
    cannot interleave writes to any case-state file with a concurrent `review` run or a
    second `stage` invocation against the same paper."""
    cfg = Config.load()
    pid = args.paper
    if args.name == "ingest" and pipeline.is_new_pdf_source(args.paper):
        pid = ""  # not yet ingested — same lock key `step` uses pre-ingest
    with state.project_lock(cfg, pid):
        result = STAGES[args.name](cfg, args.paper)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = Config.load()
    pids = reviewed_papers(cfg)
    if not pids:
        print("(no papers ingested yet)")
        return 0
    for pid in pids:
        case = pipeline.load_case(cfg, pid)
        meta = state.load_meta(cfg, pid)
        status = f"{case.status}/{case.phase}" if case else "-"
        print(f"{pid:<30} {status:<18} {case.disposition if case else '':<22} "
              f"{meta.get('direction', '')[:50]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    meta = state.load_meta(cfg, args.paper_id)
    case = pipeline.load_case(cfg, args.paper_id)
    print(f"paper   : {meta['id']}\ntitle   : {meta.get('direction')}")
    print(f"source  : {meta.get('paper_path', '(unknown)')}")
    if case:
        print(f"phase   : {case.phase}   status: {case.status}   disposition: {case.disposition or '-'}")
        if case.reproduction_class:
            print(f"repro   : {case.reproduction_class}")
        if case.blocked_reason:
            print(f"blocked : {case.blocked_reason}")
        print("--- history ---")
        for e in case.history:
            print(f"  {e.ts}  {e.phase:<8} {e.outcome:<8} (attempt {e.attempt})  {e.reason[:90]}")
    return 0


def main() -> int:
    # Windows consoles often default to cp1252 while reviewer reports contain Unicode.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    p = argparse.ArgumentParser(prog="single-harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    rv = sub.add_parser("review", help="review one or more papers end to end")
    # `action="extend"` so both `--paper a --paper b` and `--paper a b` arrive as a list.
    rv.add_argument("--paper", required=True, action="extend", nargs="+",
                    help="PDF path(s) or case id(s); more than one writes a dossier")
    rv.add_argument("--require-grades", action="store_true",
                    help="block the report (waiting, not partial) until every in-scope "
                         "candidate is graded, instead of falling back to lens-asserted "
                         "severity for whatever could not be graded this run")
    rv.add_argument("--force-probe", action="store_true",
                    help="run the reproduction probe even with nothing settleable")
    rv.add_argument("--skip-probe", action="store_true", help="never run the probe")
    rv.add_argument("--out", help="directory for the dossier (default: reports/)")
    rv.set_defaults(func=cmd_review)

    ac = sub.add_parser("accept", help="validate + seal a driver-supplied spec (see "
                                       "`python run.py seal` for everything else)")
    ac.add_argument("--paper", required=True, help="case id")
    ac.add_argument("--reviewer", default="", help="what produced this, for the sidecar")
    ac.add_argument("--tool-policy", default="unrecorded",
                    help="what isolation the reviewer actually ran under. Defaults to "
                         "'unrecorded' rather than to anything reassuring: no mode this "
                         "command may claim can prove a sandbox was enforced.")
    ac.add_argument("--mode", default="MANUAL",
                    choices=["SESSION_SUBAGENT", "MANUAL"],
                    help="HOW this artifact was produced. MANUAL is the default because it "
                         "claims the least: a human, or an agent this harness knows nothing "
                         "about. SESSION_SUBAGENT means an isolated subagent the "
                         "controlling session dispatched autonomously — real context "
                         "isolation, no provable filesystem sandbox. (CLI_SUBPROCESS is not "
                         "offered here: nothing produces it any more, it only remains "
                         "readable on artifacts sealed before this change.)")
    ac.set_defaults(func=cmd_accept)

    ts = sub.add_parser("tasks", help="list delegable tasks (advances the pipeline first)")
    ts.add_argument("paper", help="PDF path (first ingest) or case id")
    ts.add_argument("--json", action="store_true", help="machine-readable output")
    ts.set_defaults(func=cmd_tasks)

    sl = sub.add_parser("seal", help="validate + seal one staged task answer")
    sl.add_argument("paper", help="case id")
    sl.add_argument("task_id", help="a task id from `python run.py tasks <paper>`")
    sl.add_argument("file", help="path to the delegate's staged JSON answer")
    sl.set_defaults(func=cmd_seal)

    ds = sub.add_parser("dossier", help="consolidate finished reports")
    ds.add_argument("papers", nargs="*", help="case ids (default: every reviewed paper)")
    ds.add_argument("--out")
    ds.set_defaults(func=cmd_dossier)

    pf = sub.add_parser("preflight",
                        help="check a batch is N distinct papers before reviewing it")
    pf.add_argument("--paper", nargs="*", help="PDF paths (default: every PDF in papers/)")
    pf.add_argument("--out")
    pf.set_defaults(func=cmd_preflight)

    ev = sub.add_parser("evaluate", help="system metrics over the reviewed corpus")
    ev.add_argument("papers", nargs="*", help="case ids (default: every reviewed paper)")
    ev.add_argument("--out")
    ev.set_defaults(func=cmd_evaluate)

    st = sub.add_parser("stage", help="run ONE stage by hand (debugging)")
    st.add_argument("name", choices=sorted(STAGES))
    st.add_argument("--paper", required=True, help="PDF path (ingest) or case id")
    st.set_defaults(func=cmd_stage)

    sub.add_parser("list", help="list reviewed papers").set_defaults(func=cmd_list)

    sp = sub.add_parser("status", help="one paper's pipeline state and history")
    sp.add_argument("paper_id")
    sp.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
