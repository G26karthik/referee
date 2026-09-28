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
        if (m := re.search(r"python(?:_requires)?\W{0,6}(?:>=|==|~=|=)?\s*3\.(9|1[0-3])\b", text, re.I)):
            return f"python:3.{m.group(1)}-slim"
    return DEFAULT_IMAGE


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
          "no cuda gpus", "api_key", "api key", "wandb", "login", "token", "permission denied")
_SIGNALS = {-9, -11, -6, 137, 139, 134, 136, 132, 135}


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
def result_values(stdout: str, key: str) -> list[float]:
    """Values of `key` from `REFEREE_RESULT {json}` lines (the script contract)."""
    out = []
    for line in (stdout or "").splitlines():
        if line.startswith("REFEREE_RESULT "):
            try:
                v = json.loads(line[len("REFEREE_RESULT "):]).get(key)
            except (ValueError, AttributeError):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
    return out


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


def build_env(cfg: state.Config, root: Path, env_dir: Path, image: str, checkout: Path | None,
              packages: tuple[str, ...] = ()) -> tuple[bool, str]:
    """A venv built inside the container (network on), once. Records what was installed."""
    marker = env_dir / "referee-env.json"
    if not (cfg.allow_install and cfg.allow_network) and not (state.read_json(marker) or {}).get("ok"):
        return False, "the install or network gate is shut, so no environment was built"
    with state.lock(env_dir.parent / f".{env_dir.name}.lock"):      # parallel checks build it once
        return _build_env(cfg, root, env_dir, image, checkout, packages, marker)


def _build_env(cfg, root, env_dir, image, checkout, packages, marker) -> tuple[bool, str]:
    if (m := state.read_json(marker)) and m.get("ok"):
        return True, m["detail"]
    env_dir.mkdir(parents=True, exist_ok=True)
    mounts = [(env_dir, "/env", False)] + ([(checkout, "/repo", True)] if checkout else [])
    steps = ["python -m venv /env", "/env/bin/python -m pip install --quiet --upgrade pip"]
    if packages:
        steps.append("/env/bin/python -m pip install --quiet " + " ".join(packages))
    elif checkout:
        reqs = sorted(p.name for p in checkout.glob("requirements*.txt"))
        if reqs:
            steps += [f"/env/bin/python -m pip install -r /repo/{r}" for r in reqs[:1]]
        elif (checkout / "pyproject.toml").exists() or (checkout / "setup.py").exists():
            steps.append("cp -r /repo /tmp/src && /env/bin/python -m pip install /tmp/src")
    steps.append("/env/bin/python -m pip freeze > /env/freeze.txt")
    rec = run(["sh", "-c", " && ".join(steps)], mounts=mounts, workdir="/", image=image, network=True,
              timeout=cfg.install_timeout_s, mode="install", target=str(env_dir.name))
    state.append_jsonl(root / "execution.jsonl", rec)
    ok = rec.get("returncode") == 0
    detail = (f"built {image} venv; freeze sha256 {state.sha256((env_dir / 'freeze.txt').read_bytes())[:12]}"
              if ok else f"environment build failed: {(rec.get('stderr') or rec.get('error') or '')[-300:]}")
    state.write_json(marker, {"ok": ok, "detail": detail, "image": image})
    return ok, detail


def execute(cfg: state.Config, pid: str, check: dict) -> dict:
    """Run one authorized check to completion and reconcile it; writes outcome.json."""
    from .reconcile import reconcile

    root = state.pdir(cfg, pid)
    cdir, checkout = root / "checks" / check["id"], root / "repo"
    has_repo = (checkout / ".git").is_dir()
    src = state.read_json(root / "source.json", {})
    kind, runs, values, recs = check["kind"], int(check.get("runs") or 1), [], []
    commit_ok = repo_mod.verify_commit(checkout, src.get("commit", "")) if kind == "AUTHOR_CODE" else (True, "")
    ok, why = authorize(cfg, check, commit_ok)
    outcome = {"check": check["id"], "kind": kind, "evidence": EVIDENCE.get(kind, "NONE"), "authorized": ok,
               "authorization": why, "commit": src.get("commit", "") if has_repo else ""}
    failure, ev, seeded = "", {}, False
    if ok:
        image = image_for(checkout) if kind == "AUTHOR_CODE" else DEFAULT_IMAGE
        if kind == "AUTHOR_CODE":
            ok, why = build_env(cfg, root, root / "env", image, checkout)
            # The authors' code writes into its own copy of the checkout (no .git), never into
            # the pinned checkout that released-data hashes and host `git` read.
            work = cdir / "work"
            shutil.rmtree(work, ignore_errors=True)
            shutil.copytree(checkout, work, ignore=shutil.ignore_patterns(".git"))
            mounts, workdir = [(work, f"{MOUNT}/repo", False), (root / "env", "/env", False)], f"{MOUNT}/repo"
            env = {"PATH": "/env/bin:/usr/local/bin:/usr/bin:/bin", "VIRTUAL_ENV": "/env"}
            if ok and check.get("prepare"):
                rec = run(["sh", "-c", check["prepare"]], mounts=mounts, workdir=workdir, image=image, network=True,
                          timeout=cfg.install_timeout_s, env=env, mode="prepare", target=check["id"])
                recs.append(rec)
                ok, why = rec.get("returncode") == 0, "prepare step: " + (rec.get("stderr") or rec.get("error") or "")[-300:]
        else:
            ok, why = build_env(cfg, root, cfg.projects / ".script-env", DEFAULT_IMAGE, None, SCRIPT_PACKAGES)
            # The script sees only its own approved copy, read-only (a run cannot rewrite what
            # later seeds execute); scratch space is the container's own /tmp.
            rundir = cdir / "run"
            rundir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cdir / "script.py", rundir / "script.py")
            if state.sha256((rundir / "script.py").read_bytes()) != check.get("script_sha256"):
                ok, why = False, "the script on disk is not the approved script"
            mounts = [(rundir, f"{MOUNT}/check", True), (cfg.projects / ".script-env", "/env", True)] + (
                [(checkout, f"{MOUNT}/repo", True)] if has_repo else [])
            workdir, env = (f"{MOUNT}/repo" if has_repo else f"{MOUNT}/check"), {}
        if not ok:
            outcome.update(authorized=False, authorization=why)
        for seed in (range(runs) if ok else ()):
            if kind == "AUTHOR_CODE":
                cmd, seeded = seeded_command(check["command"], check.get("seed_flag", ""), seed)
                argv = ["sh", "-c", cmd]
            else:
                argv = ["/env/bin/python", f"{MOUNT}/check/script.py", "--seed", str(seed)]
            rec = run(argv, mounts=mounts, workdir=workdir, image=image, network=False, timeout=cfg.run_timeout_s,
                      env=env, gpus=kind == "AUTHOR_CODE" and gpu(cfg), mode="evidence", target=check["id"],
                      meta={"commit": outcome["commit"], "seed": seed, "script_sha256": check.get("script_sha256", "")})
            recs.append(rec)
            ev = classify(rec)
            if kind == "AUTHOR_CODE":
                v, note = parse_metric(rec["stdout"], check.get("metric", ""))
                vs = [v] if v is not None and not (ev["failed"] and "text" in note) else []
            else:
                vs = result_values(rec["stdout"], "violated" if kind == "CERTIFICATE" else check.get("metric", ""))
            if ev["failed"]:
                failure = (rec.get("error") or (rec.get("stderr") or "")[-400:] or f"exit {rec.get('returncode')}").strip()
                break
            if not vs:            # a clean exit with no bound metric establishes nothing
                values = []
                break
            values += vs
    for rec in recs:
        state.append_jsonl(root / "execution.jsonl", rec)
    outcome.update(reconcile(kind, check.get("printed", ""), values, failure, ev, seeded,
                             ok and outcome["authorized"], outcome["authorization"]),
                   values=values, runs=len([r for r in recs if r["mode"] == "evidence"]),
                   records="execution.jsonl", finished_at=state.now())
    state.write_json(cdir / "outcome.json", outcome)
    return outcome


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
    ok, why = build_env(cfg, root, cfg.projects / ".script-env", DEFAULT_IMAGE, None, SCRIPT_PACKAGES)
    if not ok:
        return {"error": why}
    has_repo = (root / "repo" / ".git").is_dir()
    mounts = [(tdir, f"{MOUNT}/check", False), (cfg.projects / ".script-env", "/env", True)] + (
        [(root / "repo", f"{MOUNT}/repo", True)] if has_repo else [])
    rec = run(["/env/bin/python", f"{MOUNT}/check/script.py", "--seed", "0"], mounts=mounts,
              workdir=f"{MOUNT}/repo" if has_repo else f"{MOUNT}/check", image=DEFAULT_IMAGE, network=False,
              timeout=cfg.try_timeout_s, mode="try", target=cid, meta={"script_sha256": state.sha256(script)})
    state.append_jsonl(root / "execution.jsonl", rec)
    return {"returncode": rec.get("returncode"), "timed_out": rec.get("timed_out"), "error": rec.get("error", ""),
            "stdout": mask(rec.get("stdout"))[-4000:], "stderr": mask(rec.get("stderr"))[-4000:]}


def mask(s: str) -> str:
    """Draft output with every result line hidden, in either stream."""
    return re.sub(r"(?m)^.*REFEREE_RESULT.*$", "REFEREE_RESULT <masked>", s or "")
