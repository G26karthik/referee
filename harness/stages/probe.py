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

from .. import (backends, code_audit, experiment_id, probe_synth, reimplement,
                repo as repo_mod, resources as resources_mod, state)
from ..artifacts import (CodeAudit, Finding, PaperDoc, ProbeSpec,
                         ReimplementationReadiness, RepoAcquisition)
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

    reports, _, _ = audit_stage.load_reports(cfg, pid, doc)
    candidates = [f for f in report_stage.rank([f for r in reports for f in r.findings])
                  if f.verifiable_by_experiment]
    target = candidates[0] if candidates else None

    # `written_by == "harness"` marks OUR OWN previous output — set only at the bottom of
    # `run()`, below, right before it persists the spec it just built. A human-authored
    # override never carries it, since a human editing spec.json by hand has no reason to
    # know the field exists. Without this distinction a stale run's finding_id/table_ref/
    # claimed_cell_value — and `synthesize_probe`'s stale script, which skips regenerating
    # once `spec.script` is already set — kept feeding back into every later run even after
    # the audit moved its verifiable target elsewhere, so the report's chain named a
    # finding the CURRENT findings table no longer contained, reconciled against a cell
    # that was no longer the target. Treating a harness-written file as absent forces every
    # run to re-derive its target from the CURRENT audit, which is what `else` already does.
    override_data = state.read_json(override) if override.exists() else None
    if override_data is not None and override_data.get("written_by") != "harness":
        spec = ProbeSpec(**{**override_data, "paper_id": pid})
        # A spec.json a human wrote and pointed at real code is the one non-repo source
        # allowed to reconcile against a printed cell. A spec.json this stage wrote on a
        # previous run is not, and re-labelling it here would launder it.
        if spec.provenance == "template" and (spec.script or spec.command):
            spec.provenance = "driver"
    else:
        # `written_by="harness"` here, not left blank: this is what makes the file this
        # function is about to be re-read as (via `run()`'s persistence below) identify
        # itself correctly on the NEXT call, so a fresh audit target keeps being honored
        # run after run instead of freezing on whichever finding was verifiable first.
        spec = ProbeSpec(
            paper_id=pid, written_by="harness",
            finding_id=target.finding_id if target else "",
            claim=(target.target or target.statement) if target else "",
            seeds=list(range(max(3, min(cfg.seeds, 5)))),
        )

    # Keyed by (lens, finding_id), not by finding_id alone. Ids are lens-supplied, and
    # four lenses independently numbering their findings `overclaim-01`, `protocol-01`
    # and so on collide readily; a bare-id map silently kept whichever lens was loaded
    # last, so the probe could anchor to a different finding than the one it targeted.
    by_id = {(f.lens, f.finding_id): f for r in reports for f in r.findings}
    anchor = next((f for (_, fid), f in by_id.items() if fid == spec.finding_id), None) or target

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
    # Provisioning is deliberately NOT here. It belongs to the backend that will run, and
    # which backend that is cannot be known until the experiment's demand has been matched
    # against the registry — which needs this checkout. So acquisition happens first,
    # selection second, and provisioning third, in `plan_execution`.
    #
    # It used to happen here, against `select_backend(cfg)`. With `local` the only runnable
    # backend that was always the same answer, so nothing was observably wrong; register a
    # second runnable backend and the environment is built by whichever one the config
    # names while the run is planned for whichever one the experiment fits. Capability is
    # then assessed against a foreign interpreter, and the first genuinely remote backend
    # inherits a silent substitution — precisely what this harness refuses everywhere else.
    if acq.status == "unavailable":
        # PATH B opens here, or is refused here with its reasons named. "The authors
        # published no code" used to end the investigation: a scaffold that raises
        # NotImplementedError was written, nothing ran it, and a generic placebo was all
        # that reached the report. The scaffold is still written — it is the artifact a
        # reimplementer starts from — but the question of whether this paper CAN be
        # rebuilt is now asked and recorded, so the outcome is a decision rather than a
        # stop. `assess` is pure and invents nothing; when it refuses, it names the gaps.
        readiness = reimplement.assess(doc)
        acq = repo_mod.synthesize_standalone(
            root, pid, spec.claim, spec.table_ref, spec.claimed_cell_value)
        acq.reimplementation = readiness
        if readiness.established:
            write_reimplementation_prompt(root, pid, doc, spec, readiness)
            acq.reason = (f"{acq.reason} An independent reimplementation is eligible: the paper "
                          f"supplies every required ingredient. The brief is in "
                          f"reports/reimplementation_prompt.md; a completed implementation is "
                          f"sealed through `run.py accept` and runs as "
                          f"INDEPENDENT_REIMPLEMENTATION, never as the authors' code.")
        else:
            # A brief from an EARLIER, more permissive assessment must not survive a later
            # refusal. Left on disk it invites someone to rebuild a paper this run has just
            # established cannot be rebuilt without inventing the missing piece — the exact
            # thing PATH B exists to prevent, in the form of a stale file.
            (root / "reports" / "reimplementation_prompt.md").unlink(missing_ok=True)
            gaps = ", ".join(readiness.missing)
            acq.reason = (f"{acq.reason} An independent reimplementation is NOT eligible: the "
                          f"paper does not supply {gaps}. Building one would mean inventing "
                          f"{'that' if len(readiness.missing) == 1 else 'those'}, so this claim "
                          f"stays NOT_VERIFIED.")

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


def write_reimplementation_prompt(root: Path, pid: str, doc: PaperDoc, spec: ProbeSpec,
                                  readiness: ReimplementationReadiness) -> Path:
    """Write the PATH B brief to disk and return its path.

    The same shape as `audit/prompts/<lens>.md` and `reports/verdict_prompt.md`, for the
    same reason: a phase that can only proceed through a model must still leave something
    on disk a person can pick up, or the manual path is not first-class. There is
    deliberately NO fourth driver — a reimplementation is written into
    `runs/<pid>/spec.json` and sealed by `run.py accept`, which routes it down the
    existing `driver` provenance the ceiling already admits.

    What the brief does NOT do is supply anything the paper omitted. It lists the
    ingredients WITH THEIR LOCATORS so the implementer works from the paper, and it says
    plainly that a gap must be reported rather than filled.
    """
    lines = [
        f"# Independent reimplementation brief — `{pid}`", "",
        "The paper below advertises no public implementation. This harness has checked that",
        "it nevertheless specifies enough to rebuild the experiment. You are being asked to",
        "write that implementation from the paper's own formulation.", "",
        "## The rule that matters most", "",
        "**Do not invent anything the paper does not state.** If you find, while writing, that",
        "a detail you need is absent, STOP and report it as missing. A number produced by an",
        "implementation that filled its own gaps is a statement about your choices, not about",
        "this paper, and reporting it as a reproduction would be the single worst failure this",
        "system can commit. An honest `NOT_VERIFIED` is a correct outcome here.", "",
        "This is **not** a reproduction of the authors' code — it is an independent",
        "reimplementation, and it will be reported as `INDEPENDENT_REIMPLEMENTATION` however",
        "well it matches. Never describe it as the authors' code failing or succeeding.", "",
        f"## Claim under test", "", f"> {spec.claim or '(no claim bound)'}", "",
        f"Compare against `{spec.table_ref or '(no cell bound)'}` = "
        f"`{spec.claimed_cell_value or '(none)'}`.", "",
        "## What the paper supplies", "",
        "| ingredient | required | found at | the paper's own words |",
        "|---|---|---|---|",
    ]
    for i in readiness.ingredients:
        mark = "yes" if i.present else "**NO**"
        quote = (i.quote or "").replace("|", "\\|")[:160]
        lines.append(f"| {i.kind} | {'yes' if i.required else 'no'} | {mark} "
                     f"{('`' + i.ref + '`') if i.ref else ''} | {quote} |")
    lines += [
        "", "## What to produce", "",
        "A `runs/<pid>/spec.json` carrying `command` (or `script`), `metric`, `seeds`, and the",
        "identity fields, plus the implementation itself. Print one `METRIC <name> <value>` line",
        "per seed on stdout — the same contract every probe in this harness uses.", "",
        "Seal it with:", "",
        f"    python run.py accept --paper {pid} --reviewer \"<who wrote this>\"", "",
        "## The paper", "",
    ]
    for s in doc.sections:
        lines += [f"### {s.title or f'section {s.section_idx}'}  [s{s.section_idx}]", "",
                  " ".join(s.text.split()), ""]
    path = root / "reports" / "reimplementation_prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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
                   doc: PaperDoc | None = None, audit: CodeAudit | None = None,
                   root: Path | None = None) -> ProbeSpec:
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
    if acq.status not in ("cloned", "cached") or not acq.path or not acq.entrypoint:
        return spec
    # ASSESSMENT RUNS EVEN WITH THE GATE SHUT; only PROMOTION is gated. Returning early on
    # a closed `allow_repo_exec` left the report unable to say anything about why a
    # reproduction was impossible: identity `unmapped`, resources `unassessed`, no backend
    # considered. Since the gate is shut by default, that was the normal output — the
    # harness knew nothing about the very question it exists to answer carefully.
    #
    # Every assessment below is read-only. It parses files, reads this machine's hardware
    # inventory and runs `git rev-parse`. None of it executes the repository, so none of it
    # needs the gate; the gate exists to permit RUNNING third-party code, and it still
    # does exactly that a few lines further down.
    #
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

    # E2 — the commit the static audit read. Carried on the spec so execution has
    # something to verify against that is not "whatever is on disk".
    spec.commit = (audit.commit if audit is not None else "") or acq.commit

    if doc is not None:
        spec.experiment, spec.metric_identity, spec.configuration = experiment_id.resolve(
            doc, Path(acq.path), spec.table_ref, spec.finding_id, harness_seeds=len(spec.seeds))
        # Re-scope the runtime demands now that identity has run. The static audit reads the
        # tree BEFORE any experiment is identified, so everything it found was labelled
        # repository-scoped; only here is there a file to bind against. `source_ref` is the
        # "file:line" the candidate command was read from, so the file it names is the one
        # whose demands belong to this cell rather than to the checkout at large.
        #
        # A command that was never established leaves every demand repository-scoped, which
        # is the honest label: this repository holds 74 submission scripts asking for 32G,
        # 40G and 64G, and no aggregate of those describes an experiment anyone ran.
        if audit is not None and spec.experiment is not None:
            cmd = spec.experiment.command
            ref = (cmd.source_ref if cmd and spec.experiment.established else "")
            code_audit.scope_demands(audit.runtime, ref.split(":")[0] if ref else "")
        # E1 — what the CITED experiment costs. Established from the paper BEFORE a
        # backend is chosen, because the demand is a property of the experiment and the
        # supply is a property of the environment: matching them the other way round —
        # taking whatever backend is configured and asking whether the paper fits it —
        # answers a narrower question and cannot report that somewhere else would.
        requirement = resources_mod.require_resources(
            doc, spec.table_ref, spec.claimed_cell_value, caption=_caption(doc, spec.table_ref))
        budget = cfg.probe_timeout_s * max(1, len(spec.seeds))
        selection = backends.select_for(
            requirement, cfg, declared_platform=repo_mod.declared_platform(Path(acq.path)),
            walltime_s=budget)
        spec.backend_selection = selection.reason_code
        spec.backend_considered = [f"{n}: {v} — {w}" for n, v, w in selection.considered]
        # A selected backend replaces the configured one; a refusal leaves the configured
        # one in place so capability and resources are still recorded against something
        # real and the report can say what would have been needed.
        backend = selection.chosen or backend
        spec.resources = resources_mod.assess_resources(
            requirement, backend.resources(), backend=backend.name, walltime_budget_s=budget)

    # The SELECTED backend provisions, because an environment is only meaningful relative
    # to the machine that will run in it: a venv built on this host is not the environment
    # a container or a remote session would present. The install gate still decides whether
    # anything is built at all — the backend decides where, and now it is the same backend
    # whose platform capability is about to be judged against.
    #
    # `root` is a parameter rather than derived, so a caller that has no project directory
    # provisions nothing instead of writing into one it did not name.
    if root is not None and acq.status in ("cloned", "cached"):
        acq = backend.provision(cfg, root, spec.paper_id, acq)
        acq.env_backend = backend.name

    spec.backend = backend.name
    # Capability is asked of the BACKEND, so it is judged against the platform the run
    # would actually see rather than against whatever platform this process is on.
    spec.capability = backend.capability(acq, acq.env_path, cfg.python)
    spec.commit_state = repo_mod.verify_commit(acq.path, spec.commit).state
    proven, _cls, _why = experiment_id.identities_established(
        spec.experiment, spec.metric_identity, spec.configuration)

    # --- assessment ends; promotion begins ---------------------------------------------
    if not cfg.allow_repo_exec:
        # Everything above is now on the spec, so the report can state precisely what
        # would have been required and which link was missing — which is the useful thing
        # to say about a paper nobody was permitted to reproduce.
        return spec

    # Identity is the more fundamental of the two: a capable run of the wrong program is
    # worse than a crash, because nothing about it looks wrong.
    fits = spec.resources is not None and spec.resources.established
    commit_ok = spec.commit_state == "verified"
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
    #
    # `repo_exec` is stamped ONLY because `acq` is a real checkout of the paper's own
    # advertised repository — every path reaching this line came through the
    # `cloned`/`cached` gate above. A hand-written or reimplemented spec must never be
    # relabelled as the authors' code by passing through here: that is the one mislabel
    # with a victim, since it would attribute a failure of somebody else's program to the
    # authors. `driver` is left exactly as it was found.
    if spec.provenance == "driver":
        return spec
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
    spec = plan_execution(cfg, spec, acq, doc, audit, root=root)

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
