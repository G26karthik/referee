"""An executed result against the number the paper printed. Arithmetic, not judgement.

The order is the invariant: a refused run reconciles nothing (BLOCKED); an environment or
startup failure is INCONCLUSIVE, never a failed reproduction (invariant 5); a certificate
whose hypothesis assertion failed tried an inadmissible instance, never a counterexample;
a 100x ratio is a units mismatch in this harness; a deterministic run can only agree to
the printed precision, never "fail" without a noise band to fail against. Small samples are
decided with Student-t critical values (two-sided 95%): a stated relation needs its mean
paired margin beyond t*SE; a reproduction is RESOLVED only inside the confidence interval of
the mean and FAILED only outside the prediction interval of one more run. A certificate's
counterexample is an instance on which every premise of the EXACT claim holds; a violation
found only after changing the claim's reading, or on no admissible instance, is not one.
"""
from __future__ import annotations

import statistics

from .evidence import half_width, parse_value, relation
from .execute import admits

SUPPORT = ("RESOLVED_VERIFIED", "ARITHMETIC_CONSISTENT", "RELATION_HOLDS")
FAILURE = ("FAILED_REPRODUCTION", "COUNTEREXAMPLE_FOUND", "ARITHMETIC_CONTRADICTION", "RELATION_VIOLATED")
QUALIFIED = ("PREMISE_NOT_MET", "VIOLATION_UNDER_CHANGED_READING")   # neither support nor a falsification
# Some measurements completed and are kept (`stages`, `status_on_completed`), but the protocol did
# not: a stage or a seed failed. Never support, never a failure of the paper.
PARTIAL = "PARTIAL"
_TOL = 1e-9   # ponytail: a violation smaller than this (relative) is below double precision
_T975 = ((1, 12.706), (2, 4.303), (3, 3.182), (4, 2.776), (5, 2.571), (6, 2.447), (7, 2.365), (8, 2.306),
         (9, 2.262), (10, 2.228), (12, 2.179), (15, 2.131), (20, 2.086), (30, 2.042), (60, 2.0), (120, 1.98))


def t975(df: int) -> float:
    """Two-sided 95% Student-t critical value (the tabulated df at or below `df`: conservative)."""
    return next((t for d, t in reversed(_T975) if d <= df), 12.706) if df < 1000 else 1.96


def _below_precision(r: dict) -> bool:
    """A reported violation whose two sides agree to double precision, with no exact values
    printed: float round-off, not a violation an exact comparison would find."""
    if r.get("violated") != 1 or r.get("exact") or not {"lhs", "rhs"} <= set(r):
        return False
    return abs(r["lhs"] - r["rhs"]) <= _TOL * max(1.0, abs(r["lhs"]), abs(r["rhs"]))


def certificate(results: list[dict], changed: bool, step: bool, crashed: int = 0) -> dict:
    """Per instance: `violated`, `premises` (1 if every premise of the tested reading holds on
    it, evaluated exactly; None if the script did not say), `literal` (the text exactly as
    printed: holds, fails = its printed premises hold and its printed conclusion fails,
    undefined, premise_not_met). Only an admissible instance is a counterexample; a violation
    below double precision without exact values is not one; a crashed instance is inadmissible."""
    what = "a step of the printed proof" if step else "the statement as printed"
    n = len(results)
    fuzzy = [r for r in results if _below_precision(r)]
    results = [r for r in results if not _below_precision(r)]
    adm = [r for r in results if r.get("premises") == 1]
    legacy = [r for r in results if r.get("premises") is None]
    bad = [r for r in adm if r.get("violated") == 1]
    lit = {k: sum(1 for r in results if r.get("literal") == k) for k in ("holds", "fails", "undefined", "premise_not_met")}
    out = {"rule": "exact arithmetic; a counterexample must satisfy every premise of the exact claim", "n": n,
           "admissible": len(adm), "violated_admissible": len(bad),
           "reading": "changed (see deviations)" if changed else "as printed",
           **({"literal": lit} if any(lit.values()) else {}),
           **({"below_precision": len(fuzzy)} if fuzzy else {}), **({"crashed": crashed} if crashed else {})}
    tested = (f"; under the recorded change of reading: {len(bad)} of {len(adm)} admissible instance(s) violated"
              if changed else "") + (f"; {crashed} instance(s) crashed (inadmissible, not evidence)" if crashed else "")
    if fuzzy and not bad and not lit["fails"]:
        return _r("INCONCLUSIVE", f"{len(fuzzy)} reported violation(s) are float round-off (|lhs - rhs| <= {_TOL:g} "
                  "relative) and the script printed no exact values: its comparison was not exact, so nothing is "
                  f"decided{tested}", **out)
    if not results:
        return _r("INCONCLUSIVE", "no instance reported a result" + tested, **out)
    if lit["fails"] or (bad and not changed):
        if lit["fails"]:
            why = (f"{lit['fails']} instance(s) satisfy the printed premises of {what} and violate it exactly as "
                   f"printed{tested}")
        else:
            why = f"{len(bad)} admissible instance(s) satisfy every premise of {what} and violate its conclusion{tested}"
        return _r("COUNTEREXAMPLE_FOUND", why, **out)
    if bad:
        return _r("VIOLATION_UNDER_CHANGED_READING", f"{len(bad)} of {len(adm)} admissible instance(s) violate the "
                  f"claim only under the recorded change of reading; this is not a counterexample to {what}"
                  + (f"; as printed: {lit['premise_not_met']} premise never met, {lit['undefined']} undefined"
                     if lit["premise_not_met"] or lit["undefined"] else ""), **out)
    if legacy and any(r.get("violated") == 1 for r in legacy):
        return _r("INCONCLUSIVE", "violations were reported on instances whose premises the script did not "
                  "evaluate, so none is a counterexample", **out)
    if not adm and not legacy:
        return _r("PREMISE_NOT_MET", f"none of the {n} constructed instance(s) satisfies the premises of "
                  f"{'the tested reading of ' if changed else ''}{what}: the claim was not tested on any admissible "
                  f"instance (its premise may be unsatisfiable){tested}", **out)
    k = len(adm) + len(legacy)
    note = (" under the recorded change of reading" if changed else "") + "".join(
        f"; as printed: {lit[s]} {s.replace('_', ' ')}" for s in ("undefined", "premise_not_met") if lit[s]) + (
        f"; {crashed} instance(s) crashed (inadmissible)" if crashed else "")
    return _r("NO_VIOLATION_FOUND", f"all {k} admissible instance(s) satisfy the conclusion{note} — the tested "
              "instances only, never a proof", **out)


def _r(status: str, reason: str, **kw) -> dict:
    return {"status": status, "reason": reason, **kw}


def reconcile(kind: str, printed: str, values: list[float], failure: str, ev: dict,
              seeded: bool, authorized: bool, why: str, rel: str = "", cert: list[dict] | None = None,
              changed: bool = False, step: bool = False, staged: list | None = None,
              failed: dict | None = None, stage_errors: dict | None = None) -> dict:
    """`failure` ends a check that measured nothing; `failed` (seed -> error) records seeds that
    failed after the check had measured something, whose completed measurements are kept."""
    if not authorized:
        return _r("BLOCKED", f"not run: {why}. A refusal by this harness is not evidence about the paper.")
    if not admits(kind):
        return _r("INCONCLUSIVE", f"'{kind}' is not admissible provenance, so it settles nothing")
    failed, stage_errors = failed or {}, stage_errors or {}
    if kind == "CERTIFICATE":
        if failure:
            return _r("INCONCLUSIVE", f"the certificate did not complete ({failure[:400]}). A failed hypothesis "
                      "assertion means the instance was not admissible — never a counterexample.")
        return certificate(cert if cert is not None else [{"violated": v, "premises": None} for v in values],
                           changed, step, crashed=len(failed))
    if failure:
        if ev.get("infra_error"):
            return _r("INCONCLUSIVE", f"infrastructure failure ('{ev['infra_error']}'), a fact about this host, not "
                      f"the paper: {failure[:400]}")
        if not ev.get("reached"):
            why = ("a setup failure" if ev.get("setup_error") else "the run ended before printing any result or "
                   "progress line")
            return _r("INCONCLUSIVE", f"{why}: {failure[:400]}. The burden of showing the experiment ran is on "
                      "this harness.")
        if kind == "AUTHOR_CODE" and ev.get("own_code_crash"):
            return _r("FAILED_REPRODUCTION", f"the authors' code reached the experiment and then crashed in its own "
                      f"code ({ev['own_code_crash']}): {failure[:400]}", rule="a run that starts and breaks in its own code")
        return _r("INCONCLUSIVE", f"the run failed ({failure[:400]}), but not by a crash inside the authors' own "
                  "code, so nothing is established about the paper")
    stages = sorted({s for s, _ in staged or []} | set(stage_errors), key=lambda s: (s == "", s))
    if stages and stages != [""]:
        return _staged(kind, printed, rel, seeded, staged or [], stages, failed, stage_errors)
    res = _relation(kind, rel, values) if rel else _point(kind, printed, values, seeded)
    return _partial(res, failed) if failed else res


def _partial(res: dict, failed: dict, stages: dict | None = None, extra: str = "") -> dict:
    """The protocol did not complete: what completed is kept as `status_on_completed`."""
    errs = "; ".join(f"seed {k}: {v[:160]}" for k, v in sorted(failed.items())[:3])
    return {**res, "status": PARTIAL, "status_on_completed": res["status"],
            "reason": (f"partial: {len(failed)} seed(s) failed after measuring ({errs}){extra}. On what completed: "
                       f"{res['reason']}" if failed else f"partial{extra}. On what completed: {res['reason']}"),
            "failed_seeds": failed, **({"stages": stages} if stages else {})}


def _staged(kind, printed, rel, seeded, staged, stages, failed, stage_errors) -> dict:
    """Each stage (a dataset, a setting) is decided over its own seeds; a stated relation must
    hold in every stage. A stage that started and printed no result is not completed. A failure
    found in a completed stage stands; otherwise any incomplete stage or failed seed is PARTIAL."""
    per = {}
    for s in stages:
        vals = [v for t, v in staged if t == s]
        per[s or "(unnamed)"] = ({"status": "NOT_COMPLETED", "reason": stage_errors.get(s, "no result line")[:400]}
                                 if not vals else _relation(kind, rel, vals) if rel else _point(kind, printed, vals, seeded))
    sts = [p["status"] for p in per.values()]
    brief = "; ".join(f"{s}: {p['status']}" + (f" (n={p['n']})" if p.get("n") else "") for s, p in per.items())
    fail = next((s for s, p in per.items() if p["status"] in FAILURE), None)
    if fail:
        return _r(per[fail]["status"], f"stage {fail}: {per[fail]['reason']} [{brief}]", rule=per[fail].get("rule", ""),
                  stages=per, **({"failed_seeds": failed} if failed else {}))
    if "NOT_COMPLETED" in sts or failed:
        done = [p["status"] for p in per.values() if p["status"] != "NOT_COMPLETED"]
        best = next((x for x in done if x not in SUPPORT), done[0] if done else "INCONCLUSIVE")
        return _partial(_r(best, brief, stages=per), failed, per,
                        extra=f"; stages not completed: {', '.join(s for s, p in per.items() if p['status'] == 'NOT_COMPLETED') or 'none'}")
    if all(x in SUPPORT for x in sts):
        return _r(sts[0], f"every stage holds [{brief}]", rule=next(iter(per.values())).get("rule", ""), stages=per)
    return _r("INCONCLUSIVE", f"not every stage is decided [{brief}]", stages=per)


def _point(kind: str, printed: str, values: list[float], seeded: bool) -> dict:
    """A produced value against the printed number."""
    n = len(values)
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
    t = t975(n - 1)
    ci, pi = t * std / n ** 0.5, t * std * (1 + 1 / n) ** 0.5
    rule = (f"{n} seeds: RESOLVED inside the 95% CI of the mean (+-{ci:.4g}), FAILED outside the 95% prediction "
            f"interval of one more run (+-{pi:.4g}), t={t}, after crediting printed precision")
    out.update(ci=round(ci, 6), pi=round(pi, 6), t=t)
    if eff <= ci:
        return _r("RESOLVED_VERIFIED", f"produced {mean:g} vs printed {printed}{note}", rule=rule, **out)
    if eff > pi:
        return _r("FAILED_REPRODUCTION", f"produced {mean:g} vs printed {printed}: |delta| {out['delta']:g} lies "
                  f"outside the prediction interval{note}", rule=rule, **out)
    return _r("INCONCLUSIVE", f"produced {mean:g} vs printed {printed}: outside the CI but inside the prediction "
              f"interval of {n} seeds — consistent with noise, not pinned down", rule=rule, **out)


def _relation(kind: str, rel: str, margins: list[float]) -> dict:
    """A comparison the paper states, over paired results (one per REFEREE_RESULT line):
    decided on the mean margin beyond t(n-1)*SE (a two-sided 95% paired t-test). One line
    decides only when it is an exact recomputation from released data."""
    n, strict = len(margins), relation(rel)[1] in ("<", ">")
    if not n:
        return _r("INCONCLUSIVE", f"no result carried every output the relation {rel!r} names")
    if n == 1 and kind != "RELEASED_DATA":
        return _r("INCONCLUSIVE", "one result of an experiment has no noise band to decide a relation on",
                  relation=rel, margin=margins[0], n=1)
    m = statistics.fmean(margins)
    t = t975(n - 1) if n > 1 else 0.0
    band = t * statistics.stdev(margins) / n ** 0.5 if n > 1 else 0.0
    out = {"relation": rel, "margin": round(m, 6), "band": round(band, 6), "n": n, "t": t,
           "rule": f"mean paired margin beyond t({n - 1})={t} standard errors (two-sided 95%)" if n > 1
           else "exact recomputation from released data"}
    if m > band or (not strict and band == 0 and m >= 0):
        return _r("RELATION_HOLDS", f"{rel}: mean margin {m:.4g} over {n} result(s)", **out)
    if m < -band or (strict and band == 0 and m == 0):
        return _r("RELATION_VIOLATED", f"{rel} does not hold: mean margin {m:.4g} over {n} result(s)", **out)
    return _r("INCONCLUSIVE", f"{rel}: mean margin {m:.4g} is within t*SE ({band:.4g}) of equality over {n} "
              "result(s): not decided", **out)


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
