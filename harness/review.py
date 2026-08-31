"""The single entrypoint: ingest → audit → conditional probe → report.

One stage in the pipeline cannot be automated away, and pretending otherwise would be
the dishonest move. S2 needs judgement, and the judge is the Claude Code session
driving this CLI — not a subprocess it can spawn. So `review` runs every deterministic
stage, stops once when the lens results are missing, and tells the driver exactly what
to write. Running it again resumes and finishes; every stage is idempotent, so
re-running costs nothing but the stages that had not yet completed.

Two exit states matter to a caller:
    status="needs_audit"  the four lens prompts are ready; perform them, then re-run
    status="complete"     reports/<paper_id>.md exists
"""
from __future__ import annotations

from pathlib import Path

from . import state
from .artifacts import PaperDoc
from .config import Config
from .stages import audit as audit_stage
from .stages import ingest as ingest_stage
from .stages import probe as probe_stage
from .stages import report as report_stage


def _probe_reason(cfg: Config, pid: str, doc: PaperDoc) -> tuple[bool, str]:
    """Is there anything a local rerun could actually settle?

    S3 is conditional by design. Firing a GPU probe when no lens flagged anything
    experimentally checkable burns minutes to measure a noise floor nobody asked for,
    and — worse — puts a 'Measured reproduction' section in the report that reads like
    evidence about the paper when it is nothing of the kind.
    """
    reports, _ = audit_stage.load_reports(cfg, pid, doc)
    verifiable = [f for r in reports for f in r.findings if f.verifiable_by_experiment]
    if not verifiable:
        return False, "no lens flagged a finding as settleable by reproduction"
    top = report_stage.rank(verifiable)[0]
    return True, f"{len(verifiable)} settleable finding(s); probing the top one ({top.finding_id})"


def review(cfg: Config, paper: str, *, force_probe: bool = False,
           skip_probe: bool = False) -> dict:
    """Run the whole pipeline as far as it can go. Safe to call repeatedly."""
    steps: list[dict] = []

    src = Path(paper).expanduser()
    if src.suffix.lower() == ".pdf" or src.exists():
        try:
            ingested = ingest_stage.run_ingest(cfg, str(src))
        except FileNotFoundError as e:
            return {"status": "error", "error": str(e)}
    else:
        # A bare case id: the paper was ingested on an earlier run.
        pid = paper
        if not (state.project_dir(cfg, pid) / "paper" / "doc.json").exists():
            return {"status": "error", "error": f"'{paper}' is neither a PDF path nor an "
                                                f"already-ingested case id"}
        ingested = {"cached": True, "paper_id": pid}
    pid = ingested["paper_id"]
    steps.append({"stage": "S1 ingest", **ingested})

    audited = audit_stage.run_audit(cfg, pid)
    if "error" in audited:
        return {"status": "error", "paper_id": pid, **audited}
    steps.append({"stage": "S2 audit", "awaiting": audited["awaiting"],
                  "complete": audited["complete"]})

    if audited["awaiting"]:
        return {
            "status": "needs_audit", "paper_id": pid, "title": audited["title"],
            "steps": steps, "awaiting": audited["awaiting"],
            "prompts": {ln: audited["prompts"][ln] for ln in audited["awaiting"]},
            "next": (
                f"Perform each pending audit and write projects/{pid}/audit/<lens>.json. "
                f"Run each lens in a SEPARATE turn so the four readings stay independent. "
                f"Then re-run: python run.py review --paper {pid}"
            ),
        }

    doc = PaperDoc(**state.read_json(state.project_dir(cfg, pid) / "paper" / "doc.json"))
    done = (state.project_dir(cfg, pid) / "runs" / pid / "probe_results.json").exists()
    wanted, why = _probe_reason(cfg, pid, doc)
    if skip_probe:
        steps.append({"stage": "S3 probe", "skipped": "--skip-probe"})
    elif done and not force_probe:
        steps.append({"stage": "S3 probe", "cached": True})
    elif wanted or force_probe:
        steps.append({"stage": "S3 probe", "reason": "--force-probe" if force_probe and not wanted else why,
                      **probe_stage.run(cfg, pid)})
    else:
        steps.append({"stage": "S3 probe", "skipped": why})

    synth = report_stage.run_report(cfg, pid)
    if "error" in synth:
        return {"status": "error", "paper_id": pid, "steps": steps, **synth}
    steps.append({"stage": "S4 report", **synth})

    return {"status": "complete", "paper_id": pid, "title": doc.title,
            "verdict": synth["verdict"], "reason": synth["reason"],
            "findings": synth["findings"],
            "dropped_unsubstantiated": synth["dropped_unsubstantiated"],
            "probe": synth["probe"], "report_md": synth["report_md"], "steps": steps}
