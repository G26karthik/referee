"""The scholarly indexes this harness will ask, and the record it keeps of asking them.

`python -m harness.literature_providers` runs the self-check (offline; it parses fixtures).

**No scraping.** Every provider here is a documented API with stable identifiers —
OpenAlex, Crossref, arXiv, Semantic Scholar — because a prior-art result has to carry an
identifier a referee can open, and a scraped search page carries a rank instead. A snippet
is not a work.

**A provider failure is explicit, and it is not a scientific result.** `PROVIDER_OUTCOMES`
separates an index that answered from an index that rate-limited us, refused us for want
of a credential, or could not be reached at all. That distinction is the whole reason the
route can say what "completed" means: an unconfigured system has not completed a bounded
protocol, it has failed to attempt one, and counting the two the same way would let a
missing API key discharge a scientific route.

**Everything is cached, so the measurement is reproducible.** The raw response is written
under a key derived from the provider, the query and the depth, and a later run reads it
back rather than re-asking — indexes change, and a number in a report that cannot be
re-derived a month later is a number nobody can check. `from_cache` is recorded, so a
reader can tell a cached run from a live one.

**And the harness keeps the text it read.** A candidate's abstract is stored with the
SHA-256 of what was retrieved, because `literature.bind_prior_art` requires a quoted
passage to be present in the text THIS harness retrieved — the same rule
`artifact_evidence.relocate` applies to a line of source and `claims.mint` applies to a
sentence of the paper.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import ProviderCall, WorkIdentity

# Every index below is polite about being identified, and two of them rate-limit harder
# when you are not. The mailto is the operator's, supplied by `Config`; this constant is
# only the shape of the header.
USER_AGENT = "single-harness-referee/1.0 (+https://github.com/; mailto:{mail})"

PROVIDERS = ("openalex", "crossref", "arxiv", "semanticscholar")

# Politeness delay between live calls to one index. Not a rate limiter — the real bound on
# this route is the query budget in `literature.SearchProtocol` — but the indexes publish
# terms and this harness keeps them. **arXiv asks for three seconds between requests**, and
# 0.6 was enough to get `429 Rate exceeded` on two of the eight corpus papers, which then
# reported LITERATURE_BLOCKED for a reason that had nothing to do with any paper: a route
# refused by our own impoliteness is the worst kind of measurement, because it looks like
# a fact about the world.
_DELAY_S = 0.6
_DELAY_FOR = {"export.arxiv.org": 3.0}
_LAST_CALL: dict[str, float] = {}

# ONE retry, and only for the two outcomes a second attempt can actually change. A read
# timeout or a 5xx from an index is weather; `UNAUTHENTICATED` is this host lacking a
# credential and retrying it is just asking twice. This is API politeness and not a
# scientific retry budget — nothing here re-asks a question whose answer was "no".
_RETRYABLE = ("UNREACHABLE", "RATE_LIMITED")
_RETRY_PAUSE_S = 4.0


def cache_key(provider: str, query: str, top_k: int) -> str:
    return hashlib.sha256(f"{provider}|{query}|{top_k}".encode("utf-8")).hexdigest()[:24]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class ProviderError(RuntimeError):
    """Carries the OUTCOME, not just a message: the caller must record which failure."""

    def __init__(self, outcome: str, detail: str = "", status: int = 0) -> None:
        super().__init__(detail or outcome)
        self.outcome, self.detail, self.status = outcome, detail, status


# OpenAlex and Crossref both operate a "polite pool" with a much higher rate limit for
# callers that identify themselves in the QUERY STRING, not merely in the User-Agent. This
# is not a credential and grants no access this host does not already have: it is how those
# two indexes ask to be told who is asking. Without it OpenAlex rate-limited 48 of this
# corpus's queries and four papers reported LITERATURE_BLOCKED for a reason that had
# nothing to do with any paper.
_POLITE = ("openalex", "crossref")


def _polite(url: str, provider: str, mailto: str) -> str:
    if provider not in _POLITE or not (mailto or "").strip():
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}mailto={urllib.parse.quote(mailto.strip())}"


def _fetch(url: str, *, mailto: str, timeout: float, accept: str = "application/json") -> str:
    """One GET, with every failure mapped to a named outcome rather than to an exception.

    HTTP 401/403 is UNAUTHENTICATED and 429 is RATE_LIMITED, and they are different from
    UNREACHABLE for a reason that matters to this route: the first two say this host is
    not configured to ask, and the third says nobody could have asked. All three are facts
    about our configuration or the network, and none is a fact about anybody's paper.
    """
    host = urllib.parse.urlsplit(url).netloc
    delay = _DELAY_FOR.get(host, _DELAY_S)
    gap = time.monotonic() - _LAST_CALL.get(host, 0.0)
    if gap < delay:
        time.sleep(delay - gap)
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT.format(mail=mailto or "referee@localhost"),
        "Accept": accept,
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:                          # noqa: BLE001 — a body is a courtesy
            detail = ""
        if status in (401, 403):
            raise ProviderError("UNAUTHENTICATED", f"HTTP {status} {detail}".strip(),
                                status) from exc
        if status == 429:
            # A 429 IS TWO DIFFERENT FACTS AND THE BODY SAYS WHICH. "Slow down" is
            # transient and a later run gets through; "Insufficient budget ... Resets at
            # midnight UTC" is this host lacking a paid credential, which no amount of
            # waiting inside one run fixes. OpenAlex returns the second under the first's
            # status code, and recording it as RATE_LIMITED would tell a reader to retry
            # something that cannot succeed today.
            budget = any(word in detail.lower()
                         for word in ("budget", "add funds", "pricing", "subscription"))
            raise ProviderError("UNAUTHENTICATED" if budget else "RATE_LIMITED",
                                f"HTTP {status} {detail}".strip(), status) from exc
        raise ProviderError("UNREACHABLE", f"HTTP {status} {detail}".strip(),
                            status) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("UNREACHABLE", f"{type(exc).__name__}: {exc}") from exc
    finally:
        _LAST_CALL[host] = time.monotonic()
    return body


# --------------------------------------------------------------------------------------
# PARSERS — pure, and tested against recorded fixtures rather than against the network
# --------------------------------------------------------------------------------------
def _openalex_abstract(inverted: dict | None) -> str:
    """OpenAlex publishes an inverted index rather than an abstract. Rebuild it in order.

    Reconstructed exactly, with no reflowing: the text stored here is what a quoted
    passage is checked against, so a reconstruction that silently normalised anything
    would move the goalposts under `bind_prior_art`.
    """
    if not isinstance(inverted, dict) or not inverted:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for pos in positions or []:
            try:
                slots[int(pos)] = str(word)
            except (TypeError, ValueError):
                continue
    return " ".join(slots[i] for i in sorted(slots))


def parse_openalex(body: str) -> list[WorkIdentity]:
    payload = json.loads(body)
    out: list[WorkIdentity] = []
    for item in (payload.get("results") or []):
        abstract = _openalex_abstract(item.get("abstract_inverted_index"))
        ids = item.get("ids") or {}
        arxiv = ""
        for candidate in (item.get("locations") or []):
            landing = ((candidate.get("landing_page_url") or "") if isinstance(candidate, dict)
                       else "")
            if "arxiv.org/abs/" in landing:
                arxiv = landing.rsplit("/", 1)[-1]
                break
        out.append(WorkIdentity(
            doi=(item.get("doi") or ""), arxiv_id=arxiv,
            openalex_id=(ids.get("openalex") or item.get("id") or "").rsplit("/", 1)[-1],
            title=(item.get("display_name") or item.get("title") or ""),
            authors=[((a.get("author") or {}).get("display_name") or "")
                     for a in (item.get("authorships") or [])][:8],
            venue=(((item.get("primary_location") or {}).get("source") or {})
                   .get("display_name") or ""),
            year=int(item.get("publication_year") or 0),
            date=(item.get("publication_date") or ""),
            date_source="openalex_publication_date",
            url=(item.get("doi") or item.get("id") or ""),
            abstract=abstract, abstract_sha256=_sha(abstract), providers=["openalex"]))
    return out


def parse_crossref(body: str) -> list[WorkIdentity]:
    payload = json.loads(body)
    out: list[WorkIdentity] = []
    for item in ((payload.get("message") or {}).get("items") or []):
        title = " ".join(item.get("title") or []) or ""
        parts = (((item.get("issued") or {}).get("date-parts") or [[]])[0]
                 or ((item.get("created") or {}).get("date-parts") or [[]])[0] or [])
        date = "-".join(f"{int(p):02d}" if i else f"{int(p):04d}"
                        for i, p in enumerate(parts[:3]) if str(p).isdigit())
        abstract = _strip_tags(item.get("abstract") or "")
        out.append(WorkIdentity(
            doi=(item.get("DOI") or ""),
            title=title,
            authors=[" ".join(x for x in [(a.get("given") or ""), (a.get("family") or "")] if x)
                     for a in (item.get("author") or [])][:8],
            venue=" ".join(item.get("container-title") or []),
            year=int(parts[0]) if parts and str(parts[0]).isdigit() else 0,
            date=date, date_source="crossref_published",
            url=(item.get("URL") or ""), abstract=abstract,
            abstract_sha256=_sha(abstract), providers=["crossref"]))
    return out


def _strip_tags(text: str) -> str:
    """Crossref abstracts arrive as JATS. Strip the markup; do not touch the words."""
    out, depth = [], 0
    for ch in text or "":
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_arxiv(body: str) -> list[WorkIdentity]:
    """arXiv's Atom feed. `published` is the V1 SUBMISSION, which is what 'public' means."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ProviderError("MALFORMED_RESPONSE", str(exc)) from exc
    out: list[WorkIdentity] = []
    for entry in root.findall(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        abstract = " ".join((entry.findtext(f"{_ATOM}summary") or "").split())
        published = (entry.findtext(f"{_ATOM}published") or "")[:10]
        doi = ""
        for link in entry.findall(f"{_ATOM}link"):
            href = link.get("href") or ""
            if "doi.org/" in href:
                doi = href
        out.append(WorkIdentity(
            doi=doi, arxiv_id=raw_id.rsplit("/", 1)[-1],
            title=" ".join((entry.findtext(f"{_ATOM}title") or "").split()),
            authors=[" ".join((a.findtext(f"{_ATOM}name") or "").split())
                     for a in entry.findall(f"{_ATOM}author")][:8],
            venue="arXiv", year=int(published[:4]) if published[:4].isdigit() else 0,
            date=published,
            # THE ONE DATE IN THIS FILE THAT IS THE STRONGEST KIND. An arXiv `published`
            # is the first public posting, which is exactly what a prior-art chronology
            # needs — and is earlier than any proceedings date for the same work.
            date_source="arxiv_submission_v1",
            url=raw_id, abstract=abstract, abstract_sha256=_sha(abstract),
            providers=["arxiv"]))
    return out


def parse_semanticscholar(body: str) -> list[WorkIdentity]:
    payload = json.loads(body)
    out: list[WorkIdentity] = []
    for item in (payload.get("data") or []):
        ext = item.get("externalIds") or {}
        abstract = " ".join((item.get("abstract") or "").split())
        date = (item.get("publicationDate") or "")
        out.append(WorkIdentity(
            doi=(ext.get("DOI") or ""), arxiv_id=(ext.get("ArXiv") or ""),
            s2_id=(item.get("paperId") or ""),
            title=(item.get("title") or ""),
            authors=[(a.get("name") or "") for a in (item.get("authors") or [])][:8],
            venue=(item.get("venue") or ""), year=int(item.get("year") or 0),
            date=date or (f"{int(item.get('year'))}" if item.get("year") else ""),
            date_source="openalex_publication_date" if date else "proceedings_year",
            url=(item.get("url") or ""), abstract=abstract,
            abstract_sha256=_sha(abstract), providers=["semanticscholar"]))
    return out


_URL = {
    "openalex": ("https://api.openalex.org/works?search={q}&per-page={k}"
                 "&select=id,ids,doi,display_name,publication_year,publication_date,"
                 "authorships,primary_location,abstract_inverted_index,locations"),
    "crossref": "https://api.crossref.org/works?query.bibliographic={q}&rows={k}",
    "arxiv": ("http://export.arxiv.org/api/query?search_query=all:{q}"
              "&start=0&max_results={k}"),
    "semanticscholar": ("https://api.semanticscholar.org/graph/v1/paper/search?query={q}"
                        "&limit={k}&fields=title,abstract,year,publicationDate,venue,"
                        "authors,externalIds,url"),
}
_PARSE = {"openalex": parse_openalex, "crossref": parse_crossref,
          "arxiv": parse_arxiv, "semanticscholar": parse_semanticscholar}
_ACCEPT = {"arxiv": "application/atom+xml"}


def search(provider: str, query: str, *, top_k: int = 20, cache_dir: Path | str | None = None,
           mailto: str = "", timeout: float = 30.0, allow_network: bool = True,
           claim_id: str = "", query_family: str = "") -> tuple[list[WorkIdentity], ProviderCall]:
    """One query against one index. ALWAYS returns a call record, even when it failed.

    The record is the point. A route that silently dropped a failed provider would report
    a completed protocol over the indexes that happened to answer, which is how a search
    budget turns into a claim about the literature.
    """
    provider = (provider or "").strip().lower()
    key = cache_key(provider, query, top_k)
    call = ProviderCall(provider=provider, query=query, query_family=query_family,
                        claim_id=claim_id, requested_at=_now(), cache_key=key)
    if provider not in _URL:
        call.outcome, call.error = "NOT_ATTEMPTED", f"unknown provider {provider!r}"
        return [], call

    cached = _read_cache(cache_dir, key)
    started = time.monotonic()
    if cached is not None:
        body, call.from_cache = cached, True
    elif not allow_network:
        call.outcome = "NOT_ATTEMPTED"
        call.error = ("no cached response and the network is not permitted, so this query "
                      "was never asked")
        return [], call
    else:
        url = _polite(_URL[provider].format(q=urllib.parse.quote(query), k=int(top_k)),
                      provider, mailto)
        try:
            body = _fetch(url, mailto=mailto, timeout=timeout,
                          accept=_ACCEPT.get(provider, "application/json"))
        except ProviderError as exc:
            if exc.outcome not in _RETRYABLE:
                call.outcome, call.error, call.http_status = (exc.outcome, exc.detail,
                                                              exc.status)
                call.seconds = round(time.monotonic() - started, 2)
                return [], call
            time.sleep(_RETRY_PAUSE_S)
            try:
                body = _fetch(url, mailto=mailto, timeout=timeout,
                              accept=_ACCEPT.get(provider, "application/json"))
            except ProviderError as again:
                call.outcome, call.http_status = again.outcome, again.status
                call.error = f"{again.detail} (after one retry of {exc.outcome})"
                call.seconds = round(time.monotonic() - started, 2)
                return [], call
            call.error = f"succeeded on one retry after {exc.outcome}"
        _write_cache(cache_dir, key, body)

    try:
        works = _PARSE[provider](body)
    except ProviderError as exc:
        call.outcome, call.error = exc.outcome, exc.detail
        call.seconds = round(time.monotonic() - started, 2)
        return [], call
    except (ValueError, KeyError, TypeError) as exc:
        call.outcome = "MALFORMED_RESPONSE"
        call.error = f"{type(exc).__name__}: {exc}"
        call.seconds = round(time.monotonic() - started, 2)
        return [], call

    call.n_results = len(works)
    call.outcome = "COMPLETED" if works else "NO_RESULTS"
    call.http_status = call.http_status or 200
    call.seconds = round(time.monotonic() - started, 2)
    return works, call


def _read_cache(cache_dir: Path | str | None, key: str) -> str | None:
    if not cache_dir:
        return None
    path = Path(cache_dir) / f"{key}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["body"]
    except (OSError, ValueError, KeyError):
        return None


def _write_cache(cache_dir: Path | str | None, key: str, body: str) -> None:
    if not cache_dir:
        return
    path = Path(cache_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{key}.json").write_text(
            json.dumps({"key": key, "retrieved_at": _now(), "body": body}),
            encoding="utf-8")
    except OSError:
        pass                      # a cache that cannot be written is not a search failure


_ARXIV_BY_ID = "http://export.arxiv.org/api/query?id_list={q}&max_results=1"
_CROSSREF_BY_DOI = "https://api.crossref.org/works/{q}"


def lookup_doi(doi: str, *, cache_dir: Path | str | None = None, mailto: str = "",
               allow_network: bool = True, timeout: float = 30.0
               ) -> tuple[WorkIdentity | None, ProviderCall]:
    """Fetch ONE Crossref record by DOI. An identifier lookup, not a search.

    The same property `lookup_arxiv` has: no ranking, no near-miss, no title to match. A
    journal paper that prints its own DOI on page one has told this harness exactly which
    record dates it, which is worth far more than the best title match a search can offer.
    """
    ident = (doi or "").strip().rstrip(".,;)")
    key = cache_key("crossref-doi", ident, 1)
    call = ProviderCall(provider="crossref", query=f"doi={ident}",
                        query_family="TARGET_CUTOFF", requested_at=_now(), cache_key=key)
    if not ident:
        call.outcome, call.error = "NOT_ATTEMPTED", "no DOI"
        return None, call
    body = _read_cache(cache_dir, key)
    if body is not None:
        call.from_cache = True
    elif not allow_network:
        call.outcome = "NOT_ATTEMPTED"
        call.error = "no cached response and the network is not permitted"
        return None, call
    else:
        try:
            body = _fetch(_polite(_CROSSREF_BY_DOI.format(q=urllib.parse.quote(ident)),
                                  "crossref", mailto), mailto=mailto, timeout=timeout)
        except ProviderError as exc:
            call.outcome, call.error, call.http_status = exc.outcome, exc.detail, exc.status
            return None, call
        _write_cache(cache_dir, key, body)
    try:
        payload = json.loads(body)
        works = parse_crossref(json.dumps(
            {"message": {"items": [payload.get("message") or {}]}}))
    except (ValueError, KeyError, TypeError) as exc:
        call.outcome, call.error = "MALFORMED_RESPONSE", f"{type(exc).__name__}: {exc}"
        return None, call
    call.n_results = len(works)
    call.outcome = "COMPLETED" if works else "NO_RESULTS"
    call.http_status = call.http_status or 200
    return (works[0] if works else None), call


def lookup_arxiv(arxiv_id: str, *, cache_dir: Path | str | None = None, mailto: str = "",
                 allow_network: bool = True, timeout: float = 30.0
                 ) -> tuple[WorkIdentity | None, ProviderCall]:
    """Fetch ONE arXiv record by identifier. The strongest date a target paper can have.

    An identifier lookup is not a search: there is no ranking, no near-miss and no title
    to match, so the record that comes back either is this paper or the id was wrong. That
    is why an arXiv stamp the paper prints on its own first page beats every other cutoff
    source — `arxiv_submission_v1` is the day the work became public, which is exactly what
    a prior-art chronology is about.
    """
    ident = (arxiv_id or "").strip()
    key = cache_key("arxiv-id", ident, 1)
    call = ProviderCall(provider="arxiv", query=f"id_list={ident}",
                        query_family="TARGET_CUTOFF", requested_at=_now(), cache_key=key)
    if not ident:
        call.outcome, call.error = "NOT_ATTEMPTED", "no arXiv identifier"
        return None, call
    body = _read_cache(cache_dir, key)
    if body is not None:
        call.from_cache = True
    elif not allow_network:
        call.outcome = "NOT_ATTEMPTED"
        call.error = "no cached response and the network is not permitted"
        return None, call
    else:
        try:
            body = _fetch(_ARXIV_BY_ID.format(q=urllib.parse.quote(ident)), mailto=mailto,
                          timeout=timeout, accept="application/atom+xml")
        except ProviderError as exc:
            call.outcome, call.error, call.http_status = exc.outcome, exc.detail, exc.status
            return None, call
        _write_cache(cache_dir, key, body)
    try:
        works = parse_arxiv(body)
    except ProviderError as exc:
        call.outcome, call.error = exc.outcome, exc.detail
        return None, call
    call.n_results = len(works)
    call.outcome = "COMPLETED" if works else "NO_RESULTS"
    call.http_status = call.http_status or 200
    return (works[0] if works else None), call


def date_offers(title: str, *, providers: tuple[str, ...] = ("arxiv", "crossref", "openalex"),
                arxiv_id: str = "", doi: str = "",
                cache_dir: Path | str | None = None, mailto: str = "",
                allow_network: bool = True, timeout: float = 30.0
                ) -> tuple[dict[str, str], list[ProviderCall]]:
    """Every date any index offers for the TARGET paper itself, by source. For the cutoff.

    Returns offers rather than a date, because `literature.derive_cutoff` owns the
    precedence and this owns the retrieval. Splitting them is what makes "we chose the
    date most useful to the concern" checkable: the losing offers are in the record.

    A match is accepted only when the index's title agrees with the paper's after
    normalisation. A search engine given a title will always return SOMETHING, and dating
    this paper by somebody else's publication date would corrupt every chronology built on
    it — so a near-miss is no offer at all.
    """
    from . import literature

    want = literature.normalize_title(title)
    offers: dict[str, str] = {}
    calls: list[ProviderCall] = []
    if arxiv_id:
        work, call = lookup_arxiv(arxiv_id, cache_dir=cache_dir, mailto=mailto,
                                  allow_network=allow_network, timeout=timeout)
        calls.append(call)
        if work is not None and work.date:
            offers["arxiv_submission_v1"] = work.date
    if doi:
        work, call = lookup_doi(doi, cache_dir=cache_dir, mailto=mailto,
                                allow_network=allow_network, timeout=timeout)
        calls.append(call)
        if work is not None and work.date:
            offers["crossref_published"] = work.date
    if not want:
        return offers, calls
    for provider in providers:
        works, call = search(provider, title, top_k=5, cache_dir=cache_dir, mailto=mailto,
                             allow_network=allow_network, timeout=timeout,
                             query_family="TARGET_CUTOFF")
        calls.append(call)
        # THE EARLIEST EXACT-TITLE MATCH, not the first one the index happened to rank
        # highest. A title is not an identifier — an index can carry a later reprint, a
        # republication or an unrelated work under exactly the same words, and OpenAlex
        # really does return one for "Attention Is All You Need". Taking the earliest is
        # the CONSERVATIVE direction for this particular number: the cutoff is used to
        # EXCLUDE candidates, so an earlier one admits less as prior art, never more.
        matched = [w for w in works if literature.title_matches(title, w.title)]
        best = ""
        source = ""
        for work in matched:
            value = work.date or (str(work.year) if work.year else "")
            if not value:
                continue
            if not best or value < best:
                best, source = value, (work.date_source or "openalex_publication_date")
        if best and source not in offers:
            offers[source] = best
    return offers, calls


def _self_check() -> None:
    # OPENALEX PUBLISHES AN INVERTED INDEX. Rebuilt in order, or a quoted passage from an
    # OpenAlex candidate could never be located and every such concern would be refused.
    assert _openalex_abstract({"We": [0], "learn": [1], "flow": [2]}) == "We learn flow"
    assert _openalex_abstract(None) == "" and _openalex_abstract({}) == ""

    works = parse_openalex(json.dumps({"results": [{
        "id": "https://openalex.org/W1", "ids": {"openalex": "https://openalex.org/W1"},
        "doi": "https://doi.org/10.1/x", "display_name": "A Title",
        "publication_year": 2019, "publication_date": "2019-04-02",
        "authorships": [{"author": {"display_name": "A. Author"}}],
        "primary_location": {"source": {"display_name": "CVPR"}},
        "abstract_inverted_index": {"Hello": [0], "world": [1]},
        "locations": [{"landing_page_url": "https://arxiv.org/abs/1901.00001"}]}]}))
    assert len(works) == 1 and works[0].abstract == "Hello world"
    assert works[0].arxiv_id == "1901.00001" and works[0].openalex_id == "W1"
    assert works[0].date == "2019-04-02" and works[0].venue == "CVPR"

    cr = parse_crossref(json.dumps({"message": {"items": [{
        "DOI": "10.1/y", "title": ["Another Title"],
        "author": [{"given": "B.", "family": "Author"}],
        "container-title": ["ICML"], "issued": {"date-parts": [[2020, 7, 1]]},
        "abstract": "<jats:p>Body text here.</jats:p>"}]}}))
    assert cr[0].date == "2020-07-01" and cr[0].abstract == "Body text here."
    assert cr[0].date_source == "crossref_published"

    ax = parse_arxiv(
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        '<id>http://arxiv.org/abs/1504.06852v2</id><title>FlowNet</title>'
        '<summary>We learn optical flow.</summary>'
        '<published>2015-04-26T00:00:00Z</published>'
        '<author><name>A. Dosovitskiy</name></author></entry></feed>')
    # The V1 SUBMISSION DATE, which is what "public" means for a preprint.
    assert ax[0].date == "2015-04-26" and ax[0].date_source == "arxiv_submission_v1"
    assert ax[0].arxiv_id == "1504.06852v2" and ax[0].abstract == "We learn optical flow."

    s2 = parse_semanticscholar(json.dumps({"data": [{
        "paperId": "abc", "title": "T", "abstract": "A", "year": 2018,
        "publicationDate": "2018-02-03", "venue": "V",
        "externalIds": {"DOI": "10.1/z", "ArXiv": "1802.00001"}, "authors": [{"name": "C."}]}]}))
    assert s2[0].s2_id == "abc" and s2[0].arxiv_id == "1802.00001"

    # A FAILED CALL STILL RETURNS A RECORD, and the record is not a completed query.
    works, call = search("openalex", "anything", allow_network=False)
    assert works == [] and call.outcome == "NOT_ATTEMPTED" and not call.completed
    _, unknown = search("google-scholar", "q")
    assert unknown.outcome == "NOT_ATTEMPTED" and "unknown provider" in unknown.error

    # A CACHED RESPONSE REPRODUCES THE CANDIDATE SET WITH NO NETWORK.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        _write_cache(tmp, cache_key("crossref", "q", 20), json.dumps(
            {"message": {"items": [{"DOI": "10.1/c", "title": ["Cached"],
                                    "issued": {"date-parts": [[2017]]}}]}}))
        got, cached_call = search("crossref", "q", cache_dir=tmp, allow_network=False)
        assert cached_call.from_cache and cached_call.completed
        assert [w.doi for w in got] == ["10.1/c"] and got[0].year == 2017

    print("harness.literature_providers self-check ok")


if __name__ == "__main__":       # pragma: no cover
    _self_check()
