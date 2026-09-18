"""Project state on the local filesystem: one directory per project, an
append-only research log, and typed artifact files. This is the shared memory
the Director and every stage read; it stores ARTIFACTS, never transcripts.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config

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
    meta = load_meta(cfg, pid)
    meta["cost_usd"] = round(float(meta.get("cost_usd", 0.0)) + float(cost_usd or 0.0), 6)
    save_meta(cfg, pid, meta)




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
