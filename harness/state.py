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


def load_settings(path: Path, env=os.environ) -> None:
    """SH_* settings from one file (KEY=VALUE lines, `#` comments), so every command of a run, and a resumed run, uses
    the same ones: a compute host's budgets, gates and caps live in it. A variable already set in the environment wins."""
    if not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() and not key.strip().startswith("#"):
            value = re.split(r"\s#", value, maxsplit=1)[0]          # an inline comment is not part of the value
            env.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_settings(Path(os.environ.get("SH_ENV_FILE") or ROOT / "referee.env"))


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    """A whole-number setting; a value that is not one is refused, never replaced by the default unseen."""
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        raise SystemExit(f"setting {name}={os.environ.get(name)!r} is not a whole number (referee.env or the environment)")


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
    allow_source_search: bool = field(default_factory=lambda: _flag("SH_ALLOW_SOURCE_SEARCH"))   # the authors' repository
    # Finding the public datasets a paper names (registry searches that send only the artifact's
    # name). It is its own gate: it grants no code, no clone and no execution, and a run without
    # author code still needs it.
    allow_data_search: bool = field(default_factory=lambda: _flag("SH_ALLOW_DATA_SEARCH", "1"))
    # ponytail: 12 registry searches per paper cover the datasets a paper names; more is a loop.
    max_discoveries: int = field(default_factory=lambda: _int("SH_MAX_DISCOVERIES", 12))
    # ponytail: token caps sized for a few papers per run; raise per invocation via env. 6 checks
    # cover a paper's headline experiments and theorems; one follow-up plan may add 3 more where a
    # central claim is still undecided.
    max_checks: int = field(default_factory=lambda: _int("SH_MAX_CHECKS", 6))
    max_followup_checks: int = field(default_factory=lambda: _int("SH_MAX_FOLLOWUP_CHECKS", 3))
    # ponytail: 20 GB of acquired public data per check fits one host's disk; a larger artifact is
    # a documented storage blocker, never a silent subset.
    max_data_gb: int = field(default_factory=lambda: _int("SH_MAX_DATA_GB", 20))
    # ponytail: a fetch may take what SH_MAX_DATA_GB needs at 1 MB/s (20 GB: about 5.7 h); a data download is no pip
    # build, so it has its own limit. A stalled transfer is caught inside the fetcher by its read timeout; this cap
    # only ends a fetcher that stopped answering (Oct-01: a 2.9 GB archive at this host's 0.76 MB/s needs over 1 h).
    fetch_timeout_s: int = field(default_factory=lambda: _int("SH_FETCH_TIMEOUT_S", _int("SH_MAX_DATA_GB", 20) * 1024))
    # Sources a review must never read (evaluation records about the papers themselves).
    deny_sources: tuple = field(default_factory=lambda: tuple(
        s.strip().lower() for s in os.environ.get("SH_DENY_SOURCES", "ICML-2026-agent-repro").split(",") if s.strip()))
    max_revisions: int = field(default_factory=lambda: _int("SH_MAX_REVISIONS", 3))
    max_tries: int = field(default_factory=lambda: _int("SH_MAX_TRIES", 3))
    # ponytail: a paper stating more runs than this is refused, never downscaled.
    max_runs: int = field(default_factory=lambda: _int("SH_MAX_RUNS", 100))
    # ponytail: 3 seeded replicates give a reconstruction a noise band; raise via env for tighter ones.
    replicates: int = field(default_factory=lambda: _int("SH_REPLICATES", 3))
    # Concurrent script runs on this host; 0 (unset) follows the measured host (execute.parallel: CPUs and container
    # memory; 2 when the daemon cannot say).
    parallel: int = field(default_factory=lambda: _int("SH_PARALLEL", 0))
    # ponytail: a check projected (from its timed pilot run) past 2 h is a documented blocker, not downscaled.
    check_budget_s: int = field(default_factory=lambda: _int("SH_CHECK_BUDGET_S", 7200))
    # ponytail: an engineering compatibility test (does the component integrate and train) is a few
    # runs of one configuration; 30 min is its budget, never a performance benchmark's.
    compat_budget_s: int = field(default_factory=lambda: _int("SH_COMPAT_BUDGET_S", 1800))
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
