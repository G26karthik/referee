"""Findings, turned into questions a route can be chosen for.

`python -m harness.questions` runs the self-check.

A finding says what is wrong. A question says what would settle it, and the gap between
those two is the difference between a critique and a review. "The paper changes the
augmentation policy and the loss weighting together" is an observation; "is the reported
gain attributable to the augmentation or to the simultaneous loss-weight change?" is a
question that an experiment can discriminate and that a reader can act on.

**Why this is a table and not a model call.** The question a finding raises is determined
by the finding's own typed self-classification — `discrepancy_type`, `baseline_class`,
`candidate_class`, `lens` — every one of which is already a closed vocabulary the harness
clamps. Templating from those is deterministic, paper-agnostic, and inexpressible as a
paper-specific rule, which is what `tests/test_reasoning_architecture.py` requires of
every derivation in this system. Nothing here reads a number, a metric name, a count, or
a paper identity, so "if <paper> appears, soften" cannot be written even by accident.

The lens's own prose still reaches the reader — `what_would_settle_it` prefers the lens's
`recommended_resolution`, and the counter-explanations it wrote are carried verbatim. What
the lens does NOT get to decide is `route`, because whether a route exists is a structural
fact about the paper and the artifact, not an opinion about the finding.
"""
from __future__ import annotations

from .artifacts import QUESTION_KINDS, DiscoveredObject, Finding, ReviewQuestion

# Keyed on the lens's own classification of its own finding, never on content. Each entry
# is (question template, why-it-matters template). The templates take no arguments: a
# template that interpolated the finding's text would be re-stating model prose as if the
# harness had derived it.
_BY_DISCREPANCY = {
    "ARITHMETIC_ERROR": (
        "Do the paper's own printed numbers actually compose to the total it reports?",
        "An arithmetic slip in a headline quantity changes what every downstream number "
        "is a fraction of.",
    ),
    "GENUINE_CONTRADICTION": (
        "Which of the paper's two conflicting statements is the one the conclusion rests on?",
        "A conclusion drawn from the more favourable of two incompatible numbers is not "
        "supported by either.",
    ),
    "DIFFERENT_DENOMINATOR": (
        "Are the two quantities being compared normalised over the same population?",
        "A ratio compared against a ratio with a different denominator is not a comparison.",
    ),
    "DEFINITIONAL_MISMATCH": (
        "Is the quantity the paper evaluates the quantity its claim is about?",
        "Evaluating a proxy and reporting the claim in terms of the target quantity "
        "overstates what was measured.",
    ),
    "UNCLEAR_REPORTING": (
        "What exactly was measured, and over what?",
        "A number whose definition cannot be recovered from the paper cannot be checked "
        "by anyone, including a future reader of the authors' own work.",
    ),
}

_BY_BASELINE = {
    "CENTRAL_VALIDITY_THREAT": (
        "Does the proposed method still outperform the strongest appropriate baseline?",
        "The comparison the paper omits is the one its central claim is about.",
    ),
    "IMPORTANT_MISSING_BASELINE": (
        "Does the reported advantage survive comparison against the missing baseline?",
        "An advantage measured only against weaker alternatives may be an artefact of the "
        "comparison set.",
    ),
    "USEFUL_CONTROL": (
        "Would the missing control change how the reported gain is attributed?",
        "Without the control, a simpler explanation for the gain remains open.",
    ),
}

_BY_LENS = {
    "confound": (
        "Is the reported gain attributable to the mechanism the paper credits, or to a "
        "change made at the same time?",
        "Several things changed together, so the attribution the paper makes is one of "
        "several readings the evidence permits.",
    ),
    "protocol": (
        "Is the evaluation protocol appropriate for the claim being drawn from it?",
        "A protocol that does not isolate the claimed effect cannot establish it, however "
        "large the reported number.",
    ),
    "overclaim": (
        "Is the stated conclusion stronger than the reported evidence supports?",
        "The gap between what was measured and what is asserted is what a reader inherits "
        "when they cite the paper.",
    ),
    "contradiction": (
        "Do the paper's own reported quantities agree with the claim drawn from them?",
        "An internal disagreement means at least one of the two statements is wrong, and "
        "the paper does not say which.",
    ),
}

_DEFAULT = (
    "What evidence would settle this concern one way or the other?",
    "An unresolved concern about a central claim is what a first-round review exists to "
    "surface for a human reviewer.",
)

# --------------------------------------------------------------------------------------
# WHAT KIND of question, from the same closed vocabulary the templates key on
# --------------------------------------------------------------------------------------
# Three tables, consulted most-specific-first, exactly as `_template` above and
# `taxonomy.classify` are. The kind decides which ROUTES are admissible
# (`discovery.ROUTES_FOR_QUESTION`), which is the correction this vocabulary exists for:
# routing used to key on whether a printed quantity had been parsed, so an attribution
# question about a paper that printed no number at that address could reach no route at
# all and was reported as though no method applied.
#
# Nothing here reads a number, a metric name, a count or a paper identity — the same
# discipline `tests/test_reasoning_architecture.py` asserts of every derivation in this
# system.
_KIND_BY_DISCREPANCY = {
    # The paper's own operands and total. Checkable by arithmetic and re-derivable by
    # producing the things and counting them, which is why it is its own kind.
    "ARITHMETIC_ERROR": "COMPOSITION",
    # Two printed statements that cannot both hold. What settles it is which number the
    # code produces, so it is a question about a printed quantity.
    "GENUINE_CONTRADICTION": "PRINTED_QUANTITY",
    # The next three are all about what a number MEANS rather than what it is. Re-deriving
    # the same number settles none of them: a ratio over the wrong population is still the
    # same ratio, and a proxy evaluated perfectly is still a proxy.
    "DIFFERENT_DENOMINATOR": "SPECIFICATION",
    "DEFINITIONAL_MISMATCH": "SPECIFICATION",
    "UNCLEAR_REPORTING": "SPECIFICATION",
}

# Every `baseline_class` the lens treats as load-bearing asks the same structural
# question: is the comparison this claim needs actually present? That is an existence
# question and not a quantity one, and it is precisely the shape that could not be routed.
_KIND_BY_BASELINE = {
    "CENTRAL_VALIDITY_THREAT": "CONTROL_PRESENCE",
    "IMPORTANT_MISSING_BASELINE": "CONTROL_PRESENCE",
    "USEFUL_CONTROL": "CONTROL_PRESENCE",
}

_KIND_BY_LENS = {
    "confound": "ATTRIBUTION",
    "protocol": "PROTOCOL_CONFORMANCE",
    # An overclaim and a contradiction are both, in the end, disputes about whether the
    # number supports the sentence. What settles them is the number.
    "overclaim": "PRINTED_QUANTITY",
    "contradiction": "PRINTED_QUANTITY",
}

# The kind for an object the EXTRACTOR found rather than a lens. `stages/discover.build`
# mints a question for each of these so that every executable target carries a
# `question_id`; without it, the two object sources that actually reach execution in this
# corpus — printed table results and prose compositions — were the ones with no question
# at all, and a run could not say what it was spending compute on.
_KIND_BY_DISCOVERY_KIND = {
    "EXPERIMENTAL_RESULT": "PRINTED_QUANTITY",
    "DATASET_RESULT": "PRINTED_QUANTITY",
    "REPRODUCTION_TARGET": "COMPOSITION",
    "BASELINE_COMPARISON": "CONTROL_PRESENCE",
    "ABLATION": "ATTRIBUTION",
    "CONTROL": "CONTROL_PRESENCE",
    # "does the released repository implement the described method" is a conformance
    # question between the paper and the artifact. Its routing is decided before the kind
    # is consulted (`discovery._routes` handles IMPLEMENTATION_CLAIM first), so this is a
    # label for the record rather than a routing decision.
    "IMPLEMENTATION_CLAIM": "PROTOCOL_CONFORMANCE",
    "ERROR_ANALYSIS": "SPECIFICATION",
    "SCIENTIFIC_CLAIM": "UNCLASSIFIED",
    "UNANSWERED_REVIEW_QUESTION": "UNCLASSIFIED",
}

# What a question about an extractor-found object actually asks, per kind. Templates as
# above: no interpolation, so nothing a model wrote is restated as a harness derivation.
_TEMPLATE_BY_DISCOVERY_KIND = {
    "PRINTED_QUANTITY": (
        "Does the released implementation produce the value the paper prints here?",
        "A reported result nobody can re-derive is a result a reader has to take on trust.",
    ),
    "COMPOSITION": (
        "Do the paper's own printed numbers actually compose to the total it reports?",
        "An arithmetic slip in a headline quantity changes what every downstream number "
        "is a fraction of.",
    ),
    "CONTROL_PRESENCE": (
        "Is the comparison this result is measured against actually present?",
        "A reported advantage is an advantage over something, and which something decides "
        "what the number means.",
    ),
    "ATTRIBUTION": (
        "Is the reported difference attributable to the component the paper credits?",
        "Several things can differ between two reported conditions, and the paper's "
        "attribution is one of the readings the evidence permits.",
    ),
    "PROTOCOL_CONFORMANCE": (
        "Does the released artifact implement the procedure the paper describes?",
        "A published repository is a claim about the method, and a reader who runs it is "
        "relying on the two agreeing.",
    ),
    "SPECIFICATION": (
        "What exactly was measured here, and over what?",
        "A number whose definition cannot be recovered from the paper cannot be checked "
        "by anyone.",
    ),
    "UNCLASSIFIED": _DEFAULT,
}


def kind_for_finding(f: Finding) -> str:
    """What KIND of question this finding raises, from its own self-classification.

    Most-specific-first, and identical in ordering to `_template` and to
    `taxonomy.classify`: a finding that declared its own discrepancy type has said more
    about itself than one carrying only a lens name. A finding that declared neither and
    whose lens is not one of the four falls to UNCLASSIFIED rather than to a guess —
    UNCLASSIFIED routes exactly as this system routed everything before the vocabulary
    existed, so the default is the old behaviour and never a new capability.
    """
    d = (f.discrepancy_type or "").strip().upper()
    if d in _KIND_BY_DISCREPANCY:
        return _KIND_BY_DISCREPANCY[d]
    b = (f.baseline_class or "").strip().upper()
    if b in _KIND_BY_BASELINE:
        return _KIND_BY_BASELINE[b]
    return _KIND_BY_LENS.get((f.lens or "").strip().lower(), "UNCLASSIFIED")


def kind_for_discovery_kind(discovery_kind: str = "") -> str:
    """The question kind for an object the extractor found, from its DISCOVERY_KIND."""
    return _KIND_BY_DISCOVERY_KIND.get((discovery_kind or "").strip().upper(), "UNCLASSIFIED")


def for_object(obj: DiscoveredObject) -> ReviewQuestion:
    """A question for an object no finding raised, so every target has one.

    The two object sources that actually reach execution on this corpus — a printed table
    result and a prose composition — come from the extractor, not from a lens, so they
    carried `question_id=""` and the funnel could not say which question an execution was
    answering. Minting one here is not inventing a concern: it states, in the harness's own
    words and from the object's own DISCOVERY_KIND, what checking this object would settle.

    Materiality mirrors the object's CENTRALITY, which `harness.discovery._centrality`
    derived from the paper's structure. Nothing a model wrote reaches it.
    """
    kind = obj.question_kind or kind_for_discovery_kind(obj.kind)
    question, why = _TEMPLATE_BY_DISCOVERY_KIND.get(kind, _DEFAULT)
    return ReviewQuestion(
        question_id=f"Q-object-{obj.target_id}",
        question=question, kind=kind, from_finding="", source_finding_ids=[], lens="",
        why_it_matters=why,
        what_would_settle_it=("evidence that the quantity at this address is what the "
                              "paper's own artifact produces"),
        materiality=obj.centrality if obj.centrality in _MATERIALITY.values() else "UNASSESSED",
    )

# A question is only worth generating for a candidate that survived its own author's
# scrutiny. DISMISSED is the lens talking itself out of something and is reported as such;
# turning it into a question would put work back on a reviewer that the lens already did.
_QUESTIONABLE = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "")

# Materiality mirrors the finding's own self-classification, and CANNOT exceed it. Same
# asymmetry as `harness.grading`: a mechanism that could raise how much something matters
# would be a mechanism for manufacturing importance.
_MATERIALITY = {
    "CONFIRMED_FINDING": "CENTRAL",
    "PLAUSIBLE_CONCERN": "SUPPORTING",
    "OPEN_QUESTION": "PERIPHERAL",
    "DISMISSED": "PERIPHERAL",
    "": "UNASSESSED",
}


def _template(f: Finding) -> tuple[str, str]:
    """The (question, why) for one finding, from its own closed-vocabulary fields.

    Order matters and is from most specific to least: a finding that classified its own
    discrepancy has said more about itself than one that only has a lens name.
    """
    d = (f.discrepancy_type or "").strip().upper()
    if d in _BY_DISCREPANCY:
        return _BY_DISCREPANCY[d]
    b = (f.baseline_class or "").strip().upper()
    if b in _BY_BASELINE:
        return _BY_BASELINE[b]
    return _BY_LENS.get((f.lens or "").strip().lower(), _DEFAULT)


# Materiality, ranked, so a merge can take the maximum of what its sources asserted
# without any of them being able to raise it above what it already said.
_MATERIALITY_RANK = {"UNASSESSED": 0, "PERIPHERAL": 1, "SUPPORTING": 2, "CENTRAL": 3}


def derive(findings: list[Finding], *,
           minted: dict[str, str] | None = None) -> list[ReviewQuestion]:
    """One question per questionable finding, deterministically — MERGED by identity.

    `route` is deliberately left at its default NONE. Choosing a route requires knowing
    what the artifact contains and what this host can run, neither of which is a property
    of the finding — `harness.planner` decides it, from structural facts.

    **Two lenses reaching the same question about the same address is one question, and
    the agreement is evidence.** Four independent readings that all ask "is the gain
    attributable to the mechanism the paper credits?" about cell T2:r3:c1 do not give a
    reviewer four things to do; they give one, with more behind it. The merge key is
    (question text, the address the lens cited), both of which are already closed or
    verbatim — no similarity threshold, no model call, and nothing merges across
    addresses, because the same question about two different numbers really is two
    questions.

    `source_finding_ids` keeps every contributing finding, `from_finding` keeps the first
    so existing references stay valid, and `materiality` takes the MAXIMUM its sources
    asserted — never more. That ceiling is the same asymmetry as `harness.grading`: a
    merge that could raise materiality above what any single lens claimed would be a
    mechanism for manufacturing importance out of agreement alone.

    **`minted` is what makes "the same address" mean something.** It maps finding_id to
    the address `harness.claims` MINTED for that finding, and the merge key uses it
    instead of the lens's own `evidence_ref`. The lens's string is not an address: the
    prompt instructs a lens to write `p7` for a prose claim, `p7` names a page, and a page
    holds many sentences. Measured over the shipped corpus: 75 of 98 findings write a
    `p<N>` ref, 21 (paper, ref) pairs are shared by more than one finding, and 3 of the 6
    resulting merges are FALSE — different spans that happen to sit on one page. Two of
    those three raised the merged question's materiality, because the maximum rule above
    is applied across sources, so a coincidence of page numbers promoted a SUPPORTING
    concern to CENTRAL.

    Without `minted` the behaviour is unchanged, which keeps every existing caller
    working; `stages/discover.build` supplies it, because that is where the parsed
    document is already in hand.
    """
    out: list[ReviewQuestion] = []
    by_key: dict[tuple[str, str], ReviewQuestion] = {}
    addresses = dict(minted or {})
    for f in findings:
        if (f.candidate_class or "") not in _QUESTIONABLE:
            continue
        question, why = _template(f)
        # THE MERGE KEY IS THE MINTED ADDRESS, not the lens's own string. See the
        # `minted` parameter's docstring: a lens is instructed to write `p7` for prose,
        # `p7` is a page, and two findings about different sentences on page 7 were
        # merging into one question. Measured over the shipped corpus: 75 of 98 findings
        # write a `p<N>` ref, and 3 of the 6 merges that resulted are false. Two of those
        # three RAISED the merged question's materiality, because `materiality` takes the
        # maximum across sources — so a coincidence of page numbers promoted a SUPPORTING
        # concern to CENTRAL.
        #
        # A finding whose address did not resolve merges with NOTHING: its key is its own
        # finding id. Merging on an address nobody could re-derive is merging on a guess,
        # and the conservative failure (two questions where one would do) costs a reader
        # a duplicate line, while the other costs them a manufactured materiality.
        addr = addresses.get(f.finding_id, "")
        key = (question, addr if addr else f"\x00unmerged:{f.finding_id}")
        seen = by_key.get(key)
        if seen is not None:
            seen.source_finding_ids.append(f.finding_id)
            mine = _MATERIALITY.get(f.candidate_class or "", "UNASSESSED")
            if _MATERIALITY_RANK[mine] > _MATERIALITY_RANK[seen.materiality]:
                seen.materiality = mine
            if not seen.what_would_settle_it:
                seen.what_would_settle_it = (f.recommended_resolution or "").strip()
            continue
        settle = (f.recommended_resolution or "").strip()
        if not settle and f.counter_explanations:
            settle = ("Rule out the alternative explanations the review recorded: "
                      + "; ".join(c.strip() for c in f.counter_explanations[:3] if c.strip()))
        q = ReviewQuestion(
            question_id=f"Q-{f.lens or 'lens'}-{f.finding_id or len(out) + 1}",
            question=question,
            kind=kind_for_finding(f),
            from_finding=f.finding_id,
            source_finding_ids=[f.finding_id],
            lens=f.lens,
            why_it_matters=why,
            what_would_settle_it=settle,
            materiality=_MATERIALITY.get(f.candidate_class or "", "UNASSESSED"),
        )
        by_key[key] = q
        out.append(q)
    return out


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    fs = [
        Finding(finding_id="c-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                recommended_resolution="Run the method with one modification at a time."),
        Finding(finding_id="x-01", lens="contradiction", candidate_class="PLAUSIBLE_CONCERN",
                discrepancy_type="ARITHMETIC_ERROR"),
        Finding(finding_id="o-01", lens="overclaim", candidate_class="DISMISSED"),
        Finding(finding_id="p-01", lens="protocol", candidate_class="OPEN_QUESTION",
                baseline_class="CENTRAL_VALIDITY_THREAT",
                counter_explanations=["the gain could come from the longer schedule"]),
    ]
    qs = derive(fs)
    assert [q.from_finding for q in qs] == ["c-01", "x-01", "p-01"], "DISMISSED asks nothing"
    assert [q.source_finding_ids for q in qs] == [["c-01"], ["x-01"], ["p-01"]]
    assert qs[0].materiality == "CENTRAL" and qs[2].materiality == "PERIPHERAL"
    assert "attributable" in qs[0].question
    assert "compose" in qs[1].question, "discrepancy_type beats lens"
    assert "strongest appropriate baseline" in qs[2].question, "baseline_class beats lens"
    assert qs[2].what_would_settle_it.startswith("Rule out")
    assert all(q.route == "NONE" for q in qs), "questions do not choose their own route"

    # --- the same question at the same address, from two lenses, is ONE question ------
    agree = [
        Finding(finding_id="a-01", lens="confound", candidate_class="OPEN_QUESTION",
                evidence_ref="T2:r3:c1"),
        Finding(finding_id="b-01", lens="confound", candidate_class="CONFIRMED_FINDING",
                evidence_ref="T2:r3:c1", recommended_resolution="Ablate one change at a time."),
        Finding(finding_id="c-02", lens="confound", candidate_class="CONFIRMED_FINDING",
                evidence_ref="T9:r0:c0"),
    ]
    # The addresses are the ones the HARNESS minted, supplied by `stages/discover.build`.
    # Passing the lens's own `evidence_ref` here would test the defect rather than the
    # fix: `p7` is a page, not an address, and two findings about different sentences on
    # page 7 must not become one question.
    minted = {"a-01": "T2:r3:c1", "b-01": "T2:r3:c1", "c-02": "T9:r0:c0"}
    m = derive(agree, minted=minted)
    assert len(m) == 2, "same question + same address merges; a different address does not"
    assert m[0].source_finding_ids == ["a-01", "b-01"]
    assert m[0].from_finding == "a-01", "the first source stays addressable"
    assert m[0].materiality == "CENTRAL", "the merge takes the maximum its sources asserted"
    assert m[0].what_would_settle_it == "Ablate one change at a time.", (
        "a later source may fill in what an earlier one left blank")
    assert m[1].source_finding_ids == ["c-02"]
    # and it can only ever take the maximum, never exceed it
    for _ in range(4):
        assert derive(agree, minted=minted)[0].materiality == "CENTRAL"

    # --- A LENS'S OWN REF NEVER MERGES ANYTHING ---------------------------------------
    # `p7` is a page. Two findings about different sentences on page 7 were becoming one
    # question, and the merge takes the MAXIMUM materiality of its sources, so the
    # coincidence promoted a SUPPORTING concern to CENTRAL. Measured on the shipped
    # corpus: 75 of 98 findings write a `p<N>` ref and 3 of the 6 resulting merges were
    # false, two of them raising materiality.
    page_shaped = [
        Finding(finding_id="d-01", lens="confound", candidate_class="OPEN_QUESTION",
                evidence_ref="p7"),
        Finding(finding_id="d-02", lens="confound", candidate_class="CONFIRMED_FINDING",
                evidence_ref="p7"),
    ]
    unmerged = derive(page_shaped)          # no `minted`: nothing resolved, nothing merges
    assert len(unmerged) == 2, "a page number is not an address and must merge nothing"
    assert [q.materiality for q in unmerged] == ["PERIPHERAL", "CENTRAL"], (
        "and neither of them may inherit the other's materiality — this is the promotion "
        "the false merges were producing")
    # supplying real, DIFFERENT minted addresses for the same page keeps them apart
    apart = derive(page_shaped, minted={"d-01": "P7:10-90", "d-02": "P7:400-480"})
    assert len(apart) == 2
    # and supplying the SAME minted address merges them, which is the case that should
    assert len(derive(page_shaped, minted={"d-01": "P7:10-90", "d-02": "P7:10-90"})) == 1

    # every template is reachable and none of them interpolates anything
    for table in (_BY_DISCREPANCY, _BY_BASELINE, _BY_LENS, _TEMPLATE_BY_DISCOVERY_KIND):
        for q, why in table.values():
            assert "{" not in q and "{" not in why

    # --- THE QUESTION KIND -------------------------------------------------------------
    # Closed vocabulary, derived by the same most-specific-first walk as the templates.
    for table in (_KIND_BY_DISCREPANCY, _KIND_BY_BASELINE, _KIND_BY_LENS,
                  _KIND_BY_DISCOVERY_KIND):
        assert set(table.values()) <= set(QUESTION_KINDS), table
    assert set(_TEMPLATE_BY_DISCOVERY_KIND) <= set(QUESTION_KINDS)
    assert set(_KIND_BY_DISCOVERY_KIND.values()) <= set(_TEMPLATE_BY_DISCOVERY_KIND), (
        "every kind an extractor-found object can take needs a question to ask")

    assert kind_for_finding(Finding(lens="protocol",
                                    discrepancy_type="ARITHMETIC_ERROR")) == "COMPOSITION", (
        "discrepancy_type beats the lens, exactly as the template does")
    assert kind_for_finding(Finding(lens="protocol",
                                    baseline_class="USEFUL_CONTROL")) == "CONTROL_PRESENCE"
    assert kind_for_finding(Finding(lens="confound")) == "ATTRIBUTION"
    assert kind_for_finding(Finding(lens="protocol")) == "PROTOCOL_CONFORMANCE"
    assert kind_for_finding(Finding(lens="overclaim")) == "PRINTED_QUANTITY"
    assert kind_for_finding(Finding(lens="")) == "UNCLASSIFIED", (
        "a finding that classified itself as nothing gets no guessed kind")

    # THE KIND IS A FUNCTION OF THE TEMPLATE, which is what makes a merge consistent.
    # Two findings merge on (question text, address); if two templates that are equal
    # could carry different kinds, a merged question's kind would depend on which source
    # was seen first, and the kind decides which routes are admissible.
    seen_kind: dict[str, str] = {}
    for lens in list(_BY_LENS) + [""]:
        for d in list(_BY_DISCREPANCY) + [""]:
            for b in list(_BY_BASELINE) + [""]:
                f = Finding(lens=lens, discrepancy_type=d, baseline_class=b)
                text = _template(f)[0]
                k = kind_for_finding(f)
                assert seen_kind.setdefault(text, k) == k, (text, k, seen_kind[text])

    # --- a question for an object no lens raised ---------------------------------------
    obj = DiscoveredObject(target_id="TGT-RES-T1r0c1", kind="EXPERIMENTAL_RESULT",
                           centrality="CENTRAL")
    oq = for_object(obj)
    assert oq.kind == "PRINTED_QUANTITY" and oq.question_id == "Q-object-TGT-RES-T1r0c1"
    assert oq.from_finding == "" and oq.source_finding_ids == [], (
        "it is minted by the harness and must not claim a finding raised it")
    assert oq.materiality == "CENTRAL", "materiality mirrors the object's own centrality"
    comp = for_object(DiscoveredObject(target_id="T2", kind="REPRODUCTION_TARGET"))
    assert comp.kind == "COMPOSITION" and "compose" in comp.question
    assert for_object(DiscoveredObject(target_id="T3", kind="SCIENTIFIC_CLAIM")).kind \
        == "UNCLASSIFIED"
    print("harness.questions self-check ok")


if __name__ == "__main__":
    _self_check()
