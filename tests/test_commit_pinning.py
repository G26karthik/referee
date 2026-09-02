"""E2 — the code that runs must be the code that was audited.

`acquire` used to do `git clone --depth 1` of the default branch and then record
`rev-parse HEAD` into `acq.commit`. That records what arrived; it does not pin anything.
The gap it leaves is a moving branch:

    run 1   clone HEAD = abc1  ->  static audit reads abc1
                                   experiment identity resolved against abc1
                                   findings cite abc1's files and line numbers
    (someone pushes)
    run 2   cache refreshed, HEAD = def2  ->  execution runs def2
                                              reconciliation labelled with abc1's audit

Nothing errors. The report reads as though one commit had been audited and reproduced,
and the reconciliation — which is the thing allowed to say FAILED_REPRODUCTION and drive
RED — is about code the audit never opened.

So the SHA is recorded in full, acquisition is asked for a specific revision rather than
handed whatever the branch points at, and verification happens against the disk at the
moment of execution rather than against a value stored during planning. All four of those
are separately load-bearing and separately tested below.

These fixtures use local `git init` only. No network, no clone, no remote.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.artifacts import (CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExperimentIdentity, MetricIdentity, ProbeSpec, RepoAcquisition,
                               ResourceCapability)
from harness.backends import authorize, local_backend
from harness.config import Config
from harness.local_exec import reconcile, run_probe, verify_execution_commit
from harness.repo import dirty_files, head_commit, is_shallow, verify_commit
from harness.stages.probe import plan_execution
from harness.stages.report import overall_verdict


# --------------------------------------------------------------------------- #
# Local git fixtures
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> str:
    env_args = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    p = subprocess.run(["git", *env_args, *args], cwd=str(repo), capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"
    return (p.stdout or "").strip()


def _repo(tmp_path: Path, name: str = "r") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    (repo / "eval.py").write_text("print('v1')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "first")
    return repo


def _advance(repo: Path) -> str:
    """Push the branch forward, the way an upstream default branch moves."""
    (repo / "eval.py").write_text("print('v2')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "second")
    return _git(repo, "rev-parse", "HEAD")


def _cfg(**over) -> Config:
    cfg = Config.load()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _est(cls):
    return cls(state="established", established=True, reason="fixture")


def _qualified(commit: str = "", cwd: str = "") -> ProbeSpec:
    return ProbeSpec(paper_id="p", command=["python", "eval.py"], provenance="repo_exec",
                     cwd=cwd, commit=commit, table_ref="T1:r0:c1", claimed_cell_value="59.28",
                     experiment=_est(ExperimentIdentity), metric_identity=_est(MetricIdentity),
                     configuration=_est(ConfigurationIdentity),
                     capability=ExecCapability(established=True, reason_code="established"),
                     resources=ResourceCapability(state="satisfied", reason="fixture"))


# --------------------------------------------------------------------------- #
# Reading the checkout
# --------------------------------------------------------------------------- #
def test_the_recorded_sha_is_full_length_not_abbreviated(tmp_path):
    """A 12-character prefix cannot be compared for equality with a real HEAD without
    loosening the comparison, and loosening it is the one thing this check may not do."""
    sha = head_commit(_repo(tmp_path))
    assert len(sha) == 40 and sha == sha.lower()


def test_a_directory_that_is_not_a_checkout_has_no_commit(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert head_commit(plain) == ""


def test_a_local_repository_is_not_shallow(tmp_path):
    assert is_shallow(_repo(tmp_path)) is False


def test_modified_tracked_files_are_reported(tmp_path):
    repo = _repo(tmp_path)
    assert dirty_files(repo) == []
    (repo / "eval.py").write_text("print('tampered')\n", encoding="utf-8")
    assert dirty_files(repo) == ["eval.py"]


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def test_a_matching_commit_verifies(tmp_path):
    repo = _repo(tmp_path)
    ver = verify_commit(repo, head_commit(repo))
    assert ver.state == "verified" and ver.established
    assert ver.actual == ver.expected


def test_a_moving_default_branch_cannot_silently_change_the_audited_code(tmp_path):
    """The core case. The audit read abc1; the branch has since moved to def2; the spec
    still carries abc1. Executing def2 under abc1's findings must be refused."""
    repo = _repo(tmp_path)
    audited = head_commit(repo)
    moved = _advance(repo)
    assert audited != moved

    ver = verify_commit(repo, audited)
    assert ver.state == "mismatch" and not ver.established
    assert audited[:12] in ver.reason and moved[:12] in ver.reason


def test_a_clean_sha_over_a_modified_tree_is_not_the_audited_code(tmp_path):
    """HEAD matching is necessary and not sufficient: the SHA stops describing the files
    the moment one of them is edited."""
    repo = _repo(tmp_path)
    sha = head_commit(repo)
    (repo / "eval.py").write_text("print('tampered')\n", encoding="utf-8")
    ver = verify_commit(repo, sha)
    assert ver.state == "dirty" and not ver.established
    assert ver.dirty_files == ["eval.py"]


def test_no_recorded_commit_is_unknown_and_blocks(tmp_path):
    """"We never wrote down which commit we audited" is not evidence that this is it."""
    ver = verify_commit(_repo(tmp_path), "")
    assert ver.state == "unknown" and not ver.established


def test_a_missing_checkout_is_unknown_and_blocks(tmp_path):
    ver = verify_commit(tmp_path / "nope", "a" * 40)
    assert ver.state == "unknown" and not ver.established


def test_an_abbreviated_but_unambiguous_prefix_still_verifies(tmp_path):
    """Artifacts written before full SHAs were stored hold 12 characters. Git's own
    abbreviation semantics apply — this is not a loosened comparison, it is the same one."""
    repo = _repo(tmp_path)
    sha = head_commit(repo)
    assert verify_commit(repo, sha[:12]).state == "verified"


def test_a_prefix_too_short_to_identify_a_commit_is_refused(tmp_path):
    """Seven characters collide across a large repository's history. The check is not
    solved by accepting less evidence."""
    repo = _repo(tmp_path)
    sha = head_commit(repo)
    ver = verify_commit(repo, sha[:7])
    assert ver.state == "mismatch" and not ver.established


def test_a_prefix_that_does_not_match_is_still_a_mismatch(tmp_path):
    repo = _repo(tmp_path)
    assert verify_commit(repo, "0" * 40).state == "mismatch"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_omitting_the_verification_entirely_fails_closed():
    """`authorize` defaults `commit` to None, and None refuses. A caller that forgets the
    check must not inherit permission from having forgotten it."""
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), local_backend())
    assert not auth.allowed and auth.decision == "commit_unverified"
    assert auth.failure_class == "commit_mismatch"


@pytest.mark.parametrize("state", ["mismatch", "dirty", "unknown", "unassessed"])
def test_only_a_verified_commit_authorizes(state):
    ver = CommitVerification(state=state, expected="a" * 40, actual="b" * 40, reason=state)
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), local_backend(), commit=ver)
    assert not auth.allowed and auth.decision == "commit_unverified", state


def test_a_verified_commit_lets_authorization_through():
    ver = CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), local_backend(), commit=ver)
    assert auth.allowed and auth.decision == "authorized"
    assert "a" * 12 in auth.detail, "the authorized commit is named in the record"


def test_the_commit_check_precedes_the_identity_check():
    """Both refuse, and the order is deliberate: reasoning about which experiment some
    other commit implements is reasoning about the wrong artifact."""
    spec = _qualified()
    spec.experiment = None
    ver = CommitVerification(state="mismatch", expected="a" * 40, actual="b" * 40)
    auth = authorize(_cfg(allow_repo_exec=True), spec, local_backend(), commit=ver)
    assert auth.decision == "commit_unverified", auth.decision


# --------------------------------------------------------------------------- #
# Verification happens at execution, not at planning
# --------------------------------------------------------------------------- #
def test_the_check_reads_the_disk_rather_than_a_stored_value(tmp_path):
    """A checkout replaced between planning and execution must be caught. Comparing two
    recorded strings would not catch it; reading HEAD now does."""
    repo = _repo(tmp_path)
    audited = head_commit(repo)
    spec = _qualified(commit=audited, cwd=str(repo))
    assert verify_execution_commit(spec).state == "verified"

    _advance(repo)                                  # the checkout moves under the spec
    assert verify_execution_commit(spec).state == "mismatch"


def test_a_harness_authored_probe_has_no_commit_to_be_wrong_about():
    assert verify_execution_commit(ProbeSpec(paper_id="p")) is None


def test_a_moved_checkout_blocks_the_run_and_writes_a_blocked_result(tmp_path):
    repo = _repo(tmp_path)
    audited = head_commit(repo)
    _advance(repo)
    spec = _qualified(commit=audited, cwd=str(repo))
    spec.command = ["python", "-c", "print('SH_METRIC arm=reproduction seed=0 value=59.28')"]

    result = run_probe(_cfg(allow_repo_exec=True), tmp_path / "out", spec)
    assert result.verdict == "blocked"
    assert result.authorization is not None
    assert result.authorization.decision == "commit_unverified"
    assert result.commit_verification is not None
    assert result.commit_verification.state == "mismatch"
    assert result.seeds_run == [] and not result.arms, "nothing may have executed"


def test_the_audited_commit_is_carried_through_the_execution_artifact(tmp_path):
    repo = _repo(tmp_path)
    audited = head_commit(repo)
    spec = _qualified(commit=audited, cwd=str(repo))
    spec.command = ["python", "-c", "raise SystemExit(0)"]
    result = run_probe(_cfg(allow_repo_exec=False), tmp_path / "out", spec)
    assert result.commit_verification is not None
    assert result.commit_verification.expected == audited
    assert result.commit_verification.actual == audited


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def test_planning_records_the_audited_commit_on_the_spec(tmp_path, monkeypatch):
    from harness.artifacts import CandidateCommand, CodeAudit
    from harness.stages import probe as probe_stage

    repo = _repo(tmp_path)
    sha = head_commit(repo)
    monkeypatch.setattr(probe_stage.repo_mod, "assess_capability",
                        lambda *a, **k: ExecCapability(established=True, reason_code="established"))
    monkeypatch.setattr(probe_stage.experiment_id, "resolve", lambda *a, **k: (
        ExperimentIdentity(state="established",
                           command=CandidateCommand(argv=["python", "eval.py"],
                                                    source_ref="README.md:1")),
        MetricIdentity(state="established"), ConfigurationIdentity(state="established")))
    monkeypatch.setattr(probe_stage.resources_mod, "assess_resources",
                        lambda *a, **k: ResourceCapability(state="satisfied", reason="stub"))

    acq = RepoAcquisition(url="u", status="cloned", path=str(repo), entrypoint="eval.py",
                          env_status="ready", env_path=str(repo / "env"))
    spec = plan_execution(_cfg(allow_repo_exec=True), ProbeSpec(paper_id="p"), acq, None,
                          CodeAudit(repo_path=str(repo), commit=sha))
    assert spec.commit == sha, "the audited SHA is what execution will be verified against"


def test_planning_refuses_to_promote_when_the_checkout_has_moved(tmp_path, monkeypatch):
    from harness.artifacts import CandidateCommand, CodeAudit
    from harness.stages import probe as probe_stage

    repo = _repo(tmp_path)
    audited = head_commit(repo)
    _advance(repo)

    monkeypatch.setattr(probe_stage.repo_mod, "assess_capability",
                        lambda *a, **k: ExecCapability(established=True, reason_code="established"))
    monkeypatch.setattr(probe_stage.experiment_id, "resolve", lambda *a, **k: (
        ExperimentIdentity(state="established",
                           command=CandidateCommand(argv=["python", "eval.py"])),
        MetricIdentity(state="established"), ConfigurationIdentity(state="established")))
    monkeypatch.setattr(probe_stage.resources_mod, "assess_resources",
                        lambda *a, **k: ResourceCapability(state="satisfied", reason="stub"))

    acq = RepoAcquisition(url="u", status="cloned", path=str(repo), entrypoint="eval.py",
                          env_status="ready", env_path=str(repo / "env"))
    spec = plan_execution(_cfg(allow_repo_exec=True), ProbeSpec(paper_id="p"), acq, None,
                          CodeAudit(repo_path=str(repo), commit=audited))
    assert spec.provenance != "repo_exec" and not spec.command


# --------------------------------------------------------------------------- #
# The invariant
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("values", [[59.30, 59.26], [999.0, 999.0]])
def test_a_commit_refusal_produces_neither_verdict(values):
    ver = CommitVerification(state="mismatch", expected="a" * 40, actual="b" * 40,
                             reason="HEAD moved")
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), local_backend(), commit=ver)
    spec = ProbeSpec(paper_id="p", provenance="repo_exec", table_ref="T1:r0:c1",
                     claimed_cell_value="59.28")
    rec = reconcile(spec, values, 0.10, [0, 1], authorization=auth)
    assert rec.status == "INCONCLUSIVE"
    assert rec.failure_class == "commit_mismatch"


def test_a_commit_mismatch_cannot_drive_the_paper_red():
    ver = CommitVerification(state="mismatch", expected="a" * 40, actual="b" * 40)
    auth = authorize(_cfg(allow_repo_exec=True), _qualified(), local_backend(), commit=ver)
    spec = ProbeSpec(paper_id="p", provenance="repo_exec", table_ref="T1:r0:c1",
                     claimed_cell_value="59.28")
    rec = reconcile(spec, [999.0, 999.0], 0.10, [0, 1], authorization=auth)
    verdict, _ = overall_verdict([], rec)
    assert verdict == "GREEN", "running the wrong commit convicts nobody"
