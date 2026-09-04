"""S2 — the four-lens audit, driven by the Claude Code session instead of an SDK call.

`run_audit` does not call a model. It renders one self-contained prompt file per lens
into `audit/prompts/<lens>.md` and stops. The driver (the interactive session) opens
one prompt at a time, performs that audit, and writes `audit/<lens>.json`.

Working one lens per turn preserves the property the SDK sessions gave for free:
four independent readings rather than one reading echoed four times. It is weaker
than process isolation — a single context can remember the previous lens — so the
prompt says so explicitly and the lenses are ordered to be run separately.

What is NOT weaker is provenance. `load_reports` re-checks every finding's
`evidence_quote` against the parsed paper and, when the finding cites a cell, against
that cell's real contents. The driver is a language model; the harness does not take
its word for anything it can verify itself.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import functools
import hashlib
import os
import re
import time

from .. import audit_driver, grading, pdf, state
from ..artifacts import (CANDIDATE_CLASSES, CONFIDENCES, DISCREPANCY_TYPES, EVIDENCE_ORIGINS,
                         SEVERITIES, Equation, Figure, Finding, LensReport, PaperDoc)
from ..config import Config
from ..prompts import audit as P

LENSES = tuple(P.LENSES)
SECTION_BUDGET_CHARS = int(os.environ.get("SH_AUDIT_BUDGET_CHARS", "70000"))
_SEVERITIES = SEVERITIES
_CELL_REF = re.compile(r"T(\d+):r(\d+):c(\d+)")
# The four admissible shapes for `evidence_ref`. Anything else — empty, "figure 3",
# "section 5", a bare page number — names no location a reader can check, so a finding
# carrying it is not substantiated. See `_substantiated`.
_PAGE_REF = re.compile(r"p\d+", re.I)
_FIG_REF = re.compile(r"F(\d+)")
_EQ_REF = re.compile(r"E(\d+)")
_QUOTE_MIN = 8
# No letter, no digit: a bare arrow, bullet, dash or other glyph. Checked against the
# FLATTENED quote (`_flat`, already lowercased), so this is deliberately about alphabet
# and digit characters only — matched after normalisation, not before.
_MEANINGLESS_QUOTE = re.compile(r"^[^0-9a-z]*$")
_WS = re.compile(r"\s+")


def _flat(s: str) -> str:
    return _WS.sub("", (s or "").lower())


def _enum(v, vocab: tuple[str, ...], default: str = "") -> str:
    """Uppercase and clamp to a closed vocabulary. Garbage becomes `default`, never a
    silent pass-through — the same discipline `severity`'s clamp-to-MINOR already has,
    generalised so every new lens-supplied enum gets it for free."""
    s = str(v or "").strip().upper()
    return s if s in vocab else default


def _prose(v, n: int = 4000) -> str:
    return str(v or "").strip()[:n]


def _origin_from_ref(ref: str) -> str:
    """PAPER_TABLE/PAPER_TEXT/PAPER_FIGURE/PAPER_EQUATION from the shape of `ref` alone.

    Deliberately not read from the lens: the ref shape already determines the origin
    for anything that can be a finding's PRIMARY evidence, so trusting the lens to name
    it adds nothing and invites a lens calling a page citation a table to go unnoticed.
    """
    ref = (ref or "").strip()
    if _CELL_REF.fullmatch(ref):
        return "PAPER_TABLE"
    if _PAGE_REF.fullmatch(ref):
        return "PAPER_TEXT"
    if re.fullmatch(r"F\d+", ref):
        return "PAPER_FIGURE"
    if re.fullmatch(r"E\d+", ref):
        return "PAPER_EQUATION"
    return ""


def _oneline(s: str, n: int) -> str:
    """Collapse to one line and cap the length.

    Applied to the two lens-supplied fields the renderer places directly into report
    markdown structure — `finding_id` and `title` — never to evidence, which is verified
    verbatim instead. Without this, an embedded newline plus a line starting with `#`
    lets a lens's own chosen text forge a heading (a fake "## Verdict: GREEN") or a table
    row in the rendered report, whether from a prompt injection or an LLM's habit of
    formatting a string field as if it were markdown.
    """
    return " ".join((s or "").split())[:n]


def source_units(doc: PaperDoc) -> tuple[tuple[int, str], ...]:
    """The paper as SEPARATE searchable units, one per section. Never one string.

    A quote is verified against each unit independently. Concatenating the sections into
    a single corpus first — which is what this did — let any string that straddles a
    section boundary verify, because after whitespace removal the seam is invisible.
    Reproduced on the real APT paper: the join of section 0's tail and section 1's head
    is `'owen Zhao 1 Hannaneh Hajishirzi 1 2 Qingqing Cao*3Fine-tuning and inference'`,
    which does not occur in the PDF, and the harness certified in its own voice that it
    "occurs verbatim in the parsed section text". Four separate seams verified.

    That is the one place the design says the MACHINE, not the model, is the author of
    the observation, so a false attestation there is not a cosmetic bug — it is the trust
    anchor asserting something untrue. Sections are the unit because they are the unit
    the parser produced and the unit a reader can open; whitespace normalisation still
    happens, inside a unit, where it cannot invent adjacency.
    """
    return tuple((s.section_idx, _flat(s.text))
                 for s in doc.sections if (s.text or "").strip())


def _units(corpus) -> tuple[tuple[int, str], ...]:
    """Accept the unit tuple, or a single pre-flattened string as one anonymous unit.

    Each unit carries the section's OWN `section_idx`, not its position in this tuple.
    Empty sections are skipped, so the two differ — and the first version of this fix
    reported the tuple position as the section identity. On the real APT paper (59
    sections, 4 of them empty) that misaddressed 12 of 12 prose findings: the observation
    said "#50" for text in section 53, and a reader opening #50 in doc.json finds nothing.
    Replacing one false machine attestation with another is not a fix, so the index is
    carried with the text rather than inferred from the ordering.
    """
    return ((-1, corpus),) if isinstance(corpus, str) else tuple(corpus)


def render_numbers(doc: PaperDoc) -> str:
    out = []
    for n in doc.reported_numbers:
        where = n.table_ref or f"p{n.page}"
        if n.table_ref:
            out.append(f"- [{where}] {n.method} · {n.metric} = {n.value}"
                       f"{f' (±{n.seeds_or_variance})' if n.seeds_or_variance else ' (NO VARIANCE REPORTED)'}"
                       f"  << {n.benchmark}")
        else:
            out.append(f"- [{where}] prose claim: \"{n.source_quote}\"")
    return "\n".join(out)


def context(doc: PaperDoc) -> dict[str, str]:
    return {
        "sections_text": pdf.render_sections(doc.sections, SECTION_BUDGET_CHARS),
        "tables_text": pdf.render_tables(doc.tables),
        "claims_text": "(none pre-extracted — identify the paper's claims yourself "
                       "from the sections below; that judgement is part of your job)",
        "numbers_text": render_numbers(doc),
        "pdf_path": doc.source_path,
        "figures_text": pdf.render_figures(doc.figures),
        "equations_text": pdf.render_equations(doc.equations),
    }


def _header(lens: str, pid: str) -> str:
    return f"""<!-- generated by `run.py stage audit --paper {pid}` — do not edit -->

# Audit lens: `{lens}`  ·  paper `{pid}`

**Driver instructions.** Perform this audit now, then PRINT your result to standard
output as a single JSON object matching the schema at the end of this file — do not
write `audit/{lens}.json` or any other file yourself; only what you print is read, and
it is validated and written to that path by the harness after you finish. Run each lens
in a SEPARATE turn and do not let one lens's findings influence another — they are
meant to be independent readings.

Every finding is re-verified when the report is built: a finding whose
`evidence_quote` is not actually present in this paper, or whose cited cell does not
contain what it says, is DROPPED. Quote exactly.

---

"""


def lens_is_accepted(root: Path, lens: str) -> tuple[bool, str]:
    """A lens result counts only if the HARNESS recorded writing it.

    File existence is not provenance. `audit/<lens>.json` can exist because a reviewer
    with filesystem access wrote it directly, bypassing `parse_lens_json`'s validation
    (the `evidence_quote`-without-`evidence_ref` refusal among others) and the
    staging/promotion step `run_lens` exists to provide — which is exactly what happened
    on a real six-paper run: three reviewers side-wrote their lens file, their prose
    stdout was correctly rejected, no `.driver.json` sidecar was ever created for those
    lenses, and `run_audit`/`load_reports` (checking file existence alone) counted them
    as complete anyway. Findings reached reports with no record of who produced them and
    with the driver's own validation gate bypassed.

    Checked, in order: the lens file exists; a `.driver.json` sidecar exists beside it;
    `written_by` names a path that actually validates (`audit_driver` or
    `manual_accept`, see `accept_lens`); the sidecar's `content_sha256` matches the lens
    file's CURRENT bytes — the seal that makes this a real check rather than a token,
    since a file edited after acceptance is no longer the file that was accepted.

    ONE predicate, TWO callers (`run_audit`'s completeness list and `load_reports`) —
    the same discipline `_substantiated` already has as the boolean shadow of
    `verify_evidence`, for the same reason: if the two callers could disagree about
    whether a lens counts, the controller's audit/collect phases would oscillate
    (`_phase_audit` reports `ok` on an empty `awaiting` while `_phase_collect` reports
    `waiting` on the same lens), burning the bounded retry loop for no reason.
    """
    lens_path = root / "audit" / f"{lens}.json"
    if not lens_path.exists():
        return False, "no lens file"
    driver_path = lens_path.with_suffix(".driver.json")
    if not driver_path.exists():
        return False, ("no provenance sidecar (.driver.json) — most likely side-written "
                       "by the reviewer instead of produced through the validated staging path")
    try:
        rec = state.read_json(driver_path)
    except Exception:
        return False, "provenance sidecar is not valid JSON"
    if not isinstance(rec, dict) or rec.get("written_by") not in ("audit_driver", "manual_accept"):
        return False, f"provenance sidecar written_by={rec.get('written_by') if isinstance(rec, dict) else None!r} not recognized"
    want = rec.get("content_sha256")
    if not want:
        return False, "provenance sidecar has no content_sha256"
    if hashlib.sha256(lens_path.read_bytes()).hexdigest() != want:
        return False, "lens file content changed after its provenance sidecar was written"
    return True, ""


def accept_lens(cfg: Config, pid: str, lens: str, raw: str) -> dict:
    """Validate and persist a HAND-WRITTEN lens file through the SAME gate the
    auto-audit path uses, closing the asymmetry `lens_is_accepted` exists to police:
    before this, a manually-written `audit/<lens>.json` skipped `parse_lens_json`
    entirely (no ref-less-quote refusal, no structural check) and had no sidecar, so it
    could never itself satisfy `lens_is_accepted`. One validation path for both channels.
    """
    report = audit_driver.parse_lens_json(raw, lens)
    root = state.project_dir(cfg, pid)
    out = root / "audit" / f"{lens}.json"
    state.write_json(out, report.model_dump())
    content_sha256 = hashlib.sha256(out.read_bytes()).hexdigest()
    record = {"lens": lens, "paper_id": pid, "written_by": "manual_accept",
              "content_sha256": content_sha256, "findings": len(report.findings),
              "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    state.write_json(out.with_suffix(".driver.json"), record)
    return record


def run_audit(cfg: Config, pid: str, lenses: tuple[str, ...] = LENSES) -> dict:
    """Write one prompt file per lens. Does not call a model."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))
    state.set_phase(cfg, pid, "audit")

    ctx = context(doc)
    pdir = root / "audit" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    for lens in lenses:
        body = _header(lens, pid) + P.build(lens, doc.title, **ctx)
        (pdir / f"{lens}.md").write_text(body, encoding="utf-8")

    done = [ln for ln in lenses if lens_is_accepted(root, ln)[0]]
    todo = [ln for ln in lenses if ln not in done]
    state.append_log(
        cfg, pid, artifact_type="audit_prompts", phase="audit",
        headers={"lenses": list(lenses), "awaiting": todo, "complete": done,
                 "chars": sum(len((pdir / f'{ln}.md').read_text(encoding='utf-8')) for ln in lenses)},
        path=str(pdir),
    )
    return {
        "paper_id": pid, "title": doc.title,
        "prompts": {ln: str(pdir / f"{ln}.md") for ln in lenses},
        "awaiting": todo, "complete": done,
        "next": (f"Read each prompt in audit/prompts/, perform that audit, and write "
                 f"audit/<lens>.json. Awaiting: {', '.join(todo) or 'none'}.")
        if todo else "All four lenses have results; run synthesize_report.",
    }


def load_reports(cfg: Config, pid: str,
                 doc: PaperDoc) -> tuple[list[LensReport], int, list[str]]:
    """Load every lens result, dropping findings that cannot be substantiated.

    Returns (reports, n_dropped, invalid_lenses). A lens whose file is missing,
    unparseable, empty, `null`, or not the `{"findings": [...]}` shape a lens report has
    to be is named in `invalid_lenses` — it still yields a report (so `lenses_run`
    bookkeeping and diagnostics stay visible) but MUST NOT be read as "this lens ran and
    found nothing": the caller — `run_report` and the controller's `collect` phase — has
    to refuse a verdict rather than render one over a panel that never actually completed.
    A `{"findings": []}` file some lens genuinely wrote is not in this list; a lens that
    never produced a well-formed report at all is.
    """
    root = state.project_dir(cfg, pid)
    corpus = source_units(doc)
    by_idx = {t.table_idx: t for t in doc.tables}
    by_figure = {f.figure_idx: f for f in doc.figures}
    by_equation = {e.equation_idx: e for e in doc.equations}
    max_page = doc.n_pages or 0
    reports, dropped, invalid = [], 0, []
    for lens in LENSES:
        path = root / "audit" / f"{lens}.json"
        if not path.exists():
            invalid.append(lens)
            continue
        ok, why = lens_is_accepted(root, lens)
        if not ok:
            # A file at this path that the harness did not seal — see `lens_is_accepted`.
            # Named explicitly rather than silently invalid, because the operator's fix
            # (re-run the lens) is different from the fix for a malformed file.
            reports.append(LensReport(lens=lens, notes=f"(no provenance record: {why})"))
            invalid.append(lens)
            continue
        try:
            raw = state.read_json(path)
        except Exception:
            reports.append(LensReport(lens=lens, notes="(unparseable lens file)"))
            invalid.append(lens)
            continue
        report, n, valid = _coerce(lens, raw, corpus, by_idx, max_page,
                                   by_figure=by_figure, by_equation=by_equation)
        reports.append(report)
        dropped += n
        if not valid:
            invalid.append(lens)
    return reports, dropped, invalid


def verify_evidence(quote: str, ref: str, corpus: str | Sequence[tuple[int, str]],
                    by_idx: dict, max_page: int = 0, *,
                    by_figure: dict[int, Figure] | None = None,
                    by_equation: dict[int, Equation] | None = None) -> tuple[str, str]:
    """(evidence_class, verified_observation) — WHAT THE HARNESS ITSELF CONFIRMED.

    This is the machine half of a finding, and the reason it lives here rather than in
    the lens file is that a lens must not be able to assert that its own reasoning was
    checked. Everything a lens writes is a claim; everything this function returns is an
    observation, and the report renders the two apart so a reader can tell which is which.

    The observation text is generated, never copied: it names the address, states that the
    contents matched, and quotes what was found. A reader can re-run the same comparison
    from `doc.json` in a few seconds.

    `max_page`, when given, bounds a `p<N>` reference against the paper's own page count.
    Optional (0 = unchecked) so the many unit tests that exercise this function directly
    do not have to carry a page count they have no opinion about; `load_reports` — the one
    caller that matters for a real review — always passes the parsed paper's `n_pages`.
    """
    q = _flat(quote)
    if not q or _MEANINGLESS_QUOTE.fullmatch(q):
        # A quote with no letter and no digit — a bare arrow, a bullet, a dash, a lone
        # punctuation mark — cannot support a claim about anything, however exactly it
        # matches a cell. `cell_verified` is the STRONGEST evidence class this harness
        # grants, and existence of a cell is not the same as existence of a finding: a
        # trivial fragment earning it let a report cite "⇑" as strong, checkable evidence
        # for a claim the symbol says nothing about.
        return "unverified", ""
    ref = (ref or "").strip()
    m = _CELL_REF.fullmatch(ref)
    if m:
        t = by_idx.get(int(m.group(1)))
        if t is None:
            return "unverified", ""
        cell = t.cell(int(m.group(2)), int(m.group(3)))
        if _flat(cell) != q:
            return "unverified", ""
        return "cell_verified", (
            f"Cell {ref} of the parsed paper contains {cell.strip()!r}, which matches the "
            f"quoted evidence character for character after whitespace normalisation.")
    fm = _FIG_REF.fullmatch(ref)
    if fm:
        fig = (by_figure or {}).get(int(fm.group(1)))
        if fig is None or len(q) < _QUOTE_MIN or q not in _flat(fig.caption):
            return "unverified", ""
        return "caption_verified", (
            f"Figure caption {ref} (page {fig.page}, {fig.label!r}) of the parsed paper "
            f"contains the quoted text verbatim. A CAPTION NAMES A FIGURE; it does not "
            f"report the figure's plotted values, and nothing about those values is "
            f"verified here.")
    em = _EQ_REF.fullmatch(ref)
    if em:
        eq = (by_equation or {}).get(int(em.group(1)))
        if eq is None or len(q) < _QUOTE_MIN or q not in _flat(eq.text):
            return "unverified", ""
        return "equation_verified", (
            f"Equation {ref} (page {eq.page}"
            f"{f', numbered {eq.number}' if eq.number else ''}) of the parsed paper contains "
            f"the quoted text verbatim. Display-equation extraction is lossy: symbols, "
            f"sub/superscripts and inline math may be missing or mangled, and this verifies "
            f"the extracted text only.")
    pm = _PAGE_REF.fullmatch(ref)
    if not pm:
        return "unverified", ""
    if max_page > 0:
        page_num = int(pm.group()[1:])
        if page_num < 1 or page_num > max_page:
            # An impossible reference: this paper does not have a page `page_num`. A quote
            # that happens to occur somewhere in the corpus is not evidence for a claim
            # anchored to a page that cannot exist, and letting it verify anyway is exactly
            # the "page/table/cell references are actually valid" gap this check closes.
            return "unverified", ""
    if len(q) < _QUOTE_MIN:
        return "unverified", ""
    # WITHIN ONE SECTION. A quote spanning two sections is not in the paper.
    hit = next((idx for idx, unit in _units(corpus) if q in unit), None)
    if hit is None:
        return "unverified", ""
    where = f"section_idx {hit}" if hit >= 0 else "the parsed section text"
    return "prose_verified", (
        f"The quoted text occurs verbatim inside a single parsed section ({where}); the lens "
        f"cited {ref}, which is NOT checked — only the section containing the quote is. "
        f"Verified as a substring of one section, not across sections and not as a page.")


def _substantiated(quote: str, ref: str, corpus: str | Sequence[tuple[int, str]],
                   by_idx: dict) -> bool:
    """Is this quote really in the paper — and if it cites a cell, is it that cell?

    The length floor applies to PROSE quotes only. A short prose fragment matches
    anywhere and proves nothing, but a cell citation is already pinned to exact
    coordinates, so "0.0001" at T1:r0:c1 is the most checkable evidence there is —
    and rejecting it would throw away precisely the table numbers this harness exists
    to audit.

    A MISSING OR MALFORMED `ref` IS NOT SUBSTANTIATED. This is the load-bearing line.
    Earlier the reference was optional: an absent `evidence_ref` fell through to the
    prose branch, so a finding that cited a table cell and then lost its reference in
    serialization was re-checked as a free-floating substring against the whole paper —
    and passed, because the cell's own text does appear somewhere in the corpus. The
    finding survived with its provenance deleted, silently demoted from "this exact cell
    says X" to "these characters occur somewhere", which is the difference between
    evidence an editor can check in seconds and no evidence at all. Requiring the
    reference to be present and well-formed means a lost one is DROPPED and counted,
    where the report already prints the count, instead of quietly downgraded.

    The predicate is now the boolean shadow of `verify_evidence`, which returns the same
    decision plus the machine-written observation that goes on the finding. One
    implementation, so the thing that DROPS a finding and the thing that DESCRIBES a kept
    one can never disagree about whether the evidence held.
    """
    return verify_evidence(quote, ref, corpus, by_idx)[0] != "unverified"


def _coerce(lens: str, data, corpus: str | Sequence[tuple[int, str]],
            by_idx: dict, max_page: int = 0, *,
            by_figure: dict[int, Figure] | None = None,
            by_equation: dict[int, Equation] | None = None) -> tuple[LensReport, int, bool]:
    """(report, n_dropped, valid). `valid` is whether the FILE ITSELF was the shape a
    lens report has to be — a JSON object carrying a `findings` list — independent of
    whether any individual finding inside it survived verification (C9). `{}`, `null`,
    a bare list, or a `findings` value that is not a list are each structurally invalid
    the same way `audit_driver.parse_lens_json` already refuses them on the auto-audit
    path; this is that same validation applied to the persisted/manual path, which had
    none — `{}` used to coerce to zero findings with no distinction from a lens that
    genuinely ran clean.
    """
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        note = ("(lens file was not a JSON object)" if not isinstance(data, dict)
                else "(lens file has no 'findings' list)")
        return LensReport(lens=lens, notes=note), 0, False
    verify = functools.partial(verify_evidence, by_figure=by_figure, by_equation=by_equation)
    findings, dropped = [], 0
    for i, f in enumerate(data.get("findings") or []):
        if not isinstance(f, dict):
            dropped += 1
            continue
        statement = str(f.get("statement") or "").strip()
        quote = str(f.get("evidence_quote") or "").strip()
        ref = str(f.get("evidence_ref") or "").strip()
        evidence_class, observation = verify(quote, ref, corpus, by_idx, max_page)
        if not statement or evidence_class == "unverified":
            dropped += 1
            continue
        sev = str(f.get("severity") or "").upper()
        sev = sev if sev in _SEVERITIES else "MINOR"
        calc = f.get("independent_calculation")
        calc = calc if isinstance(calc, dict) else {}
        verification_state = grading.pass_b_state(f, sev)
        calc_class = grading.recheck_calculation(calc, verify, corpus, by_idx, max_page)
        # Baseline derivation from the lens's OWN submission alone — no grader has run
        # yet (that happens in a later pipeline phase; see `stages/grade.py:attach`,
        # which re-derives these same four fields once a grade exists, strictly
        # DOWNWARD from whatever is set here). `graded=False` is what makes turning the
        # grading gate off, or never running it, leave the verdict exactly where it was
        # before this subsystem existed: `stages.report.counted()` falls back to
        # `severity` whenever `counted_severity` is empty, and it is empty here only
        # when neither this baseline nor a later grade capped it.
        finding_class, counted_severity, binding_cap, derivation = grading.derive(
            lens_severity=sev, verification_state=verification_state,
            calc_class=calc_class, graded=False, evidence_class=evidence_class)
        if counted_severity == sev:
            # Nothing capped it — leave `counted_severity` empty rather than a value
            # identical to `severity`, so a reader can tell "verified equal" apart from
            # "never assessed" by checking whether it is set at all.
            finding_class, counted_severity, binding_cap, derivation = "UNGRADED", "", "", ""
        findings.append(Finding(
            finding_id=_oneline(str(f.get("finding_id") or f"{lens}-{i + 1:02d}"), 60),
            lens=lens, severity=sev,
            title=_oneline(str(f.get("title") or statement), 90), statement=statement,
            target=str(f.get("target") or ""), evidence_quote=quote, evidence_ref=ref,
            counter_explanations=[str(c) for c in (f.get("counter_explanations") or [])
                                 if isinstance(c, (str, int, float))],
            verifiable_by_experiment=bool(f.get("verifiable_by_experiment")),
            # The lens's own layers, kept apart from each other and from the evidence.
            # Each falls back to `statement` so a file written before the split still
            # produces a complete finding.
            claim=str(f.get("claim") or f.get("target") or "").strip(),
            reasoning=str(f.get("reasoning") or statement).strip(),
            conclusion=str(f.get("conclusion") or statement).strip(),
            severity_rationale=str(f.get("severity_rationale") or "").strip(),
            # The pass-B fields: lens-authored, non-degeneracy checked but not rewritten
            # — a lens's actual words are kept even when `verification_state` below
            # says they were not enough.
            candidate_class=_enum(f.get("candidate_class"), CANDIDATE_CLASSES),
            confidence=_enum(f.get("confidence"), CONFIDENCES),
            discrepancy_type=_enum(f.get("discrepancy_type"), DISCREPANCY_TYPES),
            what_the_paper_says=_prose(f.get("what_the_paper_says")),
            alternative_interpretation=_prose(f.get("alternative_interpretation")),
            why_alternative_fails=_prose(f.get("why_alternative_fails")),
            steelman=_prose(f.get("steelman")),
            effect_on_claim=_prose(f.get("effect_on_claim")),
            recommended_resolution=_prose(f.get("recommended_resolution")),
            independent_calculation=calc,
            # NOT read from `f`. A lens supplying any of these is overwritten here,
            # because the whole point of a harness-written field is that the harness —
            # not the model — is its author (invariant #2, extended to every axis added
            # since: a lens cannot certify its own reasoning, its own arithmetic, or its
            # own non-degeneracy any more than it could certify its own evidence).
            evidence_class=evidence_class,
            verified_observation=observation,
            verification_state=verification_state,
            calc_class=calc_class,
            evidence_origin=_origin_from_ref(ref),
            origin_consistency=("consistent"
                                if _enum(f.get("evidence_origin"), EVIDENCE_ORIGINS)
                                in ("", _origin_from_ref(ref)) else "corrected"),
            finding_class=finding_class, counted_severity=counted_severity,
            binding_cap=binding_cap, derivation=derivation,
        ))
    return LensReport(
        lens=lens, findings=findings,
        unasked_question=str(data.get("unasked_question") or "").strip(),
        notes=str(data.get("notes") or ""),
    ), dropped, True
