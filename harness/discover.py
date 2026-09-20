"""What is worth checking in this paper, whether it is checkable, and the plan for each.

Consolidates `discovery.py` + `questions.py` + `reimplement.py` + `stages/discover.py`
(2,039 lines -> this file). Deterministic and model-free throughout: every judgement here
is derived from the paper's own structure (`PaperDoc`) and from findings' own closed-
vocabulary self-classification, never from a number, a metric name or a paper identity —
the same signature discipline `decide.py`'s pure functions already hold to.

`python -m harness.discover` runs the self-check.
"""
from __future__ import annotations

import re

from . import decide, locate
from .schema import (
    ClaimRef, DiscoveredObject, Finding, PaperDoc, PlanDecision, ReimplementationIngredient,
    ReimplementationReadiness, ReviewQuestion, TargetOutcome, TargetSet,
)

# ========================================================================================
# PATH B ELIGIBILITY — was reimplement.py. Pure over the document alone: can this paper be
# reconstructed well enough to test, when the authors published no code? Never infers a
# missing ingredient from a present one and never fills a gap with a plausible default —
# a paper that omits its optimizer does not get Adam. Keyword/structure detection over the
# parsed text, not a parser and not a model; conservative in the direction of refusing a
# reimplementation it might have been able to attempt.
# ========================================================================================
INGREDIENTS: tuple[tuple[str, bool, tuple[str, ...]], ...] = (
    ("method", True, ("algorithm", "pseudocode", "we define", "is defined as", "objective",
                      "loss function", "our method", "we propose", "formulation")),
    ("architecture", False, ("architecture", "layer", "encoder", "decoder", "hidden",
                             "embedding", "network", "backbone", "kernel", "estimator")),
    ("preprocessing", False, ("preprocess", "normali", "augment", "tokeni", "resize",
                              "standardi", "cleaning", "filtering")),
    # CONCRETE PROCEDURE ONLY. "We train the model" is the assertion a procedure exists,
    # not the procedure — accepting it once sent an implementer to invent the very thing
    # the paper's own protocol lens had found it omits (no optimizer, no LR, no epochs).
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


def _find(needles: tuple[str, ...], doc: PaperDoc) -> tuple[str, str]:
    """First (locator, quote) matching any needle — the surrounding sentence, so a human
    can check it against the section it names."""
    for s in doc.sections:
        low = s.text.lower()
        for n in needles:
            i = low.find(n)
            if i < 0:
                continue
            start = max(0, low.rfind(".", 0, i) + 1)
            end = low.find(".", i)
            end = len(s.text) if end < 0 else end + 1
            quote = " ".join(s.text[start:end].split())[:_MAX_QUOTE]
            return f"s{s.section_idx}", quote
    return "", ""


def reimplementation_readiness(doc: PaperDoc) -> ReimplementationReadiness:
    """Is there enough in this paper to rebuild the experiment independently? Every
    ingredient carries the locator it was found at, so the judgement is auditable."""
    found: list[ReimplementationIngredient] = []
    for name, required, needles in INGREDIENTS:
        ref, quote = _find(needles, doc)
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
        found.append(ReimplementationIngredient(
            kind=name, required=required, present=bool(ref), ref=ref, quote=quote))

    # A comparison target is an ingredient too — without an addressed cell, any run
    # produces a number in a vacuum. Tables, not prose: the same rule the provenance
    # ceiling already keeps.
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
# QUESTIONS — was questions.py. A finding says what is wrong; a question says what would
# settle it. Templated from a finding's own closed-vocabulary self-classification, never
# from its prose — deterministic, paper-agnostic, and inexpressible as a paper-specific
# rule for the same reason `decide.plan`/`decide.score` are.
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
# The kind for an object the EXTRACTOR found rather than a lens — so every executable
# target carries a question_id; without it, printed table results and prose compositions
# (the two object sources that actually reach execution) had no question to point to.
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
}
_QUESTIONABLE = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "")
_MATERIALITY = {"CONFIRMED_FINDING": "CENTRAL", "PLAUSIBLE_CONCERN": "SUPPORTING",
                "OPEN_QUESTION": "PERIPHERAL", "DISMISSED": "PERIPHERAL", "": "UNASSESSED"}
_MATERIALITY_RANK = {"UNASSESSED": 0, "PERIPHERAL": 1, "SUPPORTING": 2, "CENTRAL": 3}


def kind_for_finding(f: Finding) -> str:
    """Most-specific-first, matching `_template`'s order: a discrepancy type beats a
    baseline class beats a bare lens name."""
    d = (f.discrepancy_type or "").strip().upper()
    if d in _KIND_BY_DISCREPANCY:
        return _KIND_BY_DISCREPANCY[d]
    b = (f.baseline_class or "").strip().upper()
    if b in _KIND_BY_BASELINE:
        return _KIND_BY_BASELINE[b]
    return _KIND_BY_LENS.get((f.lens or "").strip().lower(), "UNCLASSIFIED")


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
    harness's own words from the object's DISCOVERY_KIND, what checking it would settle —
    never invents a concern."""
    kind = obj.question_kind or kind_for_discovery_kind(obj.kind)
    question, why = _TEMPLATE_BY_DISCOVERY_KIND.get(kind, _DEFAULT)
    return ReviewQuestion(
        question_id=f"Q-object-{obj.target_id}", question=question, kind=kind,
        from_finding="", source_finding_ids=[], lens="", why_it_matters=why,
        what_would_settle_it=("evidence that the quantity at this address is what the "
                              "paper's own artifact produces"),
        materiality=obj.centrality if obj.centrality in _MATERIALITY.values() else "UNASSESSED")


def questions_from_findings(findings: list[Finding], *,
                            minted: dict[str, str] | None = None) -> list[ReviewQuestion]:
    """One question per questionable finding, MERGED by (question text, minted address) —
    never by the lens's own `evidence_ref`, which is often a page number ('p7') rather
    than an address: two findings about different sentences on one page must not merge,
    and `materiality` takes the maximum its sources asserted, never more (`route` stays
    NONE — choosing one needs the artifact and the host, neither a property of a finding)."""
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
# DISCOVERY — was discovery.py. What is checkable in this paper, from structure alone.
# ========================================================================================
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")
_KIND_ABBREV = {
    "SCIENTIFIC_CLAIM": "CLM", "EXPERIMENTAL_RESULT": "RES", "BASELINE_COMPARISON": "BAS",
    "ABLATION": "ABL", "CONTROL": "CTL", "DATASET_RESULT": "DAT", "ERROR_ANALYSIS": "ERR",
    "IMPLEMENTATION_CLAIM": "IMP", "REPRODUCTION_TARGET": "REP",
    "UNANSWERED_REVIEW_QUESTION": "QST",
}
_QUESTION_CLASSES = ("OPEN_QUESTION", "DISMISSED")

# WHICH ROUTES A QUESTION OF EACH KIND CAN REACH, cheapest first — replaces a single
# has-a-printed-value gate that wrongly required a number for EVERY question kind (an
# attribution question is not settled by re-deriving a number the paper already
# published). INDEPENDENT_RECONSTRUCTION is the no-artifact FALLBACK for the three
# execution-shaped kinds, offered ALONGSIDE AUTHOR_CODE_EXECUTION when the paper's own
# specification is complete, so a target whose repository identity later fails to bind
# still has somewhere to fall back to.
ROUTES_FOR_QUESTION = {
    "PRINTED_QUANTITY": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"),
    "COMPOSITION": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"),
    "ATTRIBUTION": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    "CONTROL_PRESENCE": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    "PROTOCOL_CONFORMANCE": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    # DELIBERATELY NOT EXECUTABLE: re-producing a quantity cannot say it was the right
    # quantity to produce. Reading the code can say what was computed; running it cannot
    # say what it should have been.
    "SPECIFICATION": ("ARTIFACT_INSPECTION",),
    # NO ROUTE — the literature-search route this kind once reached was deleted (0
    # structurally-bound relations across the whole corpus); honestly NO_ROUTE_AVAILABLE.
    "PRIOR_ART": (),
    "UNCLASSIFIED": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION", "INDEPENDENT_RECONSTRUCTION"),
}
_NEEDS_REPO = ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION")
_RECONSTRUCTION_ROUTES = ("INDEPENDENT_RECONSTRUCTION",)
# Only these three kinds are ABOUT a printed quantity at all — reading the checkout cannot
# say what a number the paper never printed would have been, so with none parsed the
# object reaches no route, exactly as the pre-existing single gate decided for every kind.
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
    """From STRUCTURE alone, never a model — a model that could declare its own target
    central could raise the priority of whatever it happened to find first."""
    if in_abstract or anchored_by_confirmed or self_checking:
        return "CENTRAL"
    if anchored_by_any or is_reported_result:
        return "SUPPORTING"
    return "PERIPHERAL"


def _routes(kind: str, ref: ClaimRef | None, *, repo_available: bool, has_value: bool,
           arithmetic_broken: bool, lens: str, question_kind: str = "UNCLASSIFIED",
           specification_complete: bool = False) -> list[str]:
    """Admissible routes, cheapest first — the escalation policy AS DATA, so `decide.plan`
    stops at the first it can authorize rather than an instruction someone has to remember."""
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
    has_value = quantity is not None and quantity.value is not None
    arithmetic_broken = bool(quantity and quantity.arithmetic_ok is False)
    self_checking = bool(quantity and quantity.expression)
    question_kind = question_kind or kind_for_discovery_kind(kind)
    routes = _routes(kind, ref, repo_available=repo_available, has_value=has_value,
                     arithmetic_broken=arithmetic_broken, lens=lens,
                     question_kind=question_kind, specification_complete=specification_complete)
    resolved = bool(ref and ref.resolved)

    # THREE causes, not one: `harness_addressable` used to collapse into one boolean whose
    # refusal sentence claimed "no re-derivable address" 39/39 times it was really "no
    # route applies" — a limit of the method inventory reported as a limit of extraction.
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


def prose_compositions(doc: PaperDoc) -> list[ClaimRef]:
    """Every prose span stating a composition the harness can re-evaluate — the FinChain
    shape ('58 x 5 x 10 = 2,900'), admitted because the arithmetic is checkable without
    running anything and the total is re-derivable by producing the things and counting."""
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
                       specification_complete: bool = False) -> tuple[list[DiscoveredObject], dict]:
    """(objects, extraction_coverage) for one paper. Deterministic, consults no model,
    runs before any execution."""
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

    # ① printed table results the extractor already addressed and parsed a number for
    for num in doc.reported_numbers:
        ref_s = (num.table_ref or "").strip()
        if not ref_s or ref_s in claimed_refs:
            continue
        ref = locate.resolve(doc, ref_s)
        if not ref.resolved:
            continue
        claimed_refs.add(ref_s)
        _add(objects, taken, _object(
            "DATASET_RESULT" if num.benchmark else "EXPERIMENTAL_RESULT", ref,
            claim_text=num.source_quote or ref.quote, n=len(objects),
            repo_available=repo_available, metric=num.metric,
            experiment=num.benchmark or num.method, is_reported_result=True, taken=taken,
            anchored_by_any=ref.ref in attacked, anchored_by_confirmed=attacked.get(ref.ref, False),
            proposed_by_lens=ref.ref in proposed, material_abstract_idx=material_abstract_idx,
            materiality_doc=doc, specification_complete=specification_complete))

    # ② prose compositions — self-checking, and previously unreachable
    for ref in prose_compositions(doc):
        if ref.ref in claimed_refs:
            continue
        claimed_refs.add(ref.ref)
        _add(objects, taken, _object(
            "REPRODUCTION_TARGET", ref, claim_text=ref.quote, n=len(objects),
            repo_available=repo_available, taken=taken, in_abstract=ref.section_idx == abstract_idx,
            material_abstract_idx=material_abstract_idx, materiality_doc=doc,
            specification_complete=specification_complete))

    # ③ whatever the lenses argued about, at the address the harness can re-derive
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

    # ④ the artifact itself is a claim the paper makes
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
        "objects_addressable": sum(1 for o in objects if o.harness_addressable),
        "findings_with_resolved_ref": sum(
            1 for f in findings if locate.address(doc, f.evidence_ref, f.evidence_quote).resolved),
        "findings_total": len(findings),
        "lens_proposed_verifiable": sum(1 for f in findings if f.verifiable_by_experiment),
    }
    return objects, coverage


# ========================================================================================
# PIPELINE ORCHESTRATION — was stages/discover.py. Builds the whole target set: questions,
# objects, priority order, plans, and — for anything settled without execution — outcomes.
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
    """Pursue a target against the paper's own printed content — nothing runs. Two routes
    reach here and establish DIFFERENT things: ARITHMETIC_RECHECK re-evaluates a printed
    composition operand by operand (either disposition is a real resolution);
    PAPER_INTERNAL_CHECK re-verifies a concern's quotation, which settles nothing about
    whether the concern is correct — sharing one disposition with the first used to print
    findings' own disputed sentences under '## What held up'."""
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
    """Targets answered by the SAME run are one experiment, not many — grouped by (route,
    experiment, metric), which is what a run is actually determined by. A target with no
    declared experiment AND metric groups with nothing (grouping on blanks once collapsed
    two unrelated claims into one)."""
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
    """A SUPPORTING target does not earn an execution while a CENTRAL one is available — a
    SET-level fact `decide.plan` cannot see since it decides one target at a time."""
    if not any(p.requires_execution and o.centrality == "CENTRAL" for o, p in zip(objects, plans)):
        return plans
    out: list[PlanDecision] = []
    for obj, plan in zip(objects, plans):
        if plan.requires_execution and obj.centrality != "CENTRAL":
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
    attempt is known and none settled — otherwise a resource-ordering heuristic becomes a
    standing refusal with no scientific content."""
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
    """Mint a ReviewQuestion for every target that will EXECUTE and has none — the two
    object sources that actually reach execution (a printed table result, a prose
    composition) come from the extractor, not a lens, so a launch could not name what it
    was answering. Minted only for targets this run is about to spend on, not all of them."""
    out = list(qs)
    for obj, plan in zip(objects, plans):
        if not plan.requires_execution or obj.question_id:
            continue
        q = question_for_object(obj)
        obj.question_id = q.question_id
        out.append(q)
    return out


_CONCLUSION = {
    "REPRODUCTION_SUCCESS": "the paper's stated value was re-derived from the authors' own "
                            "code; this question is closed in the paper's favour.",
    "REPRODUCTION_FAILURE": "the authors' own code, run at a verified commit, did not "
                            "produce the value the paper states.",
    "PAPER_INTERNAL_EVIDENCE": "settled against the paper's own printed content, with "
                               "nothing executed.",
    "ARTIFACT_EVIDENCE": "settled by reading the released code.",
    "ARTIFACT_LIMITATION": "no usable artifact reached this question. Nothing about the "
                           "paper follows from that.",
    "ENVIRONMENT_LIMITATION": "this host could not mount the experiment. A fact about the "
                              "machine, not about the paper.",
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
    "NOT_INVESTIGATED": "",
}


def _conclusion(out: TargetOutcome) -> str:
    if out.disposition == "CITATION_VERIFIED_ONLY":
        return ("the concern's quotation was re-verified against the paper, so it cites "
                "the paper accurately; whether the concern is correct was not "
                "investigated and this route could not investigate it.")
    return _CONCLUSION.get(out.evidence_state, "")


def sync_questions(ts: TargetSet) -> TargetSet:
    """Re-derive every ReviewQuestion's harness-written half from the target set — a FOLD,
    not an accumulation, so a question can never keep a resolution the evidence behind it
    has since lost. A merged question reports its HIGHEST-PRIORITY target's state (the
    conservative choice: a question stays open if its central target is blocked even when
    a supporting one settled) — `evidence_refs` still lists every contributing target."""
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
    """Where the target set lives inside a project directory. The ONE spelling —
    `routes.py` and `discover.py` used to spell this three-component path independently."""
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

    reports = audit_stage.load_reports(cfg, pid, doc)
    findings = [f for r in reports for f in r.findings]

    prior = load(cfg, pid)
    prior_outcomes = {o.target_id: o for o in prior.outcomes} if prior else None
    ts = build(pid, doc, findings, investigation_open=investigation_open,
              prior_outcomes=prior_outcomes)
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
         prior_outcomes: dict[str, TargetOutcome] | None = None) -> TargetSet:
    """The whole target set for one paper. Pure with respect to what is on disk — takes
    `findings` directly rather than loading them, so this module has no dependency on
    audit.py's file layout. `investigation_open=False` means a material failure is already
    established from the paper's own evidence, so `decide.plan` refuses every executable
    route with SUPERSEDED_BY_ESTABLISHED_FAILURE — but questions, addresses, routes,
    centrality and coverage still compute in full: a paper that stops early is not
    reviewed less carefully, it is one where the remaining work could not change the answer.
    `prior_outcomes` is this paper's own previous discover pass, the one piece of history
    this otherwise-pure rebuild may consult, so `_undefer_...` can tell "nobody tried the
    central target yet" apart from "it was tried and settled nothing"."""
    minted: dict[str, str] = {}
    for f in findings:
        if not f.finding_id:
            continue
        ref = locate.address(doc, f.evidence_ref or "", f.evidence_quote or "")
        if ref is not None and ref.resolved and ref.ref:
            minted[f.finding_id] = ref.ref
    qs = questions_from_findings(findings, minted=minted)

    repo_available = bool((doc.repo_url or "").strip())
    readiness = reimplementation_readiness(doc)
    objects, coverage = discovered_objects(
        doc, findings, qs, repo_available=repo_available,
        specification_complete=readiness.established)

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
