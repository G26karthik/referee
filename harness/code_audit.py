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

from .artifacts import CodeAudit, CodeAuditFinding

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


def audit_repo(repo_path: str | Path, max_files: int = 800) -> CodeAudit:
    """Static audit of a checkout. Never imports, never executes, never leaves the tree."""
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
