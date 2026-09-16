"""The one correspondence a paper does not print, and the rules that keep it honest.

A paper's abstract says "reduces training memory by 40%". Its Table 3 says `40.2`. Nothing
in the document says those are the same claim, and measured over the eight-paper corpus
nothing deterministic recovers the link: zero cross-references in any Abstract, one in any
Conclusion, and none of the twenty-one numbers headline sentences print occurs in exactly
one recovered cell. Every model-free mechanism fired on almost nothing — `in_abstract` on
0 of 706 objects, `materiality.basis_for_ref` on 1 of 1,729 addresses, the deterministic
claim graph on 0 of 1,729.

So a reader proposes the pairing and the harness checks it, which is what every other
model-supplied fact in this system already does. These tests pin the four properties that
make that safe:

  * the reader supplies two quotations and certifies nothing;
  * a pairing whose halves do not both resolve is refused and counted;
  * a numeric disagreement is refused rather than reported as a contradiction, because
    this channel has no grader, no severity cap, and no way to tell a real inconsistency
    from a reader citing the wrong cell;
  * with the gate closed, NOTHING changes — the graph, the materiality rule and the
    decision are exactly what they were before this channel existed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import claimgraph, claimlink, claimlink_driver, materiality, state
from harness.artifacts import ClaimLink, PaperDoc, Section, Table
from harness.config import Config

ABSTRACT = ("We reduce peak memory by 40%. The method needs no extra supervision and "
            "trains in a single pass.")
BODY = "Peak memory falls to 40.2 on the held-out split, against 67.0 for the baseline."
CONCLUSION = "Memory use is reduced substantially across every benchmark we tried."


def _doc(pid: str = "p") -> PaperDoc:
    return PaperDoc(
        paper_id=pid, title="A Paper", n_pages=3, content_sha="d" * 12,
        sections=[
            Section(section_idx=0, title="", page_start=1, text="A Paper. Some Authors."),
            Section(section_idx=1, title="Abstract", page_start=1, text=ABSTRACT),
            Section(section_idx=2, title="Results", page_start=2, text=BODY),
            Section(section_idx=3, title="Conclusion", page_start=3, text=CONCLUSION),
        ],
        tables=[Table(table_idx=1, page=2, label="1", caption="Table 1 peak memory",
                      header=["method", "mem"], rows=[["ours", "40.2"], ["base", "67.0"]])])


def _link(**kw) -> ClaimLink:
    return ClaimLink(**kw)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path / "projects")


# --------------------------------------------------------------------------- #
# 1. What an accepted link says, and what it does not
# --------------------------------------------------------------------------- #
def test_the_rounding_a_paper_actually_does_is_accepted_and_named():
    """"40%" in the abstract against 40.2 in the cell is the paper rounding its own result.

    Refusing it would refuse the normal case: a paper states its headline number to the
    precision it chooses to state it, and the tolerance is that precision rather than a
    constant, so "40%" admits 40.2 and "40.23%" would not.
    """
    doc = _doc()
    ok = claimlink.verify(doc, _link(claim_quote="We reduce peak memory by 40%.",
                                     evidence_ref="T1:r0:c1", evidence_quote="40.2"))
    assert ok.accepted and ok.numeric_relation == "ROUNDS_TO"
    assert ok.claim_value == 40.0 and ok.evidence_value == 40.2
    assert ok.claim_section_idx == materiality.abstract_section_idx(doc)


def test_an_accepted_link_says_the_paper_depends_on_it_and_nothing_about_truth():
    """The sentence a reader will actually read, and the disclaimer it has to carry.

    A dependency is not a verdict. "The abstract rests on this cell" is exactly as true
    when the cell is wrong as when it is right, and a machine-written observation that
    did not say so would be read as corroboration.
    """
    ok = claimlink.verify(_doc(), _link(claim_quote="We reduce peak memory by 40%.",
                                        evidence_ref="T1:r0:c1", evidence_quote="40.2"))
    assert "inside the Abstract" in ok.verified_observation
    assert "establishes nothing about whether the claim is correct" in ok.verified_observation
    assert "T1:r0:c1" in ok.verified_observation


def test_a_qualitative_headline_claim_locates_evidence_without_asserting_arithmetic():
    """Most conclusions state no number. A pointer is still a dependency."""
    ok = claimlink.verify(_doc(), _link(claim_quote=CONCLUSION, evidence_ref="T1:r0:c1",
                                        evidence_quote="40.2"))
    assert ok.accepted and ok.numeric_relation == "NO_NUMBER_IN_CLAIM"
    assert "asserts no arithmetic" in ok.verified_observation


# --------------------------------------------------------------------------- #
# 2. The refusals, each for its own reason
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("claim,ref,quote,reason", [
    ("We reduce peak memory by 99%.", "T1:r0:c1", "40.2", "claim_unresolved"),
    (BODY, "T1:r0:c1", "40.2", "claim_not_headline"),
    ("We reduce peak memory by 40%.", "T9:r9:c9", "40.2", "evidence_unresolved"),
    ("We reduce peak memory by 40%.", "T1:r0:c1", "67.0", "evidence_unresolved"),
    ("We reduce peak memory by 40%.", "T1:r1:c1", "67.0", "numeric_mismatch"),
])
def test_each_way_a_pairing_fails_is_named_separately(claim, ref, quote, reason):
    """Four different things go wrong and they need four different answers.

    A quotation that is not in the paper, a body sentence dressed as a headline claim, an
    address that names nothing, a quotation that does not match the address it names, and
    two numbers that disagree are not one failure. Counting them together would make "the
    reader proposed twelve and eight held" unactionable.
    """
    out = claimlink.verify(_doc(), _link(claim_quote=claim, evidence_ref=ref,
                                         evidence_quote=quote))
    assert out.accepted is False and out.refusal == reason, out
    assert out.verified_observation, "a refusal a reader cannot act on is not a refusal"


def test_a_numeric_disagreement_is_refused_and_not_turned_into_a_finding():
    """The channel that must not become a second contradiction lens.

    Two numbers that disagree may be a real inconsistency or a reader citing the wrong
    cell, and nothing here can tell them apart. Raising a contradiction belongs to a lens,
    under quotation verification, an evidence ceiling and independent grading; minting one
    here would reach a reader with none of those.
    """
    out = claimlink.verify(_doc(), _link(claim_quote="We reduce peak memory by 40%.",
                                         evidence_ref="T1:r1:c1", evidence_quote="67.0"))
    assert out.refusal == "numeric_mismatch"
    assert "the contradiction lens's job" in out.verified_observation
    for word in ("FATAL", "MAJOR", "MINOR", "finding", "severity"):
        assert word not in out.verified_observation, word
    assert not hasattr(out, "severity")


def test_a_claim_cannot_cite_itself():
    doc = _doc()
    minted_claim = claimlink._resolve_claim(doc, "We reduce peak memory by 40%.")
    out = claimlink.verify(doc, _link(claim_quote="We reduce peak memory by 40%.",
                                      evidence_ref=minted_claim.ref,
                                      evidence_quote="We reduce peak memory by 40%."))
    assert out.refusal == "self_reference"


def test_the_same_pairing_proposed_twice_counts_once():
    result = claimlink.verify_all(_doc(), [
        _link(claim_quote="We reduce peak memory by 40%.", evidence_ref="T1:r0:c1",
              evidence_quote="40.2"),
        _link(claim_quote="We reduce peak memory by 40%.", evidence_ref="T1:r0:c1",
              evidence_quote="40.2"),
    ])
    assert result.accepted == 1 and result.refusals == {"duplicate": 1}
    assert result.proposed == 2, "the duplicate is kept as a record, not discarded"


# --------------------------------------------------------------------------- #
# 3. The reader certifies nothing
# --------------------------------------------------------------------------- #
def test_a_reader_cannot_certify_its_own_pairing():
    """Invariant 2, on the newest channel. Everything the harness writes is the harness's.

    Checked at BOTH boundaries: the driver strips the harness-owned keys before anything
    is verified, and `verify` overwrites them regardless. Either alone would be enough
    today, and a rule enforced in one place is a rule that survives one refactor.
    """
    payload = json.dumps({"links": [{
        "claim_quote": "We reduce peak memory by 40%.", "evidence_ref": "T1:r1:c1",
        "evidence_quote": "67.0", "rationale": "r",
        "accepted": True, "numeric_relation": "EQUAL", "claim_ref": "P1:0-1",
        "verified_observation": "THE HARNESS CONFIRMED THIS"}]})
    proposals, _notes, meta = claimlink_driver.parse_links(payload)
    assert meta["harness_keys_stripped"] == 4, meta
    assert proposals[0].accepted is False and proposals[0].verified_observation == ""
    assert proposals[0].claim_ref == ""

    checked = claimlink.verify(_doc(), proposals[0])
    assert checked.accepted is False and checked.refusal == "numeric_mismatch"
    assert "THE HARNESS CONFIRMED THIS" not in checked.verified_observation


def test_the_reader_is_never_shown_a_finding_a_severity_or_another_readers_output():
    """The prompt's parameter list is the guarantee, not a sentence inside the prompt."""
    import inspect

    from harness.prompts import claimlink as prompt_mod
    params = set(inspect.signature(prompt_mod.build).parameters)
    assert params == {"title", "abstract_text", "conclusion_text", "tables_text",
                      "figures_text", "equations_text", "numbers_text", "pdf_path"}
    body = prompt_mod.build("T", ABSTRACT, CONCLUSION, "T1:r0:c1 = 40.2")
    assert "DO NOT USE THIS TO REPORT PROBLEMS" in body
    assert "Omit a claim rather than guess" in body


# --------------------------------------------------------------------------- #
# 4. With the gate closed, nothing changes
# --------------------------------------------------------------------------- #
def test_the_gate_is_closed_by_default_and_says_what_that_means():
    ok, why = claimlink_driver.available(Config())
    assert ok is False
    assert "SH_ALLOW_CLAIM_LINKS" in why
    assert "exactly as it did before this channel existed" in why


def test_no_links_reproduces_the_graph_that_existed_before_this_channel():
    """The property that makes an optional model channel safe to put on the always-on path.

    A channel that could only be assessed by running it is a channel nobody can turn off
    to see what it did. `build(doc)` and `build(doc, [])` must be the same graph, and a
    REFUSED link must leave it untouched — otherwise a proposal the harness rejected would
    still be moving the answer.
    """
    doc = _doc()
    base = claimgraph.build(doc)
    assert claimgraph.build(doc, None).counts() == base.counts()
    assert claimgraph.build(doc, []).counts() == base.counts()
    assert all(e.kind != "SUPPORTED_BY" for e in base.edges)

    refused = _link(link_id="L1", claim_quote="q", claim_ref="P1:0-31",
                    claim_section_idx=1, evidence_ref="T1:r0:c1", accepted=False,
                    refusal="numeric_mismatch")
    assert claimgraph.build(doc, [refused]).counts() == base.counts()


def test_a_verified_link_is_the_edge_the_paper_does_not_print():
    """And the dependency it creates is a path a referee can open, hop by hop."""
    doc = _doc()
    verified = claimlink.verify(doc, _link(claim_quote="We reduce peak memory by 40%.",
                                           evidence_ref="T1:r0:c1", evidence_quote="40.2"))
    assert verified.accepted
    before = claimgraph.build(doc)
    after = claimgraph.build(doc, [verified])
    assert claimgraph.graph_centrality(before, "T1:r0:c1") != "CENTRAL", (
        "the deterministic graph must not already establish this, or the test proves nothing")
    assert claimgraph.graph_centrality(after, "T1:r0:c1") == "CENTRAL"

    path = claimgraph.dependency(after, "T1:r0:c1")
    assert path is not None and path.edges[-1].kind == "SUPPORTED_BY"
    assert "the harness verified both halves" in path.explain(after)


# --------------------------------------------------------------------------- #
# 5. Sealing, reuse and the prompt it was answered against
# --------------------------------------------------------------------------- #
def _plant(cfg: Config, doc: PaperDoc) -> str:
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    if not (cfg.projects_dir / doc.paper_id / "project.json").exists():
        state.create_project(cfg, "", doc.title, pid=doc.paper_id)
    state.write_json(cfg.projects_dir / doc.paper_id / "paper" / "doc.json", doc.model_dump())
    return doc.paper_id


def test_an_accepted_reading_is_sealed_and_an_edited_one_is_refused(cfg: Config):
    doc = _doc()
    pid = _plant(cfg, doc)
    payload = json.dumps({"links": [
        {"claim_quote": "We reduce peak memory by 40%.", "evidence_ref": "T1:r0:c1",
         "evidence_quote": "40.2", "rationale": "the headline memory number"},
        {"claim_quote": BODY, "evidence_ref": "T1:r0:c1", "evidence_quote": "40.2",
         "rationale": "a body sentence"}], "notes": "one pairing could not be made"})
    sealed = claimlink_driver.accept(cfg, pid, doc, payload, reader="a test")
    assert sealed.proposed == 2 and sealed.accepted == 1
    assert sealed.refusals == {"claim_not_headline": 1}
    assert sealed.notes.startswith("one pairing")

    back = claimlink_driver.load(cfg, pid)
    assert back is not None and back.accepted == 1
    # the refused pairing is on disk too: a reader that proposed two of which one did not
    # resolve is a different reader from one that proposed one
    assert len(back.links) == 2 and any(x.refusal for x in back.links)

    out = cfg.projects_dir / pid / "links" / f"{pid}.claimlinks.json"
    out.write_text(out.read_text(encoding="utf-8").replace("ROUNDS_TO", "EQUAL"),
                   encoding="utf-8")
    assert claimlink_driver.load(cfg, pid) is None, "an edited artifact is not a sealed one"


def test_a_link_set_minted_against_a_different_prompt_is_not_reused(cfg: Config):
    """A re-parse moves addresses, and a link pointing at a span that moved is wrong.

    `extraction_version` states this rule for stored references and does not enforce it.
    Here it is enforced, because this artifact is new and has no archive to invalidate by
    enforcing it.
    """
    doc = _doc()
    pid = _plant(cfg, doc)
    claimlink_driver.accept(cfg, pid, doc, json.dumps({"links": [
        {"claim_quote": "We reduce peak memory by 40%.", "evidence_ref": "T1:r0:c1",
         "evidence_quote": "40.2", "rationale": "r"}]}), reader="a test")
    sidecar = cfg.projects_dir / pid / "links" / f"{pid}.claimlinks.driver.json"
    record = state.read_json(sidecar)
    record["prompt_sha256"] = "a" * 64
    state.write_json(sidecar, record)
    assert claimlink_driver.load(cfg, pid, prompt_sha256="b" * 64) is None
    assert claimlink_driver.load(cfg, pid, prompt_sha256="a" * 64) is not None
    assert claimlink_driver.load(cfg, pid) is not None, "no hash asked for, no hash checked"


def test_the_prompt_is_written_every_run_so_a_host_with_no_reader_can_still_answer(cfg: Config):
    """The manual channel. `accept_lens`, `accept_grade` and `accept_verdict` all have one."""
    from harness.stages import audit as audit_stage

    doc = _doc()
    pid = _plant(cfg, doc)
    res = audit_stage.run_audit(cfg, pid)
    prompt = Path(res["claimlink_prompt"])
    assert prompt.is_file() and prompt.name == "prompt.md"
    body = prompt.read_text(encoding="utf-8")
    assert ABSTRACT in body and CONCLUSION in body
    assert "T1:r0:c1" in body
    # the BODY is deliberately withheld: a reader shown it pairs a headline claim with the
    # sentence that restates it, which is a paraphrase and not evidence
    assert BODY not in body


def test_a_closed_gate_leaves_the_audit_phase_and_its_artifacts_untouched(cfg: Config):
    """Belt and braces on the property the whole design rests on."""
    from harness.controller import _claim_links
    from harness.artifacts import CaseState

    doc = _doc()
    pid = _plant(cfg, doc)
    case = CaseState(paper_id=pid, phase="audit")
    prompt = cfg.projects_dir / pid / "links" / "prompt.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("anything", encoding="utf-8")
    assert cfg.allow_claim_links is False
    _claim_links(cfg, case, str(prompt), "")
    assert claimlink_driver.load(cfg, pid) is None
    assert not (cfg.projects_dir / pid / "links" / f"{pid}.claimlinks.json").exists()


# --------------------------------------------------------------------------- #
# 6. The hyphen a line break put there
# --------------------------------------------------------------------------- #
# Found by running the real reader against `cvpr` and reading why it was refused. The
# reader quoted the abstract the way any human reads it and the harness rejected 8 of its
# 11 sentences as not present in the paper. They were present: the PDF holds
# `gener-\nationframework` where the reader wrote `generationframework`.
#
# The same defect was costing the EVIDENCE GATE findings, measured over the evaluated
# corpus: 4 of the 5 findings it dropped were correctly-quoted sentences refused for a
# hyphen the typesetter inserted — 80% of every drop in the corpus.
_BROKEN = ("This paper presents WeatherGen, the first unified diverse-weather LiDAR data "
           "diffusion gener-\nation framework, significantly improving fidelity.")


def _hyphenated_doc() -> PaperDoc:
    return PaperDoc(
        paper_id="h", title="A Paper", n_pages=2,
        sections=[Section(section_idx=0, title="Abstract", page_start=1, text=_BROKEN),
                  Section(section_idx=1, title="Results", page_start=2,
                          text="Fidelity improves.")],
        tables=[Table(table_idx=1, page=2, label="1", rows=[["ours", "40.2"]])])


def test_a_quotation_refused_only_for_a_line_break_hyphen_now_resolves():
    """The exact shape the real reader produced, and the exact address it resolves to."""
    from harness import claims

    doc = _hyphenated_doc()
    as_read = ("This paper presents WeatherGen, the first unified diverse-weather LiDAR "
               "data diffusion generation framework, significantly improving fidelity.")
    minted = claims.mint(doc, as_read)
    assert minted.resolved and minted.kind == "prose_claim", minted
    assert minted.section_idx == 0
    # the quote handed back is the PAPER's own text, hyphen and all — a reader following
    # the address must see what the document says, not a normalisation of it
    assert "gener-" in minted.quote


def test_a_real_compound_hyphen_is_not_removed():
    """`diverse-weather` is a word the authors wrote; `gener-ation` is a line break.

    The difference is decidable, and only from the original text: a line-break hyphen is
    the one immediately followed by whitespace. If the projection removed both, a quote
    saying `diverseweather` would match and the harness would be accepting text the paper
    does not contain — trading one false refusal for one false attestation.
    """
    from harness import claims

    doc = _hyphenated_doc()
    flat, offsets = claims.flatten(_BROKEN)
    projected, index = claims.soft_hyphen_projection(flat, offsets, _BROKEN)
    assert "diverse-weather" in projected, "a compound hyphen must survive"
    assert "generationframework" in projected, "a line-break hyphen must not"
    assert len(index) == len(projected)
    assert claims.mint(doc, "the first unified diverseweather LiDAR").resolved is False


def test_an_exact_match_still_says_verbatim_and_a_recovered_one_does_not():
    """The observation is a machine attestation, so it may not overstate what was checked."""
    from harness.stages import audit as audit_stage

    doc = _hyphenated_doc()
    units = audit_stage.source_units(doc)
    exact = audit_stage.verify_evidence("Fidelity improves.", "p2", units, {})
    assert exact[0] == "prose_verified" and "occurs verbatim inside" in exact[1]

    recovered = audit_stage.verify_evidence(
        "diffusion generation framework, significantly improving fidelity", "p1", units, {})
    assert recovered[0] == "prose_verified", recovered
    assert "occurs verbatim" not in recovered[1], recovered[1]
    assert "NOT a character-for-character match" in recovered[1]
    assert "hyphens a line break inserted" in recovered[1]


def test_the_exact_search_still_runs_first():
    """A quotation that matches the paper character for character is never resolved
    through a normalisation, so the common path is unchanged and so is its attestation."""
    from harness.stages import audit as audit_stage

    doc = _doc()
    units = audit_stage.source_units(doc)
    klass, observation = audit_stage.verify_evidence(
        "We reduce peak memory by 40%", "p1", units, {})
    assert klass == "prose_verified" and "occurs verbatim inside" in observation
