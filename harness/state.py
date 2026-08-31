"""Project state on the local filesystem: one directory per project, an
append-only research log, and typed artifact files. This is the shared memory
the Director and every stage read; it stores ARTIFACTS, never transcripts.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config

SUBDIRS = [
    "paper",        # S1 — the ingested PaperDoc
    "audit",        # S2 — one LensReport per auditor
    "reports",      # S4 — the rendered evaluation report
    # S3 reproduction trigger
    "studies",
    "grounding",
    "runs",
    "analysis",
    "checkpoints",
]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(s: str, n: int = 28) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:n] or "project").strip("-")


def project_dir(cfg: Config, pid: str) -> Path:
    return cfg.projects_dir / pid


def slot_dir(cfg: Config, pid: str, slot: str = "") -> Path:
    """Artifact root for a stage. slot="" → the canonical project dir (single-idea path);
    a non-empty slot → candidates/<slot>/ so K tournament candidates never clobber each
    other's chain/spec/grounding artifacts."""
    root = project_dir(cfg, pid)
    return (root / "candidates" / slugify(slot, 40)) if slot else root


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
        "created_at": _now(),
        "updated_at": _now(),
        "cost_usd": 0.0,       # LLM/agent cost
        "gpu_cost_usd": 0.0,   # Modal GPU cost — the budgeted resource
    }
    save_meta(cfg, pid, meta)
    (root / "research_log.jsonl").touch()
    return pid


def load_meta(cfg: Config, pid: str) -> dict[str, Any]:
    return json.loads((project_dir(cfg, pid) / "project.json").read_text(encoding="utf-8"))


def save_meta(cfg: Config, pid: str, meta: dict[str, Any]) -> None:
    meta["updated_at"] = _now()
    (project_dir(cfg, pid) / "project.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def set_phase(cfg: Config, pid: str, phase: str) -> None:
    meta = load_meta(cfg, pid)
    meta["phase"] = phase
    save_meta(cfg, pid, meta)


def add_cost(cfg: Config, pid: str, cost_usd: float) -> None:
    meta = load_meta(cfg, pid)
    meta["cost_usd"] = round(float(meta.get("cost_usd", 0.0)) + float(cost_usd or 0.0), 6)
    save_meta(cfg, pid, meta)


def add_gpu_cost(cfg: Config, pid: str, gpu_cost_usd: float) -> float:
    """Accumulate Modal GPU cost (the budgeted resource) and return the new total."""
    meta = load_meta(cfg, pid)
    meta["gpu_cost_usd"] = round(float(meta.get("gpu_cost_usd", 0.0)) + float(gpu_cost_usd or 0.0), 4)
    save_meta(cfg, pid, meta)
    return meta["gpu_cost_usd"]


def gpu_cost(cfg: Config, pid: str) -> float:
    return float(load_meta(cfg, pid).get("gpu_cost_usd", 0.0))


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
        "ts": _now(),
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


def read_log(cfg: Config, pid: str) -> list[dict[str, Any]]:
    p = project_dir(cfg, pid) / "research_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, obj: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
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
