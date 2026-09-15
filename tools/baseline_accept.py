"""Validate and seal SINGLE-MODEL CODEX CRITIQUE responses.

Usage:
    python tools/baseline_accept.py <paper-id> [...]

``SH_PROJECTS_DIR`` selects the run, exactly as it does for ``run.py``.  The raw
``baseline/response.json`` is preserved.  A normalized, evidence-checked report and a
hash-bearing provenance sidecar are written beside it.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import audit_driver, delegation, state  # noqa: E402
from harness.artifacts import PaperDoc  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.stages import audit  # noqa: E402


def accept_one(cfg: Config, pid: str, reviewer: str = "SINGLE-MODEL CODEX CRITIQUE") -> dict:
    root = state.project_dir(cfg, pid)
    prompt = root / "baseline" / "prompt.md"
    # A fresh retry may intentionally coexist with an excluded earlier response.  Prefer
    # the explicitly fresh file so evidence is preserved without letting it be counted.
    fresh_path = root / "baseline" / "response.fresh.json"
    raw_path = fresh_path if fresh_path.is_file() else root / "baseline" / "response.json"
    if not prompt.is_file() or not raw_path.is_file():
        raise FileNotFoundError(f"missing baseline prompt or response for {pid}")

    parsed = audit_driver.parse_lens_json(raw_path.read_text(encoding="utf-8"), "baseline")
    doc = PaperDoc(**state.read_json(root / "paper" / "doc.json"))
    report, dropped, valid = audit._coerce(  # same evidence gate as REFEREE lenses
        "baseline", parsed.model_dump(), audit.source_units(doc),
        {t.table_idx: t for t in doc.tables}, doc.n_pages,
        by_figure={f.figure_idx: f for f in doc.figures},
        by_equation={e.equation_idx: e for e in doc.equations},
    )
    if not valid:
        raise ValueError(f"invalid baseline response for {pid}")

    out = root / "baseline" / "accepted.json"
    state.write_json(out, report.model_dump())
    record = {
        "paper_id": pid,
        **delegation.provenance_record(mode="SESSION_SUBAGENT", reviewer=reviewer,
                                       tool_policy="ephemeral, ignore-rules, read-only sandbox"),
        "controller": "codex exec v0.154.0-alpha.6.2", "provider_reported": "openai",
        "model_requested": "gpt-5.6-luna", "model_reported": "gpt-5.6-luna",
        "reasoning_effort_reported": os.environ.get("SH_BASELINE_REASONING_EFFORT", "low"),
        "authorization": "RUN_AUTHORIZATION.json",
        "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
        "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "raw_response": raw_path.name,
        "content_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "kept": len(report.findings),
        "dropped": dropped,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    state.write_json(out.with_suffix(".driver.json"), record)
    return record


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit("usage: baseline_accept.py <paper-id> [...]")
    cfg = Config.load()
    for pid in argv:
        rec = accept_one(cfg, pid)
        print(f"{pid}: kept={rec['kept']} dropped={rec['dropped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
