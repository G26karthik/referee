"""Paths, execution settings, and where a remote sandbox is asked for.

No credentials are REQUIRED. Nothing in the review pipeline calls a hosted model: the
deterministic stages are plain Python, and the judgement stages are driven by whatever
reviewer the operator configures (see `stages/audit.py`).

`Config.load()` therefore cannot fail for want of a token — the only thing it does is
make sure the cases directory exists. Two subsystems can *use* credentials when the
operator supplies them, and both are off by default: the audit/grade/verdict drivers,
and the remote sandbox backend (`SH_ALLOW_SANDBOX`, read from the environment or from a
`.env.sandbox` file beside the repo). A missing token is never an error here; it makes a
capability unavailable and is reported as such.
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

# Credentials for the remote sandbox are read from the environment, or from this file if
# it exists, so an operator can keep a provider token out of their shell history. Loaded
# with `setdefault` semantics: a variable already exported always wins, and nothing here
# is ever written back to disk.
#
# The file is deliberately NOT in the repo and is git-ignored. It is read at
# `Config.load()` time rather than at import time so a test that manipulates the
# environment is not racing a module-level side effect.
ENV_FILES = (BASE_DIR / ".env.sandbox", REPO_ROOT / ".env.sandbox")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    """An unparseable value falls back to the default rather than crashing the run.

    A malformed `SH_SANDBOX_CPU=four` should not take a review down at import time; the
    declared default is a safe answer and `sandbox.spec_from_config` reports what it used.
    """
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(float(raw)) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def load_env_files(paths: "tuple[pathlib.Path, ...] | None" = None) -> list[str]:
    """Populate the environment from `.env.sandbox`, without overriding what is set.

    Returns the files that were read, so a caller can say where a credential came from.
    Quoted values are kept verbatim; an unquoted trailing ` # comment` is stripped.
    """
    read: list[str] = []
    for path in (paths if paths is not None else ENV_FILES):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if not (value[:1] in "\"'" and value[-1:] == value[:1]):
                value = value.split(" #", 1)[0].rstrip()
            os.environ.setdefault(key.strip(), value.strip("\"'"))
        read.append(str(path))
    return read


@dataclass
class Config:
    # WHERE a run's artifacts live, and therefore WHICH RUN this is. Overridable because
    # a new evaluation must not overwrite the one it is compared against: every paper
    # `preflight` reports as RESUMES would resume its existing project and rewrite it in
    # place, which is right per paper and destroys the historical record in aggregate.
    # `cfg.projects_dir.parent / "reports"` is where the corpus and evaluation artifacts
    # land, so pointing this at a fresh directory yields a self-contained run identity
    # rather than a half-separated one.
    projects_dir: pathlib.Path = field(
        default_factory=lambda: pathlib.Path(os.environ["SH_PROJECTS_DIR"]).resolve()
        if os.environ.get("SH_PROJECTS_DIR") else PROJECTS_DIR)
    python: str = field(default_factory=lambda: os.environ.get("SH_PYTHON") or sys.executable)
    device: str = field(default_factory=lambda: os.environ.get("SH_DEVICE", "auto"))
    seeds: int = field(default_factory=lambda: int(os.environ.get("SH_SEEDS", "5")))
    probe_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_PROBE_TIMEOUT", "1800")))
    # How many targets one paper may pursue to execution. A budget, not a policy: which
    # targets are worth pursuing is `harness.planner`'s decision and the ORDER is
    # `harness.priority`'s, so lowering this drops the least useful targets first rather
    # than an arbitrary subset. Raising it costs compute and changes no rule.
    max_targets: int = field(default_factory=lambda: int(os.environ.get("SH_MAX_TARGETS", "3")))

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
    # WHICH executable the built-in delegation template invokes, instead of discovering
    # `claude` on PATH. Not a convenience: without it, every assertion about the argv the
    # harness builds — the deny flags, the pinned settings, the per-role model — was
    # guarded by `if default_cmd():` and therefore skipped on any host without the CLI,
    # which is every CI box. The isolation guarantees were asserted nowhere. With this,
    # `tests/test_delegation_path.py` drives the whole built-in path against a scripted
    # reviewer double, the way `tests/test_sandbox_backend.py` drives the cloud provider.
    #
    # It grants nothing. The gate is still `allow_auto_audit`, and a double named here
    # produces output that `parse_lens_json` and then `stages.audit.load_reports` check
    # exactly as they check a real reviewer's.
    reviewer_exe: str = field(default_factory=lambda: (os.environ.get("SH_REVIEWER_EXE") or "").strip())
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

    # --- S2.5 independent grading --------------------------------------------------
    # OFF by default, same reasoning as `allow_auto_audit`: a second external process,
    # a second command an operator must opt into. Unlike a lens, the grader runs with
    # ZERO tools and no filesystem access at all — see `harness/grade_driver.py` — so it
    # cannot read the severity or any other finding it is deliberately not shown.
    #
    # Grading does not change what THRESHOLDS mean (`RED_FATAL` etc. in
    # `stages/report.py` are untouched); it changes what is ELIGIBLE to be counted at
    # each severity. With the gate closed, `Finding.counted_severity` stays empty and
    # `stages.report.counted()` falls back to the lens's own `severity` — the verdict is
    # byte-identical to what it was before this subsystem existed.
    allow_grading: bool = field(default_factory=lambda: _flag("SH_ALLOW_GRADING"))
    grade_cmd: str = field(default_factory=lambda: os.environ.get("SH_GRADE_CMD", ""))
    # WHICH model grades. Empty means the role's declared default in
    # `harness.prompts.grade.ROLE_SPEC`, never the CLI's ambient default — that was the real
    # state and it was invisible: `grade_driver.default_cmd()` emitted no `--model` at
    # all, so the "independent second reader" ran on whatever the operator's
    # `~/.claude/settings.json` said, which on the development host was the same model
    # family as the `overclaim` lens it was meant to check independently. A grader whose
    # identity is a property of the operator's ambient config is not a recorded fact.
    grade_model: str = field(default_factory=lambda: (os.environ.get("SH_GRADE_MODEL") or "").strip())
    grade_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_GRADE_TIMEOUT", "600")))
    grade_retries: int = field(default_factory=lambda: int(os.environ.get("SH_GRADE_RETRIES", "2")))
    # "serious": only FATAL/MAJOR candidates are graded — justified by threshold
    # reachability, not cost: a MINOR cannot cross any RED branch on its own, so grading
    # it can only ever move a verdict toward GREEN, the direction a false negative there
    # is cheapest. "all" grades every substantiated candidate, for evaluation runs.
    grade_scope: str = field(default_factory=lambda: os.environ.get("SH_GRADE_SCOPE", "serious"))
    grade_budget_chars: int = field(
        default_factory=lambda: int(os.environ.get("SH_GRADE_BUDGET_CHARS", "45000")))
    # If set, an incomplete grading pass blocks the report (`waiting`, not a partial
    # report) instead of the default — proceed with whatever graded, everything else
    # ungraded and falling back to its lens severity. Off by default: abstention is not
    # failure, and a paper must still reach a complete report when the grader available
    # this run could not cover everything, exactly as the audit lenses already work.
    require_grades: bool = field(default_factory=lambda: _flag("SH_REQUIRE_GRADES"))

    # --- claim links ------------------------------------------------------------------
    # The one correspondence a paper does not print: which number a headline claim rests
    # on. OFF by default, and the default is load-bearing rather than cautious. With the
    # gate closed no link is ever established, `claimgraph` has no SUPPORTED_BY edges, and
    # `materiality` falls back to its own structural rule — which is exactly the state the
    # harness was in before this channel existed. So turning it off reproduces the
    # pre-claim-link decision bit for bit, the same property `allow_grading` has and for
    # the same reason: a model channel that could only be assessed by running it is a
    # channel nobody can turn off to see what it did.
    allow_claim_links: bool = field(default_factory=lambda: _flag("SH_ALLOW_CLAIM_LINKS"))
    claimlink_cmd: str = field(default_factory=lambda: os.environ.get("SH_CLAIMLINK_CMD", ""))
    # Same reasoning as `grade_model` and `verdict_model`: empty means the role's declared
    # default in `harness.prompts.claimlink.ROLE_SPEC`, never the CLI's ambient default.
    claimlink_model: str = field(
        default_factory=lambda: (os.environ.get("SH_CLAIMLINK_MODEL") or "").strip())
    claimlink_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("SH_CLAIMLINK_TIMEOUT", "600")))

    # --- the authors'-code auditor ---------------------------------------------------
    # "Did the authors actually implement the code correctly?" — one read-only pass over
    # the PINNED checkout, proposing code-level scientific concerns. OFF by default and
    # for the same reason as every other model channel: with it closed the artifact route
    # still runs, on deterministic probes alone, and a reviewer sees exactly what the
    # harness could establish without a model.
    #
    # It has NO EXECUTION AUTHORITY and NO DECISION AUTHORITY, and neither is enforced by
    # this flag — the confinement grants `Read` and `Grep` and nothing else, and every
    # code citation it writes is relocated by `artifact_evidence.relocate` before it can
    # survive. A model statement about code is not artifact evidence.
    allow_artifact_review: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_ARTIFACT_REVIEW"))
    artifact_review_cmd: str = field(
        default_factory=lambda: os.environ.get("SH_ARTIFACT_REVIEW_CMD", ""))
    artifact_review_model: str = field(
        default_factory=lambda: (os.environ.get("SH_ARTIFACT_REVIEW_MODEL") or "").strip())
    artifact_review_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("SH_ARTIFACT_REVIEW_TIMEOUT", "900")))

    # --- the prior-art route ----------------------------------------------------------
    # Two separate gates, because they buy two different things and fail in two different
    # ways. `allow_literature_search` permits QUERYING public scholarly indexes — network
    # egress to OpenAlex, Crossref, arXiv and Semantic Scholar — and
    # `allow_literature_review` permits a model to read what came back. Off by default,
    # both: with the first closed nothing is searched and the route reports LITERATURE_
    # BLOCKED, which is a fact about this host; with only the second closed the route
    # searches on deterministic queries and adjudicates nothing semantically.
    #
    # NEITHER GATE CAN PRODUCE A NOVELTY CONCLUSION, and that is not enforced by these
    # flags — `artifacts.NOVELTY_ESTABLISHING_AUTHORITIES` is the empty tuple, so a
    # completed search with no match establishes nothing whatever either gate is set to.
    allow_literature_search: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_LITERATURE_SEARCH"))
    allow_literature_review: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_LITERATURE_REVIEW"))
    literature_cmd: str = field(default_factory=lambda: os.environ.get("SH_LITERATURE_CMD", ""))
    literature_model: str = field(
        default_factory=lambda: (os.environ.get("SH_LITERATURE_MODEL") or "").strip())
    literature_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("SH_LITERATURE_TIMEOUT", "600")))
    # The indexes to ask, in order. An operator may narrow this; narrowing it narrows the
    # PUBLISHED protocol too, because `LiteratureSearch.protocol` records what was
    # required and `protocol_completed` is false when a required index did not answer.
    literature_providers: str = field(
        default_factory=lambda: os.environ.get("SH_LITERATURE_PROVIDERS",
                                               "openalex,crossref,arxiv"))
    # How deep each query goes. A BOUND, published in the record: "no match in the top 20"
    # means exactly that and says nothing about rank 21.
    literature_top_k: int = field(
        default_factory=lambda: int(os.environ.get("SH_LITERATURE_TOP_K", "20")))
    literature_max_claims: int = field(
        default_factory=lambda: int(os.environ.get("SH_LITERATURE_MAX_CLAIMS", "6")))
    # How many retrieved candidates a reader is shown per claim. A reader given 160
    # abstracts for one sentence has not read 160 abstracts.
    literature_max_candidates: int = field(
        default_factory=lambda: int(os.environ.get("SH_LITERATURE_MAX_CANDIDATES", "12")))
    # Identifies this harness to the indexes, which ask politely to be told who is asking.
    # Not a credential: none of the three default providers requires one, which is why
    # they are the default and Semantic Scholar is not.
    literature_mailto: str = field(
        default_factory=lambda: os.environ.get("SH_LITERATURE_MAILTO", ""))

    # --- the focused-validation route -------------------------------------------------
    # ONE gate, and it buys the DESIGN rather than the run. With it closed the route still
    # runs: the arms, the metric, the benchmark, the split and the addressed question are
    # all read off the document, and every design reports SPECIFICATION_BLOCKED naming the
    # ingredients the paper does not bind — which is the honest answer for a paper that
    # does not state enough, and is what a reader sees without any model in the loop.
    #
    # EXECUTION IS NOT GATED HERE. A focused validation runs the authors' own checkout, so
    # it passes through `backends.authorize` and `SH_ALLOW_REPO_EXEC` exactly as a
    # reproduction does. Nothing in this flag can start a process.
    allow_validation_design: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_VALIDATION_DESIGN"))
    validation_cmd: str = field(default_factory=lambda: os.environ.get("SH_VALIDATION_CMD", ""))
    validation_model: str = field(
        default_factory=lambda: (os.environ.get("SH_VALIDATION_MODEL") or "").strip())
    validation_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("SH_VALIDATION_TIMEOUT", "600")))
    # How many focused validations one paper may design. A BUDGET, not a gate: which
    # targets are worth it is `planner`'s decision and the order is `priority`'s.
    validation_max_designs: int = field(
        default_factory=lambda: int(os.environ.get("SH_VALIDATION_MAX_DESIGNS", "2")))

    # --- the substantive verdict -----------------------------------------------------
    # A single, best-effort, never-retried, whole-paper opinion — see
    # `harness/prompts/verdict.py`. OFF by default: a third external process an operator
    # must opt into, same reasoning as the two gates above it. Its failure NEVER blocks
    # a report; only `verdict`/`verdict_reason` (the deterministic ones) do that.
    allow_substantive_verdict: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_SUBSTANTIVE_VERDICT"))
    verdict_cmd: str = field(default_factory=lambda: os.environ.get("SH_VERDICT_CMD", ""))
    # Same reasoning as `grade_model`. Empty means the default declared in
    # `harness.prompts.verdict.ROLE_SPEC`, and the model that answered is recorded on the
    # sidecar either way.
    verdict_model: str = field(default_factory=lambda: (os.environ.get("SH_VERDICT_MODEL") or "").strip())
    verdict_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("SH_VERDICT_TIMEOUT", "300")))

    # Which execution backend runs THIRD-PARTY repository code. Not a gate, and selecting
    # one grants nothing on its own: the repo-exec gate, the identity chain, the commit
    # verification and the capability check all still apply, and `authorize()` is the only
    # thing that may say yes. An unknown name is refused rather than substituted (see
    # backends.select_backend).
    #
    #   local   this machine, subprocesses. The default, and the only backend that runs
    #           code THIS harness authored (see backends.local_backend).
    #   modal   a remote Linux sandbox, leased per paper (harness/sandbox.py). Needs
    #           `allow_sandbox` AND provider credentials; without either it reports
    #           itself unavailable and nothing runs there.
    #   kaggle
    #   colab   declarations of published hardware. Never runners.
    exec_backend: str = field(default_factory=lambda: os.environ.get("SH_EXEC_BACKEND", "local"))

    # --- the remote sandbox ------------------------------------------------------------
    # A fifth gate, and the only one that spends money rather than merely risk. It permits
    # LEASING remote compute: creating a sandbox, staging the audited checkout into it and
    # building the repository's environment there. It does not permit running third-party
    # code — `allow_repo_exec` still decides that, and both are required for a remote
    # reproduction. Off by default for the same reason `allow_install` is: an operator has
    # to ask for it once per invocation.
    #
    # A sandbox is leased PER PAPER, not per command: the checkout is staged once, the
    # environment is built once, and every (seed, arm) of every target runs in that same
    # session. `stages/probe.run` releases it in a `finally`, and `run.py sandbox` lists
    # and releases any that outlived their run.
    allow_sandbox: bool = field(default_factory=lambda: _flag("SH_ALLOW_SANDBOX"))
    # What to RESERVE. These are a request, and `sandbox.open_session` verifies the
    # sandbox it got is not smaller than what was asked for — a declared profile that is
    # never checked against the machine is the kind of unverified assumption every other
    # preflight in this harness exists to refuse.
    #
    # `SH_SANDBOX_GPU` is empty by default: a review whose experiments are CPU-bound must
    # not silently bill for an accelerator, and an unstated GPU demand is reported as
    # unknown by `select_for` rather than matched against a card nobody asked for.
    sandbox_gpu: str = field(default_factory=lambda: (os.environ.get("SH_SANDBOX_GPU") or "").strip())
    sandbox_cpu: float = field(default_factory=lambda: _float("SH_SANDBOX_CPU", 4.0))
    sandbox_memory_mib: int = field(default_factory=lambda: _int("SH_SANDBOX_MEMORY_MIB", 16384))
    # Declared only. A sandbox's ephemeral disk is not a reservable quantity, so this is
    # what we claim to offer for requirement matching and is verified after the fact
    # against what the session actually reports free.
    sandbox_disk_gib: int = field(default_factory=lambda: _int("SH_SANDBOX_DISK_GIB", 50))
    # The sandbox's own lifetime ceiling. It has to exceed provisioning plus every run the
    # paper will make, or the session dies mid-experiment and the result is a timeout that
    # says nothing about the paper.
    sandbox_timeout_s: int = field(default_factory=lambda: _int("SH_SANDBOX_TIMEOUT", 3600))
    # A backstop UNDER the `finally` that releases a lease. If this process is killed
    # between commands, nothing here runs the teardown, and the machine would bill until
    # `sandbox_timeout_s`. An idle ceiling makes it terminate itself instead.
    #
    # The tradeoff is real and fails in the safe direction: set too low, the session dies
    # while the harness is doing local work between targets, and the next command reports
    # `launched=False` — a setup failure, INCONCLUSIVE, never a verdict about the paper.
    # Set to 0 to disable it and rely on the lease ceiling alone.
    sandbox_idle_timeout_s: int = field(
        default_factory=lambda: _int("SH_SANDBOX_IDLE_TIMEOUT", 900))
    sandbox_python: str = field(
        default_factory=lambda: (os.environ.get("SH_SANDBOX_PYTHON") or "3.12").strip())
    sandbox_app: str = field(
        default_factory=lambda: (os.environ.get("SH_SANDBOX_APP") or "single-harness-review").strip())
    # How long the staging step may take: a shallow fetch of one commit plus a pip install
    # of the repository's declared stack. Separate from `install_timeout_s` because the
    # remote leg also pays for image pull and sandbox startup.
    sandbox_setup_timeout_s: int = field(
        default_factory=lambda: _int("SH_SANDBOX_SETUP_TIMEOUT", 2400))

    allow_network: bool = field(default_factory=lambda: _flag("SH_ALLOW_NETWORK", True))
    allow_install: bool = field(default_factory=lambda: _flag("SH_ALLOW_INSTALL"))
    allow_repo_exec: bool = field(default_factory=lambda: _flag("SH_ALLOW_REPO_EXEC"))
    allow_synthesis: bool = field(default_factory=lambda: _flag("SH_ALLOW_SYNTHESIS", True))
    # ALIGNMENT TRIAL — off by default, like every other gate that runs third-party code.
    # `harness.alignment.trial` invokes a candidate command with `--help` to CONFIRM a
    # statically-read argparse surface against the real program — no experiment runs, but
    # a `--help` invocation still executes the top of a file this harness did not write,
    # so it is gated exactly as repository execution is and requires the SAME isolation
    # sufficiency (`harness.isolation.sufficient_for_repo_exec`). A separate gate from
    # `allow_repo_exec` because confirming an argparse surface is a narrower, cheaper
    # permission an operator may reasonably grant without granting the full experiment.
    allow_alignment_trial: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_ALIGNMENT_TRIAL"))

    # --- PATH B, governed reconstruction (Step 8) --------------------------------------
    # TWO gates, because WRITING a reconstruction and RUNNING it are different acts with
    # different risk, the same separation `allow_sandbox` (lease) and `allow_repo_exec`
    # (run) already make for a real repository.
    #
    #   driver  OFF by default. Delegates to an external reviewer to WRITE a
    #           reconstruction script from the paper's own specification — a fourth
    #           external process, same reasoning as `allow_auto_audit`/`allow_grading`/
    #           `allow_substantive_verdict`. Nothing it writes is executed by this gate;
    #           it only produces text this harness then verifies (`reimplement_driver.
    #           conformance`) before persisting it.
    #   exec    OFF by default, and separate from `allow_repo_exec` on purpose: a governed
    #           reconstruction is code a MODEL wrote, not the authors' own published
    #           artifact, so granting the one never grants the other. `backends.authorize`
    #           additionally requires the SAME isolation floor `allow_repo_exec` requires
    #           (`harness.isolation.sufficient_for_repo_exec` — CONTAINER or
    #           REMOTE_SESSION) — decision 10: no capability that increases third-party
    #           code execution lands without that boundary enforced, and a model-authored
    #           script nobody reviewed is exactly such a capability.
    allow_reimplementation_driver: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_REIMPLEMENTATION_DRIVER"))
    reimplementation_cmd: str = field(
        default_factory=lambda: os.environ.get("SH_REIMPLEMENTATION_CMD", ""))
    reimplementation_model: str = field(
        default_factory=lambda: (os.environ.get("SH_REIMPLEMENTATION_MODEL") or "").strip())
    reimplementation_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("SH_REIMPLEMENTATION_TIMEOUT", "600")))
    allow_reimplementation_exec: bool = field(
        default_factory=lambda: _flag("SH_ALLOW_REIMPLEMENTATION_EXEC"))
    # DIAGNOSTIC MODE — off by default, and the default is the whole point.
    #
    # A harness-authored probe can never settle a printed quantity: `provenance.admits`
    # refuses `synthesized` and `template` in both directions. Until now the probe stage
    # ran one anyway whenever identity failed, which is exactly when it was already known
    # that nothing admissible could come of it. Over the eight-paper corpus that was 160
    # processes across 16 targets, every one of them refused at the reconciler afterwards.
    # Spending compute on a run whose result is inadmissible before it starts is not
    # conservatism, it is waste — and it produced the table of sixteen plausible-looking
    # deltas that the admissibility rule then had to catch.
    #
    # With this ON, a diagnostic still runs, and it is written to `runs/<pid>/diagnostics/`
    # as a `DiagnosticRun` rather than a `TargetOutcome`: invisible to the funnel, to
    # `claim_status`, to `overall_verdict`, to `triage` and to the four reader-facing rows.
    # It informs a reader and it decides nothing.
    diagnostic_mode: bool = field(default_factory=lambda: _flag("SH_DIAGNOSTIC_MODE"))
    clone_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_CLONE_TIMEOUT", "600")))
    install_timeout_s: int = field(default_factory=lambda: int(os.environ.get("SH_INSTALL_TIMEOUT", "1800")))
    max_audit_files: int = field(default_factory=lambda: int(os.environ.get("SH_MAX_AUDIT_FILES", "800")))

    @classmethod
    def load(cls) -> "Config":
        # Read `.env.sandbox` BEFORE the field defaults are evaluated, so a token or a
        # gate written there is seen by the `default_factory` lambdas above. Nothing is
        # overridden: an exported variable always wins.
        load_env_files()
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        return cls()

    def open_delegation_gates(self, *, auto_audit: bool = False, auto_grade: bool = False,
                              substantive_verdict: bool = False) -> list[str]:
        """Widen the three delegation gates from an operator's per-invocation FLAGS.
        Returns the names of the gates this call actually opened.

        The defect this exists to fix, verbatim from the shipped strings: `run.py review
        --paper x.pdf --auto-audit --auto-grade` — the command the project's own
        documentation calls "the entrypoint" — set neither gate, so the operator got back
        *"auto-audit gate is closed; set SH_ALLOW_AUTO_AUDIT=1 (or pass --auto-audit)"*
        having just passed `--auto-audit`. The gate and the message that described it
        disagreed, and the message was the one a human read.

        WIDENING ONLY, and never narrowing: a `False` argument leaves a gate that the
        environment opened exactly as it was. An operator who exported
        `SH_ALLOW_GRADING=1` and then ran without `--auto-grade` did not ask for grading
        to be turned off, and a flag default silently closing a gate the environment
        opened is the same class of surprise in the other direction.

        THE REJECTED ALTERNATIVE was to read `sys.argv` here, so the flags would work
        without any caller doing anything. It is one line and it is wrong twice: a
        library that inspects the process's command line opens a gate in any process
        whose argv happens to contain the string — a test runner, a subagent, an
        unrelated tool — and it makes the widening invisible at the call site, which is
        the one place a security-relevant decision has to be readable. So this is a
        method the entrypoint must CALL, and `tests/test_delegation_path.py` asserts that
        the promise the drivers print and the wiring that fulfils it cannot drift apart.
        """
        opened: list[str] = []
        for asked, attr in ((auto_audit, "allow_auto_audit"), (auto_grade, "allow_grading"),
                            (substantive_verdict, "allow_substantive_verdict")):
            if asked and not getattr(self, attr):
                setattr(self, attr, True)
                opened.append(attr)
        return opened

    def has_gpu(self) -> bool:
        """Cheap check that does not import torch into the harness process."""
        return shutil.which("nvidia-smi") is not None
