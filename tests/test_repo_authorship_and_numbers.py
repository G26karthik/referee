"""Regressions for two defects found auditing real ICML papers.

1. A cited repository was recorded as the paper's own (`harness/repo.py`). The paper
   under audit advertised no code; its only GitHub URL was the bibliography entry for a
   third-party model it evaluated. That URL became `repo_url`, which is the URL S3a
   clones and the static auditor reads — so a stranger's codebase would have been
   audited and reported against these authors, and with the execution gate open it would
   have been run.

2. `parse_cell_number` truncated scientific notation (`harness/local_exec.py`). A cell
   reading "1.23e4 ± 3.29e2" parsed as 1.23, four orders of magnitude low, and that
   value is what `reconcile` compares against a reproduced metric.
"""
from __future__ import annotations

import pytest

from harness.artifacts import PaperDoc, Section
from harness.local_exec import parse_cell_number
from harness.repo import find_repo_urls, official_repo_url


def _doc(*sections: tuple[str, str]) -> PaperDoc:
    return PaperDoc(paper_id="t", title="T",
                    sections=[Section(section_idx=i, title=t, text=x)
                              for i, (t, x) in enumerate(sections)])


# --------------------------------------------------------------------------- #
# 1. repository authorship
# --------------------------------------------------------------------------- #
CITED = ("References",
         "Ben Wang and Aran Komatsuzaki. GPT-J-6B: A 6 Billion Parameter Autoregressive "
         "Language Model. https://github.com/kingoflolz/mesh-transformer-jax, May 2021.")
OWN = ("Abstract",
       "Our code and models are publicly available at https://github.com/ROIM1998/APT.")


def test_a_references_only_url_is_never_the_papers_own_repo():
    """The exact failure: a paper that ships no code must not acquire someone else's."""
    doc = _doc(("Introduction", "We evaluate GPT-J on several benchmarks."), CITED)
    assert official_repo_url(doc) == ""


def test_the_cited_url_is_still_reported_as_a_candidate():
    """Suppressing it from `repo_url` must not hide it from the diagnostic list."""
    doc = _doc(("Introduction", "We evaluate GPT-J."), CITED)
    assert find_repo_urls(doc) == ["https://github.com/kingoflolz/mesh-transformer-jax"]


def test_an_advertised_repo_is_still_found():
    assert official_repo_url(_doc(OWN)) == "https://github.com/ROIM1998/APT"


def test_an_advertised_repo_wins_over_cited_ones_in_the_same_paper():
    doc = _doc(OWN, ("References", "Stanford alpaca. https://github.com/tatsu-lab/stanford_alpaca, 2023. "
                     "LLM-Pruner. https://github.com/horseee/LLM-Pruner, 2023."))
    assert official_repo_url(doc) == "https://github.com/ROIM1998/APT"
    assert len(find_repo_urls(doc)) == 3, "all three remain candidates"


def test_a_url_with_no_availability_cue_anywhere_is_refused():
    """A bare link in the body is not a claim of authorship."""
    doc = _doc(("Method", "We build on the implementation at https://github.com/someone/theirs."))
    assert official_repo_url(doc) == ""


def test_a_cue_inside_the_reference_section_does_not_rescue_a_citation():
    """Reference entries sometimes read 'available at'; that is still a citation."""
    doc = _doc(("References", "Some Author. A Model. Code available at "
                              "https://github.com/other/model, 2021."))
    assert official_repo_url(doc) == ""


def test_a_paper_with_no_urls_at_all_yields_nothing():
    doc = _doc(("Abstract", "We release nothing."))
    assert official_repo_url(doc) == "" and find_repo_urls(doc) == []


# --------------------------------------------------------------------------- #
# 2. numeric parsing of a cited cell
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cell,expected", [
    ("1.23e4±3.29e2", 12300.0),          # the regression: was 1.23
    ("1.23e4 ± 3.29e2", 12300.0),
    ("9.14e3±8.38e2", 9140.0),
    ("-1.5E-3", -0.0015),
    ("12.196 ± 0.207", 12.196),          # plain decimals unaffected
    # Three columns extraction ran together. This used to read 253.6 — the FIRST number
    # of three — and that value was reconciled against a measured metric and printed to a
    # reviewer as the quantity the paper states. A cell this harness cannot segment is a
    # cell whose quantity it does not know, so it is refused. See `parse_cell_number`.
    ("253.6% 114.8% 74.2%", None),
    ("52.7", 52.7),
    ("1,234.5", 1234.5),
    ("", None),
    # A mean fused with its standard deviation by extraction. `acl`'s Table 2 is written
    # entirely in this shape, and `60.357.47` read as 60.357 is the number the manuscript
    # printed as a paper's own claim.
    ("60.357.47", None),
    ("65.4132.53", None),
    # Two adjacent cells run together. The leading value is recoverable and must still be
    # refused: the string reports two quantities, not one.
    ("1.23e4±3.29e2 9.14e3±8.38e2", None),
    ("12.5 [11.0, 14.0]", 12.5),         # a stated interval reports one quantity
])
def test_parse_cell_number(cell: str, expected):
    assert parse_cell_number(cell) == expected


def test_a_cell_reporting_more_than_one_quantity_is_refused_rather_than_guessed():
    """The cell-side half of the rule `claims.parse_quantity` has always applied.

    `parse_quantity` refuses a span that reports no single number ("anything else yields
    nothing"). The cell path had no such rule and took the leading run of digits, which is
    the positional coincidence invariant 18 forbids on the output side, applied to the
    paper's side of the same comparison. Every execution this harness ever performed
    reached the reconciler through it.
    """
    for merged in ("60.357.47", "253.6% 114.8% 74.2%", "1.23e4±3.29e2 9.14e3±8.38e2"):
        assert parse_cell_number(merged) is None, merged
    # and the refusal is narrow: single quantities, and a quantity stated with its own
    # uncertainty, still parse.
    for ok, want in (("60.35", 60.35), ("61.4 ± 0.3", 61.4), ("1.01e4±6.31e2", 10100.0),
                     ("95.3%", 95.3), ("2,900 test cases", 2900.0)):
        assert parse_cell_number(ok) == want, ok


def test_the_exponent_is_not_dropped_from_a_reconciled_cell():
    """Stated as its own test because the failure is silent and off by 10,000x."""
    assert parse_cell_number("1.23e4±3.29e2") != 1.23


# --------------------------------------------------------------------------- #
# 3. the acquisition gate itself
# --------------------------------------------------------------------------- #
def test_acquire_never_falls_back_to_the_unqualified_candidate_list(tmp_path):
    """The clone decision must honour authorship, not just the ingest-time field.

    The first version of this fix set `repo_url` at ingest and stopped there. `acquire`
    still fell back to `repo_urls`, then to a fresh unfiltered scan, so a cited
    repository was still selected for cloning. Only the URL being unreachable prevented
    a stranger's code from being fetched and audited against these authors.
    """
    from harness.config import Config
    from harness.repo import acquire

    doc = _doc(("Introduction", "We evaluate GPT-J."), CITED)
    doc.repo_urls = find_repo_urls(doc)          # the citation IS a candidate
    doc.repo_url = official_repo_url(doc)        # but it is not the paper's own
    assert doc.repo_urls and not doc.repo_url

    acq = acquire(Config(projects_dir=tmp_path), tmp_path, "t", doc)
    assert acq.status == "unavailable"
    assert acq.url == "", "no URL may be selected for cloning"
    assert "none can be attributed to these authors" in acq.reason
    assert "kingoflolz" in acq.reason, "the rejected candidate is still named, not hidden"


def test_acquire_applies_the_rule_to_a_document_ingested_before_it_existed(tmp_path):
    """A stale doc.json carrying a citation in `repo_url` must be re-judged, not trusted."""
    from harness.config import Config
    from harness.repo import acquire

    doc = _doc(("Introduction", "We evaluate GPT-J."), CITED)
    doc.repo_urls = find_repo_urls(doc)
    doc.repo_url = ""                            # as a corrected re-ingest would leave it
    assert acquire(Config(projects_dir=tmp_path), tmp_path, "t", doc).status == "unavailable"


def test_acquire_still_selects_a_genuinely_advertised_repo(tmp_path):
    from harness.config import Config
    from harness.repo import acquire

    doc = _doc(OWN)
    doc.repo_url = official_repo_url(doc)
    acq = acquire(Config(projects_dir=tmp_path, allow_network=False), tmp_path, "t", doc)
    assert acq.url == "https://github.com/ROIM1998/APT"
    assert acq.status == "blocked", "network gate shut, but the right URL was selected"
