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
import time
from pathlib import Path

from .artifacts import (ArmStats, CommitVerification, ExecAuthorization, ProbeResult,
                        ProbeSpec, Reconciliation)
from .backends import ExecRequest, ExecutionBackend, authorize, backend_for
from .repo import verify_commit
from .experiment_id import identities_established
from .config import Config

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


def write_probe(root: Path, spec: ProbeSpec) -> Path:
    """Materialize `runs/<paper_id>/probe.py`. Returns its path.

    When `spec.command` is set the code under test is the paper's own checkout, so
    nothing is generated — the returned path is only used to locate the run directory.
    """
    path = root / "runs" / spec.paper_id / "probe.py"
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
    """The magnitude a table cell reports, or None if the cell holds no number."""
    m = _LEADING_NUMBER.search((text or "").replace(",", ""))
    return float(m.group()) if m else None


def json_metric(stdout: str, metric: str = "") -> float | None:
    """Last JSON object on stdout that carries a usable metric, as a float.

    The fallback for repositories that do not implement the SH_METRIC contract. Scans
    from the end so a final summary wins over per-epoch logging, and only accepts keys
    that actually name a metric — a JSON blob full of hyperparameters must not be
    mined for whichever number happens to parse.
    """
    keys = ([metric.lower()] if metric else []) + list(_JSON_METRIC_KEYS)
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        lowered = {str(k).lower(): v for k, v in data.items()}
        for key in keys:
            v = lowered.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
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

    def describe(self) -> str:
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


def reached_experiment(evidence: StartupEvidence) -> bool:
    """Did the run get past setup into the experiment the paper describes?

    The burden of proof is on establishing that it did. An unproven start yields
    INCONCLUSIVE, which accuses nobody; assuming a start yields FAILED_REPRODUCTION, which
    drives RED and names the authors. Those errors are not symmetric, so the default is
    the one that cannot manufacture an accusation.
    """
    if evidence.setup_error:
        return False                                        # decisive against
    if evidence.saw_contract_line or evidence.saw_json_metric:
        return True                                         # decisive for
    long_enough = evidence.ran_seconds >= _STARTUP_WINDOW_S
    if evidence.timed_out and long_enough:
        return True                                         # killed while working
    return long_enough and evidence.stdout_lines >= _MIN_OUTPUT_LINES


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
            f"{spec.table_ref or 'the cited cell'}."
        )
        return rec

    if values:
        rec.reproduced_value = round(statistics.fmean(values), 6)
        rec.reproduced_std = round(statistics.stdev(values), 6) if len(values) > 1 else 0.0
    if rec.claimed_value is not None and rec.reproduced_value is not None:
        rec.delta_error = round(abs(rec.reproduced_value - rec.claimed_value), 6)

    # --- the provenance ceiling ---------------------------------------------------
    # A synthesized probe is OUR reimplementation of the paper's formulation, run at toy
    # scale on synthetic data. Its number is real and its noise band is real, but it is
    # a measurement of the MECHANISM, not of the benchmark the cell reports, so it is not
    # entitled to either verdict. Letting it convict would be the same unearned inference
    # this harness exists to catch other people making — and letting it ACQUIT would be
    # worse, because a toy that happens to land near the printed number would launder a
    # claim nobody checked. Only code the authors wrote may reconcile against their cell.
    if spec.provenance not in ("driver", "repo_exec"):
        rec.status = "INCONCLUSIVE"
        near = ("" if rec.delta_error is None else
                f" (|delta| {rec.delta_error:.4f} against a 2-sigma band of {noise_band:.4f})")
        rec.reason = (
            f"the probe that ran was {spec.provenance}, not the paper's own code: "
            f"{'a mechanism reimplementation at toy scale' if spec.provenance == 'synthesized' else 'the identical-arms noise-floor template'}. "
            f"Its result{near} is evidence about the mechanism, not a reproduction of the "
            f"value printed at {spec.table_ref or 'the cited cell'}, so no reproduction "
            f"verdict is drawn. Open SH_ALLOW_REPO_EXEC to reconcile against the authors' code."
        )
        return rec

    # --- the identity precondition ------------------------------------------------
    # Capability asks whether the code CAN run. Identity asks whether running it answers
    # the question. A capable run of the wrong program produces a confident irrelevant
    # number, which is more dangerous than a crash because nothing looks wrong. This gate
    # applies to success and failure alike, and sits after the provenance ceiling so a
    # synthesized probe still reports the ceiling rather than an identity class.
    rec.experiment_state = spec.experiment.state if spec.experiment else "unmapped"
    rec.metric_state = spec.metric_identity.state if spec.metric_identity else "unmapped"
    rec.configuration_state = spec.configuration.state if spec.configuration else "unmapped"
    if spec.command:                       # only a repository run needs an identity chain
        proven, failure_class, why = identities_established(
            spec.experiment, spec.metric_identity, spec.configuration)
        if not proven:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = failure_class
            rec.reason = (
                f"the executed program was not bound to the cited cell, so its output cannot "
                f"be compared with {spec.table_ref or 'it'}: {why}. A run that succeeds without "
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

        reached = reached_experiment(evidence)
        rec.reached_experiment = reached
        if not reached:
            rec.status = "INCONCLUSIVE"
            rec.failure_class = "timeout" if evidence.timed_out else "startup_failure"
            rec.reason = (
                f"the process exited before any sign that the experiment itself began "
                f"({evidence.describe()}): {failure}. The environment was capable, so this may yet "
                f"be a defect in the paper's code — but nothing here distinguishes that from a "
                f"setup failure, and the burden of showing the experiment ran is on this harness."
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
                      f"{spec.table_ref or '(none cited)'}, so there is no claim to reconcile")
        return rec

    ratio = _scale_ratio(rec.claimed_value, rec.reproduced_value)
    if 50 <= ratio <= 200 or 0.005 <= ratio <= 0.02:
        rec.status = "INCONCLUSIVE"
        rec.reason = (f"the cell reads {rec.claimed_value:g} and the run produced "
                      f"{rec.reproduced_value:g}, a factor of about {ratio:.0f}x. That is a "
                      f"percentage-versus-fraction units mismatch in this harness, and it "
                      f"would be dishonest to score it as a failed reproduction.")
        return rec

    if noise_band <= 0:
        rec.status = "INCONCLUSIVE"
        rec.reason = (f"seed-to-seed noise measured as zero over {len(rec.seeds_run)} seeds, so "
                      f"the `<= 2 sigma` test has no band to test against. Delta was "
                      f"{rec.delta_error:.4f}.")
    elif rec.delta_error <= noise_band:
        rec.status = "RESOLVED_VERIFIED"
        rec.reason = (f"reproduced {rec.reproduced_value:g} against the cell's "
                      f"{rec.claimed_value:g}: |delta| {rec.delta_error:.4f} is within the "
                      f"2-sigma band {noise_band:.4f}. The printed number stands.")
    else:
        rec.status = "FAILED_REPRODUCTION"
        rec.reason = (f"reproduced {rec.reproduced_value:g} against the cell's "
                      f"{rec.claimed_value:g}: |delta| {rec.delta_error:.4f} exceeds the "
                      f"2-sigma band {noise_band:.4f} over {len(rec.seeds_run)} seeds.")
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


def verify_execution_commit(spec: ProbeSpec) -> CommitVerification | None:
    """Re-check, at the moment of execution, that the checkout is the audited commit.

    Returns None for a probe this harness authored — there is no repository involved, so
    there is no commit to be wrong about. For a repository run it reads the working tree
    on disk NOW rather than trusting the SHA recorded during planning, because the whole
    failure being closed is that planning and execution can see different code: a default
    branch moves, a cached checkout is refreshed, and the second run executes something
    the first run's audit never read while still carrying that audit's findings.
    """
    if not spec.command:
        return None
    return verify_commit(spec.cwd, spec.commit)


def _blocked(cfg: Config, root: Path, spec: ProbeSpec, auth: ExecAuthorization,
             seconds: float, commit: CommitVerification | None = None) -> ProbeResult:
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
    if spec.table_ref or spec.claimed_cell_value:
        result.reconciliation = reconcile(spec, [], 0.0, [], authorization=auth)
    # `script_path` stays empty: no probe was written, and naming a file that does not
    # exist would invite a reader to go looking for the code that ran.
    out_dir = root / "runs" / spec.paper_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe_results.json").write_text(
        json.dumps(result.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def run_probe(cfg: Config, root: Path, spec: ProbeSpec,
              backend: ExecutionBackend | None = None) -> ProbeResult:
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
    """
    t0 = time.time()
    backend = backend or backend_for(cfg, spec)
    commit = verify_execution_commit(spec)
    auth = authorize(cfg, spec, backend, commit=commit)
    if not auth.allowed or backend is None:
        return _blocked(cfg, root, spec, auth, round(time.time() - t0, 1), commit)

    script = write_probe(root, spec)
    out_dir = script.parent

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
            p = backend.execute(ExecRequest(argv=cmd, cwd=str(cwd),
                                            timeout_s=cfg.probe_timeout_s,
                                            label=f"seed={seed} arm={arm}"))
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
                    per_seed.setdefault(m.group(1), {})[int(m.group(2))] = float(m.group(3))
                    saw_metric = True
                    evidence.saw_contract_line = True
                elif x := _AUX.match(line):
                    aux_seed.setdefault(x.group(1), {}).setdefault(
                        x.group(2), {})[int(x.group(3))] = float(x.group(4))
                    evidence.saw_contract_line = True
            # A third-party repository owes this harness nothing, so when it prints no
            # SH_METRIC line fall back to a JSON summary on stdout. Only for the
            # command path: a generated probe that skipped its own contract is a bug
            # in the probe, and papering over it would hide that.
            if spec.command and not saw_metric:
                # The key comes from MetricIdentity when one was established. Scanning the
                # generic key list would re-open the hole this gate closes: it would pick
                # whichever number happened to parse, regardless of what it measures.
                bound_key = (spec.metric_identity.output_key
                             if spec.metric_identity and spec.metric_identity.established else "")
                if (v := json_metric(p.stdout or "", bound_key or spec.metric)) is not None:
                    per_seed.setdefault(arm, {})[seed] = v
                    evidence.saw_json_metric = True
            if p.returncode != 0:
                failed.append(seed)
                err = (p.stderr or "").strip()[-400:]
                # Only the FIRST attempt's signature is kept, and only if no attempt has
                # already shown the experiment running: one seed crashing on an import
                # after another produced metrics is a runtime failure, not a setup one.
                if not evidence.setup_error and not (evidence.saw_contract_line or evidence.saw_json_metric):
                    evidence.setup_error = classify_setup_error(p.stderr or "")
                first_failure = first_failure or f"exit {p.returncode}: {err[-200:]}"
                log.append({"seed": seed, "arm": arm, "rc": p.returncode, "error": err})

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
        result.reason = (f"only {len(ok)}/{len(spec.arms)} arms produced >=2 seeds; "
                         f"{len(set(failed))} seed-run(s) failed. See probe_log.json.")
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
    if spec.table_ref or spec.claimed_cell_value:
        repro_arm = spec.arms[-1] if spec.arms else ""
        measured = per_seed.get(repro_arm) or {}
        if not measured:
            measured = {s: v for a in per_seed for s, v in per_seed[a].items()}
        result.reconciliation = reconcile(
            spec, [measured[s] for s in sorted(measured)], result.noise_band,
            sorted(measured), failure=first_failure if result.verdict == "failed" else "",
            evidence=evidence, authorization=auth)

    (out_dir / "probe_results.json").write_text(
        json.dumps(result.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    if log:
        (out_dir / "probe_log.json").write_text(
            json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
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
    def _spec(cell: str, provenance: str = "repo_exec") -> ProbeSpec:
        return ProbeSpec(paper_id="r", table_ref="T1:r0:c1", claimed_cell_value=cell,
                         provenance=provenance)

    assert parse_cell_number("12.196 ± 0.207") == 12.196, "the reported value leads the cell"
    assert parse_cell_number("80.5%(161)") == 80.5
    assert parse_cell_number("n/a") is None
    assert json_metric('noise\n{"epochs": 90}\n{"accuracy": 0.91}\n') == 0.91
    assert json_metric('{"lr": 0.1}') is None, "a hyperparameter blob is not a metric"

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
    assert drv.status == "RESOLVED_VERIFIED", "a human-written faithful repro still counts"

    # --- SH_AUX contract ---------------------------------------------------------------
    m = _AUX.match("SH_AUX key=linear_probe_acc arm=ldreg seed=3 value=0.914000")
    assert m and m.groups() == ("linear_probe_acc", "ldreg", "3", "0.914000")
    assert _AUX.match("SH_AUX key=k arm=a seed=0") is None, "a malformed aux line is ignored"
    print("local_exec self-check OK")
