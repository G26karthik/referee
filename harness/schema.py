"""ONE evidence object family for REFEREE v4.

Every vocabulary is a named module-level tuple; no field description retypes an
enumeration as free prose.

Write-side enforcement: `_Base` still allows `extra` fields (a stage adding a field must
not break the pipeline) — a different concern from a DECLARED field's value being wrong.
Nine fields — `ExecAuthorization.decision`/`.failure_class`,
`Reconciliation.provenance`/`.failure_class`, `ProbeSpec.provenance`,
`TargetOutcome.provenance`/`.failure_class`, `CaseState.phase`/`.status` — are enforced via
`Vocab(...)`: a real membership check against their declared tuple, or a one-line-noted
`LEGACY_VALUES` exception, since a wrong value in one of these is a silent trust-boundary
failure. Every other vocabulary-bearing field stays plain `str`: enforcing every annotated
field would convert "drop this one bad field" into "crash the whole review" on a typo.

`LEGACY_VALUES` starts EMPTY, deliberately: a hand-guessed exception is exactly the
retyping-as-free-prose mistake this schema exists to stop. A real one is added only after
`tools/validate_legacy_json.py` (or equivalent) loads real `projects/*/**.json` through
these nine fields and a `ValidationError` names a genuine historical value.

`python -m harness.schema` runs the self-check.
"""
from __future__ import annotations

from typing import Annotated, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from .provenance import PROVENANCE_LABELS, PROVENANCE_VALUES
from .failures import FAILURE_KINDS
from .taxonomy import EVIDENCE_STATES, RESOLUTION_STATES, SCIENTIFIC_CLASSES

# What happens to a paper once the review completes. Never a severity and never a colour.
PAPER_DISPOSITIONS: tuple[str, ...] = (
    "STOP_MATERIAL_FAILURE",       # a material failure was established; see `basis`
    "BLOCKED_SPECIFICATION",       # a central question needed detail the paper omits
    "BLOCKED_ARTIFACT",            # a central question needed code that is absent or unbindable
    "BLOCKED_RESOURCES",           # a central question needed hardware not obtainable here
    # A central question needed a verification approach this system does not implement:
    # never the paper's fault, never the artifact's, never the host's — this review's own
    # method inventory ran out.
    "BLOCKED_METHOD",
    "PASS_TO_HUMAN_UNRESOLVED",    # a central question was pursued admissibly and stayed open
    "PASS_TO_HUMAN_CONCERNS",      # verified concerns that weaken a claim without rejecting it
    "PASS_TO_HUMAN_CLEAN",         # nothing of the above, within the audited scope
    "NOT_REVIEWED",                # the pipeline did not complete
)

# ON WHAT a material failure was established. Never a severity and never a colour: which
# KIND of evidence carried the decision are different things to hand a referee.
DISPOSITION_BASIS: tuple[str, ...] = (
    "AUTHOR_CODE_REPRODUCTION",      # the authors' own checkout, at a verified commit
    "INDEPENDENT_REIMPLEMENTATION",  # a reproduction the ceiling admits that is NOT theirs
    "PAPER_ARITHMETIC",              # the paper's own printed composition does not evaluate
    # A certificate found a concrete instance violating a stated theorem/bound — never
    # conflated with INDEPENDENT_REIMPLEMENTATION, a reconstruction of an experiment.
    "INDEPENDENT_CERTIFICATE",
    "NONE",
)

# Which locator a central-claim dependency was established from.
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


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


# === Enforced-field machinery — see the module docstring for which fields use this =====
LEGACY_VALUES: dict[str, tuple[str, ...]] = {
    # `CaseState.verdict`, the old RED|GREEN colour field, was removed entirely rather
    # than widened; `CaseState.disposition` (a `decide.PAPER_DISPOSITIONS` value, never a
    # colour) is what a caller reads for routing now. `_Base`'s `extra="allow"` means an
    # old controller.json with a stray `verdict` key still reads fine, just unenforced.
}


def _vocab_validator(vocab: tuple[str, ...], legacy_key: str):
    """A plain validator CALLABLE (not a type) for one enforced field. Kept separate from
    the `Annotated[...]` construction at each field because pyright rejects a function CALL
    used directly in type-annotation position, even though `Annotated`'s own metadata slot
    is ordinary runtime data pyright never type-checks.
    """
    allowed = set(vocab) | set(LEGACY_VALUES.get(legacy_key, ()))

    def _check(value: str) -> str:
        if value == "" or value in allowed:
            return value
        raise ValueError(
            f"{legacy_key}: {value!r} is not a member of its declared vocabulary "
            f"({', '.join(vocab)}) and is not a named LEGACY_VALUES exception. If this is "
            f"a new, intentional value, add it to the vocabulary tuple; if it is a real "
            f"historical artifact, add a one-line-noted LEGACY_VALUES entry instead of "
            f"widening the vocabulary silently.")

    return _check


# === 1. DOCUMENT — the parsed paper, and the only thing evidence is checked against ====

# What PaperDoc's own addressable units are called. Also CrossRef.kind's vocabulary: a
# citation names one of these unit kinds, never a different vocabulary from the units
# themselves — collapsing the two used to let a citation-shaped sentence become a
# fabricated Figure/Table object (see INVARIANT_MAP.md on `_FIGURE_CAPTION`).
SOURCE_UNIT_KINDS = ("figure", "table", "equation", "section", "appendix")

CAPTION_SOURCES = ("ruled_positional", "geometric_paired", "none")

# The addresses `harness.claims.resolve` can re-derive from a PaperDoc alone. A kind the
# resolver cannot re-derive does not belong here: an address nobody can check is not one.
REFERENCE_KINDS = ("table_cell", "prose_claim", "figure", "equation", "section_span")

# ambiguous != not_found: a quote occurring three times is a real quote at an address
# nobody can name, never silently resolved to the first occurrence.
REFERENCE_RESOLUTIONS = ("resolved", "not_found", "ambiguous", "malformed", "span_mismatch")


class QuantFinding(_Base):
    """One reported number, kept only when a verbatim source_quote anchors it."""

    benchmark: str = Field(default="", description="dataset/task the number is on")
    metric: str = Field(default="", description="e.g. 'top-1 accuracy', 'perplexity'")
    method: str = Field(default="", description="which method this number is for")
    value: str = Field(default="", description="the reported number (keep units)")
    baseline_value: str = Field(default="", description="the baseline it improves over")
    delta: str = Field(default="", description="reported improvement, e.g. '+2.1%'")
    seeds_or_variance: str = Field(default="", description="#seeds / std / CI, '' if unreported")
    dataset_scale: str = Field(default="", description="train size / #params / compute")
    source_quote: str = Field(default="", description="verbatim snippet the number came from")
    page: int = Field(default=0, description="1-indexed PDF page (0 = unknown)")
    table_ref: str = Field(default="", description="'T<t>:r<r>:c<c>' if it came from a table")


class Table(_Base):
    """One extracted table, addressable by (table_idx, row_idx, col_idx). `caption` is
    verbatim — never reconstructed as f'Table {n}: {rest}', which invents punctuation the
    paper does not have and flows unverified into two other fields (see INVARIANT_MAP.md)."""

    table_idx: int
    page: int = Field(default=0)
    caption: str = Field(default="", description="the caption line VERBATIM as printed")
    label: str = Field(default="", description="the printed number, e.g. '3' from 'Table 3'")
    caption_source: str = Field(default="none", description=" | ".join(CAPTION_SOURCES))
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
    """A figure CAPTION — never asserted as the plotted content; `caption_verified` is
    capped at LOW confidence for exactly that reason. `image_path` (when set) is a
    best-effort PNG crop, ADVISORY ONLY: a lens may `Read` it, but only a caption/table/
    prose quote the harness can re-verify may settle a finding."""

    figure_idx: int
    page: int = 0
    label: str = ""
    caption: str = ""
    image_path: str = ""

    def ref(self) -> str:
        return f"F{self.figure_idx}"


class Equation(_Base):
    """One extracted display equation, lossy by construction. Deliberately willing to find
    NOTHING: a wrongly assembled body would be a false `equation_verified` attestation."""

    equation_idx: int
    page: int = 0
    number: str = ""
    text: str = ""

    def ref(self) -> str:
        return f"E{self.equation_idx}"


class CrossRef(_Base):
    """One place the PROSE cites a numbered object — what the paper CITES, never what it
    CONTAINS. Carries no `exists`/`resolved`/`severity`: that comparison is another
    module's job (see `docs/HARNESS_ARCHITECTURE.md` on document-integrity)."""

    kind: str = Field(default="", description=" | ".join(SOURCE_UNIT_KINDS))
    number: str = Field(default="", description="the printed number, verbatim")
    page: int = 0
    section_idx: int = 0
    span: str = Field(default="", description="a P<i>:<a>-<b> address minted by `locate`")
    quote: str = Field(default="", description="the citing sentence, re-verifiable")


class PaperDoc(_Base):
    """The parsed paper. The ONLY thing a finding's evidence can be verified against.
    `content_sha` is the paper's identity as a DOCUMENT, since two files can slugify
    identically to the same `paper_id`."""

    paper_id: str
    title: str = ""
    source_path: str = ""
    content_sha: str = Field(default="", description="sha256 of the PDF bytes, first 12 hex")
    n_pages: int = 0
    sections: list[Section] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    equations: list[Equation] = Field(default_factory=list)
    reported_numbers: list[QuantFinding] = Field(default_factory=list)
    crossrefs: list[CrossRef] = Field(default_factory=list)
    body_end_section_idx: int = Field(
        default=-1, description="index of the References heading; -1 = not found")
    extraction_version: int = Field(
        default=1, description="bumped when address numbering/scope changes; a ref minted "
                               "under an older parse must not resolve against a newer one")
    repo_url: str = Field(default="", description="the official repo the paper advertises")
    repo_urls: list[str] = Field(
        default_factory=list, description="every candidate URL found, best first — NOT the "
                                          "same list as `repo_url`, which requires a cue word")


class ReportedQuantity(_Base):
    """A number parsed out of one reference's text. Willing to find NOTHING: two numbers
    with no stated relation is two numbers, not a reported quantity."""

    value: float | None = Field(default=None, description="or None if none is unambiguous")
    raw: str = Field(default="", description="the token the value was parsed from, verbatim")
    operands: list[float] = Field(default_factory=list, description="LHS numbers of a composition")
    expression: str = Field(default="", description="e.g. '58*5*10', or '' if none stated")
    arithmetic_ok: bool | None = Field(
        default=None, description="does `expression` evaluate to `value`? None = nothing to check")


class ClaimRef(_Base):
    """One resolvable address into the parsed paper. A lens supplies a QUOTE; the harness
    mints the ADDRESS (`locate.mint`) and re-reads the span off the document
    (`locate.resolve`). Grammar: `T<t>:r<r>:c<c>` table_cell · `F<n>` figure · `E<n>`
    equation · `S<i>` section_span · `P<i>:<start>-<end>` prose_claim."""

    ref: str = Field(default="")
    kind: str = Field(default="", description=" | ".join(REFERENCE_KINDS))
    quote: str = Field(default="")
    section_idx: int = Field(default=-1, description="-1 when not section-anchored")
    span: tuple[int, int] | None = Field(default=None, description="prose_claim only")
    page: int = Field(default=0)
    resolution: str = Field(default="malformed", description=" | ".join(REFERENCE_RESOLUTIONS))
    detail: str = Field(default="")
    quantity: ReportedQuantity | None = Field(default=None)

    @property
    def resolved(self) -> bool:
        return self.resolution == "resolved"


# === 2. AUDIT — the four scientific lenses, and the second, blinded grader =============

LENSES = ("overclaim", "protocol", "confound", "contradiction")

# NOTE is not a defect grade: a candidate that survived verification but threatens no
# claim (an open question, a figure-only observation) is PRINTED without being COUNTED.
SEVERITIES = ("FATAL", "MAJOR", "MINOR", "NOTE")
CONFIDENCES = ("HIGH", "MEDIUM", "LOW")

# Only CONFIRMED_FINDING may carry FATAL/MAJOR (`grading.CANDIDATE_CAP`). DISMISSED is
# kept as a reportable bucket, not dropped at the source.
CANDIDATE_CLASSES = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "DISMISSED")

BASELINE_CLASSES = ("OPTIONAL_COMPARISON", "USEFUL_CONTROL", "IMPORTANT_MISSING_BASELINE",
                    "CENTRAL_VALIDITY_THREAT", "NOT_APPLICABLE")
PRIOR_ART_BASES = ("PAPER_INTERNAL", "EXTERNAL_VERIFIED", "REVIEWER_INFERENCE", "NOT_APPLICABLE")
DISCREPANCY_TYPES = ("ARITHMETIC_ERROR", "DEFINITIONAL_MISMATCH", "DIFFERENT_DENOMINATOR",
                     "UNCLEAR_REPORTING", "GENUINE_CONTRADICTION", "NOT_A_DISCREPANCY",
                     "NOT_APPLICABLE")
EVIDENCE_ORIGINS = ("PAPER_TABLE", "PAPER_TEXT", "PAPER_FIGURE", "PAPER_EQUATION",
                    "EXTERNAL_LITERATURE", "REVIEWER_INFERENCE")

# HARNESS-WRITTEN, from the shape of `evidence_ref`, never trusted from a lens.
EVIDENCE_CLASSES = ("cell_verified", "prose_verified", "caption_verified", "equation_verified",
                    "unverified")
VERIFICATION_STATES = ("complete", "incomplete", "legacy")
CALC_CLASSES = ("recomputed_ok", "recomputed_mismatch", "operands_unverified", "unparseable",
                "not_applicable", "not_attempted")

GRADE_VERDICTS = ("CONFIRMED", "PLAUSIBLE", "REFUTED", "INSUFFICIENT")
# A DIFFERENT vocabulary from SEVERITIES (has NONE, not NOTE) — Grade.severity is the
# grader's own field and is never unioned with Finding.severity.
GRADE_SEVERITIES = ("FATAL", "MAJOR", "MINOR", "NONE")
RESOLUTIONS = ("COUNT_AS_FINDING", "REPORT_AS_CONCERN", "REPORT_AS_QUESTION", "DROP")
FINDING_CLASSES = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "REFUTED", "UNGRADED")


class Grade(_Base):
    """A second, blinded reviewer's read of ONE candidate finding. Every field is a
    MODEL's claim — what the harness derives from it lives on `Finding` instead
    (`finding_class`, `counted_severity`, ... — see `harness/grading.py`)."""

    verdict: str = Field(default="", description=" | ".join(GRADE_VERDICTS))
    severity: str = Field(default="", description=" | ".join(GRADE_SEVERITIES))
    confidence: str = Field(default="", description=" | ".join(CONFIDENCES))
    impact_statement: str = ""
    falsification: str = ""
    falsification_survived: bool = False
    steelman: str = ""
    independent_evidence_ref: str = ""
    independent_evidence_quote: str = ""
    reached_independently: bool = False
    resolution: str = Field(default="", description="ADVISORY ONLY: " + " | ".join(RESOLUTIONS))
    open_question: str = ""
    notes: str = ""


class EvidencePointer(_Base):
    """One SIDE of a cross-section concern. Each side resolves against the paper
    independently; a finding with an unresolvable side is dropped whole."""

    role: str = Field(default="", description="the lens's own label for this side")
    evidence_quote: str = Field(default="")
    evidence_ref: str = Field(default="")
    evidence_class: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(EVIDENCE_CLASSES))
    verified_observation: str = Field(default="", description="WRITTEN BY THE HARNESS")


class Finding(_Base):
    """One defect a lens asserts. Three groups of fields, and confusing them is the whole
    risk of this type: WRITTEN BY THE LENS, VERIFIED (`evidence_quote`/`evidence_ref`) ·
    WRITTEN BY THE HARNESS (`verified_observation`/`evidence_class`, and everything below
    `grade`) · WRITTEN BY THE LENS, UNVERIFIED (`claim`, `reasoning`, `conclusion`,
    `severity`, and the pass-B fields)."""

    finding_id: str = ""
    lens: str = Field(default="", description=" | ".join(LENSES))
    severity: str = Field(default="MINOR", description=" | ".join(SEVERITIES))
    title: str = Field(default="", description="one compressed line, <=90 chars")
    statement: str = Field(default="")
    target: str = Field(default="", description="the claim or cell under attack, verbatim")
    evidence_quote: str = Field(default="")
    evidence_ref: str = Field(default="", description="'p7' or 'T2:r3:c4'")
    counter_explanations: list[str] = Field(default_factory=list)
    verifiable_by_experiment: bool = Field(default=False, description="unchecked model boolean")
    additional_evidence: list[EvidencePointer] = Field(default_factory=list)

    claim: str = Field(default="", description="falls back to `target`")
    verified_observation: str = Field(default="", description="WRITTEN BY THE HARNESS")
    evidence_class: str = Field(default="unverified", description="WRITTEN BY THE HARNESS: " + " | ".join(EVIDENCE_CLASSES))
    reasoning: str = Field(default="", description="falls back to `statement`")
    conclusion: str = Field(default="", description="falls back to `statement`")
    severity_rationale: str = Field(default="")

    candidate_class: str = Field(default="", description=" | ".join(CANDIDATE_CLASSES))
    confidence: str = Field(default="", description=" | ".join(CONFIDENCES))
    discrepancy_type: str = Field(default="", description=" | ".join(DISCREPANCY_TYPES))
    baseline_class: str = Field(default="", description=" | ".join(BASELINE_CLASSES))
    prior_art_basis: str = Field(default="", description=" | ".join(PRIOR_ART_BASES))
    what_the_paper_says: str = Field(default="")
    alternative_interpretation: str = Field(default="")
    why_alternative_fails: str = Field(default="")
    steelman: str = Field(default="")
    effect_on_claim: str = Field(default="")
    recommended_resolution: str = Field(default="")
    independent_calculation: dict = Field(default_factory=dict)

    scientific_class: str = Field(default="UNRESOLVED_QUESTION",
                                  description="WRITTEN BY THE HARNESS: " + " | ".join(SCIENTIFIC_CLASSES))
    verification_state: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(VERIFICATION_STATES))
    calc_class: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(CALC_CLASSES))
    evidence_origin: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(EVIDENCE_ORIGINS))
    origin_consistency: str = Field(default="")
    cross_section: bool = Field(default=False, description="WRITTEN BY THE HARNESS")
    source_part: str = Field(default="", description="WRITTEN BY THE HARNESS")
    merged_from: list[str] = Field(default_factory=list, description="WRITTEN BY THE HARNESS")

    grade: Grade | None = Field(default=None)
    finding_class: str = Field(default="UNGRADED", description="WRITTEN BY THE HARNESS: " + " | ".join(FINDING_CLASSES))
    counted_severity: str = Field(default="", description="WRITTEN BY THE HARNESS; '' = ungraded")
    grade_state: str = Field(default="not_graded")
    binding_cap: str = Field(default="")
    derivation: str = Field(default="")
    grader_evidence_class: str = Field(default="unverified")
    grader_verified_observation: str = Field(default="")
    evidence_ceiling: str = Field(default="")
    evidence_sources: list[str] = Field(default_factory=list)

    def as_reasoning(self) -> str:
        return (self.reasoning or self.statement or "").strip()

    def as_conclusion(self) -> str:
        return (self.conclusion or self.statement or "").strip()

    def as_claim(self) -> str:
        return (self.claim or self.target or "").strip()


class LensReport(_Base):
    lens: str
    schema_version: int = Field(default=0, description="0 = pre-contract file")
    findings: list[Finding] = Field(default_factory=list)
    unasked_question: str = Field(default="")
    notes: str = Field(default="")
    merged_duplicates: int = Field(default=0, description="WRITTEN BY THE HARNESS")


# === 3. REIMPLEMENTATION — governed reconstruction, when no repository is published ====

REIMPL_INGREDIENT_KINDS = ("method", "architecture", "preprocessing", "training", "dataset",
                          "metric", "hyperparameters", "comparison_target")
# The REQUIRED subset only — every binding must be one of these.
REIMPL_BINDING_KINDS = ("method", "training", "dataset", "metric", "comparison_target")


class ReimplementationIngredient(_Base):
    kind: str = Field(description=" | ".join(REIMPL_INGREDIENT_KINDS))
    required: bool = Field(default=True)
    present: bool = False
    ref: str = Field(default="", description="s<N> section, E<N> equation, or T<N> table")
    quote: str = Field(default="")


class ReimplementationReadiness(_Base):
    """Whether PATH B is open — an ELIGIBILITY decision, never a claim about the paper."""

    established: bool = False
    ingredients: list[ReimplementationIngredient] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list, description="non-empty => NOT_VERIFIED")
    reason: str = ""


class ReimplementationBinding(_Base):
    """One required ingredient tied to BOTH a paper locator and an implementation locator.
    `verified` is WRITTEN BY THE HARNESS, never trusted from the driver's own report."""

    kind: str = Field(description=" | ".join(REIMPL_BINDING_KINDS))
    paper_ref: str = ""
    paper_quote: str = ""
    impl_ref: str = Field(default="", description="line/function in the generated script")
    impl_quote: str = Field(default="")
    verified: bool = Field(default=False, description="WRITTEN BY THE HARNESS")
    bound: bool = Field(default=False, description="paper_ref, impl_ref and verified all hold")


class ReimplementationConformance(_Base):
    """The gate `reimpl_exec` provenance adds: every required ingredient bound AND
    verified, before `local_exec.reconcile`/`backends.authorize` may draw a verdict from
    a reconstruction. A conformant reconstruction's disagreement is about the paper's
    STATED METHOD — see `PROVENANCE_LABEL['reimpl_exec']`, never AUTHOR_REPOSITORY."""

    established: bool = False
    verified_by: str = Field(default="", description="'' = never independently assessed")
    generated_by: str = Field(default="", description="must differ from verified_by")
    independently_verified: bool = Field(default=False, description="WRITTEN BY THE HARNESS")
    bindings: list[ReimplementationBinding] = Field(default_factory=list)
    unbound: list[str] = Field(default_factory=list)
    reason: str = ""
    # The paper's own replication count for this experiment, as the generator declared it.
    # 0 = none declared; used only after `routes` re-finds `replication_quote` in the paper.
    replication_runs: int = 0
    replication_quote: str = ""


# === 4. REPO ACQUISITION + STATIC AUDIT — what the checkout is, and what it demands ====

REPO_ACQUISITION_STATUSES = ("cloned", "cached", "synthesized", "unavailable", "blocked",
                             "failed", "not_attempted")
# "empty_environment": a bare venv with none of the repo's declared deps installed —
# distinct from "ready", which used to be set unconditionally (see INVARIANT_MAP.md).
ENV_STATUSES = ("ready", "blocked", "failed", "not_attempted", "empty_environment")

RUNTIME_KINDS = ("python_version", "cuda_runtime", "external_download", "model_artifact",
                 "dataset_artifact", "absolute_path", "scheduler_allocation")
RUNTIME_STATES = ("established", "ambiguous", "unknown", "not_applicable")
RUNTIME_SCOPES = ("experiment", "repository")

CODE_AUDIT_CATEGORIES = ("baseline_crippling", "data_leakage", "metric_deviation")


class RepoAcquisition(_Base):
    """Where the code under test came from. `status` "unavailable"/"blocked" are not
    accusations — a paper shipping no code is not thereby a bad paper."""

    url: str = ""
    status: str = Field(default="not_attempted", description=" | ".join(REPO_ACQUISITION_STATUSES))
    path: str = Field(default="", description="runs/<pid>/repo or .../standalone_probe.py")
    commit: str = Field(default="", description="the full 40-char SHA on disk")
    requested_revision: str = Field(default="", description="'' = default branch; not a pin")
    pinned: bool = Field(default=False)
    shallow: bool = Field(default=False, description="a depth-1 clone")
    reason: str = ""
    reimplementation: "ReimplementationReadiness | None" = Field(
        default=None, description="set only when NO repository was advertised")
    dependency_files: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    entrypoint: str = Field(default="")
    env_path: str = Field(default="")
    env_status: str = Field(default="not_attempted", description=" | ".join(ENV_STATUSES))
    env_backend: str = Field(default="", description="which backend BUILT this environment")


class RuntimeDemand(_Base):
    """One runtime demand read out of the checkout. `state` and `value` separate on
    purpose: established-with-a-value, established-as-conflicting, and absent are three
    different facts a reader must be able to tell apart."""

    kind: str = Field(default="", description=" | ".join(RUNTIME_KINDS))
    state: str = Field(default="unknown", description=" | ".join(RUNTIME_STATES))
    value: str = Field(default="")
    scope: str = Field(default="repository", description=" | ".join(RUNTIME_SCOPES))
    file: str = Field(default="")
    line: int = Field(default=0)
    code_quote: str = Field(default="")
    note: str = Field(default="")


class CodeAuditFinding(_Base):
    """One static-inspection hit, quoted from source like a lens quotes the paper."""

    finding_id: str = ""
    rule_id: str = Field(default="")
    category: str = Field(default="", description=" | ".join(CODE_AUDIT_CATEGORIES))
    severity: str = Field(default="MINOR", description=" | ".join(SEVERITIES))
    title: str = Field(default="")
    statement: str = ""
    file: str = Field(default="")
    line: int = 0
    code_quote: str = Field(default="")
    counter_explanations: list[str] = Field(default_factory=list)

    @property
    def scientific_class(self) -> str:
        """Always IMPLEMENTATION_ISSUE — a property, never a settable field, so a static
        hit cannot be classified as anything but a statement about the released artifact."""
        from . import taxonomy
        return taxonomy.classify(is_artifact_finding=True)


class CodeAudit(_Base):
    """The static pass. No execution, so it is safe on an untrusted clone."""

    repo_path: str = ""
    commit: str = Field(default="", description="the checkout's HEAD at the moment of audit")
    files_scanned: int = 0
    lines_scanned: int = 0
    declarations_scanned: list[str] = Field(default_factory=list)
    findings: list[CodeAuditFinding] = Field(default_factory=list)
    runtime: list[RuntimeDemand] = Field(default_factory=list)
    unparseable: list[str] = Field(default_factory=list)
    skipped: str = Field(default="")


# === 5. STATIC ARTIFACT INSPECTION — what the released code can establish, and its ceiling ===

# THREE LEVELS. Level 3 ("the reported result is false") HAS NO SPELLING here — the
# encoding of the rule, not a note about it. See INVARIANT_MAP.md.
ARTIFACT_AUTHORITY = ("ARTIFACT_FACT", "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN",
                      "PAPER_ARTIFACT_MISMATCH", "NONE")
ARTIFACT_IDENTITY_STATES = ("ESTABLISHED", "PARTIAL", "AMBIGUOUS", "UNBOUND")
ARTIFACT_IDENTITY_BASES = ("paper_names_the_command", "readme_maps_the_experiment",
                          "script_passes_the_config", "authors_experiment_table",
                          "auditor_assertion")
ARTIFACT_QUESTION_SCOPES = ("FILE_PRESENCE", "ENTRYPOINT_PRESENCE", "DEPENDENCY_DECLARED",
                           "MANIFEST_PRESENCE", "CONFIG_LITERAL", "COMMAND_PRESENCE",
                           "IMPLEMENTATION_CORRESPONDENCE")
# IMPLEMENTATION_CORRESPONDENCE excluded BY CONSTRUCTION — the fix, not a note about it.
SETTLEABLE_BY_ARTIFACT_FACT = tuple(
    s for s in ARTIFACT_QUESTION_SCOPES if s != "IMPLEMENTATION_CORRESPONDENCE")
MISMATCH_REFUSALS = ("paper_statement_unaddressed", "artifact_fact_unlocated",
                     "experiment_identity_unbound", "experiment_identity_not_deterministic",
                     "values_not_comparable", "paper_value_not_derivable",
                     "artifact_value_not_derivable", "no_disagreement")


class SourceSpan(_Base):
    """Where in the PINNED checkout something is, re-derivable by hand from the SHA.
    Located by the harness, never asserted by a writer — `artifact_evidence.relocate` is
    `locate.mint` for source."""

    file: str = Field(default="")
    line: int = Field(default=0)
    end_line: int = Field(default=0)
    char_span: tuple[int, int] | None = Field(default=None)
    quote: str = Field(default="")
    file_sha256: str = Field(default="")
    node_type: str = Field(default="")


class ArtifactSnapshot(_Base):
    """The immutable checkout every artifact fact is tied to, RECORDED BEFORE ANYTHING
    RUNS. `dirty` defaults True: an unknown tree is not an audited tree."""

    repo_url: str = ""
    commit: str = Field(default="")
    tree_sha: str = Field(default="")
    dirty: bool = Field(default=True)
    captured_at: str = ""
    note: str = Field(default="")

    @property
    def audited(self) -> bool:
        return bool(self.commit) and bool(self.tree_sha) and not self.dirty


class ArtifactFact(_Base):
    """One thing established about the released artifact. `authority` is WRITTEN BY THE
    HARNESS from what was actually located, never from a rule's own opinion of itself."""

    fact_id: str = ""
    probe: str = Field(default="")
    settles: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(ARTIFACT_QUESTION_SCOPES))
    statement: str = Field(default="")
    authority: str = Field(default="NONE", description="WRITTEN BY THE HARNESS: " + " | ".join(ARTIFACT_AUTHORITY))
    snapshot: ArtifactSnapshot | None = None
    span: SourceSpan | None = None
    paper_ref: str = Field(default="")
    paper_quote: str = Field(default="")
    paper_value: str = ""
    artifact_value: str = ""
    experiment_id: str = Field(default="", description="'' means unbound")
    identity_state: str = Field(default="UNBOUND", description="WRITTEN BY THE HARNESS: " + " | ".join(ARTIFACT_IDENTITY_STATES))
    identity_basis: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(ARTIFACT_IDENTITY_BASES))
    identity_span: SourceSpan | None = Field(default=None, description="WRITTEN BY THE HARNESS")
    refusal: str = Field(default="", description=" | ".join(MISMATCH_REFUSALS))
    counter_explanations: list[str] = Field(default_factory=list)

    @property
    def about_the_paper(self) -> bool:
        return self.authority == "PAPER_ARTIFACT_MISMATCH"

    @property
    def endpoints_only(self) -> bool:
        return self.authority == "ENDPOINTS_VERIFIED_ARTIFACT_CONCERN"


class ArtifactInspection(_Base):
    """One ARTIFACT_INSPECTION route attempt. "The repository cloned" is not artifact
    evidence — `discharged` requires a bounded question, a look, and an answer."""

    paper_id: str = ""
    target_id: str = ""
    snapshot: ArtifactSnapshot | None = None
    facts: list[ArtifactFact] = Field(default_factory=list)
    files_examined: list[str] = Field(default_factory=list)
    statements_examined: list[str] = Field(default_factory=list)
    question_scope: str = Field(default="", description=" | ".join(ARTIFACT_QUESTION_SCOPES))
    discharged: bool = Field(default=False, description="WRITTEN BY THE HARNESS")
    reason: str = Field(default="")
    escalations: list[str] = Field(default_factory=list)
    proposed: int = Field(default=0)
    relocated: int = Field(default=0)

    def bound_mismatches(self) -> list[ArtifactFact]:
        return [f for f in self.facts if f.about_the_paper]

    def endpoint_concerns(self) -> list[ArtifactFact]:
        return [f for f in self.facts if f.endpoints_only]


# === 6. IDENTITY + CAPABILITY — which experiment, which quantity, can this machine run it ===

IDENTITY_STATES = ("established", "ambiguous", "no_candidate", "unmapped", "unsupported")
COMMAND_SOURCES = ("readme", "run_script", "scripts_dir", "makefile")
CELL_QUANTITIES = ("accuracy", "memory", "latency", "flops", "macs", "loss", "params")
CELL_BASES = ("absolute", "relative_to_baseline")

CAPABILITY_CODES = ("established", "not_attempted", "environment_incompatible",
                    "dependency_missing", "invalid_invocation", "resources_insufficient",
                    "commit_mismatch")


class IdentityEvidence(_Base):
    """One quoted justification for an identity claim, checkable like S2 evidence."""

    quote: str = ""
    source_ref: str = ""
    note: str = ""


class CandidateCommand(_Base):
    """A command the REPOSITORY advertises, discovered rather than invented."""

    argv: list[str] = Field(default_factory=list)
    source: str = Field(default="", description=" | ".join(COMMAND_SOURCES))
    source_ref: str = Field(default="")
    declared_args: dict[str, str] = Field(default_factory=dict)
    declared_args_evidence: dict[str, str] = Field(default_factory=dict)
    seed_flag: str = Field(default="")
    seed_values: list[str] = Field(default_factory=list)
    emits: list[str] = Field(default_factory=list)
    label: str = Field(default="")


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
    row_method: str = Field(default="")
    considered: int = Field(default=0)
    rejected: list[str] = Field(default_factory=list)


class MetricIdentity(_Identity):
    """That the executed number is the SAME quantity, on the same basis, as the cell.
    `basis` matters as much as `quantity`: 253.6% relative-to-baseline is not the same
    number as an absolute memory reading, however correctly the latter is measured."""

    cell_quantity: str = Field(default="", description=" | ".join(CELL_QUANTITIES))
    cell_basis: str = Field(default="", description=" | ".join(CELL_BASES))
    cell_unit: str = ""
    output_key: str = Field(default="")
    output_quantity: str = ""
    output_basis: str = ""
    requires_arms: list[str] = Field(default_factory=list)


class ConfigurationIdentity(_Identity):
    matched: dict[str, str] = Field(default_factory=dict)
    unrecoverable: list[str] = Field(default_factory=list)
    seed_policy_paper: str = ""
    seed_policy_repo: str = ""
    seed_policy_match: bool | None = None


class ExecCapability(_Base):
    """Whether this machine can give the paper's own code a fair run. Recorded whether or
    not execution is attempted — "we did not try" and "we tried and broke" are different."""

    established: bool = False
    reason_code: str = Field(default="not_attempted", description=" | ".join(CAPABILITY_CODES))
    detail: str = Field(default="")
    env_status: str = Field(default="", description="RepoAcquisition.env_status at planning time")
    interpreter: str = Field(default="")
    interpreter_is_repo_env: bool = Field(default=False)
    entrypoint: str = ""
    checked_imports: list[str] = Field(default_factory=list)
    missing_dependencies: list[str] = Field(default_factory=list)
    accepts_seed_argument: bool | None = Field(default=None)
    declared_platform: str = Field(default="")
    current_platform: str = Field(default="", description="the platform the run would actually SEE")
    backend: str = Field(default="")


# === 7. COMPARISON + RESOURCES — what a result is held against, and whether it fits ====

COMPARISON_KINDS = ("AGAINST_PRINTED_VALUE", "AGAINST_EXISTENCE", "AGAINST_SPECIFICATION",
                    # A theorem/bound's claimed inequality, checked by an exact-arithmetic
                    # certificate against a concrete instance — never a printed cell.
                    "AGAINST_CLAIMED_BOUND")
COMPARISON_STATES = ("established", "no_reference", "unsupported", "unmapped")
RESOURCE_STATES = ("satisfied", "insufficient", "unknown", "unassessed")
RESOURCE_EVIDENCE_KINDS = ("declared_requirement", "declared_hardware", "derived_floor")
GIB = 1024 ** 3


class Comparison(_Base):
    """What one target's result would be held against. Derived by `harness.decide` from
    the ROUTE, never from the question and never from a model."""

    kind: str = Field(default="", description=" | ".join(COMPARISON_KINDS))
    state: str = Field(default="unmapped", description=" | ".join(COMPARISON_STATES))
    measured: str = Field(default="")
    reference: str = Field(default="")
    reason: str = Field(default="")

    @property
    def established(self) -> bool:
        return self.state == "established"


class ResourceEvidence(_Base):
    quote: str = Field(default="")
    source_ref: str = Field(default="")
    kind: str = Field(default="", description=" | ".join(RESOURCE_EVIDENCE_KINDS))
    note: str = ""


class ResourceRequirement(_Base):
    """What the CITED experiment demands. Every field optional: an unstated demand is
    `None`, reported as unknown, never as zero (which would authorize on the paper's
    silence)."""

    vram_bytes: int | None = None
    ram_bytes: int | None = None
    disk_bytes: int | None = None
    cpu_count: int | None = None
    gpu_count: int | None = None
    gpu_model: str = Field(default="")
    walltime_s: int | None = Field(default=None)
    model_scale: str = Field(default="")
    vram_is_floor_only: bool = Field(default=False, description="fp16 weight lower bound only")
    evidence: list[ResourceEvidence] = Field(default_factory=list)
    unstated: list[str] = Field(default_factory=list)

    @property
    def stated(self) -> bool:
        return any(v is not None for v in (self.vram_bytes, self.ram_bytes, self.disk_bytes,
                                           self.cpu_count, self.gpu_count, self.walltime_s))

    @property
    def memory_stated(self) -> bool:
        """Whether the quantity that actually decides fit is established — `stated` is an
        OR over six fields and let an unstated VRAM demand match vacuously."""
        return self.vram_bytes is not None or self.ram_bytes is not None


class ResourceCapability(_Base):
    """`satisfied` is the only green light. `unknown` BLOCKS — converting the paper's
    silence about cost into permission to run it is the inference this type refuses."""

    state: str = Field(default="unassessed", description=" | ".join(RESOURCE_STATES))
    reason: str = ""
    backend: str = ""
    requirement: ResourceRequirement | None = None
    shortfalls: list[str] = Field(default_factory=list)
    available_vram_bytes: int | None = None
    available_ram_bytes: int | None = None
    available_disk_bytes: int | None = None
    available_cpu_count: int | None = None
    available_gpu_count: int | None = None
    available_gpu_model: str = ""

    @property
    def established(self) -> bool:
        return self.state == "satisfied"


# === 8. EXECUTION — commit identity, the process record, authorization, reconciliation ===

COMMIT_STATES = ("verified", "mismatch", "dirty", "unknown", "unassessed")

# How a non-zero exit is accounted for. Only 'runtime_failure' may convict.
FAILURE_CLASSES = (
    "none", "environment_incompatible", "dependency_missing", "invalid_invocation",
    "startup_failure", "runtime_failure", "timeout", "experiment_unidentified",
    "metric_unbound", "configuration_unmatched", "comparison_unestablished",
    "execution_unauthorized", "resources_insufficient", "commit_mismatch",
    "backend_unavailable", "credentials_unavailable",
    "reimplementation_nonconformant", "infrastructure_failure",
)

# How an execution request was decided. `allowed` is the whole decision; the code names
# WHICH condition settled it.
EXEC_DECISIONS = (
    "authorized", "not_repo_execution", "gate_closed", "no_backend",
    "provenance_insufficient", "identity_unproven", "capability_unproven",
    "resources_unproven", "commit_unverified", "backend_cannot_execute",
    "backend_mismatch", "backend_offline", "spec_incomplete",
    "isolation_insufficient", "conformance_unproven", "commit_changed_during_execution",
)

RECONCILIATION_STATUSES = ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION", "INCONCLUSIVE",
                          "NOT_ATTEMPTED",
                          # EXACT_CERTIFICATE's own two terminal states, about a claimed
                          # bound, not a printed cell: "found" is a violating instance;
                          # "none found" checks tested instances, never proves the bound.
                          "COUNTEREXAMPLE_FOUND", "NO_VIOLATION_FOUND")
PROBE_VERDICTS = ("detectable", "within_noise", "calibration", "degenerate", "failed", "single_arm",
                 "not_started")


class CommitVerification(_Base):
    """That the checkout about to run is the one the static audit and identity layer
    read. A SHA recorded after cloning proves what arrived, not what was asked for."""

    state: str = Field(default="unassessed", description=" | ".join(COMMIT_STATES))
    expected: str = Field(default="")
    actual: str = Field(default="")
    dirty_files: list[str] = Field(default_factory=list)
    shallow: bool = Field(default=False)
    reason: str = ""

    @property
    def established(self) -> bool:
        return self.state == "verified"


class ExecutionRecord(_Base):
    """One process this harness started, kept WHOLE — full stdout/stderr, untruncated —
    written to `runs/<pid>/execution.jsonl` for every attempt, success or failure."""

    seed: int = 0
    arm: str = ""
    backend: str = ""
    argv: list[str] = Field(default_factory=list)
    launch_argv: list[str] = Field(default_factory=list, description="e.g. the full `docker run` line")
    cwd: str = Field(default="")
    interpreter: str = Field(default="")
    environment: str = Field(default="", description="the backend's own account of WHERE this ran")
    commit: str = Field(default="")
    provenance: str = Field(default="", description="whose program this record is of")
    started_at: str = ""
    ended_at: str = ""
    seconds: float = 0.0
    launched: bool = Field(default=False)
    completed: bool = Field(default=False)
    timed_out: bool = False
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = Field(default="")
    metric: float | None = Field(default=None)


# `TargetOutcome.provenance` is broader than the reproduction ceiling's domain: "paper"
# (the ARITHMETIC_RECHECK route; nothing executed) and "artifact" (the static-inspection
# route's token) are current, intentional, non-reproduction provenances added to this
# field's OWN vocabulary rather than `LEGACY_VALUES`, reserved for genuine past errors.
TARGET_OUTCOME_PROVENANCE_VALUES = PROVENANCE_VALUES + ("paper", "artifact")

# Enforced-field type aliases for the four ladder objects below — see `_vocab_validator`'s
# own docstring for why each must be a literal `Annotated[...]`, not a call returning one.
_ExecDecisionField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(EXEC_DECISIONS, "ExecAuthorization.decision"))]
_ExecFailureClassField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(FAILURE_CLASSES, "ExecAuthorization.failure_class"))]
_ReconciliationProvenanceField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(PROVENANCE_VALUES, "Reconciliation.provenance"))]
_ReconciliationFailureClassField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(FAILURE_CLASSES, "Reconciliation.failure_class"))]
_ProbeSpecProvenanceField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(PROVENANCE_VALUES, "ProbeSpec.provenance"))]
_TargetOutcomeProvenanceField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(TARGET_OUTCOME_PROVENANCE_VALUES, "TargetOutcome.provenance"))]
_TargetOutcomeFailureClassField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(FAILURE_CLASSES, "TargetOutcome.failure_class"))]


class ExecAuthorization(_Base):
    """Whether an execution request may proceed — the record of a refusal, not just a
    bool. See `harness/execute.py`'s `authorize()` for the ordered ladder that fills this."""

    allowed: bool = False
    decision: _ExecDecisionField = "gate_closed"
    backend: str = Field(default="")
    failure_class: _ExecFailureClassField = "execution_unauthorized"
    detail: str = Field(default="")


class Reconciliation(_Base):
    """Executed number vs the table cell the paper printed. `status` is decided
    arithmetically from `delta_error` against the measured noise band — never judgement."""

    table_ref: str = Field(default="")
    claim_ref: str = Field(default="", description="equal to table_ref when the address is a cell")
    claim_kind: str = Field(default="", description=" | ".join(REFERENCE_KINDS))
    target_id: str = ""
    finding_id: str = ""
    metric: str = ""
    claimed_value: float | None = Field(default=None)
    claimed_raw: str = Field(default="")
    reproduced_value: float | None = Field(default=None)
    reproduced_std: float | None = None
    seeds_run: list[int] = Field(default_factory=list)
    delta_error: float | None = Field(default=None)
    claimed_precision: float | None = Field(
        default=None, description="half the rounding interval the paper's own digits imply")
    noise_band: float = Field(default=0.0, description="2 * seed-to-seed sigma")
    status: str = Field(default="NOT_ATTEMPTED", description=" | ".join(RECONCILIATION_STATUSES))
    provenance: _ReconciliationProvenanceField = ""
    failure_class: _ReconciliationFailureClassField = "none"
    experiment_state: str = Field(default="unmapped")
    metric_state: str = Field(default="unmapped")
    configuration_state: str = Field(default="unmapped")
    comparison_kind: str = Field(default="", description=" | ".join(COMPARISON_KINDS))
    comparison_state: str = Field(default="")
    reached_experiment: bool | None = Field(default=None)
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
    device: str = Field(default="unknown", description="cuda / mps / cpu")
    seeds_run: list[int] = Field(default_factory=list)
    seeds_failed: list[int] = Field(default_factory=list)
    arms: dict[str, ArmStats] = Field(default_factory=dict)
    measured_delta: float = Field(default=0.0)
    measured_std: float = Field(default=0.0)
    noise_band: float = Field(default=0.0)
    is_overclaimed: bool | None = Field(default=None)
    calibration: bool | None = Field(default=None, description="identical-arms noise-floor run")
    claimed_delta: float | None = Field(default=None)
    claim_within_noise: bool | None = Field(default=None)
    verdict: str = Field(default="", description=" | ".join(PROBE_VERDICTS))
    reason: str = ""
    seconds: float = 0.0
    script_path: str = ""
    repo: RepoAcquisition | None = Field(default=None)
    code_audit: CodeAudit | None = Field(default=None)
    reconciliation: Reconciliation | None = Field(default=None)
    provenance: str = Field(default="template")
    mechanism: str = Field(default="")
    rationale: str = Field(default="")
    capability: ExecCapability | None = Field(default=None)
    backend: str = Field(default="local")
    execution_log: str = Field(default="", description="runs/<pid>/execution.jsonl")
    executions: int = Field(default=0, description="how many processes were started")
    backend_selection: str = Field(default="")
    backend_considered: list[str] = Field(default_factory=list)
    commit_state: str = Field(default="unassessed")
    resources: ResourceCapability | None = Field(default=None)
    commit_verification: CommitVerification | None = Field(default=None)
    authorization: ExecAuthorization | None = Field(default=None)
    experiment: ExperimentIdentity | None = None
    metric_identity: MetricIdentity | None = None
    configuration: ConfigurationIdentity | None = None
    aux: dict[str, dict[str, ArmStats]] = Field(default_factory=dict)


# Why a backend was or was not selected. `backend_unavailable` is a TRANSIENT property of
# the world (worth a retry); every other code is permanent for this experiment/registry.
SELECTION_CODES = ("selected", "no_backend", "resources_insufficient", "platform_incompatible",
                   "credentials_unavailable", "backend_unavailable", "requirement_unknown")


class ProbeSpec(_Base):
    """What to actually run locally to test one finding. `script` is the escape hatch;
    empty measures this machine's seed noise floor instead."""

    paper_id: str
    finding_id: str = Field(default="")
    claim: str = Field(default="")
    claimed_delta: float | None = Field(default=None)
    metric: str = Field(default="accuracy")
    arms: list[str] = Field(default_factory=lambda: ["baseline", "treatment"])
    seeds: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    dataset: str = Field(default="digits")
    epochs: int = Field(default=30)
    script: str = Field(default="", description="empty = default template")
    command: list[str] = Field(default_factory=list, description="the repo's own eval command")
    cwd: str = Field(default="")
    interpreter: str = Field(default="", description="'' = cfg.python")
    table_ref: str = Field(default="")
    claim_ref: str = Field(default="", description="any address `locate.resolve` can re-derive")
    claim_kind: str = Field(default="", description=" | ".join(REFERENCE_KINDS))
    claimed_cell_value: str = Field(default="")
    target_id: str = Field(default="")
    written_by: str = Field(default="", description="'harness' = persisted by `execute.py` itself")
    provenance: _ProbeSpecProvenanceField = Field(
        default="template",
        description="template=noise floor · synthesized=planner-authored from the paper · "
                    "driver=human-written · repo_exec=authors' checkout · "
                    "reimpl_exec=governed reconstruction, admissible only with "
                    "reimplementation_conformance.established")
    mechanism: str = Field(default="")
    rationale: str = Field(default="")
    aux_metrics: list[str] = Field(default_factory=list)
    capability: ExecCapability | None = Field(default=None)
    experiment: ExperimentIdentity | None = Field(default=None)
    metric_identity: MetricIdentity | None = Field(default=None)
    configuration: ConfigurationIdentity | None = Field(default=None)
    comparison: Comparison | None = Field(default=None)
    backend: str = Field(default="local")
    resources: ResourceCapability | None = Field(default=None)
    commit: str = Field(default="", description="the audited SHA execution is verified against")
    commit_state: str = Field(default="unassessed")
    backend_selection: str = Field(default="", description=" | ".join(SELECTION_CODES))
    backend_considered: list[str] = Field(default_factory=list)
    reimplementation_conformance: "ReimplementationConformance | None" = Field(
        default=None, description="set only for provenance 'reimpl_exec'")
    # EXACT_CERTIFICATE reuses `ReimplementationConformance`'s shape verbatim: the
    # `cert_exec` gate is structurally identical, every REQUIRED element bound to a
    # verified locator AND independently verified before a verdict may be drawn.
    certificate_conformance: "ReimplementationConformance | None" = Field(
        default=None, description="set only for provenance 'cert_exec'")


# === 9. REPORT — the reader-facing rows, the verdict, the whole-paper opinion ==========

FINDING_STATES = ("MATERIAL_FAILURE_ESTABLISHED", "CONCERNS_RECORDED",
                  "NO_CONCERN_SURVIVED_VERIFICATION")
QUESTION_STATES = ("ALL_QUESTIONS_SETTLED", "SOME_QUESTIONS_SETTLED", "NO_QUESTION_SETTLED",
                  "NO_QUESTION_RAISED")
EXECUTION_STATES = ("EXECUTION_CONTRADICTED_A_PRINTED_QUANTITY",
                    "EXECUTION_REPRODUCED_A_PRINTED_QUANTITY",
                    "EXECUTION_PRODUCED_NO_ADMISSIBLE_EVIDENCE",
                    "EXECUTION_BLOCKED_BEFORE_IT_STARTED",
                    "EXECUTION_WARRANTED_AND_NOT_ATTEMPTED", "NO_EXECUTION_WARRANTED")
SCOPE_STATES = ("CENTRAL_CLAIMS_LEFT_UNCHECKED", "SOME_TARGETS_PURSUED", "NO_TARGET_PURSUED")


class ScientificFinding(_Base):
    """One finding as a REVIEWER reads it — a PROJECTION of `Finding` + `ReviewQuestion` +
    `TargetOutcome`, assembled and never re-judged by `report.py`."""

    finding_id: str = ""
    scientific_class: str = Field(default="UNRESOLVED_QUESTION", description=" | ".join(SCIENTIFIC_CLASSES))
    title: str = ""
    statement: str = ""
    lens: str = Field(default="", description=" | ".join(LENSES))
    corroborating_lenses: list[str] = Field(default_factory=list)
    severity: str = Field(default="", description="counted_severity, or the lens's own if ungraded")
    confidence: str = Field(default="")
    claim_ref: str = Field(default="")
    evidence_quote: str = ""
    evidence_class: str = Field(default="")
    question_id: str = ""
    question: str = Field(default="")
    why_material: str = ""
    evidence_needed: str = Field(default="")
    resolution_status: str = Field(default="NOT_INVESTIGATED", description=" | ".join(RESOLUTION_STATES))
    evidence_state: str = Field(default="NOT_INVESTIGATED", description=" | ".join(EVIDENCE_STATES))
    experiment_necessity: str = Field(default="NO_EXPERIMENT_NEEDED")
    evidence_refs: list[str] = Field(default_factory=list)
    conclusion: str = Field(default="")


class ReviewOutcome(_Base):
    """The four rows a human reads first — four independent folds over disjoint inputs,
    so what a review ESTABLISHED and what happened during EXECUTION can never merge into
    one sentence. See `harness/report.py`'s `outcome_rows()`."""

    paper_id: str = ""
    execution_actor: str = Field(default="", description="derived from provenance alone")
    finding_state: str = Field(default="NO_CONCERN_SURVIVED_VERIFICATION", description=" | ".join(FINDING_STATES))
    finding_detail: str = ""
    question_state: str = Field(default="NO_QUESTION_RAISED", description=" | ".join(QUESTION_STATES))
    question_detail: str = ""
    execution_state: str = Field(default="NO_EXECUTION_WARRANTED", description=" | ".join(EXECUTION_STATES))
    execution_detail: str = Field(default="", description="the exact reason, carried verbatim")
    scope_state: str = Field(default="NO_TARGET_PURSUED", description=" | ".join(SCOPE_STATES))
    scope_detail: str = ""
    claim_status: str = Field(default="NOT_VERIFIED", description="carried, not decided here")


CLAIM_STATUSES = ("VERIFIED_FAILURE", "VERIFIED_SUPPORT", "NOT_VERIFIED")
REPRODUCTION_STATUSES = ("REPRODUCED", "FAILED_REPRODUCTION", "NOT_VERIFIED", "NOT_ATTEMPTED")
VERDICT_AGREEMENTS = ("agree", "harness_harsher", "model_harsher", "contested", "unavailable")
SUBSTANTIVE_VERDICTS = ("STRONG", "SOUND_WITH_MINOR_CONCERNS", "SUBSTANTIAL_CONCERNS",
                        "CENTRAL_CLAIM_NOT_ESTABLISHED", "INCONCLUSIVE")
WEAKNESS_SCOPES = ("LOCAL", "SYSTEMIC", "MIXED")
CONTRIBUTION_VERDICTS = ("YES", "YES_QUALIFIED", "NO", "UNDETERMINED")


class EvalReport(_Base):
    paper_id: str
    title: str = ""
    targets_summary: dict = Field(default_factory=dict)
    review_efficiency: dict = Field(default_factory=dict, description="CaseLedger.efficiency, carried")
    reviewer_report_path: str = Field(default="")
    ledger_path: str = Field(default="")
    claim_status: str = Field(default="", description=" | ".join(CLAIM_STATUSES))
    reproduction_status: str = Field(default="", description=" | ".join(REPRODUCTION_STATUSES))
    execution_provenance: str = Field(
        default="", description=" | ".join(PROVENANCE_LABELS) + " — fails closed to SYNTHESIZED_DIAGNOSTIC")
    disposition: str = Field(default="NOT_REVIEWED", description=" | ".join(PAPER_DISPOSITIONS))
    disposition_basis: str = Field(default="NONE", description=" | ".join(DISPOSITION_BASIS))
    disposition_reason: str = Field(default="")
    findings: list[Finding] = Field(default_factory=list)
    scientific_findings: list[ScientificFinding] = Field(
        default_factory=list, description="the PRIMARY output; `findings` stays the raw record")
    unasked_question: str = ""
    n_pages: int = 0
    n_sections: int = 0
    n_tables: int = 0
    n_numbers: int = 0
    lenses_run: list[str] = Field(default_factory=list)
    dropped_findings: int = Field(default=0)
    claim_status_if_cell_backed_only: str = Field(
        default="", description="same claim-status table, findings with a verified cell only")
    probe: ProbeResult | None = Field(default=None)
    experimental_chain: "ExperimentalChain | None" = Field(default=None)
    claim_status_if_lens_severity_only: str = Field(
        default="", description="same claim-status table, counted_severity erased")
    grade_coverage: dict = Field(default_factory=dict)
    substantive_verdict: "SubstantiveVerdict | None" = Field(default=None)
    verdict_agreement: str = Field(default="", description=" | ".join(VERDICT_AGREEMENTS))
    verdict_contested: bool = Field(default=False)
    claim_status_reason: str = Field(default="")
    artifact_state: str = Field(default="", description="HARNESS-WRITTEN, see taxonomy.ARTIFACT_STATES")
    review_path: str = Field(default="", description="HARNESS-WRITTEN: PAPER_ONLY | PAPER_AND_ARTIFACT")
    coverage: "CoverageReport | None" = Field(default=None)
    document_observations: list["DocumentObservation"] = Field(default_factory=list)
    guarantees: "ReviewGuarantees | None" = Field(default=None)
    outcome: "ReviewOutcome | None" = Field(default=None)
    n_figures: int = Field(default=0)
    n_equations: int = Field(default=0)
    self_audit: "ReviewSelfAudit | None" = Field(default=None)


class SubstantiveVerdict(_Base):
    """One model's whole-paper opinion. Every field is a model's claim; none is harness-
    written, none is evidence — printed, labelled as opinion, consumed by no threshold."""

    verdict: str = Field(default="", description=" | ".join(SUBSTANTIVE_VERDICTS))
    reason: str = ""
    strongest_contribution: str = ""
    weakest_link: str = ""
    weaknesses_are: str = Field(default="", description=" | ".join(WEAKNESS_SCOPES))
    real_contribution: str = Field(default="")
    strongest_support: str = Field(default="")
    strongest_threat: str = Field(default="")
    claims_well_supported: list[str] = Field(default_factory=list)
    claims_needing_qualification: list[str] = Field(default_factory=list)
    core_contribution_stands: str = Field(default="", description=" | ".join(CONTRIBUTION_VERDICTS))


SELF_AUDIT_STATES = ("pass", "fail", "not_applicable")


class SelfAuditItem(_Base):
    key: str = ""
    question: str = ""
    state: str = Field(default="not_applicable", description=" | ".join(SELF_AUDIT_STATES))
    detail: str = ""
    offenders: list[str] = Field(default_factory=list)
    n_offenders: int = 0
    n_in_scope: int = 0


class ReviewSelfAudit(_Base):
    """Did this review do the work it claims? `complete=False` never changes the verdict —
    only whether the review may present itself as finished."""

    items: list[SelfAuditItem] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    complete: bool = False
    summary: str = ""


# === 10. CORPUS ACCOUNTING — every requested paper, in exactly one terminal state ======

CORPUS_STATES = ("requested", "started", "completed", "failed", "inconclusive")


class CorpusEntry(_Base):
    """One requested paper's terminal state, keyed on the REQUEST — two files can slugify
    to the same paper_id, and a paper_id-keyed summary would collapse them."""

    source: str = Field(default="")
    paper_id: str = ""
    state: str = Field(default="requested", description=" | ".join(CORPUS_STATES))
    reason: str = ""
    phase: str = ""
    reproduction_class: str = ""
    failure_kind: str = Field(default="", description=" | ".join(FAILURE_KINDS))
    resume_after: str = ""
    report_path: str = ""
    disposition: str = Field(default="NOT_REVIEWED", description=" | ".join(PAPER_DISPOSITIONS))
    disposition_basis: str = Field(default="NONE", description=" | ".join(DISPOSITION_BASIS))


class CorpusReport(_Base):
    """The batch, request by request — `sum(counts.values()) == requested` is asserted,
    never assumed (`harness/pipeline.py`)."""

    requested: int = 0
    entries: list[CorpusEntry] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    by_state: dict[str, list[str]] = Field(default_factory=dict)
    complete: bool = Field(default=False)
    summary: str = ""


# === 11. REVIEW-SURFACE COVERAGE — structural, never issue recall ======================

SURFACE_KINDS = ("table_cell", "figure", "equation", "section_span", "reported_quantity")


class ReviewSurface(_Base):
    """The paper's own addressable surface, enumerated from `PaperDoc` and NOTHING ELSE —
    a signature-level guarantee that a numerator cannot redefine the denominator."""

    paper_id: str = ""
    content_sha: str = Field(default="")
    addresses: list[str] = Field(default_factory=list)
    by_kind: dict[str, int] = Field(default_factory=dict)
    table_cells_nonempty: int = 0
    table_cells_total: int = Field(default=0, description="including empty padding cells")
    sections: int = 0
    prose_chars_total: int = Field(default=0)
    prose_chars_presented: int = Field(default=0, description="how much a reading pass actually carried")
    reading_parts: int = Field(default=1)
    surface_empty: bool = Field(default=True, description="a rate over this is None, never 1.0")


class ReadingRecord(_Base):
    """HOW THE PAPER WAS READ. `reader_visible_fraction` is the ceiling on any recall
    claim this system makes."""

    extracted_prose_chars: int = 0
    pages_with_text: int = 0
    pages_total: int = 0
    extracted_text_fraction: float | None = Field(default=None)
    reader_visible_chars: int = 0
    reader_visible_fraction: float | None = Field(default=None)
    anchor_chars: int = 0
    anchor_repeat_chars: int = 0
    anchor_repeat_fraction: float | None = None
    number_of_parts: int = 1
    lenses_total: int = 0
    lenses_completed: int = 0
    lens_syntheses_required: int = Field(default=0)
    lens_syntheses_completed: int = 0


class CoverageReport(_Base):
    """Two numerators, one denominator — `addressed` (an address was minted) and
    `examined` (a route was pursued) are different claims and must never share a number."""

    paper_id: str = ""
    content_sha: str = ""
    surface_size: int = 0
    addressed: int = 0
    examined: int = 0
    off_surface: list[str] = Field(default_factory=list, description="a defect channel, not a bucket")
    addressed_rate: float | None = None
    examined_rate: float | None = None
    prose_presented_fraction: float | None = Field(default=None)
    reading: ReadingRecord | None = Field(default=None)
    by_kind_addressed: dict[str, int] = Field(default_factory=dict)
    semantic_coverage: str = Field(
        default="not_machine_detectable",
        description="ALWAYS this value — structural coverage is not issue recall")


# === 12. DOCUMENT INTEGRITY — observations, never conclusions ==========================

INTEGRITY_CHECKS = ("CROSSREF_UNRESOLVED", "OBJECT_UNCITED", "LABEL_DUPLICATED",
                    "LABEL_OUT_OF_ORDER", "BODY_UNCAPTIONED", "NUMBERING_GAP",
                    "SECTION_REF_UNRESOLVED", "TABLE_ARITHMETIC", "PROSE_CELL_MISMATCH",
                    "CAPTION_LABEL_CONFLICT", "VISION_TABLE_MISMATCH", "VISION_TABLE_UNSURE",
                    "VISION_TITLE_MISMATCH")
INTEGRITY_ABOUT = ("PAPER", "EXTRACTION")


class DocumentObservation(_Base):
    """One mechanically determined document-level fact. NOT a finding, NOT a severity —
    deliberately no severity/confidence/scientific_class/verdict field on this type."""

    check: str = Field(default="", description=" | ".join(INTEGRITY_CHECKS))
    about: str = Field(description="REQUIRED, no default: " + " | ".join(INTEGRITY_ABOUT))
    ref: str = Field(default="")
    quote: str = Field(default="")
    page: int = 0
    detail: str = Field(default="")


# === 13. GUARANTEES AND NON-GUARANTEES =================================================

GUARANTEE_KINDS = ("PROCESS", "SCIENTIFIC")


class Guarantee(_Base):
    """One thing this system does or does not promise. PROCESS = mechanically enforced
    per review; SCIENTIFIC = about the paper — this system makes NONE of these."""

    kind: str = Field(default="PROCESS", description=" | ".join(GUARANTEE_KINDS))
    statement: str = ""
    holds: bool = Field(default=False, description="for PROCESS: held FOR THIS REVIEW")
    evidence: str = Field(default="")


class ReviewGuarantees(_Base):
    paper_id: str = ""
    guarantees: list[Guarantee] = Field(default_factory=list)
    non_guarantees: list[str] = Field(default_factory=list)
    unmet: list[str] = Field(default_factory=list, description="a defect in this harness, never a paper finding")


class ExperimentalChain(_Base):
    """finding -> claim -> evidence -> identity -> repo -> commit -> execution ->
    reconciliation. Assembled at report time; INVENTS NOTHING."""

    finding_id: str = ""
    claim: str = Field(default="")
    evidence_refs: list[str] = Field(default_factory=list)
    repo_url: str = ""
    commit: str = Field(default="")
    commit_state: str = Field(default="unassessed", description=" | ".join(COMMIT_STATES))
    experiment_state: str = Field(default="unmapped", description=" | ".join(IDENTITY_STATES))
    metric_state: str = Field(default="unmapped", description=" | ".join(IDENTITY_STATES))
    configuration_state: str = Field(default="unmapped", description=" | ".join(IDENTITY_STATES))
    capability_code: str = Field(default="not_attempted", description=" | ".join(CAPABILITY_CODES))
    resource_state: str = Field(default="unassessed", description=" | ".join(RESOURCE_STATES))
    backend: str = ""
    authorization: str = Field(default="")
    provenance: str = Field(default="")
    executed: bool = Field(default=False)
    executions: int = Field(default=0)
    execution_log: str = Field(default="")
    reconciliation: str = Field(default="", description=" | ".join(RECONCILIATION_STATUSES))
    failure_class: str = ""
    broken_link: str = Field(default="", description="the FIRST link not established")


# === 14. DISCOVERY — targets, questions, plans, and where each target ended ============

DISCOVERY_KINDS = ("SCIENTIFIC_CLAIM", "EXPERIMENTAL_RESULT", "BASELINE_COMPARISON",
                   "ABLATION", "CONTROL", "DATASET_RESULT", "ERROR_ANALYSIS",
                   "IMPLEMENTATION_CLAIM", "REPRODUCTION_TARGET", "UNANSWERED_REVIEW_QUESTION")
CENTRALITY = ("CENTRAL", "SUPPORTING", "PERIPHERAL", "UNASSESSED")
ADDRESSING_BLOCKERS = ("NONE", "ADDRESS_UNRESOLVED", "QUANTITY_UNPARSED", "NO_ROUTE")

# Cheapest first — the ORDER is load-bearing (`harness.decide` walks it). EXACT_CERTIFICATE
# sits right after ARITHMETIC_RECHECK: cheap and decisive like the paper-internal routes,
# but needs no repository at all (see `harness.certificate`).
VERIFICATION_ROUTES = ("PAPER_INTERNAL_CHECK", "ARITHMETIC_RECHECK", "EXACT_CERTIFICATE",
                       "ARTIFACT_INSPECTION", "AUTHOR_CODE_EXECUTION",
                       "INDEPENDENT_RECONSTRUCTION", "NONE")

ROUTE_ATTEMPT_STATES = ("DISCHARGED_RAN", "DISCHARGED_COMPLETED", "DISCHARGED_BLOCKED",
                        "COMPLETED_INCONCLUSIVE", "GATE_CLOSED", "DEFERRED_BUDGET",
                        "DEFERRED_POLICY", "NOT_TRIED")

TARGET_DISPOSITIONS = (
    "PENDING", "REPRODUCED", "FAILED_REPRODUCTION", "PAPER_ONLY_RESOLVED",
    "CITATION_VERIFIED_ONLY", "PAPER_ARITHMETIC_CONTRADICTION",
    "ARTIFACT_FACT_ESTABLISHED", "ARTIFACT_CONCERN_VERIFIED_ENDPOINTS",
    "ARTIFACT_MISMATCH_ESTABLISHED", "ARTIFACT_INSPECTION_INCONCLUSIVE",
    "SPECIFICATION_BLOCKED", "ARTIFACT_BLOCKED", "ENVIRONMENT_BLOCKED", "RESOURCE_BLOCKED",
    "ADDRESSING_BLOCKED", "REPORTING_BLOCKED", "NO_ROUTE_AVAILABLE", "IDENTITY_BLOCKED",
    "COMPARISON_BLOCKED", "AUTHORIZATION_BLOCKED", "INCONCLUSIVE", "NO_EXPERIMENT_NEEDED",
    "BUDGET_DEFERRED", "SUPERSEDED_BY_ESTABLISHED_FAILURE", "NOT_ATTEMPTED",
    # EXACT_CERTIFICATE's own two terminal dispositions. A counterexample is a material
    # failure like FAILED_REPRODUCTION; the absence of one is NEVER support — it checks
    # only the instances actually tried, never a proof the bound holds in general.
    "COUNTEREXAMPLE_ESTABLISHED", "NO_COUNTEREXAMPLE_FOUND",
)
# Facts about a gate/artifact/host, never the paper — the two reproduction dispositions
# are the only exceptions (see `harness.taxonomy`).
BLOCKED_DISPOSITIONS = ("SPECIFICATION_BLOCKED", "ADDRESSING_BLOCKED", "REPORTING_BLOCKED",
                        "NO_ROUTE_AVAILABLE", "ARTIFACT_BLOCKED", "ENVIRONMENT_BLOCKED",
                        "RESOURCE_BLOCKED", "IDENTITY_BLOCKED", "AUTHORIZATION_BLOCKED",
                        "COMPARISON_BLOCKED")

PLAN_ACTIONS = (
    "NO_EXPERIMENT_NEEDED", "PAPER_ONLY_RESOLUTION", "AUTHOR_CODE_REPRODUCTION",
    "INDEPENDENT_RECONSTRUCTION", "MECHANISM_TEST_ONLY", "ARTIFACT_INSPECTION_ONLY",
    "INFEASIBLE_SPECIFICATION", "INFEASIBLE_ADDRESSING", "INFEASIBLE_REPORTING",
    "INFEASIBLE_ROUTE", "INFEASIBLE_ARTIFACT", "INFEASIBLE_ENVIRONMENT",
    "DEFERRED_TO_ANOTHER_TARGET", "OUTRANKED_BY_CENTRAL_TARGET",
    "SUPERSEDED_BY_ESTABLISHED_FAILURE",
    # Named identically to its own route: `decide.implemented_routes`'s `r in PLAN_ACTIONS`
    # membership test is how a route is recognised as runnable.
    "EXACT_CERTIFICATE",
)
ACTIONS_REQUIRING_EXECUTION = ("AUTHOR_CODE_REPRODUCTION", "INDEPENDENT_RECONSTRUCTION",
                               "MECHANISM_TEST_ONLY", "EXACT_CERTIFICATE")

NECESSITY_FOR_ACTION = {
    "NO_EXPERIMENT_NEEDED": "NO_EXPERIMENT_NEEDED",
    "PAPER_ONLY_RESOLUTION": "NO_EXPERIMENT_NEEDED",
    "DEFERRED_TO_ANOTHER_TARGET": "NO_EXPERIMENT_NEEDED",
    "OUTRANKED_BY_CENTRAL_TARGET": "NO_EXPERIMENT_NEEDED",
    "SUPERSEDED_BY_ESTABLISHED_FAILURE": "NO_EXPERIMENT_NEEDED",
    **{a: "EXPERIMENT_WARRANTED" for a in ACTIONS_REQUIRING_EXECUTION},
    "INFEASIBLE_SPECIFICATION": "EXPERIMENT_UNDERSPECIFIED",
    "INFEASIBLE_ADDRESSING": "EXPERIMENT_NOT_EXECUTABLE",
    "INFEASIBLE_REPORTING": "EXPERIMENT_NOT_EXECUTABLE",
    "INFEASIBLE_ROUTE": "EXPERIMENT_NOT_EXECUTABLE",
    "INFEASIBLE_ARTIFACT": "EXPERIMENT_NOT_EXECUTABLE",
    "INFEASIBLE_ENVIRONMENT": "EXPERIMENT_NOT_EXECUTABLE",
}

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
    "BUDGET_DEFERRED": "EXPERIMENT_WARRANTED",
    "NOT_ATTEMPTED": "EXPERIMENT_WARRANTED",
    "PENDING": "EXPERIMENT_WARRANTED",
    "NO_EXPERIMENT_NEEDED": "NO_EXPERIMENT_NEEDED",
    "CITATION_VERIFIED_ONLY": "EXPERIMENT_WARRANTED",
    # A counterexample resolves the question as conclusively as FAILED_REPRODUCTION does.
    "COUNTEREXAMPLE_ESTABLISHED": "EXPERIMENT_RESOLVED",
    # Like CITATION_VERIFIED_ONLY: checking finitely many instances and finding no
    # violation settles nothing — the underlying question stays warranted.
    "NO_COUNTEREXAMPLE_FOUND": "EXPERIMENT_WARRANTED",
}

QUESTION_KINDS = ("PRINTED_QUANTITY", "COMPOSITION", "ATTRIBUTION", "CONTROL_PRESENCE",
                  "PROTOCOL_CONFORMANCE", "SPECIFICATION", "PRIOR_ART", "UNCLASSIFIED",
                  # A theorem/lemma/proposition/bound stated with an inequality or
                  # asymptotic operator — routes to EXACT_CERTIFICATE, never a printed
                  # table cell. See `harness.discover.is_mathematical_bound`.
                  "MATHEMATICAL_BOUND")

# Whether an experiment was NECESSARY, and what became of that judgement. Never counted
# beside "we needed one and could not run it" — see invariant 22 in CLAUDE.md.
EXPERIMENT_NECESSITY = (
    "NO_EXPERIMENT_NEEDED", "EXPERIMENT_WARRANTED", "EXPERIMENT_NOT_EXECUTABLE",
    "EXPERIMENT_UNDERSPECIFIED", "EXPERIMENT_EXECUTED", "EXPERIMENT_RESOLVED",
    "EXPERIMENT_UNRESOLVED",
)


class ReviewQuestion(_Base):
    """A methodological concern turned into something answerable — a finding says what is
    wrong, a question says what would settle it. `route` is the harness's decision, never
    the lens's."""

    question_id: str = ""
    question: str = Field(default="")
    kind: str = Field(default="UNCLASSIFIED", description="WRITTEN BY THE HARNESS: " + " | ".join(QUESTION_KINDS))
    from_finding: str = Field(default="")
    source_finding_ids: list[str] = Field(default_factory=list)
    lens: str = ""
    claim_ref: ClaimRef | None = Field(default=None)
    why_it_matters: str = Field(default="")
    what_would_settle_it: str = Field(default="")
    possible_resolution_routes: list[str] = Field(default_factory=list, description="WRITTEN BY THE HARNESS")
    route: str = Field(default="NONE", description="WRITTEN BY THE HARNESS: " + " | ".join(VERIFICATION_ROUTES))
    materiality: str = Field(default="UNASSESSED", description=" | ".join(CENTRALITY))
    resolution_status: str = Field(default="NOT_INVESTIGATED", description="WRITTEN BY THE HARNESS: " + " | ".join(RESOLUTION_STATES))
    evidence_state: str = Field(default="NOT_INVESTIGATED", description="WRITTEN BY THE HARNESS: " + " | ".join(EVIDENCE_STATES))
    evidence_refs: list[str] = Field(default_factory=list)
    resolved_without_execution: bool = Field(default=False)
    resolution: str = Field(default="")
    conclusion: str = Field(default="")

    @property
    def evidence_needed(self) -> str:
        return self.what_would_settle_it

    @property
    def is_open(self) -> bool:
        return self.resolution_status in ("UNRESOLVED", "NOT_INVESTIGATED")


class DiscoveredObject(_Base):
    """One thing the review found worth checking. `harness_addressable` — never a lens's
    own `verifiable_by_experiment` — decides whether this object can be pursued."""

    target_id: str = ""
    kind: str = Field(default="", description=" | ".join(DISCOVERY_KINDS))
    ref: ClaimRef | None = Field(default=None)
    claim_text: str = Field(default="")
    metric: str = Field(default="")
    expected_value: float | None = Field(default=None)
    expected_raw: str = Field(default="")
    experiment: str = Field(default="")
    centrality: str = Field(default="UNASSESSED", description="WRITTEN BY THE HARNESS: " + " | ".join(CENTRALITY))
    materiality_basis: str = Field(default="NONE", description="WRITTEN BY THE HARNESS: " + " | ".join(MATERIALITY_BASES))
    evidence_requirements: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list, description="admissible routes, cheapest first")
    status: str = Field(default="PENDING", description=" | ".join(TARGET_DISPOSITIONS))
    proposed_by_lens: bool = Field(default=False, description="metadata only, never authoritative")
    harness_addressable: bool = Field(default=False, description="WRITTEN BY THE HARNESS")
    addressing_blocker: str = Field(default="NONE", description="WRITTEN BY THE HARNESS: " + " | ".join(ADDRESSING_BLOCKERS))
    note: str = Field(default="")
    counter_explanations: list[str] = Field(default_factory=list)
    question_id: str = Field(default="", description="REQUIRED whenever the plan requires execution")
    question_kind: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(QUESTION_KINDS))
    priority: float = Field(default=0.0, description="WRITTEN BY THE HARNESS: see harness.priority")
    priority_reason: str = Field(default="")


class PlanDecision(_Base):
    """What the planner decided about one target, and every gate it consulted."""

    target_id: str = ""
    action: str = Field(default="NO_EXPERIMENT_NEEDED", description=" | ".join(PLAN_ACTIONS))
    route: str = Field(default="NONE", description=" | ".join(VERIFICATION_ROUTES))
    reason: str = ""
    gates: dict[str, bool] = Field(default_factory=dict)
    blocking_gate: str = Field(default="")
    requires_execution: bool = False
    necessity: str = Field(default="NO_EXPERIMENT_NEEDED", description=" | ".join(EXPERIMENT_NECESSITY))
    why_material: str = Field(default="")
    paper_only_insufficient_because: str = Field(default="")
    inspection_insufficient_because: str = Field(default="")
    competing_explanations: list[str] = Field(default_factory=list)
    expected_observation: str = Field(default="")
    attempt: int = Field(default=1, description="which re-plan attempt this is, for this target")
    superseded_by: str = Field(default="", description="the route that replaced this attempt")


class TargetOutcome(_Base):
    """Where one target ended, with the evidence behind it. Independent per target."""

    target_id: str = ""
    disposition: str = Field(default="NOT_ATTEMPTED", description=" | ".join(TARGET_DISPOSITIONS))
    action: str = Field(default="")
    route: str = Field(default="NONE", description=" | ".join(VERIFICATION_ROUTES))
    provenance: _TargetOutcomeProvenanceField = ""
    reconciliation: Reconciliation | None = None
    failure_class: _TargetOutcomeFailureClassField = "none"
    reason: str = ""
    execution_ref: str = Field(default="", description="e.g. 'runs/<pid>/execution.jsonl'")
    authorized: bool | None = Field(default=None)
    attempts: int = 0
    launched: int = Field(default=0, description="processes STARTED, read off the execution record")
    identity_state: str = Field(default="", description=" | ".join(IDENTITY_STATES) + ", or ''")

    @property
    def establishes_failure(self) -> bool:
        """The three routes that may contribute a material failure: FAILED_REPRODUCTION
        and COUNTEREXAMPLE_ESTABLISHED (both gated by the provenance ceiling) and
        PAPER_ARITHMETIC_CONTRADICTION (unconditional; its own ceiling already ran inside
        `locate.parse_quantity`)."""
        from .provenance import admits
        return ((self.disposition in ("FAILED_REPRODUCTION", "COUNTEREXAMPLE_ESTABLISHED")
                and admits(self.provenance))
                or self.disposition == "PAPER_ARITHMETIC_CONTRADICTION")

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
        from . import taxonomy
        return taxonomy.concerns_the_paper(self.evidence_state)

    def necessity(self, warranted: bool) -> str:
        if not warranted:
            return "NO_EXPERIMENT_NEEDED"
        return NECESSITY_FOR_DISPOSITION.get(self.disposition, "EXPERIMENT_UNRESOLVED")


class RouteAttempt(_Base):
    """Durable accounting for one applicable route for one material review question."""

    question_id: str = ""
    route: str = Field(default="NONE", description=" | ".join(VERIFICATION_ROUTES))
    target_ids: list[str] = Field(default_factory=list)
    attempted: bool = False
    completed: bool = False
    exhausted: bool = False
    state: str = Field(default="NOT_TRIED", description=" | ".join(ROUTE_ATTEMPT_STATES))
    blocker: str = Field(default="")
    source_refs: list[str] = Field(default_factory=list)
    reason: str = ""


class TargetSet(_Base):
    """Every target considered for one paper, in priority order, with its disposition."""

    paper_id: str = ""
    objects: list[DiscoveredObject] = Field(default_factory=list)
    questions: list[ReviewQuestion] = Field(default_factory=list)
    plans: list[PlanDecision] = Field(default_factory=list)
    outcomes: list[TargetOutcome] = Field(default_factory=list)
    route_attempts: list[RouteAttempt] = Field(default_factory=list)
    route_exhaustion_question_ids: list[str] = Field(default_factory=list)
    extraction_coverage: dict = Field(default_factory=dict)

    def by_id(self, target_id: str) -> DiscoveredObject | None:
        return next((o for o in self.objects if o.target_id == target_id), None)

    def outcome_for(self, target_id: str) -> TargetOutcome | None:
        return next((o for o in self.outcomes if o.target_id == target_id), None)


# === 15. LEDGER — every traceable line from a paper's words to a scientific implication ===

class LedgerEntry(_Base):
    entry_id: str = ""
    claim_text: str = ""
    materiality_basis: str = Field(default="NONE", description=" | ".join(MATERIALITY_BASES))
    source_ref: str = Field(default="")
    source_quote: str = ""
    question: str = ""
    target_id: str = ""
    selection_reason: str = Field(default="")
    route: str = ""
    action: str = ""
    provenance: str = ""
    commit: str = ""
    command: list[str] = Field(default_factory=list)
    observed: str = Field(default="")
    compared_with: str = Field(default="")
    admissibility: str = Field(default="")
    disposition: str = ""
    implication: str = Field(default="")
    evidence_state: str = Field(default="NOT_INVESTIGATED", description=" | ".join(EVIDENCE_STATES))
    resolution_state: str = Field(default="NOT_INVESTIGATED", description=" | ".join(RESOLUTION_STATES))
    necessity: str = Field(default="", description=" | ".join(EXPERIMENT_NECESSITY))
    concerns_the_paper: bool = Field(default=False)
    launched: int = Field(default=0)
    why_material: str = Field(default="")


class CaseLedger(_Base):
    """The machine-readable audit trace for one paper — NOT the reviewer's report."""

    paper_id: str = ""
    entries: list[LedgerEntry] = Field(default_factory=list)
    route_attempts: list[RouteAttempt] = Field(default_factory=list)
    efficiency: dict = Field(default_factory=dict)


# === 16. ASSESSMENT + CONTROLLER — pre-execution gate, and the phase state machine =====

class PaperAssessment(_Base):
    """Whether the paper's own evidence already settles it, asked BEFORE anything runs.
    `material_failure_established` stops the expensive investigation branch and stops
    nothing else — discovery, report, ledger and the four pure layers still run."""

    material_failure_established: bool = False
    basis: str = Field(default="NONE", description=" | ".join(DISPOSITION_BASIS))
    reason: str = Field(default="")
    counted_fatal_ids: list[str] = Field(default_factory=list)


PHASES = ("ingest", "audit", "collect", "grade", "assess", "discover", "probe", "report", "done")
CASE_STATUSES = ("pending", "running", "waiting", "complete", "error")
PHASE_OUTCOMES = ("ok", "waiting", "retry", "abstain", "error")
RETRY_POLICIES = ("now", "later", "never")


class PhaseEvent(_Base):
    """One attempt at one phase. The controller's audit trail."""

    phase: str = Field(default="", description=" | ".join(PHASES))
    outcome: str = Field(default="", description=" | ".join(PHASE_OUTCOMES))
    reason: str = ""
    detail: dict = Field(default_factory=dict)
    attempt: int = 1
    ts: str = ""


_CasePhaseField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(PHASES, "CaseState.phase"))]
_CaseStatusField: TypeAlias = Annotated[
    str, AfterValidator(_vocab_validator(CASE_STATUSES, "CaseState.status"))]


class CaseState(_Base):
    """One paper's position in the workflow, persisted at `projects/<pid>/controller.json`
    so a batch survives the process ending and resumes exactly where it stopped."""

    paper_id: str = ""
    source: str = Field(default="")
    content_sha: str = ""
    phase: _CasePhaseField = "ingest"
    status: _CaseStatusField = "pending"
    attempts: dict[str, int] = Field(default_factory=dict)
    history: list[PhaseEvent] = Field(default_factory=list)
    awaiting: list[str] = Field(default_factory=list)
    blocked_reason: str = Field(default="")
    reproduction_class: str = Field(default="")
    assessment: PaperAssessment | None = Field(default=None)
    disposition: str = Field(default="NOT_REVIEWED", description="carried from the report: " + " | ".join(PAPER_DISPOSITIONS))
    disposition_basis: str = Field(default="NONE", description=" | ".join(DISPOSITION_BASIS))
    report_path: str = ""
    resume_after: str = Field(default="", description="advisory hint; the controller does not sleep on it")
    failure_kind: str = Field(default="", description=" | ".join(FAILURE_KINDS))
    retry_policy: str = Field(default="", description=" | ".join(RETRY_POLICIES))

    @property
    def terminal(self) -> bool:
        return self.status in ("complete", "error")


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    """A construction smoke test, not the invariant suite — see
    `tests/test_schema_invariants.py` for the exhaustive vocabulary sweeps this module's
    ratchet requires."""
    # every vocabulary tuple is non-empty and every default value it documents belongs
    import inspect
    import sys

    mod = sys.modules[__name__]
    vocab_names = [n for n, v in vars(mod).items()
                   if n.isupper() and isinstance(v, tuple) and n != "GIB"]
    assert len(vocab_names) >= 60, f"expected 60+ vocabularies, found {len(vocab_names)}"
    for name in vocab_names:
        vocab = getattr(mod, name)
        assert all(isinstance(v, str) for v in vocab), name

    # a handful of round-trips through the models that carry the trust-boundary vocab
    assert ExecAuthorization(decision="authorized", allowed=True).decision in EXEC_DECISIONS
    assert Reconciliation(status="INCONCLUSIVE").status in RECONCILIATION_STATUSES
    tgt = TargetOutcome(disposition="PAPER_ARITHMETIC_CONTRADICTION", provenance="paper")
    assert tgt.establishes_failure
    tgt2 = TargetOutcome(disposition="FAILED_REPRODUCTION", provenance="synthesized")
    assert not tgt2.establishes_failure, "the provenance ceiling must still refuse here"

    finding = Finding(severity="NOTE")
    assert finding.severity in SEVERITIES, "NOTE must round-trip (v4 ratchet regression)"

    doc = PaperDoc(paper_id="p")
    assert isinstance(doc.crossrefs, list)

    # closed-vocabulary cross-references this module itself declares must stay consistent
    assert set(BLOCKED_DISPOSITIONS) <= set(TARGET_DISPOSITIONS)
    assert set(ACTIONS_REQUIRING_EXECUTION) <= set(PLAN_ACTIONS)
    # ARTIFACT_INSPECTION_ONLY is the one action absent from this table (falls back to
    # NO_EXPERIMENT_NEEDED); any OTHER gap must fail this check.
    assert set(PLAN_ACTIONS) - set(NECESSITY_FOR_ACTION) == {"ARTIFACT_INSPECTION_ONLY"}
    assert set(REIMPL_BINDING_KINDS) <= set(REIMPL_INGREDIENT_KINDS)

    for name, p in inspect.signature(TargetOutcome.necessity).parameters.items():
        if name == "self":
            continue
        assert str(p.annotation) == "bool", name

    print("harness.schema self-check ok")


if __name__ == "__main__":
    _self_check()
