"""Runtime demands read out of a checkout — and, mostly, what must NOT be read out of one.

Six detection rules were designed against the APT author checkout and then attacked. All
six came back the same way: the evidence supports an OBSERVATION and not a CONCLUSION. So
this layer is report-only. It puts a file, a line and a verbatim token in front of a
reader, and it gates nothing — `select_for` and `authorize` never see it.

That is not caution for its own sake. Every test below whose name begins `test_no` or
`test_a_..._is_not` pins a rule that looked sound and was fatal:

  conda `=` is fuzzy, not PEP 440. `python=3.9` is the 3.9 SERIES, so a mismatch computed
  against a backend's own Python would be a refusal resting on a normalisation this module
  invented — and `environment.yml` here is a `conda env export` of one developer's machine.

  `nvidia-cublas-cu11` is a payload of .so files that installs and imports on a CPU-only
  host. It is a build tag, not the sentence "a GPU must be present".

  co-present cu11 and cu12 pins are two libraries' transitive closures — torch 2.0.1's and
  cupy-cuda12x's. Reading them as a conflict reports a contradiction that is not in the file.

  `load_dataset("csv", data_files=...)` names a builder shipped inside the `datasets` wheel.
  Its demand is "this local path must exist" — the opposite of a download.

  `from_pretrained(model_name_or_path)` names no checkpoint. A rule that guessed from other
  literals in the same file offered 'right', 'epoch' and 'max_length' as candidates.

The real APT checkout is used where it is present, because a fixture cannot show that a
rule survives 165 files, 174 declaration files and 74 submission scripts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness import code_audit
from harness.artifacts import (RUNTIME_KINDS, RUNTIME_SCOPES, RUNTIME_STATES,
                               CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExperimentIdentity, MetricIdentity, ProbeSpec,
                               ResourceCapability)
from harness.backends import authorize, local_backend, select_for
from harness.config import Config

APT = Path("projects/apt-icml/runs/apt-icml/repo")
_HAS_APT = APT.is_dir()
needs_apt = pytest.mark.skipif(not _HAS_APT, reason="the APT checkout is not present")


def _repo(tmp_path: Path, **files) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def _kinds(audit, kind: str) -> list:
    return [d for d in audit.runtime if d.kind == kind]


# --------------------------------------------------------------------------- #
# Python version
# --------------------------------------------------------------------------- #
def test_an_explicit_conda_interpreter_pin_is_established_verbatim(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "name: x\ndependencies:\n  - python=3.9.19=h955ad1f_1\n  - pip\n"}))
    (d,) = _kinds(a, "python_version")
    assert d.state == "established" and d.line == 3
    assert "3.9.19" in d.value, "the token is stored verbatim, not normalised to a specifier"
    assert "fuzzy" in d.note, "the note must say conda '=' is not PEP 440 '=='"


def test_python_requires_is_read_from_packaging_metadata(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "setup.py": "setup(name='x', python_requires='>=3.9,<3.11')\n"}))
    (d,) = _kinds(a, "python_version")
    assert d.value == ">=3.9,<3.11" and d.state == "established"


def test_two_declarations_that_disagree_establish_no_interpreter(tmp_path):
    """The binder/CI shape. A demo env pins an old Python on purpose; it is not the
    training environment, and picking either one would be a guess."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "dependencies:\n  - python=3.9\n",
        "binder/environment.yml": "dependencies:\n  - python=3.7\n"}))
    pins = _kinds(a, "python_version")
    assert len(pins) == 2
    assert {d.state for d in pins} == {"ambiguous"}
    assert all("do not agree" in d.note for d in pins)


def test_a_package_whose_name_merely_starts_with_python_is_not_an_interpreter(tmp_path):
    """`python-dateutil==2.9.0.post0` sits in APT's own environment.yml. An unanchored
    match reports a required Python of 2.9.0.post0, which is not a Python that exists."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "dependencies:\n  - python-dateutil==2.9.0.post0\n"
                           "  - pythonwin==1.0\n"}))
    assert _kinds(a, "python_version") == []


def test_a_pip_block_pin_is_not_an_interpreter_pin(tmp_path):
    """Conda nests a `pip:` list. A PyPI package in there is not the interpreter."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "dependencies:\n  - pip:\n    - python-lsp-server==1.0\n"}))
    assert _kinds(a, "python_version") == []


def test_no_demand_is_read_out_of_a_comment(tmp_path):
    """This harness's own requirements.txt carries `cu126`, `cu121`, `+cu126` and
    'Python 3.13' on commented lines. A naive extractor reports two requirements."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "requirements.txt": "# Use cu126, NOT cu121: no wheels for Python 3.13\n"
                            "#   pip install torch --index-url .../whl/cu126\n"
                            "# Verified: torch 2.13.0+cu126\n"
                            "pydantic>=2\n",
        "run.sh": "# wget -O data.tar https://example.org/data.tar\n"}))
    assert a.runtime == [], [d.value for d in a.runtime]
    assert "requirements.txt" in a.declarations_scanned, "it must be READ and yield nothing"


# --------------------------------------------------------------------------- #
# CUDA — a build tag, never a hardware demand
# --------------------------------------------------------------------------- #
def test_a_cuda_build_tag_is_established_as_a_tag(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "requirements.txt": "torch==1.10.2+cu113\n"
                            "--extra-index-url https://download.pytorch.org/whl/cu113\n"}))
    tags = _kinds(a, "cuda_runtime")
    assert len(tags) == 1, "one demand per tag family — both lines say cu113"
    assert tags[0].value == "+cu113" and tags[0].line == 1
    assert "nothing about whether a GPU must be present" in tags[0].note


def test_gpu_usage_without_a_declared_cuda_yields_no_cuda_demand(tmp_path):
    """`.cuda()` is the case the brief singles out. There is no rule for it, deliberately:
    a call site cannot say which CUDA the run needs."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "train.py": "import torch\n"
                    "if torch.cuda.is_available():\n"
                    "    model = model.cuda()\n"
                    "dev = torch.device('cuda:0')\n"
                    "print(torch.version.cuda)\n"}))
    assert _kinds(a, "cuda_runtime") == []


def test_co_present_cu11_and_cu12_pins_are_not_reported_as_a_conflict(tmp_path):
    """APT's real environment.yml. The cu11 block is torch 2.0.1's transitive closure and
    the cu12 block is cupy-cuda12x's — two libraries, two closures, no contradiction."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "dependencies:\n  - pip:\n"
                           "    - cupy-cuda12x==13.1.0\n"
                           "    - nvidia-cublas-cu11==11.10.3.66\n"
                           "    - nvidia-cublas-cu12==12.1.3.1\n"
                           "    - nvidia-cudnn-cu11==8.5.0.96\n"
                           "    - nvidia-cudnn-cu12==8.9.2.26\n"}))
    tags = _kinds(a, "cuda_runtime")
    assert {d.state for d in tags} == {"established"}, "co-presence is not ambiguity"
    assert len(tags) == 3, "one per family (cuda12x, cu11, cu12), not one per package"


# --------------------------------------------------------------------------- #
# Network / downloads
# --------------------------------------------------------------------------- #
def test_an_explicit_shell_download_is_established(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "scripts/prepare.sh": "mkdir -p data\n"
                              "# Download the eval data\n"
                              "wget -O data/x.tar https://example.org/x.tar\n"}))
    (d,) = _kinds(a, "external_download")
    assert d.line == 3 and d.value == "https://example.org/x.tar"
    assert "wget" in d.code_quote


def test_a_harmless_network_import_establishes_nothing(tmp_path):
    """`import requests` is not an acquisition. Neither is a helper whose name happens to
    contain a library's — APT has `select_wandb(weight, bias)`, where wandb means
    'weight and bias'."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "net.py": "import requests\nimport urllib.request\nfrom openai import OpenAI\n\n"
                  "def select_wandb(weight, bias):\n    return weight, bias\n"}))
    assert _kinds(a, "external_download") == []


def test_a_function_whose_only_purpose_is_fetching_is_established(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "load.py": "from huggingface_hub import hf_hub_download\n"
                   "p = hf_hub_download('org/repo', 'f.bin')\n"}))
    (d,) = _kinds(a, "external_download")
    assert d.value == "hf_hub_download" and d.line == 2


# --------------------------------------------------------------------------- #
# Model / dataset artifacts — identity only
# --------------------------------------------------------------------------- #
def test_a_literal_checkpoint_establishes_identity_and_nothing_more(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "m.py": "m = AutoModel.from_pretrained('princeton-nlp/CoFi-MNLI-s95')\n"}))
    (d,) = _kinds(a, "model_artifact")
    assert d.value == "princeton-nlp/CoFi-MNLI-s95"
    assert "not that it is present" in d.note
    assert "resolves from a local cache" in d.note, "a hub id is not proof of a download"


def test_a_variable_checkpoint_names_no_artifact(tmp_path):
    """21 of APT's 25 call sites are of this shape. A rule that guessed from other literals
    in the same file offered 'right', 'epoch' and 'max_length' as checkpoints."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "m.py": "mode = 'right'\nmax_length = 128\n"
                "m = AutoModel.from_pretrained(model_name_or_path)\n"
                "t = AutoTokenizer.from_pretrained(args.tok if x else other)\n"}))
    assert _kinds(a, "model_artifact") == []


def test_a_literal_dataset_id_is_established(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "d.py": "ds = load_dataset('squad_v2')\n"}))
    (d,) = _kinds(a, "dataset_artifact")
    assert d.value == "squad_v2"


@pytest.mark.parametrize("builder", ["csv", "json", "text", "parquet", "imagefolder"])
def test_a_packaged_builder_is_a_local_read_not_a_dataset(tmp_path, builder):
    """`load_dataset('csv', data_files=...)` demands that a LOCAL path exist. Reporting it
    as a dataset acquisition inverts the requirement."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "d.py": f"ds = load_dataset({builder!r}, data_files='./data/train.csv')\n"}))
    assert _kinds(a, "dataset_artifact") == []


def test_an_absolute_path_literal_is_its_own_kind(tmp_path):
    """APT loads `from_pretrained('/data/zbw/delta/out-test/MNLI/MNLI_bert-mnli/')`. That is
    a fact about one machine, and it is not a checkpoint identity."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "p.py": "m = M.from_pretrained('/data/zbw/delta/out-test/MNLI/MNLI_bert-mnli/')\n"}))
    assert _kinds(a, "model_artifact") == []
    (d,) = _kinds(a, "absolute_path")
    assert d.value.startswith("/data/zbw/")
    assert "nothing establishes that it exists anywhere else" in d.note


def test_a_relative_path_is_not_a_platform_requirement(tmp_path):
    """A shell-text regex for this was designed and abandoned: over 172 real shell files it
    produced 68 hits of which 3 were real, slicing `/elastictuning` out of the middle of a
    relative `output/${model}/...` and turning `~/miniconda3/x` into a `/miniconda3` that
    exists on no machine. Only Python string literals are read."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "r.py": "p = os.path.join('output', name, 'ckpt')\nq = './data/train.csv'\n",
        "run.sh": 'out="output/${model}/${task}/elastictuning/mac${c}"\n'
                  "source ~/miniconda3/etc/profile.d/conda.sh\n"}))
    assert _kinds(a, "absolute_path") == []


# --------------------------------------------------------------------------- #
# Scope — a repository is not an experiment
# --------------------------------------------------------------------------- #
def test_an_unbound_scheduler_block_is_summarised_once_as_ambiguous(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "scripts/a.sh": "#SBATCH --mem=32G\n#SBATCH --time=24:00:00\n",
        "scripts/b.sh": "#SBATCH --mem=64G\n#SBATCH --time=500:00:00\n"}))
    (d,) = _kinds(a, "scheduler_allocation")
    assert d.state == "ambiguous" and d.value == ""
    assert "2 script(s)" in d.note and "none could be bound" in d.note


def test_a_scheduler_block_is_read_only_for_the_named_entrypoint(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "scripts/a.sh": "#SBATCH --mem=32G\n#SBATCH --gres=gpu:1\n",
        "scripts/b.sh": "#SBATCH --mem=64G\n"}), entrypoint="scripts/a.sh")
    bound = [d for d in _kinds(a, "scheduler_allocation") if d.state == "established"]
    assert {d.value for d in bound} == {"--mem=32G", "--gres=gpu:1"}
    assert all(d.file == "scripts/a.sh" for d in bound), "b.sh's 64G must not be attributed"
    assert all(d.scope == "experiment" for d in bound)


def test_without_an_entrypoint_every_demand_is_repository_scoped(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "dependencies:\n  - python=3.9\n",
        "d.py": "ds = load_dataset('squad')\n"}))
    assert a.runtime and {d.scope for d in a.runtime} == {"repository"}


def test_scope_marks_only_the_entrypoint_file(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "mine.py": "ds = load_dataset('squad')\n",
        "other.py": "ds = load_dataset('xsum')\n"}), entrypoint="mine.py")
    by_file = {d.file: d.scope for d in _kinds(a, "dataset_artifact")}
    assert by_file == {"mine.py": "experiment", "other.py": "repository"}


# --------------------------------------------------------------------------- #
# The invariant: none of this may permit or convict
# --------------------------------------------------------------------------- #
def _qualified() -> ProbeSpec:
    est = dict(state="established", established=True, reason="fixture")
    return ProbeSpec(
        paper_id="p", command=["python", "eval.py"], provenance="repo_exec",
        experiment=ExperimentIdentity(**est), metric_identity=MetricIdentity(**est),
        configuration=ConfigurationIdentity(**est),
        capability=ExecCapability(established=True, reason_code="established"),
        resources=ResourceCapability(state="satisfied", reason="fits"))


def test_no_runtime_demand_reaches_selection_or_authorization(tmp_path):
    """The load-bearing test. Every rule here is report-only, so a demand cannot be the
    reason a reproduction is refused — and cannot be the reason one is permitted either.

    Verified structurally rather than by assertion about intent: `select_for` takes a
    `ResourceRequirement` and `authorize` takes a spec, a backend and a commit. Neither
    signature can receive a `RuntimeDemand`, and `CodeAudit` is not among their arguments.
    """
    import inspect

    for fn in (select_for, authorize):
        params = " ".join(inspect.signature(fn).parameters)
        assert "runtime" not in params and "audit" not in params, params

    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "dependencies:\n  - python=3.9.19\n",
        "scripts/p.sh": "wget -O d https://example.org/d\n",
        "m.py": "m = M.from_pretrained('org/name')\n"}))
    assert len(a.runtime) >= 3, "the fixture really does carry demands"

    cfg = Config.load()
    cfg.allow_repo_exec = True
    good = CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)
    auth = authorize(cfg, _qualified(), local_backend(), commit=good)
    assert auth.allowed, "a fully qualified spec is still authorized with demands present"
    assert auth.failure_class == "none"


def test_a_runtime_demand_is_never_a_finding_about_the_paper(tmp_path):
    """`CodeAudit.findings` is the defect channel — the report renders it as 'suspicions
    with line numbers'. A requirement filed there reads as an accusation."""
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "dependencies:\n  - python=3.9\n",
        "m.py": "m = M.from_pretrained('org/name')\n"}))
    assert a.runtime, "demands were found"
    assert all(f.category in ("baseline_crippling", "data_leakage", "metric_deviation")
               for f in a.findings), "no demand leaked into the defect channel"
    assert not any(getattr(d, "severity", None) for d in a.runtime), "demands carry no severity"


def test_every_demand_is_locatable_and_uses_the_declared_vocabulary(tmp_path):
    a = code_audit.audit_repo(_repo(tmp_path, **{
        "environment.yml": "dependencies:\n  - python=3.9\n  - pip:\n    - torch==2.0.1+cu118\n",
        "scripts/p.sh": "#SBATCH --mem=8G\nwget -O d https://example.org/d\n",
        "m.py": "m = M.from_pretrained('org/name')\nds = load_dataset('squad')\n"}))
    assert a.runtime
    for d in a.runtime:
        assert d.kind in RUNTIME_KINDS and d.state in RUNTIME_STATES
        assert d.scope in RUNTIME_SCOPES
        assert d.file and d.note, d
        # The one row with no line is the summarised scheduler block, which cites a count
        # rather than a single directive.
        assert d.line > 0 or d.kind == "scheduler_allocation"


def test_a_checkout_with_nothing_to_declare_is_distinguishable_from_one_unread(tmp_path):
    """An empty demand list means two opposite things without `declarations_scanned`."""
    silent = code_audit.audit_repo(_repo(tmp_path, **{"requirements.txt": "numpy>=1.26\n"}))
    assert silent.runtime == [] and "requirements.txt" in silent.declarations_scanned

    missing = code_audit.audit_repo(tmp_path / "nope")
    assert missing.runtime == [] and missing.declarations_scanned == []
    assert missing.skipped, "a checkout that does not exist says so"


# --------------------------------------------------------------------------- #
# The real checkout
# --------------------------------------------------------------------------- #
@needs_apt
def test_the_apt_checkout_yields_its_actual_declarations():
    a = code_audit.audit_repo(APT)
    found = {(d.kind, d.file, d.line): d for d in a.runtime}

    py = found[("python_version", "environment.yml", 16)]
    assert py.state == "established" and "3.9.19" in py.value

    assert ("cuda_runtime", "requirements.txt", 14) in found, "torch==1.10.2+cu113"
    assert len(_kinds(a, "cuda_runtime")) == 4, "24 nvidia-* pins are 2 closures, not 24 facts"

    urls = {d.line for d in _kinds(a, "external_download")}
    assert urls == {5, 10, 17, 18}, "scripts/prepare_data.sh, comment on line 4 excluded"
    assert all(d.file == "scripts/prepare_data.sh" for d in _kinds(a, "external_download"))

    assert ("absolute_path", "post_analysis.py", 468) in found
    assert {d.value for d in _kinds(a, "model_artifact")} == {
        "bert-base-uncased", "princeton-nlp/CoFi-MNLI-s95"}
    # `load_dataset("csv", ...)` at utils/utils.py:77 is excluded.
    assert "csv" not in {d.value for d in _kinds(a, "dataset_artifact")}

    (sched,) = _kinds(a, "scheduler_allocation")
    assert sched.state == "ambiguous", "74 scripts disagree and none is bound to the cell"
    assert {d.scope for d in a.runtime} == {"repository"}, "APT has no established experiment"
    assert len(a.declarations_scanned) > 100, "the declaration pass really read the tree"


@needs_apt
def test_the_apt_checkout_still_produces_the_same_static_findings():
    """The runtime pass must not have disturbed the defect pass it shares a walk with."""
    a = code_audit.audit_repo(APT)
    assert a.files_scanned == 165
    assert all(f.severity in ("MAJOR", "MINOR") for f in a.findings), "still never FATAL"
