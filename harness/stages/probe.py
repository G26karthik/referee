"""S3 — run what the review decided to run, and record how each target ended.

**What decides the targets.** `harness.stages.discover` does, before this stage is
reached: it discovers what is addressable, `harness.priority` orders it, and
`harness.planner` decides which of it justifies an execution. This stage pursues that
list, in that order, up to `cfg.max_targets`, and writes one `TargetOutcome` per target.
Every target keeps INDEPENDENT state, so a paper whose first target is blocked by hardware
and whose second reproduces reports both.

What this module used to do instead was one line — take the highest-ranked finding a lens
had marked `verifiable_by_experiment`, and stop. One target, chosen by an unchecked model
boolean, ordered by a report display sort, with no fallback when it blocked. That line
survives as `finding_target` in `build_spec`, reached only when no target set exists on
disk.

If nothing justifies an execution the stage still runs: a probe with no intervention
measures this machine's seed-noise floor, which is the number every "is this gain real?"
question divides by — worth having even with nothing to test.

A hand-written, SEALED `control/spec.json` overrides all of this, which is how the
driver supplies a faithful reproduction of the paper's actual setup. Sealed, not merely
present: `accept_spec` validates and hash-seals the proposal (never trusting `command`,
`provenance`, or any identity/capability/resources/commit field from the raw file), and
`build_spec` trusts a `control/spec.json` only when `spec_is_accepted` confirms that seal
— a file dropped at that path by any other means is ignored outright, the same discipline
`stages.audit.accept_lens`/`lens_is_accepted` apply to a lens file.

**Where `claimed_delta` may come from.** Only two places, both traceable:

  1. The driver writes it into `spec.json` alongside a real reproduction script, and
     seals it with `run.py accept`.
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

import hashlib
import json
import re
from pathlib import Path

from . import artifact as artifact_stage
from . import literature as literature_stage
from . import validation as validation_stage
from .. import (backends, claimgraph, claims, code_audit, comparison as comparison_mod,
                delegation, exhaustion, experiment_id, materiality, planner, probe_synth,
                provenance as provenance_mod, reimplement, reimplement_driver,
                repo as repo_mod, resources as resources_mod, state)
from ..artifacts import (CodeAudit, DiscoveredObject, Finding, FocusedValidation,
                         PaperDoc, PlanDecision, ProbeResult, ProbeSpec,
                         ReimplementationReadiness, RepoAcquisition, TargetOutcome)
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


def grounded_quantity(target) -> tuple[float | None, str]:
    """(value, verbatim) the discovered target's own address reports.

    The general replacement for `grounded_claimed_delta`'s cell-only chain. The quantity
    was parsed by `harness.claims.parse_quantity` at discovery time, from the text at an
    address the harness re-derived — so a prose-stated total reaches the reconciler by
    exactly the same route a cell does, and by no weaker one. `parse_quantity` refuses
    every span that reports more than one number, which is what keeps this from becoming
    the prose-shaped version of a positional guess.
    """
    ref = getattr(target, "ref", None)
    q = getattr(ref, "quantity", None) if ref else None
    if q is None or q.value is None:
        return None, ""
    return q.value, (q.raw or "")


# Fields a hand-authored, unsealed spec proposal may ever supply. Everything else —
# every `.established` identity/capability/resources block, `commit`, `commit_state`,
# and `provenance` itself — is stripped before the proposal is even considered, and is
# recomputed or reassigned by this harness alone. This is the allowlist that makes
# forging trusted provenance by writing a plausible-looking JSON file impossible: no
# field this list omits can ever reach `backends.authorize` from a hand file, sealed or
# not.
_SPEC_PROPOSAL_ALLOWED_FIELDS = frozenset({
    "paper_id", "finding_id", "claim", "claimed_delta", "metric", "arms", "seeds",
    "dataset", "epochs", "script", "table_ref", "claim_ref", "claim_kind",
    "claimed_cell_value", "target_id", "mechanism", "rationale", "aux_metrics",
})

# `delegation.WRITTEN_BY.values()` names every token a real delegation mode can seal
# with, exactly as `stages.audit._ACCEPTED_WRITERS` builds its own allowlist. Neither
# "harness" nor "driver_accept" is a delegation mode: "harness" marks `build_spec`'s own
# generated output (never sidecar-sealed, so it never reaches this check at all — kept
# here only for symmetry with the vocabulary `written_by` draws from) and
# "driver_accept" is the ONE token `accept_spec` ever stamps into `spec.driver.json`,
# regardless of which delegation `mode` produced the proposal — the same role
# `stages.audit.COMPOSED_WRITER` plays for a composed lens file: a fixed, extra token
# alongside the delegation vocabulary, not a member of it.
_SPEC_ACCEPTED_WRITERS = tuple(delegation.WRITTEN_BY.values()) + ("harness", "driver_accept")


def _strip_to_allowed_fields(data: dict) -> dict:
    return {k: v for k, v in (data or {}).items() if k in _SPEC_PROPOSAL_ALLOWED_FIELDS}


def spec_is_accepted(root: Path) -> tuple[bool, str]:
    """Does `control/spec.json` carry a valid seal — the same discipline
    `stages.audit.lens_is_accepted` applies to lens files, on this channel.

    A spec with no sidecar, a sidecar whose `written_by` is not a real accept-path
    token, or a sidecar whose recorded hash does not match the file's current bytes is
    NOT accepted — refused outright, never partially trusted.
    """
    control = state.control_dir(root)
    path = control / "spec.json"
    sidecar = control / "spec.driver.json"
    if not path.exists() or not sidecar.exists():
        return False, "no sealed spec.json (or no sidecar) for this case"
    try:
        rec = state.read_json(sidecar)
    except (OSError, ValueError):
        return False, "spec.driver.json is not valid JSON"
    if not isinstance(rec, dict) or rec.get("written_by") not in _SPEC_ACCEPTED_WRITERS:
        return False, (f"provenance sidecar written_by="
                       f"{rec.get('written_by') if isinstance(rec, dict) else None!r} not recognized")
    want = rec.get("content_sha256")
    if not want:
        return False, "provenance sidecar has no content_sha256"
    if hashlib.sha256(path.read_bytes()).hexdigest() != want:
        return False, "spec.json content changed after its provenance sidecar was written"
    return True, ""


def accept_spec(cfg: Config, pid: str, raw: str, *, reviewer: str = "",
                tool_policy: str = "", mode: str = "MANUAL") -> dict:
    """Validate and seal a hand-authored `spec.json` proposal — the "driver" provenance
    path — through the same explicit accept discipline `stages.audit.accept_lens`
    already applies to lens files.

    Every field NOT in `_SPEC_PROPOSAL_ALLOWED_FIELDS` is stripped before this function
    does anything else with the proposal: `provenance`, `command`, every `.established`
    identity/capability/resources block, `commit`, and `commit_state` can never be
    supplied by the proposal, whatever it claims — they are always assigned here or, for
    `repo_exec`, exclusively by `plan_execution`'s own real-audit-gated promotion.

    A `command` in the raw proposal REFUSES outright: `driver` provenance is a hand
    -written script (a faithful reproduction this harness could not derive on its own),
    never the paper's own repository entrypoint, which may only ever be attributed by
    `plan_execution` after a real clone, a real static audit, and a real identity match.
    """
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
    proposal["provenance"] = "driver"
    spec = ProbeSpec(**proposal)

    root = state.project_dir(cfg, pid)
    control = state.control_dir(root)
    out = control / "spec.json"
    state.write_json(out, spec.model_dump())
    content_sha256 = hashlib.sha256(out.read_bytes()).hexdigest()
    record = {
        "paper_id": pid,
        **delegation.provenance_record(mode=mode, reviewer=reviewer, tool_policy=tool_policy),
        "content_sha256": content_sha256,
        "ts": state.now(),
    }
    # `written_by` in the sidecar is ALWAYS this function's own fixed token, never the
    # raw proposal's and never merely whatever `delegation.provenance_record` derived
    # from `mode` — a driver spec is sealed as "driver_accept" however it was produced,
    # so `spec_is_accepted` has exactly one token to recognize on this channel rather
    # than the whole delegation vocabulary, mirroring `stages.audit.COMPOSED_WRITER`.
    record["written_by"] = "driver_accept"
    state.write_json(control / "spec.driver.json", record)
    return {"accepted": True, "paper_id": pid, "content_sha256": content_sha256}


def build_spec(cfg: Config, pid: str, doc: PaperDoc, target=None) -> ProbeSpec:
    """The spec to run, with `claimed_delta` allowed only where it is grounded.

    Two invariants, applied to the hand-written and generated paths alike, so a stale
    `spec.json` left by the old scraper cannot resurrect a phantom delta on reload:

      - `script == ""`  =>  no claim under test. This is a noise-floor calibration.
      - otherwise       =>  keep an explicit driver value, else ground it in a cell.
    """
    root = state.project_dir(cfg, pid)

    reports, _, _ = audit_stage.load_reports(cfg, pid, doc)
    # THE FALLBACK, not the selector. This line used to BE the selector — one target, from
    # an unchecked lens boolean, ordered by a report display sort — and it is kept only for
    # a project with no target set on disk (one reviewed before the discovery phase
    # existed, or a direct call to this function). When `target` is supplied, discovery,
    # prioritisation and the trigger gate have already chosen, and their choice stands.
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

    # A SEALED driver spec is the only hand-authored source ever trusted here.
    # `spec_is_accepted` requires a content-hash-verified sidecar written by
    # `accept_spec` itself — file existence and a `written_by != "harness"` claim are NOT
    # enough, which is the exact gap that let a forged `control/spec.json` (or, before
    # Task 3, a forged `runs/<pid>/spec.json` reachable from inside a container) claim
    # `provenance: "repo_exec"` with fabricated `established=true` identity/capability/
    # resources blocks and reach `backends.authorize` with those claims intact — only the
    # commit was freshly re-verified there; everything else was trusted from the file.
    control = state.control_dir(root)
    accepted, _why = spec_is_accepted(root)
    if accepted:
        # Only the fields `accept_spec` could ever have sealed are trusted here — the
        # same `_strip_to_allowed_fields` allowlist it applies, applied again, so a
        # `control/spec.json` a container process managed to overwrite in place (see
        # `test_container_writing_into_the_mount_cannot_forge_a_seal`) still cannot widen
        # what it grants beyond the allowlist even if it also forged a matching hash.
        # `provenance` is always "driver" (accept_spec never seals anything else) and
        # every identity/capability/resources/commit field starts unestablished here,
        # exactly as a freshly-built spec's would, so `plan_execution`'s own real
        # assessment is what fills them in, never this file.
        sealed = state.read_json(control / "spec.json")
        proposal = _strip_to_allowed_fields(sealed)
        proposal["paper_id"] = pid
        spec = ProbeSpec(**proposal)
        spec.provenance = "driver"
        spec.written_by = "driver_accept"
    else:
        # No sealed proposal for this case (nothing staged, or staged but not yet
        # `accept_spec`'d, or a `control/spec.json` that failed its own hash/sidecar
        # check — see `spec_is_accepted`). The harness's own auto-built spec is the only
        # other source; a hand file that skipped the explicit accept step is never
        # partially trusted.
        #
        # `written_by="harness"` here, not left blank: this is what makes the file this
        # function is about to be re-read as (via `run()`'s persistence below) identify
        # itself correctly on the NEXT call, so a fresh audit target keeps being honored
        # run after run instead of freezing on whichever finding was verifiable first.
        spec = ProbeSpec(
            paper_id=pid, written_by="harness",
            finding_id=(target_finding_id or
                        (finding_target.finding_id if finding_target and target is None else "")),
            claim=((getattr(target, "claim_text", "") or
                    getattr(getattr(target, "ref", None), "quote", ""))
                   if target is not None else
                   ((finding_target.target or finding_target.statement)
                    if finding_target else "")),
            seeds=list(range(max(3, min(cfg.seeds, 5)))),
        )

    # Keyed by (lens, finding_id), not by finding_id alone. Ids are lens-supplied, and
    # four lenses independently numbering their findings `overclaim-01`, `protocol-01`
    # and so on collide readily; a bare-id map silently kept whichever lens was loaded
    # last, so the probe could anchor to a different finding than the one it targeted.
    by_id = {(f.lens, f.finding_id): f for r in reports for f in r.findings}
    anchor = (next((f for (_, fid), f in by_id.items() if fid == spec.finding_id), None)
              or finding_target)

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

    # A DISCOVERED target overrides the finding-derived address, because it carries one
    # the harness minted and re-derived rather than one a lens wrote. This is the whole of
    # the prose path: `ClaimRef.quote` is the verbatim span, `ClaimRef.quantity` the number
    # `parse_quantity` refused to guess at, and neither had any way to reach a ProbeSpec
    # before. A cell target still lands in `table_ref` as well, so nothing keyed on a cell
    # address loses it.
    if target is not None and getattr(target, "ref", None) is not None:
        ref_obj = target.ref
        spec.target_id = getattr(target, "target_id", "")
        spec.claim_ref = ref_obj.ref
        spec.claim_kind = ref_obj.kind
        # The selected target is authoritative.  Keeping the first globally ranked
        # finding's claim/value here contaminated later targets (APT T1 carried T2's
        # 46.8 value and contradiction id), making alignment evidence about the wrong
        # experiment.
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
    on disk a person can pick up, or the manual path is not first-class. This is the
    MANUAL channel — an operator who wants to review a reconstruction themselves before
    sealing it stages a `script` (never a `command`, which only `plan_execution`'s own
    real-audit-gated promotion may ever set) at `control/.staged/spec.json` and seals it
    with `run.py accept`, which validates it and hash-seals it under `driver` provenance
    through `accept_spec` — never trusted unconditionally, unlike before.

    Step 8 added an AUTOMATED channel alongside this one, not in place of it:
    `harness.reimplement_driver` delegates the writing, and machine-VERIFIES every
    required ingredient's binding before sealing it as `reimpl_exec` — the provenance
    `local_exec.reconcile` and `backends.authorize` both refuse to let settle anything
    without `ReimplementationConformance.established`. The two channels are deliberately
    unmerged: a human reviewing their own reconstruction before sealing it is a different,
    and adequate, discipline from a model's unreviewed output, which is why only the
    second needs the extra machine check.

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
        "A `spec.json` naming your `script` (never `command` — that field is reserved for ",
        "this harness's own repository-entrypoint promotion and a staged proposal setting it ",
        "is refused outright), plus `metric`, `seeds`, and the identity fields, plus the ",
        "implementation itself. Print one `METRIC <name> <value>` line per seed on stdout — ",
        "the same contract every probe in this harness uses.", "",
        f"Stage it at `control/.staged/spec.json` under this case's project directory, then seal it with:",
        "",
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
        # `claim_ref`/`claim_quote` carry a PROSE target into the identity layer. Without
        # them a prose-addressed spec arrived with `table_ref=""` and every link answered
        # "no cell address", so the one shape this pipeline was extended to reach would
        # have been blocked at the last gate before execution.
        claim_quote = ""
        if spec.claim_ref and spec.claim_kind == "prose_claim":
            resolved = claims.resolve(doc, spec.claim_ref)
            claim_quote = resolved.quote if resolved.resolved else ""
        spec.experiment, spec.metric_identity, spec.configuration = experiment_id.resolve(
            doc, Path(acq.path), spec.table_ref, spec.finding_id, harness_seeds=len(spec.seeds),
            claim_ref=spec.claim_ref, claim_quote=claim_quote)
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
    #
    # The seed flag is dropped for a target whose quantity is a COUNT. Such a target is a
    # count of what the repository's own generator produces: it has no seed-to-seed
    # distribution, this harness passes no `--seed` for it, and demanding that the
    # repository accept one would refuse the run over an argument nobody sends. The two
    # runs still happen — running the generator twice and getting the same count is the
    # evidence that the quantity is deterministic, which is what `local_exec.reconcile`
    # requires before it will compare a count against a printed precision.
    is_count = bool(spec.metric_identity and spec.metric_identity.established
                    and spec.metric_identity.cell_quantity == "count")
    spec.capability = backend.capability(acq, acq.env_path, cfg.python,
                                         flag="" if is_count else "seed")
    # Verified against the tree the BACKEND will run, not against a same-named directory
    # on this disk. Under the local backend those are the same thing and this is exactly
    # what it always did; under a remote one, certifying the local checkout and then
    # running elsewhere would leave the executed tree unverified while the record said
    # `verified` — see `ExecutionBackend.commit_tree`.
    spec.commit_state = repo_mod.verify_commit(
        acq.path, spec.commit, tree=backend.commit_tree(acq.path)).state
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


# How an execution ended, in the target vocabulary. Keyed on the RECONCILIATION's own
# status and failure class, both of which are written by the deterministic layer — so a
# disposition is a re-labelling of an existing decision and never a second one.
_DISPOSITION_FOR_RECONCILIATION = {
    "RESOLVED_VERIFIED": "REPRODUCED",
    "FAILED_REPRODUCTION": "FAILED_REPRODUCTION",
    "NOT_ATTEMPTED": "NOT_ATTEMPTED",
}
# INCONCLUSIVE splits by WHY, because "the hardware is not here", "the gate refused" and
# "it ran and settled nothing" are three different facts and only the last one is about
# the experiment. None of the three is about the paper — invariants 4 to 7.
_DISPOSITION_FOR_FAILURE_CLASS = {
    "resources_insufficient": "RESOURCE_BLOCKED",
    "execution_unauthorized": "AUTHORIZATION_BLOCKED",
    "dependency_missing": "ENVIRONMENT_BLOCKED",
    "environment_failure": "ENVIRONMENT_BLOCKED",
    "platform_incompatible": "ENVIRONMENT_BLOCKED",
    "setup_failure": "ENVIRONMENT_BLOCKED",
    "infrastructure_failure": "ENVIRONMENT_BLOCKED",
    "experiment_unidentified": "IDENTITY_BLOCKED",
    "metric_unbound": "IDENTITY_BLOCKED",
    "configuration_unmatched": "IDENTITY_BLOCKED",
    # The right program, and nothing to hold its output against. Kept apart from the three
    # identity classes above because those blame the artifact and this one does not.
    "comparison_unestablished": "COMPARISON_BLOCKED",
    "commit_mismatch": "ARTIFACT_BLOCKED",
    "backend_unavailable": "ENVIRONMENT_BLOCKED",
    "credentials_unavailable": "ENVIRONMENT_BLOCKED",
}


def outcome_for(target_id: str, result: ProbeResult, action: str, route: str) -> TargetOutcome:
    """One target's terminal state, read off the ProbeResult the runner already wrote.

    Decides nothing. `disposition` comes from the reconciliation's own status, and
    `provenance` from the spec that ran, so the provenance ceiling reaches
    `TargetOutcome.establishes_failure` intact.
    """
    rec = result.reconciliation
    status = rec.status if rec else "NOT_ATTEMPTED"
    disposition = _DISPOSITION_FOR_RECONCILIATION.get(status, "")
    if not disposition:
        disposition = _DISPOSITION_FOR_FAILURE_CLASS.get(
            (rec.failure_class if rec else "") or "", "INCONCLUSIVE")
    # WHAT IDENTITY FOUND, read off the spec the runner actually ran rather than off the
    # reconciliation. The reconciliation's copy is now populated before the provenance
    # ceiling returns, but the spec is the primary record and it survives even when no
    # reconciliation was produced at all (a refused run writes none).
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
        # How many processes this target actually STARTED, read off the runner's own
        # count rather than inferred from the disposition. The funnel's "launched" term
        # has to come from the execution record: a target can be AUTHORIZATION_BLOCKED
        # having started nothing, and a target can be INCONCLUSIVE having started three
        # processes, and only this number tells those apart.
        launched=result.executions,
        execution_ref=result.script_path or "", attempts=1)


def resync_cached_outcomes(cfg: Config, pid: str) -> dict:
    """Re-attach already-produced `TargetOutcome`s onto a freshly discovered target set.

    `discover` recomputes objects and plans from scratch on every `review` invocation —
    reasonably, since a new grade or a new lens finding can change what is checkable — but
    it runs BEFORE this stage and writes nothing about execution. When the controller
    finds a cached `probe_results.json` and skips `run()` entirely (the whole point of the
    cache: no repeat execution), the `discovery/targets.json` THIS invocation's `discover`
    pass just wrote never receives that target's outcome, so every count that reads
    `target_set.outcomes` (`CaseLedger.efficiency`, `evaluate`'s funnel) reports it as never
    launched — despite `control/probe_results.json` still holding a valid record.

    This repeats the bookkeeping `_review` does after a fresh run, reading every result
    from disk instead of running anything. It spends no execution and changes no
    experimental record; it only repairs `TargetOutcome`s to agree with the cached
    `ProbeResult`s that already exist.
    """
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
            # A current plan must have a current outcome.  Legacy runs did not persist
            # blocked secondary outcomes, so cached discovery used to silently lose them
            # and violate the warranted-target conservation identity.  Do not invent the
            # old blocker; record the bookkeeping boundary explicitly.
            outcomes.append(TargetOutcome(
                target_id=obj.target_id, disposition="NOT_ATTEMPTED",
                action=plan.action, route=plan.route, launched=0,
                reason=("a cached primary result exists, but this target has no durable "
                        "result or outcome record. This is a legacy harness persistence "
                        "boundary, not evidence about the paper.")))

    for obj, plan in deferred:
        outcomes.append(TargetOutcome(
            target_id=obj.target_id, disposition="BUDGET_DEFERRED",
            action=plan.action, route=plan.route, launched=0,
            reason=(f"an experiment is warranted for this target and this run's budget "
                    f"of {cfg.max_targets} target(s) was already spent on "
                    f"higher-priority ones. A limit of this run, not of the paper.")))

    # Discovery is recomputed before cached probe resync, so the runtime Step-6
    # AUTHOR_CODE_EXECUTION -> INDEPENDENT_RECONSTRUCTION re-plan is not present in the
    # new TargetSet. Recreate it only from durable identity evidence and a durable
    # reconstruction outcome; otherwise cached refreshes turn an exhausted author-code
    # route back into NOT_TRIED and attach the reconstruction result to the wrong route.
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
    exhaustion.refresh(target_set, cfg)
    state.write_json(discover_stage.targets_path_in(root), target_set.model_dump())
    return {"resynced": len(outcomes), "pursued": len(pairs), "deferred": len(deferred)}


def _executable_targets(cfg: Config, pid: str):
    """(target, plan) pairs the planner authorised for execution, in priority order.

    Non-material targets are capped by `cfg.max_targets`. The cap is a budget applied to
    an ALREADY ordered list, so the targets it drops are the least useful ones rather than
    whichever came last. A target whose stored materiality basis is recognised by
    `materiality.is_material` bypasses that numeric cap: silently deferring the paper's
    material question because three less consequential targets happened to rank ahead of
    it would turn an efficiency setting into a scientific policy.

    Returns (target_set, pursued, deferred). The third is what the budget dropped and it
    is returned rather than discarded: a target the planner judged worth running and this
    run did not reach is a different fact from one the planner refused, and a funnel that
    cannot tell them apart makes the budget look like a decision. `SH_MAX_TARGETS` is
    documented as a budget and not a gate, and this is what makes that checkable.
    """
    from . import discover as discover_stage
    ts = discover_stage.load(cfg, pid)
    if ts is None:
        return None, [], []
    plans = {p.target_id: p for p in ts.plans if p.requires_execution}
    material_questions = {q.question_id for q in ts.questions if q.materiality == "CENTRAL"}
    pairs = [(o, plans[o.target_id]) for o in ts.objects if o.target_id in plans]
    cap = max(1, cfg.max_targets)
    pursued = []
    deferred = []
    non_material_pursued = 0
    for obj, plan in pairs:
        basis = getattr(obj, "materiality_basis", "") or "NONE"
        if (materiality.is_material(basis)
                or bool(obj.question_id and obj.question_id in material_questions)):
            pursued.append((obj, plan))
        elif non_material_pursued < cap:
            pursued.append((obj, plan))
            non_material_pursued += 1
        else:
            deferred.append((obj, plan))
    return ts, pursued, deferred


def run(cfg: Config, pid: str) -> dict:
    """S3 for one paper. Wraps `_review` so a leased machine is always given back.

    The `finally` is not tidiness. A backend that leases remote compute bills from the
    moment it is created until something terminates it, and every other failure path in
    this stage is designed to be survivable — a blocked target ends that target, a fault
    pursuing one target is recorded and the paper continues. Those same guarantees mean an
    exception can leave this function without ever reaching a teardown that was written
    inline, and the cost of that is money accruing on a machine nobody is watching.
    Backends that lease nothing return `False` from `release` and this costs a no-op.
    """
    try:
        return _review(cfg, pid)
    finally:
        try:
            released, detail = backends.select_backend(cfg).release(
                state.project_dir(cfg, pid), pid)
            if released:
                state.append_log(cfg, pid, artifact_type="sandbox_release", phase="probe",
                                 headers={"detail": detail}, path="")
        except Exception:                                # noqa: BLE001
            # A teardown that raises must not replace the review's own outcome — including
            # its own exception, which is the thing the caller needs to see.
            pass


def admissible_if_it_succeeds(cfg: Config, spec: ProbeSpec) -> tuple[bool, str]:
    """May a process be started for this spec at all?

    THE RULE: a process is started only for a spec whose result would be ADMISSIBLE if it
    succeeded. `provenance.admits` already refuses `synthesized` and `template` in both
    directions at the reconciler — so running one is spending compute on a number that was
    inadmissible before the first process existed.

    That is not a hypothetical. Over the eight-paper corpus the probe stage ran 160
    processes across 16 targets, every one of them the same harness-authored diagnostic,
    every one refused afterwards at the ceiling. The refusals were correct; the spend was
    not, and the sixteen plausible-looking deltas it produced are the table the
    admissibility rule then had to catch.

    `SH_DIAGNOSTIC_MODE` re-enables the diagnostic explicitly. When it does, the result is
    written as a `DiagnosticRun` and never as a `TargetOutcome`, so it stays out of the
    funnel and out of every decision function.
    """
    if provenance_mod.admits(spec.provenance):
        return True, ""
    if cfg.diagnostic_mode:
        return True, "diagnostic mode"
    return False, (
        f"the only program available for this target was {spec.provenance or 'unset'}, "
        f"which the provenance ceiling does not admit against a printed quantity in "
        f"either direction. Running it could not have produced evidence about this paper, "
        f"so nothing was started. Set SH_DIAGNOSTIC_MODE=1 to run it as a diagnostic; its "
        f"result is recorded separately and settles nothing.")


def _not_started(obj, plan, why: str,
                 disposition: str = "IDENTITY_BLOCKED") -> TargetOutcome:
    """A target whose only available program could never have spoken. Nothing ran.

    IDENTITY_BLOCKED rather than INCONCLUSIVE, and the difference is the whole point:
    INCONCLUSIVE means "it ran and settled nothing", and claiming that for a run that
    never existed is the same overstatement in miniature that `launched` exists to
    prevent. `launched=0` is a measurement here, not a default.

    `disposition` is a parameter because the two reasons a process is not started are two
    different facts. IDENTITY_BLOCKED says the program that would have run was not bound
    to what was printed; COMPARISON_BLOCKED says it was, and its output would have had
    nothing to be held against. Reporting the second as the first would blame the artifact
    for a limit of this review's arithmetic.
    """
    return TargetOutcome(
        target_id=obj.target_id, disposition=disposition,
        action=plan.action, route=plan.route, launched=0, reason=why)


def _superseded_by_established_failure(obj, plan, stopper) -> TargetOutcome:
    """Record an expensive target that no longer needs to be pursued.

    This is neither a refusal nor an attempt: another target has already established the
    material paper-level failure, so spending on this one cannot change the review's
    decision.  Kept in one helper so the pre-acquisition stop and the between-target stop
    produce exactly the same durable state.
    """
    return TargetOutcome(
        target_id=obj.target_id,
        disposition="SUPERSEDED_BY_ESTABLISHED_FAILURE",
        action=plan.action, route=plan.route, launched=0,
        reason=(f"a material failure was already established on target "
                f"{getattr(stopper, 'target_id', '?')}, so no further expensive "
                f"experiment was started for this paper; this target was not refused "
                f"and was not attempted"))


def _graph_for(doc: PaperDoc, pairs: list) -> object | None:
    """The claim graph, built ONCE and only when a focused validation needs it.

    `claimgraph.build` parses the whole document; a paper with no target on this route
    must not pay for it. Reused rather than rebuilt inside the design layer, so the
    COMPARISON nodes a focused validation acts on are the same ones the claim-link channel
    and `dependency()` read — a second inventory of the paper's own contrasts would be
    free to disagree with the first.
    """
    if not any(plan.action == validation_stage.ACTION for _obj, plan in pairs):
        return None
    try:
        return claimgraph.build(doc)
    except Exception:                             # noqa: BLE001 — a graph is an optimisation
        return None


def establish_comparison(spec: ProbeSpec, route: str, *,
                         settlement_declared: bool = False) -> ProbeSpec:
    """Record what this spec's result would be held against, from the ROUTE it took.

    Derived rather than assumed, which is the whole of the correction: every execution in
    this system ended at `local_exec.reconcile`, and `reconcile` performs exactly one
    comparison — a measured number against a quantity the paper printed. For an
    AUTHOR_CODE_EXECUTION that is the right comparison. For a FOCUSED_VALIDATION_EXPERIMENT
    it is not a comparison at all: an attribution experiment holds one arm against another
    and the paper printed neither, so reconciling either against a cell answers a question
    nobody asked.

    `settlement_declared` is the second half of what a between-arms comparison needs and
    is written by `stages.validation.prepare`, never by this function: two arms with no
    rule declared for them BEFORE the run is a comparison with nothing to settle it
    against, and a rule chosen once both numbers are in settles whatever its author
    wanted. A caller that does not pass it gets the pre-focused-validation behaviour,
    which is what keeps every other route and every fixture unchanged.

    Attached to the spec whether or not it is established, for the same reason capability
    and identity are: "we could not have compared it either" is more useful to a reader
    than leaving the question unanswered.
    """
    spec.comparison = comparison_mod.derive(
        route,
        printed_value_available=bool((spec.claimed_cell_value or "").strip()),
        arms_specified=len(spec.arms or []),
        settlement_declared=bool(settlement_declared))
    return spec


def may_be_compared(spec: ProbeSpec) -> tuple[bool, str]:
    """May a process start, given what its result could be held against?

    The same rule `admissible_if_it_succeeds` applies to provenance, one question earlier:
    a run whose output could not be compared with anything was inadmissible before the
    first process existed, so starting it spends compute on a number that cannot speak.

    A spec with no comparison recorded passes. That is deliberate and it is what keeps
    every hand-written `spec.json` and every fixture behaving exactly as it did — the
    gates already in front of them (provenance, identity, capability, resources, commit)
    are untouched.
    """
    c = spec.comparison
    if comparison_mod.admits_verdict(c):
        return True, ""
    assert c is not None                      # admits_verdict(None) is True
    return False, (
        f"a {c.kind.lower().replace('_', ' ')} comparison would be needed to answer this "
        f"target and this review could not carry one out: {c.reason} Nothing was started, "
        f"because a run whose result has nothing to be held against cannot produce "
        f"evidence about this paper.")


# --------------------------------------------------------------------------- #
# STEP 6 — route fallback: AUTHOR_CODE_EXECUTION identity failure re-plans
# --------------------------------------------------------------------------- #
# A DECIDED failure to bind, not merely "not yet assessed". `unmapped` means identity was
# never reached (no checkout, the gate shut, an earlier precondition failing first) and is
# not grounds for a fallback: the fallback is for a route this review genuinely TRIED and
# could not bind, not one it never got to.
_IDENTITY_FAILURE_STATES = ("ambiguous", "no_candidate", "unsupported")


def identity_failed(spec: ProbeSpec) -> bool:
    """Did identity resolution conclude the checkout could not be bound to the cited
    claim — on any of the three axes: which command, which quantity, which configuration?

    Read from what `plan_execution` already wrote onto the spec (`experiment_id.resolve`'s
    output); this function decides nothing on its own and adds no new judgement.
    """
    for ident in (spec.experiment, spec.metric_identity, spec.configuration):
        if ident is not None and ident.state in _IDENTITY_FAILURE_STATES:
            return True
    return False


def replan_after_author_code_exhausted(
        obj: DiscoveredObject, plan: PlanDecision, spec: ProbeSpec) -> PlanDecision | None:
    """Step 6's re-plan: fall back to INDEPENDENT_RECONSTRUCTION once identity has
    genuinely failed to bind the authors' checkout to the cited claim.

    Discovery cannot decide this — whether AUTHOR_CODE_EXECUTION binds is a fact only a
    real checkout establishes, which is why `discovery._routes` can only OFFER
    INDEPENDENT_RECONSTRUCTION alongside it (when the paper's own specification is
    complete) and never CHOOSE between them. This function is where the choice is made,
    once the fact discovery could not know is in hand.

    Three conditions, all required: the target's ORIGINAL plan chose AUTHOR_CODE_EXECUTION
    (a re-plan of anything else is not this fallback); the object's own routes offer
    INDEPENDENT_RECONSTRUCTION (which — see `discovery._routes` — only happens when the
    paper specifies enough, so this function never invents a route discovery refused);
    and identity genuinely failed rather than being merely unassessed.

    `plan.superseded_by` is set on the ORIGINAL PlanDecision to the fallback's route —
    mutated, not replaced, so the attempt that was actually tried first stays in the
    record. The caller is responsible for appending the returned fallback into
    `TargetSet.plans`; this function only decides and narrates, exactly as
    `harness.planner.plan` does everywhere else.
    """
    if plan.route != "AUTHOR_CODE_EXECUTION":
        return None
    if "INDEPENDENT_RECONSTRUCTION" not in obj.routes:
        return None
    if not identity_failed(spec):
        return None
    fallback = planner.plan(
        obj, artifact_available=True, specification_complete=True,
        investigation_open=True, author_code_exhausted=True, attempt=plan.attempt + 1)
    plan.superseded_by = fallback.route
    return fallback


def _fallback_note(cfg: Config, fallback_plan: PlanDecision | None) -> str:
    """A short, honest suffix for a target outcome's `reason`, or '' when no fallback
    applied. Reached only when `attempt_reimplementation_fallback` returned None — a
    fallback the planner itself refused is reported with the planner's own reason; one it
    approved but that did not actually run names the CURRENT reason why, from the same
    gates `attempt_reimplementation_fallback` and `backends.authorize` read, rather than
    the pre-Step-8 claim that no driver for the route existed at all."""
    if fallback_plan is None:
        return ""
    if fallback_plan.requires_execution:
        ok, why = reimplement_driver.available(cfg)
        if not ok:
            detail = f"no reviewer is configured to write one ({why})"
        elif not cfg.allow_reimplementation_exec:
            detail = "SH_ALLOW_REIMPLEMENTATION_EXEC is not set"
        else:
            detail = ("either it did not produce a conformant reconstruction, or this "
                     "backend's isolation is insufficient to run one — see "
                     "backends.authorize's 'reimpl_exec' branch")
        return (f" A fallback to {fallback_plan.route} (attempt {fallback_plan.attempt}) "
                f"was also considered, because the paper specifies enough to attempt one; "
                f"it did not run: {detail}.")
    return (f" A fallback to {fallback_plan.route} (attempt {fallback_plan.attempt}) was "
           f"considered and refused: {fallback_plan.reason}")


def _direct_reconstruction_note(cfg: Config, plan: PlanDecision | None) -> str:
    """Why a directly planned reconstruction did not run.

    `_fallback_note` deliberately describes a second attempt after authors' code was
    exhausted.  A plan whose *first* route is INDEPENDENT_RECONSTRUCTION is not a
    fallback, and calling it one obscures the more important fact that the planner chose
    this route outright.  Keep the gate diagnosis identical while naming the attempt
    honestly.
    """
    if plan is None:
        return ""
    if plan.requires_execution:
        ok, why = reimplement_driver.available(cfg)
        if not ok:
            detail = f"no reviewer is configured to write one ({why})"
        elif not cfg.allow_reimplementation_exec:
            detail = "SH_ALLOW_REIMPLEMENTATION_EXEC is not set"
        else:
            detail = ("either it did not produce a conformant reconstruction, or this "
                      "backend's isolation is insufficient to run one — see "
                      "backends.authorize's 'reimpl_exec' branch")
        return (f" The planned {plan.route} route (attempt {plan.attempt}) did not run: "
                f"{detail}.")
    return (f" The planned {plan.route} route (attempt {plan.attempt}) was refused: "
            f"{plan.reason}")


def _full_paper_text(doc: PaperDoc) -> str:
    """Every section, joined — the same text `write_reimplementation_prompt` shows a
    human implementer, so the automated and manual PATH B channels work from identical
    evidence."""
    parts = []
    for s in doc.sections:
        parts.append(f"### {s.title or f'section {s.section_idx}'}  [s{s.section_idx}]\n"
                     + " ".join(s.text.split()))
    return "\n\n".join(parts)


def attempt_reimplementation_fallback(
        cfg: Config, root: Path, pid: str, doc: PaperDoc, base_spec: ProbeSpec,
        fallback_plan: PlanDecision | None, out_dir: Path | None = None) -> ProbeResult | None:
    """Execute Step 6's INDEPENDENT_RECONSTRUCTION fallback with Step 8's governed
    reconstruction, when one is available or can be produced. Returns None — never a
    placeholder result — whenever no CONFORMANT sealed reconstruction exists to run, so
    every existing caller's refusal/deferral path is exactly what it falls back to.

    Spends no execution deciding: `reimplement.assess` is pure, `load_accepted` only reads
    disk, and `reimplement_driver.run` degrades to None with its gate shut exactly like
    every other delegation driver in this pipeline (`allow_reimplementation_driver` is off
    by default). The eventual `_run` call still goes through the ordinary authorization
    path — `backends.authorize`'s `reimpl_exec` branch — so a sealed but non-conformant, or
    conformant-but-under-insufficient-isolation, reconstruction is refused there exactly as
    any other inadmissible spec would be; this function only avoids attempting one that
    plainly cannot even be produced.
    """
    if fallback_plan is None or not fallback_plan.requires_execution:
        return None
    readiness = reimplement.assess(doc)
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
        reimplementation_conformance=conf, written_by="harness")
    fspec = establish_comparison(fspec, fallback_plan.route)
    fdir = out_dir or (root / "runs" / pid / "reimplementation")
    fdir.mkdir(parents=True, exist_ok=True)
    state.write_json(fdir / f"{target_id}.spec.json", fspec.model_dump())
    return _run(cfg, root, fspec, out_dir=fdir)


def _review(cfg: Config, pid: str) -> dict:
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))
    state.set_phase(cfg, pid, "probe")

    from . import discover as discover_stage

    target_set, pairs, deferred = _executable_targets(cfg, pid)
    # Discovery can itself finish a deterministic paper-only check (notably an arithmetic
    # recheck) while also finding separate targets worth executing.  That established
    # failure is already in `TargetSet.outcomes` before this stage starts.  Apply the same
    # Tier-1-plus-Tier-2 primitive used between executions *before* even constructing the
    # primary probe or acquiring a repository: acquisition, static audit, identity and
    # execution are all parts of the expensive branch which can no longer change the
    # paper-level decision.  The targets still receive terminal outcomes so reporting and
    # ledger accounting remain complete.
    initial_stopper = (materiality.material_target_failure(
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
        exhaustion.refresh(target_set, cfg)
        state.write_json(discover_stage.targets_path_in(root), target_set.model_dump())
        return {
            "paper_id": pid,
            "verdict": "not_started",
            "reason": (f"a material failure was already established on target "
                       f"{getattr(initial_stopper, 'target_id', '?')}; all pending expensive "
                       f"targets were recorded as superseded and no acquisition or execution "
                       f"was started"),
            "executions": 0,
            "superseded_targets": len(superseded),
        }
    primary = pairs[0][0] if pairs else None
    spec = build_spec(cfg, pid, doc, primary)
    # S3a/S3b run before execution is planned, because what the static audit finds is
    # useful even when every execution gate is shut — and because deciding what to run
    # requires knowing what was acquired.
    acq, audit = acquire_and_audit(cfg, root, pid, doc, spec)
    # S3c runs after acquisition so the planner can see the checkout, and before
    # execution planning so the authors' own entrypoint still wins when it is available.
    spec = synthesize_probe(cfg, doc, spec, acq)
    spec = plan_execution(cfg, spec, acq, doc, audit, root=root)
    # THE FOCUSED-VALIDATION DESIGN, for a target on that route and for no other. It runs
    # HERE — after identity has bound against a real checkout and before the comparison is
    # derived — because a design that binds is what names the two arms, and the arms are
    # what `establish_comparison` counts. A design that does not bind leaves `spec.arms`
    # alone and nothing starts.
    fv_graph = _graph_for(doc, pairs) if pairs else None
    validations: list[FocusedValidation] = []
    fv = None
    if pairs and pairs[0][1].action == validation_stage.ACTION:
        spec, fv, fv_record = validation_stage.prepare(
            cfg, doc, pairs[0][0], pairs[0][1], spec, fv_graph)
        state.write_json(state.control_dir(root) / "validation.driver.json", fv_record)
    # WHAT ITS RESULT WOULD BE HELD AGAINST, from the route the planner chose. Attached
    # whether or not it is established; only the gate below reads it.
    if pairs:
        spec = establish_comparison(
            spec, pairs[0][1].route,
            settlement_declared=bool(fv is not None and fv.design is not None
                                     and fv.design.established))

    # Only the harness's OWN generated spec is re-persisted here. A sealed `driver_accept`
    # spec (`spec.written_by == "driver_accept"`, set in `build_spec` above) must never be
    # overwritten by this run's own regenerated output — that would silently unseal a
    # human's accepted proposal (the file's bytes would no longer match the hash
    # `accept_spec` recorded in `spec.driver.json`, so the NEXT invocation would refuse to
    # trust it, discarding a real driver reproduction for no reason the operator asked for).
    if spec.written_by == "harness":
        state.write_json(state.control_dir(root) / "spec.json", spec.model_dump())

    # THE RULE: nothing starts unless its result could speak. See
    # `admissible_if_it_succeeds` for what this stopped costing, and `may_be_compared` for
    # the same question asked one step earlier — a run whose output has nothing to be held
    # against cannot produce evidence however impeccable the program that produced it.
    comparable, uncomparable_why = may_be_compared(spec)
    if not comparable:
        may_run, refusal = False, uncomparable_why
    else:
        may_run, refusal = admissible_if_it_succeeds(cfg, spec)
    # STEP 6 — the re-plan primitive, consulted once identity has had a real checkout to
    # bind against. `None` on every path where AUTHOR_CODE_EXECUTION was not the route, or
    # identity did bind, or the object never offered a fallback in the first place.
    original_plan = pairs[0][1] if pairs else None
    direct_reconstruction = bool(
        original_plan is not None
        and original_plan.route == "INDEPENDENT_RECONSTRUCTION")
    fallback_plan = replan_after_author_code_exhausted(
        pairs[0][0], original_plan, spec) if pairs else None
    if fallback_plan is not None and target_set is not None:
        target_set.plans.append(fallback_plan)
    reconstruction_plan = original_plan if direct_reconstruction else fallback_plan
    reconstruction_result = None
    if comparable and (direct_reconstruction or not may_run):
        reconstruction_result = attempt_reimplementation_fallback(
            cfg, root, pid, doc, spec, reconstruction_plan)
    if direct_reconstruction and reconstruction_result is not None:
        result = reconstruction_result
    elif direct_reconstruction:
        # The discover-time route was reconstruction itself.  Never run the base
        # synthesized/repository-shaped spec merely because it happens to pass its own
        # gates: that would execute a different route from the one the planner recorded.
        result = ProbeResult(
            paper_id=pid, verdict="not_started", provenance="reimpl_exec",
            reason=(uncomparable_why if not comparable else
                    _direct_reconstruction_note(cfg, reconstruction_plan).strip()),
            executions=0, script_path="")
    elif may_run:
        result = _run(cfg, root, spec)
    elif reconstruction_result is not None:
        result = reconstruction_result
    else:
        # A ProbeResult that records the refusal and no execution. `executions=0` is the
        # measurement the funnel reads, and it is correct: nothing was started.
        result = ProbeResult(
            paper_id=pid, verdict="not_started", provenance=spec.provenance,
            reason=refusal, executions=0, script_path="",
            experiment=spec.experiment, metric_identity=spec.metric_identity,
            configuration=spec.configuration, capability=spec.capability,
            resources=spec.resources, commit_state=spec.commit_state,
            backend=spec.backend)
    result.repo, result.code_audit = acq, audit
    # `probe_results.json` IS THE EXECUTION RECORD, and a paper with no executable target
    # has no execution to record. `_run` already wrote the file when it ran; this rewrite
    # attaches acquisition and the static audit so the file is the whole of S3 rather than
    # a torso the report has to reassemble.
    #
    # THE GUARD IS NOT TIDINESS. This stage is now entered even when nothing is
    # executable, so the three routes that do not execute — artifact inspection, the
    # prior-art search, the focused-validation design — can reach a paper at all; before,
    # the controller returned before calling it and they were skipped for most papers.
    # Writing a ProbeResult on that path made `stages/report` print a "Measured
    # reproduction" section for a probe that started nothing, which is the precise
    # overclaim `tests/test_review` exists to catch. The reading routes seal their own
    # records under `literature/`, `artifact/` and `validation/`; none of them is an
    # execution and none belongs in this file.
    if pairs:
        state.write_json(state.control_dir(root) / "probe_results.json", result.model_dump())

    # --- the remaining targets ---------------------------------------------------------
    # One blocked target used to end the paper's reproduction. It no longer does: the
    # checkout and the static audit are already in hand, so every further target costs
    # only its own run, and each keeps INDEPENDENT state. A paper whose first target is
    # blocked by hardware and whose second reproduces reports both, and the report has to
    # explain both rather than whichever happened to be first.
    outcomes: list[TargetOutcome] = []
    if target_set is not None and pairs:
        if fv is not None and not (fv.design is not None and fv.design.established):
            # THE DESIGN DID NOT BIND, so nothing was started and the reason is the
            # paper's method section rather than any gate of ours. Reported as
            # SPECIFICATION_BLOCKED naming the ingredient, not as COMPARISON_BLOCKED,
            # which would report a limit of our arithmetic for a limit of their reporting.
            outcomes.append(validation_stage.outcome_for(fv, pairs[0][1]))
            validations.append(fv)
        elif fv is not None and may_run and not direct_reconstruction:
            fv = validation_stage.adjudicate(fv, spec, result)
            outcomes.append(validation_stage.outcome_for(
                fv, pairs[0][1], provenance=result.provenance,
                execution_ref=result.execution_log))
            validations.append(fv)
        elif reconstruction_result is not None and reconstruction_plan is not None:
            outcomes.append(outcome_for(pairs[0][0].target_id, reconstruction_result,
                                        reconstruction_plan.action,
                                        reconstruction_plan.route))
        elif may_run and not direct_reconstruction:
            outcomes.append(outcome_for(pairs[0][0].target_id, result,
                                        pairs[0][1].action, pairs[0][1].route))
        else:
            note = (_direct_reconstruction_note(cfg, reconstruction_plan)
                    if direct_reconstruction else
                    (_fallback_note(cfg, fallback_plan) if comparable else ""))
            outcomes.append(_not_started(
                pairs[0][0], pairs[0][1],
                (uncomparable_why if not comparable else refusal + note),
                ("AUTHORIZATION_BLOCKED" if direct_reconstruction and comparable
                 else "IDENTITY_BLOCKED" if comparable else "COMPARISON_BLOCKED")))
        for obj, plan in pairs[1:]:
            # THE DYNAMIC EARLY STOP, and the only place it exists. `harness/assessment.py`
            # stops BEFORE investigation, from findings already counted at grade time; it
            # runs once and knows nothing about what execution then established. So a
            # target that proved a material failure on the authors' own code did not stop
            # the next three targets cloning, planning, authorizing and launching.
            #
            # The condition is deliberately the SAME CALL `claim_status` makes, so this
            # adds no second rule about what a material failure is: Tier 1
            # (`establishes_failure`, which carries the provenance ceiling) AND Tier 2
            # (materiality). A BLOCKER cannot reach it because a blocker does not
            # establish anything, and an established NON-material defect cannot reach it
            # because `material_target_failure` returns None for one — which are the two
            # things this stop must never do.
            stopper = materiality.material_target_failure(target_set.objects, outcomes)
            if stopper is not None:
                outcomes.append(_superseded_by_established_failure(obj, plan, stopper))
                continue
            try:
                other = build_spec(cfg, pid, doc, obj)
                other.written_by = "harness"
                other = synthesize_probe(cfg, doc, other, acq)
                other = plan_execution(cfg, other, acq, doc, audit, root=root)
                other_fv = None
                if plan.action == validation_stage.ACTION:
                    other, other_fv, other_record = validation_stage.prepare(
                        cfg, doc, obj, plan, other, fv_graph)
                    state.write_json(
                        state.control_dir(root) / "targets" / obj.target_id /
                        "validation.driver.json", other_record)
                    if not (other_fv.design is not None
                            and other_fv.design.established):
                        outcomes.append(validation_stage.outcome_for(other_fv, plan))
                        validations.append(other_fv)
                        continue
                other = establish_comparison(
                    other, plan.route,
                    settlement_declared=bool(other_fv is not None
                                             and other_fv.design is not None
                                             and other_fv.design.established))
                tdir = root / "runs" / pid / "targets" / obj.target_id
                ctdir = state.control_dir(root) / "targets" / obj.target_id
                state.write_json(ctdir / "spec.json", other.model_dump())
                # STEP 6 — the same re-plan, per remaining target. `plan` here is the
                # target's ORIGINAL discover-time decision, exactly as for the primary.
                other_fallback = replan_after_author_code_exhausted(obj, plan, other)
                if other_fallback is not None:
                    target_set.plans.append(other_fallback)
                other_direct_reconstruction = plan.route == "INDEPENDENT_RECONSTRUCTION"
                other_reconstruction = (plan if other_direct_reconstruction
                                        else other_fallback)
                comparable_here, why_here = may_be_compared(other)
                if not comparable_here:
                    outcomes.append(_not_started(obj, plan, why_here, "COMPARISON_BLOCKED"))
                    continue
                ok_here, why_here = admissible_if_it_succeeds(cfg, other)
                if other_direct_reconstruction or not ok_here:
                    other_reconstruction_result = attempt_reimplementation_fallback(
                        cfg, root, pid, doc, other, other_reconstruction,
                        out_dir=tdir / "reimplementation")
                    if (other_reconstruction_result is not None
                            and other_reconstruction is not None):
                        state.write_json(ctdir / "probe_results.json",
                                         other_reconstruction_result.model_dump())
                        outcomes.append(outcome_for(
                            obj.target_id, other_reconstruction_result,
                            other_reconstruction.action, other_reconstruction.route))
                        continue
                    note = (_direct_reconstruction_note(cfg, other_reconstruction)
                            if other_direct_reconstruction else
                            _fallback_note(cfg, other_fallback))
                    outcomes.append(_not_started(
                        obj, plan, why_here + note,
                        ("AUTHORIZATION_BLOCKED" if other_direct_reconstruction
                         else "IDENTITY_BLOCKED")))
                    continue
                tres = _run(cfg, root, other, out_dir=tdir, results_dir=ctdir)
                state.write_json(ctdir / "probe_results.json", tres.model_dump())
                if other_fv is not None:
                    other_fv = validation_stage.adjudicate(other_fv, other, tres)
                    outcomes.append(validation_stage.outcome_for(
                        other_fv, plan, provenance=tres.provenance,
                        execution_ref=tres.execution_log))
                    validations.append(other_fv)
                else:
                    outcomes.append(outcome_for(obj.target_id, tres, plan.action,
                                                plan.route))
            except Exception as e:                # noqa: BLE001
                # A fault pursuing one target is not evidence about the paper and must not
                # cost the paper its other targets — the same rule `_phase_probe` applies
                # to the stage as a whole, applied per target.
                outcomes.append(TargetOutcome(
                    target_id=obj.target_id, disposition="INCONCLUSIVE",
                    action=plan.action, route=plan.route,
                    reason=f"pursuing this target raised {type(e).__name__}: {e}. Recorded as "
                           f"inconclusive; a fault in this harness is not evidence about the "
                           f"paper."))
        for obj, plan in deferred:
            # Warranted, ordered, and past this run's budget. Recorded with its own
            # disposition so the review can say "we judged this worth running and did not
            # reach it", which is neither a refusal nor a blocked target.
            outcomes.append(TargetOutcome(
                target_id=obj.target_id, disposition="BUDGET_DEFERRED",
                action=plan.action, route=plan.route, launched=0,
                reason=(f"an experiment is warranted for this target and this run's budget "
                        f"of {cfg.max_targets} target(s) was already spent on "
                        f"higher-priority ones. A limit of this run, not of the paper.")))
        # Persist every secondary outcome, including pre-launch blockers. Cached reviews
        # can then reconstruct the exact route ledger without silently turning a prior
        # attempt into NOT_TRIED merely because no ProbeResult was produced.
        for out in outcomes:
            odir = state.control_dir(root) / "targets" / out.target_id
            state.write_json(odir / "outcome.json", out.model_dump())
        keep = [o for o in target_set.outcomes if o.target_id not in {x.target_id for x in outcomes}]
        target_set.outcomes = keep + outcomes
        by_outcome = {o.target_id: o.disposition for o in outcomes}
        for obj in target_set.objects:
            if obj.target_id in by_outcome:
                obj.status = by_outcome[obj.target_id]
        # The questions were written before anything ran and still say NOT_INVESTIGATED.
        # Re-fold them over the outcomes as they now stand — the same call `discover`
        # makes, which is why it is a fold and not an accumulation.
        discover_stage.sync_questions(target_set)
        exhaustion.refresh(target_set, cfg)
        state.write_json(discover_stage.targets_path_in(root), target_set.model_dump())

    # --- the artifact route ------------------------------------------------------------
    # AFTER acquisition, because it needs the checkout, and structurally separate from the
    # execution loop above because its targets are not executable ones: `planner` reaches
    # ARTIFACT_INSPECTION_ONLY only where no executable route applies, so nothing here
    # competes with, defers, or suppresses a run. It spends no process and no budget.
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
        exhaustion.refresh(target_set, cfg)
        state.write_json(discover_stage.targets_path_in(root), target_set.model_dump())

    # --- the prior-art route -----------------------------------------------------------
    # NEEDS NO CHECKOUT, so it runs whether or not the authors published one, and it is
    # here rather than in `discover` for the same reason the artifact route is: `discover`
    # is pure with respect to the parsed paper and consults no external source at all.
    # Like the artifact route it spends no process and no execution budget, and `planner`
    # reaches LITERATURE_SEARCH_ONLY only where no executable route applies.
    literature_outcomes, search = literature_stage.run_route(cfg, pid, doc, target_set)
    if target_set is not None and literature_outcomes:
        for out in literature_outcomes:
            state.write_json(
                state.control_dir(root) / "targets" / out.target_id / "outcome.json",
                out.model_dump())
        replaced = {o.target_id for o in literature_outcomes}
        target_set.outcomes = [o for o in target_set.outcomes
                               if o.target_id not in replaced] + literature_outcomes
        by_literature = {o.target_id: o.disposition for o in literature_outcomes}
        for obj in target_set.objects:
            if obj.target_id in by_literature:
                obj.status = by_literature[obj.target_id]
        discover_stage.sync_questions(target_set)
        exhaustion.refresh(target_set, cfg)
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
        path=str(state.control_dir(root) / "probe_results.json"),
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
            "artifact_targets": len(artifact_outcomes),
            "artifact_facts": len(inspection.facts) if inspection else 0,
            # THREE COUNTS, NEVER SUMMED. Works retrieved is what the search saw;
            # concerns is what survived every endpoint check; and a concern is a question
            # for a referee, not a novelty finding.
            # FOUR COUNTS, NEVER SUMMED, for the same reason the literature route's three
        # are not: a design attempted, a design that bound, a contrast actually measured
        # and a question actually settled are four different facts, and adding any two of
        # them produces a number that means nothing.
        **{f"validation_{k}": v for k, v in
           validation_stage.summarise(validations).items()},
        "literature_targets": len(literature_outcomes),
            "literature_works": len(search.works) if search else 0,
            "literature_concerns": len(search.concerns()) if search else 0,
            "reconciliation": rec.status if rec else None,
            "seconds": result.seconds, "reason": result.reason,
            "script": result.script_path, "results": "control/probe_results.json"}
