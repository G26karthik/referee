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
    # --- S2 autonomy -------------------------------------------------------------------
    # OFF by default, and the default stays off on purpose. S2 is the judgement stage;
    # this harness has no model of its own to call, so "autonomous" here can only mean
    # "shell out to whatever reviewer the operator configures". That is a real capability
    # and a real risk: the findings it produces are authored by an external process, so
    # every lens filled this way is recorded in `audit/<lens>.driver.json` with the exact
    # command that wrote it. Provenance for a machine-written audit is the same
    # requirement as provenance for a machine-written probe.
    #
    # `SH_AUDIT_CMD` is a command template with two placeholders:
    #     {prompt}  the lens prompt file to read
    #     {out}     the path the command must write valid lens JSON to
    # e.g.  SH_AUDIT_CMD='claude -p --output-format text "$(cat {prompt})" > {out}'
    #
    # Nothing about this weakens verification: `load_reports` re-checks every quote
    # against the parsed PDF regardless of who wrote the JSON, so an auto-filled lens
    # that invents evidence has its findings dropped exactly like a human's would.
    allow_auto_audit: bool = field(default_factory=lambda: _flag("SH_ALLOW_AUTO_AUDIT"))
    audit_cmd: str = field(default_factory=lambda: os.environ.get("SH_AUDIT_CMD", ""))
    audit_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_AUDIT_TIMEOUT", "900")))
    # How many times the controller may re-attempt ONE lens whose reviewer failed. A
    # timeout or a truncated JSON object is transient; the same prompt run again may well
    # succeed. Bounded, and every attempt is recorded in the case history — a retry that
    # is not visible is indistinguishable from a stage that never ran.
    #
    # Nothing else in the pipeline is retryable. An identity, resource, commit or
    # authorization refusal is deterministic, and re-running one would be an attempt to
    # get a different answer out of a gate that is doing its job.
    audit_retries: int = field(default_factory=lambda: int(os.environ.get("SH_AUDIT_RETRIES", "2")))

    # Which execution backend runs THIRD-PARTY repository code. Not a gate — `local` is
    # the only registered backend, and selecting one grants nothing on its own: the
    # repo-exec gate, the identity chain and the capability check all still apply. It
    # exists so that adding a Linux/WSL/container backend later is a name here rather
    # than a change to anything that decides what a verdict means. An unknown name is
    # refused rather than substituted (see backends.select_backend).
    exec_backend: str = field(default_factory=lambda: os.environ.get("SH_EXEC_BACKEND", "local"))

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
