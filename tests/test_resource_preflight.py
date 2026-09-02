"""E1 — the published experiment has to FIT, and that is decided before anything starts.

The failure this closes is quiet. Provenance, identity and capability all pass for a
repository whose experiment is three times larger than the card present: the code is the
authors', the command is the right one, the environment builds, the process launches. It
then loads a 7B checkpoint onto 8 GB and dies. By that point it has printed banners and
run for minutes, which is exactly the evidence `reached_experiment` reads as "the
experiment began" — so the crash reconciles as FAILED_REPRODUCTION and the report says the
authors' own code does not reproduce the number it prints. It reproduces fine; the machine
was too small, and nothing in the pipeline knew that.

The other half is the tempting fix. Halve the batch, drop to int8, shorten the sequence,
run two seeds instead of five, use the 1.3B checkpoint — and it completes. What completed
is a different experiment, and its number reconciled against the paper's cell is worse
than the crash, because a crash is visibly wrong and a confident figure is not. So there
is no adaptation path in `harness/resources.py`, and `test_there_is_no_path_that_shrinks_
an_experiment_to_fit` exists to keep it that way.
"""
from __future__ import annotations

import pytest

from harness.artifacts import (GIB, CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExperimentIdentity, MetricIdentity, PaperDoc, ProbeSpec,
                               RepoAcquisition, ResourceCapability, ResourceEvidence,
                               ResourceRequirement, Section, Table)
from harness.backends import authorize, local_backend
from harness.config import Config
from harness.local_exec import reconcile
from harness.resources import (assess_resources, declared_hardware, declared_memory_cost,
                               host_disk_bytes, host_vram_bytes, model_weight_floor,
                               require_resources, verify_requirement)
from harness.stages.probe import plan_execution
from harness.stages.report import overall_verdict


# --------------------------------------------------------------------------- #
# Fixtures — APT's own sentences, verbatim from the ingested paper
# --------------------------------------------------------------------------- #
APT_TEXT = (
    "Both training and evaluation are conducted on a single A100 GPU. "
    "Also, APT costs less than 24GB of memory when pruning 30% parameters in LLaMA2-7B "
    "models before tuning, which can be easily adapted to the consumer-level GPUs. "
    "In contrast, LLM-Pruner costs about 80GB memory when pruning the LLaMA 7B model."
)


def _apt() -> PaperDoc:
    return PaperDoc(
        paper_id="apt-icml", title="APT",
        sections=[Section(section_idx=4, title="Experimental Setup", page_start=7, text=APT_TEXT)],
        tables=[Table(table_idx=2, page=7,
                      caption="Table 3: LLaMA 2 7B 30% sparsity pruning results with "
                              "GPT4-generated Alpaca dataset",
                      rows=[["Method", "TrainMem"], ["LoRA", "100.0%"], ["APT", "86.4%"],
                            ["LLMPruner", "253.6%"]])])


class ThisMachine:
    """The real host, as measured in the WSL feasibility investigation."""
    vram_bytes, ram_bytes, disk_bytes = 8 * GIB, 15 * GIB, 224 * GIB
    cpu_count, gpu_count, gpu_name = 16, 1, "NVIDIA GeForce RTX 4060 Laptop GPU"


class BigMachine(ThisMachine):
    vram_bytes, ram_bytes = 80 * GIB, 512 * GIB


class BlindMachine(ThisMachine):
    vram_bytes = None                      # a backend that cannot report its accelerator


def _fits() -> ResourceCapability:
    return ResourceCapability(state="satisfied", reason="fixture")


def _verified() -> CommitVerification:
    return CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)


def _est(cls):
    return cls(state="established", established=True, reason="fixture")


def _spec(resources: ResourceCapability | None) -> ProbeSpec:
    return ProbeSpec(paper_id="apt", command=["python", "eval.py"], provenance="repo_exec",
                     table_ref="T2:r3:c11", claimed_cell_value="253.6%",
                     experiment=_est(ExperimentIdentity), metric_identity=_est(MetricIdentity),
                     configuration=_est(ConfigurationIdentity),
                     capability=ExecCapability(established=True, reason_code="established"),
                     resources=resources)


def _cfg(**over) -> Config:
    cfg = Config.load()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------- #
# The APT case: 24 GiB declared vs 8 GiB present
# --------------------------------------------------------------------------- #
def test_apt_is_rejected_before_execution_on_vram():
    """The headline case. Nothing is launched; the refusal is arithmetic on declared numbers."""
    doc = _apt()
    req = require_resources(doc, "T2:r3:c11", caption=doc.tables[0].caption)
    cap = assess_resources(req, ThisMachine(), backend="local")

    assert cap.state == "insufficient" and not cap.established
    assert any("VRAM" in s for s in cap.shortfalls), cap.shortfalls
    assert "24.0 GiB" in cap.reason and "8.0 GiB" in cap.reason, cap.reason


def test_the_same_experiment_fits_a_machine_that_is_big_enough():
    """The check must be a comparison, not a refusal with extra steps."""
    doc = _apt()
    req = require_resources(doc, "T2:r3:c11", caption=doc.tables[0].caption)
    assert assess_resources(req, BigMachine(), backend="stub").state == "satisfied"


def test_the_requirement_is_the_methods_stated_cost_not_the_baselines():
    """APT states 24GB for itself and 80GB for LLM-Pruner in adjacent sentences. Charging
    the method with the figure it was arguing against would block on the paper's own
    rhetorical contrast."""
    value, ev = declared_memory_cost(_apt())
    assert value is not None and abs(value - 24 * GIB) < GIB, value
    assert ev is not None and "24GB" in ev.quote and "LLM-Pruner" not in ev.quote


def test_a_soft_hyphenated_cost_is_still_read():
    """Verbatim from the ingested APT PDF, line-broken as the extractor produced it.

    Regression on a defect the first APT demonstration exposed. Anchored on `memor\\w+`,
    the pattern missed "24GB of mem- ory" entirely — and with the method's own sentence
    invisible, the only remaining match was the CONTRASTED BASELINE, so the requirement
    came out as 80 GiB attributed to APT with LLM-Pruner's sentence as its evidence. The
    verdict was still INSUFFICIENT, which is what makes it worth a test: the right answer
    for the wrong reason, carrying a misattributed quote.
    """
    real = PaperDoc(paper_id="apt", title="APT", sections=[Section(
        section_idx=4, title="Setup", page_start=7,
        text=("Also, APT costs less than 24GB of mem- ory when pruning 30% parameters in "
              "LLaMA2-7B models before tuning, which can be easily adapted to the consumer- "
              "level GPUs. In contrast, LLM-Pruner costs about 80GB memory when pruning the "
              "LLaMA 7B model6."))])
    value, ev = declared_memory_cost(real)
    assert value is not None and abs(value - 24 * GIB) < GIB, value
    assert ev is not None and "APT costs" in ev.quote and "LLM-Pruner" not in ev.quote
    assert verify_requirement(require_resources(real), real) == []


def test_normalization_is_symmetric_between_quote_and_corpus():
    """The stored quote is dehyphenated, so verification must dehyphenate the corpus too.
    A one-sided normalization would make every recovered quote unverifiable."""
    from harness.resources import normalize
    assert normalize("of mem- ory when") == "of memory when"
    assert normalize("multi-head attention") == "multi-head attention", "real hyphens survive"


def test_declared_hardware_is_evidence_but_never_a_vram_requirement():
    """"A single A100" says the authors had 40 GB, not that the experiment needs it. Reading
    it as a requirement would refuse experiments that fit comfortably."""
    gpu, count, ev = declared_hardware(_apt())
    assert gpu == "A100" and count == 1
    assert ev is not None and ev.kind == "declared_hardware"

    only_hw = PaperDoc(paper_id="h", title="H", sections=[Section(
        section_idx=0, title="Setup", page_start=1,
        text="All runs are conducted on a single A100 GPU.")])
    req = require_resources(only_hw)
    assert req.gpu_model == "A100"
    assert req.vram_bytes is None, "owning an A100 is not a statement of need"


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
def test_every_requirement_carries_a_quote_that_re_verifies():
    doc = _apt()
    req = require_resources(doc, "T2:r3:c11", caption=doc.tables[0].caption)
    assert req.evidence, "a requirement with no evidence is a guess"
    assert verify_requirement(req, doc) == [], "every quote must be findable in the paper"
    for ev in req.evidence:
        assert ev.quote and ev.kind


def test_a_fabricated_requirement_quote_is_caught():
    """The same discipline S2 applies to a finding's evidence. A requirement that blocks
    execution is an assertion about the paper and has to be re-checkable."""
    doc = _apt()
    req = require_resources(doc, "T2:r3:c11", caption=doc.tables[0].caption)
    req.evidence.append(ResourceEvidence(quote="we used a supercomputer", source_ref="p1",
                                         kind="declared_requirement"))
    bad = verify_requirement(req, doc)
    assert len(bad) == 1 and "supercomputer" in bad[0].quote


def test_the_three_evidence_kinds_are_distinguished():
    doc = _apt()
    req = require_resources(doc, "T2:r3:c11", caption=doc.tables[0].caption)
    kinds = {e.kind for e in req.evidence}
    assert {"declared_requirement", "declared_hardware", "derived_floor"} <= kinds, kinds


def test_a_caption_is_part_of_the_verifiable_corpus():
    """The derived floor is read off the cited table's own caption. A verifier that looked
    only at section prose would reject the requirement anchored most tightly to the cell."""
    doc = _apt()
    req = require_resources(doc, "T2:r3:c11", caption=doc.tables[0].caption)
    floor = [e for e in req.evidence if e.kind == "derived_floor"]
    assert floor and verify_requirement(req, doc) == []


# --------------------------------------------------------------------------- #
# The derived floor
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("caption,at_least_gib", [
    ("Table 3: LLaMA 2 7B 30% sparsity pruning", 12),
    ("Table 1: LLaMA 2 70B evaluation", 120),
    ("Table 4: OPT 13B ablation", 24),
])
def test_a_named_model_scale_is_a_hard_lower_bound(caption, at_least_gib):
    """Whatever the batch, schedule or precision above fp16, the weights are resident."""
    floor, scale, ev = model_weight_floor(caption, "T0:r0:c0")
    assert floor is not None and floor >= at_least_gib * GIB, (caption, floor)
    assert ev is not None and ev.kind == "derived_floor" and scale


def test_a_base_sized_encoder_is_not_charged_a_billion_parameter_floor():
    floor, _, _ = model_weight_floor("Table 2: RoBERTa-base and T5-base pruning", "T2:r0:c0")
    assert floor is not None and floor < GIB


def test_the_floor_raises_a_stated_cost_but_never_lowers_it():
    """A paper claiming 4GB for a model whose weights alone are 13GB has been misread, and
    the larger figure is the one that cannot be wrong."""
    doc = PaperDoc(
        paper_id="x", title="X",
        sections=[Section(section_idx=0, title="S", page_start=1,
                          text="Our method costs only 4GB of memory during training.")],
        tables=[Table(table_idx=0, page=1, caption="Table 1: LLaMA 2 7B results",
                      rows=[["a", "b"]])])
    req = require_resources(doc, "T0:r0:c1", caption=doc.tables[0].caption)
    assert req.vram_bytes is not None and req.vram_bytes > 12 * GIB, req.vram_bytes


def test_the_floor_follows_the_cited_table_not_the_papers_largest():
    """A paper reporting both RoBERTa and LLaMA-2-7B has two very different demands, and
    the floor has to come from the caption of the cell under audit.

    APT's real Table 2 caption names the families without a scale — "RoBERTa and T5
    pruning ... under 60% sparsity" — so no floor is derivable from it, and None is the
    honest answer rather than borrowing the 7B figure from Table 3.
    """
    apt_t2 = "Table 2: RoBERTa and T5 pruning with APT compared to baselines under 60% sparsity"
    unscaled, _, _ = model_weight_floor(apt_t2, "T1")
    assert unscaled is None, "a family name without a scale supports no floor"

    scaled, _, _ = model_weight_floor("Table 2: RoBERTa-base and T5-base pruning", "T1")
    big, _, _ = model_weight_floor("Table 3: LLaMA 2 7B 30% sparsity pruning", "T2")
    assert scaled is not None and big is not None and scaled < big / 10


# --------------------------------------------------------------------------- #
# Silence is not permission
# --------------------------------------------------------------------------- #
def test_a_paper_that_states_no_cost_yields_unknown_not_satisfied():
    quiet = PaperDoc(paper_id="q", title="Q", sections=[Section(
        section_idx=0, title="S", page_start=1,
        text="We train a transformer and report accuracy on the test split.")])
    req = require_resources(quiet)
    assert not req.stated and req.unstated
    cap = assess_resources(req, ThisMachine(), backend="local")
    assert cap.state == "unknown" and not cap.established


def test_an_unassessed_requirement_is_not_a_satisfied_one():
    assert assess_resources(None, ThisMachine()).state == "unassessed"
    assert not assess_resources(None, ThisMachine()).established


def test_a_backend_that_cannot_measure_itself_blocks():
    """A backend reporting None for VRAM has not reported zero, and must not be read as
    satisfying a stated 24 GiB."""
    doc = _apt()
    req = require_resources(doc, "T2:r3:c11", caption=doc.tables[0].caption)
    cap = assess_resources(req, BlindMachine(), backend="blind")
    assert cap.state == "unknown" and not cap.established
    assert "cannot report" in cap.reason


# --------------------------------------------------------------------------- #
# The other quantities
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field,value,label", [
    ("ram_bytes", 256 * GIB, "RAM"),
    ("disk_bytes", 4000 * GIB, "disk"),
    ("cpu_count", 128, "CPU"),
    ("gpu_count", 8, "GPU count"),
])
def test_each_quantity_is_independently_capable_of_blocking(field, value, label):
    req = ResourceRequirement(**{field: value},
                              evidence=[ResourceEvidence(quote="q", kind="declared_requirement")])
    cap = assess_resources(req, ThisMachine(), backend="local")
    assert cap.state == "insufficient" and any(label in s for s in cap.shortfalls), cap.shortfalls


def test_a_declared_walltime_beyond_the_budget_blocks():
    req = ResourceRequirement(walltime_s=72 * 3600,
                              evidence=[ResourceEvidence(quote="72 hours",
                                                         kind="declared_requirement")])
    cap = assess_resources(req, BigMachine(), backend="stub", walltime_budget_s=1800)
    assert cap.state == "insufficient" and any("walltime" in s for s in cap.shortfalls)


def test_the_host_probes_report_quantities_or_none():
    vram, name, count = host_vram_bytes()
    assert vram is None or vram > 0
    assert (name == "" and count == 0) or (name and count >= 1)
    assert host_disk_bytes() and host_disk_bytes() > 0


# --------------------------------------------------------------------------- #
# Nothing is shrunk to fit
# --------------------------------------------------------------------------- #
def test_there_is_no_path_that_shrinks_an_experiment_to_fit():
    """An altered experiment is not a reproduction. This asserts the absence of the
    affordance, because the danger is not a bug in such a function — it is having one."""
    import harness.resources as R
    for banned in ("shrink", "reduce_to_fit", "fit_batch", "downscale", "adapt_requirement",
                   "scale_down", "relax", "best_effort_requirement"):
        assert not hasattr(R, banned), f"{banned} must not exist"


def test_a_resource_refusal_leaves_the_experiment_exactly_as_published(tmp_path):
    """The seeds, arms and schedule after a refusal are the ones that went in. Nothing is
    quietly trimmed on the way to producing the INCONCLUSIVE."""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "eval.py").write_text("import os\n", encoding="utf-8")
    acq = RepoAcquisition(url="u", status="cloned", path=str(repo), entrypoint="eval.py",
                          env_status="ready", env_path=str(repo / "env"))
    before = ProbeSpec(paper_id="apt", seeds=[0, 1, 2, 3, 4], arms=["baseline", "treatment"],
                       epochs=30, table_ref="T2:r3:c11")
    after = plan_execution(_cfg(allow_repo_exec=True), before, acq, _apt())
    assert after.seeds == [0, 1, 2, 3, 4] and after.arms == ["baseline", "treatment"]
    assert after.epochs == 30
    assert after.provenance != "repo_exec" and not after.command


# --------------------------------------------------------------------------- #
# The gate, and the invariant behind it
# --------------------------------------------------------------------------- #
def test_authorization_refuses_an_experiment_that_does_not_fit():
    tight = ResourceCapability(state="insufficient",
                               reason="VRAM: requires 24.0 GiB, the 'local' backend offers 8.0 GiB",
                               shortfalls=["VRAM: 24.0 GiB vs 8.0 GiB"])
    auth = authorize(_cfg(allow_repo_exec=True), _spec(tight), local_backend(),
                     commit=_verified())
    assert not auth.allowed and auth.decision == "resources_unproven"
    assert auth.failure_class == "resources_insufficient"
    assert "24.0 GiB" in auth.detail


@pytest.mark.parametrize("resources", [
    None,
    ResourceCapability(state="unknown", reason="the paper states no cost"),
    ResourceCapability(state="unassessed", reason="never examined"),
    ResourceCapability(state="insufficient", reason="too big"),
])
def test_only_satisfied_authorizes(resources):
    auth = authorize(_cfg(allow_repo_exec=True), _spec(resources), local_backend(),
                     commit=_verified())
    assert not auth.allowed, resources
    assert auth.decision == "resources_unproven"


def test_a_satisfied_requirement_lets_authorization_through():
    auth = authorize(_cfg(allow_repo_exec=True), _spec(_fits()), local_backend(),
                     commit=_verified())
    assert auth.allowed and auth.decision == "authorized"


@pytest.mark.parametrize("values", [[59.30, 59.26], [999.0, 999.0]])
def test_a_resource_refusal_produces_neither_verdict(values):
    """Both directions. A number obtained without establishing that the experiment fits has
    no standing to acquit a cell either."""
    tight = ResourceCapability(state="insufficient", reason="too big")
    auth = authorize(_cfg(allow_repo_exec=True), _spec(tight), local_backend(),
                     commit=_verified())
    spec = ProbeSpec(paper_id="apt", provenance="repo_exec", table_ref="T1:r0:c1",
                     claimed_cell_value="59.28")
    rec = reconcile(spec, values, 0.10, [0, 1], authorization=auth)
    assert rec.status == "INCONCLUSIVE"
    assert rec.failure_class == "resources_insufficient"


def test_a_resource_refusal_cannot_drive_the_paper_red():
    tight = ResourceCapability(state="insufficient", reason="too big")
    auth = authorize(_cfg(allow_repo_exec=True), _spec(tight), local_backend(),
                     commit=_verified())
    spec = ProbeSpec(paper_id="apt", provenance="repo_exec", table_ref="T1:r0:c1",
                     claimed_cell_value="59.28")
    rec = reconcile(spec, [999.0, 999.0], 0.10, [0, 1], authorization=auth)
    verdict, _ = overall_verdict([], rec)
    assert verdict == "GREEN", "an 8 GB card says nothing about a paper"
