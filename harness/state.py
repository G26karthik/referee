"""Configuration, project paths, JSON on disk and the project lock.

ponytail: one host, one operator, a few papers per run. State is plain files under
`projects/<pid>/`; a second host would need a real store and a distributed lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    projects: Path = field(default_factory=lambda: Path(os.environ.get("SH_PROJECTS_DIR") or ROOT / "projects"))
    python: str = field(default_factory=lambda: os.environ.get("SH_PYTHON") or sys.executable)
    # Execution gates: each is an explicit per-invocation opt-in (invariant 4).
    allow_repo_exec: bool = field(default_factory=lambda: _flag("SH_ALLOW_REPO_EXEC"))
    allow_script_exec: bool = field(default_factory=lambda: _flag("SH_ALLOW_SCRIPT_EXEC") or _flag(
        "SH_ALLOW_CERTIFICATE_EXEC") or _flag("SH_ALLOW_REIMPLEMENTATION_EXEC"))
    allow_install: bool = field(default_factory=lambda: _flag("SH_ALLOW_INSTALL"))
    allow_network: bool = field(default_factory=lambda: _flag("SH_ALLOW_NETWORK", "1"))
    allow_source_search: bool = field(default_factory=lambda: _flag("SH_ALLOW_SOURCE_SEARCH"))
    # ponytail: token caps sized for a few papers per run; raise per invocation via env.
    max_checks: int = field(default_factory=lambda: _int("SH_MAX_CHECKS", 3))
    max_revisions: int = field(default_factory=lambda: _int("SH_MAX_REVISIONS", 3))
    max_tries: int = field(default_factory=lambda: _int("SH_MAX_TRIES", 3))
    # ponytail: a paper stating more runs than this is refused, never downscaled.
    max_runs: int = field(default_factory=lambda: _int("SH_MAX_RUNS", 100))
    # ponytail: 3 seeded replicates give a reconstruction a noise band; raise via env for tighter ones.
    replicates: int = field(default_factory=lambda: _int("SH_REPLICATES", 3))
    # ponytail: 2 concurrent script seeds fit a 6-CPU Docker VM; raise via env on a bigger host.
    parallel: int = field(default_factory=lambda: _int("SH_PARALLEL", 2))
    # ponytail: a check projected (from its timed pilot run) past 2 h is a documented blocker, not downscaled.
    check_budget_s: int = field(default_factory=lambda: _int("SH_CHECK_BUDGET_S", 7200))
    run_timeout_s: int = field(default_factory=lambda: _int("SH_RUN_TIMEOUT_S", 3600))
    install_timeout_s: int = field(default_factory=lambda: _int("SH_INSTALL_TIMEOUT_S", 3600))
    try_timeout_s: int = field(default_factory=lambda: _int("SH_TRY_TIMEOUT_S", 300))
    # ponytail: 30k chars per Read call keeps dense text (~1.8 chars/token) under the Read
    # tool's 25k-token cap.
    read_chunk: int = 30_000


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n].strip("-") or "paper"


def pdir(cfg: Config, pid: str) -> Path:
    return cfg.projects / pid


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    """Atomic: a reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), **obj}, ensure_ascii=False) + "\n")


if sys.platform == "win32":
    import msvcrt

    def _lock(f) -> None:
        while True:          # LK_LOCK gives up after ~10s; wait out a longer holder
            f.seek(0)
            try:
                return msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                time.sleep(0.05)

    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def lock(path: Path):
    """An OS lock on `path`: parallel seals of one paper serialize here."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        _lock(f)
        try:
            yield
        finally:
            _unlock(f)
