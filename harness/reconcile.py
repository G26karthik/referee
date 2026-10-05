"""An executed result against the number the paper printed. Arithmetic, not judgement.

The order is the invariant: a refused run reconciles nothing (BLOCKED); an environment or
startup failure is INCONCLUSIVE, never a failed reproduction (invariant 5); a certificate
whose hypothesis assertion failed tried an inadmissible instance, never a counterexample;
a 100x ratio is a units mismatch in this harness; a deterministic run can only agree to
the printed precision, never "fail" without a noise band to fail against. Small samples are
decided with Student-t critical values (two-sided 95%): a stated relation needs its mean
paired margin beyond t*SE; a reproduction is RESOLVED only inside the confidence interval of
the mean and FAILED only outside the prediction interval of one more run. A certificate's
counterexample is an instance on which every premise of the EXACT claim holds (evaluated by the
script: `premises_hold` 1); a violation found only after changing the claim's reading, on an instance
whose premises were not evaluated, or on no admissible instance, is not one.
Identical results of several runs are one measurement, never independent replicates — but equal
SUMMARY values are not identical runs: the runs' other outputs and their data fingerprints, per stage,
say whether they differed (harness/independence.py); a static route of the seed into a generator does
not. Independent replicates whose compared value repeats are a zero-variance sample: a proportion of
counted trials (zero events included) is decided by its exact binomial interval, anything else by an
exact sign test that needs six replicates to reach 95% — never by a t-test with a standard error of
zero. Counted trials are pooled over seeds only when every value is a whole count of its trials and the
seeds' counts agree (chi-square, 95%); otherwise the replicates decide. Where the paper's text and the
authors' code define the compared quantity differently, both readings are decided on the same run
(same data, same cohort), stage by stage, and neither is chosen; values tagged with a reading the
check does not declare are never pooled. An engineering compatibility test is a condition every run
must meet, never a statistical comparison.
"""
from __future__ import annotations

import math
import re
import statistics

from . import independence
from .evidence import half_width, margin, parse_value, relation
from .execute import admits

SUPPORT = ("RESOLVED_VERIFIED", "ARITHMETIC_CONSISTENT", "RELATION_HOLDS")
FAILURE = ("FAILED_REPRODUCTION", "COUNTEREXAMPLE_FOUND", "ARITHMETIC_CONTRADICTION", "RELATION_VIOLATED")
QUALIFIED = ("PREMISE_NOT_MET", "VIOLATION_UNDER_CHANGED_READING")   # neither support nor a falsification
# Some measurements completed and are kept (`stages`, `status_on_completed`), but the protocol did
# not: a stage or a seed failed. Never support, never a failure of the paper.
PARTIAL = "PARTIAL"
# The paper's definition and the authors' code's definition, run on the same data and cohort, give
# different results: recorded with both, never resolved by the harness.
READINGS_DIFFER = "READINGS_DIFFER"
_TOL = 1e-9   # ponytail: a violation smaller than this (relative) is below double precision
# An exact two-sided sign test reaches 95% only from six replicates that all fall on one side (p = 2 * 0.5^6).
SIGN_MIN = 6
_T975 = ((1, 12.706), (2, 4.303), (3, 3.182), (4, 2.776), (5, 2.571), (6, 2.447), (7, 2.365), (8, 2.306),
         (9, 2.262), (10, 2.228), (12, 2.179), (15, 2.131), (20, 2.086), (30, 2.042), (60, 2.0), (120, 1.98))


def t975(df: int) -> float:
    """Two-sided 95% Student-t critical value (the tabulated df at or below `df`: conservative)."""
    return next((t for d, t in reversed(_T975) if d <= df), 12.706) if df < 1000 else 1.96


def _below_precision(r: dict) -> bool:
    """A reported violation (`violated`, or the printed text failing: `literal` fails) whose two sides
    agree to double precision, with no exact values printed: float round-off, not a violation an exact
    comparison would find."""
    if not (r.get("violated") == 1 or r.get("literal") == "fails") or r.get("exact") or not {"lhs", "rhs"} <= set(r):
        return False
    return abs(r["lhs"] - r["rhs"]) <= _TOL * max(1.0, abs(r["lhs"]), abs(r["rhs"]))


def certificate(results: list[dict], changed: bool, step: bool, crashed: int = 0) -> dict:
    """Per instance: `violated`, `premises` (1 if every premise of the tested reading holds on
    it, evaluated exactly; None if the script did not say — not admissible, and not 'unmet'),
    `literal` (the text exactly as printed: holds, fails = its printed premises hold and its printed
    conclusion fails, undefined, premise_not_met), `reading` (a named definition the instance was
    computed under: a certificate tests the printed claim, so such an instance is treated as tested
    under a changed reading). Only an admissible instance is a counterexample; a violation below
    double precision without exact values is not one; a crashed instance is inadmissible."""
    what = "a step of the printed proof" if step else "the statement as printed"
    n = len(results)
    fuzzy = [r for r in results if _below_precision(r)]
    results = [r for r in results if not _below_precision(r)]
    moved = lambda r: changed or bool(r.get("reading"))
    adm = [r for r in results if r.get("premises") == 1]
    unsaid = [r for r in results if r.get("premises") not in (0, 1)]
    bad = [r for r in adm if r.get("violated") == 1 and not moved(r)]
    bad_moved = [r for r in adm if r.get("violated") == 1 and moved(r)]
    lit_fails = [r for r in results if r.get("literal") == "fails" and not r.get("reading")]
    lit_moved = [r for r in results if r.get("literal") == "fails" and r.get("reading")]
    tagged = sum(1 for r in results if r.get("reading"))
    lit = {k: sum(1 for r in results if r.get("literal") == k) for k in ("holds", "fails", "undefined", "premise_not_met")}
    out = {"rule": "exact arithmetic; a counterexample must satisfy every premise of the exact claim", "n": n,
           "admissible": len(adm), "violated_admissible": len(bad) + len(bad_moved),
           "reading": "changed (see deviations)" if changed else "as printed",
           **({"literal": lit} if any(lit.values()) else {}), **({"premises_unsaid": len(unsaid)} if unsaid else {}),
           **({"under_named_reading": tagged} if tagged else {}),
           **({"below_precision": len(fuzzy)} if fuzzy else {}), **({"crashed": crashed} if crashed else {})}
    tested = (f"; under the recorded change of reading: {len(bad_moved)} of {len(adm)} admissible instance(s) violated"
              if changed else "") + (f"; {crashed} instance(s) crashed (inadmissible, not evidence)" if crashed else "")
    if fuzzy and not bad and not bad_moved and not lit_fails and not lit_moved:
        return _r("INCONCLUSIVE", f"{len(fuzzy)} reported violation(s) are float round-off (|lhs - rhs| <= {_TOL:g} "
                  "relative) and the script printed no exact values: its comparison was not exact, so nothing is "
                  f"decided{tested}", **out)
    if not results:
        return _r("INCONCLUSIVE", "no instance reported a result" + tested, **out)
    if lit_fails or bad:
        if lit_fails:
            why = (f"{len(lit_fails)} instance(s) satisfy the printed premises of {what} and violate it exactly as "
                   f"printed{tested}")
        else:
            why = f"{len(bad)} admissible instance(s) satisfy every premise of {what} and violate its conclusion{tested}"
        return _r("COUNTEREXAMPLE_FOUND", why, **out)
    if bad_moved or lit_moved:
        how = ("the recorded change of reading" if changed else "a named reading (`reading` on the result line) that "
               "is not the claim as printed")
        return _r("VIOLATION_UNDER_CHANGED_READING", f"{len(bad_moved) + len(lit_moved)} instance(s) violate the claim "
                  f"only under {how}; this is not a counterexample to {what}"
                  + (f"; as printed: {lit['premise_not_met']} premise never met, {lit['undefined']} undefined"
                     if lit["premise_not_met"] or lit["undefined"] else ""), **out)
    if unsaid and any(r.get("violated") == 1 for r in unsaid):
        return _r("INCONCLUSIVE", "violations were reported on instances whose premises the script did not "
                  "evaluate, so none is a counterexample", **out)
    if not adm and unsaid:
        return _r("INCONCLUSIVE", f"the script evaluated the premises of {n - len(unsaid)} of {n} instance(s) (no "
                  "`premises_hold` 0/1 on the others) and none holds: an instance whose premises were not evaluated is "
                  "not admissible, and unevaluated is not unmet, so nothing is decided" + tested, **out)
    if not adm:
        return _r("PREMISE_NOT_MET", f"none of the {n} constructed instance(s) satisfies the premises of "
                  f"{'the tested reading of ' if changed else ''}{what}: the claim was not tested on any admissible "
                  f"instance (its premise may be unsatisfiable){tested}", **out)
    note = (" under the recorded change of reading" if changed else "") + "".join(
        f"; as printed: {lit[s]} {s.replace('_', ' ')}" for s in ("undefined", "premise_not_met") if lit[s]) + (
        f"; {len(unsaid)} instance(s) without evaluated premises (not admissible)" if unsaid else "") + (
        f"; {crashed} instance(s) crashed (inadmissible)" if crashed else "")
    return _r("NO_VIOLATION_FOUND", f"all {len(adm)} admissible instance(s) satisfy the conclusion{note} — the tested "
              "instances only, never a proof", **out)


def _r(status: str, reason: str, **kw) -> dict:
    return {"status": status, "reason": reason, **kw}


def reconcile(kind: str, printed: str, values: list[float], failure: str, ev: dict,
              seeded: bool, authorized: bool, why: str, rel: str = "", cert: list[dict] | None = None,
              changed: bool = False, step: bool = False, staged: list | None = None,
              failed: dict | None = None, stage_errors: dict | None = None, readings: list | None = None,
              cohort_mismatch: list | None = None, deterministic: bool = False, test: str = "",
              detail: list | None = None, rng: bool | None = None, metric: str = "", undefined: dict | None = None) -> dict:
    """`failure` ends a check that measured nothing; `failed` (seed -> error) records seeds that
    failed after the check had measured something, whose completed measurements are kept.
    `staged` entries are [stage, value] or [stage, value, reading]; `readings` names the
    definitions (the paper's, the code's) the script computed side by side; `deterministic`: the
    computation has no randomness (released files, a declared deterministic pipeline); `test`
    "compatibility": an engineering condition each run must meet. `detail` (per run and stage: every numeric
    output, counted trials, a data fingerprint) says whether replicates that repeat a value were different
    runs; `rng` (does --seed reach a generator anywhere in the script?) is recorded context only. `metric`
    names the compared output of a point check (its counted trials, when it declares them). `undefined` (stage -> why)
    names units whose compared quantity the run says is undefined: completed, deciding nothing, never NOT_COMPLETED."""
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
    def decide(vals, stage="", reading=""):
        ind = independence.assess(detail, stage, reading, rng) if detail is not None else None
        lines = [r for r in detail or [] if r.get("stage", "") == stage and r.get("reading", "") == reading
                 and not r.get("missing")]
        if rel and test == "compatibility":
            return _condition(rel, vals)
        if rel:
            return _relation(kind, rel, vals, deterministic, ind, lines)
        return _point(kind, printed, vals, seeded, deterministic, ind, lines, metric)
    tags = sorted({str(e[2]) for e in staged or [] if len(e) > 2} - set(readings or []))
    if tags:          # a definition the check does not declare: never pooled with another into one sample
        return _r("INCONCLUSIVE", f"result line(s) under reading(s) {tags[:5]} that the check does not declare"
                  f"{' (it declares ' + str(list(readings)) + ')' if readings else ' (it declares none)'}: values "
                  "computed under different definitions are never pooled into one sample, so nothing is decided",
                  undeclared_readings=tags[:20])
    undefined = {s: w for s, w in (undefined or {}).items() if s not in {e[0] for e in staged or []}}
    if readings:
        return _by_reading(readings, staged or [], failed, stage_errors, decide, cohort_mismatch or [], printed, undefined)
    return _decide(values, [e[:2] for e in staged or []], failed, stage_errors, decide, undefined=undefined)


def _decide(values: list[float], staged: list, failed: dict, stage_errors: dict, decide, reading: str = "",
            undefined: dict | None = None) -> dict:
    undefined = undefined or {}
    stages = sorted({s for s, _ in staged} | set(stage_errors) | set(undefined), key=lambda s: (s == "", s))
    if stages and stages != [""]:
        return _staged(staged, stages, failed, stage_errors, lambda vals, s="": decide(vals, s, reading), undefined)
    if not values and "" in undefined:
        return _r("INCONCLUSIVE", f"the compared quantity is undefined on what this check ran: {undefined['']}",
                  undefined_stages=[""])
    res = decide(values, "", reading)
    return _partial(res, failed) if failed else res


_LISTED = 10   # ponytail: stages named in a readings reason; every stage is in the outcome's tables


def _show(p: dict) -> str:
    return p["status"] + (f" {p['reproduced']:g}" if isinstance(p.get("reproduced"), (int, float)) else
                          f" margin {p['margin']:g}" if isinstance(p.get("margin"), (int, float)) else "")


def _by_reading(names: list, staged: list, failed: dict, stage_errors: dict, decide, mismatch: list,
                printed: str, undefined: dict | None = None) -> dict:
    """Each reading decided on its own results of the same runs, then compared STAGE BY STAGE over the
    union of their stages. A stage where one reading printed a result and another did not is not
    comparable (INCONCLUSIVE, the stages named). A stage whose readings differ in status, or for a
    printed number in value beyond its printed precision, makes READINGS_DIFFER, the stages named; the
    same finding in every stage stands. Every reading's own result and stage table is kept (`readings`)
    and `stages` is the cross-reading table: the harness never picks the reading that matches, nor
    shows one reading's stages in place of the others'."""
    per, printed_in = {}, {}
    for r in names:
        sub = [[e[0], e[1]] for e in staged if len(e) > 2 and e[2] == r]
        per[r] = _decide([v for _, v in sub], sub, failed, stage_errors, decide, r, undefined)
        printed_in[r] = {s for s, _ in sub} | set(undefined or {})
    base = {"readings": per, "rule": "each reading decided on the same runs, data and cohort, stage by stage; none "
                                     "is chosen"}
    name = lambda s: s or "(no stage)"
    brief = "; ".join(f"{r}: {_show(p)}" for r, p in per.items())
    if mismatch:
        return _r("INCONCLUSIVE", f"the readings were computed on different cohorts in stage(s) {mismatch[:5]}, so "
                  f"they are not comparable [{brief}]", **base)
    union = set().union(*printed_in.values()) if printed_in else set()
    missing = {name(s): [r for r in names if s not in printed_in[r]] for s in sorted(union)
               if any(s not in printed_in[r] for r in names)}
    if missing:
        listed = "; ".join(f"{s}: {', '.join(rs)}" for s, rs in list(missing.items())[:_LISTED])
        return _r("INCONCLUSIVE", f"reading(s) printed no result in {len(missing)} stage(s) where another reading did "
                  f"({listed}{'; ...' if len(missing) > _LISTED else ''}): those stages are not comparable, so the "
                  f"readings are not decided [{brief}]", missing_readings=missing, **base)
    tables = {r: p.get("stages") or {"": p} for r, p in per.items()}
    prec = half_width(printed) if printed else 0.0
    cross, differ, open_ = {}, [], []
    decided = lambda p: p["status"] in SUPPORT + FAILURE + QUALIFIED + ("NO_VIOLATION_FOUND",)
    for key in next(iter(tables.values())):
        ps = {r: t.get(key) or {"status": "NOT_COMPLETED", "reason": "no result"} for r, t in tables.items()}
        vals = [p.get("reproduced") for p in ps.values()]
        by_value = all(isinstance(v, (int, float)) for v in vals) and max(vals) - min(vals) > prec + 1e-12
        # Different findings, or values apart beyond the printed precision. A reading decided beside one that is
        # not yet decided (within its noise band, wanting replicates) is not comparable yet: never a disagreement.
        mixed = len({p["status"] for p in ps.values()}) > 1
        apart = by_value or (mixed and all(decided(p) for p in ps.values()))
        each = "; ".join(f"{r}: {_show(p)}" for r, p in ps.items())
        cross[key] = {"status": READINGS_DIFFER if apart else "INCONCLUSIVE" if mixed else next(iter(ps.values()))["status"],
                      "reason": each, "readings": {r: p["status"] for r, p in ps.items()}}
        differ += [key] if apart else []
        open_ += [key] if mixed and not apart else []
    listed = "; ".join(f"{name(k)}: {cross[k]['reason']}" for k in differ[:_LISTED]) + ("; ..." if len(differ) > _LISTED else "")
    stages = {"stages": cross} if list(cross) != [""] else {}
    if any(p["status"] == PARTIAL for p in per.values()):
        done = {p.get("status_on_completed", p["status"]) for p in per.values()}
        on = READINGS_DIFFER if differ else done.pop() if len(done) == 1 else "INCONCLUSIVE"
        res = _r(on, f"the readings differ in {len(differ)} completed stage(s) [{listed}]" if differ else
                 f"every reading gives the same finding in every completed stage [{brief}]", **stages)
        return {**_partial(res, failed, cross if stages else None, extra="; readings: " + brief), **base,
                **({"readings_differ_in": [name(k) for k in differ]} if differ else {})}
    if differ:
        return _r(READINGS_DIFFER, f"the readings give different results on the same runs, data and cohort in "
                  f"{len(differ)} of {len(cross)} stage(s) [{listed}]; which one the paper's number means is for a human "
                  "to decide", readings_differ_in=[name(k) for k in differ], **stages, **base)
    if open_:
        listed = "; ".join(f"{name(k)}: {cross[k]['reason']}" for k in open_[:_LISTED])
        return _r("INCONCLUSIVE", f"in {len(open_)} stage(s) one reading is decided and another is not yet ({listed}): "
                  "the readings are not comparable there, so they are not decided", readings_undecided_in=[
                      name(k) for k in open_], **stages, **base)
    shared = next(iter(per.values()))["status"]
    return _r(shared, f"every reading gives the same finding in every stage [{brief}]", **stages, **base)


def _partial(res: dict, failed: dict, stages: dict | None = None, extra: str = "") -> dict:
    """The protocol did not complete: what completed is kept as `status_on_completed`."""
    errs = "; ".join(f"seed {k}: {v[:160]}" for k, v in sorted(failed.items())[:3])
    return {**res, "status": PARTIAL, "status_on_completed": res["status"],
            "reason": (f"partial: {len(failed)} seed(s) did not complete ({errs}){extra}. On what completed: "
                       f"{res['reason']}" if failed else f"partial{extra}. On what completed: {res['reason']}"),
            "failed_seeds": failed, **({"stages": stages} if stages else {})}


UNDEFINED = "UNDEFINED"   # a unit whose compared quantity the run says is undefined there: completed, deciding nothing


def _staged(staged, stages, failed, stage_errors, decide, undefined: dict | None = None) -> dict:
    """Each stage (a dataset, a setting) is decided over its own seeds; a stated relation must
    hold in every stage. A stage that started and printed no result is not completed; a stage whose
    run said why its quantity is undefined there is UNDEFINED (completed, deciding nothing, listed in
    `undefined_stages`). A failure found in a completed stage stands; otherwise any incomplete stage or
    failed seed is PARTIAL. The rest is decided over the stages where the quantity is defined."""
    per, undefined = {}, undefined or {}
    for s in stages:
        vals = [v for t, v in staged if t == s]
        per[s or "(unnamed)"] = (decide(vals, s) if vals else
                                 {"status": UNDEFINED, "reason": undefined[s][:400]} if s in undefined else
                                 {"status": "NOT_COMPLETED", "reason": stage_errors.get(s, "no result line")[:400]})
    und = [s for s, p in per.items() if p["status"] == UNDEFINED]
    note = (f"; {len(und)} stage(s) undefined (the compared quantity has no value there; they decide nothing)"
            if und else "")
    extra = {"undefined_stages": und} if und else {}
    defined = {s: p for s, p in per.items() if p["status"] != UNDEFINED}
    sts = [p["status"] for p in defined.values()]
    brief = "; ".join(f"{s}: {p['status']}" + (f" (n={p['n']})" if p.get("n") else "") for s, p in defined.items())
    if not defined:
        return _r("INCONCLUSIVE", f"the compared quantity is undefined in every stage{note}", stages=per, **extra)
    fail = next((s for s, p in defined.items() if p["status"] in FAILURE), None)
    if "NOT_COMPLETED" in sts or failed:     # the protocol did not complete: what completed is kept, decides nothing
        done = [p["status"] for p in defined.values() if p["status"] != "NOT_COMPLETED"]
        best = per[fail]["status"] if fail else next((x for x in done if x not in SUPPORT), done[0] if done else
                                                     "INCONCLUSIVE")
        return {**_partial(_r(best, brief + note, stages=per), failed, per,
                           extra=f"; stages not completed: {', '.join(s for s, p in per.items() if p['status'] == 'NOT_COMPLETED') or 'none'}"),
                **extra}
    if fail:
        return _r(per[fail]["status"], f"stage {fail}: {per[fail]['reason']} [{brief}]{note}", rule=per[fail].get("rule", ""),
                  stages=per, **extra)
    if all(x in SUPPORT + ("NO_VIOLATION_FOUND",) for x in sts):
        weakest = "NO_VIOLATION_FOUND" if "NO_VIOLATION_FOUND" in sts else sts[0]
        return _r(weakest, ("no stage was violated (tested replicates only, never a proof) " if weakest == "NO_VIOLATION_FOUND"
                            else "every stage holds ") + f"[{brief}]{note}", rule=next(iter(defined.values())).get("rule", ""),
                  stages=per, **extra)
    return _r("INCONCLUSIVE", f"not every stage is decided [{brief}]{note}", stages=per, **extra,
              rule=next((p["rule"] for p in defined.values() if p.get("rule")), ""))


def _point(kind: str, printed: str, values: list[float], seeded: bool, deterministic: bool = False,
           ind: dict | None = None, lines: list[dict] | None = None, metric: str = "") -> dict:
    """A produced value against the printed number. Identical values of several runs are one
    measurement (n_independent 1): they never give a noise band — unless the runs are shown to
    differ, when they are a zero-variance sample of independent replicates (`_zero_point`)."""
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
    if _repeats(values):
        std = out["std"] = 0.0                      # round-off is no spread
    if deterministic and kind == "RECONSTRUCTION" and n > 1 and std > 0:
        return _r("INCONCLUSIVE", f"declared deterministic, but its {n} runs differ (std {std:.4g}): the computation is "
                  "not deterministic as declared, so nothing is decided", rule="a declared deterministic pipeline must "
                  "repeat exactly", **out)
    if not seeded or n < 2 or std == 0:
        if n > 1 and std == 0 and ind and ind["independent"] and not deterministic:
            return _zero_point(printed, mean, n, ind, lines or [], metric, out, note)
        rule = "deterministic run: agreement only within the printed precision; a difference is reported, not scored"
        lost = bool(ind and ind["state"] == "unknown")
        what = "a deterministic run" if deterministic or not seeded or n < 2 else (
            "runs whose per-run record is missing (whether they differed is unknown)" if lost else
            f"one measurement ({n} identical runs)")
        if n > 1 and std == 0:
            rule = "agreement only within the printed precision; a difference is reported, not scored"
            if lost:
                rule += f"; the {n} runs repeated the value and {ind['basis']}"
            else:
                out["n_independent"] = 1
                rule += (f"; the {n} runs gave identical results: one measurement, not {n} replicates"
                         + ("" if deterministic else f" ({ind['basis']})" if ind else
                            " (nothing they printed shows the seed varied them)"))
        return (_r("RESOLVED_VERIFIED", f"produced {mean:g} vs printed {printed}: within printed precision{note}",
                   rule=rule, **out) if eff <= 1e-12 else
                _r("INCONCLUSIVE", f"produced {mean:g} vs printed {printed}: |delta| {out['delta']:g} exceeds the "
                   f"printed precision, but {what} has no noise band to call it a failure", rule=rule, **out))
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


def _relation(kind: str, rel: str, margins: list[float], deterministic: bool = False, ind: dict | None = None,
              lines: list[dict] | None = None) -> dict:
    """A comparison the paper states, over paired results (one per REFEREE_RESULT line):
    decided on the mean margin beyond t(n-1)*SE (a two-sided 95% paired t-test). One line
    decides only when it is an exact recomputation from released data. A compared proportion of
    counted trials is decided by its exact binomial interval. Identical margins of several runs are
    one measurement — unless the runs are shown to differ (`ind`), when they are a zero-variance
    sample of independent replicates, decided by an exact sign test and never by a t-test with a
    standard error of zero. For a deterministic computation identical margins are one measurement
    decided on their sign; for a seeded experiment whose runs are identical, the seed did not vary
    the run (no replicates, no noise band)."""
    n, strict = len(margins), relation(rel)[1] in ("<", ">")
    if not n:
        return _r("INCONCLUSIVE", f"no result carried every output the relation {rel!r} names")
    shown = bool(ind and ind["independent"]) and not deterministic
    prop = _proportion(rel, lines or [], shown, strict) if not deterministic else None
    if prop and "status" in prop:
        return prop
    return {**_over_margins(kind, rel, margins, deterministic, ind, shown, strict), **(prop or {})}


def _repeats(values: list[float], ind: dict | None = None) -> bool:
    """Do several runs give one compared value? Equal to 12 significant digits (the same sum taken in another order is
    round-off, Sep-29 changepoint C7). Decided on the compared values themselves: whether the runs were independent is
    the independence record's question (`ind`), never whether their values agree (it ignores time-like outputs, which
    a timing comparison compares)."""
    return len(values) > 1 and len({float(f"{v:.12g}") for v in values}) == 1


def _over_margins(kind: str, rel: str, margins: list[float], deterministic: bool, ind: dict | None, shown: bool,
                  strict: bool) -> dict:
    n = len(margins)
    same = _repeats(margins, ind)
    if deterministic and kind == "RECONSTRUCTION" and n > 1 and (not _repeats(margins, ind) or (
            ind and ind["state"] == "different")):
        return _r("INCONCLUSIVE", f"declared deterministic, but its {n} runs differ: the computation is not deterministic "
                  "as declared, so its runs are neither one measurement nor replicates", relation=rel, n=n,
                  rule="a declared deterministic pipeline must repeat exactly")
    if same:
        if not deterministic:
            if shown:
                return _zero_variance(rel, margins, ind or {}, strict)
            if ind and ind["state"] == "unknown":
                return _r("INCONCLUSIVE", f"the {n} seeded runs repeated one margin and {ind['basis']}: they are "
                          "neither shown to be independent replicates nor shown to be one run repeated, so nothing is "
                          "decided", relation=rel, margin=round(margins[0], 6), n=n,
                          rule="replicates are counted only from what the runs printed; a missing record is not "
                               "evidence either way")
            return _r("INCONCLUSIVE", f"the {n} seeded runs gave identical results and nothing they printed shows the "
                      f"seed varied them: one measurement, not {n} independent replicates, with no noise band"
                      + (f" ({ind['basis']})" if ind else ""),
                      relation=rel, margin=round(margins[0], 6), n=n, n_independent=1,
                      rule="identical results of seeded runs are one measurement, not replicates")
        m = statistics.fmean(margins)
        out = {"relation": rel, "margin": round(m, 6), "band": 0.0, "n": n, "n_independent": 1,
               "rule": f"deterministic computation: identical in all {n} runs, one measurement (not {n} replicates), "
                       "decided on the sign of its margin"}
        if m > 0 or (not strict and m == 0):
            return _r("RELATION_HOLDS", f"{rel}: margin {m:.4g} (one deterministic measurement)", **out)
        return _r("RELATION_VIOLATED", f"{rel} does not hold: margin {m:.4g} (one deterministic measurement)", **out)
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
    # Three replicates (the floor REFEREE itself sets) carry t(2)=4.3: a margin within that band may be decided by
    # more of them, so a finished check is extended once (execute._extension), never re-run for a direction.
    return _r("INCONCLUSIVE", f"{rel}: mean margin {m:.4g} is within t*SE ({band:.4g}) of equality over {n} "
              "result(s): not decided", **({"needs_replicates": SIGN_MIN} if not deterministic and n < SIGN_MIN else {}), **out)


def _lentz(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):                         # ponytail: 400 terms converge to 1e-14 for every n this harness sees
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / (c if abs(c) > tiny else tiny)
            h *= d * c
        if abs(d * c - 1.0) < 1e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """The regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0 or x >= 1.0:
        return 0.0 if x <= 0.0 else 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _lentz(a, b, x) / a
    return 1.0 - front * _lentz(b, a, 1.0 - x) / b


def _beta_quantile(p: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """The exact two-sided confidence interval of a proportion of k events in n trials; zero (or all)
    events give the closed forms 1-(a/2)^(1/n) and (a/2)^(1/n)."""
    a = 1.0 - conf
    lo = 0.0 if k == 0 else _beta_quantile(a / 2, k, n - k + 1)
    hi = 1.0 if k == n else _beta_quantile(1 - a / 2, k + 1, n - k)
    return lo, hi


_CHI95 = (3.841, 5.991, 7.815, 9.488, 11.070)   # chi-square 95% critical values, 1..5 degrees of freedom


def chi2_95(df: int) -> float:
    """The 95% critical value of chi-square: tabulated to 5 df, Wilson-Hilferty beyond (within 0.3%)."""
    if df <= len(_CHI95):
        return _CHI95[max(df, 1) - 1]
    return df * (1 - 2 / (9 * df) + 1.6449 * math.sqrt(2 / (9 * df))) ** 3


def _is_count(v, trials) -> bool:
    """A proportion of counted trials: in [0, 1], and value x trials a whole number of events."""
    return isinstance(v, (int, float)) and 0.0 <= v <= 1.0 and abs(v * trials - round(v * trials)) <= 1e-6


def _heterogeneity(lines: list[dict], b: str) -> tuple[float, int] | None:
    """(chi-square, df) of the seeds' counts of `b` against one shared proportion; None if it does not reject
    at 95% (all-zero or all-full counts agree). Pooled counts are one sample only if the seeds agree."""
    per: dict = {}
    for r in lines:
        k, t = per.get(r["seed"], (0, 0))
        per[r["seed"]] = (k + round(r["out"][b] * r["trials"][b]), t + int(r["trials"][b]))
    k_all, n_all = sum(k for k, _ in per.values()), sum(t for _, t in per.values())
    p = k_all / n_all if n_all else 0.0
    if len(per) < 2 or p in (0.0, 1.0):
        return None
    chi = sum((k - t * p) ** 2 / (t * p * (1 - p)) for k, t in per.values())
    return (chi, len(per) - 1) if chi > chi2_95(len(per) - 1) else None


def _proportion(rel: str, lines: list[dict], pooled: bool, strict: bool) -> dict | None:
    """The relation decided on a compared PROPORTION OF COUNTED TRIALS: the one output that every result
    line declares with its number of independent trials (`binomial`), appearing once in the relation,
    the other outputs constant. Events and trials are pooled over the independent replicates (over the
    first run alone when the replicates are not shown to differ) — only when every value is a whole count
    of its trials in [0, 1] and the seeds' counts agree (chi-square, 95%); otherwise the replicates decide,
    and the returned {"binomial_not_pooled": why} says so. The exact Clopper-Pearson interval is decided by
    its worse end. Zero events are decided by the interval, not by a standard error of zero."""
    left, _, right, names = relation(rel)
    counted = [n for n in names if lines and all(n in (r.get("trials") or {}) for r in lines)]
    if len(counted) != 1 or len(re.findall(rf"\b{re.escape(counted[0])}\b", f"{left} {right}")) != 1:
        return None
    b = counted[0]
    if not all(_is_count(r["out"].get(b), r["trials"][b]) for r in lines):
        return {"binomial_not_pooled": f"`{b}` is declared under `binomial`, but not every value is a proportion of "
                                       "its counted trials (a value outside [0, 1], or value x trials not a whole "
                                       "number): decided over the replicates, not as counted trials"}
    used = lines if pooled else [r for r in lines if r["seed"] == lines[0]["seed"]]
    others = {n: used[0]["out"].get(n) for n in names if n != b}
    if any(v is None or any(r["out"].get(n) != v for r in used) for n, v in others.items()):
        return None                                 # the rest of the relation moves between runs: no single interval
    if pooled and (het := _heterogeneity(used, b)):
        return {"binomial_not_pooled": f"the seeds' counts of `{b}` disagree beyond binomial noise (chi-square "
                                       f"{het[0]:.4g} > {chi2_95(het[1]):.4g}, {het[1]} df, 95%): not one pooled "
                                       "sample; decided over the replicates"}
    trials = sum(int(r["trials"][b]) for r in used)
    events = sum(round(r["out"][b] * r["trials"][b]) for r in used)
    if trials <= 0 or not 0 <= events <= trials:
        return None
    lo, hi = clopper_pearson(events, trials)
    try:
        at = [margin(rel, {**others, b: v}) for v in (lo, hi)]
    except (ZeroDivisionError, ValueError):
        return None
    worst, best = min(at), max(at)
    n_ind = len({r["seed"] for r in used})
    out = {"relation": rel, "events": events, "trials": trials, "ci": [round(lo, 6), round(hi, 6)],
           "margin": round(worst, 6), "n": n_ind, "n_independent": n_ind if pooled else 1,
           "rule": "exact Clopper-Pearson interval (two-sided 95%) of a proportion of counted trials, zero events "
                   "included, decided by the worse end of the interval"}
    what = f"{b} = {events}/{trials} pooled over {n_ind} replicate(s); exact 95% interval [{lo:.4g}, {hi:.4g}]"
    if worst > 0 or (not strict and worst >= 0):
        return _r("RELATION_HOLDS", f"{rel}: {what}; the relation holds over the whole interval (margin >= {worst:.4g})", **out)
    if best < 0 or (strict and best <= 0):
        return _r("RELATION_VIOLATED", f"{rel} does not hold: {what}; it fails over the whole interval (margin <= {best:.4g})",
                  **out)
    return _r("INCONCLUSIVE", f"{rel}: {what}; the interval straddles the relation's boundary, so it is not decided", **out)


def _zero_variance(rel: str, margins: list[float], ind: dict, strict: bool) -> dict:
    """Independent replicates that repeat one margin. Each replicate either meets the relation or not,
    so the sample is binary: an exact two-sided sign test (p = 2 * 0.5^n, significant from six
    replicates), never a t-test whose standard error is zero. A margin exactly at a non-strict
    boundary is 'no violation found', with the exact bound on the rate of violations."""
    n, m = len(margins), margins[0]
    met = m > 0 or (not strict and m == 0)
    p = min(1.0, 2 * 0.5 ** n)
    ub = 1 - 0.05 ** (1 / n)                       # exact one-sided 95% bound on the per-replicate rate of the opposite outcome
    out = {"relation": rel, "margin": round(m, 6), "n": n, "n_independent": n, "independence": ind.get("basis", ""),
           "rule": f"{n} independent replicates repeat one margin (zero variance): exact sign test p = {p:.3g}; a t-test "
                   "would divide by a standard error of zero"}
    if met and m == 0:
        return _r("NO_VIOLATION_FOUND", f"{rel}: the margin is exactly 0 (the boundary) in all {n} independent replicates; "
                  f"0 of {n} violated it (exact one-sided 95% upper bound on the per-replicate violation rate {ub:.3g}). "
                  "Tested replicates only, never a proof", **out)
    if n >= SIGN_MIN:
        return (_r("RELATION_HOLDS", f"{rel}: met with margin {m:.4g} in all {n} independent replicates "
                   f"(exact sign test p = {p:.3g})", **out) if met else
                _r("RELATION_VIOLATED", f"{rel} does not hold: margin {m:.4g} in all {n} independent replicates "
                   f"(exact sign test p = {p:.3g})", **out))
    return _r("INCONCLUSIVE", f"{rel}: {'met' if met else 'violated'} with margin {m:.4g} in all {n} independent replicates, "
              f"but an exact two-sided sign test needs {SIGN_MIN} replicates to reach 95% (p = {p:.3g} for {n}); the runs were "
              f"shown to differ ({ind.get('basis', '')})", needs_replicates=SIGN_MIN, **out)


def _zero_point(printed: str, v: float, n: int, ind: dict, lines: list[dict], metric: str, out: dict, note: str) -> dict:
    """Independent replicates that all produced one value (zero variance). A compared proportion of counted
    trials (`binomial` on the metric, whole counts, seeds that agree) is decided by exact Clopper-Pearson
    intervals, as the t rule is: RESOLVED where the pooled interval reaches the printed number (its precision
    credited), FAILED only where the interval of ONE replicate's own trials (the widest, one more run) does
    not reach it either, INCONCLUSIVE between. Anything else by an exact two-sided sign test: every replicate
    lies inside the printed precision (RESOLVED) or on one side outside it, which rejects the printed value at
    95% from six replicates (p = 2 * 0.5^n). Never by a confidence interval whose standard error is zero."""
    claimed, prec = out["claimed"], out["precision"]
    out = {**out, "n_independent": n, "independence": ind.get("basis", "")}
    inside = abs(v - claimed) <= prec + 1e-12
    reach = lambda lo, hi: lo <= claimed + prec + 1e-12 and hi >= claimed - prec - 1e-12
    if metric and lines and all(metric in (r.get("trials") or {}) and _is_count(r["out"].get(metric), r["trials"][metric])
                                for r in lines) and not _heterogeneity(lines, metric):
        per: dict = {}
        for r in lines:
            k0, t0 = per.get(r["seed"], (0, 0))
            per[r["seed"]] = (k0 + round(r["out"][metric] * r["trials"][metric]), t0 + int(r["trials"][metric]))
        k, t = sum(a for a, _ in per.values()), sum(b for _, b in per.values())
        lo, hi = clopper_pearson(k, t)
        plo, phi = clopper_pearson(*min(per.values(), key=lambda kt: kt[1]))
        out.update(events=k, trials=t, ci=[round(lo, 6), round(hi, 6)], one_run_ci=[round(plo, 6), round(phi, 6)],
                   rule="exact Clopper-Pearson intervals (two-sided 95%) of a proportion of counted trials, over "
                        f"{n} independent replicates that repeated one value: RESOLVED where the pooled interval reaches "
                        "the printed number (its precision credited), FAILED only outside one replicate's own interval")
        what = (f"{metric} = {k}/{t} over {n} independent replicates; exact 95% interval [{lo:.4g}, {hi:.4g}], one "
                f"replicate's [{plo:.4g}, {phi:.4g}]")
        if reach(lo, hi):
            return _r("RESOLVED_VERIFIED", f"{what} vs printed {printed}{note}", **out)
        if not reach(plo, phi):
            return _r("FAILED_REPRODUCTION", f"{what}: even one replicate's interval excludes the printed {printed}{note}",
                      **out)
        return _r("INCONCLUSIVE", f"{what}: the pooled interval excludes the printed {printed}, one more replicate's "
                  f"does not — consistent with noise, not pinned down{note}", **out)
    p = min(1.0, 2 * 0.5 ** n)
    out["rule"] = (f"{n} independent replicates repeat one value (zero variance): RESOLVED inside the printed precision; "
                   f"outside it on one side, an exact two-sided sign test (p = {p:.3g}; 95% from {SIGN_MIN} replicates), "
                   "never a confidence interval whose standard error is zero")
    if inside:
        return _r("RESOLVED_VERIFIED", f"produced {v:g} in all {n} independent replicates vs printed {printed}: within "
                  f"printed precision{note}", **out)
    if n >= SIGN_MIN:
        return _r("FAILED_REPRODUCTION", f"produced {v:g} in all {n} independent replicates vs printed {printed}: every "
                  f"replicate lies outside the printed precision on one side (exact sign test p = {p:.3g}){note}", **out)
    return _r("INCONCLUSIVE", f"produced {v:g} in all {n} independent replicates vs printed {printed}, outside the printed "
              f"precision, but an exact two-sided sign test needs {SIGN_MIN} replicates to reach 95% (p = {p:.3g} for "
              f"{n}); the runs were shown to differ ({ind.get('basis', '')})", needs_replicates=SIGN_MIN, **out)


def _condition(rel: str, margins: list[float]) -> dict:
    """An engineering compatibility test (a layer swapped in trains, a module runs): a condition
    each run must meet. No statistics and no performance conclusion: every run meets it, holds;
    no run meets it, violated; some do, inconclusive (unstable)."""
    n, strict = len(margins), relation(rel)[1] in ("<", ">")
    if not n:
        return _r("INCONCLUSIVE", f"no result carried every output the condition {rel!r} names")
    ok = sum(1 for m in margins if m > 0 or (not strict and m == 0))
    out = {"relation": rel, "n": n, "met": ok, "margin": round(min(margins), 6),
           "rule": "engineering compatibility condition, required in every run; no statistical inference and no "
                   "performance conclusion"}
    if ok == n:
        return _r("RELATION_HOLDS", f"{rel}: met in all {n} run(s) (compatibility only)", **out)
    if ok == 0:
        return _r("RELATION_VIOLATED", f"{rel}: met in none of {n} run(s)", **out)
    return _r("INCONCLUSIVE", f"{rel}: met in {ok} of {n} run(s): not stable, not decided", **out)


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
