"""Running a paper's own repository inside a container, and what that is worth.

`python -m harness.container` runs the self-check.

**The hole this fills.** `harness.isolation` requires CONTAINER or REMOTE_SESSION before a
paper's own repository may be executed, and until now this host offered neither: the local
backend declares VENV, which scopes imports and wall time and confines nothing else, and
the remote sandbox needs provider credentials nobody supplied. So `repo_exec` was not
merely gated off, it was unreachable BY CONSTRUCTION — `authorize()` refused with
`isolation_insufficient` no matter what any environment variable said. Every execution
this harness has ever performed on a real paper was therefore a harness-authored
diagnostic, which the provenance ceiling correctly forbids from concluding anything, and
"a failed reproduction from the authors' own code" existed only in fixtures.

This module does not lower that requirement. It supplies a backend that meets it.

**What a container is worth here, stated rather than implied.** A container gives the
repository its own filesystem, its own process namespace, its own user and its own
network stack, and it does not carry the operator's home directory, credentials, PATH or
SSH agent. That is what CONTAINER means in `harness.isolation`, and it is the property
that makes running a stranger's training script a defensible act rather than a reckless
one. It is NOT a security boundary against a determined adversary sharing a kernel, and
nothing here claims that. The claim is the one the isolation vocabulary actually makes:
the code runs somewhere that is not the operator's machine-as-they-use-it.

**Why the checkout is bind-mounted rather than copied.** The remote sandbox has to stage
the audited commit into another machine and therefore has to re-verify it there, because
a copy can differ from the original. A bind mount is not a copy: the bytes the container
reads are the same bytes on the same disk that `verify_commit` certified, so verifying
locally IS verifying the tree that runs. That is why `ContainerBackend.commit_tree`
returns None, and it is a real difference between the two backends rather than a shortcut.

**Nothing here falls back.** Every failure to reach the daemon, build the environment or
start the container is reported as itself. There is no branch that runs the experiment on
the host instead, for the same reason `sandbox.py` has none: a fallback that silently
downgrades isolation would make the isolation requirement advisory.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid

# The container path the paper's `runs/<pid>` directory is mounted at. One mount covers
# both halves the run needs — `repo/` (the audited checkout) and `env/` (the interpreter
# built for it) — so there is exactly one host path to translate and exactly one place a
# translation can be wrong.
MOUNT = "/work"

# Default base image. A slim Debian python, chosen because it is small, official, and
# carries a real glibc — a musl base silently breaks manylinux wheels, which is the kind
# of environment difference invariant 7 says must never be read as a scientific failure.
DEFAULT_IMAGE = "python:3.11-slim"
_PY_VERSION = re.compile(r"(3)\.(\d{1,2})")
# Python versions with an official slim image. A repository declaring something outside
# this set gets the default and a stated reason, rather than an image name that does not
# resolve and a pull failure two steps later.
_SUPPORTED_MINORS = (9, 10, 11, 12, 13)

_DOCKER_TIMEOUT_S = 60

# The declared, fixed baseline this harness installs for a GOVERNED RECONSTRUCTION whose
# checkout supplies no environment of its own (`RepoAcquisition.dependency_files` empty,
# so the venv `ContainerBackend.provision` built is a bare interpreter). These are never
# "the paper's dependencies" -- a reconstruction script is authored by THIS HARNESS'S OWN
# driver from the paper's stated method, never copied from the paper's repository, and it
# commonly needs the same standard ML runtime a Python interpreter itself already is: a
# fact about what this harness's generated code requires to exist at all, not a value the
# paper specifies. Fixed and auditable rather than inferred from a script's own imports --
# installing whatever a script happens to import would let the reconstruction's own text
# choose what gets installed, which is exactly the undeclared-package risk this stays
# clear of. Measured against the seven `sanchez24a-icml` reconstructions this exists for:
# every one imports `torch` and `transformers`; five of seven also import `datasets`; one
# imports `numpy`. All four cover every import across the whole set.
RECONSTRUCTION_BASELINE_REQUIREMENTS: tuple[str, ...] = (
    "torch", "numpy", "transformers", "datasets",
)


def docker_cli() -> str:
    """Path to the docker client, or "" when there is none on PATH."""
    return shutil.which("docker") or ""


def _run(argv: list[str], timeout: int = _DOCKER_TIMEOUT_S) -> tuple[int, str, str]:
    """Run a docker CLI command. Never raises; every ending comes back as data."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except FileNotFoundError:
        return 127, "", "docker client not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"docker command timed out after {timeout}s"
    except OSError as e:                                    # pragma: no cover - host specific
        return 1, "", f"could not run docker: {e}"
    return p.returncode, p.stdout or "", p.stderr or ""


def daemon_status() -> tuple[bool, str]:
    """Is a container runtime reachable RIGHT NOW, and if not, why not.

    Two failures an operator resolves differently, kept apart: no client installed at all,
    and a client whose daemon is not running. The second is the common one — Docker
    Desktop not started — and reporting it as "no container runtime" would send an
    operator to install software they already have.
    """
    if not docker_cli():
        return False, "no docker client is on PATH"
    rc, out, err = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if rc != 0:
        detail = (err or out).strip().splitlines()
        why = detail[-1][:200] if detail else "docker info failed"
        return False, f"the docker client is installed but its daemon is not reachable: {why}"
    return True, f"docker daemon {out.strip()} is reachable"


def gpu_available() -> tuple[bool, str]:
    """Can a container be given this host's GPU?

    Asked by actually starting one, because `--gpus all` is accepted by the CLI on hosts
    where it then fails at run time, and a reservation this harness cannot honour is worse
    than no GPU at all: `assess_capability` would pass a repository that needs CUDA and the
    crash would arrive later, where invariant 7 has to work to stop it convicting a paper.
    """
    rc, out, err = _run(["docker", "run", "--rm", "--gpus", "all", DEFAULT_IMAGE,
                         "sh", "-lc", "nvidia-smi -L || true"], timeout=300)
    if rc != 0:
        return False, (err or out).strip()[:200] or "the runtime refused --gpus all"
    name = (out or "").strip().splitlines()
    if name and name[0].startswith("GPU "):
        return True, name[0][:160]
    return False, "the container started but reported no GPU"


def inventory() -> dict:
    """What a container on this host actually gets. Measured, never declared.

    `docker info` reports the daemon's own view — the number of CPUs and the memory of the
    Linux VM on a Desktop install, which is what a process inside a container will see, and
    which on Windows is NOT the host's RAM. Reporting the host's figure here would hand
    `select_for` a machine that does not exist and turn a resource refusal into a crash.
    """
    rc, out, err = _run(["docker", "info", "--format", "{{json .}}"])
    if rc != 0:
        return {"ok": False, "detail": (err or out).strip()[:200]}
    try:
        info = json.loads(out)
    except (ValueError, TypeError):
        return {"ok": False, "detail": "docker info did not return JSON"}
    return {
        "ok": True,
        "cpus": int(info.get("NCPU") or 0) or None,
        "ram_bytes": int(info.get("MemTotal") or 0) or None,
        "server_version": str(info.get("ServerVersion") or ""),
        "operating_system": str(info.get("OperatingSystem") or ""),
        "detail": f"{info.get('OperatingSystem') or 'docker'} "
                  f"{info.get('ServerVersion') or ''}".strip(),
    }


def image_for(python_version: str = "") -> tuple[str, str]:
    """The base image for a repository declaring `python_version`, and why that one.

    A declaration outside the set of published slim images falls back to the default WITH
    a reason, rather than producing an image tag that does not resolve. The reason travels
    into `RepoAcquisition.reason`, so "we ran 3.11 because 3.7 has no official slim image"
    is a sentence a reader can check rather than a silent substitution.
    """
    m = _PY_VERSION.search(python_version or "")
    if not m:
        return DEFAULT_IMAGE, ""
    minor = int(m.group(2))
    if minor not in _SUPPORTED_MINORS:
        return DEFAULT_IMAGE, (f"the repository declares python 3.{minor}, which has no "
                               f"official slim image; {DEFAULT_IMAGE} was used instead")
    return f"python:3.{minor}-slim", ""


def install_reconstruction_baseline(
        py_in: str, host_mount: str, image: str, timeout: int) -> tuple[bool, str]:
    """Install `RECONSTRUCTION_BASELINE_REQUIREMENTS` into an already-built venv.

    Called ONLY from the governed-reconstruction fallback (`stages.probe
    .attempt_reimplementation_fallback`), never from author-code execution: an
    AUTHOR_CODE_EXECUTION spec runs the checkout's OWN declared dependencies exactly as
    `ContainerBackend.provision` installed them, and adding anything beyond that would be
    the undeclared-package risk this module's own docstring forbids. A reconstruction is
    different in kind -- the script came from `harness.reimplement_driver`, not from the
    paper's repository -- so this is this harness supplying its own generated code with a
    runtime, the same relationship it already has with the Python interpreter itself.

    Returns `(True, "")` on success and `(False, detail)` otherwise; never raises, so a
    caller can fold the failure into `TargetOutcome.reason` exactly like every other
    container step.
    """
    rc, out, err = _run(
        run_argv([py_in, "-m", "pip", "install", "--quiet",
                 *RECONSTRUCTION_BASELINE_REQUIREMENTS],
                host_mount=host_mount, image=image),
        timeout=timeout)
    if rc != 0:
        return False, (f"installing this harness's reconstruction baseline "
                       f"({', '.join(RECONSTRUCTION_BASELINE_REQUIREMENTS)}) failed: "
                       f"{(err or out).strip()[-300:]}")
    return True, ""


def to_container_path(host_path: str, host_mount: str) -> str:
    """Translate a path under the mounted directory into its path inside the container.

    Refuses — by returning "" — for anything not under the mount. A silent passthrough is
    how a host path ends up in an argv the container cannot resolve, and the resulting
    "file not found" would be recorded against the paper rather than against this
    translation. `sandbox.py` refuses the same case for the same reason.
    """
    hp = (host_path or "").replace("\\", "/").rstrip("/")
    hm = (host_mount or "").replace("\\", "/").rstrip("/")
    if not hp or not hm:
        return ""
    if hp.lower() == hm.lower():
        return MOUNT
    if not hp.lower().startswith(hm.lower() + "/"):
        return ""
    return f"{MOUNT}/{hp[len(hm) + 1:]}"


def container_name(pid: str = "", label: str = "") -> str:
    """A unique, greppable name for one run's container.

    Named rather than anonymous because an anonymous container cannot be killed by the
    code that started it: `subprocess.run(timeout=...)` kills the docker CLIENT, and the
    container it asked for goes on running. See `remove`.
    """
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{pid}-{label}".strip("-"))[:80].strip("-")
    return f"sh-{slug}-{uuid.uuid4().hex[:12]}" if slug else f"sh-{uuid.uuid4().hex[:12]}"


def remove(name: str) -> tuple[bool, str]:
    """Force-remove a container by name. Returns whether it is gone, and the detail.

    THE BUG THIS EXISTS FOR. `subprocess.run(argv, timeout=t)` kills `docker run`, the
    client. The container keeps running, holding the GPU and the CPU it reserved, while
    the harness records `timed_out=True` and moves on to the next seed. The next seed then
    contends with a process this harness believes it stopped, and `resources()` measures a
    machine that is already busy. A timeout that does not stop the work is not a timeout.

    Absent is success: a container that already exited under `--rm` is gone, which is the
    outcome asked for.
    """
    if not name:
        return False, "no container name to remove"
    rc, out, err = _run(["docker", "rm", "--force", name], timeout=30)
    if rc == 0:
        return True, f"removed container {name}"
    detail = (err or out).strip()
    if "No such container" in detail or "no such container" in detail:
        return True, f"container {name} had already exited"
    return False, f"could not remove container {name}: {detail[:200]}"


def run_argv(argv: list[str], *, host_mount: str, workdir: str = "", image: str = "",
             gpus: bool = False, env: dict | None = None,
             network: bool = True, name: str = "") -> list[str]:
    """The full `docker run` command line for one process. Pure; starts nothing.

    Separate from `execute` so that what a container run actually IS can be asserted by a
    test on a host with no Docker at all — including the three properties that matter and
    are easy to lose: `--rm` so a review never accumulates containers, a workdir inside
    the mount rather than on the host, and a `--name` so that a timeout has something to
    kill.
    """
    out = ["docker", "run", "--rm", "-v", f"{host_mount}:{MOUNT}"]
    if name:
        out += ["--name", name]
    if workdir:
        out += ["-w", workdir]
    if gpus:
        out += ["--gpus", "all"]
    if not network:
        # Used for the run itself once provisioning has finished. An experiment that
        # reaches the network mid-run is not reproducible, and a repository that needs to
        # download its dataset at run time should say so through its declared demand.
        out += ["--network", "none"]
    for k, v in sorted((env or {}).items()):
        out += ["-e", f"{k}={v}"]
    out.append(image or DEFAULT_IMAGE)
    return out + list(argv)


# --------------------------------------------------------------------------- #
def _self_check() -> None:
    import inspect

    # --- path translation refuses rather than passes through --------------------------
    mnt = r"C:\proj\acl\runs\acl"
    assert to_container_path(mnt + r"\repo", mnt) == "/work/repo"
    assert to_container_path(mnt + r"\env\bin\python", mnt) == "/work/env/bin/python"
    assert to_container_path(mnt, mnt) == MOUNT
    assert to_container_path("/work/repo", mnt) == "", "already-translated path is not under the mount"
    assert to_container_path(r"C:\elsewhere\repo", mnt) == "", (
        "a path outside the mount must be REFUSED, not relayed — a host path in an argv "
        "produces a not-found that gets recorded against the paper")
    assert to_container_path("", mnt) == "" and to_container_path(mnt, "") == ""
    # Forward slashes and a trailing separator are the same mount.
    assert to_container_path("C:/proj/acl/runs/acl/repo", "C:/proj/acl/runs/acl/") == "/work/repo"

    # --- the command line says what it does -------------------------------------------
    argv = run_argv(["python", "-c", "print(1)"], host_mount="/h", workdir="/work/repo")
    assert argv[:3] == ["docker", "run", "--rm"], "a review must not accumulate containers"
    assert "-v" in argv and f"/h:{MOUNT}" in argv
    assert argv[argv.index("-w") + 1] == "/work/repo"
    assert argv[-3:] == ["python", "-c", "print(1)"], "the argv is relayed, not rewritten"
    assert "--gpus" not in argv
    assert "--gpus" in run_argv([], host_mount="/h", gpus=True)
    assert "--network" not in run_argv([], host_mount="/h")
    assert run_argv([], host_mount="/h", network=False)[-3:-1] == ["--network", "none"]
    env_argv = run_argv([], host_mount="/h", env={"B": "2", "A": "1"})
    assert env_argv.index("A=1") < env_argv.index("B=2"), "deterministic ordering"

    # --- image selection is a declaration plus a stated reason -------------------------
    assert image_for("") == (DEFAULT_IMAGE, "")
    assert image_for("3.10") == ("python:3.10-slim", "")
    assert image_for("Python 3.12.4") == ("python:3.12-slim", "")
    img, why = image_for("3.7")
    assert img == DEFAULT_IMAGE and "no official slim image" in why, (
        "an unsupported declaration falls back WITH a reason, never silently")

    # --- signatures admit plain data only ---------------------------------------------
    for name, p in inspect.signature(image_for).parameters.items():
        assert str(p.annotation) == "str", name

    # --- the daemon probe never raises, whatever this host has -------------------------
    ok, why = daemon_status()
    assert isinstance(ok, bool) and isinstance(why, str) and why
    inv = inventory()
    assert isinstance(inv, dict) and "ok" in inv
    if not ok:
        assert inv["ok"] is False, "no daemon means no measurement, not a guessed one"
    print(f"harness.container self-check ok — daemon: {why}")


if __name__ == "__main__":
    _self_check()
