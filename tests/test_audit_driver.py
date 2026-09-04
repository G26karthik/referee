"""Major #6 — a timed-out auto-audit command must not leave orphans running.

`run_lens` shells out to a configured reviewer (`SH_AUDIT_CMD`) with `shell=True`, so the
process Python can see is the shell, not the reviewer it launches. The old
`subprocess.run(..., timeout=...)` killed only that shell on a timeout; anything the shell
had spawned — the actual reviewer, and anything IT spawned — kept running as an orphan,
free to keep writing to the staged output path after this module had already declared the
attempt failed and moved on, and still running when a retry started a second reviewer over
the same prompt. These tests prove the whole tree dies with the timeout, not just its root.

Every command below is a script FILE on disk, invoked as `<python> <script> ...`, rather
than an inline `-c "..."` string — shell=True on Windows runs through cmd.exe, whose own
quoting rules mangle a command line carrying embedded double quotes, and a script file
sidesteps that entirely.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from harness.audit_driver import AuditDriverError, run_lens
from harness.config import Config


def _cfg(**over) -> Config:
    cfg = Config(allow_auto_audit=True)
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_a_timed_out_command_kills_the_child_it_spawned(tmp_path: Path):
    """The spawner (the direct child of the shell) launches a grandchild that sleeps
    briefly and then writes a marker file. If only the spawner were killed, the
    grandchild would survive long enough to write it; a correct tree-kill prevents that.
    """
    marker = tmp_path / "marker.txt"
    spawner = tmp_path / "spawner.py"
    # `repr()` is what safely embeds an arbitrary Windows path (backslashes and all) into
    # generated Python source — hand-rolled quoting is exactly how a literal `\U` in a
    # temp-directory path (`...\Users\...`) gets misread as the start of a unicode escape.
    child_code = (
        "import time, pathlib\n"
        "time.sleep(1.5)\n"
        f"pathlib.Path({str(marker)!r}).write_text('child ran')\n"
    )
    spawner.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    prompt = tmp_path / "lens.md"
    prompt.write_text("prompt", encoding="utf-8")
    out = tmp_path / "lens.json"

    cfg = _cfg(audit_timeout_s=1,
              audit_cmd=f'"{sys.executable}" "{spawner}" {{prompt}} {{out}}')

    started = time.time()
    with pytest.raises(AuditDriverError, match="timed out"):
        run_lens(cfg, "p", "overclaim", prompt, out)
    # The call itself must return close to the configured timeout, not after the
    # grandchild's own 30s sleep — proof the wait ended at OUR timeout, not its exit.
    assert time.time() - started < 10

    # Give the grandchild the time it would have needed to write the marker if it had
    # survived the kill, then confirm it did not.
    time.sleep(2.5)
    assert not marker.exists(), "the orphaned grandchild kept running past the timeout"


def test_a_command_that_finishes_within_the_timeout_is_unaffected(tmp_path: Path):
    """The tree-kill machinery must not change behaviour for the common case: a command
    that writes valid output and exits before the timeout."""
    prompt = tmp_path / "lens.md"
    prompt.write_text("prompt", encoding="utf-8")
    out = tmp_path / "lens.json"
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import sys\n"
        "body = '{\"lens\":\"overclaim\",\"findings\":[],"
        "\"unasked_question\":\"\",\"notes\":\"\"}'\n"
        "open(sys.argv[1], 'w').write(body)\n",
        encoding="utf-8",
    )
    cfg = _cfg(audit_timeout_s=30, audit_cmd=f'"{sys.executable}" "{writer}" {{out}} {{prompt}}')

    record = run_lens(cfg, "p", "overclaim", prompt, out)
    assert record["findings"] == 0
    assert out.exists()
