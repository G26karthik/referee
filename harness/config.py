"""Paths and execution settings.

No credentials are REQUIRED, and this process never holds one: the deterministic stages
are plain Python, and every judgement stage (a lens, a grade, the substantive verdict, a
governed reconstruction, an authors'-code reading) is answered by a subagent the
CONTROLLING Claude Code session dispatches -- see `harness/tasks.py`. This harness only
ever writes a prompt file and validates a JSON answer against one.

`Config.load()` therefore cannot fail for want of a token -- the only thing it does is
make sure the cases directory exists.
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
    # WHERE a run's artifacts live, and therefore WHICH RUN this is. Overridable because
    # a new evaluation must not overwrite the one it is compared against.
    projects_dir: pathlib.Path = field(
        default_factory=lambda: pathlib.Path(os.environ["SH_PROJECTS_DIR"]).resolve()
        if os.environ.get("SH_PROJECTS_DIR") else PROJECTS_DIR)
    python: str = field(default_factory=lambda: os.environ.get("SH_PYTHON") or sys.executable)
    seeds: int = field(default_factory=lambda: int(os.environ.get("SH_SEEDS", "5")))
    probe_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_PROBE_TIMEOUT", "1800")))
    # How many targets one paper may pursue to execution. A budget, not a policy: which
    # targets are worth pursuing is `harness.planner`'s decision and the ORDER is
    # `harness.priority`'s, so lowering this drops the least useful targets first.
    max_targets: int = field(default_factory=lambda: int(os.environ.get("SH_MAX_TARGETS", "3")))

    # --- S3 code-reproduction gates, graded by risk since fetching and running code are
    # different acts: network (clone, read-only, for the static audit), install (resolves
    # a stranger's dependency list, arbitrary setup code), exec (runs the repo's own
    # entrypoint). `synthesis` is not a risk gate: the probe planner writes a script from
    # the paper's own published formulation, readable at `runs/<pid>/probe.py` before it
    # runs, so it carries none of the third-party risk the other gates hold back.
    #
    # --- S2 autonomy: one delegation channel. The controlling Claude Code session
    # dispatches its own isolated subagents, one per delegable unit (`harness/tasks.py`);
    # this harness never shells out to a model CLI of its own. `load_reports` re-checks
    # every quote against the parsed PDF regardless of who wrote the JSON.

    # --- S2.5 independent grading: the grader is shown ZERO tools and no filesystem
    # access (see `harness/prompts/grade.py`), so it cannot read the finding it is
    # deliberately not shown. Grading changes what is ELIGIBLE to be counted at each
    # severity, not what thresholds mean; ungraded, `Finding.counted_severity` stays
    # empty and falls back to the lens's own `severity`.
    #
    # Empty means the role's declared default in `harness.prompts.grade.ROLE_SPEC`.
    grade_model: str = field(default_factory=lambda: (os.environ.get("SH_GRADE_MODEL") or "").strip())
    # "serious": only FATAL/MAJOR candidates are graded (a MINOR cannot cross any
    # threshold on its own). "all" grades every substantiated candidate, for evaluation.
    grade_scope: str = field(default_factory=lambda: os.environ.get("SH_GRADE_SCOPE", "serious"))
    grade_budget_chars: int = field(
        default_factory=lambda: int(os.environ.get("SH_GRADE_BUDGET_CHARS", "45000")))
    # Defaults ON: an incomplete grading pass blocks the report (`waiting`) until every
    # in-scope candidate is graded. SH_REQUIRE_GRADES=0 falls back to lens-asserted
    # severity for whatever is not graded this run.
    require_grades: bool = field(default_factory=lambda: _flag("SH_REQUIRE_GRADES", True))

    # --- the authors'-code auditor: one read-only pass over the PINNED checkout,
    # proposing code-level scientific concerns. NO EXECUTION AUTHORITY and NO DECISION
    # AUTHORITY -- every code citation it writes is relocated by
    # `artifact_evidence.relocate` before it can survive. A model statement about code is
    # not artifact evidence.
    artifact_review_model: str = field(
        default_factory=lambda: (os.environ.get("SH_ARTIFACT_REVIEW_MODEL") or "").strip())

    # --- the substantive verdict: a single, best-effort, whole-paper opinion (see
    # `harness/prompts/verdict.py`). Its absence NEVER blocks a report; only
    # `verdict`/`verdict_reason` (the deterministic ones) do that.
    verdict_model: str = field(default_factory=lambda: (os.environ.get("SH_VERDICT_MODEL") or "").strip())

    # Which execution backend runs THIRD-PARTY repository code. Not a gate on its own:
    # the repo-exec gate, identity chain, commit verification and capability check all
    # still apply, and `authorize()` is the only thing that may say yes. An unknown name
    # is refused rather than substituted.
    #
    #   local     this machine, subprocesses. The default.
    #   container a local container with its own filesystem namespace.
    #   kaggle
    #   colab     declarations of published hardware. Never runners.
    exec_backend: str = field(default_factory=lambda: os.environ.get("SH_EXEC_BACKEND", "local"))

    allow_network: bool = field(default_factory=lambda: _flag("SH_ALLOW_NETWORK", True))
    allow_install: bool = field(default_factory=lambda: _flag("SH_ALLOW_INSTALL"))
    allow_repo_exec: bool = field(default_factory=lambda: _flag("SH_ALLOW_REPO_EXEC"))
    allow_synthesis: bool = field(default_factory=lambda: _flag("SH_ALLOW_SYNTHESIS", True))

    # --- PATH B, governed reconstruction: TWO gates, because WRITING a reconstruction and
    # RUNNING it are different acts, the same separation `allow_install`/`allow_repo_exec`
    # already make for a real repository.
    #
    #   driver  Delegates to a session subagent to WRITE a reconstruction script from the
    #           paper's own specification. Nothing it writes is executed by this gate; it
    #           only produces text this harness then verifies before persisting it.
    #   exec    OFF by default, separate from `allow_repo_exec`: a governed reconstruction
    #           is code a MODEL wrote, not the authors' own published artifact, so
    #           granting the one never grants the other. `backends.authorize` additionally
    #           requires the same isolation floor `allow_repo_exec` requires.
    allow_reimplementation_driver: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_REIMPLEMENTATION_DRIVER", True))
    reimplementation_model: str = field(
        default_factory=lambda: (os.environ.get("SH_REIMPLEMENTATION_MODEL") or "").strip())
    allow_reimplementation_exec: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_REIMPLEMENTATION_EXEC"))
    allow_certificate_exec: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_CERTIFICATE_EXEC"))
    # DIAGNOSTIC MODE -- off by default. A harness-authored probe can never settle a
    # printed quantity (`provenance.admits` refuses `synthesized`/`template` both ways),
    # so running one when identity fails is spending compute on a result inadmissible
    # before it starts. With this ON, a diagnostic still runs, written to
    # `runs/<pid>/diagnostics/` as a `DiagnosticRun` rather than a `TargetOutcome`:
    # invisible to the funnel and every reader-facing row. It informs a reader and
    # decides nothing.
    diagnostic_mode: bool = field(default_factory=lambda: _flag("SH_DIAGNOSTIC_MODE"))
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
