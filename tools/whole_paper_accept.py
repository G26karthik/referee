"""Seal paper-only whole-paper assessment responses with prompt provenance."""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import state, verdict_driver  # noqa: E402
from harness.config import Config  # noqa: E402


def main(argv: list[str]) -> int:
    cfg = Config.load()
    for pid in argv:
        root = state.project_dir(cfg, pid)
        prompt = root / "reports" / "whole_paper_prompt.md"
        raw = root / "reports" / "whole_paper_response.json"
        verdict = verdict_driver.parse_verdict_json(raw.read_text(encoding="utf-8"))
        rec = verdict_driver._seal(cfg, pid, verdict, {
            "written_by": "verdict_driver",
            "reader": "SINGLE-MODEL CODEX WHOLE-PAPER ASSESSOR",
            "controller": "codex exec v0.154.0-alpha.6.2",
            "provider_reported": "openai",
            "model_requested": "gpt-5.6-luna",
            "model_reported": "gpt-5.6-luna",
            "reasoning_effort_reported": "low",
            "command_policy": "ephemeral, ignore-rules, read-only sandbox",
            "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
            "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            "authorization": "RUN_AUTHORIZATION.json",
            "seconds": 0.0,
            "ts_started": "unrecorded",
        })
        print(f"{pid}: {rec['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
