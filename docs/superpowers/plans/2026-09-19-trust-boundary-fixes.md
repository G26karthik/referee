# Phase 1: Execution Trust-Boundary Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close four code-verified trust-boundary bugs in the REFEREE execution pipeline (`single-harness`) — forgeable execution provenance, a container mount that lets audited code overwrite trusted state, non-atomic state writes, and no concurrency locking — without touching any of the scientific evidence/grading/taxonomy logic.

**Architecture:** No new abstractions. Extend `harness/state.py` with an atomic `write_json`, a `project_lock` context manager, and a `control_dir()` helper; introduce one new physically-separate `control/` subdirectory per case that no execution backend ever mounts; extend the existing `accept_lens`-style stage/seal pattern (already used for lens files, grades, and the substantive verdict) to `spec.json`, closing the one channel that never got it.

**Tech Stack:** Python 3.13, Pydantic models in `harness/artifacts.py`, pytest, no new third-party dependencies (locking uses stdlib `msvcrt`/`fcntl`, atomic writes use stdlib `tempfile`/`os.replace`).

**Spec:** This plan implements only the 4 bugs confirmed by direct code reading during the 2026-09-19 audit (see project memory `referee_redesign_audit.md` for the full audit). It does **not** implement the broader 19-section REFEREE redesign spec (agent-runtime consolidation, model routing, observability, benchmark infra, run-archive cleanup) — those are separate, later phases, sequenced but not detailed here.

## Global Constraints

- Every existing test must still pass: baseline is **2305 passed, 3 skipped, 0 failed** (full suite, `PYTHONUTF8=1 C:\Users\saita\OneDrive\Desktop\RamanIQ\.venv\Scripts\python.exe -m pytest tests -q`), 18m17s wall time.
- No new third-party dependency. Locking uses stdlib `msvcrt` (win32) / `fcntl` (posix); atomic writes use stdlib `tempfile`/`os.replace`.
- Do not touch `harness/taxonomy.py`, `harness/artifacts.py`'s vocabulary enums, `harness/grading.py`, `harness/provenance.py`'s admissibility rule, or any report-facing wording — this phase is pure trust-boundary plumbing, not a scientific-authority change.
- Every module touched keeps its existing `if __name__ == "__main__":` self-check (per `tests/test_self_checks.py`, which discovers self-checks by walking `harness/**/*.py` with `ast`); if a new module is added it needs one too. No new module is added in this plan — all changes land in existing files.
- `state.write_json`'s public signature (`write_json(path: Path, obj: Any) -> str`) does not change — only its implementation becomes atomic — so none of its ~114 existing call sites need to change for Task 1.
- Windows is the primary dev host (`C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness`, PowerShell/Git-Bash); every new code path must work on `win32` (tests run there) and must not silently no-op there (this is exactly the class of bug the `container.py`/`sandbox.py` "no silent fallback" docstrings warn about elsewhere in this codebase).

---

## File Structure

| File | Change |
|---|---|
| `harness/state.py` | Make `write_json` atomic; add `project_lock()`, `control_dir()`; make `save_meta`/`add_cost` use them |
| `harness/local_exec.py` | Route the one direct `Path.write_text(json.dumps(...))` call through `state.write_json`; strip forgeable fields from a hand-supplied spec override (defense in depth) |
| `harness/stages/probe.py` | Move `spec.json`, `probe_results.json`, `validation.driver.json`, and per-target `spec.json`/`probe_results.json`/`outcome.json` from `runs/<pid>/...` to `state.control_dir(root)/...`; add `accept_spec()`; rewrite `build_spec`'s override logic to require a sealed spec |
| `harness/controller.py` | Wrap `step()`'s body in `state.project_lock(cfg, case.paper_id)` |
| `run.py` | Add an `accept-spec` staged-file branch to `cmd_accept`, mirroring the existing `lens:`/`grade:`/`whole-paper` branches |
| `tests/test_state_atomicity.py` (new) | Task 1 tests |
| `tests/test_state_locking.py` (new) | Task 2 tests |
| `tests/test_control_state_isolation.py` (new) | Task 3 tests, including the container-mount regression |
| `tests/test_spec_sealing.py` (new) | Task 4 tests, including the two adversarial regressions the audit specifically asked for |

---

### Task 1: Atomic state writes

**Files:**
- Modify: `harness/state.py:121-124` (`write_json`), `harness/state.py:75-79` (`save_meta`)
- Modify: `harness/local_exec.py` (find the direct `write_text(json.dumps(...))` call near where `probe_results.json`/execution records are persisted outside of `state.write_json`)
- Test: `tests/test_state_atomicity.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `state.write_json(path: Path, obj: Any) -> str` (same signature, now atomic) — every later task calls this and nothing else to persist JSON.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state_atomicity.py
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

    cfg = Config.load()
    monkeypatch.setattr(cfg, "projects_dir", tmp_path, raising=False)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests/test_state_atomicity.py -v`
Expected: all three FAIL — `write_json` currently calls `path.write_text(...)` directly (no `os.replace`, so monkeypatching it changes nothing and the destination ends up with the NEW, "attacker" content instead of being untouched); `save_meta` calls `.write_text` directly too, so `os.replace` is never called at all.

- [ ] **Step 3: Make `write_json` atomic**

Replace `harness/state.py:121-124`:

```python
def write_json(path: Path, obj: Any) -> str:
    """Write JSON atomically: build the full content, fsync it to a temp file in the
    SAME directory as `path`, then `os.replace` it into place. `os.replace` is atomic
    on both POSIX and Windows when source and destination share a volume, which they
    always do here since the temp file is created next to its destination.

    A process killed at any point before the `os.replace` call leaves `path` exactly
    as it was; a process killed during or after `os.replace` leaves it exactly as the
    new write intended. There is no window in which a reader can observe a truncated
    or partially-written file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2, default=str, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return str(path)
```

Add to the imports at the top of `harness/state.py`:

```python
import os
import tempfile
```

- [ ] **Step 4: Make `save_meta` use it**

Replace `harness/state.py:75-79`:

```python
def save_meta(cfg: Config, pid: str, meta: dict[str, Any]) -> None:
    meta["updated_at"] = now()
    write_json(project_dir(cfg, pid) / "project.json", meta)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests/test_state_atomicity.py -v`
Expected: PASS (all 3).

- [ ] **Step 6: Find and fix the direct `write_text` in `local_exec.py`**

Run: `grep -n "write_text(json" C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness\harness\local_exec.py`

This finds the direct `probe_results.json`-shaped write the 2026-09-19 audit identified around line 1096-1097. Replace that direct `Path(...).write_text(json.dumps(...), encoding="utf-8")` call with `state.write_json(<same path>, <same object>)` (importing `from . import state` at the top of `local_exec.py` if it is not already imported — check first with `grep -n "^from . import\|^from \.\. import\|import state" harness/local_exec.py`).

- [ ] **Step 7: Sweep for any other raw JSON `write_text` in `harness/`**

Run: `grep -rn "write_text(json.dumps" C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness\harness`

Expected after step 6: **zero matches**. If any remain, replace each with `state.write_json` following the same pattern as step 6, and re-run this grep until it is empty. This is not a placeholder — it is the actual completion check for Task 1, since the audit found 114 call sites and only `state.write_json` itself plus this one `local_exec.py` outlier were non-atomic; everything else already calls `state.write_json`, which step 3 just made atomic for free.

- [ ] **Step 8: Run the full test suite**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests -q`
Expected: **2305 passed, 3 skipped, 0 failed** (plus the 3 new tests from this task) — no regression from the baseline recorded in Global Constraints.

- [ ] **Step 9: Commit**

```bash
git add harness/state.py harness/local_exec.py tests/test_state_atomicity.py
git commit -m "fix: make all harness JSON state writes atomic (temp file + fsync + os.replace)

A crash or a killed subprocess mid-write could leave any case-state file
(spec.json, probe_results.json, discovery/targets.json, ledger, etc.)
truncated or corrupt, since state.write_json was a direct path.write_text
with no temp file and no fsync. Fixed once, at the shared write_json
helper every writer already calls.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Locking — one advisory lock per case

**Files:**
- Modify: `harness/state.py` (add `project_lock`)
- Modify: `harness/state.py:88-91` (`add_cost`, currently a read-modify-write race)
- Modify: `harness/controller.py:687-754` (`step`)
- Test: `tests/test_state_locking.py` (new)

**Interfaces:**
- Consumes: `state.project_dir(cfg, pid)` (existing).
- Produces: `state.project_lock(cfg: Config, pid: str)` — a context manager. Every later phase-mutating code path in this codebase should eventually be reached only from inside `controller.step`, which now holds this lock for its whole body; no other task in this plan needs to acquire it directly except `add_cost`, which is called from `state.append_log` outside of `step()` too (audit/grade/probe stages append cost as they go, not just once per phase).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state_locking.py
import threading
import time
from pathlib import Path

import pytest

from harness import state
from harness.config import Config


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.load()
    object.__setattr__(cfg, "projects_dir", tmp_path) if hasattr(cfg, "__dataclass_fields__") else setattr(cfg, "projects_dir", tmp_path)
    return cfg


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests/test_state_locking.py -v`
Expected: FAIL — `state.project_lock` does not exist (`AttributeError`), and `add_cost`'s current implementation has no serialization so the cost test would also lose updates even if a no-op lock existed.

- [ ] **Step 3: Add the lock primitive to `harness/state.py`**

Add near the top of `harness/state.py`, after the existing imports:

```python
import contextlib
import sys

if sys.platform == "win32":
    import msvcrt

    def _lock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```

Add the context manager, after `create_project`:

```python
@contextlib.contextmanager
def project_lock(cfg: Config, pid: str):
    """Exclusive OS-level advisory lock over one case's state.

    Not a lock-file-EXISTS convention — an actual `msvcrt`/`fcntl` lock on an open file
    handle, released automatically by the OS if the holding process dies or is killed,
    so a crash can never leave a case permanently unlockable the way a stale PID-file
    lock could. Every phase transition (`controller.step`) holds this for its whole
    body, so two invocations of the harness racing on the same paper id serialize
    instead of interleaving writes to `project.json`, `controller.json`,
    `discovery/targets.json`, or any other case-state file.
    """
    root = project_dir(cfg, pid)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock"
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+b") as f:
        _lock_file(f)
        try:
            yield
        finally:
            _unlock_file(f)
```

- [ ] **Step 4: Make `add_cost` use it**

Replace `harness/state.py:88-91`:

```python
def add_cost(cfg: Config, pid: str, cost_usd: float) -> None:
    with project_lock(cfg, pid):
        meta = load_meta(cfg, pid)
        meta["cost_usd"] = round(float(meta.get("cost_usd", 0.0)) + float(cost_usd or 0.0), 6)
        save_meta(cfg, pid, meta)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests/test_state_locking.py -v`
Expected: PASS (both).

- [ ] **Step 6: Wrap `controller.step` in the lock**

In `harness/controller.py`, `step()` (line 687), wrap the whole body (from `if case.terminal:` through the final `return save_case(cfg, case)`, i.e. lines ~694-754) in `with state.project_lock(cfg, case.paper_id):`. Concretely, change:

```python
def step(cfg: Config, case: CaseState, **opts) -> CaseState:
    """Advance one phase. ...
    """
    if case.terminal:
        return case
    ...
    return save_case(cfg, case)
```

to:

```python
def step(cfg: Config, case: CaseState, **opts) -> CaseState:
    """Advance one phase. Records exactly one `PhaseEvent`, whatever happens.

    Idempotent against a terminal case and safe to call on a `waiting` one — a waiting
    case re-attempts its phase, which is how a run resumes once the evidence it was
    waiting for has arrived.

    The whole body runs under `state.project_lock` — a phase handler reads and writes
    several case-state files (spec.json, targets.json, controller.json, ...), and
    without serialization two concurrent invocations of the same paper id could
    interleave those writes.
    """
    if case.terminal:
        return case
    with state.project_lock(cfg, case.paper_id):
        if case.phase == "done":
            case.status = "complete"
            return save_case(cfg, case)

        handler = _HANDLERS[case.phase]
        attempt = case.attempts.get(case.phase, 0) + 1
        case.attempts[case.phase] = attempt
        case.status = "running"

        try:
            out = handler(cfg, case, **opts)
        except Exception as e:                       # noqa: BLE001 — a handler fault is a case error, not a crash
            out = PhaseOutcome("error", f"{type(e).__name__}: {e}")

        case.history.append(PhaseEvent(
            phase=case.phase, outcome=out.outcome, reason=out.reason,
            detail=out.detail or {}, attempt=attempt,
            ts=state.now()))
        if out.reproduction_class:
            case.reproduction_class = out.reproduction_class

        if out.outcome == "error":
            case.status, case.blocked_reason = "error", out.reason
        elif out.outcome == "waiting":
            case.status, case.blocked_reason = "waiting", out.reason
        elif out.outcome == "retry":
            case.status, case.blocked_reason = "running", out.reason
            if case.phase not in RETRYABLE:
                case.status, case.blocked_reason = "waiting", (
                    f"{case.phase} asked to retry, but only {', '.join(RETRYABLE)} may be "
                    f"re-attempted: {out.reason}")
        else:                                        # ok | abstain
            case.blocked_reason = ""
            case.phase = _next_phase(case.phase)
            if case.phase == "done":
                missing = audit_stage.missing_reading_artifacts(cfg, case.paper_id)
                if missing:
                    case.phase, case.status = "audit", "waiting"
                    case.blocked_reason = (
                        f"{len(missing)} required reading artifact(s) are missing, so this "
                        f"review cannot be completed as the one it describes: "
                        f"{', '.join(missing[:8])}"
                        + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""))
                    case.history.append(PhaseEvent(
                        phase="done", outcome="waiting", reason=case.blocked_reason,
                        detail={"missing": missing}, attempt=attempt, ts=state.now()))
                    return save_case(cfg, case)
                case.status = "complete"

        return save_case(cfg, case)
```

(Only the indentation of the existing body changed, plus the new `with` line and docstring addition — no logic changed.)

- [ ] **Step 7: Run the full test suite**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests -q`
Expected: **2305 passed, 3 skipped, 0 failed** (plus 2 new). If `test_controller*.py` tests call `step` directly with a mocked `cfg` lacking `projects_dir`, verify they still pass — `project_lock` creates the directory itself (`root.mkdir(parents=True, exist_ok=True)`) so this should be transparent.

- [ ] **Step 8: Commit**

```bash
git add harness/state.py harness/controller.py tests/test_state_locking.py
git commit -m "fix: add an OS-level advisory lock per case, held for every phase step

harness/ had no locking of any kind: two concurrent invocations of the
same paper id could interleave writes to project.json, controller.json,
spec.json, discovery/targets.json etc. controller.step now holds
state.project_lock for its whole body, and add_cost (a read-modify-write
on project.json) uses it directly since cost is appended from several
call sites outside of step().

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Separate trusted control state from the bind-mounted execution workspace

**Files:**
- Modify: `harness/state.py` (add `control_dir`)
- Modify: `harness/stages/probe.py` (move `probe_results.json`, `validation.driver.json`, and per-target `probe_results.json`/`outcome.json` off `runs/<pid>/...`)
- Test: `tests/test_control_state_isolation.py` (new)

**Interfaces:**
- Consumes: `state.control_dir(root: Path) -> Path` (new).
- Produces: every read/write of `probe_results.json`, `validation.driver.json`, and per-target `probe_results.json`/`outcome.json` now goes through `state.control_dir(root) / ...` instead of `root / "runs" / pid / ...`. `spec.json` is handled in Task 4 (it needs sealing logic, not just a path move, so moving it here would be redone).

**Why `sandbox.json` is explicitly left alone:** the audit confirmed `SandboxBackend` does not bind-mount anything — `sandbox.json` already lives beside, not inside, the checkout, and is never reachable from inside a leased sandbox. Moving it would be file-churn with no security benefit, so this task does not touch `harness/sandbox.py`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests/test_control_state_isolation.py -v`
Expected: FAIL — `state.control_dir` does not exist yet (`AttributeError`).

- [ ] **Step 3: Add `control_dir` to `harness/state.py`**

Add after `project_dir`:

```python
def control_dir(root: Path) -> Path:
    """The trusted control-state directory for one case: `<project_root>/control/`.

    Structurally separate from `runs/<pid>/`, which IS bind-mounted — in whole, by
    `ContainerBackend` — into every execution that runs a paper's own (or a driver's,
    or a reconstruction's) code. A file under `control/` must never be reachable from
    inside a container or a subprocess running untrusted code; that separation is what
    makes it safe for a LATER harness invocation to read a file here back as
    pre-verified state, rather than as something the code it just ran could have
    overwritten.
    """
    return Path(root) / "control"
```

- [ ] **Step 4: Move `probe_results.json` in `harness/stages/probe.py`**

Run: `grep -n '"runs" / pid / "probe_results.json"' C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness\harness\stages\probe.py`

This finds 4 occurrences (lines ~390, 710, 1457, 1793 per the 2026-09-19 audit — re-confirm exact line numbers with the grep above, since earlier edits in this plan may have shifted them). Replace every one of `root / "runs" / pid / "probe_results.json"` with `state.control_dir(root) / "probe_results.json"`.

- [ ] **Step 5: Move `validation.driver.json`**

Run: `grep -n '"validation.driver.json"' C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness\harness\stages\probe.py`

Replace `root / "runs" / pid / "validation.driver.json"` with `state.control_dir(root) / "validation.driver.json"`.

- [ ] **Step 6: Move per-target `outcome.json` and `probe_results.json`**

Run: `grep -n '"targets" / .*"outcome.json"\|"targets" / .*"probe_results.json"' C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness\harness\stages\probe.py`

For every occurrence of `root / "runs" / pid / "targets" / <target_id_expr> / "outcome.json"` or `.../ "probe_results.json"`, replace the `root / "runs" / pid / "targets"` prefix with `state.control_dir(root) / "targets"`. Do **not** change any occurrence of `root / "runs" / pid / "targets" / <id> / "spec.json"` in this step — that one is handled in Task 4 alongside the primary `spec.json`, since it needs the same sealing treatment, not just a path move. Do **not** change `root / "runs" / pid / "reimplementation"` (the reconstruction script output directory) or `root / "runs" / pid / "targets" / <id> / "reimplementation"` — those are genuinely disposable execution workspace (the container legitimately needs to write there), not control state.

- [ ] **Step 7: Run the full test suite**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests -q`
Expected: some existing tests will fail here if they assert exact paths like `runs/<pid>/probe_results.json` (e.g. `tests/test_local_execution.py`, `tests/test_sandbox_backend.py`, `tests/test_probe_stage.py` or similar). For each failure:
  - Read the failing assertion.
  - If it asserts a literal path containing `runs" / pid / "probe_results.json"` (or `validation.driver.json`, or `targets/<id>/outcome.json` / `probe_results.json`), update the assertion to `control_dir(root) / "probe_results.json"` etc. — this is the expected, intended invariant change, not a regression to work around.
  - If it fails for any other reason, stop and investigate before changing the test — per this plan's Global Constraints, only the path relocation is in scope here.

- [ ] **Step 8: Run the new isolation test again**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests/test_control_state_isolation.py -v`
Expected: PASS (both).

- [ ] **Step 9: Commit**

```bash
git add harness/state.py harness/stages/probe.py tests/test_control_state_isolation.py
git commit -m "fix: move control-state JSON out of the container's bind-mounted directory

ContainerBackend bind-mounts runs/<pid>/ (parent of the checkout) with no
:ro, so a paper's own code running inside the container could overwrite
probe_results.json / validation.driver.json / per-target outcome.json,
which a LATER invocation reads back as trusted. These now live in a new
sibling control/ directory that no backend ever mounts. spec.json is
handled in the next commit since it also needs a real seal, not just a
path move.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Real acceptance/sealing for `spec.json` ("driver" provenance)

**Files:**
- Modify: `harness/stages/probe.py` (`build_spec`, `plan_execution`'s callers, `resync_cached_outcomes`); add `accept_spec`
- Modify: `run.py` (`cmd_accept`)
- Test: `tests/test_spec_sealing.py` (new) — includes the two adversarial regressions the audit specifically flagged

**Interfaces:**
- Consumes: `state.control_dir(root)` (Task 3), `delegation.provenance_record(mode, reviewer, tool_policy) -> dict` and `delegation.WRITTEN_BY: dict[str,str]` (existing, unchanged).
- Produces: `stages.probe.accept_spec(cfg: Config, pid: str, raw: str, *, reviewer: str = "", tool_policy: str = "", mode: str = "MANUAL") -> dict` — mirrors `stages.audit.accept_lens`'s contract (validate, strip, seal, sidecar, return a small summary dict). `stages.probe.spec_is_accepted(root: Path) -> tuple[bool, str]` — mirrors `stages.audit.lens_is_accepted`.

**The exact bug this closes** (verified by reading `stages/probe.py:build_spec` and `plan_execution`, and `backends.authorize`, on 2026-09-19): a hand-authored `runs/<pid>/spec.json` is accepted as-is by `build_spec` as long as `written_by != "harness"` — there is no allowlist and no seal step, unlike every other reviewer-facing artifact in this codebase (lens files, grades, the substantive verdict all go through a `_ACCEPTED_WRITERS` gate populated only by a real `accept_*` function). If the paper's own entrypoint is not found by the static audit, or acquisition does not reach `cloned`/`cached` status, `plan_execution` returns the spec **unmodified** (`stages/probe.py`, the early-return guard before the identity/capability/resources assessment) — so a spec.json that sets `provenance: "repo_exec"`, a fabricated `command`, and hand-set `experiment.established: true` / `capability.established: true` / `resources.established: true` blocks reaches `backends.authorize` with those claims intact, and only the commit is freshly re-verified (everything else is trusted from the file). A hostile paper repository that reaches this file at all (via the Task 3 mount hole, before that fix, or via any other write path) could therefore manufacture a FAILED_REPRODUCTION with `repo_exec`-strength provenance.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_spec_sealing.py
import json
from pathlib import Path

import pytest

from harness import state
from harness.config import Config
from harness.stages import probe as probe_stage
from harness.artifacts import PaperDoc


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.load()
    cfg.projects_dir = tmp_path
    return cfg


def _bare_doc(pid: str) -> PaperDoc:
    return PaperDoc(paper_id=pid, title="t", abstract="", tables=[], reported_numbers=[])


def test_a_raw_unsealed_spec_json_is_never_trusted(tmp_path):
    """FORGED TRUSTED DRIVER METADATA. An attacker (or a paper's own code, via the
    Task-3 mount hole) drops a spec.json claiming provenance=repo_exec and
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
    assert spec.script == str(script_path)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests/test_spec_sealing.py -v`
Expected: FAIL — `probe_stage.accept_spec` and `probe_stage.spec_is_accepted` do not exist yet; `build_spec` currently trusts the forged file.

- [ ] **Step 3: Read the current sealing pattern to mirror**

Read `harness/stages/audit.py` lines 380-500 (`_ACCEPTED_WRITERS`, `lens_is_accepted`, `unit_is_accepted`, `accept_lens`) in full before writing this step — the new `spec_is_accepted`/`accept_spec` must follow the identical shape: a content-hash sidecar (`<name>.driver.json` with `content_sha256`), `written_by` checked against an allowlist built from `delegation.WRITTEN_BY.values()`, and `delegation.provenance_record(mode=mode, reviewer=reviewer, tool_policy=tool_policy)` supplying every provenance field (never trusted from the raw input).

- [ ] **Step 4: Add `accept_spec` and `spec_is_accepted` to `harness/stages/probe.py`**

Add near `build_spec` (imports needed: `hashlib`, `from .. import delegation`, `from ..artifacts import ProbeSpec` already imported):

```python
import hashlib

from .. import delegation

# Fields a hand-authored, unsealed spec proposal may ever supply. Everything else —
# every `.established` identity/capability/resources block, `commit`, `commit_state`,
# and `provenance` itself — is stripped before the proposal is even considered, and is
# recomputed or reassigned by this harness alone. This is the allowlist that makes
# forging trusted provenance by writing a plausible-looking JSON file impossible: no
# field this list omits can ever reach `backends.authorize` from a hand file, sealed or
# not.
_SPEC_PROPOSAL_ALLOWED_FIELDS = frozenset({
    "paper_id", "finding_id", "claim", "claimed_delta", "metric", "arms", "seeds",
    "dataset", "epochs", "script", "table_ref", "claim_ref", "claim_kind",
    "claimed_cell_value", "target_id", "mechanism", "rationale", "aux_metrics",
})

_SPEC_ACCEPTED_WRITERS = tuple(delegation.WRITTEN_BY.values()) + ("harness",)


def _strip_to_allowed_fields(data: dict) -> dict:
    return {k: v for k, v in (data or {}).items() if k in _SPEC_PROPOSAL_ALLOWED_FIELDS}


def spec_is_accepted(root: Path) -> tuple[bool, str]:
    """Does `control/spec.json` carry a valid seal — the same discipline
    `stages.audit.lens_is_accepted` applies to lens files, on this channel.

    A spec with no sidecar, a sidecar whose `written_by` is not a real accept-path
    token, or a sidecar whose recorded hash does not match the file's current bytes is
    NOT accepted — refused outright, never partially trusted.
    """
    control = state.control_dir(root)
    path = control / "spec.json"
    sidecar = control / "spec.driver.json"
    if not path.exists() or not sidecar.exists():
        return False, "no sealed spec.json (or no sidecar) for this case"
    try:
        rec = state.read_json(sidecar)
    except (OSError, ValueError):
        return False, "spec.driver.json is not valid JSON"
    if not isinstance(rec, dict) or rec.get("written_by") not in _SPEC_ACCEPTED_WRITERS:
        return False, (f"provenance sidecar written_by="
                       f"{rec.get('written_by') if isinstance(rec, dict) else None!r} not recognized")
    want = rec.get("content_sha256")
    if not want:
        return False, "provenance sidecar has no content_sha256"
    if hashlib.sha256(path.read_bytes()).hexdigest() != want:
        return False, "spec.json content changed after its provenance sidecar was written"
    return True, ""


def accept_spec(cfg: Config, pid: str, raw: str, *, reviewer: str = "",
                tool_policy: str = "", mode: str = "MANUAL") -> dict:
    """Validate and seal a hand-authored `spec.json` proposal — the "driver" provenance
    path — through the same explicit accept discipline `stages.audit.accept_lens`
    already applies to lens files. Reads from `control/.staged/spec.json`.

    Every field NOT in `_SPEC_PROPOSAL_ALLOWED_FIELDS` is stripped before this function
    does anything else with the proposal: `provenance`, `command`, every `.established`
    identity/capability/resources block, `commit`, and `commit_state` can never be
    supplied by the proposal, whatever it claims — they are always assigned here or, for
    `repo_exec`, exclusively by `plan_execution`'s own real-audit-gated promotion.

    A `command` in the raw proposal REFUSES outright: `driver` provenance is a hand
    -written script (a faithful reproduction this harness could not derive on its own),
    never the paper's own repository entrypoint, which may only ever be attributed by
    `plan_execution` after a real clone, a real static audit, and a real identity match.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("spec proposal must be a JSON object")
    if data.get("command"):
        raise ValueError(
            "a spec proposal may not set 'command' — that is the authors' own "
            "repository entrypoint and may only be attributed by this harness's own "
            "plan_execution, never accepted from a hand file")
    proposal = _strip_to_allowed_fields(data)
    proposal["paper_id"] = pid
    script = (proposal.get("script") or "").strip()
    if script and not Path(script).is_file():
        raise ValueError(f"script {script!r} does not exist; a driver spec must point "
                         f"at a real, readable file")
    proposal["provenance"] = "driver"
    spec = ProbeSpec(**proposal)

    root = state.project_dir(cfg, pid)
    control = state.control_dir(root)
    out = control / "spec.json"
    state.write_json(out, spec.model_dump())
    content_sha256 = hashlib.sha256(out.read_bytes()).hexdigest()
    record = {
        "paper_id": pid,
        **delegation.provenance_record(mode=mode, reviewer=reviewer, tool_policy=tool_policy),
        "content_sha256": content_sha256,
        "ts": state.now(),
    }
    # `written_by` in the sidecar is ALWAYS `delegation.provenance_record`'s own output,
    # never the raw proposal's — the whole point of this function existing.
    record["written_by"] = "driver_accept"
    state.write_json(control / "spec.driver.json", record)
    return {"accepted": True, "paper_id": pid, "content_sha256": content_sha256}
```

- [ ] **Step 5: Rewrite `build_spec`'s override logic in `harness/stages/probe.py`**

Replace the override-reading block (originally lines 110-169, now shifted by Task 3's edits — locate it by its content, not its line number):

```python
    root = state.project_dir(cfg, pid)
    override = root / "runs" / pid / "spec.json"
```

through:

```python
    override_data = state.read_json(override) if override.exists() else None
    if override_data is not None and override_data.get("written_by") != "harness":
        spec = ProbeSpec(**{**override_data, "paper_id": pid})
        if spec.provenance == "template" and (spec.script or spec.command):
            spec.provenance = "driver"
    else:
        spec = ProbeSpec(
            paper_id=pid, written_by="harness",
            finding_id=(target_finding_id or
                        (finding_target.finding_id if finding_target and target is None else "")),
            claim=((getattr(target, "claim_text", "") or
                    getattr(getattr(target, "ref", None), "quote", ""))
                   if target is not None else
                   ((finding_target.target or finding_target.statement)
                    if finding_target else "")),
            seeds=list(range(max(3, min(cfg.seeds, 5)))),
        )
```

with:

```python
    root = state.project_dir(cfg, pid)
    control = state.control_dir(root)
    accepted, _why = spec_is_accepted(root)
    if accepted:
        # A SEALED driver spec: content-hash verified against its sidecar, written_by
        # stamped by accept_spec itself. Only the allowlisted fields it could ever have
        # supplied are trusted; provenance is always "driver" (accept_spec never seals
        # anything else) and every identity/capability/resources/commit field starts
        # unestablished here, exactly as a freshly-built spec's would, so
        # plan_execution's own real assessment is what fills them in, never this file.
        sealed = state.read_json(control / "spec.json")
        proposal = _strip_to_allowed_fields(sealed)
        proposal["paper_id"] = pid
        spec = ProbeSpec(**proposal)
        spec.provenance = "driver"
        spec.written_by = "driver_accept"
    else:
        # No sealed proposal for this case (nothing staged, or staged but not yet
        # accept_spec'd, or a control/spec.json that failed its own hash/sidecar check
        # — see spec_is_accepted). The harness's own auto-built spec is the only other
        # source; a hand file that skipped the explicit accept step is never partially
        # trusted.
        spec = ProbeSpec(
            paper_id=pid, written_by="harness",
            finding_id=(target_finding_id or
                        (finding_target.finding_id if finding_target and target is None else "")),
            claim=((getattr(target, "claim_text", "") or
                    getattr(getattr(target, "ref", None), "quote", ""))
                   if target is not None else
                   ((finding_target.target or finding_target.statement)
                    if finding_target else "")),
            seeds=list(range(max(3, min(cfg.seeds, 5)))),
        )
```

Note: this removes the `override`/`override_data` variables entirely — search the rest of `build_spec` and `_review` for any other reference to `override` or to `root / "runs" / pid / "spec.json"` as a READ (not the harness's own persistence WRITE, which Task 3 already left alone pending this task) and remove/update them to use `spec_is_accepted`/`control` instead.

- [ ] **Step 6: Move the harness's own `spec.json` write to `control/`**

Find `state.write_json(root / "runs" / pid / "spec.json", spec.model_dump())` (the harness's own persistence at the end of `_review`, originally line 1379) and change it to:

```python
    state.write_json(state.control_dir(root) / "spec.json", spec.model_dump())
```

This write only ever fires for the `written_by == "harness"` branch's output in practice (a sealed `driver_accept` spec is not re-persisted by this line — verify this by reading the surrounding ~20 lines before making the change, and if the sealed spec IS re-written here, guard it: `if spec.written_by == "harness": state.write_json(...)` so a re-run never overwrites a human's sealed proposal with the harness's own regenerated one).

- [ ] **Step 7: Update `resync_cached_outcomes`'s references**

`resync_cached_outcomes` (in `harness/stages/probe.py`) reads `root / "runs" / pid / "spec.json"` at what was originally line 770-771 to check for a stale author-code spec during re-planning. Update it to `state.control_dir(root) / "spec.json"` (for the primary target) — the per-target branch at the same lines (`root / "runs" / pid / "targets" / obj.target_id / "spec.json"`) should similarly move to `state.control_dir(root) / "targets" / obj.target_id / "spec.json"`, and wherever that per-target spec is WRITTEN (originally around line 1267/1579-1624) should move the same way, mirroring Task 3's treatment of the per-target `outcome.json`/`probe_results.json`.

- [ ] **Step 8: Add the `accept-spec` branch to `run.py cmd_accept`**

In `run.py`, inside `cmd_accept` (starting at line 160), add a new staged-file branch alongside the existing `lens:`/`grade:`/`whole-paper` ones — right after the `whole-paper` block (around line 224), before the `from harness import delegation` summary block:

```python
    # A driver-supplied faithful reproduction: one spec, one staged file.
    if (src := root / "control" / ".staged" / "spec.json").exists():
        take(src, "spec",
             lambda raw: (
                 f"sealed, sha256={probe_stage.accept_spec(cfg, args.paper, raw, reviewer=args.reviewer, tool_policy=args.tool_policy, mode=args.mode)['content_sha256'][:12]}"
             ))
```

(`probe_stage` is `harness.stages.probe` — check the top of `run.py` for its existing import alias, e.g. `from harness.stages import probe as probe_stage`, and add it if not already present, following the same pattern as `audit_stage`/`grade_stage`.)

- [ ] **Step 9: Run all four test files and the adversarial ones specifically**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests/test_spec_sealing.py tests/test_control_state_isolation.py tests/test_state_locking.py tests/test_state_atomicity.py -v`
Expected: PASS on all.

- [ ] **Step 10: Run the full test suite**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests -q`
Expected: **2305 passed, 3 skipped, 0 failed**, plus the new tests from Tasks 1-4. Investigate and fix (not skip) any test that asserted the OLD, insecure override behavior (`written_by != "harness"` alone being sufficient) — per Global Constraints, that expectation was the bug, and this plan's job is to change it; document in the commit message which test(s) changed and why the old expectation was wrong.

- [ ] **Step 11: Run the no-network subset as a second confirmation**

Run: `cd C:\Users\saita\OneDrive\Desktop\RamanIQ\single-harness && PYTHONUTF8=1 ../.venv/Scripts/python.exe -m pytest tests -q -m "not network"`
Expected: **2301 passed, 7 deselected, 0 failed**, plus new tests.

- [ ] **Step 12: Commit**

```bash
git add harness/stages/probe.py run.py tests/test_spec_sealing.py
git commit -m "fix: require an explicit accept_spec seal before spec.json is ever trusted

A hand-authored spec.json was trusted whenever written_by != 'harness',
with no allowlist and no seal — unlike every other reviewer-facing
artifact (lens files, grades, the substantive verdict), which all go
through a real accept_* function. A spec claiming provenance=repo_exec
with fabricated established=true identity/capability/resources blocks
reached backends.authorize unmodified whenever plan_execution's own
early-return guard fired (repo not cloned, or no entrypoint found) —
only the commit was freshly re-verified. accept_spec now mirrors
stages.audit.accept_lens: a content-hash sidecar, a written_by stamped
by the accept function itself (never trusted from the input), and an
allowlist that strips every identity/capability/resources/commit/
provenance/command field from the raw proposal before anything else
touches it. run.py accept-spec is the CLI entry point.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review (performed before handing this plan off)

**Spec coverage against the 4 confirmed bugs:**
1. Spec.json provenance forgery → Task 4 (allowlist + real seal + adversarial tests).
2. Container RW mount escalation → Task 3 (control/ separation + mount-content regression test).
3. Zero atomic writes → Task 1.
4. Zero locking → Task 2.

**Explicitly deferred, not silently dropped:** quote-vs-value grounding for focused validation, and the control/treatment effective-invocation-identity binding, are real items from the original 19-section redesign spec but were **not** part of the 4 bugs this audit actually verified by reading code — auditing `harness/validation.py`/`harness/between_arms.py` in the same depth as Tasks 1-4 is its own phase, sequenced after this one (see project memory `referee_redesign_audit.md`). Run-archive cleanup, the agent-runtime consolidation, model routing, and observability are separate later phases per the same memory.

**Placeholder scan:** every step above has concrete code, exact grep commands, exact file paths, and exact expected outputs (pass/fail counts) — no "add appropriate error handling" language.

**Type consistency:** `state.control_dir(root: Path) -> Path` is defined once (Task 3, Step 3) and used with the identical signature in Tasks 3 and 4. `probe_stage.accept_spec` / `spec_is_accepted` signatures introduced in Task 4 match their usage in the new `run.py` branch and in `tests/test_spec_sealing.py`.
