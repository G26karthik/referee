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

`auto_audit=True` is the one way that pause can be skipped, and it does not remove the
judgement — it delegates it to a command the operator configures (`harness/audit_driver`).
The default is still to stop, because the harness has no reviewer of its own to call and
manufacturing one would be the dishonest move this module was written to avoid.

`review_suite` runs many papers in one pass. It does not make the audit stage
unnecessary; it makes the pause happen ONCE for a whole batch instead of once per paper,
which is the part of the manual loop that actually cost time.
"""
from __future__ import annotations

from pathlib import Path

from . import audit_driver, state
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
           skip_probe: bool = False, auto_audit: bool = False) -> dict:
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

    # Optional S2 autonomy. Off unless the operator opened the gate AND configured a
    # reviewer command; see `audit_driver`. A lens that the command fails to produce stays
    # pending and the pipeline falls back to the normal manual stop, because a lens that
    # silently became an empty findings list would read downstream as "nothing was wrong".
    auto: dict = {}
    if auto_audit and audited["awaiting"]:
        ok, why = audit_driver.available(cfg)
        if not ok:
            auto = {"attempted": False, "reason": why}
        else:
            auto = {"attempted": True, **audit_driver.fill(
                cfg, pid, audited["awaiting"], audited["prompts"])}
            audited = audit_stage.run_audit(cfg, pid)
            if "error" in audited:
                return {"status": "error", "paper_id": pid, **audited}

    steps.append({"stage": "S2 audit", "awaiting": audited["awaiting"],
                  "complete": audited["complete"],
                  **({"auto_audit": auto} if auto else {})})

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


def review_suite(cfg: Config, papers: list[str], *, force_probe: bool = False,
                 skip_probe: bool = False, auto_audit: bool = False,
                 dossier_out: Path | None = None) -> dict:
    """Run `review` over a batch, then consolidate whatever finished into one dossier.

    One paper failing does not stop the batch: a bad PDF or a half-written lens file is
    a fact about that paper, not a reason to abandon the other nine. Papers that stop for
    audits are collected and reported together at the end, so the operator performs one
    round of judgement for the whole suite rather than being interrupted per paper.

    The dossier is built from the papers that reached `complete`. Papers that did not are
    passed to `dossier.build` as well, so they are listed as missing rather than quietly
    dropped — a summary that omits its failures is worse than no summary.
    """
    from . import dossier as dossier_mod

    results: list[dict] = []
    for paper in papers:
        res = review(cfg, paper, force_probe=force_probe, skip_probe=skip_probe,
                     auto_audit=auto_audit)
        res["input"] = paper
        results.append(res)

    complete = [r for r in results if r["status"] == "complete"]
    pending = [r for r in results if r["status"] == "needs_audit"]
    errors = [r for r in results if r["status"] == "error"]

    out = {
        "papers": len(results),
        "complete": [r["paper_id"] for r in complete],
        "needs_audit": {r["paper_id"]: r["awaiting"] for r in pending},
        "errors": {r.get("paper_id", r["input"]): r.get("error") for r in errors},
        "results": results,
    }
    # Every paper that produced a paper_id is offered to the dossier; the ones without a
    # finished report come back in its `missing` list.
    ordered = [r["paper_id"] for r in results if r.get("paper_id")]
    if ordered:
        out["dossier"] = dossier_mod.build(cfg, ordered, dossier_out)
    return out
