"""Where execution physically happens — and the one place that decides it may.

Everything above this module reasons about *whether* something should run: the audit
stage decides what is worth checking, `experiment_id` decides which program answers a
cited cell, `repo.assess_capability` decides whether a fair attempt is possible. None of
that changes when the process runs on this laptop, inside WSL, or inside a container.
What changes is six mechanical things — capability probing, environment provisioning,
launching a command, collecting its output, cleaning up, and reporting what platform and
resources are actually on offer — and this module is the seam those six sit behind.

`python -m harness.backends` runs the self-check.

**Two responsibilities, deliberately separated.**

A backend is a *mechanism*. It knows how to start a process and what platform that
process will see. It has no opinion about whether the process ought to start.

`authorize()` is the *policy*. It is a pure function of the spec, the config gates and
the identity/capability records already attached to that spec, and it is the only thing
that may say yes. A backend cannot authorize itself, which is why the decision lives here
as a free function rather than as a method on `ExecutionBackend`: a container backend
added later inherits the policy by construction instead of re-implementing it, and there
is exactly one place to read to find out what the harness is willing to run.

**Why `authorize()` exists at all, given `plan_execution` already checks.**

`stages/probe.plan_execution` refuses to promote a spec to `repo_exec` unless identity
and capability hold — but a hand-written `runs/<pid>/spec.json` never passes through that
promotion. A spec.json carrying `command` and `"provenance": "repo_exec"` reached
`run_probe` and executed, with the repo-exec gate shut and no identity established, and
its crash was eligible to reconcile as FAILED_REPRODUCTION. So the check is repeated at
the point of execution, against the artifact rather than against the control flow that
produced it. The two checks are not redundant; the first decides what to plan, and this
one decides what to run.

**What "unauthorized" costs.** Nothing is executed and no verdict is produced. The
reconciliation is INCONCLUSIVE with `failure_class = "execution_unauthorized"`, which
accuses nobody — refusing to run someone's code is a fact about this harness.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from . import repo as repo_mod, resources as resources_mod
from .artifacts import (CommitVerification, ExecAuthorization, ExecCapability, ProbeSpec,
                        RepoAcquisition)
from .config import Config
from .experiment_id import identities_established

def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(buf) -> str:
    """`TimeoutExpired.stdout` is bytes or str depending on how the child was opened."""
    if buf is None:
        return ""
    return buf.decode("utf-8", "replace") if isinstance(buf, bytes) else str(buf)


# Provenances whose code the *authors* wrote. Only these may be run as repo execution,
# and only these may reconcile against a printed cell — the same ceiling `reconcile`
# enforces, restated here so an unauthorized command cannot even start.
_REPO_PROVENANCE = "repo_exec"


# --------------------------------------------------------------------------- #
# The unit of work, and what came back
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExecRequest:
    """One process to run. Deliberately dumb: argv, where, how long, with what env.

    Frozen because a backend must not be able to edit the command it was handed. The
    argv is built upstream from what the repository itself advertises, and a backend
    that rewrote it would be inventing an invocation while appearing to relay one.
    """

    argv: list[str]
    cwd: str
    timeout_s: int
    env: dict[str, str] | None = None
    label: str = ""                      # e.g. "seed=3 arm=reproduction", for logs only


@dataclass
class ExecOutcome:
    """What a backend observed. Three distinguishable endings, not two.

        launched=False                 the process never existed — this harness failed to
                                       start anything, the clearest possible setup failure
        launched=True,  completed=False killed by the timeout, with whatever it had done
        launched=True,  completed=True  ran to an exit code, which may be non-zero

    Collapsing the first two into "it failed" is what let an unlaunchable command and a
    long-running experiment cut short read as the same event. They are opposite evidence
    about whether the experiment was reached.

    The identity fields — `argv`, `cwd`, `environment`, `started_at`, `ended_at` — are echoed back by the
    backend rather than assumed by the caller. A record that says what the caller INTENDED
    to run is not evidence of what ran; a reviewer checking a reproduction verdict needs
    the command the process was actually given, from the thing that gave it.
    """

    launched: bool
    completed: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    backend: str = ""
    error: str = ""                      # why it did not launch, when launched is False
    argv: list[str] = field(default_factory=list)
    cwd: str = ""
    environment: str = ""                # the backend's own account of where this ran
    started_at: str = ""                 # UTC ISO-8601
    ended_at: str = ""

    @property
    def ok(self) -> bool:
        return self.completed and self.returncode == 0


@dataclass(frozen=True)
class BackendResources:
    """What a backend can actually offer, asked before anything is planned.

    `has_gpu` alone was not enough and is kept only as a convenience. A boolean cannot
    express "8 GiB is not the 24 GiB this experiment declares", which is the comparison
    E1 exists to make — so the quantities are here in bytes, and `None` means the backend
    could not report that quantity rather than that it has none of it.
    """

    name: str
    platform: str                        # what `sys.platform` reports INSIDE the backend
    python: str
    has_gpu: bool = False
    vram_bytes: int | None = None        # the largest single visible accelerator
    ram_bytes: int | None = None
    disk_bytes: int | None = None        # free, where runs are written
    cpu_count: int | None = None
    gpu_count: int | None = None
    gpu_name: str = ""
    python_version: str = ""             # 'major.minor.micro' INSIDE the backend, if knowable
    detail: str = ""


@dataclass(frozen=True)
class BackendAvailability:
    usable: bool
    detail: str = ""


@dataclass(frozen=True)
class BackendProfile:
    """What a backend OFFERS, stated so an experiment's demand can be matched against it.

    Distinct from `BackendResources`, which is what a backend currently measures on the
    machine it is running on. A profile is a declaration, and a declaration is what makes
    selection possible for an environment this host cannot interrogate: nobody here can
    ask a Kaggle notebook how much VRAM it has, but its published specification is a fact
    that can be compared against a 24 GiB requirement.

    `can_execute` is the load-bearing field. A profile whose `can_execute` is False
    describes a place an experiment WOULD fit; it is never a place an experiment RUNS.
    `authorize()` refuses it outright, so a declaration can never become a reproduction.
    """

    name: str
    platform: str
    vram_bytes: int | None = None
    ram_bytes: int | None = None
    disk_bytes: int | None = None
    cpu_count: int | None = None
    gpu_count: int | None = None
    gpu_name: str = ""
    python_version: str = ""
    max_walltime_s: int | None = None
    requires_credentials: bool = False
    can_execute: bool = False
    detail: str = ""
    # ponytail: `network_at_runtime` and `reproducibility` are DECLARED and consulted by
    # nothing. `select_for` cannot match them because no requirement encodes the other
    # half: nothing extracts "this experiment downloads a checkpoint at runtime" from the
    # paper or the repository, and `ResourceRequirement` has no field for it. Inventing
    # one would make an unknown demand look answered, which is the failure `memory_stated`
    # exists to close.
    #
    # The ceiling: an experiment needing a runtime download could be selected for an
    # environment with no runtime network. It fails safe — the download raises during
    # startup, `reached_experiment` is False, and the result is INCONCLUSIVE rather than a
    # reproduction verdict — but it fails wastefully, after provisioning.
    #
    # The upgrade path is an extractor, not a field: a `code_audit` rule over the checkout
    # for `from_pretrained`, `load_dataset`, `hf_hub_download`, `torch.hub.load`, `wget`
    # and `curl`, whose findings carry a file:line the way every other requirement carries
    # a quote. Then a network demand is evidence, and this field has something to match.
    network_at_runtime: bool = False
    reproducibility: str = ""


@dataclass(frozen=True)
class BackendSelection:
    """Which environment can host this experiment, and what was rejected on the way.

    `considered` is not diagnostics padding. An operator reading "resources_insufficient"
    needs to know whether nothing was close or whether one candidate was one tier away,
    and a reader auditing an INCONCLUSIVE needs to see that the refusal was a matching
    result rather than an omission.
    """

    chosen: "ExecutionBackend | None"
    reason_code: str
    reason: str
    considered: tuple[tuple[str, str, str], ...] = ()      # (name, verdict, why)

    @property
    def selected(self) -> bool:
        return self.chosen is not None and self.reason_code == "selected"


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #
class ExecutionBackend(ABC):
    """Five operations to implement, three to override. That is the whole provider contract.

        MUST     resources    what is on offer, in bytes
                 capability    can this repository be given a fair run here
                 provision     build the environment the repository declares
                 execute       start one process, collect its output
                 cleanup       remove only what provisioning created

        MAY      profile       what to MATCH a demand against, when it cannot be measured
                 available     is this reachable right now
                 environment   where a process this backend starts actually runs

    A remote provider is a class implementing the first five and overriding the last
    three. Nothing above this seam changes: the audit, identity and reconciliation layers
    never import a concrete backend and never branch on `backend.name`. They ask for a
    platform, a capability record, an outcome — all backend-independent shapes — so a new
    backend changes where a process runs without changing what any verdict means.

    **What is deliberately not here.** No submit/poll/fetch triple, and no staging call.
    A remote backend blocks inside `execute()` and returns the same `ExecOutcome`, and
    stages the checkout inside `provision()` by returning a `RepoAcquisition` whose `path`
    and `env_path` are meaningful in ITS namespace — which `execute` then interprets as
    `cwd`. Splitting either into lifecycle methods buys asynchronous resume, and nothing
    in this harness resumes an execution today, so it would be an interface shaped around
    a caller that does not exist.

    `platform` is the load-bearing member for future backends. `assess_capability`
    compares a repository's declared platform against the platform the run will actually
    see, and today that is `sys.platform` only because today the only backend is this
    machine. A Linux container backend reports 'linux', and a repository pinned to
    `linux-64` becomes capable without a single change to the capability logic itself.
    """

    name: str = "abstract"

    # --- resource / platform checks ---------------------------------------------------
    @abstractmethod
    def resources(self) -> BackendResources: ...

    def profile(self) -> BackendProfile:
        """What this backend offers, for requirement matching. Measured by default.

        A backend that can be interrogated derives its profile from `resources()`. One
        that cannot — anything remote — overrides this with its published specification.
        """
        r = self.resources()
        return BackendProfile(
            name=self.name, platform=r.platform, vram_bytes=r.vram_bytes,
            ram_bytes=r.ram_bytes, disk_bytes=r.disk_bytes, cpu_count=r.cpu_count,
            gpu_count=r.gpu_count, gpu_name=r.gpu_name, python_version=r.python_version,
            network_at_runtime=True, can_execute=True, detail=r.detail)

    @property
    def platform(self) -> str:
        return self.resources().platform

    def available(self) -> BackendAvailability:
        """Is this backend usable on this machine right now?

        Distinct from `profile().can_execute`, and the distinction is the whole reason both
        exist. `can_execute` is a permanent property — a declaration will never run
        anything. `available` is a property of the moment: a real runner whose provider is
        returning 503 can execute and cannot execute *now*. One is a reason to stop asking;
        the other is a reason to try later, and collapsing them loses that.
        """
        return BackendAvailability(True, "")

    def environment(self) -> str:
        """Where a process this backend starts will actually run, in one line.

        Stamped onto every `ExecOutcome` so the execution record locates itself. Derived
        from `resources()` by default, which is enough for a machine you can point at. A
        remote backend overrides it to name the session it obtained — otherwise a
        reproduction verdict would rest on hardware nobody can identify afterwards.
        """
        r = self.resources()
        return " ".join(x for x in (self.name, r.platform, r.python, r.gpu_name) if x)

    # --- capability checking ------------------------------------------------------------
    @abstractmethod
    def capability(self, acq: RepoAcquisition, interpreter: str,
                   harness_python: str, flag: str = "seed") -> ExecCapability: ...

    # --- environment provisioning -------------------------------------------------------
    @abstractmethod
    def provision(self, cfg: Config, root: Path, pid: str,
                  acq: RepoAcquisition) -> RepoAcquisition: ...

    # --- command execution + output collection ------------------------------------------
    @abstractmethod
    def execute(self, req: ExecRequest) -> ExecOutcome: ...

    # --- cleanup --------------------------------------------------------------------------
    @abstractmethod
    def cleanup(self, root: Path, pid: str) -> list[str]: ...


class LocalBackend(ExecutionBackend):
    """This machine, this OS, subprocesses. Exactly the behaviour that existed before.

    Every method here delegates to the function that already did the job, so extracting
    the interface changed no observable behaviour: `repo.assess_capability` still decides
    capability, `repo.build_env` still provisions, and `subprocess.run` still runs. The
    only thing that moved is who calls them.
    """

    name = "local"

    def resources(self) -> BackendResources:
        cfg = Config.load()
        vram, gpu_name, gpu_count = resources_mod.host_vram_bytes()
        return BackendResources(
            name=self.name, platform=sys.platform, python=cfg.python,
            python_version=".".join(str(n) for n in sys.version_info[:3]),
            has_gpu=cfg.has_gpu(), vram_bytes=vram, gpu_name=gpu_name, gpu_count=gpu_count or None,
            ram_bytes=resources_mod.host_ram_bytes(),
            disk_bytes=resources_mod.host_disk_bytes(cfg.projects_dir),
            cpu_count=os.cpu_count(),
            detail=f"local {sys.platform} host, {gpu_name or 'no GPU detected'}",
        )

    def capability(self, acq: RepoAcquisition, interpreter: str,
                   harness_python: str, flag: str = "seed") -> ExecCapability:
        cap = repo_mod.assess_capability(acq, interpreter, harness_python, flag,
                                         platform=self.platform)
        cap.backend = self.name
        return cap

    def provision(self, cfg: Config, root: Path, pid: str,
                  acq: RepoAcquisition) -> RepoAcquisition:
        return repo_mod.build_env(cfg, root, pid, acq)

    def execute(self, req: ExecRequest) -> ExecOutcome:
        """Run it. Never raises: a backend that throws turns a runner fault into a crash
        halfway through a seed loop, and the caller needs every ending as data."""
        started, t0 = _utc(), time.time()
        env = {**os.environ, **req.env} if req.env else None
        stamp = dict(backend=self.name, argv=list(req.argv), cwd=req.cwd,
                     environment=self.environment(), started_at=started)
        try:
            p = subprocess.run(req.argv, cwd=req.cwd or None, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=req.timeout_s,
                               env=env)
        except subprocess.TimeoutExpired as e:
            # A timeout is not an absence of evidence. Whatever the process printed before
            # it was killed is the only thing that can say whether the experiment started,
            # and `reached_experiment` reads exactly that.
            return ExecOutcome(launched=True, completed=False, timed_out=True,
                               stdout=_text(e.stdout), stderr=_text(e.stderr),
                               seconds=round(time.time() - t0, 3), ended_at=_utc(),
                               error=f"timeout after {req.timeout_s}s", **stamp)
        except OSError as e:
            return ExecOutcome(launched=False, completed=False,
                               seconds=round(time.time() - t0, 3), ended_at=_utc(),
                               error=f"could not start: {e}", **stamp)
        return ExecOutcome(launched=True, completed=True, returncode=p.returncode,
                           stdout=p.stdout or "", stderr=p.stderr or "",
                           seconds=round(time.time() - t0, 3), ended_at=_utc(), **stamp)

    def cleanup(self, root: Path, pid: str) -> list[str]:
        """Remove the provisioned environment for one paper. Returns what was removed.

        Only the directory this backend created is touched — never the checkout, which
        is the evidence the static audit cites, and never `runs/<pid>` itself, which
        holds the probe source and results a reader needs to check the report.
        """
        env_dir = Path(root) / "runs" / pid / "env"
        if not env_dir.is_dir():
            return []
        shutil.rmtree(env_dir, ignore_errors=True)
        return [str(env_dir)]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class DeclaredBackend(ExecutionBackend):
    """An environment this harness knows the specification of but cannot drive.

    Kaggle and Colab are real places an experiment could run, with published hardware, and
    neither can be provisioned from this host: there is no API here, no credentials, and
    no way to move a checkout into a notebook session. Registering them as declarations
    rather than omitting them is what lets selection produce a useful refusal — "a 16 GiB
    T4 would fit this experiment but cannot be provisioned from here" tells an operator
    what to do next, where a bare "no backend" does not.

    Every path that could execute is closed rather than stubbed. `available()` is False,
    `can_execute` is False so `authorize()` refuses before anything is attempted, and
    `execute()` raises. There is no integration here to go stale or to be mistaken for
    one, and no code path by which a declaration becomes a reproduction verdict.
    """

    spec: BackendProfile
    why_unavailable: str = "this environment cannot be provisioned from this host"

    def resources(self) -> BackendResources:
        p = self.spec
        return BackendResources(
            name=p.name, platform=p.platform, python="", python_version=p.python_version,
            has_gpu=bool(p.vram_bytes),
            vram_bytes=p.vram_bytes, ram_bytes=p.ram_bytes, disk_bytes=p.disk_bytes,
            cpu_count=p.cpu_count, gpu_count=1 if p.vram_bytes else None,
            gpu_name=p.gpu_name, detail=p.detail)

    def profile(self) -> BackendProfile:
        return self.spec

    def available(self) -> BackendAvailability:
        return BackendAvailability(False, self.why_unavailable)

    def capability(self, acq: RepoAcquisition, interpreter: str,
                   harness_python: str, flag: str = "seed") -> ExecCapability:
        return ExecCapability(
            established=False, reason_code="environment_incompatible", backend=self.name,
            detail=f"'{self.name}' is a declared environment, not a runner: {self.why_unavailable}")

    def provision(self, cfg: Config, root: Path, pid: str,
                  acq: RepoAcquisition) -> RepoAcquisition:
        acq.env_status = "blocked"
        acq.reason = f"'{self.name}' cannot be provisioned from this host"
        return acq

    def execute(self, req: ExecRequest) -> ExecOutcome:
        raise NotImplementedError(
            f"'{self.name}' is a declaration of an environment's specification, not an "
            f"integration with it. Nothing here can run {req.argv[:1]}. Reaching this line "
            f"means an authorization gate was bypassed.")

    def cleanup(self, root: Path, pid: str) -> list[str]:
        return []


class KaggleBackend(DeclaredBackend):
    """Kaggle free tier, as published: one T4, ~13 GiB RAM, a 12-hour session ceiling."""

    name = "kaggle"
    why_unavailable = ("Kaggle sessions need an account, an API token and a notebook upload; "
                       "this harness holds no credentials and cannot provision one")
    spec = BackendProfile(
        name="kaggle", platform="linux", vram_bytes=16 * (1024 ** 3),
        ram_bytes=13 * (1024 ** 3), disk_bytes=73 * (1024 ** 3), cpu_count=4,
        gpu_count=1, gpu_name="Tesla T4", max_walltime_s=12 * 3600, network_at_runtime=False,
        requires_credentials=True, can_execute=False, reproducibility="session",
        detail="Kaggle free tier, published specification")


class ColabBackend(DeclaredBackend):
    """Google Colab free tier, as published: one T4, ~13 GiB RAM, pre-emptible."""

    name = "colab"
    why_unavailable = ("Colab sessions are interactive, credentialed and pre-emptible; there "
                       "is no way to drive one from this host")
    spec = BackendProfile(
        name="colab", platform="linux", vram_bytes=16 * (1024 ** 3),
        ram_bytes=13 * (1024 ** 3), disk_bytes=78 * (1024 ** 3), cpu_count=2,
        gpu_count=1, gpu_name="Tesla T4", max_walltime_s=12 * 3600, network_at_runtime=False,
        requires_credentials=True, can_execute=False, reproducibility="session",
        detail="Google Colab free tier, published specification")


class UnknownBackend(ValueError):
    """The operator named a backend that is not registered."""


_REGISTRY: dict[str, type[ExecutionBackend]] = {
    "local": LocalBackend,
    # Declarations, not integrations. They exist so `select_for` can say WHY an experiment
    # has nowhere to run — see DeclaredBackend. Neither can execute anything.
    "kaggle": KaggleBackend,
    "colab": ColabBackend,
}


def register_backend(name: str, cls: type[ExecutionBackend]) -> None:
    _REGISTRY[name] = cls


def registered_backends() -> list[str]:
    return sorted(_REGISTRY)


def select_backend(cfg: Config) -> ExecutionBackend:
    """The backend the operator asked for, by name.

    Raises on an unregistered name rather than falling back to local. A typo in
    `SH_EXEC_BACKEND` is an operator error, and quietly running a Linux repository on
    this Windows host because 'docker' was misspelled is precisely the substitution
    every other check in this harness exists to prevent. The caller decides what an
    unknown backend means for a review; here it is simply refused.
    """
    name = (getattr(cfg, "exec_backend", "") or "local").strip().lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise UnknownBackend(
            f"execution backend '{name}' is not registered; known backends: "
            f"{', '.join(registered_backends())}")
    return cls()


def backend_for(cfg: Config, spec: ProbeSpec) -> ExecutionBackend | None:
    """The backend this spec would run on, or None when the operator named an unknown one.

    Harness-authored code always runs locally — see `local_backend`. Only a spec carrying
    the repository's own command is subject to selection, and an unresolvable selection
    yields None rather than a substitute, so `authorize` refuses instead of running a
    Linux repository on whatever host happened to be available.
    """
    if not spec.command:
        return local_backend()
    try:
        return select_backend(cfg)
    except UnknownBackend:
        return None


def _fits(need: int | None, have: int | None) -> bool:
    """Does a declared demand fit a declared offer? Unknown on EITHER side does not fit.

    An unmeasured offer is not a large one, and an unstated demand is not a small one —
    the same asymmetry `assess_resources` encodes, applied to matching.
    """
    return need is None or (have is not None and need <= have)


def select_for(requirement, cfg: Config, declared_platform: str = "",
               walltime_s: int | None = None) -> BackendSelection:
    """Which registered environment can host THIS experiment. A matching problem.

    Not a configuration lookup. `select_backend` answers "which backend did the operator
    name"; this answers "which backend can run an experiment that declares 24 GiB of VRAM
    and a Linux-only stack", which is a different question and the one that has to be
    asked before a reproduction is attempted.

    Candidates are ranked so that a runnable backend always beats a declared one, and
    among runnable ones the smallest sufficient environment wins — a reproduction should
    not silently claim a larger machine than it needs.

    The refusal codes are ordered by what is most useful to act on, closest-to-a-yes
    first: `backend_unavailable` (a real runner that fits, merely unreachable now) then
    `credentials_unavailable` ("somewhere could run this, but not from here") then
    `platform_incompatible` and `resources_insufficient`. Only the first can resolve
    itself; the rest need an operator, a different machine, or nothing at all.
    """
    if requirement is None or not getattr(requirement, "stated", False):
        return BackendSelection(
            None, "requirement_unknown",
            "the experiment's resource demand was never established, so no environment can "
            "be shown to host it; matching a backend against an unknown demand would be "
            "choosing one at random")

    if getattr(requirement, "walltime_s", None) and walltime_s is None:
        walltime_s = requirement.walltime_s

    runnable: list[tuple[int, ExecutionBackend, BackendProfile]] = []
    declared: list[tuple[ExecutionBackend, BackendProfile]] = []
    considered: list[tuple[str, str, str]] = []

    for name in registered_backends():
        backend = _REGISTRY[name]()
        p = backend.profile()

        if declared_platform and declared_platform not in (p.platform or ""):
            considered.append((name, "platform_incompatible",
                               f"the repository declares a {declared_platform}-only stack and "
                               f"'{name}' offers {p.platform or 'an unknown platform'}"))
            continue

        short = [lbl for lbl, need, have in
                 (("VRAM", requirement.vram_bytes, p.vram_bytes),
                  ("RAM", requirement.ram_bytes, p.ram_bytes),
                  ("disk", requirement.disk_bytes, p.disk_bytes),
                  ("CPU", requirement.cpu_count, p.cpu_count),
                  # A multi-GPU experiment does not fit a single-GPU environment, and
                  # `assess_resources` has always said so. Omitting the count here let
                  # selection report `selected: local` for an 8-GPU experiment that the
                  # resource check then refused as `insufficient` — two components
                  # disagreeing about the same experiment on the same host, with the
                  # optimistic one written into `spec.backend_selection`.
                  ("GPU count", requirement.gpu_count, p.gpu_count))
                 if not _fits(need, have)]
        if walltime_s and p.max_walltime_s and walltime_s > p.max_walltime_s:
            short.append("walltime")
        if short:
            considered.append((name, "resources_insufficient",
                               f"'{name}' cannot meet: {', '.join(short)}"))
            continue

        if not p.can_execute:
            considered.append((name, "credentials_unavailable",
                               f"'{name}' has the hardware for this experiment but "
                               f"{backend.available().detail}"))
            declared.append((backend, p))
            continue
        if not backend.available().usable:
            considered.append((name, "unavailable", backend.available().detail))
            continue

        considered.append((name, "fits", f"'{name}' meets every stated requirement"))
        # Rank by VRAM so the smallest sufficient environment is preferred.
        runnable.append((p.vram_bytes or 0, backend, p))

    frozen = tuple(considered)
    offline = [w for _, v, w in considered if v == "unavailable"]
    if runnable and not requirement.memory_stated:
        # A candidate met everything that was stated, and the memory demand was not among
        # it. Choosing it would be reporting `selected` for an experiment whose deciding
        # quantity nobody established — and `assess_resources` would then refuse the same
        # spec as `unknown`, so selecting here would only put a contradiction on the spec.
        #
        # Checked at this point rather than beside the `stated` guard at the top so a
        # candidate that WAS ruled out on platform, size or credentials still reports that
        # specific reason instead of being flattened into ignorance.
        return BackendSelection(
            None, "requirement_unknown",
            "an environment meets every requirement this experiment states, but its memory "
            "demand was never established, so no environment can be SHOWN to host it. "
            + (f"Unstated: {', '.join(requirement.unstated)}." if requirement.unstated else ""),
            frozen)
    if runnable:
        runnable.sort(key=lambda t: t[0])
        backend, p = runnable[0][1], runnable[0][2]
        return BackendSelection(backend, "selected",
                                f"'{p.name}' meets every stated requirement of this experiment",
                                frozen)
    if offline:
        # Ranked above a declaration, and above the size and platform reports. A backend
        # that fits the experiment AND can run it is the closest thing to a yes the
        # registry holds: it needs no credentials, no account and no operator setup, only
        # a later attempt. It is the one refusal in this function that may resolve itself
        # without anything about the paper or the host changing, which is why it is
        # reported ahead of the ones that will not.
        return BackendSelection(
            None, "backend_unavailable",
            "an environment that can host this experiment is registered but not reachable: "
            + "; ".join(offline), frozen)
    if declared:
        names = ", ".join(p.name for _, p in declared)
        return BackendSelection(
            None, "credentials_unavailable",
            f"the experiment fits {names}, but that environment cannot be provisioned from "
            f"this host, so no reproduction can be attempted. This is a limit of the runner, "
            f"not a fact about the paper.", frozen)
    if any(v == "platform_incompatible" for _, v, _ in considered) and \
            not any(v == "resources_insufficient" for _, v, _ in considered):
        return BackendSelection(None, "platform_incompatible",
                                "no registered environment runs the platform this repository "
                                "declares", frozen)
    if considered:
        return BackendSelection(
            None, "resources_insufficient",
            "no registered environment is large enough for the experiment as published: "
            + "; ".join(why for _, v, why in considered if v == "resources_insufficient"), frozen)
    return BackendSelection(None, "no_backend", "no execution backend is registered", frozen)


def local_backend() -> LocalBackend:
    """The backend that runs code THIS harness authored.

    Kept separate from `select_backend` on purpose. A generated probe and a noise-floor
    calibration are our own code, measuring this machine — running them somewhere else
    would measure a different machine and answer a different question. Only third-party
    repository execution is subject to backend selection.
    """
    return LocalBackend()


# --------------------------------------------------------------------------- #
# Policy — the only thing that may say yes
# --------------------------------------------------------------------------- #
def authorize(cfg: Config, spec: ProbeSpec, backend: ExecutionBackend | None,
              commit: CommitVerification | None = None) -> ExecAuthorization:
    """May this spec execute? Pure function of the spec, the gates, the backend, and a
    FRESH commit verification supplied by the caller.

    Ordered so the reported reason is the one an operator can act on first, and so a
    blocker that is about *us* is never reported as a blocker about the paper:

      1. not repository execution at all — our own code, no repo gates apply
      2. no usable backend
      3. the repo-exec gate is shut
      4. provenance is not the authors' own checkout
      5. the checkout is not provably the audited commit                     (E2)
      6. experiment / metric / configuration identity is not established
      7. this machine cannot give the code a fair run
      8. the published experiment does not fit this backend's hardware       (E1)

    Provenance and commit sit together, before identity, because they answer the same
    prior question — is this the right code at all — and reasoning about which experiment
    some other commit implements is reasoning about the wrong artifact.

    Identity precedes capability because a capable run of the wrong program is worse
    than a crash: a crash is visible, and a confident number from the wrong experiment
    is not. Resources come last of the substantive checks only because they are the most
    expensive fact to be wrong about in the other direction — an experiment that does not
    fit produces an OOM that reads, at the stderr, exactly like broken code.

    `commit` is a PARAMETER rather than a field read off the spec, and defaults to None,
    which refuses. That is deliberate: the check has to be made against the disk at the
    moment of execution, and a caller that forgets to make it must fail closed rather
    than inherit a verification made minutes earlier against a checkout since replaced.
    """
    if not spec.command:
        # A generated probe, a driver's script, or the identical-arms template. This is
        # code we or the operator authored, running under our own interpreter, and the
        # provenance ceiling in `reconcile` already caps what it may conclude.
        return ExecAuthorization(
            allowed=True, decision="not_repo_execution", backend=local_backend().name,
            detail="the code under test was authored by this harness or its operator, "
                   "not fetched from the paper's repository")

    if backend is None:
        return ExecAuthorization(allowed=False, decision="no_backend", backend="",
                                 failure_class="backend_unavailable",
                                 detail="no execution backend is available for this experiment")

    name = backend.name
    # A declared environment describes a place an experiment WOULD fit. It is never a
    # place one RUNS. Refusing here, before the gate and before every scientific
    # precondition, means no path exists by which a specification becomes a reproduction
    # verdict — `DeclaredBackend.execute` raises, and this is the check that ensures the
    # raise is unreachable rather than load-bearing.
    #
    # Ahead of the generic availability check on purpose. A declaration is also
    # "unavailable", and reporting it that way would answer "no usable backend" when the
    # useful answer is "a 16 GiB T4 would host this, but it needs credentials this
    # harness does not hold" — which is the difference between a dead end and a next step.
    if not backend.profile().can_execute:
        return ExecAuthorization(
            allowed=False, decision="backend_cannot_execute", backend=name,
            failure_class="credentials_unavailable" if backend.profile().requires_credentials
            else "backend_unavailable",
            detail=f"'{name}' is a declared environment, not a runner: "
                   f"{backend.available().detail}")

    if not backend.available().usable:
        # `backend_offline`, not `no_backend`. The registry HAS a runner for this
        # experiment; it cannot be reached at this moment. An operator reading
        # "no backend" looks for one to add, which is the wrong next step.
        return ExecAuthorization(allowed=False, decision="backend_offline", backend=name,
                                 failure_class="backend_unavailable",
                                 detail=f"'{name}' can execute but is not reachable now: "
                                        f"{backend.available().detail}")

    if not cfg.allow_repo_exec:
        return ExecAuthorization(
            allowed=False, decision="gate_closed", backend=name,
            failure_class="execution_unauthorized",
            detail="SH_ALLOW_REPO_EXEC is not set; running a third-party repository "
                   "stays an explicit per-invocation opt-in")

    if spec.provenance != _REPO_PROVENANCE:
        return ExecAuthorization(
            allowed=False, decision="provenance_insufficient", backend=name,
            failure_class="execution_unauthorized",
            detail=f"a command was set but provenance is '{spec.provenance}', not "
                   f"'{_REPO_PROVENANCE}'; only the authors' own checkout is run as repository code")

    # --- E2: the code that runs must be the code that was audited ---------------------
    if commit is None or not commit.established:
        detail = (commit.reason if commit is not None
                  else "the checkout was not verified against the audited commit before "
                       "execution, so it cannot be shown to be the code the audit read")
        return ExecAuthorization(allowed=False, decision="commit_unverified", backend=name,
                                 failure_class="commit_mismatch", detail=detail)

    proven, cls, why = identities_established(
        spec.experiment, spec.metric_identity, spec.configuration)
    if not proven:
        return ExecAuthorization(allowed=False, decision="identity_unproven", backend=name,
                                 failure_class=cls, detail=why)

    cap = spec.capability
    if cap is None or not cap.established:
        return ExecAuthorization(
            allowed=False, decision="capability_unproven", backend=name,
            failure_class="execution_unauthorized",
            detail=(cap.detail if cap is not None
                    else "execution capability was never assessed for this spec"))

    # --- E1: the published experiment must fit, and no substitute is offered -----------
    res = spec.resources
    if res is None or not res.established:
        return ExecAuthorization(
            allowed=False, decision="resources_unproven", backend=name,
            failure_class="resources_insufficient",
            detail=(res.reason if res is not None
                    else "the experiment's resource demand was never assessed against this "
                         "backend, so it cannot be shown to fit"))

    # The records above were produced against ONE backend. `spec.backend` names it, and a
    # capability or resource assessment made for a different machine says nothing about
    # this one — an 8 GiB card's `satisfied` is not an 80 GiB card's, and a `win32`
    # capability is not a `linux` capability. `ProbeSpec.backend` exists to carry exactly
    # this, so it is checked rather than assumed.
    planned = (spec.backend or "").strip()
    if planned and planned != name:
        return ExecAuthorization(
            allowed=False, decision="backend_mismatch", backend=name,
            failure_class="execution_unauthorized",
            detail=f"capability and resources were assessed against '{planned}' but execution "
                   f"was requested on '{name}'; an assessment of one environment is not an "
                   f"assessment of another")

    return ExecAuthorization(
        allowed=True, decision="authorized", backend=name, failure_class="none",
        detail=f"identity established, capability established, the published experiment fits "
               f"this backend, the checkout is the audited commit {commit.actual[:12]}, "
               f"provenance is the authors' own checkout, and the execution gate is open")


if __name__ == "__main__":  # self-check: python -m harness.backends
    import tempfile

    cfg = Config.load()
    backend = select_backend(cfg)
    assert backend.name == "local"
    assert isinstance(backend, LocalBackend)
    res = backend.resources()
    assert res.platform == sys.platform and res.python
    assert backend.available().usable
    # E1 needs quantities, not a boolean. A backend that cannot say how much it has must
    # report None, which blocks — never 0, which would read as "has none" and never a
    # bare True, which cannot be compared with a declared 24 GiB at all.
    assert res.cpu_count and res.disk_bytes, "cpu and disk are always measurable"
    for field in ("vram_bytes", "ram_bytes", "gpu_count"):
        assert getattr(res, field) is None or getattr(res, field) > 0, field

    # --- execution + output collection -----------------------------------------------
    out = backend.execute(ExecRequest([cfg.python, "-c", "print('SH_DEVICE cpu')"], "", 60))
    assert out.launched and out.completed and out.ok, out.stderr
    assert "SH_DEVICE cpu" in out.stdout and out.backend == "local"

    bad = backend.execute(ExecRequest([cfg.python, "-c", "raise SystemExit(3)"], "", 60))
    assert bad.launched and bad.completed and bad.returncode == 3 and not bad.ok

    nope = backend.execute(ExecRequest(["sh-no-such-binary-anywhere"], "", 60))
    assert not nope.launched and not nope.completed, "an unlaunchable command must say so"
    assert "could not start" in nope.error

    slow = backend.execute(ExecRequest([cfg.python, "-c", "import time; time.sleep(5)"], "", 1))
    assert slow.launched and not slow.completed and slow.timed_out, "a timeout is not a failed launch"

    # --- the provider-neutral contract -------------------------------------------------
    # Every registered backend, not just the one that runs here. A new provider is a class
    # and a profile; if either of these fails, something above the seam would have to change.
    for _name in registered_backends():
        _b = _REGISTRY[_name]()
        assert _b.name == _name and _b.profile().name == _name
        assert _b.resources().platform, f"{_name} must say what platform it presents"
        assert _b.environment(), f"{_name} must be able to say where a run happens"

    # Where it ran, as the backend itself reports it. Locally the hardware is implicit;
    # remotely it is the only record of what a verdict came from.
    assert out.environment == backend.environment() and sys.platform in out.environment

    # --- registry ----------------------------------------------------------------------
    assert "local" in registered_backends()
    try:
        select_backend(type("C", (), {"exec_backend": "docker-typo", "allow_repo_exec": False})())
        raise AssertionError("an unregistered backend name must be refused, not substituted")
    except UnknownBackend:
        pass

    # --- authorization: the invariant --------------------------------------------------
    from .artifacts import (ConfigurationIdentity, ExperimentIdentity, MetricIdentity,
                            ResourceCapability)

    own = ProbeSpec(paper_id="s")
    assert authorize(cfg, own, backend).allowed, "our own probe is not repository execution"
    assert authorize(cfg, own, backend).decision == "not_repo_execution"

    repo_spec = ProbeSpec(paper_id="s", command=["python", "eval.py"], provenance="repo_exec")
    a = authorize(cfg, repo_spec, backend)
    assert not a.allowed and a.decision == "gate_closed", a.decision
    assert a.failure_class == "execution_unauthorized"

    open_cfg = Config.load()
    open_cfg.allow_repo_exec = True

    # E2 — with the gate open, an unverified checkout is the next thing in the way, and
    # omitting the verification entirely must refuse rather than wave through.
    a = authorize(open_cfg, repo_spec, backend)
    assert not a.allowed and a.decision == "commit_unverified", a.decision
    good = CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)
    moved = CommitVerification(state="mismatch", expected="a" * 40, actual="b" * 40,
                               reason="HEAD moved")
    assert authorize(open_cfg, repo_spec, backend, commit=moved).decision == "commit_unverified"

    a = authorize(open_cfg, repo_spec, backend, commit=good)
    assert not a.allowed and a.decision == "identity_unproven", a.decision

    est = dict(state="established", established=True, reason="self-check fixture")
    repo_spec.experiment = ExperimentIdentity(**est)
    repo_spec.metric_identity = MetricIdentity(**est)
    repo_spec.configuration = ConfigurationIdentity(**est)
    a = authorize(open_cfg, repo_spec, backend, commit=good)
    assert not a.allowed and a.decision == "capability_unproven", a.decision

    repo_spec.capability = ExecCapability(established=True, reason_code="established")
    # E1 — capable and identified is still not enough: the experiment has to fit, and an
    # unassessed demand is not a small one.
    a = authorize(open_cfg, repo_spec, backend, commit=good)
    assert not a.allowed and a.decision == "resources_unproven", a.decision
    tight = ResourceCapability(state="insufficient", reason="24 GiB required, 8 GiB present")
    assert authorize(open_cfg, repo_spec.model_copy(update={"resources": tight}), backend,
                     commit=good).decision == "resources_unproven"

    repo_spec.resources = ResourceCapability(state="satisfied", reason="fits")
    a = authorize(open_cfg, repo_spec, backend, commit=good)
    assert a.allowed and a.decision == "authorized", a.decision

    # Provenance is checked independently of everything else: a fully identified,
    # fully capable spec that is not the authors' own code still may not run as one.
    laundered = repo_spec.model_copy(update={"provenance": "synthesized"})
    assert authorize(open_cfg, laundered, backend,
                     commit=good).decision == "provenance_insufficient"

    assert not authorize(open_cfg, repo_spec, None, commit=good).allowed, "no backend, no run"

    # A runner that is down is not an absent runner, and neither is a declaration. Three
    # refusals, three names — collapsing them would send an operator looking for a backend
    # to add when the registry already holds one that would have run the experiment.
    class _Down(LocalBackend):
        name = "down"

        def available(self) -> BackendAvailability:
            return BackendAvailability(False, "the provider API returned 503")

    decisions = {authorize(open_cfg, repo_spec, None, commit=good).decision,
                 authorize(open_cfg, repo_spec, _Down(), commit=good).decision,
                 authorize(open_cfg, repo_spec, KaggleBackend(), commit=good).decision}
    assert decisions == {"no_backend", "backend_offline", "backend_cannot_execute"}, decisions

    # ...and selection reports the same three distinctly. An unreachable runner used to
    # land in `resources_insufficient` with an empty reason, having been rejected for
    # nothing of the kind.
    from .artifacts import ResourceEvidence, ResourceRequirement
    _fitting = ResourceRequirement(vram_bytes=2 * (1024 ** 3),
                                   evidence=[ResourceEvidence(quote="q", kind="declared_requirement")])
    _saved = dict(_REGISTRY)
    try:
        _REGISTRY.clear()
        _REGISTRY["down"] = _Down
        _sel = select_for(_fitting, cfg)
        assert _sel.reason_code == "backend_unavailable" and _sel.chosen is None, _sel.reason_code
        assert "503" in _sel.reason
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(_saved)

    # Selection and the resource check must agree. They did not about GPU count: an 8-GPU
    # experiment selected the single-card local backend, which `assess_resources` then
    # refused — the optimistic answer being the one written onto the spec.
    _multi = ResourceRequirement(vram_bytes=1024 ** 3, gpu_count=8,
                                 evidence=[ResourceEvidence(quote="q", kind="declared_requirement")])
    assert select_for(_multi, cfg).reason_code == "resources_insufficient"
    assert resources_mod.assess_resources(_multi, res, backend="local").state == "insufficient"

    # --- cleanup -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert backend.cleanup(root, "p") == [], "nothing provisioned means nothing removed"
        env_dir = root / "runs" / "p" / "env"
        env_dir.mkdir(parents=True)
        (env_dir / "marker").write_text("x", encoding="utf-8")
        keep = root / "runs" / "p" / "probe.py"
        keep.write_text("# evidence", encoding="utf-8")
        removed = backend.cleanup(root, "p")
        assert removed == [str(env_dir)] and not env_dir.exists()
        assert keep.exists(), "cleanup must not remove the artifacts a reader checks"

    print("backends self-check OK")
