"""Canonical node functions — one per pipeline stage.

Each returns a COMPACT dict (a pointer plus a headline), never a full body. This is
the surface `run.py node <name>` drives.

    ingest_paper       S1  PDF → PaperDoc. Deterministic; no model involved.
    audit_paper        S2  writes one prompt file per lens for the driver to execute.
    run_probe          S3  local reproduction on this machine's GPU.
    synthesize_report  S4  verified rank → reports/<paper_id>.md

Only S2 needs a model, and it is the Claude Code session you are already in.
"""
from __future__ import annotations

from .config import Config
from .stages import audit as audit_stage
from .stages import ingest as ingest_stage
from .stages import probe as probe_stage
from .stages import report as report_stage


def ingest_paper(cfg: Config, paper: str) -> dict:
    """S1. `paper` is the PATH to a PDF; the case id is derived from its filename."""
    return ingest_stage.run_ingest(cfg, paper)


def audit_paper(cfg: Config, paper: str) -> dict:
    """S2. Renders `audit/prompts/<lens>.md`. The driver writes `audit/<lens>.json`."""
    return audit_stage.run_audit(cfg, paper)


def run_probe(cfg: Config, paper: str) -> dict:
    """S3. Runs the reproduction probe locally across seeds and measures the noise floor."""
    return probe_stage.run(cfg, paper)


def synthesize_report(cfg: Config, paper: str) -> dict:
    """S4. Re-verifies every finding's evidence, ranks, renders."""
    return report_stage.run_report(cfg, paper)
