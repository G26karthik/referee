"""The container backend: what it builds, what it refuses, and what it really does.

The Phase-1 release audit found real `docker run` code with ZERO tests. Every process this
harness had ever launched came from `LocalBackend` under a fixture that monkeypatches the
isolation level to get past `authorize()`. So the one backend entitled to run a paper's own
code had never been exercised, and `SH_ALLOW_REPO_EXEC` was guarding a path nobody had run.

Two layers, deliberately separated:

  * **Pure tests** assert what a container run IS — the argv, the mount, the network
    policy, the path translation, the refusals. They need no Docker and run everywhere,
    which matters because these are the properties that must not drift silently.
  * **Live tests** actually start containers. They are skipped when no daemon is reachable
    and are marked `docker`, so a laptop without Docker Desktop running still gets a green
    suite and nobody is tempted to weaken the pure tests to compensate.

Nothing here lowers an admissibility requirement to obtain a launch. The authorization
tests below assert the opposite: that a container backend still refuses without a verified
commit, established identity and open gate, exactly as any other backend does.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness import backends, container as container_mod, isolation as iso
from harness.artifacts import (CommitVerification, ConfigurationIdentity, ExecCapability,
                               ExecutionRecord, ExperimentIdentity, MetricIdentity,
                               ProbeSpec, RepoAcquisition, ResourceCapability)
from harness.config import Config
from harness.local_exec import run_probe
from harness.repo import head_commit


def _daemon() -> bool:
    return container_mod.daemon_status()[0]


requires_docker = pytest.mark.skipif(not _daemon(), reason="no reachable container runtime")
docker = pytest.mark.docker


# --------------------------------------------------------------------------- #
# What a container run IS. No daemon needed.
# --------------------------------------------------------------------------- #
def test_the_run_argv_carries_rm_the_mount_and_the_workdir():
    argv = container_mod.run_argv(["python", "train.py"], host_mount=r"C:\runs\p",
                                  workdir="/work/repo")
    assert argv[:3] == ["docker", "run", "--rm"], "a review must not accumulate containers"
    assert "-v" in argv and f"C:\\runs\\p:{container_mod.MOUNT}" in argv
    assert argv[argv.index("-w") + 1] == "/work/repo"
    assert argv[-2:] == ["python", "train.py"], "the process command must be relayed last"


def test_the_run_itself_gets_no_network_and_provisioning_does():
    """An experiment that reaches the network mid-run is not reproducible.

    Provisioning is the exception and must keep it, because pip needs an index. The two
    call sites are what make this a policy rather than an accident.
    """
    run = container_mod.run_argv(["python", "x.py"], host_mount="/m", network=False)
    assert run[run.index("--network") + 1] == "none"
    prov = container_mod.run_argv(["python", "-m", "venv", "/work/env"], host_mount="/m")
    assert "--network" not in prov


def test_a_named_container_is_requested_so_a_timeout_has_something_to_kill():
    name = container_mod.container_name("apt-icml", "seed=3 arm=reproduction")
    argv = container_mod.run_argv(["python", "x.py"], host_mount="/m", name=name)
    assert argv[argv.index("--name") + 1] == name
    assert name.startswith("sh-") and "apt-icml" in name
    assert container_mod.container_name("a", "b") != container_mod.container_name("a", "b")
    # Docker names accept only [a-zA-Z0-9][a-zA-Z0-9_.-]*; a label carries spaces and '='.
    assert " " not in name and "=" not in name


def test_a_path_outside_the_mount_is_refused_rather_than_relayed():
    """A silent passthrough is how a host path ends up in an argv the container cannot
    resolve, and the resulting file-not-found would be recorded against the paper."""
    mount = r"C:\runs\p"
    assert container_mod.to_container_path(r"C:\runs\p\repo", mount) == "/work/repo"
    assert container_mod.to_container_path(mount, mount) == container_mod.MOUNT
    assert container_mod.to_container_path(r"C:\Windows\System32", mount) == ""
    assert container_mod.to_container_path("", mount) == ""
    assert container_mod.to_container_path(r"C:\runs\p\repo", "") == ""


def test_execute_refuses_a_cwd_that_was_never_translated():
    """The refusal that keeps a container backend from running on the host.

    Reachable without a daemon because it is checked before anything is started, which is
    the point: the guard must not depend on Docker being present.
    """
    out = backends.ContainerBackend().execute(backends.ExecRequest(
        argv=["python", "train.py"], cwd=r"C:\runs\p\repo", timeout_s=10))
    assert out.launched is False and out.completed is False
    assert "not inside /work" in out.error
    assert "not prepared for a container" in out.error


def test_the_backend_declares_container_isolation_and_the_gate_accepts_only_that():
    b = backends.ContainerBackend()
    assert b.isolation == "CONTAINER"
    assert iso.sufficient_for_repo_exec("CONTAINER") is True
    assert iso.sufficient_for_repo_exec("REMOTE_SESSION") is True
    assert iso.sufficient_for_repo_exec("VENV") is False, (
        "a venv scopes imports and wall time and confines nothing else")


def test_an_unsupported_python_falls_back_with_a_stated_reason():
    img, why = container_mod.image_for("3.7")
    assert img == container_mod.DEFAULT_IMAGE and "no official slim image" in why
    img, why = container_mod.image_for("3.12")
    assert img == "python:3.12-slim" and why == ""
    img, why = container_mod.image_for("")
    assert img == container_mod.DEFAULT_IMAGE and why == ""


def test_the_execution_record_can_carry_the_launch_command_line():
    """A containerised record that names the script but not the image is not evidence.

    `argv` stays the command the PROCESS received so records compare across backends;
    `launch_argv` is what this harness issued.
    """
    rec = ExecutionRecord(argv=["python", "train.py"],
                          launch_argv=["docker", "run", "--rm", "--network", "none",
                                       "python:3.11-slim", "python", "train.py"])
    assert rec.argv == ["python", "train.py"]
    assert "--network" in rec.launch_argv and "python:3.11-slim" in rec.launch_argv
    assert ExecutionRecord().launch_argv == [], "empty for a backend that runs argv directly"


def test_provision_refuses_without_the_install_and_network_gates(tmp_path):
    acq = RepoAcquisition(status="cloned", path=str(tmp_path / "runs" / "p" / "repo"))
    got = backends.ContainerBackend().provision(
        Config(allow_install=False, allow_network=True), tmp_path, "p", acq)
    assert got.env_status == "blocked" and "gate is shut" in got.reason


def test_a_declared_only_backend_cannot_execute_and_the_container_one_can():
    assert backends.ContainerBackend().profile().can_execute is True
    for name in ("kaggle", "colab"):
        assert backends._REGISTRY[name]().profile().can_execute is False


# --------------------------------------------------------------------------- #
# Authorization does not soften for a container. These need no daemon either.
#
# The risk this section exists for: a backend that finally CLEARS the isolation gate is
# exactly the one on which somebody would be tempted to relax a scientific condition to
# obtain a launch. Satisfying isolation must buy isolation and nothing else.
# --------------------------------------------------------------------------- #
VERIFIED = CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)


def _qualified_spec() -> ProbeSpec:
    """Everything except isolation already satisfied, so one condition decides at a time."""
    est = dict(state="established", established=True, reason="fixture")
    return ProbeSpec(
        paper_id="p", command=["python", "eval.py"], provenance="repo_exec",
        experiment=ExperimentIdentity(**est), metric_identity=MetricIdentity(**est),
        configuration=ConfigurationIdentity(**est),
        # `established` is a derived property on both of these, not a settable field:
        # ExecCapability reads `reason_code`, ResourceCapability reads `state == satisfied`.
        capability=ExecCapability(established=True, reason_code="established"),
        resources=ResourceCapability(state="satisfied", reason="fixture"),
        backend="container")


class _OfflineContainer(backends.ContainerBackend):
    """The real class with only availability stubbed, so no daemon is needed.

    Subclassed rather than monkeypatched because what is under test is the class's own
    declared profile: a test that replaced `profile()` would assert its own fixture.
    """

    def available(self):                                     # noqa: D102
        return backends.BackendAvailability(True, "stubbed available for this test")

    def resources(self):                                     # noqa: D102
        return backends.BackendResources(name="container", platform="linux",
                                         python="python3", ram_bytes=8 << 30,
                                         disk_bytes=100 << 30, cpu_count=4,
                                         detail="stubbed")


def test_the_container_backend_is_authorized_when_every_condition_holds():
    """The positive case. Without it the negatives below could all pass vacuously."""
    cfg = Config.load()
    cfg.allow_repo_exec = True
    a = backends.authorize(cfg, _qualified_spec(), _OfflineContainer(), commit=VERIFIED)
    assert a.allowed is True and a.decision == "authorized", a.decision


@pytest.mark.parametrize("mutate,expected", [
    (lambda s: setattr(s, "provenance", "synthesized"), "provenance_insufficient"),
    (lambda s: setattr(s, "experiment", ExperimentIdentity(state="ambiguous")),
     "identity_unproven"),
    (lambda s: setattr(s, "metric_identity", MetricIdentity(state="unmapped")),
     "identity_unproven"),
    (lambda s: setattr(s, "configuration", ConfigurationIdentity(state="unmapped")),
     "identity_unproven"),
    (lambda s: setattr(s, "capability", ExecCapability(reason_code="dependency_missing")),
     "capability_unproven"),
    (lambda s: setattr(s, "resources", ResourceCapability(state="insufficient")),
     "resources_unproven"),
    (lambda s: setattr(s, "resources", ResourceCapability(state="unknown")),
     "resources_unproven"),
])
def test_a_container_does_not_excuse_any_scientific_condition(mutate, expected):
    cfg = Config.load()
    cfg.allow_repo_exec = True
    spec = _qualified_spec()
    mutate(spec)
    a = backends.authorize(cfg, spec, _OfflineContainer(), commit=VERIFIED)
    assert a.allowed is False and a.decision == expected, a.decision


def test_a_container_does_not_excuse_an_unverified_commit():
    cfg = Config.load()
    cfg.allow_repo_exec = True
    a = backends.authorize(cfg, _qualified_spec(), _OfflineContainer(), commit=None)
    assert a.allowed is False and a.decision == "commit_unverified"


def test_a_container_does_not_open_a_shut_gate():
    cfg = Config.load()
    cfg.allow_repo_exec = False
    a = backends.authorize(cfg, _qualified_spec(), _OfflineContainer(), commit=VERIFIED)
    assert a.allowed is False and a.decision == "gate_closed"


# --------------------------------------------------------------------------- #
# Live. These really start containers.
# --------------------------------------------------------------------------- #
@pytest.fixture
def mounted(tmp_path):
    """A `runs/<pid>` directory laid out the way `provision` leaves one."""
    mount = tmp_path / "runs"
    (mount / "repo").mkdir(parents=True)
    b = backends.ContainerBackend()
    b._host_mount = str(mount)
    b._image_name = container_mod.DEFAULT_IMAGE
    return b, mount, container_mod.to_container_path(str(mount / "repo"), str(mount))


@docker
@requires_docker
def test_the_daemon_reports_itself_and_the_backend_is_available():
    ok, why = container_mod.daemon_status()
    assert ok and "reachable" in why
    av = backends.ContainerBackend().available()
    assert av.usable is True and av.detail


@docker
@requires_docker
def test_resources_are_measured_from_the_daemon_not_declared():
    """The memory figure must be the daemon's, not the host's.

    On a Desktop install the Linux VM gets a slice of the machine; reporting the host's
    figure hands `select_for` a machine that does not exist and turns an honest resource
    refusal into a crash inside the experiment.
    """
    r = backends.ContainerBackend().resources()
    assert r.platform == "linux"
    assert r.ram_bytes > 0 and r.cpu_count and r.cpu_count > 0
    assert r.disk_bytes > 0
    inv = container_mod.inventory()
    assert inv["ok"] is True and inv["ram_bytes"] == r.ram_bytes


@docker
@requires_docker
def test_a_real_container_runs_and_its_stdout_stderr_and_exit_code_come_back(mounted):
    b, mount, repo_in = mounted
    (mount / "repo" / "emit.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'accuracy': 0.873}))\n"
        "sys.stderr.write('on-stderr\\n')\n"
        "sys.exit(3)\n", encoding="utf-8")
    out = b.execute(backends.ExecRequest(argv=["python", "emit.py"], cwd=repo_in,
                                         timeout_s=120, label="seed=0 arm=reproduction"))
    assert out.launched and out.completed and out.timed_out is False
    assert out.returncode == 3, "a non-zero exit is an outcome, not a launch failure"
    assert json.loads(out.stdout.strip())["accuracy"] == 0.873
    assert "on-stderr" in out.stderr
    assert out.backend == "container" and out.seconds > 0
    assert out.started_at and out.ended_at


@docker
@requires_docker
def test_the_record_carries_the_docker_line_that_produced_it(mounted):
    b, mount, repo_in = mounted
    (mount / "repo" / "ok.py").write_text("print('hi')\n", encoding="utf-8")
    out = b.execute(backends.ExecRequest(argv=["python", "ok.py"], cwd=repo_in,
                                         timeout_s=60))
    assert out.argv == ["python", "ok.py"], "argv stays the command the process received"
    assert out.launch_argv[:3] == ["docker", "run", "--rm"]
    assert container_mod.DEFAULT_IMAGE in out.launch_argv
    assert "none" in out.launch_argv, "the network policy must be in the record"
    assert any(s.endswith(f":{container_mod.MOUNT}") for s in out.launch_argv)


@docker
@requires_docker
def test_the_operators_machine_is_not_visible_from_inside(mounted):
    """What CONTAINER is worth here, asserted rather than assumed.

    Not a claim of adversarial containment against a shared kernel. The claim is the one
    the isolation vocabulary makes: the code runs somewhere that is not the operator's
    machine as they use it.
    """
    b, mount, repo_in = mounted
    (mount / "repo" / "look.py").write_text(
        "import os, socket\n"
        "print('home', os.path.exists('/c/Users'), os.path.exists(os.path.expanduser('~/.ssh')))\n"
        "print('cwd', os.getcwd())\n"
        "print('host', socket.gethostname())\n", encoding="utf-8")
    out = b.execute(backends.ExecRequest(argv=["python", "look.py"], cwd=repo_in,
                                         timeout_s=60))
    assert out.completed and out.returncode == 0
    assert "home False False" in out.stdout
    assert "cwd /work/repo" in out.stdout


@docker
@requires_docker
def test_the_run_really_has_no_network(mounted):
    b, mount, repo_in = mounted
    (mount / "repo" / "net.py").write_text(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=5)\n"
        "    print('REACHED')\n"
        "except OSError as e:\n"
        "    print('refused', type(e).__name__)\n", encoding="utf-8")
    out = b.execute(backends.ExecRequest(argv=["python", "net.py"], cwd=repo_in,
                                         timeout_s=90))
    assert "REACHED" not in out.stdout, "the run-time container must not reach the network"
    assert "refused" in out.stdout


@docker
@requires_docker
def test_the_environment_variables_a_request_carries_reach_the_process(mounted):
    b, mount, repo_in = mounted
    (mount / "repo" / "env.py").write_text(
        "import os; print('SEED', os.environ.get('SH_SEED'))\n", encoding="utf-8")
    out = b.execute(backends.ExecRequest(argv=["python", "env.py"], cwd=repo_in,
                                         timeout_s=60, env={"SH_SEED": "7"}))
    assert "SEED 7" in out.stdout


@docker
@requires_docker
def test_a_timeout_kills_the_container_and_does_not_merely_kill_the_client(mounted):
    """THE REGRESSION THIS FILE WAS WRITTEN FOR.

    `subprocess.run(timeout=)` kills `docker run`, the client. Before `container.remove`
    the container went on running, holding the GPU it reserved, while the harness recorded
    a timeout and started the next seed against a machine it believed was free.
    """
    b, mount, repo_in = mounted
    (mount / "repo" / "sleep.py").write_text("import time; time.sleep(600)\n",
                                             encoding="utf-8")
    out = b.execute(backends.ExecRequest(argv=["python", "sleep.py"], cwd=repo_in,
                                         timeout_s=5, label="timeout-probe"))
    assert out.launched is True, "it did start"
    assert out.completed is False and out.timed_out is True
    assert "THE CONTAINER MAY STILL BE RUNNING" not in out.error, out.error

    name = out.launch_argv[out.launch_argv.index("--name") + 1]
    alive = subprocess.run(["docker", "ps", "--quiet", "--filter", f"name={name}"],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    assert alive == "", f"container {name} survived its own timeout"


@docker
@requires_docker
def test_removing_a_container_that_already_exited_is_success_not_failure():
    # Docker 29 makes `rm --force` idempotent and exits 0 for an absent container; older
    # daemons print "No such container" and exit non-zero. Both must read as success,
    # because the outcome asked for is that the container is gone.
    ok, detail = container_mod.remove("sh-definitely-not-a-real-container-xyz")
    assert ok is True, detail
    assert "sh-definitely-not-a-real-container-xyz" in detail
    ok, detail = container_mod.remove("")
    assert ok is False and "no container name" in detail


@docker
@requires_docker
def test_containers_are_not_left_behind_after_a_normal_run(mounted):
    b, mount, repo_in = mounted
    (mount / "repo" / "quick.py").write_text("print('done')\n", encoding="utf-8")
    out = b.execute(backends.ExecRequest(argv=["python", "quick.py"], cwd=repo_in,
                                         timeout_s=60))
    name = out.launch_argv[out.launch_argv.index("--name") + 1]
    left = subprocess.run(["docker", "ps", "-a", "--quiet", "--filter", f"name={name}"],
                          capture_output=True, text=True, timeout=30).stdout.strip()
    assert left == "", "--rm should have removed it"


@docker
@requires_docker
def test_the_import_probe_runs_inside_the_container_not_on_this_host(mounted):
    """The venv lives in the container's filesystem view.

    Resolving its modules from the host would start nothing and report every dependency
    missing, refusing a capable environment as `dependency_missing` — a fact about this
    harness reported as a fact about the repository.
    """
    b, mount, _ = mounted
    acq = RepoAcquisition(status="cloned", path=str(mount / "repo"))
    resolve = b._resolver(acq)
    missing = resolve("python", mount / "repo", ["json", "definitely_not_a_module_xyz"])
    assert "definitely_not_a_module_xyz" in missing
    assert "json" not in missing, "the container's own interpreter has the stdlib"


@docker
@requires_docker
def test_provision_builds_the_environment_inside_the_container(tmp_path):
    """No interpreter is installed on the host. The venv is written through the mount."""
    mount = tmp_path / "runs"
    (mount / "repo").mkdir(parents=True)
    acq = RepoAcquisition(status="cloned", path=str(mount / "repo"), dependency_files=[])
    got = backends.ContainerBackend().provision(
        Config(allow_install=True, allow_network=True), tmp_path, "p", acq)
    assert got.env_status == "ready", got.reason
    assert got.env_path == "/work/env/bin/python"
    assert "declares no dependency file" in got.reason
    # The venv was written through the bind mount by the container's own Linux python, so
    # it is on the host disk. `bin/python` itself is a Linux symlink that Windows cannot
    # stat (WinError 1920), so the directory is what this asserts: the point is that the
    # environment landed on the host, not that Windows can follow a Linux symlink.
    assert (mount / "env" / "bin").is_dir(), "the venv must land on the host disk"
    assert (mount / "env" / "pyvenv.cfg").is_file()


# --------------------------------------------------------------------------- #
# The staging gap: `local_exec.run_probe` must call `stage()` before `execute()`
# --------------------------------------------------------------------------- #
# `ContainerBackend.execute`'s own docstring says it "expects req.argv/req.cwd to already
# be in this container's /work namespace — stage() is the translation step, called by
# local_exec.run_probe before this". Nothing enforced that until now: `run_probe`'s main
# execution loop passed `resolve_command`'s host `cwd` straight to `execute()`, which the
# refusal branch a few lines below this docstring correctly rejected as "not inside
# /work". A synthetic fixture, not a paper: this proves the staging wire-up, not any
# reproduction.
def _git(repo: Path, *args: str) -> None:
    p = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"


def _repo_at(path: Path, body: str) -> str:
    """A throwaway git checkout at an EXACT path — it must sit under the `mount_root`
    `run_probe` derives (`runs/<pid>`) for staging to have anything to translate.
    Returns its commit."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "run.py").write_text(body, encoding="utf-8")
    _git(path, "init", "--quiet")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "fixture")
    return head_commit(path)


EMITS_METRIC = """\
import argparse
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
print("SH_DEVICE cpu")
print(f"SH_METRIC arm=reproduction seed={a.seed} value={1.00 + a.seed * 0.01:.4f}")
"""


def _established(cls):
    return cls(state="established", established=True, reason="fixture")


def _container_spec(repo: Path, commit: str, *, interpreter: str = "") -> ProbeSpec:
    """A fully qualified repo_exec spec whose command names a BARE `python` — exactly what
    `resolve_command` rewrites to `spec.interpreter or cfg.python` before this harness's
    OWN venv (a host path with no meaning inside the container) reaches `stage()`, which
    is the fallback-substitution branch `stage()`'s own docstring describes.
    """
    return ProbeSpec(
        paper_id="fixture", provenance="repo_exec", backend="container",
        command=["python", "run.py", "--seed", "{seed}"],
        cwd=str(repo), interpreter=interpreter, commit=commit,
        arms=["reproduction"], seeds=[0, 1, 2],
        table_ref="T1:r0:c1", claimed_cell_value="1.00", target_id="T1",
        experiment=_established(ExperimentIdentity),
        metric_identity=_established(MetricIdentity),
        configuration=_established(ConfigurationIdentity),
        capability=ExecCapability(established=True, reason_code="established",
                                  interpreter_is_repo_env=True),
        resources=ResourceCapability(state="satisfied", reason="fixture fits"),
    )


def test_run_probe_stages_a_container_backends_paths_before_executing(tmp_path):
    """No daemon needed: `execute()` is swapped for a spy that records the request it
    actually received, isolating `run_probe`'s own behaviour from whether Docker happens
    to be reachable on the machine running the suite.
    """
    root = tmp_path / "projects" / "fixture"
    repo = root / "runs" / "fixture" / "repo"
    commit = _repo_at(repo, EMITS_METRIC)
    cfg = Config(projects_dir=tmp_path / "projects", allow_repo_exec=True)
    spec = _container_spec(repo, commit)

    captured: list[backends.ExecRequest] = []

    class _Spy(backends.ContainerBackend):
        def execute(self, req):
            captured.append(req)
            seed = int(req.argv[-1])
            value = 1.00 + seed * 0.01
            return backends.ExecOutcome(
                launched=True, completed=True, returncode=0,
                stdout=f"SH_DEVICE cpu\nSH_METRIC arm=reproduction seed={seed} value={value:.4f}\n",
                stderr="", seconds=0.1, started_at="t", ended_at="t",
                backend=self.name, argv=req.argv, cwd=req.cwd, environment="container")

    result = run_probe(cfg, root, spec, backend=_Spy())

    assert len(captured) == 3, "one execute() call per (seed, arm) pair"
    for req in captured:
        assert req.cwd.startswith(container_mod.MOUNT), (
            f"run_probe passed {req.cwd!r} straight to execute() without staging it into "
            f"the container's {container_mod.MOUNT} namespace — the exact gap "
            f"ContainerBackend.execute's own docstring already warns about")
        assert req.argv[0] == "python3", (
            "this harness's own host interpreter path must be substituted for the image's "
            "own interpreter, never relayed into the container")
    assert result.reconciliation is not None
    assert result.reconciliation.status == "RESOLVED_VERIFIED", result.reconciliation.reason


@docker
@requires_docker
def test_a_real_repo_exec_reaches_resolved_verified_through_the_container_backend(tmp_path):
    """End to end, against a real daemon. Before the staging fix, EVERY repo_exec run on
    the container backend refused at `execute()`'s own `not inside /work` guard, because
    `run_probe` hand it a host `cwd` unmodified — `authorize()`, commit verification and
    identity all passed, and the run still produced nothing. This is the scenario
    CLAUDE.md's Known Limitations section once described as already fixed and verified;
    run for real here rather than trusted from prose.
    """
    root = tmp_path / "projects" / "fixture"
    repo = root / "runs" / "fixture" / "repo"
    commit = _repo_at(repo, EMITS_METRIC)
    cfg = Config(projects_dir=tmp_path / "projects", allow_repo_exec=True)
    spec = _container_spec(repo, commit)

    result = run_probe(cfg, root, spec, backend=backends.ContainerBackend())

    assert result.authorization is not None and result.authorization.allowed, (
        result.authorization.detail if result.authorization else "no authorization recorded")
    assert result.seeds_run == [0, 1, 2] and not result.seeds_failed, result.reason
    rec = result.reconciliation
    assert rec is not None and rec.status == "RESOLVED_VERIFIED", (
        rec.reason if rec else "no reconciliation")
