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
    "contradicted": "The evidence contradicts the claim as printed; an independent audit found the failure rests on the "
                    "paper's own words.",
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
_ROWS = 8          # ponytail: result rows shown per check (the rest are counted, and all are in ledger.json)


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
    elif st == "FAILURE_FOUND":
        reason = "false_as_printed" if theory else "contradicted"
    elif st == "PROOF_GAP_FOUND":
        reason = "proof_step_invalid"
    elif st == "PREMISE_NOT_MET":
        reason = "premise_impossible"
    elif _failure_audit(tgt, "DEPENDS") or st == "READINGS_DISAGREE":
        reason = "interpretation_uncertain"
    elif st == "READING_CHANGED":
        undefined = any(c["kind"] == "CERTIFICATE" and ((c.get("literal") or {}).get("undefined") or 0) > 0
                        and c["status"] in ("NO_VIOLATION_FOUND", "VIOLATION_UNDER_CHANGED_READING") for c in tgt)
        reason = "notation_defect" if theory and undefined else "changed_protocol"
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
    elif any(b.get("basis") == "harness" and b.get("blocker") not in ("not run", "pending")
             for b in comp.get("not_run") or []):
        reason = "blocked"
    else:
        reason = "not_checked"
    return {"decision": VERIFIED if reason == "supported" else NOT_VERIFIED, "reason": reason,
            "reason_text": REASONS[reason], "because": _because(cc, tgt, reason, comp)}


def _because(cc: dict, tgt: list[dict], reason: str, comp: dict) -> list[str]:
    """The facts a decision rests on, in the harness's words, each with who says so."""
    out = []
    for c in tgt:
        out.append(f"{_what(c)}: {_status_words(c)}")
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
    if c.get("status_on_completed"):
        words += f" (on what completed: {c['status_on_completed'].lower().replace('_', ' ')})"
    if n.get("units_declared", 0) > 1:
        words += (f"; {n['units_with_result']} of {n['units_declared']} settings with a result"
                  + (f", {n['units_undefined']} undefined" if n.get("units_undefined") else "")
                  + (f", {n['units_not_completed']} missing" if n.get("units_not_completed") else ""))
    if st in ("BLOCKED", "NOT_CHECKABLE", "INCONCLUSIVE") and c.get("reason"):
        words += f" — {_short(c['reason'], 200)}"
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


def result_rows(c: dict) -> list[str]:
    """Markdown lines with the numbers a check produced: per stage (or per group of stages when there are many) the
    compared outputs' means, the margin of the stated comparison with its 95% band, the runs and how many of them were
    independent; for exact cases the counts; for a printed number the value produced beside it."""
    st, outs = c.get("stages") or {}, c.get("outputs") or {}
    t = c.get("target") or {}
    lines = []
    if c["kind"] == "CERTIFICATE":
        lit = {k: v for k, v in (c.get("literal") or {}).items() if v}
        n = len(c.get("values") or [])
        if n:
            lines.append(f"- {n} exact cases; {c.get('admissible', 0)} met every assumption of the tested reading"
                         + (f"; as printed: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in lit.items()) if lit else ""))
        return lines
    if c.get("readings") and isinstance(c["readings"], dict):
        for r, p in c["readings"].items():
            lines.append(f"- reading **{r}**: {p.get('status', '').lower().replace('_', ' ')}" + (
                f", value {fmt(p['reproduced'])}" if isinstance(p.get("reproduced"), (int, float)) else
                f", margin {fmt(p['margin'])}" if isinstance(p.get("margin"), (int, float)) else ""))
    decided = {s: p for s, p in st.items() if p.get("status") not in ("UNDEFINED", "NOT_COMPLETED")}
    rel = t.get("relation")
    if st and len(decided) <= _ROWS:
        head = list(dict.fromkeys(n for s in decided for n in (outs.get(s) or {})))[:4]
        lines.append("| Setting | " + " | ".join(head) + (" | margin ± 95% band" if rel else " | value") + " | runs (independent) | result |")
        lines.append("|---" * (len(head) + 4) + "|")
        for s, p in decided.items():
            o = outs.get(s) or {}
            val = (f"{fmt(p.get('margin'))} ± {fmt(p.get('band', 0))}" if rel and "margin" in p else
                   fmt(p.get("reproduced")) if "reproduced" in p else "")
            lines.append(f"| {s or '(all)'} | " + " | ".join(fmt(o.get(h)) if h in o else "" for h in head)
                         + f" | {val} | {p.get('n', '')} ({p.get('n_independent', p.get('n', ''))}) | "
                           f"{p['status'].lower().replace('_', ' ')} |")
    elif st:
        groups: dict = {}
        for s, p in decided.items():
            groups.setdefault(_group(s), []).append(p)
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
    ident = [f"{n} ({_short(json.dumps((i.get('observed') or {}), ensure_ascii=False)[:120], 120)})"
             for n, i in (c.get("data_identity") or {}).items() if isinstance(i, dict)][:3]
    if ident:
        parts.append("data: " + "; ".join(ident))
    elif fid.get("data"):
        parts.append("data: " + _short(fid["data"]["used"], 160))
    for a in ("model", "metric", "baselines"):
        if fid.get(a):
            parts.append(f"{a}: {_short(fid[a]['used'], 160)}")
    if c.get("covers"):
        parts.append("scope: " + ", ".join(c["covers"][:10]))
    n = c.get("counts") or {}
    if n.get("launches"):
        parts.append(f"{n['launches']} run(s)" + (" (" + c["runs_source"].replace("_", " ") + ")" if c.get("runs_source") else ""))
    return "; ".join(parts)


def fidelity_notes(c: dict) -> list[str]:
    """Where the paper and the released code disagree, or the paper is silent, as the script author stated it."""
    out = []
    for f in c.get("fidelity") or []:
        if f.get("agrees") is False:
            out.append(f"{f['aspect']}: paper and code differ — paper \"{_short(f.get('paper'), 120)}\"; code "
                       f"`{(f.get('code') or {}).get('file', '')}`; " + (f"both computed (reading {f['reading']})"
                                                                         if f.get("reading") else _short(f.get("explained"), 200)))
    for d in c.get("deviations") or []:
        if d.get("changes_claim"):
            out.append("changed from the paper: " + _short(d.get("used"), 200) + (f" (only in reading {d['reading']})"
                                                                                if d.get("reading") else ""))
    return out[:6]


def evidence_line(c: dict, led: dict) -> str:
    sha = (c.get("script_sha256") or "")[:12]
    return (f"{c['id']}" + (f" · script `{sha}`" if sha else "") + (f" · command `{_short(c['command'], 80)}`" if c.get("command") else "")
            + (" · records `execution.jsonl`" if c.get("records") else "") + (f" · repo commit `{c['commit'][:10]}`" if c.get("commit") else "")
            + f" · `checks/{c['id']}/outcome.json`")


# --- prose validation ---------------------------------------------------------------------------------------------
def numbers_of(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", re.sub(r"(?<=\d),(?=\d{3})", "", text or ""))]


def unknown_numbers(text: str, allowed: list[float]) -> list[str]:
    """Numbers in model prose that neither the record nor the paper holds (to the digits written). Small whole numbers
    (counts up to 12) are free; an id such as K3 or C7 is not a number."""
    bad, t = [], re.sub(r"(?<=\d),(?=\d{3})", "", text or "")
    for m in re.finditer(r"(?<![\w.\-])(\d+(?:\.(\d+))?)(%?)(?![\w])", t):
        if t[max(0, m.start() - 1):m.start()].isalpha():
            continue
        v, dec, pct = float(m.group(1)), len(m.group(2) or ""), m.group(3)
        if not dec and v <= 12:
            continue
        tol = 0.5 * 10 ** -dec + 1e-12
        cands = [v] + ([v / 100] if pct else [])
        if not any(_near(allowed, x, tol if x == v else tol / 100) for x in cands):
            bad.append(m.group(0))
    return bad


def _near(allowed: list[float], x: float, tol: float) -> bool:
    """Is some allowed number within `tol` of x (a rounding of it to the digits written)?"""
    i = bisect.bisect_left(allowed, x - tol)
    return i < len(allowed) and allowed[i] <= x + tol


def allowed_numbers(*texts: str) -> list[float]:
    vals = set()
    for t in texts:
        for v in numbers_of(t):
            vals.update((v, -v, abs(v)))
    return sorted(vals)


def problems(text: str, led: dict, paper, allowed: list[float]) -> list[str]:
    """Why a model-written passage cannot be published: a status word no cited check earns, a quote not in the paper, a
    number the record and the paper do not hold."""
    if not text:
        return []
    return ([f"status language: {s[:120]!r}" for s in report.unearned(_unquote(text), led)][:2]
            + [f"quote not in the paper: {q[:80]!r}" for q in report.unquoted(text, paper)][:2]
            + [f"number not in the record or the paper: {n}" for n in unknown_numbers(text, allowed)][:3])


def _unquote(text: str) -> str:
    """Text with its quoted spans removed: words inside quotation marks are someone else's (the paper's, a reference
    record's), not REFEREE's assertion."""
    return re.sub(r'"[^"\n]*"', " ", text or "")


# --- the page ------------------------------------------------------------------------------------------------------
def render(x, led: dict, rep: dict | None, cmp: dict | None = None) -> str:
    p, s = led["paper"], led["source"]
    rep, cmp = rep or {}, cmp or {}
    by_id = {c["id"]: c for c in led["checks"]}
    claims = led["central_claims"]
    det = json.dumps(led, ensure_ascii=False, default=str)
    allowed = allowed_numbers(det, "\n".join(x.paper.pages) if hasattr(x.paper, "pages") else "",
                              json.dumps(cmp, ensure_ascii=False))
    held: list[str] = []

    def prose(text: str, where: str) -> str:
        bad = problems(text, led, x.paper, allowed)
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
    lines += ["## Decisions on the main claims", "",
              f"{len(claims)} main claims were extracted from the paper before any test was planned. "
              f"{len(verified)} verified; {len(claims) - len(verified)} not verified.", "",
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
            lines.append(f"- **What ran:** {ran_line(c, x.root)}. Result: {_status_words(c)}.")
            lines += ["  " + r if not r.startswith("|") else r for r in result_rows(c)]
            lines += [f"  - {n}" for n in fidelity_notes(c)]
        for c in sup:
            lines.append(f"- Supporting test (does not decide the claim): {_what(c)} — {_status_words(c)}.")
        if comp.get("scope_not_run"):
            lines.append("- **Not tested:** " + ", ".join(comp["scope_not_run"][:12]) + ".")
        for b in (comp.get("not_run") or [])[:5]:
            if not (b.get("item", "").startswith("C") and b["item"][1:].isdigit()):
                lines.append(f"  - {b['item']}: {b['blocker']} — {_short(b.get('why'), 200)} "
                             f"({'harness record' if b.get('basis') == 'harness' else (b.get('basis') or 'planner') + ' says'})")
        if not tgt and cc.get("why_unchecked"):
            lines.append(f"- **No test ran.** The planner's reason: {_short(cc['why_unchecked'], 300)}")
        o = other.get(k)
        if o:
            lines.append(f"- **Other reproduction record ({o.get('source', 'reference')}):** {_short(o.get('reference_finding'), 300)} "
                         f"Comparable: {o.get('comparable', '?')} — {prose(o.get('why', ''), k + ' reference')} "
                         f"Agreement: {str(o.get('agreement', '')).replace('_', ' ')}.")
        ev = [evidence_line(c, led) for c in tgt + sup]
        if ev:
            lines.append("- Evidence: " + "; ".join(ev))
        lines.append("")
    terms = [t for t in rep.get("terms") or [] if isinstance(t, dict) and t.get("term") and t.get("definition")][:12]
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
