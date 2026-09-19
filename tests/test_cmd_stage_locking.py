"""`run.py cmd_stage` — the `python run.py stage <name> --paper <pid>` debug entrypoint —
used to call `STAGES[args.name](Config.load(), args.paper)` directly with no lock at all,
unlike `controller.step`, which wraps every phase handler in
`with state.project_lock(cfg, case.paper_id): ...`. Two unserialized invocations against
the same paper (or one racing a real `review` run) could interleave writes to
`control/probe_results.json`, `control/targets/<id>/outcome.json`, and
`artifact/<pid>.route.json` with no lock — last-writer-wins.

These tests are adversarial in the same shape `tests/test_state_locking.py` already
uses: real threads, a slow handler, and an assertion on the INTERLEAVING order rather
than only on the final state, because a lock bug that merely delays the second caller
(without ever letting them race) would pass a weaker assertion that only checked the end
result.
"""
import argparse
import threading
import time
from pathlib import Path

import pytest

import run
from harness import state
from harness.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(projects_dir=tmp_path / "projects")


def _args(name: str, paper: str) -> argparse.Namespace:
    return argparse.Namespace(name=name, paper=paper)


def test_two_cmd_stage_calls_on_the_same_paper_serialize(tmp_path, monkeypatch):
    """Two `cmd_stage` invocations against the SAME already-ingested case id, from two
    threads, must serialize rather than interleave — the exact scenario `controller.step`
    is already protected against and `cmd_stage` was not."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "lock test", pid="stagelock")
    monkeypatch.setattr(run.Config, "load", classmethod(lambda cls: cfg))

    order = []

    def slow_stage(cfg_, pid_):
        order.append(f"start:{pid_}")
        time.sleep(0.2)
        order.append(f"end:{pid_}")
        return {"ok": True}

    monkeypatch.setitem(run.STAGES, "report", slow_stage)

    def first():
        run.cmd_stage(_args("report", pid))

    def second():
        time.sleep(0.05)  # ensure `first` acquires the lock first
        run.cmd_stage(_args("report", pid))

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    # Serialized: the first call's start AND end must both precede the second call's
    # start. Interleaving (start:pid, start:pid, end:pid, end:pid) is exactly what an
    # unlocked `cmd_stage` used to permit and is the failure this test exists to catch.
    assert order == [f"start:{pid}", f"end:{pid}", f"start:{pid}", f"end:{pid}"], order


def test_cmd_stage_contends_with_a_concurrent_controller_step_lock(tmp_path, monkeypatch):
    """The lock `cmd_stage` takes must be the SAME lock `controller.step` takes for this
    paper id — not a lock of its own that happens to also exist. Holding
    `state.project_lock` directly (simulating a real `review` run in flight) must block a
    concurrent `cmd_stage` call until released."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "cross-lock test", pid="crosslock")
    monkeypatch.setattr(run.Config, "load", classmethod(lambda cls: cfg))

    order = []

    def fast_stage(cfg_, pid_):
        order.append("stage-ran")
        return {"ok": True}

    monkeypatch.setitem(run.STAGES, "grade", fast_stage)

    def holder():
        with state.project_lock(cfg, pid):
            order.append("holder-acquired")
            time.sleep(0.2)
            order.append("holder-released")

    def caller():
        time.sleep(0.05)
        run.cmd_stage(_args("grade", pid))

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=caller)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert order == ["holder-acquired", "holder-released", "stage-ran"], order


def test_ingest_stage_with_a_fresh_pdf_path_locks_on_empty_pid_not_the_raw_path(
    tmp_path, monkeypatch
):
    """`ingest`'s `--paper` is a PDF path, and the eventual paper id is not known until
    ingestion allocates one — the same situation `controller.open_case` represents with an
    empty `case.paper_id`. `cmd_stage` must lock on `""` for this case, exactly matching
    what `controller.step` already locks on for a fresh ingest, and must NOT try to lock on
    the literal PDF path: `state.project_dir` joins a pid under `projects_dir` with
    `Path.__truediv__`, which for an ABSOLUTE path silently discards `projects_dir` and
    resolves to the PDF's own path — so locking on the raw path would make `project_lock`
    try to `mkdir` a directory AT the PDF file's own location, an outright crash
    (`FileExistsError`/`NotADirectoryError`) that has nothing to do with concurrency."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(run.Config, "load", classmethod(lambda cls: cfg))

    pdf_path = tmp_path / "a_fresh_paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    seen_pids = []
    real_lock = state.project_lock

    def spy_lock(cfg_, pid_):
        seen_pids.append(pid_)
        return real_lock(cfg_, pid_)

    monkeypatch.setattr(run.state, "project_lock", spy_lock)
    monkeypatch.setitem(run.STAGES, "ingest",
                        lambda cfg_, paper_: {"cached": False, "paper_id": "a-fresh-paper"})

    run.cmd_stage(_args("ingest", str(pdf_path)))

    assert seen_pids == [""], (
        f"expected the pre-ingest lock key to be '' (matching controller.step's own "
        f"empty case.paper_id before ingestion), got {seen_pids!r}")
    # And the lock must have actually done something real: the shared root-level lock
    # file for a fresh ingest lives at `projects_dir/.lock`, not inside the PDF's own
    # directory and not a directory created AT the PDF's path.
    assert (cfg.projects_dir / ".lock").exists()
    assert pdf_path.is_file(), "the PDF itself must be untouched by locking"


def test_ingest_stage_with_an_already_ingested_case_id_locks_on_that_id(tmp_path, monkeypatch):
    """Re-running the `ingest` stage against an id that was already ingested (the `cached`
    branch of `_phase_ingest`) is the OTHER `ingest` shape: here `--paper` already IS the
    case id, so the lock key must be that id, not `""`."""
    cfg = _cfg(tmp_path)
    pid = state.create_project(cfg, "https://example.com/repo", "already ingested",
                               pid="already-ingested")
    monkeypatch.setattr(run.Config, "load", classmethod(lambda cls: cfg))

    seen_pids = []
    real_lock = state.project_lock

    def spy_lock(cfg_, pid_):
        seen_pids.append(pid_)
        return real_lock(cfg_, pid_)

    monkeypatch.setattr(run.state, "project_lock", spy_lock)
    monkeypatch.setitem(run.STAGES, "ingest",
                        lambda cfg_, paper_: {"cached": True, "paper_id": paper_})

    run.cmd_stage(_args("ingest", pid))

    assert seen_pids == [pid], seen_pids


def test_two_cmd_stage_calls_on_different_papers_do_not_contend(tmp_path, monkeypatch):
    """A lock keyed on the wrong thing (e.g. a single global lock) would serialize even
    UNRELATED papers, silently degrading concurrency for every stage invocation. Two
    different paper ids must run concurrently, not one after the other."""
    cfg = _cfg(tmp_path)
    pid_a = state.create_project(cfg, "https://example.com/repo", "a", pid="paper-a")
    pid_b = state.create_project(cfg, "https://example.com/repo", "b", pid="paper-b")
    monkeypatch.setattr(run.Config, "load", classmethod(lambda cls: cfg))

    barrier = threading.Barrier(2, timeout=5)
    order = []

    def slow_stage(cfg_, pid_):
        order.append(f"start:{pid_}")
        barrier.wait()  # both threads must reach here concurrently, or this times out
        order.append(f"end:{pid_}")
        return {"ok": True}

    monkeypatch.setitem(run.STAGES, "audit", slow_stage)

    def call(pid):
        run.cmd_stage(_args("audit", pid))

    t1 = threading.Thread(target=call, args=(pid_a,))
    t2 = threading.Thread(target=call, args=(pid_b,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()
    assert {f"start:{pid_a}", f"start:{pid_b}"} <= set(order)
    assert {f"end:{pid_a}", f"end:{pid_b}"} <= set(order)
