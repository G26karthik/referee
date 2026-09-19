"""Bounded prior-art investigation: the deterministic half of the LITERATURE_SEARCH route.

`python -m harness.literature` runs the self-check.

**One rule shapes everything in this file, and it is not symmetric:**

    finding prior art may support a novelty concern;
    failing to find prior art does NOT establish novelty.

That is encoded rather than stated. There is no authority, no disposition, no evidence
state and no function here that means "novel": `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES`
is the empty tuple, `PriorArtFact.establishes_novelty` is a membership test in it, and a
bounded search that completed and matched nothing produces
`SEARCH_COMPLETED_NO_MATCH_FOUND`, whose evidence state is a fact about the SEARCH. A
reader asking this module whether a paper is new gets nothing back, because the question
is not expressible.

**What the route does answer** is bounded: does an earlier work appear to make the same
specific contribution; does an earlier work materially overlap an explicit novelty claim;
does the paper claim "first" where an earlier candidate is relevant; is there an
apparently omitted prior work a human referee should inspect. Every one of those is a
question for a referee, and none of them is a verdict.

**Three deterministic halves and one model half, kept apart.** The harness extracts the
claims (they are quotations this document contains, minted by `claims.mint`), establishes
the target's earliest public date, canonicalises and dates the candidates, and decides the
chronology. What a model may do is propose queries and propose a RELATION between one
claim and one candidate. Whether that relation carries any authority is decided here, from
what actually verified — which is the same arrangement `claimlink` and `artifact_evidence`
have, for the third time and for the same reason.

**Chronology is the half that must be deterministic.** A candidate dated after the
target's cutoff cannot be prior art for it whatever a reading says, and `chronology`
refuses to order two dates more precisely than their sources allow: a proceedings year
does not put one same-year paper before another, so that case is
CONTEMPORANEOUS_UNRESOLVED and not a quiet PREDATES. The date each side used and the
source it came from are both recorded, so "the date most useful to the concern" would be
visible if it ever happened.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date as _date

from . import claims, materiality
from .artifacts import (CANDIDATE_CITATION_STATES, CHRONOLOGY_STATES,
                        CONCERNING_RELATIONS, CUTOFF_SOURCES, CUTOFF_STATES,
                        LITERATURE_AUTHORITY, LITERATURE_REFUSALS, LITERATURE_RELATIONS,
                        NOVELTY_ESTABLISHING_AUTHORITIES, PRIOR_ART_BINDING_BASES,
                        PRIOR_ART_CAPABLE_AUTHORITIES, PRIOR_ART_ELIGIBLE_CHRONOLOGY,
                        LiteratureSearch, PaperDoc, PriorArtFact, SearchClaim,
                        SearchCutoff, SearchProtocol, WorkIdentity)

# --------------------------------------------------------------------------------------
# WHAT THE PAPER CLAIMS FOR ITSELF
# --------------------------------------------------------------------------------------
# The search targets are QUOTATIONS THIS DOCUMENT CONTAINS, not a model's summary of what
# the paper is about. A scan for the words a paper uses when it claims priority, run over
# the paper's own summary of itself, then minted through `claims.mint` exactly as every
# other quotation in this system is — so a search target that did not resolve is dropped
# before a single query is issued.
#
# The markers are surface forms and nothing more. `is_novelty_claim` below is the harness
# half of "is this actually a novelty claim", and it is deliberately the SAME list: a
# reviewer that proposes a concern against a sentence claiming nothing is refused by the
# same rule that would have refused the sentence as a search target.
_NOVELTY_MARKERS = (
    # priority
    "first to", "the first", "for the first time", "to the best of our knowledge",
    "the first work", "novel",
    # contribution. Broader than the priority list on purpose, and measured rather than
    # guessed: on the eight-paper corpus a list without these missed `apt-icml` entirely,
    # whose contribution sentences say "We design APT to..." and "our experiments show",
    # and reduced `sanchez24a-icml` to one claim. A paper that states what it contributes
    # without using the word "novel" is the common case, not the exception.
    "we introduce", "we propose", "we present", "we design", "we develop", "we build",
    "we demonstrate", "we show that", "we devise", "this paper presents",
    "this work presents", "this paper proposes", "our contribution", "our method",
    "our approach", "our framework", "our experiments show",
    "unlike prior", "unlike existing", "unlike previous", "in contrast to prior",
    "in contrast to existing",
)

# Markers that assert PRIORITY rather than merely describing a contribution. Kept apart
# because §13's strongest binding basis — `explicit_first_claim_met` — needs the target to
# have claimed to be first at something narrow, and "we propose a method" is not that.
_PRIORITY_MARKERS = ("first to", "the first", "for the first time", "the first work",
                     "to the best of our knowledge")

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A word that could be a method name: an acronym, or a CamelCase coinage. Both are what a
# paper actually calls its contribution, and both are searchable strings.
_METHOD_TOKEN = re.compile(r"\b([A-Z][A-Za-z]*[A-Z][A-Za-z0-9-]*|[A-Z]{2,}[0-9]*)\b")
_STOP_METHOD = {"WE", "THE", "OUR", "THIS", "IN", "ON", "AND", "FOR", "WITH", "ACL",
                "ICML", "ICLR", "CVPR", "NEURIPS", "IEEE", "ACM", "ARXIV", "PDF",
                "GPU", "CPU", "API", "SOTA", "LLM", "LLMS", "AI", "ML", "NLP", "CV"}


def is_novelty_claim(text: str) -> bool:
    """Does this sentence claim novelty, priority or a contribution AT ALL?

    The harness half of §11's target-side verification. A reviewer may propose a prior-art
    concern against any sentence it likes; a sentence that claims nothing cannot carry one,
    and `target_claim_not_a_novelty_claim` is the refusal that says so. Surface forms only
    — this is a filter, not a reading.
    """
    low = (text or "").lower()
    return any(m in low for m in _NOVELTY_MARKERS)


def claims_priority(text: str) -> bool:
    """Does it claim to be FIRST at something, as opposed to merely being a contribution?"""
    low = (text or "").lower()
    return any(m in low for m in _PRIORITY_MARKERS)


def method_names(doc: PaperDoc) -> list[str]:
    """The names the paper gives its own contribution, FROM ITS TITLE ONLY.

    Title-only, and measured rather than assumed. Scanning the abstract too produced
    "LMs", "GPT-2", "LOCAL" and "CFG" as method names on the eight-paper corpus and spent
    a third of the query budget searching an index for the string "LOCAL" — a method name
    is what makes an exact-phrase query worth issuing, and a wrong one buys a page of
    somebody else's work.

    The strongest form is the token before a colon: papers name their contribution there
    ("APT:", "WeatherGen:", "LDREG:"), which is also why it survives an ALL-CAPS title
    that defeats the CamelCase grammar.
    """
    title = target_title(doc)
    if not title:
        return []
    out: list[str] = []
    head = title.split(":", 1)[0].strip() if ":" in title else ""
    if head and 3 <= len(head) <= 24 and " " not in head:
        out.append(head)
    for token in _METHOD_TOKEN.findall(title):
        if token.upper() in _STOP_METHOD or len(token) < 3 or token in out:
            continue
        out.append(token)
    return out[:3]


def novelty_claims(doc: PaperDoc, *, limit: int = 6) -> list[SearchClaim]:
    """The paper's own explicit novelty, priority and contribution claims, addressed.

    Read out of the Abstract, the Introduction and the Conclusion, because that is where a
    paper states what it claims for itself — the same three places `claimlink` looks for a
    headline, and for the same reason. A sentence that `claims.mint` cannot place uniquely
    is DROPPED: a search target whose address this harness cannot re-derive is a search
    for something nobody can check afterwards.
    """
    out: list[SearchClaim] = []
    seen: set[str] = set()
    by_idx = {int(s.section_idx): s for s in doc.sections}
    for idx in _claim_sections(doc):
        text = ((by_idx.get(idx).text if by_idx.get(idx) else "") or "")
        for sentence in _SENTENCE_END.split(text):
            sentence = sentence.strip()
            if not (40 <= len(sentence) <= 400) or not is_novelty_claim(sentence):
                continue
            minted = claims.mint(doc, sentence)
            if not minted.resolved or minted.quote in seen:
                continue
            seen.add(minted.quote)
            low = sentence.lower()
            marker = next((m for m in _NOVELTY_MARKERS if m in low), "")
            out.append(SearchClaim(
                claim_id=f"lit-{len(out) + 1:02d}",
                ref=minted.ref, quote=minted.quote,
                claim_kind=("NOVELTY_MARKER" if claims_priority(sentence) else "CONTRIBUTION"),
                marker=marker, concepts=_concepts(sentence)))
            if len(out) >= limit:
                return out
    return out


def _claim_sections(doc: PaperDoc) -> list[int]:
    """The `section_idx` values of the Abstract, the Introduction and the Conclusion.

    `section_idx` is the DOCUMENT's own numbering and not a position in the list — the
    same distinction `materiality._section` makes, and the reason it looks sections up
    rather than slicing them. Back matter is excluded by `body_end_section_idx`, so a
    reference-list fragment mis-parsed as a lettered heading cannot become a place this
    route reads novelty claims out of.
    """
    idxs: list[int] = []
    for finder in (materiality.abstract_section_idx, materiality.conclusion_section_idx):
        i = finder(doc)
        if i >= 0 and i not in idxs:
            idxs.append(i)
    end = doc.body_end_section_idx
    for section in doc.sections:
        i = int(section.section_idx)
        if end >= 0 and i >= end:
            continue
        head = " ".join((section.title or "").split()).lower()
        if "introduction" in head and i not in idxs:
            idxs.append(i)
    return [i for i in idxs if i >= 0]


_CONCEPT_STOP = {"the", "this", "that", "with", "from", "into", "which", "while", "their",
                 "these", "those", "than", "such", "also", "been", "were", "have", "has",
                 "our", "we", "propose", "present", "introduce", "novel", "first", "new",
                 "work", "paper", "approach", "method", "results", "show", "shows",
                 "demonstrate", "best", "knowledge", "prior", "existing", "previous"}


def _concepts(sentence: str) -> list[str]:
    """The content words a query can be built from. No stemming, no synonyms, no model."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", sentence or "")
    out: list[str] = []
    for w in words:
        if w.lower() in _CONCEPT_STOP or w.lower() in out:
            continue
        out.append(w.lower())
    return out[:12]


# --------------------------------------------------------------------------------------
# THE BOUNDED SEARCH PROTOCOL
# --------------------------------------------------------------------------------------
# "Route exhausted" for this route means THE DECLARED PROTOCOL COMPLETED, and these are
# the bounds it declares. They are published in the record rather than buried in a
# constant, because a search budget that a reader cannot see is a search budget that can
# quietly become a claim about the literature.
QUERY_FAMILIES = (
    "EXACT_METHOD_NAME",        # A: the name the paper gives its own contribution
    "ACRONYM_EXPANSION",        # B: the same name spelled out, where the paper spells it
    "CONTRIBUTION_DECOMPOSITION",  # C: the claim sentence's own content words
    "TASK_AND_MECHANISM",       # D: what it does, plus how
    "BENCHMARK_AND_MECHANISM",  # E: what it is measured on, plus how
    "TERMINOLOGY_VARIANT",      # F: close spellings of the same idea
)

DEFAULT_PROTOCOL = SearchProtocol(
    query_families=list(QUERY_FAMILIES[:4]),
    providers_required=["openalex", "crossref", "arxiv"],
    top_k=20, max_queries_per_claim=4, max_claims=6, max_candidates_reviewed=12,
    notes="bounded. The literature is not enumerable: completing this protocol means "
          "these queries reached these indexes and their top-k were adjudicated, and it "
          "means nothing whatever about rank k+1.",
)


def deterministic_queries(doc: PaperDoc, claim: SearchClaim, *,
                          protocol: SearchProtocol | None = None) -> list[tuple[str, str]]:
    """[(family, query)] built from the PAPER's own words, with no model involved.

    The gated query proposer can add families D through F; with its gate closed the route
    still searches, on the paper's own method name and the claim sentence's own content
    words. That is the property every model channel in this system has, and it is what
    makes this one assessable: a route that only runs when a model runs cannot be measured
    against one that does not.
    """
    protocol = protocol or DEFAULT_PROTOCOL
    out: list[tuple[str, str]] = []
    # THE TITLE IS THE BEST EXACT-PHRASE QUERY A PAPER HAS, when extraction recovered one
    # this harness will stand behind. It is also the query most likely to return the
    # target paper itself, which is harmless: the target cannot predate its own cutoff.
    title = target_title(doc)
    if title:
        out.append(("EXACT_METHOD_NAME", title))
    for name in method_names(doc)[:1]:
        out.append(("EXACT_METHOD_NAME", name))
    head = " ".join(claim.concepts[:6])
    if head:
        out.append(("CONTRIBUTION_DECOMPOSITION", head))
    tail = " ".join(claim.concepts[:3] + claim.concepts[6:9])
    if tail.strip() and tail != head:
        out.append(("TASK_AND_MECHANISM", tail))
    return out[:max(1, protocol.max_queries_per_claim)]


# --------------------------------------------------------------------------------------
# WHEN THE TARGET BECAME PUBLIC
# --------------------------------------------------------------------------------------
# Strongest first, and the order IS the rule: the first arXiv submission is what "public"
# means for a paper that had one, and a proceedings year is a last resort that cannot
# order two papers inside it.
_CUTOFF_PRECEDENCE = ("arxiv_submission_v1", "crossref_published",
                      "openalex_publication_date", "operator_supplied", "proceedings_year")


def derive_cutoff(offers: dict[str, str], *, evidence: str = "") -> SearchCutoff:
    """The target's earliest defensible public date, from every date any source offered.

    EVERY offer is recorded, and then one is chosen by a fixed precedence. Recording them
    all is the check: choosing the date most convenient for a concern would be visible in
    the artifact rather than invisible in a function.

    A year-only source yields YEAR_ONLY, which is not a weak ESTABLISHED — it is the state
    in which a same-year candidate must be refused as unorderable.
    """
    offers = {k: (v or "").strip() for k, v in (offers or {}).items() if (v or "").strip()}
    for source in _CUTOFF_PRECEDENCE:
        raw = offers.get(source, "")
        if not raw:
            continue
        iso = _iso(raw)
        if not iso:
            continue
        year_only = source == "proceedings_year" or len(iso) < 10
        return SearchCutoff(
            date=iso, source=source,
            state="YEAR_ONLY" if year_only else "ESTABLISHED",
            evidence=evidence or f"{source} reported {raw}", candidates=dict(offers))
    if offers:
        return SearchCutoff(state="AMBIGUOUS", candidates=dict(offers),
                            evidence="dates were offered and none parsed to a usable date")
    return SearchCutoff(state="UNKNOWN", evidence=evidence or "no source dated this paper")


# The paper's OWN arXiv identifier, and a year anchored to a publication statement.
# Both are read off PAGE ONE only. That bound is the whole safety of them: every paper
# cites other people's arXiv ids and other people's proceedings years, and a scan over the
# body would date this paper by somebody else's publication.
_OWN_ARXIV = re.compile(r"arxiv[:\s]*(\d{4}\.\d{4,5})", re.I)
_PROCEEDINGS = re.compile(
    r"(?:proceedings|copyright|©|\(c\)|conference on|workshop|"
    r"published as|camera[- ]ready)[^.]{0,140}?\b(19[89]\d|20[0-4]\d)\b", re.I)


def front_matter(doc: PaperDoc) -> str:
    """Page one, which is where a paper states what it is. Never the body.

    Falls back to the first three sections only when no section carries a page number,
    because an unpaginated extraction is a limit of our parsing and not a licence to read
    the whole document for a date.
    """
    page_one = " ".join((s.text or "") for s in doc.sections if int(s.page_start or 1) <= 1)
    return page_one if page_one.strip() else " ".join(
        (s.text or "") for s in doc.sections[:3])


_OWN_DOI = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")


def own_doi(doc: PaperDoc) -> str:
    """The DOI the paper prints on its own first page, or ''.

    Second only to an arXiv stamp, and for the same reason: a DOI is an identifier, so
    looking it up is not a search — there is no ranking and no title to match, and the
    record that comes back either is this paper or the identifier was misprinted. Read
    off page one only, because a reference list is nothing but other people's DOIs.
    """
    found = _OWN_DOI.findall(front_matter(doc)[:6000])
    return found[0].rstrip(".,;)") if found else ""


def own_arxiv_id(doc: PaperDoc) -> str:
    """The arXiv id the paper prints on its own first page, or ''. The strongest source.

    An arXiv stamp on page one is the paper telling you its own identifier, and looking
    that id up gives the v1 submission date — the one date that is unambiguously "when
    this became public". Proceedings PDFs mostly carry no such stamp, which is why this
    is one source among several rather than the only one.
    """
    found = _OWN_ARXIV.findall(front_matter(doc)[:6000])
    return found[0] if found else ""


def proceedings_year(doc: PaperDoc) -> str:
    """A year ANCHORED to a publication statement on page one, or ''.

    Anchored rather than scanned. "The latest year printed on page one" would have worked
    on more of the corpus and would have been wrong whenever a first page carries a
    citation, and a cutoff that is a year too late ADMITS work as prior art that is not —
    which is the direction this route must never err in. A paper whose date cannot be
    anchored gets no year here and the route says the cutoff is unknown.
    """
    hits = _PROCEEDINGS.findall(front_matter(doc)[:8000])
    return sorted(hits)[-1] if hits else ""


# An extracted "title" that is really the author line. Both are real: two of the eight
# corpus papers have `doc.title` set to "Jayesh Singla * 1 Ananye Agarwal * 1 ..." and
# "Guillaume V. Sanchez * 1 2 ...", and searching an index for an author line returns
# somebody else's paper — which would then DATE this one. A title this harness cannot
# trust yields no title lookup at all, which is a limitation to report rather than route
# around.
_AUTHOR_LINE = re.compile(r"\*\s*\d|\b\d\s+[A-Z][a-z]+\s+[A-Z]")


# Publication boilerplate a title-shaped extraction picks up off the top of page one.
# Generic rather than paper-specific: these are the strings a PUBLISHER prints, and three
# of the eight corpus papers have one of them as `doc.title` — "Proceedings of the 64th
# Annual Meeting of the Association...", "Accepted: 6 November 2024 / Published online:
# ...". Searching an index for one returns an unrelated work, which would then date this
# paper by somebody else's publication.
_TITLE_BOILERPLATE = ("proceedings of", "accepted:", "received:", "published online",
                      "copyright", "downloaded from", "preprint", "under review as",
                      "published as a conference paper", "to appear in",
                      "annual meeting of the", "this wacv paper", "this cvpr paper",
                      "this iccv paper")


def target_title(doc: PaperDoc) -> str:
    """The paper's title, or '' when what extraction recovered is not one.

    RETURNING NOTHING IS THE POINT. A title lookup is what dates the target, and a lookup
    on the wrong string does not fail — it succeeds, with somebody else's paper, and every
    chronological judgement built on it is then wrong in a way nothing downstream can
    detect. A title this harness cannot trust produces no lookup and an honest
    CUTOFF UNKNOWN.
    """
    title = " ".join((doc.title or "").split())
    low = title.lower()
    if len(title) < 12 or _AUTHOR_LINE.search(title):
        return ""
    if any(low.startswith(b) or b in low for b in _TITLE_BOILERPLATE):
        return ""
    return title


def title_matches(want: str, got: str, *, min_prefix: int = 40) -> bool:
    """Is `got` (an index's title) the same work as `want` (ours), after normalisation?

    Equality, or one a PREFIX of the other with at least `min_prefix` characters in
    common. The prefix rule exists because extraction truncates: the corpus's own titles
    come back as "APT: Adaptive Pruning and Tuning Pretrained Language Models for" and
    "WeatherGen: A Unified Diverse Weather Generator for LiDAR Point Cloud", and an
    equality test dates neither paper. Forty normalised characters of exact prefix is a
    strong identifier; fuzzy similarity is not, and is deliberately not used.
    """
    a, b = normalize_title(want), normalize_title(got)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= min_prefix and long.startswith(short)


def _iso(raw: str) -> str:
    """'2024-03-07' | '2024-03' | '2024' -> an ISO prefix, or '' if it is not a date."""
    raw = (raw or "").strip()
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", raw)
    if not m:
        return ""
    year = int(m.group(1))
    if not 1900 <= year <= 2100:
        return ""
    if m.group(3):
        try:
            _date(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return f"{year:04d}"
        return f"{year:04d}-{m.group(2)}-{m.group(3)}"
    if m.group(2):
        return f"{year:04d}-{m.group(2)}"
    return f"{year:04d}"


# --------------------------------------------------------------------------------------
# CHRONOLOGY
# --------------------------------------------------------------------------------------
def chronology(cutoff: SearchCutoff | None, work: WorkIdentity | None) -> tuple[str, str]:
    """(state, basis). Deterministic, and never more precise than the two sources allow.

    THE BOUNDARY CASES ARE THE POINT. Same year with a day on both sides orders normally.
    Same year with a year on either side does NOT order — a proceedings year does not put
    one 2024 paper before another 2024 paper, and answering PREDATES there would
    manufacture priority out of a rounding. An undated candidate is CANDIDATE_DATE_UNKNOWN
    and not "probably earlier".
    """
    if cutoff is None or cutoff.state in ("UNKNOWN", "AMBIGUOUS") or not cutoff.date:
        return "TARGET_CUTOFF_UNKNOWN", "the target's earliest public date is not established"
    theirs = _iso((work.date if work else "") or "") or (
        f"{work.year:04d}" if work and work.year else "")
    if not theirs:
        return "CANDIDATE_DATE_UNKNOWN", "no index dated the candidate"
    ours = cutoff.date
    basis = (f"candidate {theirs} ({(work.date_source if work else '') or 'unknown source'}) "
             f"vs target {ours} ({cutoff.source})")
    precision = min(len(ours), len(theirs))
    a, b = theirs[:precision], ours[:precision]
    if a < b:
        return "PREDATES_CUTOFF", basis
    if a > b:
        return "POSTDATES_CUTOFF", basis
    return "CONTEMPORANEOUS_UNRESOLVED", basis + " — equal at the precision both are known to"


# --------------------------------------------------------------------------------------
# ONE PAPER IS NOT FOUR PAPERS
# --------------------------------------------------------------------------------------
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lowercase, punctuation-free, whitespace-collapsed. For the FALLBACK key only."""
    return _WS.sub(" ", _PUNCT.sub(" ", (title or "").lower())).strip()


def normalize_doi(doi: str) -> str:
    d = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.strip("/")


def normalize_arxiv(arxiv_id: str) -> str:
    """'arXiv:2301.01234v3' -> '2301.01234'. The VERSION is dropped deliberately.

    v1 and v3 of one preprint are one work. Keeping the version would make a candidate set
    count a paper twice for having been revised — and would also lose the fact that what
    matters for chronology is v1, which is when it became public.
    """
    a = (arxiv_id or "").strip().lower()
    a = a.replace("arxiv:", "").strip()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/"):
        if a.startswith(prefix):
            a = a[len(prefix):]
    return re.sub(r"v\d+$", "", a).strip("/")


def canonical_id(work: WorkIdentity) -> str:
    """The strongest identifier this work has, and the key it folds on.

    DESCENDING STRENGTH, and the order is the whole of the rule: a DOI identifies a work,
    an arXiv id identifies a preprint, an index id identifies a record IN THAT INDEX, and a
    title identifies nothing on its own. The title fallback is conjunctive — title AND
    first author AND year — because two different works with similar titles must not merge,
    and over a candidate set drawn from several indexes that happens.
    """
    doi = normalize_doi(work.doi)
    if doi:
        return f"doi:{doi}"
    arx = normalize_arxiv(work.arxiv_id)
    if arx:
        return f"arxiv:{arx}"
    if (work.openalex_id or "").strip():
        return f"openalex:{work.openalex_id.strip().lower()}"
    if (work.s2_id or "").strip():
        return f"s2:{work.s2_id.strip().lower()}"
    title = normalize_title(work.title)
    if not title:
        return ""
    first = normalize_title(work.authors[0]) if work.authors else ""
    return f"title:{title}|{first}|{work.year or 0}"


def merge_works(works: list[WorkIdentity]) -> list[WorkIdentity]:
    """Fold provider records into canonical works, keeping every alias.

    What survives a merge is the UNION of the identifiers and the EARLIEST date, because
    the arXiv preprint and the conference version of one paper became public when the
    preprint did — and prior art is about when the work was public, not about when a
    publisher printed it.
    """
    by_key: dict[str, WorkIdentity] = {}
    order: list[str] = []
    for w in works:
        key = canonical_id(w)
        if not key:
            continue
        if key not in by_key:
            merged = w.model_copy(deep=True)
            merged.canonical_id = key
            merged.aliases = sorted(set(merged.aliases) | _identifiers(w))
            by_key[key] = merged
            order.append(key)
            continue
        cur = by_key[key]
        cur.doi = cur.doi or w.doi
        cur.arxiv_id = cur.arxiv_id or w.arxiv_id
        cur.openalex_id = cur.openalex_id or w.openalex_id
        cur.s2_id = cur.s2_id or w.s2_id
        cur.title = cur.title or w.title
        cur.venue = cur.venue or w.venue
        cur.url = cur.url or w.url
        cur.authors = cur.authors or list(w.authors)
        if len(w.abstract or "") > len(cur.abstract or ""):
            cur.abstract, cur.abstract_sha256 = w.abstract, w.abstract_sha256
        theirs, ours = _iso(w.date or ""), _iso(cur.date or "")
        if theirs and (not ours or theirs < ours):
            cur.date, cur.date_source = theirs, w.date_source
        if w.year and (not cur.year or w.year < cur.year):
            cur.year = w.year
        cur.aliases = sorted(set(cur.aliases) | _identifiers(w))
        cur.providers = sorted(set(cur.providers) | set(w.providers))
    return [by_key[k] for k in order]


def _identifiers(w: WorkIdentity) -> set[str]:
    out = set()
    if normalize_doi(w.doi):
        out.add(f"doi:{normalize_doi(w.doi)}")
    if normalize_arxiv(w.arxiv_id):
        out.add(f"arxiv:{normalize_arxiv(w.arxiv_id)}")
    if (w.openalex_id or "").strip():
        out.add(f"openalex:{w.openalex_id.strip().lower()}")
    if (w.s2_id or "").strip():
        out.add(f"s2:{w.s2_id.strip().lower()}")
    return out


def trim_for_record(works: list[WorkIdentity], keep: set[str]) -> list[WorkIdentity]:
    """Keep every candidate's IDENTITY; keep the retrieved TEXT only where it was read.

    A paper's bounded search retrieves several hundred distinct works and the record has
    to list all of them — the deduplicated count is a published bound, and a reader who
    cannot see the candidate set cannot judge the search. What the record does not need is
    six hundred abstracts: one corpus paper's search file came to 976 KB, almost all of it
    text nobody looked at.

    So the text is kept for exactly the works a reader was SHOWN, which are the only ones
    a quotation could have come from, and every other work keeps its `abstract_sha256`.
    Nothing is lost that cannot be recovered: the raw provider responses are in the cache
    under their own keys, which is what makes the search reproducible in the first place.
    """
    out: list[WorkIdentity] = []
    for work in works:
        if work.canonical_id in keep or not work.abstract:
            out.append(work)
            continue
        lean = work.model_copy(deep=True)
        lean.abstract = ""
        out.append(lean)
    return out


# A NAME THE PAPER SAYS IT INTRODUCES. Read out of the claim sentence itself rather than
# the title, because the papers this matters most for are exactly the ones whose title
# extraction failed. Same grammar as `method_names`: an acronym or a CamelCase coinage.
_INTRODUCES = ("introduce", "introduced", "present", "presents", "propose", "proposed",
               "we call", "named", "dubbed", "termed")


def coined_names(sentence: str) -> set[str]:
    """Names a sentence claims the paper INTRODUCES. Empty unless it says so."""
    low = (sentence or "").lower()
    if not any(word in low for word in _INTRODUCES):
        return set()
    return {t for t in _METHOD_TOKEN.findall(sentence or "")
            if t.upper() not in _STOP_METHOD and len(t) >= 4}


def same_work(doc: PaperDoc, work: WorkIdentity | None) -> bool:
    """Is this candidate the TARGET PAPER ITSELF — its preprint, or its own record?

    A PAPER IS NOT PRIOR ART FOR ITSELF, and the first live run found exactly that: the
    ACL paper's own arXiv preprint came back verified at both ends and earlier than the
    proceedings, because it IS earlier than the proceedings. Two deterministic tests, both
    identity rather than resemblance: the candidate carries an identifier the paper prints
    on its own first page, or its title is the paper's title.
    """
    if work is None:
        return False
    own_ids = set()
    if own_arxiv_id(doc):
        own_ids.add(f"arxiv:{normalize_arxiv(own_arxiv_id(doc))}")
    if own_doi(doc):
        own_ids.add(f"doi:{normalize_doi(own_doi(doc))}")
    if own_ids & (set(work.aliases) | {canonical_id(work)}):
        return True
    mine = target_title(doc)
    return bool(mine) and title_matches(mine, work.title)


def may_be_same_work(doc: PaperDoc, claim: SearchClaim, work: WorkIdentity | None) -> bool:
    """The case this harness CANNOT decide, named so it can be refused out loud.

    With no title this harness will stand behind, a candidate whose title carries the very
    name the paper says it introduces is either that paper's own earlier version or a real
    priority conflict. Nothing available here separates them: the authors are not reliably
    extracted, the identifiers are not printed, and the reviewer's opinion is the thing
    being checked. A paper WITH a usable title is not subject to this — there the identity
    question is already answered by `same_work`, and a shared coined name is then a
    terminology collision or a genuine conflict, which is a referee's call to make.
    """
    if work is None or target_title(doc):
        return False
    names = coined_names(claim.quote)
    if not names:
        return False
    title = normalize_title(work.title)
    return any(normalize_title(name) in title for name in names)


def citable(work: WorkIdentity | None) -> bool:
    """Does this record identify a WORK, or merely a row in somebody's index?

    A candidate with no DOI and no arXiv id and no index id is a title, and a title is not
    something a referee can be asked to go and read.
    """
    if work is None:
        return False
    return bool(normalize_doi(work.doi) or normalize_arxiv(work.arxiv_id)
                or (work.openalex_id or "").strip() or (work.s2_id or "").strip())


# --------------------------------------------------------------------------------------
# WHAT THE TARGET ALREADY CITES
# --------------------------------------------------------------------------------------
def bibliography_text(doc: PaperDoc) -> str:
    """The target's own reference list, as text. '' when extraction did not recover one.

    `body_end_section_idx` is the boundary the parser already computes. Using it rather
    than searching for a heading matters: a bibliography fragment mis-parsed as a lettered
    heading is exactly the failure that field exists to survive.
    """
    end = doc.body_end_section_idx
    if end is None or end < 0 or end >= len(doc.sections):
        return ""
    return "\n".join((s.text or "") for s in doc.sections[end:])


def citation_state(doc: PaperDoc, work: WorkIdentity | None, *,
                   bibliography: str | None = None) -> tuple[str, str]:
    """(state, evidence). Does the TARGET already cite this candidate?

    NOT A REFUSAL EITHER WAY, and that is §8's whole point. A cited predecessor may still
    challenge a novelty claim — but the concern is then "the stated distinction may be
    insufficient", which is a scientific question, and not "the authors omitted prior
    art", which is an accusation about their scholarship. Printing the second where the
    first is true would be this system inventing misconduct out of a search result.

    A bibliography extraction did not recover is BIBLIOGRAPHY_UNAVAILABLE and never
    APPARENTLY_UNCITED: "we could not read the reference list" and "it is not in the
    reference list" are opposite facts, and the second is the one that names the authors.
    """
    text = bibliography if bibliography is not None else bibliography_text(doc)
    if not (text or "").strip():
        return "BIBLIOGRAPHY_UNAVAILABLE", "no reference list was recovered from the paper"
    if work is None:
        return "BIBLIOGRAPHY_UNAVAILABLE", "no candidate"
    flat = normalize_title(text)
    doi = normalize_doi(work.doi)
    if doi and doi in (text or "").lower():
        return "CITED_BY_TARGET", f"the reference list contains the DOI {doi}"
    arx = normalize_arxiv(work.arxiv_id)
    if arx and arx in (text or "").lower():
        return "CITED_BY_TARGET", f"the reference list contains arXiv:{arx}"
    title = normalize_title(work.title)
    if len(title) >= 25 and title in flat:
        return "CITED_BY_TARGET", "the reference list contains the candidate's title"
    return "APPARENTLY_UNCITED", ("neither the candidate's identifiers nor its title occur "
                                 "in the recovered reference list")


# --------------------------------------------------------------------------------------
# THE AUTHORITY LADDER
# --------------------------------------------------------------------------------------
def _fact_id(claim_id: str, key: str) -> str:
    return "pa-" + hashlib.sha256(f"{claim_id}|{key}".encode()).hexdigest()[:10]


def bind_prior_art(doc: PaperDoc, claim: SearchClaim, work: WorkIdentity | None, *,
                   relation: str = "INSUFFICIENT_EVIDENCE", candidate_quote: str = "",
                   cutoff: SearchCutoff | None = None, binding_basis: str = "",
                   reviewer_note: str = "", bibliography: str | None = None) -> PriorArtFact:
    """One adjudicated (claim, candidate) pair. The authority is decided HERE, from checks.

    Four requirements, and each has its own refusal so that a reader can tell which half
    was missing:

      the TARGET side     the quoted claim re-mints to the same address, and it actually
                          claims novelty, priority or a contribution;
      the CANDIDATE side  the record identifies a work (a DOI, an arXiv id, an index id),
                          and the quoted passage is present in the text THIS HARNESS
                          retrieved — not in a search-engine snippet and not in the
                          reviewer's account of it;
      CHRONOLOGY          the candidate is public before the target's established cutoff;
      the RELATION        it is one the reviewer's own vocabulary says raises a prior-art
                          question at all.

    All four give ENDPOINTS_VERIFIED_LITERATURE_CONCERN — two real works, a real passage,
    a verified chronology, and an OVERLAP that is still the reviewer's reading. Level 3
    needs `binding_basis` to be one of the deterministic ones AND the claim to have been a
    priority claim, and it is deliberately hard: **a corpus on which it is zero prints
    zero.**
    """
    fid = _fact_id(claim.claim_id, canonical_id(work) if work else (candidate_quote or "?"))
    relation = (relation or "").strip().upper()
    if relation not in LITERATURE_RELATIONS:
        relation = "INSUFFICIENT_EVIDENCE"
    cite_state, cite_why = citation_state(doc, work, bibliography=bibliography)
    chrono, chrono_basis = chronology(cutoff, work)

    def refuse(reason: str, statement: str, *, authority: str = "NONE") -> PriorArtFact:
        return PriorArtFact(
            fact_id=fid, claim_id=claim.claim_id, target_ref=claim.ref,
            target_quote=claim.quote, candidate=work, candidate_quote=candidate_quote,
            candidate_quote_located=located, relation=relation, statement=statement,
            authority=authority, chronology=chrono, chronology_basis=chrono_basis,
            citation_state=cite_state, refusal=reason, reviewer_note=reviewer_note)

    located = bool(candidate_quote.strip()) and _passage_present(work, candidate_quote)

    minted = claims.mint(doc, claim.quote)
    if not minted.resolved or minted.ref != claim.ref:
        return refuse("target_claim_unaddressed",
                      "the claim this concern is about does not re-mint to the address it "
                      "was searched under, so there is no paper half to hold anything "
                      "against.")
    if not is_novelty_claim(claim.quote):
        return refuse("target_claim_not_a_novelty_claim",
                      "the quoted sentence resolves in the paper and claims no novelty, "
                      "priority or contribution, so an earlier work overlapping it would "
                      "contradict nothing the paper asserted.")
    if same_work(doc, work):
        return refuse("candidate_is_the_target_itself",
                      "the candidate is this paper: it carries an identifier the paper "
                      "prints on its own first page, or its title. A paper's own preprint "
                      "is earlier than its proceedings version and is prior art for "
                      "nothing.")
    if may_be_same_work(doc, claim, work):
        return refuse("candidate_may_be_the_target_itself",
                      f"the candidate's title carries {sorted(coined_names(claim.quote))}, "
                      f"which this sentence says the paper introduces, and extraction "
                      f"recovered no title this harness will stand behind — so whether "
                      f"this is the paper's own earlier version or somebody else's work of "
                      f"the same name cannot be settled here. Reporting it as prior art "
                      f"would be an accusation this harness cannot support; it is recorded "
                      f"for a human to glance at.")
    if not citable(work):
        return refuse("candidate_identity_unestablished",
                      "the candidate carries no DOI, no arXiv id and no index id, so it "
                      "names no work a referee could open.")
    if not candidate_quote.strip():
        return refuse("insufficient_candidate_evidence",
                      "no passage from the candidate was offered, so the overlap rests on "
                      "its title alone.")
    if not located:
        return refuse("candidate_quote_unresolved",
                      "the quoted passage is not in the candidate text this harness "
                      "retrieved. A search-engine snippet is not the work.")
    if chrono == "TARGET_CUTOFF_UNKNOWN":
        return refuse("target_cutoff_unknown",
                      "the target's own earliest public date is not established, so no "
                      "chronological claim can be made in either direction.")
    if chrono == "CANDIDATE_DATE_UNKNOWN":
        return refuse("candidate_date_unknown",
                      "no index dated the candidate, and an undated work is not an "
                      "earlier one.")
    if chrono not in PRIOR_ART_ELIGIBLE_CHRONOLOGY:
        return refuse("candidate_not_earlier",
                      f"the candidate is not established as public before the target: "
                      f"{chrono_basis}. Later or contemporaneous work may be related and "
                      f"is prior art for nothing here.")
    if relation not in CONCERNING_RELATIONS:
        return refuse("relation_not_concerning",
                      f"both works are real and the earlier one is earlier; the reviewer's "
                      f"own answer is {relation}, which raises no prior-art question.")

    basis = (binding_basis or "").strip()
    endpoints = (
        f"the paper states {claim.quote[:120]!r} at {claim.ref}; {work.title!r} "
        f"({work.canonical_id or canonical_id(work)}) was public at {work.date or work.year} "
        f"and contains the quoted passage; {chrono_basis}. {cite_why}. ")
    if basis in PRIOR_ART_BINDING_BASES and basis != "reviewer_reading" \
            and claims_priority(claim.quote):
        return PriorArtFact(
            fact_id=fid, claim_id=claim.claim_id, target_ref=claim.ref,
            target_quote=claim.quote, candidate=work, candidate_quote=candidate_quote,
            candidate_quote_located=True, relation=relation,
            statement=endpoints + (
                f"The relationship itself is bound by {basis}: the correspondence is "
                f"established from the two documents rather than proposed. This is a "
                f"prior-art relation a referee must adjudicate; it is not a finding that "
                f"the paper is not novel, which is not a conclusion this system reaches."),
            authority="STRUCTURALLY_BOUND_PRIOR_ART", chronology=chrono,
            chronology_basis=chrono_basis, citation_state=cite_state,
            binding_basis=basis, reviewer_note=reviewer_note)

    return PriorArtFact(
        fact_id=fid, claim_id=claim.claim_id, target_ref=claim.ref,
        target_quote=claim.quote, candidate=work, candidate_quote=candidate_quote,
        candidate_quote_located=True, relation=relation,
        statement=endpoints + (
            "Both ends are verified and the OVERLAP between them is the literature "
            "reviewer's reading, not something this harness checked. It is a question for "
            "a referee: whether these two contributions are the same contribution is a "
            "semantic judgement in the authors' field."),
        authority="ENDPOINTS_VERIFIED_LITERATURE_CONCERN", chronology=chrono,
        chronology_basis=chrono_basis, citation_state=cite_state,
        binding_basis=(basis or "reviewer_reading"), reviewer_note=reviewer_note)


def _passage_present(work: WorkIdentity | None, quote: str) -> bool:
    """Is the quoted passage in the text THIS HARNESS retrieved for this work?

    `claims.mint` for a candidate work, and the same discipline: the reviewer supplies the
    passage and the harness finds it. Whitespace is collapsed on both sides because
    abstracts arrive from indexes with line breaks the reviewer will not have reproduced;
    nothing else is normalised, and a passage that is not there is not softened.
    """
    if work is None:
        return False
    hay = _WS.sub(" ", (work.abstract or "")).strip().lower()
    needle = _WS.sub(" ", (quote or "")).strip().lower()
    return bool(needle) and len(needle) >= 20 and needle in hay


# --------------------------------------------------------------------------------------
# WHAT THE ROUTE ENDED AS
# --------------------------------------------------------------------------------------
def outcome_disposition(search: LiteratureSearch) -> tuple[str, str]:
    """(disposition, reason) — one of the five terminal states, and why.

    THE ORDER ENCODES THE ASYMMETRY. A bound relation and a verified concern are checked
    first because they are the only things that settle anything; everything below them is
    a statement about the SEARCH, and the last two are statements about this host. There
    is no branch that returns "novel", and the branch a clean search takes says what it
    actually did: the declared protocol completed and nothing qualified.
    """
    bound = search.bound_relations()
    if bound:
        return ("PRIOR_ART_RELATION_STRUCTURALLY_BOUND",
                f"{len(bound)} prior-art relation(s) bound from the two documents "
                f"themselves. A referee must adjudicate them; nothing here concludes the "
                f"paper is not novel.")
    concerns = search.concerns()
    if concerns:
        return ("LITERATURE_MATCH_VERIFIED_ENDPOINTS",
                f"{len(concerns)} earlier work(s) verified as real, earlier and quoted "
                f"against an addressed novelty claim. The OVERLAP is the reviewer's "
                f"reading and is a question for a referee.")
    if not search.provider_calls:
        return ("LITERATURE_BLOCKED",
                "no index was queried, so nothing was searched. This is a fact about this "
                "host's configuration and not about the paper.")
    if not search.protocol_completed:
        bad = [c for c in search.provider_calls if not c.completed]
        why: dict[str, int] = {}
        for call in bad:
            why[call.outcome] = why.get(call.outcome, 0) + 1
        return ("LITERATURE_BLOCKED",
                f"the declared protocol did not complete: {len(bad)} of "
                f"{len(search.provider_calls)} queries did not reach an index that "
                f"answered ("
                + ", ".join(f"{k} x{v}" for k, v in sorted(why.items()))
                + f"), across {', '.join(search.providers_failed) or 'one or more '
                                                                    'providers'}. "
                f"An unconfigured or unreachable index is not a completed query. The "
                f"{len(search.provider_calls) - len(bad)} queries that DID complete are in "
                f"the record and established nothing further; what this state says is that "
                f"the bounds this route published were not carried out, which is a fact "
                f"about this host.")
    if search.works and search.facts:
        return ("SEARCH_INCONCLUSIVE",
                f"{len(search.works)} candidate work(s) were retrieved and none could be "
                f"adjudicated: the evidence did not reach them. Refusals: "
                + _refusal_summary(search))
    return ("SEARCH_COMPLETED_NO_MATCH_FOUND",
            f"the declared bounded protocol completed — {len(search.provider_calls)} "
            f"queries across {len(search.providers_completed)} index(es), "
            f"{search.raw_results} raw results, {len(search.works)} distinct works — and "
            f"no candidate qualified as prior art for any searched claim. THIS IS NOT "
            f"EVIDENCE OF NOVELTY: it is a statement about a bounded search, and the "
            f"literature it did not reach is not enumerable.")


def _refusal_summary(search: LiteratureSearch) -> str:
    counts: dict[str, int] = {}
    for f in search.facts:
        if f.refusal:
            counts[f.refusal] = counts.get(f.refusal, 0) + 1
    return ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())) or "none recorded"


def discharge(search: LiteratureSearch) -> tuple[bool, str]:
    """Did this route answer the question it was given? Only two outcomes can say yes.

    A COMPLETED SEARCH THAT MATCHED NOTHING DOES NOT DISCHARGE, and that is the single
    most important line in this module. The route looked, the route finished, and the
    question — "is there earlier work making this contribution?" — is still open, because
    a bounded protocol cannot close it. Saying otherwise would let a search budget resolve
    a scientific question by exhausting itself.
    """
    disposition, reason = outcome_disposition(search)
    return disposition in ("PRIOR_ART_RELATION_STRUCTURALLY_BOUND",
                           "LITERATURE_MATCH_VERIFIED_ENDPOINTS"), reason


# WHAT A VERIFIED LITERATURE OBSERVATION BUYS A LATER ROUTE. Keyed on the RELATION, which
# is what was looked at, and recorded as sentences into `LiteratureSearch.escalations`,
# which no planner, gate or disposition reads — the same shape `artifact_evidence`'s
# escalations have, and for the same reason: a reviewer's prose may not decide what runs.
_ESCALATION_FOR_RELATION = {
    "POSSIBLE_OMITTED_BASELINE":
        "names a comparison arm a focused-validation experiment could build, and a "
        "baseline the paper does not report against",
    "LIKELY_DIRECT_PREDECESSOR":
        "names an earlier method a focused-validation experiment could measure against",
    "POSSIBLE_PRIORITY_CONFLICT":
        "identifies a priority question a human referee must adjudicate; no measurement "
        "settles it",
}


def escalations_from(facts: list[PriorArtFact]) -> list[str]:
    """What these concerns make newly possible for a LATER route. Recorded, never acted on."""
    out: list[str] = []
    for fact in facts:
        if not fact.supports_concern:
            continue
        line = _ESCALATION_FOR_RELATION.get(fact.relation, "")
        if not line:
            continue
        who = (fact.candidate.title if fact.candidate else "") or "the candidate work"
        out.append(f"{who!r} {line}")
    return sorted(set(out))


def summarise(search: LiteratureSearch) -> dict:
    """The counts §15 asks to be published, each read off a different part of the record.

    Search breadth belongs in coverage: every one of these is a bound the reader needs in
    order to know what "no match" is a statement about.
    """
    return {
        "claims_searched": len(search.claims_searched),
        "queries_planned": sum(len(c.queries) for c in search.claims_searched),
        "queries_executed": len(search.provider_calls),
        "providers_completed": search.providers_completed,
        "providers_failed": search.providers_failed,
        "raw_results": search.raw_results,
        "deduplicated_works": len(search.works),
        "pre_cutoff_candidates": sum(
            1 for w in search.works if chronology(search.cutoff, w)[0] == "PREDATES_CUTOFF"),
        "candidates_shown_bound": (search.protocol.max_candidates_reviewed
                                   if search.protocol else 0),
        "candidates_semantically_reviewed": search.candidates_reviewed,
        "proposals_received": search.proposed,
        "endpoint_verified_concerns": len(search.concerns()),
        "structurally_bound": len(search.bound_relations()),
        "cited_by_target": search.cited_by_target,
        "apparently_uncited": search.apparently_uncited,
        "protocol_completed": search.protocol_completed,
    }


def _self_check() -> None:
    from .artifacts import Section

    # THE ASYMMETRY, asserted on the vocabulary rather than on a docstring.
    assert NOVELTY_ESTABLISHING_AUTHORITIES == ()
    assert set(PRIOR_ART_CAPABLE_AUTHORITIES) < set(LITERATURE_AUTHORITY)
    assert not any("NOVEL" in a for a in LITERATURE_AUTHORITY)

    # EVERY VALUE THIS MODULE CAN EMIT IS IN THE DECLARED VOCABULARY. Asserted here rather
    # than trusted, because a string this module invents would be a state nothing
    # downstream has a rule for — and an unrecognised state fails OPEN in a renderer.
    assert set(CONCERNING_RELATIONS) < set(LITERATURE_RELATIONS)
    assert set(_ESCALATION_FOR_RELATION) <= set(LITERATURE_RELATIONS)
    assert set(PRIOR_ART_ELIGIBLE_CHRONOLOGY) < set(CHRONOLOGY_STATES)
    assert set(_CUTOFF_PRECEDENCE) < set(CUTOFF_SOURCES)

    doc = PaperDoc(paper_id="p", title="FlowNet: Learning Optical Flow", body_end_section_idx=2,
                   sections=[
                       Section(section_idx=0, title="Abstract", text=(
                           "We introduce FlowNet, a convolutional network for optical flow. "
                           "This is the first method to learn optical flow end to end. "
                           "It reaches 2.7 average endpoint error on Sintel.")),
                       Section(section_idx=1, title="1 Introduction",
                               text="Optical flow is old."),
                       Section(section_idx=2, title="References", text=(
                           "A. Author. Learning to see. arXiv:2001.00001. 2020.")),
                   ])
    found = novelty_claims(doc)
    assert found and all(c.ref for c in found), found
    assert any(c.claim_kind == "NOVELTY_MARKER" for c in found)
    assert "FlowNet" in method_names(doc), method_names(doc)

    # A YEAR-ONLY CUTOFF CANNOT ORDER A SAME-YEAR CANDIDATE.
    year_only = derive_cutoff({"proceedings_year": "2024"})
    assert year_only.state == "YEAR_ONLY"
    same = WorkIdentity(doi="10.1/x", title="t", date="2024-01-02", year=2024)
    assert chronology(year_only, same)[0] == "CONTEMPORANEOUS_UNRESOLVED"
    exact = derive_cutoff({"arxiv_submission_v1": "2024-06-01",
                           "proceedings_year": "2024"})
    assert exact.state == "ESTABLISHED" and exact.source == "arxiv_submission_v1"
    assert exact.candidates["proceedings_year"] == "2024"      # the loser is recorded
    assert chronology(exact, same)[0] == "PREDATES_CUTOFF"
    later = WorkIdentity(doi="10.1/y", title="t", date="2025-01-01", year=2025)
    assert chronology(exact, later)[0] == "POSTDATES_CUTOFF"
    assert chronology(exact, WorkIdentity(doi="10.1/z", title="t"))[0] == "CANDIDATE_DATE_UNKNOWN"

    # ONE PAPER IS NOT FOUR PAPERS, AND TWO PAPERS ARE NOT ONE.
    folded = merge_works([
        WorkIdentity(doi="10.1/A", title="Deep Nets", date="2021-05-01", providers=["crossref"]),
        WorkIdentity(doi="10.1/a", arxiv_id="arXiv:2101.00001v2", title="Deep Nets",
                     date="2021-01-04", providers=["openalex"]),
        WorkIdentity(arxiv_id="2101.00001", title="Deep Nets", providers=["arxiv"]),
        WorkIdentity(doi="10.1/B", title="Deep Nets", authors=["Other"], year=2021),
    ])
    assert len(folded) == 3, [w.canonical_id for w in folded]
    assert folded[0].date == "2021-01-04"                      # the EARLIEST public date
    assert "arxiv:2101.00001" in folded[0].aliases

    # THE LADDER.
    claim = found[0]
    priority = next(c for c in found if c.claim_kind == "NOVELTY_MARKER")
    work = WorkIdentity(doi="10.9/earlier", title="End-to-end optical flow", date="2013-03-01",
                        date_source="arxiv_submission_v1",
                        abstract="We learn optical flow end to end with a network.")
    work.canonical_id = canonical_id(work)
    cutoff = derive_cutoff({"arxiv_submission_v1": "2015-04-26"})
    ok = bind_prior_art(doc, priority, work, relation="LIKELY_DIRECT_PREDECESSOR",
                        candidate_quote="We learn optical flow end to end with a network.",
                        cutoff=cutoff)
    assert ok.authority == "ENDPOINTS_VERIFIED_LITERATURE_CONCERN", ok.refusal
    assert ok.supports_concern and not ok.establishes_novelty
    assert ok.citation_state == "APPARENTLY_UNCITED"
    assert ok.citation_state in CANDIDATE_CITATION_STATES
    assert ok.chronology in CHRONOLOGY_STATES and not ok.refusal
    assert all(f.refusal in ("",) + LITERATURE_REFUSALS for f in [ok])
    assert year_only.state in CUTOFF_STATES and exact.state in CUTOFF_STATES

    # A LATER WORK IS NOT PRIOR ART, WHATEVER THE READING SAYS.
    work_late = work.model_copy(deep=True)
    work_late.date, work_late.doi = "2020-01-01", "10.9/later"
    work_late.canonical_id = canonical_id(work_late)
    assert bind_prior_art(doc, priority, work_late, relation="LIKELY_DIRECT_PREDECESSOR",
                          candidate_quote=work.abstract,
                          cutoff=cutoff).refusal == "candidate_not_earlier"

    # A QUOTE THAT IS NOT IN THE RETRIEVED TEXT IS NOT EVIDENCE.
    assert bind_prior_art(doc, priority, work, relation="LIKELY_DIRECT_PREDECESSOR",
                          candidate_quote="We solve a completely different problem here.",
                          cutoff=cutoff).refusal == "candidate_quote_unresolved"

    # LEVEL 3 NEEDS A DETERMINISTIC BASIS AND A PRIORITY CLAIM.
    bound = bind_prior_art(doc, priority, work, relation="POSSIBLE_PRIORITY_CONFLICT",
                           candidate_quote=work.abstract, cutoff=cutoff,
                           binding_basis="explicit_first_claim_met")
    assert bound.authority == "STRUCTURALLY_BOUND_PRIOR_ART"
    assert bind_prior_art(doc, priority, work, relation="POSSIBLE_PRIORITY_CONFLICT",
                          candidate_quote=work.abstract, cutoff=cutoff,
                          binding_basis="reviewer_reading"
                          ).authority == "ENDPOINTS_VERIFIED_LITERATURE_CONCERN"
    contribution = next((c for c in found if c.claim_kind == "CONTRIBUTION"), None)
    if contribution is not None:
        assert bind_prior_art(doc, contribution, work, relation="POSSIBLE_PRIORITY_CONFLICT",
                              candidate_quote=work.abstract, cutoff=cutoff,
                              binding_basis="explicit_first_claim_met"
                              ).authority == "ENDPOINTS_VERIFIED_LITERATURE_CONCERN"

    # AND THE ONE THAT MATTERS: A COMPLETED EMPTY SEARCH RESOLVES NOTHING.
    from .artifacts import ProviderCall
    empty = LiteratureSearch(
        paper_id="p", cutoff=cutoff, claims_searched=found,
        provider_calls=[ProviderCall(provider="openalex", query="q", outcome="NO_RESULTS")])
    disposition, why = outcome_disposition(empty)
    assert disposition == "SEARCH_COMPLETED_NO_MATCH_FOUND", disposition
    assert "NOT EVIDENCE OF NOVELTY" in why
    assert discharge(empty)[0] is False
    from . import taxonomy
    assert taxonomy.evidence_state(disposition) == "BOUNDED_SEARCH_NO_MATCH"
    assert taxonomy.resolution_state("BOUNDED_SEARCH_NO_MATCH") == "UNRESOLVED"
    assert not taxonomy.concerns_the_paper("BOUNDED_SEARCH_NO_MATCH")

    # AN UNCONFIGURED INDEX IS NOT A COMPLETED QUERY.
    blocked = empty.model_copy(deep=True)
    blocked.provider_calls.append(
        ProviderCall(provider="s2", query="q", outcome="UNAUTHENTICATED"))
    assert outcome_disposition(blocked)[0] == "LITERATURE_BLOCKED"
    assert blocked.providers_failed == ["s2"]

    print("harness.literature self-check ok")


if __name__ == "__main__":       # pragma: no cover
    _self_check()
