"""Do the seeded replicates of a stage measure different things?

Equal summary values do not say the runs were the same experiment: an exact-recovery indicator
is 1 in every replicate of a working method, and a false-alarm rate is 0 in every replicate of a
valid test. The old rule read "the compared value repeated" as "the seed did not vary the run"
and decided nothing. The evidence is the runs themselves:

  - the OTHER outputs of a stage's result lines (coverage, counts, sizes) differ between seeds;
  - the script's own fingerprint of the data it generated (`data_fingerprint`) differs;
  - or, when neither is available, the script demonstrably feeds `--seed` to a random generator
    (a static flow check), so identical summaries are draws that coincided.

Only when the lines are identical AND nothing shows the seed reaches a generator (or a
declared fingerprint is identical) are the runs one measurement. A time-like output is ignored:
it differs between two runs of the same experiment.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re

_SINKS = {"default_rng", "randomstate", "seed", "manual_seed", "manual_seed_all", "seedsequence", "pcg64",
          "mt19937", "philox", "sfc64", "prngkey", "set_seed", "seed_everything"}
_SINK_KW = {"random_state", "seed", "rng", "generator", "random_seed", "seeds"}
_TIMEY = re.compile(r"time|sec|elapsed|duration|latency|wall|runtime|clock|stamp", re.I)


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
    body = [{k: v for k, v in sorted(r.get("out", {}).items()) if not _TIMEY.search(k)} for r in rows]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def assess(detail: list[dict] | None, stage: str, reading: str, rng: bool | None) -> dict:
    """The state of one stage's replicates: `different` (shown), `seeded` (identical summaries, but the
    seed reaches a random generator), `identical` (a declared data fingerprint repeats), `unproven`
    (identical lines and nothing showing the seed varied them) or `single`/`unknown`.
    `independent` is true for `different` and `seeded`."""
    by_seed: dict[int, list[dict]] = {}
    for r in detail or []:
        if r.get("stage", "") == stage and r.get("reading", "") == reading:
            by_seed.setdefault(int(r["seed"]), []).append(r)
    if not detail:
        return {"state": "unknown", "independent": False, "n": 0, "basis": "no per-run result lines were recorded"}
    if len(by_seed) < 2:
        return {"state": "single", "independent": False, "n": len(by_seed), "basis": "fewer than two seeds"}
    declared = {s: tuple(sorted(str(r["data_fp"]) for r in rs if r.get("data_fp"))) for s, rs in by_seed.items()}
    if all(declared.values()):
        k = len(set(declared.values()))
        return {"state": "different" if k > 1 else "identical", "independent": k > 1, "n": len(by_seed),
                "distinct": k, "basis": f"the script's own data fingerprints: {k} distinct in {len(by_seed)} seeds"}
    lines = {s: _fp(rs) for s, rs in by_seed.items()}
    k = len(set(lines.values()))
    if k > 1:
        return {"state": "different", "independent": True, "n": len(by_seed), "distinct": k,
                "basis": f"the runs' result lines differ in their outputs ({k} distinct in {len(by_seed)} seeds), "
                         "though the compared value may repeat"}
    if rng:
        return {"state": "seeded", "independent": True, "n": len(by_seed), "distinct": 1,
                "basis": "the result lines are identical, but --seed reaches a random generator in the script "
                         "(a static check): the draws differ and their summary coincided"}
    return {"state": "unproven", "independent": False, "n": len(by_seed), "distinct": 1,
            "basis": "the result lines are identical and nothing shows the seed varied the run"}
