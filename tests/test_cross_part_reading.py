"""Reading a long paper in parts without losing the relationships between them.

Traversing a paper in bounded parts recovers the prose a single truncated prompt threw
away, and it introduces a failure a single prompt did not have: a relationship between two
spans that now land in different prompts. The contradiction lens exists for exactly one of
those — "they say one thing in the abstract and another in the conclusion" — so a naive
chunking would buy coverage by removing the capability the project was started for.

Two mechanisms answer it, and these tests pin both.

  * **Anchors.** A deterministic packet — title, abstract, conclusion, outline — repeated
    identically in every part. Extracted, never generated, and carrying no model output,
    so a part never inherits another part's findings.
  * **Within-lens synthesis.** After one lens has read every part blind, that same lens
    gets one pass over its own quotation-grounded observations to recover what spans
    across them. It sees no other lens's output and decides nothing.

The independence claim this preserves, stated precisely: OVERCLAIM never sees PROTOCOL's
output and so on between lenses. It never meant a single reader must forget the first half
of a paper before reading the second.
"""
from __future__ import annotations

from harness import reading
from harness.artifacts import Finding, PaperDoc, Section


def _doc(*sections: tuple[str, str], title: str = "A Paper") -> PaperDoc:
    return PaperDoc(paper_id="p", title=title, sections=[
        Section(section_idx=i, title=t, page_start=i + 1, text=body)
        for i, (t, body) in enumerate(sections)])


def _finding(fid: str, quote: str, desc: str, ref: str = "", lens: str = "contradiction"):
    return Finding(finding_id=fid, lens=lens, statement=desc,
                   evidence_quote=quote, evidence_ref=ref)


def _part_containing(plan, needle: str) -> int | None:
    for p in plan.parts:
        if any(needle in s.text for s in p.sections):
            return p.number
    return None


# --------------------------------------------------------------------------- #
# 1. The abstract and the conclusion land in different parts
# --------------------------------------------------------------------------- #
def test_an_abstract_conclusion_contradiction_survives_being_split_across_parts():
    """The capability a naive chunking would have destroyed.

    The claim and its retraction are at opposite ends of a paper long enough to need two
    parts. Neither part contains both. Both must still be in front of the reader.
    """
    doc = _doc(("", "front matter"),
               ("Abstract", "We achieve a 40% reduction in peak memory."),
               ("Method", "m " * 20_000),
               ("Results", "r " * 20_000),
               ("Conclusion", "Peak memory was comparable to the baseline."))
    plan = reading.plan(doc, 20_000)
    assert plan.coverage.parts > 1, "fixture must actually split"

    claim, retraction = "40% reduction in peak memory", "comparable to the baseline"
    assert _part_containing(plan, claim) != _part_containing(plan, retraction), (
        "fixture must place the two poles in different parts")

    # Both poles reach every part, through the anchors rather than through the body.
    for part in plan.parts:
        rendered = reading.render_part(part, plan.anchor)
        assert claim in rendered, f"abstract missing from {part.label}"
        assert retraction in rendered, f"conclusion missing from {part.label}"


def test_the_anchor_packet_contains_no_model_output():
    """A part must not inherit another part's findings, or the panel becomes one reading."""
    doc = _doc(("Abstract", "we claim X"), ("Body", "b" * 5000),
               ("Conclusion", "we showed Y"))
    anchor = reading.anchors(doc)
    rendered = anchor.render()
    for field in ("finding", "severity", "confidence", "verdict", "concern"):
        assert field not in rendered.lower(), field
    assert set(anchor._fields) == {
        "title", "abstract", "conclusion", "outline", "abstract_idx", "conclusion_idx"}


def test_the_anchor_is_identical_in_every_part():
    """Deterministic and stable: a packet that drifted between parts would be a channel."""
    doc = _doc(("Abstract", "a claim"), ("Body", "z" * 60_000), ("Conclusion", "a result"))
    plan = reading.plan(doc, 20_000)
    assert plan.coverage.parts > 1
    heads = {reading.render_part(p, plan.anchor).split("## The span you are reading now")[0]
             for p in plan.parts}
    assert len(heads) == 1, "the anchor text must be byte-identical across parts"


# --------------------------------------------------------------------------- #
# 2-3. Method/results and appendix/main-text, across parts
# --------------------------------------------------------------------------- #
def test_method_and_results_observations_from_different_parts_meet_in_synthesis():
    doc = _doc(("Abstract", "a"), ("Method", "m" * 30_000),
               ("Results", "r" * 30_000), ("Conclusion", "c"))
    plan = reading.plan(doc, 20_000)
    per_part = {
        1: [_finding("F1", "we train for 100 epochs", "the method states 100 epochs")],
        2: [_finding("F2", "trained for 10 epochs", "the results table says 10 epochs")],
    }
    brief = reading.synthesis_brief("p", "contradiction", plan.anchor, per_part)
    body = brief.render()
    assert "100 epochs" in body and "10 epochs" in body, (
        "both observations must reach the synthesis, whatever part produced them")
    assert brief.parts_read == 2
    assert [c["part"] for c in brief.candidates] == [1, 2]
    assert "a method described one way and evaluated another" in body


def test_an_appendix_and_main_text_observation_keep_their_locations():
    """Where each observation came from must survive into synthesis.

    A cross-section claim whose two halves cannot be located is not checkable, and the
    verification gate downstream needs the address as much as the quotation.
    """
    plan = reading.plan(_doc(("Abstract", "a"), ("Main", "x" * 30_000),
                             ("Appendix", "y" * 30_000)), 20_000)
    per_part = {
        1: [Finding(finding_id="F1", lens="contradiction", statement="main text says 4.2",
                    evidence_quote="accuracy of 4.2", evidence_ref="T1:r0:c1")],
        2: [Finding(finding_id="F2", lens="contradiction", statement="appendix says 3.1",
                    evidence_quote="accuracy of 3.1", evidence_ref="T7:r2:c1")],
    }
    brief = reading.synthesis_brief("p", "contradiction", plan.anchor, per_part)
    body = brief.render()
    assert "T1:r0:c1" in body and "T7:r2:c1" in body
    assert "an appendix result inconsistent with the main text" in body


# --------------------------------------------------------------------------- #
# 4. The same concern raised in two parts
# --------------------------------------------------------------------------- #
def test_the_same_concern_from_two_parts_is_presented_for_merging():
    """Overlap and anchors both make one concern reachable from two parts.

    The synthesis is asked to merge; the harness's own merge key is the minted address, so
    two candidates quoting the same span converge downstream regardless.
    """
    plan = reading.plan(_doc(("Abstract", "a"), ("Body", "b" * 40_000)), 20_000)
    same = "no variance is reported for Table 2"
    per_part = {1: [_finding("F1", same, "variance missing", ref="T2:r0:c0")],
                2: [_finding("F2", same, "variance missing", ref="T2:r0:c0")]}
    brief = reading.synthesis_brief("p", "protocol", plan.anchor, per_part)
    assert len(brief.candidates) == 2
    assert {c["ref"] for c in brief.candidates} == {"T2:r0:c0"}
    assert "duplicated concerns that are one concern and should merge" in brief.render()


# --------------------------------------------------------------------------- #
# 5. Consistency must not become a contradiction
# --------------------------------------------------------------------------- #
def test_a_consistent_paper_offers_the_synthesis_nothing_to_contradict():
    """The synthesis may propose only; it cannot manufacture grounding.

    Nothing here asserts a model will behave. It asserts the contract that makes invention
    detectable: every candidate carries a quotation, and a proposal that cannot be
    relocated in the paper is dropped by the same gate every other candidate passes.
    """
    plan = reading.plan(_doc(("Abstract", "we report 5.0"), ("Body", "b" * 40_000),
                             ("Conclusion", "we reported 5.0")), 20_000)
    per_part = {1: [_finding("F1", "we report 5.0", "states 5.0")],
                2: [_finding("F2", "we reported 5.0", "restates 5.0")]}
    brief = reading.synthesis_brief("p", "contradiction", plan.anchor, per_part)
    assert all(c["quote"] for c in brief.candidates), (
        "every input to synthesis is quotation-grounded")
    assert "5.0" in brief.render()


def test_an_unquoted_observation_never_reaches_the_synthesis():
    """The fluent assertion with no referent, refused one layer earlier than usual."""
    plan = reading.plan(_doc(("Abstract", "a"), ("Body", "b" * 1000)), 70_000)
    per_part = {1: [_finding("F1", "", "the evaluation feels weak"),
                    _finding("F2", "a real quotation", "grounded")]}
    brief = reading.synthesis_brief("p", "overclaim", plan.anchor, per_part)
    assert [c["finding_id"] for c in brief.candidates] == ["F2"]


# --------------------------------------------------------------------------- #
# 6. Cross-lens independence is untouched
# --------------------------------------------------------------------------- #
def test_a_synthesis_brief_carries_only_its_own_lenss_observations():
    """The independence that must not be traded for whole-paper reasoning."""
    plan = reading.plan(_doc(("Abstract", "a"), ("Body", "b" * 1000)), 70_000)
    mine = _finding("F1", "quote mine", "mine", lens="contradiction")
    brief = reading.synthesis_brief("p", "contradiction", plan.anchor, {1: [mine]})
    body = brief.render()
    assert brief.lens == "contradiction"
    assert "quote mine" in body
    for foreign in ("quote theirs", "protocol", "confound", "overclaim"):
        assert foreign not in body, f"{foreign!r} leaked into a contradiction brief"


def test_the_brief_carries_no_grade_severity_or_decision():
    """Synthesis proposes. It must not be steerable by what the harness has concluded."""
    plan = reading.plan(_doc(("Abstract", "a"), ("Body", "b" * 1000)), 70_000)
    f = Finding(finding_id="F1", lens="protocol", statement="d",
                evidence_quote="q", severity="FATAL", counted_severity="MINOR")
    brief = reading.synthesis_brief("p", "protocol", plan.anchor, {1: [f]})
    keys = set(brief.candidates[0])
    assert keys == {"part", "section_idx", "page", "ref", "quote", "statement",
                    "finding_id"}
    body = brief.render()
    assert "FATAL" not in body and "MINOR" not in body


# --------------------------------------------------------------------------- #
# Accounting
# --------------------------------------------------------------------------- #
def test_coverage_reports_four_separate_numbers_and_whether_synthesis_is_needed():
    split = reading.plan(_doc(("Abstract", "a"), ("Body", "b" * 60_000),
                              ("Conclusion", "c")), 20_000)
    assert split.coverage.parts > 1
    assert split.coverage.part_local_fraction == 1.0
    assert split.coverage.anchor_chars > 0
    assert split.coverage.anchor_repeat_chars == (
        split.coverage.anchor_chars * (split.coverage.parts - 1))
    assert split.coverage.synthesis_required is True

    whole = reading.plan(_doc(("Abstract", "a"), ("Body", "b" * 100)), 70_000)
    assert whole.coverage.parts == 1
    assert whole.coverage.anchor_repeat_chars == 0, "one part repeats nothing"
    assert whole.coverage.synthesis_required is False, (
        "a paper read in one pass has nothing to synthesise across")


def test_no_prose_reports_no_coverage_rather_than_full_coverage():
    empty = reading.plan(PaperDoc(paper_id="e", title="T"), 70_000)
    assert empty.coverage.part_local_fraction is None
    assert empty.coverage.anchor_overhead_fraction is None


def test_the_anchor_is_charged_against_the_budget_before_the_paper_is_packed():
    """A part that measured as fitting must not arrive over length."""
    doc = _doc(("Abstract", "A" * 3000), ("Conclusion", "C" * 3000),
               ("Body", "b" * 50_000))
    plan = reading.plan(doc, 20_000)
    for part in plan.parts:
        assert len(reading.render_part(part, plan.anchor)) <= 20_000 + 2_000, (
            "body plus anchor must stay near the budget, allowing for headings")


def test_a_very_long_abstract_is_clipped_rather_than_crowding_out_the_paper():
    doc = _doc(("Abstract", "A" * 40_000), ("Body", "b" * 5_000))
    anchor = reading.anchors(doc)
    assert len(anchor.abstract) <= reading.ANCHOR_SECTION_CHARS + 32
    assert "anchor clipped" in anchor.abstract
