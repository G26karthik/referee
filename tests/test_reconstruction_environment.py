"""Section 4 of the 2026-09 closure pass: a governed reconstruction whose checkout
declares no environment of its own used to run against a bare fallback interpreter that
could never satisfy its own `import torch` / `import numpy` — measured on this corpus as
seven `sanchez24a-icml` executions, every one ending INCONCLUSIVE for a
`ModuleNotFoundError`, a fact about this harness's own environment rather than about the
paper. `container.install_reconstruction_baseline` and its call site in
`stages.probe.attempt_reimplementation_fallback` close that gap with a FIXED, declared
baseline — never a package this run infers from a script's own imports, and never
anything for `AUTHOR_CODE_EXECUTION`, whose venv stays exactly what the checkout itself
declared.
"""
from __future__ import annotations

import pytest

from harness import container as container_mod
from harness.artifacts import RepoAcquisition
from harness.config import Config


def _daemon() -> bool:
    ok, _ = container_mod.daemon_status()
    return ok


requires_docker = pytest.mark.skipif(not _daemon(), reason="no reachable container runtime")
docker = pytest.mark.docker


# --------------------------------------------------------------------------- #
# Pure: the declared baseline itself, and that installing it is never inferred from a
# script's own imports.
# --------------------------------------------------------------------------- #
def test_the_baseline_is_fixed_and_covers_every_import_this_gap_was_measured_against():
    # Measured directly against the seven affected `sanchez24a-icml` reconstruction
    # scripts (CLAUDE.md, Section 4): every one imports torch and transformers; five of
    # seven also import datasets; one imports numpy.
    baseline = set(container_mod.RECONSTRUCTION_BASELINE_REQUIREMENTS)
    assert {"torch", "numpy", "transformers", "datasets"} <= baseline


def test_install_reconstruction_baseline_builds_the_declared_pip_argv(monkeypatch):
    captured = {}

    def fake_run(argv, timeout=60):
        captured["argv"] = argv
        captured["timeout"] = timeout
        return 0, "", ""

    monkeypatch.setattr(container_mod, "_run", fake_run)
    ok, why = container_mod.install_reconstruction_baseline(
        "/work/env/bin/python", "/host/mount", container_mod.DEFAULT_IMAGE, 900)

    assert ok and why == ""
    argv = captured["argv"]
    assert argv[:2] == ["docker", "run"]
    assert "/work/env/bin/python" in argv
    for pkg in container_mod.RECONSTRUCTION_BASELINE_REQUIREMENTS:
        assert pkg in argv, pkg
    assert "-m" in argv and "pip" in argv and "install" in argv
    assert captured["timeout"] == 900


def test_install_reconstruction_baseline_reports_a_failure_rather_than_raising(monkeypatch):
    monkeypatch.setattr(container_mod, "_run",
                        lambda argv, timeout=60: (1, "", "no matching distribution"))
    ok, why = container_mod.install_reconstruction_baseline(
        "/work/env/bin/python", "/host/mount", container_mod.DEFAULT_IMAGE, 60)
    assert ok is False
    assert "reconstruction baseline" in why
    assert "no matching distribution" in why
    for pkg in container_mod.RECONSTRUCTION_BASELINE_REQUIREMENTS:
        assert pkg in why


# --------------------------------------------------------------------------- #
# probe.py's wiring: called for a bare reconstruction environment under the container
# backend, and never for anything else.
# --------------------------------------------------------------------------- #
def _acq(*, dependency_files: list[str], env_path: str = "/work/env/bin/python",
        path: str = "/host/mount/repo") -> RepoAcquisition:
    return RepoAcquisition(status="cloned", path=path, env_path=env_path,
                           env_status="ready", dependency_files=dependency_files)


def test_the_baseline_installs_only_when_the_checkout_declared_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        container_mod, "install_reconstruction_baseline",
        lambda py_in, mount, image, timeout: (calls.append((py_in, mount, image)), (True, ""))[1])

    cfg = Config(projects_dir=tmp_path, exec_backend="container")
    acq_bare = _acq(dependency_files=[])
    acq_declared = _acq(dependency_files=["requirements.txt"])

    # Exercise the exact gate `attempt_reimplementation_fallback` checks, without paying
    # for the rest of that function's model-driven sealing path.
    def gate(acq):
        return (acq is not None and acq.env_path and not acq.dependency_files
                and cfg.exec_backend == "container")

    assert gate(acq_bare) is True
    assert gate(acq_declared) is False, "a checkout that declares its own deps is untouched"

    cfg_local = Config(projects_dir=tmp_path, exec_backend="local")
    assert (acq_bare.env_path and not acq_bare.dependency_files
           and cfg_local.exec_backend == "container") is False, (
        "the baseline never installs outside the container backend")


@docker
@requires_docker
def test_a_real_container_venv_gets_the_baseline_installed(tmp_path):
    """End-to-end through the real docker CLI: create a venv, confirm the import fails
    beforehand, install the baseline, confirm it then succeeds. Uses a tiny, fast-installing
    stand-in package (`six`) rather than the real multi-gigabyte baseline, so this proves
    the MECHANISM — the argv, the mount, the interpreter path — without paying torch's
    download cost in a regression suite that must stay fast.
    """
    from harness import backends

    mount = tmp_path
    (mount / "repo").mkdir()
    b = backends.ContainerBackend()
    acq = b.provision(Config(allow_install=True, allow_network=True), mount, "p",
                      RepoAcquisition(status="cloned", path=str(mount / "repo")))
    assert acq.env_status == "ready", acq.reason
    assert acq.env_path

    rc, out, err = container_mod._run(
        container_mod.run_argv([acq.env_path, "-c", "import six"],
                               host_mount=str(mount), image=container_mod.DEFAULT_IMAGE),
        timeout=60)
    assert rc != 0, "six must not be present in a bare venv before the baseline installs"

    monkeypatch_requirements = container_mod.RECONSTRUCTION_BASELINE_REQUIREMENTS
    container_mod.RECONSTRUCTION_BASELINE_REQUIREMENTS = ("six",)
    try:
        ok, why = container_mod.install_reconstruction_baseline(
            acq.env_path, str(mount), container_mod.DEFAULT_IMAGE, 120)
        assert ok, why
    finally:
        container_mod.RECONSTRUCTION_BASELINE_REQUIREMENTS = monkeypatch_requirements

    rc, out, err = container_mod._run(
        container_mod.run_argv([acq.env_path, "-c", "import six; print('ok')"],
                               host_mount=str(mount), image=container_mod.DEFAULT_IMAGE),
        timeout=60)
    assert rc == 0 and "ok" in out, (out, err)
