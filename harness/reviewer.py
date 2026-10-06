"""The reviewer-facing report: about two pages per paper, in plain words, that a referee can read without the paper.

Everything that decides is the harness's: each main claim's decision (Verified, or Not verified with one specific
reason) is computed here from the ledger; what ran, on what, with which numbers, is printed from the check records;
denominators are the harness's own counts. The model writes only the plain-language parts (what the paper does, why
each decision came out as it did, the terms a reader needs), and each part is published only if every status word it
uses is earned by a cited check, every quote is in the paper and every number is a number the record or the paper
holds; otherwise it is withheld and the harness's own lines stand alone. Another reproduction record (an HF logbook)
is shown beside each claim, after the decisions were sealed, with whether the two tests are comparable; it never
changes a decision. Internal check codes appear only in the evidence lines.
"""
from __future__ import annotations

import bisect
import json
import re
from pathlib import Path

from . import report, state
from .reconcile import CONSTRUCTION_FAILED, FAILURE, SUPPORT

VERIFIED, NOT_VERIFIED = "VERIFIED", "NOT_VERIFIED"
# Each reason is one sentence of plain English; the decision's own facts follow it in `because`.
REASONS = {
    "supported": "The requested test ran as specified and its result supports the claim within the tested scope.",
    "existence_shown": "A valid example meets every printed assumption and shows the claimed property. One such example "
                       "shows that the stated object exists.",
    "contradicted": "The evidence contradicts the claim as printed.",
    "false_as_printed": "A case that meets every printed assumption violates the printed statement.",
    "proof_step_invalid": "A step of the printed proof fails on cases that meet its assumptions. This does not show "
                          "that the statement itself is false.",
    "construction_failed": "The paper's construction of an example failed on cases that meet its assumptions. This shows "
                           "a gap in the construction. It does not show that no example exists.",
    "premise_impossible": "A printed assumption can never hold, by a general argument that the verifier checked. So the "
                          "statement as printed applies to no case. A false assumption is not a counterexample.",
    "premise_not_met": "No tested case met the printed assumptions. The test says nothing about the statement as printed.",
    "notation_defect": "The statement is not defined as printed (for example, an index out of range). Under a corrected "
                       "reading it held on the tested cases, which is not a proof.",
    "changed_protocol": "The test ran only under a changed protocol, reading or dataset. Its result is about the "
                        "changed claim, not the printed one.",
    "interpretation_uncertain": "The result depends on how an ambiguous definition is read, and the readings give "
                                "different results.",
    "checks_disagree": "Two tests of the same printed statement disagree.",
    "incomplete_coverage": "Only part of the claimed scope was tested.",
    "scope_not_tested": "The test linked to this claim computed other cases than the ones the claim states. The claim "
                        "itself was not tested.",
    "witness_cases_only": "Valid examples exist for each tested case. The statement covers every case, and finite cases "
                          "do not show that.",
    "finite_cases_only": "No counterexample was found in the tested cases. Finite cases cannot prove a general "
                         "statement.",
    "undecided": "The test ran, but its result is within noise or undefined, so it decides nothing.",
    "missing_input": "The test could not run because required data or credentials were not available to this run. This "
                     "says nothing about the claim.",
    "resource_limit": "The test stopped at a limit of this run (time, memory or the number of tests). This says nothing "
                      "about the claim.",
    "test_failed": "The test started but failed before it gave a result, for a reason the harness recorded (a fault of "
                   "this run). This says nothing about the claim.",
    "blocked": "The test was refused by a gate of this run, for a reason the harness recorded.",
    "not_checked": "No test was run.",
    "pending": "The test has not finished.",
}
KIND_WORDS = {"RECONSTRUCTION": "a fresh run of the experiment", "RELEASED_DATA": "a recomputation from released files",
              "CERTIFICATE": "exact-arithmetic test cases", "AUTHOR_CODE": "the authors' documented command",
              "ARITHMETIC": "the paper's own printed numbers, recomputed"}
BASIS_WORDS = {"published_results": "an audit of the authors' released result files (nothing re-run)",
               "predictions": "a recomputation of the metric from released predictions or scores"}
_ROWS = 12         # ponytail: result rows shown per check (the rest are counted, and all are in ledger.json)


def _failure_audit(cs: list[dict], verdict: str) -> bool:
    return any(c["status"] in FAILURE and (c.get("audit") or {}).get("verdict") == verdict for c in cs)


def decision(cc: dict, by_id: dict) -> dict:
    """VERIFIED only when the requested test ran as specified, an audit found its code computes what the claim states, and
    the result supports the claim within the tested scope: for an existence statement, one valid example meeting every
    printed assumption; a universal statement is never verified by finite cases. Otherwise NOT_VERIFIED with one specific
    reason: contradicted, false as printed, an invalid proof step, a failed construction, an impossible or unmet premise, a
    notation defect, a changed protocol, an ambiguous reading, checks that disagree, incomplete coverage, a scope the test
    did not compute, examples for the tested cases only, finite cases only, undecided, missing input, a resource limit, a
    refusal, no test, or not finished. Missing evidence is never read as falsity."""
    st, comp, theory = cc.get("claim_status", ""), cc.get("completion") or {}, cc.get("claim_type") == "theory"
    form = report.claim_form(cc)
    cs = [by_id[k] for k in cc.get("checks") or [] if k in by_id]
    tgt = [c for c in cs if c.get("role", "target") == "target"]
    witness = [c for c in tgt if c["kind"] == "CERTIFICATE" and c.get("status") == "WITNESS_FOUND"]
    shown = comp.get("experiment") == "RAN_AS_SPECIFIED" and not comp.get("coverage_unverified")
    if st == "SUPPORT_FOUND" and theory:
        reason = ("existence_shown" if witness and form == "existential" and shown else
                  "witness_cases_only" if witness and form == "universal_existential" else
                  "incomplete_coverage" if witness and form == "existential" else "finite_cases_only")
    elif st == "SUPPORT_FOUND" and shown:
        reason = "supported"
    elif st == "NO_VIOLATION_FOUND" and theory:
        reason = "finite_cases_only"
    elif st == "SUPPORT_FOUND" and comp.get("experiment") == "RAN_WITH_CHANGES":
        reason = "changed_protocol"                 # one test as specified, another under a change: not the printed scope
    elif st == "SUPPORT_FOUND":
        reason = "incomplete_coverage"
    elif st == "FAILURE_FOUND":
        reason = "false_as_printed" if theory else "contradicted"
    elif st == "PROOF_GAP_FOUND":
        reason = "construction_failed" if any(c.get("status") == CONSTRUCTION_FAILED for c in tgt) else "proof_step_invalid"
    elif st == "PREMISE_NOT_MET":
        reason = "premise_impossible" if any(c.get("premise_argument") for c in tgt) else "premise_not_met"
    elif _failure_audit(tgt, "DEPENDS") or st == "READINGS_DISAGREE":
        reason = "interpretation_uncertain"
    elif st == "READING_CHANGED":
        certs = [c for c in tgt if c["kind"] == "CERTIFICATE" and c["status"] in ("NO_VIOLATION_FOUND",
                                                                                  "VIOLATION_UNDER_CHANGED_READING")]
        lit = lambda k: sum((c.get("literal") or {}).get(k) or 0 for c in certs)
        # The printed text evaluated on every case and held in all of them: the claim is untested only in the sense
        # that finite cases never prove it — a changed reading's violation is about the changed statement.
        printed_held = lit("holds") > 0 and not (lit("fails") or lit("undefined") or lit("premise_not_met"))
        never = lit("premise_not_met") > 0 and not (lit("holds") or lit("fails"))
        reason = (("premise_impossible" if any(c.get("premise_argument") for c in certs) else "premise_not_met")
                  if theory and never else "notation_defect" if theory and lit("undefined")
                  else "finite_cases_only" if theory and printed_held else "changed_protocol")
    elif st == "CHECKS_DISAGREE":
        reason = "checks_disagree"
    elif st == "PARTIAL_EVIDENCE":
        reason = "incomplete_coverage"
    elif st == "PENDING" or any(c["status"] == "PENDING" for c in tgt):
        reason = "pending"
    elif st == "NO_VIOLATION_FOUND":
        reason = "undecided"
    elif st == "NOTHING_DECIDED" and any(c.get("values") for c in tgt):
        reason = "undecided"
    elif (why := _stopped(cc, tgt, comp)):
        reason = why
    else:
        reason = "not_checked"
    # A test that computed none of the items the claim names (other cases: an ARL bound for an ADD claim) speaks for none
    # of it; a finding about the printed text itself (a premise, a definition, a failure, the readings) still stands.
    if comp.get("none_covered") and reason in ("supported", "existence_shown", "finite_cases_only", "witness_cases_only",
                                               "incomplete_coverage", "changed_protocol", "undecided"):
        reason = "scope_not_tested"
    text = REASONS[reason]
    if any(c["status"] == "VIOLATION_UNDER_CHANGED_READING" for c in tgt):
        if reason == "notation_defect":
            text = ("The statement is not defined as printed (for example, an index out of range). Under the changed "
                    "reading tested instead, cases violate it. That result is about the changed statement.")
        elif reason in ("finite_cases_only", "premise_impossible", "premise_not_met"):
            text += (" Cases do violate a changed reading of it. That result is about the changed statement, not the "
                     "printed one.")
    verified = reason in ("supported", "existence_shown")
    return {"decision": VERIFIED if verified else NOT_VERIFIED, "reason": reason, "reason_text": text,
            "because": _because(cc, tgt, reason, comp), "narrower": [] if verified else narrower(tgt)}


def _stopped(cc: dict, tgt: list[dict], comp: dict) -> str:
    """Why no test of the claim gave a result, when the harness (or the planner, for a claim no test took up) recorded a
    stop: missing input (data, credentials), a limit of this run (time, memory, the number of tests), or a refusal."""
    words = [b.get("blocker") for b in comp.get("not_run") or [] if b.get("blocker") not in ("not run", "pending")
             and (b.get("basis") == "harness" or (not tgt and not b.get("unverified")))]
    words += ["data" if c.get("data_blocker") else report.resource_word(c.get("resource"))
              if str(c.get("reason", "")).startswith("RESOURCE BLOCKER") else "refused" for c in tgt if c["status"] == "BLOCKED"]
    if not tgt and cc.get("blocker") and not cc.get("unverified"):
        words.append(cc["blocker"])
    if {"data", "credentials"} & set(words):
        return "missing_input"
    if {"compute", "cap"} & set(words):
        return "resource_limit"
    if "failed" in words:
        return "test_failed"
    return "blocked" if "refused" in words or any(c["status"] == "BLOCKED" for c in tgt) else ""


def narrower(tgt: list[dict]) -> list[str]:
    """What the tests did show, narrower than the claim: valid examples among the constructed cases, and the settings in
    which a comparison held (under every reading the check computed). A narrower finding never decides the claim."""
    out = []
    for c in tgt:
        moved = any(d.get("changes_claim") for d in c.get("deviations") or [])
        tail = " (under the changes the test recorded)" if moved else ""
        if c.get("witnesses"):
            out.append(f"{c['witnesses']} valid example(s) among {c.get('instances') or len(c.get('values') or [])} "
                       f"constructed cases{tail}")
        st = c.get("stages") or {}
        held = [s for s, p in st.items() if p.get("status") in SUPPORT + ("NO_VIOLATION_FOUND",)]
        if held and (len(held) < len(st) or c.get("status") not in SUPPORT):
            out.append(f"the comparison held in {len(held)} of {len(st)} settings" + (" under every reading" if c.get(
                "readings") else "") + (" that completed" if c.get("status") == "PARTIAL" else "") + ": "
                + ", ".join(held[:8]) + (" and more" if len(held) > 8 else "") + tail)
    return out


def _because(cc: dict, tgt: list[dict], reason: str, comp: dict) -> list[str]:
    """The facts a decision rests on, in the harness's words, each with who says so."""
    out = []
    for c in tgt:
        out.append(f"{_what(c)}: {_status_words(c)}")
        if c["status"] in FAILURE and (c.get("audit") or {}).get("verdict") == "STANDS":
            out.append(f"{c['id']}: an independent audit, shown the failing cases, found the failure rests on the paper's own words")
        if c["status"] in FAILURE and (c.get("image_check") or {}).get("agrees"):
            out.append(f"{c['id']}: every number was read off the page image and agrees with the extracted text")
    if comp.get("undefined_settings"):
        out.append("undefined (not measured): " + ", ".join(comp["undefined_settings"][:6])
                   + (" …" if len(comp["undefined_settings"]) > 6 else ""))
    if comp.get("changes"):
        out.append("changed from the paper: " + "; ".join(_short(x, 140) for x in comp["changes"][:3]))
    if reason in ("incomplete_coverage", "blocked", "not_checked", "undecided", "changed_protocol") and comp.get("scope_not_run"):
        out.append("not tested: " + ", ".join(comp["scope_not_run"][:8]) + (" …" if len(comp["scope_not_run"]) > 8 else ""))
    for b in (comp.get("not_run") or [])[:4]:
        if b.get("item", "").startswith("C") and b["item"][1:].isdigit():
            continue                               # a check's own blocker is already said above
        out.append(f"{b['item']}: {b['blocker']} ({'harness record' if b.get('basis') == 'harness' else b.get('basis', 'planner') + ' says'})"
                   + (f" — {_short(b['why'], 160)}" if b.get("why") else ""))
    if not tgt and cc.get("why_unchecked"):
        out.append(f"why no test (planner's words): {_short(cc['why_unchecked'], 240)}")
    return out[:8]


def _short(s, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _what(c: dict) -> str:
    w = KIND_WORDS.get(c["kind"], c["kind"])
    if c.get("basis") in BASIS_WORDS:
        w = BASIS_WORDS[c["basis"]]
    if c.get("test") == "compatibility":
        w = "an integration (compatibility) test"
    if c["kind"] == "CERTIFICATE" and c.get("step"):
        w = "exact-arithmetic test cases of one proof step"
    return f"{w} ({c['id']})"


def _status_words(c: dict) -> str:
    st, n = c["status"], c.get("counts") or {}
    words: str = {"RELATION_HOLDS": "the stated comparison held", "RELATION_VIOLATED": "the stated comparison did not hold",
             "RESOLVED_VERIFIED": "the printed number was reproduced within its precision",
             "FAILED_REPRODUCTION": "the result is outside the printed number's prediction interval",
             "NO_VIOLATION_FOUND": "no violation in the tested cases", "COUNTEREXAMPLE_FOUND": "a counterexample was found",
             "PREMISE_NOT_MET": "no tested case met the printed assumptions",
             "VIOLATION_UNDER_CHANGED_READING": "violated only under a changed reading",
             "READINGS_DIFFER": "the readings give different results", "PARTIAL": "only part of the planned runs completed",
             "INCONCLUSIVE": "not decided", "BLOCKED": "not run", "NOT_CHECKABLE": "no approved test",
             "ARITHMETIC_CONSISTENT": "the paper's numbers agree", "ARITHMETIC_CONTRADICTION": "the paper's numbers disagree",
             "PENDING": "not finished"}.get(st, str(st))
    if c["kind"] == "CERTIFICATE" and c.get("step") and st == "COUNTEREXAMPLE_FOUND":
        words = "the proof step as printed fails on cases that meet its assumptions (the theorem itself is not refuted)"
    if c.get("status_on_completed"):
        words += f" (on what completed: {c['status_on_completed'].lower().replace('_', ' ')})"
    if n.get("units_declared", 0) > 1:
        words += (f"; {n['units_with_result']} of {n['units_declared']} settings with a result"
                  + (f", {n['units_undefined']} undefined" if n.get("units_undefined") else "")
                  + (f", {n['units_not_completed']} missing" if n.get("units_not_completed") else ""))
    if st == "BLOCKED" and str(c.get("reason", "")).startswith("RESOURCE BLOCKER"):
        words = words.replace("not run", "stopped at a resource limit" + (" after its pilot run" if (n.get("launches") or 0) else ""), 1)
    if st in ("BLOCKED", "NOT_CHECKABLE", "INCONCLUSIVE") and c.get("reason"):
        words += f" — {_short(c['reason'], 200).rstrip('.')}"
    return words


# --- numbers: what was run and what it gave -----------------------------------------------------------------------
def fmt(v) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return str(v)
    if v == 0:
        return "0"
    a = abs(v)
    return f"{v:.5g}" if 1e-3 <= a < 1e5 else f"{v:.3e}"


def _group(stage: str) -> str:
    """The first token of a stage name made of several (`V|ARL|thr=1` -> `V`), for a compact summary only."""
    m = re.match(r"([^|/:\s]+)[|/:]", stage or "")
    return m.group(1) if m else ""


def result_rows(c: dict, limit: int = _ROWS) -> list[str]:
    """Markdown lines with the numbers a check produced: per stage (or per group of stages when there are many) the
    compared outputs' means, the margin of the stated comparison with its 95% band, the runs and how many of them were
    independent; for exact cases the counts; for a printed number the value produced beside it."""
    st, outs = c.get("stages") or {}, c.get("outputs") or {}
    t = c.get("target") or {}
    lines = []
    if c["kind"] == "CERTIFICATE":
        lit = {k: v for k, v in (c.get("literal") or {}).items() if v}
        words = {"holds": "hold", "fails": "fail", "undefined": "undefined", "premise_not_met": "premise not met"}
        n = len(c.get("values") or [])
        cases = c.get("instances") or n                     # older records: one line per case
        k = c.get("readings_per_instance") or 1
        if n:
            lines.append(f"- {cases} case{'s' if cases != 1 else ''}" + (f" ({n} result lines: each case under {k} readings)" if k > 1 else "")
                         + f"; {c.get('admissible_instances', c.get('admissible', 0))} met every assumption of the tested reading"
                         + ("; as printed: " + ", ".join(f"{v} {words.get(x, x)}" for x, v in lit.items()) if lit else ""))
        return lines
    by_reading = {f"{s} [{r}]": p for r, rp in (c.get("readings") or {}).items() if isinstance(rp, dict)
                  for s, p in (rp.get("stages") or {}).items()} if isinstance(c.get("readings"), dict) else {}
    if isinstance(c.get("readings"), dict) and not by_reading:          # a record with one status per reading only
        for r, p in c["readings"].items():
            lines.append(f"- reading **{r}**: {p.get('status', '').lower().replace('_', ' ')}" + (
                f", value {fmt(p['reproduced'])}" if isinstance(p.get("reproduced"), (int, float)) else
                f", margin {fmt(p['margin'])}" if isinstance(p.get("margin"), (int, float)) else ""))
    # Each reading is its own row beside the outputs it printed: a status alone hides the magnitudes and their signs.
    if by_reading:
        st = by_reading
    decided = {s: p for s, p in st.items() if p.get("status") not in ("UNDEFINED", "NOT_COMPLETED")}
    rel = t.get("relation")
    if st and len(decided) <= limit:
        head = list(dict.fromkeys([n for n in t.get("names") or [] if any(n in (outs.get(s) or {}) for s in decided)]
                                  + [n for s in decided for n in (outs.get(s) or {})]))[:4]
        cols = ["Setting", *head, "margin ± 95% band" if rel else "value", "runs (independent)", "result"]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|---" * len(cols) + "|")
        for s, p in decided.items():
            o = outs.get(s) or {}
            val = (f"{fmt(p.get('margin'))} ± {fmt(p.get('band', 0))}" if rel and "margin" in p else
                   fmt(p.get("reproduced")) if "reproduced" in p else "")
            runs = f"{p['n']} ({p.get('n_independent', p['n'])})" if "n" in p else ""
            lines.append("| " + " | ".join([s.strip() or "(all)", *(fmt(o[h]) if h in o else "" for h in head), val, runs,
                                             p["status"].lower().replace("_", " ")]) + " |")
    elif st:
        groups: dict = {}
        for s, p in decided.items():
            r = s[s.rfind(" [") + 2:-1] if by_reading else ""       # a reading is never pooled with another
            groups.setdefault((_group(s), r), []).append(p)
        for (g, r), ps in sorted(groups.items()):
            ms = sorted(p["margin"] for p in ps if isinstance(p.get("margin"), (int, float)))
            by: dict = {}
            for p in ps:
                by[p["status"]] = by.get(p["status"], 0) + 1
            label = ("settings " + repr(g) if g else "settings") + (f" under reading {r}" if r else "")
            lines.append(f"- {label}: {len(ps)} decided — " + ", ".join(
                f"{v} {k.lower().replace('_', ' ')}" for k, v in sorted(by.items(), key=lambda kv: -kv[1]))
                + (f"; margin median {fmt(ms[len(ms) // 2])}, range {fmt(ms[0])} to {fmt(ms[-1])}" if ms else ""))
    elif isinstance(c.get("values"), list) and c["values"] and t.get("value"):
        vs = c["values"]
        lines.append(f"- produced {fmt(sum(vs) / len(vs))} (mean of {len(vs)} run(s)) against the printed {t['value']}")
    n = c.get("counts") or {}
    if n.get("units_declared", 0) > 1 or n.get("launches", 0) > 1:
        ind = n.get("independent_replicates")
        lines.append(f"- counts: {n['units_declared']} settings declared = {n['units_with_result']} with a result + "
                     f"{n['units_undefined']} undefined + {n['units_not_completed']} missing"
                     + (f"; {len(n['readings'])} readings each" if n.get("readings") else "")
                     + f"; {n['launches']} launch(es)" + (f", {ind} independent" if ind is not None else "")
                     + f"; {n['result_lines']} result lines" + ("" if n.get("reconciles") else " — DOES NOT RECONCILE"))
    return lines


def ran_line(c: dict, root: Path) -> str:
    """What the check executed: the evidence kind, the data it read (its identity lines), the method and metric (the
    script author's fidelity table), and its runs."""
    parts = [_what(c)]
    fid = {f["aspect"]: f for f in c.get("fidelity") or [] if not f.get("not_applicable")}
    ident = [f"{n} ({_observed(i.get('observed'))})" if i.get("observed") else n
             for n, i in (c.get("data_identity") or {}).items() if isinstance(i, dict)][:3]
    if ident:
        parts.append("data: " + "; ".join(ident))
    elif fid.get("data"):
        parts.append("data: " + _sentence(fid["data"]["used"], 130))
    for a in ("model", "metric", "baselines"):
        if fid.get(a):
            parts.append(f"{a}: {_sentence(fid[a]['used'], 130)}")
    if c.get("covers"):
        parts.append("scope: " + ", ".join(c["covers"][:6]) + (f" and {len(c['covers']) - 6} more" if len(c["covers"]) > 6 else ""))
    n = c.get("counts") or {}
    if n.get("launches"):
        planned = n.get("launches_planned")
        parts.append((f"{n['launches']} of {planned} planned runs" if planned and planned != n["launches"] else
                      f"{n['launches']} run(s)") + (f" ({RUNS_WORDS.get(c['runs_source'], c['runs_source'])})"
                                                    if c.get("runs_source") else ""))
    return "; ".join(parts)


def flat_eq(a, b) -> bool:
    """Do two model-written reasons say the same thing (one may be a cut copy of the other)?"""
    a, b = report.flat(str(a or ""))[:120], report.flat(str(b or ""))[:120]
    return bool(a) and (a.startswith(b) or b.startswith(a))


def brief(c: dict) -> str:
    """One line for the main text: the kind of test, the datasets it read (by name), and its runs."""
    n, data = c.get("counts") or {}, list(c.get("data_identity") or {})[:3]
    planned = n.get("launches_planned")
    runs = (f"{n['launches']} of {planned} planned runs" if n.get("launches") and planned and planned != n["launches"]
            else f"{n['launches']} run(s)" if n.get("launches") else "")
    return _what(c) + (f" on {', '.join(data)}" if data else "") + (f"; {runs}" if runs else "")


def _details(c: dict, root: Path, prose, bears_on: list[str], led: dict) -> list[str]:
    """A test in full, once: what it ran on, its numbers and counts, paper/code differences and declared changes, the
    independent audit, and its records (the only place a check code leads a line)."""
    rows, shown = result_rows(c), set(result_rows(c, limit=6))     # what the main text printed is not printed again
    table = [r for r in rows if r.startswith("|")]
    out = [f"**{c['id']}** · {_what(c).rsplit(' (', 1)[0]} · bears on {', '.join(bears_on) or 'no main claim'}", "",
           f"- What ran: {ran_line(c, root)}.", f"- Result: {_status_words(c)}."]
    out += (["", *table, ""] if len(table) > 8 else []) + [f"  {r}" for r in rows if not r.startswith("|")
                                                           and (r not in shown or r.startswith("- counts:"))]
    out += [f"  - {n}" for n in fidelity_notes(c)]
    out += [f"  {n}" if n.startswith("  ") else f"  - {n}" for n in audit_notes(c, prose)]
    return out + [f"- Evidence: {evidence_line(c, led)}", ""]


def _observed(obs) -> str:
    """A dataset's identity line as words: its scalar facts, numbers to five significant digits."""
    if not isinstance(obs, dict):
        return _short(obs, 100)
    items = [(k, v) for k, v in obs.items() if isinstance(v, (int, float, str)) and not isinstance(v, bool)][:5]
    return ", ".join(f"{k} {fmt(v) if isinstance(v, (int, float)) else _short(v, 40)}" for k, v in items)


def _sentence(s, n: int) -> str:
    """The first whole sentence (or clause before a semicolon) of a model-written description, so a note is never cut
    mid-word; past `n` characters it is shortened as a last resort."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    m = re.match(r"(.{12,}?)(?:\.(?=\s+[A-Z(\"'])|;(?=\s))", s)
    return _short(m.group(1) + "." if m else s, n)


RUNS_WORDS = {"paper": "the run count the paper states", "referee": "count chosen by REFEREE; the paper states none",
              "referee_floor": "REFEREE's minimum for a noise band; the paper states fewer or none",
              "deterministic": "a deterministic computation", "compatibility": "the compatibility runs"}


def audit_notes(c: dict, prose) -> list[str]:
    """What the independent audit of a failure found, with the printed words a DEPENDS verdict rests on and what the
    other reading gives (the auditor's words, published only through the prose checks)."""
    a = c.get("audit") or {}
    if c["status"] not in FAILURE or not a.get("verdict"):
        return []
    if a["verdict"] == "STANDS":
        return ["Independent audit (shown the failing cases): the failure rests on the paper's own words."]
    if a["verdict"] != "DEPENDS":
        return ["Independent audit: unresolved, so the failure is not counted against the printed claim."]
    out = ["Independent audit (shown the failing cases): the failure depends on how these printed words are read:"]
    for d in (a.get("depends_on") or [])[:3]:
        if isinstance(d, dict) and d.get("printed"):
            alt = prose(_short(d.get("alternative"), 260), f"{c['id']} audit")
            out.append(f"  \"{_short(d['printed'], 160)}\" (p{d.get('page')})" + (f" — other reading: {alt}" if alt else ""))
    return out


def fidelity_notes(c: dict) -> list[str]:
    """Where the paper and the released code disagree, or the paper is silent, as the script author stated it."""
    out = []
    for f in c.get("fidelity") or []:
        if f.get("agrees") is False:
            out.append(f"{f['aspect']}: paper and code differ — paper \"{_short(f.get('paper'), 120)}\"; code "
                       f"`{(f.get('code') or {}).get('file', '')}`; " + (f"both computed (reading {f['reading']})"
                                                                         if f.get("reading") else _sentence(f.get("explained"), 200)))
    for d in c.get("deviations") or []:
        if d.get("changes_claim"):
            out.append("changed from the paper: " + _sentence(d.get("used"), 160) + (f" (only in reading {d['reading']})"
                                                                                   if d.get("reading") else ""))
    more = len(out) - 4
    return out[:4] + ([f"{more} more paper/code differences or changes are listed in `review.md`."] if more > 0 else [])


def evidence_line(c: dict, led: dict) -> str:
    sha = (c.get("script_sha256") or "")[:12]
    return (f"{c['id']}" + (f" · script `{sha}`" if sha else "") + (f" · command `{_short(c['command'], 80)}`" if c.get("command") else "")
            + (" · records `execution.jsonl`" if c.get("records") else "") + (f" · repo commit `{c['commit'][:10]}`" if c.get("commit") else "")
            + f" · `checks/{c['id']}/outcome.json`")


# --- prose validation ---------------------------------------------------------------------------------------------
_NUMTOK = re.compile(r"(?<![A-Za-z_\d.])(-?)(\d+(?:\.(\d+))?)(?:[eE]([-+]?\d+))?(%?)")


def numbers_of(text: str) -> list[float]:
    t = re.sub(r"(?<=\d),(?=\d{3})", "", text or "")
    return [float(f"{m.group(1)}{m.group(2)}" + (f"e{m.group(4)}" if m.group(4) else "")) for m in _NUMTOK.finditer(t)]


def unknown_numbers(text: str, allowed: list[float]) -> list[str]:
    """Numbers in model prose (signed, with a unit or a percent attached) that neither the record nor the paper holds to
    the digits written. Small whole counts (up to 12) are free; an id such as K3 or C7 is not a number."""
    bad, t = [], re.sub(r"(?<=\d),(?=\d{3})", "", text or "")
    for m in _NUMTOK.finditer(t):
        sign, body, dec, exp, pct = m.group(1), m.group(2), m.group(3) or "", m.group(4), m.group(5)
        v = float(f"{sign}{body}" + (f"e{exp}" if exp else ""))
        if not dec and not exp and not sign and v <= 12:
            continue
        tol = (0.5 * 10 ** -len(dec)) * (10 ** int(exp) if exp else 1) + 1e-12
        if not (_near(allowed, v, tol) or (pct and _near(allowed, v / 100, tol / 100))):
            bad.append(m.group(0))
    return bad


def _near(allowed: list[float], x: float, tol: float) -> bool:
    """Is some allowed number within `tol` of x (a rounding of it to the digits written)?"""
    i = bisect.bisect_left(allowed, x - tol)
    return i < len(allowed) and allowed[i] <= x + tol


def numeric_leaves(obj, out: set | None = None) -> set:
    """The numbers a record HOLDS as numbers (and the numbers in its keys, such as stage names): never the digits inside
    its strings — a sha256, a timestamp, a model-written reason are not measurements."""
    out = set() if out is None else out
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.update((float(obj), -float(obj), abs(float(obj))))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                out.update(numbers_of(k))
            numeric_leaves(v, out)
    elif isinstance(obj, list):
        for v in obj:
            numeric_leaves(v, out)
    return out


def harness_texts(led: dict) -> list[str]:
    """The reasons the harness itself wrote (a resource blocker's measurement, a limit it enforced): their numbers are
    the record's. A reason a model wrote (`reason_by` model) is not."""
    return [str(c.get("reason") or "") for c in led.get("checks") or [] if c.get("reason_by", "harness") == "harness"]


def allowed_numbers(record, *texts: str) -> list[float]:
    vals = numeric_leaves(record)
    for t in texts:
        for v in numbers_of(t):
            vals.update((v, -v, abs(v)))
    return sorted(vals)


# an HTML tag by name, a comment or an entity — never an inequality such as t_F<t_H or x<y>z
_MARKUP = re.compile(r"(?i)</?(?:a|b|i|u|s|em|strong|span|div|p|br|hr|img|script|style|sub|sup|table|tr|td|th|ul|ol|li|"
                     r"h[1-6]|font|iframe|code|pre|details|summary|svg|math|object|embed|link|meta|form|input|button)\b[^<>]*>"
                     r"|<!--|&#?\w+;")


_ATTRIBUTED = re.compile(r"(?i)^\s*the (?:other )?records?\b")


# A sentence about the other record (its entries, its judge, HF): its verdict words are the record's, not REFEREE's.
_RECORD_SENT = re.compile(r"(?i)\b(?:the|this|that|other|each|every|its|one|all|both)\s+(?:\w+\s+)?(?:records?|entries|entry|"
                          r"summar(?:y|ies)|logbooks?|judges?)\b|\brecord's\b|\bHF\b")


def problems(text: str, led: dict, paper, allowed: list[float], refs_flat: str = "", record: bool = False,
             verified: bool = False) -> list[str]:
    """Why a model-written passage cannot be published: markup or an entity (it would render as something the checks never
    saw), a status word no cited check earns, a quote not in the paper (or the registered record), a number the record
    and the paper do not hold. `record`: the passage says what ANOTHER reproduction record found; when every sentence of
    it is attributed to that record ("The record reports ..."), its verdict words are the record's, not REFEREE's, and
    are not REFEREE status language (numbers and quotes are still checked)."""
    if not text:
        return []
    if _MARKUP.search(text):
        return ["markup or a character entity in the text"]
    quotes = [q for q in re.findall(r'"([^"\n]*)"', text) if len(q) >= 20 and not paper.occurs(q.replace("\\n", "\n"))
              and report.flat(q) not in refs_flat]
    theirs = record and bool(_ATTRIBUTED.match(text))      # the field is the record's finding, opened as such
    body = _unquote(text, paper, refs_flat)
    if record and not theirs:
        # In a comparison, a sentence about the other record (and not about REFEREE) reports that record's verdict; a
        # sentence about REFEREE may use its own decision word only when that is the claim's decision (`verified`).
        body = " ".join(s for s in re.split(r"(?<=[.!?])\s+", body) if "REFEREE" in s or not _RECORD_SENT.search(s))
    status = [] if theirs else [f"status language: {s[:120]!r}" for s in report.unearned(body, led, earned_support=verified)]
    return (status[:2] + [f"quote not in the paper or the record: {q[:80]!r}" for q in quotes][:2]
            + [f"number not in the record or the paper: {n}" for n in unknown_numbers(text, allowed)][:3])


def _unquote(text: str, paper, refs_flat: str = "") -> str:
    """Text with the quoted spans removed that are someone else's words — the paper's, or a registered reference
    record's, re-found there — so a status word inside them is not REFEREE's assertion. Any other quoted word stays."""
    def keep(m):
        q = m.group(1)
        return " " if len(q) >= 8 and (paper.occurs(q) or report.flat(q) in refs_flat) else m.group(0)
    return re.sub(r'"([^"\n]*)"', keep, text or "")


# --- the pages --------------------------------------------------------------------------------------------------------
_DASHES = re.compile(r"\s*[—―]\s*|\s+–\s+")


def _undash(text: str) -> str:
    """No dash as punctuation in the harness's or a model's words (the reader's style rule): an em dash or a spaced en
    dash becomes a semicolon, an en dash between numbers "to". The paper's own words, quoted, are kept as printed."""
    out = []
    for part in re.split(r'("[^"\n]*")', text):
        if part.startswith('"') and part.endswith('"') and len(part) > 1:
            out.append(part)
        else:
            part = re.sub(r"(?<=\d)–(?=\d)", " to ", part)
            out.append(_DASHES.sub("; ", part))
    return "".join(out)


def _ident(p: dict) -> str:
    """The paper's public identifier: its arXiv id and version, else the PDF's file name (never a path on this host)."""
    if p.get("arxiv_id"):
        return f"arXiv {p['arxiv_id']}{p.get('arxiv_version', '')}"
    name = re.split(r"[\\/]", str(p.get("source") or ""))[-1]
    return f"PDF file {name}" if name else "paper PDF"


def render(x, led: dict, rep: dict | None, cmp: dict | None = None) -> str:
    """Write the two-page reviewer page (reviewer.md) and the trace record that maps every claim to its tests by code
    (trace.md); return the page. Model prose is published on either only through the same checks."""
    rep, cmp = rep or {}, cmp or {}
    refs = _reference_text(x.root)
    allowed = allowed_numbers(led, "\n".join(x.paper.pages) if hasattr(x.paper, "pages") else "", refs, *harness_texts(led))
    refs_flat = report.flat(refs)
    held: list[str] = []

    def prose(text: str, where: str, record: bool = False, verified: bool = False) -> str:
        bad = problems(text, led, x.paper, allowed, refs_flat, record, verified)
        if bad:
            if not any(h.startswith(f"{where}: ") for h in held):
                held.append(f"{where}: " + "; ".join(bad))
            return ("_(Model-written text withheld here: it used a number, a quotation or a status word that the record "
                    "does not support. The reason is in `reviewer.withheld.json`.)_")
        return text.strip()
    trace_text = _undash(trace(x, led, rep, cmp, prose))
    (x.root / "trace.md").write_text(trace_text, encoding="utf-8")
    text = _undash(page(x, led, rep, cmp, prose))
    (x.root / "reviewer.md").write_text(text, encoding="utf-8")
    state.write_json(x.root / "reviewer.withheld.json", held)
    return text


def _theirs(prose, text: str, where: str) -> str:
    """A field that is the other record's own finding (its verdict, its measurement): its verdict words are the
    record's; its numbers and quotes are still checked against the registered record."""
    got = prose("The record reports: " + text, where, record=True)
    return text if got.startswith("The record reports: ") else got


def _cell(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "/").strip()


def page(x, led: dict, rep: dict, cmp: dict, prose) -> str:
    """The reviewer page, on the comparison template: the paper; each main claim (grouped where related) beside what the
    other record found and what this run executed, with its decision; how the tests line up; what the evidence
    supports; terms and symbols; the record checked. Full descriptions only: codes are in the trace record."""
    p, s = led["paper"], led["source"]
    claims, by_id = led["central_claims"], {c["id"]: c for c in led["checks"]}
    pos = {cc.get("id"): i + 1 for i, cc in enumerate(claims)}
    mine = {str(e.get("id")): e for e in rep.get("claims") or [] if isinstance(e, dict)}
    other = {str(e.get("id")): e for e in cmp.get("claims") or [] if isinstance(e, dict)}
    title = lambda cc: _cell(mine.get(cc.get("id"), {}).get("title")) or _short(cc.get("statement") or cc["quote"], 90)
    refs = state.read_json(x.root / "reference" / "index.json", []) or []
    entries = sorted({f"{_cell((o.get('entry') or {}).get('space'))} at `{_cell((o.get('entry') or {}).get('revision'))[:10]}`"
                      for o in other.values() if (o.get("entry") or {}).get("space")})
    lines = [f"# {p['title']}", "",
             f"**Paper:** {_ident(p)}, PDF sha256 `{p['sha256'][:12]}`. **Authors' code:** "
             + (f"{s['url']} at `{s['commit'][:10]}`." if s.get("url") else "none attributed.")
             + (f" **Other record:** {', '.join(_cell(r.get('source')) for r in refs)}"
                + (f"; selected entries: {'; '.join(entries[:4])}" if entries else "") + "." if refs else
                " **Other record:** none registered.") + f" **This run:** REFEREE harness `{_harness_commit()}`.", "",
             "_A first-pass review aid. It makes no accept or reject recommendation. The harness computes every decision "
             "from the recorded tests. The plain-language parts were written by a model and checked against the record. "
             "The other record is another attempt, not ground truth._", "",
             "## The paper", "", prose(rep.get("overview", ""), "overview") or "_(no overview)_", ""]
    n_ver = sum(1 for cc in claims if (cc.get("decision") or {}).get("decision") == VERIFIED)
    lines += ["## Claims and evidence", "",
              f"The paper makes {len(claims)} main claims, extracted before any test was planned. {n_ver} verified, "
              f"{len(claims) - n_ver} not verified. The trace record (`trace.md`) maps each claim to its tests.", "",
              "| Claim in the paper | Other record | This REFEREE run |", "|---|---|---|"]
    for group in _groups(claims, rep):
        left, mid, right = [], [], []
        for cc in group:
            k, d, o = cc.get("id"), cc.get("decision") or {}, other.get(cc.get("id"))
            left.append(f"**{pos[k]}. {title(cc)}.** {_cell(cc.get('statement') or cc['quote'])} (p{cc.get('page')})")
            if not o:
                mid.append("No other record registered." if not refs else "Not covered by the record.")
            elif o.get("agreement") == "not_covered" and not o.get("hf_verdict"):
                mid.append("Not covered by the record.")
            else:
                verdict = _short(_cell(o.get("hf_verdict") or o.get("reference_finding")), 200)
                meas = _theirs(prose, _cell(o.get("hf_measurement")), f"{k} record measurement") if o.get("hf_measurement") else ""
                mid.append(f"{pos[k]}: " + _theirs(prose, verdict, f"{k} record verdict") + (f" {meas}" if meas else ""))
            res = mine.get(k, {}).get("result")
            said = prose(_cell(res), f"{k} result") if res else _harness_result(cc, by_id)
            right.append(f"{pos[k]}: {said} **{'Verified' if d.get('decision') == VERIFIED else 'Not verified'}** "
                         f"({REASON_WORDS.get(d.get('reason', ''), d.get('reason', '').replace('_', ' '))}).")
        lines.append("| " + " | ".join("<br>".join(col) for col in (left, mid, right)) + " |")
    lines += ["", "## How the tests line up", "",
              "- **Other record (data, model, baselines, seeds, metric):** "
              + (prose(_cell(cmp.get("hf_setup")), "record setup", record=True) if cmp.get("hf_setup") else "not stated."),
              "- **This run (data, model, baselines, seeds, metric):** "
              + (prose(_cell(rep.get("setup")), "our setup") if rep.get("setup") else "see the trace record."),
              "- **What differs, or why a test did not run:** "
              + (prose(_cell(cmp.get("differences")), "differences", record=True) if cmp.get("differences") else "see the trace record."),
              "", "## Comparison", ""]
    for cc in claims:
        k, d, o = cc.get("id"), cc.get("decision") or {}, other.get(cc.get("id")) or {}
        sup = o.get("supports") or ""
        if sup:
            lines.append(f"- **For claim {pos[k]}, the evidence supports:** "
                         + prose(_cell(sup), f"{k} supports", True, d.get("decision") == VERIFIED))
    if cmp.get("overall"):
        lines.append("- **Overall comparison and limits of this conclusion:** "
                     + prose(_cell(cmp["overall"]), "overall", record=True))
    if not cmp:
        lines.append("- No other record is registered for this paper, so there is nothing to compare.")
    terms = [t for t in rep.get("terms") or [] if isinstance(t, dict) and t.get("term") and t.get("definition")][:10]
    if terms:
        lines += ["", "## Terms and symbols", ""] + [
            f"- **{_short(t['term'], 40)}**: {prose(_short(t['definition'], 240), 'term ' + str(t['term'])[:20])}" for t in terms]
    revs = sorted({_cell((o.get("entry") or {}).get("revision")) for o in other.values() if (o.get("entry") or {}).get("revision")})
    lines += ["", "## Record checked", "",
              f"Paper: PDF sha256 `{p['sha256'][:12]}`. Other record revision: "
              + (", ".join(f"`{r[:10]}`" for r in revs[:4]) if revs else "not stated") + ". Run output: "
              f"`projects/{x.root.name}/` (this page `reviewer.md`, trace record `trace.md`, ledger `ledger.json`, every "
              "process `execution.jsonl`)."]
    return "\n".join(lines) + "\n"


# The decision reasons in a reader's words (the decision line itself is the harness's).
REASON_WORDS = {"supported": "supported within the tested scope", "existence_shown": "a valid example shows it",
                "contradicted": "contradicted", "false_as_printed": "false as printed", "construction_failed":
                "the paper's construction failed in tested cases", "proof_step_invalid": "a proof step fails as printed",
                "premise_impossible": "a printed assumption can never hold", "premise_not_met":
                "no tested case met the assumptions", "notation_defect": "undefined as printed",
                "changed_protocol": "tested only under a changed protocol", "interpretation_uncertain":
                "depends on how a definition is read", "checks_disagree": "tests disagree", "incomplete_coverage":
                "only part of the scope tested", "scope_not_tested": "the test computed other cases",
                "witness_cases_only": "examples for tested cases only", "finite_cases_only": "finite cases only",
                "undecided": "within noise", "missing_input": "missing data or credentials", "resource_limit":
                "a time or memory limit of this run", "test_failed": "the test failed to run", "blocked": "refused by a gate",
                "not_checked": "no test", "pending": "not finished"}


def _groups(claims: list[dict], rep: dict) -> list[list[dict]]:
    """Rows of the claims table: the report writer's groups of related claims (each claim at most once, in the
    extraction order), every other claim alone."""
    by = {cc.get("id"): cc for cc in claims}
    seen, rows = set(), []
    first = {}
    for g in rep.get("groups") or []:
        ids = [i for i in g.get("claims") or [] if i in by and i not in seen]
        if len(ids) > 1:
            seen |= set(ids)
            first[ids[0]] = [by[i] for i in ids]
    for cc in claims:
        k = cc.get("id")
        if k in first:
            rows.append(first[k])
        elif k not in seen:
            rows.append([cc])
    return rows


def _harness_result(cc: dict, by_id: dict) -> str:
    """What ran for a claim and what it showed, in the harness's own words (when no checked model sentence exists)."""
    tgt = [by_id[i] for i in cc.get("checks") or [] if i in by_id and by_id[i].get("role", "target") == "target"]
    if not tgt:
        return "Not run: no test was planned for it."
    return " ".join(f"{_what(c).rsplit(' (', 1)[0].capitalize()}: {_status_words(c).split(' — ')[0]}." for c in tgt[:2])


def trace(x, led: dict, rep: dict, cmp: dict, prose) -> str:
    """The trace record: every claim by its code (K1..) with its decision, the tests (C1..) behind it with their
    numbers, readings, deviations, audits, blockers and the other record beside it; every test once, in full."""
    p, s = led["paper"], led["source"]
    by_id = {c["id"]: c for c in led["checks"]}
    claims = led["central_claims"]
    code = f"authors' code {s['url']} at `{s['commit'][:10]}`" if s.get("url") else "no author code attributed"
    lines = [f"# Trace record: {p['title']}", "",
             f"{_ident(p)} · PDF sha256 `{p['sha256'][:12]}` · {code} · harness `{_harness_commit()}`", "",
             "_This record maps each finding to its tests. K1 to Kn are the paper's main claims, numbered as they were "
             "extracted before any test. C1 to Cn are REFEREE's tests (checks), numbered as planned. The two-page page "
             "(`reviewer.md`) states the same decisions in words. Decisions are computed by the harness; model-written "
             "text is published only after the same checks._", ""]
    verified = [cc for cc in claims if (cc.get("decision") or {}).get("decision") == VERIFIED]
    first = sum(1 for cc in claims if cc.get("id") and cc.get("origin") != "plan:2")
    added = sum(1 for cc in claims if cc.get("origin") == "plan:2")
    how = (f"{first} main claims were extracted from the paper before any test was planned"
           + (f"; {added} more were added by the follow-up round" if added else "") if first else
           f"{len(claims)} main claims were listed by the planner (no extraction is on record)")
    lines += ["## Decisions on the main claims", "",
              f"{how}. {len(verified)} verified; {len(claims) - len(verified)} not verified.", "",
              "| Claim | Form | Decision | Reason |", "|---|---|---|---|"]
    for cc in claims:
        d = cc.get("decision") or {}
        lines.append(f"| {cc.get('id', '')}. {_short(cc.get('statement') or cc['quote'], 140)} | "
                     f"{report.claim_form(cc)}{' (classified by the audit)' if cc.get('form_by') == 'audit' else ''} | "
                     f"**{'Verified' if d.get('decision') == VERIFIED else 'Not verified'}** | "
                     f"{d.get('reason', '').replace('_', ' ')} |")
    lines.append("")
    expl = {str(e.get("id")): e.get("explanation") or e.get("result") or "" for e in rep.get("claims") or [] if isinstance(e, dict)}
    other = {str(e.get("id")): e for e in cmp.get("claims") or [] if isinstance(e, dict)}
    shown: dict = {}
    details: list[str] = []          # each test once, in full, after the claims
    cites = {}
    for cc in claims:
        for i in cc.get("checks") or []:
            cites.setdefault(i, []).append(cc.get("id", ""))
    for cc in claims:
        d, comp, k = cc.get("decision") or {}, cc.get("completion") or {}, cc.get("id", "")
        lines += [f"### {k}. {_short(cc.get('statement') or cc['quote'], 200)}", "",
                  f"**Decision: {'Verified' if d.get('decision') == VERIFIED else 'Not verified'}.** {d.get('reason_text', '')}",
                  "", f"The paper (p{cc.get('page')}): \"{_short(cc['quote'], 300)}\""]
        if cc.get("assumptions"):
            lines += ["", "Assumptions as printed: " + "; ".join(f"\"{_short(a['quote'], 120)}\"" for a in cc["assumptions"][:3])]
        if expl.get(k):
            lines += ["", prose(expl[k], f"{k} explanation")]
        lines.append("")
        tgt = [by_id[i] for i in cc.get("checks") or [] if i in by_id and by_id[i].get("role", "target") == "target"]
        sup = [by_id[i] for i in cc.get("checks") or [] if i in by_id and by_id[i].get("role") == "supporting"]
        for c in tgt:
            if c["id"] in shown:            # a test is described once; a later claim it bears on points back to it
                lines.append(f"- **What ran:** {_what(c)}, the same test as under {shown[c['id']]}. Result: {_status_words(c)}.")
                continue
            shown[c["id"]] = k
            lines.append(f"- **What ran:** {brief(c)}. Result: {_status_words(c)}.")
            if (rel := (c.get("target") or {}).get("relation")):
                lines.append(f"  - compared: `{rel}` (margin = left side minus right side; "
                             + ("the paper's own comparison)" if c.get("criterion") == "stated" else
                                "a criterion chosen by REFEREE, which the paper's sentence does not state)"))
            lines += [f"  - {n}" for n in output_notes(c)]
            rows = [r for r in result_rows(c, limit=6) if not r.startswith("- counts:")]
            table = [r for r in rows if r.startswith("|")]
            lines += (["", *table, ""] if table else []) + ["  " + r for r in rows if not r.startswith("|")]
            if d.get("reason") == "changed_protocol":
                lines += [f"  - changed from the paper: {_sentence(u, 160)}" for u in (comp.get("changes") or [])[:2]]
            audit = audit_notes(c, prose)
            lines += [f"  {n}" if n.startswith("  ") else f"  - {n}" for n in audit[:2]]
            details += _details(c, x.root, prose, cites.get(c["id"], []), led)
        for c in sup:
            lines.append(f"- Supporting test (does not decide the claim): {_what(c)}; {_status_words(c)}.")
            if c["id"] not in shown:
                shown[c["id"]] = k
                details += _details(c, x.root, prose, cites.get(c["id"], []), led)
        for n in d.get("narrower") or []:
            lines.append(f"- **Narrower finding (does not decide the claim):** {n}.")
        for t in comp.get("transfers") or []:
            lines.append(f"- **Argued by transfer, not computed:** {t['item']} ({t['check']}): {_sentence(t.get('argument'), 200)}")
        if comp.get("coverage_unverified"):
            lines.append(f"- **Coverage not verified:** no audit read what {', '.join(comp['coverage_unverified'])} computed.")
        if comp.get("scope_not_run"):
            lines.append("- **Not tested to completion** (no test covering it computed it and ran all its runs): "
                         + ", ".join(comp["scope_not_run"][:12]) + ("; and more" if len(comp["scope_not_run"]) > 12 else "") + ".")
        # Only a reason of its own is listed: an item whose test did not finish says so under "What ran" already.
        why_not = [b for b in comp.get("not_run") or [] if not (b.get("item", "").startswith("C") and b["item"][1:].isdigit())
                   and b.get("blocker") not in ("not run", "pending")]
        for b in why_not[:3]:
            lines.append(f"  - {b['item']}: {b['blocker']}; {_sentence(b.get('why'), 160)} "
                         f"({'harness record' if b.get('basis') == 'harness' else (b.get('basis') or 'planner') + ' says'})")
        if len(why_not) > 3:
            lines.append(f"  - {len(why_not) - 3} more items with a stated reason are in `ledger.json`.")
        if not tgt and cc.get("why_unchecked") and not any(flat_eq(b.get("why"), cc["why_unchecked"]) for b in why_not):
            lines.append(f"- **No test ran.** The planner's reason: {_sentence(cc['why_unchecked'], 300)}")
        o = other.get(k)
        if o:
            said = "; ".join(f"\"{_short(q, 160)}\"" for q in o.get("quotes") or [])
            finding = o.get("hf_verdict") or o.get("reference_finding") or ""
            lines.append(f"- **Other reproduction record ({_short(o.get('source', 'reference'), 60)}):** "
                         + (f"entry {_cell((o.get('entry') or {}).get('space'))}; " if (o.get("entry") or {}).get("space") else "")
                         + f"{prose(_short(finding, 300), k + ' reference finding', record=True)}"
                         + (f" {prose(_short(o['hf_measurement'], 300), k + ' record measurement', record=True)}"
                            if o.get("hf_measurement") else "")
                         + (f" In its words: {said}." if said else "")
                         + f" Comparable: {o.get('comparable', '?')}. Agreement: {str(o.get('agreement', '')).replace('_', ' ')}."
                         + (f" {prose(o['why'], k + ' reference', True, d.get('decision') == VERIFIED)}" if o.get("why") else ""))
        if tgt + sup:
            lines.append("- Evidence: " + ", ".join(c["id"] for c in tgt + sup) + " (under Test details)")
        lines.append("")
    if details:
        lines += ["## Test details", "", "_Each test once: what it ran on, the paper/code differences and changes its "
                  "script declared, the full result counts, the independent audit, and its records._", "", *details]
    terms = [t for t in rep.get("terms") or [] if isinstance(t, dict) and t.get("term") and t.get("definition")][:12]
    if terms:
        lines += ["## Terms", ""] + [f"- **{_short(t['term'], 60)}**: {prose(_short(t['definition'], 300), 'term ' + str(t['term'])[:20])}"
                                     for t in terms] + [""]
    lines += ["## Record", "",
              f"Full trace of statuses: `review.md`; machine ledger: `ledger.json`; every process: `execution.jsonl`. "
              f"Workflow: {led['workflow']['finished']} of {led['workflow']['checks_planned']} planned tests reached an end "
              "state (an end state is not a reproduction)."]
    return "\n".join(lines) + "\n"


def output_notes(c: dict) -> list[str]:
    """What each compared output is, as an independent audit of the script found it (definition, unit, aggregation)."""
    return [f"{o['name']}" + (f" [{o['reading']}]" if o.get("reading") else "") + f" is {o['definition']} (unit: {o['unit']}; "
            f"{o['aggregation']}; audited)" for o in ((c.get("scope_audit") or {}).get("outputs") or [])[:6]]


def _reference_text(root: Path) -> str:
    """The registered reference records' text (for re-finding their quotes and numbers); empty when none."""
    out = []
    for r in state.read_json(Path(root) / "reference" / "index.json", []) or []:
        try:
            out.append((Path(root) / "reference" / r["file"]).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(out)


def _harness_commit() -> str:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short=10", "HEAD"], cwd=state.ROOT, capture_output=True, text=True,
                             timeout=20).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "harness"], cwd=state.ROOT, capture_output=True, text=True,
                               timeout=20).stdout.strip()
        return out + ("+dirty" if dirty else "")
    except (OSError, ValueError):
        return "unknown"
