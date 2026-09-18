import json
import os
from pathlib import Path

import pytest

from harness import state


def test_write_json_leaves_no_partial_file_on_a_crash_mid_write(tmp_path, monkeypatch):
    """A write that fails after the temp file is fully written, but before the
    atomic replace, must never touch the destination path at all."""
    target = tmp_path / "spec.json"
    target.write_text(json.dumps({"written_by": "harness", "version": 1}), encoding="utf-8")

    real_replace = os.replace

    def _boom(src, dst):
        raise OSError("simulated crash between fsync and replace")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        state.write_json(target, {"written_by": "attacker", "version": 2})
    monkeypatch.setattr(os, "replace", real_replace)

    # The destination is untouched — still the last GOOD write, not truncated and not
    # holding the failed write's bytes.
    assert json.loads(target.read_text(encoding="utf-8")) == {"written_by": "harness", "version": 1}
    # No stray temp file left behind in the same directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name != "spec.json"]
    assert leftovers == [], f"temp file(s) not cleaned up: {leftovers}"


def test_write_json_round_trips_and_creates_parent_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "c.json"
    state.write_json(target, {"x": 1})
    assert state.read_json(target) == {"x": 1}


def test_save_meta_is_atomic_too(tmp_path, monkeypatch):
    """`save_meta` must not have its own direct `write_text` — it has to go through
    the same atomic path as everything else, or fixing `write_json` alone is cosmetic."""
    from harness.config import Config

    cfg = Config(projects_dir=tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "test paper", pid="fixture")

    real_replace = os.replace
    calls = []

    def _spy(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    state.set_phase(cfg, pid, "audit")
    assert any(str(dst).endswith("project.json") for _src, dst in calls), (
        "save_meta must call os.replace on project.json, i.e. go through the atomic "
        "write_json path rather than its own direct write_text")
