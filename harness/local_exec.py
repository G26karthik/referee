"""S3 — run a reproduction probe on the local GPU. No Modal, no git, no network.

The harness owns the harnessing, not the science: it writes a script, runs it once
per seed in a subprocess, parses a fixed stdout contract, and does the statistics.
What the script actually trains is the driver's business — pass it in `ProbeSpec.script`.

**Stdout contract.** The probe prints, on their own lines:

    SH_DEVICE <cuda|mps|cpu>
    SH_METRIC arm=<name> seed=<int> value=<float>
    SH_AUX    key=<name> arm=<name> seed=<int> value=<float>    (optional, repeatable)

Anything else on stdout is ignored, so a script may log freely.

**Why a subprocess per seed** rather than a loop inside one process: a seed is only
really independent if CUDA state, cuDNN autotune caches, and library RNG all start
fresh. Re-seeding in-process leaves enough shared state that the measured spread
under-reports the true seed noise — which is the one number this stage exists to get right.

`python -m harness.local_exec` runs the self-check.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time
from pathlib import Path

from .artifacts import (ArmStats, CommitVerification, ExecAuthorization, ExecutionRecord,
                        ProbeResult, ProbeSpec, Reconciliation)
from .backends import ExecRequest, ExecutionBackend, authorize, backend_for
from .repo import verify_commit
from .experiment_id import identities_established
from .config import Config
from . import comparison as comparison_mod, provenance as provenance_mod, state

_METRIC = re.compile(r"^SH_METRIC\s+arm=(\S+)\s+seed=(-?\d+)\s+value=([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*$")
_DEVICE = re.compile(r"^SH_DEVICE\s+(\S+)\s*$")
# Secondary measurements. A probe that reports only its headline number cannot answer
# "and did the treatment break anything else?", which is the first thing an editor asks
# of any regularizer.
_AUX = re.compile(r"^SH_AUX\s+key=(\S+)\s+arm=(\S+)\s+seed=(-?\d+)\s+value=([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*$")
# A cell reads "59.28", "12.196 ± 0.207", "35.3 41.6" or "80.5%(161)". Reconciliation
# takes the FIRST magnitude, which is the reported value; the rest is variance or a
# neighbouring column that the extractor collapsed into one string.
# The exponent is not optional decoration. A cell reading "1.23e4 ± 3.29e2" parsed
# without it yields 1.23 instead of 12300 — a factor of 10,000 — and that value is what
# `reconcile` compares against a reproduced metric. Silently reading four orders of
# magnitude off a printed cell is the worst available failure for this function, because
# the result still looks like a number and still divides cleanly by a noise band.
_LEADING_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
# TWO decimal points inside one token. This is not a number any paper prints; it is what
# extraction produces when it fuses a mean with the standard deviation printed beside it,
# and the fusion is invisible because the result still parses as a float. Measured over
# the eight-paper corpus: 15 of 2,862 table cells carry this signature, and `acl`'s
# Table 2 is written entirely in it — `60.357.47` is 60.35 and 7.47, `65.4132.53` is
# 65.41 and 32.53. Reading the leading run of digits off `60.357.47` yields 60.357, a
# quantity that appears nowhere in the paper, and that value was then reconciled against
# a measured metric and printed to a reviewer as "the printed value". Refusing costs
# 0.5% of cells and is the only honest answer: a cell this harness cannot segment is a
# cell whose quantity it does not know.
_MERGED_DECIMALS = re.compile(r"\d+\.\d+\.\d")
# 2,900 -> 2900, and ONLY there. Stripping every comma would also erase the separator in
# `12.5 [11.0, 14.0]`, turning a recognised interval into an unparseable run of numbers.
# `claims.py` draws the line in the same place, for the same reason.
_CELL_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
# The two shapes in which a cell reports ONE quantity using more than one number: a value
# with its uncertainty after a plus-minus sign, and a value with its uncertainty or
# interval in brackets. The leading number is the reported quantity in both, which is why
# these are admitted while a bare run of several numbers is not. Both tolerate scientific
# notation, because `2024-icml-sapg` writes every cell as `1.01e4±6.31e2` and dropping the
# exponent here would reintroduce the factor-of-10,000 error the comment above describes.
#
# These FULL-MATCH the whole cell rather than its prefix. Anchoring only the front admits
# `1.23e4±3.29e2 9.14e3±8.38e2` — two adjacent cells extraction ran together, which occurs
# in `2024-icml-sapg` — and would hand back the first cell's value as though the string
# reported one quantity. A shape is a shape only if it accounts for every character.
_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_VALUE_WITH_UNCERTAINTY = re.compile(
    rf"^\s*({_NUM})\s*%?\s*(?:"
    rf"(?:±|\+/-|\+-)\s*{_NUM}\s*%?"
    rf"|[\(\[]\s*{_NUM}(?:\s*(?:,|±|–|—|-|to)\s*{_NUM})?\s*[\)\]]\s*%?"
    rf")\s*$")
# A repo that does not implement the SH_METRIC contract usually still prints a JSON
# summary. These are the keys worth reading, in preference order.
_JSON_METRIC_KEYS = ("value", "metric", "score", "result", "accuracy", "acc", "top1",
                     "top_1", "f1", "map", "miou", "iou", "psnr", "bleu", "auc", "mean")

# The default probe. With no intervention supplied both arms are identical, so it
# measures this machine's SEED NOISE FLOOR — the denominator every "is this gain real?"
# question divides by. Trains on the local GPU through torch when it is installed and
# falls back to sklearn on CPU when it is not, so the stage runs either way.
#
# Placeholders are __NAME__ rather than {name}: the body is full of f-strings and dict
# literals, and doubling every brace for str.format turns it into line noise.
DEFAULT_TEMPLATE = '''\
"""Auto-generated reproduction probe — __METRIC__ on __DATASET__, arms __ARMS__.

Claim under test: __CLAIM__

Edit `build(seed, arm)` to apply the paper's mechanism to the treatment arm. As
generated both arms are IDENTICAL, so the measured delta is this machine's seed noise
and any claimed gain smaller than twice that number is undetectable here.

Contract (parsed by harness/local_exec.py — keep both lines):
    SH_DEVICE <cuda|mps|cpu>
    SH_METRIC arm=<name> seed=<int> value=<float>
"""
import argparse

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, required=True)
ap.add_argument("--arm", required=True)
args = ap.parse_args()

from sklearn.datasets import load_breast_cancer, load_digits, load_wine
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

DATASETS = {"digits": load_digits, "breast_cancer": load_breast_cancer, "wine": load_wine}
X, y = DATASETS["__DATASET__"](return_X_y=True)

# Split BEFORE scaling. Fitting the scaler on all of X leaks test statistics into
# training; this probe must not commit the protocol error it exists to detect.
Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=args.seed, stratify=y)
scaler = StandardScaler().fit(Xtr)
Xtr, Xte = scaler.transform(Xtr), scaler.transform(Xte)

try:
    import torch
except ImportError:
    torch = None

if torch is None:
    DEVICE = "cpu"
    print("SH_DEVICE cpu", flush=True)
    from sklearn.neural_network import MLPClassifier

    def build(seed, arm):
        # TODO(driver): branch on `arm` to apply the paper's mechanism.
        return MLPClassifier(hidden_layer_sizes=(64,), max_iter=__EPOCHS__, random_state=seed)

    score = build(args.seed, args.arm).fit(Xtr, ytr).score(Xte, yte)
else:
    DEVICE = ("cuda" if torch.cuda.is_available()
              else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
              else "cpu")
    print(f"SH_DEVICE {DEVICE}", flush=True)

    def build(seed, arm):
        # TODO(driver): branch on `arm` to apply the paper's mechanism.
        torch.manual_seed(seed)
        return torch.nn.Sequential(
            torch.nn.Linear(Xtr.shape[1], 64), torch.nn.ReLU(),
            torch.nn.Linear(64, int(y.max()) + 1),
        ).to(DEVICE)

    xt = torch.tensor(Xtr, dtype=torch.float32, device=DEVICE)
    yt = torch.tensor(ytr, dtype=torch.long, device=DEVICE)
    xv = torch.tensor(Xte, dtype=torch.float32, device=DEVICE)
    yv = torch.tensor(yte, dtype=torch.long, device=DEVICE)

    model = build(args.seed, args.arm)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = torch.nn.CrossEntropyLoss()
    g = torch.Generator().manual_seed(args.seed)
    for _ in range(__EPOCHS__):
        for idx in torch.randperm(len(xt), generator=g).to(DEVICE).split(128):
            opt.zero_grad()
            loss_fn(model(xt[idx]), yt[idx]).backward()
            opt.step()
    model.eval()
    with torch.no_grad():
        score = (model(xv).argmax(1) == yv).float().mean().item()

print(f"SH_METRIC arm={args.arm} seed={args.seed} value={score:.6f}", flush=True)
'''


def render_default(spec: ProbeSpec) -> str:
    body = DEFAULT_TEMPLATE
    for key, value in (("__METRIC__", spec.metric), ("__DATASET__", spec.dataset),
                       ("__ARMS__", "/".join(spec.arms)), ("__EPOCHS__", str(spec.epochs)),
                       ("__CLAIM__", (spec.claim or "(none given)").replace('"""', "'''"))):
        body = body.replace(key, value)
    return body


def write_probe(root: Path, spec: ProbeSpec, out_dir: Path | None = None) -> Path:
    """Materialize `runs/<paper_id>/probe.py`. Returns its path.

    When `spec.command` is set the code under test is the paper's own checkout, so
    nothing is generated — the returned path is only used to locate the run directory.
    """
    path = (out_dir or (root / "runs" / spec.paper_id)) / "probe.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not spec.command:
        path.write_text(spec.script or render_default(spec), encoding="utf-8")
    return path


def resolve_command(cfg: Config, spec: ProbeSpec, script: Path, seed: int,
                    arm: str) -> tuple[list[str], Path]:
    """The argv and working directory for one (seed, arm) run.

    Two shapes. `spec.command` runs the repository's own evaluation entrypoint with
    `{seed}` / `{arm}` substituted, inside the checkout, using the isolated
    interpreter that `repo.build_env` created. Everything else runs the generated
    probe script the historical way. A bare leading `python` is rewritten to the
    chosen interpreter so a README command works unmodified.
    """
    if spec.command:
        argv = [str(c).replace("{seed}", str(seed)).replace("{arm}", arm) for c in spec.command]
        if argv and Path(argv[0]).name.lower() in ("python", "python3", "python.exe", "python3.exe"):
            argv[0] = spec.interpreter or cfg.python
        return argv, Path(spec.cwd) if spec.cwd else script.parent
    return ([spec.interpreter or cfg.python, str(script), "--seed", str(seed), "--arm", arm],
            script.parent)


def parse_cell_number(text: str) -> float | None:
    """The quantity a table cell reports, or None when the cell reports no single one.

    This used to be `_LEADING_NUMBER.search(...)` — the first run of digits in the string,
    whatever else the string contained. That is the positional coincidence invariant 18
    forbids on the output side, applied to the paper's side of the same comparison, and it
    was reached by the reconciler on every execution this harness has ever performed.

    What it produced, measured on the shipped corpus: `acl`'s Table 2 cell `60.357.47` —
    a mean of 60.35 fused with a standard deviation of 7.47 by extraction — was read as
    **60.357**, a number that appears nowhere in that paper, and was then compared against
    a measured metric and printed to a reviewer under the heading "printed value". APT's
    `253.6% 114.8% 74.2%`, three separate columns collapsed into one string, was read as
    253.6 the same way. `claims.parse_quantity` has refused exactly this since it was
    written ("anything else yields nothing"); the cell path never had the rule.

    So it has it now, in three tiers:

      1. a token carrying two decimal points is an extraction fusion, never a printed
         number, and is REFUSED outright;
      2. a cell whose leading number is followed by an uncertainty — `61.4 ± 0.3`,
         `1.01e4±6.31e2`, `59.3 (0.4)` — reports one quantity and yields its leading
         number, because which number is the value is unambiguous in that shape;
      3. exactly one number yields that number, and anything else is REFUSED.

    Measured cost of the refusal over the eight-paper corpus: of 2,862 table cells, 814
    carry one number and 73 carry a recognised uncertainty shape, both admitted; 15 carry
    the fusion signature and 127 carry several numbers in no recognised shape, both now
    refused. A refused cell loses a target. A mis-segmented cell loses the review's
    honesty, and it does so silently, because the wrong answer is still a float.
    """
    raw = _CELL_THOUSANDS.sub("", (text or ""))
    if _MERGED_DECIMALS.search(raw):
        return None
    if (m := _VALUE_WITH_UNCERTAINTY.match(raw)):
        return float(m.group(1))
    nums = _LEADING_NUMBER.findall(raw)
    return float(nums[0]) if len(nums) == 1 else None


def printed_precision_half_width(text: str) -> float:
    """Half the rounding interval implied by how many digits were printed.

    "59.3" was rounded to one decimal place, so the true value could be anywhere in
    [59.25, 59.35) — a half-width of 0.05. "59.28" implies [59.275, 59.285), half-width
    0.005. Standard significant-figures reasoning, not a tolerance invented for this
    harness: a paper printing fewer digits is stating less precision, and a reproduction
    must not be judged against precision the paper never claimed. `Decimal` reads the
    digit count directly, from plain and scientific notation alike, and unlike a float
    round-trip it never silently loses or invents trailing digits.
    """
    m = _LEADING_NUMBER.search((text or "").replace(",", ""))
    if not m:
        return 0.0
    try:
        exponent = Decimal(m.group()).as_tuple().exponent
    except (InvalidOperation, ValueError, TypeError):
        return 0.0
    if not isinstance(exponent, int):          # 'n' or 'F' — not a finite decimal
        return 0.0
    return 0.5 * (10 ** exponent)


# Keys whose VALUE names which experiment an output object belongs to. An object carrying
# one of these can be matched against the target, which is what makes a parse
# target-aware rather than positional.
_TARGET_KEYS = ("split", "dataset", "task", "subset", "benchmark", "eval_set", "config",
                "experiment", "model")
# Keys whose value is a nested object of results. A metric found inside one of these was
# published under a structured schema rather than shouted at the top level.
_CONTAINER_KEYS = ("results", "metrics", "summary", "final", "eval", "test", "scores")
# Keys whose value NAMES the metric the object reports, e.g. {"metric":"accuracy","value":..}.
_NAMING_KEYS = ("metric", "name", "metric_name", "key")

# The preference order. A parse from a higher tier wins outright; disagreement WITHIN the
# winning tier is refused rather than resolved by position. `positional` exists only to be
# reported and is never authoritative — see `parse_metric`.
METRIC_TIERS = ("target_bound", "named_artifact", "structured", "identity", "positional")


@dataclass(frozen=True)
class MetricParse:
    """What a stdout scan concluded about the target's metric, and how it concluded it.

    `authoritative` is the field that matters. A value this harness would print but must
    not reconcile against a printed cell is exactly the failure mode `parse_quantity` and
    the provenance ceiling exist to prevent elsewhere, and it is the failure mode a
    last-JSON-object-wins scan produces: a number that looks right, divides cleanly by a
    noise band, and came from whichever line the repository happened to print last.
    """

    value: float | None = None
    tier: str = ""
    ambiguous: bool = False
    candidates: tuple[tuple[str, float], ...] = ()
    detail: str = ""

    @property
    def authoritative(self) -> bool:
        return (self.value is not None and not self.ambiguous
                and self.tier in ("target_bound", "named_artifact", "structured", "identity"))


def _json_objects(stdout: str) -> list[dict]:
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _number(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _tiered(obj: dict, key: str, experiment_hint: str) -> list[tuple[str, float]]:
    """Every (tier, value) this object offers for `key`, most authoritative first."""
    lowered = {str(k).lower(): v for k, v in obj.items()}
    hint = (experiment_hint or "").lower()
    found: list[tuple[str, float]] = []

    top = _number(lowered.get(key))
    if top is not None:
        matched = any(
            isinstance(lowered.get(tk), str) and hint and
            (lowered[tk].lower() in hint or hint in lowered[tk].lower())
            for tk in _TARGET_KEYS)
        if matched:
            found.append(("target_bound", top))
        named = any(isinstance(lowered.get(nk), str) and lowered[nk].lower() == key
                    for nk in _NAMING_KEYS)
        if named:
            found.append(("named_artifact", top))
        found.append(("identity", top))

    for ck in _CONTAINER_KEYS:
        inner = lowered.get(ck)
        if isinstance(inner, dict):
            v = _number({str(k).lower(): x for k, x in inner.items()}.get(key))
            if v is not None:
                found.append(("structured", v))
    return found


def parse_metric(stdout: str, key: str, experiment_hint: str = "") -> MetricParse:
    """The target's metric from a repository's stdout, or a refusal that says why.

    **Positional coincidence may never establish a reconciliation.** The old scan took the
    last JSON object carrying a usable key. That is right for per-epoch logging followed
    by a summary and wrong for a summary followed by a per-class breakdown reusing the
    same key, and nothing on stdout distinguishes the two cases — so the answer depended
    on what the repository happened to print last, which is not evidence.

    What replaces it is a preference order over how a value was IDENTIFIED, and a refusal
    when the winning tier disagrees with itself:

      target_bound    the object names the split/dataset/task and it matches the target
      named_artifact  the object names the metric it reports, and it is this one
      structured      the value came from a results/metrics/summary object
      identity        the bound key at the top level, and nothing else to go on

    Two objects in the winning tier reporting different numbers is an AMBIGUITY, not a
    tie to be broken. A repository that wants a reconciliation can emit the `SH_METRIC`
    contract line, which is checked before this is ever reached.
    """
    if not key:
        return MetricParse(detail="no metric identity was established, so no key is bound; "
                                  "a generic scan for whatever number parses is exactly the "
                                  "guess this refuses to make")
    key = key.lower()
    per_tier: dict[str, list[float]] = {}
    for obj in _json_objects(stdout):
        for tier, value in _tiered(obj, key, experiment_hint):
            per_tier.setdefault(tier, []).append(value)

    for tier in METRIC_TIERS:
        values = per_tier.get(tier) or []
        if not values:
            continue
        distinct = sorted(set(values))
        candidates = tuple((tier, v) for v in distinct)
        if len(distinct) > 1:
            return MetricParse(
                value=None, tier=tier, ambiguous=True, candidates=candidates,
                detail=(f"{len(distinct)} different values for '{key}' were reported at the "
                        f"same identification tier ({tier}): "
                        f"{', '.join(str(v) for v in distinct)}. Which one corresponds to the "
                        f"cited quantity is not decidable from stdout, and taking the last is "
                        f"positional coincidence rather than evidence."))
        return MetricParse(value=distinct[0], tier=tier, candidates=candidates,
                           detail=f"'{key}' identified at tier {tier}")
    return MetricParse(detail=f"no JSON object on stdout reported '{key}'")


def json_metric(stdout: str, metric: str = "", strict: bool = False) -> float | None:
    """Last JSON object on stdout that carries a usable metric, as a float.

    **Diagnostic only.** Kept because a report is more useful when it can say what the run
    printed, and because the generic key list is the only thing available when no metric
    identity was established. What it must never do is establish a reconciliation: the
    execution path calls `parse_metric` instead, which refuses a positional answer.

    `strict` is what a bound `MetricIdentity.output_key` means: ONLY that key is
    accepted, because the whole point of establishing which output corresponds to the
    cited metric is to stop here from mining a generic "value"/"score"/"mean" out of
    whichever number happens to parse when the bound key is not the one present.
    """
    if strict and not metric:
        return None
    keys = ([metric.lower()] if metric else []) + ([] if strict else list(_JSON_METRIC_KEYS))
    for obj in reversed(_json_objects(stdout)):
        lowered = {str(k).lower(): v for k, v in obj.items()}
        for key in keys:
            v = _number(lowered.get(key))
            if v is not None:
                return v
    return None


def _scale_ratio(a: float, b: float) -> float:
    """|a/b| clamped away from division by zero, used only to spot unit mismatches."""
    if b == 0:
        return float("inf") if a else 1.0
    return abs(a / b)


# Signatures that a process died during SETUP rather than during the experiment. Each is
# a failure of the runner or the invocation, not of the science: an unresolved import, an
# argument parser rejecting a flag this harness invented, a missing script. When one of
# these appears the question is settled — the experiment was not reached — regardless of
# what else the process printed.
_SETUP_FAILURE_SIGNATURES = (
    "modulenotfounderror", "importerror", "cannot import name",
    "unrecognized arguments", "the following arguments are required", "invalid choice",
    "no such file or directory", "can't open file", "syntaxerror",
    "command not found", "is not recognized as an internal or external command",
)
# Failures that are facts about the HOST, its drivers or its network — never about the
# paper's code — and, unlike a setup failure, may strike well AFTER the experiment
# demonstrably started: a CUDA OOM twenty minutes into training reads, at the stderr,
# exactly like broken code, but convicting the paper on it is exactly the unearned
# inference this harness exists to refuse. Matched by SIGNATURE (a fact about what
# happened), never by paper or repository name.
_INFRA_FAILURE_SIGNATURES = (
    "out of memory", "cuda out of memory", "cuda error", "cublas", "cudnn", "nccl",
    "no space left on device", "disk quota exceeded",
    "segmentation fault", "core dumped", "access violation", "bus error",
    "driver/library version mismatch", "cuda driver version is insufficient",
    "the paging file is too small", "insufficient system resources",
    # HOST memory exhaustion, as each runtime actually spells it. The bare "out of
    # memory" above catches the Linux OOM-killer's own message and CUDA's, and used to
    # be the whole story — but an allocation that fails INSIDE the process raises a
    # typed exception whose text contains none of those words. This host has ~15 GiB of
    # RAM and the papers under audit routinely ask for more, so an in-process allocation
    # failure is not an edge case here, it is the expected way a real reproduction dies
    # — and every one of these was convicting the paper for it.
    "memoryerror", "arraymemoryerror", "unable to allocate", "cannot allocate memory",
    "std::bad_alloc", "bad_alloc", "killed process", "oom-kill",
    # A checkpoint that is simply GONE. 401/403 (below) cover a checkpoint we are not
    # allowed to fetch; 404 is one the authors moved or deleted, which is equally not a
    # statement about whether their method works.
    "404 client error", "http error 404", "entrynotfounderror", "revisionnotfounderror",
    "401 client error", "403 client error", "gated repo", "repository not found",
    "you need to accept the license", "please log in", "authentication required",
    "connection refused", "could not resolve host", "getaddrinfo failed",
    "name or service not known", "temporary failure in name resolution",
    "max retries exceeded", "network is unreachable", "connectionerror", "sslerror",
)
# The OS killed the process rather than the program exiting on its own — POSIX signal
# termination (a negative returncode) and the Windows crash-status codes. An OOM-killed
# or segfaulting process usually prints nothing at all, so the signature list above
# cannot see it; the exit code is the only evidence left.
#
# BOTH ENCODINGS OF A SIGNAL DEATH ARE HERE, and the second one is the one that bites.
# `subprocess` reports a directly-launched child killed by signal N as -N, which is what
# this set originally held. But the moment the entrypoint is a shell wrapper — `bash
# run.sh`, `torchrun`, a Makefile, anything that propagates `$?` — the shell reports the
# same death as 128+N, and 137 (an OOM kill) arrived here as an ordinary non-zero exit.
# With startup evidence present that is a FAILED_REPRODUCTION and therefore a RED: the
# host running out of memory, published as a finding against the authors. Real
# repositories are full of shell entrypoints, so this was reachable on the most ordinary
# path there is.
_SIGNAL_RETURNCODES = (9, 11, 6, 8, 4, 7)    # SIGKILL, SIGSEGV, SIGABRT, SIGFPE, SIGILL, SIGBUS
_INFRA_FAILURE_RETURNCODES = frozenset(
    {-n for n in _SIGNAL_RETURNCODES} |      # subprocess, direct child
    {128 + n for n in _SIGNAL_RETURNCODES} | # shell wrapper, 128+signal
    {
        0xC0000005 - (1 << 32),              # STATUS_ACCESS_VIOLATION, as a signed int32
        0xC00000FD - (1 << 32),              # STATUS_STACK_OVERFLOW
    })
# A process that ran this long did something beyond importing and exiting. Used only as
# corroboration alongside real output — never on its own.
_STARTUP_WINDOW_S = 30.0
_MIN_OUTPUT_LINES = 5


@dataclass
class StartupEvidence:
    """What the process showed us about whether it got past setup into the experiment.

    Deliberately several independent signals rather than one. Requiring this harness's own
    `SH_*` contract lines would be wrong: a third-party repository owes them nothing, and a
    perfectly good reproduction that prints its metrics as plain text would be scored as
    never having started. So the contract lines are SUFFICIENT evidence, not NECESSARY —
    substantial output over a plausible runtime, or a timeout after real work, count too.
    """

    saw_contract_line: bool = False        # SH_DEVICE / SH_METRIC / SH_AUX
    saw_json_metric: bool = False          # a parseable JSON summary on stdout
    stdout_lines: int = 0
    ran_seconds: float = 0.0
    timed_out: bool = False
    setup_error: str = ""                  # a matched signature from _SETUP_FAILURE_SIGNATURES
    infra_error: str = ""                  # a matched signature from _INFRA_FAILURE_SIGNATURES

    def describe(self) -> str:
        if self.infra_error:
            return f"the process reported '{self.infra_error}', an infrastructure failure"
        if self.setup_error:
            return f"the process reported '{self.setup_error}', a setup-phase failure"
        bits = []
        if self.saw_contract_line:
            bits.append("emitted the SH_ output contract")
        if self.saw_json_metric:
            bits.append("printed a parseable JSON metric")
        if self.stdout_lines:
            bits.append(f"{self.stdout_lines} line(s) of output")
        bits.append(f"ran {self.ran_seconds:.0f}s")
        if self.timed_out:
            bits.append("killed by timeout")
        return ", ".join(bits) if bits else "no output at all"


def classify_setup_error(stderr: str) -> str:
    """The setup-failure signature present in stderr, or '' if none is."""
    low = (stderr or "").lower()
    for sig in _SETUP_FAILURE_SIGNATURES:
        if sig in low:
            return sig
    return ""


def classify_infra_failure(stderr: str, returncode: int | None = None) -> str:
    """The infrastructure-failure signature present, or '' if none is.

    Distinct from `classify_setup_error`: a setup failure means the experiment was never
    reached. An infrastructure failure can strike well AFTER it was — a CUDA OOM twenty
    minutes into training, a segfault, a host that SIGKILLs the process for memory
    pressure — and none of those is evidence the paper's code is broken, however far the
    run had progressed when it happened. Checked by TEXT SIGNATURE, and — because an
    OOM-killed or segfaulting process usually prints nothing — by the OS's own exit code.
    """
    low = (stderr or "").lower()
    for sig in _INFRA_FAILURE_SIGNATURES:
        if sig in low:
            return sig
    if returncode is not None and returncode in _INFRA_FAILURE_RETURNCODES:
        return f"process terminated by the operating system (exit code {returncode})"
    return ""


def reached_experiment(evidence: StartupEvidence) -> bool:
    """Did the run get past setup into the experiment the paper describes?

    The burden of proof is on establishing that it did. An unproven start yields
    INCONCLUSIVE, which accuses nobody; assuming a start yields FAILED_REPRODUCTION, which
    drives RED and names the authors. Those errors are not symmetric, so the default is
    the one that cannot manufacture an accusation.
    """
    if evidence.setup_error:
        return False                                        # decisive against
    if evidence.timed_out:
        # ALSO decisive against, and this is the correction. A timeout is THIS HARNESS'S
        # wall clock, not the paper's failure: the process was alive and working when we
        # killed it, so nothing about whether the experiment would have completed was
        # established. Treating it as a reach convicted papers for `cfg.probe_timeout_s`
        # — a run that printed progress for 1800s and was killed reconciled as
        # FAILED_REPRODUCTION and drove RED. `reconcile` maps this to `timeout`, which is
        # INCONCLUSIVE, and INCONCLUSIVE accuses nobody.
        return False
    if evidence.saw_contract_line or evidence.saw_json_metric:
        return True                                         # decisive for
    long_enough = evidence.ran_seconds >= _STARTUP_WINDOW_S
    return long_enough and evidence.stdout_lines >= _MIN_OUTPUT_LINES


def _addressed(spec: ProbeSpec) -> str:
    """How to NAME what a reconciliation is about, in a refusal a human will read.

    A prose-stated total is not "the cited cell", and saying so in a refusal would tell a
    reader the harness was looking somewhere it was not.
    """
    ref = spec.claim_ref or spec.table_ref
    if not ref:
        return "the cited quantity"
    kind = spec.claim_kind or ("table_cell" if spec.table_ref else "")
    noun = {"table_cell": "cell", "prose_claim": "prose claim", "figure": "figure",
            "equation": "equation", "section_span": "section"}.get(kind, "quantity")
    return f"the {noun} at {ref}"


def reconcile(spec: ProbeSpec, values: list[float], noise_band: float,
              seeds_run: list[int], failure: str = "",
              evidence: StartupEvidence | None = None,
              authorization: ExecAuthorization | None = None) -> Reconciliation:
    """Executed metric vs the table cell the paper printed. Arithmetic, not judgement.

    The rule the caller asked for is `delta_error <= 2σ => RESOLVED_VERIFIED`, and
    that is what happens — but three situations must NOT reach a verdict, because in
    each of them a "FAILED_REPRODUCTION" would be an accusation the measurement does
    not support:

      * nothing parsed, so there is no reproduced number to compare;
      * the noise band is zero, so `<= 2σ` degenerates into demanding exactness;
      * the two numbers differ by almost exactly 100x or 0.01x, which is an accuracy
        reported as a fraction against a cell printed as a percentage — a units
        mismatch in this harness, not a doctored claim in the paper.

    A crash IS a failed reproduction, because the code was run and did not work.

    A REFUSED run is none of the above. When `authorization` says the execution was not
    permitted, nothing ran, so there is nothing to reconcile and the refusal is reported
    as itself — a fact about this harness's own gates, never a finding about the paper.
    """
    evidence = evidence or StartupEvidence()
    rec = Reconciliation(table_ref=spec.table_ref, finding_id=spec.finding_id,
                         claim_ref=spec.claim_ref or spec.table_ref,
                         claim_kind=spec.claim_kind or ("table_cell" if spec.table_ref else ""),
                         target_id=spec.target_id,
                         metric=spec.metric, claimed_raw=spec.claimed_cell_value,
                         provenance=spec.provenance,
                         noise_band=round(noise_band, 6), seeds_run=sorted(seeds_run))
    rec.claimed_value = parse_cell_number(spec.claimed_cell_value)

    # --- the authorization precondition --------------------------------------------
    # Checked before anything else, because if execution was refused then whatever
    # numbers are in `values` did not come from the run this reconciliation describes.
    # This is the only branch that can be reached without a process having existed.
    if authorization is not None and not authorization.allowed:
        rec.status = "INCONCLUSIVE"
        rec.failure_class = authorization.failure_class or "execution_unauthorized"
        rec.reached_experiment = False
        rec.reason = (
            f"the repository was not executed, so nothing was reproduced: "
            f"{authorization.detail} (decision '{authorization.decision}'). No reproduction "
            f"verdict is drawn, because a refusal by this harness is not evidence about "
            f"{_addressed(spec)}."
        )
        return rec

    if values:
        rec.reproduced_value = round(statistics.fmean(values), 6)
        rec.reproduced_std = round(statistics.stdev(values), 6) if len(values) > 1 else 0.0
    if rec.claimed_value is not None and rec.reproduced_value is not None:
        rec.delta_error = round(abs(rec.reproduced_value - rec.claimed_value), 6)

    # --- WHAT IDENTITY ACTUALLY SAID, recorded before any branch can return -----------
    # These three used to be assigned BELOW the provenance ceiling, which returns for
    # every synthesized and template probe — so on every such reconciliation they kept
    # their field defaults, and the default is the string "unmapped". A default is not a
    # measurement, and this one was read as one: over the eight-paper corpus all 16
    # reconciliations reported `unmapped` on all three states while the specs that
    # produced them recorded `no_candidate` (the cited row is a third-party baseline the
    # authors' code does not implement), `ambiguous` (84 advertised commands could emit
    # the metric) and `unmapped`. Three different findings about three repositories,
    # flattened into one word by the order of two blocks.
    #
    # Assigning them here changes no decision — `identities_established` below is still
    # what gates, and the ceiling still returns first — and it makes the record say which
    # of the identity failures actually occurred, which is the difference between "we did
    # not look" and "we looked and the cell is not something their code produces".
    rec.experiment_state = spec.experiment.state if spec.experiment else "unmapped"
    rec.metric_state = spec.metric_identity.state if spec.metric_identity else "unmapped"
    rec.configuration_state = spec.configuration.state if spec.configuration else "unmapped"

    # --- the provenance ceiling ---------------------------------------------------
    # A synthesized probe is OUR reimplementation of the paper's formulation, run at toy
    # scale on synthetic data. Its number is real and its noise band is real, but it is
    # a measurement of the MECHANISM, not of the benchmark the cell reports, so it is not
    # entitled to either verdict. Letting it convict would be the same unearned inference
    # this harness exists to catch other people making — and letting it ACQUIT would be
    # worse, because a toy that happens to land near the printed number would launder a
    # claim nobody checked. Only code the authors wrote may reconcile against their cell.
    if not provenance_mod.admits(spec.provenance):
        rec.status = "INCONCLUSIVE"
        near = ("" if rec.delta_error is None else
                f" (|delta| {rec.delta_error:.4f} against a 2-sigma band of {noise_band:.4f})")
        rec.reason = (
            f"the probe that ran was {spec.provenance}, not the paper's own code: "
            f"{'a mechanism reimplementation at toy scale' if spec.provenance == 'synthesized' else 'the identical-arms noise-floor template'}. "
            f"Its result{near} is evidence about the mechanism, not a reproduction of the "
            f"value printed at {_addressed(spec)}, so no reproduction "
            f"verdict is drawn. Open SH_ALLOW_REPO_EXEC to reconcile against the authors' code."
        )
        return rec

    # --- the reimplementation conformance precondition (decision 1) -------------------
    # `reimpl_exec` has no repository experiment to bind against — there is no repository,
    # which is the reason this route exists at all — so `identities_established` further
    # below, which asks whether a REPO COMMAND emits the cited quantity, does not apply to
    # it and is skipped for this provenance. The question a reconstruction has to answer
    # instead is whether EVERY required ingredient the paper specifies was actually
    # realized in the code that ran, bound to both a paper locator and a verified
    # implementation locator — that is exactly what `ReimplementationConformance` records.
    # A nonconformant reconstruction settles nothing: any disagreement it produced would be
    # a disagreement with the reimplementer's own invention, never a finding about the
    # paper's stated method.
    if spec.provenance == "reimpl_exec":
        conf = spec.reimplementation_conformance
        if conf is None or not conf.established:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "reimplementation_nonconformant"
            rec.reason = (
                f"this reconstruction is not fully conformant, so no verdict is drawn about "
                f"{_addressed(spec)}: "
                f"{(conf.reason if conf is not None else 'no ReimplementationConformance was recorded for this spec')}. "
                f"A disagreement from an unbound reconstruction would be a disagreement with "
                f"the reimplementer's own invention, not a finding about the paper's stated "
                f"method."
            )
            return rec

    # --- WHICH COMPARISON THIS IS, and whether this system can perform it -------------
    # The arithmetic below is ONE comparison: a measured quantity against the one the paper
    # printed. Nothing in the record used to say so, and nothing checked that it was the
    # right comparison for the target — every route ended here and was reconciled against a
    # cell, including routes whose evidence is a difference between two arms the paper
    # printed neither of.
    #
    # Applied AFTER the ceiling and identically to every kind, so a synthesized probe still
    # reports the ceiling rather than a comparison class: which comparison was intended
    # cannot rescue a program that was never entitled to reconcile anything.
    if spec.comparison is not None:
        rec.comparison_kind = spec.comparison.kind
        rec.comparison_state = spec.comparison.state
        if not comparison_mod.admits_verdict(spec.comparison):
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "comparison_unestablished"
            rec.reason = (
                f"this target needed a "
                f"{spec.comparison.kind.lower().replace('_', ' ')} comparison and this "
                f"review could not carry one out: {spec.comparison.reason} Whatever ran "
                f"has measured something; there is nothing here to hold it against, so no "
                f"verdict is drawn about {_addressed(spec)}.")
            return rec
        if spec.comparison.kind == "BETWEEN_ARMS":
            # AND THE ONE COMPARISON THIS FUNCTION MUST NOT PERFORM, even though it is now
            # established and admissible. Everything below holds a measured quantity
            # against one the PAPER PRINTED; a focused validation holds one arm against
            # another and the paper printed neither, so running the arithmetic below on it
            # would reconcile an arm against a cell that describes a different experiment
            # — the exact defect `harness.comparison` was written to name, arriving one
            # layer lower and wearing a verdict.
            #
            # The comparison IS performed, by `between_arms.compare`, from
            # `stages.validation.adjudicate`, over the per-arm statistics `run_probe`
            # produced. This is NOT_ATTEMPTED rather than INCONCLUSIVE because nothing
            # here failed: the right arithmetic ran somewhere else, and saying "it settled
            # nothing" would report a completed comparison as a dead end.
            rec.status = "NOT_ATTEMPTED"
            rec.failure_class = "none"
            rec.reason = (
                "this target is a focused validation: its result is one arm held against "
                "another, which `harness.between_arms` performs against the settlement "
                "condition the design declared before the run. No quantity the paper "
                "printed is reconciled here, because the paper printed neither arm.")
            return rec
    elif spec.provenance:
        # A spec built before this layer existed, or by hand. The comparison it gets is the
        # one this function has always performed, recorded so the trace says which.
        rec.comparison_kind = "AGAINST_PRINTED_VALUE"

    # --- the identity precondition ------------------------------------------------
    # Capability asks whether the code CAN run. Identity asks whether running it answers
    # the question. A capable run of the wrong program produces a confident irrelevant
    # number, which is more dangerous than a crash because nothing looks wrong. This gate
    # applies to success and failure alike, and sits after the provenance ceiling so a
    # synthesized probe still reports the ceiling rather than an identity class.
    #
    # Applied to every provenance the ceiling above admits EXCEPT `reimpl_exec`, not only
    # to `spec.command`. A `driver` spec can carry a hand-written `script` with no
    # `command` at all — a human wrote `spec.json` and pointed it at real code — and
    # gating this check on `spec.command` let exactly that spec skip straight to the
    # arithmetic below with experiment/metric/configuration identity never assessed.
    # Provenance says WHOSE code ran; identity says whether running it answers the cited
    # cell, and a driver script is not exempt from the second question just because a
    # human, not the planner, wrote it. The scientific prerequisites are the same
    # regardless of who authored the probe.
    #
    # `reimpl_exec` is the one exception, and it is an exception rather than a gap: there
    # is no repository command for `identities_established` to ask about — the check it
    # performs (`ExperimentIdentity`/`MetricIdentity`/`ConfigurationIdentity` all binding a
    # REPO COMMAND to the cited cell) is not a question a from-scratch reconstruction can
    # even be asked. The reconformance precondition just above is what stands in its place
    # for this provenance, and it is at least as strict: it requires every required
    # ingredient bound AND VERIFIED, not merely one command among several.
    #
    # The three states themselves are recorded further up, before the ceiling, so that a
    # refused probe still carries what identity found rather than a field default.
    if spec.provenance != "reimpl_exec":
        proven, failure_class, why = identities_established(
            spec.experiment, spec.metric_identity, spec.configuration)
        if not proven:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = failure_class
            rec.reason = (
                f"the executed program was not bound to the cited cell, so its output cannot "
                f"be compared with {_addressed(spec)}: {why}. A run that succeeds without "
                f"this binding has measured something, but not the thing the paper printed."
            )
            return rec

    # --- the capability precondition ----------------------------------------------
    # A crash is a failed reproduction only if the code was actually run. Three things
    # can produce a non-zero exit that says nothing about the paper: an environment this
    # machine could not build, dependencies that were never installed, and an argv this
    # harness invented that the repository does not accept. Each is a fact about the
    # runner. Convicting on them would be the same unearned inference the provenance
    # ceiling above exists to prevent, arriving through a different door.
    if failure:
        cap = spec.capability
        if cap is not None and not cap.established:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = cap.reason_code
            rec.reached_experiment = False
            rec.reason = (
                f"the run could not be mounted fairly, so its failure is not evidence about the "
                f"paper: {cap.detail}. The process exited with: {failure}. Classified "
                f"'{cap.reason_code}' — a reproduction verdict requires that the experiment was "
                f"actually attempted under the environment the authors declared."
            )
            return rec

        # --- infrastructure failures never convict ---------------------------------
        # Checked before `reached_experiment`, and independent of it: a CUDA OOM, a
        # segfault, a killed process, a driver mismatch, a blocked dataset download, or a
        # gated-repo auth failure is a fact about this host, not about the paper's code —
        # whether or not the experiment had visibly started when it happened. A crash
        # twenty minutes into training reads, at the stderr, exactly like broken code;
        # convicting on it is the unearned inference this harness exists to refuse.
        if evidence.infra_error:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "infrastructure_failure"
            rec.reached_experiment = reached_experiment(evidence)
            rec.reason = (
                f"the run failed for an infrastructure reason ('{evidence.infra_error}'), a "
                f"fact about this host, its drivers or its network — not about the paper's "
                f"code: {failure}. Startup evidence: {evidence.describe()}. An infrastructure "
                f"failure is never read as a failed reproduction, however far the experiment "
                f"had progressed when it happened."
            )
            return rec

        reached = reached_experiment(evidence)
        rec.reached_experiment = reached
        if not reached:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "timeout" if evidence.timed_out else "startup_failure"
            # Three different situations land here and the sentence has to name the right
            # one. It used to say "exited before any sign that the experiment itself began"
            # for all of them, which is simply false when a setup signature appeared AFTER
            # the process had printed contract lines, or when our own clock killed a run
            # that had been working for half an hour.
            if evidence.timed_out:
                why = (f"this harness stopped the run at its own wall-clock limit, so whether "
                       f"the experiment would have completed was never established")
            elif evidence.setup_error:
                why = (f"at least one attempt failed for a setup reason "
                       f"('{evidence.setup_error}'), so the run was not a fair attempt at the "
                       f"experiment even where other attempts produced output")
            else:
                why = ("the process exited before any sign that the experiment itself began")
            rec.reason = (
                f"{why} ({evidence.describe()}): {failure}. Nothing here distinguishes a defect "
                f"in the paper's code from a failure of the runner, and the burden of showing "
                f"the experiment ran is on this harness."
            )
            return rec

        rec.status = "FAILED_REPRODUCTION"
        rec.failure_class = "runtime_failure"
        rec.reason = (f"the paper's code reached the experiment and then failed: {failure}. "
                      f"Startup evidence: {evidence.describe()}. A reproduction that runs and "
                      f"breaks is a failed reproduction, not an inconclusive one.")
        return rec
    if rec.reproduced_value is None:
        rec.status = "INCONCLUSIVE"
        rec.reason = ("the run produced no parseable metric (no SH_METRIC line and no JSON "
                      "summary), so there is nothing to compare against the cell")
        return rec
    if rec.claimed_value is None:
        rec.status = "INCONCLUSIVE"
        rec.reason = (f"no number could be parsed out of the cited cell "
                      f"{spec.claim_ref or spec.table_ref or '(none cited)'}, so there is no claim to reconcile")
        return rec

    ratio = _scale_ratio(rec.claimed_value, rec.reproduced_value)
    if 50 <= ratio <= 200 or 0.005 <= ratio <= 0.02:
        rec.status = "INCONCLUSIVE"
        rec.reason = (f"the cell reads {rec.claimed_value:g} and the run produced "
                      f"{rec.reproduced_value:g}, a factor of about {ratio:.0f}x. That is a "
                      f"percentage-versus-fraction units mismatch in this harness, and it "
                      f"would be dishonest to score it as a failed reproduction.")
        return rec

    # The paper printed the claimed value to some number of digits, and that is a
    # statement of PRECISION, not just of magnitude: "59.3" could be anywhere in
    # [59.25, 59.35), and a reproduction landing inside that interval has not
    # disagreed with the cell at all. Without this, the identical measurement flips
    # between RESOLVED_VERIFIED and FAILED_REPRODUCTION solely because the paper printed
    # one extra decimal — the raw delta shrinks by construction as the claimed value
    # trades precision for the appearance of agreement, and rounding precision is not a
    # magic tolerance, it is a property of what the paper actually asserted.
    assert rec.delta_error is not None  # claimed_value and reproduced_value are both set above
    rec.claimed_precision = round(printed_precision_half_width(spec.claimed_cell_value), 6)
    effective_delta = max(0.0, rec.delta_error - rec.claimed_precision)
    precision_note = (
        f" (the cell's printed precision of ±{rec.claimed_precision:g} is credited first, "
        f"leaving an effective |delta| of {effective_delta:.4f})"
        if rec.claimed_precision else "")

    # A quantity with no seed-to-seed variance BY CONSTRUCTION is not a very precise
    # measurement and it is not a degenerate one either — it is a different kind of
    # quantity. A count of the items a generator produces has no distribution to have a
    # band; the right tolerance for it is the precision the paper printed, which
    # `claimed_precision` already carries.
    #
    # Narrow on purpose, and the narrowness is the safety argument. It requires the METRIC
    # IDENTITY to have established that the cited quantity is a `count`, which
    # `experiment_id` only concludes for a prose composition naming a population. For a
    # stochastic metric, zero measured variance means the opposite thing — the seed never
    # reached the model — and that case still refuses below, unchanged.
    deterministic_count = (
        noise_band <= 0
        and spec.metric_identity is not None
        and spec.metric_identity.established
        and spec.metric_identity.cell_quantity == "count"
        and len(rec.seeds_run) > 1
        and rec.reproduced_std == 0.0)

    if deterministic_count:
        within = effective_delta <= 0
        rec.status = "RESOLVED_VERIFIED" if within else "FAILED_REPRODUCTION"
        rec.reason = (
            f"reproduced a count of {rec.reproduced_value:g} against the stated "
            f"{rec.claimed_value:g}, identically across {len(rec.seeds_run)} seeds. A count "
            f"has no seed-to-seed distribution, so the tolerance is the precision the paper "
            f"printed rather than a 2-sigma band: |delta| {rec.delta_error:.4f}"
            f"{precision_note} "
            + ("is within it. The stated total stands."
               if within else "exceeds it."))
    elif noise_band <= 0:
        rec.status = "INCONCLUSIVE"
        rec.reason = (f"seed-to-seed noise measured as zero over {len(rec.seeds_run)} seeds, so "
                      f"the `<= 2 sigma` test has no band to test against. Delta was "
                      f"{rec.delta_error:.4f}{precision_note}.")
    elif effective_delta <= noise_band:
        rec.status = "RESOLVED_VERIFIED"
        rec.reason = (f"reproduced {rec.reproduced_value:g} against the cell's "
                      f"{rec.claimed_value:g}: |delta| {rec.delta_error:.4f}{precision_note} is "
                      f"within the 2-sigma band {noise_band:.4f}. The printed number stands.")
    else:
        rec.status = "FAILED_REPRODUCTION"
        rec.reason = (f"reproduced {rec.reproduced_value:g} against the cell's "
                      f"{rec.claimed_value:g}: |delta| {rec.delta_error:.4f}{precision_note} "
                      f"exceeds the 2-sigma band {noise_band:.4f} over {len(rec.seeds_run)} seeds.")
    if spec.provenance == "reimpl_exec" and rec.status in ("RESOLVED_VERIFIED", "FAILED_REPRODUCTION"):
        # Decision 1's wording requirement, applied regardless of which branch above set
        # the verdict: a conformant reconstruction's disagreement is a finding about the
        # paper's STATED METHOD, and must never be read as the authors' own code failing —
        # it is not the authors' own code. `harness.provenance.PROVENANCE_LABEL` already
        # keeps `execution_provenance` from saying AUTHOR_REPOSITORY; this keeps the
        # reconciliation's own sentence from implying it too.
        rec.reason += (
            " This ran as a governed reconstruction this harness's driver wrote and bound "
            "to the paper ingredient-by-ingredient (INDEPENDENT_REIMPLEMENTATION), not the "
            "authors' own code: this is a finding about whether the paper's stated method "
            "reproduces, never a statement that the authors' code failed."
        )
    return rec


def _stats(values: list[float]) -> ArmStats:
    return ArmStats(values=values, n=len(values),
                    mean=statistics.fmean(values) if values else 0.0,
                    std=statistics.stdev(values) if len(values) > 1 else 0.0)


def _noise(per_seed: dict[str, dict[int, float]], arms: list[str]) -> tuple[float, str]:
    """Seed-to-seed noise on the DIFFERENCE, not on either arm alone.

    When both arms ran under the same seed the comparison is paired, and the spread of
    the per-seed differences is the right denominator: shared variance cancels, so it
    does not inflate the noise band and hide a real effect.

    A paired spread of EXACTLY zero is the degenerate case, not a very precise one: it
    means the seed is not perturbing the two arms independently (identical arms, or a
    seed that never reaches the model). Dividing by it would make any nonzero delta
    look infinitely significant, so fall back to the arm spread, which still measures
    this machine's real noise floor.
    """
    if len(arms) == 2:
        a, b = per_seed.get(arms[0], {}), per_seed.get(arms[1], {})
        shared = sorted(set(a) & set(b))
        if len(shared) > 1:
            paired = statistics.stdev([b[s] - a[s] for s in shared])
            if paired > 0:
                return paired, f"paired over {len(shared)} seeds"
    spreads = [_stats(list(v.values())).std for v in per_seed.values()]
    return (max(spreads) if spreads else 0.0), "unpaired (wider arm spread)"


def verify_execution_commit(spec: ProbeSpec,
                            backend: ExecutionBackend | None = None) -> CommitVerification | None:
    """Re-check, at the moment of execution, that the checkout is the audited commit.

    Returns None for a probe this harness authored — there is no repository involved, so
    there is no commit to be wrong about. For a repository run it reads the working tree
    NOW rather than trusting the SHA recorded during planning, because the whole failure
    being closed is that planning and execution can see different code: a default branch
    moves, a cached checkout is refreshed, and the second run executes something the first
    run's audit never read while still carrying that audit's findings.

    The tree it reads is the BACKEND's, not necessarily this disk's. Verifying a directory
    on the operator's machine and then running somewhere else would leave the executed
    tree unverified while the record said otherwise — so the backend names the checkout it
    will actually run, and a backend that cannot produce one fails closed (see
    `ExecutionBackend.commit_tree`).
    """
    if not spec.command:
        return None
    tree = backend.commit_tree(spec.cwd) if backend is not None else None
    return verify_commit(spec.cwd, spec.commit, tree=tree)


def _blocked(cfg: Config, root: Path, spec: ProbeSpec, auth: ExecAuthorization,
             seconds: float, commit: CommitVerification | None = None,
             results_dir: Path | None = None) -> ProbeResult:
    """The result of a run that was refused. Nothing executed; nothing is concluded.

    A distinct verdict rather than a reused one. 'failed' would say the probe ran and
    produced nothing usable, which is a different and more damaging statement than "this
    harness declined to run it" — and the difference matters most in exactly the case
    where it is easiest to lose: a repository whose execution was refused must not look,
    in the report or in the JSON, like a repository whose code did not work.
    """
    result = ProbeResult(
        paper_id=spec.paper_id, finding_id=spec.finding_id, claim=spec.claim,
        verdict="blocked", provenance=spec.provenance, mechanism=spec.mechanism,
        rationale=spec.rationale, calibration=False, backend=auth.backend,
        authorization=auth, capability=spec.capability, experiment=spec.experiment,
        metric_identity=spec.metric_identity, configuration=spec.configuration,
        seconds=seconds, resources=spec.resources, commit_verification=commit,
        backend_selection=spec.backend_selection, backend_considered=spec.backend_considered,
        commit_state=spec.commit_state,
        reason=(f"execution was not authorized ('{auth.decision}'): {auth.detail}. "
                f"No process was started, so no measurement exists and no claim about "
                f"the paper is drawn from this."),
    )
    if spec.claim_ref or spec.table_ref or spec.claimed_cell_value:
        result.reconciliation = reconcile(spec, [], 0.0, [], authorization=auth)
    # `script_path` stays empty: no probe was written, and naming a file that does not
    # exist would invite a reader to go looking for the code that ran.
    #
    # This is CONTROL STATE, not execution workspace — unlike `write_probe`'s own default
    # (which must stay under `runs/<pid>/`, or the backend could never find the script to
    # run) — so its default is `state.control_dir(root)`, never a bare `out_dir` fallback:
    # nothing ran here, so there is no execution directory this result belongs beside.
    results_dir = results_dir or state.control_dir(root)
    results_dir.mkdir(parents=True, exist_ok=True)
    state.write_json(results_dir / "probe_results.json", result.model_dump())
    return result


def run_probe(cfg: Config, root: Path, spec: ProbeSpec,
              backend: ExecutionBackend | None = None,
              out_dir: Path | None = None,
              results_dir: Path | None = None) -> ProbeResult:
    """Write the probe, run every (arm, seed) through the backend, aggregate.

    The backend is asked for, not assumed: `backend_for` returns the local one for code
    this harness authored and the operator-selected one for the repository's own command.

    Authorization is re-checked HERE, against the spec that is about to run, rather than
    trusting that whatever produced it applied the gates. `plan_execution` only sees specs
    it built; a `runs/<pid>/spec.json` written by hand goes straight past it, and one
    carrying `command` plus `"provenance": "repo_exec"` used to execute with the repo-exec
    gate shut and no identity established — after which its crash was eligible to become
    FAILED_REPRODUCTION. A refused spec produces a result with verdict 'blocked': no
    process is started, no arms are measured, and the reconciliation is INCONCLUSIVE.

    `out_dir` and `results_dir` answer two different questions and must not be conflated.
    `out_dir` is EXECUTION WORKSPACE — where `probe.py`/the script and its `env/` live,
    which a backend must be able to reach, so it stays under `runs/<pid>/...` (defaulted by
    `write_probe`) and is bind-mounted whole by `ContainerBackend`. `results_dir` is where
    THIS FUNCTION's own `probe_results.json` side effect is written — a later invocation's
    trusted record, per `harness.state.control_dir`'s own contract — and must never be
    reachable from inside a container. Defaulting `results_dir` to `out_dir` when the
    caller gave one preserves every existing explicit-`out_dir` caller's behaviour
    unchanged (a per-target caller that wants its own interim copy isolated passes
    `results_dir` explicitly, as `stages/probe.py` now does; the `.../reimplementation`
    fallback callers do not, because that directory is disposable execution workspace by
    design and colocating its own `probe_results.json` there is no different from
    colocating `execution.jsonl`). Only the bare call with NEITHER argument — the primary
    target's own `_run(cfg, root, spec)` — silently wrote into the bind-mounted directory
    before this default changed.
    """
    t0 = time.time()
    results_dir = results_dir or out_dir or state.control_dir(root)
    # A SPEC THAT CLAIMS AN ADMISSIBLE PROVENANCE AND CONTAINS NO PROGRAM IS REFUSED.
    #
    # `write_probe` falls back to DEFAULT_TEMPLATE when a spec carries neither `script`
    # nor `command`, and that template runs two IDENTICAL arms: it measures this machine's
    # seed noise floor and nothing about any paper. It is meant to be reached with
    # provenance `template`, which the ceiling refuses, so its number settles nothing.
    #
    # A hand-written `runs/<pid>/spec.json` declaring `"provenance": "driver"` with neither
    # field reached it too — and `driver` is admissible, so the noise floor's own accuracy
    # was reconciled against the paper's printed cell and was eligible to produce
    # FAILED_REPRODUCTION. `reconcile` cannot catch it: the ceiling is the only guard it
    # applies before the identity gate, and this spec passes the ceiling by assertion.
    # `overall_verdict` could not catch it either, because nothing there reads
    # `ProbeResult.calibration`. So a review could be RED on the harness's own calibration
    # run against a paper nobody had executed.
    #
    # Refused here rather than downgraded, because there is nothing to downgrade: a spec
    # that says it is a human reproduction of the paper's method and contains no program
    # is malformed, not weak. `report.overall_verdict` refuses a calibration
    # reconciliation as well, so the hole is closed on both sides of the seam.
    if provenance_mod.admits(spec.provenance) and not (spec.script or "").strip() \
            and not spec.command:
        blocked_auth = ExecAuthorization(
            allowed=False, decision="spec_incomplete",
            detail=(f"this spec declares provenance '{spec.provenance}', which the "
                    f"provenance ceiling admits, and carries neither a script nor a "
                    f"command. There is no program here to attribute to the authors or to "
                    f"a reviewer: running it would execute this harness's identical-arms "
                    f"noise-floor template and then reconcile its number against the "
                    f"paper's printed cell."))
        return _blocked(cfg, root, spec, blocked_auth, round(time.time() - t0, 1),
                        verify_execution_commit(spec, backend), results_dir=results_dir)
    backend = backend or backend_for(cfg, spec)
    commit = verify_execution_commit(spec, backend)
    auth = authorize(cfg, spec, backend, commit=commit)
    if not auth.allowed or backend is None:
        return _blocked(cfg, root, spec, auth, round(time.time() - t0, 1), commit,
                        results_dir=results_dir)

    script = write_probe(root, spec, out_dir)
    out_dir = script.parent

    # The directory a backend that runs ELSEWHERE would need staged — `runs/<pid>`,
    # which covers `repo/`, `env/` and every `targets/<id>/reimplementation` output area
    # under it. `stage()` is a no-op for a backend that already runs where this process
    # does; only `ContainerBackend` overrides it. See `ExecutionBackend.stage`.
    mount_root = str(root / "runs" / spec.paper_id)

    records: list[ExecutionRecord] = []
    mislabelled: list[str] = []
    per_seed: dict[str, dict[int, float]] = {a: {} for a in spec.arms}
    aux_seed: dict[str, dict[str, dict[int, float]]] = {}
    device, failed, log = "unknown", [], []
    first_failure = ""
    # Startup evidence is accumulated across every (seed, arm) attempt, taking the most
    # favourable observation: if ANY attempt demonstrably reached the experiment, the
    # command is not a setup failure, and a later crash is about the code rather than
    # about this machine.
    evidence = StartupEvidence()
    for seed in spec.seeds:
        for arm in spec.arms:
            cmd, cwd = resolve_command(cfg, spec, script, seed, arm)
            cmd, cwd_staged = backend.stage(cmd, str(cwd), mount_root, pid=spec.paper_id)
            p = backend.execute(ExecRequest(argv=cmd, cwd=cwd_staged,
                                            timeout_s=cfg.probe_timeout_s,
                                            label=f"seed={seed} arm={arm}"))
            # Recorded BEFORE anything is parsed out of it, and for every ending. A record
            # written only on failure cannot answer "what produced this number", which is
            # the one question a reproduction verdict has to survive.
            record = ExecutionRecord(
                seed=seed, arm=arm, backend=p.backend, argv=p.argv, cwd=p.cwd,
                launch_argv=list(getattr(p, "launch_argv", []) or []),
                environment=p.environment, interpreter=spec.interpreter,
                provenance=spec.provenance,
                # The commit identifies the authors' code, so it is written when the
                # authors' code is what ran and left blank otherwise. It used to be
                # stamped unconditionally: every synthesized diagnostic in the shipped
                # corpus carries the cloned repository's SHA, which is the one identity
                # that record is not evidence of.
                commit=(spec.commit if spec.provenance == "repo_exec" else ""),
                started_at=p.started_at, ended_at=p.ended_at, seconds=p.seconds,
                launched=p.launched, completed=p.completed, timed_out=p.timed_out,
                returncode=p.returncode, stdout=p.stdout, stderr=p.stderr, error=p.error)
            records.append(record)
            evidence.ran_seconds = max(evidence.ran_seconds, p.seconds)
            if not p.completed:
                # Two different endings arrive here and they are opposite evidence. A
                # timeout means the process was doing something for the whole window; a
                # failure to launch means it never existed, which is this harness failing
                # to start anything and the clearest possible setup failure.
                failed.append(seed)
                evidence.timed_out = evidence.timed_out or p.timed_out
                if not p.launched:
                    evidence.setup_error = evidence.setup_error or "could not start the process"
                first_failure = first_failure or p.error
                log.append({"seed": seed, "arm": arm, "rc": None, "error": p.error})
                continue
            evidence.stdout_lines = max(
                evidence.stdout_lines, sum(1 for ln in (p.stdout or "").splitlines() if ln.strip()))
            saw_metric = False
            for line in (p.stdout or "").splitlines():
                if d := _DEVICE.match(line):
                    device = d.group(1)
                    evidence.saw_contract_line = True
                elif m := _METRIC.match(line):
                    # Keyed by what the HARNESS passed, and accepted only when the process
                    # echoes it back. Keying on the asserted values let ONE process fill
                    # every seed slot: a script ignoring --seed and printing three
                    # seed=/value= lines produced seeds_run=[0,1,2] and a fabricated
                    # spread of 0.01, which then passed the `std <= 0` guard that exists
                    # precisely to catch a pipeline the seed does not perturb, and earned
                    # RESOLVED_VERIFIED from a single execution.
                    #
                    # A mismatch is recorded rather than silently dropped: a repository
                    # that labels its output differently is a metric-binding problem to
                    # report, not a measurement to accept.
                    if m.group(1) != arm or int(m.group(2)) != seed:
                        # NOT reach evidence either. Setting `saw_contract_line` before this
                        # check let a line the harness had just refused as "not a
                        # measurement to accept" still satisfy `reached_experiment`, so the
                        # same fabricated output that could no longer fill a seed slot could
                        # still license a FAILED_REPRODUCTION.
                        mislabelled.append(
                            f"seed={seed} arm={arm} was passed, but the process reported "
                            f"seed={m.group(2)} arm={m.group(1)}")
                        continue
                    per_seed.setdefault(arm, {})[seed] = float(m.group(3))
                    record.metric = float(m.group(3))
                    saw_metric = True
                    evidence.saw_contract_line = True
                elif x := _AUX.match(line):
                    # Same rule as SH_METRIC, for the same reason: an auxiliary series keyed
                    # on the arm and seed the PROCESS asserts let one process fill the whole
                    # aux table, and the report renders those numbers.
                    if x.group(2) != arm or int(x.group(3)) != seed:
                        mislabelled.append(
                            f"seed={seed} arm={arm} was passed, but an SH_AUX line reported "
                            f"seed={x.group(3)} arm={x.group(2)}")
                        continue
                    aux_seed.setdefault(x.group(1), {}).setdefault(
                        arm, {})[seed] = float(x.group(4))
                    evidence.saw_contract_line = True
            # A third-party repository owes this harness nothing, so when it prints no
            # SH_METRIC line fall back to a JSON summary on stdout. Only for the
            # command path: a generated probe that skipped its own contract is a bug
            # in the probe, and papering over it would hide that.
            metric_captured = saw_metric
            if spec.command and not saw_metric:
                # The key comes from MetricIdentity, and ONLY from it — `strict=True`
                # means no generic key list. Without a bound key there is nothing here:
                # falling back to a "value"/"score"/"mean" scan is exactly the guess C6
                # forbids, because it can silently mine a DIFFERENT quantity than the one
                # the cited cell reports and the reconciler is authorized to compare.
                bound_key = (spec.metric_identity.output_key
                             if spec.metric_identity and spec.metric_identity.established else "")
                # Target-aware, and refusing rather than guessing. The hint comes only from
                # a configuration identity that was actually ESTABLISHED — `spec.dataset`
                # would be wrong here, because it defaults to the noise-floor template's
                # own toy dataset and would match a repository's output object for reasons
                # that have nothing to do with the cited cell.
                hint = " ".join(
                    v for v in (spec.configuration.matched or {}).values()
                    if isinstance(v, str)) if (
                        spec.configuration and spec.configuration.established) else ""
                parsed = parse_metric(p.stdout or "", bound_key, hint)
                if parsed.authoritative and parsed.value is not None:
                    per_seed.setdefault(arm, {})[seed] = parsed.value
                    record.metric = parsed.value
                    evidence.saw_json_metric = True
                    metric_captured = True
                elif parsed.ambiguous:
                    # The run produced numbers and none of them is identifiably THE one.
                    # Recorded on the attempt so the report can say so, and deliberately
                    # NOT captured: an ambiguous metric reconciled against a printed cell
                    # is the positional coincidence this refuses to make.
                    mislabelled.append(parsed.detail)
                    log.append({"seed": seed, "arm": arm, "rc": p.returncode,
                                "metric_ambiguous": parsed.detail,
                                "candidates": [v for _, v in parsed.candidates]})
            if p.returncode != 0:
                err = (p.stderr or "").strip()[-400:]
                if metric_captured:
                    # The experiment already reported its result for this (seed, arm)
                    # before the process exited non-zero — a post-measurement condition
                    # (cleanup, telemetry, a sync barrier), not a failed run. Exit-code
                    # semantics must not override a scientific result that was already
                    # captured; the exit code itself still lives on `record.returncode`
                    # for anyone auditing this attempt.
                    log.append({"seed": seed, "arm": arm, "rc": p.returncode, "error": err,
                               "note": "metric captured before this non-zero exit; not "
                                       "counted as a failed attempt"})
                else:
                    failed.append(seed)
                    # Classified PER ATTEMPT, and unconditionally. The suppression that used
                    # to live here — skip the classifier once any attempt had emitted a
                    # contract line — made the verdict depend on the order the seed loop
                    # happened to run in. Same repository, same missing package, same seeds:
                    # succeed-then-fail gave FAILED_REPRODUCTION and RED, fail-then-succeed
                    # gave INCONCLUSIVE and GREEN. No principle makes both right, so it was
                    # not encoding a judgement about the failure; it was encoding which seed
                    # came first. A dependency, an argv or a platform failure on ANY attempt
                    # means the run was not a fair attempt, whichever seed hit it. Infra
                    # failures (OOM, driver, disk, a killed process) are classified the same
                    # way, independent of order, and independent of `setup_error` — either
                    # can be present without the other.
                    sig = classify_setup_error(p.stderr or "")
                    infra_sig = classify_infra_failure(p.stderr or "", p.returncode)
                    evidence.setup_error = evidence.setup_error or sig
                    evidence.infra_error = evidence.infra_error or infra_sig
                    first_failure = first_failure or f"exit {p.returncode}: {err[-200:]}"
                    log.append({"seed": seed, "arm": arm, "rc": p.returncode, "error": err})

    # One commit verification, made before this loop started, does not describe every
    # attempt inside it: a multi-seed run against a real repository can run for minutes,
    # and nothing above prevents the checkout from being modified underneath it while it
    # does. Re-verified once more now that every attempt has finished; if the commit no
    # longer verifies, the whole run is retracted to INCONCLUSIVE rather than reconciled
    # as though every measurement gathered under it came from one unchanging commit —
    # reusing the authorization precondition `reconcile` already enforces, rather than a
    # second, separate refusal path.
    if spec.command and records:
        commit_after = verify_execution_commit(spec)
        if commit_after is not None and commit_after.state != "verified":
            auth = ExecAuthorization(
                allowed=False, decision="commit_changed_during_execution", backend=auth.backend,
                failure_class="commit_mismatch",
                detail=(f"the checkout no longer verifies as the audited commit after "
                        f"execution ({commit_after.reason}); the repository changed while "
                        f"{len(records)} process(es) ran against it, so nothing gathered "
                        f"under this run can be attributed to one unchanging commit"))
            commit = commit_after

    arms = {a: _stats([per_seed.get(a, {})[s] for s in sorted(per_seed.get(a, {}))])
            for a in spec.arms}
    ok = [a for a in spec.arms if arms[a].n >= 2]
    # No driver script means the default template ran, and the default template's arms
    # are identical by construction. Recording that as a fact on the result keeps the
    # renderer from having to infer it from whether a claimed delta happens to be set.
    calibration = not (spec.script or "").strip() and not spec.command
    result = ProbeResult(
        paper_id=spec.paper_id, finding_id=spec.finding_id, claim=spec.claim,
        device=device, seeds_run=sorted({s for v in per_seed.values() for s in v}),
        seeds_failed=sorted(set(failed)), arms=arms, claimed_delta=spec.claimed_delta,
        calibration=calibration, provenance=spec.provenance, mechanism=spec.mechanism,
        rationale=spec.rationale,
        aux={key: {arm: _stats([by_seed[s] for s in sorted(by_seed)])
                   for arm, by_seed in per_arm.items()}
             for key, per_arm in sorted(aux_seed.items())},
        seconds=round(time.time() - t0, 1), script_path=str(script),
        capability=spec.capability, experiment=spec.experiment,
        metric_identity=spec.metric_identity, configuration=spec.configuration,
        backend=backend.name, authorization=auth,
        resources=spec.resources, commit_verification=commit,
        backend_selection=spec.backend_selection, backend_considered=spec.backend_considered,
        commit_state=spec.commit_state,
    )

    if len(ok) < len(spec.arms):
        result.verdict = "failed"
        extra = (f" {len(mislabelled)} metric line(s) were discarded for naming a seed or arm "
                 f"the harness did not request: {mislabelled[0]}." if mislabelled else "")
        result.reason = (f"only {len(ok)}/{len(spec.arms)} arms produced >=2 seeds; "
                         f"{len(set(failed))} seed-run(s) failed.{extra} See probe_log.json.")
    else:
        std, how = _noise(per_seed, spec.arms)
        delta = arms[spec.arms[-1]].mean - arms[spec.arms[0]].mean
        result.measured_delta = round(delta, 6)
        result.measured_std = round(std, 6)
        result.noise_band = round(2 * std, 6)
        if std <= 0:
            # No noise estimate means no detectability test. Calling this "detectable"
            # would turn a broken probe into a confident accusation.
            result.verdict = "degenerate"
            result.reason = (
                f"seed-to-seed noise measured as exactly 0 over {len(result.seeds_run)} seeds "
                f"on {device}: the seed is not perturbing the pipeline, so no detectability "
                f"claim can be made. Measured delta was {delta:+.4f}."
            )
        elif calibration:
            # Identical arms. "The measured effect sits inside the noise band" would be
            # true by construction and would read as a finding against the paper, so no
            # detectability call is made at all — only the floor is reported. The
            # claimed-delta test still stands where a delta was supplied: that compares
            # the paper's number against this machine, which is what a floor is for.
            if spec.claimed_delta is not None:
                result.claim_within_noise = abs(spec.claimed_delta) < 2 * std
            result.verdict = "calibration"
            result.reason = (
                f"noise-floor calibration on {device} over {len(result.seeds_run)} seeds: "
                f"std {std:.4f} ({how}), 2-sigma band {2 * std:.4f}. No paper-specific "
                f"script was evaluated, so no claim was reproduced."
            )
        else:
            # Signed delta is kept for the report; the detectability test is on
            # magnitude, because an effect in the wrong direction is not evidence
            # of a gain either.
            result.is_overclaimed = abs(delta) < 2 * std
            if spec.claimed_delta is not None:
                result.claim_within_noise = abs(spec.claimed_delta) < 2 * std
            result.verdict = "within_noise" if result.is_overclaimed else "detectable"
            result.reason = (
                f"measured delta {delta:+.4f} vs 2-sigma noise band {2 * std:.4f} "
                f"(std {std:.4f}, {how}) on {device} over {len(result.seeds_run)} seeds"
            )

    # S3d — reconcile against the cited cell, but only when a cell was actually cited.
    # A probe with no `table_ref` was never pointed at a number, so producing a
    # reconciliation for it would be inventing the thing being checked.
    if spec.claim_ref or spec.table_ref or spec.claimed_cell_value:
        repro_arm = spec.arms[-1] if spec.arms else ""
        measured = per_seed.get(repro_arm) or {}
        if not measured:
            measured = {s: v for a in per_seed for s, v in per_seed[a].items()}
        result.reconciliation = reconcile(
            spec, [measured[s] for s in sorted(measured)], result.noise_band,
            # `first_failure` is passed whenever some (seed, arm) attempt exited non-zero
            # WITHOUT having already reported its metric — a genuine gap in the measured
            # set, before or during the experiment. An attempt that printed its metric and
            # THEN exited non-zero (cleanup, telemetry, a sync barrier) is excluded from
            # this by `run_probe`'s own loop, on purpose: the measurement it reported is
            # already in `measured`, so a post-completion exit code never overrides a
            # scientific result that was already captured. `reconcile` already gates a
            # genuine failure through capability and startup evidence; it simply has to be
            # told there was one.
            sorted(measured), failure=first_failure,
            evidence=evidence, authorization=auth)

    # One JSON object per line rather than one array: a run that dies mid-loop leaves the
    # attempts it did make readable, and a long stdout does not have to be held in memory
    # alongside every other attempt's.
    if records:
        result.executions = len(records)
        result.execution_log = str(out_dir / "execution.jsonl")
        with (out_dir / "execution.jsonl").open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r.model_dump(), ensure_ascii=False) + "\n")

    state.write_json(results_dir / "probe_results.json", result.model_dump())
    if log:
        state.write_json(out_dir / "probe_log.json", log)
    return result


if __name__ == "__main__":  # self-check: python -m harness.local_exec
    import tempfile

    cfg = Config.load()
    with tempfile.TemporaryDirectory() as td:
        spec = ProbeSpec(paper_id="selfcheck", seeds=[0, 1, 2], epochs=12,
                         claim="self-check: the default template measures the noise floor",
                         claimed_delta=0.002)
        res = run_probe(cfg, Path(td), spec)
        print(json.dumps(res.model_dump(exclude={"arms"}), indent=2))
        assert res.verdict != "failed", res.reason
        assert res.arms["baseline"].n == 3 and res.arms["treatment"].n == 3
        assert res.calibration is True, "no script means the default identical-arms template"
        assert res.verdict == "calibration", "a calibration run must not render a paper verdict"
        assert res.is_overclaimed is None, "identical arms accuse nobody of anything"
        assert res.measured_delta == 0.0, "identical arms must give exactly zero delta"
        assert res.measured_std > 0, "arm spread must still expose the machine noise floor"
        assert res.claim_within_noise is True, "a 0.002 claimed delta is below this noise floor"
        assert Path(res.script_path).exists()
        assert res.reconciliation is None, "no cell was cited, so nothing may be reconciled"

    # --- S3d reconciliation arithmetic ------------------------------------------------
    from .artifacts import ConfigurationIdentity, ExperimentIdentity, MetricIdentity

    _EST = dict(state="established", reason="self-check fixture")

    def _spec(cell: str, provenance: str = "repo_exec", identity: bool = True) -> ProbeSpec:
        """`identity=True` mirrors what `plan_execution` actually produces: it only ever
        sets provenance to 'repo_exec' once experiment/metric/configuration identity is
        established, so that is the realistic fixture for testing the ARITHMETIC below in
        isolation. `identity=False` is the C4 adversarial case — see the provenance-ceiling
        block further down."""
        spec = ProbeSpec(paper_id="r", table_ref="T1:r0:c1", claimed_cell_value=cell,
                         provenance=provenance)
        if identity:
            spec.experiment = ExperimentIdentity(**_EST)
            spec.metric_identity = MetricIdentity(**_EST)
            spec.configuration = ConfigurationIdentity(**_EST)
        return spec

    assert parse_cell_number("12.196 ± 0.207") == 12.196, "the reported value leads the cell"
    assert parse_cell_number("80.5%(161)") == 80.5
    assert parse_cell_number("n/a") is None
    assert json_metric('noise\n{"epochs": 90}\n{"accuracy": 0.91}\n') == 0.91
    assert json_metric('{"lr": 0.1}') is None, "a hyperparameter blob is not a metric"

    # --- target-aware metric binding ---------------------------------------------------
    one = parse_metric('{"eval_accuracy": 0.87}', "eval_accuracy")
    assert one.authoritative and one.value == 0.87 and one.tier == "identity"

    two = parse_metric('{"eval_accuracy": 0.81}\n{"eval_accuracy": 0.87}', "eval_accuracy")
    assert two.ambiguous and two.value is None and not two.authoritative, (
        "two numbers under one key at one tier is an ambiguity, not a tie to be broken")

    # a split-bound object outranks a bare one, and the ambiguity disappears
    bound = parse_metric('{"eval_accuracy": 0.81}\n{"split":"cifar100-test","eval_accuracy":0.87}',
                         "eval_accuracy", "cifar100-test resnet")
    assert bound.authoritative and bound.value == 0.87 and bound.tier == "target_bound"

    nested = parse_metric('{"results": {"eval_accuracy": 0.9}}', "eval_accuracy")
    assert nested.authoritative and nested.tier == "structured"

    assert not parse_metric('{"eval_accuracy": 0.9}', "").authoritative, \
        "no bound key means no authoritative parse, whatever stdout says"
    assert not parse_metric('{"loss": 0.2}', "eval_accuracy").authoritative

    ok = reconcile(_spec("59.28"), [59.30, 59.26], 0.10, [0, 1])
    assert ok.status == "RESOLVED_VERIFIED", ok.reason
    bad = reconcile(_spec("59.28"), [64.10, 64.20], 0.10, [0, 1])
    assert bad.status == "FAILED_REPRODUCTION", bad.reason
    assert bad.delta_error is not None and bad.delta_error > bad.noise_band, bad.reason
    # A crash convicts only once the experiment is shown to have begun. An unresolved
    # import is a missing dependency on THIS machine, and reading it as a failed
    # reproduction turned an unbuilt environment into RED against the authors.
    crash = reconcile(_spec("59.28"), [], 0.10, [], failure="exit 1: ModuleNotFoundError",
                      evidence=StartupEvidence(setup_error="modulenotfounderror"))
    assert crash.status == "INCONCLUSIVE", "an import that never resolved is not a reproduction"
    assert crash.failure_class == "startup_failure", crash.failure_class
    ran = reconcile(_spec("59.28"), [], 0.10, [], failure="exit 1: RuntimeError: NaN loss",
                    evidence=StartupEvidence(saw_contract_line=True, stdout_lines=20,
                                             ran_seconds=90.0))
    assert ran.status == "FAILED_REPRODUCTION", "a crash after the experiment began still convicts"
    assert ran.failure_class == "runtime_failure", ran.failure_class
    blocked = reconcile(
        _spec("59.28"), [], 0.10, [], failure="exit 1: ImportError",
        evidence=StartupEvidence(),
        # capability decided before execution: this machine could not mount the run
    )
    assert blocked.status == "INCONCLUSIVE", "no startup evidence means no conviction"
    units = reconcile(_spec("97.0"), [0.9684, 0.9690], 0.01, [0, 1])
    assert units.status == "INCONCLUSIVE", "a 100x units mismatch must never convict a paper"
    silent = reconcile(_spec("59.28"), [], 0.10, [])
    assert silent.status == "INCONCLUSIVE", "no parsed metric means no verdict"
    flat = reconcile(_spec("59.28"), [64.0, 64.0], 0.0, [0, 1])
    assert flat.status == "INCONCLUSIVE", "a zero noise band has nothing to test against"

    # --- the provenance ceiling -------------------------------------------------------
    # The same arithmetic that convicts under `repo_exec` must NOT convict under a probe
    # this harness wrote itself, in either direction.
    synth_bad = reconcile(_spec("59.28", "synthesized"), [64.10, 64.20], 0.10, [0, 1])
    assert synth_bad.status == "INCONCLUSIVE", "a toy reimplementation may not convict a cell"
    assert synth_bad.delta_error is not None, "but the arithmetic is still recorded"
    synth_ok = reconcile(_spec("59.28", "synthesized"), [59.30, 59.26], 0.10, [0, 1])
    assert synth_ok.status == "INCONCLUSIVE", "nor may it acquit one by landing nearby"
    assert synth_ok.provenance == "synthesized"
    tmpl = reconcile(_spec("59.28", "template"), [59.30, 59.26], 0.10, [0, 1])
    assert tmpl.status == "INCONCLUSIVE", "the identical-arms template reconciles nothing"
    drv = reconcile(_spec("59.28", "driver"), [59.30, 59.26], 0.10, [0, 1])
    assert drv.status == "RESOLVED_VERIFIED", "a human-written repro still counts, once identity holds"

    # --- C4: provenance alone is not enough — identity must be established too ---------
    # A driver spec.json can carry a `script` with no `command` at all, so gating the
    # identity check on `spec.command` (the old condition) let it skip straight past this
    # check. Neither direction is trusted without the same chain a repo_exec spec needs.
    naive = reconcile(_spec("59.28", "driver", identity=False), [59.30, 59.26], 0.10, [0, 1])
    assert naive.status == "INCONCLUSIVE", "a driver spec with no identity chain may not acquit"
    assert naive.failure_class in ("experiment_unidentified", "metric_unbound",
                                   "configuration_unmatched"), naive.failure_class
    naive_bad = reconcile(_spec("59.28", "driver", identity=False), [64.10, 64.20], 0.10, [0, 1])
    assert naive_bad.status == "INCONCLUSIVE", "a driver spec with no identity chain may not convict either"
    naive_repo = reconcile(_spec("59.28", "repo_exec", identity=False), [59.30, 59.26], 0.10, [0, 1])
    assert naive_repo.status == "INCONCLUSIVE", "repo_exec without identity established may not reconcile"

    # --- C10: printed precision, not raw digits, decides whether a delta agrees --------
    # The same underlying reproduced value must not flip status solely because the paper
    # printed one extra decimal place for the claimed cell.
    coarse = reconcile(_spec("59.3"), [59.2787, 59.2787], 0.001, [0, 1])
    fine = reconcile(_spec("59.28"), [59.2787, 59.2787], 0.001, [0, 1])
    assert coarse.status == "RESOLVED_VERIFIED", coarse.reason
    assert fine.status == "RESOLVED_VERIFIED", fine.reason
    assert coarse.claimed_precision == 0.05 and fine.claimed_precision == 0.005
    integer_cell = reconcile(_spec("59"), [59.2787, 59.2787], 0.001, [0, 1])
    assert integer_cell.status == "RESOLVED_VERIFIED", integer_cell.reason
    # A genuinely wrong reproduction is not rescued by precision credit.
    wrong = reconcile(_spec("59.3"), [70.0, 70.0], 0.001, [0, 1])
    assert wrong.status == "FAILED_REPRODUCTION", wrong.reason

    # --- SH_AUX contract ---------------------------------------------------------------
    m = _AUX.match("SH_AUX key=linear_probe_acc arm=ldreg seed=3 value=0.914000")
    assert m and m.groups() == ("linear_probe_acc", "ldreg", "3", "0.914000")
    assert _AUX.match("SH_AUX key=k arm=a seed=0") is None, "a malformed aux line is ignored"
    print("local_exec self-check OK")
