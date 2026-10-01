"""Do the seeded replicates of a stage measure different things?

Equal summary values do not say the runs were the same experiment: an exact-recovery indicator
is 1 in every replicate of a working method, and a false-alarm rate is 0 in every replicate of a
valid test. Nor does a script that feeds `--seed` to some generator say that THIS stage's draws
depend on it (a stage may draw from a fixed generator inside a seeded script). The evidence is the
runs themselves, per stage, at run time:

  - the OTHER outputs of a stage's result lines (coverage, counts, sizes) differ between seeds:
    different runs, whatever the data fingerprints say (fixed acquired data, seeded training);
  - else the script's own fingerprint of the random draws / data that stage consumed
    (`data_fingerprint`, on every result line) differs: draws that differ, a summary that coincided;
  - identical lines with identical fingerprints are one run repeated (one measurement), and
    identical lines without fingerprints are unproven (one measurement, never replicates).

The static seed flow (`seed_flow`) is recorded as context only: it never makes repeated lines
independent replicates. A seed whose per-run lines were not recorded is evidence missing, never
identical. A time-like output (a whole name token such as `seconds`, `elapsed`, `wall_time`) is
ignored: it differs between two runs of the same experiment.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re

_SINKS = {"default_rng", "randomstate", "seed", "manual_seed", "manual_seed_all", "seedsequence", "pcg64",
          "mt19937", "philox", "sfc64", "prngkey", "set_seed", "seed_everything"}
_SINK_KW = {"random_state", "seed", "rng", "generator", "random_seed", "seeds"}
# Whole name tokens of a wall clock (`fit_secs`, `trainTime`, `elapsed_s`); a substring (`intersection_size`,
# `n_timesteps`) is not one. `time` is a token of a wall clock and of a delay counted in samples alike
# (`detection_time`): it stays ignored, the side on which a clock output can never make duplicates "different runs".
_CLOCK = {"time", "times", "sec", "secs", "seconds", "elapsed", "duration", "latency", "wall", "walltime", "wallclock",
          "runtime", "clock", "timestamp", "stamp"}
_TOKENS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def _timey(name: str) -> bool:
    return any(t.lower() in _CLOCK for t in _TOKENS.findall(name))


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def seed_flow(script: str) -> bool | None:
    """Does the value of `--seed` reach a random generator's construction or seeding in `script`?
    True/False from a static flow (assignments, loops, arguments passed to functions of the script);
    None if the script does not parse. A script that never reads its seed is False."""
    try:
        tree = ast.parse(script)
    except (SyntaxError, ValueError):
        return None
    dest = {"seed"}
    manual = False
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument":
            for a in n.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("--") and "seed" in a.value:
                    dest.add(a.value.lstrip("-").replace("-", "_"))
            for k in n.keywords:
                if k.arg == "dest" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
                    dest.add(k.value.value)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value == "--seed":
            manual = True                              # sys.argv parsed by hand: taint what is assigned from it
    funcs = {f.name: f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    names: set[str] = set()

    def tainted(e: ast.AST | None) -> bool:
        return e is not None and (any(isinstance(m, ast.Attribute) and m.attr in dest for m in ast.walk(e))
                                  or bool(_names(e) & names) or (manual and any(
                                      isinstance(m, ast.Constant) and m.value == "--seed" for m in ast.walk(e))))
    for _ in range(6):                                 # ponytail: 6 passes reach any chain of assignments and calls in a script
        before = len(names)
        for n in ast.walk(tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and tainted(getattr(n, "value", None)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    names |= _names(t)
            elif isinstance(n, (ast.For, ast.comprehension)) and tainted(n.iter):
                names |= _names(n.target)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in funcs:
                params = [a.arg for a in funcs[n.func.id].args.args]
                names |= {p for p, a in zip(params, n.args) if tainted(a)} | {k.arg for k in n.keywords if k.arg and tainted(k.value)}
        if len(names) == before:
            break
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id if isinstance(n.func, ast.Name) else ""
        sink = f.lower() in _SINKS or any(w in f.lower() for w in ("seed", "rng")) or f in ("Random", "Generator")
        if (sink and (any(tainted(a) for a in n.args) or any(tainted(k.value) for k in n.keywords))) or any(
                k.arg in _SINK_KW and tainted(k.value) for k in n.keywords):
            return True
    return False


def _fp(rows: list[dict]) -> str:
    """The outputs of one seed's result lines for a stage, less anything time-like."""
    body = [{k: v for k, v in sorted(r.get("out", {}).items()) if not _timey(k)} for r in rows]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def assess(detail: list[dict] | None, stage: str, reading: str, rng: bool | None) -> dict:
    """The state of one stage's replicates, from what the runs printed: `different` (their other outputs
    differ, or their data fingerprints do), `identical` (identical lines and fingerprints: one run repeated),
    `unproven` (identical lines, no fingerprint on every line), `unknown` (a seed's lines were not recorded)
    or `single`. Only `different` is `independent`. `rng` (the static seed flow) is context, never evidence."""
    by_seed: dict[int, list[dict]] = {}
    for r in detail or []:
        if r.get("stage", "") == stage and r.get("reading", "") == reading:
            by_seed.setdefault(int(r["seed"]), []).append(r)
    if not detail:
        return {"state": "unknown", "independent": False, "n": 0, "basis": "no per-run result lines were recorded"}
    lost = sorted(s for s, rs in by_seed.items() if any(r.get("missing") for r in rs))
    if lost:
        return {"state": "unknown", "independent": False, "n": len(by_seed),
                "basis": f"the per-run result lines of seed(s) {lost[:10]} were not recorded, so whether the replicates "
                         "differed is unknown (evidence missing, not identical runs)"}
    if len(by_seed) < 2:
        return {"state": "single", "independent": False, "n": len(by_seed), "basis": "fewer than two seeds"}
    lines = {s: _fp(rs) for s, rs in by_seed.items()}
    k = len(set(lines.values()))
    if k > 1:
        return {"state": "different", "independent": True, "n": len(by_seed), "distinct": k,
                "basis": f"the runs' result lines differ in their outputs ({k} distinct in {len(by_seed)} seeds), "
                         "though the compared value may repeat"}
    fps = {s: tuple(sorted(str(r.get("data_fp") or "") for r in rs)) for s, rs in by_seed.items()}
    flow = (f"; the static seed flow ({'--seed reaches a generator somewhere in the script' if rng else 'none found'}) "
            "is context, not evidence about this stage" if rng is not None else "")
    if all(all(r.get("data_fp") for r in rs) for rs in by_seed.values()):
        k = len(set(fps.values()))
        return {"state": "different" if k > 1 else "identical", "independent": k > 1, "n": len(by_seed), "distinct": k,
                "basis": (f"identical result lines, but the script's own data fingerprints of this stage's draws differ "
                          f"({k} distinct in {len(by_seed)} seeds): draws that differ, a summary that coincided" if k > 1 else
                          f"identical result lines and identical data fingerprints in {len(by_seed)} seeds: one run "
                          "repeated") + flow}
    return {"state": "unproven", "independent": False, "n": len(by_seed), "distinct": 1,
            "basis": "the result lines are identical and not every line carries a `data_fingerprint`, so nothing the "
                     "runs printed shows the seed varied this stage" + flow}
