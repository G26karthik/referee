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

**What a link is and is not.** An accepted link says: this sentence is in the Abstract or
the Conclusion, this address exists, and the number the sentence prints is the number at
that address. It does NOT say the claim is true, that the evidence supports it, or that
the reader's reasoning is sound. It is a dependency, and a dependency is what materiality
has always needed and never had.

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
            "accepted": False, "refusal": reason, "verified_observation": observation})

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
    return proposal.model_copy(update={
        "accepted": True, "refusal": "", "claim_ref": claim.ref,
        "claim_section_idx": claim.section_idx, "numeric_relation": relation,
        "claim_value": claim.value, "evidence_value": evidence.value,
        "verified_observation": (
            f"The quoted sentence occurs verbatim at {claim.ref}, inside {where}, {detail}. "
            f"This establishes that the paper's own summary of itself depends on "
            f"{evidence.ref}; it establishes nothing about whether the claim is correct."),
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
    return ClaimLinkSet(paper_id=doc.paper_id, links=out, proposed=len(out),
                        accepted=sum(1 for x in out if x.accepted), refusals=refusals)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.claimlink
    from .artifacts import Section, Table

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
    assert result.refusals == {"duplicate": 1, "claim_not_headline": 1}, result.refusals
    assert len(result.links) == 3, "a refused proposal is kept, not discarded"
    assert [x.link_id for x in result.links] == ["L01", "L02", "L03"]

    print("harness.claimlink self-check ok")
