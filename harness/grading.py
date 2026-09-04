"""Pure derivation logic for what a finding's severity actually earns. No I/O, no model
call, no Config — every function here is data in, data out, so the rule that decides
what the verdict counts is as inspectable and arguable as the threshold table in
`stages/report.py`.

Two independent axes feed `Finding.counted_severity`, the field `stages.report.counted`
reads instead of the lens's raw `severity`:

  1. LENS-SIDE SELF-VERIFICATION (this module, always active once a lens writes
     `schema_version: 2`). Did the lens's own falsification/steelman work show real
     work, or is it blank/degenerate? Did its own arithmetic, if it made any, actually
     reproduce the number it disputed? `pass_b_state` / `recheck_calculation`.
  2. GRADER-SIDE INDEPENDENT JUDGEMENT (Stage 3/4 — a second, blinded reviewer's
     verdict on the SAME candidate). `derive` combines both axes into one line.

Neither axis, nor their combination, can ever PROMOTE a lens's own asserted severity —
only cap it. `derive`'s one safety property, asserted by test over the whole reachable
table: `RANK[counted_severity] <= RANK[lens_severity]`. Grading can only move a verdict
toward GREEN.
"""
from __future__ import annotations

import ast
import operator

RANK = {"FATAL": 3, "MAJOR": 2, "MINOR": 1, "NOTE": 0, "": 0}

_STOPLIST = {"n/a", "na", "none", "not applicable", "see above", "same as above", ""}
_MIN_CHARS = 40


def _flat(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def pass_b_state(f: dict, severity: str) -> str:
    """'legacy' | 'incomplete' | 'complete', for the falsification/steelman triple.

    'legacy': the file predates `schema_version: 2` — every finding on it is UNCAPPED so
    a schema change can never silently erase the existing evidence base, the same
    guarantee `tests/test_finding_traceability.py:170-183` already makes about an
    earlier split.

    'incomplete': a required field is blank, under `_MIN_CHARS` once flattened, on the
    stoplist, or identical to `statement`/`claim`/`title` — i.e. present but not really
    written. `steelman` is required only when the lens itself asserted FATAL or MAJOR;
    a MINOR does not need a defense of the authors to be worth printing.
    """
    if f.get("schema_version") != 2:
        return "legacy"
    required = ["alternative_interpretation", "why_alternative_fails"]
    if severity in ("FATAL", "MAJOR"):
        required.append("steelman")
    others = {_flat(f.get(k)) for k in ("statement", "claim", "title")}
    for key in required:
        t = _flat(f.get(key))
        if len(t) < _MIN_CHARS or t in _STOPLIST or t in others:
            return "incomplete"
    return "complete"


# A deliberately tiny arithmetic sandbox: literals and the four operators, nothing that
# can name a variable, call a function, or read anything outside the expression itself.
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
        # Evaluate BOTH operands before applying the operator, then check the magnitude
        # of a Pow's exponent on its VALUE, not on the AST shape. `9**9**9` parses
        # right-associative as `9 ** (9**9)`, so the exponent is a nested BinOp, not a
        # literal `ast.Constant` — checking `isinstance(node.right, ast.Constant)`
        # missed it, and the inner `9**9=387420489` evaluates fine on its own, so the
        # outer `9**387420489` reached Python's bignum pow and hung. Evaluating first
        # and gating on the resulting number closes that regardless of nesting depth.
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"disallowed expression node: {type(node).__name__}")


def recheck_calculation(calc: dict, verify_fn, corpus, by_idx, max_page: int = 0) -> str:
    """CALC_CLASSES — independently redo a lens's own arithmetic, by machine.

    `verify_fn` is `stages.audit.verify_evidence`, passed in rather than imported so
    this module stays free of any import back into the pipeline that already imports
    IT — `stages/audit.py` calls this, and a module that imported it back would be a
    cycle for no reason other than convenience.

    Turns spec §6 ("never claim a number is wrong without recomputing it") from
    exhortation into arithmetic: a lens that disputes a percentage but cannot supply two
    verified operands and an expression that reproduces its own stated result is caught,
    not trusted.
    """
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


# --------------------------------------------------------------------------- #
# The counted-severity derivation table
# --------------------------------------------------------------------------- #
# A verdict caps how severe a candidate may be counted, whatever anyone graded it.
# CONFIRMED carries no cap of its own — the lens's and grader's own severities, and the
# structural caps below, are what actually bound it.
_VERDICT_CAP = {"CONFIRMED": "FATAL", "PLAUSIBLE": "MINOR", "INSUFFICIENT": "NOTE", "REFUTED": "NOTE"}

# Confidence CAPS severity; it never SETS it. "A high-severity, low-confidence finding
# is usually not appropriate" becomes a rule instead of an exhortation.
_CONFIDENCE_CAP = {"HIGH": "FATAL", "MEDIUM": "MAJOR", "LOW": "MINOR"}

_CLASS = {"CONFIRMED": "CONFIRMED_FINDING", "PLAUSIBLE": "PLAUSIBLE_CONCERN",
          "INSUFFICIENT": "OPEN_QUESTION", "REFUTED": "REFUTED"}

# How strong a citation is caps what it can earn, independent of severity or grading. A
# caption NAMES a figure without reporting its plotted values (see
# `harness.artifacts.Figure`); a display equation is lossy-extracted. The escape hatch
# is the point: a figure-based concern that ALSO cites a table cell has its ceiling
# lifted to that cell's — see the `max` over both citations below — so a lens is never
# silenced, only told that the only way a picture moves a verdict is by finding the
# number behind it.
EVIDENCE_CEILING = {"cell_verified": "FATAL", "prose_verified": "FATAL",
                    "equation_verified": "MINOR", "caption_verified": "NOTE",
                    "unverified": "NOTE"}

# Every cap that binds is named, in this fixed order, so the same inputs always name the
# same binding cap and `derive` is total and reproducible — the same discipline
# `stages.report.finding_key` already has for its own tiebreak order.
_CAP_ORDER = ("lens", "grader", "verdict", "confidence", "evidence",
             "falsification_conceded", "no_impact_statement", "no_steelman",
             "grader_cannot_cite", "fatal_needs_two_cells")


def derive(*, lens_severity: str, verification_state: str, calc_class: str,
          graded: bool, grade_verdict: str = "", grade_severity: str = "",
          confidence: str = "", falsification_survived: bool = True,
          has_impact_statement: bool = True, has_steelman: bool = True,
          evidence_class: str = "unverified",
          grader_evidence_class: str = "unverified") -> tuple[str, str, str, str]:
    """(finding_class, counted_severity, binding_cap, derivation) — the ONE function that
    decides what a finding actually counts as, from data alone. No model call, no I/O.

    The safety property everything here is arranged to make true, and which
    `tests/test_grading_table.py` asserts over the whole reachable input space:

        RANK[counted_severity] <= RANK[lens_severity]

    Grading — lens-side non-degeneracy alone, or a full grader verdict — can only
    DEMOTE a lens's own asserted severity, never promote it. That asymmetry is what
    makes it safe to let a second model, or even the lens's own unverified pass-B
    fields, influence what a report counts: the worst either can do is under-count a
    real defect as a softer one, never manufacture a defect that was not there.
    """
    caps: dict[str, str] = {"lens": lens_severity}

    if calc_class == "recomputed_mismatch":
        # The lens's OWN arithmetic, redone by machine, does not reproduce its own
        # claimed result — the finding is asserting something the harness can show is
        # not what the cited numbers actually compute to.
        caps["lens"] = min(caps["lens"], "MINOR", key=lambda s: RANK[s])
    if verification_state == "incomplete":
        # Blank or degenerate falsification/steelman — see `pass_b_state`. `legacy`
        # (pre-`schema_version: 2`) is deliberately UNCAPPED here; see that function's
        # docstring for why a schema change must never silently erase old evidence.
        caps["lens"] = "NOTE"

    if not graded:
        # A caption- or equation-only citation caps its ceiling regardless of whether
        # grading ever runs — see `EVIDENCE_CEILING`. Nothing else in this branch
        # touches it, so an ordinary cell/prose-backed finding is unaffected.
        caps["evidence"] = EVIDENCE_CEILING.get(evidence_class, "FATAL")
        binding = min(caps, key=lambda k: RANK[caps[k]])
        return ("UNGRADED", caps[binding], binding,
                f"ungraded; lens severity {lens_severity} capped by {binding}={caps[binding]}")

    caps["verdict"] = _VERDICT_CAP.get(grade_verdict, "NOTE")
    if grade_severity in ("FATAL", "MAJOR", "MINOR"):
        # "NONE" (REFUTED/INSUFFICIENT have no severity of their own) is deliberately
        # NOT added as a cap — it would otherwise force counted_severity down to NOTE
        # regardless of `_VERDICT_CAP`, which is the `verdict` cap's job, not this one's.
        caps["grader"] = grade_severity
    caps["confidence"] = _CONFIDENCE_CAP.get(confidence, "MINOR")
    # The BETTER of the two citations sets the ceiling — a figure-only concern that a
    # blinded grader independently corroborated against a table cell is no longer
    # figure-only. Only when NEITHER citation clears cell/prose does the ceiling bite.
    caps["evidence"] = max(EVIDENCE_CEILING.get(evidence_class, "FATAL"),
                           EVIDENCE_CEILING.get(grader_evidence_class, "FATAL"),
                           key=lambda s: RANK[s])

    structural_hit = False
    if grade_verdict == "CONFIRMED" and not falsification_survived:
        caps["falsification_conceded"], structural_hit = "MINOR", True
    if grade_verdict in ("CONFIRMED", "PLAUSIBLE") and not has_impact_statement:
        caps["no_impact_statement"], structural_hit = "MINOR", True
    if grade_verdict == "CONFIRMED" and lens_severity in ("FATAL", "MAJOR") and not has_steelman:
        caps["no_steelman"], structural_hit = "MINOR", True
    if grade_verdict == "CONFIRMED" and grader_evidence_class == "unverified":
        caps["grader_cannot_cite"], structural_hit = "MINOR", True
    if not (evidence_class == "cell_verified" and grader_evidence_class == "cell_verified"):
        caps["fatal_needs_two_cells"] = "MAJOR"

    # A weak (LOW-confidence) refutation must not be able to zero out a lens's own
    # FATAL/MAJOR on its own say-so — that would let the grading subsystem DECIDE, in
    # the acquitting direction, on exactly the thin evidence it exists to catch.
    if grade_verdict == "REFUTED" and confidence == "LOW":
        grade_verdict = "PLAUSIBLE"
        caps["verdict"] = _VERDICT_CAP["PLAUSIBLE"]

    binding = min((k for k in _CAP_ORDER if k in caps), key=lambda k: RANK[caps[k]])
    counted = caps[binding]
    finding_class = _CLASS.get(grade_verdict, "OPEN_QUESTION")
    if finding_class == "CONFIRMED_FINDING" and structural_hit:
        # A grader that says CONFIRMED while conceding a reasonable non-problem reading,
        # or without stating impact or a steelman, has contradicted itself. Resolved
        # DOWNWARD, never upward — the same direction every other cap moves in.
        finding_class = "PLAUSIBLE_CONCERN"
    return (finding_class, counted, binding,
           f"graded {grade_verdict}/{grade_severity}/{confidence}; "
           f"counted={counted} bound by {binding}")


if __name__ == "__main__":  # self-check: python -m harness.grading
    assert pass_b_state({}, "MAJOR") == "legacy"
    assert pass_b_state({"schema_version": 2}, "MAJOR") == "incomplete"
    assert pass_b_state({
        "schema_version": 2, "statement": "s",
        "alternative_interpretation": "a" * 41, "why_alternative_fails": "b" * 41,
        "steelman": "c" * 41,
    }, "MAJOR") == "complete"
    assert pass_b_state({
        "schema_version": 2, "statement": "s",
        "alternative_interpretation": "a" * 41, "why_alternative_fails": "b" * 41,
    }, "MINOR") == "complete", "steelman not required below MAJOR"
    assert pass_b_state({
        "schema_version": 2,
        "alternative_interpretation": "n/a", "why_alternative_fails": "n/a", "steelman": "n/a",
    }, "FATAL") == "incomplete", "stoplisted text must not pass"

    def _v(quote, ref, corpus, by_idx, max_page=0):
        return ("cell_verified", "") if quote in corpus else ("unverified", "")

    corpus, by_idx = "76.4 and 76.1 are the two cells", {}
    ok = recheck_calculation({"applies": True,
                              "operands": [{"quote": "76.4", "ref": "x"}, {"quote": "76.1", "ref": "y"}],
                              "expression": "(76.4 - 76.1) / 76.1 * 100", "result": "0.394"},
                             _v, corpus, by_idx)
    assert ok == "recomputed_ok", ok
    bad = recheck_calculation({"applies": True,
                               "operands": [{"quote": "76.4", "ref": "x"}, {"quote": "76.1", "ref": "y"}],
                               "expression": "(76.4 - 76.1) / 76.1 * 100", "result": "9.0"},
                              _v, corpus, by_idx)
    assert bad == "recomputed_mismatch", bad
    unver = recheck_calculation({"applies": True,
                                 "operands": [{"quote": "not in corpus", "ref": "x"}],
                                 "expression": "1", "result": "1"},
                                _v, corpus, by_idx)
    assert unver == "operands_unverified", unver
    assert recheck_calculation({"applies": False}, _v, corpus, by_idx) == "not_applicable"
    assert recheck_calculation({}, _v, corpus, by_idx) == "not_applicable"
    unparse = recheck_calculation({"applies": True, "operands": [{"quote": "76.4", "ref": "x"}],
                                   "expression": "__import__('os')", "result": "1"},
                                  _v, corpus, by_idx)
    assert unparse == "unparseable", unparse
    huge = recheck_calculation({"applies": True, "operands": [{"quote": "76.4", "ref": "x"}],
                                "expression": "9**9**9", "result": "1"}, _v, corpus, by_idx)
    assert huge == "unparseable", huge

    # --- derive() -----------------------------------------------------------------
    def _d(**kw):
        defaults = dict(lens_severity="FATAL", verification_state="complete",
                        calc_class="not_applicable", graded=True, evidence_class="cell_verified",
                        grader_evidence_class="cell_verified", falsification_survived=True,
                        has_impact_statement=True, has_steelman=True)
        return derive(**{**defaults, **kw})

    # Ungraded: only lens-side capping applies. `derive` itself always names the
    # binding cap (here "lens", since nothing capped it below the asserted severity);
    # the convenience of leaving `counted_severity` BLANK when nothing actually changed
    # it is `stages.audit._coerce`'s business, not this function's.
    ungraded = derive(lens_severity="MAJOR", verification_state="complete",
                      calc_class="not_applicable", graded=False, evidence_class="cell_verified")
    assert ungraded[:3] == ("UNGRADED", "MAJOR", "lens"), ungraded
    capped = derive(lens_severity="MAJOR", verification_state="incomplete",
                    calc_class="not_applicable", graded=False, evidence_class="cell_verified")
    assert capped[1] == "NOTE" and capped[2] == "lens", capped
    mismatch = derive(lens_severity="FATAL", verification_state="complete",
                      calc_class="recomputed_mismatch", graded=False, evidence_class="cell_verified")
    assert mismatch[1] == "MINOR", mismatch

    # A caption-only citation caps at NOTE even with NO grading involved at all.
    caption_only = derive(lens_severity="MAJOR", verification_state="complete",
                          calc_class="not_applicable", graded=False,
                          evidence_class="caption_verified")
    assert caption_only[1] == "NOTE", caption_only
    equation_only = derive(lens_severity="MAJOR", verification_state="complete",
                           calc_class="not_applicable", graded=False,
                           evidence_class="equation_verified")
    assert equation_only[1] == "MINOR", equation_only
    # A figure discrepancy corroborated by a real cell citation from the GRADER lifts
    # the ceiling — the escape hatch is the point.
    corroborated = _d(grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH",
                      evidence_class="caption_verified", grader_evidence_class="cell_verified")
    assert corroborated[1] == "MAJOR", corroborated  # still capped by fatal_needs_two_cells

    # The one property that makes grading safe to turn on: it can only demote.
    for lens_sev in ("FATAL", "MAJOR", "MINOR"):
        for verdict in ("CONFIRMED", "PLAUSIBLE", "REFUTED", "INSUFFICIENT"):
            for conf in ("HIGH", "MEDIUM", "LOW"):
                for sh in (True, False):
                    fc, cs, cap, why = derive(
                        lens_severity=lens_sev, verification_state="complete",
                        calc_class="not_applicable", graded=True, grade_verdict=verdict,
                        grade_severity=lens_sev, confidence=conf,
                        falsification_survived=sh, has_impact_statement=sh,
                        has_steelman=sh, evidence_class="cell_verified",
                        grader_evidence_class="cell_verified")
                    assert RANK[cs] <= RANK[lens_sev], (lens_sev, verdict, conf, sh, cs)

    # The same promotion-safety property, swept over evidence classes too (ungraded).
    for lens_sev in ("FATAL", "MAJOR", "MINOR"):
        for ec in EVIDENCE_CEILING:
            _, cs, _, _ = derive(lens_severity=lens_sev, verification_state="complete",
                                 calc_class="not_applicable", graded=False, evidence_class=ec)
            assert RANK[cs] <= RANK[lens_sev], (lens_sev, ec, cs)

    # A FATAL needs BOTH readers' citations to be cell-verified.
    both_cells = _d(grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH")
    assert both_cells[1] == "FATAL", both_cells
    one_prose = _d(grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH",
                   grader_evidence_class="prose_verified")
    assert one_prose[1] == "MAJOR", one_prose

    # Low confidence can never reach FATAL/MAJOR.
    low = _d(grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="LOW")
    assert low[1] == "MINOR", low

    # A LOW-confidence refutation does not zero a FATAL — it becomes a MINOR concern.
    weak_refute = _d(grade_verdict="REFUTED", grade_severity="NONE", confidence="LOW")
    assert weak_refute == ("PLAUSIBLE_CONCERN", "MINOR", weak_refute[2], weak_refute[3])
    # A confident refutation counts for nothing.
    strong_refute = _d(grade_verdict="REFUTED", grade_severity="NONE", confidence="HIGH")
    assert strong_refute[0] == "REFUTED" and strong_refute[1] == "NOTE", strong_refute

    # INSUFFICIENT is a question, not a finding — counts for nothing.
    insuff = _d(grade_verdict="INSUFFICIENT", grade_severity="NONE", confidence="LOW")
    assert insuff == ("OPEN_QUESTION", "NOTE", insuff[2], insuff[3])

    # A CONFIRMED that concedes its own falsification cannot stay CONFIRMED.
    conceded = _d(grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH",
                  falsification_survived=False)
    assert conceded[0] == "PLAUSIBLE_CONCERN" and conceded[1] == "MINOR", conceded

    # Disagreement resolves to the minimum, in both directions.
    lens_high_grader_low = derive(lens_severity="FATAL", verification_state="complete",
                                  calc_class="not_applicable", graded=True,
                                  grade_verdict="CONFIRMED", grade_severity="MINOR",
                                  confidence="HIGH", falsification_survived=True,
                                  has_impact_statement=True, has_steelman=True,
                                  evidence_class="cell_verified",
                                  grader_evidence_class="cell_verified")
    assert lens_high_grader_low[1] == "MINOR", lens_high_grader_low
    lens_low_grader_high = derive(lens_severity="MINOR", verification_state="complete",
                                  calc_class="not_applicable", graded=True,
                                  grade_verdict="CONFIRMED", grade_severity="FATAL",
                                  confidence="HIGH", falsification_survived=True,
                                  has_impact_statement=True, has_steelman=True,
                                  evidence_class="cell_verified",
                                  grader_evidence_class="cell_verified")
    assert lens_low_grader_high[1] == "MINOR", "the grader cannot promote a lens's MINOR"

    print("grading self-check OK")
