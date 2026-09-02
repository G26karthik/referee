"""S3 — decide what to reproduce, then hand it to the local runner.

The probe targets the highest-ranked finding the lenses marked
`verifiable_by_experiment`. If none is marked, it still runs: a probe with no
intervention measures this machine's seed-noise floor, which is the number every
"is this gain real?" question divides by — worth having even with nothing to test.

A hand-written `spec.json` under `runs/<paper_id>/` overrides all of this, which is
how the driver supplies a faithful reproduction of the paper's actual setup.

**Where `claimed_delta` may come from.** Only two places, both traceable:

  1. The driver writes it into `spec.json` alongside a real reproduction script.
  2. It is read off the `QuantFinding.delta` the extractor already parsed for the
     ADDRESSED CELL the target finding cites — the same provenance chain the audit
     stage enforces on evidence quotes.

It is never mined out of a finding's prose. An earlier version scraped the first
percentage-shaped token out of the finding text, which pulled in whatever number
happened to be nearby — an expert-audit rate, a wall-clock figure — and then, because
the report inferred "calibration run" from `claimed_delta is None`, a successful
scrape silently promoted an identical-arms calibration into something rendered as a
reproduction of the paper. Both halves of that are closed here: a spec with no script
carries no claim at all, and a claim that is not grounded in a cell is not a claim.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import (backends, code_audit, experiment_id, probe_synth, repo as repo_mod,
                resources as resources_mod, state)
from ..artifacts import CodeAudit, Finding, PaperDoc, ProbeSpec, RepoAcquisition
from ..config import Config
from ..local_exec import run_probe as _run
from . import audit as audit_stage
from . import report as report_stage

_CELL_REF = re.compile(r"^T\d+:r\d+:c\d+$")
# A delta is only unambiguous when it carries its scale. "+2.1%" and "+2.1 pp" both
# mean 0.021; a bare "+0.5" could be half a point or fifty, so it is refused.
_SCALED_DELTA = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:%|pp)\s*$", re.IGNORECASE)


def grounded_claimed_delta(doc: PaperDoc, finding: Finding | None) -> float | None:
    """The delta the paper printed for the cell this finding cites, as a fraction.

    Returns None unless the whole chain holds: the finding anchors to a cell address,
    the extractor recorded a number at that address, and that number carries an
    explicit `delta` with a scale on it. Anything less is a guess, and a probe that
    invents the number it is checking is worse than one that checks nothing.
    """
    ref = (getattr(finding, "evidence_ref", "") or "").strip()
    if not _CELL_REF.match(ref):
        return None
    for num in doc.reported_numbers:
        if (num.table_ref or "").strip() != ref:
            continue
        m = _SCALED_DELTA.match(num.delta or "")
        if m:
            return round(float(m.group(1)) / 100.0, 6)
    return None


def build_spec(cfg: Config, pid: str, doc: PaperDoc) -> ProbeSpec:
    """The spec to run, with `claimed_delta` allowed only where it is grounded.

    Two invariants, applied to the hand-written and generated paths alike, so a stale
    `spec.json` left by the old scraper cannot resurrect a phantom delta on reload:

      - `script == ""`  =>  no claim under test. This is a noise-floor calibration.
      - otherwise       =>  keep an explicit driver value, else ground it in a cell.
    """
    root = state.project_dir(cfg, pid)
    override = root / "runs" / pid / "spec.json"

    reports, _ = audit_stage.load_reports(cfg, pid, doc)
    candidates = [f for f in report_stage.rank([f for r in reports for f in r.findings])
                  if f.verifiable_by_experiment]
    target = candidates[0] if candidates else None

    if override.exists():
        spec = ProbeSpec(**{**state.read_json(override), "paper_id": pid})
        # A spec.json a human wrote and pointed at real code is the one non-repo source
        # allowed to reconcile against a printed cell. A spec.json this stage wrote on a
        # previous run is not, and re-labelling it here would launder it.
        if spec.provenance == "template" and (spec.script or spec.command):
            spec.provenance = "driver"
    else:
        spec = ProbeSpec(
            paper_id=pid,
            finding_id=target.finding_id if target else "",
            claim=(target.target or target.statement) if target else "",
            seeds=list(range(max(3, min(cfg.seeds, 5)))),
        )

    by_id = {f.finding_id: f for r in reports for f in r.findings}
    anchor = by_id.get(spec.finding_id) or target

    if not (spec.script or "").strip() and not spec.command:
        # Identical arms measure this machine, not the paper. Carrying a claimed delta
        # through here is what let a calibration run be read as a reproduction.
        spec.claimed_delta = None
    elif spec.claimed_delta is None:
        # A real reproduction with no driver-supplied number: the only other admissible
        # source is the cell the targeted finding cites.
        spec.claimed_delta = grounded_claimed_delta(doc, anchor)

    # The cell the executed metric will be reconciled against. Carried on the spec so
    # `local_exec.reconcile` never has to re-open the paper, and so a spec with no cell
    # cited produces no reconciliation at all rather than a guess.
    if not spec.table_ref and anchor is not None:
        ref = (anchor.evidence_ref or "").strip()
        if _CELL_REF.match(ref):
            spec.table_ref = ref
    if spec.table_ref and not spec.claimed_cell_value:
        spec.claimed_cell_value = cell_contents(doc, spec.table_ref)
    return spec


def _caption(doc: PaperDoc, ref: str) -> str:
    """The cited table's caption. E1 reads the model scale out of it, so the requirement
    is about the experiment under audit rather than about the paper's largest one."""
    m = re.fullmatch(r"T(\d+):r\d+:c\d+", (ref or "").strip())
    if not m:
        return ""
    for table in doc.tables:
        if table.table_idx == int(m.group(1)):
            return table.caption or ""
    return ""


def cell_contents(doc: PaperDoc, ref: str) -> str:
    """The verbatim contents of an addressed cell, or '' if the address does not resolve."""
    m = re.fullmatch(r"T(\d+):r(\d+):c(\d+)", (ref or "").strip())
    if not m:
        return ""
    for table in doc.tables:
        if table.table_idx == int(m.group(1)):
            return table.cell(int(m.group(2)), int(m.group(3)))
    return ""


def acquire_and_audit(cfg: Config, root: Path, pid: str, doc: PaperDoc,
                      spec: ProbeSpec) -> tuple[RepoAcquisition, CodeAudit]:
    """S3a + S3b: get the paper's code if we are allowed to, then read it without running it.

    Ordered so the static audit still happens on a cached checkout when the network
    gate is shut — reading code that is already on disk needs no permission, and it is
    the half of this stage that produces findings.
    """
    # E2 — if a previous run recorded which commit it audited, acquisition must land on
    # THAT commit rather than on wherever the default branch has since moved. Passing it
    # is what turns a fetch into a pin.
    acq = repo_mod.acquire(cfg, root, pid, doc, revision=audited_commit(root, pid))
    if acq.status in ("cloned", "cached"):
        # Provisioning is the backend's job, because an environment is only meaningful
        # relative to the machine that will run in it: a venv built here is not the
        # environment a container would present. The install gate still decides whether
        # anything is built at all — the backend decides where.
        try:
            acq = backends.select_backend(cfg).provision(cfg, root, pid, acq)
        except backends.UnknownBackend as e:
            acq.env_status, acq.reason = "blocked", str(e)
    elif acq.status == "unavailable":
        acq = repo_mod.synthesize_standalone(
            root, pid, spec.claim, spec.table_ref, spec.claimed_cell_value)

    if acq.status in ("cloned", "cached") and acq.path:
        audit = code_audit.audit_repo(acq.path, cfg.max_audit_files)
        # THE audited commit, stamped at the moment the audit read the tree. Everything
        # downstream — identity, execution, reconciliation — is about this SHA or about
        # nothing, and a later run that finds a different HEAD must refuse rather than
        # carry these findings over onto code they were not derived from.
        audit.commit = repo_mod.head_commit(Path(acq.path))
    else:
        audit = CodeAudit(repo_path=acq.path, skipped=(
            f"no checkout to inspect (acquisition status '{acq.status}'): {acq.reason}"))
    return acq, audit


def audited_commit(root: Path, pid: str) -> str:
    """The commit a previous run's audit recorded, if there was one.

    Read from the persisted probe result rather than recomputed, because the point is to
    pin to what was audited BEFORE, not to whatever is on disk now.
    """
    path = root / "runs" / pid / "probe_results.json"
    if not path.exists():
        return ""
    try:
        data = state.read_json(path)
    except (OSError, ValueError):
        return ""
    audit = (data or {}).get("code_audit") or {}
    return str(audit.get("commit") or "").strip().lower()


def synthesize_probe(cfg: Config, doc: PaperDoc, spec: ProbeSpec,
                     acq: RepoAcquisition) -> ProbeSpec:
    """S3c — author `runs/<pid>/probe.py` from the paper's own formulation.

    Three conditions, all required. The synthesis gate is open; no human already wrote a
    script or a command for this paper (theirs wins, always); and the audit actually
    marked something as settleable by experiment, which is what `spec.finding_id` being
    set means. With nothing marked, the identical-arms template still runs and still
    reports the noise floor — that is the honest output for a paper whose findings are
    all documentary.
    """
    if not cfg.allow_synthesis:
        return spec
    if (spec.script or "").strip() or spec.command:
        return spec
    if not spec.finding_id:
        return spec

    plan = probe_synth.plan(doc, claim=spec.claim, acq=acq)
    if not plan.keeps_finding:
        # The probe tests the paper's mechanism, not the finding that ranked first.
        # Filing it under that finding's id would attribute a measurement of one thing
        # to a claim about another.
        spec.finding_id = ""
    spec.claim = plan.claim or spec.claim
    spec.script = plan.script
    spec.arms = plan.arms
    spec.metric = plan.metric
    spec.mechanism = plan.mechanism
    spec.rationale = plan.rationale
    spec.aux_metrics = plan.aux_metrics
    spec.provenance = "synthesized"
    return spec


def plan_execution(cfg: Config, spec: ProbeSpec, acq: RepoAcquisition,
                   doc: PaperDoc | None = None, audit: CodeAudit | None = None) -> ProbeSpec:
    """Point the spec at the repository's own entrypoint, if every gate allows it.

    Four conditions now, all required: the execution gate is open, a checkout exists, an
    entrypoint was found, and — the addition — this machine is CAPABLE of giving that
    entrypoint a fair run. Failing any of them the spec is left alone and the probe runs
    the local template, which is honest about measuring only this machine.

    The capability requirement exists because of what happens downstream. A repo_exec
    spec that crashes reconciles to FAILED_REPRODUCTION, which drives RED on its own with
    the sentence "the paper's own code does not reproduce the number it prints". That
    sentence must never be produced by an environment this harness failed to build. So
    capability is assessed here, before anything executes, and a spec that cannot be run
    fairly is never promoted to `repo_exec` at all — the capability record is attached to
    the spec either way, so the report can say why execution was not attempted.

    The interpreter fallback is also gone. `acq.env_path or cfg.python` silently ran a
    third-party repository under the harness's own venv, which by construction lacks that
    repository's dependencies — guaranteeing an import crash whenever the install gate is
    shut, which is the default. There is no longer a path that substitutes an interpreter.
    """
    if not cfg.allow_repo_exec:
        return spec
    if acq.status not in ("cloned", "cached") or not acq.path or not acq.entrypoint:
        return spec
    # BOTH preconditions are assessed, and only then is promotion decided. Recording the
    # capability even when identity already failed keeps the report able to say "we could
    # not have run it either" rather than leaving that unknown — two independent blockers
    # are more useful to a reader than the first one encountered.
    try:
        backend = backends.select_backend(cfg)
    except backends.UnknownBackend:
        # An operator typo must not become a substitution. Nothing is promoted, the
        # synthesized/template probe still runs, and the report says why.
        return spec
    spec.backend = backend.name
    # Capability is asked of the BACKEND, so it is judged against the platform the run
    # would actually see rather than against whatever platform this process is on.
    spec.capability = backend.capability(acq, acq.env_path, cfg.python)
    # E2 — the commit the static audit read. Carried on the spec so execution has
    # something to verify against that is not "whatever is on disk".
    spec.commit = (audit.commit if audit is not None else "") or acq.commit
    if doc is not None:
        spec.experiment, spec.metric_identity, spec.configuration = experiment_id.resolve(
            doc, Path(acq.path), spec.table_ref, spec.finding_id, harness_seeds=len(spec.seeds))
        # E1 — what the CITED experiment costs, against what this backend has. Assessed
        # here, before promotion, because the answer cannot be obtained by trying: an
        # experiment three times larger than the card starts fine and dies minutes in.
        spec.resources = resources_mod.assess_resources(
            resources_mod.require_resources(
                doc, spec.table_ref, spec.claimed_cell_value,
                caption=_caption(doc, spec.table_ref)),
            backend.resources(), backend=backend.name,
            walltime_budget_s=cfg.probe_timeout_s * max(1, len(spec.seeds)))
    proven, _cls, _why = experiment_id.identities_established(
        spec.experiment, spec.metric_identity, spec.configuration)

    # Identity is the more fundamental of the two: a capable run of the wrong program is
    # worse than a crash, because nothing about it looks wrong.
    fits = spec.resources is not None and spec.resources.established
    commit_ok = repo_mod.verify_commit(acq.path, spec.commit).established
    if not proven or not spec.capability.established or not fits or not commit_ok:
        # Any one of these is disqualifying, and none of them is fixed by trying anyway.
        # Note what is NOT here: no branch that reduces the requirement, picks a smaller
        # variant, or accepts a nearby commit. An altered experiment is not a reproduction
        # and adjacent code is not the audited code.
        return spec                              # keep the synthesized/template probe

    # The command comes from what the repository advertises, never from a filename plus
    # an invented flag. `--seed` is used only where the repo itself defines one.
    command = list(spec.experiment.command.argv) if spec.experiment.command else []
    seed_flag = spec.experiment.command.seed_flag if spec.experiment.command else ""
    if seed_flag:
        command += [seed_flag, "{seed}"]
    spec.command = command
    spec.cwd = acq.path
    spec.interpreter = acq.env_path
    spec.arms = ["reproduction"]
    # The authors' own code outranks anything this harness could author, so it replaces a
    # synthesized script and takes the provenance that is allowed to reconcile a cell.
    spec.script = ""
    spec.provenance = "repo_exec"
    return spec


def run(cfg: Config, pid: str) -> dict:
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))
    state.set_phase(cfg, pid, "probe")

    spec = build_spec(cfg, pid, doc)
    # S3a/S3b run before execution is planned, because what the static audit finds is
    # useful even when every execution gate is shut — and because deciding what to run
    # requires knowing what was acquired.
    acq, audit = acquire_and_audit(cfg, root, pid, doc, spec)
    # S3c runs after acquisition so the planner can see the checkout, and before
    # execution planning so the authors' own entrypoint still wins when it is available.
    spec = synthesize_probe(cfg, doc, spec, acq)
    spec = plan_execution(cfg, spec, acq, doc, audit)

    state.write_json(root / "runs" / pid / "spec.json", spec.model_dump())
    result = _run(cfg, root, spec)
    result.repo, result.code_audit = acq, audit
    # `_run` already wrote the file; rewrite it now that acquisition and the static
    # audit are attached, so `probe_results.json` is the whole of S3 rather than a
    # torso the report has to reassemble.
    state.write_json(root / "runs" / pid / "probe_results.json", result.model_dump())

    rec = result.reconciliation
    state.append_log(
        cfg, pid, artifact_type="probe_result", phase="probe",
        headers={"verdict": result.verdict, "device": result.device,
                 "measured_delta": result.measured_delta, "measured_std": result.measured_std,
                 "claimed_delta": result.claimed_delta,
                 "claim_within_noise": result.claim_within_noise,
                 "repo_status": acq.status, "code_findings": len(audit.findings),
                 "provenance": result.provenance, "mechanism": result.mechanism,
                 "reconciliation": rec.status if rec else None,
                 "finding_id": result.finding_id, "seconds": result.seconds},
        path=str(root / "runs" / pid / "probe_results.json"),
    )
    return {"paper_id": pid, "verdict": result.verdict, "device": result.device,
            "finding_id": result.finding_id or None,
            "measured_delta": result.measured_delta, "measured_std": result.measured_std,
            "noise_band": result.noise_band, "claimed_delta": result.claimed_delta,
            "claim_within_noise": result.claim_within_noise,
            "seeds_run": result.seeds_run, "seeds_failed": result.seeds_failed,
            "repo": acq.status, "repo_url": acq.url or None, "entrypoint": acq.entrypoint or None,
            "frameworks": acq.frameworks, "env": acq.env_status,
            "provenance": result.provenance, "mechanism": result.mechanism or None,
            "code_findings": len(audit.findings),
            "code_audit_skipped": audit.skipped or None,
            "reconciliation": rec.status if rec else None,
            "seconds": result.seconds, "reason": result.reason,
            "script": result.script_path, "results": f"runs/{pid}/probe_results.json"}
