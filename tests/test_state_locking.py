import sys
import threading
import time
from pathlib import Path

import pytest

from harness import state
from harness.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path / "projects")


def test_project_lock_serializes_concurrent_add_cost(tmp_path):
    """20 threads each add $0.01, 50 times, concurrently. Without a lock this is a
    classic lost-update race (load_meta / mutate / save_meta with no serialization);
    with the lock every increment must survive."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "race test", pid="race")

    def worker():
        for _ in range(50):
            state.add_cost(cfg, pid, 0.01)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    meta = state.load_meta(cfg, pid)
    assert meta["cost_usd"] == pytest.approx(20 * 50 * 0.01, abs=1e-6), (
        f"expected no lost updates, got {meta['cost_usd']}")


def test_project_lock_is_reentrant_safe_across_two_processes_worth_of_handles(tmp_path):
    """Two separate `open()` handles on the same lock file (simulating two OS processes,
    since threads in one process would share the GIL and never actually race the file
    lock) must serialize: the second acquire blocks until the first releases."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "lock test", pid="locktest")

    order = []

    def first():
        with state.project_lock(cfg, pid):
            order.append("first-acquired")
            time.sleep(0.2)
            order.append("first-released")

    def second():
        time.sleep(0.05)  # ensure first() acquires first
        with state.project_lock(cfg, pid):
            order.append("second-acquired")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert order == ["first-acquired", "first-released", "second-acquired"], order


def test_add_cost_from_inside_project_lock_does_not_deadlock(tmp_path):
    """`controller.step` holds `project_lock` for a phase handler's whole call. A
    handler that calls `append_log(..., cost_usd=X)` (the real shape of per-call cost
    tracking) triggers `add_cost`, which acquires the SAME case's lock again on the
    SAME thread. The lock is not reentrant at the OS level, so without a fix this second
    acquire blocks forever on a lock this thread already holds. Run it on a background
    thread with a bounded join so a regression hangs the test instead of the suite."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "reentrant test", pid="reentrant")

    def run():
        with state.project_lock(cfg, pid):
            state.append_log(cfg, pid, artifact_type="test", phase="test", cost_usd=1.5)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "project_lock self-deadlocked on a nested add_cost"

    meta = state.load_meta(cfg, pid)
    assert meta["cost_usd"] == pytest.approx(1.5)


def test_project_lock_nested_context_managers_same_thread(tmp_path):
    """Directly nested `with project_lock(...):` blocks on the same thread — the general
    case `add_cost`'s nesting is one instance of — must all succeed without touching the
    OS lock a second time, and the depth counter must unwind back to a clean release."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "nested test", pid="nested")

    with state.project_lock(cfg, pid):
        with state.project_lock(cfg, pid):
            with state.project_lock(cfg, pid):
                state.add_cost(cfg, pid, 2.0)

    assert state._reentrancy.depths[pid] == 0
    meta = state.load_meta(cfg, pid)
    assert meta["cost_usd"] == pytest.approx(2.0)

    # The OS lock was actually released, not merely un-nested: a fresh acquire on a new
    # thread must not block.
    acquired = []

    def probe():
        with state.project_lock(cfg, pid):
            acquired.append(True)

    t = threading.Thread(target=probe)
    t.start()
    t.join(timeout=5)
    assert acquired == [True]


def test_project_lock_reentrancy_is_scoped_to_one_case(tmp_path):
    """Holding case A's lock must not be mistaken for holding case B's: nesting a
    different pid's `project_lock` inside must still take a real (uncontended, so
    harmless) OS lock rather than being skipped by A's depth counter."""
    cfg = _cfg(tmp_path)
    pid_a = state.create_project(cfg, "https://example.com/repo", "a", pid="case-a")
    pid_b = state.create_project(cfg, "https://example.com/repo", "b", pid="case-b")

    with state.project_lock(cfg, pid_a):
        with state.project_lock(cfg, pid_b):
            state.add_cost(cfg, pid_b, 3.0)
        assert state._reentrancy.depths[pid_b] == 0
        assert state._reentrancy.depths.get(pid_a, 0) == 1

    assert state.load_meta(cfg, pid_b)["cost_usd"] == pytest.approx(3.0)


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt-specific behavior")
def test_lock_file_edeadlk_is_indistinguishable_from_ordinary_contention(tmp_path):
    """Documents why `_lock_file`'s `except OSError:` is NOT narrowed to re-raise on
    `EDEADLK` (errno 36): on this CRT, EDEADLK is what `msvcrt.locking(LK_LOCK, ...)`
    raises whenever its own internal ~9s retry budget is exhausted, for ANY conflicting
    holder — a different thread in this same process (exercised here), a different
    process, or a genuine same-thread self-deadlock — with no attribute (`winerror` is
    None) distinguishing the cases. A narrowed except that re-raised on EDEADLK was
    tried and reverted because it turned this ordinary, resolvable contention into a
    spurious permanent failure. Self-deadlock is instead prevented earlier, and
    precisely, by `project_lock`'s own reentrancy depth counter."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "contend", pid="contend")
    lock_path = state.project_dir(cfg, pid) / ".lock"
    lock_path.touch(exist_ok=True)

    results = []

    def holder():
        with open(lock_path, "r+b") as f:
            state._lock_file(f)
            time.sleep(9.5)  # outlast msvcrt's own ~9s internal retry budget
            state._unlock_file(f)

    def contender():
        with open(lock_path, "r+b") as f:
            try:
                state._lock_file(f)  # must wait it out via the outer retry loop, not raise
                state._unlock_file(f)
                results.append("acquired")
            except OSError as e:
                results.append(f"raised: {e}")

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=contender)
    t1.start()
    time.sleep(0.1)
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert results == ["acquired"], (
        f"ordinary same-process contention must be waited out, not raised: {results}")
