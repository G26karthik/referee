"""S3a — find the paper's code, get it, and work out how to run it.

Everything here is gated. Cloning a repository and resolving its dependencies means
fetching code from the internet and, later, executing it; that is a different risk
class from every other stage in this harness, which is otherwise fully offline. So
`Config.allow_network` and `Config.allow_install` both default to False and each
function returns a `blocked` status rather than doing anything when its gate is shut.
A blocked acquisition is not a finding about the paper — it is a fact about this run,
and the report says so.

URL extraction is deliberately conservative. PDF text mangles links in ways that are
easy to see once you look at real output:

    "Code is available: https: //github.com/wuyang98/weathergen"   space after the colon
    "code is available here: https://github.com/HanxunH/LDReg."    sentence-final period
    "project is available at https: //github.com/.../finchain.git." both, plus .git
    "2https://github.com/facebookresearch/vissl"                   a FOOTNOTE to someone
                                                                   else's code

The last one is why proximity to an availability cue is scored rather than taking the
first match: a paper's related-work footnotes routinely point at other people's
repositories, and cloning one of those would audit the wrong project entirely.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from .artifacts import PaperDoc, RepoAcquisition
from .config import Config

# Hosts worth cloning. Anything else (project pages, personal sites) is not a repo.
_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "huggingface.co")
_URL = re.compile(
    r"https?\s*:\s*/\s*/\s*(?:www\.)?(" + "|".join(re.escape(h) for h in _HOSTS) + r")"
    r"\s*/\s*([A-Za-z0-9_.\-]+)\s*/\s*([A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)
# Phrases that mark a link as THIS paper's code rather than a citation to someone else's.
_CUES = (
    "code is available", "code are available", "code available", "code will be",
    "code and models", "code and data", "project is available", "project page",
    "open source code", "our code", "our implementation", "implementation is available",
    "source code", "codebase is", "released at", "available at", "available here", "code:",
)
_CUE_WINDOW = 140          # chars before the URL searched for a cue
_DEP_FILES = ("requirements.txt", "environment.yml", "environment.yaml", "pyproject.toml",
              "setup.py", "setup.cfg", "requirements-dev.txt", "requirements/base.txt")
# Import name -> the framework label the report shows. Order is significance, not preference.
_FRAMEWORKS = {
    "torch": "pytorch", "pytorch": "pytorch", "torchvision": "pytorch", "pytorch-lightning": "pytorch",
    "lightning": "pytorch", "tensorflow": "tensorflow", "tensorflow-gpu": "tensorflow",
    "keras": "tensorflow", "jax": "jax", "jaxlib": "jax", "flax": "jax", "optax": "jax",
    "transformers": "huggingface", "diffusers": "huggingface", "datasets": "huggingface",
    "accelerate": "huggingface", "scikit-learn": "sklearn", "sklearn": "sklearn",
    "sympy": "sympy", "numpy": "numpy", "scipy": "scipy", "pandas": "pandas",
    "xgboost": "xgboost", "lightgbm": "lightgbm", "mamba-ssm": "pytorch", "timm": "pytorch",
}
# Scripts a reviewer would try first, best candidate first.
_ENTRY_CANDIDATES = (
    "eval.py", "evaluate.py", "evaluation.py", "test.py", "inference.py", "infer.py",
    "predict.py", "demo.py", "main.py", "run.py", "train.py",
)
_ENTRY_DIRS = ("", "scripts", "tools", "src", "experiments")


# --------------------------------------------------------------------------- #
# URL extraction
# --------------------------------------------------------------------------- #
def _normalize(host: str, owner: str, name: str) -> str:
    """Rebuild a canonical clone URL from the three captured pieces.

    The pieces are captured separately precisely so the whitespace PDF extraction
    injects (`https: //github .com / owner / repo`) never reaches the output.
    """
    name = name.rstrip(".,;:)]}")               # sentence punctuation glued to the path
    if name.lower().endswith(".git"):
        name = name[:-4]
    owner = owner.strip(".,;:)]}")
    if not owner or not name:
        return ""
    return f"https://{host.lower()}/{owner}/{name}"


def _score(text: str, start: int, host: str, url: str) -> int:
    """How likely is this the paper's OWN code? Higher is better."""
    window = text[max(0, start - _CUE_WINDOW):start].lower()
    score = 0
    if any(c in window for c in _CUES):
        score += 3
    if host.lower() == "github.com":
        score += 1
    # "2https://github.com/facebookresearch/vissl" — a footnote marker fused to the
    # URL is the signature of a citation to someone else's repository.
    if start > 0 and text[start - 1].isdigit():
        score -= 2
    if "/blob/" in url or "/tree/" in url:
        score -= 1
    return score


def find_repo_urls(doc: PaperDoc) -> list[str]:
    """Every candidate repository URL in the paper, most-likely-official first."""
    text = "\n".join(s.text for s in doc.sections)
    best: dict[str, tuple[int, int]] = {}
    for m in _URL.finditer(text):
        url = _normalize(m.group(1), m.group(2), m.group(3))
        if not url:
            continue
        rank = (_score(text, m.start(), m.group(1), m.group(0)), -m.start())
        if url not in best or rank > best[url]:
            best[url] = rank
    return [u for u, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True)]


# --------------------------------------------------------------------------- #
# Dependency inspection — pure parsing, no network, always safe to run
# --------------------------------------------------------------------------- #
_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def _parse_requirements(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):        # -r nested, -e editable, --index-url
            continue
        if m := _REQ_LINE.match(line):
            out.append(m.group(1).lower())
    return out


def _parse_environment_yml(text: str) -> list[str]:
    """Minimal conda parse: the `dependencies:` list plus its nested `pip:` block.

    Deliberately not a YAML dependency — this only needs the package names, and the
    harness must not grow a parser it cannot justify for one file.
    """
    out, in_deps = [], False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^dependencies\s*:", stripped):
            in_deps = True
            continue
        if in_deps and re.match(r"^[A-Za-z_]+\s*:", stripped) and not stripped.startswith("- "):
            break                                    # a new top-level key ends the list
        if in_deps and stripped.startswith("-"):
            item = stripped.lstrip("- ").strip()
            if item.endswith(":"):                   # the `pip:` sub-block header
                continue
            if m := _REQ_LINE.match(item):
                out.append(m.group(1).lower())
    return out


def _parse_pyproject(raw: bytes) -> list[str]:
    try:
        data = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, UnicodeError):
        return []
    out = []
    for dep in (data.get("project") or {}).get("dependencies") or []:
        if m := _REQ_LINE.match(str(dep)):
            out.append(m.group(1).lower())
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    out += [k.lower() for k in poetry if k.lower() != "python"]
    return out


def inspect_dependencies(repo: Path) -> tuple[list[str], list[str], list[str]]:
    """(dependency files found, package names, framework labels). Never executes anything."""
    files, deps = [], []
    for rel in _DEP_FILES:
        path = repo / rel
        if not path.is_file():
            continue
        files.append(rel)
        try:
            if rel.endswith(".toml"):
                deps += _parse_pyproject(path.read_bytes())
            elif rel.endswith((".yml", ".yaml")):
                deps += _parse_environment_yml(path.read_text(encoding="utf-8", errors="replace"))
            elif rel.endswith(".txt"):
                deps += _parse_requirements(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    seen, ordered = set(), []
    for d in deps:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    frameworks = []
    for d in ordered:
        fw = _FRAMEWORKS.get(d)
        if fw and fw not in frameworks:
            frameworks.append(fw)
    return files, ordered, frameworks


def find_entrypoint(repo: Path) -> str:
    """A repo-relative evaluation script a reviewer would plausibly run first."""
    for directory in _ENTRY_DIRS:
        for name in _ENTRY_CANDIDATES:
            cand = repo / directory / name if directory else repo / name
            if cand.is_file():
                return str(cand.relative_to(repo)).replace("\\", "/")
    return ""


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #
def _git(args: list[str], cwd: Path | None, timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def acquire(cfg: Config, root: Path, pid: str, doc: PaperDoc) -> RepoAcquisition:
    """Clone the paper's repository into `runs/<pid>/repo`, if we are allowed to.

    Returns a populated `RepoAcquisition` in every path, including the ones where
    nothing happened, because "we did not look" and "we looked and there is nothing"
    are different facts and the report has to be able to tell them apart.
    """
    urls = [doc.repo_url] if doc.repo_url else []
    urls += [u for u in (doc.repo_urls or []) if u not in urls]
    if not urls:
        urls = find_repo_urls(doc)
    url = urls[0] if urls else ""
    dest = root / "runs" / pid / "repo"

    if not url:
        return RepoAcquisition(status="unavailable",
                               reason="the paper advertises no repository URL in its text")
    if dest.exists() and (dest / ".git").exists():
        acq = RepoAcquisition(url=url, status="cached", path=str(dest),
                              reason="clone already present; not re-fetched")
        head = _git(["rev-parse", "HEAD"], dest, 30)
        acq.commit = (head.stdout or "").strip()[:12] if head.returncode == 0 else ""
    elif not cfg.allow_network:
        return RepoAcquisition(
            url=url, status="blocked",
            reason="network gate closed (set SH_ALLOW_NETWORK=1 to clone). Nothing was "
                   "fetched, so nothing here is evidence about the paper's code.")
    elif shutil.which("git") is None:
        return RepoAcquisition(url=url, status="failed", reason="git is not on PATH")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            p = _git(["clone", "--depth", "1", "--recurse-submodules", url, str(dest)],
                     None, cfg.clone_timeout_s)
        except subprocess.TimeoutExpired:
            return RepoAcquisition(url=url, status="failed",
                                   reason=f"clone timed out after {cfg.clone_timeout_s}s")
        if p.returncode != 0:
            return RepoAcquisition(
                url=url, status="failed",
                reason=f"git clone exited {p.returncode}: {(p.stderr or '').strip()[-300:]}")
        acq = RepoAcquisition(url=url, status="cloned", path=str(dest))
        head = _git(["rev-parse", "HEAD"], dest, 30)
        acq.commit = (head.stdout or "").strip()[:12] if head.returncode == 0 else ""

    acq.dependency_files, acq.dependencies, acq.frameworks = inspect_dependencies(dest)
    acq.entrypoint = find_entrypoint(dest)
    return acq


# --------------------------------------------------------------------------- #
# Isolated environment
# --------------------------------------------------------------------------- #
def _venv_python(env_dir: Path) -> Path:
    return env_dir / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python")


def build_env(cfg: Config, root: Path, pid: str, acq: RepoAcquisition) -> RepoAcquisition:
    """Create `runs/<pid>/env` and install the repo's declared stack into it.

    Installing is the second gate, separate from cloning on purpose: reading a
    repository's dependency list is useful on its own, and is safe, whereas resolving
    it runs arbitrary setup code from the index.
    """
    if acq.status not in ("cloned", "cached") or not acq.path:
        acq.env_status = "not_attempted"
        return acq
    if not (cfg.allow_install and cfg.allow_network):
        acq.env_status = "blocked"
        return acq

    env_dir = root / "runs" / pid / "env"
    py = _venv_python(env_dir)
    if not py.exists():
        try:
            subprocess.run([cfg.python, "-m", "venv", str(env_dir)], capture_output=True,
                           text=True, timeout=600, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            acq.env_status, acq.reason = "failed", f"venv creation failed: {e}"
            return acq

    installed = False
    for rel in acq.dependency_files:
        if not rel.endswith(".txt"):
            continue                                  # only pip requirement files are installable here
        try:
            p = subprocess.run([str(py), "-m", "pip", "install", "-r",
                                str(Path(acq.path) / rel)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=cfg.install_timeout_s)
        except subprocess.TimeoutExpired:
            acq.env_status = "failed"
            acq.reason = f"pip install -r {rel} timed out after {cfg.install_timeout_s}s"
            return acq
        if p.returncode != 0:
            acq.env_status = "failed"
            acq.reason = f"pip install -r {rel} exited {p.returncode}: {(p.stderr or '').strip()[-300:]}"
            return acq
        installed = True
    acq.env_path = str(py)
    acq.env_status = "ready" if installed else "ready"
    if not installed:
        acq.reason = "no pip requirements file to install; the bare venv is what ran"
    return acq


# --------------------------------------------------------------------------- #
# Standalone fallback when the paper ships no code
# --------------------------------------------------------------------------- #
STANDALONE_TEMPLATE = '''\
"""Standalone verification probe — __PID__.

The paper advertises no runnable repository, so there is nothing to clone. This file
is a scaffold for reproducing the paper's stated formulation directly, and AS
GENERATED IT REPRODUCES NOTHING: `compute(seed)` returns a constant, so the harness
will report it as a degenerate probe rather than as evidence about the paper.

Claim this probe is meant to settle:
    __CLAIM__

Cell the result will be reconciled against:
    __CELL__ = __CELL_VALUE__

To make it real, implement `compute(seed)` from the paper's own equations — the maths
is in the sections the audit lenses quoted — and return the metric the cited cell
reports, on the same scale. Anything the paper does not state is a modelling choice
you must not silently invent; leave it out and say so, rather than tuning until the
number matches.

Contract (parsed by harness/local_exec.py — keep both lines):
    SH_DEVICE <cuda|mps|cpu>
    SH_METRIC arm=<name> seed=<int> value=<float>
"""
import argparse

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, required=True)
ap.add_argument("--arm", default="reproduction")
args = ap.parse_args()

try:
    import torch
    DEVICE = ("cuda" if torch.cuda.is_available()
              else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
              else "cpu")
except ImportError:
    DEVICE = "cpu"
print(f"SH_DEVICE {DEVICE}", flush=True)


def compute(seed: int) -> float:
    """TODO(driver): implement the paper's formulation and return its metric."""
    raise NotImplementedError(
        "standalone_probe.py is a scaffold: implement compute() from the paper's "
        "equations before treating its output as a reproduction."
    )


print(f"SH_METRIC arm={args.arm} seed={args.seed} value={compute(args.seed):.6f}", flush=True)
'''


def synthesize_standalone(root: Path, pid: str, claim: str, cell_ref: str,
                          cell_value: str) -> RepoAcquisition:
    """Write `runs/<pid>/standalone_probe.py` when there is no repository to clone.

    The scaffold raises rather than returning a plausible number. A file that silently
    emitted a constant would flow through reconciliation and produce a confident
    "FAILED_REPRODUCTION" verdict against a paper whose code was never run — which is
    the same class of error this whole stage exists to catch.
    """
    path = root / "runs" / pid / "standalone_probe.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = STANDALONE_TEMPLATE
    for key, value in (("__PID__", pid), ("__CLAIM__", (claim or "(none recorded)")[:400]),
                       ("__CELL__", cell_ref or "(no cell cited)"),
                       ("__CELL_VALUE__", cell_value or "(unknown)")):
        body = body.replace(key, str(value).replace('"""', "'''"))
    path.write_text(body, encoding="utf-8")
    return RepoAcquisition(
        status="synthesized", path=str(path),
        reason="no repository advertised; wrote a standalone scaffold. It raises "
               "NotImplementedError until its compute() is written, so it cannot "
               "produce a reproduction verdict on its own.",
        frameworks=["standalone"],
    )


if __name__ == "__main__":  # self-check: python -m harness.repo
    from .artifacts import Section

    d = PaperDoc(paper_id="t", sections=[Section(
        section_idx=0,
        text="Code is available: https: //github.com/wuyang98/weathergen and see "
             "2https://github.com/facebookresearch/vissl for the baseline. "
             "This project is available at https: //github.com/mbzuai-nlp/finchain.git.")])
    urls = find_repo_urls(d)
    assert urls[0] == "https://github.com/wuyang98/weathergen", urls
    assert "https://github.com/mbzuai-nlp/finchain" in urls, urls
    assert urls[-1] == "https://github.com/facebookresearch/vissl", "footnote cite must rank last"
    assert _parse_requirements("torch>=2.0  # comment\n-r other.txt\n\nnumpy==1.26\n") == \
        ["torch", "numpy"]
    assert _parse_environment_yml(
        "name: x\ndependencies:\n  - python=3.11\n  - pip:\n    - jax==0.4\nvariables:\n  A: 1\n"
    ) == ["python", "jax"]
    print("repo self-check OK")
