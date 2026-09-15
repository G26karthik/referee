"""What is worth checking in this paper, and whether it is checkable at all.

`python -m harness.discovery` runs the self-check.

**The boolean this module exists to demote.** `Finding.verifiable_by_experiment` is a
plain `bool` a lens writes. Nothing checked it, nothing corroborated it, and until now it
was the sole gate on the entire execution half of the system: `stages/probe.build_spec`
filtered on it, took `candidates[0]`, and that was the paper's reproduction universe. A
lens that failed to set it on a reproducible claim removed that claim from the pipeline
permanently and invisibly.

Here it survives only as `DiscoveredObject.proposed_by_lens` — metadata. What decides
whether an object can be pursued is `harness_addressable`, a conjunction of structural
facts the harness derives itself: the reference resolves against the parsed paper, a
quantity was parsed where the route needs one, and at least one route is not NONE.

**Discovery is deliberately narrow about prose.** Every sentence in a paper contains a
number somewhere, and treating each one as a target would produce a coverage denominator
made of noise. Two prose sources are admitted, both self-justifying:

  1. a COMPOSITION claim — `a x b x c = d` — because it carries its own check: the
     harness can re-evaluate the arithmetic, and an executable route exists (produce the
     things and count them). This is the shape of the claim that was previously
     unreachable, and it is unreachable no longer.
  2. a span a FINDING already quoted, because a lens has already argued it matters and
     the quote has already been re-verified against the document.

Anything else is not discarded silently — `extraction_coverage` records how much of the
paper was addressable, so "no targets" reads as a measurement rather than as silence.
"""
from __future__ import annotations

import re

from . import (claims, comparison as comparison_mod, materiality,
               questions as questions_mod)
from .artifacts import (ClaimRef, DiscoveredObject, Finding, PaperDoc,
                        ReviewQuestion)

# A sentence, roughly. Papers abbreviate ("et al.", "Fig. 3"), so this splits on a period
# followed by whitespace and a capital, which leaves abbreviations attached rather than
# shattering a claim across two candidate spans.
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")

_KIND_ABBREV = {
    "SCIENTIFIC_CLAIM": "CLM", "EXPERIMENTAL_RESULT": "RES", "BASELINE_COMPARISON": "BAS",
    "ABLATION": "ABL", "CONTROL": "CTL", "DATASET_RESULT": "DAT", "ERROR_ANALYSIS": "ERR",
    "IMPLEMENTATION_CLAIM": "IMP", "REPRODUCTION_TARGET": "REP",
    "UNANSWERED_REVIEW_QUESTION": "QST",
}

# Findings whose own class says they are questions rather than defects. Kept as targets
# so a reviewer can see the question was asked; they carry no severity anywhere.
_QUESTION_CLASSES = ("OPEN_QUESTION", "DISMISSED")


def _target_id(kind: str, ref: str, n: int, taken: set[str] | None = None) -> str:
    """A stable id derived from the object's own address, made unique.

    Kind plus address is stable across runs, which is what makes a target id usable in a
    ledger — but it is not unique: two findings from different lenses citing the same cell
    produce the same (kind, address). While they did, one target could appear twice in a
    report under one id, and a lookup by id returned whichever was last. The suffix is
    appended ONLY on a real collision, so the common case keeps the readable id.
    """
    slug = re.sub(r"[^A-Za-z0-9.-]", "", (ref or f"n{n}").replace(":", ""))
    base = f"TGT-{_KIND_ABBREV.get(kind, 'OBJ')}-{slug or n}"
    if taken is None or base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _centrality(*, in_abstract: bool, anchored_by_confirmed: bool,
                anchored_by_any: bool, self_checking: bool,
                is_reported_result: bool = False) -> str:
    """How much the paper's conclusion rests on this, from STRUCTURE alone.

    Never read from a model. A model that could declare its own target central would be
    able to raise the priority of whatever it happened to find first, which is the same
    defect as a lens that could certify its own evidence.

    The five signals, and why each is structural rather than a judgement:

      in_abstract           the paper put it in its own summary of itself
      anchored_by_confirmed a lens's CONFIRMED_FINDING attacks this exact address
      self_checking         it states a composition, so the paper made it load-bearing
                            enough to spell out the arithmetic
      anchored_by_any       some lens cited this address at all
      is_reported_result    `stages/ingest` kept it in `reported_numbers`, which it only
                            does for a number carrying a verbatim source quote — the
                            extractor's own judgement that this is a RESULT and not an
                            incidental figure. That is a SUPPORTING floor and not more:
                            a paper reports many numbers and stakes its conclusion on few.
    """
    if in_abstract or anchored_by_confirmed or self_checking:
        return "CENTRAL"
    if anchored_by_any or is_reported_result:
        return "SUPPORTING"
    return "PERIPHERAL"


# WHICH ROUTES A QUESTION OF EACH KIND CAN REACH, cheapest first.
#
# This table replaces `has_value` as the thing that decides whether an object can be
# pursued at all. `has_value` asked "did the paper print a number at this address", and it
# gated EVERY route: a target with one could reach execution and a target without one
# could reach nothing, whatever question it raised. That is the right requirement for a
# question about a printed quantity and the wrong one for every other kind — an
# attribution question is not settled by re-deriving a number the paper already published,
# and a missing control is not a number at all. Over the shipped corpus the consequence was
# that 52 of 52 targets carrying an addressing blocker were blocked on "no route applies",
# which was a statement about this table rather than about the papers.
#
# The requirement `has_value` expressed is not discarded — it splits in two, and both
# halves are below. A question that is ABOUT a printed quantity still needs the paper to
# have printed one (`_QUESTION_NEEDS_PRINTED_VALUE`); and separately, any individual route
# that would be reconciled AGAINST a printed value needs one, because a run with nothing
# to compare its number to settles nothing. What no longer needs one is a question answered
# by comparing two arms or by whether something is present — exactly the kinds that could
# reach no route at all before.
ROUTES_FOR_QUESTION = {
    "PRINTED_QUANTITY": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION",
                         "INDEPENDENT_RECONSTRUCTION"),
    "COMPOSITION": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION",
                    "INDEPENDENT_RECONSTRUCTION"),
    # These three kinds need a run that VARIES something rather than one that merely
    # re-derives a printed cell.  FOCUSED_VALIDATION_EXPERIMENT used to be listed here,
    # but no executor implements its between-arms evidence contract.  Advertising that
    # placeholder as applicable made the route denominator promise work the product
    # could not attempt.  Until an executor exists, retain only implemented acquisition
    # and reconstruction paths.
    # INDEPENDENT_RECONSTRUCTION is the no-artifact FALLBACK on all three, not a
    # preference: with no repository there is nothing to inspect and no arm to vary, and
    # the paper-only path is the one that reaches the governed reconstruction route. It
    # compares against a printed value, so it is offered only where the paper printed one
    # — which is why the printed-value requirement below is per-route as well as per-kind.
    "ATTRIBUTION": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    "CONTROL_PRESENCE": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    "PROTOCOL_CONFORMANCE": ("ARTIFACT_INSPECTION", "INDEPENDENT_RECONSTRUCTION"),
    # DELIBERATELY NOT EXECUTABLE. "Is the quantity the paper evaluates the quantity its
    # claim is about?" is not settled by producing that quantity again: a proxy measured
    # perfectly is still a proxy, and a ratio over the wrong population is still the same
    # ratio. Reading the released code can say what was actually computed; running it
    # cannot say what it should have been.
    "SPECIFICATION": ("ARTIFACT_INSPECTION",),
    # Architectural only, as `VERIFICATION_ROUTES` already says: nothing implements
    # literature search, so this reaches a route that produces no evidence and the
    # question is reported open. The route exists so a future one has somewhere to attach.
    "PRIOR_ART": ("LITERATURE_SEARCH",),
    # EXACTLY WHAT `has_value` USED TO GIVE EVERY TARGET. A finding that classified itself
    # as nothing gets the old behaviour, so the default of this vocabulary is never a new
    # capability.
    "UNCLASSIFIED": ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION",
                     "INDEPENDENT_RECONSTRUCTION"),
}

# A route that only exists when the paper's own artifact does. Kept as data beside the
# table above so the structural facts a route can require are visible in one place.
_NEEDS_REPO = ("ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION")

# INDEPENDENT_RECONSTRUCTION used to be offered ONLY when no repository existed at all —
# the sole fallback for a paper with nothing to inspect or run. **Step 6 widens that.**
# `AUTHOR_CODE_EXECUTION` binding is a fact only `stages/probe.py` can establish, against a
# real checkout; discovery cannot know in advance whether the cited cell will resolve to
# one of the repository's commands. A route offered here as the ONLY option when a repo
# exists therefore left no route to fall back to when identity failed to bind — the target
# ended IDENTITY_BLOCKED with nothing else considered, even on a paper whose method section
# says enough to attempt an independent rebuild.
#
# So it is now offered ALONGSIDE AUTHOR_CODE_EXECUTION whenever the paper ALSO specifies
# enough (`specification_complete`) — never merely because a repository failed to bind,
# since discovery runs before any checkout exists and cannot know that yet. What changes at
# PROBE time is not this route list; it is which entry the planner is willing to consider,
# via `planner.plan(..., author_code_exhausted=True)` once identity has genuinely failed.
_RECONSTRUCTION_ROUTES = ("INDEPENDENT_RECONSTRUCTION",)

# WHICH KINDS ARE ABOUT A PRINTED QUANTITY AT ALL. For these three, every route exists to
# establish what the number at that address is — reading the checkout cannot say what a
# number the paper never printed would have been — so with no parsed quantity the object
# has nothing to be checked against and reaches no route, exactly as `has_value` decided
# for every target before this table existed.
#
# Listed rather than derived from the route table, and the difference matters: all three
# of the widened kinds carry INDEPENDENT_RECONSTRUCTION as a no-artifact fallback, so
# deriving this set with `any(needs_printed_value)` would let that fallback re-impose the
# requirement the widening exists to remove. The two rules are separate because they are
# about different things — this one about what the QUESTION is, the per-route one below
# about what an individual route could be reconciled against.
_QUESTION_NEEDS_PRINTED_VALUE = ("PRINTED_QUANTITY", "COMPOSITION", "UNCLASSIFIED")


def _routes(kind: str, ref: ClaimRef | None, *, repo_available: bool,
            has_value: bool, arithmetic_broken: bool, lens: str,
            question_kind: str = "UNCLASSIFIED",
            specification_complete: bool = False) -> list[str]:
    """Admissible routes for one object, cheapest first.

    The ORDER is the escalation policy, expressed as data. `harness.planner` walks this
    list and stops at the first route it can authorize, so "use the cheapest admissible
    evidence, and only then execute" is a property of this ordering rather than an
    instruction someone has to remember.

    `question_kind` is what decides the candidate set (`ROUTES_FOR_QUESTION`); structural
    facts then filter it — the artifact exists, the paper printed a quantity where the
    route's comparison needs one, the composition does not evaluate, and — Step 6 —
    whether the paper's own specification is complete enough to offer
    INDEPENDENT_RECONSTRUCTION as a FALLBACK alongside AUTHOR_CODE_EXECUTION rather than
    only in its absence.
    """
    routes: list[str] = []
    if arithmetic_broken:
        # The paper's own printed composition does not evaluate. Nothing needs to run,
        # and a route that costs nothing must be offered before one that costs a GPU-day.
        routes.append("ARITHMETIC_RECHECK")
    if lens == "contradiction":
        routes.append("PAPER_INTERNAL_CHECK")

    qk = question_kind if question_kind in ROUTES_FOR_QUESTION else "UNCLASSIFIED"
    if kind == "IMPLEMENTATION_CLAIM":
        routes.append("ARTIFACT_INSPECTION" if repo_available else "NONE")
    elif has_value or qk not in _QUESTION_NEEDS_PRINTED_VALUE:
        # The `has_value` requirement, narrowed to the question kinds it is actually
        # about. A question whose evidence would be reconciled against a printed cell
        # needs the cell; one answered by comparing two arms, or by whether something is
        # present, does not — and requiring it of those was what made a whole class of
        # question reach no route at all.
        for candidate in ROUTES_FOR_QUESTION[qk]:
            if candidate in _RECONSTRUCTION_ROUTES:
                # No repo: the only route left, unconditionally — the pre-Step-6
                # behaviour, unchanged. A repo exists: offered ANYWAY, but only as a
                # fallback the paper's own method section can support — inventing the
                # missing half of an unspecified experiment measures our reconstruction,
                # not the paper, which is exactly what `planner.INFEASIBLE_SPECIFICATION`
                # refuses once this route is actually chosen.
                if repo_available and not specification_complete:
                    continue
            elif candidate in _NEEDS_REPO and not repo_available:
                continue
            # And a route that would be reconciled AGAINST a printed value needs one even
            # when the question itself does not: an independent reconstruction produces a
            # number, and a number with nothing to compare it to settles nothing.
            if comparison_mod.needs_printed_value(candidate) and not has_value:
                continue
            routes.append(candidate)
    seen: set[str] = set()
    ordered = [r for r in routes if not (r in seen or seen.add(r))]
    return [r for r in ordered if r != "NONE"] or ["NONE"]


def _object(kind: str, ref: ClaimRef | None, *, claim_text: str, n: int,
            repo_available: bool, lens: str = "", metric: str = "", experiment: str = "",
            in_abstract: bool = False, anchored_by_confirmed: bool = False,
            anchored_by_any: bool = False, is_reported_result: bool = False,
            proposed_by_lens: bool = False, question_id: str = "",
            question_kind: str = "", specification_complete: bool = False,
            counter_explanations: tuple[str, ...] = (),
            material_abstract_idx: int = -1,
            materiality_doc: PaperDoc | None = None,
            taken: set[str] | None = None) -> DiscoveredObject:
    quantity = ref.quantity if (ref and ref.quantity) else None
    has_value = quantity is not None and quantity.value is not None
    arithmetic_broken = bool(quantity and quantity.arithmetic_ok is False)
    self_checking = bool(quantity and quantity.expression)
    # The kind of question this object exists to answer, and therefore which routes it
    # can reach. A finding-anchored object inherits its finding's kind; an object the
    # extractor found derives one from its own DISCOVERY_KIND. Both walks live in
    # `harness.questions`, so the object and the ReviewQuestion minted for it below can
    # never disagree about what is being asked.
    question_kind = question_kind or questions_mod.kind_for_discovery_kind(kind)
    routes = _routes(kind, ref, repo_available=repo_available, has_value=has_value,
                     arithmetic_broken=arithmetic_broken, lens=lens,
                     question_kind=question_kind,
                     specification_complete=specification_complete)
    resolved = bool(ref and ref.resolved)

    # `harness_addressable` is a conjunction of three distinct requirements, and until now
    # only the conjunction survived: every failure reached `planner.classify` as one
    # boolean and left as INFEASIBLE_ADDRESSING, whose reader-facing sentence claims this
    # review could not build an address. Over the shipped corpus that was printed 39 times
    # and was wrong every time — all 52 targets carrying it had a RESOLVED address, and
    # the actual blocker in 52 of 52 was the third requirement. So the CAUSE is named
    # here, as a closed vocabulary token, and the planner reports the one that applies.
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
        expected_raw=quantity.raw if quantity else "",
        experiment=experiment,
        # DERIVED FROM THE OBJECT'S OWN ADDRESS, in one place for every discovery source,
        # so a call site cannot supply a materiality that disagrees with where the claim
        # actually sits. Object references are admitted only when this parsed document's
        # own Abstract/Conclusion provides the unique dependency link.
        materiality_basis=materiality.basis_for_ref(
            ref, material_abstract_idx, doc=materiality_doc),
        centrality=_centrality(in_abstract=in_abstract,
                               anchored_by_confirmed=anchored_by_confirmed,
                               anchored_by_any=anchored_by_any,
                               self_checking=self_checking,
                               is_reported_result=is_reported_result),
        evidence_requirements=requirements, routes=routes,
        proposed_by_lens=proposed_by_lens, harness_addressable=addressable, note=note,
        addressing_blocker=blocker,
        counter_explanations=list(counter_explanations),
        question_id=question_id, question_kind=question_kind,
    )


def _add(objects: list, taken: set, obj: DiscoveredObject) -> DiscoveredObject:
    """Append one object and reserve its id, so the next collision gets a suffix."""
    taken.add(obj.target_id)
    objects.append(obj)
    return obj


def prose_compositions(doc: PaperDoc) -> list[ClaimRef]:
    """Every prose span that states a composition the harness can re-evaluate.

    This is the FinChain shape — "58 topics x 5 templates x 10 instances = 2,900 test
    cases" — and it is admitted because it justifies itself twice over: the arithmetic is
    checkable without running anything, and the total is a quantity an execution can
    re-derive by producing the things and counting them.
    """
    out: list[ClaimRef] = []
    seen: set[str] = set()
    for section in doc.sections:
        for sentence in _SENTENCE.split(section.text or ""):
            q = claims.parse_quantity(sentence)
            if not (q and q.expression and q.value is not None):
                continue
            ref = claims.mint(doc, sentence.strip())
            if ref.resolved and ref.ref not in seen:
                seen.add(ref.ref)
                out.append(ref)
    return out


def discover(doc: PaperDoc, findings: list[Finding],
             questions: list[ReviewQuestion] | None = None,
             *, repo_available: bool | None = None,
             specification_complete: bool = False) -> tuple[list[DiscoveredObject], dict]:
    """(objects, extraction_coverage) for one paper.

    Deterministic given its inputs. Runs before any execution and consults no model.

    `specification_complete` is Step 6's fallback condition: with it True and a repository
    available, every object whose kind offers INDEPENDENT_RECONSTRUCTION gets it ALONGSIDE
    AUTHOR_CODE_EXECUTION rather than not at all, so a target whose identity later fails to
    bind against the checkout still has a route `planner.plan(...,
    author_code_exhausted=True)` can fall back to. Default False reproduces the route list
    exactly as it was before Step 6 for every existing caller.
    """
    questions = questions or []
    if repo_available is None:
        repo_available = bool((doc.repo_url or "").strip())
    # EVERY source finding, not just the first. Keying on `from_finding` alone meant a
    # merged question's SECOND object got `question_id=""`, was skipped by
    # `sync_questions`, and never entered `evidence_refs` — so the documented mitigation
    # "evidence_refs lists every target so nothing is hidden" did not work, and the
    # question reported one target when it had two. Measured: apt-icml has 8 questions and
    # exactly 8 question-carrying objects, for findings that demonstrably merged.
    by_finding: dict[str, ReviewQuestion] = {}
    for q in questions:
        for fid in (q.source_finding_ids or ([q.from_finding] if q.from_finding else [])):
            if fid:
                by_finding.setdefault(fid, q)
    # LEGACY, and knowingly wrong: section 0 is the untitled front-matter block (title and
    # authors), so this fired on 0 of 878 objects across the eight-paper corpus. It feeds
    # `_centrality`'s `in_abstract` and nothing else, and it is left exactly as it is
    # because fixing it moves `centrality`, which propagates into priority ordering,
    # `planner._WORTH_PURSUING`, `unresolved_central`, `disposition.blockers_from` and the
    # coverage accounting. That change needs a measured corpus re-derivation and is not
    # part of this task.
    abstract_idx = doc.sections[0].section_idx if doc.sections else -1
    # CORRECTED, and used ONLY for materiality: the Abstract located by its own heading.
    # The divergence above is deliberate and temporary — see `harness.materiality`.
    material_abstract_idx = materiality.abstract_section_idx(doc)

    objects: list[DiscoveredObject] = []
    claimed_refs: set[str] = set()
    taken: set[str] = set()

    # Which addresses a lens actually argued about, resolved once so both the printed
    # results below and the finding-anchored objects further down agree about them.
    # Without this the same cell could be SUPPORTING as a reported number and CENTRAL as
    # a finding's target, and which one a reader saw would depend on iteration order.
    attacked: dict[str, bool] = {}
    proposed: set[str] = set()
    for f in findings:
        r = claims.address(doc, f.evidence_ref, f.evidence_quote)
        if r.resolved:
            confirmed = (f.candidate_class or "") == "CONFIRMED_FINDING"
            attacked[r.ref] = attacked.get(r.ref, False) or confirmed
            # The lens's own `verifiable_by_experiment` follows the ADDRESS, not the
            # finding: a printed result discovered in ① and separately cited by a lens is
            # one object, and losing the lens's opinion of it because the extractor found
            # it first would make the metadata depend on iteration order.
            if f.verifiable_by_experiment:
                proposed.add(r.ref)

    # ① printed table results the extractor already addressed and parsed a number for
    for num in doc.reported_numbers:
        ref_s = (num.table_ref or "").strip()
        if not ref_s or ref_s in claimed_refs:
            continue
        ref = claims.resolve(doc, ref_s)
        if not ref.resolved:
            continue
        claimed_refs.add(ref_s)
        _add(objects, taken, _object(
            "DATASET_RESULT" if num.benchmark else "EXPERIMENTAL_RESULT", ref,
            claim_text=num.source_quote or ref.quote, n=len(objects),
            repo_available=repo_available, metric=num.metric,
            experiment=num.benchmark or num.method, is_reported_result=True, taken=taken,
            anchored_by_any=ref.ref in attacked,
            anchored_by_confirmed=attacked.get(ref.ref, False),
            proposed_by_lens=ref.ref in proposed,
            material_abstract_idx=material_abstract_idx,
            materiality_doc=doc,
            specification_complete=specification_complete))

    # ② prose compositions — self-checking, and previously unreachable
    for ref in prose_compositions(doc):
        if ref.ref in claimed_refs:
            continue
        claimed_refs.add(ref.ref)
        _add(objects, taken, _object(
            "REPRODUCTION_TARGET", ref, claim_text=ref.quote, n=len(objects),
            repo_available=repo_available, taken=taken,
            in_abstract=ref.section_idx == abstract_idx,
            material_abstract_idx=material_abstract_idx,
            materiality_doc=doc,
            specification_complete=specification_complete))

    # ③ whatever the lenses argued about, at the address the harness can re-derive
    for f in findings:
        ref = claims.address(doc, f.evidence_ref, f.evidence_quote)
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
            in_abstract=ref.section_idx == abstract_idx,
            anchored_by_confirmed=confirmed, anchored_by_any=True,
            proposed_by_lens=bool(f.verifiable_by_experiment), taken=taken,
            material_abstract_idx=material_abstract_idx,
            materiality_doc=doc,
            specification_complete=specification_complete,
            # The finding's OWN alternative readings, carried verbatim. This is the only
            # place they enter the discovery layer; nothing downstream may add to them,
            # so "what would this experiment discriminate between?" is always answered in
            # the words of whoever raised the concern.
            counter_explanations=tuple(f.counter_explanations),
            question_id=q.question_id if q else "",
            # The FINDING's kind, not the object's DISCOVERY_KIND. `q` may be absent —
            # a DISMISSED finding raises no question — and the routing must still be
            # decided by what the finding said about itself rather than by which bucket
            # the object landed in.
            question_kind=q.kind if q else questions_mod.kind_for_finding(f)))
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
        "sections": len(doc.sections),
        "equations_extracted": len(doc.equations),
        "objects_discovered": len(objects),
        "objects_addressable": sum(1 for o in objects if o.harness_addressable),
        "findings_with_resolved_ref": sum(
            1 for f in findings if claims.address(doc, f.evidence_ref, f.evidence_quote).resolved),
        "findings_total": len(findings),
        "lens_proposed_verifiable": sum(1 for f in findings if f.verifiable_by_experiment),
    }
    return objects, coverage


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    from .artifacts import QuantFinding, Section, Table

    doc = PaperDoc(
        paper_id="selfcheck", repo_url="https://example.invalid/repo",
        sections=[
            Section(section_idx=0, title="Abstract", page_start=1,
                    text="We build a benchmark of 58 topics x 5 templates x 10 instances "
                         "= 2,900 test cases. Accuracy improves markedly."),
            Section(section_idx=1, title="Method", page_start=2,
                    text="We report 12 x 3 = 40 configurations in the appendix."),
        ],
        tables=[Table(table_idx=1, page=4, rows=[["ours", "61.4"], ["base", "59.3"]])],
        reported_numbers=[QuantFinding(value="61.4", metric="accuracy", benchmark="X",
                                       source_quote="61.4", table_ref="T1:r0:c1")],
    )
    fs = [Finding(finding_id="c-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                  evidence_ref="T1:r0:c1", evidence_quote="61.4",
                  claim="the gain comes from the augmentation", verifiable_by_experiment=False)]

    objs, cov = discover(doc, fs)
    kinds = {o.kind for o in objs}
    assert "REPRODUCTION_TARGET" in kinds, "a prose composition must become a target"
    assert "IMPLEMENTATION_CLAIM" in kinds and "DATASET_RESULT" in kinds

    comps = [o for o in objs if o.kind == "REPRODUCTION_TARGET"]
    assert len(comps) == 2, comps
    good = next(o for o in comps if o.expected_value == 2900.0)
    assert good.harness_addressable and good.centrality == "CENTRAL"
    assert "AUTHOR_CODE_EXECUTION" in good.routes and good.routes[0] == "ARTIFACT_INSPECTION"

    broken = next(o for o in comps if o.expected_value == 40.0)
    assert broken.routes[0] == "ARITHMETIC_RECHECK", "a broken composition needs no execution"

    # the lens said NOT verifiable; the harness disagrees on structural grounds
    anchored = next(o for o in objs if o.claim_text.startswith("the gain"))
    assert anchored.proposed_by_lens is False and anchored.harness_addressable is True

    assert cov["objects_addressable"] >= 3 and cov["lens_proposed_verifiable"] == 0

    # an unresolvable finding is still discovered, and says why it cannot be pursued
    ghost = [Finding(finding_id="g-01", lens="overclaim", evidence_ref="p9",
                     evidence_quote="a sentence that is not in this paper at all")]
    gobjs, _ = discover(doc, ghost)
    g = next(o for o in gobjs if o.target_id.startswith("TGT-CLM"))
    assert not g.harness_addressable and "re-derive" in g.note

    # --- ROUTING IS KEYED ON THE QUESTION, NOT ON `has_value` --------------------------
    # The table is closed, and every route in it is one `harness.comparison` knows what to
    # compare. A route with no comparison could be planned, executed and then have nothing
    # to be held against, which is the shape this whole layer exists to make impossible.
    from .artifacts import QUESTION_KINDS, VERIFICATION_ROUTES

    assert set(ROUTES_FOR_QUESTION) == set(QUESTION_KINDS), (
        "every question kind must say which routes it can reach")
    for qk, rs in ROUTES_FOR_QUESTION.items():
        assert set(rs) <= set(VERIFICATION_ROUTES), qk
        for r in rs:
            assert comparison_mod.kind_for_route(r), (qk, r)

    # THE TWO PRINTED-VALUE RULES ARE ABOUT DIFFERENT THINGS and must not be collapsed.
    # Swept behaviourally rather than asserted structurally, because the structural
    # statement is not the one that matters: PRINTED_QUANTITY carries ARTIFACT_INSPECTION,
    # which needs no printed value on its own and is pointless when there is no number to
    # establish, and the whole-kind gate is what says so.
    assert set(_QUESTION_NEEDS_PRINTED_VALUE) <= set(QUESTION_KINDS)
    for qk in QUESTION_KINDS:
        got = _routes("SCIENTIFIC_CLAIM", None, repo_available=True, has_value=False,
                      arithmetic_broken=False, lens="", question_kind=qk)
        if qk in _QUESTION_NEEDS_PRINTED_VALUE:
            assert got == ["NONE"], (qk, got)
        else:
            assert got != ["NONE"], (
                f"{qk} is not about a printed quantity and must reach a route without one")

    # A question with no parsed quantity can still reach static artifact inspection, but
    # the unimplemented focused-validation placeholder is not advertised.
    for qk in ("ATTRIBUTION", "CONTROL_PRESENCE", "PROTOCOL_CONFORMANCE"):
        got = _routes("SCIENTIFIC_CLAIM", None, repo_available=True, has_value=False,
                      arithmetic_broken=False, lens="", question_kind=qk)
        assert got == ["ARTIFACT_INSPECTION"], (qk, got)
        assert "FOCUSED_VALIDATION_EXPERIMENT" not in got
        assert got != ["NONE"], qk
    # and one whose comparison IS against a printed value still does not
    assert _routes("SCIENTIFIC_CLAIM", None, repo_available=True, has_value=False,
                   arithmetic_broken=False, lens="",
                   question_kind="PRINTED_QUANTITY") == ["NONE"], (
        "a run reconciled against a cell needs the cell")

    # UNCLASSIFIED reproduces the pre-vocabulary behaviour exactly, which is what makes
    # the default of this vocabulary safe: it is never a new capability.
    for repo in (True, False):
        for val in (True, False):
            old = (["ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION"] if repo
                   else ["INDEPENDENT_RECONSTRUCTION"]) if val else ["NONE"]
            assert _routes("SCIENTIFIC_CLAIM", None, repo_available=repo, has_value=val,
                           arithmetic_broken=False, lens="",
                           question_kind="UNCLASSIFIED") == old, (repo, val)

    # A specification question reads the artifact and does not run it: producing the same
    # number again cannot say the number was the right quantity to produce.
    spec_routes = _routes("SCIENTIFIC_CLAIM", None, repo_available=True, has_value=True,
                          arithmetic_broken=False, lens="", question_kind="SPECIFICATION")
    assert spec_routes == ["ARTIFACT_INSPECTION"], spec_routes

    # And the object carries the kind it was routed by, so nothing downstream re-derives it.
    confound = [Finding(finding_id="c-02", lens="confound", candidate_class="CONFIRMED_FINDING",
                        evidence_quote="Accuracy improves markedly.")]
    cobjs, _ = discover(doc, confound, questions_mod.derive(confound))
    anchored_obj = next(o for o in cobjs if o.question_kind == "ATTRIBUTION")
    assert "FOCUSED_VALIDATION_EXPERIMENT" not in anchored_obj.routes
    assert anchored_obj.harness_addressable, (
        "an attribution question with a resolved address and a repository is checkable")
    print("harness.discovery self-check ok")


if __name__ == "__main__":
    _self_check()
