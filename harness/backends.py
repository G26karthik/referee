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

import json
import os
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from . import (container as container_mod, isolation as isolation_mod, repo as repo_mod,
               resources as resources_mod)
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
_REIMPL_PROVENANCE = "reimpl_exec"


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
    # HOW STRONGLY this backend confines code it did not write. See `harness.isolation`.
    #
    # Distinct from `can_execute`, and both are required for repository execution:
    # `can_execute` says a process can be started here at all, `isolation` says whether
    # starting a THIRD PARTY's process here is safe. The local backend answers yes to the
    # first and VENV to the second, which is exactly the combination that ran a paper's
    # repository under the operator's own user with their filesystem and network in reach.
    #
    # Defaults to NONE so a backend that forgets to declare one is refused rather than
    # trusted — the same fail-closed direction as `provenance.admits`.
    isolation: str = "NONE"
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
    # HOW STRONGLY this backend confines code it did not write (`harness.isolation`).
    # Declared per class rather than derived, because no runtime probe can establish it:
    # a backend that confines knows that it does, and one that does not cannot be asked.
    # NONE by default so a new backend that forgets to declare a level is refused
    # repository execution rather than silently granted it.
    isolation: str = "NONE"

    def __init__(self, cfg: Config | None = None) -> None:
        self._cfg = cfg

    def config(self) -> Config:
        """The config this backend was SELECTED with, falling back to the environment's.

        `resources()`, `profile()` and `available()` take no arguments — they answer
        questions about the backend, not about a request — but the answers depend on
        settings: which interpreter the local backend would use, which accelerator a
        reservation asks for, whether a gate is open. Re-reading the environment inside
        each of them made those answers ignore the `cfg` the caller was actually holding,
        so a selection made against one configuration could be reported against another.

        Optional, and defaulting to a fresh load, because the registry constructs backends
        by name with no arguments in the interface conformance checks and in
        `registered_backends`. `select_backend` and `select_for` pass the config through.
        """
        return self._cfg if self._cfg is not None else Config.load()

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
            network_at_runtime=True, can_execute=True,
            # The measured default describes a backend that runs on THIS host under an
            # interpreter this harness built, which is VENV and not more. A backend that
            # genuinely confines overrides `profile()` and says so; `LocalBackend` is the
            # one that reaches this line, and VENV is the honest answer for it.
            isolation=self.isolation, detail=r.detail)

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

    def release(self, root: Path, pid: str) -> tuple[bool, str]:
        """Give back leased compute, keeping every artifact. Default: nothing was leased.

        Distinct from `cleanup`, and the distinction is a lifetime. `cleanup` removes what
        provisioning wrote to durable storage — a venv that can be rebuilt — and is called
        when an operator wants the disk back. `release` ends a *lease*: a machine that is
        billing right now and whose only reason to exist was this paper.

        Two different callers follow from that. Nothing calls `cleanup` in the pipeline,
        deliberately: a built environment is worth keeping between runs. `stages/probe.run`
        calls `release` in a `finally`, because a paper that crashes mid-review must not
        leave a machine running — that is the one failure mode in this seam that costs the
        operator money rather than accuracy, and it accrues silently.
        """
        return False, "this backend leases nothing, so there is nothing to release"

    def commit_tree(self, cwd: str) -> "repo_mod.GitTree | None":
        """The tree `verify_commit` must read to certify what THIS backend will run.

        `None` means the local path, which is the whole story for a backend that runs on
        this machine. A backend that runs elsewhere returns a reader over the checkout it
        will actually execute — so E2 is enforced against the tree that runs rather than
        against a same-named directory on the operator's disk.
        """
        return None


class LocalBackend(ExecutionBackend):
    """This machine, this OS, subprocesses. Exactly the behaviour that existed before.

    Every method here delegates to the function that already did the job, so extracting
    the interface changed no observable behaviour: `repo.assess_capability` still decides
    capability, `repo.build_env` still provisions, and `subprocess.run` still runs. The
    only thing that moved is who calls them.
    """

    name = "local"
    # A venv scopes imports and wall time. It does not confine the filesystem, the
    # network, the environment or the user, so it may not host the authors' own code --
    # see `harness.isolation` and `authorize`'s isolation condition. Declaring anything
    # stronger here would be a claim this process cannot back up.
    isolation = "VENV"

    def resources(self) -> BackendResources:
        cfg = self.config()
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


class ContainerBackend(ExecutionBackend):
    """A Linux container on this host. The first backend entitled to run a paper's code.

    The mechanics are in `harness/container.py`; this class is the seam. Everything above
    it is unchanged: `authorize()` is still the only thing that may permit a run,
    `verify_commit` still decides whether the tree is the audited one, `assess_capability`
    still decides whether the repository gets a fair run, and `local_exec.reconcile` still
    decides what a number may conclude. A containerised reproduction and a local one pass
    through the identical gates in the identical order.

    **Three things it does differently.**

    *The platform the run will see is linux, and it is measured.* `assess_capability`
    compares a repository's declared environment against the backend's platform rather
    than against `sys.platform`, and that parameter existed for exactly this backend. A
    `linux-64` conda environment is `environment_incompatible` on the Windows host and
    capable here, with no change to the capability logic itself.

    *The import probe is delegated to the interpreter that will run it.* The venv lives
    inside the container's filesystem view, so resolving its modules from the host would
    start nothing and report every dependency missing — refusing a capable environment as
    `dependency_missing`. `assess_capability` takes a resolver for this reason.

    *The commit is NOT re-verified in a second place, and that is a real difference from
    the remote sandbox rather than a shortcut.* The sandbox stages a copy onto another
    machine, so it has to certify the copy. A bind mount is not a copy: the container
    reads the same bytes on the same disk that `verify_commit` already certified, so
    `commit_tree` returning None means "the local tree IS the tree that runs", which is
    true here and false there.

    **What it does not do.** It does not fall back. A daemon that is not running, an image
    that will not pull, an environment that will not build and a container that will not
    start are each reported as themselves and refuse, because a fallback to the host would
    make the isolation requirement advisory.
    """

    name = "container"
    # The repository gets its own filesystem, process namespace, user and network stack,
    # and does not carry the operator's home directory, credentials or PATH. That is what
    # CONTAINER means in `harness.isolation`, and it is the property that makes running a
    # stranger's training script defensible. It is not a claim of adversarial containment
    # against a shared kernel, and `harness/container.py` says so in those words.
    isolation = "CONTAINER"

    # `available()` and `resources()` are called by `select_for` for EVERY registered
    # backend on EVERY paper, and the GPU probe starts a container. Cached per process so
    # a review does not pay for it once per target.
    _gpu_cache: "tuple[bool, str] | None" = None

    def _gpu(self) -> tuple[bool, str]:
        if ContainerBackend._gpu_cache is None:
            if not self.available().usable:
                ContainerBackend._gpu_cache = (False, "no reachable container runtime")
            else:
                ContainerBackend._gpu_cache = container_mod.gpu_available()
        return ContainerBackend._gpu_cache

    def resources(self) -> BackendResources:
        """What a container on this host actually gets. Measured from the daemon.

        The memory figure is the daemon's, not the host's. On a Desktop install those
        differ — the Linux VM is given a slice of the machine — and reporting the host's
        would hand `select_for` a machine that does not exist, turning an honest resource
        refusal into a crash inside the experiment.
        """
        cfg = self.config()
        inv = container_mod.inventory()
        has_gpu, gpu_detail = self._gpu()
        vram, gpu_name, gpu_count = (resources_mod.host_vram_bytes() if has_gpu
                                     else (0, "", None))
        return BackendResources(
            name=self.name, platform="linux", python="python3",
            python_version="", has_gpu=has_gpu, vram_bytes=vram,
            gpu_name=(gpu_name or gpu_detail if has_gpu else ""), gpu_count=gpu_count or None,
            ram_bytes=inv.get("ram_bytes") or 0,
            # The mount is a host directory, so the disk a run can fill is the host's.
            disk_bytes=resources_mod.host_disk_bytes(cfg.projects_dir),
            cpu_count=inv.get("cpus"),
            detail=(inv.get("detail") or "container runtime")
                   + (f", {gpu_detail}" if has_gpu else ", no GPU in containers"))

    def profile(self) -> BackendProfile:
        r = self.resources()
        return BackendProfile(
            name=self.name, platform="linux", vram_bytes=r.vram_bytes, ram_bytes=r.ram_bytes,
            disk_bytes=r.disk_bytes, cpu_count=r.cpu_count, gpu_count=r.gpu_count,
            gpu_name=r.gpu_name, python_version=r.python_version,
            network_at_runtime=False, can_execute=True, isolation=self.isolation,
            reproducibility="container", detail=r.detail)

    def available(self) -> BackendAvailability:
        ok, why = container_mod.daemon_status()
        return BackendAvailability(ok, why)

    def environment(self) -> str:
        r = self.resources()
        return " ".join(x for x in (self.name, r.platform, r.detail) if x)

    # --- capability -------------------------------------------------------------------
    def _mount(self, acq: RepoAcquisition) -> str:
        """The host directory bind-mounted into the container.

        `runs/<pid>` holds both halves a run needs — `repo/` and `env/` — so one mount
        covers them and there is exactly one host path to translate.
        """
        p = Path(acq.path) if acq.path else None
        return str(p.parent) if p else ""

    def _resolver(self, acq: RepoAcquisition):
        """An import probe that runs inside the container, not on this host."""
        mount, image = self._mount(acq), self._image(acq)

        def resolve(interpreter: str, repo: Path, modules: list[str]) -> list[str]:
            if not modules:
                return []
            probe = ("import importlib.util, json, sys\n"
                     "sys.path.insert(0, sys.argv[1])\n"
                     "out = []\n"
                     "for m in sys.argv[2:]:\n"
                     "    try:\n"
                     "        if importlib.util.find_spec(m) is None: out.append(m)\n"
                     "    except Exception: out.append(m)\n"
                     "print(json.dumps(out))\n")
            repo_in = container_mod.to_container_path(str(repo), mount) or f"{container_mod.MOUNT}/repo"
            argv = container_mod.run_argv(
                [interpreter, "-c", probe, repo_in, *modules],
                host_mount=mount, workdir=repo_in, image=image)
            rc, out, _ = container_mod._run(argv, timeout=300)
            if rc != 0:
                return list(modules)              # cannot ask: unresolved, never "fine"
            try:
                return list(json.loads((out or "[]").strip().splitlines()[-1]))
            except (ValueError, TypeError, IndexError):
                return list(modules)

        return resolve

    def capability(self, acq: RepoAcquisition, interpreter: str,
                   harness_python: str, flag: str = "seed") -> ExecCapability:
        cap = repo_mod.assess_capability(acq, interpreter, harness_python, flag,
                                         platform=self.platform,
                                         resolve_imports=self._resolver(acq))
        cap.backend = self.name
        return cap

    # --- provisioning -----------------------------------------------------------------
    def _image(self, acq: RepoAcquisition) -> str:
        return container_mod.image_for(getattr(acq, "declared_python", "") or "")[0]

    def provision(self, cfg: Config, root: Path, pid: str,
                  acq: RepoAcquisition) -> RepoAcquisition:
        """Build the repository's declared stack INSIDE a container, onto the host disk.

        The venv is created by the container's own Linux python and written through the
        bind mount, so it persists between runs exactly as the local one does while being
        native to the platform that will execute it. Nothing is installed on the host.

        `env_status` becomes "ready" only when something was actually installed OR the
        repository declares no dependencies at all. It used to be set to "ready"
        unconditionally, which is how three corpus clones carried `env_status: ready`
        beside a site-packages directory holding nothing but pip.
        """
        if acq.status not in ("cloned", "cached") or not acq.path:
            acq.env_status = "not_attempted"
            return acq
        ok, why = container_mod.daemon_status()
        if not ok:
            acq.env_status, acq.reason = "blocked", why
            return acq
        if not (cfg.allow_install and cfg.allow_network):
            acq.env_status = "blocked"
            acq.reason = "the install or network gate is shut, so no environment was built"
            return acq

        mount = self._mount(acq)
        image, image_note = container_mod.image_for(getattr(acq, "declared_python", "") or "")
        repo_in = container_mod.to_container_path(str(acq.path), mount)
        if not repo_in:
            acq.env_status = "failed"
            acq.reason = "the checkout is not under the directory this backend mounts"
            return acq
        env_in = f"{container_mod.MOUNT}/env"
        py_in = f"{env_in}/bin/python"

        rc, out, err = container_mod._run(
            container_mod.run_argv(["python", "-m", "venv", env_in],
                                   host_mount=mount, image=image), timeout=600)
        if rc != 0:
            acq.env_status = "failed"
            acq.reason = f"venv creation in the container failed: {(err or out).strip()[-300:]}"
            return acq

        installed = False
        for rel in acq.dependency_files:
            if not rel.endswith(".txt"):
                continue                      # only pip requirement files are installable here
            rc, out, err = container_mod._run(
                container_mod.run_argv([py_in, "-m", "pip", "install", "-r", f"{repo_in}/{rel}"],
                                       host_mount=mount, workdir=repo_in, image=image),
                timeout=cfg.install_timeout_s)
            if rc != 0:
                acq.env_status = "failed"
                acq.reason = f"pip install -r {rel} failed in the container: {(err or out).strip()[-300:]}"
                return acq
            installed = True

        acq.env_path = py_in
        # A bare venv is not the repository's declared stack. Saying "ready" for one let
        # `assess_capability`'s env_status gate pass and surfaced the real problem later
        # as `dependency_missing`, which reads as a fact about the repository rather than
        # about what this harness installed.
        declared = [r for r in acq.dependency_files if r.endswith(".txt")]
        if installed or not acq.dependency_files:
            acq.env_status = "ready"
            acq.reason = ((image_note + "; " if image_note else "")
                          + ("installed " + ", ".join(declared) if installed
                             else "the repository declares no dependency file; a bare "
                                  f"{image} interpreter is what would run"))
        else:
            acq.env_status = "empty_environment"
            acq.reason = ((image_note + "; " if image_note else "")
                          + "the repository declares dependencies this backend cannot "
                            "install from: " + ", ".join(acq.dependency_files))
        return acq

    # --- execution --------------------------------------------------------------------
    def execute(self, req: ExecRequest) -> ExecOutcome:
        """Run one process in a container. Never raises; every ending comes back as data."""
        started, t0 = _utc(), time.time()
        mount = str(Path(req.cwd).parents[0]) if req.cwd else ""
        stamp = dict(backend=self.name, argv=list(req.argv), cwd=req.cwd,
                     environment=self.environment(), started_at=started)
        if not req.cwd.startswith(container_mod.MOUNT):
            # The argv and cwd are built upstream from what `provision` returned, which is
            # already in the container's namespace. Anything else means the translation
            # did not happen, and relaying a host path would produce a not-found recorded
            # against the paper. Refuse instead of rewriting.
            return ExecOutcome(launched=False, completed=False, seconds=0.0, ended_at=_utc(),
                               error=(f"refusing to run: cwd {req.cwd!r} is not inside "
                                      f"{container_mod.MOUNT}, so this command was not "
                                      f"prepared for a container"), **stamp)
        host_mount = getattr(self, "_host_mount", "") or mount
        argv = container_mod.run_argv(
            list(req.argv), host_mount=host_mount, workdir=req.cwd,
            image=getattr(self, "_image_name", container_mod.DEFAULT_IMAGE),
            gpus=self._gpu()[0], env=dict(req.env or {}), network=False)
        try:
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=req.timeout_s)
        except subprocess.TimeoutExpired as e:
            return ExecOutcome(launched=True, completed=False, timed_out=True,
                               stdout=_text(e.stdout), stderr=_text(e.stderr),
                               seconds=round(time.time() - t0, 3), ended_at=_utc(),
                               error=f"timeout after {req.timeout_s}s", **stamp)
        except OSError as e:
            return ExecOutcome(launched=False, completed=False,
                               seconds=round(time.time() - t0, 3), ended_at=_utc(),
                               error=f"could not start the container: {e}", **stamp)
        return ExecOutcome(launched=True, completed=True, returncode=p.returncode,
                           stdout=p.stdout or "", stderr=p.stderr or "",
                           seconds=round(time.time() - t0, 3), ended_at=_utc(), **stamp)

    def cleanup(self, root: Path, pid: str) -> list[str]:
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
        requires_credentials=True, can_execute=False, isolation="REMOTE_SESSION",
        reproducibility="session",
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
        requires_credentials=True, can_execute=False, isolation="REMOTE_SESSION",
        reproducibility="session",
        detail="Google Colab free tier, published specification")


class SandboxBackend(ExecutionBackend):
    """A remote Linux sandbox, leased per paper. The mechanics live in `harness/sandbox.py`.

    This class is thin on purpose. Everything specific to a provider — leasing, staging,
    measuring, running, releasing — is in one module behind six calls, and everything
    specific to *review* stays where it already was: `authorize()` is still the only thing
    that may say yes, `verify_commit` still decides whether the tree is the audited one,
    `assess_capability` still decides whether the code gets a fair run, and
    `local_exec.reconcile` still decides what a number may conclude. A remote reproduction
    and a local one pass through the identical gates in the identical order.

    **Three things it does differently, and each closes a hole a naive remote would open.**

    *Capability is asked of the sandbox's interpreter.* The static half of the check reads
    the local checkout, which is byte-identical because both trees are the same verified
    commit. The one dynamic question — can this interpreter resolve the entrypoint's
    imports — is delegated to the machine that owns the interpreter.

    *The commit is verified inside the sandbox, by the same function.* `commit_tree`
    returns a reader over the staged checkout, so E2 certifies the tree that will run.
    A missing session returns a tree that cannot be read, which reports `unknown` and
    refuses, rather than falling back to certifying a directory on this disk.

    *Nothing falls back to local.* Every failure — no client, no credentials, a closed
    gate, a refused reservation, a machine smaller than the reservation, a commit that
    could not be staged — sets `env_status` and a reason, and capability then refuses.
    There is no branch that runs the experiment here instead.
    """

    name = "modal"
    # The leased machine is not the operator's, holds none of their credentials or files,
    # and is torn down in `stages/probe.run`'s `finally`. That is what REMOTE_SESSION
    # means, and it is why this is currently the ONLY backend entitled to run a paper's
    # own repository — see `harness.isolation` and `authorize`'s isolation condition.
    isolation = "REMOTE_SESSION"

    # --- what is on offer -------------------------------------------------------------
    def _spec(self):
        from . import sandbox as sandbox_mod
        return sandbox_mod.spec_from_config(self.config())

    def resources(self) -> BackendResources:
        """The RESERVATION, not a measurement. Nothing is leased to answer this.

        Called during planning, before any sandbox exists, and by `select_for` for every
        registered backend on every paper — so it must not contact a provider. What a
        reservation actually turned into is measured in `provision` and refused there if it
        is smaller; what a process actually ran on is stamped by `execute` from that
        measurement. This is the request.
        """
        spec = self._spec()
        return BackendResources(
            name=self.name, platform="linux", python=spec.python_version,
            python_version=spec.python_version,
            has_gpu=bool(spec.gpu), vram_bytes=spec.vram_bytes,
            gpu_count=spec.gpu_count, gpu_name=spec.gpu_kind,
            ram_bytes=spec.memory_mib * 1024 * 1024,
            disk_bytes=spec.disk_gib * (1024 ** 3),
            cpu_count=max(1, int(spec.cpu)),
            detail=f"remote sandbox reservation — {spec.describe()}")

    def profile(self) -> BackendProfile:
        r = self.resources()
        spec = self._spec()
        return BackendProfile(
            name=self.name, platform="linux", vram_bytes=r.vram_bytes, ram_bytes=r.ram_bytes,
            disk_bytes=r.disk_bytes, cpu_count=r.cpu_count, gpu_count=r.gpu_count,
            gpu_name=r.gpu_name, python_version=r.python_version,
            max_walltime_s=spec.timeout_s, requires_credentials=True,
            # The load-bearing difference from Kaggle and Colab: this one can be driven.
            # It is still refused by `authorize` whenever `available()` is false, so a
            # missing token or a closed gate blocks exactly as a declaration would — but
            # it blocks as `backend_offline`, which an operator can resolve, rather than
            # as `credentials_unavailable`, which they cannot.
            can_execute=True, isolation=self.isolation,
            network_at_runtime=True, reproducibility="session",
            detail=f"leased Linux sandbox — {spec.describe()}")

    def available(self) -> BackendAvailability:
        from . import sandbox as sandbox_mod
        ok, why = sandbox_mod.driver_status(self.config())
        return BackendAvailability(ok, why)

    def environment(self) -> str:
        """What a run WOULD happen on, for the planning record.

        `execute` overrides this per outcome with what the session actually measured,
        because that is the line a reproduction verdict has to be locatable from. This one
        answers the interface's question before any machine exists.
        """
        spec = self._spec()
        return f"{self.name} linux python{spec.python_version} {spec.gpu_kind or 'no GPU'}"

    # --- staging ------------------------------------------------------------------------
    def provision(self, cfg: Config, root: Path, pid: str,
                  acq: RepoAcquisition) -> RepoAcquisition:
        """Lease a machine, stage the audited commit, build the repo's stack, verify both.

        Mirrors `LocalBackend.provision`'s refusals exactly where they coincide — no
        checkout is `not_attempted`, a shut install or network gate is `blocked` — so the
        only new refusals are the ones genuinely about a remote machine.
        """
        from . import sandbox as sandbox_mod

        if acq.status not in ("cloned", "cached") or not acq.path:
            acq.env_status = "not_attempted"
            return acq
        if not (cfg.allow_install and cfg.allow_network):
            acq.env_status = "blocked"
            acq.reason = ("a remote session needs SH_ALLOW_NETWORK to fetch the audited "
                          "commit and SH_ALLOW_INSTALL to build the repository's stack")
            return acq
        try:
            session = sandbox_mod.open_session(
                cfg, local_path=acq.path, url=acq.url, commit=acq.commit,
                requirement_files=[r for r in acq.dependency_files if r.endswith(".txt")],
                paper_id=pid)
        except sandbox_mod.SandboxSetupError as e:
            # `blocked` when the refusal is about permission or equipment we do not have,
            # `failed` when a lease was obtained and the staging did not work. The two are
            # different facts about US and neither is a fact about the paper — but only the
            # first tells an operator there is something they could change.
            gated = any(s in str(e) for s in
                        ("SH_ALLOW_SANDBOX", "credentials", "not importable",
                         "SH_ALLOW_NETWORK", "SH_ALLOW_INSTALL"))
            acq.env_status = "blocked" if gated else "failed"
            acq.reason = f"no remote sandbox was staged for this checkout: {e}"
            return acq

        # E2, inside the machine that will run. The SAME function certifies it — see
        # `repo.GitTree`. A sandbox whose tree is not provably the audited commit is
        # released here rather than carried into an execution that would be labelled with
        # a SHA nobody verified there.
        verified = repo_mod.verify_commit(
            acq.path, acq.commit, tree=sandbox_mod.SandboxGitTree(session))
        if not verified.established:
            sandbox_mod.release(acq.path)
            acq.env_status = "failed"
            acq.reason = (f"the sandbox checkout could not be certified as the audited "
                          f"commit: {verified.reason}")
            return acq

        acq.env_path = session.env_python
        acq.env_status = "ready"
        gpu = session.gpu_name or "no GPU"
        acq.reason = (f"staged into remote sandbox {session.sandbox_id} at commit "
                      f"{verified.actual[:12]}, verified in place; the machine reports "
                      f"{gpu} and {session.measured.get('cpu_count') or '?'} CPU(s)")
        return acq

    # --- the two questions that must be asked inside the machine ----------------------
    def capability(self, acq: RepoAcquisition, interpreter: str,
                   harness_python: str, flag: str = "seed") -> ExecCapability:
        from . import sandbox as sandbox_mod

        session = sandbox_mod.session_for(acq.path) if acq.path else None
        if session is None:
            cap = ExecCapability(
                established=False, reason_code="environment_incompatible", backend=self.name,
                env_status=acq.env_status or "", interpreter=interpreter or "",
                entrypoint=acq.entrypoint or "", current_platform="linux",
                detail=("no remote sandbox is staged for this checkout, so there is no "
                        "interpreter to give the repository a fair run: "
                        + (acq.reason or "nothing was provisioned")))
            return cap
        cap = repo_mod.assess_capability(
            acq, interpreter, harness_python, flag, platform=session.platform,
            resolve_imports=sandbox_mod.import_resolver(session))
        cap.backend = self.name
        return cap

    def commit_tree(self, cwd: str) -> "repo_mod.GitTree | None":
        from . import sandbox as sandbox_mod

        session = sandbox_mod.session_for(cwd) if cwd else None
        if session is None:
            return sandbox_mod.AbsentGitTree(
                cwd, "no remote sandbox is staged for this checkout, so the tree that would "
                     "run cannot be read, let alone shown to be the audited commit")
        return sandbox_mod.SandboxGitTree(session)

    # --- running ------------------------------------------------------------------------
    def execute(self, req: ExecRequest) -> ExecOutcome:
        """Run one command in the session staged for `req.cwd`. Never raises.

        `req.cwd` is a path on THIS machine — the local checkout — and the translation to
        the sandbox's own namespace happens in `sandbox.run_in`. That is why the interface
        needed no new parameter: the local checkout is the identifier both halves of the
        pipeline already carry, and it is the key the session is stored under.
        """
        from . import sandbox as sandbox_mod

        started, t0 = _utc(), time.time()
        session = sandbox_mod.session_for(req.cwd) if req.cwd else None
        if session is None:
            return ExecOutcome(
                launched=False, completed=False, backend=self.name, argv=list(req.argv),
                cwd=req.cwd, environment=self.environment(), started_at=started,
                ended_at=_utc(), seconds=round(time.time() - t0, 3),
                error=("no remote sandbox is staged for this checkout; nothing was run, and "
                       "this command was NOT run on the local machine instead"))
        run = sandbox_mod.run_in(session, list(req.argv), req.cwd, req.timeout_s, req.env)
        return ExecOutcome(
            launched=run.launched, completed=run.completed, returncode=run.returncode,
            stdout=run.stdout, stderr=run.stderr, seconds=run.seconds,
            timed_out=run.timed_out, backend=self.name, error=run.error,
            argv=run.argv or list(req.argv), cwd=run.cwd or req.cwd,
            # From what the machine MEASURED, not from what was reserved. This is the only
            # record of the hardware a remote verdict came from.
            environment=session.environment(), started_at=started, ended_at=_utc())

    # --- giving the machine back ---------------------------------------------------------
    def release(self, root: Path, pid: str) -> tuple[bool, str]:
        from . import sandbox as sandbox_mod
        return sandbox_mod.release(Path(root) / "runs" / pid / "repo")

    def cleanup(self, root: Path, pid: str) -> list[str]:
        """End the lease and remove the session record. Never touches the checkout.

        The checkout is the evidence the static audit cites and `runs/<pid>` holds the
        results a reader needs, so the same rule the local backend follows applies: only
        what provisioning itself created is removed.
        """
        from . import sandbox as sandbox_mod

        checkout = Path(root) / "runs" / pid / "repo"
        record = sandbox_mod.session_path(checkout)
        existed = record.is_file()
        sandbox_mod.release(checkout)
        return [str(record)] if existed else []


class UnknownBackend(ValueError):
    """The operator named a backend that is not registered."""


_REGISTRY: dict[str, type[ExecutionBackend]] = {
    "local": LocalBackend,
    # A Linux container on this host, and the first LOCAL backend whose isolation is
    # sufficient to run a paper's own repository. It is registered unconditionally and
    # reports itself unavailable when no daemon answers, so registering it grants nothing
    # on a host without a container runtime — the same discipline `modal` follows.
    "container": ContainerBackend,
    # A real runner, and the only registered one that is not this machine. Needs the
    # sandbox gate and provider credentials; without either it reports itself unavailable
    # and `authorize` refuses, so registering it grants nothing on its own.
    "modal": SandboxBackend,
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
    return cls(cfg)


def backend_for(cfg: Config, spec: ProbeSpec) -> ExecutionBackend | None:
    """The backend this spec would run on, or None when the operator named an unknown one.

    Harness-authored code always runs locally — see `local_backend`. Only a spec carrying
    the repository's own command, OR a governed reconstruction, is subject to selection;
    an unresolvable selection yields None rather than a substitute, so `authorize` refuses
    instead of running a Linux repository on whatever host happened to be available.

    `reimpl_exec` is the one provenance without `spec.command` that is NOT exempted here.
    It has no `command` because it runs as `spec.script`, exactly like `template` and
    `synthesized` — but unlike them it is third-party (model-authored) code, and forcing
    it onto `local_backend()` regardless of `cfg.exec_backend` would make it structurally
    unable to ever reach the CONTAINER/REMOTE_SESSION isolation `backends.authorize`'s
    `reimpl_exec` branch requires — permanently refusing it even when an operator has
    configured the sandbox and taken on that risk deliberately.
    """
    if not spec.command and spec.provenance != "reimpl_exec":
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

    runnable: list[tuple[tuple[int, int], ExecutionBackend, BackendProfile]] = []
    declared: list[tuple[ExecutionBackend, BackendProfile]] = []
    considered: list[tuple[str, str, str]] = []

    for name in registered_backends():
        # Constructed WITH the config being matched against, so a profile answers for the
        # settings in play rather than for whatever the environment happens to hold.
        backend = _REGISTRY[name](cfg)
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
        # Two keys, and the first one matters more than it looks. Among environments that
        # ALL meet every stated requirement, the one the operator configured wins; only
        # then is the smallest sufficient one preferred.
        #
        # Without the first key, `SH_EXEC_BACKEND` meant nothing whenever a second runner
        # also fitted: an operator who asked for the remote sandbox would have their small
        # experiments silently run on this laptop instead, because a laptop is "smaller"
        # and the requirement was met either way. That is a substitution — the run happens
        # on hardware the operator did not choose, and nothing in the record says a choice
        # was overridden. Selection may still rule the named backend OUT on platform,
        # size, credentials or availability, which is a refusal it reports; what it may not
        # do is quietly prefer a different one that is equally sufficient.
        preferred = 0 if name == (getattr(cfg, "exec_backend", "") or "local").strip().lower() else 1
        runnable.append(((preferred, p.vram_bytes or 0), backend, p))

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
    if spec.provenance == _REIMPL_PROVENANCE:
        # --- GOVERNED RECONSTRUCTION, gated like repository execution, not like our own
        # code -------------------------------------------------------------------------
        # A `reimpl_exec` spec carries no `spec.command` — it runs as `spec.script`, like
        # every other harness-authored probe — so without this branch it would fall
        # straight into the `not spec.command` free pass below and run UNCONFINED as "code
        # this harness or its operator authored". It is neither: it is a MODEL's output,
        # written by `harness.reimplement_driver` from the paper's specification and never
        # reviewed by a human before running, which is exactly the third-party-code-
        # execution risk `sufficient_for_repo_exec` exists to confine. Decision 10 is
        # explicit that no capability increasing that risk lands without the boundary
        # enforced, so this reuses the identical floor `repo_exec` requires rather than
        # inventing a weaker one for "only" a reconstruction.
        if backend is None:
            return ExecAuthorization(
                allowed=False, decision="no_backend", backend="",
                failure_class="backend_unavailable",
                detail="no execution backend is available for this experiment")
        name = backend.name
        if not backend.profile().can_execute:
            return ExecAuthorization(
                allowed=False, decision="backend_cannot_execute", backend=name,
                failure_class="credentials_unavailable" if backend.profile().requires_credentials
                else "backend_unavailable",
                detail=f"'{name}' is a declared environment, not a runner: "
                       f"{backend.available().detail}")
        if not backend.available().usable:
            return ExecAuthorization(
                allowed=False, decision="backend_offline", backend=name,
                failure_class="backend_unavailable",
                detail=f"'{name}' can execute but is not reachable now: "
                       f"{backend.available().detail}")
        if not cfg.allow_reimplementation_exec:
            return ExecAuthorization(
                allowed=False, decision="gate_closed", backend=name,
                failure_class="execution_unauthorized",
                detail="SH_ALLOW_REIMPLEMENTATION_EXEC is not set; running a governed "
                       "reconstruction stays an explicit per-invocation opt-in, separate "
                       "from SH_ALLOW_REPO_EXEC because this is not the authors' code")
        if not isolation_mod.sufficient_for_repo_exec(backend.profile().isolation):
            return ExecAuthorization(
                allowed=False, decision="isolation_insufficient", backend=name,
                failure_class="execution_unauthorized",
                detail=isolation_mod.refusal_detail(name, backend.profile().isolation))
        conf = spec.reimplementation_conformance
        if conf is None or not conf.established:
            return ExecAuthorization(
                allowed=False, decision="conformance_unproven", backend=name,
                failure_class="execution_unauthorized",
                detail=(conf.reason if conf is not None else
                        "no ReimplementationConformance was established for this spec: every "
                        "required ingredient must be bound to both a paper locator and a "
                        "verified implementation locator before a reconstruction may run"))
        return ExecAuthorization(
            allowed=True, decision="authorized", backend=name, failure_class="none",
            detail="every required ingredient is bound to both a paper locator and a "
                   "verified implementation locator, the execution gate is open, and this "
                   "backend confines third-party code to a container or a leased remote "
                   "session")

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

    # --- THE ISOLATION BOUNDARY -------------------------------------------------------
    # Placed after the gate and before provenance, because it answers a question that is
    # prior to every scientific one: not "is this the right code" but "is running somebody
    # else's code HERE safe at all". A venv scopes imports and wall time; it does not
    # confine the filesystem, the network, the environment or the user, and until this
    # condition existed `SH_ALLOW_REPO_EXEC=1` ran a paper's repository under the
    # operator's own account with all four in reach.
    #
    # This is a TIGHTENING and never a widening: every spec refused here was already
    # refused-or-worse by one of the conditions below on any backend that would have run
    # it, and nothing previously refused becomes permitted. `harness.isolation` reads no
    # configuration, so there is no environment variable that lowers the requirement.
    if not isolation_mod.sufficient_for_repo_exec(backend.profile().isolation):
        return ExecAuthorization(
            allowed=False, decision="isolation_insufficient", backend=name,
            failure_class="execution_unauthorized",
            detail=isolation_mod.refusal_detail(name, backend.profile().isolation))

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

    # --- THE ISOLATION BOUNDARY, before every scientific condition ---------------------
    # The local backend can start a process and cannot confine one, so with the gate open
    # it is refused here — before commit, identity, capability or resources are consulted,
    # because "is running a stranger's code here safe" is prior to "is it the right code".
    a = authorize(open_cfg, repo_spec, backend)
    assert not a.allowed and a.decision == "isolation_insufficient", a.decision
    assert a.failure_class == "execution_unauthorized"
    assert "CONTAINER or REMOTE_SESSION" in a.detail
    assert "Nothing about the paper follows" in a.detail
    assert backend.profile().isolation == "VENV", "a venv is not a boundary"

    # Every remaining condition is exercised against a backend that IS entitled to run
    # third-party code. This is the local backend with one field changed, so the rest of
    # the ladder is tested exactly as it always was.
    class _Confined(LocalBackend):
        name = "local"
        isolation = "REMOTE_SESSION"

    confined = _Confined(cfg)
    assert isolation_mod.sufficient_for_repo_exec(confined.profile().isolation)
    backend = confined

    # E2 — with the gate open and the boundary satisfied, an unverified checkout is the
    # next thing in the way, and omitting the verification entirely must refuse rather
    # than wave through.
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
