"""What the paper's argument DEPENDS ON, as a graph over claims rather than objects.

`python -m harness.claimgraph` runs the self-check;
`python -m harness.claimgraph <paper_id>` builds the graph for a project on disk.

**The measurement that made this necessary.** `discovery._centrality` decides how much a
paper's conclusion rests on an object from four booleans. One of them, `in_abstract`, read
the wrong section for a long time; correcting it was expected to matter and was therefore
measured before the locators were unified:

    legacy abstract locator:      0 / 706 discovered objects
    corrected abstract locator:   0 / 706 discovered objects

Nothing moved. The reason is structural rather than incidental — discovered objects come
from table cells and parsed quantities, and an abstract carries prose — so `in_abstract`
contributes nothing to centrality on real papers under EITHER spelling. Of the four
remaining signals one is a sentence-shape test, one is extractor bookkeeping, and the
dominant one is whether a lens happened to attack the address: 105 of 112 CENTRAL objects
in the evaluated corpus are CENTRAL for that reason alone. So "central" has largely meant
"the panel wrote about it", which is a fact about the review and not about the paper.

Adding a fifth boolean would move the number without making it mean anything. Importance
is not a property an object has on its own; it is a RELATION — which of the paper's claims
would stop holding if this number were wrong. That relation needs a graph, and this module
builds one.

**Everything here is derived from `PaperDoc` and nothing else.** No lens output, no
finding, no target, no grade, no outcome reaches any function in this module, and the
signatures are what say so — the same discipline `coverage.surface` has, for the same
reason: a dependency graph that could see which addresses the panel attacked would
rediscover the defect it was written to replace.

**The shape.** Two node kinds and four edge kinds, deliberately few.

    CLAIM      a sentence the paper prints that states a number or cites an object.
               HEADLINE when it sits in the Abstract or the Conclusion — the paper's own
               summary of itself — and SUPPORTING anywhere else. That is a position in the
               document, not a judgement about worth.
    RESULT     a table cell, a reported quantity, a table, a figure or an equation.

    STATES     CLAIM  -> RESULT   this sentence printed this number
    CITES      CLAIM  -> RESULT   this sentence cites Table 3 / Figure 2 / Equation 4
    IN_TABLE   RESULT -> RESULT   this cell belongs to that table
    COMPARES   RESULT -> RESULT   two numbers of the same metric on the same benchmark,
                                  reported for different methods: an experimental
                                  comparison the paper itself set up

**What the graph answers, and how it answers it.** `dependency(address)` returns the
shortest path from a HEADLINE claim to that address, or None. A path is an EXPLANATION —
"the abstract cites Table 3, and this is a cell of Table 3" — which is the thing a
boolean could never give a referee, and the thing that makes the answer arguable rather
than merely asserted.

**The one edge the paper does not print.** Measured over the eight-paper corpus, the
deterministic edges above establish a headline dependency for 0 of 1,729 addresses, and
that is not a defect of this module: there are zero cross-references in any Abstract and
one in any Conclusion, and none of the twenty-one numbers headline sentences print occurs
in exactly one recovered cell. An abstract says "reduces memory by 40%", Table 3 says
`40.2`, and nothing in the document says those are the same claim.

So `SUPPORTED_BY` comes from `harness/claimlink.py`: a reader PROPOSES the pairing and the
harness verifies both halves — the claim quotation must mint to an address inside the
Abstract or Conclusion, the evidence address must resolve against the quotation given for
it, and where both carry a number the arithmetic is re-derived. `build(doc)` with no links
produces exactly the graph it produced before that channel existed, which is what makes
the channel safe to have: it can only ADD a dependency, never remove or alter one.

**This module changes no decision.** `materiality.basis_for_ref` and
`discovery._centrality` are untouched, and `compare_with_existing` exists so the delta can
be measured on every paper before anything is rewired. A decision rule replaced without
that measurement would be a different reviewer wearing the same name.
"""
from __future__ import annotations

import bisect
import re
from collections import deque
from typing import NamedTuple

from . import claims, materiality
from .artifacts import PaperDoc

CLAIM_KINDS = ("HEADLINE", "SUPPORTING")
RESULT_KINDS = ("TABLE_CELL", "REPORTED_QUANTITY", "TABLE", "FIGURE", "EQUATION",
                "COMPARISON")
EDGE_KINDS = ("STATES", "CITES", "IN_TABLE", "MEASURED_BY", "MEASURES", "SUPPORTED_BY")

# How far a HEADLINE claim's dependency may reach. Four is what the longest real chain
# needs: a headline cites Table 3 (1), the cell is in Table 3 (2), that cell belongs to a
# comparison (3), and the baseline it is compared against is the other member (4). It is a
# bound rather than a tuning knob — raise it and a chain of shared comparisons reaches
# most of a large paper, at which point "depends on" stops distinguishing anything.
MAX_DEPTH = 4

# A sentence ends at one of these followed by whitespace. Deliberately crude: the job is
# to bound a span, not to parse English, and a span that swallows two sentences addresses
# a superset of the right text rather than the wrong text.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A span shorter than this addresses nothing in particular — the floor `claims.mint`
# applies to a lens's quotation, applied here for the same reason.
MIN_CLAIM_CHARS = 24
# A sentence with no digit is still a claim when it makes a COMPARATIVE assertion — "our
# method outperforms the baseline" is the paper staking itself on something, and an
# abstract that states its result in words rather than numbers is common.
_COMPARATIVE = re.compile(
    r"\b(outperform\w*|improv\w+|reduc\w+|increas\w+|decreas\w+|better|worse|"
    r"higher|lower|faster|slower|exceed\w*|surpass\w*|state[- ]of[- ]the[- ]art|"
    r"compared (?:to|with)|relative to|over the baseline)\b", re.I)


class ClaimNode(NamedTuple):
    """One sentence the paper prints that makes a checkable assertion.

    `address` is minted by `claims.mint`, so every claim node resolves back into the
    document and a reader can open it. A sentence whose quote is ambiguous — it occurs
    twice — mints nothing and is not a node: an address nobody can name is not a claim
    anyone can check, which is the same rule `claims.mint` applies to a lens.
    """

    claim_id: str
    kind: str                  # HEADLINE | SUPPORTING
    address: str               # P<i>:<a>-<b>
    section_idx: int
    page: int
    text: str                  # the sentence, verbatim


class ResultNode(NamedTuple):
    result_id: str
    kind: str                  # one of RESULT_KINDS
    address: str               # T<t>:r<r>:c<c> | P<i>:<a>-<b> | F<n> | E<n> | table:<t>
    label: str                 # the paper's own printed label, where it has one
    value: float | None
    metric: str
    benchmark: str
    method: str


class Edge(NamedTuple):
    kind: str
    src: str
    dst: str
    why: str                   # one line a reader can check against the paper


class DependencyPath(NamedTuple):
    """How a headline claim reaches a result, stated so a referee can disagree with it."""

    claim_id: str
    claim_kind: str
    edges: list                # Edge, in order
    depth: int

    def explain(self, graph: ClaimGraph) -> str:
        claim = graph.claims[self.claim_id]
        where = "the abstract or conclusion" if claim.kind == "HEADLINE" else "the body"
        out = [f"a {claim.kind.lower()} claim in {where} ({claim.address})"]
        out += [e.why for e in self.edges]
        return "; ".join(out)


class ClaimGraph(NamedTuple):
    paper_id: str
    claims: dict               # claim_id -> ClaimNode
    results: dict              # result_id -> ResultNode
    edges: list                # Edge
    by_address: dict           # address -> result_id
    abstract_idx: int
    conclusion_idx: int

    @property
    def headline_claims(self) -> list:
        return [c for c in self.claims.values() if c.kind == "HEADLINE"]

    def counts(self) -> dict:
        per_edge = {k: 0 for k in EDGE_KINDS}
        for e in self.edges:
            per_edge[e.kind] = per_edge.get(e.kind, 0) + 1
        return {"claims": len(self.claims),
                "headline_claims": len(self.headline_claims),
                "results": len(self.results),
                "edges": len(self.edges), **per_edge}


# --------------------------------------------------------------------------- #
# Building it
# --------------------------------------------------------------------------- #
def _number(text: str) -> float | None:
    q = claims.parse_quantity(text or "")
    return q.value if q is not None else None


class _Span(NamedTuple):
    section_idx: int
    start: int
    end: int
    text: str                  # verbatim, off the document

    @property
    def address(self) -> str:
        return f"P{self.section_idx}:{self.start}-{self.end}"


def sentence_spans(doc: PaperDoc, section_idx: int) -> list:
    """Every sentence of one section, as a span whose address resolves.

    ADDRESSED BY OFFSET, never by minting from the text. `claims.mint` searches the whole
    document for a quotation and refuses anything occurring twice, which is exactly right
    when a LENS supplies the quote and the harness must locate it — and exactly wrong here,
    where the harness already knows which characters it is looking at. A paper whose
    abstract sentence reappears almost verbatim in its introduction is completely ordinary,
    and minting refuses it: the first version of this module built claim nodes that way and
    produced ZERO headline claims on seven of the eight corpus papers, which is the same
    null result `in_abstract` produced and for a related reason.

    THE SPLIT RUNS ON THE ORIGINAL TEXT AND THE ADDRESS IS IN FLATTENED COORDINATES, and
    the two are not interchangeable. `claims.flatten` removes every space, so a sentence
    boundary — a full stop FOLLOWED BY WHITESPACE — does not exist in flattened text at
    all; splitting there silently produced one span per section, which is why the second
    version of this module reported one or two headline claims per paper. So the boundary
    is found where whitespace still exists and the offsets are mapped across.

    Crude on purpose. "et al." and "Fig. 2" split a sentence in two, and a fragment still
    addresses real text at a real address — it is a smaller claim, not a wrong one. What
    would be wrong is a span covering text the paper does not have, and that is impossible
    here because both endpoints come from the document.
    """
    unit = next((u for u in claims.section_units(doc) if u[0] == section_idx), None)
    if unit is None:
        return []
    _idx, flat, offsets, original, _page = unit
    out: list[_Span] = []
    pending = 0
    for piece in _SENTENCE_END.finditer(original):
        cut = piece.end()
        span = _span_between(original, offsets, flat, section_idx, pending, cut)
        if span is not None:
            out.append(span)
            pending = cut
    tail = _span_between(original, offsets, flat, section_idx, pending, len(original))
    if tail is not None:
        out.append(tail)
    return out


def _span_between(original: str, offsets: list, flat: str, section_idx: int,
                  o_start: int, o_end: int):
    """One sentence as a flattened-coordinate span, or None when it is too short to be one.

    `MIN_CLAIM_CHARS` is the same floor `claims.mint` applies for the same reason: a span
    of a few characters addresses nothing in particular, and an abbreviation that split a
    sentence should be absorbed into the next piece rather than becoming a claim node
    asserting "et al.".
    """
    f_start = bisect.bisect_left(offsets, o_start)
    f_end = bisect.bisect_left(offsets, o_end)
    if f_end - f_start < MIN_CLAIM_CHARS:
        return None
    return _Span(section_idx, f_start, f_end,
                 claims._verbatim(original, offsets, f_start, f_end))


def _is_claim_sentence(span: _Span, cited: set) -> bool:
    """Does this sentence assert something checkable?

    Three ways, and a sentence needs one: it prints a number, it makes a comparative
    assertion, or it cites a numbered object. A sentence that does none of those is
    scaffolding — "we describe our method below" — and a graph whose nodes are mostly
    scaffolding cannot support an argument about what depends on what.
    """
    if any(span.start <= at < span.end for at in cited):
        return True
    return bool(re.search(r"\d", span.text)) or bool(_COMPARATIVE.search(span.text))


def _result_nodes(doc: PaperDoc) -> tuple[dict, dict]:
    """(results by id, address -> id). Every unit of the paper a claim could rest on.

    A reported quantity whose `table_ref` resolves IS that cell and does not become a
    second node — the same rule `coverage._prose_quantity_addresses` applies to the
    coverage denominator, and for the same reason: counting one unit twice would let a
    dependency reach the same number by two routes and look better supported than it is.
    """
    results: dict[str, ResultNode] = {}
    by_address: dict[str, str] = {}

    def add(node: ResultNode) -> None:
        results.setdefault(node.result_id, node)
        by_address.setdefault(node.address, node.result_id)

    for table in doc.tables:
        add(ResultNode(result_id=f"table:{table.table_idx}", kind="TABLE",
                       address=f"table:{table.table_idx}", label=table.label or "",
                       value=None, metric="", benchmark="", method=""))
        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row):
                if not (cell or "").strip():
                    continue
                ref = table.ref(r, c)
                add(ResultNode(result_id=ref, kind="TABLE_CELL", address=ref, label="",
                               value=_number(cell), metric="", benchmark="", method=""))

    for fig in doc.figures:
        if (fig.caption or "").strip():
            add(ResultNode(result_id=fig.ref(), kind="FIGURE", address=fig.ref(),
                           label=fig.label or "", value=None,
                           metric="", benchmark="", method=""))
    for eq in doc.equations:
        if (eq.text or "").strip():
            add(ResultNode(result_id=eq.ref(), kind="EQUATION", address=eq.ref(),
                           label=str(eq.number or ""), value=None,
                           metric="", benchmark="", method=""))

    for num in doc.reported_numbers:
        ref = (num.table_ref or "").strip()
        if ref and claims.resolve(doc, ref).resolved:
            # The quantity IS that cell. Enrich the cell node with what the extractor
            # recovered about it rather than adding a rival node at the same address.
            existing = results.get(ref)
            if existing is not None and not existing.metric:
                results[ref] = existing._replace(metric=num.metric or "",
                                                 benchmark=num.benchmark or "",
                                                 method=num.method or "",
                                                 value=existing.value
                                                 if existing.value is not None
                                                 else _number(num.value))
            continue
        minted = claims.mint(doc, num.source_quote or "")
        if not minted.resolved:
            continue
        add(ResultNode(result_id=minted.ref, kind="REPORTED_QUANTITY", address=minted.ref,
                       label="", value=_number(num.value), metric=num.metric or "",
                       benchmark=num.benchmark or "", method=num.method or ""))
    return results, by_address


def _claim_kind(section_idx: int, abstract_idx: int, conclusion_idx: int) -> str:
    return "HEADLINE" if section_idx in (abstract_idx, conclusion_idx) else "SUPPORTING"


def _object_for_citation(doc: PaperDoc, kind: str, number: str) -> str:
    """Which recovered object a `CrossRef` names, or '' when the label is not unique.

    Strict on purpose, and the strictness is `materiality._verified_citation`'s rather
    than a new rule: an object with no printed label, or a label two objects share, is not
    identified by "Table 3" and a dependency built on the guess would be a dependency on
    whichever object the extractor happened to number first.
    """
    want = (number or "").strip()
    if not want:
        return ""
    if kind == "table":
        hits = [t for t in doc.tables if (t.label or "").strip() == want]
        return f"table:{hits[0].table_idx}" if len(hits) == 1 else ""
    if kind == "figure":
        hits = [f for f in doc.figures if (f.label or "").strip() == want]
        return hits[0].ref() if len(hits) == 1 else ""
    if kind == "equation":
        hits = [e for e in doc.equations if str(e.number or "").strip() == want]
        return hits[0].ref() if len(hits) == 1 else ""
    return ""


def build(doc: PaperDoc, links=None) -> ClaimGraph:
    """The graph. A `PaperDoc`, and optionally the VERIFIED claim links for it.

    `links` is a `ClaimLinkSet` or a list of `ClaimLink`, and only its ACCEPTED members
    are read — a refused pairing is a record of what a reader proposed, not a dependency.
    Passing none produces exactly the graph this function produced before the claim-link
    channel existed, which is the property that makes an optional model channel safe: the
    deterministic graph is the floor and links can only add to it.

    Still a `PaperDoc` and nothing about the review. A `ClaimLinkSet` carries no finding,
    no severity and no outcome — it is a set of address pairs the harness has already
    checked against this same document — so the guarantee in the module docstring is
    unchanged, and the self-check asserts it over the source.

    Claim nodes come from two deterministic sources and no others: the verbatim span each
    reported number was printed in, and the citing sentence each cross-reference was found
    in. That is a bound as much as a definition — splitting every section into sentences
    would make a 100k-character paper about a thousand claim nodes, most of which assert
    nothing checkable, and a graph whose nodes are mostly noise cannot support an argument
    about what depends on what.
    """
    abstract_idx = materiality.abstract_section_idx(doc)
    conclusion_idx = materiality.conclusion_section_idx(doc)
    results, by_address = _result_nodes(doc)
    edges: list[Edge] = []
    page_of = {s.section_idx: s.page_start for s in doc.sections}

    # WHERE each cross-reference and each reported number sits, by section and offset, so
    # a sentence can be matched to them by containment rather than by searching for text.
    xrefs_at: dict[int, list] = {}
    for xref in doc.crossrefs:
        parsed = claims.resolve(doc, xref.span or "")
        if not parsed.resolved or parsed.kind != "prose_claim":
            continue
        target = _object_for_citation(doc, xref.kind, xref.number)
        if not target or target not in results:
            continue
        xrefs_at.setdefault(parsed.section_idx, []).append(
            (parsed.span[0], target, xref.kind, xref.number))

    numbers_at: dict[int, list] = {}
    for num in doc.reported_numbers:
        minted = claims.mint(doc, num.source_quote or "")
        if not minted.resolved:
            continue
        ref = (num.table_ref or "").strip()
        target = ref if ref in results else by_address.get(minted.ref)
        if target is None or target not in results:
            continue
        if minted.kind == "prose_claim":
            numbers_at.setdefault(minted.section_idx, []).append(
                (minted.span[0], target))

    # CLAIM NODES, from sentence spans. Every sentence of the Abstract and the Conclusion
    # that asserts something checkable, plus every body sentence that prints a number or
    # cites a numbered object. Bounded by construction: the two summary sections are
    # small, and a body sentence has to carry one of the two things this graph can follow.
    claim_nodes: dict[str, ClaimNode] = {}
    central = [i for i in (abstract_idx, conclusion_idx) if i >= 0]
    sections = sorted({*central, *xrefs_at, *numbers_at})
    for section_idx in sections:
        anchors = {at for at, *_ in xrefs_at.get(section_idx, ())}
        anchors |= {at for at, _ in numbers_at.get(section_idx, ())}
        for span in sentence_spans(doc, section_idx):
            headline = section_idx in central
            if not (headline and _is_claim_sentence(span, anchors)) and not any(
                    span.start <= at < span.end for at in anchors):
                continue
            node = ClaimNode(
                claim_id=span.address,
                kind=_claim_kind(section_idx, abstract_idx, conclusion_idx),
                address=span.address, section_idx=section_idx,
                page=page_of.get(section_idx, 0), text=span.text)
            claim_nodes[node.claim_id] = node
            # STATES — this sentence printed that number.
            for at, target in numbers_at.get(section_idx, ()):
                if span.start <= at < span.end:
                    edges.append(Edge("STATES", node.claim_id, target,
                                      f"that sentence prints the quantity at {target}"))
            # CITES — this sentence cites that numbered object.
            for at, target, kind, number in xrefs_at.get(section_idx, ()):
                if span.start <= at < span.end:
                    edges.append(Edge("CITES", node.claim_id, target,
                                      f"that sentence cites {kind} {number} ({target})"))

    # SUPPORTED_BY — the edge the document does not print, proposed and verified.
    #
    # The claim node is minted from the LINK's own address rather than matched against a
    # sentence span, because the two need not coincide: a reader may quote a clause, and
    # the splitter may have cut at an abbreviation. Both are addresses into the same
    # section and both resolve; insisting they be the same span would discard a verified
    # dependency over a disagreement about where a sentence ends.
    for link in _accepted_links(links):
        target = link.evidence_ref if link.evidence_ref in results else \
            by_address.get(link.evidence_ref)
        if target is None:
            # A verified evidence address the deterministic pass did not enumerate — a
            # prose quantity, most often. It is a real unit of the paper and the harness
            # has already resolved it, so it joins the graph rather than being dropped.
            target = link.evidence_ref
            results[target] = ResultNode(
                result_id=target, kind="REPORTED_QUANTITY", address=target, label="",
                value=link.evidence_value, metric="", benchmark="", method="")
            by_address.setdefault(target, target)
        claim_nodes.setdefault(link.claim_ref, ClaimNode(
            claim_id=link.claim_ref,
            kind=_claim_kind(link.claim_section_idx, abstract_idx, conclusion_idx),
            address=link.claim_ref, section_idx=link.claim_section_idx,
            page=page_of.get(link.claim_section_idx, 0), text=link.claim_quote))
        edges.append(Edge("SUPPORTED_BY", link.claim_ref, target,
                          f"a reader paired that claim with {target}, and the harness "
                          f"verified both halves ({link.numeric_relation})"))

    # IN_TABLE — a cell belongs to its table, so citing the table reaches the cell.
    for result in list(results.values()):
        if result.kind != "TABLE_CELL":
            continue
        table_id = f"table:{result.address.split(':', 1)[0][1:]}"
        if table_id in results:
            edges.append(Edge("IN_TABLE", table_id, result.result_id,
                              f"{result.address} is a cell of {table_id}"))

    # COMPARISON — an experimental comparison the PAPER set up, as a NODE.
    #
    # A node and not a clique of edges. The first version joined every pair of results
    # sharing a metric and a benchmark, which on `sanchez24a-icml` produced 4,458 edges
    # over 2,229 pairs — quadratic in the size of one results table, and a structure in
    # which every number is one hop from every other. "Depends on" that reaches everything
    # distinguishes nothing. One node per comparison is linear in the members, says what
    # the comparison IS rather than only that two numbers are related, and keeps any two
    # members exactly two hops apart.
    groups: dict[tuple, list[ResultNode]] = {}
    for result in results.values():
        # BOTH keys, or it is not a comparison. Two accuracies on different datasets are
        # two measurements, and grouping on the metric alone would claim the paper set up
        # a contrast it never printed.
        if not (result.metric and result.benchmark):
            continue
        groups.setdefault((result.metric.strip().lower(),
                           result.benchmark.strip().lower()), []).append(result)
    for (metric, benchmark), members in sorted(groups.items()):
        methods = sorted({m.method.strip() for m in members if m.method.strip()})
        if len(methods) < 2:
            continue                      # one method is a measurement, not a comparison
        node_id = f"comparison:{metric}|{benchmark}"
        results[node_id] = ResultNode(
            result_id=node_id, kind="COMPARISON", address=node_id,
            label=f"{metric} on {benchmark}", value=None,
            metric=metric, benchmark=benchmark, method=" vs ".join(methods))
        why = (f"the paper reports {metric} on {benchmark} for "
               f"{len(methods)} methods: {', '.join(methods)}")
        for member in members:
            edges.append(Edge("MEASURED_BY", member.result_id, node_id, why))
            edges.append(Edge("MEASURES", node_id, member.result_id, why))

    return ClaimGraph(paper_id=doc.paper_id, claims=claim_nodes, results=results,
                      edges=edges, by_address=by_address,
                      abstract_idx=abstract_idx, conclusion_idx=conclusion_idx)


# --------------------------------------------------------------------------- #
# Asking it questions
# --------------------------------------------------------------------------- #
def _accepted_links(links) -> list:
    """The accepted members of a `ClaimLinkSet`, a plain list, or nothing.

    Accepted only. A refused pairing is a record of what a reader proposed and of why the
    harness would not take it; treating it as a dependency would be treating the proposal
    as the verification, which is the whole distinction this channel rests on.
    """
    if links is None:
        return []
    rows = getattr(links, "links", links)
    return [x for x in (rows or [])
            if getattr(x, "accepted", False) and getattr(x, "claim_ref", "")
            and getattr(x, "evidence_ref", "")]


def _adjacency(graph: ClaimGraph) -> dict:
    out: dict[str, list] = {}
    for edge in graph.edges:
        out.setdefault(edge.src, []).append(edge)
    return out


def dependency(graph: ClaimGraph, address: str, *, kind: str = "HEADLINE",
               max_depth: int = MAX_DEPTH) -> Path | None:
    """The SHORTEST path from a claim of `kind` to `address`, or None.

    Breadth-first from every qualifying claim at once, so the path returned is the
    shortest over all of them — which matters because the length of the chain is the
    argument's strength. "The abstract prints this number" and "the abstract cites a table
    one of whose cells shares a metric with this one" are both dependencies and they are
    not the same dependency, and a reader has to be able to tell them apart.

    None is a real answer and is not "unimportant". It means this document, as extracted,
    gives no chain from the paper's own summary of itself to this address.
    """
    target = graph.by_address.get(address, address)
    if target not in graph.results:
        return None
    adjacency = _adjacency(graph)
    queue = deque()
    seen: set[str] = set()
    for claim in graph.claims.values():
        if claim.kind != kind:
            continue
        queue.append((claim.claim_id, claim.claim_id, []))
        seen.add(claim.claim_id)
    while queue:
        node_id, origin, trail = queue.popleft()
        if node_id == target and trail:
            return DependencyPath(claim_id=origin, claim_kind=kind,
                                  edges=trail, depth=len(trail))
        if len(trail) >= max_depth:
            continue
        for edge in adjacency.get(node_id, ()):
            if edge.dst in seen:
                continue
            seen.add(edge.dst)
            queue.append((edge.dst, origin, [*trail, edge]))
    return None


def graph_centrality(graph: ClaimGraph, address: str) -> str:
    """CENTRAL | SUPPORTING | PERIPHERAL, from dependency rather than from position.

    CENTRAL means the paper's own summary of itself reaches this address. SUPPORTING means
    only a body claim does. PERIPHERAL means no claim in the extracted document reaches it
    at all — which is a statement about this document as extracted, and never a statement
    that the number does not matter.
    """
    if dependency(graph, address, kind="HEADLINE") is not None:
        return "CENTRAL"
    if dependency(graph, address, kind="SUPPORTING") is not None:
        return "SUPPORTING"
    return "PERIPHERAL"


def comparisons(graph: ClaimGraph) -> list[tuple[str, list, str]]:
    """Every experimental comparison the paper set up. (id, members, why).

    One row per comparison rather than one per pair of numbers. `sanchez24a-icml` reports
    the same metric on the same benchmark for many methods; as pairs that is 2,229 rows
    and as comparisons it is a handful, and the handful is what the paper actually set up.
    """
    members: dict[str, list] = {}
    why: dict[str, str] = {}
    for edge in graph.edges:
        if edge.kind != "MEASURES":
            continue
        members.setdefault(edge.src, []).append(edge.dst)
        why[edge.src] = edge.why
    return sorted((cid, sorted(ms), why[cid]) for cid, ms in members.items())


# --------------------------------------------------------------------------- #
# The delta, which has to be measured before anything is rewired
# --------------------------------------------------------------------------- #
class Delta(NamedTuple):
    """How the graph's answer differs from the rule in force, per paper.

    Reported rather than applied. A decision rule replaced without this measurement is a
    different reviewer wearing the same name, and the whole argument for the graph is that
    the rule it replaces was never measured against anything either.
    """

    paper_id: str
    addresses: int
    agree: int
    graph_only: list           # the graph calls it CENTRAL, the current rule does not
    rule_only: list            # the current rule calls it CENTRAL, the graph does not
    graph_counts: dict
    rule_counts: dict

    @property
    def agreement(self) -> float | None:
        return self.agree / self.addresses if self.addresses else None


def compare_with_existing(doc: PaperDoc, graph: ClaimGraph | None = None,
                          links=None) -> Delta:
    """The graph's centrality against `materiality.basis_for_ref`, address by address.

    Compared against MATERIALITY rather than against `discovery._centrality`, deliberately.
    `_centrality`'s dominant input is whether a lens attacked the address, so comparing
    against it would be comparing a property of the paper with a property of the panel and
    the disagreement would be uninterpretable. `basis_for_ref` is the harness's existing
    structural answer to the same question this module asks, and it is the rule that
    actually gates a paper-level failure.
    """
    graph = graph if graph is not None else build(doc, links)
    agree, graph_only, rule_only = 0, [], []
    graph_counts = {"CENTRAL": 0, "SUPPORTING": 0, "PERIPHERAL": 0}
    rule_counts = {"material": 0, "none": 0}
    addresses = [r.address for r in graph.results.values()
                 if r.kind in ("TABLE_CELL", "REPORTED_QUANTITY")]
    for address in addresses:
        mine = graph_centrality(graph, address)
        graph_counts[mine] += 1
        ref = claims.resolve(doc, address)
        basis = materiality.basis_for_ref(ref, graph.abstract_idx, doc=doc)
        theirs = materiality.is_material(basis)
        rule_counts["material" if theirs else "none"] += 1
        if (mine == "CENTRAL") == theirs:
            agree += 1
        elif mine == "CENTRAL":
            graph_only.append(address)
        else:
            rule_only.append(address)
    return Delta(paper_id=doc.paper_id, addresses=len(addresses), agree=agree,
                 graph_only=graph_only, rule_only=rule_only,
                 graph_counts=graph_counts, rule_counts=rule_counts)


# --------------------------------------------------------------------------- #
def _fixture() -> PaperDoc:
    from .artifacts import Figure, QuantFinding, Section, Table
    return PaperDoc(
        paper_id="fixture", title="A Paper", n_pages=4,
        sections=[
            Section(section_idx=0, title="", page_start=1, text="A Paper. Some Authors."),
            Section(section_idx=1, title="Abstract", page_start=1,
                    text="We reach 91.4 accuracy on CIFAR-100, as Table 1 shows."),
            Section(section_idx=2, title="Results", page_start=2,
                    text="The baseline reaches 85.4 accuracy on CIFAR-100. "
                         "An ablation in Figure 1 removes the regulariser."),
            Section(section_idx=3, title="Conclusion", page_start=4,
                    text="The method improves accuracy on CIFAR-100."),
        ],
        tables=[Table(table_idx=1, page=2, label="1", caption="Table 1 results",
                      header=["method", "acc"], rows=[["ours", "91.4"], ["base", "85.4"]])],
        figures=[Figure(figure_idx=1, page=3, label="1", caption="Figure 1 ablation")],
        reported_numbers=[
            QuantFinding(metric="accuracy", benchmark="CIFAR-100", method="ours",
                         value="91.4", table_ref="T1:r0:c1", page=1,
                         source_quote="We reach 91.4 accuracy on CIFAR-100, as Table 1 shows."),
            QuantFinding(metric="accuracy", benchmark="CIFAR-100", method="base",
                         value="85.4", table_ref="T1:r1:c1", page=2,
                         source_quote="The baseline reaches 85.4 accuracy on CIFAR-100."),
        ],
    )


if __name__ == "__main__":       # self-check: python -m harness.claimgraph [paper_id]
    import json
    import pathlib
    import sys

    if len(sys.argv) > 1:
        from . import state
        pid = sys.argv[1]
        doc = PaperDoc(**state.read_json(
            pathlib.Path("projects") / pid / "paper" / "doc.json"))
        g = build(doc)
        print(json.dumps({"paper": pid, **g.counts(),
                          "comparisons": len(comparisons(g))}, indent=2))
        d = compare_with_existing(doc, g)
        print(json.dumps({"agreement": d.agreement, "graph": d.graph_counts,
                          "rule": d.rule_counts, "graph_only": len(d.graph_only),
                          "rule_only": len(d.rule_only)}, indent=2))
        raise SystemExit(0)

    doc = _fixture()
    g = build(doc)

    # The abstract's own sentence is a HEADLINE claim; the Results sentence is not.
    kinds = {c.kind for c in g.claims.values()}
    assert kinds == {"HEADLINE", "SUPPORTING"}, kinds
    assert any(c.kind == "HEADLINE" and "91.4" in c.text for c in g.claims.values())
    assert any(c.kind == "SUPPORTING" and "85.4" in c.text for c in g.claims.values())

    # A quantity that resolves to a cell is that cell, and not a second node beside it.
    assert "T1:r0:c1" in g.results and g.results["T1:r0:c1"].metric == "accuracy"
    assert sum(1 for r in g.results.values() if r.kind == "REPORTED_QUANTITY") == 0

    # The headline number is reached directly; the BASELINE is reached through the
    # comparison the paper itself set up, which is the chain a boolean cannot express.
    direct = dependency(g, "T1:r0:c1")
    assert direct is not None and direct.depth == 1, direct
    assert graph_centrality(g, "T1:r0:c1") == "CENTRAL"
    through = dependency(g, "T1:r1:c1")
    assert through is not None, "the baseline the headline is compared against"
    # THE CHAIN, and it is the point of the whole module: the abstract states its own
    # number, that number belongs to a comparison the paper set up, and the baseline is
    # the other member. No boolean over one object can express that, and a referee can
    # disagree with it because every hop names something openable.
    assert [e.kind for e in through.edges] == ["STATES", "MEASURED_BY", "MEASURES"], through
    assert "accuracy on cifar-100" in through.explain(g).lower(), through.explain(g)
    assert graph_centrality(g, "T1:r1:c1") == "CENTRAL"

    # A comparison is ONE node carrying its members, not one row per pair of numbers.
    comps = comparisons(g)
    assert len(comps) == 1, comps
    cid, members, why = comps[0]
    assert cid == "comparison:accuracy|cifar-100", cid
    assert members == ["T1:r0:c1", "T1:r1:c1"], members
    assert "2 methods" in why, why
    assert sum(1 for r in g.results.values() if r.kind == "COMPARISON") == 1

    # An abstract sentence that repeats almost verbatim elsewhere is still a claim node:
    # the address comes from the offset, not from searching the document for the text.
    assert any(c.kind == "HEADLINE" for c in g.claims.values())
    for claim_node in g.claims.values():
        assert claims.resolve(doc, claim_node.address).resolved, claim_node.address

    # Nothing about the review can reach this module, and the signature says so.
    import inspect
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    for forbidden in ("Finding", "TargetSet", "DiscoveredObject", "TargetOutcome",
                      "LensReport", "Grade"):
        assert f"import {forbidden}" not in src and f", {forbidden}" not in src, forbidden
    # `links` is the ONLY thing this module accepts besides the document, and a
    # `ClaimLinkSet` is a set of address pairs the harness has already checked against
    # this same document — no finding, no severity, no outcome. The guarantee the
    # docstring makes is that nothing about the REVIEW can reach the denominator, and it
    # still holds.
    assert list(inspect.signature(build).parameters) == ["doc", "links"]

    # WITH NO LINKS the graph is exactly what it was before the channel existed.
    assert build(doc).counts() == build(doc, None).counts()
    assert build(doc, []).counts() == build(doc).counts()
    assert all(e.kind != "SUPPORTED_BY" for e in build(doc).edges)

    # A verified link adds the edge the document does not print, and a REFUSED one does
    # not — the distinction the whole channel rests on.
    from .artifacts import ClaimLink
    accepted = ClaimLink(link_id="L1", claim_quote="x", claim_ref="P1:0-45",
                         claim_section_idx=1, evidence_ref="T1:r1:c1",
                         accepted=True, numeric_relation="EQUAL")
    refused = accepted.model_copy(update={"accepted": False, "refusal": "numeric_mismatch"})
    linked = build(doc, [accepted])
    assert any(e.kind == "SUPPORTED_BY" for e in linked.edges)
    assert build(doc, [refused]).counts() == build(doc).counts()

    # An address the graph does not hold answers None rather than guessing.
    assert dependency(g, "T9:r9:c9") is None
    assert graph_centrality(g, "T9:r9:c9") == "PERIPHERAL"

    delta = compare_with_existing(doc, g)
    assert delta.addresses == sum(1 for r in g.results.values()
                                  if r.kind in ("TABLE_CELL", "REPORTED_QUANTITY"))
    assert delta.agreement is not None
    print("harness.claimgraph self-check ok")
