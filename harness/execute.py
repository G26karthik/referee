"""The one execution gate (`authorize`) and the container runner.

Every process a review starts runs in a Linux container (`--rm`, named so a timeout can
kill it, `--network none` for the experiment itself) and leaves an ExecutionRecord:
argv, image, commit, script sha, timestamps, exit code, stdout, stderr (invariant 16).
Model-written scripts see the checkout READ-ONLY, so released data cannot be edited after
it was hashed. A draft run (`try`) is recorded with mode="try" and never counts.
"""
from __future__ import annotations

import json
import math
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


def run_image(image: str, gpus: bool) -> str:
    """The image a step runs in. A step given the GPU runs on the full image of the same Python:
    GPU stacks compile kernels at run time (triton, torch.compile) and a slim image has no C
    compiler, so the GPU would fail or be silently dropped for the CPU. The environment is the same."""
    return image.removesuffix("-slim") if gpus else image


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
    for host, inside, ro in mounts:   # a named volume stays a name (resolving it made an empty host dir)
        launch += ["-v", f"{_src(host)}:{inside}{':ro' if ro else ''}"]
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


def awake() -> float:
    """Seconds this host has been awake since it booted: a clock that stops while the host sleeps (a closed lid, a
    critical battery), so a step's run time is what it ran, never the hours its containers were frozen. Windows:
    the unbiased interrupt time; macOS: CLOCK_UPTIME_RAW; Linux: CLOCK_MONOTONIC (both stop in suspend)."""
    import sys
    if sys.platform == "win32":
        import ctypes
        t = ctypes.c_ulonglong(0)
        if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(t)):
            return t.value / 1e7
    for clk in ("CLOCK_UPTIME_RAW", "CLOCK_MONOTONIC"):
        if hasattr(time, clk):
            return float(time.clock_gettime(getattr(time, clk)))
    return float(time.monotonic())


def _slept(rec: dict, wall: float) -> float:
    """Seconds the host slept since `rec` started (its wall offset from the awake clock grew by exactly that), at most
    the `wall` seconds elapsed. A record started before this clock existed has no offset: nothing is subtracted."""
    off = rec.get("awake_offset")
    s = min(max(0.0, time.time() - awake() - off), max(0.0, wall)) if isinstance(off, (int, float)) else 0.0
    return s if s >= 1 else 0.0           # under a second is the two clocks' jitter, not a sleep


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
           "network": network, **(meta or {}), "container": name, "started_at": state.now(),
           "awake_offset": round(time.time() - awake(), 3)}   # wall minus awake: grows only while the host sleeps
    rc, out = _docker(launch, 480)          # ponytail: includes an image pull; 8 min fits one tool call
    if rc != 0 and _docker(["docker", "inspect", name], 60)[0] != 0:
        rec.update(returncode=None, timed_out=False, stdout="", stderr="", ended_at=state.now(), seconds=0,
                   error=f"could not start: {out.strip()[-300:]}")
    return rec


def collect(rec: dict, timeout: int) -> dict | None:
    """The finished record of a started step, or None while it runs (or while the daemon is
    away). A step past `timeout` of the host's awake time is killed and recorded as timed out; the hours a sleeping
    host froze it are not its run time (Oct-01: a 6 h laptop sleep read as a run past the limit)."""
    if "returncode" in rec:
        return rec
    name = rec["container"]
    rc, out = _docker(["docker", "inspect", "-f", "{{json .State}}", name], 60)
    if rc != 0:                           # only "no such container" is a vanished step; any other
        if "no such" not in out.lower():  # error (daemon away, a 500 under load) is asked again later
            return None
        return {**rec, "returncode": None, "timed_out": False, "stdout": "", "stderr": "", "ended_at": state.now(),
                "seconds": 0, "error": "the container disappeared before it was collected"}
    st, killed = json.loads(out), False
    if st.get("Running"):
        wall = time.time() - _secs(st.get("StartedAt", ""))
        if wall - _slept(rec, wall) <= timeout:
            return None
        _docker(["docker", "kill", name], 60)
        rc, out = _docker(["docker", "inspect", "-f", "{{json .State}}", name], 60)   # collected now, on the same clock
        if rc != 0 or json.loads(out).get("Running"):
            return None
        st, killed = json.loads(out), True
    try:
        p = subprocess.run(["docker", "logs", name], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        so, se = p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as e:
        so, se = "", f"logs unavailable: {e}"
    t0, t1 = _secs(st.get("StartedAt", "")), _secs(st.get("FinishedAt", ""))
    slept = _slept(rec, t1 - t0)
    ran = max(0.0, t1 - t0 - slept)
    timed_out = killed or ran >= timeout
    _docker(["docker", "rm", "-f", name], 60)
    return {**rec, "returncode": None if timed_out else st.get("ExitCode"), "timed_out": timed_out,
            "stdout": so[-_OUT_CAP:], "stderr": se[-_OUT_CAP:], "ended_at": st.get("FinishedAt", state.now())[:19] + "Z",
            "seconds": round(ran, 2), **({"host_slept_s": round(slept)} if slept >= 1 else {}),
            **({"error": f"timeout after {timeout}s"} if timed_out else {"error": "out of memory"} if st.get("OOMKilled") else {})}


def _vanished(rec: dict) -> bool:
    """The step's container was removed before it was collected: an infrastructure event."""
    return (rec.get("error") or "").startswith("the container disappeared")


def _volume_gone(name: str) -> bool:
    """Docker no longer holds this named volume (its storage was reset or pruned): what it held
    must be rebuilt, never mounted empty. A daemon that cannot answer is not an answer."""
    rc, out = _docker(["docker", "volume", "inspect", name], 60)
    return rc != 0 and "no such volume" in out.lower()


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
    both = (rec.get("stdout") or "") + "\n" + err
    # The burden is on showing it started: a result or progress line in either stream shows it;
    # otherwise only a long, talkative, clean run does — talk before its traceback, not the
    # traceback itself. An unproven start accuses nobody.
    marked = any(t in both for t in ("REFEREE_RESULT ", "REFEREE_PROGRESS ", "REFEREE_DATA "))
    talk = both.rsplit("Traceback (most recent call last)", 1)[0]
    reached = marked or (not setup and not rec.get("timed_out") and not rec.get("error")
                         and rec.get("seconds", 0) >= 30 and len(talk.splitlines()) >= 6)
    frames = re.findall(r'File "([^"]+)", line \d+', err.rsplit("Traceback (most recent call last)", 1)[-1])
    own = frames[-1] if frames and frames[-1].startswith(f"{MOUNT}/repo/") and "site-packages" not in frames[-1] else ""
    return {"infra_error": infra, "setup_error": setup, "reached": reached, "own_code_crash": own,
            "failed": rec.get("returncode") != 0 or bool(rec.get("timed_out") or rec.get("error"))}


# --- metric parsing: a value bound by name, never by position (invariant 16) ------------
def json_lines(text: str, tag: str) -> list[dict]:
    """The `<tag> {json}` lines of a stream: REFEREE_RESULT (the compared outputs, stdout only),
    REFEREE_DATA (a dataset's identity) and REFEREE_PROGRESS (a stage starting), either stream."""
    out = []
    for line in (text or "").splitlines():
        if line.startswith(tag + " "):
            try:
                d = json.loads(line[len(tag) + 1:])
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
    return out


def _num(d: dict) -> dict:
    """The finite numeric fields (a NaN or infinity is no measurement)."""
    return {k: float(v) for k, v in d.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


def _results(stdout: str) -> list[dict]:
    """The numeric fields of each `REFEREE_RESULT {json}` line (the script contract)."""
    return [_num(d) for d in json_lines(stdout, "REFEREE_RESULT")]


def result_values(stdout: str, key: str) -> list[float]:
    """Values of `key`, by name, from the result lines."""
    return [d[key] for d in _results(stdout) if key in d]


def staged_values(stdout: str, rel: str, metric: str) -> list[list]:
    """[stage, value] per result line (plus the line's `reading`, when it names one): the
    relation's paired margin (every named output present) or the named metric. `stage` ("" if
    unnamed) is the unit a result is decided in: a dataset, a setting, a panel — a stage is
    decided over the seeds, never averaged with another stage. A `reading` names the definition
    (the paper's, the authors' code's) a value was computed under."""
    names = relation(rel)[3] if rel else [metric] if metric else []
    return [[str(d.get("stage") or "")[:80], margin(rel, _num(d)) if rel else _num(d)[metric]]
            + ([str(d["reading"])[:40]] if d.get("reading") else [])
            for d in json_lines(stdout, "REFEREE_RESULT") if names and all(n in _num(d) for n in names)]


def compared_names(check: dict) -> list[str]:
    """The outputs a check's result lines must carry: `violated`, the relation's names, or the metric."""
    rel = (check.get("target") or {}).get("relation", "")
    return (["violated"] if check.get("kind") == "CERTIFICATE" else relation(rel)[3] if rel else
            [check["metric"]] if check.get("metric") else [])


def _cohort(v) -> str | None:
    """A reading's cohort (the models, items or rows it was computed over) as one comparable key."""
    if isinstance(v, list):
        items = sorted(str(i) for i in v)
        return f"{len(items)}:{state.sha256(json.dumps(items))[:16]}"
    return str(v)[:80] if isinstance(v, (str, int)) and not isinstance(v, bool) else None


def reading_cohorts(stdout: str, check: dict) -> dict:
    """{stage: {reading: cohort key}} over the result lines that carry the compared outputs."""
    names, out = compared_names(check), {}
    for d in json_lines(stdout, "REFEREE_RESULT"):
        if names and all(n in _num(d) for n in names):
            out.setdefault(str(d.get("stage") or "")[:80], {})[str(d.get("reading") or "")[:40]] = _cohort(d.get("cohort"))
    return out


def cohort_mismatch(stdout: str, check: dict) -> list[str]:
    """Stages whose declared readings were computed on different cohorts (not comparable)."""
    want = [r["name"] for r in check.get("readings") or []]
    return sorted(s or "(no stage)" for s, rs in reading_cohorts(stdout, check).items()
                  if want and len({rs.get(r) for r in want if r in rs}) > 1)


def _data_identity(rec: dict, check: dict) -> list[str]:
    """A check that acquired data prints, per dataset, a REFEREE_DATA line saying whether what it read
    matches the paper's own description (`matches`: true or false), in either stream."""
    if not check.get("acquire"):
        return []
    ids = json_lines((rec.get("stdout") or "") + "\n" + (rec.get("stderr") or ""), "REFEREE_DATA")
    if not ids:
        return ["the check acquired data, but the run printed no REFEREE_DATA line (each dataset's identity against "
                "the paper's own description, with `matches` true or false)"]
    if any(not isinstance(d.get("matches"), bool) for d in ids):
        return ["a REFEREE_DATA line carries no boolean `matches` (true or false: does the data read match the paper's "
                "own description)"]
    return []


def result_schema(rec: dict, check: dict) -> list[str]:
    """What a completed run's result lines owe the script contract and did not deliver, by name
    only (a value is never shown): the compared outputs on some line; a result line for every
    declared unit, and none under a stage name it did not declare; every declared reading in every
    stage, each with its `cohort` (a list of item ids), the same cohort for all, and no `reading` the
    check does not declare; a `data_fingerprint` on every result line of a stochastic reconstruction
    (the run's own evidence that its replicates differ); `binomial` outputs that are proportions of
    their counted trials; `premises_hold` on every certificate instance; a REFEREE_DATA identity for
    acquired data. A defect is the script's to fix: it says nothing about the paper."""
    names, out = compared_names(check), _data_identity(rec, check)
    if check.get("kind") == "CERTIFICATE":
        rows = cert_rows(rec.get("stdout") or "")
        if not rows:
            return out + ["no REFEREE_RESULT line carries `violated` (0 or 1)"]
        if (k := sum(1 for r in rows if r["premises"] is None)):
            out.append(f"{k} of {len(rows)} certificate result line(s) carry no `premises_hold` (0 or 1): an instance "
                       "whose premises were not evaluated is not admissible")
        return out
    lines = [d for d in json_lines(rec.get("stdout") or "", "REFEREE_RESULT") if all(n in _num(d) for n in names)]
    if names and not lines:
        out.append(f"no REFEREE_RESULT line carries every compared output {names}")
    if (bad := sorted({k for d in lines if isinstance(d.get("binomial"), dict) for k, t in d["binomial"].items()
                       if k in _num(d) and isinstance(t, (int, float)) and not isinstance(t, bool) and t >= 1
                       and not (0 <= _num(d)[k] <= 1 and abs(_num(d)[k] * t - round(_num(d)[k] * t)) <= 1e-6)})):
        out.append(f"output(s) {bad[:10]} declared under `binomial` are not proportions of their counted trials (a "
                   "value outside [0, 1], or value x trials not a whole number)")
    if (check.get("kind") == "RECONSTRUCTION" and check.get("stochastic") is not False and check.get("test") != "compatibility"
            and (k := sum(1 for d in lines if not d.get("data_fingerprint")))):
        out.append(f"{k} of {len(lines)} result line(s) of a stochastic reconstruction carry no `data_fingerprint` (a "
                   "sha256 of the random draws / data that stage consumed): without it, repeated values are one "
                   "measurement, never replicates")
    if not check.get("readings") and (tags := sorted({str(d["reading"])[:40] for d in lines if d.get("reading")})):
        out.append(f"result line(s) carry a `reading` {tags[:10]} but the check declares no readings: they are never "
                   "pooled, and nothing is decided on them")
    if check.get("kind") in ("RECONSTRUCTION", "RELEASED_DATA") and lines:
        declared, printed = units(rec), {str(d.get("stage") or "")[:80] for d in lines}
        if declared and (miss := sorted(declared - printed)):
            out.append(f"declared unit(s) {miss[:10]} printed no result line")
        if declared and (extra := sorted(printed - declared)):
            out.append(f"result line(s) under stage name(s) {[e or '(no stage)' for e in extra[:10]]} that the "
                       "REFEREE_PROGRESS units line did not declare")
        want = [r["name"] for r in check.get("readings") or []]
        for st, rs in sorted(reading_cohorts(rec.get("stdout") or "", check).items()) if want else []:
            where = f"stage {st or '(no stage)'}"
            if (miss := [r for r in want if r not in rs]):
                out.append(f"{where}: declared reading(s) {miss} printed no result line")
            if (extra := sorted(set(rs) - set(want))):
                out.append(f"{where}: result line(s) under reading(s) {[e or '(none)' for e in extra]} not declared")
            if any(rs.get(r) is None for r in want if r in rs):
                out.append(f"{where}: a reading's result line carries no `cohort` (the items it was computed over)")
            elif len({rs[r] for r in want if r in rs}) > 1:
                out.append(f"{where}: the readings were computed on different cohorts")
        if want and any(d.get("cohort") is not None and not isinstance(d.get("cohort"), list) for d in lines):
            out.append("a reading's `cohort` is not a list of item ids (the models, items or rows it was computed "
                       "over): a count or a label does not show the readings shared their items")
    return out[:20]                                             # ponytail: 20 defects per run


def split_units(declared: set, staged: list, run_failed: bool, why: str) -> tuple[dict, list[str]]:
    """(incomplete stages, schema defects) of one run. A declared unit without a result is not
    completed, except in the one case where the results are unambiguous: a completed run that
    declared ONE unit and printed its results without a stage name. That is a labeling defect
    (recorded; the results are the check's only unit), never a measurement that did not happen.
    Several declared units are never matched to results by guessing."""
    printed = {e[0] for e in staged}
    miss = declared - printed
    if not run_failed and len(declared) == 1 and miss == declared and printed == {""}:
        return {}, [f"the run completed and declared one unit {sorted(declared)}, but printed its result line(s) "
                    "without that `stage` name"]
    return {s: why for s in miss}, []


def cert_rows(stdout: str) -> list[dict]:
    """A certificate's instances: `violated`, `premises` (1/0 when the script evaluated every
    premise of the exact claim; None when it did not say), `literal` (the text as printed),
    `lhs`/`rhs` (floats), `exact` (the script also printed both sides as exact strings) and, only
    when the line names one, the `reading` it was computed under (never a counterexample to the
    printed claim: reconcile.certificate)."""
    rows = []
    for d in json_lines(stdout, "REFEREE_RESULT"):
        if d.get("violated") in (0, 1):
            rows.append({"violated": int(d["violated"]),
                         "premises": int(d["premises_hold"]) if d.get("premises_hold") in (0, 1) else None,
                         "literal": d.get("literal") if d.get("literal") in ("holds", "fails", "undefined",
                                                                             "premise_not_met") else None,
                         **{k: float(d[k]) for k in ("lhs", "rhs") if isinstance(d.get(k), (int, float))
                            and not isinstance(d.get(k), bool)},
                         "exact": all(isinstance(d.get(k), str) and d.get(k) for k in ("lhs_exact", "rhs_exact")),
                         **({"reading": str(d["reading"])[:40]} if d.get("reading") else {})})
    return rows


def result_detail(stdout: str, seed: int) -> list[dict]:
    """What one run printed, per result line, for judging whether replicates differed: every finite
    numeric output, the number of counted trials a proportion declares (`binomial`: {output: trials}),
    and the script's fingerprint of the data it generated (`data_fingerprint`)."""
    rows = []
    for d in json_lines(stdout, "REFEREE_RESULT")[:2000]:                    # ponytail: 2000 lines per run
        out = dict(list(_num(d).items())[:40])                                # ponytail: 40 outputs per line
        b = d.get("binomial") if isinstance(d.get("binomial"), dict) else {}
        rows.append({"seed": seed, "stage": str(d.get("stage") or "")[:80], "reading": str(d.get("reading") or "")[:40],
                     "out": out, "data_fp": str(d.get("data_fingerprint") or "")[:64] or None,
                     "trials": {k: int(v) for k, v in b.items() if k in out and isinstance(v, (int, float))
                                and not isinstance(v, bool) and v >= 1}})
    return rows


def relation_margins(stdout: str, rel: str) -> list[float]:
    """One paired margin per result line that carries every output the relation names."""
    return [e[1] for e in staged_values(stdout, rel, "")]


def failure_text(rec: dict) -> str:
    """What ended a failed run, in its own last words (progress bars skipped), with its exit
    code and duration: the fact a reader needs, not a guess about whether it began."""
    if rec.get("error"):
        return str(rec["error"])
    lines = [ln.strip() for ln in (rec.get("stderr") or "").splitlines()
             if ln.strip() and "it/s]" not in ln and "s/it]" not in ln and "%|" not in ln]
    tail = " | ".join(lines[-3:])[-400:]
    return (f"exit {rec.get('returncode')} after {rec.get('seconds', 0):.0f}s: "
            + (tail or "no error output"))


def units(rec: dict) -> set[str]:
    """The result units a run declared (`REFEREE_PROGRESS {"units": [...]}`): each must print a result."""
    both = (rec.get("stdout") or "") + "\n" + (rec.get("stderr") or "")
    return {str(u)[:80] for d in json_lines(both, "REFEREE_PROGRESS") if isinstance(d.get("units"), list)
            for u in d["units"][:1000]}          # ponytail: 1000 units per check (a threshold grid x detectors)


def markers(st: dict, rec: dict) -> set[str]:
    """Record a run's dataset identities (first report per dataset) and the stages it started
    (progress markers: they locate a failure, they are not units); returns those stages."""
    both = (rec.get("stdout") or "") + "\n" + (rec.get("stderr") or "")
    ids = st.setdefault("data_identity", {})
    for d in json_lines(both, "REFEREE_DATA")[:20]:            # ponytail: 20 datasets per run
        name = str(d.get("dataset") or "")[:120]
        if name and name not in ids:
            s = json.dumps({k: d[k] for k in list(d)[:16]}, default=str)
            ids[name] = json.loads(s) if len(s) <= 4000 else {"truncated": s[:4000]}   # ponytail: 4 kB each
    started = {str(d.get("stage"))[:80] for d in json_lines(both, "REFEREE_PROGRESS") if d.get("stage")}
    if started and "stage_times" not in st:                   # the pilot's own timing, by stage
        st["stage_times"] = [{"stage": str(d.get("stage"))[:80], "t": d.get("t")}
                             for d in json_lines(both, "REFEREE_PROGRESS")[:40]]
    return started


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


DOWNLOAD_CACHE = "referee-download-cache"   # pip/uv wheels shared by every build: a rebuild re-downloads nothing
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


def env_mounts(env_dir: Path, envinfo: dict) -> tuple[list, dict]:
    """How a script sees its environment, read-only: the venv at /env, and a layered env's own
    packages at /extra (first on PYTHONPATH)."""
    if envinfo.get("layered"):
        return ([(envinfo["base"], "/env", True), (volume(env_dir), "/extra", True)],
                {"PYTHONPATH": "/extra/extra"})
    return [(volume(env_dir), "/env", True)], {}


def _env_steps(checkout: Path | None, packages: tuple[str, ...], base: str = "") -> tuple[str, list[str], bool]:
    uv = bool(checkout and not packages and (checkout / "uv.lock").is_file())
    reqs = sorted(p.name for p in checkout.glob("requirements*.txt")) if checkout else []
    if base:   # a thin layer over the read-only base (a copy of a torch env is ~6 GB per script)
        return f"{base} plus {' '.join(packages)} (layered)", [
            "mkdir -p /env/extra", "/base/bin/python -m pip install --quiet --target /env/extra " + " ".join(packages),
            "echo REFEREE_FREEZE", "/base/bin/python -m pip freeze --path /env/extra"], False
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
        if not (m["ok"] and any(_volume_gone(v) for v in (m.get("volume") or volume(env_dir), m.get("base")) if v)):
            return m
        marker.replace(env_dir / "referee-env.vanished.json")   # kept; the same build runs again
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
                    infra := classify(done)["infra_error"] or ("the container vanished" if _vanished(done) else "")
                    or (f"no finish within {cfg.install_timeout_s}s (a slow download)" if done.get("timed_out") else "")):
                fix = (b["image"], f"an infrastructure failure ('{infra}'): rebuilt unchanged")
            if done.get("returncode") == 0 or not fix or b["attempt"] > MAX_RECOVERIES:
                ok = done.get("returncode") == 0
                freeze = (done.get("stdout") or "").split("REFEREE_FREEZE", 1)[-1].strip()   # kept in execution.jsonl
                m = {"ok": ok, "image": b["image"], "builder": builder, "uv": uv, "recovery": b["recovery"],
                     "volume": volume(env_dir),   # layered only if the step that ran was (a copy started earlier is a full env)
                     **({"base": base, "layered": True} if "--target /env/extra" in " ".join(done.get("argv") or []) else {}),
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
                         mounts=[(volume(env_dir), "/env", False), (DOWNLOAD_CACHE, "/root/.cache", False)]
                         + ([(checkout, "/repo", True)] if checkout else []) + ([(base, "/base", True)] if base else []),
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


# --- public artifacts the checkout does not ship: acquired by harness code (never model code)
# with the network on, from sources the paper or the checkout cites, each file hashed; every
# later step sees them read-only at /work/data. A cited dataset PAGE may be followed to the
# same site's files matching `include`.
DATA_MOUNT = f"{MOUNT}/data"
FETCHER = Path(__file__).with_name("fetcher.py")   # mounted read-only into the network-on container that runs it


def _named_missing(r: dict) -> list[dict]:
    """The NAMED files a source's set lacks: listed or include-matched files not admitted, and include names without
    a wildcard that matched no file (a glob that matched nothing is a gap of the set, not a missing name)."""
    return list(r.get("missing") or []) + [{"file": p, "class": "missing", "why": "no file of the source has this name"}
                                           for p in r.get("unmatched_include") or [] if not re.search(r"[*?\[]", p)]


def data_blocker(sources: list[dict], man: dict) -> list[dict]:
    """The required sources of a check that admitted no file, each with the class of its failure
    (fetcher.failure_class): what a planner and a reader need to tell a bug from a network fault, a
    missing file, an inaccessible source or content that failed validation. A required source that admitted
    files but lacks a NAMED one is blocked too (`incomplete`), with the class of what kept that file out —
    a transfer fault stays a fault of this run, never a finding about the source."""
    from .fetcher import RANK
    got = {str(r.get("dir")): r for r in man.get("sources") or []}
    out = []
    for i, s in enumerate(sources):
        r = got.get(str(i)) or {}
        if s.get("required", True) and not r.get("admitted_files"):
            errs = [a.get("error") for a in (r.get("attempts") or []) + (r.get("followed") or []) if a.get("error")
                    and not a.get("text_only") and not a.get("speculative")]
            out.append({"source": s["source"], "class": r.get("failure_class") or "bug",
                        "detail": (errs[-1] if errs else r.get("error") or "the acquisition returned no record")[:300],
                        "rejected": [f"{x.get('file')}: {x.get('why')}" for x in (r.get("rejected") or [])[:5]],
                        "recovery": (r.get("recovery") or [])[:5]})
        elif s.get("required", True) and (gone := _named_missing(r)):
            seen = {m.get("class") for m in gone}
            out.append({"source": s["source"], "class": next((k for k in RANK if k in seen), "missing"), "incomplete": True,
                        "detail": (f"incomplete: {len(gone)} named file(s) of this source were not admitted "
                                   f"({r.get('admitted_files')} were): " + "; ".join(
                                       f"{m['file']} [{m.get('class')}]: {m.get('why', '')[:80]}" for m in gone[:5]))[:300],
                        "missing": gone[:50], "rejected": [f"{x.get('file')}: {x.get('why')}" for x in (r.get("rejected") or [])[:5]],
                        "recovery": (r.get("recovery") or [])[:5]})
    return out


def data_gaps(data: dict) -> list[str]:
    """What an acquired set lacks against its plan, one short line each — a source that admitted nothing, a cut in the
    files followed, a named file not admitted, an `include` pattern that matched nothing; [] when it is complete.
    The data an experiment ran on is what was requested only if this is empty."""
    out = []
    for r in data.get("sources") or []:
        src = str(r.get("source") or "?")[:120]
        if not r.get("admitted_files"):
            out.append(f"{src}: no file admitted ({r.get('failure_class') or 'unknown'})")
        if t := r.get("truncated"):
            out.append(f"{src}: {t.get('followed')} of {t.get('matched')} matching files followed (a cap); not followed: "
                       + ", ".join(map(str, (t.get("not_followed") or [])[:5])))
        out += [f"{src}: {m.get('file')} not admitted ({m.get('class')}: {str(m.get('why', ''))[:120]})" for m in r.get("missing") or []]
        out += [f"{src}: include {p!r} matched no file" for p in r.get("unmatched_include") or []]
    return out


def data_volume(root: Path, cid: str) -> str:
    return _cname("data", Path(root).resolve(), cid)


def data_mount(root: Path, cid: str) -> list:
    """The acquired data of a check, read-only at /work/data, once it has any file."""
    d = state.read_json(Path(root) / "checks" / cid / "data.json") or {}
    return [(d.get("volume") or data_volume(root, cid), DATA_MOUNT, True)] if d.get("n_files") else []


def truncated(man: dict) -> bool:
    """Did the acquisition follow fewer files than matched (a cap)? Such a set is what its own check was given, never
    a complete acquisition of the plan: it is not shared with another check, and a reopened check fetches again."""
    return any(s.get("truncated") for s in man.get("sources") or [])


def record_src(cdir: Path, stdout: str, listing: list[dict]) -> list[dict]:
    """The quote-only text of released code, notebooks and READMEs the fetcher sent on its own stdout line, written
    to checks/<id>/record_src/<source>/<path> (never mounted into a run, never executed) so a reading of a released
    implementation can be quoted. Only files the manifest lists, whose sha256 matches, inside that folder: -> kept."""
    import base64
    import zlib
    out = cdir / "record_src"
    shutil.rmtree(out, ignore_errors=True)
    line = next((ln.split(" ", 1)[1] for ln in (stdout or "").splitlines() if ln.startswith("REFEREE_RECORD_SRC ")), "")
    try:
        items = json.loads(zlib.decompress(base64.b64decode(line))) if line else []
    except (ValueError, zlib.error):
        items = []
    want, kept = {x.get("path"): x for x in listing if isinstance(x, dict)}, []
    for it in items if isinstance(items, list) else []:
        rel = f"{it.get('dir')}/{it.get('path')}"
        x, raw = want.get(rel), str(it.get("text", "")).encode("utf-8")
        p = (out / rel).resolve()
        if not x or state.sha256(raw) != x.get("sha256") or out.resolve() not in p.parents:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        kept.append(x)
    return kept


def fetch(cfg: state.Config, root: Path, cid: str, sources: list[dict]) -> dict | None:
    """Acquire a check's cited public artifacts once (non-blocking: None while it runs). The
    manifest (sources, HTTP status, revision, every file's size and sha256) is data.json and an
    ExecutionRecord (mode=fetch); a failed source is recorded, never silently skipped."""
    cdir = Path(root) / "checks" / cid
    f = cdir / "data.json"
    if (d := state.read_json(f)) and "n_files" in d:
        if not (d["n_files"] and _volume_gone(d.get("volume") or data_volume(root, cid))):
            return d
        f.replace(cdir / "data.vanished.json")    # kept (its hashes); the same sources are acquired again
        d = None
    # Public data needs the network gate alone (code never enters through it); only a hub download
    # installs a client library, and so also needs the install gate.
    # Each source meets only the gate it needs: a closed install gate refuses a hub download, never the plain
    # HTTP source beside it.
    shut = [("SH_ALLOW_NETWORK is not set" if not cfg.allow_network else
             "SH_ALLOW_INSTALL is not set (a Hugging Face download installs its client)"
             if not cfg.allow_install and s["source"].startswith("hf://") else "") for s in sources]
    if all(shut):
        d = {"sources": [{"source": s["source"], "dir": str(i), "failure_class": "gate", "admitted_files": 0,
                          "attempts": [], "error": f"refused: {why}"} for i, (s, why) in enumerate(zip(sources, shut))],
             "files": [], "n_files": 0, "bytes": 0, "status": "none", "fetched_at": state.now()}
        shutil.rmtree(cdir / "record_src", ignore_errors=True)      # no text of an earlier acquisition outlives it
        state.write_json(f, d)
        return d
    run_sources = [{**s, "refused": why} if why else s for s, why in zip(sources, shut)]
    if not docker_status()[0]:
        return None
    st = d or {}
    if not st.get("rec"):   # the same sources, already acquired for another check of this paper: shared, read-only
        for other in sorted(Path(root).glob("checks/*/data.json")):
            o = state.read_json(other) or {}
            if other.parent.name != cid and o.get("fetched_at") and o.get("n_files") and o.get("status") != "partial" and not (
                    data_gaps(o)) and not _volume_gone(o.get("volume") or data_volume(root, other.parent.name)) and [
                    {k: s.get(k) for k in ("source", "include")} for s in o.get("plan", [])] == [
                    {k: s.get(k) for k in ("source", "include")} for s in sources]:   # a partial set is never shared
                if (other.parent / "record_src").is_dir():
                    shutil.rmtree(cdir / "record_src", ignore_errors=True)
                    shutil.copytree(other.parent / "record_src", cdir / "record_src")
                state.write_json(f, {**o, "shared_with": other.parent.name})
                return state.read_json(f)
    plan_key = [{k: s.get(k) for k in ("source", "include")} for s in sources]
    if not st.get("rec"):                                   # the same plan is already being acquired for another check: wait for it
        for other in sorted(Path(root).glob("checks/*/data.json")):
            o = state.read_json(other) or {}
            if other.parent.name != cid and o.get("rec") and not o.get("fetched_at") and [
                    {k: s.get(k) for k in ("source", "include")} for s in o.get("sources", [])] == plan_key:
                return None
    if st.get("rec"):
        done = collect(st["rec"], cfg.install_timeout_s)
        if done is None:
            return None
        state.append_jsonl(Path(root) / "execution.jsonl", {**done, "stdout": done.get("stdout", "")[-20000:]})
        man = next(iter(json_lines(done.get("stdout", ""), "REFEREE_MANIFEST")), None)
        # No manifest: the container itself failed. That is this host (it could not start, ran out of memory
        # or time) or this code, never the source's fault.
        infra = bool(done.get("error")) or done.get("timed_out")
        d = man or {"sources": [{"source": s["source"], "dir": str(i), "admitted_files": 0, "attempts": [],
                                 "failure_class": "infrastructure" if infra else "bug", "error": failure_text(done)}
                                for i, s in enumerate(sources)],
                    "files": [], "n_files": 0, "bytes": 0, "status": "none"}
        listed = d.get("record_src") or []
        d["record_src"] = record_src(cdir, done.get("stdout", ""), listed)
        if lost := [x.get("path") for x in listed if x not in d["record_src"]]:   # never silent: listed, not received intact
            d["record_src_lost"] = lost[:50]
        state.write_json(f, {**d, "plan": sources, "volume": data_volume(root, cid), "fetched_at": state.now(),
                             "seconds": done.get("seconds")})
        return state.read_json(f)
    rec = start(_cname("fetch", cdir.resolve(), json.dumps(sources, sort_keys=True)), ["python", "/referee/fetcher.py"],
                mounts=[(data_volume(root, cid), "/data", False), (DOWNLOAD_CACHE, "/root/.cache", False),
                        (FETCHER.parent, "/referee", True)],
                workdir="/", image=DEFAULT_IMAGE, network=True, mode="fetch", target=cid,
                env={"REFEREE_SOURCES": json.dumps(run_sources), "REFEREE_CAP": str(cfg.max_data_gb << 30),
                     "REFEREE_DENY": ",".join(cfg.deny_sources), "HF_HUB_DISABLE_TELEMETRY": "1"})
    state.write_json(f, {"rec": rec, "sources": sources})
    return None


def smoke(cfg: state.Config, root: Path, cid: str, r: int, kind: str, attributed: bool) -> dict | None:
    """The harness's own draft run of a sealed script (seed 0, network off, result lines
    MASKED, never evidence), so no verifier approves a script nobody has seen run. None while
    its environment builds or it runs; then the record (it is the verifier's to read)."""
    cdir = Path(root) / "checks" / cid
    f = cdir / f"smoke.{r}.json"
    st = state.read_json(f) or {}
    if "returncode" in st:
        return st
    scratch = _cname("smoke-ckpt", cdir.resolve(), r)          # a throwaway /work/ckpt for the draft
    if st.get("rec"):
        done = collect(st["rec"], cfg.try_timeout_s)
        if done is None:
            return None
        state.append_jsonl(Path(root) / "execution.jsonl", done)
        _docker(["docker", "volume", "rm", "-f", scratch], 60)
        ev = classify(done)
        if (str(done.get("error", "")).startswith("could not start") or _vanished(done)) and st.get("starts", 0) < 3:
            state.write_json(f, {"starts": st.get("starts", 0) + 1})   # an infrastructure event: start it again
            return None
        infra = ev["infra_error"] or ("the container could not start" if done.get("error", "").startswith("could not")
                                      or _vanished(done) else "")
        # A draft that completed is held to the result contract (names only; values stay masked).
        check = _smoke_check(root, cid, r)
        schema = result_schema(done, check) if not ev["failed"] and not done.get("timed_out") else []
        res = {"returncode": done.get("returncode"), "timed_out": done.get("timed_out"), "seconds": done.get("seconds"),
               "reached": ev["reached"], "infra_error": infra, "schema": schema,
               "failed": ev["failed"] and not done.get("timed_out"), "failure": failure_text(done) if ev["failed"] else "",
               "stdout": mask(done.get("stdout") or "")[-3000:], "stderr": mask(done.get("stderr") or "")[-3000:]}
        state.write_json(f, res)
        return res
    script = (cdir / f"script.{r}.py").read_text(encoding="utf-8")
    skip = lambda why: {"returncode": None, "skipped": why, "failed": False, "reached": False, "stdout": "", "stderr": ""}
    if not cfg.allow_script_exec:              # the one gate holds for the harness's own draft too
        return skip("SH_ALLOW_SCRIPT_EXEC is not set, so no model-written script runs")
    if not docker_status()[0]:
        return None                            # the daemon is away: wait for it
    base_dir, base = script_env(cfg, root, kind, attributed)
    if base is None:
        return None
    if not base["ok"]:                         # not the author's to fix: the check will be BLOCKED at execution
        return skip(f"its environment did not build: {base['detail'][-400:]}")
    try:
        env_dir, envinfo = with_packages(cfg, root, base_dir, base, script)
    except ValueError as e:
        env_dir, envinfo = None, {"ok": False, "detail": str(e)}
    if envinfo is None:
        return None
    if not envinfo["ok"]:                      # the packages it declares: the author's to fix
        res = {"returncode": None, "failed": True, "reached": False, "stdout": "", "stderr": "",
               "failure": f"the environment with its REFEREE_PACKAGES did not build: {envinfo['detail'][-600:]}"}
        state.write_json(f, res)
        return res
    sdir = cdir / f"smoke{r}"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "script.py").write_bytes(script.encode("utf-8"))
    has_repo = (Path(root) / "repo" / ".git").is_dir()
    em, env = env_mounts(env_dir, envinfo)
    mounts = [(sdir, f"{MOUNT}/check", True)] + em + [(scratch, f"{MOUNT}/ckpt", False)] + (
        [(Path(root) / "repo", f"{MOUNT}/repo", True)] if has_repo else []) + data_mount(root, cid)
    gpus = kind == "RECONSTRUCTION" and gpu(cfg)
    rec = start(_cname(cdir.resolve(), "smoke", r, state.sha256(script), st.get("starts", 0)),
                ["/env/bin/python", f"{MOUNT}/check/script.py", "--seed", "0"], mounts=mounts, env=env,
                workdir=f"{MOUNT}/repo" if has_repo else f"{MOUNT}/check",
                image=run_image(envinfo.get("image", DEFAULT_IMAGE), gpus), network=False, gpus=gpus, mode="try",
                target=cid, meta={"script_sha256": state.sha256(script), "smoke": r})
    state.write_json(f, {"rec": rec, "starts": st.get("starts", 0)})
    return None


def _smoke_check(root: Path, cid: str, r: int) -> dict:
    """The check as its draft will run: the plan's check with the round's sealed script fields (its
    metric, readings and `stochastic`, so a stochastic draft is held to its data fingerprints)."""
    from .tasks import _sealed
    from .report import merged
    plan = merged(_sealed(Path(root), "plan"), _sealed(Path(root), "plan:2")) or {"checks": []}
    c = next((k for k in plan["checks"] if k["id"] == cid), {})
    g = _sealed(Path(root), f"gen:{cid}.{r}") or {}
    return {**c, "metric": g.get("metric") or c.get("metric", ""),
            **({"stochastic": g["stochastic"]} if isinstance(g.get("stochastic"), bool) else {}),
            "readings": (c.get("readings") or []) + [x for x in g.get("readings") or []
                                                     if x["name"] not in {y["name"] for y in c.get("readings") or []}]}


_HOST: dict = {}   # measured once per process (per projects dir): every task brief states it, none waits on it twice


def host(cfg: state.Config) -> dict:
    """This host as measured, for a plan seal to compare a `compute` blocker against: {cpus, ram_mb (for containers),
    gpu, vram_mb (the first GPU's memory), disk_free_gb (where Docker keeps volumes)}; None where not measurable
    (the daemon away; Docker's root not on this host's filesystem, as under Docker Desktop). Unknown is never 0, and
    a failed measurement is not kept: the next call asks again."""
    key = str(cfg.projects)
    if key not in _HOST:
        rc, out = _docker(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}} {{.DockerRootDir}}"], 30)
        if rc != 0:
            return {"cpus": None, "ram_mb": None, "gpu": False, "vram_mb": None, "disk_free_gb": None}
        parts = out.strip().split(maxsplit=2) if rc == 0 else []
        num = lambda i: int(parts[i]) if len(parts) > i and parts[i].isdigit() else None
        g = bool(parts) and gpu(cfg)                     # a daemon that cannot answer is no answer about the GPU
        vram = _gpu_mb() if g else ""
        root_dir = Path(parts[2]) if len(parts) > 2 else None
        try:
            disk = round(shutil.disk_usage(root_dir).free / 2 ** 30, 1) if root_dir and root_dir.is_dir() else None
        except OSError:
            disk = None
        _HOST[key] = {"cpus": num(0), "ram_mb": num(1) // 2 ** 20 if num(1) else None, "gpu": g,
                      "vram_mb": int(vram.split(",")[0]) if vram.split(",")[0].strip().isdigit() else None, "disk_free_gb": disk}
    return dict(_HOST[key])


def host_facts(cfg: state.Config) -> str:
    """What this host offers a check, measured (not assumed), for planners and script authors."""
    h, q = host(cfg), lambda v, unit: f"{v} {unit}" if v is not None else f"unknown {unit}"
    gpu_s = ("YES (CUDA; use it where the method trains a network), " + (f"{h['vram_mb']} MB GPU memory" if h["vram_mb"]
             else "GPU memory unknown")) if h["gpu"] else "no"
    return (f"one Docker host: {q(h['cpus'], 'CPUs')}, {q(h['ram_mb'], 'MB RAM')} for containers, GPU inside containers: "
            f"{gpu_s}; disk for data: " + (f"{h['disk_free_gb']:g} GB free" if h["disk_free_gb"] is not None else
                                           "free space unknown (Docker's storage is not measurable from this host)") +
            f"; {cfg.parallel} runs at a time; one run at most {cfg.run_timeout_s // 60} min; all runs of one check at "
            f"most {cfg.check_budget_s / 3600:g} h (configured); acquired data at most {cfg.max_data_gb} GB per check")


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
        if check.get("acquire") and "n_files" not in (state.read_json(cdir / "data.json") or {}):
            return True                                      # its cited data is (re-)acquiring
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
        st.update(env=envinfo, env_dir=str(env_dir), why=why, seed=0, values=[], cert=[], staged=[], rec=None,
                  stage="prepare" if check.get("prepare") else "run", done_seeds=[], failed_seeds={},
                  image=run_image(envinfo.get("image", DEFAULT_IMAGE),    # one image for every run of the check
                                  kind in ("AUTHOR_CODE", "RECONSTRUCTION") and gpu(cfg)))
        reuse_checkpoints(root, cdir, check, st)       # ended seeds of this exact script/command are kept
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
    env_dir, image = Path(st["env_dir"]), st.get("image") or st["env"].get("image", DEFAULT_IMAGE)
    if kind == "AUTHOR_CODE":
        mounts, workdir = [(cdir / "work", f"{MOUNT}/repo", False), (volume(env_dir), "/env", False)], f"{MOUNT}/repo"
        env = {"PATH": "/env/bin:/usr/local/bin:/usr/bin:/bin", "VIRTUAL_ENV": "/env", **(UV_RUN if st["env"].get("uv") else {})}
    else:
        em, env = env_mounts(env_dir, st["env"])
        mounts = [(cdir / "run", f"{MOUNT}/check", True)] + em + (
            [(checkout, f"{MOUNT}/repo", True)] if has_repo else []) + data_mount(root, cid)
        workdir = f"{MOUNT}/repo" if has_repo else f"{MOUNT}/check"
    # Steps in flight, by seed ("-1" is the prepare step). Independent seeds of a script run
    # SH_PARALLEL at a time (private /tmp, read-only mounts); author code one at a time (it
    # writes into one work copy, and may hold the GPU). Parallel is scheduling, never a downscale.
    fly = st.setdefault("fly", {})
    if st.get("rec"):                                        # one in-flight step, from before parallel seeds
        fly["-1" if st["stage"] == "prepare" else str(st["seed"])] = st["rec"]
        st.setdefault("next", st["seed"] + (st["stage"] == "run"))
    st["rec"] = None
    st.setdefault("next", st.get("seed", 0))
    runs, width = max(int(check.get("runs") or 1), int(st.get("runs_extended") or 0)), 1 if kind == "AUTHOR_CODE" else max(1, cfg.parallel)
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
            if kind in ("RECONSTRUCTION", "RELEASED_DATA"):      # what it printed before the limit is kept beside the
                kept = staged_values(done.get("stdout") or "", (check.get("target") or {}).get("relation", ""),
                                     check.get("metric", ""))   # blocker (pilot_stages), deciding nothing (Oct-01 C8)
                st["values"], st["staged"] = st.get("values", []) + [e[1] for e in kept], st.get("staged", []) + kept
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
        started = markers(st, done)
        rel = (check.get("target") or {}).get("relation", "")
        rows, staged = [], []
        if kind == "AUTHOR_CODE":
            v, note = parse_metric(done["stdout"], check.get("metric", ""))
            vs = [v] if v is not None and not (ev["failed"] and "text" in note) else []
        elif kind == "CERTIFICATE":
            # A crashed instance is inadmissible: rows it printed before crashing count for nothing.
            rows = [] if ev["failed"] else cert_rows(done["stdout"])
            vs = [r["violated"] for r in rows]
        else:   # bound by name: the relation's outputs or the generator's compared output, per stage
            staged = staged_values(done["stdout"], rel, check.get("metric", ""))
            vs = [e[1] for e in staged]
        # A declared result unit that printed no result is not completed, whether or not the run failed.
        declared = units(done) if kind in ("RECONSTRUCTION", "RELEASED_DATA") else set()
        st["units"] = sorted(set(st.get("units") or []) | declared)
        last = next((s for s in reversed([str(d.get("stage")) for d in json_lines(
            (done.get("stdout") or "") + "\n" + (done.get("stderr") or ""), "REFEREE_PROGRESS") if d.get("stage")])), "")
        failed_stages, defects = split_units(declared, staged, ev["failed"], failure_text(done) if ev["failed"]
                                             else "a declared unit printed no result line")
        for s, e in failed_stages.items():
            st.setdefault("stage_errors", {}).setdefault(s, e)
        # Execution and the result contract are recorded apart from what the results say.
        if kind in ("RECONSTRUCTION", "RELEASED_DATA", "CERTIFICATE") and not ev["failed"]:
            defects = sorted(set(defects) | set(result_schema(done, check)))
        mism = cohort_mismatch(done["stdout"], check) if check.get("readings") else []
        st["schema_defects"] = sorted(set(st.get("schema_defects") or []) | set(defects))[:40]   # ponytail: 40 kept
        st["cohort_mismatch"] = sorted(set(st.get("cohort_mismatch") or []) | set(mism))
        st["ok_runs"] = st.get("ok_runs", 0) + (not ev["failed"])
        if ev["failed"] or not vs:
            err = (failure_text(done) + (f" (during {last})" if last else "")) if ev["failed"] else \
                f"exit 0 after {done.get('seconds', 0):.0f}s with no result line"
            if kind == "AUTHOR_CODE" or not st.get("done_seeds") and not vs:
                return _finish(cfg, root, check, {**_cancel(root, st), "ev": ev, "failure": err} if ev["failed"]
                               else {**_cancel(root, st), "values": []})   # a clean exit with nothing establishes nothing
            # A later stage (or a later seed) failed: what the run measured before it failed is kept
            # and the remaining seeds still run, so completed stages accumulate their replicates.
            st.setdefault("failed_seeds", {})[key] = err
        st["values"] += vs
        st.setdefault("cert", []).extend(rows)
        st.setdefault("staged", []).extend(staged)
        # A run that measured and then failed keeps its measurements (staged), so it keeps the lines that say
        # whether its replicates differed too: a measurement without them would be unprovable, not identical.
        detail = result_detail(done["stdout"], int(key)) if kind in ("RECONSTRUCTION", "RELEASED_DATA") else []
        st.setdefault("detail", []).extend(detail)
        st.setdefault("seed_seconds", {})[key] = done.get("seconds") or 0
        st["seed"] += 1
        st.setdefault("done_seeds", []).append(int(key))
        state.append_jsonl(cdir / "seeds.jsonl", {"key": _ckpt_key(check), "seed": int(key), "values": vs,
                                                   "cert": rows, "staged": staged, "seconds": done.get("seconds"),
                                                   "error": st.get("failed_seeds", {}).get(key, ""),
                                                   "stage_errors": failed_stages, "units": sorted(declared),
                                                   "schema": defects, "cohort_mismatch": mism, "detail": detail})
        if "pilot_s" not in st:                           # the first completed run is the pilot
            st["pilot_s"] = done.get("seconds") or 0
            if (why := _over_budget(cfg, check, st, runs)):
                return _finish(cfg, root, check, {**_cancel(root, st), "blocker": why})
            st["budget_s"] = budget(cfg, check)[0]       # the budget the projection was admitted under
    _sample_memory(st, fly)
    if st.get("reused") and not st.get("budget_checked") and st["seed"] < runs:   # a pilot from a checkpoint
        st["budget_checked"] = True
        if (why := _over_budget(cfg, check, st, runs)):
            return _finish(cfg, root, check, {**_cancel(root, st), "blocker": why})
        st["budget_s"] = budget(cfg, check)[0]
    if st["stage"] == "run" and st["seed"] >= runs and not fly:
        if not (more := _extension(cfg, check, st, runs)):
            return _finish(cfg, root, check, st)
        st["runs_extended"], runs = more, more          # independent replicates repeated one value: only more can decide
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
    if todo and _storage_changed(st, env_dir, root, check):
        # Docker's storage was reset under a running check (a volume is gone, or its environment
        # or data is being rebuilt under the same name): every in-flight step is recorded as
        # vanished, the check waits for the rebuild, and the ended seeds are reused.
        for rec in fly.values():
            state.append_jsonl(root / "execution.jsonl", {**rec, "returncode": None, "timed_out": False, "stdout": "",
                                                           "stderr": "", "ended_at": state.now(), "seconds": 0,
                                                           "error": "the container disappeared before it was collected "
                                                                    "(Docker's volumes were gone)"})
        state.write_json(cdir / "exec.json", {"token": st.get("token", ""), "storage_lost_at": state.now()})
        return True
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


def _storage_changed(st: dict, env_dir: Path, root: Path, check: dict) -> bool:
    """Is what this running check started on still there, unchanged? Its env marker must still be
    the one it started with (a rebuild sets it aside first), its data must not be re-acquiring,
    and Docker must still hold every volume it mounts."""
    now = state.read_json(env_dir / "referee-env.json") or {}
    if not str(st["env"].get("detail", "")).startswith(str(now.get("detail") or "\0")):
        return True                  # script_env may append to the detail, never change it
    if check.get("acquire") and "n_files" not in (state.read_json(root / "checks" / check["id"] / "data.json") or {}):
        return True
    return any(_volume_gone(v) for v in [st["env"].get("volume") or volume(env_dir), st["env"].get("base")]
               + [m[0] for m in data_mount(root, check["id"])] if v)


def resource_action(done: dict, st: dict, key: str, timeout: int) -> tuple[str, str]:
    """What a run that hit a resource limit leads to: ("retry", "") once alone after an
    out-of-memory kill that may have shared memory, or once for a replicate past the per-run limit
    whose pilot took under a quarter of it (a stalled host); ("blocker", why) when the same failure
    would repeat (killed out of memory alone, or past the per-run limit); ("", "") otherwise."""
    if done.get("mode") != "evidence":
        return "", ""
    err = (done.get("stderr") or "").lower()
    if "no space left on device" in err or "disk quota exceeded" in err:
        return "blocker", ("storage", "a run ran out of disk space on this host (a measured limit); the protocol "
                           "is not shortened, so it is not repeated.")
    if "cuda out of memory" in err or "outofmemoryerror" in err:
        if not st.get("vram_retry"):
            st.update(width=1, vram_retry=True)          # another run may have shared the GPU: once, alone
            return "retry", ""
        return "blocker", ("vram", f"a single run exhausted the GPU's memory even when running alone "
                           f"(this host's GPU memory: {_gpu_mb()} MB, a measured hardware limit); it is not "
                           "repeated and never downscaled.")
    if done.get("error") == "out of memory":
        if not st.get("oom_retry"):
            st.update(width=1, oom_retry=True)
            return "retry", ""
        return "blocker", ("memory", f"a single run was killed out of memory after {done.get('seconds', 0):.0f}s even "
                           f"when running alone (peak observed {st.get('peak_mb', {}).get(key, '?')} MB; this host's "
                           f"Docker VM has {_vm_mb()} MB, a measured hardware limit). The same run fails the same way, "
                           "so it is not repeated; it needs a host with more memory.")
    if done.get("timed_out"):
        per = st.setdefault("stalls_by_seed", {})
        if 0 < st.get("pilot_s", 0) * 4 < timeout and not per.get(key):
            # The timed pilot of this same script took under a quarter of the limit: this replicate
            # met a stalled host (a hung daemon, a throttled GPU), not a long protocol. Once more.
            per[key] = 1
            return "retry", ""
        return "blocker", ("per_run_timeout", f"a single run exceeded the configured per-run limit of {timeout}s "
                           "(SH_RUN_TIMEOUT_S, a setting of this run); the protocol is not shortened, so it is not "
                           "repeated.")
    return "", ""


def budget(cfg: state.Config, check: dict) -> tuple[int, str]:
    """(seconds, setting) a check's runs may take: an engineering compatibility test has its own,
    smaller budget; every other check the per-check budget."""
    return ((cfg.compat_budget_s, "SH_COMPAT_BUDGET_S") if check.get("test") == "compatibility" else
            (cfg.check_budget_s, "SH_CHECK_BUDGET_S"))


def _projected(cfg: state.Config, check: dict, st: dict, runs: int, spent: float = 0.0) -> tuple[float, int, str, int]:
    """(seconds needed, budget, its setting, runs at a time): the time already `spent` plus the timed
    pilot's projection of the remaining runs."""
    w = 1 if check["kind"] == "AUTHOR_CODE" else max(1, min(cfg.parallel, st.get("width", cfg.parallel)))
    limit, setting = budget(cfg, check)
    return spent + st.get("pilot_s", 0) * (runs - st["seed"]) / w, limit, setting, w


def _spent(st: dict) -> float:
    """The seconds the check's completed seeds took (each seed's own run time; the pilot's where unrecorded)."""
    secs, pilot = st.get("seed_seconds") or {}, st.get("pilot_s", 0)
    return sum(v or pilot for v in secs.values()) + pilot * max(0, st.get("seed", 0) - len(secs))


def _over_budget(cfg: state.Config, check: dict, st: dict, runs: int) -> str:
    """The documented blocker when the timed pilot projects the remaining runs past the budget."""
    need, limit, setting, w = _projected(cfg, check, st, runs)
    if need <= limit:
        return ""
    fmt = lambda sec: f"{sec / 3600:.1f} h" if sec >= 3600 else f"{sec / 60:.0f} min" if sec >= 60 else f"{sec:.0f} s"
    peak = max((st.get("peak_mb") or {}).values(), default=None)
    has_gpu = check["kind"] in ("AUTHOR_CODE", "RECONSTRUCTION") and gpu(cfg)
    return ("time_budget", f"the {runs} runs need about {fmt(need)} more at the pilot's measured {fmt(st['pilot_s'])} "
            f"per run ({w} at a time; peak memory {peak or '?'} MB of {_vm_mb()} MB; GPU available to the container: "
            f"{'yes' if has_gpu else 'no'}); the configured time budget for this check is {fmt(limit)} "
            f"({setting}, a setting of this run, not a hardware limit). The run count is not reduced: the "
            "completed run(s) are recorded as a pilot and decide nothing.")


def _wants(x) -> bool:
    """Does a reconciled result (or any stage or reading inside it) ask for more replicates?"""
    if isinstance(x, dict):
        return bool(x.get("needs_replicates")) or any(_wants(v) for v in x.values() if isinstance(v, (dict, list)))
    return isinstance(x, list) and any(_wants(v) for v in x)


def _extension(cfg: state.Config, check: dict, st: dict, runs: int) -> int:
    """The replicate count to extend a finished stochastic experiment to, or 0. Independent replicates that
    repeat one value (a recovery ratio of 1.0, no false alarm) are decided by an exact sign test, which reaches
    95% only from SIGN_MIN of them; a margin within t*SE at fewer than SIGN_MIN replicates is wide mostly because
    t(2)=4.3: when only more replicates stand between the check and a decision, and the time its seeds already
    took plus the extra runs fit the check's time budget, the harness runs them (once, never fewer than planned,
    never a downscale, and whatever direction the result leans). The protocol records the extension and why."""
    from .reconcile import SIGN_MIN
    if (check["kind"] != "RECONSTRUCTION" or check.get("test") == "compatibility" or check.get("stochastic") is False
            or st.get("runs_extended") or runs >= SIGN_MIN or st.get("failed_seeds")):
        return 0
    if not _wants(_reconciled(check, st, True, "", failure="")):
        return 0
    need, limit, _, _ = _projected(cfg, check, st, SIGN_MIN, spent=_spent(st))
    return SIGN_MIN if need <= limit else 0


def _reconciled(check: dict, st: dict, authorized: bool, why: str, failure: str | None = None) -> dict:
    from .reconcile import reconcile
    kind = check["kind"]
    return reconcile(kind, check.get("printed", ""), st.get("values", []), st.get("failure", "") if failure is None else failure,
                     st.get("ev", {}), bool(st.get("seeded")) or (kind == "RECONSTRUCTION" and st.get("seed", 0) > 1),
                     authorized, why, (check.get("target") or {}).get("relation", ""),
                     cert=st.get("cert") if kind == "CERTIFICATE" and st.get("cert") else None,
                     changed=any(d.get("changes_claim") for d in check.get("deviations") or []),
                     step=bool(check.get("step")), staged=st.get("staged"),
                     failed=st.get("failed_seeds"), stage_errors=st.get("stage_errors"),
                     readings=[r["name"] for r in check.get("readings") or []],
                     cohort_mismatch=st.get("cohort_mismatch"),
                     deterministic=kind == "RELEASED_DATA" or check.get("stochastic") is False,
                     test=check.get("test", ""), detail=st.get("detail"), rng=check.get("seed_flow"),
                     metric=check.get("metric", ""))


def _stage_summary(staged: list) -> dict:
    """Per stage: how many results and their mean (a pilot's measurements, deciding nothing)."""
    out: dict = {}
    for e in staged:          # a reading, where one is named, is kept apart like a stage
        out.setdefault((e[0] or "(all)") + (f" [{e[2]}]" if len(e) > 2 else ""), []).append(e[1])
    return {s: {"n": len(vs), "mean": round(sum(vs) / len(vs), 6)} for s, vs in out.items()}


def _ckpt_key(check: dict) -> str:
    return check.get("script_sha256") or f"{check.get('command', '')}|{check.get('seed_flag', '')}"


def _checkpoints(cdir: Path, check: dict) -> list[dict]:
    """Seeds of this exact approved script (or documented command) that already completed."""
    f, key, seen, out = cdir / "seeds.jsonl", _ckpt_key(check), set(), []
    for row in map(json.loads, f.read_text(encoding="utf-8").splitlines() if f.exists() else []):
        if row.get("key") == key and row["seed"] not in seen and row["seed"] < max(int(check.get("runs") or 1), 6):   # 6: SIGN_MIN
            seen.add(row["seed"])
            out.append(row)
    return out


def _seed_record(log: list[dict], check: dict, seed: int) -> dict | None:
    """The last evidence run of this exact approved script for `seed` that printed result lines."""
    return next((r for r in reversed(log) if r.get("target") == check.get("id") and r.get("mode") == "evidence"
                 and r.get("script_sha256") == check.get("script_sha256") and int(r.get("seed") or 0) == seed
                 and "REFEREE_RESULT " in (r.get("stdout") or "")), None)


def reuse_checkpoints(root: Path, cdir: Path, check: dict, st: dict, fresh: bool = False) -> list[dict]:
    """Fold the ended seeds of this exact script/command (seeds.jsonl) into a check's state, as a new
    poll reuses them and as tools/replay.py re-decides them — one path, so the two cannot drift. A row
    written before per-run `detail` existed gets it rebuilt from that seed's own stdout in
    execution.jsonl (same script sha, same seed); failing that, its stages carry a `missing` marker:
    evidence missing, never identical runs. `fresh` (replay) re-derives the result schema and cohort
    comparison from the recorded stdout under the current rules."""
    rows, scripted = _checkpoints(cdir, check), check["kind"] in ("RECONSTRUCTION", "RELEASED_DATA")
    want = [r for r in rows if scripted and (fresh or (r.get("staged") and not r.get("detail")))]
    f = Path(root) / "execution.jsonl"
    log = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if want and f.exists() else []
    for k, v in (("values", []), ("cert", []), ("staged", []), ("detail", []), ("done_seeds", []), ("failed_seeds", {})):
        st.setdefault(k, v)
    for row in rows:
        st["values"] += row["values"]
        st["cert"] += row.get("cert") or []
        st["staged"] += row.get("staged") or []
        rec = _seed_record(log, check, int(row["seed"])) if row in want else None
        detail = result_detail(rec.get("stdout") or "", int(row["seed"])) if rec else (row.get("detail") or [])
        if not detail and row.get("staged") and scripted:
            detail = [{"seed": int(row["seed"]), "stage": e[0], "reading": e[2] if len(e) > 2 else "", "out": {},
                       "trials": {}, "data_fp": None, "missing": True}
                      for e in {(e[0], e[2] if len(e) > 2 else ""): e for e in row["staged"]}.values()]
        st["detail"] += detail
        st["done_seeds"].append(row["seed"])
        st.setdefault("seed_seconds", {})[str(row["seed"])] = row.get("seconds") or 0
        if row.get("error"):
            st["failed_seeds"][str(row["seed"])] = row["error"]
        st["ok_runs"] = st.get("ok_runs", 0) + (not row.get("error"))
        st["units"] = sorted(set(st.get("units") or []) | set(row.get("units") or []))
        # A reused seed follows the same rule as a new one: a completed run's unit printed under
        # another name is a labeling defect, not an incomplete stage.
        errs, defects = split_units(set(row.get("units") or []), row.get("staged") or [], bool(row.get("error")), "")
        for s in errs:                                  # only a declared unit can be incomplete
            if s in (row.get("stage_errors") or {}):
                st.setdefault("stage_errors", {}).setdefault(s, row["stage_errors"][s])
        schema, mism = set(row.get("schema") or []), set(row.get("cohort_mismatch") or [])
        if fresh and rec and not row.get("error"):
            schema = set(result_schema(rec, check))
            mism = set(cohort_mismatch(rec.get("stdout") or "", check)) if check.get("readings") else set()
        st["schema_defects"] = sorted(set(st.get("schema_defects") or []) | set(defects) | schema)[:40]   # ponytail: 40 kept
        st["cohort_mismatch"] = sorted(set(st.get("cohort_mismatch") or []) | mism)
        st.setdefault("pilot_s", row.get("seconds") or 0)
    st["seed"], st["reused"] = len(st["done_seeds"]), len(st["done_seeds"])
    return rows


def ckpt_volume(cdir: Path, seed: int) -> str:
    """A per-seed scratch volume mounted at /work/ckpt: a long run may save progress there and
    resume from it after an infrastructure restart (it never holds evidence)."""
    return _cname("ckpt", Path(cdir).resolve(), seed)


def _gpu_mb() -> str:
    rc, out = _docker(["docker", "run", "--rm", "--gpus", "all", DEFAULT_IMAGE, "sh", "-c",
                       "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits"], 120)
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else "?"


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
    planned = int(check.get("runs") or 1)
    runs, rel = max(planned, int(st.get("runs_extended") or 0)), (check.get("target") or {}).get("relation", "")
    src = check.get("runs_source") or ("paper" if check.get("runs_quote") else "referee")
    devs = check.get("deviations") or []
    return {"runs": runs, "runs_from": {"paper": f"the paper: {check.get('runs_quote', '')!r}",
                                        "referee_floor": "REFEREE: at least SH_REPLICATES seeded replicates",
                                        "referee": "REFEREE: the paper states no run count",
                                        "deterministic": "REFEREE: a deterministic computation runs once (released "
                                                         "files) or twice (a pipeline: the second run must repeat the "
                                                         "first); reruns are never replicates",
                                        "compatibility": "REFEREE: an engineering compatibility test, SH_REPLICATES "
                                                         "runs each required to meet its condition (the paper's run "
                                                         "count belongs to its performance experiments)"}.get(src, src),
            "seeds": "0..n-1 passed as --seed (REFEREE)" if check["kind"] != "AUTHOR_CODE"
            else (f"the documented {check.get('seed_flag')} flag (REFEREE varies only its value)"
                  if check.get("seed_flag") else "none (one documented run)"),
            "decision_rule": rule,
            **({"relation": f"{rel} (written by REFEREE's planner for the quoted sentence)"} if rel else {}),
            "supplied_by_referee": [d["used"] for d in devs if not d.get("printed") and not d.get("changes_claim")],
            "claim_changes": [d["used"] for d in devs if d.get("changes_claim")],
            **({"replicates_extended": f"from {planned} to {runs}: the {planned} independent replicates left the result "
                f"inside their noise band (a repeated value awaiting the exact sign test, or a margin within t*SE), which "
                f"only more replicates can narrow; extended once, to the {runs} the sign test needs"} if runs > planned else {}),
            **({"pilot_seconds": st["pilot_s"]} if st.get("pilot_s") else {}),
            **({"admitted_under_check_budget_s": st["budget_s"]} if st.get("budget_s") else {}),
            **({"seeds_reused_from_checkpoints": st["reused"]} if st.get("reused") else {})}


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
    kind, src = check["kind"], state.read_json(root / "source.json", {})
    authorized = st.get("authorized", True)
    why = st.get("why", "")
    outcome = {"check": check["id"], "kind": kind, "evidence": EVIDENCE.get(kind, "NONE"), "authorized": authorized,
               "authorization": why, "commit": src.get("commit", "") if (root / "repo" / ".git").is_dir() else ""}
    if st.get("blocker"):         # a documented final blocker of this host: nothing about the paper is established
        kind_of, text = st["blocker"] if isinstance(st["blocker"], (list, tuple)) else ("time_budget", st["blocker"])
        outcome.update(status="BLOCKED", reason=f"RESOURCE BLOCKER: {text}", rule="bounded pilot and resources",
                       resource=kind_of, values=[], pilot_values=st.get("values", []),
                       pilot_stages=_stage_summary(st.get("staged") or []), runs=st.get("records", 0) + st.get("reused", 0),
                       records="execution.jsonl", finished_at=state.now())
    else:
        outcome.update(_reconciled(check, st, authorized, why),
                       values=st.get("values", []), runs=st.get("records", 0) + st.get("reused", 0), records="execution.jsonl",
                       finished_at=state.now())
    outcome["protocol"] = protocol(check, st, outcome.get("rule", ""))
    # How the runs went (execution), apart from what their results say (the status above).
    outcome["execution"] = {"runs_planned": max(int(check.get("runs") or 1), int(st.get("runs_extended") or 0)),
                            "runs_ended": st.get("records", 0) + st.get("reused", 0),
                            "runs_exited_ok": st.get("ok_runs", 0), "runs_failed": sorted((st.get("failed_seeds") or {}),
                                                                                           key=int),
                            "schema_defects": st.get("schema_defects") or []}
    for k in ("test", "basis", "stochastic"):
        if check.get(k) not in (None, ""):
            outcome[k] = check[k]
    if check.get("readings"):     # the definitions; `readings` (from reconcile) holds each one's result
        outcome["reading_defs"] = [{"name": r["name"], "source": r["source"]} for r in check["readings"]]
    if st.get("staged") and not outcome.get("stages") and outcome["status"] != "BLOCKED":
        # Ended before its stages were decided (the host, the operator): what completed is kept per
        # stage, never pooled across stages, and decides nothing.
        outcome["completed_stages"] = _stage_summary(st["staged"])
    if st.get("reused"):          # seeds that ended earlier (seeds.jsonl) count as runs; which ones is said
        outcome["reused_seeds"] = st["reused"]
    for k in ("data_identity", "stage_times", "peak_mb"):
        if st.get(k):
            outcome[k] = st[k]
    if (d := state.read_json(root / "checks" / check["id"] / "data.json")):
        outcome["data"] = {k: d.get(k) for k in ("sources", "n_files", "bytes", "volume")}
    if st.get("env"):
        outcome["environment"] = {k: st["env"].get(k) for k in ("detail", "image", "builder", "recovery")}
        if st.get("image") and st["image"] != st["env"].get("image"):
            outcome["environment"]["run_image"] = st["image"]   # given the GPU (run_image)
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


def try_script(cfg: state.Config, pid: str, cid: str, script: str, c: dict) -> dict:
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
    scratch = _cname("try-ckpt", tdir.resolve(), time.time())        # a throwaway /work/ckpt for the draft
    em, env = env_mounts(env_dir, envinfo)
    mounts = [(tdir, f"{MOUNT}/check", False)] + em + [(scratch, f"{MOUNT}/ckpt", False)] + (
        [(root / "repo", f"{MOUNT}/repo", True)] if has_repo else []) + data_mount(root, cid)
    gpus = c.get("kind") == "RECONSTRUCTION" and gpu(cfg)
    rec = run(["/env/bin/python", f"{MOUNT}/check/script.py", "--seed", "0"], mounts=mounts, env=env,
              workdir=f"{MOUNT}/repo" if has_repo else f"{MOUNT}/check",
              image=run_image(envinfo.get("image", DEFAULT_IMAGE), gpus), network=False, timeout=cfg.try_timeout_s,
              mode="try", target=cid, gpus=gpus, meta={"script_sha256": state.sha256(script)})
    _docker(["docker", "volume", "rm", "-f", scratch], 60)
    state.append_jsonl(root / "execution.jsonl", rec)
    return {"environment": envinfo["detail"], "returncode": rec.get("returncode"), "timed_out": rec.get("timed_out"),
            "error": rec.get("error", ""), "stdout": mask(rec.get("stdout") or "")[-4000:],
            "stderr": mask(rec.get("stderr") or "")[-4000:]}


def mask(s: str) -> str:
    """Draft output with every result line hidden, in either stream."""
    return re.sub(r"(?m)^.*REFEREE_RESULT.*$", "REFEREE_RESULT <masked>", s or "")
