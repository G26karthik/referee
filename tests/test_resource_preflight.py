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


# --------------------------------------------------------------------------- #
# C7 — three concrete underestimation defects
# --------------------------------------------------------------------------- #
def test_a_cheaper_baseline_is_never_chosen_over_the_methods_own_larger_cost():
    """C7.1 — smallest-anywhere used to be the whole rule, so a baseline's cost winning
    only worked when it happened to be numerically larger than the method's own. Reverse
    the ordering — a CHEAPER baseline stated first, the method's own larger cost second —
    and the picked figure must still be the method's, not the smaller baseline's."""
    doc = PaperDoc(paper_id="p", title="P", sections=[Section(
        section_idx=0, text=(
            "In contrast, the baseline needs only about 8GB of memory to run. "
            "Our method costs less than 24GB of memory when training the full model."))])
    value, ev = declared_memory_cost(doc)
    assert value is not None and abs(value - 24 * GIB) < GIB, value
    assert ev is not None and "8GB" not in ev.quote


def test_a_paper_stating_only_a_baselines_cost_yields_no_declared_cost():
    """C7.1, the abstention half — if every memory-cost sentence in the paper is
    contrast-flagged (about some OTHER method), nothing is returned. Attributing a
    comparison figure to the method under audit would be exactly the misattribution
    this function exists to avoid; it must abstain, not guess."""
    doc = PaperDoc(paper_id="p", title="P", sections=[Section(
        section_idx=0, text="Compared to ours, the prior baseline requires 8GB of memory.")])
    value, ev = declared_memory_cost(doc)
    assert value is None and ev is None


def test_the_fp16_floor_alone_does_not_satisfy_the_resource_check():
    """C7.2 — a lower bound (weights only, fp16) is not a requirement. A backend meeting
    the floor with no declared cost to anchor it must not be reported `satisfied`; the
    true demand (activations, optimizer state, gradients, KV cache) was never stated."""
    doc = PaperDoc(paper_id="p", title="P", tables=[Table(
        table_idx=0, page=1, caption="Table 1: LLaMA 2 7B results", rows=[["ours", "1"]])])
    req = require_resources(doc, "T0:r0:c0", caption=doc.tables[0].caption)
    assert req.vram_is_floor_only is True
    huge = BigMachine()
    cap = assess_resources(req, huge, backend="stub")
    assert cap.state == "unknown", cap.reason
    assert not cap.established
    assert "lower bound" in cap.reason


def test_a_declared_cost_still_satisfies_even_when_a_floor_also_applies():
    """The floor-only refusal must not swallow a genuinely DECLARED cost — APT's own
    fixture states 24GB, which is a real requirement, not a bare weight-count floor."""
    doc = _apt()
    req = require_resources(doc, "T2:r3:c11", caption=doc.tables[0].caption)
    assert req.vram_is_floor_only is False
    assert assess_resources(req, BigMachine(), backend="stub").state == "satisfied"


@pytest.mark.parametrize("sentence", [
    "Our method uses only 30% of the 24GB memory the baseline needs.",
    "This reduces memory to 24GB (12% of the original footprint).",
])
def test_a_percent_qualified_figure_is_not_read_as_an_absolute_cost(sentence):
    """C7.3 — a number immediately qualified by a percent sign states a RATIO, not a
    standalone cost. Reading the absolute figure out of it drops the qualifier that made
    it a ratio, silently inflating or deflating the actual demand."""
    doc = PaperDoc(paper_id="p", title="P", sections=[Section(section_idx=0, text=sentence)])
    value, ev = declared_memory_cost(doc)
    assert value is None and ev is None


def test_an_unrelated_percent_elsewhere_in_the_sentence_does_not_disqualify_a_real_cost():
    """The percent guard is LOCAL to the number, not sentence-wide — APT's own sentence
    ("24GB of memory when pruning 30% parameters") has a percent sign nowhere near the
    memory figure, and must still be read."""
    value, ev = declared_memory_cost(_apt())
    assert value is not None and abs(value - 24 * GIB) < GIB, value


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


def test_plan_execution_cannot_promote_when_only_resources_are_short(tmp_path, monkeypatch):
    """Mutation guarantee: `plan_execution`'s `not fits` condition is load-bearing on its
    own, isolated from identity, capability and commit — every OTHER precondition is
    stubbed to succeed here, so only the resource check stands between this spec and
    `repo_exec`. If it were ever dropped from the `or` chain, this is the one test that
    would still catch it; the existing end-to-end APT fixture cannot, because on this
    host identity/capability/commit fail right alongside resources and mask the mutation.
    """
    from harness import repo as repo_mod
    from harness.artifacts import CandidateCommand
    from harness.stages import probe as probe_stage

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "eval.py").write_text('import os\np.add_argument("--seed")\n', encoding="utf-8")
    monkeypatch.setattr(probe_stage.repo_mod, "assess_capability",
                        lambda *a, **k: ExecCapability(established=True, reason_code="established"))
    monkeypatch.setattr(probe_stage.experiment_id, "resolve", lambda *a, **k: (
        ExperimentIdentity(state="established",
                           command=CandidateCommand(argv=["python", "eval.py"],
                                                    source_ref="README.md:1")),
        MetricIdentity(state="established"), ConfigurationIdentity(state="established")))
    monkeypatch.setattr(probe_stage.repo_mod, "verify_commit",
                        lambda *a, **k: CommitVerification(state="verified", expected="a" * 40,
                                                           actual="a" * 40))
    # The ONE precondition under test: resources insufficient, everything else established.
    monkeypatch.setattr(probe_stage.resources_mod, "assess_resources",
                        lambda *a, **k: ResourceCapability(state="insufficient", reason="stub"))

    cfg = Config(projects_dir=tmp_path, allow_repo_exec=True)
    acq = RepoAcquisition(url="u", status="cloned", path=str(repo), entrypoint="eval.py",
                          env_status="ready", env_path="/tmp/env/python")
    spec = plan_execution(cfg, ProbeSpec(paper_id="p"), acq, PaperDoc(paper_id="p", title="T"))
    assert spec.provenance != "repo_exec", "resources insufficient must block promotion alone"
    assert not spec.command
    assert repo_mod is not None


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
def test_authorization_refuses_an_experiment_that_does_not_fit(confined_local):
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
def test_only_satisfied_authorizes(confined_local, resources):
    auth = authorize(_cfg(allow_repo_exec=True), _spec(resources), local_backend(),
                     commit=_verified())
    assert not auth.allowed, resources
    assert auth.decision == "resources_unproven"


def test_a_satisfied_requirement_lets_authorization_through(confined_local):
    auth = authorize(_cfg(allow_repo_exec=True), _spec(_fits()), local_backend(),
                     commit=_verified())
    assert auth.allowed and auth.decision == "authorized"


@pytest.mark.parametrize("values", [[59.30, 59.26], [999.0, 999.0]])
def test_a_resource_refusal_produces_neither_verdict(confined_local, values):
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


# --------------------------------------------------------------------------- #
# The demand that decides — and the leak that let five unstated ones ride on one
# --------------------------------------------------------------------------- #
def _one(**kw) -> ResourceRequirement:
    return ResourceRequirement(
        evidence=[ResourceEvidence(quote="q", kind="declared_requirement")], **kw)


@pytest.mark.parametrize("field,value", [
    ("cpu_count", 2), ("disk_bytes", 1 * GIB), ("walltime_s", 60), ("gpu_count", 1),
])
def test_a_stated_non_memory_demand_does_not_vouch_for_an_unstated_memory_one(field, value):
    """The leak, closed. `stated` is an OR over six fields and gated the whole check.

    A requirement naming only `cpu_count=2` reached `satisfied`, because every unstated
    field matched vacuously — `need is None` reads as "fits" — and the reason string said
    so out loud: "every declared requirement fits the backend: CPU 2 <= 16". The one
    quantity that decides whether a run reaches its first measurement had never been
    compared with anything. SAPG is a real instance: a scraped "60 hours" makes
    `stated` true with vram, ram, disk and cpu all unestablished.
    """
    req = _one(**{field: value})
    assert req.stated and not req.memory_stated
    cap = assess_resources(req, ThisMachine(), backend="local", walltime_budget_s=3600)
    assert cap.state == "unknown", cap.reason
    assert "memory" in cap.reason


@pytest.mark.parametrize("field,value", [("vram_bytes", 2 * GIB), ("ram_bytes", 2 * GIB)])
def test_either_memory_figure_is_enough_to_assess(field, value):
    """A CPU-only experiment's binding constraint is host RAM. Demanding a VRAM number
    from it would block runs that fit, which is the opposite failure."""
    req = _one(**{field: value})
    assert req.memory_stated
    assert assess_resources(req, ThisMachine(), backend="local").state == "satisfied"


def test_a_definite_shortfall_still_outranks_an_unknown_memory_demand():
    """Ordering matters. An experiment declaring 128 CPUs on a 16-core host is refused,
    and naming that is more useful than reporting that its memory was never stated."""
    req = _one(cpu_count=128)
    cap = assess_resources(req, ThisMachine(), backend="local")
    assert cap.state == "insufficient" and any("CPU" in s for s in cap.shortfalls)


def test_authorization_refuses_a_requirement_with_no_memory_figure(confined_local):
    spec = _spec(assess_resources(_one(cpu_count=2), ThisMachine(), backend="local"))
    auth = authorize(_cfg(allow_repo_exec=True), spec, local_backend(), commit=_verified())
    assert not auth.allowed and auth.decision == "resources_unproven"
    assert auth.failure_class == "resources_insufficient"
