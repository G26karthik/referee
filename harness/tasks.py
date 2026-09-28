"""THE DELEGATION PROTOCOL: what a session subagent should answer right now, and how its
answer gets validated and sealed. The harness never spawns its own `claude` CLI subprocess;
every judgement this review needs (a lens reading, a grade, the substantive verdict, a
governed reconstruction, an authors'-code reading) is answered by an isolated subagent the
controlling session dispatches. `pending()` lists what still needs an answer and where to
read/write it; `seal()` validates whatever comes back and persists it through the SAME
`parse_*`/`accept_*` machinery the manual channel has always used (`harness/agent.py`,
`harness/audit.py`, `harness/reimplement_driver.py`, `harness/artifact_review_driver.py`);
`advance()` runs the deterministic pipeline as far as it can and reports what is pending.

**The protocol, for whatever is orchestrating the subagents:**

    1. `python run.py tasks <paper-id-or-pdf>` -- advances the deterministic pipeline as
       far as it can and prints the pending task list as JSON.
    2. For each task: read `task["prompt"]`, have an ISOLATED subagent answer it (a fresh
       context per task), and have it write its JSON answer to `task["out"]`.
    3. `python run.py seal <paper-id> <task-id> <path-to-that-file>` -- validates and
       seals it. Exit 0 and "sealed <id>" on success; exit 1 and an actionable refusal
       reason on failure.
    4. Re-run `python run.py tasks <paper-id>` -- sealing can unblock a phase, change what
       is pending, or finish the review.

**What this module is NOT.** It does not decide what a lens found, what a grade means, or
whether a reconstruction may run — those are `harness/audit.py`, `harness/decide.py` and
`harness/execute.py`'s jobs. It does not trust a delegate's own say-so about anything
harness-owned (invariant 2): every `seal()` path routes through the same validation the
manual channel already used, and a malformed or self-certifying answer is refused with a
message the worker can act on, never silently repaired.

**Task ids** are `"<role>"` for a whole-paper role (`verdict`, `artifact_review`) or
`"<role>:<sub-id>"` otherwise (`lens:overclaim/part-02`, `reimpl_gen:T0:r0:c0`). `seal()`
dispatches on the role prefix.

`python -m harness.tasks` runs the self-check.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import agent, artifact_review_driver, pipeline, reimplement_driver, state
from .audit import LENSES as _LENSES
from .audit import accept_grade, plan_for, run_audit, run_grade, unit_is_accepted, units_for
from .config import Config
from .prompts import audit as AUDIT_P
from .reviewer_cli import prompt_fingerprint, prompt_is_unchanged
from .schema import PaperDoc

# What every sealed artifact's `reviewer`/`grader`/`reader` field names. One isolated
# subagent context per task -- see `harness.delegation.ISOLATION_CLAIM["SESSION_SUBAGENT"]`
# for exactly what that mode is and is not entitled to claim.
REVIEWER = "controlling session subagent (Agent tool), one isolated context per task"

# Model tier + reasoning effort per role, attached to every task dict as dispatch metadata
# (this harness never calls a model itself). Every role judges prose against evidence
# rather than merely reformatting it, so none is "mechanical" enough for a cheaper tier
# than sonnet; only effort varies with how much the role has to weigh.
_EFFORT: dict[str, tuple[str, str]] = {   # role -> (model, effort)
    "lens": ("sonnet", "high"),             # per-lens/synthesis model is still read from
                                             # `prompts.audit.LENSES` below; this is the
                                             # fallback if a lens declares none.
    "grade": ("sonnet", "medium"),
    "verdict": ("sonnet", "medium"),
    "reimpl_gen": ("sonnet", "high"),
    "reimpl_verify": ("sonnet", "high"),
    "artifact_review": ("sonnet", "medium"),
    "cert_gen": ("sonnet", "high"),
    "cert_verify": ("sonnet", "high"),
    "extraction_audit": ("sonnet", "low"),   # vision compare: mechanical, needs image input
}


def _task(*, id: str, role: str, prompt: Path, out: Path, model: str = "",
         effort: str = "", after: list[str] | None = None) -> dict:
    default_model, default_effort = _EFFORT.get(role, ("sonnet", "medium"))
    return {"id": id, "role": role, "prompt": str(prompt), "out": str(out),
           "model": model or default_model, "effort": effort or default_effort,
           "after": list(after or [])}


def _staging_out(cfg: Config, pid: str, role: str, sub: str) -> Path:
    safe = (sub or role).replace("/", "__").replace(":", "__")
    return state.project_dir(cfg, pid) / "tasks" / "out" / f"{role}__{safe}.json"


def _doc(cfg: Config, pid: str) -> PaperDoc | None:
    p = state.project_dir(cfg, pid) / "paper" / "doc.json"
    if not p.exists():
        return None
    return PaperDoc(**state.read_json(p))


# --------------------------------------------------------------------------- #
# LENS  (whole / part / synthesis units -- was tools/subagent_accept.py's `units`/`pending`)
# --------------------------------------------------------------------------- #
def _lens_units(cfg: Config, pid: str, doc: PaperDoc):
    root = state.project_dir(cfg, pid)
    run_audit(cfg, pid)                # renders/refreshes every prompt; no model call
    return units_for(root, _LENSES, plan_for(doc))


def _lens_tasks(cfg: Config, pid: str, doc: PaperDoc) -> list[dict]:
    """Every lens/part/synthesis unit with a rendered prompt, not yet sealed, and — for a
    synthesis — whose own parts are ALL sealed (a synthesis prompt over incomplete parts
    is not a question anyone can answer yet, so it is not offered)."""
    units = _lens_units(cfg, pid, doc)
    sealed = {u.unit_id for u in units if unit_is_accepted(u)[0]}
    out = []
    for u in units:
        if u.unit_id in sealed or not u.prompt_path.is_file():
            continue
        if u.kind == "synthesis":
            parts = [p for p in units if p.lens == u.lens and p.kind == "part"]
            if not all(p.unit_id in sealed for p in parts):
                continue
        model, effort = _EFFORT["lens"]
        declared = AUDIT_P.LENSES.get(u.lens, {}).get("model", "")
        out.append(_task(id=f"lens:{u.unit_id}", role="lens", prompt=u.prompt_path,
                         out=u.out_path, model=declared or model, effort=effort))
    return out


def _seal_lens(cfg: Config, pid: str, unit_id: str, doc: PaperDoc, raw: str) -> dict:
    units = _lens_units(cfg, pid, doc)
    match = [u for u in units if u.unit_id == unit_id]
    if not match:
        raise ValueError(f"lens:{unit_id}: no such unit for '{pid}'; run "
                         f"`python run.py tasks {pid}` to list what is actually pending")
    unit = match[0]
    # Seal-time mirror of `_lens_tasks`' offer rules: never over a current seal, never a
    # synthesis before its own prompt exists and every part of its lens is sealed.
    if unit_is_accepted(unit)[0]:
        raise ValueError(f"lens:{unit_id}: already sealed against the current prompt")
    if not unit.prompt_path.is_file():
        raise ValueError(f"lens:{unit_id}: its prompt is not rendered yet")
    if unit.kind == "synthesis" and not all(
            unit_is_accepted(p)[0] for p in units if p.lens == unit.lens and p.kind == "part"):
        raise ValueError(f"lens:{unit_id}: a synthesis is sealed only after all its parts")
    report = agent.parse_lens_json(raw, unit.lens)     # raises agent.AgentError on bad input
    unit.out_path.parent.mkdir(parents=True, exist_ok=True)
    state.write_json(unit.out_path, report.model_dump())
    raw_path = unit.out_path.with_suffix(".raw.txt")
    raw_path.write_text(raw, encoding="utf-8")
    record = {
        "lens": unit.lens, "unit_id": unit.unit_id, "paper_id": pid,
        **agent.provenance_record(mode="SESSION_SUBAGENT", reviewer=REVIEWER),
        "content_sha256": hashlib.sha256(unit.out_path.read_bytes()).hexdigest(),
        "prompt_sha256": prompt_fingerprint(unit.prompt_path),
        "prompt_path": str(unit.prompt_path),
        "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "raw_response": raw_path.name,
        "findings": len(report.findings),
        "ts": state.now(),
    }
    state.write_json(unit.sidecar_path, record)
    ok, why = unit_is_accepted(unit)
    if not ok:
        raise ValueError(f"lens:{unit_id}: sealed and still refused: {why}")
    return record


# --------------------------------------------------------------------------- #
# GRADE
# --------------------------------------------------------------------------- #
def _grade_tasks(cfg: Config, pid: str) -> list[dict]:
    res = run_grade(cfg, pid)          # renders prompts; deterministic, no model call
    if "error" in res:
        return []                      # e.g. the lens panel is not complete yet
    model, effort = _EFFORT["grade"]
    return [_task(id=f"grade:{slug}", role="grade", prompt=Path(path),
                  out=state.project_dir(cfg, pid) / "audit" / "grade" / f"{slug}.json",
                  model=model, effort=effort)
            for slug, path in res["prompts"].items()]


def _seal_grade(cfg: Config, pid: str, slug: str, raw: str) -> dict:
    # Only a grade the harness is currently asking for: a slug is a pure function of a
    # model-written finding id, so a grade sealed ahead of its finding would bind to it.
    if slug not in (run_grade(cfg, pid).get("prompts") or {}):
        raise ValueError(f"grade:{slug}: not a pending grade for '{pid}'")
    return accept_grade(cfg, pid, slug, raw, grader=REVIEWER,
                        tool_policy="unrecorded", mode="SESSION_SUBAGENT")


# --------------------------------------------------------------------------- #
# VERDICT  (one whole-paper opinion; its prompt is rendered by `pipeline.run_report_stage`)
# --------------------------------------------------------------------------- #
def _verdict_prompt_path(cfg: Config, pid: str) -> Path:
    return state.project_dir(cfg, pid) / "reports" / "verdict_prompt.md"


def _verdict_task(cfg: Config, pid: str) -> dict | None:
    if agent.load_verdict(cfg, pid) is not None:
        return None
    prompt = _verdict_prompt_path(cfg, pid)
    if not prompt.is_file():
        return None
    model, effort = _EFFORT["verdict"]
    return _task(id="verdict", role="verdict", prompt=prompt,
                out=state.project_dir(cfg, pid) / "reports" / "substantive.json",
                model=model, effort=effort)


def _seal_verdict(cfg: Config, pid: str, raw: str) -> dict:
    prompt = _verdict_prompt_path(cfg, pid)
    prompt_sha = prompt_fingerprint(prompt) if prompt.is_file() else ""
    return agent.accept_verdict(cfg, pid, raw, reader=REVIEWER, mode="SESSION_SUBAGENT",
                                prompt_sha256=prompt_sha)


# --------------------------------------------------------------------------- #
# GOVERNED RECONSTRUCTION  (reimpl_gen then reimpl_verify -- a DIFFERENT subagent context
# for each, exactly as `reimplement_driver.conformance` requires: `generated_by` and
# `verified_by` must be distinct attributions for `independently_verified` to be True)
# --------------------------------------------------------------------------- #
def _readiness(cfg: Config, pid: str, doc: PaperDoc):
    from . import discover
    return discover.reimplementation_readiness(doc)


def _reimpl_generated_path(cfg: Config, pid: str, target_id: str) -> Path:
    return reimplement_driver._briefs_dir(cfg, pid) / "generated" / f"{target_id}.json"


def _reimpl_targets(cfg: Config, pid: str) -> list[str]:
    """Every target with a persisted generator brief -- written by
    `routes.attempt_reimplementation_fallback` during the probe phase, whenever an
    INDEPENDENT_RECONSTRUCTION fallback is in play and no accepted reconstruction is
    sealed yet."""
    d = reimplement_driver._briefs_dir(cfg, pid)
    if not d.is_dir():
        return []
    prefix = "reimpl_gen__"
    return sorted(p.stem[len(prefix):] for p in d.glob(f"{prefix}*.md"))


def _reimpl_tasks(cfg: Config, pid: str, doc: PaperDoc) -> list[dict]:
    out: list[dict] = []
    readiness = None
    gen_model, gen_effort = _EFFORT["reimpl_gen"]
    ver_model, ver_effort = _EFFORT["reimpl_verify"]
    for target_id in _reimpl_targets(cfg, pid):
        gen_path = _reimpl_generated_path(cfg, pid, target_id)
        brief_path = reimplement_driver._briefs_dir(cfg, pid) / f"reimpl_gen__{target_id}.md"
        brief = brief_path.read_text(encoding="utf-8")
        sealed = reimplement_driver.load_accepted(cfg, pid, target_id)
        answered = reimplement_driver.answered_brief(cfg, pid, target_id, brief)
        if sealed is not None and (sealed[1].established or answered):
            continue                   # established, or refused against this very brief
        if sealed is not None and not answered:
            # A refusal of a brief that has since changed: archived (kept for audit), not
            # the answer to the question now being asked.
            for f in reimplement_driver._paths(cfg, pid, target_id):
                if f.is_file():
                    f.replace(f.with_name(f"{f.stem}.superseded{f.suffix}"))
            if gen_path.is_file():
                gen_path.unlink()
        if not gen_path.is_file():
            out.append(_task(id=f"reimpl_gen:{target_id}", role="reimpl_gen",
                             prompt=brief_path,
                             out=_staging_out(cfg, pid, "reimpl_gen", target_id),
                             model=gen_model, effort=gen_effort))
            continue
        # A GENERATION EXISTS: build (or refresh) the verifier's brief from it, and offer
        # the verify task -- ONLY now, and only to a subagent this generator's own answer
        # was not shown building the brief for (a fresh context reading it below).
        if readiness is None:
            readiness = _readiness(cfg, pid, doc)
        raw = gen_path.read_text(encoding="utf-8")
        verify_path = reimplement_driver.persist_verification_brief(
            cfg, pid, target_id, readiness, raw)
        out.append(_task(id=f"reimpl_verify:{target_id}", role="reimpl_verify",
                         prompt=verify_path,
                         out=_staging_out(cfg, pid, "reimpl_verify", target_id),
                         model=ver_model, effort=ver_effort,
                         after=[f"reimpl_gen:{target_id}"]))
    return out


def _seal_reimpl_gen(cfg: Config, pid: str, target_id: str, raw: str) -> dict:
    # Validate it parses as a reconstruction report before storing it -- the same
    # validation `reimplement_driver.accept_reimplementation` will run again once a
    # verifier's answer is sealed; failing fast here means a malformed generation never
    # reaches the verifier task at all.
    reimplement_driver.parse_reimplementation_report(raw)
    path = _reimpl_generated_path(cfg, pid, target_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    reimplement_driver.stamp_brief(cfg, pid, target_id)
    return {"target_id": target_id, "stored": str(path)}


def _refuse_malformed_verdict(task_id: str, notes: str) -> None:
    """A verifier answer that is not valid JSON is a delivery error, not a judgement: it is
    refused so the worker fixes and resubmits it, never sealed as a rejection."""
    if notes.startswith(("verifier JSON invalid", "verifier returned no JSON")):
        raise ValueError(f"{task_id}: {notes}")


def _seal_reimpl_verify(cfg: Config, pid: str, target_id: str, doc: PaperDoc, raw: str) -> dict:
    gen_path = _reimpl_generated_path(cfg, pid, target_id)
    if not gen_path.is_file():
        raise ValueError(f"reimpl_verify:{target_id}: no generated reconstruction to verify "
                         f"for '{pid}'; seal reimpl_gen:{target_id} first")
    # The rule `reimplement_driver.run` always applied: the verifier is named on the seal
    # ONLY if its reply parses as approved. A rejection is not an error -- it seals
    # honestly as `established=False`, exactly as a human reviewer's rejection would.
    script, bindings, _gnotes, _meta = reimplement_driver.parse_reimplementation_report(
        gen_path.read_text(encoding="utf-8"))
    approved, _notes = reimplement_driver._parse_verification(
        raw, reimplement_driver.required_kinds(
            script, bindings, reimplement_driver.load_released(cfg, pid, target_id)))
    _refuse_malformed_verdict(f"reimpl_verify:{target_id}", _notes)
    readiness = _readiness(cfg, pid, doc)
    reimplement_driver.accept_reimplementation(
        cfg, pid, target_id, gen_path.read_text(encoding="utf-8"), readiness,
        reviewer=(REVIEWER + " (verifier)") if approved else "",
        generated_by=REVIEWER + " (generator)", mode="SESSION_SUBAGENT")
    sealed = reimplement_driver.load_accepted(cfg, pid, target_id)
    if sealed is None:
        raise ValueError(f"reimpl_verify:{target_id}: sealed but refused by load_accepted "
                         f"-- an internal attribution inconsistency, not a JSON problem")
    _script, conf = sealed
    return {"target_id": target_id, "established": conf.established, "reason": conf.reason}


# --------------------------------------------------------------------------- #
# AUTHORS'-CODE READING  (one per paper; its prompt is persisted by
# `harness.stages.artifact._reviewer_facts` during the probe phase)
# --------------------------------------------------------------------------- #
def _artifact_review_prompt_path(cfg: Config, pid: str) -> Path:
    return state.project_dir(cfg, pid) / "tasks" / "artifact_review.md"


def _artifact_review_task(cfg: Config, pid: str) -> dict | None:
    prompt = _artifact_review_prompt_path(cfg, pid)
    if not prompt.is_file():
        return None
    _out, sidecar = artifact_review_driver._paths(cfg, pid)
    fresh, _note = prompt_is_unchanged(sidecar, prompt)
    if fresh:
        return None                    # a valid sealed reading already answers this
    model = artifact_review_driver.role_model(cfg)
    _default_model, effort = _EFFORT["artifact_review"]
    return _task(id="artifact_review", role="artifact_review", prompt=prompt,
                out=_staging_out(cfg, pid, "artifact_review", "inspection"),
                model=model, effort=effort)


def _checkout_for(cfg: Config, pid: str, doc: PaperDoc) -> tuple[Path, str]:
    """Where `routes.repo.acquire` already cloned this paper's repository, if it did —
    read-only: this never fetches anything itself, only locates what is already there."""
    from . import repo as repo_mod
    root = state.project_dir(cfg, pid)
    dest = root / "runs" / pid / "repo"
    url = doc.repo_url or repo_mod.official_repo_url(doc)
    return dest, url


def _seal_artifact_review(cfg: Config, pid: str, doc: PaperDoc, raw: str) -> dict:
    root, url = _checkout_for(cfg, pid, doc)
    if not root.is_dir():
        raise ValueError(f"artifact_review: no checkout at {root} for '{pid}' to relocate "
                         f"citations against")
    prompt = _artifact_review_prompt_path(cfg, pid)
    prompt_sha = prompt_fingerprint(prompt) if prompt.is_file() else ""
    inspection = artifact_review_driver.accept(
        cfg, pid, doc, root, raw, url=url, reader=REVIEWER, mode="SESSION_SUBAGENT",
        prompt_sha256=prompt_sha)
    return {"proposed": inspection.proposed, "relocated": inspection.relocated,
           "discharged": inspection.discharged}


# --------------------------------------------------------------------------- #
# EXACT CERTIFICATE  (cert_gen then cert_verify, distinct subagents -- same rule as above)
# --------------------------------------------------------------------------- #
def _cert_dir(cfg: Config, pid: str) -> Path:
    return state.project_dir(cfg, pid) / "tasks" / "certificate"


def _cert_obj(cfg: Config, pid: str, target_id: str):
    from . import discover
    ts = discover.load(cfg, pid)
    return next((o for o in (ts.objects if ts else []) if o.target_id == target_id), None)


def _cert_tasks(cfg: Config, pid: str, doc: PaperDoc) -> list[dict]:
    from . import certificate, routes
    d, out = _cert_dir(cfg, pid), []
    for tid, brief in routes.certificate_targets(cfg, pid):
        gen = d / "generated" / f"{tid}.json"
        stamp = gen.with_suffix(".brief_sha256")
        if gen.is_file() and (not stamp.is_file()
                              or stamp.read_text(encoding="utf-8") != certificate.brief_sha(brief)):
            gen.unlink()               # answered a brief that has since changed
        if not gen.is_file():
            prompt = d / f"cert_gen__{tid}.md"
            prompt.parent.mkdir(parents=True, exist_ok=True)
            prompt.write_text(brief, encoding="utf-8")
            out.append(_task(id=f"cert_gen:{tid}", role="cert_gen", prompt=prompt,
                             out=_staging_out(cfg, pid, "cert_gen", tid)))
            continue
        obj = _cert_obj(cfg, pid, tid)
        raw = gen.read_text(encoding="utf-8")
        script, bindings, quotes, _notes, _n = certificate.parse(raw)
        prompt = d / f"cert_verify__{tid}.md"
        claim, ref = getattr(obj, "claim_text", ""), getattr(obj, "ref", None)
        prompt.write_text(certificate.verification_brief(
            doc, claim=claim, ref=ref, script=script, bindings=bindings, paper_quotes=quotes,
            scope=certificate.parse_scope(raw),
            images=certificate.page_images(cfg, pid, doc, ref, claim)), encoding="utf-8")
        out.append(_task(id=f"cert_verify:{tid}", role="cert_verify", prompt=prompt,
                         out=_staging_out(cfg, pid, "cert_verify", tid),
                         after=[f"cert_gen:{tid}"]))
    return out


def _seal_cert_gen(cfg: Config, pid: str, tid: str, raw: str) -> dict:
    from . import certificate
    certificate.parse(raw)             # fail fast: a malformed generation never reaches a verifier
    path = _cert_dir(cfg, pid) / "generated" / f"{tid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    prompt = _cert_dir(cfg, pid) / f"cert_gen__{tid}.md"
    brief = prompt.read_text(encoding="utf-8") if prompt.is_file() else ""
    path.with_suffix(".brief_sha256").write_text(certificate.brief_sha(brief), encoding="utf-8")
    return {"target_id": tid, "stored": str(path)}


def _seal_cert_verify(cfg: Config, pid: str, tid: str, raw: str) -> dict:
    from . import certificate
    gen = _cert_dir(cfg, pid) / "generated" / f"{tid}.json"
    if not gen.is_file():
        raise ValueError(f"cert_verify:{tid}: seal cert_gen:{tid} first")
    _refuse_malformed_verdict(f"cert_verify:{tid}", certificate.parse_verification(raw)[1])
    certificate.accept(cfg, pid, tid, gen.read_text(encoding="utf-8"), raw,
                       generated_by=REVIEWER + " (generator)",
                       reviewer=REVIEWER + " (verifier)",
                       brief_sha256=(gen.with_suffix(".brief_sha256").read_text(encoding="utf-8")
                                     if gen.with_suffix(".brief_sha256").is_file() else ""))
    sealed = certificate.load_accepted(cfg, pid, tid)
    return {"target_id": tid, "established": bool(sealed and sealed[1].established)}


# --------------------------------------------------------------------------- #
# EXTRACTION AUDIT  (vision check of the parser; only ever a document observation)
# --------------------------------------------------------------------------- #
def _extraction_audit_task(cfg: Config, pid: str) -> dict | None:
    from . import extraction_audit
    if extraction_audit.load(cfg, pid) is not None:
        return None
    return _task(id="extraction_audit", role="extraction_audit",
                 prompt=extraction_audit.build_prompt(cfg, pid),
                 out=_staging_out(cfg, pid, "extraction_audit", "audit"))


# --------------------------------------------------------------------------- #
# THE PROTOCOL
# --------------------------------------------------------------------------- #
def pending(cfg: Config, pid: str) -> list[dict]:
    """Every delegable unit this paper still needs RIGHT NOW — deterministic, no model
    call, safe to call as often as wanted. `[]` before the paper is ingested."""
    doc = _doc(cfg, pid)
    if doc is None:
        return []
    # Offer a role only once the case has reached the phase that consumes it: a verdict,
    # reconstruction or certificate built from a previous run's discovery is stale work.
    from .schema import PHASES
    case = pipeline.load_case(cfg, pid)
    at = PHASES.index(case.phase) if case and case.phase in PHASES else 0
    x = _extraction_audit_task(cfg, pid)          # independent of every phase: run early
    out: list[dict] = [x] if x is not None else []
    out += _lens_tasks(cfg, pid, doc)
    if at >= PHASES.index("grade"):
        out += _grade_tasks(cfg, pid)
    if at >= PHASES.index("probe"):
        out += _reimpl_tasks(cfg, pid, doc)
        a = _artifact_review_task(cfg, pid)
        out += [a] if a is not None else []
        out += _cert_tasks(cfg, pid, doc)
    if at >= PHASES.index("report"):
        v = _verdict_task(cfg, pid)
        out += [v] if v is not None else []
    return out


def _require_doc(doc: PaperDoc | None, pid: str, label: str) -> PaperDoc:
    if doc is None:
        raise ValueError(f"{label}: '{pid}' has not been ingested")
    return doc


def seal(cfg: Config, pid: str, task_id: str, path: str | Path) -> dict:
    """Validate a staged answer and, on success, persist it through the same
    `parse_*`/`accept_*` gate the manual channel has always used. Raises `ValueError` with
    an actionable message on any validation failure — the worker reads it and fixes its
    JSON; nothing here repairs a malformed answer."""
    from . import extraction_audit as EA
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"{task_id}: no such file: {src}")
    raw = src.read_text(encoding="utf-8")
    role, _, sub = str(task_id).partition(":")
    doc = _doc(cfg, pid)
    # Table-driven dispatch by role prefix. Each lambda is only EVALUATED for the matching
    # role, so a role needing no doc never pays `_require_doc`'s check.
    handlers = {
        "lens": lambda: _seal_lens(cfg, pid, sub, _require_doc(doc, pid, f"lens:{sub}"), raw),
        "grade": lambda: _seal_grade(cfg, pid, sub, raw),
        "verdict": lambda: _seal_verdict(cfg, pid, raw),
        "reimpl_gen": lambda: _seal_reimpl_gen(cfg, pid, sub, raw),
        "reimpl_verify": lambda: _seal_reimpl_verify(
            cfg, pid, sub, _require_doc(doc, pid, f"reimpl_verify:{sub}"), raw),
        "artifact_review": lambda: _seal_artifact_review(
            cfg, pid, _require_doc(doc, pid, "artifact_review"), raw),
        "cert_gen": lambda: _seal_cert_gen(cfg, pid, sub, raw),
        "cert_verify": lambda: _seal_cert_verify(cfg, pid, sub, raw),
        "extraction_audit": lambda: EA.seal(cfg, pid, raw),
    }
    handler = handlers.get(role)
    if handler is None:
        raise ValueError(f"{task_id}: unknown role {role!r}; expected one of "
                         f"{', '.join(handlers)}")
    try:
        return handler()
    except ValueError:
        raise
    except Exception as e:                        # noqa: BLE001 — turned into an actionable refusal
        raise ValueError(f"{task_id}: {type(e).__name__}: {e}") from e


def _seal_newer_than_probe(cfg: Config, pid: str) -> bool:
    """A certificate or reconstruction sealed after the last probe pass is evidence that
    pass never saw: the probe must run again (a plain rewind stops short of it)."""
    root = state.project_dir(cfg, pid)
    probe = state.control_dir(root) / "probe_results.json"
    if not probe.is_file():
        return False
    since = probe.stat().st_mtime
    runs = root / "runs" / pid
    seals = [*runs.glob("certificates/*.driver.json"), *runs.glob("reimplementation/*.driver.json")]
    return any(s.stat().st_mtime > since for s in seals)


def advance(cfg: Config, pid: str) -> dict:
    """Run the deterministic pipeline as far as it goes, then report what is pending.

    A single `pipeline.drive` call both re-renders every prompt a phase would render (each
    phase handler is idempotent and driven by what is on disk) and, for a case that was
    already `complete`, REWINDS to `collect` and re-derives — so a grade or a verdict
    sealed after the report was last written is picked up and the report re-rendered,
    without this function needing its own rewind logic.
    """
    case = pipeline.open_case(cfg, pid)
    case = pipeline.drive(cfg, case, force_probe=_seal_newer_than_probe(cfg, case.paper_id or pid))
    resolved_pid = case.paper_id or pid
    return {"paper_id": resolved_pid, "phase": case.phase, "status": case.status,
           "blocked_reason": case.blocked_reason, "tasks": pending(cfg, resolved_pid)}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.tasks
    import json
    import subprocess
    import tempfile

    from .schema import Grade, SubstantiveVerdict

    def _write(base: Path, content: str) -> Path:
        p = base / f"staged-{hashlib.sha256(content.encode()).hexdigest()[:12]}.json"
        p.write_text(content, encoding="utf-8")
        return p

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = Config(projects_dir=td / "projects")

        # --- before ingest: nothing pending ------------------------------------------------
        assert pending(cfg, "nope") == []

        # --- seal() dispatches by role prefix, and refuses cleanly ------------------------
        pid = "p"
        state.create_project(cfg, "", "T", pid=pid)
        from .schema import PaperDoc as _PD, Section
        state.write_json(
            state.project_dir(cfg, pid) / "paper" / "doc.json",
            _PD(paper_id=pid, title="T", n_pages=1,
               sections=[Section(section_idx=0, title="Method", page_start=1,
                                 text="We train with Adam for 30 epochs.")]).model_dump())

        # A task id naming a role this module does not know is refused, not silently
        # ignored -- the worker's mistake is visible, not swallowed.
        stub = td / "stub.json"
        stub.write_text('{"whatever": 1}', encoding="utf-8")
        try:
            seal(cfg, pid, "not_a_role:x", stub)
            raise AssertionError("an unknown role must be refused")
        except ValueError as e:
            assert "unknown role" in str(e)

        # A missing staged file is refused with the path named.
        try:
            seal(cfg, pid, "lens:overclaim", td / "does-not-exist.json")
            raise AssertionError("a missing file must be refused")
        except ValueError as e:
            assert "no such file" in str(e)

        # A lens unit id that does not exist for this paper is refused, actionably.
        try:
            seal(cfg, pid, "lens:not-a-lens", stub)
            raise AssertionError("an unknown lens unit must be refused")
        except ValueError as e:
            assert "no such unit" in str(e)

        # --- grade / verdict seal through the SAME accept_* the manual channel uses ------
        # A grade the harness is not asking for (no such candidate) is refused: a slug is a
        # pure function of a model-written finding id and would bind to a later finding.
        grade_json = Grade(verdict="CONFIRMED", severity="MAJOR").model_dump()
        try:
            _seal_grade(cfg, pid, "c-01", json.dumps(grade_json))
            raise AssertionError("a grade sealed ahead of its finding must be refused")
        except ValueError as e:
            assert "not a pending grade" in str(e)
        # The sealing itself (offline, against a hand-placed prompt)
        grade_dir = state.project_dir(cfg, pid) / "audit" / "grade"
        (grade_dir / "prompts").mkdir(parents=True, exist_ok=True)
        rec = accept_grade(cfg, pid, "c-01", json.dumps(grade_json), grader=REVIEWER,
                           mode="SESSION_SUBAGENT")
        assert rec["verdict"] == "CONFIRMED" and rec["written_by"] == "session_subagent"
        assert rec["grader"] == REVIEWER

        verdict_prompt = _verdict_prompt_path(cfg, pid)
        verdict_prompt.parent.mkdir(parents=True, exist_ok=True)
        verdict_prompt.write_text("assess this paper", encoding="utf-8")
        assert _verdict_task(cfg, pid) is not None, "a rendered prompt with no verdict yet is pending"
        v = SubstantiveVerdict(verdict="STRONG", reason="r", strongest_contribution="c",
                              weakest_link="w", weaknesses_are="LOCAL")
        vrec = seal(cfg, pid, "verdict", _write(td, json.dumps(v.model_dump())))
        assert vrec["verdict"] == "STRONG" and vrec["delegation_mode"] == "SESSION_SUBAGENT"
        assert _verdict_task(cfg, pid) is None, "a sealed, fresh-prompt verdict is not pending again"

        # --- reimpl_gen -> reimpl_verify: a DIFFERENT context for each, mutually exclusive
        # in the pending list ---------------------------------------------------------------
        from .schema import Table

        pid2 = "p2"
        state.create_project(cfg, "", "T2", pid=pid2)
        doc2 = _PD(paper_id=pid2, title="T2", n_pages=1, sections=[
            Section(section_idx=0, title="Method", page_start=1,
                   text="We define the objective as a sum and train with Adam for 30 "
                        "epochs on the CIFAR-100 dataset, reporting accuracy on Table 1.")],
                  tables=[Table(table_idx=0, caption="Table 1: results",
                                header=["method", "acc"], rows=[["ours", "0.9"]])])
        state.write_json(state.project_dir(cfg, pid2) / "paper" / "doc.json", doc2.model_dump())
        reimplement_driver.persist_brief(cfg, pid2, "t1", "GENERATE A RECONSTRUCTION")
        gen_tasks = _reimpl_tasks(cfg, pid2, doc2)
        assert len(gen_tasks) == 1 and gen_tasks[0]["id"] == "reimpl_gen:t1"
        assert gen_tasks[0]["model"] == "sonnet" and gen_tasks[0]["effort"] == "high"

        good_gen = json.dumps({
            "script": "import argparse\n# method: sum objective\ndef train():\n    pass\n"
                     "print('SH_METRIC arm=a seed=0 value=0.9')",
            "bindings": [
                {"kind": "method", "impl_ref": "line 2", "impl_quote": "# method: sum objective"},
                {"kind": "training", "impl_ref": "line 3", "impl_quote": "def train():"},
                {"kind": "dataset", "impl_ref": "line 1", "impl_quote": "import argparse"},
                {"kind": "metric", "impl_ref": "line 5",
                "impl_quote": "SH_METRIC arm=a seed=0 value=0.9"},
                {"kind": "comparison_target", "impl_ref": "line 5",
                "impl_quote": "SH_METRIC arm=a seed=0 value=0.9"},
            ], "notes": ""})
        gen_rec = seal(cfg, pid2, "reimpl_gen:t1", _write(td, good_gen))
        assert gen_rec["target_id"] == "t1"

        verify_tasks = _reimpl_tasks(cfg, pid2, doc2)
        assert len(verify_tasks) == 1 and verify_tasks[0]["id"] == "reimpl_verify:t1", (
            "once generated, ONLY reimpl_verify is offered for this target")
        assert verify_tasks[0]["after"] == ["reimpl_gen:t1"]
        verify_prompt = Path(verify_tasks[0]["prompt"]).read_text(encoding="utf-8")
        assert "independent verifier" in verify_prompt

        approval = json.dumps({"approved": True, "approved_kinds": [
            "method", "training", "dataset", "metric", "comparison_target"], "notes": "ok"})
        vrec2 = seal(cfg, pid2, "reimpl_verify:t1", _write(td, approval))
        assert vrec2["established"] is True, vrec2
        assert _reimpl_tasks(cfg, pid2, doc2) == [], (
            "an established reconstruction is not offered again")
        assert reimplement_driver.load_accepted(cfg, pid2, "t1") is not None

        # A rejection seals honestly rather than erroring.
        reimplement_driver.persist_brief(cfg, pid2, "t2", "GENERATE ANOTHER")
        seal(cfg, pid2, "reimpl_gen:t2", _write(td, good_gen))
        rejection = json.dumps({"approved": False, "approved_kinds": [], "notes": "no"})
        vrec3 = seal(cfg, pid2, "reimpl_verify:t2", _write(td, rejection))
        assert vrec3["established"] is False

        # --- authors'-code reading: persisted prompt -> pending task -> sealed answer ----
        pid3 = "p3"
        state.create_project(cfg, "", "T3", pid=pid3)
        doc3 = _PD(paper_id=pid3, title="T3", n_pages=1, repo_url="https://example.invalid/r",
                  sections=[Section(section_idx=0, title="Method", page_start=1,
                                    text="We release requirements.txt alongside the code.")])
        state.write_json(state.project_dir(cfg, pid3) / "paper" / "doc.json", doc3.model_dump())

        root3 = td / "repo3"
        root3.mkdir()
        (root3 / "requirements.txt").write_text("torch==2.1.0\n", encoding="utf-8")
        git_ok = True
        try:
            for args in (("init", "-q"), ("config", "user.email", "s@e"),
                        ("config", "user.name", "s"), ("add", "-A"), ("commit", "-qm", "c")):
                subprocess.run(["git", *args], cwd=root3, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):    # pragma: no cover
            git_ok = False

        if git_ok:
            assert _artifact_review_task(cfg, pid3) is None, "no task before any checkout exists"
            checkout, url = _checkout_for(cfg, pid3, doc3)
            checkout.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.copytree(root3, checkout)
            assert url == "https://example.invalid/r"

            from . import artifact_evidence
            from .stages import artifact as artifact_stage
            snap = artifact_evidence.snapshot(checkout, url)
            artifact_stage._reviewer_facts(cfg, pid3, doc3, checkout, snap, url, [])
            task = _artifact_review_task(cfg, pid3)
            assert task is not None and task["role"] == "artifact_review"
            assert task["prompt"] == str(_artifact_review_prompt_path(cfg, pid3))

            concern = json.dumps({"concerns": [
                {"kind": "SUSPICIOUS_IMPLEMENTATION", "title": "x", "statement": "s",
                 "file": "requirements.txt", "code_quote": "torch==2.1.0"}], "notes": "n"})
            arec = seal(cfg, pid3, "artifact_review", _write(td, concern))
            assert arec["proposed"] == 1 and arec["relocated"] == 1
            assert _artifact_review_task(cfg, pid3) is None, (
                "a fresh sealed reading is not pending again")
        else:                                       # pragma: no cover
            print("harness.tasks self-check: git unavailable, skipping artifact_review path")

        # --- advance(): runs the deterministic pipeline and never raises on an already-
        # ingested, minimally-populated project (whatever it cannot progress past becomes
        # an 'error'/'waiting' PhaseOutcome, not an exception escaping this function) -----
        res = advance(cfg, pid)
        assert set(res) == {"paper_id", "phase", "status", "blocked_reason", "tasks"}
        assert res["paper_id"] == pid

    print(json.dumps({"self_check": "ok"}, indent=2))
