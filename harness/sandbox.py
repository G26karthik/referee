"""The remote sandbox: leasing a Linux machine, staging the audited commit into it.

`python -m harness.sandbox` runs the self-check (offline — it leases nothing).

This module is the *provider driver*. It knows how to obtain a sandbox, put one specific
commit inside it, ask it what hardware it actually has, and run a command in it. It has
no opinion about whether a command ought to run: `backends.authorize` is still the only
thing that may say yes, and `backends.SandboxBackend` is the thin class that presents
what is here through the `ExecutionBackend` interface every other stage already speaks.

**Why a session rather than a call.** A review runs one paper's checkout many times —
every (seed, arm) of every target — and a provider call per command would re-clone and
re-install for each one. So a sandbox is leased PER PAPER: `open_session` creates it,
fetches the audited commit, builds the repository's declared environment and measures
what it got; `run_in` then executes commands against that same live machine. This is
also what makes the commit guarantee meaningful — every run in the session is provably
the same tree, verified once, in place.

**Three things this deliberately does not do.**

*No experiment is authored here.* The reference implementation this restores ran a coding
agent inside the pod to write and run an experiment from a brief. That is the right shape
for generating research and the wrong shape for reviewing it: a number produced by code an
agent wrote in a container measures the agent, not the paper. What runs here is the
command the repository itself advertises, unmodified, exactly as under the local backend.

*Nothing is shrunk to fit.* There is no branch that lowers a batch size, drops precision
or picks a smaller GPU tier when the reservation is refused. `open_session` verifies the
machine it got against what was asked for and FAILS when it is smaller, because a run on
less hardware than the experiment declares is a different experiment.

*No silent local fallback.* If the driver is missing, the credentials are absent, the gate
is shut or the session cannot be staged, the outcome is a refusal with a stated reason.
A backend that quietly ran a Linux repository on the operator's Windows laptop because the
provider was down would be the exact substitution every preflight here exists to prevent.

**Credentials.** `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in the environment, or a
`~/.modal.toml` profile, or a `.env.sandbox` file beside the repo (see `config`). Nothing
in this module reads, logs or persists a token value; `driver_status` reports only whether
one is present.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .artifacts import GIB
from .config import Config

# --------------------------------------------------------------------------- #
# What the sandbox looks like from inside
# --------------------------------------------------------------------------- #
WORKSPACE = "/workspace"
REPO_DIR = f"{WORKSPACE}/repo"
ENV_DIR = f"{WORKSPACE}/env"
ENV_PYTHON = f"{ENV_DIR}/bin/python"
SESSION_FILE = "sandbox.json"

# Published VRAM per accelerator, used to DECLARE what a reservation offers so an
# experiment's stated demand can be matched against it before anything is leased.
#
# This is not the table `resources.py` deliberately refuses to have. That one would
# convert a paper's declared HARDWARE into a requirement ("they used an A100, so it needs
# 40 GiB"), which is the inference that blocks experiments fitting comfortably. This one
# runs the other way: it says what WE are asking the provider for, which is a fact about
# our own request and is verified against the machine after it arrives.
GPU_VRAM_BYTES: dict[str, int] = {
    "T4": 16 * GIB,
    "L4": 24 * GIB,
    "A10G": 24 * GIB,
    "A100": 40 * GIB,
    "A100-40GB": 40 * GIB,
    "A100-80GB": 80 * GIB,
    "L40S": 48 * GIB,
    "H100": 80 * GIB,
    "H200": 141 * GIB,
    "B200": 180 * GIB,
}

# Approximate on-demand rates ($/hour), carried so a lease can be priced and a report can
# say what a reproduction cost. An unknown tier prices at 0.0 and says so rather than
# guessing: a fabricated cost is worse than an absent one.
GPU_HOURLY_USD: dict[str, float] = {
    "": 0.0,
    "T4": 0.59, "L4": 0.80, "A10G": 1.10, "A100": 2.10, "A100-40GB": 2.10,
    "A100-80GB": 2.50, "L40S": 1.95, "H100": 3.95, "H200": 4.54, "B200": 6.25,
}
CPU_HOURLY_USD = 0.048       # per physical core, on-demand
MEM_HOURLY_USD_PER_GIB = 0.0057


def lease_cost_usd(spec: "SandboxSpec", seconds: float) -> float:
    """What a lease of this shape cost for this long, to the nearest hundredth of a cent.

    Returns the CPU+memory figure alone when the GPU tier is unknown, and never invents a
    rate for a tier that is not in the table.
    """
    hours = max(0.0, seconds) / 3600.0
    rate = GPU_HOURLY_USD.get(spec.gpu.upper(), GPU_HOURLY_USD.get(spec.gpu, 0.0))
    rate += spec.cpu * CPU_HOURLY_USD
    rate += (spec.memory_mib / 1024.0) * MEM_HOURLY_USD_PER_GIB
    return round(hours * rate, 6)


@dataclass(frozen=True)
class SandboxSpec:
    """The reservation to REQUEST. A declaration, verified once the machine arrives."""

    gpu: str = ""
    cpu: float = 4.0
    memory_mib: int = 16384
    # Declared, never requested: `Sandbox.create` has no disk parameter, so this is what
    # we claim to offer for requirement matching and is verified against what the machine
    # reports free once it exists.
    disk_gib: int = 50
    timeout_s: int = 3600
    idle_timeout_s: int = 900        # self-terminate if nothing runs; 0 disables
    python_version: str = "3.12"
    app_name: str = "single-harness-review"
    apt_packages: tuple[str, ...] = ("git", "ca-certificates", "curl", "build-essential")

    @property
    def vram_bytes(self) -> int | None:
        """None when no accelerator was requested, or when its tier is not in the table.

        `None` is 'we cannot say', and `backends._fits` treats an unknown offer as one
        that does not satisfy a stated demand — the same asymmetry E1 encodes. A tier we
        do not have a published figure for must therefore not report a number.
        """
        if not self.gpu:
            return None
        return GPU_VRAM_BYTES.get(self.gpu.upper()) or GPU_VRAM_BYTES.get(self.gpu)

    @property
    def gpu_count(self) -> int | None:
        if not self.gpu:
            return None
        # "A100:4" is the provider's own syntax for a multi-GPU reservation.
        _, _, count = self.gpu.partition(":")
        try:
            return max(1, int(count)) if count else 1
        except ValueError:
            return 1

    @property
    def gpu_kind(self) -> str:
        return self.gpu.partition(":")[0]

    def describe(self) -> str:
        return (f"{self.cpu:g} vCPU, {self.memory_mib} MiB RAM, "
                f"{self.gpu_kind or 'no GPU'}"
                + (f" x{self.gpu_count}" if (self.gpu_count or 0) > 1 else "")
                + f", python {self.python_version}, {self.timeout_s}s lease ceiling")


def spec_from_config(cfg: Config) -> SandboxSpec:
    return SandboxSpec(
        gpu=cfg.sandbox_gpu, cpu=cfg.sandbox_cpu, memory_mib=cfg.sandbox_memory_mib,
        disk_gib=cfg.sandbox_disk_gib, timeout_s=cfg.sandbox_timeout_s,
        idle_timeout_s=cfg.sandbox_idle_timeout_s,
        python_version=cfg.sandbox_python, app_name=cfg.sandbox_app)


# --------------------------------------------------------------------------- #
# Is the driver usable at all
# --------------------------------------------------------------------------- #
def credentials_present() -> bool:
    """Are provider credentials reachable? Checked, never read.

    Either both token variables, or a profile file the client would find on its own.
    """
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    return (Path.home() / ".modal.toml").is_file()


def driver_status(cfg: Config | None = None) -> tuple[bool, str]:
    """(usable, why not). Three separable causes, each with its own sentence.

    Kept apart because they need different things from an operator: install a package,
    supply a token, or open a gate. Collapsing them into "unavailable" tells a reader
    which of the three to do exactly never.
    """
    if cfg is not None and not cfg.allow_sandbox:
        return False, ("the sandbox gate SH_ALLOW_SANDBOX is closed; leasing remote compute "
                       "stays an explicit per-invocation opt-in because it spends money")
    try:
        import modal                                              # noqa: F401
    except Exception as e:                                        # noqa: BLE001
        return False, (f"the 'modal' client is not importable in this interpreter ({e}); "
                       f"install it with `pip install modal`")
    if not credentials_present():
        return False, ("no provider credentials are reachable: set MODAL_TOKEN_ID and "
                       "MODAL_TOKEN_SECRET (in the environment or in .env.sandbox), or run "
                       "`modal token new`")
    return True, ""


# --------------------------------------------------------------------------- #
# A leased machine
# --------------------------------------------------------------------------- #
@dataclass
class SandboxSession:
    """One live sandbox, with one verified checkout in it.

    Persisted to `runs/<pid>/sandbox.json` so the record outlives the process. Backends
    are re-instantiated freely — `plan_execution` builds one, `run_probe` builds another —
    so a session that lived only in an instance would be lost between planning and
    execution, and the second half of the run would have nowhere to go.

    `local_path` is the KEY. It is the local checkout this session mirrors, which is the
    one identifier both halves of the pipeline already carry: `capability()` receives it as
    `acq.path` and `execute()` receives it as `req.cwd`. Keying on it is how a sandbox is
    found again without widening the backend interface.
    """

    sandbox_id: str
    local_path: str
    spec: SandboxSpec
    repo_dir: str = REPO_DIR
    env_python: str = ""
    commit: str = ""
    url: str = ""
    paper_id: str = ""
    created_at: float = 0.0
    measured: dict[str, Any] = field(default_factory=dict)
    setup_log: list[str] = field(default_factory=list)

    # --- what the record says about the machine ---------------------------------------
    @property
    def platform(self) -> str:
        return str(self.measured.get("platform") or "linux")

    @property
    def gpu_name(self) -> str:
        return str(self.measured.get("gpu_name") or "")

    @property
    def age_seconds(self) -> float:
        return round(time.time() - self.created_at, 1) if self.created_at else 0.0

    def environment(self) -> str:
        """Where a process in this session runs, in one line, from what was MEASURED.

        Stamped onto every `ExecOutcome` and copied verbatim onto the execution record.
        A reproduction verdict from hardware nobody can identify afterwards is not
        evidence, so this names the sandbox, the platform it reported and the accelerator
        it actually has — not the tier that was requested.
        """
        parts = ["modal", f"sandbox:{self.sandbox_id}", self.platform,
                 str(self.measured.get("python_version") or ""),
                 self.gpu_name or "no GPU"]
        return " ".join(p for p in parts if p)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["spec"] = asdict(self.spec)
        return d

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SandboxSession":
        data = dict(data)
        spec = data.pop("spec", None) or {}
        known = {f for f in SandboxSpec.__dataclass_fields__}
        spec = {k: v for k, v in spec.items() if k in known}
        if isinstance(spec.get("apt_packages"), list):
            spec["apt_packages"] = tuple(spec["apt_packages"])
        return cls(spec=SandboxSpec(**spec),
                   **{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class RemoteRun:
    """What one command in the sandbox did. The shape `ExecOutcome` needs, no more."""

    launched: bool
    completed: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    error: str = ""
    argv: list[str] = field(default_factory=list)
    cwd: str = ""

    @property
    def ok(self) -> bool:
        return self.completed and self.returncode == 0


# In-process caches. `_SESSIONS` is the record; `_HANDLES` holds the live provider object
# so a second command in the same session does not pay another round trip to re-attach.
_SESSIONS: dict[str, SandboxSession] = {}
_HANDLES: dict[str, Any] = {}


def _key(local_path: str | Path) -> str:
    try:
        return str(Path(local_path).resolve()).lower()
    except OSError:
        return str(local_path).lower()


def session_path(local_path: str | Path) -> Path:
    """`runs/<pid>/sandbox.json` — beside the checkout, not inside it.

    Inside would make the checkout dirty and `verify_commit` would refuse the tree, which
    is the correct behaviour and the reason this file lives one level up.
    """
    return Path(local_path).parent / SESSION_FILE


def _remember(session: SandboxSession) -> SandboxSession:
    _SESSIONS[_key(session.local_path)] = session
    path = session_path(session.local_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session.to_json(), indent=2), encoding="utf-8")
    except OSError:
        pass                      # the in-process cache still works; the record is a bonus
    return session


def _forget(local_path: str | Path) -> None:
    key = _key(local_path)
    _SESSIONS.pop(key, None)
    _HANDLES.pop(key, None)
    try:
        session_path(local_path).unlink(missing_ok=True)
    except OSError:
        pass


def _handle(session: SandboxSession) -> Any:
    """The live provider object for a session, re-attaching by id if needed."""
    key = _key(session.local_path)
    if key in _HANDLES:
        return _HANDLES[key]
    import modal

    sb = modal.Sandbox.from_id(session.sandbox_id)
    _HANDLES[key] = sb
    return sb


def session_for(local_path: str | Path, *, revive: bool = True) -> SandboxSession | None:
    """The session staged for this checkout, or None.

    Reads the in-process cache first, then the persisted record. `revive` re-attaches to
    the provider and confirms the sandbox is still alive; a record naming a sandbox that
    has since timed out is removed rather than returned, because a dead session that
    still answers `session_for` would produce commands that fail for a reason having
    nothing to do with the paper.
    """
    key = _key(local_path)
    session = _SESSIONS.get(key)
    if session is None:
        path = session_path(local_path)
        if not path.is_file():
            return None
        try:
            session = SandboxSession.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None
        _SESSIONS[key] = session
    if not revive:
        return session
    try:
        probe = _run_raw(session, ["true"], workdir=WORKSPACE, timeout=60)
    except Exception:                                              # noqa: BLE001
        _forget(local_path)
        return None
    if not probe.launched:
        _forget(local_path)
        return None
    return session


def list_sessions(projects_dir: str | Path) -> list[SandboxSession]:
    """Every session record under `projects/`, alive or not. For `run.py sandbox`.

    Does not contact the provider: an operator asking what might still be billing needs
    the list even when the network is down, and `release` is what actually checks.
    """
    out: list[SandboxSession] = []
    for path in sorted(Path(projects_dir).glob(f"*/runs/*/{SESSION_FILE}")):
        try:
            out.append(SandboxSession.from_json(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return out


# --------------------------------------------------------------------------- #
# Running a command in a session
# --------------------------------------------------------------------------- #
def _text(stream) -> str:
    """Drain a provider stream into a string, whatever shape it hands back."""
    if stream is None:
        return ""
    try:
        data = stream.read()
    except Exception as e:                                         # noqa: BLE001
        return f"<stream unreadable: {e}>"
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    if isinstance(data, str):
        return data
    try:
        return "".join(
            x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x) for x in data)
    except TypeError:
        return str(data)


def _run_raw(session: SandboxSession, argv: list[str], *, workdir: str,
             timeout: int, env: dict[str, str] | None = None) -> RemoteRun:
    """One command in the sandbox. Never raises: every ending is data.

    The three endings the local backend distinguishes are distinguished here too, and for
    the same reason: a command that never started is this harness failing to launch
    anything, a command killed by the timeout was doing something for the whole window,
    and a command that exited has a code. Collapsing the first two loses the difference
    between a setup failure and an experiment that ran out of time.
    """
    t0 = time.time()
    try:
        sb = _handle(session)
    except Exception as e:                                         # noqa: BLE001
        return RemoteRun(launched=False, completed=False, argv=list(argv), cwd=workdir,
                         seconds=round(time.time() - t0, 3),
                         error=f"could not attach to sandbox {session.sandbox_id}: {e}")
    try:
        proc = sb.exec(*argv, workdir=workdir, timeout=timeout,
                       **({"env": env} if env else {}))
    except Exception as e:                                         # noqa: BLE001
        return RemoteRun(launched=False, completed=False, argv=list(argv), cwd=workdir,
                         seconds=round(time.time() - t0, 3),
                         error=f"could not start the process in the sandbox: {e}")
    # Read both streams BEFORE waiting. `wait()` on a process whose pipes are full blocks
    # forever, and the output is the only evidence about whether the experiment started.
    out, err = _text(getattr(proc, "stdout", None)), _text(getattr(proc, "stderr", None))
    rc: int | None = None
    wait_error = ""
    try:
        proc.wait()
        rc = getattr(proc, "returncode", None)
    except Exception as e:                                         # noqa: BLE001
        wait_error = str(e)
    seconds = round(time.time() - t0, 3)
    if rc is None:
        # No exit code came back. If the wall clock reached the window this is a timeout —
        # a launch, with whatever it printed — and otherwise it is a provider fault, which
        # is `infrastructure_failure` downstream and never a statement about the code.
        timed_out = seconds >= timeout - 1 or "timeout" in wait_error.lower()
        why = (f"timeout after {timeout}s" if timed_out else
               f"the sandbox returned no exit code: {wait_error or 'unknown'}")
        return RemoteRun(launched=True, completed=False, timed_out=timed_out, stdout=out,
                         stderr=err, seconds=seconds, argv=list(argv), cwd=workdir, error=why)
    return RemoteRun(launched=True, completed=True, returncode=rc, stdout=out, stderr=err,
                     seconds=seconds, argv=list(argv), cwd=workdir)


def _shell(session: SandboxSession, script: str, *, workdir: str = WORKSPACE,
           timeout: int = 600) -> RemoteRun:
    return _run_raw(session, ["bash", "-lc", script], workdir=workdir, timeout=timeout)


def translate_cwd(session: SandboxSession, local_cwd: str) -> str | None:
    """The in-sandbox directory a local working directory names, or None if it names none.

    The checkout root maps to `session.repo_dir` and a path inside it maps to the same
    relative position. Anything else returns None and the caller REFUSES — a command
    whose working directory could not be located must not be run somewhere plausible
    instead, which is how a run against the wrong tree would look exactly like a run
    against the right one.
    """
    if not local_cwd:
        return None
    try:
        here, base = Path(local_cwd).resolve(), Path(session.local_path).resolve()
    except OSError:
        return None
    if here == base:
        return session.repo_dir
    try:
        rel = here.relative_to(base)
    except ValueError:
        return None
    return str(PurePosixPath(session.repo_dir).joinpath(*rel.parts))


_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def translate_argv(session: SandboxSession, argv: list[str]) -> tuple[list[str], str]:
    """(argv for the sandbox, why it was refused). A refusal returns an empty argv.

    Only two rewrites happen, and both are of paths this harness itself put there:
    the interpreter, and any absolute path inside the staged checkout. A local absolute
    path pointing anywhere else is REFUSED rather than passed through — a Windows
    interpreter or a host file handed to a Linux sandbox produces a startup crash whose
    stderr is indistinguishable from the repository's own code being broken, and that is
    the misreading `reached_experiment` cannot undo after the fact.
    """
    out: list[str] = []
    for i, raw in enumerate(argv):
        arg = str(raw)
        mapped = translate_cwd(session, arg) if (
            _WINDOWS_PATH.match(arg) or arg.startswith("/")) else None
        if mapped is not None:
            out.append(mapped)
            continue
        if arg.startswith(WORKSPACE + "/") or arg == WORKSPACE:
            out.append(arg)                       # already sandbox-native (the built venv)
            continue
        if _WINDOWS_PATH.match(arg):
            return [], (f"argument {i} ('{arg[:80]}') is a path on this host and names "
                        f"nothing in the sandbox; the command was not rewritten to make "
                        f"it run somewhere else")
        out.append(arg)
    return out, ""


def run_in(session: SandboxSession, argv: list[str], local_cwd: str, timeout: int,
           env: dict[str, str] | None = None) -> RemoteRun:
    """Run one command against a staged session. The whole of what `execute()` needs."""
    workdir = translate_cwd(session, local_cwd) or session.repo_dir
    mapped, refused = translate_argv(session, argv)
    if refused:
        return RemoteRun(launched=False, completed=False, argv=list(argv), cwd=workdir,
                         error=refused)
    return _run_raw(session, mapped, workdir=workdir, timeout=timeout, env=env)


# --------------------------------------------------------------------------- #
# Asking the machine what it is
# --------------------------------------------------------------------------- #
_INVENTORY = r"""
import json, os, shutil, subprocess, sys
out = {"platform": sys.platform,
       "python_version": ".".join(str(n) for n in sys.version_info[:3]),
       "cpu_count": os.cpu_count()}
try:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                out["ram_bytes"] = int(line.split()[1]) * 1024
                break
except Exception:
    pass
try:
    out["disk_bytes"] = shutil.disk_usage("/").free
except Exception:
    pass
try:
    p = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                        "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=90)
    rows = [r.strip() for r in (p.stdout or "").splitlines() if r.strip()]
    if p.returncode == 0 and rows:
        names, mibs = [], []
        for r in rows:
            name, _, mib = r.partition(",")
            names.append(name.strip())
            try:
                mibs.append(int(float(mib.strip())))
            except ValueError:
                pass
        out["gpu_count"] = len(rows)
        out["gpu_name"] = names[0] if names else ""
        if mibs:
            out["vram_bytes"] = max(mibs) * 1024 * 1024
except Exception:
    pass
print("SH_INVENTORY " + json.dumps(out))
"""


def inventory(session: SandboxSession, timeout: int = 180) -> dict[str, Any]:
    """What the sandbox reports about itself. `{}` when it could not be asked."""
    run = _run_raw(session, ["python", "-c", _INVENTORY], workdir=WORKSPACE, timeout=timeout)
    for line in (run.stdout or "").splitlines():
        if line.startswith("SH_INVENTORY "):
            try:
                return dict(json.loads(line[len("SH_INVENTORY "):]))
            except ValueError:
                return {}
    return {}


def _shortfall(spec: SandboxSpec, measured: dict[str, Any]) -> list[str]:
    """Where the machine is smaller than the reservation. Empty means it is not.

    An UNMEASURED quantity is not a shortfall — the sandbox may simply not expose it —
    but a measured quantity below what was asked for is, and it must stop the session
    rather than be recorded and ignored. The one that matters most is VRAM: a run that
    silently got a T4 where an A100 was reserved produces a number from different
    hardware than the resource preflight cleared, and nothing downstream can tell.

    "We could not ask the machine anything" is a THIRD state and is not decided here.
    An empty inventory once landed in this function and came out as "no accelerator is
    visible", which is a claim about the hardware made from an absence of evidence about
    the hardware — the same error as reading a paper's silence about its memory cost as a
    small cost. `open_session` refuses an uninterrogable machine on its own terms, before
    this comparison is attempted at all.
    """
    short: list[str] = []
    want_vram = spec.vram_bytes
    have_vram = measured.get("vram_bytes")
    if want_vram and have_vram is not None and have_vram < want_vram * 0.9:
        short.append(f"VRAM {have_vram // GIB} GiB against {want_vram // GIB} GiB reserved")
    if want_vram and measured.get("gpu_count") == 0:
        short.append(f"no accelerator is visible although {spec.gpu} was reserved")
    want_gpus = spec.gpu_count or 0
    have_gpus = measured.get("gpu_count")
    if want_gpus > 1 and have_gpus is not None and have_gpus < want_gpus:
        short.append(f"{have_gpus} GPU(s) against {want_gpus} reserved")
    have_ram = measured.get("ram_bytes")
    if have_ram is not None and have_ram < spec.memory_mib * 1024 * 1024 * 0.9:
        short.append(f"RAM {have_ram // GIB} GiB against {spec.memory_mib // 1024} GiB reserved")
    return short


# --------------------------------------------------------------------------- #
# Staging one commit into a fresh machine
# --------------------------------------------------------------------------- #
class SandboxSetupError(RuntimeError):
    """Staging failed. Carries the sentence the acquisition record will report."""


def _image(spec: SandboxSpec):
    import modal

    img = modal.Image.debian_slim(python_version=spec.python_version)
    if spec.apt_packages:
        img = img.apt_install(*spec.apt_packages)
    return img.env({"PYTHONUNBUFFERED": "1", "GIT_TERMINAL_PROMPT": "0",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1"})


def _create(spec: SandboxSpec):
    import modal

    app = modal.App.lookup(spec.app_name, create_if_missing=True)
    kwargs: dict[str, Any] = {
        "app": app, "image": _image(spec), "timeout": spec.timeout_s,
        "cpu": spec.cpu, "memory": spec.memory_mib, "workdir": WORKSPACE,
    }
    if spec.gpu:
        kwargs["gpu"] = spec.gpu
    if spec.idle_timeout_s > 0:
        kwargs["idle_timeout"] = spec.idle_timeout_s
    # No `block_network`: staging needs the network to fetch the audited commit and to
    # install the repository's declared stack, and it cannot be turned off after creation.
    # `BackendProfile.network_at_runtime` says so rather than claiming an isolation that
    # is not there.
    return modal.Sandbox.create(**kwargs)


def _fetch_script(url: str, commit: str) -> str:
    """Bring exactly one commit into a fresh checkout, and stand on it.

    `fetch --depth 1 origin <sha>` rather than a clone: the audited commit is usually not
    the tip of the default branch, and asking the server for that one object graph is what
    both GitHub and GitLab support. A clone of the tip followed by a checkout would fail on
    every paper whose repository has moved since it was audited — and standing on the tip
    instead is the substitution `verify_commit` exists to catch.
    """
    return "\n".join([
        "set -euo pipefail",
        f"rm -rf {shlex.quote(REPO_DIR)}",
        f"mkdir -p {shlex.quote(REPO_DIR)}",
        f"cd {shlex.quote(REPO_DIR)}",
        "git init -q .",
        f"git remote add origin {shlex.quote(url)}",
        f"git fetch -q --depth 1 origin {shlex.quote(commit)}",
        "git checkout -q --force FETCH_HEAD",
        "git submodule update -q --init --depth 1 --recursive || true",
        "git rev-parse HEAD",
    ])


def _venv_script(spec: SandboxSpec, requirement_files: list[str]) -> str:
    """Build the repository's declared stack, exactly as `repo.build_env` does locally.

    Only `.txt` requirement files, for the same reason: a conda `environment.yml` is not
    installable with pip, and pretending otherwise would build a *different* environment
    and then call it the repository's own.
    """
    lines = ["set -euo pipefail", f"python -m venv {shlex.quote(ENV_DIR)}",
             f"{shlex.quote(ENV_PYTHON)} -m pip install -q --upgrade pip setuptools wheel"]
    for rel in requirement_files:
        target = str(PurePosixPath(REPO_DIR) / rel.replace("\\", "/"))
        lines.append(f"{shlex.quote(ENV_PYTHON)} -m pip install -r {shlex.quote(target)}")
    lines.append(f"{shlex.quote(ENV_PYTHON)} -c 'import sys; print(sys.version)'")
    return "\n".join(lines)


def open_session(cfg: Config, *, local_path: str, url: str, commit: str,
                 requirement_files: list[str] | None = None,
                 paper_id: str = "", reuse: bool = True) -> SandboxSession:
    """Lease a machine, put the audited commit in it, build the repo's stack, measure it.

    Raises `SandboxSetupError` with a reportable sentence on every failure, and never
    leaves a sandbox running behind one: a lease that cannot be used is a lease that is
    only costing money.

    Nothing is leased until every precondition holds, and the preconditions are ordered so
    the sentence a caller gets back names something whose fixing would actually unblock it:

      1. no URL, or no audited commit — this can never work, with any credentials
      2. the network / install gates — permission to fetch and to build
      3. the sandbox gate, the client, the credentials — permission and equipment

    The first group is first on purpose. Reporting "supply a provider token" to a caller
    with no audited SHA would send an operator to fix something that would not help: the
    commit is required BEFORE the machine, because staging "whatever the default branch
    points at" is not a thing this function can do.
    """
    if reuse and (existing := session_for(local_path)) is not None:
        if existing.commit == (commit or "").strip().lower() and existing.env_python:
            return existing
        # A session staged for a DIFFERENT commit is not this run's session. Release it
        # rather than reuse it: the whole guarantee is that every command in a session
        # ran against one verified tree.
        release(local_path)

    commit = (commit or "").strip().lower()
    if not url:
        raise SandboxSetupError("no repository URL to stage into a sandbox")
    if len(commit) != 40:
        raise SandboxSetupError(
            "no full audited commit was recorded for this checkout, so there is nothing to "
            "stage; a sandbox holding the default branch would not be the code that was "
            "audited")
    if not (cfg.allow_network and cfg.allow_install):
        raise SandboxSetupError(
            "a remote session needs both SH_ALLOW_NETWORK (to fetch the audited commit) "
            "and SH_ALLOW_INSTALL (to build the repository's declared stack); "
            f"network={'on' if cfg.allow_network else 'off'}, "
            f"install={'on' if cfg.allow_install else 'off'}")
    ok, why = driver_status(cfg)
    if not ok:
        raise SandboxSetupError(why)

    spec = spec_from_config(cfg)
    t0 = time.time()
    try:
        sb = _create(spec)
    except Exception as e:                                         # noqa: BLE001
        raise SandboxSetupError(f"the provider refused a {spec.describe()} sandbox: {e}") from e

    session = SandboxSession(
        sandbox_id=getattr(sb, "object_id", "") or "", local_path=str(local_path), spec=spec,
        commit=commit, url=url, paper_id=paper_id, created_at=t0)
    _HANDLES[_key(local_path)] = sb

    def fail(message: str) -> "SandboxSetupError":
        try:
            sb.terminate()
        except Exception:                                          # noqa: BLE001
            pass
        _forget(local_path)
        return SandboxSetupError(message)

    if not session.sandbox_id:
        raise fail("the provider returned a sandbox with no identifier, so the run could "
                   "not be located afterwards")

    budget = max(120, cfg.sandbox_setup_timeout_s)
    # Keep the session record on disk from here on, so a crash mid-setup still leaves an
    # operator something `run.py sandbox release` can act on.
    _remember(session)

    fetched = _shell(session, _fetch_script(url, commit), timeout=min(budget, 1200))
    session.setup_log.append(f"fetch rc={fetched.returncode} in {fetched.seconds}s")
    if not fetched.ok:
        raise fail(f"the audited commit {commit[:12]} could not be staged in the sandbox: "
                   f"{(fetched.stderr or fetched.error or '').strip()[-300:]}")

    session.measured = inventory(session)
    if not session.measured:
        # Distinct from a shortfall, and it has to be: an empty inventory is an absence of
        # evidence about the machine, not evidence of a small one. It still blocks — a
        # number from hardware nobody could identify is not a reproduction — but it blocks
        # saying what is actually wrong, which is that this harness could not interrogate
        # the sandbox it just leased.
        raise fail("the sandbox was leased but could not be interrogated, so the hardware "
                   "a run there would use cannot be established; nothing was executed")
    if short := _shortfall(spec, session.measured):
        # Refused, not recorded and continued. Nothing here reduces the experiment to fit
        # the machine that turned up, and running on less hardware than the preflight
        # cleared would produce a number from an environment nobody assessed.
        raise fail("the sandbox that was provisioned is smaller than the reservation: "
                   + "; ".join(short))

    built = _shell(session, _venv_script(spec, list(requirement_files or [])),
                   timeout=budget)
    session.setup_log.append(f"venv rc={built.returncode} in {built.seconds}s")
    if not built.ok:
        raise fail("the repository's declared stack could not be installed in the sandbox: "
                   + (built.stderr or built.error or "").strip()[-400:])
    session.env_python = ENV_PYTHON

    # Detach so the sandbox outlives this process. Every later command re-attaches by id,
    # and `release` is what ends the lease.
    try:
        sb.detach()
    except Exception:                                              # noqa: BLE001
        pass
    return _remember(session)


def release(local_path: str | Path) -> tuple[bool, str]:
    """End the lease for this checkout. (released, detail). Idempotent.

    Called from `stages/probe.run` in a `finally`, so a paper that crashes mid-review
    still gives the machine back. A sandbox left running bills until its own timeout,
    which is the one failure in this module that costs the operator money rather than
    accuracy — so it is safe to call with no session, twice, or after the sandbox has
    already died.
    """
    session = session_for(local_path, revive=False)
    if session is None:
        return False, "no sandbox session is recorded for this checkout"
    detail = f"sandbox {session.sandbox_id} released after {session.age_seconds}s"
    cost = lease_cost_usd(session.spec, session.age_seconds)
    if cost:
        detail += f" (~${cost:.4f})"
    try:
        _handle(session).terminate()
    except Exception as e:                                         # noqa: BLE001
        detail = f"sandbox {session.sandbox_id} could not be terminated: {e}"
        _forget(local_path)
        return False, detail
    _forget(local_path)
    return True, detail


def release_all(projects_dir: str | Path) -> list[tuple[str, bool, str]]:
    """Release every recorded session. For `run.py sandbox release --all`."""
    out = []
    for session in list_sessions(projects_dir):
        ok, detail = release(session.local_path)
        out.append((session.sandbox_id, ok, detail))
    return out


# --------------------------------------------------------------------------- #
# The two pure checks that have to happen INSIDE the machine
# --------------------------------------------------------------------------- #
class SandboxGitTree:
    """`repo.GitTree` over a staged session, so commit verification runs in the sandbox.

    The point is that `repo.verify_commit` is not reimplemented here. Its logic is the
    fail-closed part — a redirected `.git`, an assume-unchanged path, an uninspectable
    tree and a dirty tree each block, and each blocks for a stated reason — and a second
    copy of it would be a second thing to get wrong. So the *reads* are abstracted and the
    *reasoning* is shared: the same function certifies a local checkout and a remote one.
    """

    def __init__(self, session: SandboxSession, timeout: int = 180) -> None:
        self.session = session
        self.timeout = timeout
        self.path = session.repo_dir

    def git(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        run = _run_raw(self.session, ["git", *args], workdir=self.session.repo_dir,
                       timeout=min(self.timeout, max(30, timeout)))
        if not run.launched:
            return 128, "", run.error
        return (run.returncode if run.returncode is not None else 128), run.stdout, run.stderr

    def is_dir(self) -> bool:
        rc, out, _ = self.git(["rev-parse", "--show-toplevel"], 60)
        return rc == 0 and bool((out or "").strip())

    def is_file(self, relpath: str) -> bool:
        target = str(PurePosixPath(self.session.repo_dir) / relpath)
        run = _shell(self.session, f"test -f {shlex.quote(target)}",
                     workdir=self.session.repo_dir, timeout=60)
        return bool(run.ok)


class AbsentGitTree:
    """A checkout that is not there. Every read fails, so `verify_commit` reports `unknown`.

    Returned when a sandbox backend is asked to certify a tree for a paper it has no
    session for. The alternative — returning None and letting verification fall back to
    the LOCAL checkout — would certify a tree on this disk and then run somewhere that
    does not have it, which is the one substitution the commit check exists to prevent.
    So a missing session fails closed, with a sentence that says what is missing.
    """

    def __init__(self, path: str, why: str) -> None:
        self.path = path
        self.why = why

    def git(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        return 128, "", self.why

    def is_dir(self) -> bool:
        return False

    def is_file(self, relpath: str) -> bool:
        return False


def import_resolver(session: SandboxSession) -> Callable[[str, Path, list[str]], list[str]]:
    """A `repo.missing_imports` that asks the SANDBOX's interpreter, not this one.

    Without it, capability assessment for a remote backend runs `find_spec` against a
    Linux path on a Windows host, fails to start, and reports every dependency missing —
    so a perfectly capable session would be refused as `dependency_missing`, which is a
    statement about the environment that would be wrong in the most expensive direction.
    """
    probe = (
        "import importlib.util, json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "out = []\n"
        "for m in sys.argv[2:]:\n"
        "    try:\n"
        "        if importlib.util.find_spec(m) is None: out.append(m)\n"
        "    except Exception: out.append(m)\n"
        "print('SH_MISSING ' + json.dumps(out))\n"
    )

    def resolve(interpreter: str, repo: Path, modules: list[str]) -> list[str]:
        if not modules:
            return []
        argv = [interpreter or session.env_python or "python", "-c", probe,
                session.repo_dir, *modules]
        run = _run_raw(session, argv, workdir=session.repo_dir, timeout=240)
        if not run.ok:
            return list(modules)          # cannot ask: unresolved, never "fine"
        for line in (run.stdout or "").splitlines():
            if line.startswith("SH_MISSING "):
                try:
                    return list(json.loads(line[len("SH_MISSING "):]))
                except ValueError:
                    return list(modules)
        return list(modules)

    return resolve


if __name__ == "__main__":  # self-check: python -m harness.sandbox  (leases nothing)
    cfg = Config.load()

    # --- the declared reservation -----------------------------------------------------
    spec = spec_from_config(cfg)
    assert spec.vram_bytes is None or spec.vram_bytes > 0
    assert SandboxSpec(gpu="").vram_bytes is None, "no GPU asked for reports no VRAM"
    assert SandboxSpec(gpu="A100-80GB").vram_bytes == 80 * GIB
    assert SandboxSpec(gpu="a100-80gb").vram_bytes == 80 * GIB, "tier names are case-free"
    assert SandboxSpec(gpu="H100:4").gpu_count == 4 and SandboxSpec(gpu="H100:4").gpu_kind == "H100"
    assert SandboxSpec(gpu="RTX-9090").vram_bytes is None, "an unpublished tier states nothing"
    assert lease_cost_usd(SandboxSpec(gpu="RTX-9090"), 3600) > 0, "cpu+mem still price"

    # --- a shortfall is a refusal, an unmeasured quantity is a DIFFERENT refusal -------
    big = SandboxSpec(gpu="A100-80GB", memory_mib=16384)
    assert _shortfall(big, {}) == [], (
        "an empty inventory is an absence of evidence about the machine, not evidence of "
        "a small one; `open_session` refuses it on its own terms")
    assert _shortfall(big, {"gpu_count": 1, "vram_bytes": 80 * GIB,
                            "ram_bytes": 16 * GIB}) == []
    assert _shortfall(big, {"gpu_count": 1, "vram_bytes": 16 * GIB}), "a T4 is not an A100"
    assert _shortfall(big, {"gpu_count": 0}), "no card where one was reserved"
    assert _shortfall(big, {"ram_bytes": 4 * GIB}), "4 GiB is not the 16 reserved"
    assert _shortfall(SandboxSpec(gpu="H100:8"),
                      {"gpu_count": 2, "vram_bytes": 80 * GIB}), "2 of 8 is short"
    assert _shortfall(SandboxSpec(), {"gpu_count": 0}) == [], "no GPU asked for, none missed"

    # --- path translation refuses rather than relocating ------------------------------
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        # The real layout, because `list_sessions` globs it: projects/<pid>/runs/<pid>/repo.
        checkout = Path(td) / "p" / "runs" / "p" / "repo"
        (checkout / "sub").mkdir(parents=True)
        s = SandboxSession(sandbox_id="sb-1", local_path=str(checkout), spec=spec)
        assert translate_cwd(s, str(checkout)) == REPO_DIR
        assert translate_cwd(s, str(checkout / "sub")) == f"{REPO_DIR}/sub"
        assert translate_cwd(s, str(Path(td) / "elsewhere")) is None
        assert translate_cwd(s, "") is None
        argv, why = translate_argv(s, [ENV_PYTHON, "eval.py", "--seed", "0"])
        assert argv == [ENV_PYTHON, "eval.py", "--seed", "0"] and not why
        argv, why = translate_argv(s, [ENV_PYTHON, str(checkout / "eval.py")])
        assert argv == [ENV_PYTHON, f"{REPO_DIR}/eval.py"], argv
        argv, why = translate_argv(s, [r"C:\Python313\python.exe", "eval.py"])
        assert argv == [] and "names nothing in the sandbox" in why, why

        # --- the session record survives a round trip ---------------------------------
        s.measured = {"platform": "linux", "gpu_name": "NVIDIA A100-SXM4-80GB",
                      "python_version": "3.12.7"}
        s.env_python = ENV_PYTHON
        again = SandboxSession.from_json(json.loads(json.dumps(s.to_json())))
        assert again.sandbox_id == "sb-1" and again.spec == s.spec
        assert again.environment() == s.environment()
        assert "sandbox:sb-1" in s.environment() and "A100" in s.environment()
        _remember(s)
        assert session_path(checkout).is_file(), "the record lands beside the checkout"
        assert session_path(checkout).parent.name == "p", "never inside the tree git watches"
        assert session_for(checkout, revive=False) is not None
        found = list_sessions(Path(td))
        assert [x.sandbox_id for x in found] == ["sb-1"], found
        _forget(checkout)
        assert session_for(checkout, revive=False) is None

    # --- the staging script pins, and never stands on a branch ------------------------
    script = _fetch_script("https://example.invalid/x.git", "b" * 40)
    assert f"git fetch -q --depth 1 origin {'b' * 40}" in script
    assert "checkout -q --force FETCH_HEAD" in script
    assert "set -euo pipefail" in script
    for bad in ("origin/main", "origin HEAD", "--depth 1 origin main"):
        assert f"fetch -q --depth 1 {bad}" not in script
    venv = _venv_script(spec, ["requirements.txt", "sub/dev.txt"])
    assert f"{REPO_DIR}/requirements.txt" in venv and f"{REPO_DIR}/sub/dev.txt" in venv

    # --- refusals happen before anything is leased ------------------------------------
    shut = Config.load()
    shut.allow_sandbox = False
    ok, why = driver_status(shut)
    assert not ok and "SH_ALLOW_SANDBOX" in why

    def _refuses(url: str, commit: str, needle: str) -> None:
        open_cfg = Config.load()
        open_cfg.allow_sandbox = open_cfg.allow_network = open_cfg.allow_install = True
        try:
            open_session(open_cfg, local_path="nowhere", paper_id="p", url=url, commit=commit)
        except SandboxSetupError as e:
            # Any of the three cheap refusals is acceptable here — which one fires depends
            # on whether this machine has the client and a token, and the point of the
            # check is that NONE of them leases a sandbox first.
            assert (needle in str(e) or "not importable" in str(e)
                    or "credentials" in str(e)), e
        else:
            raise AssertionError(f"expected a refusal for url={url!r} commit={commit!r}")

    _refuses("", "c" * 40, "no repository URL")
    _refuses("https://x/y.git", "", "no full audited commit")
    _refuses("https://x/y.git", "abc", "no full audited commit")

    print("harness.sandbox self-check OK "
          f"— reservation: {spec.describe()}; driver: "
          f"{'usable' if driver_status(cfg)[0] else driver_status(cfg)[1][:60]}")
