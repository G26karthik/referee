"""The one execution gate (`authorize`) and the container runner.

Every process a review starts runs in a Linux container (`--rm`, named so a timeout can
kill it, `--network none` for the experiment itself) and leaves an ExecutionRecord:
argv, image, commit, script sha, timestamps, exit code, stdout, stderr (invariant 16).
Model-written scripts see the checkout READ-ONLY, so released data cannot be edited after
it was hashed. A draft run (`try`) is recorded with mode="try" and never counts.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import repo as repo_mod
from . import state
from .evidence import margin, relation

MOUNT = "/work"
DEFAULT_IMAGE = "python:3.11-slim"
# The evidence each check kind produces. Exact membership is the provenance ceiling
# (invariant 3): anything else — a draft `try`, a model's reasoning — settles nothing.
EVIDENCE = {"AUTHOR_CODE": "AUTHOR_CODE_REPRODUCTION", "RELEASED_DATA": "RELEASED_DATA_RECOMPUTATION",
            "RECONSTRUCTION": "PAPER_DERIVED_IMPLEMENTATION", "CERTIFICATE": "INSTANCE_CHECK",
            "ARITHMETIC": "PAPER_ARITHMETIC"}
# ponytail: a fixed, auditable baseline for model-written scripts (never chosen by the
# script's own imports); SH_SCRIPT_PACKAGES widens it per invocation.
SCRIPT_PACKAGES = ("numpy", "scipy", "pandas", "scikit-learn", "sympy")
_OUT_CAP = 1_000_000   # ponytail: 1 MB of stdout/stderr kept per process record


def admits(kind: str) -> bool:
    return kind in EVIDENCE


def docker_status() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "no docker client is on PATH"
    rc, out = _docker(["docker", "info", "--format", "{{.ServerVersion}}"], 60)
    return (True, f"docker {out.strip()}") if rc == 0 else (False, f"docker daemon not reachable: {out.strip()[-200:]}")


def _docker(argv: list[str], timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def gpu(cfg: state.Config) -> bool:
    """Can a container get this host's GPU? Asked once by starting one, then cached."""
    cache = cfg.projects / ".gpu.json"
    if (known := state.read_json(cache)) is not None:
        return bool(known)
    rc, out = _docker(["docker", "run", "--rm", "--gpus", "all", DEFAULT_IMAGE, "sh", "-c", "nvidia-smi -L"], 300)
    state.write_json(cache, rc == 0 and "GPU " in out)
    return rc == 0 and "GPU " in out


def image_for(checkout: Path | None) -> str:
    """The slim image of the Python the repo declares (3.9–3.13), else the default."""
    for name in (".python-version", "pyproject.toml", "setup.cfg", "setup.py", "environment.yml"):
        try:
            text = (checkout / name).read_text(encoding="utf-8", errors="replace") if checkout else ""
        except OSError:
            continue
        pat = r"^\s*3\.(9|1[0-3])\b" if name == ".python-version" else \
            r"python(?:_requires)?\W{0,6}(?:>=|==|~=|=)?\s*3\.(9|1[0-3])\b"
        if (m := re.search(pat, text, re.I | re.M)):
            return f"python:3.{m.group(1)}-slim"
    return DEFAULT_IMAGE


# --- dependency recovery (CLAUDE.md "Dependency recovery"): documented, isolated, recorded -
_CC = r"'?[\w./+-]*(?:gcc|cc|g\+\+|c\+\+|clang)'?"
_COMPILER = re.compile(rf"unable to execute {_CC}|command {_CC} failed|No such file or directory: {_CC}|"
                       rf"{_CC}:? (?:command )?not found|[Cc]\+?\+? compiler", re.I)
MAX_RECOVERIES = 2   # ponytail: two rebuilds per environment, each from scratch


def _declared(checkout: Path | None) -> str:
    for name in ("pyproject.toml", "setup.cfg", "setup.py"):
        try:
            text = (checkout / name).read_text(encoding="utf-8", errors="replace") if checkout else ""
        except OSError:
            continue
        if (m := re.search(r"(?:requires-python|python_requires)\s*=\s*[\"']([^\"']+)", text)):
            return m.group(1)
    return ""


def recover(stderr: str, image: str, checkout: Path | None) -> tuple[str, str] | None:
    """The next documented rebuild for an environment that failed, or None (then BLOCKED):
    a build that needed a compiler -> the full image of the same Python; every available
    release of a dependency needing a newer Python -> that Python, if the project's declared
    range admits it. Nothing else changes: no requirement is edited, added or dropped."""
    minor = int(m.group(1)) if (m := re.search(r"python:3\.(\d+)", image)) else 11
    if image.endswith("-slim") and _COMPILER.search(stderr or ""):
        full = image.removesuffix("-slim")
        return full, f"a build step needed a C/C++ compiler, which {image} lacks: rebuilt on {full} (same Python)"
    need = sorted({int(n) for n in re.findall(r"Requires-Python\s*>=\s*3\.(\d+)", stderr or "")} - set(range(minor + 1)))
    spec = _declared(checkout)
    for n in need[:1]:
        caps = re.findall(r"(<=?|==)\s*3\.(\d+)", spec)
        if n <= 13 and not any((op == "<" and int(v) <= n) or (op == "<=" and int(v) < n) or (op == "==" and int(v) != n)
                               for op, v in caps):
            new = image.replace(f"3.{minor}", f"3.{n}", 1)
            return new, (f"no release of a dependency supports Python 3.{minor} (it needs >=3.{n}): rebuilt on {new}, "
                         f"within the project's declared range {spec or '(none declared)'}")
    return None


def run(argv: list[str], *, mounts: list[tuple[Path, str, bool]], workdir: str, image: str,
        network: bool, timeout: int, env: dict | None = None, gpus: bool = False, mode: str,
        target: str, meta: dict | None = None) -> dict:
    """One process in a fresh container. Never raises: every ending is data."""
    name = f"referee-{uuid.uuid4().hex[:12]}"
    launch = ["docker", "run", "--rm", "--name", name, "-w", workdir]
    for host, inside, ro in mounts:
        launch += ["-v", f"{Path(host).resolve()}:{inside}{':ro' if ro else ''}"]
    launch += (["--network", "none"] if not network else []) + (["--gpus", "all"] if gpus else [])
    for k, v in sorted((env or {}).items()):
        launch += ["-e", f"{k}={v}"]
    launch += [image, *argv]
    rec = {"mode": mode, "target": target, "argv": argv, "launch_argv": launch, "cwd": workdir,
           "image": image, "network": network, **(meta or {}), "started_at": state.now()}
    t0 = time.time()
    try:
        p = subprocess.run(launch, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        rec.update(returncode=p.returncode, stdout=(p.stdout or "")[-_OUT_CAP:], stderr=(p.stderr or "")[-_OUT_CAP:],
                   timed_out=False)
    except subprocess.TimeoutExpired as e:
        _docker(["docker", "rm", "--force", name], 60)    # killing the client leaves the container
        rec.update(returncode=None, timed_out=True, error=f"timeout after {timeout}s",
                   stdout=_text(e.stdout)[-_OUT_CAP:], stderr=_text(e.stderr)[-_OUT_CAP:])
    except OSError as e:
        rec.update(returncode=None, timed_out=False, stdout="", stderr="", error=f"could not start: {e}")
    rec.update(ended_at=state.now(), seconds=round(time.time() - t0, 2))
    return rec


def _text(b) -> str:
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")


# --- detached steps: the Docker daemon owns every long process. A host process started by a
# tool call does not outlive that call, so an install or an evidence run is started detached
# under a deterministic name and collected by a later poll: idempotent (a second start adopts
# the running container instead of writing beside it) and immune to the poller dying.
def _secs(ts: str) -> float:
    import datetime
    m = re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", ts or "")
    return datetime.datetime.strptime(m.group(), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=datetime.timezone.utc).timestamp() if m else 0.0


def start(name: str, argv: list[str], *, mounts: list[tuple[Path, str, bool]], workdir: str, image: str,
          network: bool, env: dict | None = None, gpus: bool = False, mode: str, target: str,
          meta: dict | None = None) -> dict:
    """Start one step detached; returns its pending record (the caller persists it)."""
    launch = ["docker", "run", "-d", "--name", name, "--label", "referee=1", "--label", f"referee.mode={mode}",
              "-w", workdir]
    for host, inside, ro in mounts:
        launch += ["-v", f"{_src(host)}:{inside}{':ro' if ro else ''}"]
    launch += (["--network", "none"] if not network else []) + (["--gpus", "all"] if gpus else [])
    for k, v in sorted((env or {}).items()):
        launch += ["-e", f"{k}={v}"]
    launch += [image, *argv]
    rec = {"mode": mode, "target": target, "argv": argv, "launch_argv": launch, "cwd": workdir, "image": image,
           "network": network, **(meta or {}), "container": name, "started_at": state.now()}
    rc, out = _docker(launch, 480)          # ponytail: includes an image pull; 8 min fits one tool call
    if rc != 0 and _docker(["docker", "inspect", name], 60)[0] != 0:
        rec.update(returncode=None, timed_out=False, stdout="", stderr="", ended_at=state.now(), seconds=0,
                   error=f"could not start: {out.strip()[-300:]}")
    return rec


def collect(rec: dict, timeout: int) -> dict | None:
    """The finished record of a started step, or None while it runs (or while the daemon is
    away). A step past `timeout` is killed and recorded as timed out."""
    if "returncode" in rec:
        return rec
    name = rec["container"]
    rc, out = _docker(["docker", "inspect", "-f", "{{json .State}}", name], 60)
    if rc != 0:                           # only "no such container" is a vanished step; any other
        if "no such" not in out.lower():  # error (daemon away, a 500 under load) is asked again later
            return None
        return {**rec, "returncode": None, "timed_out": False, "stdout": "", "stderr": "", "ended_at": state.now(),
                "seconds": 0, "error": "the container disappeared before it was collected"}
    st = json.loads(out)
    if st.get("Running"):
        if time.time() - _secs(st.get("StartedAt", "")) > timeout:
            _docker(["docker", "kill", name], 60)
        return None
    try:
        p = subprocess.run(["docker", "logs", name], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        so, se = p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as e:
        so, se = "", f"logs unavailable: {e}"
    t0, t1 = _secs(st.get("StartedAt", "")), _secs(st.get("FinishedAt", ""))
    timed_out = t1 - t0 >= timeout
    _docker(["docker", "rm", "-f", name], 60)
    return {**rec, "returncode": None if timed_out else st.get("ExitCode"), "timed_out": timed_out,
            "stdout": so[-_OUT_CAP:], "stderr": se[-_OUT_CAP:], "ended_at": st.get("FinishedAt", state.now())[:19] + "Z",
            "seconds": round(max(0.0, t1 - t0), 2),
            **({"error": f"timeout after {timeout}s"} if timed_out else {"error": "out of memory"} if st.get("OOMKilled") else {})}


def _vanished(rec: dict) -> bool:
    """The step's container was removed before it was collected: an infrastructure event."""
    return (rec.get("error") or "").startswith("the container disappeared")


def _cname(*parts) -> str:
    return "referee-" + state.sha256("|".join(str(x) for x in parts))[:16]


def volume(env_dir: Path) -> str:
    """The Docker named volume holding the venv whose markers live in `env_dir`: a venv is
    thousands of small files, which a host bind mount (worse, a synced folder) writes slowly."""
    return _cname("env", Path(env_dir).resolve())


def _src(host) -> str:
    return host if isinstance(host, str) and not re.search(r"[\\/:]", host) else str(Path(host).resolve())


# --- environment failure is not scientific failure (invariant 5) ------------------------
_SETUP = ("modulenotfounderror", "importerror", "cannot import name", "unrecognized arguments",
          "the following arguments are required", "invalid choice", "no such file or directory",
          "can't open file", "syntaxerror", "command not found", "not found: ")
_INFRA = ("out of memory", "cuda out of memory", "cuda error", "cublas", "cudnn", "nccl",
          "no space left on device", "disk quota exceeded", "segmentation fault", "core dumped",
          "bus error", "driver/library version mismatch", "cuda driver version is insufficient",
          "memoryerror", "unable to allocate", "cannot allocate memory", "bad_alloc", "killed",
          "404 client error", "401 client error", "403 client error", "gated repo",
          "repository not found", "connection refused", "could not resolve host",
          "temporary failure in name resolution", "name or service not known",
          "max retries exceeded", "network is unreachable", "connectionerror", "sslerror",
          "couldn't connect", "could not connect", "found no nvidia driver", "not compiled with cuda",
          "no cuda gpus", "api_key", "api key", "wandb", "login", "token", "permission denied",
          "do not match the hashes", "hash mismatch", "connection reset", "read timed out",
          "incompleteread", "failed to download", "error sending request")   # a corrupt or cut download
_SIGNALS = {-9, -11, -6, 137, 139, 134, 136, 132, 135, 255}   # 255: killed by a daemon restart


def classify(rec: dict) -> dict:
    """What a process showed about whether the experiment itself ever started, and whether
    a crash happened in the authors' own code (the last traceback frame under /work/repo,
    not in a library) — the only crash that can count against the paper."""
    err = rec.get("stderr") or ""
    low = err.lower() + (rec.get("error") or "").lower()
    infra = next((s for s in _INFRA if s in low), "") or (
        f"killed by the OS (exit {rec['returncode']})" if rec.get("returncode") in _SIGNALS else "")
    setup = next((s for s in _SETUP if s in low), "")
    out_lines = len((rec.get("stdout") or "").splitlines())
    # The burden is on showing it started: an unproven start accuses nobody.
    reached = (not setup and not rec.get("timed_out") and not rec.get("error")
               and ("REFEREE_RESULT" in (rec.get("stdout") or "") or (rec.get("seconds", 0) >= 30 and out_lines >= 5)))
    frames = re.findall(r'File "([^"]+)", line \d+', err.rsplit("Traceback (most recent call last)", 1)[-1])
    own = frames[-1] if frames and frames[-1].startswith(f"{MOUNT}/repo/") and "site-packages" not in frames[-1] else ""
    return {"infra_error": infra, "setup_error": setup, "reached": reached, "own_code_crash": own,
            "failed": rec.get("returncode") != 0 or bool(rec.get("timed_out") or rec.get("error"))}


# --- metric parsing: a value bound by name, never by position (invariant 16) ------------
def _results(stdout: str) -> list[dict]:
    """The numeric fields of each `REFEREE_RESULT {json}` line (the script contract)."""
    out = []
    for line in (stdout or "").splitlines():
        if line.startswith("REFEREE_RESULT "):
            try:
                d = json.loads(line[len("REFEREE_RESULT "):])
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append({k: float(v) for k, v in d.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
    return out


def result_values(stdout: str, key: str) -> list[float]:
    """Values of `key`, by name, from the result lines."""
    return [d[key] for d in _results(stdout) if key in d]


def cert_rows(stdout: str) -> list[dict]:
    """A certificate's instances: `violated`, `premises` (1/0 when the script evaluated every
    premise of the exact claim; None when it did not say), `literal` (the text as printed)."""
    rows = []
    for line in (stdout or "").splitlines():
        if not line.startswith("REFEREE_RESULT "):
            continue
        try:
            d = json.loads(line[len("REFEREE_RESULT "):])
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("violated") in (0, 1):
            rows.append({"violated": int(d["violated"]),
                         "premises": int(d["premises_hold"]) if d.get("premises_hold") in (0, 1) else None,
                         "literal": d.get("literal") if d.get("literal") in ("holds", "fails", "undefined",
                                                                             "premise_not_met") else None})
    return rows


def relation_margins(stdout: str, rel: str) -> list[float]:
    """One paired margin per result line that carries every output the relation names."""
    names = relation(rel)[3]
    return [margin(rel, d) for d in _results(stdout) if all(n in d for n in names)]


def parse_metric(stdout: str, key: str) -> tuple[float | None, str]:
    """An author program's metric by its exact name: a JSON field (top level, or inside
    results/metrics/summary/...), else a `key: value` line. Several different values at the
    same tier is refused — taking the last would be positional coincidence."""
    if not key:
        return None, "no metric key was bound"
    k, tiers = key.lower(), {"json": [], "text": []}
    for line in (stdout or "").splitlines():
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = {str(a).lower(): b for a, b in json.loads(s).items()}
            except (ValueError, AttributeError):
                continue
            for d in [obj] + [{str(a).lower(): b for a, b in v.items()} for v in obj.values() if isinstance(v, dict)]:
                if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool):
                    tiers["json"].append(float(d[k]))
        else:
            for m in re.finditer(rf"(?<![\w.]){re.escape(k)}\s*[:=]\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)", s, re.I):
                tiers["text"].append(float(m.group(1)))
    for tier in ("json", "text"):
        vals = sorted(set(tiers[tier]))
        if len(vals) > 1:
            return None, f"{len(vals)} different values for '{key}' at one tier ({tier}): {vals[:5]}"
        if vals:
            return vals[0], f"'{key}' read from {tier} output"
    return None, f"the output never reports '{key}'"


# --- the gate ---------------------------------------------------------------------------
def authorize(cfg: state.Config, check: dict, commit_ok: tuple[bool, str] = (False, "not checked")) -> tuple[bool, str]:
    """May this check's process start? The only place execution is permitted (invariant 4).
    Author code: gate, docker, attributed repo, clean pinned commit, two-key identity.
    Model-written script: gate, docker, an independent verifier's approval of THIS sha."""
    kind = check.get("kind", "")
    if not admits(kind) or kind == "ARITHMETIC":
        return False, f"'{kind}' is not an executable kind"
    if kind == "AUTHOR_CODE" and not cfg.allow_repo_exec:
        return False, "SH_ALLOW_REPO_EXEC is not set: running the authors' code is an explicit opt-in"
    if kind != "AUTHOR_CODE" and not cfg.allow_script_exec:
        return False, "SH_ALLOW_SCRIPT_EXEC is not set: running a model-written script is an explicit opt-in"
    ok, why = docker_status()
    if not ok:
        return False, f"no container runtime: {why}"
    if kind == "AUTHOR_CODE":
        if not check.get("repo_attributed"):
            return False, "the repository is not established as the authors' own"
        if not commit_ok[0]:
            return False, f"the checkout is not the pinned commit: {commit_ok[1]}"
        if not check.get("identity", {}).get("established"):
            return False, f"experiment identity is not established: {check.get('identity', {}).get('reason', 'no binding')}"
        return True, "authors' code at a clean pinned commit, identity established by two independent keys"
    appr = check.get("approval") or {}
    if not appr.get("approved") or appr.get("script_sha256") != check.get("script_sha256"):
        return False, "no independent verifier approved this exact script"
    return True, "an independent verifier approved this exact script"


UV_RUN = {"UV_PROJECT_ENVIRONMENT": "/env", "UV_NO_SYNC": "1", "UV_FROZEN": "1", "UV_OFFLINE": "1",
          "UV_PYTHON_DOWNLOADS": "never", "UV_CACHE_DIR": "/tmp/uv-cache"}   # `uv run` uses /env, never syncs


_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?((==|>=|<=|~=|!=|<|>)[A-Za-z0-9.*+!-]+)?$")


def packages(script: str) -> tuple[str, ...]:
    """Extra pip packages a script declares on a `# REFEREE_PACKAGES: a b==1.2` line. The line
    is part of the approved script (its sha covers it); only plain requirement specs pass."""
    m = re.search(r"(?m)^#\s*REFEREE_PACKAGES:\s*(.*)$", script or "")
    specs = tuple(sorted(set(re.split(r"[\s,]+", m.group(1).strip())) - {""})) if m else ()
    bad = [x for x in specs if not _PKG.match(x)]
    if bad or len(specs) > 10:                                    # ponytail: 10 extra packages
        raise ValueError(f"REFEREE_PACKAGES lists at most 10 plain pip requirements (no URLs, paths or "
                         f"options); refused: {bad[:3] or len(specs)}")
    return specs


def with_packages(cfg: state.Config, root: Path, env_dir: Path, envinfo: dict | None,
                  script: str) -> tuple[Path, dict | None]:
    """A derived environment for a script that declares packages: a copy of the built one plus
    those packages (recorded like any build); otherwise the environment itself."""
    extra = packages(script)
    if not extra or not envinfo or not envinfo.get("ok"):
        return env_dir, envinfo
    d = root / "env-extra" / state.sha256(f"{volume(env_dir)}|{' '.join(extra)}")[:12]
    return d, ensure_env(cfg, root, d, envinfo.get("image", DEFAULT_IMAGE), None, extra, base=volume(env_dir))


def _env_steps(checkout: Path | None, packages: tuple[str, ...], base: str = "") -> tuple[str, list[str], bool]:
    uv = bool(checkout and not packages and (checkout / "uv.lock").is_file())
    reqs = sorted(p.name for p in checkout.glob("requirements*.txt")) if checkout else []
    if base:
        return f"a copy of {base} plus {' '.join(packages)}", [
            "cp -a /base/. /env/", "/env/bin/python -m pip install --quiet " + " ".join(packages),
            "echo REFEREE_FREEZE", "/env/bin/python -m pip freeze"], False
    if packages:
        builder, add = "baseline packages", ["/env/bin/python -m pip install --quiet " + " ".join(packages)]
    elif uv:
        builder = "uv sync --frozen (the authors' lockfile)"
        add = ["/env/bin/python -m pip install --quiet uv", "cp -r /repo /tmp/src && cd /tmp/src && "
               "UV_PROJECT_ENVIRONMENT=/env UV_PYTHON=/env/bin/python UV_PYTHON_DOWNLOADS=never "
               "/env/bin/uv sync --frozen --inexact --no-editable"]
    elif reqs:
        builder, add = f"pip -r {reqs[0]}", [f"/env/bin/python -m pip install -r /repo/{reqs[0]}"]
    elif checkout and ((checkout / "pyproject.toml").exists() or (checkout / "setup.py").exists()):
        builder, add = "pip install of the package", ["cp -r /repo /tmp/src && /env/bin/python -m pip install /tmp/src"]
    else:
        builder, add = "no declared dependencies", []
    return builder, ["python -m venv /env", "/env/bin/python -m pip install --quiet --upgrade pip", *add,
                     "echo REFEREE_FREEZE", "/env/bin/python -m pip freeze"], uv


def ensure_env(cfg: state.Config, root: Path, env_dir: Path, image: str, checkout: Path | None,
               packages: tuple[str, ...] = (), base: str = "") -> dict | None:
    """A venv built inside a container (network on), once, from what the checkout declares:
    its uv lockfile (path sources included), else requirements*.txt, else its package.
    Non-blocking: returns the marker (ok, detail, image, builder, uv, recovery) once the
    build ended, None while it runs. A failed attempt is rebuilt from an empty dir only along
    `recover` (or unchanged after an infrastructure failure); every attempt is an
    ExecutionRecord."""
    marker, build = env_dir / "referee-env.json", env_dir.parent / f".{env_dir.name}.build.json"
    if (m := state.read_json(marker)) and "ok" in m:
        return m
    if not (cfg.allow_install and cfg.allow_network):
        return {"ok": False, "detail": "the install or network gate is shut, so no environment was built", "image": image}
    if not docker_status()[0]:
        return None                       # the daemon is away: wait for it, never conclude
    with state.lock(env_dir.parent / f".{env_dir.name}.lock"):
        if (m := state.read_json(marker)) and "ok" in m:
            return m
        builder, steps, uv = _env_steps(checkout, packages, base)
        b = state.read_json(build) or {"image": image, "attempt": 0, "recovery": [], "rec": None}
        if b["rec"]:
            done = collect(b["rec"], cfg.install_timeout_s)
            if done is None:
                return None
            state.append_jsonl(root / "execution.jsonl", done)
            b["rec"] = None
            err = (done.get("stderr") or "") + (done.get("error") or "")
            fix = recover(err, b["image"], checkout) if done.get("returncode") != 0 else None
            if not fix and done.get("returncode") != 0 and (
                    infra := classify(done)["infra_error"] or ("the container vanished" if _vanished(done) else "")):
                fix = (b["image"], f"an infrastructure failure ('{infra}'): rebuilt unchanged")
            if done.get("returncode") == 0 or not fix or b["attempt"] > MAX_RECOVERIES:
                ok = done.get("returncode") == 0
                freeze = (done.get("stdout") or "").split("REFEREE_FREEZE", 1)[-1].strip()   # kept in execution.jsonl
                m = {"ok": ok, "image": b["image"], "builder": builder, "uv": uv, "recovery": b["recovery"],
                     "volume": volume(env_dir),
                     "detail": f"built {b['image']} venv by {builder}; freeze sha256 {state.sha256(freeze)[:12]}"
                     if ok else f"environment build failed: {err[-300:]}"}
                state.write_json(marker, m)
                build.unlink(missing_ok=True)
                return m
            b["recovery"].append({"failure": err[-300:], "action": fix[1], "image": fix[0]})
            b["image"] = fix[0]
        b["attempt"] += 1
        _docker(["docker", "volume", "rm", "-f", volume(env_dir)], 120)   # isolated: every attempt starts empty
        env_dir.mkdir(parents=True, exist_ok=True)
        b["rec"] = start(_cname(env_dir.resolve(), b["attempt"]), ["sh", "-c", " && ".join(steps)],
                         mounts=[(volume(env_dir), "/env", False)] + ([(checkout, "/repo", True)] if checkout else [])
                         + ([(base, "/base", True)] if base else []),
                         workdir="/", image=b["image"], network=True, mode="install", target=env_dir.name,
                         meta={"builder": builder, "recovery": list(b["recovery"])})
        state.write_json(build, b)
        return None


def author_env(cfg: state.Config, root: Path) -> dict | None:
    """The authors' environment (root/env): used by AUTHOR_CODE and by reconstructions that
    drive the authors' code; started as soon as a plan needs it."""
    return ensure_env(cfg, root, root / "env", image_for(root / "repo"), root / "repo")


def script_env(cfg: state.Config, root: Path, kind: str, attributed: bool) -> tuple[Path, dict | None]:
    """A RECONSTRUCTION runs where the authors' code runs (their environment) when that
    builds; every other script, or a failed build, runs on the fixed baseline."""
    base = cfg.projects / ".script-env"
    if kind == "RECONSTRUCTION" and attributed and (root / "repo" / ".git").is_dir():
        env = author_env(cfg, root)
        if env is None or (env.get("ok") and env.get("builder") != "no declared dependencies"):
            return root / "env", env      # a checkout that declares nothing has no environment of its own
        b = ensure_env(cfg, root, base, DEFAULT_IMAGE, None, SCRIPT_PACKAGES)
        return base, b and {**b, "detail": f"{b['detail']} (the authors' environment did not build: {env['detail'][-200:]})"}
    return base, ensure_env(cfg, root, base, DEFAULT_IMAGE, None, SCRIPT_PACKAGES)


def poll(cfg: state.Config, pid: str, cid: str) -> bool:
    """Advance one started check by at most one container step; True while it still runs.
    State lives in checks/<id>/exec.json, so any caller may poll and none needs to survive."""
    root = state.pdir(cfg, pid)
    cdir, checkout = root / "checks" / cid, root / "repo"
    if (cdir / "outcome.json").exists() or not (cdir / "exec.json").exists():
        return False
    check, st = state.read_json(cdir / "check.json"), state.read_json(cdir / "exec.json")
    kind, has_repo = check["kind"], (checkout / ".git").is_dir()
    src = state.read_json(root / "source.json", {})
    if "env" not in st:                                      # first poll: the gate, then the environment
        commit_ok = repo_mod.verify_commit(checkout, src.get("commit", "")) if kind == "AUTHOR_CODE" else (True, "")
        ok, why = authorize(cfg, check, commit_ok)
        if not ok:
            if why.startswith("no container runtime"):
                return True                                  # wait for the daemon; never a verdict
            return _finish(cfg, root, check, {**st, "authorized": False, "why": why})
        env_dir, envinfo = ((root / "env", author_env(cfg, root)) if kind == "AUTHOR_CODE" else
                            with_packages(cfg, root, *script_env(cfg, root, kind, bool(check.get("repo_attributed"))),
                                          (cdir / "script.py").read_text(encoding="utf-8")))
        if envinfo is None:
            return True
        st.update(env=envinfo, env_dir=str(env_dir), why=why, seed=0, values=[], cert=[], rec=None,
                  stage="prepare" if check.get("prepare") else "run", done_seeds=[])
        for row in _checkpoints(cdir, check):          # completed seeds of this exact script/command are kept
            st["values"] += row["values"]
            st["cert"] += row.get("cert") or []
            st["done_seeds"].append(row["seed"])
            st.setdefault("pilot_s", row.get("seconds") or 0)
        st["seed"] = len(st["done_seeds"])
        log = root / "execution.jsonl"          # this exact script was killed for memory before: one seed at a time
        if check.get("script_sha256") and log.exists() and any(
                r.get("script_sha256") == check["script_sha256"] and r.get("returncode") == 137
                for r in map(json.loads, log.read_text(encoding="utf-8").splitlines())):
            st["width"] = 1
        if not envinfo["ok"]:
            return _finish(cfg, root, check, {**st, "authorized": False, "why": envinfo["detail"]})
        if kind == "AUTHOR_CODE":   # the authors' code writes into its own copy (no .git), never the pinned checkout
            shutil.rmtree(cdir / "work", ignore_errors=True)
            shutil.copytree(checkout, cdir / "work", ignore=shutil.ignore_patterns(".git"))
        else:                       # the script sees only its approved copy, read-only
            (cdir / "run").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cdir / "script.py", cdir / "run" / "script.py")
            if state.sha256((cdir / "run" / "script.py").read_bytes()) != check.get("script_sha256"):
                return _finish(cfg, root, check, {**st, "authorized": False, "why": "the script on disk is not the approved script"})
        state.write_json(cdir / "exec.json", st)
    env_dir, image = Path(st["env_dir"]), st["env"].get("image", DEFAULT_IMAGE)
    if kind == "AUTHOR_CODE":
        mounts, workdir = [(cdir / "work", f"{MOUNT}/repo", False), (volume(env_dir), "/env", False)], f"{MOUNT}/repo"
        env = {"PATH": "/env/bin:/usr/local/bin:/usr/bin:/bin", "VIRTUAL_ENV": "/env", **(UV_RUN if st["env"].get("uv") else {})}
    else:
        mounts = [(cdir / "run", f"{MOUNT}/check", True), (volume(env_dir), "/env", True)] + (
            [(checkout, f"{MOUNT}/repo", True)] if has_repo else [])
        workdir, env = (f"{MOUNT}/repo" if has_repo else f"{MOUNT}/check"), {}
    # Steps in flight, by seed ("-1" is the prepare step). Independent seeds of a script run
    # SH_PARALLEL at a time (private /tmp, read-only mounts); author code one at a time (it
    # writes into one work copy, and may hold the GPU). Parallel is scheduling, never a downscale.
    fly = st.setdefault("fly", {})
    if st.get("rec"):                                        # one in-flight step, from before parallel seeds
        fly["-1" if st["stage"] == "prepare" else str(st["seed"])] = st["rec"]
        st.setdefault("next", st["seed"] + (st["stage"] == "run"))
    st["rec"] = None
    st.setdefault("next", st.get("seed", 0))
    runs, width = int(check.get("runs") or 1), 1 if kind == "AUTHOR_CODE" else max(1, cfg.parallel)
    timeout = cfg.install_timeout_s if st["stage"] == "prepare" else cfg.run_timeout_s
    for key, rec in sorted(fly.items(), key=lambda kv: int(kv[0])):
        done = collect(rec, timeout)
        if done is None:
            continue
        state.append_jsonl(root / "execution.jsonl", done)
        del fly[key]
        act, text = resource_action(done, st, key, timeout)
        if act == "retry":
            st.setdefault("redo", []).append(int(key))
            continue
        if act == "blocker":
            return _finish(cfg, root, check, {**_cancel(root, st), "blocker": text})
        per = st.setdefault("restarts_by_seed", {})
        if (_vanished(done) or (done["mode"] == "evidence" and classify(done)["infra_error"])) and per.get(key, 0) < 3:
            per[key] = per.get(key, 0) + 1
            st["restarts"] = st.get("restarts", 0) + 1   # ponytail: 3 restarts per seed; a vanished or daemon-killed
            st.setdefault("redo", []).append(int(key))   # step is infrastructure, never a result: run that seed again
            continue
        if st["stage"] == "prepare":
            if done.get("returncode") != 0:
                return _finish(cfg, root, check, {**st, "authorized": False, "why": "prepare step: " + (
                    done.get("stderr") or done.get("error") or "")[-300:]})
            st["stage"] = "run"
            continue
        st["records"] = st.get("records", 0) + 1
        ev = classify(done)
        rel = (check.get("target") or {}).get("relation", "")
        if kind == "AUTHOR_CODE":
            v, note = parse_metric(done["stdout"], check.get("metric", ""))
            vs = [v] if v is not None and not (ev["failed"] and "text" in note) else []
        elif kind == "CERTIFICATE":
            rows = cert_rows(done["stdout"])
            vs = [r["violated"] for r in rows]
            st.setdefault("cert", []).extend(rows)
        else:   # bound by name: the relation's outputs or the generator's compared output
            vs = (relation_margins(done["stdout"], rel) if rel else result_values(done["stdout"], check.get("metric", "")))
        if ev["failed"]:
            return _finish(cfg, root, check, {**_cancel(root, st), "ev": ev, "failure": (
                done.get("error") or (done.get("stderr") or "")[-400:] or f"exit {done.get('returncode')}").strip()})
        if not vs:                # a clean exit with no bound metric establishes nothing
            return _finish(cfg, root, check, {**_cancel(root, st), "values": []})
        st["values"] += vs
        st["seed"] += 1
        st.setdefault("done_seeds", []).append(int(key))
        state.append_jsonl(cdir / "seeds.jsonl", {"key": _ckpt_key(check), "seed": int(key), "values": vs,
                                                   "cert": rows if kind == "CERTIFICATE" else [],
                                                   "seconds": done.get("seconds")})
        if "pilot_s" not in st:                           # the first completed run is the pilot
            st["pilot_s"] = done.get("seconds") or 0
            w = 1 if kind == "AUTHOR_CODE" else max(1, min(cfg.parallel, st.get("width", cfg.parallel)))
            hours = st["pilot_s"] * (runs - st["seed"]) / w / 3600
            if hours > cfg.check_budget_s / 3600:
                return _finish(cfg, root, check, {**_cancel(root, st), "blocker": (
                    f"the {runs} runs need about {hours:.1f} h more at the pilot's {st['pilot_s'] / 60:.1f} min per "
                    f"run ({w} at a time); this host's per-check budget is {cfg.check_budget_s / 3600:.1f} h "
                    "(SH_CHECK_BUDGET_S). The run count is not reduced: the completed run(s) are recorded as a "
                    "pilot and decide nothing.")})
    _sample_memory(st, fly)
    if st["stage"] == "run" and st["seed"] >= runs and not fly:
        return _finish(cfg, root, check, st)
    todo = []
    if st["stage"] == "prepare" and not fly:
        todo = [-1]
    # The host is the limit, not the check: at most SH_PARALLEL evidence runs at once across every review.
    rc, out = _docker(["docker", "ps", "-q", "--filter", "label=referee=1"], 60)
    rc2, builds = _docker(["docker", "ps", "-q", "--filter", "label=referee.mode=install"], 60)
    free = cfg.parallel - (len(out.split()) - len(builds.split())) if rc == rc2 == 0 else 0
    # ...and shared fairly: with several checks running, each holds at most an equal share of the slots.
    active = [e for e in cfg.projects.glob("*/checks/*/exec.json") if not (e.parent / "outcome.json").exists()]
    width = min(width, max(1, cfg.parallel // max(1, len(active))), st.get("width", width))
    if "pilot_s" not in st:
        width = 1                                          # a bounded pilot: one run first, timed, then the rest
    done_set = set(st.get("done_seeds", []))
    while st["next"] in done_set:
        st["next"] += 1
    while st["stage"] == "run" and len(fly) + len(todo) < width and len(todo) < free and (
            st.get("redo") or st["next"] < runs):
        if st.get("redo"):
            todo.append(st["redo"].pop(0))
        else:
            todo.append(st["next"])
            st["next"] += 1
            while st["next"] in done_set:
                st["next"] += 1
    for seed in todo:
        if seed < 0:
            argv, network, mode = ["sh", "-c", check["prepare"]], True, "prepare"
        elif kind == "AUTHOR_CODE":
            cmd, st["seeded"] = seeded_command(check["command"], check.get("seed_flag", ""), seed)
            argv, network, mode = ["sh", "-c", cmd], False, "evidence"
        else:
            argv, network, mode = ["/env/bin/python", f"{MOUNT}/check/script.py", "--seed", str(seed)], False, "evidence"
        meta = {"commit": src.get("commit", "") if has_repo else "", "seed": seed,
                "script_sha256": check.get("script_sha256", "")} if mode == "evidence" else {}
        ckpt = [(ckpt_volume(cdir, seed), f"{MOUNT}/ckpt", False)] if kind != "AUTHOR_CODE" and seed >= 0 else []
        fly[str(seed)] = start(_cname(cdir.resolve(), st.get("token", ""), f"{mode}{seed}"), argv, mounts=mounts + ckpt,
                               workdir=workdir, image=image, network=network, env=env,
                               gpus=kind in ("AUTHOR_CODE", "RECONSTRUCTION") and gpu(cfg),   # an experiment may use it
                               mode=mode, target=cid, meta=meta)
    state.write_json(cdir / "exec.json", st)
    return True


def resource_action(done: dict, st: dict, key: str, timeout: int) -> tuple[str, str]:
    """What a run that hit a resource limit leads to: ("retry", "") once alone after an
    out-of-memory kill that may have shared memory; ("blocker", why) when the same failure
    would repeat (killed out of memory alone, or past the per-run limit); ("", "") otherwise."""
    if done.get("mode") != "evidence":
        return "", ""
    if done.get("error") == "out of memory":
        if not st.get("oom_retry"):
            st.update(width=1, oom_retry=True)
            return "retry", ""
        return "blocker", (f"a single run was killed out of memory after {done.get('seconds', 0):.0f}s even when "
                           f"running alone (peak observed {st.get('peak_mb', {}).get(key, '?')} MB; this host's "
                           f"Docker VM has {_vm_mb()} MB). The same run fails the same way, so it is not repeated; "
                           "it needs a host with more memory.")
    if done.get("timed_out"):
        return "blocker", (f"a single run exceeded this host's per-run limit of {timeout}s (SH_RUN_TIMEOUT_S); the "
                           "protocol is not shortened, so it is not repeated.")
    return "", ""


def _ckpt_key(check: dict) -> str:
    return check.get("script_sha256") or f"{check.get('command', '')}|{check.get('seed_flag', '')}"


def _checkpoints(cdir: Path, check: dict) -> list[dict]:
    """Seeds of this exact approved script (or documented command) that already completed."""
    f, key, seen, out = cdir / "seeds.jsonl", _ckpt_key(check), set(), []
    for row in map(json.loads, f.read_text(encoding="utf-8").splitlines() if f.exists() else []):
        if row.get("key") == key and row["seed"] not in seen and row["seed"] < int(check.get("runs") or 1):
            seen.add(row["seed"])
            out.append(row)
    return out


def ckpt_volume(cdir: Path, seed: int) -> str:
    """A per-seed scratch volume mounted at /work/ckpt: a long run may save progress there and
    resume from it after an infrastructure restart (it never holds evidence)."""
    return _cname("ckpt", Path(cdir).resolve(), seed)


def _vm_mb() -> str:
    rc, out = _docker(["docker", "info", "--format", "{{.MemTotal}}"], 30)
    return str(int(out.strip()) // 2 ** 20) if rc == 0 and out.strip().isdigit() else "?"


def _sample_memory(st: dict, fly: dict) -> None:
    """The peak memory seen for each run in flight (sampled at each poll)."""
    if not fly:
        return
    rc, out = _docker(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}",
                       *[r["container"] for r in fly.values()]], 30)
    units = {"b": 1 / 2 ** 20, "kib": 1 / 1024, "kb": 1 / 1024, "mib": 1, "mb": 1, "gib": 1024, "gb": 1024}
    names = {r["container"]: k for k, r in fly.items()}
    for line in out.splitlines() if rc == 0 else []:
        m = re.match(r"(\S+)\s+([\d.]+)\s*([A-Za-z]+)", line)
        if m and m.group(1) in names and m.group(3).lower() in units:
            mb = round(float(m.group(2)) * units[m.group(3).lower()])
            peak = st.setdefault("peak_mb", {})
            peak[names[m.group(1)]] = max(mb, peak.get(names[m.group(1)], 0))


def protocol(check: dict, st: dict, rule: str) -> dict:
    """Which protocol choices the paper stated and which REFEREE supplied, per check."""
    runs, rel = int(check.get("runs") or 1), (check.get("target") or {}).get("relation", "")
    src = check.get("runs_source") or ("paper" if check.get("runs_quote") else "referee")
    devs = check.get("deviations") or []
    return {"runs": runs, "runs_from": {"paper": f"the paper: {check.get('runs_quote', '')!r}",
                                        "referee_floor": "REFEREE: at least SH_REPLICATES seeded replicates",
                                        "referee": "REFEREE: the paper states no run count"}.get(src, src),
            "seeds": "0..n-1 passed as --seed (REFEREE)" if check["kind"] != "AUTHOR_CODE"
            else (f"the documented {check.get('seed_flag')} flag (REFEREE varies only its value)"
                  if check.get("seed_flag") else "none (one documented run)"),
            "decision_rule": rule,
            **({"relation": f"{rel} (written by REFEREE's planner for the quoted sentence)"} if rel else {}),
            "supplied_by_referee": [d["used"] for d in devs if not d.get("printed")],
            "claim_changes": [d["used"] for d in devs if d.get("changes_claim")],
            **({"pilot_seconds": st["pilot_s"]} if st.get("pilot_s") else {}),
            **({"seeds_reused_from_checkpoints": len(st.get("done_seeds", []))} if st.get("done_seeds") else {})}


def stop(cfg: state.Config, pid: str, cid: str, why: str) -> dict:
    """The operator ends a running check: in-flight runs are cancelled (each recorded) and the
    check is INCONCLUSIVE for a stated reason about this host, never a finding about the paper."""
    root = state.pdir(cfg, pid)
    with state.lock(root / ".lock"):
        cdir = root / "checks" / cid
        st = state.read_json(cdir / "exec.json") or {}
        if (cdir / "outcome.json").exists() or not st:
            return {"error": f"{cid} is not running"}
        _finish(cfg, root, state.read_json(cdir / "check.json"), {
            **_cancel(root, st), "ev": {"infra_error": "stopped by the operator"},
            "failure": f"stopped by the operator after {st.get('seed', 0)} completed run(s): {why}"})
        return state.read_json(cdir / "outcome.json")


def _cancel(root: Path, st: dict) -> dict:
    """End the seeds still in flight once one seed decided the check; each leaves a record."""
    for rec in st.get("fly", {}).values():
        _docker(["docker", "rm", "-f", rec["container"]], 60)
        state.append_jsonl(root / "execution.jsonl", {**rec, "returncode": None, "timed_out": False, "stdout": "",
                                                       "stderr": "", "ended_at": state.now(), "seconds": 0,
                                                       "error": "cancelled: another seed of this check ended it"})
    st["fly"] = {}
    return st


def _finish(cfg: state.Config, root: Path, check: dict, st: dict) -> bool:
    from .reconcile import reconcile
    kind, src = check["kind"], state.read_json(root / "source.json", {})
    authorized = st.get("authorized", True)
    why = st.get("why", "")
    outcome = {"check": check["id"], "kind": kind, "evidence": EVIDENCE.get(kind, "NONE"), "authorized": authorized,
               "authorization": why, "commit": src.get("commit", "") if (root / "repo" / ".git").is_dir() else ""}
    if st.get("blocker"):         # a documented final blocker of this host: nothing about the paper is established
        outcome.update(status="BLOCKED", reason=f"RESOURCE BLOCKER: {st['blocker']}", rule="bounded pilot and resources",
                       values=[], pilot_values=st.get("values", []), runs=st.get("records", 0),
                       records="execution.jsonl", finished_at=state.now())
    else:
        outcome.update(reconcile(kind, check.get("printed", ""), st.get("values", []), st.get("failure", ""),
                                 st.get("ev", {}), bool(st.get("seeded")) or (kind == "RECONSTRUCTION" and st.get("seed", 0) > 1),
                                 authorized, why, (check.get("target") or {}).get("relation", ""),
                                 cert=st.get("cert") if kind == "CERTIFICATE" and st.get("cert") else None,
                                 changed=any(d.get("changes_claim") for d in check.get("deviations") or []),
                                 step=bool(check.get("step"))),
                       values=st.get("values", []), runs=st.get("records", 0), records="execution.jsonl",
                       finished_at=state.now())
    outcome["protocol"] = protocol(check, st, outcome.get("rule", ""))
    if st.get("env"):
        outcome["environment"] = {k: st["env"].get(k) for k in ("detail", "image", "builder", "recovery")}
    if (st.get("ev") or {}).get("setup_error") and kind != "AUTHOR_CODE":   # the script never started: revisable
        outcome.update(setup_error=st["ev"]["setup_error"], setup_log=st.get("failure", "")[-1500:])
    state.write_json(root / "checks" / check["id"] / "outcome.json", outcome)
    return False


def seeded_command(command: str, flag: str, seed: int) -> tuple[str, bool]:
    """The documented command, with only the value of its own documented seed flag varied."""
    if not flag:
        return command, False
    pat = re.compile(rf"({re.escape(flag)}(?:\s+|=))\d+")
    return (pat.sub(lambda m: f"{m.group(1)}{seed}", command, count=1), True) if pat.search(command) else (command, False)


def try_script(cfg: state.Config, pid: str, cid: str, script: str) -> dict:
    """A draft run for a generator: same sandbox, network none, result lines MASKED so a
    script cannot be tuned toward the printed number, recorded as mode='try' (never evidence)."""
    if not cfg.allow_script_exec:
        return {"error": "SH_ALLOW_SCRIPT_EXEC is not set, so no draft may run"}
    root = state.pdir(cfg, pid)
    tdir = root / "checks" / cid / "try"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "script.py").write_bytes(script.encode("utf-8"))
    ok, why = docker_status()
    if not ok:
        return {"error": why}
    c = next((k for k in (state.read_json(root / "sealed" / "plan.json") or {}).get("checks", []) if k["id"] == cid), {})
    try:
        env_dir, envinfo = with_packages(cfg, root, *script_env(cfg, root, c.get("kind", ""),
                                                              bool(c.get("repo_attributed"))), script)
    except ValueError as e:
        return {"error": str(e)}
    if envinfo is None:
        return {"error": "the environment is still being built in the background: retry the draft in a few "
                         "minutes, or finish without one"}
    if not envinfo["ok"]:
        return {"error": envinfo["detail"]}
    has_repo = (root / "repo" / ".git").is_dir()
    mounts = [(tdir, f"{MOUNT}/check", False), (volume(env_dir), "/env", True)] + (
        [(root / "repo", f"{MOUNT}/repo", True)] if has_repo else [])
    rec = run(["/env/bin/python", f"{MOUNT}/check/script.py", "--seed", "0"], mounts=mounts,
              workdir=f"{MOUNT}/repo" if has_repo else f"{MOUNT}/check", image=envinfo.get("image", DEFAULT_IMAGE),
              network=False, timeout=cfg.try_timeout_s, mode="try", target=cid,
              meta={"script_sha256": state.sha256(script)})
    state.append_jsonl(root / "execution.jsonl", rec)
    return {"environment": envinfo["detail"], "returncode": rec.get("returncode"), "timed_out": rec.get("timed_out"),
            "error": rec.get("error", ""), "stdout": mask(rec.get("stdout") or "")[-4000:],
            "stderr": mask(rec.get("stderr") or "")[-4000:]}


def mask(s: str) -> str:
    """Draft output with every result line hidden, in either stream."""
    return re.sub(r"(?m)^.*REFEREE_RESULT.*$", "REFEREE_RESULT <masked>", s or "")
