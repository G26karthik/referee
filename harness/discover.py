"""What is worth checking in this paper, whether it is checkable, and the plan for each.

Deterministic and model-free throughout: every judgement here is derived from the
paper's own structure (`PaperDoc`) and from findings' own closed-vocabulary
self-classification, never from a number, a metric name or a paper identity -- the same
signature discipline `decide.py`'s pure functions already hold to.

`python -m harness.discover` runs the self-check.
"""
from __future__ import annotations

import re

from . import decide, locate
from .experiment_id import own_method
from .schema import (
    ClaimRef, DiscoveredObject, Finding, PaperDoc, PlanDecision, ReimplementationIngredient,
    ReimplementationReadiness, ReviewQuestion, TargetOutcome, TargetSet,
)

# ========================================================================================
# PATH B ELIGIBILITY — can this paper be reconstructed well enough to test, when the
# authors published no code? Never infers a missing ingredient from a present one and
# never fills a gap with a plausible default -- a paper that omits its optimizer does
# not get Adam. Keyword/structure detection, not a model; conservative toward refusal.
# ========================================================================================
INGREDIENTS: tuple[tuple[str, bool, tuple[str, ...]], ...] = (
    ("method", True, ("algorithm", "pseudocode", "we define", "is defined as", "objective",
                      "loss function", "our method", "we propose", "formulation")),
    ("architecture", False, ("architecture", "layer", "encoder", "decoder", "hidden",
                             "embedding", "network", "backbone", "kernel", "estimator")),
    ("preprocessing", False, ("preprocess", "normali", "augment", "tokeni", "resize",
                              "standardi", "cleaning", "filtering")),
    # CONCRETE PROCEDURE ONLY. "We train the model" asserts a procedure exists, not the
    # procedure -- an implementer must not be sent to invent it (no optimizer, no LR).
    ("training", True, ("optimizer", "adam", "sgd", "rmsprop", "adagrad", "learning rate",
                        "epoch", "batch size", "weight decay", "momentum", "lr=",
                        "iterations", "training steps")),
    ("dataset", True, ("dataset", "corpus", "benchmark", "we evaluate on", "test set",
                       "training set", "samples", "records")),
    ("metric", True, ("accuracy", "precision", "recall", "f1", "f-score", "auc", "bleu",
                      "perplexity", "error rate", "mse", "rmse", "map", "iou", "dice")),
    ("hyperparameters", False, ("learning rate", "batch size", "weight decay", "dropout",
                                "momentum", "temperature", "hidden size", "num_layers",
                                "seed")),
)
_ALGORITHM_BLOCK = re.compile(r"\balgorithm\s+\d+\b", re.I)
_MAX_QUOTE = 200
# Title page and bibliography describe OTHER work: a cited "Adam Stein" is not an optimizer.
_NOT_THE_PAPER = ("references", "bibliography", "acknowledg")


def _needle(n: str) -> re.Pattern:
    """Word-start match; a short word needle must also end a word ("map" is not "mapping")."""
    tail = r"(?![a-z])" if len(n) <= 4 and n[-1].isalpha() else ""
    return re.compile(r"\b" + re.escape(n) + tail, re.I)


def _target_table(doc: PaperDoc, ref: str = ""):
    """(table, printed label) for a table-cell target, else (None, "")."""
    m = re.match(r"^T(\d+)", ref or "")
    t = next((t for t in doc.tables if m and t.table_idx == int(m.group(1))), None)
    return t, ((t.label if t else "") or "")


def target_sections(doc: PaperDoc, ref: str = "") -> tuple[int, ...]:
    """Sections that belong to ONE target's experiment: those naming its table's printed
    label ("Table 1"), or the section its prose claim sits in. Empty when unknown."""
    m = re.match(r"^T(\d+)", ref or "")
    if m:
        t = next((t for t in doc.tables if t.table_idx == int(m.group(1))), None)
        label = (t.label if t else "") or ""
        if not label:
            return ()
        pat = re.compile(rf"\bTable\s+{re.escape(label)}\b", re.I)
        return tuple(s.section_idx for s in doc.sections if pat.search(s.text or ""))
    m = re.match(r"^[SsPp](\d+)", ref or "")
    return (int(m.group(1)),) if m else ()


def _find(needles: tuple[str, ...], doc: PaperDoc,
          prefer: tuple[int, ...] = ()) -> tuple[str, str]:
    """First (locator, quote) matching any needle -- the surrounding sentence."""
    # A target's own sections first: the first paper-wide keyword hit is usually a
    # DIFFERENT experiment, and a brief quoting it sends the implementer there.
    ordered = ([s for s in doc.sections if s.section_idx in prefer]
               + [s for s in doc.sections if s.section_idx not in prefer])
    for s in ordered:
        title = (s.title or "").strip().lower()
        if title.startswith(_NOT_THE_PAPER) or (
                not title and s.section_idx == 0 and len(doc.sections) > 1):
            continue
        low = s.text.lower()
        for n in needles:
            m = _needle(n).search(s.text)
            if not m:
                continue
            i = m.start()
            start = max(0, low.rfind(".", 0, i) + 1)
            end = low.find(".", i)
            end = len(s.text) if end < 0 else end + 1
            quote = " ".join(s.text[start:end].split())[:_MAX_QUOTE]
            return f"s{s.section_idx}", quote
    return "", ""


def reimplementation_readiness(doc: PaperDoc, target_ref: str = "") -> ReimplementationReadiness:
    """Is there enough in this paper to rebuild the experiment independently? Every
    ingredient carries the locator it was found at, so the judgement is auditable. With
    `target_ref`, each ingredient is sought in that target's own sections first; WHETHER
    it is present anywhere in the paper does not change."""
    found: list[ReimplementationIngredient] = []
    prefer = target_sections(doc, target_ref)
    table, label = _target_table(doc, target_ref)
    claim = (locate.resolve(doc, target_ref)
             if table is None and re.match(r"^[Pp]\d+:", target_ref or "") else None)
    for name, required, needles in INGREDIENTS:
        ref, quote = _find(needles, doc, prefer)
        # A keyword hit OUTSIDE the target's own sections describes another experiment;
        # the paper's own sentence presenting the target (its table, or the claim
        # itself) is the honest locator.
        if (prefer and name in ("method", "dataset", "metric") and ref.startswith("s")
                and int(ref[1:]) not in prefer):
            if label:
                ref, quote = _find((f"Table {label}",), doc, prefer)
            elif claim is not None and claim.resolved:
                ref, quote = claim.ref, claim.quote[:_MAX_QUOTE]
        # A prose target's printed quantity names what is measured, whatever else its
        # section mentions.
        if name == "metric" and claim is not None and claim.resolved and claim.quantity:
            ref, quote = claim.ref, claim.quote[:_MAX_QUOTE]
        if name == "method" and not ref:
            if doc.equations:
                e = doc.equations[0]
                ref, quote = f"E{getattr(e, 'number', 1)}", (getattr(e, "body", "") or "")[:_MAX_QUOTE]
            else:
                for s in doc.sections:
                    if _ALGORITHM_BLOCK.search(s.text):
                        ref = f"s{s.section_idx}"
                        quote = " ".join(s.text.split())[:_MAX_QUOTE]
                        break
        if name == "metric" and not ref:
            # A printed results table names its own quantity (header or caption); a word
            # list cannot enumerate every field's metrics, and the verifier checks the rest.
            t = next((t for t in doc.tables if t.rows and (t.header or t.caption)), None)
            if t is not None:
                ref = f"T{t.table_idx}"
                quote = (t.caption or " | ".join(t.header))[:_MAX_QUOTE]
        found.append(ReimplementationIngredient(
            kind=name, required=required, present=bool(ref), ref=ref, quote=quote))

    # A comparison target is an ingredient too -- without an addressed cell, any run
    # produces a number in a vacuum. The target's OWN cell when there is one.
    cell = locate.resolve(doc, target_ref) if table is not None else claim
    if cell is not None and cell.resolved and (table is not None or cell.quantity):
        found.append(ReimplementationIngredient(
            kind="comparison_target", required=True, present=True, ref=cell.ref,
            quote=(f"{cell.quote} ({(table.caption or '')[:_MAX_QUOTE]})" if table is not None
                   else cell.quote[:_MAX_QUOTE])))
    else:
        has_target = any(t.rows for t in doc.tables)
        found.append(ReimplementationIngredient(
            kind="comparison_target", required=True, present=has_target,
            ref=f"T{doc.tables[0].table_idx}" if has_target and doc.tables else "",
            quote=(doc.tables[0].caption[:_MAX_QUOTE] if has_target and doc.tables else "")))

    missing = [i.kind for i in found if i.required and not i.present]
    established = not missing
    reason = (
        "every required ingredient is present in the paper, so an independent "
        "reimplementation can be attempted and compared against a printed cell"
        if established else
        "the paper does not supply " + ", ".join(missing) +
        f" — an implementation would have to invent {'that' if len(missing) == 1 else 'those'}, "
        "which would make any result a statement about the invention rather than the paper")
    return ReimplementationReadiness(
        established=established, ingredients=found, missing=missing, reason=reason)


# ========================================================================================
# QUESTIONS — a finding says what is wrong; a question says what would settle it.
# Templated from a finding's own closed-vocabulary self-classification, never from its
# prose -- deterministic and paper-agnostic, like `decide.plan`/`decide.score`.
# ========================================================================================
_BY_DISCREPANCY = {
    "ARITHMETIC_ERROR": (
        "Do the paper's own printed numbers actually compose to the total it reports?",
        "An arithmetic slip in a headline quantity changes what every downstream number "
        "is a fraction of."),
    "GENUINE_CONTRADICTION": (
        "Which of the paper's two conflicting statements is the one the conclusion rests on?",
        "A conclusion drawn from the more favourable of two incompatible numbers is not "
        "supported by either."),
    "DIFFERENT_DENOMINATOR": (
        "Are the two quantities being compared normalised over the same population?",
        "A ratio compared against a ratio with a different denominator is not a comparison."),
    "DEFINITIONAL_MISMATCH": (
        "Is the quantity the paper evaluates the quantity its claim is about?",
        "Evaluating a proxy and reporting the claim in terms of the target quantity "
        "overstates what was measured."),
    "UNCLEAR_REPORTING": (
        "What exactly was measured, and over what?",
        "A number whose definition cannot be recovered from the paper cannot be checked "
        "by anyone, including a future reader of the authors' own work."),
}
_BY_BASELINE = {
    "CENTRAL_VALIDITY_THREAT": (
        "Does the proposed method still outperform the strongest appropriate baseline?",
        "The comparison the paper omits is the one its central claim is about."),
    "IMPORTANT_MISSING_BASELINE": (
        "Does the reported advantage survive comparison against the missing baseline?",
        "An advantage measured only against weaker alternatives may be an artefact of the "
        "comparison set."),
    "USEFUL_CONTROL": (
        "Would the missing control change how the reported gain is attributed?",
        "Without the control, a simpler explanation for the gain remains open."),
}
_BY_LENS = {
    "confound": (
        "Is the reported gain attributable to the mechanism the paper credits, or to a "
        "change made at the same time?",
        "Several things changed together, so the attribution the paper makes is one of "
        "several readings the evidence permits."),
    "protocol": (
        "Is the evaluation protocol appropriate for the claim being drawn from it?",
        "A protocol that does not isolate the claimed effect cannot establish it, however "
        "large the reported number."),
    "overclaim": (
        "Is the stated conclusion stronger than the reported evidence supports?",
        "The gap between what was measured and what is asserted is what a reader inherits "
        "when they cite the paper."),
    "contradiction": (
        "Do the paper's own reported quantities agree with the claim drawn from them?",
        "An internal disagreement means at least one of the two statements is wrong, and "
        "the paper does not say which."),
}
_DEFAULT = (
    "What evidence would settle this concern one way or the other?",
    "An unresolved concern about a central claim is what a first-round review exists to "
    "surface for a human reviewer.")

_KIND_BY_DISCREPANCY = {
    "ARITHMETIC_ERROR": "COMPOSITION", "GENUINE_CONTRADICTION": "PRINTED_QUANTITY",
    "DIFFERENT_DENOMINATOR": "SPECIFICATION", "DEFINITIONAL_MISMATCH": "SPECIFICATION",
    "UNCLEAR_REPORTING": "SPECIFICATION",
}
_KIND_BY_BASELINE = {
    "CENTRAL_VALIDITY_THREAT": "CONTROL_PRESENCE", "IMPORTANT_MISSING_BASELINE": "CONTROL_PRESENCE",
    "USEFUL_CONTROL": "CONTROL_PRESENCE",
}
_KIND_BY_LENS = {
    "confound": "ATTRIBUTION", "protocol": "PROTOCOL_CONFORMANCE",
    "overclaim": "PRINTED_QUANTITY", "contradiction": "PRINTED_QUANTITY",
}
# The kind for an object the EXTRACTOR found rather than a lens, so every executable
# target carries a question_id.
_KIND_BY_DISCOVERY_KIND = {
    "EXPERIMENTAL_RESULT": "PRINTED_QUANTITY", "DATASET_RESULT": "PRINTED_QUANTITY",
    "REPRODUCTION_TARGET": "COMPOSITION", "BASELINE_COMPARISON": "CONTROL_PRESENCE",
    "ABLATION": "ATTRIBUTION", "CONTROL": "CONTROL_PRESENCE",
    "IMPLEMENTATION_CLAIM": "PROTOCOL_CONFORMANCE", "ERROR_ANALYSIS": "SPECIFICATION",
    "SCIENTIFIC_CLAIM": "UNCLASSIFIED", "UNANSWERED_REVIEW_QUESTION": "UNCLASSIFIED",
}
_TEMPLATE_BY_DISCOVERY_KIND = {
    "PRINTED_QUANTITY": ("Does the released implementation produce the value the paper "
                        "prints here?",
                        "A reported result nobody can re-derive is a result a reader has "
                        "to take on trust."),
    "COMPOSITION": _BY_DISCREPANCY["ARITHMETIC_ERROR"],
    "CONTROL_PRESENCE": ("Is the comparison this result is measured against actually "
                        "present?",
                        "A reported advantage is an advantage over something, and which "
                        "something decides what the number means."),
    "ATTRIBUTION": ("Is the reported difference attributable to the component the paper "
                    "credits?",
                    "Several things can differ between two reported conditions, and the "
                    "paper's attribution is one of the readings the evidence permits."),
    "PROTOCOL_CONFORMANCE": ("Does the released artifact implement the procedure the "
                            "paper describes?",
                            "A published repository is a claim about the method, and a "
                            "reader who runs it is relying on the two agreeing."),
    "SPECIFICATION": ("What exactly was measured here, and over what?",
                      "A number whose definition cannot be recovered from the paper "
                      "cannot be checked by anyone."),
    "UNCLASSIFIED": _DEFAULT,
    "MATHEMATICAL_BOUND": ("Does the stated result hold on concrete admissible instances, and "
                           "does each checked step of its printed proof hold?",
                           "A mathematical result the paper's conclusions rest on is only as "
                           "strong as its proof. Explicit instances can refute the statement "
                           "or a proof step; they can never prove it."),
}
_QUESTIONABLE = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "")
_MATERIALITY = {"CONFIRMED_FINDING": "CENTRAL", "PLAUSIBLE_CONCERN": "SUPPORTING",
                "OPEN_QUESTION": "PERIPHERAL", "DISMISSED": "PERIPHERAL", "": "UNASSESSED"}
_MATERIALITY_RANK = {"UNASSESSED": 0, "PERIPHERAL": 1, "SUPPORTING": 2, "CENTRAL": 3}

# MATHEMATICAL_BOUND detection: a theorem/lemma/proposition/corollary/bound/rate NAMED,
# stated together with an inequality or asymptotic operator. Neither half alone is
# enough -- "the bound is O(1/T)" alone is an ordinary complexity remark, and "Theorem
# 3.1 states our main result" alone is just a citation to it.
_BOUND_KEYWORD = re.compile(r"\b(theorem|lemma|proposition|corollary)\b|\bbound(?:ed|s)?\b"
                            r"|\brate\b", re.I)
_BOUND_OPERATOR = re.compile(
    r"[≤≥⩽⩾≲]|(?<![A-Za-z0-9_])[<>](?!=)|\bO\("
    r"|\bat most\b|\bbounded by\b|\bconverges at rate\b|\brate of\b", re.I)
# A finding whose own `evidence_ref` names a table cell ("T2:r3:c4") is about a PRINTED
# NUMBER, never a theorem statement — EXACT_CERTIFICATE is for prose/section claims only.
_TABLE_CELL_REF = re.compile(r"^T\d+:")


def is_mathematical_bound(*texts: str) -> bool:
    """Does this text state a theorem/lemma/bound/rate together with an inequality or
    asymptotic operator? The EXACT_CERTIFICATE trigger. Reads no metric name, no paper
    identity and no number."""
    blob = " ".join(t for t in texts if t)
    return bool(_BOUND_KEYWORD.search(blob) and _BOUND_OPERATOR.search(blob))


def kind_for_finding(f: Finding) -> str:
    """Most-specific-first, matching `_template`'s order: a discrepancy type beats a
    baseline class beats a bare lens name. A SPECIFICATION/PRINTED_QUANTITY finding that
    states a theorem/bound with an inequality, and cites no printed table cell, becomes
    MATHEMATICAL_BOUND instead -- EXACT_CERTIFICATE checks it in exact arithmetic."""
    d = (f.discrepancy_type or "").strip().upper()
    if d in _KIND_BY_DISCREPANCY:
        kind = _KIND_BY_DISCREPANCY[d]
    else:
        b = (f.baseline_class or "").strip().upper()
        if b in _KIND_BY_BASELINE:
            kind = _KIND_BY_BASELINE[b]
        else:
            kind = _KIND_BY_LENS.get((f.lens or "").strip().lower(), "UNCLASSIFIED")
    if (kind in ("SPECIFICATION", "PRINTED_QUANTITY")
            and not _TABLE_CELL_REF.match((f.evidence_ref or "").strip())
            and is_mathematical_bound(f.as_claim(), f.statement, f.evidence_quote, f.target)):
        return "MATHEMATICAL_BOUND"
    return kind


def kind_for_discovery_kind(discovery_kind: str = "") -> str:
    return _KIND_BY_DISCOVERY_KIND.get((discovery_kind or "").strip().upper(), "UNCLASSIFIED")


def _template(f: Finding) -> tuple[str, str]:
    d = (f.discrepancy_type or "").strip().upper()
    if d in _BY_DISCREPANCY:
        return _BY_DISCREPANCY[d]
    b = (f.baseline_class or "").strip().upper()
    if b in _BY_BASELINE:
        return _BY_BASELINE[b]
    return _BY_LENS.get((f.lens or "").strip().lower(), _DEFAULT)


def question_for_object(obj: DiscoveredObject) -> ReviewQuestion:
    """A question for an object no finding raised, so every target has one. States, in the
    harness's own words from the object's DISCOVERY_KIND, what checking it would settle,
    never inventing a concern."""
    kind = obj.question_kind or kind_for_discovery_kind(obj.kind)
    question, why = _TEMPLATE_BY_DISCOVERY_KIND.get(kind, _DEFAULT)
    return ReviewQuestion(
        question_id=f"Q-object-{obj.target_id}", question=question, kind=kind,
        from_finding="", source_finding_ids=[], lens="", why_it_matters=why,
        what_would_settle_it=(
            "an admissible instance that violates the statement (refuting it) or a step of "
            "its printed proof (refuting the proof as printed); instances that satisfy it "
            "never settle it" if kind == "MATHEMATICAL_BOUND" else
            "evidence that the quantity at this address is what the paper's own artifact "
            "produces"),
        materiality=obj.centrality if obj.centrality in _MATERIALITY.values() else "UNASSESSED")


def questions_from_findings(findings: list[Finding], *,
                            minted: dict[str, str] | None = None) -> list[ReviewQuestion]:
    """One question per questionable finding, MERGED by (question text, minted address),
    never by the lens's own `evidence_ref` (often a page number, not an address). Two
    findings about different sentences on one page must not merge; `materiality` takes
    the maximum its sources asserted, never more. `route` stays NONE -- choosing one
    needs the artifact and the host, neither a property of a finding."""
    out: list[ReviewQuestion] = []
    by_key: dict[tuple[str, str], ReviewQuestion] = {}
    addresses = dict(minted or {})
    for f in findings:
        if (f.candidate_class or "") not in _QUESTIONABLE:
            continue
        question, why = _template(f)
        addr = addresses.get(f.finding_id, "")
        key = (question, addr if addr else f"\x00unmerged:{f.finding_id}")
        seen = by_key.get(key)
        if seen is not None:
            seen.source_finding_ids.append(f.finding_id)
            mine = _MATERIALITY.get(f.candidate_class or "", "UNASSESSED")
            if _MATERIALITY_RANK[mine] > _MATERIALITY_RANK[seen.materiality]:
                seen.materiality = mine
            if not seen.what_would_settle_it:
                seen.what_would_settle_it = (f.recommended_resolution or "").strip()
            continue
        settle = (f.recommended_resolution or "").strip()
        if not settle and f.counter_explanations:
            settle = ("Rule out the alternative explanations the review recorded: "
                      + "; ".join(c.strip() for c in f.counter_explanations[:3] if c.strip()))
        q = ReviewQuestion(
            question_id=f"Q-{f.lens or 'lens'}-{f.finding_id or len(out) + 1}",
            question=question, kind=kind_for_finding(f), from_finding=f.finding_id,
            source_finding_ids=[f.finding_id], lens=f.lens, why_it_matters=why,
            what_would_settle_it=settle,
            materiality=_MATERIALITY.get(f.candidate_class or "", "UNASSESSED"))
        by_key[key] = q
        out.append(q)
    return out


# ========================================================================================
# DISCOVERY — what is checkable in this paper, from structure alone.
# ========================================================================================
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")
_KIND_ABBREV = {
    "SCIENTIFIC_CLAIM": "CLM", "EXPERIMENTAL_RESULT": "RES", "BASELINE_COMPARISON": "BAS",
    "ABLATION": "ABL", "CONTROL": "CTL", "DATASET_RESULT": "DAT", "ERROR_ANALYSIS": "ERR",
    "IMPLEMENTATION_CLAIM": "IMP", "REPRODUCTION_TARGET": "REP",
    "UNANSWERED_REVIEW_QUESTION": "QST",
}
_QUESTION_CLASSES = ("OPEN_QUESTION", "DISMISSED")

# WHICH ROUTES A QUESTION OF EACH KIND CAN REACH, cheapest first (an attribution
# question is not settled by re-deriving a number the paper already published).
# INDEPENDENT_RECONSTRUCTION is the no-artifact FALLBACK for the three execution-shaped
# kinds, offered ALONGSIDE AUTHOR_CODE_EXECUTION when the paper's own specification is
# complete, so a target whose repository identity later fails to bind still has a fallback.
ROUTES_FOR_QUESTION = {
    "PRINTED_QUANTITY": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"),
    "COMPOSITION": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"),
    "ATTRIBUTION": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    "CONTROL_PRESENCE": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    "PROTOCOL_CONFORMANCE": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    # DELIBERATELY NOT EXECUTABLE: reproducing a quantity cannot say it was the right
    # quantity to produce. Reading the code can say what was computed; running it cannot.
    "SPECIFICATION": ("ARTIFACT_INSPECTION",),
    "PRIOR_ART": (),      # no route — honestly NO_ROUTE_AVAILABLE
    "UNCLASSIFIED": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"),
    # A theorem/lemma/bound stated with an inequality — EXACT_CERTIFICATE alone. Reading a
    # released checkout cannot evaluate a claimed inequality; the exact-arithmetic
    # certificate is the one route that can.
    "MATHEMATICAL_BOUND": ("EXACT_CERTIFICATE",),
}
_NEEDS_REPO = ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION")
_RECONSTRUCTION_ROUTES = ("INDEPENDENT_RECONSTRUCTION",)
# Only these three kinds are ABOUT a printed quantity at all: with none parsed the object
# reaches no route.
_QUESTION_NEEDS_PRINTED_VALUE = ("PRINTED_QUANTITY", "COMPOSITION", "UNCLASSIFIED")


def _target_id(kind: str, ref: str, n: int, taken: set[str] | None = None) -> str:
    """Kind + address is stable across runs but not unique (two lenses can cite the same
    cell); the numeric suffix is appended only on a real collision."""
    slug = re.sub(r"[^A-Za-z0-9.-]", "", (ref or f"n{n}").replace(":", ""))
    base = f"TGT-{_KIND_ABBREV.get(kind, 'OBJ')}-{slug or n}"
    if taken is None or base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _centrality(*, in_abstract: bool, anchored_by_confirmed: bool, anchored_by_any: bool,
                self_checking: bool, is_reported_result: bool = False) -> str:
    """From STRUCTURE alone, never a model, which could raise the priority of whatever
    target it happened to find first."""
    if in_abstract or anchored_by_confirmed or self_checking:
        return "CENTRAL"
    if anchored_by_any or is_reported_result:
        return "SUPPORTING"
    return "PERIPHERAL"


def _routes(kind: str, ref: ClaimRef | None, *, repo_available: bool, has_value: bool,
           arithmetic_broken: bool, lens: str, question_kind: str = "UNCLASSIFIED",
           specification_complete: bool = False) -> list[str]:
    """Admissible routes, cheapest first -- the escalation policy AS DATA, so `decide.plan`
    stops at the first it can authorize."""
    routes: list[str] = []
    if arithmetic_broken:
        routes.append("ARITHMETIC_RECHECK")
    if lens == "contradiction":
        routes.append("PAPER_INTERNAL_CHECK")

    qk = question_kind if question_kind in ROUTES_FOR_QUESTION else "UNCLASSIFIED"
    if kind == "IMPLEMENTATION_CLAIM":
        routes.append("ARTIFACT_INSPECTION" if repo_available else "NONE")
    elif has_value or qk not in _QUESTION_NEEDS_PRINTED_VALUE:
        for candidate in ROUTES_FOR_QUESTION[qk]:
            if candidate in _RECONSTRUCTION_ROUTES:
                if repo_available and not specification_complete:
                    continue
            elif candidate in _NEEDS_REPO and not repo_available:
                continue
            if decide.needs_printed_value(candidate) and not has_value:
                continue
            routes.append(candidate)
    seen: set[str] = set()
    ordered = [r for r in routes if not (r in seen or seen.add(r))]
    return [r for r in ordered if r != "NONE"] or ["NONE"]


def _object(kind: str, ref: ClaimRef | None, *, claim_text: str, n: int,
           repo_available: bool, lens: str = "", metric: str = "", experiment: str = "",
           in_abstract: bool = False, anchored_by_confirmed: bool = False,
           anchored_by_any: bool = False, is_reported_result: bool = False,
           proposed_by_lens: bool = False, question_id: str = "", question_kind: str = "",
           specification_complete: bool = False,
           counter_explanations: tuple[str, ...] = (), material_abstract_idx: int = -1,
           materiality_doc: PaperDoc | None = None,
           taken: set[str] | None = None) -> DiscoveredObject:
    quantity = ref.quantity if (ref and ref.quantity) else None
    # A lone prose number is a comparison target only when the sentence reports a measured
    # outcome; "sequences of length 1", "5 seeds", "published in 2020" are not results.
    # A table cell and a printed composition carry their own structure and are exempt.
    if (quantity is not None and ref is not None and ref.kind == "prose_claim"
            and not quantity.expression and not locate.measurement_context(ref.quote)):
        quantity = None
    has_value = quantity is not None and quantity.value is not None
    arithmetic_broken = bool(quantity and quantity.arithmetic_ok is False)
    self_checking = bool(quantity and quantity.expression)
    question_kind = question_kind or kind_for_discovery_kind(kind)
    routes = _routes(kind, ref, repo_available=repo_available, has_value=has_value,
                     arithmetic_broken=arithmetic_broken, lens=lens,
                     question_kind=question_kind, specification_complete=specification_complete)
    resolved = bool(ref and ref.resolved)

    # THREE causes, not one collapsed boolean: address unresolved, quantity unparsed, or
    # no route applies -- distinct limits, reported distinctly.
    requirements: list[str] = []
    blocker = "NONE"
    if not resolved:
        requirements.append("an address in the parsed paper that the harness can re-derive")
        blocker = "ADDRESS_UNRESOLVED"
    needs_value = kind in ("EXPERIMENTAL_RESULT", "DATASET_RESULT", "REPRODUCTION_TARGET")
    if needs_value and not has_value:
        requirements.append("a single unambiguous printed quantity to compare against")
        blocker = blocker if blocker != "NONE" else "QUANTITY_UNPARSED"
    if routes == ["NONE"]:
        requirements.append("any legitimate route to evidence; none exists for this object")
        blocker = blocker if blocker != "NONE" else "NO_ROUTE"

    addressable = resolved and not requirements
    note = ("structurally checkable" if addressable
            else "scientifically relevant, but " + "; ".join(requirements))

    return DiscoveredObject(
        target_id=_target_id(kind, ref.ref if ref else "", n, taken),
        kind=kind, ref=ref, claim_text=claim_text.strip()[:600], metric=metric,
        expected_value=quantity.value if quantity else None,
        expected_raw=quantity.raw if quantity else "", experiment=experiment,
        materiality_basis=decide.basis_for_ref(ref, material_abstract_idx, doc=materiality_doc),
        centrality=_centrality(in_abstract=in_abstract, anchored_by_confirmed=anchored_by_confirmed,
                               anchored_by_any=anchored_by_any, self_checking=self_checking,
                               is_reported_result=is_reported_result),
        evidence_requirements=requirements, routes=routes, proposed_by_lens=proposed_by_lens,
        harness_addressable=addressable, note=note, addressing_blocker=blocker,
        counter_explanations=list(counter_explanations), question_id=question_id,
        question_kind=question_kind)


def _add(objects: list, taken: set, obj: DiscoveredObject) -> DiscoveredObject:
    taken.add(obj.target_id)
    objects.append(obj)
    return obj


# FORMAL STATEMENTS — the paper's own numbered results, found from structure alone. A
# statement HEADER is the label, its number, an optional short parenthetical, then a full
# stop and a capitalised sentence, at the START of a sentence: "… eigenvalues. Theorem
# 3.1. For any u" is one; "given in Theorem 3.1. Then" and "Theorem 3.1 yields" are
# citations. Definitions state nothing checkable and are excluded.
_FORMAL_HEAD = re.compile(
    r"\b(Theorem|Proposition|Lemma|Corollary|Result)\s+([A-Z]?\d+(?:\.\d+)*)"
    r"\s*(?:\([^()]{0,120}\))?\s*\.\s+(?=[A-Z(\u2200-\u22ff])")
_FORMAL_END = re.compile(
    r"\b(?:Proof\b|Remark\s+\d|Definition\s+\d|Example\s+\d|Theorem\s+\d|Lemma\s+\d"
    r"|Proposition\s+\d|Corollary\s+\d|Assumption\s+\d)")
_SUMMARY_TITLE = re.compile(r"abstract|introduction|contribution|conclusion|summary|overview",
                            re.I)
_FORMAL_MAX_CHARS = 1200


def _statement_head(text: str, start: int) -> bool:
    before = text[:start].rstrip()
    return not before or before[-1] in ".:;)]\n" or before.endswith("\u25a1")


def formal_statements(doc: PaperDoc) -> list[dict]:
    """[{label, section_idx, text, context}] — the FIRST occurrence of every numbered
    theorem/proposition/lemma/corollary/result the paper states, in paper order. A later
    restatement (an appendix repeating the statement before its proof) is not a second
    statement."""
    out, seen = [], set()
    for sec in doc.sections:
        title = (sec.title or "").strip().lower()
        if title.startswith(_NOT_THE_PAPER):
            continue
        text = sec.text or ""
        for m in _FORMAL_HEAD.finditer(text):
            label = f"{m.group(1)} {m.group(2)}"
            if label in seen or not _statement_head(text, m.start()):
                continue
            body_start = m.end()
            nxt = _FORMAL_END.search(text, body_start)
            end = min(nxt.start() if nxt else len(text), body_start + _FORMAL_MAX_CHARS)
            body = " ".join(text[m.start():end].split())
            if len(body) - len(label) < 40:
                continue
            seen.add(label)
            out.append({"label": label, "section_idx": sec.section_idx, "text": body,
                        "context": " ".join(text[max(0, m.start() - 80):m.start()].split())})
    return out


_NUMBERED_TITLE = re.compile(r"^\s*(\d+|[A-Z](?=\.))((?:\.\d+)*)\.?\s+[A-Za-z]")


def _summary_sections(doc: PaperDoc) -> list:
    """The abstract, introduction, contributions and conclusion — plus the sections a PDF
    parse split them into, whose titles are running headers or sentence fragments rather
    than a new numbered heading."""
    out, inside = [], False
    abstract = decide.abstract_section_idx(doc)
    for sec in doc.sections:
        title = sec.title or ""
        if _SUMMARY_TITLE.search(title) or sec.section_idx == abstract:
            inside = True
        elif _NUMBERED_TITLE.match(title) or title.strip().lower().startswith(_NOT_THE_PAPER):
            inside = False
        if inside:
            out.append(sec)
    return out


def _section_numbers(doc: PaperDoc, section_idx: int) -> list[str]:
    """The numbered heading a section sits under ("3.1") and its parents ("3"), found by
    walking back past unnumbered fragments."""
    for sec in reversed([s for s in doc.sections if s.section_idx <= section_idx]):
        m = _NUMBERED_TITLE.match(sec.title or "")
        if m:
            parts = (m.group(1) + m.group(2)).split(".")
            return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]
    return []


def _own_row(doc: PaperDoc, method: str) -> bool:
    """Does a table row report the paper's OWN method? The one rule execution binding uses
    (`experiment_id.own_method`): a cited baseline's row is supporting evidence for the
    comparison, never the paper's central claim, even when the abstract names it."""
    return own_method(doc, method)


def _cited_in_summary(doc: PaperDoc, label: str, own_section: int, own_text: str) -> bool:
    """Is this statement cited by the paper's own summary — by its label, or by the
    numbered section it sits in ("Section 3.1") — outside the statement itself? Structure,
    not a model: the paper's own summary says which results it rests on."""
    pats = [re.compile(rf"\b{re.escape(label)}(?![\d.]*\d)")] + [
        re.compile(rf"\bSec(?:tion|\.)?s?\s+{re.escape(n)}(?![\d.]*\d)")
        for n in _section_numbers(doc, own_section)]
    for sec in _summary_sections(doc):
        text = " ".join((sec.text or "").split())
        if sec.section_idx == own_section:
            text = text.replace(own_text, " ")
        if any(p.search(text) for p in pats):
            return True
    return False


_FORMAL_RANK = {"Theorem": 0, "Result": 0, "Proposition": 1, "Corollary": 2, "Lemma": 3}


def _mint_statement(doc: PaperDoc, st: dict) -> ClaimRef:
    """An address for a statement, preferring its own words; a verbatim restatement
    elsewhere makes those ambiguous, so the words just before it disambiguate."""
    ref = locate.mint(doc, st["text"][:300])
    if not ref.resolved and st["context"]:
        ref = locate.mint(doc, (st["context"] + " " + st["text"])[:400])
    return ref


ABSTRACT_QUESTION = "Q-abstract-claims"     # the same id `harness.routes` plans under


def _mint_step(doc: PaperDoc, quote: str, first_section: int) -> ClaimRef:
    """A proof step's address: inside the proof's own sections first (its words may repeat
    a statement given elsewhere), then anywhere it is unique."""
    if first_section >= 0:
        # ponytail: a proof is searched over at most 8 consecutive sections from its start.
        for idx in range(first_section, first_section + 8):
            ref = locate.mint_in(doc, quote, idx)
            if ref.resolved:
                return ref
    return locate.mint(doc, quote)


def prose_compositions(doc: PaperDoc) -> list[ClaimRef]:
    """Every prose span stating a composition the harness can re-evaluate, e.g.
    '58 x 5 x 10 = 2,900': the arithmetic is checkable without running anything."""
    out: list[ClaimRef] = []
    seen: set[str] = set()
    for section in doc.sections:
        for sentence in _SENTENCE.split(section.text or ""):
            q = locate.parse_quantity(sentence)
            if not (q and q.expression and q.value is not None):
                continue
            ref = locate.mint(doc, sentence.strip())
            if ref.resolved and ref.ref not in seen:
                seen.add(ref.ref)
                out.append(ref)
    return out


def discovered_objects(doc: PaperDoc, findings: list[Finding],
                       questions: list[ReviewQuestion] | None = None, *,
                       repo_available: bool | None = None,
                       specification_complete: bool = False,
                       max_formal: int = 10, proof_maps: dict | None = None,
                       max_step_targets: int = 12,
                       check_plans: list[dict] | None = None) -> tuple[list[DiscoveredObject], dict]:
    """(objects, extraction_coverage) for one paper. Deterministic, consults no model,
    runs before any execution. `proof_maps` and `check_plans` are SEALED, harness-validated
    proposals (see `harness.certificate.load_proof_maps`, `harness.routes.load_check_plans`):
    every address in them has already been re-found in the paper, and is re-found again
    here before it becomes a target."""
    questions = questions or []
    if repo_available is None:
        repo_available = bool((doc.repo_url or "").strip())
    by_finding: dict[str, ReviewQuestion] = {}
    for q in questions:
        for fid in (q.source_finding_ids or ([q.from_finding] if q.from_finding else [])):
            if fid:
                by_finding.setdefault(fid, q)
    abstract_idx = material_abstract_idx = decide.abstract_section_idx(doc)

    objects: list[DiscoveredObject] = []
    claimed_refs: set[str] = set()
    taken: set[str] = set()

    attacked: dict[str, bool] = {}
    proposed: set[str] = set()
    for f in findings:
        r = locate.address(doc, f.evidence_ref, f.evidence_quote)
        if r.resolved:
            confirmed = (f.candidate_class or "") == "CONFIRMED_FINDING"
            attacked[r.ref] = attacked.get(r.ref, False) or confirmed
            if f.verifiable_by_experiment:
                proposed.add(r.ref)

    # (1) printed table results the extractor already addressed and parsed a number for.
    # A RESULT table the paper's own summary cites ("Table 2 shows ...") carries a central
    # claim — the same structural rule numbered results use; a data-statistics table's
    # cells describe the data, never the method, and are peripheral.
    summary_cited: dict[int, bool] = {}
    for num in doc.reported_numbers:
        ref_s = (num.table_ref or "").strip()
        if not ref_s or ref_s in claimed_refs:
            continue
        ref = locate.resolve(doc, ref_s)
        if not ref.resolved:
            continue
        claimed_refs.add(ref_s)
        data_stat = (num.benchmark or "").startswith("[data statistic]")
        t_idx = int(re.match(r"T(\d+)", ref_s).group(1)) if re.match(r"T(\d+)", ref_s) else -1
        if t_idx not in summary_cited:
            table = next((t for t in doc.tables if t.table_idx == t_idx), None)
            summary_cited[t_idx] = bool(table is not None and table.label and _cited_in_summary(
                doc, f"Table {table.label}", -1, ""))
        _add(objects, taken, _object(
            "DATASET_RESULT" if num.benchmark else "EXPERIMENTAL_RESULT", ref,
            claim_text=num.source_quote or ref.quote, n=len(objects),
            repo_available=repo_available, metric=num.metric,
            experiment=num.benchmark or num.method, is_reported_result=not data_stat, taken=taken,
            in_abstract=summary_cited[t_idx] and not data_stat and _own_row(doc, num.method),
            anchored_by_any=ref.ref in attacked, anchored_by_confirmed=attacked.get(ref.ref, False),
            proposed_by_lens=ref.ref in proposed, material_abstract_idx=material_abstract_idx,
            materiality_doc=doc, specification_complete=specification_complete))

    # (2) prose compositions — self-checking
    for ref in prose_compositions(doc):
        if ref.ref in claimed_refs:
            continue
        claimed_refs.add(ref.ref)
        _add(objects, taken, _object(
            "REPRODUCTION_TARGET", ref, claim_text=ref.quote, n=len(objects),
            repo_available=repo_available, taken=taken, in_abstract=ref.section_idx == abstract_idx,
            material_abstract_idx=material_abstract_idx, materiality_doc=doc,
            specification_complete=specification_complete))

    # (3) whatever the lenses argued about, at the address the harness can re-derive
    for f in findings:
        ref = locate.address(doc, f.evidence_ref, f.evidence_quote)
        if ref.resolved and ref.ref in claimed_refs and (f.candidate_class or "") == "":
            continue
        confirmed = (f.candidate_class or "") == "CONFIRMED_FINDING"
        if (f.baseline_class or "") in ("IMPORTANT_MISSING_BASELINE", "CENTRAL_VALIDITY_THREAT"):
            kind = "BASELINE_COMPARISON"
        elif (f.candidate_class or "") in _QUESTION_CLASSES:
            kind = "UNANSWERED_REVIEW_QUESTION"
        else:
            kind = "SCIENTIFIC_CLAIM"
        q = by_finding.get(f.finding_id)
        _add(objects, taken, _object(
            kind, ref if ref.resolved else None, claim_text=f.as_claim() or f.statement,
            n=len(objects), repo_available=repo_available, lens=f.lens,
            in_abstract=ref.section_idx == abstract_idx, anchored_by_confirmed=confirmed,
            anchored_by_any=True, proposed_by_lens=bool(f.verifiable_by_experiment), taken=taken,
            material_abstract_idx=material_abstract_idx, materiality_doc=doc,
            specification_complete=specification_complete,
            counter_explanations=tuple(f.counter_explanations),
            question_id=q.question_id if q else "",
            question_kind=q.kind if q else kind_for_finding(f)))
        if ref.resolved:
            claimed_refs.add(ref.ref)

    # (4) the paper's own numbered results — positive verification targets whether or not a
    # lens questioned them, so a theory paper is never reduced to its reviewers' concerns.
    # ponytail: at most `max_formal` (SH_MAX_FORMAL_TARGETS) — summary-cited first, then
    # theorem > proposition > corollary > lemma, then paper order; each costs two Sonnet
    # tasks. The rest are counted in coverage (`formal_statements_found`), never hidden.
    statements = formal_statements(doc)
    ranked = sorted(statements, key=lambda st: (
        not _cited_in_summary(doc, st["label"], st["section_idx"], st["text"]),
        _FORMAL_RANK.get(st["label"].split()[0], 4), statements.index(st)))
    targeted = 0
    parents: list[tuple[DiscoveredObject, dict]] = []
    for st in ranked:
        if targeted >= max(0, max_formal):
            break
        ref = _mint_statement(doc, st)
        if not ref.resolved or ref.ref in claimed_refs:
            continue
        claimed_refs.add(ref.ref)
        central = _cited_in_summary(doc, st["label"], st["section_idx"], st["text"])
        parents.append((_add(objects, taken, _object(
            "SCIENTIFIC_CLAIM", ref, claim_text=st["text"], n=len(objects),
            repo_available=repo_available, taken=taken, question_kind="MATHEMATICAL_BOUND",
            in_abstract=central, anchored_by_any=True, material_abstract_idx=material_abstract_idx,
            materiality_doc=doc, specification_complete=specification_complete)), st))
        targeted += 1

    # (4b) each step of a mapped proof is its own target: the audit covers every explicit
    # step the proof asserts, not the one a model happened to choose. The child inherits the
    # parent's centrality and carries the step's words, which the certificate must check.
    from . import certificate
    step_targets, steps_unaddressable = 0, 0
    for parent, st in parents:
        pm = (proof_maps or {}).get(parent.target_id) or {}
        first = certificate.proof_location(doc, st["label"])[0]
        for step in pm.get("steps") or []:
            if step_targets >= max(0, max_step_targets):
                break
            ref = _mint_step(doc, step.get("quote") or "", first)
            if not ref.resolved or ref.ref in claimed_refs:
                steps_unaddressable += 0 if ref.resolved else 1
                continue
            claimed_refs.add(ref.ref)
            child = _add(objects, taken, _object(
                "SCIENTIFIC_CLAIM", ref, claim_text=f"{st['label']} — proof step: {step['quote']}",
                n=len(objects), repo_available=repo_available, taken=taken,
                question_kind="MATHEMATICAL_BOUND", in_abstract=parent.centrality == "CENTRAL",
                anchored_by_any=True, material_abstract_idx=material_abstract_idx,
                materiality_doc=doc, specification_complete=specification_complete))
            child.parent_target, child.prebound_quote = parent.target_id, step["quote"]
            step_targets += 1

    # (4c) a check planner's proposals for a central claim that reached no route: each is
    # re-resolved here, linked to that claim's question for PRIORITY only, and routed like
    # any other printed quantity. Its materiality stays whatever the structure says.
    planned = 0
    by_ref = {o.ref.ref: o for o in objects if o.ref is not None and o.ref.resolved}
    for plan in check_plans or []:
        for item in plan.get("refs") or []:
            ref = locate.resolve(doc, str(item.get("ref") or ""))
            if not ref.resolved:
                continue
            obj = by_ref.get(ref.ref)
            if obj is None:
                obj = _add(objects, taken, _object(
                    "EXPERIMENTAL_RESULT" if ref.kind == "table_cell" else "SCIENTIFIC_CLAIM",
                    ref, claim_text=ref.quote, n=len(objects), repo_available=repo_available,
                    taken=taken, question_kind="PRINTED_QUANTITY", is_reported_result=True,
                    material_abstract_idx=material_abstract_idx, materiality_doc=doc,
                    specification_complete=specification_complete))
                by_ref[ref.ref] = obj
            # Only a target no question owns yet: a lens's own question, or an earlier plan's
            # link, is never overwritten or counted twice.
            if not obj.question_id and not obj.planned_for:
                obj.question_id = obj.planned_for = str(plan.get("question_id") or "")
                planned += 1

    # (5) the artifact itself is a claim the paper makes
    if repo_available:
        _add(objects, taken, _object(
            "IMPLEMENTATION_CLAIM", None, n=len(objects), repo_available=True, taken=taken,
            claim_text=f"The released repository {doc.repo_url} implements the described method."))
        objects[-1].harness_addressable = True
        objects[-1].evidence_requirements = []
        objects[-1].note = "checkable by static inspection of the checkout"

    coverage = {
        "tables_extracted": len(doc.tables),
        "table_cells": sum(len(r) for t in doc.tables for r in t.rows),
        "reported_numbers": len(doc.reported_numbers),
        "prose_compositions": sum(1 for o in objects if o.kind == "REPRODUCTION_TARGET"),
        "sections": len(doc.sections), "equations_extracted": len(doc.equations),
        "objects_discovered": len(objects),
        "formal_statements_found": len(statements),
        "formal_statements_targeted": targeted,
        "proof_steps_mapped": sum(len((proof_maps or {}).get(p.target_id, {}).get("steps") or [])
                                  for p, _st in parents),
        "proof_step_targets": step_targets,
        "proof_steps_unaddressable": steps_unaddressable,
        "check_plan_targets": planned,
        "objects_addressable": sum(1 for o in objects if o.harness_addressable),
        "findings_with_resolved_ref": sum(
            1 for f in findings if locate.address(doc, f.evidence_ref, f.evidence_quote).resolved),
        "findings_total": len(findings),
        "lens_proposed_verifiable": sum(1 for f in findings if f.verifiable_by_experiment),
    }
    return objects, coverage


# ========================================================================================
# PIPELINE ORCHESTRATION — builds the whole target set: questions, objects, priority
# order, plans, and outcomes for anything settled without execution.
# ========================================================================================
_DISPOSITION_FOR_ACTION = {
    "PAPER_ONLY_RESOLUTION": "CITATION_VERIFIED_ONLY",
    "INFEASIBLE_SPECIFICATION": "SPECIFICATION_BLOCKED",
    "INFEASIBLE_ADDRESSING": "ADDRESSING_BLOCKED",
    "INFEASIBLE_REPORTING": "REPORTING_BLOCKED",
    "INFEASIBLE_ROUTE": "NO_ROUTE_AVAILABLE",
    "DEFERRED_TO_ANOTHER_TARGET": "NO_EXPERIMENT_NEEDED",
    "OUTRANKED_BY_CENTRAL_TARGET": "NO_EXPERIMENT_NEEDED",
    "SUPERSEDED_BY_ESTABLISHED_FAILURE": "SUPERSEDED_BY_ESTABLISHED_FAILURE",
    "INFEASIBLE_ARTIFACT": "ARTIFACT_BLOCKED",
    "INFEASIBLE_ENVIRONMENT": "ENVIRONMENT_BLOCKED",
    "NO_EXPERIMENT_NEEDED": "NOT_ATTEMPTED",
}
_SETTLING_DISPOSITIONS = ("PAPER_ONLY_RESOLVED", "PAPER_ARITHMETIC_CONTRADICTION",
                          "REPRODUCED", "FAILED_REPRODUCTION")


def paper_only_outcome(obj: DiscoveredObject, plan: PlanDecision) -> TargetOutcome:
    """Pursue a target against the paper's own printed content -- nothing runs. Two
    routes reach here and establish DIFFERENT things: ARITHMETIC_RECHECK re-evaluates a
    printed composition operand by operand (either disposition is a real resolution);
    PAPER_INTERNAL_CHECK re-verifies a concern's quotation, which settles nothing about
    whether the concern is correct."""
    q = obj.ref.quantity if (obj.ref and obj.ref.quantity) else None
    if plan.route == "ARITHMETIC_RECHECK" and q and q.expression:
        agrees = q.arithmetic_ok
        disposition = "PAPER_ONLY_RESOLVED" if agrees else "PAPER_ARITHMETIC_CONTRADICTION"
        return TargetOutcome(
            target_id=obj.target_id, disposition=disposition, action=plan.action,
            route=plan.route, provenance="paper",
            reason=(f"the paper prints {q.expression} = {q.raw}; re-evaluated here that is "
                    f"{'consistent' if agrees else 'NOT consistent'} with the printed total. "
                    f"No execution was required to establish this."))
    return TargetOutcome(
        target_id=obj.target_id, disposition="CITATION_VERIFIED_ONLY", action=plan.action,
        route=plan.route, provenance="paper",
        reason=("the quotation behind this concern was re-verified against the parsed "
                "paper, so the concern cites the paper accurately. That is all this route "
                "establishes: whether the concern is correct was not investigated here, "
                "and this target settles nothing about the paper."))


def _one_per_experiment(objects: list[DiscoveredObject],
                        plans: list[PlanDecision]) -> list[PlanDecision]:
    """Targets answered by the SAME run are one experiment, not many -- grouped by
    (route, experiment, metric), what a run is actually determined by. A target with no
    declared experiment AND metric groups with nothing."""
    seen: dict[tuple[str, str, str], str] = {}
    out: list[PlanDecision] = []
    for obj, plan in zip(objects, plans):
        if not plan.requires_execution:
            out.append(plan)
            continue
        if not (obj.experiment and obj.metric):
            out.append(plan)
            continue
        key = (plan.route, obj.experiment, obj.metric)
        first = seen.get(key)
        if first is None:
            seen[key] = obj.target_id
            out.append(plan)
            continue
        out.append(PlanDecision(
            target_id=plan.target_id, action="NO_EXPERIMENT_NEEDED", route=plan.route,
            reason=(f"the same run answers this and {first}, which is being pursued: same "
                    f"route, same experiment, same metric. Reported alongside it rather "
                    f"than re-run."),
            gates=dict(plan.gates, answered_by_another_target=True),
            blocking_gate="answered_by_another_target", requires_execution=False))
    return out


def _demote_when_a_central_target_is_being_pursued(
        objects: list[DiscoveredObject], plans: list[PlanDecision]) -> list[PlanDecision]:
    """A SUPPORTING target does not earn an execution while a CENTRAL one is available --
    a SET-level fact `decide.plan` cannot see since it decides one target at a time."""
    # Within a route FAMILY only: a central theorem being certified says nothing about
    # whether a supporting printed result needs its run, and vice versa.
    central_families = {decide.route_family(p.route) for o, p in zip(objects, plans)
                        if p.requires_execution and o.centrality == "CENTRAL"}
    if not central_families:
        return plans
    out: list[PlanDecision] = []
    for obj, plan in zip(objects, plans):
        if (plan.requires_execution and obj.centrality != "CENTRAL" and not obj.planned_for
                and decide.route_family(plan.route) in central_families):
            out.append(PlanDecision(
                target_id=plan.target_id, action="NO_EXPERIMENT_NEEDED", route=plan.route,
                reason=(f"a {obj.centrality.lower()} result, and at least one CENTRAL target "
                        f"is being pursued. Reported as an open question rather than run."),
                gates=dict(plan.gates, outranked_by_a_central_target=True),
                blocking_gate="outranked_by_a_central_target", requires_execution=False))
        else:
            out.append(plan)
    return out


def _undefer_when_the_central_target_did_not_settle(
        objects: list[DiscoveredObject], plans: list[PlanDecision],
        prior_outcomes: dict[str, TargetOutcome] | None) -> list[PlanDecision]:
    """Give a deferred SUPPORTING target its own turn once every CENTRAL sibling's prior
    attempt is known and none settled -- otherwise a resource-ordering heuristic becomes
    a standing refusal with no scientific content."""
    if not prior_outcomes:
        return plans
    by_question: dict[str, list[DiscoveredObject]] = {}
    for obj in objects:
        if obj.question_id:
            by_question.setdefault(obj.question_id, []).append(obj)
    out: list[PlanDecision] = []
    for obj, plan in zip(objects, plans):
        if plan.blocking_gate != "outranked_by_a_central_target":
            out.append(plan)
            continue
        centrals = [o for o in by_question.get(obj.question_id, [])
                   if o.centrality == "CENTRAL" and o.target_id != obj.target_id]
        central_outcomes = [prior_outcomes[o.target_id] for o in centrals
                            if o.target_id in prior_outcomes]
        all_known = bool(centrals) and len(central_outcomes) == len(centrals)
        any_settled = any(o.disposition in _SETTLING_DISPOSITIONS for o in central_outcomes)
        if all_known and not any_settled:
            dispositions = ", ".join(sorted({o.disposition for o in central_outcomes}))
            out.append(plan.model_copy(update=dict(
                reason=("a supporting result, previously deferred while a central target "
                        "for this question was pursued; that central target's own prior "
                        f"attempt reached a terminal, non-settling disposition "
                        f"({dispositions}), so this target is no longer outranked and "
                        "gets its own attempt."),
                gates=dict(plan.gates, outranked_by_a_central_target=False,
                          central_target_did_not_settle=True),
                blocking_gate="", requires_execution=True)))
        else:
            out.append(plan)
    return out


def _question_for_every_executable_target(
        objects: list[DiscoveredObject], plans: list[PlanDecision], qs: list) -> list:
    """Mint a ReviewQuestion for every target that will EXECUTE and has none: printed
    table results and prose compositions come from the extractor, not a lens, so a
    launch could not otherwise name what it was answering."""
    out = list(qs)
    for obj, plan in zip(objects, plans):
        if not plan.requires_execution or obj.question_id:
            continue
        q = question_for_object(obj)
        obj.question_id = q.question_id
        out.append(q)
    return out


_CONCLUSION = {
    # Provenance-neutral on purpose: an admissible run is either the authors' code at a
    # verified commit or a verified independent reimplementation (reimpl_exec); the
    # target's own reason says which.
    "REPRODUCTION_SUCCESS": "an admissible run (the authors' code at a verified commit, or a "
                            "verified independent reimplementation of the stated method) "
                            "re-derived the paper's stated value; see the target for which.",
    "REPRODUCTION_FAILURE": "an admissible run (the authors' code at a verified commit, or a "
                            "verified independent reimplementation of the stated method) did "
                            "not produce the value the paper states; see the target for which.",
    "PAPER_INTERNAL_EVIDENCE": "settled against the paper's own printed content, with "
                               "nothing executed.",
    "ARTIFACT_EVIDENCE": "settled by reading the released code.",
    "ARTIFACT_LIMITATION": "no usable artifact reached this question. Nothing about the "
                           "paper follows from that.",
    "ENVIRONMENT_LIMITATION": "the experiment was not run: this host, a harness gate or an "
                              "unproven precondition refused it. A fact about the run, not "
                              "about the paper.",
    "SPECIFICATION_LIMITATION": "the paper does not specify enough to build the experiment "
                                "that would answer this. Reported unresolved rather than "
                                "answered with an invented one.",
    "EXTRACTION_LIMITATION": "this review could not build a re-derivable address for the "
                             "claim, so there was nothing an execution could be reconciled "
                             "against. A limit of what extraction recovered.",
    "REPORTING_LIMITATION": "the claim is addressed and the paper prints no single "
                            "unambiguous quantity there to compare a reproduction against.",
    "NO_ROUTE_AVAILABLE": "the claim is addressed and quantified, and no verification "
                          "route this system has would settle the question.",
    "COMPARISON_LIMITATION": "a route applies to this question and this review could not "
                             "carry out the comparison at the end of it, so nothing was run.",
    "CITATION_VERIFIED": "the concern's quotation was re-verified against the paper, so it "
                         "cites the paper accurately; whether the concern is correct was "
                         "not investigated.",
    "INCONCLUSIVE_EXECUTION": "something ran and settled nothing admissible; the question "
                              "stands open for a human reviewer.",
    "CERTIFICATE_VIOLATION": "an independently verified exact-arithmetic certificate "
                             "constructed an instance that satisfies every hypothesis the "
                             "paper's theorem states and violates the bound it claims.",
    "CERTIFICATE_NO_VIOLATION": "an independently verified exact-arithmetic certificate "
                                "found no violation among the tested instances. This "
                                "checks those instances only and is never a proof that the "
                                "bound holds in general.",
    "NOT_INVESTIGATED": "",
}


def _conclusion(out: TargetOutcome) -> str:
    if out.disposition == "CITATION_VERIFIED_ONLY":
        return ("the concern's quotation was re-verified against the paper, so it cites "
                "the paper accurately; whether the concern is correct was not "
                "investigated and this route could not investigate it.")
    return _CONCLUSION.get(out.evidence_state, "")


def sync_questions(ts: TargetSet) -> TargetSet:
    """Re-derive every ReviewQuestion's harness-written half from the target set -- a
    FOLD, not an accumulation, so a question never keeps a resolution the evidence behind
    it has since lost. A merged question reports its HIGHEST-PRIORITY target's state
    (conservative: stays open if its central target is blocked even when a supporting one
    settled); `evidence_refs` still lists every contributing target."""
    by_question: dict[str, DiscoveredObject] = {}
    all_targets: dict[str, list[str]] = {}
    for obj in ts.objects:
        if not obj.question_id:
            continue
        by_question.setdefault(obj.question_id, obj)
        all_targets.setdefault(obj.question_id, []).append(obj.target_id)
    plans = {p.target_id: p for p in ts.plans}
    outcomes = {o.target_id: o for o in ts.outcomes}

    for q in ts.questions:
        obj = by_question.get(q.question_id)
        if obj is None:
            q.possible_resolution_routes = []
            q.route = "NONE"
            q.resolution_status, q.evidence_state = "NOT_INVESTIGATED", "NOT_INVESTIGATED"
            q.evidence_refs = []
            continue
        q.claim_ref = obj.ref
        q.possible_resolution_routes = [r for r in obj.routes if r != "NONE"]
        q.route = obj.routes[0] if obj.routes else "NONE"

        out = outcomes.get(obj.target_id)
        if out is None:
            plan = plans.get(obj.target_id)
            q.resolution_status = "NOT_INVESTIGATED"
            q.evidence_state = "NOT_INVESTIGATED"
            q.evidence_refs = list(all_targets.get(q.question_id, []))
            q.resolution = ""
            q.conclusion = ("an experiment is warranted for this question and has not "
                            "concluded in this run."
                            if plan is not None and plan.requires_execution else "")
            continue

        q.evidence_state = out.evidence_state
        q.resolution_status = out.resolution_state
        q.resolved_without_execution = out.disposition == "PAPER_ONLY_RESOLVED"
        q.evidence_refs = [r for r in all_targets.get(q.question_id, []) + [out.execution_ref] if r]
        q.resolution = out.reason if q.resolution_status.startswith("RESOLVED") else ""
        q.conclusion = _conclusion(out)
    return ts


def targets_path_in(root):
    """Where the target set lives inside a project directory."""
    return root / "discovery" / "targets.json"


def targets_path(cfg, pid: str):
    from . import state
    return targets_path_in(state.project_dir(cfg, pid))


def load(cfg, pid: str) -> TargetSet | None:
    from . import state
    p = targets_path(cfg, pid)
    if not p.exists():
        return None
    try:
        return TargetSet(**state.read_json(p))
    except (OSError, ValueError):
        return None


def run(cfg, pid: str, *, investigation_open: bool = True) -> dict:
    """The DISCOVER-phase pipeline entry point: read the ingested paper and the composed
    lens findings, build the whole target set, and persist it."""
    from . import audit as audit_stage
    from . import state

    root = state.project_dir(cfg, pid)
    doc_path = root / "paper" / "doc.json"
    if not doc_path.exists():
        return {"error": f"no ingested paper for '{pid}' — run ingest_paper first"}
    doc = PaperDoc(**state.read_json(doc_path))
    state.set_phase(cfg, pid, "discover")

    reports, _dropped, _invalid = audit_stage.load_reports(cfg, pid, doc)
    findings = [f for r in reports for f in r.findings]

    prior = load(cfg, pid)
    prior_outcomes = {o.target_id: o for o in prior.outcomes} if prior else None
    from . import certificate, routes
    ts = build(pid, doc, findings, investigation_open=investigation_open,
              prior_outcomes=prior_outcomes, max_formal=cfg.max_formal_targets,
              proof_maps=certificate.load_proof_maps(cfg, pid),
              max_step_targets=cfg.max_proof_step_targets,
              check_plans=routes.load_check_plans(cfg, pid))
    path = targets_path(cfg, pid)
    state.write_json(path, ts.model_dump())

    executable = [p for p in ts.plans if p.requires_execution]
    state.append_log(
        cfg, pid, artifact_type="target_set", phase="discover",
        headers={"objects": len(ts.objects), "questions": len(ts.questions),
                "addressable": sum(1 for o in ts.objects if o.harness_addressable),
                "requires_execution": len(executable),
                "resolved_without_execution":
                    ts.extraction_coverage.get("targets_resolved_without_execution", 0)},
        path=str(path))

    return {"paper_id": pid, "objects": len(ts.objects), "questions": len(ts.questions),
           "addressable": sum(1 for o in ts.objects if o.harness_addressable),
           "requires_execution": len(executable),
           "resolved_without_execution":
               ts.extraction_coverage.get("targets_resolved_without_execution", 0),
           "blocked_before_execution":
               ts.extraction_coverage.get("targets_blocked_before_execution", 0),
           "top_target": ts.objects[0].target_id if ts.objects else None,
           "targets": "discovery/targets.json"}


def build(pid: str, doc: PaperDoc, findings: list[Finding], *,
         investigation_open: bool = True,
         prior_outcomes: dict[str, TargetOutcome] | None = None,
         max_formal: int = 10, proof_maps: dict | None = None, max_step_targets: int = 12,
         check_plans: list[dict] | None = None) -> TargetSet:
    """The whole target set for one paper. Pure with respect to what is on disk -- takes
    `findings` directly rather than loading them. `investigation_open=False` means a
    material failure is already established, so `decide.plan` refuses every executable
    route with SUPERSEDED_BY_ESTABLISHED_FAILURE, but questions, addresses, routes,
    centrality and coverage still compute in full. `prior_outcomes` is this paper's own
    previous discover pass, so `_undefer_...` can tell "nobody tried the central target
    yet" apart from "it was tried and settled nothing"."""
    minted: dict[str, str] = {}
    for f in findings:
        if not f.finding_id:
            continue
        ref = locate.address(doc, f.evidence_ref or "", f.evidence_quote or "")
        if ref is not None and ref.resolved and ref.ref:
            minted[f.finding_id] = ref.ref
    qs = questions_from_findings(findings, minted=minted)
    if any(p.get("question_id") == ABSTRACT_QUESTION and p.get("refs") for p in check_plans or []):
        # The abstract IS the paper's headline, structurally: a check planned for it answers a
        # central question. Only its linked targets' PRIORITY follows from this.
        qs.append(ReviewQuestion(
            question_id=ABSTRACT_QUESTION, kind="PRINTED_QUANTITY", materiality="CENTRAL",
            question="Does the evidence the paper prints support the headline claims of its "
                     "abstract?",
            why_it_matters="The abstract states what the paper asks a reader to believe.",
            what_would_settle_it="an admissible reproduction of the printed quantities the "
                                 "abstract's claims rest on"))

    repo_available = bool((doc.repo_url or "").strip())
    readiness = reimplementation_readiness(doc)
    objects, coverage = discovered_objects(
        doc, findings, qs, repo_available=repo_available,
        specification_complete=readiness.established, max_formal=max_formal,
        proof_maps=proof_maps, max_step_targets=max_step_targets, check_plans=check_plans)

    ordered = decide.order(objects, artifact_available=repo_available)

    proposed = [decide.plan(obj, artifact_available=repo_available,
                            specification_complete=readiness.established,
                            investigation_open=investigation_open)
               for obj in ordered]
    proposed = _demote_when_a_central_target_is_being_pursued(ordered, proposed)
    proposed = _undefer_when_the_central_target_did_not_settle(ordered, proposed, prior_outcomes)
    proposed = _one_per_experiment(ordered, proposed)
    qs = _question_for_every_executable_target(ordered, proposed, qs)

    plans: list[PlanDecision] = []
    outcomes: list[TargetOutcome] = []
    for obj, plan in zip(ordered, proposed):
        plans.append(plan)
        if plan.requires_execution:
            obj.status = "PENDING"
            continue
        if plan.action == "PAPER_ONLY_RESOLUTION":
            outcome = paper_only_outcome(obj, plan)
        else:
            outcome = TargetOutcome(
                target_id=obj.target_id,
                disposition=_DISPOSITION_FOR_ACTION.get(plan.action, "NOT_ATTEMPTED"),
                action=plan.action, route=plan.route, reason=plan.reason)
        obj.status = outcome.disposition
        outcomes.append(outcome)

    resolved_ids = {o.target_id for o in outcomes
                   if o.disposition in ("PAPER_ONLY_RESOLVED", "PAPER_ARITHMETIC_CONTRADICTION")}
    ts_partial = TargetSet(paper_id=pid, objects=ordered, questions=qs, plans=plans, outcomes=outcomes)
    sync_questions(ts_partial)

    coverage["investigation_open"] = investigation_open
    coverage["targets_superseded_by_established_failure"] = sum(
        1 for o in outcomes if o.disposition == "SUPERSEDED_BY_ESTABLISHED_FAILURE")
    coverage["specification_complete"] = readiness.established
    coverage["specification_missing"] = readiness.missing
    coverage["repository_advertised"] = repo_available
    coverage["targets_requiring_execution"] = sum(1 for p in plans if p.requires_execution)
    by_id = {o.target_id: o for o in ordered}
    coverage["executable_targets_with_a_question"] = sum(
        1 for p in plans if p.requires_execution
        and getattr(by_id.get(p.target_id), "question_id", ""))
    kinds: dict[str, int] = {}
    for p in plans:
        if not p.requires_execution:
            continue
        k = getattr(by_id.get(p.target_id), "question_kind", "") or "UNCLASSIFIED"
        kinds[k] = kinds.get(k, 0) + 1
    coverage["executable_question_kinds"] = kinds
    coverage["targets_resolved_without_execution"] = len(resolved_ids)
    coverage["targets_citation_verified_only"] = sum(
        1 for o in outcomes if o.disposition == "CITATION_VERIFIED_ONLY")
    coverage["targets_blocked_before_execution"] = sum(
        1 for o in outcomes if o.disposition in
        ("SPECIFICATION_BLOCKED", "ARTIFACT_BLOCKED", "ENVIRONMENT_BLOCKED"))
    coverage["targets_outranked_by_a_central_one"] = sum(
        1 for p in plans if p.blocking_gate == "outranked_by_a_central_target")
    coverage["targets_answered_by_another_run"] = sum(
        1 for p in plans if p.blocking_gate == "answered_by_another_target")

    result = TargetSet(paper_id=pid, objects=ordered, questions=qs, plans=plans,
                       outcomes=outcomes, extraction_coverage=coverage)
    return decide.refresh_route_attempts(result, None)


# --------------------------------------------------------------------------------------- #
def _self_check() -> None:
    from .schema import QuantFinding, Section, Table

    # --- reimplementation readiness -----------------------------------------------------
    full = ("We propose a method. The objective is defined as a sum. We train with the "
           "Adam optimizer for 30 epochs at a learning rate of 1e-3 on the CIFAR-100 "
           "dataset and report accuracy on the test set.")
    doc0 = PaperDoc(paper_id="p", sections=[Section(section_idx=0, title="M", text=full)],
                    tables=[Table(table_idx=0, rows=[["a", "1.0"]])])
    r = reimplementation_readiness(doc0)
    assert r.established and not r.missing
    no_cell = reimplementation_readiness(
        PaperDoc(paper_id="p", sections=[Section(section_idx=0, title="M", text=full)]))
    assert not no_cell.established and "comparison_target" in no_cell.missing

    # --- MATHEMATICAL_BOUND / EXACT_CERTIFICATE detection ---------------------------------
    assert is_mathematical_bound("Theorem 3.1. The regret is bounded by O(1/sqrt(T)).")
    assert is_mathematical_bound("Lemma 2 states the error is at most epsilon.")
    assert not is_mathematical_bound("Theorem 3.1 states our main result.")  # no operator
    assert not is_mathematical_bound("The value in the table is 59.3.")      # no theorem word
    bound_finding = Finding(
        finding_id="p-01", lens="contradiction", discrepancy_type="UNCLEAR_REPORTING",
        target="Theorem 3.1. The convergence rate is bounded by O(1/T).",
        evidence_ref="p9", evidence_quote="Theorem 3.1")
    assert kind_for_finding(bound_finding) == "MATHEMATICAL_BOUND"
    # a table-cell citation is a PRINTED NUMBER, never routed as a theorem statement
    cell_finding = bound_finding.model_copy(update={"evidence_ref": "T2:r1:c3"})
    assert kind_for_finding(cell_finding) != "MATHEMATICAL_BOUND"
    routes = _routes("SCIENTIFIC_CLAIM", None, repo_available=True, has_value=False,
                     arithmetic_broken=False, lens="contradiction",
                     question_kind="MATHEMATICAL_BOUND")
    assert routes == ["PAPER_INTERNAL_CHECK", "EXACT_CERTIFICATE"], routes

    # --- questions -----------------------------------------------------------------------
    fs = [Finding(finding_id="c-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                 recommended_resolution="Run the method with one modification at a time."),
         Finding(finding_id="x-01", lens="contradiction", candidate_class="PLAUSIBLE_CONCERN",
                 discrepancy_type="ARITHMETIC_ERROR"),
         Finding(finding_id="o-01", lens="overclaim", candidate_class="DISMISSED")]
    qs = questions_from_findings(fs)
    assert [q.from_finding for q in qs] == ["c-01", "x-01"], "DISMISSED asks nothing"
    assert qs[0].materiality == "CENTRAL"
    assert kind_for_finding(Finding(lens="confound")) == "ATTRIBUTION"

    # --- discovery + priority + planning, end to end --------------------------------------
    doc = PaperDoc(
        paper_id="selfcheck", repo_url="https://example.invalid/repo",
        sections=[Section(section_idx=0, title="Abstract", page_start=1,
                          text="We build a benchmark of 58 topics x 5 templates x 10 "
                               "instances = 2,900 test cases. Accuracy improves markedly."),
                  Section(section_idx=1, title="Method", page_start=2,
                          text="We report 12 x 3 = 40 configurations in the appendix.")],
        tables=[Table(table_idx=1, page=4, rows=[["ours", "61.4"], ["base", "59.3"]])],
        reported_numbers=[QuantFinding(value="61.4", metric="accuracy", benchmark="X",
                                       source_quote="61.4", table_ref="T1:r0:c1")])
    findings2 = [Finding(finding_id="cc-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                        evidence_ref="T1:r0:c1", evidence_quote="61.4",
                        claim="the gain comes from the augmentation")]
    objs, cov = discovered_objects(doc, findings2)
    kinds = {o.kind for o in objs}
    assert "REPRODUCTION_TARGET" in kinds and "IMPLEMENTATION_CLAIM" in kinds
    comps = [o for o in objs if o.kind == "REPRODUCTION_TARGET"]
    assert len(comps) == 2
    good = next(o for o in comps if o.expected_value == 2900.0)
    assert good.harness_addressable and good.centrality == "CENTRAL"
    broken = next(o for o in comps if o.expected_value == 40.0)
    assert broken.routes[0] == "ARITHMETIC_RECHECK"

    ts = build("selfcheck", doc, findings2)
    assert any(p.requires_execution for p in ts.plans), "a central printed quantity must be pursued"
    assert any(p.action == "PAPER_ONLY_RESOLUTION" for p in ts.plans)
    broken_out = next(o for o in ts.outcomes if o.disposition == "PAPER_ARITHMETIC_CONTRADICTION")
    assert broken_out.establishes_failure

    # --- routing keyed on the QUESTION, not on has_value ----------------------------------
    from .schema import QUESTION_KINDS, VERIFICATION_ROUTES
    assert set(ROUTES_FOR_QUESTION) == set(QUESTION_KINDS)
    for qk, rs in ROUTES_FOR_QUESTION.items():
        assert set(rs) <= set(VERIFICATION_ROUTES)
        for r in rs:
            assert decide.kind_for_route(r) or r == "NONE", (qk, r)
    for qk in QUESTION_KINDS:
        got = _routes("SCIENTIFIC_CLAIM", None, repo_available=True, has_value=False,
                      arithmetic_broken=False, lens="", question_kind=qk)
        if qk in _QUESTION_NEEDS_PRINTED_VALUE or qk == "PRIOR_ART":
            assert got == ["NONE"], (qk, got)
        else:
            assert got != ["NONE"], qk

    # --- the early stop: SUPERSEDED_BY_ESTABLISHED_FAILURE reaches every executable target
    ts_closed = build("selfcheck", doc, findings2, investigation_open=False)
    assert all(not p.requires_execution for p in ts_closed.plans)

    print("harness.discover self-check ok")


if __name__ == "__main__":
    _self_check()
