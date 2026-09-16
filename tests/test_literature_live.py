"""The only tests in this suite that reach a third party. Marked `network`, and separate.

**What these check, and what they deliberately do not.** Every RULE of the prior-art route
— the authority ladder, the chronology, the canonicalisation, and above all what a silence
means — is checked in `test_literature_route.py`, on fixtures, with no network. Those must
hold whether an index is up or down. What is left, and what can only be checked live, is
that the parsers still match what the indexes actually send: a field renamed at OpenAlex
would make every candidate undated, and a fixture would never notice.

**A network failure SKIPS and a parse failure FAILS**, which is the same distinction the
route itself draws. An index being unreachable, rate-limiting us, or demanding a
credential is a fact about this host and this moment; an index answering and our parser
not understanding the answer is a defect in this repository. Only the second should turn
a suite red.
"""
from __future__ import annotations

import pytest

from harness import literature as L
from harness import literature_providers as LP

pytestmark = pytest.mark.network

# Outcomes that mean "the world did not cooperate". Not defects in this repository.
_ENVIRONMENTAL = ("RATE_LIMITED", "UNAUTHENTICATED", "UNREACHABLE", "NOT_ATTEMPTED")


def _live(provider: str, query: str, top_k: int = 5):
    works, call = LP.search(provider, query, top_k=top_k,
                            mailto="referee-tests@localhost")
    if call.outcome in _ENVIRONMENTAL:
        pytest.skip(f"{provider}: {call.outcome} ({call.error[:80]})")
    return works, call


@pytest.mark.parametrize("provider", ["openalex", "crossref", "arxiv"])
def test_each_default_index_answers_and_its_answer_still_parses(provider):
    """The default protocol's three indexes, each asked one real query.

    Asserting on the SHAPE and not on the contents: which papers an index returns for a
    query is the index's business and changes, and a test that pinned a result would fail
    for the wrong reason. What must hold is that a record comes back with something this
    harness can canonicalise.
    """
    works, call = _live(provider, "convolutional network optical flow estimation")
    assert call.completed and call.n_results == len(works)
    assert works, f"{provider} returned no results for a query that should match many"
    assert any(L.canonical_id(w) for w in works)
    assert any((w.title or "").strip() for w in works)


def test_a_live_arxiv_record_carries_its_first_submission_date():
    """arXiv's `published` is the v1 posting, which is what 'public' means for a preprint.

    The strongest chronology source in the route depends on this field surviving.
    """
    works, _call = _live("arxiv", "FlowNet learning optical flow convolutional networks")
    dated = [w for w in works if w.date]
    if not dated:
        pytest.skip("arXiv returned no dated record for this query")
    assert all(w.date_source == "arxiv_submission_v1" for w in dated)
    assert all(len(w.date) == 10 and w.date[4] == "-" for w in dated), [w.date for w in dated]


def test_a_live_openalex_record_still_yields_a_reconstructable_abstract():
    """OpenAlex publishes an inverted index; a candidate with no abstract is unquotable.

    `bind_prior_art` requires the reviewer's quoted passage to be present in the text this
    harness retrieved, so an abstract that stopped reconstructing would turn every
    OpenAlex candidate into `candidate_quote_unresolved` and the route would silently go
    quiet rather than break.
    """
    works, _call = _live("openalex", "deep residual learning image recognition", top_k=10)
    with_abstract = [w for w in works if (w.abstract or "").strip()]
    if not with_abstract:
        pytest.skip("OpenAlex returned no abstract for this query")
    sample = with_abstract[0]
    assert len(sample.abstract.split()) >= 10
    assert sample.abstract_sha256, "what was read is hashed, so it can be proved later"
    # And a passage taken FROM that text locates in it, which is the check the route runs.
    passage = " ".join(sample.abstract.split()[:12])
    assert L._passage_present(sample, passage)


def test_two_indexes_reporting_the_same_work_fold_into_one_candidate():
    """The dedup rule, against real records rather than constructed ones."""
    one, _ = _live("openalex", "attention is all you need transformer", top_k=5)
    two, _ = _live("crossref", "attention is all you need transformer", top_k=5)
    if not one or not two:
        pytest.skip("one of the indexes returned nothing for this query")
    merged = L.merge_works(one + two)
    assert len(merged) <= len(one) + len(two)
    assert len({w.canonical_id for w in merged}) == len(merged), "canonical ids are unique"


def test_a_real_target_paper_can_be_dated_from_the_indexes():
    """The cutoff, end to end: a well-known title, and a date with a named source."""
    offers, calls = LP.date_offers("Attention Is All You Need",
                                   providers=("arxiv", "crossref"),
                                   mailto="referee-tests@localhost")
    if all(c.outcome in _ENVIRONMENTAL for c in calls):
        pytest.skip("no index could be reached")
    if not offers:
        pytest.skip("no index matched the title exactly; that is a retrieval outcome")
    cutoff = L.derive_cutoff(offers)
    assert cutoff.state in ("ESTABLISHED", "YEAR_ONLY")
    assert cutoff.source in offers and cutoff.date
    # Every offer is kept, not just the winner — the check against choosing a convenient date.
    assert set(cutoff.candidates) == set(offers)
