"""Typed artifacts that cross stage boundaries (the ONLY thing that crosses).

Pydantic v2, permissive (extra allowed) so a stage can add a field without breaking
the pipeline and a partial LLM response degrades instead of raising. Each stage
writes one of these as JSON into the research log; downstream stages receive an
allow-listed subset — never the transcript.

Reviewer pipeline (S1 → S4):
    PaperDoc      ① ingestion: sectioned text + addressable tables + numbers
    LensReport    ② audit: one blinded auditor's findings
    EvalReport    ④ synthesis: the ranked, rendered verdict
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# The one harness import here, and it is safe in both directions: `harness.failures` is
# pure regex and imports nothing from this package, so the vocabulary can be defined
# beside the classification logic that owns it rather than duplicated here.
from .disposition import DISPOSITION_BASIS, PAPER_DISPOSITIONS
from .failures import FAILURE_KINDS
# Same argument again: `harness.materiality` is pure regex and pure derivation over
# duck-typed inputs, and imports nothing from this package, so the vocabulary lives beside
# the rule that owns it.
from .materiality import MATERIALITY_BASES
# `harness.taxonomy` is pure vocabulary plus pure derivation and imports nothing from this
# package either, so the same argument applies. Re-exported here because the field
# descriptions below name the vocabularies, and a description that drifts from the tuple
# it documents is worse than no description. The tuples are the SAME objects, asserted in
# `tests/test_first_review.py`.
from .taxonomy import (
    EVIDENCE_STATES,
    RESOLUTION_STATES,
    SCIENTIFIC_CLASSES,
)

# Whether an experiment was NECESSARY, and what became of that judgement. A separate axis
# from the action taken and from the target's disposition, because "we decided none was
# needed" is a SUCCESSFUL review outcome and must never be counted beside "we needed one
# and could not run it". The first four are decided before anything runs; the last three
# are what became of one that was.
EXPERIMENT_NECESSITY = (
    "NO_EXPERIMENT_NEEDED",       # the question does not turn on anything runnable
    "EXPERIMENT_WARRANTED",       # one is needed, and a route exists
    "EXPERIMENT_NOT_EXECUTABLE",  # one is needed; no artifact or host can mount it
    "EXPERIMENT_UNDERSPECIFIED",  # one is needed; the paper does not say enough to build it
    "EXPERIMENT_EXECUTED",        # it ran
    "EXPERIMENT_RESOLVED",        # it ran and settled the question
    "EXPERIMENT_UNRESOLVED",      # it ran and did not
)



class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------------- #
# ① INGESTION (S1)
# --------------------------------------------------------------------------- #
class QuantFinding(_Base):
    """A single reported number — the quantitative-prior unit, and the atom the
    overclaim and contradiction lenses argue over.

    `source_quote` is mandatory in practice: `stages/ingest.py` keeps a number only when
    it can attach the verbatim span it was printed in, so a number without a quote is
    DROPPED rather than guessed. `page` and `table_ref` carry it back to where it was
    printed. (This used to name a function `_keep_number`, which exists nowhere in the
    repository — a docstring pointing at a function nobody can open is worse than one
    that states the rule, because a reader spends time looking for it.)
    """

    benchmark: str = Field(default="", description="dataset/task the number is on, e.g. 'CIFAR-100'")
    metric: str = Field(default="", description="e.g. 'top-1 accuracy', 'perplexity'")
    method: str = Field(default="", description="which method this number is for (baseline vs proposed)")
    value: str = Field(default="", description="the reported number (keep units)")
    baseline_value: str = Field(default="", description="the baseline it improves over, if stated")
    delta: str = Field(default="", description="reported improvement, e.g. '+2.1%'")
    seeds_or_variance: str = Field(default="", description="#seeds / std / CI if reported ('' = not reported)")
    dataset_scale: str = Field(default="", description="train size / #params / compute if stated")
    source_quote: str = Field(default="", description="verbatim snippet the number came from (anti-fabrication)")
    page: int = Field(default=0, description="1-indexed PDF page the quote was printed on (0 = unknown)")
    table_ref: str = Field(default="", description="cell address 'T<table>:r<row>:c<col>' if it came from a table")


class Table(_Base):
    """One extracted table, addressable by (table_idx, row_idx, col_idx).

    `header` is row 0 when the extractor found one; `rows` excludes it. Cell text is
    whitespace-normalized but otherwise verbatim — the contradiction lens compares
    narrative claims against these strings, so they must not be reformatted.
    """

    table_idx: int
    page: int = Field(default=0, description="1-indexed PDF page")
    caption: str = Field(
        default="",
        description="the caption line VERBATIM as the paper printed it. It used to be "
                    "reconstructed as f'Table {n}: {rest}', which inserted a colon the "
                    "paper does not have: the real line 'Table 2 lists the quantitative "
                    "comparison between Weath-' became 'Table 2: lists the quantitative...'. "
                    "That string flows into QuantFinding.benchmark and "
                    "DiscoveredObject.experiment and reaches a human in targets.json and "
                    "the ledger, and unlike `evidence_quote` it is never re-verified. A "
                    "quotation the paper does not contain is what invariant 1 exists to "
                    "prevent.")
    label: str = Field(
        default="",
        description="the printed number, e.g. '3' from 'Table 3', kept OUT of `caption` so "
                    "an unlabelled table can be reported as unlabelled instead of being "
                    "given its neighbour's number. '' means this extractor could not pair a "
                    "caption with this body.")
    caption_source: str = Field(
        default="none", description="ruled_positional | geometric_paired | none — how the "
                                    "caption was associated with the body")
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)

    def ref(self, row_idx: int, col_idx: int) -> str:
        return f"T{self.table_idx}:r{row_idx}:c{col_idx}"

    def cell(self, row_idx: int, col_idx: int) -> str:
        if 0 <= row_idx < len(self.rows) and 0 <= col_idx < len(self.rows[row_idx]):
            return self.rows[row_idx][col_idx]
        return ""


class Section(_Base):
    section_idx: int
    title: str = ""
    page_start: int = 0
    page_end: int = 0
    text: str = ""


class Figure(_Base):
    """One extracted figure CAPTION — never the figure's plotted content, which this
    harness has no way to read. `ref()` is what `verify_evidence` matches against; the
    evidence class it earns (`caption_verified`) supports at most LOW confidence in
    `harness.grading.EVIDENCE_CONFIDENCE_CEILING` for exactly that reason: a caption
    NAMES a figure, it does not report the values in it. That bounds how SURE anyone can
    be, not how severe the finding may be — and a second independent check (the grader's
    own citation, or a harness-reproduced calculation) lifts the bound."""

    figure_idx: int
    page: int = 0
    label: str = ""
    caption: str = ""

    def ref(self) -> str:
        return f"F{self.figure_idx}"


class Equation(_Base):
    """One extracted display equation. Text-only and lossy by construction — symbols,
    sub/superscripts and inline math routinely survive PDF extraction mangled or
    missing — so its evidence class (`equation_verified`) supports at most MEDIUM
    confidence, not the HIGH a table cell earns.

    Extraction is deliberately willing to find NOTHING. Some papers' equations arrive as
    per-glyph fragments with no operator on any one line; `pdf._equation_body` returns ''
    rather than attaching a number to whatever text happens to precede it, because a
    wrongly assembled body would make `verify_evidence` certify a quote as
    `equation_verified` against text that is not the equation — a false machine
    attestation, which is worse than an absent one."""

    equation_idx: int
    page: int = 0
    number: str = ""
    text: str = ""

    def ref(self) -> str:
        return f"E{self.equation_idx}"


class CrossRef(_Base):
    """One place the PROSE cites a figure, table, equation, section or appendix.

    What the paper CITES, never what the paper CONTAINS. The two are constantly confused
    and confusing them is the whole risk of a document-integrity layer: over the shipped
    corpus, a naive "cited but not recovered" check produced twelve claims that a table or
    equation was missing, and all twelve of those objects are present in the paper and
    absent only from what extraction recovered.

    So this type carries no `exists`, no `resolved` and no `severity` field. It records a
    citation and a span that can be re-verified, and any comparison against a recovered
    object is somebody else's job and has to say whose property the answer is.

    It also exists because the citations were previously being ingested as CAPTIONS:
    `_FIGURE_CAPTION` matched any line beginning "Figure N", so
    'Figure 4 shows the visual comparison with real-world' became a figure object, and
    `stages.audit.verify_evidence` would then certify body prose as `caption_verified` —
    a false machine attestation of exactly the kind `pdf._equation_body` refuses to make.
    """

    kind: str = Field(default="", description="figure | table | equation | section | appendix")
    number: str = Field(default="", description="the printed number, verbatim")
    page: int = 0
    section_idx: int = 0
    span: str = Field(default="", description="a P<i>:<a>-<b> address minted by `claims`")
    quote: str = Field(default="", description="the citing sentence, so the reference itself "
                                               "is re-verifiable against the document")


class PaperDoc(_Base):
    """The parsed representation of the paper — sections, tables, numbers — and the
    ONLY thing a finding's evidence can be verified against.

    `source_path` is also handed to the reviewer, which MAY open the original PDF to
    settle a table/figure/equation-dependent ambiguity extraction can lose (see
    `SOURCE_FIDELITY` in `harness/prompts/audit.py`). That is deliberately admissible
    only to KILL a candidate or point at the right unit to cite — never to BE the
    evidence: `verify_evidence` still only accepts a quote it can re-check against this
    parsed document, so what the reviewer merely SAW in the PDF can never earn a
    verdict-moving severity on its own.
    """

    paper_id: str
    title: str = ""
    source_path: str = ""
    content_sha: str = Field(
        default="",
        description="sha256 of the PDF bytes, first 12 hex. The paper's identity as a document "
                    "rather than as a filename: `paper_id` is a slug of the file stem and two "
                    "different papers can slugify identically, at which point one would silently "
                    "load the other's doc.json. Empty on documents ingested before this field.",
    )
    n_pages: int = 0
    sections: list[Section] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    equations: list[Equation] = Field(default_factory=list)
    reported_numbers: list[QuantFinding] = Field(default_factory=list)
    crossrefs: list[CrossRef] = Field(
        default_factory=list,
        description="every place the prose cites a numbered object. WHAT THE PROSE CITES, "
                    "not what exists — see `CrossRef`.")
    body_end_section_idx: int = Field(
        default=-1,
        description="the index of the References/Bibliography heading, i.e. where the body "
                    "ends and back matter begins. -1 = not found. A boundary INDEX rather "
                    "than a per-section is_appendix flag, because the flag would be wrong "
                    "wherever a bibliography fragment was mis-parsed as a lettered heading — "
                    "which is exactly what happens: a single capital at line start matches "
                    "the section-number grammar, so an author initial turns 16 KB of one "
                    "paper's reference list into five appendix-shaped sections.")
    extraction_version: int = Field(
        default=1,
        description="bumped whenever a change renumbers or re-scopes addressable objects "
                    "(F<n>, T<i>:r<r>:c<c>, E<n>). A stored reference minted under an older "
                    "parse must not silently resolve against a newer one: it would point at "
                    "a different object and the harness would attest to it.")
    repo_url: str = Field(
        default="", description="the official code repository the paper advertises ('' = none found)"
    )
    repo_urls: list[str] = Field(
        default_factory=list,
        description="every candidate repo URL found in the text, best first. `repo_url` is "
                    "NOT simply `repo_urls[0]`: `stages/ingest` sets it from "
                    "`repo.official_repo_url`, which requires a cue word before the URL "
                    "and refuses one sitting in the reference list, so the advertised "
                    "repository can be absent while candidates are present. The two "
                    "answer different questions and the description used to conflate them.",
    )


# --------------------------------------------------------------------------- #
# ①b ADDRESSABLE REFERENCES
# --------------------------------------------------------------------------- #
# A printed result should not have to be an extractable TABLE CELL to be checkable. It
# had to, and that single assumption — `^T\d+:r\d+:c\d+$`, enforced in three places —
# was what kept a prose-stated result off the autonomous evidence path entirely: no
# grounded delta, no `table_ref`, so `reconcile` never ran on it.
#
# What replaces it is a REFERENCE KIND plus a resolver. The kinds below are exactly the
# addresses `harness.claims.resolve` can re-derive from the parsed paper; a kind the
# resolver cannot re-derive does not belong here, because an address nobody can check is
# not an address.
REFERENCE_KINDS = ("table_cell", "prose_claim", "figure", "equation", "section_span")

# Why a reference did or did not resolve. `ambiguous` is kept apart from `not_found`
# deliberately: a quote occurring three times in a paper is a real quote at an address
# nobody can name, and silently taking the first occurrence would mint a stable id for a
# span the lens may not have meant.
#
# NOT `RESOLUTION_STATES`, which is imported from `harness.taxonomy` at the top of this
# file and is a different axis entirely: whether a review QUESTION was settled and by
# what. This tuple used to be bound to that name and shadowed it, so
# `ScientificFinding.resolution_status`, `ReviewQuestion.resolution_status` and
# `LedgerEntry.resolution_state` — three fields a reader inspects the schema of — all
# documented themselves as "resolved | not_found | ambiguous | malformed | span_mismatch".
# The renders were right only because `stages/report._RESOLUTION_GLOSS` hardcodes the real
# tokens, which means the two definitions could drift with nothing failing.
REFERENCE_RESOLUTIONS = ("resolved", "not_found", "ambiguous", "malformed", "span_mismatch")


class ReportedQuantity(_Base):
    """A number the paper printed, parsed out of a reference's own text.

    Deliberately willing to find NOTHING. Two numbers in a span with no relation stated
    between them is not a reported quantity, it is two numbers, and picking one would be
    the same positional coincidence that `local_exec.json_metric` was criticised for.
    See `harness.claims.parse_quantity` for the three rules that decide.
    """

    value: float | None = Field(default=None, description="the quantity, or None if none is unambiguous")
    raw: str = Field(default="", description="the token the value was parsed from, verbatim")
    operands: list[float] = Field(
        default_factory=list, description="left-hand-side numbers when the span states a composition")
    expression: str = Field(
        default="", description="the composition, e.g. '58*5*10', or '' when none was stated")
    arithmetic_ok: bool | None = Field(
        default=None,
        description="does `expression` actually evaluate to `value`? None = no expression to check. "
                    "Re-evaluated by the harness with `grading._safe_eval`, never taken on trust — "
                    "this is what lets a composition claim be a REPRODUCTION TARGET rather than "
                    "merely a quotation.",
    )


class ClaimRef(_Base):
    """One resolvable address into the parsed paper, and the evidence it points at.

    The invariant that makes this safe to accept from a model: **a lens supplies a QUOTE,
    the harness mints the ADDRESS.** `harness.claims.mint` searches the parsed document
    for the quote and refuses unless it occurs exactly once. A lens that writes a
    `prose_claim` ref itself is not trusted either — `resolve` re-reads the span off the
    document and returns `span_mismatch` when the text there is not the quote.

    Ref grammar, all re-derivable from `PaperDoc` alone:

        T<t>:r<r>:c<c>      table_cell
        F<n>                figure (the CAPTION, never the plotted values)
        E<n>                equation
        S<i>                section_span
        P<i>:<start>-<end>  prose_claim — a character span in section <i>'s flattened text
    """

    ref: str = Field(default="", description="the address, in the grammar above")
    kind: str = Field(default="", description=" | ".join(REFERENCE_KINDS))
    quote: str = Field(default="", description="the text at that address, verbatim from the doc")
    section_idx: int = Field(default=-1, description="-1 when the kind is not section-anchored")
    span: tuple[int, int] | None = Field(
        default=None, description="(start, end) into the flattened section text, prose_claim only")
    page: int = Field(default=0, description="1-indexed page, 0 = unknown")
    resolution: str = Field(default="malformed",
                        description=" | ".join(REFERENCE_RESOLUTIONS))
    detail: str = Field(default="", description="why, when `resolution` is not 'resolved'")
    quantity: ReportedQuantity | None = Field(
        default=None, description="the number this reference reports, when one is unambiguous")

    @property
    def resolved(self) -> bool:
        return self.resolution == "resolved"


# --------------------------------------------------------------------------- #
# ② AUDIT (S2)
# --------------------------------------------------------------------------- #
# NOTE is not a defect grade. It exists so a candidate that survived verification but
# carries no threat to any claim (an open question, a figure-only observation, a
# non-degenerate-but-inconsequential concern) can be PRINTED without being COUNTED —
# `_SEVERITY_RANK` in `stages/report.py` deliberately has no "NOTE" key in the counting
# dict, so it can never cross a threshold, only display below MINOR.
SEVERITIES = ("FATAL", "MAJOR", "MINOR", "NOTE")

# Confidence is a SEPARATE axis from severity — "how sure am I", not "how bad is it".
# A HIGH-confidence MINOR and a LOW-confidence FATAL are both coherent; the ceiling a
# grader's confidence places on `counted_severity` lives in `harness/grading.py`.
CONFIDENCES = ("HIGH", "MEDIUM", "LOW")

# The four-way, non-collapsible bucket a lens must sort its own candidate into.
# `harness.grading.CANDIDATE_CAP` ENFORCES the distinction rather than merely documenting
# it: only CONFIRMED_FINDING is eligible to carry FATAL/MAJOR, a PLAUSIBLE_CONCERN is
# capped at MINOR, and an OPEN_QUESTION or DISMISSED counts toward no threshold at all.
# "Never promote a suspicion directly to a confirmed finding" is that cap, not an
# instruction. See `FINDING_CLASSES` below for the harness-derived analogue, which
# additionally distinguishes REFUTED/UNGRADED.
#
# DISMISSED is kept as a reportable bucket rather than being dropped at the source. A
# candidate the lens raised and then talked itself out of is exactly the work the
# discipline asks for, and a reviewer who can see it did not have to wonder whether the
# question was ever asked.
CANDIDATE_CLASSES = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "DISMISSED")

# §10 — how much a MISSING comparison actually matters, which is a different question
# from whether it is missing. Absence is easy to establish and says nothing on its own;
# these four say what the absence costs the paper, and `harness.grading.BASELINE_CAP`
# holds a finding to the lens's own answer.
BASELINE_CLASSES = ("OPTIONAL_COMPARISON", "USEFUL_CONTROL", "IMPORTANT_MISSING_BASELINE",
                    "CENTRAL_VALIDITY_THREAT", "NOT_APPLICABLE")

# §10 — WHERE a novelty or prior-art claim's load-bearing half comes from. The harness can
# re-verify a quote from this paper and cannot re-verify the reviewer's recollection of
# another one, so the three are not interchangeable and `harness.grading.PRIOR_ART_CAP`
# prices them differently.
PRIOR_ART_BASES = ("PAPER_INTERNAL", "EXTERNAL_VERIFIED", "REVIEWER_INFERENCE",
                   "NOT_APPLICABLE")

# What KIND of numeric discrepancy this is, once recomputed — these are not equivalent,
# and only GENUINE_CONTRADICTION is evidence of a defect; the others are usually not.
DISCREPANCY_TYPES = ("ARITHMETIC_ERROR", "DEFINITIONAL_MISMATCH", "DIFFERENT_DENOMINATOR",
                     "UNCLEAR_REPORTING", "GENUINE_CONTRADICTION", "NOT_A_DISCREPANCY",
                     "NOT_APPLICABLE")

# Where a finding's evidence comes FROM — derived by the harness from the shape of
# `evidence_ref`, never trusted from the lens (a lens calling its own citation
# PAPER_TABLE when it cited a page is confused about what it cited, not authoritative
# about it). EXTERNAL_LITERATURE and REVIEWER_INFERENCE never anchor a finding's
# primary evidence — invariant #1 already drops any finding with no in-paper quote.
EVIDENCE_ORIGINS = ("PAPER_TABLE", "PAPER_TEXT", "PAPER_FIGURE", "PAPER_EQUATION",
                    "EXTERNAL_LITERATURE", "REVIEWER_INFERENCE")

# How strongly a finding's evidence was checked. Computed by the harness in
# `stages/audit._substantiated` — NEVER read from the lens file. The distinction this
# encodes is the one the report has to preserve: a cell citation is checkable in seconds
# by anyone holding the paper, a prose quote is checkable with a search, a caption names
# a figure without reporting its plotted values, an equation is lossy-extracted, and
# everything else is a model's word for it.
EVIDENCE_CLASSES = (
    "cell_verified",     # evidence_ref named a cell and that cell's contents match the quote
    "prose_verified",    # the quote was found verbatim in the parsed section text at a page ref
    "caption_verified",  # the quote was found verbatim in a figure caption
    "equation_verified",  # the quote was found verbatim in an extracted display equation
    "unverified",        # none of the above — such findings are dropped, never reach a report
)

# Whether the lens's own falsification/steelman work is present and non-degenerate.
# "legacy" is not a defect either — it means the file predates `schema_version: 2`, so
# capping it would silently erase evidence a schema change should never be able to erase
# (see `tests/test_finding_traceability.py:170-183` for the identical guarantee made
# about an earlier split). Computed by `harness.grading.pass_b_state`.
VERIFICATION_STATES = ("complete", "incomplete", "legacy")

# The outcome of independently re-doing a lens's own arithmetic (`independent_calculation`)
# through `harness.grading.recheck_calculation`. Distinguishes "the lens's math is wrong"
# from "the lens's math is right but its operands are not really in the paper" from
# "the lens made no calculation to check" — these are not equivalent conclusions.
CALC_CLASSES = ("recomputed_ok", "recomputed_mismatch", "operands_unverified",
                "unparseable", "not_applicable", "not_attempted")

# --- the independent grader (Stage 3) ---------------------------------------------- #
# A grader's verdict on ONE candidate finding it was shown blinded — no lens name, no
# severity, no other finding, no threshold. See `harness/grading.py` for how these
# combine into `Finding.counted_severity`, the field the verdict actually counts.
GRADE_VERDICTS = ("CONFIRMED", "PLAUSIBLE", "REFUTED", "INSUFFICIENT")
GRADE_SEVERITIES = ("FATAL", "MAJOR", "MINOR", "NONE")
RESOLUTIONS = ("COUNT_AS_FINDING", "REPORT_AS_CONCERN", "REPORT_AS_QUESTION", "DROP")

# The harness-derived, five-way analogue of `CANDIDATE_CLASSES`: REFUTED and UNGRADED are
# facts about the GRADING PROCESS, not buckets a lens could ever put itself in.
FINDING_CLASSES = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION",
                   "REFUTED", "UNGRADED")


class Grade(_Base):
    """A second, blinded reviewer's independent read of ONE candidate finding.

    Every field here is a MODEL'S CLAIM, exactly as `Finding.severity` is — nothing on
    this model is harness-written, which is what makes the invariant-#2 test for the
    grader trivial to state: a `Grade` that round-trips through `_coerce` unchanged is
    correct behaviour, because nothing on it needs overwriting. What the harness DOES
    write, from this plus the candidate's own evidence, lives back on `Finding`:
    `finding_class`, `counted_severity`, `grade_state`, `binding_cap`, `derivation`,
    `grader_evidence_class`, `grader_verified_observation` — see `harness/grading.py`.
    """

    verdict: str = Field(default="", description=" | ".join(GRADE_VERDICTS))
    severity: str = Field(default="", description="the GRADER's own impact grade: "
                                                   + " | ".join(GRADE_SEVERITIES))
    confidence: str = Field(default="", description=" | ".join(CONFIDENCES))
    impact_statement: str = Field(default="", description="what breaks in the paper's argument if "
                                                           "this candidate is right")
    falsification: str = Field(default="", description="the most reasonable reading under which "
                                                        "this is NOT a problem")
    falsification_survived: bool = False
    steelman: str = Field(default="", description="the strongest good-faith reading of the "
                                                   "authors' choice")
    independent_evidence_ref: str = Field(default="", description="the cell/page/figure/equation "
                                                                   "the GRADER would cite")
    independent_evidence_quote: str = ""
    reached_independently: bool = Field(
        default=False, description="from the paper itself, or from the first reader's argument?")
    resolution: str = Field(
        default="", description="ADVISORY ONLY, never obeyed: " + " | ".join(RESOLUTIONS))
    open_question: str = Field(default="", description="if this is really a question, the "
                                                        "question to print")
    notes: str = ""


class EvidencePointer(_Base):
    """One SIDE of a concern that lives in more than one place in the paper.

    A cross-section concern — the abstract asserts what the conclusion concedes, a table
    disagrees with the prose that cites it — has two locations and is only checkable if
    both of them are. Written as one quotation plus prose asserting that some other
    section disagrees, it is unfalsifiable: the second half names nothing a reader can
    open, so the claim rests on the model's word for what the rest of the paper says.
    That is precisely the shape of assertion this harness refuses everywhere else, and it
    would have arrived through the cross-part synthesis, which exists to find exactly
    these relationships.

    So each side carries its own quotation and its own reference, each resolves against
    the parsed paper independently, and a finding any of whose sides fails to resolve is
    dropped whole. `role` is the lens's own label for what this side is ("abstract
    claim", "conclusion concession"); it is prose and decides nothing.
    """

    role: str = Field(default="", description="the lens's label for this side of the concern")
    evidence_quote: str = Field(default="", description="verbatim text at this location")
    evidence_ref: str = Field(default="", description="'p7' | 'T2:r3:c4' | 'F1' | 'E2'")
    # HARNESS-WRITTEN, exactly as on `Finding` and for the same reason (invariant 2): a
    # lens must not be able to certify that its own second citation was checked.
    evidence_class: str = Field(default="", description="WRITTEN BY THE HARNESS: "
                                                        + " | ".join(EVIDENCE_CLASSES))
    verified_observation: str = Field(default="", description="WRITTEN BY THE HARNESS")


class Finding(_Base):
    """One defect a lens asserts, with the layers of its justification kept apart.

    The fields divide into three groups that must not be confused, because the whole
    value of the report depends on a reader being able to tell them apart:

      WRITTEN BY THE LENS, VERIFIED   evidence_quote + evidence_ref. Re-checked against
                                      the parsed paper by `_substantiated`; a finding
                                      whose quote is not really there is dropped.
      WRITTEN BY THE HARNESS          verified_observation + evidence_class. Machine
                                      generated at load time, so a lens cannot assert
                                      that its own reasoning was confirmed.
      WRITTEN BY THE LENS, UNVERIFIED claim, reasoning, conclusion, severity. This is
                                      inference. It is useful and it is not evidence,
                                      and the renderer labels it as inference.

    Everything after `verifiable_by_experiment` is optional with a fallback to
    `statement`, so lens files written before this split still load unchanged.
    """

    finding_id: str = ""
    lens: str = Field(default="", description="overclaim | protocol | confound | contradiction")
    severity: str = Field(default="MINOR", description="FATAL | MAJOR | MINOR")
    title: str = Field(default="", description="one compressed line, <=90 chars")
    statement: str = Field(default="", description="the defect, one or two sentences")
    target: str = Field(default="", description="the claim or cell under attack, verbatim")
    evidence_quote: str = Field(default="", description="verbatim text/cell value that establishes the defect")
    evidence_ref: str = Field(default="", description="'p7' or 'T2:r3:c4' — where evidence_quote lives")
    counter_explanations: list[str] = Field(
        default_factory=list, description="what else could produce the reported result"
    )
    verifiable_by_experiment: bool = Field(
        default=False, description="could a reproduction run settle this? drives the S3 trigger"
    )
    additional_evidence: list[EvidencePointer] = Field(
        default_factory=list,
        description="further locations this ONE concern depends on. Empty for an ordinary "
                    "single-location finding. Every entry is verified exactly as "
                    "`evidence_quote`/`evidence_ref` are, and a finding with an "
                    "unresolvable side is dropped rather than kept with one half checked.")

    # --- the traceability split -------------------------------------------------------
    claim: str = Field(
        default="", description="the paper's own assertion under scrutiny. Falls back to `target`.")
    verified_observation: str = Field(
        default="",
        description="WRITTEN BY THE HARNESS at load time, never by the lens: what was actually "
                    "matched, and where. Any value supplied in a lens file is overwritten.",
    )
    evidence_class: str = Field(
        default="unverified",
        description="WRITTEN BY THE HARNESS: " + " | ".join(EVIDENCE_CLASSES))
    reasoning: str = Field(
        default="",
        description="the lens's inference from the evidence. UNVERIFIED model prose. Falls back "
                    "to `statement`.",
    )
    conclusion: str = Field(
        default="",
        description="what the lens concludes follows. UNVERIFIED. Falls back to `statement`.")
    severity_rationale: str = Field(
        default="",
        description="why this severity and not the one below it. UNVERIFIED, and the reason "
                    "`severity` is the softest joint in the chain: the evidence behind a "
                    "finding is machine-checked, the grade attached to it is not, and the "
                    "grade is what `overall_verdict` counts.",
    )

    # --- pass-B: the lens's own falsification/steelman work, and its own arithmetic ---
    # All lens-authored. Non-degeneracy is checked, not trusted — `verification_state`
    # below is the harness's verdict on whether this triple shows real work.
    candidate_class: str = Field(default="", description=" | ".join(CANDIDATE_CLASSES))
    confidence: str = Field(default="", description="separate from severity: " + " | ".join(CONFIDENCES))
    discrepancy_type: str = Field(default="", description=" | ".join(DISCREPANCY_TYPES))
    baseline_class: str = Field(
        default="", description="for a MISSING-COMPARISON finding, what its absence actually "
                                "costs the paper: " + " | ".join(BASELINE_CLASSES))
    prior_art_basis: str = Field(
        default="", description="for a NOVELTY/PRIOR-ART finding, where its load-bearing half "
                                "comes from: " + " | ".join(PRIOR_ART_BASES))
    what_the_paper_says: str = Field(default="", description="the claim restated plainly, before argument")
    alternative_interpretation: str = Field(
        default="", description="the reasonable reading under which this is NOT a problem")
    why_alternative_fails: str = Field(
        default="", description="why that reading does not resolve the issue, or '' if it does "
                                "(in which case this should not be a finding at all)")
    steelman: str = Field(default="", description="the strongest good-faith defense of the authors' choice")
    effect_on_claim: str = Field(default="", description="what follows for the paper's central claim")
    recommended_resolution: str = Field(default="", description="what would settle this")
    independent_calculation: dict = Field(
        default_factory=dict,
        description="{applies, operands:[{quote,ref}], expression, result, method} — STRUCTURED "
                    "so `harness.grading.recheck_calculation` can re-verify the operands and "
                    "re-evaluate the expression by machine instead of trusting the lens's math.",
    )

    # --- harness-written, joining the machine half above; never trusted from a lens file --
    scientific_class: str = Field(
        default="UNRESOLVED_QUESTION",
        description="WRITTEN BY THE HARNESS: what KIND of scientific issue this is, derived by "
                    "`taxonomy.classify` from the closed-vocabulary fields the lens already "
                    "chose. It is deliberately NOT an input to `grading.derive`: what kind of "
                    "issue something is must not decide how severe it is, for the same reason "
                    "evidence type must not. " + " | ".join(SCIENTIFIC_CLASSES))
    verification_state: str = Field(default="", description="WRITTEN BY THE HARNESS: "
                                                             + " | ".join(VERIFICATION_STATES))
    calc_class: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(CALC_CLASSES))
    evidence_origin: str = Field(default="", description="WRITTEN BY THE HARNESS from the shape of "
                                                          "evidence_ref: " + " | ".join(EVIDENCE_ORIGINS))
    origin_consistency: str = Field(
        default="", description="WRITTEN BY THE HARNESS: 'consistent' or 'corrected' against "
                                "whatever origin the lens itself claimed")
    cross_section: bool = Field(
        default=False,
        description="WRITTEN BY THE HARNESS: true when `additional_evidence` is non-empty, "
                    "i.e. this concern was established from more than one location. Derived "
                    "rather than asserted, so it cannot say two locations were checked when "
                    "one was.")
    source_part: str = Field(
        default="",
        description="WRITTEN BY THE HARNESS: which reading produced this concern — "
                    "'part-01', 'synthesis', or '' for a paper read in one pass. A fact "
                    "about how the review was conducted; it reaches no threshold.")
    merged_from: list[str] = Field(
        default_factory=list,
        description="WRITTEN BY THE HARNESS: finding ids folded into this one because they "
                    "carried the identical resolved address set, lens and scientific class. "
                    "Deduplication is over ADDRESSES, never over how similar two prose "
                    "statements sound.")

    # --- the independent grader (Stage 3/4) --------------------------------------------
    grade: Grade | None = Field(default=None, description="the blinded second reviewer's claim, "
                                                           "or None if this candidate was not graded")
    finding_class: str = Field(default="UNGRADED", description="WRITTEN BY THE HARNESS: "
                                                                + " | ".join(FINDING_CLASSES))
    counted_severity: str = Field(
        default="",
        description="WRITTEN BY THE HARNESS: the severity `overall_verdict` actually counts. '' "
                    "means ungraded — the verdict falls back to `severity` unchanged, which is "
                    "what makes turning grading off reproduce the pre-grading verdict exactly.",
    )
    grade_state: str = Field(default="not_graded", description="WRITTEN BY THE HARNESS: coverage "
                                                                "diagnostic for why this is or is not graded")
    binding_cap: str = Field(default="", description="WRITTEN BY THE HARNESS: which cap in "
                                                      "harness.grading actually bound counted_severity")
    derivation: str = Field(default="", description="WRITTEN BY THE HARNESS: one-line human-readable "
                                                     "explanation of the derivation above")
    grader_evidence_class: str = Field(default="unverified", description="WRITTEN BY THE HARNESS: "
                                                                          "the grader's OWN citation, "
                                                                          "verified the same way the "
                                                                          "lens's was")
    grader_verified_observation: str = Field(default="", description="WRITTEN BY THE HARNESS")
    evidence_ceiling: str = Field(
        default="",
        description="WRITTEN BY THE HARNESS: the highest CONFIDENCE this finding's citations can "
                    "support, from `harness.grading.evidence_support`. Evidence type bounds how "
                    "sure anyone can be; confidence then bounds severity. It never caps severity "
                    "directly — see that module for why the earlier direct ceiling was wrong.")
    evidence_sources: list[str] = Field(
        default_factory=list,
        description="WRITTEN BY THE HARNESS: the independent checks behind this finding — the "
                    "lens's citation, the grader's own citation, and a harness-recomputed "
                    "calculation each count once. More than one LIFTS `evidence_ceiling`: "
                    "corroborated evidence collectively supports a stronger finding.")

    def as_reasoning(self) -> str:
        return (self.reasoning or self.statement or "").strip()

    def as_conclusion(self) -> str:
        return (self.conclusion or self.statement or "").strip()

    def as_claim(self) -> str:
        return (self.claim or self.target or "").strip()


class LensReport(_Base):
    lens: str
    schema_version: int = Field(
        default=0,
        description="the LENS PROMPT's contract version this report was written against. "
                    "Report-level, not per-finding — `prompts/audit._RETURN` puts it here, "
                    "beside `findings`. Declared explicitly rather than surviving as an "
                    "`extra` field because `stages/audit._coerce` has to read it and thread "
                    "it into `grading.pass_b_state`: while it was only an extra, that "
                    "function looked for it on each FINDING, found nothing on every real "
                    "lens file, and returned `legacy` — which is uncapped — so the whole "
                    "falsification/steelman non-degeneracy check was inert in production "
                    "while its unit tests passed. 0 means a pre-contract file.",
    )
    findings: list[Finding] = Field(default_factory=list)
    unasked_question: str = Field(
        default="", description="the obvious baseline/comparison this lens finds conspicuously absent"
    )
    notes: str = Field(default="", description="what the auditor actually checked vs skimmed")
    merged_duplicates: int = Field(
        default=0,
        description="WRITTEN BY THE HARNESS: concerns folded into an earlier one because "
                    "they resolved to the identical address set under the same lens and the "
                    "same scientific class. Counted rather than silent: a paper read in "
                    "parts can raise one concern twice, and a merge that left no trace "
                    "would look like a reader that found less.")


# --------------------------------------------------------------------------- #
# ③ LOCAL REPRODUCTION PROBE (S3)
# --------------------------------------------------------------------------- #
# Why a backend was or was not selected for an experiment. Selection is a matching
# problem, not a configuration lookup: the question is which registered environment can
# host THIS experiment's declared demand, and the answer has to name the ones that could
# not and why. "No backend" and "a T4 would fit but cannot be provisioned from here" are
# different facts, and only the second tells an operator what to do next.
SELECTION_CODES = (
    "selected",
    "no_backend",               # nothing is registered
    "resources_insufficient",   # every candidate is too small
    "platform_incompatible",    # every candidate runs the wrong OS
    "credentials_unavailable",  # a candidate fits but needs provisioning this host cannot do
    "backend_unavailable",      # a candidate CAN execute and fits, but is not reachable now
    "requirement_unknown",      # the demand was never established, so nothing can be matched
)
# `backend_unavailable` is the one state that has no equivalent among the others: every
# other code is a permanent property of the experiment or of the registry, and this one is
# a transient property of the world. Before it existed, a runnable backend that was simply
# down fell through to `resources_insufficient` — with an empty reason, because no
# candidate had actually been rejected for size. A retry is the right response to one and
# never to the others, which is why they must not share a code.


class ProbeSpec(_Base):
    """What to actually run locally to test one finding.

    `script` is the escape hatch: when the driver can write a faithful reproduction of
    the paper's setup, it goes here and the default template is not used. When it is
    empty the default probe measures this machine's SEED NOISE FLOOR instead, which is
    the number you need before any claimed delta can be called detectable.
    """

    paper_id: str
    finding_id: str = Field(default="", description="the Finding this probe tests, if any")
    claim: str = Field(default="", description="the claim under test, verbatim")
    claimed_delta: float | None = Field(default=None, description="the delta the paper claims")
    metric: str = Field(default="accuracy", description="name of the metric the script prints")
    arms: list[str] = Field(default_factory=lambda: ["baseline", "treatment"])
    seeds: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    dataset: str = Field(default="digits", description="dataset key the default template loads")
    epochs: int = Field(default=30, description="training length for the default template")
    script: str = Field(default="", description="full probe source; empty = default template")
    command: list[str] = Field(
        default_factory=list,
        description="the repo's own evaluation command, e.g. ['python','eval.py','--seed','{seed}']. "
                    "When set it REPLACES the generated script: {seed} and {arm} are substituted "
                    "per run. This is how a cloned repository is executed.",
    )
    cwd: str = Field(default="", description="working directory for `command` (the checkout root)")
    interpreter: str = Field(
        default="", description="python to run with; '' = cfg.python. Set to the isolated venv."
    )
    table_ref: str = Field(
        default="", description="the cell 'T<t>:r<r>:c<c>' the executed metric is reconciled against"
    )
    claim_ref: str = Field(
        default="",
        description="the ADDRESS the executed metric is reconciled against, in any form "
                    "`harness.claims.resolve` can re-derive — a cell, but also a prose span "
                    "'P<i>:<a>-<b>', a figure or an equation. `table_ref` stays cell-only so "
                    "nothing keyed on a cell address silently starts matching prose; this is "
                    "the general field, and it is what made a prose-stated result reachable by "
                    "the autonomous path at all.",
    )
    claim_kind: str = Field(default="", description=" | ".join(REFERENCE_KINDS))
    claimed_cell_value: str = Field(
        default="", description="that address's contents verbatim, carried so reconciliation can parse it"
    )
    target_id: str = Field(
        default="", description="the DiscoveredObject this spec pursues, '' for a legacy spec")
    written_by: str = Field(
        default="",
        description="'harness' when `stages/probe.run` persisted this spec.json itself, at the "
                    "end of a run. Never set by a human-authored override, which is the whole "
                    "point of the field: `build_spec` reads it to tell its OWN previous output "
                    "apart from a genuine hand-written spec.json, so a stale target the audit has "
                    "since moved past is never mistaken for an instruction to keep re-running it.",
    )
    provenance: str = Field(
        default="template",
        description="who wrote the code that runs: 'template' = the identical-arms noise floor; "
                    "'synthesized' = the probe planner wrote it from the paper's own formulation; "
                    "'driver' = a human wrote spec.json; 'repo_exec' = the paper's own checkout; "
                    "'reimpl_exec' = harness.reimplement_driver's governed reconstruction, "
                    "admissible ONLY with `reimplementation_conformance.established` — see "
                    "local_exec.reconcile. This decides how far a reconciliation is allowed to go.",
    )
    mechanism: str = Field(
        default="", description="the planner template that authored the script, e.g. 'ldreg'"
    )
    rationale: str = Field(
        default="", description="why the planner chose this mechanism, in one paragraph"
    )
    aux_metrics: list[str] = Field(
        default_factory=list, description="secondary quantities the script reports via SH_AUX"
    )
    capability: ExecCapability | None = Field(
        default=None,
        description="whether this machine can give the repository a fair run. Decided before "
                    "execution; gates whether a crash may be read as a failed reproduction.",
    )
    experiment: ExperimentIdentity | None = Field(
        default=None, description="which repository command produces the cited cell")
    metric_identity: MetricIdentity | None = Field(
        default=None, description="that the executed number is the cell's quantity, on its basis")
    configuration: ConfigurationIdentity | None = Field(
        default=None, description="dataset/model/schedule/seed policy, matched or explicitly not")
    comparison: Comparison | None = Field(
        default=None,
        description="what this spec's result would be held against, derived by "
                    "`harness.comparison` from the ROUTE. None means the layer did not run, "
                    "which is how a hand-written spec.json keeps behaving exactly as it "
                    "did; an unestablished one stops the process before it starts, for the "
                    "same reason `admissible_if_it_succeeds` does.")
    backend: str = Field(
        default="local",
        description="the execution backend this spec was planned against. Recorded on the spec "
                    "because capability was assessed against THAT backend's platform, so a spec "
                    "planned for one backend and run on another is not the spec that was checked.",
    )
    resources: ResourceCapability | None = Field(
        default=None,
        description="E1 — whether the published experiment fits this backend's hardware. A "
                    "requirement is a fact about the paper, so it is stored on the spec; the "
                    "hardware it was compared against is recorded alongside it.",
    )
    commit: str = Field(
        default="",
        description="E2 — the audited commit. The SHA the static audit read and the identity "
                    "layer reasoned over. Execution is verified against THIS, not against "
                    "whatever the checkout happens to hold when the run starts.",
    )
    commit_state: str = Field(
        default="unassessed",
        description="the checkout's state at PLANNING time. Recorded even when the execution "
                    "gate is shut, so a report can say the code was not the audited code; "
                    "execution re-verifies against the disk independently.")
    backend_selection: str = Field(
        default="", description="why a backend was or was not chosen: " + " | ".join(SELECTION_CODES))
    backend_considered: list[str] = Field(
        default_factory=list,
        description="every registered environment and why it was accepted or rejected. Kept so "
                    "an INCONCLUSIVE can say 'a 16 GiB T4 would fit but cannot be provisioned "
                    "from here' rather than leaving a reader to infer it from a bare refusal.",
    )
    reimplementation_conformance: "ReimplementationConformance | None" = Field(
        default=None,
        description="set only for provenance 'reimpl_exec' — whether harness.reimplement_driver's "
                    "governed reconstruction actually binds every REQUIRED ingredient the paper "
                    "specifies to both a paper locator and a verified implementation locator. "
                    "None or `established=False` refuses reconciliation exactly as an unproven "
                    "ExperimentIdentity refuses one for `repo_exec` — see local_exec.reconcile.",
    )


class ReimplementationIngredient(_Base):
    """One thing an independent reimplementation needs, and whether the paper supplies it.

    `ref` and `quote` exist so the judgement is checkable rather than asserted: a reader
    can open the section named and see the same sentence. An absent ingredient carries
    neither, which is the point — there is nothing to show.
    """

    kind: str = Field(description="method | architecture | preprocessing | training | dataset | "
                                  "metric | hyperparameters | comparison_target")
    required: bool = Field(default=True, description="whether its absence blocks reimplementation")
    present: bool = False
    ref: str = Field(default="", description="s<N> section, E<N> equation, or T<N> table")
    quote: str = Field(default="", description="the sentence found there, for a human to re-check")


class ReimplementationReadiness(_Base):
    """Whether PATH B is open for this paper — an ELIGIBILITY decision, never a claim.

    Nothing in this artifact says anything about whether the paper is right. It says
    whether the paper says enough to rebuild its experiment, so that "we did not test
    this" can be an established fact with named gaps instead of an unexplained stop.
    """

    established: bool = False
    ingredients: list[ReimplementationIngredient] = Field(default_factory=list)
    missing: list[str] = Field(
        default_factory=list,
        description="required ingredients the paper does not supply. Non-empty ⇒ NOT_VERIFIED, "
                    "and an implementation would have to INVENT these, which is the one thing "
                    "PATH B may never do.",
    )
    reason: str = ""


class ReimplementationBinding(_Base):
    """One required ingredient, tied to BOTH a paper locator and an implementation locator.

    `paper_ref`/`paper_quote` are copied verbatim from the `ReimplementationReadiness`
    ingredient that already established eligibility — they are not re-derived here, so a
    binding can never claim a paper locator eligibility itself refused. `impl_ref` and
    `impl_quote` are what `harness.reimplement_driver` adds: WHERE in the generated
    program this ingredient is realized, and the literal snippet that shows it.

    `verified` is written by the harness, never trusted from the driver's own report —
    the same discipline `claims.verify_evidence` applies to a lens's quote: `impl_quote`
    is re-checked against the actual script text the driver returned, so a delegate that
    asserts a binding it did not really write cannot make one true by saying so.
    """

    kind: str = Field(description="method | training | dataset | metric | comparison_target "
                                  "(the REQUIRED ReimplementationIngredient kinds only)")
    paper_ref: str = ""
    paper_quote: str = ""
    impl_ref: str = Field(default="", description="line/function in the generated script")
    impl_quote: str = Field(default="", description="the literal snippet naming impl_ref")
    verified: bool = Field(
        default=False,
        description="`impl_quote` is non-empty and was found verbatim in the script text this "
                    "harness actually persisted — written by the harness, never read off the "
                    "driver's own say-so.",
    )
    bound: bool = Field(
        default=False,
        description="paper_ref, impl_ref and verified all hold — the one thing decision 1 "
                    "requires of EVERY required ingredient before a reconstruction may run.",
    )


class ReimplementationConformance(_Base):
    """Whether a governed reconstruction may reconcile against a printed cell at all.

    THE GATE `reimpl_exec` PROVENANCE ADDS. `ReimplementationReadiness.established` says
    the PAPER specifies enough to attempt a reconstruction; this says the ATTEMPT that was
    actually written stayed inside what the paper specified — every required ingredient
    bound to both a paper locator and a verified implementation locator, with nothing
    invented. Established here is required in `local_exec.reconcile` and `backends.authorize`
    before either may draw a verdict from a `reimpl_exec` run, exactly as an unproven
    `ExperimentIdentity` refuses one for `repo_exec`.

    A conformant reconstruction's disagreement may establish a failure of the paper's
    STATED METHOD; it is never phrased as the authors' own code failing, because it is
    not the authors' own code — see `harness.provenance.PROVENANCE_LABEL['reimpl_exec']`.
    """

    established: bool = False
    verified_by: str = Field(
        default="",
        description="identity of the independent verifier that checked the proposed "
                    "bindings. Empty means the generator's report has not been "
                    "independently assessed and cannot authorize execution.")
    generated_by: str = Field(
        default="",
        description="identity of the context that generated the reconstruction. It must "
                    "be non-empty and different from verified_by for established=True.")
    independently_verified: bool = Field(
        default=False,
        description="written by the acceptance boundary only when generated_by and "
                    "verified_by name distinct recorded contexts. Generated code cannot "
                    "self-certify conformance.")
    bindings: list[ReimplementationBinding] = Field(default_factory=list)
    unbound: list[str] = Field(
        default_factory=list,
        description="required ingredient kinds that did NOT bind — missing an impl_ref, "
                    "an unverifiable impl_quote, or absent from the driver's own report.",
    )
    reason: str = ""


class RepoAcquisition(_Base):  # noqa: D401 — see ReimplementationReadiness above
    """Where the code under test came from, and whether it is really the paper's.

    `status` is the honest part. "unavailable" and "blocked" are not accusations: a
    paper that ships no code is not thereby a bad paper, and a harness running with
    the network gate closed has not discovered anything about the repository.
    """

    url: str = ""
    status: str = Field(
        default="not_attempted",
        description="cloned | cached | synthesized | unavailable | blocked | failed | not_attempted",
    )
    path: str = Field(default="", description="runs/<pid>/repo or runs/<pid>/standalone_probe.py")
    commit: str = Field(
        default="",
        description="the FULL 40-character SHA on disk. Full, not abbreviated, because this is "
                    "the value execution is verified against and a 12-character prefix cannot "
                    "be compared for equality with a real HEAD without loosening the check.",
    )
    requested_revision: str = Field(
        default="",
        description="the commit acquisition was ASKED for. Empty means the default branch was "
                    "taken, which is not a pin: the branch moves and the next clone is different "
                    "code under the same reasoning.",
    )
    pinned: bool = Field(
        default=False,
        description="the checkout is at an explicitly requested revision, not at whatever the "
                    "default branch pointed to when git was run",
    )
    shallow: bool = Field(
        default=False,
        description="a depth-1 clone. It holds one commit and cannot check out another without "
                    "a further fetch, which matters when the audited SHA is not the one present.",
    )
    reason: str = ""
    reimplementation: "ReimplementationReadiness | None" = Field(
        default=None,
        description="set only when NO repository was advertised: whether the paper says enough "
                    "to rebuild the experiment independently (PATH B), and if not, which "
                    "ingredients are missing. Eligibility only — it concludes nothing about the "
                    "paper's claims.",
    )
    dependency_files: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list, description="torch / jax / sklearn / …")
    entrypoint: str = Field(default="", description="the command the probe will run, if one was found")
    env_path: str = Field(default="", description="isolated interpreter built for this repo, if any")
    env_status: str = Field(default="not_attempted", description="ready | blocked | failed | not_attempted")
    env_backend: str = Field(
        default="",
        description="which backend BUILT this environment. An environment is only meaningful "
                    "relative to the machine that will run in it, so a run planned for one "
                    "backend against an interpreter another backend created is running under a "
                    "substitute environment — the thing capability assessment exists to catch.",
    )


# What a checkout says it NEEDS in order to run, as opposed to what it does wrong. The
# static audit already reads the tree; these are the demands it can read out of the same
# pass, and they are requirements rather than defects — so they carry a state and a value
# instead of a severity. Nothing here is a finding about the paper.
#
# The kinds are exactly the ones for which real repository evidence exists in a rigid,
# checkable grammar. There is deliberately no kind for a demand that would have to be
# inferred: no "requires a GPU because it calls .cuda()", no "needs 40 GB because it
# mentions an A100", no "needs the network because it imports requests". A demand that
# cannot be read off a declaration is UNKNOWN, and UNKNOWN is not permission.
RUNTIME_KINDS = (
    "python_version",        # conda `python=`, `python_requires`, `requires-python`
    "cuda_runtime",          # a CUDA-tagged wheel or an nvidia-*-cuNN runtime pin
    "external_download",     # an explicit wget/curl/hf_hub_download/urlopen CALL
    "model_artifact",        # a checkpoint the code names as a string literal
    "dataset_artifact",      # a dataset the code names as a string literal
    "absolute_path",         # a POSIX path literal that only exists on the authors' host
    "scheduler_allocation",  # what the authors' own #SBATCH block asked the cluster for
)

# The same four words the identity and resource layers use. A fifth state would be a fifth
# way to be uncertain, and the point of a categorical vocabulary is that there is one.
RUNTIME_STATES = ("established", "ambiguous", "unknown", "not_applicable")

# Whether a demand is known to belong to the experiment under audit, or merely to the
# repository that contains it. A repository holds many experiments: APT ships 74 shell
# scripts carrying `#SBATCH` blocks whose `--mem` is 32G, 40G or 64G and whose `--time`
# ranges from 24 to 500 hours. Aggregating those into one number describes no experiment
# that was ever run, so a demand is `experiment` scope ONLY when it was found in the file
# the established experiment identity names, and `repository` otherwise.
RUNTIME_SCOPES = ("experiment", "repository")


class RuntimeDemand(_Base):
    """One runtime demand read out of the checkout, located the way every quote here is.

    `state` and `value` are separate on purpose. A demand can be established with a value
    ("python 3.9.19"), established as conflicting with no single value ("ambiguous: the
    requirements file pins a CUDA 11.3 wheel and the environment file pins both cu11 and
    cu12 runtimes"), or absent. Collapsing them would make "no value" indistinguishable
    from "several values", which is the difference between not knowing and being misled.
    """

    kind: str = Field(default="", description=" | ".join(RUNTIME_KINDS))
    state: str = Field(default="unknown", description=" | ".join(RUNTIME_STATES))
    value: str = Field(
        default="",
        description="the demand itself, verbatim where possible: '3.9.19', '11.3', a URL, a "
                    "hub id, a path. Empty when the state is ambiguous or unknown.")
    scope: str = Field(default="repository", description=" | ".join(RUNTIME_SCOPES))
    file: str = Field(default="", description="repo-relative path the evidence lives in")
    line: int = Field(default=0, description="1-based line, so a reader can open it directly")
    code_quote: str = Field(default="", description="verbatim source or config line")
    note: str = Field(default="", description="what this establishes, and what it does not")


class CodeAuditFinding(_Base):
    """One static-inspection hit, quoted from source the way a lens quotes the paper.

    `code_quote` is verbatim and `file`/`line` locate it, for the same reason evidence
    quotes are re-verified in S2: a reviewer must be able to check it in seconds.
    """

    finding_id: str = ""
    rule_id: str = Field(default="", description="which detector fired, e.g. 'leak-scale-before-split'")
    category: str = Field(
        default="", description="baseline_crippling | data_leakage | metric_deviation"
    )
    severity: str = Field(default="MINOR", description="FATAL | MAJOR | MINOR")
    title: str = Field(default="", description="one compressed line, <=90 chars")
    statement: str = ""
    file: str = Field(default="", description="repo-relative path")
    line: int = 0
    code_quote: str = Field(default="", description="verbatim source, never reformatted")
    counter_explanations: list[str] = Field(default_factory=list)

    @property
    def scientific_class(self) -> str:
        """Always IMPLEMENTATION_ISSUE: a static hit is by construction a statement about
        the released artifact, not about the paper's argument. A property rather than a
        field so it cannot be read from a file or set to anything else."""
        from . import taxonomy
        return taxonomy.classify(is_artifact_finding=True)


# --------------------------------------------------------------------------- #
# ②b STATIC ARTIFACT INSPECTION — what the released artifact itself can establish
# --------------------------------------------------------------------------- #
# THREE LEVELS, AND THE THIRD IS DELIBERATELY NOT A VALUE HERE.
#
#   level 1  ARTIFACT_FACT            a deterministic fact about the checkout
#   level 2  PAPER_ARTIFACT_MISMATCH  the paper says X, the pinned artifact says Y, and
#                                     the experiment identity connecting them is bound
#   level 3  the reported scientific result is false
#
# Level 3 has no spelling on this type, and that is the encoding of the rule rather than
# a note about it: static inspection alone essentially never establishes that a reported
# result is wrong. A configuration inconsistency may create a verified concern, establish
# a reproducibility defect, trigger execution, trigger focused validation, and become
# material when a central claim provably depends on it — every one of those is a
# downstream decision made by a downstream module, and none of them is reachable by
# writing a stronger string here. An AST warning may not become RED.
ARTIFACT_AUTHORITY = (
    "ARTIFACT_FACT",             # level 1: about the checkout, and nothing else
    # LEVEL 1.5, and the correction that made the first version of this ladder wrong.
    # A relocated code quotation proves that this code exists at this location in this
    # audited snapshot. It does NOT prove that the code contradicts the paper. Where both
    # endpoints are deterministic and the CORRESPONDENCE between them is the auditor's
    # reading, this is what the pairing is worth — the same distinction
    # `ENDPOINTS_VERIFIED_SEMANTIC_LINK` draws on the claim-link channel, for the same
    # reason: verifying two ends does not verify the relationship between them.
    "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN",
    "PAPER_ARTIFACT_MISMATCH",   # level 2: about the paper AND the checkout, identity bound
    "NONE",                      # observed; establishes nothing
)

# HOW an experiment identity was established, and how well. Only ESTABLISHED may support
# level-2 mismatch authority: a repository sets a batch size in a dozen places, and a
# model saying "this looks like the right config" is a guess wearing a citation's clothes.
# NAMED `ARTIFACT_` RATHER THAN `IDENTITY_STATES`, which already exists below for a
# different thing — `ProbeSpec`'s experiment-identity resolution (established / ambiguous /
# no_candidate / unmapped / unsupported). Two vocabularies under one name is not a style
# problem: the first version of this constant shadowed that one, and the auditor run's
# identity histogram came back keyed on the WRONG five values, counting nothing.
ARTIFACT_IDENTITY_STATES = (
    "ESTABLISHED",   # a deterministic source in the paper or the artifact links the two
    "PARTIAL",       # one side links; the other is the auditor's reading
    "AMBIGUOUS",     # several candidates fit and nothing in either document chooses
    "UNBOUND",       # nothing was offered
)

# The deterministic sources that may establish an identity. Each is a thing a reader can
# open. `auditor_assertion` is deliberately in the list and deliberately NOT sufficient:
# it is recorded so a human can see what was claimed, and it classifies as AMBIGUOUS.
ARTIFACT_IDENTITY_BASES = (
    "paper_names_the_command",    # the paper prints the command or path itself
    "readme_maps_the_experiment",  # the checkout's own README maps experiment -> file
    "script_passes_the_config",   # a committed script names both the experiment and the file
    "authors_experiment_table",   # the repository documents its experiments in a table
    "auditor_assertion",          # the auditor's reading, and nothing deterministic
)

# WHAT a level-1 fact is allowed to settle. A probe answers a bounded question and may
# discharge only a target asking that bounded question — the defect this closes is that
# "the checkout advertises evaluate.py" was accepted as settling "the released repository
# implements the described method", which is a semantic correspondence question no
# entrypoint existing can answer.
ARTIFACT_QUESTION_SCOPES = (
    "FILE_PRESENCE",        # does the advertised file exist in the pinned tree?
    "ENTRYPOINT_PRESENCE",  # does the repository contain a runnable entrypoint it advertises?
    "DEPENDENCY_DECLARED",  # is dependency X declared, and at what version?
    "MANIFEST_PRESENCE",    # is a dependency manifest present at all?
    "CONFIG_LITERAL",       # does config key K literally equal V at this pinned file/span?
    "COMMAND_PRESENCE",     # does command C exist?
    # NOT a scope any probe may claim. Named so that a question of this shape can be
    # RECOGNISED and refused rather than falling through to whichever fact happened to be
    # established: "does the repository implement the described method", "is the
    # implementation faithful", "does this code reproduce the paper" are semantic
    # correspondence questions, and a bounded artifact fact is supporting evidence for one,
    # never its answer.
    "IMPLEMENTATION_CORRESPONDENCE",
)

# The scopes a deterministic probe may discharge. IMPLEMENTATION_CORRESPONDENCE is
# excluded by construction, which is the fix rather than a note about it.
SETTLEABLE_BY_ARTIFACT_FACT = tuple(
    s for s in ARTIFACT_QUESTION_SCOPES if s != "IMPLEMENTATION_CORRESPONDENCE")

# WHY a proposed mismatch was not bound. Named per attempt, because "we found nothing"
# and "we found it and could not say which experiment it belongs to" are opposite
# results: the first is a clean artifact, the second is an open question.
MISMATCH_REFUSALS = (
    "paper_statement_unaddressed",   # no resolvable paper locator for the claimed statement
    "artifact_fact_unlocated",       # the file/span/hash did not relocate in the pinned tree
    "experiment_identity_unbound",   # the config exists; which experiment it configures is open
    # THE IDENTITY WAS OFFERED AND IS NOT DETERMINISTIC. Kept apart from `unbound`
    # because they are opposite facts about the auditor: one gave nothing, the other gave
    # a reading. Both stop at ENDPOINTS_VERIFIED_ARTIFACT_CONCERN.
    "experiment_identity_not_deterministic",
    "values_not_comparable",         # the two sides do not state the same kind of quantity
    # THE PAPER'S NUMBER WAS THE AUDITOR'S PICK. Found by hand-checking the corpus's first
    # level-2 mismatch: the auditor quoted a whole hyperparameter table and reported the
    # paper value as "Epochs 16 (CNN/DM column)". The span is real and the number is in it
    # — and so are 40, 32, 15 and 6, and WHICH of them is the CNN/DM column's is the
    # auditor's reading of a table layout this harness cannot re-derive. A level-2
    # mismatch may not rest on a number a model chose out of a row of numbers.
    "paper_value_not_derivable",
    "no_disagreement",               # both sides located and identity bound; they agree
)


class SourceSpan(_Base):
    """Where in the PINNED checkout something is, re-derivable by hand from the SHA.

    A model statement about code is not artifact evidence. What makes a code citation
    evidence is the same thing that makes a paper quotation evidence: the harness, not the
    writer, locates it — `artifact_evidence.relocate` searches the file for the quoted
    text and refuses unless it occurs exactly once, exactly as `claims.mint` refuses an
    ambiguous paper quotation. The file hash is recorded so a later reader can prove the
    file has not moved under the citation.
    """

    file: str = Field(default="", description="repository-relative path, forward slashes")
    line: int = Field(default=0, description="1-indexed first line of the span")
    end_line: int = Field(default=0, description="1-indexed last line, == line for one line")
    char_span: tuple[int, int] | None = Field(
        default=None, description="(start, end) byte offsets into the file's decoded text")
    quote: str = Field(default="", description="the source text at that span, VERBATIM")
    file_sha256: str = Field(default="", description="of the file's bytes in the pinned tree")
    node_type: str = Field(default="", description="the AST node kind, when one was matched")


class ArtifactSnapshot(_Base):
    """The immutable checkout every artifact fact is tied to.

    RECORDED BEFORE ANYTHING RUNS. A later execution that mutates the working tree
    invalidates the EXECUTION's identity — `repo.verify_commit` already refuses a dirty
    tree — and must not rewrite history: the static facts below were established against
    THIS snapshot and stay tied to it. That is why `dirty` and `tree_sha` are stored here
    rather than re-read at report time.
    """

    repo_url: str = ""
    commit: str = Field(default="", description="the full SHA the facts below are about")
    tree_sha: str = Field(default="", description="`git rev-parse HEAD^{tree}`, '' if unknown")
    dirty: bool = Field(default=True, description="defaults to DIRTY: an unknown tree is not "
                                                  "an audited tree")
    captured_at: str = ""
    note: str = Field(default="", description="why the snapshot is incomplete, when it is")

    @property
    def audited(self) -> bool:
        """A snapshot artifact evidence may be tied to at all. Fail-closed in every field."""
        return bool(self.commit) and bool(self.tree_sha) and not self.dirty


class ArtifactFact(_Base):
    """One thing established about the released artifact, and the strongest authority it has.

    `authority` is WRITTEN BY THE HARNESS from what was actually located, never read from
    a proposal or from a rule's own opinion of itself. A rule that fires is a rule that
    fired; whether what it found is a fact about the checkout, a mismatch with the paper,
    or nothing reportable is decided here.
    """

    fact_id: str = ""
    probe: str = Field(default="", description="which deterministic probe or rule produced it")
    settles: str = Field(default="", description="WRITTEN BY THE HARNESS: which of "
                                                 + " | ".join(ARTIFACT_QUESTION_SCOPES)
                                                 + " this fact answers. '' answers none, and "
                                                   "a fact that answers none discharges "
                                                   "nothing whatever its authority")
    statement: str = Field(default="", description="what was established, in the harness's words")
    authority: str = Field(default="NONE", description="WRITTEN BY THE HARNESS: "
                                                       + " | ".join(ARTIFACT_AUTHORITY))
    snapshot: ArtifactSnapshot | None = None
    span: SourceSpan | None = None
    # --- level 2 only -----------------------------------------------------------------
    paper_ref: str = Field(default="", description="the paper address the mismatch is against")
    paper_quote: str = Field(default="", description="verbatim, re-verified by `claims`")
    paper_value: str = ""
    artifact_value: str = ""
    experiment_id: str = Field(default="", description="what binds the two; '' means unbound")
    identity_state: str = Field(
        default="UNBOUND", description="WRITTEN BY THE HARNESS: "
                                       + " | ".join(ARTIFACT_IDENTITY_STATES))
    identity_basis: str = Field(
        default="", description="WRITTEN BY THE HARNESS: which of "
                                + " | ".join(ARTIFACT_IDENTITY_BASES))
    identity_span: SourceSpan | None = Field(
        default=None, description="WRITTEN BY THE HARNESS: the relocated artifact location "
                                  "that establishes the identity, when one does")
    refusal: str = Field(default="", description=" | ".join(MISMATCH_REFUSALS))
    # --- always -----------------------------------------------------------------------
    counter_explanations: list[str] = Field(default_factory=list)

    @property
    def about_the_paper(self) -> bool:
        """Only a bound mismatch says anything about the paper. Everything else is the code.

        ENDPOINTS_VERIFIED_ARTIFACT_CONCERN is deliberately NOT about the paper: both of
        its locations are real and the correspondence between them is the auditor's
        reading, which is a question for a referee and not a finding about the document.
        """
        return self.authority == "PAPER_ARTIFACT_MISMATCH"

    @property
    def endpoints_only(self) -> bool:
        return self.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"


class ArtifactInspection(_Base):
    """One ARTIFACT_INSPECTION route attempt, and whether it discharged its contract.

    "The repository cloned successfully" is not artifact evidence, and `discharged` is
    what stops it from becoming so: a route discharges only when it had a question it
    could answer from the artifact, it looked, and it reached an answer. A route that
    obtained a checkout and established nothing is COMPLETED_INCONCLUSIVE, which is a
    different word from ARTIFACT_EVIDENCE for the same reason CITATION_VERIFIED is a
    different word from PAPER_ONLY_RESOLVED.
    """

    paper_id: str = ""
    target_id: str = ""
    snapshot: ArtifactSnapshot | None = None
    facts: list[ArtifactFact] = Field(default_factory=list)
    files_examined: list[str] = Field(default_factory=list)
    statements_examined: list[str] = Field(
        default_factory=list, description="the paper statements this route was asked about")
    question_scope: str = Field(
        default="", description="the bounded scope of the question this route was asked, "
                                "or IMPLEMENTATION_CORRESPONDENCE for one it may not settle")
    discharged: bool = Field(default=False, description="WRITTEN BY THE HARNESS: the route "
                                                        "answered the question it was given")
    reason: str = Field(default="", description="why it did or did not discharge")
    escalations: list[str] = Field(
        default_factory=list,
        description="what this inspection makes newly possible for a LATER route — a "
                    "narrowed command, an identified configuration, a metric "
                    "implementation, a split definition. Recorded, never acted on here.")
    proposed: int = Field(default=0, description="code-auditor proposals received")
    relocated: int = Field(default=0, description="proposals whose citation the harness relocated")

    def bound_mismatches(self) -> list[ArtifactFact]:
        return [f for f in self.facts if f.about_the_paper]

    def endpoint_concerns(self) -> list[ArtifactFact]:
        """Both locations verified, the correspondence still the auditor's reading."""
        return [f for f in self.facts if f.endpoints_only]


class CodeAudit(_Base):
    """The static pass. Runs with no execution, so it is safe on an untrusted clone."""

    repo_path: str = ""
    commit: str = Field(
        default="",
        description="the checkout's HEAD at the moment this audit read it. THE audited commit: "
                    "everything downstream — identity, execution, reconciliation — must be about "
                    "this SHA or about nothing.",
    )
    files_scanned: int = 0
    lines_scanned: int = 0
    declarations_scanned: list[str] = Field(
        default_factory=list,
        description="non-Python declaration files read for runtime demands — environment.yml, "
                    "requirements.txt, pyproject.toml, setup.py/cfg, *.sh, *.sbatch. Listed "
                    "separately because `files_scanned` counts the Python AST pass only, and a "
                    "reader has to be able to tell 'no demand found' from 'nothing was read'.")
    findings: list[CodeAuditFinding] = Field(default_factory=list)
    runtime: list[RuntimeDemand] = Field(
        default_factory=list,
        description="runtime/environment demands read out of the checkout. Recorded whether or "
                    "not anything can act on them: the capability comparison stops at its first "
                    "blocker, so a demand recorded here is still reportable when an earlier "
                    "blocker short-circuited the comparison that would have used it.")
    unparseable: list[str] = Field(default_factory=list, description="files whose AST would not build")
    skipped: str = Field(default="", description="why the audit did not run, if it did not")


# --------------------------------------------------------------------------- #
# Execution capability — the precondition on convicting a paper with its own code
# --------------------------------------------------------------------------- #
# Why a crash is not automatically a failed reproduction. "The code was run and did not
# work" is only true if the code was, in fact, run. A repository can exit non-zero
# because its declared environment cannot exist on this machine, because its
# dependencies were never installed, or because this harness invented an argv the
# entrypoint does not accept — none of which is evidence about the paper. Those are
# facts about the runner. Distinguishing them requires deciding, BEFORE execution and
# from structured state rather than from a stderr tail, whether a fair attempt is even
# possible.
CAPABILITY_CODES = (
    "established",              # a fair attempt is possible
    "not_attempted",            # repo execution was never planned
    "environment_incompatible", # the declared environment cannot be built or was not built
    "dependency_missing",       # the target interpreter cannot import what the entrypoint needs
    "invalid_invocation",       # the command this harness would issue is not one the repo accepts
    "resources_insufficient",   # the declared experiment does not fit this backend's hardware
    "commit_mismatch",          # the checkout on disk is not the commit that was audited
)
# How a non-zero exit is accounted for. Only `runtime_failure` may convict.
FAILURE_CLASSES = (
    "none",
    "environment_incompatible",
    "dependency_missing",
    "invalid_invocation",
    "startup_failure",          # capable environment, but the process died before the experiment
    "runtime_failure",          # the experiment was reached and then failed
    "timeout",
    "experiment_unidentified",  # no proven mapping from the cited cell to a repo command
    "metric_unbound",           # the executed number is not the cell's quantity/basis
    "configuration_unmatched",  # the run's settings are not the cell's settings
    # The result would have had nothing to be held against. Distinct from the three
    # identity classes above: those say the wrong program would run, this says the
    # right one would run and this harness could not compare what it produced.
    "comparison_unestablished",
    "execution_unauthorized",   # the harness refused to run it — a fact about us, not them
    "resources_insufficient",   # the experiment as published does not fit the hardware here
    "commit_mismatch",          # the code that would run is not the code that was audited
    "backend_unavailable",      # no registered backend can host this experiment
    "credentials_unavailable",  # a backend could host it but cannot be provisioned from here
)

# How an execution request was decided. `allowed` is the whole decision; the code names
# WHICH condition settled it, so a report can say "the gate was shut" rather than the
# undifferentiated "we did not run it".
EXEC_DECISIONS = (
    "authorized",
    "not_repo_execution",       # our own generated probe; repository gates do not apply
    "gate_closed",              # SH_ALLOW_REPO_EXEC is not set
    "no_backend",               # no usable execution backend
    "provenance_insufficient",  # a command was set, but the code is not the authors'
    "identity_unproven",        # experiment / metric / configuration not established
    "capability_unproven",      # this machine cannot give the code a fair run
    "resources_unproven",       # the experiment's resource demand is unmet or unestablished
    "commit_unverified",        # the checkout is not provably the audited commit
    "backend_cannot_execute",   # the selected backend is a declaration, not a runner
    "backend_mismatch",         # assessed against one backend, asked to run on another
    "backend_offline",          # a real runner, temporarily unreachable — retry may succeed
    # A spec that claims an admissible provenance and contains no program at all. Not a
    # weak spec, a malformed one: `write_probe` would fall back to the identical-arms
    # noise-floor template, whose number would then be reconciled against the paper's
    # printed cell under a provenance the ceiling admits. See `local_exec.run_probe`.
    "spec_incomplete",
)



# Both new decisions above are refusals with the same shape as the others: they name a
# fact about this harness or this host, never about the paper. `resources_unproven`
# covers "the experiment is bigger than the machine" AND "we could not establish how big
# it is" — because permitting on the strength of the second would be permitting on the
# strength of the paper's silence.


# --------------------------------------------------------------------------- #
# Identity — WHICH experiment, WHICH quantity, WHICH configuration
# --------------------------------------------------------------------------- #
# Capability answers "can this machine run the code". It does not answer "is this the
# right program, emitting the right quantity, under the right settings". A repository can
# execute perfectly and still say nothing about the cited cell — which is worse than a
# crash, because a crash is visible and a confident irrelevant number is not.
#
# States are CATEGORICAL on purpose. A numeric confidence invites a threshold, and a
# threshold turns "we are not sure which experiment this is" into an execution
# authorization. Abstention has to be a state, not a low score.
IDENTITY_STATES = (
    "established",   # proven, with re-verifiable evidence
    "ambiguous",     # several candidates fit equally well; picking one would be a guess
    "no_candidate",  # nothing in the repository can produce this
    "unmapped",      # not yet assessed
    "unsupported",   # the repository cannot express what the cell requires
)


class IdentityEvidence(_Base):
    """One quoted justification for an identity claim, checkable the way S2 evidence is.

    The mapping from a paper cell to a repository command is judgement, and this harness
    does not accept judgement without provenance anywhere else. `source_ref` names a
    file and line in the checkout ("scripts/x.sh:93"), a paper cell ("T2:r3:c11") or a
    page ("p7"), so a reader can re-check the inference rather than trust it.
    """

    quote: str = ""
    source_ref: str = ""
    note: str = ""


class CandidateCommand(_Base):
    """A command the REPOSITORY advertises, discovered rather than invented."""

    argv: list[str] = Field(default_factory=list)
    source: str = Field(default="", description="readme | run_script | scripts_dir | makefile")
    source_ref: str = Field(default="", description="file:line the command was read from")
    declared_args: dict[str, str] = Field(
        default_factory=dict,
        description="field -> value this command's OWN text declares (a hardcoded "
                    "'--sparsity 0.5' in a scripts_dir candidate, a referenced config "
                    "file, or an argparse default), written by "
                    "`harness.alignment.candidates.declared_configuration`. This is what "
                    "lets `harness.alignment.configuration.narrow` tell two candidates "
                    "that both emit the cited quantity apart by what CONFIGURATION each "
                    "one runs under — the fix for a repository whose 84 scripts all "
                    "measure accuracy and differ only by which row they belong to.")
    declared_args_evidence: dict[str, str] = Field(
        default_factory=dict,
        description="field -> source_ref for each entry in `declared_args`, so a matched "
                    "configuration is checkable the way every other evidence pointer in "
                    "this harness is, not merely asserted.")
    seed_flag: str = Field(default="", description="the seed flag the repo itself uses, if any")
    seed_values: list[str] = Field(default_factory=list, description="seeds the repo passes")
    emits: list[str] = Field(default_factory=list, description="output keys/files this command writes")
    label: str = Field(default="", description="what the command actually does")


class ArgSpec(_Base):
    """One CLI argument an entrypoint's own `argparse.add_argument` call declares.

    Written by `harness.alignment.argparse_surface`, which reads the SOURCE and never
    runs it — `harness.alignment.trial` is the only place in that package that asks the
    program itself, and it is optional and gated. Every field here is what the source
    text says, not what a real invocation would show.
    """

    flag: str = ""
    default: str = ""
    choices: list[str] = Field(default_factory=list)
    type: str = ""
    required: bool = False
    is_flag: bool = Field(
        default=False, description="action='store_true': no value, presence only")
    source_ref: str = ""


class TrialResult(_Base):
    """The outcome of an OPTIONAL, GATED `--help` invocation confirming an argparse
    surface against the real program.

    Written by `harness.alignment.trial`, which never touches `backends.authorize()` or
    `experiment_id.identities_established` — this is a separate, narrower permission
    (`SH_ALLOW_ALIGNMENT_TRIAL`) for a cheaper action than running the experiment, gated
    by the SAME isolation sufficiency repository execution requires, because a `--help`
    invocation still executes the top of a file this harness did not write.

    `attempted=False` means the gate was shut or isolation was insufficient — nothing
    ran, and that is recorded as a fact about this review, never as a fact about the
    repository. `attempted=True, ran=False` means a process was refused or crashed before
    producing output. Only `ran=True` entitles `confirmed_flags` to mean anything.
    """

    attempted: bool = False
    ran: bool = False
    argv: list[str] = Field(default_factory=list)
    returncode: int | None = None
    stdout_tail: str = Field(default="", description="the last portion of stdout, for a reader")
    confirmed_flags: list[str] = Field(
        default_factory=list,
        description="flags the STATIC surface declared and this run's --help output "
                    "actually printed")
    unconfirmed_flags: list[str] = Field(
        default_factory=list,
        description="flags the static surface declared that --help did NOT print — a "
                    "real disagreement between the source read and the program run, "
                    "worth a reader's attention")
    backend: str = ""
    reason: str = Field(default="", description="why nothing ran, when `attempted` is False")


class _Identity(_Base):
    state: str = Field(default="unmapped", description=" | ".join(IDENTITY_STATES))
    evidence: list[IdentityEvidence] = Field(default_factory=list)
    reason: str = ""

    @property
    def established(self) -> bool:
        return self.state == "established"


class ExperimentIdentity(_Identity):
    """Which repository command produces the cited cell — or that none does."""

    command: CandidateCommand | None = None
    finding_id: str = ""
    table_ref: str = ""
    row_method: str = Field(default="", description="the method the cited row names, e.g. 'LLMPruner'")
    considered: int = Field(default=0, description="candidate commands examined")
    rejected: list[str] = Field(default_factory=list, description="candidate + why it was refused")


class MetricIdentity(_Identity):
    """That the executed number is the SAME quantity, on the same basis, as the cell.

    `basis` is as load-bearing as `quantity`. A cell reading 253.6% is a ratio against a
    baseline run; peak memory in MB is not that number no matter how correctly it is
    measured, and reconciling them would be a units error dressed as a reproduction.
    """

    cell_quantity: str = Field(default="", description="accuracy|memory|latency|flops|macs|loss|params")
    cell_basis: str = Field(default="", description="absolute | relative_to_baseline")
    cell_unit: str = ""
    output_key: str = Field(default="", description="the repo output key that carries the quantity")
    output_quantity: str = ""
    output_basis: str = ""
    requires_arms: list[str] = Field(
        default_factory=list, description="a relative cell needs its baseline arm too")


class ConfigurationIdentity(_Identity):
    """Dataset, model, sparsity, schedule and seed policy, matched or explicitly not."""

    matched: dict[str, str] = Field(default_factory=dict)
    unrecoverable: list[str] = Field(default_factory=list)
    seed_policy_paper: str = ""
    seed_policy_repo: str = ""
    seed_policy_match: bool | None = None


class ExecCapability(_Base):
    """Whether this machine can give the paper's own code a fair run.

    Recorded whether or not execution is attempted, because "we did not try" and "we
    tried and the environment was wrong" and "we tried and their code broke" are three
    different facts and only the last one is about the paper.
    """

    established: bool = False
    reason_code: str = Field(default="not_attempted", description=" | ".join(CAPABILITY_CODES))
    detail: str = Field(default="", description="one human-readable sentence naming the blocker")
    env_status: str = Field(default="", description="RepoAcquisition.env_status at planning time")
    interpreter: str = Field(default="", description="the python that would run the repo")
    interpreter_is_repo_env: bool = Field(
        default=False,
        description="False means the harness's own venv, which by construction lacks the "
                    "repository's dependencies — never a capable configuration",
    )
    entrypoint: str = ""
    checked_imports: list[str] = Field(default_factory=list)
    missing_dependencies: list[str] = Field(default_factory=list)
    accepts_seed_argument: bool | None = Field(
        default=None, description="does the repo actually take the --seed this harness would pass?"
    )
    declared_platform: str = Field(default="", description="platform the repo's env file declares")
    current_platform: str = Field(
        default="",
        description="the platform the run would actually SEE. Equal to sys.platform under the "
                    "local backend, but it is the backend's answer rather than this process's: a "
                    "Linux container reports 'linux', which is what makes a linux-64 repository "
                    "capable there without changing any of the logic that decides capability.",
    )
    backend: str = Field(default="", description="the execution backend that answered these checks")


# --------------------------------------------------------------------------- #
# Comparison — WHAT THE RESULT WOULD BE COMPARED AGAINST
# --------------------------------------------------------------------------- #
# Identity asks whether the right program ran. This asks what its output is then held up
# against, and until now there was only ever one answer: a measured number versus a
# quantity the paper printed. That is the right comparison for "is 61.4 the number their
# code produces" and it is not a comparison at all for "is the gain attributable to the
# augmentation or to the loss weight", which holds one ARM against another and where the
# paper printed neither side.
#
# Because `local_exec.reconcile` could only do the first, `discovery._routes` offered an
# executable route only where a printed quantity had been parsed — so an attribution, a
# control-presence or a protocol-conformance question could reach no route at all and was
# reported as "no verification route this system has would settle the question". A route
# existed; the comparison at the end of it did not. `harness.comparison` names which.
COMPARISON_KINDS = (
    "AGAINST_PRINTED_VALUE",   # a measured quantity vs the one the paper printed
    "BETWEEN_ARMS",            # one measured arm vs another measured arm
    "AGAINST_EXISTENCE",       # something the claim requires is present, or it is not
    "AGAINST_SPECIFICATION",   # an observed procedure vs the one the paper specifies
)

# Four different facts about whether the comparison could be carried out, and not one of
# them is a finding about the paper. `unsupported` in particular is a limit of this
# system's method inventory — the same distinction `NO_ROUTE_AVAILABLE` draws one layer up.
COMPARISON_STATES = (
    "established",        # the comparison can be performed
    "no_reference",       # the paper printed nothing at this address to compare against
    "arms_unspecified",   # a second arm would be needed and none was built
    "unsupported",        # this system has no arithmetic for this kind of comparison
    "unmapped",           # not assessed, or the route produces nothing to compare
)


class Comparison(_Base):
    """What one target's result would be held against, and whether that is possible.

    Derived by `harness.comparison` from the ROUTE, never from the question and never
    from anything a model wrote. Keeping it a property of the route is what stops "this
    is an attribution question" from becoming "so its number may be reconciled against a
    printed cell": the route decides what is produced, and what is produced decides what
    it can be compared with.
    """

    kind: str = Field(default="", description=" | ".join(COMPARISON_KINDS))
    state: str = Field(default="unmapped", description=" | ".join(COMPARISON_STATES))
    measured: str = Field(default="", description="what the route would produce")
    reference: str = Field(default="", description="what it would be compared against")
    reason: str = Field(default="", description="why it cannot be carried out, when it cannot")

    @property
    def established(self) -> bool:
        return self.state == "established"


# --------------------------------------------------------------------------- #
# Resources — DOES THE PUBLISHED EXPERIMENT FIT, decided before anything runs
# --------------------------------------------------------------------------- #
# Capability asks "can this machine run the code". Identity asks "is this the right
# program". Neither asks "does the experiment the paper published fit in the hardware
# present" — and that question cannot be answered by trying, because trying produces a
# CUDA OOM some minutes in, which looks exactly like the authors' code failing.
#
# The temptation this type exists to remove is the obvious one: shrink the batch, drop to
# int8, cut the sequence length, run fewer seeds, use the smaller model — and the run
# completes. What completed is a different experiment, and reporting its number against
# the paper's cell would be the most damaging thing this harness could do, because unlike
# a crash it produces a confident figure with nothing wrong on its face.
#
# So resources are a PRE-execution precondition with categorical states, and there is
# deliberately no knob anywhere that adapts a requirement downward to fit a backend.
RESOURCE_STATES = (
    "satisfied",     # every established requirement fits, with evidence for each
    "insufficient",  # at least one requirement exceeds what the backend offers
    "unknown",       # the demand could not be established from paper or repository
    "unassessed",    # not yet examined
)

GIB = 1024 ** 3


class ResourceEvidence(_Base):
    """One quoted justification for a resource requirement, re-checkable like S2 evidence.

    A requirement without a quote is a guess, and a guess that BLOCKS is as unaccountable
    as a guess that permits — a reader has to be able to see why the harness decided an
    experiment did not fit, and disagree with it.
    """

    quote: str = Field(default="", description="verbatim from the paper or the repository")
    source_ref: str = Field(default="", description="'p7' | 'T2:r3:c11' | 'scripts/x.sh:12'")
    kind: str = Field(
        default="",
        description="declared_requirement = the authors state what the method costs; "
                    "declared_hardware = the authors state what they ran on, an upper bound "
                    "on the machine and not a statement of need; "
                    "derived_floor = computed from a model scale the cell itself names",
    )
    note: str = ""


class ResourceRequirement(_Base):
    """What the CITED experiment demands, in bytes and counts, each with its evidence.

    Every field is optional because most papers state few of them. An unstated demand is
    `None` and is reported as unknown — never as zero, which would read as "needs
    nothing" and would authorize execution on the strength of the paper's silence.
    """

    vram_bytes: int | None = None
    ram_bytes: int | None = None
    disk_bytes: int | None = None
    cpu_count: int | None = None
    gpu_count: int | None = None
    gpu_model: str = Field(default="", description="the accelerator the paper names, e.g. 'A100'")
    walltime_s: int | None = Field(default=None, description="declared runtime / execution budget")
    model_scale: str = Field(default="", description="the model the cited cell reports, e.g. 'LLaMA 2 7B'")
    vram_is_floor_only: bool = Field(
        default=False,
        description="`vram_bytes` came ONLY from the fp16 weight lower bound, with no "
                    "declared cost to anchor it. A lower bound is a hard minimum, not a "
                    "requirement: it counts the weights and nothing else — no activations, "
                    "no optimizer state, no gradients, no KV cache. `assess_resources` must "
                    "not report `satisfied` on the strength of a floor alone.",
    )
    evidence: list[ResourceEvidence] = Field(default_factory=list)
    unstated: list[str] = Field(
        default_factory=list, description="requirement fields the sources did not establish")

    @property
    def stated(self) -> bool:
        """Did any source establish any demand at all?"""
        return any(v is not None for v in (self.vram_bytes, self.ram_bytes, self.disk_bytes,
                                           self.cpu_count, self.gpu_count, self.walltime_s))

    @property
    def memory_stated(self) -> bool:
        """Is the demand that actually decides whether an experiment fits established?

        `stated` is an OR over six fields, and it was the only thing gating both backend
        selection and the resource check. So a paper that stated nothing but "trained for
        60 hours" — SAPG does exactly this — produced `stated=True`, and every unstated
        field then matched vacuously because `need is None` reads as "fits". A requirement
        naming only `cpu_count=2` reached `resources: satisfied` and `authorize: allowed`
        on a machine whose VRAM had never been compared with anything.

        Memory is singled out because it is the quantity the whole check exists for:
        `evaluate.py --seed 0` launches happily on an 8 GiB card and dies loading a 7B
        checkpoint, twenty minutes in, with a stderr indistinguishable from broken code.
        Disk and CPU shortfalls announce themselves; an unstated memory demand does not.

        Either figure counts. A CPU-only experiment's binding constraint is host RAM, and
        demanding a VRAM number from it would block runs that fit.
        """
        return self.vram_bytes is not None or self.ram_bytes is not None


class ResourceCapability(_Base):
    """Whether the backend satisfies the requirement. `satisfied` is the only green light.

    `unknown` blocks, and that is the deliberate half. The alternative reading — "nothing
    was established, so nothing is in the way" — is exactly the inference the docstring on
    ResourceRequirement refuses: it converts a paper's silence about its own cost into
    permission to run it. The asymmetry is the same one that governs the rest of S3: a
    wrongly blocked run is INCONCLUSIVE and accuses nobody, while a wrongly permitted one
    OOMs and reconciles as the authors' code failing.
    """

    state: str = Field(default="unassessed", description=" | ".join(RESOURCE_STATES))
    reason: str = ""
    backend: str = ""
    requirement: ResourceRequirement | None = None
    shortfalls: list[str] = Field(
        default_factory=list, description="one line per requirement the backend cannot meet")
    available_vram_bytes: int | None = None
    available_ram_bytes: int | None = None
    available_disk_bytes: int | None = None
    available_cpu_count: int | None = None
    available_gpu_count: int | None = None
    available_gpu_model: str = ""

    @property
    def established(self) -> bool:
        return self.state == "satisfied"


# --------------------------------------------------------------------------- #
# Commit identity — the code that runs must be the code that was audited
# --------------------------------------------------------------------------- #
COMMIT_STATES = (
    "verified",    # HEAD equals the audited commit and the tree is clean
    "mismatch",    # HEAD is a different commit — a moving branch, or a re-clone
    "dirty",       # HEAD matches but the working tree has been modified
    "unknown",     # no audited commit recorded, or HEAD could not be read
    "unassessed",
)


class CommitVerification(_Base):
    """That the checkout about to run is the one the static audit and identity layer read.

    Recording a SHA after cloning proves nothing: it says what arrived, not what was
    asked for. A default branch moves, a cached checkout is refreshed, and the next run
    executes different code under the same reasoning — with the audit findings, the
    experiment identity and the reconciliation all still labelled with the old commit.
    """

    state: str = Field(default="unassessed", description=" | ".join(COMMIT_STATES))
    expected: str = Field(default="", description="the audited commit, from the audit artifact")
    actual: str = Field(default="", description="`git rev-parse HEAD` in the checkout now")
    dirty_files: list[str] = Field(default_factory=list)
    shallow: bool = Field(default=False, description="a depth-1 clone cannot reach another commit")
    reason: str = ""

    @property
    def established(self) -> bool:
        return self.state == "verified"


class ExecutionRecord(_Base):
    """One process this harness started, kept whole.

    The evidence a reproduction verdict rests on. Before this existed, `run_probe` wrote
    `{seed, arm, rc, error[-400:]}` for FAILED attempts only — so a successful run left
    no record of what command ran, where, when, or what it printed, and a reviewer asking
    "what actually produced this number" had nothing to read.

    Written for every attempt, success or failure, to `runs/<pid>/execution.jsonl`. Full
    stdout and stderr, untruncated: the whole point is that the parse which produced the
    metric can be re-done by hand from the same bytes.
    """

    seed: int = 0
    arm: str = ""
    backend: str = ""
    argv: list[str] = Field(default_factory=list, description="the command as the backend received it")
    launch_argv: list[str] = Field(
        default_factory=list,
        description="the command line this harness ISSUED, when it differs from the one the "
                    "process received. Empty for a backend that runs argv directly; for a "
                    "container it is the whole docker run line. Without it a containerised "
                    "record names the script but not the image, the mount or the network "
                    "policy, and the isolation a verdict depends on is absent from the "
                    "artifact the verdict must be re-derivable from.")
    cwd: str = Field(default="", description="working directory the process actually ran in")
    interpreter: str = Field(default="", description="the python that ran it, when one did")
    environment: str = Field(
        default="",
        description="the backend's own identification of WHERE this ran — platform, python and "
                    "accelerator, plus whatever session identity a remote backend has. Locally "
                    "this is implicit in the host; remotely it is the only record of the hardware "
                    "a verdict came from, and a reproduction nobody can locate is not evidence.",
    )
    commit: str = Field(default="", description="the audited commit this execution is about, "
                                                "written only when the authors' repository is "
                                                "what ran")
    provenance: str = Field(
        default="",
        description="whose program this record is of, copied from the spec. The execution log "
                    "is the artifact a reproduction verdict must be re-derivable from by hand, "
                    "and it carried no provenance at all while stamping the authors' repository "
                    "commit onto every record - so eighty harness-authored diagnostic runs were "
                    "identified in the durable trace by the authors' SHA and nothing else.")
    started_at: str = ""
    ended_at: str = ""
    seconds: float = 0.0
    launched: bool = Field(default=False, description="the process existed")
    completed: bool = Field(default=False, description="it ran to an exit code")
    timed_out: bool = False
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = Field(default="", description="why it did not launch or did not finish")
    metric: float | None = Field(
        default=None, description="the value parsed from this attempt's output, if any")


class ExecAuthorization(_Base):
    """Whether an execution request may proceed. The record of a refusal, not just a bool.

    Every path that does not execute has to be able to say which condition stopped it,
    because the alternatives are not interchangeable: "the operator did not open the
    gate", "we could not identify which experiment this is" and "their code will not run
    here" are three different sentences and none of them is about the paper being wrong.
    """

    allowed: bool = False
    decision: str = Field(default="gate_closed", description=" | ".join(EXEC_DECISIONS))
    backend: str = Field(default="", description="the backend that would have run it")
    failure_class: str = Field(
        default="execution_unauthorized",
        description="how a refusal is accounted for downstream: " + " | ".join(FAILURE_CLASSES),
    )
    detail: str = Field(default="", description="one sentence naming the condition that decided it")


class Reconciliation(_Base):
    """Executed number vs the table cell the paper printed.

    `status` is decided arithmetically from `delta_error` against the measured noise
    band — never by judgement, for the same reason the RED/GREEN verdict is a
    threshold table.
    """

    table_ref: str = Field(default="", description="the addressed cell 'T<t>:r<r>:c<c>'")
    claim_ref: str = Field(
        default="",
        description="the address actually reconciled against — a cell, or a prose span, or "
                    "another form `harness.claims.resolve` re-derives. Equal to `table_ref` "
                    "when the address is a cell.")
    claim_kind: str = Field(default="", description=" | ".join(REFERENCE_KINDS))
    target_id: str = ""
    finding_id: str = ""
    metric: str = ""
    claimed_value: float | None = Field(default=None, description="parsed from the cited cell")
    claimed_raw: str = Field(default="", description="the cell's contents, verbatim")
    reproduced_value: float | None = Field(default=None, description="mean over the seeds that ran")
    reproduced_std: float | None = None
    seeds_run: list[int] = Field(default_factory=list)
    delta_error: float | None = Field(default=None, description="|reproduced - claimed|")
    claimed_precision: float | None = Field(
        default=None,
        description="half the rounding interval implied by how many digits the paper printed "
                    "for the claimed cell, e.g. 0.05 for '59.3'. Subtracted from delta_error "
                    "before comparing to the noise band, so the same underlying result does not "
                    "flip between RESOLVED_VERIFIED and FAILED_REPRODUCTION solely because the "
                    "paper printed one extra decimal.",
    )
    noise_band: float = Field(default=0.0, description="2 * seed-to-seed sigma")
    status: str = Field(
        default="NOT_ATTEMPTED",
        description="RESOLVED_VERIFIED | FAILED_REPRODUCTION | INCONCLUSIVE | NOT_ATTEMPTED",
    )
    provenance: str = Field(
        default="",
        description="the ProbeSpec.provenance that produced this. Only 'driver', 'repo_exec' and "
                    "'reimpl_exec' may reach a verdict: a synthesized probe is our reimplementation "
                    "on a toy problem, and it is not entitled to convict a paper's printed number.",
    )
    failure_class: str = Field(
        default="none",
        description="how a non-zero exit was accounted for: " + " | ".join(FAILURE_CLASSES) +
                    ". Only 'runtime_failure' — the experiment was reached and then failed — may "
                    "produce FAILED_REPRODUCTION from a crash.",
    )
    experiment_state: str = Field(default="unmapped", description="ExperimentIdentity.state")
    metric_state: str = Field(default="unmapped", description="MetricIdentity.state")
    configuration_state: str = Field(default="unmapped", description="ConfigurationIdentity.state")
    comparison_kind: str = Field(
        default="",
        description="WHICH comparison this reconciliation performed — " +
                    " | ".join(COMPARISON_KINDS) + " — or '' when the layer did not run. "
                    "Recorded because the arithmetic below is the AGAINST_PRINTED_VALUE "
                    "one and nothing in the record used to say so; a reader tracing a "
                    "verdict has to be able to see which of four comparisons produced it.")
    comparison_state: str = Field(
        default="", description="Comparison.state at the time of this reconciliation, or ''")
    reached_experiment: bool | None = Field(
        default=None,
        description="did the process get past setup into the experiment? None means not assessed. "
                    "The burden of proof is on establishing this, not on disproving it.",
    )
    reason: str = ""


class ArmStats(_Base):
    values: list[float] = Field(default_factory=list)
    mean: float = 0.0
    std: float = 0.0
    n: int = 0


class ProbeResult(_Base):
    paper_id: str
    finding_id: str = ""
    claim: str = ""
    device: str = Field(default="unknown", description="cuda / mps / cpu, as the script reported")
    seeds_run: list[int] = Field(default_factory=list)
    seeds_failed: list[int] = Field(default_factory=list)
    arms: dict[str, ArmStats] = Field(default_factory=dict)
    measured_delta: float = Field(default=0.0, description="treatment mean - baseline mean")
    measured_std: float = Field(default=0.0, description="seed-to-seed noise, the hardware floor")
    noise_band: float = Field(default=0.0, description="2 * measured_std")
    is_overclaimed: bool | None = Field(
        default=None, description="measured delta sits inside the 2-sigma noise band"
    )
    calibration: bool | None = Field(
        default=None,
        description="no paper-specific script ran: the arms were identical, so this measured "
                    "the machine's seed-noise floor and says nothing about the paper. None means "
                    "the result predates this field — the renderer falls back to inference.",
    )
    claimed_delta: float | None = Field(
        default=None,
        description="the delta the paper claims, only ever set from a grounded source "
                    "(driver-supplied in spec.json, or an addressed cell's QuantFinding.delta)",
    )
    claim_within_noise: bool | None = Field(
        default=None, description="the PAPER's claimed delta sits inside the noise band"
    )
    verdict: str = Field(
        default="", description="detectable | within_noise | calibration | degenerate | failed"
    )
    reason: str = ""
    seconds: float = 0.0
    script_path: str = ""
    repo: RepoAcquisition | None = Field(
        default=None, description="S3a — where the code came from, if acquisition ran"
    )
    code_audit: CodeAudit | None = Field(
        default=None, description="S3b — static inspection of that code, if it ran"
    )
    reconciliation: Reconciliation | None = Field(
        default=None, description="S3d — executed metric vs the cited table cell, if it ran"
    )
    provenance: str = Field(
        default="template", description="mirrors ProbeSpec.provenance — who authored the code that ran"
    )
    mechanism: str = Field(default="", description="the planner template that authored the script")
    rationale: str = Field(default="", description="why this probe was chosen")
    capability: ExecCapability | None = Field(
        default=None, description="mirrors ProbeSpec.capability — S3's execution precondition"
    )
    backend: str = Field(default="local", description="the backend that ran, or would have run, this probe")
    execution_log: str = Field(
        default="",
        description="path to runs/<pid>/execution.jsonl — one ExecutionRecord per attempt, "
                    "with full stdout/stderr. Empty when nothing was executed.")
    executions: int = Field(default=0, description="how many processes were started")
    backend_selection: str = Field(
        default="", description="mirrors ProbeSpec.backend_selection — why this environment, "
                                "or why none")
    backend_considered: list[str] = Field(
        default_factory=list, description="mirrors ProbeSpec.backend_considered")
    commit_state: str = Field(
        default="unassessed",
        description="mirrors ProbeSpec.commit_state — the checkout's state at PLANNING time. "
                    "Distinct from `commit_verification`, which is the fresh check made at "
                    "execution; a probe that never executes has the first and not the second.")
    resources: ResourceCapability | None = Field(
        default=None, description="mirrors ProbeSpec.resources — E1's pre-execution fit check")
    commit_verification: CommitVerification | None = Field(
        default=None,
        description="E2 — the checkout's identity CHECKED AT EXECUTION TIME, not at planning. "
                    "A stored SHA proves what arrived once; this proves what would run now.",
    )
    authorization: ExecAuthorization | None = Field(
        default=None,
        description="whether execution was permitted, and which condition decided it. Present "
                    "even when the answer was yes, so a report never has to infer a refusal from "
                    "the absence of numbers.",
    )
    experiment: ExperimentIdentity | None = None
    metric_identity: MetricIdentity | None = None
    configuration: ConfigurationIdentity | None = None
    aux: dict[str, dict[str, ArmStats]] = Field(
        default_factory=dict,
        description="secondary measurements from SH_AUX lines, as aux[key][arm]. These answer the "
                    "'and did it break anything else?' question a single headline metric cannot.",
    )


# --------------------------------------------------------------------------- #
# ④ REPORT (S4)
# --------------------------------------------------------------------------- #
class ScientificFinding(_Base):
    """One finding as a REVIEWER reads it: what kind of issue, and what became of it.

    A projection, not a new judgement. Every field is copied from a `Finding`, the
    `ReviewQuestion` it raised, or the `TargetOutcome` that pursued it — `stages/report`
    assembles it and decides nothing. It exists because the primary output of this system
    is a set of scientific findings with resolution states, and reading that off three
    joined artifacts at render time is how a report starts to disagree with its own ledger.

    The three axes are three fields here for the same reason they are three axes
    everywhere else: `scientific_class` says what kind of problem it is,
    `resolution_status` whether it was settled, `evidence_state` what the route produced.
    A single label would destroy two of them, and a colour destroys all three.
    """

    finding_id: str = ""
    scientific_class: str = Field(default="UNRESOLVED_QUESTION", description=" | ".join(SCIENTIFIC_CLASSES))
    title: str = ""
    statement: str = ""
    lens: str = Field(default="", description="who raised it")
    corroborating_lenses: list[str] = Field(
        default_factory=list,
        description="other lenses that reached the same question at the same address. "
                    "Independent agreement, recorded — it raises no severity anywhere.")
    severity: str = Field(
        default="",
        description="the severity `overall_verdict` counts, after every cap. With grading "
                    "off this IS the lens's own asserted severity — `stages.report.counted` "
                    "falls back to it, which is what makes turning grading off reproduce "
                    "the pre-grading decision exactly. It is not independently verified "
                    "unless `--auto-grade` ran.")
    confidence: str = Field(default="", description="the lens's own, bounded by its evidence")
    claim_ref: str = Field(default="", description="the address in the paper")
    evidence_quote: str = ""
    evidence_class: str = Field(default="", description="how the quotation re-verified")

    # --- the question this finding raises, and what became of it ----------------------
    question_id: str = ""
    question: str = Field(default="", description="the question, as a question")
    why_material: str = ""
    evidence_needed: str = Field(default="", description="what would settle it")
    resolution_status: str = Field(default="NOT_INVESTIGATED", description=" | ".join(RESOLUTION_STATES))
    evidence_state: str = Field(default="NOT_INVESTIGATED", description=" | ".join(EVIDENCE_STATES))
    experiment_necessity: str = Field(default="NO_EXPERIMENT_NEEDED", description=" | ".join(EXPERIMENT_NECESSITY))
    evidence_refs: list[str] = Field(default_factory=list)
    conclusion: str = Field(default="", description="what follows scientifically, if anything")


class ReviewOutcome(_Base):
    """The four rows a human reviewer reads first — see `harness/outcome.py`.

    Four independent folds over disjoint inputs, so that "what did this review establish
    about the paper" and "what happened when we tried to run something" can never be the
    same sentence. Every field is derived from a decision an earlier stage already made:
    `finding_state` from `claim_status`, which is where the provenance ceiling was applied;
    `execution_state` from each `TargetOutcome`'s own derived `evidence_state`, which is
    where it was applied a second time. Nothing here is stored by a stage and read back by
    another, and no model writes any of it.

    `triage` and `claim_status` are CARRIED, not decided. They exist on this object so the
    block a reader sees and the threshold table that produced the colour cannot disagree.
    """

    paper_id: str = ""
    execution_actor: str = Field(
        default="",
        description="whose program produced the evidence the execution row describes, in "
                    "the reader's words, derived from provenance alone. The two execution "
                    "states that say anything about the paper used to name the authors' "
                    "code unconditionally, while being reachable from all three admissible "
                    "provenances - so a reimplementation that disagreed with a paper "
                    "accused the authors' code of producing the disagreement.")
    finding_state: str = Field(
        default="NO_CONCERN_SURVIVED_VERIFICATION",
        description="MATERIAL_FAILURE_ESTABLISHED | CONCERNS_RECORDED | "
                    "NO_CONCERN_SURVIVED_VERIFICATION — what this review established about "
                    "the paper's science. A function of `claim_status` and the kept findings "
                    "and of nothing about execution, so a blocked, refused or inadmissible "
                    "run cannot move it.")
    finding_detail: str = ""
    question_state: str = Field(
        default="NO_QUESTION_RAISED",
        description="ALL_QUESTIONS_SETTLED | SOME_QUESTIONS_SETTLED | NO_QUESTION_SETTLED | "
                    "NO_QUESTION_RAISED — what became of the questions the review raised.")
    question_detail: str = ""
    execution_state: str = Field(
        default="NO_EXECUTION_WARRANTED",
        description="EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY | "
                    "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY | "
                    "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE | "
                    "EXECUTION_BLOCKED_BEFORE_IT_STARTED | "
                    "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED | NO_EXECUTION_WARRANTED. Only the "
                    "two that name a printed quantity say anything about the paper; the other "
                    "four are facts about a plan, an artifact, a host or a gate.")
    execution_detail: str = Field(
        default="",
        description="THE EXACT REASON, carried verbatim from the target outcome. An execution "
                    "that settled nothing must be able to say why in the same breath, or "
                    "'did not produce admissible evidence' becomes another opaque token.")
    scope_state: str = Field(
        default="NO_TARGET_PURSUED",
        description="CENTRAL_CLAIMS_LEFT_UNCHECKED | SOME_TARGETS_PURSUED | NO_TARGET_PURSUED — "
                    "how much of the paper the three rows above are an assessment of.")
    scope_detail: str = ""
    claim_status: str = Field(default="NOT_VERIFIED", description="carried, not decided here")
    triage: str = Field(default="GREEN", description="carried, not decided here; routing only")


class EvalReport(_Base):
    paper_id: str
    title: str = ""
    verdict: str = Field(default="", description="RED | GREEN — the binary paper-level decision")
    verdict_reason: str = Field(default="", description="the deterministic rule that produced the verdict")
    triage: str = Field(
        default="",
        description="RED | YELLOW | GREEN — the REVIEW-level decision a first-pass reviewer "
                    "hands to a human. A strict projection of `verdict`: triage RED is binary "
                    "RED, unchanged and unreachable by accumulation, and the split is inside "
                    "the old GREEN. YELLOW means a verified MAJOR concern or a central claim "
                    "this review could address and did not settle — neither of which is an "
                    "accusation, and neither of which is 'checked and clean'. See "
                    "`stages/report.triage`.",
    )
    triage_reason: str = Field(default="", description="the deterministic fold that produced `triage`")
    targets_summary: dict = Field(
        default_factory=dict,
        description="how many targets ended in each disposition. A paper now has a SET of "
                    "targets and they end differently; a single reproduction status cannot "
                    "carry 'one blocked, one reproduced, one failed'.",
    )
    review_efficiency: dict = Field(
        default_factory=dict,
        description="`CaseLedger.efficiency`, carried here so the conditional-escalation claim "
                    "is checkable from the report alone: questions generated, how many closed "
                    "without running anything, how many escalated, which gate stopped the rest.",
    )
    reviewer_report_path: str = Field(
        default="", description="the concise 1-2 page report a human actually reads")
    ledger_path: str = Field(default="", description="the machine-readable evidence trace")
    claim_status: str = Field(
        default="",
        description="VERIFIED_FAILURE | VERIFIED_SUPPORT | NOT_VERIFIED — the epistemic state the "
                    "binary verdict projects from. RED iff VERIFIED_FAILURE; BOTH other states are "
                    "GREEN, because 'checked and held' and 'could not check' are the same DECISION "
                    "about the paper while being opposite states of knowledge. Kept as its own "
                    "field so a report can never let them look alike.",
    )
    reproduction_status: str = Field(
        default="",
        description="REPRODUCED | FAILED_REPRODUCTION | NOT_VERIFIED | NOT_ATTEMPTED — how much "
                    "experimental evidence stands behind the verdict, reported beside it and never "
                    "folded into it.",
    )
    execution_provenance: str = Field(
        default="",
        description="AUTHOR_REPOSITORY | INDEPENDENT_REIMPLEMENTATION | SYNTHESIZED_DIAGNOSTIC — "
                    "what actually ran, in the reader's vocabulary. Fails closed to "
                    "SYNTHESIZED_DIAGNOSTIC so nothing unrecognised is ever reported as the "
                    "authors' own code.",
    )
    disposition: str = Field(
        default="NOT_REVIEWED",
        description="WHAT HAPPENS TO THE PAPER: " + " | ".join(PAPER_DISPOSITIONS) + ". "
                    "Folded by `harness.disposition.derive` and carried here, never decided "
                    "here. This is NOT a fifth verdict and does not replace the colour: "
                    "`triage` sorts a queue, `verdict` is the binary claim decision, "
                    "`claim_status` is the epistemic state underneath it, and this is the "
                    "ACTION. STOP_MATERIAL_FAILURE is exactly claim_status == "
                    "VERIFIED_FAILURE, asserted as an identity so the two cannot disagree.",
    )
    disposition_basis: str = Field(
        default="NONE",
        description="ON WHAT a material failure was established: " + " | ".join(DISPOSITION_BASIS)
                    + ". NONE for every disposition that is not a stop. Kept apart from the "
                    "disposition because 'four lenses agreed and the blinded grader "
                    "sustained it' and 'the authors' own code did not produce the number' "
                    "are different things to hand a referee.",
    )
    disposition_reason: str = Field(
        default="", description="the deterministic rule that produced `disposition`")
    findings: list[Finding] = Field(default_factory=list)
    scientific_findings: list[ScientificFinding] = Field(
        default_factory=list,
        description="the PRIMARY output: every kept finding as a scientific category with a "
                    "resolution state, joined to the question it raised. `findings` above is "
                    "the raw lens-level record and stays exactly as it was; this is what the "
                    "reviewer report and the evaluation layer read.")
    unasked_question: str = ""
    n_pages: int = 0
    n_sections: int = 0
    n_tables: int = 0
    n_numbers: int = 0
    lenses_run: list[str] = Field(default_factory=list)
    dropped_findings: int = Field(
        default=0, description="findings discarded because their evidence could not be substantiated"
    )
    verdict_if_cell_backed_only: str = Field(
        default="",
        description="the SAME threshold table applied to only those findings whose evidence is a "
                    "verified table cell. Not a second opinion and not a grader — the identical "
                    "rule over a subset of the identical findings. Where it differs from `verdict`, "
                    "the verdict depends on grades resting on prose, which is the one link in the "
                    "chain the harness cannot check.",
    )
    probe: ProbeResult | None = Field(default=None, description="local reproduction result, if S3 ran")
    experimental_chain: "ExperimentalChain | None" = Field(
        default=None,
        description="the link from a finding to what execution did or did not establish about it",
    )
    verdict_if_lens_severity_only: str = Field(
        default="",
        description="the SAME threshold table with `counted_severity` erased — the verdict grading "
                    "would have produced no effect on. Mirrors `verdict_if_cell_backed_only`.",
    )
    grade_coverage: dict = Field(
        default_factory=dict,
        description="{candidates, graded, pending} from `stages.grade.coverage` — visible even "
                    "when it is all zero, so a reader can tell 'not graded' from 'graded clean'.",
    )
    substantive_verdict: "SubstantiveVerdict | None" = Field(
        default=None,
        description="a model's whole-paper opinion, in the reviewer spec's own vocabulary. "
                    "Printed, never counted — no threshold reads this field.",
    )
    verdict_agreement: str = Field(
        default="", description="agree | harness_harsher | model_harsher | contested | "
                                "unavailable — HARNESS-COMPUTED from `verdict` vs `substantive_verdict`")
    verdict_contested: bool = Field(
        default=False,
        description="true when the model's substantive read is CENTRAL_CLAIM_NOT_ESTABLISHED "
                    "while the deterministic table says GREEN. The one consequence of the "
                    "dissent: it demands human attention (a contested banner, `run.py` exit 3), "
                    "never a change of color.",
    )
    claim_status_reason: str = Field(
        default="",
        description="the deterministic rule that produced `claim_status`. Discarded at the "
                    "call site until the reader-facing outcome block needed to state it: "
                    "'nothing was established' and 'a reproduction failed' are the same "
                    "field with opposite reasons.")
    artifact_state: str = Field(
        default="",
        description="HARNESS-WRITTEN: NO_ARTIFACT_ADVERTISED | ARTIFACT_NOT_FETCHED | "
                    "ARTIFACT_UNOBTAINABLE | ARTIFACT_PRESENT_UNUSABLE | "
                    "ARTIFACT_PRESENT_USABLE | ARTIFACT_UNASSESSED. Derived by "
                    "`taxonomy.artifact_state`; only the first is a fact about the paper. "
                    "`execution_provenance` read SYNTHESIZED_DIAGNOSTIC for four of these, "
                    "so the review could not say whether the authors published anything.")
    review_path: str = Field(
        default="",
        description="HARNESS-WRITTEN: PAPER_ONLY | PAPER_AND_ARTIFACT. Which of the two "
                    "review paths this paper was on, which decides what evidence was "
                    "reachable: only the artifact path can produce an admissible "
                    "reproduction, and only the paper-only path reaches the governed "
                    "reconstruction route.")
    coverage: "CoverageReport | None" = Field(
        default=None,
        description="HARNESS-WRITTEN: structural review-surface coverage, with the paper's own "
                    "addressable surface as the denominator. NOT issue recall — see "
                    "`harness/coverage.py` and `CoverageReport.semantic_coverage`.")
    document_observations: list["DocumentObservation"] = Field(
        default_factory=list,
        description="HARNESS-WRITTEN: mechanically determined document-level facts. Its own "
                    "list, never `findings`, and it gates nothing — see "
                    "`harness/docintegrity.py`. Each says whether it is about the PAPER or "
                    "about this harness's EXTRACTION.")
    guarantees: "ReviewGuarantees | None" = Field(
        default=None,
        description="HARNESS-WRITTEN: what this review guarantees and what it explicitly does "
                    "not, with the artifact that establishes each. See `harness/guarantees.py`.")
    outcome: "ReviewOutcome | None" = Field(
        default=None,
        description="HARNESS-WRITTEN: the four rows a reviewer reads first — what was "
                    "established, what was settled, what execution produced, how much was "
                    "looked at. A pure projection of fields decided upstream; see "
                    "`harness/outcome.py` for why the four are folded separately.",
    )
    n_figures: int = Field(default=0, description="figure captions extracted (citable as F<n>)")
    n_equations: int = Field(default=0, description="display equations extracted (citable as E<n>)")
    self_audit: "ReviewSelfAudit | None" = Field(
        default=None,
        description="HARNESS-WRITTEN: did this review exercise the discipline it claims? See "
                    "`harness/selfaudit.py`. Consumed by no threshold — a failed check refuses "
                    "to let the review call itself complete, it does not change the verdict.",
    )


SUBSTANTIVE_VERDICTS = ("STRONG", "SOUND_WITH_MINOR_CONCERNS", "SUBSTANTIAL_CONCERNS",
                        "CENTRAL_CLAIM_NOT_ESTABLISHED", "INCONCLUSIVE")


class SubstantiveVerdict(_Base):
    """One model's whole-paper opinion — see `EvalReport.substantive_verdict`. Every
    field here is a model's claim; none of it is harness-written, because none of it is
    evidence — it is printed as an opinion, labelled as one, consumed by no threshold.

    The fields below the first three are the reviewer spec's own whole-paper questions,
    answered one at a time rather than compressed into a single sentence. That shape is
    the point: "what is the real contribution", "what most threatens it" and "are the
    weaknesses local or systemic" are separate judgements, and a reader who disagrees with
    the overall verdict needs to see which of them they disagree with.
    """

    verdict: str = Field(default="", description=" | ".join(SUBSTANTIVE_VERDICTS))
    reason: str = ""
    strongest_contribution: str = ""
    weakest_link: str = ""
    weaknesses_are: str = Field(default="", description="LOCAL | SYSTEMIC | MIXED")
    real_contribution: str = Field(
        default="", description="what the paper actually contributes, in the reviewer's own words")
    strongest_support: str = Field(default="", description="the evidence that most supports it")
    strongest_threat: str = Field(default="", description="the evidence that most threatens it")
    claims_well_supported: list[str] = Field(
        default_factory=list, description="claims that stand on the evidence as presented")
    claims_needing_qualification: list[str] = Field(
        default_factory=list, description="claims that hold only in a narrower form")
    core_contribution_stands: str = Field(
        default="", description="YES | YES_QUALIFIED | NO | UNDETERMINED — the whole-paper "
                                "answer, reasoned rather than counted. Read as an opinion: no "
                                "threshold consumes it.")


class SelfAuditItem(_Base):
    """One line of the reviewer self-audit — see `harness/selfaudit.py`.

    Harness-written throughout. `state` is `pass | fail | not_applicable`, and
    `not_applicable` is a real answer rather than a hedge: a paper with no serious
    findings has no serious findings to have steelmanned, and reporting that as a pass
    would claim diligence that was never exercised.
    """

    key: str = ""
    question: str = ""
    state: str = Field(default="not_applicable", description="pass | fail | not_applicable")
    detail: str = ""
    offenders: list[str] = Field(
        default_factory=list, description="the findings responsible, named so the gap is actionable")
    n_offenders: int = 0
    n_in_scope: int = 0


class ReviewSelfAudit(_Base):
    """Did this review do the work it claims to have done? See `harness/selfaudit.py`.

    `complete` being False does NOT change the verdict — the threshold table is the
    verdict and nothing here is allowed a second path to a color. What it changes is
    whether the review may present itself as finished.
    """

    items: list[SelfAuditItem] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list, description="keys of the unmet checks")
    complete: bool = False
    summary: str = ""


# --------------------------------------------------------------------------- #
# Corpus accounting — every requested paper, in exactly one terminal state
# --------------------------------------------------------------------------- #
# `requested` is not a synonym for "queued": it means a paper was asked for and NO case
# exists for it, which is a harness defect and the state this vocabulary exists to make
# unmissable. `started` likewise means the loop stopped mid-flight without recording why.
# Neither may hide inside `failed`, because the operator's next action differs.
CORPUS_STATES = ("requested", "started", "completed", "failed", "inconclusive")


class CorpusEntry(_Base):
    """One requested paper's terminal state. Keyed on the REQUEST, not on `paper_id`:
    two different files can slugify to the same id, and a `paper_id`-keyed summary
    silently collapses them into one row."""

    source: str = Field(default="", description="exactly what the caller asked for")
    paper_id: str = ""
    state: str = Field(default="requested", description=" | ".join(CORPUS_STATES))
    reason: str = ""
    verdict: str = ""
    phase: str = ""
    reproduction_class: str = ""
    failure_kind: str = Field(default="", description=" | ".join(FAILURE_KINDS))
    resume_after: str = ""
    report_path: str = ""
    disposition: str = Field(
        default="NOT_REVIEWED",
        description="WHAT HAPPENS TO THIS PAPER: " + " | ".join(PAPER_DISPOSITIONS) + ". "
                    "Distinct from `state`, which says how the RUN ended: a paper can be "
                    "`completed` and still be STOP_MATERIAL_FAILURE or BLOCKED_ARTIFACT, "
                    "and until this field existed both mapped to `completed` with the "
                    "difference visible only inside the report.")
    disposition_basis: str = Field(
        default="NONE", description=" | ".join(DISPOSITION_BASIS))


class CorpusReport(_Base):
    """The batch, request by request — see `harness/corpus.py` for the conservation law
    (`sum(counts.values()) == requested`) that is asserted rather than assumed."""

    requested: int = 0
    entries: list[CorpusEntry] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    by_state: dict[str, list[str]] = Field(default_factory=dict)
    complete: bool = Field(default=False, description="every requested paper reached `completed`")
    summary: str = ""


# --------------------------------------------------------------------------- #
# ⑦ REVIEW-SURFACE COVERAGE — structural, never issue recall
# --------------------------------------------------------------------------- #
# The measure this project is entitled to make. There is no adjudicated ground truth for
# any paper in the corpus, so precision and recall over "real issues" are not computable
# and are not computed (`harness/evaluation.LIMITATIONS` says so in the artifact). What IS
# computable is how much of the paper's own addressable surface a review connected to a
# question, and against WHAT denominator.
#
# The denominator that must never be used is the one this system already published:
# `targets_addressable / targets_discovered` divides the harness's own object list by
# itself, and because an unresolvable reference never becomes an object at all, a WORSE
# extractor scores HIGHER. A denominator that improves when the measurement degrades is
# not a denominator. See `harness/coverage.py`.
SURFACE_KINDS = ("table_cell", "figure", "equation", "section_span", "reported_quantity")


class ReviewSurface(_Base):
    """The paper's own addressable surface, enumerated from `PaperDoc` and nothing else.

    `harness.coverage.surface` takes a `PaperDoc` and NOTHING derived from the review, so
    the numerator cannot redefine the denominator. That is a signature-level guarantee of
    the same kind `grading.derive` makes: the fabrication is inexpressible rather than
    merely absent.

    `content_sha` records WHICH DOCUMENT this was computed over, because a coverage number
    with no document identity can be quoted beside any paper.
    """

    paper_id: str = ""
    content_sha: str = Field(
        default="", description="sha256[:12] of the PDF bytes this surface was computed over")
    addresses: list[str] = Field(
        default_factory=list,
        description="every addressable unit, deduplicated, each re-resolvable by "
                    "`claims.resolve` against the same document")
    by_kind: dict[str, int] = Field(default_factory=dict, description=" | ".join(SURFACE_KINDS))
    table_cells_nonempty: int = 0
    table_cells_total: int = Field(
        default=0, description="every row x column slot, including the empty padding cells "
                               "`pdf.render_tables` skips and the lens therefore never saw")
    sections: int = 0
    prose_chars_total: int = Field(default=0, description="extracted section text, in full")
    prose_chars_presented: int = Field(
        default=0,
        description="how much of it a reading pass actually carried, computed from the same "
                    "`harness.reading.plan` call that builds the prompts. It is the ceiling "
                    "on any recall claim. It used to measure `pdf.render_sections`, which "
                    "divided one budget across every section and hard-sliced each, so a long "
                    "paper was TRUNCATED before a lens read a word of it; the plan tiles the "
                    "paper in bounded parts instead, and `coverage.prose_presented` still "
                    "computes the old number so the difference stays measurable.")
    reading_parts: int = Field(
        default=1,
        description="how many bounded passes the paper was traversed in at this budget. One "
                    "means it fitted whole and no cross-part synthesis was required.")
    surface_empty: bool = Field(
        default=True,
        description="true when extraction recovered no addressable unit at all. A rate over "
                    "an empty surface is None, never 1.0.")


class ReadingRecord(_Base):
    """HOW THE PAPER WAS READ — seven numbers that answer seven different questions.

    They are separate for the reason the four axes are separate: collapsing them is how
    "the lenses read the paper" gets printed about a run in which they read a third of it.

    `extracted_text_fraction` is about EXTRACTION and `reader_visible_fraction` is about
    the READER, and they compose — a page extraction recovered nothing from is invisible
    to a reader however complete the reading plan is. The first has the PDF's own page
    count as its denominator, so unlike a rate computed over recovered objects it cannot
    be raised by a worse extractor.

    `reader_visible_fraction` is the ceiling on any recall claim this system makes, and it
    is the number this work exists to move: `pdf.render_sections` divided one budget
    across every section and hard-sliced each, presenting between 0.33 and 0.84 of the
    extracted prose across the evaluated corpus. `harness.reading` replaces the cut with a
    plan of bounded parts that tile the paper, and `anchor_repeat_fraction` is what that
    costs — the stable packet each part re-carries so a cross-section comparison stays
    available in every pass.

    NONE OF THESE IS ISSUE RECALL. `CoverageReport.semantic_coverage` says so in the same
    artifact, unconditionally, and for the same reason it always did.
    """

    extracted_prose_chars: int = 0
    pages_with_text: int = 0
    pages_total: int = 0
    extracted_text_fraction: float | None = Field(
        default=None, description="pages extraction recovered prose from / pages in the PDF")
    reader_visible_chars: int = 0
    reader_visible_fraction: float | None = Field(
        default=None,
        description="of the extracted prose, what some reading pass actually carried. "
                    "1.0 is the target and None — never 1.0 — when there is no prose.")
    anchor_chars: int = 0
    anchor_repeat_chars: int = 0
    anchor_repeat_fraction: float | None = None
    number_of_parts: int = 1
    lenses_total: int = 0
    lenses_completed: int = 0
    lens_syntheses_required: int = Field(
        default=0, description="one per lens on a paper read in more than one part, zero "
                               "otherwise — a paper read whole has nothing to synthesise across")
    lens_syntheses_completed: int = 0


class CoverageReport(_Base):
    """Two numerators over one denominator, and they are not the same claim.

    `addressed` counts surface units this review minted an address for. `examined` counts
    units where a route was actually PURSUED. Corpus-wide, 629 of 867 targets ended
    NOT_INVESTIGATED, so reporting the first as coverage would claim ~93% of papers where
    nearly three quarters of the targets were never looked at.

    `off_surface` is a DEFECT CHANNEL, not a bucket: an address the review minted that is
    not in the surface means the two were computed over different documents or the surface
    enumeration is incomplete. It is reported rather than silently dropped or counted.
    """

    paper_id: str = ""
    content_sha: str = ""
    surface_size: int = 0
    addressed: int = 0
    examined: int = 0
    off_surface: list[str] = Field(default_factory=list)
    addressed_rate: float | None = None
    examined_rate: float | None = None
    prose_presented_fraction: float | None = Field(
        default=None,
        description="of the extracted prose, what a reading pass actually carried. The same "
                    "measurement as `reading.reader_visible_fraction` and computed from the "
                    "same `harness.reading.plan` call, under the name the frozen evaluation "
                    "artifacts and the manuscript already use — renaming it would silently "
                    "change what a published number meant.")
    reading: ReadingRecord | None = Field(
        default=None,
        description="how this paper was read: parts, coverage, anchor cost, and how many "
                    "lens syntheses were required and completed.")
    by_kind_addressed: dict[str, int] = Field(default_factory=dict)
    # ALWAYS PRESENT, and always this value. Borrowed from the one external repo that got
    # this right: its claim-coverage report carries `semantic_extraction_coverage:
    # "not_machine_detectable"` unconditionally, so a clean report cannot be read as
    # evidence that every substantive claim was found. The same caveat applies here and
    # for the same reason, so it is a field rather than a paragraph someone may not read.
    semantic_coverage: str = Field(
        default="not_machine_detectable",
        description="Structural coverage says nothing about whether the review found the "
                    "issues that matter. That question needs adjudicated ground truth, "
                    "which no paper in this corpus has.")


# --------------------------------------------------------------------------- #
# ⑧ DOCUMENT INTEGRITY — observations, never conclusions
# --------------------------------------------------------------------------- #
INTEGRITY_CHECKS = (
    "CROSSREF_UNRESOLVED",      # the prose cites Figure N and no such object was recovered
    "OBJECT_UNCITED",           # a recovered object nothing in the prose cites
    "LABEL_DUPLICATED",         # two tables both printed "Table 10"
    "LABEL_OUT_OF_ORDER",       # a caption sequence that does not ascend
    "BODY_UNCAPTIONED",         # a recovered body with no paired caption
    "NUMBERING_GAP",            # a label set with a hole in it
    "SECTION_REF_UNRESOLVED",   # "Section 4.2" with no such numbered heading
    "TABLE_ARITHMETIC",         # an average column that does not average its own row
    "PROSE_CELL_MISMATCH",      # the prose states a number the cited cell does not carry
    "CAPTION_LABEL_CONFLICT",   # a caption's printed label disagrees with its neighbours
)

# WHOSE PROPERTY the observation is, and the field is required because getting this wrong
# is the whole risk of the layer. A naive "referenced but missing" check over the shipped
# corpus produced twelve observations of the second kind and none of the first: every
# table and equation it called absent is present in the paper and merely absent from what
# extraction recovered. Reporting those as defects would make a colour a property of
# extraction quality, which is the same failure the removed count-of-findings threshold had.
INTEGRITY_ABOUT = (
    "PAPER",        # a property of the document the authors published
    "EXTRACTION",   # a property of what this harness recovered from it
)


class DocumentObservation(_Base):
    """One mechanically determined document-level fact. NOT a finding, NOT a severity.

    Modelled on `CodeAudit.runtime`'s `RuntimeDemand`, which goes in its own list and
    never in `findings` "because a requirement filed there would be read as an
    accusation", and which gates nothing. The same posture applies here, and more
    strongly: these observations are kept apart from scientific validity unless a
    scientific consequence is DEMONSTRATED, and nothing in this layer demonstrates one.

    DELIBERATELY ABSENT: `severity`, `confidence`, `scientific_class`, `candidate_class`,
    `counted_severity`, `verdict`, `route`. Their absence is asserted by a field-absence
    test, because a field that exists is a field something will eventually read.
    """

    check: str = Field(default="", description=" | ".join(INTEGRITY_CHECKS))
    about: str = Field(description="REQUIRED, no default: " + " | ".join(INTEGRITY_ABOUT))
    ref: str = Field(default="", description="an address `claims.resolve` can re-derive, or ''")
    quote: str = Field(default="", description="verbatim from the parsed document")
    page: int = 0
    detail: str = Field(
        default="",
        description="the reader-facing sentence. An EXTRACTION observation must say so in "
                    "its own words rather than relying on the `about` field being read.")


# --------------------------------------------------------------------------- #
# ⑨ GUARANTEES AND NON-GUARANTEES
# --------------------------------------------------------------------------- #
GUARANTEE_KINDS = (
    "PROCESS",     # something this system enforces mechanically, per review
    "SCIENTIFIC",  # something about the paper. This system makes NONE of these.
)


class Guarantee(_Base):
    """One thing this system does or does not promise, and what makes it checkable.

    Split by KIND because the two are constantly confused: "every evidence pointer was
    re-verified against the parsed paper" is a process guarantee this system enforces and
    can be checked per review, while "every important issue was found" is a scientific
    guarantee it does not make and could not check without adjudicated ground truth.
    A system that lists both under one heading is inviting the second to be read off the
    first.
    """

    kind: str = Field(default="PROCESS", description=" | ".join(GUARANTEE_KINDS))
    statement: str = ""
    holds: bool = Field(
        default=False,
        description="for a PROCESS guarantee, whether it held FOR THIS REVIEW, established "
                    "from an artifact rather than asserted")
    evidence: str = Field(default="", description="the artifact and field that establishes it")


class ReviewGuarantees(_Base):
    """What this review guarantees, what it explicitly does not, and why.

    Non-guarantees are first-class and are printed. The list is closed and paper-agnostic:
    it describes the SYSTEM, so nothing in it may be derived from a paper's identity or
    from how a particular review turned out. Only `Guarantee.holds` and `evidence` are
    per-review, and both are read off artifacts.
    """

    paper_id: str = ""
    guarantees: list[Guarantee] = Field(default_factory=list)
    non_guarantees: list[str] = Field(default_factory=list)
    unmet: list[str] = Field(
        default_factory=list,
        description="PROCESS guarantees that did NOT hold for this review. A non-empty list "
                    "is a defect in this harness, not a finding about the paper.")


class ExperimentalChain(_Base):
    """finding → claim → evidence → identity → repo → commit → execution → reconciliation.

    Assembled at report time from artifacts already on disk. It INVENTS NOTHING: every
    field is copied from a `ProbeResult`, a `RepoAcquisition`, an identity record or a
    `Reconciliation` that some earlier stage wrote and that a reader can open.

    Its purpose is to make an experimental conclusion refutable in the same way a paper
    finding is. A reader who doubts an INCONCLUSIVE can see exactly which link was not
    established; a reader who doubts a FAILED_REPRODUCTION can see the commit, the
    command, the metric binding and the arithmetic that produced it.
    """

    finding_id: str = ""
    claim: str = Field(default="", description="the paper assertion the experiment addresses")
    evidence_refs: list[str] = Field(default_factory=list, description="cells/pages the claim rests on")
    repo_url: str = ""
    commit: str = Field(default="", description="the audited SHA the reasoning is about")
    commit_state: str = Field(default="unassessed", description=" | ".join(COMMIT_STATES))
    experiment_state: str = Field(default="unmapped", description=" | ".join(IDENTITY_STATES))
    metric_state: str = Field(default="unmapped", description=" | ".join(IDENTITY_STATES))
    configuration_state: str = Field(default="unmapped", description=" | ".join(IDENTITY_STATES))
    capability_code: str = Field(default="not_attempted", description=" | ".join(CAPABILITY_CODES))
    resource_state: str = Field(default="unassessed", description=" | ".join(RESOURCE_STATES))
    backend: str = ""
    authorization: str = Field(default="", description="the ExecAuthorization decision")
    provenance: str = Field(default="", description="who wrote the code that ran")
    executed: bool = Field(default=False, description="did any process actually run")
    executions: int = Field(default=0, description="how many processes were started")
    execution_log: str = Field(
        default="", description="runs/<pid>/execution.jsonl — the full record of each one")
    reconciliation: str = Field(default="", description="RESOLVED_VERIFIED | FAILED_REPRODUCTION | INCONCLUSIVE")
    failure_class: str = ""
    broken_link: str = Field(
        default="",
        description="the FIRST link in the chain that was not established, named so a reader "
                    "sees why no reproduction verdict was reached without re-deriving it",
    )


# --------------------------------------------------------------------------- #
# ⑤ CONTROLLER — the autonomous orchestration state machine
# --------------------------------------------------------------------------- #
# The controller exists so the workflow is a program rather than a convention. Before it,
# `review()` ran the deterministic stages, returned "needs_audit", and the process ended;
# resuming depended on an actor outside the repository following prose instructions.
#
# What the controller may and may not do is the whole design. It SEQUENCES and it
# RETRIES; it does not JUDGE. It cannot authorize an execution, choose a backend, set an
# identity state, or write a reconciliation status — every one of those stays in the
# deterministic layer underneath, and the controller only records the answer it got.
# --------------------------------------------------------------------------- #
# ③b QUESTIONS, TARGETS, PLANS, LEDGER (S3a — the review's own reasoning state)
# --------------------------------------------------------------------------- #
# What a discovery pass may find. These are the OBJECTS a first-round reviewer argues
# about, and the reason the list is closed is the same reason every other vocabulary here
# is closed: an open one lets a model name its own category and thereby escape whatever
# rule is keyed on the category.
DISCOVERY_KINDS = (
    "SCIENTIFIC_CLAIM",           # an assertion the paper makes about the world
    "EXPERIMENTAL_RESULT",        # a printed number, in a table or in prose
    "BASELINE_COMPARISON",        # a comparison against another method
    "ABLATION",                   # a component removed to attribute the gain
    "CONTROL",                    # a condition that isolates an explanation
    "DATASET_RESULT",             # a result on one named dataset
    "ERROR_ANALYSIS",             # a qualitative or per-class breakdown
    "IMPLEMENTATION_CLAIM",       # a claim about the released artifact
    "REPRODUCTION_TARGET",        # a printed quantity an execution could re-derive
    "UNANSWERED_REVIEW_QUESTION", # a question the paper leaves open
)

# How much the paper's central conclusion rests on this object. Set by the harness from
# structural evidence (is it in the abstract? is it the compared-against number? does a
# finding attack it?), NOT read from a model — a model that could declare its own target
# central could raise the priority of whatever it happened to find first.
CENTRALITY = ("CENTRAL", "SUPPORTING", "PERIPHERAL", "UNASSESSED")

# WHICH of `harness_addressable`'s three requirements failed. The conjunction is one
# boolean and the three failures are three different facts about three different things:
# the first is a limit of this harness's extraction, the second of the paper's reporting,
# the third of this harness's method inventory. Collapsing them told 52 of 52 corpus
# targets that their address could not be built while printing the address.
ADDRESSING_BLOCKERS = (
    "NONE",                # addressable
    "ADDRESS_UNRESOLVED",  # no address in the parsed paper the harness can re-derive
    "QUANTITY_UNPARSED",   # the kind needs a printed quantity and none parsed unambiguously
    "NO_ROUTE",            # addressed and quantified; no route this system has applies
)

# The ways a question can be answered, cheapest first. The ORDER is load-bearing:
# `harness.planner` walks it and stops at the first admissible route, which is what makes
# "escalate only when justified" a property of the code rather than an instruction.
VERIFICATION_ROUTES = (
    "PAPER_INTERNAL_CHECK",           # the paper contradicts itself; no execution needed
    "ARITHMETIC_RECHECK",             # the printed composition does not evaluate
    "ARTIFACT_INSPECTION",            # static reading of the released repository
    "AUTHOR_CODE_EXECUTION",          # run the authors' own checkout
    "INDEPENDENT_RECONSTRUCTION",     # rebuild the method from the paper's specification
    "FOCUSED_VALIDATION_EXPERIMENT",  # a new experiment that discriminates explanations
    "LITERATURE_SEARCH",              # prior-art / novelty, when the tooling exists
    "NONE",                           # scientifically relevant, no legitimate route
)

# What became of one applicable (review question, evidence route) pair.  This is a
# separate vocabulary from TARGET_DISPOSITIONS: a target is one executable/checkable
# object, while a route attempt answers whether the review exhausted one way of answering
# the question.  In particular, COMPLETED_INCONCLUSIVE records work which finished but did
# not settle the scientific question (the canonical case is PAPER_INTERNAL_CHECK merely
# re-verifying a quotation).
ROUTE_ATTEMPT_STATES = (
    "DISCHARGED_RAN",
    "DISCHARGED_COMPLETED",
    "DISCHARGED_BLOCKED",
    "COMPLETED_INCONCLUSIVE",
    "GATE_CLOSED",
    "DEFERRED_BUDGET",
    "DEFERRED_POLICY",
    "NOT_TRIED",
)

# Where one target ended up. Every one of these is TERMINAL for that target and for that
# target only — the paper's evidence collection continues. That is the whole point of the
# type: a single blocked target used to end the paper's reproduction outright.
TARGET_DISPOSITIONS = (
    "PENDING",
    "REPRODUCED",
    "FAILED_REPRODUCTION",
    "PAPER_ONLY_RESOLVED",        # settled without running anything
    # NOT a resolution, and kept apart from the line above because it was one for seven
    # papers and should not have been. The paper-only route has two branches and they
    # establish different things: re-evaluating a composition the paper printed
    # (ARITHMETIC_RECHECK) settles whether the paper's own arithmetic holds, while
    # re-verifying the QUOTATION behind a concern (PAPER_INTERNAL_CHECK) establishes only
    # that the concern rests on text the paper really contains. The second answers nothing
    # about whether the concern is correct — `stages/discover._paper_only_outcome` says so
    # in its own reason string — yet both shared PAPER_ONLY_RESOLVED, so the review filed
    # the disputed sentence under "What held up" while its own findings attacked it, and
    # the funnel counted 20 corpus targets as "settled a question about the paper" when
    # the honest count was zero. Its evidence state is NOT_INVESTIGATED, deliberately.
    "CITATION_VERIFIED_ONLY",     # the quotation is real; the concern was not settled
    # A THIRD fact that used to share PAPER_ONLY_RESOLVED's label, and the one that
    # actually matters: the ARITHMETIC_RECHECK branch settles the paper's own composition
    # in EITHER direction, and "it agrees" and "it does not" are opposite conclusions, not
    # two readings of one disposition. `claims.parse_quantity` recomputes the operands the
    # paper itself prints and compares the product to the paper's own stated total — no
    # model judgement, no execution, no repository. When it does not evaluate, that
    # ESTABLISHES A DEFECT, and `establishes_failure` recognises this disposition
    # directly, never through `provenance.admits` — this is not reproduction evidence and
    # must never be judged by the reproduction ceiling, which exists to gate EXECUTION
    # provenance. Its provenance stays "paper" and always will.
    #
    # ESTABLISHED IS NOT MATERIAL. Whether the defect also stops the paper is a separate
    # question, decided by `harness.materiality` from where the claim sits in the paper:
    # a contradicted composition in an appendix footnote is established, reported and
    # ledger-recorded, and does not reject the paper.
    "PAPER_ARITHMETIC_CONTRADICTION",
    # THE ONE DISPOSITION THAT REACHES `ARTIFACT_EVIDENCE`, and the reason that state
    # was previously unreachable. `ARTIFACT_EVIDENCE` and `RESOLVED_FROM_ARTIFACT` sat in
    # their vocabularies with no disposition mapping to them: the static code pass ran on
    # every cloned paper, wrote its hits into the machine report, and reached no question,
    # no target and no evidence state. The route existed in name only.
    #
    # It is written ONLY by `artifact_evidence.discharge`, which requires an AUDITED
    # snapshot, at least one statement the route was asked about, and at least one fact
    # carrying authority. "The repository cloned successfully" produces
    # COMPARISON_BLOCKED, not this. And it is NOT in `establishes_failure`: an
    # inconsistency between a paper and its released code is a real result and is not a
    # demonstration that the reported number is wrong — which of the two configurations
    # produced it is a question for execution, and invariant 8's materiality gate still
    # decides whether anything follows for the paper.
    # FIVE TERMINAL STATES FOR ONE ROUTE, because the first version had one and it was
    # too broad. `ARTIFACT_RESOLVED` accepted any fact carrying authority as settling any
    # statement the route was given, so "the checkout advertises evaluate.py" discharged
    # "the released repository implements the described method" on all four repository
    # papers. An entrypoint existing is SUPPORTING EVIDENCE for that question and is not
    # its answer.
    "ARTIFACT_FACT_ESTABLISHED",           # a bounded fact about the checkout, matching the question
    "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS",  # both locations verified; the relation is model-proposed
    "ARTIFACT_MISMATCH_ESTABLISHED",        # paper and code disagree, with identity ESTABLISHED
    "ARTIFACT_INSPECTION_INCONCLUSIVE",     # the route completed and settled no question
    # FIVE TERMINAL STATES FOR THE LITERATURE ROUTE, and the asymmetry is in which ones
    # exist. There is no NOVEL and no NOVELTY_VERIFIED here, because a bounded search that
    # matched nothing has established a fact about the SEARCH: the declared protocol
    # completed. The literature is not enumerable, and a disposition meaning "novel" would
    # turn `top_k = 20` into a claim about rank 21.
    "LITERATURE_MATCH_VERIFIED_ENDPOINTS",   # a real earlier work, a real passage, the overlap read
    "PRIOR_ART_RELATION_STRUCTURALLY_BOUND",  # the relation itself bound; rare, and may be zero
    "SEARCH_COMPLETED_NO_MATCH_FOUND",       # the protocol completed and no candidate qualified
    "SEARCH_INCONCLUSIVE",                   # candidates found; the evidence did not reach them
    "LITERATURE_BLOCKED",                    # provider, credential or cutoff unavailable
    # FOUR TERMINAL STATES FOR THE FOCUSED-VALIDATION ROUTE, and the split that matters
    # is between the first two and the third. A focused validation is a NEW experiment
    # this review designed; a reproduction re-runs one the paper published. Folding the
    # two together would let an experiment nobody published convict a paper through a
    # disposition whose name says "reproduction", so they are separate strings with
    # separate evidence states and separate entries in `establishes_failure`.
    #
    # `VALIDATION_OBSERVATION_ONLY` is the honest default and is expected to be the
    # commonest: two arms really ran and really were compared, and the conformance that
    # would let the comparison say something about the PAPER was not established. It
    # settles nothing and says so.
    "VALIDATION_DEFECT_ESTABLISHED",   # conformant, identity-bound, and the prediction failed
    "VALIDATION_SUPPORTS_CLAIM",       # conformant, identity-bound, and the prediction held
    "VALIDATION_OBSERVATION_ONLY",     # the arms were compared; conformance was not bound
    "VALIDATION_INCONCLUSIVE",         # it ran and the declared settlement rule fired neither way
    "SPECIFICATION_BLOCKED",      # the paper does not say enough to run it
    "ARTIFACT_BLOCKED",           # no code, or the code does not contain the experiment
    "ENVIRONMENT_BLOCKED",        # dependencies, platform, install
    "RESOURCE_BLOCKED",           # the hardware this experiment needs is not here
    # THREE REFUSALS THAT USED TO SHARE ONE LABEL. `harness_addressable` is a
    # conjunction of three distinct requirements and every failure of any of them landed
    # on ADDRESSING_BLOCKED, whose reader-facing sentence is "this review could not build
    # a re-derivable address for the claim". Over the shipped corpus that sentence was
    # printed 39 times across six of seven reviews and was FALSE every time: all 52
    # targets carrying it have a RESOLVED address, and in one review it sits two lines
    # under the printed address it claims not to have. The real blocker in all 52 was
    # that no verification route applies. A reviewer needs the three apart, because one
    # is our extraction, one is the paper's reporting, and one is our method inventory.
    "ADDRESSING_BLOCKED",         # WE could not build an address for the claim
    "REPORTING_BLOCKED",          # the paper prints no unambiguous quantity to compare against
    "NO_ROUTE_AVAILABLE",         # addressed and quantified; no route this system has applies
    "IDENTITY_BLOCKED",           # what would run is not bound to what was printed
    # Identity says the right program would run; this says its output could not be
    # held against anything. A focused validation experiment compares two arms and
    # this harness could build one; an artifact-inspection route answers a presence
    # question this system has no admissible evidence model for. A limit of our
    # method inventory, like NO_ROUTE_AVAILABLE and unlike ARTIFACT_BLOCKED — and, on
    # a CENTRAL target, `disposition.BLOCKER_FOR_DISPOSITION` maps it to METHOD for
    # exactly that reason: a central claim this review had no comparison for must
    # never read as a paper checked and found clean.
    "COMPARISON_BLOCKED",         # nothing this run produced could be compared
    "AUTHORIZATION_BLOCKED",      # a gate refused; a fact about this harness
    "INCONCLUSIVE",               # it ran and settled nothing
    "NO_EXPERIMENT_NEEDED",       # the referee judged none necessary — a review outcome
    "BUDGET_DEFERRED",            # warranted, ordered, and past this run's target budget
    # A material failure was already established from the paper itself, so the investigation
    # branch stopped. A property of the REVIEW's own conclusion, never of the target.
    "SUPERSEDED_BY_ESTABLISHED_FAILURE",
    "NOT_ATTEMPTED",
)

# None of these is a statement about the PAPER except the two that name a reproduction.
# `harness.stages.report` keys on that distinction, and invariants 4-7 in CLAUDE.md are
# the reason: a failed install is a fact about this host.
BLOCKED_DISPOSITIONS = ("SPECIFICATION_BLOCKED", "ADDRESSING_BLOCKED", "REPORTING_BLOCKED",
                        "NO_ROUTE_AVAILABLE", "ARTIFACT_BLOCKED", "ENVIRONMENT_BLOCKED",
                        "RESOURCE_BLOCKED", "IDENTITY_BLOCKED", "AUTHORIZATION_BLOCKED",
                        "COMPARISON_BLOCKED")

# What the planner decided to DO about a target. Separate from the disposition because
# "we decided no experiment was needed" and "we tried and were blocked" are different
# facts, and a system that cannot say the first one has to pretend it tried.
PLAN_ACTIONS = (
    "NO_EXPERIMENT_NEEDED",
    "PAPER_ONLY_RESOLUTION",
    "AUTHOR_CODE_REPRODUCTION",
    "INDEPENDENT_RECONSTRUCTION",
    "FOCUSED_VALIDATION_EXPERIMENT",
    "MECHANISM_TEST_ONLY",
    # READING THE PINNED ARTIFACT, and nothing running. Reachable only where no
    # executable route applies, so it can never SUPPRESS an execution — the failure mode
    # `PAPER_INTERNAL_CHECK` had when it sat in `planner._RESOLVING` and silently
    # cancelled every escalation behind it. It outranks the citation re-check in that
    # slot because it can actually settle an artifact-only question, where a citation
    # re-check settles nothing by construction, and it is refused outright for a question
    # whose answer is a MEASUREMENT (`artifact_evidence.requires_execution`).
    "ARTIFACT_INSPECTION_ONLY",
    # SEARCHING THE PUBLISHED LITERATURE, and nothing running. Reachable only where no
    # executable route applies, for the same reason `ARTIFACT_INSPECTION_ONLY` is: a
    # bounded prior-art search is cheap and must never be the reason an experiment did not
    # happen. What it can settle is a prior-art QUESTION, and what it can never settle —
    # by construction, not by policy — is whether a contribution is new.
    "LITERATURE_SEARCH_ONLY",
    "INFEASIBLE_SPECIFICATION",
    "INFEASIBLE_ADDRESSING",
    # The two that used to be folded into the line above. See TARGET_DISPOSITIONS.
    "INFEASIBLE_REPORTING",
    "INFEASIBLE_ROUTE",
    "INFEASIBLE_ARTIFACT",
    "INFEASIBLE_ENVIRONMENT",
    # Two deferrals. Neither is a judgement that no experiment is needed — one target is
    # answered by another target's run, the other is outranked by a more central claim.
    # Folding them into NO_EXPERIMENT_NEEDED made "the referee decided not to run this"
    # and "the referee is running it under another name" the same number.
    "DEFERRED_TO_ANOTHER_TARGET",
    "OUTRANKED_BY_CENTRAL_TARGET",
    # The paper's own evidence already settled it. NOT a refusal and NOT a blocker: the
    # referee reached a conclusion and there is nothing left for an experiment to add.
    # Reproducing a claim already disproved from the paper buys nothing, so this is a
    # NO_EXPERIMENT_NEEDED necessity like the two deferrals above it, and its evidence
    # state is NOT_INVESTIGATED because declining to run something settles nothing on the
    # evidence axis (invariant 22).
    "SUPERSEDED_BY_ESTABLISHED_FAILURE",
)

ACTIONS_REQUIRING_EXECUTION = ("AUTHOR_CODE_REPRODUCTION", "INDEPENDENT_RECONSTRUCTION",
                               "FOCUSED_VALIDATION_EXPERIMENT", "MECHANISM_TEST_ONLY")


# Plan action -> the necessity judgement it embodies. Pure re-labelling of a decision the
# planner has already made; nothing here decides anything.
NECESSITY_FOR_ACTION = {
    "NO_EXPERIMENT_NEEDED": "NO_EXPERIMENT_NEEDED",
    "PAPER_ONLY_RESOLUTION": "NO_EXPERIMENT_NEEDED",
    "DEFERRED_TO_ANOTHER_TARGET": "NO_EXPERIMENT_NEEDED",
    "OUTRANKED_BY_CENTRAL_TARGET": "NO_EXPERIMENT_NEEDED",
    "SUPERSEDED_BY_ESTABLISHED_FAILURE": "NO_EXPERIMENT_NEEDED",
    # The four warranting actions are ACTIONS_REQUIRING_EXECUTION, spliced in rather than
    # retyped: the tuple above and this table listed the same four names independently,
    # so an action added to one and not the other would have changed what
    # `funnel.warranting_experiment` counts without changing what needs execution.
    **{a: "EXPERIMENT_WARRANTED" for a in ACTIONS_REQUIRING_EXECUTION},
    "INFEASIBLE_SPECIFICATION": "EXPERIMENT_UNDERSPECIFIED",
    "INFEASIBLE_ADDRESSING": "EXPERIMENT_NOT_EXECUTABLE",
    "INFEASIBLE_REPORTING": "EXPERIMENT_NOT_EXECUTABLE",
    # NOT NO_EXPERIMENT_NEEDED, deliberately. Invariant 22 makes NO_EXPERIMENT_NEEDED a
    # SUCCESSFUL review outcome — the referee judged that nothing runnable would settle
    # the question. "This system has no method that applies" is a limit of our method
    # inventory, not a judgement that none is needed, and counting it as success would
    # inflate the one number invariant 22 exists to keep honest.
    "INFEASIBLE_ROUTE": "EXPERIMENT_NOT_EXECUTABLE",
    "INFEASIBLE_ARTIFACT": "EXPERIMENT_NOT_EXECUTABLE",
    "INFEASIBLE_ENVIRONMENT": "EXPERIMENT_NOT_EXECUTABLE",
}

# Terminal disposition -> what became of an experiment that was warranted. Only consulted
# for a target whose plan warranted one; a target that needed no experiment keeps
# NO_EXPERIMENT_NEEDED whatever its disposition.
NECESSITY_FOR_DISPOSITION = {
    "REPRODUCED": "EXPERIMENT_RESOLVED",
    "FAILED_REPRODUCTION": "EXPERIMENT_RESOLVED",
    "INCONCLUSIVE": "EXPERIMENT_UNRESOLVED",
    "SPECIFICATION_BLOCKED": "EXPERIMENT_UNDERSPECIFIED",
    "ADDRESSING_BLOCKED": "EXPERIMENT_NOT_EXECUTABLE",
    "REPORTING_BLOCKED": "EXPERIMENT_NOT_EXECUTABLE",
    "NO_ROUTE_AVAILABLE": "EXPERIMENT_NOT_EXECUTABLE",
    "ARTIFACT_BLOCKED": "EXPERIMENT_NOT_EXECUTABLE",
    "ENVIRONMENT_BLOCKED": "EXPERIMENT_NOT_EXECUTABLE",
    "RESOURCE_BLOCKED": "EXPERIMENT_NOT_EXECUTABLE",
    "IDENTITY_BLOCKED": "EXPERIMENT_NOT_EXECUTABLE",
    "COMPARISON_BLOCKED": "EXPERIMENT_NOT_EXECUTABLE",
    "AUTHORIZATION_BLOCKED": "EXPERIMENT_NOT_EXECUTABLE",
    # Warranted, ordered, and nothing has happened to it yet. Still warranted — a target
    # this run did not reach must not be counted as one that ran and settled nothing.
    "BUDGET_DEFERRED": "EXPERIMENT_WARRANTED",
    "NOT_ATTEMPTED": "EXPERIMENT_WARRANTED",
    "PENDING": "EXPERIMENT_WARRANTED",
    "NO_EXPERIMENT_NEEDED": "NO_EXPERIMENT_NEEDED",
    # Nothing ran, so an experiment that was warranted still is. Unreachable in practice
    # — the only producer of this disposition is the PAPER_ONLY_RESOLUTION action, whose
    # necessity is NO_EXPERIMENT_NEEDED and which therefore never consults this table —
    # and mapped anyway so the fail-open default (`EXPERIMENT_UNRESOLVED`, "it ran and did
    # not settle it") can never describe a target on which nothing was executed.
    "CITATION_VERIFIED_ONLY": "EXPERIMENT_WARRANTED",
}

# The review-level triage, which is NOT the reproduction status and must never be read as
# one. See `stages/report.triage` for the fold, and CLAUDE.md invariant 8 for why RED
# still cannot be reached by accumulation.
TRIAGE_LEVELS = ("RED", "YELLOW", "GREEN")


# WHAT KIND OF QUESTION this is — the semantic unit of investigation.
#
# Routing used to key on `has_value`: a target with a parsed printed quantity could reach
# an executable route and a target without one could not, whatever the question was. That
# made "did the paper print a number here" the only thing that decided whether a concern
# could be pursued, and it is the wrong question for most of what a referee asks. An
# attribution question is not answered by re-deriving the number the paper already
# published; a missing control is not a number at all.
#
# Derived by `harness.questions` from the finding's OWN closed-vocabulary self-
# classification — `discrepancy_type`, then `baseline_class`, then the lens — exactly as
# `taxonomy.classify` and the question templates are, so no count, number, metric name or
# paper identity reaches the derivation and a paper-specific rule stays inexpressible.
QUESTION_KINDS = (
    "PRINTED_QUANTITY",       # is the number the paper printed the number its code produces?
    "COMPOSITION",            # do the paper's own printed operands compose to its total?
    "ATTRIBUTION",            # is the effect caused by the mechanism the paper credits?
    "CONTROL_PRESENCE",       # is the comparison or control the claim needs actually there?
    "PROTOCOL_CONFORMANCE",   # does the procedure match the claim drawn from it?
    "SPECIFICATION",          # is the quantity or procedure defined well enough to check?
    "PRIOR_ART",              # is the contribution new relative to published work?
    # The eighth, and it is not a gap in the list above. A finding that declared no
    # discrepancy type, no baseline class and no recognised lens has said nothing about
    # what kind of question it raises, and guessing one would let routing be decided by a
    # default. Its routes are exactly what `has_value` used to give every target, so an
    # unclassified question behaves as the whole system did before this vocabulary existed.
    "UNCLASSIFIED",
)


class ReviewQuestion(_Base):
    """A methodological concern turned into something answerable.

    A finding says what is wrong. A question says what would settle it, which is the
    difference between a critique and a review: "the paper changes augmentation and loss
    weighting together" is an observation, and "is the gain attributable to the
    augmentation or to the loss weight?" is a question an experiment can discriminate.

    `route` is the harness's decision, not the lens's. A lens may argue that a question
    matters; whether a route exists is a structural fact about the paper and the artifact.
    """

    question_id: str = ""
    question: str = Field(default="", description="the question, as a question")
    kind: str = Field(
        default="UNCLASSIFIED",
        description="WRITTEN BY THE HARNESS: " + " | ".join(QUESTION_KINDS) + ". What KIND "
                    "of question this is, which is what decides the routes admissible for "
                    "it. Derived from the finding's own closed-vocabulary self-"
                    "classification, never from its prose and never from a lens's opinion "
                    "of what should be run.")
    from_finding: str = Field(default="", description="the finding_id this was derived from")
    source_finding_ids: list[str] = Field(
        default_factory=list,
        description="every finding that raised this question. Two lenses reaching the same "
                    "question about the same address is ONE question with two sources — "
                    "and that agreement is evidence, so it is recorded rather than "
                    "de-duplicated away at render time.")
    lens: str = ""
    claim_ref: ClaimRef | None = Field(
        default=None,
        description="the address in the paper this question is about, minted by the harness "
                    "from the finding's quotation. A question with no address can be asked "
                    "and cannot be pursued.")
    why_it_matters: str = Field(default="", description="what turns on the answer")
    what_would_settle_it: str = Field(default="", description="the evidence that would close it")
    possible_resolution_routes: list[str] = Field(
        default_factory=list,
        description="WRITTEN BY THE HARNESS: the admissible routes, cheapest first. A lens "
                    "may argue a question matters; whether a route exists is a structural "
                    "fact about the paper and the artifact.")
    route: str = Field(default="NONE", description="WRITTEN BY THE HARNESS: " + " | ".join(VERIFICATION_ROUTES))
    materiality: str = Field(default="UNASSESSED", description=" | ".join(CENTRALITY))
    resolution_status: str = Field(
        default="NOT_INVESTIGATED", description="WRITTEN BY THE HARNESS: " + " | ".join(RESOLUTION_STATES))
    evidence_state: str = Field(
        default="NOT_INVESTIGATED", description="WRITTEN BY THE HARNESS: " + " | ".join(EVIDENCE_STATES))
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="where the evidence behind the resolution lives — an execution log, a "
                    "target id, an address in the paper")
    resolved_without_execution: bool = Field(
        default=False, description="answered from the paper or the artifact alone")
    resolution: str = Field(default="", description="the answer, when one was reached")
    conclusion: str = Field(
        default="", description="what follows scientifically, when anything does")

    @property
    def evidence_needed(self) -> str:
        """Alias for `what_would_settle_it`, which is the stored name. Kept as a property
        rather than a second field so a reader cannot set one and leave the other."""
        return self.what_would_settle_it

    @property
    def is_open(self) -> bool:
        return self.resolution_status in ("UNRESOLVED", "NOT_INVESTIGATED")


class DiscoveredObject(_Base):
    """One thing the review found worth checking, with its address and its routes.

    **`verifiable_by_experiment` is no longer authoritative anywhere.** A lens may still
    write it and it is carried here as `proposed_by_lens`, but what decides whether this
    object can be pursued is `harness_addressable` — a harness-derived conjunction of a
    resolved `ClaimRef`, a parsed quantity where one is required, and a route that is not
    NONE. That demotion is the point: the boolean was an unchecked model field standing
    as the sole gate on the entire execution half of the system.
    """

    target_id: str = ""
    kind: str = Field(default="", description=" | ".join(DISCOVERY_KINDS))
    ref: ClaimRef | None = Field(default=None, description="the resolved address, or None")
    claim_text: str = Field(default="", description="the claim, in the paper's own words where possible")
    metric: str = Field(default="", description="the quantity, when the object reports one")
    expected_value: float | None = Field(default=None, description="what the paper printed")
    expected_raw: str = Field(default="", description="verbatim, as printed")
    experiment: str = Field(default="", description="the experiment/configuration this belongs to")
    centrality: str = Field(default="UNASSESSED", description="WRITTEN BY THE HARNESS: " + " | ".join(CENTRALITY))
    materiality_basis: str = Field(
        default="NONE",
        description="WRITTEN BY THE HARNESS: " + " | ".join(MATERIALITY_BASES) + ". WHY a "
                    "failure established on this target would be material to a CENTRAL "
                    "scientific claim, or NONE. A separate axis from `centrality`, which "
                    "asks only how strongly to prioritise and track this object: "
                    "`centrality` returns CENTRAL for anything that prints a composition "
                    "(`self_checking`), which is a fact about the form of a sentence and "
                    "not about what the paper's conclusion rests on, so it cannot gate a "
                    "paper-level stop. A basis rather than a bare boolean so the reason is "
                    "auditable off the artifact. See `harness.materiality` — it is a "
                    "CONSERVATIVE SUFFICIENT CONDITION and deliberately not a complete "
                    "materiality model: NONE means 'not machine-established as material', "
                    "never 'does not matter'.")
    evidence_requirements: list[str] = Field(
        default_factory=list, description="what would have to be true to check this")
    routes: list[str] = Field(default_factory=list, description="admissible routes, cheapest first")
    discovery_confidence: str = Field(default="", description=" | ".join(CONFIDENCES))
    status: str = Field(default="PENDING", description=" | ".join(TARGET_DISPOSITIONS))
    proposed_by_lens: bool = Field(
        default=False, description="the lens's own `verifiable_by_experiment`, kept as METADATA")
    harness_addressable: bool = Field(
        default=False, description="WRITTEN BY THE HARNESS: is this structurally checkable at all")
    addressing_blocker: str = Field(
        default="NONE",
        description="WRITTEN BY THE HARNESS: WHICH of `harness_addressable`'s three "
                    "requirements failed — " + " | ".join(ADDRESSING_BLOCKERS) + ". The "
                    "conjunction alone reached the planner as one boolean and left as one "
                    "refusal, so a paper whose claim we addressed perfectly and had no "
                    "method for was told its address could not be built.")
    note: str = Field(default="", description="why it is or is not addressable")
    counter_explanations: list[str] = Field(
        default_factory=list,
        description="the alternative readings the originating finding itself offered, carried "
                    "through untouched so `planner.plan` can record what an experiment would "
                    "discriminate between without inventing anything. Empty when the object "
                    "came from a printed quantity rather than from a finding.")
    question_id: str = Field(
        default="",
        description="the ReviewQuestion this target exists to answer. REQUIRED of every "
                    "object whose plan requires an execution — `stages/discover.build` "
                    "mints one for an object the extractor found rather than a lens, so "
                    "that a run always says which question it was spending compute on.")
    question_kind: str = Field(
        default="",
        description="WRITTEN BY THE HARNESS: " + " | ".join(QUESTION_KINDS) + ", or '' when "
                    "the object predates this layer. Carried on "
                    "the object because `discovery._routes` needs it before any question "
                    "object exists, and because the planner and the probe read it without "
                    "a lookup. Always equal to the bound question's `kind`.")
    priority: float = Field(default=0.0, description="WRITTEN BY THE HARNESS: see harness.priority")
    priority_reason: str = Field(default="", description="which dimensions produced that score")


class PlanDecision(_Base):
    """What the planner decided about one target, and every gate it consulted.

    The gates are recorded rather than summarised because "we did not run this" is only
    trustworthy when a reader can see WHICH condition failed. A model proposing an
    experiment gets no say here: `harness.planner.classify` is a pure function of typed
    structural facts.
    """

    target_id: str = ""
    action: str = Field(default="NO_EXPERIMENT_NEEDED", description=" | ".join(PLAN_ACTIONS))
    route: str = Field(default="NONE", description=" | ".join(VERIFICATION_ROUTES))
    reason: str = ""
    gates: dict[str, bool] = Field(
        default_factory=dict, description="every precondition consulted, by name, and its answer")
    blocking_gate: str = Field(default="", description="the first gate that failed, '' if none did")
    requires_execution: bool = False
    necessity: str = Field(
        default="NO_EXPERIMENT_NEEDED", description=" | ".join(EXPERIMENT_NECESSITY))

    # --- WHY this escalation, not merely why not ---------------------------------------
    # `gates` already records every precondition consulted and its answer, which is the
    # "why not" half. These four are the "why yes" half: a system that spends compute must
    # be able to answer "why did you spend it on this?" in a machine-readable way, and
    # "the gates passed" is not that answer.
    why_material: str = Field(
        default="", description="what turns on the answer to this target's question")
    paper_only_insufficient_because: str = Field(
        default="", description="why the paper's own content could not settle it")
    inspection_insufficient_because: str = Field(
        default="", description="why reading the released artifact could not settle it")
    competing_explanations: list[str] = Field(
        default_factory=list,
        description="the readings this experiment would discriminate between. Carried from "
                    "the finding's own counter-explanations, never invented here.")
    expected_observation: str = Field(
        default="", description="what the run should produce if the paper's claim holds")

    # --- STEP 6: route fallback, recorded as an ORDERED LOG rather than an overwrite ----
    # A target's identity binding against AUTHOR_CODE_EXECUTION is a fact only
    # `stages/probe.py` can establish — it needs a real checkout — so the FIRST plan for a
    # target that also offers INDEPENDENT_RECONSTRUCTION cannot know in advance whether a
    # fallback will be needed. When it is, `harness.planner.plan` is called again with
    # `author_code_exhausted=True` and `TargetSet.plans` gets a SECOND `PlanDecision` for
    # the SAME `target_id`, appended after the first. Every existing reader that does
    # `{p.target_id: p for p in ts.plans}` already keeps the LAST entry for a repeated key,
    # so this needs no change anywhere that reads "the current plan for this target" — it
    # only adds meaning for a reader that wants the full attempt history.
    attempt: int = Field(
        default=1,
        description="which attempt this plan represents for its target. 1 is the first "
                    "route chosen; a re-plan after an earlier attempt's identity failed "
                    "to bind increments it. Never decided by `plan()` itself — the caller "
                    "that performs the re-plan numbers its own attempts.")
    superseded_by: str = Field(
        default="",
        description="the ROUTE of the attempt that replaced this one, set on an EARLIER "
                    "attempt once a later one is made — never set by `plan()`, which does "
                    "not know its own future. '' means this is the plan currently in force "
                    "for its target. Never a foreign target_id: a re-plan is a second "
                    "attempt at the SAME question, not a new one.")


class TargetOutcome(_Base):
    """Where one target ended, with the evidence behind it.

    Independent per target. A paper with a blocked target A and a reproduced target C has
    two of these, and the report has to explain both rather than reporting whichever one
    happened to be first.
    """

    target_id: str = ""
    disposition: str = Field(default="NOT_ATTEMPTED", description=" | ".join(TARGET_DISPOSITIONS))
    action: str = Field(default="", description="the PlanDecision.action that led here")
    route: str = Field(default="NONE", description=" | ".join(VERIFICATION_ROUTES))
    provenance: str = Field(
        default="", description="template | synthesized | driver | repo_exec | reimpl_exec")
    reconciliation: Reconciliation | None = None
    failure_class: str = Field(default="none", description=" | ".join(FAILURE_CLASSES))
    reason: str = ""
    execution_ref: str = Field(
        default="", description="where the whole record lives, e.g. 'runs/<pid>/execution.jsonl'")
    attempts: int = 0

    launched: int = Field(
        default=0,
        description="processes this target actually STARTED, read off the execution record "
                    "rather than inferred from a disposition. `judged to warrant an "
                    "execution` and `an execution happened` are different counts and a "
                    "funnel that reports one as the other is reporting an intention as a "
                    "result.")
    identity_state: str = Field(
        default="",
        description="what the EXPERIMENT identity layer concluded for this target: "
                    + " | ".join(IDENTITY_STATES) + ", or empty when identity was never "
                    "reached. Carried here because the reconciliation's own copy is "
                    "written after the provenance ceiling has already returned for a "
                    "refused probe, and a reader asking WHY nothing bound needs the four "
                    "answers apart: `no_candidate` (the cited quantity is not something "
                    "this checkout produces), `ambiguous` (several commands could, and "
                    "choosing would be a guess), `unsupported` (the repository cannot "
                    "express what the cell requires) and `unmapped` (nothing was "
                    "assessed) are four different facts about the artifact.")

    @property
    def establishes_failure(self) -> bool:
        """The target-level states that may contribute a material failure. Kept as a
        property so the fold in `stages/report` cannot accidentally count a blocked
        target as a failed one.

        THREE STRUCTURALLY SEPARATE ROUTES, deliberately never merged into one condition:

          VALIDATION_DEFECT_ESTABLISHED  a controlled experiment this review DESIGNED,
                               subject to the SAME provenance ceiling as a reproduction
                               and, before it, to three conditions the reproduction path
                               does not have: the design conformant to the paper, the
                               comparison identity bound arm-to-arm, and the rule that
                               fired being one the addressed question admits
                               (`ArmComparison.establishes_defect`). It is a separate
                               disposition rather than a FAILED_REPRODUCTION because the
                               experiment is not one the authors published, and a referee
                               reading the review must be able to see which happened.
          FAILED_REPRODUCTION  admissible only through `provenance.admits` — the
                               reproduction-provenance ceiling, which exists to keep a
                               synthesized or template probe from convicting a paper.
          PAPER_ARITHMETIC_CONTRADICTION  admissible UNCONDITIONALLY, by disposition
                               alone, and never through `provenance.admits`. Its
                               provenance is "paper" — not a member of
                               `ADMISSIBLE_REPRODUCTION_PROVENANCE` and never meant to
                               be — because this is not reproduction evidence and the
                               reproduction ceiling has no authority over it. The
                               ceiling that DOES apply already ran, inside
                               `claims.parse_quantity`, before this disposition could
                               ever be written: the composition must be unambiguous,
                               deterministically recomputed from the paper's own
                               printed operands, with no model judgement involved.
        """
        from .provenance import admits
        return ((self.disposition in ("FAILED_REPRODUCTION",
                                      "VALIDATION_DEFECT_ESTABLISHED")
                 and admits(self.provenance))
                or self.disposition == "PAPER_ARITHMETIC_CONTRADICTION")

    # The three scientific axes, DERIVED — never stored, never read from a file, and so
    # never able to drift from the disposition and provenance they describe. See
    # `harness.taxonomy` for why they are three and not one.
    @property
    def evidence_state(self) -> str:
        from . import taxonomy
        return taxonomy.evidence_state(self.disposition, self.provenance)

    @property
    def resolution_state(self) -> str:
        from . import taxonomy
        return taxonomy.resolution_state(self.evidence_state)

    @property
    def concerns_the_paper(self) -> bool:
        """Does this outcome say anything about the PAPER, as opposed to about an
        artifact, a host, or one of this harness's own gates? Invariants 4-7."""
        from . import taxonomy
        return taxonomy.concerns_the_paper(self.evidence_state)

    def necessity(self, warranted: bool) -> str:
        """What became of the experiment-necessity judgement for this target.

        `warranted` comes from the PlanDecision, because a target that needed no
        experiment keeps NO_EXPERIMENT_NEEDED whatever happened to it afterwards —
        collapsing the two is how "we decided not to run this" starts being counted
        beside "we needed to and could not".
        """
        if not warranted:
            return "NO_EXPERIMENT_NEEDED"
        return NECESSITY_FOR_DISPOSITION.get(self.disposition, "EXPERIMENT_UNRESOLVED")


class RouteAttempt(_Base):
    """Durable accounting for one applicable route for one material review question.

    The booleans are intentionally explicit rather than inferred by consumers.  A route
    can be attempted and completed without being exhausted: verifying that a quotation
    exists completes PAPER_INTERNAL_CHECK, but it does not answer whether the concern is
    correct.  ``source_refs`` makes the row traceable to the paper address, target, plan,
    and execution record from which it was derived.
    """

    question_id: str = ""
    route: str = Field(default="NONE", description=" | ".join(VERIFICATION_ROUTES))
    target_ids: list[str] = Field(default_factory=list)
    attempted: bool = False
    completed: bool = False
    exhausted: bool = False
    state: str = Field(default="NOT_TRIED", description=" | ".join(ROUTE_ATTEMPT_STATES))
    blocker: str = Field(
        default="",
        description="typed terminal blocker or harness-owned gate/budget/policy cause")
    source_refs: list[str] = Field(
        default_factory=list,
        description="paper addresses, target ids and execution records supporting this row")
    reason: str = ""


class TargetSet(_Base):
    """Every target considered for one paper, in priority order, with its disposition.

    The campaign's `manuscript/target_sets.json` was the prototype for this and was
    hand-curated: no harness module read or wrote it. This is the same accounting, emitted
    by the pipeline, which is what makes "every plausible target was considered" a
    checkable property rather than a methodology claim.
    """

    paper_id: str = ""
    objects: list[DiscoveredObject] = Field(default_factory=list)
    questions: list[ReviewQuestion] = Field(default_factory=list)
    plans: list[PlanDecision] = Field(default_factory=list)
    outcomes: list[TargetOutcome] = Field(default_factory=list)
    route_attempts: list[RouteAttempt] = Field(
        default_factory=list,
        description="one durable row per applicable implemented route for each central or "
                    "machine-material question")
    route_exhaustion_question_ids: list[str] = Field(
        default_factory=list,
        description="the exact question denominator used by route-exhaustion coverage")
    extraction_coverage: dict = Field(
        default_factory=dict,
        description="how much of the paper was addressable at all: tables extracted, cells with "
                    "numbers, prose quantities found, refs that resolved. An empty target set "
                    "caused by failed table extraction reads identically to a paper with no "
                    "checkable results unless this is recorded.",
    )

    def by_id(self, target_id: str) -> DiscoveredObject | None:
        return next((o for o in self.objects if o.target_id == target_id), None)

    def outcome_for(self, target_id: str) -> TargetOutcome | None:
        return next((o for o in self.outcomes if o.target_id == target_id), None)


class LedgerEntry(_Base):
    """One traceable line from a paper's own words to a scientific implication.

    The property that matters is that every field is filled from an artifact that already
    exists — none of it is re-derived prose. For any material conclusion the system must
    be able to answer: what claim, where stated, what question, which target, why that
    target, which route, what code at what commit, what happened, what was observed,
    what published quantity it was compared with, why that comparison is admissible, and
    what follows.
    """

    entry_id: str = ""
    claim_text: str = ""
    materiality_basis: str = Field(
        default="NONE",
        description="WRITTEN BY THE HARNESS: " + " | ".join(MATERIALITY_BASES) + ". Copied "
                    "from the target's DiscoveredObject so a reader tracing a paper-level "
                    "stop can see WHY this target was entitled to cause one — or, on an "
                    "established defect carrying NONE, why it was not.")
    source_ref: str = Field(default="", description="the ClaimRef address")
    source_quote: str = ""
    question: str = ""
    target_id: str = ""
    selection_reason: str = Field(default="", description="why this target was prioritised")
    route: str = ""
    action: str = ""
    provenance: str = ""
    commit: str = ""
    command: list[str] = Field(default_factory=list)
    observed: str = Field(default="", description="what the execution produced, in one line")
    compared_with: str = Field(default="", description="the published quantity, verbatim")
    admissibility: str = Field(default="", description="why that comparison is or is not admissible")
    disposition: str = ""
    implication: str = Field(default="", description="the scientific conclusion, if any, that follows")

    # --- the three axes, carried so the trace can be sorted by any of them -------------
    evidence_state: str = Field(
        default="NOT_INVESTIGATED", description=" | ".join(EVIDENCE_STATES))
    resolution_state: str = Field(
        default="NOT_INVESTIGATED", description=" | ".join(RESOLUTION_STATES))
    necessity: str = Field(
        default="", description="what became of the experiment question for this entry: "
                                + " | ".join(EXPERIMENT_NECESSITY))
    concerns_the_paper: bool = Field(
        default=False,
        description="does this entry's evidence say anything about the PAPER, as opposed "
                    "to about an artifact, a host, or one of this harness's gates? The "
                    "single most misread distinction in the whole trace, so it is a field.")
    launched: int = Field(default=0, description="processes this entry's target STARTED")
    why_material: str = Field(default="", description="what turns on this entry's answer")


class CaseLedger(_Base):
    """The machine-readable audit trace for one paper. NOT the reviewer's report.

    Kept separate on purpose: the reviewer report is one to two pages and summarises this,
    and a human should not have to read the ledger to understand the conclusion. The
    ledger exists so that any line of the report can be traced back to an artifact.
    """

    paper_id: str = ""
    entries: list[LedgerEntry] = Field(default_factory=list)
    route_attempts: list[RouteAttempt] = Field(
        default_factory=list,
        description="the per-question route accounting copied from discovery/targets.json")
    efficiency: dict = Field(
        default_factory=dict,
        description="the conditional-escalation instrumentation: questions generated, resolved "
                    "without execution, requiring artifact inspection, triggering execution, "
                    "blocked before execution, seconds spent. Measured, never asserted — a "
                    "system claiming efficiency it does not count is claiming nothing.",
    )


class PaperAssessment(_Base):
    """Whether the paper's own evidence already settles it — asked BEFORE anything runs.

    Written by `harness.assessment.assess` from the kept findings and nothing else. The
    restriction is the point: a gate that could be moved by an execution outcome would let
    infrastructure decide what a review established, which is invariants 4 to 7 in the one
    place they would be hardest to notice.

    `material_failure_established` stops the EXPENSIVE INVESTIGATION BRANCH — acquisition,
    static audit, identity, resources, execution — and stops nothing else. Discovery, the
    report, the ledger and the four pure layers all still run, because the referee record
    has to be complete whatever the outcome.
    """

    material_failure_established: bool = False
    basis: str = Field(default="NONE", description=" | ".join(DISPOSITION_BASIS))
    reason: str = Field(default="", description="the deterministic rule that produced this")
    counted_fatal_ids: list[str] = Field(
        default_factory=list,
        description="the findings that stopped the paper, by id. 'We stopped early' is "
                    "only trustworthy when a reader can see which finding stopped it.")


# --------------------------------------------------------------------------- #
# ⑨ CLAIM LINKS — the one correspondence the paper does not print
# --------------------------------------------------------------------------- #
# Measured over the eight-paper corpus, NO deterministic rule connects a paper's headline
# claims to its numbers, because papers do not write that connection down:
#
#     cross-references appearing in any Abstract ................... 0
#     cross-references appearing in any Conclusion ................. 1
#     reported numbers resolving into any Abstract ................. 0
#     numbers printed by headline sentences ....................... 21
#     of those, occurring in exactly one recovered table cell ...... 0
#
# An abstract says "reduces training memory by 40%". It does not say "see Table 3", and
# 40% is not a cell. The correspondence is SEMANTIC, and every model-free mechanism that
# tried to recover it fired on nothing: `in_abstract` on 0 of 706 objects under either
# spelling of its locator, `materiality.basis_for_ref` on 1 of 1,729 addresses, and the
# deterministic claim graph on 0 of 1,729.
#
# So the pairing is PROPOSED by a reader and VERIFIED by the harness, which is the
# arrangement every other model-supplied fact in this system already has. The proposal is
# a pair of addresses; the harness re-resolves both, requires the claim side to be in the
# paper's own summary of itself, and where both sides carry numbers re-derives the
# relationship between them. Invariant 2 is untouched: nothing below that the harness
# writes may be read from the reader's file.
CLAIM_LINK_RELATIONS = (
    "EQUAL",                    # the claim prints the evidence's value
    "ROUNDS_TO",                # the claim prints a rounding of it — "40%" for 40.2
    "NO_NUMBER_IN_CLAIM",       # a qualitative claim; the link is a pointer, not arithmetic
    "NO_NUMBER_AT_EVIDENCE",    # the evidence is a caption, an equation or prose
    "MISMATCH",                 # both carry numbers and they disagree — REFUSED
)

# Why a proposed link was not accepted. Named per link rather than counted, because
# "the reader proposed twelve and eight held" is only actionable with the four reasons.
CLAIM_LINK_REFUSALS = (
    "claim_unresolved",         # the claim quotation is not in the paper, or is ambiguous
    "claim_not_headline",       # it resolved, and not inside the Abstract or Conclusion
    "evidence_unresolved",      # the evidence address names nothing in the parsed paper
    "numeric_mismatch",         # both sides carry a number and the numbers disagree
    "duplicate",                # the same (claim, evidence) pair proposed twice
    "self_reference",           # the evidence IS the claim span; a claim cannot cite itself
)


# WHAT AN ACCEPTED LINK IS AUTHORITY FOR, which is not one thing.
#
# A verified link proves that both ENDPOINTS exist and are what the reader said they were:
# the sentence is in the paper, exactly once, inside the Abstract or the Conclusion; the
# address resolves and holds the quoted contents; any number both sides carry was
# recomputed. It does NOT prove the third thing, which is the one a referee cares about:
#
#     that this evidence scientifically supports this headline claim.
#
# That relationship was proposed by a model, and unless the DOCUMENT ITSELF binds the two
# — by citing the object from the claim sentence, or by an identity this harness can
# establish without a model — nothing here checks it. Calling all of them "machine-checked
# dependencies" would launder a model's semantic judgement into a deterministic result,
# which is the exact failure `verified_observation` exists to prevent one level down.
CLAIM_LINK_AUTHORITY = (
    # Both endpoints deterministically verified; the SUPPORT RELATIONSHIP is model-proposed.
    # Useful for investigation priority, graph navigation, coverage, route planning and
    # human explanation. It has NO paper-stopping authority and may not acquire any.
    "ENDPOINTS_VERIFIED_SEMANTIC_LINK",
    # The relationship ITSELF is established from the document, by one of
    # `CLAIM_LINK_BINDING_BASES`. This class MAY become a materiality input in future,
    # subject to the materiality audit. It is deliberately hard to reach.
    "STRUCTURALLY_BOUND_LINK",
    # An endpoint requirement failed. `refusal` says which.
    "REFUSED",
)

# HOW a link's relationship was bound to the document, when it was. Named rather than
# boolean so a reader can re-derive the binding by hand, and so a future basis arrives as
# a new name instead of silently widening an existing one.
CLAIM_LINK_BINDING_BASES = (
    # The claim sentence itself cites the evidence's object by its printed label, that
    # label identifies exactly one recovered object, and that object is the one the
    # evidence address names. The paper drew the arrow; this only followed it.
    "explicit_crossref",
    # Metric, benchmark, comparison arm and statistic are each independently established
    # between the claim sentence and the cell, and the two numbers agree. Every one of the
    # four is a separate check recorded in `binding_checks`; three of four is not a
    # binding, because the missing one is exactly where a plausible-looking pairing goes
    # wrong.
    "quantitative_identity",
)


class ClaimLink(_Base):
    """One proposed correspondence between a headline claim and the evidence behind it.

    THE READER SUPPLIES TWO QUOTATIONS. Everything else on this type is written by the
    harness, exactly as on `Finding`: the reader says "this sentence rests on that cell",
    and the harness decides whether both halves exist, whether the claim is really in the
    paper's summary of itself, and whether the two numbers agree.

    `rationale` is the reader's prose and is never checked. It is kept because a link a
    human disagrees with is more useful with the reasoning attached than without it, and
    it reaches no threshold.
    """

    link_id: str = ""
    # --- WRITTEN BY THE READER --------------------------------------------------------
    claim_quote: str = Field(default="", description="verbatim sentence from the Abstract "
                                                     "or Conclusion")
    evidence_ref: str = Field(default="", description="T<t>:r<r>:c<c> | F<n> | E<n> | p<N>")
    evidence_quote: str = Field(default="", description="verbatim text at that address")
    rationale: str = Field(default="", description="UNVERIFIED: the reader's reason for "
                                                   "pairing them")
    # --- WRITTEN BY THE HARNESS -------------------------------------------------------
    claim_ref: str = Field(default="", description="WRITTEN BY THE HARNESS: the address "
                                                   "minted for `claim_quote`")
    claim_section_idx: int = Field(default=-1, description="WRITTEN BY THE HARNESS")
    accepted: bool = Field(default=False, description="WRITTEN BY THE HARNESS")
    refusal: str = Field(default="", description="WRITTEN BY THE HARNESS: "
                                                 + " | ".join(CLAIM_LINK_REFUSALS))
    numeric_relation: str = Field(default="", description="WRITTEN BY THE HARNESS: "
                                                          + " | ".join(CLAIM_LINK_RELATIONS))
    claim_value: float | None = Field(default=None, description="WRITTEN BY THE HARNESS")
    evidence_value: float | None = Field(default=None, description="WRITTEN BY THE HARNESS")
    verified_observation: str = Field(
        default="", description="WRITTEN BY THE HARNESS: what it actually confirmed, in "
                                "its own words, re-derivable from doc.json by hand")
    link_authority: str = Field(
        default="REFUSED", description="WRITTEN BY THE HARNESS: "
                                       + " | ".join(CLAIM_LINK_AUTHORITY))
    binding_basis: str = Field(
        default="", description="WRITTEN BY THE HARNESS: which of "
                                + " | ".join(CLAIM_LINK_BINDING_BASES)
                                + " bound the relationship, '' when none did")
    binding_checks: dict[str, bool] = Field(
        default_factory=dict,
        description="WRITTEN BY THE HARNESS: each named requirement of the strongest "
                    "binding attempted, and whether it held. Kept even when the binding "
                    "failed, because 'which of the four was missing' is the actionable "
                    "half of a zero.")


class ClaimLinkSet(_Base):
    """Every link proposed for one paper, accepted and refused alike.

    The refused ones are kept. A reader that proposed twelve pairings of which four did
    not resolve is a different reader from one that proposed eight, and a set that
    silently dropped the failures would make the two indistinguishable — the same reason
    `load_reports` counts dropped findings rather than discarding them.
    """

    paper_id: str = ""
    links: list[ClaimLink] = Field(default_factory=list)
    proposed: int = 0
    accepted: int = 0
    refusals: dict[str, int] = Field(default_factory=dict)
    notes: str = Field(default="", description="the reader's own account of what it did")
    # THE TWO COUNTS ARE REPORTED SEPARATELY AND NEVER SUMMED INTO ONE HEADLINE. They
    # answer different questions — "did both ends of this pairing survive deterministic
    # validation" and "did the document itself bind them" — and the second is the only one
    # that could ever inform a paper-level decision. A zero is printed as a zero.
    endpoints_verified: int = Field(
        default=0, description="accepted links whose SUPPORT RELATIONSHIP is model-proposed")
    structurally_bound: int = Field(
        default=0, description="accepted links whose relationship the DOCUMENT establishes")

    def accepted_links(self) -> list[ClaimLink]:
        return [x for x in self.links if x.accepted]

    def bound_links(self) -> list[ClaimLink]:
        """Only the links whose RELATIONSHIP the document binds. Never `accepted_links`."""
        return [x for x in self.links
                if x.accepted and x.link_authority == "STRUCTURALLY_BOUND_LINK"]


# --------------------------------------------------------------------------------------
# PRIOR ART, AND THE ONE RULE THAT SHAPES THE WHOLE VOCABULARY
#
#     finding prior art may support a novelty concern;
#     failing to find prior art does NOT establish novelty.
#
# That asymmetry is encoded here rather than stated in prose, and the encoding is that
# **there is no value anywhere in this vocabulary that means "novel"**. There is no
# authority for it, no disposition for it, and `NOVELTY_ESTABLISHING_AUTHORITIES` below is
# the empty tuple BY CONSTRUCTION — a caller asking "does this establish novelty?" gets
# False from a membership test that can never succeed, rather than from a rule someone
# could later relax. A completed search with no candidate is a fact about THE SEARCH, and
# the only honest thing it can say is that the declared protocol completed.
#
# The global literature is not enumerable. Everything below is written so that a bounded
# protocol can report what it did without any path by which "we looked at twenty results"
# becomes "there is nothing at rank twenty-one".
LITERATURE_AUTHORITY = (
    # LEVEL 1: a work exists, with the identity the index reports. Says nothing whatever
    # about the target paper — it is the bibliographic analogue of `ARTIFACT_FACT`.
    "BIBLIOGRAPHIC_FACT",
    # LEVEL 2: an addressed novelty/contribution claim in the target, a resolved passage
    # in an EARLIER work, and a verified chronology — with the OVERLAP between them still
    # a model's reading. A referee's question, not a novelty failure. The third channel to
    # need this distinction, after `ENDPOINTS_VERIFIED_SEMANTIC_LINK` and
    # `ENDPOINTS_VERIFIED_ARTIFACT_CONCERN`, and for the same reason each time: verifying
    # two ends does not verify the relationship between them.
    "ENDPOINTS_VERIFIED_LITERATURE_CONCERN",
    # LEVEL 3: the RELATIONSHIP itself is established from bounded evidence — see
    # `PRIOR_ART_BINDING_BASES`. Deliberately hard to reach, and may be zero over a whole
    # corpus. It is not manufactured to make the route produce a number.
    "STRUCTURALLY_BOUND_PRIOR_ART",
    # LEVEL 4 — "the paper is not novel" — HAS NO SPELLING. Novelty is a scholarly
    # judgement requiring semantic comparison and field context; a strong prior-art match
    # is a serious human-facing concern and is not a verdict this system may reach.
    "NONE",
)

# The authorities that may support a prior-art CONCERN, and the ones that establish
# novelty. The second is empty and is a tuple rather than a constant `False` so that the
# asymmetry is a fact about the vocabulary that a test can read.
PRIOR_ART_CAPABLE_AUTHORITIES = ("ENDPOINTS_VERIFIED_LITERATURE_CONCERN",
                                 "STRUCTURALLY_BOUND_PRIOR_ART")
NOVELTY_ESTABLISHING_AUTHORITIES: tuple[str, ...] = ()

# HOW a prior-art relationship was bound without a model, when it was. Each is a thing a
# reader can open in both documents. `reviewer_reading` is deliberately in the list and
# deliberately NOT sufficient, exactly as `auditor_assertion` is for experiment identity.
PRIOR_ART_BINDING_BASES = (
    "named_method_lineage",      # both works name the same method, and the dates are unambiguous
    "explicit_first_claim_met",  # the target claims "first to X"; the earlier work states that X
    "identical_identifier",      # the same DOI or arXiv id describes the claimed contribution
    "reviewer_reading",          # the literature reviewer's judgement, and nothing deterministic
)

# WHAT the literature reviewer may propose about a (target claim, candidate work) pair.
# A closed vocabulary for the same reason every other model-facing one is closed: an open
# one lets the proposer name a category and escape whatever rule is keyed on it.
LITERATURE_RELATIONS = (
    "LIKELY_DIRECT_PREDECESSOR",
    "RELATED_BUT_MATERIALLY_DIFFERENT",
    "TERMINOLOGY_COLLISION_ONLY",
    "POSSIBLE_OMITTED_BASELINE",
    "POSSIBLE_PRIORITY_CONFLICT",
    "INSUFFICIENT_EVIDENCE",
)

# The relations that can carry a concern at all. The other three are real answers — and
# "related but materially different" is the answer a careful search most often produces,
# which is why it is a value rather than a silence.
CONCERNING_RELATIONS = ("LIKELY_DIRECT_PREDECESSOR", "POSSIBLE_OMITTED_BASELINE",
                        "POSSIBLE_PRIORITY_CONFLICT")

# WHERE a target paper's earliest public date came from, strongest first. Chronology is
# the one half of this route that must be deterministic: a candidate dated after the
# target cannot be prior art for it, whatever any reading says about the overlap.
CUTOFF_SOURCES = (
    "arxiv_submission_v1",   # the first arXiv submission, which is what "public" means here
    "crossref_published",    # the publisher's own earliest printed date
    "openalex_publication_date",
    "proceedings_year",      # year only: a same-year candidate cannot be ordered against it
    "operator_supplied",
    "none",
)

# How well the cutoff is known. YEAR_ONLY is its own state and not a weak ESTABLISHED:
# it is the state in which a same-year candidate must be refused rather than admitted.
CUTOFF_STATES = ("ESTABLISHED", "YEAR_ONLY", "AMBIGUOUS", "UNKNOWN")

# Where one candidate sits relative to the target's cutoff. Derived from dates and never
# from a reading, and the source of each date is recorded so that "we chose the date most
# useful to the concern" is checkable rather than promised.
CHRONOLOGY_STATES = (
    "PREDATES_CUTOFF",         # public before the target: eligible to be prior art
    "POSTDATES_CUTOFF",        # later work. Possibly related; prior art for nothing here
    "CONTEMPORANEOUS_UNRESOLVED",  # the two dates cannot be ordered at the precision known
    "CANDIDATE_DATE_UNKNOWN",
    "TARGET_CUTOFF_UNKNOWN",
)

# The only chronology that may support prior-art authority, named as a tuple so the rule
# is a membership test rather than a comparison someone can widen.
PRIOR_ART_ELIGIBLE_CHRONOLOGY = ("PREDATES_CUTOFF",)

# Whether the TARGET already cites the candidate. Not a refusal either way: §8 of the
# design — a cited predecessor may still challenge a novelty claim, but that is
# "the stated distinction may be insufficient" and not "the authors omitted prior art".
# Calling the first the second would accuse the authors of something they did not do.
CANDIDATE_CITATION_STATES = ("CITED_BY_TARGET", "APPARENTLY_UNCITED",
                             "BIBLIOGRAPHY_UNAVAILABLE")

# WHY a proposed prior-art relation did not bind. Named per attempt for the same reason
# `MISMATCH_REFUSALS` is: "we found nothing" and "we found it and could not date it" are
# opposite results about a search.
LITERATURE_REFUSALS = (
    "target_claim_unaddressed",        # the quoted claim does not resolve in the paper
    "target_claim_not_a_novelty_claim",  # it resolves, and it claims no novelty or contribution
    # A PAPER IS NOT PRIOR ART FOR ITSELF. Found by the first live run: the ACL paper's own
    # arXiv preprint came back as a LIKELY_DIRECT_PREDECESSOR — same title, same benchmark
    # name, same 58 topics and 12 domains, posted before the proceedings — and the reviewer
    # said so in its own note. Every endpoint verified and the finding would have been an
    # accusation about the authors citing themselves.
    "candidate_is_the_target_itself",
    # AND THE CASE WHERE THE HARNESS CANNOT TELL. When extraction did not recover a title
    # this harness will stand behind, a candidate whose title carries the very name the
    # paper says it introduces is either that paper's own earlier version or a real
    # priority conflict — and nothing available here separates them. Refused with the
    # reason said out loud, because reporting it as prior art would be a false accusation
    # and dropping it silently would hide something a referee should glance at.
    "candidate_may_be_the_target_itself",
    "candidate_identity_unestablished",  # no DOI, no arXiv id, no index id: not a citable work
    "candidate_quote_unresolved",      # the quoted passage is not in the retrieved candidate text
    "candidate_date_unknown",          # no provider dated it
    "target_cutoff_unknown",           # the target's own earliest public date is not established
    "candidate_not_earlier",           # dated, and not before the target's cutoff
    "relation_not_concerning",         # the reviewer's own answer raises no prior-art question
    "relation_not_deterministic",      # a concern, and the overlap is the reviewer's reading
    "insufficient_candidate_evidence",  # metadata only where the claim needs the work's content
)

# What a provider call actually did. A provider this system could not reach is a
# CONFIGURATION outcome and never a scientific one: "we have no Semantic Scholar key" is
# not evidence about anybody's paper, and a protocol that counted it as a completed query
# would discharge a route by being unconfigured.
PROVIDER_OUTCOMES = (
    "COMPLETED",            # the index answered, with results or with none
    "NO_RESULTS",           # the index answered and matched nothing. A completed query
    "RATE_LIMITED",
    "UNAUTHENTICATED",      # the index requires a credential this host does not hold
    "UNREACHABLE",
    "MALFORMED_RESPONSE",
    "NOT_ATTEMPTED",
)

# The outcomes that count toward the declared protocol being carried out.
PROTOCOL_COMPLETING_OUTCOMES = ("COMPLETED", "NO_RESULTS")


class WorkIdentity(_Base):
    """One scholarly work, canonicalised across the indexes that report it.

    ONE PAPER IS NOT FOUR PAPERS. The same work is an arXiv preprint, a Crossref record,
    an OpenAlex work and a conference version, and counting those as four prior works
    would make a search's candidate count a measure of how many indexes answered.
    `literature.canonical_id` folds them, strongest identifier first, and `aliases` keeps
    every id that folded so the merge is re-derivable by hand rather than asserted.

    The fallback — normalised title plus first author plus year — is deliberately last and
    deliberately conjunctive: two different works with similar titles must not merge.
    """

    canonical_id: str = Field(default="", description="WRITTEN BY THE HARNESS: doi: | arxiv: "
                                                      "| openalex: | s2: | title:")
    doi: str = ""
    arxiv_id: str = ""
    openalex_id: str = ""
    s2_id: str = ""
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    venue: str = ""
    year: int = 0
    date: str = Field(default="", description="ISO-8601, the earliest public date known")
    date_source: str = Field(default="none", description=" | ".join(CUTOFF_SOURCES))
    url: str = ""
    abstract: str = Field(default="", description="as retrieved; the text a quotation is "
                                                  "checked against")
    abstract_sha256: str = Field(default="", description="of the retrieved text, so a later "
                                                         "reader can prove what was read")
    aliases: list[str] = Field(
        default_factory=list, description="every provider id that folded into this work")
    providers: list[str] = Field(default_factory=list, description="which indexes reported it")


class SearchCutoff(_Base):
    """The target paper's earliest defensible public date, and how well it is known.

    Only work public BEFORE this may support a prior-art concern. `state` is what stops
    the rule being quietly widened: with YEAR_ONLY, a candidate from the same year is
    CONTEMPORANEOUS_UNRESOLVED rather than earlier, because a proceedings year does not
    order two papers within it.
    """

    date: str = Field(default="", description="ISO-8601; '' when unknown")
    source: str = Field(default="none", description=" | ".join(CUTOFF_SOURCES))
    state: str = Field(default="UNKNOWN", description=" | ".join(CUTOFF_STATES))
    evidence: str = Field(default="", description="what established it, in the harness's words")
    candidates: dict[str, str] = Field(
        default_factory=dict,
        description="every date any provider offered, by source. Recorded BEFORE one is "
                    "chosen, so 'the date most useful to the concern' is visible if it "
                    "ever happens")


class ProviderCall(_Base):
    """One query against one index, and what it did. The unit the protocol is counted in."""

    provider: str = ""
    query: str = Field(default="", description="the exact query string sent")
    query_family: str = ""
    claim_id: str = ""
    requested_at: str = ""
    outcome: str = Field(default="NOT_ATTEMPTED", description=" | ".join(PROVIDER_OUTCOMES))
    http_status: int = 0
    n_results: int = 0
    cache_key: str = Field(default="", description="sha256 of provider+query+top_k; the "
                                                   "cached raw response is stored under it")
    from_cache: bool = False
    error: str = ""
    seconds: float = 0.0

    @property
    def completed(self) -> bool:
        """Did this query actually get carried out? A credential we lack is not a result."""
        return self.outcome in PROTOCOL_COMPLETING_OUTCOMES


class SearchClaim(_Base):
    """One bounded thing the route searched for, addressed in the target paper.

    NOT "is this paper novel". A claim is a quoted sentence that resolves in the document,
    minted by `claims.mint` exactly as every other quotation in this system is, plus the
    concepts a query can be built from.
    """

    claim_id: str = ""
    ref: str = Field(default="", description="WRITTEN BY THE HARNESS: the minted address")
    quote: str = Field(default="", description="verbatim, re-verified against the parsed paper")
    claim_kind: str = Field(default="", description="NOVELTY_MARKER | CONTRIBUTION | "
                                                    "HEADLINE_CLAIM | METHOD_NAME")
    marker: str = Field(default="", description="the novelty word the scan matched, when it did")
    concepts: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list, description="the exact queries issued")


class SearchProtocol(_Base):
    """The bounds of the search, PUBLISHED so that "exhausted" means something checkable.

    "Route exhausted" for this route means THE DECLARED PROTOCOL COMPLETED. It does not
    and cannot mean that the literature was searched: the universe is not enumerable, and
    a system that let a budget stand in for the world would turn `top_k = 20` into a
    statement about rank 21.
    """

    query_families: list[str] = Field(default_factory=list)
    providers_required: list[str] = Field(default_factory=list)
    top_k: int = 0
    max_queries_per_claim: int = 0
    max_claims: int = 0
    # HOW MANY RETRIEVED CANDIDATES ARE ACTUALLY READ, per claim. A separate bound from
    # `top_k` because they are separate facts: top_k is how deep each query went, and this
    # is how many of what came back a reader was shown. Four queries at top_k 20 across
    # two indexes returns up to 160 records for one sentence, and a reader shown all of
    # them is a reader shown none of them carefully. Published, like every other bound
    # here, because "no match" means nothing without it.
    max_candidates_reviewed: int = 0
    notes: str = ""


class PriorArtFact(_Base):
    """One adjudicated (target claim, candidate work) pair, and the authority it carries.

    `authority` is WRITTEN BY THE HARNESS from what actually verified — the claim's
    address, the candidate's identity, the candidate quotation against retrieved text, and
    the chronology — never read from the reviewer that proposed the pair. The reviewer
    supplies two quotations and a relation; everything that decides what the pair
    ESTABLISHES is decided here.
    """

    fact_id: str = ""
    claim_id: str = ""
    target_ref: str = Field(default="", description="WRITTEN BY THE HARNESS: the minted address")
    target_quote: str = ""
    candidate: WorkIdentity | None = None
    candidate_quote: str = Field(default="", description="verbatim from the retrieved text")
    candidate_quote_located: bool = Field(
        default=False, description="WRITTEN BY THE HARNESS: the quotation was found in the "
                                   "text this harness itself retrieved")
    relation: str = Field(default="INSUFFICIENT_EVIDENCE", description=" | ".join(
        LITERATURE_RELATIONS) + " — the REVIEWER's proposal, and a proposal only")
    statement: str = Field(default="", description="what this pair establishes, in the "
                                                   "harness's words")
    authority: str = Field(default="NONE", description="WRITTEN BY THE HARNESS: "
                                                       + " | ".join(LITERATURE_AUTHORITY))
    chronology: str = Field(default="TARGET_CUTOFF_UNKNOWN",
                            description="WRITTEN BY THE HARNESS: " + " | ".join(CHRONOLOGY_STATES))
    chronology_basis: str = Field(default="", description="which date, from which source, "
                                                          "on each side")
    citation_state: str = Field(default="BIBLIOGRAPHY_UNAVAILABLE",
                                description="WRITTEN BY THE HARNESS: "
                                            + " | ".join(CANDIDATE_CITATION_STATES))
    binding_basis: str = Field(default="", description="WRITTEN BY THE HARNESS: which of "
                                                       + " | ".join(PRIOR_ART_BINDING_BASES))
    refusal: str = Field(default="", description=" | ".join(LITERATURE_REFUSALS))
    reviewer_note: str = Field(default="", description="the reviewer's own prose, kept for a "
                                                       "human and consumed by nothing")

    @property
    def supports_concern(self) -> bool:
        """Does this pair raise a prior-art question a referee should look at?"""
        return self.authority in PRIOR_ART_CAPABLE_AUTHORITIES

    @property
    def establishes_novelty(self) -> bool:
        """Always False, and it is a membership test in an EMPTY tuple that makes it so.

        The asymmetry this whole route is built around, written where a caller trips over
        it: nothing this search produces — least of all its silence — establishes that a
        contribution is new.
        """
        return self.authority in NOVELTY_ESTABLISHING_AUTHORITIES


class LiteratureSearch(_Base):
    """One LITERATURE_SEARCH route attempt over one paper, and what its protocol did.

    `discharged` is what stops a completed search from resolving anything: a route
    discharges only when it adjudicated a pair to an authority, and a search that found no
    qualifying candidate reports SEARCH_COMPLETED_NO_MATCH_FOUND, whose evidence state is
    a fact about the search and not about the paper.
    """

    paper_id: str = ""
    target_id: str = ""
    cutoff: SearchCutoff | None = None
    protocol: SearchProtocol | None = None
    claims_searched: list[SearchClaim] = Field(default_factory=list)
    provider_calls: list[ProviderCall] = Field(default_factory=list)
    works: list[WorkIdentity] = Field(
        default_factory=list, description="the DEDUPLICATED candidate set")
    raw_results: int = Field(default=0, description="records returned, before dedup")
    facts: list[PriorArtFact] = Field(default_factory=list)
    cited_by_target: int = 0
    apparently_uncited: int = 0
    discharged: bool = Field(default=False, description="WRITTEN BY THE HARNESS")
    reason: str = ""
    escalations: list[str] = Field(
        default_factory=list,
        description="what this search makes newly possible for a LATER route — a missing "
                    "comparison worth measuring, a baseline worth building. Recorded, "
                    "never acted on here.")
    candidates_reviewed: int = Field(
        default=0, description="distinct candidate works a reader was actually SHOWN. A "
                               "different number from `proposed`, which counts what came "
                               "back: a reader that read twelve abstracts and proposed "
                               "nothing has done the work and found nothing, and a reader "
                               "that was shown nothing has not")
    proposed: int = Field(default=0, description="reviewer proposals received")
    notes: str = Field(default="", description="the reviewer's own account of what it did")

    @property
    def providers_completed(self) -> list[str]:
        return sorted({c.provider for c in self.provider_calls if c.completed})

    @property
    def providers_failed(self) -> list[str]:
        done = set(self.providers_completed)
        return sorted({c.provider for c in self.provider_calls if c.provider not in done})

    def concerns(self) -> list[PriorArtFact]:
        return [f for f in self.facts if f.supports_concern]

    def bound_relations(self) -> list[PriorArtFact]:
        return [f for f in self.facts if f.authority == "STRUCTURALLY_BOUND_PRIOR_ART"]

    @property
    def protocol_completed(self) -> bool:
        """The DECLARED bounds were carried out. Never "the literature was searched".

        Every planned query reached an index that answered. A provider this host could not
        authenticate to leaves this False, because an unconfigured system has not
        completed a protocol — it has failed to attempt one, and the two must not print
        the same number.
        """
        return bool(self.provider_calls) and all(c.completed for c in self.provider_calls)


# --------------------------------------------------------------------------- #
# Focused validation — WHAT A CONTROLLED EXPERIMENT THIS REVIEW DESIGNED CAN SAY
# --------------------------------------------------------------------------- #
# The route exists for the questions a referee asks that re-running a published number
# cannot answer: two variables changed at once, a missing control, a data budget that
# differs between arms, a protocol confound, a comparison that is not apples-to-apples.
# The question it asks is always the same one:
#
#     what is the SMALLEST scientifically legitimate experiment, derivable from the
#     paper and its artifact, that discriminates between the competing explanations?
#
# "Derivable" is the whole of the discipline. An experiment that answers the question by
# choosing an optimizer, a split, an augmentation strength or a schedule the paper never
# stated measures OUR choice, not theirs — so the missing ingredient is named and the
# design is refused. `NEVER_ASSUMED` is that rule as data.

# The eight things a focused-validation experiment needs before it may be built. Every
# one of them is a REQUIREMENT and none has a default: a design missing any of them is
# SPECIFICATION_BLOCKED and names which, rather than being completed from convention.
VALIDATION_INGREDIENTS = (
    "addressed_question",       # a review question with an address that re-resolves
    "competing_explanations",   # at least two readings the run would discriminate between
    "arm_instantiation",        # the paper/artifact says enough to BUILD both arms
    "metric_identity",          # the quantity, its basis and its unit, on both arms
    "dataset_identity",         # the benchmark AND the split, on both arms
    "controlled_variables",     # what is held fixed, named rather than assumed
    "changed_variable",         # exactly what differs, named
    "settlement_condition",     # what observation would settle it, declared BEFORE the run
)

# The scientific choices this harness will not supply, whatever the convention is. Named
# as data so that "we filled in the usual value" is a diff a reader can see rather than a
# habit nobody wrote down. A design that would need one of these and does not have it
# reports `SPECIFICATION_BLOCKED` with the ingredient named.
NEVER_ASSUMED = (
    "optimizer", "learning_rate", "schedule", "split", "augmentation_strength",
    "preprocessing", "threshold", "batch_size", "epochs", "seed_policy",
    "early_stopping", "weight_decay", "tokenizer", "normalisation",
)

VALIDATION_DESIGN_STATES = (
    "DESIGNED",                # every ingredient bound; the experiment could be built
    "SPECIFICATION_BLOCKED",   # at least one ingredient is missing from the paper/artifact
    "NOT_APPLICABLE",          # this question is not one a controlled experiment answers
)

# FOUR RUNGS, AND THE TOP ONE HAS NO SPELLING — the same device `ARTIFACT_AUTHORITY` and
# `LITERATURE_AUTHORITY` use. The unreachable rung here is CAUSAL ATTRIBUTION: "the
# mechanism the paper credits is what produces the gain". One controlled comparison, on
# one benchmark, at one scale, cannot establish that — it can establish that under THIS
# controlled comparison the effect the claim predicts did or did not appear, which is a
# bounded observation a referee can act on and is not a causal law.
VALIDATION_AUTHORITY = (
    "ARM_MEASUREMENT",               # level 1: about one run of one arm, and nothing else
    "CONTROLLED_OBSERVATION",        # level 2: two arms compared; conformance NOT established
    "CONFORMANT_CONTROLLED_RESULT",  # level 3: + conformance, bound identity, answers the question
    "NONE",                          # observed; establishes nothing
)

# Only the third rung may reach a defect, and even then materiality decides independently
# whether anything follows for the paper.
DEFECT_CAPABLE_VALIDATION_AUTHORITIES = ("CONFORMANT_CONTROLLED_RESULT",)

# THE EMPTY TUPLE THAT MAKES LEVEL 4 INEXPRESSIBLE. `ArmComparison.establishes_attribution`
# is a membership test in this, so the question "did this experiment prove the mechanism"
# has one answer for every input this system can produce, and there is no threshold a
# later contributor could relax to change it.
CAUSAL_ATTRIBUTION_AUTHORITIES: tuple[str, ...] = ()

# WHETHER THE EXPERIMENT IS THE PAPER'S EXPERIMENT. Not a judgement and not a model field:
# `validation.conformance` derives it from whether every arm was instantiated from a
# source that relocates, no `NEVER_ASSUMED` choice was supplied by this harness, and the
# provenance is one the reproduction ceiling already admits.
VALIDATION_CONFORMANCE_STATES = (
    "CONFORMANT",    # every arm derives from the paper or the pinned artifact
    "DEVIATES",      # at least one scientific choice is this harness's, not the authors'
    "UNASSESSED",    # conformance was never reached
)

# HOW THE QUESTION WOULD BE SETTLED, DECLARED BEFORE ANYTHING RUNS. There is deliberately
# no universal percentage anywhere in this module: the rule belongs to the review question
# and is carried on the design, so a result cannot be settled against a threshold chosen
# after the numbers came back.
SETTLEMENT_RULES = (
    "DIRECTION_AGREES",              # the treatment moves the metric the way the claim says
    "EFFECT_EXCEEDS_TOLERANCE",      # the gap between arms exceeds a tolerance the design declared
    "EFFECT_WITHIN_EQUIVALENCE_MARGIN",  # non-inferiority / equivalence, against a declared margin
    "ARMS_INDISTINGUISHABLE",        # the two arms' intervals overlap, or they do not
)

# Rules whose arithmetic needs a NUMBER the design must declare. Listed rather than
# inferred, because a rule that silently defaults its tolerance to zero would settle every
# comparison in whichever direction the noise happened to fall.
RULES_REQUIRING_TOLERANCE = ("EFFECT_EXCEEDS_TOLERANCE", "EFFECT_WITHIN_EQUIVALENCE_MARGIN")

# Rules whose arithmetic needs per-arm UNCERTAINTY. A comparison asked to decide overlap
# between two arms that reported a single number each is refused, not answered.
RULES_REQUIRING_UNCERTAINTY = ("ARMS_INDISTINGUISHABLE",)

BETWEEN_ARM_STATES = (
    "SETTLED_AS_PREDICTED",       # the declared rule fired in the direction the claim predicts
    "SETTLED_AGAINST_PREDICTION",  # it fired the other way
    "NOT_SETTLED",                # it fired neither way; the question stays open
    "REFUSED",                    # the comparison was not performed at all — see `refusal`
)

# Every way a between-arms comparison can be refused. Closed, and every one of them is a
# fact about the experiment or about this harness — not one is a finding about the paper.
BETWEEN_ARM_REFUSALS = (
    "",
    "arm_missing",                    # fewer than two arms produced a measurement
    "arm_produced_no_value",          # an arm ran and reported no value for the metric
    "metric_identity_mismatch",       # the arms measured different quantities, bases or units
    "dataset_identity_mismatch",      # the arms ran on different benchmarks or splits
    "statistical_unit_mismatch",      # the arms aggregate over different units
    "not_a_controlled_comparison",    # more than one thing differs between the arms
    "settlement_condition_undeclared",  # no rule was declared before the run
    "tolerance_undeclared",           # the declared rule needs a tolerance and none was given
    "uncertainty_unavailable",        # the declared rule needs intervals and the arms have none
    "relative_difference_undefined",  # the control arm measured zero
    "design_not_established",         # the design was SPECIFICATION_BLOCKED
)

ARM_ROLES = ("control", "treatment")

# Which direction a metric has to move for the claim to hold. Declared on the design, from
# the claim's own wording, and never inferred from the numbers.
PREDICTED_DIRECTIONS = ("INCREASE", "DECREASE", "NO_CHANGE", "UNSTATED")

OBSERVED_DIRECTIONS = ("INCREASE", "DECREASE", "NO_CHANGE", "UNDETERMINED")


class ValidationArm(_Base):
    """One arm of a focused-validation experiment, and where every part of it came from.

    `instantiation_basis` is the field that makes conformance checkable: an arm the paper
    describes and an arm this harness assembled are two different things, and an
    experiment whose arms cannot say which they are cannot be held against the paper.
    """

    role: str = Field(default="", description=" | ".join(ARM_ROLES))
    label: str = Field(default="", description="the arm's own name, e.g. the method a row names")
    address: str = Field(
        default="",
        description="where the paper reports this arm, when it does — a cell, a prose span. "
                    "Empty is normal and is not a defect: the control arm of an attribution "
                    "question is usually an arm the paper never ran.")
    method: str = Field(default="", description="the method this arm instantiates")
    configuration: dict[str, str] = Field(
        default_factory=dict,
        description="every scientific choice this arm fixes, by name. WRITTEN FROM THE "
                    "PAPER OR THE PINNED ARTIFACT — a key whose value this harness chose "
                    "is what `NEVER_ASSUMED` forbids and what `conformance` detects.")
    varies: list[str] = Field(
        default_factory=list, description="what this arm changes relative to the control")
    source: str = Field(
        default="", description="paper | artifact | harness — where this arm's configuration "
                                "came from. 'harness' is never conformant.")
    instantiation_basis: str = Field(
        default="", description="the address or file:line each choice was read from")
    command: list[str] = Field(
        default_factory=list, description="the artifact's own command for this arm, when one exists")


class SettlementCondition(_Base):
    """What observation would settle the question, declared BEFORE anything runs.

    There is no default rule and no default tolerance. A design that reaches execution
    without one is refused by `between_arms.compare` with
    `settlement_condition_undeclared`, which is the only way this system can honestly say
    a controlled result settled anything: a rule chosen after the numbers arrived settles
    whatever the author of the rule wanted.
    """

    rule: str = Field(default="", description=" | ".join(SETTLEMENT_RULES))
    tolerance: float | None = Field(
        default=None,
        description="the margin the declared rule compares against, in the metric's own "
                    "unit. Required for the rules in RULES_REQUIRING_TOLERANCE and "
                    "deliberately never defaulted — a tolerance of zero settles every "
                    "comparison in whichever direction the noise fell.")
    tolerance_basis: str = Field(
        default="",
        description="where the tolerance came from — the paper's own stated effect size, "
                    "its own reported variance, or the measured seed-noise band. A "
                    "tolerance with no basis is a number this review invented.")
    predicted_direction: str = Field(
        default="UNSTATED", description=" | ".join(PREDICTED_DIRECTIONS))
    statement: str = Field(
        default="", description="the condition in one sentence, as a referee would read it")
    declared_before_execution: bool = Field(
        default=False,
        description="WRITTEN BY THE HARNESS when the design is persisted, before any "
                    "process starts. False makes the comparison refuse.")


class ValidationDesign(_Base):
    """The smallest legitimate experiment that would discriminate the explanations — or
    the named reason one cannot be built.

    Every ingredient in `VALIDATION_INGREDIENTS` is required and `missing` names the ones
    that are not bound. `state` is derived from `missing` rather than stored beside it, so
    a design cannot be reported DESIGNED while something it needs is absent.
    """

    design_id: str = ""
    paper_id: str = ""
    target_id: str = ""
    question_id: str = ""
    question_kind: str = Field(default="", description="the ReviewQuestion kind this answers")
    question_ref: str = Field(
        default="", description="the addressed claim the question is about, re-resolvable")
    question_text: str = ""
    competing_explanations: list[str] = Field(
        default_factory=list,
        description="the readings this run would discriminate between. CARRIED from the "
                    "finding's own counter-explanations, never invented here.")
    comparison_id: str = Field(
        default="",
        description="the claim-graph COMPARISON node this design reuses, e.g. "
                    "'comparison:accuracy|cifar-100'. Empty when the paper set up no such "
                    "comparison, which is itself an unbound ingredient.")
    metric: str = ""
    metric_basis: str = Field(default="", description="absolute | relative_to_baseline")
    metric_unit: str = ""
    metric_ref: str = Field(default="", description="where the paper defines the metric")
    benchmark: str = ""
    split: str = Field(
        default="",
        description="the evaluation split, from the paper. NEVER defaulted to 'test': "
                    "which split a number was measured on is exactly the kind of choice "
                    "`NEVER_ASSUMED` keeps this harness from supplying.")
    statistical_unit: str = Field(
        default="", description="what one measurement is over — an example, a seed, a fold")
    arms: list[ValidationArm] = Field(default_factory=list)
    changed_variables: list[str] = Field(default_factory=list)
    controlled_variables: list[str] = Field(default_factory=list)
    settlement: SettlementCondition | None = None
    state: str = Field(default="SPECIFICATION_BLOCKED",
                       description=" | ".join(VALIDATION_DESIGN_STATES))
    missing: list[str] = Field(
        default_factory=list, description="unbound entries of VALIDATION_INGREDIENTS")
    assumed: list[str] = Field(
        default_factory=list,
        description="NEVER_ASSUMED choices this harness would have had to supply. A "
                    "non-empty list makes the design SPECIFICATION_BLOCKED, whatever else "
                    "is bound.")
    conformance: str = Field(default="UNASSESSED",
                             description=" | ".join(VALIDATION_CONFORMANCE_STATES))
    conformance_basis: str = Field(default="", description="why, naming the arm and the source")
    statement: str = Field(
        default="", description="what this experiment would establish, in the harness's words")
    reason: str = Field(default="", description="why it cannot be built, when it cannot")

    @property
    def established(self) -> bool:
        return self.state == "DESIGNED"

    @property
    def control(self) -> ValidationArm | None:
        return next((a for a in self.arms if a.role == "control"), None)

    @property
    def treatment(self) -> ValidationArm | None:
        return next((a for a in self.arms if a.role == "treatment"), None)


class ArmMeasurement(_Base):
    """What ONE arm of a focused validation actually produced.

    Persisted per arm rather than folded immediately into a delta, because the delta is
    the only thing the old `ProbeResult.measured_delta` kept and a referee tracing a
    between-arms verdict needs each side's identity, configuration, unit and repetition
    count to check that the two were comparable at all.
    """

    design_id: str = ""
    role: str = Field(default="", description=" | ".join(ARM_ROLES))
    label: str = ""
    experiment_id: str = Field(
        default="", description="the artifact command or reconstruction that produced it")
    configuration: dict[str, str] = Field(default_factory=dict)
    metric: str = ""
    metric_basis: str = ""
    unit: str = ""
    benchmark: str = ""
    split: str = ""
    statistical_unit: str = ""
    value: float | None = Field(default=None, description="the arm's point estimate, or None")
    uncertainty: float | None = Field(
        default=None, description="one standard deviation over the repetitions, when there "
                                  "were repetitions. None is not zero.")
    n: int = Field(default=0, description="repetitions that produced a value")
    seeds: list[int] = Field(default_factory=list)
    provenance: str = Field(default="", description="mirrors ProbeSpec.provenance")
    execution_ref: str = Field(default="", description="where the raw record lives")
    reason: str = Field(default="", description="why this arm produced no value, when it did not")


class ArmComparison(_Base):
    """One arm held against another, against a rule declared before either of them ran.

    `state` is arithmetic over the two measurements and the declared rule. `authority` is
    what that arithmetic is ENTITLED to say, which is a different question and the one
    this class exists to keep separate: a comparison can be perfectly computed and still
    say nothing about the paper, because the arms were this harness's arms.
    """

    design_id: str = ""
    control_label: str = ""
    treatment_label: str = ""
    metric: str = ""
    unit: str = ""
    control_value: float | None = None
    treatment_value: float | None = None
    difference: float | None = Field(
        default=None, description="treatment - control, in the metric's own unit")
    relative_difference: float | None = Field(
        default=None,
        description="difference / |control|, or None when the control measured zero. "
                    "Reported as None rather than as infinity or as a large number: a "
                    "relative change against zero is undefined, not enormous.")
    direction: str = Field(default="UNDETERMINED", description=" | ".join(OBSERVED_DIRECTIONS))
    intervals_overlap: bool | None = Field(
        default=None,
        description="do the arms' one-sigma intervals overlap? None when either arm "
                    "reported no uncertainty — never False, which would read as a "
                    "separation nobody measured.")
    settlement_rule: str = Field(default="", description=" | ".join(SETTLEMENT_RULES))
    tolerance: float | None = None
    state: str = Field(default="REFUSED", description=" | ".join(BETWEEN_ARM_STATES))
    refusal: str = Field(default="", description=" | ".join(BETWEEN_ARM_REFUSALS))
    authority: str = Field(default="NONE", description=" | ".join(VALIDATION_AUTHORITY))
    conformance: str = Field(default="UNASSESSED",
                             description=" | ".join(VALIDATION_CONFORMANCE_STATES))
    answers_question: bool = Field(
        default=False,
        description="WRITTEN BY THE HARNESS: does the rule that fired answer the question "
                    "the design was built for? A settled comparison that answers a "
                    "different question settles a different question.")
    provenance: str = Field(default="", description="the ProbeSpec.provenance behind both arms")
    statement: str = Field(default="", description="what this establishes, in the harness's words")
    reason: str = ""

    @property
    def establishes_defect(self) -> bool:
        """May this comparison contribute a paper-level defect?

        Three conditions and all of them are structural: the top authority rung, the rule
        having fired AGAINST the claim's own prediction, and the comparison answering the
        question it was designed for. Materiality then decides independently whether
        anything follows for the paper — this property is never the last word.
        """
        return (self.authority in DEFECT_CAPABLE_VALIDATION_AUTHORITIES
                and self.state == "SETTLED_AGAINST_PREDICTION"
                and self.answers_question)

    @property
    def supports_claim(self) -> bool:
        return (self.authority in DEFECT_CAPABLE_VALIDATION_AUTHORITIES
                and self.state == "SETTLED_AS_PREDICTED"
                and self.answers_question)

    @property
    def establishes_attribution(self) -> bool:
        """Did this experiment establish that the credited mechanism causes the effect?

        A membership test in `CAUSAL_ATTRIBUTION_AUTHORITIES`, which is the empty tuple,
        so the answer is False for every input this system can construct. One controlled
        comparison on one benchmark is evidence about that comparison. Generalising it to
        a mechanism is the reader's job and is not a thing this route may assert.
        """
        return self.authority in CAUSAL_ATTRIBUTION_AUTHORITIES


class FocusedValidation(_Base):
    """One FOCUSED_VALIDATION_EXPERIMENT route attempt over one target.

    `discharged` requires a comparison that reached an authority. A design that was built
    and blocked, and a run whose arms could not be compared, both leave it False — the
    same property `LiteratureSearch.discharged` has, and for the same reason: a route that
    discharged by failing to conclude is an exhaustion number that rises fastest where the
    least was established.
    """

    paper_id: str = ""
    target_id: str = ""
    design: ValidationDesign | None = None
    measurements: list[ArmMeasurement] = Field(default_factory=list)
    comparison: ArmComparison | None = None
    disposition: str = Field(default="NOT_ATTEMPTED", description=" | ".join(TARGET_DISPOSITIONS))
    discharged: bool = Field(default=False, description="WRITTEN BY THE HARNESS")
    launched: int = Field(default=0, description="processes actually started for this route")
    reason: str = ""


PHASES = ("ingest", "audit", "collect", "grade", "assess", "discover", "probe", "report",
          "done")

CASE_STATUSES = (
    "pending",    # created, nothing run yet
    "running",    # mid-pipeline, more work is possible right now
    "waiting",    # blocked on evidence that must arrive from outside this process
    "complete",   # a report exists
    "error",      # the case could not be reviewed at all
)

# What one phase attempt concluded. `abstain` is deliberately NOT a case outcome: a paper
# whose reproduction cannot proceed still gets a full review, so an abstention is recorded
# on the case and the pipeline continues to the report.
PHASE_OUTCOMES = ("ok", "waiting", "retry", "abstain", "error")


class PhaseEvent(_Base):
    """One attempt at one phase. The controller's audit trail."""

    phase: str = Field(default="", description=" | ".join(PHASES))
    outcome: str = Field(default="", description=" | ".join(PHASE_OUTCOMES))
    reason: str = ""
    detail: dict = Field(default_factory=dict, description="the stage's own compact result")
    attempt: int = 1
    ts: str = ""


class CaseState(_Base):
    """One paper's position in the workflow, persisted at `projects/<pid>/controller.json`.

    Persisted rather than held in memory because the loop has to survive the process
    ending — a run that stops for lens evidence must resume exactly where it stopped,
    and a batch of papers must be able to advance independently across invocations.
    """

    paper_id: str = ""
    source: str = Field(default="", description="the PDF path or case id this case came from")
    content_sha: str = ""
    phase: str = Field(default="ingest", description=" | ".join(PHASES))
    status: str = Field(default="pending", description=" | ".join(CASE_STATUSES))
    attempts: dict[str, int] = Field(default_factory=dict, description="attempts made per phase")
    history: list[PhaseEvent] = Field(default_factory=list)
    awaiting: list[str] = Field(default_factory=list, description="lenses with no result yet")
    blocked_reason: str = Field(default="", description="why `waiting` or `error`")
    reproduction_class: str = Field(
        default="",
        description="why reproduction did not conclude, from the deterministic layer. A case can "
                    "be `complete` with this set — an unreproducible paper is still a reviewed "
                    "paper, and treating abstention as failure is what makes a harness crash on "
                    "the papers it most needs to be careful about.",
    )
    verdict: str = Field(default="", description="the S4 verdict, once the report exists")
    assessment: PaperAssessment | None = Field(
        default=None,
        description="the ASSESS phase's answer: did the paper's own evidence already "
                    "establish a material failure? Persisted so a reader of the case can "
                    "see WHY the investigation branch did or did not run, without "
                    "re-deriving it from the findings.")
    disposition: str = Field(
        default="NOT_REVIEWED",
        description="WHAT HAPPENS TO THIS PAPER, carried from the report: "
                    + " | ".join(PAPER_DISPOSITIONS) + ". Persisted on the case so a batch "
                    "summary can answer 'which of these do I stop?' without re-opening "
                    "every report.")
    disposition_basis: str = Field(default="NONE", description=" | ".join(DISPOSITION_BASIS))
    report_path: str = ""
    resume_after: str = Field(
        default="",
        description="best-effort hint (free text, e.g. 'resets 3:20pm (Asia/Kolkata)') for when "
                    "re-running review is worth trying again after an account-level rate limit — "
                    "advisory only, the controller does not sleep or poll on it.",
    )
    failure_kind: str = Field(
        default="",
        description="WHY the last phase attempt did not succeed, from `harness.failures.classify`: "
                    + " | ".join(FAILURE_KINDS) + ". Persisted because 'waiting' alone does not "
                    "tell an operator whether to wait, re-run, or fix their install — and only "
                    "a retry-NOW failure is allowed to consume part of a bounded retry budget.",
    )
    retry_policy: str = Field(
        default="", description="now | later | never, for `failure_kind` above")

    @property
    def terminal(self) -> bool:
        return self.status in ("complete", "error")
