"""Run what the review decided to run, and record how each target ended.

Consolidates `stages/probe.py` (1,744 lines) — the S3 orchestration that decides the spec
for each target, acquires and audits the repository, plans execution (author code or a
governed reconstruction), dispatches to `execute.py`'s runner, and folds every target's
`TargetOutcome` back onto the `TargetSet` — into this file. Ported close to verbatim
rather than forced into a clean "one route, one implementation" shape: the primary-target
and secondary-target flows share roughly a dozen interacting special cases (direct
reconstruction vs. a fallback from author code, comparable vs. not, admissible vs. not,
the dynamic early stop, the artifact route that never competes with execution budget), and
a from-scratch protocol abstraction over that risks losing correctness for cosmetic
uniformity — the same argument the plan itself makes against tabularizing `reconcile()`.

What decides the TARGETS is `discover.py`, before this module is reached: it discovers
what is addressable, orders it, and decides which of it justifies an execution. This
module pursues that list, in that order, up to `cfg.max_targets`, and writes one
`TargetOutcome` per target — every target keeps INDEPENDENT state.

`python -m harness.routes` runs the self-check.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import decide, execute, locate
from . import provenance as provenance_mod
from . import state
from .config import Config
from .schema import (
    CodeAudit, DiscoveredObject, Finding, PaperDoc, PlanDecision, ProbeResult, ProbeSpec,
    ReimplementationReadiness, RepoAcquisition, TargetOutcome,
)

# These remain their OWN files, unchanged — pure leaf modules the fork's own reconnaissance
# confirmed have no cross-file duplication with each other or with execute.py's core:
# experiment_id.py (identity resolution), resources.py (requirement extraction), repo.py
# (acquisition/build_env/capability/synthesize_standalone — E2's verify_commit/head_commit
# are the only pieces `execute.py` needed directly and are ported there), code_audit.py,
# probe_synth.py, reimplement_driver.py (governed-reconstruction generation, distinct from
# `discover.reimplementation_readiness`'s pure eligibility check), and the artifact-
# inspection route (artifact_evidence.py + stages/artifact.py). Keeping them separate is
# the same "reuse when genuinely simpler than rewriting" argument `container.py`/
# `isolation.py` already follow from `execute.py` — these six are lean and single-purpose,
# and inlining them would add risk without reducing duplication that does not exist.
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
    channel uses: "harness" marks `build_spec`'s own generated output (never sidecar-
    sealed, kept only for symmetry) and "driver_accept" is the one token `accept_spec`
    ever stamps, regardless of which delegation mode produced the proposal."""
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
    """Validate and seal a hand-authored `spec.json` proposal — the "driver" provenance
    path. Every field NOT in `_SPEC_PROPOSAL_ALLOWED_FIELDS` is stripped before anything
    else, so `provenance`/`command`/every `.established` block can never be supplied by a
    hand file, sealed or not. A `command` in the raw proposal refuses outright: `driver`
    provenance is a hand-written script, never the paper's own repository entrypoint,
    which only `plan_execution`'s own real-audit-gated promotion may ever attribute."""
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
        spec.claim = getattr(target, "claim_text", "") or ref_obj.quote
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
    `reimpl_exec` — the two are deliberately unmerged."""
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
    ]
    for s in doc.sections:
        lines += [f"### {s.title or f'section {s.section_idx}'}  [s{s.section_idx}]", "",
                 " ".join(s.text.split()), ""]
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

    plan = probe_synth.plan(doc, claim=spec.claim, acq=acq)
    if not plan.keeps_finding:
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
    """Point the spec at the repository's own entrypoint, if every gate allows it. Four
    conditions, all required: the execution gate open, a checkout, an entrypoint, and this
    machine CAPABLE of a fair run. Assessment runs even with the gate shut (only PROMOTION
    is gated), so the report can say precisely what would have been required."""
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
    spec.capability = backend.capability(acq, acq.env_path, cfg.python,
                                         flag="" if is_count else "seed")
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
    seed_flag = spec.experiment.command.seed_flag if spec.experiment.command else ""
    if seed_flag:
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
        attempts=1)


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
    pursued, deferred, non_material_pursued = [], [], 0
    for obj, plan in pairs:
        basis = getattr(obj, "materiality_basis", "") or "NONE"
        if (decide.is_material(basis)
                or bool(obj.question_id and obj.question_id in material_questions)):
            pursued.append((obj, plan))
        elif non_material_pursued < cap:
            pursued.append((obj, plan))
            non_material_pursued += 1
        else:
            deferred.append((obj, plan))
    return ts, pursued, deferred


def admissible_if_it_succeeds(cfg: Config, spec: ProbeSpec) -> tuple[bool, str]:
    """May a process be started for this spec at all? A process is started only for a
    spec whose result would be ADMISSIBLE if it succeeded — running an inadmissible
    provenance spends compute on a number refused before the first process existed."""
    if provenance_mod.admits(spec.provenance):
        return True, ""
    if cfg.diagnostic_mode:
        return True, "diagnostic mode"
    return False, (
        f"the only program available for this target was {spec.provenance or 'unset'}, "
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


def attempt_reimplementation_fallback(
        cfg: Config, root: Path, pid: str, doc: PaperDoc, base_spec: ProbeSpec,
        fallback_plan: PlanDecision | None, out_dir: Path | None = None,
        acq: RepoAcquisition | None = None) -> ProbeResult | None:
    """Execute the governed-reconstruction fallback. Returns None — never a placeholder —
    whenever no CONFORMANT sealed reconstruction exists to run, so every caller's
    refusal/deferral path is exactly what it falls back to. The eventual run still goes
    through the ordinary `execute.authorize` path (`reimpl_exec` branch)."""
    from . import discover as discover_stage

    if fallback_plan is None or not fallback_plan.requires_execution:
        return None
    readiness = discover_stage.reimplementation_readiness(doc)
    if not readiness.established:
        return None
    target_id = base_spec.target_id or "default"
    sealed = reimplement_driver.load_accepted(cfg, pid, target_id)
    if sealed is None:
        ok, _why = reimplement_driver.available(cfg)
        if not ok:
            return None
        brief = reimplement_driver.build_brief(
            readiness, paper_title=doc.title, claim=base_spec.claim,
            table_ref=base_spec.table_ref, claimed_cell_value=base_spec.claimed_cell_value,
            paper_text=_full_paper_text(doc))
        sealed = reimplement_driver.run(cfg, brief, readiness, pid=pid, target_id=target_id)
    if sealed is None:
        return None
    script, conf = sealed
    fspec = ProbeSpec(
        paper_id=pid, target_id=target_id, finding_id=base_spec.finding_id,
        claim=base_spec.claim, claim_ref=base_spec.claim_ref, claim_kind=base_spec.claim_kind,
        table_ref=base_spec.table_ref, claimed_cell_value=base_spec.claimed_cell_value,
        metric=base_spec.metric or "accuracy", seeds=list(base_spec.seeds) or [0, 1, 2],
        arms=["reproduction"], script=script, provenance="reimpl_exec",
        interpreter=(acq.env_path if acq is not None else "") or "",
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
    fallback_plan = replan_after_author_code_exhausted(
        pairs[0][0], original_plan, spec) if pairs else None
    if fallback_plan is not None and target_set is not None:
        target_set.plans.append(fallback_plan)
    reconstruction_plan = original_plan if direct_reconstruction else fallback_plan
    reconstruction_result = None
    if comparable and (direct_reconstruction or not may_run or fallback_plan is not None):
        reconstruction_result = attempt_reimplementation_fallback(
            cfg, root, pid, doc, spec, reconstruction_plan, acq=acq)
    if direct_reconstruction and reconstruction_result is not None:
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
        if reconstruction_result is not None and reconstruction_plan is not None:
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
                reason=(f"an experiment is warranted for this target and this run's budget "
                       f"of {cfg.max_targets} target(s) was already spent on "
                       f"higher-priority ones. A limit of this run, not of the paper.")))
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
    """Re-attach already-produced `TargetOutcome`s onto a freshly discovered target set —
    the recovery mechanism for the incident where a cached second pass lost every prior
    execution's outcome from the funnel. Spends no execution; repairs bookkeeping only."""
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
    if primary_outcome_path.exists():
        outcomes.append(TargetOutcome(**state.read_json(primary_outcome_path)))
    else:
        outcomes.append(outcome_for(primary_obj.target_id, primary_result,
                                    primary_plan.action, primary_plan.route))

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

    print("harness.routes self-check ok")


if __name__ == "__main__":
    _self_check()
