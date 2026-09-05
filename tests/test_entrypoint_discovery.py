"""Discovery — a repository that names its entrypoint must be heard naming it.

Discovery decides only whether the DEEPER assessment runs at all. It cannot bind a claim
to a command and it authorises nothing; experiment identity and `backends.authorize`
remain the gates. But a discovery miss is not harmless, because it misattributes the
resulting non-reproduction: the harness stops at "no runnable entrypoint" and never
reaches the capability and resource questions that actually decide the paper, so the
report names the wrong terminal stage.

Every case below is drawn from a real repository in the current evaluation corpus:

  * a README advertising `python <dir>/<verb>_<noun>.py` in a directory the fixed
    candidate list does not scan, under a filename it does not spell;
  * a README whose command block is preceded by a ```python example, which shifted every
    fence boundary by one and hid the commands entirely;
  * a README advertising `srun python3 main_simclr.py` — a scheduler prefix in front of
    an otherwise plain invocation, invisible to an anchored `python|bash|make` pattern.

None of these repositories was hard to run for the reason the harness reported.
"""
from __future__ import annotations

from pathlib import Path

from harness.experiment_id import harvest_candidates
from harness.repo import find_entrypoint


def _repo(tmp_path: Path, *, files: dict[str, str], readme: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if readme:
        (repo / "README.md").write_text(readme, encoding="utf-8")
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return repo


# --------------------------------------------------------------------------- #
# What the repository advertises outranks what its files are called
# --------------------------------------------------------------------------- #
def test_an_advertised_program_outside_the_candidate_list_is_found(tmp_path: Path):
    """`chaineval/evaluate_predictions.py`: right directory, wrong spelling, plainly advertised."""
    repo = _repo(tmp_path, files={"pkg/evaluate_predictions.py": "print(1)\n"},
                 readme="## Evaluate\n```bash\npython pkg/evaluate_predictions.py --input x\n```\n")
    assert find_entrypoint(repo) == "pkg/evaluate_predictions.py"


def test_an_advertised_program_outranks_a_conventionally_named_file(tmp_path: Path):
    """A repo holding both must be judged on what it TELLS a reader to run."""
    repo = _repo(tmp_path,
                 files={"evaluate.py": "# a profiler, not the experiment\n",
                        "scripts/run_task.sh": "python train.py\n"},
                 readme="```bash\nbash scripts/run_task.sh\n```\n")
    assert find_entrypoint(repo) == "scripts/run_task.sh"


def test_the_filename_convention_still_applies_when_nothing_is_advertised(tmp_path: Path):
    """The fallback is unchanged: a repository that advertises nothing is read as before."""
    repo = _repo(tmp_path, files={"evaluate.py": "x=1\n"})
    assert find_entrypoint(repo) == "evaluate.py"


def test_a_repository_with_neither_yields_no_entrypoint(tmp_path: Path):
    assert find_entrypoint(_repo(tmp_path, files={"lib/helper.py": "x=1\n"})) == ""


def test_an_advertised_program_that_does_not_exist_is_not_an_entrypoint(tmp_path: Path):
    """A README may name a file the checkout does not contain. Discovery is not invention."""
    repo = _repo(tmp_path, files={"evaluate.py": "x=1\n"},
                 readme="```bash\npython does_not_exist.py\n```\n")
    assert find_entrypoint(repo) == "evaluate.py", "falls through to what is actually there"


# --------------------------------------------------------------------------- #
# Launcher prefixes hide an otherwise plain invocation
# --------------------------------------------------------------------------- #
def test_a_scheduler_prefixed_command_is_still_an_advertised_command(tmp_path: Path):
    repo = _repo(tmp_path, files={"main_simclr.py": "x=1\n"},
                 readme="```\nsrun python3 main_simclr.py --ddp --dist_eval\n```\n")
    assert ["python3", "main_simclr.py"] in [c.argv for c in harvest_candidates(repo)]
    assert find_entrypoint(repo) == "main_simclr.py"


def test_a_multi_gpu_launcher_with_its_own_flags_is_unwrapped(tmp_path: Path):
    """`accelerate launch --mixed_precision fp16 train.py` advertises `train.py`."""
    repo = _repo(tmp_path, files={"train.py": "x=1\n"},
                 readme="```bash\naccelerate launch --mixed_precision 'fp16' train.py\n```\n")
    assert ["python", "train.py"] in [c.argv for c in harvest_candidates(repo)]


def test_a_torchrun_prefix_is_unwrapped(tmp_path: Path):
    repo = _repo(tmp_path, files={"run_glue.py": "x=1\n"},
                 readme="```bash\ntorchrun --nproc_per_node 4 run_glue.py --task mnli\n```\n")
    assert ["python", "run_glue.py"] in [c.argv for c in harvest_candidates(repo)]


def test_a_launcher_naming_no_program_advertises_nothing(tmp_path: Path):
    """Recognising a launcher must not manufacture a command out of its flags."""
    repo = _repo(tmp_path, files={"train.py": "x=1\n"},
                 readme="```bash\nsrun --nodes 4 --gres gpu:4\n```\n")
    assert [c for c in harvest_candidates(repo) if "srun" in " ".join(c.argv)] == []
    assert [c for c in harvest_candidates(repo)
            if c.source == "readme" and c.argv[-1].endswith(".py")] == []


# --------------------------------------------------------------------------- #
# Fence pairing
# --------------------------------------------------------------------------- #
def test_a_python_example_block_does_not_hide_a_later_command_block(tmp_path: Path):
    """The ```python block used to consume the fence that opened the command block."""
    readme = ("## Method\n```python\ndef loss(x):\n    return x\n```\n\n"
              "## Run\n```\npython main_linear_prob.py --exp_name lp\n```\n")
    repo = _repo(tmp_path, files={"main_linear_prob.py": "x=1\n"}, readme=readme)
    argvs = [" ".join(c.argv) for c in harvest_candidates(repo)]
    assert "python main_linear_prob.py --exp_name lp" in argvs


def test_python_source_lines_are_not_mistaken_for_commands(tmp_path: Path):
    """Admitting every info string must not turn example code into an advertised command."""
    readme = "```python\nimport torch\nmodel.train()\nx = make(1)\n```\n"
    repo = _repo(tmp_path, files={"train.py": "x=1\n"}, readme=readme)
    assert harvest_candidates(repo) == []


# --------------------------------------------------------------------------- #
# Discovery is not authorization
# --------------------------------------------------------------------------- #
def test_discovery_alone_binds_nothing(tmp_path: Path):
    """An entrypoint is a gate for assessment, never a claim that this is the experiment.

    The guard that matters lives in `experiment_id`/`backends.authorize`; this test pins
    the boundary so a future widening of discovery cannot quietly become a mapping.
    """
    repo = _repo(tmp_path, files={"pkg/evaluate_predictions.py": "print(1)\n"},
                 readme="```bash\npython pkg/evaluate_predictions.py\n```\n")
    entry = find_entrypoint(repo)
    assert entry == "pkg/evaluate_predictions.py"
    # The candidate carries a source_ref back to the line that advertised it, so the
    # mapping stays auditable rather than asserted.
    cmd = next(c for c in harvest_candidates(repo) if c.argv[-1].endswith(".py"))
    assert cmd.source == "readme" and cmd.source_ref.startswith("README.md:")
