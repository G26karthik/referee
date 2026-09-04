"""S3b — read the paper's code without running it, looking for the three cheats.

This pass is pure static analysis: it parses files and never imports or executes
them, which is what makes it safe to point at an untrusted clone. It runs whether or
not the execution gates are open.

**Nothing here emits FATAL.** A static hit is a suspicion with a line number, not a
demonstration — `scaler.fit` before a split may be dead code, and a smaller epoch
count on a baseline arm may be the published recipe. FATAL in this stage is reserved
for `stages/probe.py`, where a reproduction that was actually attempted actually
failed. Keeping that boundary is what stops a grep from convicting anybody.

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

# Tokens that mark an identifier or branch as being about the comparison arm.
_BASELINE = ("baseline", "base_line", "vanilla", "reference", "prior", "competitor", "orig")
_OURS = ("ours", "our_", "proposed", "method", "novel", "new_", "full", "ldreg", "improved")
# Names whose value IS a training budget. Cutting one of these on the baseline arm only
# is the cheapest way to manufacture a gain.
_BUDGET = ("epoch", "epochs", "n_epochs", "num_epochs", "max_epochs", "max_steps", "steps",
           "num_steps", "iters", "iterations", "max_iter", "n_iter", "num_train_steps",
           "lr", "learning_rate", "base_lr", "init_lr", "warmup", "patience", "n_trials",
           "num_layers", "hidden_size", "width", "batch_size")
_SPLITTERS = ("train_test_split", "random_split", "train_val_split", "split_data")
_STD_METRICS = ("accuracy", "acc", "precision", "recall", "f1", "f1_score", "iou", "miou",
                "psnr", "ssim", "auc", "map", "mape", "rmse", "mse", "bleu", "rouge",
                "perplexity", "top1", "top_1", "dice")


def _has(name: str, tokens) -> bool:
    low = (name or "").lower()
    return any(t in low for t in tokens)


def _quote(lines: list[str], lineno: int) -> str:
    """The source line, verbatim except for surrounding whitespace."""
    return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""


def _num(node: ast.AST) -> float | None:
    """The numeric value of a literal, including a negated one. None if not a number."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _num(node.operand)
        return None if inner is None else -inner
    return None


def _branch_assignments(body: list[ast.stmt]) -> dict[str, tuple[float, int]]:
    """{budget name: (value, lineno)} assigned directly in this branch."""
    out: dict[str, tuple[float, int]] = {}
    for stmt in body:
        if not isinstance(stmt, ast.Assign):
            continue
        value = _num(stmt.value)
        if value is None:
            continue
        for target in stmt.targets:
            name = (target.id if isinstance(target, ast.Name) else
                    target.attr if isinstance(target, ast.Attribute) else "")
            if name and name.lower() in _BUDGET:
                out[name] = (value, stmt.lineno)
    return out


def _test_mentions(test: ast.AST) -> str:
    """Flatten a branch condition to text so it can be checked for arm names."""
    try:
        return ast.unparse(test).lower()
    except Exception:                                  # pragma: no cover - unparse is total in 3.13
        return ""


# --------------------------------------------------------------------------- #
# Rule 1 — baseline crippling
# --------------------------------------------------------------------------- #
def _rule_baseline_budget(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """A branch keyed on the arm that hands the baseline a smaller training budget.

    Only fires when BOTH branches set the same knob to different literals and the
    baseline side is the smaller one — an asymmetry that is visible in the code and
    needs no interpretation.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cond = _test_mentions(node.test)
        base_is_body = _has(cond, _BASELINE)
        base_is_else = _has(cond, _OURS)
        if not (base_is_body or base_is_else):
            continue
        body, orelse = _branch_assignments(node.body), _branch_assignments(node.orelse)
        base, ours = (body, orelse) if base_is_body else (orelse, body)
        for knob, (bval, bline) in base.items():
            if knob not in ours:
                continue
            oval, _ = ours[knob]
            if bval < oval:
                out.append(CodeAuditFinding(
                    rule_id="cripple-branch-budget", category="baseline_crippling",
                    severity="MAJOR",
                    title=f"Baseline branch gets {knob}={bval:g} where the method gets {oval:g}"[:90],
                    statement=(
                        f"`{rel}` branches on the arm at line {node.lineno} and assigns "
                        f"`{knob}` = {bval:g} on the baseline side against {oval:g} on the "
                        f"proposed side. A comparison in which only one arm receives the "
                        f"larger {knob} does not isolate the method; it measures the budget."),
                    file=rel, line=bline, code_quote=_quote(lines, bline),
                    counter_explanations=[
                        "the smaller value may be the baseline's own published recipe, in which "
                        "case matching it would be the wrong thing to do",
                        "the branch may be dead code or a debug path never taken in the reported runs",
                    ]))
    return out


def _rule_baseline_dict_budget(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """A config dict mapping arm name -> budget, where the baseline entry is smaller."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        entries: dict[str, tuple[float, int]] = {}
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            v = _num(value)
            if v is not None:
                entries[key.value] = (v, getattr(value, "lineno", node.lineno))
        base = {k: v for k, v in entries.items() if _has(k, _BASELINE)}
        ours = {k: v for k, v in entries.items() if _has(k, _OURS)}
        if not (base and ours):
            continue
        bkey, (bval, bline) = min(base.items(), key=lambda kv: kv[1][0])
        okey, (oval, _) = max(ours.items(), key=lambda kv: kv[1][0])
        if bval < oval:
            out.append(CodeAuditFinding(
                rule_id="cripple-config-table", category="baseline_crippling", severity="MAJOR",
                title=f"Per-arm config gives '{bkey}' {bval:g} against '{okey}' {oval:g}"[:90],
                statement=(
                    f"`{rel}` holds a per-arm literal table in which the baseline entry "
                    f"'{bkey}' is {bval:g} while '{okey}' is {oval:g}. If that value is a "
                    f"learning rate, an epoch count or a width, the arms were not given "
                    f"equal footing and the reported margin includes the difference."),
                file=rel, line=bline, code_quote=_quote(lines, bline),
                counter_explanations=[
                    "the two arms may legitimately need different values, e.g. a tuned "
                    "per-method learning rate taken from each method's own paper",
                ]))
    return out


def _rule_baseline_augmentation(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """Augmentation applied on one arm only — the same asymmetry, in the data pipeline."""
    out = []
    aug = ("augment", "randomcrop", "randomresized", "randomflip", "randomhorizontal",
           "colorjitter", "randaugment", "autoaugment", "mixup", "cutmix", "transform")
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cond = _test_mentions(node.test)
        if not (_has(cond, _OURS) and not _has(cond, _BASELINE)):
            continue
        try:
            body_src = ast.unparse(ast.Module(body=node.body, type_ignores=[])).lower()
            else_src = ast.unparse(ast.Module(body=node.orelse, type_ignores=[])).lower() \
                if node.orelse else ""
        except Exception:                              # pragma: no cover
            continue
        if _has(body_src, aug) and not _has(else_src, aug):
            out.append(CodeAuditFinding(
                rule_id="cripple-augmentation-one-arm", category="baseline_crippling",
                severity="MAJOR",
                title="Data augmentation is applied on the proposed arm only"[:90],
                statement=(
                    f"`{rel}` line {node.lineno} gates augmentation on the arm: the proposed "
                    f"branch builds augmentation the other branch does not. Standard "
                    f"augmentation withheld from the baseline lowers it by an amount that has "
                    f"nothing to do with the contribution."),
                file=rel, line=node.lineno, code_quote=_quote(lines, node.lineno),
                counter_explanations=[
                    "the method may BE an augmentation, in which case the asymmetry is the "
                    "experiment rather than a defect",
                ]))
    return out


# --------------------------------------------------------------------------- #
# Rule 2 — data leakage
# --------------------------------------------------------------------------- #
def _rule_fit_before_split(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """A preprocessor fitted before the split sees the test rows it will later score."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue
        fits, splits = [], []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("fit", "fit_transform"):
                fits.append(node)
            elif isinstance(func, ast.Name) and func.id in _SPLITTERS:
                splits.append(node)
            elif isinstance(func, ast.Attribute) and func.attr in _SPLITTERS:
                splits.append(node)
        if not (fits and splits):
            continue
        first_split = min(s.lineno for s in splits)
        for f in fits:
            if f.lineno >= first_split:
                continue
            try:
                src = ast.unparse(f)
            except Exception:                          # pragma: no cover
                continue
            out.append(CodeAuditFinding(
                rule_id="leak-fit-before-split", category="data_leakage", severity="MAJOR",
                title="Preprocessor fitted on the full data before the train/test split"[:90],
                statement=(
                    f"`{rel}` calls `{src[:70]}` at line {f.lineno}, before the split at line "
                    f"{first_split}. Statistics fitted on all rows carry test-set information "
                    f"into training, which inflates the reported score by an amount no "
                    f"held-out evaluation can recover."),
                file=rel, line=f.lineno, code_quote=_quote(lines, f.lineno),
                counter_explanations=[
                    "the fitted object may be discarded and refitted after the split",
                    "the data fitted on may be a separate pretraining corpus, not the "
                    "evaluation set",
                ]))
    return out


def _rule_fit_on_test(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """`.fit(X_test)` — the unambiguous form of the same error."""
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("fit", "fit_transform", "partial_fit"):
            continue
        for arg in node.args:
            try:
                name = ast.unparse(arg).lower()
            except Exception:                          # pragma: no cover
                continue
            if re.search(r"\b(x_?test|test_?x|testset|test_set|test_data|x_?eval)\b", name):
                out.append(CodeAuditFinding(
                    rule_id="leak-fit-on-test", category="data_leakage", severity="MAJOR",
                    title=f"`{node.func.attr}` is called on the test split itself"[:90],
                    statement=(
                        f"`{rel}` line {node.lineno} fits on `{name[:40]}`. Fitting any "
                        f"parameter — a scaler, an encoder, a model — on the evaluation split "
                        f"means the reported number is measured on data the fit already saw."),
                    file=rel, line=node.lineno, code_quote=_quote(lines, node.lineno),
                    counter_explanations=[
                        "transductive settings legitimately fit on unlabelled test inputs; "
                        "that has to be declared, and changes what the number means",
                    ]))
                break
    return out


def _rule_test_used_as_val(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """Selecting the checkpoint on the test split turns the test score into a training score."""
    out = []
    pat = re.compile(
        r"(val(idation)?[\w\[\]'\". ]*=\s*[\w\.]*test)|"
        r"(best[\w_]*\s*=\s*[^=\n]*\btest)|"
        r"(early_?stop\w*\([^)]*\btest)|"
        r"(monitor\s*=\s*[\"'][^\"']*test)", re.IGNORECASE)
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#") or not pat.search(stripped):
            continue
        out.append(CodeAuditFinding(
            rule_id="leak-model-selection-on-test", category="data_leakage", severity="MAJOR",
            title="Model selection or early stopping is driven by the test split"[:90],
            statement=(
                f"`{rel}` line {i} uses the test split as the validation signal. A checkpoint "
                f"chosen by the test score has been fitted to the test set through the "
                f"selection itself, so the reported number is optimistic by an amount that "
                f"grows with the number of checkpoints considered."),
            file=rel, line=i, code_quote=stripped,
            counter_explanations=[
                "some benchmarks define no validation split, in which case this is a "
                "limitation of the benchmark that the paper still has to state",
            ]))
    return out


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


# --------------------------------------------------------------------------- #
# Rule 3 — metric deviation
# --------------------------------------------------------------------------- #
def _metric_like(name: str) -> bool:
    low = (name or "").lower().lstrip("_")
    return any(low == m or low.startswith(m + "_") or low.endswith("_" + m)
               for m in _STD_METRICS)


def _rule_metric_best_of_n(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """A scoring function that maximises over candidates is an oracle, not the metric."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _metric_like(fn.name):
            continue
        try:
            src = ast.unparse(fn)
        except Exception:                              # pragma: no cover
            continue
        low = src.lower()
        # `argmax(dim=...)` is the ordinary way to read a prediction; a bare max/sort over
        # a *score* list inside the metric is selection over candidates.
        selects = re.search(r"\bmax\s*\(\s*(scores|candidates|preds|results|accs|hyps)", low) \
            or re.search(r"\bsorted\s*\([^)]*\)\s*\[-?\d?:?\]?", low) and "threshold" in low
        if not selects:
            continue
        line = fn.lineno
        out.append(CodeAuditFinding(
            rule_id="metric-best-of-n", category="metric_deviation", severity="MAJOR",
            title=f"`{fn.name}` maximises over candidates, which is best-of-N, not {fn.name}"[:90],
            statement=(
                f"`{rel}` defines `{fn.name}` at line {line} and takes a maximum over several "
                f"candidate predictions or thresholds inside the metric itself. That reports "
                f"an oracle upper bound: the standard definition scores one prediction chosen "
                f"without reference to the label."),
            file=rel, line=line, code_quote=_quote(lines, line),
            counter_explanations=[
                "best-of-N is a legitimate metric when it is named as such and the baselines "
                "get the same N",
            ]))
    return out


def _rule_metric_filters_samples(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """Dropping hard samples inside the metric shrinks the denominator."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _metric_like(fn.name):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Compare):
                continue
            try:
                src = ast.unparse(node).lower()
            except Exception:                          # pragma: no cover
                continue
            if not re.search(r"\b(y_?true|target|label|gt|ground_?truth)\b", src):
                continue
            if not re.search(r"[<>]=?|!=", src):
                continue
            out.append(CodeAuditFinding(
                rule_id="metric-filters-ground-truth", category="metric_deviation",
                severity="MAJOR",
                title=f"`{fn.name}` filters samples by their ground truth before scoring"[:90],
                statement=(
                    f"`{rel}` line {node.lineno}, inside `{fn.name}`, selects samples using the "
                    f"label (`{src[:50]}`). Excluding cases by their ground truth changes the "
                    f"denominator, so the reported figure is not comparable with a published "
                    f"number computed over the full split."),
                file=rel, line=node.lineno, code_quote=_quote(lines, node.lineno),
                counter_explanations=[
                    "ignore-index masking (e.g. void pixels in segmentation, padding in NLP) "
                    "is standard and expected for some metrics",
                ]))
            break
    return out


def _rule_metric_shadows_library(tree: ast.AST, lines: list[str], rel: str) -> list[CodeAuditFinding]:
    """A hand-rolled metric alongside an imported standard one is worth diffing."""
    out = []
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("sklearn.metrics", "torchmetrics", "evaluate", "scipy.stats")):
            imported |= {a.asname or a.name for a in node.names}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _metric_like(fn.name):
            continue
        if fn.name in imported or not imported:
            continue
        out.append(CodeAuditFinding(
            rule_id="metric-shadows-standard", category="metric_deviation", severity="MINOR",
            title=f"`{fn.name}` is hand-written while standard metrics are imported nearby"[:90],
            statement=(
                f"`{rel}` imports {sorted(imported)[:3]} yet defines its own `{fn.name}` at "
                f"line {fn.lineno}. A custom implementation of a named metric is where a "
                f"definition quietly drifts — averaging mode, denominator, tie handling — so "
                f"it should be diffed against the library version before its numbers are "
                f"compared with anyone else's."),
            file=rel, line=fn.lineno, code_quote=_quote(lines, fn.lineno),
            counter_explanations=["the custom version may exist purely for speed and agree exactly"]))
    return out


_RULES = (
    _rule_baseline_budget, _rule_baseline_dict_budget, _rule_baseline_augmentation,
    _rule_fit_before_split, _rule_fit_on_test, _rule_test_used_as_val, _rule_unseeded_split,
    _rule_metric_best_of_n, _rule_metric_filters_samples, _rule_metric_shadows_library,
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
import numpy as np
from sklearn.metrics import accuracy_score, f1_score
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

BUDGET = {"baseline": 10, "ours": 200}

def accuracy(y_true, y_pred, scores):
    keep = y_true > 0
    return max(scores)

def main(arm):
    X, y = np.zeros((10, 3)), np.zeros(10)
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    X_train, X_test, y_train, y_test = train_test_split(Xs, y)
    if arm == "baseline":
        epochs = 5
        lr = 0.0001
    else:
        epochs = 100
        lr = 0.01
    val_set = X_test
    scaler.fit(X_test)
    return epochs, lr, val_set
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
    for expected in ("cripple-branch-budget", "cripple-config-table", "leak-fit-before-split",
                     "leak-fit-on-test", "leak-model-selection-on-test", "leak-unseeded-split",
                     "metric-best-of-n", "metric-filters-ground-truth", "metric-shadows-standard"):
        assert expected in hits, f"rule did not fire: {expected} (got {sorted(hits)})"
    clean = audit_source("clean.py", "def f(x):\n    return x + 1\n")
    assert clean == [], f"a clean file must produce nothing, got {clean}"
    assert audit_source("broken.py", "def (:\n") == [], "a syntax error must not raise"
    assert audit_repo("does-not-exist").skipped, "a missing checkout reports skipped, not empty"
    assert all(f.severity != "FATAL" for f in audit_source("selfcheck.py", _SELFCHECK)), \
        "static analysis must never emit FATAL — that is reserved for a failed reproduction"
    print("code_audit self-check OK")
