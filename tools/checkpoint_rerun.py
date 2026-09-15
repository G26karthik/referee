"""Re-run the corpus under the CURRENT code into a NEW run identity.

    python tools/checkpoint_rerun.py <label>

The pre-fix artifacts under `projects/` are historical experimental evidence and this
script never writes to them. It copies the expensive, model-produced half of each case —
the parsed document, the four sealed lens files, the sealed grades, the sealed whole-paper
assessment — into `runs_postfix/<label>/projects/<pid>/`, and re-derives everything the
deterministic layer owns: assessment, discovery, planning, probing, reconciliation,
reporting.

Copying rather than re-delegating is the point. The reasoning half of the review is
IDENTICAL between the two runs by construction, so any difference in the funnel is
attributable to the code that changed and to nothing else. Re-running the lenses would
spend tokens and would also change the panel, which would make the comparison
uninterpretable.

What is deliberately NOT copied: `controller.json` (the case restarts), `discovery/`,
`runs/` and the derived reports. Those are exactly what is being re-measured.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import controller, corpus as corpus_mod            # noqa: E402
from harness import planner as planner_mod                      # noqa: E402
from harness.config import BASE_DIR, Config                     # noqa: E402
from harness.stages import discover as discover_stage           # noqa: E402

CORPUS = ("0c06a98d7c818f6f", "2024-icml-sapg", "5993d35ff0996b52", "acl",
          "apt-icml", "cvpr", "iclr", "sanchez24a-icml")

# The sealed model outputs. Everything else in a case directory is derived.
COPY_DIRS = ("paper", "audit")
COPY_FILES = ("project.json",)
COPY_GLOBS = ("reports/*.substantive.json", "reports/*.substantive.driver.json")


def stage(label: str) -> Path:
    src_root = BASE_DIR / "projects"
    dst_root = BASE_DIR / "runs_postfix" / label / "projects"
    dst_root.mkdir(parents=True, exist_ok=True)
    for pid in CORPUS:
        src, dst = src_root / pid, dst_root / pid
        if dst.exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for d in COPY_DIRS:
            if (src / d).is_dir():
                shutil.copytree(src / d, dst / d, dirs_exist_ok=True)
        for f in COPY_FILES:
            if (src / f).is_file():
                shutil.copy2(src / f, dst / f)
        for pattern in COPY_GLOBS:
            for f in src.glob(pattern):
                (dst / "reports").mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst / "reports" / f.name)
        (dst / "research_log.jsonl").touch()
    return dst_root


def funnel(cfg: Config) -> dict:
    """The six terms plus the new `bound`, read off the target sets this run wrote."""
    total = {"discovered": 0, "checkable": 0, "warranting_experiment": 0,
             "launched": 0, "completed": 0, "resolved": 0,
             "processes": 0, "superseded": 0,
             # Step 5's own terms. `with_question` is the invariant "every executable
             # target names the question it is spending compute on", counted rather than
             # asserted so a rerun can show it held; `comparison_blocked` is the new
             # refusal, and it can never exceed what was warranted.
             "with_question": 0, "comparison_blocked": 0}
    kinds: dict[str, int] = {}
    per_paper = {}
    for pid in CORPUS:
        ts = discover_stage.load(cfg, pid)
        if ts is None:
            continue
        outs = list(ts.outcomes)
        cov = dict(ts.extraction_coverage or {})
        # THE CURRENT plan per target, not the raw log. Step 6's route fallback appends a
        # SECOND `PlanDecision` for a target whose AUTHOR_CODE_EXECUTION identity failed,
        # rather than replacing the first — see `planner.current_plans` — so summing
        # `ts.plans` directly here double-counted exactly one re-planned target as two
        # warranting an experiment (88 -> 89 on this corpus, for zero new targets).
        live_plans = planner_mod.current_plans(ts.plans)
        row = {
            "discovered": len(ts.objects),
            "checkable": sum(1 for o in ts.objects if o.harness_addressable),
            "warranting_experiment": sum(1 for p in live_plans if p.requires_execution),
            "launched": sum(1 for o in outs if o.launched > 0),
            "processes": sum(o.launched for o in outs),
            "completed": sum(1 for o in outs
                             if o.reconciliation is not None
                             and o.reconciliation.status != "NOT_ATTEMPTED"),
            "resolved": sum(1 for o in outs if o.resolution_state.startswith("RESOLVED")),
            "superseded": sum(1 for o in outs
                              if o.disposition == "SUPERSEDED_BY_ESTABLISHED_FAILURE"),
            "with_question": cov.get("executable_targets_with_a_question", 0),
            "comparison_blocked": sum(1 for o in outs
                                      if o.disposition == "COMPARISON_BLOCKED"),
        }
        per_paper[pid] = dict(row, question_kinds=dict(
            cov.get("executable_question_kinds", {}) or {}))
        for k, v in row.items():
            total[k] += v
        for k, v in (cov.get("executable_question_kinds", {}) or {}).items():
            kinds[k] = kinds.get(k, 0) + v
    return {"total": total, "question_kinds": kinds, "per_paper": per_paper}


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "checkpoint"
    dst_root = stage(label)
    cfg = Config.load()
    cfg.projects_dir = dst_root
    print(f"run identity: {dst_root}")
    print(f"gates: repo_exec={cfg.allow_repo_exec} network={cfg.allow_network} "
          f"install={cfg.allow_install} synthesis={cfg.allow_synthesis} "
          f"diagnostic={cfg.diagnostic_mode}")

    cases = controller.drive_all(cfg, list(CORPUS))
    accounting = corpus_mod.account(list(CORPUS), list(cases))
    print(f"\n{accounting.summary}")
    for e in accounting.entries:
        print(f"  {e.state:<12} {e.paper_id:<20} {e.disposition:<26} {e.verdict}")

    out = {"label": label, "corpus": accounting.model_dump(), "funnel": funnel(cfg),
           "gates": {"allow_repo_exec": cfg.allow_repo_exec,
                     "allow_network": cfg.allow_network,
                     "allow_install": cfg.allow_install,
                     "allow_synthesis": cfg.allow_synthesis,
                     "diagnostic_mode": cfg.diagnostic_mode,
                     "max_targets": cfg.max_targets}}
    path = dst_root.parent / "funnel.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    t = out["funnel"]["total"]
    print(f"\nfunnel: discovered {t['discovered']} -> checkable {t['checkable']} -> "
          f"warranting {t['warranting_experiment']} -> launched {t['launched']} -> "
          f"completed {t['completed']} | resolved {t['resolved']} | "
          f"processes {t['processes']} | superseded {t['superseded']}")
    print(f"question-centric: warranted targets naming a question "
          f"{t['with_question']}/{t['warranting_experiment']} | comparison blocked "
          f"{t['comparison_blocked']}")
    for k, v in sorted(out["funnel"]["question_kinds"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:<24} {v}")
    print(f"written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
