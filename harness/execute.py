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


def build_env(cfg: state.Config, root: Path, env_dir: Path, image: str, checkout: Path | None,
              packages: tuple[str, ...] = ()) -> dict:
    """A venv built inside the container (network on), once, from what the checkout declares:
    its uv lockfile (path sources included), else requirements*.txt, else its package. A
    failed build is rebuilt from scratch only along `recover`; every attempt is an
    ExecutionRecord. Returns the marker: ok, detail, image, builder, uv, recovery."""
    marker = env_dir / "referee-env.json"
    if not (cfg.allow_install and cfg.allow_network) and not (state.read_json(marker) or {}).get("ok"):
        return {"ok": False, "detail": "the install or network gate is shut, so no environment was built", "image": image}
    with state.lock(env_dir.parent / f".{env_dir.name}.lock"):      # parallel checks build it once
        if (m := state.read_json(marker)) and "ok" in m:            # ponytail: a failed build is not retried per check
            return m
        uv = bool(checkout and not packages and (checkout / "uv.lock").is_file())
        reqs = sorted(p.name for p in checkout.glob("requirements*.txt")) if checkout else []
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
        steps = ["python -m venv /env", "/env/bin/python -m pip install --quiet --upgrade pip", *add,
                 "/env/bin/python -m pip freeze > /env/freeze.txt"]
        recovery: list[dict] = []
        rec: dict = {}
        for _ in range(1 + MAX_RECOVERIES):
            shutil.rmtree(env_dir, ignore_errors=True)               # isolated: every attempt starts empty
            env_dir.mkdir(parents=True, exist_ok=True)
            mounts = [(env_dir, "/env", False)] + ([(checkout, "/repo", True)] if checkout else [])
            rec = run(["sh", "-c", " && ".join(steps)], mounts=mounts, workdir="/", image=image, network=True,
                      timeout=cfg.install_timeout_s, mode="install", target=str(env_dir.name),
                      meta={"builder": builder, "recovery": list(recovery)})
            state.append_jsonl(root / "execution.jsonl", rec)
            if rec.get("returncode") == 0 or not (fix := recover(rec.get("stderr", ""), image, checkout)):
                break
            recovery.append({"failure": (rec.get("stderr") or "")[-300:], "action": fix[1], "image": fix[0]})
            image = fix[0]
        ok = rec.get("returncode") == 0
        detail = (f"built {image} venv by {builder}; freeze sha256 {state.sha256((env_dir / 'freeze.txt').read_bytes())[:12]}"
                  if ok else f"environment build failed: {(rec.get('stderr') or rec.get('error') or '')[-300:]}")
        m = {"ok": ok, "detail": detail, "image": image, "builder": builder, "uv": uv, "recovery": recovery}
        state.write_json(marker, m)
        return m


def author_env(cfg: state.Config, pid: str) -> dict:
    """The authors' environment (root/env): used by AUTHOR_CODE and by reconstructions that
    drive the authors' code; built in the background as soon as a plan needs it."""
    root = state.pdir(cfg, pid)
    return build_env(cfg, root, root / "env", image_for(root / "repo"), root / "repo")


def script_env(cfg: state.Config, root: Path, kind: str, attributed: bool) -> tuple[Path, dict]:
    """A RECONSTRUCTION runs where the authors' code runs (their environment) when that
    builds; every other script, or a failed build, runs on the fixed baseline."""
    base = cfg.projects / ".script-env"
    if kind == "RECONSTRUCTION" and attributed and (root / "repo" / ".git").is_dir():
        env = author_env(cfg, root.name)
        if env.get("ok"):
            return root / "env", env
        b = build_env(cfg, root, base, DEFAULT_IMAGE, None, SCRIPT_PACKAGES)
        return base, {**b, "detail": f"{b['detail']} (the authors' environment did not build: {env['detail'][-200:]})"}
    return base, build_env(cfg, root, base, DEFAULT_IMAGE, None, SCRIPT_PACKAGES)


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
    failure, ev, seeded, literal, envinfo = "", {}, False, [], {}
    rel = (check.get("target") or {}).get("relation", "")
    if ok:
        if kind == "AUTHOR_CODE":
            envinfo = author_env(cfg, pid)
            ok, why, image, gpus = envinfo["ok"], envinfo["detail"], envinfo["image"], gpu(cfg)
            # The authors' code writes into its own copy of the checkout (no .git), never into
            # the pinned checkout that released-data hashes and host `git` read.
            work = cdir / "work"
            shutil.rmtree(work, ignore_errors=True)
            shutil.copytree(checkout, work, ignore=shutil.ignore_patterns(".git"))
            mounts, workdir = [(work, f"{MOUNT}/repo", False), (root / "env", "/env", False)], f"{MOUNT}/repo"
            env = {"PATH": "/env/bin:/usr/local/bin:/usr/bin:/bin", "VIRTUAL_ENV": "/env",
                   **(UV_RUN if envinfo.get("uv") else {})}
            if ok and check.get("prepare"):
                rec = run(["sh", "-c", check["prepare"]], mounts=mounts, workdir=workdir, image=image, network=True,
                          timeout=cfg.install_timeout_s, env=env, mode="prepare", target=check["id"])
                recs.append(rec)
                ok, why = rec.get("returncode") == 0, "prepare step: " + (rec.get("stderr") or rec.get("error") or "")[-300:]
        else:
            env_dir, envinfo = script_env(cfg, root, kind, bool(check.get("repo_attributed")))
            ok, why, image = envinfo["ok"], envinfo["detail"], envinfo.get("image", DEFAULT_IMAGE)
            gpus = env_dir == root / "env" and gpu(cfg)
            # The script sees only its own approved copy, read-only (a run cannot rewrite what
            # later seeds execute); scratch space is the container's own /tmp.
            rundir = cdir / "run"
            rundir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cdir / "script.py", rundir / "script.py")
            if state.sha256((rundir / "script.py").read_bytes()) != check.get("script_sha256"):
                ok, why = False, "the script on disk is not the approved script"
            mounts = [(rundir, f"{MOUNT}/check", True), (env_dir, "/env", True)] + (
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
                      env=env, gpus=gpus, mode="evidence", target=check["id"],
                      meta={"commit": outcome["commit"], "seed": seed, "script_sha256": check.get("script_sha256", "")})
            recs.append(rec)
            ev = classify(rec)
            if kind == "AUTHOR_CODE":
                v, note = parse_metric(rec["stdout"], check.get("metric", ""))
                vs = [v] if v is not None and not (ev["failed"] and "text" in note) else []
            else:   # bound by name: the relation's outputs, the generator's compared output, or `violated`
                vs = (relation_margins(rec["stdout"], rel) if rel else
                      result_values(rec["stdout"], "violated" if kind == "CERTIFICATE" else check.get("metric", "")))
                literal += result_values(rec["stdout"], "literal_violated") if kind == "CERTIFICATE" else []
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
                             ok and outcome["authorized"], outcome["authorization"], rel),
                   values=values, runs=len([r for r in recs if r["mode"] == "evidence"]),
                   records="execution.jsonl", finished_at=state.now())
    if envinfo:
        outcome["environment"] = {k: envinfo.get(k) for k in ("detail", "image", "builder", "recovery")}
    if literal:           # the printed text evaluated exactly as printed, beside the recorded deviations
        outcome["literal"] = {"n": len(literal), "violated": sum(1 for v in literal if v == 1)}
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
    c = next((k for k in (state.read_json(root / "sealed" / "plan.json") or {}).get("checks", []) if k["id"] == cid), {})
    if c.get("kind") == "RECONSTRUCTION" and c.get("repo_attributed") and not (root / "env" / "referee-env.json").exists():
        return {"error": "the authors' environment is still being built in the background: retry the draft in a "
                         "few minutes, or finish without one"}
    env_dir, envinfo = script_env(cfg, root, c.get("kind", ""), bool(c.get("repo_attributed")))
    if not envinfo["ok"]:
        return {"error": envinfo["detail"]}
    has_repo = (root / "repo" / ".git").is_dir()
    mounts = [(tdir, f"{MOUNT}/check", False), (env_dir, "/env", True)] + (
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
