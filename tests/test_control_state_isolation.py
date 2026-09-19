# tests/test_control_state_isolation.py
from pathlib import Path

from harness import container as container_mod
from harness.backends import ContainerBackend
from harness.artifacts import RepoAcquisition
from harness import state


CONTROL_FILENAMES = {
    "probe_results.json", "validation.driver.json", "outcome.json",
}


def test_container_mount_never_contains_control_state_filenames(tmp_path):
    """The container backend bind-mounts `Path(acq.path).parent`. After this task,
    that directory (`runs/<pid>/`) must contain only execution workspace — the
    checkout, the built env, and per-target script/output directories — and never a
    control-state JSON file a later invocation would read back as trusted.
    """
    root = tmp_path / "projects" / "fixture"
    runs_dir = root / "runs" / "fixture"
    (runs_dir / "repo").mkdir(parents=True)
    (runs_dir / "repo" / ".git").mkdir()  # looks like a checkout

    acq = RepoAcquisition(status="cloned", path=str(runs_dir / "repo"))
    backend = ContainerBackend()
    mount = backend._mount(acq)
    assert Path(mount) == runs_dir, "sanity: the mount is still the parent of the checkout"

    # Simulate a full run: everything Task 3 relocates should now live under control/,
    # a sibling of runs/, not inside it.
    control = state.control_dir(root)
    state.write_json(control / "probe_results.json", {"provenance": "repo_exec"})
    state.write_json(control / "validation.driver.json", {"design": {}})
    state.write_json(control / "targets" / "t1" / "outcome.json", {"disposition": "REPRODUCED"})

    mounted_names = {p.name for p in Path(mount).rglob("*") if p.is_file()}
    assert not (mounted_names & CONTROL_FILENAMES), (
        f"control-state file(s) found inside the bind-mounted directory: "
        f"{mounted_names & CONTROL_FILENAMES}")


def test_control_dir_is_a_sibling_of_runs_not_inside_it(tmp_path):
    root = tmp_path / "projects" / "fixture"
    control = state.control_dir(root)
    assert control == root / "control"
    assert not str(control).startswith(str(root / "runs"))
