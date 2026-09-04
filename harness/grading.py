"""Pure derivation logic for what a finding's severity actually earns. No I/O, no model
call, no Config — every function here is data in, data out, so the rule that decides
what the verdict counts is as inspectable and arguable as the threshold table in
`stages/report.py`.

THE THREE QUESTIONS ARE NOT ONE QUESTION. This module exists so that "is there an
issue", "how sure are we", and "how much does it matter" cannot collapse into a single
finding-implies-MAJOR path:

  IS THERE AN ISSUE   the grader's `verdict`, and the lens's own `candidate_class` —
                      CONFIRMED_FINDING is a different claim from PLAUSIBLE_CONCERN, and
                      `CANDIDATE_CAP` makes the difference count.
  HOW SURE ARE WE     confidence, bounded by what the cited evidence can actually
                      support (`evidence_support`) and lifted by corroboration.
  HOW MUCH DOES IT    severity. The only one of the three any threshold counts, and the
  MATTER              one nothing here sets — only caps.

Two axes feed `Finding.counted_severity`, the field `stages.report.counted` reads
instead of the lens's raw `severity`:

  1. LENS-SIDE SELF-VERIFICATION (this module, always active once a lens writes
     `schema_version: 2`). Did the lens's own falsification/steelman work show real
     work, or is it blank/degenerate? Did its own arithmetic, if it made any, actually
     reproduce the number it disputed? Is its own severity consistent with its own
     candidate class, confidence and baseline classification?
     `pass_b_state` / `recheck_calculation` / the self-consistency caps in `derive`.
  2. GRADER-SIDE INDEPENDENT JUDGEMENT (Stage 3/4 — a second, blinded reviewer's
     verdict on the SAME candidate). `derive` combines both axes into one line.

Neither axis, nor their combination, can ever PROMOTE a lens's own asserted severity —
only cap it. `derive`'s one safety property, asserted by test over the whole reachable
table: `RANK[counted_severity] <= RANK[lens_severity]`. Grading can only move a verdict
toward GREEN.

WHAT IS DELIBERATELY NOT EXPRESSIBLE HERE. `derive`'s signature admits vocabulary values
and booleans only — no counts, no deltas, no seed numbers, no metric names, no paper
identity. "No standard deviation reported = MAJOR", "epsilon below 0.05 is fine", "this
paper gets a softer grade" are not rules this function could be made to hold even by
someone trying. `tests/test_reasoning_architecture.py` asserts the signature.
"""
from __future__ import annotations

import ast
import operator

RANK = {"FATAL": 3, "MAJOR": 2, "MINOR": 1, "NOTE": 0, "": 0}

_STOPLIST = {"n/a", "na", "none", "not applicable", "see above", "same as above", ""}
_MIN_CHARS = 40


def _flat(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def pass_b_state(f: dict, severity: str, schema_version=None) -> str:
    """'legacy' | 'incomplete' | 'complete', for the falsification/steelman triple.

    'legacy': the file predates `schema_version: 2` — every finding on it is UNCAPPED so
    a schema change can never silently erase the existing evidence base, the same
    guarantee `tests/test_finding_traceability.py:170-183` already makes about an
    earlier split.

    'incomplete': a required field is blank, under `_MIN_CHARS` once flattened, on the
    stoplist, or identical to `statement`/`claim`/`title` — i.e. present but not really
    written. `steelman` is required only when the lens itself asserted FATAL or MAJOR;
    a MINOR does not need a defense of the authors to be worth printing.

    `schema_version` IS THE REPORT'S, not the finding's, and it has to be passed in.
    `harness/prompts/audit.py`'s `_RETURN` puts `"schema_version": 2` at the top level of
    the lens report — beside `findings`, `unasked_question` and `notes` — so reading it
    off the finding dict, which is what this function used to do, found nothing on every
    real lens file ever produced. Every finding came back `legacy`, `legacy` is
    deliberately uncapped, and the whole non-degeneracy check was therefore inert in
    production while passing its own unit tests, which construct the flag inside the
    finding. The fallback to `f.get(...)` is kept so both callers work.
    """
    version = schema_version if schema_version is not None else f.get("schema_version")
    if version != 2:
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

# §3 — "Never promote a suspicion directly to a confirmed finding." `CANDIDATE_CLASSES`
# has always DOCUMENTED that only CONFIRMED_FINDING is eligible to carry FATAL/MAJOR and
# that a PLAUSIBLE_CONCERN or OPEN_QUESTION counts toward no threshold; nothing enforced
# it, so a lens could sort its own candidate into "I could not rule out an alternative
# explanation" and still have it counted at MAJOR. Keyed on the lens's OWN bucket, so
# this holds a finding to what its author already said and cannot invent a demotion.
CANDIDATE_CAP = {"CONFIRMED_FINDING": "FATAL", "PLAUSIBLE_CONCERN": "MINOR",
                 "OPEN_QUESTION": "NOTE", "DISMISSED": "NOTE", "": "FATAL"}

# --------------------------------------------------------------------------- #
# Evidence strength — a bound on CONFIDENCE, not a severity ceiling of its own
# --------------------------------------------------------------------------- #
# An earlier version of this table capped severity DIRECTLY by citation type:
# `caption_verified` could never exceed NOTE, `equation_verified` never MINOR, whatever
# else the finding established. That is a universal, evidence-type-keyed severity rule —
# structurally the same move as "no standard deviation = MAJOR", just pointed the other
# way — and it produced the wrong answer for the same reason: it decided IMPACT from a
# fact about the citation rather than from what the finding actually showed. A caption
# that names the very quantity a headline claim rests on, whose arithmetic the harness
# then independently reproduced, is not a NOTE.
#
# What a weak citation genuinely bounds is HOW SURE anyone can be. A caption reports no
# plotted values; display-equation extraction drops and mangles symbols. So evidence type
# sets a ceiling on CONFIDENCE, confidence caps severity through `_CONFIDENCE_CAP` below,
# and severity is left to be earned by impact. Verbatim section text is not weak evidence
# and carries no penalty — it is the paper's own words, checked character for character.
EVIDENCE_CONFIDENCE_CEILING = {
    "cell_verified": "HIGH",
    "prose_verified": "HIGH",
    "equation_verified": "MEDIUM",
    "caption_verified": "LOW",
    "unverified": "LOW",
}

CONFIDENCE_RANK = {"HIGH": 2, "MEDIUM": 1, "LOW": 0, "": 0}
_CONFIDENCE_BY_RANK = {2: "HIGH", 1: "MEDIUM", 0: "LOW"}

# CORROBORATION LIFTS THE CEILING, one step per additional independent check. This is the
# other half of the same principle: two weak-but-independent citations of the same concern
# are not as weak as one. `recheck_calculation`'s `recomputed_ok` counts as a corroborating
# source in its own right — it means the HARNESS re-verified both operands against the
# parsed paper and re-evaluated the lens's own expression to the lens's own stated result,
# which is a machine check, not a citation strength.
_CORROBORATING_CALC = ("recomputed_ok",)


def evidence_support(evidence_class: str, grader_evidence_class: str = "unverified",
                     calc_class: str = "not_applicable") -> tuple[str, tuple[str, ...]]:
    """(confidence_ceiling, the sources that produced it) — pure, no severity involved.

    The ceiling starts at the STRONGEST single citation's own ceiling and is lifted one
    confidence step for each further independent source, capped at HIGH. So:

      caption alone                     -> LOW      (a caption reports no plotted values)
      caption + harness-recomputed math -> MEDIUM
      caption + grader's own table cell -> MEDIUM
      caption + cell + recomputed math  -> HIGH
      one table cell                     -> HIGH     (no penalty; nothing to lift)

    Nothing here can lower the ceiling below what the best single source supports, and
    nothing can raise it above HIGH — so it is monotone in evidence and cannot invent
    confidence out of two unverified citations (both floor at LOW, and `unverified`
    findings are dropped upstream by invariant #1 anyway).
    """
    # (label, own ceiling rank). A recomputation carries no citation strength of its own —
    # it corroborates whatever was cited — so it enters at LOW and contributes only a lift.
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


# §10 — a missing comparison the reviewer ITSELF classified as optional cannot then be
# counted as a validity threat. This is self-consistency, not a severity checklist: the
# cap is keyed on the lens's own answer to "would this change the interpretation", so it
# can only ever hold a finding to what its author already said about it.
BASELINE_CAP = {"OPTIONAL_COMPARISON": "NOTE", "USEFUL_CONTROL": "MINOR",
                "IMPORTANT_MISSING_BASELINE": "MAJOR", "CENTRAL_VALIDITY_THREAT": "FATAL",
                "NOT_APPLICABLE": "FATAL", "": "FATAL"}

# §10 — a novelty/prior-art claim resting on the reviewer's recollection of outside
# literature has no page in THIS paper behind its load-bearing half. The paper-internal
# quote it does carry is real, so the finding is not dropped; it is held to what an
# unverifiable external comparison can support.
PRIOR_ART_CAP = {"PAPER_INTERNAL": "FATAL", "EXTERNAL_VERIFIED": "MAJOR",
                 "REVIEWER_INFERENCE": "MINOR", "NOT_APPLICABLE": "FATAL", "": "FATAL"}

# Every cap that binds is named, in this fixed order, so the same inputs always name the
# same binding cap and `derive` is total and reproducible — the same discipline
# `stages.report.finding_key` already has for its own tiebreak order.
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
    """(finding_class, counted_severity, binding_cap, derivation) — the ONE function that
    decides what a finding actually counts as, from data alone. No model call, no I/O.

    THE THREE QUESTIONS ARE KEPT APART, which is the whole reason this is a table and not
    a judgement. "Is there an issue" is `grade_verdict`. "How sure are we" is confidence,
    bounded by what the evidence can support (`evidence_support`). "How much does it
    matter" is severity, and it is the only one of the three that any threshold counts.
    Confidence CAPS severity and never sets it; evidence type caps CONFIDENCE and never
    severity directly (see `EVIDENCE_CONFIDENCE_CEILING` for why the earlier direct
    severity ceiling was the wrong shape).

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

    # §10 — self-consistency caps, applied in both branches. Each is keyed on the lens's
    # OWN classification of its own finding, so neither can hold a finding below what its
    # author already conceded about it, and neither is an evidence-type or
    # absence-of-evidence rule.
    if candidate_class:
        caps["candidate_class"] = CANDIDATE_CAP.get(candidate_class, "FATAL")
    if baseline_class:
        caps["baseline_class"] = BASELINE_CAP.get(baseline_class, "FATAL")
    if prior_art_basis:
        caps["prior_art_basis"] = PRIOR_ART_CAP.get(prior_art_basis, "FATAL")

    # §13, narrowly. The lens's declared confidence is unchecked self-assertion, so it is
    # NOT admitted as a general cap on the ungraded path — but a lens that declares LOW
    # confidence and asserts FATAL/MAJOR anyway has contradicted the one combination its
    # own prompt tells it not to produce ("A HIGH-severity, LOW-confidence combination is
    # usually wrong"). That self-contradiction is checkable without trusting either half.
    if lens_confidence == "LOW" and lens_severity in ("FATAL", "MAJOR"):
        caps["self_contradictory_confidence"] = "MINOR"

    ceiling, support = evidence_support(evidence_class, grader_evidence_class, calc_class)

    if not graded:
        # Evidence type bounds CONFIDENCE, and that ceiling bounds severity — so a
        # caption-only concern is held to what LOW confidence can carry (MINOR), not
        # flattened to NOTE, and the same caption corroborated by a machine-recomputed
        # calculation reaches MEDIUM and so MAJOR. An ordinary cell- or prose-backed
        # finding has a HIGH ceiling and is untouched, which is what keeps a run with
        # grading off identical to the pre-grading verdict for every such finding.
        caps["confidence"] = _CONFIDENCE_CAP[ceiling]
        binding = min((k for k in _CAP_ORDER if k in caps), key=lambda k: RANK[caps[k]])
        return ("UNGRADED", caps[binding], binding,
                f"ungraded; lens severity {lens_severity} capped by {binding}="
                f"{caps[binding]}; evidence supports at most {ceiling} confidence "
                f"from {'+'.join(support) or 'nothing'}")

    caps["verdict"] = _VERDICT_CAP.get(grade_verdict, "NOTE")
    if grade_severity in ("FATAL", "MAJOR", "MINOR"):
        # "NONE" (REFUTED/INSUFFICIENT have no severity of their own) is deliberately
        # NOT added as a cap — it would otherwise force counted_severity down to NOTE
        # regardless of `_VERDICT_CAP`, which is the `verdict` cap's job, not this one's.
        caps["grader"] = grade_severity
    # The grader's declared confidence, held to what the evidence can actually support.
    # Neither half alone: a grader claiming HIGH off a lone figure caption is claiming
    # more than a caption can give, and a grader claiming LOW off two verified cells is
    # still only as sure as it said it was.
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
    # FATAL — the one grade that says a central claim does not stand — requires TWO
    # independent checks, not one. A machine-reproduced calculation counts as one of them:
    # `recomputed_ok` means the harness re-verified both operands against the parsed paper
    # and re-evaluated the lens's own expression to the lens's own result, which is a
    # stronger check than a second citation, not a weaker one. Requiring literally two
    # table cells excluded exactly that case for no reason a reader could defend.
    cells = sum(1 for c in (evidence_class, grader_evidence_class) if c == "cell_verified")
    if not (cells >= 2 or (cells >= 1 and calc_class == "recomputed_ok")):
        caps["fatal_needs_corroboration"] = "MAJOR"

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
           f"graded {grade_verdict}/{grade_severity}/{confidence}; evidence supports at "
           f"most {ceiling} confidence from {'+'.join(support) or 'nothing'}, so effective "
           f"confidence {effective}; counted={counted} bound by {binding}")


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
    # The REPORT's version, passed in. This is the calling convention `stages/audit._coerce`
    # uses, and the one that was missing: without it every real finding read `legacy`.
    real = {"statement": "s", "alternative_interpretation": "a" * 41,
            "why_alternative_fails": "b" * 41, "steelman": "c" * 41}
    assert pass_b_state(real, "MAJOR") == "legacy", "no version anywhere is still legacy"
    assert pass_b_state(real, "MAJOR", 2) == "complete", \
        "the report-level version must reach the check"
    assert pass_b_state({"statement": "s"}, "MAJOR", 2) == "incomplete"
    assert pass_b_state(real, "MAJOR", 1) == "legacy", "an older contract is legacy"

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

    # --- evidence strength bounds CONFIDENCE, and corroboration lifts it --------------
    assert evidence_support("cell_verified")[0] == "HIGH"
    assert evidence_support("prose_verified")[0] == "HIGH"
    assert evidence_support("equation_verified")[0] == "MEDIUM"
    assert evidence_support("caption_verified")[0] == "LOW"
    assert evidence_support("unverified")[0] == "LOW"
    # One more independent source lifts one step from the STRONGEST source's own ceiling.
    assert evidence_support("caption_verified", calc_class="recomputed_ok")[0] == "MEDIUM"
    assert evidence_support("equation_verified", calc_class="recomputed_ok")[0] == "HIGH"
    # A cell citation is already HIGH on its own, so a weak second source adds nothing —
    # corroboration lifts a ceiling, it does not stack past one.
    assert evidence_support("caption_verified", "cell_verified")[0] == "HIGH"
    assert evidence_support("caption_verified", "cell_verified", "recomputed_ok")[0] == "HIGH"
    # A failed recomputation is not a corroborating source.
    assert evidence_support("caption_verified", calc_class="recomputed_mismatch")[0] == "LOW"
    # Monotone: adding a source never lowers the ceiling.
    for ec in EVIDENCE_CONFIDENCE_CEILING:
        base = CONFIDENCE_RANK[evidence_support(ec)[0]]
        for gc in EVIDENCE_CONFIDENCE_CEILING:
            for cc in ("not_applicable", "recomputed_ok"):
                assert CONFIDENCE_RANK[evidence_support(ec, gc, cc)[0]] >= base, (ec, gc, cc)

    # A caption-only citation is held to what LOW confidence carries — MINOR, NOT the
    # blanket NOTE an earlier evidence-type severity ceiling imposed.
    caption_only = derive(lens_severity="MAJOR", verification_state="complete",
                          calc_class="not_applicable", graded=False,
                          evidence_class="caption_verified")
    assert caption_only[1] == "MINOR", caption_only
    # …and a caption whose arithmetic the HARNESS independently reproduced reaches MAJOR.
    # "Corroborated evidence can collectively support a stronger finding" is the rule
    # this asserts; it is exactly what the old universal caption→NOTE cap made impossible.
    caption_plus_math = derive(lens_severity="MAJOR", verification_state="complete",
                               calc_class="recomputed_ok", graded=False,
                               evidence_class="caption_verified")
    assert caption_plus_math[1] == "MAJOR", caption_plus_math
    equation_only = derive(lens_severity="MAJOR", verification_state="complete",
                           calc_class="not_applicable", graded=False,
                           evidence_class="equation_verified")
    assert equation_only[1] == "MAJOR", equation_only          # MEDIUM ceiling -> MAJOR
    # A figure discrepancy corroborated by a real cell citation from the GRADER lifts
    # the ceiling — the escape hatch is the point.
    corroborated = _d(grade_verdict="CONFIRMED", grade_severity="FATAL", confidence="HIGH",
                      evidence_class="caption_verified", grader_evidence_class="cell_verified")
    assert corroborated[1] == "MAJOR", corroborated   # MEDIUM ceiling, and one cell only

    # --- self-consistency caps (§3, §10, §13) ----------------------------------------
    # A lens that sorted its own candidate below CONFIRMED_FINDING cannot have it counted
    # as one, however severe it also called it.
    assert derive(lens_severity="FATAL", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  candidate_class="PLAUSIBLE_CONCERN")[1] == "MINOR"
    assert derive(lens_severity="FATAL", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  candidate_class="OPEN_QUESTION")[1] == "NOTE"
    assert derive(lens_severity="FATAL", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  candidate_class="DISMISSED")[1] == "NOTE"
    assert derive(lens_severity="FATAL", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  candidate_class="CONFIRMED_FINDING")[1] == "FATAL"
    # A missing comparison the lens itself called optional is not a validity threat.
    assert derive(lens_severity="MAJOR", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  baseline_class="OPTIONAL_COMPARISON")[1] == "NOTE"
    assert derive(lens_severity="MAJOR", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  baseline_class="CENTRAL_VALIDITY_THREAT")[1] == "MAJOR"
    # A prior-art claim resting on the reviewer's own recollection is held to MINOR.
    assert derive(lens_severity="FATAL", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  prior_art_basis="REVIEWER_INFERENCE")[1] == "MINOR"
    # LOW confidence + FATAL is the one combination the prompt forbids outright.
    assert derive(lens_severity="FATAL", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  lens_confidence="LOW")[1] == "MINOR"
    assert derive(lens_severity="MINOR", verification_state="complete",
                  calc_class="not_applicable", graded=False, evidence_class="cell_verified",
                  lens_confidence="LOW")[1] == "MINOR", "a LOW-confidence MINOR is coherent"

    # A FATAL still needs two independent checks — but a machine-reproduced calculation
    # is now admissible as the second, which literal "two table cells" excluded.
    one_cell_one_calc = _d(grade_verdict="CONFIRMED", grade_severity="FATAL",
                           confidence="HIGH", calc_class="recomputed_ok",
                           grader_evidence_class="prose_verified")
    assert one_cell_one_calc[1] == "FATAL", one_cell_one_calc

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

    # The same promotion-safety property, swept over every new axis too (ungraded).
    for lens_sev in ("FATAL", "MAJOR", "MINOR"):
        for ec in EVIDENCE_CONFIDENCE_CEILING:
            for cand in ("", *CANDIDATE_CAP):
                for base in ("", *BASELINE_CAP):
                    for lc in ("", "HIGH", "MEDIUM", "LOW"):
                        _, cs, _, _ = derive(
                            lens_severity=lens_sev, verification_state="complete",
                            calc_class="not_applicable", graded=False, evidence_class=ec,
                            candidate_class=cand, baseline_class=base, lens_confidence=lc)
                        assert RANK[cs] <= RANK[lens_sev], (lens_sev, ec, cand, base, lc, cs)

    # A FATAL needs two independent checks; two cells is the canonical pair.
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
