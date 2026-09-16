"""The one correspondence a paper does not print, proposed by a reader and checked here.

`python -m harness.claimlink` runs the self-check.

**Why this module exists, measured rather than argued.** A referee's first question about
a printed number is "does the paper's conclusion depend on this?", and this harness could
not answer it. Every model-free mechanism that tried fired on almost nothing:

    discovery._centrality's `in_abstract`, legacy locator ......  0 / 706 objects
    discovery._centrality's `in_abstract`, corrected locator ...  0 / 706 objects
    materiality.basis_for_ref ..................................  1 / 1,729 addresses
    the deterministic claim graph ..............................  0 / 1,729 addresses
    value-matching a headline number to a unique cell ..........  0 / 21 numbers

The reason is not extraction quality and not these eight papers. Across the whole corpus
there are ZERO cross-references in any Abstract and ONE in any Conclusion: an abstract
states a result in prose — "reduces training memory by 40%" — and does not write "see
Table 3", and the 40% it prints is a rounding, a rename, or a delta that appears in no
cell. **The correspondence is semantic, and the document does not state it.**

**So a reader proposes the pairing and the harness checks it**, which is the arrangement
every other model-supplied fact in this system already has. The reader supplies two
quotations. This module decides whether both halves exist, whether the claim is really in
the paper's own summary of itself, and — where both sides carry a number — whether the two
numbers agree. Nothing a reader writes about whether its own pairing holds is read.

**What a link is and is not, and the distinction the first version of this module got
wrong.** An accepted link says: this sentence is in the Abstract or the Conclusion, this
address exists and holds what the reader quoted, and any number both sides carry was
recomputed. Those are the ENDPOINTS. It does not say the claim is true, and — this is the
part that needed correcting — it does not say

    this evidence scientifically supports this headline claim.

**That relationship was proposed by a model.** Unless the DOCUMENT binds the two ends,
nothing here checks it, and calling all of them "machine-checked dependencies" would
launder a semantic judgement into a deterministic result. So an accepted link carries an
AUTHORITY:

    ENDPOINTS_VERIFIED_SEMANTIC_LINK  both ends deterministic, relationship model-proposed
    STRUCTURALLY_BOUND_LINK           the relationship itself established from the document

The first is useful for investigation priority, graph navigation, coverage, route planning
and human explanation, and has no paper-stopping authority. Only the second could ever
become a materiality input, and only after the materiality audit. The two counts are
reported separately and never summed; `structural_binding` below is deliberately hard to
satisfy, and a corpus on which it returns nothing prints zero.

**A MISMATCH is refused, not reported as a contradiction.** Two numbers that disagree may
be a real inconsistency or may be a reader pairing the wrong cell, and this module cannot
tell those apart. Raising a contradiction is the contradiction lens's job, under
quotation verification and grading; minting one here would let a pairing proposal reach a
reader as a finding with no grader and no severity cap. The link is dropped and counted.
"""
from __future__ import annotations

from typing import NamedTuple

from . import claims, materiality
from .artifacts import ClaimLink, ClaimLinkSet, PaperDoc

# How close a claim's printed number must be to the evidence's for ROUNDS_TO. The claim
# side decides the tolerance, because the claim is the rounded one: "40%" against 40.2 is
# the paper rounding its own result, and "40.23%" against 40.2 is not the same statement.
# `claims._printed_half_width` already owns this rule for the arithmetic route, so it is
# borrowed rather than re-derived — two spellings of "how precise is a printed number"
# would be two rules and one place to drift.
_ROUNDING = claims._printed_half_width


class _Side(NamedTuple):
    ok: bool
    ref: str
    section_idx: int
    value: float | None
    # THE TOKEN, not the sentence. `_ROUNDING` measures how precise a PRINTED number is,
    # so it has to be handed "40" and not "We reduce peak memory by 40%." — given the
    # sentence it measures the sentence, returns a half-width of 0.5 for whatever digits
    # it finds first, and a rounding the paper plainly made comes back MISMATCH.
    raw: str
    text: str


def _resolve_evidence(doc: PaperDoc, ref: str, quote: str) -> _Side:
    """The evidence half: an address that resolves, and its value if it has one.

    The quotation is checked against the address when the reader supplied one, using the
    same comparison `stages.audit.verify_evidence` applies — whitespace-normalised, inside
    one unit. A reader that names a real cell and quotes a different cell's contents has
    not identified the evidence, and accepting the address alone would launder that.
    """
    parsed = claims.resolve(doc, (ref or "").strip(), quote or "")
    if not parsed.resolved:
        return _Side(False, "", -1, None, "", "")
    quantity = parsed.quantity
    return _Side(True, parsed.ref, parsed.section_idx,
                 quantity.value if quantity is not None else None,
                 quantity.raw if quantity is not None else "", parsed.quote)


def _resolve_claim(doc: PaperDoc, quote: str) -> _Side:
    """The claim half: minted from the quotation, and required to be prose.

    Minted rather than taken, exactly as `claims.mint` is used for a lens's evidence: the
    reader supplies the sentence and the harness finds it, so a sentence that is not in
    the paper — or that occurs twice and therefore addresses nothing in particular — never
    becomes a claim node.
    """
    minted = claims.mint(doc, (quote or "").strip())
    if not minted.resolved or minted.kind != "prose_claim":
        return _Side(False, "", -1, None, "", "")
    quantity = minted.quantity
    return _Side(True, minted.ref, minted.section_idx,
                 quantity.value if quantity is not None else None,
                 quantity.raw if quantity is not None else "", minted.quote)


def numeric_relation(claim_value: float | None, evidence_value: float | None,
                     claim_raw: str = "") -> str:
    """How the claim's number relates to the evidence's. Pure arithmetic.

    EQUAL, ROUNDS_TO, or MISMATCH when both sides carry a number; otherwise which side is
    missing one. ROUNDS_TO is admitted because it is what papers actually do — an abstract
    prints 40% for a measured 40.2 — and the tolerance is the claim's own printed
    precision rather than a constant, so "40%" admits 40.2 and "40.23%" does not.
    """
    if claim_value is None:
        return "NO_NUMBER_IN_CLAIM"
    if evidence_value is None:
        return "NO_NUMBER_AT_EVIDENCE"
    if claim_value == evidence_value:
        return "EQUAL"
    half = _ROUNDING(claim_raw or str(claim_value))
    return "ROUNDS_TO" if abs(claim_value - evidence_value) <= half else "MISMATCH"


def _evidence_object(doc: PaperDoc, ref: str) -> str:
    """Which numbered object an evidence address belongs to: 'table:3', 'F2', 'E7', ''.

    A cell belongs to its table, so a claim citing "Table 3" reaches every cell of table 3
    — which is what makes `explicit_crossref` a real binding rather than a coincidence of
    numbering. A prose address belongs to no numbered object and can never be bound this
    way, correctly: "as shown in the text" identifies nothing.
    """
    ref = (ref or "").strip()
    m = claims._CELL_REF.match(ref)
    if m:
        return f"table:{int(m.group(1))}"
    return ref if (claims._FIG_REF.match(ref) or claims._EQ_REF.match(ref)) else ""


def _tokens(text: str) -> set[str]:
    """Lower-cased alphanumeric words of 3+ characters. Deterministic, no model, no stemming.

    Three characters because "on", "of" and "is" match everything, and no stemming because
    a stemmer is a heuristic whose failures would be invisible inside a binding that claims
    to be deterministic. A binding that misses a plural is a binding this harness does not
    claim; a binding that fires on a stem collision is a false one.
    """
    import re
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3}


def _cell_identity(doc: PaperDoc, ref: str) -> tuple[str, str, str]:
    """(column header, row label, table caption) for a cell address, each '' when absent.

    The row LABEL is column 0 of the row, which is how every results table in this corpus
    names its arm. Both are read off the extracted table and neither is inferred.
    """
    m = claims._CELL_REF.match((ref or "").strip())
    if not m:
        return "", "", ""
    t, r, c = (int(g) for g in m.groups())
    for table in doc.tables:
        if table.table_idx != t:
            continue
        header = table.header[c] if 0 <= c < len(table.header) else ""
        label = table.cell(r, 0) if c != 0 else ""
        return header, label, table.caption
    return "", "", ""


def structural_binding(doc: PaperDoc, claim: "_Side", evidence: "_Side",
                       relation: str) -> tuple[str, dict[str, bool]]:
    """Does the DOCUMENT bind this claim to this evidence? (basis, the checks it ran).

    Two admitted bases, and a link that satisfies neither is an
    ENDPOINTS_VERIFIED_SEMANTIC_LINK rather than a weaker binding. Everything here is
    re-derivable from `doc.json` by hand, which is what separates the two authorities.

    **`explicit_crossref` — the paper drew the arrow.** The claim sentence contains a
    cross-reference (`doc.crossrefs`, minted by `pdf.extract_crossrefs` and re-resolved
    here) whose printed label identifies exactly one recovered object, and that object is
    the one the evidence address belongs to. Measured over the eight-paper corpus there
    are zero cross-references in any Abstract and one in any Conclusion, so this fires
    almost never — which is the fact that made the whole channel necessary and is not a
    reason to loosen the rule.

    **`quantitative_identity` — the numbers are the same number.** All four of:

      * *statistic* — both ends carry an unambiguous parsed quantity and the two agree to
        the claim's own printed precision (EQUAL or ROUNDS_TO);
      * *metric* — the cell's column header is non-empty and its words appear in the claim
        sentence, so the claim and the cell are about the same measured thing;
      * *comparison* — the cell's row label is non-empty and appears in the claim, so the
        claim is about the same ARM and not merely the same metric;
      * *benchmark* — the table's caption shares a word with the claim that is neither a
        metric nor an arm word, so the two are about the same evaluation and not two
        different datasets that happen to report the same accuracy.

    Three of four is not a binding. The missing one is precisely where a plausible pairing
    goes wrong: same metric and arm on a different benchmark is the confusion that makes a
    number look reproduced when it is a different number.
    """
    claim_words = _tokens(claim.text)

    # --- basis 1 ----------------------------------------------------------------------
    from .claimgraph import _object_for_citation
    want = _evidence_object(doc, evidence.ref)
    if want and claim.section_idx >= 0:
        span = claims.resolve(doc, claim.ref)
        for xref in doc.crossrefs:
            parsed = claims.resolve(doc, xref.span or "")
            if not parsed.resolved or parsed.kind != "prose_claim":
                continue
            if parsed.section_idx != claim.section_idx or span.span is None:
                continue
            if not (span.span[0] <= parsed.span[0] < span.span[1]):
                continue
            if _object_for_citation(doc, xref.kind, xref.number) == want:
                return "explicit_crossref", {"crossref_inside_claim": True,
                                             "label_identifies_one_object": True,
                                             "names_the_evidence_object": True}

    # --- basis 2 ----------------------------------------------------------------------
    header, row_label, caption = _cell_identity(doc, evidence.ref)
    metric_words, arm_words = _tokens(header), _tokens(row_label)
    checks = {
        "statistic_agrees": relation in ("EQUAL", "ROUNDS_TO"),
        "metric_identified": bool(metric_words) and metric_words <= claim_words,
        "comparison_arm_identified": bool(arm_words) and arm_words <= claim_words,
        "benchmark_identified": bool(
            (_tokens(caption) & claim_words) - metric_words - arm_words),
    }
    if all(checks.values()):
        return "quantitative_identity", checks
    return "", checks


def verify(doc: PaperDoc, proposal: ClaimLink, *, abstract_idx: int | None = None,
           conclusion_idx: int | None = None, seen: set | None = None) -> ClaimLink:
    """One proposed link, checked. Returns the link with the harness's half filled in.

    The four conditions, in the order a reader would check them by hand:

      1. the claim quotation is in the paper, exactly once, as prose;
      2. it sits inside the Abstract or the Conclusion — the paper's own summary of
         itself, which is what makes the dependency a HEADLINE one rather than any
         sentence pointing at any number;
      3. the evidence address resolves, and matches the quotation the reader gave for it;
      4. where both sides carry a number, the numbers agree to the claim's own precision.

    Every one of them is re-derivable from `doc.json` by hand, which is the standard every
    machine-written attestation in this harness is held to.
    """
    abstract_idx = (materiality.abstract_section_idx(doc) if abstract_idx is None
                    else abstract_idx)
    conclusion_idx = (materiality.conclusion_section_idx(doc) if conclusion_idx is None
                      else conclusion_idx)

    def refuse(reason: str, observation: str = "") -> ClaimLink:
        return proposal.model_copy(update={
            "accepted": False, "refusal": reason, "verified_observation": observation,
            "link_authority": "REFUSED", "binding_basis": "", "binding_checks": {}})

    claim = _resolve_claim(doc, proposal.claim_quote)
    if not claim.ok:
        return refuse("claim_unresolved",
                      "The quoted sentence does not occur exactly once in the parsed "
                      "paper, so there is no address for the claim half of this link.")
    if claim.section_idx not in (abstract_idx, conclusion_idx):
        return refuse("claim_not_headline",
                      f"The quoted sentence resolves to section {claim.section_idx}, which "
                      f"is neither the Abstract ({abstract_idx}) nor the Conclusion "
                      f"({conclusion_idx}). A dependency of the paper's summary on a "
                      f"number has to start in that summary.")

    evidence = _resolve_evidence(doc, proposal.evidence_ref, proposal.evidence_quote)
    if not evidence.ok:
        return refuse("evidence_unresolved",
                      f"{proposal.evidence_ref!r} names nothing in the parsed paper whose "
                      f"contents match the quotation given for it.")
    if evidence.ref == claim.ref:
        return refuse("self_reference",
                      "The evidence address is the claim's own span. A sentence citing "
                      "itself establishes no dependency on anything.")

    key = (claim.ref, evidence.ref)
    if seen is not None and key in seen:
        return refuse("duplicate",
                      "The same pairing of claim and evidence was already proposed; a "
                      "dependency counted twice would look better supported than it is.")
    relation = numeric_relation(claim.value, evidence.value, claim.raw)
    if relation == "MISMATCH":
        return refuse("numeric_mismatch",
                      f"The claim states {claim.value} and {evidence.ref} contains "
                      f"{evidence.value}. That is either a real inconsistency or the wrong "
                      f"cell, and this module cannot tell which — raising a contradiction "
                      f"is the contradiction lens's job, under quotation verification and "
                      f"independent grading. The link is refused and counted.")
    if seen is not None:
        seen.add(key)

    basis, checks = structural_binding(doc, claim, evidence, relation)
    authority = ("STRUCTURALLY_BOUND_LINK" if basis
                 else "ENDPOINTS_VERIFIED_SEMANTIC_LINK")
    where = "the Abstract" if claim.section_idx == abstract_idx else "the Conclusion"
    detail = {
        "EQUAL": f"and states {claim.value}, which is exactly what {evidence.ref} contains",
        "ROUNDS_TO": (f"and states {claim.value}, which is {evidence.ref}'s {evidence.value} "
                      f"to the precision the claim itself prints"),
        "NO_NUMBER_IN_CLAIM": (f"and states no unambiguous quantity, so this link locates "
                               f"the evidence it points at and asserts no arithmetic"),
        "NO_NUMBER_AT_EVIDENCE": (f"and {evidence.ref} carries no unambiguous quantity, so "
                                  f"this link locates the evidence and asserts no arithmetic"),
    }[relation]
    # WHAT THE OBSERVATION MAY CLAIM depends on the authority, and the difference is the
    # whole point of the split. With no document binding, the harness checked the two
    # ENDPOINTS and a model asserted the support relationship between them, and the
    # sentence has to say that — "depends on" full stop would be the harness attesting to
    # a judgement it did not make.
    says = ({
        "explicit_crossref": (
            f"The claim sentence itself cites the object {evidence.ref} belongs to, so the "
            f"PAPER binds this claim to this evidence and the harness only followed the "
            f"citation. This is a dependency the document states."),
        "quantitative_identity": (
            f"The claim and {evidence.ref} agree on the metric, the benchmark, the "
            f"comparison arm and the value, each established from the extracted table "
            f"rather than asserted, so the relationship is bound by the document."),
    }[basis] if basis else (
        f"A READER proposed that this claim rests on {evidence.ref}; the harness verified "
        f"both ENDPOINTS and has not checked the support relationship between them, which "
        f"remains a model's judgement. Useful for priority, navigation and explanation; "
        f"it carries no authority over the paper-level decision."))
    return proposal.model_copy(update={
        "accepted": True, "refusal": "", "claim_ref": claim.ref,
        "claim_section_idx": claim.section_idx, "numeric_relation": relation,
        "claim_value": claim.value, "evidence_value": evidence.value,
        "link_authority": authority, "binding_basis": basis, "binding_checks": checks,
        "verified_observation": (
            f"The quoted sentence occurs verbatim at {claim.ref}, inside {where}, {detail}. "
            f"{says} It establishes nothing about whether the claim is correct."),
    })


def verify_all(doc: PaperDoc, proposals: list[ClaimLink]) -> ClaimLinkSet:
    """Every proposal for one paper, checked, with the refusals kept and counted."""
    abstract_idx = materiality.abstract_section_idx(doc)
    conclusion_idx = materiality.conclusion_section_idx(doc)
    seen: set = set()
    out: list[ClaimLink] = []
    refusals: dict[str, int] = {}
    for i, proposal in enumerate(proposals or []):
        link = verify(doc, proposal.model_copy(
            update={"link_id": proposal.link_id or f"L{i + 1:02d}"}),
            abstract_idx=abstract_idx, conclusion_idx=conclusion_idx, seen=seen)
        if link.refusal:
            refusals[link.refusal] = refusals.get(link.refusal, 0) + 1
        out.append(link)
    return ClaimLinkSet(
        paper_id=doc.paper_id, links=out, proposed=len(out),
        accepted=sum(1 for x in out if x.accepted), refusals=refusals,
        endpoints_verified=sum(
            1 for x in out if x.link_authority == "ENDPOINTS_VERIFIED_SEMANTIC_LINK"),
        structurally_bound=sum(
            1 for x in out if x.link_authority == "STRUCTURALLY_BOUND_LINK"))


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.claimlink
    from .artifacts import CrossRef, Section, Table

    doc = PaperDoc(
        paper_id="p", title="A Paper", n_pages=3,
        sections=[
            Section(section_idx=0, title="", page_start=1, text="A Paper. Authors."),
            Section(section_idx=1, title="Abstract", page_start=1,
                    text="We reduce peak memory by 40%. The approach is simple and needs "
                         "no extra supervision at any stage of training."),
            Section(section_idx=2, title="Results", page_start=2,
                    text="Peak memory falls to 40.2 on the held-out split."),
            Section(section_idx=3, title="Conclusion", page_start=3,
                    text="Memory use is reduced substantially across every benchmark."),
        ],
        tables=[Table(table_idx=1, page=2, label="1", caption="Table 1 memory",
                      header=["method", "mem"], rows=[["ours", "40.2"], ["base", "67.0"]])])

    def link(**kw):
        return ClaimLink(**kw)

    # 1. The rounding a paper actually does: "40%" in the abstract, 40.2 in the cell.
    ok = verify(doc, link(claim_quote="We reduce peak memory by 40%.",
                          evidence_ref="T1:r0:c1", evidence_quote="40.2"))
    assert ok.accepted and ok.numeric_relation == "ROUNDS_TO", ok
    assert ok.claim_ref.startswith("P1:") and ok.claim_section_idx == 1
    assert "inside the Abstract" in ok.verified_observation
    assert "establishes nothing about whether the claim is correct" in ok.verified_observation

    # 1b. AND IT IS NOT A BOUND LINK. The arithmetic holds and the endpoints resolve, and
    # nothing in the document says this sentence is about that cell — the claim says
    # "peak memory" and the column is headed "mem". A reader asserted the correspondence.
    assert ok.link_authority == "ENDPOINTS_VERIFIED_SEMANTIC_LINK", ok.link_authority
    assert ok.binding_basis == "" and ok.binding_checks["metric_identified"] is False
    assert "support relationship" in ok.verified_observation
    assert "carries no authority over the paper-level decision" in ok.verified_observation

    # 2. A qualitative conclusion claim is a LOCATION and asserts no arithmetic.
    qual = verify(doc, link(
        claim_quote="Memory use is reduced substantially across every benchmark.",
        evidence_ref="T1:r0:c1", evidence_quote="40.2"))
    assert qual.accepted and qual.numeric_relation == "NO_NUMBER_IN_CLAIM", qual

    # 3. The four refusals, each for its own reason.
    absent = verify(doc, link(claim_quote="We reduce peak memory by 99%.",
                              evidence_ref="T1:r0:c1", evidence_quote="40.2"))
    assert absent.refusal == "claim_unresolved", absent

    body = verify(doc, link(claim_quote="Peak memory falls to 40.2 on the held-out split.",
                            evidence_ref="T1:r0:c1", evidence_quote="40.2"))
    assert body.refusal == "claim_not_headline", body
    assert "neither the Abstract" in body.verified_observation

    nowhere = verify(doc, link(claim_quote="We reduce peak memory by 40%.",
                               evidence_ref="T9:r9:c9", evidence_quote="40.2"))
    assert nowhere.refusal == "evidence_unresolved", nowhere

    wrong = verify(doc, link(claim_quote="We reduce peak memory by 40%.",
                             evidence_ref="T1:r1:c1", evidence_quote="67.0"))
    assert wrong.refusal == "numeric_mismatch", wrong
    assert "the contradiction lens's job" in wrong.verified_observation

    # A quotation that does not match the address it names is not evidence, even when
    # the address is real — the same rule `verify_evidence` applies to a lens.
    mismatched_quote = verify(doc, link(claim_quote="We reduce peak memory by 40%.",
                                        evidence_ref="T1:r0:c1", evidence_quote="67.0"))
    assert mismatched_quote.refusal == "evidence_unresolved", mismatched_quote

    # 4. Nothing the reader asserts about its own link is read.
    forged = verify(doc, link(claim_quote="We reduce peak memory by 40%.",
                              evidence_ref="T1:r1:c1", evidence_quote="67.0",
                              accepted=True, numeric_relation="EQUAL",
                              verified_observation="THE HARNESS CONFIRMED THIS"))
    assert forged.accepted is False and forged.refusal == "numeric_mismatch"
    assert "THE HARNESS CONFIRMED THIS" not in forged.verified_observation

    # 4b. THE TWO BINDINGS, each reachable, and each from the document alone.
    quant_doc = PaperDoc(
        paper_id="b", title="B", n_pages=2,
        sections=[
            Section(section_idx=0, title="Abstract", page_start=1,
                    text="Ours reaches 91.4 accuracy on ImageNet. Throughput also "
                         "improves, as Table 3 shows."),
            Section(section_idx=1, title="Conclusion", page_start=2,
                    text="Accuracy on ImageNet improves throughout."),
        ],
        tables=[Table(table_idx=3, page=1, label="3",
                      caption="Table 3 accuracy on ImageNet",
                      header=["method", "accuracy"],
                      rows=[["ours", "91.4"], ["base", "88.0"]])])

    # Metric (the column header), comparison arm (the row label), benchmark (the caption)
    # and the value all bind, each read off the extracted table rather than asserted.
    quant = verify(quant_doc, link(claim_quote="Ours reaches 91.4 accuracy on ImageNet.",
                                   evidence_ref="T3:r0:c1", evidence_quote="91.4"))
    assert quant.accepted and quant.numeric_relation == "EQUAL", quant
    assert quant.binding_basis == "quantitative_identity", quant.binding_checks
    assert quant.link_authority == "STRUCTURALLY_BOUND_LINK"
    assert "bound by the document" in quant.verified_observation

    # The second sentence cites Table 3, the label identifies exactly one recovered table,
    # and the evidence cell is in it. The PAPER drew this arrow; this only followed it.
    cited_quote = "Throughput also improves, as Table 3 shows."
    cited_doc = quant_doc.model_copy(update={"crossrefs": [CrossRef(
        kind="table", number="3", page=1, section_idx=0,
        span=claims.mint(quant_doc, "as Table 3 shows").ref, quote=cited_quote)]})
    cited = verify(cited_doc, link(claim_quote=cited_quote, evidence_ref="T3:r0:c1",
                                   evidence_quote="91.4"))
    assert cited.accepted and cited.link_authority == "STRUCTURALLY_BOUND_LINK", cited
    assert cited.binding_basis == "explicit_crossref", cited.binding_basis
    assert "the PAPER binds this claim" in cited.verified_observation

    # A cross-reference to a DIFFERENT object does not bind this one.
    other = cited_doc.model_copy(update={"tables": [
        *cited_doc.tables,
        Table(table_idx=4, page=2, label="4", caption="Table 4 latency",
              header=["method", "ms"], rows=[["ours", "91.4"]])]})
    assert verify(other, link(claim_quote=cited_quote, evidence_ref="T4:r0:c1",
                              evidence_quote="91.4")).binding_basis == ""

    # THREE OF FOUR IS NOT A BINDING. The conclusion sentence names the metric and the
    # benchmark and no arm, so which of the two rows it means is a reader's guess.
    weak = verify(quant_doc, link(claim_quote="Accuracy on ImageNet improves throughout.",
                                  evidence_ref="T3:r0:c1", evidence_quote="91.4"))
    assert weak.accepted and weak.binding_basis == "", weak.binding_checks
    assert weak.binding_checks["comparison_arm_identified"] is False
    assert weak.binding_checks["benchmark_identified"] is True
    assert weak.link_authority == "ENDPOINTS_VERIFIED_SEMANTIC_LINK"

    # 4c. A reader cannot award itself the only authority that could matter.
    forged_authority = verify(doc, link(
        claim_quote="We reduce peak memory by 40%.", evidence_ref="T1:r0:c1",
        evidence_quote="40.2", link_authority="STRUCTURALLY_BOUND_LINK",
        binding_basis="explicit_crossref", binding_checks={"anything": True}))
    assert forged_authority.link_authority == "ENDPOINTS_VERIFIED_SEMANTIC_LINK"
    assert forged_authority.binding_basis == ""

    # 5. The set keeps its refusals, counted by reason.
    result = verify_all(doc, [
        link(claim_quote="We reduce peak memory by 40%.", evidence_ref="T1:r0:c1",
             evidence_quote="40.2"),
        link(claim_quote="We reduce peak memory by 40%.", evidence_ref="T1:r0:c1",
             evidence_quote="40.2"),                       # the same pairing again
        link(claim_quote="Peak memory falls to 40.2 on the held-out split.",
             evidence_ref="T1:r0:c1", evidence_quote="40.2"),
    ])
    assert result.proposed == 3 and result.accepted == 1, result
    # THE TWO COUNTS ARE SEPARATE AND NEITHER IS "accepted". `accepted` is the endpoint
    # test; `structurally_bound` is the only one with any road to a decision, and on this
    # fixture — as on the real corpus — it is zero, printed as zero.
    assert result.endpoints_verified == 1 and result.structurally_bound == 0, result
    assert result.refusals == {"duplicate": 1, "claim_not_headline": 1}, result.refusals
    assert len(result.links) == 3, "a refused proposal is kept, not discarded"
    assert [x.link_id for x in result.links] == ["L01", "L02", "L03"]

    print("harness.claimlink self-check ok")
