"""Paths and local-execution settings.

No credentials. Nothing in this harness calls a hosted model: the deterministic
stages are plain Python, and the judgement stages are driven by the Claude Code
session you are already sitting in (see `stages/audit.py`).

`Config.load()` therefore cannot fail for want of a token — the only thing it does
is make sure the cases directory exists.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
from dataclasses import dataclass, field

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent      # single-harness/
REPO_ROOT = BASE_DIR.parent
PROJECTS_DIR = BASE_DIR / "projects"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    projects_dir: pathlib.Path = PROJECTS_DIR
    python: str = field(default_factory=lambda: os.environ.get("SH_PYTHON") or sys.executable)
    device: str = field(default_factory=lambda: os.environ.get("SH_DEVICE", "auto"))
    seeds: int = field(default_factory=lambda: int(os.environ.get("SH_SEEDS", "5")))
    probe_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_PROBE_TIMEOUT", "1800")))

    # --- S3 code-reproduction gates -------------------------------------------------
    # These are graded by risk, not bundled, because fetching code and running code are
    # different acts.
    #
    #   network  ON  by default. `git clone --depth 1` of a URL the paper itself
    #                advertises, read-only, so the static AST audit has something to
    #                read. Nothing fetched is imported or executed by that audit.
    #   install  OFF by default. Resolving a stranger's dependency list runs arbitrary
    #                setup.py/build hooks, which is execution wearing a package manager
    #                as a hat.
    #   exec     OFF by default. Running the repository's own entrypoint is running
    #                third-party code, and it stays an explicit, per-invocation opt-in.
    #
    # `synthesis` is a fourth thing and deliberately not a risk gate: the probe planner
    # writes a script from the PAPER'S OWN published formulation and runs THAT. The code
    # executed is generated here and readable at `runs/<pid>/probe.py` before it runs, so
    # it carries none of the third-party risk the other gates exist to hold back.
    allow_network: bool = field(default_factory=lambda: _flag("SH_ALLOW_NETWORK", True))
    allow_install: bool = field(default_factory=lambda: _flag("SH_ALLOW_INSTALL"))
    allow_repo_exec: bool = field(default_factory=lambda: _flag("SH_ALLOW_REPO_EXEC"))
    allow_synthesis: bool = field(default_factory=lambda: _flag("SH_ALLOW_SYNTHESIS", True))
    clone_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_CLONE_TIMEOUT", "600")))
    install_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_INSTALL_TIMEOUT", "1800")))
    max_audit_files: int = field(default_factory=lambda: int(os.environ.get("SH_MAX_AUDIT_FILES", "800")))

    @classmethod
    def load(cls) -> "Config":
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        return cls()

    def has_gpu(self) -> bool:
        """Cheap check that does not import torch into the harness process."""
        return shutil.which("nvidia-smi") is not None
