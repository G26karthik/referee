"""How a whole paper reaches a reader, and what that costs.

`python -m harness.reading` runs the self-check.

**The problem this owns.** `pdf.render_sections` divides one character budget equally
across every section and hard-slices each one, so a long paper was cut before any reader
saw a word of it: between 33% and 84% of the extracted prose over the evaluated corpus,
while the review's scope line said four lenses read the paper. `pdf.plan_reading` removes
the cut by traversing the paper in bounded parts. This module owns what that traversal
needs to stay scientifically sound, which is more than the parts themselves.

**Three things, kept apart on purpose.**

*Anchors* are a small, deterministic packet repeated identically in every part: the title,
the abstract, the conclusion and the section outline. They exist because a paper read in
two parts otherwise loses the comparison the contradiction lens was built for — "the
abstract says one thing and the conclusion says another" is unraisable when the abstract
and the conclusion are in different prompts. The packet is extracted, never generated, and
carries NO model output: a part must not inherit another part's findings, or the panel's
four independent readings become one reading echoed forward.

*Parts* are blind to one another. A lens reading part 2 does not see what it wrote about
part 1. That keeps a concern from being anchored by an earlier pass's framing, and it
keeps every candidate attributable to the span that produced it.

*Synthesis* is where cross-part reasoning happens, once, per lens, after that lens has
read every part. Its inputs are the anchors and that lens's own quotation-grounded
candidates with their locations — not hidden reasoning, not another lens's output. It may
PROPOSE concerns and decides nothing; everything it proposes passes the same quotation and
address verification as every other candidate.

**What independence means here, stated precisely because it is easy to overclaim.**
Independence is BETWEEN lenses: `overclaim` never sees `protocol`'s output, `protocol`
never sees `confound`'s, and so on. It has never meant that one scientific reader must
forget the first half of a paper before reading the second. A reviewer who cannot reason
across sections cannot review a paper, and claiming whole-paper review while forbidding it
would be the overclaim this system exists to catch.
"""
from __future__ import annotations

from typing import NamedTuple

from . import materiality as materiality_mod, pdf
from .artifacts import PaperDoc, Section

# How much of a part's budget the repeated anchor packet may take. Above this the anchors
# are trimmed rather than the paper: a packet that crowds out the text it was meant to
# contextualise has inverted its own purpose.
ANCHOR_BUDGET_FRACTION = 0.25
# Longest abstract or conclusion carried whole. Beyond it the head is kept, because an
# abstract states its claims first and a conclusion restates them first.
ANCHOR_SECTION_CHARS = 6_000


class AnchorPacket(NamedTuple):
    """Deterministic, identical in every part, and free of model output."""

    title: str
    abstract: str
    conclusion: str
    outline: list[tuple[int, str, int]]        # (section_idx, title, chars)
    abstract_idx: int
    conclusion_idx: int

    @property
    def chars(self) -> int:
        return len(self.render())

    def render(self) -> str:
        out = [f"# {self.title}"] if self.title else []
        if self.abstract:
            out.append(f"## Abstract  [section {self.abstract_idx}]\n{self.abstract}")
        if self.conclusion:
            out.append(f"## Conclusion  [section {self.conclusion_idx}]\n{self.conclusion}")
        if self.outline:
            rows = "\n".join(f"- [{i}] {t or '(untitled)'}  ({n:,} chars)"
                             for i, t, n in self.outline)
            out.append("## Section outline of the whole paper\n" + rows)
        return "\n\n".join(out)


def _clip(text: str, limit: int = ANCHOR_SECTION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + " …[anchor clipped]"


def anchors(doc: PaperDoc) -> AnchorPacket:
    """The packet every part carries.

    The abstract and conclusion locators are `harness.materiality`'s, deliberately: a
    second spelling of "which section is the abstract" is how the two drift. `discovery`
    carried one until recently — it read `doc.sections[0]`, the untitled front-matter
    block — and there is now exactly one locator in the harness. This module uses it
    rather than adding a third.
    """
    a_idx = materiality_mod.abstract_section_idx(doc)
    c_idx = materiality_mod.conclusion_section_idx(doc)
    by_idx = {s.section_idx: s for s in doc.sections}
    return AnchorPacket(
        title=doc.title or "",
        abstract=_clip((by_idx[a_idx].text if a_idx in by_idx else "") or ""),
        conclusion=_clip((by_idx[c_idx].text if c_idx in by_idx else "") or ""),
        outline=[(s.section_idx, s.title, len(s.text or "")) for s in doc.sections
                 if (s.text or "") or s.title],
        abstract_idx=a_idx,
        conclusion_idx=c_idx,
    )


class ReadingCoverage(NamedTuple):
    """Four numbers that answer four different questions, reported separately.

    Collapsing them is how "the readers saw the paper" gets printed about a run in which
    they saw a third of it. `part_local_fraction` is the one that replaces the old
    presented fraction; the anchor figures are a cost, not a coverage claim, and
    `synthesis_required` says whether anything was read in more than one piece at all.
    """

    extracted_prose_chars: int          # what extraction recovered
    part_local_chars: int               # of that, what some part carried (union, no double count)
    anchor_chars: int                   # size of the packet, once
    anchor_repeat_chars: int            # what repeating it across parts costs
    parts: int
    synthesis_required: bool

    @property
    def part_local_fraction(self) -> float | None:
        """None when there is no prose, never 1.0, for the reason SectionPresentation gives."""
        if not self.extracted_prose_chars:
            return None
        return self.part_local_chars / self.extracted_prose_chars

    @property
    def anchor_overhead_fraction(self) -> float | None:
        """Repeated anchor characters as a share of the prose. The cost of the design."""
        if not self.extracted_prose_chars:
            return None
        return self.anchor_repeat_chars / self.extracted_prose_chars


class ReadingPlan(NamedTuple):
    parts: list                          # pdf.ReadingPart
    anchor: AnchorPacket
    coverage: ReadingCoverage


def plan(doc: PaperDoc, budget_chars: int) -> ReadingPlan:
    """The whole reading strategy for one paper.

    The anchor packet is charged against the budget before the paper is packed, because
    every part carries it. Not charging it is how a part that measured as fitting arrives
    over length.
    """
    anchor = anchors(doc)
    room = max(400, budget_chars - min(anchor.chars,
                                       int(budget_chars * ANCHOR_BUDGET_FRACTION)))
    parts = pdf.plan_reading(list(doc.sections), room)

    live = [s for s in doc.sections if (s.text or "")]
    total = sum(len(s.text) for s in live)
    covered = 0
    for s in live:
        spans = sorted((a, b) for p in parts for (i, a, b) in p.slices
                       if i == s.section_idx)
        reach = 0
        for a, b in spans:
            if b > reach:
                covered += b - max(a, reach)
                reach = b
    n = len(parts)
    return ReadingPlan(
        parts=parts, anchor=anchor,
        coverage=ReadingCoverage(
            extracted_prose_chars=total,
            part_local_chars=covered,
            anchor_chars=anchor.chars,
            anchor_repeat_chars=anchor.chars * max(0, n - 1),
            parts=n,
            synthesis_required=n > 1,
        ))


# --------------------------------------------------------------------------- #
# Within-lens cross-part synthesis
# --------------------------------------------------------------------------- #
# What the synthesis pass is asked to look for. Named rather than left to the model,
# because an open-ended "find anything else" invites invention, and every one of these is
# a relationship BETWEEN two spans that a part-local reader structurally could not see.
SYNTHESIS_TARGETS = (
    "cross-section contradictions",
    "abstract or conclusion inconsistent with the body",
    "a table disagreeing with the prose that cites it",
    "a method described one way and evaluated another",
    "an appendix result inconsistent with the main text",
    "duplicated concerns that are one concern and should merge",
    "a concern whose significance changes once another section is taken into account",
)


class SynthesisBrief(NamedTuple):
    """The whole input to one lens's cross-part synthesis. Nothing else reaches it.

    Three exclusions make this a synthesis rather than a second opinion. It sees no other
    lens's output, so cross-lens independence is untouched. It sees no hidden reasoning,
    only candidates that already carry a quotation. And it sees no decision, grade or
    outcome, so it cannot be steered by what the harness has concluded so far.
    """

    paper_id: str
    lens: str
    anchor: AnchorPacket
    candidates: list          # dicts: part, section_idx, page, ref, quote, statement
    parts_read: int

    def render(self) -> str:
        rows = []
        for c in self.candidates:
            where = f"part {c.get('part', '?')} · section {c.get('section_idx', '?')}"
            if c.get("page"):
                where += f" · p{c['page']}"
            if c.get("ref"):
                where += f" · {c['ref']}"
            rows.append(f"- [{where}] {c.get('description', '').strip()}\n"
                        f"  quoted: \"{(c.get('quote') or '').strip()}\"")
        asks = "\n".join(f"  - {t}" for t in SYNTHESIS_TARGETS)
        return (f"{self.anchor.render()}\n\n"
                f"## Your own observations across {self.parts_read} parts of this paper\n"
                + ("\n".join(rows) if rows else "- (none)")
                + f"\n\n## What to look for now\n{asks}\n")


def synthesis_brief(paper_id: str, lens: str, anchor: AnchorPacket,
                    per_part_findings: dict[int, list]) -> SynthesisBrief:
    """Assemble one lens's own grounded observations across its own parts.

    `per_part_findings` maps a part number to that part's findings. A finding with no
    evidence quotation is dropped here rather than passed on: the synthesis reasons over
    what can be relocated in the paper, and an unquoted observation is exactly the fluent
    assertion this system refuses everywhere else.
    """
    out = []
    for part in sorted(per_part_findings):
        for f in per_part_findings[part] or []:
            quote = (getattr(f, "evidence_quote", "") or "").strip()
            if not quote:
                continue
            out.append({
                "part": part,
                "section_idx": getattr(f, "section_idx", None),
                "page": getattr(f, "page", None),
                "ref": getattr(f, "evidence_ref", "") or "",
                "quote": quote,
                # `statement` is the finding's own prose; `title` is its short form and
                # is the fallback, because a finding with neither says nothing at all.
                "statement": (getattr(f, "statement", "") or getattr(f, "title", "") or ""),
                "finding_id": getattr(f, "finding_id", "") or "",
            })
    return SynthesisBrief(paper_id=paper_id, lens=lens, anchor=anchor,
                          candidates=out, parts_read=len(per_part_findings))


def render_part(part, anchor: AnchorPacket) -> str:
    """One part's prompt body: the anchors, then this part's own sections.

    The order is deliberate. The anchors come first so a reader meets the paper's claims
    before the span it is being asked to examine, which is the order a referee reads in.
    """
    head = anchor.render()
    body = pdf.render_part(part)
    where = (f"\n\n## The span you are reading now — {part.label}\n"
             f"Sections below are the portion of the paper assigned to this pass. The "
             f"abstract, conclusion and outline above describe the whole paper.\n")
    return f"{head}{where}\n{body}" if head else body


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.reading
    doc = PaperDoc(
        paper_id="p", title="A Paper",
        sections=[
            Section(section_idx=0, title="", page_start=1, text="front matter"),
            Section(section_idx=1, title="Abstract", page_start=1, text="we claim X."),
            Section(section_idx=2, title="Method", page_start=2, text="m" * 5000),
            Section(section_idx=3, title="Results", page_start=4, text="r" * 5000),
            Section(section_idx=4, title="Conclusion", page_start=6, text="we showed Y."),
        ])
    a = anchors(doc)
    assert a.abstract == "we claim X.", a.abstract
    assert a.conclusion == "we showed Y.", a.conclusion
    assert a.title == "A Paper"
    assert len(a.outline) == 5
    assert "Abstract" in a.render() and "Conclusion" in a.render()

    p = plan(doc, 4000)
    assert p.coverage.parts > 1, "this fixture must need more than one part"
    assert p.coverage.part_local_fraction == 1.0, p.coverage
    assert p.coverage.anchor_repeat_chars == a.chars * (p.coverage.parts - 1)
    assert p.coverage.synthesis_required is True
    rendered = render_part(p.parts[0], a)
    assert "we claim X." in rendered and "we showed Y." in rendered
    assert "part 1 of" in rendered

    one = plan(PaperDoc(paper_id="q", title="T",
                        sections=[Section(section_idx=0, title="Abstract", text="a")]), 70_000)
    assert one.coverage.parts == 1 and one.coverage.synthesis_required is False
    assert one.coverage.anchor_repeat_chars == 0

    empty = plan(PaperDoc(paper_id="e"), 70_000)
    assert empty.coverage.part_local_fraction is None, "no prose is not full coverage"
    print("harness.reading self-check ok")
