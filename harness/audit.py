"""S2 (audit) + S2.5 (grade): the four scientific lenses, a long paper read in bounded
parts with one cross-part synthesis, the second blinded grader, and the pure
severity-derivation table underneath both (vocabulary strings and booleans only, so
"no std dev = MAJOR" is inexpressible).

Model dispatch (spawn/confine/parse/seal a reviewer subprocess) is `agent.py`'s job
(`agent.fill_lenses`/`agent.fill_grades`). This module writes prompts, verifies what comes
back against the parsed paper, composes a part-read lens file, and derives what a finding
counts as; it stops at producing verified, composed `LensReport`s (question-syncing for the
DISCOVER phase is `discover.py`'s job).

Two invariants held here: a lens supplies a QUOTE and the harness alone decides whether it
is real (`verify_evidence` -- `verified_observation`/`evidence_class` are WRITTEN BY THE
HARNESS, never read from a lens file); and severity may only be CAPPED, never raised, by
anything downstream of a lens's own assertion (`derive`,
`RANK[counted_severity] <= RANK[lens_severity]` over the whole reachable table).

`python -m harness.audit` runs the self-check.
"""
from __future__ import annotations

import ast
import functools
import hashlib
import operator
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from . import agent, decide, paper, state, taxonomy
from .config import Config
from .locate import _self_projection, flatten, soft_hyphen_projection
from .prompts import audit as P
from .prompts import grade as G
from .schema import (BASELINE_CLASSES, CANDIDATE_CLASSES, CONFIDENCES, DISCREPANCY_TYPES,
                     EVIDENCE_ORIGINS, PRIOR_ART_BASES, SEVERITIES, Equation,
                     EvidencePointer, Figure, Finding, Grade, LensReport, PaperDoc)

# =============================================================================================
# PART 1 -- PURE SEVERITY DERIVATION
# =============================================================================================
# Three questions kept apart: IS THERE AN ISSUE (verdict/candidate_class), HOW SURE ARE WE
# (confidence, bounded by evidence_support), HOW MUCH DOES IT MATTER (severity -- the only
# one any threshold counts, and the one nothing here SETS, only caps). `scientific_class`
# is deliberately not a parameter. Swept below: RANK[counted] <= RANK[lens].

RANK = {"FATAL": 3, "MAJOR": 2, "MINOR": 1, "NOTE": 0, "": 0}

_STOPLIST = {"n/a", "na", "none", "not applicable", "see above", "same as above", ""}
_MIN_CHARS = 40


def _flat_g(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def pass_b_state(f: dict, severity: str, schema_version=None) -> str:
    """'legacy' (pre `schema_version: 2`, uncapped) | 'incomplete' (a required field is
    blank/stoplisted/identical to statement-claim-title) | 'complete'. `schema_version`
    is the REPORT's, not the finding's, and must be threaded through explicitly."""
    version = schema_version if schema_version is not None else f.get("schema_version")
    if version != 2:
        return "legacy"
    required = ["alternative_interpretation", "why_alternative_fails"]
    if severity in ("FATAL", "MAJOR"):
        required.append("steelman")          # not required below MAJOR
    others = {_flat_g(f.get(k)) for k in ("statement", "claim", "title")}
    for key in required:
        t = _flat_g(f.get(key))
        if len(t) < _MIN_CHARS or t in _STOPLIST or t in others:
            return "incomplete"
    return "complete"


# Tiny arithmetic sandbox: literals and four operators only.
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise ValueError("only numeric literals are allowed")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        # Pow's exponent is bounded on its VALUE, not AST shape (9**9**9 nests as BinOp).
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"disallowed expression node: {type(node).__name__}")


def recheck_calculation(calc: dict, verify_fn, corpus, by_idx, max_page: int = 0) -> str:
    """Independently redo a lens's own arithmetic, catching a disputed percentage that
    cannot supply two verified operands reproducing its own claimed result. `verify_fn`
    is `verify_evidence`, passed in."""
    if not calc or not calc.get("applies"):
        return "not_applicable"
    operands = calc.get("operands") or []
    expression, result = calc.get("expression"), calc.get("result")
    if not operands or not expression or result in (None, ""):
        return "not_attempted"
    for op in operands:
        quote = str((op or {}).get("quote") or "")
        ref = str((op or {}).get("ref") or "")
        if verify_fn(quote, ref, corpus, by_idx, max_page)[0] == "unverified":
            return "operands_unverified"
    try:
        computed = _safe_eval(ast.parse(str(expression), mode="eval"))
        wanted = float(str(result).strip().rstrip("%"))
    except (ValueError, SyntaxError, TypeError, ZeroDivisionError, RecursionError):
        return "unparseable"
    tolerance = max(abs(wanted) * 0.02, 0.005)
    return "recomputed_ok" if abs(computed - wanted) <= tolerance else "recomputed_mismatch"


# A grade verdict caps severity; confidence caps it too, but never sets it.
_VERDICT_CAP = {"CONFIRMED": "FATAL", "PLAUSIBLE": "MINOR", "INSUFFICIENT": "NOTE",
                "REFUTED": "NOTE"}
_CONFIDENCE_CAP = {"HIGH": "FATAL", "MEDIUM": "MAJOR", "LOW": "MINOR"}
_CLASS = {"CONFIRMED": "CONFIRMED_FINDING", "PLAUSIBLE": "PLAUSIBLE_CONCERN",
          "INSUFFICIENT": "OPEN_QUESTION", "REFUTED": "REFUTED"}

# Only CONFIRMED_FINDING may carry FATAL/MAJOR, however severe the lens also called it.
CANDIDATE_CAP = {"CONFIRMED_FINDING": "FATAL", "PLAUSIBLE_CONCERN": "MINOR",
                 "OPEN_QUESTION": "NOTE", "DISMISSED": "NOTE", "": "FATAL"}

# Evidence TYPE bounds CONFIDENCE, never severity directly: a caption reports no plotted
# values (LOW); equation extraction is lossy (MEDIUM); a cell/prose quote is HIGH.
EVIDENCE_CONFIDENCE_CEILING = {
    "cell_verified": "HIGH", "prose_verified": "HIGH", "equation_verified": "MEDIUM",
    "caption_verified": "LOW", "unverified": "LOW",
}
CONFIDENCE_RANK = {"HIGH": 2, "MEDIUM": 1, "LOW": 0, "": 0}
_CONFIDENCE_BY_RANK = {2: "HIGH", 1: "MEDIUM", 0: "LOW"}
# A recomputed calculation only corroborates; it enters at LOW and can only lift.
_CORROBORATING_CALC = ("recomputed_ok",)


def evidence_support(evidence_class: str, grader_evidence_class: str = "unverified",
                     calc_class: str = "not_applicable") -> tuple[str, tuple[str, ...]]:
    """(confidence_ceiling, sources) -- pure, no severity involved. Starts at the
    strongest citation's own ceiling and lifts one step per further independent source,
    capped at HIGH."""
    graded: list[tuple[str, int]] = []
    if evidence_class != "unverified":
        graded.append((evidence_class,
                       CONFIDENCE_RANK[EVIDENCE_CONFIDENCE_CEILING.get(evidence_class, "HIGH")]))
    if grader_evidence_class != "unverified":
        graded.append((f"grader:{grader_evidence_class}",
                       CONFIDENCE_RANK[EVIDENCE_CONFIDENCE_CEILING.get(grader_evidence_class,
                                                                       "HIGH")]))
    if calc_class in _CORROBORATING_CALC:
        graded.append(("harness_recomputed", 0))
    if not graded:
        return "LOW", ()
    lifted = min(2, max(rank for _, rank in graded) + len(graded) - 1)
    return _CONFIDENCE_BY_RANK[lifted], tuple(label for label, _ in graded)


# Self-consistency caps, keyed on the lens's OWN classification of its own finding.
BASELINE_CAP = {"OPTIONAL_COMPARISON": "NOTE", "USEFUL_CONTROL": "MINOR",
                "IMPORTANT_MISSING_BASELINE": "MAJOR", "CENTRAL_VALIDITY_THREAT": "FATAL",
                "NOT_APPLICABLE": "FATAL", "": "FATAL"}
PRIOR_ART_CAP = {"PAPER_INTERNAL": "FATAL", "EXTERNAL_VERIFIED": "MAJOR",
                 "REVIEWER_INFERENCE": "MINOR", "NOT_APPLICABLE": "FATAL", "": "FATAL"}

# Fixed order so identical inputs always name the identical binding cap.
_CAP_ORDER = ("lens", "grader", "verdict", "confidence", "self_contradictory_confidence",
             "candidate_class", "baseline_class", "prior_art_basis",
             "falsification_conceded", "no_impact_statement", "no_steelman",
             "grader_cannot_cite", "fatal_needs_corroboration")


def derive(*, lens_severity: str, verification_state: str, calc_class: str,
          graded: bool, grade_verdict: str = "", grade_severity: str = "",
          confidence: str = "", falsification_survived: bool = True,
          has_impact_statement: bool = True, has_steelman: bool = True,
          evidence_class: str = "unverified",
          grader_evidence_class: str = "unverified",
          lens_confidence: str = "", candidate_class: str = "",
          baseline_class: str = "", prior_art_basis: str = "") -> tuple[str, str, str, str]:
    """(finding_class, counted_severity, binding_cap, derivation) -- the ONE function
    deciding what a finding counts as, from data alone. Grading can only DEMOTE, never
    promote: `RANK[counted_severity] <= RANK[lens_severity]` always (self-check swept)."""
    caps: dict[str, str] = {"lens": lens_severity}

    if calc_class == "recomputed_mismatch":
        caps["lens"] = min(caps["lens"], "MINOR", key=lambda s: RANK[s])
    if verification_state == "incomplete":       # 'legacy' is deliberately uncapped here
        caps["lens"] = "NOTE"

    if candidate_class:
        caps["candidate_class"] = CANDIDATE_CAP.get(candidate_class, "FATAL")
    if baseline_class:
        caps["baseline_class"] = BASELINE_CAP.get(baseline_class, "FATAL")
    if prior_art_basis:
        caps["prior_art_basis"] = PRIOR_ART_CAP.get(prior_art_basis, "FATAL")

    # A lens declaring LOW confidence and asserting FATAL/MAJOR anyway contradicts itself.
    if lens_confidence == "LOW" and lens_severity in ("FATAL", "MAJOR"):
        caps["self_contradictory_confidence"] = "MINOR"

    ceiling, support = evidence_support(evidence_class, grader_evidence_class, calc_class)

    if not graded:
        # Evidence type bounds CONFIDENCE, and that ceiling bounds severity: a
        # caption-only concern is held to MINOR (LOW), not flattened to NOTE.
        caps["confidence"] = _CONFIDENCE_CAP[ceiling]
        binding = min((k for k in _CAP_ORDER if k in caps), key=lambda k: RANK[caps[k]])
        return ("UNGRADED", caps[binding], binding,
                f"ungraded; lens severity {lens_severity} capped by {binding}="
                f"{caps[binding]}; evidence supports at most {ceiling} confidence "
                f"from {'+'.join(support) or 'nothing'}")

    caps["verdict"] = _VERDICT_CAP.get(grade_verdict, "NOTE")
    if grade_severity in ("FATAL", "MAJOR", "MINOR"):     # "NONE" is the verdict cap's job
        caps["grader"] = grade_severity
    effective = min(confidence or "LOW", ceiling, key=lambda c: CONFIDENCE_RANK[c])
    caps["confidence"] = _CONFIDENCE_CAP.get(effective, "MINOR")

    structural_hit = False
    if grade_verdict == "CONFIRMED" and not falsification_survived:
        caps["falsification_conceded"], structural_hit = "MINOR", True
    if grade_verdict in ("CONFIRMED", "PLAUSIBLE") and not has_impact_statement:
        caps["no_impact_statement"], structural_hit = "MINOR", True
    if grade_verdict == "CONFIRMED" and lens_severity in ("FATAL", "MAJOR") and not has_steelman:
        caps["no_steelman"], structural_hit = "MINOR", True
    if grade_verdict == "CONFIRMED" and grader_evidence_class == "unverified":
        caps["grader_cannot_cite"], structural_hit = "MINOR", True
    # FATAL needs TWO independent checks; a recomputed calculation counts as one of them.
    cells = sum(1 for c in (evidence_class, grader_evidence_class) if c == "cell_verified")
    if not (cells >= 2 or (cells >= 1 and calc_class == "recomputed_ok")):
        caps["fatal_needs_corroboration"] = "MAJOR"

    # A weak (LOW-confidence) refutation must not zero out a lens's own FATAL/MAJOR.
    if grade_verdict == "REFUTED" and confidence == "LOW":
        grade_verdict = "PLAUSIBLE"
        caps["verdict"] = _VERDICT_CAP["PLAUSIBLE"]

    binding = min((k for k in _CAP_ORDER if k in caps), key=lambda k: RANK[caps[k]])
    counted = caps[binding]
    finding_class = _CLASS.get(grade_verdict, "OPEN_QUESTION")
    if finding_class == "CONFIRMED_FINDING" and structural_hit:
        # CONFIRMED while conceding a structural gap contradicts itself: resolved downward.
        finding_class = "PLAUSIBLE_CONCERN"
    return (finding_class, counted, binding,
           f"graded {grade_verdict}/{grade_severity}/{confidence}; evidence supports at "
           f"most {ceiling} confidence from {'+'.join(support) or 'nothing'}, so effective "
           f"confidence {effective}; counted={counted} bound by {binding}")


# =============================================================================================
# PART 2 -- AUDIT (S2): reading units, prompts, quote verification, part composition
# =============================================================================================
LENSES = tuple(P.LENSES)
# THE DEFAULT, not the live value -- `budget_chars()` reads `SH_AUDIT_BUDGET_CHARS` at call
# time through `harness.report`, so a printed budget can never drift from the one used.
SECTION_BUDGET_CHARS = 70_000
_CELL_REF = re.compile(r"T(\d+):r(\d+):c(\d+)")
# The four admissible shapes for `evidence_ref`; anything else names no checkable location.
_PAGE_REF = re.compile(r"p\d+", re.I)
_FIG_REF = re.compile(r"F(\d+)")
_EQ_REF = re.compile(r"E(\d+)")
_QUOTE_MIN = 8
_MEANINGLESS_QUOTE = re.compile(r"^[^0-9a-z]*$")   # no letter/digit supports no claim
_WS = re.compile(r"\s+")


def budget_chars() -> int:
    from . import report as _report   # deferred: report.py imports RANK from here at top level
    return _report.budget_chars()


def _flat(s: str) -> str:
    return _WS.sub("", (s or "").lower())


def _enum(v, vocab: tuple[str, ...], default: str = "") -> str:
    """Uppercase and clamp to a closed vocabulary; garbage becomes `default`."""
    s = str(v or "").strip().upper()
    return s if s in vocab else default


def _prose(v, n: int = 4000) -> str:
    return str(v or "").strip()[:n]


def _origin_from_ref(ref: str) -> str:
    """PAPER_TABLE/TEXT/FIGURE/EQUATION from the SHAPE of `ref` alone, never from the lens."""
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
    """Collapse to one line and cap the length -- stops a lens-chosen `finding_id`/`title`
    from forging a markdown heading or table row in the report."""
    return " ".join((s or "").split())[:n]


def source_units(doc: PaperDoc) -> tuple[tuple[int, str, str], ...]:
    """The paper as SEPARATE per-section units, never concatenated (a join would let a
    quote straddling a seam verify against text the PDF never contains). The third
    element is the same text with soft-hyphens removed."""
    out = []
    for section in doc.sections:
        if not (section.text or "").strip():
            continue
        flat, offsets = flatten(section.text)
        projected, _index = soft_hyphen_projection(flat, offsets, section.text)
        out.append((section.section_idx, flat, projected))
    return tuple(out)


def _units(corpus) -> tuple[tuple[int, str, str], ...]:
    """Accept the unit tuple, or a single pre-flattened string as one anonymous unit."""
    if isinstance(corpus, str):
        return ((-1, corpus, corpus),)
    return tuple(u if len(u) >= 3 else (u[0], u[1], u[1]) for u in corpus)


_NUMBER_TABLE_IDX = re.compile(r"^T(\d+):")


def _part_pages(part) -> set[int]:
    return {p for s in part.sections
           for p in range(s.page_start or 0, (s.page_end or s.page_start or 0) + 1)}


def _part_table_idxs(doc: PaperDoc, part) -> set[int]:
    """Table indices a part's OWN text refers to by printed label ("Table 8")."""
    text = " ".join(s.text for s in part.sections)
    out = set()
    for t in doc.tables:
        label = (t.label or "").strip()
        if label and re.search(rf"\bTable\s+{re.escape(label)}\b", text, re.IGNORECASE):
            out.add(t.table_idx)
    return out


def _in_part(n, pages: set[int], tables: set[int]) -> bool:
    m = _NUMBER_TABLE_IDX.match(n.table_ref or "")
    if m:
        return int(m.group(1)) in tables
    # An unlocated number (page 0, unrecorded) can never be safely excluded.
    return not n.page or n.page in pages


def render_numbers(doc: PaperDoc, part=None) -> str:
    """Every reported number, or -- for one PART of a long paper -- only the numbers that
    part's own sections could plausibly ground. `part=None` sees every number."""
    pages = _part_pages(part) if part is not None else set()
    tables = _part_table_idxs(doc, part) if part is not None else set()
    out = []
    for n in doc.reported_numbers:
        if part is not None and not _in_part(n, pages, tables):
            continue
        where = n.table_ref or f"p{n.page}"
        if n.table_ref:
            out.append(f"- [{where}] {n.method} · {n.metric} = {n.value}"
                       f"{f' (±{n.seeds_or_variance})' if n.seeds_or_variance else ' (NO VARIANCE REPORTED)'}"
                       f"  << {n.benchmark}")
        else:
            out.append(f"- [{where}] prose claim: \"{n.source_quote}\"")
    return "\n".join(out)


def context(doc: PaperDoc, part=None, anchor=None) -> dict[str, str]:
    """Everything a lens prompt is built from for one pass. `part=None` renders the whole
    paper in one pass."""
    if part is None:
        whole = paper.plan_reading(list(doc.sections), max(400, budget_chars()))
        sections_text = paper.render_part(whole[0]) if whole else ""
    else:
        sections_text = (paper.render_part_with_anchor(part, anchor) if anchor is not None
                         else paper.render_part(part))
    return {
        "sections_text": sections_text,
        "tables_text": paper.render_tables(doc.tables),
        "claims_text": "(none pre-extracted — identify the paper's claims yourself "
                       "from the sections below; that judgement is part of your job)",
        "numbers_text": render_numbers(doc, part),
        "pdf_path": doc.source_path,
        "figures_text": paper.render_figures(doc.figures),
        "equations_text": paper.render_equations(doc.equations),
    }


def _header(lens: str, pid: str, unit_id: str = "", out_name: str = "") -> str:
    """The driver-facing preamble: the one rule that holds on every channel is that a
    reading never writes its own output file."""
    out_name = out_name or f"{lens}.json"
    unit_id = unit_id or lens
    return f"""<!-- generated by `run.py stage audit --paper {pid}` — do not edit -->

# Audit lens: `{unit_id}`  ·  paper `{pid}`

**Driver instructions.** Perform this audit now and return a single JSON object matching
the schema at the end of this file. Print it to standard output unless whoever dispatched
you named a different destination.

**Never write `{out_name}` directly.** That path is the harness's to write, after
validation; a hand-placed file carries no provenance and is refused by `lens_is_accepted`.

Run each lens in a SEPARATE session and do not let one lens's findings influence another.

Every finding is re-verified when the report is built: a finding whose `evidence_quote`
is not actually present in this paper, or whose cited cell does not contain what it says,
is DROPPED. Quote exactly.

---

"""


# A paper that fits in one pass is one unit per lens. A paper that does not is N part
# units plus one cross-part SYNTHESIS unit per lens; `compose_lens` assembles
# `audit/<lens>.json` from them so nothing downstream needs to know how it was traversed.
SYNTHESIS_ID = "synthesis"
COMPOSED_WRITER = "composed_from_parts"   # distinct from every delegation mode


def part_id_of(number: int) -> str:
    return f"part-{number:02d}"


def _part_number(part_id: str) -> int:
    """`part-03` -> 3. 0 for anything that is not a part id, including SYNTHESIS_ID."""
    tail = (part_id or "").rpartition("-")[2]
    return int(tail) if tail.isdigit() else 0


class AuditUnit(NamedTuple):
    lens: str
    kind: str                  # whole | part | synthesis
    part_id: str               # 'part-01' | 'synthesis' | '' for a whole-paper unit
    prompt_path: Path
    out_path: Path
    manifest_path: Path        # under audit/reading/, never beside audit/*.json

    @property
    def unit_id(self) -> str:
        return self.lens if self.kind == "whole" else f"{self.lens}/{self.part_id}"

    @property
    def sidecar_path(self) -> Path:
        return self.out_path.with_suffix(".driver.json")


def plan_for(doc: PaperDoc) -> paper.ReadingPlan:
    """The reading strategy for this paper, at the budget the prompts will actually use."""
    return paper.plan(doc, budget_chars())


def units_for(root: Path, lenses: tuple[str, ...], plan: paper.ReadingPlan) -> list[AuditUnit]:
    """Every reading this paper needs, in dispatch order: a lens's parts before its
    synthesis."""
    audit = root / "audit"
    pdir, mdir = audit / "prompts", audit / "reading"
    out: list[AuditUnit] = []
    n = max(1, plan.coverage.parts)
    for lens in lenses:
        if n == 1:
            out.append(AuditUnit(lens, "whole", "", pdir / f"{lens}.md",
                                 audit / f"{lens}.json",
                                 mdir / f"{lens}.manifest.json"))
            continue
        for i in range(1, n + 1):
            pid_ = part_id_of(i)
            out.append(AuditUnit(lens, "part", pid_, pdir / lens / f"{pid_}.md",
                                 audit / lens / "parts" / f"{pid_}.json",
                                 mdir / lens / f"{pid_}.manifest.json"))
        out.append(AuditUnit(lens, "synthesis", SYNTHESIS_ID,
                             pdir / lens / f"{SYNTHESIS_ID}.md",
                             audit / lens / f"{SYNTHESIS_ID}.json",
                             mdir / lens / f"{SYNTHESIS_ID}.manifest.json"))
    return out


# Accepted writers: every delegation mode plus COMPOSED_WRITER for assembled parts.
_ACCEPTED_WRITERS = agent.ROLES["lens"].writers + (COMPOSED_WRITER,)


def _sealed(path: Path) -> tuple[bool, str]:
    """Does a sealed provenance sidecar beside `path` describe `path`'s CURRENT bytes?"""
    return agent.verify_seal(path, accepted_writers=_ACCEPTED_WRITERS)


def unit_is_accepted(unit: AuditUnit) -> tuple[bool, str]:
    """Sealed AND produced against the prompt CURRENTLY on disk -- a part whose span moved
    has a sealed output answering a question nobody is asking any more. A whole-paper
    unit is deliberately not prompt-pinned."""
    ok, why = _sealed(unit.out_path)
    if not ok or unit.kind == "whole":
        return ok, why
    fresh, note = agent.prompt_is_unchanged(unit.sidecar_path, unit.prompt_path)
    if not fresh:
        return False, note
    return True, ""


def lens_is_accepted(root: Path, lens: str) -> tuple[bool, str]:
    """A lens result counts only if the HARNESS recorded writing it: file existence alone
    is not provenance."""
    ok, why = _sealed(root / "audit" / f"{lens}.json")
    return ok, ("no lens file" if why == "no output file" else why)


def accept_lens(cfg: Config, pid: str, lens: str, raw: str, *,
                reviewer: str = "", tool_policy: str = "unrecorded",
                mode: str = "MANUAL") -> dict:
    """Validate and persist a HAND-WRITTEN lens file through the same gate the automated
    dispatch path uses."""
    report = agent.parse_lens_json(raw, lens)
    root = state.project_dir(cfg, pid)
    out = root / "audit" / f"{lens}.json"
    return agent.seal(out, report.model_dump(), mode=mode, reviewer=reviewer,
                      tool_policy=tool_policy,
                      extra={"lens": lens, "paper_id": pid, "findings": len(report.findings)})


def _locate(quote: str, corpus) -> int | None:
    """Which parsed section contains this quote, or None. Used only to LABEL a candidate."""
    q = _flat(quote)
    if not q:
        return None
    return next((idx for idx, unit, _proj in _units(corpus) if q in unit), None)


def synthesis_inputs(doc: PaperDoc, lens: str,
                     units: list[AuditUnit]) -> dict[int, list[dict]]:
    """That lens's own VERIFIED part-local observations, keyed by part number -- never an
    unverified quotation, so synthesis cannot reason over text not in the paper."""
    corpus = source_units(doc)
    by_idx = {t.table_idx: t for t in doc.tables}
    out: dict[int, list[dict]] = {}
    for unit in units:
        if unit.lens != lens or unit.kind != "part":
            continue
        number = _part_number(unit.part_id)
        try:
            data = state.read_json(unit.out_path)
        except Exception:
            out[number] = []
            continue
        kept: list[dict] = []
        for f in (data.get("findings") or []) if isinstance(data, dict) else []:
            if not isinstance(f, dict):
                continue
            quote = str(f.get("evidence_quote") or "").strip()
            ref = str(f.get("evidence_ref") or "").strip()
            if not _substantiated(quote, ref, corpus, by_idx):
                continue
            kept.append({
                "evidence_quote": quote, "evidence_ref": ref,
                "statement": str(f.get("statement") or f.get("title") or ""),
                "finding_id": str(f.get("finding_id") or ""),
                "section_idx": _locate(quote, corpus),
                "page": None,
            })
        out[number] = kept
    return out


def _write_manifest(unit: AuditUnit, *, pid: str, part, anchor, prompt_text: str,
                    budget: int, inputs: dict | None = None) -> dict:
    """WHAT WENT IN, written when the prompt is -- makes the part-isolation claim
    checkable (span, anchor digest, prompt digest). NOT sealed: an input record, not an
    attestation about a validated output."""
    rec = {
        "paper_id": pid, "lens": unit.lens, "unit_id": unit.unit_id,
        "kind": unit.kind, "part_id": unit.part_id,
        "prompt_path": str(unit.prompt_path),
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "budget_chars": budget,
        "anchor_sha256": (hashlib.sha256(anchor.render().encode("utf-8")).hexdigest()
                          if anchor is not None and unit.kind != "whole" else ""),
        "section_ids": ([s.section_idx for s in part.sections] if part is not None
                        else "whole-paper"),
        "slices": ([list(t) for t in part.slices] if part is not None else []),
        "part_number": getattr(part, "number", 1) if part is not None else 1,
        "part_total": getattr(part, "total", 1) if part is not None else 1,
        "chars": getattr(part, "chars", 0) if part is not None else 0,
        "ts": state.now(),
    }
    if inputs is not None:
        rec["synthesis_inputs"] = inputs
    unit.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    state.write_json(unit.manifest_path, rec)
    return rec


def compose_lens(cfg: Config, pid: str, lens: str,
                 units: list[AuditUnit]) -> dict | None:
    """Assemble `audit/<lens>.json` from that lens's sealed part+synthesis artifacts, or
    None while any unit is missing -- a lens is never half-composed. Findings carried
    verbatim apart from two harness-written labels (`source_part`, and a disambiguated
    finding id where two readings chose the same one)."""
    mine = [u for u in units if u.lens == lens and u.kind in ("part", "synthesis")]
    if not mine or not all(unit_is_accepted(u)[0] for u in mine):
        return None
    root = state.project_dir(cfg, pid)
    findings: list[dict] = []
    composed_from: list[dict] = []
    seen_ids: set[str] = set()
    notes: list[str] = []
    unasked = ""
    for unit in mine:
        data = state.read_json(unit.out_path)
        side = state.read_json(unit.sidecar_path)
        raw = (data.get("findings") or []) if isinstance(data, dict) else []
        for i, f in enumerate(raw):
            if not isinstance(f, dict):
                continue
            f = dict(f)
            fid = str(f.get("finding_id") or "").strip() or f"{lens}-{unit.part_id}-{i + 1:02d}"
            if fid in seen_ids:
                fid = f"{unit.part_id}:{fid}"
            seen_ids.add(fid)
            f["finding_id"] = fid
            f["source_part"] = unit.part_id           # HARNESS-WRITTEN
            findings.append(f)
        if isinstance(data, dict):
            note = str(data.get("notes") or "").strip()
            if note:
                notes.append(f"[{unit.part_id}] {note}")
            question = str(data.get("unasked_question") or "").strip()
            # The synthesis's answer wins over a part reader's (only saw one span).
            if question and (unit.kind == "synthesis" or not unasked):
                unasked = question
        composed_from.append({
            "unit_id": unit.unit_id, "kind": unit.kind, "path": str(unit.out_path),
            "content_sha256": side.get("content_sha256", ""),
            "written_by": side.get("written_by", ""),
            "prompt_sha256": side.get("prompt_sha256", ""),
            "model_reported": (side.get("envelope") or {}).get("model_reported", ""),
            "findings": len(raw),
        })
    out = root / "audit" / f"{lens}.json"
    payload = {"lens": lens, "schema_version": 2, "findings": findings,
              "unasked_question": unasked, "notes": "  ".join(notes)}
    return agent.seal(out, payload, mode="MANUAL", extra={
        "lens": lens, "paper_id": pid, "written_by": COMPOSED_WRITER,
        "composed_from": composed_from,
        "parts": sum(1 for u in mine if u.kind == "part"),
        "synthesis": any(u.kind == "synthesis" for u in mine),
        "findings": len(findings),
        "tool_policy": "composed; each reading in composed_from records its own policy",
    })


def reading_record(cfg: Config, pid: str, doc: PaperDoc,
                   lenses: tuple[str, ...] = LENSES) -> dict:
    """How much of this paper reached a reader, and how it was carried out.
    `reader_visible_fraction` is the ceiling on every recall claim this system makes; it
    is not issue recall or extraction quality (`extracted_text_fraction` is that ceiling)."""
    root = state.project_dir(cfg, pid)
    plan = plan_for(doc)
    units = units_for(root, lenses, plan)
    cov = plan.coverage
    syntheses = [u for u in units if u.kind == "synthesis"]
    pages: set[int] = set()   # every page a recovered section COVERS, not just its start
    for sec in doc.sections:
        if not (sec.text or "").strip():
            continue
        last = sec.page_end if sec.page_end >= sec.page_start else sec.page_start
        pages.update(range(sec.page_start, last + 1))
    pages.discard(0)
    return {
        "extracted_prose_chars": cov.extracted_prose_chars,
        "pages_with_text": len(pages),
        "pages_total": doc.n_pages or 0,
        "extracted_text_fraction": (len(pages) / doc.n_pages) if doc.n_pages else None,
        "reader_visible_chars": cov.part_local_chars,
        "reader_visible_fraction": cov.part_local_fraction,
        "anchor_chars": cov.anchor_chars,
        "anchor_repeat_chars": cov.anchor_repeat_chars,
        "anchor_repeat_fraction": cov.anchor_overhead_fraction,
        "number_of_parts": cov.parts,
        "lenses_total": len(lenses),
        "lenses_completed": sum(1 for ln in lenses if lens_is_accepted(root, ln)[0]),
        "lens_syntheses_required": len(syntheses),
        "lens_syntheses_completed": sum(1 for u in syntheses if unit_is_accepted(u)[0]),
    }


def missing_reading_artifacts(cfg: Config, pid: str,
                              lenses: tuple[str, ...] = LENSES) -> list[str]:
    """Reading units this paper needs and does not have; empty is the only clean answer."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return ["paper/doc.json"]
    doc = PaperDoc(**state.read_json(doc_path))
    units = units_for(root, lenses, plan_for(doc))
    missing = [u.unit_id for u in units if not unit_is_accepted(u)[0]]
    missing += [f"{ln}.json" for ln in lenses if not lens_is_accepted(root, ln)[0]]
    return missing


def run_audit(cfg: Config, pid: str, lenses: tuple[str, ...] = LENSES) -> dict:
    """Write one prompt file per reading unit, and compose any lens whose units are all
    in. Calls no model. A synthesis prompt is not written until its parts are sealed."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))
    state.set_phase(cfg, pid, "audit")

    plan = plan_for(doc)
    units = units_for(root, lenses, plan)
    by_number = {part.number: part for part in plan.parts}
    pdir = root / "audit" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}
    deferred: list[str] = []
    for unit in units:
        part = by_number.get(_part_number(unit.part_id)) if unit.kind == "part" else None
        if unit.kind == "synthesis":
            siblings = [u for u in units if u.lens == unit.lens and u.kind == "part"]
            if not all(unit_is_accepted(u)[0] for u in siblings):
                deferred.append(unit.unit_id)
                continue
            brief = paper.synthesis_brief(pid, unit.lens, plan.anchor,
                                            synthesis_inputs(doc, unit.lens, units))
            ctx = context(doc)
            body = P.build_synthesis(
                unit.lens, doc.title, brief.render(),
                "\n".join(f"  - {t}" for t in paper.SYNTHESIS_TARGETS),
                tables_text=ctx["tables_text"], figures_text=ctx["figures_text"],
                equations_text=ctx["equations_text"], pdf_path=ctx["pdf_path"])
            inputs = {"parts_read": brief.parts_read,
                      "candidates": len(brief.candidates),
                      "candidate_finding_ids": [c["finding_id"] for c in brief.candidates]}
        else:
            ctx = context(doc, part, plan.anchor if unit.kind == "part" else None)
            note = P.part_note(part.label) if part is not None else ""
            body = P.build(unit.lens, doc.title, reading_note=note, **ctx)
            inputs = None
        # Sanitised: a real prompt carries raw PDF-extraction control bytes.
        body = paper.sanitise_controls(
            _header(unit.lens, pid, unit.unit_id, unit.out_path.name) + body)
        unit.prompt_path.parent.mkdir(parents=True, exist_ok=True)
        unit.prompt_path.write_text(body, encoding="utf-8")
        written[unit.unit_id] = str(unit.prompt_path)
        _write_manifest(unit, pid=pid, part=part, anchor=plan.anchor, prompt_text=body,
                        budget=budget_chars(), inputs=inputs)

    done = [u.unit_id for u in units if unit_is_accepted(u)[0]]
    todo = [u.unit_id for u in units if u.unit_id not in done and u.unit_id in written]

    # Compose every lens whose readings are all in. Idempotent.
    composed: dict[str, int] = {}
    if plan.coverage.parts > 1:
        for lens in lenses:
            record = compose_lens(cfg, pid, lens, units)
            if record:
                composed[lens] = record["findings"]

    cov = plan.coverage
    state.append_log(
        cfg, pid, artifact_type="audit_prompts", phase="audit",
        headers={"lenses": list(lenses), "units": [u.unit_id for u in units],
                 "awaiting": todo, "deferred": deferred, "complete": done,
                 "parts": cov.parts,
                 "reader_visible_fraction": cov.part_local_fraction,
                 "anchor_repeat_fraction": cov.anchor_overhead_fraction,
                 "chars": sum(len(Path(v).read_text(encoding="utf-8"))
                              for v in written.values())},
        path=str(pdir),
    )
    return {
        "paper_id": pid, "title": doc.title,
        "prompts": written,
        "units": [{"unit_id": u.unit_id, "lens": u.lens, "kind": u.kind,
                   "part_id": u.part_id, "prompt": str(u.prompt_path),
                   "out": str(u.out_path)} for u in units],
        "awaiting": todo, "deferred": deferred, "complete": done,
        "lenses_complete": [ln for ln in lenses if lens_is_accepted(root, ln)[0]],
        "composed": composed, "parts": cov.parts,
        "reader_visible_fraction": cov.part_local_fraction,
        "next": (f"Read each prompt in audit/prompts/, perform that reading, and return "
                 f"its JSON. Awaiting: {', '.join(todo) or 'none'}."
                 + (f" Deferred until their parts are in: {', '.join(deferred)}."
                    if deferred else ""))
        if (todo or deferred) else "Every reading is in; run synthesize_report.",
    }


def load_reports(cfg: Config, pid: str,
                 doc: PaperDoc) -> tuple[list[LensReport], int, list[str]]:
    """Load every lens result, dropping findings that cannot be substantiated.
    (reports, n_dropped, invalid_lenses) -- a lens named in `invalid_lenses` still yields
    a report but MUST NOT be read as "this lens ran and found nothing"."""
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
    """(evidence_class, verified_observation) -- WHAT THE HARNESS ITSELF CONFIRMED, never
    the lens's own word. Generated, never copied, so a reader can re-run the same
    comparison from `doc.json`. `max_page`, when given, bounds a `p<N>` reference."""
    q = _flat(quote)
    if not q or _MEANINGLESS_QUOTE.fullmatch(q):
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
            return "unverified", ""
    if len(q) < _QUOTE_MIN:
        return "unverified", ""
    # WITHIN ONE SECTION -- a quote spanning two sections is not in the paper.
    units = _units(corpus)
    hit = next((idx for idx, unit, _proj in units if q in unit), None)
    dehyphenated = False
    if hit is None:
        # Typesetter hyphen, tried only after the exact search fails.
        probe = soft_hyphen_projection(*_self_projection(q))[0]
        if len(probe) >= _QUOTE_MIN:
            hit = next((idx for idx, _unit, proj in units if probe in proj), None)
            dehyphenated = hit is not None
    if hit is None:
        return "unverified", ""
    where = f"section_idx {hit}" if hit >= 0 else "the parsed section text"
    # "VERBATIM" is dropped when false: a hyphen-recovered match is not character-for-character.
    how = "occurs verbatim inside" if not dehyphenated else "occurs inside"
    note = ("" if not dehyphenated else
            " — matched after removing the hyphens a line break inserted into the PDF, so "
            "the quotation differs from the extracted characters only by that hyphenation "
            "and is NOT a character-for-character match")
    return "prose_verified", (
        f"The quoted text {how} a single parsed section ({where}){note}; the lens "
        f"cited {ref}, which is NOT checked — only the section containing the quote is. "
        f"Verified as a substring of one section, not across sections and not as a page.")


def _substantiated(quote: str, ref: str, corpus: str | Sequence[tuple[int, str]],
                   by_idx: dict) -> bool:
    """Is this quote really in the paper, and if it cites a cell, is it that cell? The
    boolean shadow of `verify_evidence`."""
    return verify_evidence(quote, ref, corpus, by_idx)[0] != "unverified"


def weakest_evidence_class(classes: Sequence[str]) -> str:
    """The evidence class a MULTI-LOCATION concern is held to: its weakest side. NOT fed
    into `evidence_support` as a second source -- that would let two citations corroborate
    each other, raising rather than capping."""
    order = {c: CONFIDENCE_RANK[EVIDENCE_CONFIDENCE_CEILING.get(c, "HIGH")]
             for c in classes}
    return min(classes, key=lambda c: order[c]) if classes else "unverified"


def _address_identity(f: Finding) -> tuple:
    """This concern's IDENTITY: the ORDERED locations it was established from."""
    sides = [(f.evidence_ref, _flat(f.evidence_quote))]
    sides += [(e.evidence_ref, _flat(e.evidence_quote)) for e in f.additional_evidence]
    return tuple(sides)


def _concern_identity(f: Finding) -> tuple:
    """WHAT KIND of concern this is, in closed vocabulary only -- the lens's OWN
    classification, not `scientific_class`."""
    return (f.scientific_class, f.discrepancy_type, f.baseline_class,
            f.candidate_class, f.prior_art_basis)


def deduplicate(findings: list[Finding]) -> tuple[list[Finding], int]:
    """Fold concerns that resolved to the identical address set (over addresses and
    closed vocabulary, never over text; run AFTER verification). The survivor keeps its
    own identity and records the ids folded into it."""
    kept: list[Finding] = []
    first_by_key: dict[tuple, Finding] = {}
    merged = 0
    for f in findings:
        key = (f.lens, _concern_identity(f), _address_identity(f))
        first = first_by_key.get(key)
        if first is None:
            first_by_key[key] = f
            kept.append(f)
            continue
        merged += 1
        if f.finding_id and f.finding_id not in first.merged_from:
            first.merged_from.append(f.finding_id)
    return kept, merged


def _coerce(lens: str, data, corpus: str | Sequence[tuple[int, str]],
            by_idx: dict, max_page: int = 0, *,
            by_figure: dict[int, Figure] | None = None,
            by_equation: dict[int, Equation] | None = None) -> tuple[LensReport, int, bool]:
    """(report, n_dropped, valid) -- `valid` is whether the FILE ITSELF was the right
    shape, independent of whether any individual finding survived verification."""
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        note = ("(lens file was not a JSON object)" if not isinstance(data, dict)
                else "(lens file has no 'findings' list)")
        return LensReport(lens=lens, notes=note), 0, False
    verify = functools.partial(verify_evidence, by_figure=by_figure, by_equation=by_equation)
    schema_version = data.get("schema_version")     # the REPORT's version, not each finding's
    findings, dropped = [], 0
    for i, f in enumerate(data.get("findings") or []):
        if not isinstance(f, dict):
            dropped += 1
            continue
        statement = str(f.get("statement") or "").strip()
        quote = str(f.get("evidence_quote") or "").strip()
        ref = str(f.get("evidence_ref") or "").strip()
        evidence_class, observation = verify(quote, ref, corpus, by_idx, max_page)
        # EVERY SIDE, OR NONE -- a concern from two locations is checkable only if both are.
        extra, extra_holds = [], True
        for side in (f.get("additional_evidence") or []):
            if not isinstance(side, dict):
                extra_holds = False
                break
            side_quote = str(side.get("evidence_quote") or "").strip()
            side_ref = str(side.get("evidence_ref") or "").strip()
            side_class, side_obs = verify(side_quote, side_ref, corpus, by_idx, max_page)
            if side_class == "unverified":
                extra_holds = False
                break
            extra.append(EvidencePointer(
                role=_oneline(str(side.get("role") or ""), 80),
                evidence_quote=side_quote, evidence_ref=side_ref,
                evidence_class=side_class, verified_observation=side_obs))
        if not statement or evidence_class == "unverified" or not extra_holds:
            dropped += 1
            continue
        if extra:
            observation = " ".join(
                [observation] + [f"Further location cited ({e.role or 'unlabelled'}), "
                                 f"{e.evidence_ref}: {e.verified_observation}" for e in extra])
            evidence_class = weakest_evidence_class(
                [evidence_class] + [e.evidence_class for e in extra])
        sev = str(f.get("severity") or "").upper()
        sev = sev if sev in SEVERITIES else "MINOR"
        calc = f.get("independent_calculation")
        calc = calc if isinstance(calc, dict) else {}
        verification_state = pass_b_state(f, sev, schema_version)
        calc_class = recheck_calculation(calc, verify, corpus, by_idx, max_page)
        candidate_class = _enum(f.get("candidate_class"), CANDIDATE_CLASSES)
        confidence = _enum(f.get("confidence"), CONFIDENCES)
        baseline_class = _enum(f.get("baseline_class"), BASELINE_CLASSES)
        prior_art_basis = _enum(f.get("prior_art_basis"), PRIOR_ART_BASES)
        evidence_ceiling, evidence_sources = evidence_support(evidence_class, calc_class=calc_class)
        finding_class, counted_severity, binding_cap, derivation = derive(
            lens_severity=sev, verification_state=verification_state,
            calc_class=calc_class, graded=False, evidence_class=evidence_class,
            lens_confidence=confidence, candidate_class=candidate_class,
            baseline_class=baseline_class, prior_art_basis=prior_art_basis)
        if counted_severity == sev:
            # Nothing capped it -- blank so a reader can tell "verified equal" from "never assessed".
            finding_class, counted_severity, binding_cap, derivation = "UNGRADED", "", "", ""
        findings.append(Finding(
            finding_id=_oneline(str(f.get("finding_id") or f"{lens}-{i + 1:02d}"), 60),
            lens=lens, severity=sev,
            title=_oneline(str(f.get("title") or statement), 90), statement=statement,
            target=str(f.get("target") or ""), evidence_quote=quote, evidence_ref=ref,
            counter_explanations=[str(c) for c in (f.get("counter_explanations") or [])
                                 if isinstance(c, (str, int, float))],
            verifiable_by_experiment=bool(f.get("verifiable_by_experiment")),
            additional_evidence=extra,
            claim=str(f.get("claim") or f.get("target") or "").strip(),
            reasoning=str(f.get("reasoning") or statement).strip(),
            conclusion=str(f.get("conclusion") or statement).strip(),
            severity_rationale=str(f.get("severity_rationale") or "").strip(),
            candidate_class=candidate_class,
            confidence=confidence,
            baseline_class=baseline_class,
            prior_art_basis=prior_art_basis,
            discrepancy_type=_enum(f.get("discrepancy_type"), DISCREPANCY_TYPES),
            what_the_paper_says=_prose(f.get("what_the_paper_says")),
            alternative_interpretation=_prose(f.get("alternative_interpretation")),
            why_alternative_fails=_prose(f.get("why_alternative_fails")),
            steelman=_prose(f.get("steelman")),
            effect_on_claim=_prose(f.get("effect_on_claim")),
            recommended_resolution=_prose(f.get("recommended_resolution")),
            independent_calculation=calc,
            # Everything below is HARNESS-WRITTEN, never read from `f`.
            evidence_class=evidence_class,
            verified_observation=observation,
            scientific_class=taxonomy.classify(
                lens=lens, discrepancy_type=_enum(f.get("discrepancy_type"), DISCREPANCY_TYPES),
                baseline_class=baseline_class, candidate_class=candidate_class),
            verification_state=verification_state,
            calc_class=calc_class,
            evidence_origin=_origin_from_ref(ref),
            origin_consistency=("consistent"
                                if _enum(f.get("evidence_origin"), EVIDENCE_ORIGINS)
                                in ("", _origin_from_ref(ref)) else "corrected"),
            cross_section=bool(extra),
            source_part=_oneline(str(f.get("source_part") or ""), 40),
            finding_class=finding_class, counted_severity=counted_severity,
            binding_cap=binding_cap, derivation=derivation,
            evidence_ceiling=evidence_ceiling, evidence_sources=list(evidence_sources),
        ))
    findings, merged = deduplicate(findings)
    return LensReport(
        lens=lens, findings=findings, merged_duplicates=merged,
        unasked_question=str(data.get("unasked_question") or "").strip(),
        notes=str(data.get("notes") or ""),
    ), dropped, True


# =============================================================================================
# PART 3 -- GRADE (S2.5): candidate selection, prompts, sealing, report-time attach
# =============================================================================================
_SLUG = re.compile(r"[^a-zA-Z0-9_-]+")
_GRADE_PAGE_REF = re.compile(r"p(\d+)")
_LENS_RANK = {"overclaim": 3, "contradiction": 2, "confound": 1, "protocol": 0}


def slug_for(finding_id: str) -> str:
    """A filesystem/path-traversal-safe name for a candidate's prompt/output files --
    `finding_id` is lens-supplied and may contain `/`, `\\`, `..`."""
    base = _SLUG.sub("-", finding_id or "unnamed").strip("-")[:40] or "unnamed"
    return f"{base}-{hashlib.sha256((finding_id or '').encode()).hexdigest()[:8]}"


def _rank_key(f: Finding) -> tuple:
    """Most-severe-first by `counted_severity` (falling back to `severity`), then a
    fixed lens tiebreak, then the id."""
    counted = f.counted_severity or f.severity
    return (RANK.get(counted, 0), _LENS_RANK.get(f.lens, 0), f.finding_id)


def select_candidates(reports: list[LensReport], scope: str) -> list[Finding]:
    """Every substantiated finding in scope, most severe first."""
    findings = [f for r in reports for f in r.findings]
    if scope != "all":
        findings = [f for f in findings if f.severity in ("FATAL", "MAJOR")]
    return sorted(findings, key=_rank_key, reverse=True)


def _ref_sections(doc: PaperDoc, ref: str) -> set[int]:
    """The section_idx(es) an evidence_ref's own page could live in. A page ref ("p7")
    resolves directly; a table/figure/equation ref resolves via THAT object's own page."""
    ref = (ref or "").strip()
    page = None
    m = _GRADE_PAGE_REF.fullmatch(ref)
    if m:
        page = int(m.group(1))
    else:
        m = _CELL_REF.fullmatch(ref)
        if m:
            table = next((t for t in doc.tables if t.table_idx == int(m.group(1))), None)
            page = table.page if table else None
        elif re.fullmatch(r"F(\d+)", ref):
            fig = next((f for f in doc.figures if f.figure_idx == int(ref[1:])), None)
            page = fig.page if fig else None
        elif re.fullmatch(r"E(\d+)", ref):
            eq = next((e for e in doc.equations if e.equation_idx == int(ref[1:])), None)
            page = eq.page if eq else None
    if page is None:
        return set()
    return {s.section_idx for s in doc.sections
           if s.page_start <= page <= (s.page_end or s.page_start)}


def _section_render(doc: PaperDoc, refs: list[str], budget: int) -> tuple[str, str]:
    """The sections this finding's OWN evidence lives in (every evidence_ref, resolved to its
    section) and the abstract come first; the rest of the paper then fills the budget, so
    the grader can still look for refuting or already-disclosed context elsewhere."""
    wanted: set[int] = set()
    for ref in refs:
        wanted |= _ref_sections(doc, ref)
    abstract_idx = decide.abstract_section_idx(doc)
    wanted.add(abstract_idx if abstract_idx >= 0 else
              (doc.sections[0].section_idx if doc.sections else -1))
    wanted.discard(-1)

    ordered = ([s for s in doc.sections if s.section_idx in wanted]
               + [s for s in doc.sections if s.section_idx not in wanted])
    kept, withheld, used = [], [], 0
    for s in ordered:
        body = f"## {s.title}  [p{s.page_start}]\n{s.text}"
        if used + len(body) > budget and kept:
            withheld.append((s.section_idx in wanted, s.title or f"section {s.section_idx}"))
            continue
        kept.append((s.section_idx, body))
        used += len(body)
    kept.sort(key=lambda t: t[0])
    text = "\n\n".join(b for _, b in kept)
    cited = sum(1 for c, _ in withheld if c)
    note = (f"{len(withheld)} of {len(ordered)} section(s) withheld for budget "
            f"({cited} of them cited by this finding): "
            f"{', '.join(t for _, t in withheld[:8])}" if withheld
            else "the whole paper is included")
    return text, note


def build_prompts(cfg: Config, pid: str, doc: PaperDoc,
                  candidates: list[Finding]) -> dict[str, str]:
    """Regenerated every run -- never edit, like `audit/prompts/*.md`."""
    root = state.project_dir(cfg, pid)
    pdir = root / "audit" / "grade" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for f in candidates:
        slug = slug_for(f.finding_id)
        # EVERY SIDE of a multi-location concern gets its own section in scope.
        refs = [f.evidence_ref, *(side.evidence_ref for side in f.additional_evidence)]
        sections_text, note = _section_render(doc, refs, cfg.grade_budget_chars)
        body = G.build(
            claim=f.as_claim(), statement=f.statement, target=f.target,
            reasoning=f.as_reasoning(), conclusion=f.as_conclusion(),
            counter_explanations=f.counter_explanations,
            evidence_quote=f.evidence_quote, evidence_ref=f.evidence_ref,
            evidence_class=f.evidence_class, verified_observation=f.verified_observation,
            additional_evidence=[e.model_dump() for e in f.additional_evidence],
            sections_text=sections_text, tables_text=paper.render_tables(doc.tables),
            withheld_note=note,
        )
        (pdir / f"{slug}.md").write_text(body, encoding="utf-8")
        paths[slug] = str(pdir / f"{slug}.md")
    return paths


_ACCEPTED_GRADE_WRITERS = agent.ROLES["grade"].writers


def grade_is_accepted(gdir, slug: str) -> tuple[bool, str]:
    """The grading analogue of `lens_is_accepted`."""
    path = gdir / f"{slug}.json"
    ok, why = agent.verify_seal(path, accepted_writers=_ACCEPTED_GRADE_WRITERS)
    return ok, ("no grade file" if why == "no output file" else why)


def accept_grade(cfg: Config, pid: str, slug: str, raw: str, *,
                 grader: str = "", tool_policy: str = "unrecorded",
                 mode: str = "MANUAL") -> dict:
    """Validate and seal a grade produced OUTSIDE the automated dispatch path -- the
    grading analogue of `accept_lens`. Blinding is a property of what the grader was
    SHOWN (the prompt file), not which process read it."""
    grade = agent.parse_grade_json(raw)
    gdir = state.project_dir(cfg, pid) / "audit" / "grade"
    out = gdir / f"{slug}.json"
    record = agent.seal(out, grade.model_dump(), mode=mode, reviewer=grader,
                        tool_policy=tool_policy,
                        extra={"slug": slug, "paper_id": pid, "verdict": grade.verdict})
    record["grader"] = record.pop("reviewer")     # sidecar field is `grader`, never `reviewer`
    state.write_json(out.with_suffix(".driver.json"), record)
    return record


def run_grade(cfg: Config, pid: str) -> dict:
    """Write one prompt per in-scope candidate. Calls no model."""
    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}'"}
    doc = PaperDoc(**state.read_json(doc_path))
    reports, _dropped, invalid = load_reports(cfg, pid, doc)
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
    matching findings, IN PLACE. An unmatched finding keeps its ungraded baseline."""
    root = state.project_dir(cfg, pid)
    gdir = root / "audit" / "grade"
    corpus = source_units(doc)
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
            grader_evidence_class, grader_obs = verify_evidence(
                g.independent_evidence_quote, g.independent_evidence_ref, corpus, by_idx, max_page,
                by_figure=by_figure, by_equation=by_equation)
            finding_class, counted_severity, binding_cap, derivation = derive(
                lens_severity=f.severity, verification_state=f.verification_state,
                calc_class=f.calc_class, graded=True, grade_verdict=g.verdict,
                grade_severity=g.severity,
                confidence=_enum(g.confidence, CONFIDENCES),   # clamped, as at parse time above
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
            # Re-derived with the GRADER's citation in scope.
            f.evidence_ceiling, sources = evidence_support(
                f.evidence_class, grader_evidence_class, f.calc_class)
            f.evidence_sources = list(sources)
            graded_n += 1
    return {"graded": graded_n}


# =============================================================================================
# SELF-CHECK
# =============================================================================================
if __name__ == "__main__":            # python -m harness.audit
    import inspect

    from .locate import mint
    from .schema import Section, Table

    doc = PaperDoc(
        paper_id="selfcheck", title="T",
        sections=[Section(section_idx=0, title="Results", page_start=3,
                          text="Our method reaches 91.4 accuracy on CIFAR-100, "
                               "improving over the 88.2 baseline.")],
        tables=[Table(table_idx=1, page=3, rows=[["baseline", "88.2"], ["ours", "91.4"]])],
        figures=[Figure(figure_idx=1, page=4, caption="Training loss over epochs.")],
    )
    corpus = source_units(doc)
    by_idx = {t.table_idx: t for t in doc.tables}
    by_fig = {f.figure_idx: f for f in doc.figures}

    # --- 1. real quote passes; fabricated is dropped and counted ---
    real = verify_evidence("91.4", "T1:r1:c1", corpus, by_idx)
    assert real[0] == "cell_verified", real
    fabricated = verify_evidence("99.9% never printed anywhere", "p3", corpus, by_idx)
    assert fabricated[0] == "unverified", fabricated

    data = {"schema_version": 2, "findings": [
        {"finding_id": "f1", "statement": "s", "severity": "MAJOR",
         "evidence_quote": "91.4", "evidence_ref": "T1:r1:c1"},
        {"finding_id": "f2", "statement": "s2", "severity": "MAJOR",
         "evidence_quote": "totally fabricated text not in the paper",
         "evidence_ref": "p3"},
    ]}
    report, dropped, valid = _coerce("overclaim", data, corpus, by_idx, doc.n_pages,
                                     by_figure=by_fig)
    assert valid and dropped == 1 and len(report.findings) == 1, (dropped, report.findings)

    # A quote occurring twice is REFUSED an address (invariant 1).
    doc2 = PaperDoc(paper_id="dup", sections=[
        Section(section_idx=0, text="Each case is checked by hand."),
        Section(section_idx=1, text="Each case is checked by hand."),
    ])
    assert mint(doc2, "Each case is checked by hand.").resolution == "ambiguous"

    # --- 2. harness-writes-not-lens-writes: claimed evidence_class/verified_observation
    #        are IGNORED and overwritten regardless of content ---
    forged = {"schema_version": 2, "findings": [
        {"finding_id": "f3", "statement": "s3", "severity": "MINOR",
         "evidence_quote": "91.4", "evidence_ref": "T1:r1:c1",
         "evidence_class": "cell_verified", "verified_observation": "a lens wrote this itself"},
    ]}
    report2, _, _ = _coerce("overclaim", forged, corpus, by_idx, doc.n_pages, by_figure=by_fig)
    f3 = report2.findings[0]
    assert f3.evidence_class == "cell_verified"          # independently re-derived, not copied
    assert f3.verified_observation != "a lens wrote this itself"
    assert "Cell T1:r1:c1" in f3.verified_observation    # the HARNESS's own generated text

    # --- 3. derive's signature: vocabulary strings and booleans only ---
    import typing as _t   # stringified annotations (future import) need resolving
    hints = _t.get_type_hints(derive)
    for name in inspect.signature(derive).parameters:
        assert hints[name] in (str, bool), (name, hints[name])

    # --- 4. Never-raise-severity sweep ---
    for lens_sev in ("FATAL", "MAJOR", "MINOR", "NOTE"):
        for ec in EVIDENCE_CONFIDENCE_CEILING:
            for cand in ("", *CANDIDATE_CAP):
                for base in ("", *BASELINE_CAP):
                    for graded in (False, True):
                        for verdict in ("CONFIRMED", "PLAUSIBLE", "REFUTED", "INSUFFICIENT"):
                            for conf in ("HIGH", "MEDIUM", "LOW"):
                                _, cs, _, _ = derive(
                                    lens_severity=lens_sev, verification_state="complete",
                                    calc_class="not_applicable", graded=graded,
                                    grade_verdict=verdict, grade_severity=lens_sev,
                                    confidence=conf, evidence_class=ec,
                                    grader_evidence_class=ec, candidate_class=cand,
                                    baseline_class=base)
                                assert RANK[cs] <= RANK[lens_sev], \
                                    (lens_sev, ec, cand, base, graded, verdict, conf, cs)

    # --- 5. cross-section ceiling from the WEAKEST side; unresolvable side drops the
    #        finding WHOLE, never half-kept (invariant 29) ---
    assert weakest_evidence_class(["cell_verified", "caption_verified"]) == "caption_verified"
    cross = {"schema_version": 2, "findings": [
        {"finding_id": "f4", "statement": "s4", "severity": "MAJOR",
         "evidence_quote": "91.4", "evidence_ref": "T1:r1:c1",
         "additional_evidence": [
             {"role": "figure side", "evidence_quote": "Training loss over epochs.",
              "evidence_ref": "F1"}]},
    ]}
    report3, _, _ = _coerce("overclaim", cross, corpus, by_idx, doc.n_pages, by_figure=by_fig)
    assert len(report3.findings) == 1
    f4 = report3.findings[0]
    assert f4.evidence_class == "caption_verified", f4.evidence_class     # weakest side wins

    unresolvable_side = {"schema_version": 2, "findings": [
        {"finding_id": "f5", "statement": "s5", "severity": "MAJOR",
         "evidence_quote": "91.4", "evidence_ref": "T1:r1:c1",
         "additional_evidence": [
             {"role": "bad side", "evidence_quote": "not anywhere in this paper at all",
              "evidence_ref": "p3"}]},
    ]}
    report4, dropped4, _ = _coerce("overclaim", unresolvable_side, corpus, by_idx, doc.n_pages,
                                   by_figure=by_fig)
    assert len(report4.findings) == 0 and dropped4 == 1, (report4.findings, dropped4)

    # --- dedup, slug, and ranking sanity ---
    assert slug_for("overclaim-01") != slug_for("overclaim-02")
    assert slug_for("../../etc/passwd") and "/" not in slug_for("../../etc/passwd")
    dup_report = LensReport(lens="overclaim", findings=[
        Finding(finding_id="a", lens="overclaim", severity="FATAL",
               evidence_ref="T1:r1:c1", evidence_quote="91.4", scientific_class="OTHER"),
        Finding(finding_id="b", lens="overclaim", severity="MINOR",
               evidence_ref="T1:r1:c1", evidence_quote="91.4", scientific_class="OTHER"),
    ])
    kept, merged = deduplicate(dup_report.findings)
    assert merged == 1 and len(kept) == 1 and kept[0].merged_from == ["b"], (kept, merged)

    # --- 6. render_numbers/context: a PART sees only numbers it could ground; part=None
    #        is unrestricted ---
    from .schema import QuantFinding as _QF

    numdoc = PaperDoc(paper_id="nums", sections=[
        Section(section_idx=0, title="Intro", page_start=1, page_end=1, text="intro text"),
        Section(section_idx=1, title="Results", page_start=2, page_end=2,
               text="We beat the baseline, see Table 9."),
        Section(section_idx=2, title="Other", page_start=3, page_end=3, text="unrelated"),
    ], tables=[Table(table_idx=9, page=2, label="9", rows=[["ours", "91.4"]])],
       reported_numbers=[
           _QF(table_ref="T9:r0:c0", value="91.4", page=2),           # in the Table 9 part
           _QF(source_quote="we sample 2,900 cases", page=1),         # part-0 prose number
           _QF(source_quote="unlocated", page=0),                     # never safely excluded
       ])
    # Built directly (not via `paper.plan_reading`, whose 400-char floor would pack all
    # three tiny sections into one part) -- one section per part is the shape tested here.
    def _one_section_part(s):
        return paper.ReadingPart(number=s.section_idx + 1, total=3, sections=[s],
                                 chars=len(s.text), split_sections=0,
                                 slices=[(s.section_idx, 0, len(s.text))])
    part0, part1, part2 = (_one_section_part(s) for s in numdoc.sections)
    n0, n1, n2 = (render_numbers(numdoc, p) for p in (part0, part1, part2))
    assert "2,900" in n0 and "T9:r0:c0" not in n0
    assert "T9:r0:c0" in n1 and "2,900" not in n1     # Table 9's own label is mentioned here
    assert "T9:r0:c0" not in n2 and "2,900" not in n2
    for n in (n0, n1, n2):
        assert "unlocated" in n, "a number with no located page is never silently excluded"
    assert render_numbers(numdoc, None) == render_numbers(numdoc)  # whole-paper: unrestricted
    ctx_whole = context(numdoc)
    assert "T9:r0:c0" in ctx_whole["numbers_text"] and "2,900" in ctx_whole["numbers_text"]

    # --- 7. _section_render: the finding's OWN sections and the abstract come first; the
    #        rest of the paper fills the budget (the grader must see context elsewhere) ---
    gdoc = PaperDoc(paper_id="grd", title="G", sections=[
        Section(section_idx=0, title="Abstract", page_start=1, page_end=1, text="we claim X."),
        Section(section_idx=1, title="Setup", page_start=2, page_end=2, text="setup details."),
        Section(section_idx=2, title="Results", page_start=3, page_end=3,
               text="91.4 on the benchmark."),
        Section(section_idx=3, title="Limitations", page_start=4, page_end=4,
               text="a limitation nobody needs here."),
    ], tables=[Table(table_idx=1, page=3, rows=[["ours", "91.4"]])])
    g_text, g_note = _section_render(gdoc, ["T1:r0:c0"], 100_000)
    assert "91.4 on the benchmark" in g_text and "we claim X" in g_text   # cited + abstract
    assert "setup details" in g_text and "nobody needs here" in g_text   # rest fills budget
    assert "the whole paper is included" in g_note
    cited_text, cited_note = _section_render(gdoc, ["T1:r0:c0"], 80)
    assert "91.4 on the benchmark" in cited_text and "we claim X" in cited_text
    assert "nobody needs here" not in cited_text and "0 of them cited" in cited_note
    g_text2, _ = _section_render(gdoc, ["T1:r0:c0", "p2"], 100_000)      # a cross-section side
    assert "setup details" in g_text2                                   # the second side's page
    tight_text, tight_note = _section_render(gdoc, ["T1:r0:c0", "p2"], 40)
    assert "withheld for budget" in tight_note

    gfinding = Finding(finding_id="gf-01", lens="overclaim", severity="MAJOR",
                       evidence_ref="T1:r0:c0", evidence_quote="91.4",
                       additional_evidence=[EvidencePointer(evidence_ref="p2")])
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as _gtd:
        gcfg = Config(projects_dir=Path(_gtd))
        state.create_project(gcfg, "", "G", pid="gp")
        prompts = build_prompts(gcfg, "gp", gdoc, [gfinding])
        [gbody_path] = prompts.values()
        gbody = Path(gbody_path).read_text(encoding="utf-8")
        assert "setup details" in gbody and "91.4 on the benchmark" in gbody
        assert "nobody needs here" in gbody             # the rest of the paper, within budget

    print("harness.audit self-check OK")
