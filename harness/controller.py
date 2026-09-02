"""The orchestration loop, as a program rather than a convention.

Before this module, `review()` ran the deterministic stages, hit the audit stage, returned
`status="needs_audit"`, and the process ended. Resuming depended on an actor outside the
repository reading prose in CLAUDE.md and re-invoking the CLI. Every stage worked; the
workflow between them did not exist as code. This module is that workflow.

`python -m harness.controller` runs the self-check.

**What the controller may do.** Sequence phases, decide what to attempt next from
persisted state, re-attempt a phase whose failure was transient, and record everything it
observed. That is the whole list.

**What it may not do, and cannot.** It cannot authorize an execution, select a backend,
set an identity state, decide a resource sufficiency, or write a reconciliation status.
Those live below it in `backends.authorize`, `experiment_id`, `resources` and
`local_exec.reconcile`, and the controller only ever *requests* the phase that consults
them and *records the answer it got*. A controller that could overrule a gate would make
every gate advisory.

**Abstention is not failure.** This is the distinction that decides whether the system
survives contact with real papers. A paper with no repository, an ambiguous experiment, a
24 GiB requirement on an 8 GiB card, or a backend that cannot be provisioned still gets a
complete review — the reproduction abstains, `CaseState.reproduction_class` names why,
and the case reaches `complete` with a report. Only a paper that could not be read at all,
or one where no lens produced anything, is an `error`. Treating an unreproducible paper as
a crashed run would make the harness fail hardest on exactly the papers that most need a
careful answer.

**Retries are bounded, recorded, and rare.** Precisely one thing is retryable: a lens
whose reviewer failed. A timeout or a truncated JSON object is transient and the same
prompt may well succeed on a second attempt. Nothing else is. An identity, resource,
commit, capability or authorization refusal is deterministic — re-running one is an
attempt to get a different answer out of a gate that is working correctly, and this module
does not have a code path that does it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import audit_driver, state
from .artifacts import (PHASES, CaseState, PaperDoc, PhaseEvent, ProbeResult)
from .config import Config
from .stages import audit as audit_stage
from .stages import ingest as ingest_stage
from .stages import probe as probe_stage
from .stages import report as report_stage

# Only this phase may be re-attempted, and only up to `cfg.audit_retries`. See the module
# docstring: everything else that can refuse is deterministic, and retrying a deterministic
# refusal is asking a gate the same question until it changes its mind.
RETRYABLE = ("audit",)


@dataclass(frozen=True)
class PhaseOutcome:
    """What one attempt at one phase concluded.

    `abstain` is deliberately absent from the ways a CASE can end. A phase may abstain —
    the probe routinely does — and the pipeline continues to the report regardless. The
    abstention travels on `reproduction_class`, not as a terminal state.
    """

    outcome: str                       # ok | waiting | retry | abstain | error
    reason: str = ""
    detail: dict | None = None
    reproduction_class: str = ""

    @property
    def advances(self) -> bool:
        return self.outcome in ("ok", "abstain")


# --------------------------------------------------------------------------- #
# Case lifecycle
# --------------------------------------------------------------------------- #
def case_path(cfg: Config, pid: str) -> Path:
    return state.project_dir(cfg, pid) / "controller.json"


def load_case(cfg: Config, pid: str) -> CaseState | None:
    p = case_path(cfg, pid)
    if not p.exists():
        return None
    try:
        return CaseState(**state.read_json(p))
    except (OSError, ValueError):
        return None


def save_case(cfg: Config, case: CaseState) -> CaseState:
    if case.paper_id:
        state.write_json(case_path(cfg, case.paper_id), case.model_dump())
    return case


def rewind(case: CaseState, phase: str) -> CaseState:
    """Move a finished case back to `phase` so a re-invocation actually re-derives.

    Without this a `complete` case is terminal, `step` returns it untouched, and
    re-running `review` on it hands back whatever report was on disk — including after
    the code that produced it has changed, and including when the caller explicitly asked
    for `--force-probe`. The flag was accepted and silently ignored.

    The history is kept. A case that has been re-derived should show both runs, because
    "this was reviewed twice and the verdict moved" is exactly the kind of thing a reader
    of the second report needs to be able to see.
    """
    if PHASES.index(phase) >= PHASES.index(case.phase) and case.phase != "done":
        return case
    case.phase, case.status, case.blocked_reason = phase, "running", ""
    case.attempts = {k: v for k, v in case.attempts.items()
                     if PHASES.index(k) < PHASES.index(phase)}
    return case


def open_case(cfg: Config, source: str) -> CaseState:
    """The case for one input — a PDF path or an already-ingested id. Resumes if it exists.

    Resuming from disk rather than starting fresh is what makes the loop survive the
    process ending, which is the normal way a run stops when it is waiting for lens
    evidence that has to arrive from somewhere else.
    """
    src = Path(source).expanduser()
    if not (src.suffix.lower() == ".pdf" or src.exists()):
        existing = load_case(cfg, source)
        if existing:
            return existing
        return CaseState(paper_id=source, source=source, phase="ingest")
    return CaseState(source=str(src), phase="ingest")


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
def _phase_ingest(cfg: Config, case: CaseState, **_) -> PhaseOutcome:
    src = Path(case.source).expanduser()
    if not (src.suffix.lower() == ".pdf" or src.exists()):
        # A bare case id: the paper was ingested by an earlier run.
        pid = case.paper_id or case.source
        if not (state.project_dir(cfg, pid) / "paper" / "doc.json").exists():
            return PhaseOutcome("error", f"'{case.source}' is neither a PDF path nor an "
                                         f"already-ingested case id")
        case.paper_id = pid
        return PhaseOutcome("ok", "resumed from an existing ingest", {"cached": True})
    try:
        res = ingest_stage.run_ingest(cfg, str(src))
    except FileNotFoundError as e:
        return PhaseOutcome("error", str(e))
    case.paper_id = res["paper_id"]
    case.content_sha = res.get("content_sha", "") or ""
    return PhaseOutcome("ok", f"ingested {res.get('sections', 0)} section(s), "
                              f"{res.get('tables', 0)} table(s)", res)


def _phase_audit(cfg: Config, case: CaseState, *, auto_audit: bool = False, **_) -> PhaseOutcome:
    """Render the lens prompts, then fill whatever the configured reviewer can fill.

    The two halves are separate on purpose. Prompt rendering is deterministic and always
    happens, so a run that cannot delegate still leaves an operator everything they need.
    Filling is the delegation, and it is allowed to fail per lens without taking the
    others down — three lenses of evidence is a weaker panel than four, and it is not
    nothing.
    """
    res = audit_stage.run_audit(cfg, case.paper_id)
    if "error" in res:
        return PhaseOutcome("error", res["error"])
    case.awaiting = list(res["awaiting"])
    if not case.awaiting:
        return PhaseOutcome("ok", "every lens already has a result",
                            {"complete": res["complete"]})

    if not auto_audit:
        return PhaseOutcome(
            "waiting",
            f"{len(case.awaiting)} lens(es) have no result: {', '.join(case.awaiting)}. "
            f"Prompts are in audit/prompts/; run with --auto-audit to delegate them.",
            {"awaiting": case.awaiting, "prompts": res["prompts"]})

    ok, why = audit_driver.available(cfg)
    if not ok:
        # The reviewer is unavailable, which is an operator-configuration fact, not a
        # transient one. Waiting rather than retrying: a second attempt would fail
        # identically and would only bury the reason under repeated attempts.
        return PhaseOutcome("waiting", f"cannot delegate the audit: {why}",
                            {"awaiting": case.awaiting})

    filled = audit_driver.fill(cfg, case.paper_id, case.awaiting, res["prompts"])
    after = audit_stage.run_audit(cfg, case.paper_id)
    case.awaiting = list(after.get("awaiting", []))
    detail = {"filled": filled["filled"], "failed": filled["failed"],
              "awaiting": case.awaiting}
    if not case.awaiting:
        return PhaseOutcome("ok", f"delegated and filled {len(filled['filled'])} lens(es)", detail)
    # `step` increments the counter BEFORE calling this handler, so `attempts` already
    # includes the attempt that just failed. `audit_retries` counts RE-attempts, so the
    # budget is exhausted once `attempts` exceeds it: retries=2 yields three tries in
    # total. Comparing with `<` instead spent the budget one attempt early and, at
    # retries=1, retried nothing at all.
    attempts = case.attempts.get("audit", 1)
    why = "; ".join(f"{k}: {v}" for k, v in filled["failed"].items())
    if attempts <= max(0, cfg.audit_retries):
        return PhaseOutcome(
            "retry",
            f"{len(case.awaiting)} lens(es) still pending after attempt {attempts} of "
            f"{cfg.audit_retries + 1}: {why}", detail)
    return PhaseOutcome(
        "waiting",
        f"{len(case.awaiting)} lens(es) could not be produced in {attempts} attempt(s): {why}",
        detail)


def _phase_collect(cfg: Config, case: CaseState, **_) -> PhaseOutcome:
    """Load every lens result and verify its evidence. The gate the LLM cannot pass.

    `load_reports` re-checks each finding's quote against the parsed paper and, when a
    cell is cited, against that cell's contents. Findings that fail are dropped and
    counted here — the same treatment whether a human, a session, or a delegated
    subprocess wrote the file.
    """
    doc_path = state.project_dir(cfg, case.paper_id) / "paper" / "doc.json"
    if not doc_path.exists():
        return PhaseOutcome("error", "no ingested paper to verify findings against")
    doc = PaperDoc(**state.read_json(doc_path))
    reports, dropped = audit_stage.load_reports(cfg, case.paper_id, doc)
    if not reports:
        return PhaseOutcome("error", "no audit lens produced a result, so there is nothing "
                                     "to review; the paper has not been audited")
    kept = sum(len(r.findings) for r in reports)
    return PhaseOutcome("ok", f"{len(reports)} lens(es), {kept} substantiated finding(s), "
                              f"{dropped} dropped as unsubstantiated",
                        {"lenses": [r.lens for r in reports], "findings": kept,
                         "dropped": dropped})


def _reproduction_class(result: ProbeResult | None) -> str:
    """Why reproduction did not conclude, read off the artifact the gates wrote.

    Read, not decided. Every value here was set by `authorize`, `reconcile` or the
    identity/resource layers; the controller's only contribution is to surface it on the
    case so a batch summary can say which papers abstained and why.
    """
    if result is None:
        return "not_attempted"
    rec = result.reconciliation
    if rec is not None and rec.status in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION"):
        return rec.status
    if rec is not None and (rec.failure_class or "") not in ("", "none"):
        return rec.failure_class
    auth = result.authorization
    if auth is not None and not auth.allowed:
        return auth.failure_class or auth.decision
    return result.verdict or "inconclusive"


def _phase_probe(cfg: Config, case: CaseState, *, force_probe: bool = False,
                 skip_probe: bool = False, **_) -> PhaseOutcome:
    """Request reproduction. Whether anything runs is settled below this function.

    The controller asks; `plan_execution` and `backends.authorize` decide. Every way this
    can end short of a measurement is an ABSTENTION, never an error: a missing repository,
    an unidentified experiment, an unbound metric, an environment that cannot host the
    experiment and a shut gate are all facts about the runner or about what could be
    proven, and none of them is a reason to abandon the review.
    """
    doc_path = state.project_dir(cfg, case.paper_id) / "paper" / "doc.json"
    doc = PaperDoc(**state.read_json(doc_path))
    root = state.project_dir(cfg, case.paper_id)
    done = (root / "runs" / case.paper_id / "probe_results.json").exists()

    if skip_probe:
        return PhaseOutcome("abstain", "reproduction skipped by request (--skip-probe)",
                            {"skipped": "--skip-probe"}, "skipped")
    if done and not force_probe:
        existing = ProbeResult(**state.read_json(root / "runs" / case.paper_id / "probe_results.json"))
        return PhaseOutcome("ok", "reproduction already ran for this paper",
                            {"cached": True}, _reproduction_class(existing))

    reports, _ = audit_stage.load_reports(cfg, case.paper_id, doc)
    verifiable = [f for r in reports for f in r.findings if f.verifiable_by_experiment]
    if not verifiable and not force_probe:
        return PhaseOutcome("abstain", "no lens marked a finding as settleable by reproduction",
                            {"skipped": "nothing settleable"}, "nothing_settleable")

    try:
        res = probe_stage.run(cfg, case.paper_id)
    except Exception as e:                       # noqa: BLE001 — a crashed probe is not a crashed review
        return PhaseOutcome(
            "abstain",
            f"the reproduction stage raised {type(e).__name__}: {e}. Recorded as inconclusive; "
            f"a fault in this harness is not evidence about the paper.",
            {"error": f"{type(e).__name__}: {e}"}, "probe_error")

    result_path = root / "runs" / case.paper_id / "probe_results.json"
    result = ProbeResult(**state.read_json(result_path)) if result_path.exists() else None
    return PhaseOutcome("ok", res.get("reason", "") or "reproduction attempted", res,
                        _reproduction_class(result))


def _phase_report(cfg: Config, case: CaseState, **_) -> PhaseOutcome:
    res = report_stage.run_report(cfg, case.paper_id)
    if "error" in res:
        return PhaseOutcome("error", res["error"])
    case.verdict = res["verdict"]
    case.report_path = res["report_md"]
    return PhaseOutcome("ok", res["reason"], res)


_HANDLERS = {
    "ingest": _phase_ingest,
    "audit": _phase_audit,
    "collect": _phase_collect,
    "probe": _phase_probe,
    "report": _phase_report,
}


# --------------------------------------------------------------------------- #
# The machine
# --------------------------------------------------------------------------- #
def _next_phase(phase: str) -> str:
    i = PHASES.index(phase)
    return PHASES[min(i + 1, len(PHASES) - 1)]


def step(cfg: Config, case: CaseState, **opts) -> CaseState:
    """Advance one phase. Records exactly one `PhaseEvent`, whatever happens.

    Idempotent against a terminal case and safe to call on a `waiting` one — a waiting
    case re-attempts its phase, which is how a run resumes once the evidence it was
    waiting for has arrived.
    """
    if case.terminal:
        return case
    if case.phase == "done":
        case.status = "complete"
        return save_case(cfg, case)

    handler = _HANDLERS[case.phase]
    attempt = case.attempts.get(case.phase, 0) + 1
    case.attempts[case.phase] = attempt
    case.status = "running"

    try:
        out = handler(cfg, case, **opts)
    except Exception as e:                       # noqa: BLE001 — a handler fault is a case error, not a crash
        out = PhaseOutcome("error", f"{type(e).__name__}: {e}")

    case.history.append(PhaseEvent(
        phase=case.phase, outcome=out.outcome, reason=out.reason,
        detail=out.detail or {}, attempt=attempt,
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    if out.reproduction_class:
        case.reproduction_class = out.reproduction_class

    if out.outcome == "error":
        case.status, case.blocked_reason = "error", out.reason
    elif out.outcome == "waiting":
        case.status, case.blocked_reason = "waiting", out.reason
    elif out.outcome == "retry":
        # The phase stays where it is; `drive` will call this function again. Bounding is
        # the handler's job because only it knows what its own budget means.
        case.status, case.blocked_reason = "running", out.reason
        if case.phase not in RETRYABLE:
            case.status, case.blocked_reason = "waiting", (
                f"{case.phase} asked to retry, but only {', '.join(RETRYABLE)} may be "
                f"re-attempted: {out.reason}")
    else:                                        # ok | abstain
        case.blocked_reason = ""
        case.phase = _next_phase(case.phase)
        if case.phase == "done":
            case.status = "complete"

    return save_case(cfg, case)


def drive(cfg: Config, case: CaseState, *, max_steps: int = 40, **opts) -> CaseState:
    """Run `step` until the case is terminal or blocked on evidence it cannot obtain.

    A case that already finished is re-derived from `collect` rather than returned as-is.
    Collect, probe and report are idempotent and cheap to re-check — the probe guards
    itself with its own `already ran` test — so re-invoking `review` always produces a
    report from the current code and the current lens files, instead of handing back
    whatever happened to be on disk. `--force-probe` rewinds one phase further, which is
    the only way that flag can mean anything on a finished case.

    `max_steps` is a backstop against a handler that neither advances nor blocks. It is
    not a budget: the pipeline is six phases and a small retry allowance, so reaching it
    means something is wrong, and stopping is better than spinning.
    """
    if case.status == "complete":
        case = rewind(case, "probe" if opts.get("force_probe") else "collect")
    for _ in range(max_steps):
        before = (case.phase, case.status, case.attempts.get(case.phase, 0))
        case = step(cfg, case, **opts)
        if case.terminal or case.status == "waiting":
            return case
        if (case.phase, case.status, case.attempts.get(case.phase, 0)) == before:
            case.status = "waiting"
            case.blocked_reason = f"the '{case.phase}' phase made no progress"
            return save_case(cfg, case)
    case.status = "waiting"
    case.blocked_reason = f"stopped after {max_steps} steps without reaching a terminal state"
    return save_case(cfg, case)


def drive_all(cfg: Config, sources: list[str], **opts) -> list[CaseState]:
    """Advance several papers independently, interleaved.

    Round-robin rather than one-paper-at-a-time so a paper that blocks — waiting on lens
    evidence, or on a reviewer that is slow — does not hold up the rest of the batch, and
    so an interrupted run leaves every paper partly advanced rather than the first three
    finished and the last three untouched.

    Cases share nothing. Every artifact path is derived from that paper's own id, and one
    paper reaching `error` has no effect on any other — a bad PDF is a fact about that
    PDF.
    """
    cases = [open_case(cfg, s) for s in sources]
    for _ in range(len(PHASES) * (2 + max(0, cfg.audit_retries)) + 4):
        active = [c for c in cases if not c.terminal and c.status != "waiting"]
        if not active:
            break
        for i, case in enumerate(cases):
            if case.terminal or case.status == "waiting":
                continue
            cases[i] = step(cfg, case, **opts)
    return cases


def summarize(cases: list[CaseState]) -> dict:
    """The batch, as a caller sees it. Pure — reads the cases, decides nothing."""
    return {
        "papers": len(cases),
        "complete": [c.paper_id for c in cases if c.status == "complete"],
        "waiting": {c.paper_id or c.source: c.blocked_reason
                    for c in cases if c.status == "waiting"},
        "errors": {c.paper_id or c.source: c.blocked_reason for c in cases if c.status == "error"},
        "verdicts": {c.paper_id: c.verdict for c in cases if c.verdict},
        "reproduction": {c.paper_id: c.reproduction_class
                         for c in cases if c.reproduction_class},
    }


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
_STAGE_LABEL = {"ingest": "S1 ingest", "audit": "S2 audit", "collect": "S2 verify",
                "probe": "S3 verify", "report": "S4 report"}


def _detail(case: CaseState, phase: str) -> dict:
    for e in reversed(case.history):
        if e.phase == phase:
            return e.detail or {}
    return {}


def as_result(cfg: Config, case: CaseState) -> dict:
    """One `CaseState` as the flat dict the CLI and the dossier consume."""
    steps = [{"stage": _STAGE_LABEL.get(e.phase, e.phase), "outcome": e.outcome,
              "attempt": e.attempt, "reason": e.reason, **(e.detail or {})}
             for e in case.history]
    if case.status == "error":
        return {"status": "error", "paper_id": case.paper_id or None,
                "error": case.blocked_reason, "steps": steps}

    doc_path = state.project_dir(cfg, case.paper_id) / "paper" / "doc.json"
    title = str(state.read_json(doc_path).get("title") or "") if doc_path.exists() else ""

    if case.status != "complete":
        prompts = _detail(case, "audit").get("prompts") or {}
        return {"status": "needs_audit", "paper_id": case.paper_id, "title": title,
                "steps": steps, "awaiting": list(case.awaiting),
                "prompts": {ln: prompts[ln] for ln in case.awaiting if ln in prompts},
                "blocked_reason": case.blocked_reason,
                "next": (f"Write projects/{case.paper_id}/audit/<lens>.json for each pending "
                         f"lens, one lens per turn, then re-run. Or pass --auto-audit to let "
                         f"the controller delegate them.")}

    synth, collected = _detail(case, "report"), _detail(case, "collect")
    return {"status": "complete", "paper_id": case.paper_id, "title": title,
            "verdict": case.verdict, "reason": synth.get("reason", ""),
            "findings": synth.get("findings", collected.get("findings", 0)),
            "dropped_unsubstantiated": synth.get("dropped_unsubstantiated",
                                                 collected.get("dropped", 0)),
            "probe": synth.get("probe"), "reproduction": case.reproduction_class or None,
            "report_md": case.report_path, "steps": steps}


def review(cfg: Config, paper: str, **opts) -> dict:
    """Review one paper as far as it can go. Safe to call repeatedly."""
    return as_result(cfg, drive(cfg, open_case(cfg, paper), **opts))


def review_papers(cfg: Config, papers: list[str], *, dossier_out: Path | None = None,
                  **opts) -> dict:
    """Review a batch, interleaved, then consolidate whatever finished into one dossier.

    One paper failing does not stop the batch: a bad PDF or a half-written lens file is a
    fact about that paper, not a reason to abandon the others. Papers that did not finish
    are still handed to the dossier, which lists them as missing — a summary that omits
    its failures is worse than no summary.
    """
    from . import dossier as dossier_mod

    cases = drive_all(cfg, papers, **opts)
    results = []
    for paper, case in zip(papers, cases):
        r = as_result(cfg, case)
        r["input"] = paper
        results.append(r)

    out = {**summarize(cases), "results": results,
           "needs_audit": {c.paper_id: c.awaiting for c in cases if c.status == "waiting"}}
    ordered = [c.paper_id for c in cases if c.paper_id]
    if ordered:
        out["dossier"] = dossier_mod.build(cfg, ordered, dossier_out)
    return out


if __name__ == "__main__":  # self-check: python -m harness.controller
    import json
    import tempfile

    from .artifacts import Reconciliation

    # --- phase ordering -----------------------------------------------------------------
    assert PHASES == ("ingest", "audit", "collect", "probe", "report", "done")
    assert _next_phase("ingest") == "audit" and _next_phase("done") == "done"
    assert RETRYABLE == ("audit",), "only a delegated lens may be re-attempted"

    # --- reproduction class is READ from the artifact, never decided here ---------------
    assert _reproduction_class(None) == "not_attempted"
    verified = ProbeResult(paper_id="p", reconciliation=Reconciliation(status="RESOLVED_VERIFIED"))
    assert _reproduction_class(verified) == "RESOLVED_VERIFIED"
    failed = ProbeResult(paper_id="p", reconciliation=Reconciliation(status="FAILED_REPRODUCTION"))
    assert _reproduction_class(failed) == "FAILED_REPRODUCTION"
    blocked = ProbeResult(paper_id="p", verdict="blocked", reconciliation=Reconciliation(
        status="INCONCLUSIVE", failure_class="resources_insufficient"))
    assert _reproduction_class(blocked) == "resources_insufficient"

    # --- a case that cannot be ingested is an error, and only that -----------------------
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td))
        case = drive(cfg, open_case(cfg, "not-a-real-case-id"))
        assert case.status == "error", case.status
        assert case.terminal and "neither a PDF path" in case.blocked_reason
        assert [e.phase for e in case.history] == ["ingest"]
        assert case.attempts == {"ingest": 1}

    # --- the machine records every attempt ----------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td))
        a, b = drive_all(cfg, ["nope-one", "nope-two"])
        assert a.status == b.status == "error", (a.status, b.status)
        assert a.paper_id != b.paper_id, "cases must not share identity"
        s = summarize([a, b])
        assert s["papers"] == 2 and len(s["errors"]) == 2 and not s["complete"]
        print(json.dumps(s, indent=2))

    print("controller self-check OK")
