"""Where execution physically happens, whether it may, and what a number may conclude:
execution backends, the two ordered ladders (`authorize`, `reconcile`), and the runner
(`run_probe`).

`python -m harness.execute` runs the self-check.

Two responsibilities, deliberately separated: a backend is a MECHANISM (starts a process,
presents a platform, has no opinion on whether it ought to); `authorize()` is the POLICY,
the only thing that may say yes, and lives as a free function so a new backend inherits it
by construction. `authorize()` is checked again at the point of execution, not only at
planning, since a hand-written `spec.json` never passes through the planning-time
promotion.

`reconcile()` does not reduce to a pure predicate table: five pure preconditions
(authorization, the provenance ceiling, reimpl conformance, the comparison, identity), one
non-predicate RECORDER (assigns experiment/metric/configuration state before the other
preconditions, so a refused reconciliation still carries what identity actually found), a
four-rung failure ladder (capability, infrastructure, not-reached, runtime), and an
imperative arithmetic tail — ported close to verbatim rather than tabularized, since a
table shape here would cost correctness the imperative version does not.

Not yet folded into this module: `experiment_id.py`'s resolution algorithms
(`identities_established`, their AND-point, IS ported below); `resources.py`'s extraction
helpers (`assess_resources` IS ported below); `repo.py`'s acquisition/build/capability
(E2's `verify_commit`/`head_commit` ARE ported below); and the artifact-inspection route.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import container as container_mod
from . import isolation as isolation_mod
from . import provenance as provenance_mod
from . import state
from .config import Config
from .experiment_id import identities_established
from .schema import (
    ArmStats, CommitVerification, ConfigurationIdentity, ExecAuthorization, ExecCapability,
    ExecutionRecord, ExperimentIdentity, MetricIdentity, ProbeResult, ProbeSpec,
    Reconciliation, RepoAcquisition,
)

DEFAULT_TEMPLATE = '''"""Identical-arms noise-floor probe, generated because no paper-specific script was
available for this target. Measures this machine's seed-to-seed variance, nothing else."""
import random
import sys

metric = "__METRIC__"
dataset = "__DATASET__"
arms = "__ARMS__".split("/")
epochs = __EPOCHS__
claim = """__CLAIM__"""


def _measure(seed: int) -> float:
    random.seed(seed)
    return 0.5 + random.gauss(0, 0.02)


if __name__ == "__main__":
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 0
    arm = sys.argv[sys.argv.index("--arm") + 1] if "--arm" in sys.argv else arms[0]
    print("SH_DEVICE cpu")
    value = _measure(seed)
    print(f"SH_METRIC arm={arm} seed={seed} value={value:.6f}")
'''


def _utc() -> str:
    return state.now()


def _text(buf) -> str:
    """`TimeoutExpired.stdout` is bytes or str depending on how the child was opened."""
    if buf is None:
        return ""
    return buf.decode("utf-8", "replace") if isinstance(buf, bytes) else str(buf)


def _looks_like_host_interpreter(token: str) -> bool:
    """Is `token` a filesystem path this harness's OWN host wrote, not a bare PATH name?"""
    if not token:
        return False
    return bool(re.match(r"^[A-Za-z]:[\\/]", token) or token.startswith("\\\\")
               or "\\" in token or token.startswith("/"))


# Provenances whose code the AUTHORS wrote — the same ceiling `reconcile` enforces,
# restated here so an unauthorized command cannot even start.
_REPO_PROVENANCE = "repo_exec"
_REIMPL_PROVENANCE = "reimpl_exec"
_CERT_PROVENANCE = "cert_exec"


# === THE UNIT OF WORK, AND WHAT CAME BACK ===============================================
@dataclass(frozen=True)
class ExecRequest:
    """One process to run. Frozen so a backend cannot edit the command it was handed."""
    argv: list[str]
    cwd: str
    timeout_s: int
    env: dict[str, str] | None = None
    label: str = ""


@dataclass
class ExecOutcome:
    """Three distinguishable endings: launched=False (never existed), launched=True/
    completed=False (killed by timeout), launched=True/completed=True (ran to an exit
    code) — opposite evidence about whether the experiment was reached."""
    launched: bool
    completed: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    backend: str = ""
    error: str = ""
    argv: list[str] = field(default_factory=list)
    launch_argv: list[str] = field(default_factory=list)
    cwd: str = ""
    environment: str = ""
    started_at: str = ""
    ended_at: str = ""

    @property
    def ok(self) -> bool:
        return self.completed and self.returncode == 0


@dataclass(frozen=True)
class BackendResources:
    name: str
    platform: str
    python: str
    has_gpu: bool = False
    vram_bytes: int | None = None
    ram_bytes: int | None = None
    disk_bytes: int | None = None
    cpu_count: int | None = None
    gpu_count: int | None = None
    gpu_name: str = ""
    python_version: str = ""
    detail: str = ""


@dataclass(frozen=True)
class BackendAvailability:
    usable: bool
    detail: str = ""


@dataclass(frozen=True)
class BackendProfile:
    """What a backend OFFERS. `can_execute` is load-bearing: False describes a place an
    experiment WOULD fit, never a place one RUNS — `authorize()` refuses it outright."""
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
    # Defaults to NONE so a backend that forgets to declare a level is refused rather than
    # trusted — the same fail-closed direction as `provenance.admits`.
    isolation: str = "NONE"
    detail: str = ""
    network_at_runtime: bool = False
    reproducibility: str = ""


@dataclass(frozen=True)
class BackendSelection:
    chosen: "ExecutionBackend | None"
    reason_code: str
    reason: str
    considered: tuple[tuple[str, str, str], ...] = ()

    @property
    def selected(self) -> bool:
        return self.chosen is not None and self.reason_code == "selected"


# === THE INTERFACE =======================================================================
class ExecutionBackend(ABC):
    """Five operations to implement, three to override. The audit, identity and
    reconciliation layers never import a concrete backend or branch on `backend.name`."""

    name: str = "abstract"
    # Declared per class, never derived — no runtime probe can establish confinement
    # strength. NONE by default so a forgotten declaration is refused repo execution.
    isolation: str = "NONE"

    def __init__(self, cfg: Config | None = None) -> None:
        self._cfg = cfg

    def config(self) -> Config:
        return self._cfg if self._cfg is not None else Config.load()

    @abstractmethod
    def resources(self) -> BackendResources: ...

    def profile(self) -> BackendProfile:
        r = self.resources()
        return BackendProfile(
            name=self.name, platform=r.platform, vram_bytes=r.vram_bytes,
            ram_bytes=r.ram_bytes, disk_bytes=r.disk_bytes, cpu_count=r.cpu_count,
            gpu_count=r.gpu_count, gpu_name=r.gpu_name, python_version=r.python_version,
            network_at_runtime=True, can_execute=True, isolation=self.isolation, detail=r.detail)

    @property
    def platform(self) -> str:
        return self.resources().platform

    def available(self) -> BackendAvailability:
        return BackendAvailability(True, "")

    def environment(self) -> str:
        r = self.resources()
        return " ".join(x for x in (self.name, r.platform, r.python, r.gpu_name) if x)

    @abstractmethod
    def capability(self, acq: RepoAcquisition, interpreter: str,
                  harness_python: str, flag: str = "seed") -> ExecCapability: ...

    @abstractmethod
    def provision(self, cfg: Config, root: Path, pid: str,
                  acq: RepoAcquisition) -> RepoAcquisition: ...

    @abstractmethod
    def execute(self, req: ExecRequest) -> ExecOutcome: ...

    def cleanup(self, root: Path, pid: str) -> list[str]:
        env_dir = Path(root) / "runs" / pid / "env"
        if not env_dir.is_dir():
            return []
        shutil.rmtree(env_dir, ignore_errors=True)
        return [str(env_dir)]

    def release(self, root: Path, pid: str) -> tuple[bool, str]:
        return False, "this backend leases nothing, so there is nothing to release"

    def commit_tree(self, cwd: str):
        """The tree `verify_commit` must read to certify what THIS backend will run. None
        means the local path."""
        return None

    def stage(self, argv: list[str], cwd: str, mount_root: str,
             pid: str = "") -> tuple[list[str], str]:
        """Translate a host-built (argv, cwd) into whatever namespace `execute()` expects.
        Default: no translation."""
        return list(argv), cwd


class LocalBackend(ExecutionBackend):
    """This machine, this OS, subprocesses."""

    name = "local"
    # A venv scopes imports and wall time. It does not confine the filesystem, the
    # network, the environment or the user, so it may not host the authors' own code.
    isolation = "VENV"

    def resources(self) -> BackendResources:
        cfg = self.config()
        vram, gpu_name, gpu_count = host_vram_bytes()
        return BackendResources(
            name=self.name, platform=sys.platform, python=cfg.python,
            python_version=".".join(str(n) for n in sys.version_info[:3]),
            has_gpu=cfg.has_gpu(), vram_bytes=vram, gpu_name=gpu_name, gpu_count=gpu_count or None,
            ram_bytes=host_ram_bytes(), disk_bytes=host_disk_bytes(cfg.projects_dir),
            cpu_count=os.cpu_count(),
            detail=f"local {sys.platform} host, {gpu_name or 'no GPU detected'}")

    def capability(self, acq: RepoAcquisition, interpreter: str,
                  harness_python: str, flag: str = "seed") -> ExecCapability:
        from . import repo as repo_mod
        cap = repo_mod.assess_capability(acq, interpreter, harness_python, flag,
                                         platform=self.platform)
        cap.backend = self.name
        return cap

    def provision(self, cfg: Config, root: Path, pid: str,
                  acq: RepoAcquisition) -> RepoAcquisition:
        from . import repo as repo_mod
        return repo_mod.build_env(cfg, root, pid, acq)

    def execute(self, req: ExecRequest) -> ExecOutcome:
        """Never raises: the caller needs every ending as data."""
        started, t0 = _utc(), time.time()
        env = {**os.environ, **req.env} if req.env else None
        stamp: dict = dict(backend=self.name, argv=list(req.argv), cwd=req.cwd,
                          environment=self.environment(), started_at=started)
        try:
            p = subprocess.run(req.argv, cwd=req.cwd or None, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=req.timeout_s, env=env)
        except subprocess.TimeoutExpired as e:
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


class ContainerBackend(ExecutionBackend):
    """A Linux container on this host — the first backend entitled to run a paper's code.
    Does not fall back: a daemon that is not running, an image that will not pull, an
    environment that will not build and a container that will not start each refuse,
    because a fallback to the host would make the isolation requirement advisory."""

    name = "container"
    isolation = "CONTAINER"
    _gpu_cache: "tuple[bool, str] | None" = None

    def _gpu(self) -> tuple[bool, str]:
        if ContainerBackend._gpu_cache is None:
            if not self.available().usable:
                ContainerBackend._gpu_cache = (False, "no reachable container runtime")
            else:
                ContainerBackend._gpu_cache = container_mod.gpu_available()
        return ContainerBackend._gpu_cache

    def resources(self) -> BackendResources:
        cfg = self.config()
        inv = container_mod.inventory()
        has_gpu, gpu_detail = self._gpu()
        vram, gpu_name, gpu_count = (host_vram_bytes() if has_gpu else (0, "", None))
        return BackendResources(
            name=self.name, platform="linux", python="python3", python_version="",
            has_gpu=has_gpu, vram_bytes=vram,
            gpu_name=(gpu_name or gpu_detail if has_gpu else ""), gpu_count=gpu_count or None,
            ram_bytes=inv.get("ram_bytes") or 0,
            disk_bytes=host_disk_bytes(cfg.projects_dir), cpu_count=inv.get("cpus"),
            detail=(inv.get("detail") or "container runtime")
                   + (f", {gpu_detail}" if has_gpu else ", no GPU in containers"))

    def profile(self) -> BackendProfile:
        r = self.resources()
        return BackendProfile(
            name=self.name, platform="linux", vram_bytes=r.vram_bytes, ram_bytes=r.ram_bytes,
            disk_bytes=r.disk_bytes, cpu_count=r.cpu_count, gpu_count=r.gpu_count,
            gpu_name=r.gpu_name, python_version=r.python_version, network_at_runtime=False,
            can_execute=True, isolation=self.isolation, reproducibility="container",
            detail=r.detail)

    def available(self) -> BackendAvailability:
        ok, why = container_mod.daemon_status()
        return BackendAvailability(ok, why)

    def environment(self) -> str:
        r = self.resources()
        return " ".join(x for x in (self.name, r.platform, r.detail) if x)

    def _mount(self, acq: RepoAcquisition) -> str:
        p = Path(acq.path) if acq.path else None
        return str(p.parent) if p else ""

    def _resolver(self, acq: RepoAcquisition):
        """An import probe that runs inside the container, not on this host — the venv
        lives inside the container's filesystem view."""
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
            argv = container_mod.run_argv([interpreter, "-c", probe, repo_in, *modules],
                                          host_mount=mount, workdir=repo_in, image=image)
            rc, out, _ = container_mod._run(argv, timeout=300)
            if rc != 0:
                return list(modules)
            try:
                return list(json.loads((out or "[]").strip().splitlines()[-1]))
            except (ValueError, TypeError, IndexError):
                return list(modules)

        return resolve

    def capability(self, acq: RepoAcquisition, interpreter: str,
                  harness_python: str, flag: str = "seed") -> ExecCapability:
        from . import repo as repo_mod
        cap = repo_mod.assess_capability(acq, interpreter, harness_python, flag,
                                         platform=self.platform, resolve_imports=self._resolver(acq))
        cap.backend = self.name
        return cap

    def _image(self, acq: RepoAcquisition) -> str:
        return container_mod.image_for(getattr(acq, "declared_python", "") or "")[0]

    def provision(self, cfg: Config, root: Path, pid: str,
                  acq: RepoAcquisition) -> RepoAcquisition:
        """Build the repository's declared stack INSIDE a container, onto the host disk.
        `env_status` becomes "ready" only when something was actually installed OR the
        repository declares no dependencies at all — a bare venv is not the declared
        stack."""
        if acq.status not in ("cloned", "cached") or not acq.path:
            acq.env_status = "not_attempted"
            return acq
        if not (cfg.allow_install and cfg.allow_network):
            acq.env_status = "blocked"
            acq.reason = "the install or network gate is shut, so no environment was built"
            return acq
        ok, why = container_mod.daemon_status()
        if not ok:
            acq.env_status, acq.reason = "blocked", why
            return acq

        mount = self._mount(acq)
        image, image_note = container_mod.image_for(getattr(acq, "declared_python", "") or "")
        # Remembered so `execute()` (and `stage()`, ahead of it) run the SAME image and
        # mount this env was built against, rather than always defaulting to DEFAULT_IMAGE.
        self._host_mount, self._image_name, self._pid = mount, image, pid
        repo_in = container_mod.to_container_path(str(acq.path), mount)
        if not repo_in:
            acq.env_status = "failed"
            acq.reason = "the checkout is not under the directory this backend mounts"
            return acq
        env_in = f"{container_mod.MOUNT}/env"
        py_in = f"{env_in}/bin/python"

        rc, out, err = container_mod._run(
            container_mod.run_argv(["python", "-m", "venv", env_in], host_mount=mount, image=image),
            timeout=600)
        if rc != 0:
            acq.env_status = "failed"
            acq.reason = f"venv creation in the container failed: {(err or out).strip()[-300:]}"
            return acq

        installed = False
        for rel in acq.dependency_files:
            if not rel.endswith(".txt"):
                continue
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

    def stage(self, argv: list[str], cwd: str, mount_root: str,
             pid: str = "") -> tuple[list[str], str]:
        """Translate a host-built (argv, cwd) into this container's `/work` namespace.
        Only entries actually rooted at `mount_root` are rewritten; a host path outside
        the mount is never silently relayed. `argv[0]` gets special handling: a
        reconstruction with no environment of its own falls back to `cfg.python`, a host
        path meaningless inside this image, substituted with the image's own `python3`."""
        self._host_mount = mount_root or getattr(self, "_host_mount", "")
        if pid:
            self._pid = pid
        translated: list[str] = []
        for i, token in enumerate(argv):
            hit = container_mod.to_container_path(token, mount_root) if mount_root else ""
            if hit:
                translated.append(hit)
            elif i == 0 and _looks_like_host_interpreter(token):
                translated.append("python3")
            else:
                translated.append(token)
        cwd_in = (container_mod.to_container_path(cwd, mount_root) if mount_root else "") or cwd
        return translated, cwd_in

    def execute(self, req: ExecRequest) -> ExecOutcome:
        """Expects `req.argv`/`req.cwd` already in this container's `/work` namespace —
        `stage()` is the translation step. Anything untranslated is refused, not rewritten
        here."""
        started, t0 = _utc(), time.time()
        stamp: dict = dict(backend=self.name, argv=list(req.argv), cwd=req.cwd,
                          environment=self.environment(), started_at=started)
        if not req.cwd.startswith(container_mod.MOUNT):
            return ExecOutcome(launched=False, completed=False, seconds=0.0, ended_at=_utc(),
                               error=(f"refusing to run: cwd {req.cwd!r} is not inside "
                                      f"{container_mod.MOUNT}, so this command was not "
                                      f"prepared for a container"), **stamp)
        host_mount = getattr(self, "_host_mount", "")
        name = container_mod.container_name(getattr(self, "_pid", ""), req.label)
        argv = container_mod.run_argv(
            list(req.argv), host_mount=host_mount, workdir=req.cwd,
            image=getattr(self, "_image_name", container_mod.DEFAULT_IMAGE),
            gpus=self._gpu()[0], env=dict(req.env or {}), network=False, name=name)
        stamp["launch_argv"] = list(argv)
        try:
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=req.timeout_s)
        except subprocess.TimeoutExpired as e:
            # Killing `docker run` kills the client, not the container.
            killed, detail = container_mod.remove(name)
            return ExecOutcome(launched=True, completed=False, timed_out=True,
                               stdout=_text(e.stdout), stderr=_text(e.stderr),
                               seconds=round(time.time() - t0, 3), ended_at=_utc(),
                               error=(f"timeout after {req.timeout_s}s; {detail}" if killed else
                                      f"timeout after {req.timeout_s}s; THE CONTAINER MAY "
                                      f"STILL BE RUNNING: {detail}"), **stamp)
        except OSError as e:
            container_mod.remove(name)
            return ExecOutcome(launched=False, completed=False,
                               seconds=round(time.time() - t0, 3), ended_at=_utc(),
                               error=f"could not start the container: {e}", **stamp)
        return ExecOutcome(launched=True, completed=True, returncode=p.returncode,
                           stdout=p.stdout or "", stderr=p.stderr or "",
                           seconds=round(time.time() - t0, 3), ended_at=_utc(), **stamp)


class DeclaredBackend(ExecutionBackend):
    """An environment this harness knows the specification of but cannot drive. Every
    path that could execute is closed: `available()` is False, `can_execute` is False so
    `authorize()` refuses before anything is attempted, and `execute()` raises."""

    spec: BackendProfile
    why_unavailable: str = "this environment cannot be provisioned from this host"

    def resources(self) -> BackendResources:
        p = self.spec
        return BackendResources(
            name=p.name, platform=p.platform, python="", python_version=p.python_version,
            has_gpu=bool(p.vram_bytes), vram_bytes=p.vram_bytes, ram_bytes=p.ram_bytes,
            disk_bytes=p.disk_bytes, cpu_count=p.cpu_count, gpu_count=1 if p.vram_bytes else None,
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
            f"integration with it. Reaching this line means an authorization gate was bypassed.")

    def cleanup(self, root: Path, pid: str) -> list[str]:
        return []


class KaggleBackend(DeclaredBackend):
    name = "kaggle"
    why_unavailable = ("Kaggle sessions need an account, an API token and a notebook upload; "
                       "this harness holds no credentials and cannot provision one")
    spec = BackendProfile(
        name="kaggle", platform="linux", vram_bytes=16 * (1024 ** 3), ram_bytes=13 * (1024 ** 3),
        disk_bytes=73 * (1024 ** 3), cpu_count=4, gpu_count=1, gpu_name="Tesla T4",
        max_walltime_s=12 * 3600, network_at_runtime=False, requires_credentials=True,
        can_execute=False, isolation="REMOTE_SESSION", reproducibility="session",
        detail="Kaggle free tier, published specification")


class ColabBackend(DeclaredBackend):
    name = "colab"
    why_unavailable = ("Colab sessions are interactive, credentialed and pre-emptible; there "
                       "is no way to drive one from this host")
    spec = BackendProfile(
        name="colab", platform="linux", vram_bytes=16 * (1024 ** 3), ram_bytes=13 * (1024 ** 3),
        disk_bytes=78 * (1024 ** 3), cpu_count=2, gpu_count=1, gpu_name="Tesla T4",
        max_walltime_s=12 * 3600, network_at_runtime=False, requires_credentials=True,
        can_execute=False, isolation="REMOTE_SESSION", reproducibility="session",
        detail="Google Colab free tier, published specification")


class UnknownBackend(ValueError):
    """The operator named a backend that is not registered."""


_REGISTRY: dict[str, type[ExecutionBackend]] = {
    "local": LocalBackend,
    "container": ContainerBackend,
    "kaggle": KaggleBackend,
    "colab": ColabBackend,
}


def registered_backends() -> list[str]:
    return sorted(_REGISTRY)


def select_backend(cfg: Config) -> ExecutionBackend:
    """The backend the operator asked for, by name. Raises on an unregistered name rather
    than falling back to local — a typo must not silently substitute an environment."""
    name = (getattr(cfg, "exec_backend", "") or "local").strip().lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise UnknownBackend(f"execution backend '{name}' is not registered; known backends: "
                             f"{', '.join(registered_backends())}")
    return cls(cfg)


def local_backend() -> LocalBackend:
    """The backend that runs code THIS harness authored — kept separate from
    `select_backend`, since a generated probe measures this machine."""
    return LocalBackend()


def backend_for(cfg: Config, spec: ProbeSpec) -> ExecutionBackend | None:
    """The backend this spec would run on, or None when the operator named an unknown
    one. `reimpl_exec` and `cert_exec` are the provenances without `spec.command` that are
    NOT exempted to `local_backend()`: model-authored code nobody ran before must be able
    to reach CONTAINER/REMOTE_SESSION isolation like `repo_exec` does."""
    if not spec.command and spec.provenance not in ("reimpl_exec", _CERT_PROVENANCE):
        return local_backend()
    try:
        return select_backend(cfg)
    except UnknownBackend:
        return None


def _fits(need: int | None, have: int | None) -> bool:
    """Unknown on EITHER side does not fit — an unmeasured offer is not a large one, and
    an unstated demand is not a small one."""
    return need is None or (have is not None and need <= have)


def select_for(requirement, cfg: Config, declared_platform: str = "",
              walltime_s: int | None = None) -> BackendSelection:
    """Which registered environment can host THIS experiment. Candidates are ranked so a
    runnable backend always beats a declared one, and among runnable ones the operator's
    named backend wins, then the smallest sufficient one."""
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
        preferred = 0 if name == (getattr(cfg, "exec_backend", "") or "local").strip().lower() else 1
        runnable.append(((preferred, p.vram_bytes or 0), backend, p))

    frozen = tuple(considered)
    offline = [w for _, v, w in considered if v == "unavailable"]
    if runnable and not requirement.memory_stated:
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
        return BackendSelection(
            None, "backend_unavailable",
            "an environment that can host this experiment is registered but not reachable: "
            + "; ".join(offline), frozen)
    if declared:
        names = ", ".join(p.name for _, p in declared)
        return BackendSelection(
            None, "credentials_unavailable",
            f"the experiment fits {names}, but that environment cannot be provisioned from "
            f"this host, so no reproduction can be attempted.", frozen)
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


# === HOST RESOURCE MEASUREMENT — from resources.py, unchanged ===========================
def host_vram_bytes() -> tuple[int | None, str, int]:
    """(vram_bytes of the largest GPU, its name, count), via nvidia-smi only."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None, "", 0
        rows = [r.split(",") for r in out.stdout.strip().splitlines() if r.strip()]
        if not rows:
            return None, "", 0
        best = max(rows, key=lambda r: int(r[0].strip()))
        return int(best[0].strip()) * (1024 ** 2), best[1].strip(), len(rows)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, "", 0


def host_ram_bytes() -> int | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class _STAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                           ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                           ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                           ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                           ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            s = _STAT()
            s.dwLength = ctypes.sizeof(_STAT)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return int(s.ullTotalPhys)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        return None


def host_disk_bytes(path: str | Path = ".") -> int | None:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


# === THE COMMIT BOUNDARY (E2) — from repo.py, unchanged ==================================
def head_commit(repo: Path, tree=None) -> str:
    from . import repo as repo_mod
    return repo_mod.head_commit(repo, tree)


def verify_commit(repo: str | Path, expected: str, tree=None) -> CommitVerification:
    """Is the checkout about to run the one that was audited? Fresh, at the point of use
    — reads the disk NOW rather than comparing two recorded strings, because the failure
    being closed is that a default branch moves or a cache refreshes between audit and
    execution. Only `verified` permits execution; `unknown` blocks, because "we never
    wrote down which commit we audited" is not evidence that this is that commit."""
    from . import repo as repo_mod
    return repo_mod.verify_commit(repo, expected, tree=tree)


# === ② AUDIT identity — the AND-point `authorize()` and `reconcile()` both consult. Not
# yet folded in here: `experiment_id.py`'s resolve_experiment/resolve_metric/
# resolve_configuration, the resolution algorithms that PRODUCE these identities. ========

# === ① RESOURCE CAPABILITY (E1) — from resources.py. `require_resources` (the EXTRACTION
# half) is not yet folded in; `assess_resources` (the COMPARISON) is, since `authorize()`
# depends on it directly. ==================================================================
def assess_resources(req, resources, backend: str = "",
                     walltime_budget_s: int | None = None):
    """Does the backend satisfy the requirement? `satisfied` is the only permitting
    state. Never looks for a configuration that would fit — the published experiment is
    the experiment."""
    from .schema import ResourceCapability

    def _gib(n):
        return "unknown" if n is None else f"{n / (1024 ** 3):.1f} GiB"

    cap = ResourceCapability(backend=backend, requirement=req)
    cap.available_vram_bytes = getattr(resources, "vram_bytes", None)
    cap.available_ram_bytes = getattr(resources, "ram_bytes", None)
    cap.available_disk_bytes = getattr(resources, "disk_bytes", None)
    cap.available_cpu_count = getattr(resources, "cpu_count", None)
    cap.available_gpu_count = getattr(resources, "gpu_count", None)
    cap.available_gpu_model = getattr(resources, "gpu_name", "") or ""

    if req is None:
        cap.state = "unassessed"
        cap.reason = "resource requirements were never examined for this experiment"
        return cap
    if not req.stated:
        cap.state = "unknown"
        cap.reason = (
            "neither the paper nor the repository states what this experiment costs, so it "
            "cannot be shown to fit. Silence about a demand is not evidence that the demand "
            "is small.")
        return cap

    checks = (("VRAM", req.vram_bytes, cap.available_vram_bytes, _gib),
             ("RAM", req.ram_bytes, cap.available_ram_bytes, _gib),
             ("disk", req.disk_bytes, cap.available_disk_bytes, _gib),
             ("CPU", req.cpu_count, cap.available_cpu_count, str),
             ("GPU count", req.gpu_count, cap.available_gpu_count, str))
    unmeasured = []
    for label, need, have, fmt in checks:
        if need is None:
            continue
        if have is None:
            unmeasured.append(f"{label}: the experiment declares {fmt(need)} and the backend "
                              f"cannot report what it has")
            continue
        if need > have:
            cap.shortfalls.append(
                f"{label}: the published experiment requires {fmt(need)}, the '{backend}' "
                f"backend offers {fmt(have)}")

    if walltime_budget_s and req.walltime_s and req.walltime_s > walltime_budget_s:
        cap.shortfalls.append(
            f"walltime: the paper declares {req.walltime_s / 3600:.1f}h for this experiment "
            f"and the execution budget is {walltime_budget_s / 3600:.1f}h")

    if cap.shortfalls:
        cap.state = "insufficient"
        cap.reason = ("the experiment as published does not fit this backend: "
                      + "; ".join(cap.shortfalls) + ". No reduced configuration is substituted.")
        return cap
    if not req.memory_stated:
        cap.state = "unknown"
        cap.reason = (
            "the sources establish some of this experiment's demands but not its memory: "
            f"{', '.join(n for n in req.unstated if n in ('vram', 'ram')) or 'vram, ram'} "
            "were never stated.")
        return cap
    if unmeasured:
        cap.state = "unknown"
        cap.reason = ("the backend could not report the resources this experiment declares: "
                      + "; ".join(unmeasured))
        return cap
    if req.vram_bytes is not None and req.vram_is_floor_only:
        cap.state = "unknown"
        cap.reason = ("the only established figure is the model weights' floor at fp16; "
                      "activations, optimizer state, gradients and any KV cache were never "
                      "stated, so fitting the floor proves nothing about fitting the run.")
        return cap
    cap.state = "satisfied"
    cap.reason = f"the experiment as published fits '{backend}'"
    return cap


# === ③ THE FIRST ORDERED LADDER — authorize(). 3 branches, 14 distinct decision strings. =
def authorize(cfg: Config, spec: ProbeSpec, backend: ExecutionBackend | None,
             commit: CommitVerification | None = None) -> ExecAuthorization:
    """May this spec execute? Pure function of the spec, the gates, the backend, and a
    FRESH commit verification supplied by the caller.

    Three branches, checked in this order:

      A. `reimpl_exec` — gated LIKE repository execution, not like our own code: 6 rungs
         (no_backend, backend_cannot_execute, backend_offline, gate_closed,
         isolation_insufficient, conformance_unproven). It is a MODEL's output, never
         reviewed by a human, exactly the third-party-code-execution risk isolation exists
         to confine.
      B. `not spec.command` — our own code, no repo gates apply. Free pass.
      C. repository execution — 11 rungs: no_backend, backend_cannot_execute,
         backend_offline, gate_closed, isolation_insufficient, provenance_insufficient,
         commit_unverified, identity_unproven, capability_unproven, resources_unproven,
         backend_mismatch.

    Provenance and commit sit together, before identity, since they answer the same prior
    question (is this the right code at all); identity precedes capability since a capable
    run of the wrong program is worse than a crash; resources come last.

    `commit` defaults to None, which refuses — a caller that forgets must fail closed.
    """
    if spec.provenance == _REIMPL_PROVENANCE:
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

    if spec.provenance == _CERT_PROVENANCE:
        # Mirrors the `reimpl_exec` ladder above rung for rung: a model-authored script
        # nobody has run before, gated LIKE repository execution, not branch B's free pass.
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
        if not getattr(cfg, "allow_certificate_exec", False):
            return ExecAuthorization(
                allowed=False, decision="gate_closed", backend=name,
                failure_class="execution_unauthorized",
                detail="SH_ALLOW_CERTIFICATE_EXEC is not set; running an exact-arithmetic "
                       "certificate stays an explicit per-invocation opt-in, separate from "
                       "SH_ALLOW_REPO_EXEC because this is not the authors' code")
        if not isolation_mod.sufficient_for_repo_exec(backend.profile().isolation):
            return ExecAuthorization(
                allowed=False, decision="isolation_insufficient", backend=name,
                failure_class="execution_unauthorized",
                detail=isolation_mod.refusal_detail(name, backend.profile().isolation))
        conf = spec.certificate_conformance
        if conf is None or not conf.established:
            return ExecAuthorization(
                allowed=False, decision="conformance_unproven", backend=name,
                failure_class="execution_unauthorized",
                detail=(conf.reason if conf is not None else
                        "no CertificateConformance was established for this spec: every "
                        "required element (hypotheses, claimed_bound, instance) must be "
                        "bound to a verified paper quote and implementation locator, and "
                        "independently verified, before a certificate may run"))
        return ExecAuthorization(
            allowed=True, decision="authorized", backend=name, failure_class="none",
            detail="every required element is bound to a verified paper quote and "
                   "implementation locator, the execution gate is open, and this backend "
                   "confines third-party code to a container or a leased remote session")

    if not spec.command:
        return ExecAuthorization(
            allowed=True, decision="not_repo_execution", backend=local_backend().name,
            detail="the code under test was authored by this harness or its operator, "
                   "not fetched from the paper's repository")

    if backend is None:
        return ExecAuthorization(allowed=False, decision="no_backend", backend="",
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

    # THE ISOLATION BOUNDARY — after the gate, before every scientific condition: "is
    # running somebody else's code here safe" is prior to "is it the right code".
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

    # E2 — the code that runs must be the code that was audited.
    if commit is None or not commit.established:
        detail = (commit.reason if commit is not None
                 else "the checkout was not verified against the audited commit before "
                      "execution, so it cannot be shown to be the code the audit read")
        return ExecAuthorization(allowed=False, decision="commit_unverified", backend=name,
                                 failure_class="commit_mismatch", detail=detail)

    proven, cls, why = identities_established(spec.experiment, spec.metric_identity, spec.configuration)
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

    # E1 — the published experiment must fit, and no substitute is offered.
    res = spec.resources
    if res is None or not res.established:
        return ExecAuthorization(
            allowed=False, decision="resources_unproven", backend=name,
            failure_class="resources_insufficient",
            detail=(res.reason if res is not None
                   else "the experiment's resource demand was never assessed against this "
                        "backend, so it cannot be shown to fit"))

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


# === METRIC PARSING — invariant 18: positional coincidence may never establish a
# reconciliation. ==========================================================================
_CELL_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_MERGED_DECIMALS = re.compile(r"\d+\.\d+\.\d+")
_VALUE_WITH_UNCERTAINTY = re.compile(
    r"^\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(?:\(\d|±|\+/-)")
_LEADING_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_DEVICE = re.compile(r"^SH_DEVICE\s+(\S+)")
_METRIC = re.compile(r"^SH_METRIC\s+arm=(\S+)\s+seed=(\d+)\s+value=([-\d.eE+]+)")
_AUX = re.compile(r"^SH_AUX\s+key=(\S+)\s+arm=(\S+)\s+seed=(\d+)\s+value=([-\d.eE+]+)")


def render_default(spec: ProbeSpec) -> str:
    body = DEFAULT_TEMPLATE
    for key, value in (("__METRIC__", spec.metric), ("__DATASET__", spec.dataset),
                      ("__ARMS__", "/".join(spec.arms)), ("__EPOCHS__", str(spec.epochs)),
                      ("__CLAIM__", (spec.claim or "(none given)").replace('"""', "'''"))):
        body = body.replace(key, value)
    return body


def write_probe(root: Path, spec: ProbeSpec, out_dir: Path | None = None) -> Path:
    """Materialize `runs/<paper_id>/probe.py`. When `spec.command` is set, nothing is
    generated — the code under test is the paper's own checkout."""
    path = (out_dir or (root / "runs" / spec.paper_id)) / "probe.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not spec.command:
        path.write_text(spec.script or render_default(spec), encoding="utf-8")
    return path


def resolve_command(cfg: Config, spec: ProbeSpec, script: Path, seed: int,
                    arm: str) -> tuple[list[str], Path]:
    """The argv and working directory for one (seed, arm) run. A bare leading `python` is
    rewritten to the chosen interpreter so a README command works unmodified."""
    if spec.command:
        argv = [str(c).replace("{seed}", str(seed)).replace("{arm}", arm) for c in spec.command]
        if argv and Path(argv[0]).name.lower() in ("python", "python3", "python.exe", "python3.exe"):
            argv[0] = spec.interpreter or cfg.python
        return argv, Path(spec.cwd) if spec.cwd else script.parent
    return ([spec.interpreter or cfg.python, str(script), "--seed", str(seed), "--arm", arm],
           script.parent)


def parse_cell_number(text: str) -> float | None:
    """The quantity a table cell reports, or None when the cell reports no single one.
    Three tiers: a token with two decimal points is an extraction fusion, refused
    outright; a leading number followed by an uncertainty shape yields its leading number;
    exactly one number yields that number, anything else is refused."""
    raw = _CELL_THOUSANDS.sub("", (text or ""))
    if _MERGED_DECIMALS.search(raw):
        return None
    if (m := _VALUE_WITH_UNCERTAINTY.match(raw)):
        return float(m.group(1))
    nums = _LEADING_NUMBER.findall(raw)
    return float(nums[0]) if len(nums) == 1 else None


def printed_precision_half_width(text: str) -> float:
    """Half the rounding interval implied by how many digits were printed — "59.3" implies
    [59.25, 59.35), half-width 0.05. Standard significant-figures reasoning: a reproduction
    must not be judged against precision the paper never claimed."""
    m = _LEADING_NUMBER.search((text or "").replace(",", ""))
    if not m:
        return 0.0
    try:
        exponent = Decimal(m.group()).as_tuple().exponent
    except (InvalidOperation, ValueError, TypeError):
        return 0.0
    if not isinstance(exponent, int):
        return 0.0
    return 0.5 * (10 ** exponent)


_TARGET_KEYS = ("split", "dataset", "task", "subset", "benchmark", "eval_set", "config",
               "experiment", "model")
_CONTAINER_KEYS = ("results", "metrics", "summary", "final", "eval", "test", "scores")
_NAMING_KEYS = ("metric", "name", "metric_name", "key")
METRIC_TIERS = ("target_bound", "named_artifact", "structured", "identity", "positional")


@dataclass(frozen=True)
class MetricParse:
    value: float | None = None
    tier: str = ""
    ambiguous: bool = False
    candidates: tuple[tuple[str, float], ...] = ()
    detail: str = ""

    @property
    def authoritative(self) -> bool:
        return (self.value is not None and not self.ambiguous
               and self.tier in ("target_bound", "named_artifact", "structured", "identity"))


def _json_objects(stdout: str) -> list[dict]:
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _number(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _tiered(obj: dict, key: str, experiment_hint: str) -> list[tuple[str, float]]:
    lowered = {str(k).lower(): v for k, v in obj.items()}
    hint = (experiment_hint or "").lower()
    found: list[tuple[str, float]] = []
    top = _number(lowered.get(key))
    if top is not None:
        matched = any(isinstance(lowered.get(tk), str) and hint and
                     (lowered[tk].lower() in hint or hint in lowered[tk].lower())
                     for tk in _TARGET_KEYS)
        if matched:
            found.append(("target_bound", top))
        named = any(isinstance(lowered.get(nk), str) and lowered[nk].lower() == key
                   for nk in _NAMING_KEYS)
        if named:
            found.append(("named_artifact", top))
        found.append(("identity", top))
    for ck in _CONTAINER_KEYS:
        inner = lowered.get(ck)
        if isinstance(inner, dict):
            v = _number({str(k).lower(): x for k, x in inner.items()}.get(key))
            if v is not None:
                found.append(("structured", v))
    return found


def parse_metric(stdout: str, key: str, experiment_hint: str = "") -> MetricParse:
    """The target's metric from a repository's stdout, or a refusal that says why. A
    preference order over how a value was IDENTIFIED, and a refusal when the winning tier
    disagrees with itself — never the last-JSON-object-wins scan invariant 18 forbids."""
    if not key:
        return MetricParse(detail="no metric identity was established, so no key is bound; "
                                  "a generic scan for whatever number parses is exactly the "
                                  "guess this refuses to make")
    key = key.lower()
    per_tier: dict[str, list[float]] = {}
    for obj in _json_objects(stdout):
        for tier, value in _tiered(obj, key, experiment_hint):
            per_tier.setdefault(tier, []).append(value)
    for tier in METRIC_TIERS:
        values = per_tier.get(tier) or []
        if not values:
            continue
        distinct = sorted(set(values))
        candidates = tuple((tier, v) for v in distinct)
        if len(distinct) > 1:
            return MetricParse(
                value=None, tier=tier, ambiguous=True, candidates=candidates,
                detail=(f"{len(distinct)} different values for '{key}' were reported at the "
                       f"same identification tier ({tier}): {', '.join(str(v) for v in distinct)}. "
                       f"Taking the last would be positional coincidence rather than evidence."))
        return MetricParse(value=distinct[0], tier=tier, candidates=candidates,
                           detail=f"'{key}' identified at tier {tier}")
    return MetricParse(detail=f"no JSON object on stdout reported '{key}'")


def _scale_ratio(a: float, b: float) -> float:
    if b == 0:
        return float("inf") if a else 1.0
    return abs(a / b)


# === STARTUP EVIDENCE ====================================================================
_SETUP_FAILURE_SIGNATURES = (
    "modulenotfounderror", "importerror", "cannot import name", "unrecognized arguments",
    "the following arguments are required", "invalid choice", "no such file or directory",
    "can't open file", "syntaxerror", "command not found",
    "is not recognized as an internal or external command",
)
_INFRA_FAILURE_SIGNATURES = (
    "out of memory", "cuda out of memory", "cuda error", "cublas", "cudnn", "nccl",
    "no space left on device", "disk quota exceeded", "segmentation fault", "core dumped",
    "access violation", "bus error", "driver/library version mismatch",
    "cuda driver version is insufficient", "the paging file is too small",
    "insufficient system resources", "memoryerror", "arraymemoryerror",
    "unable to allocate", "cannot allocate memory", "std::bad_alloc", "bad_alloc",
    "killed process", "oom-kill", "404 client error", "http error 404",
    "entrynotfounderror", "revisionnotfounderror", "401 client error", "403 client error",
    "gated repo", "repository not found", "you need to accept the license", "please log in",
    "authentication required", "connection refused", "could not resolve host",
    "getaddrinfo failed", "name or service not known", "temporary failure in name resolution",
    "max retries exceeded", "network is unreachable", "connectionerror", "sslerror",
)
_SIGNAL_RETURNCODES = (9, 11, 6, 8, 4, 7)
_INFRA_FAILURE_RETURNCODES = frozenset(
    {-n for n in _SIGNAL_RETURNCODES} | {128 + n for n in _SIGNAL_RETURNCODES}
    | {0xC0000005 - (1 << 32), 0xC00000FD - (1 << 32)})
_STARTUP_WINDOW_S = 30.0
_MIN_OUTPUT_LINES = 5


@dataclass
class StartupEvidence:
    """What the process showed us about whether it got past setup into the experiment —
    several independent signals, since a third-party repository owes this harness's own
    SH_* contract nothing."""
    saw_contract_line: bool = False
    saw_json_metric: bool = False
    stdout_lines: int = 0
    ran_seconds: float = 0.0
    timed_out: bool = False
    setup_error: str = ""
    infra_error: str = ""

    def describe(self) -> str:
        if self.infra_error:
            return f"the process reported '{self.infra_error}', an infrastructure failure"
        if self.setup_error:
            return f"the process reported '{self.setup_error}', a setup-phase failure"
        bits = []
        if self.saw_contract_line:
            bits.append("emitted the SH_ output contract")
        if self.saw_json_metric:
            bits.append("printed a parseable JSON metric")
        if self.stdout_lines:
            bits.append(f"{self.stdout_lines} line(s) of output")
        bits.append(f"ran {self.ran_seconds:.0f}s")
        if self.timed_out:
            bits.append("killed by timeout")
        return ", ".join(bits) if bits else "no output at all"


def classify_setup_error(stderr: str) -> str:
    low = (stderr or "").lower()
    for sig in _SETUP_FAILURE_SIGNATURES:
        if sig in low:
            return sig
    return ""


def classify_infra_failure(stderr: str, returncode: int | None = None) -> str:
    """Distinct from a setup failure: this can strike well AFTER the experiment started —
    a CUDA OOM twenty minutes in — and is never evidence the paper's code is broken."""
    low = (stderr or "").lower()
    for sig in _INFRA_FAILURE_SIGNATURES:
        if sig in low:
            return sig
    if returncode is not None and returncode in _INFRA_FAILURE_RETURNCODES:
        return f"process terminated by the operating system (exit code {returncode})"
    return ""


def reached_experiment(evidence: StartupEvidence) -> bool:
    """Did the run get past setup? The burden of proof is on establishing that it did — an
    unproven start yields INCONCLUSIVE (accuses nobody); assuming one yields
    FAILED_REPRODUCTION (names the authors). Those errors are not symmetric."""
    if evidence.setup_error:
        return False
    if evidence.timed_out:
        return False
    if evidence.saw_contract_line or evidence.saw_json_metric:
        return True
    long_enough = evidence.ran_seconds >= _STARTUP_WINDOW_S
    return long_enough and evidence.stdout_lines >= _MIN_OUTPUT_LINES


def _addressed(spec: ProbeSpec) -> str:
    ref = spec.claim_ref or spec.table_ref
    if not ref:
        return "the cited quantity"
    kind = spec.claim_kind or ("table_cell" if spec.table_ref else "")
    noun = {"table_cell": "cell", "prose_claim": "prose claim", "figure": "figure",
           "equation": "equation", "section_span": "section"}.get(kind, "quantity")
    return f"the {noun} at {ref}"


# === ④ THE SECOND ORDERED STRUCTURE — reconcile(). NOT a pure predicate table: 5 pure
# preconditions + 1 non-predicate RECORDER + a 4-rung failure ladder + an imperative
# arithmetic tail. Ported close to verbatim — see the module docstring for why. ===========
def reconcile(spec: ProbeSpec, values: list[float], noise_band: float,
             seeds_run: list[int], failure: str = "",
             evidence: StartupEvidence | None = None,
             authorization: ExecAuthorization | None = None) -> Reconciliation:
    """Executed metric vs the table cell the paper printed. Arithmetic, not judgement.

    Three situations must NOT reach a verdict: nothing parsed; the noise band is zero, so
    `<= 2 sigma` degenerates into demanding exactness; the two numbers differ by almost
    exactly 100x or 0.01x, a units mismatch in this harness rather than a doctored claim.
    A crash IS a failed reproduction (the code was run and did not work); a REFUSED run
    reconciles nothing (a fact about this harness's own gates, never about the paper).
    """
    evidence = evidence or StartupEvidence()
    rec = Reconciliation(table_ref=spec.table_ref, finding_id=spec.finding_id,
                         claim_ref=spec.claim_ref or spec.table_ref,
                         claim_kind=spec.claim_kind or ("table_cell" if spec.table_ref else ""),
                         target_id=spec.target_id, metric=spec.metric,
                         claimed_raw=spec.claimed_cell_value, provenance=spec.provenance,
                         noise_band=round(noise_band, 6), seeds_run=sorted(seeds_run))
    rec.claimed_value = parse_cell_number(spec.claimed_cell_value)

    # --- the authorization precondition ---------------------------------------------
    if authorization is not None and not authorization.allowed:
        rec.status = "INCONCLUSIVE"
        rec.failure_class = authorization.failure_class or "execution_unauthorized"
        rec.reached_experiment = False
        rec.reason = (
            f"the repository was not executed, so nothing was reproduced: "
            f"{authorization.detail} (decision '{authorization.decision}'). No reproduction "
            f"verdict is drawn, because a refusal by this harness is not evidence about "
            f"{_addressed(spec)}.")
        return rec

    if values:
        rec.reproduced_value = round(statistics.fmean(values), 6)
        rec.reproduced_std = round(statistics.stdev(values), 6) if len(values) > 1 else 0.0
    if rec.claimed_value is not None and rec.reproduced_value is not None:
        rec.delta_error = round(abs(rec.reproduced_value - rec.claimed_value), 6)

    # --- R2: THE RECORDER — assigned before the ceiling can return, so a refused
    # reconciliation still carries what identity actually found rather than the
    # "unmapped" field default.
    rec.experiment_state = spec.experiment.state if spec.experiment else "unmapped"
    rec.metric_state = spec.metric_identity.state if spec.metric_identity else "unmapped"
    rec.configuration_state = spec.configuration.state if spec.configuration else "unmapped"

    # --- the provenance ceiling -------------------------------------------------------
    if not provenance_mod.admits(spec.provenance):
        rec.status = "INCONCLUSIVE"
        near = ("" if rec.delta_error is None else
               f" (|delta| {rec.delta_error:.4f} against a 2-sigma band of {noise_band:.4f})")
        rec.reason = (
            f"the probe that ran was {spec.provenance}, not the paper's own code: "
            f"{'a mechanism reimplementation at toy scale' if spec.provenance == 'synthesized' else 'the identical-arms noise-floor template'}. "
            f"Its result{near} is evidence about the mechanism, not a reproduction of the "
            f"value printed at {_addressed(spec)}, so no reproduction verdict is drawn. "
            f"Open SH_ALLOW_REPO_EXEC to reconcile against the authors' code.")
        return rec

    # --- the reimplementation conformance precondition (decision 1) -------------------
    if spec.provenance == "reimpl_exec":
        conf = spec.reimplementation_conformance
        if conf is None or not conf.established:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "reimplementation_nonconformant"
            rec.reason = (
                f"this reconstruction is not fully conformant, so no verdict is drawn about "
                f"{_addressed(spec)}: "
                f"{(conf.reason if conf is not None else 'no ReimplementationConformance was recorded for this spec')}. "
                f"A disagreement from an unbound reconstruction would be a disagreement with "
                f"the reimplementer's own invention, not a finding about the paper's stated method.")
            return rec

    # --- the certificate conformance precondition + verdict (cert_exec) ---------------
    # A SEPARATE branch, returning early, rather than falling into the shared failure
    # ladder / arithmetic tail below: those are shaped for a printed-cell reproduction, and
    # a certificate's crash (a failed hypothesis assertion) means the CONSTRUCTED INSTANCE
    # was inadmissible under the theorem's own hypotheses — never a counterexample.
    if spec.provenance == "cert_exec":
        conf = spec.certificate_conformance
        if conf is None or not conf.established:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "reimplementation_nonconformant"
            rec.reason = (
                f"this certificate is not fully conformant, so no verdict is drawn about "
                f"{_addressed(spec)}: "
                f"{(conf.reason if conf is not None else 'no CertificateConformance was recorded for this spec')}. "
                f"A disagreement from an unbound certificate would be a disagreement with an "
                f"unverified construction, never a finding about the paper's stated theorem.")
            return rec
        rec.comparison_kind = rec.comparison_kind or "AGAINST_CLAIMED_BOUND"
        if failure:
            rec.status = "INCONCLUSIVE"
            rec.reached_experiment = reached_experiment(evidence)
            rec.reason = (
                f"the certificate script did not complete cleanly: {failure}. A failed "
                f"hypothesis assertion means the constructed instance was not admissible "
                f"under the theorem's own stated hypotheses — never a counterexample — and "
                f"a crash before any instance completed settles nothing about "
                f"{_addressed(spec)}.")
            return rec
        if not values:
            rec.status = "INCONCLUSIVE"
            rec.reason = ("the certificate produced no parseable SH_METRIC value for any "
                         "instance, so no verdict is drawn")
            return rec
        if any(v == 1 for v in values):
            rec.status = "COUNTEREXAMPLE_FOUND"
            rec.reason = (
                f"at least one of {len(values)} exact-arithmetic instance(s) satisfied "
                f"every hypothesis stated at {_addressed(spec)} and violated the bound it "
                f"claims — a concrete counterexample, checked in exact rational arithmetic.")
        else:
            rec.status = "NO_VIOLATION_FOUND"
            rec.reason = (
                f"all {len(values)} exact-arithmetic instance(s) satisfied the bound "
                f"claimed at {_addressed(spec)}. This checks the tested instances only and "
                f"is never a proof that the bound holds in general.")
        return rec

    # --- which comparison this is, and whether this system can perform it -------------
    if spec.comparison is not None:
        rec.comparison_kind = spec.comparison.kind
        rec.comparison_state = spec.comparison.state
        from . import decide as _decide
        if not _decide.admits_verdict(spec.comparison):
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "comparison_unestablished"
            rec.reason = (
                f"this target needed a "
                f"{spec.comparison.kind.lower().replace('_', ' ')} comparison and this "
                f"review could not carry one out: {spec.comparison.reason} Whatever ran "
                f"has measured something; there is nothing here to hold it against, so no "
                f"verdict is drawn about {_addressed(spec)}.")
            return rec
    elif spec.provenance:
        rec.comparison_kind = "AGAINST_PRINTED_VALUE"

    # --- the identity precondition (every admissible provenance except reimpl_exec,
    # whose analogous gate is the reconformance precondition just above) --------------
    if spec.provenance != "reimpl_exec":
        proven, failure_class, why = identities_established(
            spec.experiment, spec.metric_identity, spec.configuration)
        if not proven:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = failure_class
            rec.reason = (
                f"the executed program was not bound to the cited cell, so its output cannot "
                f"be compared with {_addressed(spec)}: {why}. A run that succeeds without "
                f"this binding has measured something, but not the thing the paper printed.")
            return rec

    # --- the capability precondition + the failure ladder ------------------------------
    if failure:
        cap = spec.capability
        if cap is not None and not cap.established:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = cap.reason_code
            rec.reached_experiment = False
            rec.reason = (
                f"the run could not be mounted fairly, so its failure is not evidence about the "
                f"paper: {cap.detail}. The process exited with: {failure}. Classified "
                f"'{cap.reason_code}' — a reproduction verdict requires that the experiment was "
                f"actually attempted under the environment the authors declared.")
            return rec

        if evidence.infra_error:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "infrastructure_failure"
            rec.reached_experiment = reached_experiment(evidence)
            rec.reason = (
                f"the run failed for an infrastructure reason ('{evidence.infra_error}'), a "
                f"fact about this host, its drivers or its network — not about the paper's "
                f"code: {failure}. Startup evidence: {evidence.describe()}. An infrastructure "
                f"failure is never read as a failed reproduction, however far the experiment "
                f"had progressed when it happened.")
            return rec

        reached = reached_experiment(evidence)
        rec.reached_experiment = reached
        if not reached:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "timeout" if evidence.timed_out else "startup_failure"
            if evidence.timed_out:
                why = ("this harness stopped the run at its own wall-clock limit, so whether "
                      "the experiment would have completed was never established")
            elif evidence.setup_error:
                why = (f"at least one attempt failed for a setup reason "
                      f"('{evidence.setup_error}'), so the run was not a fair attempt at the "
                      f"experiment even where other attempts produced output")
            else:
                why = "the process exited before any sign that the experiment itself began"
            rec.reason = (
                f"{why} ({evidence.describe()}): {failure}. Nothing here distinguishes a defect "
                f"in the paper's code from a failure of the runner, and the burden of showing "
                f"the experiment ran is on this harness.")
            return rec

        rec.status = "FAILED_REPRODUCTION"
        rec.failure_class = "runtime_failure"
        rec.reason = (f"the paper's code reached the experiment and then failed: {failure}. "
                     f"Startup evidence: {evidence.describe()}. A reproduction that runs and "
                     f"breaks is a failed reproduction, not an inconclusive one.")
        return rec

    # --- the arithmetic tail -------------------------------------------------------
    if rec.reproduced_value is None:
        rec.status = "INCONCLUSIVE"
        rec.reason = ("the run produced no parseable metric (no SH_METRIC line and no JSON "
                     "summary), so there is nothing to compare against the cell")
        return rec
    if rec.claimed_value is None:
        rec.status = "INCONCLUSIVE"
        rec.reason = (f"no number could be parsed out of the cited cell "
                     f"{spec.claim_ref or spec.table_ref or '(none cited)'}, so there is no claim to reconcile")
        return rec

    ratio = _scale_ratio(rec.claimed_value, rec.reproduced_value)
    if 50 <= ratio <= 200 or 0.005 <= ratio <= 0.02:
        rec.status = "INCONCLUSIVE"
        rec.reason = (f"the cell reads {rec.claimed_value:g} and the run produced "
                     f"{rec.reproduced_value:g}, a factor of about {ratio:.0f}x. That is a "
                     f"percentage-versus-fraction units mismatch in this harness, and it "
                     f"would be dishonest to score it as a failed reproduction.")
        return rec

    rec.claimed_precision = round(printed_precision_half_width(spec.claimed_cell_value), 6)
    effective_delta = max(0.0, rec.delta_error - rec.claimed_precision)
    precision_note = (
        f" (the cell's printed precision of ±{rec.claimed_precision:g} is credited first, "
        f"leaving an effective |delta| of {effective_delta:.4f})"
        if rec.claimed_precision else "")

    deterministic_count = (
        noise_band <= 0
        and spec.metric_identity is not None
        and spec.metric_identity.established
        and spec.metric_identity.cell_quantity == "count"
        and len(rec.seeds_run) > 1
        and rec.reproduced_std == 0.0)

    if deterministic_count:
        within = effective_delta <= 0
        rec.status = "RESOLVED_VERIFIED" if within else "FAILED_REPRODUCTION"
        rec.reason = (
            f"reproduced a count of {rec.reproduced_value:g} against the stated "
            f"{rec.claimed_value:g}, identically across {len(rec.seeds_run)} seeds. A count "
            f"has no seed-to-seed distribution, so the tolerance is the precision the paper "
            f"printed rather than a 2-sigma band: |delta| {rec.delta_error:.4f}{precision_note} "
            + ("is within it. The stated total stands." if within else "exceeds it."))
    elif noise_band <= 0:
        rec.status = "INCONCLUSIVE"
        rec.reason = (f"seed-to-seed noise measured as zero over {len(rec.seeds_run)} seeds, so "
                     f"the `<= 2 sigma` test has no band to test against. Delta was "
                     f"{rec.delta_error:.4f}{precision_note}.")
    elif effective_delta <= noise_band:
        rec.status = "RESOLVED_VERIFIED"
        rec.reason = (f"reproduced {rec.reproduced_value:g} against the cell's "
                     f"{rec.claimed_value:g}: |delta| {rec.delta_error:.4f}{precision_note} is "
                     f"within the 2-sigma band {noise_band:.4f}. The printed number stands.")
    else:
        rec.status = "FAILED_REPRODUCTION"
        rec.reason = (f"reproduced {rec.reproduced_value:g} against the cell's "
                     f"{rec.claimed_value:g}: |delta| {rec.delta_error:.4f}{precision_note} "
                     f"exceeds the 2-sigma band {noise_band:.4f} over {len(rec.seeds_run)} seeds.")
    if spec.provenance == "reimpl_exec" and rec.status in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION"):
        rec.reason += (
            " This ran as a governed reconstruction this harness's driver wrote and bound "
            "to the paper ingredient-by-ingredient (INDEPENDENT_REIMPLEMENTATION), not the "
            "authors' own code: this is a finding about whether the paper's stated method "
            "reproduces, never a statement that the authors' code failed.")
    return rec


def _stats(values: list[float]) -> ArmStats:
    return ArmStats(values=values, n=len(values),
                    mean=statistics.fmean(values) if values else 0.0,
                    std=statistics.stdev(values) if len(values) > 1 else 0.0)


def _noise(per_seed: dict[str, dict[int, float]], arms: list[str]) -> tuple[float, str]:
    """Seed-to-seed noise on the DIFFERENCE, not on either arm alone — shared variance
    cancels under a paired comparison. A paired spread of exactly zero is degenerate (the
    seed is not perturbing the arms independently), not precise, so it falls back to the
    arm spread rather than manufacturing infinite significance."""
    if len(arms) == 2:
        a, b = per_seed.get(arms[0], {}), per_seed.get(arms[1], {})
        shared = sorted(set(a) & set(b))
        if len(shared) > 1:
            paired = statistics.stdev([b[s] - a[s] for s in shared])
            if paired > 0:
                return paired, f"paired over {len(shared)} seeds"
    spreads = [_stats(list(v.values())).std for v in per_seed.values()]
    return (max(spreads) if spreads else 0.0), "unpaired (wider arm spread)"


def verify_execution_commit(spec: ProbeSpec,
                            backend: ExecutionBackend | None = None) -> CommitVerification | None:
    """Re-check, at the moment of execution, that the checkout is the audited commit —
    against the BACKEND's own tree, not necessarily this disk's."""
    if not spec.command:
        return None
    tree = backend.commit_tree(spec.cwd) if backend is not None else None
    return verify_commit(spec.cwd, spec.commit, tree=tree)


def _blocked(cfg: Config, root: Path, spec: ProbeSpec, auth: ExecAuthorization,
            seconds: float, commit: CommitVerification | None = None,
            results_dir: Path | None = None) -> ProbeResult:
    """The result of a run that was refused. A distinct verdict from 'failed': a
    repository whose execution was refused must not look like one whose code did not
    work."""
    result = ProbeResult(
        paper_id=spec.paper_id, finding_id=spec.finding_id, claim=spec.claim,
        verdict="blocked", provenance=spec.provenance, mechanism=spec.mechanism,
        rationale=spec.rationale, calibration=False, backend=auth.backend,
        authorization=auth, capability=spec.capability, experiment=spec.experiment,
        metric_identity=spec.metric_identity, configuration=spec.configuration,
        seconds=seconds, resources=spec.resources, commit_verification=commit,
        backend_selection=spec.backend_selection, backend_considered=spec.backend_considered,
        commit_state=spec.commit_state,
        reason=(f"execution was not authorized ('{auth.decision}'): {auth.detail}. "
               f"No process was started, so no measurement exists and no claim about "
               f"the paper is drawn from this."))
    if spec.claim_ref or spec.table_ref or spec.claimed_cell_value:
        result.reconciliation = reconcile(spec, [], 0.0, [], authorization=auth)
    results_dir = results_dir or state.control_dir(root)
    results_dir.mkdir(parents=True, exist_ok=True)
    state.write_json(results_dir / "probe_results.json", result.model_dump())
    return result


def run_probe(cfg: Config, root: Path, spec: ProbeSpec,
             backend: ExecutionBackend | None = None,
             out_dir: Path | None = None,
             results_dir: Path | None = None) -> ProbeResult:
    """Write the probe, run every (arm, seed) through the backend, aggregate. Authorization
    is re-checked HERE, against the spec about to run, rather than trusting whatever
    produced it applied the gates — a hand-written `spec.json` never passes through
    planning-time promotion."""
    t0 = time.time()
    results_dir = results_dir or out_dir or state.control_dir(root)

    # A spec claiming an admissible provenance and containing no program is refused, not
    # downgraded to the identical-arms template — that would let a calibration run become
    # a reproduction verdict.
    if provenance_mod.admits(spec.provenance) and not (spec.script or "").strip() \
           and not spec.command:
        blocked_auth = ExecAuthorization(
            allowed=False, decision="spec_incomplete",
            detail=(f"this spec declares provenance '{spec.provenance}', which the "
                   f"provenance ceiling admits, and carries neither a script nor a "
                   f"command. There is no program here to attribute to the authors or to "
                   f"a reviewer."))
        return _blocked(cfg, root, spec, blocked_auth, round(time.time() - t0, 1),
                        verify_execution_commit(spec, backend), results_dir=results_dir)

    backend = backend or backend_for(cfg, spec)
    commit = verify_execution_commit(spec, backend)
    auth = authorize(cfg, spec, backend, commit=commit)
    if not auth.allowed or backend is None:
        return _blocked(cfg, root, spec, auth, round(time.time() - t0, 1), commit,
                        results_dir=results_dir)

    script = write_probe(root, spec, out_dir)
    out_dir = script.parent
    mount_root = str(root / "runs" / spec.paper_id)

    records: list[ExecutionRecord] = []
    mislabelled: list[str] = []
    per_seed: dict[str, dict[int, float]] = {a: {} for a in spec.arms}
    aux_seed: dict[str, dict[str, dict[int, float]]] = {}
    device, failed, log = "unknown", [], []
    first_failure = ""
    evidence = StartupEvidence()
    for seed in spec.seeds:
        for arm in spec.arms:
            cmd, cwd = resolve_command(cfg, spec, script, seed, arm)
            cmd, cwd_staged = backend.stage(cmd, str(cwd), mount_root, pid=spec.paper_id)
            p = backend.execute(ExecRequest(argv=cmd, cwd=cwd_staged,
                                            timeout_s=cfg.probe_timeout_s,
                                            label=f"seed={seed} arm={arm}"))
            record = ExecutionRecord(
                seed=seed, arm=arm, backend=p.backend, argv=p.argv, cwd=p.cwd,
                launch_argv=list(getattr(p, "launch_argv", []) or []),
                environment=p.environment, interpreter=spec.interpreter, provenance=spec.provenance,
                commit=(spec.commit if spec.provenance == "repo_exec" else ""),
                started_at=p.started_at, ended_at=p.ended_at, seconds=p.seconds,
                launched=p.launched, completed=p.completed, timed_out=p.timed_out,
                returncode=p.returncode, stdout=p.stdout, stderr=p.stderr, error=p.error)
            records.append(record)
            evidence.ran_seconds = max(evidence.ran_seconds, p.seconds)
            if not p.completed:
                failed.append(seed)
                evidence.timed_out = evidence.timed_out or p.timed_out
                if not p.launched:
                    evidence.setup_error = evidence.setup_error or "could not start the process"
                first_failure = first_failure or p.error
                log.append({"seed": seed, "arm": arm, "rc": None, "error": p.error})
                continue
            evidence.stdout_lines = max(
                evidence.stdout_lines, sum(1 for ln in (p.stdout or "").splitlines() if ln.strip()))
            saw_metric = False
            for line in (p.stdout or "").splitlines():
                if d := _DEVICE.match(line):
                    device = d.group(1)
                    evidence.saw_contract_line = True
                elif m := _METRIC.match(line):
                    if m.group(1) != arm or int(m.group(2)) != seed:
                        mislabelled.append(
                            f"seed={seed} arm={arm} was passed, but the process reported "
                            f"seed={m.group(2)} arm={m.group(1)}")
                        continue
                    per_seed.setdefault(arm, {})[seed] = float(m.group(3))
                    record.metric = float(m.group(3))
                    saw_metric = True
                    evidence.saw_contract_line = True
                elif x := _AUX.match(line):
                    if x.group(2) != arm or int(x.group(3)) != seed:
                        mislabelled.append(
                            f"seed={seed} arm={arm} was passed, but an SH_AUX line reported "
                            f"seed={x.group(3)} arm={x.group(2)}")
                        continue
                    aux_seed.setdefault(x.group(1), {}).setdefault(arm, {})[seed] = float(x.group(4))
                    evidence.saw_contract_line = True
            metric_captured = saw_metric
            if spec.command and not saw_metric:
                bound_key = (spec.metric_identity.output_key
                            if spec.metric_identity and spec.metric_identity.established else "")
                hint = " ".join(v for v in (spec.configuration.matched or {}).values()
                                if isinstance(v, str)) if (
                                    spec.configuration and spec.configuration.established) else ""
                parsed = parse_metric(p.stdout or "", bound_key, hint)
                if parsed.authoritative and parsed.value is not None:
                    per_seed.setdefault(arm, {})[seed] = parsed.value
                    record.metric = parsed.value
                    evidence.saw_json_metric = True
                    metric_captured = True
                elif parsed.ambiguous:
                    mislabelled.append(parsed.detail)
                    log.append({"seed": seed, "arm": arm, "rc": p.returncode,
                               "metric_ambiguous": parsed.detail,
                               "candidates": [v for _, v in parsed.candidates]})
            if p.returncode != 0:
                err = (p.stderr or "").strip()[-400:]
                if metric_captured:
                    log.append({"seed": seed, "arm": arm, "rc": p.returncode, "error": err,
                               "note": "metric captured before this non-zero exit; not "
                                       "counted as a failed attempt"})
                else:
                    failed.append(seed)
                    sig = classify_setup_error(p.stderr or "")
                    infra_sig = classify_infra_failure(p.stderr or "", p.returncode)
                    evidence.setup_error = evidence.setup_error or sig
                    evidence.infra_error = evidence.infra_error or infra_sig
                    first_failure = first_failure or f"exit {p.returncode}: {err[-200:]}"
                    log.append({"seed": seed, "arm": arm, "rc": p.returncode, "error": err})

    if spec.command and records:
        commit_after = verify_execution_commit(spec)
        if commit_after is not None and commit_after.state != "verified":
            auth = ExecAuthorization(
                allowed=False, decision="commit_changed_during_execution", backend=auth.backend,
                failure_class="commit_mismatch",
                detail=(f"the checkout no longer verifies as the audited commit after "
                       f"execution ({commit_after.reason}); the repository changed while "
                       f"{len(records)} process(es) ran against it."))
            commit = commit_after

    arms = {a: _stats([per_seed.get(a, {})[s] for s in sorted(per_seed.get(a, {}))])
           for a in spec.arms}
    ok = [a for a in spec.arms if arms[a].n >= 2]
    calibration = not (spec.script or "").strip() and not spec.command
    result = ProbeResult(
        paper_id=spec.paper_id, finding_id=spec.finding_id, claim=spec.claim,
        device=device, seeds_run=sorted({s for v in per_seed.values() for s in v}),
        seeds_failed=sorted(set(failed)), arms=arms, claimed_delta=spec.claimed_delta,
        calibration=calibration, provenance=spec.provenance, mechanism=spec.mechanism,
        rationale=spec.rationale,
        aux={key: {arm: _stats([by_seed[s] for s in sorted(by_seed)])
                  for arm, by_seed in per_arm.items()}
            for key, per_arm in sorted(aux_seed.items())},
        seconds=round(time.time() - t0, 1), script_path=str(script),
        capability=spec.capability, experiment=spec.experiment,
        metric_identity=spec.metric_identity, configuration=spec.configuration,
        backend=backend.name, authorization=auth, resources=spec.resources,
        commit_verification=commit, backend_selection=spec.backend_selection,
        backend_considered=spec.backend_considered, commit_state=spec.commit_state)

    if len(ok) < len(spec.arms):
        result.verdict = "failed"
        extra = (f" {len(mislabelled)} metric line(s) were discarded for naming a seed or arm "
                f"the harness did not request: {mislabelled[0]}." if mislabelled else "")
        result.reason = (f"only {len(ok)}/{len(spec.arms)} arms produced >=2 seeds; "
                        f"{len(set(failed))} seed-run(s) failed.{extra} See probe_log.json.")
    else:
        std, how = _noise(per_seed, spec.arms)
        delta = arms[spec.arms[-1]].mean - arms[spec.arms[0]].mean
        result.measured_delta = round(delta, 6)
        result.measured_std = round(std, 6)
        result.noise_band = round(2 * std, 6)
        if std <= 0:
            result.verdict = "degenerate"
            result.reason = (
                f"seed-to-seed noise measured as exactly 0 over {len(result.seeds_run)} seeds "
                f"on {device}: the seed is not perturbing the pipeline. Measured delta was "
                f"{delta:+.4f}.")
        elif len(spec.arms) < 2:
            # One arm has no between-arm delta: "within_noise" of itself would contradict
            # the cell reconciliation below, which is this run's only verdict.
            result.verdict = "single_arm"
            result.reason = (
                f"one arm ({spec.arms[0] if spec.arms else '?'}), std {std:.4f} ({how}) over "
                f"{len(result.seeds_run)} seeds on {device}; see the cell reconciliation")
        elif calibration:
            if spec.claimed_delta is not None:
                result.claim_within_noise = abs(spec.claimed_delta) < 2 * std
            result.verdict = "calibration"
            result.reason = (
                f"noise-floor calibration on {device} over {len(result.seeds_run)} seeds: "
                f"std {std:.4f} ({how}), 2-sigma band {2 * std:.4f}. No paper-specific "
                f"script was evaluated, so no claim was reproduced.")
        else:
            result.is_overclaimed = abs(delta) < 2 * std
            if spec.claimed_delta is not None:
                result.claim_within_noise = abs(spec.claimed_delta) < 2 * std
            result.verdict = "within_noise" if result.is_overclaimed else "detectable"
            result.reason = (
                f"measured delta {delta:+.4f} vs 2-sigma noise band {2 * std:.4f} "
                f"(std {std:.4f}, {how}) on {device} over {len(result.seeds_run)} seeds")

    if spec.claim_ref or spec.table_ref or spec.claimed_cell_value:
        repro_arm = spec.arms[-1] if spec.arms else ""
        measured = per_seed.get(repro_arm) or {}
        if not measured:
            measured = {s: v for a in per_seed for s, v in per_seed[a].items()}
        result.reconciliation = reconcile(
            spec, [measured[s] for s in sorted(measured)], result.noise_band,
            sorted(measured), failure=first_failure, evidence=evidence, authorization=auth)

    if records:
        result.executions = len(records)
        result.execution_log = str(out_dir / "execution.jsonl")
        with (out_dir / "execution.jsonl").open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r.model_dump(), ensure_ascii=False) + "\n")

    state.write_json(results_dir / "probe_results.json", result.model_dump())
    if log:
        state.write_json(out_dir / "probe_log.json", log)
    return result


# --------------------------------------------------------------------------------------- #
def _self_check() -> None:
    import tempfile

    cfg = Config.load()

    # --- backends: the provider-neutral contract ----------------------------------------
    backend = select_backend(cfg)
    assert backend.name == "local" and isinstance(backend, LocalBackend)
    res = backend.resources()
    assert res.platform == sys.platform and res.python
    assert backend.available().usable

    out = backend.execute(ExecRequest([cfg.python, "-c", "print('SH_DEVICE cpu')"], "", 60))
    assert out.launched and out.completed and out.ok, out.stderr
    nope = backend.execute(ExecRequest(["sh-no-such-binary-anywhere"], "", 60))
    assert not nope.launched and not nope.completed

    for _name in registered_backends():
        _b = _REGISTRY[_name]()
        assert _b.name == _name and _b.profile().name == _name
        assert _b.resources().platform

    try:
        select_backend(type("C", (), {"exec_backend": "docker-typo", "allow_repo_exec": False})())
        raise AssertionError("an unregistered backend name must be refused")
    except UnknownBackend:
        pass

    # --- authorize(): the invariant, ordered exactly as the reference ---------------------
    own = ProbeSpec(paper_id="s")
    assert authorize(cfg, own, backend).allowed
    assert authorize(cfg, own, backend).decision == "not_repo_execution"

    repo_spec = ProbeSpec(paper_id="s", command=["python", "eval.py"], provenance="repo_exec")
    a = authorize(cfg, repo_spec, backend)
    assert not a.allowed and a.decision == "gate_closed"

    open_cfg = Config.load()
    open_cfg.allow_repo_exec = True
    a = authorize(open_cfg, repo_spec, backend)
    assert not a.allowed and a.decision == "isolation_insufficient", a.decision
    assert "CONTAINER or REMOTE_SESSION" in a.detail

    class _Confined(LocalBackend):
        name = "local"
        isolation = "REMOTE_SESSION"

    confined = _Confined(cfg)
    backend2 = confined
    a = authorize(open_cfg, repo_spec, backend2)
    assert not a.allowed and a.decision == "commit_unverified", a.decision
    good = CommitVerification(state="verified", expected="a" * 40, actual="a" * 40)
    a = authorize(open_cfg, repo_spec, backend2, commit=good)
    assert not a.allowed and a.decision == "identity_unproven", a.decision

    est = dict(state="established", established=True, reason="self-check fixture")
    repo_spec.experiment = ExperimentIdentity(**est)
    repo_spec.metric_identity = MetricIdentity(**est)
    repo_spec.configuration = ConfigurationIdentity(**est)
    a = authorize(open_cfg, repo_spec, backend2, commit=good)
    assert not a.allowed and a.decision == "capability_unproven", a.decision
    repo_spec.capability = ExecCapability(established=True, reason_code="established")
    a = authorize(open_cfg, repo_spec, backend2, commit=good)
    assert not a.allowed and a.decision == "resources_unproven", a.decision
    from .schema import ResourceCapability
    repo_spec.resources = ResourceCapability(state="satisfied", reason="fits")
    a = authorize(open_cfg, repo_spec, backend2, commit=good)
    assert a.allowed and a.decision == "authorized", a.decision

    laundered = repo_spec.model_copy(update={"provenance": "synthesized"})
    assert authorize(open_cfg, laundered, backend2, commit=good).decision == "provenance_insufficient"
    assert not authorize(open_cfg, repo_spec, None, commit=good).allowed

    # --- authorize(): cert_exec mirrors reimpl_exec's ladder, rung for rung --------------
    from .schema import ReimplementationConformance
    cert_spec = ProbeSpec(paper_id="s", script="print('x')", provenance="cert_exec")
    a = authorize(cfg, cert_spec, backend)
    assert not a.allowed and a.decision == "gate_closed", a.decision
    a = authorize(open_cfg, cert_spec, backend)
    assert not a.allowed and a.decision == "gate_closed", "cert_exec has its own gate"
    cert_open = Config.load()
    cert_open.allow_certificate_exec = True
    a = authorize(cert_open, cert_spec, backend)
    assert not a.allowed and a.decision == "isolation_insufficient", a.decision
    a = authorize(cert_open, cert_spec, backend2)
    assert not a.allowed and a.decision == "conformance_unproven", a.decision
    cert_spec.certificate_conformance = ReimplementationConformance(
        established=True, generated_by="g", verified_by="v", independently_verified=True)
    a = authorize(cert_open, cert_spec, backend2)
    assert a.allowed and a.decision == "authorized", a.decision

    # --- reconcile(): the arithmetic and the ceiling --------------------------------------
    def _spec(cell: str, provenance: str = "repo_exec", identity: bool = True) -> ProbeSpec:
        spec = ProbeSpec(paper_id="r", table_ref="T1:r0:c1", claimed_cell_value=cell,
                         provenance=provenance)
        if identity:
            spec.experiment = ExperimentIdentity(**est)
            spec.metric_identity = MetricIdentity(**est)
            spec.configuration = ConfigurationIdentity(**est)
        return spec

    ok = reconcile(_spec("59.28"), [59.30, 59.26], 0.10, [0, 1])
    assert ok.status == "RESOLVED_VERIFIED", ok.reason
    bad = reconcile(_spec("59.28"), [64.10, 64.20], 0.10, [0, 1])
    assert bad.status == "FAILED_REPRODUCTION", bad.reason
    crash = reconcile(_spec("59.28"), [], 0.10, [], failure="exit 1: ModuleNotFoundError",
                      evidence=StartupEvidence(setup_error="modulenotfounderror"))
    assert crash.status == "INCONCLUSIVE" and crash.failure_class == "startup_failure"
    ran = reconcile(_spec("59.28"), [], 0.10, [], failure="exit 1: RuntimeError: NaN loss",
                    evidence=StartupEvidence(saw_contract_line=True, stdout_lines=20, ran_seconds=90.0))
    assert ran.status == "FAILED_REPRODUCTION" and ran.failure_class == "runtime_failure"
    units = reconcile(_spec("97.0"), [0.9684, 0.9690], 0.01, [0, 1])
    assert units.status == "INCONCLUSIVE", "a 100x units mismatch must never convict a paper"
    synth_bad = reconcile(_spec("59.28", "synthesized"), [64.10, 64.20], 0.10, [0, 1])
    assert synth_bad.status == "INCONCLUSIVE"
    naive = reconcile(_spec("59.28", "driver", identity=False), [59.30, 59.26], 0.10, [0, 1])
    assert naive.status == "INCONCLUSIVE"

    # --- reconcile(): cert_exec — its own branch, never the printed-cell arithmetic ------
    def _cert_spec(claim_ref: str = "S3", conformance=None) -> ProbeSpec:
        return ProbeSpec(paper_id="r", claim_ref=claim_ref, claim_kind="section_span",
                         provenance="cert_exec", certificate_conformance=conformance)

    unbound_cert = reconcile(_cert_spec(), [0.0, 0.0], 0.0, [0, 1])
    assert unbound_cert.status == "INCONCLUSIVE", "no conformance recorded => no verdict"
    conf = ReimplementationConformance(established=True, generated_by="g", verified_by="v",
                                       independently_verified=True)
    crashed_cert = reconcile(_cert_spec(conformance=conf), [], 0.0, [],
                             failure="AssertionError: hypothesis failed",
                             evidence=StartupEvidence(setup_error="assertionerror"))
    assert crashed_cert.status == "INCONCLUSIVE", (
        "a failed hypothesis assertion is inadmissible, never a counterexample")
    no_values_cert = reconcile(_cert_spec(conformance=conf), [], 0.0, [])
    assert no_values_cert.status == "INCONCLUSIVE"
    violated = reconcile(_cert_spec(conformance=conf), [0.0, 1.0, 0.0], 0.0, [0, 1, 2])
    assert violated.status == "COUNTEREXAMPLE_FOUND", violated.reason
    assert violated.comparison_kind == "AGAINST_CLAIMED_BOUND"
    clean = reconcile(_cert_spec(conformance=conf), [0.0, 0.0, 0.0], 0.0, [0, 1, 2])
    assert clean.status == "NO_VIOLATION_FOUND", clean.reason

    # --- parse_metric: no positional coincidence ------------------------------------------
    one = parse_metric('{"eval_accuracy": 0.87}', "eval_accuracy")
    assert one.authoritative and one.value == 0.87 and one.tier == "identity"
    two = parse_metric('{"eval_accuracy": 0.81}\n{"eval_accuracy": 0.87}', "eval_accuracy")
    assert two.ambiguous and not two.authoritative
    bound = parse_metric('{"eval_accuracy": 0.81}\n{"split":"cifar100-test","eval_accuracy":0.87}',
                         "eval_accuracy", "cifar100-test resnet")
    assert bound.authoritative and bound.value == 0.87 and bound.tier == "target_bound"

    # --- run_probe end to end: the default template measures the noise floor -------------
    with tempfile.TemporaryDirectory() as td:
        spec = ProbeSpec(paper_id="selfcheck", seeds=[0, 1, 2], epochs=12,
                         claim="self-check: the default template measures the noise floor",
                         claimed_delta=0.002)
        r = run_probe(cfg, Path(td), spec)
        assert r.verdict != "failed", r.reason
        assert r.calibration is True and r.verdict == "calibration"
        assert r.reconciliation is None, "no cell was cited, so nothing may be reconciled"

    print("harness.execute self-check ok")


if __name__ == "__main__":
    _self_check()
