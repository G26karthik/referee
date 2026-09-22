"""A container backend meeting `harness.isolation`'s CONTAINER requirement for executing a
paper's own repository.

A container gives the repository its own filesystem, process namespace, user and network
stack, carrying none of the operator's home directory, credentials, PATH or SSH agent —
that is what CONTAINER means here. It is not a security boundary against a kernel-sharing
adversary; the claim is only that the code runs somewhere that is not the operator's own
machine.

The checkout is bind-mounted, not copied: the bytes the container reads are the bytes
`verify_commit` certified, so verifying locally verifies the tree that runs, and
`ContainerBackend.commit_tree` returns None. Nothing here falls back — every failure to
reach the daemon, build the environment or start the container is reported as itself,
never silently run on the host instead.

`python -m harness.container` runs the self-check.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid

# The container path the paper's `runs/<pid>` directory is mounted at: one mount covers
# both `repo/` (the audited checkout) and `env/` (the interpreter built for it).
MOUNT = "/work"

# Slim Debian python: small, official, real glibc (musl silently breaks manylinux wheels).
DEFAULT_IMAGE = "python:3.11-slim"
_PY_VERSION = re.compile(r"(3)\.(\d{1,2})")
# Python minors with an official slim image; anything else falls back with a reason.
_SUPPORTED_MINORS = (9, 10, 11, 12, 13)

_DOCKER_TIMEOUT_S = 60

# Fixed baseline installed for a GOVERNED RECONSTRUCTION whose checkout supplies no
# environment of its own. Never "the paper's dependencies" -- the reconstruction script is
# authored by this harness's own driver, not copied from the paper's repository, and
# commonly needs a standard ML runtime to exist at all. Fixed and auditable rather than
# inferred from the script's own imports, which would let generated text choose what gets
# installed.
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
    """Is a container runtime reachable RIGHT NOW, and if not, why not — distinguishing no
    client installed from a client whose daemon (commonly Docker Desktop) is not running."""
    if not docker_cli():
        return False, "no docker client is on PATH"
    rc, out, err = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if rc != 0:
        detail = (err or out).strip().splitlines()
        why = detail[-1][:200] if detail else "docker info failed"
        return False, f"the docker client is installed but its daemon is not reachable: {why}"
    return True, f"docker daemon {out.strip()} is reachable"


def gpu_available() -> tuple[bool, str]:
    """Can a container be given this host's GPU? Asked by actually starting one, since
    `--gpus all` is CLI-accepted on hosts where it then fails at run time."""
    rc, out, err = _run(["docker", "run", "--rm", "--gpus", "all", DEFAULT_IMAGE,
                         "sh", "-lc", "nvidia-smi -L || true"], timeout=300)
    if rc != 0:
        return False, (err or out).strip()[:200] or "the runtime refused --gpus all"
    name = (out or "").strip().splitlines()
    if name and name[0].startswith("GPU "):
        return True, name[0][:160]
    return False, "the container started but reported no GPU"


def inventory() -> dict:
    """What a container on this host actually gets, measured via `docker info` (the
    daemon's own view — on a Desktop install this is the Linux VM, not the host's RAM)."""
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
    """The base image for a repository declaring `python_version`, and why that one. A
    declaration outside the published slim images falls back to the default WITH a reason
    (which travels into `RepoAcquisition.reason`) rather than an image tag that won't
    resolve."""
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
    """Install `RECONSTRUCTION_BASELINE_REQUIREMENTS` into an already-built venv. Called
    ONLY from the governed-reconstruction fallback, never from author-code execution
    (which runs the checkout's OWN declared dependencies exactly as `provision` installed
    them). Never raises: returns `(True, "")` or `(False, detail)` for `TargetOutcome.reason`.
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
    Refuses — by returning "" — for anything not under the mount, rather than passing
    through a host path that the container cannot resolve."""
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
    """A unique, greppable name for one run's container. Named rather than anonymous
    because `subprocess.run(timeout=...)` kills the docker CLIENT, not the container it
    asked for — a name is what lets `remove` kill it too."""
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{pid}-{label}".strip("-"))[:80].strip("-")
    return f"sh-{slug}-{uuid.uuid4().hex[:12]}" if slug else f"sh-{uuid.uuid4().hex[:12]}"


def remove(name: str) -> tuple[bool, str]:
    """Force-remove a container by name. Returns whether it is gone, and the detail. Exists
    because `subprocess.run(argv, timeout=t)` kills only the docker CLIENT, leaving the
    container running and holding its GPU/CPU; a container already gone under `--rm`
    counts as success too."""
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
    """The full `docker run` command line for one process. Pure; starts nothing — testable
    without Docker. `--rm` so a review never accumulates containers; `--name` so a timeout
    has something to kill."""
    out = ["docker", "run", "--rm", "-v", f"{host_mount}:{MOUNT}"]
    if name:
        out += ["--name", name]
    if workdir:
        out += ["-w", workdir]
    if gpus:
        out += ["--gpus", "all"]
    if not network:
        # For the run itself, once provisioning has finished: reaching the network
        # mid-run would make the experiment not reproducible.
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
