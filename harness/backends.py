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
    detail: str = ""


@dataclass(frozen=True)
class BackendAvailability:
    usable: bool
    detail: str = ""


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #
class ExecutionBackend(ABC):
    """Six operations. Adding Linux/WSL/container means implementing these and nothing else.

    The audit, identity and reconciliation layers never import a concrete backend and
    never branch on `backend.name`. They ask for a platform, a capability record, an
    outcome — all of which are backend-independent shapes — so a new backend changes
    where a process runs without changing what any verdict means.

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

    @property
    def platform(self) -> str:
        return self.resources().platform

    def available(self) -> BackendAvailability:
        """Is this backend usable on this machine right now?"""
        return BackendAvailability(True, "")

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
        started = time.time()
        env = None
        if req.env:
            env = {**os.environ, **req.env}
        try:
            p = subprocess.run(req.argv, cwd=req.cwd or None, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=req.timeout_s,
                               env=env)
        except subprocess.TimeoutExpired:
            return ExecOutcome(launched=True, completed=False, timed_out=True,
                               seconds=round(time.time() - started, 3), backend=self.name,
                               error=f"timeout after {req.timeout_s}s")
        except OSError as e:
            return ExecOutcome(launched=False, completed=False,
                               seconds=round(time.time() - started, 3), backend=self.name,
                               error=f"could not start: {e}")
        return ExecOutcome(launched=True, completed=True, returncode=p.returncode,
                           stdout=p.stdout or "", stderr=p.stderr or "",
                           seconds=round(time.time() - started, 3), backend=self.name)

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
class UnknownBackend(ValueError):
    """The operator named a backend that is not registered."""


_REGISTRY: dict[str, type[ExecutionBackend]] = {"local": LocalBackend}


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

    if backend is None or not backend.available().usable:
        detail = (backend.available().detail if backend is not None
                  else "no execution backend is available")
        return ExecAuthorization(allowed=False, decision="no_backend",
                                 backend=getattr(backend, "name", ""),
                                 failure_class="execution_unauthorized",
                                 detail=f"repository execution needs a usable backend: {detail}")

    name = backend.name
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
