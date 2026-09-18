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
