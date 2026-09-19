"""S2.5 — grade the serious findings a second, blinded time.

Sits between S2 (audit/collect) and S3 (probe): after collect, so no token grades a
candidate whose evidence was already refused; before probe, so a paused grading pass
does not follow a long reproduction run.

`run_grade` renders one prompt per in-scope candidate and reports coverage — it does
not call a model, mirroring `stages.audit.run_audit`'s shape exactly. `fill_grades`
is the delegation, mirroring `audit_driver.fill`. `attach` is the report-time step: it
loads whatever grades exist on disk and re-derives `Finding.finding_class` /
`counted_severity` via `harness.grading.derive` — the ONLY place those fields are
written once a grade exists, exactly as `stages.audit._coerce` is the only place that
writes the ungraded baseline. Both are idempotent and safe to re-run: nothing here is
cached in a way that could go stale against a corrected lens file or a re-graded slug.
"""
from __future__ import annotations

import hashlib
import re
import time

from .. import delegation, grade_driver, grading, pdf, sealing, state
from ..artifacts import Finding, Grade, LensReport, PaperDoc
from ..config import Config
from ..prompts import grade as G
from . import audit as audit_stage
from .report import rank

_SLUG = re.compile(r"[^a-zA-Z0-9_-]+")
_PAGE_REF = re.compile(r"p(\d+)")


def slug_for(finding_id: str) -> str:
    """A filesystem-safe, path-traversal-safe name for a candidate's prompt/output
    files. `finding_id` is lens-supplied and `stages.audit._oneline` leaves `/`, `\\`,
    `..` intact — a naive `f"{finding_id}.json"` path is a traversal write primitive
    handed to a language model. The hash suffix also disambiguates two ids that
    happen to sanitise to the same prefix."""
    base = _SLUG.sub("-", finding_id or "unnamed").strip("-")[:40] or "unnamed"
    return f"{base}-{hashlib.sha256((finding_id or '').encode()).hexdigest()[:8]}"


def select_candidates(reports: list[LensReport], scope: str) -> list[Finding]:
    """Every substantiated finding in scope, in `stages.report.rank` order — most
    severe first, so a grading pass interrupted partway covers what matters most."""
    findings = [f for r in reports for f in r.findings]
    if scope != "all":
        findings = [f for f in findings if f.severity in ("FATAL", "MAJOR")]
    return rank(findings)


def _section_render(doc: PaperDoc, evidence_ref: str, budget: int) -> tuple[str, str]:
    """Section text under `budget` chars, quote-bearing section first — so the
    grader's own citation target is never the thing truncated away — plus a note
    naming what was withheld, per `SOURCE_FIDELITY`'s instruction to return
    INSUFFICIENT rather than guess when a judgement depends on a withheld section."""
    m = _PAGE_REF.fullmatch((evidence_ref or "").strip())
    target_page = int(m.group(1)) if m else None

    def _priority(s):
        if target_page and s.page_start <= target_page <= (s.page_end or s.page_start):
            return 0
        return 1
    ordered = sorted(doc.sections, key=_priority)
    kept, withheld, used = [], [], 0
    for s in ordered:
        body = f"## {s.title}  [p{s.page_start}]\n{s.text}"
        if used + len(body) > budget and kept:
            withheld.append(s.title or f"section {s.section_idx}")
            continue
        kept.append((s.section_idx, body))
        used += len(body)
    kept.sort(key=lambda t: t[0])
    text = "\n\n".join(b for _, b in kept)
    note = (f"{len(withheld)} section(s) withheld for budget: {', '.join(withheld[:8])}"
           if withheld else "all sections included")
    return text, note


def build_prompts(cfg: Config, pid: str, doc: PaperDoc,
                  candidates: list[Finding]) -> dict[str, str]:
    """Regenerated every run — never edit, like `audit/prompts/*.md`."""
    root = state.project_dir(cfg, pid)
    pdir = root / "audit" / "grade" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for f in candidates:
        slug = slug_for(f.finding_id)
        sections_text, note = _section_render(doc, f.evidence_ref, cfg.grade_budget_chars)
        # EVERY SIDE of a multi-location concern, and each side's own section context. A
        # grader shown one half of "the abstract asserts what the conclusion concedes"
        # cannot grade it, and would have to take the other half from the first reader —
        # which is precisely the deference independent grading exists to remove. Nothing
        # here tells the grader WHICH READING produced the concern: a part reader and a
        # cross-part synthesis are the same lens under the same rules, and a grader that
        # knew which had spoken could weigh the pass instead of the argument.
        for side in f.additional_evidence:
            more, _note = _section_render(doc, side.evidence_ref, cfg.grade_budget_chars)
            if more and more not in sections_text:
                sections_text = f"{sections_text}\n\n{more}"
        body = G.build(
            claim=f.as_claim(), statement=f.statement, target=f.target,
            reasoning=f.as_reasoning(), conclusion=f.as_conclusion(),
            counter_explanations=f.counter_explanations,
            evidence_quote=f.evidence_quote, evidence_ref=f.evidence_ref,
            evidence_class=f.evidence_class, verified_observation=f.verified_observation,
            additional_evidence=[e.model_dump() for e in f.additional_evidence],
            sections_text=sections_text, tables_text=pdf.render_tables(doc.tables),
            withheld_note=note,
        )
        (pdir / f"{slug}.md").write_text(body, encoding="utf-8")
        paths[slug] = str(pdir / f"{slug}.md")
    return paths


def grade_is_accepted(gdir, slug: str) -> tuple[bool, str]:
    """The grading analogue of `stages.audit.lens_is_accepted` — a grade counts only if
    the harness recorded writing it, sealed by a content hash. See that function's
    docstring; the reasoning is identical, substituting 'grader' for 'lens'."""
    path = gdir / f"{slug}.json"
    ok, why = sealing.verify_seal(path, accepted_writers=_ACCEPTED_GRADE_WRITERS)
    return ok, ("no grade file" if why == "no output file" else why)


# Every writer a validated grade path can produce: the CLI grader, plus every mode the
# delegation vocabulary admits. ONE set, so a mode cannot be sealed by `accept_grade` and
# then refused by `grade_is_accepted`, which would read as "the grader never ran".
_ACCEPTED_GRADE_WRITERS = ("grade_driver",) + tuple(delegation.WRITTEN_BY.values())


def accept_grade(cfg: Config, pid: str, slug: str, raw: str, *,
                 grader: str = "", tool_policy: str = "unrecorded",
                 mode: str = "MANUAL") -> dict:
    """The grading analogue of `stages.audit.accept_lens`: validate and seal a grade a
    grader produced OUTSIDE the `grade_driver` subprocess path.

    Exists for the same reason its audit counterpart does. `SH_ALLOW_GRADING` shells out
    to a CLI, and when that CLI is unavailable — a rate limit, no install, a revoked
    credential — there was no other way to get a grade in, so `counted_severity` fell
    back to lens-asserted severity and every report said "grading is off". The blinding
    that makes a grade worth anything is a property of WHAT THE GRADER WAS SHOWN, which
    is the prompt file, not of which process read it; so a second reader that sees only
    `audit/grade/prompts/<slug>.md` is as blinded as the subprocess was.

    What is NOT equivalent, and is recorded rather than glossed: the subprocess ran with
    `--allowedTools ""` and an empty sandbox cwd, so it COULD NOT open the lens file and
    read the severity withheld from it. Nothing reaching this function can prove that.
    `tool_policy` defaults to `unrecorded`, and `grader` names what did the reading.
    """
    grade = grade_driver.parse_grade_json(raw)
    gdir = state.project_dir(cfg, pid) / "audit" / "grade"
    out = gdir / f"{slug}.json"
    # The mode, recorded, exactly as `accept_lens` records it. A grade produced by an
    # isolated subagent the controller dispatched and a grade a human typed are different
    # provenance, and `grade_coverage` reporting one number over both would say a paper's
    # severities were independently weighed when some of them were not.
    record = sealing.seal(out, grade.model_dump(), mode=mode, reviewer=grader,
                          tool_policy=tool_policy,
                          extra={"slug": slug, "paper_id": pid, "verdict": grade.verdict})
    # The sidecar's own field has always been `grader`, never `reviewer` — `sealing.seal`
    # derives a generic `reviewer` field internally (shared with every other instance,
    # already defaulted to "unnamed" and stripped by `delegation.provenance_record`), and
    # this is the one instance whose schema-specific name differs, so it is RENAMED
    # rather than duplicated: `grade_is_accepted` and every existing reader of this
    # sidecar has only ever read `grader`.
    record["grader"] = record.pop("reviewer")
    state.write_json(out.with_suffix(".driver.json"), record)
    return record


def run_grade(cfg: Config, pid: str) -> dict:
    """Write one prompt per in-scope candidate. Does not call a model."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}'"}
    doc = PaperDoc(**state.read_json(doc_path))
    reports, _dropped, invalid = audit_stage.load_reports(cfg, pid, doc)
    if invalid:
        return {"error": f"audit panel is incomplete or invalid ({', '.join(invalid)}); "
                         f"cannot select candidates to grade until it collects cleanly"}

    candidates = select_candidates(reports, cfg.grade_scope)
    prompts = build_prompts(cfg, pid, doc, candidates)
    gdir = root / "audit" / "grade"
    done = [slug for slug in prompts if grade_is_accepted(gdir, slug)[0]]
    todo = [slug for slug in prompts if slug not in done]
    return {
        "paper_id": pid, "candidates": len(candidates),
        "prompts": {s: prompts[s] for s in todo}, "awaiting": todo, "complete": done,
        "slugs": {slug_for(f.finding_id): f.finding_id for f in candidates},
    }


def coverage(cfg: Config, pid: str) -> dict:
    res = run_grade(cfg, pid)
    if "error" in res:
        return res
    return {"paper_id": pid, "candidates": res["candidates"],
            "graded": len(res["complete"]), "pending": len(res["awaiting"])}


def attach(cfg: Config, pid: str, doc: PaperDoc, reports: list[LensReport]) -> dict:
    """Load whatever grades exist and re-derive the harness-written fields on the
    matching findings, IN PLACE. Safe to call whether or not grading ever ran —
    findings with no accepted grade simply keep the ungraded baseline `_coerce` set.
    """
    root = state.project_dir(cfg, pid)
    gdir = root / "audit" / "grade"
    corpus = audit_stage.source_units(doc)
    by_idx = {t.table_idx: t for t in doc.tables}
    by_figure = {fig.figure_idx: fig for fig in doc.figures}
    by_equation = {e.equation_idx: e for e in doc.equations}
    max_page = doc.n_pages or 0
    graded_n = 0
    for r in reports:
        for f in r.findings:
            slug = slug_for(f.finding_id)
            ok, _why = grade_is_accepted(gdir, slug)
            if not ok:
                continue
            try:
                g = Grade(**state.read_json(gdir / f"{slug}.json"))
            except Exception:
                continue
            grader_evidence_class, grader_obs = audit_stage.verify_evidence(
                g.independent_evidence_quote, g.independent_evidence_ref, corpus, by_idx, max_page,
                by_figure=by_figure, by_equation=by_equation)
            finding_class, counted_severity, binding_cap, derivation = grading.derive(
                lens_severity=f.severity, verification_state=f.verification_state,
                calc_class=f.calc_class, graded=True, grade_verdict=g.verdict,
                grade_severity=g.severity, confidence=g.confidence,
                falsification_survived=g.falsification_survived,
                has_impact_statement=bool(g.impact_statement.strip()),
                has_steelman=bool(g.steelman.strip()),
                evidence_class=f.evidence_class, grader_evidence_class=grader_evidence_class,
                lens_confidence=f.confidence, candidate_class=f.candidate_class,
                baseline_class=f.baseline_class, prior_art_basis=f.prior_art_basis)
            f.grade = g
            f.grade_state = "graded"
            f.finding_class = finding_class
            f.counted_severity = "" if counted_severity == f.severity else counted_severity
            f.binding_cap = binding_cap
            f.derivation = derivation
            f.grader_evidence_class = grader_evidence_class
            f.grader_verified_observation = grader_obs
            # Re-derived with the GRADER's citation now in scope: a figure-only concern the
            # grader independently corroborated against a table cell is no longer
            # figure-only, and the ceiling the report prints has to say so.
            f.evidence_ceiling, sources = grading.evidence_support(
                f.evidence_class, grader_evidence_class, f.calc_class)
            f.evidence_sources = list(sources)
            graded_n += 1
    return {"graded": graded_n}


if __name__ == "__main__":  # self-check: python -m harness.stages.grade
    from ..artifacts import Section, Table

    doc = PaperDoc(paper_id="p", title="T",
                   sections=[Section(section_idx=0, title="Results", page_start=3,
                                     text="a" * 100),
                            Section(section_idx=1, title="Limitations", page_start=8,
                                     text="b" * 100)],
                   tables=[Table(table_idx=0, page=3, rows=[["m", "91.4"]])])
    text, note = _section_render(doc, "p8", budget=150)
    assert "Limitations" in text and "all sections included" not in note, note
    assert slug_for("overclaim-01") != slug_for("overclaim-02")
    assert slug_for("../../etc/passwd") and "/" not in slug_for("../../etc/passwd")
    assert len(slug_for("x" * 500)) < 60, "slug must stay short regardless of input length"

    reports = [LensReport(lens="overclaim", findings=[
        Finding(finding_id="overclaim-01", lens="overclaim", severity="FATAL"),
        Finding(finding_id="overclaim-02", lens="overclaim", severity="MINOR"),
    ])]
    serious = select_candidates(reports, "serious")
    assert [f.finding_id for f in serious] == ["overclaim-01"], serious
    everything = select_candidates(reports, "all")
    assert len(everything) == 2

    print("grade self-check OK")
