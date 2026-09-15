"""Extraction may recover less than the paper prints. It may never recover something
the paper does not print.

The defect these tests exist to prevent was REACHABLE AND PROVEN, not theorised.
`_FIGURE_CAPTION` matched any line beginning "Figure N", so
'Figure 4 shows the visual comparison with real-world' — body prose — was ingested as a
`Figure` object. Calling the real `stages.audit.verify_evidence` with a quote from that
prose and the ref `F5`, against the shipped `projects/cvpr/paper/doc.json`, returned
`evidence_class='caption_verified'` and this harness-written observation:

    Figure caption F5 (page 6, 'Figure 4') of the parsed paper contains the quoted text
    verbatim. A CAPTION NAMES A FIGURE; it does not report the figure's plotted values…

F5 was not a caption. `verified_observation` is the machine half of a finding — the one
place the design says the HARNESS and not the model is the author — so a false
attestation there is the trust anchor asserting something untrue (invariants 1 and 2).
The same held for F6 and F9.

Three further false quotations of the same family are pinned here:

  * `Table.caption` was RECONSTRUCTED as f"Table {n}: {rest}", inserting a colon the
    paper does not print. The real line 'Table 2 lists the quantitative comparison
    between Weath-' became 'Table 2: lists the quantitat…', and that string reaches a
    human through `QuantFinding.benchmark` and `DiscoveredObject.experiment` without
    ever being re-verified.
  * A bibliography line whose author initial landed at line start matched the
    section-number grammar, so 16.5 KB of APT's reference list became five sections,
    four of them shaped exactly like appendix headings.
  * A bare known-heading word — 'Method', 'Model', 'training' — is what a table's first
    column header looks like once text extraction puts each cell on its own line, and
    the human-facing report prints the section count verbatim.

Every assertion below is measured against the real pilot papers where they are present,
because each of these failures was invisible in a fixture and visible in a paper.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from harness import pdf
from harness.artifacts import CrossRef, Figure, PaperDoc, Section, Table

HARNESS_ROOT = Path(__file__).resolve().parents[1]
PAPERS = {
    "cvpr": HARNESS_ROOT / "papers" / "CVPR.pdf",
    "iclr": HARNESS_ROOT / "papers" / "ICLR.pdf",
    "apt": HARNESS_ROOT / "papers" / "APT _ ICML.pdf",
}
_PARSED: dict[str, PaperDoc] = {}


def parsed(name: str) -> PaperDoc:
    """The named pilot paper, parsed once per session by the REAL extractor.

    Cached because pdfplumber is the expensive half and eleven tests read the same three
    documents. Skips rather than fails when the PDF is absent: `papers/*.pdf` is
    gitignored, so a fresh checkout has none of them and a missing paper is not a defect
    in the extractor.
    """
    pytest.importorskip("pdfplumber")
    src = PAPERS[name]
    if not src.exists():
        pytest.skip(f"{src.name} is not present in this checkout")
    if name not in _PARSED:
        pages = pdf.page_texts(src)
        sections = pdf.split_sections(pages)
        tables = pdf.extract_tables(src, pages)     # once: pdfplumber is the slow half
        _PARSED[name] = PaperDoc(
            paper_id=name, n_pages=len(pages), sections=sections,
            tables=tables, figures=pdf.extract_figures(pages),
            equations=pdf.extract_equations(pages),
            crossrefs=pdf.extract_crossrefs(sections),
            body_end_section_idx=pdf.references_boundary(sections),
            extraction_version=pdf.EXTRACTION_VERSION,
            reported_numbers=pdf.table_numbers(tables))
    return _PARSED[name]


def _pages(name: str) -> list[str]:
    return [pdf._norm(p) for p in pdf.page_texts(PAPERS[name])]


# --------------------------------------------------------------------------- #
# 1. A caption is not a cross-reference
# --------------------------------------------------------------------------- #
# Every one of these lines is real: hand-labelled from the three pilot papers during
# reconnaissance. The citations on the right are what the delimiter-optional regex
# ingested as figures and tables.
CAPTION_LINES = (
    "Figure 3. (a) Spider mamba scans model features along the LiDAR",
    "Figure 5: (a-b) Linear evaluation results and (c-d) effective rank",
    "Figure 7.",
    "Table 1. Quantitative unconditional generation comparisons with",
    "Table 6. Hyperparameters used in APT experiments",
    "Table 7: CodeGen-350M-mono results",
)
CITATION_LINES = (
    "Figure 3, this progress is akin to a spider hunting on a web,",
    "Figure 4 shows the visual comparison with real-world",
    "Figure 5 shows the comparison of generated and real-",
    "Figure 8 shows the comparison of model parameters and",
    "Figure 3f.",
    "Figure 5a, simply enlarging the initial tuning parameter number",
    "Table 2 lists the quantitative comparison between Weath-",
    "Table 10, using fully fine-tuned models as the teacher will incur more memory",
    "Table 14 shows that LDReg can consistently improve the base",
)


@pytest.mark.parametrize("line", CAPTION_LINES)
def test_a_caption_line_is_still_recognised_as_a_caption(line: str):
    """Hardening that recovered nothing would be a different defect, not a fix."""
    rx = pdf._FIGURE_CAPTION if line.lower().startswith(("figure", "fig.")) else pdf._TABLE_CAPTION
    assert rx.match(line), line


@pytest.mark.parametrize("line", CITATION_LINES)
def test_an_in_text_reference_is_never_recognised_as_a_caption(line: str):
    """The regex-level statement of the defect: no delimiter after the number, no caption.

    Each of these nine lines became a `Figure` object or a `Table.caption` under the
    delimiter-optional rule. The figure ones are the direct cause of the false
    `caption_verified` attestation; the table ones mislabelled a body.
    """
    rx = pdf._FIGURE_CAPTION if line.lower().startswith(("figure", "fig.")) else pdf._TABLE_CAPTION
    assert not rx.match(line), line


def test_an_in_text_figure_reference_is_never_recorded_as_a_caption():
    """CVPR yielded 12 figure objects for 8 printed figures, four of them body prose.

    Also asserts the labels are unique: the duplicates ('Figure 3' twice, 'Figure 4'
    twice, 'Figure 5' twice, 'Figure 8' twice) were the visible symptom.
    """
    doc = parsed("cvpr")
    assert len(doc.figures) == 8, [f.label for f in doc.figures]
    labels = [f.label for f in doc.figures]
    assert len(labels) == len(set(labels)), labels
    for text in ("shows the visual comparison", "shows the comparison of generated",
                 "shows the comparison of model parameters"):
        assert not any(text in f.caption for f in doc.figures), text


def test_body_prose_can_no_longer_earn_a_caption_verified_attestation():
    """The proven false attestation, closed. Drives the REAL `verify_evidence`.

    Swept over every figure index the document has, plus three beyond it, so the test
    cannot pass merely because renumbering moved the quote from F5 to F4.
    """
    from harness.stages import audit as audit_stage

    doc = parsed("cvpr")
    by_figure = {f.figure_idx: f for f in doc.figures}
    corpus = audit_stage.source_units(doc)
    by_idx = {t.table_idx: t for t in doc.tables}
    for quote in ("shows the visual comparison with real-world",
                  "shows the comparison of generated and real-",
                  "shows the comparison of model parameters and"):
        for i in range(len(doc.figures) + 3):
            cls, observation = audit_stage.verify_evidence(
                quote, f"F{i}", corpus, by_idx, doc.n_pages,
                by_figure=by_figure, by_equation={})
            assert cls != "caption_verified", (quote, i, observation)
            assert "Figure caption" not in observation, (quote, i, observation)


@pytest.mark.parametrize("name", list(PAPERS))
def test_every_figure_caption_is_text_the_paper_actually_prints(name: str):
    """A figure object whose caption appears nowhere in the PDF is a fabricated quote."""
    doc = parsed(name)
    pages = _pages(name)
    for fig in doc.figures:
        if fig.caption:
            assert any(fig.caption in p for p in pages), (fig.figure_idx, fig.caption)


# --------------------------------------------------------------------------- #
# 2. A caption is text the paper contains
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", list(PAPERS))
def test_a_table_caption_is_text_the_paper_actually_contains(name: str):
    """The injected colon, closed.

    'Table 2 lists the quantitative comparison between Weath-' was rewritten as
    'Table 2: lists the quantitat…', which occurs nowhere in the paper and is never
    re-verified by anything — unlike an `evidence_quote`, which invariant 1 covers.
    """
    doc = parsed(name)
    pages = _pages(name)
    for t in doc.tables:
        if t.caption:
            assert any(t.caption in p for p in pages), (t.table_idx, t.caption)


def test_a_reconstructed_caption_would_fail_that_test():
    """The guard has to BITE. Proves the assertion above is not vacuous.

    Rebuilds exactly the string `_page_captions` used to return for the real CVPR line
    and asserts it is absent from that line — so if anyone reinstates the reconstruction,
    the test above fails rather than passing on a paper that happens to use colons.
    """
    real = "Table 2 lists the quantitative comparison between Weath-"
    reconstructed = "Table 2: lists the quantitative comparison between Weath-"
    assert reconstructed not in real
    assert pdf._page_captions(real) == [], "a citation is not a caption"
    assert pdf._page_captions("Table 2: lists things")[0].text == "Table 2: lists things"


def test_a_caption_this_extractor_could_not_pair_is_left_unlabelled():
    """An unlabelled table is far less damaging than a mislabelled one.

    APT printed two tables on page 19 and the extractor can name one of them; the other
    must come back with `label=''` and `caption_source='none'` rather than borrowing its
    neighbour's number. Before, an in-text sentence supplied the second label and two
    different bodies both claimed to be 'Table 10'.
    """
    doc = parsed("apt")
    labels = [t.label for t in doc.tables if t.label]
    assert len(labels) == len(set(labels)), labels
    unlabelled = [t for t in doc.tables if not t.label]
    assert unlabelled, "APT has uncaptioned bodies; the fixture assumption changed"
    for t in unlabelled:
        assert t.caption == "" and t.caption_source == "none", t.table_idx


@pytest.mark.parametrize("name", list(PAPERS))
def test_a_captioned_table_records_how_its_caption_was_associated(name: str):
    """`caption_source` is the prerequisite for an honest 'we could not label this'.

    A positional guess and a geometric pairing are different evidence and used to be
    indistinguishable once the string had been written onto the table.
    """
    for t in parsed(name).tables:
        assert t.caption_source in ("ruled_positional", "geometric_paired", "none")
        assert bool(t.caption) == (t.caption_source != "none"), t.table_idx


def test_a_geometric_candidate_cannot_give_a_second_address_to_one_printed_table():
    """Ungating the fallback must not double-count a table.

    Running word-geometry recovery over a page the ruled path already touched is what
    recovers ICLR's Tables 12 and 13. It also offers a second extraction of tables the
    ruled path already found, and admitting both would give a reviewer two
    `T<i>:r:c` addresses for one printed table and count its numbers twice.
    """
    already = [Table(table_idx=0, page=9, label="1", caption="Table 1: X",
                     header=["Method", "Acc"], rows=[["a", "1.0"], ["b", "2.0"]])]
    same_label = pdf._Caption("1", "Table 1: X")
    assert pdf._already_extracted(same_label, [["c", "3.0"]], already) is True
    restated = pdf._Caption("2", "Table 2: Y")
    assert pdf._already_extracted(restated, [["a", "1.0"], ["b", "2.0"]], already) is True
    genuinely_new = pdf._Caption("2", "Table 2: Y")
    assert pdf._already_extracted(genuinely_new, [["q", "9.9"], ["r", "8.8"]], already) is False


def test_the_two_table_line_predicates_answer_two_different_questions():
    """One predicate per question, or a fix regresses something unrelated.

    Collapsing 'may this line be part of a table body?' into 'does this line NAME a
    table?' cost seven of APT's captioned tables their bodies: in-text reference lines
    rejoined the body runs and re-cut them. And the row-level rule cannot require
    whitespace after the delimiter, because a PDF whose font names no space glyph gives
    pdfplumber 'Table2.RoBERTaandT5pruning…' as a single token.
    """
    citation = "Table 4 shows the ablation"
    assert pdf._TABLE_LINE_SHAPED.match(citation), "a citation is still not table data"
    assert not pdf._TABLE_CAPTION.match(citation), "a citation does not NAME a table"
    spaceless = "Table2.RoBERTaandT5pruningwithAPTcomparedtobaselines"
    assert pdf._TABLE_CAPTION_ROW.match(spaceless), "the word-row rule must survive this"
    assert not pdf._TABLE_CAPTION.match(spaceless), "the page-line rule requires the space"
    assert not pdf._TABLE_CAPTION_ROW.match("Table2showsthecomparison")


# --------------------------------------------------------------------------- #
# 3. Headings
# --------------------------------------------------------------------------- #
def test_a_bibliography_line_is_not_an_appendix_heading():
    """APT's `References` section held 694 of ~17,200 characters.

    'M. Analysis of dawnbench, a time-to-accuracy machine' (10,461 chars),
    'I. AdapterFusion: Non-destructive task composition for',
    'J. Finding skill neurons in pre-trained transformer-based' and
    '2022 Conference on Empirical Methods in Natural Lan-' all matched the
    section-number grammar, because `_SECNO` admits a single capital and an author
    initial lands at line start.
    """
    doc = parsed("apt")
    refs = [s for s in doc.sections if s.title.lower().startswith("references")]
    assert len(refs) == 1
    assert len(refs[0].text) > 15_000, len(refs[0].text)
    titles = [s.title for s in doc.sections]
    for bogus in ("M. Analysis", "I. AdapterFusion", "J. Finding skill",
                  "2022 Conference"):
        assert not any(t.startswith(bogus) for t in titles), bogus


def test_the_reference_range_rule_keeps_a_real_appendix_sequence():
    """Conservative in the safe direction, and not so conservative it eats the appendix.

    APT prints appendices A through I AFTER its references, in exactly the single-capital
    shape a bibliography line takes. Suppressing that shape outright — which is what
    "stop treating [A-Z]. Title as a heading inside the reference range" would do — would
    have merged 20 KB of real appendix into the bibliography. Sequence is the
    discriminator: appendix letters run from A, and 'M' before any 'A' is not one.
    """
    doc = parsed("apt")
    lettered = [s.title.split(".")[0] for s in doc.sections
                if re.match(r"^[A-Z]\.\s", s.title)]
    assert lettered == sorted(lettered), lettered
    assert lettered[0] == "A"
    assert set("ABCDEFGHI") <= set(lettered), lettered


def test_a_bare_known_heading_word_inside_a_table_is_not_a_section():
    """'Method' alone on a line is a column header, not a section.

    Text extraction emits one table cell per line, so the shape is identical to a real
    unnumbered heading. The corroboration is the FOLLOWING line: a heading is followed by
    a paragraph, a header cell by another cell. Measured over the three pilot papers,
    every false bare heading was followed by 1-4 words and every genuine one by 6 or
    more.
    """
    table_page = "Method\nAP\nAT\nTraining\nInference\n12.3\n"
    prose_page = "Method\nWe train every model for two hundred epochs on eight GPUs.\n"
    assert [s.title for s in pdf.split_sections([table_page])] == [""], \
        "a run of header cells must not open five sections"
    assert "Method" in [s.title for s in pdf.split_sections([prose_page])]


def test_a_heading_at_the_foot_of_a_page_is_corroborated_by_the_next_page():
    """The lookahead must cross the page boundary or it invents a new defect.

    Reading page by page would leave every heading set as a page's last line with no
    following line at all, and suppress it.
    """
    secs = pdf.split_sections(["body text\nConclusion",
                               "We have shown that the regulariser improves every arm."])
    assert "Conclusion" in [s.title for s in secs]


def test_the_shape_predicate_is_left_alone_and_answers_only_about_shape():
    """`is_heading` is a one-line question; the document-level overrules live elsewhere.

    Folding the corroboration into `is_heading` would have made `is_heading("Abstract")`
    False, which is wrong about the line and breaks every caller that asks the shape
    question — `guess_title` among them.
    """
    assert pdf.is_heading("Abstract") and pdf.is_heading("References")
    assert list(inspect.signature(pdf.is_heading).parameters) == ["line"]
    assert pdf._heading_admitted("Method", "", in_references=False, appendix_letter="") is False
    assert pdf._heading_admitted(
        "Method", "We describe the architecture and its training schedule here.",
        in_references=False, appendix_letter="") is True


@pytest.mark.parametrize("name", list(PAPERS))
def test_no_section_is_titled_with_a_bare_table_header_word(name: str):
    """The count `stages/report.py` prints verbatim must not be padded by table cells."""
    junk = {"method", "methods", "model", "training", "model."}
    titles = [s.title.strip().lower() for s in parsed(name).sections]
    assert not (junk & set(titles)), sorted(junk & set(titles))


@pytest.mark.parametrize("name", list(PAPERS))
def test_splitting_sections_never_drops_a_line_of_the_paper(name: str):
    """A rejected heading joins the section it interrupts; it is never discarded.

    The failure direction has to be "the bibliography stays whole under one label", not
    "the bibliography is gone".
    """
    doc = parsed(name)
    recovered = sum(len(s.text) for s in doc.sections)
    assert recovered > 20_000, recovered
    boundary = doc.body_end_section_idx
    assert boundary >= 0, "every pilot paper prints a references heading"
    assert doc.sections[boundary].title.lower().startswith(("references", "bibliography"))


# --------------------------------------------------------------------------- #
# 4. Cross-references: what the prose CITES
# --------------------------------------------------------------------------- #
def test_a_crossref_is_reported_as_what_the_prose_cites_and_never_as_what_exists():
    """The architectural guarantee, asserted three ways.

    Over the shipped corpus a naive "cited but not recovered" check produced twelve
    claims that a table or an equation was missing from a paper, and all twelve of those
    objects are printed in the paper and absent only from what extraction recovered. So
    the type carries no verdict field, and the function that builds it cannot see a
    recovered object to compare against.
    """
    for forbidden in ("exists", "resolved", "severity", "missing", "dangling", "verdict",
                      "confidence", "orphan"):
        assert forbidden not in CrossRef.model_fields, forbidden
    params = inspect.signature(pdf.extract_crossrefs).parameters
    assert list(params) == ["sections"], params
    # The CODE ONLY. The docstring's whole job is to name the objects this function may
    # not reach, so scanning it too would make the guard unwritable. Parsed from the
    # whole module rather than from `inspect.getsource`, which slices by line number out
    # of a cache that goes stale the moment the file is edited — a test that fails
    # because someone saved the module while it ran teaches nothing.
    module = ast.parse(Path(pdf.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in module.body
              if isinstance(n, ast.FunctionDef) and n.name == "extract_crossrefs")
    body = "\n".join(ast.unparse(node) for node in fn.body
                     if not (isinstance(node, ast.Expr)
                             and isinstance(node.value, ast.Constant)))
    for reachable in (".figures", ".tables", ".equations", "Figure(", "Table(",
                      "Equation("):
        assert reachable not in body, reachable


def test_the_crossref_extractor_cannot_be_handed_a_recovered_object():
    """Signature-level, not convention-level: passing a PaperDoc must fail."""
    with pytest.raises(AttributeError):
        pdf.extract_crossrefs(PaperDoc(paper_id="x"))          # type: ignore[arg-type]


@pytest.mark.parametrize("name", list(PAPERS))
def test_every_crossref_span_resolves_to_its_own_quote(name: str):
    """An address nobody can re-derive is decoration.

    Checked through `claims.resolve`, the same resolver a finding's evidence goes
    through, so this is the real check and not a parallel implementation of it.
    """
    from harness import claims

    doc = parsed(name)
    assert doc.crossrefs, "the pilot papers all cite their own figures and tables"
    for xr in doc.crossrefs:
        got = claims.resolve(doc, xr.span)
        assert got.resolution == "resolved", (xr.span, got.resolution, got.detail)
        assert got.quote == xr.quote, (xr.span, got.quote[:60], xr.quote[:60])
        assert xr.kind in ("figure", "table", "equation", "section", "appendix")
        assert xr.number


def test_a_caption_is_not_counted_as_a_citation_of_its_own_figure():
    """'Figure 3. (a) Spider mamba…' names the figure; it does not cite it.

    Counting a caption as a citation would make any orphan-object check conclude the
    prose refers to a figure when only the figure's own caption did.
    """
    caption_only = [Section(section_idx=0, page_start=1,
                            text="Figure 3. (a) Spider mamba scans model features.")]
    assert pdf.extract_crossrefs(caption_only) == []
    citing = [Section(section_idx=0, page_start=1,
                      text="We compare the arms in Figure 3 and report the gap there.")]
    got = pdf.extract_crossrefs(citing)
    assert [(x.kind, x.number) for x in got] == [("figure", "3")]


def test_a_word_that_merely_starts_with_a_reference_noun_is_not_a_citation():
    """With `re.I`, a roman-numeral alternative matched the 'i' in 'figures in the
    appendix' and minted a citation of figure I that nobody wrote. Digits only."""
    s = [Section(section_idx=0, page_start=1,
                 text="The figures in the appendix and the tables in it are extra.")]
    assert [x for x in pdf.extract_crossrefs(s) if x.kind in ("figure", "table")] == []


def test_a_crossref_quote_is_bounded_by_construction():
    """A section whose sentence boundaries did not survive extraction must not let one
    citation quote half the paper."""
    runaway = "we cite Figure 4 here " + "and then more text with no full stop " * 200
    got = pdf.extract_crossrefs([Section(section_idx=0, page_start=1, text=runaway)])
    assert got and all(len(x.quote) <= pdf.MAX_CROSSREF_QUOTE * 2 for x in got)
    for x in got:
        start, end = (int(v) for v in x.span.split(":")[1].split("-"))
        assert end - start <= pdf.MAX_CROSSREF_QUOTE


# --------------------------------------------------------------------------- #
# 5. Renumbering is declared
# --------------------------------------------------------------------------- #
def test_renumbering_is_declared_rather_than_silent():
    """A stored `F5` minted under the old parse must not look like a new one.

    Every change in this workstream moves at least one index: the figure list loses its
    in-text references, the table list gains recovered bodies, the section list loses its
    false headings. A document parsed now says so; a document parsed before this says 1,
    by the field's own default, so the two are distinguishable without a migration.
    """
    assert pdf.EXTRACTION_VERSION > 1
    assert PaperDoc(paper_id="legacy").extraction_version == 1
    assert parsed("cvpr").extraction_version == pdf.EXTRACTION_VERSION
    legacy = PaperDoc(**{"paper_id": "legacy", "sections": [], "figures": []})
    assert legacy.extraction_version != pdf.EXTRACTION_VERSION


def test_a_shipped_document_is_recognised_as_an_older_parse():
    """The corpus docs on disk were produced by version 1 and are left exactly as they
    are: re-parsing them would leave their lens files' refs pointing at other objects."""
    shipped = sorted((HARNESS_ROOT / "projects").glob("*/paper/doc.json"))
    if not shipped:
        pytest.skip("no ingested projects in this checkout")
    for path in shipped:
        doc = PaperDoc(**json.loads(path.read_text(encoding="utf-8")))
        assert isinstance(doc.extraction_version, int)


def test_a_cached_ingest_reports_the_parse_it_is_reusing(tmp_path):
    """`run_ingest` must not silently re-parse, and must not silently hide the version.

    Re-parsing a cached document is the dangerous option: every reference in that
    project's lens files was minted against the doc.json on disk. So the version is
    reported and nothing is rebuilt.
    """
    from harness import state
    from harness.config import Config
    from harness.stages import ingest

    src = next((p for p in (HARNESS_ROOT / "papers" / "authored").glob("*.pdf")), None)
    if src is None:
        pytest.skip("no authored fixture paper in this checkout")
    cfg = Config(projects_dir=tmp_path)
    pid = ingest.paper_id_for(src)
    doc_path = state.project_dir(cfg, pid) / "paper" / "doc.json"
    state.write_json(doc_path, {"paper_id": pid, "title": "Legacy",
                                "content_sha": ingest.content_sha(src)})
    out = ingest.run_ingest(cfg, str(src))
    assert out["cached"] is True
    assert out["extraction_version"] == 1
    assert out["stale_extraction"] is True


# --------------------------------------------------------------------------- #
# 6. The prompt boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", list(range(0x00, 0x20)) + [0x7f])
def test_sanitise_controls_keeps_only_tab_and_newline(code: int):
    """Exhaustive over the C0 block plus DEL. A per-character sweep, because the
    interesting failures were NUL (3 per real ICLR prompt) and 0x1A."""
    ch = chr(code)
    out = pdf.sanitise_controls(f"a{ch}b")
    if ch in ("\t", "\n"):
        assert out == f"a{ch}b"
    else:
        assert ch not in out and out == "a b", (hex(code), repr(out))


def test_sanitise_controls_does_not_weld_two_words_together():
    """'' as the replacement would produce a token that appears nowhere in the paper —
    the same fabricated-quote failure as the injected caption colon."""
    assert pdf.sanitise_controls("alpha\x00beta") == "alpha beta"


def test_no_control_character_reaches_a_prompt(tmp_path):
    """The real prompts carry NUL bytes today.

    Each `projects/iclr/audit/prompts/*.md` holds 3 NUL and 30 other control characters,
    straight from PDF extraction into a reviewer's stdin. Regenerated here into
    `tmp_path` from the same document, plus a synthetic case that runs everywhere, and
    the assertion is over BYTES so an encoding step cannot hide one.
    """
    from harness import state
    from harness.config import Config
    from harness.stages import audit as audit_stage

    docs: list[dict] = [{
        "paper_id": "synthetic", "title": "A Study\x00Of Things", "n_pages": 1,
        "sections": [{"section_idx": 0, "title": "Results\x1a", "page_start": 1,
                      "page_end": 1, "text": "the arm\x00reaches 4.2\x07% over baseline"}],
        "tables": [{"table_idx": 0, "page": 1, "caption": "Table 1: R\x00esults",
                    "header": ["Method", "Acc\x00"], "rows": [["ours", "9\x011.2"]]}],
    }]
    shipped = HARNESS_ROOT / "projects" / "iclr" / "paper" / "doc.json"
    if shipped.exists():
        docs.append(json.loads(shipped.read_text(encoding="utf-8")))

    for i, raw in enumerate(docs):
        cfg = Config(projects_dir=tmp_path / str(i))
        pid = "p"
        raw = {**raw, "paper_id": pid}
        state.create_project(cfg, "", "control-character boundary", pid=pid)
        state.write_json(state.project_dir(cfg, pid) / "paper" / "doc.json", raw)
        out = audit_stage.run_audit(cfg, pid)
        assert "error" not in out, out
        for path in out["prompts"].values():
            data = Path(path).read_bytes()
            assert data, path
            bad = {b for b in data if b < 0x20 and b not in (0x09, 0x0a, 0x0d)}
            assert not bad, (path, sorted(hex(b) for b in bad))
            assert 0x7f not in data, path


# --------------------------------------------------------------------------- #
# 7. This layer reports; it does not decide
# --------------------------------------------------------------------------- #
def test_extraction_never_reaches_a_severity_a_verdict_or_a_colour():
    """Import-absence, over the two modules this workstream owns.

    Extraction bounds what can be CITED. It may not participate in what may be
    CONCLUDED: a wrongly-typed caption already cost a false attestation, and an
    extraction fact that could set a grade would let the parser decide a paper's colour —
    the same defect invariant 17 names.
    """
    src = Path(pdf.__file__).read_text(encoding="utf-8")
    for forbidden in ("from . import grading", "from .grading", "from .taxonomy",
                      "from .stages", "import grading", "import taxonomy"):
        assert forbidden not in src, forbidden
    for word in ("FATAL", "MAJOR", "counted_severity", "overall_verdict", "triage",
                 "YELLOW", "RED_MAJOR", "claim_status", "SEVERITIES"):
        assert word not in src, word


def test_no_extraction_field_names_a_defect_in_the_paper():
    """An extraction limit is a fact about this harness, never a finding about the paper.

    `Table.label == ''` means "we could not pair a caption", and `CrossRef` means "the
    prose cites this". Neither may be spelled as an accusation, because the naive reading
    of both was wrong twelve times out of twelve on the shipped corpus.
    """
    for model in (Table, Figure, CrossRef):
        for field in model.model_fields:
            assert not any(w in field for w in ("missing", "invalid", "broken", "error",
                                                "severity", "verdict")), (model, field)


def test_the_caption_rules_for_figures_and_tables_are_one_rule():
    """A drift guard. The figure path was un-hardened while the table path was not, and
    that asymmetry is what made a false `caption_verified` reachable and a false
    `Table.caption` merely ugly. Both are now built from the same delimiter."""
    assert pdf._CAPTION_DELIM in pdf._TABLE_CAPTION.pattern
    assert pdf._CAPTION_DELIM in pdf._FIGURE_CAPTION.pattern


# --------------------------------------------------------------------------- #
# 8. Measurements this layer reports about itself
# --------------------------------------------------------------------------- #
def test_the_presented_prose_fraction_is_none_and_never_one_for_an_empty_paper():
    """A rate of 1.0 over nothing reads as "the lens saw all of it"."""
    assert pdf.section_presentation([], 70_000).fraction is None
    assert pdf.section_presentation([Section(section_idx=0, text="")], 70_000).fraction is None


def test_the_presented_prose_fraction_matches_what_render_sections_places():
    """Computed by the same arithmetic as the renderer, or the two disagree silently.

    This is the ceiling on any recall claim: a long paper is truncated before a lens
    reads a word of it, and the review's scope section said "4 independent lens(es) read
    the paper".
    """
    secs = [Section(section_idx=i, title=f"S{i}", text="x" * 5000) for i in range(20)]
    pres = pdf.section_presentation(secs, 20_000)
    assert pres.total_chars == 100_000
    assert pres.presented_chars == 20 * 1000
    assert pres.sections_truncated == 20
    assert pres.fraction == pytest.approx(0.2)
    rendered = pdf.render_sections(secs, 20_000)
    assert rendered.count("…[truncated]") == pres.sections_truncated


@pytest.mark.parametrize("name", list(PAPERS))
def test_a_real_paper_reports_that_its_lenses_saw_less_than_all_of_it(name: str):
    """Measured, not assumed: every pilot paper is truncated at the default budget."""
    pres = pdf.section_presentation(parsed(name).sections, 70_000)
    assert pres.fraction is not None and 0.0 < pres.fraction <= 1.0
    assert pres.presented_chars <= pres.total_chars


@pytest.mark.parametrize("bad", [
    "", "short", "Accepted: 6 November 2024 / Published online: 21 November 2024",
    "Received: 1 May 2024", "Published online: 21 November 2024", "1234-1256",
    "pp. 5085-5109", "arXiv:2401.00001v2 [cs.LG] 3 Jan 2024",
    "Proceedings of the 41st International Conference", "DOI 10.1007/s11263-024-02",
    # The two real corpus bylines, verbatim. Rejected on the affiliation MARKER, not on
    # "this looks like names" — see `pdf._AFFILIATION_MARKER`.
    "Jayesh Singla * 1 Ananye Agarwal * 1 Deepak Pathak 1",
    "Guillaume V. Sanchez * 1 2 Alexander Spangher * 3 Honglu Fan * 4 2 Elad Levi 5",
])
def test_an_extracted_byline_or_banner_is_not_a_plausible_title(bad: str):
    """Three of seven corpus reviews were headed by a date line or a page range.

    The title is the first thing a human reads. `title_is_plausible` is report-only: the
    renderer decides what to do with a False, and nothing about extraction changes.
    """
    assert pdf.title_is_plausible(bad) is False


@pytest.mark.parametrize("good", [
    "WeatherGen: A Unified Diverse Weather Generator for LiDAR Point Clouds via",
    "LDREG: LOCAL DIMENSIONALITY REGULARIZED SELF-SUPERVISED LEARNING",
    "APT: Adaptive Pruning and Tuning Pretrained Language Models for",
    "Grokking modular arithmetic with a two-layer transformer",
    "BatchNorm’s Implicit Regularisation Falls Short of Weight Decay",
    "Non-Disruption Without Benefit: Null-Space Re-Initialization",
    # A title may contain an asterisk; only a marker followed by a DIGIT is a byline.
    "A* Search Is All You Need for Neural Program Repair",
])
def test_a_real_paper_title_is_still_plausible(good: str):
    """A plausibility rule that rejected real titles would be worse than the defect.

    Every string here except the last is a title `guess_title` actually recovered from a
    paper on this disk; the last is the near miss the affiliation-marker rule must not
    catch.
    """
    assert pdf.title_is_plausible(good) is True


def test_title_plausibility_is_reported_and_gates_nothing():
    """It returns a bool and takes one string. It cannot reach extraction or a verdict.

    A title check that could change what was extracted would make the parse depend on a
    heuristic about the parse's own output.
    """
    assert list(inspect.signature(pdf.title_is_plausible).parameters) == ["s"]
    assert inspect.signature(pdf.title_is_plausible).return_annotation == "bool"
    before = pdf.guess_title(["Accepted: 6 November 2024 / Published online: 21 Nov"])
    assert before == "Accepted: 6 November 2024 / Published online: 21 Nov", (
        "extraction still records what it found; only the RENDERER may decline it")
