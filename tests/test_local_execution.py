"""E3 — the LocalBackend actually running something, through the whole chain.

Everything from `authorize()` down had been tested against stubs: the gates were proven
to refuse, and the arithmetic was proven correct, but no test drove a real process from
authorization through execution to a reproduction verdict. `RESOLVED_VERIFIED` and
`FAILED_REPRODUCTION` were reachable in principle and had never been produced.

These tests produce both, against a **synthetic local fixture** — a throwaway git
repository with a script this harness wrote. That fixture is plumbing evidence and
nothing else: it establishes that the execution path works, and it says nothing whatever
about any paper. A real reproduction additionally requires the paper's own repository,
its commit, and identity resolved from the paper's own tables, none of which a fixture
can supply.

The execution gate is opened on an **in-memory `Config` object** for these tests. No
environment variable is set, so nothing outside the test process is affected and the
harness's own defaults are untouched.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.artifacts import (CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExecutionRecord, ExperimentIdentity, MetricIdentity, ProbeSpec,
                               ResourceCapability)
from harness.backends import ExecRequest, LocalBackend, authorize, local_backend
from harness.config import Config
from harness.local_exec import run_probe, verify_execution_commit
from harness.repo import head_commit, verify_commit
from harness.stages.report import overall_verdict

# The value the fixture's "paper" prints in its "table", and what the script emits.
CELL = "59.28"


# --------------------------------------------------------------------------- #
# A real git repository with a real script
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> None:
    p = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"


def _repo(tmp_path: Path, body: str, name: str = "fixture") -> tuple[Path, str]:
    """A throwaway checkout whose `run.py` is `body`. Returns (path, commit)."""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    (repo / "run.py").write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    return repo, head_commit(repo)


EMITS_MATCH = """\
import argparse, sys
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
print("SH_DEVICE cpu")
# Deterministic, seed-dependent, and within a hair of the cited cell.
print(f"SH_METRIC arm=reproduction seed={a.seed} value={59.28 + a.seed * 0.01:.4f}")
"""

EMITS_MISMATCH = EMITS_MATCH.replace("59.28 +", "71.40 +")

# Emits a seed-varying metric that matches the cell, THEN dies — a post-measurement
# condition (here, a stray exception after the result line; in the wild, a telemetry
# client failing to flush or a sync barrier timing out). C3: the measurement for this
# (seed, arm) is already complete once SH_METRIC is printed, and an exit code arriving
# after it must not override a scientific result that was already captured.
FAILS_AFTER_STARTING = """import argparse
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
print("SH_DEVICE cpu", flush=True)
print(f"SH_METRIC arm=reproduction seed={a.seed} value={59.28 + a.seed * 0.01:.4f}", flush=True)
raise RuntimeError("loss became NaN at step 4000")
"""

# The other half of C3: a genuine failure that strikes BEFORE the experiment ever
# produces ITS measurement. The fix above must not become a blanket amnesty for a
# non-zero exit code — this must still convict.
CRASHES_BEFORE_MEASURING = """import argparse
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
print("SH_DEVICE cpu", flush=True)
raise RuntimeError("loss became NaN at step 4000")
"""

# C2: an infrastructure failure — here, a CUDA OOM — must never become FAILED_REPRODUCTION,
# however it reads at the stderr and however far the process got.
FAILS_WITH_CUDA_OOM = """import argparse, sys
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
print("SH_DEVICE cuda", flush=True)
sys.stderr.write("RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB\\n")
sys.exit(1)
"""

FAILS_AT_IMPORT = """\
import a_module_that_does_not_exist
print("never reached")
"""


def _cfg(tmp_path: Path, **over) -> Config:
    cfg = Config(projects_dir=tmp_path / "projects")
    cfg.probe_timeout_s = 60
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _established(cls):
    return cls(state="established", established=True, reason="fixture")


def _spec(repo: Path, commit: str, cfg: Config, *, seeds=(0, 1, 2)) -> ProbeSpec:
    """A fully qualified repo_exec spec. Every precondition genuinely satisfied."""
    return ProbeSpec(
        paper_id="fixture", provenance="repo_exec",
        command=[cfg.python, "run.py", "--seed", "{seed}"],
        cwd=str(repo), interpreter=cfg.python, commit=commit,
        arms=["reproduction"], seeds=list(seeds),
        table_ref="T1:r0:c1", claimed_cell_value=CELL,
        experiment=_established(ExperimentIdentity),
        metric_identity=_established(MetricIdentity),
        configuration=_established(ConfigurationIdentity),
        capability=ExecCapability(established=True, reason_code="established",
                                  interpreter_is_repo_env=True),
        resources=ResourceCapability(state="satisfied", reason="fixture fits"),
    )


# --------------------------------------------------------------------------- #
# The backend runs a process and reports what it did
# --------------------------------------------------------------------------- #
def test_the_backend_echoes_back_what_it_actually_ran(tmp_path):
    """A record of what the caller INTENDED to run is not evidence of what ran."""
    out = LocalBackend().execute(ExecRequest(
        [__import__("sys").executable, "-c", "print('hello')"], str(tmp_path), 60))
    assert out.ok and "hello" in out.stdout
    assert out.argv[-1] == "print('hello')" and out.cwd == str(tmp_path)
    assert out.started_at.endswith("Z") and out.ended_at.endswith("Z")
    assert out.backend == "local" and out.seconds >= 0


def test_a_timeout_keeps_the_output_the_process_had_already_produced(tmp_path):
    """The only evidence that can say whether the experiment started is what it printed
    before it was killed, and `reached_experiment` reads exactly that."""
    import sys
    out = LocalBackend().execute(ExecRequest(
        [sys.executable, "-u", "-c",
         "print('SH_DEVICE cpu', flush=True)\nimport time; time.sleep(30)"], str(tmp_path), 2))
    assert out.launched and not out.completed and out.timed_out
    assert "SH_DEVICE cpu" in out.stdout, "partial output is evidence, not noise"


# --------------------------------------------------------------------------- #
# The full chain: authorize → execute → reconcile
# --------------------------------------------------------------------------- #
def test_a_qualified_execution_that_matches_resolves_verified(tmp_path):
    """The verdict this system had never produced.

    Synthetic fixture: this proves the execution path, not any paper.
    """
    repo, commit = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    spec = _spec(repo, commit, cfg)

    result = run_probe(cfg, tmp_path / "projects" / "fixture", spec)

    assert result.authorization is not None and result.authorization.decision == "authorized"
    assert result.commit_verification.state == "verified"
    assert result.seeds_run == [0, 1, 2] and not result.seeds_failed
    rec = result.reconciliation
    assert rec is not None and rec.status == "RESOLVED_VERIFIED", rec.reason
    assert rec.reproduced_value is not None and abs(rec.reproduced_value - 59.29) < 0.05
    assert rec.failure_class in ("", "none")


def test_a_qualified_execution_that_mismatches_fails_reproduction(tmp_path):
    repo, commit = _repo(tmp_path, EMITS_MISMATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture", _spec(repo, commit, cfg))

    rec = result.reconciliation
    assert rec.status == "FAILED_REPRODUCTION", rec.reason
    assert rec.delta_error is not None and rec.delta_error > rec.noise_band
    verdict, why = overall_verdict([], rec)
    assert verdict == "RED", "a real failed reproduction is the one thing that drives RED alone"


def test_a_metric_captured_before_a_nonzero_exit_still_resolves(tmp_path):
    """C3 — the concrete failure the red-team found: a process prints the correct result
    for every requested seed and THEN exits non-zero for a reason unrelated to the
    measurement itself. Exit-code semantics must not override a scientific result that
    was already captured — this used to reconcile as FAILED_REPRODUCTION solely because
    `run_probe` counted processes that exited zero rather than measurements emitted.
    """
    repo, commit = _repo(tmp_path, FAILS_AFTER_STARTING)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture", _spec(repo, commit, cfg))

    assert result.seeds_run == [0, 1, 2], "every seed's metric was captured before it died"
    assert result.seeds_failed == [], "a post-completion exit is not a failed seed"
    rec = result.reconciliation
    assert rec.status == "RESOLVED_VERIFIED", rec.reason
    assert rec.failure_class in ("", "none")


def test_a_crash_before_any_measurement_is_still_a_failed_reproduction(tmp_path):
    """The other half of C3: the fix above must not become a blanket amnesty for a
    non-zero exit code. A genuine failure that strikes BEFORE the experiment produces
    its measurement still convicts."""
    repo, commit = _repo(tmp_path, CRASHES_BEFORE_MEASURING)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture", _spec(repo, commit, cfg))

    assert result.seeds_failed == [0, 1, 2], "no seed ever produced a measurement"
    rec = result.reconciliation
    assert rec.status == "FAILED_REPRODUCTION", rec.reason
    assert rec.failure_class == "runtime_failure"
    assert rec.reached_experiment is True, "it did start — that is why this convicts"


def test_a_cuda_oom_is_infrastructure_not_a_failed_reproduction(tmp_path):
    """C2 — an infrastructure failure must never become FAILED_REPRODUCTION, however it
    reads at the stderr and however far the process got before it happened."""
    repo, commit = _repo(tmp_path, FAILS_WITH_CUDA_OOM)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture", _spec(repo, commit, cfg))

    assert result.seeds_failed == [0, 1, 2]
    rec = result.reconciliation
    assert rec.status == "INCONCLUSIVE", rec.reason
    assert rec.failure_class == "infrastructure_failure"
    assert overall_verdict([], rec)[0] == "GREEN", "an infrastructure failure accuses nobody"


def test_a_checkout_modified_mid_run_retracts_the_whole_result(tmp_path):
    """Major #16 — one commit verification, made before the seed loop starts, does not
    describe every attempt inside it. Here the FIRST seed's own process tampers with the
    tracked script on disk, simulating the checkout changing while later seeds still run
    against it. The commit is re-verified once the loop ends; finding it no longer clean
    must retract the whole run to INCONCLUSIVE rather than reconcile the seeds gathered
    under a commit that stopped being the audited one partway through."""
    body = ("import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--seed', type=int, required=True)\n"
            "ap.add_argument('--arm', default='reproduction')\n"
            "a = ap.parse_args()\n"
            "print('SH_DEVICE cpu', flush=True)\n"
            "print(f'SH_METRIC arm={a.arm} seed={a.seed} value=59.28', flush=True)\n"
            "if a.seed == 0:\n"
            "    with open('run.py', 'a') as f:\n"
            "        f.write('\\n# tampered while the run was still in progress\\n')\n")
    repo, commit = _repo(tmp_path, body)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture", _spec(repo, commit, cfg, seeds=(0, 1)))

    assert result.seeds_run == [0, 1], "both seeds still reported a metric"
    rec = result.reconciliation
    assert rec is not None and rec.status == "INCONCLUSIVE", rec.reason
    assert rec.failure_class == "commit_mismatch"
    assert result.authorization.decision == "commit_changed_during_execution"
    assert overall_verdict([], rec)[0] == "GREEN"


def test_an_import_failure_is_infrastructure_not_the_paper(tmp_path):
    """The distinction the whole capability layer exists for. Nothing was reproduced and
    nobody is accused."""
    repo, commit = _repo(tmp_path, FAILS_AT_IMPORT)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture", _spec(repo, commit, cfg))

    rec = result.reconciliation
    assert rec.status == "INCONCLUSIVE", rec.reason
    assert rec.failure_class == "startup_failure"
    assert rec.reached_experiment is False
    assert overall_verdict([], rec)[0] == "GREEN"


# --------------------------------------------------------------------------- #
# The gate still holds when a real process is on the other side of it
# --------------------------------------------------------------------------- #
def test_the_shut_gate_stops_a_process_that_would_otherwise_have_run(tmp_path):
    repo, commit = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=False)
    result = run_probe(cfg, tmp_path / "projects" / "fixture", _spec(repo, commit, cfg))

    assert result.verdict == "blocked"
    assert result.authorization.decision == "gate_closed"
    assert result.executions == 0 and not result.execution_log
    assert result.reconciliation.status == "INCONCLUSIVE"


def test_a_commit_that_moved_between_audit_and_execution_blocks(tmp_path):
    repo, audited = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    spec = _spec(repo, audited, cfg)

    (repo / "run.py").write_text(EMITS_MISMATCH, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "upstream moved on")

    result = run_probe(cfg, tmp_path / "projects" / "fixture", spec)
    assert result.verdict == "blocked"
    assert result.commit_verification.state == "mismatch"
    assert result.executions == 0, "the moved code must never have run"


def test_an_edited_working_tree_blocks_even_at_the_right_commit(tmp_path):
    repo, commit = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    spec = _spec(repo, commit, cfg)
    (repo / "run.py").write_text(EMITS_MISMATCH, encoding="utf-8")   # not committed

    result = run_probe(cfg, tmp_path / "projects" / "fixture", spec)
    assert result.verdict == "blocked"
    assert result.commit_verification.state == "dirty"
    assert result.executions == 0


@pytest.mark.parametrize("field,value", [
    ("experiment", None),
    ("metric_identity", None),
    ("configuration", None),
    ("capability", ExecCapability(established=False, reason_code="dependency_missing")),
    ("resources", ResourceCapability(state="insufficient", reason="24 GiB vs 8 GiB")),
])
def test_every_precondition_stops_a_runnable_process(tmp_path, field, value):
    """The script would run and would match. Each missing precondition alone prevents it."""
    repo, commit = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    spec = _spec(repo, commit, cfg)
    setattr(spec, field, value)

    result = run_probe(cfg, tmp_path / "projects" / "fixture", spec)
    assert result.verdict == "blocked", field
    assert result.executions == 0, f"{field}: nothing may have run"
    assert result.reconciliation.status == "INCONCLUSIVE"


def test_the_provenance_ceiling_holds_over_a_real_execution(tmp_path):
    """The same process, the same matching numbers — but the code is ours, so it may
    neither convict nor acquit."""
    repo, commit = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    spec = _spec(repo, commit, cfg)
    spec.provenance = "synthesized"

    result = run_probe(cfg, tmp_path / "projects" / "fixture", spec)
    assert result.reconciliation.status == "INCONCLUSIVE"


# --------------------------------------------------------------------------- #
# Execution artifacts
# --------------------------------------------------------------------------- #
def test_every_attempt_is_recorded_with_enough_to_re_derive_the_metric(tmp_path):
    repo, commit = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture",
                       _spec(repo, commit, cfg, seeds=(0, 1)))

    assert result.executions == 2 and result.execution_log
    lines = Path(result.execution_log).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        r = ExecutionRecord(**json.loads(line))
        assert r.launched and r.completed and r.returncode == 0
        assert r.argv and r.cwd == str(repo) and r.commit == commit
        assert r.started_at and r.ended_at and r.seconds >= 0
        assert r.backend == "local" and r.interpreter == cfg.python
        # The parse is re-derivable by hand from the same bytes.
        assert f"seed={r.seed}" in r.stdout and str(r.metric)[:5] in r.stdout


def test_a_failed_attempt_keeps_its_stderr_whole(tmp_path):
    """`probe_log.json` kept a 400-character tail. A traceback does not fit in 400
    characters, and the tail is the part that says least about the cause."""
    repo, commit = _repo(tmp_path, FAILS_AT_IMPORT)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture",
                       _spec(repo, commit, cfg, seeds=(0,)))

    r = ExecutionRecord(**json.loads(
        Path(result.execution_log).read_text(encoding="utf-8").splitlines()[0]))
    assert r.returncode != 0 and "ModuleNotFoundError" in r.stderr
    assert "Traceback" in r.stderr, "the whole stderr, not a tail"


def test_the_report_chain_points_at_the_execution_log(tmp_path):
    from harness.stages.report import build_chain

    repo, commit = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    result = run_probe(cfg, tmp_path / "projects" / "fixture",
                       _spec(repo, commit, cfg, seeds=(0,)))

    chain = build_chain([], result)
    assert chain.executed is True and chain.executions == 1
    assert chain.execution_log == result.execution_log
    assert chain.commit == "" or chain.commit_state == "verified"


# --------------------------------------------------------------------------- #
# Commit verification is fresh, not remembered
# --------------------------------------------------------------------------- #
def test_the_commit_check_reads_the_disk_at_execution_time(tmp_path):
    repo, audited = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    spec = _spec(repo, audited, cfg)
    assert verify_execution_commit(spec).state == "verified"

    (repo / "run.py").write_text(EMITS_MISMATCH, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "moved")
    assert verify_execution_commit(spec).state == "mismatch"
    assert verify_commit(repo, audited).state == "mismatch"


def test_authorization_still_fails_closed_without_a_commit_check(tmp_path):
    repo, commit = _repo(tmp_path, EMITS_MATCH)
    cfg = _cfg(tmp_path, allow_repo_exec=True)
    auth = authorize(cfg, _spec(repo, commit, cfg), local_backend())     # commit omitted
    assert not auth.allowed and auth.decision == "commit_unverified"
    ok = authorize(cfg, _spec(repo, commit, cfg), local_backend(),
                   commit=CommitVerification(state="verified", expected=commit, actual=commit))
    assert ok.allowed
