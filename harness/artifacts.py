"""Typed artifacts that cross stage boundaries (the ONLY thing that crosses).

Pydantic v2, permissive (extra allowed) so a stage can add a field without breaking
the pipeline and a partial LLM response degrades instead of raising. Each stage
writes one of these as JSON into the research log; downstream stages receive an
allow-listed subset — never the transcript.

Reviewer pipeline (S1 → S4):
    PaperDoc      ① ingestion: sectioned text + addressable tables + claims
    LensReport    ② audit: one blinded auditor's findings
    EvalReport    ④ synthesis: the ranked, rendered verdict

The S3 reproduction-trigger artifacts (StudySpec, GroundingReport, RunReport,
ResultsAnalysis, CheckpointReview) are retained unchanged.
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


class Claim(_Base):
    """One assertion the paper makes about itself, with provenance."""

    claim_id: str
    kind: str = Field(default="", description="premise | contribution | result | comparison")
    text: str = Field(default="", description="the claim, as the auditors will judge it")
    source_quote: str = Field(default="", description="verbatim sentence(s) the claim was read from")
    section: str = Field(default="", description="section title the quote sits in")
    page: int = 0


class PaperDoc(_Base):
    """The ONLY representation of the paper that crosses into the audit stage.

    Nothing downstream re-opens the PDF, which is exactly what lets every auditor
    run with `allowed_tools=[]`.
    """

    paper_id: str
    title: str = ""
    source_path: str = ""
    n_pages: int = 0
    sections: list[Section] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
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
SEVERITIES = ("FATAL", "MAJOR", "MINOR")


class Finding(_Base):
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
    commit: str = Field(default="", description="resolved HEAD, so a rerun is pinned")
    reason: str = ""
    dependency_files: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list, description="torch / jax / sklearn / …")
    entrypoint: str = Field(default="", description="the command the probe will run, if one was found")
    env_path: str = Field(default="", description="isolated interpreter built for this repo, if any")
    env_status: str = Field(default="not_attempted", description="ready | blocked | failed | not_attempted")


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
    files_scanned: int = 0
    lines_scanned: int = 0
    findings: list[CodeAuditFinding] = Field(default_factory=list)
    unparseable: list[str] = Field(default_factory=list, description="files whose AST would not build")
    skipped: str = Field(default="", description="why the audit did not run, if it did not")


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
    n_claims: int = 0
    n_numbers: int = 0
    lenses_run: list[str] = Field(default_factory=list)
    dropped_findings: int = Field(
        default=0, description="findings discarded because their evidence could not be substantiated"
    )
    probe: ProbeResult | None = Field(default=None, description="local reproduction result, if S3 ran")


# --------------------------------------------------------------------------- #
# S3 REPRODUCTION TRIGGER — retained from the research pipeline, unchanged
# --------------------------------------------------------------------------- #
class PaperSummary(_Base):
    """Prior-art record produced by the literature scout (S3 prior-art lookup)."""

    paper_id: str
    title: str
    source_url: str = ""
    problem_and_context: str = Field(default="", description="what problem, why it matters now")
    existing_approaches_and_limitations: str = ""
    proposed_solution_and_methodology: str = ""
    results_and_impact: str = Field(default="", description="did it work, by what metric")
    future_work_and_limitations: str = ""
    research_gaps: list[str] = Field(default_factory=list)
    quantitative_findings: list[QuantFinding] = Field(default_factory=list)


class StudySpec(_Base):
    idea_id: str
    goal: str = ""
    scientific_hparams: list[str] = []
    nuisance_hparams: list[str] = []
    fixed_hparams: list[str] = []
    baseline: str = ""
    claim_generality: str = Field(default="", description="'generic' or 'domain-specific'")
    evaluation_contract: str = ""
    sesoi: str = Field(default="", description="smallest effect size worth caring about")
    confirm_threshold: str = Field(default="", description="the number that confirms the hypothesis")
    refute_threshold: str = Field(default="", description="the number that refutes it")
    min_seeds: int = Field(default=0, description="seeds needed for the effect to clear noise")
    baseline_target: str = Field(default="", description="the published baseline number the probe must reproduce")


class GroundingReport(_Base):
    idea_id: str
    baseline_reconciled: bool | None = None
    baseline_target: str = ""
    baseline_observed: str = ""
    scale_downgraded: bool = False
    measured_noise: str = Field(default="", description="seed-to-seed std measured by the probe")
    required_delta: str = Field(default="", description="the Δ under test")
    power_verdict: str = Field(default="", description="'detectable' | 'underpowered' | 'undetectable'")
    recommended_min_seeds: int = 0
    sesoi: str = ""
    confirm_threshold: str = ""
    refute_threshold: str = ""
    go_no_go: str = Field(default="", description="'go' | 'underpowered' | 'undetectable' | 'baseline_mismatch'")
    reason: str = ""
    l4_gpu_cost_usd: float = 0.0


class RunReport(_Base):
    experiment_id: str
    scale: str = Field(default="probe", description="probe | full")
    status: str = ""
    metrics: dict = {}
    subject_planned: str = ""
    subject_executed: str = ""
    artifacts_dir: str = ""
    commit_sha: str = ""
    branch: str = ""
    gpu: str = ""
    pod_seconds: float = 0.0
    gpu_cost_usd: float = 0.0


class ResultsAnalysis(_Base):
    experiment_id: str
    verdict: str = Field(default="", description="confirmed|refuted|negative|inconclusive|broken")
    matches_prediction: bool | None = None
    adversarial_section: str = Field(default="", description="alt explanations + disconfirming evidence (mandatory)")
    replicate_before_extend_ok: bool | None = None
    confidence: str = ""


class Review(_Base):
    persona: str = ""
    quality: int = 0
    clarity: int = 0
    significance: int = 0
    originality: int = 0
    confidence: int = 0
    verified: str = ""
    strengths: list[str] = []
    weaknesses: list[str] = []
    questions: list[str] = []
    recommendation: str = ""


class CheckpointReview(_Base):
    checkpoint: str = Field(default="", description="pre_experiment | post_results | post_manuscript")
    decision: str = Field(default="", description="approve | request_changes")
    comments: str = ""
