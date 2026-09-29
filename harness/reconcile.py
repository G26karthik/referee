"""An executed result against the number the paper printed. Arithmetic, not judgement.

The order is the invariant: a refused run reconciles nothing (BLOCKED); an environment or
startup failure is INCONCLUSIVE, never a failed reproduction (invariant 5); a certificate
whose hypothesis assertion failed tried an inadmissible instance, never a counterexample;
a 100x ratio is a units mismatch in this harness; a deterministic run can only agree to
the printed precision, never "fail" without a noise band to fail against; a stated
comparison (a relation) is decided only beyond 2 standard errors of its paired margins.
"""
from __future__ import annotations

import statistics

from .evidence import half_width, parse_value, relation
from .execute import admits

SUPPORT = ("RESOLVED_VERIFIED", "ARITHMETIC_CONSISTENT", "RELATION_HOLDS")
FAILURE = ("FAILED_REPRODUCTION", "COUNTEREXAMPLE_FOUND", "ARITHMETIC_CONTRADICTION", "RELATION_VIOLATED")


def _r(status: str, reason: str, **kw) -> dict:
    return {"status": status, "reason": reason, **kw}


def reconcile(kind: str, printed: str, values: list[float], failure: str, ev: dict,
              seeded: bool, authorized: bool, why: str, rel: str = "") -> dict:
    if not authorized:
        return _r("BLOCKED", f"not run: {why}. A refusal by this harness is not evidence about the paper.")
    if not admits(kind):
        return _r("INCONCLUSIVE", f"'{kind}' is not admissible provenance, so it settles nothing")
    n = len(values)
    if kind == "CERTIFICATE":
        if failure:
            return _r("INCONCLUSIVE", f"the certificate did not complete ({failure[:200]}). A failed hypothesis "
                      "assertion means the instance was not admissible — never a counterexample.")
        if not values:
            return _r("INCONCLUSIVE", "no instance reported a result")
        bad = sum(1 for v in values if v == 1)
        return (_r("COUNTEREXAMPLE_FOUND", f"{bad} of {n} exact-arithmetic instance(s) satisfied every stated "
                   "hypothesis and violated the checked relation", rule="exact arithmetic on admissible instances")
                if bad else
                _r("NO_VIOLATION_FOUND", f"all {n} instance(s) satisfied the checked relation — the tested "
                   "instances only, never a proof", rule="exact arithmetic on admissible instances"))
    if failure:
        if ev.get("infra_error"):
            return _r("INCONCLUSIVE", f"infrastructure failure ('{ev['infra_error']}'), a fact about this host, not "
                      f"the paper: {failure[:200]}")
        if not ev.get("reached"):
            why = ("a setup failure" if ev.get("setup_error") else "no sign the experiment itself began")
            return _r("INCONCLUSIVE", f"{why}: {failure[:200]}. The burden of showing the experiment ran is on "
                      "this harness.")
        if kind == "AUTHOR_CODE" and ev.get("own_code_crash"):
            return _r("FAILED_REPRODUCTION", f"the authors' code reached the experiment and then crashed in its own "
                      f"code ({ev['own_code_crash']}): {failure[:200]}", rule="a run that starts and breaks in its own code")
        return _r("INCONCLUSIVE", f"the run failed ({failure[:200]}), but not by a crash inside the authors' own "
                  "code, so nothing is established about the paper")
    if rel:
        return _relation(kind, rel, values)
    claimed = parse_value(printed)
    if not values or claimed is None:
        return _r("INCONCLUSIVE", "no metric was reported" if not values else f"no number in the printed {printed!r}")
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if n > 1 else 0.0
    out = {"claimed": claimed, "reproduced": round(mean, 6), "std": round(std, 6), "n": n,
           "delta": round(abs(mean - claimed), 6), "precision": half_width(printed)}
    ratio = abs(claimed / mean) if mean else float("inf")
    if 50 <= ratio <= 200 or 0.005 <= ratio <= 0.02:
        return _r("INCONCLUSIVE", f"printed {claimed:g} vs produced {mean:g}: a ~{ratio:.0f}x ratio is a "
                  "percentage-vs-fraction units mismatch, not a failed reproduction", **out)
    eff = max(0.0, out["delta"] - out["precision"])
    tol = out["precision"] + (2 * std if seeded else 0.0) + 1e-9
    if eff > (2 * std if seeded else 1e-12) and any(abs(claimed - (s - mean)) <= tol for s in (1, 100)):
        return _r("INCONCLUSIVE", f"printed {claimed:g} vs produced {mean:g}: the produced value is the complement "
                  "(error vs accuracy), a metric-orientation mismatch, not a failed reproduction", **out)
    note = "" if kind == "AUTHOR_CODE" else " (not the authors' code: a finding about the paper's stated method or data)"
    if not seeded or n < 2 or std == 0:
        rule = "deterministic run: agreement only within the printed precision; a difference is reported, not scored"
        return (_r("RESOLVED_VERIFIED", f"produced {mean:g} vs printed {printed}: within printed precision{note}",
                   rule=rule, **out) if eff <= 1e-12 else
                _r("INCONCLUSIVE", f"produced {mean:g} vs printed {printed}: |delta| {out['delta']:g} exceeds the "
                   f"printed precision, but a deterministic run has no noise band to call it a failure", rule=rule, **out))
    band = 2 * std
    rule = f"mean over {n} seeds within 2 sigma ({band:.4g}) after crediting printed precision"
    return (_r("RESOLVED_VERIFIED", f"produced {mean:g} vs printed {printed}{note}", rule=rule, band=band, **out)
            if eff <= band else
            _r("FAILED_REPRODUCTION", f"produced {mean:g} vs printed {printed}: |delta| {out['delta']:g} exceeds "
               f"2 sigma{note}", rule=rule, band=band, **out))


def _relation(kind: str, rel: str, margins: list[float]) -> dict:
    """A comparison the paper states, over paired results (one per REFEREE_RESULT line):
    decided on the mean margin beyond 2 standard errors. One line decides only when it is an
    exact recomputation from released data; one run of an experiment has no noise band."""
    n, strict = len(margins), relation(rel)[1] in ("<", ">")
    if not n:
        return _r("INCONCLUSIVE", f"no result carried every output the relation {rel!r} names")
    if n == 1 and kind != "RELEASED_DATA":
        return _r("INCONCLUSIVE", "one result of an experiment has no noise band to decide a relation on",
                  relation=rel, margin=margins[0], n=1)
    m = statistics.fmean(margins)
    band = 2 * statistics.stdev(margins) / n ** 0.5 if n > 1 else 0.0
    out = {"relation": rel, "margin": round(m, 6), "band": round(band, 6), "n": n,
           "rule": "mean paired margin beyond 2 standard errors" if n > 1 else "exact recomputation from released data"}
    if m > band or (not strict and band == 0 and m >= 0):
        return _r("RELATION_HOLDS", f"{rel}: mean margin {m:.4g} over {n} result(s)", **out)
    if m < -band or (strict and band == 0 and m == 0):
        return _r("RELATION_VIOLATED", f"{rel} does not hold: mean margin {m:.4g} over {n} result(s)", **out)
    return _r("INCONCLUSIVE", f"{rel}: mean margin {m:.4g} is within 2 standard errors ({band:.4g}) of equality", **out)


def arithmetic(lo: float, hi: float, printed: str) -> dict:
    """The paper's own printed operands, recomputed over their rounding intervals, against
    its own printed result: a contradiction only if no rounding of the inputs reaches it."""
    claimed = parse_value(printed)
    if claimed is None:
        return _r("INCONCLUSIVE", f"no number in the printed result {printed!r}")
    prec = half_width(printed)
    ok = lo - 1e-12 <= claimed + prec and claimed - prec <= hi + 1e-12
    return _r("ARITHMETIC_CONSISTENT" if ok else "ARITHMETIC_CONTRADICTION",
              f"the paper's own operands give [{lo:.6g}, {hi:.6g}] over their printed precision vs printed {printed}",
              rule="exact interval arithmetic over the printed precision of every operand and the result",
              claimed=claimed, reproduced=[lo, hi], precision=prec)
