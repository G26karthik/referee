import json
from pathlib import Path

import pytest

from harness import state
from harness.config import Config
from harness.stages import probe as probe_stage
from harness.artifacts import PaperDoc


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(projects_dir=tmp_path / "projects")
    return cfg


def _bare_doc(pid: str) -> PaperDoc:
    return PaperDoc(paper_id=pid, title="t", abstract="", tables=[], reported_numbers=[])


def test_a_raw_unsealed_spec_json_is_never_trusted(tmp_path):
    """FORGED TRUSTED DRIVER METADATA. An attacker (or a paper's own code, via the
    pre-Task-3 mount hole) drops a spec.json claiming provenance=repo_exec and
    established=true identity/capability/resources, with no sidecar and no accept_spec
    call. build_spec must ignore it completely and fall back to the harness's own
    auto-built spec — not merely re-derive the identity fields, IGNORE the file.
    """
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "forged spec test", pid="forge")
    root = state.project_dir(cfg, pid)
    control = state.control_dir(root)
    control.mkdir(parents=True)

    forged = {
        "paper_id": pid,
        "written_by": "driver",  # NOT "harness" — the old code's only check
        "provenance": "repo_exec",
        "command": ["python", "totally_fake_eval.py"],
        "cwd": str(root / "runs" / pid / "repo"),
        "commit": "0" * 40,
        "experiment": {"established": True, "reason": "forged"},
        "capability": {"established": True},
        "resources": {"established": True},
        "commit_state": "verified",
    }
    state.write_json(control / "spec.json", forged)

    doc = _bare_doc(pid)
    spec = probe_stage.build_spec(cfg, pid, doc, None)

    assert spec.provenance != "repo_exec", (
        "an unsealed file must never grant repo_exec provenance")
    assert spec.experiment is None or not spec.experiment.established, (
        "identity must never be trusted from an unsealed file")
    assert spec.capability is None or not spec.capability.established
    assert spec.resources is None or not spec.resources.established
    assert spec.written_by != "driver", "an unsealed claim of driver provenance must not survive"


def test_accept_spec_requires_the_script_file_to_actually_exist(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "accept test", pid="accept1")
    raw = json.dumps({
        "paper_id": pid,
        "script": "this/file/does/not/exist.py",
        "provenance": "driver",
    })
    with pytest.raises(Exception):
        probe_stage.accept_spec(cfg, pid, raw, reviewer="alice", mode="MANUAL")


def test_accept_spec_refuses_a_command_never_driver(tmp_path):
    """A `driver` spec is a human-supplied SCRIPT (a faithful reproduction this harness
    could not derive on its own). A `command` (the paper's own repository entrypoint)
    may ONLY be set by plan_execution's own internal, real-audit-gated promotion —
    never accepted from a hand file, however sealed."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "accept test 2", pid="accept2")
    raw = json.dumps({
        "paper_id": pid,
        "command": ["python", "eval.py"],
        "provenance": "repo_exec",
    })
    with pytest.raises(Exception):
        probe_stage.accept_spec(cfg, pid, raw, reviewer="alice", mode="MANUAL")


def test_accept_spec_seals_a_legitimate_script_and_it_is_then_trusted(tmp_path):
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "accept test 3", pid="accept3")
    root = state.project_dir(cfg, pid)
    (root / "runs" / pid).mkdir(parents=True)
    script_path = root / "runs" / pid / "probe.py"
    script_path.write_text("print('SH_METRIC arm=baseline seed=0 value=1.0')", encoding="utf-8")

    raw = json.dumps({
        "paper_id": pid,
        "script": str(script_path),
        "provenance": "driver",
        "claim": "the paper's claim",
    })
    result = probe_stage.accept_spec(cfg, pid, raw, reviewer="alice", tool_policy="", mode="MANUAL")
    assert result["accepted"] is True

    accepted, why = probe_stage.spec_is_accepted(root)
    assert accepted, why

    doc = _bare_doc(pid)
    spec = probe_stage.build_spec(cfg, pid, doc, None)
    assert spec.provenance == "driver"
    assert spec.script == script_path.read_text(encoding="utf-8"), (
        "the sealed spec must carry the script's CONTENT, never the path string — "
        "every other consumer of ProbeSpec.script (local_exec.write_probe, probe_synth) "
        "treats it as literal source text, and storing the path here would also let the "
        "referenced file be edited after sealing with no seal violation at all"
    )
    assert spec.written_by == "driver_accept", (
        "written_by must be stamped by accept_spec itself, never trusted from the input")


def test_container_writing_into_the_mount_cannot_forge_a_seal(tmp_path):
    """REPOSITORY CODE ATTEMPTING TO MUTATE TRUSTED RUN STATE. Even if hostile code
    running inside a container writes directly to control/spec.json (bypassing
    accept_spec entirely — simulating a bug elsewhere, or a future backend that mounts
    control/ by mistake), build_spec must refuse it for lack of a valid sidecar, not
    merely for lacking written_by=='harness'."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "mount forge test", pid="mountforge")
    root = state.project_dir(cfg, pid)
    control = state.control_dir(root)
    control.mkdir(parents=True)

    # No .driver.json sidecar at all — as if a container process wrote this file
    # directly rather than going through accept_spec.
    state.write_json(control / "spec.json", {
        "paper_id": pid, "written_by": "driver_accept", "provenance": "driver",
        "script": "/nonexistent/x.py",
    })

    doc = _bare_doc(pid)
    spec = probe_stage.build_spec(cfg, pid, doc, None)
    assert spec.written_by != "driver_accept" or spec.script != "/nonexistent/x.py", (
        "a spec.json with no valid sidecar must be ignored, whoever's written_by it claims")
