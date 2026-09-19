#!/usr/bin/env python
"""single-harness CLI — an autonomous replication auditor for ML papers.

    python run.py review --paper a.pdf b.pdf c.pdf [--auto-audit] [--auto-grade]
    python run.py dossier [<paper-id> ...]
    python run.py stage <ingest|audit|grade|probe|report> --paper <pdf-or-id>
    python run.py list
    python run.py status <paper-id>
    python run.py sandbox [--release]      # leased remote machines

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
            print("\nCONTESTED - the independent substantive read disagrees sharply "
                 "with this verdict; see the report.")
        if unmet := _detail(res, "S4 report").get("self_audit_failed"):
            print(f"\nWARNING: reviewer self-audit: {len(unmet)} check(s) unmet "
                 f"({', '.join(unmet)}) — see '## Reviewer self-audit' in the report. "
                 f"The verdict is unaffected; the review is not claiming to be complete.")
        cats = _detail(res, "S4 report").get("scientific_classes") or {}
        summary = ", ".join(f"{n} {k.replace('_', ' ').lower()}"
                            for k, n in cats.items()) or "no findings survived verification"
        print(f"\n=== {res['title']} ===")
        # THE DISPOSITION FIRST, because it is the one line a caller acts on. It is not a
        # verdict and does not replace the colour — see `harness/disposition.py` — it says
        # what happens to the paper now.
        rep = _detail(res, "S4 report")
        if disp := rep.get("disposition"):
            basis = rep.get("disposition_basis") or "NONE"
            print(f"disposition : {disp}" + (f"  (established by {basis})"
                                             if basis != "NONE" else ""))
            print(f"              {rep.get('disposition_reason', '')}")
        print(f"{summary}\n")
        # The REVIEWER report, not the machine trace. The two are separate artifacts and
        # printing the trace to a terminal is what invariant 19 exists to stop; the trace
        # is on disk at `report_md` for anyone tracing a line of this.
        review = res.get("reviewer_report_md") or res.get("report_md")
        print(Path(review).read_text(encoding="utf-8"))
        print(f"machine trace: {res['report_md']}")
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
            # DISPOSITION, not verdict. `state` says how the run ended and `disposition`
            # says what happens to the paper; a batch summary that printed only the first
            # made STOP_MATERIAL_FAILURE and BLOCKED_ARTIFACT both read as `completed`.
            extra = e.get("disposition") or e.get("verdict") or e.get("failure_kind") or ""
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
                     tool_policy=args.tool_policy,
                     mode=args.mode)["findings"]) + " finding(s)")

    # A paper long enough to need part-splitting has no `audit/<lens>.json` to stage into
    # until every one of that lens's units is sealed — `accept_lens` above only ever
    # targets the composed file. `tools/subagent_accept.py` is the gate for a part or a
    # synthesis unit (`python tools/subagent_accept.py seal <pid> <unit-id> <staged-file>`
    # or `sweep <pid>` over `audit/.staged_subagent/`); this command does not duplicate it.

    # Grades live one level down, keyed by candidate slug rather than by lens name, so
    # this takes whatever is there instead of iterating a known vocabulary.
    grade_dir = root / "audit" / "grade" / ".staged"
    for src in sorted(grade_dir.glob("*.json")) if grade_dir.is_dir() else []:
        take(src, f"grade:{src.stem}",
             lambda raw, s=src.stem: grade_stage.accept_grade(
                 cfg, args.paper, s, raw, grader=args.reviewer,
                 tool_policy=args.tool_policy, mode=args.mode)["verdict"])

    # The whole-paper read: one per paper, so a single staged file rather than a directory.
    if (src := root / "reports" / ".staged" / "substantive.json").exists():
        take(src, "whole-paper",
             lambda raw: verdict_driver.accept_verdict(
                 cfg, args.paper, raw, reader=args.reviewer,
                 tool_policy=args.tool_policy, mode=args.mode)["verdict"])

    # A driver-supplied faithful reproduction: one spec, one staged file.
    if (src := root / "control" / ".staged" / "spec.json").exists():
        take(src, "spec",
             lambda raw: (
                 f"sealed, sha256={probe_stage.accept_spec(cfg, args.paper, raw, reviewer=args.reviewer, tool_policy=args.tool_policy, mode=args.mode)['content_sha256'][:12]}"
             ))

    from harness import delegation
    if accepted:
        print(f"mode     : {args.mode} — {delegation.ISOLATION_CLAIM.get(args.mode, '?')}")
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


def cmd_preflight(args: argparse.Namespace) -> int:
    """Is every requested paper a distinct document, and which id will each get?

    Runs before a batch, spends nothing, and reviews nothing. Exit 0 when every requested
    file is a distinct document; exit 2 when the batch is refused, which happens when two
    requested files are byte-identical or a file cannot be read.

    A duplicate is refused rather than reviewed twice because it inflates every corpus
    count silently: `allocate_paper_id` correctly recognises the second file as the same
    document and resumes its project, which is right per paper and invisible in aggregate.
    An eight-file request that is seven documents produces seven reviews and a claim about
    eight papers.
    """
    from harness import preflight
    from harness.config import BASE_DIR
    cfg = Config.load()
    papers_dir = BASE_DIR / "papers"
    sources = args.paper or sorted(str(p) for p in papers_dir.glob("*.pdf"))
    if not sources:
        print(f"no PDFs to check: pass --paper, or put some in {papers_dir}")
        return 2
    res = preflight.run(cfg, sources, Path(args.out) if args.out else None)
    print(preflight.render(res))
    print(f"\nwritten: {res['paths']['md']}\n         {res['paths']['json']}")
    return 0 if res["ok"] else 2


def cmd_evaluate(args: argparse.Namespace) -> int:
    """System metrics over the reviewed corpus. Counts artifacts; runs no stage.

    Deliberately separate from `dossier`, which is a reader's summary of the papers. This
    is a summary of the SYSTEM — how much of each paper it could address, how often it
    judged an experiment necessary, which gate stopped the rest — and it reports nothing
    it cannot count. See `harness/evaluation.LIMITATIONS` for what is absent and why.
    """
    from harness import evaluation
    cfg = Config.load()
    res = evaluation.run(cfg, args.papers or reviewed_papers(cfg),
                         Path(args.out) if args.out else None)
    print(evaluation.render(res))
    print(f"\nwritten: {res['paths']['md']}\n         {res['paths']['json']}")
    return 0 if res["papers_measured"] else 1


def reviewed_papers(cfg: Config) -> list[str]:
    """Every case that has an ingested paper.

    `projects/` also holds directories from an earlier research pipeline that were never
    papers under review. Listing by `project.json` alone swept those into the dossier as
    "missing", which is noise about work this harness never did.
    """
    return [m["id"] for m in state.list_projects(cfg)
            if (state.project_dir(cfg, m["id"]) / "paper" / "doc.json").exists()]


def cmd_stage(args: argparse.Namespace) -> int:
    """Run ONE stage and print its compact result. For debugging, not for review.

    Locked exactly the way `controller.step` locks a phase handler —
    `with state.project_lock(cfg, case.paper_id): ...` — so this debug entrypoint cannot
    interleave writes to `control/probe_results.json`, `control/targets/<id>/outcome.json`,
    `artifact/<pid>.route.json`, or any other case-state file with a concurrent `review`
    run or a second `stage` invocation against the same paper. Unlike `controller.step`,
    there is no `CaseState` here to read a `paper_id` off, so the lock key is derived the
    same way `controller.open_case` derives one: `--paper` for the `ingest` stage is a PDF
    path, and the paper id is not allocated until ingestion runs, which is exactly the
    situation `open_case` represents with an empty `case.paper_id` — so the lock key here
    is `""` for that same situation, matching what `step` actually locks on today rather
    than inventing a different key. (Locking on the raw PDF path itself would be worse
    than no lock: `state.project_dir` joins it under `projects_dir` with `Path.__truediv__`,
    which for an ABSOLUTE path silently discards `projects_dir` entirely and resolves to
    the PDF's own path, so `project_lock` would then try to `mkdir` a directory at the
    location of the PDF file itself.) Every other stage's `--paper` is already a case id.
    """
    cfg = Config.load()
    pid = args.paper
    if args.name == "ingest" and controller.is_new_pdf_source(args.paper):
        pid = ""  # not yet ingested — same lock key `step` uses pre-ingest
    with state.project_lock(cfg, pid):
        result = STAGES[args.name](cfg, args.paper)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_sandbox(args: argparse.Namespace) -> int:
    """Show or release leased remote machines. The one command that is about money.

    `stages/probe.run` releases a paper's sandbox in a `finally`, so under normal
    operation there is nothing here to do. This exists for the case that `finally` cannot
    cover — the process was killed, the host lost power, the provider was unreachable at
    teardown — because a sandbox nobody released keeps billing until its own timeout and
    nothing else in this harness would ever mention it again.

    `status` contacts no provider: an operator asking what might still be running needs
    the answer when the network is down too, and `release` is what actually checks.
    """
    from harness import sandbox as sandbox_mod

    cfg = Config.load()
    ok, why = sandbox_mod.driver_status(cfg)
    print(f"driver  : {'usable' if ok else 'unusable — ' + why}")
    print(f"reserve : {sandbox_mod.spec_from_config(cfg).describe()}")
    print(f"backend : SH_EXEC_BACKEND={cfg.exec_backend}  "
          f"sandbox gate {'OPEN' if cfg.allow_sandbox else 'closed'}  "
          f"repo-exec gate {'OPEN' if cfg.allow_repo_exec else 'closed'}")

    sessions = sandbox_mod.list_sessions(cfg.projects_dir)
    if not sessions:
        print("\nno sandbox sessions are recorded")
        return 0
    print(f"\n{len(sessions)} recorded session(s):")
    for s in sessions:
        cost = sandbox_mod.lease_cost_usd(s.spec, s.age_seconds)
        print(f"  {s.sandbox_id:<26} {s.paper_id[:30]:<30} {s.commit[:12]:<13} "
              f"{s.spec.gpu_kind or 'no GPU':<10} {s.age_seconds:>9.0f}s  ~${cost:.4f}")
    if not args.release:
        print("\n(pass --release to terminate them)")
        return 0
    failures = 0
    for sandbox_id, released, detail in sandbox_mod.release_all(cfg.projects_dir):
        print(f"  {'released' if released else 'FAILED  '} {sandbox_id:<26} {detail}")
        failures += 0 if released else 1
    return 1 if failures else 0


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
    # Windows consoles often default to cp1252 while reviewer reports contain Unicode
    # status badges. Keep the report artifact unchanged, but ensure printing it cannot
    # turn a completed review into a process error on a legacy console.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
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
                         "'unrecorded' rather than to anything reassuring: only the "
                         "CLI_SUBPROCESS mode can prove a sandbox was enforced, and "
                         "`harness.delegation` forces this back to 'unrecorded' for every "
                         "other mode rather than trusting what is passed here.")
    ac.add_argument("--mode", default="MANUAL",
                    choices=["CLI_SUBPROCESS", "SESSION_SUBAGENT", "MANUAL"],
                    help="HOW these artifacts were produced. MANUAL is the default because "
                         "it claims the least: a human, or an agent this harness knows "
                         "nothing about. SESSION_SUBAGENT means an isolated subagent the "
                         "controlling session dispatched autonomously, one per task — real "
                         "context isolation, no provable filesystem sandbox. "
                         "CLI_SUBPROCESS is a reviewer process this harness spawned and "
                         "confined itself, and is the only mode entitled to report an "
                         "enforced tool policy. The three are distinct provenance classes "
                         "and are never pooled.")
    ac.set_defaults(func=cmd_accept)

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

    sb = sub.add_parser("sandbox", help="show or release leased remote machines")
    sb.add_argument("--release", action="store_true",
                    help="terminate every recorded session (safe to run twice)")
    sb.set_defaults(func=cmd_sandbox)

    sp = sub.add_parser("status", help="one paper's controller state and history")
    sp.add_argument("paper_id")
    sp.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
