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
import subprocess
import time
from pathlib import Path

from .artifacts import ArmStats, ProbeResult, ProbeSpec, Reconciliation
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
_LEADING_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
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


def reconcile(spec: ProbeSpec, values: list[float], noise_band: float,
              seeds_run: list[int], failure: str = "") -> Reconciliation:
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
    """
    rec = Reconciliation(table_ref=spec.table_ref, finding_id=spec.finding_id,
                         metric=spec.metric, claimed_raw=spec.claimed_cell_value,
                         provenance=spec.provenance,
                         noise_band=round(noise_band, 6), seeds_run=sorted(seeds_run))
    rec.claimed_value = parse_cell_number(spec.claimed_cell_value)
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

    if failure:
        rec.status = "FAILED_REPRODUCTION"
        rec.reason = (f"the paper's code did not run to completion: {failure}. A reproduction "
                      f"that crashes is a failed reproduction, not an inconclusive one.")
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


def run_probe(cfg: Config, root: Path, spec: ProbeSpec) -> ProbeResult:
    """Write the probe, run every (arm, seed) in its own process, aggregate."""
    t0 = time.time()
    script = write_probe(root, spec)
    out_dir = script.parent

    per_seed: dict[str, dict[int, float]] = {a: {} for a in spec.arms}
    aux_seed: dict[str, dict[str, dict[int, float]]] = {}
    device, failed, log = "unknown", [], []
    first_failure = ""
    for seed in spec.seeds:
        for arm in spec.arms:
            cmd, cwd = resolve_command(cfg, spec, script, seed, arm)
            try:
                p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=cfg.probe_timeout_s)
            except (subprocess.TimeoutExpired, OSError) as e:
                failed.append(seed)
                why = (f"timeout after {cfg.probe_timeout_s}s"
                       if isinstance(e, subprocess.TimeoutExpired) else f"could not start: {e}")
                first_failure = first_failure or why
                log.append({"seed": seed, "arm": arm, "rc": None, "error": why})
                continue
            saw_metric = False
            for line in (p.stdout or "").splitlines():
                if d := _DEVICE.match(line):
                    device = d.group(1)
                elif m := _METRIC.match(line):
                    per_seed.setdefault(m.group(1), {})[int(m.group(2))] = float(m.group(3))
                    saw_metric = True
                elif x := _AUX.match(line):
                    aux_seed.setdefault(x.group(1), {}).setdefault(
                        x.group(2), {})[int(x.group(3))] = float(x.group(4))
            # A third-party repository owes this harness nothing, so when it prints no
            # SH_METRIC line fall back to a JSON summary on stdout. Only for the
            # command path: a generated probe that skipped its own contract is a bug
            # in the probe, and papering over it would hide that.
            if spec.command and not saw_metric:
                if (v := json_metric(p.stdout or "", spec.metric)) is not None:
                    per_seed.setdefault(arm, {})[seed] = v
            if p.returncode != 0:
                failed.append(seed)
                err = (p.stderr or "").strip()[-400:]
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
            sorted(measured), failure=first_failure if result.verdict == "failed" else "")

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
    crash = reconcile(_spec("59.28"), [], 0.10, [], failure="exit 1: ModuleNotFoundError")
    assert crash.status == "FAILED_REPRODUCTION", "code that will not run is a failed reproduction"
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
