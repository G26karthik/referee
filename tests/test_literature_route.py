"""The prior-art route, and the one asymmetry it exists to enforce.

    finding prior art may support a novelty concern;
    failing to find prior art does NOT establish novelty.

Every test below is PURE: no network, no subprocess, no model. The candidate sets are
fixtures and the provider responses are recorded bytes, because the rules this route
turns on — chronology, canonicalisation, endpoint verification, what a silence means —
must hold identically whether an index is up or down. The tests that actually reach an
index are in `test_literature_live.py` and are marked `network`.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from harness import literature as L
from harness import literature_driver as LD
from harness import literature_providers as LP
from harness import state, taxonomy
from harness.artifacts import (LITERATURE_AUTHORITY, NOVELTY_ESTABLISHING_AUTHORITIES,
                               PaperDoc, ProviderCall, Section, TARGET_DISPOSITIONS,
                               LiteratureSearch, WorkIdentity)
from harness.config import Config
from harness.prompts import literature as LPR
from harness.stages import literature as LS


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------
def doc_with(abstract: str, *, title: str = "FlowNet: Learning Optical Flow",
             references: str = "[1] Somebody. Something else. 2011.") -> PaperDoc:
    return PaperDoc(paper_id="p", title=title, body_end_section_idx=2, sections=[
        Section(section_idx=0, title="Abstract", text=abstract),
        Section(section_idx=1, title="1 Introduction",
                text="Optical flow estimation is a classical problem."),
        Section(section_idx=2, title="References", text=references),
    ])


ABSTRACT = ("We introduce FlowNet for optical flow estimation. "
            "This is the first method to learn optical flow end to end from synthetic "
            "data. It reaches 2.7 average endpoint error.")

EARLIER_ABSTRACT = "We learn optical flow end to end with a convolutional network."


def earlier(**kw) -> WorkIdentity:
    work = WorkIdentity(doi="10.9/earlier", title="End-to-end optical flow",
                        date="2013-03-01", date_source="arxiv_submission_v1",
                        abstract=EARLIER_ABSTRACT, **kw)
    work.canonical_id = L.canonical_id(work)
    return work


CUTOFF = L.derive_cutoff({"arxiv_submission_v1": "2015-04-26"})


def priority_claim(doc: PaperDoc):
    return next(c for c in L.novelty_claims(doc) if c.claim_kind == "NOVELTY_MARKER")


# --------------------------------------------------------------------------------------
# THE ASYMMETRY — the reason this route exists in this shape
# --------------------------------------------------------------------------------------
def test_no_vocabulary_value_anywhere_means_novel():
    """The rule is encoded, not asserted: there is nothing to relax later.

    A future contributor cannot make a clean search establish novelty by editing a
    threshold, because there is no value to set. `NOVELTY_ESTABLISHING_AUTHORITIES` is
    empty, no authority or disposition spells it, and the membership test that asks the
    question can never succeed.
    """
    assert NOVELTY_ESTABLISHING_AUTHORITIES == ()
    for value in LITERATURE_AUTHORITY + TARGET_DISPOSITIONS:
        assert "NOVEL" not in value.upper(), value
    assert "NOVELTY_VERIFIED" not in TARGET_DISPOSITIONS
    fact = L.bind_prior_art(doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)),
                            earlier(), relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.supports_concern and fact.establishes_novelty is False


def test_a_completed_empty_search_does_not_become_novelty_established():
    """§15, and the single most important behaviour in the route.

    Twenty results with no match means no match in twenty results. On the evidence axis
    it is `BOUNDED_SEARCH_NO_MATCH`, which is not in `EVIDENCE_ABOUT_THE_PAPER`; on the
    resolution axis it is UNRESOLVED; and it does not discharge the route.
    """
    search = LiteratureSearch(paper_id="p", cutoff=CUTOFF, provider_calls=[
        ProviderCall(provider="openalex", query="q", outcome="NO_RESULTS"),
        ProviderCall(provider="crossref", query="q", outcome="COMPLETED", n_results=20)])
    disposition, why = L.outcome_disposition(search)
    assert disposition == "SEARCH_COMPLETED_NO_MATCH_FOUND"
    assert "NOT EVIDENCE OF NOVELTY" in why
    assert L.discharge(search) == (False, why)
    assert taxonomy.evidence_state(disposition) == "BOUNDED_SEARCH_NO_MATCH"
    assert taxonomy.resolution_state("BOUNDED_SEARCH_NO_MATCH") == "UNRESOLVED"
    assert not taxonomy.concerns_the_paper("BOUNDED_SEARCH_NO_MATCH")


def test_the_search_budget_appears_in_the_record():
    """§15: "no match" is a statement with a denominator, or it is not a statement."""
    search = LiteratureSearch(
        paper_id="p", cutoff=CUTOFF, protocol=LS.protocol_for(Config()),
        claims_searched=L.novelty_claims(doc_with(ABSTRACT)), raw_results=37,
        provider_calls=[ProviderCall(provider="openalex", query="q", outcome="COMPLETED",
                                     n_results=20)],
        works=[earlier()])
    summary = L.summarise(search)
    for key in ("queries_executed", "providers_completed", "raw_results",
                "deduplicated_works", "pre_cutoff_candidates", "protocol_completed",
                "endpoint_verified_concerns", "structurally_bound"):
        assert key in summary, key
    assert summary["raw_results"] == 37 and summary["deduplicated_works"] == 1
    assert summary["pre_cutoff_candidates"] == 1
    assert search.protocol.top_k > 0 and search.protocol.providers_required


# --------------------------------------------------------------------------------------
# DEDUPLICATION — one paper is not four papers, and two papers are not one
# --------------------------------------------------------------------------------------
def test_exact_doi_deduplication():
    folded = L.merge_works([
        WorkIdentity(doi="10.1/A", title="Deep Nets", date="2021-05-01",
                     providers=["crossref"]),
        WorkIdentity(doi="https://doi.org/10.1/a", title="Deep Nets", date="2021-05-01",
                     providers=["openalex"])])
    assert len(folded) == 1
    assert folded[0].canonical_id == "doi:10.1/a"
    assert sorted(folded[0].providers) == ["crossref", "openalex"]


def test_arxiv_and_conference_versions_are_one_work_with_the_earliest_date():
    """Prior art is about when a work became PUBLIC, not when a publisher printed it."""
    folded = L.merge_works([
        WorkIdentity(doi="10.1/a", title="Deep Nets", date="2021-05-01",
                     providers=["crossref"]),
        WorkIdentity(doi="10.1/a", arxiv_id="arXiv:2101.00001v3", title="Deep Nets",
                     date="2021-01-04", date_source="arxiv_submission_v1",
                     providers=["openalex"]),
        WorkIdentity(arxiv_id="2101.00001", title="Deep Nets", providers=["arxiv"])])
    assert len(folded) == 2, [w.canonical_id for w in folded]
    assert folded[0].date == "2021-01-04"
    assert folded[0].date_source == "arxiv_submission_v1"
    assert "arxiv:2101.00001" in folded[0].aliases
    assert L.normalize_arxiv("arXiv:2101.00001v3") == "2101.00001"


def test_same_title_distinct_papers_are_not_merged():
    """The title fallback is CONJUNCTIVE. Similar titles are common; identity is not."""
    folded = L.merge_works([
        WorkIdentity(title="Attention Is All You Need", authors=["A. One"], year=2017),
        WorkIdentity(title="Attention Is All You Need", authors=["B. Two"], year=2021),
        WorkIdentity(doi="10.2/x", title="Attention Is All You Need", authors=["A. One"],
                     year=2017)])
    assert len(folded) == 3, [w.canonical_id for w in folded]


def test_a_record_with_no_identifier_is_not_a_citable_work():
    assert L.citable(earlier())
    assert not L.citable(WorkIdentity(title="Something", year=2010))
    assert not L.citable(None)


# --------------------------------------------------------------------------------------
# CHRONOLOGY — §12, the half that must be deterministic
# --------------------------------------------------------------------------------------
def test_candidate_earlier_than_target_is_eligible():
    assert L.chronology(CUTOFF, earlier())[0] == "PREDATES_CUTOFF"


def test_candidate_later_than_target_cannot_support_a_prior_art_concern():
    """A later work may be related. It is prior art for nothing, whatever the reading."""
    later = earlier()
    later.date, later.doi = "2020-01-01", "10.9/later"
    later.canonical_id = L.canonical_id(later)
    assert L.chronology(CUTOFF, later)[0] == "POSTDATES_CUTOFF"
    fact = L.bind_prior_art(doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)), later,
                            relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.refusal == "candidate_not_earlier"
    assert fact.authority == "NONE" and not fact.supports_concern


def test_same_year_earlier_month_orders_when_both_dates_are_days():
    fine = L.derive_cutoff({"arxiv_submission_v1": "2024-06-01"})
    early = WorkIdentity(doi="10.1/e", title="t", date="2024-01-02", year=2024)
    late = WorkIdentity(doi="10.1/l", title="t", date="2024-11-02", year=2024)
    assert L.chronology(fine, early)[0] == "PREDATES_CUTOFF"
    assert L.chronology(fine, late)[0] == "POSTDATES_CUTOFF"


def test_a_year_only_cutoff_cannot_order_a_same_year_candidate():
    """A proceedings year does not put one 2024 paper before another 2024 paper."""
    year_only = L.derive_cutoff({"proceedings_year": "2024"})
    assert year_only.state == "YEAR_ONLY"
    same = WorkIdentity(doi="10.1/s", title="t", date="2024-01-02", year=2024)
    assert L.chronology(year_only, same)[0] == "CONTEMPORANEOUS_UNRESOLVED"
    older = WorkIdentity(doi="10.1/o", title="t", date="2023-12-31", year=2023)
    assert L.chronology(year_only, older)[0] == "PREDATES_CUTOFF"


def test_a_candidate_with_no_date_is_not_probably_earlier():
    fact = L.bind_prior_art(
        doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)),
        WorkIdentity(doi="10.9/undated", title="t", abstract=EARLIER_ABSTRACT),
        relation="LIKELY_DIRECT_PREDECESSOR", candidate_quote=EARLIER_ABSTRACT,
        cutoff=CUTOFF)
    assert fact.refusal == "candidate_date_unknown"


def test_a_missing_target_cutoff_blocks_every_chronological_claim():
    fact = L.bind_prior_art(doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)),
                            earlier(), relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT,
                            cutoff=L.derive_cutoff({}))
    assert fact.refusal == "target_cutoff_unknown"
    assert fact.chronology == "TARGET_CUTOFF_UNKNOWN"


def test_the_cutoff_records_every_offer_not_only_the_one_it_chose():
    """"We chose the date most useful to the concern" has to be visible if it happens."""
    cutoff = L.derive_cutoff({"arxiv_submission_v1": "2015-04-26",
                              "crossref_published": "2015-06-01",
                              "proceedings_year": "2015"})
    assert cutoff.source == "arxiv_submission_v1" and cutoff.state == "ESTABLISHED"
    assert cutoff.candidates["crossref_published"] == "2015-06-01"
    assert cutoff.candidates["proceedings_year"] == "2015"


def test_inconsistent_provider_dates_resolve_by_a_fixed_precedence_not_by_convenience():
    assert L.derive_cutoff({"crossref_published": "2020-01-01",
                            "openalex_publication_date": "2019-01-01"}).date == "2020-01-01"
    assert L.derive_cutoff({"openalex_publication_date": "2019-01-01"}).date == "2019-01-01"


# --------------------------------------------------------------------------------------
# ENDPOINTS — §11
# --------------------------------------------------------------------------------------
def test_a_target_quote_that_does_not_resolve_is_refused():
    doc = doc_with(ABSTRACT)
    claim = priority_claim(doc)
    claim.quote = "We invented a sentence that is not in this paper at all, anywhere."
    fact = L.bind_prior_art(doc, claim, earlier(), relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.refusal == "target_claim_unaddressed"


def test_a_sentence_claiming_no_novelty_cannot_carry_a_prior_art_concern():
    doc = doc_with(ABSTRACT)
    claim = priority_claim(doc)
    claim.quote = "It reaches 2.7 average endpoint error."
    claim.ref = "does-not-matter"
    fact = L.bind_prior_art(doc, claim, earlier(), relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.refusal in ("target_claim_unaddressed", "target_claim_not_a_novelty_claim")
    assert not L.is_novelty_claim("It reaches 2.7 average endpoint error.")


def test_a_candidate_quote_that_is_not_in_the_retrieved_text_is_refused():
    """A search-engine snippet is not the work, and neither is a recollection of it."""
    fact = L.bind_prior_art(doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)),
                            earlier(), relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote="We solve an entirely different problem here.",
                            cutoff=CUTOFF)
    assert fact.refusal == "candidate_quote_unresolved"
    assert fact.candidate_quote_located is False


def test_a_candidate_with_no_identifier_is_refused_even_with_a_perfect_quote():
    nameless = WorkIdentity(title="End-to-end optical flow", date="2013-03-01",
                            abstract=EARLIER_ABSTRACT)
    fact = L.bind_prior_art(doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)),
                            nameless, relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.refusal == "candidate_identity_unestablished"


def test_metadata_only_candidates_are_refused_rather_than_judged_on_their_titles():
    bare = earlier()
    bare.abstract = ""
    fact = L.bind_prior_art(doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)), bare,
                            relation="LIKELY_DIRECT_PREDECESSOR", candidate_quote="",
                            cutoff=CUTOFF)
    assert fact.refusal == "insufficient_candidate_evidence"


def test_a_reviewer_answer_that_raises_no_prior_art_question_is_recorded_not_promoted():
    """RELATED_BUT_MATERIALLY_DIFFERENT is a real answer and the commonest right one."""
    fact = L.bind_prior_art(doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)),
                            earlier(), relation="RELATED_BUT_MATERIALLY_DIFFERENT",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.refusal == "relation_not_concerning"
    assert fact.authority == "NONE" and fact.chronology == "PREDATES_CUTOFF"


def test_a_paper_is_not_prior_art_for_itself():
    """THE DEFECT THE FIRST LIVE RUN FOUND, on the corpus's only endpoint-verified concern.

    `acl`'s own arXiv preprint came back as a LIKELY_DIRECT_PREDECESSOR with every endpoint
    verified — same title, same benchmark name, same 58 topics and 12 domains — because a
    preprint really is earlier than its proceedings version. The reviewer even said so in
    its note. Reported as prior art it would have been an accusation about the authors.
    """
    doc = doc_with(ABSTRACT)
    doc.sections[0].text = ("arXiv:1504.06852 " + ABSTRACT)
    doc.sections[0].page_start = 1
    preprint = WorkIdentity(arxiv_id="1504.06852v1", title="FlowNet",
                            date="2015-04-26", abstract=EARLIER_ABSTRACT)
    preprint.canonical_id = L.canonical_id(preprint)
    preprint.aliases = [preprint.canonical_id]
    assert L.own_arxiv_id(doc) == "1504.06852"
    assert L.same_work(doc, preprint)
    fact = L.bind_prior_art(doc, priority_claim(doc), preprint,
                            relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT,
                            cutoff=L.derive_cutoff({"proceedings_year": "2016"}))
    assert fact.refusal == "candidate_is_the_target_itself"
    assert fact.authority == "NONE" and not fact.supports_concern


def test_the_same_title_is_the_same_work_even_without_an_identifier():
    doc = doc_with(ABSTRACT, title="FlowNet: Learning Optical Flow With Convolutional Nets")
    twin = WorkIdentity(doi="10.9/twin", date="2014-01-01",
                        title="FlowNet: Learning Optical Flow With Convolutional Nets")
    twin.canonical_id = L.canonical_id(twin)
    assert L.same_work(doc, twin)


def test_an_undecidable_self_match_is_refused_out_loud_rather_than_reported():
    """No trustworthy title, and the candidate's title carries the name the paper coins.

    Either the paper's own earlier version or somebody else's work of the same name, and
    nothing here separates them. Refusing is the conservative direction: reporting it as
    prior art is an accusation, and the refusal names what a human should glance at.
    """
    doc = doc_with("We introduce FinChain, a symbolic benchmark for financial reasoning. "
                   "This is the first benchmark of its kind.",
                   title="Proceedings of the 64th Annual Meeting of the Association")
    assert L.target_title(doc) == "", "a proceedings header is not a title"
    claim = next(c for c in L.novelty_claims(doc) if "FinChain" in c.quote)
    assert L.coined_names(claim.quote) == {"FinChain"}
    twin = WorkIdentity(arxiv_id="2506.02515", date="2025-06-03",
                        title="FinChain: A Symbolic Benchmark for Financial Reasoning",
                        abstract="we introduce FinChain, the first benchmark for finance.")
    twin.canonical_id = L.canonical_id(twin)
    fact = L.bind_prior_art(doc, claim, twin, relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote="we introduce FinChain, the first benchmark "
                                            "for finance.",
                            cutoff=L.derive_cutoff({"proceedings_year": "2026"}))
    assert fact.refusal == "candidate_may_be_the_target_itself"
    assert "cannot be settled here" in fact.statement


def test_a_usable_title_lets_a_shared_coined_name_through_as_a_referees_question():
    """The self-check must not become a filter on every name collision.

    With a title this harness will stand behind, identity is already decided by
    `same_work`; a DIFFERENT work using the same name is a terminology collision or a real
    priority conflict, and which of those it is belongs to a referee.
    """
    doc = doc_with("We introduce FlowNet, a network for optical flow. "
                   "This is the first method to learn optical flow end to end.")
    claim = priority_claim(doc)
    other = WorkIdentity(doi="10.9/other", date="2013-01-01",
                         title="FlowNet: an unrelated flow scheduling system",
                         abstract=EARLIER_ABSTRACT)
    other.canonical_id = L.canonical_id(other)
    assert not L.may_be_same_work(doc, claim, other)
    fact = L.bind_prior_art(doc, claim, other, relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.authority == "ENDPOINTS_VERIFIED_LITERATURE_CONCERN", fact.refusal


# --------------------------------------------------------------------------------------
# AUTHORITY — §2
# --------------------------------------------------------------------------------------
def test_a_verified_overlap_stops_at_endpoints_when_the_relation_is_a_reading():
    fact = L.bind_prior_art(doc_with(ABSTRACT), priority_claim(doc_with(ABSTRACT)),
                            earlier(), relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF,
                            binding_basis="reviewer_reading")
    assert fact.authority == "ENDPOINTS_VERIFIED_LITERATURE_CONCERN"
    assert "the literature reviewer's reading" in fact.statement


def test_level_three_needs_both_a_deterministic_basis_and_a_priority_claim():
    doc = doc_with(ABSTRACT)
    bound = L.bind_prior_art(doc, priority_claim(doc), earlier(),
                             relation="POSSIBLE_PRIORITY_CONFLICT",
                             candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF,
                             binding_basis="explicit_first_claim_met")
    assert bound.authority == "STRUCTURALLY_BOUND_PRIOR_ART"
    assert taxonomy.evidence_state("PRIOR_ART_RELATION_STRUCTURALLY_BOUND") == \
        "PRIOR_ART_EVIDENCE"
    assert taxonomy.resolution_state("PRIOR_ART_EVIDENCE") == "RESOLVED_FROM_LITERATURE"

    contribution = next(c for c in L.novelty_claims(doc) if c.claim_kind == "CONTRIBUTION")
    weaker = L.bind_prior_art(doc, contribution, earlier(),
                              relation="POSSIBLE_PRIORITY_CONFLICT",
                              candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF,
                              binding_basis="explicit_first_claim_met")
    assert weaker.authority == "ENDPOINTS_VERIFIED_LITERATURE_CONCERN"


def test_no_literature_outcome_can_reach_a_material_failure():
    """§16. The route may raise a question; it may not stop a paper."""
    from harness.artifacts import TargetOutcome
    for disposition in ("PRIOR_ART_RELATION_STRUCTURALLY_BOUND",
                        "LITERATURE_MATCH_VERIFIED_ENDPOINTS",
                        "SEARCH_COMPLETED_NO_MATCH_FOUND", "SEARCH_INCONCLUSIVE",
                        "LITERATURE_BLOCKED"):
        out = TargetOutcome(target_id="t", disposition=disposition,
                            provenance="literature")
        assert out.establishes_failure is False, disposition


# --------------------------------------------------------------------------------------
# WHAT THE PAPER ALREADY CITES — §8
# --------------------------------------------------------------------------------------
def test_a_cited_predecessor_is_a_different_concern_from_an_omitted_one():
    """Calling the first the second would accuse the authors of something they did not do."""
    doc = doc_with(ABSTRACT, references="[1] X. Y. End-to-end optical flow. 2013. "
                                        "doi:10.9/earlier")
    fact = L.bind_prior_art(doc, priority_claim(doc), earlier(),
                            relation="LIKELY_DIRECT_PREDECESSOR",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.citation_state == "CITED_BY_TARGET"
    assert fact.supports_concern, "being cited does not refuse the concern"


def test_an_uncited_candidate_is_marked_uncited():
    doc = doc_with(ABSTRACT)
    fact = L.bind_prior_art(doc, priority_claim(doc), earlier(),
                            relation="POSSIBLE_OMITTED_BASELINE",
                            candidate_quote=EARLIER_ABSTRACT, cutoff=CUTOFF)
    assert fact.citation_state == "APPARENTLY_UNCITED"


def test_an_unrecovered_bibliography_is_never_reported_as_uncited():
    """"We could not read the reference list" and "it is not in it" are opposite facts."""
    doc = doc_with(ABSTRACT)
    doc.body_end_section_idx = -1
    state_, _why = L.citation_state(doc, earlier())
    assert state_ == "BIBLIOGRAPHY_UNAVAILABLE"


# --------------------------------------------------------------------------------------
# PROVIDERS — §4, §14
# --------------------------------------------------------------------------------------
def test_a_provider_failure_is_explicit_and_does_not_complete_the_protocol():
    search = LiteratureSearch(paper_id="p", cutoff=CUTOFF, provider_calls=[
        ProviderCall(provider="openalex", query="q", outcome="COMPLETED", n_results=3),
        ProviderCall(provider="semanticscholar", query="q", outcome="UNAUTHENTICATED")])
    assert search.providers_completed == ["openalex"]
    assert search.providers_failed == ["semanticscholar"]
    assert search.protocol_completed is False
    assert L.outcome_disposition(search)[0] == "LITERATURE_BLOCKED"


def test_one_provider_succeeding_does_not_discharge_a_protocol_that_required_two():
    """§14: an unconfigured index is a CONFIGURATION outcome, not scientific discharge."""
    search = LiteratureSearch(paper_id="p", cutoff=CUTOFF, provider_calls=[
        ProviderCall(provider="crossref", query="q", outcome="NO_RESULTS"),
        ProviderCall(provider="arxiv", query="q", outcome="UNREACHABLE")])
    disposition, why = L.outcome_disposition(search)
    assert disposition == "LITERATURE_BLOCKED"
    assert "not a completed query" in why
    assert taxonomy.evidence_state(disposition) == "LITERATURE_LIMITATION"


def test_an_index_that_answered_with_nothing_is_a_completed_query():
    search = LiteratureSearch(paper_id="p", cutoff=CUTOFF, provider_calls=[
        ProviderCall(provider="crossref", query="q", outcome="NO_RESULTS")])
    assert search.protocol_completed is True
    assert L.outcome_disposition(search)[0] == "SEARCH_COMPLETED_NO_MATCH_FOUND"


def test_the_blocked_reason_counts_what_did_not_complete():
    """A route blocked by one failed query in fifty must not read like fifty failures."""
    calls = [ProviderCall(provider="crossref", query=f"q{i}", outcome="COMPLETED",
                          n_results=3) for i in range(49)]
    calls.append(ProviderCall(provider="arxiv", query="q49", outcome="UNREACHABLE"))
    _disposition, why = L.outcome_disposition(
        LiteratureSearch(paper_id="p", cutoff=CUTOFF, provider_calls=calls))
    assert "1 of 50 queries" in why and "UNREACHABLE x1" in why
    assert "49 queries that DID complete" in why


def test_arxiv_is_asked_at_the_rate_it_publishes():
    """A route blocked by our own impoliteness looks like a fact about the world.

    arXiv asks for three seconds between requests. At 0.6 it returned `429 Rate exceeded`
    on two of the eight corpus papers, and both reported LITERATURE_BLOCKED.
    """
    assert LP._DELAY_FOR["export.arxiv.org"] >= 3.0
    assert LP._DELAY_S > 0


def test_only_a_transient_outcome_is_retried():
    """A credential this host does not hold is not made available by asking twice."""
    assert set(LP._RETRYABLE) == {"UNREACHABLE", "RATE_LIMITED"}
    assert "UNAUTHENTICATED" not in LP._RETRYABLE


def test_a_cached_run_reproduces_the_normalized_candidate_set_with_no_network():
    body = json.dumps({"message": {"items": [
        {"DOI": "10.1/c", "title": ["Cached Work"],
         "issued": {"date-parts": [[2017, 3, 2]]},
         "abstract": "<jats:p>Some earlier method.</jats:p>"}]}})
    with tempfile.TemporaryDirectory() as tmp:
        LP._write_cache(tmp, LP.cache_key("crossref", "q", 20), body)
        first, call_one = LP.search("crossref", "q", cache_dir=tmp, allow_network=False)
        second, call_two = LP.search("crossref", "q", cache_dir=tmp, allow_network=False)
    assert call_one.from_cache and call_two.from_cache
    assert [w.doi for w in first] == [w.doi for w in second] == ["10.1/c"]
    assert first[0].date == "2017-03-02" and first[0].abstract == "Some earlier method."
    assert L.canonical_id(first[0]) == L.canonical_id(second[0])


def test_a_query_that_was_never_asked_cannot_count_as_exhausted():
    _, call = LP.search("openalex", "anything", allow_network=False)
    assert call.outcome == "NOT_ATTEMPTED" and call.completed is False
    assert "never asked" in call.error


def test_the_record_keeps_every_candidates_identity_and_only_the_read_text():
    """A published candidate count needs every candidate; it does not need every abstract.

    The hash of what was retrieved stays on every work either way, so a later reader can
    still prove what this harness read — and the raw responses are in the cache.
    """
    read, unread = earlier(), earlier()
    unread.doi = "10.9/unread"
    unread.canonical_id = L.canonical_id(unread)
    trimmed = L.trim_for_record([read, unread], {read.canonical_id})
    assert len(trimmed) == 2, "no candidate is dropped from the count"
    kept = {w.canonical_id: w for w in trimmed}
    assert kept[read.canonical_id].abstract == EARLIER_ABSTRACT
    assert kept[unread.canonical_id].abstract == ""
    assert all(w.canonical_id and w.doi for w in trimmed)


# --------------------------------------------------------------------------------------
# THE DRIVER BOUNDARY — §11
# --------------------------------------------------------------------------------------
def test_a_reviewer_claiming_novelty_failure_has_the_claim_stripped():
    proposals, _notes, meta = LD.parse_matches(json.dumps({"matches": [{
        "candidate_id": "c1", "relation": "LIKELY_DIRECT_PREDECESSOR",
        "candidate_quote": EARLIER_ABSTRACT, "binding_basis": "reviewer_reading",
        "authority": "STRUCTURALLY_BOUND_PRIOR_ART", "accepted": True,
        "prior_art_established": True, "novelty_failure": True, "severity": "FATAL",
        "paper_decision": "RED", "chronology": "PREDATES_CUTOFF", "refusal": ""}]}))
    assert meta["harness_keys_stripped"] == 8
    assert set(proposals[0]) <= set(LD._MATCH_KEYS)
    for banned in ("authority", "novelty_failure", "severity", "paper_decision",
                   "prior_art_established", "accepted", "refusal", "chronology"):
        assert banned not in proposals[0], banned


def test_a_model_asserting_an_overlap_whose_endpoints_fail_establishes_nothing():
    doc = doc_with(ABSTRACT)
    claim = priority_claim(doc)
    work = earlier()
    _block, by_id = LD.candidates_block([work])
    facts = LD.adjudicate(doc, claim, [{
        "candidate_id": "c1", "relation": "LIKELY_DIRECT_PREDECESSOR",
        "candidate_quote": "A passage that appears in no retrieved abstract.",
        "binding_basis": "explicit_first_claim_met"}], by_id, cutoff=CUTOFF)
    assert len(facts) == 1
    assert facts[0].authority == "NONE"
    assert facts[0].refusal == "candidate_quote_unresolved"


def test_a_proposal_naming_a_candidate_the_harness_never_retrieved_is_dropped_whole():
    doc = doc_with(ABSTRACT)
    _block, by_id = LD.candidates_block([earlier()])
    assert LD.adjudicate(doc, priority_claim(doc), [
        {"candidate_id": "c99", "relation": "LIKELY_DIRECT_PREDECESSOR",
         "candidate_quote": EARLIER_ABSTRACT}], by_id, cutoff=CUTOFF) == []


def test_the_reviewer_gets_no_tools_at_all():
    """It must not do its own searching: a page nobody retrieved cannot be re-read."""
    conf = LD.review_confinement(Config(allow_literature_review=True))
    assert conf.allowed_tools == ()
    for tool in ("WebFetch", "WebSearch", "Bash", "Read", "Grep", "Write"):
        assert tool in conf.disallowed_tools, tool


def test_neither_prompt_can_be_shown_a_finding_a_severity_or_a_verdict():
    import inspect
    for fn in (LPR.queries, LPR.compare):
        joined = " ".join(inspect.signature(fn).parameters)
        for banned in ("finding", "severity", "verdict", "grade", "outcome", "decision"):
            assert banned not in joined, (fn.__name__, banned)


def test_an_unknown_relation_is_not_renamed_into_a_concerning_one():
    proposals, _n, _m = LD.parse_matches(json.dumps({"matches": [
        {"candidate_id": "c1", "relation": "DEFINITELY_PLAGIARISM"}]}))
    assert proposals[0]["relation"] == "INSUFFICIENT_EVIDENCE"


def test_a_query_outside_the_declared_families_is_dropped_rather_than_counted():
    queries, _notes = LD.parse_queries(json.dumps({"queries": [
        {"family": "EXACT_METHOD_NAME", "query": "flownet"},
        {"family": "WHATEVER_I_LIKE", "query": "something else"}]}))
    assert queries == [("EXACT_METHOD_NAME", "flownet")]


# --------------------------------------------------------------------------------------
# THE ROUTE, END TO END, OFFLINE
# --------------------------------------------------------------------------------------
def _cache_empty_crossref(cfg: Config, pid: str, doc: PaperDoc) -> Path:
    cache = state.project_dir(cfg, pid) / "literature" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    empty = json.dumps({"message": {"items": []}})
    protocol = LS.protocol_for(cfg)
    for claim in L.novelty_claims(doc, limit=protocol.max_claims):
        for _family, query in L.deterministic_queries(doc, claim, protocol=protocol):
            LP._write_cache(cache, LP.cache_key("crossref", query, protocol.top_k), empty)
    LP._write_cache(cache, LP.cache_key("crossref", doc.title, 5), empty)
    return cache


def test_a_closed_search_gate_writes_no_record_rather_than_an_empty_one():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(projects_dir=Path(tmp) / "projects", allow_literature_search=False)
        assert LS.run_route(cfg, "p", doc_with(ABSTRACT), None) == ([], None)


def test_the_route_searches_the_papers_own_novelty_sentences_and_addresses_them():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(projects_dir=Path(tmp) / "projects", allow_literature_search=True,
                     allow_network=False, literature_providers="crossref")
        state.create_project(cfg, "", "T", pid="p")
        doc = doc_with(ABSTRACT)
        _cache_empty_crossref(cfg, "p", doc)
        _outcomes, search = LS.run_route(cfg, "p", doc, None)
        assert search is not None
        assert search.claims_searched, "a paper claiming to be first has search targets"
        for claim in search.claims_searched:
            assert claim.ref and claim.quote
            assert claim.quote in " ".join(s.text for s in doc.sections)
            assert claim.queries, "every searched claim records the exact queries issued"
        assert search.protocol_completed and not search.discharged
        assert LD.load(cfg, "p") is not None


def test_a_search_that_completed_with_no_match_leaves_the_target_unresolved():
    from harness.artifacts import ClaimRef, DiscoveredObject, PlanDecision, TargetSet
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(projects_dir=Path(tmp) / "projects", allow_literature_search=True,
                     allow_network=False, literature_providers="crossref")
        state.create_project(cfg, "", "T", pid="p")
        doc = doc_with(ABSTRACT)
        _cache_empty_crossref(cfg, "p", doc)
        ts = TargetSet(paper_id="p", objects=[DiscoveredObject(
            target_id="t1", question_kind="PRIOR_ART",
            claim_text="is the contribution new relative to published work?",
            ref=ClaimRef(ref="P0:0-10", kind="prose_claim", resolution="resolved"),
            routes=["LITERATURE_SEARCH"])],
            plans=[PlanDecision(target_id="t1", action=LS.ACTION,
                                route="LITERATURE_SEARCH", requires_execution=False)])
        outcomes, _search = LS.run_route(cfg, "p", doc, ts)
        assert len(outcomes) == 1
        assert outcomes[0].disposition == "SEARCH_COMPLETED_NO_MATCH_FOUND"
        assert outcomes[0].resolution_state == "UNRESOLVED"
        assert outcomes[0].evidence_state not in taxonomy.EVIDENCE_ABOUT_THE_PAPER


def test_the_route_is_only_planned_where_no_executable_route_applies():
    """It must never be the reason an experiment did not happen."""
    from harness import planner
    from harness.artifacts import ClaimRef, DiscoveredObject

    def plan_for(routes):
        obj = DiscoveredObject(
            target_id="t", question_kind="PRIOR_ART", centrality="CENTRAL",
            claim_text="c", harness_addressable=True, routes=list(routes),
            ref=ClaimRef(ref="P0:0-10", kind="prose_claim", resolution="resolved"))
        return planner.plan(obj, artifact_available=True, specification_complete=True,
                            environment_state="ok")

    assert plan_for(["LITERATURE_SEARCH"]).action == "LITERATURE_SEARCH_ONLY"
    runnable = plan_for(["AUTHOR_CODE_EXECUTION", "LITERATURE_SEARCH"])
    assert runnable.action != "LITERATURE_SEARCH_ONLY", runnable.action


@pytest.mark.parametrize("disposition,evidence", [
    ("PRIOR_ART_RELATION_STRUCTURALLY_BOUND", "PRIOR_ART_EVIDENCE"),
    ("LITERATURE_MATCH_VERIFIED_ENDPOINTS", "LITERATURE_ENDPOINTS_VERIFIED"),
    ("SEARCH_COMPLETED_NO_MATCH_FOUND", "BOUNDED_SEARCH_NO_MATCH"),
    ("SEARCH_INCONCLUSIVE", "LITERATURE_LIMITATION"),
    ("LITERATURE_BLOCKED", "LITERATURE_LIMITATION"),
])
def test_every_literature_disposition_maps_to_its_own_evidence_state(disposition, evidence):
    assert taxonomy.evidence_state(disposition) == evidence
    assert disposition in TARGET_DISPOSITIONS
