"""S2 (audit) + S2.5 (grade), consolidated: the four scientific lenses, a long paper read
in bounded parts with one cross-part synthesis, the second blinded grader, and the pure
severity-derivation table underneath both. Consolidates `harness/stages/audit.py` (reading
units, prompts, quote verification, part/synthesis composition, dedup),
`harness/stages/grade.py` (candidate selection, grader prompts, sealing, report-time
attach) and `harness/grading.py` (`derive` -- the one function deciding what a finding
counts as; vocabulary strings and booleans only, so "no std dev = MAJOR" is inexpressible).

Model dispatch (spawn/confine/parse/seal a reviewer subprocess) is NOT here -- that is
`agent.py`'s job (`agent.fill_lenses`/`agent.fill_grades`). This module writes prompts,
verifies what comes back against the parsed paper, composes a part-read lens file, and
derives what a finding counts as; it stops at producing verified, composed `LensReport`s
(question-syncing for the DISCOVER phase is `discover.py`'s job, not this one's).

Two invariants held here: a lens supplies a QUOTE and the harness alone decides whether it
is real (`verify_evidence` -- `verified_observation`/`evidence_class` are WRITTEN BY THE
HARNESS, never read from a lens file); and severity may only be CAPPED, never raised, by
anything downstream of a lens's own assertion (`derive`, `RANK[counted_severity] <=
RANK[lens_severity]` over the whole reachable table).

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

from . import agent, paper, state, taxonomy
from .config import Config
from .locate import _self_projection, flatten, soft_hyphen_projection
from .prompts import audit as P
from .prompts import grade as G
from .schema import (BASELINE_CLASSES, CANDIDATE_CLASSES, CONFIDENCES, DISCREPANCY_TYPES,
                     EVIDENCE_ORIGINS, PRIOR_ART_BASES, SEVERITIES, Equation,
                     EvidencePointer, Figure, Finding, Grade, LensReport, PaperDoc)

# =============================================================================================
# PART 1 -- PURE SEVERITY DERIVATION  (was harness/grading.py)
# =============================================================================================
# Three questions kept apart: IS THERE AN ISSUE (verdict/candidate_class), HOW SURE ARE WE
# (confidence, bounded by evidence_support), HOW MUCH DOES IT MATTER (severity -- the only
# one any threshold counts, and the one nothing here SETS, only caps). Vocabulary strings
# and booleans only; `scientific_class` is deliberately not a parameter (a CONFOUND is not
# always MAJOR any more than "no variance" is). Swept below: RANK[counted] <= RANK[lens].

RANK = {"FATAL": 3, "MAJOR": 2, "MINOR": 1, "NOTE": 0, "": 0}

_STOPLIST = {"n/a", "na", "none", "not applicable", "see above", "same as above", ""}
_MIN_CHARS = 40


def _flat_g(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def pass_b_state(f: dict, severity: str, schema_version=None) -> str:
    """'legacy' (pre `schema_version: 2`, always UNCAPPED) | 'incomplete' (a required
    field is blank/stoplisted/identical to statement-claim-title) | 'complete'.
    `schema_version` is the REPORT's, not the finding's, and must be threaded through
    explicitly -- reading it off the finding itself made every real finding 'legacy'."""
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


# A deliberately tiny arithmetic sandbox: literals and four operators, nothing that can
# name a variable, call a function, or read anything outside the expression itself.
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
        # Both operands evaluated first, then a Pow's exponent bounded on its VALUE, not
        # its AST shape: `9**9**9` parses as `9 ** (9**9)`, a nested BinOp not a literal.
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"disallowed expression node: {type(node).__name__}")


def recheck_calculation(calc: dict, verify_fn, corpus, by_idx, max_page: int = 0) -> str:
    """Independently redo a lens's own arithmetic, by machine, catching a disputed
    percentage that cannot supply two verified operands reproducing its own claimed
    result. `verify_fn` is `verify_evidence`, passed in (kept parametric from the
    pre-consolidation split, where this lived in a separate, model-free module)."""
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


# A grade verdict caps severity; CONFIRMED carries no cap of its own (the lens's/grader's
# own severities and the structural caps below are what actually bound it).
_VERDICT_CAP = {"CONFIRMED": "FATAL", "PLAUSIBLE": "MINOR", "INSUFFICIENT": "NOTE",
                "REFUTED": "NOTE"}
# Confidence CAPS severity; it never SETS it.
_CONFIDENCE_CAP = {"HIGH": "FATAL", "MEDIUM": "MAJOR", "LOW": "MINOR"}
_CLASS = {"CONFIRMED": "CONFIRMED_FINDING", "PLAUSIBLE": "PLAUSIBLE_CONCERN",
          "INSUFFICIENT": "OPEN_QUESTION", "REFUTED": "REFUTED"}

# Only CONFIRMED_FINDING is eligible to carry FATAL/MAJOR -- a lens that sorted its own
# candidate below that cannot have it counted as one however severe it also called it.
CANDIDATE_CAP = {"CONFIRMED_FINDING": "FATAL", "PLAUSIBLE_CONCERN": "MINOR",
                 "OPEN_QUESTION": "NOTE", "DISMISSED": "NOTE", "": "FATAL"}

# Evidence TYPE bounds CONFIDENCE, never severity directly -- "caption -> NOTE" was the
# same defect as "no variance -> MAJOR": deciding impact from something that is not
# impact. A caption reports no plotted values (LOW); display-equation extraction is lossy
# (MEDIUM); a cell or a verbatim section quote is checkable as-is (HIGH, no penalty).
EVIDENCE_CONFIDENCE_CEILING = {
    "cell_verified": "HIGH", "prose_verified": "HIGH", "equation_verified": "MEDIUM",
    "caption_verified": "LOW", "unverified": "LOW",
}
CONFIDENCE_RANK = {"HIGH": 2, "MEDIUM": 1, "LOW": 0, "": 0}
_CONFIDENCE_BY_RANK = {2: "HIGH", 1: "MEDIUM", 0: "LOW"}
# A machine-recomputed calculation corroborates whatever was cited; it carries no
# citation strength of its own, so it enters at LOW and only ever contributes a lift.
_CORROBORATING_CALC = ("recomputed_ok",)


def evidence_support(evidence_class: str, grader_evidence_class: str = "unverified",
                     calc_class: str = "not_applicable") -> tuple[str, tuple[str, ...]]:
    """(confidence_ceiling, sources) -- pure, no severity involved. Starts at the
    strongest single citation's own ceiling and lifts one step per further independent
    source, capped at HIGH: monotone, so it never drops below the best source and never
    invents HIGH from two weak ones."""
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


# Self-consistency caps: each keyed on the lens's OWN classification of its own finding,
# so neither can hold a finding below what its author already conceded about it, and
# neither is an evidence-type or absence-of-evidence rule.
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
    deciding what a finding counts as, from data alone. Grading -- lens-side
    non-degeneracy alone, or a full grader verdict -- can only DEMOTE, never promote:
    `RANK[counted_severity] <= RANK[lens_severity]` always (swept in the self-check)."""
    caps: dict[str, str] = {"lens": lens_severity}

    if calc_class == "recomputed_mismatch":
        # The lens's own arithmetic, redone by machine, does not reproduce its own claim.
        caps["lens"] = min(caps["lens"], "MINOR", key=lambda s: RANK[s])
    if verification_state == "incomplete":
        # `legacy` (pre `schema_version: 2`) is deliberately UNCAPPED here.
        caps["lens"] = "NOTE"

    if candidate_class:
        caps["candidate_class"] = CANDIDATE_CAP.get(candidate_class, "FATAL")
    if baseline_class:
        caps["baseline_class"] = BASELINE_CAP.get(baseline_class, "FATAL")
    if prior_art_basis:
        caps["prior_art_basis"] = PRIOR_ART_CAP.get(prior_art_basis, "FATAL")

    # A lens declaring LOW confidence and asserting FATAL/MAJOR anyway has contradicted
    # its own prompt's rule -- checkable without trusting either half.
    if lens_confidence == "LOW" and lens_severity in ("FATAL", "MAJOR"):
        caps["self_contradictory_confidence"] = "MINOR"

    ceiling, support = evidence_support(evidence_class, grader_evidence_class, calc_class)

    if not graded:
        # Evidence type bounds CONFIDENCE, and that ceiling bounds severity -- a
        # caption-only concern is held to what LOW confidence carries (MINOR), not
        # flattened to NOTE; the same caption corroborated by recomputed math reaches
        # MEDIUM and so MAJOR. This is what keeps grading-off byte-identical to the
        # pre-grading verdict for an ordinary cell- or prose-backed finding.
        caps["confidence"] = _CONFIDENCE_CAP[ceiling]
        binding = min((k for k in _CAP_ORDER if k in caps), key=lambda k: RANK[caps[k]])
        return ("UNGRADED", caps[binding], binding,
                f"ungraded; lens severity {lens_severity} capped by {binding}="
                f"{caps[binding]}; evidence supports at most {ceiling} confidence "
                f"from {'+'.join(support) or 'nothing'}")

    caps["verdict"] = _VERDICT_CAP.get(grade_verdict, "NOTE")
    if grade_severity in ("FATAL", "MAJOR", "MINOR"):
        # "NONE" is deliberately not added here -- that is the `verdict` cap's job.
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
    # FATAL requires TWO independent checks; a machine-reproduced calculation counts as
    # one of them (a stronger check than a second citation, not a weaker one).
    cells = sum(1 for c in (evidence_class, grader_evidence_class) if c == "cell_verified")
    if not (cells >= 2 or (cells >= 1 and calc_class == "recomputed_ok")):
        caps["fatal_needs_corroboration"] = "MAJOR"

    # A weak (LOW-confidence) refutation must not zero out a lens's own FATAL/MAJOR on its
    # own say-so -- that would let grading decide, in the acquitting direction, on thin
    # evidence.
    if grade_verdict == "REFUTED" and confidence == "LOW":
        grade_verdict = "PLAUSIBLE"
        caps["verdict"] = _VERDICT_CAP["PLAUSIBLE"]

    binding = min((k for k in _CAP_ORDER if k in caps), key=lambda k: RANK[caps[k]])
    counted = caps[binding]
    finding_class = _CLASS.get(grade_verdict, "OPEN_QUESTION")
    if finding_class == "CONFIRMED_FINDING" and structural_hit:
        # A grader saying CONFIRMED while conceding a reasonable non-problem reading, or
        # without stating impact or a steelman, has contradicted itself. Resolved DOWNWARD.
        finding_class = "PLAUSIBLE_CONCERN"
    return (finding_class, counted, binding,
           f"graded {grade_verdict}/{grade_severity}/{confidence}; evidence supports at "
           f"most {ceiling} confidence from {'+'.join(support) or 'nothing'}, so effective "
           f"confidence {effective}; counted={counted} bound by {binding}")


# =============================================================================================
# PART 2 -- AUDIT (S2): reading units, prompts, quote verification, part composition
#           (was harness/stages/audit.py)
# =============================================================================================
LENSES = tuple(P.LENSES)
# THE DEFAULT, not the live value -- `budget_chars()` below reads `SH_AUDIT_BUDGET_CHARS`
# at call time through `harness.coverage`, the one reader of that variable, so the budget a
# review PRINTS and the budget its prompts were built at can never drift apart.
SECTION_BUDGET_CHARS = 70_000
_CELL_REF = re.compile(r"T(\d+):r(\d+):c(\d+)")
# The four admissible shapes for `evidence_ref`; anything else names no checkable location.
_PAGE_REF = re.compile(r"p\d+", re.I)
_FIG_REF = re.compile(r"F(\d+)")
_EQ_REF = re.compile(r"E(\d+)")
_QUOTE_MIN = 8
# No letter, no digit: a bare arrow/bullet/dash cannot support a claim about anything.
_MEANINGLESS_QUOTE = re.compile(r"^[^0-9a-z]*$")
_WS = re.compile(r"\s+")


def budget_chars() -> int:
    # Deferred import: report.py imports RANK from this module at its own top level, so a
    # top-level import here would be a cycle. By the time this function is actually
    # CALLED both modules have finished loading.
    from . import report as _report                # noqa: PLC0415 -- one reader of the env
    return _report.budget_chars()


def _flat(s: str) -> str:
    return _WS.sub("", (s or "").lower())


def _enum(v, vocab: tuple[str, ...], default: str = "") -> str:
    """Uppercase and clamp to a closed vocabulary; garbage becomes `default`, never a
    silent pass-through."""
    s = str(v or "").strip().upper()
    return s if s in vocab else default


def _prose(v, n: int = 4000) -> str:
    return str(v or "").strip()[:n]


def _origin_from_ref(ref: str) -> str:
    """PAPER_TABLE/TEXT/FIGURE/EQUATION from the SHAPE of `ref` alone -- never trusted
    from the lens, so a lens calling a page citation a table cannot go unnoticed."""
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
    from forging a markdown heading or table row in the rendered report."""
    return " ".join((s or "").split())[:n]


def source_units(doc: PaperDoc) -> tuple[tuple[int, str, str], ...]:
    """The paper as SEPARATE per-section units, never concatenated -- a join would let a
    quote straddling a section seam verify against text the PDF never actually contains.
    The third element is the same text with the typesetter's line-break hyphens removed."""
    out = []
    for section in doc.sections:
        if not (section.text or "").strip():
            continue
        flat, offsets = flatten(section.text)
        projected, _index = soft_hyphen_projection(flat, offsets, section.text)
        out.append((section.section_idx, flat, projected))
    return tuple(out)


def _units(corpus) -> tuple[tuple[int, str, str], ...]:
    """Accept the unit tuple, or a single pre-flattened string as one anonymous unit.
    Each unit carries its OWN section_idx rather than its position in this tuple."""
    if isinstance(corpus, str):
        return ((-1, corpus, corpus),)
    return tuple(u if len(u) >= 3 else (u[0], u[1], u[1]) for u in corpus)


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


def context(doc: PaperDoc, part=None, anchor=None) -> dict[str, str]:
    """Everything a lens prompt is built from for one pass. `part=None` renders the whole
    paper in one pass and does NOT fall back to a hard equal-split slice across sections."""
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
        "numbers_text": render_numbers(doc),
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


# A paper that fits in one pass is one unit per lens (unchanged artifact layout). A paper
# that does not is N part units plus one cross-part SYNTHESIS unit per lens, and
# `compose_lens` assembles `audit/<lens>.json` from them so nothing downstream needs to
# know how the paper was traversed.
UNIT_KINDS = ("whole", "part", "synthesis")
SYNTHESIS_ID = "synthesis"
# Distinct from every delegation mode, because nothing DELEGATED wrote a composed file --
# this harness assembled it from artifacts that were themselves sealed (provenance by
# reference, walkable via `composed_from`).
COMPOSED_WRITER = "composed_from_parts"


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
    synthesis (which cannot be rendered before the parts it reads are sealed)."""
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


# Accepted writers for a lens artifact: every mode `agent.py`'s delegation vocabulary
# admits, plus COMPOSED_WRITER for a lens assembled from already-sealed parts.
_ACCEPTED_WRITERS = agent.ROLES["lens"].writers + (COMPOSED_WRITER,)


def _sealed(path: Path) -> tuple[bool, str]:
    """Does a sealed provenance sidecar beside `path` describe `path`'s CURRENT bytes?"""
    return agent.verify_seal(path, accepted_writers=_ACCEPTED_WRITERS)


def unit_is_accepted(unit: AuditUnit) -> tuple[bool, str]:
    """Sealed AND produced against the prompt CURRENTLY on disk -- a part whose span moved
    (budget/extraction changed since) has a sealed output answering a question nobody is
    asking any more. A whole-paper unit is deliberately not prompt-pinned (the historical,
    unsplit path predates prompt fingerprinting)."""
    ok, why = _sealed(unit.out_path)
    if not ok or unit.kind == "whole":
        return ok, why
    fresh, note = agent.prompt_is_unchanged(unit.sidecar_path, unit.prompt_path)
    if not fresh:
        return False, note
    return True, ""


def lens_is_accepted(root: Path, lens: str) -> tuple[bool, str]:
    """A lens result counts only if the HARNESS recorded writing it -- file existence is
    not provenance; a hand-placed file bypasses validation and staging entirely."""
    ok, why = _sealed(root / "audit" / f"{lens}.json")
    return ok, ("no lens file" if why == "no output file" else why)


def accept_lens(cfg: Config, pid: str, lens: str, raw: str, *,
                reviewer: str = "", tool_policy: str = "unrecorded",
                mode: str = "MANUAL") -> dict:
    """Validate and persist a HAND-WRITTEN lens file through the same gate the automated
    dispatch path uses. `mode` defaults to MANUAL -- the mode promising least isolation."""
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
    """That lens's own VERIFIED part-local observations, keyed by part number. Verified,
    not merely produced: a synthesis reasoning over an unverified quotation would be
    building cross-section concerns on top of text that may not be in the paper at all."""
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
    checkable (span, anchor digest, prompt digest) rather than merely asserted. NOT
    sealed: this is an input record the harness wrote about itself, not an attestation
    about an output that came through a validated path."""
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
    None while any unit is still missing -- a lens is never half-composed. Deterministic:
    findings carried verbatim apart from two harness-written labels (`source_part`, and a
    disambiguated finding id where two readings happened to choose the same one)."""
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
            # The synthesis saw this lens's observations from the whole paper, so its
            # answer wins over a part reader's, which only saw one span.
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
        # NOT a tool policy this harness enforced -- each reading records its own.
        "tool_policy": "composed; each reading in composed_from records its own policy",
    })


def reading_record(cfg: Config, pid: str, doc: PaperDoc,
                   lenses: tuple[str, ...] = LENSES) -> dict:
    """How much of this paper reached a reader, and how it was carried out.
    `reader_visible_fraction` is the ceiling on every recall claim this system makes --
    it is not issue recall, and it is not extraction quality (`extracted_text_fraction`
    is the separate, unmeasured-underneath-it ceiling for that)."""
    root = state.project_dir(cfg, pid)
    plan = plan_for(doc)
    units = units_for(root, lenses, plan)
    cov = plan.coverage
    syntheses = [u for u in units if u.kind == "synthesis"]
    # Every page a recovered section COVERS, not only the page it starts on.
    pages: set[int] = set()
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
    """Reading units this paper needs and does not have; empty is the only clean answer.
    Checked at the LAST gate, not only the first, because the audit phase can be resumed
    or skipped past on a cached case."""
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
    in. Calls no model. A synthesis prompt is deliberately not written until its parts
    are sealed -- writing it early would be a synthesis over nothing."""
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
        # Sanitised at the boundary: a real prompt carries raw PDF-extraction control bytes.
        body = paper.sanitise_controls(
            _header(unit.lens, pid, unit.unit_id, unit.out_path.name) + body)
        unit.prompt_path.parent.mkdir(parents=True, exist_ok=True)
        unit.prompt_path.write_text(body, encoding="utf-8")
        written[unit.unit_id] = str(unit.prompt_path)
        _write_manifest(unit, pid=pid, part=part, anchor=plan.anchor, prompt_text=body,
                        budget=budget_chars(), inputs=inputs)

    done = [u.unit_id for u in units if unit_is_accepted(u)[0]]
    todo = [u.unit_id for u in units if u.unit_id not in done and u.unit_id in written]

    # Compose every lens whose readings are all in. Idempotent: the composed bytes are a
    # pure function of the part artifacts.
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
    a report (so completeness bookkeeping stays visible) but MUST NOT be read as "this
    lens ran and found nothing"."""
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
    """(evidence_class, verified_observation) -- WHAT THE HARNESS ITSELF CONFIRMED (never
    the lens's own word, invariant 2). Generated, never copied, so a reader can re-run the
    same comparison from `doc.json`. `max_page`, when given, bounds a `p<N>` reference."""
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
        # The typesetter's hyphen, tried only after the exact search fails, so a
        # character-for-character quotation is never resolved through a normalisation.
        probe = soft_hyphen_projection(*_self_projection(q))[0]
        if len(probe) >= _QUOTE_MIN:
            hit = next((idx for idx, _unit, proj in units if probe in proj), None)
            dehyphenated = hit is not None
    if hit is None:
        return "unverified", ""
    where = f"section_idx {hit}" if hit >= 0 else "the parsed section text"
    # "VERBATIM" IS DROPPED WHEN IT WOULD BE FALSE -- a hyphen-recovered match differs
    # from the extracted characters by that hyphenation and is not character-for-character.
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
    """Is this quote really in the paper -- and if it cites a cell, is it that cell? The
    boolean shadow of `verify_evidence`, so the thing that DROPS a finding and the thing
    that DESCRIBES a kept one can never disagree about whether the evidence held."""
    return verify_evidence(quote, ref, corpus, by_idx)[0] != "unverified"


def weakest_evidence_class(classes: Sequence[str]) -> str:
    """The evidence class a MULTI-LOCATION concern is held to: its weakest side, because a
    reader who cannot confirm that half cannot confirm the concern. Deliberately NOT fed
    into `evidence_support` as a second independent source -- that would let one reader's
    two citations corroborate itself, a mechanism that raises rather than caps."""
    order = {c: CONFIDENCE_RANK[EVIDENCE_CONFIDENCE_CEILING.get(c, "HIGH")]
             for c in classes}
    return min(classes, key=lambda c: order[c]) if classes else "unverified"


def _address_identity(f: Finding) -> tuple:
    """This concern's IDENTITY: the ORDERED locations it was established from. Ordered,
    not a set, because the two halves of a cross-section concern are not interchangeable."""
    sides = [(f.evidence_ref, _flat(f.evidence_quote))]
    sides += [(e.evidence_ref, _flat(e.evidence_quote)) for e in f.additional_evidence]
    return tuple(sides)


def _concern_identity(f: Finding) -> tuple:
    """WHAT KIND of concern this is, in closed vocabulary only -- the lens's OWN
    classification, not `scientific_class` (a SUMMARY of it that can fold two genuinely
    different concerns anchored on the same quote into one token)."""
    return (f.scientific_class, f.discrepancy_type, f.baseline_class,
            f.candidate_class, f.prior_art_basis)


def deduplicate(findings: list[Finding]) -> tuple[list[Finding], int]:
    """Fold concerns that resolved to the identical address set. OVER ADDRESSES AND
    CLOSED VOCABULARY, NEVER OVER TEXT: run AFTER verification, so every address in the
    key already resolved against the paper. The survivor keeps its own identity and
    records the ids folded into it."""
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
    """(report, n_dropped, valid) -- `valid` is whether the FILE ITSELF was the shape a
    lens report has to be, independent of whether any individual finding survived
    verification."""
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
        # EVERY SIDE, OR NONE -- a concern established from two locations is checkable
        # only if both are (invariant 29); dropped whole and counted, like any finding
        # whose single quotation is not in the paper.
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
            # The class the CAPS are computed from, not the class of the primary citation.
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
        # No grader has run yet -- `graded=False` is what makes turning grading off, or
        # never running it, leave the verdict exactly where it was before this axis existed.
        finding_class, counted_severity, binding_cap, derivation = derive(
            lens_severity=sev, verification_state=verification_state,
            calc_class=calc_class, graded=False, evidence_class=evidence_class,
            lens_confidence=confidence, candidate_class=candidate_class,
            baseline_class=baseline_class, prior_art_basis=prior_art_basis)
        if counted_severity == sev:
            # Nothing capped it -- leave `counted_severity` blank so a reader can tell
            # "verified equal" apart from "never assessed".
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
            # Everything below is HARNESS-WRITTEN, never read from `f` -- a lens cannot
            # certify its own evidence, reasoning, arithmetic, or classification any more
            # than invariant 2 lets it certify its own evidence class.
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
#           (was harness/stages/grade.py; the CLI dispatch loop itself is now
#           `agent.run_candidate`/`agent.fill_grades` and is not reimplemented here)
# =============================================================================================
_SLUG = re.compile(r"[^a-zA-Z0-9_-]+")
_GRADE_PAGE_REF = re.compile(r"p(\d+)")
_LENS_RANK = {"overclaim": 3, "contradiction": 2, "confound": 1, "protocol": 0}


def slug_for(finding_id: str) -> str:
    """A filesystem/path-traversal-safe name for a candidate's prompt/output files --
    `finding_id` is lens-supplied and may contain `/`, `\\`, `..`. The hash suffix also
    disambiguates two ids that sanitise to the same prefix."""
    base = _SLUG.sub("-", finding_id or "unnamed").strip("-")[:40] or "unnamed"
    return f"{base}-{hashlib.sha256((finding_id or '').encode()).hexdigest()[:8]}"


def _rank_key(f: Finding) -> tuple:
    """Most-severe-first by what a verdict actually counts (`counted_severity`, falling
    back to the lens's own `severity`), then a fixed lens tiebreak, then the id."""
    counted = f.counted_severity or f.severity
    return (RANK.get(counted, 0), _LENS_RANK.get(f.lens, 0), f.finding_id)


def select_candidates(reports: list[LensReport], scope: str) -> list[Finding]:
    """Every substantiated finding in scope, most severe first -- so an interrupted
    grading pass covers what matters most first."""
    findings = [f for r in reports for f in r.findings]
    if scope != "all":
        findings = [f for f in findings if f.severity in ("FATAL", "MAJOR")]
    return sorted(findings, key=_rank_key, reverse=True)


def _section_render(doc: PaperDoc, evidence_ref: str, budget: int) -> tuple[str, str]:
    """Section text under `budget` chars, quote-bearing section first, plus a note naming
    what was withheld -- so a judgement never silently depends on a dropped section."""
    m = _GRADE_PAGE_REF.fullmatch((evidence_ref or "").strip())
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
    """Regenerated every run -- never edit, like `audit/prompts/*.md`."""
    root = state.project_dir(cfg, pid)
    pdir = root / "audit" / "grade" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for f in candidates:
        slug = slug_for(f.finding_id)
        sections_text, note = _section_render(doc, f.evidence_ref, cfg.grade_budget_chars)
        # EVERY SIDE of a multi-location concern gets its own section context -- a grader
        # shown only one half cannot grade it and would defer to the first reader, which
        # is precisely the deference independent grading exists to remove.
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
            sections_text=sections_text, tables_text=paper.render_tables(doc.tables),
            withheld_note=note,
        )
        (pdir / f"{slug}.md").write_text(body, encoding="utf-8")
        paths[slug] = str(pdir / f"{slug}.md")
    return paths


_ACCEPTED_GRADE_WRITERS = agent.ROLES["grade"].writers


def grade_is_accepted(gdir, slug: str) -> tuple[bool, str]:
    """The grading analogue of `lens_is_accepted`: counts only if the harness recorded
    writing it, sealed by a content hash."""
    path = gdir / f"{slug}.json"
    ok, why = agent.verify_seal(path, accepted_writers=_ACCEPTED_GRADE_WRITERS)
    return ok, ("no grade file" if why == "no output file" else why)


def accept_grade(cfg: Config, pid: str, slug: str, raw: str, *,
                 grader: str = "", tool_policy: str = "unrecorded",
                 mode: str = "MANUAL") -> dict:
    """Validate and seal a grade produced OUTSIDE the automated dispatch path -- the
    grading analogue of `accept_lens`. Blinding is a property of what the grader was
    SHOWN (the prompt file), not of which process read it, so a second reader given only
    `audit/grade/prompts/<slug>.md` is as blinded as a dispatched subprocess."""
    grade = agent.parse_grade_json(raw)
    gdir = state.project_dir(cfg, pid) / "audit" / "grade"
    out = gdir / f"{slug}.json"
    record = agent.seal(out, grade.model_dump(), mode=mode, reviewer=grader,
                        tool_policy=tool_policy,
                        extra={"slug": slug, "paper_id": pid, "verdict": grade.verdict})
    # The sidecar's field has always been `grader`, never `reviewer`.
    record["grader"] = record.pop("reviewer")
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
    matching findings, IN PLACE. Safe whether or not grading ever ran -- an unmatched
    finding simply keeps the ungraded baseline `_coerce` already set."""
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
            # Re-derived with the GRADER's citation in scope: a figure-only concern the
            # grader independently corroborated against a table cell is no longer
            # figure-only, and the ceiling the report prints has to say so.
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

    # --- 1. Quote verification: real quote passes; fabricated is dropped and counted ---
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

    # A quote occurring twice is REFUSED an address (invariant 1) -- the harness-side
    # address grammar `verify_evidence` relies on `locate` for (flatten/soft-hyphen).
    doc2 = PaperDoc(paper_id="dup", sections=[
        Section(section_idx=0, text="Each case is checked by hand."),
        Section(section_idx=1, text="Each case is checked by hand."),
    ])
    assert mint(doc2, "Each case is checked by hand.").resolution == "ambiguous"

    # --- 2. Harness-writes-not-lens-writes: a lens's own claimed evidence_class/
    #        verified_observation are IGNORED and overwritten regardless of content ---
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

    # --- 3. grading.derive's signature: vocabulary strings and booleans only ---
    # `from __future__ import annotations` stringifies annotations, so resolve them
    # rather than comparing raw `Signature` objects to the `str`/`bool` types directly.
    import typing as _t
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

    # --- 5. Cross-section evidence ceiling from the WEAKEST side; an unresolvable side
    #        drops the finding WHOLE, never half-kept (invariant 29) ---
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

    print("harness.audit self-check OK")
