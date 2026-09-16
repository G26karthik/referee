"""The LITERATURE_SEARCH route: a bounded prior-art investigation, run per paper.

`python -m harness.stages.literature` runs the self-check (offline).

**What this stage is, stated so it cannot be read as more.** It searches public scholarly
indexes for work that predates the paper and appears to make the same contribution one of
the paper's own novelty sentences claims. It answers bounded questions a referee asks, and
it never answers "is this paper novel" — a question a bounded search cannot answer, and
which has no spelling anywhere in this route's vocabulary.

**Three things happen here that do not happen in a search engine.** The claims searched
for are QUOTATIONS minted against the parsed paper, so a concern always names a sentence a
referee can open. The target's earliest public date is established from the indexes'
own records and a candidate later than it is refused whatever a reading says. And the
candidate set is CANONICALISED, so one paper appearing as a preprint, a Crossref record
and an OpenAlex work counts once.

**It never suppresses an execution.** `planner` reaches `LITERATURE_SEARCH_ONLY` only
where no executable route applies — the same placement `ARTIFACT_INSPECTION_ONLY` has, and
for the same reason: a cheap route that settles nothing must never be why an experiment
did not happen.

**And a paper-level search runs even when no target was planned for it**, because the
novelty claims this route searches are the paper's own and are not a finding's to raise.
The record is written whatever happens; what changes with the gates is whether anything
was asked and whether anything read the answers.
"""
from __future__ import annotations

from pathlib import Path

from .. import literature, literature_driver, literature_providers, state
from ..artifacts import (DiscoveredObject, LiteratureSearch, PaperDoc, PlanDecision,
                         PriorArtFact, ProviderCall, SearchCutoff, SearchProtocol,
                         TargetOutcome, TargetSet, WorkIdentity)
from ..config import Config
from ..prompts import literature as LP

ACTION = "LITERATURE_SEARCH_ONLY"


def planned(target_set: TargetSet | None) -> list[tuple[DiscoveredObject, PlanDecision]]:
    """The (object, plan) pairs this route owns. Never anything `probe` will execute."""
    if target_set is None:
        return []
    by_id = {o.target_id: o for o in target_set.objects}
    out = []
    for plan in target_set.plans:
        if plan.action != ACTION or plan.requires_execution:
            continue
        obj = by_id.get(plan.target_id)
        if obj is not None:
            out.append((obj, plan))
    return out


def protocol_for(cfg: Config) -> SearchProtocol:
    """The bounds this run declares, from the operator's configuration. Published as-is.

    Narrowing the provider list narrows the PROTOCOL, not just the work: `protocol_
    completed` is false when a required index did not answer, so an operator who removes
    an index gets a smaller honest protocol rather than a cheaper claim.
    """
    providers = [p.strip().lower() for p in (cfg.literature_providers or "").split(",")
                 if p.strip()]
    return SearchProtocol(
        query_families=list(literature.DEFAULT_PROTOCOL.query_families),
        providers_required=providers or list(literature.DEFAULT_PROTOCOL.providers_required),
        top_k=max(1, int(cfg.literature_top_k)),
        max_queries_per_claim=literature.DEFAULT_PROTOCOL.max_queries_per_claim,
        max_claims=max(1, int(cfg.literature_max_claims)),
        max_candidates_reviewed=max(1, int(cfg.literature_max_candidates)),
        notes=literature.DEFAULT_PROTOCOL.notes)


def cutoff_for(cfg: Config, doc: PaperDoc, *, cache_dir: Path | None = None,
               providers: tuple[str, ...] = ()) -> tuple[SearchCutoff, list[ProviderCall]]:
    """The target's own earliest public date, and the calls that established it.

    The paper's own printed year is offered LAST and only as `proceedings_year`, which
    yields YEAR_ONLY — so a paper whose date this harness could establish only from a
    copyright line cannot be used to order a same-year candidate. That is the difference
    between knowing when a paper appeared and knowing which of two papers came first.
    """
    title = literature.target_title(doc)
    own_id = literature.own_arxiv_id(doc)
    own_doi = literature.own_doi(doc)
    offers: dict[str, str] = {}
    calls: list[ProviderCall] = []
    if title or own_id or own_doi:
        offers, calls = literature_providers.date_offers(
            title, providers=(providers or tuple(protocol_for(cfg).providers_required)),
            arxiv_id=own_id, doi=own_doi, cache_dir=cache_dir,
            mailto=cfg.literature_mailto,
            allow_network=bool(cfg.allow_literature_search and cfg.allow_network))
    year = literature.proceedings_year(doc)
    if year and "proceedings_year" not in offers:
        offers["proceedings_year"] = year
    return literature.derive_cutoff(offers), calls


def _queries_for(cfg: Config, doc: PaperDoc, claim, protocol: SearchProtocol
                 ) -> tuple[list[tuple[str, str]], dict | None]:
    """(queries, driver record). Deterministic first; the proposer only ADDS.

    The deterministic families run whatever the review gate says, so a run with the gate
    closed still searches — and a run with it open can be compared against one without it,
    which is the only way to find out what the proposer was worth.
    """
    out = literature.deterministic_queries(doc, claim, protocol=protocol)
    if not cfg.allow_literature_review:
        return out, None
    prompt = LP.queries(doc.title, claim.quote, ", ".join(claim.concepts),
                        ", ".join(literature.method_names(doc)))
    raw, record = literature_driver.call(cfg, prompt, tag=f"queries:{claim.claim_id}")
    if not raw:
        return out, record
    try:
        proposed, _notes = literature_driver.parse_queries(raw)
    except literature_driver.LiteratureDriverError as exc:
        record["failure"] = f"unusable_output: {exc}"
        return out, record
    have = {q.lower() for _f, q in out}
    for family, query in proposed:
        if query.lower() in have:
            continue
        out.append((family, query))
        have.add(query.lower())
    record["proposed_queries"] = len(proposed)
    return out[:max(1, protocol.max_queries_per_claim) * 2], record


def _search_claim(cfg: Config, doc: PaperDoc, claim, protocol: SearchProtocol,
                  cache_dir: Path | None) -> tuple[list[WorkIdentity], list[ProviderCall],
                                                   int, list[dict]]:
    """Everything one claim's queries returned: (works, calls, raw count, driver records)."""
    queries, record = _queries_for(cfg, doc, claim, protocol)
    claim.queries = [q for _f, q in queries]
    records = [record] if record else []
    works: list[WorkIdentity] = []
    calls: list[ProviderCall] = []
    raw = 0
    for family, query in queries:
        for provider in protocol.providers_required:
            got, call = literature_providers.search(
                provider, query, top_k=protocol.top_k, cache_dir=cache_dir,
                mailto=cfg.literature_mailto,
                allow_network=bool(cfg.allow_literature_search and cfg.allow_network),
                claim_id=claim.claim_id, query_family=family)
            calls.append(call)
            raw += len(got)
            works += got
    return works, calls, raw, records


def _adjudicate_claim(cfg: Config, doc: PaperDoc, claim, works: list[WorkIdentity],
                      cutoff: SearchCutoff, bibliography: str,
                      protocol: SearchProtocol | None = None,
                      shown: set[str] | None = None
                      ) -> tuple[list[PriorArtFact], int, list[dict]]:
    """(facts, proposals received, driver records) for ONE claim's pre-cutoff candidates.

    ONLY PRE-CUTOFF CANDIDATES ARE SHOWN TO THE READER. A later work cannot be prior art,
    so putting it in front of a reader asked to find overlaps buys nothing and costs a
    plausible wrong answer — `bind_prior_art` would refuse it afterwards, and a refusal
    the harness never had to make is better than one it did.
    """
    shown = shown if shown is not None else set()
    eligible = [w for w in works
                if literature.chronology(cutoff, w)[0] == "PREDATES_CUTOFF"]
    # IN RETRIEVAL ORDER, and cut at the declared bound. The order is the indexes' own
    # ranking, which is the only ordering this harness has any reason to trust — sorting
    # by anything else here would be this system deciding which earlier work is most
    # relevant, which is the judgement it is asking a reader for.
    bound = (protocol or literature.DEFAULT_PROTOCOL).max_candidates_reviewed
    eligible = eligible[:max(1, int(bound))]
    if not eligible or not cfg.allow_literature_review:
        return [], 0, []
    # WHAT A READER WAS ACTUALLY SHOWN, recorded here and nowhere earlier: these are the
    # only works a quoted passage could have come from, so they are the only ones whose
    # retrieved text the record has to keep.
    shown.update(w.canonical_id for w in eligible)
    block, by_id = literature_driver.candidates_block(eligible)
    when = f"{cutoff.date} (source: {cutoff.source}, {cutoff.state})"
    raw, record = literature_driver.call(
        cfg, LP.compare(doc.title, claim.quote, claim.ref, when, block),
        tag=f"compare:{claim.claim_id}")
    if not raw:
        return [], 0, [record]
    try:
        proposals, _notes, meta = literature_driver.parse_matches(raw)
    except literature_driver.LiteratureDriverError as exc:
        record["failure"] = f"unusable_output: {exc}"
        return [], 0, [record]
    record.update(meta)
    facts = literature_driver.adjudicate(doc, claim, proposals, by_id, cutoff=cutoff,
                                         bibliography=bibliography)
    return facts, len(proposals), [record]


def run_route(cfg: Config, pid: str, doc: PaperDoc, target_set: TargetSet | None
              ) -> tuple[list[TargetOutcome], LiteratureSearch | None]:
    """The bounded protocol, over one paper. ([], None) when the search gate is shut.

    The gate is checked ONCE, here, and a closed gate produces no record at all rather
    than an empty one: an empty record and a record nobody asked for look identical
    afterwards, and this route's entire discipline is that a silence must say which
    silence it is.
    """
    if not cfg.allow_literature_search:
        return [], None

    protocol = protocol_for(cfg)
    cache_dir = state.project_dir(cfg, pid) / "literature" / "cache"
    cutoff, cutoff_calls = cutoff_for(cfg, doc, cache_dir=cache_dir,
                                      providers=tuple(protocol.providers_required))
    bibliography = literature.bibliography_text(doc)
    claims = literature.novelty_claims(doc, limit=protocol.max_claims)

    works: list[WorkIdentity] = []
    calls: list[ProviderCall] = list(cutoff_calls)
    facts: list[PriorArtFact] = []
    records: list[dict] = []
    shown: set[str] = set()
    raw_results = 0
    proposed = 0
    for claim in claims:
        got, claim_calls, raw, query_records = _search_claim(
            cfg, doc, claim, protocol, cache_dir)
        works += got
        calls += claim_calls
        raw_results += raw
        records += query_records
        claim_works = literature.merge_works(got)
        claim_facts, n, review_records = _adjudicate_claim(
            cfg, doc, claim, claim_works, cutoff, bibliography, protocol, shown)
        facts += claim_facts
        proposed += n
        records += review_records

    merged = literature.trim_for_record(literature.merge_works(works), shown)
    search = LiteratureSearch(
        paper_id=pid, cutoff=cutoff, protocol=protocol, claims_searched=claims,
        provider_calls=calls, works=merged, raw_results=raw_results, facts=facts,
        cited_by_target=sum(1 for f in facts if f.citation_state == "CITED_BY_TARGET"),
        apparently_uncited=sum(1 for f in facts
                               if f.citation_state == "APPARENTLY_UNCITED"),
        candidates_reviewed=len(shown), proposed=proposed,
        escalations=literature.escalations_from(facts))
    discharged, reason = literature.discharge(search)
    disposition, _ = literature.outcome_disposition(search)
    search.discharged, search.reason = discharged, reason

    outcomes: list[TargetOutcome] = []
    for obj, plan in planned(target_set):
        outcomes.append(TargetOutcome(
            target_id=obj.target_id, disposition=disposition, action=plan.action,
            route=plan.route, launched=0, provenance="literature",
            reason=(f"a bounded prior-art search was carried out for this question. "
                    f"{reason}")))

    try:
        literature_driver.seal(cfg, pid, search, {
            "written_by": "literature_driver", "protocol": protocol.model_dump(),
            "disposition": disposition, "discharged": discharged,
            "summary": literature.summarise(search),
            "calls": [r for r in records if r],
        })
    except OSError:
        pass
    return outcomes, search


# --------------------------------------------------------------------------- #
if __name__ == "__main__":       # self-check: python -m harness.stages.literature
    import json
    import tempfile

    from .. import taxonomy
    from ..artifacts import ClaimRef, Section

    doc = PaperDoc(paper_id="p", title="FlowNet: Learning Optical Flow",
                   body_end_section_idx=2, sections=[
                       Section(section_idx=0, title="Abstract", text=(
                           "We introduce FlowNet for optical flow estimation. "
                           "This is the first method to learn optical flow end to end "
                           "from synthetic data.")),
                       Section(section_idx=1, title="1 Introduction",
                               text="Optical flow estimation is a classical problem."),
                       Section(section_idx=2, title="References",
                               text="[1] Somebody. Something else. 2011."),
                   ])

    # A CLOSED SEARCH GATE PRODUCES NO RECORD, not an empty one.
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td) / "projects", allow_literature_search=False)
        assert run_route(cfg, "p", doc, None) == ([], None)

    # WITH THE GATE OPEN AND NO NETWORK: the protocol did not complete, and the route
    # says so instead of reporting a clean search.
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td) / "projects", allow_literature_search=True,
                     allow_network=False, literature_providers="crossref")
        state.create_project(cfg, "", "T", pid="p")
        outcomes, search = run_route(cfg, "p", doc, None)
        assert search is not None and outcomes == []
        assert search.claims_searched, "the paper's own novelty sentences are the targets"
        assert all(c.ref for c in search.claims_searched)
        assert not search.protocol_completed
        assert literature.outcome_disposition(search)[0] == "LITERATURE_BLOCKED"
        assert search.discharged is False
        sealed = literature_driver.load(cfg, "p")
        assert sealed is not None and len(sealed.claims_searched) == len(search.claims_searched)

    # A COMPLETED SEARCH THAT MATCHED NOTHING IS NOT NOVELTY, and the outcome a planned
    # target receives carries that and nothing stronger.
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(projects_dir=Path(td) / "projects", allow_literature_search=True,
                     allow_network=False, literature_providers="crossref")
        state.create_project(cfg, "", "T", pid="p")
        cache = state.project_dir(cfg, "p") / "literature" / "cache"
        # Every query this run will issue, answered from cache with an empty result set.
        protocol = protocol_for(cfg)
        cutoff_probe, _ = cutoff_for(cfg, doc, cache_dir=cache,
                                     providers=("crossref",))
        empty = json.dumps({"message": {"items": []}})
        cache.mkdir(parents=True, exist_ok=True)
        for claim in literature.novelty_claims(doc, limit=protocol.max_claims):
            for _family, query in literature.deterministic_queries(doc, claim,
                                                                   protocol=protocol):
                key = literature_providers.cache_key("crossref", query, protocol.top_k)
                literature_providers._write_cache(cache, key, empty)
        for key in (literature_providers.cache_key("crossref", doc.title, 5),):
            literature_providers._write_cache(cache, key, empty)

        ts = TargetSet(paper_id="p", objects=[DiscoveredObject(
            target_id="t1", question_kind="PRIOR_ART",
            claim_text="is the contribution new relative to published work?",
            ref=ClaimRef(ref="P0:0-10", kind="prose_claim", resolution="resolved"),
            routes=["LITERATURE_SEARCH"])],
            plans=[PlanDecision(target_id="t1", action=ACTION, route="LITERATURE_SEARCH",
                                requires_execution=False)])
        outcomes, search = run_route(cfg, "p", doc, ts)
        assert search is not None and search.protocol_completed, search.provider_calls
        assert len(outcomes) == 1
        got = outcomes[0]
        assert got.disposition == "SEARCH_COMPLETED_NO_MATCH_FOUND", got.disposition
        assert got.evidence_state == "BOUNDED_SEARCH_NO_MATCH"
        assert got.resolution_state == "UNRESOLVED"
        assert got.evidence_state not in taxonomy.EVIDENCE_ABOUT_THE_PAPER
        assert got.establishes_failure is False
        assert "NOT EVIDENCE OF NOVELTY" in got.reason
        assert search.discharged is False

        # The declared bounds are in the record, so "no match" is a statement with a
        # denominator rather than a claim about the literature.
        s = literature.summarise(search)
        assert s["queries_executed"] == len(search.provider_calls)
        assert s["providers_completed"] == ["crossref"] and s["structurally_bound"] == 0
        assert s["protocol_completed"] is True

    print("harness.stages.literature self-check ok")
