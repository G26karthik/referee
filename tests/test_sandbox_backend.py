"""The remote sandbox backend: what it must guarantee that the local one already did.

A second runnable backend is the first real test of the claim `ExecutionBackend` makes in
its docstring — that a new provider changes *where* a process runs and changes *nothing*
about what any verdict means. So these tests are mostly not about leasing machines. They
are about the four places where a remote runner could quietly weaken a guarantee, and each
one is asserted rather than assumed:

  1. **No silent local fallback.** Every failure to obtain or stage a machine must refuse.
     A backend that ran a Linux repository on the operator's Windows laptop because the
     provider was unreachable would produce a startup crash that reads, at the stderr,
     exactly like the authors' code being broken — the single most damaging output here.

  2. **E2 is verified against the tree that RUNS.** Certifying a directory on this disk
     and then executing somewhere else leaves the executed tree unverified while the
     record says `verified`. The same `verify_commit` must do it, through the backend's
     own reader, with the same four outcomes and the same fail-closed branches.

  3. **Capability is asked of the interpreter that will run.** The static checks read the
     same bytes wherever the checkout sits; resolving an import means asking an
     interpreter, and the default resolver against a Linux venv path from a Windows host
     reports every dependency missing — refusing a capable session for a reason that is
     about us and is also wrong.

  4. **A reservation is not a measurement.** `resources()` is what we ask for. What
     arrived is measured inside the machine, refused if it is smaller, and stamped onto
     every execution record from the measurement rather than from the request.

Nothing here contacts a provider. `_FakeSandbox` answers the six calls `harness/sandbox.py`
makes, which is enough to drive staging, inventory, verification and execution end to end.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import repo as repo_mod
from harness import sandbox as sandbox_mod
from harness.artifacts import (GIB, CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExperimentIdentity, MetricIdentity, ProbeSpec, RepoAcquisition,
                               ResourceCapability, ResourceEvidence, ResourceRequirement)
from harness.backends import (BackendAvailability, ExecRequest, SandboxBackend, authorize,
                              registered_backends, select_for)
from harness.config import Config
from harness.sandbox import (ENV_PYTHON, REPO_DIR, AbsentGitTree, SandboxGitTree,
                             SandboxSession, SandboxSetupError, SandboxSpec, spec_from_config)

SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def _cfg(**over) -> Config:
    cfg = Config.load()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _open_cfg(**over) -> Config:
    """A config with every gate a remote session needs already open."""
    return _cfg(allow_sandbox=True, allow_network=True, allow_install=True, **over)


def _acq(path: str, **over) -> RepoAcquisition:
    acq = RepoAcquisition(url="https://github.com/authors/paper.git", status="cloned",
                          path=path, commit=SHA, entrypoint="eval.py",
                          dependency_files=["requirements.txt"])
    for k, v in over.items():
        setattr(acq, k, v)
    return acq


# --------------------------------------------------------------------------- #
# A provider that answers, without a provider
# --------------------------------------------------------------------------- #
class _Proc:
    def __init__(self, out: str = "", err: str = "", rc: int | None = 0) -> None:
        self.stdout, self.stderr, self.returncode = _Stream(out), _Stream(err), rc

    def wait(self) -> None:
        return None


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


DEFAULT_INVENTORY = {"platform": "linux", "python_version": "3.12.7", "cpu_count": 8,
                     "ram_bytes": 32 * GIB, "disk_bytes": 200 * GIB}
KEEP = object()          # "leave this at the healthy default" — distinct from None


class _FakeSandbox:
    """The five calls `harness/sandbox.py` makes on a provider handle, and a script.

    Defaults to a HEALTHY staged machine: the audited commit at HEAD, a clean tree, no
    locked index paths, a real `--show-toplevel`, and an inventory that clears the
    reservation. A test then overrides only the answer it is about, which is what keeps
    each one readable as a single claim.

    `script` maps a substring of the joined argv to a `_Proc`; first match wins, and it is
    consulted before the defaults so an override always takes effect. `inventory=None`
    means the machine answers nothing at all — the uninterrogable case, which is
    deliberately not the same as `KEEP`.
    """

    def __init__(self, script: dict[str, _Proc] | None = None,
                 inventory=KEEP, object_id: str = "sb-test-1", head: str = SHA) -> None:
        self.object_id = object_id
        self.script = dict(script or {})
        self.calls: list[list[str]] = []
        self.terminated = False
        self.detached = False
        inv = DEFAULT_INVENTORY if inventory is KEEP else inventory
        self.defaults: dict[str, _Proc] = {
            "rev-parse --show-toplevel": _Proc(f"{REPO_DIR}\n"),
            "rev-parse --is-shallow-repository": _Proc("true\n"),
            "rev-parse HEAD": _Proc(f"{head}\n"),
            "status --porcelain": _Proc(""),
            "ls-files -v": _Proc("H eval.py\n"),
            "test -f": _Proc("", "", 1),          # `.git` is a directory, not a file
        }
        if inv is not None:
            self.defaults["SH_INVENTORY"] = _Proc(f"SH_INVENTORY {json.dumps(inv)}\n")

    def exec(self, *args, **kwargs):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        joined = " ".join(argv)
        for table in (self.script, self.defaults):
            for needle, proc in table.items():
                if needle in joined:
                    return proc
        return _Proc()

    def terminate(self, wait: bool = False) -> None:
        self.terminated = True

    def detach(self) -> None:
        self.detached = True


@pytest.fixture(autouse=True)
def _no_leaked_sessions():
    """No test may leave a session in the module caches for the next one to find."""
    yield
    sandbox_mod._SESSIONS.clear()
    sandbox_mod._HANDLES.clear()


@pytest.fixture
def credentialed(monkeypatch):
    """Credentials present, so `driver_status` clears and the tested path is the real one.

    Set as environment variables rather than by stubbing `credentials_present`, so the
    check under test is the one that ships. The values are not tokens and reach no
    provider: `_create` is always replaced before a session is opened.
    """
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-not-a-real-token-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "test-not-a-real-token-secret")
    if not sandbox_mod.driver_status(_open_cfg())[0]:
        pytest.skip("the 'modal' client is not installed in this interpreter")


@pytest.fixture
def staged(tmp_path, monkeypatch, credentialed):
    """A checkout on disk with a fake sandbox staged for it. Returns (session, fake)."""
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    fake = _FakeSandbox()
    monkeypatch.setattr(sandbox_mod, "_create", lambda spec: fake)
    session = sandbox_mod.open_session(
        _open_cfg(), local_path=str(checkout), url="https://github.com/a/b.git",
        commit=SHA, requirement_files=["requirements.txt"], paper_id="p")
    return session, fake


# --------------------------------------------------------------------------- #
# The registry, and what makes this different from a declaration
# --------------------------------------------------------------------------- #
def test_the_sandbox_is_a_registered_runner_not_a_declaration():
    """Kaggle says "somewhere like this could host it". This one says "and I can drive it".

    That single boolean is what moves the refusal from `credentials_unavailable`, which an
    operator cannot act on, to `backend_offline`, which they can.
    """
    assert "modal" in registered_backends()
    p = SandboxBackend().profile()
    assert p.can_execute is True, "a declaration would be False here"
    assert p.requires_credentials is True
    assert p.platform == "linux" and p.max_walltime_s


def test_the_backend_answers_the_interface_with_no_client_and_no_credentials(monkeypatch):
    """`select_for` instantiates every registered backend on every paper.

    So none of `resources`, `profile`, `environment` or `available` may contact a provider,
    import an optional dependency at module level, or raise when unconfigured.
    """
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    b = SandboxBackend()
    r = b.resources()
    assert r.name == "modal" and r.platform == "linux"
    assert b.environment() and isinstance(b.available(), BackendAvailability)
    assert b.profile().name == "modal"


def test_a_reservation_is_reported_as_a_reservation_not_a_measurement():
    """`resources()` runs before any machine exists, so it can only be the request.

    Reporting it as though it were measured hardware is the shape of overclaim this whole
    harness is built against — so the detail line says which it is, and `execute` stamps
    the measured figure instead.
    """
    r = SandboxBackend().resources()
    assert "reservation" in r.detail
    assert SandboxSpec(gpu="").vram_bytes is None, "no card asked for states no VRAM"
    assert SandboxSpec(gpu="A100-80GB").vram_bytes == 80 * GIB


@pytest.mark.parametrize("gate,needle", [
    ("allow_sandbox", "SH_ALLOW_SANDBOX"),
])
def test_a_closed_gate_makes_the_backend_unavailable_with_a_reason(gate, needle):
    cfg = _open_cfg()
    setattr(cfg, gate, False)
    ok, why = sandbox_mod.driver_status(cfg)
    assert not ok and needle in why


def test_the_gate_refusal_is_offline_and_never_a_declaration():
    """`authorize` consults `can_execute` first, then availability. The order matters.

    A closed gate must not be reported as `backend_cannot_execute` — that is the sentence
    reserved for a published specification nobody can drive, and it would tell an operator
    to go and find a different machine when the answer is one environment variable.
    """
    spec = ProbeSpec(paper_id="p", command=["python", "eval.py"], provenance="repo_exec")
    auth = authorize(_cfg(allow_repo_exec=True, allow_sandbox=False), spec, SandboxBackend(),
                     commit=CommitVerification(state="verified", expected=SHA, actual=SHA))
    assert not auth.allowed
    assert auth.decision == "backend_offline", auth.decision
    assert auth.failure_class == "backend_unavailable"


# --------------------------------------------------------------------------- #
# 1. No silent local fallback
# --------------------------------------------------------------------------- #
def test_executing_with_no_session_refuses_and_says_it_did_not_run_locally(tmp_path):
    """The failure this backend exists to not have.

    `launched=False` is the record's own account of "no process existed", which
    `reached_experiment` reads as a setup failure and the reconciler turns into
    INCONCLUSIVE. What must never happen is a process starting HERE.
    """
    out = SandboxBackend().execute(ExecRequest(["python", "eval.py"], str(tmp_path), 60))
    assert out.launched is False and out.completed is False
    assert out.returncode is None and not out.stdout
    assert "NOT run on the local machine" in out.error
    assert out.backend == "modal"


def test_a_host_path_is_refused_rather_than_rewritten(staged):
    """An argument naming a file on this disk names nothing in the sandbox.

    Passing it through produces a crash whose stderr is indistinguishable from the
    repository's own code failing, and `reached_experiment` cannot undo that after the
    fact. So the command is refused instead, and nothing is invented to make it run.
    """
    session, _ = staged
    argv, why = sandbox_mod.translate_argv(session, [r"C:\Python313\python.exe", "eval.py"])
    assert argv == [] and "names nothing in the sandbox" in why
    run = sandbox_mod.run_in(session, [r"C:\Py\python.exe", "eval.py"],
                             session.local_path, 60)
    assert run.launched is False and "names nothing" in run.error


def test_the_checkout_and_its_subdirectories_map_but_nothing_else(staged, tmp_path):
    session, _ = staged
    (Path(session.local_path) / "sub").mkdir()
    assert sandbox_mod.translate_cwd(session, session.local_path) == REPO_DIR
    assert sandbox_mod.translate_cwd(session, str(Path(session.local_path) / "sub")) \
        == f"{REPO_DIR}/sub"
    assert sandbox_mod.translate_cwd(session, str(tmp_path / "elsewhere")) is None


@pytest.mark.parametrize("kwargs,needle", [
    (dict(url="", commit=SHA), "no repository URL"),
    (dict(url="https://x/y.git", commit=""), "no full audited commit"),
    (dict(url="https://x/y.git", commit="abc123"), "no full audited commit"),
])
def test_the_cheap_refusals_happen_before_a_machine_is_leased(kwargs, needle, monkeypatch,
                                                              credentialed):
    """Ordered so nothing is billed for a request that could never have worked.

    In particular the commit is required BEFORE the lease: staging "whatever the default
    branch points at" is not something this module can do, so a caller with no audited
    SHA is refused for free.
    """
    leased = []
    monkeypatch.setattr(sandbox_mod, "_create",
                        lambda spec: leased.append(spec) or _FakeSandbox())
    with pytest.raises(SandboxSetupError) as e:
        sandbox_mod.open_session(_open_cfg(), local_path="nowhere", paper_id="p", **kwargs)
    assert needle in str(e.value)
    assert leased == [], "a refusal that leases a machine has already cost money"


def test_a_shut_install_or_network_gate_blocks_before_leasing(tmp_path, monkeypatch):
    leased = []
    monkeypatch.setattr(sandbox_mod, "_create",
                        lambda spec: leased.append(spec) or _FakeSandbox())
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    for gate in ("allow_install", "allow_network"):
        cfg = _open_cfg()
        setattr(cfg, gate, False)
        acq = SandboxBackend().provision(cfg, tmp_path, "p", _acq(str(checkout)))
        assert acq.env_status == "blocked", gate
        assert "SH_ALLOW_" in acq.reason
    assert leased == []


def test_provisioning_does_nothing_without_a_checkout(tmp_path):
    acq = SandboxBackend().provision(_open_cfg(), tmp_path, "p",
                                     RepoAcquisition(status="unavailable"))
    assert acq.env_status == "not_attempted"


def test_a_refused_reservation_is_blocked_and_a_broken_stage_is_failed(tmp_path, monkeypatch,
                                                                        credentialed):
    """`blocked` and `failed` are different facts about US, and only one is actionable.

    `blocked` means we lack permission or equipment; `failed` means we had a machine and
    the staging did not work. Neither is a fact about the paper, and both keep the target
    out of `repo_exec` — but an operator reading `blocked` has something to change.
    """
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)

    monkeypatch.setattr(sandbox_mod, "_create",
                        lambda spec: (_ for _ in ()).throw(RuntimeError("quota exceeded")))
    acq = SandboxBackend().provision(_open_cfg(), tmp_path, "p", _acq(str(checkout)))
    assert acq.env_status == "failed" and "quota exceeded" in acq.reason

    fake = _FakeSandbox(script={"git fetch": _Proc("", "not our ref", 128)})
    monkeypatch.setattr(sandbox_mod, "_create", lambda spec: fake)
    acq = SandboxBackend().provision(_open_cfg(), tmp_path, "p", _acq(str(checkout)))
    assert acq.env_status == "failed"
    assert "could not be staged" in acq.reason and "not our ref" in acq.reason
    assert fake.terminated, "a lease that cannot be used must not be left running"


def test_a_machine_smaller_than_the_reservation_is_refused_not_accommodated(
        tmp_path, monkeypatch, credentialed):
    """Nothing here shrinks the experiment to fit the machine that turned up.

    A run on less hardware than the resource preflight cleared produces a number from an
    environment nobody assessed, and the OOM it produces reads like broken code.
    """
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    small = dict(DEFAULT_INVENTORY, gpu_count=1, vram_bytes=16 * GIB, gpu_name="Tesla T4")
    fake = _FakeSandbox(inventory=small)          # a T4 where an A100 was reserved
    monkeypatch.setattr(sandbox_mod, "_create", lambda spec: fake)
    acq = SandboxBackend().provision(_open_cfg(sandbox_gpu="A100-80GB"), tmp_path, "p",
                                     _acq(str(checkout)))
    assert acq.env_status == "failed"
    assert "smaller than the reservation" in acq.reason and "VRAM" in acq.reason
    assert fake.terminated


def test_a_machine_that_cannot_be_interrogated_is_a_different_refusal(tmp_path, monkeypatch,
                                                                      credentialed):
    """An empty inventory is an absence of evidence about the machine, not a small machine.

    This was a real defect: the shortfall comparison read a missing `gpu_count` as "no
    accelerator is visible", which is a claim about the hardware drawn from having learned
    nothing about the hardware — the same error as reading a paper's silence about its
    memory cost as a small cost. It still blocks, and now it blocks saying what is wrong.
    """
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    fake = _FakeSandbox(inventory=None)         # no SH_INVENTORY line at all
    monkeypatch.setattr(sandbox_mod, "_create", lambda spec: fake)
    acq = SandboxBackend().provision(_open_cfg(), tmp_path, "p", _acq(str(checkout)))
    assert acq.env_status == "failed"
    assert "could not be interrogated" in acq.reason
    assert "smaller than the reservation" not in acq.reason
    assert fake.terminated


def test_an_unmeasured_quantity_is_not_a_shortfall_but_a_measured_one_is():
    big = SandboxSpec(gpu="A100-80GB", memory_mib=16384)
    assert sandbox_mod._shortfall(big, {}) == []
    assert sandbox_mod._shortfall(big, {"gpu_count": 1, "vram_bytes": 80 * GIB}) == []
    assert sandbox_mod._shortfall(big, {"gpu_count": 0})
    assert sandbox_mod._shortfall(big, {"gpu_count": 1, "vram_bytes": 24 * GIB})
    assert sandbox_mod._shortfall(SandboxSpec(), {"gpu_count": 0}) == [], "none asked for"


# --------------------------------------------------------------------------- #
# 2. E2 against the tree that runs — by the SAME function
# --------------------------------------------------------------------------- #
class _ScriptedTree:
    """A `repo.GitTree` whose answers are declared. Stands in for any checkout, anywhere."""

    def __init__(self, head: str = SHA, status: str = "", ls_files: str = "",
                 dot_git_is_file: bool = False, exists: bool = True,
                 status_rc: int = 0, shallow: str = "false") -> None:
        self.path = "/workspace/repo"
        self._head, self._status, self._ls = head, status, ls_files
        self._dot_git_is_file, self._exists = dot_git_is_file, exists
        self._status_rc, self._shallow = status_rc, shallow

    def git(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        if args[:2] == ["rev-parse", "HEAD"]:
            return (0, self._head, "") if self._head else (128, "", "not a repository")
        if args[:1] == ["rev-parse"] and "--is-shallow-repository" in args:
            return 0, self._shallow, ""
        if args[:1] == ["status"]:
            return (self._status_rc, self._status,
                    "" if self._status_rc == 0 else "index.lock held")
        if args[:1] == ["ls-files"]:
            return 0, self._ls, ""
        return 0, "", ""

    def is_dir(self) -> bool:
        return self._exists

    def is_file(self, relpath: str) -> bool:
        return self._dot_git_is_file and relpath == ".git"


def test_the_same_verify_commit_certifies_a_remote_tree():
    """Not a second copy of the logic — the same function, reading somewhere else.

    `verify_commit` is the fail-closed half of the execution guarantee. Reimplementing it
    for a remote backend would create a second most-safety-critical function, free to
    drift from the first. So the reads are abstracted (`repo.GitTree`) and the reasoning
    is shared, and this asserts all four outcomes arrive through the abstraction.
    """
    ok = repo_mod.verify_commit("/workspace/repo", SHA, tree=_ScriptedTree())
    assert ok.state == "verified" and ok.established and ok.actual == SHA

    mismatch = repo_mod.verify_commit("/workspace/repo", SHA, tree=_ScriptedTree(head="b" * 40))
    assert mismatch.state == "mismatch" and not mismatch.established

    dirty = repo_mod.verify_commit("/workspace/repo", SHA,
                                   tree=_ScriptedTree(status=" M eval.py\n"))
    assert dirty.state == "dirty" and dirty.dirty_files == ["eval.py"]

    absent = repo_mod.verify_commit("/workspace/repo", SHA, tree=_ScriptedTree(exists=False))
    assert absent.state == "unknown"


def test_every_fail_closed_branch_also_applies_remotely():
    """The three ways a tree is UNINSPECTABLE rather than clean, through the abstraction.

    Each of these once certified: `dirty_files` returned [] on any `git status` failure, a
    redirected `.git` was never checked, and an assume-unchanged path is invisible to
    `status` by design. A remote backend must inherit all three refusals, not just the
    obvious dirty-tree one.
    """
    locked = repo_mod.verify_commit("/workspace/repo", SHA,
                                    tree=_ScriptedTree(status_rc=1))
    assert locked.state == "unknown" and "not a clean tree" in locked.reason

    redirected = repo_mod.verify_commit("/workspace/repo", SHA,
                                        tree=_ScriptedTree(dot_git_is_file=True))
    assert redirected.state == "unknown" and "not a directory" in redirected.reason

    skipped = repo_mod.verify_commit(
        "/workspace/repo", SHA, tree=_ScriptedTree(ls_files="S eval.py\nH other.py\n"))
    assert skipped.state == "unknown" and "skip-worktree" in skipped.reason


def test_no_session_means_the_tree_cannot_be_read_and_verification_refuses(tmp_path):
    """The alternative would certify a directory here and then run somewhere else.

    `commit_tree` returning None would fall back to the LOCAL checkout — which exists, is
    clean, and is at the audited commit — producing `verified` for a tree the sandbox does
    not have. `AbsentGitTree` makes that impossible: it cannot be read, so the outcome is
    `unknown`, and `unknown` blocks.
    """
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    tree = SandboxBackend().commit_tree(str(checkout))
    assert isinstance(tree, AbsentGitTree)
    ver = repo_mod.verify_commit(str(checkout), SHA, tree=tree)
    assert ver.state == "unknown" and not ver.established
    auth = authorize(_cfg(allow_repo_exec=True, allow_sandbox=True),
                     ProbeSpec(paper_id="p", command=["python", "eval.py"],
                               provenance="repo_exec"),
                     SandboxBackend(), commit=ver)
    assert not auth.allowed and auth.failure_class in ("commit_mismatch", "backend_unavailable")


def test_a_staged_session_verifies_its_own_tree_in_place(staged):
    session, fake = staged
    ver = repo_mod.verify_commit(session.local_path, SHA, tree=SandboxGitTree(session))
    assert ver.state == "verified" and ver.actual == SHA
    ran = [" ".join(c) for c in fake.calls]
    assert any("rev-parse HEAD" in c for c in ran), "HEAD was read inside the sandbox"
    assert any("status --porcelain" in c for c in ran)


def test_provisioning_refuses_a_sandbox_whose_tree_is_the_wrong_commit(tmp_path, monkeypatch,
                                                                       credentialed):
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    fake = _FakeSandbox(head="b" * 40)
    monkeypatch.setattr(sandbox_mod, "_create", lambda spec: fake)
    acq = SandboxBackend().provision(_open_cfg(), tmp_path, "p", _acq(str(checkout)))
    assert acq.env_status == "failed"
    assert "could not be certified as the audited commit" in acq.reason


def test_staging_pins_a_commit_and_never_stands_on_a_branch():
    script = sandbox_mod._fetch_script("https://github.com/a/b.git", SHA)
    assert f"git fetch -q --depth 1 origin {SHA}" in script
    assert "checkout -q --force FETCH_HEAD" in script
    for branchy in ("origin main", "origin/main", "origin HEAD"):
        assert f"fetch -q --depth 1 {branchy}" not in script


# --------------------------------------------------------------------------- #
# 3. Capability, asked of the interpreter that will run
# --------------------------------------------------------------------------- #
def test_capability_without_a_session_is_refused_with_the_provisioning_reason(tmp_path):
    acq = _acq(str(tmp_path), env_status="blocked", reason="the gate was shut")
    cap = SandboxBackend().capability(acq, ENV_PYTHON, "python")
    assert not cap.established and cap.reason_code == "environment_incompatible"
    assert "no remote sandbox is staged" in cap.detail and "the gate was shut" in cap.detail
    assert cap.backend == "modal"


def test_the_import_check_is_delegated_to_the_machine_that_owns_the_interpreter(staged):
    """The one capability question that cannot be answered by reading files.

    Running the default resolver against `/workspace/env/bin/python` from a Windows host
    fails to start and reports every dependency missing — so a capable session would be
    refused as `dependency_missing`, a statement about the environment that is both wrong
    and expensive. The sandbox answers instead.
    """
    session, fake = staged
    repo_path = Path(session.local_path)
    (repo_path / "eval.py").write_text("import torch\nimport numpy\n", encoding="utf-8")
    (repo_path / "args.py").write_text("p.add_argument('--seed')\n", encoding="utf-8")
    acq = _acq(str(repo_path), env_status="ready", env_path=ENV_PYTHON)

    fake.script["SH_MISSING"] = _Proc('SH_MISSING []\n')
    resolver = sandbox_mod.import_resolver(session)
    assert resolver(ENV_PYTHON, repo_path, ["torch", "numpy"]) == []

    cap = repo_mod.assess_capability(acq, ENV_PYTHON, "python", platform="linux",
                                     resolve_imports=resolver)
    assert cap.established, cap.detail
    assert cap.checked_imports == ["torch", "numpy"]

    # And the default resolver, on this host, cannot answer it — which is precisely why
    # the injection exists rather than being a convenience.
    local = repo_mod.assess_capability(acq, ENV_PYTHON, "python", platform="linux")
    assert not local.established and local.reason_code == "dependency_missing"


def test_a_resolver_that_cannot_ask_reports_unresolved_never_fine(staged):
    """"We could not ask" must not read as "nothing is missing".

    The same asymmetry every preflight here encodes: an unanswerable question is not a
    favourable answer, because the cost of being wrong in that direction is an import
    crash that becomes evidence about the paper.
    """
    session, fake = staged
    fake.script["SH_MISSING"] = _Proc("", "the interpreter is gone", 127)
    resolver = sandbox_mod.import_resolver(session)
    assert resolver(ENV_PYTHON, Path(session.local_path), ["torch"]) == ["torch"]


def test_a_linux_only_repository_becomes_capable_on_the_sandbox(tmp_path):
    """The claim `ExecCapability.current_platform` was written for.

    A repository pinning `linux-64` is `environment_incompatible` on this Windows host and
    must clear the same check unchanged when the platform the run will SEE is linux. No
    line of `assess_capability` knows a sandbox exists.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "environment.yml").write_text("dependencies:\n  - libgcc-ng\n",
                                               encoding="utf-8")
    (repo_path / "eval.py").write_text("import numpy\n", encoding="utf-8")
    (repo_path / "args.py").write_text("p.add_argument('--seed')\n", encoding="utf-8")
    acq = _acq(str(repo_path), env_status="ready", env_path=ENV_PYTHON)
    assert repo_mod.declared_platform(repo_path) == "linux"

    on_windows = repo_mod.assess_capability(acq, ENV_PYTHON, "python", platform="win32",
                                            resolve_imports=lambda *_: [])
    assert not on_windows.established
    assert on_windows.reason_code == "environment_incompatible"

    on_linux = repo_mod.assess_capability(acq, ENV_PYTHON, "python", platform="linux",
                                          resolve_imports=lambda *_: [])
    assert on_linux.established, on_linux.detail


# --------------------------------------------------------------------------- #
# 4. What ran, and where — from the measurement
# --------------------------------------------------------------------------- #
def test_the_execution_record_names_the_machine_that_was_measured(staged):
    """A reproduction verdict from hardware nobody can identify afterwards is not evidence.

    Locally the hardware is implicit — there is one machine. Remotely it is not, so the
    stamp carries the sandbox's own id and the accelerator it actually reported, not the
    tier that was requested.
    """
    session, fake = staged
    session.measured = dict(session.measured, gpu_name="NVIDIA A100-SXM4-80GB")
    sandbox_mod._remember(session)
    fake.script["eval.py"] = _Proc("SH_METRIC arm=reproduction seed=0 value=0.5\n")
    out = SandboxBackend().execute(
        ExecRequest([ENV_PYTHON, "eval.py", "--seed", "0"], session.local_path, 120))
    assert out.launched and out.completed and out.returncode == 0
    assert "SH_METRIC" in out.stdout
    assert out.cwd == REPO_DIR, "the record says where the process actually ran"
    assert f"sandbox:{session.sandbox_id}" in out.environment
    assert "A100" in out.environment
    assert out.started_at and out.ended_at


def test_a_non_zero_exit_is_an_outcome_and_a_missing_code_is_a_timeout(staged):
    session, fake = staged
    fake.script["boom"] = _Proc("partial\n", "Traceback\n", 1)
    out = SandboxBackend().execute(ExecRequest([ENV_PYTHON, "boom.py"],
                                               session.local_path, 120))
    assert out.launched and out.completed and out.returncode == 1
    assert out.stdout == "partial\n" and "Traceback" in out.stderr

    fake.script["hang"] = _Proc("some output\n", "", None)
    slow = SandboxBackend().execute(ExecRequest([ENV_PYTHON, "hang.py"],
                                                session.local_path, 1))
    assert slow.launched is True and slow.completed is False
    assert slow.stdout == "some output\n", "a timeout keeps what the process printed"


def test_a_backend_may_not_edit_the_command_it_was_handed(staged):
    """Only the two rewrites this harness itself created: the interpreter and staged paths.

    A backend that rewrote the argv would be inventing an invocation while appearing to
    relay one — and the whole identity chain rests on the command coming from what the
    repository advertises.
    """
    session, fake = staged
    SandboxBackend().execute(ExecRequest([ENV_PYTHON, "eval.py", "--batch", "64"],
                                         session.local_path, 60))
    ran = [c for c in fake.calls if "eval.py" in " ".join(c)]
    assert ran and ran[-1] == [ENV_PYTHON, "eval.py", "--batch", "64"], ran


# --------------------------------------------------------------------------- #
# Sessions: one lease per paper, found again, and always given back
# --------------------------------------------------------------------------- #
def test_a_session_is_keyed_on_the_local_checkout_so_both_halves_find_it(staged):
    """Why the backend interface needed no new parameter.

    `plan_execution` builds one backend instance and `run_probe` builds another, so a
    session held on an instance would be lost between planning and execution. The local
    checkout is the identifier both halves already carry — `capability` gets it as
    `acq.path`, `execute` gets it as `req.cwd`.
    """
    session, _ = staged
    assert sandbox_mod.session_for(session.local_path, revive=False) is not None
    record = sandbox_mod.session_path(session.local_path)
    assert record.is_file() and record.name == "sandbox.json"
    assert record.parent.name == "p", "beside the checkout, never inside the tree git watches"
    assert not (Path(session.local_path) / "sandbox.json").exists()


def test_a_second_target_reuses_the_lease_rather_than_paying_for_another(
        tmp_path, monkeypatch, credentialed):
    """`plan_execution` runs once per target, so provisioning runs once per target.

    Without reuse, a three-target paper leases three machines and stages the checkout three
    times. The session is per PAPER, and the record on disk is what makes that true across
    the backend instances each stage builds for itself.
    """
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    created = []
    monkeypatch.setattr(sandbox_mod, "_create",
                        lambda spec: created.append(spec) or _FakeSandbox())
    b = SandboxBackend()
    for _ in range(3):
        acq = b.provision(_open_cfg(), tmp_path, "p", _acq(str(checkout)))
        assert acq.env_status == "ready", acq.reason
    assert len(created) == 1, f"leased {len(created)} machines for one paper"


def test_a_session_staged_for_a_different_commit_is_released_not_reused(
        tmp_path, monkeypatch, credentialed):
    """The guarantee is that every command in a session ran against ONE verified tree."""
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    second_sha = "c" * 40
    fakes = iter([_FakeSandbox(object_id="sb-1", head=SHA),
                  _FakeSandbox(object_id="sb-2", head=second_sha)])
    leased: list[_FakeSandbox] = []
    monkeypatch.setattr(sandbox_mod, "_create",
                        lambda spec: leased.append(next(fakes)) or leased[-1])
    b = SandboxBackend()
    first = b.provision(_open_cfg(), tmp_path, "p", _acq(str(checkout)))
    assert first.env_status == "ready", first.reason
    other = b.provision(_open_cfg(), tmp_path, "p", _acq(str(checkout), commit=second_sha))
    assert other.env_status == "ready", other.reason
    assert leased[0].terminated, "the lease for the old commit was given back"
    assert not leased[1].terminated
    assert sandbox_mod.session_for(str(checkout), revive=False).commit == second_sha


def test_release_is_idempotent_and_safe_with_nothing_staged(tmp_path, staged):
    released, detail = sandbox_mod.release(tmp_path / "nothing")
    assert released is False and "no sandbox session is recorded" in detail

    session, fake = staged
    ok, detail = sandbox_mod.release(session.local_path)
    assert ok and fake.terminated and session.sandbox_id in detail
    again, _ = sandbox_mod.release(session.local_path)
    assert again is False, "releasing twice must not raise"


def test_release_is_wired_into_the_stage_so_a_crash_cannot_leave_a_machine_running(
        tmp_path, monkeypatch):
    """The one failure mode in this seam that costs money rather than accuracy.

    Every other path in `probe.run` is designed to be survivable, which means an exception
    can leave the function without reaching a teardown written inline. `release` is called
    from a `finally`, and backends that lease nothing return False and pay a no-op.
    """
    from harness.stages import probe as probe_stage

    calls = []

    class _Recording(SandboxBackend):
        def release(self, root, pid):
            calls.append(pid)
            return True, "released"

    monkeypatch.setattr("harness.backends.select_backend", lambda cfg: _Recording())
    monkeypatch.setattr(probe_stage, "_review",
                        lambda cfg, pid: (_ for _ in ()).throw(RuntimeError("mid-review")))
    with pytest.raises(RuntimeError, match="mid-review"):
        probe_stage.run(_cfg(), "some-paper")
    assert calls == ["some-paper"], "the lease was given back despite the crash"


def test_a_teardown_fault_never_replaces_the_reviews_own_outcome(monkeypatch):
    from harness.stages import probe as probe_stage

    class _Broken(SandboxBackend):
        def release(self, root, pid):
            raise RuntimeError("provider unreachable at teardown")

    monkeypatch.setattr("harness.backends.select_backend", lambda cfg: _Broken())
    monkeypatch.setattr(probe_stage, "_review", lambda cfg, pid: {"paper_id": pid, "ok": True})
    assert probe_stage.run(_cfg(), "p") == {"paper_id": "p", "ok": True}


def test_the_local_backend_leases_nothing_and_says_so(tmp_path):
    from harness.backends import LocalBackend

    released, detail = LocalBackend().release(tmp_path, "p")
    assert released is False and "leases nothing" in detail
    assert LocalBackend().commit_tree(str(tmp_path)) is None, "local verifies the local tree"


def test_cleanup_ends_the_lease_and_keeps_every_artifact(tmp_path, monkeypatch, credentialed):
    checkout = tmp_path / "runs" / "p" / "repo"
    checkout.mkdir(parents=True)
    keep = checkout / "eval.py"
    keep.write_text("x = 1\n", encoding="utf-8")
    results = tmp_path / "runs" / "p" / "probe_results.json"
    results.write_text("{}", encoding="utf-8")
    fake = _FakeSandbox()
    monkeypatch.setattr(sandbox_mod, "_create", lambda spec: fake)
    b = SandboxBackend()
    b.provision(_open_cfg(), tmp_path, "p", _acq(str(checkout)))
    removed = b.cleanup(tmp_path, "p")
    assert removed == [str(sandbox_mod.session_path(checkout))]
    assert fake.terminated
    assert keep.exists() and results.exists(), "the evidence a reader checks survives"


def test_the_session_record_survives_a_round_trip_through_disk(staged):
    session, _ = staged
    raw = json.loads(sandbox_mod.session_path(session.local_path).read_text(encoding="utf-8"))
    again = SandboxSession.from_json(raw)
    assert again.sandbox_id == session.sandbox_id
    assert again.commit == SHA and again.env_python == ENV_PYTHON
    assert again.spec == session.spec
    assert "MODAL_TOKEN" not in json.dumps(raw), "no credential is ever persisted"


def test_a_lease_can_be_priced_and_an_unknown_tier_is_not_invented():
    assert sandbox_mod.lease_cost_usd(SandboxSpec(gpu="A100-80GB"), 3600) > 2.4
    unknown = sandbox_mod.lease_cost_usd(SandboxSpec(gpu="RTX-9090"), 3600)
    assert 0 < unknown < 1, "cpu+memory still price; no GPU rate is guessed"


# --------------------------------------------------------------------------- #
# Selection: an explicit choice is not silently overridden
# --------------------------------------------------------------------------- #
def _req(**kw) -> ResourceRequirement:
    return ResourceRequirement(
        evidence=[ResourceEvidence(quote="q", kind="declared_requirement")], **kw)


def test_the_operators_named_backend_wins_among_equally_sufficient_runners(monkeypatch):
    """`SH_EXEC_BACKEND` meant nothing whenever a second runner also fitted.

    Ranking on VRAM alone made a laptop "smaller" than a leased Linux box, so an operator
    who asked for the sandbox had their small experiments silently run here — a run on
    hardware they did not choose, with nothing in the record saying a choice was
    overridden. Selection may still rule the named backend OUT, which it reports; what it
    may not do is quietly prefer a different one.
    """
    monkeypatch.setattr(SandboxBackend, "available",
                        lambda self: BackendAvailability(True, ""))
    req = _req(ram_bytes=2 * GIB, vram_bytes=1 * GIB)

    chose_local = select_for(req, _cfg(exec_backend="local"))
    asked_modal = select_for(req, _cfg(exec_backend="modal", sandbox_gpu="A10G"))
    assert chose_local.reason_code == "selected" and chose_local.chosen.name == "local"
    assert asked_modal.reason_code == "selected", asked_modal.reason
    assert asked_modal.chosen.name == "modal", (
        "the operator asked for the sandbox and a sufficient laptop replaced it")


def test_selection_may_still_rule_the_named_backend_out_and_says_why(monkeypatch):
    monkeypatch.setattr(SandboxBackend, "available",
                        lambda self: BackendAvailability(True, ""))
    # 80 GiB does not fit an A10G, so naming it cannot select it.
    sel = select_for(_req(ram_bytes=2 * GIB, vram_bytes=80 * GIB),
                     _cfg(exec_backend="modal", sandbox_gpu="A10G"))
    verdicts = {n: v for n, v, _ in sel.considered}
    assert verdicts["modal"] == "resources_insufficient"
    assert sel.chosen is None or sel.chosen.name != "modal"


def test_an_unreachable_sandbox_is_reported_as_the_closest_thing_to_a_yes(monkeypatch):
    """A registered runner that fits and is merely gated outranks a declaration.

    It is the one refusal in `select_for` that can resolve itself without anything about
    the paper or the host changing — one environment variable — so it is reported ahead of
    "somewhere with a T4 could host this and we hold no credentials", which is true and
    actionable by nobody.
    """
    import sys
    if sys.platform.startswith("linux"):
        pytest.skip("needs a non-Linux host so the local backend is ruled out on platform")
    sel = select_for(_req(ram_bytes=2 * GIB, vram_bytes=12 * GIB),
                     _cfg(allow_sandbox=False, sandbox_gpu="A10G"),
                     declared_platform="linux")
    assert sel.reason_code == "backend_unavailable", sel.reason
    assert "SH_ALLOW_SANDBOX" in sel.reason
    assert sel.chosen is None


def test_a_declared_reservation_of_no_gpu_matches_no_vram_demand():
    """The default reservation must not silently satisfy a GPU experiment.

    `SH_SANDBOX_GPU` is empty by default so a CPU-bound review does not bill for an
    accelerator — which means the profile reports no VRAM, and `_fits` treats an unknown
    offer as one that does not satisfy a stated demand.
    """
    assert spec_from_config(_cfg(sandbox_gpu="")).vram_bytes is None
    sel = select_for(_req(ram_bytes=2 * GIB, vram_bytes=12 * GIB), _cfg(sandbox_gpu=""))
    verdicts = {n: v for n, v, _ in sel.considered}
    assert verdicts["modal"] == "resources_insufficient"


# --------------------------------------------------------------------------- #
# The whole chain, on a backend that is not this machine
# --------------------------------------------------------------------------- #
def test_every_gate_still_stands_between_a_remote_backend_and_an_execution(monkeypatch):
    """`authorize` is unchanged, and a new backend inherits the policy by construction.

    This is the point of the seam: the sandbox backend adds no path around the gates. Each
    precondition is removed in turn from an otherwise-authorized remote spec, and each
    one alone is disqualifying.
    """
    monkeypatch.setattr(SandboxBackend, "available",
                        lambda self: BackendAvailability(True, ""))
    verified = CommitVerification(state="verified", expected=SHA, actual=SHA)

    def _spec(**over) -> ProbeSpec:
        spec = ProbeSpec(
            paper_id="p", command=["python", "eval.py"], provenance="repo_exec",
            backend="modal",
            experiment=ExperimentIdentity(state="established", established=True, reason="f"),
            metric_identity=MetricIdentity(state="established", established=True, reason="f"),
            configuration=ConfigurationIdentity(state="established", established=True,
                                                reason="f"),
            capability=ExecCapability(established=True, reason_code="established"),
            resources=ResourceCapability(state="satisfied", reason="fits"))
        for k, v in over.items():
            setattr(spec, k, v)
        return spec

    b, open_cfg = SandboxBackend(), _cfg(allow_repo_exec=True, allow_sandbox=True)
    assert authorize(open_cfg, _spec(), b, commit=verified).allowed

    assert not authorize(_cfg(allow_repo_exec=False, allow_sandbox=True), _spec(), b,
                         commit=verified).allowed
    assert not authorize(open_cfg, _spec(), b, commit=None).allowed
    assert not authorize(open_cfg, _spec(provenance="synthesized"), b,
                         commit=verified).allowed
    assert not authorize(open_cfg, _spec(experiment=None), b, commit=verified).allowed
    assert not authorize(open_cfg, _spec(capability=None), b, commit=verified).allowed
    assert not authorize(open_cfg, _spec(resources=None), b, commit=verified).allowed
    # An assessment made for one environment is not an assessment of another.
    mismatch = authorize(open_cfg, _spec(backend="local"), b, commit=verified)
    assert not mismatch.allowed and mismatch.decision == "backend_mismatch"


def _git(repo: Path, *args: str) -> None:
    import subprocess

    p = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"


def test_planning_against_an_unavailable_sandbox_records_it_and_promotes_nothing(tmp_path,
                                                                                 monkeypatch):
    """The whole planning path with `modal` selected and no credentials — a real checkout.

    The unit tests above drive each backend method; this drives `plan_execution`, which is
    what actually decides whether a spec becomes `repo_exec`. Two things must hold and
    both are easy to get wrong:

    the assessment still HAPPENS — capability, commit state and the backend name are all
    recorded, so the report can say precisely why a reproduction was impossible rather
    than leaving it unknown; and nothing is promoted, so the template probe still runs on
    the local backend and no `repo_exec` provenance is minted for a machine that was never
    obtained.
    """
    from harness.stages import probe as probe_stage

    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    repo = tmp_path / "runs" / "p" / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    (repo / "eval.py").write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser(); p.add_argument('--seed', type=int)\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    commit = repo_mod.head_commit(repo)

    acq = RepoAcquisition(url="https://github.com/a/b.git", status="cloned", path=str(repo),
                          commit=commit, entrypoint="eval.py",
                          dependency_files=["requirements.txt"])
    cfg = _cfg(exec_backend="modal", allow_sandbox=True, allow_network=True,
               allow_install=True, allow_repo_exec=True)
    spec = probe_stage.plan_execution(
        cfg, ProbeSpec(paper_id="p", provenance="template", script="print('x')"), acq,
        root=tmp_path)

    assert spec.backend == "modal", "the assessment is recorded against the selected backend"
    assert spec.capability is not None and not spec.capability.established
    assert "no remote sandbox is staged" in spec.capability.detail
    # BLOCKED, and the reason NAMES the obstruction — but which obstruction it is depends
    # on the host, and the test must not depend on the host. With the `modal` client
    # installed the sandbox refuses for want of credentials; without it, the client itself
    # is missing. Both are "no machine was obtained", both leave `env_status` blocked, and
    # asserting only one of them made this test fail on every interpreter that simply does
    # not have the optional dependency. The property is that the refusal is recorded and
    # attributed to the sandbox, not that it has one particular cause.
    assert acq.env_status == "blocked", acq.env_status
    assert "no remote sandbox was staged" in acq.reason, acq.reason
    assert any(cause in acq.reason for cause in ("credentials", "modal")), acq.reason
    # E2 was asked of the tree that WOULD run, which does not exist — not of the local
    # checkout, which is clean and at the audited commit and would have said `verified`.
    assert spec.commit_state != "verified", spec.commit_state
    # And nothing was promoted.
    assert spec.command == [] and spec.provenance == "template"


def test_a_synthesized_probe_still_runs_locally_even_when_the_sandbox_is_configured():
    """Code THIS harness authored measures THIS machine, and moving it changes the question.

    A generated probe and a noise-floor calibration are our own code. Running them in a
    leased sandbox would measure the sandbox, so `backend_for` sends them to the local
    backend regardless of what the operator configured for repository execution.
    """
    from harness.backends import backend_for

    spec = ProbeSpec(paper_id="p", provenance="template")
    assert backend_for(_cfg(exec_backend="modal"), spec).name == "local"
    repo_spec = ProbeSpec(paper_id="p", command=["python", "eval.py"], provenance="repo_exec")
    assert backend_for(_cfg(exec_backend="modal"), repo_spec).name == "modal"
