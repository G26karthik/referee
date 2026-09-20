"""S3b — read the paper's code without running it, looking for what an AST can prove.

This pass is pure static analysis: it parses files and never imports or executes
them, which is what makes it safe to point at an untrusted clone. It runs whether or
not the execution gates are open.

**This used to be ten pattern-matching rules across three cheat classes (baseline
crippling, data leakage, metric deviation). Nine are gone.** Measured over the four
repository papers this corpus ever ran, the ten rules produced six hits and five were
false — every one because the rules matched SUBSTRINGS of identifiers ("eval" inside
"rescaled_eval_metrics", "new_" and "transform" inside a LoRA-rank resize) and an
identifier is not a semantic category. Seven of the ten never fired on any real
repository at all. `artifact_review_driver` already does the judgment this was
trying to approximate — a full LLM read of the checkout against the paper's method
section, with every citation it makes relocated and hashed by `artifact_evidence`
before it counts for anything — so a noisy hand-written substitute for that reading
is exactly the "procedural heuristic standing in for a judgment an LLM can make
safely, once grounded" this pass removes. What remains is the one rule whose hit is
true by construction and needs no interpretation: a call to a known data-splitting
function with no seed keyword is not reproducible, whatever it means scientifically.

**Nothing here emits FATAL.** A static hit is a suspicion with a line number, not a
demonstration. FATAL in this stage is reserved for `stages/probe.py`, where a
reproduction that was actually attempted actually failed. Keeping that boundary is
what stops a grep from convicting anybody.

Every finding carries `file`, `line` and a VERBATIM `code_quote`, for the same reason
the audit lenses must quote the paper: a reviewer has to be able to open the file and
see it in seconds, and a claim that cannot be located is not a claim.

`python -m harness.code_audit <path>` runs the self-check, or audits a real directory.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from .artifacts import CodeAudit, CodeAuditFinding, RuntimeDemand

# Directories that are never the paper's own contribution. Auditing a vendored copy of
# someone else's library produces findings against the wrong authors.
_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "env", "site-packages",
              "third_party", "3rdparty", "external", "externals", "vendor", "build", "dist",
              ".tox", ".mypy_cache", ".pytest_cache", "docs", "doc"}
_MAX_BYTES = 400_000          # a generated or minified file is not hand-written cheating

_SPLITTERS = ("train_test_split", "random_split", "train_val_split", "split_data")


def _quote(lines: list[str], lineno: int) -> str:
    """The source line, verbatim except for surrounding whitespace."""
    return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""


def _rule_unseeded_split(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """An unseeded split cannot be reproduced, so neither can the number it produced."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.id if isinstance(node.func, ast.Name) else
                node.func.attr if isinstance(node.func, ast.Attribute) else "")
        if name not in _SPLITTERS:
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        if kwargs & {"random_state", "seed", "generator", "stratify"}:
            continue
        out.append(CodeAuditFinding(
            rule_id="leak-unseeded-split", category="data_leakage", severity="MINOR",
            title=f"`{name}` is called with no random_state, so the split is not reproducible"[:90],
            statement=(
                f"`{rel}` line {node.lineno} splits without pinning the randomness. Every run "
                f"draws a different split, so a reported single number cannot be reproduced "
                f"and the seed-to-seed spread is folded invisibly into it."),
            file=rel, line=node.lineno, code_quote=_quote(lines, node.lineno),
            counter_explanations=["a global seed may be set elsewhere in the process"]))
    return out


_RULES = (
    _rule_unseeded_split,
)


def audit_source(rel: str, src: str) -> list[CodeAuditFinding]:
    """Every rule against one file. Raises nothing: a file that will not parse yields []."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return []
    lines = src.splitlines()
    out: list[CodeAuditFinding] = []
    for rule in _RULES:
        try:
            out += rule(tree, lines, rel)
        except RecursionError:                         # pragma: no cover - pathological AST
            continue
    return out


# --------------------------------------------------------------------------- #
# Runtime demands — what the checkout says it NEEDS, as opposed to what it does wrong
# --------------------------------------------------------------------------- #
# These are REPORT-ONLY, and that is a finding rather than a shortcut. Six detection rules
# were designed against this repository's real declarations and then attacked; every one of
# the six came back with the same verdict, that the evidence supports an OBSERVATION and not
# a CONCLUSION. So nothing here reaches `select_for` or `authorize`, no demand blocks, and no
# demand can be the reason a reproduction is refused. What they do is put a file, a line and
# a verbatim token in front of a reader.
#
# Four of the arguments for that, kept because each one killed a rule that looked sound:
#
#   * conda `=` is fuzzy, not PEP 440 `==`. `python=3.9` means the 3.9 SERIES, and even
#     `python=3.9.19` is satisfied by any build of it. So the value is stored verbatim and
#     never normalised into a specifier — and never compared against a backend's own Python,
#     because a mismatch computed from a `conda env export` dump of one developer's machine
#     would be a refusal resting on a normalisation this module invented.
#   * `nvidia-cublas-cu11` is a payload of .so files. It installs, imports and does nothing
#     on a CPU-only host. It is a build tag; it is not the sentence "a GPU must be present",
#     so it may not produce one. For the same reason there is no CUDA-major arithmetic:
#     `cupy-cuda92` never referred to a CUDA 9.2 that existed, and `int(tag[:2])` on it
#     invents a version.
#   * co-present `cu11` and `cu12` pins are not a contradiction. In this checkout they are
#     torch 2.0.1's transitive closure and cupy-cuda12x's, two libraries with two closures.
#     A rule that read them as conflicting would report a conflict that is not there.
#   * `load_dataset("csv", data_files=...)` names a builder shipped inside the `datasets`
#     wheel. The demand it expresses is "this local path must exist", the exact opposite of
#     a download.
#
# They also go in `CodeAudit.runtime`, never in `CodeAudit.findings`. The findings list is
# the defect channel and the report renders it as "suspicions with line numbers"; a
# requirement filed there would be read as an accusation.
_DECL_FILES = ("environment.yml", "environment.yaml", "requirements.txt", "requirements-dev.txt",
               "pyproject.toml", "setup.py", "setup.cfg")

# Builders packaged inside `datasets`. Naming one is a statement about a LOCAL file.
_LOCAL_BUILDERS = {"csv", "json", "jsonl", "text", "parquet", "arrow", "pandas", "sql",
                   "imagefolder", "audiofolder", "videofolder", "webdataset", "generator"}

# Functions whose whole purpose is to fetch. Unlike `requests.get`, none of these has a
# non-network meaning, so the call site alone is the evidence.
_FETCH_FUNCS = {"hf_hub_download", "snapshot_download", "urlretrieve", "urlopen"}
_ARTIFACT_FUNCS = {"from_pretrained": "model_artifact", "load_dataset": "dataset_artifact"}

# An interpreter pin. The package token must EQUAL `python`: `python-dateutil==2.9.0.post0`
# sits in this very checkout and an unanchored match reports it as a Python version.
_CONDA_PYTHON = re.compile(r"^\s*-\s*python\s*(=|==|>=|<=|>|<|!=)\s*([\w.*]+)")
_PY_REQUIRES = re.compile(r"""(?:python_requires|requires-python)\s*[=:]\s*["']([^"']+)["']""")
# A CUDA build tag, kept verbatim. No arithmetic on the digits.
_CUDA_TAG = re.compile(r"\b(?:\+cu\d{2,3}|cupy-cuda\d+x?|nvidia-[\w-]+-cu\d{2}|/whl/cu\d{2,3})\b")
# A download COMMAND. `wget` must start a shell word, and a commented line is not a command.
_SHELL_FETCH = re.compile(r"(?:^|[\s;&|(])(wget|curl)\s")
_URL = re.compile(r"https?://[^\s\"'>)]+")
_SBATCH = re.compile(r"^#SBATCH\s+(--[\w-]+|-\w)[=\s]+(\S+)", re.M)
# An absolute POSIX path, read ONLY out of a Python string literal — never out of shell text.
# A shell regex was designed for this and abandoned: on the 172 shell files here it produced
# 68 hits of which 3 were real, slicing `/elastictuning` out of the middle of a relative
# `output/${model}/...` and turning `~/miniconda3/...` into a `/miniconda3` that exists on no
# machine. A Python string literal beginning with `/` needs no heuristic.
_ABS_PATH = re.compile(r"^/\w[\w.-]*/")


def _demand(kind: str, value: str, file: str, line: int, quote: str, note: str,
            state: str = "established") -> RuntimeDemand:
    return RuntimeDemand(kind=kind, state=state, value=value, file=file, line=line,
                         code_quote=quote.strip()[:200], note=note)


def _declared_demands(rel: str, text: str) -> list[RuntimeDemand]:
    """Python and CUDA declarations out of one dependency/environment file."""
    out, lines = [], text.splitlines()
    seen_cuda: set[str] = set()
    in_pip = False
    for i, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue                       # a comment is not a declaration
        # conda files nest a `pip:` block; a PyPI pin in there is not an interpreter pin.
        if re.match(r"^\s*-\s*pip\s*:\s*$", raw):
            in_pip = True
        elif raw and not raw[0].isspace():
            in_pip = False

        if not in_pip and (m := _CONDA_PYTHON.match(raw)):
            out.append(_demand(
                "python_version", stripped, rel, i, raw,
                f"the interpreter this environment file pins. Stored verbatim: conda '{m.group(1)}' "
                f"is fuzzy matching, not PEP 440, so '{m.group(2)}' is a series and not an exact "
                f"requirement. Not compared against any backend."))
        if m := _PY_REQUIRES.search(raw):
            out.append(_demand("python_version", m.group(1), rel, i, raw,
                               "an explicit packaging constraint on the interpreter"))
        if m := _CUDA_TAG.search(raw):
            # One demand per tag family per file. This checkout pins 24 `nvidia-*-cuNN`
            # wheels, which are two libraries' transitive closures — torch 2.0.1's and
            # cupy-cuda12x's — rather than 24 independent facts. A row each buries the two
            # informative lines under the closure that follows from them, and reading the
            # co-present cu11 and cu12 blocks as a CONFLICT would report a contradiction
            # that is not in the file. The first occurrence carries the citation.
            family = re.sub(r".*?(cu(?:da)?\d+)x?$", r"\1", m.group(0))
            if family in seen_cuda:
                continue
            seen_cuda.add(family)
            out.append(_demand(
                "cuda_runtime", m.group(0), rel, i, raw,
                "a CUDA-tagged wheel or runtime pin. It establishes that this package was built "
                "against that CUDA, and nothing about whether a GPU must be present: these "
                "wheels install and import on a CPU-only host."))
    return out


def _shell_demands(rel: str, text: str, want_sbatch: bool = False) -> list[RuntimeDemand]:
    """Downloads out of one shell file, and its scheduler block only if it can be bound.

    `want_sbatch` is False unless this file IS the experiment's entrypoint. An allocation is
    a property of one submission script: this checkout holds 74 of them asking for 32G, 40G
    or 64G and for 24 to 500 hours, so emitting all 592 directives buries every other demand
    under numbers that describe no single experiment. Unbound, they are summarised once as
    `ambiguous` instead — several candidates, and picking one would be a guess.
    """
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        if m := _SBATCH.match(raw):
            if want_sbatch:
                out.append(_demand(
                    "scheduler_allocation", f"{m.group(1)}={m.group(2)}", rel, i, raw,
                    "what the authors' own submission script for THIS experiment asked the "
                    "cluster for. An allocation ceiling the run stayed inside, not a measured "
                    "cost, so it bounds the demand from above and does not state it."))
            continue
        if raw.lstrip().startswith("#"):
            continue                       # a comment mentioning a download is not one
        if _SHELL_FETCH.search(raw) and (u := _URL.search(raw)):
            out.append(_demand(
                "external_download", u.group(0), rel, i, raw,
                "an explicit download command. Whether the CITED experiment runs this file is a "
                "separate question that `scope` answers, and often cannot."))
    return out


def runtime_from_source(rel: str, src: str) -> list[RuntimeDemand]:
    """Artifact, fetch and absolute-path demands out of one Python file, by AST.

    Only STRING LITERALS. `from_pretrained(model_name_or_path)` cannot say which checkpoint
    it loads, and a rule that guessed from other literals in the same file offered 'right',
    'epoch' and 'max_length' as candidate checkpoints. A literal is evidence; a variable is
    not, and the honest output for a variable is nothing at all.
    """
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return []
    lines, out = src.splitlines(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        quote = _quote(lines, node.lineno)
        if name in _FETCH_FUNCS:
            out.append(_demand("external_download", name, rel, node.lineno, quote,
                               "a call whose only purpose is to fetch from a remote host"))
            continue
        kind = _ARTIFACT_FUNCS.get(name)
        if kind is None:
            continue
        arg0 = node.args[0] if node.args else None
        if not (isinstance(arg0, ast.Constant) and isinstance(arg0.value, str)):
            continue                       # a variable names no artifact
        value = arg0.value
        if kind == "dataset_artifact" and value.lower() in _LOCAL_BUILDERS:
            continue                       # names a local file reader, not a dataset
        if _ABS_PATH.match(value):
            out.append(_demand(
                "absolute_path", value, rel, node.lineno, quote,
                "an absolute POSIX path compiled into the source. It exists on the machine this "
                "was written on; nothing establishes that it exists anywhere else."))
            continue
        out.append(_demand(
            kind, value, rel, node.lineno, quote,
            f"the {'checkpoint' if kind == 'model_artifact' else 'dataset'} this line names. "
            f"Establishes IDENTITY only — not that it is present, and not that it will be "
            f"fetched: a hub id resolves from a local cache when one is warm."))
    return out


def scope_demands(demands: list[RuntimeDemand], entrypoint_file: str = "") -> None:
    """Mark, in place, which demands belong to the experiment rather than to the repository.

    `experiment` requires the evidence to sit in the file an ESTABLISHED experiment identity
    names. Everything else is `repository`, including every declaration file, because a
    dependency manifest is a property of the checkout and not of one cited cell.

    Nothing wider is attempted. Following imports to decide reachability is the analysis this
    module does not do, and guessing is worse than the honest broader label: this repository
    holds 74 submission scripts whose declared memory is 32G, 40G or 64G, so a
    repository-level number would describe no experiment that was ever run.
    """
    target = (entrypoint_file or "").replace("\\", "/").strip().lstrip("./")
    for d in demands:
        d.scope = "experiment" if target and d.file == target else "repository"


def runtime_demands(repo: Path, max_files: int = 800,
                    entrypoint: str = "") -> tuple[list[RuntimeDemand], list[str]]:
    """Every runtime demand in a checkout, plus the declaration files that were read.

    The second return value is not bookkeeping. Without it "no demand found" and "nothing was
    read" are the same empty list, and those are opposite facts about a repository.
    """
    out: list[RuntimeDemand] = []
    read: list[str] = []
    for name in _DECL_FILES:
        for path in sorted(repo.rglob(name)):
            if any(part in _SKIP_DIRS for part in path.relative_to(repo).parts[:-1]):
                continue
            rel = str(path.relative_to(repo)).replace("\\", "/")
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            read.append(rel)
            out += _declared_demands(rel, text)
    target = (entrypoint or "").replace("\\", "/").strip().lstrip("./")
    unbound: list[str] = []
    for pattern in ("*.sh", "*.sbatch"):
        for path in sorted(repo.rglob(pattern))[:max_files]:
            if any(part in _SKIP_DIRS for part in path.relative_to(repo).parts[:-1]):
                continue
            rel = str(path.relative_to(repo)).replace("\\", "/")
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            read.append(rel)
            bound = bool(target) and rel == target
            out += _shell_demands(rel, text, want_sbatch=bound)
            if not bound and _SBATCH.search(text):
                unbound.append(rel)
    # Two declaration files that disagree about the interpreter establish no interpreter.
    # A `binder/environment.yml` pinning python=3.7 next to a training `environment.yml`
    # pinning 3.9 is the common shape, and the demo pin is deliberately old — so the rows
    # keep their own citations and every one of them drops to `ambiguous`, rather than one
    # being picked. Compared as TEXT: deciding that ">=3.8" and "3.9.19" agree would need a
    # specifier solver, and conda's `=` is not PEP 440 anyway.
    pins = [d for d in out if d.kind == "python_version"]
    if len({d.value for d in pins}) > 1:
        where = ", ".join(f"{d.file}:{d.line}" for d in pins)
        for d in pins:
            d.state = "ambiguous"
            d.note = (f"this checkout declares more than one interpreter and they do not agree "
                      f"({where}), so none of them is established. Which one produced the "
                      f"published numbers is not recorded anywhere in the tree.")

    if unbound:
        # One row, not 592. `ambiguous` is exactly right here: several candidate figures
        # exist and choosing among them would be a guess dressed as a measurement.
        out.append(RuntimeDemand(
            kind="scheduler_allocation", state="ambiguous", value="",
            file=unbound[0], line=0,
            code_quote=f"{len(unbound)} submission script(s) declare #SBATCH allocations",
            note=f"{len(unbound)} script(s) here declare cluster allocations and none could be "
                 f"bound to the cited experiment. Their values disagree, so no single figure "
                 f"describes the experiment under audit; the first is cited so a reader can "
                 f"see the shape. Binding one needs an experiment identity naming its script."))
    return out, read


def _python_files(repo: Path, limit: int) -> list[Path]:
    found = []
    for path in sorted(repo.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.relative_to(repo).parts[:-1]):
            continue
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
        if len(found) >= limit:
            break
    return found


def audit_repo(repo_path: str | Path, max_files: int = 800,
               entrypoint: str = "") -> CodeAudit:
    """Static audit of a checkout. Never imports, never executes, never leaves the tree.

    `entrypoint` is the file an established experiment identity names, and it decides only
    one thing: whether a demand is labelled `experiment` scope or `repository` scope. Passing
    nothing means every demand is repository-scoped, which is the correct answer whenever the
    experiment could not be identified.
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return CodeAudit(repo_path=str(repo),
                         skipped=f"no directory at {repo} — nothing was acquired to audit")
    files = _python_files(repo, max_files)
    if not files:
        return CodeAudit(repo_path=str(repo), skipped="the checkout contains no Python files")

    audit = CodeAudit(repo_path=str(repo), files_scanned=len(files))
    for path in files:
        rel = str(path.relative_to(repo)).replace("\\", "/")
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            audit.unparseable.append(rel)
            continue
        audit.lines_scanned += src.count("\n") + 1
        try:
            ast.parse(src)
        except (SyntaxError, ValueError, RecursionError):
            audit.unparseable.append(rel)
            continue
        audit.findings += audit_source(rel, src)
        audit.runtime += runtime_from_source(rel, src)
    declared, read = runtime_demands(repo, max_files, entrypoint)
    audit.runtime += declared
    audit.declarations_scanned = read
    scope_demands(audit.runtime, entrypoint)
    for i, f in enumerate(audit.findings, start=1):
        f.finding_id = f"code-{i:02d}"
    order = {"MAJOR": 1, "MINOR": 0}
    audit.findings.sort(key=lambda f: (order.get(f.severity, 0), f.category, f.file, f.line),
                        reverse=True)
    return audit


_SELFCHECK = '''
import torch

def make_splits(dataset):
    return torch.utils.data.random_split(dataset, [8, 2])
'''

if __name__ == "__main__":  # self-check: python -m harness.code_audit [path]
    if len(sys.argv) > 1:
        result = audit_repo(sys.argv[1])
        print(f"{result.files_scanned} files, {len(result.findings)} findings"
              f"{' — ' + result.skipped if result.skipped else ''}")
        for finding in result.findings:
            print(f"  [{finding.severity}] {finding.file}:{finding.line} "
                  f"{finding.rule_id} — {finding.title}")
        raise SystemExit(0)

    hits = {f.rule_id for f in audit_source("selfcheck.py", _SELFCHECK)}
    assert hits == {"leak-unseeded-split"}, f"rule did not fire as expected: {sorted(hits)}"
    seeded = audit_source("t.py", "import torch\n"
                          "torch.utils.data.random_split(d, [8, 2], generator=g)\n")
    assert seeded == [], "a seeded split must not be flagged"
    clean = audit_source("clean.py", "def f(x):\n    return x + 1\n")
    assert clean == [], f"a clean file must produce nothing, got {clean}"
    assert audit_source("broken.py", "def (:\n") == [], "a syntax error must not raise"
    assert audit_repo("does-not-exist").skipped, "a missing checkout reports skipped, not empty"
    assert all(f.severity != "FATAL" for f in audit_source("selfcheck.py", _SELFCHECK)), \
        "static analysis must never emit FATAL — that is reserved for a failed reproduction"
    print("code_audit self-check OK")
