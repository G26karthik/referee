"""Is an ESTABLISHED defect material to a central scientific claim of the paper?

`python -m harness.materiality` runs the self-check.

**The defect this module exists to close.** `TargetOutcome.establishes_failure` answers
"did this target's evidence route establish a defect" — per-target, route-local, correct.
`stages/report.claim_status` then treated ANY established failure as a paper-level
material failure, so a contradicted composition in an appendix footnote and a failed
reproduction of the paper's headline number reached `STOP_MATERIAL_FAILURE` by the same
path. Established defect and material failure are different claims, and one of them stops
a paper.

**Why `DiscoveredObject.centrality` cannot be the gate.** `discovery._centrality` returns
CENTRAL when `in_abstract or anchored_by_confirmed or self_checking`, and
`self_checking = bool(quantity and quantity.expression)` — i.e. "the paper printed
`a x b x c = d` here". That is a fact about the FORM of a sentence, not about what the
paper's conclusion rests on, and it is a PRECONDITION of the arithmetic route: no
composition expression, no `arithmetic_ok`, no `PAPER_ARITHMETIC_CONTRADICTION`. Keying
materiality on centrality would therefore be vacuous for exactly the route it was added to
gate — an appendix footnote reading "the pilot annotation used 12 x 3 = 40 scratch files,
which are not used anywhere in the evaluation" is classified CENTRAL today. Measured over
the eight-paper corpus, 105 of 112 CENTRAL objects are CENTRAL because a lens attacked the
address, which would also hand a model a second route to paper-level rejection.

`centrality` is kept, unchanged, for what it is actually good at: how strongly the
investigation should prioritise and track an object. It is no longer read as "failure here
invalidates the paper".

**What this is, stated exactly.** A CONSERVATIVE SUFFICIENT CONDITION for machine-
established paper-level materiality — not a materiality model, and not complete. A target
is material when it resolves inside the paper's own Abstract or Conclusion, or when one of
those sections explicitly cites the target's unique printed table, figure, equation or
numbered Result identity. Everything else is NONE, which means "not machine-established as
material", never "not important".

Cross-reference materiality is deliberately stricter than ordinary citation extraction.
The central heading must be unique, the recovered object must carry a unique printed
label, and the citation quote must resolve back into that section and name that exact
label. A numbered Result has no first-class extraction object, so it is admitted only when
exactly one non-central paper sentence defines ``Result N`` and the target resolves to
that sentence. A lens's prose, its centrality judgement and its proposed dependency are
not inputs to this module.

Under-stopping is the accepted direction. Over-stopping is not: a defect that fails this
test stays visible, ledger-recorded and reported under "## Established failures", and only
loses the authority to stop the paper.

**There is one abstract locator in this harness, and it is `abstract_section_idx` below.**
`discovery.discover` used to compute `abstract_idx = doc.sections[0].section_idx`, but
section 0 is the untitled front-matter block (title and authors); the real Abstract is a
TITLED section at index 1 on seven of the eight corpus papers and index 2 on the eighth.
The two spellings have now been unified, against a measured corpus re-derivation rather
than on the argument that the corrected one is obviously better:

    legacy locator (doc.sections[0]):     0 / 706 discovered objects
    corrected locator (this module):      0 / 706 discovered objects

No object's centrality moves either way, so nothing the v2 run reported is restated. The
reason it fires on nothing is structural — discovered objects come from table cells and
parsed quantities, and an abstract carries prose — and that null result is the strongest
evidence available that object-level centrality does not represent claim importance. A
claim-to-evidence linking module (`harness/claimgraph.py`) was built as an answer to this
and was deleted in the 2026-09-20 destructive simplification pass after it never once
produced a structurally-bound link across the measured corpus (see CLAUDE.md's Known
Limitations) — this basis-string vocabulary remains the only materiality signal.
"""
from __future__ import annotations

import re

# The closed vocabulary. `NONE` is not a failure of the paper and not a judgement about
# the claim; it says this harness did not machine-establish materiality for it.
MATERIALITY_BASES: tuple[str, ...] = (
    "ABSTRACT_CLAIM",   # the address resolves inside the paper's own Abstract
    "CONCLUSION_CLAIM",  # the address resolves inside the paper's own Conclusion
    "ABSTRACT_TABLE_REFERENCE",
    "CONCLUSION_TABLE_REFERENCE",
    "ABSTRACT_FIGURE_REFERENCE",
    "CONCLUSION_FIGURE_REFERENCE",
    "ABSTRACT_EQUATION_REFERENCE",
    "CONCLUSION_EQUATION_REFERENCE",
    "ABSTRACT_RESULT_REFERENCE",
    "CONCLUSION_RESULT_REFERENCE",
    "NONE",
)

# What each basis licenses, in a referee's words. Held here so the report, the ledger and
# the artifact cannot drift apart about what the token meant.
BASIS_REASON: dict[str, str] = {
    "ABSTRACT_CLAIM":
        "the claim resolves inside the paper's own Abstract, so the authors themselves "
        "selected it as part of the summary of their work",
    "CONCLUSION_CLAIM":
        "the claim resolves inside the paper's own Conclusion, so the authors themselves "
        "selected it as part of the paper's concluding claim",
    "ABSTRACT_TABLE_REFERENCE":
        "the paper's Abstract explicitly cites this uniquely labelled table",
    "CONCLUSION_TABLE_REFERENCE":
        "the paper's Conclusion explicitly cites this uniquely labelled table",
    "ABSTRACT_FIGURE_REFERENCE":
        "the paper's Abstract explicitly cites this uniquely labelled figure",
    "CONCLUSION_FIGURE_REFERENCE":
        "the paper's Conclusion explicitly cites this uniquely labelled figure",
    "ABSTRACT_EQUATION_REFERENCE":
        "the paper's Abstract explicitly cites this uniquely numbered equation",
    "CONCLUSION_EQUATION_REFERENCE":
        "the paper's Conclusion explicitly cites this uniquely numbered equation",
    "ABSTRACT_RESULT_REFERENCE":
        "the paper's Abstract explicitly cites this uniquely defined numbered Result",
    "CONCLUSION_RESULT_REFERENCE":
        "the paper's Conclusion explicitly cites this uniquely defined numbered Result",
    "NONE":
        "this review did not machine-establish that a central scientific claim depends on "
        "this target. That is a limit of this harness's materiality model, never a "
        "statement that the target does not matter",
}

# Mirrors `pdf._REFERENCES_HEADING`'s grammar: an optional section number or letter, an
# optional dot, then the word. Matching the paper's OWN heading is the whole point — a
# positional guess is what the legacy locator does, and it is wrong on 8 of 8 papers.
_ABSTRACT_HEADING = re.compile(r"^(?:\d+|[A-Z])?\.?\s*abstract\b", re.I)
_CONCLUSION_HEADING = re.compile(
    r"^(?:\d+|[A-Z])?\.?\s*(?:(?:discussion|summary)\s+(?:and|&)\s+)?"
    r"(?:conclusions?|concluding remarks)\b", re.I)
_TARGET_REFS: tuple[tuple[str, re.Pattern[str], str, str, str], ...] = (
    ("table", re.compile(r"^T(\d+):r\d+:c\d+$"), "tables", "table_idx", "label"),
    ("figure", re.compile(r"^F(\d+)$"), "figures", "figure_idx", "label"),
    ("equation", re.compile(r"^E(\d+)$"), "equations", "equation_idx", "number"),
)
_CITATION_WORD = {
    "table": r"(?:tables?|tabs?\.)",
    "figure": r"(?:figures?|figs?\.)",
    "equation": r"(?:equations?|eqs?\.|eqn\.?)",
    "result": r"results?",
}
_RESULT_AT_SENTENCE_START = re.compile(
    r"(?:^|(?<=[.!?])\s+)result\s+(\d+(?:\.\d+)*)\b", re.I)


def _unique_section_idx(doc, heading: re.Pattern[str]) -> int:
    hits: list[int] = []
    for s in (getattr(doc, "sections", None) or []):
        title = " ".join((getattr(s, "title", "") or "").split())
        if title and heading.match(title):
            hits.append(int(getattr(s, "section_idx", -1)))
    return hits[0] if len(hits) == 1 and hits[0] >= 0 else -1


def abstract_section_idx(doc) -> int:
    """The `section_idx` of the paper's Abstract, or -1 when no heading names one.

    -1 is a REFUSAL, and every caller below treats it as "no target can be material".
    A paper whose abstract heading extraction did not recover simply has no
    machine-established material target, which is the conservative answer.
    """
    return _unique_section_idx(doc, _ABSTRACT_HEADING)


def conclusion_section_idx(doc) -> int:
    """The uniquely headed Conclusion's section index, else the fail-closed -1."""
    return _unique_section_idx(doc, _CONCLUSION_HEADING)


def _section(doc, section_idx: int):
    hits = [s for s in (getattr(doc, "sections", None) or [])
            if int(getattr(s, "section_idx", -1)) == section_idx]
    return hits[0] if len(hits) == 1 else None


def _norm(text: str) -> str:
    return " ".join((text or "").replace("\u00ad", "").split())


def _label(text: str, kind: str) -> str:
    """Canonical printed object number, never an extraction/list index fallback."""
    value = _norm(text)
    if not value:
        return ""
    prefix = re.compile(rf"^(?:{_CITATION_WORD[kind]})\s*\(?\s*", re.I)
    return prefix.sub("", value).strip().strip("()").rstrip(".:").upper()


def _quote_cites(kind: str, label: str, quote: str) -> bool:
    if not label or not _norm(quote):
        return False
    pattern = re.compile(
        # A sentence-final full stop is punctuation, while ``2.1`` must not satisfy a
        # request for object 2. Reject a following word character or decimal continuation.
        rf"\b{_CITATION_WORD[kind]}\s*\(?\s*{re.escape(label)}(?!\w|\.\d)", re.I)
    return bool(pattern.search(_norm(quote)))


def _verified_citation(doc, section_idx: int, kind: str, label: str) -> bool:
    """Does a structured citation re-resolve to this exact central section and label?"""
    section = _section(doc, section_idx)
    if section is None:
        return False
    text = _norm(getattr(section, "text", "") or "")
    if not text:
        return False
    for xr in (getattr(doc, "crossrefs", None) or []):
        quote = _norm(getattr(xr, "quote", "") or "")
        if (str(getattr(xr, "kind", "") or "").lower() != kind
                or int(getattr(xr, "section_idx", -1)) != section_idx
                or _label(getattr(xr, "number", "") or "", kind) != label
                or not quote or text.count(quote) != 1
                or not _quote_cites(kind, label, quote)):
            continue
        return True
    return False


def _object_identity(doc, ref) -> tuple[str, str] | None:
    """The target's kind and unique printed label, or None on every ambiguity."""
    address = str(getattr(ref, "ref", "") or "")
    for kind, pattern, collection_name, idx_name, label_name in _TARGET_REFS:
        match = pattern.fullmatch(address)
        if not match:
            continue
        idx = int(match.group(1))
        collection = list(getattr(doc, collection_name, None) or [])
        targets = [o for o in collection if int(getattr(o, idx_name, -1)) == idx]
        if len(targets) != 1:
            return None
        printed = _label(getattr(targets[0], label_name, "") or "", kind)
        if not printed:
            return None
        same_label = [o for o in collection
                      if _label(getattr(o, label_name, "") or "", kind) == printed]
        return (kind, printed) if len(same_label) == 1 else None
    return None


def _result_identity(doc, ref, central_idxs: set[int]) -> tuple[str, str] | None:
    """A uniquely defined ``Result N`` addressed by a resolved paper-prose target."""
    if getattr(ref, "kind", "") not in ("prose_claim", "section_span"):
        return None
    target_idx = int(getattr(ref, "section_idx", -1))
    if target_idx < 0 or target_idx in central_idxs:
        return None
    quote = _norm(getattr(ref, "quote", "") or "")
    match = _RESULT_AT_SENTENCE_START.match(quote)
    section = _section(doc, target_idx)
    if not match or section is None:
        return None
    target_text = _norm(getattr(section, "text", "") or "")
    if not target_text or target_text.count(quote) != 1:
        return None
    label = _label(match.group(1), "result")
    definitions: list[tuple[int, str]] = []
    for s in (getattr(doc, "sections", None) or []):
        idx = int(getattr(s, "section_idx", -1))
        if idx in central_idxs:
            continue
        for found in _RESULT_AT_SENTENCE_START.finditer(_norm(getattr(s, "text", "") or "")):
            found_label = _label(found.group(1), "result")
            if found_label == label:
                definitions.append((idx, found_label))
    return ("result", label) if definitions == [(target_idx, label)] else None


def _central_result_citation(doc, section_idx: int, label: str) -> bool:
    section = _section(doc, section_idx)
    if section is None:
        return False
    text = _norm(getattr(section, "text", "") or "")
    # Unlike tables/figures/equations, Result has no first-class CrossRef extractor. The
    # exact paper text is therefore scanned here, but only after a unique definition has
    # bound the label to the resolved target above.
    return _quote_cites("result", label, text)


def basis_for_ref(ref, abstract_idx: int, *, doc=None) -> str:
    """The materiality basis for one resolved address. Total, and fails closed.

    Requires the address to have RESOLVED: an unresolved reference names no span in the
    paper, so nothing about where it sits is established either.
    """
    if ref is None:
        return "NONE"
    if not getattr(ref, "resolved", False):
        return "NONE"
    # When the document is available, re-derive the heading here. This prevents a stale
    # or caller-supplied index from bypassing the unique-heading refusal.
    if doc is not None:
        abstract_idx = abstract_section_idx(doc)
    # `section_idx` defaults to -1 on a ClaimRef that has none (a table cell, a figure),
    # and the explicit non-negative checks stop -1 == -1 reading as a match.
    section_idx = int(getattr(ref, "section_idx", -1))
    if abstract_idx >= 0 and section_idx == abstract_idx:
        return "ABSTRACT_CLAIM"
    if doc is None:
        return "NONE"
    conclusion_idx = conclusion_section_idx(doc)
    if conclusion_idx >= 0 and section_idx == conclusion_idx:
        return "CONCLUSION_CLAIM"

    central = (("ABSTRACT", abstract_idx), ("CONCLUSION", conclusion_idx))
    central_idxs = {idx for _, idx in central if idx >= 0}
    identity = _object_identity(doc, ref) or _result_identity(doc, ref, central_idxs)
    if identity is None:
        return "NONE"
    kind, label = identity
    for central_name, idx in central:
        if idx < 0:
            continue
        cited = (_central_result_citation(doc, idx, label) if kind == "result"
                 else _verified_citation(doc, idx, kind, label))
        if cited:
            return f"{central_name}_{kind.upper()}_REFERENCE"
    return "NONE"


def is_material(basis: str = "") -> bool:
    """Only a named paper-owned basis licenses a paper-level stop."""
    return (basis or "").strip().upper() in set(MATERIALITY_BASES) - {"NONE"}


def basis_for_target(target_id: str, objects) -> str | None:
    """The stored basis for a target, or None when the target has NO object at all.

    `None` and `"NONE"` are different answers and the caller must be able to tell them
    apart: `"NONE"` is "this target exists and materiality was not established for it",
    `None` is "this target has no object in the set", which is a state-consistency defect
    in this harness — see `unjoinable_established_failures`.
    """
    # A blank id joins NOTHING, whatever the object set contains. Every target the
    # pipeline builds carries a real id, so a blank one means the caller has no target
    # identity to offer — and letting it match an object that also happens to carry a
    # blank id would be a join by accident on the one path where a wrong answer convicts.
    if not (target_id or "").strip():
        return None
    for o in (objects or []):
        if getattr(o, "target_id", "") == target_id:
            return (getattr(o, "materiality_basis", "") or "NONE")
    return None


def material_target_failure(objects, outcomes):
    """THE ONE PRIMITIVE. The first outcome that both establishes a defect AND is material.

    Route-blind by construction: it reads `establishes_failure` (which owns the provenance
    ceiling for execution routes and the deterministic-recompute precondition for the
    arithmetic route) and the stored materiality basis. There is deliberately no branch on
    provenance, disposition or route here, so "one rule for arithmetic and another for
    execution" is inexpressible rather than merely absent.

    A target whose object is missing is NOT material — never material by default — and is
    reported separately rather than silently skipped.
    """
    for o in (outcomes or []):
        if not getattr(o, "establishes_failure", False):
            continue
        if is_material(basis_for_target(getattr(o, "target_id", ""), objects) or "NONE"):
            return o
    return None


def established_defects(outcomes) -> list:
    """TIER 1 ALONE: every outcome whose own evidence route established a defect.

    Deliberately separate from `material_target_failure`, and deliberately blind to
    materiality, objects and route. It answers "did this review prove something is wrong
    with the paper", which is a different question from "is that enough to reject the
    paper" — and a disposition that could not tell them apart called a paper with a
    proven arithmetic contradiction in it CLEAN.
    """
    return [o for o in (outcomes or []) if getattr(o, "establishes_failure", False)]


def has_established_defect(outcomes) -> bool:
    """Tier 1, as a predicate. See `established_defects`."""
    return bool(established_defects(outcomes))


def unjoinable_established_failures(objects, outcomes) -> list[str]:
    """Target ids that established a defect and have NO object to assess materiality from.

    A reviewer implementation defect, never evidence about the paper: it means the target
    set the report is folding over disagrees with the outcomes attached to it. Surfaced
    through `guarantees.ACCOUNTING_REPRODUCIBLE`, which is where a count that cannot be
    re-derived from artifacts already belongs.
    """
    return [getattr(o, "target_id", "") for o in (outcomes or [])
            if getattr(o, "establishes_failure", False)
            and basis_for_target(getattr(o, "target_id", ""), objects) is None]


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    from .artifacts import ClaimRef, DiscoveredObject, PaperDoc, Section, TargetOutcome

    # --- the vocabulary is closed and every entry is glossed --------------------------
    assert set(BASIS_REASON) == set(MATERIALITY_BASES)
    assert is_material("ABSTRACT_CLAIM") and not is_material("NONE") and not is_material("")
    for junk in ("abstract", " ABSTRACT_CLAIM ", "CENTRAL", "ABSTRACT"):
        assert is_material(junk) == (junk.strip().upper() == "ABSTRACT_CLAIM"), junk

    # --- THE LOCATOR: by heading, never by position -----------------------------------
    # The real corpus shape: section 0 is the untitled front-matter block and the Abstract
    # is a titled section further down. The legacy locator answers 0 here and is wrong.
    doc = PaperDoc(paper_id="p", sections=[
        Section(section_idx=0, title="", text="A Paper Title\nA. Author, B. Author"),
        Section(section_idx=1, title="Abstract", text="We report 58 x 5 x 10 = 2,901 cases."),
        Section(section_idx=2, title="1. Introduction", text="..."),
        Section(section_idx=3, title="Appendix A", text="12 x 3 = 40 scratch files.")])
    assert abstract_section_idx(doc) == 1, "the Abstract is found by its own heading"
    assert doc.sections[0].section_idx == 0, "and it is NOT the first section"

    # every real spelling seen in the corpus, plus numbered variants
    for title, hit in (("Abstract", True), ("ABSTRACT", True), ("abstract", True),
                       ("1. Abstract", True), ("A. Abstract", True), ("Abstract.", True),
                       ("5 Leaderboard", False), ("Abstractive Summarisation", False),
                       ("", False), ("Introduction", False)):
        d = PaperDoc(paper_id="p", sections=[Section(section_idx=7, title=title, text="t")])
        assert (abstract_section_idx(d) == 7) == hit, (title, hit)
    assert abstract_section_idx(PaperDoc(paper_id="p")) == -1, "no sections, no abstract"

    # --- the basis, and the -1 trap ----------------------------------------------------
    in_abs = ClaimRef(ref="P1:0-20", kind="prose_claim", section_idx=1, resolution="resolved")
    in_apx = ClaimRef(ref="P3:0-20", kind="prose_claim", section_idx=3, resolution="resolved")
    cell = ClaimRef(ref="T1:r0:c1", kind="table_cell", resolution="resolved")
    assert basis_for_ref(in_abs, 1) == "ABSTRACT_CLAIM"
    assert basis_for_ref(in_apx, 1) == "NONE"
    assert basis_for_ref(cell, 1) == "NONE", "a table cell carries no section and is not material"
    assert basis_for_ref(None, 1) == "NONE"
    # THE TRAP: a ClaimRef with no section defaults to -1, and a paper with no abstract
    # locates -1. Those must not meet.
    assert int(cell.section_idx) == -1 and basis_for_ref(cell, -1) == "NONE"
    assert basis_for_ref(in_abs, -1) == "NONE", "no abstract located, nothing is material"
    # an unresolved address establishes no location either
    unresolved = ClaimRef(ref="P1:0-20", kind="prose_claim", section_idx=1,
                          resolution="not_found")
    assert not unresolved.resolved and basis_for_ref(unresolved, 1) == "NONE"

    # --- the primitive is route-blind --------------------------------------------------
    # Swept over the CODE with the docstring removed, because the docstring's whole job is
    # to name the routes this must not branch on.
    import ast
    tree = ast.parse(inspect.getsource(material_target_failure).lstrip())
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    for forbidden in ("repo_exec", "reimpl_exec", "PAPER_ARITHMETIC", "FAILED_REPRODUCTION",
                      "provenance", "disposition"):
        assert forbidden not in code, (
            f"{forbidden} in the shared primitive would make materiality route-specific")

    material = DiscoveredObject(target_id="A", materiality_basis="ABSTRACT_CLAIM")
    incidental = DiscoveredObject(target_id="B", materiality_basis="NONE")

    def _out(tid, disposition, provenance=""):
        return TargetOutcome(target_id=tid, disposition=disposition, provenance=provenance)

    # Every route that can establish a defect, gated identically.
    for disposition, prov in (("PAPER_ARITHMETIC_CONTRADICTION", "paper"),
                              ("FAILED_REPRODUCTION", "repo_exec"),
                              ("FAILED_REPRODUCTION", "reimpl_exec"),
                              ("FAILED_REPRODUCTION", "driver")):
        hit = _out("A", disposition, prov)
        miss = _out("B", disposition, prov)
        assert hit.establishes_failure and miss.establishes_failure, (disposition, prov)
        assert material_target_failure([material, incidental], [hit]) is hit, (disposition, prov)
        assert material_target_failure([material, incidental], [miss]) is None, (disposition, prov)
        # and the material one is found wherever it sits in the list
        assert material_target_failure([material, incidental], [miss, hit]) is hit

    # a defect the ceiling never admitted cannot become material by sitting on a material
    # object — Tier 1 has to pass first
    inadmissible = _out("A", "FAILED_REPRODUCTION", "synthesized")
    assert not inadmissible.establishes_failure
    assert material_target_failure([material], [inadmissible]) is None
    # nor can a blocked target
    assert material_target_failure([material], [_out("A", "RESOURCE_BLOCKED")]) is None

    # --- Tier 1 alone, and it is NOT the same question as Tier 2 -----------------------
    hit = _out("A", "PAPER_ARITHMETIC_CONTRADICTION", "paper")
    miss = _out("B", "PAPER_ARITHMETIC_CONTRADICTION", "paper")
    blocked = _out("A", "RESOURCE_BLOCKED")
    assert has_established_defect([miss]), (
        "an established defect on a non-material target is still an established defect")
    assert material_target_failure([material, incidental], [miss]) is None
    assert established_defects([hit, miss, blocked]) == [hit, miss]
    assert not has_established_defect([blocked]) and not has_established_defect([])
    # blind to objects and to materiality: it takes no objects at all
    assert set(inspect.signature(has_established_defect).parameters) == {"outcomes"}
    assert set(inspect.signature(established_defects).parameters) == {"outcomes"}

    # --- the missing join: never material, always reported -----------------------------
    orphan = _out("GHOST", "PAPER_ARITHMETIC_CONTRADICTION", "paper")
    assert basis_for_target("GHOST", [material]) is None
    assert material_target_failure([material], [orphan]) is None, (
        "an unjoinable established failure must never default to material")
    assert unjoinable_established_failures([material], [orphan]) == ["GHOST"]
    assert unjoinable_established_failures([material], [_out("A", "PAPER_ARITHMETIC_CONTRADICTION")]) == []
    # a BLOCKED orphan is not an accounting defect: it established nothing to join for
    assert unjoinable_established_failures([], [_out("GHOST", "RESOURCE_BLOCKED")]) == []
    # "" and None are different answers
    assert basis_for_target("B", [incidental]) == "NONE"
    assert basis_for_target("B", []) is None
    # a blank id joins nothing, even against an object that carries a blank id
    blank = DiscoveredObject(target_id="", materiality_basis="ABSTRACT_CLAIM")
    for empty in ("", "   ", None):
        assert basis_for_target(empty, [blank, material]) is None, repr(empty)
    assert material_target_failure(
        [blank], [TargetOutcome(target_id="", disposition="PAPER_ARITHMETIC_CONTRADICTION",
                                provenance="paper")]) is None

    print("harness.materiality self-check ok")


if __name__ == "__main__":
    _self_check()
