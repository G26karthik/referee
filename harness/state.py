"""Project state on the local filesystem: one directory per project, an
append-only research log, and typed artifact files. This is the shared memory
the Director and every stage read; it stores ARTIFACTS, never transcripts.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config

if sys.platform == "win32":
    import msvcrt
    import time as _time

    def _lock_file(f) -> None:
        # msvcrt.locking's LK_LOCK only retries internally for ~10s before raising
        # OSError (errno 36 / EDEADLK, "Resource deadlock avoided") — it does NOT block
        # indefinitely the way POSIX fcntl.flock(LOCK_EX) does. Retry-wrap it so a
        # longer-held lock (a slow phase handler) is waited out rather than crashing
        # the waiter, matching fcntl.flock's blocking contract.
        #
        # EDEADLK is NOT a reliable signal of a genuine same-thread self-deadlock on
        # this platform: msvcrt raises the identical errno 36 once its own internal
        # retry budget (~10 attempts) is exhausted, whether the conflicting lock is
        # held by another thread in this same process, another process, or (the real
        # bug this would otherwise indicate) this same thread. Measured directly against
        # this CRT: two threads in the same process legitimately contending for this
        # lock for longer than ~9s reproduce errno 36 with no distinguishing attribute
        # (`winerror` is None) from a true self-deadlock — so narrowing this except to
        # re-raise on EDEADLK was tried and reverted; it turned ordinary contention
        # (`test_project_lock_serializes_concurrent_add_cost`) into a spurious failure.
        # The real fix for self-deadlock is reentrancy in `project_lock` itself, below,
        # which never reaches this function a second time on the same thread/case.
        while True:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                _time.sleep(0.05)

    def _unlock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

SUBDIRS = [
    "paper",     # S1 — the ingested PaperDoc
    "audit",     # S2 — prompts in, one lens report per lens out
    "runs",      # S3 — checkout, probe source, probe results
    "reports",   # S4 — the rendered evaluation report
]


def now() -> str:
    """The one UTC timestamp in this harness.

    Nine sites formatted this string independently, two of them as identically
    bodied private functions in different modules. One format, one source of time.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(s: str, n: int = 28) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:n] or "project").strip("-")


def project_dir(cfg: Config, pid: str) -> Path:
    return cfg.projects_dir / pid


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


def new_project_id(direction: str) -> str:
    date = time.strftime("%Y%m%d", time.gmtime())
    return f"{date}-{slugify(direction, 24)}-{uuid.uuid4().hex[:6]}"


def create_project(cfg: Config, repo_url: str, direction: str, pid: str | None = None) -> str:
    """Create one case directory. `pid` is explicit for the reviewer pipeline, where a
    case is identified by its paper file rather than by a date-stamped slug."""
    pid = pid or new_project_id(direction)
    root = project_dir(cfg, pid)
    root.mkdir(parents=True, exist_ok=False)
    for d in SUBDIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    meta = {
        "id": pid,
        "repo_url": repo_url,
        "direction": direction,
        "phase": "created",
        "status": "active",
        "created_at": now(),
        "updated_at": now(),
        "cost_usd": 0.0,
    }
    save_meta(cfg, pid, meta)
    (root / "research_log.jsonl").touch()
    return pid


# Per-thread re-entrancy depth, keyed by paper id. `project_lock` is held for the
# whole body of `controller.step`, and a phase handler running inside that body is
# entitled to call `add_cost`/`append_log` itself — that is the normal shape of future
# per-call cost tracking, not a caller bug. The OS-level lock below is not reentrant
# (a second acquire by the same thread would block on a lock it already holds — forever
# on POSIX `fcntl.flock`, and on Windows until `EDEADLK` is raised), so a thread that
# already holds this case's lock must skip re-acquiring it rather than deadlock itself.
# `threading.local` scopes the counter to this thread alone: a DIFFERENT thread or
# process must still contend for the real OS lock, exactly as before.
_reentrancy = threading.local()


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

    Reentrant per THREAD per CASE: a call already holding this case's lock on this
    thread (e.g. a phase handler calling `add_cost` from inside `controller.step`)
    re-enters without touching the OS lock. A different thread, a different process, or
    the same thread on a DIFFERENT case still takes the real lock and blocks normally.
    """
    depths = getattr(_reentrancy, "depths", None)
    if depths is None:
        depths = _reentrancy.depths = {}
    if depths.get(pid, 0) > 0:
        depths[pid] += 1
        try:
            yield
        finally:
            depths[pid] -= 1
        return

    root = project_dir(cfg, pid)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock"
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+b") as f:
        _lock_file(f)
        depths[pid] = 1
        try:
            yield
        finally:
            depths[pid] -= 1
            _unlock_file(f)


def load_meta(cfg: Config, pid: str) -> dict[str, Any]:
    return json.loads((project_dir(cfg, pid) / "project.json").read_text(encoding="utf-8"))


def save_meta(cfg: Config, pid: str, meta: dict[str, Any]) -> None:
    meta["updated_at"] = now()
    write_json(project_dir(cfg, pid) / "project.json", meta)


def set_phase(cfg: Config, pid: str, phase: str) -> None:
    meta = load_meta(cfg, pid)
    meta["phase"] = phase
    save_meta(cfg, pid, meta)


def add_cost(cfg: Config, pid: str, cost_usd: float) -> None:
    with project_lock(cfg, pid):
        meta = load_meta(cfg, pid)
        meta["cost_usd"] = round(float(meta.get("cost_usd", 0.0)) + float(cost_usd or 0.0), 6)
        save_meta(cfg, pid, meta)


# In-process only: several lens/grade calls now run concurrently on threads within one
# controller invocation, and a plain `open(..., "a").write(...)` from two threads at once
# can interleave two records into one unparseable line. A per-process lock is enough —
# cross-process writers still serialize through `project_lock`'s OS-level lock, which every
# writer of this file already holds for the duration of its phase.
_log_lock = threading.Lock()


def append_log(
    cfg: Config,
    pid: str,
    *,
    artifact_type: str,
    phase: str,
    headers: dict[str, Any] | None = None,
    path: str | None = None,
    cost_usd: float = 0.0,
) -> None:
    """Append one artifact record to research_log.jsonl (the Director reads headers)."""
    rec = {
        "ts": now(),
        "type": artifact_type,
        "phase": phase,
        "headers": headers or {},
        "path": path,
        "cost_usd": round(float(cost_usd or 0.0), 6),
    }
    with _log_lock:
        with (project_dir(cfg, pid) / "research_log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if cost_usd:
        add_cost(cfg, pid, cost_usd)


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


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def list_projects(cfg: Config) -> list[dict[str, Any]]:
    out = []
    if not cfg.projects_dir.exists():
        return out
    for d in sorted(cfg.projects_dir.iterdir()):
        meta = d / "project.json"
        if meta.exists():
            out.append(json.loads(meta.read_text(encoding="utf-8")))
    return out
