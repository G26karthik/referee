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
from .reconcile import FAILURE

VERIFIED, NOT_VERIFIED = "VERIFIED", "NOT_VERIFIED"
# Each reason is one sentence of plain English; the decision's own facts follow it in `because`.
REASONS = {
    "supported": "The requested test ran as specified and its result supports the claim within the tested scope.",
    "contradicted": "The evidence contradicts the claim as printed.",
    "false_as_printed": "A case that meets every printed assumption violates the printed statement.",
    "proof_step_invalid": "A step of the printed proof fails on cases that meet its assumptions. This does not show "
                          "that the statement itself is false.",
    "premise_impossible": "A printed assumption cannot hold, so the statement as printed applies to no case.",
    "notation_defect": "The statement is not defined as printed (for example, an index out of range). Under a corrected "
                       "reading it held on the tested cases, which is not a proof.",
    "changed_protocol": "The test ran only under a changed protocol, reading or dataset. Its result is about the "
                        "changed claim, not the printed one.",
    "interpretation_uncertain": "The result depends on how an ambiguous definition is read, and the readings give "
                                "different results.",
    "checks_disagree": "Two tests of the same printed statement disagree.",
    "incomplete_coverage": "Only part of the claimed scope was tested.",
    "finite_cases_only": "No counterexample was found in the tested cases. Finite cases cannot prove a general "
                         "statement.",
    "undecided": "The test ran, but its result is within noise or undefined, so it decides nothing.",
    "blocked": "The test could not run, for a reason the harness recorded.",
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
    """VERIFIED only when the requested test ran as specified and supports the claim within the tested scope; a
    mathematical statement is never verified by finite cases. Otherwise NOT_VERIFIED with one specific reason:
    contradicted, false as printed, an invalid proof step, an impossible premise, a notation defect, a changed
    protocol, an ambiguous reading, checks that disagree, incomplete coverage, finite cases only, undecided, a recorded
    blocker, no test, or not finished. Missing evidence is never read as falsity."""
    st, comp, theory = cc.get("claim_status", ""), cc.get("completion") or {}, cc.get("claim_type") == "theory"
    cs = [by_id[k] for k in cc.get("checks") or [] if k in by_id]
    tgt = [c for c in cs if c.get("role", "target") == "target"]
    if st == "SUPPORT_FOUND" and not theory and comp.get("experiment") == "RAN_AS_SPECIFIED":
        reason = "supported"
    elif st in ("SUPPORT_FOUND", "NO_VIOLATION_FOUND") and theory:
        reason = "finite_cases_only"
    elif st == "SUPPORT_FOUND" and comp.get("experiment") == "RAN_WITH_CHANGES":
        reason = "changed_protocol"                 # one test as specified, another under a change: not the printed scope
    elif st == "SUPPORT_FOUND":
        reason = "incomplete_coverage"
    elif st == "FAILURE_FOUND":
        reason = "false_as_printed" if theory else "contradicted"
    elif st == "PROOF_GAP_FOUND":
        reason = "proof_step_invalid"
    elif st == "PREMISE_NOT_MET":
        reason = "premise_impossible"
    elif _failure_audit(tgt, "DEPENDS") or st == "READINGS_DISAGREE":
        reason = "interpretation_uncertain"
    elif st == "READING_CHANGED":
        certs = [c for c in tgt if c["kind"] == "CERTIFICATE" and c["status"] in ("NO_VIOLATION_FOUND",
                                                                                  "VIOLATION_UNDER_CHANGED_READING")]
        lit = lambda k: sum((c.get("literal") or {}).get(k) or 0 for c in certs)
        # The printed text evaluated on every case and held in all of them: the claim is untested only in the sense
        # that finite cases never prove it — a changed reading's violation is about the changed statement.
        printed_held = lit("holds") > 0 and not (lit("fails") or lit("undefined") or lit("premise_not_met"))
        reason = ("notation_defect" if theory and lit("undefined") else "finite_cases_only" if theory and printed_held
                  else "changed_protocol")
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
    elif any(c["status"] == "BLOCKED" for c in tgt) or any(
            b.get("basis") == "harness" and b.get("blocker") not in ("not run", "pending") for b in comp.get("not_run") or []):
        reason = "blocked"
    else:
        reason = "not_checked"
    text = REASONS[reason]
    if any(c["status"] == "VIOLATION_UNDER_CHANGED_READING" for c in tgt):
        if reason == "notation_defect":
            text = ("The statement is not defined as printed (for example, an index out of range). Under the changed "
                    "reading tested instead, cases violate it; that is about the changed statement, not the printed one.")
        elif reason == "finite_cases_only":
            text += (" Cases do violate a changed reading of it; that is about the changed statement, not the printed "
                     "one.")
    return {"decision": VERIFIED if reason == "supported" else NOT_VERIFIED, "reason": reason,
            "reason_text": text, "because": _because(cc, tgt, reason, comp)}


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
            lines.append(f"- {cases} cases" + (f" ({n} result lines: each case under {k} readings)" if k > 1 else "")
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
            r = s[s.rfind(" ["):] if by_reading else ""             # a reading is never pooled with another
            groups.setdefault((_group(s) + r).strip(), []).append(p)
        for g, ps in sorted(groups.items()):
            ms = sorted(p["margin"] for p in ps if isinstance(p.get("margin"), (int, float)))
            by: dict = {}
            for p in ps:
                by[p["status"]] = by.get(p["status"], 0) + 1
            lines.append(f"- {'settings ' + repr(g) if g else 'settings'}: {len(ps)} decided — " + ", ".join(
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


def allowed_numbers(record, *texts: str) -> list[float]:
    vals = numeric_leaves(record)
    for t in texts:
        for v in numbers_of(t):
            vals.update((v, -v, abs(v)))
    return sorted(vals)


_MARKUP = re.compile(r"<[A-Za-z/!]|&#?\w+;")


def problems(text: str, led: dict, paper, allowed: list[float], refs_flat: str = "") -> list[str]:
    """Why a model-written passage cannot be published: markup or an entity (it would render as something the checks never
    saw), a status word no cited check earns, a quote not in the paper (or the registered record), a number the record
    and the paper do not hold."""
    if not text:
        return []
    if _MARKUP.search(text):
        return ["markup or a character entity in the text"]
    quotes = [q for q in re.findall(r'"([^"\n]*)"', text) if len(q) >= 20 and not paper.occurs(q.replace("\\n", "\n"))
              and report.flat(q) not in refs_flat]
    return ([f"status language: {s[:120]!r}" for s in report.unearned(_unquote(text, paper, refs_flat), led)][:2]
            + [f"quote not in the paper or the record: {q[:80]!r}" for q in quotes][:2]
            + [f"number not in the record or the paper: {n}" for n in unknown_numbers(text, allowed)][:3])


def _unquote(text: str, paper, refs_flat: str = "") -> str:
    """Text with the quoted spans removed that are someone else's words — the paper's, or a registered reference
    record's, re-found there — so a status word inside them is not REFEREE's assertion. Any other quoted word stays."""
    def keep(m):
        q = m.group(1)
        return " " if len(q) >= 8 and (paper.occurs(q) or report.flat(q) in refs_flat) else m.group(0)
    return re.sub(r'"([^"\n]*)"', keep, text or "")


# --- the page ------------------------------------------------------------------------------------------------------
def render(x, led: dict, rep: dict | None, cmp: dict | None = None) -> str:
    p, s = led["paper"], led["source"]
    rep, cmp = rep or {}, cmp or {}
    by_id = {c["id"]: c for c in led["checks"]}
    claims = led["central_claims"]
    refs = _reference_text(x.root)
    allowed = allowed_numbers(led, "\n".join(x.paper.pages) if hasattr(x.paper, "pages") else "", refs)
    refs_flat = report.flat(refs)
    held: list[str] = []

    def prose(text: str, where: str) -> str:
        bad = problems(text, led, x.paper, allowed, refs_flat)
        if bad:
            held.append(f"{where}: " + "; ".join(bad))
            return ("_(Model-written text withheld here: it used a number, a quotation or a status word that the record "
                    "does not support. The reason is in `reviewer.withheld.json`.)_")
        return text.strip()

    ident = (f"arXiv {p['arxiv_id']}{p.get('arxiv_version', '')}" if p.get("arxiv_id") else p.get("source", "")) or "paper PDF"
    code = f"authors' code {s['url']} at `{s['commit'][:10]}`" if s.get("url") else "no author code attributed"
    commit = _harness_commit()
    lines = [f"# {p['title']}", "",
             f"REFEREE review · {ident} · PDF sha256 `{p['sha256'][:12]}` · {code} · harness `{commit}`", "",
             "_A first-pass review aid for a human referee. It gives no accept or reject recommendation. Decisions are "
             "computed by the harness from the recorded tests; the plain-language explanations are model-written and "
             "were checked against the record._", ""]
    lines += ["## What the paper does", "", prose(rep.get("overview", ""), "overview") or "_(no overview)_", ""]
    verified = [cc for cc in claims if (cc.get("decision") or {}).get("decision") == VERIFIED]
    first = sum(1 for cc in claims if cc.get("id") and cc.get("origin") != "plan:2")
    added = sum(1 for cc in claims if cc.get("origin") == "plan:2")
    how = (f"{first} main claims were extracted from the paper before any test was planned"
           + (f"; {added} more were added by the follow-up round" if added else "") if first else
           f"{len(claims)} main claims were listed by the planner (no extraction is on record)")
    lines += ["## Decisions on the main claims", "",
              f"{how}. {len(verified)} verified; {len(claims) - len(verified)} not verified.", "",
              "| Claim | Decision | Reason |", "|---|---|---|"]
    for cc in claims:
        d = cc.get("decision") or {}
        lines.append(f"| {cc.get('id', '')}. {_short(cc.get('statement') or cc['quote'], 140)} | "
                     f"**{'Verified' if d.get('decision') == VERIFIED else 'Not verified'}** | "
                     f"{d.get('reason', '').replace('_', ' ')} |")
    open_ = [cc.get("id", "") for cc in claims if (cc.get("decision") or {}).get("decision") != VERIFIED]
    if open_:
        lines += ["", f"**Unresolved claims:** {', '.join(open_)}. Each is explained below."]
    lines.append("")
    expl = {str(e.get("id")): e.get("explanation", "") for e in rep.get("claims") or [] if isinstance(e, dict)}
    other = {str(e.get("id")): e for e in cmp.get("claims") or [] if isinstance(e, dict)}
    shown: dict = {}
    details: list[str] = []          # each test once, in full, after the claims: the main text stays near two pages
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
            lines.append("Assumptions as printed: " + "; ".join(f"\"{_short(a['quote'], 120)}\"" for a in cc["assumptions"][:3]))
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
            rows = [r for r in result_rows(c, limit=6) if not r.startswith("- counts:")]
            table = [r for r in rows if r.startswith("|")]
            lines += (["", *table, ""] if table else []) + ["  " + r for r in rows if not r.startswith("|")]
            if d.get("reason") == "changed_protocol":
                lines += [f"  - changed from the paper: {_sentence(u, 160)}" for u in (comp.get("changes") or [])[:2]]
            audit = audit_notes(c, prose)
            lines += [f"  {n}" if n.startswith("  ") else f"  - {n}" for n in audit[:2]]
            details += _details(c, x.root, prose, cites.get(c["id"], []), led)
        for c in sup:
            lines.append(f"- Supporting test (does not decide the claim): {_what(c)} — {_status_words(c)}.")
            if c["id"] not in shown:
                shown[c["id"]] = k
                details += _details(c, x.root, prose, cites.get(c["id"], []), led)
        if comp.get("scope_not_run"):
            lines.append("- **Not tested:** " + ", ".join(comp["scope_not_run"][:12])
                         + (" …" if len(comp["scope_not_run"]) > 12 else "") + ".")
        # Only a reason of its own is listed: an item whose test did not finish says so under "What ran" already.
        why_not = [b for b in comp.get("not_run") or [] if not (b.get("item", "").startswith("C") and b["item"][1:].isdigit())
                   and b.get("blocker") not in ("not run", "pending")]
        for b in why_not[:3]:
            lines.append(f"  - {b['item']}: {b['blocker']} — {_sentence(b.get('why'), 160)} "
                         f"({'harness record' if b.get('basis') == 'harness' else (b.get('basis') or 'planner') + ' says'})")
        if len(why_not) > 3:
            lines.append(f"  - {len(why_not) - 3} more items with a stated reason are in `ledger.json`.")
        if not tgt and cc.get("why_unchecked") and not any(flat_eq(b.get("why"), cc["why_unchecked"]) for b in why_not):
            lines.append(f"- **No test ran.** The planner's reason: {_sentence(cc['why_unchecked'], 300)}")
        o = other.get(k)
        if o:
            said = "; ".join(f"\"{_short(q, 160)}\"" for q in o.get("quotes") or [])
            lines.append(f"- **Other reproduction record ({_short(o.get('source', 'reference'), 60)}):** "
                         f"{prose(_short(o.get('reference_finding'), 300), k + ' reference finding')}"
                         + (f" In its words: {said}." if said else "")
                         + f" Comparable: {o.get('comparable', '?')} — {prose(o.get('why', ''), k + ' reference')} "
                         f"Agreement: {str(o.get('agreement', '')).replace('_', ' ')}.")
        if tgt + sup:
            lines.append("- Evidence: " + ", ".join(c["id"] for c in tgt + sup) + " (under Test details)")
        lines.append("")
    if details:
        lines += ["## Test details", "", "_Each test once: what it ran on, the paper/code differences and changes its "
                  "script declared, the full result counts, the independent audit, and its records._", "", *details]
    terms =[t for t in rep.get("terms") or [] if isinstance(t, dict) and t.get("term") and t.get("definition")][:12]
    if terms:
        lines += ["## Terms", ""] + [f"- **{_short(t['term'], 60)}**: {prose(_short(t['definition'], 300), 'term ' + str(t['term'])[:20])}"
                                     for t in terms] + [""]
    qs = [q for q in rep.get("open_questions") or [] if isinstance(q, str)][:5]
    if qs:
        lines += ["## Questions for the authors", ""] + [f"- {prose(_short(q, 300), 'question')}" for q in qs] + [""]
    lines += ["## Record", "",
              f"Full trace: `review.md`; machine ledger: `ledger.json`; every process: `execution.jsonl`. Workflow: "
              f"{led['workflow']['finished']} of {led['workflow']['checks_planned']} planned tests reached an end state "
              "(an end state is not a reproduction)."]
    if held:
        lines += ["", f"{len(held)} model-written passage(s) were withheld; why is recorded in `reviewer.withheld.json`."]
    text = "\n".join(lines) + "\n"
    (x.root / "reviewer.md").write_text(text, encoding="utf-8")
    state.write_json(x.root / "reviewer.withheld.json", held)
    return text


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
