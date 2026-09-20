"""The orchestration loop, as a program rather than a convention — was controller.py
(1006 lines) + preflight.py (237) + corpus.py (126).

Before this module, `review()` ran the deterministic stages, hit the audit stage, returned
`status="needs_audit"`, and the process ended. Resuming depended on an actor outside the
repository reading prose in CLAUDE.md and re-invoking the CLI. This module is the
workflow: sequence phases, decide what to attempt next from persisted state, re-attempt a
phase whose failure was transient, and record everything it observed. That is the whole
list.

**What it may not do, and cannot.** It cannot authorize an execution, select a backend,
set an identity state, decide a resource sufficiency, or write a reconciliation status.
Those live below it in `execute.authorize`/`execute.reconcile`, and this module only ever
*requests* the phase that consults them and *records the answer it got*.

**Abstention is not failure.** A paper with no repository, an ambiguous experiment, a
24 GiB requirement on an 8 GiB card, or a backend that cannot be provisioned still gets a
complete review — the reproduction abstains, `CaseState.reproduction_class` names why, and
the case reaches `complete` with a report.

**Retries are bounded, recorded, and rare.** Precisely two things are retryable: a lens
whose reviewer failed, and a grade whose reviewer failed. A timeout or a truncated JSON
object is transient and the same prompt may well succeed on a second attempt. Nothing else
is — an identity, resource, commit, capability or authorization refusal is deterministic.

`python -m harness.pipeline` runs the self-check.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import agent, assessment, decide, discover, routes, state
from .audit import (attach, coverage as grade_coverage_fn, load_reports, missing_reading_artifacts,
                    reading_record, run_audit, run_grade)
from .config import Config
from .prompts import verdict as verdict_prompts
from .report import (CATEGORY_ORDER, assemble_report, build_ledger, counted, rank,
                     render_eval_report, render_reviewer_report)
from .schema import (CORPUS_STATES, PHASES, CaseState, CorpusEntry, CorpusReport, PaperDoc,
                     PhaseEvent, ProbeResult, ReadingRecord)
from .stages import ingest as ingest_stage

# Only these phases may be re-attempted, and only up to their own retry budget
# (`cfg.audit_retries` / `cfg.grade_retries`). Everything else that can refuse is
# deterministic, and retrying a deterministic refusal is asking a gate the same question
# until it changes its mind.
RETRYABLE = ("audit", "grade")
# How many dispatch rounds one audit phase may run. Two is what the design needs — the
# parts, then each lens's synthesis over its own parts — and the third is slack for a
# reading that failed in the first round and succeeded in the second. Not a budget: the
# loop already stops the moment a round fills nothing.
_MAX_AUDIT_ROUNDS = 3


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

    Without this a `complete` case is terminal and re-running `review` on it hands back
    whatever report was on disk — including after the code that produced it has changed.
    The history is kept: a case re-derived twice should show both runs.
    """
    if PHASES.index(phase) >= PHASES.index(case.phase) and case.phase != "done":
        return case
    case.phase, case.status, case.blocked_reason = phase, "running", ""
    case.attempts = {k: v for k, v in case.attempts.items()
                     if PHASES.index(k) < PHASES.index(phase)}
    return case


def is_new_pdf_source(source: str) -> bool:
    """Is `source` a not-yet-ingested PDF path, rather than an already-ingested case id?"""
    src = Path(source).expanduser()
    return src.suffix.lower() == ".pdf" or src.exists()


def open_case(cfg: Config, source: str) -> CaseState:
    """The case for one input — a PDF path or an already-ingested id. Resumes if it exists."""
    if not is_new_pdf_source(source):
        existing = load_case(cfg, source)
        if existing:
            return existing
        return CaseState(paper_id=source, source=source, phase="ingest")
    return CaseState(source=str(Path(source).expanduser()), phase="ingest")


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
def _phase_ingest(cfg: Config, case: CaseState, **_) -> PhaseOutcome:
    src = Path(case.source).expanduser()
    if not is_new_pdf_source(case.source):
        pid = case.paper_id or case.source
        if not (state.project_dir(cfg, pid) / "paper" / "doc.json").exists():
            case.failure_kind, case.retry_policy = "bad_invocation", "never"
            return PhaseOutcome("error", f"'{case.source}' is neither a PDF path nor an "
                                         f"already-ingested case id")
        case.paper_id = pid
        return PhaseOutcome("ok", "resumed from an existing ingest", {"cached": True})
    try:
        res = ingest_stage.run_ingest(cfg, str(src))
    except FileNotFoundError as e:
        case.failure_kind, case.retry_policy = "extraction_failed", "never"
        return PhaseOutcome("error", str(e))
    case.paper_id = res["paper_id"]
    case.content_sha = res.get("content_sha", "") or ""
    return PhaseOutcome("ok", f"ingested {res.get('sections', 0)} section(s), "
                              f"{res.get('tables', 0)} table(s)", res)


def _record_failure(case: CaseState, kinds: dict) -> None:
    """Persist WHY a delegation failed, worst policy first, onto the case."""
    if not kinds:
        return
    order = {"never": 0, "later": 1, "now": 2}
    worst = min(kinds.values(), key=lambda k: order.get(k.get("retry", "now"), 3))
    case.failure_kind = worst.get("kind", "") or case.failure_kind
    case.retry_policy = worst.get("retry", "") or case.retry_policy


def _phase_audit(cfg: Config, case: CaseState, *, auto_audit: bool = False, **_) -> PhaseOutcome:
    """Render the lens prompts, then fill whatever the configured reviewer can fill."""
    res = run_audit(cfg, case.paper_id)
    if "error" in res:
        return PhaseOutcome("error", res["error"])
    case.awaiting = list(res["awaiting"])
    deferred = list(res.get("deferred") or [])
    if not case.awaiting and not deferred:
        return PhaseOutcome("ok", "every reading already has a result",
                            {"complete": res["complete"]})

    if not auto_audit:
        pending = case.awaiting + deferred
        return PhaseOutcome(
            "waiting",
            f"{len(pending)} reading(s) have no result: {', '.join(pending)}. "
            f"Prompts are in audit/prompts/; run with --auto-audit to delegate them."
            + (f" {len(deferred)} of them is a cross-part synthesis whose prompt is written "
               f"once its own parts are in." if deferred else ""),
            {"awaiting": case.awaiting, "deferred": deferred, "prompts": res["prompts"]})

    ok, why = agent.available(cfg, "lens")
    if not ok:
        return PhaseOutcome("waiting", f"cannot delegate the audit: {why}",
                            {"awaiting": case.awaiting})

    meta = state.load_meta(cfg, case.paper_id)
    pdf_path = meta.get("paper_path") or ""
    pdf_dir = str(Path(pdf_path).resolve().parent) if pdf_path else ""

    filled = {"filled": [], "failed": {}, "rate_limited": {}, "blocked": {}, "kinds": {}}
    after = res
    for _ in range(_MAX_AUDIT_ROUNDS):
        pending = list(after.get("awaiting") or [])
        if not pending:
            break
        round_result = agent.fill_lenses(cfg, case.paper_id, pending, after["prompts"],
                                         pdf_dir=pdf_dir, units=after.get("units"))
        filled["filled"] += round_result["filled"]
        for bucket in ("failed", "rate_limited", "blocked", "kinds"):
            filled[bucket].update(round_result.get(bucket) or {})
        after = run_audit(cfg, case.paper_id)
        if not round_result["filled"]:
            break
        done_now = set(after.get("complete") or [])
        for bucket in ("failed", "rate_limited", "blocked", "kinds"):
            filled[bucket] = {k: v for k, v in filled[bucket].items() if k not in done_now}

    case.awaiting = list(after.get("awaiting", [])) + list(after.get("deferred") or [])
    detail = {"filled": filled["filled"], "failed": filled["failed"],
              "rate_limited": filled["rate_limited"], "blocked": filled.get("blocked", {}),
              "kinds": filled.get("kinds", {}), "awaiting": case.awaiting,
              "parts": after.get("parts"),
              "reader_visible_fraction": after.get("reader_visible_fraction")}
    _record_failure(case, filled.get("kinds", {}))
    if not case.awaiting:
        return PhaseOutcome("ok", f"delegated and filled {len(filled['filled'])} reading(s) "
                                  f"across {after.get('parts', 1)} part(s) of the paper", detail)

    unretryable = {**filled["rate_limited"], **filled.get("blocked", {})}
    if not (unretryable or filled["failed"]):
        return PhaseOutcome("waiting", f"{len(case.awaiting)} reading(s) still to run: "
                                       f"{', '.join(case.awaiting)}", detail)
    if unretryable:
        case.attempts["audit"] = max(0, case.attempts.get("audit", 1) - 1)
        hints = [v.get("reset_hint", "") for v in filled.get("kinds", {}).values()]
        case.resume_after = next((h for h in hints if h), "") or case.resume_after
        why = "; ".join(f"{k}: {v}" for k, v in unretryable.items())
        later = bool(filled["rate_limited"])
        return PhaseOutcome(
            "waiting",
            f"{len(unretryable)} lens(es) blocked by a {case.failure_kind or 'non-retryable'} "
            f"failure, not by reviewer quality — "
            + ("re-run once available" if later else "this needs an operator fix, not a retry")
            + (f" ({case.resume_after})" if case.resume_after and later else "") + f": {why}",
            detail)

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
    """Load every lens result and verify its evidence. The gate the LLM cannot pass."""
    doc_path = state.project_dir(cfg, case.paper_id) / "paper" / "doc.json"
    if not doc_path.exists():
        return PhaseOutcome("error", "no ingested paper to verify findings against")
    doc = PaperDoc(**state.read_json(doc_path))
    reports, dropped, invalid = load_reports(cfg, case.paper_id, doc)
    if not reports:
        return PhaseOutcome("error", "no audit lens produced a result, so there is nothing "
                                     "to review; the paper has not been audited")
    if invalid:
        return PhaseOutcome(
            "waiting",
            f"{len(invalid)} lens(es) produced no usable result and cannot be counted as "
            f"run: {', '.join(invalid)}. A verdict may not be drawn over an incomplete or "
            f"invalid panel.",
            {"invalid": invalid, "lenses": [r.lens for r in reports]})
    kept = sum(len(r.findings) for r in reports)
    return PhaseOutcome("ok", f"{len(reports)} lens(es), {kept} substantiated finding(s), "
                              f"{dropped} dropped as unsubstantiated",
                        {"lenses": [r.lens for r in reports], "findings": kept,
                         "dropped": dropped})


def _reproduction_class(result: ProbeResult | None) -> str:
    """Why reproduction did not conclude, read off the artifact the gates wrote."""
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


def _phase_grade(cfg: Config, case: CaseState, *, auto_grade: bool = False, **_) -> PhaseOutcome:
    """Independently grade whatever candidates are in scope. An unavailable, exhausted, or
    never-requested grader returns `ok`, not `waiting` — findings simply count at their
    lens-asserted severity unless `SH_REQUIRE_GRADES` says otherwise."""
    res = run_grade(cfg, case.paper_id)
    if "error" in res:
        return PhaseOutcome("ok", res["error"])
    awaiting = list(res["awaiting"])
    if not awaiting:
        return PhaseOutcome("ok", f"{res['candidates']} candidate(s) in scope, all graded",
                            {"complete": res["complete"], "candidates": res["candidates"]})

    if not auto_grade:
        if cfg.require_grades:
            return PhaseOutcome(
                "waiting",
                f"{len(awaiting)} candidate(s) ungraded and SH_REQUIRE_GRADES is set: run "
                f"with --auto-grade to delegate them.", {"awaiting": awaiting})
        return PhaseOutcome(
            "ok", f"grading not requested; {len(awaiting)} candidate(s) will count at "
                 f"their lens-asserted severity", {"awaiting": awaiting})

    ok, why = agent.available(cfg, "grade")
    if not ok:
        if cfg.require_grades:
            return PhaseOutcome("waiting", f"cannot delegate grading: {why}", {"awaiting": awaiting})
        return PhaseOutcome(
            "ok", f"grader unavailable ({why}); {len(awaiting)} candidate(s) will count "
                 f"at their lens-asserted severity", {"awaiting": awaiting})

    filled = agent.fill_grades(cfg, case.paper_id, awaiting, res["prompts"])
    after = run_grade(cfg, case.paper_id)
    still_awaiting = list(after.get("awaiting", []))
    detail = {"filled": filled["filled"], "failed": filled["failed"],
              "rate_limited": filled["rate_limited"], "blocked": filled.get("blocked", {}),
              "kinds": filled.get("kinds", {}), "awaiting": still_awaiting}
    _record_failure(case, filled.get("kinds", {}))
    if not still_awaiting:
        return PhaseOutcome("ok", f"delegated and graded {len(filled['filled'])} candidate(s)", detail)

    unretryable = {**filled["rate_limited"], **filled.get("blocked", {})}
    if unretryable:
        case.attempts["grade"] = max(0, case.attempts.get("grade", 1) - 1)
        why = "; ".join(f"{k}: {v}" for k, v in unretryable.items())
        outcome = "waiting" if cfg.require_grades else "ok"
        return PhaseOutcome(outcome, f"{len(unretryable)} candidate(s) blocked by a "
                                     f"{case.failure_kind or 'non-retryable'} failure, not by "
                                     f"grader quality: {why}", detail)

    attempts = case.attempts.get("grade", 1)
    why = "; ".join(f"{k}: {v}" for k, v in filled["failed"].items())
    if attempts <= max(0, cfg.grade_retries):
        return PhaseOutcome(
            "retry", f"{len(still_awaiting)} candidate(s) still pending after attempt "
                    f"{attempts} of {cfg.grade_retries + 1}: {why}", detail)
    if cfg.require_grades:
        return PhaseOutcome(
            "waiting", f"{len(still_awaiting)} candidate(s) could not be graded in "
                      f"{attempts} attempt(s): {why}", detail)
    return PhaseOutcome(
        "ok", f"{len(still_awaiting)} candidate(s) ungraded after {attempts} attempt(s); "
             f"they will count at their lens-asserted severity", detail)


def _phase_assess(cfg: Config, case: CaseState, **_) -> PhaseOutcome:
    """Has the paper's own evidence already settled it? Asked BEFORE anything expensive.

    Pure and model-free, over the kept findings alone. What a True answer stops is the
    INVESTIGATION BRANCH — acquisition, static audit, identity, resources, execution. It
    does not stop the review itself: discovery, the report, the ledger and the four pure
    layers all still run, because the referee record has to be complete whatever the
    outcome. See `harness/assessment.py`.
    """
    doc_path = state.project_dir(cfg, case.paper_id) / "paper" / "doc.json"
    if not doc_path.exists():
        return PhaseOutcome("error", "no ingested paper to assess findings against")
    doc = PaperDoc(**state.read_json(doc_path))
    reports, _dropped, _invalid = load_reports(cfg, case.paper_id, doc)
    # Grades are attached here rather than read raw, because the gate reads COUNTED
    # severity: a lens-asserted FATAL the blinded grader demoted must not stop a paper.
    attach(cfg, case.paper_id, doc, reports)
    findings = [f for r in reports for f in r.findings]

    case.assessment = assessment.assess(findings)
    established = case.assessment.material_failure_established
    return PhaseOutcome(
        "ok",
        ("a material failure is already established from the paper itself; the "
         "investigation branch will not run. " + case.assessment.reason)
        if established else case.assessment.reason,
        {"material_failure_established": established,
         "basis": case.assessment.basis,
         "counted_fatal": list(case.assessment.counted_fatal_ids)})


def _phase_discover(cfg: Config, case: CaseState, **_) -> PhaseOutcome:
    """Ask what this paper offers to check, before anything expensive happens.

    Deterministic and model-free. A paper with nothing addressable still produces a target
    set, whose `extraction_coverage` says how much of the paper was reachable at all.
    """
    res = discover.run(cfg, case.paper_id,
                       investigation_open=assessment.investigation_open(case.assessment))
    if "error" in res:
        return PhaseOutcome("error", res["error"])
    return PhaseOutcome(
        "ok",
        f"{res['objects']} object(s) discovered, {res['addressable']} addressable; "
        f"{res['resolved_without_execution']} resolved without execution, "
        f"{res['requires_execution']} would require one",
        res)


def _phase_probe(cfg: Config, case: CaseState, *, force_probe: bool = False,
                 skip_probe: bool = False, **_) -> PhaseOutcome:
    """Request reproduction. Whether anything runs is settled below this function — the
    controller asks; `routes.plan_execution` and `execute.authorize` decide. Every way this
    can end short of a measurement is an ABSTENTION, never an error.
    """
    doc_path = state.project_dir(cfg, case.paper_id) / "paper" / "doc.json"
    doc = PaperDoc(**state.read_json(doc_path))
    root = state.project_dir(cfg, case.paper_id)
    done = (state.control_dir(root) / "probe_results.json").exists()

    if skip_probe:
        return PhaseOutcome("abstain", "reproduction skipped by request (--skip-probe)",
                            {"skipped": "--skip-probe"}, "skipped")
    if done and not force_probe:
        existing = ProbeResult(**state.read_json(state.control_dir(root) / "probe_results.json"))
        routes.resync_cached_outcomes(cfg, case.paper_id)
        return PhaseOutcome("ok", "reproduction already ran for this paper",
                            {"cached": True}, _reproduction_class(existing))

    ts = discover.load(cfg, case.paper_id)
    executable = [p for p in ts.plans if p.requires_execution] if ts else []
    no_execution_justified = ts is not None and not executable and not force_probe
    if no_execution_justified:
        resolved = ts.extraction_coverage.get("targets_resolved_without_execution", 0)
        blocked = ts.extraction_coverage.get("targets_blocked_before_execution", 0)
        detail = {"skipped": "no execution justified", "objects": len(ts.objects),
                  "resolved_without_execution": resolved,
                  "blocked_before_execution": blocked}
        reason = (f"no target justified an execution: {len(ts.objects)} object(s) "
                  f"discovered, {resolved} settled against the paper itself, {blocked} "
                  f"blocked before execution by specification, artifact or environment")
        try:
            reading = routes.run(cfg, case.paper_id)
        except Exception as e:                   # noqa: BLE001 — never an error
            detail["reading_routes_error"] = f"{type(e).__name__}: {e}"
            return PhaseOutcome("abstain", reason + (
                f". The non-executing routes raised {type(e).__name__} and were recorded "
                f"as inconclusive."), detail, "no_execution_justified")
        for key in ("artifact_targets", "artifact_facts", "literature_targets",
                    "literature_works", "literature_concerns", "validation_designed",
                    "validation_specification_blocked", "validation_launched"):
            if key in reading:
                detail[key] = reading[key]
        return PhaseOutcome("abstain", reason + (
            ". The routes that do not execute still ran: static artifact inspection."),
            detail, "no_execution_justified")
    if ts is None:
        reports, _, _ = load_reports(cfg, case.paper_id, doc)
        verifiable = [f for r in reports for f in r.findings if f.verifiable_by_experiment]
        if not verifiable and not force_probe:
            return PhaseOutcome("abstain", "no lens marked a finding as settleable by reproduction",
                                {"skipped": "nothing settleable"}, "nothing_settleable")

    try:
        res = routes.run(cfg, case.paper_id)
    except Exception as e:                       # noqa: BLE001 — a crashed probe is not a crashed review
        return PhaseOutcome(
            "abstain",
            f"the reproduction stage raised {type(e).__name__}: {e}. Recorded as inconclusive; "
            f"a fault in this harness is not evidence about the paper.",
            {"error": f"{type(e).__name__}: {e}"}, "probe_error")

    result_path = state.control_dir(root) / "probe_results.json"
    result = ProbeResult(**state.read_json(result_path)) if result_path.exists() else None
    return PhaseOutcome("ok", res.get("reason", "") or "reproduction attempted", res,
                        _reproduction_class(result))


def run_report_stage(cfg: Config, pid: str) -> dict:
    """Everything `report.assemble_report` needs is an already-produced artifact; this
    function is the I/O half `assemble_report`'s own docstring says a caller owns —
    reading those artifacts, dispatching the whole-paper verdict, and writing the four
    files a finished review consists of."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))

    reports, dropped, invalid = load_reports(cfg, pid, doc)
    if not reports:
        return {"error": f"no audit lenses have run for '{pid}' — run audit_paper first, "
                         f"then write audit/<lens>.json for each prompt"}
    if invalid:
        return {"error": f"{len(invalid)} lens(es) produced no usable result for '{pid}' and "
                         f"cannot be counted as run: {', '.join(invalid)}. Write a valid "
                         f"audit/<lens>.json for each, then re-run."}
    state.set_phase(cfg, pid, "report")

    probe_path = state.control_dir(root) / "probe_results.json"
    probe = ProbeResult(**state.read_json(probe_path)) if probe_path.exists() else None

    attach(cfg, pid, doc, reports)             # in-place: sets counted_severity etc.
    cov = grade_coverage_fn(cfg, pid)
    if "error" in cov:
        cov = {"paper_id": pid, "candidates": 0, "graded": 0, "pending": 0}

    target_set = discover.load(cfg, pid)

    # The whole-paper verdict prompt, built the same way regardless of whether anything
    # can dispatch it — a review with no reachable model still leaves an operator
    # everything they need, the same discipline `_phase_audit` already applies.
    for_prompt = rank([f for r in reports for f in r.findings])
    findings_summary = "\n".join(
        f"- [{counted(f)}] ({f.lens}) {f.title}: {f.statement}" for f in for_prompt[:30])
    withdrawn = [f for f in for_prompt
                 if f.finding_class in ("REFUTED", "OPEN_QUESTION")
                 or f.candidate_class in ("OPEN_QUESTION", "DISMISSED")]
    questions_summary = "\n".join(
        f"- [{f.finding_class}/{f.candidate_class or 'unsorted'}] {f.title}"
        for f in withdrawn[:20])
    grading_summary = f"{cov['graded']} of {cov['candidates']} candidate(s) independently graded."
    verdict_prompt = verdict_prompts.build(
        doc.title, findings_summary, grading_summary, probe.reason if probe else "",
        questions_summary)
    prompt_path = root / "reports" / "verdict_prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(verdict_prompt, encoding="utf-8")

    substantive = agent.load_verdict(cfg, pid)
    if substantive is None:
        substantive = agent.run_verdict(cfg, verdict_prompt, timeout_s=cfg.verdict_timeout_s,
                                        pid=pid)

    reading = ReadingRecord(**reading_record(cfg, pid, doc))

    report, case_ledger = assemble_report(
        pid=pid, title=doc.title, doc=doc, reports=reports, dropped_findings=dropped,
        probe=probe, target_set=target_set, grade_coverage=cov,
        substantive_verdict=substantive, reading_record=reading,
        probe_stage_seconds=0.0, cfg=cfg)

    # `assemble_report` calls `decide.refresh_route_attempts(target_set, cfg)` internally,
    # mutating it in place — persisted here so a resumed case sees the refreshed rows too.
    if target_set is not None:
        state.write_json(discover.targets_path(cfg, pid), target_set.model_dump())

    ledger_path = root / "reports" / f"{pid}.ledger.json"
    json_path = root / "reports" / f"{pid}.json"
    md_path = root / "reports" / f"{pid}.md"
    review_path = root / "reports" / f"{pid}.review.md"

    state.write_json(ledger_path, case_ledger.model_dump())
    state.write_json(json_path, report.model_dump())
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_eval_report(report), encoding="utf-8")
    review_path.write_text(render_reviewer_report(report, target_set), encoding="utf-8")

    classes: dict[str, int] = {}
    for sf in report.scientific_findings:
        classes[sf.scientific_class] = classes.get(sf.scientific_class, 0) + 1
    classes = {k: classes[k] for k in CATEGORY_ORDER if k in classes}

    state.append_log(
        cfg, pid, artifact_type="eval_report", phase="report",
        headers={"verdict": report.verdict, "claim_status": report.claim_status,
                 "reproduction_status": report.reproduction_status,
                 "review_path": report.review_path, "artifact_state": report.artifact_state,
                 "document_observations": len(report.document_observations),
                 "surface_addressed": (report.coverage.addressed if report.coverage else 0),
                 "surface_size": (report.coverage.surface_size if report.coverage else 0),
                 "guarantees_unmet": (list(report.guarantees.unmet)
                                      if report.guarantees else []),
                 "scientific_classes": classes,
                 "ran_as": report.execution_provenance, "findings": len(report.findings),
                 "dropped": dropped,
                 "severities": {s: sum(1 for f in report.findings if counted(f) == s)
                               for s in ("FATAL", "MAJOR", "MINOR", "NOTE")},
                 "lenses": report.lenses_run, "probe": probe.verdict if probe else None,
                 "grade_coverage": report.grade_coverage,
                 "verdict_contested": report.verdict_contested,
                 "self_audit_failed": report.self_audit.failed if report.self_audit else []},
        path=str(md_path),
    )
    return {"paper_id": pid, "verdict": report.verdict, "reason": report.verdict_reason,
            "scientific_classes": classes,
            "review_path": report.review_path, "artifact_state": report.artifact_state,
            "document_observations": len(report.document_observations),
            "guarantees_unmet": (list(report.guarantees.unmet) if report.guarantees else []),
            "triage": report.triage, "triage_reason": report.triage_reason,
            "disposition": report.disposition, "disposition_basis": report.disposition_basis,
            "disposition_reason": report.disposition_reason,
            "targets": report.targets_summary,
            "claim_status": report.claim_status, "reproduction_status": report.reproduction_status,
            "ran_as": report.execution_provenance,
            "findings": len(report.findings), "dropped_unsubstantiated": dropped,
            "lenses": report.lenses_run, "probe": probe.verdict if probe else None,
            "report_md": str(md_path), "report": f"reports/{pid}.json",
            "reviewer_report_md": str(review_path), "ledger": f"reports/{pid}.ledger.json",
            "verdict_contested": report.verdict_contested,
            "self_audit_complete": bool(report.self_audit and report.self_audit.complete),
            "self_audit_failed": report.self_audit.failed if report.self_audit else []}


def _phase_report(cfg: Config, case: CaseState, **_) -> PhaseOutcome:
    res = run_report_stage(cfg, case.paper_id)
    if "error" in res:
        return PhaseOutcome("error", res["error"])
    case.verdict = res.get("triage") or res["verdict"]
    case.disposition = res.get("disposition") or "NOT_REVIEWED"
    case.disposition_basis = res.get("disposition_basis") or "NONE"
    case.report_path = res["report_md"]
    return PhaseOutcome("ok", res.get("triage_reason") or res["reason"], res)


_HANDLERS = {
    "ingest": _phase_ingest,
    "audit": _phase_audit,
    "collect": _phase_collect,
    "grade": _phase_grade,
    "assess": _phase_assess,
    "discover": _phase_discover,
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
    """Advance one phase. Records exactly one `PhaseEvent`, whatever happens."""
    if case.terminal:
        return case
    with state.project_lock(cfg, case.paper_id):
        if case.phase == "done":
            case.status = "complete"
            return save_case(cfg, case)

        handler = _HANDLERS[case.phase]
        attempt = case.attempts.get(case.phase, 0) + 1
        case.attempts[case.phase] = attempt
        case.status = "running"

        try:
            out = handler(cfg, case, **opts)
        except Exception as e:                       # noqa: BLE001 — a handler fault is a case error
            out = PhaseOutcome("error", f"{type(e).__name__}: {e}")

        case.history.append(PhaseEvent(
            phase=case.phase, outcome=out.outcome, reason=out.reason,
            detail=out.detail or {}, attempt=attempt, ts=state.now()))
        if out.reproduction_class:
            case.reproduction_class = out.reproduction_class

        if out.outcome == "error":
            case.status, case.blocked_reason = "error", out.reason
        elif out.outcome == "waiting":
            case.status, case.blocked_reason = "waiting", out.reason
        elif out.outcome == "retry":
            case.status, case.blocked_reason = "running", out.reason
            if case.phase not in RETRYABLE:
                case.status, case.blocked_reason = "waiting", (
                    f"{case.phase} asked to retry, but only {', '.join(RETRYABLE)} may be "
                    f"re-attempted: {out.reason}")
        else:                                        # ok | abstain
            case.blocked_reason = ""
            case.phase = _next_phase(case.phase)
            if case.phase == "done":
                missing = missing_reading_artifacts(cfg, case.paper_id)
                if missing:
                    case.phase, case.status = "audit", "waiting"
                    case.blocked_reason = (
                        f"{len(missing)} required reading artifact(s) are missing, so this "
                        f"review cannot be completed as the one it describes: "
                        f"{', '.join(missing[:8])}"
                        + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""))
                    case.history.append(PhaseEvent(
                        phase="done", outcome="waiting", reason=case.blocked_reason,
                        detail={"missing": missing}, attempt=attempt, ts=state.now()))
                    return save_case(cfg, case)
                case.status = "complete"

        return save_case(cfg, case)


def drive(cfg: Config, case: CaseState, *, max_steps: int = 40, **opts) -> CaseState:
    """Run `step` until the case is terminal or blocked on evidence it cannot obtain."""
    if case.status == "complete":
        case = rewind(case, "discover" if opts.get("force_probe") else "collect")
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
    """Advance several papers independently, interleaved — round-robin so a paper that
    blocks does not hold up the rest of the batch."""
    resume_at = "discover" if opts.get("force_probe") else "collect"
    cases = []
    for s in sources:
        case = open_case(cfg, s)
        cases.append(rewind(case, resume_at) if case.status == "complete" else case)
    for _ in range(len(PHASES) * (2 + max(0, cfg.audit_retries, cfg.grade_retries)) + 4):
        active = [c for c in cases if not c.terminal and c.status != "waiting"]
        if not active:
            break
        for i, case in enumerate(cases):
            if case.terminal or case.status == "waiting":
                continue
            cases[i] = step(cfg, case, **opts)
    return cases


def summarize(cases: list[CaseState]) -> dict:
    """The batch, as a caller sees it. Pure — reads the cases, decides nothing.

    NOT the authoritative accounting: `account` below is, built from the REQUEST list
    rather than the cases that happen to exist.
    """
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
                "assess": "S2 assess", "probe": "S3 verify", "report": "S4 report"}


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
                "error": case.blocked_reason, "steps": steps,
                "failure_kind": case.failure_kind, "retry_policy": case.retry_policy}

    doc_path = state.project_dir(cfg, case.paper_id) / "paper" / "doc.json"
    title = str(state.read_json(doc_path).get("title") or "") if doc_path.exists() else ""

    if case.status != "complete":
        prompts = _detail(case, "audit").get("prompts") or {}
        return {"status": "needs_audit", "paper_id": case.paper_id, "title": title,
                "steps": steps, "awaiting": list(case.awaiting),
                "prompts": {ln: prompts[ln] for ln in case.awaiting if ln in prompts},
                "blocked_reason": case.blocked_reason,
                "failure_kind": case.failure_kind, "retry_policy": case.retry_policy,
                "resume_after": case.resume_after,
                "next": (f"Write projects/{case.paper_id}/audit/<lens>.json for each pending "
                         f"lens, one lens per turn, then re-run. Or pass --auto-audit to let "
                         f"the pipeline delegate them.")}

    synth, collected = _detail(case, "report"), _detail(case, "collect")
    return {"status": "complete", "paper_id": case.paper_id, "title": title,
            "verdict": case.verdict, "reason": synth.get("reason", ""),
            "findings": synth.get("findings", collected.get("findings", 0)),
            "dropped_unsubstantiated": synth.get("dropped_unsubstantiated",
                                                 collected.get("dropped", 0)),
            "probe": synth.get("probe"), "reproduction": case.reproduction_class or None,
            "report_md": case.report_path, "steps": steps,
            "verdict_contested": bool(synth.get("verdict_contested"))}


def review(cfg: Config, paper: str, **opts) -> dict:
    """Review one paper as far as it can go. Safe to call repeatedly."""
    return as_result(cfg, drive(cfg, open_case(cfg, paper), **opts))


# --------------------------------------------------------------------------- #
# Preflight — was preflight.py. Is this batch N distinct papers, before anything is spent?
# --------------------------------------------------------------------------- #
PREFLIGHT_STATES = ("NEW", "RESUMES", "DISAMBIGUATED", "DUPLICATE_REQUEST", "UNREADABLE")
BLOCKING_STATES = ("DUPLICATE_REQUEST", "UNREADABLE")


def _recorded_sha(cfg: Config, pid: str) -> str | None:
    doc = cfg.projects_dir / pid / "paper" / "doc.json"
    if not doc.exists():
        return None
    try:
        return str(state.read_json(doc).get("content_sha") or "")
    except (OSError, ValueError):
        return ""


def inspect_paper(cfg: Config, src: Path, seen: dict[str, Path]) -> dict:
    """What will happen to this one file, without ingesting it. `seen` is mutated so the
    SECOND appearance of one document is reported as a duplicate."""
    try:
        sha = ingest_stage.content_sha(src)
    except (OSError, ValueError) as exc:
        return {"path": str(src), "paper_id": "", "content_sha": "",
                "state": "UNREADABLE", "detail": f"could not hash the file: {exc}"}

    slug = ingest_stage.paper_id_for(src)
    if sha in seen:
        return {"path": str(src), "paper_id": "", "content_sha": sha,
                "state": "DUPLICATE_REQUEST",
                "detail": (f"byte-identical to {seen[sha].name}, already in this request. "
                           f"Reviewing it twice would report two papers and produce one "
                           f"review.")}
    seen[sha] = src

    pid, same = ingest_stage.allocate_paper_id(cfg, src, sha)
    recorded = _recorded_sha(cfg, pid)
    if pid != slug:
        st = "DISAMBIGUATED"
        detail = (f"the readable slug '{slug}' is held by a DIFFERENT document, so this "
                  f"paper is '{pid}'. Two papers, two ids, nothing shared.")
    elif recorded is None:
        st, detail = "NEW", f"no project exists for '{pid}'; it will be created"
    elif same:
        st = "RESUMES"
        detail = (f"the same document as the existing project '{pid}' "
                  f"(content_sha {sha}); that review will resume rather than restart. "
                  f"It is ONE paper in the corpus, not two.")
    else:
        st, detail = "NEW", f"'{pid}' exists and holds this document's first ingest"
    return {"path": str(src), "paper_id": pid, "content_sha": sha, "state": st, "detail": detail}


def preflight_check(cfg: Config, sources: list[str | Path]) -> dict:
    """The whole preflight for one requested batch. `distinct_documents` is the number a
    corpus claim may use — counted from content_sha, not from the length of the request."""
    seen: dict[str, Path] = {}
    entries = [inspect_paper(cfg, Path(s), seen) for s in sources]
    blocking = [e for e in entries if e["state"] in BLOCKING_STATES]
    ids = [e["paper_id"] for e in entries if e["paper_id"]]
    return {
        "requested": len(entries),
        "distinct_documents": len({e["content_sha"] for e in entries if e["content_sha"]}),
        "distinct_paper_ids": len(set(ids)),
        "entries": entries,
        "blocking": [f"{Path(e['path']).name}: {e['detail']}" for e in blocking],
        "ok": not blocking,
        "resumes": [e["paper_id"] for e in entries if e["state"] == "RESUMES"],
        "new": [e["paper_id"] for e in entries if e["state"] == "NEW"],
        "disambiguated": [e["paper_id"] for e in entries if e["state"] == "DISAMBIGUATED"],
    }


def render_preflight(result: dict) -> str:
    L = ["# Corpus preflight", "",
         f"{result['requested']} file(s) requested · "
         f"{result['distinct_documents']} distinct document(s) · "
         f"{result['distinct_paper_ids']} distinct paper id(s)", "",
         "| file | paper id | content sha | state |", "|---|---|---|---|"]
    for e in result["entries"]:
        L.append(f"| `{Path(e['path']).name}` | `{e['paper_id'] or '—'}` | "
                 f"`{e['content_sha'] or '—'}` | {e['state']} |")
    L += ["", "## What each state means", ""]
    for e in result["entries"]:
        L.append(f"- `{Path(e['path']).name}` — **{e['state']}**: {e['detail']}")
    if result["blocking"]:
        L += ["", "## This batch is refused", ""]
        L += [f"- {b}" for b in result["blocking"]]
        L += ["", "A batch whose file count and document count disagree cannot support a "
                  "claim about how many papers were reviewed."]
    else:
        L += ["", f"Every requested file is a distinct document. A claim about "
                  f"{result['distinct_documents']} paper(s) is checkable from the "
                  f"`content_sha` column above."]
    L.append("")
    return "\n".join(L)


def run_preflight(cfg: Config, sources: list[str | Path], out: Path | None = None) -> dict:
    result = preflight_check(cfg, sources)
    out = out or (cfg.projects_dir.parent / "reports")
    out.mkdir(parents=True, exist_ok=True)
    state.write_json(out / "preflight.json", result)
    (out / "preflight.md").write_text(render_preflight(result), encoding="utf-8")
    result["paths"] = {"json": str(out / "preflight.json"), "md": str(out / "preflight.md")}
    return result


# --------------------------------------------------------------------------- #
# Corpus accounting — was corpus.py. Every requested paper, exactly one terminal state.
# --------------------------------------------------------------------------- #
_FROM_STATUS = {"complete": "completed", "waiting": "inconclusive", "error": "failed",
                "pending": "started", "running": "started"}


def _state_for(case: CaseState | None) -> tuple[str, str]:
    if case is None:
        return "requested", "no case was ever opened for this input"
    st = _FROM_STATUS.get(case.status, "started")
    if st == "completed":
        return st, case.verdict or "complete"
    return st, case.blocked_reason or f"status={case.status} at phase={case.phase}"


def account(requests: list[str], cases: list[CaseState | None]) -> CorpusReport:
    """The batch, request by request. `requests` is the authority on what was asked; a
    shorter or None-padded `cases` list is accounted for, not dropped, so a crash that
    loses a case still leaves that paper visible as `requested`."""
    padded = list(cases) + [None] * max(0, len(requests) - len(cases))
    entries: list[CorpusEntry] = []
    for source, case in zip(requests, padded):
        st, reason = _state_for(case)
        entries.append(CorpusEntry(
            source=source, paper_id=(case.paper_id if case else ""), state=st,
            reason=reason, verdict=(case.verdict if case else ""),
            phase=(case.phase if case else ""),
            reproduction_class=(case.reproduction_class if case else ""),
            failure_kind=(case.failure_kind if case else ""),
            resume_after=(case.resume_after if case else ""),
            report_path=(case.report_path if case else ""),
            disposition=(getattr(case, "disposition", "") or "NOT_REVIEWED"
                         if case and st == "completed" else "NOT_REVIEWED"),
            disposition_basis=(getattr(case, "disposition_basis", "") or "NONE"
                               if case and st == "completed" else "NONE"),
        ))

    by_state = {s: [e.source for e in entries if e.state == s] for s in CORPUS_STATES}
    report = CorpusReport(
        requested=len(requests), entries=entries,
        counts={s: len(v) for s, v in by_state.items()}, by_state=by_state,
        complete=all(e.state == "completed" for e in entries) and bool(entries))
    total = sum(report.counts.values())
    assert total == report.requested, (
        f"corpus accounting lost {report.requested - total} of {report.requested} "
        f"paper(s): {report.counts}")
    report.summary = (f"{report.requested} requested · "
                      + " · ".join(f"{n} {s}" for s, n in report.counts.items() if n))
    return report


# --------------------------------------------------------------------------- #
# The batch entry point
# --------------------------------------------------------------------------- #
def review_papers(cfg: Config, papers: list[str], *, dossier_out: Path | None = None,
                  **opts) -> dict:
    """Review a batch, interleaved, then consolidate whatever finished into one dossier.

    **THE BATCH IS COUNTED BEFORE IT IS SPENT.** `preflight_check` answers, from the PDFs'
    bytes, whether an N-file request is N distinct documents. Only a DUPLICATE DOCUMENT
    blocks here; two different papers that slugify to the same id proceed with their
    distinct ids, and nothing is spent when it refuses — the check reads bytes and
    allocates no case.
    """
    from . import summarize as summarize_mod

    pre = preflight_check(cfg, list(papers))
    duplicates = [e for e in pre["entries"] if e["state"] == "DUPLICATE_REQUEST"]
    if duplicates:
        blocking = [f"{Path(e['path']).name}: {e['detail']}" for e in duplicates]
        return {"requested": pre["requested"], "completed": 0, "failed": 0,
                "results": [], "needs_audit": {}, "preflight": pre,
                "error": ("this batch was refused before anything ran: "
                          + "; ".join(blocking) +
                          f". {pre['requested']} file(s) requested and "
                          f"{pre['distinct_documents']} distinct document(s) found — a "
                          f"review of the second number may not be reported as a review "
                          f"of the first.")}

    cases = drive_all(cfg, papers, **opts)
    results = []
    for paper, case in zip(papers, cases):
        r = as_result(cfg, case)
        r["input"] = paper
        results.append(r)

    accounting = account(papers, list(cases))
    out = {**summarize(cases), "results": results,
           "needs_audit": {c.paper_id: c.awaiting for c in cases if c.status == "waiting"},
           "preflight": pre, "corpus": accounting.model_dump()}
    ordered = [c.paper_id for c in cases if c.paper_id]
    if ordered:
        out["dossier"] = summarize_mod.build_dossier(cfg, ordered, dossier_out)
        corpus_path = (dossier_out or (cfg.projects_dir.parent / "reports")) / "corpus.json"
        try:
            state.write_json(corpus_path, accounting.model_dump())
            out["corpus_path"] = str(corpus_path)
        except OSError as e:
            out["corpus_path_error"] = str(e)
    return out


# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # self-check: python -m harness.pipeline
    import tempfile

    from .schema import Reconciliation

    # --- phase ordering -------------------------------------------------------------
    assert PHASES == ("ingest", "audit", "collect", "grade", "assess", "discover",
                      "probe", "report", "done")
    assert _next_phase("ingest") == "audit" and _next_phase("done") == "done"
    assert _next_phase("collect") == "grade" and _next_phase("grade") == "assess"
    assert _next_phase("assess") == "discover"
    assert _next_phase("discover") == "probe"
    assert RETRYABLE == ("audit", "grade")

    # --- reproduction class is READ from the artifact, never decided here -----------
    assert _reproduction_class(None) == "not_attempted"
    verified = ProbeResult(paper_id="p", reconciliation=Reconciliation(status="RESOLVED_VERIFIED"))
    assert _reproduction_class(verified) == "RESOLVED_VERIFIED"
    failed = ProbeResult(paper_id="p", reconciliation=Reconciliation(status="FAILED_REPRODUCTION"))
    assert _reproduction_class(failed) == "FAILED_REPRODUCTION"
    blocked = ProbeResult(paper_id="p", verdict="blocked", reconciliation=Reconciliation(
        status="INCONCLUSIVE", failure_class="resources_insufficient"))
    assert _reproduction_class(blocked) == "resources_insufficient"

    # --- a case that cannot be ingested is an error, and only that -------------------
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td))
        case = drive(cfg, open_case(cfg, "not-a-real-case-id"))
        assert case.status == "error", case.status
        assert case.terminal and "neither a PDF path" in case.blocked_reason
        assert [e.phase for e in case.history] == ["ingest"]
        assert case.attempts == {"ingest": 1}

    # --- the machine records every attempt --------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td))
        a, b = drive_all(cfg, ["nope-one", "nope-two"])
        assert a.status == b.status == "error", (a.status, b.status)
        assert a.paper_id != b.paper_id, "cases must not share identity"
        s = summarize([a, b])
        assert s["papers"] == 2 and len(s["errors"]) == 2 and not s["complete"]

    # --- preflight: duplicate bytes under different names refuse the batch -----------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = Config(projects_dir=root / "projects")
        cfg.projects_dir.mkdir(parents=True)
        a = root / "Alpha Paper.pdf"
        a.write_bytes(b"%PDF-1.4 alpha")
        b = root / "alpha-paper-copy.pdf"
        b.write_bytes(b"%PDF-1.4 alpha")
        c = root / "beta.pdf"
        c.write_bytes(b"%PDF-1.4 beta")

        r = preflight_check(cfg, [a, b, c])
        assert r["requested"] == 3 and r["distinct_documents"] == 2
        assert not r["ok"], "a duplicate request must refuse the batch"
        states = [e["state"] for e in r["entries"]]
        assert states == ["NEW", "DUPLICATE_REQUEST", "NEW"], states

        pid = ingest_stage.paper_id_for(c)
        paper_dir = cfg.projects_dir / pid / "paper"
        paper_dir.mkdir(parents=True)
        state.write_json(paper_dir / "doc.json", {
            "paper_id": pid, "title": "Beta", "content_sha": ingest_stage.content_sha(c)})
        r2 = preflight_check(cfg, [c])
        assert r2["entries"][0]["state"] == "RESUMES" and r2["ok"]
        assert r2["distinct_documents"] == 1

        text = render_preflight(r)
        assert "This batch is refused" in text and "byte-identical" in text

    # --- corpus accounting: conservation and a lost case are both visible -------------
    done = CaseState(paper_id="a", source="a.pdf", status="complete", phase="done",
                     verdict="GREEN", report_path="reports/a.md")
    stuck = CaseState(paper_id="b", source="b.pdf", status="waiting", phase="audit",
                      blocked_reason="rate-limited", failure_kind="rate_limited")
    broken = CaseState(paper_id="c", source="c.pdf", status="error", phase="ingest",
                       blocked_reason="not a PDF", failure_kind="extraction_failed")
    r = account(["a.pdf", "b.pdf", "c.pdf"], [done, stuck, broken])
    assert r.requested == 3 and sum(r.counts.values()) == 3, r.counts
    assert not r.complete and r.by_state["completed"] == ["a.pdf"]

    lost = account(["a.pdf", "z.pdf"], [done])
    assert lost.requested == 2 and lost.counts["requested"] == 1
    assert lost.entries[1].state == "requested" and "no case was ever opened" in lost.entries[1].reason
    assert account(["a.pdf"], [done]).complete
    assert not account([], []).complete, "an empty corpus is not a completed corpus"

    print("harness.pipeline self-check OK")
