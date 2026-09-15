"""Paper-owned materiality links: exact structure only, and every ambiguity refuses."""
from __future__ import annotations

import pytest

from harness import claims, discovery, materiality
from harness.artifacts import (CrossRef, Equation, Figure, Finding, PaperDoc,
                               QuantFinding, Section, Table)


def _central_doc(kind: str, *, title: str = "Abstract", duplicate_label: bool = False):
    noun = {"table": "Table", "figure": "Figure", "equation": "Equation"}[kind]
    sentence = f"The headline claim is supported by {noun} 2."
    common = dict(
        paper_id="p",
        sections=[Section(section_idx=0, title=title, text=sentence),
                  Section(section_idx=1, title="Method", text="Supporting detail.")],
        crossrefs=[CrossRef(kind=kind, number="2", section_idx=0, quote=sentence)],
    )
    if kind == "table":
        objects = [Table(table_idx=7, label="2", rows=[["61.4"]])]
        if duplicate_label:
            objects.append(Table(table_idx=8, label="2", rows=[["59.3"]]))
        doc = PaperDoc(**common, tables=objects)
        ref = claims.resolve(doc, "T7:r0:c0")
    elif kind == "figure":
        objects = [Figure(figure_idx=7, label="Figure 2", caption="Figure 2: result")]
        if duplicate_label:
            objects.append(Figure(figure_idx=8, label="Figure 2", caption="Figure 2: other"))
        doc = PaperDoc(**common, figures=objects)
        ref = claims.resolve(doc, "F7")
    else:
        objects = [Equation(equation_idx=7, number="2", text="y = x")]
        if duplicate_label:
            objects.append(Equation(equation_idx=8, number="2", text="z = x"))
        doc = PaperDoc(**common, equations=objects)
        ref = claims.resolve(doc, "E7")
    return doc, ref


@pytest.mark.parametrize(
    ("kind", "basis"),
    [("table", "ABSTRACT_TABLE_REFERENCE"),
     ("figure", "ABSTRACT_FIGURE_REFERENCE"),
     ("equation", "ABSTRACT_EQUATION_REFERENCE")],
)
def test_abstract_exactly_citing_a_unique_recovered_object_grants_the_explicit_basis(
        kind, basis):
    doc, ref = _central_doc(kind)
    assert materiality.basis_for_ref(
        ref, materiality.abstract_section_idx(doc), doc=doc) == basis


@pytest.mark.parametrize(
    ("kind", "basis"),
    [("table", "CONCLUSION_TABLE_REFERENCE"),
     ("figure", "CONCLUSION_FIGURE_REFERENCE"),
     ("equation", "CONCLUSION_EQUATION_REFERENCE")],
)
def test_conclusion_exactly_citing_a_unique_recovered_object_grants_the_explicit_basis(
        kind, basis):
    doc, ref = _central_doc(kind, title="6. Conclusion")
    assert materiality.basis_for_ref(ref, -1, doc=doc) == basis


def test_a_resolved_conclusion_claim_is_material_without_model_judgement():
    text = "The method does not support the claimed improvement."
    doc = PaperDoc(paper_id="p", sections=[
        Section(section_idx=4, title="Conclusions", text=text)])
    ref = claims.mint(doc, text)
    assert materiality.basis_for_ref(ref, -1, doc=doc) == "CONCLUSION_CLAIM"


@pytest.mark.parametrize("title", ["Conclusion", "Conclusions", "6. Conclusion",
                                    "Discussion and Conclusions", "Concluding Remarks"])
def test_conclusion_locator_accepts_only_explicit_conclusion_headings(title):
    doc = PaperDoc(paper_id="p", sections=[Section(section_idx=4, title=title, text="x")])
    assert materiality.conclusion_section_idx(doc) == 4


@pytest.mark.parametrize(("title", "basis"), [
    ("Abstract", "ABSTRACT_RESULT_REFERENCE"),
    ("Conclusion", "CONCLUSION_RESULT_REFERENCE"),
])
def test_a_central_numbered_result_reference_binds_only_to_its_unique_paper_definition(
        title, basis):
    central = "Result 3 supplies the paper's main comparison."
    definition = "Result 3 establishes a 2.1 point improvement."
    doc = PaperDoc(paper_id="p", sections=[
        Section(section_idx=0, title=title, text=central),
        Section(section_idx=1, title="Analysis", text=definition),
        Section(section_idx=2, title="Additional Results", text="Result 4 is secondary."),
    ])
    ref = claims.mint(doc, definition)
    assert materiality.basis_for_ref(
        ref, materiality.abstract_section_idx(doc), doc=doc) == basis


@pytest.mark.parametrize("kind", ["table", "figure", "equation"])
def test_duplicate_printed_labels_make_the_object_identity_ambiguous(kind):
    doc, ref = _central_doc(kind, duplicate_label=True)
    assert materiality.basis_for_ref(
        ref, materiality.abstract_section_idx(doc), doc=doc) == "NONE"


def test_two_result_definitions_with_the_same_number_are_ambiguous():
    definition = "Result 3 establishes the primary comparison."
    doc = PaperDoc(paper_id="p", sections=[
        Section(section_idx=0, title="Abstract", text="Result 3 is our main result."),
        Section(section_idx=1, title="Analysis", text=definition),
        Section(section_idx=2, title="Appendix", text="Result 3 establishes another comparison."),
    ])
    ref = claims.mint(doc, definition)
    assert materiality.basis_for_ref(ref, 0, doc=doc) == "NONE"


def test_an_unrelated_section_mention_cannot_grant_a_central_dependency():
    doc, ref = _central_doc("table")
    doc.sections[0].text = "We report the paper's main claim."
    doc.sections.append(Section(section_idx=2, title="Related Work",
                                text="The comparison appears in Table 2."))
    doc.crossrefs = [CrossRef(kind="table", number="2", section_idx=2,
                              quote="The comparison appears in Table 2.")]
    assert materiality.basis_for_ref(ref, 0, doc=doc) == "NONE"


def test_a_stale_crossref_whose_quote_does_not_name_the_object_fails_closed():
    doc, ref = _central_doc("table")
    doc.sections[0].text = "The headline claim is supported by Table 2. Our method works."
    doc.crossrefs = [CrossRef(kind="table", number="2", section_idx=0,
                              quote="Our method works.")]
    assert materiality.basis_for_ref(ref, 0, doc=doc) == "NONE"


def test_duplicate_central_headings_fail_closed_even_if_the_caller_supplies_an_index():
    text = "The primary result is 61.4."
    doc = PaperDoc(paper_id="p", sections=[
        Section(section_idx=0, title="Abstract", text=text),
        Section(section_idx=1, title="Abstract", text="A second extracted abstract."),
    ])
    ref = claims.mint(doc, text)
    assert materiality.abstract_section_idx(doc) == -1
    assert materiality.basis_for_ref(ref, 0, doc=doc) == "NONE"


def test_discovery_assigns_the_paper_owned_table_reference_basis():
    doc, _ = _central_doc("table")
    doc.reported_numbers = [QuantFinding(value="61.4", metric="accuracy",
                                         source_quote="61.4", table_ref="T7:r0:c0")]
    objects, _ = discovery.discover(doc, [])
    result = next(o for o in objects if o.ref and o.ref.ref == "T7:r0:c0")
    assert result.materiality_basis == "ABSTRACT_TABLE_REFERENCE"


def test_model_prose_and_centrality_cannot_manufacture_a_dependency():
    doc, _ = _central_doc("table")
    doc.sections[0].text = "We report the paper's main claim."
    doc.crossrefs = []
    doc.reported_numbers = [QuantFinding(value="61.4", metric="accuracy",
                                         source_quote="61.4", table_ref="T7:r0:c0")]
    finding = Finding(
        finding_id="model-1", lens="overclaim", candidate_class="CONFIRMED_FINDING",
        evidence_ref="T7:r0:c0", evidence_quote="61.4",
        claim="The model says the abstract depends on Table 2.",
        statement="The model says this is central and material.",
    )
    objects, _ = discovery.discover(doc, [finding])
    matching = [o for o in objects if o.ref and o.ref.ref == "T7:r0:c0"]
    assert any(o.centrality == "CENTRAL" for o in matching)
    assert {o.materiality_basis for o in matching} == {"NONE"}


def test_every_explicit_paper_owned_basis_is_material_but_unknown_prose_is_not():
    assert all(materiality.is_material(b) for b in materiality.MATERIALITY_BASES
               if b != "NONE")
    assert not materiality.is_material("CENTRAL")
    assert not materiality.is_material("the model says material")
