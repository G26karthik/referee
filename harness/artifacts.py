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


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------------- #
# ① INGESTION (S1)
# --------------------------------------------------------------------------- #
class QuantFinding(_Base):
    """A single reported number — the quantitative-prior unit, and the atom the
    overclaim and contradiction lenses argue over.

    `source_quote` is mandatory in practice (enforced at extraction, see
    `stages/ingest.py:_keep_number`): a number without a verbatim quote is DROPPED,
    never guessed. `page` and `table_ref` carry it back to where it was printed.
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
    caption: str = ""
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
    evidence class it earns (`caption_verified`) is capped at NOTE in
    `harness.grading.EVIDENCE_CEILING` for exactly that reason: a caption NAMES a
    figure, it does not report the values in it."""

    figure_idx: int
    page: int = 0
    label: str = ""
    caption: str = ""

    def ref(self) -> str:
        return f"F{self.figure_idx}"


class Equation(_Base):
    """One extracted display equation. Text-only and lossy by construction — symbols,
    sub/superscripts and inline math routinely survive PDF extraction mangled or
    missing — so its evidence class (`equation_verified`) is capped at MINOR, not
    trusted the way a table cell is."""

    equation_idx: int
    page: int = 0
    number: str = ""
    text: str = ""

    def ref(self) -> str:
        return f"E{self.equation_idx}"


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
    repo_url: str = Field(
        default="", description="the official code repository the paper advertises ('' = none found)"
    )
    repo_urls: list[str] = Field(
        default_factory=list,
        description="every candidate repo URL found in the text, best first; repo_url is repo_urls[0]",
    )


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

# The three-way, non-collapsible bucket a lens must sort its own candidate into. Only
# CONFIRMED_FINDING is eligible to carry FATAL/MAJOR once graded; a PLAUSIBLE_CONCERN or
# OPEN_QUESTION is printed but counts toward no threshold. See `FINDING_CLASSES` below
# for the harness-derived analogue, which additionally distinguishes REFUTED/UNGRADED.
CANDIDATE_CLASSES = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION")

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
    verification_state: str = Field(default="", description="WRITTEN BY THE HARNESS: "
                                                             + " | ".join(VERIFICATION_STATES))
    calc_class: str = Field(default="", description="WRITTEN BY THE HARNESS: " + " | ".join(CALC_CLASSES))
    evidence_origin: str = Field(default="", description="WRITTEN BY THE HARNESS from the shape of "
                                                          "evidence_ref: " + " | ".join(EVIDENCE_ORIGINS))
    origin_consistency: str = Field(
        default="", description="WRITTEN BY THE HARNESS: 'consistent' or 'corrected' against "
                                "whatever origin the lens itself claimed")

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

    def as_reasoning(self) -> str:
        return (self.reasoning or self.statement or "").strip()

    def as_conclusion(self) -> str:
        return (self.conclusion or self.statement or "").strip()

    def as_claim(self) -> str:
        return (self.claim or self.target or "").strip()


class LensReport(_Base):
    lens: str
    findings: list[Finding] = Field(default_factory=list)
    unasked_question: str = Field(
        default="", description="the obvious baseline/comparison this lens finds conspicuously absent"
    )
    notes: str = Field(default="", description="what the auditor actually checked vs skimmed")


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
    claimed_cell_value: str = Field(
        default="", description="that cell's contents verbatim, carried so reconciliation can parse it"
    )
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
                    "'driver' = a human wrote spec.json; 'repo_exec' = the paper's own checkout. "
                    "This decides how far a reconciliation is allowed to go — see local_exec.reconcile.",
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


class RepoAcquisition(_Base):
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
    declared_args: dict[str, str] = Field(default_factory=dict)
    seed_flag: str = Field(default="", description="the seed flag the repo itself uses, if any")
    seed_values: list[str] = Field(default_factory=list, description="seeds the repo passes")
    emits: list[str] = Field(default_factory=list, description="output keys/files this command writes")
    label: str = Field(default="", description="what the command actually does")


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
    cwd: str = Field(default="", description="working directory the process actually ran in")
    interpreter: str = Field(default="", description="the python that ran it, when one did")
    environment: str = Field(
        default="",
        description="the backend's own identification of WHERE this ran — platform, python and "
                    "accelerator, plus whatever session identity a remote backend has. Locally "
                    "this is implicit in the host; remotely it is the only record of the hardware "
                    "a verdict came from, and a reproduction nobody can locate is not evidence.",
    )
    commit: str = Field(default="", description="the audited commit this execution is about")
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
    band — never by judgement, for the same reason the RED/YELLOW/GREEN verdict is a
    threshold table.
    """

    table_ref: str = Field(default="", description="the addressed cell 'T<t>:r<r>:c<c>'")
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
        description="the ProbeSpec.provenance that produced this. Only 'driver' and 'repo_exec' "
                    "may reach a verdict: a synthesized probe is our reimplementation on a toy "
                    "problem, and it is not entitled to convict a paper's printed number.",
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
class EvalReport(_Base):
    paper_id: str
    title: str = ""
    verdict: str = Field(default="", description="RED | YELLOW | GREEN")
    verdict_reason: str = Field(default="", description="the deterministic rule that produced the verdict")
    findings: list[Finding] = Field(default_factory=list)
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
                    "while the deterministic table says GREEN/YELLOW. The one consequence of the "
                    "dissent: it demands human attention (a contested banner, `run.py` exit 3), "
                    "never a change of color.",
    )


SUBSTANTIVE_VERDICTS = ("STRONG", "SOUND_WITH_MINOR_CONCERNS", "SUBSTANTIAL_CONCERNS",
                        "CENTRAL_CLAIM_NOT_ESTABLISHED", "INCONCLUSIVE")


class SubstantiveVerdict(_Base):
    """One model's whole-paper opinion — see `EvalReport.substantive_verdict`. Every
    field here is a model's claim; none of it is harness-written, because none of it is
    evidence — it is printed as an opinion, labelled as one, consumed by no threshold."""

    verdict: str = Field(default="", description=" | ".join(SUBSTANTIVE_VERDICTS))
    reason: str = ""
    strongest_contribution: str = ""
    weakest_link: str = ""
    weaknesses_are: str = Field(default="", description="LOCAL | SYSTEMIC | MIXED")


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
PHASES = ("ingest", "audit", "collect", "grade", "probe", "report", "done")

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
    report_path: str = ""
    resume_after: str = Field(
        default="",
        description="best-effort hint (free text, e.g. 'resets 3:20pm (Asia/Kolkata)') for when "
                    "re-running review is worth trying again after an account-level rate limit — "
                    "advisory only, the controller does not sleep or poll on it.",
    )

    @property
    def terminal(self) -> bool:
        return self.status in ("complete", "error")
