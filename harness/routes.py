"""Run what the review decided to run, and record how each target ended.

The S3 orchestration: decides the spec for each target, acquires and audits the
repository, plans execution (author code or a governed reconstruction), dispatches to
`execute.py`'s runner, and folds every target's `TargetOutcome` back onto the
`TargetSet`. The primary-target and secondary-target flows share roughly a dozen
interacting special cases (direct reconstruction vs. a fallback from author code,
comparable vs. not, admissible vs. not, the dynamic early stop, the artifact route that
never competes with execution budget), so this stays close to that shape rather than a
from-scratch protocol abstraction that would risk losing correctness for uniformity.

What decides the TARGETS is `discover.py`, before this module is reached: it discovers
what is addressable, orders it, and decides which of it justifies an execution. This
module pursues that list, in that order, up to `cfg.max_targets`, and writes one
`TargetOutcome` per target — every target keeps INDEPENDENT state.

`python -m harness.routes` runs the self-check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import decide, execute, locate
from . import provenance as provenance_mod
from . import state
from .config import Config
from .schema import (
    CodeAudit, DiscoveredObject, ExperimentIdentity, Finding, PaperDoc, PlanDecision,
    ProbeResult, ProbeSpec,
    ReimplementationReadiness, RepoAcquisition, TargetOutcome,
)

# These remain their OWN files, unchanged — pure leaf modules with no cross-file
# duplication with execute.py's core: experiment_id.py, resources.py, repo.py,
# code_audit.py, probe_synth.py, reimplement_driver.py, and the artifact-inspection
# route (artifact_evidence.py + stages/artifact.py).
from . import certificate
from . import code_audit
from . import experiment_id
from . import probe_synth
from . import reimplement_driver
from . import repo as repo_mod
from . import resources as resources_mod
from .stages import artifact as artifact_stage

_CELL_REF = re.compile(r"^T\d+:r\d+:c\d+$")
_SCALED_DELTA = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:%|pp)\s*$", re.IGNORECASE)


def grounded_claimed_delta(doc: PaperDoc, finding: Finding | None) -> float | None:
    """The delta the paper printed for the cell this finding cites, as a fraction. None
    unless the whole chain holds: a cell address, a recorded number there, an explicit,
    scaled delta — never mined from a finding's prose."""
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


def grounded_quantity(target) -> tuple[float | None, str]:
    """(value, verbatim) a discovered target's own address reports — the general
    replacement for `grounded_claimed_delta`'s cell-only chain, reaching a prose-stated
    total by exactly the same route a cell does."""
    ref = getattr(target, "ref", None)
    q = getattr(ref, "quantity", None) if ref else None
    if q is None or q.value is None:
        return None, ""
    return q.value, (q.raw or "")


_SPEC_PROPOSAL_ALLOWED_FIELDS = frozenset({
    "paper_id", "finding_id", "claim", "claimed_delta", "metric", "arms", "seeds",
    "dataset", "epochs", "script", "table_ref", "claim_ref", "claim_kind",
    "claimed_cell_value", "target_id", "mechanism", "rationale", "aux_metrics",
})


def _strip_to_allowed_fields(data: dict) -> dict:
    return {k: v for k, v in (data or {}).items() if k in _SPEC_PROPOSAL_ALLOWED_FIELDS}


def _spec_accepted_writers() -> tuple[str, ...]:
    """Every token a real delegation mode can seal with, plus the two fixed extras this
    channel uses: "harness" marks `build_spec`'s own generated output, and
    "driver_accept" is the one token `accept_spec` ever stamps."""
    from . import agent as agent_mod
    return tuple(agent_mod.WRITTEN_BY.values()) + ("harness", "driver_accept")


def spec_is_accepted(root: Path) -> tuple[bool, str]:
    """Does `control/spec.json` carry a valid seal? A spec with no sidecar, a sidecar
    whose `written_by` is not a real accept-path token, or a mismatched hash is NOT
    accepted — refused outright, never partially trusted."""
    from . import agent as agent_mod
    control = state.control_dir(root)
    path = control / "spec.json"
    return agent_mod.verify_seal(path, accepted_writers=_spec_accepted_writers())


def accept_spec(cfg: Config, pid: str, raw: str, *, reviewer: str = "",
               tool_policy: str = "", mode: str = "MANUAL") -> dict:
    """Validate and seal a hand-authored `spec.json` proposal -- the "driver" provenance
    path. Every field NOT in `_SPEC_PROPOSAL_ALLOWED_FIELDS` is stripped before anything
    else, so `provenance`/`command`/every `.established` block can never be supplied by a
    hand file. A `command` in the raw proposal refuses outright: `driver` provenance is a
    hand-written script, never the paper's own repository entrypoint, which only
    `plan_execution`'s own real-audit-gated promotion may ever attribute."""
    import json

    from . import agent as agent_mod

    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("spec proposal must be a JSON object")
    if data.get("command"):
        raise ValueError(
            "a spec proposal may not set 'command' — that is the authors' own "
            "repository entrypoint and may only be attributed by this harness's own "
            "plan_execution, never accepted from a hand file")
    proposal = _strip_to_allowed_fields(data)
    proposal["paper_id"] = pid
    script = (proposal.get("script") or "").strip()
    if script and not Path(script).is_file():
        raise ValueError(f"script {script!r} does not exist; a driver spec must point "
                        f"at a real, readable file")
    if script:
        proposal["script"] = Path(script).read_text(encoding="utf-8")
    proposal["provenance"] = "driver"
    spec = ProbeSpec(**proposal)

    root = state.project_dir(cfg, pid)
    control = state.control_dir(root)
    out = control / "spec.json"
    record = agent_mod.seal(out, spec.model_dump(), mode=mode, reviewer=reviewer,
                            tool_policy=tool_policy,
                            extra={"paper_id": pid, "written_by": "driver_accept"})
    return {"accepted": True, "paper_id": pid, "content_sha256": record["content_sha256"]}


def build_spec(cfg: Config, pid: str, doc: PaperDoc, target=None) -> ProbeSpec:
    """The spec to run, with `claimed_delta` allowed only where it is grounded:
    `script == ""` means a noise-floor calibration and no claim under test; otherwise a
    driver-supplied value is kept or it is grounded in a cited cell — never scraped from
    prose."""
    from . import audit as audit_stage
    from . import report as report_stage

    root = state.project_dir(cfg, pid)
    reports, _dropped, _invalid_lenses = audit_stage.load_reports(cfg, pid, doc)
    candidates = [f for f in report_stage.rank([f for r in reports for f in r.findings])
                 if f.verifiable_by_experiment]
    finding_target = candidates[0] if candidates else None
    target_finding_id = ""
    if target is not None and getattr(target, "question_id", ""):
        from . import discover as discover_stage
        tset_path = discover_stage.targets_path_in(root)
        if tset_path.exists():
            raw_ts = state.read_json(tset_path)
            qrow = next((q for q in raw_ts.get("questions", [])
                        if q.get("question_id") == target.question_id), {})
            target_finding_id = (qrow.get("from_finding")
                                 or next(iter(qrow.get("source_finding_ids") or []), ""))
            if target_finding_id:
                finding_target = next(
                    (f for f in candidates if f.finding_id == target_finding_id), None)

    control = state.control_dir(root)
    accepted, _why = spec_is_accepted(root)
    if accepted:
        sealed = state.read_json(control / "spec.json")
        proposal = _strip_to_allowed_fields(sealed)
        proposal["paper_id"] = pid
        spec = ProbeSpec(**proposal)
        spec.provenance = "driver"
        spec.written_by = "driver_accept"
    else:
        spec = ProbeSpec(
            paper_id=pid, written_by="harness",
            finding_id=(target_finding_id or
                       (finding_target.finding_id if finding_target and target is None else "")),
            claim=((getattr(target, "claim_text", "") or
                   getattr(getattr(target, "ref", None), "quote", ""))
                  if target is not None else
                  ((finding_target.target or finding_target.statement)
                   if finding_target else "")),
            seeds=list(range(max(3, min(cfg.seeds, 5)))))

    by_id = {(f.lens, f.finding_id): f for r in reports for f in r.findings}
    anchor = (next((f for (_, fid), f in by_id.items() if fid == spec.finding_id), None)
             or finding_target)

    if not (spec.script or "").strip() and not spec.command:
        spec.claimed_delta = None
    elif spec.claimed_delta is None:
        spec.claimed_delta = grounded_claimed_delta(doc, anchor)

    if not spec.table_ref and anchor is not None:
        ref = (anchor.evidence_ref or "").strip()
        if _CELL_REF.match(ref):
            spec.table_ref = ref
    if spec.table_ref and not spec.claimed_cell_value:
        spec.claimed_cell_value = cell_contents(doc, spec.table_ref)

    if target is not None and getattr(target, "ref", None) is not None:
        ref_obj = target.ref
        spec.target_id = getattr(target, "target_id", "")
        spec.claim_ref = ref_obj.ref
        spec.claim_kind = ref_obj.kind
        # The claim under test is the PAPER's own words at the re-resolved address — a
        # lens's paraphrase of it is a proposal about the claim, not the claim.
        paper_words = ref_obj.quote if (ref_obj.resolved and ref_obj.kind == "prose_claim"
                                        and len(ref_obj.quote or "") >= 40) else ""
        spec.claim = paper_words or getattr(target, "claim_text", "") or ref_obj.quote
        if ref_obj.kind == "table_cell":
            spec.table_ref = ref_obj.ref
        value, raw = grounded_quantity(target)
        if value is not None:
            spec.claimed_cell_value = raw or ref_obj.quote
        if getattr(target, "metric", ""):
            spec.metric = target.metric
    elif not spec.claim_ref and spec.table_ref:
        spec.claim_ref, spec.claim_kind = spec.table_ref, "table_cell"
    return spec


def _caption(doc: PaperDoc, ref: str) -> str:
    m = re.fullmatch(r"T(\d+):r\d+:c\d+", (ref or "").strip())
    if not m:
        return ""
    for table in doc.tables:
        if table.table_idx == int(m.group(1)):
            return table.caption or ""
    return ""


def cell_contents(doc: PaperDoc, ref: str) -> str:
    m = re.fullmatch(r"T(\d+):r(\d+):c(\d+)", (ref or "").strip())
    if not m:
        return ""
    for table in doc.tables:
        if table.table_idx == int(m.group(1)):
            return table.cell(int(m.group(2)), int(m.group(3)))
    return ""


def acquire_and_audit(cfg: Config, root: Path, pid: str, doc: PaperDoc,
                      spec: ProbeSpec) -> tuple[RepoAcquisition, CodeAudit]:
    """S3a + S3b: get the paper's code if allowed, then read it without running it —
    ordered so the static audit still happens on a cached checkout when the network gate
    is shut."""
    from . import discover as discover_stage

    acq = repo_mod.acquire(cfg, root, pid, doc, revision=audited_commit(root, pid))
    # What exactly was reviewed: the paper's bytes and printed identifiers, the code's URL,
    # commit and how it was attributed, and the data archives the paper links.
    state.write_json(root / "paper" / "sources.json", {
        "paper": repo_mod.paper_identifiers(doc),
        "code": {"url": acq.url, "status": acq.status, "commit": acq.commit,
                 "requested_revision": acq.requested_revision, "pinned": acq.pinned,
                 "shallow": acq.shallow, "discovered_by": acq.discovered_by,
                 "evidence": acq.discovery_evidence, "reason": acq.reason},
        "data": repo_mod.data_sources(doc)})
    if acq.status == "unavailable":
        readiness = discover_stage.reimplementation_readiness(doc)
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
            (root / "reports" / "reimplementation_prompt.md").unlink(missing_ok=True)
            gaps = ", ".join(readiness.missing)
            acq.reason = (f"{acq.reason} An independent reimplementation is NOT eligible: the "
                         f"paper does not supply {gaps}. Building one would mean inventing "
                         f"{'that' if len(readiness.missing) == 1 else 'those'}, so this claim "
                         f"stays NOT_VERIFIED.")

    if acq.status in ("cloned", "cached") and acq.path:
        audit = code_audit.audit_repo(acq.path, cfg.max_audit_files)
        audit.commit = repo_mod.head_commit(Path(acq.path))
    else:
        audit = CodeAudit(repo_path=acq.path, skipped=(
            f"no checkout to inspect (acquisition status '{acq.status}'): {acq.reason}"))
    return acq, audit


def write_reimplementation_prompt(root: Path, pid: str, doc: PaperDoc, spec: ProbeSpec,
                                  readiness: ReimplementationReadiness) -> Path:
    """The PATH B brief, for the MANUAL channel: an operator who wants to review a
    reconstruction before sealing it stages a `script` at `control/.staged/spec.json` and
    seals it with `run.py accept` under `driver` provenance. The AUTOMATED channel
    (`reimplement_driver`) machine-verifies every ingredient's binding before sealing as
    `reimpl_exec`."""
    lines = [
        f"# Independent reimplementation brief — `{pid}`", "",
        "The paper below advertises no public implementation. This harness has checked that",
        "it nevertheless specifies enough to rebuild the experiment. You are being asked to",
        "write that implementation from the paper's own formulation.", "",
        "## The rule that matters most", "",
        "**Do not invent anything the paper does not state.** If you find, while writing, that",
        "a detail you need is absent, STOP and report it as missing.", "",
        "This is **not** a reproduction of the authors' code — it is an independent",
        "reimplementation, and it will be reported as `INDEPENDENT_REIMPLEMENTATION` however",
        "well it matches.", "",
        f"## Claim under test", "", f"> {spec.claim or '(no claim bound)'}", "",
        f"Compare against `{spec.table_ref or '(no cell bound)'}` = "
        f"`{spec.claimed_cell_value or '(none)'}`.", "",
        "## What the paper supplies", "",
        "| ingredient | required | found at | the paper's own words |", "|---|---|---|---|",
    ]
    for i in readiness.ingredients:
        mark = "yes" if i.present else "**NO**"
        quote = (i.quote or "").replace("|", "\\|")[:160]
        lines.append(f"| {i.kind} | {'yes' if i.required else 'no'} | {mark} "
                    f"{('`' + i.ref + '`') if i.ref else ''} | {quote} |")
    lines += [
        "", "## What to produce", "",
        "A `spec.json` naming your `script` (never `command`), plus `metric`, `seeds`, and the ",
        "identity fields, plus the implementation itself. Print one `METRIC <name> <value>` ",
        "line per seed on stdout.", "",
        f"Stage it at `control/.staged/spec.json` under this case's project directory, then seal it with:",
        "", f"    python run.py accept --paper {pid} --reviewer \"<who wrote this>\"", "",
        "## The paper", "",
        _scoped_paper_text(
            doc, table_ref=spec.table_ref, claim_ref=spec.claim_ref,
            ingredient_refs=tuple(i.ref for i in readiness.ingredients)),
        "",
    ]
    path = root / "reports" / "reimplementation_prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def audited_commit(root: Path, pid: str) -> str:
    """The commit a previous run's audit recorded — read from the persisted result, so a
    re-acquisition pins to what was audited BEFORE, not whatever is on disk now."""
    path = state.control_dir(root) / "probe_results.json"
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
    """S3c — author `runs/<pid>/probe.py` from the paper's own formulation, only when the
    synthesis gate is open, nobody already wrote a script or command, and the audit
    marked something settleable."""
    if not cfg.allow_synthesis:
        return spec
    if (spec.script or "").strip() or spec.command:
        return spec
    if not spec.finding_id:
        return spec
    # A target-bound spec is about ONE printed quantity. The generic placebo measures what an
    # auxiliary term buys on the template's own task — nothing at the target's address — so it
    # would only attach "accuracy"/"digits" to a claim that names neither.
    if spec.claim_ref or spec.table_ref:
        return spec

    plan = probe_synth.plan(doc, claim=spec.claim, acq=acq)
    if not plan.keeps_finding:
        spec.finding_id = ""
    spec.claim = plan.claim or spec.claim
    spec.metric = plan.metric
    spec.script = plan.script
    spec.arms = plan.arms
    spec.mechanism = plan.mechanism
    spec.rationale = plan.rationale
    spec.aux_metrics = plan.aux_metrics
    spec.provenance = "synthesized"
    return spec


def plan_execution(cfg: Config, spec: ProbeSpec, acq: RepoAcquisition,
                   doc: PaperDoc | None = None, audit: CodeAudit | None = None,
                   root: Path | None = None) -> ProbeSpec:
    """Point the spec at the repository's own entrypoint, if every gate allows it. Four
    conditions, all required: the execution gate open, a checkout, an entrypoint, and this
    machine CAPABLE of a fair run. Assessment runs even with the gate shut (only PROMOTION
    is gated), so the report can say precisely what would have been required."""
    if acq.status in ("cloned", "cached") and acq.path and not acq.entrypoint:
        # ASSESSED, not unassessed: the checkout was read and names no command to run, so
        # the authors'-code route is exhausted for this target. Left unset, the
        # reconstruction fallback (`replan_after_author_code_exhausted`) never engages and a
        # data-only release is never used at all.
        spec.experiment = ExperimentIdentity(
            state="no_candidate", table_ref=spec.table_ref,
            reason="the pinned checkout advertises no runnable entrypoint, so no author "
                   "command can be bound to this target")
        return spec
    if acq.status not in ("cloned", "cached") or not acq.path or not acq.entrypoint:
        return spec
    try:
        backend = execute.select_backend(cfg)
    except execute.UnknownBackend:
        return spec

    spec.commit = (audit.commit if audit is not None else "") or acq.commit

    if doc is not None:
        claim_quote = ""
        if spec.claim_ref and spec.claim_kind == "prose_claim":
            resolved = locate.resolve(doc, spec.claim_ref)
            claim_quote = resolved.quote if resolved.resolved else ""
        spec.experiment, spec.metric_identity, spec.configuration = experiment_id.resolve(
            doc, Path(acq.path), spec.table_ref, spec.finding_id, harness_seeds=len(spec.seeds),
            claim_ref=spec.claim_ref, claim_quote=claim_quote)
        if audit is not None and spec.experiment is not None:
            cmd = spec.experiment.command
            ref = (cmd.source_ref if cmd and spec.experiment.established else "")
            code_audit.scope_demands(audit.runtime, ref.split(":")[0] if ref else "")
        requirement = resources_mod.require_resources(
            doc, spec.table_ref, spec.claimed_cell_value, caption=_caption(doc, spec.table_ref))
        budget = cfg.probe_timeout_s * max(1, len(spec.seeds))
        selection = execute.select_for(
            requirement, cfg, declared_platform=repo_mod.declared_platform(Path(acq.path)),
            walltime_s=budget)
        spec.backend_selection = selection.reason_code
        spec.backend_considered = [f"{n}: {v} — {w}" for n, v, w in selection.considered]
        backend = selection.chosen or backend
        spec.resources = execute.assess_resources(
            requirement, backend.resources(), backend=backend.name, walltime_budget_s=budget)

    if root is not None and acq.status in ("cloned", "cached"):
        acq = backend.provision(cfg, root, spec.paper_id, acq)
        acq.env_backend = backend.name

    spec.backend = backend.name
    is_count = bool(spec.metric_identity and spec.metric_identity.established
                   and spec.metric_identity.cell_quantity == "count")
    # The invocation is the one the repository documents: a seed flag is passed only when
    # the bound program defines one, and capability is judged on THAT program, not on
    # whichever file happened to be picked as the checkout's nominal entrypoint.
    bound = (spec.experiment.command if spec.experiment is not None
             and spec.experiment.established else None)
    program = experiment_id.target_file(Path(acq.path), bound) if bound else None
    if program is not None:
        acq = acq.model_copy(update={"entrypoint": program.relative_to(acq.path).as_posix()})
    # A seed the README already pins ("--seed 42") is the documented protocol; it is kept,
    # not overridden, and the run is then as seedless as one with no flag at all.
    seed_flag = experiment_id.harness_seed_flag(bound)
    spec.capability = backend.capability(
        acq, acq.env_path, cfg.python,
        flag="" if (is_count or (bound and not seed_flag)) else (seed_flag.lstrip("-") or "seed"))
    if bound is not None and not seed_flag:
        # ponytail: 3 identical invocations show whether a seedless program is deterministic
        spec.seeds = spec.seeds[:3]
    spec.commit_state = repo_mod.verify_commit(
        acq.path, spec.commit, tree=backend.commit_tree(acq.path)).state
    proven, _cls, _why = experiment_id.identities_established(
        spec.experiment, spec.metric_identity, spec.configuration)

    if not cfg.allow_repo_exec:
        return spec

    fits = spec.resources is not None and spec.resources.established
    commit_ok = spec.commit_state == "verified"
    if not proven or not spec.capability.established or not fits or not commit_ok:
        return spec

    command = list(spec.experiment.command.argv) if spec.experiment.command else []
    if seed_flag and seed_flag not in command:
        command += [seed_flag, "{seed}"]
    spec.command = command
    spec.cwd = acq.path
    spec.interpreter = acq.env_path
    spec.arms = ["reproduction"]
    if spec.provenance == "driver":
        return spec
    spec.script = ""
    spec.provenance = "repo_exec"
    return spec


_DISPOSITION_FOR_RECONCILIATION = {
    "RESOLVED_VERIFIED": "REPRODUCED", "FAILED_REPRODUCTION": "FAILED_REPRODUCTION",
    "NOT_ATTEMPTED": "NOT_ATTEMPTED",
    # EXACT_CERTIFICATE's own pair — never folded into REPRODUCED/FAILED_REPRODUCTION,
    # which are about a printed cell (`execute.reconcile`'s cert_exec branch).
    "COUNTEREXAMPLE_FOUND": "COUNTEREXAMPLE_ESTABLISHED",
    "NO_VIOLATION_FOUND": "NO_COUNTEREXAMPLE_FOUND",
}
_DISPOSITION_FOR_FAILURE_CLASS = {
    "resources_insufficient": "RESOURCE_BLOCKED", "execution_unauthorized": "AUTHORIZATION_BLOCKED",
    "dependency_missing": "ENVIRONMENT_BLOCKED", "environment_failure": "ENVIRONMENT_BLOCKED",
    "platform_incompatible": "ENVIRONMENT_BLOCKED", "setup_failure": "ENVIRONMENT_BLOCKED",
    "infrastructure_failure": "ENVIRONMENT_BLOCKED", "experiment_unidentified": "IDENTITY_BLOCKED",
    "metric_unbound": "IDENTITY_BLOCKED", "configuration_unmatched": "IDENTITY_BLOCKED",
    "comparison_unestablished": "COMPARISON_BLOCKED", "commit_mismatch": "ARTIFACT_BLOCKED",
    "backend_unavailable": "ENVIRONMENT_BLOCKED", "credentials_unavailable": "ENVIRONMENT_BLOCKED",
}


def outcome_for(target_id: str, result: ProbeResult, action: str, route: str) -> TargetOutcome:
    """One target's terminal state, read off the ProbeResult the runner already wrote.
    Decides nothing: `disposition` comes from the reconciliation's own status, and
    `provenance` from the spec that ran, so the provenance ceiling reaches
    `TargetOutcome.establishes_failure` intact."""
    rec = result.reconciliation
    status = rec.status if rec else "NOT_ATTEMPTED"
    disposition = _DISPOSITION_FOR_RECONCILIATION.get(status, "")
    if not disposition:
        disposition = _DISPOSITION_FOR_FAILURE_CLASS.get(
            (rec.failure_class if rec else "") or "", "INCONCLUSIVE")
    identity_state = ""
    if result.experiment is not None:
        identity_state = result.experiment.state or ""
    elif rec is not None:
        identity_state = rec.experiment_state or ""

    return TargetOutcome(
        target_id=target_id, disposition=disposition, action=action, route=route,
        provenance=result.provenance, reconciliation=rec, identity_state=identity_state,
        failure_class=(rec.failure_class if rec else "none"),
        reason=(rec.reason if rec else result.reason) or result.reason,
        launched=result.executions,
        execution_ref=(result.execution_log or result.script_path or ""),
        authorized=(result.authorization.allowed if result.authorization is not None else None),
        attempts=1, evidence_kind=result.evidence_kind)


def _executable_targets(cfg: Config, pid: str):
    """(target, plan) pairs the planner authorised, in priority order. A target whose
    stored materiality basis is recognised bypasses the numeric budget — silently
    deferring the paper's material question because less consequential targets ranked
    ahead would turn an efficiency setting into a scientific policy."""
    from . import discover as discover_stage

    ts = discover_stage.load(cfg, pid)
    if ts is None:
        return None, [], []
    plans = {p.target_id: p for p in ts.plans if p.requires_execution}
    material_questions = {q.question_id for q in ts.questions if q.materiality == "CENTRAL"}
    pairs = [(o, plans[o.target_id]) for o in ts.objects if o.target_id in plans]
    cap = max(1, cfg.max_targets)
    # One budget PER ROUTE FAMILY. An exact certificate and an empirical run answer different
    # questions at different costs; a shared cap let cheap proof fragments, which sort first
    # on cost, spend the whole budget before any printed result was attempted.
    # ponytail: two families; a third route kind would get its own pool here.
    pursued, deferred, spent = [], [], {"certificate": 0, "empirical": 0}
    for obj, plan in pairs:
        basis = getattr(obj, "materiality_basis", "") or "NONE"
        family = decide.route_family(plan.route)
        if (decide.is_material(basis)
                or bool(obj.question_id and obj.question_id in material_questions)):
            pursued.append((obj, plan))
        elif spent[family] < cap:
            pursued.append((obj, plan))
            spent[family] += 1
        else:
            deferred.append((obj, plan))
    # Empirical first: the run loop's primary slot goes to the paper's printed results, and
    # certificates follow — each keeps its own relative order.
    pursued.sort(key=lambda pair: decide.route_family(pair[1].route) == "certificate")
    return ts, pursued, deferred


def admissible_if_it_succeeds(cfg: Config, spec: ProbeSpec) -> tuple[bool, str]:
    """May a process be started for this spec at all? A process is started only for a
    spec whose result would be ADMISSIBLE if it succeeded — running an inadmissible
    provenance spends compute on a number refused before the first process existed."""
    if provenance_mod.admits(spec.provenance):
        return True, ""
    if cfg.diagnostic_mode:
        return True, "diagnostic mode"
    why_no_author_code = (f"{spec.experiment.reason}; " if spec.experiment is not None
                          and spec.experiment.state in _IDENTITY_FAILURE_STATES
                          and spec.experiment.reason else "")
    return False, (
        f"{why_no_author_code}the only program available for this target was "
        f"{spec.provenance or 'unset'}, "
        f"which the provenance ceiling does not admit against a printed quantity in "
        f"either direction. Set SH_DIAGNOSTIC_MODE=1 to run it as a diagnostic; its "
        f"result is recorded separately and settles nothing.")


def _not_started(obj, plan, why: str, disposition: str = "IDENTITY_BLOCKED") -> TargetOutcome:
    """A target whose only available program could never have spoken. `launched=0` is a
    measurement, not a default — IDENTITY_BLOCKED rather than INCONCLUSIVE, because
    INCONCLUSIVE means "it ran and settled nothing"."""
    return TargetOutcome(target_id=obj.target_id, disposition=disposition,
                         action=plan.action, route=plan.route, launched=0, reason=why)


def _superseded_by_established_failure(obj, plan, stopper) -> TargetOutcome:
    return TargetOutcome(
        target_id=obj.target_id, disposition="SUPERSEDED_BY_ESTABLISHED_FAILURE",
        action=plan.action, route=plan.route, launched=0,
        reason=(f"a material failure was already established on target "
               f"{getattr(stopper, 'target_id', '?')}, so no further expensive "
               f"experiment was started for this paper; this target was not refused "
               f"and was not attempted"))


def establish_comparison(spec: ProbeSpec, route: str) -> ProbeSpec:
    """What this spec's result would be held against, derived from the ROUTE it took —
    every execution ends at `execute.reconcile`, which performs exactly one comparison."""
    spec.comparison = decide.derive_comparison(
        route, printed_value_available=bool((spec.claimed_cell_value or "").strip()))
    return spec


def may_be_compared(spec: ProbeSpec) -> tuple[bool, str]:
    """May a process start, given what its result could be held against? A spec with no
    comparison recorded passes, which keeps every hand-written spec.json behaving exactly
    as it did."""
    c = spec.comparison
    if decide.admits_verdict(c):
        return True, ""
    assert c is not None
    return False, (
        f"a {c.kind.lower().replace('_', ' ')} comparison would be needed to answer this "
        f"target and this review could not carry one out: {c.reason} Nothing was started, "
        f"because a run whose result has nothing to be held against cannot produce "
        f"evidence about this paper.")


_IDENTITY_FAILURE_STATES = ("ambiguous", "no_candidate", "unsupported")


def identity_failed(spec: ProbeSpec) -> bool:
    """Did identity resolution conclude the checkout could not be bound — on which
    command, which quantity, or which configuration?"""
    for ident in (spec.experiment, spec.metric_identity, spec.configuration):
        if ident is not None and ident.state in _IDENTITY_FAILURE_STATES:
            return True
    return False


def author_code_route_blocked(spec: ProbeSpec) -> bool:
    """Did AUTHOR_CODE_EXECUTION genuinely fail to bind — on ANY promotion gate — as
    opposed to never having been assessed at all?"""
    if spec.provenance == "repo_exec":
        return False
    if identity_failed(spec):
        return True
    if spec.capability is not None and not spec.capability.established:
        return True
    return False


def replan_after_author_code_exhausted(
        obj: DiscoveredObject, plan: PlanDecision, spec: ProbeSpec) -> PlanDecision | None:
    """Step 6's re-plan: fall back to INDEPENDENT_RECONSTRUCTION once author code has
    genuinely failed to bind. Three conditions, all required: the original plan chose
    AUTHOR_CODE_EXECUTION; the object's own routes offer the reconstruction; and the
    refusal was a genuine, ASSESSED one, not a gate this review never reached."""
    if plan.route != "AUTHOR_CODE_EXECUTION":
        return None
    if "INDEPENDENT_RECONSTRUCTION" not in obj.routes:
        return None
    if not author_code_route_blocked(spec):
        return None
    fallback = decide.plan(
        obj, artifact_available=True, specification_complete=True,
        investigation_open=True, author_code_exhausted=True, attempt=plan.attempt + 1)
    plan.superseded_by = fallback.route
    return fallback


def _reconstruction_gate_detail(cfg: Config, readiness: ReimplementationReadiness | None) -> str:
    """The ONE reason a reconstruction did not run — eligibility (the PAPER's own
    specification) checked FIRST, so a paper that fails it is never reported as "no
    reviewer configured"."""
    if readiness is not None and not readiness.established:
        missing = [i.kind for i in readiness.ingredients if not (i.ref or "").strip()]
        return (f"the paper does not specify enough to attempt a governed reconstruction "
               f"(missing: {', '.join(missing) if missing else 'a required ingredient'})")
    ok, why = reimplement_driver.available(cfg)
    if not ok:
        return f"no reviewer is configured to write one ({why})"
    if not cfg.allow_reimplementation_exec:
        return "SH_ALLOW_REIMPLEMENTATION_EXEC is not set"
    return ("either it did not produce a conformant reconstruction, or this backend's "
           "isolation is insufficient to run one — see execute.authorize's "
           "'reimpl_exec' branch")


def reconstruction_specification_blocked(readiness: ReimplementationReadiness | None) -> bool:
    """Is THIS the reason a reconstruction did not run: the paper's own specification,
    not one of this harness's gates?"""
    return readiness is not None and not readiness.established


def _fallback_note(cfg: Config, fallback_plan: PlanDecision | None,
                   readiness: ReimplementationReadiness | None = None) -> str:
    if fallback_plan is None:
        return ""
    if fallback_plan.requires_execution:
        detail = _reconstruction_gate_detail(cfg, readiness)
        return (f" A fallback to {fallback_plan.route} (attempt {fallback_plan.attempt}) "
               f"was also considered, because the paper specifies enough to attempt one; "
               f"it did not run: {detail}.")
    return (f" A fallback to {fallback_plan.route} (attempt {fallback_plan.attempt}) was "
           f"considered and refused: {fallback_plan.reason}")


def _direct_reconstruction_note(cfg: Config, plan: PlanDecision | None,
                                readiness: ReimplementationReadiness | None = None) -> str:
    if plan is None:
        return ""
    if plan.requires_execution:
        detail = _reconstruction_gate_detail(cfg, readiness)
        return (f" The planned {plan.route} route (attempt {plan.attempt}) did not run: "
               f"{detail}.")
    return (f" The planned {plan.route} route (attempt {plan.attempt}) was refused: "
           f"{plan.reason}")


def _full_paper_text(doc: PaperDoc) -> str:
    parts = []
    for s in doc.sections:
        parts.append(f"### {s.title or f'section {s.section_idx}'}  [s{s.section_idx}]\n"
                    + " ".join(s.text.split()))
    return "\n\n".join(parts)


_TABLE_IDX_REF = re.compile(r"^T(\d+)")
_SECTION_NAMED_REF = re.compile(r"^[SsPp](\d+)")


def _table_label_sections(doc: PaperDoc, table_idx: int | None) -> set[int]:
    """Sections whose text mentions this table's own printed label ("Table 8") — a
    generator's synthetic-data-generator constants and similar details can live only in an
    appendix section that never does anything but refer to the table by number."""
    if table_idx is None:
        return set()
    table = next((t for t in doc.tables if t.table_idx == table_idx), None)
    label = (table.label if table else "") or ""
    if not label:
        return set()
    pat = re.compile(rf"\bTable\s+{re.escape(label)}\b", re.IGNORECASE)
    return {s.section_idx for s in doc.sections if pat.search(s.text or "")}


def _ref_table_idx(ref: str) -> int | None:
    m = _TABLE_IDX_REF.match((ref or "").strip())
    return int(m.group(1)) if m else None


def _ref_section_idx(ref: str) -> int | None:
    """The section a locator names directly — 's12'/'S12' (section_span), 'P12:a-b'
    (prose_claim). A table/figure/equation ref names no section directly and resolves
    through `_table_label_sections` instead."""
    m = _SECTION_NAMED_REF.match((ref or "").strip())
    return int(m.group(1)) if m else None


# Vocabulary of how an experiment was run. A section dense in it (an "Experimental Setup"
# paragraph, a training-details appendix) is what a reconstruction most needs after the
# sections that name the target, whether or not it mentions the target's table.
_SETUP_TERMS = re.compile(
    r"\b(learning rate|batch size|epochs?|optimi[sz]er|adam|sgd|layers?|heads?|embedding"
    r"|dimension|hidden|seeds?|trained|training|hyper-?parameters?|samples?|steps?"
    r"|iterations?|warm-?up|weight decay|dropout|initiali[sz]ed|configuration)\b", re.I)
_NO_SETUP = re.compile(r"^(references|bibliography|acknowledg)", re.I)


def _scoped_paper_text(doc: PaperDoc, *, table_ref: str = "", claim_ref: str = "",
                       ingredient_refs: tuple[str, ...] = (), cap: int = 40_000) -> str:
    """The paper text a reconstruction generator brief needs, within `cap` characters.
    First the sections that name the target: its table's printed label, every section a
    readiness ingredient was found in, the section the claim cites, and the abstract. Then
    the rest of the paper fills the budget, densest in experimental-setup vocabulary first
    (bibliography never), so the setup and training details are never dropped merely for
    not naming the table. Kept in paper order, with an explicit note of what was left out.
    """
    wanted: set[int] = set()
    wanted |= _table_label_sections(doc, _ref_table_idx(table_ref))
    for ref in ingredient_refs:
        idx = _ref_section_idx(ref)
        wanted.add(idx) if idx is not None else wanted.update(
            _table_label_sections(doc, _ref_table_idx(ref)))
    claim_idx = _ref_section_idx(claim_ref)
    if claim_idx is not None:
        wanted.add(claim_idx)
    else:
        wanted |= _table_label_sections(doc, _ref_table_idx(claim_ref))
    abstract_idx = decide.abstract_section_idx(doc)
    wanted.add(abstract_idx if abstract_idx >= 0 else
              (doc.sections[0].section_idx if doc.sections else -1))
    wanted.discard(-1)

    def setup_density(sec) -> float:
        text = sec.text or ""
        return len(_SETUP_TERMS.findall(text)) / (1 + len(text) / 1000)

    rest = sorted((sec for sec in doc.sections if sec.section_idx not in wanted
                   and (sec.text or "").strip() and not _NO_SETUP.match(sec.title or "")),
                  key=setup_density, reverse=True)
    ordered = [sec for sec in doc.sections if sec.section_idx in wanted] + rest
    kept, used = {}, 0
    for sec in ordered:
        body = (f"### {sec.title or f'section {sec.section_idx}'}  [s{sec.section_idx}]\n"
               + " ".join(sec.text.split()))
        if used + len(body) > cap and kept:
            continue
        kept[sec.section_idx] = body
        used += len(body)
    parts = [kept[i] for i in sorted(kept)]
    omitted = [sec.section_idx for sec in doc.sections if sec.section_idx not in kept]
    if omitted:
        parts.append(f"[... sections omitted: {', '.join(f's{i}' for i in omitted)} ...]")
    return "\n\n".join(parts)


def attempt_certificate_check(
        cfg: Config, root: Path, pid: str, doc: PaperDoc, base_spec: ProbeSpec,
        plan_: PlanDecision | None, out_dir: Path | None = None) -> ProbeResult | None:
    """Execute a sealed EXACT_CERTIFICATE check. Returns None -- never a placeholder --
    whenever no CONFORMANT sealed certificate exists for this target yet: the target
    stays NOT_ATTEMPTED, awaiting one from the controlling session's task list
    (`certificate_targets` below lists exactly these targets). Modelled on
    `attempt_reimplementation_fallback` MINUS the repo/seed/sibling-table logic -- a
    certificate answers ONE theorem, not a shared experimental protocol.
    """
    if plan_ is None or not plan_.requires_execution:
        return None
    target_id = base_spec.target_id or "default"
    sealed = certificate.load_accepted(cfg, pid, target_id)
    if sealed is None:
        return None
    script, conf, instance_count = sealed
    if not conf.established:
        return None
    cspec = ProbeSpec(
        paper_id=pid, target_id=target_id, finding_id=base_spec.finding_id,
        claim=base_spec.claim, claim_ref=base_spec.claim_ref, claim_kind=base_spec.claim_kind,
        # A label only (value read from SH_METRIC). No table_ref: a theorem's bound is
        # never a printed cell.
        metric="certificate", seeds=list(range(max(1, instance_count))), arms=["certificate"],
        script=script, provenance="cert_exec", certificate_conformance=conf,
        written_by="harness")
    cspec = establish_comparison(cspec, plan_.route)
    cdir = out_dir or (root / "runs" / pid / "certificates")
    cdir.mkdir(parents=True, exist_ok=True)
    state.write_json(cdir / f"{target_id}.spec.json", cspec.model_dump())
    return execute.run_probe(cfg, root, cspec, out_dir=cdir)


def certificate_targets(cfg: Config, pid: str) -> list[tuple[str, str, str]]:
    """(target_id, generator_brief, base_brief_sha) for every EXACT_CERTIFICATE-routed
    target still owed an answer -- the pure, read-only helper a controlling session's task
    protocol consults. Spends no model call. A target is owed one when it has no seal for
    its CURRENT brief: the first brief, or a REVISION brief carrying a rejected attempt and
    its verifier's required changes (`reimplement_driver.record_attempt` bounds the rounds)."""
    from . import discover as discover_stage

    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return []
    doc = PaperDoc(**state.read_json(doc_path))
    ts = discover_stage.load(cfg, pid)
    if ts is None:
        return []
    # Only a target the plan will actually run: a certificate for a target outranked by a
    # central one would be generated and verified and then never executed.
    routed = {p.target_id for p in ts.plans
              if p.route == "EXACT_CERTIFICATE" and p.requires_execution}
    out: list[tuple[str, str]] = []
    for obj in ts.objects:
        if obj.target_id not in routed:
            continue
        kw = dict(claim=obj.claim_text, ref=obj.ref, step=obj.prebound_quote,
                  images=certificate.page_images(cfg, pid, doc, obj.ref, obj.claim_text))
        base = certificate.brief_sha(certificate.build_brief(doc, **kw))
        revision = reimplement_driver.read_revision(
            certificate.revision_path(cfg, pid, obj.target_id), base)
        brief = certificate.build_brief(doc, revision=revision, **kw) if revision else \
            certificate.build_brief(doc, **kw)
        # A sealed certificate answers only the brief it was given: when that brief has
        # since changed (upstream, or a revision round), the question is asked again.
        if certificate.load_accepted(cfg, pid, obj.target_id) is not None and (
                certificate.sealed_record(cfg, pid, obj.target_id).get("brief_sha256")
                == certificate.brief_sha(brief)):
            continue
        out.append((obj.target_id, brief, base))
    return out


def proof_map_targets(cfg: Config, pid: str) -> list[tuple[str, str, str]]:
    """(parent target_id, proof-map brief, statement label) for every pursued numbered
    result with a located proof and no proof map sealed for its current brief."""
    from . import discover as discover_stage

    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    ts = discover_stage.load(cfg, pid)
    if not doc_path.exists() or ts is None:
        return []
    doc = PaperDoc(**state.read_json(doc_path))
    pursued = {p.target_id for p in decide.current_plans(ts.plans) if p.requires_execution}
    sealed = certificate.load_proof_maps(cfg, pid)
    out: list[tuple[str, str, str]] = []
    for obj in ts.objects:
        if (obj.target_id not in pursued or obj.question_kind != "MATHEMATICAL_BOUND"
                or obj.parent_target or not certificate.statement_label(obj.claim_text)):
            continue
        brief = certificate.proof_map_brief(
            doc, claim=obj.claim_text, ref=obj.ref,
            images=certificate.page_images(cfg, pid, doc, obj.ref, obj.claim_text))
        if not brief or sealed.get(obj.target_id, {}).get("brief_sha256") == certificate.brief_sha(brief):
            continue
        out.append((obj.target_id, brief, certificate.statement_label(obj.claim_text)))
    return out


# === CHECK PLANS — dead-end central claims turned into checkable addresses ==============
# ScientistTwo's rebuttal planner, bounded to what REFEREE may do: a model PROPOSES which
# printed cell or number would settle a central concern (or the abstract's claims); the
# harness re-resolves it (invariant 1), routes it, and never lets the link reach
# materiality (invariants 7, 9).

def _plan_paths(cfg: Config, pid: str, unit: str) -> tuple[Path, Path]:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", unit)
    out = state.project_dir(cfg, pid) / "runs" / pid / "check_plans" / f"{safe}.json"
    return out, out.with_suffix(".driver.json")


def _tables_for_planner(doc: PaperDoc) -> str:
    # ponytail: 12 tables x 4 rows, 8000 chars — enough to name a headline cell; a planner
    # needing a deeper row cites the table and the review reports it unresolved.
    lines: list[str] = []
    # A figure's axis labels parse as a "table" of mostly empty cells: real tables — those
    # with a caption or printed label and mostly filled cells — are listed first.
    def filled(t) -> float:
        cells = [c for row in t.rows for c in row]
        return sum(1 for c in cells if str(c).strip()) / max(1, len(cells))
    ranked = sorted((t for t in doc.tables if t.rows and filled(t) >= 0.5),
                    key=lambda t: (not (t.caption or t.label), t.table_idx))
    for t in ranked[:12]:
        lines.append(f"T{t.table_idx} ({(t.caption or t.label or '').strip()[:160]})")
        if t.header:
            lines.append("  header: " + " | ".join(str(h) for h in t.header))
        for r, row in enumerate(t.rows[:4]):
            lines.append(f"  r{r}: " + " | ".join(f"[{t.ref(r, c)}] {cell}"
                                                  for c, cell in enumerate(row)))
    return "\n".join(lines)[:8000]


def check_plan_targets(cfg: Config, pid: str) -> list[tuple[str, str, str]]:
    """(unit, brief, question_id) for the abstract (when nothing in it is a target yet)
    and each CENTRAL question whose every bound target reached no route — capped at
    `cfg.max_check_plans`, skipping any unit already planned for its current brief."""
    from . import discover as discover_stage
    from .prompts import certificate as CP

    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    ts = discover_stage.load(cfg, pid)
    if not doc_path.exists() or ts is None:
        return []
    doc = PaperDoc(**state.read_json(doc_path))
    tables = _tables_for_planner(doc)
    units: list[tuple[str, str, str, str, str]] = []   # unit, qid, subject, evidence, ref
    abstract = decide.abstract_section_idx(doc)
    # The abstract is planned for unless one of its claims is already being TESTED. A
    # quotation re-check or a no-route target in the abstract tests nothing (invariant 10).
    executing = {p.target_id for p in ts.plans if p.requires_execution}
    if abstract >= 0 and not any(o.ref is not None and o.ref.section_idx == abstract
                                 and o.target_id in executing for o in ts.objects):
        text = next((" ".join((s.text or "").split()) for s in doc.sections
                     if s.section_idx == abstract), "")
        units.append(("abstract", discover_stage.ABSTRACT_QUESTION,
                      "The paper's headline claims, as its abstract states them.", text, ""))
    for q in sorted(ts.questions, key=lambda q: q.question_id):
        bound = [o for o in ts.objects if o.question_id == q.question_id]
        if q.materiality == "CENTRAL" and bound and all(o.routes == ["NONE"] for o in bound):
            ref = bound[0].ref
            units.append((q.question_id, q.question_id, f"{q.question} {bound[0].claim_text}",
                          getattr(ref, "quote", "") or "", getattr(ref, "ref", "") or ""))
    out: list[tuple[str, str, str]] = []
    for unit, qid, subject, evidence, ref in units[:max(0, cfg.max_check_plans)]:
        brief = CP.check_plan_build(doc.title, subject, evidence, tables,
                                    _scoped_paper_text(doc, claim_ref=ref, cap=20_000))
        brief = brief.replace(chr(0), "")
        out_path, sidecar = _plan_paths(cfg, pid, unit)
        try:
            done = sidecar.is_file() and state.read_json(sidecar).get("brief_sha256") \
                == certificate.brief_sha(brief)
        except (OSError, ValueError):
            done = False
        if not done:
            out.append((unit, brief, qid))
    return out


def accept_check_plan(cfg: Config, pid: str, unit: str, raw: str, *, question_id: str,
                      brief: str, reviewer: str = "") -> dict:
    """Validate and seal one planner answer. A proposal is KEPT only when the harness
    re-resolves it to a table cell or a printed quantity; every drop says why."""
    from . import delegation, sealing

    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"check_plan:{unit}: no JSON object in the output")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"check_plan:{unit}: output is not valid JSON: {e}") from e
    doc = PaperDoc(**state.read_json(state.project_dir(cfg, pid) / "paper" / "doc.json"))
    kept, dropped = [], []
    for p in (data.get("proposals") or [])[:2] if isinstance(data, dict) else []:
        if not isinstance(p, dict):
            continue
        ref_s, quote = str(p.get("ref") or "").strip(), str(p.get("quote") or "").strip()
        got = (locate.resolve(doc, ref_s) if ref_s else locate.mint(doc, quote)) \
            if (ref_s or quote) else None
        ok = got is not None and got.resolved and (
            got.kind == "table_cell" or (got.quantity is not None and got.quantity.value is not None))
        (kept if ok else dropped).append({
            "ref": got.ref if ok else (ref_s or quote[:120]),
            "why": " ".join(str(p.get("why") or "").split())[:300],
            **({} if ok else {"dropped": "not re-resolved to a printed cell or quantity"})})
    prov = delegation.provenance_record(mode="SESSION_SUBAGENT", reviewer=reviewer)
    out, _sidecar = _plan_paths(cfg, pid, unit)
    return sealing.seal(
        out, {"unit": unit, "question_id": question_id, "refs": kept, "dropped": dropped,
              "none": str(data.get("none") or "")[:500] if isinstance(data, dict) else ""},
        mode="SESSION_SUBAGENT", reviewer=prov["reviewer"], tool_policy=prov["tool_policy"],
        extra={"paper_id": pid, "unit": unit, "brief_sha256": certificate.brief_sha(brief)})


def load_check_plans(cfg: Config, pid: str) -> list[dict]:
    from . import delegation, sealing

    d = state.project_dir(cfg, pid) / "runs" / pid / "check_plans"
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        if f.name.endswith(".driver.json") or not sealing.verify_seal(
                f, accepted_writers=tuple(delegation.WRITTEN_BY.values()))[0]:
            continue
        try:
            out.append(state.read_json(f))
        except (OSError, ValueError):
            continue
    return out


def _certificate_refusal(cfg: Config, pid: str, obj, plan) -> TargetOutcome:
    """A target whose certificate is not (yet) conformant. UNCHECKABLE is a limit of this
    method for this relation — NO_ROUTE_AVAILABLE, never a finding about the paper; any
    other refusal stays NOT_ATTEMPTED and its reason says whether a revision is owed."""
    rec = certificate.sealed_record(cfg, pid, obj.target_id)
    disposition = ("NO_ROUTE_AVAILABLE" if rec and not rec.get("established")
                   and rec.get("verdict") == "UNCHECKABLE" else "NOT_ATTEMPTED")
    return _not_started(obj, plan, _certificate_pending_reason(cfg, pid, obj.target_id),
                        disposition)


def _certificate_pending_reason(cfg: Config, pid: str, target_id: str) -> str:
    rec = certificate.sealed_record(cfg, pid, target_id)
    if rec and not rec.get("established"):
        verdict = rec.get("verdict") or "REVISE"
        attempt = int(rec.get("attempt") or 1)
        tail = ("the verifier judged this relation not finitely checkable by this method"
                if verdict == "UNCHECKABLE" else
                f"a revision is owed (attempt {attempt} of {1 + max(0, cfg.max_revisions)})"
                if attempt < 1 + max(0, cfg.max_revisions) else
                f"the revision budget is spent ({attempt} attempt(s))")
        return (f"the independent verifier did not approve the certificate generated for this "
                f"target ({verdict}; {tail}), so nothing ran and no verdict may be drawn. "
                f"Required changes: "
                + " ".join(str(rec.get("required_changes") or "none given").split())[:400]
                + ". Verifier: "
                + " ".join(str(rec.get("verifier_notes") or "no reason recorded").split())[:600])
    return ("no sealed, conformant EXACT_CERTIFICATE exists yet for this target; awaiting one "
            "from the controlling session's task list.")


def attempt_reimplementation_fallback(
        cfg: Config, root: Path, pid: str, doc: PaperDoc, base_spec: ProbeSpec,
        fallback_plan: PlanDecision | None, out_dir: Path | None = None,
        acq: RepoAcquisition | None = None) -> ProbeResult | None:
    """Execute the governed-reconstruction fallback. Returns None -- never a placeholder
    -- whenever no CONFORMANT sealed reconstruction exists to run. The eventual run still
    goes through the ordinary `execute.authorize` path (`reimpl_exec` branch).

    Generation is asynchronous: with no accepted reconstruction sealed yet, this persists
    the generator's brief (`reimplement_driver.persist_brief`) for `harness.tasks.pending`
    to list as a `reimpl_gen` task, and returns None. The next pipeline pass that reaches
    here finds the sealed reconstruction (once a generator AND a separately-attributed
    verifier subagent have both answered) via `load_accepted` above and proceeds.
    """
    from . import discover as discover_stage

    if fallback_plan is None or not fallback_plan.requires_execution:
        return None
    readiness = discover_stage.reimplementation_readiness(
        doc, base_spec.claim_ref or base_spec.table_ref)
    checkout = (Path(acq.path) if acq is not None and acq.path
                and acq.status in ("cloned", "cached") else None)
    released = reimplement_driver.released_files(checkout) if checkout is not None else []
    # With the authors' own data released, a missing TRAINING description does not make
    # the paper unreconstructable: a statistic of released outputs trains nothing.
    # `reimplement_driver.conformance` still requires every other ingredient bound.
    if not readiness.established and not (released and set(readiness.missing) <= {"training"}):
        return None
    target_id = base_spec.target_id or "default"
    manifest = reimplement_driver.released_manifest_path(cfg, pid, target_id)
    if released:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        state.write_json(manifest, released)
    sealed = reimplement_driver.load_accepted(cfg, pid, target_id)
    brief = reimplement_driver.build_brief(
        readiness, paper_title=doc.title, claim=base_spec.claim,
        table_ref=base_spec.table_ref, claimed_cell_value=base_spec.claimed_cell_value,
        paper_text=_scoped_paper_text(
            doc, table_ref=base_spec.table_ref, claim_ref=base_spec.claim_ref,
            ingredient_refs=tuple(i.ref for i in readiness.ingredients)),
        released=released)
    # The base brief's sha is what a revision round is keyed to; a REVISE verdict with
    # budget left appends the rejected attempt and its required changes.
    base_sha = certificate.brief_sha(brief)
    base_file = reimplement_driver.base_sha_path(cfg, pid, target_id)
    base_file.parent.mkdir(parents=True, exist_ok=True)
    base_file.write_text(base_sha, encoding="utf-8")
    revision = reimplement_driver.read_revision(
        reimplement_driver.revision_path(cfg, pid, target_id), base_sha)
    if revision:
        from .prompts import reimplement as RP
        brief += RP.revision_block(revision)
    stale = (sealed is not None and not sealed[1].established
             and not reimplement_driver.answered_brief(cfg, pid, target_id, brief))
    if sealed is None or stale:
        ok, _why = reimplement_driver.available(cfg)
        if ok:
            reimplement_driver.persist_brief(cfg, pid, target_id, brief)
        return None
    script, conf = sealed
    if not conf.established:
        rec = reimplement_driver.sealed_record(cfg, pid, target_id)
        conf = conf.model_copy(update={"reason": (
            f"{conf.reason}. The independent verifier ({rec.get('verdict') or 'REVISE'}, "
            f"attempt {rec.get('attempt') or 1}): "
            f"{' '.join(str(rec.get('verifier_notes') or 'no reason recorded').split())[:600]}")})
    # Cells of one table share one experiment's protocol. If an independent verifier
    # refused a sibling reconstruction of that table, this one may not settle a cell.
    table = re.match(r"T\d+", base_spec.table_ref or "")
    if conf.established and table:
        rdir = root / "runs" / pid / "reimplementation"
        for f in sorted(rdir.glob(f"TGT-*-{table.group()}r*.json")):
            sib = f.stem
            if sib == target_id or sib.endswith((".driver", ".spec", ".superseded",
                                                 ".inputs", ".revision")):
                continue
            other = reimplement_driver.load_accepted(cfg, pid, sib)
            if other is not None and not other[1].established:
                conf = conf.model_copy(update={"established": False, "reason": (
                    f"sibling reconstruction {sib} of the same table was not approved by its "
                    f"independent verifier; a shared-protocol assumption is unsettled, so no "
                    f"cell of {table.group()} is reconciled from a reconstruction")})
                break
    # The paper's replication count replaces the harness default only once its quote is
    # re-found in the paper and names that number as a count of seeds/runs/trials.
    seeds = list(base_spec.seeds) or [0, 1, 2]
    quote = " ".join((conf.replication_quote or "").split())
    if (conf.replication_runs and re.search(
            rf"\b{conf.replication_runs}\b.{{0,30}}\b(seed|run|trial|repetition|repeat|time)s?\b"
            rf"|\b(seed|run|trial|repetition|repeat)s?\b.{{0,30}}\b{conf.replication_runs}\b",
            quote, re.I)
            and quote in " ".join(_full_paper_text(doc).split())):
        seeds = list(range(conf.replication_runs))
    # A recomputation's inputs are re-hashed NOW: a released file that changed since the
    # generator saw it is not the file the verifier approved, so nothing may be concluded.
    if conf.released_inputs:
        now = {f"{r['path']}@sha256:{r['sha256']}" for r in released}
        drift = [x for x in conf.released_inputs if x not in now]
        if drift:
            conf = conf.model_copy(update={"established": False, "reason": (
                "the released file(s) " + ", ".join(drift) + " no longer match the pinned "
                "checkout's bytes, so this recomputation is not about the approved data")})
    fspec = ProbeSpec(
        paper_id=pid, target_id=target_id, finding_id=base_spec.finding_id,
        claim=base_spec.claim, claim_ref=base_spec.claim_ref, claim_kind=base_spec.claim_kind,
        table_ref=base_spec.table_ref, claimed_cell_value=base_spec.claimed_cell_value,
        # A label only (the value is read from SH_METRIC). Unset means the target named no
        # metric; it is then labelled by the printed cell it is compared against.
        metric=(base_spec.metric
                or f"printed value at {base_spec.table_ref or base_spec.claim_ref}"),
        seeds=seeds, arms=["reproduction"], script=script, provenance="reimpl_exec",
        interpreter=(acq.env_path if acq is not None else "") or "",
        cwd=(str(checkout) if conf.released_inputs and checkout is not None else ""),
        reimplementation_conformance=conf, written_by="harness")
    fspec = establish_comparison(fspec, fallback_plan.route)
    fdir = out_dir or (root / "runs" / pid / "reimplementation")
    fdir.mkdir(parents=True, exist_ok=True)
    state.write_json(fdir / f"{target_id}.spec.json", fspec.model_dump())

    if (acq is not None and acq.env_path and not acq.dependency_files
           and cfg.exec_backend == "container"):
        from . import container as container_mod
        mount = str(Path(acq.path).parent) if acq.path else ""
        image = container_mod.image_for(getattr(acq, "declared_python", "") or "")[0]
        if mount:
            ok, why = container_mod.install_reconstruction_baseline(
                acq.env_path, mount, image, cfg.install_timeout_s)
            if not ok:
                acq.reason = (acq.reason + "; " if acq.reason else "") + why

    return execute.run_probe(cfg, root, fspec, out_dir=fdir)


def run(cfg: Config, pid: str) -> dict:
    """S3 for one paper. Wraps `_review` so a leased machine is always given back — a
    backend that leases remote compute bills from creation until something terminates it."""
    try:
        return _review(cfg, pid)
    finally:
        try:
            released, detail = execute.select_backend(cfg).release(
                state.project_dir(cfg, pid), pid)
            if released:
                state.append_log(cfg, pid, artifact_type="sandbox_release", phase="probe",
                                 headers={"detail": detail}, path="")
        except Exception:                                # noqa: BLE001
            pass


def _review(cfg: Config, pid: str) -> dict:
    from . import discover as discover_stage

    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))
    state.set_phase(cfg, pid, "probe")
    reconstruction_readiness = discover_stage.reimplementation_readiness(doc)

    target_set, pairs, deferred = _executable_targets(cfg, pid)
    initial_stopper = (decide.material_target_failure(
        target_set.objects, target_set.outcomes) if target_set is not None else None)
    if initial_stopper is not None and (pairs or deferred):
        superseded = [_superseded_by_established_failure(obj, plan, initial_stopper)
                     for obj, plan in pairs + deferred]
        superseded_ids = {out.target_id for out in superseded}
        target_set.outcomes = [out for out in target_set.outcomes
                               if out.target_id not in superseded_ids] + superseded
        by_outcome = {out.target_id: out.disposition for out in superseded}
        for obj in target_set.objects:
            if obj.target_id in by_outcome:
                obj.status = by_outcome[obj.target_id]
        target_set.extraction_coverage["targets_superseded_by_established_failure"] = sum(
            1 for out in target_set.outcomes
            if out.disposition == "SUPERSEDED_BY_ESTABLISHED_FAILURE")
        discover_stage.sync_questions(target_set)
        decide.refresh_route_attempts(target_set, cfg)
        state.write_json(discover_stage.targets_path_in(root), target_set.model_dump())
        return {"paper_id": pid, "verdict": "not_started",
               "reason": (f"a material failure was already established on target "
                          f"{getattr(initial_stopper, 'target_id', '?')}; all pending expensive "
                          f"targets were recorded as superseded and no acquisition or execution "
                          f"was started"),
               "executions": 0, "superseded_targets": len(superseded)}

    primary = pairs[0][0] if pairs else None
    spec = build_spec(cfg, pid, doc, primary)
    acq, audit = acquire_and_audit(cfg, root, pid, doc, spec)
    spec = synthesize_probe(cfg, doc, spec, acq)
    spec = plan_execution(cfg, spec, acq, doc, audit, root=root)
    if pairs:
        spec = establish_comparison(spec, pairs[0][1].route)

    if spec.written_by == "harness":
        state.write_json(state.control_dir(root) / "spec.json", spec.model_dump())

    comparable, uncomparable_why = may_be_compared(spec)
    if not comparable:
        may_run, refusal = False, uncomparable_why
    else:
        may_run, refusal = admissible_if_it_succeeds(cfg, spec)

    original_plan = pairs[0][1] if pairs else None
    direct_reconstruction = bool(
        original_plan is not None and original_plan.route == "INDEPENDENT_RECONSTRUCTION")
    # EXACT_CERTIFICATE is its OWN third branch, dispatched separately below: its script
    # comes from a SEALED subagent artifact, never from `synthesize_probe`/`plan_execution`.
    direct_certificate = bool(
        original_plan is not None and original_plan.route == "EXACT_CERTIFICATE")
    fallback_plan = (replan_after_author_code_exhausted(pairs[0][0], original_plan, spec)
                     if pairs and not direct_certificate else None)
    if fallback_plan is not None and target_set is not None:
        target_set.plans.append(fallback_plan)
    reconstruction_plan = original_plan if direct_reconstruction else fallback_plan
    reconstruction_result = None
    if not direct_certificate and comparable and (
            direct_reconstruction or not may_run or fallback_plan is not None):
        reconstruction_result = attempt_reimplementation_fallback(
            cfg, root, pid, doc, spec, reconstruction_plan, acq=acq)
    certificate_result = (attempt_certificate_check(cfg, root, pid, doc, spec, original_plan)
                          if direct_certificate else None)
    if direct_certificate and certificate_result is not None:
        result = certificate_result
    elif direct_certificate:
        result = ProbeResult(
            paper_id=pid, verdict="not_started", provenance="cert_exec",
            reason=_certificate_pending_reason(cfg, pid, pairs[0][0].target_id if pairs else ""),
            executions=0, script_path="")
    elif direct_reconstruction and reconstruction_result is not None:
        result = reconstruction_result
    elif direct_reconstruction:
        result = ProbeResult(
            paper_id=pid, verdict="not_started", provenance="reimpl_exec",
            reason=(uncomparable_why if not comparable else
                   _direct_reconstruction_note(cfg, reconstruction_plan).strip()),
            executions=0, script_path="")
    elif may_run:
        result = execute.run_probe(cfg, root, spec)
    elif reconstruction_result is not None:
        result = reconstruction_result
    else:
        result = ProbeResult(
            paper_id=pid, verdict="not_started", provenance=spec.provenance,
            reason=refusal, executions=0, script_path="",
            experiment=spec.experiment, metric_identity=spec.metric_identity,
            configuration=spec.configuration, capability=spec.capability,
            resources=spec.resources, commit_state=spec.commit_state, backend=spec.backend)
    result.repo, result.code_audit = acq, audit
    if pairs:
        state.write_json(state.control_dir(root) / "probe_results.json", result.model_dump())

    outcomes: list[TargetOutcome] = []
    if target_set is not None and pairs:
        if direct_certificate and certificate_result is not None:
            outcomes.append(outcome_for(pairs[0][0].target_id, certificate_result,
                                        original_plan.action, original_plan.route))
        elif direct_certificate:
            outcomes.append(_certificate_refusal(cfg, pid, pairs[0][0], pairs[0][1]))
        elif reconstruction_result is not None and reconstruction_plan is not None:
            outcomes.append(outcome_for(pairs[0][0].target_id, reconstruction_result,
                                        reconstruction_plan.action, reconstruction_plan.route))
        elif may_run and not direct_reconstruction:
            outcomes.append(outcome_for(pairs[0][0].target_id, result,
                                        pairs[0][1].action, pairs[0][1].route))
        else:
            note = (_direct_reconstruction_note(cfg, reconstruction_plan, reconstruction_readiness)
                   if direct_reconstruction else
                   (_fallback_note(cfg, fallback_plan, reconstruction_readiness) if comparable else ""))
            spec_blocked = (comparable
                           and (direct_reconstruction or fallback_plan is not None)
                           and reconstruction_specification_blocked(reconstruction_readiness))
            outcomes.append(_not_started(
                pairs[0][0], pairs[0][1],
                (uncomparable_why if not comparable else refusal + note),
                ("SPECIFICATION_BLOCKED" if spec_blocked else
                "AUTHORIZATION_BLOCKED" if direct_reconstruction and comparable
                else "IDENTITY_BLOCKED" if comparable else "COMPARISON_BLOCKED")))
        if (comparable and not direct_reconstruction and fallback_plan is not None
               and reconstruction_result is None):
            outcomes.append(TargetOutcome(
                target_id=pairs[0][0].target_id,
                disposition=("SPECIFICATION_BLOCKED"
                            if reconstruction_specification_blocked(reconstruction_readiness)
                            else "AUTHORIZATION_BLOCKED"),
                action=fallback_plan.action, route=fallback_plan.route, launched=0,
                reason=(_fallback_note(cfg, fallback_plan, reconstruction_readiness).strip()
                       or "a fallback to this route was considered and did not run"),
                failure_class=("none"
                              if reconstruction_specification_blocked(reconstruction_readiness)
                              else "execution_unauthorized")))
        for obj, plan in pairs[1:]:
            # THE DYNAMIC EARLY STOP — the same call `claim_status` makes (Tier 1 AND
            # Tier 2), so this adds no second rule about what a material failure is.
            stopper = decide.material_target_failure(target_set.objects, outcomes)
            if stopper is not None:
                outcomes.append(_superseded_by_established_failure(obj, plan, stopper))
                continue
            try:
                if plan.route == "EXACT_CERTIFICATE":
                    # Its OWN dispatch, like the primary target above: a sealed subagent
                    # artifact or nothing.
                    other_base = build_spec(cfg, pid, doc, obj)
                    tdir = root / "runs" / pid / "targets" / obj.target_id
                    ctdir = state.control_dir(root) / "targets" / obj.target_id
                    cert_result = attempt_certificate_check(
                        cfg, root, pid, doc, other_base, plan, out_dir=tdir / "certificates")
                    if cert_result is not None:
                        state.write_json(ctdir / "probe_results.json", cert_result.model_dump())
                        outcomes.append(outcome_for(obj.target_id, cert_result,
                                                    plan.action, plan.route))
                    else:
                        outcomes.append(_certificate_refusal(cfg, pid, obj, plan))
                    continue
                other = build_spec(cfg, pid, doc, obj)
                other.written_by = "harness"
                other = synthesize_probe(cfg, doc, other, acq)
                other = plan_execution(cfg, other, acq, doc, audit, root=root)
                other = establish_comparison(other, plan.route)
                tdir = root / "runs" / pid / "targets" / obj.target_id
                ctdir = state.control_dir(root) / "targets" / obj.target_id
                state.write_json(ctdir / "spec.json", other.model_dump())
                other_fallback = replan_after_author_code_exhausted(obj, plan, other)
                if other_fallback is not None:
                    target_set.plans.append(other_fallback)
                other_direct_reconstruction = plan.route == "INDEPENDENT_RECONSTRUCTION"
                other_reconstruction = plan if other_direct_reconstruction else other_fallback
                comparable_here, why_here = may_be_compared(other)
                if not comparable_here:
                    outcomes.append(_not_started(obj, plan, why_here, "COMPARISON_BLOCKED"))
                    continue
                ok_here, why_here = admissible_if_it_succeeds(cfg, other)
                if other_direct_reconstruction or not ok_here or other_fallback is not None:
                    other_reconstruction_result = attempt_reimplementation_fallback(
                        cfg, root, pid, doc, other, other_reconstruction,
                        out_dir=tdir / "reimplementation", acq=acq)
                    if (other_reconstruction_result is not None
                           and other_reconstruction is not None):
                        state.write_json(ctdir / "probe_results.json",
                                        other_reconstruction_result.model_dump())
                        outcomes.append(outcome_for(
                            obj.target_id, other_reconstruction_result,
                            other_reconstruction.action, other_reconstruction.route))
                        continue
                    note = (_direct_reconstruction_note(cfg, other_reconstruction,
                                                       reconstruction_readiness)
                           if other_direct_reconstruction else
                           _fallback_note(cfg, other_fallback, reconstruction_readiness))
                    other_spec_blocked = (
                        (other_direct_reconstruction or other_fallback is not None)
                        and reconstruction_specification_blocked(reconstruction_readiness))
                    outcomes.append(_not_started(
                        obj, plan, why_here + note,
                        ("SPECIFICATION_BLOCKED" if other_spec_blocked else
                        "AUTHORIZATION_BLOCKED" if other_direct_reconstruction
                        else "IDENTITY_BLOCKED")))
                    if not other_direct_reconstruction and other_fallback is not None:
                        outcomes.append(TargetOutcome(
                            target_id=obj.target_id,
                            disposition=("SPECIFICATION_BLOCKED" if other_spec_blocked
                                        else "AUTHORIZATION_BLOCKED"),
                            action=other_fallback.action, route=other_fallback.route, launched=0,
                            reason=(_fallback_note(cfg, other_fallback,
                                                  reconstruction_readiness).strip()
                                   or "a fallback to this route was considered and did "
                                      "not run"),
                            failure_class=("none" if other_spec_blocked
                                          else "execution_unauthorized")))
                    continue
                tres = execute.run_probe(cfg, root, other, out_dir=tdir, results_dir=ctdir)
                state.write_json(ctdir / "probe_results.json", tres.model_dump())
                outcomes.append(outcome_for(obj.target_id, tres, plan.action, plan.route))
            except Exception as e:                # noqa: BLE001
                outcomes.append(TargetOutcome(
                    target_id=obj.target_id, disposition="INCONCLUSIVE",
                    action=plan.action, route=plan.route,
                    reason=f"pursuing this target raised {type(e).__name__}: {e}. Recorded as "
                          f"inconclusive; a fault in this harness is not evidence about the "
                          f"paper."))
        for obj, plan in deferred:
            outcomes.append(TargetOutcome(
                target_id=obj.target_id, disposition="BUDGET_DEFERRED",
                action=plan.action, route=plan.route, launched=0,
                reason=(f"an experiment is warranted for this target and this run's "
                       f"{decide.route_family(plan.route)} budget of {cfg.max_targets} "
                       f"target(s) was already spent on higher-priority ones. A limit of this "
                       f"run, not of the paper.")))
        for out in outcomes:
            odir = state.control_dir(root) / "targets" / out.target_id
            state.write_json(odir / "outcome.json", out.model_dump())
        keep = [o for o in target_set.outcomes if o.target_id not in {x.target_id for x in outcomes}]
        target_set.outcomes = keep + outcomes
        by_outcome = {o.target_id: o.disposition for o in outcomes}
        for obj in target_set.objects:
            if obj.target_id in by_outcome:
                obj.status = by_outcome[obj.target_id]
        discover_stage.sync_questions(target_set)
        decide.refresh_route_attempts(target_set, cfg)
        state.write_json(discover_stage.targets_path_in(root), target_set.model_dump())

    artifact_outcomes, inspection = artifact_stage.run_route(
        cfg, pid, doc, target_set, acq.path, url=acq.url or (doc.repo_url or ""))
    if target_set is not None and artifact_outcomes:
        for out in artifact_outcomes:
            state.write_json(
                state.control_dir(root) / "targets" / out.target_id / "outcome.json",
                out.model_dump())
        replaced = {o.target_id for o in artifact_outcomes}
        target_set.outcomes = [o for o in target_set.outcomes
                               if o.target_id not in replaced] + artifact_outcomes
        by_artifact = {o.target_id: o.disposition for o in artifact_outcomes}
        for obj in target_set.objects:
            if obj.target_id in by_artifact:
                obj.status = by_artifact[obj.target_id]
        discover_stage.sync_questions(target_set)
        decide.refresh_route_attempts(target_set, cfg)
        state.write_json(discover_stage.targets_path_in(root), target_set.model_dump())

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
        path=str(state.control_dir(root) / "probe_results.json"))
    return {"paper_id": pid, "verdict": result.verdict, "device": result.device,
           "finding_id": result.finding_id or None,
           "measured_delta": result.measured_delta, "measured_std": result.measured_std,
           "noise_band": result.noise_band, "claimed_delta": result.claimed_delta,
           "claim_within_noise": result.claim_within_noise,
           "seeds_run": result.seeds_run, "seeds_failed": result.seeds_failed,
           "repo": acq.status, "repo_url": acq.url or None, "entrypoint": acq.entrypoint or None,
           "frameworks": acq.frameworks, "env": acq.env_status,
           "provenance": result.provenance, "mechanism": result.mechanism or None,
           "code_findings": len(audit.findings), "code_audit_skipped": audit.skipped or None,
           "artifact_targets": len(artifact_outcomes),
           "artifact_facts": len(inspection.facts) if inspection else 0,
           "reconciliation": rec.status if rec else None,
           "seconds": result.seconds, "reason": result.reason,
           "targets": f"discovery/targets.json"}


def resync_cached_outcomes(cfg: Config, pid: str) -> dict:
    """Re-attach already-produced `TargetOutcome`s onto a freshly discovered target set --
    recovers a cached second pass that would otherwise lose every prior execution's
    outcome. Spends no execution; repairs bookkeeping only."""
    from . import discover as discover_stage

    root = state.project_dir(cfg, pid)
    primary_path = state.control_dir(root) / "probe_results.json"
    if not primary_path.exists():
        return {"resynced": 0, "reason": "no cached probe result"}

    target_set, pairs, deferred = _executable_targets(cfg, pid)
    if target_set is None or not pairs:
        return {"resynced": 0, "reason": "no executable target this discover pass"}

    outcomes: list[TargetOutcome] = []
    primary_obj, primary_plan = pairs[0]
    primary_result = ProbeResult(**state.read_json(primary_path))
    primary_outcome_path = (state.control_dir(root) / "targets" /
                            primary_obj.target_id / "outcome.json")
    # The cached primary result belongs to whichever target was primary WHEN IT RAN. Target
    # order can change between passes, so it is attached only to the target it names.
    rec = primary_result.reconciliation
    ran_for = (rec.target_id if rec is not None else "") or ""
    if primary_outcome_path.exists():
        outcomes.append(TargetOutcome(**state.read_json(primary_outcome_path)))
    elif ran_for == primary_obj.target_id:
        outcomes.append(outcome_for(primary_obj.target_id, primary_result,
                                    primary_plan.action, primary_plan.route))
    else:
        outcomes.append(TargetOutcome(
            target_id=primary_obj.target_id, disposition="NOT_ATTEMPTED",
            action=primary_plan.action, route=primary_plan.route, launched=0,
            reason=(f"the cached primary result was produced for "
                    f"{ran_for or 'an unrecorded target'}, not for this one, so it is not "
                    f"attached here; this target has no durable result of its own yet.")))

    for obj, plan in pairs[1:]:
        cached_outcome = state.control_dir(root) / "targets" / obj.target_id / "outcome.json"
        cached = state.control_dir(root) / "targets" / obj.target_id / "probe_results.json"
        if cached_outcome.exists():
            outcomes.append(TargetOutcome(**state.read_json(cached_outcome)))
        elif cached.exists():
            tres = ProbeResult(**state.read_json(cached))
            outcomes.append(outcome_for(obj.target_id, tres, plan.action, plan.route))
        else:
            outcomes.append(TargetOutcome(
                target_id=obj.target_id, disposition="NOT_ATTEMPTED",
                action=plan.action, route=plan.route, launched=0,
                reason=("a cached primary result exists, but this target has no durable "
                       "result or outcome record. This is a legacy harness persistence "
                       "boundary, not evidence about the paper.")))

    by_pair = {obj.target_id: (obj, plan) for obj, plan in pairs}
    for out in outcomes:
        pair = by_pair.get(out.target_id)
        if pair is None or out.route != "INDEPENDENT_RECONSTRUCTION":
            continue
        obj, plan = pair
        if plan.route != "AUTHOR_CODE_EXECUTION":
            continue
        spec_path = (state.control_dir(root) / "spec.json" if obj is primary_obj else
                    state.control_dir(root) / "targets" / obj.target_id / "spec.json")
        if not spec_path.exists():
            continue
        fallback = replan_after_author_code_exhausted(
            obj, plan, ProbeSpec(**state.read_json(spec_path)))
        if fallback is not None:
            target_set.plans.append(fallback)

    keep = [o for o in target_set.outcomes if o.target_id not in {x.target_id for x in outcomes}]
    target_set.outcomes = keep + outcomes
    by_outcome = {o.target_id: o.disposition for o in outcomes}
    for obj in target_set.objects:
        if obj.target_id in by_outcome:
            obj.status = by_outcome[obj.target_id]
    discover_stage.sync_questions(target_set)
    decide.refresh_route_attempts(target_set, cfg)
    state.write_json(discover_stage.targets_path_in(root), target_set.model_dump())
    return {"resynced": len(outcomes), "pursued": len(pairs), "deferred": len(deferred)}


# --------------------------------------------------------------------------------------- #
def _self_check() -> None:
    from .schema import PaperDoc, Section, Table

    doc = PaperDoc(paper_id="p", sections=[Section(section_idx=0, title="M", text="text")],
                   tables=[Table(table_idx=1, rows=[["59.3"]])])
    assert cell_contents(doc, "T1:r0:c0") == "59.3"
    assert cell_contents(doc, "T9:r0:c0") == ""

    from .schema import Finding, QuantFinding
    doc2 = PaperDoc(paper_id="p", reported_numbers=[
        QuantFinding(table_ref="T1:r0:c0", delta="+2.1%")])
    f = Finding(evidence_ref="T1:r0:c0")
    assert grounded_claimed_delta(doc2, f) == 0.021
    assert grounded_claimed_delta(doc2, Finding(evidence_ref="p7")) is None

    assert not admissible_if_it_succeeds(Config.load(), ProbeSpec(paper_id="p", provenance="synthesized"))[0]
    assert admissible_if_it_succeeds(Config.load(), ProbeSpec(paper_id="p", provenance="repo_exec"))[0]

    # --- EXACT_CERTIFICATE: no sealed certificate => None, never a placeholder ------------
    import json
    import tempfile

    from .schema import PlanDecision as _PlanDecision

    no_plan = attempt_certificate_check(Config.load(), Path("."), "p", doc, ProbeSpec(paper_id="p"), None)
    assert no_plan is None
    unwarranted = attempt_certificate_check(
        Config.load(), Path("."), "p", doc, ProbeSpec(paper_id="p"),
        _PlanDecision(target_id="T", requires_execution=False))
    assert unwarranted is None, "a plan that does not require execution must never run one"

    with tempfile.TemporaryDirectory() as td:
        cert_cfg = Config(projects_dir=Path(td), allow_certificate_exec=True)
        bound_doc = PaperDoc(paper_id="fw", title="A Frank-Wolfe paper", sections=[
            Section(section_idx=0, title="Setup", text="Let the objective be convex."),
            Section(section_idx=1, title="Convergence",
                   text="Theorem 3.1. Under the above hypotheses, the optimality gap "
                        "satisfies gap(t) <= 2/(t+2) for all t >= 1."),
        ])
        root2 = state.project_dir(cert_cfg, "fw")
        (root2 / "paper").mkdir(parents=True)
        state.write_json(root2 / "paper" / "doc.json", bound_doc.model_dump())

        base = ProbeSpec(paper_id="fw", target_id="TGT-CLM-1", claim="Theorem 3.1",
                         claim_ref="S1", claim_kind="section_span")
        plan_ = _PlanDecision(target_id="TGT-CLM-1", action="EXACT_CERTIFICATE",
                              route="EXACT_CERTIFICATE", requires_execution=True)

        assert attempt_certificate_check(cert_cfg, root2, "fw", bound_doc, base, plan_) is None, (
            "no sealed certificate yet => None, target stays NOT_ATTEMPTED"
        )

        script = ("print('SH_METRIC arm=certificate seed=0 value=1')\n"
                 "print('SH_AUX key=lhs arm=certificate seed=0 value=2.0')\n")
        raw = json.dumps({
            "script": script, "instance_count": 1,
            "bindings": [
                {"kind": "hypotheses", "impl_ref": "line 1",
                 "impl_quote": "print('SH_METRIC arm=certificate seed=0 value=1')"},
                {"kind": "claimed_bound", "impl_ref": "line 1",
                 "impl_quote": "print('SH_METRIC arm=certificate seed=0 value=1')"},
                {"kind": "instance", "impl_ref": "line 2",
                 "impl_quote": "print('SH_AUX key=lhs arm=certificate seed=0 value=2.0')"},
            ],
            "paper_quotes": {"hypotheses": "Let the objective be convex.",
                            "claimed_bound": "gap(t) <= 2/(t+2) for all t >= 1"},
            "notes": "",
        })
        verdict = json.dumps({"approved": True,
                              "approved_kinds": ["hypotheses", "claimed_bound", "instance"],
                              "notes": "checked"})
        sealed = certificate.accept(cert_cfg, "fw", "TGT-CLM-1", raw, verdict,
                                    generated_by="gen", reviewer="ver")
        assert sealed["established"] is True

        result = attempt_certificate_check(cert_cfg, root2, "fw", bound_doc, base, plan_)
        # Once sealed, the wiring must reach `execute.authorize`'s cert_exec rung ladder,
        # proven by the provenance tag and a non-None result.
        assert result is not None and result.provenance == "cert_exec"
        assert result.reconciliation is not None
        assert result.reconciliation.status in ("INCONCLUSIVE", "COUNTEREXAMPLE_FOUND"), (
            result.reconciliation.status)

        # `outcome_for` / `_DISPOSITION_FOR_RECONCILIATION`: the reconciliation-status ->
        # disposition mapping this route adds, tested directly and deterministically.
        from .schema import Reconciliation as _Reconciliation
        won = ProbeResult(paper_id="fw", provenance="cert_exec",
                          reconciliation=_Reconciliation(status="COUNTEREXAMPLE_FOUND",
                                                         provenance="cert_exec"))
        clean = ProbeResult(paper_id="fw", provenance="cert_exec",
                            reconciliation=_Reconciliation(status="NO_VIOLATION_FOUND",
                                                           provenance="cert_exec"))
        out = outcome_for("TGT-CLM-1", won, plan_.action, plan_.route)
        assert out.disposition == "COUNTEREXAMPLE_ESTABLISHED", out.disposition
        assert out.establishes_failure, "an admissible cert_exec counterexample must establish"
        clean_out = outcome_for("TGT-CLM-1", clean, plan_.action, plan_.route)
        assert clean_out.disposition == "NO_COUNTEREXAMPLE_FOUND", clean_out.disposition
        assert not clean_out.establishes_failure, "checked-clean must never establish a failure"

        # certificate_targets: nothing to list once this target's own sealed certificate
        # exists.
        assert certificate_targets(cert_cfg, "fw") == []

    # --- _scoped_paper_text: relevance-scoped, never the whole paper, and covers every
    # cited section plus the abstract, with an omission note for the rest --------------
    from .schema import Table as _Table

    scope_doc = PaperDoc(paper_id="scope", sections=[
        Section(section_idx=0, title="Abstract", text="We propose a method."),
        Section(section_idx=1, title="Method", text="The method does X."),
        Section(section_idx=2, title="Appendix A: Generator",
               text="The synthetic generator uses alpha=0.3, as used in Table 8."),
        Section(section_idx=3, title="Unrelated", text="Nothing relevant here."),
    ], tables=[_Table(table_idx=8, page=5, label="8", caption="Results")])
    scoped = _scoped_paper_text(scope_doc, table_ref="T8:r0:c0", claim_ref="s1",
                                ingredient_refs=("s1", "T8"))
    assert "alpha=0.3" in scoped, "the section mentioning the table's own label must be kept"
    assert "does X" in scoped, "the claim's own section must be kept"
    assert "We propose a method" in scoped, "the abstract must always be kept"
    assert "Nothing relevant here" in scoped, "the rest of the paper fills the budget"
    tiny = _scoped_paper_text(scope_doc, table_ref="T8:r0:c0", claim_ref="s1",
                              ingredient_refs=("s1",), cap=10)
    assert "sections omitted" in tiny, "a tight cap still omits explicitly, never silently"
    # Under a cap, an unnamed setup section outranks unrelated prose, and a bibliography
    # never fills the budget.
    setup_doc = PaperDoc(paper_id="setup", sections=[
        Section(section_idx=0, title="Abstract", text="We propose a method."),
        Section(section_idx=1, title="Discussion", text="Broad remarks on impact. " * 8),
        Section(section_idx=2, title="Setup",
               text="Trained with learning rate 0.001, batch size 64, 12 layers, 3 seeds."),
        Section(section_idx=3, title="References", text="[1] A. Author. Title."),
    ])
    capped = _scoped_paper_text(setup_doc, claim_ref="s0", cap=160)
    assert "learning rate 0.001" in capped and "Broad remarks" not in capped
    assert "A. Author" not in capped and "sections omitted" in capped

    print("harness.routes self-check ok")


if __name__ == "__main__":
    _self_check()
