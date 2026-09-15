"""Reading the WHOLE paper, and proving that is what happens.

`render_sections` divides one character budget equally across every section and
hard-slices each one, so a long paper is cut before any reader sees a word of it. Measured
over the evaluated corpus, readers were shown between 34% and 84% of the extracted prose
while the review's scope line said four lenses read the paper. The largest paper carried
106k characters and the reader saw 34k of them.

`plan_reading` replaces the cut with a plan: the fewest prompts that show all of it. The
property these tests exist for is COVERAGE, and it is reconstructed rather than trusted —
every character of every section must appear in some part. A test that merely checked the
part count would pass on a planner that silently dropped a section.

Raising the budget instead was rejected, and one test pins why: a constant large enough for
the longest paper in one corpus is not a property of papers, and its failure mode is
silent.
"""
from __future__ import annotations

import pytest

from harness import pdf
from harness.artifacts import Section


def _sec(idx: int, text: str, title: str = "") -> Section:
    return Section(section_idx=idx, title=title or f"S{idx}", page_start=idx, text=text)


def _covered(parts, section: Section) -> bool:
    """Is every character of `section` visible in some part?

    Computed from the recorded offsets, not by searching for a slice's text. Searching is
    what a first version of this helper did, and it reported a hole in a correct plan the
    moment a fixture's text repeated: `str.find` returns the earliest match, so every
    slice of "yyyy..." resolves to offset 0 and the union collapses. A coverage check that
    degrades on repetitive input is not a coverage check.

    Overlap is allowed; a gap is not.
    """
    spans = sorted((a, b) for p in parts for (idx, a, b) in p.slices
                   if idx == section.section_idx)
    if not spans:
        return not section.text
    reach = 0
    for a, b in spans:
        if a > reach:
            return False                      # a hole between this span and the last
        reach = max(reach, b)
    return reach >= len(section.text)


def _slices_say_what_the_text_is(parts, sections) -> bool:
    """The offsets must actually describe the text each part carries."""
    whole = {s.section_idx: s.text for s in sections}
    for p in parts:
        assert len(p.slices) == len(p.sections)
        for sec, (idx, a, b) in zip(p.sections, p.slices):
            assert sec.section_idx == idx
            if whole[idx][a:b] != sec.text:
                return False
    return True


# --------------------------------------------------------------------------- #
# The guarantee
# --------------------------------------------------------------------------- #
def test_a_paper_that_fits_is_one_part_and_nothing_is_cut():
    secs = [_sec(0, "a" * 1000), _sec(1, "b" * 1000)]
    parts = pdf.plan_reading(secs, 70_000)
    assert len(parts) == 1
    assert parts[0].number == 1 and parts[0].total == 1 and parts[0].split_sections == 0
    assert parts[0].chars == 2000
    rendered = pdf.render_part(parts[0])
    assert "…[truncated]" not in rendered
    assert "a" * 1000 in rendered and "b" * 1000 in rendered


def test_every_character_of_every_section_reaches_some_part():
    """The property. Reconstructed, not asserted by construction."""
    secs = [_sec(0, "x" * 900), _sec(1, "y" * 4100), _sec(2, "z" * 300),
            _sec(3, "w" * 5000), _sec(4, "v" * 50)]
    parts = pdf.plan_reading(secs, 2000)
    assert len(parts) > 1, "this fixture must actually need splitting"
    for s in secs:
        assert _covered(parts, s), f"section {s.section_idx} has a hole"
    assert _slices_say_what_the_text_is(parts, secs)


def test_coverage_holds_on_repetitive_text_that_defeats_substring_search():
    """The degenerate case that broke the first version of the coverage helper.

    A section of one repeated character is not a real paper, and it is exactly the input
    that makes a search-based check pass or fail for reasons unrelated to the planner.
    """
    secs = [_sec(0, "y" * 41_000)]
    parts = pdf.plan_reading(secs, 2000)
    assert _covered(parts, secs[0])
    assert _slices_say_what_the_text_is(parts, secs)


def test_no_part_exceeds_the_budget():
    secs = [_sec(i, "q" * 700) for i in range(20)]
    parts = pdf.plan_reading(secs, 2000)
    for p in parts:
        assert p.chars <= 2000, (p.number, p.chars)
    assert sum(p.chars for p in parts) >= sum(len(s.text) for s in secs)


def test_document_order_is_preserved_across_parts():
    """A reader that meets Limitations before Methods is reading a different document."""
    secs = [_sec(i, chr(97 + i) * 800) for i in range(10)]
    parts = pdf.plan_reading(secs, 2000)
    seen = [s.section_idx for p in parts for s in p.sections]
    assert seen == sorted(seen), seen


def test_a_section_larger_than_the_budget_is_sliced_with_an_overlap():
    """A concern whose sentence straddles a cut must stay quotable by one of the parts."""
    body = " ".join(f"word{i:05d}" for i in range(4000))        # ~40k chars, spaces
    parts = pdf.plan_reading([_sec(0, body)], 5000)
    assert len(parts) > 1
    assert all(p.split_sections == 1 for p in parts)
    assert _covered(parts, _sec(0, body))
    texts = [s.text for p in parts for s in p.sections]
    joined = "".join(texts)
    assert len(joined) > len(body), "consecutive parts must share an overlap"


def test_a_cut_lands_between_words_rather_than_inside_one():
    body = " ".join(f"token{i:04d}" for i in range(2000))
    parts = pdf.plan_reading([_sec(0, body)], 3000)
    for p in parts[:-1]:
        assert p.sections[-1].text.endswith(tuple("0123456789")), (
            "a slice should end on a completed token, not mid-word")


def test_parts_are_numbered_so_a_reader_can_be_told_where_it_is():
    parts = pdf.plan_reading([_sec(i, "m" * 900) for i in range(6)], 2000)
    assert [p.number for p in parts] == list(range(1, len(parts) + 1))
    assert {p.total for p in parts} == {len(parts)}
    assert parts[0].label == f"part 1 of {len(parts)}"


def test_an_empty_or_textless_paper_yields_no_parts():
    assert pdf.plan_reading([], 70_000) == []
    assert pdf.plan_reading([_sec(0, ""), _sec(1, "")], 70_000) == []


def test_a_textless_section_is_dropped_and_the_rest_still_covered():
    secs = [_sec(0, "a" * 500), _sec(1, ""), _sec(2, "c" * 500)]
    parts = pdf.plan_reading(secs, 70_000)
    idxs = [s.section_idx for p in parts for s in p.sections]
    assert idxs == [0, 2]


def test_render_part_never_truncates_and_keeps_the_heading_and_page():
    part = pdf.plan_reading([_sec(3, "body text here", title="Method")], 70_000)[0]
    out = pdf.render_part(part)
    assert out.startswith("## Method  [p3]")
    assert "body text here" in out
    assert "truncated" not in out


# --------------------------------------------------------------------------- #
# Why this rather than a bigger constant
# --------------------------------------------------------------------------- #
def test_the_planner_is_correct_at_a_length_no_constant_would_have_covered():
    """The reason the budget was not simply raised.

    A 60-page paper at this harness's own extraction ceiling carries far more prose than
    any single prompt a corpus happened to need. Packing handles it; a constant tuned to
    one corpus does not, and fails silently when it stops being enough.
    """
    secs = [_sec(i, "p" * 40_000) for i in range(12)]          # 480k chars
    parts = pdf.plan_reading(secs, 70_000)
    assert len(parts) >= 7
    for s in secs:
        assert _covered(parts, s)
    assert all(p.chars <= 70_000 for p in parts)


def test_the_old_renderer_still_truncates_which_is_why_it_is_not_used_for_parts():
    """Pins the behaviour being replaced, so a future edit cannot quietly reintroduce it."""
    secs = [_sec(0, "a" * 5000), _sec(1, "b" * 5000)]
    out = pdf.render_sections(secs, 2000)
    assert "…[truncated]" in out
    pres = pdf.section_presentation(secs, 2000)
    assert pres.fraction is not None and pres.fraction < 1.0
    assert pres.sections_truncated == 2


# --------------------------------------------------------------------------- #
# On real corpus shapes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("total_chars,n_sections,expected_max_parts", [
    (50_163, 22, 1),      # 0c06a98d7c818f6f
    (105_614, 46, 2),     # sanchez24a-icml, the largest in the corpus
    (99_953, 29, 2),      # acl, the worst previously-presented fraction at 0.34
])
def test_corpus_sized_papers_need_at_most_two_passes(total_chars, n_sections,
                                                     expected_max_parts):
    """Cost is the objection to chunking, so it is measured rather than assumed."""
    per = total_chars // n_sections
    secs = [_sec(i, "s" * per) for i in range(n_sections)]
    parts = pdf.plan_reading(secs, 70_000)
    assert len(parts) <= expected_max_parts, len(parts)
    for s in secs:
        assert _covered(parts, s)
